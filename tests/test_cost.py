from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
import pytest
from house_climate.config import load_config, TouBand, TouTable
from house_climate.analytics import cost

from conftest import CFG_PATH

CFG = load_config(CFG_PATH)
TZ = "America/Los_Angeles"
PEAK_RATE = max(b.rate for b in CFG.tou.bands)   # config's on-peak rate, whatever it's named

def _at_local(hour, minute=0, status="cooling", day=10):
    # build a UTC ts whose LA-local time is hour:minute on 2026-08-<day> (a weekday for day=10)
    local = datetime(2026, 8, day, hour, minute, tzinfo=ZoneInfo(TZ))
    return {"ts": local.astimezone(timezone.utc), "equipment_status": status}

def _cool_at_local(hour, minute=0):
    return _at_local(hour, minute, "cooling")

def test_peak_vs_offpeak_split():
    # 2026-08-10 is a Monday. One tick at 17:00 (peak) then 17:03; one at 22:00 (offpeak) then 22:03
    rows = [_cool_at_local(17, 0), _cool_at_local(17, 3),
            _cool_at_local(22, 0), _cool_at_local(22, 3)]
    res = cost.compute(rows, CFG.tou, CFG.system_kw, TZ)
    assert res.by_band["peak"]["minutes"] > 0
    assert res.by_band["offpeak"]["minutes"] > 0
    # peak rate (0.4313) > offpeak (0.0893): equal minutes => peak dollars higher
    assert res.by_band["peak"]["dollars"] > res.by_band["offpeak"]["dollars"]

def test_boundary_2059_is_peak():
    # 2026-08-10 is a Monday; peak is 17:00-21:00 so 20:59 is peak, 21:00 is offpeak.
    rows = [_cool_at_local(20, 59), _cool_at_local(21, 0), _cool_at_local(21, 3)]
    res = cost.compute(rows, CFG.tou, CFG.system_kw, TZ)
    assert res.by_band["peak"]["minutes"] > 0          # the 20:59 tick
    assert res.by_band["offpeak"]["minutes"] > 0       # the 21:00 tick now has duration

def test_heating_priced_at_heat_kw_not_system_kw():
    # Two heating ticks 10 minutes apart at 10:00 and 10:10 (midpeak, weekday).
    rows = [_at_local(10, 0, "heating"), _at_local(10, 10, "heating")]
    res = cost.compute(rows, CFG.tou, system_kw=2.5, tz=TZ, heat_kw=0.5)
    band = res.by_band["midpeak"]
    mid_rate = next(b.rate for b in CFG.tou.bands if b.name == "midpeak")
    # 10 minutes at 0.5 kW = (10/60)*0.5 kWh, priced at the midpeak rate
    expected_dollars = (10 / 60.0) * 0.5 * mid_rate
    assert band["dollars"] == pytest.approx(expected_dollars, rel=1e-6)
    # Much less than if it had been priced at system_kw (2.5 kW).
    wrong_dollars = (10 / 60.0) * 2.5 * mid_rate
    assert band["dollars"] < wrong_dollars


def test_interval_priced_at_midpoint_band():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Los_Angeles")
    # One 3-min cooling interval straddling the 17:00 peak edge: starts
    # 16:58:30, ends 17:01:30. Midpoint = 17:00:00 -> priced as PEAK.
    # (Start-attribution billed it entirely mid-peak.)
    rows = [
        {"ts": datetime(2026, 8, 10, 16, 58, 30, tzinfo=tz).astimezone(timezone.utc),
         "equipment_status": "cooling"},
        {"ts": datetime(2026, 8, 10, 17, 1, 30, tzinfo=tz).astimezone(timezone.utc),
         "equipment_status": "idle"},
    ]
    res = cost.compute(rows, CFG.tou, CFG.system_kw, "America/Los_Angeles")
    assert "peak" in res.by_band
    assert res.by_band["peak"]["minutes"] == 3.0


# --- Absolute-dollar oracles for the COOLING path (the dominant real cost).
# Prior tests only asserted inequalities/minutes; a bug that dropped the /60 or
# mispriced system_kw for cooling would keep every inequality true and ship.

