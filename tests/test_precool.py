import datetime as dt
import pytest
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from house_climate.analytics import precool

TZ = "America/Los_Angeles"


def _day_rows(date, sp_before, sp_after, cool_in_peak, outdoor=85.0):
    """One weekday of 5-min readings from 16:45 to 21:00 local. `cool_in_peak`
    makes the AC cool through the whole peak window (or coast idle)."""
    tz = ZoneInfo(TZ)
    rows = []
    t = datetime(date.year, date.month, date.day, 16, 45, tzinfo=tz)
    end = datetime(date.year, date.month, date.day, 21, 0, tzinfo=tz)
    while t < end:
        sp = sp_before if t.hour < 17 else sp_after
        in_peak = 17 <= t.hour < 21
        status = ("cooling" if cool_in_peak else "idle") if in_peak else "idle"
        rows.append({"ts": t.astimezone(timezone.utc), "cool_setpoint_f": sp,
                     "equipment_status": status, "daikin_outdoor_temp_f": outdoor,
                     "indoor_temp_f": 75.0})
        t += timedelta(minutes=5)
    return rows


def test_effectiveness_precool_vs_normal():
    rows = []
    rows += _day_rows(dt.date(2026, 8, 3), 70, 78, cool_in_peak=False)   # Mon, pre-cool
    rows += _day_rows(dt.date(2026, 8, 4), 70, 78, cool_in_peak=False)   # Tue, pre-cool
    rows += _day_rows(dt.date(2026, 8, 5), 74, 74, cool_in_peak=True)    # Wed, normal
    rows += _day_rows(dt.date(2026, 8, 6), 74, 74, cool_in_peak=True)    # Thu, normal
    res = precool.effectiveness(rows, TZ)
    assert res["ready"] is True
    assert res["precool"]["days"] == 2 and res["normal"]["days"] == 2
    assert res["precool"]["avg_peak_cool_min"] < res["normal"]["avg_peak_cool_min"]
    assert res["peak_min_saved_per_day"] >= 0


def test_effectiveness_collecting_without_control_days():
    rows = (_day_rows(dt.date(2026, 8, 3), 70, 78, cool_in_peak=False)
            + _day_rows(dt.date(2026, 8, 4), 70, 78, cool_in_peak=False))
    res = precool.effectiveness(rows, TZ)
    assert res["ready"] is False        # no normal (pre-cool-off) days to compare
    assert res["normal_days"] == 0


def test_effectiveness_skips_weekends():
    # Sat + Sun only -> the example TOU peak is weekday-only, so nothing to evaluate.
    rows = (_day_rows(dt.date(2026, 8, 1), 74, 74, cool_in_peak=True)   # Sat
            + _day_rows(dt.date(2026, 8, 2), 74, 74, cool_in_peak=True))  # Sun
    res = precool.effectiveness(rows, TZ)
    assert res["ready"] is False


def test_unclassifiable_day_is_skipped_not_normal():
    """A day whose setpoint can't be observed around 17:00 (poller gap) must
    be EXCLUDED — defaulting it into 'normal' poisoned the control group."""
    rows = []
    rows += _day_rows(dt.date(2026, 8, 3), 70, 78, cool_in_peak=False)   # Mon, pre-cool
    rows += _day_rows(dt.date(2026, 8, 4), 70, 78, cool_in_peak=False)   # Tue, pre-cool
    # Wed: genuine pre-cool day but the 16:45-17:15 window has no readings
    wed = _day_rows(dt.date(2026, 8, 5), 70, 78, cool_in_peak=False)
    tz = ZoneInfo(TZ)
    wed = [r for r in wed
           if not (16 <= r["ts"].astimezone(tz).hour < 17
                   or (r["ts"].astimezone(tz).hour == 17
                       and r["ts"].astimezone(tz).minute <= 15))]
    rows += wed
    rows += _day_rows(dt.date(2026, 8, 6), 74, 74, cool_in_peak=True)    # Thu, normal
    rows += _day_rows(dt.date(2026, 8, 7), 74, 74, cool_in_peak=True)    # Fri, normal
    res = precool.effectiveness(rows, TZ)
    assert res["ready"] is True
    assert res["precool"]["days"] == 2   # Wed did NOT land in either group
    assert res["normal"]["days"] == 2


