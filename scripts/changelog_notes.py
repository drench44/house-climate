#!/usr/bin/env python3
"""Print one version's CHANGELOG.md section — the GitHub Release notes.

    python scripts/changelog_notes.py v1.1.0

The release workflow (.github/workflows/release.yml) runs this on a pushed
`vX.Y.Z` tag and pipes the output to `gh release create` as the release body, so
CHANGELOG.md is the single source for the changelog AND the GitHub Release notes.

Stdlib only; the pure extraction is unit-tested in tests/test_changelog_notes.py.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_NEXT_RELEASE = re.compile(r"^##\s*\[", re.MULTILINE)


def notes_for(changelog: str, tag: str) -> str:
    """The body under ``## [<version>] …`` up to the next ``## [`` heading, with
    the heading itself excluded. ``tag`` may be ``v1.1.0`` or ``1.1.0``. Returns
    ``""`` if that version has no section."""
    version = tag[1:] if tag.startswith("v") else tag
    heading = re.compile(rf"^##\s*\[{re.escape(version)}\][^\n]*$", re.MULTILINE)
    m = heading.search(changelog)
    if not m:
        return ""
    rest = changelog[m.end():]
    nxt = _NEXT_RELEASE.search(rest)
    return (rest[:nxt.start()] if nxt else rest).strip()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: changelog_notes.py <tag>", file=sys.stderr)
        return 2
    try:
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        changelog = ""   # missing/unreadable → fall through to the tag-name fallback
    notes = notes_for(changelog, argv[0])
    # Fall back to the tag name so a release is never created with empty notes.
    print(notes if notes else argv[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
