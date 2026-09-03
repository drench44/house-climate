"""Moisture-case analytics for the crawl space.

Every function here feeds the evidence page (/moisture.html): source
attribution, rainfall lag correlation, condensation risk, threshold-hour
counters, intervention before/after comparison, and the winter projection.

House rule, same as the rest of the stack: no conclusion is shown until the
data supports it. Every result carries ready=False + a reason until its gate
passes, and the gates are explicit constants below — the honesty is
inspectable.
"""
import math
from datetime import timedelta

# lag1_autocorr lives in coupling.py, where the rest of the autocorrelation
# machinery is. Imported rather than duplicated so the two never drift.
from .coupling import lag1_autocorr

# ---- gates -----------------------------------------------------------------
# Source attribution: hourly-mean pairs needed per window, and the minimum
# spread outdoor dew point must show. If outdoor dew point barely moved,
# correlation against it is meaningless no matter how many hours exist.
ATTR_MIN_HOURS_7D = 96      # >= 4 of 7 days covered
ATTR_MIN_HOURS_30D = 360    # >= 15 of 30 days covered
ATTR_MIN_OUTDOOR_STD_F = 2.5

# Attribution verdict bands on |r| (crawl dp vs outdoor dp).
ATTR_R_COUPLED = 0.6        # crawl follows outdoor air -> ventilation-dominant
ATTR_R_DECOUPLED = 0.25     # crawl ignores outdoor air -> soil/ground-dominant

# Rainfall lag correlation.
RAIN_MAX_LAG_DAYS = 5
RAIN_MIN_DAYS = 10          # overlapping (rain, crawl) day pairs
RAIN_MIN_WET_DAYS = 3       # days with real rain; correlation needs contrast
RAIN_WET_THRESHOLD_IN = 0.05
RAIN_R_STRONG = 0.5
RAIN_R_WEAK = 0.3

# Condensation risk: spread (air temp - dew point) below this is condensing
# territory on any surface at or below air temperature (joists, ducts).
CONDENSATION_SPREAD_F = 3.0
# Assumed supply-duct surface temperature while the AC runs. Not measured —
# labeled as an assumption everywhere it is shown.
DUCT_SURFACE_ASSUMED_F = 57.0

# Intervention comparison: daily-mean samples needed on each side before a
# change is called real. The significance bar itself is the Welch-t 95% CI
# (see _metric_compare) — one bar for both the interval and the verdict.
BASELINE_MIN_DAYS = 10
BASELINE_MAX_DAYS = 60      # baseline AND post windows are capped at this

# Winter projection: days of daily-mean history and the outdoor-temp span the
# fit must have seen. Extrapolating a summer-only fit into winter is exactly
# the weak-fit dishonesty the house style forbids.
PROJ_MIN_DAYS = 45
PROJ_MIN_TEMP_SPAN_F = 30.0
# A representative cold-winter design point for the projection readout
# (Dec-Feb normals for a temperate marine climate: mean temp ~41F, mean
# dew point ~36F).
WINTER_TEMP_F = 41.0
WINTER_DP_F = 36.0


def pearson(pairs):
    """Pearson r over (x, y) pairs. None when undefined (n<3 or zero
    variance on either axis)."""
    pts = [(x, y) for x, y in pairs if x is not None and y is not None]
    n = len(pts)
    if n < 3:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    syy = sum((p[1] - my) ** 2 for p in pts)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    return sxy / math.sqrt(sxx * syy)


def _mean_std(vals):
    n = len(vals)
    if n == 0:
        return None, None
    m = sum(vals) / n
    if n < 2:
        return m, None
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    return m, math.sqrt(var)


# ---------------------------------------------------------------------------
# Source attribution: does crawl dew point track outdoor dew point?
# ---------------------------------------------------------------------------

