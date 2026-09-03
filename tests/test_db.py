import math
from datetime import datetime, timezone, timedelta

import pytest
from house_climate import db

def _reading(**over):
    base = dict(ts=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
                device_id="dev1", indoor_temp_f=72.4, indoor_humidity=48.0,
                heat_setpoint_f=68.0, cool_setpoint_f=72.0,
                equipment_status="cooling", mode="cool",
                daikin_outdoor_temp_f=91.0, daikin_outdoor_humidity=30.0,
                wx_outdoor_temp_f=90.5, wx_humidity=31.0, wx_dewpoint_f=56.0,
                wx_solar_wm2=865.0, wx_uv=7.0, wx_fc_high_f=94.0, wx_fc_low_f=58.0,
                wx_conditions="Clear", wx_aqi=37.0, wx_alert_count=0, weather_ok=True)
    base.update(over)
    return base

def test_insert_and_recent(conn):
    db.insert_reading(conn, _reading())
    rows = db.recent_readings(conn, "dev1", datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc))
    assert len(rows) == 1
    assert rows[0]["equipment_status"] == "cooling"

def test_record_error(conn):
    db.record_error(conn, "dev1", "daikin_429", "rate limited")
    got = conn.execute("SELECT kind FROM poll_errors").fetchone()
    assert got[0] == "daikin_429"


def test_filter_change_roundtrip(conn):
    assert db.latest_filter_change(conn, "dev1") is None
    db.record_filter_change(conn, "dev1", changed_at=datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc))
    db.record_filter_change(conn, "dev1")  # now(); must win as "latest"
    latest = db.latest_filter_change(conn, "dev1")
    assert latest is not None
    assert latest > datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    # scoped per device
    assert db.latest_filter_change(conn, "other") is None


def test_continuous_aggregate_buckets_ticks_by_status(conn):
    # The readings_hourly continuous aggregate (db/aggregates.sql) had no test
    # at all: a broken CA definition would ship green because nothing reads it.
    # Insert a mixed-status hour, refresh the CA, and assert the tick bucketing.
    h = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    for i, status in enumerate(["cooling", "cooling", "overcool",
                                "heating", "heating", "fan", "idle"]):
        db.insert_reading(conn, _reading(ts=h + timedelta(minutes=5 * i + 1),
                                         equipment_status=status))
    # refresh_continuous_aggregate needs autocommit (the conn fixture uses it).
    conn.execute("CALL refresh_continuous_aggregate('readings_hourly', NULL, NULL)")
    rows = db.hourly_readings(conn, "dev1", h - timedelta(hours=1))
    assert len(rows) == 1
    b = rows[0]
    assert b["cool_ticks"] == 3      # 2 cooling + 1 overcool
    assert b["heat_ticks"] == 2
    assert b["fan_ticks"] == 1       # idle is not counted


def test_sensor_reading_roundtrip(conn):
    ts = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)
    assert db.latest_sensor_reading(conn, "ecowitt_ch8") is None
    db.insert_sensor_reading(conn, "ecowitt_ch8", ts, temp_f=78.6, humidity=40.0, battery=0.0)
    db.insert_sensor_reading(conn, "ecowitt_ch8",
                             datetime(2026, 8, 11, 20, 3, tzinfo=timezone.utc),
                             temp_f=78.9, humidity=41.0, battery=0.0)
    latest = db.latest_sensor_reading(conn, "ecowitt_ch8")
    assert latest["temp_f"] == 78.9 and latest["humidity"] == 41.0   # newest wins
    assert db.latest_sensor_reading(conn, "ecowitt_ch7") is None      # scoped per sensor


def test_sensor_readings_range_carries_temp_and_dewpoint(conn):
    # The crawl condensation alert reads temp_f AND dewpoint_f off these rows
    # (alerts._condense). If this SELECT ever drops dewpoint_f, _condense
    # silently returns None forever and the alert dies in production with every
    # pure alert test still green — lock the column contract the alert depends on.
    ts = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)
    db.insert_sensor_reading(conn, "ecowitt_crawl", ts,
                             temp_f=60.0, humidity=95.0, dewpoint_f=58.5)
    rows = db.sensor_readings_range(conn, "ecowitt_crawl",
                                    datetime(2026, 8, 12, 0, tzinfo=timezone.utc))
    assert len(rows) == 1
    r = rows[0]
    assert r["temp_f"] == 60.0 and r["humidity"] == 95.0 and r["dewpoint_f"] == 58.5


