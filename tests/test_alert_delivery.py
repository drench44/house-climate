"""Alert DELIVERY: what leaves the box and when. Covers the cooldown that
survives a restart (kv-persisted), the generic webhook channel, and
alerts.push_suppress (evaluated + shown on the wall, never pushed).

The kv tests need the real test DB (the `conn` fixture skips without it); the
sink / suppress / config tests are pure."""
import json
import os
import types
from datetime import datetime, timezone, timedelta

import pytest

from house_climate.config import load_config, Secrets
from house_climate.web import alerts

from conftest import CFG_PATH

CFG = load_config(CFG_PATH)
TEST_DSN = os.environ.get("TEST_DB_DSN")
_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


class _Rec:
    """A sink that records what it was asked to send."""
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


# --- cooldown persisted across restarts ------------------------------------
#
# One kv row per dedupe key ("alert_sent:<key>|<variant>"), so two writers can
# never clobber each other's record. Assertions look at specific keys, not the
# whole map: test_app.py imports the web app, whose real alert thread shares
# this test DB for the whole session.

def test_last_sent_round_trips_through_kv(conn):
    al = alerts.Alert("freeze", "warning", "x", variant="v1")
    alerts.record_sent(conn, al.dedupe_key, _NOW)
    assert alerts.load_last_sent(conn)[al.dedupe_key] == _NOW


def test_restart_does_not_resend_an_alert_inside_its_cooldown(conn):
    """The bug: last_sent lived only in memory, so every deploy re-pushed every
    active alert. Simulate two process lifetimes: the second loads the first's
    record from kv and stays quiet."""
    al = alerts.Alert("crawl_mold", "warning", "damp")
    cooldown = timedelta(hours=1)

    first = _Rec()
    alerts._dispatch(first, [al], alerts.load_last_sent(conn), cooldown, _NOW,
                     on_sent=lambda k, ts: alerts.record_sent(conn, k, ts))
    assert [a.key for a in first.sent] == ["crawl_mold"]

    # "restart": a brand-new in-memory map, rebuilt from kv
    second = _Rec()
    alerts._dispatch(second, [al], alerts.load_last_sent(conn), cooldown,
                     _NOW + timedelta(minutes=10))
    assert second.sent == []
    # ...and once the cooldown has passed it sends again
    alerts._dispatch(second, [al], alerts.load_last_sent(conn), cooldown,
                     _NOW + timedelta(minutes=61))
    assert [a.key for a in second.sent] == ["crawl_mold"]


def test_alert_loop_across_a_restart_sends_once(conn, monkeypatch):
    """Loop-level: two separate alert_loop lifetimes against the same DB, the
    same alert firing in both. Exactly one push."""
    secrets = Secrets("k", "t", "e@x", TEST_DSN)
    rec = _Rec()
    monkeypatch.setattr(alerts, "make_sink", lambda cfg: rec)
    monkeypatch.setattr(alerts, "evaluate_current",
                        lambda *a, **k: [alerts.Alert("deliverytest", "warning", "x")])

    class _Stop(Exception):
        pass

    def _stop(_):
        raise _Stop()

    monkeypatch.setattr(alerts.time, "sleep", _stop)
    for _ in range(2):
        with pytest.raises(_Stop):
            alerts.alert_loop(CFG, secrets)
    assert [a.key for a in rec.sent] == ["deliverytest"]


def test_unreadable_kv_still_alerts(monkeypatch):
    """Fail-soft: a kv read that raises must never suppress an alert."""
    def boom(*a, **k):
        raise RuntimeError("kv unreadable")
    monkeypatch.setattr(alerts.db, "kv_prefix", boom)
    last_sent = alerts.load_last_sent(object())
    assert last_sent == {}
    rec = _Rec()
    alerts._dispatch(rec, [alerts.Alert("freeze", "warning", "x")], last_sent,
                     timedelta(hours=1), _NOW)
    assert len(rec.sent) == 1


def test_malformed_kv_entry_is_ignored_not_fatal(monkeypatch):
    monkeypatch.setattr(alerts.db, "kv_prefix", lambda c, p: [
        ("alert_sent:freeze|", {"ts": "not-a-date"}),
        ("alert_sent:crawl_mold|", {"ts": _NOW.isoformat()}),
        ("alert_sent:nobar", {"ts": _NOW.isoformat()}),
        ("alert_sent:odd|", "not-a-dict"),
        ("alert_sent:naive|", {"ts": "2026-08-10T12:00:00"})])
    assert alerts.load_last_sent(object()) == {("crawl_mold", ""): _NOW}


