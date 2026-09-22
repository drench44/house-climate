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


# Returned as crawl_rows when a crawl probe IS configured but its readings
# could not be fetched. Distinct from None ("no probe"), so a broken lookup
# raises an alert instead of looking exactly like a house without a crawl probe.
CRAWL_FETCH_FAILED = object()


def _alert_context(conn, device_id, cfg, since, rows, now=None):
    """Gather the extra data evaluate() needs beyond the thermostat readings:
    recent crawl-probe rows (mold), the throttled filter-due flag, and the
    AirNow-preferred outdoor AQI. Each piece degrades to None/False on its own
    failure so a hiccup in one never blocks the core alerts."""
    global _filter_due_cache, _filter_due_at
    now = now or datetime.now(timezone.utc)
    crawl_rows = None
    sensor_id = None
    try:
        sensor_id, _ = api._crawl_sensor_id(cfg)
        if sensor_id is not None:
            # Fetch crawl history over a window COMFORTABLY LARGER than the mold
            # sustained requirement. Using the short-cycle `since` window (which
            # can be ~= crawl_mold_sustained_minutes) meant _sustained could
            # never find a run spanning mold_min -- the fetched span always fell
            # just short, so the mold alert could never fire (fable's bug).
            # Span the LONGEST crawl sustained-window (mold/saturated share
            # mold_min; condensation has its own) -- a window shorter than any
            # of them starves _sustained of the rows a run needs, the same bug
            # the mold fetch once had.
            # Also at least the offline threshold, so an empty fetch really
            # means "silent for longer than crawl_offline_minutes".
            span_min = max(cfg.alerts.get("crawl_mold_sustained_minutes", 180),
                           cfg.alerts.get("crawl_condensation_sustained_minutes", 180),
                           cfg.alerts.get("crawl_offline_minutes", _CRAWL_OFFLINE_MINUTES))
            crawl_since = now - timedelta(minutes=span_min * 2)
            crawl_since = min(crawl_since, since)   # never fetch LESS than `since`
            crawl_rows = db.sensor_readings_range(conn, sensor_id, crawl_since)
    except Exception:
        log.exception("crawl-context fetch failed")
        if sensor_id is not None:
            crawl_rows = CRAWL_FETCH_FAILED

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

    # OUTSIDE the try: a bad `rows` shape is not an AQI-resolution failure, and
    # letting it be reported as one sends the next debugger to the kv table
    # instead of to the caller that changed the row shape.
    wx_aqi = rows[-1].get("wx_aqi") if rows else None
    outdoor_aqi = None
    aqi_source = None
    try:
        outdoor_aqi, aqi_source = api.resolve_outdoor_aqi(conn, wx_aqi)
    except Exception:
        # Deliberately still broad: this runs in the alert loop, and an
        # unforeseen resolver error must not take the crawl/offline/filter
        # alerts down with it. But be honest about what the swallow costs --
        # `evaluate` then falls back to the reading's own wx_aqi, and when the
        # weather feed omits that (common) NO unhealthy-air push fires at all,
        # even during a real event. The monitor going dark is separately
        # alarmed upstream in Home Assistant, which is why this is a logged
        # WARNING here rather than a second push that would double-buzz the
        # same outage.
        log.exception("AQI resolution failed; falling back to the reading's own "
                      "wx_aqi (%s) -- an unhealthy-air push may be suppressed "
                      "entirely if that is null", wx_aqi)

    return crawl_rows, _filter_due_cache, outdoor_aqi, aqi_source


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str
    message: str
    # Extra dimension for the cooldown key, empty for almost every alert. It
    # exists because the same `key` can carry a MATERIALLY different message:
    # an air-quality push sourced from a real monitor and one sourced from the
    # weather feed's model say different things, and keying the cooldown on
    # `key` alone meant the corrected, caveated message was swallowed as a
    # duplicate of the unqualified one already on the phone. That left the
    # reader holding the unhedged claim -- the exact failure the caveat exists
    # to prevent. `key` stays the stable identity everything else asserts on.
    variant: str = ""
    # How bad, for alerts whose condition can WORSEN while it stands: the
    # freeze band (1 frost, 2 freeze, 3 hard) and the count of active NWS
    # alerts. A higher level than the one last pushed is a new event and
    # pushes through the cooldown; the same or a lower level (warming up, an
    # NWS alert expiring) is not. 0 for everything else.
    level: int = 0

    @property
    def dedupe_key(self):
        return (self.key, self.variant)


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
             crawl_rows=None, filter_due=None, outdoor_aqi=None,
             aqi_source=None, checked=None) -> list[Alert]:
    """Evaluate all alert conditions against the recent thermostat readings.

    checked -- optional set; filled with the keys whose check actually RAN on
               current data. An alert that is absent because its data was
               stale (thermostat outage, weather feed gap, crawl probe quiet)
               did not clear, so the re-arm logic must not treat it as
               cleared. See _checked_keys.

    Extra context (kept optional so the pure function stays easy to test, and
    absent context simply skips that alert rather than erroring):
      crawl_rows   -- recent crawl-space sensor rows
                      [{ts, humidity, temp_f, dewpoint_f}], for the sustained
                      crawl alerts (mold / saturated on humidity; condensation
                      on the temp-to-dewpoint spread). None -> no crawl probe
                      configured, every crawl alert skipped. [] or a latest
                      row older than crawl_offline_minutes -> the probe has
                      gone quiet: crawl_sensor_offline fires and the condition
                      alerts stand down (old readings are not a current
                      condition).
      filter_due   -- precomputed bool from the same runtime-hours logic the
                      dashboard shows. None -> filter alert skipped.
      outdoor_aqi  -- the effective outdoor AQI (AirNow-preferred, resolved by
                      the caller). None -> falls back to the reading's wx_aqi.
      aqi_source   -- provenance of that number, from resolve_outdoor_aqi:
                      "airnow" (a real monitor) or "weather" (the feed's
                      MODEL). Anything other than "airnow", None included, is
                      treated as modeled and says so in the message. Defaulting
                      an unknown provenance to "trustworthy" is precisely the
                      claim we cannot make.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    a = cfg.alerts
    out = []
    # "Offline" means NO FRESH READINGS — full stop. The old error-count
    # trigger fired this critical alert whenever ANY poll errors accumulated
    # (including Ecowitt-gateway failures, which are a different device),
    # even while thermostat data was perfectly fresh. Error counts are
    # context, not the test.
    #
    # A stale thermostat skips only the checks built on the thermostat rows.
    # It used to return early and skip EVERYTHING, so a Daikin cloud outage
    # also silenced the crawl alerts, which come from a separate Ecowitt probe
    # that was still reporting. The poller writes the weather fields into the
    # same row, so those (freeze, NWS, modeled AQI, feed health) are just as
    # old and are skipped too.
    thermo_fresh = False
    if not rows:
        out.append(Alert("offline", "critical", "Thermostat offline / no data at all"))
    else:
        stale_after = cfg.poll_interval_s * a["offline_missed_polls"]   # e.g. 180 * 5 = 900s
        if (now - rows[-1]["ts"]).total_seconds() > stale_after:
            extra = f" ({poll_errors_recent} poll errors in 20m)" if poll_errors_recent else ""
            out.append(Alert("offline", "critical", f"No fresh reading from thermostat{extra}"))
        else:
            thermo_fresh = True

    if thermo_fresh:
        out.extend(_thermostat_alerts(rows, cfg, now))

    out.extend(_crawl_alerts(crawl_rows, a, now))

    # Filter due: the runtime-hours threshold the dashboard already tracks,
    # surfaced as a push so it isn't only visible to someone who opens the
    # page. Computed from runtime history, so a thermostat outage doesn't make
    # it any less true.
    if filter_due:
        out.append(Alert("filter_due", "warning",
                         "HVAC filter is due for a change (runtime threshold reached)"))

    # Air quality: evaluated on the latest value only, not sustained -- smoke
    # is actionable the moment it shows up. Prefer the caller-resolved AirNow
    # AQI (fresher, and present even when the weather feed omits wx_aqi); fall
    # back to the reading's own wx_aqi, but only while that reading is fresh.
    # A monitor value comes from Home Assistant, not the thermostat row, so it
    # stays valid through a thermostat outage; a modeled value does not.
    #
    # Provenance follows the branch that produced the NUMBER, not the caller's
    # label. When the resolver gave us nothing and we fell back to the
    # reading's own wx_aqi, that value is the weather feed's model no matter
    # what `aqi_source` says -- so an `aqi_source="airnow"` passed alongside
    # `outdoor_aqi=None` cannot relabel a modeled number as a measurement.
    from_monitor = outdoor_aqi is not None and aqi_source == "airnow"
    if from_monitor:
        aqi = outdoor_aqi
    elif thermo_fresh:
        aqi = outdoor_aqi if outdoor_aqi is not None else rows[-1].get("wx_aqi")
    else:
        aqi = None
    if aqi is not None and aqi >= a.get("aqi_unhealthy", 101):
        # Say WHICH number this is. `resolve_outdoor_aqi` silently falls back
        # from the pushed monitor value to the weather feed's own model after
        # 30 quiet minutes, and the two disagree in the direction that matters:
        # on 2026-08-31 the model read 113 "Unhealthy" against a monitor's 85
        # "Moderate". A push that reads identically either way turns a monitor
        # outage into a confident false claim on someone's phone. When the
        # resolver could not run at all (aqi_source is None but the reading
        # carried its own wx_aqi), that is the modeled feed too.
        est = "" if from_monitor else ", estimated from the weather feed, not a monitor"
        out.append(Alert("air_quality", "warning",
                          f"Outdoor air unhealthy (AQI {int(round(aqi))}{est}):"
                          " keep windows closed, run purifiers",
                          variant="airnow" if from_monitor else "estimate"))
    if checked is not None:
        checked |= _checked_keys(rows, thermo_fresh, crawl_rows, filter_due, aqi,
                                 out, a, now)
    return out


_THERMO_KEYS = frozenset({"humidity_high", "setpoint_drift", "short_cycling",
                          "peak_surge", "equipment_unknown", "weather_feed_stale"})


def _checked_keys(rows, thermo_fresh, crawl_rows, filter_due, aqi, fired, a, now):
    """Which alert keys were really evaluated on current data this pass."""
    keys = {"offline"}
    if thermo_fresh:
        keys |= _THERMO_KEYS
        latest = rows[-1]
        if latest.get("wx_outdoor_temp_f") is not None or latest.get("daikin_outdoor_temp_f") is not None:
            keys.add("freeze")
        if latest.get("wx_alert_count") is not None:
            keys.add("weather_alert")
    if crawl_rows is not None:
        keys.add("crawl_sensor_offline")
        fresh = (crawl_rows is not CRAWL_FETCH_FAILED and crawl_rows
                 and (now - crawl_rows[-1]["ts"]).total_seconds()
                 <= a.get("crawl_offline_minutes", _CRAWL_OFFLINE_MINUTES) * 60)
        if fresh:
            keys |= {"crawl_saturated", "crawl_condensation"}
            # Saturation SUPPRESSES the mold alert rather than clearing it:
            # the crawl is wetter, not drier, so mold must not re-arm.
            if not any(al.key == "crawl_saturated" for al in fired):
                keys.add("crawl_mold")
    if filter_due is not None:
        keys.add("filter_due")
    if aqi is not None:
        keys.add("air_quality")
    return keys


def _thermostat_alerts(rows, cfg, now):
    """The checks that read the thermostat rows (and the weather fields the
    poller stores in those same rows). Only meaningful while they are fresh."""
    a = cfg.alerts
    out = []

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
        # The band is the level, so a COLDER band is a new event: a 34°F
        # frost at dawn must not hold back the push for a 15°F hard freeze
        # that evening under the same cooldown. Warming back up is not news.
        level = 3 if outdoor_t <= 20 else 2 if outdoor_t <= 28 else 1
        out.append(Alert("freeze", "critical" if level == 3 else "warning",
                         f"{'Hard freeze' if level == 3 else 'Freeze risk'}: outdoor"
                         f" {int(round(outdoor_t))}°F (at or below {freeze_at}°F)."
                         " Protect pipes and unheated zones.", level=level))

    n_wx = latest.get("wx_alert_count") or 0
    if n_wx > 0:
        # The count is the level: a new NWS alert issued while an earlier one
        # is active (a morning Wind Advisory, an afternoon Flash Flood
        # Warning) is a new event. One expiring is not.
        out.append(Alert("weather_alert", "warning",
                         f"{n_wx} active NWS weather alert{'s' if n_wx != 1 else ''} for your area",
                         level=int(n_wx)))

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
    # Weather-feed staleness (issue #5): when the feed is down, wx_aqi and
    # wx_alert_count go null, silently suppressing the AQI and NWS alerts above
    # at exactly the moment (smoke, storms) they matter. Surface the outage
    # itself — sustained so a brief blip doesn't page — so the suppression is
    # visible. Skipped when no weather feed is configured.
    def _wx_down(r):
        v = r.get("weather_ok")
        return None if v is None else (v is False)
    if cfg.weather_url and _sustained(rows, _wx_down, a.get("weather_stale_minutes", 30)):
        out.append(Alert("weather_feed_stale", "warning",
                         "Weather feed is down — outdoor AQI and NWS weather alerts are"
                         " unavailable until it recovers."))
    return out


_CRAWL_OFFLINE_MINUTES = 45   # default: the probe reports every few minutes


def _fmt_span(seconds):
    m = int(seconds // 60)
    return f"{m} min" if m < 120 else f"{m // 60}h"


def _crawl_alerts(crawl_rows, a, now):
    """Crawl-space alerts. `crawl_rows` is None when no crawl probe is
    configured (or its fetch failed, which _alert_context logs); an empty list
    means the probe IS configured but has sent nothing in the fetch window.

    Every crawl check needs RECENT data. `_sustained` only looks at the run
    ending at the latest row, so a probe that died at 80% RH used to keep the
    mold alert firing on its last readings for hours, then go quiet with no
    word that the probe was gone. A silent probe now raises its own alert and
    the condition checks stand down until it reports again."""
    if crawl_rows is None:
        return []
    if crawl_rows is CRAWL_FETCH_FAILED:
        return [Alert("crawl_sensor_offline", "warning",
                      "Crawl-space check could not run (its readings could not be"
                      " read from the database). Crawl mold and condensation alerts"
                      " are paused until it recovers.", variant="fetch_failed")]
    offline_min = a.get("crawl_offline_minutes", _CRAWL_OFFLINE_MINUTES)
    last_ts = crawl_rows[-1]["ts"] if crawl_rows else None
    if last_ts is None:
        return [Alert("crawl_sensor_offline", "warning",
                      "Crawl-space sensor has sent no readings in the last several"
                      " hours. Check its battery and the gateway. Crawl mold and"
                      " condensation alerts are paused until it reports.")]
    age_s = (now - last_ts).total_seconds()
    if age_s > offline_min * 60:
        return [Alert("crawl_sensor_offline", "warning",
                      f"Crawl-space sensor has not reported for {_fmt_span(age_s)}."
                      " Check its battery and the gateway. Crawl mold and"
                      " condensation alerts are paused until it reports.")]

    out = []
    # Crawl-space mold risk: SUSTAINED high RH on the crawl probe (a brief
    # spike isn't mold). This is the whole reason the Ecowitt probe exists;
    # without it the crawl had zero proactive coverage.
    mold_pct = a.get("crawl_mold_pct", 75)
    mold_min = a.get("crawl_mold_sustained_minutes", 180)
    sat_pct = a.get("crawl_saturated_pct", 90)

    def _sat(r):
        v = r.get("humidity")
        return None if v is None else v >= sat_pct

    def _mold(r):
        v = r.get("humidity")
        return None if v is None else v >= mold_pct
    # Two-tier RH: sustained >=90% is a distinct, worse regime than the 75%
    # mold watch (wood driven toward the decay-fungi moisture range). When
    # it holds, fire the escalated alert and SUPPRESS the mold alert -- one
    # damp crawl must not buzz the phone twice for the same condition. The
    # two keys have independent cooldowns, so without this suppression a
    # >=90% crawl sends BOTH every cooldown window.
    #
    # The escalation only makes sense when the saturated bar sits ABOVE the
    # mold bar. If a config transposes them (sat <= mold), escalating would
    # mislabel a merely-moldy crawl as "near saturation" AND suppress the
    # accurate mold alert -- so in that case disable the tier and let the
    # plain mold alert fire, rather than silently corrupting it.
    sat_active = sat_pct > mold_pct
    if sat_active and _sustained(crawl_rows, _sat, mold_min):
        out.append(Alert("crawl_saturated", "warning",
                         f"Crawl-space humidity sustained above {sat_pct}%: near saturation."
                         " Structural wood is wetting toward the decay range:"
                         " vapor barrier / dehumidifier, not just ventilation."))
    elif _sustained(crawl_rows, _mold, mold_min):
        out.append(Alert("crawl_mold", "warning",
                         f"Crawl-space humidity sustained above {mold_pct}%: mold risk."
                         " Check ventilation/dehumidifier."))

    # Condensation risk: a sustained air-to-dew-point spread below the
    # threshold means any surface at or below crawl air temp (joists, cold
    # AC ducts) is at the dew point -- liquid water on structural wood, the
    # worst crawl failure mode. Independent of the RH tiers above: a cold
    # winter crawl can condense at an RH reading below the 75% mold bar,
    # and this fires on the physics (spread) the RH number alone misses.
    # Needs both temp and dew point; a row missing either is NOT OBSERVED
    # (None), never treated as safe.
    cond_spread = a.get("crawl_condensation_spread_f", 3.0)
    cond_min = a.get("crawl_condensation_sustained_minutes", 180)

    def _condense(r):
        t, dp = r.get("temp_f"), r.get("dewpoint_f")
        if t is None or dp is None:
            return None
        return (t - dp) < cond_spread
    if _sustained(crawl_rows, _condense, cond_min):
        out.append(Alert("crawl_condensation", "warning",
                         f"Crawl-space condensation risk: air-to-dew-point spread under "
                         f"{cond_spread:g}°F sustained: water forming on joists/ducts."
                         " Check for standing water, seal vents, run a dehumidifier."))
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


class WebhookSink:
    """Generic push channel: POST {key, severity, title, message} as JSON to a
    URL (for example a Home Assistant webhook automation that fans the alert
    out to phones). The URL is a secret, so it comes from the ALERT_WEBHOOK_URL
    environment variable, never from config.json."""

    def __init__(self, url): self.url = url

    def send(self, alert: Alert):
        # Same contract as NtfySink: a non-2xx must raise so the alert stays
        # unsent and retries next cycle instead of being marked delivered. The
        # error is re-raised WITHOUT the requests message, which embeds the
        # URL: the caller logs it, and the URL is a secret.
        try:
            r = requests.post(self.url, json={"key": alert.key, "severity": alert.severity,
                                              "title": f"house-climate: {alert.key}",
                                              "message": alert.message},
                              timeout=10)
            r.raise_for_status()
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            raise RuntimeError(f"webhook push failed ({type(e).__name__}"
                               f"{f', HTTP {status}' if status else ''})") from None


class NoopSink:
    def send(self, alert): log.info("ALERT(noop) %s: %s", alert.key, alert.message)


# Not an alert: the receiver must stamp it and stay silent.
HEARTBEAT_KEY = "heartbeat"


def _send_heartbeat(sink):
    """Proof of life for the push path. A webhook receiver can answer 200
    while doing nothing (Home Assistant does, for an unknown webhook id), so
    the receiver stamps each heartbeat and alerts when they stop arriving.
    Only a WebhookSink has a receiver that can do that; returns False for
    the others."""
    if not isinstance(sink, WebhookSink):
        return False
    sink.send(Alert(HEARTBEAT_KEY, "info", "house-climate alert relay heartbeat"))
    return True


def make_sink(cfg, env=None):
    """Build the configured push sink. Raises ValueError when the channel
    cannot actually deliver (webhook with no usable URL): the web app builds
    the sink at startup, so the misconfiguration stops the process with a clear
    message instead of every alert quietly going nowhere."""
    env = os.environ if env is None else env
    ch = cfg.alerts.get("channel")
    if ch == "ntfy":
        return NtfySink(cfg.alerts["ntfy_topic"])
    if ch == "webhook":
        url = (env.get("ALERT_WEBHOOK_URL") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(
                "alerts.channel is 'webhook' but the ALERT_WEBHOOK_URL environment "
                "variable is missing or not an http(s) URL; set it in .env or pick "
                "another channel")
        return WebhookSink(url)
    return NoopSink()


def pushable(fired, cfg):
    """The alerts that should leave the box. Keys in alerts.push_suppress are
    still evaluated and still shown on the wall (/api/anomalies), just never
    pushed, because another system (for example Home Assistant) already sends
    them and a second buzz for the same event is noise."""
    suppress = set(cfg.alerts.get("push_suppress") or ())
    return [al for al in fired if al.key not in suppress]


_EPOCH_START = datetime.min.replace(tzinfo=timezone.utc)


def _rearm_cleared(fired, last_sent, cleared_since, grace, now, on_rearm=None,
                   last_level=None, checked=None):
    """Forget the send record of any alert that has stopped firing for at
    least `grace`, so its NEXT occurrence pushes at once. The cooldown exists
    to stop a standing condition (a filter overdue for weeks) re-buzzing; it
    must not swallow a new occurrence of something that had cleared. The
    grace keeps a condition flickering at its threshold from buzzing on every
    flicker. `fired` is every alert evaluated (pushed or suppressed).

    Liveness is by KEY, not variant: the air-quality source flipping from the
    monitor to the estimate and back is one standing condition. And only keys
    in `checked` (evaluated on current data) can start clearing; None means
    everything was checked."""
    live = {al.key for al in fired}
    for dk in list(last_sent):
        if dk[0] in live or (checked is not None and dk[0] not in checked):
            cleared_since.pop(dk, None)
            continue
        since = cleared_since.setdefault(dk, now)
        if now - since >= grace:
            del last_sent[dk]
            cleared_since.pop(dk, None)
            if last_level is not None:
                last_level.pop(dk, None)
            if on_rearm is not None:
                try:
                    on_rearm(dk)
                except Exception:
                    log.exception("alert %s/%s re-armed in this process, but its stored"
                                  " send time could not be cleared, so a restart would"
                                  " quiet it again until its cooldown runs out", *dk)


def _dispatch(sink, fired, last_sent, cooldown, now, on_sent=None, last_level=None):
    """Send each due alert (not sent within `cooldown`), mutating last_sent.
    Per-alert guard: a failed send (ntfy/webhook 4xx/5xx raises) is logged and
    the alert left UNSENT (last_sent is not updated, so it retries next cycle
    instead of being suppressed) and never blocks the remaining alerts.

    `on_sent(dedupe_key, ts)` persists a successful send (see record_sent). Its
    failure is logged and ignored: the in-memory cooldown still holds for this
    process, and the worst case is one repeat push after a restart."""
    levels = last_level if last_level is not None else {}
    for al in fired:
        worse = al.level > levels.get(al.dedupe_key, 0)
        if now - last_sent.get(al.dedupe_key, _EPOCH_START) < cooldown and not worse:
            continue
        try:
            sink.send(al)
        except Exception:
            log.exception("failed to send alert %s; will retry", al.key)
            continue
        last_sent[al.dedupe_key] = now
        levels[al.dedupe_key] = al.level
        if on_sent is not None:
            try:
                on_sent(al.dedupe_key, now)
            except Exception:
                log.exception("could not persist the send time for alert %s; a "
                              "restart inside its cooldown may push it again", al.key)


# Cooldown records live in kv, one row per dedupe key, so a restart or deploy
# does not re-push every active alert (the in-memory map used to start empty
# on every boot).
_SENT_PREFIX = "alert_sent:"


def _sent_kv_key(dedupe_key):
    key, variant = dedupe_key
    return f"{_SENT_PREFIX}{key}|{variant}"


def record_sent(conn, dedupe_key, ts, level=0):
    db.kv_set(conn, _sent_kv_key(dedupe_key), {"ts": ts.isoformat(), "level": level})


def forget_sent(conn, dedupe_key):
    db.kv_delete(conn, _sent_kv_key(dedupe_key))


_HEARTBEAT_KV = "alert_relay_heartbeat"


def _heartbeat_due(conn, every, now):
    """True when no heartbeat has been sent within `every`. An unreadable kv
    reads as due: a spare heartbeat costs nothing, a missing one alarms."""
    try:
        row = db.kv_get(conn, _HEARTBEAT_KV)
        last = datetime.fromisoformat(row["value"]["ts"]) if row else None
    except Exception:
        log.exception("could not read the last relay heartbeat; sending one")
        return True
    return last is None or now - last >= every


_LEVEL_UNKNOWN = 1_000_000


def load_last_levels(conn):
    """{(key, variant): level last pushed}, from the same kv rows as
    load_last_sent. Missing or unreadable reads as 0 (the safe direction: at
    worst one repeat push of a worsening condition)."""
    try:
        rows = db.kv_prefix(conn, _SENT_PREFIX)
    except Exception:
        log.exception("could not read alert levels from kv")
        return {}
    out = {}
    for k, v in rows:
        try:
            key, variant = k[len(_SENT_PREFIX):].split("|", 1)
            # A row written before levels existed has none. Reading it as 0
            # would make every standing freeze/NWS alert look "worse" and
            # re-push once on the upgrade; reading it as unbounded keeps it
            # quiet until it clears or its cooldown runs out, as before.
            lvl = v.get("level", _LEVEL_UNKNOWN)
        except (ValueError, AttributeError):
            continue
        if isinstance(lvl, int) and not isinstance(lvl, bool):
            out[(key, variant)] = lvl
    return out


def load_last_sent(conn):
    """The persisted cooldown map {(key, variant): datetime}. Fail-soft in the
    safe direction: an unreadable kv table returns {}, which can at worst cause
    a repeat push, never a missed one. Malformed rows are skipped."""
    try:
        rows = db.kv_prefix(conn, _SENT_PREFIX)
    except Exception:
        log.exception("could not read alert cooldowns from kv; alerting as if "
                      "none were sent recently")
        return {}
    out = {}
    for k, v in rows:
        try:
            key, variant = k[len(_SENT_PREFIX):].split("|", 1)
            ts = datetime.fromisoformat(v["ts"])
        except (ValueError, TypeError, KeyError, AttributeError):
            log.warning("ignoring malformed alert cooldown row %r", k)
            continue
        if ts.tzinfo is None:
            log.warning("ignoring alert cooldown row %r with a naive timestamp", k)
            continue
        out[(key, variant)] = ts
    return out


def _poll_errors_recent(conn):
    # Thermostat-poll errors only: Ecowitt-gateway failures are a different
    # device and must not feed the offline evaluation. Only decorates the
    # offline message, so a failed count must not take every alert down.
    try:
        return conn.execute(
            "SELECT count(*) FROM poll_errors WHERE ts > now() - interval '20 minutes'"
            " AND kind LIKE 'daikin%'"
        ).fetchone()[0]
    except Exception:
        log.exception("poll-error count failed; offline message will omit it")
        return 0


def evaluate_current(conn, device_id, cfg, now=None, checked=None):
    """Evaluate every alert against live data. The ONE entry point for both
    the push loop and the wall's alert strip (/api/anomalies), so the two can
    never disagree about what is wrong. The strip used to call evaluate() with
    no crawl rows, no filter flag and no resolved AQI, so it could never show
    crawl, filter or monitor-AQI alerts that were being pushed."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=cfg.alerts["short_cycles_window_hours"])
    rows = db.recent_readings(conn, device_id, since)
    errs = _poll_errors_recent(conn)
    crawl_rows, filter_due, outdoor_aqi, aqi_source = _alert_context(
        conn, device_id, cfg, since, rows, now=now)
    return evaluate(rows, cfg, errs, now, crawl_rows=crawl_rows,
                    filter_due=filter_due, outdoor_aqi=outdoor_aqi,
                    aqi_source=aqi_source, checked=checked)


