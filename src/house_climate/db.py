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
    # Chores / routines (F3): a task belongs to a person and is worth points;
    # a completion is one (task, local day). Weekly payout = points of the
    # completions in the current ISO week.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chore_tasks (
               id         serial PRIMARY KEY,
               person     text NOT NULL,
               title      text NOT NULL,
               points     int  NOT NULL DEFAULT 1,
               active     boolean NOT NULL DEFAULT true,
               created_at timestamptz NOT NULL DEFAULT now()
           )""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chore_completions (
               id      serial PRIMARY KEY,
               task_id int  NOT NULL REFERENCES chore_tasks(id) ON DELETE CASCADE,
               done_on date NOT NULL,
               UNIQUE (task_id, done_on)
           )""")
    # Family message board (F4): short shared notes on the wall display.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
               id         serial PRIMARY KEY,
               author     text,
               body       text NOT NULL,
               pinned     boolean NOT NULL DEFAULT false,
               created_at timestamptz NOT NULL DEFAULT now()
           )""")
    # CalDAV local-first cache (F1 calendar / F2 reminders). The collections are
    # the shared category calendars (Strategy B: each carries its own color); the
    # events/todos are cached so the UI never touches the network on the render
    # path. iCloud stays the source of truth via the sync engine.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS caldav_collections (
               url          text PRIMARY KEY,
               kind         text,
               display_name text,
               color        text,
               ctag         text,
               sync_token   text,
               updated_at   timestamptz NOT NULL DEFAULT now()
           )""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS caldav_events (
               href           text PRIMARY KEY,
               collection_url text NOT NULL,
               uid            text,
               etag           text,
               summary        text,
               start_utc      timestamptz,
               end_utc        timestamptz,
               all_day        boolean NOT NULL DEFAULT false,
               location       text,
               color          text,
               recurrence_id  text,
               raw_ics        text
           )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS caldav_events_start ON caldav_events (start_utc)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS caldav_todos (
               href           text PRIMARY KEY,
               collection_url text NOT NULL,
               uid            text,
               etag           text,
               summary        text,
               due_utc        timestamptz,
               status         text,
               priority       int,
               color          text,
               list_name      text,
               raw_ics        text
           )""")
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


def add_chore_task(conn, person, title, points=1):
    cur = conn.execute(
        "INSERT INTO chore_tasks (person, title, points) VALUES (%s, %s, %s) RETURNING id",
        (person, title, int(points)))
    return cur.fetchone()[0]


def delete_chore_task(conn, task_id) -> bool:
    cur = conn.execute("DELETE FROM chore_tasks WHERE id=%s RETURNING id", (task_id,))
    return cur.fetchone() is not None


def toggle_chore_done(conn, task_id, day) -> bool:
    """Toggle a task's completion for `day` (a date). Returns the new done state.
    Returns False if the task doesn't exist."""
    exists = conn.execute("SELECT 1 FROM chore_tasks WHERE id=%s", (task_id,)).fetchone()
    if not exists:
        return False
    deleted = conn.execute(
        "DELETE FROM chore_completions WHERE task_id=%s AND done_on=%s RETURNING id",
        (task_id, day)).fetchone()
    if deleted:
        return False
    conn.execute(
        "INSERT INTO chore_completions (task_id, done_on) VALUES (%s, %s)"
        " ON CONFLICT DO NOTHING", (task_id, day))
    return True


def chores_overview(conn, today, week_start) -> dict:
    """Active tasks grouped by person with a done-today flag, plus each person's
    points earned so far this week."""
    cur = conn.execute(
        """SELECT t.id, t.person, t.title, t.points,
                  EXISTS(SELECT 1 FROM chore_completions c
                         WHERE c.task_id=t.id AND c.done_on=%s) AS done_today
           FROM chore_tasks t WHERE t.active ORDER BY t.person, t.id""",
        (today,))
    tasks = [{"id": r[0], "person": r[1], "title": r[2], "points": r[3],
              "done_today": r[4]} for r in cur.fetchall()]
    cur = conn.execute(
        """SELECT t.person, COALESCE(SUM(t.points), 0)
           FROM chore_completions c JOIN chore_tasks t ON t.id=c.task_id
           WHERE c.done_on >= %s GROUP BY t.person""",
        (week_start,))
    week_points = {r[0]: int(r[1]) for r in cur.fetchall()}
    return {"tasks": tasks, "week_points": week_points}


