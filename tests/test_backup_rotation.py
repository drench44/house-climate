"""The nightly backup's file handling: row-count files, monthly keepers, rotation.

These run the real `backup/house-climate-backup.sh` against a stub `docker` on
PATH, so they need no database and no Docker. The restore itself is proven by
the CI backup-restore job; this file proves what ends up in the backup
directory, which is what you reach for in an outage.
"""
import os
import shutil
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
# (except $STUB_FAIL_TABLE, which errors like psql would), pg_dump emits
# $STUB_DUMP_BYTES (5000) bytes, the newest reading is $STUB_NEWEST (default:
# now), a throwaway container starts and accepts connections, and any other
# psql call succeeds. Every `docker run`/`rm`/`logs` is recorded in
# $STUB_CALLS so a test can prove the throwaway container was cleaned up.
STUB = r"""#!/bin/bash
case "$1" in
  inspect)
    case "$*" in *Config.Image*) echo "stub/timescaledb:pinned" ;; *) echo true ;; esac
    exit 0 ;;
  run|rm|logs) echo "$*" >> "$STUB_CALLS"; [ "$1" = run ] && echo stubcontainerid; exit 0 ;;
  exec)
    args="$*"
    case "$args" in
      *pg_dump*) head -c "${STUB_DUMP_BYTES:-5000}" /dev/zero; exit 0 ;;
      *pg_restore*) cat >/dev/null; exit 0 ;;
      *"max(ts)"*) echo "${STUB_NEWEST:-$(date +%s)}"; exit 0 ;;
      *"SELECT count(*) FROM "*)
        t="${args##*FROM }"
        if [ -n "${STUB_FAIL_TABLE:-}" ] && [ "$t" = "$STUB_FAIL_TABLE" ]; then
          echo "ERROR:  relation \"$t\" does not exist"; exit 1
        fi
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
             HC_STAMP=str(tmp_path / "state" / "stamp"),
             HC_VERIFY_STAMP=str(tmp_path / "state" / "verify-stamp"),
             STUB_CALLS=str(tmp_path / "docker-calls"))
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
    assert not list(dest.rglob("*.partial*"))


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


def test_a_table_that_cannot_be_counted_still_leaves_the_dump_but_fails(env):
    """A count error must not cost the night its backup: the dump lands, but
    with no .counts it cannot be verified, so the run fails loudly (no stamp,
    no monthly copy) and says why."""
    e, dest = env
    r = run(dict(e, STUB_FAIL_TABLE="interventions"))
    assert r.returncode != 0
    assert "interventions" in r.stderr
    assert "does not exist" in r.stderr          # psql's own reason, not hidden
    assert today_dump(dest).stat().st_size == 5000
    assert not Path(f"{today_dump(dest)}.counts").exists()
    assert not this_month(dest).exists()
    assert not Path(e["HC_STAMP"]).exists()


def test_an_undersized_dump_leaves_nothing_behind(env):
    e, dest = env
    r = run(dict(e, STUB_DUMP_BYTES="10"))
    assert r.returncode != 0
    assert "undersized" in r.stderr
    assert not today_dump(dest).exists()
    assert not Path(f"{today_dump(dest)}.counts").exists()
    assert not list(dest.rglob("*.partial*"))
    assert not Path(e["HC_STAMP"]).exists()


def test_a_failed_monthly_copy_fails_the_run_but_keeps_the_daily(env):
    e, dest = env
    dest.mkdir()
    (dest / "monthly").write_text("a file where the monthly directory should be")
    r = run(e)
    assert r.returncode != 0
    assert "monthly copy" in r.stderr
    assert today_dump(dest).exists()
    assert not Path(e["HC_STAMP"]).exists()


@pytest.mark.parametrize("keep", ["abc", "-1"])
def test_a_bad_monthly_keep_is_refused(env, keep):
    e, dest = env
    r = run(dict(e, HC_KEEP_MONTHLY=keep))
    assert r.returncode != 0
    assert "HC_KEEP_MONTHLY" in r.stderr


def test_rotation_never_deletes_tonights_dump(env):
    """Names dated in the future (a clock that was wrong once) sort newer than
    tonight's. Rotation must still keep tonight's dump."""
    e, dest = env
    dest.mkdir()
    for i in range(20):
        make_old(dest / f"climate-2099-01-{i + 1:02d}.dump", days_ago=0)
    r = run(e)
    assert r.returncode == 0, r.stderr
    assert today_dump(dest).exists()
    assert Path(f"{today_dump(dest)}.counts").exists()


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock (Linux)")
def test_a_second_run_at_the_same_time_is_refused(env):
    e, dest = env
    dest.mkdir()
    with open(dest / ".lock", "w") as held:
        holder = subprocess.Popen(["flock", "-n", str(dest / ".lock"), "sleep", "5"])
        time.sleep(0.3)
        try:
            r = run(e)
        finally:
            holder.kill()
            holder.wait()
    assert r.returncode != 0
    assert "another backup run" in r.stderr
    assert not today_dump(dest).exists()


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


def _dump_then_verify(e, **extra):
    assert run(e).returncode == 0
    return run(dict(e, **extra), "--verify-dump", "latest")


def test_verify_dump_passes_a_good_dump_and_writes_its_stamp(env, tmp_path):
    e, _ = env
    r = _dump_then_verify(e)
    assert r.returncode == 0, r.stderr
    assert "verify-dump OK" in r.stdout
    assert Path(e["HC_VERIFY_STAMP"]).read_text().strip()
    calls = Path(e["STUB_CALLS"]).read_text()
    assert "--network none" in calls
    # the throwaway container AND its anonymous data volume are removed
    assert "rm -f -v hc-verify-dump-" in calls


def test_verify_dump_fails_when_the_newest_reading_is_too_old(env):
    e, _ = env
    r = _dump_then_verify(e, STUB_NEWEST=str(int(time.time()) - 7 * 3600))
    assert r.returncode != 0
    assert "older than the dump" in r.stderr
    assert not Path(e["HC_VERIFY_STAMP"]).exists()
    assert "rm -f -v hc-verify-dump-" in Path(e["STUB_CALLS"]).read_text()


def test_verify_dump_fails_when_rows_are_missing(env):
    e, dest = env
    assert run(e).returncode == 0
    counts = Path(f"{today_dump(dest)}.counts")
    counts.write_text(counts.read_text().replace("readings=5", "readings=999"))
    r = run(e, "--verify-dump", "latest")
    assert r.returncode != 0
    assert "lost data" in r.stderr and "readings=5(want>=999)" in r.stderr
    assert not Path(e["HC_VERIFY_STAMP"]).exists()


def test_verify_dump_latest_picks_by_date_in_the_name(env):
    e, dest = env
    assert run(e).returncode == 0
    older = dest / "climate-2020-01-01.dump"
    make_old(older, days_ago=0)
    os.utime(older, (time.time() + 3600, time.time() + 3600))   # newest mtime
    r = run(e, "--verify-dump", "latest")
    assert r.returncode == 0, r.stderr
    assert today_dump(dest).name in r.stdout
