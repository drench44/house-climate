"""Thermal characterization of the building envelope, from the Daikin readings.

Two estimates:

- coasting_constant: fit the passive-drift model  dT_in/dt = (T_out - T_in)/tau
  over IDLE periods (no active heating/cooling). `tau` is the building time
  constant in hours — larger means better insulation + more thermal mass. The
  intuitive spin-offs are the half-life (hours to close half an indoor/outdoor
  gap) and the drift rate at a 20 deg-F gap.

- cooling_load_curve: cooling runtime fraction vs outdoor temperature. The
  x-intercept is the balance point (below it, no cooling needed); the slope is
  the cooling load added per degree of outdoor temperature.

Both need weeks of varied weather to be reliable, so each returns
{"ready": False, "reason": "collecting", ...} while the data is too thin — the
UI shows an honest "learning" state that sharpens as history accumulates.
"""
from collections import defaultdict

_IDLE = "idle"
_COOL = {"cooling", "overcool"}
_MAX_GAP_H = 0.5   # ignore a step spanning > 30 min (missed polls)


def _outdoor(r):
    """Outdoor temp, preferring the Daikin's own sensor over the weather feed."""
    v = r.get("daikin_outdoor_temp_f")
    return v if v is not None else r.get("wx_outdoor_temp_f")


def coasting_constant(readings, *, min_run_min=15, min_gap_f=2.0, min_samples=25):
    """Building time constant tau (hours) from passive drift during idle runs."""
    rows = sorted(readings, key=lambda r: r["ts"])
    xs, ys = [], []   # (gap, drift_rate) pairs

    def flush(run):
        if len(run) < 2:
            return
        span_min = (run[-1]["ts"] - run[0]["ts"]).total_seconds() / 60.0
        if span_min < min_run_min:
            return
        for a, b in zip(run, run[1:]):
            ti_a, ti_b, to = a.get("indoor_temp_f"), b.get("indoor_temp_f"), _outdoor(a)
            if ti_a is None or ti_b is None or to is None:
                continue
            dt_h = (b["ts"] - a["ts"]).total_seconds() / 3600.0
            if dt_h <= 0 or dt_h > _MAX_GAP_H:
                continue
            gap = to - ti_a
            if abs(gap) < min_gap_f:      # tiny gaps are all noise
                continue
            xs.append(gap)
            ys.append((ti_b - ti_a) / dt_h)   # drift, deg-F/hr

    run = []
    for r in rows:
        if r.get("equipment_status") == _IDLE:
            run.append(r)
        else:
            flush(run)
            run = []
    flush(run)

    if len(xs) < min_samples:
        return {"ready": False, "reason": "collecting", "samples": len(xs)}
    sxx = sum(x * x for x in xs)
    if sxx <= 0:
        return {"ready": False, "reason": "collecting", "samples": len(xs)}
    k = sum(x * y for x, y in zip(xs, ys)) / sxx   # slope through origin, per hour
    if k <= 1e-4:
        return {"ready": False, "reason": "no_signal", "samples": len(xs)}
    tau = 1.0 / k
    ss_res = sum((y - k * x) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum(y * y for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {
        "ready": True,
        "tau_hours": round(tau, 1),
        "half_life_hours": round(tau * 0.6931, 1),
        "drift_per_hr_at_20f": round(k * 20, 2),
        "samples": len(xs),
        "r2": round(r2, 2),
    }


def cooling_load_curve(readings, *, bin_f=2.0, min_bins=5, min_spread_f=10.0,
                       min_bin_minutes=9.0, min_r2=0.6):
    """Cooling runtime fraction vs outdoor temp -> balance point + load slope."""
    rows = sorted(readings, key=lambda r: r["ts"])
    cool_min = defaultdict(float)
    tot_min = defaultdict(float)
    for a, b in zip(rows, rows[1:]):
        to = _outdoor(a)
        if to is None:
            continue
        dt_min = (b["ts"] - a["ts"]).total_seconds() / 60.0
        if dt_min <= 0 or dt_min > _MAX_GAP_H * 60:
            continue
        binf = round(to / bin_f) * bin_f
        tot_min[binf] += dt_min
        if a.get("equipment_status") in _COOL:
            cool_min[binf] += dt_min

    pts = [(binf, cool_min[binf] / tot) for binf, tot in tot_min.items()
           if tot >= min_bin_minutes]
    pts.sort()
    if len(pts) < min_bins or (pts[-1][0] - pts[0][0]) < min_spread_f:
        return {"ready": False, "reason": "collecting", "bins": len(pts)}

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or sxy <= 0:      # no positive load-vs-temp trend yet
        return {"ready": False, "reason": "no_signal", "bins": len(pts)}
    slope = sxy / sxx
    intercept = my - slope * mx
    # Fit quality: a low R^2 means the runtime-vs-temp relationship is still too
    # noisy to trust (e.g. the AC ran for reasons other than outdoor heat, like
    # manual testing or pre-cool). Report "noisy" rather than a confident-but-
    # wrong balance point until the cloud of points actually lines up.
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    if r2 < min_r2:
        return {"ready": False, "reason": "noisy", "bins": len(pts), "r2": round(r2, 2)}
    balance = -intercept / slope
    # Plausibility: a cooling balance point outside ~50-80 deg-F is physically
    # implausible for a home and means the fit is still contaminated (e.g. the
    # AC ran at cool outdoor temps because of manual testing or pre-cool). Hold
    # at "learning" rather than publish a nonsense number.
    if not (50.0 <= balance <= 80.0):
        return {"ready": False, "reason": "noisy", "bins": len(pts), "r2": round(r2, 2)}
    return {
        "ready": True,
        "balance_point_f": round(balance, 1),
        "slope_pct_per_f": round(slope * 100, 1),
        "bins": len(pts),
        "r2": round(r2, 2),
        "points": [[round(x, 1), round(y * 100, 1)] for x, y in pts],
    }
