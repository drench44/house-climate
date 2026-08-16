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
