"""Stale data must never look current, and 'tomorrow' must mean tomorrow.

Covers: the humidity panel's age gate, outdoor freshness measured from the
station's observation time (not our poll time), the moisture page's 'now'
dew points and gap-now age gates, tomorrow's forecast high taken from the live
feed's daily forecast (never today's high), and the peak-minutes fit using
only days that actually have a peak window.

DB-backed tests use the `conn` fixture (skipped without TEST_DB_DSN); the
feed-parsing and correlation tests are pure."""
import dataclasses
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import pytest

from house_climate import db
from house_climate.analytics import correlation
from house_climate.config import load_config
from house_climate.web import api

from conftest import CFG_PATH

CFG = load_config(CFG_PATH)
TZ = ZoneInfo(CFG.timezone)
CRAWL_CFG = dataclasses.replace(CFG, ecowitt={
    "enabled": True, "gateway_url": "http://gw",
    "channels": {"8": "Upstairs", "7": "Downstairs"},
    "outdoor_name": "Crawl Space"})


@pytest.fixture(autouse=True)
def _isolate_caches(monkeypatch):
    api._ah_fit_cache.clear()
    # No test here may reach a real weather feed: default to "feed down".
    monkeypatch.setattr(api, "_live_feed", lambda cfg: None)
    yield
    api._ah_fit_cache.clear()


def _reading(ts, **over):
    r = dict(ts=ts, device_id="dev1", indoor_temp_f=75, indoor_humidity=55,
             heat_setpoint_f=68, cool_setpoint_f=72, equipment_status="idle",
             mode="cool", daikin_outdoor_temp_f=65, daikin_outdoor_humidity=30,
             wx_outdoor_temp_f=65, wx_humidity=30, wx_dewpoint_f=40,
             wx_solar_wm2=400, wx_uv=4, wx_fc_high_f=80, wx_fc_low_f=55,
             wx_conditions="Clear", wx_aqi=20, wx_alert_count=0, weather_ok=True)
    r.update(over)
    return r


# --- #7 humidity panel ---------------------------------------------------------

def test_humidity_fresh_reading_reports_its_age(conn):
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, _reading(now - timedelta(minutes=2)))
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["available"] is True and h["stale"] is False
    assert 100 <= h["age_s"] <= 200
    assert h["indoor_rh"] == 55 and h["window"] is not None


def test_humidity_stale_reading_is_marked_and_not_presented_as_current(conn):
    """A dead poller used to leave the last indoor humidity AND the 'open the
    windows' advice on screen as if current, for up to 7 days."""
    now = datetime.now(timezone.utc)
    for m in range(0, 60, 10):
        db.insert_reading(conn, _reading(now - timedelta(hours=5, minutes=m)))
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["available"] is True
    assert h["stale"] is True and h["age_s"] >= 5 * 3600
    for k in ("indoor_rh", "indoor_dp", "outdoor_dp", "outdoor_rh",
              "dew_point_delta", "window"):
        assert h[k] is None, k
    assert isinstance(h["trend"], list)      # history is still history


def test_humidity_stale_row_does_not_feed_a_modeled_aqi(conn):
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, _reading(now - timedelta(hours=2), wx_aqi=160))
    h = api.build_humidity(conn, "dev1", CFG)
    assert h["outdoor_aqi"] is None


def test_humidity_stale_row_keeps_a_fresh_monitor_aqi(conn):
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, _reading(now - timedelta(hours=2), wx_aqi=160))
    db.kv_set(conn, "ha_outdoor_aqi", {"aqi": 44})
    h = api.build_humidity(conn, "dev1", CFG)
    assert (h["outdoor_aqi"], h["aqi_source"]) == (44, "airnow")


# --- #8 outdoor freshness from observation time ----------------------------------

def _feed(now, obs_age_s, **over):
    f = {"ts": int(now.timestamp()), "obsTs": int(now.timestamp() - obs_age_s),
         "obsAgeSec": int(obs_age_s), "obsSource": "STN1", "obsFields": ["temp", "humidity"],
         "weatherStale": False, "weatherAgeSec": 20, "aqiStale": False}
    f.update(over)
    return f


