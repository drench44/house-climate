"""GET /health/full against a real Postgres (the judgements themselves are in
tests/test_deep_health.py). Skipped without TEST_DB_DSN, like test_app.py;
CI always sets it (conftest refuses to run CI without it).
"""
import dataclasses
import os
from datetime import datetime, timedelta, timezone

import pytest

TEST_DSN = os.environ.get("TEST_DB_DSN")
if not TEST_DSN:
    pytest.skip("TEST_DB_DSN not set; skipping /health/full route tests",
                allow_module_level=True)

from conftest import CFG_PATH  # noqa: E402

os.environ.setdefault("DB_DSN", TEST_DSN)
os.environ.setdefault("DAIKIN_API_KEY", "test-key")
os.environ.setdefault("DAIKIN_INTEGRATOR_TOKEN", "test-token")
os.environ.setdefault("DAIKIN_EMAIL", "test@example.com")
os.environ.setdefault("CONFIG_PATH", CFG_PATH)
os.environ.setdefault("CLIMATE_ALLOWED_HOSTS", "testserver")

from fastapi.testclient import TestClient  # noqa: E402

from house_climate import db  # noqa: E402
from house_climate.web import alerts as alertsmod  # noqa: E402
from house_climate.web import app as appmod  # noqa: E402

client = TestClient(appmod.app)
COMMIT = "c0ffee" + "0" * 34


def _now():
    return datetime.now(timezone.utc)


def _reading(conn, ts, thermostat=True, weather=True):
    row = {c: None for c in db.READING_COLUMNS}
    row.update(ts=ts, device_id="dev1", weather_ok=weather)
    if thermostat:
        row.update(equipment_status="idle", mode="cool", indoor_temp_f=70.0)
    db.insert_reading(conn, row)


@pytest.fixture
def house(conn, monkeypatch):
    """A stack that works: the deploy record, a poller on the same commit that
    started 10 minutes ago, readings of every kind written since, room sensors
    on, the alert loop just ran."""
    monkeypatch.setattr(appmod, "BUILD_INFO", {
        "engine_commit": COMMIT, "overlay_commit": None,
        "config_sha256": appmod.CONFIG_SHA256, "built_at": None})
    monkeypatch.setattr(appmod, "cfg", dataclasses.replace(appmod.cfg, ecowitt={
        "enabled": True, "channels": {"1": "Upstairs"}, "outdoor_name": "Crawl"}))
    monkeypatch.setitem(alertsmod.LOOP_STATE, "last_run", _now())
    started = _now() - timedelta(minutes=10)
    db.kv_set(conn, "poller_heartbeat", {"ts": _now().isoformat(), "commit": COMMIT,
                                         "started_at": started.isoformat()})
    _reading(conn, _now() - timedelta(minutes=2))
    db.insert_sensor_reading(conn, "ecowitt_ch1", _now() - timedelta(minutes=1), temp_f=70.0)
    return conn, started


def _full():
    r = client.get("/health/full")
    assert r.status_code == 200, r.text
    return r.json()


def test_all_green(house):
    r = _full()
    assert r["status"] == "ok", r["problems"]
    assert r["poller"]["ok"] is True and r["poller"]["commit"] == COMMIT
    for name in ("thermostat", "rooms", "weather"):
        assert r["sources"][name]["ok"] is True, name
    assert r["config"]["matches_deploy"] is True
    assert r["deploy"]["engine_commit"] == COMMIT
    assert r["alerts"]["ok"] is True
    assert r["settings"]["daikin"]["ok"] is True


def test_liveness_is_unchanged(house):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["checks"]["db"] == "ok"


def test_the_old_poller_still_ticking_fails(house):
    conn, started = house
    db.kv_set(conn, "poller_heartbeat", {"ts": _now().isoformat(), "commit": "0ld" * 13 + "0",
                                         "started_at": started.isoformat()})
    r = _full()
    assert r["poller"]["status"] == "other_commit"
    assert "poller: other_commit" in r["problems"]


def test_readings_from_before_the_poller_started_do_not_count(house):
    conn, _ = house
    db.kv_set(conn, "poller_heartbeat", {"ts": _now().isoformat(), "commit": COMMIT,
                                         "started_at": _now().isoformat()})
    r = _full()
    assert r["sources"]["thermostat"]["status"] == "waiting"
    assert r["sources"]["rooms"]["status"] == "waiting"
    assert r["status"] == "degraded"


def test_a_weather_only_row_is_not_a_thermostat_reading(house):
    conn, _ = house
    conn.execute("TRUNCATE readings")
    _reading(conn, _now() - timedelta(minutes=1), thermostat=False)
    r = _full()
    assert r["sources"]["weather"]["ok"] is True
    assert r["sources"]["thermostat"]["status"] == "waiting"


def test_stale_room_sensors(house):
    conn, _ = house
    conn.execute("TRUNCATE sensor_readings")
    db.insert_sensor_reading(conn, "ecowitt_ch1", _now() - timedelta(hours=2), temp_f=70.0)
    assert _full()["sources"]["rooms"]["status"] == "stale"


def test_a_dead_alert_loop_fails(house, monkeypatch):
    monkeypatch.setitem(alertsmod.LOOP_STATE, "last_run", _now() - timedelta(hours=1))
    r = _full()
    assert r["alerts"]["status"] == "stale" and "alert loop: stale" in r["problems"]


def test_a_webhook_channel_without_its_url_fails(house, monkeypatch):
    monkeypatch.setattr(appmod, "cfg", dataclasses.replace(
        appmod.cfg, alerts=dict(appmod.cfg.alerts, channel="webhook")))
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "")
    r = _full()
    assert r["settings"]["alert_channel"]["ok"] is False
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://ha.example/api/webhook/x")
    assert _full()["settings"]["alert_channel"]["ok"] is True


def test_the_image_carries_other_config_bytes(house, monkeypatch):
    monkeypatch.setattr(appmod, "BUILD_INFO", dict(appmod.BUILD_INFO, config_sha256="0" * 64))
    r = _full()
    assert r["config"]["matches_deploy"] is False and r["status"] == "degraded"


def test_the_database_down_is_named(house, monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(appmod, "_db", boom)
    r = _full()
    assert r["db"] == {"ok": False, "error": "RuntimeError: db down"}
    assert r["poller"]["status"] == "waiting" and r["status"] == "degraded"
