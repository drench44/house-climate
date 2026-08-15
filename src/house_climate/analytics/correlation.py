from statistics import mean
from zoneinfo import ZoneInfo


def cooling_degree_days(readings, base_f=65.0, tz="America/Los_Angeles",
                        max_gap_s=600) -> float:
    """Time-weighted CDD per local day. Each reading is weighted by the
    (gap-capped) interval it covers — a plain mean of surviving readings
    would let a poller outage bias a day toward whatever hours survived
    (e.g. only-afternoon readings reading 17 CDD on a 5 CDD day)."""
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


def predict_peak_cost(fc_high_f, history, tou, system_kw, tz, target_date=None) -> dict:
    """Predict tomorrow's cooling and what the 17:00-21:00 window of it costs.

    history: [{day_high, cool_minutes, peak_cool_minutes}] per past local day.
    Peak dollars are computed from predicted PEAK-WINDOW minutes only —
    pricing the whole day's cooling at the peak rate overstated the figure
    ~3x (most cooling happens outside 17:00-21:00 even with no shifting).
    The rate comes from the band actually in force at 17:30 on target_date,
    so a weekend "peak window" is honestly priced at the off-peak rate.
    """
    highs = [h["day_high"] for h in history]
    mins = [h["cool_minutes"] for h in history]
    peak_mins = [h["peak_cool_minutes"] for h in history]
    if len(history) >= 3:
        slope, intercept = _linfit(highs, mins)
        pred = max(0.0, slope * fc_high_f + intercept)
        pslope, pintercept = _linfit(highs, peak_mins)
        pred_peak = max(0.0, pslope * fc_high_f + pintercept)
        basis = "linear fit"
    else:
        pred = mean(mins) if mins else 0.0
        pred_peak = mean(peak_mins) if peak_mins else 0.0
        basis = "historical mean"
    # The peak window is 4h and is a subset of the day — enforce both.
    pred_peak = min(pred_peak, pred, 240.0)

    band = "peak"
    rate = next((b.rate for b in tou.bands if b.name == "peak"), 0.0)
    if target_date is not None:
        from datetime import datetime, time
        probe = datetime.combine(target_date, time(17, 30), tzinfo=ZoneInfo(tz))
        band, rate = tou.band_for(probe)
    dollars = pred_peak / 60.0 * system_kw * rate
    return {"predicted_cool_minutes": pred,
            "predicted_peak_cool_minutes": pred_peak,
            "predicted_peak_dollars": dollars,
            "peak_band": band, "peak_rate_used": rate,
            "basis": basis}
