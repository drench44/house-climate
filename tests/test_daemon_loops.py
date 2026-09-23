"""Loop-level coverage for the two long-running daemons (issue #13).

The building blocks (poll_once, evaluate, _dispatch, _discover_device_id) were
each well tested, but the orchestration loops in poller.run() and
alerts.alert_loop() — and specifically their reconnect/self-heal glue, the code
that runs when infrastructure is flaky — were never executed by a test. These
drive one-or-two real iterations against the test DB, breaking out of the
otherwise-infinite loop by having the end-of-iteration sleep raise.
"""
import os

import pytest

from house_climate.config import load_config, Secrets
from house_climate.web import alerts
from house_climate import poller

from conftest import CFG_PATH

TEST_DSN = os.environ.get("TEST_DB_DSN")


class _Stop(Exception):
    pass


def _stop_after(n):
    """A sleep() replacement that raises _Stop on its n-th call, so a `while
    True` loop whose sleep sits at the end of the body runs exactly n times."""
    state = {"i": 0}

    def _sleep(_):
        state["i"] += 1
        if state["i"] >= n:
            raise _Stop()
    return _sleep


def test_alert_loop_runs_a_full_iteration(conn, monkeypatch):
    cfg = load_config(CFG_PATH)
    secrets = Secrets("k", "t", "e@x", TEST_DSN)
    monkeypatch.setattr(alerts.time, "sleep", _stop_after(1))
    # One full iteration: connect, load readings, evaluate, dispatch, then the
    # end-of-loop sleep raises _Stop. No exception from the body itself.
    with pytest.raises(_Stop):
        alerts.alert_loop(cfg, secrets)


def test_alert_loop_recovers_from_a_db_error(conn, monkeypatch):
    cfg = load_config(CFG_PATH)
    secrets = Secrets("k", "t", "e@x", TEST_DSN)
    calls = {"n": 0}
    real = alerts.db.recent_readings

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db error")   # trip the reconnect path
        return real(*a, **k)

    monkeypatch.setattr(alerts.db, "recent_readings", flaky)
    monkeypatch.setattr(alerts.time, "sleep", _stop_after(2))
    with pytest.raises(_Stop):
        alerts.alert_loop(cfg, secrets)
    assert calls["n"] >= 2   # first raised (except -> reconnect), second succeeded


