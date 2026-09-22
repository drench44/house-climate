from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
import pytest
from house_climate import poller, db
from house_climate.daikin import DeviceState, RateLimited, DaikinError, DaikinUnreachable
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
    monkeypatch.setattr(poller.db, "insert_reading", lambda c, r: None)
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
    monkeypatch.setattr(poller.db, "insert_reading", lambda c, r: None)
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


def test_discover_device_id_empty_returns_none():
    # An empty device list must return None (-> run() logs + retries), NOT
    # raise IndexError as devices[0] used to.
    class NoDevices:
        def list_devices(self): return []
    assert poller._discover_device_id(None, NoDevices()) is None


def test_discover_device_id_upserts_and_returns_first(monkeypatch):
    upserted = []
    monkeypatch.setattr(poller.db, "upsert_device",
                        lambda c, i, n, m: upserted.append(i))

    class TwoDevices:
        def list_devices(self):
            return [{"id": "d1", "name": "Main", "model": "ONE"},
                    {"id": "d2", "name": "Shop", "model": "ONE"}]
    assert poller._discover_device_id(None, TwoDevices()) == "d1"
    assert upserted == ["d1", "d2"]


def test_discover_device_id_propagates_daikin_error():
    # A transient API failure must propagate so run() retries (not treated as
    # "no devices" -> misleading credentials message).
    class Failing:
        def list_devices(self): raise DaikinError("HTTP 503")
    with pytest.raises(DaikinError):
        poller._discover_device_id(None, Failing())


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


def test_poll_ecowitt_success_inserts_and_returns_count(monkeypatch):
    # The previously-untested happy path: parse -> attach signal -> compute
    # dewpoint -> insert, returning ecowitt_ok(n).
    inserted = []
    monkeypatch.setattr(poller.ecowitt, "fetch_livedata", lambda url: {})
    monkeypatch.setattr(poller.ecowitt, "parse_channels",
                        lambda data, ch: [{"sensor_id": "ecowitt_ch1", "temp_f": 60.0,
                                           "humidity": 80.0, "battery_low": False}])
    monkeypatch.setattr(poller.ecowitt, "fetch_sensors_info", lambda url: {})
    monkeypatch.setattr(poller.ecowitt, "signal_by_sensor_id", lambda info: {"ecowitt_ch1": 85})
    monkeypatch.setattr(poller.db, "insert_sensor_reading",
                        lambda c, sid, ts, **k: inserted.append((sid, k)))
    assert poller.poll_ecowitt(None, _ecowitt_cfg()) == "ecowitt_ok(1)"
    sid, kw = inserted[0]
    assert sid == "ecowitt_ch1"
    assert kw["extra"] == {"signal": 85}          # signal attached
    assert kw["dewpoint_f"] is not None           # dewpoint computed


def test_poll_ecowitt_disabled_returns_off():
    assert poller.poll_ecowitt(None, SimpleNamespace(ecowitt={"enabled": False})) == "ecowitt_off"
    assert poller.poll_ecowitt(None, SimpleNamespace(ecowitt=None)) == "ecowitt_off"


# --- update_precip: the rainfall-series builder behind the moisture verdict.
# DB-free via monkeypatched db.* + requests. Resets the module throttle globals.

