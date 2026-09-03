# Changelog

All notable changes to house-climate are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every code-changing pull request adds a line under `## [Unreleased]`; a release
rolls that section to a dated version via `python scripts/release.py`.

## [Unreleased]

### Fixed
- A configured sensor that is not a floor above the crawl — a garage, a shed —
  made the floor-to-floor path check refuse for every floor, including ones
  whose names place them perfectly well. Sensors that cannot be placed in the
  building are now dropped from the ordering rather than blocking it. Found by
  running the new panel against a real house.
- Floor names are matched on whole words. Substring matching put a "Cupboard"
  on an upper floor, a "Playground" on the ground floor and a "Maintenance
  Room" on the main one — a confidently WRONG order, which is worse than no
  order, because a wrong order is what makes the check name an expensive
  repair. An airstream or an outbuilding ("Attic Fan", "Upstairs Garage") is
  never treated as a floor whatever its name contains.
- The floor-to-floor verdict now names the floors it compared and the channels
  it left out. It previously stated "each floor follows the crawl less closely
  than the one below it" after reasoning over whatever subset survived the
  ordering — and the excluded channel can be the largest coupling on the page.
- Renaming a channel now invalidates the cached fit. Names decide which sensors
  take part in the floor-to-floor check and in what order, but only sensor ids
  were in the cache key, so a rename served a verdict computed over a different
  set of floors until the entry expired.

### Added
- Absolute humidity (g/m³) as a first-class moisture measure:
  `absolute_humidity_gm3` plus a dew-point variant, and a matching SQL fragment
  so daily and hourly rollups convert per reading (AH is nonlinear in
  temperature — averaging temperature first would be wrong). The SQL
  interpolates the Magnus constants from `analytics/humidity` rather than
  repeating them, so the two paths cannot drift apart.
- Crawl-to-floor absolute humidity gap analytics: hourly and daily gap series
  against every configured non-crawl channel, and a before/after comparison
  across each intervention marker with the existing Welch-t and
  seasonal-confound guards. The gap is reported without a directional verdict
  — narrowing and widening are both consistent with a successful intervention,
  depending on which mechanism the work targeted.
- Transport gain (`analytics/coupling.py`): measures how much of the crawl's
  own dampness reaches each floor above, controlling for outdoor air and time
  of day, with Newey-West intervals and an effective-sample-size correction so
  a month of correlated hourly readings is not counted as 720 independent
  ones. Six readiness gates, each refusing with a named reason.
- Stack-effect check: tests whether the crawl-to-floor link strengthens with
  the indoor-to-outdoor temperature difference. This is the guard against a
  vented crawl acting as a better local weather station than the outdoor feed,
  which would otherwise produce coupling with no air movement at all.
- Transport prediction test: predicts each floor's moisture change after an
  intervention from the measured transport gain, then checks it against what
  actually happened — the direction-unambiguous proof that crawl air is
  reaching the living space.
- Cross-floor consistency check: flags an upper floor that follows the crawl
  more closely than the floor below it, which points at leaky ducts or an open
  chase rather than air working up through the floor assembly.
- Moisture page: a crawl-to-floor gap panel (tiles, 60-day multi-line chart,
  intervention before/after) and a transport panel carrying the gain, the
  stack check, the prediction test and a methodology note.
- Dashboard: a gap strip under the crawl panel — current gap and direction per
  floor, plus the transport verdict when it is already cached. It reads from
  `/api/crawl`, which the dashboard already polls, and never triggers a fit.
- `db.indoor_hourly()` for the hourly indoor-to-outdoor temperature difference
  and air-handler duty that the stack-effect check needs.

### Changed
- Before/after comparisons now discount consecutive days for autocorrelation
  before forming their confidence interval. Counting a smooth run of damp days
  as that many independent observations made the interval too narrow and let
  ordinary weather read as a real change. Existing crawl intervention verdicts
  become slightly more conservative as a result.
- The crawl-to-floor gap comparison additionally requires 14 days on each side
  (up from 10) and drops the first week after an intervention, where an open
  hatch and disturbed soil produce a transient that is not the result of the
  work.
- Autocorrelation is now measured on the clock rather than on row position, in
  both the hourly and the daily paths. Rows either side of an outage are not
  neighbours, and pairing them made the data look choppier than it is —
  inflating the effective sample size and narrowing every interval built on it.
- An intervention comparison that could not be checked against outdoor air now
  reports `unchecked` instead of `real`. A seasonal swing cannot be ruled out
  without that check, and the two were previously displayed identically.
- Two flat runs at different values now report `collecting` rather than a
  `real` change with a zero-width interval — that pattern is a stuck sensor,
  not a perfectly clean result.
