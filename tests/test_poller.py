from types import SimpleNamespace
from datetime import datetime, timezone
from house_climate import poller
from house_climate.daikin import DeviceState, RateLimited, DaikinError
from house_climate.weather import WeatherSnapshot

STATE = DeviceState(72.4, 48.0, 68.0, 72.0, "cooling", "cool", 91.0, 30.0)
WX = WeatherSnapshot(True, 90.5, 31.0, 56.0, 865.0, 7.0, 94.0, 58.0, "Clear", 37.0, 0)
WX_DOWN = WeatherSnapshot(False, None, None, None, None, None, None, None, None, None, None)

def test_build_reading_merges_sources():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    row = poller.build_reading("dev1", STATE, WX, now)
    assert row["indoor_temp_f"] == 72.4
    assert row["wx_solar_wm2"] == 865.0
    assert row["weather_ok"] is True
    assert row["ts"] == now

class FakeClient:
    def __init__(self, exc=None): self.exc = exc
    def read_device(self, _):
        if self.exc: raise self.exc
        return STATE

class FakeConn:
    def __init__(self): self.rows=[]; self.errors=[]
    # match db module functions via monkeypatch below

def test_poll_once_ok(monkeypatch):
    inserted = {}
    monkeypatch.setattr(poller.db, "insert_reading", lambda c, r: inserted.update(r))
    monkeypatch.setattr(poller.weather, "fetch", lambda *a, **k: WX)
    from types import SimpleNamespace
    cfg = SimpleNamespace(weather_url="u", weather_url_fallback=None)
    assert poller.poll_once(None, FakeClient(), "dev1", cfg) == "ok"
    assert inserted["equipment_status"] == "cooling"

def test_poll_once_rate_limited_records_error(monkeypatch):
    recorded = {}
    monkeypatch.setattr(poller.db, "record_error",
                        lambda c, d, k, det: recorded.update(kind=k))
    monkeypatch.setattr(poller.weather, "fetch", lambda *a, **k: WX)
    monkeypatch.setattr(poller, "_weather_ok_last", True)
    cfg = SimpleNamespace(weather_url="u", weather_url_fallback=None)
    kind = poller.poll_once(None, FakeClient(RateLimited()), "dev1", cfg)
    assert kind == "daikin_429"
    assert recorded["kind"] == "daikin_429"


def test_poll_once_daikin_error_records_error(monkeypatch):
    # Previously untested branch: a DaikinError (now also raised on an upstream
    # shape change) must record a daikin_error, keeping poll_errors populated.
    recorded = []
    monkeypatch.setattr(poller.db, "record_error",
                        lambda c, d, k, det: recorded.append(k))
    monkeypatch.setattr(poller.weather, "fetch", lambda *a, **k: WX)
    monkeypatch.setattr(poller, "_weather_ok_last", True)
    cfg = SimpleNamespace(weather_url="u", weather_url_fallback=None)
    kind = poller.poll_once(None, FakeClient(DaikinError("HTTP 500")), "dev1", cfg)
    assert kind == "daikin_error"
    assert "daikin_error" in recorded


def test_poll_once_records_weather_error_on_transition(monkeypatch):
    # Weather feed goes dark while the thermostat poll succeeds: a weather_error
    # must be recorded (once, on the ok->down transition) so the outage is
    # visible in /api/anomalies instead of silently blanking dew-point advice.
    recorded = []
    monkeypatch.setattr(poller.db, "insert_reading", lambda c, r: None)
    monkeypatch.setattr(poller.db, "record_error",
                        lambda c, d, k, det: recorded.append(k))
    monkeypatch.setattr(poller.weather, "fetch", lambda *a, **k: WX_DOWN)
    monkeypatch.setattr(poller, "_weather_ok_last", True)   # was healthy
    cfg = SimpleNamespace(weather_url="u", weather_url_fallback=None)
    assert poller.poll_once(None, FakeClient(), "dev1", cfg) == "ok"
    assert recorded == ["weather_error"]


def test_poll_once_no_duplicate_weather_error_while_still_down(monkeypatch):
    recorded = []
    monkeypatch.setattr(poller.db, "insert_reading", lambda c, r: None)
    monkeypatch.setattr(poller.db, "record_error",
                        lambda c, d, k, det: recorded.append(k))
    monkeypatch.setattr(poller.weather, "fetch", lambda *a, **k: WX_DOWN)
    monkeypatch.setattr(poller, "_weather_ok_last", False)   # already down
    cfg = SimpleNamespace(weather_url="u", weather_url_fallback=None)
    assert poller.poll_once(None, FakeClient(), "dev1", cfg) == "ok"
    assert recorded == []   # no repeat row every tick


def _ecowitt_cfg():
    return SimpleNamespace(ecowitt={"enabled": True, "gateway_url": "http://gw",
                                    "channels": {"1": "crawl"}})


def test_poll_ecowitt_records_error_on_fetch_failure(monkeypatch):
    recorded = []
    monkeypatch.setattr(poller.db, "record_error",
                        lambda c, d, k, det: recorded.append(k))
    monkeypatch.setattr(poller.ecowitt, "fetch_livedata",
                        lambda url: (_ for _ in ()).throw(RuntimeError("gw down")))
    assert poller.poll_ecowitt(None, _ecowitt_cfg()) == "ecowitt_error"
    assert recorded == ["ecowitt_fetch"]


def test_poll_ecowitt_records_error_on_insert_failure(monkeypatch):
    # The fix: the parse+insert loop is now inside the try, so a DB failure
    # partway through is recorded as ecowitt_error instead of escaping to run()
    # and looking like idle sensor data.
    recorded = []
    monkeypatch.setattr(poller.db, "record_error",
                        lambda c, d, k, det: recorded.append(k))
    monkeypatch.setattr(poller.ecowitt, "fetch_livedata", lambda url: {})
    monkeypatch.setattr(poller.ecowitt, "parse_channels",
                        lambda data, ch: [{"sensor_id": "ecowitt_ch1", "temp_f": 60.0,
                                           "humidity": 80.0, "battery_low": False}])
    monkeypatch.setattr(poller.ecowitt, "fetch_sensors_info", lambda url: {})
    monkeypatch.setattr(poller.ecowitt, "signal_by_sensor_id", lambda info: {})
    monkeypatch.setattr(poller.db, "insert_sensor_reading",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db write failed")))
    assert poller.poll_ecowitt(None, _ecowitt_cfg()) == "ecowitt_error"
    assert recorded == ["ecowitt_fetch"]
