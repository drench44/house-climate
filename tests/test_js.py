"""Runs the executable JS tests (tests/js/*.test.mjs) as part of pytest.

The chart logic in common.js (timePath gap-splitting, gridLevels thinning,
hover math) is real logic that deserves real execution, not just static
grep contracts. There is no Node on the box, so the runner falls back to a
disposable node:20-alpine container (docker IS on the box); locally a plain
`node` is used when available. If neither exists the module skips loudly —
a skip here means the JS layer went unverified, not that it passed.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HC_ROOT = Path(__file__).resolve().parents[1]
JS_DIR = Path(__file__).resolve().parent / "js"


def _command(rel_files):
    """Explicit file list — `node --test <dir>` is flaky across Node majors.
    TAP reporter forced: Node 24 switched the piped-output default to the
    spec reporter, which breaks the `# fail 0` summary assertions below."""
    if shutil.which("node"):
        return ["node", "--test", "--test-reporter=tap",
                *[str(HC_ROOT / f) for f in rel_files]]
    if shutil.which("docker"):
        return [
            "docker", "run", "--rm",
            "-v", f"{HC_ROOT}:/hc:ro", "-w", "/hc",
            "node:20-alpine", "node", "--test", "--test-reporter=tap", *rel_files,
        ]
    return None


def test_js_suite_passes():
    rel_files = sorted(str(p.relative_to(HC_ROOT)) for p in JS_DIR.glob("*.test.mjs"))
    assert rel_files, "no JS test files found — tests/js went missing"
    cmd = _command(rel_files)
    if cmd is None:
        pytest.skip("neither node nor docker available — JS tests NOT run")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, (
        f"JS tests failed (runner: {cmd[0]}):\n{res.stdout}\n{res.stderr}"
    )
    # a runner that found zero tests exits 0 — refuse that as a pass
    assert "# tests 0" not in res.stdout, f"JS runner found no tests:\n{res.stdout}"
    assert "# fail 0" in res.stdout, f"unexpected node --test summary:\n{res.stdout}"
