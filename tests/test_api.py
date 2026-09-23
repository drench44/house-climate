import calendar
import dataclasses
import math
import pytest
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from house_climate import db
from house_climate.web import api
from house_climate.analytics import humidity
from house_climate.config import load_config

from conftest import CFG_PATH

CFG = load_config(CFG_PATH)
TZ = ZoneInfo(CFG.timezone)

# Crawl/moisture tests run against a known ecowitt shape, independent of the
# deployment config (whose sensors may be disabled or named differently).
CRAWL_CFG = dataclasses.replace(CFG, ecowitt={
    "enabled": True, "gateway_url": "http://gw",
    "channels": {"8": "Upstairs", "7": "Downstairs"},
    "outdoor_name": "Crawl Space"})


# --- _day_is_complete: pure, DB-free. A day anchors the cost average / forecast
# fit only if nearly all of it, midnight to midnight, was actually observed.
# (Runs locally, no Postgres needed.)

from datetime import date as _date

DAY = _date(2026, 8, 10)          # a Monday


def _poll_rows(day=DAY, every_min=3, skip=None, status=lambda local: "idle",
               start_day_offset=-1, end_day_offset=2):
    """Readings every `every_min` minutes from the day before `day` to the day
    after it, like the real poller, minus any local [a, b) spans in `skip`."""
    rows = []
    t = datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(days=start_day_offset)
    end = datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(days=end_day_offset)
    while t < end:
        if not any(a <= t < b for a, b in (skip or [])):
            rows.append({"ts": t.astimezone(timezone.utc),
                         "equipment_status": status(t)})
        t += timedelta(minutes=every_min)
    return rows


def _local(h, m=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def test_day_is_complete_fully_polled_day():
    assert api._day_is_complete(_poll_rows(), DAY, TZ) is True


def test_day_is_complete_tolerates_a_few_missed_polls():
    # A 12-minute hole: 10 of it credited, 2 unobserved. Not an outage.
    rows = _poll_rows(skip=[(_local(9, 1), _local(9, 12))])
    assert api._day_is_complete(rows, DAY, TZ) is True


def test_day_is_complete_rejects_an_outage_that_could_hide_runtime():
    """The audit case: a 2.5-hour outage across the afternoon peak used to pass
    (no gap over 3 hours, spans 2am to 10pm) and priced the day at about 60%
    of its real cost."""
    rows = _poll_rows(skip=[(_local(16, 31), _local(19, 0))])
    assert api._day_is_complete(rows, DAY, TZ) is False


def test_day_is_complete_rejects_sparse_readings():
    # Two-hourly readings span the day but vouch for only 10 minutes of each
    # two hours; the old rule called this complete.
    rows = _poll_rows(every_min=120)
    assert api._day_is_complete(rows, DAY, TZ) is False


def test_day_is_complete_rejects_a_late_start():
    # First reading of the day at 01:30: the first hour and a half is unseen.
    rows = [r for r in _poll_rows(start_day_offset=0)
            if r["ts"] >= _local(1, 30).astimezone(timezone.utc)]
    assert api._day_is_complete(rows, DAY, TZ) is False


@pytest.mark.parametrize("day,hours", [(_date(2026, 3, 8), 23), (_date(2026, 11, 1), 25)])
def test_day_is_complete_on_clock_change_days(day, hours):
    """Subtracting two local midnights that share a tzinfo is wall-clock math
    in Python, so every day measured 24 hours: a perfect spring-forward day
    (23h) was always refused, and a fall-back day (25h) let a 70-minute
    outage through."""
    start, end = api._local_day_bounds(day, TZ)
    assert (end - start).total_seconds() == hours * 3600
    rows, t = [], start - timedelta(hours=1)
    while t < end + timedelta(hours=1):
        rows.append({"ts": t, "equipment_status": "idle"})
        t += timedelta(minutes=3)
    assert api._day_is_complete(rows, day, TZ) is True
    hole = [r for r in rows
            if not (start + timedelta(hours=14) <= r["ts"] < start + timedelta(hours=15, minutes=10))]
    assert api._day_is_complete(hole, day, TZ) is False


def test_day_is_complete_empty_is_false():
    assert api._day_is_complete([], DAY, TZ) is False


def test_undercounted_outage_day_cost_is_what_the_old_rule_let_through():
    """The number the fix keeps out of avg_per_day: with the outage, the day
    prices well under the same day fully observed."""
    busy = lambda t: "cooling" if 14 <= t.hour < 21 else "idle"
    full = _poll_rows(status=busy)
    gappy = _poll_rows(status=busy, skip=[(_local(16, 31), _local(19, 0))])
    start, end = api._local_day_bounds(DAY, TZ)
    c_full = api.cost.compute(full, CFG.tou, CFG.system_kw, CFG.timezone, start=start, end=end)
    c_gap = api.cost.compute(gappy, CFG.tou, CFG.system_kw, CFG.timezone, start=start, end=end)
    assert c_gap.total_dollars < 0.75 * c_full.total_dollars
    assert api._day_is_complete(gappy, DAY, TZ) is False
    assert api._day_is_complete(full, DAY, TZ) is True


# --- day slicing across midnight (pure). Every interval belongs to exactly one
# day, by its midpoint, the rule cost.compute prices bands by.

def test_a_day_of_continuous_cooling_is_a_full_day_of_minutes():
    """Slicing rows by timestamp first gave each day's last reading zero
    minutes: a day of nonstop cooling came to 1437 minutes, not 1440."""
    rows = _poll_rows(status=lambda t: "cooling")
    start, end = api._local_day_bounds(DAY, TZ)
    assert api.runtime.status_minutes(rows, api.runtime.COOL_STATUSES,
                                      start=start, end=end) == pytest.approx(1440)
    res = api.cost.compute(rows, CFG.tou, CFG.system_kw, CFG.timezone, start=start, end=end)
    assert sum(b["minutes"] for b in res.by_band.values()) == pytest.approx(1440)


def test_day_slices_partition_the_total():
    """Days priced one at a time add up to the same span priced at once, with
    polls that do not line up with midnight."""
    rows = _poll_rows(every_min=7, status=lambda t: "cooling" if t.minute % 2 else "idle")
    d0, d1 = DAY, DAY + timedelta(days=1)
    s0, e0 = api._local_day_bounds(d0, TZ)
    s1, e1 = api._local_day_bounds(d1, TZ)
    whole = api.cost.compute(rows, CFG.tou, CFG.system_kw, CFG.timezone, start=s0, end=e1)
    parts = [api.cost.compute(rows, CFG.tou, CFG.system_kw, CFG.timezone, start=a, end=b)
             for a, b in ((s0, e0), (s1, e1))]
    assert sum(p.total_dollars for p in parts) == pytest.approx(whole.total_dollars)
    assert sum(p.total_kwh for p in parts) == pytest.approx(whole.total_kwh)


def test_peak_minutes_use_the_midpoint_like_the_bill():
    """An interval 16:58:30 -> 17:01:30 is billed as peak (midpoint 17:00). The
    forecast's peak minutes classified it by its start and called it
    mid-peak, and dropped the last peak interval of the window outright."""
    rows = [{"ts": datetime(2026, 8, 10, 16, 58, 30, tzinfo=TZ).astimezone(timezone.utc),
             "equipment_status": "cooling"},
            {"ts": datetime(2026, 8, 10, 17, 1, 30, tzinfo=TZ).astimezone(timezone.utc),
             "equipment_status": "idle"}]
    peak = api.runtime.status_minutes(
        rows, api.runtime.COOL_STATUSES,
        include=lambda m: CFG.tou.is_peak(m.astimezone(TZ)))
    assert peak == pytest.approx(3.0)


# --- _extremes / _coverage: pure outdoor-history helpers, DB-free. ---

def _wxrow(minute, **fields):
    return {"ts": datetime(2026, 8, 12, 0, minute, tzinfo=timezone.utc), **fields}


def test_extremes_returns_high_and_low_with_timestamps():
    rows = [_wxrow(0, t=60.0), _wxrow(3, t=72.0), _wxrow(6, t=55.0)]
    ex = api._extremes(rows, "t")
    assert ex["high"]["v"] == 72.0
    assert ex["high"]["ts"] == "2026-08-12T00:03:00+00:00"
    assert ex["low"]["v"] == 55.0
    assert ex["low"]["ts"] == "2026-08-12T00:06:00+00:00"


def test_extremes_ignores_rows_missing_the_field():
    rows = [_wxrow(0, t=None), _wxrow(3, t=64.0), _wxrow(6)]
    ex = api._extremes(rows, "t")
    assert ex["high"]["v"] == 64.0 and ex["low"]["v"] == 64.0


def test_extremes_none_when_no_row_has_the_field():
    assert api._extremes([_wxrow(0), _wxrow(3, t=None)], "t") is None


def test_coverage_full_window_is_one():
    assert api._coverage(24, 24) == 1.0


def test_coverage_partial_window_is_fraction():
    assert api._coverage(6, 24) == 0.25


def test_coverage_clamps_above_one():
    # More observed buckets than nominal window hours (DST/rounding) -> capped.
    assert api._coverage(25, 24) == 1.0


def test_coverage_zero_window_is_zero():
    assert api._coverage(0, 0) == 0.0


def _seed(conn):
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(20):
        db.insert_reading(conn, dict(
            ts=base + timedelta(minutes=3*i), device_id="dev1",
            indoor_temp_f=72+i*0.1, indoor_humidity=48, heat_setpoint_f=68,
            cool_setpoint_f=72, equipment_status="cooling" if i % 2 else "idle",
            mode="cool", daikin_outdoor_temp_f=90, daikin_outdoor_humidity=30,
            wx_outdoor_temp_f=90, wx_humidity=30, wx_dewpoint_f=56, wx_solar_wm2=800,
            wx_uv=7, wx_fc_high_f=94, wx_fc_low_f=58, wx_conditions="Clear",
            wx_aqi=37, wx_alert_count=0, weather_ok=True))


def test_now_returns_latest(conn):
    _seed(conn)
    now = api.build_now(conn, "dev1")
    assert now["indoor_temp_f"] is not None
    assert "equipment_status" in now


def test_now_stale_when_no_readings(conn):
    now = api.build_now(conn, "dev1")
    assert now == {"stale": True}


def test_history_filters_by_range(conn):
    _seed(conn)
    hist = api.build_history(conn, "dev1", "24h")
    assert len(hist) == 20
    assert hist[0]["outdoor_temp_f"] == 90


# --- build_outdoor: outdoor conditions history for decision-making ---

def _seed_outdoor(conn, n=24, step_min=60, temp=lambda i: 60.0 + i,
                  rh=lambda i: 90.0 - i, dp=lambda i: 55.0 + i * 0.2,
                  base=None):
    """Insert `n` readings spaced `step_min` apart carrying outdoor weather.
    The lambdas let a test shape the outdoor curve to hit specific extremes."""
    base = base or (datetime.now(timezone.utc) - timedelta(minutes=step_min * (n - 1)))
    for i in range(n):
        db.insert_reading(conn, dict(
            ts=base + timedelta(minutes=step_min * i), device_id="dev1",
            indoor_temp_f=72, indoor_humidity=48, heat_setpoint_f=68,
            cool_setpoint_f=72, equipment_status="idle", mode="cool",
            daikin_outdoor_temp_f=None, daikin_outdoor_humidity=None,
            wx_outdoor_temp_f=temp(i), wx_humidity=rh(i), wx_dewpoint_f=dp(i),
            wx_solar_wm2=400, wx_uv=4, wx_fc_high_f=80, wx_fc_low_f=55,
            wx_conditions="Overcast", wx_aqi=31, wx_alert_count=0, weather_ok=True))


def test_outdoor_unavailable_when_empty(conn):
    out = api.build_outdoor(conn, "dev1", "24h")
    assert out["available"] is False
    assert out["reason"] == "no_data"


def test_outdoor_now_reports_latest_conditions(conn):
    _seed_outdoor(conn, n=24)
    out = api.build_outdoor(conn, "dev1", "24h")
    assert out["available"] is True
    assert out["range"] == "24h"
    assert "data_start" in out
    now = out["now"]
    assert now["temp_f"] == 83.0        # temp(23) = 60 + 23
    assert now["rh"] == 67.0            # rh(23)  = 90 - 23
    assert now["dew_f"] == 59.6         # dp(23) = 55 + 23*0.2
    assert now["conditions"] == "Overcast"
    assert now["aqi"] == 31 and now["aqi_source"] == "weather"
    assert now["weather_ok"] is True and now["stale"] is False


def test_outdoor_extremes_span_the_window(conn):
    _seed_outdoor(conn, n=24)
    out = api.build_outdoor(conn, "dev1", "24h")
    assert out["temp"]["high"]["v"] == 83.0 and out["temp"]["low"]["v"] == 60.0
    assert out["rh"]["high"]["v"] == 90.0 and out["rh"]["low"]["v"] == 67.0
    assert out["dew"]["high"]["v"] == 59.6 and out["dew"]["low"]["v"] == 55.0
    assert "ts" in out["rh"]["high"]


def test_outdoor_series_carries_temp_rh_dp(conn):
    _seed_outdoor(conn, n=24)
    out = api.build_outdoor(conn, "dev1", "24h")
    assert len(out["series"]) >= 1
    pt = out["series"][0]
    assert set(pt) == {"ts", "temp_avg", "rh_avg", "dp_avg"}
    assert pt["temp_avg"] is not None


def test_outdoor_coverage_is_per_field_and_flags_a_gappy_window(conn):
    # Only 6 hourly readings inside a 24h window -> each field's coverage is
    # exactly 6/24, so a caller never mistakes a quarter-full window for whole.
    _seed_outdoor(conn, n=6, step_min=60)
    out = api.build_outdoor(conn, "dev1", "24h")
    assert out["coverage"] == {"temp": 0.25, "rh": 0.25, "dew": 0.25}


def test_outdoor_coverage_full_window_is_one(conn):
    _seed_outdoor(conn, n=24, step_min=60)  # 24 hourly buckets across 24h
    out = api.build_outdoor(conn, "dev1", "24h")
    assert out["coverage"]["rh"] == 1.0


def test_outdoor_coverage_isolates_a_sparse_field(conn):
    # RH every hour, temp only in the first reading: RH coverage stays high
    # while temp coverage collapses -- the full RH column can't mask the gap.
    _seed_outdoor(conn, n=6, step_min=60,
                  temp=lambda i: 60.0 if i == 0 else None,
                  rh=lambda i: 80.0, dp=lambda i: 55.0)
    out = api.build_outdoor(conn, "dev1", "24h")
    assert out["coverage"]["temp"] < out["coverage"]["rh"]
    assert out["coverage"]["temp"] == round(1 / 24, 3)


def test_outdoor_data_start_is_weather_scoped_not_device_age(conn):
    # Old device: indoor-only rows for 3 days, but the WEATHER feed only started
    # 6h ago inside the 24h window. data_start must track the FEED, not the
    # device -- otherwise a young feed reads as real gaps. This test fails on a
    # device-earliest data_start (would give now-72h) and passes on weather-
    # scoped, so it locks the fix, not just the contract.
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    for h in range(6, 73, 6):  # indoor-only rows, now-72h .. now-6h, NO weather
        db.insert_reading(conn, _reading_row(now - timedelta(hours=h),
                                             wx_outdoor_temp_f=None, wx_humidity=None,
                                             wx_dewpoint_f=None, weather_ok=False))
    _seed_outdoor(conn, n=6, step_min=60, base=now - timedelta(hours=6))
    out = api.build_outdoor(conn, "dev1", "24h", now=now)
    assert out["coverage"]["rh"] < 1.0
    ds = datetime.fromisoformat(out["data_start"])
    assert ds == now - timedelta(hours=6)          # feed start, not the 72h-old indoor row
    assert ds >= now - timedelta(hours=24)         # inside window -> young feed, not gaps


def test_outdoor_data_start_predates_window_when_gap_is_real(conn):
    # Mature feed: weather data from before the window, then a real outage; the
    # feed only returns for the last 6h -> data_start predates the window, so
    # the same low coverage reads as a real feed gap rather than youth.
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    db.insert_reading(conn, _reading_row(now - timedelta(hours=48)))  # weather-bearing
    _seed_outdoor(conn, n=6, step_min=60, base=now - timedelta(hours=6))
    out = api.build_outdoor(conn, "dev1", "24h", now=now)
    assert out["coverage"]["rh"] < 1.0
    ds = datetime.fromisoformat(out["data_start"])
    assert ds == now - timedelta(hours=48)         # exact earliest weather row -> real gap


def test_outdoor_series_granularity_coarsens_with_range(conn):
    # 12h of readings every 15 min. The chart bucket widens with range
    # (_OUTDOOR_BUCKETS_S 15m/1h/3h), so the same data yields progressively
    # fewer points -- proving the range->bucket wiring end to end.
    now = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)
    _seed_outdoor(conn, n=48, step_min=15, base=now - timedelta(hours=12))
    n24 = len(api.build_outdoor(conn, "dev1", "24h", now=now)["series"])   # 15m buckets
    n7 = len(api.build_outdoor(conn, "dev1", "7d", now=now)["series"])     # 1h buckets
    n30 = len(api.build_outdoor(conn, "dev1", "30d", now=now)["series"])   # 3h buckets
    assert n24 > n7 > n30
    assert n7 == 12 and n30 == 4   # 12h -> 12 hourly, 4 three-hourly buckets


