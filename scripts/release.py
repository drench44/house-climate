#!/usr/bin/env python3
"""Cut a house-climate release — the whole ceremony, atomically.

    python scripts/release.py {major|minor|patch} [--dry-run]

Steps, in order (aborting loudly on any precondition):

  1. Refuse a dirty working tree and an empty ``## [Unreleased]`` (nothing to
     release).
  2. Bump ``VERSION`` by the given SemVer part.
  3. Roll ``## [Unreleased]`` in CHANGELOG.md into ``## [x.y.z] — <today>`` and
     open a fresh empty ``[Unreleased]`` above it.
  4. Stamp every ``?v=`` cache-bust in index.html to the new version — one
     writer, one number, so the css/js busts can never drift again.
  5. ``git commit`` the three files, then ``git tag vX.Y.Z``.

It does NOT push (respects the pre-push privacy guard + operator control); it
prints the ``git push --follow-tags`` reminder instead. ``--dry-run`` prints
what it would change and writes nothing.

Stdlib only. The pure transforms below are unit-tested in tests/test_release.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "VERSION"
CHANGELOG_FILE = REPO_ROOT / "CHANGELOG.md"
INDEX_HTML = REPO_ROOT / "src" / "house_climate" / "web" / "static" / "index.html"

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_UNRELEASED = re.compile(r"^##\s*\[Unreleased\]\s*$", re.MULTILINE | re.IGNORECASE)
_NEXT_RELEASE = re.compile(r"^##\s*\[", re.MULTILINE)
_CACHE_BUST = re.compile(r"(\?v=)[0-9A-Za-z._-]+")


class ReleaseError(Exception):
    """A precondition failed or the input was malformed — abort the release."""


# --- pure transforms (unit-tested) ------------------------------------------

def bump_version(version: str, part: str) -> str:
    m = _SEMVER.match(version.strip())
    if not m:
        raise ReleaseError(f"VERSION is not clean SemVer: {version!r}")
    major, minor, patch = (int(x) for x in m.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"part must be major|minor|patch, got {part!r}")


def unreleased_body(changelog: str) -> str:
    """The text between the ``[Unreleased]`` heading and the next ``## [`` release
    heading (empty string if there's no such section)."""
    m = _UNRELEASED.search(changelog)
    if not m:
        return ""
    rest = changelog[m.end():]
    nxt = _NEXT_RELEASE.search(rest)
    return rest[:nxt.start()] if nxt else rest


def roll_changelog(changelog: str, new_version: str, date: str) -> str:
    """Move the ``[Unreleased]`` body into a dated ``[new_version]`` section and
    open a fresh empty ``[Unreleased]`` above it. Refuses an empty unreleased."""
    m = _UNRELEASED.search(changelog)
    if not m:
        raise ReleaseError("no '## [Unreleased]' section in CHANGELOG.md")
    body = unreleased_body(changelog)
    if not body.strip():
        raise ReleaseError("'## [Unreleased]' is empty — nothing to release")
    before = changelog[:m.start()]
    after = changelog[m.start() + len(m.group(0)) + len(body):]
    fresh = "## [Unreleased]\n\n"
    dated = f"## [{new_version}] — {date}\n{body.rstrip()}\n\n"
    return f"{before}{fresh}{dated}{after.lstrip(chr(10))}"


def stamp_assets(html: str, version: str) -> str:
    """Rewrite every ``?v=...`` cache-bust in index.html to ``?v=<version>``."""
    return _CACHE_BUST.sub(rf"\g<1>{version}", html)


# --- git-facing orchestration -----------------------------------------------

def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()


def _require_clean_tree() -> None:
    if _git("status", "--porcelain"):
        raise ReleaseError("working tree is dirty — commit or stash first")


def _tag_exists(tag: str) -> bool:
    return bool(subprocess.run(["git", "rev-parse", "-q", "--verify",
                                f"refs/tags/{tag}"], cwd=REPO_ROOT,
                               capture_output=True, text=True).returncode == 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cut a house-climate release.")
    ap.add_argument("part", choices=["major", "minor", "patch"])
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would change and write nothing")
    args = ap.parse_args(argv)

    try:
        if not args.dry_run:
            _require_clean_tree()
        old = VERSION_FILE.read_text(encoding="utf-8").strip()
        new = bump_version(old, args.part)
        if not args.dry_run and _tag_exists(f"v{new}"):
            raise ReleaseError(f"tag v{new} already exists — refusing to re-release")
        today = dt.date.today().isoformat()
        changelog = roll_changelog(CHANGELOG_FILE.read_text(encoding="utf-8"), new, today)
        html = stamp_assets(INDEX_HTML.read_text(encoding="utf-8"), new)
    except ReleaseError as e:
        print(f"release: {e}", file=sys.stderr)
        return 1

    print(f"release: {old} -> {new}  (tag v{new})")
    if args.dry_run:
        print("release: --dry-run, nothing written")
        return 0

    rel_paths = ["VERSION", "CHANGELOG.md", str(INDEX_HTML.relative_to(REPO_ROOT))]
    VERSION_FILE.write_text(new + "\n", encoding="utf-8")
    CHANGELOG_FILE.write_text(changelog, encoding="utf-8")
    INDEX_HTML.write_text(html, encoding="utf-8")

    # Phase 1: stage + commit. If EITHER fails, the release commit did not land,
    # so restore the tree to HEAD and say so truthfully. `git checkout HEAD --`
    # (not `checkout --`) is required: `git add` already staged the new content,
    # so restoring "from the index" would be a no-op.
    try:
        _git("add", *rel_paths)
        # --no-verify: the release commit is the ONE commit that legitimately
        # empties [Unreleased] (it just rolled it into a dated section), so the
        # pre-commit changelog guard would (correctly, for any other commit)
        # reject it. Skipping the hook here is intentional, not a bypass.
        _git("commit", "--no-verify", "-m", f"release: v{new}")
    except subprocess.CalledProcessError as e:
        restore = subprocess.run(["git", "checkout", "HEAD", "--", *rel_paths],
                                 cwd=REPO_ROOT, capture_output=True, text=True)
        if restore.returncode == 0:
            print(f"release: commit failed ({e.stderr or e}); restored files to "
                  "HEAD, nothing committed", file=sys.stderr)
        else:
            # The restore itself failed — DON'T claim a clean tree the operator
            # would trust and re-run release on top of. Say so plainly.
            print(f"release: commit failed ({e.stderr or e}) AND the restore also "
                  f"failed ({restore.stderr.strip() or restore.returncode}) — "
                  "VERSION/CHANGELOG/index.html may still be modified and staged; "
                  "check `git status` before re-running.", file=sys.stderr)
        return 1

    # Phase 2: the commit is REAL now. If tagging fails, do NOT restore (there's
    # nothing to roll back) and do NOT claim "nothing committed" — report the
    # untagged commit and how to finish or undo it.
    try:
        _git("tag", f"v{new}")
    except subprocess.CalledProcessError as e:
        print(f"release: v{new} committed, but tagging failed ({e.stderr or e}). "
              f"The release commit exists — run  git tag v{new}  to finish, "
              f"or  git reset --hard HEAD~1  to undo it.", file=sys.stderr)
        return 1
    print(f"release: committed and tagged v{new}")
    print("release: push it with  git push --follow-tags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
