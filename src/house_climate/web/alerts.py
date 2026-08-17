import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests

from .. import db
from ..analytics import runtime
from . import api

log = logging.getLogger("house_climate.alerts")

# Filter-due scans a long runtime history, so recompute it at most hourly
# (running hours accrue over days) instead of every poll tick.
_FILTER_RECHECK_S = 3600
_filter_due_cache = None
_filter_due_at = 0.0


def _alert_context(conn, device_id, cfg, since, rows):
    """Gather the extra data evaluate() needs beyond the thermostat readings:
    recent crawl-probe rows (mold), the throttled filter-due flag, and the
    AirNow-preferred outdoor AQI. Each piece degrades to None/False on its own
    failure so a hiccup in one never blocks the core alerts."""
    global _filter_due_cache, _filter_due_at
    crawl_rows = None
    try:
        sensor_id, _ = api._crawl_sensor_id(cfg)
        if sensor_id is not None:
            # Fetch crawl history over a window COMFORTABLY LARGER than the mold
            # sustained requirement. Using the short-cycle `since` window (which
            # can be ~= crawl_mold_sustained_minutes) meant _sustained could
            # never find a run spanning mold_min -- the fetched span always fell
            # just short, so the mold alert could never fire (fable's bug).
            mold_min = cfg.alerts.get("crawl_mold_sustained_minutes", 180)
            crawl_since = datetime.now(timezone.utc) - timedelta(minutes=mold_min * 2)
            crawl_since = min(crawl_since, since)   # never fetch LESS than `since`
            crawl_rows = db.sensor_readings_range(conn, sensor_id, crawl_since)
    except Exception:
        log.exception("crawl-context fetch failed")

    now_mono = time.monotonic()
    if _filter_due_cache is None or now_mono - _filter_due_at >= _FILTER_RECHECK_S:
        try:
            _filter_due_cache = bool(api.filter_status(conn, device_id, cfg)["due"])
            _filter_due_at = now_mono        # only extend the TTL on a real result
        except Exception:
            # Don't advance _filter_due_at on failure: a transient DB hiccup
            # must not suppress the filter-due check for a whole hour. Keep the
            # last good value (or default off if we never had one) and retry
            # next tick.
            log.exception("filter-status check failed")
            if _filter_due_cache is None:
                _filter_due_cache = False

    outdoor_aqi = None
    try:
        wx_aqi = rows[-1].get("wx_aqi") if rows else None
        outdoor_aqi, _ = api.resolve_outdoor_aqi(conn, wx_aqi)
    except Exception:
        log.exception("AQI resolution failed")

    return crawl_rows, _filter_due_cache, outdoor_aqi


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str
    message: str


_SUSTAINED_MAX_GAP_S = 900   # a bigger hole means the condition wasn't OBSERVED


def _sustained(rows, predicate, minutes):
    """True if `predicate` holds on a CONTIGUOUS OBSERVED run of rows ending
    at the latest row, and that run spans >= `minutes`. Contiguity requires
    both the predicate AND sampling: a poller outage inside the run breaks
    it — two samples three hours apart are two moments, not three sustained
    hours.

    `predicate(row)` is TRI-STATE: True (condition holds), False (condition
    broken), or None (NOT OBSERVED — the field this predicate needs is null on
    this row). A None is skipped, not treated as a break: a cloud API that
    drops one null reading every ~15 min must not reset a genuinely sustained
    run. The sampling-gap guard still fires if the nulls span a real hole. If
    the latest row doesn't actively satisfy the predicate, returns False."""
    if not rows or predicate(rows[-1]) is not True:
        return False
    run = []
    for r in reversed(rows):          # walk back from the latest row
        if run and (run[-1]["ts"] - r["ts"]).total_seconds() > _SUSTAINED_MAX_GAP_S:
            break                     # unobserved gap -> the run starts after it
        p = predicate(r)
        if p is True:
            run.append(r)
        elif p is None:
            continue                  # missing observation -> skip, don't break
        else:
            break                     # condition broken -> stop
    if len(run) < 2:
        return False
    span = (run[0]["ts"] - run[-1]["ts"]).total_seconds() / 60.0  # latest - earliest in the trailing run
    return span >= minutes


