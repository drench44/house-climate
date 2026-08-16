"""HTTP-layer test for the FastAPI app (web/app.py), specifically the
/api/ha/air endpoint's request-validation path (422s, not 500s).

house_climate.web.app has import-time env requirements (DB_DSN,
DAIKIN_API_KEY, DAIKIN_INTEGRATOR_TOKEN, DAIKIN_EMAIL) and opens a real DB
connection + starts a background alert-evaluator thread at import. Those env
vars are set here, before the import, using the same TEST_DB_DSN the `conn`
fixture uses — so this module needs a real test DB and is skipped (not
failed) wherever conn-backed tests already skip: no TEST_DB_DSN locally.

pop_and_store_aqi (the AQI pop/validate/store seam) already has direct unit
coverage in tests/test_api.py; this file only covers the HTTP layer wrapped
around it: status codes and what actually lands in Postgres.
"""
import os

import pytest

TEST_DSN = os.environ.get("TEST_DB_DSN")
if not TEST_DSN:
    pytest.skip("TEST_DB_DSN not set; skipping HTTP-layer app tests",
                allow_module_level=True)

from conftest import CFG_PATH  # noqa: E402 (must follow the skip above)

# house_climate.web.app reads these at IMPORT time (module-level
# load_secrets() + db.connect()) — they must exist before the import below.
os.environ.setdefault("DB_DSN", TEST_DSN)
os.environ.setdefault("DAIKIN_API_KEY", "test-key")
os.environ.setdefault("DAIKIN_INTEGRATOR_TOKEN", "test-token")
os.environ.setdefault("DAIKIN_EMAIL", "test@example.com")
os.environ.setdefault("CONFIG_PATH", CFG_PATH)

from fastapi.testclient import TestClient  # noqa: E402

from house_climate.web.app import app  # noqa: E402
from house_climate import db  # noqa: E402

client = TestClient(app)


def test_ha_air_combined_body_stores_aqi_and_room(conn):
    resp = client.post("/api/ha/air", json={"outdoor_aqi": 137, "upstairs": 8})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"ok": True, "stored": 1}

    kv = db.kv_get(conn, "ha_outdoor_aqi")
    assert kv is not None
    assert kv["value"] == {"aqi": 137}

    rooms = db.latest_air(conn)
    assert [r["room"] for r in rooms] == ["upstairs"]
    assert rooms[0]["pm25"] == 8.0


def test_ha_air_aqi_only_body_pops_before_room_loop_stores_zero_rooms(conn):
    """The AQI key must never be treated as a room — stored counts rooms
    only, so an AQI-only body stores 0 rooms (but the AQI itself still
    lands in kv)."""
    resp = client.post("/api/ha/air", json={"outdoor_aqi": 137})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "stored": 0}

    kv = db.kv_get(conn, "ha_outdoor_aqi")
    assert kv is not None and kv["value"] == {"aqi": 137}
    assert db.latest_air(conn) == []


def test_ha_air_bad_aqi_returns_422_not_500(conn):
    resp = client.post("/api/ha/air", json={"outdoor_aqi": 1001})
    assert resp.status_code == 422, resp.text
    # nothing stored on the rejected request
    assert db.kv_get(conn, "ha_outdoor_aqi") is None
    assert db.latest_air(conn) == []


# --- dashboard feature toggles (F0, issue #26) ---

def test_settings_defaults_all_enabled(conn):
    r = client.get("/api/settings")
    assert r.status_code == 200
    feats = r.json()["features"]
    assert feats and all(f["enabled"] for f in feats)
    keys = {f["key"] for f in feats}
    assert {"scene", "cost", "humidity", "crawl", "ribbon", "runtime", "health", "learning"} <= keys


def test_settings_toggle_persists_and_is_isolated(conn):
    assert client.post("/api/settings", json={"features": {"crawl": False}}).status_code == 200
    feats = {f["key"]: f["enabled"] for f in client.get("/api/settings").json()["features"]}
    assert feats["crawl"] is False
    assert feats["humidity"] is True          # other tiles unaffected
    client.post("/api/settings", json={"features": {"crawl": True}})
    again = {f["key"]: f["enabled"] for f in client.get("/api/settings").json()["features"]}
    assert again["crawl"] is True


def test_settings_ignores_unknown_keys(conn):
    r = client.post("/api/settings", json={"features": {"not_a_tile": False}})
    assert r.status_code == 200
    assert "not_a_tile" not in {f["key"] for f in r.json()["features"]}


def test_settings_bad_body_422(conn):
    assert client.post("/api/settings", json={"nope": 1}).status_code == 422


def test_dashboard_html_wires_toggles(conn):
    html = client.get("/").text
    assert 'data-feature="crawl"' in html
    assert 'id="btn-settings"' in html and 'id="settings-panel"' in html


def test_settings_merge_accumulates_without_clobber(conn):
    # Two separate patches must both stick (the atomic jsonb merge; the old
    # read-modify-write path lost updates under concurrency).
    client.post("/api/settings", json={"features": {"cost": False}})
    client.post("/api/settings", json={"features": {"ribbon": False}})
    feats = {f["key"]: f["enabled"] for f in client.get("/api/settings").json()["features"]}
    assert feats["cost"] is False and feats["ribbon"] is False
    assert feats["humidity"] is True


# --- family calendar (F1, issue #27) ---

def test_calendar_endpoint_empty_is_unconfigured(conn):
    r = client.get("/api/calendar").json()
    assert r["configured"] is False and r["days"] == []


def test_calendar_configured_after_sync(conn):
    from test_caldav import _client
    from house_climate import caldav
    caldav.sync_events(conn, _client())
    assert client.get("/api/calendar").json()["configured"] is True


def test_calendar_in_feature_registry(conn):
    assert "calendar" in {f["key"] for f in client.get("/api/settings").json()["features"]}
