import calendar
from datetime import datetime, timezone, timedelta, time
from zoneinfo import ZoneInfo
from .. import db
from ..analytics import runtime, cost, correlation, humidity, moisture, thermal, precool

_RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}

_PRECOOL_COOLING = {"cooling", "overcool"}
_ONPEAK_START = time(17, 0)
_ONPEAK_END = time(21, 0)
_PRECOOL_MAX_GAP_S = 600
_PRECOOL_MIN_FC_HIGH_F = 78
_PRECOOL_MIN_SHIFTABLE_KWH = 0.2

# Same 600s (10 min) staleness threshold build_now() uses to call a reading
# stale -- reused here so a dead poller/outage draws a short, honest gap in
# the activity strip instead of one giant block stretching across the outage.
_TIMELINE_MAX_GAP_S = 600


def _since(range_key):
    return datetime.now(timezone.utc) - _RANGES.get(range_key, _RANGES["24h"])


# A day counts toward the complete-day cost average / forecast fit only if its
# readings both span the day (first by ~2am, last by ~10pm) AND have no interior
# gap longer than this. Endpoint-only spanning let a day with a long mid-day
# poller outage pass as "complete"; cost.compute then gap-caps the missing
# runtime to zero, dragging that day's total -- and the avg/projection built on
# it -- silently low. 3h is comfortably above normal poll spacing yet flags a
# real multi-hour outage.
_COMPLETE_DAY_MAX_GAP_S = 3 * 3600


def _day_is_complete(drows, zone) -> bool:
    ts = sorted(r["ts"] for r in drows)
    if not ts:
        return False
    if ts[0].astimezone(zone).hour > 2 or ts[-1].astimezone(zone).hour < 22:
        return False
    return all((b - a).total_seconds() <= _COMPLETE_DAY_MAX_GAP_S
               for a, b in zip(ts, ts[1:]))


def _peak_mid_weekday_rates(cfg):
    """The two highest distinct weekday TOU rates as (peak, mid), derived from
    the rates themselves rather than hardcoded band NAMES so any utility works
    (a table whose bands are 'on-peak'/'part-peak' used to silently yield no
    pre-cool savings). Returns (None, None) if fewer than two distinct rates."""
    rates = sorted({b.rate for b in cfg.tou.bands if b.days in ("weekday", "all")})
    if len(rates) < 2:
        return None, None
    return rates[-1], rates[-2]


def build_now(conn, device_id) -> dict:
    rows = db.recent_readings(conn, device_id, datetime.now(timezone.utc) - timedelta(hours=1))
    if not rows:
        return {"stale": True}
    last = rows[-1]
    return {**{k: last[k] for k in (
        "indoor_temp_f", "indoor_humidity", "cool_setpoint_f", "heat_setpoint_f",
        "equipment_status", "mode", "wx_outdoor_temp_f", "wx_solar_wm2",
        "wx_conditions")},
        "ts": last["ts"].isoformat(),
        "age_s": (datetime.now(timezone.utc) - last["ts"]).total_seconds(),
        "stale": (datetime.now(timezone.utc) - last["ts"]).total_seconds() > 600,
        # Surface the weather feed's health so a dead feed is visible on the
        # "now" tile (dew-point advice silently vanishes otherwise). The column
        # is NOT NULL, and build_now only reads the freshly-written latest row,
        # so this is always the poller's real last verdict.
        "weather_ok": last["weather_ok"]}


def build_history(conn, device_id, range_key) -> list[dict]:
    if range_key == "30d":
        rows = db.hourly_readings(conn, device_id, _since("30d"))

        def eq(r):
            if (r.get("cool_ticks") or 0) > 0:
                return "cooling"
            if (r.get("heat_ticks") or 0) > 0:
                return "heating"
            if (r.get("fan_ticks") or 0) > 0:
                return "fan"
            return "idle"
        return [{"ts": r["bucket"].isoformat(), "indoor_temp_f": r["avg_indoor_temp_f"],
                 "indoor_humidity": r["avg_indoor_humidity"], "outdoor_temp_f": r["avg_outdoor_temp_f"],
                 "equipment_status": eq(r)} for r in rows]
    rows = db.recent_readings(conn, device_id, _since(range_key))
    return [{"ts": r["ts"].isoformat(), "indoor_temp_f": r["indoor_temp_f"],
             "indoor_humidity": r["indoor_humidity"],
             "outdoor_temp_f": r["wx_outdoor_temp_f"],
             "equipment_status": r["equipment_status"]} for r in rows]


def build_runtime(conn, device_id, cfg, days) -> dict:
    rows = db.recent_readings(conn, device_id, datetime.now(timezone.utc) - timedelta(days=days))
    res = runtime.compute(rows, short_cycle_min=cfg.short_cycle_minutes)
    return {"minutes": res.minutes, "cycle_count": len(res.cycles),
            "short_cycles": res.short_cycles,
            "short_cycles_setpoint_induced": res.short_cycles_setpoint_induced}


def build_cost(conn, device_id, cfg, days) -> dict:
    rows = db.recent_readings(conn, device_id, datetime.now(timezone.utc) - timedelta(days=days))
    try:
        res = cost.compute(rows, cfg.tou, cfg.system_kw, cfg.timezone, heat_kw=cfg.heat_kw)
    except ValueError as e:
        # An uncovered TOU minute makes band_for raise. Degrade this panel
        # rather than 500 /api/cost (panels share one fetch batch, so a 500
        # blanks the whole dashboard).
        return {"available": False, "reason": "tou_gap", "detail": str(e)}
    return {"available": True, "by_band": res.by_band,
            "total_dollars": round(res.total_dollars, 2),
            "total_kwh": round(res.total_kwh, 2),
            "pct_runtime_peak": round(res.pct_runtime_peak, 1)}


def build_cost_summary(conn, device_id, cfg, now=None) -> dict:
    try:
        return _cost_summary_impl(conn, device_id, cfg, now=now)
    except ValueError as e:
        # A TOU table with an uncovered minute (misconfig, DST edge, band typo)
        # makes band_for raise. Degrade this panel instead of 500ing
        # /api/cost/summary, which shares one fetch batch with the rest of the
        # dashboard (same reasoning build_precool_advice documents).
        return {"available": False, "reason": "tou_gap", "detail": str(e)}


