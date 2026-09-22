from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import timedelta
from zoneinfo import ZoneInfo

_RUNNING = {"cooling", "overcool", "heating"}
MAX_GAP_S = 600


def _window_indices(rows, start, end, max_gap_s):
    """Index range of the rows whose credited interval can have its midpoint in
    [start, end). A row starting up to max_gap_s before `start` can still land
    its midpoint inside, so the range reaches back that far."""
    stamps = [r["ts"] for r in rows]
    lo = bisect_left(stamps, start - timedelta(seconds=max_gap_s)) if start is not None else 0
    hi = bisect_left(stamps, end) if end is not None else len(rows)
    return lo, hi


def credited_intervals(readings, *, start=None, end=None, max_gap_s=MAX_GAP_S):
    """The accounting unit every time-sliced total shares: one interval per
    reading, from its timestamp to the next reading's, credited with at most
    max_gap_s (anything longer is unobserved, never invented), and attributed
    to the window that holds its MIDPOINT, the same rule band pricing uses.

    Yields (row, credited_minutes, midpoint_utc). `readings` should reach past
    both window edges (the row just before `start` and just after `end`): the
    interval that straddles midnight belongs to whichever day holds its
    midpoint, and slicing the rows by their own timestamp first gave the last
    row of every day zero minutes."""
    rows = sorted(readings, key=lambda r: r["ts"])
    lo, hi = _window_indices(rows, start, end, max_gap_s)
    for i in range(lo, hi):
        row = rows[i]
        dt = (rows[i + 1]["ts"] - row["ts"]).total_seconds() if i + 1 < len(rows) else 0
        mins = min(dt, max_gap_s) / 60.0
        if mins <= 0:
            continue
        mid = row["ts"] + timedelta(minutes=mins / 2)
        if start is not None and mid < start:
            continue
        if end is not None and mid >= end:
            continue
        yield row, mins, mid


def observed_seconds(readings, start, end, *, max_gap_s=MAX_GAP_S):
    """Seconds of [start, end) that readings actually vouch for: each reading
    covers from its timestamp to the next one, at most max_gap_s. What is left
    over is time nobody observed, where equipment could have run uncounted."""
    rows = sorted(readings, key=lambda r: r["ts"])
    lo, hi = _window_indices(rows, start, end, max_gap_s)
    total = 0.0
    for i in range(lo, hi):
        a = rows[i]["ts"]
        dt = (rows[i + 1]["ts"] - a).total_seconds() if i + 1 < len(rows) else 0
        b = a + timedelta(seconds=min(dt, max_gap_s))
        overlap = (min(b, end) - max(a, start)).total_seconds()
        if overlap > 0:
            total += overlap
    return total


@dataclass
class CostResult:
    by_band: dict = field(default_factory=dict)
    total_dollars: float = 0.0
    total_kwh: float = 0.0
    pct_runtime_peak: float = 0.0


def compute(readings, tou, system_kw, tz, *, max_gap_s=MAX_GAP_S, heat_kw=None,
            start=None, end=None) -> CostResult:
    """Estimated cost of the runtime in `readings`. With `start`/`end`, only
    intervals whose midpoint falls in [start, end) are counted, so a day, a
    week or a month can be priced from rows that run past its edges and every
    interval lands in exactly one slice (see credited_intervals)."""
    zone = ZoneInfo(tz)
    res = CostResult()
    running_min = 0.0
    seasons_run = set()   # seasons in which cooling/heating actually ran
    for row, mins, mid in credited_intervals(readings, start=start, end=end,
                                             max_gap_s=max_gap_s):
        status = row["equipment_status"]
        if status not in _RUNNING:
            continue
        # Price each interval at its MIDPOINT's band, not its start's: start
        # attribution systematically misprices the interval straddling every
        # band boundary (16:58->17:01 billed entirely mid-peak, 20:58->21:01
        # entirely peak). Midpoint makes the expected boundary error ~zero.
        local = mid.astimezone(zone)
        seasons_run.add(tou.season(local.month))
        band, rate = tou.band_for(local)
        kw = (heat_kw if heat_kw is not None else system_kw) if status == "heating" else system_kw
        kwh = mins / 60.0 * kw
        dollars = kwh * rate
        b = res.by_band.setdefault(band, {"minutes": 0.0, "kwh": 0.0, "dollars": 0.0})
        b["minutes"] += mins; b["kwh"] += kwh; b["dollars"] += dollars
        res.total_kwh += kwh; res.total_dollars += dollars
        running_min += mins
    # "% of runtime in the peak band" -- the peak band(s) are those at the
    # highest rate among the bands APPLICABLE TO THE SEASON(S) that actually
    # ran (name-independent so "on-peak" etc. works), NOT "the highest rate
    # that happened to run". Two subtleties this guards:
    #  - If cooling ran only off-peak, the peak band simply didn't run, so
    #    peak_min stays 0 and pct is 0 -- not 100 (picking the max among only
    #    the bands that ran mislabels off-peak-only runtime as 100% peak).
    #  - Restricting to seasons_run keeps a DIFFERENT season's higher peak rate
    #    (e.g. winter @ $0.44 vs summer @ $0.40) from stealing the "peak" label
    #    for a summer-only query, which zeroed pct_runtime_peak for real
    #    seasonal tariffs. With no season present (nothing ran) this is moot.
    peak_min = 0.0
    applicable = [b for b in tou.bands if b.season in seasons_run]
    if applicable:
        peak_rate = max(b.rate for b in applicable)
        peak_names = {b.name for b in applicable if b.rate == peak_rate}
        peak_min = sum(res.by_band.get(n, {}).get("minutes", 0.0) for n in peak_names)
    res.pct_runtime_peak = (peak_min / running_min * 100) if running_min else 0.0
    return res
