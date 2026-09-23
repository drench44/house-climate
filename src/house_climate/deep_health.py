"""The full health report (GET /health/full): does house-climate actually WORK?

/health is the container healthcheck: 503 only when the database is
unreachable, so an outage elsewhere never restart-loops web. It reports data
ages as information. This report is what a deploy gate (and a person) reads
instead, and it fails, by name, for every way the stack can be up and still
not working:

- the poller: its heartbeat is recent, and it carries the commit the poller
  runs, so a gate can require the NEW poller (not the one the deploy
  replaced) to be the one ticking;
- the data: the newest thermostat reading, room-sensor reading and weather
  reading are each recent AND were written after that poller started, so a
  reading the previous container wrote proves nothing;
- the alert loop in this process has evaluated recently;
- the settings the stack needs: the Daikin credentials, and the webhook URL
  when alerts go to a webhook;
- config.json is the one the deploy shipped: the deploy bakes the sha256 of
  the config it staged into build_info.json, and this compares it with the
  bytes the image carries.

Every gated check lands in `problems` and turns `status` to "degraded". The
backup heartbeat goes in `notes` and never fails anything. The route always
answers 200; liveness stays with /health.

Pure: web/app.py gathers the inputs, this module judges them.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from pathlib import Path

log = logging.getLogger("house_climate.deep_health")

# Newest-data limits. The poller ticks every poll_interval_s (180 s in the
# house config); these allow a few missed ticks, never a stopped producer.
THERMOSTAT_MAX_AGE_S = 900
ROOMS_MAX_AGE_S = 900
WEATHER_MAX_AGE_S = 1800


def heartbeat_max_age_s(poll_interval_s: float) -> float:
    """A poller heartbeat older than three ticks (and at least 10 minutes)
    means the loop stopped."""
    return max(600.0, 3.0 * float(poll_interval_s))


# build_info.json sits next to this file in the image. The deploy writes it
# into the staged tree (never into the repo), so a dev checkout has none.
BUILD_INFO_PATH = Path(__file__).resolve().parent / "build_info.json"


def iso(t) -> str | None:
    """A datetime or epoch seconds as ISO 8601 UTC with a Z."""
    if t is None:
        return None
    if isinstance(t, (int, float)):
        t = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value) -> dt.datetime | None:
    """An aware datetime from a datetime or an ISO 8601 string; None else."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, str) and value:
        try:
            t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    return None