def add_message(conn, body, author=None):
    cur = conn.execute(
        "INSERT INTO messages (author, body) VALUES (%s, %s) RETURNING id",
        (author, body))
    return cur.fetchone()[0]


def list_messages(conn, limit=20) -> list[dict]:
    """Newest first, pinned messages always on top."""
    cur = conn.execute(
        "SELECT id, author, body, pinned, created_at FROM messages"
        " ORDER BY pinned DESC, created_at DESC LIMIT %s", (limit,))
    return [{"id": r[0], "author": r[1], "body": r[2], "pinned": r[3],
             "created_at": r[4]} for r in cur.fetchall()]


def delete_message(conn, message_id) -> bool:
    cur = conn.execute(
        "DELETE FROM messages WHERE id=%s RETURNING id", (message_id,))
    return cur.fetchone() is not None


def set_message_pinned(conn, message_id, pinned) -> bool:
    cur = conn.execute(
        "UPDATE messages SET pinned=%s WHERE id=%s RETURNING id",
        (bool(pinned), message_id))
    return cur.fetchone() is not None


def upsert_caldav_collection(conn, url, kind, display_name, color, ctag, sync_token):
    conn.execute(
        """INSERT INTO caldav_collections (url, kind, display_name, color, ctag, sync_token, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (url) DO UPDATE SET
             kind=EXCLUDED.kind, display_name=EXCLUDED.display_name, color=EXCLUDED.color,
             ctag=EXCLUDED.ctag, sync_token=EXCLUDED.sync_token, updated_at=now()""",
        (url, kind, display_name, color, ctag, sync_token))


def caldav_collections(conn, kind=None) -> list[dict]:
    if kind:
        cur = conn.execute("SELECT url, kind, display_name, color, ctag, sync_token"
                           " FROM caldav_collections WHERE kind=%s", (kind,))
    else:
        cur = conn.execute("SELECT url, kind, display_name, color, ctag, sync_token"
                           " FROM caldav_collections")
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def upsert_caldav_event(conn, collection_url, href, etag, color, ev) -> None:
    conn.execute(
        """INSERT INTO caldav_events
             (href, collection_url, uid, etag, summary, start_utc, end_utc, all_day,
              location, color, recurrence_id, raw_ics)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (href) DO UPDATE SET
             collection_url=EXCLUDED.collection_url, uid=EXCLUDED.uid, etag=EXCLUDED.etag,
             summary=EXCLUDED.summary, start_utc=EXCLUDED.start_utc, end_utc=EXCLUDED.end_utc,
             all_day=EXCLUDED.all_day, location=EXCLUDED.location, color=EXCLUDED.color,
             recurrence_id=EXCLUDED.recurrence_id, raw_ics=EXCLUDED.raw_ics""",
        (href, collection_url, ev.get("uid"), etag, ev.get("summary"),
         ev.get("start"), ev.get("end"), bool(ev.get("all_day")), ev.get("location"),
         color, ev.get("recurrence_id"), ev.get("raw_ics")))


def delete_caldav_event(conn, href) -> None:
    conn.execute("DELETE FROM caldav_events WHERE href=%s", (href,))


