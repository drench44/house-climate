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
#   house-climate-backup.sh --verify-dump <file|latest>  # restore a REAL dump file
#                                              # into a throwaway container, verify

set -uo pipefail

CONTAINER="${HC_DB_CONTAINER:-house-climate-db-1}"
DB_USER="${HC_DB_USER:-climate}"
DB_NAME="${HC_DB_NAME:-climate}"
DEST_DIR="${HC_BACKUP_DIR:-/var/backups/house-climate}"
KEEP="${HC_KEEP:-14}"                       # daily dumps retained on this box
# One dump per calendar month is also copied into $DEST_DIR/monthly/ and kept
# much longer. Fourteen dailies only protect you from damage you notice within
# two weeks; bad rows written by a bug, or a table quietly emptied, can go
# unseen for longer than that, and then every daily already carries it. A
# monthly dump is ~1 MB a year in, so keeping two years costs nothing.
# 0 = keep every monthly forever.
KEEP_MONTHLY="${HC_KEEP_MONTHLY:-24}"
MIN_BYTES="${HC_MIN_BYTES:-2000}"           # a real -Fc dump of this DB is well above this
STAMP="${HC_STAMP:-$HOME/.local/state/house-climate-backup-last-success}"
VERIFY_STAMP="${HC_VERIFY_STAMP:-$HOME/.local/state/house-climate-backup-last-verify}"
# --verify-dump: how far the newest reading in a dump may trail the moment the
# dump was written. The poller writes every few minutes, so a dump whose newest
# reading is hours older than the file came from a DB that had stopped
# recording (or from the wrong database) — a backup of nothing new.
VERIFY_MAX_LAG="${HC_VERIFY_MAX_LAG_SECS:-21600}"

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

# hc_to_prune <keep>  (stdin: file names, newest first) -> the names to delete.
# keep <= 0 prunes nothing: "keep all" must never read as "keep none". A
# non-integer keep also prunes nothing — the caller validates it loudly.
hc_to_prune() {
  local keep="$1" n=0 line
  case "$keep" in ''|*[!0-9]*) cat >/dev/null; return ;; esac
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    n=$((n + 1))
    [ "$keep" -gt 0 ] && [ "$n" -gt "$keep" ] && echo "$line"
  done
  return 0
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
  # Rotation: keep the newest N, delete the rest; 0 means keep everything.
  check "$(printf '%s\n' c b a | hc_to_prune 2)" "a"
  check "$(printf '%s\n' c b a | hc_to_prune 3)" ""
  check "$(printf '%s\n' c b a | hc_to_prune 5)" ""
  check "$(printf '%s\n' c b a | hc_to_prune 0)" ""
  check "$(printf '%s\n' c b a | hc_to_prune 1 | tr '\n' ' ')" "b a "
  check "$(printf '%s\n' c b a | hc_to_prune x)" ""
  check "$(printf '' | hc_to_prune 2)" ""
  [ "$fails" -eq 0 ] && { echo "selftest OK"; exit 0; } || { echo "selftest FAILED ($fails)"; exit 1; }
fi

# ISO-8601 time. GNU date on the box and in CI; the fallback keeps the script
# usable (and its tests runnable) on a BSD date that has no -I.
now_iso() { date -Is 2>/dev/null || date +%Y-%m-%dT%H:%M:%S%z; }

fail() { echo "house-climate-backup FAIL: $1 $(now_iso)" >&2; exit 1; }

# hc_counts <container> <db> -> "t=n t=n ..." for every HC_VERIFY_TABLES table.
# A table that cannot be counted is left OUT of the list, which hc_lost then
# reports as MISSING (on the restored side) or UNCOUNTED-SOURCE (on the source
# side) — never as a pass. ON_ERROR_STOP: without it psql can exit 0 on a
# statement error. psql's own error text for each failed count is appended to
# the file named by $HC_COUNT_ERRS (when set), so a failure says WHY.
hc_counts() {
  local ctr="$1" db="$2" out="" t c
  for t in $HC_VERIFY_TABLES; do
    if c="$(docker exec "$ctr" psql -U "$DB_USER" -d "$db" -v ON_ERROR_STOP=1 \
              -tAc "SELECT count(*) FROM $t" 2>&1)"; then
      out="$out $t=$c"
    elif [ -n "${HC_COUNT_ERRS:-}" ]; then
      echo "$t: $(echo "$c" | head -n 1)" >> "$HC_COUNT_ERRS"
    fi
  done
  echo "$out"
}

