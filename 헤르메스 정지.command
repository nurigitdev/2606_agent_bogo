#!/bin/zsh
# ════════════════════════════════════════════════════════════════════════
#  헤르메스 정지 — Finder 더블클릭 1회로 깔끔히 내림
# ════════════════════════════════════════════════════════════════════════
#  기본: CEO 대시보드만 정지(봇 상시가동·통신 백본은 보존 — 데이터/연결 유지).
#  완전 정지가 필요하면 터미널에서:  ./app/hermes_oneclick.sh stop --all
#  Korean path safe.
# ════════════════════════════════════════════════════════════════════════
set -u

SELF_DIR="${0:A:h}"
REPO="$SELF_DIR/app"
ONECLICK="$REPO/hermes_oneclick.sh"

C_INFO=$'\033[0;36m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[헤르메스]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
fail() { printf "%s[오류]%s %s\n"   "$C_ERR"  "$C_RST" "$*"; }

pause_exit() {
  print -r -- ""
  print -r -- "──────────────────────────────────────────────"
  print -r -- "이 창은 Enter 또는 아무 키나 누르면 닫힙니다."
  read -k1 -s 2>/dev/null || true
  exit "${1:-0}"
}

print -r -- ""
say "헤르메스 대시보드 정지"
print -r -- ""

if [[ ! -f "$ONECLICK" ]]; then
  fail "hermes_oneclick.sh 를 찾지 못했습니다: $ONECLICK"
  pause_exit 1
fi
chmod +x "$ONECLICK" 2>/dev/null || true

"$ONECLICK" stop
rc=$?
print -r -- ""
say "봇 상시가동까지 완전히 내리려면:  cd \"$REPO\" && ./hermes_oneclick.sh stop --all"
pause_exit $rc
