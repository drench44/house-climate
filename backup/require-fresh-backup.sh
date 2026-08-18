#!/bin/bash
#
# require-fresh-backup.sh -- run the house-climate backup and assert THIS run
# left a fresh success stamp, so a deploy can gate on "a just-verified restore
# point exists RIGHT NOW" and refuse to proceed otherwise.
#
# WHY: a deploy can wreck the climate DB volume (bad rebuild, failed migration,
# TimescaleDB major-version bump). This is the pre-deploy tripwire: take a
# snapshot immediately before touching the box, and DO NOT DEPLOY if it fails.
# It delegates the dump to house-climate-backup.sh (single source of truth),
# then asserts the success stamp ADVANCED to at/after the moment this run began
# -- so a backup that exits 0 without actually writing a fresh dump, OR a stale
# stamp left by an earlier run (a retry inside the freshness window), cannot
# wave a deploy through. Absolute age alone would not catch that; run-anchoring
# does.
#
#   require-fresh-backup.sh                 # run the backup, assert fresh
#   require-fresh-backup.sh --selftest      # pure predicate logic, portable, no host mutation
#   require-fresh-backup.sh --gate-selftest # imperative path against a stubbed backup (GNU date / CI)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="${HC_BACKUP_SCRIPT:-$HERE/house-climate-backup.sh}"
STAMP="${HC_STAMP:-$HOME/.local/state/house-climate-backup-last-success}"
MAX_AGE="${HC_MAX_AGE_SECS:-900}"           # a just-run backup stamps within seconds

# --- pure predicate (selftest-covered) ---------------------------------------
# hc_fresh <stamp_epoch> <now_epoch> <max_age> -> ok | fail:<reason>
# Empty, non-numeric, future, or too-old stamps are all FAILURES, never a pass.
hc_fresh() {
  local s="$1" now="$2" max="$3"
  [ -n "$s" ] || { echo "fail:no-stamp"; return; }
  case "$s" in (*[!0-9]*) echo "fail:bad-stamp"; return ;; esac
  local age=$(( now - s ))
  [ "$age" -ge 0 ]      || { echo "fail:stamp-in-future"; return; }
  [ "$age" -le "$max" ] || { echo "fail:stale-${age}s"; return; }
  echo "ok"
}

if [ "${1:-}" = "--selftest" ]; then
  fails=0
  check() { [ "$1" = "$2" ] || { echo "SELFTEST FAIL: got '$1' want '$2'"; fails=$((fails+1)); }; }
  check "$(hc_fresh 1000 1300 900)"   "ok"                  # fresh (age 300)
  check "$(hc_fresh 2000 2000 900)"   "ok"                  # age 0 — same-second stamp, the common case
  check "$(hc_fresh 1000 1900 900)"   "ok"                  # age == max_age (boundary)
  check "$(hc_fresh 1000 1901 900)"   "fail:stale-901s"     # age == max_age + 1
  check "$(hc_fresh 1000 2000 900)"   "fail:stale-1000s"    # stale
  check "$(hc_fresh '' 2000 900)"     "fail:no-stamp"       # empty
  check "$(hc_fresh abc 2000 900)"    "fail:bad-stamp"      # non-numeric
  check "$(hc_fresh 1000.5 2000 900)" "fail:bad-stamp"      # float
  check "$(hc_fresh 2000 1000 900)"   "fail:stamp-in-future" # future / clock skew
  [ "$fails" -eq 0 ] && { echo "selftest OK"; exit 0; } || { echo "selftest FAILED ($fails)"; exit 1; }
fi

fail() { echo "require-fresh-backup FAIL: $1 $(date -Is 2>/dev/null || date)" >&2; exit 1; }

