#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO Start (Linux/generic) -- one terminal or file-manager run activates all components
# ════════════════════════════════════════════════════════════════════════
#  WHY  macOS uses 'BOGO_start.command' (Finder double-click), but Linux file managers
#    cannot execute .command files. This .sh is the Linux/generic entry point that calls
#    the same core (app/bogo_oneclick.sh) -- it shares the identical core with the macOS
#    .command (no duplicated logic). Because step_restore inside bogo_oneclick.sh auto-restores
#    the in-folder backup, all the user does is 'copy the folder, then run this file once'
#    (the data follows automatically too).
#  Usage:  ./BOGO_start.sh   or double-click in a file manager (execute permission required).
#  Path-safe (spaced/Unicode): resolves its own location dynamically.
# ════════════════════════════════════════════════════════════════════════
set -u

# This script's own location (symlink/space/Unicode path safe). Absolute path via BASH_SOURCE.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SELF_PATH="$SELF_DIR/$(basename "${BASH_SOURCE[0]:-$0}")"
REPO="$SELF_DIR/../app"
ONECLICK="$REPO/bogo_oneclick.sh"

# ── Terminal auto-detect fallback ───────────────────────────────────────
# WHY  A file-manager double-click often runs without a TTY (including environments where
#   .desktop Terminal=true is ignored). Then no logs are visible and a non-developer cannot
#   tell whether it succeeded or failed. If stdout is not a terminal (=likely a GUI double-click)
#   and no recursion guard is set, detect an installed terminal emulator and relaunch ourselves
#   inside it (to make logs visible). If detection fails, fall back to a log file and report its
#   location. The BOGO_IN_TERM guard prevents infinite recursion.
if [ -z "${BOGO_IN_TERM:-}" ] && [ ! -t 1 ]; then
  export BOGO_IN_TERM=1
  for term in x-terminal-emulator gnome-terminal konsole xfce4-terminal mate-terminal tilix kitty alacritty xterm; do
    if command -v "$term" >/dev/null 2>&1; then
      case "$term" in
        gnome-terminal|tilix) exec "$term" -- bash "$SELF_PATH" ;;
        *)                    exec "$term" -e bash "$SELF_PATH" ;;
      esac
    fi
  done
  # No terminal emulator found -> fall back to a log file (avoids a false "nothing happened").
  LOGF="$SELF_DIR/BOGO_start_log.txt"
  printf '[BOGO] No terminal found; writing logs to a file: %s\n' "$LOGF"
  BOGO_IN_TERM=1 bash "$SELF_PATH" >"$LOGF" 2>&1
  exit $?
fi

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[BOGO]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
ok()   { printf "%s[OK]%s %s\n"    "$C_OK"   "$C_RST" "$*"; }
fail() { printf "%s[ERROR]%s %s\n" "$C_ERR"  "$C_RST" "$*"; }

# When double-clicked in a file manager (=run without a terminal), wait so the window does not
# close immediately. When run directly in a terminal, read waits for input; when non-interactive
# it passes through instantly.
pause_exit() {
  printf '\n──────────────────────────────────────────────\n'
  printf 'Press Enter or any key to close.\n'
  read -r -n1 -s 2>/dev/null || true
  exit "${1:-0}"
}

printf '\n'; say "Starting the BOGO one-click launcher (Linux/generic)"; say "Location: $REPO"; printf '\n'

if [ ! -f "$ONECLICK" ]; then
  fail "bogo_oneclick.sh not found: $ONECLICK"
  fail "This file must sit in the project root that contains the 'app' folder."
  pause_exit 1
fi
# Execute-permission self-heal: restore the entry point/core even if the +x bit is lost on git clone / folder copy.
chmod +x "$ONECLICK" "$SELF_PATH" 2>/dev/null || true
for s in "$REPO"/*.sh "$REPO"/service/*.sh; do
  [ -f "$s" ] && chmod +x "$s" 2>/dev/null || true
done

bash "$ONECLICK" start
rc=$?

printf '\n'
if [ "$rc" -eq 0 ]; then
  ok "All components activated. Open the URL above to test."
elif [ "$rc" -eq 2 ]; then
  fail "Stopped because Docker is not ready."
  say "Next action: start the Docker daemon, then run again."
  say "  - Linux native:  sudo systemctl start docker  (on permission errors: sudo usermod -aG docker \"\$USER\" then re-login)"
  say "  - when using colima: colima start"
else
  fail "A problem occurred during startup. Check the log above."
  say "Manual diagnosis:  cd \"$REPO\" && ./bogo_oneclick.sh status"
fi

pause_exit "$rc"
