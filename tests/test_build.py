"""house_climate.web.build.compute_build — the static-asset fingerprint behind
/api/version's build token. DB-free (no app import), so it runs everywhere
unlike the DB-gated /api/version test."""
import re

import house_climate.web.build as b


def test_same_tree_yields_the_same_hash(tmp_path):
    (tmp_path / "a.css").write_text("x")
    (tmp_path / "b.js").write_text("y")
    assert b.compute_build(str(tmp_path)) == b.compute_build(str(tmp_path))


def test_a_changed_byte_changes_the_hash(tmp_path):
    (tmp_path / "a.css").write_text("x")
    before = b.compute_build(str(tmp_path))
    (tmp_path / "a.css").write_text("X")
    assert b.compute_build(str(tmp_path)) != before, \
        "the fingerprint must move when a served asset's bytes change"


def test_only_html_css_js_are_fingerprinted(tmp_path):
    (tmp_path / "a.css").write_text("x")
    base = b.compute_build(str(tmp_path))
    (tmp_path / "notes.txt").write_text("not a served asset")
    assert b.compute_build(str(tmp_path)) == base


def test_is_a_12_char_hex_token(tmp_path):
    (tmp_path / "a.js").write_text("x")
    assert re.fullmatch(r"[0-9a-f]{12}", b.compute_build(str(tmp_path)))


def test_stable_across_subdirectories(tmp_path):
    # dirs are walked sorted, so a nested asset contributes deterministically —
    # the same tree hashes the same even once a subdir exists.
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.js").write_text("z")
    (tmp_path / "a.css").write_text("x")
    assert b.compute_build(str(tmp_path)) == b.compute_build(str(tmp_path))


def test_empty_dir_does_not_raise(tmp_path):
    # a missing/empty static dir must degrade to a hash of nothing, never raise
    assert re.fullmatch(r"[0-9a-f]{12}", b.compute_build(str(tmp_path / "nope")))
