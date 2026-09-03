import json
import psycopg

READING_COLUMNS = [
    "ts", "device_id", "indoor_temp_f", "indoor_humidity", "heat_setpoint_f",
    "cool_setpoint_f", "equipment_status", "mode", "daikin_outdoor_temp_f",
    "daikin_outdoor_humidity", "wx_outdoor_temp_f", "wx_humidity", "wx_dewpoint_f",
    "wx_solar_wm2", "wx_uv", "wx_fc_high_f", "wx_fc_low_f", "wx_conditions",
    "wx_aqi", "wx_alert_count", "weather_ok", "wx_rain_today_in",
]

# Magnus formula (Alduchov & Eskridge 1996), same constants as
# analytics/humidity.py dew_point_f — this SQL fragment exists so the one-time
# dewpoint backfill in ensure_app_schema computes the exact same numbers the
# poller stores going forward.
_MAGNUS_SQL = (
    "(243.12 * (ln(humidity/100.0) + 17.62*((temp_f-32.0)/1.8)/(243.12+(temp_f-32.0)/1.8))"
    " / (17.62 - (ln(humidity/100.0) + 17.62*((temp_f-32.0)/1.8)/(243.12+(temp_f-32.0)/1.8))))"
    " * 1.8 + 32.0"
)


# Absolute humidity (g/m^3) from stored temp_f + dewpoint_f, matching
# analytics/humidity.absolute_humidity_from_dew_point_gm3 exactly. It lives in
# SQL because AH is NONLINEAR in temperature: averaging a day's AH readings is
# not the same as computing AH from that day's mean temp and mean dew point,
# so the rollup converts per row and averages afterwards. 216.7 is
# 100 * M_water / R; 6.112 hPa is saturation vapour pressure at 0 C.
def _ah_sql(temp_col: str, dp_col: str) -> str:
    tc = f"(({temp_col}-32.0)/1.8)"
    dc = f"(({dp_col}-32.0)/1.8)"
    return (f"(216.7 * (6.112 * exp(17.62*{dc}/(243.12+{dc})))"
            f" / ({tc} + 273.15))")


_AH_SENSOR_SQL = _ah_sql("temp_f", "dewpoint_f")
_AH_WX_SQL = _ah_sql("wx_outdoor_temp_f", "wx_dewpoint_f")


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True)


def ensure_app_schema(conn) -> None:
    """Create tables that aren't in the initdb bootstrap (which only runs on a
    fresh volume). Idempotent, so it is safe to call on every web startup and
    brings an already-provisioned database up to date without a migration tool.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS filter_events (
               id         serial PRIMARY KEY,
               device_id  text NOT NULL,
               changed_at timestamptz NOT NULL DEFAULT now(),
               note       text
           )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS filter_events_device_ts"
        " ON filter_events (device_id, changed_at DESC)")
    # --- moisture-case additions (2026-08-12) ---
    # Station rain gauge snapshot (wx.json rainToday, inches, resets midnight).
    conn.execute(
        "ALTER TABLE readings ADD COLUMN IF NOT EXISTS wx_rain_today_in double precision")
    # Dew point as first-class stored data for every auxiliary sensor. The
    # poller computes it on insert; this backfills every pre-existing row once
    # (idempotent: only touches rows where it is still NULL).
    conn.execute(
        "ALTER TABLE sensor_readings ADD COLUMN IF NOT EXISTS dewpoint_f double precision")
    conn.execute(
        f"UPDATE sensor_readings SET dewpoint_f = {_MAGNUS_SQL}"
        " WHERE dewpoint_f IS NULL AND humidity > 0 AND temp_f IS NOT NULL")
    # One row per local calendar day of rainfall. source: 'station' (the
    # house's own gauge via wx.json — authoritative) or 'openmeteo' (gridded
    # backfill for days before station capture began).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS precip_daily (
               day        date PRIMARY KEY,
               inches     double precision NOT NULL,
               source     text NOT NULL,
               updated_at timestamptz NOT NULL DEFAULT now()
           )""")
    # Intervention markers: "vapor barrier installed", etc. Each freezes the
    # preceding period as a labeled baseline for before/after comparison.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS interventions (
               id         serial PRIMARY KEY,
               marked_on  date NOT NULL,
               label      text NOT NULL,
               note       text,
               created_at timestamptz NOT NULL DEFAULT now()
           )""")
    # Tiny durable key/value store for state pushed from outside (e.g. Home
    # Assistant's pre-cool toggle), so it survives web restarts.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS kv (
               k          text PRIMARY KEY,
               v          jsonb NOT NULL,
               updated_at timestamptz NOT NULL DEFAULT now()
           )""")
    # Indoor PM2.5 pushed by Home Assistant from the Levoit purifiers (the
    # only indoor particulate sensors in the house until the WH45 lands).
    # Plain table, not a hypertable: <3k rows/day at the worst push cadence.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS air_readings (
               ts    timestamptz NOT NULL,
               room  text NOT NULL,
               pm25  real NOT NULL,
               PRIMARY KEY (ts, room)
           )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS air_readings_room_ts"
        " ON air_readings (room, ts DESC)")
    ensure_aggregates(conn)


