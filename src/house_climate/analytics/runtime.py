from dataclasses import dataclass, field
from datetime import timedelta

_COOL = {"cooling", "overcool"}
_HEAT = {"heating"}
_FAN = {"fan"}


def _bucket(status: str) -> str:
    if status in _COOL: return "cool"
    if status in _HEAT: return "heat"
    if status in _FAN: return "fan"
    return "idle"


@dataclass
class Cycle:
    status: str
    start: object
    end: object
    minutes: float
    setpoint_induced: bool = False


@dataclass
class RuntimeResult:
    minutes: dict = field(default_factory=dict)
    cycles: list = field(default_factory=list)
    short_cycles: int = 0
    # Short cool/heat cycles that coincide with a setpoint change (pre-cool,
    # schedule, or manual). Reported separately, NOT counted as faults.
    short_cycles_setpoint_induced: int = 0


def _setpoint_change_times(rows, key):
    """Timestamps where the given setpoint value moved between consecutive
    non-null readings. Missing/None values are skipped (never a change), so it
    is safe on rows that lack setpoint columns."""
    times = []
    prev = None
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        if prev is not None and abs(v - prev) > 1e-9:
            times.append(r["ts"])
        prev = v
    return times


def compute(readings, *, max_gap_s=600, short_cycle_min=10, setpoint_grace_s=360) -> RuntimeResult:
    res = RuntimeResult(minutes={"cool": 0.0, "heat": 0.0, "fan": 0.0, "idle": 0.0})
    if not readings:
        return res
    rows = sorted(readings, key=lambda r: r["ts"])

    # A short cycle we asked for isn't a fault. Pre-cool, the schedule, and
    # manual edits all move the setpoint, which starts (or cuts short) a cycle;
    # a short cool/heat cycle whose start or end sits within setpoint_grace_s of
    # such a change is attributed to that change, not to the equipment.
    cool_changes = _setpoint_change_times(rows, "cool_setpoint_f")
    heat_changes = _setpoint_change_times(rows, "heat_setpoint_f")
    grace = timedelta(seconds=setpoint_grace_s)

    cur = None
    for i, row in enumerate(rows):
        b = _bucket(row["equipment_status"])
        raw_dt = ((rows[i + 1]["ts"] - row["ts"]).total_seconds()
                  if i + 1 < len(rows) else 0)
        mins = min(raw_dt, max_gap_s) / 60.0
        res.minutes[b] += mins
        # A gap longer than max_gap_s is unobserved time. The MINUTES keep
        # their clamped credit (runtime doctrine), but the CYCLE gets none
        # of it and breaks at the gap: two runs separated by a dead poller
        # are two cycles, and unobserved credit must not stretch a short
        # cycle past the fault threshold — either way short-cycling would
        # hide during exactly the flaky stretches it matters in.
        gap_follows = raw_dt > max_gap_s
        cyc_mins = 0.0 if gap_follows else mins
        if cur and cur.status == b:
            cur.end = row["ts"]; cur.minutes += cyc_mins
        else:
            if cur and cur.status in ("cool", "heat"):
                res.cycles.append(cur)
            cur = Cycle(b, row["ts"], row["ts"], cyc_mins)
        if gap_follows:
            if cur.status in ("cool", "heat"):
                res.cycles.append(cur)
            cur = None
    if cur and cur.status in ("cool", "heat"):
        res.cycles.append(cur)

    for c in res.cycles:
        changes = cool_changes if c.status == "cool" else heat_changes
        c.setpoint_induced = any(
            c.start - grace <= t <= c.end + grace for t in changes)

    # A cycle whose observed duration is 0 is degenerate: a single isolated
    # cool/heat sample (last row in the window, or one immediately before a
    # poller gap, both of which get cyc_mins=0). Its true length is unknown,
    # not "under the threshold", so it must not be scored as a short-cycle
    # fault -- one stray sample would otherwise trip a short-cycling alert.
    short = [c for c in res.cycles
             if c.status in ("cool", "heat") and 0 < c.minutes < short_cycle_min]
    res.short_cycles = sum(1 for c in short if not c.setpoint_induced)
    res.short_cycles_setpoint_induced = sum(1 for c in short if c.setpoint_induced)
    return res
