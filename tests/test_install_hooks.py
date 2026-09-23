"""scripts/install-hooks.sh: plain vs chained behind the ci-policy global hooks.

Each case runs the real script in a throwaway repo with its own HOME and
global git config, so the machine's real config is never read or written.
Why it matters: setting core.hooksPath=.githooks when the ci-policy global
hooks are installed silently bypasses them (seen 2026-09-23).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-hooks.sh"
STAMP = "# ci-policy managed: global pre-push hook. Do not edit.\n"


@pytest.fixture
def repo(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.environ["PATH"],
           "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, work / "scripts" / "install-hooks.sh")
    subprocess.run(["git", "init", "-q"], cwd=work, env=env, check=True)

    def git(*args):
        return subprocess.run(["git", *args], cwd=work, env=env, capture_output=True, text=True)

    def run():
        return subprocess.run(["bash", "scripts/install-hooks.sh"], cwd=work, env=env,
                              capture_output=True, text=True, check=True)

    def global_hooks(stamp=STAMP, executable=True):
        hooks = home / "global-hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "pre-push").write_text("#!/usr/bin/env bash\n" + stamp)
        (hooks / "pre-push").chmod(0o755 if executable else 0o644)
        subprocess.run(["git", "config", "--global", "core.hooksPath", str(hooks)],
                       env=env, check=True)

    return git, run, global_hooks


def local(git, key):
    return git("config", "--local", "--get", key).stdout.strip()


def test_no_global_hooks_sets_the_plain_hooks_path(repo):
    git, run, _ = repo
    out = run().stdout
    assert local(git, "core.hooksPath") == ".githooks"
    assert local(git, "ci-policy.chainHooksPath") == ""
    assert "core.hooksPath=.githooks" in out


def test_ci_policy_global_hooks_are_chained_not_bypassed(repo):
    git, run, global_hooks = repo
    global_hooks()
    out = run().stdout
    assert local(git, "core.hooksPath") == ""
    assert local(git, "ci-policy.chainHooksPath") == ".githooks"
    assert "chained" in out


def test_a_rerun_migrates_a_plain_clone_to_the_chain(repo):
    git, run, global_hooks = repo
    run()
    assert local(git, "core.hooksPath") == ".githooks"
    global_hooks()
    run()
    assert local(git, "core.hooksPath") == ""
    assert local(git, "ci-policy.chainHooksPath") == ".githooks"


@pytest.mark.parametrize("stamp,executable", [
    ("# some other global hook\n", True),
    ("# not ci-policy managed, replaced by hand\n", True),
    (STAMP, False),
])
def test_a_global_hook_that_is_not_a_working_ci_policy_hook_gets_the_plain_path(repo, stamp, executable):
    git, run, global_hooks = repo
    global_hooks(stamp=stamp, executable=executable)
    run()
    assert local(git, "core.hooksPath") == ".githooks"
    assert local(git, "ci-policy.chainHooksPath") == ""
