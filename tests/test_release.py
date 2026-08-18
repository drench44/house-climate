"""scripts/release.py — the version bump + changelog roll + asset stamp ceremony.

These pin the pure transforms (no real git needed): SemVer math, rolling
`[Unreleased]` into a dated release with a fresh empty `[Unreleased]` on top,
and stamping every `?v=` in index.html to the new version so the cache-busts
can never drift.
"""
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "release", Path(__file__).resolve().parents[1] / "scripts" / "release.py")
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)


# --- bump_version -----------------------------------------------------------

@pytest.mark.parametrize("version,part,expected", [
    ("1.2.3", "major", "2.0.0"),
    ("1.2.3", "minor", "1.3.0"),
    ("1.2.3", "patch", "1.2.4"),
    ("0.0.0", "minor", "0.1.0"),
])
def test_bump_version(version, part, expected):
    assert release.bump_version(version, part) == expected


def test_bump_version_rejects_bad_part():
    with pytest.raises(release.ReleaseError):
        release.bump_version("1.2.3", "sideways")


def test_bump_version_rejects_non_semver():
    with pytest.raises(release.ReleaseError):
        release.bump_version("v1.2", "patch")


# --- changelog roll ---------------------------------------------------------

CHANGELOG = """\
# Changelog

## [Unreleased]

### Added
- A new thing

## [1.0.0] — 2026-08-17
### Added
- Baseline
"""


def test_roll_changelog_moves_unreleased_into_a_dated_release():
    out = release.roll_changelog(CHANGELOG, "1.1.0", "2026-08-20")
    assert "## [1.1.0] — 2026-08-20" in out
    # the moved body rides along under the new dated heading
    dated = out.split("## [1.1.0]")[1].split("## [1.0.0]")[0]
    assert "- A new thing" in dated


def test_roll_changelog_opens_a_fresh_empty_unreleased_on_top():
    out = release.roll_changelog(CHANGELOG, "1.1.0", "2026-08-20")
    assert out.count("## [Unreleased]") == 1
    # the surviving [Unreleased] sits ABOVE the new release and carries no bullets
    head = out.split("## [1.1.0]")[0]
    assert "## [Unreleased]" in head
    assert "- A new thing" not in head


def test_unreleased_body_detects_content_and_emptiness():
    assert release.unreleased_body(CHANGELOG).strip() != ""
    empty = "# Changelog\n\n## [Unreleased]\n\n## [1.0.0] — 2026-08-17\n- x\n"
    assert release.unreleased_body(empty).strip() == ""


def test_roll_changelog_refuses_empty_unreleased():
    empty = "# Changelog\n\n## [Unreleased]\n\n## [1.0.0] — 2026-08-17\n- x\n"
    with pytest.raises(release.ReleaseError):
        release.roll_changelog(empty, "1.1.0", "2026-08-20")


# --- asset stamping ---------------------------------------------------------

def test_stamp_assets_rewrites_every_cache_bust_to_the_version():
    html = ('<link href="styles.css?v=80">\n'
            '<script src="common.js?v=7"></script>\n'
            '<script src="app.js?v=79"></script>')
    out = release.stamp_assets(html, "1.1.0")
    assert "styles.css?v=1.1.0" in out
    assert "common.js?v=1.1.0" in out
    assert "app.js?v=1.1.0" in out
    assert "?v=80" not in out and "?v=79" not in out and "?v=7" not in out


def test_stamp_assets_is_idempotent():
    html = '<script src="app.js?v=1.1.0"></script>'
    assert release.stamp_assets(html, "1.1.0") == html


# --- main() : preconditions + the real commit/tag ceremony ------------------

import subprocess


def test_require_clean_tree_raises_when_dirty(monkeypatch):
    monkeypatch.setattr(release, "_git",
                        lambda *a: "M foo" if a and a[0] == "status" else "")
    with pytest.raises(release.ReleaseError):
        release._require_clean_tree()


def test_require_clean_tree_ok_when_clean(monkeypatch):
    monkeypatch.setattr(release, "_git", lambda *a: "")
    release._require_clean_tree()   # must not raise


def test_main_dry_run_writes_nothing(tmp_path, monkeypatch):
    """--dry-run must not mutate the release files (or touch git). Runs against a
    fixture with a NON-EMPTY [Unreleased] so it is independent of the live repo's
    release state: right after a real release [Unreleased] is empty, which makes
    a cut abort (rc 1) — so pinning this to the live tree would break the moment
    the first real release lands. The fixture keeps the intent (dry-run mutates
    nothing) without that coupling."""
    vf = tmp_path / "VERSION"
    vf.write_text("1.2.3\n")
    cf = tmp_path / "CHANGELOG.md"
    cf.write_text("# Changelog\n\n## [Unreleased]\n\n### Added\n- a change\n\n"
                  "## [1.2.3] — 2026-01-01\n\n### Added\n- prior\n")
    hf = tmp_path / "index.html"
    hf.write_text('<link href="styles.css?v=1.2.3">\n')
    monkeypatch.setattr(release, "VERSION_FILE", vf)
    monkeypatch.setattr(release, "CHANGELOG_FILE", cf)
    monkeypatch.setattr(release, "INDEX_HTML", hf)
    before = [f.read_bytes() for f in (vf, cf, hf)]
    rc = release.main(["patch", "--dry-run"])
    assert rc == 0, "dry-run against a releasable tree succeeds"
    assert [f.read_bytes() for f in (vf, cf, hf)] == before, "dry-run writes nothing"


def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