def test_outdoor_age_is_the_station_observation_age(conn, monkeypatch):
    """Live: /api/outdoor said age_s 45 (seconds since our poll) while the
    station reading behind it was ~24 minutes old."""
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, _reading(now - timedelta(seconds=45)))
    monkeypatch.setattr(api, "_live_feed", lambda cfg: _feed(now, 24 * 60))
    o = api.build_outdoor(conn, "dev1", "24h", now=now, cfg=CFG)["now"]
    assert o["obs_age_s"] == pytest.approx(24 * 60, abs=5)
    assert o["age_s"] == pytest.approx(24 * 60, abs=5)
    assert o["poll_age_s"] == 45
    assert o["obs_source"] == "STN1"
    assert o["stale"] is False       # an hourly station is normally this old


def test_outdoor_stale_when_the_station_observation_is_old(conn, monkeypatch):
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, _reading(now - timedelta(seconds=45)))
    monkeypatch.setattr(api, "_live_feed", lambda cfg: _feed(now, 3 * 3600))
    o = api.build_outdoor(conn, "dev1", "24h", now=now, cfg=CFG)["now"]
    assert o["stale"] is True and o["age_s"] >= 3 * 3600


def test_outdoor_stale_when_the_feed_says_weather_is_stale(conn, monkeypatch):
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, _reading(now - timedelta(seconds=45)))
    monkeypatch.setattr(api, "_live_feed",
                        lambda cfg: _feed(now, 60, weatherStale=True))
    assert api.build_outdoor(conn, "dev1", "24h", now=now, cfg=CFG)["now"]["stale"] is True


def test_outdoor_model_fill_reports_model_source(conn, monkeypatch):
    """No station fields: the value is the model's, aged by the feed's own
    weather age, and labelled as such."""
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, _reading(now - timedelta(seconds=45)))
    monkeypatch.setattr(api, "_live_feed",
                        lambda cfg: _feed(now, 9 * 3600, obsFields=[], weatherAgeSec=30))
    o = api.build_outdoor(conn, "dev1", "24h", now=now, cfg=CFG)["now"]
    assert o["obs_source"] == "model"
    assert o["stale"] is False
    assert o["obs_age_s"] == pytest.approx(30, abs=5)


def test_outdoor_without_the_feed_falls_back_to_poll_age(conn):
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, _reading(now - timedelta(seconds=45)))
    o = api.build_outdoor(conn, "dev1", "24h", now=now, cfg=CFG)["now"]
    assert o["obs_age_s"] is None and o["age_s"] == 45 and o["stale"] is False


# --- #9 moisture 'now' age gates ---------------------------------------------------

def _seed_sensors(conn, end, hours=6):
    from house_climate.analytics import humidity as hum
    for i in range(hours * 4):
        ts = end - timedelta(minutes=15 * i)
        db.insert_sensor_reading(conn, "ecowitt_outdoor", ts, temp_f=63.0, humidity=72.0,
                                 dewpoint_f=hum.dew_point_f(63.0, 72.0))
        db.insert_sensor_reading(conn, "ecowitt_ch7", ts, temp_f=71.0, humidity=50.0,
                                 dewpoint_f=hum.dew_point_f(71.0, 50.0))
        db.insert_reading(conn, _reading(ts))


def test_moisture_now_values_present_when_fresh(conn):
    now = datetime.now(timezone.utc)
    _seed_sensors(conn, now - timedelta(minutes=3))
    m = api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)
    dp = m["dp_now"]
    assert dp["crawl"] is not None and dp["reference"] is not None
    assert dp["thermostat"] is not None and dp["outdoor"] is not None
    assert dp["crawl_age_s"] < 600 and m["delta"]["now"] is not None
    down = next(f for f in m["ah"]["floors"] if f["name"] == "Downstairs")
    assert down["gap_now"] is not None and down["gap_now_at"] is not None


def test_moisture_now_values_withheld_when_sensors_are_dead(conn):
    """A dead crawl/floor probe's last value used to show as current, and the
    crawl-vs-indoor gap was computed from it."""
    now = datetime.now(timezone.utc)
    _seed_sensors(conn, now - timedelta(hours=4))
    m = api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)
    dp = m["dp_now"]
    assert dp["crawl"] is None and dp["reference"] is None
    assert dp["thermostat"] is None and dp["outdoor"] is None
    assert dp["crawl_age_s"] >= 4 * 3600 and dp["reference_age_s"] >= 4 * 3600
    assert m["delta"]["now"] is None
    for f in m["ah"]["floors"]:
        assert f["gap_now"] is None, f["name"]
    # the history is still there
    assert m["delta"]["series"]


def test_dashboard_gap_now_withheld_when_old(conn):
    now = datetime.now(timezone.utc)
    _seed_sensors(conn, now - timedelta(hours=4))
    summary = api._ah_gap_summary(conn, "dev1", CRAWL_CFG, now)
    assert summary["available"] is True
    for f in summary["floors"]:
        assert f["gap_now"] is None, f["name"]