# The continuous aggregate + policies. This MIRRORS db/aggregates.sql, which the
# initdb bootstrap runs on a FRESH volume only. Re-applying the same idempotent
# DDL here (every startup) (re)creates a MISSING aggregate/policy on an
# already-provisioned database — previously aggregates.sql ran once at volume
# creation and never again, so a newly-added aggregate never reached existing
# DBs. Every statement is guarded (IF NOT EXISTS / if_not_exists / ALTER SET), so
# it's a no-op on an up-to-date DB. NOTE: IF-NOT-EXISTS only heals what's absent;
# CHANGING an existing aggregate's definition or a policy still needs a real
# migration (drop + recreate). Keep the two in sync; the test suite asserts the
# aggregate is recreated after ensure_app_schema runs on a DB missing it.
def ensure_aggregates(conn) -> None:
    conn.execute(
        """CREATE MATERIALIZED VIEW IF NOT EXISTS readings_hourly
           WITH (timescaledb.continuous) AS
           SELECT time_bucket('1 hour', ts) AS bucket,
                  device_id,
                  avg(indoor_temp_f)  AS avg_indoor_temp_f,
                  min(indoor_temp_f)  AS min_indoor_temp_f,
                  max(indoor_temp_f)  AS max_indoor_temp_f,
                  avg(indoor_humidity) AS avg_indoor_humidity,
                  avg(wx_outdoor_temp_f) AS avg_outdoor_temp_f,
                  avg(wx_solar_wm2)   AS avg_solar_wm2,
                  count(*) FILTER (WHERE equipment_status IN ('cooling','overcool')) AS cool_ticks,
                  count(*) FILTER (WHERE equipment_status = 'heating') AS heat_ticks,
                  count(*) FILTER (WHERE equipment_status = 'fan') AS fan_ticks
           FROM readings
           GROUP BY bucket, device_id
           WITH NO DATA""")
    conn.execute(
        "SELECT add_continuous_aggregate_policy('readings_hourly',"
        " start_offset => INTERVAL '3 hours', end_offset => INTERVAL '1 hour',"
        " schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE)")
    conn.execute(
        "ALTER TABLE readings SET (timescaledb.compress,"
        " timescaledb.compress_segmentby = 'device_id')")
    conn.execute(
        "SELECT add_compression_policy('readings', INTERVAL '30 days', if_not_exists => TRUE)")


