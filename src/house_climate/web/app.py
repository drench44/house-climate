import os
import threading
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

from .. import db
from ..config import load_config, load_secrets
from . import alerts, api
from .alerts import alert_loop

cfg = load_config(os.environ.get("CONFIG_PATH", "config.json"))
secrets = load_secrets(os.environ)
conn = db.connect(secrets.db_dsn)
db.ensure_app_schema(conn)  # create runtime-added tables (filter_events) if missing

_conn_lock = threading.Lock()


def _db():
    """Live connection with self-heal. The module-level connection is opened
    once; after a TimescaleDB restart it would raise OperationalError on
    every request forever (psycopg3 does not auto-reconnect). One cheap ping
    per request buys automatic recovery. psycopg3 serializes a single execute,
    but the close-and-reassign below is a multi-step sequence its internal lock
    does NOT cover — under concurrent requests two threads could close/replace
    the connection while a third is mid-execute. _conn_lock makes the reconnect
    atomic so only one thread rebuilds the handle."""
    global conn
    try:
        conn.execute("SELECT 1")
        return conn
    except Exception:
        pass
    with _conn_lock:
        # Re-check inside the lock: another thread may have already healed it
        # while we waited, so we don't needlessly churn a fresh connection.
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        conn = db.connect(secrets.db_dsn)
        db.ensure_app_schema(conn)
        return conn

app = FastAPI(title="house-climate")

_device_id_cache = None
_device_id_cached_at = 0.0
_DEVICE_ID_TTL_S = 300   # re-resolve every 5 min so a device swap / DB wipe is picked up


def _device(c):
    """Resolve the device id to query: DEVICE_ID env if set, else the most
    recently seen row in the devices table. Cached with a short TTL rather than
    forever, so replacing the thermostat or restoring a fresh DB is picked up
    without a process restart (the old code cached the first id for the life of
    the process)."""
    global _device_id_cache, _device_id_cached_at
    env_device = os.environ.get("DEVICE_ID")
    if env_device:
        return env_device
    if _device_id_cache is not None and (time.monotonic() - _device_id_cached_at) < _DEVICE_ID_TTL_S:
        return _device_id_cache
    cur = c.execute("SELECT device_id FROM devices ORDER BY last_seen DESC LIMIT 1")
    row = cur.fetchone()
    if row is None:
        return _device_id_cache or "unknown"   # keep a prior id through a transient empty read
    _device_id_cache = row[0]
    _device_id_cached_at = time.monotonic()
    return _device_id_cache


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/now")
def now():
    c = _db()
    return api.build_now(c, _device(c))


@app.get("/api/history")
def history(range: str = "24h"):
    c = _db()
    return api.build_history(c, _device(c), range)


@app.get("/api/runtime")
def runtime_ep(days: int = Query(7, ge=1, le=400)):
    c = _db()
    return api.build_runtime(c, _device(c), cfg, days)


@app.get("/api/cost")
def cost_ep(days: int = Query(1, ge=1, le=400)):
    c = _db()
    return api.build_cost(c, _device(c), cfg, days)


@app.get("/api/cost/summary")
def cost_summary_ep():
    c = _db()
    return api.build_cost_summary(c, _device(c), cfg)


@app.get("/api/forecast")
def forecast():
    c = _db()
    return api.build_forecast(c, _device(c), cfg)


@app.get("/api/precool")
def precool():
    c = _db()
    return api.build_precool_advice(c, _device(c), cfg)


@app.get("/api/humidity")
def humidity_ep():
    c = _db()
    return api.build_humidity(c, _device(c), cfg)


@app.get("/api/rooms")
def rooms_ep():
    return api.build_rooms(_db(), cfg)


@app.get("/api/crawl")
def crawl_ep(range: str = "24h"):
    c = _db()
    return api.build_crawl(c, _device(c), cfg, range)


@app.get("/api/moisture")
def moisture_ep():
    c = _db()
    return api.build_moisture(c, _device(c), cfg)


@app.post("/api/ha/precool")
def ha_precool_ep(body: dict):
    """Home Assistant pushes the pre-cool toggle's state here (LAN-only, no
    auth — same trust model as the rest of this bind). Stored durably so the
    dashboard chip reflects the automation's real state across restarts."""
    from fastapi import HTTPException
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(422, "body must be {\"enabled\": true|false}")
    db.kv_set(_db(), "ha_precool", {"enabled": enabled})
    return {"ok": True, "enabled": enabled}


