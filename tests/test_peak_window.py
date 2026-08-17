"""Regression guard for issue #2: the on-peak window must come from the TOU
table, not a hardcoded weekday 17:00-21:00. These use a MIDDAY (12:00-16:00)
peak — a shape the old hardcode got exactly backwards (it treated the evening
as peak and the real midday peak as off-peak)."""
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo

from house_climate.config import TouBand, TouTable
from house_climate.analytics import correlation, precool

TZ = "America/Los_Angeles"


def _midday_peak_tou():
    # Weekday peak 12:00-16:00 (e.g. a solar-duck / midday-peak utility); the
    # weekday off band wraps 16:00->12:00 to cover the rest of the day.
    bands = (
        TouBand("peak", "summer", "weekday", time(12, 0), time(16, 0), 0.50),
        TouBand("off", "summer", "weekday", time(16, 0), time(12, 0), 0.10),
        TouBand("off", "summer", "weekend", time(0, 0), time(0, 0), 0.10),
    )
    return TouTable(frozenset(range(1, 13)), bands)


def _fri(h, m=0):   # 2026-08-14 is a Friday
    return datetime(2026, 8, 14, h, m, tzinfo=ZoneInfo(TZ))


def test_is_peak_follows_table_not_5pm_9pm():
    tou = _midday_peak_tou()
    assert tou.is_peak(_fri(14, 0)) is True          # real midday peak
    assert tou.is_peak(_fri(10, 0)) is False
    # The evening the OLD code hardcoded as peak (17<=h<21) is off-peak here:
    assert tou.is_peak(_fri(17, 30)) is False
    assert tou.is_peak(_fri(20, 0)) is False
    # Weekend has no peak band at all.
    assert tou.is_peak(datetime(2026, 8, 15, 14, 0, tzinfo=ZoneInfo(TZ))) is False


def test_peak_window_derived_from_table():
    assert _midday_peak_tou().peak_window(_fri(0)) == (time(12, 0), time(16, 0), True)


def test_flat_table_has_no_peak():
    flat = TouTable(frozenset(range(1, 13)),
                    (TouBand("flat", "summer", "all", time(0, 0), time(0, 0), 0.20),))
    assert flat.peak_rate("summer") is None
    assert flat.is_peak(_fri(14, 0)) is False
    assert flat.peak_window(_fri(0)) is None


def test_predict_peak_cost_prices_at_the_real_peak_window():
    tou = _midday_peak_tou()
    hist = [{"day_high": 70.0, "cool_minutes": 60.0, "peak_cool_minutes": 20.0},
            {"day_high": 85.0, "cool_minutes": 180.0, "peak_cool_minutes": 60.0},
            {"day_high": 100.0, "cool_minutes": 300.0, "peak_cool_minutes": 110.0}]
    out = correlation.predict_peak_cost(100.0, hist, tou, 3.0, TZ, target_date=date(2026, 8, 14))
    # The rate used is the midday peak (0.50), NOT the 17:30 off-peak (0.10) the
    # old hardcoded probe would have charged.
    assert out["peak_band"] == "peak"
    assert out["peak_rate_used"] == 0.50


def test_precool_effectiveness_uses_passed_window():
    # Build pre-cool vs normal days over an 11:45-16:00 window (peak 12-16).
    tz = ZoneInfo(TZ)

    def day(d, sp_before, sp_after, cool):
        rows = []
        t = datetime(d.year, d.month, d.day, 11, 45, tzinfo=tz)
        end = datetime(d.year, d.month, d.day, 16, 0, tzinfo=tz)
        while t < end:
            sp = sp_before if t.hour < 12 else sp_after
            in_peak = 12 <= t.hour < 16
            status = ("cooling" if cool else "idle") if in_peak else "idle"
            rows.append({"ts": t.astimezone(timezone.utc),
                         "cool_setpoint_f": sp, "equipment_status": status,
                         "daikin_outdoor_temp_f": 90.0, "indoor_temp_f": 75.0})
            t += timedelta(minutes=5)
        return rows

    rows = []
    rows += day(date(2026, 8, 3), 70, 78, cool=False)   # Mon pre-cool
    rows += day(date(2026, 8, 4), 70, 78, cool=False)   # Tue pre-cool
    rows += day(date(2026, 8, 5), 74, 74, cool=True)    # Wed normal
    rows += day(date(2026, 8, 6), 74, 74, cool=True)    # Thu normal
    res = precool.effectiveness(rows, TZ, peak_start=time(12, 0), peak_end=time(16, 0),
                                peak_weekday_only=True)
    assert res["ready"] is True
    assert res["precool"]["avg_peak_cool_min"] < res["normal"]["avg_peak_cool_min"]


def _two_humped_tou():
    # Same top rate (0.50) in a morning AND an evening window — a solar-duck
    # shape. The midday trough between them is off-peak (0.10).
    bands = (
        TouBand("peak", "summer", "weekday", time(6, 0), time(9, 0), 0.50),
        TouBand("peak", "summer", "weekday", time(17, 0), time(21, 0), 0.50),
        TouBand("off", "summer", "weekday", time(9, 0), time(17, 0), 0.10),
        TouBand("off", "summer", "weekday", time(21, 0), time(6, 0), 0.10),
        TouBand("off", "summer", "weekend", time(0, 0), time(0, 0), 0.10),
    )
    return TouTable(frozenset(range(1, 13)), bands)


def test_peak_windows_keeps_two_humps_separate():
    wins = _two_humped_tou().peak_windows(_fri(0))
    assert wins == [(time(6, 0), time(9, 0), True), (time(17, 0), time(21, 0), True)]


def test_two_humped_peak_priced_at_peak_not_midday_trough():
    # Regression for the min-start/max-end envelope bug: the old peak_window
    # returned (06:00, 21:00) and predict_peak_cost probed ~13:30 -> off-peak.
    tou = _two_humped_tou()
    hist = [{"day_high": 70.0, "cool_minutes": 60.0, "peak_cool_minutes": 20.0},
            {"day_high": 85.0, "cool_minutes": 180.0, "peak_cool_minutes": 60.0},
            {"day_high": 100.0, "cool_minutes": 300.0, "peak_cool_minutes": 110.0}]
    out = correlation.predict_peak_cost(100.0, hist, tou, 3.0, TZ, target_date=date(2026, 8, 14))
    assert out["peak_rate_used"] == 0.50          # NOT the 0.10 midday trough
    # window cap is the sum of both 3h+4h humps = 7h = 420 min, not the 15h envelope
    assert out["predicted_peak_cool_minutes"] <= 420.0