def test_outdoor_hourly_carries_temp_rh_dp(conn):
    # The /api/outdoor series and the crawl-vs-outdoor attribution both read
    # this one query. It must average outdoor temp, RH and dew point per hour.
    h = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)
    db.insert_reading(conn, _reading(ts=h + timedelta(minutes=5),
                                     wx_outdoor_temp_f=60.0, wx_humidity=80.0, wx_dewpoint_f=54.0))
    db.insert_reading(conn, _reading(ts=h + timedelta(minutes=35),
                                     wx_outdoor_temp_f=64.0, wx_humidity=70.0, wx_dewpoint_f=56.0))
    rows = db.outdoor_hourly(conn, "dev1", datetime(2026, 8, 12, 0, tzinfo=timezone.utc))
    assert len(rows) == 1
    r = rows[0]
    assert r["temp"] == 62.0 and r["rh"] == 75.0 and r["dp"] == 55.0


def test_outdoor_hourly_includes_bucket_when_dewpoint_null_but_temp_present(conn):
    # A partial weather feed (temp/RH but no dew point) must still surface the
    # hour with dp=None, not vanish — otherwise the series hides real coverage.
    h = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
    db.insert_reading(conn, _reading(ts=h + timedelta(minutes=10),
                                     wx_outdoor_temp_f=61.0, wx_humidity=78.0, wx_dewpoint_f=None))
    rows = db.outdoor_hourly(conn, "dev1", datetime(2026, 8, 12, 0, tzinfo=timezone.utc))
    assert len(rows) == 1
    assert rows[0]["temp"] == 61.0 and rows[0]["rh"] == 78.0 and rows[0]["dp"] is None


def test_first_weather_ts_ignores_pre_feed_rows(conn):
    assert db.first_weather_ts(conn, "dev1") is None          # never reported
    old = datetime(2026, 8, 10, 3, 0, tzinfo=timezone.utc)
    # an indoor-only row (no weather) predates the feed and must be ignored
    db.insert_reading(conn, _reading(ts=old, wx_outdoor_temp_f=None,
                                     wx_humidity=None, wx_dewpoint_f=None))
    assert db.first_weather_ts(conn, "dev1") is None          # still no weather
    feed = old + timedelta(hours=10)
    db.insert_reading(conn, _reading(ts=feed + timedelta(hours=2)))
    db.insert_reading(conn, _reading(ts=feed))
    assert db.first_weather_ts(conn, "dev1") == feed          # earliest WEATHER row, not `old`


def test_outdoor_series_bucket_seconds_actually_widens_the_bucket(conn):
    # Two readings in DIFFERENT hours but the SAME 3h window: hourly buckets
    # keep them apart (2 rows), a 3h bucket merges them (1 row). This is what a
    # no-op that ignored bucket_s could NOT satisfy -- it locks the widening.
    day = datetime(2026, 8, 12, tzinfo=timezone.utc)
    db.insert_reading(conn, _reading(ts=day + timedelta(hours=6, minutes=10),
                                     wx_outdoor_temp_f=60.0, wx_humidity=80.0, wx_dewpoint_f=54.0))
    db.insert_reading(conn, _reading(ts=day + timedelta(hours=7, minutes=30),
                                     wx_outdoor_temp_f=64.0, wx_humidity=70.0, wx_dewpoint_f=56.0))
    hourly = db.outdoor_series(conn, "dev1", day, 3600)
    assert len(hourly) == 2                                   # distinct hours
    three_h = db.outdoor_series(conn, "dev1", day, 3 * 3600)
    assert len(three_h) == 1                                  # merged into one 3h bucket
    assert three_h[0]["temp"] == 62.0 and three_h[0]["rh"] == 75.0 and three_h[0]["dp"] == 55.0


def test_outdoor_hourly_includes_bucket_when_only_rh_present(conn):
    # Exercises the wx_humidity arm of the widened OR independently: an
    # RH-only feed still surfaces the hour, with temp and dp null.
    h = datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc)
    db.insert_reading(conn, _reading(ts=h + timedelta(minutes=10),
                                     wx_outdoor_temp_f=None, wx_humidity=82.0, wx_dewpoint_f=None))
    rows = db.outdoor_hourly(conn, "dev1", datetime(2026, 8, 12, 0, tzinfo=timezone.utc))
    assert len(rows) == 1
    assert rows[0]["rh"] == 82.0 and rows[0]["temp"] is None and rows[0]["dp"] is None


def test_outdoor_hourly_orders_buckets_ascending(conn):
    # Multi-bucket: the /api/outdoor series depends on ascending bucket order.
    for hr in (5, 3, 7):
        db.insert_reading(conn, _reading(
            ts=datetime(2026, 8, 12, hr, 15, tzinfo=timezone.utc),
            wx_outdoor_temp_f=60.0 + hr))
    rows = db.outdoor_hourly(conn, "dev1", datetime(2026, 8, 12, 0, tzinfo=timezone.utc))
    buckets = [r["bucket"] for r in rows]
    assert len(rows) == 3 and buckets == sorted(buckets)


