#!/usr/bin/env bash
# Install the pre-push privacy guard for this clone. Safe for anyone to
# run: without the operator's private scanner on disk, the hook stays an
# inert no-op.
#
# Two setups run this repo's .githooks:
#   - plain: core.hooksPath=.githooks (the default for any clone).
#   - chained: when the machine has the ci-policy global git hooks
#     (github.com/drench44/ci-policy) installed as its global core.hooksPath,
#     those run first and hand off to .githooks via ci-policy.chainHooksPath.
#     Setting core.hooksPath=.githooks here would bypass them, so in that case
#     this chains instead.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

global_hooks=$(git config --global core.hooksPath || true)
case "$global_hooks" in
  "~"*) global_hooks="$HOME${global_hooks#\~}" ;;
esac
if [ -n "$global_hooks" ] && [ -x "$global_hooks/pre-push" ] \
  && head -n 2 "$global_hooks/pre-push" | tail -n 1 \
    | grep -qE '^# ci-policy managed: global pre-push hook'; then
  # Chain first, then clear the local override: a crash between the two then
  # leaves .githooks still running (directly), never neither.
  git config --local ci-policy.chainHooksPath .githooks
  git config --local --unset-all core.hooksPath 2>/dev/null || true
  # A local core.hooksPath that survived (any value) wins over the global
  # hooks, so the chain would never run: refuse rather than report success.
  if [ -n "$(git config --local --get-all core.hooksPath 2>/dev/null || true)" ]; then
    echo "error: could not clear this clone's local core.hooksPath; the repo hooks would not run. Fix .git/config and rerun." >&2
    exit 1
  fi
  how="chained behind the ci-policy global hooks (ci-policy.chainHooksPath=.githooks)"
else
  git config --local --unset-all ci-policy.chainHooksPath 2>/dev/null || true
  git config --local --replace-all core.hooksPath .githooks
  how="core.hooksPath=.githooks"
fi
# .githooks/pre-commit (the changelog guard) is now active for everyone; the
# pre-push privacy guard only does work in operator mode.
if [ -x "$HOME/Documents/garage/privacy/scan-repo.sh" ]; then
  git config guard.operator true
  echo "installed: $how, guard.operator=true (operator mode: pushes are scanned)"
else
  echo "installed: $how (inert: private scanner not present on this machine)"
fi