def _cost_summary_impl(conn, device_id, cfg, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(cfg.timezone)
    rows = db.recent_readings(conn, device_id, now - timedelta(days=40))

    def costed(subset):
        res = cost.compute(subset, cfg.tou, cfg.system_kw, cfg.timezone, heat_kw=cfg.heat_kw)
        return res.total_dollars, res.total_kwh

    now_local = now.astimezone(zone)
    today_local_date = now_local.date()
    month_start_local = datetime(now_local.year, now_local.month, 1, tzinfo=zone)

    today_rows = [r for r in rows if r["ts"].astimezone(zone).date() == today_local_date]
    week_rows = [r for r in rows if r["ts"] >= now - timedelta(days=7)]
    mtd_rows = [r for r in rows if r["ts"] >= month_start_local]

    today_res = cost.compute(today_rows, cfg.tou, cfg.system_kw, cfg.timezone, heat_kw=cfg.heat_kw)
    today_dollars, today_kwh = today_res.total_dollars, today_res.total_kwh

    week_res = cost.compute(week_rows, cfg.tou, cfg.system_kw, cfg.timezone, heat_kw=cfg.heat_kw)
    week_dollars, week_kwh = week_res.total_dollars, week_res.total_kwh

    mtd_res = cost.compute(mtd_rows, cfg.tou, cfg.system_kw, cfg.timezone, heat_kw=cfg.heat_kw)
    mtd_dollars, mtd_kwh = mtd_res.total_dollars, mtd_res.total_kwh

    if mtd_res.by_band:
        by_band, pct_runtime_peak = mtd_res.by_band, mtd_res.pct_runtime_peak
    else:
        by_band, pct_runtime_peak = week_res.by_band, week_res.pct_runtime_peak

    # complete-day average: group all loaded rows by local calendar date, then
    # keep only PAST days whose readings span roughly the full day (first
    # reading by 2am local, last reading at/after 10pm local). This excludes
    # today's partial day and any partial first day at the start of history.
    by_date: dict = {}
    for r in rows:
        by_date.setdefault(r["ts"].astimezone(zone).date(), []).append(r)

    # The first calendar day of the 40-day load window is clipped at the
    # window edge — it can pass the completeness test while missing its
    # earliest hours, so exclude it outright.
    window_edge_day = (now - timedelta(days=40)).astimezone(zone).date()
    complete_day_dollars = []
    for d, drows in by_date.items():
        if d >= today_local_date or d <= window_edge_day:
            continue
        if _day_is_complete(drows, zone):
            dollars, _ = costed(drows)
            complete_day_dollars.append(dollars)

    complete_days = len(complete_day_dollars)
    avg_per_day_raw = (sum(complete_day_dollars) / complete_days) if complete_days else None
    avg_per_day = round(avg_per_day_raw, 2) if avg_per_day_raw is not None else None

    days_in_month = calendar.monthrange(now_local.year, now_local.month)[1]
    # Project from the rounded avg (the number a user actually sees), not the
    # raw mean, so "projected == avg_per_day * days_in_month" holds exactly.
    projected_month = round(avg_per_day * days_in_month, 2) if avg_per_day is not None else None

    # Current TOU band: always computable (even with zero readings), since it
    # depends only on the TOU table and the clock (now_local, above), not on
    # device data. Computed once and reused for both the live-accrual rate
    # below and the returned band_now/tier_now fields, so band_now is
    # populated even on an empty DB (it used to stay None there while
    # tier_now was already populated — an inconsistency).
    band_now, rate_now = cfg.tou.band_for(now_local)

    # Live-accrual: the latest reading's equipment state + current TOU band,
    # so the client can tick today's cost up between server refreshes at the
    # real accrual rate (kW x band rate) instead of guessing.
    if rows:
        latest = rows[-1]
        eq = latest["equipment_status"]
        # Staleness gate (same 600s rule as build_now): a poller that died
        # mid-cooling must not leave the dashboard ticking money upward for
        # a compressor that may have been off for hours.
        fresh = (now - latest["ts"]).total_seconds() <= 600
        running = fresh and eq in {"cooling", "overcool", "heating"}
        kw = cfg.system_kw if eq in {"cooling", "overcool"} else (cfg.heat_kw if eq == "heating" else 0.0)
        live_rate_per_hr = round(kw * rate_now, 4) if running else 0.0
        as_of = latest["ts"].isoformat()
    else:
        running = False
        live_rate_per_hr = 0.0
        as_of = None

    distinct_rates = sorted({b.rate for b in cfg.tou.bands})

    def _tier(rate):
        if rate is None or len(distinct_rates) <= 1:
            return "flat"
        if rate <= distinct_rates[0]:
            return "off"
        if rate >= distinct_rates[-1]:
            return "peak"
        return "mid"

    next_band, next_at = cfg.tou.next_transition(now_local)
    next_rate = None
    if next_at is not None:
        next_rate = cfg.tou.band_for(next_at)[1]

    return {
        "available": True,
        # today carries its own band split so the UI's breakdown can sum to
        # the "today" headline it sits under (the month split confused people).
        "today": {"dollars": round(today_dollars, 2), "kwh": round(today_kwh, 2),
                  "by_band": today_res.by_band},
        "week": {"dollars": round(week_dollars, 2), "kwh": round(week_kwh, 2)},
        "month_to_date": {"dollars": round(mtd_dollars, 2), "kwh": round(mtd_kwh, 2)},
        # Honesty fields: when tracking began (so the UI can say "since Aug 10"
        # instead of implying a full month), and the assumed compressor draw
        # behind every dollar figure (estimates, not measurements).
        "data_since": rows[0]["ts"].isoformat() if rows else None,
        "assumed_kw": cfg.system_kw,
        "avg_per_day": avg_per_day,
        "projected_month": projected_month,
        "complete_days": complete_days,
        "pct_runtime_peak": round(pct_runtime_peak, 1),
        "by_band": by_band,
        "tz": cfg.timezone,
        "as_of": as_of,
        "running": running,
        "band_now": band_now,
        "live_rate_per_hr": live_rate_per_hr,
        "rate_now": rate_now,
        "tier_now": _tier(rate_now),
        "next_band": next_band,
        "next_rate": next_rate,
        "next_tier": _tier(next_rate) if next_rate is not None else None,
        "next_change_at": next_at.isoformat() if next_at else None,
        "minutes_to_change": int((next_at - now_local).total_seconds() // 60) if next_at else None,
        # The rate the "on-peak" legend swatch should show, derived from the
        # TOU table rather than hardcoded, so it can't disagree with the
        # config-driven peak strip elsewhere on the page.
        "peak_rate": distinct_rates[-1] if distinct_rates else None,
        # The time window(s) the ribbon chart shades as "on-peak", derived
        # from whichever band(s) sit at the peak rate rather than a
        # hardcoded 17:00-21:00 — any utility's TOU shape shades correctly.
        # Weekend-only or season-gated-off bands never surface here (the
        # chart only ever needs weekday/all-days windows); a flat rate
        # (no distinct peak) yields [].
        "peak_windows": [
            {"start": b.start.strftime("%H:%M"), "end": b.end.strftime("%H:%M"),
             "weekday_only": b.days == "weekday"}
            for b in cfg.tou.bands
            if distinct_rates and b.rate == distinct_rates[-1] and b.days in ("weekday", "all")
        ],
    }


_AIRNOW_STALE_S = 1800   # AirNow is hourly + HA heartbeats every 5 min; 30 min silent = feed dead


def resolve_outdoor_aqi(conn, wx_aqi, now=None):
    """Resolve the effective outdoor AQI and its source. Prefers the fresher
    AirNow value Home Assistant pushes (kv "ha_outdoor_aqi") when it's present
    and not stale, else the weather feed's wx_aqi. Shared by the humidity panel
    and the air-quality alert so both agree on which number is authoritative.
    Returns (aqi, source) where source is "airnow" | "weather" | None."""
    now = now or datetime.now(timezone.utc)
    outdoor_aqi, aqi_source = wx_aqi, ("weather" if wx_aqi is not None else None)
    _an = db.kv_get(conn, "ha_outdoor_aqi")
    if _an is not None and isinstance(_an["value"], dict):
        age = (now - _an["updated_at"]).total_seconds()
        aqi_val = _an["value"].get("aqi")
        if aqi_val is not None and age <= _AIRNOW_STALE_S:
            outdoor_aqi, aqi_source = aqi_val, "airnow"
    return outdoor_aqi, aqi_source


def build_humidity(conn, device_id, cfg) -> dict:
    now = datetime.now(timezone.utc)
    rows = db.recent_readings(conn, device_id, now - timedelta(days=7))
    if not rows:
        return {"available": False}

    latest = rows[-1]
    indoor_rh = latest.get("indoor_humidity")
    indoor_dp = humidity.dew_point_f(latest.get("indoor_temp_f"), indoor_rh)
    outdoor_dp = latest.get("wx_dewpoint_f")
    outdoor_rh = latest.get("wx_humidity")
    outdoor_temp = latest.get("wx_outdoor_temp_f")

    dew_point_delta = None
    if indoor_dp is not None and outdoor_dp is not None:
        dew_point_delta = round(indoor_dp - outdoor_dp, 1)

    outdoor_aqi, aqi_source = resolve_outdoor_aqi(conn, latest.get("wx_aqi"))
    window = humidity.window_advice(indoor_dp, outdoor_dp, outdoor_temp, outdoor_aqi=outdoor_aqi)

    ac_stats = humidity.avg_rh_by_state(rows)
    ac_effect = None
    if (ac_stats["cooling_n"] >= humidity.AC_EFFECT_MIN_SAMPLES
            and ac_stats["idle_n"] >= humidity.AC_EFFECT_MIN_SAMPLES):
        ac_effect = {
            "cooling": round(ac_stats["cooling"], 1),
            "idle": round(ac_stats["idle"], 1),
            "drop": round(ac_stats["idle"] - ac_stats["cooling"], 1),
        }

    trend_rows = [r for r in rows if r["ts"] >= now - timedelta(hours=24)]
    step = 3 if len(trend_rows) > 60 else 1
    trend = []
    last_i = len(trend_rows) - 1
    for i, r in enumerate(trend_rows):
        if i % step != 0 and i != last_i:
            continue
        dp = humidity.dew_point_f(r.get("indoor_temp_f"), r.get("indoor_humidity"))
        trend.append({
            "ts": r["ts"].isoformat(),
            "indoor_rh": r.get("indoor_humidity"),
            "indoor_dp": round(dp, 1) if dp is not None else None,
            "outdoor_dp": r.get("wx_dewpoint_f"),
        })

    return {
        "available": True,
        "indoor_rh": indoor_rh,
        "indoor_dp": round(indoor_dp, 1) if indoor_dp is not None else None,
        "outdoor_dp": outdoor_dp,
        "outdoor_rh": outdoor_rh,
        "outdoor_aqi": outdoor_aqi,
        "aqi_source": aqi_source,
        "aqi_category": humidity.aqi_category(outdoor_aqi),
        "aqi_unhealthy": cfg.alerts.get("aqi_unhealthy", 101),
        "dew_point_delta": dew_point_delta,
        "window": window,
        "ac_effect": ac_effect,
        "trend": trend,
    }


_THERMAL_DAYS = 45   # window for envelope characterization; grows into it


def build_thermal(conn, device_id, cfg, now=None) -> dict:
    """Envelope characterization: passive coasting time constant + cooling load
    curve. Both sharpen as history accumulates; each carries its own readiness."""
    now = now or datetime.now(timezone.utc)
    rows = db.recent_readings(conn, device_id, now - timedelta(days=_THERMAL_DAYS))
    if not rows:
        return {"available": False}
    hours = (rows[-1]["ts"] - rows[0]["ts"]).total_seconds() / 3600.0
    pe = precool.effectiveness(rows, cfg.timezone)
    # Turn the minutes-of-peak-cooling saved into dollars, using the on-peak vs
    # mid-peak rate gap, so "what it saves" reads in money on the dashboard.
    if pe.get("ready") and pe.get("peak_min_saved_per_day"):
        peak_r, mid_r = _peak_mid_weekday_rates(cfg)
        if peak_r and mid_r and peak_r > mid_r:
            kwh = pe["peak_min_saved_per_day"] / 60.0 * cfg.system_kw
            pe["dollars_saved_per_day"] = round(kwh * (peak_r - mid_r), 2)
    # Last state HA pushed for the pre-cool toggle (see /api/ha/precool) —
    # lets the dashboard chip say "paused" from fact instead of inference.
    ha = db.kv_get(conn, "ha_precool")
    ha_precool = None
    if ha is not None and isinstance(ha["value"], dict):
        ha_precool = {"enabled": bool(ha["value"].get("enabled")),
                      "updated_at": ha["updated_at"].isoformat()}
    return {
        "available": True,
        "history_hours": round(hours, 1),
        "coasting": thermal.coasting_constant(rows),
        "load": thermal.cooling_load_curve(rows),
        "precool_eval": pe,
        "ha_precool": ha_precool,
    }


def build_rooms(conn, cfg, now=None) -> dict:
    """Latest Ecowitt room-sensor readings, one entry per configured channel.
    A channel with no reading yet (sensor not installed) is returned with
    present=False so the UI can show a placeholder (e.g. Crawl Space)."""
    ec = cfg.ecowitt
    if not ec or not ec.get("enabled"):
        return {"available": False}
    now = now or datetime.now(timezone.utc)

    def room_entry(sensor_id, name, channel):
        latest = db.latest_sensor_reading(conn, sensor_id)
        if latest is None or latest.get("ts") is None:
            return {"name": name, "channel": channel, "present": False}
        age = (now - latest["ts"]).total_seconds()
        extra = latest.get("extra") or {}
        return {
            "name": name, "channel": channel, "present": True,
            "temp_f": latest["temp_f"], "humidity": latest["humidity"],
            "battery_low": latest["battery"] is not None and latest["battery"] >= 1,
            "signal": extra.get("signal"),
            "age_s": int(age), "stale": age > 900,
        }

    rooms = [room_entry(f"ecowitt_ch{ch}", name, ch)
             for ch, name in ec.get("channels", {}).items()]
    # The single outdoor T&H sensor (WH32) — used here as the crawlspace probe.
    outdoor_name = ec.get("outdoor_name")
    if outdoor_name:
        rooms.append(room_entry("ecowitt_outdoor", outdoor_name, "outdoor"))
    return {"available": True, "rooms": rooms}


# Indoor PM2.5 (µg/m³) severity bands — EPA-derived: under 12 is the annual
# standard (good), 12-35 tracks the 24-hour standard (elevated), above 35 is
# genuinely bad air. The dashboard colors its chips off these.
PM25_ELEVATED = 12.0
PM25_BAD = 35.0
# HA heartbeats the push every 5 minutes even without changes; three missed
# heartbeats means the bridge (or HA) is down and the number can't be trusted.
_AIR_STALE_S = 900


def build_air(conn, now=None) -> dict:
    """Latest indoor PM2.5 per room, pushed by HA from the Levoit purifiers.
    Readings are taken AT each purifier, which cleans its own vicinity first —
    treat them as room-level indication, not certified µg/m³."""
    now = now or datetime.now(timezone.utc)
    rows = db.latest_air(conn)
    if not rows:
        return {"available": False}
    out = []
    for r in rows:
        age_s = (now - r["ts"]).total_seconds()
        out.append({
            "room": r["room"],
            "pm25": r["pm25"],
            "age_s": age_s,
            "stale": age_s > _AIR_STALE_S,
        })
    return {"available": True, "rooms": out,
            "thresholds": {"elevated": PM25_ELEVATED, "bad": PM25_BAD}}


def validate_aqi(aqi):
    """Validate an outdoor AQI value pushed by HA alongside room PM2.5:
    None (absent/null) is allowed and means "nothing to store"; otherwise
    it must be a non-bool int/float in 0-1000. Raises ValueError on an
    invalid value so the HTTP layer can turn it into a 422. Same validation
    style as the room pm25 check in ha_air_ep."""
    if aqi is None:
        return None
    if isinstance(aqi, bool) or not isinstance(aqi, (int, float)) \
            or not (0 <= float(aqi) <= 1000):
        raise ValueError("outdoor_aqi must be 0-1000 or null")
    return float(aqi)


def pop_and_store_aqi(body: dict, conn) -> None:
    """Pop the optional top-level "outdoor_aqi" out of an /api/ha/air body
    IN PLACE, so the caller's room loop never sees it as a room key, then
    validate and (if non-null) store it under kv key "ha_outdoor_aqi" as
    {"aqi": <float>} -- the exact contract the awareness/education surfaces
    read back via db.kv_get. Raises ValueError on an invalid value (bad
    type, out of 0-1000 range); the caller turns that into a 422. Null or
    absent stores nothing and is not an error."""
    aqi = validate_aqi(body.pop("outdoor_aqi", None))
    if aqi is not None:
        db.kv_set(conn, "ha_outdoor_aqi", {"aqi": aqi})


# Crawl-space RH doctrine (matches the dashboard's crawlRhClass): under 65%
# is healthy, 65-75% is the watch zone, over 75% sustained grows mold.
_CRAWL_RH_WATCH = 65
_CRAWL_RH_MOLD = 75
# Bucket sizes per range: small enough to keep real texture, large enough to
# keep the payload bounded (~100-250 points at every range).
_CRAWL_BUCKETS_S = {"24h": 900, "7d": 3600, "30d": 3 * 3600}
# Same gap clamp the timeline uses: never credit a threshold-crossing span
# for time nobody measured (sensor cadence is ~180s; 600s covers a missed
# push or two, an outage becomes uncounted time instead of invented hours).
_CRAWL_MAX_GAP_S = 600
_CRAWL_TREND_WINDOW_H = 3   # "rising/falling" compares the last 3h vs the 3h before


def _crawl_sensor_id(cfg):
    """Resolve which Ecowitt sensor is the crawl-space probe by its configured
    NAME, so re-plumbing the crawl onto a WH31 channel later is a config edit,
    not a code change. Today it is the WH32 in the gateway's outdoor slot."""
    ec = cfg.ecowitt or {}
    if not ec.get("enabled"):
        return None, None
    for ch, name in (ec.get("channels") or {}).items():
        if "crawl" in str(name).lower():
            return f"ecowitt_ch{ch}", name
    outdoor_name = ec.get("outdoor_name") or ""
    if "crawl" in outdoor_name.lower():
        return "ecowitt_outdoor", outdoor_name
    return None, None


def build_crawl(conn, device_id, cfg, range_key, now=None) -> dict:
    """Dedicated crawl-space humidity view: bucketed series for the chart,
    exact extremes for the high/low markers, gap-capped time above the mold
    thresholds, a short-horizon trend, and vent advice from the dew points."""
    now = now or datetime.now(timezone.utc)
    sensor_id, sensor_name = _crawl_sensor_id(cfg)
    if sensor_id is None:
        return {"available": False, "reason": "not_configured"}

    since = now - _RANGES.get(range_key, _RANGES["24h"])
    range_key = range_key if range_key in _RANGES else "24h"
    rows = db.sensor_readings_range(conn, sensor_id, since)
    rh_rows = [r for r in rows if r.get("humidity") is not None]
    if not rh_rows:
        return {"available": False, "reason": "no_data"}

    series = [{
        "ts": s["bucket"].isoformat(),
        "rh_avg": round(s["rh_avg"], 1) if s["rh_avg"] is not None else None,
        "rh_min": s["rh_min"], "rh_max": s["rh_max"],
        "temp_avg": round(s["temp_avg"], 1) if s["temp_avg"] is not None else None,
    } for s in db.sensor_series(conn, sensor_id, since, _CRAWL_BUCKETS_S[range_key])]

    latest = rows[-1]
    age_s = (now - latest["ts"]).total_seconds()
    rh_now = latest.get("humidity")
    temp_now = latest.get("temp_f")
    dew_now = humidity.dew_point_f(temp_now, rh_now)

    high = max(rh_rows, key=lambda r: r["humidity"])
    low = min(rh_rows, key=lambda r: r["humidity"])
    rh_avg = sum(r["humidity"] for r in rh_rows) / len(rh_rows)
    temps = [r["temp_f"] for r in rows if r.get("temp_f") is not None]

    # Time above each threshold, crediting each reading with the gap to the
    # next one (capped) — the same honesty rule the activity strip follows.
    hours_total = hours_65 = hours_75 = 0.0
    for i, r in enumerate(rh_rows):
        nxt = rh_rows[i + 1]["ts"] if i + 1 < len(rh_rows) else min(now, r["ts"] + timedelta(seconds=_CRAWL_MAX_GAP_S))
        h = min((nxt - r["ts"]).total_seconds(), _CRAWL_MAX_GAP_S) / 3600.0
        if h <= 0:
            continue
        hours_total += h
        if r["humidity"] > _CRAWL_RH_WATCH:
            hours_65 += h
        if r["humidity"] > _CRAWL_RH_MOLD:
            hours_75 += h

    # Trend: mean RH of the last 3h vs the 3h before it. Needs real samples
    # in both windows, otherwise stay silent rather than guess.
    trend = None
    recent = [r["humidity"] for r in rh_rows if r["ts"] >= now - timedelta(hours=_CRAWL_TREND_WINDOW_H)]
    prior = [r["humidity"] for r in rh_rows
             if now - timedelta(hours=2 * _CRAWL_TREND_WINDOW_H) <= r["ts"] < now - timedelta(hours=_CRAWL_TREND_WINDOW_H)]
    if len(recent) >= 3 and len(prior) >= 3:
        delta = sum(recent) / len(recent) - sum(prior) / len(prior)
        direction = "rising" if delta > 1 else ("falling" if delta < -1 else "steady")
        trend = {"dir": direction, "delta": round(delta, 1), "window_h": _CRAWL_TREND_WINDOW_H}

    # Vent advice: would outside air dry the crawl or wet it? Compare dew
    # points (absolute moisture), and flag the condensation case — outdoor
    # dew point at/above the crawl temperature means outside air fogs the
    # crawl's cold surfaces no matter what the RH numbers say.
    dev_rows = db.recent_readings(conn, device_id, now - timedelta(hours=1))
    outdoor_dp = dev_rows[-1].get("wx_dewpoint_f") if dev_rows else None
    vent = None
    if outdoor_dp is not None and dew_now is not None:
        if temp_now is not None and outdoor_dp >= temp_now - 2:
            vent = {"action": "keep_closed",
                    "reason": "Outside air would condense on crawl surfaces — keep vents closed."}
        elif outdoor_dp <= dew_now - 5:
            vent = {"action": "vent",
                    "reason": "Outside air is much drier — venting would dry the crawl."}
        else:
            vent = {"action": "neutral",
                    "reason": "Outside air is about as damp as the crawl — venting changes little."}

    return {
        "available": True,
        "range": range_key,
        "sensor": sensor_name,
        "rh_now": rh_now, "temp_now": temp_now,
        "dew_now": round(dew_now, 1) if dew_now is not None else None,
        "age_s": int(age_s), "stale": age_s > 900,
        "rh_high": {"v": high["humidity"], "ts": high["ts"].isoformat()},
        "rh_low": {"v": low["humidity"], "ts": low["ts"].isoformat()},
        "rh_avg": round(rh_avg, 1),
        "temp_high": max(temps) if temps else None,
        "temp_low": min(temps) if temps else None,
        "hours_total": round(hours_total, 1),
        "hours_above_65": round(hours_65, 1),
        "hours_above_75": round(hours_75, 1),
        "thresholds": {"watch": _CRAWL_RH_WATCH, "mold": _CRAWL_RH_MOLD},
        "trend": trend,
        "outdoor_dp": outdoor_dp,
        "vent": vent,
        "data_start": rows[0]["ts"].isoformat(),
        "series": series,
    }


def _reference_sensor_id(cfg):
    """The indoor sensor the crawl is compared against for the dew-point
    delta. Prefer a channel named like 'downstairs' (the floor directly above
    the crawl), else the first configured non-crawl channel."""
    ec = cfg.ecowitt or {}
    channels = ec.get("channels") or {}
    for ch, name in channels.items():
        if "down" in str(name).lower():
            return f"ecowitt_ch{ch}", name
    for ch, name in channels.items():
        if "crawl" not in str(name).lower():
            return f"ecowitt_ch{ch}", name
    return None, None


def build_moisture(conn, device_id, cfg, now=None) -> dict:
    """The whole moisture case in one payload: dew points everywhere, the
    crawl-to-indoor delta, source attribution, condensation risk, rainfall
    lag correlation, threshold counters, intervention comparisons, and the
    winter projection. Each analytic carries its own readiness gate."""
    now = now or datetime.now(timezone.utc)
    sensor_id, sensor_name = _crawl_sensor_id(cfg)
    if sensor_id is None:
        return {"available": False, "reason": "not_configured"}
    tz = cfg.timezone

    daily = db.sensor_daily_stats(conn, sensor_id, tz)
    if not daily:
        return {"available": False, "reason": "no_data"}
    outdoor_days = db.outdoor_daily(conn, device_id, tz)
    precip = db.precip_range(conn)
    precip_by_day = {p["day"]: p["inches"] for p in precip}
    ref_id, ref_name = _reference_sensor_id(cfg)

    # --- current dew points, one per sensor (plus outdoor + thermostat) ---
    def latest_dp(sid):
        r = db.latest_sensor_reading(conn, sid)
        if r is None:
            return None
        dp = humidity.dew_point_f(r.get("temp_f"), r.get("humidity"))
        return round(dp, 1) if dp is not None else None

    dev_rows = db.recent_readings(conn, device_id, now - timedelta(hours=1))
    latest_dev = dev_rows[-1] if dev_rows else {}
    dp_now = {
        "crawl": latest_dp(sensor_id),
        "reference": latest_dp(ref_id) if ref_id else None,
        "reference_name": ref_name,
        "outdoor": latest_dev.get("wx_dewpoint_f"),
        "thermostat": (lambda v: round(v, 1) if v is not None else None)(
            humidity.dew_point_f(latest_dev.get("indoor_temp_f"),
                                 latest_dev.get("indoor_humidity"))),
    }

    # --- crawl-to-indoor dew point delta, 7d hourly series ---
    crawl_h30 = db.sensor_hourly_dp(conn, sensor_id, now - timedelta(days=30))
    outdoor_h30 = db.outdoor_hourly_dp(conn, device_id, now - timedelta(days=30))
    since7 = now - timedelta(days=7)
    ref_h7 = db.sensor_hourly_dp(conn, ref_id, since7) if ref_id else []
    ref_by_bucket = {r["bucket"]: r["dp"] for r in ref_h7}
    out_by_bucket = {r["bucket"]: r["dp"] for r in outdoor_h30}
    delta_series = []
    for c in crawl_h30:
        if c["bucket"] < since7:
            continue
        ref_dp = ref_by_bucket.get(c["bucket"])
        delta_series.append({
            "ts": c["bucket"].isoformat(),
            "crawl": round(c["dp"], 1) if c["dp"] is not None else None,
            "indoor": round(ref_dp, 1) if ref_dp is not None else None,
            "outdoor": (lambda v: round(v, 1) if v is not None else None)(
                out_by_bucket.get(c["bucket"])),
            "delta": (round(c["dp"] - ref_dp, 1)
                      if c["dp"] is not None and ref_dp is not None else None),
        })
    delta_now = None
    if dp_now["crawl"] is not None and dp_now["reference"] is not None:
        delta_now = round(dp_now["crawl"] - dp_now["reference"], 1)

    # --- source attribution ---
    w7 = moisture.attribution_window(crawl_h30, outdoor_h30, now, 7,
                                     moisture.ATTR_MIN_HOURS_7D)
    w30 = moisture.attribution_window(crawl_h30, outdoor_h30, now, 30,
                                      moisture.ATTR_MIN_HOURS_30D)
    verdict = moisture.attribution_verdict(w7, w30)

    # --- condensation + rain + thresholds + interventions + projection ---
    cond = moisture.condensation_summary(daily, outdoor_days, daily)
    rain = moisture.rain_lag_correlation(precip_by_day, daily)
    thresholds = moisture.threshold_rollups(daily)
    iv_report = moisture.intervention_report(daily, db.list_interventions(conn),
                                             outdoor_days=outdoor_days)
    projection = moisture.winter_projection(daily, outdoor_days)

    # --- last-30-day daily table (charts + report), rain joined in ---
    src_by_day = {p["day"]: p["source"] for p in precip}
    daily_out = [{
        "day": d["day"].isoformat(),
        "rh_min": d["rh_min"], "rh_max": d["rh_max"],
        "rh_mean": round(d["rh_mean"], 1) if d["rh_mean"] is not None else None,
        "dp_min": (round(d["dp_min"], 1) if d["dp_min"] is not None else None),
        "dp_max": (round(d["dp_max"], 1) if d["dp_max"] is not None else None),
        "dp_mean": (round(d["dp_mean"], 1) if d["dp_mean"] is not None else None),
        "h60": round(d["h60"], 1), "h70": round(d["h70"], 1), "h80": round(d["h80"], 1),
        "cond_h": round(d["cond_h"], 1), "obs_h": round(d["obs_h"], 1),
        "rain_in": precip_by_day.get(d["day"]),
        "rain_source": src_by_day.get(d["day"]),
    } for d in daily[-30:]]

    return {
        "available": True, "tz": tz, "sensor": sensor_name,
        "data_start": daily[0]["day"].isoformat(),
        "generated": now.isoformat(),
        "dp_now": dp_now,
        "delta": {"now": delta_now, "series": delta_series},
        "attribution": {"r7": w7, "r30": w30,
                        "verdict": {"source": verdict[0], "text": verdict[1]}
                        if verdict else None},
        "condensation": cond,
        "rain": {**rain,
                 "days": [{"day": p["day"].isoformat(), "inches": p["inches"],
                           "source": p["source"]} for p in precip[-35:]]},
        "thresholds": thresholds,
        "daily": daily_out,
        "interventions": iv_report,
        "projection": projection,
    }


def build_crawl_csv(conn, cfg) -> str:
    """Every stored crawl reading as CSV — the raw evidence export."""
    sensor_id, _ = _crawl_sensor_id(cfg)
    if sensor_id is None:
        return "ts,temp_f,humidity,dewpoint_f\n"
    rows = db.sensor_readings_range(conn, sensor_id, datetime(2000, 1, 1, tzinfo=timezone.utc))
    lines = ["ts,temp_f,humidity,dewpoint_f"]
    for r in rows:
        dp = r.get("dewpoint_f")
        lines.append(f"{r['ts'].isoformat()},{r.get('temp_f') if r.get('temp_f') is not None else ''},"
                     f"{r.get('humidity') if r.get('humidity') is not None else ''},"
                     f"{round(dp, 2) if dp is not None else ''}")
    return "\n".join(lines) + "\n"


def build_precip_csv(conn) -> str:
    lines = ["day,inches,source"]
    for p in db.precip_range(conn):
        lines.append(f"{p['day'].isoformat()},{p['inches']},{p['source']}")
    return "\n".join(lines) + "\n"


def build_forecast(conn, device_id, cfg, days=14) -> dict:
    rows = db.recent_readings(conn, device_id, datetime.now(timezone.utc) - timedelta(days=days))
    if not rows:
        return {"available": False}
    zone = ZoneInfo(cfg.timezone)
    today_local = datetime.now(zone).date()
    by_day = {}
    for r in rows:
        d = r["ts"].astimezone(zone).date()
        by_day.setdefault(d, []).append(r)
    history = []
    for d, drows in by_day.items():
        # Complete past days only: today's partial day would enter the fit as
        # "a cool day that needed no cooling" (the 6am high is the overnight
        # low!), and the clipped first day of the window understates both
        # axes. Same completeness rule the cost averages use.
        if d >= today_local:
            continue
        if not _day_is_complete(drows, zone):
            continue
        highs = [x["wx_outdoor_temp_f"] for x in drows if x.get("wx_outdoor_temp_f") is not None]
        if not highs:
            continue
        cool_min = runtime.compute(drows, short_cycle_min=cfg.short_cycle_minutes).minutes["cool"]
        peak_rows = [x for x in drows if 17 <= x["ts"].astimezone(zone).hour < 21]
        peak_min = (runtime.compute(peak_rows, short_cycle_min=cfg.short_cycle_minutes)
                    .minutes["cool"] if peak_rows else 0.0)
        history.append({"day_high": max(highs), "cool_minutes": cool_min,
                        "peak_cool_minutes": peak_min})
    fc_high = rows[-1].get("wx_fc_high_f")
    if fc_high is None or not history:
        return {"available": False}
    tomorrow = (datetime.now(zone) + timedelta(days=1)).date()
    pred = correlation.predict_peak_cost(fc_high, history, cfg.tou, cfg.system_kw, cfg.timezone, target_date=tomorrow)
    cdd = correlation.cooling_degree_days(rows, tz=cfg.timezone)
    return {"available": True, "fc_high_f": fc_high, "target_date": tomorrow.isoformat(),
            "predicted_cool_minutes": round(pred["predicted_cool_minutes"]),
            "predicted_peak_cool_minutes": round(pred["predicted_peak_cool_minutes"]),
            "predicted_peak_dollars": round(pred["predicted_peak_dollars"], 2),
            "peak_band": pred["peak_band"],
            "basis": pred["basis"], "cooling_degree_days_recent": round(cdd, 1),
            "days_of_history": len(history)}


def build_precool_advice(conn, device_id, cfg, now=None) -> dict:
    """Estimate $ saved by pre-cooling before the example TOU schedule's 5-9pm
    weekday on-peak window. Pre-cooling before 5pm shifts cooling load from on-peak into
    mid-peak (7am-5pm weekday) -- NOT off-peak, which isn't reachable before
    5pm on a weekday -- so the honest savings rate is peak_rate - mid_rate."""
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(cfg.timezone)
    rows = db.recent_readings(conn, device_id, now - timedelta(days=14))
    if not rows:
        return {"relevant": False, "reason": "collecting"}

    rows = sorted(rows, key=lambda r: r["ts"])
    onpeak_days = set()
    onpeak_cool_kwh = 0.0
    for i, row in enumerate(rows):
        local = row["ts"].astimezone(zone)
        if local.weekday() >= 5:
            continue
        if not (_ONPEAK_START <= local.time() < _ONPEAK_END):
            continue
        onpeak_days.add(local.date())
        if row["equipment_status"] not in _PRECOOL_COOLING:
            continue
        dt = (rows[i + 1]["ts"] - row["ts"]).total_seconds() if i + 1 < len(rows) else 0
        mins = min(dt, _PRECOOL_MAX_GAP_S) / 60.0
        if mins <= 0:
            continue
        onpeak_cool_kwh += mins / 60.0 * cfg.system_kw

    if not onpeak_days:
        return {"relevant": False, "reason": "collecting"}

    avg_peak_cool_kwh = onpeak_cool_kwh / len(onpeak_days)

    fc_high = rows[-1].get("wx_fc_high_f")
    # Derive the peak/mid rates from the two highest weekday rates (generic;
    # not hardcoded band names). Default to 0.0 (not None) so a single-rate or
    # missing table can never raise a TypeError here — a 500 on /api/precool
    # would blank the whole dashboard, since every panel shares one fetch batch.
    # No rate gap degrades to "not relevant" via the guard below.
    peak_rate, mid_rate = _peak_mid_weekday_rates(cfg)
    peak_rate = peak_rate or 0.0
    mid_rate = mid_rate or 0.0

    if fc_high is None or fc_high < _PRECOOL_MIN_FC_HIGH_F:
        return {"relevant": False, "reason": "mild", "fc_high": fc_high}
    if avg_peak_cool_kwh < _PRECOOL_MIN_SHIFTABLE_KWH:
        return {"relevant": False, "reason": "low_peak_use"}
    if peak_rate <= mid_rate:
        return {"relevant": False, "reason": "no_rate_gap"}

    savings = avg_peak_cool_kwh * (peak_rate - mid_rate)
    return {
        "relevant": True,
        "fc_high": fc_high,
        "shiftable_kwh": round(avg_peak_cool_kwh, 2),
        "peak_rate": peak_rate,
        "mid_rate": mid_rate,
        "savings": round(savings, 2),
    }


def build_timeline(conn, device_id, cfg, hours=24) -> dict:
    """"Today's Story": the equipment_status history for the last `hours` as
    merged run-length segments (for a horizontal activity strip), plus every
    setpoint change in that window.

    Segments merge consecutive readings that share equipment_status, but only
    across gaps no bigger than _TIMELINE_MAX_GAP_S -- a same-status reading
    arriving after a bigger gap (poller outage, etc.) starts a NEW segment
    rather than silently absorbing the outage into one block. Any gap bigger
    than the clamp -- including the trailing gap from the last reading to
    "now" -- is truncated to the clamp, leaving a visible hole in the strip
    instead of drawing a block for a span with no evidence behind it.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=hours)
    rows = db.recent_readings(conn, device_id, window_start)
    if not rows:
        return {"available": False}
    rows = sorted(rows, key=lambda r: r["ts"])

    segments = []
    cur = None  # {"status", "start", "end"}
    for i, row in enumerate(rows):
        status = row.get("equipment_status") or "unknown"
        ts = row["ts"]
        next_boundary = rows[i + 1]["ts"] if i + 1 < len(rows) else now
        gap_s = (next_boundary - ts).total_seconds()
        span_end = ts + timedelta(seconds=min(gap_s, _TIMELINE_MAX_GAP_S))

        if cur is not None and cur["status"] == status and cur["end"] == ts:
            cur["end"] = span_end
        else:
            if cur is not None:
                segments.append(cur)
            cur = {"status": status, "start": ts, "end": span_end}
    if cur is not None:
        segments.append(cur)

    seg_out = [{
        "status": s["status"],
        "start": s["start"].isoformat(),
        "end": s["end"].isoformat(),
        "minutes": round((s["end"] - s["start"]).total_seconds() / 60.0, 1),
    } for s in segments]

    setpoint_changes = []
    prev = None
    for row in rows:
        if prev is not None and (
                row["cool_setpoint_f"] != prev["cool_setpoint_f"]
                or row["heat_setpoint_f"] != prev["heat_setpoint_f"]):
            setpoint_changes.append({
                "ts": row["ts"].isoformat(),
                "cool": row["cool_setpoint_f"],
                "heat": row["heat_setpoint_f"],
                "mode": row["mode"],
                "prev_cool": prev["cool_setpoint_f"],
                "prev_heat": prev["heat_setpoint_f"],
            })
        prev = row

    return {
        "available": True,
        "hours": hours,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "segments": seg_out,
        "setpoint_changes": setpoint_changes,
        "tz": cfg.timezone,
    }


_HEALTH_HISTORY_DAYS = 400  # "all available history" for the filter clock, bounded to a sane query window
_HOLD_WINDOW_DAYS = 14


def filter_status(conn, device_id, cfg, rows_all=None, now=None) -> dict:
    """Cumulative HVAC-running-hours since the last logged filter change vs the
    reminder threshold. Shared by the System Health panel and the filter-due
    alert so both use the identical clock. Loads its own ~400-day window if the
    caller doesn't supply rows_all."""
    now = now or datetime.now(timezone.utc)
    if rows_all is None:
        rows_all = db.recent_readings(conn, device_id, now - timedelta(days=_HEALTH_HISTORY_DAYS))
    changed_at = db.latest_filter_change(conn, device_id)
    filter_rows = ([r for r in rows_all if r["ts"] >= changed_at]
                   if changed_at is not None else rows_all)
    rt_filter = runtime.compute(filter_rows, short_cycle_min=cfg.short_cycle_minutes)
    running_minutes = rt_filter.minutes["cool"] + rt_filter.minutes["heat"] + rt_filter.minutes["fan"]
    runtime_hours = running_minutes / 60.0
    threshold = cfg.filter_reminder_hours
    due = runtime_hours >= threshold
    pct = round(min(runtime_hours / threshold, 1.0) * 100, 0) if threshold > 0 else 0.0
    days_since = (now - changed_at).days if changed_at is not None else None
    return {
        "runtime_hours": round(runtime_hours, 1),
        "threshold": threshold,
        "due": due,
        "pct": pct,
        "changed_at": changed_at.isoformat() if changed_at is not None else None,
        "days_since": days_since,
    }


def build_health(conn, device_id, cfg, now=None) -> dict:
    """System Health panel: setpoint hold tightness, short-cycling, and a
    cumulative HVAC-runtime filter reminder.

    One query loads ~400 days of history; the 14-day hold/short-cycling
    window is sliced from that in memory rather than issued as a second
    query.
    """
    now = now or datetime.now(timezone.utc)
    rows_all = db.recent_readings(conn, device_id, now - timedelta(days=_HEALTH_HISTORY_DAYS))
    if not rows_all:
        return {"available": False}

    since_14d = now - timedelta(days=_HOLD_WINDOW_DAYS)
    rows_14d = [r for r in rows_all if r["ts"] >= since_14d]

    # --- Hold tightness: deviation from the ACTIVE target by mode.
    # cool -> cool setpoint; heat/emheat -> heat setpoint; auto -> the
    # [heat, cool] BAND (inside the band is a perfect hold — comparing auto
    # against the cool setpoint alone would report a winter house held
    # exactly at its heat setpoint as an 8-degree failure). "off"/unknown
    # modes have no target and are skipped.
    abs_devs = []
    for r in rows_14d:
        mode = r.get("mode")
        indoor = r.get("indoor_temp_f")
        if indoor is None:
            continue
        if mode == "cool":
            sp = r.get("cool_setpoint_f")
            if sp is None:
                continue
            abs_devs.append(abs(indoor - sp))
        elif mode in ("heat", "emheat"):
            sp = r.get("heat_setpoint_f")
            if sp is None:
                continue
            abs_devs.append(abs(indoor - sp))
        elif mode == "auto":
            hsp, csp = r.get("heat_setpoint_f"), r.get("cool_setpoint_f")
            if hsp is None or csp is None:
                continue
            if indoor < hsp:
                abs_devs.append(hsp - indoor)
            elif indoor > csp:
                abs_devs.append(indoor - csp)
            else:
                abs_devs.append(0.0)

    if abs_devs:
        within = sum(1 for d in abs_devs if d <= cfg.setpoint_tolerance_f)
        hold = {
            "avg_abs_dev": round(sum(abs_devs) / len(abs_devs), 2),
            "max_abs_dev": round(max(abs_devs), 2),
            "pct_within_tol": round(within / len(abs_devs) * 100, 1),
        }
    else:
        hold = None

    # --- Short-cycling over the same 14-day window. "Healthy" follows the
    # same convention the Runtime panel already uses (app.js: short_cycles >
    # 0 -> warn) -- any short cycle at all means not healthy.
    rt_14d = runtime.compute(rows_14d, short_cycle_min=cfg.short_cycle_minutes)
    short_cycles = rt_14d.short_cycles
    short_cycles_induced = rt_14d.short_cycles_setpoint_induced
    short_cycles_healthy = short_cycles == 0

    # --- Filter reminder: cumulative RUNNING minutes (cool + heat + fan
    # buckets, which already fold cooling/overcool together) since the filter
    # was last changed. The clock starts at the most recent filter_events row;
    # with no logged change it falls back to all loaded history ("hours since
    # we started recording"). Note the runtime clock can only count runtime we
    # actually tracked, so a change logged before recording began shows a real
    # calendar age but a low hours figure until new runtime accrues.
    filter_info = filter_status(conn, device_id, cfg, rows_all=rows_all, now=now)

    return {
        "available": True,
        "hold": hold,
        "tolerance": cfg.setpoint_tolerance_f,
        "short_cycles": short_cycles,
        "short_cycles_setpoint_induced": short_cycles_induced,
        "short_cycles_healthy": short_cycles_healthy,
        "filter": filter_info,
    }


# --- dashboard feature toggles (F0, issue #26) ---------------------------------
# The registry of toggleable dashboard tiles. Existing panels are listed here;
# new FamView-derived tiles (F1-F8) append their (key, label) as they land. The
# key must match the section's data-feature attribute in index.html.
DASHBOARD_FEATURES = [
    ("scene", "House & rooms"),
    ("cost", "Cost rail"),
    ("humidity", "Humidity"),
    ("crawl", "Crawl space"),
    ("ribbon", "Last 24 hours"),
    ("runtime", "Runtime"),
    ("health", "System health"),
    ("learning", "Learning"),
    ("calendar", "Calendar"),
]


def build_calendar(conn, cfg, now=None) -> dict:
    """Upcoming events (next 14 days) from the local CalDAV cache, grouped by
    local day for an agenda view. Each event carries its category calendar's
    color (Strategy B). `configured` is False until the bot account has synced,
    so the tile can prompt to connect iCloud."""
    tz = ZoneInfo(cfg.timezone)
    now = now or datetime.now(timezone.utc)
    events = db.upcoming_events(conn, now - timedelta(hours=6), now + timedelta(days=14), limit=60)
    days = {}
    for e in events:
        local = e["start_utc"].astimezone(tz)
        days.setdefault(local.date().isoformat(), []).append({
            "summary": e["summary"],
            "time": None if e["all_day"] else local.strftime("%-I:%M%p").lower(),
            "all_day": e["all_day"],
            "location": e["location"],
            "color": e["color"],
        })
    return {"configured": bool(db.caldav_collections(conn, kind="VEVENT")),
            "days": [{"date": k, "events": v} for k, v in sorted(days.items())]}
_FEATURE_KEYS = {k for k, _ in DASHBOARD_FEATURES}


def _feature_overrides(conn) -> dict:
    kv = db.kv_get(conn, "dashboard_settings")
    if kv and isinstance(kv["value"], dict):
        ov = kv["value"].get("features")
        if isinstance(ov, dict):
            return ov
    return {}


def get_dashboard_settings(conn) -> dict:
    """Every registered feature with its effective enabled state (default on).
    Server-side (kv-backed) so the wall display and phones agree."""
    ov = _feature_overrides(conn)
    return {"features": [
        {"key": k, "label": label, "enabled": bool(ov.get(k, True))}
        for k, label in DASHBOARD_FEATURES]}


def set_dashboard_settings(conn, features: dict) -> dict:
    """Merge a {key: bool} patch over the stored overrides. Unknown keys are
    ignored (so a stale client can't inject junk); values coerce to bool. The
    merge is atomic in SQL (see db.merge_kv_features) so racing toggles from the
    same UI don't clobber each other."""
    clean = {k: bool(v) for k, v in features.items() if k in _FEATURE_KEYS}
    db.merge_kv_features(conn, "dashboard_settings", clean)
    return get_dashboard_settings(conn)
