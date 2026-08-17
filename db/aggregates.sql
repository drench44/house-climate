-- Runs via initdb on a FRESH volume only. db.ensure_aggregates() mirrors this
-- and re-applies it (idempotently) on every app startup, so edits here also
-- reach already-provisioned databases. Keep the two in sync.
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_hourly
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
WITH NO DATA;

SELECT add_continuous_aggregate_policy('readings_hourly',
    start_offset => INTERVAL '3 hours',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);

ALTER TABLE readings SET (timescaledb.compress, timescaledb.compress_segmentby = 'device_id');
SELECT add_compression_policy('readings', INTERVAL '30 days', if_not_exists => TRUE);
