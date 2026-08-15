from datetime import datetime, timezone, timedelta
from house_climate.analytics import runtime


def _r(minute, status):
    return {"ts": datetime(2026, 8, 10, 12, minute, tzinfo=timezone.utc),
            "equipment_status": status}


def test_runtime_minutes_and_cycles():
    rows = [_r(0, "cooling"), _r(3, "cooling"), _r(6, "cooling"),
            _r(9, "idle"), _r(12, "cooling"), _r(15, "idle")]
    res = runtime.compute(rows, short_cycle_min=10)
    # first cool run spans 0->9 = 9 min; second 12->15 = 3 min
    assert round(res.minutes["cool"]) == 12
    assert len(res.cycles) == 2
    assert res.short_cycles == 2       # both cool cycles < 10 min


def test_gap_is_clamped():
    rows = [_r(0, "cooling"),
            {"ts": datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc),
             "equipment_status": "cooling"}]
    res = runtime.compute(rows, max_gap_s=600)
    assert round(res.minutes["cool"]) == 10   # 2h gap clamped to 10 min


def _rs(minute, status, cool_sp):
    return {"ts": datetime(2026, 8, 10, 12, minute, tzinfo=timezone.utc),
            "equipment_status": status, "cool_setpoint_f": cool_sp}


def test_setpoint_induced_short_cycle_excluded():
    # Setpoint drops 74->70 at minute 3, kicking on a brief cool cycle -- we
    # asked for it, so it must not count as an equipment short-cycle fault.
    rows = [_rs(0, "idle", 74), _rs(3, "cooling", 70), _rs(6, "cooling", 70), _rs(9, "idle", 70)]
    res = runtime.compute(rows, short_cycle_min=10)
    assert res.short_cycles == 0
    assert res.short_cycles_setpoint_induced == 1


def test_short_cycle_without_setpoint_change_counts():
    # Identical brief cool cycle, but the setpoint never moves -> a real fault.
    rows = [_rs(0, "idle", 74), _rs(3, "cooling", 74), _rs(6, "cooling", 74), _rs(9, "idle", 74)]
    res = runtime.compute(rows, short_cycle_min=10)
    assert res.short_cycles == 1
    assert res.short_cycles_setpoint_induced == 0


def test_cycle_breaks_across_poller_gap():
    # Two distinct cool runs separated by a 2h poller outage, with "cooling"
    # observed on both sides of the gap: these are TWO cycles, not one bridged
    # 22-minute cycle — merging them hides short cycles from the health check.
    rows = ([_r(m, "cooling") for m in (0, 3, 6)]
            + [{"ts": datetime(2026, 8, 10, 14, m, tzinfo=timezone.utc),
                "equipment_status": "cooling"} for m in (0, 3)]
            + [{"ts": datetime(2026, 8, 10, 14, 6, tzinfo=timezone.utc),
                "equipment_status": "idle"}])
    res = runtime.compute(rows, max_gap_s=600, short_cycle_min=10)
    cool_cycles = [c for c in res.cycles if c.status == "cool"]
    assert len(cool_cycles) == 2
    assert res.short_cycles == 2   # both runs are under 10 minutes
