"""Poller liveness probe (issue #7)."""
from datetime import datetime, timezone, timedelta

from house_climate import db
from house_climate.healthcheck import poller_is_healthy


def test_poller_healthy_when_heartbeat_fresh(conn):
    db.kv_set(conn, "poller_heartbeat", {"ts": datetime.now(timezone.utc).isoformat()})
    ok, reason = poller_is_healthy(conn, max_age_s=900)
    assert ok, reason


def test_poller_unhealthy_when_no_heartbeat(conn):
    ok, reason = poller_is_healthy(conn, max_age_s=900)
    assert not ok
    assert "no poller heartbeat" in reason


def test_poller_unhealthy_when_heartbeat_stale(conn):
    db.kv_set(conn, "poller_heartbeat", {"ts": "x"})   # kv.updated_at = now()
    # Evaluate "now" far in the future so the just-written heartbeat is stale.
    future = datetime.now(timezone.utc) + timedelta(seconds=1000)
    ok, reason = poller_is_healthy(conn, max_age_s=900, now=future)
    assert not ok
    assert "stale" in reason
