CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS devices (
    device_id  text PRIMARY KEY,
    name       text,
    model      text,
    first_seen timestamptz DEFAULT now(),
    last_seen  timestamptz
);

CREATE TABLE IF NOT EXISTS readings (
    ts                     timestamptz NOT NULL,
    device_id              text        NOT NULL,
    indoor_temp_f          double precision,
    indoor_humidity        double precision,
    heat_setpoint_f        double precision,
    cool_setpoint_f        double precision,
    equipment_status       text,
    mode                   text,
    daikin_outdoor_temp_f  double precision,
    daikin_outdoor_humidity double precision,
    wx_outdoor_temp_f      double precision,
    wx_humidity            double precision,
    wx_dewpoint_f          double precision,
    wx_solar_wm2           double precision,
    wx_uv                  double precision,
    wx_fc_high_f           double precision,
    wx_fc_low_f            double precision,
    wx_conditions          text,
    wx_aqi                 double precision,
    wx_alert_count         integer,
    weather_ok             boolean NOT NULL DEFAULT false,
    wx_rain_today_in       double precision
);
SELECT create_hypertable('readings', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS readings_device_ts ON readings (device_id, ts DESC);

CREATE TABLE IF NOT EXISTS poll_errors (
    ts        timestamptz NOT NULL DEFAULT now(),
    device_id text,
    kind      text NOT NULL,
    detail    text
);
SELECT create_hypertable('poll_errors', 'ts', if_not_exists => TRUE);

-- Generic auxiliary sensors (crawlspace humidity monitor, and any future sensor).
-- Created from day one even before the device exists; a new sensor is a new
-- sensor_id, never a migration.
CREATE TABLE IF NOT EXISTS sensor_readings (
    ts        timestamptz NOT NULL,
    sensor_id text        NOT NULL,
    temp_f    double precision,
    humidity  double precision,
    battery   double precision,
    extra     jsonb,
    dewpoint_f double precision
);
SELECT create_hypertable('sensor_readings', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS sensor_readings_id_ts ON sensor_readings (sensor_id, ts DESC);

-- Filter-change log: each row is a "I replaced the HVAC filter" event. The
-- most recent one is the start of the current filter's runtime clock (see
-- build_health). Also created at runtime by db.ensure_app_schema so existing
-- deployments get it without a fresh volume.
-- Rainfall, one row per local day. source 'station' = the house's own rain
-- gauge (wx.json rainToday); 'openmeteo' = gridded backfill for days before
-- station capture began. Station always wins (see db.upsert_precip).
CREATE TABLE IF NOT EXISTS precip_daily (
    day        date PRIMARY KEY,
    inches     double precision NOT NULL,
    source     text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Moisture-case intervention markers ("vapor barrier installed"). Each one
-- freezes the preceding period as a labeled baseline for before/after stats.
CREATE TABLE IF NOT EXISTS interventions (
    id         serial PRIMARY KEY,
    marked_on  date NOT NULL,
    label      text NOT NULL,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Durable k/v for state pushed from outside (HA pre-cool toggle, etc.)
CREATE TABLE IF NOT EXISTS kv (
    k          text PRIMARY KEY,
    v          jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS filter_events (
    id         serial PRIMARY KEY,
    device_id  text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    note       text
);
CREATE INDEX IF NOT EXISTS filter_events_device_ts ON filter_events (device_id, changed_at DESC);

-- Indoor PM2.5 pushed by Home Assistant from the Levoit purifiers. Plain table,
-- not a hypertable (<3k rows/day). Also created at runtime by
-- db.ensure_app_schema; declared here so init.sql stays the schema of record.
CREATE TABLE IF NOT EXISTS air_readings (
    ts    timestamptz NOT NULL,
    room  text NOT NULL,
    pm25  real NOT NULL,
    PRIMARY KEY (ts, room)
);
CREATE INDEX IF NOT EXISTS air_readings_room_ts ON air_readings (room, ts DESC);
