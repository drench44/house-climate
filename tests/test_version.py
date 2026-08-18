"""house_climate.version.read_version — the single-source-of-truth SemVer reader.

Behind house_climate.__version__ and the /api/version debug endpoint. Never
raises (a partial deploy or a non-UTF-8 locale must not break import); explicit
UTF-8 so it decodes identically regardless of the box's locale.
"""
import house_climate.version as fv


def test_read_version_returns_the_file_contents(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("1.4.2\n")
    monkeypatch.setattr(fv, "REPO_ROOT", tmp_path)
    assert fv.read_version() == "1.4.2"


def test_read_version_falls_back_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(fv, "REPO_ROOT", tmp_path)
    assert fv.read_version().startswith("0.0.0")


def test_read_version_falls_back_on_empty_file(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("   \n")
    monkeypatch.setattr(fv, "REPO_ROOT", tmp_path)
    assert fv.read_version().startswith("0.0.0")


def test_read_version_survives_undecodable_file(tmp_path, monkeypatch):
    """UnicodeDecodeError is a ValueError, not OSError — if it escaped, it would
    kill the app at import (house_climate.__version__)."""
    (tmp_path / "VERSION").write_bytes(b"\xff\xfe\x80")
    monkeypatch.setattr(fv, "REPO_ROOT", tmp_path)
    assert fv.read_version().startswith("0.0.0")
