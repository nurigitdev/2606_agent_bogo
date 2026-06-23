#!/bin/zsh
# Hermes role launcher for launchd.
# Usage: run_role.sh <orchestrator|hr|dev>
# Loads .env (OPENROUTER_API_KEY etc.) then execs the venv python runtime.
# Secrets stay in .env (gitignored); this wrapper is safe to commit.
set -eu

ROLE="${1:?role argument required (orchestrator|hr|dev)}"
HERE="${0:A:h}"
cd "$HERE"

# Load .env if present (KEY=VALUE lines; ignores comments/blanks).
if [[ -f "$HERE/.env" ]]; then
  set -a
  source "$HERE/.env"
  set +a
fi

exec "$HERE/.venv/bin/python" -u "$HERE/hermes_runtime.py" "$ROLE"
