"""scripts/changelog_notes.py — extract one version's section from CHANGELOG.md.

The release workflow (.github/workflows/release.yml) runs this on a pushed
`vX.Y.Z` tag and feeds the output to `gh release create` as the GitHub Release
notes, so the changelog is the single source for release notes too.
"""
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "changelog_notes",
    Path(__file__).resolve().parents[1] / "scripts" / "changelog_notes.py")
cn = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cn)

CHANGELOG = """\
# Changelog

## [Unreleased]
### Added
- not yet

## [1.1.0] — 2026-08-20
### Added
- A new thing
- Another thing
### Fixed
- A bug

## [1.0.0] — 2026-08-17
### Added
- Baseline
"""


def test_notes_for_returns_that_versions_body():
    notes = cn.notes_for(CHANGELOG, "v1.1.0")
    assert "A new thing" in notes and "A bug" in notes
    assert "Baseline" not in notes          # stops at the next release
    assert "not yet" not in notes           # never the Unreleased section
    assert "## [1.1.0]" not in notes        # heading itself is not in the body


def test_notes_for_accepts_tag_with_or_without_v():
    assert cn.notes_for(CHANGELOG, "1.1.0") == cn.notes_for(CHANGELOG, "v1.1.0")


def test_notes_for_unknown_version_is_empty():
    assert cn.notes_for(CHANGELOG, "v9.9.9").strip() == ""


def test_notes_for_the_last_section_has_no_next_heading():
    """The final release has no following '## [' — exercise the 'else rest'
    branch so a regression can't over- or under-read the file's last release."""
    notes = cn.notes_for(CHANGELOG, "v1.0.0")
    assert "Baseline" in notes
    assert "1.1.0" not in notes


def test_main_prints_the_section_for_a_known_version(tmp_path, monkeypatch, capsys):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(cn, "REPO_ROOT", tmp_path)
    assert cn.main(["v1.1.0"]) == 0
    assert "A new thing" in capsys.readouterr().out


def test_main_falls_back_to_the_tag_name_so_notes_are_never_empty(tmp_path, monkeypatch, capsys):
    """release.yml pipes this stdout into `gh release create --notes-file`; an
    unknown version must print the tag, never an empty body."""
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(cn, "REPO_ROOT", tmp_path)
    assert cn.main(["v9.9.9"]) == 0
    assert capsys.readouterr().out.strip() == "v9.9.9"


def test_main_usage_error_returns_2(capsys):
    assert cn.main([]) == 2


def test_main_falls_back_when_changelog_is_missing(tmp_path, monkeypatch, capsys):
    """No CHANGELOG.md at release time must not traceback the release step — it
    prints the tag name so the Release still publishes."""
    monkeypatch.setattr(cn, "REPO_ROOT", tmp_path)   # empty dir, no CHANGELOG.md
    assert cn.main(["v2.0.0"]) == 0
    assert capsys.readouterr().out.strip() == "v2.0.0"
