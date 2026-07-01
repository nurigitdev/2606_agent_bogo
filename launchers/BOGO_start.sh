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
ROOT="$(cd "$SELF_DIR/.." && pwd)"
REPO="$ROOT/app"
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

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[BOGO]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
ok()   { printf "%s[OK]%s %s\n"    "$C_OK"   "$C_RST" "$*"; }
fail() { printf "%s[ERROR]%s %s\n" "$C_ERR"  "$C_RST" "$*"; }
warn() { printf "%s[WARN]%s %s\n"  "$C_WARN" "$C_RST" "$*"; }

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

try_self_update() {
  [ "${BOGO_SELF_UPDATE_DONE:-0}" = "1" ] && return 0
  [ "${BOGO_SKIP_SELF_UPDATE:-0}" = "1" ] && { say "Self-update skipped by BOGO_SKIP_SELF_UPDATE=1."; return 0; }
  command -v git >/dev/null 2>&1 || return 0

  if [ "$(git -C "$ROOT" rev-parse --is-inside-work-tree 2>/dev/null || echo false)" != "true" ]; then
    return 0
  fi

  local upstream dirty before after before_short after_short
  if ! upstream="$(git -C "$ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
    return 0
  fi

  dirty="$(git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null || true)"
  if [ -n "$dirty" ]; then
    warn "Tracked local changes exist — skipping self-update and continuing with current files."
    return 0
  fi

  before="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
  say "Checking for launcher updates from $upstream..."
  if env GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes}" git -C "$ROOT" fetch --prune; then
    if env GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes}" git -C "$ROOT" merge --ff-only "$upstream"; then
      after="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
      if [ -n "$before" ] && [ -n "$after" ] && [ "$before" != "$after" ]; then
        before_short="${before:0:7}"
        after_short="${after:0:7}"
        ok "Updated launcher code ($before_short -> $after_short); restarting with the latest version."
        export BOGO_SELF_UPDATE_DONE=1
        exec bash "$SELF_PATH" "$@"
      fi
      ok "Launcher code already up to date."
    else
      warn "Self-update skipped because fast-forward from $upstream was not possible; continuing with current files."
    fi
  else
    warn "Self-update skipped because git fetch could not complete; continuing with current files."
  fi
}

try_self_update "$@"

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
elif [ "$rc" -eq 3 ]; then
  fail "Stopped because Docker Compose is not available."
  say "Next action: install/enable Docker Compose, then run again."
  say "  - Linux native: install the docker compose plugin package for your distro."
  say "  - Docker Desktop/Colima: make sure 'docker compose version' works."
elif [ "$rc" -eq 4 ]; then
  fail "Stopped because Linux user services are not reachable."
  say "Next action: run from a normal logged-in user terminal, then run again."
  say "  - Diagnostic: systemctl --user status"
  say "  - For boot/logout persistence: sudo loginctl enable-linger \"\$USER\""
else
  fail "A problem occurred during startup. Check the log above."
  say "Manual diagnosis:  cd \"$REPO\" && ./bogo_oneclick.sh status"
fi

pause_exit "$rc"
