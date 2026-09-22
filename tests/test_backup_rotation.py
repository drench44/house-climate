"""The nightly backup's file handling: row-count files, monthly keepers, rotation.

These run the real `backup/house-climate-backup.sh` against a stub `docker` on
PATH, so they need no database and no Docker. The restore itself is proven by
the CI backup-restore job; this file proves what ends up in the backup
directory, which is what you reach for in an outage.
"""
import os
import subprocess
import time
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "backup" / "house-climate-backup.sh"
TABLES = ("readings sensor_readings interventions precip_daily air_readings "
          "filter_events poll_errors devices kv").split()

# Stands in for `docker`: the container is running, every count is $STUB_COUNT
# (except $STUB_FAIL_TABLE, which errors), pg_dump emits 5000 bytes, and any
# other psql call (the kv heartbeat) succeeds.
STUB = r"""#!/bin/bash
case "$1" in
  inspect) echo true; exit 0 ;;
  exec)
    args="$*"
    case "$args" in
      *pg_dump*) head -c 5000 /dev/zero; exit 0 ;;
      *"SELECT count(*) FROM "*)
        t="${args##*FROM }"
        [ -n "${STUB_FAIL_TABLE:-}" ] && [ "$t" = "$STUB_FAIL_TABLE" ] && exit 1
        echo "${STUB_COUNT:-5}"; exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
"""


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(STUB)
    stub.chmod(0o755)
    dest = tmp_path / "dumps"
    e = dict(os.environ,
             PATH=f"{bin_dir}:{os.environ['PATH']}",
             HC_BACKUP_DIR=str(dest),
             HC_STAMP=str(tmp_path / "state" / "stamp"))
    e.pop("HC_REQUIRE_MOUNTPOINT", None)
    return e, dest


def run(e, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], env=e,
                          capture_output=True, text=True)


def today_dump(dest):
    return dest / f"climate-{date.today():%Y-%m-%d}.dump"


def this_month(dest):
    return dest / "monthly" / f"climate-{date.today():%Y-%m}.dump"


def make_old(path, days_ago):
    path.write_bytes(b"x" * 5000)
    Path(f"{path}.counts").write_text("readings=1\n")
    t = time.time() - days_ago * 86400
    os.utime(path, (t, t))


def test_a_run_leaves_the_dump_its_counts_a_monthly_copy_and_a_stamp(env):
    e, dest = env
    r = run(e)
    assert r.returncode == 0, r.stderr
    d = today_dump(dest)
    assert d.stat().st_size == 5000
    counts = Path(f"{d}.counts").read_text().split()
    assert counts == [f"{t}=5" for t in TABLES]
    m = this_month(dest)
    assert m.read_bytes() == d.read_bytes()
    assert Path(f"{m}.counts").read_text() == Path(f"{d}.counts").read_text()
    assert Path(e["HC_STAMP"]).read_text().strip()
    assert not list(dest.rglob("*.partial"))


def test_an_existing_monthly_copy_is_never_overwritten(env):
    e, dest = env
    m = this_month(dest)
    m.parent.mkdir(parents=True)
    m.write_bytes(b"first dump of the month")
    Path(f"{m}.counts").write_text("readings=1\n")
    assert run(e).returncode == 0
    assert m.read_bytes() == b"first dump of the month"


def test_daily_rotation_keeps_the_newest_and_takes_the_counts_with_it(env):
    e, dest = env
    dest.mkdir()
    for i in range(20):
        make_old(dest / f"climate-2020-01-{i + 1:02d}.dump", days_ago=100 - i)
    assert run(e).returncode == 0
    dumps = sorted(p.name for p in dest.glob("climate-*.dump"))
    assert len(dumps) == 14
    assert today_dump(dest).name in dumps
    # The 7 oldest went, and so did their .counts files.
    for i in range(7):
        assert not (dest / f"climate-2020-01-{i + 1:02d}.dump").exists()
        assert not (dest / f"climate-2020-01-{i + 1:02d}.dump.counts").exists()
    assert (dest / "climate-2020-01-20.dump.counts").exists()
    # Daily rotation must never reach into the monthly directory.
    assert this_month(dest).exists()


def test_monthly_rotation_keeps_the_configured_number_by_date(env):
    e, dest = env
    mdir = dest / "monthly"
    mdir.mkdir(parents=True)
    for i in range(30):
        # Deliberately give OLDER months NEWER mtimes: order must come from the
        # date in the name, not from when a file was last touched.
        make_old(mdir / f"climate-{2020 + i // 12}-{i % 12 + 1:02d}.dump", days_ago=i)
    assert run(dict(e, HC_KEEP_MONTHLY="24")).returncode == 0
    kept = sorted(p.name for p in mdir.glob("climate-*.dump"))
    assert len(kept) == 24
    assert this_month(dest).name in kept
    assert "climate-2020-01.dump" not in kept
    assert not (mdir / "climate-2020-01.dump.counts").exists()
    assert "climate-2022-06.dump" in kept


def test_keep_monthly_zero_keeps_every_monthly(env):
    e, dest = env
    mdir = dest / "monthly"
    mdir.mkdir(parents=True)
    for i in range(30):
        make_old(mdir / f"climate-2000-{i:02d}.dump", days_ago=i)
    assert run(dict(e, HC_KEEP_MONTHLY="0")).returncode == 0
    assert len(list(mdir.glob("climate-*.dump"))) == 31


@pytest.mark.parametrize("keep", ["0", "abc", "-1"])
def test_a_daily_keep_that_would_delete_everything_is_refused(env, keep):
    e, dest = env
    r = run(dict(e, HC_KEEP=keep))
    assert r.returncode != 0
    assert "HC_KEEP" in r.stderr
    assert not today_dump(dest).exists()


def test_a_table_that_cannot_be_counted_fails_before_any_dump_lands(env):
    e, dest = env
    r = run(dict(e, STUB_FAIL_TABLE="interventions"))
    assert r.returncode != 0
    assert "interventions" in r.stderr
    assert not today_dump(dest).exists()
    assert not Path(e["HC_STAMP"]).exists()


def test_verify_dump_refuses_a_dump_without_its_counts(env, tmp_path):
    e, _ = env
    lone = tmp_path / "climate-2020-01-01.dump"
    lone.write_bytes(b"x" * 5000)
    r = run(e, "--verify-dump", str(lone))
    assert r.returncode != 0
    assert "row-count file" in r.stderr


def test_verify_dump_latest_with_no_dumps_fails(env):
    e, dest = env
    dest.mkdir()
    r = run(e, "--verify-dump", "latest")
    assert r.returncode != 0
    assert "no dumps" in r.stderr
