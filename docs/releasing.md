# Versioning & releasing

house-climate has one version — the SemVer string in `VERSION` at the repo root.
Everything that shows a version derives from it: `house_climate.__version__`, the
`GET /api/version` debug readout, the quiet footer line, and the `?v=` asset
cache-busts on `index.html`. `scripts/release.py` is the **only** writer.

The changelog lives in `CHANGELOG.md` (Keep a Changelog format) and becomes the
GitHub Release notes — there is no in-app "What's New" panel.

## Logging a change (every PR)

A pull request that changes app code under `src/` **must** add a bullet under
`## [Unreleased]` in `CHANGELOG.md` (`### Added` / `### Changed` / `### Fixed`).
Docs-only (`*.md`) and test-only (`tests/`) diffs are exempt.

This is enforced two ways, both running `scripts/check_changelog.py`:
- **CI** — the `changelog-guard` job blocks the PR.
- **Locally** — a pre-commit hook. Run `scripts/install-hooks.sh` once to enable
  it (it sets `core.hooksPath=.githooks`).

## Cutting a release

```
python scripts/release.py {major|minor|patch}
```

It refuses a dirty tree and an empty `[Unreleased]`, then atomically: bumps
`VERSION`, rolls `[Unreleased]` into a dated `## [x.y.z] — <today>` section (and
opens a fresh empty `[Unreleased]`), stamps every `?v=` in `index.html` to the
new version, commits the three files (`--no-verify` — the release commit is the
one commit that legitimately empties `[Unreleased]`), and tags `vX.Y.Z`. It does
**not** push. Then:

```
git push --follow-tags
```

Pushing the `vX.Y.Z` tag triggers `.github/workflows/release.yml`, which publishes
a GitHub Release whose notes are that version's changelog section
(`scripts/changelog_notes.py`).

The release commit is the only commit that reaches `main` without a pull
request. Two shared checks from
[drench44/ci-policy](https://github.com/drench44/ci-policy) run here:
`pr-policy` on every PR (the body states which review band ran, a PR that says
it fixes a regression changes a test, and a size limit), and `main-watch` on
every push to `main`, which opens an issue labeled `main-watch` for any commit
that did not come through a merged PR with green checks. Its allowlist accepts
exactly `release: vX.Y.Z` commits that touch only `VERSION`, `CHANGELOG.md` and
`src/house_climate/web/static/*.html`, which is what `release.py` writes.

Because browsers cache assets, **treat any deploy as at least a `patch`** so the
`?v=` moves and clients pick up the new bytes. Don't hand-edit `?v=` numbers — a
`test_static.py` guard fails CI on any drift.
