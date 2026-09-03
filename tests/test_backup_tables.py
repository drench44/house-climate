"""The backup's verify list must cover every table the schema creates.

`backup/house-climate-backup.sh` proves a restore worked by counting rows in
the tables named in HC_VERIFY_TABLES. A table added to the schema later, but
not to that list, would be dumped and restored without anyone ever checking it
came back — and would be discovered missing only in an emergency. This test is
the tripwire, and it needs no database.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_INIT_SQL = (_ROOT / "db" / "init.sql").read_text()
_BACKUP_SH = (_ROOT / "backup" / "house-climate-backup.sh").read_text()

# Tables deliberately left out of the restore verify, with the reason. Empty
# today: everything in the schema holds history worth proving.
UNVERIFIED_OK: dict[str, str] = {}


def _schema_tables():
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", _INIT_SQL))


def _verified_tables():
    m = re.search(r'HC_VERIFY_TABLES="\$\{HC_VERIFY_TABLES:-([^}]*)\}"', _BACKUP_SH)
    assert m, "HC_VERIFY_TABLES default not found — did the assignment change shape?"
    return set(m.group(1).split())


def test_every_schema_table_is_verified_after_a_restore():
    missing = sorted(_schema_tables() - _verified_tables() - set(UNVERIFIED_OK))
    assert not missing, (
        f"tables in db/init.sql that no restore check would notice losing: {missing}. "
        "Add them to HC_VERIFY_TABLES in backup/house-climate-backup.sh, or to "
        "UNVERIFIED_OK here with a reason.")


def test_the_verify_list_names_no_table_that_does_not_exist():
    """A typo would fail the restore self-test in CI with a confusing 'table
    missing' — catch it here instead, where the message says what is wrong."""
    unknown = sorted(_verified_tables() - _schema_tables())
    assert not unknown, f"HC_VERIFY_TABLES names tables db/init.sql never creates: {unknown}"


def test_the_schema_actually_creates_tables():
    """Guards the regex: if init.sql changed shape, both tests above would pass
    vacuously by comparing two empty sets."""
    assert len(_schema_tables()) >= 5
    assert len(_verified_tables()) >= 5


def test_the_tripwire_would_actually_fire(monkeypatch):
    """A test that can only pass is worth nothing. Simulate a table added to
    the schema and left out of the verify list, and confirm the check fails."""
    import sys
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "_schema_tables",
                        lambda: _verified_tables() | {"brand_new_table"})
    try:
        mod.test_every_schema_table_is_verified_after_a_restore()
    except AssertionError as e:
        assert "brand_new_table" in str(e)
    else:
        raise AssertionError("a table missing from HC_VERIFY_TABLES went unnoticed")