def _release_repo(tmp_path, monkeypatch):
    """A throwaway repo wired exactly like a real clone: the changelog guard
    installed as a pre-commit hook (core.hooksPath=.githooks). Points release.py's
    file/root constants at it."""
    src_root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    (repo / "src" / "house_climate" / "web" / "static").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / ".githooks").mkdir()
    # the real guard + hook, so the release commit meets the same gate a clone has
    (repo / "scripts" / "check_changelog.py").write_text(
        (src_root / "scripts" / "check_changelog.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    hook = repo / ".githooks" / "pre-commit"
    hook.write_text((src_root / ".githooks" / "pre-commit").read_text(encoding="utf-8"),
                    encoding="utf-8")
    hook.chmod(0o755)
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(
        "## [Unreleased]\n### Added\n- a shipped-this-cycle thing\n\n"
        "## [1.0.0] — 2026-08-17\n### Added\n- base\n", encoding="utf-8")
    (repo / "src" / "house_climate" / "web" / "static" / "index.html").write_text(
        '<link href="styles.css?v=1.0.0"><script src="app.js?v=1.0.0"></script>\n',
        encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.hooksPath", ".githooks")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "--no-verify", "-m", "init")
    monkeypatch.setattr(release, "REPO_ROOT", repo)
    monkeypatch.setattr(release, "VERSION_FILE", repo / "VERSION")
    monkeypatch.setattr(release, "CHANGELOG_FILE", repo / "CHANGELOG.md")
    monkeypatch.setattr(release, "INDEX_HTML",
                        repo / "src" / "house_climate" / "web" / "static" / "index.html")
    return repo


def test_main_release_commits_and_tags_despite_the_pre_commit_hook(tmp_path, monkeypatch):
    """The release commit empties [Unreleased], which the changelog guard would
    (correctly, for any other commit) reject. release.py must still succeed on a
    hooks-installed clone — it commits with --no-verify. Without that, this
    release aborts and leaves a dirty, half-released tree."""
    repo = _release_repo(tmp_path, monkeypatch)
    rc = release.main(["minor"])
    assert rc == 0
    assert (repo / "VERSION").read_text(encoding="utf-8").strip() == "1.1.0"
    assert "## [1.1.0]" in (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert _git(repo, "tag", "--list", "v1.1.0").strip() == "v1.1.0"
    # tree is clean (everything committed, nothing half-written)
    assert _git(repo, "status", "--porcelain").strip() == ""
    # the cache-busts were stamped to the new version
    assert "?v=1.1.0" in (repo / "src" / "house_climate" / "web" / "static"
                          / "index.html").read_text(encoding="utf-8")


def test_main_refuses_when_tag_already_exists(tmp_path, monkeypatch):
    repo = _release_repo(tmp_path, monkeypatch)
    _git(repo, "tag", "v1.0.1")           # the patch target already taken
    rc = release.main(["patch"])
    assert rc == 1
    assert (repo / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"  # untouched
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_main_rolls_back_when_the_commit_fails(tmp_path, monkeypatch):
    """If the commit fails after the files are written (e.g. signing is required
    and fails — --no-verify skips hooks, not signing), the tree must be restored
    to HEAD: not left with staged bumped files, and no false 'restored' when it
    wasn't. git checkout -- (from the index) is a no-op after `git add`; the fix
    must restore from HEAD."""
    repo = _release_repo(tmp_path, monkeypatch)
    _git(repo, "config", "commit.gpgsign", "true")
    _git(repo, "config", "gpg.program", "/bin/false")   # signing always fails
    rc = release.main(["minor"])
    assert rc == 1
    assert (repo / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0", \
        "VERSION must be restored to HEAD, not left bumped"
    assert _git(repo, "status", "--porcelain").strip() == "", \
        "no staged/half-written files may linger after a failed release"


def test_main_does_not_falsely_roll_back_a_landed_commit(tmp_path, monkeypatch):
    """If the commit LANDS but tagging fails, the release commit is real — the
    script must NOT restore (there's nothing to restore) and must NOT claim
    'nothing committed'. It reports the untagged commit instead."""
    repo = _release_repo(tmp_path, monkeypatch)
    real_git = release._git

    def fake_git(*a):
        if a and a[0] == "tag":
            raise subprocess.CalledProcessError(1, ["git", "tag"], stderr="boom")
        return real_git(*a)

    monkeypatch.setattr(release, "_git", fake_git)
    rc = release.main(["minor"])
    assert rc == 1
    # the commit is real and left intact
    assert real_git("log", "-1", "--format=%s").strip() == "release: v1.1.0"
    assert (repo / "VERSION").read_text(encoding="utf-8").strip() == "1.1.0"
    # the tag genuinely did not get created
    assert real_git("tag", "--list", "v1.1.0").strip() == ""


def test_main_reports_honestly_when_the_restore_also_fails(tmp_path, monkeypatch, capsys):
    """Commit fails AND the recovery `git checkout HEAD --` also fails: the
    operator must NOT be told 'restored ... nothing committed' (a lie they'd
    re-run release on top of) — the message must flag the tree may still be
    dirty."""
    repo = _release_repo(tmp_path, monkeypatch)
    _git(repo, "config", "commit.gpgsign", "true")
    _git(repo, "config", "gpg.program", "/bin/false")   # the commit fails
    real_run = subprocess.run

    def fake_run(cmd, *a, **k):
        if isinstance(cmd, list) and cmd[:3] == ["git", "checkout", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 1, "", "checkout boom")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    assert release.main(["minor"]) == 1
    err = capsys.readouterr().err.lower()
    assert "restore also failed" in err
    assert "nothing committed" not in err