def test_poller_run_loops_and_recovers(conn, monkeypatch):
    cfg = load_config(CFG_PATH)
    secrets = Secrets("k", "t", "e@x", TEST_DSN)
    monkeypatch.setattr(poller, "DaikinClient", lambda *a, **k: object())
    monkeypatch.setattr(poller, "_discover_device_id", lambda c, cl: "dev1")
    monkeypatch.setattr(poller, "poll_ecowitt", lambda *a, **k: "ecowitt_off")
    monkeypatch.setattr(poller, "update_precip", lambda *a, **k: "precip_noop")
    calls = {"n": 0}

    def flaky_poll(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")            # trip the reconnect path
        return "ok"

    monkeypatch.setattr(poller, "poll_once", flaky_poll)
    monkeypatch.setattr(poller.time, "sleep", _stop_after(2))
    with pytest.raises(_Stop):
        poller.run(cfg, secrets)
    assert calls["n"] >= 2   # recovered after the first tick raised


# --- internet outage: Daikin unreachable at the network level ---

def _no_network(*a, **k):
    import requests
    raise requests.ConnectionError("network is unreachable")


def test_poller_loop_survives_daikin_network_outage(conn, monkeypatch):
    """A network failure talking to Daikin must not skip the LAN-only Ecowitt
    poll, the rain rollup, the heartbeat, or the poll_error row."""
    from house_climate import daikin
    from house_climate.weather import WeatherSnapshot
    cfg = load_config(CFG_PATH)
    secrets = Secrets("k", "t", "e@x", TEST_DSN)
    monkeypatch.setattr(daikin.requests, "post", _no_network)
    monkeypatch.setattr(daikin.requests, "get", _no_network)
    monkeypatch.setattr(poller, "_discover_device_id", lambda c, cl: "dev1")
    monkeypatch.setattr(poller.weather, "fetch", lambda *a, **k: WeatherSnapshot(
        False, None, None, None, None, None, None, None, None, None, None))
    calls = {"ecowitt": 0, "precip": 0}
    monkeypatch.setattr(poller, "poll_ecowitt",
                        lambda *a, **k: calls.__setitem__("ecowitt", calls["ecowitt"] + 1) or "ok")
    monkeypatch.setattr(poller, "update_precip",
                        lambda *a, **k: calls.__setitem__("precip", calls["precip"] + 1) or "precip_noop")
    monkeypatch.setattr(poller.time, "sleep", _stop_after(1))
    with pytest.raises(_Stop):
        poller.run(cfg, secrets)
    assert calls == {"ecowitt": 1, "precip": 1}
    assert db_kinds(conn) >= {"daikin_network"}
    assert conn.execute("SELECT count(*) FROM kv WHERE k='poller_heartbeat'").fetchone()[0] == 1


def db_kinds(conn):
    return {r[0] for r in conn.execute("SELECT kind FROM poll_errors").fetchall()}


def test_poller_startup_retries_with_backoff_on_network_error(conn, monkeypatch):
    """No known device and Daikin unreachable at boot: retry with a growing
    delay instead of crashing (which crash-looped the container)."""
    from house_climate import daikin
    cfg = load_config(CFG_PATH)
    secrets = Secrets("k", "t", "e@x", TEST_DSN)
    monkeypatch.setattr(daikin.requests, "post", _no_network)
    monkeypatch.setattr(daikin.requests, "get", _no_network)
    delays = []

    def sleep(s):
        delays.append(s)
        if len(delays) >= 4:
            raise _Stop()
    monkeypatch.setattr(poller.time, "sleep", sleep)
    with pytest.raises(_Stop):
        poller.run(cfg, secrets)
    assert all(b > a for a, b in zip(delays, delays[1:])), delays
    assert max(delays) <= poller._BOOT_RETRY_MAX_S


def test_poller_startup_uses_known_device_when_daikin_unreachable(conn, monkeypatch):
    """A restart during an internet outage must not hold the LAN sensors
    hostage: if the database already knows the thermostat, start polling it
    right away and let poll_once record the outage."""
    from house_climate import daikin, db
    cfg = load_config(CFG_PATH)
    secrets = Secrets("k", "t", "e@x", TEST_DSN)
    db.upsert_device(conn, "dev-known", "Main", "ONE")
    monkeypatch.setattr(daikin.requests, "post", _no_network)
    monkeypatch.setattr(daikin.requests, "get", _no_network)
    seen = []
    monkeypatch.setattr(poller, "poll_once", lambda c, cl, dev, cf: seen.append(dev) or "x")
    monkeypatch.setattr(poller, "poll_ecowitt", lambda *a, **k: "ecowitt_off")
    monkeypatch.setattr(poller, "update_precip", lambda *a, **k: "precip_noop")
    monkeypatch.setattr(poller.time, "sleep", _stop_after(1))
    with pytest.raises(_Stop):
        poller.run(cfg, secrets)
    assert seen == ["dev-known"]


def test_poller_startup_does_not_fall_back_on_an_auth_error(conn, monkeypatch):
    """A bad-credentials boot (HTTP 401) is not an outage: falling back to the
    known device would start the loop and write a fresh heartbeat, so the
    healthcheck would call a misconfigured poller healthy. Keep retrying."""
    from house_climate import daikin, db
    cfg = load_config(CFG_PATH)
    secrets = Secrets("k", "t", "e@x", TEST_DSN)
    db.upsert_device(conn, "dev-known", "Main", "ONE")

    class Resp:
        status_code, ok, text = 401, False, "unauthorized"
    monkeypatch.setattr(daikin.requests, "post", lambda *a, **k: Resp())
    seen = []
    monkeypatch.setattr(poller, "poll_once", lambda *a, **k: seen.append(1) or "x")
    monkeypatch.setattr(poller.time, "sleep", _stop_after(2))
    with pytest.raises(_Stop):
        poller.run(cfg, secrets)
    assert seen == []
    assert conn.execute("SELECT count(*) FROM kv WHERE k='poller_heartbeat'").fetchone()[0] == 0


def test_poller_startup_empty_device_list_retries_without_fallback(conn, monkeypatch):
    from house_climate import db
    cfg = load_config(CFG_PATH)
    secrets = Secrets("k", "t", "e@x", TEST_DSN)
    db.upsert_device(conn, "dev-known", "Main", "ONE")

    class Empty:
        def list_devices(self): return []
    monkeypatch.setattr(poller, "DaikinClient", lambda *a, **k: Empty())
    seen = []
    monkeypatch.setattr(poller, "poll_once", lambda *a, **k: seen.append(1) or "x")
    delays = []

    def sleep(s):
        delays.append(s)
        if len(delays) >= 3:
            raise _Stop()
    monkeypatch.setattr(poller.time, "sleep", sleep)
    with pytest.raises(_Stop):
        poller.run(cfg, secrets)
    assert seen == [] and delays == sorted(delays) and delays[0] < delays[-1]


def test_poller_startup_recovers_after_a_network_error(conn, monkeypatch):
    from house_climate.daikin import DaikinUnreachable
    cfg = load_config(CFG_PATH)
    secrets = Secrets("k", "t", "e@x", TEST_DSN)

    class Flaky:
        n = 0
        def list_devices(self):
            Flaky.n += 1
            if Flaky.n == 1:
                raise DaikinUnreachable("down")
            return [{"id": "dev-new", "name": "Main", "model": "ONE"}]
    monkeypatch.setattr(poller, "DaikinClient", lambda *a, **k: Flaky())
    seen = []
    monkeypatch.setattr(poller, "poll_once", lambda c, cl, dev, cf: seen.append(dev) or "x")
    monkeypatch.setattr(poller, "poll_ecowitt", lambda *a, **k: "ecowitt_off")
    monkeypatch.setattr(poller, "update_precip", lambda *a, **k: "precip_noop")
    monkeypatch.setattr(poller.time, "sleep", _stop_after(2))
    with pytest.raises(_Stop):
        poller.run(cfg, secrets)
    assert seen == ["dev-new"]


def test_poller_heartbeat_carries_its_commit_and_start(conn, monkeypatch):
    """/health/full requires the NEW poller to be the one ticking after a
    deploy (commit) and its readings to be newer than its start: both ride in
    every heartbeat."""
    from datetime import datetime, timezone
    from house_climate import db, deep_health
    cfg = load_config(CFG_PATH)
    secrets = Secrets("k", "t", "e@x", TEST_DSN)
    monkeypatch.setattr(deep_health, "read_build_info",
                        lambda *a, **k: {"engine_commit": "c0ffee1234567"})
    monkeypatch.setattr(poller, "_discover_device_id", lambda c, cl: "dev1")
    monkeypatch.setattr(poller, "poll_once", lambda *a, **k: "ok")
    monkeypatch.setattr(poller, "poll_ecowitt", lambda *a, **k: "ok")
    monkeypatch.setattr(poller, "update_precip", lambda *a, **k: "precip_noop")
    monkeypatch.setattr(poller.time, "sleep", _stop_after(1))
    before = datetime.now(timezone.utc)
    with pytest.raises(_Stop):
        poller.run(cfg, secrets)
    hb = db.kv_get(conn, "poller_heartbeat")["value"]
    assert hb["commit"] == "c0ffee1234567"
    started = datetime.fromisoformat(hb["started_at"])
    assert before <= started <= datetime.fromisoformat(hb["ts"])