def _reading_row(ts, **over):
    base = dict(ts=ts, device_id="dev1", indoor_temp_f=72, indoor_humidity=48,
                heat_setpoint_f=68, cool_setpoint_f=72, equipment_status="idle",
                mode="cool", daikin_outdoor_temp_f=None, daikin_outdoor_humidity=None,
                wx_outdoor_temp_f=60.0, wx_humidity=70.0, wx_dewpoint_f=50.0,
                wx_solar_wm2=400, wx_uv=4, wx_fc_high_f=80, wx_fc_low_f=55,
                wx_conditions="Overcast", wx_aqi=31, wx_alert_count=0, weather_ok=True)
    base.update(over)
    return base


def test_outdoor_hourly_feeds_attribution_ignoring_dp_null_hours(conn):
    # Regression lock for the widened WHERE: outdoor_hourly now surfaces hours
    # that report temp/RH but no dew point. Attribution must ignore those (pair
    # only dp-present hours), not crash or skew -- the one backward-compat risk
    # of widening the query, exercised through the real DB path end to end.
    from house_climate.analytics import moisture
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    since = now - timedelta(days=7)
    for i in range(120):                    # crawl tracks outdoor dew point
        ts = now - timedelta(hours=120 - i)
        odp = 50 + 8 * math.sin(i / 12)
        db.insert_reading(conn, dict(
            ts=ts, device_id="dev1", indoor_temp_f=72, indoor_humidity=48,
            heat_setpoint_f=68, cool_setpoint_f=72, equipment_status="idle",
            mode="cool", daikin_outdoor_temp_f=None, daikin_outdoor_humidity=None,
            wx_outdoor_temp_f=60.0, wx_humidity=70.0,
            wx_dewpoint_f=(None if i % 6 == 0 else odp),  # 20 dp-null hours
            wx_solar_wm2=400, wx_uv=4, wx_fc_high_f=80, wx_fc_low_f=55,
            wx_conditions="Overcast", wx_aqi=31, wx_alert_count=0, weather_ok=True))
        db.insert_sensor_reading(conn, "ecowitt_crawl", ts,
                                 temp_f=64.0, humidity=80.0,
                                 dewpoint_f=55 + 8 * math.sin(i / 12))
    outdoor = db.outdoor_hourly(conn, "dev1", since)
    crawl = db.sensor_hourly_dp(conn, "ecowitt_crawl", since)
    assert any(h["dp"] is None and h["temp"] is not None for h in outdoor)
    w = moisture.attribution_window(crawl, outdoor, now, 7, moisture.ATTR_MIN_HOURS_7D)
    assert w["ready"] is True          # 100 dp-present pairs >= 96 min
    assert w["n"] == 100               # the 20 dp-null hours were excluded, not counted
    assert w["r"] > 0.9                # correlation preserved, not skewed by nulls


def test_outdoor_range_7d_selected(conn):
    _seed_outdoor(conn, n=24)
    out = api.build_outdoor(conn, "dev1", "7d")
    assert out["range"] == "7d"


def test_outdoor_invalid_range_falls_back_to_24h(conn):
    _seed_outdoor(conn, n=24)
    out = api.build_outdoor(conn, "dev1", "bogus")
    assert out["range"] == "24h"


def test_outdoor_stale_when_latest_reading_is_old(conn):
    base = datetime.now(timezone.utc) - timedelta(hours=6)
    _seed_outdoor(conn, n=3, step_min=30, base=base)  # newest ~5h old
    out = api.build_outdoor(conn, "dev1", "24h")
    assert out["now"]["stale"] is True


def test_outdoor_stale_when_weather_feed_drops_but_poller_alive(conn):
    # The silent-failure case: good weather until 5h ago, then the feed goes
    # null while the poller keeps writing fresh rows. `now` must track the last
    # REAL weather reading (stale, last known temp), not the fresh null row that
    # would falsely read as current.
    now = datetime.now(timezone.utc)
    _seed_outdoor(conn, n=3, step_min=30, base=now - timedelta(hours=5),
                  temp=lambda i: 61.0, rh=lambda i: 78.0, dp=lambda i: 57.0)
    for j in range(4):  # fresh rows, weather feed dead
        db.insert_reading(conn, dict(
            ts=now - timedelta(minutes=30 * (3 - j)), device_id="dev1",
            indoor_temp_f=72, indoor_humidity=48, heat_setpoint_f=68,
            cool_setpoint_f=72, equipment_status="idle", mode="cool",
            daikin_outdoor_temp_f=None, daikin_outdoor_humidity=None,
            wx_outdoor_temp_f=None, wx_humidity=None, wx_dewpoint_f=None,
            wx_solar_wm2=None, wx_uv=None, wx_fc_high_f=None, wx_fc_low_f=None,
            wx_conditions=None, wx_aqi=None, wx_alert_count=0, weather_ok=False))
    out = api.build_outdoor(conn, "dev1", "24h")
    assert out["now"]["stale"] is True
    assert out["now"]["temp_f"] == 61.0  # last real weather, not the null row


def test_moisture_delta_series_carries_outdoor_rh(conn):
    _seed_crawl_and_outdoor(conn)
    m = api.build_moisture(conn, "dev1", CRAWL_CFG)
    assert m["available"] is True
    rh_pts = [pt for pt in m["delta"]["series"] if pt.get("outdoor_rh") is not None]
    assert rh_pts, "expected at least one non-null outdoor_rh point"
    # seed sets wx_humidity = 80 + (i % 5) -> one reading per hour, so each
    # bucket's outdoor_rh must land in [80, 84]; a wrong-bucket mapping wouldn't.
    assert all(80.0 <= pt["outdoor_rh"] <= 84.0 for pt in rh_pts)


def _seed_crawl_and_outdoor(conn):
    """Enough paired crawl-sensor + outdoor-weather history for build_moisture
    to render a delta series (needs the crawl sensor named per CRAWL_CFG, i.e.
    the outdoor Ecowitt slot 'Crawl Space', plus a downstairs reference)."""
    base = datetime.now(timezone.utc) - timedelta(days=2)
    for i in range(48):
        ts = base + timedelta(hours=i)
        db.insert_reading(conn, dict(
            ts=ts, device_id="dev1", indoor_temp_f=72, indoor_humidity=48,
            heat_setpoint_f=68, cool_setpoint_f=72, equipment_status="idle",
            mode="cool", daikin_outdoor_temp_f=None, daikin_outdoor_humidity=None,
            wx_outdoor_temp_f=68.0, wx_humidity=80.0 + (i % 5), wx_dewpoint_f=62.0,
            wx_solar_wm2=400, wx_uv=4, wx_fc_high_f=80, wx_fc_low_f=55,
            wx_conditions="Overcast", wx_aqi=31, wx_alert_count=0, weather_ok=True))
        db.insert_sensor_reading(conn, "ecowitt_outdoor", ts,
                                 temp_f=64.0, humidity=78.0, dewpoint_f=57.0)
        db.insert_sensor_reading(conn, "ecowitt_ch7", ts,
                                 temp_f=70.0, humidity=55.0, dewpoint_f=53.0)


def test_runtime_has_minutes(conn):
    _seed(conn)
    rt = api.build_runtime(conn, "dev1", CFG, days=1)
    assert "cool" in rt["minutes"]
    assert rt["cycle_count"] >= 1


def test_cost_has_total(conn):
    _seed(conn)
    c = api.build_cost(conn, "dev1", CFG, days=1)
    assert "total_dollars" in c
    assert c["total_kwh"] >= 0


def _seed_forecast_history(conn):
    """Four COMPLETE past local days (a reading every 10 minutes, the longest
    spacing the gap cap fully credits) with varying highs, plus a single fresh
    reading today carrying the forecast high. build_forecast excludes today
    and any day not observed end to end, so the seed must supply full days."""
    now_local = datetime.now(TZ)
    highs = [82, 88, 95, 91]
    for day_offset, high in enumerate(reversed(highs), start=1):
        day = (now_local - timedelta(days=day_offset)).date()
        for h, minute in ((h, m) for h in range(24) for m in range(0, 60, 10)):
            local = datetime(day.year, day.month, day.day, h, minute, tzinfo=TZ)
            db.insert_reading(conn, dict(
                ts=local.astimezone(timezone.utc), device_id="dev1",
                indoor_temp_f=74, indoor_humidity=45, heat_setpoint_f=68,
                cool_setpoint_f=72, equipment_status="cooling" if h % 3 else "idle",
                mode="cool", daikin_outdoor_temp_f=high, daikin_outdoor_humidity=25,
                wx_outdoor_temp_f=high - abs(14 - h), wx_humidity=25, wx_dewpoint_f=52,
                wx_solar_wm2=700, wx_uv=6, wx_fc_high_f=None,
                wx_fc_low_f=60, wx_conditions="Clear",
                wx_aqi=32, wx_alert_count=0, weather_ok=True))
    db.insert_reading(conn, dict(
        ts=datetime.now(timezone.utc), device_id="dev1",
        indoor_temp_f=74, indoor_humidity=45, heat_setpoint_f=68,
        cool_setpoint_f=72, equipment_status="idle",
        mode="cool", daikin_outdoor_temp_f=90, daikin_outdoor_humidity=25,
        wx_outdoor_temp_f=90, wx_humidity=25, wx_dewpoint_f=52,
        wx_solar_wm2=700, wx_uv=6, wx_fc_high_f=96,
        wx_fc_low_f=60, wx_conditions="Clear",
        wx_aqi=32, wx_alert_count=0, weather_ok=True))


def test_forecast_available(conn, monkeypatch):
    _seed_forecast_history(conn)
    # Tomorrow's high comes from the live feed's dated daily forecast.
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date().isoformat()
    monkeypatch.setattr(api, "_live_feed", lambda cfg: {
        "fcStale": False, "fcDaily": [{"date": tomorrow, "hi": 96}]})
    fc = api.build_forecast(conn, "dev1", CFG)
    assert fc["available"] is True
    assert fc["predicted_peak_dollars"] >= 0
    assert fc["basis"] in ("linear fit", "historical mean")
    # history = the 4 complete past days only: today's partial day (which
    # would enter the fit as "a cool day needing no cooling") is excluded
    assert fc["days_of_history"] == 4
    # peak-window minutes are a subset of the day
    assert fc["predicted_peak_cool_minutes"] <= fc["predicted_cool_minutes"]


def test_forecast_history_counts_minutes_like_the_bill(conn, monkeypatch):
    """Each past day is polled every 10 minutes at :05, :15 ... and cools from
    12:05 to 14:05 and from 16:55 to 19:05. The 16:55 -> 17:05 interval is
    billed as peak (midpoint 17:00); classifying by its start called it
    mid-peak, so the forecast learned 120 peak minutes a day, not 130."""
    now_local = datetime.now(TZ)
    today = now_local.date()
    days = [today - timedelta(days=k) for k in range(1, 5)]
    for day in sorted(days):
        for h in range(24):
            for m in range(5, 60, 10):
                local = datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
                hm = (h, m)
                cooling = (12, 5) <= hm < (14, 5) or (16, 55) <= hm < (19, 5)
                db.insert_reading(conn, dict(
                    ts=local.astimezone(timezone.utc), device_id="dev1",
                    indoor_temp_f=74, indoor_humidity=45, heat_setpoint_f=68,
                    cool_setpoint_f=72, equipment_status="cooling" if cooling else "idle",
                    mode="cool", daikin_outdoor_temp_f=90, daikin_outdoor_humidity=25,
                    wx_outdoor_temp_f=90, wx_humidity=25, wx_dewpoint_f=52,
                    wx_solar_wm2=700, wx_uv=6, wx_fc_high_f=95, wx_fc_low_f=60,
                    wx_conditions="Clear", wx_aqi=32, wx_alert_count=0, weather_ok=True))
    _insert_reading(conn, datetime.now(timezone.utc), "idle")
    seen = {}

    def fake_predict(fc_high, history, *a, **k):
        seen["history"] = history
        return {"predicted_cool_minutes": 0, "predicted_peak_cool_minutes": 0,
                "predicted_peak_dollars": 0, "peak_band": "peak", "basis": "stub",
                "has_peak": True, "peak_windows": [], "peak_days_of_history": 0}
    monkeypatch.setattr(api.correlation, "predict_peak_cost", fake_predict)
    # the forecast needs tomorrow's high from the live feed before it fits
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
    monkeypatch.setattr(api, "_live_feed", lambda cfg: {
        "fcStale": False, "fcDaily": [{"date": tomorrow.isoformat(), "hi": 95}]})
    api.build_forecast(conn, "dev1", CFG)
    weekday_days = sum(1 for d in days if d.weekday() < 5)
    hist = seen["history"]
    assert len(hist) == 4
    assert all(h["cool_minutes"] == pytest.approx(250) for h in hist)
    # The example tariff's peak is weekday-only: 130 peak minutes on each
    # weekday, none at the weekend.
    assert sorted(h["peak_cool_minutes"] for h in hist) == pytest.approx(
        sorted([130.0] * weekday_days + [0.0] * (4 - weekday_days)))