def test_dashboard_gap_now_present_when_fresh(conn):
    now = datetime.now(timezone.utc)
    _seed_sensors(conn, now - timedelta(minutes=3))
    summary = api._ah_gap_summary(conn, "dev1", CRAWL_CFG, now)
    down = next(f for f in summary["floors"] if f["name"] == "Downstairs")
    assert down["gap_now"] is not None


def test_fresh_gap_now_helper():
    now = datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc)
    mk = lambda h: {"bucket": now.replace(minute=0) - timedelta(hours=h), "gap": 1.234}
    assert api._fresh_gap_now([mk(3), mk(0)], now) == (1.23, mk(0)["bucket"])
    assert api._fresh_gap_now([mk(3)], now) == (None, None)
    assert api._fresh_gap_now([], now) == (None, None)


# --- #13 tomorrow's forecast high ------------------------------------------------------

_TODAY = date(2026, 9, 22)          # a Tuesday
_TOMORROW = date(2026, 9, 23)


def _wx(**over):
    d = {"fcHigh": 71.6, "fcStale": False,
         "fcDaily": [{"day": "TUE", "date": "2026-09-22", "hi": 72},
                     {"day": "WED", "date": "2026-09-23", "hi": 68}],
         "dailyForecast": [{"day": "Tue", "hi": 72}, {"day": "Wed", "hi": 68}]}
    d.update(over)
    return d


def test_tomorrow_high_comes_from_the_dated_daily_forecast():
    assert api._forecast_high_for(_wx(), _TOMORROW) == 68


def test_tomorrow_high_falls_back_to_the_weekday_label():
    wx = _wx(fcDaily=None)
    assert api._forecast_high_for(wx, _TOMORROW) == 68


@pytest.mark.parametrize("wx", [
    None,
    {"fcHigh": 71.6},                                            # no daily data at all
    _wx(fcDaily=[{"date": "2026-09-22", "hi": 72}],
        dailyForecast=[{"day": "Tue", "hi": 72}]),               # only today
    _wx(fcStale=True),                                           # forecast gone stale
    _wx(fcDaily=[{"date": "2026-09-23", "hi": True}],
        dailyForecast=[{"day": "Wed", "hi": "hot"}]),            # junk values
    _wx(fcDaily=None, dailyForecast=[{"day": "Wed", "hi": 68},
                                     {"day": "Wed", "hi": 90}]),  # ambiguous label
])
def test_tomorrow_high_unavailable_never_today(wx):
    """Fail soft to 'unavailable', never to today's fcHigh."""
    assert api._forecast_high_for(wx, _TOMORROW) is None


def _seed_complete_days(conn, n_days, cool_hours=range(12, 22)):
    now_local = datetime.now(TZ)
    for off in range(1, n_days + 1):
        d = (now_local - timedelta(days=off)).date()
        for h in range(24):
            local = datetime(d.year, d.month, d.day, h, 30, tzinfo=TZ)
            db.insert_reading(conn, _reading(
                local.astimezone(timezone.utc),
                equipment_status="cooling" if h in cool_hours else "idle",
                wx_outdoor_temp_f=70 + off + (10 if h == 15 else 0), wx_fc_high_f=None))
    db.insert_reading(conn, _reading(datetime.now(timezone.utc), wx_fc_high_f=71.6))


def test_forecast_uses_tomorrows_high_not_todays(conn, monkeypatch):
    _seed_complete_days(conn, 4)
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
    wx = {"fcHigh": 71.6, "fcStale": False,
          "fcDaily": [{"date": tomorrow.isoformat(), "hi": 68}]}
    monkeypatch.setattr(api, "_live_feed", lambda cfg: wx)
    fc = api.build_forecast(conn, "dev1", CFG)
    assert fc["available"] is True
    assert fc["fc_high_f"] == 68
    assert fc["target_date"] == tomorrow.isoformat()


def test_forecast_unavailable_when_the_feed_has_no_tomorrow(conn, monkeypatch):
    """Stored wx_fc_high_f (TODAY's high) is present on every row; it must not
    be relabelled as tomorrow's."""
    _seed_complete_days(conn, 4)
    monkeypatch.setattr(api, "_live_feed", lambda cfg: {"fcHigh": 71.6, "fcStale": False})
    fc = api.build_forecast(conn, "dev1", CFG)
    assert fc == {"available": False, "reason": "no_tomorrow_forecast"}


