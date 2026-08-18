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
# fit only if it both spans the day AND has no long interior gap. (Runs locally,
# no Postgres needed.)

def _day_rows(hours, day=10):
    return [{"ts": datetime(2026, 8, day, h, 0, tzinfo=TZ).astimezone(timezone.utc)}
            for h in hours]


def test_day_is_complete_full_day_every_two_hours():
    # 0,2,...,22: spans the day, max interior gap 2h < 3h -> complete.
    assert api._day_is_complete(_day_rows(list(range(0, 24, 2))), TZ) is True


def test_day_is_complete_rejects_short_span():
    # Last reading at 14:00 never reaches ~10pm -> incomplete.
    assert api._day_is_complete(_day_rows([0, 2, 4, 8, 12, 14]), TZ) is False


def test_day_is_complete_rejects_midday_outage():
    # Spans 00:00..22:00 but an 08:00->14:00 hole is a >3h interior gap: the
    # exact partial day the endpoint-only check used to pass as complete.
    assert api._day_is_complete(_day_rows([0, 2, 4, 6, 8, 14, 16, 18, 20, 22]), TZ) is False


def test_day_is_complete_empty_is_false():
    assert api._day_is_complete([], TZ) is False


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
    """Four COMPLETE past local days (hourly 0-23) with varying highs, plus a
    single fresh reading today carrying the forecast high. build_forecast now
    excludes today and partial days from the fit, so the seed must supply
    full days."""
    now_local = datetime.now(TZ)
    highs = [82, 88, 95, 91]
    for day_offset, high in enumerate(reversed(highs), start=1):
        day = (now_local - timedelta(days=day_offset)).date()
        for h in range(24):
            local = datetime(day.year, day.month, day.day, h, 30, tzinfo=TZ)
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


def test_forecast_available(conn):
    _seed_forecast_history(conn)
    fc = api.build_forecast(conn, "dev1", CFG)
    assert fc["available"] is True
    assert fc["predicted_peak_dollars"] >= 0
    assert fc["basis"] in ("linear fit", "historical mean")
    # history = the 4 complete past days only: today's partial day (which
    # would enter the fit as "a cool day needing no cooling") is excluded
    assert fc["days_of_history"] == 4
    # peak-window minutes are a subset of the day
    assert fc["predicted_peak_cool_minutes"] <= fc["predicted_cool_minutes"]


def test_forecast_unavailable_when_empty(conn):
    fc = api.build_forecast(conn, "dev1", CFG)
    assert fc == {"available": False}


def test_humidity_unavailable_when_empty(conn):
    h = api.build_humidity(conn, "dev1", CFG)
    assert h == {"available": False}


def _seed_humidity(conn):
    # 15 cooling + 15 idle readings (>= AC_EFFECT_MIN_SAMPLES each) with a
    # deliberate indoor/outdoor moisture gap so window guidance is non-neutral,
    # plus a mild outdoor temp so the "open" branch is reachable.
    base = datetime.now(timezone.utc) - timedelta(hours=5)
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
    assert h["window"]["action"] in ("open", "keep_closed", "neutral")
    assert isinstance(h["trend"], list) and len(h["trend"]) > 0


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
    _freeze_api_now(monkeypatch, updated_at + timedelta(seconds=api._AIRNOW_STALE_S + 1))

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
    """Insert one cooling/idle reading per hour in `hours` (LOCAL, on 2026-08-<day>)."""
    for i, h in enumerate(hours):
        local = datetime(2026, 8, day, h, 0, tzinfo=TZ)
        db.insert_reading(conn, dict(
            ts=local.astimezone(timezone.utc), device_id="dev1",
            indoor_temp_f=73, indoor_humidity=48, heat_setpoint_f=68,
            cool_setpoint_f=72, equipment_status="cooling" if i % 2 else "idle",
            mode="cool", daikin_outdoor_temp_f=88, daikin_outdoor_humidity=30,
            wx_outdoor_temp_f=88, wx_humidity=30, wx_dewpoint_f=55, wx_solar_wm2=750,
            wx_uv=6, wx_fc_high_f=90, wx_fc_low_f=60, wx_conditions="Clear",
            wx_aqi=30, wx_alert_count=0, weather_ok=True))


FULL_DAY_HOURS = list(range(0, 24, 2))  # 0,2,...,22 -> covers 00-02 through 22-23


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
    # 4 cool-mode readings 6.0F off cool_setpoint_f (outside the 1.0F
    # tolerance) + 4 heat-mode readings 0.5F off heat_setpoint_f (inside
    # tolerance). If the active-setpoint-by-mode selection were wrong (e.g.
    # always comparing against cool_setpoint_f), the heat-mode deviation
    # would compute as |68.5 - 72| = 3.5 instead of |68.5 - 68| = 0.5, and
    # every assertion below would fail.
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(4):
        _health_reading(conn, base + timedelta(minutes=5 * i), mode="cool", cool=72, heat=68, indoor=78)
    for i in range(4):
        _health_reading(conn, base + timedelta(minutes=100 + 5 * i), mode="heat", cool=72, heat=68, indoor=68.5)

    h = api.build_health(conn, "dev1", CFG)
    assert h["hold"]["pct_within_tol"] == 50.0
    assert h["hold"]["avg_abs_dev"] == 3.25
    assert h["hold"]["max_abs_dev"] == 6.0


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