def test_cooling_dollars_pinned_to_hand_computed_value():
    # One 3-min cooling interval fully inside peak (18:00->18:03, midpoint
    # 18:01:30 is peak on a weekday). dollars = (3/60)h * system_kw * peak_rate.
    rows = [_cool_at_local(18, 0), _at_local(18, 3, "idle")]
    res = cost.compute(rows, CFG.tou, CFG.system_kw, TZ)
    expected = (3 / 60.0) * CFG.system_kw * PEAK_RATE
    assert res.by_band["peak"]["dollars"] == pytest.approx(expected, rel=1e-9)
    assert res.by_band["peak"]["kwh"] == pytest.approx((3 / 60.0) * CFG.system_kw, rel=1e-9)
    assert res.total_dollars == pytest.approx(expected, rel=1e-9)


def test_total_dollars_equals_sum_of_band_dollars():
    rows = [_cool_at_local(17, 0), _cool_at_local(17, 3),
            _cool_at_local(13, 0), _cool_at_local(13, 3),
            _cool_at_local(22, 0), _cool_at_local(22, 3)]
    res = cost.compute(rows, CFG.tou, CFG.system_kw, TZ)
    assert res.total_dollars == pytest.approx(
        sum(b["dollars"] for b in res.by_band.values()), rel=1e-12)
    assert res.total_kwh == pytest.approx(
        sum(b["kwh"] for b in res.by_band.values()), rel=1e-12)


def test_max_gap_s_caps_billed_minutes():
    # Two cooling readings 3 hours apart. Without the gap cap the interval would
    # bill 180 min; the 600s (10 min) cap must hold it to exactly 10 minutes.
    rows = [_cool_at_local(13, 0), _at_local(16, 0, "idle")]
    res = cost.compute(rows, CFG.tou, CFG.system_kw, TZ, max_gap_s=600)
    assert res.by_band["midpeak"]["minutes"] == pytest.approx(10.0, rel=1e-9)


def test_pct_runtime_peak_uses_highest_rate_band_regardless_of_name():
    # A utility whose peak band is NOT literally named "peak" must still get a
    # correct pct_runtime_peak (the old code hardcoded the name and returned 0).
    tou = TouTable(frozenset({6, 7, 8, 9}), (
        TouBand("on-peak", "summer", "all", time(17, 0), time(21, 0), 0.50),
        TouBand("base", "summer", "all", time(21, 0), time(17, 0), 0.10),
    ))
    # 3 min at 18:00 (on-peak) + 3 min at 12:00 (base) => half the runtime peak.
    rows = [{"ts": datetime(2026, 8, 10, 18, 0, tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc),
             "equipment_status": "cooling"},
            {"ts": datetime(2026, 8, 10, 18, 3, tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc),
             "equipment_status": "idle"},
            {"ts": datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc),
             "equipment_status": "cooling"},
            {"ts": datetime(2026, 8, 10, 12, 3, tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc),
             "equipment_status": "idle"}]
    res = cost.compute(rows, tou, 3.0, TZ)
    assert res.pct_runtime_peak == pytest.approx(50.0, rel=1e-9)


def test_pct_runtime_peak_is_zero_when_all_offpeak():
    # REGRESSION (fable): running everything off-peak must report 0% peak
    # runtime, not 100%. The old generic code picked the max rate among only
    # the bands that RAN, so off-peak-only -> "offpeak" became the "peak" band.
    # Real config path (peak/midpeak/offpeak), real off-peak hours (22:00/23:00).
    rows = [_cool_at_local(22, 0), _cool_at_local(22, 3),
            _cool_at_local(23, 0), _cool_at_local(23, 3)]
    res = cost.compute(rows, CFG.tou, CFG.system_kw, TZ)
    assert res.by_band["offpeak"]["minutes"] > 0     # it did run, entirely off-peak
    assert "peak" not in res.by_band                 # the peak band never ran
    assert res.pct_runtime_peak == 0.0               # so 0% of runtime was peak


def test_pct_runtime_peak_partial_on_real_config():
    # Mixed: one 3-min cooling run at peak (18:00) + one 3-min run off-peak
    # (23:00) on the real config -> 50% peak runtime. idle terminators so the
    # long idle gap between them isn't billed.
    rows = [_cool_at_local(18, 0), _at_local(18, 3, "idle"),
            _cool_at_local(23, 0), _at_local(23, 3, "idle")]
    res = cost.compute(rows, CFG.tou, CFG.system_kw, TZ)
    assert res.pct_runtime_peak == pytest.approx(50.0, rel=1e-9)