def test_live_feed_caches_and_fails_soft(monkeypatch):
    """_live_feed: primary then fallback, cached for a short TTL, None when
    both fail. Restores the real function the autouse fixture stubbed."""
    monkeypatch.undo()
    calls = []

    class R:
        def __init__(self, ok, body):
            self.ok, self._b = ok, body

        def json(self):
            if isinstance(self._b, Exception):
                raise self._b
            return self._b

    def fake_get(url, timeout):
        calls.append(url)
        return R(False, None) if url == "p" else R(True, {"fcHigh": 1})

    monkeypatch.setattr(api.requests, "get", fake_get)
    monkeypatch.setattr(api, "_feed_cache", {"at": None, "body": None})
    cfg = dataclasses.replace(CFG, weather_url="p", weather_url_fallback="f")
    assert api._live_feed(cfg) == {"fcHigh": 1}
    assert api._live_feed(cfg) == {"fcHigh": 1}
    assert calls == ["p", "f"]                      # second call served from cache

    monkeypatch.setattr(api, "_feed_cache", {"at": None, "body": None})
    monkeypatch.setattr(api.requests, "get",
                        lambda url, timeout: R(True, ValueError("not json")))
    assert api._live_feed(cfg) is None


# --- #14 peak minutes fit only on days with a peak window --------------------------------

def test_day_has_peak_follows_the_tou_table():
    assert CFG.tou.day_has_peak(date(2026, 8, 14), TZ) is True     # Friday
    assert CFG.tou.day_has_peak(date(2026, 8, 15), TZ) is False    # Saturday


def test_weekend_zeros_do_not_drag_the_weekday_peak_fit():
    """Weekend days enter history with peak_cool_minutes=0 (no peak window), and
    fitting them together with weekdays read ~29% low on weekdays."""
    weekday = [{"day_high": h, "cool_minutes": 3 * h, "peak_cool_minutes": h, "has_peak": True}
               for h in (80.0, 85.0, 90.0, 95.0, 100.0)]
    weekend = [{"day_high": h, "cool_minutes": 3 * h, "peak_cool_minutes": 0.0, "has_peak": False}
               for h in (82.0, 97.0)]
    fri = date(2026, 8, 14)
    both = correlation.predict_peak_cost(90.0, weekday + weekend, CFG.tou, CFG.system_kw,
                                         CFG.timezone, target_date=fri)
    only = correlation.predict_peak_cost(90.0, weekday, CFG.tou, CFG.system_kw,
                                         CFG.timezone, target_date=fri)
    assert both["predicted_peak_cool_minutes"] == pytest.approx(90.0)
    assert both["predicted_peak_dollars"] == pytest.approx(only["predicted_peak_dollars"])
    assert both["has_peak"] is True


def test_a_day_without_a_peak_window_has_zero_peak_exposure():
    hist = [{"day_high": h, "cool_minutes": 3 * h, "peak_cool_minutes": h, "has_peak": True}
            for h in (80.0, 85.0, 90.0)]
    sat = correlation.predict_peak_cost(90.0, hist, CFG.tou, CFG.system_kw,
                                        CFG.timezone, target_date=date(2026, 8, 15))
    assert sat["has_peak"] is False
    assert sat["predicted_peak_cool_minutes"] == 0
    assert sat["predicted_peak_dollars"] == 0
    assert sat["peak_windows"] == []


def test_prediction_carries_the_target_days_peak_windows():
    hist = [{"day_high": h, "cool_minutes": 3 * h, "peak_cool_minutes": h, "has_peak": True}
            for h in (80.0, 85.0, 90.0)]
    fri = correlation.predict_peak_cost(90.0, hist, CFG.tou, CFG.system_kw,
                                        CFG.timezone, target_date=date(2026, 8, 14))
    wins = [b for b in CFG.tou.bands
            if b.rate == max(x.rate for x in CFG.tou.bands) and b.days != "weekend"]
    assert fri["peak_windows"] == [{"start": w.start.strftime("%H:%M"),
                                    "end": w.end.strftime("%H:%M")} for w in wins]


def test_no_peak_history_means_unknown_peak_not_zero():
    hist = [{"day_high": h, "cool_minutes": 3 * h, "peak_cool_minutes": 0.0, "has_peak": False}
            for h in (80.0, 85.0, 90.0)]
    fri = correlation.predict_peak_cost(90.0, hist, CFG.tou, CFG.system_kw,
                                        CFG.timezone, target_date=date(2026, 8, 14))
    assert fri["predicted_peak_cool_minutes"] is None
    assert fri["predicted_peak_dollars"] is None