def test_forecast_unavailable_when_empty(conn):
    fc = api.build_forecast(conn, "dev1", CFG)
    assert fc == {"available": False}


def test_humidity_unavailable_when_empty(conn):
    h = api.build_humidity(conn, "dev1", CFG)
    assert h == {"available": False}


def _seed_humidity(conn):
    # 15 cooling + 15 idle readings (>= AC_EFFECT_MIN_SAMPLES each) with a
    # deliberate indoor/outdoor moisture gap so window guidance is non-neutral,
    # plus a mild outdoor temp so the "open" branch is reachable. The newest
    # row lands at "now": the panel withholds present-tense values from a
    # reading older than _HUMIDITY_STALE_S.
    base = datetime.now(timezone.utc) - timedelta(minutes=290)
    for i in range(30):
        db.insert_reading(conn, dict(
            ts=base + timedelta(minutes=10 * i), device_id="dev1",
            indoor_temp_f=75, indoor_humidity=45 if i % 2 else 55, heat_setpoint_f=68,
            cool_setpoint_f=72, equipment_status="cooling" if i % 2 else "idle",
            mode="cool", daikin_outdoor_temp_f=65, daikin_outdoor_humidity=30,
            wx_outdoor_temp_f=65, wx_humidity=30, wx_dewpoint_f=40, wx_solar_wm2=400,
            wx_uv=4, wx_fc_high_f=80, wx_fc_low_f=55, wx_conditions="Clear",
            wx_aqi=20, wx_alert_count=0, weather_ok=True))


def test_humidity_available_with_ac_effect_and_window(conn):
    _seed_humidity(conn)
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["available"] is True
    assert h["indoor_rh"] is not None
    assert h["indoor_dp"] is not None
    assert h["outdoor_dp"] == 40
    assert h["ac_effect"] is not None
    assert h["ac_effect"]["idle"] >= h["ac_effect"]["cooling"]
    assert h["ac_effect"]["basis"] == "same_hour_of_day"
    assert h["ac_effect"]["hours_matched"] >= humidity.AC_EFFECT_MIN_HOURS
    assert h["window"]["action"] in ("open", "keep_closed", "neutral")
    assert isinstance(h["trend"], list) and len(h["trend"]) > 0


def test_humidity_ac_effect_needs_matching_hours(conn):
    """Plenty of cooling readings in the afternoon and plenty of idle ones at
    night, but never both in the same hour: nothing fair to compare, so no
    'AC effect' is claimed."""
    day = datetime.now(TZ).date() - timedelta(days=1)
    for hour, status, rh in ((15, "cooling", 45), (3, "idle", 60)):
        for m in range(0, 60, 6):
            local = datetime(day.year, day.month, day.day, hour, m, tzinfo=TZ)
            db.insert_reading(conn, dict(
                ts=local.astimezone(timezone.utc), device_id="dev1",
                indoor_temp_f=75, indoor_humidity=rh, heat_setpoint_f=68,
                cool_setpoint_f=72, equipment_status=status, mode="cool",
                daikin_outdoor_temp_f=65, daikin_outdoor_humidity=30,
                wx_outdoor_temp_f=65, wx_humidity=30, wx_dewpoint_f=40,
                wx_solar_wm2=400, wx_uv=4, wx_fc_high_f=80, wx_fc_low_f=55,
                wx_conditions="Clear", wx_aqi=20, wx_alert_count=0, weather_ok=True))
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["available"] is True
    assert h["ac_effect"] is None


def test_humidity_ac_effect_none_with_too_few_samples(conn):
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(4):
        db.insert_reading(conn, dict(
            ts=base + timedelta(minutes=10 * i), device_id="dev1",
            indoor_temp_f=75, indoor_humidity=50, heat_setpoint_f=68,
            cool_setpoint_f=72, equipment_status="cooling",
            mode="cool", daikin_outdoor_temp_f=65, daikin_outdoor_humidity=30,
            wx_outdoor_temp_f=65, wx_humidity=30, wx_dewpoint_f=40, wx_solar_wm2=400,
            wx_uv=4, wx_fc_high_f=80, wx_fc_low_f=55, wx_conditions="Clear",
            wx_aqi=20, wx_alert_count=0, weather_ok=True))
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["available"] is True
    assert h["ac_effect"] is None


def test_humidity_prefers_fresh_airnow_aqi(conn):
    _seed_humidity(conn)                      # seeds readings incl. wx_aqi
    db.kv_set(conn, "ha_outdoor_aqi", {"aqi": 142.0})   # updated_at = now()
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["outdoor_aqi"] == 142.0
    assert h["aqi_source"] == "airnow"
    assert h["aqi_category"] == humidity.aqi_category(142.0)


def test_humidity_falls_back_to_wx_aqi_when_airnow_stale(conn):
    _seed_humidity(conn)                      # wx_aqi=20
    db.kv_set(conn, "ha_outdoor_aqi", {"aqi": 999.0})
    # kv_set always stamps now(); backdate it past _AIRNOW_STALE_S (1800s).
    conn.execute(
        "UPDATE kv SET updated_at = now() - interval '40 minutes'"
        " WHERE k='ha_outdoor_aqi'")
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["outdoor_aqi"] == 20
    assert h["aqi_source"] == "weather"
    assert h["aqi_category"] == humidity.aqi_category(20)


def test_humidity_falls_back_to_wx_aqi_when_airnow_missing(conn):
    _seed_humidity(conn)                      # wx_aqi=20, no ha_outdoor_aqi kv row
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["outdoor_aqi"] == 20
    assert h["aqi_source"] == "weather"


def test_humidity_falls_back_to_wx_aqi_when_airnow_value_malformed(conn):
    _seed_humidity(conn)                      # wx_aqi=20
    db.kv_set(conn, "ha_outdoor_aqi", {"aqi": None})   # fresh, but no usable value
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["outdoor_aqi"] == 20
    assert h["aqi_source"] == "weather"


def _freeze_api_now(monkeypatch, frozen_now):
    """Pin datetime.now() as seen from inside house_climate.web.api to an
    exact instant, so the AirNow staleness check's `age` is deterministic
    instead of racing wall-clock jitter between the test's SQL UPDATE and
    build_humidity()'s own datetime.now(timezone.utc) call."""
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now.astimezone(tz) if tz is not None else frozen_now
    monkeypatch.setattr(api, "datetime", _FrozenDatetime)


def test_humidity_airnow_age_exactly_at_stale_threshold_is_fresh(conn, monkeypatch):
    """The staleness check is `age <= _AIRNOW_STALE_S` — age exactly equal to
    the threshold must still count as fresh (AirNow preferred over weather)."""
    _seed_humidity(conn)                      # wx_aqi=20
    db.kv_set(conn, "ha_outdoor_aqi", {"aqi": 142.0})
    updated_at = conn.execute(
        "SELECT updated_at FROM kv WHERE k='ha_outdoor_aqi'").fetchone()[0]
    _freeze_api_now(monkeypatch, updated_at + timedelta(seconds=api._AIRNOW_STALE_S))

    h = api.build_humidity(conn, "dev1", CFG)
    assert h["outdoor_aqi"] == 142.0
    assert h["aqi_source"] == "airnow"


def test_humidity_airnow_age_one_second_past_threshold_is_stale(conn, monkeypatch):
    """One second past the same boundary must flip to the weather fallback —
    pins the `<=` (not `<`) as the exact edge, not just "somewhere near 1800s"."""
    _seed_humidity(conn)                      # wx_aqi=20
    db.kv_set(conn, "ha_outdoor_aqi", {"aqi": 142.0})
    updated_at = conn.execute(
        "SELECT updated_at FROM kv WHERE k='ha_outdoor_aqi'").fetchone()[0]
    frozen = updated_at + timedelta(seconds=api._AIRNOW_STALE_S + 1)
    _freeze_api_now(monkeypatch, frozen)
    # Keep the thermostat reading fresh at the frozen instant, so the modeled
    # wx_aqi fallback is still a present-tense value (a stale row withholds it).
    db.insert_reading(conn, dict(
        ts=frozen, device_id="dev1", indoor_temp_f=75, indoor_humidity=50,
        heat_setpoint_f=68, cool_setpoint_f=72, equipment_status="idle", mode="cool",
        daikin_outdoor_temp_f=65, daikin_outdoor_humidity=30, wx_outdoor_temp_f=65,
        wx_humidity=30, wx_dewpoint_f=40, wx_solar_wm2=400, wx_uv=4, wx_fc_high_f=80,
        wx_fc_low_f=55, wx_conditions="Clear", wx_aqi=20, wx_alert_count=0,
        weather_ok=True))

    h = api.build_humidity(conn, "dev1", CFG)
    assert h["outdoor_aqi"] == 20
    assert h["aqi_source"] == "weather"


def test_humidity_both_aqi_sources_absent_is_none(conn):
    """No wx_aqi on the reading AND no ha_outdoor_aqi kv row at all (not
    even a stale/malformed one) -> outdoor_aqi and aqi_source both None."""
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(4):
        db.insert_reading(conn, dict(
            ts=base + timedelta(minutes=10 * i), device_id="dev1",
            indoor_temp_f=75, indoor_humidity=50, heat_setpoint_f=68,
            cool_setpoint_f=72, equipment_status="cooling",
            mode="cool", daikin_outdoor_temp_f=65, daikin_outdoor_humidity=30,
            wx_outdoor_temp_f=65, wx_humidity=30, wx_dewpoint_f=40, wx_solar_wm2=400,
            wx_uv=4, wx_fc_high_f=80, wx_fc_low_f=55, wx_conditions="Clear",
            wx_aqi=None, wx_alert_count=0, weather_ok=True))
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["outdoor_aqi"] is None
    assert h["aqi_source"] is None


def test_humidity_carries_configured_aqi_unhealthy_default(conn):
    # config.example.json sets alerts.aqi_unhealthy: 101 explicitly; this pins
    # the wire field so the wall dashboard's smoke banner can read it instead
    # of trusting a hardcoded JS constant to stay in sync with the config.
    _seed_humidity(conn)
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["aqi_unhealthy"] == CFG.alerts.get("aqi_unhealthy", 101)


def test_humidity_carries_configured_aqi_unhealthy_custom(conn):
    _seed_humidity(conn)
    custom_cfg = dataclasses.replace(CFG, alerts={**CFG.alerts, "aqi_unhealthy": 175})
    h = api.build_humidity(conn, "dev1", custom_cfg)
    assert h["aqi_unhealthy"] == 175


def test_humidity_aqi_unhealthy_defaults_when_unset_in_config(conn):
    _seed_humidity(conn)
    alerts_no_threshold = {k: v for k, v in CFG.alerts.items() if k != "aqi_unhealthy"}
    custom_cfg = dataclasses.replace(CFG, alerts=alerts_no_threshold)
    h = api.build_humidity(conn, "dev1", custom_cfg)
    assert h["aqi_unhealthy"] == 101


def _seed_local_day(conn, day, hours):
    """Insert a reading every 10 minutes through each hour in `hours` (LOCAL,
    on 2026-08-<day>), alternating cooling/idle. Ten minutes is the longest
    spacing the gap cap fully credits, so a run of whole hours is fully
    observed; a missing hour is an hour nobody saw."""
    i = 0
    for h in hours:
        for minute in range(0, 60, 10):
            local = datetime(2026, 8, day, h, minute, tzinfo=TZ)
            db.insert_reading(conn, dict(
                ts=local.astimezone(timezone.utc), device_id="dev1",
                indoor_temp_f=73, indoor_humidity=48, heat_setpoint_f=68,
                cool_setpoint_f=72, equipment_status="cooling" if i % 2 else "idle",
                mode="cool", daikin_outdoor_temp_f=88, daikin_outdoor_humidity=30,
                wx_outdoor_temp_f=88, wx_humidity=30, wx_dewpoint_f=55, wx_solar_wm2=750,
                wx_uv=6, wx_fc_high_f=90, wx_fc_low_f=60, wx_conditions="Clear",
                wx_aqi=30, wx_alert_count=0, weather_ok=True))
            i += 1


FULL_DAY_HOURS = list(range(24))


def test_cost_summary_monotonic_and_projected(conn):
    # "now" = 2026-08-10 15:00 local (a Monday, mid-afternoon -> today is partial).
    now_local = datetime(2026, 8, 10, 15, 0, tzinfo=TZ)
    now = now_local.astimezone(timezone.utc)

    # Aug 6: a partial day (readings only 10:00-14:00 local) -> must NOT count
    # as a complete day even though it's fully in the past.
    _seed_local_day(conn, 6, [10, 11, 12, 13, 14])
    # Aug 7, 8, 9: three full past days.
    _seed_local_day(conn, 7, FULL_DAY_HOURS)
    _seed_local_day(conn, 8, FULL_DAY_HOURS)
    _seed_local_day(conn, 9, FULL_DAY_HOURS)
    # Aug 10 (today): partial, readings only up to 14:00 local.
    _seed_local_day(conn, 10, [h for h in FULL_DAY_HOURS if h <= 14])

    summary = api.build_cost_summary(conn, "dev1", CFG, now=now)

    assert summary["today"]["dollars"] <= summary["week"]["dollars"] <= summary["month_to_date"]["dollars"]
    assert summary["today"]["kwh"] <= summary["week"]["kwh"] <= summary["month_to_date"]["kwh"]
    assert summary["complete_days"] == 3

    days_in_month = calendar.monthrange(2026, 8)[1]
    assert summary["avg_per_day"] is not None
    assert summary["projected_month"] == round(summary["avg_per_day"] * days_in_month, 2)
    assert summary["tz"] == CFG.timezone
    assert "peak" in summary["by_band"] or "midpeak" in summary["by_band"] or "offpeak" in summary["by_band"]


def test_cost_summary_average_excludes_a_day_whose_outage_hid_runtime(conn):
    """A 2h10m hole in the afternoon passed the old rule (spans the day, no gap
    over 3 hours) and entered avg_per_day priced as if nothing ran in it. The
    average must be built from fully observed days only."""
    now = datetime(2026, 8, 10, 15, 0, tzinfo=TZ).astimezone(timezone.utc)
    _seed_local_day(conn, 6, [h for h in FULL_DAY_HOURS if h not in (16, 17)])
    for d in (7, 8, 9):
        _seed_local_day(conn, d, FULL_DAY_HOURS)
    summary = api.build_cost_summary(conn, "dev1", CFG, now=now)
    assert summary["complete_days"] == 3
    rows = db.recent_readings(conn, "dev1", now - timedelta(days=40))
    full = [api.cost.compute(rows, CFG.tou, CFG.system_kw, CFG.timezone,
                             heat_kw=CFG.heat_kw,
                             start=api._local_day_bounds(datetime(2026, 8, d).date(), TZ)[0],
                             end=api._local_day_bounds(datetime(2026, 8, d).date(), TZ)[1]
                             ).total_dollars for d in (7, 8, 9)]
    assert summary["avg_per_day"] == round(sum(full) / 3, 2)


