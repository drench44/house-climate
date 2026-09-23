import logging
import time
from datetime import datetime, timezone, timedelta

import requests

from . import db, deep_health, weather, ecowitt
from .analytics import humidity as humidity_an
from .daikin import DaikinClient, DeviceState, RateLimited, DaikinError, DaikinUnreachable
from .weather import WeatherSnapshot

log = logging.getLogger("house_climate.poller")

# Tracks the weather feed's last-seen health so we record a poll_error only on
# the ok->down transition (and log recovery on down->ok), instead of writing a
# row every tick for the whole outage. None = unknown (first poll).
_weather_ok_last = None


def build_reading(device_id, st: DeviceState, wx: WeatherSnapshot, now) -> dict:
    return dict(
        ts=now, device_id=device_id,
        indoor_temp_f=st.indoor_temp_f, indoor_humidity=st.indoor_humidity,
        heat_setpoint_f=st.heat_setpoint_f, cool_setpoint_f=st.cool_setpoint_f,
        equipment_status=st.equipment_status, mode=st.mode,
        daikin_outdoor_temp_f=st.outdoor_temp_f, daikin_outdoor_humidity=st.outdoor_humidity,
        wx_outdoor_temp_f=wx.outdoor_temp_f, wx_humidity=wx.humidity,
        wx_dewpoint_f=wx.dewpoint_f, wx_solar_wm2=wx.solar_wm2, wx_uv=wx.uv,
        wx_fc_high_f=wx.fc_high_f, wx_fc_low_f=wx.fc_low_f, wx_conditions=wx.conditions,
        wx_aqi=wx.aqi, wx_alert_count=wx.alert_count, weather_ok=wx.ok,
        wx_rain_today_in=wx.rain_today_in, wx_rain_source=wx.rain_source)


def build_weather_only_reading(device_id, wx: WeatherSnapshot, now) -> dict:
    """The tick's weather with every thermostat field left NULL, for a tick
    where the thermostat call failed. Weather lives only in readings rows, so
    without this a thermostat outage erased the outdoor history (and the rain
    gauge counter) for its whole length. The NULL equipment_status marks it as
    weather-only; thermostat consumers skip it (db.THERMOSTAT_ROW_SQL)."""
    empty = DeviceState(None, None, None, None, None, None, None, None)
    return build_reading(device_id, empty, wx, now)


def poll_once(conn, client, device_id, cfg) -> str:
    # Stamp the row with the time the WEATHER was sampled, not the time the
    # Daikin round-trip finished. The row's ts is what buckets wx fields
    # (especially the midnight-resetting rain counter) into local days; a
    # multi-second Daikin call straddling midnight would otherwise stamp
    # yesterday's final rain total onto the new day, permanently inflating it.
    now = datetime.now(timezone.utc)
    wx = weather.fetch(cfg.weather_url, cfg.weather_url_fallback)
    _note_weather_health(conn, device_id, wx.ok)
    try:
        st = client.read_device(device_id)
    except RateLimited:
        kind, detail = "daikin_429", "rate limited"
    except DaikinUnreachable as e:
        # No network path to Daikin (internet down, DNS, timeout). Recorded
        # as a 'daikin%' kind so the thermostat-offline alert counts it.
        kind, detail = "daikin_network", str(e)
    except DaikinError as e:
        kind, detail = "daikin_error", str(e)
    else:
        db.insert_reading(conn, build_reading(device_id, st, wx, now))
        return "ok"
    db.record_error(conn, device_id, kind, detail)
    # Keep the weather this tick already fetched. Nothing is stored when the
    # feed was down too: an all-NULL row would carry no information.
    if wx.ok:
        db.insert_reading(conn, build_weather_only_reading(device_id, wx, now))
    return kind


def _note_weather_health(conn, device_id, ok: bool) -> None:
    """Record a poll_error the moment the weather feed goes dark (and log when
    it comes back), so a multi-day outage is visible in /api/anomalies instead
    of silently blanking dew-point-dependent advice with no recorded reason.
    Recorded once per transition, not once per tick, to keep poll_errors sane.
    The 'weather_' kind is deliberately NOT 'daikin%' so it never feeds the
    thermostat-offline alert."""
    global _weather_ok_last
    if ok:
        if _weather_ok_last is False:
            log.info("weather feed recovered")
    elif _weather_ok_last is not False:  # first-seen-down or ok->down
        db.record_error(conn, device_id, "weather_error",
                        "weather feed unavailable (both primary and fallback)")
        log.warning("weather feed unavailable; recorded poll_error")
    _weather_ok_last = ok


