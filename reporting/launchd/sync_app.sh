#!/bin/zsh
# Mirror the Desktop git repo (source of truth) into the operational runtime copy
# at ~/.hermes-bin/app, which launchd actually runs.
#
# WHY a copy exists: macOS TCC denies launchd-spawned background agents read access
# to ~/Desktop file *contents* (open/read is "Operation not permitted", even though
# directory listing works). The Hermes runtime must read .env / *_config.json /
# channels.json / agents/*.md / its venv at runtime, so the live copy lives outside
# Desktop. Edit code/config in the Desktop repo, then run this script to deploy.
#
# Usage: ./sync_app.sh            (sync only)
#        ./sync_app.sh --restart  (sync then reload all three launchd agents)
set -eu

SRC="${0:A:h:h}"                       # .../reporting  (this file is in reporting/launchd/)
APP="${HOME}/.hermes-bin/app"
mkdir -p "$APP/logs"

rsync -a \
  --exclude '__pycache__/' \
  --exclude '.ruff_cache/' \
  --exclude '*.bak' \
  --exclude 'logs/' \
  "$SRC"/ "$APP"/
echo "synced: $SRC -> $APP"

if [[ "${1:-}" == "--restart" ]]; then
  UID_NUM="$(id -u)"
  for r in orchestrator hr dev; do
    launchctl kickstart -k "gui/${UID_NUM}/com.hermes.${r}" && echo "restarted com.hermes.${r}"
  done
fi
