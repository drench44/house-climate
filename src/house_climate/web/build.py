"""The build fingerprint — a hash of the baked static assets.

A 12-char hex token over the served html/css/js, so `/api/version` can answer
"what bytes are actually running?" (distinct from the human SemVer). Kept
DB-free (no app import) so it is unit-testable and can never drag a DB
connection into a version read.
"""
from __future__ import annotations

import hashlib
import os


def compute_build(static_dir: str) -> str:
    """A stable 12-char hex fingerprint over every html/css/js under
    `static_dir`. Deterministic — dirs AND files are walked in sorted order, so
    the same tree yields the same token on any machine even if a subdirectory is
    added later. Never raises: an unreadable asset is skipped, so a bad read at
    import can't crash the module."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(static_dir):
        dirs.sort()  # deterministic cross-subdirectory order across machines
        for name in sorted(files):
            if name.endswith((".html", ".css", ".js")):
                path = os.path.join(root, name)
                try:
                    h.update(os.path.relpath(path, static_dir).encode())
                    with open(path, "rb") as f:
                        h.update(f.read())
                except OSError:
                    continue
    return h.hexdigest()[:12]