def test_kv_write_failure_does_not_break_dispatch():
    rec = _Rec()

    def boom(_k, _ts):
        raise RuntimeError("kv write failed")
    last_sent = {}
    alerts._dispatch(rec, [alerts.Alert("a", "warning", "x"),
                           alerts.Alert("b", "warning", "y")],
                     last_sent, timedelta(hours=1), _NOW, on_sent=boom)
    assert [a.key for a in rec.sent] == ["a", "b"]
    assert len(last_sent) == 2          # in-memory cooldown still holds


# --- webhook channel ---------------------------------------------------------

class _Resp:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


def test_webhook_posts_the_documented_json(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        return _Resp(200)
    monkeypatch.setattr(alerts.requests, "post", fake_post)
    alerts.WebhookSink("http://hook.example/x").send(
        alerts.Alert("crawl_mold", "warning", "Crawl damp"))
    (url, kw), = calls
    assert url == "http://hook.example/x"
    assert kw["json"] == {"key": "crawl_mold", "severity": "warning",
                          "title": "house-climate: crawl_mold",
                          "message": "Crawl damp"}
    assert kw["timeout"] > 0


def test_webhook_non_2xx_raises_so_the_alert_retries(monkeypatch):
    monkeypatch.setattr(alerts.requests, "post", lambda *a, **k: _Resp(500))
    with pytest.raises(Exception):
        alerts.WebhookSink("http://hook.example/x").send(
            alerts.Alert("freeze", "warning", "x"))


def _cfg_with(**alert_over):
    return types.SimpleNamespace(alerts={**CFG.alerts, **alert_over})


def test_make_sink_webhook_reads_the_url_from_the_environment():
    sink = alerts.make_sink(_cfg_with(channel="webhook"),
                            env={"ALERT_WEBHOOK_URL": "http://hook.example/x"})
    assert isinstance(sink, alerts.WebhookSink)
    assert sink.url == "http://hook.example/x"


@pytest.mark.parametrize("env", [{}, {"ALERT_WEBHOOK_URL": ""},
                                 {"ALERT_WEBHOOK_URL": "   "},
                                 {"ALERT_WEBHOOK_URL": "hook.example/x"}])
def test_make_sink_webhook_without_a_usable_url_fails_loudly(env):
    """Never a silent noop: a push channel that cannot deliver must stop the
    process at startup, not log 'sent' into the void."""
    with pytest.raises(ValueError, match="ALERT_WEBHOOK_URL"):
        alerts.make_sink(_cfg_with(channel="webhook"), env=env)


def test_make_sink_noop_and_ntfy_unchanged():
    assert isinstance(alerts.make_sink(_cfg_with(channel="noop"), env={}), alerts.NoopSink)
    s = alerts.make_sink(_cfg_with(channel="ntfy", ntfy_topic="t"), env={})
    assert isinstance(s, alerts.NtfySink)


# --- push_suppress -------------------------------------------------------------

def test_push_suppress_filters_only_the_push():
    fired = [alerts.Alert("air_quality", "warning", "smoke"),
             alerts.Alert("crawl_mold", "warning", "damp")]
    cfg = _cfg_with(push_suppress=["air_quality"])
    assert [a.key for a in alerts.pushable(fired, cfg)] == ["crawl_mold"]
    # ...and nothing is suppressed without the key (explicitly empty: CFG may
    # be an operator's config.json with a suppress list of its own)
    assert len(alerts.pushable(fired, _cfg_with(push_suppress=[]))) == 2


def test_alert_loop_does_not_push_a_suppressed_key(conn, monkeypatch):
    secrets = Secrets("k", "t", "e@x", TEST_DSN)
    rec = _Rec()
    cfg = types.SimpleNamespace(**{**CFG.__dict__,
                                   "alerts": {**CFG.alerts, "push_suppress": ["freeze"]}})
    monkeypatch.setattr(alerts, "make_sink", lambda c: rec)
    monkeypatch.setattr(alerts, "evaluate_current",
                        lambda *a, **k: [alerts.Alert("freeze", "warning", "cold"),
                                         alerts.Alert("crawl_mold", "warning", "damp")])

    class _Stop(Exception):
        pass

    def _stop(_):
        raise _Stop()
    monkeypatch.setattr(alerts.time, "sleep", _stop)
    with pytest.raises(_Stop):
        alerts.alert_loop(cfg, secrets)
    assert [a.key for a in rec.sent] == ["crawl_mold"]


# --- config validation ----------------------------------------------------------

def _load(tmp_path, **alert_over):
    with open(CFG_PATH) as f:
        d = json.load(f)
    d["alerts"].update(alert_over)
    p = tmp_path / "c.json"
    p.write_text(json.dumps(d))
    return load_config(str(p))


def test_push_suppress_loads(tmp_path):
    c = _load(tmp_path, push_suppress=["air_quality", "freeze"])
    assert c.alerts["push_suppress"] == ["air_quality", "freeze"]


@pytest.mark.parametrize("bad", ["air_quality", [1], ["no_such_alert"], {"a": 1}])
def test_push_suppress_rejects_bad_values(tmp_path, bad):
    with pytest.raises(ValueError, match="push_suppress"):
        _load(tmp_path, push_suppress=bad)


def test_unknown_channel_rejected(tmp_path):
    with pytest.raises(ValueError, match="channel"):
        _load(tmp_path, channel="pager")


@pytest.mark.parametrize("bad", [0, -5, "45", True])
def test_crawl_offline_minutes_must_be_positive_number(tmp_path, bad):
    with pytest.raises(ValueError, match="crawl_offline_minutes"):
        _load(tmp_path, crawl_offline_minutes=bad)


def test_every_alert_key_the_engine_emits_is_known_to_config():
    """push_suppress validates against config.ALERT_KEYS; an alert added to the
    engine but not to that list could never be suppressed."""
    import inspect
    import re
    from house_climate import config
    emitted = set(re.findall(r'Alert\("([a-z_]+)"', inspect.getsource(alerts)))
    assert emitted, "found no Alert(...) constructions to check"
    assert emitted <= set(config.ALERT_KEYS), emitted - set(config.ALERT_KEYS)


# --- review follow-ups ------------------------------------------------------------

def test_webhook_error_never_carries_the_secret_url(monkeypatch):
    import requests
    secret = "http://hook.example/api/webhook/SECRET-ID"

    class Boom(_Resp):
        def raise_for_status(self):
            raise requests.HTTPError(f"500 Server Error for url: {secret}", response=self)

    monkeypatch.setattr(alerts.requests, "post", lambda *a, **k: Boom(500))
    with pytest.raises(Exception) as ei:
        alerts.WebhookSink(secret).send(alerts.Alert("freeze", "warning", "x"))
    assert "SECRET-ID" not in str(ei.value) and "500" in str(ei.value)

    def conn_err(*a, **k):
        raise requests.ConnectionError(f"Max retries exceeded with url: {secret}")
    monkeypatch.setattr(alerts.requests, "post", conn_err)
    with pytest.raises(Exception) as ei:
        alerts.WebhookSink(secret).send(alerts.Alert("freeze", "warning", "x"))
    assert "SECRET-ID" not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_ntfy_channel_requires_a_topic(tmp_path):
    with pytest.raises(ValueError, match="ntfy_topic"):
        _load(tmp_path, channel="ntfy", ntfy_topic="  ")


def test_app_builds_the_sink_at_startup():
    """The module-level make_sink call is what makes a bad webhook setup stop
    the web process at boot instead of crash-looping in the background.
    Read as source: importing app.py opens a DB connection and starts threads."""
    import re
    from pathlib import Path
    src = (Path(alerts.__file__).parent / "app.py").read_text()
    assert re.search(r"^alerts\.make_sink\(cfg\)$", src, re.M), \
        "app.py no longer builds the alert sink at import time"


def test_kv_prefix_escapes_like_wildcards(conn):
    from house_climate import db
    db.kv_set(conn, "alertXsent:freeze|", {"ts": _NOW.isoformat()})
    db.kv_set(conn, "alert_sent:crawl_mold|", {"ts": _NOW.isoformat()})
    keys = {k for k, _ in db.kv_prefix(conn, "alert_sent:")}
    assert "alert_sent:crawl_mold|" in keys
    assert "alertXsent:freeze|" not in keys


# --- re-arm after a condition clears (2026-09-22) ---------------------------
#
# The cooldown stops a STANDING condition re-buzzing. It must not swallow a
# new occurrence of something that had cleared, or a condition that got
# WORSE. It must also not mistake "not checked" (stale data) for "cleared".

_GRACE = timedelta(minutes=60)


def _cycle(sink, fired, last_sent, cleared, now, levels, cooldown=timedelta(hours=12),
           checked=None):
    alerts._rearm_cleared(fired, last_sent, cleared, _GRACE, now,
                          last_level=levels, checked=checked)
    alerts._dispatch(sink, fired, last_sent, cooldown, now, last_level=levels)


def _fresh():
    return _Rec(), {}, {}, {}


def test_a_cleared_alert_pushes_again_when_it_returns():
    sink, last, cleared, lv = _fresh()
    al = alerts.Alert("short_cycling", "warning", "x")
    _cycle(sink, [al], last, cleared, _NOW, lv)                          # pushes
    _cycle(sink, [], last, cleared, _NOW + timedelta(hours=1), lv)       # clears
    _cycle(sink, [], last, cleared, _NOW + timedelta(hours=2), lv)       # 60 min clear: re-armed
    _cycle(sink, [al], last, cleared, _NOW + timedelta(hours=3), lv)     # new occurrence
    assert len(sink.sent) == 2


def test_a_standing_condition_stays_quiet_inside_the_cooldown():
    sink, last, cleared, lv = _fresh()
    al = alerts.Alert("filter_due", "warning", "x")
    for h in range(0, 11):
        _cycle(sink, [al], last, cleared, _NOW + timedelta(hours=h), lv)
    assert len(sink.sent) == 1


def test_a_flicker_shorter_than_the_grace_does_not_rearm():
    sink, last, cleared, lv = _fresh()
    al = alerts.Alert("crawl_condensation", "warning", "x")
    _cycle(sink, [al], last, cleared, _NOW, lv)
    _cycle(sink, [], last, cleared, _NOW + timedelta(minutes=3), lv)      # blips off
    _cycle(sink, [al], last, cleared, _NOW + timedelta(minutes=6), lv)    # and back
    _cycle(sink, [], last, cleared, _NOW + timedelta(minutes=40), lv)
    _cycle(sink, [al], last, cleared, _NOW + timedelta(minutes=80), lv)   # off only 40 min
    assert len(sink.sent) == 1


def test_an_alert_hidden_by_stale_data_did_not_clear():
    """A Daikin outage skips the thermostat checks. Their alerts are absent,
    not cleared, so a freeze that stood through the outage must not re-push
    when the data comes back."""
    sink, last, cleared, lv = _fresh()
    hard = alerts.Alert("freeze", "critical", "x", level=3)
    _cycle(sink, [hard], last, cleared, _NOW, lv, checked={"freeze", "offline"})
    outage = [alerts.Alert("offline", "critical", "y")]
    for m in (30, 60, 90, 120):          # 2 h of thermostat outage: freeze unchecked
        _cycle(sink, outage, last, cleared, _NOW + timedelta(minutes=m), lv,
               checked={"offline"})
    _cycle(sink, [hard], last, cleared, _NOW + timedelta(minutes=150), lv,
           checked={"freeze", "offline"})
    assert [a.key for a in sink.sent] == ["freeze", "offline"]


def test_an_air_quality_source_flip_is_one_standing_condition():
    sink, last, cleared, lv = _fresh()
    mon = alerts.Alert("air_quality", "warning", "m", variant="airnow")
    est = alerts.Alert("air_quality", "warning", "e", variant="estimate")
    _cycle(sink, [mon], last, cleared, _NOW, lv)
    for m in range(30, 240, 30):                      # monitor gone for hours
        _cycle(sink, [est], last, cleared, _NOW + timedelta(minutes=m), lv)
    _cycle(sink, [mon], last, cleared, _NOW + timedelta(minutes=240), lv)
    # the caveated estimate goes out once; the monitor's return is not news
    assert [a.variant for a in sink.sent] == ["airnow", "estimate"]


def test_saturation_does_not_rearm_the_mold_alert():
    fired = [alerts.Alert("crawl_saturated", "warning", "x")]
    keys = alerts._checked_keys([], False, [{"ts": _NOW}], None, None, fired,
                                CFG.alerts, _NOW)
    assert "crawl_saturated" in keys and "crawl_mold" not in keys


def test_rearm_clears_the_persisted_record(conn):
    al = alerts.Alert("peak_surge", "warning", "x", variant="rearm-test")
    alerts.record_sent(conn, al.dedupe_key, _NOW)
    last = alerts.load_last_sent(conn)
    cleared = {}
    forget = lambda k: alerts.forget_sent(conn, k)
    alerts._rearm_cleared([], last, cleared, _GRACE, _NOW, on_rearm=forget)
    assert al.dedupe_key in alerts.load_last_sent(conn)             # grace not over
    alerts._rearm_cleared([], last, cleared, _GRACE, _NOW + _GRACE, on_rearm=forget)
    assert al.dedupe_key not in alerts.load_last_sent(conn)
    assert al.dedupe_key not in last and al.dedupe_key not in cleared


def test_rearm_survives_a_failing_kv_delete():
    last, cleared = {("freeze", ""): _NOW}, {}

    def boom(k):
        raise RuntimeError("db down")
    alerts._rearm_cleared([], last, cleared, _GRACE, _NOW, on_rearm=boom)
    alerts._rearm_cleared([], last, cleared, _GRACE, _NOW + _GRACE, on_rearm=boom)
    assert ("freeze", "") not in last and ("freeze", "") not in cleared


# --- worse is news, better is not ---------------------------------------------

def _eval_with(**latest):
    now = datetime.now(timezone.utc)
    row = {"ts": now, "indoor_temp_f": 70, "indoor_humidity": 40,
           "heat_setpoint_f": 68, "cool_setpoint_f": 76, "equipment_status": "idle",
           "mode": "auto", "wx_outdoor_temp_f": 50, "wx_alert_count": 0,
           "weather_ok": True}
    row.update(latest)
    return {a.key: a for a in alerts.evaluate([row], CFG, 0, now)}


@pytest.mark.parametrize("temp,level,sev", [(34, 1, "warning"), (28.1, 1, "warning"),
                                             (28, 2, "warning"), (20.1, 2, "warning"),
                                             (20, 3, "critical"), (5, 3, "critical")])
def test_freeze_bands(temp, level, sev):
    al = _eval_with(wx_outdoor_temp_f=temp)["freeze"]
    assert (al.level, al.severity) == (level, sev)


def test_freeze_band_falls_back_to_the_thermostat_outdoor_sensor():
    al = _eval_with(wx_outdoor_temp_f=None, daikin_outdoor_temp_f=15)["freeze"]
    assert al.level == 3


def test_colder_pushes_warmer_does_not():
    sink, last, cleared, lv = _fresh()
    temps = [33, 26, 15, 15, 22, 30, 33, 15]    # a cold night and morning, then colder again
    for i, t in enumerate(temps):
        fired = [_eval_with(wx_outdoor_temp_f=t)["freeze"]]
        _cycle(sink, fired, last, cleared, _NOW + timedelta(hours=i), lv)
    assert [a.level for a in sink.sent] == [1, 2, 3]


def test_more_nws_alerts_push_fewer_do_not():
    sink, last, cleared, lv = _fresh()
    for i, n in enumerate([1, 2, 2, 1, 2, 3, 1]):
        fired = [_eval_with(wx_alert_count=n)["weather_alert"]]
        _cycle(sink, fired, last, cleared, _NOW + timedelta(hours=i), lv)
    assert [a.level for a in sink.sent] == [1, 2, 3]
    assert "3 active NWS weather alerts" in sink.sent[-1].message


def test_a_record_from_before_levels_does_not_repush_on_upgrade(conn):
    from house_climate import db
    db.kv_set(conn, "alert_sent:freeze|", {"ts": _NOW.isoformat()})     # old row shape
    lv = alerts.load_last_levels(conn)
    last = {("freeze", ""): _NOW}
    sink = _Rec()
    alerts._dispatch(sink, [alerts.Alert("freeze", "warning", "x", level=2)], last,
                     timedelta(hours=12), _NOW + timedelta(hours=1), last_level=lv)
    assert sink.sent == []
    alerts.record_sent(conn, ("freeze", ""), _NOW, 2)
    assert alerts.load_last_levels(conn)[("freeze", "")] == 2


# --- the loop itself --------------------------------------------------------------

class _Stop(Exception):
    pass


def _run_loop(monkeypatch, cfg, times, fired_seq, checked_seq=None):
    """Run alert_loop for len(times) cycles against a controlled clock and a
    scripted evaluate_current."""
    clock = {"i": 0}

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return times[min(clock["i"], len(times) - 1)]
    monkeypatch.setattr(alerts, "datetime", _DT)

    def _eval(conn, device_id, cfg, now=None, checked=None):
        i = clock["i"]
        if checked is not None:
            # default: every key this script ever fires was checked each pass
            checked |= (checked_seq[i] if checked_seq
                        else {a.key for fired in fired_seq for a in fired})
        return fired_seq[i]
    monkeypatch.setattr(alerts, "evaluate_current", _eval)

    def _sleep(_):
        clock["i"] += 1
        if clock["i"] >= len(times):
            raise _Stop()
    monkeypatch.setattr(alerts.time, "sleep", _sleep)
    with pytest.raises(_Stop):
        alerts.alert_loop(cfg, Secrets("k", "t", "e@x", TEST_DSN))


def _cfg(**alert_over):
    c = types.SimpleNamespace(**vars(CFG))
    c.alerts = dict(CFG.alerts, **alert_over)
    return c


def test_loop_rearms_a_cleared_alert_and_forgets_it_in_kv(conn, monkeypatch):
    rec = _Rec()
    monkeypatch.setattr(alerts, "make_sink", lambda cfg: rec)
    al = alerts.Alert("looprearm", "warning", "x")
    t0 = datetime.now(timezone.utc)
    times = [t0, t0 + timedelta(minutes=10), t0 + timedelta(minutes=80),
             t0 + timedelta(minutes=90)]
    _run_loop(monkeypatch, _cfg(cooldown_minutes=720), times, [[al], [], [], [al]])
    assert [a.key for a in rec.sent] == ["looprearm", "looprearm"]


def test_loop_does_not_rearm_a_suppressed_alert_that_still_fires(conn, monkeypatch):
    rec = _Rec()
    monkeypatch.setattr(alerts, "make_sink", lambda cfg: rec)
    al = alerts.Alert("crawl_mold", "warning", "x")
    from house_climate import db
    db.kv_set(conn, "alert_sent:crawl_mold|", {"ts": datetime.now(timezone.utc).isoformat(),
                                               "level": 0})
    t0 = datetime.now(timezone.utc)
    times = [t0 + timedelta(minutes=m) for m in (0, 70, 140)]
    _run_loop(monkeypatch, _cfg(push_suppress=["crawl_mold"]), times, [[al], [al], [al]])
    assert rec.sent == []
    assert ("crawl_mold", "") in alerts.load_last_sent(conn)      # record kept: still firing


def _webhook(monkeypatch, fail=False):
    posts = []

    def post(url, **kw):
        posts.append(kw["json"]["key"])
        return _Resp(500 if fail else 200)
    monkeypatch.setattr(alerts.requests, "post", post)
    monkeypatch.setattr(alerts, "make_sink", lambda cfg: alerts.WebhookSink("http://hook.example/x"))
    return posts


def test_loop_sends_one_heartbeat_a_day_not_one_a_cycle(conn, monkeypatch):
    conn.execute("DELETE FROM kv WHERE k = 'alert_relay_heartbeat'")
    posts = _webhook(monkeypatch)
    t0 = datetime.now(timezone.utc)
    times = [t0 + timedelta(minutes=3 * i) for i in range(4)] + [t0 + timedelta(hours=25)]
    _run_loop(monkeypatch, CFG, times, [[]] * 5)
    assert posts == ["heartbeat", "heartbeat"]


def test_loop_retries_a_failed_heartbeat_and_does_not_stamp_it(conn, monkeypatch):
    conn.execute("DELETE FROM kv WHERE k = 'alert_relay_heartbeat'")
    posts = _webhook(monkeypatch, fail=True)
    t0 = datetime.now(timezone.utc)
    _run_loop(monkeypatch, CFG, [t0, t0 + timedelta(minutes=3)], [[], []])
    assert posts == ["heartbeat", "heartbeat"]
    from house_climate import db
    assert db.kv_get(conn, "alert_relay_heartbeat") is None


def test_loop_heartbeat_can_be_turned_off(conn, monkeypatch):
    conn.execute("DELETE FROM kv WHERE k = 'alert_relay_heartbeat'")
    posts = _webhook(monkeypatch)
    t0 = datetime.now(timezone.utc)
    _run_loop(monkeypatch, _cfg(relay_heartbeat_hours=0), [t0], [[]])
    assert posts == []


def test_loop_sends_no_heartbeat_while_evaluation_fails(conn, monkeypatch):
    conn.execute("DELETE FROM kv WHERE k = 'alert_relay_heartbeat'")
    posts = _webhook(monkeypatch)

    def broken(*a, **k):
        raise RuntimeError("cannot evaluate")
    t0 = datetime.now(timezone.utc)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return t0
    monkeypatch.setattr(alerts, "datetime", _DT)
    monkeypatch.setattr(alerts, "evaluate_current", broken)
    monkeypatch.setattr(alerts.time, "sleep", lambda _: (_ for _ in ()).throw(_Stop()))
    with pytest.raises(_Stop):
        alerts.alert_loop(CFG, Secrets("k", "t", "e@x", TEST_DSN))
    assert posts == []


# --- relay heartbeat ----------------------------------------------------------

def test_heartbeat_goes_only_to_a_webhook(monkeypatch):
    posted = []
    monkeypatch.setattr(alerts.requests, "post",
                        lambda url, **kw: posted.append(kw["json"]) or _Resp(200))
    assert alerts._send_heartbeat(alerts.WebhookSink("http://hook.example/x")) is True
    assert posted == [{"key": "heartbeat", "severity": "info",
                       "title": "house-climate: heartbeat",
                       "message": "house-climate alert relay heartbeat"}]
    assert alerts._send_heartbeat(alerts.NoopSink()) is False


def test_heartbeat_is_due_daily_and_when_unreadable(conn):
    conn.execute("DELETE FROM kv WHERE k = 'alert_relay_heartbeat'")
    every = timedelta(hours=24)
    assert alerts._heartbeat_due(conn, every, _NOW)
    from house_climate import db
    db.kv_set(conn, "alert_relay_heartbeat", {"ts": _NOW.isoformat()})
    assert not alerts._heartbeat_due(conn, every, _NOW + timedelta(hours=23))
    assert alerts._heartbeat_due(conn, every, _NOW + timedelta(hours=24))
    db.kv_set(conn, "alert_relay_heartbeat", {"garbage": 1})
    assert alerts._heartbeat_due(conn, every, _NOW)


@pytest.mark.parametrize("opt", ["rearm_after_clear_minutes", "relay_heartbeat_hours"])
@pytest.mark.parametrize("bad", [-1, "1", True])
def test_rearm_and_heartbeat_settings_must_be_positive(tmp_path, opt, bad):
    with pytest.raises(ValueError, match=opt):
        _load(tmp_path, **{opt: bad})


def test_rearm_grace_cannot_be_zero(tmp_path):
    with pytest.raises(ValueError, match="rearm_after_clear_minutes"):
        _load(tmp_path, rearm_after_clear_minutes=0)


def test_heartbeat_zero_means_off(tmp_path):
    assert _load(tmp_path, relay_heartbeat_hours=0).alerts["relay_heartbeat_hours"] == 0


def test_loop_does_not_repush_an_alert_hidden_by_an_outage(conn, monkeypatch):
    rec = _Rec()
    monkeypatch.setattr(alerts, "make_sink", lambda cfg: rec)
    al = alerts.Alert("loopoutage", "warning", "x")
    t0 = datetime.now(timezone.utc)
    times = [t0 + timedelta(minutes=m) for m in (0, 30, 90, 150)]
    # the check could not run for two passes (stale data): absent, not cleared
    _run_loop(monkeypatch, _cfg(cooldown_minutes=720), times, [[al], [], [], [al]],
              checked_seq=[{"loopoutage"}, set(), set(), {"loopoutage"}])
    assert [a.key for a in rec.sent] == ["loopoutage"]