def poll_ecowitt(conn, cfg) -> str:
    """Poll the local Ecowitt gateway (if configured) and store each room
    sensor's reading. Failures are logged as poll_errors and swallowed so a
    dead gateway never stops the Daikin poll."""
    ec = cfg.ecowitt
    if not ec or not ec.get("enabled"):
        return "ecowitt_off"
    # The whole fetch->parse->insert sequence is inside the guard: a parse
    # error or a DB failure partway through the insert loop must be recorded
    # (and swallowed), not left to escape to run()'s generic handler, which
    # would log "poll failed" but write no poll_error — making a broken Ecowitt
    # path look like idle sensor data instead of a failure.
    try:
        data = ecowitt.fetch_livedata(ec["gateway_url"])
        now = datetime.now(timezone.utc)
        rows = ecowitt.parse_channels(data, ec.get("channels", {}))
        outdoor_name = ec.get("outdoor_name")
        if outdoor_name:
            o = ecowitt.parse_outdoor(data, outdoor_name)
            if o:
                rows.append(o)
        signals = ecowitt.signal_by_sensor_id(ecowitt.fetch_sensors_info(ec["gateway_url"]))
        for s in rows:
            sig = signals.get(s["sensor_id"])
            db.insert_sensor_reading(conn, s["sensor_id"], now,
                                     temp_f=s["temp_f"], humidity=s["humidity"],
                                     battery=1.0 if s["battery_low"] else 0.0,
                                     extra={"signal": sig} if sig is not None else None,
                                     dewpoint_f=humidity_an.dew_point_f(s["temp_f"], s["humidity"]))
    except Exception as e:
        db.record_error(conn, "ecowitt", "ecowitt_fetch", str(e))
        return "ecowitt_error"
    return f"ecowitt_ok({len(rows)})"


# ---------------------------------------------------------------------------
# Daily rainfall maintenance for the moisture case.
#
# Primary source is the rain gauge: every reading stores the feed's
# cumulative rain-today counter and whether it came from the gauge or a
# forecast model, and the daily total is the max of the gauge counter per
# local day. Open-Meteo fills every day since just before sensor history began
# that has no final value, labeled 'openmeteo'. The precedence between a
# complete gauge day, Open-Meteo and the placeholders (partial gauge days,
# model estimates) lives in db.upsert_precip.
# ---------------------------------------------------------------------------

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_PRECIP_LOOKBACK_DAYS = 7   # backfill starts this far before the first sensor row
_precip_last_rollup = None   # monotonic time of the last station rollup
_precip_last_backfill_day = None  # local day the backfill last ran


def _consecutive_runs(days):
    """Split a sorted list of dates into runs of consecutive days."""
    runs = []
    for d in days:
        if runs and d - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(d)
        else:
            runs.append([d])
    return runs


