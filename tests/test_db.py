from datetime import datetime, timezone
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