def test_avg_out_divides_by_observed_minutes_only():
    """Outdoor-temp average must use only the minutes that HAD a reading —
    dividing by all peak minutes dragged an 85F day toward 42F during
    weather-feed outages."""
    rows = _day_rows(dt.date(2026, 8, 3), 74, 74, cool_in_peak=True, outdoor=85.0)
    rows2 = _day_rows(dt.date(2026, 8, 4), 70, 78, cool_in_peak=False, outdoor=85.0)
    # blank the outdoor reading on half of day 1's peak rows
    tz = ZoneInfo(TZ)
    for i, r in enumerate(rows):
        if 17 <= r["ts"].astimezone(tz).hour < 21 and i % 2 == 0:
            r["daikin_outdoor_temp_f"] = None
    res = precool.effectiveness(rows + rows2, TZ, min_days_each=1)
    assert res["ready"] is True
    assert abs(res["normal"]["avg_peak_out_f"] - 85.0) < 0.5   # not ~42


def _drop(rows, start_hm, end_hm):
    """Remove local [start, end) (h, m) readings, simulating a poller gap."""
    tz = ZoneInfo(TZ)

    def hm(r):
        t = r["ts"].astimezone(tz)
        return (t.hour, t.minute)
    return [r for r in rows if not (start_hm <= hm(r) < end_hm)]


def test_a_barely_observed_day_is_not_averaged_with_full_days():
    """A normal day observed for only the first hour of the four-hour peak
    (17:00-18:00, then the poller died) used to clear the 60-minute floor and
    enter the 'normal' average with a quarter of the cooling of a full day,
    shrinking the savings figure. Most of the window must be observed."""
    rows = []
    rows += _day_rows(dt.date(2026, 8, 3), 70, 78, cool_in_peak=False)   # Mon, pre-cool
    rows += _day_rows(dt.date(2026, 8, 4), 70, 78, cool_in_peak=False)   # Tue, pre-cool
    rows += _day_rows(dt.date(2026, 8, 5), 74, 74, cool_in_peak=True)    # Wed, normal
    rows += _day_rows(dt.date(2026, 8, 6), 74, 74, cool_in_peak=True)    # Thu, normal
    fri = _drop(_day_rows(dt.date(2026, 8, 7), 74, 74, cool_in_peak=True), (18, 5), (21, 0))
    rows += fri
    res = precool.effectiveness(rows, TZ)
    assert res["ready"] is True
    assert res["normal"]["days"] == 2                  # Friday left out
    assert res["normal"]["avg_peak_cool_min"] == pytest.approx(235.0)


def test_a_gap_is_credited_at_most_ten_minutes():
    """Across a 25-minute hole the code credited all 25 minutes of cooling
    (anything up to 30); cost and runtime credit at most 10."""
    rows = []
    rows += _day_rows(dt.date(2026, 8, 3), 70, 78, cool_in_peak=False)
    rows += _day_rows(dt.date(2026, 8, 4), 70, 78, cool_in_peak=False)
    normal = []
    for d in (dt.date(2026, 8, 5), dt.date(2026, 8, 6)):
        # readings 18:05, 18:10, 18:15, 18:20 missing: 18:00 -> 18:25 is 25 min
        normal += _drop(_day_rows(d, 74, 74, cool_in_peak=True), (18, 5), (18, 25))
    res = precool.effectiveness(rows + normal, TZ)
    assert res["ready"] is True
    assert res["normal"]["avg_peak_cool_min"] == pytest.approx(235.0 - 15.0)


def test_tou_holidays_are_skipped_like_weekends():
    """A holiday has no weekday peak, so its evening says nothing about
    pre-cooling. Monday 7 September 2026 (Labor Day) must not be counted."""
    labor_day = dt.date(2026, 9, 7)
    rows = []
    rows += _day_rows(dt.date(2026, 9, 8), 70, 78, cool_in_peak=False)   # Tue, pre-cool
    rows += _day_rows(dt.date(2026, 9, 9), 70, 78, cool_in_peak=False)   # Wed, pre-cool
    rows += _day_rows(dt.date(2026, 9, 10), 74, 74, cool_in_peak=True)  # Thu, normal
    rows += _day_rows(labor_day, 74, 74, cool_in_peak=True)             # holiday
    res = precool.effectiveness(rows, TZ, min_days_each=1)
    assert res["normal"]["days"] == 2                                   # without the holiday rule
    res = precool.effectiveness(rows, TZ, min_days_each=1,
                                off_days=lambda d: d == labor_day)
    assert res["normal"]["days"] == 1
