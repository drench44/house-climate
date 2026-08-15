#!/bin/bash
#
# house-climate-backup.sh -- nightly pg_dump of the house-climate TimescaleDB.
#
# WHY: your climate history lives ONLY in the Docker named volume
# `climate_pgdata`. A volume is easy to lose -- `docker volume rm`, a corrupt
# chunk, a bad TimescaleDB major-version migration -- and when it is lost,
# years of readings go with it. This dump is the cheap insurance: a
# self-contained, restorable snapshot that survives anything that happens to
# the volume. Point HC_BACKUP_DIR somewhere that survives the box (a second
# disk, a NAS mount, an encrypted vault).
#
# FAIL-LOUD: a backup that quietly stops is worse than none, because it is
# believed. pg_dump writes to a temp file that is size-checked and only then
# atomically moved into place, so a partial or empty dump never masquerades
# as a good one.
#
# RESTORE (a TimescaleDB logical dump will NOT restore with a plain
# pg_restore -- it needs the pre/post_restore wrappers; restore into a
# `climate` DB that has ONLY the extension, so drop and recreate first if the
# container's init.sql already populated it):
#   docker exec house-climate-db-1 psql -U climate -d postgres \
#     -c "DROP DATABASE IF EXISTS climate;" -c "CREATE DATABASE climate;"
#   docker exec house-climate-db-1 psql -U climate -d climate \
#     -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" -c "SELECT timescaledb_pre_restore();"
#   cat climate-YYYY-MM-DD.dump | docker exec -i house-climate-db-1 pg_restore -U climate -d climate --no-owner
#   docker exec house-climate-db-1 psql -U climate -d climate -c "SELECT timescaledb_post_restore();"
#
#   house-climate-backup.sh              # run
#   house-climate-backup.sh --selftest   # pure logic, no docker, no host mutation

set -uo pipefail

CONTAINER="${HC_DB_CONTAINER:-house-climate-db-1}"
DB_USER="${HC_DB_USER:-climate}"
DB_NAME="${HC_DB_NAME:-climate}"
DEST_DIR="${HC_BACKUP_DIR:-/var/backups/house-climate}"
KEEP="${HC_KEEP:-14}"                       # daily dumps retained on this box
MIN_BYTES="${HC_MIN_BYTES:-2000}"           # a real -Fc dump of this DB is well above this
STAMP="${HC_STAMP:-$HOME/.local/state/house-climate-backup-last-success}"

# --- pure predicate (selftest-covered) ---------------------------------------
# hc_verdict <pg_dump_rc> <bytes> -> ok | fail:<reason>
# A zero/undersized archive is a FAILURE, never a success.
hc_verdict() {
  local rc="$1" bytes="$2"
  [ "$rc" -eq 0 ]            || { echo "fail:pg_dump-exit-$rc"; return; }
  [ "$bytes" -ge "$MIN_BYTES" ] || { echo "fail:undersized-$bytes-bytes"; return; }
  echo "ok"
}

if [ "${1:-}" = "--selftest" ]; then
  fails=0
  check() { [ "$1" = "$2" ] || { echo "SELFTEST FAIL: got '$1' want '$2'"; fails=$((fails+1)); }; }
  check "$(hc_verdict 0 50000)"  "ok"
  check "$(hc_verdict 1 50000)"  "fail:pg_dump-exit-1"
  check "$(hc_verdict 0 0)"      "fail:undersized-0-bytes"
  check "$(hc_verdict 0 100)"    "fail:undersized-100-bytes"
  [ "$fails" -eq 0 ] && { echo "selftest OK"; exit 0; } || { echo "selftest FAILED ($fails)"; exit 1; }
fi

fail() { echo "house-climate-backup FAIL: $1 $(date -Is)" >&2; exit 1; }

# Optional safety: set HC_REQUIRE_MOUNTPOINT to a path that must be a mounted
# filesystem (e.g. a NAS or encrypted vault) so a missing mount fails loud
# instead of silently dumping onto the root disk.
if [ -n "${HC_REQUIRE_MOUNTPOINT:-}" ]; then
  mountpoint -q "$HC_REQUIRE_MOUNTPOINT" || fail "$HC_REQUIRE_MOUNTPOINT is not a mountpoint"
fi
mkdir -p "$DEST_DIR" || fail "cannot create $DEST_DIR"
mkdir -p "$(dirname "$STAMP")"

docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
  || fail "container $CONTAINER not running"

day="$(date +%F)"
final="$DEST_DIR/climate-$day.dump"
tmp="$DEST_DIR/.climate-$day.dump.partial"
rm -f "$tmp"

# -Fc = custom format: compressed and restorable with pg_restore (selective,
# parallel, --clean). Write to temp first; never let a partial dump take the
# final name.
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$tmp"
rc=$?
bytes=$(wc -c < "$tmp" 2>/dev/null || echo 0)

verdict="$(hc_verdict "$rc" "$bytes")"
if [ "$verdict" != "ok" ]; then
  rm -f "$tmp"
  fail "$verdict"
fi

mv -f "$tmp" "$final" || fail "atomic move into place failed"

# Rotate: keep the newest $KEEP daily dumps.
mapfile -t old < <(ls -1t "$DEST_DIR"/climate-*.dump 2>/dev/null | tail -n +$((KEEP + 1)))
[ "${#old[@]}" -gt 0 ] && rm -f "${old[@]}"

date -Is > "$STAMP"
echo "house-climate-backup OK: $final ($bytes bytes) $(date -Is)"
