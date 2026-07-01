#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO Stop (Linux/generic) -- brings it down cleanly
# ════════════════════════════════════════════════════════════════════════
#  Default: stop only the CEO dashboard (keep bots always-on and the communication backbone -- preserve data/connections).
#  Just before stopping, bogo_oneclick.sh leaves one snapshot backup inside the folder (clean shutdown).
#  For a full stop:  ./app/bogo_oneclick.sh stop --all
#  Shares the same core (bogo_oneclick.sh) as macOS 'BOGO_stop.command' -- no duplicated logic.
# ════════════════════════════════════════════════════════════════════════
set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO="$SELF_DIR/../app"
ONECLICK="$REPO/bogo_oneclick.sh"

C_INFO=$'\033[0;36m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[BOGO]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
fail() { printf "%s[ERROR]%s %s\n" "$C_ERR"  "$C_RST" "$*"; }

pause_exit() {
  printf '\n──────────────────────────────────────────────\n'
  printf 'Press Enter or any key to close.\n'
  read -r -n1 -s 2>/dev/null || true
  exit "${1:-0}"
}

printf '\n'; say "Stopping the BOGO dashboard (Linux/generic)"; printf '\n'

if [ ! -f "$ONECLICK" ]; then
  fail "bogo_oneclick.sh not found: $ONECLICK"
  pause_exit 1
fi
chmod +x "$ONECLICK" 2>/dev/null || true

bash "$ONECLICK" stop
rc=$?
printf '\n'
say "To bring everything down including the always-on bots:  cd \"$REPO\" && ./bogo_oneclick.sh stop --all"
pause_exit "$rc"