def record_filter_change(conn, device_id, changed_at=None, note=None):
    """Log a 'filter was changed' event. changed_at defaults to now(); pass an
    explicit timestamp to backfill a change that happened before tracking."""
    if changed_at is None:
        cur = conn.execute(
            "INSERT INTO filter_events (device_id, note) VALUES (%s, %s)"
            " RETURNING changed_at", (device_id, note))
    else:
        cur = conn.execute(
            "INSERT INTO filter_events (device_id, changed_at, note)"
            " VALUES (%s, %s, %s) RETURNING changed_at",
            (device_id, changed_at, note))
    return cur.fetchone()[0]


def latest_filter_change(conn, device_id):
    """Most recent filter-change timestamp for a device, or None if never
    logged."""
    cur = conn.execute(
        "SELECT max(changed_at) FROM filter_events WHERE device_id=%s",
        (device_id,))
    row = cur.fetchone()
    return row[0] if row else None


def insert_reading(conn, reading: dict) -> None:
    cols = ", ".join(READING_COLUMNS)
    ph = ", ".join(["%s"] * len(READING_COLUMNS))
    vals = [reading.get(c) for c in READING_COLUMNS]
    conn.execute(f"INSERT INTO readings ({cols}) VALUES ({ph})", vals)


def record_error(conn, device_id, kind, detail) -> None:
    conn.execute(
        "INSERT INTO poll_errors (device_id, kind, detail) VALUES (%s, %s, %s)",
        (device_id, kind, detail))


def upsert_device(conn, device_id, name, model) -> None:
    conn.execute(
        """INSERT INTO devices (device_id, name, model, last_seen)
           VALUES (%s, %s, %s, now())
           ON CONFLICT (device_id)
           DO UPDATE SET name=EXCLUDED.name, model=EXCLUDED.model, last_seen=now()""",
        (device_id, name, model))


def recent_readings(conn, device_id, since_ts) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM readings WHERE device_id=%s AND ts >= %s ORDER BY ts",
        (device_id, since_ts))
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def latest_device_id(conn):
    cur = conn.execute("SELECT device_id FROM devices ORDER BY last_seen DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else None


def insert_sensor_reading(conn, sensor_id, ts, temp_f=None, humidity=None,
                          battery=None, extra=None, dewpoint_f=None) -> None:
    """Write one auxiliary-sensor reading (Ecowitt room sensor, etc.) into the
    generic sensor_readings table. dewpoint_f is stored first-class (computed
    by the caller via humidity.dew_point_f) — RH alone misleads across
    different temperatures, and the moisture case is built on dew points."""
    conn.execute(
        "INSERT INTO sensor_readings (ts, sensor_id, temp_f, humidity, battery, extra, dewpoint_f)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (ts, sensor_id, temp_f, humidity, battery,
         json.dumps(extra) if extra is not None else None, dewpoint_f))


