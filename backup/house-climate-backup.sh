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
#   house-climate-backup.sh                   # run
#   house-climate-backup.sh --selftest        # pure logic, no docker, no host mutation
#   house-climate-backup.sh --restore-selftest # real dump->restore into a throwaway DB

set -uo pipefail

CONTAINER="${HC_DB_CONTAINER:-house-climate-db-1}"
DB_USER="${HC_DB_USER:-climate}"
DB_NAME="${HC_DB_NAME:-climate}"
DEST_DIR="${HC_BACKUP_DIR:-/var/backups/house-climate}"
KEEP="${HC_KEEP:-14}"                       # daily dumps retained on this box
MIN_BYTES="${HC_MIN_BYTES:-2000}"           # a real -Fc dump of this DB is well above this
STAMP="${HC_STAMP:-$HOME/.local/state/house-climate-backup-last-success}"

# --- pure predicate (selftest-covered) ---------------------------------------
# Tables the restore self-test proves came back with their rows: every table in
# db/init.sql. Verifying `readings` alone proves nothing about the other eight,
# and a dump that silently dropped a small one would be nowhere near the size
# check's threshold — `interventions` is a handful of hand-entered rows.
# tests/test_backup_tables.py fails if this list and db/init.sql drift apart,
# so a table added later cannot quietly go unverified.
#
# Setting HC_VERIFY_TABLES REPLACES this list, it does not extend it — an
# overlay adding its own tables must re-list these too, or it stops verifying
# them, which is the exact silent loss of coverage this exists to prevent.
HC_VERIFY_TABLES="${HC_VERIFY_TABLES:-readings sensor_readings interventions precip_daily air_readings filter_events poll_errors devices kv}"

# hc_count <"a=1 b=2"> <name> -> the count for `name`, or empty if absent.
# An exact field match, deliberately not a regex: a table name is DATA, and
# interpolating it into a sed pattern let a name containing a metacharacter
# match the wrong table's count, while a name matching two fields produced a
# multi-line value that broke the comparison below into silence.
hc_count() {
  local pairs="$1" want="$2" kv
  for kv in $pairs; do
    [ "${kv%%=*}" = "$want" ] && { echo "${kv#*=}"; return; }
  done
}

