#!/usr/bin/env bash
# Hermes single entry point (macOS / Linux).
# Wraps bootstrap + service install/uninstall/restart/status + manual run.
#
# Usage:
#   ./hermes_ctl.sh setup                 # bootstrap (venv + deps + config) then install service
#   ./hermes_ctl.sh bootstrap             # venv + deps + config copy only
#   ./hermes_ctl.sh install               # register always-on service (launchd/systemd)
#   ./hermes_ctl.sh uninstall             # remove service registration
#   ./hermes_ctl.sh restart               # redeploy + restart all roles
#   ./hermes_ctl.sh status                # service state
#   ./hermes_ctl.sh run <role>            # foreground run one role (orchestrator|hr|dev|admin)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd="${1:-help}"; shift || true

case "$cmd" in
  setup)
    "$HERE/bootstrap.sh"
    # 봇 기동 전에 통신 백본(Colima→컨테이너→MM readiness)을 보장한다(멱등).
    "$HERE/infra_up.sh"
    "$HERE/service/install_service.sh" install
    ;;
  bootstrap)  "$HERE/bootstrap.sh" ;;
  install)    "$HERE/infra_up.sh"; "$HERE/service/install_service.sh" install ;;
  uninstall)  "$HERE/service/install_service.sh" uninstall ;;
  restart)    "$HERE/infra_up.sh"; "$HERE/service/install_service.sh" restart ;;
  infra)      "$HERE/infra_up.sh" ;;
  status)     "$HERE/service/install_service.sh" status ;;
  run)        "$HERE/run_role.sh" "${1:?역할 인자 필요 (orchestrator|hr|dev|admin)}" ;;
  help|*)
    cat <<'EOF'
Hermes 컨트롤러 (mac/linux)
  ./hermes_ctl.sh setup        부트스트랩(venv+의존성+config) 후 상시 가동 등록 (권장: 새 PC 첫 실행)
  ./hermes_ctl.sh bootstrap    venv/의존성/config 준비만
  ./hermes_ctl.sh install      상시 가동 서비스 등록 (launchd/systemd)
  ./hermes_ctl.sh uninstall    서비스 등록 해제
  ./hermes_ctl.sh restart      통신 백본 보장 + 코드 재배포 + 전체 역할 재시작
  ./hermes_ctl.sh infra        통신 백본만 보장(Colima→컨테이너→MM readiness)
  ./hermes_ctl.sh status       서비스 상태 확인
  ./hermes_ctl.sh run <role>   단일 역할 포그라운드 실행 (orchestrator|hr|dev|admin)
EOF
    ;;
esac
