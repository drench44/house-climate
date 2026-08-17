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
# TestClient sends Host: testserver; allow it past the host-allowlist guard.
os.environ.setdefault("CLIMATE_ALLOWED_HOSTS", "testserver")

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


# --- deep /health (issue #6) ---

def test_health_ok_reports_db_and_freshness(conn):
    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["db"] == "ok"
    assert "latest_reading_age_s" in body["checks"]
    assert "poller_heartbeat_age_s" in body["checks"]


def test_health_503_when_db_unreachable(conn, monkeypatch):
    # The old /health returned 200 unconditionally even with the DB down, so the
    # container healthcheck never tripped. Force _db() to fail -> must be 503.
    from house_climate.web import app as appmod

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(appmod, "_db", boom)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "error"
# --- unauthenticated-write hardening (issue #10) ---

def test_host_not_allowed_returns_400(conn):
    # A DNS-rebinding request arrives with a public-looking Host header.
    resp = client.post("/api/ha/air", json={"upstairs": 5},
                       headers={"host": "evil.example.com"})
    assert resp.status_code == 400


def test_cross_site_write_blocked_via_sec_fetch(conn):
    # A browser drive-by carries Sec-Fetch-Site: cross-site -> 403.
    resp = client.post("/api/filter/changed", json={},
                       headers={"sec-fetch-site": "cross-site"})
    assert resp.status_code == 403


def test_cross_origin_write_blocked_via_origin(conn):
    resp = client.post("/api/filter/changed", json={},
                       headers={"origin": "http://evil.example.com"})
    assert resp.status_code == 403


def test_non_browser_write_ok(conn):
    # Home Assistant / curl send neither Origin nor Sec-Fetch-Site -> allowed,
    # even with a text/plain body (we do NOT gate on Content-Type).
    resp = client.post("/api/filter/changed", content="{}",
                       headers={"content-type": "text/plain"})
    assert resp.status_code == 200, resp.text


def test_write_rate_limit_returns_429(conn, monkeypatch):
    from house_climate.web import app as appmod
    monkeypatch.setattr(appmod, "_RATE_LIMIT_WRITES_PER_MIN", 3)
    appmod._write_hits.clear()
    codes = [client.post("/api/filter/changed", json={}).status_code for _ in range(6)]
    assert 429 in codes
    assert codes.count(200) <= 3
    appmod._write_hits.clear()
# --- FastAPI routing-layer coverage (issue #12) ---

def test_ha_precool_valid_and_invalid(conn):
    assert client.post("/api/ha/precool", json={"enabled": True}).status_code == 200
    assert client.post("/api/ha/precool", json={"enabled": "yes"}).status_code == 422


def test_interventions_crud_and_error_branches(conn):
    assert client.post("/api/interventions", json={"date": "2026-08-01", "label": ""}).status_code == 422
    assert client.post("/api/interventions", json={"date": "nope", "label": "x"}).status_code == 422
    r = client.post("/api/interventions", json={"date": "2026-08-01", "label": "vapor barrier"})
    assert r.status_code == 200, r.text
    iid = r.json()["id"]
    assert any(iv["id"] == iid for iv in client.get("/api/interventions").json())
    assert client.delete(f"/api/interventions/{iid}").status_code == 200
    assert client.delete(f"/api/interventions/{iid}").status_code == 404   # already gone


def test_anomalies_endpoint_returns_list(conn):
    r = client.get("/api/anomalies")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_read_endpoints_smoke_on_empty_db(conn):
    # Every read route must serve (fails-soft to available:false), not 500, even
    # with no device/readings — exercises the route wiring + _device fallback.
    for path in ["/api/now", "/api/history", "/api/runtime", "/api/cost",
                 "/api/cost/summary", "/api/forecast", "/api/precool", "/api/humidity",
                 "/api/rooms", "/api/crawl", "/api/moisture", "/api/air",
                 "/api/thermal", "/api/timeline", "/api/health"]:
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"
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


# --- chores (F3, issue #29) ---