def attribution_window(crawl_hourly, outdoor_hourly, now, days, min_hours):
    """Correlate hourly crawl vs outdoor dew point over the trailing window.
    Returns {r, n, ready, reason}."""
    since = now - timedelta(days=days)
    outdoor_by_hour = {r["bucket"]: r["dp"] for r in outdoor_hourly
                       if r["bucket"] >= since and r["dp"] is not None}
    pairs = [(outdoor_by_hour[c["bucket"]], c["dp"]) for c in crawl_hourly
             if c["bucket"] >= since and c["dp"] is not None
             and c["bucket"] in outdoor_by_hour]
    if len(pairs) < min_hours:
        return {"ready": False, "reason": "collecting", "n": len(pairs),
                "need": min_hours, "r": None}
    _, out_std = _mean_std([p[0] for p in pairs])
    if out_std is None or out_std < ATTR_MIN_OUTDOOR_STD_F:
        return {"ready": False, "reason": "outdoor_flat", "n": len(pairs),
                "need": min_hours, "r": None}
    r = pearson(pairs)
    if r is None:
        return {"ready": False, "reason": "no_variance", "n": len(pairs),
                "need": min_hours, "r": None}
    return {"ready": True, "r": round(r, 2), "n": len(pairs)}


def attribution_verdict(w7, w30):
    """Plain-language dominant-source readout from whichever windows are
    ready. The 30d window wins when both are; None until either is.
    Classification uses |r|: a strongly NEGATIVE correlation is still strong
    coupling (the crawl's thermal mass can phase-shift the diurnal outdoor
    cycle), not evidence of decoupling."""
    w = w30 if w30.get("ready") else (w7 if w7.get("ready") else None)
    if w is None:
        return None
    r = w["r"]
    if abs(r) >= ATTR_R_COUPLED:
        how = "tracks" if r > 0 else "tracks (inversely, phase-shifted)"
        return ("ventilation",
                f"Crawl dew point {how} outdoor dew point closely (r={r:+.2f}) — "
                "outside air moving through the crawl is the dominant moisture "
                "source. Sealing/encapsulation addresses this; drainage alone would not.")
    if abs(r) <= ATTR_R_DECOUPLED:
        return ("soil",
                f"Crawl dew point barely responds to outdoor dew point (r={r:+.2f}) — "
                "moisture is coming from below (soil vapor / ground water), not from "
                "ventilation air. A ground vapor barrier addresses this; more venting would not.")
    return ("mixed",
            f"Crawl dew point partially tracks outdoor dew point (r={r:+.2f}) — "
            "both ventilation air and ground moisture contribute. Expect both a "
            "vapor barrier and air-sealing to matter.")


# ---------------------------------------------------------------------------
# Rainfall lag correlation: does crawl moisture respond to rain?
# ---------------------------------------------------------------------------

# Two-sided t critical values at alpha = 0.05/6 (Bonferroni across the six
# lags tested), by degrees of freedom. Interpolate downward (conservative).
_T_CRIT_BONF6 = [(8, 3.48), (10, 3.17), (15, 2.94), (20, 2.85),
                 (30, 2.75), (60, 2.66), (10 ** 9, 2.64)]


def _r_crit_bonf6(n):
    """Minimum |r| that is significant at family-wise 5% when the best of six
    lags is selected. Without this, max-of-six on pure noise clears a fixed
    0.5 bar ~18% of the time at n=10-15 — a coin-flip-ish 'buy a sump pump'.
    dof is rounded DOWN to the nearest table anchor (a LARGER t, so a larger
    |r| bar -> conservative); rounding up to the next anchor used a smaller t
    and let noise clear the bar slightly too easily."""
    dof = n - 2
    if dof < 1:
        return 1.0
    t = _T_CRIT_BONF6[0][1]
    for max_dof, tv in _T_CRIT_BONF6:
        if max_dof <= dof:
            t = tv
        else:
            break
    return t / math.sqrt(dof + t * t)


