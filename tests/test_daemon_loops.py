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