# run_gate: run the backup, then assert the stamp advanced to at/after this
# run's start AND is within MAX_AGE. Exits non-zero (via fail) on any breach.
# Factored out so --gate-selftest can drive the exact live path against a
# stubbed HC_BACKUP_SCRIPT/HC_STAMP. Invokes via `bash "$SCRIPT"` (not `./`) so
# a stripped x-bit can never become the one thing blocking every deploy (the
# nightly systemd unit still needs the script itself executable, so it ships
# mode 755 -- this is the belt to that unit's suspenders).
run_gate() {
  [ -f "$SCRIPT" ] || fail "backup script missing: $SCRIPT"
  local start_epoch; start_epoch="$(date +%s)"
  bash "$SCRIPT" || fail "backup run exited non-zero"

  local stamp_epoch=""
  if [ -r "$STAMP" ]; then
    # The stamp is ISO-8601 (date -Is). Convert to epoch (GNU date on box/CI).
    stamp_epoch="$(date -d "$(cat "$STAMP")" +%s 2>/dev/null || echo "")"
    # A readable-but-unparseable stamp is a date-tool problem, NOT a backup
    # failure -- say so, so no one debugs the wrong thing. (The deploy host is
    # Linux/GNU date; this bites only if run somewhere without `date -d`.)
    [ -n "$stamp_epoch" ] || fail "cannot parse stamp '$(cat "$STAMP")' in $STAMP — GNU date (date -d) required"
  fi
  # Run-anchoring: the stamp must belong to THIS run, not an earlier one.
  { [ -n "$stamp_epoch" ] && [ "$stamp_epoch" -ge "$start_epoch" ]; } \
    || fail "stamp not advanced by this run (backup no-op'd or failed to stamp)"
  local verdict; verdict="$(hc_fresh "$stamp_epoch" "$(date +%s)" "$MAX_AGE")"
  [ "$verdict" = "ok" ] || fail "$verdict"
  echo "require-fresh-backup OK: fresh restore point present $(date -Is 2>/dev/null || date)"
}

if [ "${1:-}" = "--gate-selftest" ]; then
  # Integration coverage for the imperative path -- the half that actually
  # blocks deploys. Drives run_gate against a stubbed backup + stamp. Uses
  # GNU `date -d` like the live path, so it belongs in CI (ubuntu), alongside
  # --restore-selftest.
  fails=0
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  d="$tmp/backup.sh"; st="$tmp/stamp"
  iso_at() { date -d "@$1" -Iseconds 2>/dev/null || date -r "$1" -Iseconds 2>/dev/null; }
  gate() { ( SCRIPT="$1"; STAMP="$2"; run_gate ) >/dev/null 2>&1; echo $?; }
  expect() { # <label> <0|nonzero> <actual>
    if { [ "$2" = 0 ] && [ "$3" = 0 ]; } || { [ "$2" = nonzero ] && [ "$3" != 0 ]; }; then :
    else echo "GATE-SELFTEST FAIL: $1 (want $2, got exit $3)"; fails=$((fails+1)); fi
  }

  printf '%s\n' '#!/bin/bash' 'exit 1' > "$d"; chmod +x "$d"; rm -f "$st"
  expect "delegate-exit-1-blocks" nonzero "$(gate "$d" "$st")"

  printf '%s\n' '#!/bin/bash' "date -Is > '$st'" 'exit 0' > "$d"; chmod +x "$d"; rm -f "$st"
  expect "fresh-stamp-allows" 0 "$(gate "$d" "$st")"

  printf '%s\n' '#!/bin/bash' 'exit 0' > "$d"; chmod +x "$d"; rm -f "$st"
  expect "exit0-but-no-stamp-blocks" nonzero "$(gate "$d" "$st")"

  printf '%s\n' '#!/bin/bash' 'exit 0' > "$d"; chmod +x "$d"
  iso_at "$(( $(date +%s) - 300 ))" > "$st"   # recent, but written BEFORE this run starts
  expect "stale-not-advanced-blocks" nonzero "$(gate "$d" "$st")"

  rm -f "$st"
  expect "delegate-missing-blocks" nonzero "$(gate "$tmp/nope.sh" "$st")"

  [ "$fails" -eq 0 ] && { echo "gate-selftest OK"; exit 0; } || { echo "gate-selftest FAILED ($fails)"; exit 1; }
fi

run_gate
