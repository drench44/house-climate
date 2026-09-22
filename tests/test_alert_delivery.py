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
    # ...and nothing is suppressed without the key
    assert len(alerts.pushable(fired, _cfg_with())) == 2


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