def rain_lag_correlation(precip_by_day, crawl_daily):
    """For each lag 0..RAIN_MAX_LAG_DAYS, correlate rain on day D-lag with
    crawl dew-point mean on day D. precip_by_day: {date: inches}.
    crawl_daily: [{day, dp_mean, rh_mean}].

    Honesty rules (each closes a real hole):
    - wet days are counted only in the window the lags can actually use
      ([first crawl day - max lag, last crawl day]) — backfilled rain from
      before the sensor existed proves nothing about the sensor's response.
    - a lag is usable only if ITS OWN pair count clears RAIN_MIN_DAYS and it
      paired at least RAIN_MIN_WET_DAYS wet days (contrast at that lag).
    - 'rain_driven' additionally requires the best r to clear a
      selection-corrected significance bar (Bonferroni over the six lags).
    """
    days = [d for d in crawl_daily if d.get("dp_mean") is not None]
    window_lo = min((d["day"] for d in days), default=None)
    window_hi = max((d["day"] for d in days), default=None)
    lags = []
    for lag in range(RAIN_MAX_LAG_DAYS + 1):
        pairs = []
        for d in days:
            rain = precip_by_day.get(d["day"] - timedelta(days=lag))
            if rain is not None:
                pairs.append((rain, d["dp_mean"]))
        r = pearson(pairs)
        n_wet = sum(1 for rain, _ in pairs if rain >= RAIN_WET_THRESHOLD_IN)
        lags.append({"lag": lag, "r": round(r, 2) if r is not None else None,
                     "n": len(pairs), "n_wet": n_wet})

    if window_lo is not None:
        wet_days = sum(1 for day, v in precip_by_day.items()
                       if v is not None and v >= RAIN_WET_THRESHOLD_IN
                       and window_lo - timedelta(days=RAIN_MAX_LAG_DAYS) <= day <= window_hi)
    else:
        wet_days = 0
    max_n = max((l["n"] for l in lags), default=0)
    if max_n < RAIN_MIN_DAYS:
        return {"ready": False, "reason": "collecting", "lags": lags,
                "wet_days": wet_days, "need_days": RAIN_MIN_DAYS}
    if wet_days < RAIN_MIN_WET_DAYS:
        return {"ready": False, "reason": "no_rain_yet", "lags": lags,
                "wet_days": wet_days, "need_wet": RAIN_MIN_WET_DAYS}
    usable = [l for l in lags if l["r"] is not None
              and l["n"] >= RAIN_MIN_DAYS and l["n_wet"] >= RAIN_MIN_WET_DAYS]
    if not usable:
        return {"ready": False, "reason": "no_variance", "lags": lags,
                "wet_days": wet_days}

    best = max(usable, key=lambda l: l["r"])
    r_sig = _r_crit_bonf6(best["n"])
    out = {"ready": True, "lags": lags, "wet_days": wet_days,
           "best": {"lag": best["lag"], "r": best["r"], "n": best["n"],
                    "r_significant": round(r_sig, 2)}}
    if best["r"] >= max(RAIN_R_STRONG, r_sig):
        lag_txt = "the same day" if best["lag"] == 0 else (
            f"{best['lag']} day{'s' if best['lag'] != 1 else ''} after rain")
        out["verdict"] = (
            "rain_driven",
            f"Crawl moisture rises {lag_txt} (r={best['r']:+.2f}, n={best['n']} days, "
            f"clears the {r_sig:.2f} significance bar) — the signature of liquid "
            "water intrusion. This supports drainage work (grading, gutters, "
            "possibly a sump).")
    elif best["r"] >= RAIN_R_WEAK:
        out["verdict"] = (
            "weak",
            f"Crawl moisture shows a possible rain response (best r={best['r']:+.2f} at "
            f"lag {best['lag']}d over {best['n']} days — below the {max(RAIN_R_STRONG, r_sig):.2f} "
            "bar that would make it conclusive). Keep collecting through wetter "
            "weather before funding drainage.")
    else:
        out["verdict"] = (
            "no_response",
            f"Crawl moisture does not respond to rainfall (best r="
            f"{best['r']:+.2f} across lags 0-{RAIN_MAX_LAG_DAYS}d, {wet_days} wet days "
            "observed) — no liquid-intrusion signature. This is evidence AGAINST "
            "drainage work (sump pump, regrading) being necessary.")
    return out


# ---------------------------------------------------------------------------
# Condensation risk
# ---------------------------------------------------------------------------

def condensation_summary(daily_stats, outdoor_days, crawl_daily_dp):
    """Combine per-day condensation-risk hours (air-to-dew spread < 3F,
    computed in SQL) with the duct-sweat proxy: cooling hours on days whose
    crawl dew point mean exceeds the assumed 57F duct surface. Returns
    {days: [...], hours_7d, duct_hours_7d}."""
    cooling_by_day = {d["day"]: (d.get("cooling_h") or 0) for d in outdoor_days}
    dp_by_day = {d["day"]: d.get("dp_mean") for d in crawl_daily_dp}
    days = []
    for d in daily_stats:
        dp = dp_by_day.get(d["day"])
        duct_h = 0.0
        if dp is not None and dp > DUCT_SURFACE_ASSUMED_F:
            duct_h = round(cooling_by_day.get(d["day"], 0.0), 1)
        days.append({"_date": d["day"], "day": d["day"].isoformat(),
                     "hours": round(d.get("cond_h") or 0, 1),
                     "duct_hours": duct_h,
                     "obs_hours": round(d.get("obs_h") or 0, 1)})
    # "Last 7 days" means 7 CALENDAR days, not 7 rows — after an outage the
    # last 7 rows can span 10+ days and overstate recent risk.
    cutoff = (max(x["_date"] for x in days) - timedelta(days=6)) if days else None
    last7 = [x for x in days if cutoff is not None and x["_date"] >= cutoff]
    for x in days:
        del x["_date"]
    return {
        "days": days,
        "hours_7d": round(sum(x["hours"] for x in last7), 1),
        "duct_hours_7d": round(sum(x["duct_hours"] for x in last7), 1),
        "spread_f": CONDENSATION_SPREAD_F,
        "assumed_duct_f": DUCT_SURFACE_ASSUMED_F,
    }


