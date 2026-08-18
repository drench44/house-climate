# house-climate

Open-core home-climate dashboard: FastAPI + Postgres (TimescaleDB) engine,
vanilla-JS wall, optional Home Assistant integration. This is the **public
engine** repo — keep house-specific data (real utility rates, HA entity IDs,
coordinates, LAN IPs, ntfy topics) OUT of it; those live in a private overlay.
`tests/test_no_house_data.py` guards this.

## Code review before merge — required

Any substantive change in this repo goes through review BEFORE it merges (or
opens as a PR). Run all three review agents on the branch diff and address real
findings:

- `pr-review-toolkit:silent-failure-hunter` — swallowed errors, weak fallbacks,
  silent wrong-but-reassuring outcomes
- `pr-review-toolkit:code-reviewer` — guideline/style/best-practice adherence,
  dead code, public-repo leaks
- `pr-review-toolkit:pr-test-analyzer` — test-coverage quality; flag tests that
  skip silently in CI (this repo's DB tests need a real Postgres — a bare pytest
  run silently skips them) or assert nothing

Also do a per-change review, and a whole-branch review for multi-task work.
Verify tests genuinely RUN (real DB/services, not silently skipped). This is the
default gate — it should happen without being asked. Docs-only changes
(`*.md`, comments) are exempt.

## Versioning & changelog — required

Any PR that changes app code under `src/` must add a bullet under `##
[Unreleased]` in `CHANGELOG.md` (`### Added/Changed/Fixed`). Docs-only (`*.md`)
and test-only (`tests/`) diffs are exempt — the same carve-out as the review
gate. This is enforced by the CI `changelog-guard` job and the local
`.githooks/pre-commit` hook. Releases are cut with `python scripts/release.py
{major|minor|patch}` (never hand-edit `VERSION` or the `?v=` asset stamps). See
`docs/releasing.md`.

## After cloning

After cloning, run `scripts/install-hooks.sh` — installs the pre-push
privacy guard AND the pre-commit changelog guard (both via
`core.hooksPath=.githooks`; the privacy guard is inert without the operator's
private scanner).