def update_precip(conn, device_id, cfg) -> str:
    """Station rollup hourly (so today's rain lands same-day), Open-Meteo
    backfill check once per local day. The Open-Meteo backfill is guarded and
    swallowed (a missing internet connection only delays the one-time fill,
    retried tomorrow). A DB error in the station rollup is NOT swallowed here:
    it propagates to run()'s handler, which logs it and rebuilds the connection,
    so the loop survives but the failure is still recorded/visible."""
    global _precip_last_rollup, _precip_last_backfill_day
    from zoneinfo import ZoneInfo
    now_mono = time.monotonic()
    if _precip_last_rollup is not None and now_mono - _precip_last_rollup < 3600:
        return "precip_noop"
    _precip_last_rollup = now_mono
    today = datetime.now(ZoneInfo(cfg.timezone)).date()

    def rollup(days):
        """Upsert each day's rain. A gauge value is 'station' only for today
        (re-rolled all day, judged again tomorrow) or a PAST day whose gauge
        values reach into the late evening: the counter is cumulative, so a
        suffix of the day carries the full total, but a prefix (poller died
        at 10:00, rained all afternoon) is only a lower bound. Such a day is
        stored as 'station_partial', which also demotes the 'station' row the
        live rollup wrote for it, and the backfill then replaces it. A day
        with no gauge value at all stores the feed's model estimate as
        'model'. Both placeholders rank below Open-Meteo (db.upsert_precip)."""
        n = 0
        for d in days:
            gauge = d.get("rain_in")
            if gauge is not None:
                complete = d["day"] >= today or (d.get("rain_last_hour") or 0) >= 21
                db.upsert_precip(conn, d["day"], gauge,
                                 "station" if complete else "station_partial")
                n += 1
            elif d.get("model_rain_in") is not None:
                db.upsert_precip(conn, d["day"], d["model_rain_in"], "model")
        return n

    # 1) Hourly: recent days (cheap).
    n_recent = rollup(db.outdoor_daily(conn, device_id, cfg.timezone,
                                       since_ts=datetime.now(timezone.utc) - timedelta(days=3)))

    # 2) Once per local day: FULL station rollup (heals any day a >3-day
    #    outage pushed out of the hourly window) + gridded backfill for every
    #    past day without a final value: pre-station days (lag windows need
    #    rainfall from BEFORE the first crawl reading), partial gauge days,
    #    and model-only days.
    if _precip_last_backfill_day == today:
        return f"precip_ok(station={n_recent})"
    _precip_last_backfill_day = today
    rollup(db.outdoor_daily(conn, device_id, cfg.timezone))
    filled = 0
    if cfg.latitude is not None and cfg.longitude is not None:
        cur = conn.execute("SELECT min(ts) FROM sensor_readings")
        first = cur.fetchone()[0]
        if first is not None:
            start = (first - timedelta(days=_PRECIP_LOOKBACK_DAYS)).astimezone(
                ZoneInfo(cfg.timezone)).date()
            have = {p["day"] for p in db.precip_range(conn, since_day=start)
                    if p["source"] in db.PRECIP_FINAL_SOURCES}
            missing = []
            d = start
            while d < today:
                if d not in have:
                    missing.append(d)
                d += timedelta(days=1)
            # One request per run of consecutive missing days, newest first.
            # A single start..end request spanned every gap in between, and
            # once the rollup demotes an old partial day that range can reach
            # past what the endpoint serves; one failing run must not block
            # yesterday's heal. Each run is guarded and logged on its own and
            # retried tomorrow.
            failed = 0
            for run in reversed(_consecutive_runs(missing)):
                try:
                    r = requests.get(_OPEN_METEO_URL, params={
                        "latitude": cfg.latitude, "longitude": cfg.longitude,
                        "daily": "precipitation_sum", "precipitation_unit": "inch",
                        "timezone": cfg.timezone,
                        "start_date": run[0].isoformat(),
                        "end_date": run[-1].isoformat(),
                    }, timeout=15)
                    r.raise_for_status()
                    daily = r.json().get("daily", {})
                    for day_s, inches in zip(daily.get("time", []),
                                             daily.get("precipitation_sum", [])):
                        day = datetime.strptime(day_s, "%Y-%m-%d").date()
                        if day in have or inches is None:
                            continue
                        db.upsert_precip(conn, day, inches, "openmeteo")
                        filled += 1
                except Exception as e:
                    failed += 1
                    log.warning("open-meteo precip backfill failed for %s..%s"
                                " (will retry tomorrow): %s", run[0], run[-1], e)
            if failed:
                return (f"precip_station_only({n_recent}, backfilled={filled},"
                        f" failed_ranges={failed})")
    return f"precip_ok(station={n_recent}, backfilled={filled})"


def _discover_device_id(conn, client):
    """One boot-time device-discovery attempt. Returns the first device's id
    (upserting every returned device), or None if the account returned no
    devices. Raises DaikinError (DaikinUnreachable for a network failure) on
    a transient API/network failure so run() can distinguish "retry the call"
    from "no devices, check the account". Replaces a blind devices[0] that
    raised IndexError on an empty list."""
    devices = client.list_devices()
    if not devices:
        return None
    for d in devices:
        db.upsert_device(conn, d["id"], d["name"], d["model"])
    return devices[0]["id"]


# Boot-time discovery retry delay: doubles from MIN up to MAX, so an outage
# at startup neither hammers Daikin nor waits long once it is back.
_BOOT_RETRY_MIN_S = 10
_BOOT_RETRY_MAX_S = 600