# ---------------------------------------------------------------------------
# Threshold-hour counters (weekly / monthly rollups of the daily SQL stats)
# ---------------------------------------------------------------------------

def threshold_rollups(daily_stats):
    """Weekly (ISO) and monthly cumulative hours above 60/70/80 %RH, plus
    observed hours so partial coverage is visible instead of silent."""
    weeks, months = {}, {}
    for d in daily_stats:
        iso = d["day"].isocalendar()
        wk = f"{iso[0]}-W{iso[1]:02d}"
        mo = d["day"].strftime("%Y-%m")
        for key, bucket in ((wk, weeks), (mo, months)):
            b = bucket.setdefault(key, {"h60": 0.0, "h70": 0.0, "h80": 0.0, "obs_h": 0.0})
            b["h60"] += d.get("h60") or 0
            b["h70"] += d.get("h70") or 0
            b["h80"] += d.get("h80") or 0
            b["obs_h"] += d.get("obs_h") or 0
    fmt = lambda bucket: [
        {"period": k, **{m: round(v[m], 1) for m in ("h60", "h70", "h80", "obs_h")}}
        for k, v in sorted(bucket.items())]
    return {"weeks": fmt(weeks), "months": fmt(months)}


# ---------------------------------------------------------------------------
# Intervention baselines
# ---------------------------------------------------------------------------

# Two-sided 95% t critical values by dof (Welch), rounded down conservatively.
_T_CRIT_95 = [(9, 2.26), (12, 2.18), (15, 2.13), (20, 2.09),
              (30, 2.04), (60, 2.00), (10 ** 9, 1.96)]


def _t_crit_95(dof):
    """Round dof DOWN to the nearest table anchor: a larger crit -> a wider CI
    -> conservative. Rounding up to the next anchor narrowed the Welch CI and
    flipped 'noise' to 'real' slightly too easily."""
    tv = _T_CRIT_95[0][1]
    for max_dof, v in _T_CRIT_95:
        if max_dof <= dof:
            tv = v
        else:
            break
    return tv


def _effective_days(vals, days=None):
    """How many independent days a run of correlated daily means is worth.

    `days` lets the neighbour test run on the CALENDAR: after a sensor outage,
    the previous row is not the previous day, and pairing across the gap makes
    the days look less alike than they are — which inflates this count and
    narrows every interval built from it."""
    n = len(vals)
    if n < 3:
        return n
    rho = lag1_autocorr(vals, days, step=timedelta(days=1))
    if rho <= 0:
        return n
    return max(2, min(n, int(round(n * (1.0 - rho) / (1.0 + rho)))))