def test_cost_summary_today_counts_the_interval_across_midnight(conn):
    """The 23:55 -> 00:05 interval has its midpoint at midnight, so it is
    today's. Slicing the rows by timestamp first dropped it: today showed 20
    minutes of cooling instead of 30."""
    now = datetime(2026, 8, 10, 0, 30, tzinfo=TZ).astimezone(timezone.utc)
    for local in (datetime(2026, 8, 9, 23, 55, tzinfo=TZ),
                  datetime(2026, 8, 10, 0, 5, tzinfo=TZ),
                  datetime(2026, 8, 10, 0, 15, tzinfo=TZ),
                  datetime(2026, 8, 10, 0, 25, tzinfo=TZ)):
        _insert_reading(conn, local.astimezone(timezone.utc), "cooling")
    summary = api.build_cost_summary(conn, "dev1", CFG, now=now)
    assert summary["today"]["kwh"] == round(30 / 60 * CFG.system_kw, 2)


def test_cost_summary_no_projection_with_only_partial_today(conn):
    now_local = datetime(2026, 8, 10, 15, 0, tzinfo=TZ)
    now = now_local.astimezone(timezone.utc)

    _seed_local_day(conn, 10, [h for h in FULL_DAY_HOURS if h <= 14])

    summary = api.build_cost_summary(conn, "dev1", CFG, now=now)

    assert summary["complete_days"] == 0
    assert summary["avg_per_day"] is None
    assert summary["projected_month"] is None
    assert summary["today"]["dollars"] <= summary["week"]["dollars"] <= summary["month_to_date"]["dollars"]


def test_cost_summary_empty_history(conn):
    # A device with no readings at all must not crash and must zero out cleanly.
    now = datetime(2026, 8, 10, 15, 0, tzinfo=TZ).astimezone(timezone.utc)
    summary = api.build_cost_summary(conn, "dev1", CFG, now=now)
    assert summary["today"] == {"dollars": 0.0, "kwh": 0.0, "by_band": {}}
    assert summary["week"] == {"dollars": 0.0, "kwh": 0.0}
    assert summary["month_to_date"] == {"dollars": 0.0, "kwh": 0.0}
    assert summary["complete_days"] == 0
    assert summary["avg_per_day"] is None
    assert summary["projected_month"] is None
    assert summary["pct_runtime_peak"] == 0
    assert summary["by_band"] == {}
    assert summary["as_of"] is None
    assert summary["running"] is False
    # band_now/rate_now depend only on the TOU table and the clock, not on
    # device data, so they're populated even with zero readings (matching
    # tier_now, which was already populated in this case).
    assert summary["band_now"] == "midpeak"
    assert summary["live_rate_per_hr"] == 0.0


def _insert_reading(conn, ts_utc, equipment_status, mode="cool"):
    db.insert_reading(conn, dict(
        ts=ts_utc, device_id="dev1",
        indoor_temp_f=74, indoor_humidity=45, heat_setpoint_f=68,
        cool_setpoint_f=72, equipment_status=equipment_status,
        mode=mode, daikin_outdoor_temp_f=95, daikin_outdoor_humidity=25,
        wx_outdoor_temp_f=95, wx_humidity=25, wx_dewpoint_f=55, wx_solar_wm2=800,
        wx_uv=7, wx_fc_high_f=97, wx_fc_low_f=65, wx_conditions="Clear",
        wx_aqi=35, wx_alert_count=0, weather_ok=True))


def test_cost_summary_live_accrual_running_at_peak(conn):
    # Monday 2026-08-10 18:00 local -> weekday on-peak band (17:00-21:00).
    now_local = datetime(2026, 8, 10, 18, 0, tzinfo=TZ)
    now = now_local.astimezone(timezone.utc)
    reading_ts = (now_local - timedelta(minutes=2)).astimezone(timezone.utc)
    _insert_reading(conn, reading_ts, "cooling")

    summary = api.build_cost_summary(conn, "dev1", CFG, now=now)

    band_name, rate = CFG.tou.band_for(now_local)
    assert band_name == "peak"
    assert summary["running"] is True
    assert summary["band_now"] == "peak"
    assert summary["live_rate_per_hr"] == round(CFG.system_kw * rate, 4)
    assert summary["as_of"] == reading_ts.isoformat()


def test_cost_summary_has_band_and_next_fields(conn):
    _seed(conn)
    tz = ZoneInfo(CFG.timezone)
    now = datetime(2026, 8, 10, 16, 30, tzinfo=tz).astimezone(timezone.utc)  # Mon mid-peak
    s = api.build_cost_summary(conn, "dev1", CFG, now=now)
    assert s["tier_now"] == "mid"
    assert s["next_band"] == "peak"
    assert s["next_tier"] == "peak"
    assert s["next_change_at"].startswith("2026-08-10T17:00")
    assert 25 <= s["minutes_to_change"] <= 35
    assert s["rate_now"] == CFG.tou.band_for(now.astimezone(tz))[1]
    # peak_rate feeds the ribbon legend swatch (fixed 2026-08-14: it used to
    # be a hardcoded '$0.43/kWh' that could disagree with this config).
    assert s["peak_rate"] == max(b.rate for b in CFG.tou.bands)
    # peak_windows feeds the ribbon's on-peak shading (fixed 2026-08-14: it
    # used to hardcode weekday 17:00-21:00, wrong for any other utility).
    # The example config's single peak band is weekday 17:00-21:00.
    assert s["peak_windows"] == [{"start": "17:00", "end": "21:00", "weekday_only": True}]


def test_cost_summary_on_a_tou_holiday(conn):
    """Labor Day 2026 at 18:00 with a holiday-aware tariff: the band now is
    off-peak, the live accrual runs at the off-peak rate, and the date is
    listed so the ribbon leaves it unshaded."""
    from house_climate.config import TouTable
    tou = TouTable(CFG.tou.summer_months, CFG.tou.bands,
                   holiday_rules=("labor_day",), holiday_observed="none")
    cfg = dataclasses.replace(CFG, tou=tou)
    now_local = datetime(2026, 9, 7, 18, 0, tzinfo=TZ)
    _insert_reading(conn, (now_local - timedelta(minutes=2)).astimezone(timezone.utc), "cooling")
    s = api.build_cost_summary(conn, "dev1", cfg, now=now_local.astimezone(timezone.utc))
    offpeak = min(b.rate for b in CFG.tou.bands)
    assert s["tier_now"] == "off"
    assert s["rate_now"] == offpeak
    assert s["live_rate_per_hr"] == round(CFG.system_kw * offpeak, 4)
    assert s["tou_holidays"] == ["2026-09-07"]
    # Without the holiday config the same instant is peak, as before. Built
    # explicitly: CFG may be an operator's config.json with holidays already.
    plain = dataclasses.replace(CFG, tou=TouTable(CFG.tou.summer_months, CFG.tou.bands))
    s0 = api.build_cost_summary(conn, "dev1", plain, now=now_local.astimezone(timezone.utc))
    assert s0["tier_now"] == "peak" and s0["tou_holidays"] == []


def _insert_precool_reading(conn, ts_utc, status, fc_high):
    db.insert_reading(conn, dict(
        ts=ts_utc, device_id="dev1",
        indoor_temp_f=76, indoor_humidity=45, heat_setpoint_f=68,
        cool_setpoint_f=72, equipment_status=status,
        mode="cool", daikin_outdoor_temp_f=95, daikin_outdoor_humidity=25,
        wx_outdoor_temp_f=95, wx_humidity=25, wx_dewpoint_f=55, wx_solar_wm2=800,
        wx_uv=7, wx_fc_high_f=fc_high, wx_fc_low_f=65, wx_conditions="Clear",
        wx_aqi=35, wx_alert_count=0, weather_ok=True))


def _seed_precool_onpeak_day(conn, day, fc_high):
    """24 cooling readings every 10 min from 17:00-20:50 local (Monday
    2026-08-<day>), plus a 21:00 idle boundary reading to close the last
    interval. 10-minute spacing matches api.py's _PRECOOL_MAX_GAP_S (600s)
    exactly, so no interval is silently gap-capped. Total on-peak cooling =
    240 min = 4h -> 4 * CFG.system_kw kWh (kW-agnostic: the config's
    system_kw tracks the real equipment and must not break this test)."""
    for i in range(24):
        local = datetime(2026, 8, day, 17, 0, tzinfo=TZ) + timedelta(minutes=10 * i)
        _insert_precool_reading(conn, local.astimezone(timezone.utc), "cooling", fc_high)
    boundary = datetime(2026, 8, day, 21, 0, tzinfo=TZ)
    _insert_precool_reading(conn, boundary.astimezone(timezone.utc), "idle", fc_high)


def test_precool_advice_relevant_hot_day(conn):
    # Monday 2026-08-10: 4h of on-peak (17:00-21:00) cooling -> 10 kWh/day.
    _seed_precool_onpeak_day(conn, 10, fc_high=90)

    # Off-window cooling (midpeak 10:00 and offpeak 22:00) must NOT count
    # toward shiftable_kwh -- proves the 17:00-21:00 window filter works.
    _insert_precool_reading(
        conn, datetime(2026, 8, 10, 10, 0, tzinfo=TZ).astimezone(timezone.utc), "cooling", 90)
    _insert_precool_reading(
        conn, datetime(2026, 8, 10, 10, 10, tzinfo=TZ).astimezone(timezone.utc), "idle", 90)
    _insert_precool_reading(
        conn, datetime(2026, 8, 10, 22, 0, tzinfo=TZ).astimezone(timezone.utc), "cooling", 90)
    _insert_precool_reading(
        conn, datetime(2026, 8, 10, 22, 10, tzinfo=TZ).astimezone(timezone.utc), "idle", 90)

    # Weekend on-peak-hour cooling (Saturday 2026-08-08, 17:30) must NOT
    # count either -- proves the weekday-only filter works.
    _insert_precool_reading(
        conn, datetime(2026, 8, 8, 17, 30, tzinfo=TZ).astimezone(timezone.utc), "cooling", 90)
    _insert_precool_reading(
        conn, datetime(2026, 8, 8, 17, 40, tzinfo=TZ).astimezone(timezone.utc), "idle", 90)

    now = datetime(2026, 8, 10, 22, 30, tzinfo=TZ).astimezone(timezone.utc)
    advice = api.build_precool_advice(conn, "dev1", CFG, now=now)

    peak_rate = next(b.rate for b in CFG.tou.bands if b.name == "peak")
    mid_rate = next(b.rate for b in CFG.tou.bands if b.name == "midpeak")
    off_rate = next(b.rate for b in CFG.tou.bands if b.name == "offpeak")

    shiftable = 4.0 * CFG.system_kw   # 4h of on-peak cooling at the config kW
    assert advice["relevant"] is True
    assert advice["fc_high"] == 90
    assert advice["shiftable_kwh"] == round(shiftable, 2)
    assert advice["peak_rate"] == peak_rate
    assert advice["mid_rate"] == mid_rate
    expected_savings = round(shiftable * (peak_rate - mid_rate), 2)
    assert advice["savings"] == expected_savings
    # Would be wrong if peak-off_rate (offpeak) were used instead of
    # peak-mid_rate, or if the window/weekday filters leaked extra kWh in.
    assert advice["savings"] != round(shiftable * (peak_rate - off_rate), 2)


def test_precool_advice_mild_forecast(conn):
    _seed_precool_onpeak_day(conn, 10, fc_high=70)
    now = datetime(2026, 8, 10, 22, 30, tzinfo=TZ).astimezone(timezone.utc)
    advice = api.build_precool_advice(conn, "dev1", CFG, now=now)
    assert advice == {"relevant": False, "reason": "mild", "fc_high": 70}


def test_precool_advice_low_peak_use(conn):
    # Only 2 minutes of on-peak cooling on record -> well under the 0.2 kWh
    # relevance floor, even though the forecast is hot.
    _insert_precool_reading(
        conn, datetime(2026, 8, 10, 17, 0, tzinfo=TZ).astimezone(timezone.utc), "cooling", 90)
    _insert_precool_reading(
        conn, datetime(2026, 8, 10, 17, 2, tzinfo=TZ).astimezone(timezone.utc), "idle", 90)
    now = datetime(2026, 8, 10, 22, 30, tzinfo=TZ).astimezone(timezone.utc)
    advice = api.build_precool_advice(conn, "dev1", CFG, now=now)
    assert advice == {"relevant": False, "reason": "low_peak_use"}


def test_precool_advice_collecting_when_no_onpeak_samples(conn):
    # Readings exist but none fall in the weekday 17:00-21:00 window.
    _insert_precool_reading(
        conn, datetime(2026, 8, 10, 9, 0, tzinfo=TZ).astimezone(timezone.utc), "cooling", 90)
    now = datetime(2026, 8, 10, 22, 30, tzinfo=TZ).astimezone(timezone.utc)
    advice = api.build_precool_advice(conn, "dev1", CFG, now=now)
    assert advice == {"relevant": False, "reason": "collecting"}


def test_precool_advice_collecting_when_empty(conn):
    now = datetime(2026, 8, 10, 22, 30, tzinfo=TZ).astimezone(timezone.utc)
    advice = api.build_precool_advice(conn, "dev1", CFG, now=now)
    assert advice == {"relevant": False, "reason": "collecting"}


def test_cost_summary_live_accrual_idle_not_running(conn):
    now_local = datetime(2026, 8, 10, 18, 0, tzinfo=TZ)
    now = now_local.astimezone(timezone.utc)
    reading_ts = (now_local - timedelta(minutes=2)).astimezone(timezone.utc)
    _insert_reading(conn, reading_ts, "idle")

    summary = api.build_cost_summary(conn, "dev1", CFG, now=now)

    assert summary["running"] is False
    assert summary["live_rate_per_hr"] == 0.0
    assert summary["band_now"] == "peak"
    assert summary["as_of"] == reading_ts.isoformat()


def _seed_timeline_reading(conn, ts, status, cool=72, heat=68, mode="cool"):
    db.insert_reading(conn, dict(
        ts=ts, device_id="dev1",
        indoor_temp_f=74, indoor_humidity=45, heat_setpoint_f=heat,
        cool_setpoint_f=cool, equipment_status=status,
        mode=mode, daikin_outdoor_temp_f=90, daikin_outdoor_humidity=30,
        wx_outdoor_temp_f=90, wx_humidity=30, wx_dewpoint_f=55, wx_solar_wm2=500,
        wx_uv=5, wx_fc_high_f=90, wx_fc_low_f=60, wx_conditions="Clear",
        wx_aqi=30, wx_alert_count=0, weather_ok=True))


