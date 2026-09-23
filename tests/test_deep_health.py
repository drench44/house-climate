"""deep_health.py: the judgements behind GET /health/full (no database).

The route itself is exercised against a real Postgres in
tests/test_health_full_route.py. These pin the rules a deploy gate relies on:
a reading or heartbeat the previous container wrote never counts for the new
one, and every failure is named.
"""
import datetime as dt
import json

import pytest

from house_climate import deep_health as dh

NOW = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)


def ago(s):
    return NOW - dt.timedelta(seconds=s)


def _hb(age_s=30, commit="abc1234", started_ago=600):
    return {"value": {"ts": ago(age_s).isoformat(), "commit": commit,
                      "started_at": ago(started_ago).isoformat()},
            "updated_at": ago(age_s)}


def test_heartbeat_limit_is_three_ticks_and_never_under_ten_minutes():
    assert dh.heartbeat_max_age_s(180) == 600.0
    assert dh.heartbeat_max_age_s(300) == 900.0


def test_poller_ok_when_fresh_and_on_this_commit():
    p = dh.poller_block(_hb(), NOW, 600, "abc1234")
    assert p["ok"] is True and p["status"] == "ok"
    assert p["commit"] == "abc1234" and p["age_s"] == 30.0
    assert p["heartbeat_at"] == "2026-09-23T11:59:30Z"


def test_poller_on_another_commit_is_the_old_container():
    p = dh.poller_block(_hb(commit="0ld0ld0"), NOW, 600, "abc1234")
    assert p["ok"] is False and p["status"] == "other_commit"
    # a heartbeat from before the commit was recorded at all
    p = dh.poller_block({"value": {"ts": NOW.isoformat()}, "updated_at": NOW},
                        NOW, 600, "abc1234")
    assert p["status"] == "other_commit"


def test_poller_without_a_deploy_record_is_judged_on_age_only():
    assert dh.poller_block(_hb(commit=None), NOW, 600, None)["ok"] is True


@pytest.mark.parametrize("hb,word", [(None, "waiting"), (_hb(age_s=601), "stale")])
def test_poller_missing_or_stale(hb, word):
    p = dh.poller_block(hb, NOW, 600, "abc1234")
    assert p["ok"] is False and p["status"] == word


def test_data_source_needs_fresh_data_written_after_the_poller_started():
    ok = dh.data_source(configured=True, latest=ago(60), now=NOW, max_age_s=900,
                        since=ago(300).isoformat())
    assert ok["ok"] is True and ok["data_ts"] == "2026-09-23T11:59:00Z"
    # fresh, but the previous poller wrote it: the running one has not yet
    old = dh.data_source(configured=True, latest=ago(60), now=NOW, max_age_s=900,
                         since=ago(30).isoformat())
    assert old["ok"] is False and old["status"] == "waiting"
    stale = dh.data_source(configured=True, latest=ago(901), now=NOW, max_age_s=900,
                           since=None)
    assert stale["status"] == "stale"
    none = dh.data_source(configured=True, latest=None, now=NOW, max_age_s=900, since=None)
    assert none["status"] == "waiting"
    off = dh.data_source(configured=False, latest=None, now=NOW, max_age_s=900, since=None)
    assert off == {"configured": False, "ok": True, "status": "off"}


def test_alerts_block():
    assert dh.alerts_block(ago(100), NOW, 600)["ok"] is True
    assert dh.alerts_block(None, NOW, 600)["status"] == "waiting"
    assert dh.alerts_block(ago(601), NOW, 600)["status"] == "stale"


def test_config_block():
    assert dh.config_block("c", "abc", {"config_sha256": "abc"})["matches_deploy"] is True
    assert dh.config_block("c", "abc", {"config_sha256": "def"})["matches_deploy"] is False
    assert dh.config_block("c", None, {"config_sha256": "def"})["matches_deploy"] is False
    assert dh.config_block("c", "abc", None)["matches_deploy"] is None


def test_read_build_info(tmp_path):
    p = tmp_path / "build_info.json"
    assert dh.read_build_info(p) is None
    p.write_text("{")
    assert dh.read_build_info(p) is None
    p.write_text('"x"')
    assert dh.read_build_info(p) is None
    p.write_text(json.dumps({"engine_commit": "a" * 40, "config_sha256": 7}))
    assert dh.read_build_info(p) == {"engine_commit": "a" * 40, "overlay_commit": None,
                                     "config_sha256": None, "built_at": None}


def test_assemble_names_every_failure():
    r = dh.assemble(
        now=NOW, started_at=ago(60), version="1", build="b", build_info=None,
        config={"matches_deploy": False}, db_ok=False, db_error="refused",
        settings={"alert_channel": dh.setting(True, False, "needs the URL")},
        poller={"ok": False, "status": "stale"}, alerts={"ok": False, "status": "waiting"},
        sources={"thermostat": {"ok": False, "status": "stale"},
                 "rooms": {"ok": True, "status": "ok"}},
        notes={})
    assert r["status"] == "degraded"
    assert r["problems"] == [
        "db: refused",
        "config: the image does not carry the config.json the deploy shipped",
        "setting alert_channel: needs the URL",
        "poller: stale", "alert loop: waiting", "thermostat: stale"]
    good = dh.assemble(
        now=NOW, started_at=ago(60), version="1", build="b", build_info=None,
        config={"matches_deploy": None}, db_ok=True, db_error=None, settings={},
        poller={"ok": True}, alerts={"ok": True}, sources={}, notes={})
    assert good["status"] == "ok" and good["problems"] == []


def test_iso_and_parse_ts():
    assert dh.iso(NOW) == "2026-09-23T12:00:00Z"
    assert dh.iso(NOW.replace(tzinfo=None)) == "2026-09-23T12:00:00Z"
    assert dh.parse_ts("2026-09-23T05:00:00-07:00") == NOW
    assert dh.parse_ts("junk") is None and dh.parse_ts(None) is None


def test_a_poller_from_another_build_of_the_same_commit_is_the_old_one():
    hb = _hb()
    hb["value"]["built_at"] = "2026-09-23T10:00:00Z"
    p = dh.poller_block(hb, NOW, 600, "abc1234", "2026-09-23T11:00:00Z")
    assert p["status"] == "other_build" and p["ok"] is False
    assert dh.poller_block(hb, NOW, 600, "abc1234", "2026-09-23T10:00:00Z")["ok"] is True


def test_a_stale_room_among_live_ones_is_degraded():
    s = dh.data_source(configured=True, latest=ago(60), now=NOW, max_age_s=900,
                       since=ago(300).isoformat(), extra={"stale_items": ["ecowitt_ch5"]})
    assert s["status"] == "degraded" and s["ok"] is False
    assert s["stale_items"] == ["ecowitt_ch5"]


def test_a_daikin_outage_is_named_and_still_fails():
    s = dh.data_source(configured=True, latest=ago(600), now=NOW, max_age_s=900,
                       since=ago(300).isoformat(), extra={"upstream_down": True})
    assert s["status"] == "upstream_down" and s["ok"] is False
    # a fresh reading since the start wins over old errors
    s = dh.data_source(configured=True, latest=ago(60), now=NOW, max_age_s=900,
                       since=ago(300).isoformat(), extra={"upstream_down": True})
    assert s["status"] == "ok"