def _recovering(rows, minutes):
    """After a scheduled setpoint drop the AC is legitimately mid-pulldown, so
    being above the (newly lowered) setpoint is normal recovery, not a fault.
    Treat the system as recovering if, over the last `minutes`, the cool
    setpoint was lowered (a schedule change) OR indoor temp is trending down
    (making progress toward setpoint)."""
    if len(rows) < 2:
        return False
    cutoff = rows[-1]["ts"] - timedelta(minutes=minutes)
    win = [r for r in rows if r["ts"] >= cutoff]
    if len(win) < 2:
        return False
    sp0, sp1 = win[0].get("cool_setpoint_f"), win[-1].get("cool_setpoint_f")
    if sp0 is not None and sp1 is not None and sp1 < sp0 - 0.01:
        return True                      # setpoint was lowered during the window
    t0, t1 = win[0].get("indoor_temp_f"), win[-1].get("indoor_temp_f")
    if t0 is not None and t1 is not None and t1 <= t0 - 0.5:
        return True                      # indoor temp falling toward setpoint
    return False


def evaluate(rows, cfg, poll_errors_recent, now=None, *,
             crawl_rows=None, filter_due=None, outdoor_aqi=None) -> list[Alert]:
    """Evaluate all alert conditions against the recent thermostat readings.

    Extra context (kept optional so the pure function stays easy to test, and
    absent context simply skips that alert rather than erroring):
      crawl_rows   -- recent crawl-space sensor rows [{ts, humidity}], for the
                      sustained mold-risk alert. None -> mold alert skipped.
      filter_due   -- precomputed bool from the same runtime-hours logic the
                      dashboard shows. None -> filter alert skipped.
      outdoor_aqi  -- the effective outdoor AQI (AirNow-preferred, resolved by
                      the caller). None -> falls back to the reading's wx_aqi.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    a = cfg.alerts
    out = []
    # "Offline" means NO FRESH READINGS — full stop. The old error-count
    # trigger fired this critical alert whenever ANY poll errors accumulated
    # (including Ecowitt-gateway failures, which are a different device),
    # even while thermostat data was perfectly fresh — and its early return
    # then masked every real alert. Error counts are context, not the test.
    if not rows:
        return [Alert("offline", "critical", "Thermostat offline / no data at all")]
    stale_after = cfg.poll_interval_s * a["offline_missed_polls"]   # e.g. 180 * 5 = 900s
    if (now - rows[-1]["ts"]).total_seconds() > stale_after:
        extra = f" ({poll_errors_recent} poll errors in 20m)" if poll_errors_recent else ""
        return [Alert("offline", "critical", f"No fresh reading from thermostat{extra}")]
    def _humid(r):
        v = r.get("indoor_humidity")
        return None if v is None else v >= a["humidity_high_pct"]
    if _sustained(rows, _humid, a["humidity_sustained_minutes"]):
        out.append(Alert("humidity_high", "warning",
                          f"Indoor humidity above {a['humidity_high_pct']}%"))

    def _drift(r):
        sp, indoor = r.get("cool_setpoint_f"), r.get("indoor_temp_f")
        if sp is None or indoor is None:
            return None      # not observed -> don't reset a sustained run
        return (indoor - sp) >= a["setpoint_drift_f"]
    if _sustained(rows, _drift, a["setpoint_drift_minutes"]) and not _recovering(rows, a["setpoint_drift_minutes"]):
        out.append(Alert("setpoint_drift", "warning", "Indoor temp not reaching cool setpoint"))
    res = runtime.compute(rows, short_cycle_min=cfg.short_cycle_minutes)
    if res.short_cycles >= a["short_cycles_threshold"]:
        out.append(Alert("short_cycling", "warning",
                          f"{res.short_cycles} short cycles detected"))

    # Freeze / frost: single-reading trigger (like AQI) -- a hard freeze is
    # actionable immediately, not after it persists. Prefer the station's
    # outdoor temp, fall back to the thermostat's outdoor sensor.
    latest = rows[-1]
    outdoor_t = latest.get("wx_outdoor_temp_f")
    if outdoor_t is None:
        outdoor_t = latest.get("daikin_outdoor_temp_f")
    freeze_at = a.get("freeze_temp_f", 34)
    if outdoor_t is not None and outdoor_t <= freeze_at:
        out.append(Alert("freeze", "warning",
                         f"Freeze risk: outdoor {int(round(outdoor_t))}°F (at or below {freeze_at}°F)."
                         " Protect pipes and unheated zones."))

    # Crawl-space mold risk: SUSTAINED high RH on the crawl probe (a brief
    # spike isn't mold). This is the whole reason the Ecowitt probe exists;
    # without it the crawl had zero proactive coverage. Skipped when no crawl
    # rows are supplied (probe not configured / no data).
    if crawl_rows:
        mold_pct = a.get("crawl_mold_pct", 75)
        mold_min = a.get("crawl_mold_sustained_minutes", 180)

        def _mold(r):
            v = r.get("humidity")
            return None if v is None else v >= mold_pct
        if _sustained(crawl_rows, _mold, mold_min):
            out.append(Alert("crawl_mold", "warning",
                             f"Crawl-space humidity sustained above {mold_pct}%: mold risk."
                             " Check ventilation/dehumidifier."))

    # Filter due: the runtime-hours threshold the dashboard already tracks,
    # surfaced as a push so it isn't only visible to someone who opens the page.
    if filter_due:
        out.append(Alert("filter_due", "warning",
                         "HVAC filter is due for a change (runtime threshold reached)"))

    # Air-quality + weather alerts: evaluated on the LATEST reading only, not
    # sustained like humidity/drift -- smoke and active NWS alerts are
    # actionable the moment they show up, not after they persist. Prefer the
    # caller-resolved AirNow AQI (fresher, and present even when the weather
    # feed omits wx_aqi); fall back to the reading's own wx_aqi.
    aqi = outdoor_aqi if outdoor_aqi is not None else latest.get("wx_aqi")
    if aqi is not None and aqi >= a.get("aqi_unhealthy", 101):
        out.append(Alert("air_quality", "warning",
                          f"Outdoor air unhealthy (AQI {int(round(aqi))}): keep windows closed, run purifiers"))
    if (latest.get("wx_alert_count") or 0) > 0:
        out.append(Alert("weather_alert", "warning", "Active NWS weather alert for your area"))

    # Peak-hour surge (README's promised "peak-hour surge" alert; config key
    # peak_surge_ratio was previously read by nothing). Single-reading, like
    # AQI/freeze: actionable the moment it's true. Fires only when the AC is
    # ACTIVELY running inside an on-peak window whose rate is >= peak_surge_ratio
    # times the off-peak rate — so it stays quiet on cheap/flat tariffs and only
    # nags when running now is genuinely expensive.
    try:
        now_local = now.astimezone(ZoneInfo(cfg.timezone))
        if latest.get("equipment_status") in {"cooling", "overcool", "heating"} \
                and cfg.tou.is_peak(now_local):
            # Compare against THIS season's off-peak rate, not the global min
            # across all seasons (a cheaper winter rate would skew the multiple).
            season = cfg.tou.season(now_local.month)
            band_rates = sorted({b.rate for b in cfg.tou.bands if b.season == season})
            ratio = a.get("peak_surge_ratio", 1.5)
            if len(band_rates) >= 2 and band_rates[0] > 0:
                cur_rate = cfg.tou.band_for(now_local)[1]
                if cur_rate >= ratio * band_rates[0]:
                    out.append(Alert("peak_surge", "warning",
                        f"AC running during peak — power is ${cur_rate:.2f}/kWh"
                        f" ({cur_rate / band_rates[0]:.1f}x off-peak). Shift big loads if you can."))
    except Exception:
        log.exception("peak-surge check failed")
    # Equipment-status drift (issue #4): an unrecognized Daikin equipmentStatus
    # maps to "unknown", which runtime/cost silently treat as idle -> hours and
    # dollars read LOW with no signal. Warn when unknown dominates the recent
    # window so the deflation is visible instead of silent.
    unknown_n = sum(1 for r in rows if r.get("equipment_status") == "unknown")
    if unknown_n and unknown_n / len(rows) >= a.get("equipment_unknown_frac", 0.2):
        out.append(Alert("equipment_unknown", "warning",
                         f"{unknown_n} of {len(rows)} recent readings have an unrecognized"
                         " equipment status — runtime and cost may read low. Check for a"
                         " Daikin firmware/API change."))
    return out


class NtfySink:
    def __init__(self, topic): self.url = f"https://ntfy.sh/{topic}"

    def send(self, alert: Alert):
        r = requests.post(self.url, data=alert.message.encode(),
                          headers={"Title": f"house-climate: {alert.key}",
                                   "Priority": "high" if alert.severity == "critical" else "default"},
                          timeout=10)
        # requests.post only raises on connection/timeout errors, NOT on a
        # non-2xx response. Without this, a 404 (mistyped topic), 429
        # (rate-limited), or 5xx (ntfy outage) returns normally and the caller
        # records the alert as delivered and suppresses it for the cooldown —
        # the user's phone never buzzes. Raise so the caller leaves it unsent
        # and retries.
        r.raise_for_status()


class NoopSink:
    def send(self, alert): log.info("ALERT(noop) %s: %s", alert.key, alert.message)


def make_sink(cfg):
    ch = cfg.alerts.get("channel")
    if ch == "ntfy":
        return NtfySink(cfg.alerts["ntfy_topic"])
    return NoopSink()


_EPOCH_START = datetime.min.replace(tzinfo=timezone.utc)


def _dispatch(sink, fired, last_sent, cooldown, now):
    """Send each due alert (not sent within `cooldown`), mutating last_sent.
    Per-alert guard: a failed send (ntfy 4xx/5xx now raises) is logged and the
    alert left UNSENT — last_sent is not updated, so it retries next cycle
    instead of being suppressed — and never blocks the remaining alerts."""
    for al in fired:
        if now - last_sent.get(al.key, _EPOCH_START) < cooldown:
            continue
        try:
            sink.send(al)
            last_sent[al.key] = now
        except Exception:
            log.exception("failed to send alert %s; will retry", al.key)


def alert_loop(cfg, secrets):
    conn = db.connect(secrets.db_dsn)
    sink = make_sink(cfg)
    cooldown = timedelta(minutes=cfg.alerts["cooldown_minutes"])
    last_sent = {}
    while True:
        try:
            # Self-heal a dead connection (DB restart) — retrying the same
            # broken connection forever means alerts die exactly when the
            # infrastructure is flaky, which is when they matter.
            if conn.closed:
                conn = db.connect(secrets.db_dsn)
            device_id = os.environ.get("DEVICE_ID") or db.latest_device_id(conn) or "unknown"
            since = datetime.now(timezone.utc) - timedelta(hours=cfg.alerts["short_cycles_window_hours"])
            rows = db.recent_readings(conn, device_id, since)
            # Thermostat-poll errors only: Ecowitt-gateway failures are a
            # different device and must not feed the offline evaluation.
            errs = conn.execute(
                "SELECT count(*) FROM poll_errors WHERE ts > now() - interval '20 minutes'"
                " AND kind LIKE 'daikin%'"
            ).fetchone()[0]
            crawl_rows, filter_due, outdoor_aqi = _alert_context(conn, device_id, cfg, since, rows)
            fired = evaluate(rows, cfg, errs, crawl_rows=crawl_rows,
                             filter_due=filter_due, outdoor_aqi=outdoor_aqi)
            _dispatch(sink, fired, last_sent, cooldown, datetime.now(timezone.utc))
        except Exception:
            log.exception("alert loop error")
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn = db.connect(secrets.db_dsn)
            except Exception:
                log.exception("alert loop reconnect failed; will retry")
        time.sleep(cfg.poll_interval_s)