def test_outdoor_hourly_skips_fully_null_weather_hour(conn):
    # A feed outage (all wx fields null) contributes no bucket, so coverage can
    # honestly report the gap instead of a phantom all-null row.
    h = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)
    db.insert_reading(conn, _reading(ts=h + timedelta(minutes=10),
                                     wx_outdoor_temp_f=None, wx_humidity=None, wx_dewpoint_f=None))
    rows = db.outdoor_hourly(conn, "dev1", datetime(2026, 8, 12, 0, tzinfo=timezone.utc))
    assert rows == []


def test_ensure_app_schema_heals_missing_continuous_aggregate(conn):
    """Issue #9: aggregates.sql only ran on a fresh volume, so a new/changed
    aggregate never reached existing databases. ensure_app_schema must now
    (re)create it idempotently. Simulate a DB predating the aggregate by
    dropping it, then prove ensure_app_schema brings it back."""
    conn.execute("DROP MATERIALIZED VIEW IF EXISTS readings_hourly CASCADE")
    gone = conn.execute(
        "SELECT count(*) FROM timescaledb_information.continuous_aggregates"
        " WHERE view_name = 'readings_hourly'").fetchone()[0]
    assert gone == 0

    db.ensure_app_schema(conn)

    back = conn.execute(
        "SELECT count(*) FROM timescaledb_information.continuous_aggregates"
        " WHERE view_name = 'readings_hourly'").fetchone()[0]
    assert back == 1
    # And it's usable (the compression policy re-applied without error too).
    db.hourly_readings(conn, "dev1", datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc))


# --- absolute humidity in SQL ------------------------------------------------

def test_ah_sql_formula_matches_python_without_a_database():
    """Evaluate the SQL expression as arithmetic and compare it to the Python
    conversion, reading by reading.

    This is the one test of the SQL formula that runs on a bare `pytest` with
    no Postgres — every other test of it skips locally. A typo in the
    expression would otherwise reach the page as plausible wrong numbers and
    only be caught when CI ran."""
    from house_climate.analytics import humidity as hum
    expr = db._AH_SENSOR_SQL.replace("exp(", "math.exp(")
    for temp_f in (20.0, 45.0, 63.0, 78.0, 95.0):
        for dp_f in (10.0, 40.0, 55.0, 70.0):
            if dp_f > temp_f:
                continue
            got = eval(expr, {"math": math},
                       {"temp_f": temp_f, "dewpoint_f": dp_f})
            want = hum.absolute_humidity_from_dew_point_gm3(temp_f, dp_f)
            assert got == pytest.approx(want, abs=1e-9), (temp_f, dp_f)


def test_ah_sql_uses_the_shared_constants_not_a_forked_copy():
    """If someone corrects the Magnus constants in analytics/humidity, the SQL
    must move with them — the rollups and every transport fit read the SQL
    path, so a fork would be wrong where it matters and green everywhere it
    is tested."""
    from house_climate.analytics import humidity as hum
    for value in (hum._MAGNUS_A, hum._MAGNUS_B, hum._SAT_VP_HPA_0C,
                  hum._VAPOR_DENSITY_K):
        assert str(value) in db._AH_SENSOR_SQL
        assert str(value) in db._AH_WX_SQL


def test_sensor_hourly_ah_matches_the_python_conversion(conn):
    """The SQL rollups and the live tiles must agree about the same reading.
    They take different routes to absolute humidity — SQL from stored temp +
    dew point, Python from temp + RH — so this pins them together."""
    from house_climate.analytics import humidity as hum
    ts = datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc)
    temp, rh = 63.0, 88.0
    dp = hum.dew_point_f(temp, rh)
    db.insert_sensor_reading(conn, "ecowitt_crawl", ts, temp_f=temp, humidity=rh,
                             dewpoint_f=dp)
    rows = db.sensor_hourly_ah(conn, "ecowitt_crawl",
                               datetime(2026, 8, 20, 0, tzinfo=timezone.utc))
    assert len(rows) == 1
    assert rows[0]["ah"] == pytest.approx(hum.absolute_humidity_gm3(temp, rh), abs=0.01)
    assert rows[0]["rh_max"] == rh


def test_sensor_hourly_ah_skips_rows_without_a_dew_point(conn):
    ts = datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc)
    db.insert_sensor_reading(conn, "ecowitt_crawl", ts, temp_f=63.0, humidity=88.0,
                             dewpoint_f=None)
    rows = db.sensor_hourly_ah(conn, "ecowitt_crawl",
                               datetime(2026, 8, 20, 0, tzinfo=timezone.utc))
    assert rows == []