def test_timeline_unavailable_when_empty(conn):
    t = api.build_timeline(conn, "dev1", CFG)
    assert t == {"available": False}


def test_timeline_segments_merge_and_clamp_large_gaps(conn):
    # Readings every 5 min, changing equipment_status along the way, with one
    # deliberate 60-minute gap between two same-status ("idle") readings and
    # a final reading left ~75 minutes before "now" (no reading after it).
    # Both gaps blow past the 600s (10 min) clamp, so:
    #  - the same-status gap must NOT merge across it (proves the merge rule
    #    checks contiguity, not just equal status) -- would be 6 segments
    #    instead of 7 if it wrongly merged.
    #  - both the gapped segment and the trailing (to "now") segment must be
    #    capped at 10 minutes -- would be 60m and ~75m respectively if the
    #    clamp were missing, so the assertions are non-vacuous.
    now = datetime.now(timezone.utc)
    base = now - timedelta(hours=3)
    sequence = [
        (0, "idle"), (5, "idle"),
        (10, "cooling"), (15, "cooling"), (20, "cooling"),
        (25, "heating"), (30, "heating"),
        (35, "fan"),
        (40, "idle"),
        (100, "idle"),      # +60min gap from the previous idle reading
        (105, "cooling"),   # last reading; ~75min before real "now"
    ]
    for offset, status in sequence:
        _seed_timeline_reading(conn, base + timedelta(minutes=offset), status)

    t = api.build_timeline(conn, "dev1", CFG, hours=24)

    assert t["available"] is True
    assert t["hours"] == 24
    assert t["tz"] == CFG.timezone
    datetime.fromisoformat(t["window_start"])
    datetime.fromisoformat(t["window_end"])

    segs = t["segments"]
    assert [s["status"] for s in segs] == [
        "idle", "cooling", "heating", "fan", "idle", "idle", "cooling",
    ]
    assert [s["minutes"] for s in segs] == [10.0, 15.0, 10.0, 5.0, 10.0, 5.0, 10.0]
    # Would be 60.0 without the clamp (the real gap to the next reading).
    assert segs[4]["minutes"] == 10.0
    # Would be ~75.0 without the clamp (the real gap to "now").
    assert segs[6]["minutes"] == 10.0


def test_timeline_setpoint_changes_excludes_baseline(conn):
    now = datetime.now(timezone.utc)
    base = now - timedelta(hours=2)
    rows = [
        (0, "idle", 72, 68),    # baseline -- must not appear as a change
        (5, "idle", 72, 68),
        (10, "cooling", 70, 68),   # cool setpoint change: 72 -> 70
        (15, "cooling", 70, 68),
        (20, "idle", 70, 66),      # heat setpoint change: 68 -> 66
    ]
    for offset, status, cool, heat in rows:
        _seed_timeline_reading(conn, base + timedelta(minutes=offset), status, cool=cool, heat=heat)

    t = api.build_timeline(conn, "dev1", CFG, hours=24)

    changes = t["setpoint_changes"]
    assert len(changes) == 2
    assert changes[0]["ts"] == (base + timedelta(minutes=10)).isoformat()
    assert changes[0]["cool"] == 70 and changes[0]["prev_cool"] == 72
    assert changes[0]["heat"] == 68 and changes[0]["prev_heat"] == 68
    assert changes[0]["mode"] == "cool"
    assert changes[1]["ts"] == (base + timedelta(minutes=20)).isoformat()
    assert changes[1]["cool"] == 70 and changes[1]["prev_cool"] == 70
    assert changes[1]["heat"] == 66 and changes[1]["prev_heat"] == 68
    # The baseline reading's timestamp must never appear as a change.
    assert all(c["ts"] != base.isoformat() for c in changes)


# --------------------------------------------------------------------------
# System Health: setpoint hold tightness, short-cycling, filter runtime
# --------------------------------------------------------------------------

def _health_reading(conn, ts, *, mode, cool=72, heat=68, indoor, status="idle"):
    db.insert_reading(conn, dict(
        ts=ts, device_id="dev1",
        indoor_temp_f=indoor, indoor_humidity=45, heat_setpoint_f=heat,
        cool_setpoint_f=cool, equipment_status=status,
        mode=mode, daikin_outdoor_temp_f=85, daikin_outdoor_humidity=30,
        wx_outdoor_temp_f=85, wx_humidity=30, wx_dewpoint_f=55, wx_solar_wm2=500,
        wx_uv=5, wx_fc_high_f=85, wx_fc_low_f=60, wx_conditions="Clear",
        wx_aqi=30, wx_alert_count=0, weather_ok=True))


def test_health_unavailable_when_empty(conn):
    h = api.build_health(conn, "dev1", CFG)
    assert h == {"available": False}


def test_health_hold_tight_and_skips_off_mode(conn):
    # 8 cool-mode readings within 0.3F of setpoint (tolerance is 1.0F) plus 2
    # off-mode readings 12F off setpoint -- the off-mode rows must be SKIPPED
    # (no "active setpoint" while off), or avg_abs_dev/pct_within_tol would be
    # dragged way off from what's asserted below.
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(8):
        _health_reading(conn, base + timedelta(minutes=5 * i), mode="cool", cool=72, indoor=72.3)
    for i in range(2):
        _health_reading(conn, base + timedelta(minutes=100 + 5 * i), mode="off", cool=72, indoor=60)

    h = api.build_health(conn, "dev1", CFG)
    assert h["available"] is True
    assert h["hold"]["pct_within_tol"] == 100.0
    assert h["hold"]["avg_abs_dev"] == 0.3
    assert h["hold"]["max_abs_dev"] == 0.3
    assert h["tolerance"] == CFG.setpoint_tolerance_f


def test_health_hold_mixed_modes_and_tolerance(conn):
    # 4 cool-mode readings 6.0F over cool_setpoint_f (outside the 1.0F
    # tolerance) + 4 heat-mode readings 0.5F under heat_setpoint_f (inside
    # tolerance). If the active-setpoint-by-mode selection were wrong (e.g.
    # always comparing against cool_setpoint_f), the heat-mode readings would
    # count as no miss at all instead of 0.5, and the average below would fail.
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(4):
        _health_reading(conn, base + timedelta(minutes=5 * i), mode="cool", cool=72, heat=68, indoor=78)
    for i in range(4):
        _health_reading(conn, base + timedelta(minutes=100 + 5 * i), mode="heat", cool=72, heat=68, indoor=67.5)

    h = api.build_health(conn, "dev1", CFG)
    assert h["hold"]["pct_within_tol"] == 50.0
    assert h["hold"]["avg_abs_dev"] == 3.25
    assert h["hold"]["max_abs_dev"] == 6.0


def test_health_hold_counts_only_the_miss_side_of_each_setpoint(conn):
    """A 70F house under a 76F cool setpoint is the weather doing the AC's
    job, not a 6-degree miss. In cool mode only overshoot above the cool
    setpoint counts; in heat mode only a shortfall below the heat setpoint."""
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(4):   # cool mode, well under the cool setpoint: perfect
        _health_reading(conn, base + timedelta(minutes=5 * i), mode="cool", cool=76, indoor=70)
    for i in range(4):   # heat mode, warmer than the heat setpoint: perfect
        _health_reading(conn, base + timedelta(minutes=30 + 5 * i), mode="heat", heat=66, indoor=71)
    for i in range(2):   # cool mode, 2F OVER the cool setpoint: a real miss
        _health_reading(conn, base + timedelta(minutes=60 + 5 * i), mode="cool", cool=74, indoor=76)
    h = api.build_health(conn, "dev1", CFG)
    assert h["hold"]["max_abs_dev"] == 2.0
    assert h["hold"]["avg_abs_dev"] == 0.4          # 2 misses of 2F over 10 readings
    assert h["hold"]["pct_within_tol"] == 80.0


def test_health_short_cycling_unhealthy(conn):
    # Alternating 3-minute cooling/idle blips, all well under
    # short_cycle_minutes (10) -- every cool cycle is "short".
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    pattern = ["cooling", "idle"] * 6
    for i, status in enumerate(pattern):
        _health_reading(conn, base + timedelta(minutes=3 * i), mode="cool", indoor=73, status=status)

    h = api.build_health(conn, "dev1", CFG)
    assert h["short_cycles"] >= 1
    assert h["short_cycles_healthy"] is False


def test_health_short_cycling_healthy(conn):
    # One long, steady cooling run (60 min, readings 10 min apart) followed
    # by idle -- zero short cycles.
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(7):
        _health_reading(conn, base + timedelta(minutes=10 * i), mode="cool", indoor=73, status="cooling")
    _health_reading(conn, base + timedelta(minutes=70), mode="cool", indoor=72, status="idle")

    h = api.build_health(conn, "dev1", CFG)
    assert h["short_cycles"] == 0
    assert h["short_cycles_healthy"] is True


def test_health_filter_due_flips_at_threshold(conn):
    # A crafted low threshold (1.0h = 60min) makes the boundary reachable
    # without seeding hundreds of hours of fake runtime.
    low_cfg = dataclasses.replace(CFG, filter_reminder_hours=1.0)
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    # 7 cooling readings, 10 min apart -> 6 intervals x 10min = 60min = 1.0h.
    for i in range(7):
        _health_reading(conn, base + timedelta(minutes=10 * i), mode="cool", indoor=73, status="cooling")

    h = api.build_health(conn, "dev1", low_cfg)
    assert h["filter"]["runtime_hours"] == 1.0
    assert h["filter"]["threshold"] == 1.0
    assert h["filter"]["due"] is True
    assert h["filter"]["pct"] == 100.0


def test_health_filter_not_due_below_threshold(conn):
    low_cfg = dataclasses.replace(CFG, filter_reminder_hours=1.0)
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    # 3 cooling readings, 10 min apart -> 2 intervals x 10min = 20min = 0.333h.
    for i in range(3):
        _health_reading(conn, base + timedelta(minutes=10 * i), mode="cool", indoor=73, status="cooling")

    h = api.build_health(conn, "dev1", low_cfg)
    assert h["filter"]["runtime_hours"] == 0.3
    assert h["filter"]["due"] is False
    assert h["filter"]["pct"] == 33.0
    # Sanity: with the real (300h) default threshold this same runtime is
    # nowhere near due -- proves "due" isn't hardcoded True and genuinely
    # tracks the threshold comparison.
    default_health = api.build_health(conn, "dev1", CFG)
    assert default_health["filter"]["due"] is False
    assert default_health["filter"]["threshold"] == CFG.filter_reminder_hours


def test_health_filter_clock_resets_on_change(conn):
    # Two separated blocks of cooling runtime; logging a filter change between
    # them must make the filter clock count only the post-change block.
    now = datetime.now(timezone.utc)
    for i in range(10):
        _health_reading(conn, now - timedelta(days=10) + timedelta(minutes=5 * i),
                        mode="cool", indoor=72, status="cooling")
    for i in range(10):
        _health_reading(conn, now - timedelta(days=2) + timedelta(minutes=5 * i),
                        mode="cool", indoor=72, status="cooling")

    before = api.build_health(conn, "dev1", CFG)["filter"]
    assert before["changed_at"] is None
    assert before["days_since"] is None
    hours_all = before["runtime_hours"]

    db.record_filter_change(conn, "dev1", changed_at=now - timedelta(days=5))
    after = api.build_health(conn, "dev1", CFG)["filter"]
    assert after["changed_at"] is not None
    assert after["days_since"] == 5
    assert 0 < after["runtime_hours"] < hours_all


def test_rooms_present_waiting_and_battery(conn):
    now = datetime.now(timezone.utc)
    db.insert_sensor_reading(conn, "ecowitt_ch8", now, temp_f=78.0, humidity=40.0,
                             battery=0.0, extra={"signal": 3})
    db.insert_sensor_reading(conn, "ecowitt_ch7", now, temp_f=72.0, humidity=55.0, battery=1.0)
    # Crawl Space is the outdoor WH32 slot, not a channel.
    db.insert_sensor_reading(conn, "ecowitt_outdoor", now, temp_f=62.0, humidity=70.0, battery=0.0)
    # self-contained channel map (independent of the deployment config)
    ec = {"enabled": True, "gateway_url": "http://gw", "outdoor_name": "Crawl Space",
          "channels": {"8": "Upstairs", "7": "Downstairs", "5": "Garage"}}
    r = api.build_rooms(conn, dataclasses.replace(CFG, ecowitt=ec))
    assert r["available"] is True
    by = {x["name"]: x for x in r["rooms"]}
    assert by["Upstairs"]["present"] and by["Upstairs"]["temp_f"] == 78.0
    assert by["Upstairs"]["signal"] == 3          # from extra jsonb
    assert by["Downstairs"]["signal"] is None     # no extra -> None
    assert by["Downstairs"]["battery_low"] is True
    assert by["Crawl Space"]["present"] and by["Crawl Space"]["humidity"] == 70.0
    assert by["Garage"]["present"] is False   # CH5 sensor not installed


def test_thermal_available_shape(conn):
    now = datetime.now(timezone.utc)
    for i in range(6):
        _health_reading(conn, now - timedelta(minutes=5 * i), mode="cool", indoor=74, status="idle")
    r = api.build_thermal(conn, "dev1", CFG)
    assert r["available"] is True
    assert "coasting" in r and "load" in r and "history_hours" in r


def _seed_crawl(conn, now, hours=12, rh_fn=None, temp_fn=None, step_min=3):
    """Seed crawl readings every step_min minutes covering the last `hours`."""
    rh_fn = rh_fn or (lambda i: 55.0)
    temp_fn = temp_fn or (lambda i: 62.0)
    n = int(hours * 60 / step_min)
    for i in range(n):
        ts = now - timedelta(minutes=step_min * (n - 1 - i))
        db.insert_sensor_reading(conn, "ecowitt_outdoor", ts,
                                 temp_f=temp_fn(i), humidity=rh_fn(i))


def test_crawl_not_configured(conn):
    cfg = dataclasses.replace(CFG, ecowitt=None)
    r = api.build_crawl(conn, "dev1", cfg, "24h")
    assert r == {"available": False, "reason": "not_configured"}


def test_crawl_no_data(conn):
    r = api.build_crawl(conn, "dev1", CRAWL_CFG, "24h")
    assert r == {"available": False, "reason": "no_data"}


def test_crawl_resolves_channel_named_crawl(conn):
    # If the crawl probe ever moves onto a WH31 channel, the NAME finds it.
    ec = dict(CRAWL_CFG.ecowitt)
    ec["channels"] = {"6": "Crawl Space"}
    ec["outdoor_name"] = "Backyard"
    cfg = dataclasses.replace(CFG, ecowitt=ec)
    now = datetime.now(timezone.utc)
    db.insert_sensor_reading(conn, "ecowitt_ch6", now, temp_f=60.0, humidity=50.0)
    db.insert_sensor_reading(conn, "ecowitt_ch6", now - timedelta(minutes=3),
                             temp_f=60.0, humidity=52.0)
    r = api.build_crawl(conn, "dev1", cfg, "24h", now=now)
    assert r["available"] is True
    assert r["sensor"] == "Crawl Space"
    assert r["rh_now"] == 50.0


