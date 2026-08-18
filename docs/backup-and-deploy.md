# Backup & deploy safety

The climate history lives only in the `climate_pgdata` Docker volume. Losing
that volume loses years of readings, so backups are treated as a hard
dependency of deploying, not an afterthought.

## The backup

`backup/house-climate-backup.sh` is the single source of truth: a nightly
`pg_dump -Fc` that is fail-loud (a partial/empty/undersized dump never
masquerades as good), atomic (temp file, size-checked, then moved), and
rotated. It is env-driven — `HC_BACKUP_DIR`, `HC_REQUIRE_MOUNTPOINT`, `HC_KEEP`,
`HC_MIN_BYTES`, `HC_STAMP`. Point `HC_BACKUP_DIR` somewhere that survives the
box and set `HC_REQUIRE_MOUNTPOINT` so a missing mount fails loud instead of
silently dumping onto the root disk.

## Verify the restore, don't assume it

`house-climate-backup.sh --restore-selftest` does a real dump → restore into a
throwaway DB (with the TimescaleDB pre/post_restore wrappers) → row-count
verify. CI runs it on every push, so a backup that can't be restored fails CI
instead of being discovered in an emergency.

## No deploy without a fresh backup

`backup/require-fresh-backup.sh` runs the backup and then asserts it left a
success stamp no older than `HC_MAX_AGE_SECS` (default 900s), exiting non-zero
otherwise. Wire it into your deploy so a deploy takes a fresh, just-verified
restore point immediately before touching the box and **refuses to proceed if
that snapshot fails**. A bad rebuild, a failed migration, or a TimescaleDB
major-version bump can wreck the volume; this guarantees a current restore
point exists first.
