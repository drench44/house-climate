"""house-climate's version — single source of truth.

`VERSION` (repo root) holds one SemVer string. `read_version()` reads it for
`house_climate.__version__` and the `/api/version` debug endpoint. The changelog
lives on GitHub (CHANGELOG.md, tags/Releases); it is NOT parsed or served here.

Stdlib only. Never raises — import must not fail on a partial deploy or a
non-UTF-8 locale, so a missing/undecodable VERSION degrades to a marked
fallback.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# repo root = two levels up from this file (src/house_climate/version.py). Tests
# monkeypatch this to point at a fixture tree.
REPO_ROOT = Path(__file__).resolve().parents[2]

_FALLBACK_VERSION = "0.0.0+unknown"


def read_version() -> str:
    """The SemVer string from VERSION, or a clearly-marked fallback if the file
    is missing/empty/undecodable (never raises). Explicit UTF-8 so it decodes
    identically regardless of the box's locale."""
    try:
        v = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        return v or _FALLBACK_VERSION
    except (OSError, ValueError):  # ValueError covers UnicodeDecodeError
        log.warning("VERSION unreadable/undecodable — using %s", _FALLBACK_VERSION)
        return _FALLBACK_VERSION
