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
    0.5 bar ~18% of the time at n=10-15 — a coin-flip-ish 'buy a sump pump'."""
    dof = n - 2
    if dof < 1:
        return 1.0
    t = _T_CRIT_BONF6[-1][1]
    for max_dof, tv in _T_CRIT_BONF6:
        if dof <= max_dof:
            t = tv
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

# Two-sided 95% t critical values by dof (Welch), interpolated conservatively.
_T_CRIT_95 = [(9, 2.26), (12, 2.18), (15, 2.13), (20, 2.09),
              (30, 2.04), (60, 2.00), (10 ** 9, 1.96)]


def _t_crit_95(dof):
    for max_dof, tv in _T_CRIT_95:
        if dof <= max_dof:
            return tv
    return 1.96


def _metric_compare(base_vals, post_vals):
    """Difference of daily means with a 95% Welch-t CI. ONE bar for both the
    interval and the call: verdict is 'real' exactly when |diff| exceeds the
    displayed ci95 — showing an interval that excludes zero labeled 'noise'
    (the old 1.96-CI vs 2.0-z mismatch) presented contradictory evidence."""
    bm, bs = _mean_std(base_vals)
    pm, ps = _mean_std(post_vals)
    n1, n2 = len(base_vals), len(post_vals)
    out = {"baseline_mean": round(bm, 1) if bm is not None else None,
           "baseline_sd": round(bs, 1) if bs is not None else None,
           "baseline_n": n1,
           "post_mean": round(pm, 1) if pm is not None else None,
           "post_sd": round(ps, 1) if ps is not None else None,
           "post_n": n2}
    if bm is None or pm is None:
        out.update({"diff": None, "ci95": None, "verdict": "collecting"})
        return out
    diff = pm - bm
    out["diff"] = round(diff, 1)
    if n1 < BASELINE_MIN_DAYS or n2 < BASELINE_MIN_DAYS or bs is None or ps is None:
        out.update({"ci95": None, "verdict": "collecting"})
        return out
    v1, v2 = bs ** 2 / n1, ps ** 2 / n2
    se = math.sqrt(v1 + v2)
    if se <= 0:
        out.update({"ci95": 0.0, "verdict": "real" if diff != 0 else "noise"})
        return out
    # Welch-Satterthwaite dof (always between min(n)-1 and n1+n2-2)
    dof = (v1 + v2) ** 2 / (v1 ** 2 / (n1 - 1) + v2 ** 2 / (n2 - 1))
    ci = _t_crit_95(int(dof)) * se
    out["ci95"] = round(ci, 1)
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
        pick = lambda days, key: [d[key] for d in days if d.get(key) is not None]
        metrics = {
            "rh_mean": _metric_compare(pick(base_days, "rh_mean"), pick(post_days, "rh_mean")),
            "dp_mean": _metric_compare(pick(base_days, "dp_mean"), pick(post_days, "dp_mean")),
            "h60_per_day": _metric_compare(pick(base_days, "h60"), pick(post_days, "h60")),
            "h70_per_day": _metric_compare(pick(base_days, "h70"), pick(post_days, "h70")),
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
