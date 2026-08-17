"""Poller liveness healthcheck (issue #7).

`docker compose` marks the poller "up" as long as the process is running, but
the boot loop retries forever on bad Daikin credentials and a wedged tick can
hang — either way the container looks healthy over an empty/stale DB. This
probe checks the heartbeat the poll loop writes each tick (kv "poller_heartbeat")
and exits non-zero when it is missing or older than POLLER_HEARTBEAT_MAX_AGE_S,
so Docker's restart-on-unhealthy actually fires.

Run as: python -m house_climate.healthcheck
"""
import os
import sys
from datetime import datetime, timezone

from . import db


def poller_is_healthy(conn, max_age_s: float, now=None) -> tuple[bool, str]:
    """(healthy, reason). Healthy iff a poller heartbeat exists and is younger
    than max_age_s. Pure enough to unit-test against a real connection."""
    now = now or datetime.now(timezone.utc)
    hb = db.kv_get(conn, "poller_heartbeat")
    if not hb:
        return False, "no poller heartbeat recorded yet"
    age = (now - hb["updated_at"]).total_seconds()
    if age > max_age_s:
        return False, f"poller heartbeat stale ({int(age)}s > {int(max_age_s)}s)"
    return True, f"poller heartbeat {int(age)}s old"


def main() -> int:
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("healthcheck: DB_DSN not set", file=sys.stderr)
        return 1
    max_age = float(os.environ.get("POLLER_HEARTBEAT_MAX_AGE_S", "900"))
    try:
        conn = db.connect(dsn)
        healthy, reason = poller_is_healthy(conn, max_age)
    except Exception as e:                       # DB unreachable is unhealthy too
        print(f"healthcheck: {e}", file=sys.stderr)
        return 1
    if not healthy:
        print(f"healthcheck: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