def _metric_compare(base_vals, post_vals, digits=1, min_days=None,
                    base_days=None, post_days=None):
    """Difference of daily means with a 95% Welch-t CI. ONE bar for both the
    interval and the call: verdict is 'real' exactly when |diff| exceeds the
    displayed ci95 — showing an interval that excludes zero labeled 'noise'
    (the old 1.96-CI vs 2.0-z mismatch) presented contradictory evidence."""
    bm, bs = _mean_std(base_vals)
    pm, ps = _mean_std(post_vals)
    n1, n2 = len(base_vals), len(post_vals)
    out = {"baseline_mean": round(bm, digits) if bm is not None else None,
           "baseline_sd": round(bs, digits) if bs is not None else None,
           "baseline_n": n1,
           "post_mean": round(pm, digits) if pm is not None else None,
           "post_sd": round(ps, digits) if ps is not None else None,
           "post_n": n2}
    if bm is None or pm is None:
        out.update({"diff": None, "ci95": None, "verdict": "collecting"})
        return out
    diff = pm - bm
    out["diff"] = round(diff, digits)
    need = BASELINE_MIN_DAYS if min_days is None else min_days
    if n1 < need or n2 < need or bs is None or ps is None:
        out.update({"ci95": None, "verdict": "collecting"})
        return out
    # Consecutive days are not independent samples: a damp week is damp on
    # Tuesday because it was damp on Monday. Counting each day as a fresh
    # observation makes the interval too narrow and turns weather into
    # "evidence". Each side is discounted to the number of independent days it
    # is really worth. The day-count gate above still uses the RAW count,
    # because that gate is about data coverage, not information.
    e1 = _effective_days(base_vals, base_days)
    e2 = _effective_days(post_vals, post_days)
    out["baseline_days_eff"], out["post_days_eff"] = e1, e2
    v1, v2 = bs ** 2 / e1, ps ** 2 / e2
    se = math.sqrt(v1 + v2)
    if se <= 0:
        # Neither side varied at all — a stuck or pinned sensor, not a
        # perfectly clean result. Calling that "real" with a zero-width
        # interval would be the most confident wrong answer this function
        # could give.
        out.update({"ci95": None, "verdict": "collecting"})
        return out
    # Welch-Satterthwaite dof, on the discounted counts
    if e1 < 2 or e2 < 2:
        out.update({"ci95": None, "verdict": "collecting"})
        return out
    dof = (v1 + v2) ** 2 / (v1 ** 2 / (e1 - 1) + v2 ** 2 / (e2 - 1))
    ci = _t_crit_95(int(dof)) * se
    out["ci95"] = round(ci, digits)
    out["verdict"] = "real" if abs(diff) > ci else "noise"
    return out


# If outdoor dew point moved between the baseline and post periods by at
# least this fraction of the observed crawl dew-point change (same sign),
# the change is flagged as season-confounded rather than certified real.
CONFOUND_FRACTION = 0.5


def intervention_report(daily_stats, interventions, outdoor_days=None):
    """For each marker: freeze the preceding period (back to the previous
    marker or data start, capped at BASELINE_MAX_DAYS) as its baseline, take
    from the marker to the next marker (or today, capped at
    BASELINE_MAX_DAYS) as post, and compare daily means of: crawl RH, crawl
    dew point, hours>60 and >70 per day.

    Seasonal honesty: an uncontrolled before/after straddling a season change
    would certify autumn as a successful vapor barrier. If outdoor dew point
    shifted in the same direction by >= CONFOUND_FRACTION of the crawl's
    dew-point change, the dp/rh verdicts are downgraded to 'confounded' and
    the outdoor shift is reported alongside."""
    out = []
    outdoor_by_day = {d["day"]: d.get("dp_mean") for d in (outdoor_days or [])}
    marks = sorted(interventions, key=lambda i: i["marked_on"])
    for idx, iv in enumerate(marks):
        d0 = iv["marked_on"]
        prev_mark = marks[idx - 1]["marked_on"] if idx > 0 else None
        next_mark = marks[idx + 1]["marked_on"] if idx + 1 < len(marks) else None
        base_days = [d for d in daily_stats
                     if d["day"] < d0
                     and (prev_mark is None or d["day"] >= prev_mark)
                     and d["day"] >= d0 - timedelta(days=BASELINE_MAX_DAYS)]
        post_days = [d for d in daily_stats
                     if d["day"] >= d0
                     and (next_mark is None or d["day"] < next_mark)
                     and d["day"] < d0 + timedelta(days=BASELINE_MAX_DAYS)]
        def pick(rows, key):
            """Values and their calendar days, so the autocorrelation discount
            can tell a real neighbour from one across an outage."""
            usable = [d for d in rows if d.get(key) is not None]
            return [d[key] for d in usable], [d["day"] for d in usable]

        def compare(key):
            bv, bd = pick(base_days, key)
            pv, pd = pick(post_days, key)
            return _metric_compare(bv, pv, base_days=bd, post_days=pd)

        metrics = {
            "rh_mean": compare("rh_mean"), "dp_mean": compare("dp_mean"),
            "h60_per_day": compare("h60"), "h70_per_day": compare("h70"),
        }

        # Seasonal confound check on the moisture metrics.
        out_base = [outdoor_by_day[d["day"]] for d in base_days
                    if outdoor_by_day.get(d["day"]) is not None]
        out_post = [outdoor_by_day[d["day"]] for d in post_days
                    if outdoor_by_day.get(d["day"]) is not None]
        outdoor_shift = None
        if len(out_base) >= 3 and len(out_post) >= 3:
            outdoor_shift = round(sum(out_post) / len(out_post)
                                  - sum(out_base) / len(out_base), 1)
            for key in ("dp_mean", "rh_mean"):
                m = metrics[key]
                d = m.get("diff")
                if (m["verdict"] == "real" and d is not None and outdoor_shift != 0
                        and (d > 0) == (outdoor_shift > 0)
                        and abs(outdoor_shift) >= CONFOUND_FRACTION * abs(d)):
                    m["verdict"] = "confounded"

        verdicts = [m["verdict"] for m in metrics.values()]
        if all(v == "collecting" for v in verdicts):
            overall = "collecting"
        elif any(v == "real" for v in verdicts):
            overall = "real_change"
        elif any(v == "confounded" for v in verdicts):
            overall = "confounded"
        else:
            overall = "no_change_detected"
        out.append({"id": iv["id"], "marked_on": d0.isoformat(),
                    "label": iv["label"], "note": iv.get("note"),
                    "baseline_from": base_days[0]["day"].isoformat() if base_days else None,
                    "baseline_days": len(base_days), "post_days": len(post_days),
                    "outdoor_dp_shift": outdoor_shift,
                    "metrics": metrics, "overall": overall})
    return out


