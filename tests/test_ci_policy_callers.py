"""Pins the drench44/ci-policy callers (.github/workflows/pr-policy.yml and
main-watch.yml, added 2026-09-23).

This repo is PUBLIC, so the callers must run on a GitHub-hosted runner: a
self-hosted runner on a public repo would run any fork's pull request on the
owner's box. The runner is a JSON string input under `with:`, and two refs
choose what runs, the `uses:` ref and `ci-policy-ref` (the policy code and
allowlist the job checks out); both must be one 40-hex commit, never @main.
Read as text at exact indentation: GitHub rejects a runs-on beside `uses:`,
so a moved line would make the check vanish rather than fail.
"""
import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _code(name: str) -> str:
    lines = (WORKFLOWS / name).read_text().splitlines()
    return "\n".join(l for l in lines if not l.lstrip().startswith("#"))


@pytest.mark.parametrize("name", ["pr-policy.yml", "main-watch.yml"])
def test_caller_is_pinned_and_runs_on_a_github_hosted_runner(name):
    text = _code(name)
    uses = re.findall(r"^\s+uses:\s*(\S+)", text, re.M)
    assert len(uses) == 1, uses
    m = re.fullmatch(r"drench44/ci-policy/\.github/workflows/([\w.-]+)@([0-9a-f]{40})", uses[0])
    assert m and m.group(1) == name, uses[0]
    assert re.search(r"^ {4}uses: drench44/ci-policy/", text, re.M)
    assert re.search(r"^ {4}with:\s*$", text, re.M)
    assert re.findall(r"^\s+ci-policy-ref:\s*(\S+)\s*$", text, re.M) == [m.group(2)]
    assert re.findall(r"^ {6}ci-policy-ref:\s*(\S+)\s*$", text, re.M) == [m.group(2)]
    assert re.findall(r"^\s+runs-on:\s*(.+?)\s*$", text, re.M) == ["""'"ubuntu-latest"'"""]
    assert re.findall(r"^ {6}runs-on:\s*(.+?)\s*$", text, re.M) == ["""'"ubuntu-latest"'"""]
    assert "self-hosted" not in text


def test_no_workflow_uses_a_self_hosted_runner():
    for w in sorted(WORKFLOWS.glob("*.y*ml")):
        assert "self-hosted" not in _code(w.name), w.name


# The whole caller, comments dropped, must be exactly the shared shape
# (2026-09-23): the triggers, the permissions each called workflow needs, one
# job with no `if:`, and only the inputs below. A trigger typo, a dropped
# `issues: write` or an `if: false` would otherwise make the check never run,
# with nothing red anywhere. Only the pinned SHA may vary (a bump edits both
# refs together).
CI_POLICY_RUNNER = """'"ubuntu-latest"'"""
CI_POLICY_HEAD = {
    "pr-policy.yml": (
        "name: pr-policy\non:\n  pull_request:\n"
        "    types: [opened, edited, synchronize, reopened, labeled, unlabeled, ready_for_review]\n"
        "permissions:\n  contents: read\n  pull-requests: read\njobs:\n  policy:\n"
    ),
    "main-watch.yml": (
        "name: main-watch\non:\n  push:\n    branches: [main]\n"
        "permissions:\n  contents: read\n  pull-requests: read\n  checks: read\n  statuses: read\n  issues: write\n"
        "jobs:\n  watch:\n"
    ),
}


@pytest.mark.parametrize("name", ["pr-policy.yml", "main-watch.yml"])
def test_ci_policy_caller_is_exactly_the_shared_shape(name):
    lines = (WORKFLOWS / name).read_text().splitlines()
    text = "".join(re.sub(r"\s+#.*$", "", l) + "\n" for l in lines if not l.lstrip().startswith("#"))
    sha = re.search(r"@([0-9a-f]{40})\n", text)
    assert sha, text
    want = (
        CI_POLICY_HEAD[name]
        + f"    uses: drench44/ci-policy/.github/workflows/{name}@{sha.group(1)}\n"
        + f"    with:\n      ci-policy-ref: {sha.group(1)}\n      runs-on: {CI_POLICY_RUNNER}\n"
        + {}.get(name, "")
    )
    assert text == want
