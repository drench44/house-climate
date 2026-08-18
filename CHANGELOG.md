# Changelog

All notable changes to house-climate are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every code-changing pull request adds a line under `## [Unreleased]`; a release
rolls that section to a dated version via `python scripts/release.py`.

## [Unreleased]

### Added
- Versioning system: a single `VERSION` source of truth, this changelog, git
  tags, and a `scripts/release.py` bump-roll-tag ceremony.
- Enforced changelog: a CI `changelog-guard` job and a local pre-commit hook
  block a `src/**` change that adds no `[Unreleased]` entry (docs-only and
  test-only diffs are exempt).
- Auto-published GitHub Releases: pushing a `vX.Y.Z` tag publishes a Release
  whose notes are that version's changelog section.
- A debug/ops version readout: `GET /api/version` returns `{version, build}`,
  and a quiet `house-climate v<version>` line shows in the dashboard footer.
  No changelog on the wall — releases live on GitHub.

### Changed
- Static asset cache-busting is unified to the app version (`?v=<version>`), so
  the css/js cache-busts can no longer drift apart or lag a branch.

### Fixed
- `release.py` reports honestly when a failed release commit's recovery restore
  also fails, instead of falsely claiming a clean tree.

## [1.0.0] — 2026-08-17

The baseline: house-climate as it runs in production.

### Added
- Climate dashboard: live indoor/outdoor conditions, history, runtime, cost and
  cost-summary, forecast, pre-cool advice, humidity, rooms, crawl-space and
  moisture, air quality, thermal-learning, timeline, and anomaly tiles.
- TimescaleDB engine with a Daikin poller and optional Home Assistant push.
- Nightly `pg_dump` backups (fail-loud, atomic, rotated, off-box legs) with a
  CI restore self-test, a pre-deploy backup gate that refuses to deploy without
  a fresh verified snapshot, and a dashboard "Backup stale" header badge.
- Outdoor-conditions history (`/api/outdoor`).
- Open-core split: a public engine with a private house overlay; a no-house-data
  CI guard and a pre-push privacy scanner keep house data out of the engine.