def latest_sensor_reading(conn, sensor_id):
    """Most recent reading for a sensor_id, or None if it has never reported."""
    cur = conn.execute(
        "SELECT ts, temp_f, humidity, battery, extra FROM sensor_readings"
        " WHERE sensor_id=%s ORDER BY ts DESC LIMIT 1",
        (sensor_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return {"ts": row[0], "temp_f": row[1], "humidity": row[2],
            "battery": row[3], "extra": row[4]}


def sensor_readings_range(conn, sensor_id, since_ts) -> list[dict]:
    """Raw rows for one auxiliary sensor since a timestamp, oldest first.
    Used for exact stats (true high/low with timestamps, time above a
    threshold) that bucketed data would blur."""
    cur = conn.execute(
        "SELECT ts, temp_f, humidity, dewpoint_f FROM sensor_readings"
        " WHERE sensor_id=%s AND ts >= %s ORDER BY ts",
        (sensor_id, since_ts))
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def sensor_series(conn, sensor_id, since_ts, bucket_s) -> list[dict]:
    """Bucketed humidity/temperature/dew-point series for one auxiliary
    sensor. time_bucket keeps the payload small at any range, while
    per-bucket min/max preserve the true extremes a plain average would
    shave off — the crawl chart exists to show those peaks."""
    cur = conn.execute(
        "SELECT time_bucket(%s * interval '1 second', ts) AS bucket,"
        " avg(humidity) AS rh_avg, min(humidity) AS rh_min, max(humidity) AS rh_max,"
        " avg(temp_f) AS temp_avg, avg(dewpoint_f) AS dp_avg"
        " FROM sensor_readings WHERE sensor_id=%s AND ts >= %s"
        " GROUP BY bucket ORDER BY bucket",
        (bucket_s, sensor_id, since_ts))
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def sensor_hourly_dp(conn, sensor_id, since_ts) -> list[dict]:
    """Hourly mean dew point for one sensor — the unit both correlation
    windows (source attribution) operate on."""
    cur = conn.execute(
        "SELECT time_bucket(interval '1 hour', ts) AS bucket,"
        " avg(dewpoint_f) AS dp"
        " FROM sensor_readings WHERE sensor_id=%s AND ts >= %s"
        "   AND dewpoint_f IS NOT NULL"
        " GROUP BY bucket ORDER BY bucket",
        (sensor_id, since_ts))
    return [{"bucket": r[0], "dp": r[1]} for r in cur.fetchall()]


def sensor_hourly_ah(conn, sensor_id, since_ts) -> list[dict]:
    """Hourly mean absolute humidity (g/m^3) for one sensor — the unit the
    crawl-to-floor gap and the coupling windows operate on. Rows missing
    either temperature or dew point are excluded rather than averaged as
    nulls, so a bucket's AH always reflects complete readings."""
    cur = conn.execute(
        f"SELECT time_bucket(interval '1 hour', ts) AS bucket,"
        f" avg({_AH_SENSOR_SQL}) AS ah, avg(temp_f) AS temp"
        " FROM sensor_readings WHERE sensor_id=%s AND ts >= %s"
        "   AND dewpoint_f IS NOT NULL AND temp_f IS NOT NULL"
        " GROUP BY bucket ORDER BY bucket",
        (sensor_id, since_ts))
    return [{"bucket": r[0], "ah": r[1], "temp": r[2]} for r in cur.fetchall()]


def outdoor_series(conn, device_id, since_ts, bucket_s) -> list[dict]:
    """Bucketed mean OUTDOOR temp / RH / dew point from the weather feed on the
    readings table. A bucket appears when the feed reported ANY of the three in
    it; a field is null when the feed lacked it (avg() skips nulls). `bucket_s`
    sizes the bucket so the /api/outdoor chart stays bounded at any range, the
    same way sensor_series does for the crawl chart."""
    cur = conn.execute(
        "SELECT time_bucket(%s * interval '1 second', ts) AS bucket,"
        " avg(wx_outdoor_temp_f) AS temp, avg(wx_humidity) AS rh,"
        " avg(wx_dewpoint_f) AS dp,"
        f" avg({_AH_WX_SQL}) FILTER (WHERE wx_outdoor_temp_f IS NOT NULL"
        "   AND wx_dewpoint_f IS NOT NULL) AS ah"
        " FROM readings WHERE device_id=%s AND ts >= %s"
        "   AND (wx_outdoor_temp_f IS NOT NULL OR wx_humidity IS NOT NULL"
        "        OR wx_dewpoint_f IS NOT NULL)"
        " GROUP BY bucket ORDER BY bucket",
        (bucket_s, device_id, since_ts))
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def outdoor_hourly(conn, device_id, since_ts) -> list[dict]:
    """Hourly outdoor temp / RH / dew point — the fixed-hour unit the
    crawl-vs-outdoor attribution (which reads `dp`) and the /api/outdoor
    coverage counter both work in, so those never drift from the widened
    query. The display series uses outdoor_series with a range-sized bucket."""
    return outdoor_series(conn, device_id, since_ts, 3600)


def first_weather_ts(conn, device_id):
    """Timestamp of a device's earliest reading that CARRIED weather — when the
    weather feed began producing data — or None if it never has. /api/outdoor's
    data_start uses this (not the device's first reading of any kind) so that a
    low coverage reads correctly: a young feed when data_start sits inside the
    window, real gaps when it predates it. An old device that only recently
    gained a weather feed must not look like a gappy one — hence the wx filter,
    which matches outdoor_series' own 'any wx field present' rule."""
    row = conn.execute(
        "SELECT min(ts) FROM readings WHERE device_id=%s"
        "   AND (wx_outdoor_temp_f IS NOT NULL OR wx_humidity IS NOT NULL"
        "        OR wx_dewpoint_f IS NOT NULL)", (device_id,)).fetchone()
    return row[0]


def sensor_daily_stats(conn, sensor_id, tz, since_ts=None) -> list[dict]:
    """Per-local-day stats for one sensor: RH and dew-point min/max/mean plus
    gap-capped hours above the 60/70/80 %RH thresholds and hours of
    condensation risk (air-to-dew-point spread under 3°F). The 600s gap cap
    matches the rest of the stack: an outage becomes uncounted time, never
    invented hours."""
    since_clause = "AND ts >= %(since)s" if since_ts is not None else ""
    cur = conn.execute(
        f"""WITH t AS (
              SELECT ts, humidity, temp_f, dewpoint_f,
                     least(coalesce(extract(epoch FROM
                       lead(ts) OVER (ORDER BY ts) - ts),
                       extract(epoch FROM (now() - ts))), 600) AS dt,
                     (ts AT TIME ZONE %(tz)s)::date AS day
              FROM sensor_readings
              WHERE sensor_id=%(sid)s {since_clause})
            SELECT day,
              min(humidity) AS rh_min, max(humidity) AS rh_max, avg(humidity) AS rh_mean,
              min(dewpoint_f) AS dp_min, max(dewpoint_f) AS dp_max, avg(dewpoint_f) AS dp_mean,
              avg(temp_f) AS temp_mean,
              avg({_AH_SENSOR_SQL}) AS ah_mean,
              (coalesce(sum(dt) FILTER (WHERE humidity > 60), 0)/3600.0)::float AS h60,
              (coalesce(sum(dt) FILTER (WHERE humidity > 70), 0)/3600.0)::float AS h70,
              (coalesce(sum(dt) FILTER (WHERE humidity > 80), 0)/3600.0)::float AS h80,
              (coalesce(sum(dt) FILTER (WHERE temp_f IS NOT NULL
                AND dewpoint_f IS NOT NULL AND temp_f - dewpoint_f < 3), 0)/3600.0)::float AS cond_h,
              (sum(dt)/3600.0)::float AS obs_h,
              count(*) AS n
            FROM t GROUP BY day ORDER BY day""",
        {"tz": tz, "sid": sensor_id, "since": since_ts})
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def outdoor_daily(conn, device_id, tz, since_ts=None) -> list[dict]:
    """Per-local-day outdoor conditions from the weather feed: mean temp and
    dew point, station rain-gauge total (max of the cumulative midnight-reset
    counter), and gap-capped cooling hours (for the duct-sweat proxy)."""
    since_clause = "AND ts >= %(since)s" if since_ts is not None else ""
    cur = conn.execute(
        f"""WITH t AS (
              SELECT ts, wx_outdoor_temp_f, wx_dewpoint_f, wx_rain_today_in,
                     equipment_status,
                     least(coalesce(extract(epoch FROM
                       lead(ts) OVER (ORDER BY ts) - ts),
                       extract(epoch FROM (now() - ts))), 600) AS dt,
                     (ts AT TIME ZONE %(tz)s)::date AS day
              FROM readings
              WHERE device_id=%(dev)s {since_clause})
            SELECT day,
              avg(wx_outdoor_temp_f) AS temp_mean,
              avg(wx_dewpoint_f) AS dp_mean,
              avg({_AH_WX_SQL}) AS ah_mean,
              max(wx_rain_today_in) AS rain_in,
              max(extract(hour FROM (ts AT TIME ZONE %(tz)s)))::int AS last_hour,
              (coalesce(sum(dt) FILTER (WHERE equipment_status IN
                ('cooling', 'overcool')), 0)/3600.0)::float AS cooling_h
            FROM t GROUP BY day ORDER BY day""",
        {"tz": tz, "dev": device_id, "since": since_ts})
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def upsert_precip(conn, day, inches, source) -> None:
    """Insert or update one day's rainfall. The station gauge always wins over
    a gridded backfill: an 'openmeteo' row never overwrites a 'station' row."""
    conn.execute(
        """INSERT INTO precip_daily (day, inches, source, updated_at)
           VALUES (%s, %s, %s, now())
           ON CONFLICT (day) DO UPDATE
             SET inches=EXCLUDED.inches, source=EXCLUDED.source, updated_at=now()
             WHERE precip_daily.source != 'station' OR EXCLUDED.source = 'station'""",
        (day, inches, source))


def precip_range(conn, since_day=None) -> list[dict]:
    if since_day is not None:
        cur = conn.execute(
            "SELECT day, inches, source FROM precip_daily WHERE day >= %s ORDER BY day",
            (since_day,))
    else:
        cur = conn.execute(
            "SELECT day, inches, source FROM precip_daily ORDER BY day")
    return [{"day": r[0], "inches": r[1], "source": r[2]} for r in cur.fetchall()]


def insert_air(conn, ts, room, pm25) -> None:
    conn.execute(
        """INSERT INTO air_readings (ts, room, pm25) VALUES (%s, %s, %s)
           ON CONFLICT (ts, room) DO UPDATE SET pm25=EXCLUDED.pm25""",
        (ts, room, pm25))


def latest_air(conn):
    """Newest PM2.5 per room: [{room, pm25, ts}], alphabetical by room."""
    cur = conn.execute(
        """SELECT DISTINCT ON (room) room, pm25, ts
           FROM air_readings ORDER BY room, ts DESC""")
    return [{"room": r[0], "pm25": r[1], "ts": r[2]} for r in cur.fetchall()]


def kv_set(conn, key, value) -> None:
    conn.execute(
        """INSERT INTO kv (k, v, updated_at) VALUES (%s, %s, now())
           ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, updated_at=now()""",
        (key, json.dumps(value)))


def kv_get(conn, key):
    """Returns {"value": ..., "updated_at": datetime} or None."""
    cur = conn.execute("SELECT v, updated_at FROM kv WHERE k=%s", (key,))
    row = cur.fetchone()
    if row is None:
        return None
    return {"value": row[0], "updated_at": row[1]}


def add_intervention(conn, marked_on, label, note=None):
    cur = conn.execute(
        "INSERT INTO interventions (marked_on, label, note) VALUES (%s, %s, %s)"
        " RETURNING id", (marked_on, label, note))
    return cur.fetchone()[0]


def list_interventions(conn) -> list[dict]:
    cur = conn.execute(
        "SELECT id, marked_on, label, note FROM interventions ORDER BY marked_on")
    return [{"id": r[0], "marked_on": r[1], "label": r[2], "note": r[3]}
            for r in cur.fetchall()]


def delete_intervention(conn, intervention_id) -> bool:
    cur = conn.execute(
        "DELETE FROM interventions WHERE id=%s RETURNING id", (intervention_id,))
    return cur.fetchone() is not None


def hourly_readings(conn, device_id, since_ts) -> list[dict]:
    cur = conn.execute(
        "SELECT bucket, avg_indoor_temp_f, avg_indoor_humidity, avg_outdoor_temp_f,"
        " cool_ticks, heat_ticks, fan_ticks FROM readings_hourly"
        " WHERE device_id=%s AND bucket >= %s ORDER BY bucket",
        (device_id, since_ts))
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
