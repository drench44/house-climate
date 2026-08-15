import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from house_climate.config import load_config, load_secrets

from conftest import CFG_PATH as CFG

# Self-contained TOU fixture (a public 3-tier time-of-day shape) so band math
# is tested against known values, independent of whatever rates the
# deployment/example config carries.
TOU_FIXTURE = {
    "poll_interval_s": 180, "timezone": "America/Los_Angeles",
    "system_kw": 3.0, "heat_kw": 0.5, "short_cycle_minutes": 10,
    "weather_url": "http://weather:8137/wx.json",
    "weather_url_fallback": "http://weather:8137/wx.json", "web_port": 8090,
    "latitude": 40.0, "longitude": -100.0,
    "alerts": {"channel": "noop", "cooldown_minutes": 60,
               "humidity_high_pct": 60, "humidity_sustained_minutes": 60,
               "setpoint_drift_f": 3.0, "setpoint_drift_minutes": 45,
               "short_cycles_window_hours": 3, "short_cycles_threshold": 3,
               "offline_missed_polls": 5, "peak_surge_ratio": 1.5,
               "aqi_unhealthy": 101},
    "tou": {
        "plan": "test 3-tier time-of-day",
        "seasons": {"summer": {"months": list(range(1, 13))},
                    "winter": {"months": []}},
        "bands": [
            {"name": "peak", "season": "summer", "days": "weekday",
             "start": "17:00", "end": "21:00", "rate": 0.40},
            {"name": "midpeak", "season": "summer", "days": "weekday",
             "start": "07:00", "end": "17:00", "rate": 0.16},
            {"name": "offpeak", "season": "summer", "days": "weekday",
             "start": "21:00", "end": "07:00", "rate": 0.09},
            {"name": "offpeak", "season": "summer", "days": "weekend",
             "start": "00:00", "end": "00:00", "rate": 0.09},
        ],
    },
}


def _fixture_cfg(tmp_path, mutate=None):
    d = json.loads(json.dumps(TOU_FIXTURE))
    if mutate:
        mutate(d)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(d))
    return load_config(str(p))


def test_deployment_config_loads_and_is_sane():
    """Whatever config ships next to the code must load with sane values."""
    c = load_config(CFG)
    assert c.poll_interval_s > 0
    assert c.short_cycle_minutes > 0
    assert 1 <= c.web_port <= 65535
    assert c.tou.bands, "config must define at least one TOU band"


def test_loads_health_panel_fields(tmp_path):
    c = _fixture_cfg(tmp_path, lambda d: d.update(
        filter_reminder_hours=250.0, setpoint_tolerance_f=2.0))
    assert c.filter_reminder_hours == 250.0
    assert c.setpoint_tolerance_f == 2.0


def test_health_panel_fields_default_when_absent(tmp_path):
    # A config that predates these two fields must still load, falling back
    # to the documented defaults instead of raising KeyError.
    c = _fixture_cfg(tmp_path)
    assert c.filter_reminder_hours == 300.0
    assert c.setpoint_tolerance_f == 1.0