def test_crawl_stats_high_low_avg_and_series(conn):
    now = datetime.now(timezone.utc)
    # 12h at 55%, with one 74% spike 6h ago and one 40% dip 3h ago
    n = int(12 * 60 / 3)
    spike_i = n - 1 - int(6 * 60 / 3)
    dip_i = n - 1 - int(3 * 60 / 3)
    _seed_crawl(conn, now, hours=12,
                rh_fn=lambda i: 74.0 if i == spike_i else (40.0 if i == dip_i else 55.0))
    r = api.build_crawl(conn, "dev1", CRAWL_CFG, "24h", now=now)
    assert r["available"] is True
    assert r["rh_high"]["v"] == 74.0
    assert r["rh_low"]["v"] == 40.0
    high_ts = datetime.fromisoformat(r["rh_high"]["ts"])
    assert abs((now - high_ts).total_seconds() - 6 * 3600) < 300
    assert 54.0 < r["rh_avg"] < 56.0
    assert r["stale"] is False
    # series: 15-min buckets over 12h -> ~48 buckets, min<=avg<=max everywhere
    assert 40 <= len(r["series"]) <= 50
    for s in r["series"]:
        assert s["rh_min"] <= s["rh_avg"] <= s["rh_max"]
    assert r["thresholds"] == {"watch": 65, "mold": 75}


def test_crawl_hours_above_thresholds_gap_capped(conn):
    now = datetime.now(timezone.utc)
    # 4h of readings: first 2h at 80% (mold), next 1h at 70% (watch), last 1h at 50%
    def rh(i):  # i runs oldest->newest, 3-min steps over 4h = 80 samples
        if i < 40:
            return 80.0
        if i < 60:
            return 70.0
        return 50.0
    _seed_crawl(conn, now, hours=4, rh_fn=rh)
    r = api.build_crawl(conn, "dev1", CRAWL_CFG, "24h", now=now)
    # >75% for ~2h, >65% for ~3h (gap-capped, so within a tick of exact)
    assert 1.8 <= r["hours_above_75"] <= 2.2
    assert 2.8 <= r["hours_above_65"] <= 3.2
    assert r["hours_total"] <= 4.2
    # a reading gap must not be credited: wipe and re-seed with a 2h hole
    conn.execute("TRUNCATE sensor_readings")
    for mins_ago in list(range(240, 180, -3)) + list(range(30, 0, -3)):
        db.insert_sensor_reading(conn, "ecowitt_outdoor",
                                 now - timedelta(minutes=mins_ago),
                                 temp_f=62.0, humidity=80.0)
    r2 = api.build_crawl(conn, "dev1", CRAWL_CFG, "24h", now=now)
    # ~1h of real coverage + one 10-min cap on each island's tail, never ~4h
    assert r2["hours_above_75"] < 1.8


def test_crawl_trend_rising(conn):
    now = datetime.now(timezone.utc)
    # prior 3h window at 50%, recent 3h at 58% -> rising
    _seed_crawl(conn, now, hours=6, rh_fn=lambda i: 50.0 if i < 60 else 58.0)
    r = api.build_crawl(conn, "dev1", CRAWL_CFG, "24h", now=now)
    assert r["trend"] is not None
    assert r["trend"]["dir"] == "rising"
    assert r["trend"]["delta"] > 1


def test_crawl_vent_advice_condensation(conn):
    now = datetime.now(timezone.utc)
    _seed_crawl(conn, now, hours=2, rh_fn=lambda i: 70.0, temp_fn=lambda i: 60.0)
    # outdoor dew point 66F >= crawl temp 60F - 2 -> venting condenses
    db.insert_reading(conn, dict(
        ts=now - timedelta(minutes=2), device_id="dev1",
        indoor_temp_f=72, indoor_humidity=48, heat_setpoint_f=68,
        cool_setpoint_f=72, equipment_status="idle", mode="cool",
        daikin_outdoor_temp_f=85, daikin_outdoor_humidity=60,
        wx_outdoor_temp_f=85, wx_humidity=60, wx_dewpoint_f=66.0,
        wx_solar_wm2=500, wx_uv=5, wx_fc_high_f=90, wx_fc_low_f=60,
        wx_conditions="Clear", wx_aqi=30, wx_alert_count=0, weather_ok=True))
    r = api.build_crawl(conn, "dev1", CRAWL_CFG, "24h", now=now)
    assert r["vent"]["action"] == "keep_closed"
    assert r["outdoor_dp"] == 66.0


def test_crawl_unknown_range_falls_back_to_24h(conn):
    now = datetime.now(timezone.utc)
    _seed_crawl(conn, now, hours=2)
    r = api.build_crawl(conn, "dev1", CRAWL_CFG, "nonsense", now=now)
    assert r["available"] is True
    assert r["range"] == "24h"


# ------------------------------------------------------------------
# moisture case
# ------------------------------------------------------------------

def test_schema_backfills_dewpoint(conn):
    from house_climate.analytics import humidity as hum
    now = datetime.now(timezone.utc)
    # insert WITHOUT dewpoint (as pre-upgrade rows were), then run the schema
    # catch-up and expect the Magnus backfill to fill it identically to Python
    conn.execute(
        "INSERT INTO sensor_readings (ts, sensor_id, temp_f, humidity)"
        " VALUES (%s, 'ecowitt_outdoor', 64.0, 71.0)", (now,))
    db.ensure_app_schema(conn)
    row = conn.execute(
        "SELECT dewpoint_f FROM sensor_readings WHERE sensor_id='ecowitt_outdoor'"
    ).fetchone()
    expected = hum.dew_point_f(64.0, 71.0)
    assert row[0] is not None
    assert abs(row[0] - expected) < 0.01


def test_moisture_unavailable_paths(conn):
    r = api.build_moisture(conn, "dev1", CRAWL_CFG)
    assert r == {"available": False, "reason": "no_data"}
    cfg = dataclasses.replace(CFG, ecowitt=None)
    assert api.build_moisture(conn, "dev1", cfg)["reason"] == "not_configured"


def _seed_moisture(conn, now, days=3):
    """Crawl + downstairs sensors and device readings covering `days` days."""
    from house_climate.analytics import humidity as hum
    n = days * 24 * 4  # 15-min cadence keeps the seed fast
    for i in range(n):
        ts = now - timedelta(minutes=15 * (n - 1 - i))
        crawl_t, crawl_rh = 63.0, 70.0 + (i % 5)
        down_t, down_rh = 71.0, 50.0
        db.insert_sensor_reading(conn, "ecowitt_outdoor", ts, temp_f=crawl_t,
                                 humidity=crawl_rh,
                                 dewpoint_f=hum.dew_point_f(crawl_t, crawl_rh))
        db.insert_sensor_reading(conn, "ecowitt_ch7", ts, temp_f=down_t,
                                 humidity=down_rh,
                                 dewpoint_f=hum.dew_point_f(down_t, down_rh))
        if i % 2 == 0:
            db.insert_reading(conn, dict(
                ts=ts, device_id="dev1", indoor_temp_f=72, indoor_humidity=48,
                heat_setpoint_f=68, cool_setpoint_f=74,
                equipment_status="cooling" if i % 8 == 0 else "idle", mode="cool",
                daikin_outdoor_temp_f=None, daikin_outdoor_humidity=None,
                wx_outdoor_temp_f=80 + (i % 10), wx_humidity=50,
                wx_dewpoint_f=55 + (i % 7), wx_solar_wm2=400, wx_uv=5,
                wx_fc_high_f=90, wx_fc_low_f=60, wx_conditions="Clear",
                wx_aqi=30, wx_alert_count=0, weather_ok=True,
                wx_rain_today_in=0.0))


def test_moisture_payload_shape(conn):
    now = datetime.now(timezone.utc)
    _seed_moisture(conn, now)
    m = api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)
    assert m["available"] is True
    # dew points now: crawl and the Downstairs reference resolved by name
    assert m["dp_now"]["crawl"] is not None
    assert m["dp_now"]["reference_name"] == "Downstairs"
    # crawl 63F/70% holds more absolute moisture than downstairs 71F/50%,
    # but only ~2F of dew point — the whole reason the case uses dew points.
    assert 0 < m["delta"]["now"] < 5
    assert len(m["delta"]["series"]) > 0
    s = m["delta"]["series"][-1]
    assert set(s) == {"ts", "crawl", "indoor", "outdoor", "outdoor_rh", "delta"}
    # thresholds: crawl RH 70-74 -> h60 accumulates, h80 stays 0
    wk = m["thresholds"]["weeks"]
    assert wk and wk[-1]["h60"] > 0 and wk[-1]["h80"] == 0
    # daily table present with rain fields joined
    assert m["daily"] and "rain_in" in m["daily"][-1]
    # young data: attribution + rain + projection all gated, honestly
    assert m["rain"]["ready"] is False
    assert m["projection"]["ready"] is False
    assert isinstance(m["interventions"], list)


def test_interventions_crud_and_report(conn):
    now = datetime.now(timezone.utc)
    _seed_moisture(conn, now, days=2)
    iv_id = db.add_intervention(conn, (now - timedelta(days=1)).date(),
                                "Vapor barrier", "test note")
    m = api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)
    assert len(m["interventions"]) == 1
    iv = m["interventions"][0]
    assert iv["label"] == "Vapor barrier"
    assert iv["overall"] == "collecting"   # 2 days of data can't prove anything
    assert iv["metrics"]["rh_mean"]["verdict"] == "collecting"
    assert db.delete_intervention(conn, iv_id) is True
    assert db.delete_intervention(conn, iv_id) is False


def test_precip_station_beats_openmeteo(conn):
    d = datetime.now(timezone.utc).date()
    db.upsert_precip(conn, d, 0.30, "openmeteo")
    db.upsert_precip(conn, d, 0.42, "station")     # station overwrites gridded
    db.upsert_precip(conn, d, 0.10, "openmeteo")   # gridded must NOT overwrite station
    rows = db.precip_range(conn)
    assert rows == [{"day": d, "inches": 0.42, "source": "station"}]


def test_crawl_csv_export(conn):
    from house_climate.analytics import humidity as hum
    now = datetime.now(timezone.utc)
    db.insert_sensor_reading(conn, "ecowitt_outdoor", now, temp_f=64.0,
                             humidity=71.0, dewpoint_f=hum.dew_point_f(64.0, 71.0))
    csv = api.build_crawl_csv(conn, CRAWL_CFG)
    lines = csv.strip().split("\n")
    assert lines[0] == "ts,temp_f,humidity,dewpoint_f"
    assert len(lines) == 2
    assert ",64.0,71.0," in lines[1]


def test_kv_roundtrip_and_thermal_ha_precool(conn):
    assert db.kv_get(conn, "ha_precool") is None
    now = datetime.now(timezone.utc)
    for i in range(6):
        _health_reading(conn, now - timedelta(minutes=5 * i), mode="cool",
                        indoor=74, status="idle")
    # no push yet -> thermal carries null (chip falls back to heuristic)
    t = api.build_thermal(conn, "dev1", CFG)
    assert t["ha_precool"] is None
    # HA pushes "off" -> thermal reports it as fact
    db.kv_set(conn, "ha_precool", {"enabled": False})
    t = api.build_thermal(conn, "dev1", CFG)
    assert t["ha_precool"]["enabled"] is False
    assert t["ha_precool"]["updated_at"]
    # toggle flips on -> upsert wins
    db.kv_set(conn, "ha_precool", {"enabled": True})
    t = api.build_thermal(conn, "dev1", CFG)
    assert t["ha_precool"]["enabled"] is True


def test_air_roundtrip_latest_wins_and_staleness(conn):
    """Indoor PM2.5 pushed by HA: newest row per room wins, silence past the
    15-minute heartbeat window marks the room stale, empty table is honest."""
    assert api.build_air(conn)["available"] is False
    now = datetime.now(timezone.utc)
    db.insert_air(conn, now - timedelta(minutes=2), "upstairs", 3.0)
    db.insert_air(conn, now - timedelta(minutes=2), "garage", 40.0)
    db.insert_air(conn, now - timedelta(minutes=40), "downstairs", 5.0)
    db.insert_air(conn, now - timedelta(minutes=1), "upstairs", 4.0)
    a = api.build_air(conn, now=now)
    assert a["available"] is True
    rooms = {r["room"]: r for r in a["rooms"]}
    assert rooms["upstairs"]["pm25"] == 4.0
    assert rooms["upstairs"]["stale"] is False
    assert rooms["downstairs"]["stale"] is True
    assert rooms["garage"]["pm25"] == 40.0
    assert a["thresholds"] == {"elevated": 12.0, "bad": 35.0}


def test_air_same_ts_upsert(conn):
    """A re-push at the identical timestamp updates rather than erroring
    (heartbeat + state-change can race onto the same second)."""
    now = datetime.now(timezone.utc)
    db.insert_air(conn, now, "upstairs", 3.0)
    db.insert_air(conn, now, "upstairs", 7.0)
    a = api.build_air(conn, now=now)
    assert [r["pm25"] for r in a["rooms"]] == [7.0]


def test_validate_aqi_accepts_range_and_none():
    """None/absent AQI is allowed (nothing to store); in-range int/float is
    normalized to float."""
    assert api.validate_aqi(None) is None
    assert api.validate_aqi(0) == 0.0
    assert api.validate_aqi(137) == 137.0
    assert api.validate_aqi(1000.0) == 1000.0


def test_validate_aqi_rejects_bool_and_out_of_range():
    """Same validation style as room pm25: bool is not a number, and the
    value must fall within 0-1000."""
    for bad in (True, False, -1, 1001, "137"):
        with pytest.raises(ValueError):
            api.validate_aqi(bad)


def test_pop_and_store_aqi_combined_body_pops_and_stores(conn):
    """This is the exact wiring ha_air_ep runs before its room loop: a body
    carrying BOTH outdoor_aqi and a room key. outdoor_aqi must be popped out
    IN PLACE (so the caller's room loop never treats it as a room) and
    stored under kv key "ha_outdoor_aqi" as {"aqi": float} -- the contract
    Task 4 reads back via db.kv_get."""
    assert db.kv_get(conn, "ha_outdoor_aqi") is None
    body = {"outdoor_aqi": 137.0, "upstairs": 8.0}
    api.pop_and_store_aqi(body, conn)
    assert "outdoor_aqi" not in body
    assert body == {"upstairs": 8.0}
    kv = db.kv_get(conn, "ha_outdoor_aqi")
    assert kv is not None
    assert kv["value"]["aqi"] == 137.0
    assert kv["updated_at"]