# hc_restore <container> <db> <dumpfile>: the proven TimescaleDB restore —
# pre_restore, pg_restore, post_restore — into an EMPTY database. Any step
# failing is fatal; a pg_restore that "mostly worked" is not a restore.
hc_restore() {
  local ctr="$1" db="$2" file="$3"
  docker exec "$ctr" psql -U "$DB_USER" -d "$db" -v ON_ERROR_STOP=1 \
    -c "SET client_min_messages = warning;" \
    -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" -c "SELECT timescaledb_pre_restore();" \
    >/dev/null || fail "pre_restore failed in $ctr/$db"
  docker exec -i "$ctr" pg_restore -U "$DB_USER" -d "$db" --no-owner < "$file" \
    || fail "pg_restore of $file into $ctr/$db exited non-zero"
  docker exec "$ctr" psql -U "$DB_USER" -d "$db" -v ON_ERROR_STOP=1 \
    -c "SELECT timescaledb_post_restore();" >/dev/null || fail "post_restore failed in $ctr/$db"
}

# --- restore round-trip self-test (real dump -> restore -> verify) -----------
# The plain --selftest above only checks the size/rc PREDICATE. This actually
# dumps the live DB and restores it into a THROWAWAY database using the same
# TimescaleDB pre/post_restore procedure documented at the top, then verifies
# every table came back and drops the throwaway. An untested restore is a
# guess; run this periodically (a CI job does, on every push/PR). Needs the DB
# container.
if [ "${1:-}" = "--restore-selftest" ]; then
  docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
    || fail "container $CONTAINER not running"
  testdb="climate_restore_selftest"
  tmp="$(mktemp)"
  # The throwaway DB lives in the LIVE container, so it is dropped on every
  # exit path, not only the happy one.
  trap 'rm -f "$tmp"; docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -q \
          -c "DROP DATABASE IF EXISTS $testdb;" >/dev/null 2>&1' EXIT
  # Count the source rows FIRST — a restore that recovers only the schema (a
  # classic TimescaleDB pre/post_restore failure) leaves readings queryable but
  # EMPTY, which must be a failure, not a pass. We assert the restored count is
  # at least the source count (the dump snapshot has >= this many).
  # Counting the source BEFORE the dump: rows written in between only make the
  # dump larger than the count, which passes. (A retention policy dropping
  # chunks in that window would fail loud on a good backup — rare, and the safe
  # direction for something that gates deploys.)
  [ -n "$(echo $HC_VERIFY_TABLES)" ] || fail "HC_VERIFY_TABLES is empty — nothing would be verified"
  src_counts="$(hc_counts "$CONTAINER" "$DB_NAME")"
  echo "restore-selftest: dumping $DB_NAME (${src_counts# })"
  docker exec "$CONTAINER" pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$tmp" || fail "pg_dump failed"
  bytes=$(wc -c < "$tmp"); [ "$bytes" -ge "$MIN_BYTES" ] || fail "dump undersized ($bytes bytes)"
  echo "restore-selftest: restoring into throwaway $testdb"
  docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS $testdb;" -c "CREATE DATABASE $testdb;" >/dev/null \
    || fail "create $testdb failed"
  hc_restore "$CONTAINER" "$testdb" "$tmp"
  # Verify EVERY table that carries irreplaceable history, not just readings.
  # sensor_readings holds the crawl and per-floor probes the whole moisture
  # case rests on, and interventions holds the hand-entered markers that the
  # before/after comparisons and the transport prediction hang off — a few
  # rows that could never be reconstructed, and far too few to move the dump's
  # size check if they went missing. A restore is not proven by one table
  # coming back.
  restored_counts="$(hc_counts "$CONTAINER" "$testdb")"
  lost="$(hc_lost "$src_counts" "$restored_counts" "$HC_VERIFY_TABLES")"
  # The data must survive, not just the schema.
  [ -z "$lost" ] || fail "restore lost data:$lost (schema-only restore?)"
  echo "restore-selftest OK:$restored_counts $(now_iso)"
  exit 0
fi