# hc_lost <src_counts> <restored_counts> <tables> -> "" when every table came
# back with at least as many rows as the source had, else a description of what
# was lost.
#
# Every branch that CANNOT decide reports a loss. `[ x -lt y ]` on a
# non-integer returns 2, which an `elif` reads as plain false — so an
# unparseable count used to mean "nothing lost", and a table restored empty
# could be reported healthy. Anything not a plain integer is now a failure, as
# is an empty table list: verifying nothing must never look like verifying
# everything.
hc_lost() {
  local src="$1" got="$2" tables="$3" out="" t s g
  set -f                      # a table name must never be glob-expanded
  # shellcheck disable=SC2086
  set -- $tables
  set +f
  [ "$#" -gt 0 ] || { echo " NO-TABLES-CONFIGURED"; return; }
  for t in "$@"; do
    case "$t" in
      ''|*[!A-Za-z0-9_]*) out="$out $t=BAD-TABLE-NAME"; continue ;;
    esac
    s="$(hc_count "$src" "$t")"
    g="$(hc_count "$got" "$t")"
    case "$s" in
      ''|*[!0-9]*) out="$out $t=UNCOUNTED-SOURCE"; continue ;;
    esac
    case "$g" in
      '') out="$out $t=MISSING"; continue ;;
      *[!0-9]*) out="$out $t=UNREADABLE-COUNT"; continue ;;
    esac
    [ "$g" -ge "$s" ] || out="$out $t=$g(want>=$s)"
  done
  echo "$out"
}

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
  # A restore is only good if EVERY table came back with its rows. The cases
  # below are the ones that would otherwise pass unnoticed: a small table
  # silently dropped (interventions is a handful of hand-entered rows, far too
  # small to move the dump's size check) and a table restored empty.
  check "$(hc_lost "readings=10 interventions=3" "readings=10 interventions=3" "readings interventions")" ""
  check "$(hc_lost "readings=10 interventions=3" "readings=10" "readings interventions")" " interventions=MISSING"
  check "$(hc_lost "readings=10 interventions=3" "readings=10 interventions=0" "readings interventions")" " interventions=0(want>=3)"
  check "$(hc_lost "readings=10 sensor_readings=99" "readings=10 sensor_readings=0" "readings sensor_readings")" " sensor_readings=0(want>=99)"
  # A table that is legitimately empty on both sides is not a loss.
  check "$(hc_lost "readings=10 air_readings=0" "readings=10 air_readings=0" "readings air_readings")" ""
  # A grown table (rows written between the count and the dump) is fine.
  check "$(hc_lost "readings=10" "readings=12" "readings")" ""
  # Neighbouring names must not be confused for one another.
  # Neighbouring names must not be confused. This has to put BOTH names in the
  # tables list, give them DIFFERENT counts, and list the longer name first —
  # anything less and the case passes whether or not the lookup is exact.
  check "$(hc_lost "sensor_readings=5 readings=10" "sensor_readings=5 readings=0" "readings sensor_readings")" \
        " readings=0(want>=10)"
  # A count that is not a plain number cannot be compared. That used to make
  # the comparison error out to stderr and read as "nothing lost".
  check "$(hc_lost "readings=10" "readings=ERROR" "readings")" " readings=UNREADABLE-COUNT"
  # A duplicated table name used to produce a multi-line count, which broke the
  # comparison into silence — a table restored EMPTY was reported healthy.
  check "$(hc_lost "readings=10 readings=10" "readings=0 readings=0" "readings")" \
        " readings=0(want>=10)"
  # A source count that never arrived is a failure, not an exemption.
  check "$(hc_lost "" "readings=0" "readings")" " readings=UNCOUNTED-SOURCE"
  # Absent from BOTH sides is still a loss, not a pass.
  check "$(hc_lost "" "" "readings")" " readings=UNCOUNTED-SOURCE"
  # Verifying nothing must never look like verifying everything.
  check "$(hc_lost "readings=10" "readings=10" "")" " NO-TABLES-CONFIGURED"
  check "$(hc_lost "readings=10" "readings=10" "   ")" " NO-TABLES-CONFIGURED"
  # A name that is not a plain table name is refused rather than expanded.
  check "$(hc_lost "readings=10" "readings=10" "a.b")" " a.b=BAD-TABLE-NAME"
  check "$(hc_lost "readings=10" "readings=10" "*")" " *=BAD-TABLE-NAME"
  [ "$fails" -eq 0 ] && { echo "selftest OK"; exit 0; } || { echo "selftest FAILED ($fails)"; exit 1; }
fi

fail() { echo "house-climate-backup FAIL: $1 $(date -Is)" >&2; exit 1; }

