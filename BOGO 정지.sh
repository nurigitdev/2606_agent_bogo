#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO 정지 (Linux/범용) — 깔끔히 내림
# ════════════════════════════════════════════════════════════════════════
#  기본: CEO 대시보드만 정지(봇 상시가동·통신 백본은 보존 — 데이터/연결 유지).
#  정지 직전 bogo_oneclick.sh 가 1회 스냅샷 백업을 폴더 안에 남긴다(클린 셧다운).
#  완전 정지가 필요하면:  ./app/bogo_oneclick.sh stop --all
#  macOS 의 'BOGO 정지.command' 와 동일 코어(bogo_oneclick.sh) 공유 — 중복 로직 없음.
# ════════════════════════════════════════════════════════════════════════
set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO="$SELF_DIR/app"
ONECLICK="$REPO/bogo_oneclick.sh"

C_INFO=$'\033[0;36m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[BOGO]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
fail() { printf "%s[오류]%s %s\n"   "$C_ERR"  "$C_RST" "$*"; }

pause_exit() {
  printf '\n──────────────────────────────────────────────\n'
  printf 'Enter 또는 아무 키나 누르면 닫힙니다.\n'
  read -r -n1 -s 2>/dev/null || true
  exit "${1:-0}"
}

printf '\n'; say "BOGO 대시보드 정지 (Linux/범용)"; printf '\n'

if [ ! -f "$ONECLICK" ]; then
  fail "bogo_oneclick.sh 를 찾지 못했습니다: $ONECLICK"
  pause_exit 1
fi
chmod +x "$ONECLICK" 2>/dev/null || true

bash "$ONECLICK" stop
rc=$?
printf '\n'
say "봇 상시가동까지 완전히 내리려면:  cd \"$REPO\" && ./bogo_oneclick.sh stop --all"
pause_exit "$rc"
