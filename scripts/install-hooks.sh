#!/usr/bin/env bash
# Install the pre-push privacy guard for this clone. Safe for anyone to
# run: without the operator's private scanner on disk, the hook stays an
# inert no-op.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath .githooks
if [ -x "$HOME/Documents/garage/privacy/scan-repo.sh" ]; then
  git config guard.operator true
  echo "installed: core.hooksPath=.githooks, guard.operator=true (operator mode — pushes are scanned)"
else
  echo "installed: core.hooksPath=.githooks (inert — private scanner not present on this machine)"
fi
