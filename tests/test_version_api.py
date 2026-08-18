"""/api/version — a debug/ops readout of what's actually deployed.

Returns the running SemVer plus the build hash (a sha256 over the baked static
assets). No changelog — that lives on GitHub. Must never 500.

Importing house_climate.web.app opens a real DB connection + starts the alert
thread at import (module-level load_secrets() + db.connect()), so this is
DB-gated like the other HTTP-layer app tests: skipped (not failed) without
TEST_DB_DSN, run in CI against Postgres.
"""
import os
import re

import pytest

TEST_DSN = os.environ.get("TEST_DB_DSN")
if not TEST_DSN:
    pytest.skip("TEST_DB_DSN not set; skipping HTTP-layer version tests",
                allow_module_level=True)

from conftest import CFG_PATH  # noqa: E402 (must follow the skip above)

# house_climate.web.app reads these at IMPORT time — set before importing it.
os.environ.setdefault("DB_DSN", TEST_DSN)
os.environ.setdefault("DAIKIN_API_KEY", "test-key")
os.environ.setdefault("DAIKIN_INTEGRATOR_TOKEN", "test-token")
os.environ.setdefault("DAIKIN_EMAIL", "test@example.com")
os.environ.setdefault("CONFIG_PATH", CFG_PATH)
os.environ.setdefault("CLIMATE_ALLOWED_HOSTS", "testserver")

from fastapi.testclient import TestClient  # noqa: E402

import house_climate  # noqa: E402
from house_climate.web.app import app  # noqa: E402

client = TestClient(app)


def test_version_endpoint_reports_version_and_build():
    body = client.get("/api/version").json()
    assert body["version"] == house_climate.__version__
    assert re.fullmatch(r"[0-9a-f]{12}", body["build"]), \
        f"build must be a 12-char hex token, got {body.get('build')!r}"


def test_version_endpoint_carries_no_changelog():
    """The changelog lives on GitHub, not the dashboard — the endpoint is a bare
    version readout, not a release-notes feed."""
    body = client.get("/api/version").json()
    assert "entries" not in body


def test_version_endpoint_never_500s():
    assert client.get("/api/version").status_code == 200