# ---------------------------------------------------------------------------
# Crawl-to-floor absolute-humidity gap
# ---------------------------------------------------------------------------
# The gap answers "how much more water does a cubic metre of crawl air carry
# than a cubic metre of air on this floor?" in g/m^3. Absolute humidity, not
# RH and not dew point: RH is not comparable between a 55F crawl and a 72F
# hallway, and dew point compares vapour PRESSURE rather than the vapour mass
# a given volume of air actually carries.
# Days after a marker to leave out of the "after" window. An open hatch,
# people crawling around and disturbed soil produce a transient that is not
# the steady-state result of the work.
INSTALL_SETTLE_DAYS = 7
# Days needed on each side of a marker before the gap comparison will speak.
# Higher than BASELINE_MIN_DAYS because daily gaps are more autocorrelated
# than the crawl's own humidity, so ten days buys fewer independent ones.
GAP_MIN_DAYS = 14


# Readings a day must carry before its absolute-humidity mean is trusted.
# SQL `avg` silently skips nulls, so a day where the dew point was recorded
# twice would otherwise sit in a Welch-t comparison weighing exactly as much
# as a fully observed one.
MIN_DAY_AH_READINGS = 24


def _day_ah(day):
    """A day's mean absolute humidity, or None if too little of it was
    observed to stand as a day."""
    if (day.get("ah_n") or 0) < MIN_DAY_AH_READINGS:
        return None
    return day.get("ah_mean")


def ah_excess_daily(sensor_daily, outdoor_daily):
    """Daily-mean absolute humidity above the outdoor air, in g/m^3.

    Outdoor moisture swings hard across a season and drags every sensor in the
    house with it, so a raw crawl-to-floor gap drifts for reasons that have
    nothing to do with the building. Measuring each sensor against the outdoor
    air on the same day removes that: the crawl's excess is how much the
    ground and the structure are adding, and a floor's excess is how much of
    it is reaching the living space.
    """
    out_by_day = {d["day"]: _day_ah(d) for d in outdoor_daily}
    rows = []
    for d in sensor_daily:
        v = _day_ah(d)
        o = out_by_day.get(d["day"])
        if v is None or o is None:
            continue
        rows.append({"day": d["day"], "ah": v, "outdoor": o, "excess": v - o})
    return rows


def ah_gap_hourly(crawl_hourly, floor_hourly):
    """Pair hourly crawl AH against one floor's hourly AH on the shared hour
    buckets. Returns [{bucket, crawl, floor, gap}] oldest first; hours where
    either side is missing are dropped rather than carried as a null gap."""
    floor_by_bucket = {r["bucket"]: r["ah"] for r in floor_hourly
                       if r.get("ah") is not None}
    out = []
    for c in crawl_hourly:
        if c.get("ah") is None:
            continue
        f = floor_by_bucket.get(c["bucket"])
        if f is None:
            continue
        out.append({"bucket": c["bucket"], "crawl": c["ah"], "floor": f,
                    "gap": c["ah"] - f})
    return out


