"""scripts/check_changelog.py — the enforced "did you log this change?" guard.

Runs in CI (blocking on PRs) and as a local pre-commit hook. A change under
src/ must add a bullet to the CHANGELOG's [Unreleased] section; docs-only and
test-only diffs are exempt (mirrors the review-exemption rule in CLAUDE.md).
"""
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_changelog",
    Path(__file__).resolve().parents[1] / "scripts" / "check_changelog.py")
cc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cc)


# --- requires_entry ---------------------------------------------------------

def test_src_code_change_requires_an_entry():
    assert cc.requires_entry(["src/house_climate/web/app.py"]) is True


def test_docs_only_change_is_exempt():
    assert cc.requires_entry(["README.md", "docs/releasing.md"]) is False


def test_test_only_change_is_exempt():
    assert cc.requires_entry(["tests/test_api.py", "tests/js/common.test.mjs"]) is False


def test_src_markdown_is_exempt():
    assert cc.requires_entry(["src/house_climate/notes.md"]) is False


def test_mixed_change_with_src_code_requires_entry():
    assert cc.requires_entry(["README.md", "src/house_climate/web/app.py"]) is True


# --- gained_entry -----------------------------------------------------------

BASE = "## [Unreleased]\n### Added\n- old line\n\n## [1.0.0] — 2026-08-17\n- x\n"
HEAD_NEW = "## [Unreleased]\n### Added\n- old line\n- brand new line\n\n## [1.0.0] — 2026-08-17\n- x\n"


def test_gained_entry_true_when_a_new_bullet_appears():
    assert cc.gained_entry(BASE, HEAD_NEW) is True


def test_gained_entry_false_when_unreleased_unchanged():
    assert cc.gained_entry(BASE, BASE) is False


def test_gained_entry_false_when_only_a_released_section_changed():
    head = BASE.replace("- x", "- x\n- released tweak")
    assert cc.gained_entry(BASE, head) is False


# --- main() integration: the actual BLOCK/PASS enforcement -------------------
# The pure predicates above don't prove the guard blocks. These drive main() in
# a throwaway git repo so a defanged guard (inverted condition, wrong ref,
# silent-open on git error) fails a test instead of silently shipping.

import subprocess


def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


def _write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


_SEED = ("## [Unreleased]\n### Added\n- seed\n\n"
         "## [1.0.0] — 2026-08-17\n### Added\n- base\n")


def test_main_staged_BLOCKS_src_change_without_entry(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _write(repo, "CHANGELOG.md", _SEED)
    _write(repo, "src/house_climate/app.py", "x = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "init")
    _write(repo, "src/house_climate/app.py", "x = 2\n")   # src change, NO new bullet
    _git(repo, "add", "src/house_climate/app.py")
    monkeypatch.setattr(cc, "REPO_ROOT", repo)
    assert cc.main(["--staged"]) == 1


def test_main_staged_PASSES_when_entry_added(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _write(repo, "CHANGELOG.md", _SEED)
    _write(repo, "src/house_climate/app.py", "x = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "init")
    _write(repo, "src/house_climate/app.py", "x = 2\n")
    _write(repo, "CHANGELOG.md", _SEED.replace("- seed\n", "- seed\n- new thing\n"))
    _git(repo, "add", "-A")
    monkeypatch.setattr(cc, "REPO_ROOT", repo)
    assert cc.main(["--staged"]) == 0


def test_main_staged_EXEMPTS_docs_only(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _write(repo, "CHANGELOG.md", _SEED)
    _write(repo, "README.md", "hi\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "init")
    _write(repo, "README.md", "hi there\n")
    _git(repo, "add", "README.md")
    monkeypatch.setattr(cc, "REPO_ROOT", repo)
    assert cc.main(["--staged"]) == 0


def test_main_base_BLOCKS_pr_branched_before_a_release(tmp_path, monkeypatch):
    """A PR branched before a release, changing src with no new bullet, must be
    BLOCKED — even though the release emptied [Unreleased] on the base tip.
    Reading the tip (empty) instead of the merge base let this pass for free."""
    repo = _repo(tmp_path)
    _write(repo, "CHANGELOG.md", _SEED)               # [Unreleased] has 'seed'
    _write(repo, "src/house_climate/app.py", "x = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "M")
    default = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/house_climate/app.py", "x = 2\n")   # src change, NO new bullet
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "feature")
    _git(repo, "checkout", "-q", default)
    # cut a release on the base: [Unreleased] goes empty
    _write(repo, "CHANGELOG.md",
           "## [Unreleased]\n\n## [1.1.0] — 2026-08-18\n### Added\n- seed\n\n"
           "## [1.0.0] — 2026-08-17\n### Added\n- base\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "release 1.1.0")
    _git(repo, "checkout", "-q", "feature")
    monkeypatch.setattr(cc, "REPO_ROOT", repo)
    assert cc.main(["--base", default]) == 1


def test_main_base_fails_LOUD_on_unresolvable_ref(tmp_path, monkeypatch):
    """An enforcement gate must fail CLOSED: a bad ref raises, it does not
    silently pass (the old _show swallowed git errors and waved PRs through)."""
    repo = _repo(tmp_path)
    _write(repo, "CHANGELOG.md", _SEED)
    _write(repo, "src/house_climate/app.py", "x = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.setattr(cc, "REPO_ROOT", repo)
    with pytest.raises(subprocess.CalledProcessError):
        cc.main(["--base", "no-such-ref"])
