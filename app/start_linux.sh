#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO Linux start entry point -- single executable for non-developers (thin delegating shell)
# ════════════════════════════════════════════════════════════════════════
#  WHY  In the past this file was a 'second branch' that only called 'bogo_ctl.sh setup'
#    (bootstrap -> infra_up -> service install), whereas the project root's 'BOGO_start.sh'
#    calls 'bogo_oneclick.sh start' (full core: venv -> reindex -> infra -> data auto-restore ->
#    bot/dashboard always-on registration -> dashboard health check). When two entry points do
#    different things, the outcome depends on which one the user clicks (in particular step_restore
#    data auto-restore is skipped), breaking the consistency of unattended operation.
#
#  WHAT  This file no longer holds its own core. It delegates straight to the single Linux core,
#    the root 'BOGO_start.sh' (single source of truth = bogo_oneclick.sh start). This way,
#    whichever entry point is clicked runs the exact same full core (behavior branching removed).
#
#  Usage:  double-click in a file manager (via .desktop) or run  ./start_linux.sh  in a terminal
#  Path-safe (spaced/Unicode): resolves its own location dynamically. set -u.
set -u

# ── Self location = the app directory -> its parent is the project root ───
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
START_SH="$ROOT/launchers/BOGO_start.sh"   # the single Linux core entry point (delegates to the full core)

if [ ! -f "$START_SH" ]; then
  echo "[start_linux] ERROR: root entry point not found: $START_SH" >&2
  echo "[start_linux] Make sure this file is inside the BOGO project's app directory." >&2
  exit 1
fi
chmod +x "$START_SH" 2>/dev/null || true

echo "[start_linux] Delegating to the single (full) core -> \"$START_SH\""
# Delegate to the root entry point; bogo_oneclick.sh start (full core) runs inside it.
exec bash "$START_SH"