def test_pop_and_store_aqi_invalid_raises_and_stores_nothing(conn):
    """A bad outdoor_aqi (bool or out-of-range) raises ValueError -- the
    caller (ha_air_ep) turns that into a 422 -- and nothing is written to
    kv."""
    for bad in (True, 1001):
        body = {"outdoor_aqi": bad, "upstairs": 8.0}
        with pytest.raises(ValueError):
            api.pop_and_store_aqi(body, conn)
        assert db.kv_get(conn, "ha_outdoor_aqi") is None


def test_pop_and_store_aqi_null_or_absent_stores_nothing(conn):
    """Null/absent outdoor_aqi is not an error; it just means nothing to
    store, and rooms are left untouched in the body."""
    body = {"outdoor_aqi": None, "upstairs": 8.0}
    api.pop_and_store_aqi(body, conn)
    assert body == {"upstairs": 8.0}
    assert db.kv_get(conn, "ha_outdoor_aqi") is None

    body2 = {"upstairs": 8.0}
    api.pop_and_store_aqi(body2, conn)
    assert body2 == {"upstairs": 8.0}
    assert db.kv_get(conn, "ha_outdoor_aqi") is None


# ------------------------------------------------ audit regression tests

def test_health_hold_auto_mode_uses_band(conn):
    """Auto mode holds a BAND: a winter house sitting exactly at its heat
    setpoint is a perfect hold, not an 8-degree failure vs the cool setpoint."""
    now = datetime.now(timezone.utc)
    for i in range(10):
        db.insert_reading(conn, dict(
            ts=now - timedelta(minutes=5 * i), device_id="dev1",
            indoor_temp_f=68.0, indoor_humidity=40, heat_setpoint_f=68,
            cool_setpoint_f=76, equipment_status="heating", mode="auto",
            daikin_outdoor_temp_f=40, daikin_outdoor_humidity=70,
            wx_outdoor_temp_f=40, wx_humidity=70, wx_dewpoint_f=35,
            wx_solar_wm2=0, wx_uv=0, wx_fc_high_f=45, wx_fc_low_f=30,
            wx_conditions="Cloudy", wx_aqi=20, wx_alert_count=0,
            weather_ok=True, wx_rain_today_in=0.0))
    h = api.build_health(conn, "dev1", CFG)
    assert h["hold"]["avg_abs_dev"] == 0.0
    assert h["hold"]["pct_within_tol"] == 100.0


def test_cost_summary_not_running_on_stale_data(conn):
    """A poller that died mid-cooling must not leave the ticker accruing:
    running requires a FRESH latest reading (same 600s rule as /api/now)."""
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, dict(
        ts=now - timedelta(hours=3), device_id="dev1",
        indoor_temp_f=75, indoor_humidity=45, heat_setpoint_f=68,
        cool_setpoint_f=72, equipment_status="cooling", mode="cool",
        daikin_outdoor_temp_f=95, daikin_outdoor_humidity=30,
        wx_outdoor_temp_f=95, wx_humidity=30, wx_dewpoint_f=55,
        wx_solar_wm2=800, wx_uv=7, wx_fc_high_f=98, wx_fc_low_f=60,
        wx_conditions="Clear", wx_aqi=30, wx_alert_count=0,
        weather_ok=True, wx_rain_today_in=0.0))
    summary = api.build_cost_summary(conn, "dev1", CFG, now=now)
    assert summary["running"] is False
    assert summary["live_rate_per_hr"] == 0.0


def test_ecowitt_outdoor_battery_only_from_own_entries():
    from house_climate import ecowitt
    # another common_list accessory reports battery=3; the WH32's own entries
    # report battery=0 -> the crawl probe must NOT be flagged low
    data = {"common_list": [
        {"id": "0x02", "val": "63.9", "battery": "0"},
        {"id": "0x07", "val": "71%", "battery": "0"},
        {"id": "0x19", "val": "5.2", "battery": "3"},   # unrelated accessory
    ]}
    o = ecowitt.parse_outdoor(data, "Crawl Space")
    assert o["battery_low"] is False
    # and a genuinely low WH32 still flags
    data2 = {"common_list": [
        {"id": "0x02", "val": "63.9", "battery": "1"},
        {"id": "0x07", "val": "71%", "battery": "1"},
    ]}
    assert ecowitt.parse_outdoor(data2, "Crawl Space")["battery_low"] is True


def test_sensor_daily_stats_trailing_credit_capped_at_elapsed(conn):
    """The newest row must be credited with elapsed-so-far time, never a flat
    600s of future time — 'no invented hours' includes the last row."""
    now = datetime.now(timezone.utc)
    db.insert_sensor_reading(conn, "ecowitt_outdoor", now - timedelta(seconds=60),
                             temp_f=64.0, humidity=82.0, dewpoint_f=58.0)
    stats = db.sensor_daily_stats(conn, "ecowitt_outdoor", CFG.timezone)
    total_h = sum(d["obs_h"] for d in stats)
    assert total_h <= 120 / 3600.0   # ~60s elapsed, generous margin, never 600s


# --- _backup_status: pure, DB-free. Maps (last-success, now, threshold) to the
# dashboard /api/backup payload. No Postgres needed. ---

_T0 = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def test_backup_status_unknown_when_no_heartbeat():
    # No heartbeat recorded yet (fresh deploy before first nightly run): muted
    # 'unknown', NOT a false alarm.
    s = api._backup_status(None, _T0, 108000)
    assert s == {"known": False, "last_success": None, "age_s": None,
                 "stale": False, "threshold_s": 108000}


def test_backup_status_fresh_is_not_stale():
    s = api._backup_status(_T0 - timedelta(hours=1), _T0, 108000)
    assert s["known"] is True and s["age_s"] == 3600 and s["stale"] is False


def test_backup_status_old_is_stale():
    s = api._backup_status(_T0 - timedelta(hours=31), _T0, 108000)  # 111600s > 108000
    assert s["stale"] is True and s["age_s"] == 111600


def test_backup_status_boundary_exactly_threshold_not_stale():
    s = api._backup_status(_T0 - timedelta(seconds=108000), _T0, 108000)
    assert s["age_s"] == 108000 and s["stale"] is False   # stale is age > threshold


def test_backup_status_boundary_one_past_threshold_is_stale():
    s = api._backup_status(_T0 - timedelta(seconds=108001), _T0, 108000)
    assert s["age_s"] == 108001 and s["stale"] is True


def test_build_backup_reads_kv_heartbeat(conn):
    # End-to-end against the kv table the backup script upserts into.
    conn.execute("INSERT INTO kv (k, v, updated_at) VALUES"
                 " ('backup_heartbeat', '{\"dump\": \"climate-2026-08-17.dump\"}'::jsonb, %s)",
                 (_T0 - timedelta(hours=2),))
    s = api.build_backup(conn, now=_T0, stale_s=108000)
    assert s["known"] is True and s["age_s"] == 7200 and s["stale"] is False


def test_build_backup_unknown_when_kv_empty(conn):
    s = api.build_backup(conn, now=_T0, stale_s=108000)
    assert s == {"known": False, "last_success": None, "age_s": None,
                 "stale": False, "threshold_s": 108000}



# --- absolute-humidity gap + transport gain ---------------------------------

@pytest.fixture(autouse=True)
def _clear_ah_cache():
    """The transport-gain fits are cached for half an hour so the dashboard
    poll stays cheap. Tests seed different data under the same device id, so
    the cache has to go between them or the second test reads the first
    one's numbers."""
    api._ah_fit_cache.clear()
    yield
    api._ah_fit_cache.clear()


def test_ah_section_reports_a_gap_per_indoor_channel(conn):
    now = datetime.now(timezone.utc)
    _seed_moisture(conn, now)
    ah = api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)["ah"]
    assert ah["available"] is True
    assert ah["crawl"] == "Crawl Space"
    names = [f["name"] for f in ah["floors"]]
    assert "Downstairs" in names and "Upstairs" in names, \
        "every non-crawl channel gets a gap, not just the reference one"
    down = next(f for f in ah["floors"] if f["name"] == "Downstairs")
    # 63F/70% crawl air carries more water per cubic metre than 71F/50% air.
    assert down["gap_now"] > 0
    assert down["gap_series"] and set(down["gap_series"][0]) == {
        "ts", "crawl", "floor", "gap"}


def test_ah_section_refuses_transport_gain_on_three_days_of_data(conn):
    """Three days cannot support the fit, and the payload must say which gate
    stopped it rather than shipping a number."""
    now = datetime.now(timezone.utc)
    _seed_moisture(conn, now)
    ah = api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)["ah"]
    down = next(f for f in ah["floors"] if f["name"] == "Downstairs")
    assert down["coupling"]["ready"] is False
    assert down["coupling"]["reason"] in (
        "thin_coverage", "outage", "insufficient_n_eff", "no_data", "weak_signal")
    assert "beta" not in down["coupling"] or down["coupling"].get("beta") is None


def test_ah_section_absent_channel_still_listed_without_data(conn):
    """Upstairs is configured but never reported here. It must appear with an
    empty gap rather than vanish or crash."""
    now = datetime.now(timezone.utc)
    _seed_moisture(conn, now)
    ah = api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)["ah"]
    up = next(f for f in ah["floors"] if f["name"] == "Upstairs")
    assert up["gap_now"] is None
    assert up["gap_daily"] == []
    assert up["coupling"]["ready"] is False


def test_crawl_payload_carries_the_dashboard_gap_summary(conn):
    now = datetime.now(timezone.utc)
    _seed_moisture(conn, now)
    c = api.build_crawl(conn, "dev1", CRAWL_CFG, "24h", now=now)
    summary = c["ah_gap"]
    assert summary["available"] is True
    down = next(f for f in summary["floors"] if f["name"] == "Downstairs")
    assert set(down) == {"name", "gap_now", "trend_7d", "coupling_ready",
                         "significant", "beta", "ci95", "reason"}
    assert down["coupling_ready"] is False
    assert down["beta"] is None


def test_dashboard_never_triggers_the_expensive_fit(conn):
    """The dashboard polls every few seconds. It must serve the cheap gap
    numbers and whatever fit is already cached, and never start one itself —
    otherwise one poll in every cache window stalls for over a second."""
    now = datetime.now(timezone.utc)
    _seed_moisture(conn, now)
    api.build_crawl(conn, "dev1", CRAWL_CFG, "24h", now=now)
    assert api._ah_fit_cache == {}, "the dashboard fitted a model"
    api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)
    assert len(api._ah_fit_cache) == 1, "the moisture page should fill the cache"


def test_moisture_page_reuses_a_cached_fit(conn):
    now = datetime.now(timezone.utc)
    _seed_moisture(conn, now)
    api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)
    stamped = list(api._ah_fit_cache.values())[0][0]
    api.build_moisture(conn, "dev1", CRAWL_CFG, now=now + timedelta(minutes=1))
    assert list(api._ah_fit_cache.values())[0][0] == stamped, "refitted inside the cache window"


def test_dashboard_gap_numbers_do_not_need_a_fit(conn):
    """Cold cache: the gap tiles still have real numbers, and the transport
    line simply reports that it is still being worked out."""
    now = datetime.now(timezone.utc)
    _seed_moisture(conn, now)
    summary = api.build_crawl(conn, "dev1", CRAWL_CFG, "24h", now=now)["ah_gap"]
    down = next(f for f in summary["floors"] if f["name"] == "Downstairs")
    assert down["gap_now"] is not None
    assert down["coupling_ready"] is False


def test_ah_section_not_configured_without_indoor_channels(conn):
    now = datetime.now(timezone.utc)
    _seed_moisture(conn, now)
    cfg = dataclasses.replace(CFG, ecowitt={
        "enabled": True, "gateway_url": "http://gw",
        "channels": {}, "outdoor_name": "Crawl Space"})
    ah = api.build_moisture(conn, "dev1", cfg, now=now)["ah"]
    assert ah == {"available": False, "reason": "not_configured"}


def _seed_transport(conn, now, days=32, beta=0.4, lag=2):
    """A house where a KNOWN share of the crawl's dampness reaches upstairs.

    Every API test above seeds three days, which every gate correctly refuses —
    so nothing exercised _build_fits, the stack check, the consistency check or
    the prediction test on the live path. This seeds long enough, and with a
    real signal, that the success path actually runs.
    """
    import math
    import random
    from house_climate.analytics import humidity as hum
    hours = days * 24

    def crawl_ah(i):
        return 11.0 + 2.0 * math.sin(2 * math.pi * i / 60.0)

    # House air is noisy, and its noise REMEMBERS the previous hour — a room
    # does not jump about between readings. Both properties matter here. With
    # no noise at all the residuals are essentially zero and perfectly smooth,
    # which the autocorrelation correction rightly reads as "almost no
    # independent information", and the fit is refused for a reason that says
    # more about the fixture than the code. With pure white noise the
    # correction has nothing to bite on and the effective count equals the raw
    # hour count, which would make the assertion below vacuous.
    rnd = random.Random(17)
    resid, e = [], 0.0
    for _ in range(hours):
        e = 0.7 * e + rnd.gauss(0, 0.10)
        resid.append(e)

    def floor_ah(i):
        return (8.5 + beta * (crawl_ah(i - lag) - 11.0)
                + 0.4 * math.sin(2 * math.pi * i / 24.0) + resid[i])

    def rh_temp_for(ah, temp_f):
        """Pick the RH that puts this sensor at the wanted absolute humidity."""
        lo, hi = 1.0, 99.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if hum.absolute_humidity_gm3(temp_f, mid) < ah:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    for i in range(hours):
        ts = now - timedelta(hours=hours - i)
        for sid, temp, ah in (("ecowitt_outdoor", 62.0, crawl_ah(i)),
                              ("ecowitt_ch7", 71.0, floor_ah(i)),
                              ("ecowitt_ch8", 73.0, floor_ah(i) - 0.3)):
            rh = rh_temp_for(ah, temp)
            db.insert_sensor_reading(conn, sid, ts, temp_f=temp, humidity=rh,
                                     dewpoint_f=hum.dew_point_f(temp, rh))
        db.insert_reading(conn, dict(
            ts=ts, device_id="dev1", indoor_temp_f=72.0, indoor_humidity=48,
            heat_setpoint_f=68, cool_setpoint_f=74,
            equipment_status="cooling" if i % 6 == 0 else "idle", mode="cool",
            daikin_outdoor_temp_f=None, daikin_outdoor_humidity=None,
            wx_outdoor_temp_f=58.0 + 6.0 * math.sin(2 * math.pi * i / 97.0),
            wx_humidity=60, wx_dewpoint_f=48.0 + 3.0 * math.sin(2 * math.pi * i / 83.0),
            wx_solar_wm2=100, wx_uv=1, wx_fc_high_f=70, wx_fc_low_f=50,
            wx_conditions="Clear", wx_aqi=20, wx_alert_count=0, weather_ok=True,
            wx_rain_today_in=0.0))