# --- restore round-trip self-test (real dump -> restore -> verify) -----------
# The plain --selftest above only checks the size/rc PREDICATE. This actually
# dumps the live DB and restores it into a THROWAWAY database using the same
# TimescaleDB pre/post_restore procedure documented at the top, then verifies a
# table came back and drops the throwaway. An untested restore is a guess; run
# this periodically (a CI job does, on every push/PR). Needs the DB container.
if [ "${1:-}" = "--restore-selftest" ]; then
  docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
    || fail "container $CONTAINER not running"
  testdb="climate_restore_selftest"
  tmp="$(mktemp)"
  # Count the source rows FIRST — a restore that recovers only the schema (a
  # classic TimescaleDB pre/post_restore failure) leaves readings queryable but
  # EMPTY, which must be a failure, not a pass. We assert the restored count is
  # at least the source count (the dump snapshot has >= this many).
  # Counting the source BEFORE the dump: rows written in between only make the
  # dump larger than the count, which passes. (A retention policy dropping
  # chunks in that window would fail loud on a good backup — rare, and the safe
  # direction for something that gates deploys.)
  # ON_ERROR_STOP matches every other psql call in this file: without it psql
  # can exit 0 on a statement error, and an empty count would enrol a table in
  # the verification while exempting it from every check.
  [ -n "$(echo $HC_VERIFY_TABLES)" ] || fail "HC_VERIFY_TABLES is empty — nothing would be verified"
  src_counts=""
  for t in $HC_VERIFY_TABLES; do
    c="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
         -tAc "SELECT count(*) FROM $t")" \
      || fail "cannot count source $t (table missing?)"
    src_counts="$src_counts $t=$c"
  done
  echo "restore-selftest: dumping $DB_NAME (${src_counts# })"
  docker exec "$CONTAINER" pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$tmp" || fail "pg_dump failed"
  bytes=$(wc -c < "$tmp"); [ "$bytes" -ge "$MIN_BYTES" ] || fail "dump undersized ($bytes bytes)"
  echo "restore-selftest: restoring into throwaway $testdb"
  docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS $testdb;" -c "CREATE DATABASE $testdb;" || fail "create $testdb failed"
  docker exec "$CONTAINER" psql -U "$DB_USER" -d "$testdb" -v ON_ERROR_STOP=1 \
    -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" -c "SELECT timescaledb_pre_restore();" \
    || fail "pre_restore failed"
  docker exec -i "$CONTAINER" pg_restore -U "$DB_USER" -d "$testdb" --no-owner < "$tmp"
  docker exec "$CONTAINER" psql -U "$DB_USER" -d "$testdb" -v ON_ERROR_STOP=1 \
    -c "SELECT timescaledb_post_restore();" || fail "post_restore failed"
  # Verify EVERY table that carries irreplaceable history, not just readings.
  # sensor_readings holds the crawl and per-floor probes the whole moisture
  # case rests on, and interventions holds the hand-entered markers that the
  # before/after comparisons and the transport prediction hang off — a few
  # rows that could never be reconstructed, and far too few to move the dump's
  # size check if they went missing. A restore is not proven by one table
  # coming back.
  restored_counts=""
  for t in $HC_VERIFY_TABLES; do
    got="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$testdb" -v ON_ERROR_STOP=1 \
           -tAc "SELECT count(*) FROM $t")" \
      || continue          # not queryable -> absent from the list -> MISSING
    restored_counts="$restored_counts $t=$got"
  done
  lost="$(hc_lost "$src_counts" "$restored_counts" "$HC_VERIFY_TABLES")"
  docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres \
    -c "DROP DATABASE IF EXISTS $testdb;" >/dev/null
  rm -f "$tmp"
  # The data must survive, not just the schema.
  [ -z "$lost" ] || fail "restore lost data:$lost (schema-only restore?)"
  echo "restore-selftest OK:$restored_counts $(date -Is)"
  exit 0
fi

# RECOMMENDED: set HC_REQUIRE_MOUNTPOINT to a path that must be a mounted
# filesystem (a NAS or encrypted vault) so a missing mount fails loud instead of
# silently dumping onto the root disk — where a disk failure loses the DB volume
# AND every dump together. Dumps are PLAINTEXT (they encode occupancy patterns);
# for an off-box target, encrypt (e.g. pipe through age/gpg, or dump onto an
# encrypted filesystem).
if [ -n "${HC_REQUIRE_MOUNTPOINT:-}" ]; then
  mountpoint -q "$HC_REQUIRE_MOUNTPOINT" || fail "$HC_REQUIRE_MOUNTPOINT is not a mountpoint"
fi
mkdir -p "$DEST_DIR" || fail "cannot create $DEST_DIR"
mkdir -p "$(dirname "$STAMP")" || fail "cannot create stamp dir $(dirname "$STAMP")"

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

# The dump is already safely in place ($final); still, a stamp we cannot write
# must FAIL LOUD, not print OK — the pre-deploy gate trusts this stamp to prove
# THIS run succeeded, so a silently-unwritten stamp cannot masquerade as fresh.
date -Is > "$STAMP" || fail "cannot write success stamp $STAMP (dump is at $final)"

# Record a heartbeat in the app's kv table so the dashboard header can show
# backup health -- a STALE heartbeat also catches "backup stopped running at
# all" (timer disabled, box asleep), which the OnFailure notifier can't, since
# a unit that never runs never fails. Best-effort: a good, verified dump must
# never be reported failed because this telemetry write hiccuped, so warn but
# do not exit non-zero.
# Capture psql's stderr into the WARN: a RECURRING heartbeat failure also shows
# a false "Backup stale" badge on the dashboard, so the operator needs the cause
# (missing kv table? wrong DB? auth?), not just "it failed".
if ! hb_err="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -c \
  "INSERT INTO kv (k, v, updated_at) VALUES ('backup_heartbeat', jsonb_build_object('dump', '$(basename "$final")', 'bytes', $bytes), now()) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = now();" 2>&1)"; then
  echo "house-climate-backup WARN: kv heartbeat write failed (dump is OK at $final): $hb_err" >&2
fi

echo "house-climate-backup OK: $final ($bytes bytes) $(date -Is)"
