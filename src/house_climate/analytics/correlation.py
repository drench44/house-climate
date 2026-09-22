from statistics import mean
from zoneinfo import ZoneInfo


def cooling_degree_days(readings, base_f=65.0, tz="America/Los_Angeles",
                        max_gap_s=600) -> float:
    """Cumulative cooling-degree-days (degF-days) SUMMED over the reading
    window -- so the value grows with window length; it is not a single-day
    figure. Each day's CDD is time-weighted: a reading is weighted by the
    (gap-capped) interval it covers, so a poller outage cannot bias a day
    toward whatever hours survived (e.g. only-afternoon readings reading 17
    CDD on a 5 CDD day). Note the weighting corrects INTRA-day bias only: a
    sparse outage day still contributes a whole day's degree-day, just weighted
    by the hours it observed."""
    zone = ZoneInfo(tz)
    rows = sorted(readings, key=lambda r: r["ts"])
    by_day = {}
    for a, b in zip(rows, rows[1:]):
        t = a.get("wx_outdoor_temp_f")
        if t is None:
            continue
        dt = min((b["ts"] - a["ts"]).total_seconds(), max_gap_s)
        if dt <= 0:
            continue
        day = a["ts"].astimezone(zone).date()
        acc = by_day.setdefault(day, [0.0, 0.0])
        acc[0] += t * dt
        acc[1] += dt
    return sum(max(0.0, s / w - base_f) for s, w in by_day.values() if w > 0)


def _linfit(xs, ys):
    mx, my = mean(xs), mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return slope, my - slope * mx


def _window_minutes(start, end) -> float:
    """Length of a [start, end) wall-clock window in minutes, wrap-aware."""
    s = start.hour * 60 + start.minute
    e = end.hour * 60 + end.minute
    if e <= s:
        e += 24 * 60
    return float(e - s)


def _mid_time(start, end):
    """The midpoint time of a [start, end) window, wrap-aware."""
    from datetime import time
    s = start.hour * 60 + start.minute
    e = end.hour * 60 + end.minute
    if e <= s:
        e += 24 * 60
    m = (s + (e - s) // 2) % (24 * 60)
    return time(m // 60, m % 60)


def _day_peak_windows(tou, day, weekend):
    """On-peak windows that apply on `day`'s day-type, as (start, end, None)
    merged runs of top-rate bands. tou.peak_windows() prefers weekday/all-day
    bands and drops weekend ones when both exist, which would leave a weekend
    peak with no window (priced at a midday off-peak rate)."""
    season = tou.season(day.month)
    top = tou.peak_rate(season)
    if top is None:
        return []
    kinds = ("all", "weekend") if weekend else ("all", "weekday")
    bands = sorted((b for b in tou.bands
                    if b.season == season and b.rate == top and b.days in kinds),
                   key=lambda b: (b.start, b.end))
    merged = []
    for b in bands:
        if merged and b.start <= merged[-1][1]:
            s, e, _ = merged[-1]
            merged[-1] = (s, max(e, b.end), None)
        else:
            merged.append((b.start, b.end, None))
    return merged


def predict_peak_cost(fc_high_f, history, tou, system_kw, tz, target_date=None) -> dict:
    """Predict tomorrow's cooling and what its on-peak window costs.

    history: [{day_high, cool_minutes, peak_cool_minutes, has_peak}] per past
    local day. `has_peak` (default True when absent) says whether that day had
    a peak window at all.
    Peak dollars come from predicted PEAK-WINDOW minutes only — pricing the
    whole day's cooling at the peak rate overstated the figure ~3x (most
    cooling happens outside the peak window even with no shifting).

    The peak-minutes fit uses ONLY days that had a peak window. A weekend day
    under a weekday-only peak enters history with peak_cool_minutes=0 because
    there was no window to cool in, not because the house needed no cooling
    then; fitting those zeros together with weekdays read ~29% low.

    The on-peak window and its rate are derived from the TOU table for
    target_date's season/day-type (via tou.day_has_peak / tou.peak_windows /
    tou.band_for), NOT a hardcoded 17:00-21:00. A target day with no peak
    window (a weekend under a weekday-only peak, a flat season) has zero peak
    exposure: has_peak False, zero minutes, zero dollars.

    predicted_peak_cool_minutes / predicted_peak_dollars are None when the
    target day has a peak window but no past day with one exists to learn
    from: unknown, not zero.
    """
    from datetime import datetime, time
    highs = [h["day_high"] for h in history]
    mins = [h["cool_minutes"] for h in history]
    peak_hist = [h for h in history if h.get("has_peak", True)]
    p_highs = [h["day_high"] for h in peak_hist]
    p_mins = [h["peak_cool_minutes"] for h in peak_hist]
    if len(history) >= 3:
        slope, intercept = _linfit(highs, mins)
        pred = max(0.0, slope * fc_high_f + intercept)
        basis = "linear fit"
    else:
        pred = mean(mins) if mins else 0.0
        basis = "historical mean"
    if len(peak_hist) >= 3:
        pslope, pintercept = _linfit(p_highs, p_mins)
        pred_peak = max(0.0, pslope * fc_high_f + pintercept)
    elif peak_hist:
        pred_peak = mean(p_mins)
    else:
        pred_peak = None

    # Peak window(s) + the rate in force during them, all from the table.
    zone = ZoneInfo(tz)
    top = max((b.rate for b in tou.bands), default=0.0)
    band = next((b.name for b in tou.bands if b.rate == top), None)
    rate = top
    win_minutes = 240.0                     # fallback cap (no date -> assume 4h)
    has_peak = True
    windows = []
    if target_date is not None:
        has_peak = tou.day_has_peak(target_date, zone)
        weekend = target_date.weekday() >= 5
        wins = _day_peak_windows(tou, target_date, weekend) if has_peak else []
        if wins:
            # Sum every hump (a two-humped peak has two windows), and probe the
            # rate at the FIRST hump's midpoint — a guaranteed on-peak instant,
            # so a disjoint peak isn't mispriced at the midday off-peak trough.
            win_minutes = sum(_window_minutes(s, e) for s, e, _ in wins)
            probe = _mid_time(wins[0][0], wins[0][1])
        else:
            probe = time(12, 0)             # no window: any midday minute
        band, rate = tou.band_for(datetime.combine(target_date, probe, tzinfo=zone))
        windows = [{"start": s.strftime("%H:%M"), "end": e.strftime("%H:%M")}
                   for s, e, _ in wins]

    if not has_peak:
        pred_peak, dollars = 0.0, 0.0
    elif pred_peak is None:
        dollars = None
    else:
        # Peak minutes are a subset of the day AND cannot exceed the window.
        pred_peak = min(pred_peak, pred, win_minutes)
        dollars = pred_peak / 60.0 * system_kw * rate
    return {"predicted_cool_minutes": pred,
            "predicted_peak_cool_minutes": pred_peak,
            "predicted_peak_dollars": dollars,
            "peak_band": band, "peak_rate_used": rate,
            "has_peak": has_peak, "peak_windows": windows,
            "peak_days_of_history": len(peak_hist),
            "basis": basis}
