#!/usr/bin/env bash
# BOGO single entry point (macOS / Linux).
# Wraps bootstrap + service install/uninstall/restart/status + manual run.
#
# Usage:
#   ./bogo_ctl.sh setup                 # bootstrap (venv + deps + config) then install service
#   ./bogo_ctl.sh bootstrap             # venv + deps + config copy only
#   ./bogo_ctl.sh install               # register always-on service (launchd/systemd)
#   ./bogo_ctl.sh uninstall             # remove service registration
#   ./bogo_ctl.sh restart               # redeploy + restart all roles
#   ./bogo_ctl.sh status                # service state
#   ./bogo_ctl.sh run <role>            # foreground run one role (orchestrator|hr|dev|admin)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd="${1:-help}"; shift || true

case "$cmd" in
  setup)
    "$HERE/bootstrap.sh"
    # Ensure the communication backbone (Colima -> containers -> MM readiness) before starting bots (idempotent).
    "$HERE/infra_up.sh"
    "$HERE/service/install_service.sh" install
    ;;
  bootstrap)  "$HERE/bootstrap.sh" ;;
  install)    "$HERE/infra_up.sh"; "$HERE/service/install_service.sh" install ;;
  uninstall)  "$HERE/service/install_service.sh" uninstall ;;
  restart)    "$HERE/infra_up.sh"; "$HERE/service/install_service.sh" restart ;;
  infra)      "$HERE/infra_up.sh" ;;
  status)     "$HERE/service/install_service.sh" status ;;
  run)        "$HERE/run_role.sh" "${1:?Role argument required (orchestrator|hr|dev|admin)}" ;;
  help|*)
    cat <<'EOF'
BOGO controller (mac/linux)
  ./bogo_ctl.sh setup        Bootstrap (venv+deps+config) then register always-on (recommended: first run on a new PC)
  ./bogo_ctl.sh bootstrap    Prepare venv/deps/config only
  ./bogo_ctl.sh install      Register always-on service (launchd/systemd)
  ./bogo_ctl.sh uninstall    Unregister the service
  ./bogo_ctl.sh restart      Ensure backbone + redeploy code + restart all roles
  ./bogo_ctl.sh infra        Ensure only the communication backbone (Colima -> containers -> MM readiness)
  ./bogo_ctl.sh status       Check service status
  ./bogo_ctl.sh run <role>   Foreground run a single role (orchestrator|hr|dev|admin)
EOF
    ;;
esac