def test_daily_absolute_humidity_averages_readings_not_temperatures(conn):
    """Absolute humidity does not move in a straight line with temperature, so
    a day's figure has to be worked out reading by reading and averaged
    afterwards. Computing it from the day's average temperature and average dew
    point instead gives a different — wrong — answer, and this pins the
    difference so the cheaper shortcut can never quietly replace it."""
    from house_climate.analytics import humidity as hum
    day = datetime(2026, 8, 21, tzinfo=timezone.utc)
    pairs = [(45.0, 44.0), (95.0, 60.0)]     # a cold damp hour and a hot one
    for i, (t, dp) in enumerate(pairs):
        db.insert_sensor_reading(conn, "ecowitt_crawl", day + timedelta(hours=i),
                                 temp_f=t, humidity=80.0, dewpoint_f=dp)
    daily = db.sensor_daily_stats(conn, "ecowitt_crawl", "UTC",
                                  since_ts=day - timedelta(hours=1))
    row = next(d for d in daily if d["ah_mean"] is not None)
    per_reading = sum(hum.absolute_humidity_from_dew_point_gm3(t, dp)
                      for t, dp in pairs) / len(pairs)
    shortcut = hum.absolute_humidity_from_dew_point_gm3(
        sum(t for t, _ in pairs) / 2, sum(dp for _, dp in pairs) / 2)
    assert row["ah_mean"] == pytest.approx(per_reading, abs=0.02)
    assert abs(per_reading - shortcut) > 0.1, \
        "fixture too mild to catch the shortcut — widen the temperature spread"


def test_indoor_hourly_reports_temperature_difference_and_blower_duty(conn):
    """Both feed the stack-effect check: the temperature difference drives air
    up through the building, and blower duty separates duct-driven movement
    from it."""
    base = datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc)
    for i, status in enumerate(["cooling", "idle", "idle", "fan"]):
        db.insert_reading(conn, dict(
            ts=base + timedelta(minutes=10 * i), device_id="dev1",
            indoor_temp_f=72.0, indoor_humidity=48, heat_setpoint_f=68,
            cool_setpoint_f=74, equipment_status=status, mode="cool",
            daikin_outdoor_temp_f=None, daikin_outdoor_humidity=None,
            wx_outdoor_temp_f=52.0, wx_humidity=60, wx_dewpoint_f=45,
            wx_solar_wm2=100, wx_uv=1, wx_fc_high_f=70, wx_fc_low_f=50,
            wx_conditions="Clear", wx_aqi=20, wx_alert_count=0, weather_ok=True,
            wx_rain_today_in=0.0))
    rows = db.indoor_hourly(conn, "dev1", base - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0]["dt"] == pytest.approx(20.0)     # 72F inside, 52F outside
    assert rows[0]["duty"] == pytest.approx(0.5)    # cooling + fan out of four


def test_indoor_hourly_skips_hours_without_both_temperatures(conn):
    """An hour with no outdoor reading has no temperature difference, and must
    be absent rather than reported as zero."""
    ts = datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc)
    db.insert_reading(conn, dict(
        ts=ts, device_id="dev1", indoor_temp_f=72.0, indoor_humidity=48,
        heat_setpoint_f=68, cool_setpoint_f=74, equipment_status="idle",
        mode="cool", daikin_outdoor_temp_f=None, daikin_outdoor_humidity=None,
        wx_outdoor_temp_f=None, wx_humidity=None, wx_dewpoint_f=None,
        wx_solar_wm2=None, wx_uv=None, wx_fc_high_f=None, wx_fc_low_f=None,
        wx_conditions=None, wx_aqi=None, wx_alert_count=0, weather_ok=False,
        wx_rain_today_in=None))
    assert db.indoor_hourly(conn, "dev1", ts - timedelta(hours=1)) == []


def test_outdoor_daily_and_series_carry_absolute_humidity(conn):
    from house_climate.analytics import humidity as hum
    ts = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
    db.insert_reading(conn, dict(
        ts=ts, device_id="dev1", indoor_temp_f=72.0, indoor_humidity=48,
        heat_setpoint_f=68, cool_setpoint_f=74, equipment_status="idle",
        mode="cool", daikin_outdoor_temp_f=None, daikin_outdoor_humidity=None,
        wx_outdoor_temp_f=80.0, wx_humidity=55, wx_dewpoint_f=62.0,
        wx_solar_wm2=400, wx_uv=5, wx_fc_high_f=90, wx_fc_low_f=60,
        wx_conditions="Clear", wx_aqi=30, wx_alert_count=0, weather_ok=True,
        wx_rain_today_in=0.0))
    expected = hum.absolute_humidity_from_dew_point_gm3(80.0, 62.0)
    daily = db.outdoor_daily(conn, "dev1", "UTC", since_ts=ts - timedelta(hours=1))
    assert daily[0]["ah_mean"] == pytest.approx(expected, abs=0.01)
    series = db.outdoor_series(conn, "dev1", ts - timedelta(hours=1), 3600)
    assert series[0]["ah"] == pytest.approx(expected, abs=0.01)