def test_chores_add_toggle_and_week_points(conn):
    tid = client.post("/api/chores/tasks", json={"person": "Ella", "title": "Dishes", "points": 3}).json()["id"]
    data = client.get("/api/chores").json()
    ella = next(p for p in data["people"] if p["person"] == "Ella")
    assert ella["tasks"][0]["title"] == "Dishes" and ella["tasks"][0]["done_today"] is False
    assert ella["points_week"] == 0
    # mark done -> week points reflect it
    assert client.post(f"/api/chores/tasks/{tid}/toggle", json={}).json()["done_today"] is True
    ella = next(p for p in client.get("/api/chores").json()["people"] if p["person"] == "Ella")
    assert ella["tasks"][0]["done_today"] is True and ella["points_week"] == 3
    # toggle off -> points back to 0
    assert client.post(f"/api/chores/tasks/{tid}/toggle", json={}).json()["done_today"] is False
    assert next(p for p in client.get("/api/chores").json()["people"] if p["person"] == "Ella")["points_week"] == 0


def test_chores_validation_and_delete(conn):
    assert client.post("/api/chores/tasks", json={"person": "", "title": "x"}).status_code == 422
    tid = client.post("/api/chores/tasks", json={"person": "Sam", "title": "Trash"}).json()["id"]
    assert client.delete(f"/api/chores/tasks/{tid}").status_code == 200
    assert client.delete(f"/api/chores/tasks/{tid}").status_code == 404
    assert client.get("/api/chores").json()["people"] == []


def test_chores_in_feature_registry(conn):
    assert "chores" in {f["key"] for f in client.get("/api/settings").json()["features"]}
# --- pluggable slots / tile ordering (F8, issue #34) ---

def test_tile_order_roundtrip(conn):
    r = client.post("/api/settings/order", json={"order": ["ribbon", "crawl", "humidity"]})
    assert r.status_code == 200
    assert r.json()["order"] == ["ribbon", "crawl", "humidity"]
    assert client.get("/api/settings").json()["order"] == ["ribbon", "crawl", "humidity"]


def test_tile_order_drops_unknown_keys(conn):
    r = client.post("/api/settings/order", json={"order": ["humidity", "nope", "crawl"]})
    assert r.json()["order"] == ["humidity", "crawl"]


def test_tile_order_bad_body_422(conn):
    assert client.post("/api/settings/order", json={"order": "notalist"}).status_code == 422


def test_settings_includes_order_field(conn):
    assert "order" in client.get("/api/settings").json()
# --- family message board (F4, issue #30) ---

def test_messages_crud(conn):
    assert client.get("/api/messages").json() == []
    r = client.post("/api/messages", json={"body": "  Dinner at 6  ", "author": "Gary"})
    assert r.status_code == 200
    mid = r.json()["id"]
    msgs = client.get("/api/messages").json()
    assert len(msgs) == 1 and msgs[0]["body"] == "Dinner at 6" and msgs[0]["author"] == "Gary"
    assert client.delete(f"/api/messages/{mid}").status_code == 200
    assert client.get("/api/messages").json() == []
    assert client.delete(f"/api/messages/{mid}").status_code == 404


def test_messages_reject_empty_and_too_long(conn):
    assert client.post("/api/messages", json={"body": "   "}).status_code == 422
    assert client.post("/api/messages", json={"body": "x" * 501}).status_code == 422


def test_messages_pinned_sort_first(conn):
    a = client.post("/api/messages", json={"body": "first"}).json()["id"]
    client.post("/api/messages", json={"body": "second"})
    client.post(f"/api/messages/{a}/pin", json={"pinned": True})
    msgs = client.get("/api/messages").json()
    assert msgs[0]["body"] == "first" and msgs[0]["pinned"] is True


def test_messageboard_in_feature_registry(conn):
    keys = {f["key"] for f in client.get("/api/settings").json()["features"]}
    assert "messageboard" in keys
# --- camera snapshot tile (F6, issue #32) ---

def test_camera_config_roundtrip(conn):
    assert client.post("/api/camera/config",
                       json={"url": "http://cam/snap.jpg"}).status_code == 200
    assert client.get("/api/camera/config").json() == {"url": "http://cam/snap.jpg"}
    # An empty URL clears the stored snapshot source.
    assert client.post("/api/camera/config", json={"url": ""}).status_code == 200
    assert client.get("/api/camera/config").json() == {"url": ""}


def test_camera_in_feature_registry(conn):
    keys = {f["key"] for f in client.get("/api/settings").json()["features"]}
    assert "camera" in keys