def test_tou_band_weekday_peak(tmp_path):
    c = _fixture_cfg(tmp_path)
    dt = datetime(2026, 8, 10, 17, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert c.tou.band_for(dt) == ("peak", 0.40)      # 2026-08-10 is a Monday


def test_tou_band_weekday_offpeak_wraps_midnight(tmp_path):
    c = _fixture_cfg(tmp_path)
    dt = datetime(2026, 8, 10, 2, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert c.tou.band_for(dt) == ("offpeak", 0.09)


def test_tou_band_weekday_midpeak(tmp_path):
    c = _fixture_cfg(tmp_path)
    dt = datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert c.tou.band_for(dt) == ("midpeak", 0.16)


def test_tou_band_weekend_all_day_offpeak(tmp_path):
    c = _fixture_cfg(tmp_path)
    dt = datetime(2026, 8, 8, 18, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert c.tou.band_for(dt) == ("offpeak", 0.09)   # 2026-08-08 is a Saturday


def test_secrets_from_env():
    s = load_secrets({
        "DAIKIN_API_KEY": "k", "DAIKIN_INTEGRATOR_TOKEN": "t",
        "DAIKIN_EMAIL": "e@x.com", "DB_DSN": "postgresql://x"})
    assert s.api_key == "k" and s.email == "e@x.com"


def test_next_transition_midpeak_to_peak(tmp_path):
    c = _fixture_cfg(tmp_path)
    tz = ZoneInfo("America/Los_Angeles")
    # 2026-08-10 is a Monday. 16:17 mid-peak -> next change is 17:00 peak.
    band, at = c.tou.next_transition(datetime(2026, 8, 10, 16, 17, tzinfo=tz))
    assert band == "peak"
    assert at == datetime(2026, 8, 10, 17, 0, tzinfo=tz)


def test_next_transition_peak_to_offpeak(tmp_path):
    c = _fixture_cfg(tmp_path)
    tz = ZoneInfo("America/Los_Angeles")
    band, at = c.tou.next_transition(datetime(2026, 8, 10, 19, 0, tzinfo=tz))
    assert band == "offpeak"
    assert at == datetime(2026, 8, 10, 21, 0, tzinfo=tz)


def test_next_transition_weekend_to_monday_midpeak(tmp_path):
    c = _fixture_cfg(tmp_path)
    tz = ZoneInfo("America/Los_Angeles")
    # 2026-08-08 is a Saturday (all off-peak). Next real change is Mon 07:00.
    band, at = c.tou.next_transition(datetime(2026, 8, 8, 12, 0, tzinfo=tz))
    assert band == "midpeak"
    assert at == datetime(2026, 8, 10, 7, 0, tzinfo=tz)


def test_next_transition_flat_band_is_none(tmp_path):
    def flatten(d):
        d["tou"]["bands"] = [{"name": "flat", "season": "summer", "days": "all",
                              "start": "00:00", "end": "00:00", "rate": 0.12}]
    c = _fixture_cfg(tmp_path, mutate=flatten)
    tz = ZoneInfo("America/Los_Angeles")
    assert c.tou.next_transition(datetime(2026, 8, 10, 9, 0, tzinfo=tz)) == (None, None)


def test_next_transition_across_spring_forward_dst(tmp_path):
    """next_transition's 9-day scan enumerates wall-clock boundaries with
    datetime.combine(..., tzinfo=dt_local.tzinfo) — a ZoneInfo instance
    resolves its own UTC offset per-datetime, so this must keep landing on
    the correct wall-clock instant even when the scan window straddles a
    DST transition (2026-03-08: America/Los_Angeles springs forward at 2am,
    PST -08:00 -> PDT -07:00)."""
    c = _fixture_cfg(tmp_path)
    tz = ZoneInfo("America/Los_Angeles")

    # Saturday before spring-forward: weekend, all offpeak, still PST.
    # The 9-day scan must cross the missing hour to land on Monday 07:00.
    pre = datetime(2026, 3, 7, 12, 0, tzinfo=tz)
    assert pre.utcoffset() == timedelta(hours=-8)
    band, at = c.tou.next_transition(pre)
    assert band == "midpeak"
    assert at == datetime(2026, 3, 9, 7, 0, tzinfo=tz)
    assert at.utcoffset() == timedelta(hours=-7)  # boundary lands in PDT
    assert at > pre

    # Sunday afternoon, already past the spring-forward instant (PDT) —
    # stepping dt_local across the transition must not move the boundary
    # earlier, and must still be strictly after the (now later) input.
    post = datetime(2026, 3, 8, 15, 0, tzinfo=tz)
    assert post.utcoffset() == timedelta(hours=-7)
    band2, at2 = c.tou.next_transition(post)
    assert band2 == "midpeak"
    assert at2 == at
    assert at2 > post
    assert at2 >= at


def test_next_transition_across_fall_back_dst(tmp_path):
    """Mirror of the spring-forward test for the other DST edge: 2026-11-01
    America/Los_Angeles falls back at 2am, PDT -07:00 -> PST -08:00 (an
    hour that occurs twice in wall-clock terms)."""
    c = _fixture_cfg(tmp_path)
    tz = ZoneInfo("America/Los_Angeles")

    # Saturday before fall-back: weekend, all offpeak, still PDT.
    pre = datetime(2026, 10, 31, 12, 0, tzinfo=tz)
    assert pre.utcoffset() == timedelta(hours=-7)
    band, at = c.tou.next_transition(pre)
    assert band == "midpeak"
    assert at == datetime(2026, 11, 2, 7, 0, tzinfo=tz)
    assert at.utcoffset() == timedelta(hours=-8)  # boundary lands in PST
    assert at > pre

    # Sunday afternoon, already past the fall-back instant (PST) — same
    # monotonic/correctness checks as the spring-forward case.
    post = datetime(2026, 11, 1, 15, 0, tzinfo=tz)
    assert post.utcoffset() == timedelta(hours=-8)
    band2, at2 = c.tou.next_transition(post)
    assert band2 == "midpeak"
    assert at2 == at
    assert at2 > post
    assert at2 >= at
