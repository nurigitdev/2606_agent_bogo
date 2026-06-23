#!/bin/zsh
# Hermes role launcher for launchd.
# Runs from ~/.hermes-bin/app — a non-TCC-protected ASCII path. launchd-spawned
# agents are denied read access to ~/Desktop file contents (macOS privacy/TCC), so the
# operational runtime lives here, outside Desktop. The Desktop git repo is the source of
# truth; sync_app.sh mirrors it here after edits.
set -eu
ROLE="${1:?role argument required (orchestrator|hr|dev)}"
APP="${HOME:-/Users/haris}/.hermes-bin/app"
cd "$APP"
if [[ -f "$APP/.env" ]]; then
  set -a
  source "$APP/.env"
  set +a
fi
exec "$APP/.venv/bin/python" -u "$APP/hermes_runtime.py" "$ROLE"