def upcoming_events(conn, since_utc, until_utc, limit=60) -> list[dict]:
    cur = conn.execute(
        "SELECT href, uid, summary, start_utc, end_utc, all_day, location, color"
        " FROM caldav_events WHERE start_utc IS NOT NULL AND start_utc >= %s AND start_utc < %s"
        " ORDER BY start_utc LIMIT %s", (since_utc, until_utc, limit))
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def upsert_caldav_todo(conn, collection_url, href, etag, color, list_name, td) -> None:
    conn.execute(
        """INSERT INTO caldav_todos
             (href, collection_url, uid, etag, summary, due_utc, status, priority, color, list_name, raw_ics)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (href) DO UPDATE SET
             collection_url=EXCLUDED.collection_url, uid=EXCLUDED.uid, etag=EXCLUDED.etag,
             summary=EXCLUDED.summary, due_utc=EXCLUDED.due_utc, status=EXCLUDED.status,
             priority=EXCLUDED.priority, color=EXCLUDED.color, list_name=EXCLUDED.list_name,
             raw_ics=EXCLUDED.raw_ics""",
        (href, collection_url, td.get("uid"), etag, td.get("summary"), td.get("due"),
         td.get("status"), td.get("priority"), color, list_name, td.get("raw_ics")))


def delete_caldav_todo(conn, href) -> None:
    conn.execute("DELETE FROM caldav_todos WHERE href=%s", (href,))


def open_todos(conn, limit=100) -> list[dict]:
    cur = conn.execute(
        "SELECT href, uid, summary, due_utc, status, priority, color, list_name"
        " FROM caldav_todos ORDER BY (status='COMPLETED'),"
        " (due_utc IS NULL), due_utc, priority NULLS LAST LIMIT %s", (limit,))
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_caldav_todo(conn, href):
    cur = conn.execute(
        "SELECT href, collection_url, etag, raw_ics FROM caldav_todos WHERE href=%s", (href,))
    r = cur.fetchone()
    if not r:
        return None
    return {"href": r[0], "collection_url": r[1], "etag": r[2], "raw_ics": r[3]}


def set_todo_status(conn, href, status, raw_ics, etag=None) -> None:
    conn.execute(
        "UPDATE caldav_todos SET status=%s, raw_ics=%s,"
        " etag=COALESCE(%s, etag) WHERE href=%s",
        (status, raw_ics, etag, href))


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


def outdoor_hourly(conn, device_id, since_ts) -> list[dict]:
    """Hourly mean OUTDOOR temp / RH / dew point from the weather feed on the
    readings table. A bucket appears when the feed reported ANY of the three
    that hour; a field is null when the feed lacked it (avg() skips nulls). One
    query feeds both the crawl-vs-outdoor attribution (which reads `dp`) and the
    /api/outdoor series — so the two never diverge."""
    cur = conn.execute(
        "SELECT time_bucket(interval '1 hour', ts) AS bucket,"
        " avg(wx_outdoor_temp_f) AS temp, avg(wx_humidity) AS rh,"
        " avg(wx_dewpoint_f) AS dp"
        " FROM readings WHERE device_id=%s AND ts >= %s"
        "   AND (wx_outdoor_temp_f IS NOT NULL OR wx_humidity IS NOT NULL"
        "        OR wx_dewpoint_f IS NOT NULL)"
        " GROUP BY bucket ORDER BY bucket",
        (device_id, since_ts))
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


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


def merge_kv_features(conn, key, patch: dict) -> None:
    """Atomically merge a {feature: bool} patch into kv[key]['features'] in ONE
    statement (jsonb `||`), so concurrent toggles can't clobber each other. The
    Python read-modify-write path had a lost-update race: two POSTs racing each
    read the old value, add their own key, and the last write wins."""
    conn.execute(
        """INSERT INTO kv (k, v, updated_at)
           VALUES (%s, jsonb_build_object('features', %s::jsonb), now())
           ON CONFLICT (k) DO UPDATE SET
             v = jsonb_build_object('features',
                   COALESCE(kv.v->'features', '{}'::jsonb) || (EXCLUDED.v->'features')),
             updated_at = now()""",
        (key, json.dumps(patch)))


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