def ah_gap_daily(crawl_daily, floor_daily):
    """Daily-mean crawl AH minus daily-mean floor AH, on days both sensors
    reported. Returns [{day, crawl, floor, gap}] oldest first."""
    floor_by_day = {d["day"]: _day_ah(d) for d in floor_daily}
    out = []
    for d in crawl_daily:
        c = _day_ah(d)
        f = floor_by_day.get(d["day"])
        if c is None or f is None:
            continue
        out.append({"day": d["day"], "crawl": c, "floor": f, "gap": c - f})
    return out


def change_across(rows, key, d0, settle_days=None, max_days=None):
    """Before/after change in a daily series across one marker, with the same
    windows, the same day bar and the same interval machinery the gap
    comparison uses — so the prediction test and the gap table can never
    disagree about what "after" means or how confident it is.

    Returns (diff, ci95). Either is None when the windows cannot support a
    figure; a None interval means "not measured", never "measured as zero".
    """
    settle = INSTALL_SETTLE_DAYS if settle_days is None else settle_days
    span = BASELINE_MAX_DAYS if max_days is None else max_days
    base = [r for r in rows if d0 - timedelta(days=span) <= r["day"] < d0
            and r.get(key) is not None]
    post = [r for r in rows if d0 + timedelta(days=settle) <= r["day"]
            < d0 + timedelta(days=span) and r.get(key) is not None]
    m = _metric_compare([r[key] for r in base], [r[key] for r in post],
                        digits=3, min_days=GAP_MIN_DAYS,
                        base_days=[r["day"] for r in base],
                        post_days=[r["day"] for r in post])
    if m["verdict"] == "collecting":
        return m.get("diff"), None
    return m.get("diff"), m.get("ci95")


def gap_intervention_report(gap_daily, interventions, outdoor_daily=None):
    """Before/after the crawl-to-floor gap across each intervention marker,
    using the same windows and the same Welch-t 95% bar as intervention_report.

    Deliberately NOT given a directional verdict. A ground vapour barrier
    lowers crawl AH and narrows the gap; air-sealing decouples the floor and
    WIDENS it. Both are successes, so calling a sign 'good' here would be
    guessing at which mechanism the work targeted. The report states the
    measured change and whether it clears noise; the coupling metric
    (ah_coupling_window) is the direction-unambiguous mechanism test.
    """
    outdoor_by_day = {d["day"]: _day_ah(d) for d in (outdoor_daily or [])}
    marks = sorted(interventions, key=lambda i: i["marked_on"])
    out = []
    for idx, iv in enumerate(marks):
        d0 = iv["marked_on"]
        prev_mark = marks[idx - 1]["marked_on"] if idx > 0 else None
        next_mark = marks[idx + 1]["marked_on"] if idx + 1 < len(marks) else None
        base = [g for g in gap_daily
                if g["day"] < d0
                and (prev_mark is None or g["day"] >= prev_mark)
                and g["day"] >= d0 - timedelta(days=BASELINE_MAX_DAYS)]
        settled = d0 + timedelta(days=INSTALL_SETTLE_DAYS)
        post = [g for g in gap_daily
                if g["day"] >= settled
                and (next_mark is None or g["day"] < next_mark)
                and g["day"] < d0 + timedelta(days=BASELINE_MAX_DAYS)]
        metric = _metric_compare([g["gap"] for g in base],
                                 [g["gap"] for g in post], digits=2,
                                 min_days=GAP_MIN_DAYS,
                                 base_days=[g["day"] for g in base],
                                 post_days=[g["day"] for g in post])

        # Same seasonal honesty as intervention_report: outdoor AH swings
        # hard between seasons and drags both sensors with it.
        ob = [outdoor_by_day[g["day"]] for g in base
              if outdoor_by_day.get(g["day"]) is not None]
        op = [outdoor_by_day[g["day"]] for g in post
              if outdoor_by_day.get(g["day"]) is not None]
        outdoor_shift = None
        outdoor_checked = len(ob) >= 3 and len(op) >= 3
        if outdoor_checked:
            outdoor_shift = round(sum(op) / len(op) - sum(ob) / len(ob), 2)
            d = metric.get("diff")
            if (metric["verdict"] == "real" and d is not None and outdoor_shift != 0
                    and (d > 0) == (outdoor_shift > 0)
                    and abs(outdoor_shift) >= CONFOUND_FRACTION * abs(d)):
                metric["verdict"] = "confounded"
        elif metric["verdict"] == "real":
            # Without outdoor data on both sides, a seasonal swing cannot be
            # ruled out. An unchecked result must not be displayed the same way
            # as one that passed the check.
            metric["verdict"] = "unchecked"

        out.append({"id": iv["id"], "marked_on": d0.isoformat(),
                    "label": iv["label"],
                    "baseline_days": len(base), "post_days": len(post),
                    "settle_days": INSTALL_SETTLE_DAYS,
                    "outdoor_ah_shift": outdoor_shift,
                    "outdoor_checked": outdoor_checked,
                    "metric": metric})
    return out