# --- verify a REAL dump file --------------------------------------------------
# --restore-selftest proves a fresh dump restores. It says nothing about the
# files actually sitting in $HC_BACKUP_DIR — the ones you would reach for in
# an outage. This restores one of THOSE into a throwaway container (same image
# as the live DB, no network, removed afterwards; the live DB is never
# touched), then checks:
#   1. every table came back with at least the rows counted when the dump was
#      written (the .counts file the nightly run saves beside each dump), and
#   2. the newest reading is within HC_VERIFY_MAX_LAG_SECS of the dump's own
#      timestamp, so the file holds current history rather than a stale or
#      wrong database.
# `latest` picks the newest daily dump by the date in its name. On success it writes
# $HC_VERIFY_STAMP, which a watchdog can age-check. Run it weekly.
if [ "${1:-}" = "--verify-dump" ]; then
  file="${2:-}"
  if [ "$file" = "latest" ]; then
    file="$(ls -1 "$DEST_DIR"/climate-*.dump 2>/dev/null | sort -r | head -n 1)"
    [ -n "$file" ] || fail "verify-dump: no dumps in $DEST_DIR"
  fi
  [ -n "$file" ] || fail "verify-dump: usage: --verify-dump <file|latest>"
  [ -f "$file" ] || fail "verify-dump: no such dump $file"
  [ -s "$file.counts" ] || fail "verify-dump: $file has no row-count file ($file.counts) to verify against"
  [ -n "$(echo $HC_VERIFY_TABLES)" ] || fail "HC_VERIFY_TABLES is empty — nothing would be verified"
  src_counts="$(cat "$file.counts")"
  image="${HC_VERIFY_IMAGE:-$(docker inspect -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null)}"
  [ -n "$image" ] || fail "verify-dump: cannot tell which image $CONTAINER runs (set HC_VERIFY_IMAGE)"
  vc="hc-verify-dump-$$"
  # -v: the image keeps its data in an anonymous volume, and a plain rm -f
  # leaves that volume (a full restored copy of the DB) behind on every run.
  trap 'docker rm -f -v "$vc" >/dev/null 2>&1' EXIT
  docker run -d --name "$vc" --network none \
    -e POSTGRES_USER="$DB_USER" -e POSTGRES_PASSWORD=verify -e POSTGRES_DB="$DB_NAME" \
    "$image" >/dev/null || fail "verify-dump: could not start a throwaway $image container"
  # Ready means a TCP connection works. The image first runs initdb against a
  # temporary socket-only server and then restarts; a socket check can land in
  # that gap and hand us a server that is about to vanish.
  ready=""
  for _ in $(seq 1 60); do
    if docker exec "$vc" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -tAc 'select 1' >/dev/null 2>&1; then
      ready=yes; break
    fi
    sleep 2
  done
  if [ -z "$ready" ]; then
    docker logs --tail 30 "$vc" >&2 2>&1
    fail "verify-dump: throwaway container never accepted connections (its log is above)"
  fi
  echo "verify-dump: restoring $(basename "$file") into throwaway $vc"
  hc_restore "$vc" "$DB_NAME" "$file"
  restored_counts="$(hc_counts "$vc" "$DB_NAME")"
  lost="$(hc_lost "$src_counts" "$restored_counts" "$HC_VERIFY_TABLES")"
  [ -z "$lost" ] || fail "verify-dump: $(basename "$file") lost data:$lost"
  newest="$(docker exec "$vc" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
            -tAc "SELECT coalesce(extract(epoch FROM max(ts))::bigint, 0) FROM readings")" \
    || fail "verify-dump: cannot read the newest reading"
  written="$(stat -c %Y "$file" 2>/dev/null || stat -f %m "$file")"
  case "$newest$written" in ''|*[!0-9]*) fail "verify-dump: unreadable timestamps (newest=$newest written=$written)" ;; esac
  [ "$newest" -gt 0 ] || fail "verify-dump: $(basename "$file") has no readings at all"
  lag=$(( written - newest ))
  [ "$lag" -le "$VERIFY_MAX_LAG" ] \
    || fail "verify-dump: newest reading in $(basename "$file") is ${lag}s older than the dump (max ${VERIFY_MAX_LAG}s) — was the DB still recording?"
  mkdir -p "$(dirname "$VERIFY_STAMP")" && now_iso > "$VERIFY_STAMP" \
    || fail "verify-dump: cannot write $VERIFY_STAMP"
  echo "verify-dump OK: $(basename "$file"):$restored_counts newest-reading-lag=${lag}s $(now_iso)"
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
case "$KEEP" in ''|*[!0-9]*|0) fail "HC_KEEP must be a whole number >= 1 (got '$KEEP')" ;; esac
case "$KEEP_MONTHLY" in ''|*[!0-9]*) fail "HC_KEEP_MONTHLY must be a whole number, 0 = keep all (got '$KEEP_MONTHLY')" ;; esac
mkdir -p "$DEST_DIR" || fail "cannot create $DEST_DIR"
mkdir -p "$(dirname "$STAMP")" || fail "cannot create stamp dir $(dirname "$STAMP")"

# One run at a time. The nightly timer and the pre-deploy gate both call this
# script, and two runs share the same temp names: one could move the other's
# half-written dump into place and stamp it good.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$DEST_DIR/.lock" || fail "cannot open lock $DEST_DIR/.lock"
  flock -n 9 || fail "another backup run holds $DEST_DIR/.lock"
