from dataclasses import dataclass, field
from datetime import timedelta
from zoneinfo import ZoneInfo

_RUNNING = {"cooling", "overcool", "heating"}


@dataclass
class CostResult:
    by_band: dict = field(default_factory=dict)
    total_dollars: float = 0.0
    total_kwh: float = 0.0
    pct_runtime_peak: float = 0.0


def compute(readings, tou, system_kw, tz, *, max_gap_s=600, heat_kw=None) -> CostResult:
    zone = ZoneInfo(tz)
    res = CostResult()
    rows = sorted(readings, key=lambda r: r["ts"])
    running_min = 0.0
    seasons_run = set()   # seasons in which cooling/heating actually ran
    for i, row in enumerate(rows):
        status = row["equipment_status"]
        if status not in _RUNNING:
            continue
        seasons_run.add(tou.season(row["ts"].astimezone(zone).month))
        dt = (rows[i + 1]["ts"] - row["ts"]).total_seconds() if i + 1 < len(rows) else 0
        mins = min(dt, max_gap_s) / 60.0
        if mins <= 0:
            continue
        # Price each interval at its MIDPOINT's band, not its start's: start
        # attribution systematically misprices the interval straddling every
        # band boundary (16:58->17:01 billed entirely mid-peak, 20:58->21:01
        # entirely peak). Midpoint makes the expected boundary error ~zero.
        local = (row["ts"] + timedelta(minutes=mins / 2)).astimezone(zone)
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