- Per-day absolute-humidity means now carry a reading count, and days below the
  threshold are dropped. SQL averages skip missing readings silently, so a day
  with two dew-point readings previously weighed as much as a fully observed
  one.

### Fixed
- Critical values for the transport-gain interval were wrong below 20 degrees
  of freedom and badly wrong below 10 — a range that is routinely reached,
  since 24 degrees of freedom are spent absorbing the daily rhythm. The table
  is now computed rather than recalled, and fits too thin to support any
  tabulated value are refused instead of borrowing the nearest one.
- When the autocorrelation-corrected variance broke down it fell back to the
  *uncorrected* variance — the narrowest number available — turning a failed
  calculation into false confidence. It now refuses.
- A zero variance passed the guard and published as a significant result with a
  zero-width interval.
- Optional covariates were not getting the same time-of-day treatment as the
  main predictors, which broke the equivalence the fast path relies on. A
  covariate that is purely a daily schedule is now dropped and reported, rather
  than making the whole fit singular.
- The stack-effect check skipped the identifiability and sample-size gates its
  sibling enforces, so it could publish the page's strongest causal claim from
  a crawl that no longer moves on its own.
- The cross-floor check read a point estimate off fits it had already refused,
  and assumed floors arrived in height order when they arrived in config order.
  It now requires an explicit ready flag, and refuses when the channel names do
  not establish which floor is higher.
- The prediction test treated an unmeasurable margin as a margin of zero,
  producing a confident "the crawl is not the source" from a measurement that
  was never made. It now reports `inconclusive`.
- The dashboard strip stated a transport share as fact regardless of whether it
  could be distinguished from zero, and collapsed every refusal — including a
  sensor outage — into one message.
- The moisture page clamped the displayed interval's lower bound at zero,
  hiding that it reached below zero and was consistent with no crawl air
  arriving at all.
- The gap trend compared the last fourteen rows rather than the last two
  calendar weeks, so after an outage it labelled a month of drift "vs last
  week".
- The dashboard's gap summary rebuilt the entire moisture section — 60-day
  series, every intervention comparison, the prediction test — on every poll
  and discarded almost all of it.
- A cold fit cache reported as "collecting", which reads as a statement about
  the data rather than about the server. It now says so distinctly, as does a
  failed fit.
- Marking a new intervention did not invalidate the cached fit, so a fit
  spanning the marker — which the estimator refuses to compute — could be
  served as current for up to half an hour.
- The backup restore self-test verified only the `readings` table. A restore
  that lost `sensor_readings` (the crawl and per-floor probe history) or
  `interventions` (the hand-entered markers the whole before/after case rests
  on) passed — and `interventions` is far too small for the dump's size check
  to notice. It now verifies every table carrying history, via
  `HC_VERIFY_TABLES`, and fails if any is missing or comes back with fewer rows
  than the source had. `tests/test_backup_tables.py` fails if that list and
  `db/init.sql` ever drift apart, so a table added later cannot quietly go
  unverified, and CI now seeds every table so the restore job can actually
  detect row loss rather than comparing empty against empty.
- A transport fit refused for want of independent readings reported `no_fit`,
  which named no cause. It now reports `insufficient_n_eff` with the effective
  count, so the reason is a fact about the data rather than a shrug.
- The restore check reported "nothing lost" whenever a row count could not be
  compared. A shell integer test returns an error, not false, on a non-numeric
  operand — but in an `elif` that reads as "no loss", so an unreadable or
  duplicated count silently passed a table that had come back empty. Anything
  that cannot be compared is now a failure, as is an empty table list:
  verifying nothing must never look like verifying everything.
- `changelog-guard` no longer fails a release PR: a diff that bumps `VERSION`
  (a `scripts/release.py` release, which rolls `[Unreleased]` rather than adding
  a bullet) is now exempt. Releases can PR on their own.


## [1.1.0] — 2026-08-17

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

### Removed
- The family-hub feature set (F0–F8) that had been built into this engine by
  mistake: the family calendar, reminders, chores, focus list, message board,
  photos, camera, dashboard tile-toggles, and slot ordering — plus the
  CalDAV/iCloud client and the `icalendar` dependency. These belong in the
  separate family-hub project that embeds this dashboard, not in the public
  engine. The generic `kv` store is retained (climate features use it).

### Fixed
- `release.py` reports honestly when a failed release commit's recovery restore
  also fails, instead of falsely claiming a clean tree.
- The crawl-space chips and tooltip now format timestamps with the intended
  range-aware formatter; a duplicate `fmtWhen()` from a removed family tile had
  been shadowing it.

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