else
  echo "house-climate-backup WARN: flock not found, running without the single-run lock" >&2
fi

docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
  || fail "container $CONTAINER not running"

day="$(date +%F)"
final="$DEST_DIR/climate-$day.dump"
tmp="$DEST_DIR/.climate-$day.dump.partial"
rm -f "$tmp" "$tmp.counts"

# Row counts taken just BEFORE the dump, saved beside it, so --verify-dump can
# later prove the file restores every row it should. Rows written between the
# count and the dump only make the dump bigger, which still passes.
# A count that fails must not cost the night its backup: the dump is still
# taken and put in place, and only THEN does the run fail (no .counts file, no
# success stamp, no monthly copy), so the failure is loud and the data is safe.
count_errs="$(mktemp)"
counts="$(HC_COUNT_ERRS="$count_errs" hc_counts "$CONTAINER" "$DB_NAME")"
uncounted="$(hc_lost "$counts" "$counts" "$HC_VERIFY_TABLES")"
if [ -z "$uncounted" ]; then
  echo "${counts# }" > "$tmp.counts" || { rm -f "$count_errs"; fail "cannot write row counts"; }
else
  uncounted="$uncounted ($(tr '\n' ';' < "$count_errs"))"
fi
rm -f "$count_errs"

# -Fc = custom format: compressed and restorable with pg_restore (selective,
# parallel, --clean). Write to temp first; never let a partial dump take the
# final name.
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$tmp"
rc=$?
bytes=$(wc -c < "$tmp" 2>/dev/null || echo 0)

verdict="$(hc_verdict "$rc" "$bytes")"
if [ "$verdict" != "ok" ]; then
  rm -f "$tmp" "$tmp.counts"
  fail "$verdict"
fi

# An earlier run today may have left a .counts file; remove it FIRST so a
# failed move below can never pair this dump with that run's counts.
rm -f "$final.counts" || fail "cannot replace $final.counts"
mv -f "$tmp" "$final" || fail "atomic move into place failed"
if [ -n "$uncounted" ]; then
  # Keep the dump, but a dump nothing can verify is not a success.
  fail "dump is safe at $final, but tables could not be counted, so it cannot be verified:$uncounted"
fi
mv -f "$tmp.counts" "$final.counts" \
  || fail "dump is at $final but its row counts could not be put beside it"

# Monthly keeper: the first good dump of each month is copied aside (temp name,
# then renamed, same as the daily) and kept for $KEEP_MONTHLY months.
month_dir="$DEST_DIR/monthly"
monthly="$month_dir/climate-$(date +%Y-%m).dump"
if [ ! -e "$monthly" ]; then
  mkdir -p "$month_dir" \
    && cp "$final.counts" "$monthly.counts" \
    && cp "$final" "$monthly.partial" \
    && mv -f "$monthly.partial" "$monthly" \
    || { rm -f "$monthly.partial"; fail "monthly copy to $monthly failed (daily dump is OK at $final)"; }
fi

# Rotate: keep the newest $KEEP daily dumps and $KEEP_MONTHLY monthly ones,
# newest by the DATE IN THE NAME (a clock jump or a copied-in file can give an
# old dump a new mtime), and never tonight's. Each .counts goes with its dump.
# A delete that fails is a failure: pruning that silently stops fills the disk.
prune_failed=""
while IFS= read -r f; do
  [ "$f" = "$final" ] && continue
  rm -f -- "$f" "$f.counts" || prune_failed="$prune_failed $f"
done < <(ls -1 "$DEST_DIR"/climate-*.dump 2>/dev/null | sort -r | hc_to_prune "$KEEP")
while IFS= read -r f; do
  [ "$f" = "$monthly" ] && continue
  rm -f -- "$f" "$f.counts" || prune_failed="$prune_failed $f"
done < <(ls -1 "$month_dir"/climate-*.dump 2>/dev/null | sort -r | hc_to_prune "$KEEP_MONTHLY")
[ -z "$prune_failed" ] || fail "could not delete old dumps:$prune_failed"
[ -s "$final" ] && [ -s "$final.counts" ] || fail "tonight's dump or its counts vanished during rotation ($final)"

# The dump is already safely in place ($final); still, a stamp we cannot write
# must FAIL LOUD, not print OK — the pre-deploy gate trusts this stamp to prove
# THIS run succeeded, so a silently-unwritten stamp cannot masquerade as fresh.
now_iso > "$STAMP" || fail "cannot write success stamp $STAMP (dump is at $final)"

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

echo "house-climate-backup OK: $final ($bytes bytes) $(now_iso)"
