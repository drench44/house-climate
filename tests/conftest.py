import os
from pathlib import Path

import pytest
import psycopg

from house_climate import db

# The deployment config when present (private deployments commit config.json);
# the committed example otherwise (the public repo ships only the example).
_ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = str(next(p for p in (_ROOT / "config.json",
                                _ROOT / "config.example.json") if p.exists()))

TEST_DSN = os.environ.get("TEST_DB_DSN")  # e.g. postgresql://climate:climate@localhost:5433/climate


@pytest.fixture(scope="session", autouse=True)
def _require_db_in_ci():
    """Guard against CI going green having skipped every DB-backed test.

    Locally (no CI env var) a missing TEST_DB_DSN is a graceful skip — see
    the `conn` fixture below. In CI, the Postgres service + schema load are
    supposed to always be present, so a missing TEST_DB_DSN there means the
    workflow itself broke, not that DB tests are optional. Fail loud instead
    of silently skipping ~11 tests and reporting green.
    """
    if not TEST_DSN and os.environ.get("CI"):
        pytest.exit(
            "TEST_DB_DSN is unset in CI — the Postgres service or schema-load "
            "step is broken. DB-backed tests would silently skip and CI would "
            "go green having run none of the server logic. Refusing to continue.",
            returncode=1,
        )


@pytest.fixture
def conn():
    if not TEST_DSN:
        pytest.skip("TEST_DB_DSN not set; skipping DB-backed test")
    c = psycopg.connect(TEST_DSN, autocommit=True)
    db.ensure_app_schema(c)  # filter_events may be absent on an older test volume
    c.execute("TRUNCATE readings, poll_errors, devices, filter_events,"
              " sensor_readings, precip_daily, interventions, kv, air_readings")
    yield c
    c.close()