def test_build_forecast_marks_weekend_history_days(conn, monkeypatch):
    _seed_complete_days(conn, 7)
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
    monkeypatch.setattr(api, "_live_feed", lambda cfg: {
        "fcStale": False, "fcDaily": [{"date": tomorrow.isoformat(), "hi": 90}]})
    seen = {}
    real = correlation.predict_peak_cost

    def spy(fc_high, history, *a, **k):
        seen["history"] = history
        return real(fc_high, history, *a, **k)
    monkeypatch.setattr(api.correlation, "predict_peak_cost", spy)
    fc = api.build_forecast(conn, "dev1", CFG)
    flags = [h["has_peak"] for h in seen["history"]]
    assert flags.count(False) == 2 and flags.count(True) == 5   # 7 days: one weekend
    assert fc["peak_days_of_history"] == 5
    assert fc["days_of_history"] == 7
    assert "has_peak" in fc and "peak_windows" in fc


# --- #12 the rail's band split follows the configured bands ---------------------------

def _renamed_tou_cfg():
    from house_climate.config import TouBand, TouTable, _parse_hhmm
    bands = (
        TouBand("on-peak", "summer", "weekday", _parse_hhmm("16:00"), _parse_hhmm("20:00"), 0.50),
        TouBand("shoulder", "summer", "weekday", _parse_hhmm("08:00"), _parse_hhmm("16:00"), 0.20),
        TouBand("shoulder", "summer", "weekday", _parse_hhmm("20:00"), _parse_hhmm("22:00"), 0.20),
        TouBand("super-off", "summer", "weekday", _parse_hhmm("22:00"), _parse_hhmm("08:00"), 0.05),
        TouBand("super-off", "summer", "weekend", _parse_hhmm("00:00"), _parse_hhmm("00:00"), 0.05),
        TouBand("winter-flat", "winter", "all", _parse_hhmm("00:00"), _parse_hhmm("00:00"), 0.15),
    )
    return dataclasses.replace(CFG, tou=TouTable(frozenset(range(1, 13)), bands))


def test_cost_summary_lists_todays_configured_bands_with_tiers(conn):
    cfg = _renamed_tou_cfg()
    s = api.build_cost_summary(conn, "dev1", cfg)
    assert s["bands"] == [
        {"name": "on-peak", "tier": "peak", "rate": 0.50},
        {"name": "shoulder", "tier": "mid", "rate": 0.20},
        {"name": "super-off", "tier": "off", "rate": 0.05},
    ]      # this season only, one row per name, highest rate first


# --- review follow-ups ------------------------------------------------------------------

def test_outdoor_station_fields_without_obs_ts_use_obs_age_not_model(conn, monkeypatch):
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, _reading(now - timedelta(seconds=45)))
    monkeypatch.setattr(api, "_live_feed", lambda cfg: _feed(now, 1800, obsTs=None))
    o = api.build_outdoor(conn, "dev1", "24h", now=now, cfg=CFG)["now"]
    assert o["obs_source"] == "STN1"
    assert o["obs_age_s"] == pytest.approx(1800, abs=5)


def test_outdoor_feed_down_says_the_obs_age_is_unknown(conn):
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, _reading(now - timedelta(seconds=45)))
    o = api.build_outdoor(conn, "dev1", "24h", now=now, cfg=CFG)["now"]
    assert o["obs_age_known"] is False


def test_outdoor_obs_age_boundary(conn, monkeypatch):
    now = datetime.now(timezone.utc)
    db.insert_reading(conn, _reading(now - timedelta(seconds=45)))
    lim = api._OUTDOOR_OBS_STALE_S
    monkeypatch.setattr(api, "_live_feed",
                        lambda cfg: _feed(now, lim, obsTs=now.timestamp() - lim))
    assert api.build_outdoor(conn, "dev1", "24h", now=now, cfg=CFG)["now"]["stale"] is False
    monkeypatch.setattr(api, "_live_feed", lambda cfg: _feed(now, lim + 2))
    assert api.build_outdoor(conn, "dev1", "24h", now=now, cfg=CFG)["now"]["stale"] is True


