# Backup & deploy safety

The climate history lives only in the `climate_pgdata` Docker volume. Losing
that volume loses years of readings, so backups are treated as a hard
dependency of deploying, not an afterthought.

## The backup

`backup/house-climate-backup.sh` is the single source of truth: a nightly
`pg_dump -Fc` that is fail-loud (a partial/empty/undersized dump never
masquerades as good), atomic (temp file, size-checked, then moved), and
rotated. It is env-driven — `HC_BACKUP_DIR`, `HC_REQUIRE_MOUNTPOINT`, `HC_KEEP`,
`HC_KEEP_MONTHLY`, `HC_MIN_BYTES`, `HC_STAMP`. Point `HC_BACKUP_DIR` somewhere that survives the
box and set `HC_REQUIRE_MOUNTPOINT` so a missing mount fails loud instead of
silently dumping onto the root disk.

### What lands in the backup directory

- `climate-YYYY-MM-DD.dump`: the nightly dump. The newest `HC_KEEP` (14) are
  kept.
- `climate-YYYY-MM-DD.dump.counts`: the row count of every table, taken just
  before that dump. `--verify-dump` checks a restore against it.
- `monthly/climate-YYYY-MM.dump` (+ `.counts`): the first good dump of each
  month, kept for `HC_KEEP_MONTHLY` months (24; `0` keeps them all). Fourteen
  dailies only cover damage you notice within two weeks. Bad rows from a bug,
  or a table quietly emptied, can sit unseen longer than that, and by then
  every daily carries it. The monthlies are the way back past that, and at
  about a megabyte a year they cost nothing.

## Verify the restore, don't assume it

`house-climate-backup.sh --restore-selftest` does a real dump → restore into a
throwaway DB (with the TimescaleDB pre/post_restore wrappers) → row-count
verify. CI runs it on every push, so a backup that can't be restored fails CI
instead of being discovered in an emergency.

The verify covers **every** table in `db/init.sql`, not just `readings` —
`HC_VERIFY_TABLES` lists them, and each has to come back queryable with at
least as many rows as the source had. A table that is missing entirely, or that
comes back with fewer rows than the source, fails. (A table legitimately empty
on both sides passes; there was nothing to lose.)

This matters most for the small tables. `interventions` is a handful of
hand-entered dates that the whole before/after case and the transport
prediction hang off, and it could never be reconstructed — but it is far too
small to move the dump's size check, so a dump that silently dropped it would
otherwise have looked healthy. `sensor_readings` is the crawl and per-floor
probe history. Proving `readings` survived says nothing about either.

Setting `HC_VERIFY_TABLES` **replaces** the list rather than extending it, so
an overlay adding its own tables must re-list the defaults too.

### Verify the files you actually have

`--restore-selftest` proves a fresh dump restores. It says nothing about the
files sitting in the backup directory, which are the ones you reach for in an
outage. `house-climate-backup.sh --verify-dump latest` (or a path) restores one
of those files into a throwaway container running the same image as the live
DB, with no network, and removes it afterwards, so the live database is never
touched. It passes only when:

1. every table comes back with at least the rows in that dump's `.counts`
   file, and
2. the newest reading is within `HC_VERIFY_MAX_LAG_SECS` (6 hours) of the
   moment the dump was written, so the file holds current history and not a
   database that had stopped recording.

On success it writes `HC_VERIFY_STAMP`, so a watchdog can alert when the
weekly check stops passing. CI runs a nightly backup and then `--verify-dump`
on the result, and also proves the check fails on a dump missing rows.

The comparison logic is pure and is exercised by `--selftest`, which needs no
database, so a regression in it fails CI in seconds rather than waiting on the
container job. `tests/test_backup_tables.py` fails if the list and
`db/init.sql` ever drift apart, so a table added later cannot quietly go
unverified.

## No deploy without a fresh backup

`backup/require-fresh-backup.sh` runs the backup and then asserts it left a
success stamp no older than `HC_MAX_AGE_SECS` (default 900s), exiting non-zero
otherwise. Wire it into your deploy so a deploy takes a fresh, just-verified
restore point immediately before touching the box and **refuses to proceed if
that snapshot fails**. A bad rebuild, a failed migration, or a TimescaleDB
major-version bump can wreck the volume; this guarantees a current restore
point exists first.

## Seeing it on the dashboard

A successful backup also records a `backup_heartbeat` row in the app's `kv`
table. The dashboard polls `/api/backup` and shows an amber "Backup stale"
badge in the header once that heartbeat is older than `HC_BACKUP_STALE_SECS`
(default 30h) — invisible while backups are healthy. Because it keys off the
heartbeat's age, it also surfaces the case the failure notifier can't: a backup
that stopped running entirely (timer disabled, box asleep) never *fails*, but
its heartbeat still goes stale. The heartbeat write is best-effort — a good
dump is never reported failed because the telemetry write hiccuped.