def alert_loop(cfg, secrets):
    conn = db.connect(secrets.db_dsn)
    sink = make_sink(cfg)
    cooldown = timedelta(minutes=cfg.alerts["cooldown_minutes"])
    grace = timedelta(minutes=cfg.alerts.get("rearm_after_clear_minutes", 60))
    hb_hours = cfg.alerts.get("relay_heartbeat_hours", 24)
    heartbeat_every = timedelta(hours=hb_hours) if hb_hours else None
    hb_sent_at = None      # in-memory fallback when the kv stamp cannot be read or written
    last_sent = load_last_sent(conn)
    last_level = load_last_levels(conn)
    cleared_since = {}
    while True:
        try:
            # Self-heal a dead connection (DB restart) — retrying the same
            # broken connection forever means alerts die exactly when the
            # infrastructure is flaky, which is when they matter.
            if conn.closed:
                conn = db.connect(secrets.db_dsn)
            device_id = os.environ.get("DEVICE_ID") or db.latest_device_id(conn) or "unknown"
            checked = set()
            fired = evaluate_current(conn, device_id, cfg, checked=checked)
            live = conn
            now = datetime.now(timezone.utc)
            _rearm_cleared(fired, last_sent, cleared_since, grace, now,
                           on_rearm=lambda k: forget_sent(live, k), last_level=last_level,
                           checked=checked)
            _dispatch(sink, pushable(fired, cfg), last_sent, cooldown, now,
                      on_sent=lambda k, ts: record_sent(live, k, ts, last_level.get(k, 0)),
                      last_level=last_level)
            # After evaluation on purpose: a loop that cannot evaluate must go
            # quiet, so the receiver's staleness alarm fires.
            if (heartbeat_every is not None
                    and (hb_sent_at is None or now - hb_sent_at >= heartbeat_every)
                    and _heartbeat_due(conn, heartbeat_every, now)):
                try:
                    sent = _send_heartbeat(sink)
                except Exception:
                    log.exception("relay heartbeat POST failed; will retry next cycle")
                    sent = False
                if sent:
                    hb_sent_at = now
                    try:
                        db.kv_set(conn, _HEARTBEAT_KV, {"ts": now.isoformat()})
                    except Exception:
                        log.exception("relay heartbeat sent, but its time could not be"
                                      " saved; a restart may send one early")
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