def _precip_cfg(**kw):
    base = dict(timezone="America/Los_Angeles", latitude=None, longitude=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _reset_precip(monkeypatch):
    monkeypatch.setattr(poller, "_precip_last_rollup", None)
    monkeypatch.setattr(poller, "_precip_last_backfill_day", None)


def _local_today():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


def test_update_precip_trust_gate_marks_incomplete_past_day_partial(monkeypatch):
    _reset_precip(monkeypatch)
    today = _local_today()
    yest = today - timedelta(days=1)
    days = [{"day": yest, "rain_in": 0.0, "rain_last_hour": 5},    # past + incomplete
            {"day": today, "rain_in": 0.3, "rain_last_hour": 8}]   # today -> station
    upserts = []
    monkeypatch.setattr(poller.db, "outdoor_daily", lambda *a, **k: days)
    monkeypatch.setattr(poller.db, "upsert_precip",
                        lambda c, d, inches, src: upserts.append((d, inches, src)))
    poller.update_precip(None, "dev1", _precip_cfg())
    assert (today, 0.3, "station") in upserts
    # The incomplete past day is stored only as a placeholder the backfill
    # may replace, never as the authoritative 'station' total.
    assert {src for d, _, src in upserts if d == yest} == {"station_partial"}


def test_update_precip_upserts_complete_past_day_as_station(monkeypatch):
    _reset_precip(monkeypatch)
    yest = _local_today() - timedelta(days=1)
    days = [{"day": yest, "rain_in": 0.42, "rain_last_hour": 23}]   # extends into the evening
    upserts = []
    monkeypatch.setattr(poller.db, "outdoor_daily", lambda *a, **k: days)
    monkeypatch.setattr(poller.db, "upsert_precip",
                        lambda c, d, inches, src: upserts.append((d, inches, src)))
    poller.update_precip(None, "dev1", _precip_cfg())
    assert (yest, 0.42, "station") in upserts


def test_update_precip_hourly_throttle(monkeypatch):
    _reset_precip(monkeypatch)
    calls = {"n": 0}
    def od(*a, **k):
        calls["n"] += 1
        return []
    monkeypatch.setattr(poller.db, "outdoor_daily", od)
    monkeypatch.setattr(poller.db, "upsert_precip", lambda *a, **k: None)
    poller.update_precip(None, "dev1", _precip_cfg())
    after_first = calls["n"]
    assert poller.update_precip(None, "dev1", _precip_cfg()) == "precip_noop"
    assert calls["n"] == after_first   # throttled before any rollup


def test_update_precip_backfill_failure_is_swallowed(monkeypatch):
    _reset_precip(monkeypatch)
    monkeypatch.setattr(poller.db, "outdoor_daily", lambda *a, **k: [])
    monkeypatch.setattr(poller.db, "upsert_precip", lambda *a, **k: None)
    monkeypatch.setattr(poller.db, "precip_range", lambda *a, **k: [])
    first_ts = datetime.now(timezone.utc) - timedelta(days=10)

    class FakeCur:
        def fetchone(self): return (first_ts,)

    class FakeConn:
        def execute(self, *a, **k): return FakeCur()

    def boom(*a, **k): raise RuntimeError("open-meteo down")
    monkeypatch.setattr(poller.requests, "get", boom)
    res = poller.update_precip(FakeConn(), "dev1", _precip_cfg(latitude=47.6, longitude=-122.3))
    assert res.startswith("precip_station_only")   # failure swallowed, not raised


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


def test_build_reading_carries_rain_source():
    wx = WeatherSnapshot(True, 60.0, 80.0, 54.0, None, None, None, None, None,
                         None, 0, rain_today_in=0.2, rain_source="model")
    row = poller.build_reading("dev1", STATE, wx, datetime.now(timezone.utc))
    assert row["wx_rain_today_in"] == 0.2
    assert row["wx_rain_source"] == "model"


# --- Daikin outages: a network failure is recorded like any other Daikin
# failure, and the weather sampled on that tick is kept (thermostat fields
# left empty, never invented).

def test_poll_once_network_error_records_daikin_network(monkeypatch):
    recorded = []
    monkeypatch.setattr(poller.db, "record_error",
                        lambda c, d, k, det: recorded.append(k))
    monkeypatch.setattr(poller.db, "insert_reading", lambda c, r: None)
    monkeypatch.setattr(poller.weather, "fetch", lambda *a, **k: WX)
    monkeypatch.setattr(poller, "_weather_ok_last", True)
    cfg = SimpleNamespace(weather_url="u", weather_url_fallback=None)
    kind = poller.poll_once(None, FakeClient(DaikinUnreachable("no route")), "dev1", cfg)
    assert kind == "daikin_network"
    # 'daikin%' so the thermostat-offline alert counts it
    assert recorded == ["daikin_network"]


@pytest.mark.parametrize("exc", [DaikinUnreachable("down"), RateLimited(), DaikinError("HTTP 500")])
def test_poll_once_keeps_weather_when_daikin_fails(conn, monkeypatch, exc):
    monkeypatch.setattr(poller.weather, "fetch", lambda *a, **k: WX)
    monkeypatch.setattr(poller, "_weather_ok_last", True)
    cfg = SimpleNamespace(weather_url="u", weather_url_fallback=None)
    poller.poll_once(conn, FakeClient(exc), "dev1", cfg)
    rows = conn.execute(
        "SELECT wx_outdoor_temp_f, wx_dewpoint_f, weather_ok, indoor_temp_f,"
        " equipment_status, mode, cool_setpoint_f FROM readings").fetchall()
    assert rows == [(90.5, 56.0, True, None, None, None, None)]
    # No thermostat consumer sees the row.
    assert db.recent_readings(conn, "dev1", datetime(2000, 1, 1, tzinfo=timezone.utc)) == []


def test_poll_once_stores_nothing_when_daikin_and_weather_both_fail(conn, monkeypatch):
    monkeypatch.setattr(poller.weather, "fetch", lambda *a, **k: WX_DOWN)
    monkeypatch.setattr(poller, "_weather_ok_last", False)
    cfg = SimpleNamespace(weather_url="u", weather_url_fallback=None)
    poller.poll_once(conn, FakeClient(DaikinUnreachable("down")), "dev1", cfg)
    assert conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 0


# --- update_precip against the real DB: the rain provenance, the partial-day
# heal, and the trust check, end to end through outdoor_daily + upsert_precip.

def _utc_cfg(**kw):
    base = dict(timezone="UTC", latitude=None, longitude=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _utc_yesterday_start():
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1)


def _rain_reading(ts, rain, source):
    r = {c: None for c in db.READING_COLUMNS}
    r.update(ts=ts, device_id="dev1", indoor_temp_f=70.0, equipment_status="idle",
             mode="cool", wx_outdoor_temp_f=60.0, weather_ok=True,
             wx_rain_today_in=rain, wx_rain_source=source)
    return r


def _precip_row(conn, day):
    return conn.execute("SELECT inches, source FROM precip_daily WHERE day=%s",
                        (day,)).fetchone()


def test_update_precip_model_rain_is_never_stored_as_station(conn, monkeypatch):
    _reset_precip(monkeypatch)
    y0 = _utc_yesterday_start()
    for h in range(24):
        db.insert_reading(conn, _rain_reading(y0 + timedelta(hours=h), 0.5, "model"))
    poller.update_precip(conn, "dev1", _utc_cfg())
    assert _precip_row(conn, y0.date()) == (0.5, "model")


def test_update_precip_heals_a_day_the_poller_died_partway_through(conn, monkeypatch):
    # The live rollup stored yesterday as 'station' at 10:00, then the poller
    # died. Next day the backfill must replace that partial total.
    _reset_precip(monkeypatch)
    y0 = _utc_yesterday_start()
    yday = y0.date()
    for h in range(11):
        db.insert_reading(conn, _rain_reading(y0 + timedelta(hours=h), 0.05, "gauge"))
    db.upsert_precip(conn, yday, 0.05, "station")
    db.insert_sensor_reading(conn, "ecowitt_ch1", y0 - timedelta(days=2), temp_f=60, humidity=50)

    asked = {}

    class Resp:
        def __init__(self, params): self.params = params
        def raise_for_status(self): pass
        def json(self):
            from datetime import date
            a = date.fromisoformat(self.params["start_date"])
            b = date.fromisoformat(self.params["end_date"])
            days = [(a + timedelta(days=i)) for i in range((b - a).days + 1)]
            return {"daily": {"time": [d.isoformat() for d in days],
                              "precipitation_sum": [0.9] * len(days)}}

    def fake_get(url, params=None, timeout=None):
        asked.update(params)
        return Resp(params)
    monkeypatch.setattr(poller.requests, "get", fake_get)
    poller.update_precip(conn, "dev1", _utc_cfg(latitude=1.0, longitude=2.0))
    assert _precip_row(conn, yday) == (0.9, "openmeteo")


def test_update_precip_trust_uses_last_hour_with_gauge_rain(conn, monkeypatch):
    # Gauge rain until 09:00, then the feed kept answering with no rain value
    # all evening. The day is NOT a complete gauge day.
    _reset_precip(monkeypatch)
    y0 = _utc_yesterday_start()
    for h in range(24):
        rain, src = (0.3, "gauge") if h <= 9 else (None, None)
        db.insert_reading(conn, _rain_reading(y0 + timedelta(hours=h), rain, src))
    poller.update_precip(conn, "dev1", _utc_cfg())
    assert _precip_row(conn, y0.date()) == (0.3, "station_partial")


def test_update_precip_complete_gauge_day_is_station(conn, monkeypatch):
    _reset_precip(monkeypatch)
    y0 = _utc_yesterday_start()
    for h in range(24):
        db.insert_reading(conn, _rain_reading(y0 + timedelta(hours=h), 0.1 * (h > 12), "gauge"))
    poller.update_precip(conn, "dev1", _utc_cfg())
    assert _precip_row(conn, y0.date()) == (0.1, "station")


def test_update_precip_mixed_day_stores_partial_gauge_not_model(conn, monkeypatch):
    # The real outage shape: the gauge leg reported until 09:00, then the feed
    # fell back to its model for the rest of the day.
    _reset_precip(monkeypatch)
    y0 = _utc_yesterday_start()
    for h in range(24):
        rain, src = (0.3, "gauge") if h <= 9 else (1.2, "model")
        db.insert_reading(conn, _rain_reading(y0 + timedelta(hours=h), rain, src))
    poller.update_precip(conn, "dev1", _utc_cfg())
    assert _precip_row(conn, y0.date()) == (0.3, "station_partial")


def test_update_precip_today_rows(conn, monkeypatch):
    # Today with gauge rows that stop at 10:00 is live 'station' (judged again
    # tomorrow); today with only model rows is 'model'.
    _reset_precip(monkeypatch)
    t0 = _utc_yesterday_start() + timedelta(days=1)
    now = datetime.now(timezone.utc)
    ts = [t for t in (t0 + timedelta(minutes=m) for m in (1, 2)) if t <= now]
    db.insert_reading(conn, _rain_reading(ts[0], 0.2, "gauge"))
    poller.update_precip(conn, "dev1", _utc_cfg())
    assert _precip_row(conn, t0.date()) == (0.2, "station")


def test_update_precip_one_failing_range_does_not_block_recent_days(conn, monkeypatch):
    # After an upgrade the full rollup may demote a months-old day. Its range
    # failing (e.g. too old for the endpoint) must not stop yesterday's heal.
    _reset_precip(monkeypatch)
    y0 = _utc_yesterday_start()
    yday, old = y0.date(), (y0 - timedelta(days=120)).date()
    for day0 in (y0, y0 - timedelta(days=120)):
        for h in range(5):
            db.insert_reading(conn, _rain_reading(day0 + timedelta(hours=h), 0.05, "gauge"))
    db.insert_sensor_reading(conn, "ecowitt_ch1", y0 - timedelta(days=121), temp_f=60, humidity=50)
    # Every other day in the window already has a final value.
    d = old - timedelta(days=7)
    while d < yday:
        if d != old:
            db.upsert_precip(conn, d, 0.0, "openmeteo")
        d += timedelta(days=1)

    class Resp:
        def __init__(self, params): self.params = params
        def raise_for_status(self):
            if self.params["start_date"] <= old.isoformat():
                raise RuntimeError("400 out of range")
        def json(self):
            return {"daily": {"time": [self.params["start_date"]],
                              "precipitation_sum": [0.7]}}
    monkeypatch.setattr(poller.requests, "get",
                        lambda url, params=None, timeout=None: Resp(params))
    poller.update_precip(conn, "dev1", _utc_cfg(latitude=1.0, longitude=2.0))
    assert _precip_row(conn, yday) == (0.7, "openmeteo")
    assert _precip_row(conn, old) == (0.05, "station_partial")
