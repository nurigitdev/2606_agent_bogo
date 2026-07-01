#!/bin/zsh
# ════════════════════════════════════════════════════════════════════════
#  BOGO Stop -- one Finder double-click brings it down cleanly
# ════════════════════════════════════════════════════════════════════════
#  Default: stop only the CEO dashboard (keep bots always-on and the communication backbone -- preserve data/connections).
#  For a full stop, run in a terminal:  ./app/bogo_oneclick.sh stop --all
#  Path-safe.
# ════════════════════════════════════════════════════════════════════════
set -u

SELF_DIR="${0:A:h}"
REPO="$SELF_DIR/../app"
ONECLICK="$REPO/bogo_oneclick.sh"

C_INFO=$'\033[0;36m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[BOGO]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
fail() { printf "%s[ERROR]%s %s\n" "$C_ERR"  "$C_RST" "$*"; }

pause_exit() {
  print -r -- ""
  print -r -- "──────────────────────────────────────────────"
  print -r -- "Press Enter or any key to close this window."
  read -k1 -s 2>/dev/null || true
  exit "${1:-0}"
}

print -r -- ""
say "Stopping the BOGO dashboard"
print -r -- ""

if [[ ! -f "$ONECLICK" ]]; then
  fail "bogo_oneclick.sh not found: $ONECLICK"
  pause_exit 1
fi
chmod +x "$ONECLICK" 2>/dev/null || true

"$ONECLICK" stop
rc=$?
print -r -- ""
say "To bring everything down including the always-on bots:  cd \"$REPO\" && ./bogo_oneclick.sh stop --all"
pause_exit $rc
