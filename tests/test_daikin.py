import json
import logging
from pathlib import Path
import pytest
from house_climate import daikin
from house_climate.daikin import DaikinClient, DaikinError, RateLimited

FIX = json.loads(Path(__file__).parent.joinpath("fixtures/daikin_device.json").read_text())


class FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


def _client():
    return DaikinClient("api_key", "token", "e@x.com")

def test_parse_device_maps_and_converts_c_to_f():
    st = daikin._parse_device(FIX)
    assert st.mode == "cool"
    assert st.equipment_status == "cooling"
    assert round(st.indoor_temp_f, 1) == 72.3      # 22.4C -> 72.32F
    assert st.indoor_humidity == 48
    assert round(st.cool_setpoint_f, 1) == 72.0    # 22.2C -> 71.96F

def test_equipment_status_idle():
    st = daikin._parse_device({**FIX, "equipmentStatus": 5})
    assert st.equipment_status == "idle"


# --- HTTP / auth layer (previously untested): the exact status->exception
# mapping poll_once's error contract depends on, plus token cache/refresh and
# the shape-error contract (a 200 with the wrong shape must be a DaikinError,
# not a bare KeyError that escapes poll_once and writes no poll_error).

def test_access_token_429_raises_rate_limited(monkeypatch):
    monkeypatch.setattr(daikin.requests, "post", lambda *a, **k: FakeResp(429))
    with pytest.raises(RateLimited):
        _client().access_token()


def test_access_token_5xx_raises_daikin_error(monkeypatch):
    monkeypatch.setattr(daikin.requests, "post", lambda *a, **k: FakeResp(503, text="down"))
    with pytest.raises(DaikinError):
        _client().access_token()


def test_access_token_missing_field_raises_daikin_error_not_keyerror(monkeypatch):
    # 200 OK whose body lacks accessToken (Daikin renamed a field). Must be a
    # DaikinError so poll_once records it, not a KeyError that escapes.
    monkeypatch.setattr(daikin.requests, "post", lambda *a, **k: FakeResp(200, {"nope": 1}))
    with pytest.raises(DaikinError):
        _client().access_token()


def test_access_token_non_json_raises_daikin_error(monkeypatch):
    monkeypatch.setattr(daikin.requests, "post",
                        lambda *a, **k: FakeResp(200, ValueError("not json")))
    with pytest.raises(DaikinError):
        _client().access_token()


def test_access_token_caches_then_refreshes_after_expiry(monkeypatch):
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return FakeResp(200, {"accessToken": f"tok{calls['n']}", "accessTokenExpiresIn": 3600})

    monkeypatch.setattr(daikin.requests, "post", fake_post)
    c = _client()
    assert c.access_token() == "tok1"
    assert c.access_token() == "tok1"          # cached: no second POST
    assert calls["n"] == 1
    c._exp = 0.0                                # force expiry
    assert c.access_token() == "tok2"          # refreshed
    assert calls["n"] == 2


def test_list_devices_flattens_locations(monkeypatch):
    body = [{"devices": [{"id": "d1", "name": "Main", "model": "ONE"}]},
            {"devices": [{"id": "d2", "name": "Shop", "model": "ONE"}]}]
    monkeypatch.setattr(daikin.requests, "get", lambda *a, **k: FakeResp(200, body))
    monkeypatch.setattr(DaikinClient, "_headers", lambda self: {})
    devs = _client().list_devices()
    assert [d["id"] for d in devs] == ["d1", "d2"]


def test_list_devices_bad_shape_raises_daikin_error(monkeypatch):
    monkeypatch.setattr(daikin.requests, "get", lambda *a, **k: FakeResp(200, {"unexpected": "dict"}))
    monkeypatch.setattr(DaikinClient, "_headers", lambda self: {})
    with pytest.raises(DaikinError):
        _client().list_devices()


def test_read_device_bad_shape_raises_daikin_error(monkeypatch):
    monkeypatch.setattr(daikin.requests, "get", lambda *a, **k: FakeResp(200, ["not", "a", "dict"]))
    monkeypatch.setattr(DaikinClient, "_headers", lambda self: {})
    with pytest.raises(DaikinError):
        _client().read_device("d1")


# --- Enum drift + humidity type-drift: a Daikin firmware change must SURFACE
# (a warning, or a recorded poll_error), never silently deflate the numbers.

def test_unmapped_equipment_status_maps_unknown_and_warns(caplog):
    daikin._seen_unknown.clear()
    with caplog.at_level(logging.WARNING, logger="house_climate.daikin"):
        st = daikin._parse_device({**FIX, "equipmentStatus": 99})
    assert st.equipment_status == "unknown"
    assert any("equipmentStatus" in r.message and "99" in r.message
               for r in caplog.records)


def test_unmapped_enum_warns_once_per_value(caplog):
    daikin._seen_unknown.clear()
    with caplog.at_level(logging.WARNING, logger="house_climate.daikin"):
        daikin._parse_device({**FIX, "mode": 42})
        daikin._parse_device({**FIX, "mode": 42})
    warns = [r for r in caplog.records if "mode" in r.message and "42" in r.message]
    assert len(warns) == 1


def test_numeric_string_humidity_is_coerced():
    st = daikin._parse_device({**FIX, "humIndoor": "48"})
    assert st.indoor_humidity == 48.0


def test_string_humidity_raises_daikin_error_via_read_device(monkeypatch):
    # Humidity as a non-numeric string is type-drift: it must become a
    # DaikinError (-> recorded poll_error), not a string silently in the DB.
    monkeypatch.setattr(daikin.requests, "get",
                        lambda *a, **k: FakeResp(200, {**FIX, "humIndoor": "high"}))
    monkeypatch.setattr(DaikinClient, "_headers", lambda self: {})
    with pytest.raises(DaikinError):
        _client().read_device("d1")