def test_transport_gain_reaches_the_payload_on_a_real_window(conn):
    """The success path end to end: enough days, a real signal, and the fit,
    stack check and consistency verdict all present in the payload."""
    now = datetime.now(timezone.utc)
    _seed_transport(conn, now)
    ah = api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)["ah"]
    down = next(f for f in ah["floors"] if f["name"] == "Downstairs")
    c = down["coupling"]
    assert c["ready"] is True, c.get("reason")
    # The seed puts a known share of the crawl's swing on this floor, two hours
    # later. Recovering it end to end — through the SQL rollups, the
    # detrending, the time-of-day removal and the lag search — is the point.
    assert abs(c["beta"] - 0.4) <= c["ci95"] + 0.05, c
    assert c["lag"] == 2, c
    assert c["n_eff"] < c["n"] / 2, (
        f"hourly readings counted as near-independent: n={c['n']} n_eff={c['n_eff']}")
    assert c["ci95"] > 0
    assert "stack" in down and "prediction" in down
    assert ah["fit_computed_at"] is not None


def test_floor_order_comes_from_names_and_drives_the_consistency_check(conn):
    """Downstairs must be compared as the lower floor even though it sits on a
    higher channel number than Upstairs in this config.

    Asserting the verdict is 'one of three' would pass with the ordering logic
    deleted, so this pins the ORDER the check actually reasoned over."""
    now = datetime.now(timezone.utc)
    _seed_transport(conn, now)
    ah = api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)["ah"]
    cons = ah["consistency"]
    assert cons["verdict"] != "unknown_order", cons
    assert cons.get("compared") == ["Downstairs", "Upstairs"], cons
    assert cons.get("excluded") == []


def _order(names):
    """Just the ordered floor names, for readability."""
    ordered, _ = api._floors_by_height([(f"ch{i}", n) for i, n in enumerate(names)])
    return None if ordered is None else [n for _, n in ordered]


def _excluded(names):
    return api._floors_by_height([(f"ch{i}", n) for i, n in enumerate(names)])[1]


def test_floors_by_height_reads_the_names_not_the_channel_numbers():
    """Pure: no database needed, so it runs on a bare pytest too."""
    assert _order(["Upstairs", "Downstairs"]) == ["Downstairs", "Upstairs"]
    assert _order(["Main Floor", "Attic", "Basement"]) == ["Basement", "Main Floor", "Attic"]
    # Names that place two sensors on the same level give no order to check.
    assert _order(["Upstairs", "Upper Hall"]) is None


def test_a_sensor_that_is_not_a_floor_does_not_block_the_ones_that_are():
    """Found by running this on a real house: a Garage channel made the
    floor-to-floor check refuse for Upstairs and Downstairs too, even though
    those two are perfectly placeable. A garage has no position in the stack
    of floors above a crawl — it is dropped from the ordering, not treated as
    a reason to give up on it."""
    assert _order(["Upstairs", "Downstairs", "Garage"]) == ["Downstairs", "Upstairs"]
    assert _excluded(["Upstairs", "Downstairs", "Garage"]) == ["Garage"]


def test_one_placeable_floor_is_still_not_an_order():
    """Dropping the unplaceable sensors must not leave a single floor being
    'compared' against nothing."""
    assert _order(["Upstairs", "Garage"]) is None
    assert _order(["Garage", "Shed"]) is None


def test_an_ambiguous_name_is_dropped_without_stopping_the_others():
    """A name naming two levels places the sensor at neither. It leaves the
    ordering rather than refusing it — and is reported as excluded."""
    assert _order(["Upstairs Downstairs", "Attic", "Basement"]) == ["Basement", "Attic"]
    assert _excluded(["Upstairs Downstairs", "Attic", "Basement"]) == ["Upstairs Downstairs"]


def test_floor_words_match_whole_words_only():
    """Substring matching put a cupboard on an upper floor, a playground on the
    ground floor and a maintenance room on the main one — a confidently WRONG
    order, which is worse than no order because it is what makes the check
    name an expensive repair."""
    for impostor in ("Cupboard", "Playground", "Maintenance Room", "Backup Sensor",
                     "Downspout Sensor", "First Aid Room"):
        assert _order([impostor, "Basement"]) is None, impostor
        assert impostor in _excluded([impostor, "Basement"])


def test_an_airstream_or_an_outbuilding_is_never_a_floor():
    """An attic FAN reads hot outdoor-coupled air and would sit at the top of
    the stack — exactly the shape that trips the bypass verdict. An 'Upstairs
    Garage' is a garage."""
    for impostor in ("Attic Fan", "Supply Closet", "Shed Loft", "Upstairs Garage",
                     "Outdoor Sensor", "Back Porch"):
        assert _order([impostor, "Basement"]) is None, impostor


def test_a_real_floor_name_still_places_in_the_right_order():
    """Tightening the matching must not stop ordinary names from working."""
    for lower, upper in (("Basement", "First Floor"),
                         ("First Floor", "Second Floor"),
                         ("Main Bedroom", "Master Bedroom"),
                         ("Second Floor", "Attic"),
                         ("Downstairs", "Upstairs")):
        # Listed upper-first, so a pass cannot come from input order.
        assert _order([upper, lower]) == [lower, upper], (lower, upper)


def test_unnameable_floors_refuse_the_consistency_check(conn):
    """Nothing records how high a sensor sits. If the names do not say, the
    check must refuse rather than trust channel order."""
    now = datetime.now(timezone.utc)
    _seed_transport(conn, now)
    cfg = dataclasses.replace(CFG, ecowitt={
        "enabled": True, "gateway_url": "http://gw",
        "channels": {"8": "Sensor A", "7": "Sensor B"},
        "outdoor_name": "Crawl Space"})
    ah = api.build_moisture(conn, "dev1", cfg, now=now)["ah"]
    assert ah["consistency"]["verdict"] == "unknown_order"


def test_marking_an_intervention_invalidates_the_cached_fit(conn):
    """A fit whose window straddles the work is invalid, so marking new work
    must not keep serving the old one for half an hour."""
    now = datetime.now(timezone.utc)
    _seed_transport(conn, now)
    api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)
    assert len(api._ah_fit_cache) == 1
    db.add_intervention(conn, (now - timedelta(days=10)).date(), "Vapor barrier", None)
    ah = api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)["ah"]
    assert len(api._ah_fit_cache) == 2, "the marker did not change the cache key"
    down = next(f for f in ah["floors"] if f["name"] == "Downstairs")
    assert down["coupling"]["reason"] == "straddles_intervention"


def test_gap_trend_uses_calendar_weeks_not_row_positions():
    """After an outage, fourteen rows can span a month. The arrow must not
    call that 'vs last week'."""
    today = datetime.now(timezone.utc).date()
    fresh = [{"day": today - timedelta(days=i), "gap": 2.0} for i in range(14)]
    assert api._gap_trend(fresh, today) is not None
    stale = [{"day": today - timedelta(days=i * 5), "gap": 2.0} for i in range(14)]
    assert api._gap_trend(stale, today) is None


# --- resolve_outdoor_aqi: the swap that every provenance claim rests on ------
# These run WITHOUT a database (db.kv_get monkeypatched), because the wire-level
# aqi_source assertions elsewhere in this file all take the `conn` fixture and
# therefore skip on a bare pytest run. The rule the whole branch depends on --
# "airnow" means a real monitor, anything else means the weather feed's model
# -- had zero executed Python coverage until these.

def _resolved(monkeypatch, kv, wx_aqi=42, age_s=0):
    now = datetime.now(timezone.utc)
    row = None if kv is None else {"value": kv,
                                   "updated_at": now - timedelta(seconds=age_s)}
    monkeypatch.setattr(api.db, "kv_get", lambda conn, key: row)
    return api.resolve_outdoor_aqi(None, wx_aqi, now=now)


def test_resolve_prefers_a_fresh_monitor_reading(monkeypatch):
    assert _resolved(monkeypatch, {"aqi": 85}, age_s=60) == (85, "airnow")


def test_resolve_falls_back_to_the_model_when_the_monitor_is_stale(monkeypatch):
    """The whole reason provenance has to travel: past the staleness line the
    number silently becomes the weather feed's estimate."""
    assert _resolved(monkeypatch, {"aqi": 85},
                     age_s=api._AIRNOW_STALE_S + 1) == (42, "weather")


def test_resolve_refuses_a_future_stamped_monitor_row(monkeypatch):
    """A clock-skewed or future-stamped row makes `age <= limit` true forever,
    so a wrong value would be trusted as fresh indefinitely."""
    assert _resolved(monkeypatch, {"aqi": 85}, age_s=-3600) == (42, "weather")
    assert _resolved(monkeypatch, {"aqi": 85}, age_s=-6) == (42, "weather")


def test_resolve_accepts_a_row_a_few_milliseconds_in_the_future(monkeypatch):
    """The DB stamps the row and the web app ages it; two clocks a few ms apart
    must not turn a just-written monitor reading into the modeled estimate."""
    assert _resolved(monkeypatch, {"aqi": 85}, age_s=-0.007) == (85, "airnow")
    assert _resolved(monkeypatch, {"aqi": 85}, age_s=-5) == (85, "airnow")


@pytest.mark.parametrize("kv", [None, {}, {"aqi": None}, 85, "85", ["85"]])
def test_resolve_falls_back_on_a_missing_or_malformed_monitor_row(monkeypatch, kv):
    """An HA automation that starts posting a bare number, or null, degrades the
    house to modeled AQI. It must be REPORTED as modeled, not passed off as a
    reading -- that mislabel is the entire bug this branch exists to fix."""
    assert _resolved(monkeypatch, kv) == (42, "weather")


def test_resolve_reports_no_source_when_there_is_no_number_at_all(monkeypatch):
    """A null source must mean "no AQI", never "an AQI we forgot to vouch for"
    -- the JS predicate treats unknown as an estimate on that understanding."""
    assert _resolved(monkeypatch, None, wx_aqi=None) == (None, None)


def test_resolve_logs_when_it_silently_swaps_in_the_model(monkeypatch, caplog):
    """It used to swap in total silence, so a monitor that had been dead for a
    week left no trace anywhere but a two-character marker on a wall kiosk."""
    with caplog.at_level("WARNING", logger="house_climate.api"):
        _resolved(monkeypatch, {"aqi": 85}, age_s=api._AIRNOW_STALE_S + 1)
    assert any("gone quiet" in r.getMessage() for r in caplog.records), caplog.text

    caplog.clear()
    with caplog.at_level("WARNING", logger="house_climate.api"):
        _resolved(monkeypatch, {"aqi": 85}, age_s=60)
    assert not caplog.records, "a healthy monitor read must not log a warning"


# --- local calendar, not UTC (fix: evening windows shifted a day) -----------

EVENING_UTC = datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc)   # 19:00 PDT on the 19th


def test_local_today_is_the_configured_timezone_date():
    assert EVENING_UTC.date().day == 20
    assert api._local_today(EVENING_UTC, CFG) == datetime(2026, 9, 19).date()


def test_gap_trend_windows_use_the_local_date(conn, monkeypatch):
    """After 17:00 Pacific the UTC date is already tomorrow. Handing that to
    _gap_trend moved both seven-day windows a day forward, so 'this week'
    silently lost a day of real data every evening."""
    _seed_moisture(conn, EVENING_UTC, days=1)
    seen = []
    monkeypatch.setattr(api, "_gap_trend", lambda gap_daily, today: seen.append(today))
    api._ah_gap_summary(conn, "dev1", CRAWL_CFG, EVENING_UTC)
    api._build_ah_section(conn, "dev1", CRAWL_CFG, EVENING_UTC, allow_fit=False)
    assert seen and set(seen) == {datetime(2026, 9, 19).date()}


def test_coupling_window_length_counts_local_days(conn, monkeypatch):
    """The fit window is sized from the first LOCAL day of data to LOCAL
    today; using the UTC date added a phantom day every evening."""
    _seed_moisture(conn, EVENING_UTC, days=1)
    first_local = min(d["day"] for d in db.sensor_daily_stats(
        conn, "ecowitt_outdoor", CRAWL_CFG.timezone))
    got = {}

    def fake_window(*a, **k):
        got["days"], got["tz"] = k["days"], k.get("tz")
        return {"ready": False, "reason": "stub"}
    monkeypatch.setattr(api.coupling, "coupling_window", fake_window)
    monkeypatch.setattr(api.coupling, "stack_signature",
                        lambda *a, **k: {"ready": False, "reason": "stub"})
    later = EVENING_UTC + timedelta(days=30)      # 30 days on, still the evening
    api._build_fits(conn, "dev1", CRAWL_CFG, later, "ecowitt_outdoor",
                    [("ecowitt_ch7", "Downstairs")])
    # Data starts 18 Sept local (19:00 PDT); `later` is 19 Oct local but
    # already 20 Oct in UTC.
    assert first_local == datetime(2026, 9, 18).date()
    assert got["days"] == 31                       # the UTC date made it 32
    assert got["tz"] == CRAWL_CFG.timezone



# --- filter reminder by calendar months ---------------------------------------

def _filter_cfg(hours, months):
    return dataclasses.replace(CFG, filter_reminder_hours=hours, filter_reminder_months=months)


def test_add_months_clamps_to_the_month_end():
    from datetime import date
    assert api._add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert api._add_months(date(2026, 6, 26), 6) == date(2026, 12, 26)
    assert api._add_months(date(2027, 8, 31), 6) == date(2028, 2, 29)


def test_filter_due_by_months_not_hours(conn):
    changed = datetime(2026, 6, 26, 19, 0, tzinfo=timezone.utc)
    db.record_filter_change(conn, "dev1", changed_at=changed)
    cfg = _filter_cfg(None, 6)
    before = api.filter_status(conn, "dev1", cfg, rows_all=[],
                               now=datetime(2026, 12, 20, 12, tzinfo=timezone.utc))
    assert before["due"] is False and before["due_on"] == "2026-12-26"
    assert 90 < before["pct"] < 100
    after = api.filter_status(conn, "dev1", cfg, rows_all=[],
                              now=datetime(2026, 12, 27, 12, tzinfo=timezone.utc))
    assert after["due"] is True and after["pct"] == 100


def test_filter_due_when_either_limit_is_reached(conn):
    changed = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    db.record_filter_change(conn, "dev1", changed_at=changed)
    rows = [{"ts": changed + timedelta(minutes=10 * i), "equipment_status": "cooling"}
            for i in range(7)]                                  # one hour of cooling
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    hours_hit = api.filter_status(conn, "dev1", _filter_cfg(1.0, 6), rows_all=rows, now=now)
    assert hours_hit["due"] is True
    neither = api.filter_status(conn, "dev1", _filter_cfg(100.0, 6), rows_all=rows, now=now)
    assert neither["due"] is False


def test_months_only_with_no_logged_change_is_unknown_not_zero(conn):
    conn.execute("DELETE FROM filter_events")
    st = api.filter_status(conn, "dev1", _filter_cfg(None, 6), rows_all=[])
    assert st["pct"] is None and st["due"] is False and st["due_on"] is None