def file_sha256(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError as e:
        log.warning("health: cannot read %s to fingerprint it: %s", path, e)
        return None


def read_build_info(path: Path = BUILD_INFO_PATH) -> dict | None:
    """What the deploy recorded when it built this image: engine_commit,
    overlay_commit, config_sha256, built_at. None when absent (a dev run, or an
    image built by hand), and None plus a warning when unreadable."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("health: %s unreadable: %s", path, e)
        return None
    try:
        info = json.loads(raw)
    except ValueError as e:
        log.warning("health: %s is not JSON: %s", path, e)
        return None
    if not isinstance(info, dict):
        log.warning("health: %s is not a JSON object", path)
        return None
    keep = ("engine_commit", "overlay_commit", "config_sha256", "built_at")
    return {k: info.get(k) if isinstance(info.get(k), str) else None for k in keep}


def config_block(path: str, sha256: str | None, build_info: dict | None) -> dict:
    """config.json is baked into both images, so the question is only whether
    the image carries the bytes the deploy staged. None without a record."""
    expected = (build_info or {}).get("config_sha256")
    matches = None if expected is None else (sha256 is not None and sha256 == expected)
    return {"path": path, "sha256": sha256, "expected_sha256": expected,
            "matches_deploy": matches}


def setting(required: bool, present: bool, why: str) -> dict:
    return {"required": bool(required), "present": bool(present),
            "ok": bool(present) or not required, "why": why}


def poller_block(heartbeat: dict | None, now: dt.datetime, max_age_s: float,
                 engine_commit: str | None) -> dict:
    """The poller's liveness. `heartbeat` is kv poller_heartbeat as kv_get
    returns it ({value: {ts, commit, started_at}, updated_at}). When this web
    image knows its commit, the poller must report the SAME one: web and poller
    are built from one tree, so a poller on another commit is the old container
    still running (or a half-finished deploy)."""
    if not heartbeat:
        return {"ok": False, "status": "waiting", "heartbeat_at": None,
                "commit": None, "started_at": None, "max_age_s": max_age_s}
    value = heartbeat.get("value") if isinstance(heartbeat.get("value"), dict) else {}
    at = parse_ts(heartbeat.get("updated_at"))
    commit = value.get("commit") if isinstance(value.get("commit"), str) else None
    started = parse_ts(value.get("started_at"))
    out = {"heartbeat_at": iso(at), "commit": commit, "started_at": iso(started),
           "max_age_s": max_age_s,
           "age_s": None if at is None else round((now - at).total_seconds(), 1)}
    if at is None or (now - at).total_seconds() > max_age_s:
        out["status"] = "stale"
    elif engine_commit and commit != engine_commit:
        out["status"] = "other_commit"
    else:
        out["status"] = "ok"
    out["ok"] = out["status"] == "ok"
    return out


def data_source(*, configured: bool, latest, now: dt.datetime, max_age_s: float,
                since, extra: dict | None = None) -> dict:
    """One kind of reading the poller writes (thermostat, rooms, weather).
    ok = the newest reading is at most max_age_s old AND newer than `since`
    (the running poller's start), when that start is known."""
    if not configured:
        return {"configured": False, "ok": True, "status": "off"}
    latest = parse_ts(latest)
    since = parse_ts(since)
    out = {"configured": True, "data_ts": iso(latest), "max_age_s": max_age_s,
           "data_age_s": None if latest is None else round((now - latest).total_seconds(), 1)}
    if latest is None:
        out["status"] = "waiting"
    elif (now - latest).total_seconds() > max_age_s:
        out["status"] = "stale"
    elif since is not None and latest < since:
        # the previous poller wrote it; the running one has not yet
        out["status"] = "waiting"
    else:
        out["status"] = "ok"
    out.update(extra or {})
    out["ok"] = out["status"] == "ok"
    return out


def alerts_block(last_run, now: dt.datetime, max_age_s: float) -> dict:
    """The alert loop in THIS web process (in memory, so a new container starts
    with none): it must have evaluated within max_age_s."""
    at = parse_ts(last_run)
    if at is None:
        status = "waiting"
    elif (now - at).total_seconds() > max_age_s:
        status = "stale"
    else:
        status = "ok"
    return {"ok": status == "ok", "status": status, "last_run": iso(at),
            "max_age_s": max_age_s}


def assemble(*, now: dt.datetime, started_at: dt.datetime, version: str, build: str,
             build_info: dict | None, config: dict, db_ok: bool, db_error: str | None,
             settings: dict, poller: dict, alerts: dict, sources: dict,
             notes: dict) -> dict:
    problems = []
    if not db_ok:
        problems.append(f"db: {db_error or 'unusable'}")
    if config.get("matches_deploy") is False:
        problems.append("config: the image does not carry the config.json the deploy shipped")
    for name, s in settings.items():
        if not s.get("ok"):
            problems.append(f"setting {name}: {s.get('why')}")
    if not poller.get("ok"):
        problems.append(f"poller: {poller.get('status')}")
    if not alerts.get("ok"):
        problems.append(f"alert loop: {alerts.get('status')}")
    for name, s in sources.items():
        if not s.get("ok"):
            problems.append(f"{name}: {s.get('status')}")
    return {
        "status": "ok" if not problems else "degraded",
        "problems": problems,
        "checked_at": iso(now),
        "process_started_at": iso(started_at),
        "version": version,
        "build": build,
        "deploy": build_info,
        "config": config,
        "db": {"ok": bool(db_ok), "error": db_error},
        "settings": settings,
        "poller": poller,
        "alerts": alerts,
        "sources": sources,
        "notes": notes,
    }