def test_moisture_thermostat_gate_is_tighter_than_the_sensor_gate(conn):
    """Sensors 12 min old are still 'now' (900s); a thermostat row 12 min old
    is not (600s)."""
    now = datetime.now(timezone.utc)
    _seed_sensors(conn, now - timedelta(minutes=12))
    dp = api.build_moisture(conn, "dev1", CRAWL_CFG, now=now)["dp_now"]
    assert dp["crawl"] is not None and dp["reference"] is not None
    assert dp["thermostat"] is None and dp["outdoor"] is None
    assert dp["thermostat_age_s"] >= 700


def test_forecast_reason_distinguishes_feed_down_from_no_row(conn, monkeypatch):
    _seed_complete_days(conn, 4)
    assert api.build_forecast(conn, "dev1", CFG)["reason"] == "feed_unreachable"
    monkeypatch.setattr(api, "_live_feed", lambda cfg: {"fcStale": False, "fcDaily": []})
    assert api.build_forecast(conn, "dev1", CFG)["reason"] == "no_tomorrow_forecast"


def test_forecast_api_passes_unknown_peak_through_as_null(conn, monkeypatch):
    _seed_complete_days(conn, 4)
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
    monkeypatch.setattr(api, "_live_feed", lambda cfg: {
        "fcStale": False, "fcDaily": [{"date": tomorrow.isoformat(), "hi": 90}]})
    real = correlation.predict_peak_cost

    def unknown_peak(*a, **k):
        r = real(*a, **k)
        return {**r, "predicted_peak_cool_minutes": None, "predicted_peak_dollars": None}
    monkeypatch.setattr(api.correlation, "predict_peak_cost", unknown_peak)
    fc = api.build_forecast(conn, "dev1", CFG)
    assert fc["available"] is True
    assert fc["predicted_peak_dollars"] is None and fc["predicted_peak_cool_minutes"] is None


def test_weekend_peak_band_gets_its_own_window_and_rate():
    """A tariff with a weekday AND a weekend peak: peak_windows() keeps only the
    weekday one, which used to leave Saturday with no window and a midday
    off-peak price."""
    from house_climate.config import TouBand, TouTable, _parse_hhmm as t
    tou = TouTable(frozenset(range(1, 13)), (
        TouBand("peak", "summer", "weekday", t("17:00"), t("21:00"), 0.40),
        TouBand("off", "summer", "weekday", t("21:00"), t("17:00"), 0.10),
        TouBand("peak", "summer", "weekend", t("14:00"), t("18:00"), 0.40),
        TouBand("off", "summer", "weekend", t("18:00"), t("14:00"), 0.10),
    ))
    hist = [{"day_high": h, "cool_minutes": 3 * h, "peak_cool_minutes": h, "has_peak": True}
            for h in (80.0, 85.0, 90.0)]
    sat = correlation.predict_peak_cost(90.0, hist, tou, 3.0, CFG.timezone,
                                        target_date=date(2026, 8, 15))
    assert sat["has_peak"] is True
    assert sat["peak_windows"] == [{"start": "14:00", "end": "18:00"}]
    assert sat["peak_rate_used"] == 0.40


def test_peak_fit_mean_branch_and_default_has_peak():
    # two peak days -> mean; a row with no has_peak key counts as a peak day
    hist = [{"day_high": 80.0, "cool_minutes": 100.0, "peak_cool_minutes": 30.0},
            {"day_high": 90.0, "cool_minutes": 200.0, "peak_cool_minutes": 50.0, "has_peak": True},
            {"day_high": 95.0, "cool_minutes": 250.0, "peak_cool_minutes": 0.0, "has_peak": False}]
    out = correlation.predict_peak_cost(90.0, hist, CFG.tou, CFG.system_kw, CFG.timezone,
                                        target_date=date(2026, 8, 14))
    assert out["predicted_peak_cool_minutes"] == pytest.approx(40.0)
    assert out["peak_days_of_history"] == 2


def test_season_bands_keep_a_names_highest_rate():
    from house_climate.config import TouBand, TouTable, _parse_hhmm as t
    tou = TouTable(frozenset(range(1, 13)), (
        TouBand("peak", "summer", "weekday", t("17:00"), t("21:00"), 0.40),
        TouBand("off", "summer", "weekday", t("21:00"), t("17:00"), 0.10),
        TouBand("off", "summer", "weekend", t("00:00"), t("00:00"), 0.08),
    ))
    cfg = dataclasses.replace(CFG, tou=tou)
    bands = api._season_bands(cfg, datetime(2026, 8, 14, 12, tzinfo=TZ), lambda r: "x")
    assert bands == [{"name": "peak", "tier": "x", "rate": 0.40},
                     {"name": "off", "tier": "x", "rate": 0.10}]
