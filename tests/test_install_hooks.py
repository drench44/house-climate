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

    run.home = home
    run.env = env
    run.work = work
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


def test_a_multi_valued_local_hooks_path_is_fully_cleared_when_chaining(repo):
    git, run, global_hooks = repo
    git("config", "--local", "--add", "core.hooksPath", "old-hooks")
    git("config", "--local", "--add", "core.hooksPath", "other-hooks")
    global_hooks()
    run()
    assert git("config", "--local", "--get-all", "core.hooksPath").stdout.strip() == ""
    assert local(git, "ci-policy.chainHooksPath") == ".githooks"


def test_a_multi_valued_local_hooks_path_becomes_githooks_in_plain_mode(repo):
    git, run, _ = repo
    git("config", "--local", "--add", "core.hooksPath", "old-hooks")
    git("config", "--local", "--add", "core.hooksPath", "other-hooks")
    run()
    assert git("config", "--local", "--get-all", "core.hooksPath").stdout.strip() == ".githooks"


def test_a_hooks_path_that_survives_the_unset_is_refused_not_reported_as_installed(repo):
    git, run, global_hooks = repo
    # an include file the script cannot unset from (--local only edits .git/config)
    inc = run.work / "extra.gitconfig"
    inc.write_text("[core]\n\thooksPath = somewhere-else\n")
    git("config", "--local", "include.path", str(inc))
    global_hooks()
    p = subprocess.run(["bash", "scripts/install-hooks.sh"], cwd=run.work, env=run.env,
                       capture_output=True, text=True)
    assert p.returncode == 1
    assert "could not clear" in p.stderr
    assert "installed:" not in p.stdout


def test_operator_mode_is_set_only_with_the_private_scanner(repo):
    git, run, _ = repo
    run()
    assert local(git, "guard.operator") == ""
    scanner = run.home / "Documents" / "garage" / "privacy" / "scan-repo.sh"
    scanner.parent.mkdir(parents=True)
    scanner.write_text("#!/bin/sh\nexit 0\n")
    scanner.chmod(0o755)
    out = run().stdout
    assert local(git, "guard.operator") == "true"
    assert "operator mode" in out


def test_a_tilde_global_hooks_path_is_found(repo):
    git, run, global_hooks = repo
    global_hooks()
    subprocess.run(["git", "config", "--global", "core.hooksPath", "~/global-hooks"],
                   env=run.env, check=True)
    run()
    assert local(git, "ci-policy.chainHooksPath") == ".githooks"
    assert local(git, "core.hooksPath") == ""


def test_a_rerun_after_the_global_hooks_are_removed_goes_back_to_plain(repo):
    git, run, global_hooks = repo
    global_hooks()
    run()
    assert local(git, "ci-policy.chainHooksPath") == ".githooks"
    subprocess.run(["git", "config", "--global", "--unset", "core.hooksPath"],
                   env=run.env, check=True)
    run()
    assert local(git, "core.hooksPath") == ".githooks"
    assert local(git, "ci-policy.chainHooksPath") == ""


def test_a_crash_while_clearing_still_leaves_the_chain_written(repo):
    """Fault injection: a git that dies on the unset. The chain must already be
    written, so .githooks still runs (never a state with neither key)."""
    git, run, global_hooks = repo
    git("config", "--local", "core.hooksPath", ".githooks")
    global_hooks()
    real_git = shutil.which("git")
    fake = run.home / "fakebin"
    fake.mkdir()
    (fake / "git").write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in *'--unset-all core.hooksPath'*) kill -9 $PPID; exit 137;; esac\n"
        f"exec {real_git} \"$@\"\n")
    (fake / "git").chmod(0o755)
    env = dict(run.env, PATH=f"{fake}:{run.env['PATH']}")
    p = subprocess.run(["bash", "scripts/install-hooks.sh"], cwd=run.work, env=env,
                       capture_output=True, text=True)
    assert p.returncode != 0
    assert local(git, "ci-policy.chainHooksPath") == ".githooks"
    # the old local value is still there too, so .githooks still runs directly
    assert local(git, "core.hooksPath") == ".githooks"