@app.post("/api/ha/air")
def ha_air_ep(body: dict):
    """Home Assistant pushes the purifiers' PM2.5 here (same LAN-only,
    no-auth trust model as /api/ha/precool). Body: {"upstairs": 3.0, ...};
    null/absent rooms are skipped so one offline purifier never blocks the
    others. Stored as a time series. An optional top-level "outdoor_aqi" is
    popped out before the room loop (so it's never treated as a room) and
    stored as a kv fact for the awareness/education surfaces to read back."""
    from fastapi import HTTPException
    if not isinstance(body, dict) or not body or len(body) > 10:
        raise HTTPException(422, "body must be a small {room: pm25} object")
    c = _db()
    try:
        api.pop_and_store_aqi(body, c)
    except ValueError as e:
        raise HTTPException(422, str(e))
    now = datetime.now(timezone.utc)
    stored = 0
    for room, val in body.items():
        if not isinstance(room, str) or not room or len(room) > 40:
            raise HTTPException(422, f"bad room key: {room!r}")
        if val is None:
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)) \
                or not (0 <= float(val) <= 1000):
            raise HTTPException(422, f"{room}: pm25 must be 0-1000 or null")
        db.insert_air(c, now, room.lower(), float(val))
        stored += 1
    return {"ok": True, "stored": stored}


@app.get("/api/air")
def air_ep():
    return api.build_air(_db())


@app.get("/api/moisture/export.csv")
def moisture_csv_ep():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(api.build_crawl_csv(_db(), cfg), media_type="text/csv",
                             headers={"Content-Disposition":
                                      'attachment; filename="crawl-sensor-data.csv"'})


@app.get("/api/moisture/precip.csv")
def precip_csv_ep():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(api.build_precip_csv(_db()), media_type="text/csv",
                             headers={"Content-Disposition":
                                      'attachment; filename="rainfall-daily.csv"'})


@app.get("/api/interventions")
def interventions_ep():
    return [{**iv, "marked_on": iv["marked_on"].isoformat()}
            for iv in db.list_interventions(_db())]


@app.post("/api/interventions")
def add_intervention_ep(body: dict):
    """Mark an intervention ('vapor barrier installed'). Body: {date:
    YYYY-MM-DD, label, note?}. Freezes the preceding period as that marker's
    baseline (see analytics/moisture.intervention_report)."""
    from fastapi import HTTPException
    label = (body.get("label") or "").strip()
    if not label:
        raise HTTPException(422, "label is required")
    try:
        marked_on = datetime.strptime(body.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    new_id = db.add_intervention(_db(), marked_on, label, (body.get("note") or "").strip() or None)
    return {"id": new_id, "marked_on": marked_on.isoformat(), "label": label}


@app.delete("/api/interventions/{intervention_id}")
def delete_intervention_ep(intervention_id: int):
    from fastapi import HTTPException
    if not db.delete_intervention(_db(), intervention_id):
        raise HTTPException(404, "no such intervention")
    return {"deleted": intervention_id}


@app.get("/api/thermal")
def thermal_ep():
    c = _db()
    return api.build_thermal(c, _device(c), cfg)


@app.get("/api/timeline")
def timeline_ep():
    c = _db()
    return api.build_timeline(c, _device(c), cfg)


@app.get("/api/health")
def health_ep():
    c = _db()
    return api.build_health(c, _device(c), cfg)


@app.post("/api/filter/changed")
def filter_changed_ep():
    """Log a filter change as of now and return the refreshed health block so
    the dashboard can update the filter clock without a full reload."""
    c = _db()
    db.record_filter_change(c, _device(c))
    return api.build_health(c, _device(c), cfg)


@app.get("/api/anomalies")
def anomalies_ep():
    since = datetime.now(timezone.utc) - timedelta(hours=cfg.alerts.get("short_cycles_window_hours", 3))
    c = _db()
    rows = db.recent_readings(c, _device(c), since)
    # daikin-only: Ecowitt failures are a different device (see alerts.py)
    errs = c.execute(
        "SELECT count(*) FROM poll_errors WHERE ts > now() - interval '20 minutes'"
        " AND kind LIKE 'daikin%'"
    ).fetchone()[0]
    return [a.__dict__ for a in alerts.evaluate(rows, cfg, errs)]


@app.get("/api/settings")
def settings_get():
    """Dashboard feature toggles (F0). Every tile can be turned on/off; state is
    server-side so the wall display and phones agree."""
    return api.get_dashboard_settings(_db())


@app.post("/api/settings")
def settings_post(body: dict):
    from fastapi import HTTPException
    features = body.get("features") if isinstance(body, dict) else None
    if not isinstance(features, dict):
        raise HTTPException(422, 'body must be {"features": {"<key>": true|false}}')
    return api.set_dashboard_settings(_db(), features)


@app.get("/api/camera/config")
def camera_config_get():
    return api.get_camera_config(_db())


@app.post("/api/camera/config")
def camera_config_post(body: dict):
    return api.set_camera_config(_db(), body.get("url", "") if isinstance(body, dict) else "")


# HTML must always revalidate (no-cache still allows ETag 304s): the ?v=N
# busters version the css/js, but the HTML that references them has no buster
# of its own — heuristic caching kept serving stale app.js after the
# 2026-08-13 deploy because the cached HTML still pointed at unversioned URLs.
@app.middleware("http")
async def html_no_cache(request, call_next):
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")

# alert evaluator wired in Task 11
threading.Thread(target=alert_loop, args=(cfg, secrets), daemon=True).start()
