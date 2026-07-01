#!/bin/zsh
# ════════════════════════════════════════════════════════════════════════
#  BOGO Start -- one Finder double-click activates all components
# ════════════════════════════════════════════════════════════════════════
#  WHAT  Double-clicking this file makes app/bogo_oneclick.sh bring up 5 layers in order:
#    1) venv/deps check    2) Vault RAG reindex    3) Mattermost communication backbone
#    4) CEO dashboard (127.0.0.1:8642)    5) 4 agent bot roles (launchd)
#  Each step includes health checks, idempotent re-runs, safe port-conflict cleanup, and access URL output.
#  If Docker/Colima is not running, it reports the exact blocker and the single next action -- no false completion.
#  Path-safe: resolves its own location dynamically, so it works under spaced/Unicode paths.
# ════════════════════════════════════════════════════════════════════════
set -u

SELF_DIR="${0:A:h}"
REPO="$SELF_DIR/../app"
ONECLICK="$REPO/bogo_oneclick.sh"

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[BOGO]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
ok()   { printf "%s[OK]%s %s\n"    "$C_OK"   "$C_RST" "$*"; }
fail() { printf "%s[ERROR]%s %s\n" "$C_ERR"  "$C_RST" "$*"; }

pause_exit() {
  print -r -- ""
  print -r -- "──────────────────────────────────────────────"
  print -r -- "Press Enter or any key to close this window."
  read -k1 -s 2>/dev/null || true
  exit "${1:-0}"
}

print -r -- ""
say "Starting the BOGO one-click launcher"
say "Location: $REPO"
print -r -- ""

if [[ ! -f "$ONECLICK" ]]; then
  fail "bogo_oneclick.sh not found: $ONECLICK"
  fail "This .command file must sit in the project root that contains the 'app' folder."
  pause_exit 1
fi
chmod +x "$ONECLICK" 2>/dev/null || true

# Bring up all components (idempotent). Branch on the return code.
"$ONECLICK" start
rc=$?

print -r -- ""
if [[ $rc -eq 0 ]]; then
  ok "All components activated. Open the URL above to test."
elif [[ $rc -eq 2 ]]; then
  fail "Stopped because the Mattermost communication backbone (Docker/Colima) is not ready."
  say "Next action: run  colima start  in a terminal (or start Docker Desktop), then double-click this file again."
else
  fail "A problem occurred during startup. Check the log above."
  say "Manual diagnosis:  cd \"$REPO\" && ./bogo_oneclick.sh status"
fi

pause_exit $rc
