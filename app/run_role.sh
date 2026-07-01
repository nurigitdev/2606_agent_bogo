#!/usr/bin/env bash
# BOGO role launcher (macOS manual / Linux / WSL).
# Usage: run_role.sh <orchestrator|hr|dev|admin|dashboard>
#
# Loads .env (OPENROUTER_API_KEY etc.) then execs the venv python runtime.
# Path-safe: resolves its own dir, so it works under spaces / Unicode folder names.
# Secrets stay in .env (gitignored); this wrapper is safe to commit.
set -eu

ROLE="${1:?Role argument required (orchestrator|hr|dev|admin|dashboard)}"

# Resolve this script's directory (works under any path incl. spaces / Unicode).
SRC="${BASH_SOURCE[0]:-$0}"
HERE="$(cd "$(dirname "$SRC")" && pwd)"

# Layout-aware app root resolution.
#   In-place run    : this script sits AT the app root (.venv/.env beside it)  -> APP=$HERE
#   macOS mirror run : this script is copied to ~/.bogo-bin/run_role.sh while
#                      the app contents are mirrored to ~/.bogo-bin/app/      -> APP=$HERE/app
# Pick whichever directory actually holds the runtime (bogo_runtime.py).
if [ -f "$HERE/bogo_runtime.py" ]; then
  APP="$HERE"
elif [ -f "$HERE/app/bogo_runtime.py" ]; then
  APP="$HERE/app"
else
  echo "[run_role] App root not found (bogo_runtime.py missing): $HERE" >&2
  exit 1
fi

# ── Mirror drift prevention: the sync authority is NOT here (the daemon) but the git post-commit hook ──
# The launchd daemon runs the ASCII mirror (~/.bogo-bin/app). If you edit only the source
# (BOGO_REPO=Desktop) and forget to sync the mirror, the daemon runs stale code (e.g. a missed
# mm_client localhost->127.0.0.1 fix -> Mattermost ::1 Errno 61 connection failure).
#
# * The daemon cannot self-sync (by design): macOS TCC blocks a launchd daemon from "reading file
#   contents" under ~/Desktop (access/stat metadata is allowed, so a -r test gives a false positive,
#   but the actual rsync read syscall fails). So attempting a source->mirror rsync inside the launcher
#   would not only fail every startup, that failure could kill the KeepAlive daemon and cause a
#   restart loop. Therefore we remove the sync responsibility from the launcher entirely.
#
# The single guarantee point for mirror sync = the git post-commit hook (.githooks/post-commit). Since a
# commit runs in the user session context, it passes TCC; the moment code is committed, the hook re-syncs
# the mirror and restarts the daemon with zero downtime (core.hooksPath=.githooks, auto-registered by
# install_service.sh). Auxiliary guard: install_service.sh status's mac_check_mirror_sync surfaces drift.
# Result: the launcher just "runs the mirror as-is", and drift is already removed at commit time.

cd "$APP"

# Load .env if present (KEY=VALUE lines; ignores comments/blanks).
if [ -f "$APP/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$APP/.env"
  set +a
fi

VENV_PY="$APP/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "[run_role] .venv is missing. Run ./bootstrap.sh first." >&2
  exit 1
fi

# 'dashboard' = CEO dashboard web server (ceo_dashboard.py, 127.0.0.1:8642 loopback only).
# Unlike the 4 bot roles it is a long-running HTTP server, owned always-on by launchd (com.bogo.dashboard).
# The loopback binding (HOST=127.0.0.1) is hardcoded in ceo_dashboard.py, so we do not force it here.
if [ "$ROLE" = "dashboard" ]; then
  : "${BOGO_DASHBOARD_PORT:=8642}"   # default 8642 if plist EnvironmentVariables does not provide it.
  export BOGO_DASHBOARD_PORT
  exec "$VENV_PY" -u "$APP/ceo_dashboard.py"
fi
# 'admin' = per-role learning-room admin bot (ceo_admin_runtime.py); everything else = bogo_runtime.py.
if [ "$ROLE" = "admin" ]; then
  exec "$VENV_PY" -u "$APP/ceo_admin_runtime.py"
fi
exec "$VENV_PY" -u "$APP/bogo_runtime.py" "$ROLE"