def _boot_device_id(conn, client):
    """Resolve the thermostat to poll, retrying until it is known. Never
    raises for an API or network failure (that crash-looped the container).

    A transient failure with a device already in the database (a restart
    during an internet outage) returns that device at once, so the loop starts
    and the LAN sensors, heartbeat and poll_errors keep going; poll_once
    records the outage every tick. With no known device (first boot) there is
    nothing to attach readings to, so it retries with backoff. An empty
    device list, or any Daikin error that is not an outage, also retries:
    that is an account problem, and the poller healthcheck flags the missing
    heartbeat."""
    delay = _BOOT_RETRY_MIN_S
    while True:
        try:
            device_id = _discover_device_id(conn, client)
            if device_id is not None:
                return device_id
            log.error("Daikin account returned no devices; check credentials/"
                      "authorization. Retrying in %ss.", delay)
        except DaikinError as e:
            # Log the real cause. Distinct from the empty-list branch so we
            # don't tell the operator to "check credentials" when the API
            # just hiccuped. Only an outage (no network path, or rate
            # limited) falls back to the known device: any other failure,
            # such as a 401 from bad credentials, keeps retrying without a
            # heartbeat so the healthcheck still flags it.
            known = (db.latest_device_id(conn)
                     if isinstance(e, (DaikinUnreachable, RateLimited)) else None)
            if known is not None:
                log.error("could not list Daikin devices (%s); polling last known"
                          " device %s until Daikin answers", e, known)
                return known
            log.error("could not list Daikin devices (%s); retrying in %ss", e, delay)
        time.sleep(delay)
        delay = min(delay * 2, _BOOT_RETRY_MAX_S)


def run(cfg, secrets):
    logging.basicConfig(level=logging.INFO)
    # Carried in every heartbeat: the commit this poller was built from (the
    # deploy's build_info.json; None in a hand-built image) and when it
    # started, so /health/full can require the NEW poller to be the one
    # ticking and its readings to be newer than its start.
    started_at = datetime.now(timezone.utc).isoformat()
    build = deep_health.read_build_info() or {}
    commit = build.get("engine_commit")
    # built_at is unique per deploy, so a deploy that keeps the engine commit
    # (a config change) is still told apart from the poller it replaced.
    built_at = build.get("built_at")
    conn = db.connect(secrets.db_dsn)
    # The poller writes columns (dewpoint_f, wx_rain_today_in) that only exist
    # after the schema catch-up, and it can start before/without the web
    # service — so it must ensure the schema itself. Idempotent.
    db.ensure_app_schema(conn)
    client = DaikinClient(secrets.api_key, secrets.integrator_token, secrets.email)
    # Boot-time device discovery. Neither an empty list (misconfigured or
    # unauthorized account, or a location that legitimately returns []) nor a
    # transient API/network error must crash the process: indexing devices[0]
    # blindly raised an opaque IndexError with no poll_error, then Docker's
    # restart policy hot-looped it. Fail loud and retry instead.
    device_id = _boot_device_id(conn, client)
    log.info("polling device %s every %ss", device_id, cfg.poll_interval_s)
    while True:
        started = time.monotonic()
        try:
            # A long-lived connection can be dropped by a DB restart, network
            # blip, or idle timeout. Reopen before use so the poller self-heals
            # instead of throwing "connection is closed" on every tick forever.
            if conn.closed:
                log.warning("db connection closed; reconnecting")
                conn = db.connect(secrets.db_dsn)
            log.info("poll: %s", poll_once(conn, client, device_id, cfg))
            log.info("ecowitt: %s", poll_ecowitt(conn, cfg))
            precip = update_precip(conn, device_id, cfg)
            if precip != "precip_noop":
                log.info("precip: %s", precip)
            # Liveness heartbeat: proves the poller PROCESS is alive and looping,
            # independent of whether Daikin/weather are erroring (data staleness
            # is caught separately by /health + the offline alert). The poller
            # container's healthcheck (house_climate.healthcheck) probes its age.
            db.kv_set(conn, "poller_heartbeat",
                      {"ts": datetime.now(timezone.utc).isoformat(),
                       "commit": commit, "built_at": built_at,
                       "started_at": started_at})
        except Exception:                       # never let the loop die
            log.exception("poll failed")
            try:                                # replace a broken connection
                conn.close()
            except Exception:
                pass
            try:
                conn = db.connect(secrets.db_dsn)
                log.info("db reconnected")
            except Exception:
                log.exception("db reconnect failed; will retry next tick")
        time.sleep(max(1, cfg.poll_interval_s - (time.monotonic() - started)))
