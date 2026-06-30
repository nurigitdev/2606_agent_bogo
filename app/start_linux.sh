#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO 리눅스 시작 진입점 — 비개발자용 단일 실행 파일
# ════════════════════════════════════════════════════════════════════════
#  WHY  macOS 는 "BOGO 데이터 백업.command" 같은 더블클릭 진입점이, Windows 는
#    .bat/.ps1 이 있었으나 리눅스용 비개발자 진입점이 없었다. 리눅스 사용자는
#    bogo_ctl.sh setup 을 직접 쳐야 했다(CLI 지식 요구). 이 파일이 그 간극을 메운다.
#
#  WHAT  스크립트 자기 위치를 해석해 app 디렉토리로 이동한 뒤, 최초 설치 겸 상시가동
#    등록 진입점인 bogo_ctl.sh setup 을 실행한다(bootstrap → infra_up → systemd 등록).
#    setup 은 멱등이라 재실행해도 안전하다(이미 설치돼 있으면 재배포·재시작).
#
#  사용:  파일 관리자에서 더블클릭(.desktop 경유) 또는 터미널에서  ./start_linux.sh
set -eu

# ── 자기 위치 = app 루트(한글·공백 경로 안전) ──────────────────────────
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$HERE"

if [ ! -x "$HERE/bogo_ctl.sh" ]; then
  echo "[start_linux] 오류: bogo_ctl.sh 를 찾을 수 없거나 실행권한이 없습니다: $HERE/bogo_ctl.sh" >&2
  echo "[start_linux] 이 파일이 BOGO 의 app 디렉토리 안에 있는지 확인하세요." >&2
  exit 1
fi

echo "[start_linux] BOGO 최초 설치/상시가동 등록을 시작합니다 (bogo_ctl.sh setup)..."
echo "[start_linux] 위치: $HERE"

# 최초 설치 겸 상시가동(systemd --user) 등록. 멱등이라 재실행 안전.
exec "$HERE/bogo_ctl.sh" setup
