import json
from pathlib import Path
from house_climate import weather

FIX = json.loads((Path(__file__).parent / "fixtures" / "wx.json").read_text())


class _Resp:
    """Minimal stand-in for a requests Response."""
    def __init__(self, ok=True, body=None, raises=None):
        self.ok = ok
        self._body = body
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._body


def test_parse_maps_fields():
    s = weather.parse(FIX)
    assert s.ok is True
    assert s.outdoor_temp_f == FIX["temp"]
    assert s.solar_wm2 == FIX["radiation"]
    assert s.fc_high_f == FIX["fcHigh"]
    assert s.alert_count == FIX["alertCount"]


def test_parse_empty_body_is_null_not_ok():
    # A 200 with an empty body carries no reading: it must NOT be marked healthy
    # (the old bug hardcoded ok=True, so an outage read as weather_ok=True with
    # every field null -> a silent "cool spell" downstream).
    s = weather.parse({})
    assert s.ok is False
    assert s.outdoor_temp_f is None


def test_parse_error_wrapper_is_null():
    # A reachable feed returning a 200 error object has no core reading -> null.
    assert weather.parse({"error": "rate limited"}).ok is False


def test_parse_renamed_core_fields_is_null():
    # An upstream rename of the core current-condition fields leaves no usable
    # reading -> null, rather than a false-healthy snapshot of all-None.
    assert weather.parse({"temperature": 72, "rh": 40}).ok is False


def test_parse_string_numerics_are_coerced():
    s = weather.parse({"temp": "72.5", "humidity": "40", "rainToday": "0.10"})
    assert s.ok is True
    assert s.outdoor_temp_f == 72.5
    assert s.humidity == 40.0
    assert s.rain_today_in == 0.10


def test_parse_ok_on_humidity_alone():
    # Any one core current-condition field is enough to count as a live feed.
    assert weather.parse({"humidity": 55}).ok is True


def test_fetch_happy_path_parses_body(monkeypatch):
    monkeypatch.setattr(weather.requests, "get",
                        lambda u, timeout=5: _Resp(ok=True, body=FIX))
    s = weather.fetch("http://wx/")
    assert s.ok is True
    assert s.outdoor_temp_f == FIX["temp"]


def test_fetch_200_but_empty_body_falls_through_to_fallback(monkeypatch):
    # Primary is reachable (HTTP 200) but returns an unusable body; fetch must
    # NOT accept it as healthy — it falls through to the fallback.
    calls = []

    def fake_get(u, timeout=5):
        calls.append(u)
        return _Resp(ok=True, body={}) if u == "primary" else _Resp(ok=True, body=FIX)

    monkeypatch.setattr(weather.requests, "get", fake_get)
    s = weather.fetch("primary", "fallback")
    assert calls == ["primary", "fallback"]
    assert s.ok is True and s.outdoor_temp_f == FIX["temp"]


def test_fetch_http_500_falls_to_fallback(monkeypatch):
    def fake_get(u, timeout=5):
        return _Resp(ok=False) if u == "primary" else _Resp(ok=True, body=FIX)

    monkeypatch.setattr(weather.requests, "get", fake_get)
    s = weather.fetch("primary", "fallback")
    assert s.ok is True and s.outdoor_temp_f == FIX["temp"]


def test_fetch_non_json_body_falls_through(monkeypatch):
    monkeypatch.setattr(weather.requests, "get",
                        lambda u, timeout=5: _Resp(ok=True, raises=ValueError("not json")))
    assert weather.fetch("http://wx/") == weather._NULL


def test_fetch_all_bad_returns_null(monkeypatch):
    monkeypatch.setattr(weather.requests, "get",
                        lambda u, timeout=5: _Resp(ok=True, body={}))
    s = weather.fetch("primary", "fallback")
    assert s == weather._NULL and s.ok is False


def test_fetch_unreachable_url_returns_null():
    """fetch() never raises; returns _NULL on network error."""
    s = weather.fetch("http://127.0.0.1:1/")
    assert s == weather._NULL
    assert s.ok is False


def test_fetch_fallback_both_unreachable_returns_null():
    """fetch() tries fallback if primary fails, returns _NULL if both unreachable."""
    s = weather.fetch("http://127.0.0.1:1/", "http://127.0.0.1:2/")
    assert s == weather._NULL
    assert s.ok is False
