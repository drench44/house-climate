"""The public engine must never contain house data — and this test must
never BE house data.

Hard rule learned the hard way: a public "no house data" test that lists
the operator's real strings (surname, hostnames, subnets, camera slugs,
email handle) as its scan patterns publishes every one of them. So this
file carries ZERO house-specific literals. It asserts only:

  * structure — the tree is git-enumerable and fail-closed in CI, so the
    guard can never silently pass having scanned nothing; and
  * generic shapes that are never OK in any public repo — real-format MAC
    addresses, house-precision GPS coordinate pairs, and private-key
    blocks — none of which name a specific household.

House-SPECIFIC detection (the actual subnets, hostnames, family names,
camera slugs, operator handles) lives ONLY in the private scanner's
denylist at garage/privacy/ and is enforced by the operator-side pre-push
hook. That denylist is itself house data and can never live in a public
repo — which is the whole reason it is kept separate from this file.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

# Generic shapes only — NEVER add a house-specific literal here (a surname,
# hostname, subnet, camera slug, operator handle). Those belong solely to the
# private denylist; putting them here would republish the very leak the guard
# exists to prevent.
FORBIDDEN = [
    r"\b[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}\b",   # real-format MAC (colon form)
    r"\b[0-9A-Fa-f]{2}(-[0-9A-Fa-f]{2}){5}\b",   # real-format MAC (hyphen form)
    # Paired GPS coordinates at house precision: 4+ decimals on BOTH numbers.
    # Examples/docs must use <= 3 decimals (city precision) to stay clear.
    r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",       # PEM private-key block
]

# Placeholder substrings that are NOT leaks. Each is stripped from a line
# BEFORE the FORBIDDEN check, so exempting a placeholder never exempts a real
# value that happens to share the line. Generic placeholders ONLY.
ALLOWED_LINES = [
    r"(?i)AA:BB:CC:DD:EE:FF|00:11:22:33:44:55",  # doc-placeholder MACs
]

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".db"}


def _tracked_files():
    """Files git actually tracks — i.e. what would ship to the PUBLIC repo.

    Scanning these (rather than everything, and skipping when a local
    config.json exists) is fail-CLOSED: an untracked/gitignored
    config.json is naturally excluded WITHOUT disabling the scan, and a
    config.json force-added to the index would be scanned and caught.
    Returns None if not a git checkout.
    """
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                             capture_output=True, check=True).stdout
    except Exception:
        return None
    return [s for s in out.decode().split("\0") if s]


def test_no_house_data():
    tracked = _tracked_files()
    # `not tracked` catches BOTH None (git ls-files failed — e.g. the merged
    # deploy tree has no .git) AND an empty list (git ran but enumerated zero
    # files — sparse/partial checkout, wrong cwd). Either way the scan would
    # cover nothing, so in CI that must FAIL, never pass: a guard that skips
    # is a guard that isn't there.
    if not tracked:
        if os.environ.get("CI"):
            pytest.fail(
                "guard could not enumerate a non-empty tracked tree in CI — "
                "refusing to pass by default (a guard that skips is a guard "
                "that isn't there)")
        pytest.skip(
            "no enumerable git tree: the merged private deploy tree (or a "
            "non-git checkout), where house data is expected — the guard "
            "protects the public engine repo only")
    hits = []
    for rel in tracked:
        p = ROOT / rel
        if not p.is_file() or p == SELF:
            continue
        if SKIP_DIRS & set(p.parts) or p.suffix in SKIP_SUFFIXES:
            continue
        text = p.read_text(errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            # Strip only the placeholder matches, then check the remainder —
            # so a real value sharing a line with a placeholder is NOT
            # exempted along with it.
            scrubbed = line
            for a in ALLOWED_LINES:
                scrubbed = re.sub(a, "", scrubbed)
            for pat in FORBIDDEN:
                m = re.search(pat, scrubbed)
                if m:
                    hits.append(f"{rel}:{lineno}: {m.group(0)!r}")
    assert not hits, \
        "generic-secret shapes in the public engine:\n" + "\n".join(hits)