# ---------------------------------------------------------------------------
# Winter projection: crawl dp ~ a + b*outdoor_temp + c*outdoor_dp
# ---------------------------------------------------------------------------

def _solve3(a, b):
    """Solve a 3x3 linear system via Gaussian elimination. Returns None if
    singular (collinear predictors)."""
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(3):
            if r == col:
                continue
            f = m[r][col] / m[col][col]
            for c in range(col, 4):
                m[r][c] -= f * m[col][c]
    return [m[i][3] / m[i][i] for i in range(3)]


def winter_projection(crawl_daily, outdoor_days):
    """OLS fit of daily-mean crawl dew point against daily-mean outdoor temp
    and dew point, evaluated at a representative cold-winter design point, with a 95%
    prediction interval. Refuses (ready=False) until the fit has both enough
    days and enough outdoor-temperature span to extrapolate honestly."""
    out_by_day = {d["day"]: d for d in outdoor_days}
    rows = []
    for d in crawl_daily:
        o = out_by_day.get(d["day"])
        if (d.get("dp_mean") is not None and o is not None
                and o.get("temp_mean") is not None and o.get("dp_mean") is not None):
            rows.append((o["temp_mean"], o["dp_mean"], d["dp_mean"]))
    n = len(rows)
    temps = [r[0] for r in rows]
    span = (max(temps) - min(temps)) if temps else 0.0
    base = {"n_days": n, "temp_span_f": round(span, 1),
            "need_days": PROJ_MIN_DAYS, "need_span_f": PROJ_MIN_TEMP_SPAN_F}
    if n < PROJ_MIN_DAYS:
        return {"ready": False, "reason": "collecting", **base}
    if span < PROJ_MIN_TEMP_SPAN_F:
        return {"ready": False, "reason": "narrow_temp_range", **base}

    # normal equations for [1, t, dp]
    sx = [[0.0] * 3 for _ in range(3)]
    sy = [0.0] * 3
    for t, dp, y in rows:
        x = (1.0, t, dp)
        for i in range(3):
            sy[i] += x[i] * y
            for jj in range(3):
                sx[i][jj] += x[i] * x[jj]
    beta = _solve3(sx, sy)
    if beta is None:
        return {"ready": False, "reason": "collinear", **base}

    resid = [y - (beta[0] + beta[1] * t + beta[2] * dp) for t, dp, y in rows]
    dof = n - 3
    if dof < 5:
        return {"ready": False, "reason": "collecting", **base}
    s2 = sum(r * r for r in resid) / dof
    x0 = (1.0, WINTER_TEMP_F, WINTER_DP_F)
    pred = beta[0] + beta[1] * x0[1] + beta[2] * x0[2]
    # Full prediction interval: s * sqrt(1 + x0'(X'X)^-1 x0). The leverage
    # term matters here precisely BECAUSE the winter design point sits at the
    # edge of (or beyond) the observed range — it widens the interval to
    # reflect that extrapolation honestly.
    v = _solve3(sx, list(x0))
    leverage = sum(a * b for a, b in zip(x0, v)) if v is not None else 0.0
    ci = 1.96 * math.sqrt(s2 * (1.0 + max(leverage, 0.0)))
    return {"ready": True, **base,
            "predicted_dp_f": round(pred, 1), "ci95_f": round(ci, 1),
            "at_outdoor_temp_f": WINTER_TEMP_F, "at_outdoor_dp_f": WINTER_DP_F,
            "resid_se_f": round(math.sqrt(s2), 2)}
