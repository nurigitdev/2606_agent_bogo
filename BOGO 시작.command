#!/bin/zsh
# ════════════════════════════════════════════════════════════════════════
#  BOGO 시작 — Finder 더블클릭 1회로 전 구성요소 활성화
# ════════════════════════════════════════════════════════════════════════
#  WHAT  이 파일을 더블클릭하면 app/bogo_oneclick.sh 가 5계층을 순서대로 올린다:
#    1) venv·의존성 점검    2) Vault RAG reindex    3) Mattermost 통신 백본
#    4) CEO 대시보드(127.0.0.1:8642)    5) 에이전트 봇 4역할(launchd)
#  각 단계 헬스체크·멱등 재실행·포트충돌 안전정리·접속 URL 출력 포함.
#  Docker/Colima 가 안 떠 있으면 거짓완료 없이 정확한 블로커와 할 일 1가지를 안내.
#  Korean path safe: 자기 위치를 동적 해석하므로 한글/공백 경로에서 동작.
# ════════════════════════════════════════════════════════════════════════
set -u

SELF_DIR="${0:A:h}"
REPO="$SELF_DIR/app"
ONECLICK="$REPO/bogo_oneclick.sh"

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[BOGO]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
ok()   { printf "%s[성공]%s %s\n"   "$C_OK"   "$C_RST" "$*"; }
fail() { printf "%s[오류]%s %s\n"   "$C_ERR"  "$C_RST" "$*"; }

pause_exit() {
  print -r -- ""
  print -r -- "──────────────────────────────────────────────"
  print -r -- "이 창은 Enter 또는 아무 키나 누르면 닫힙니다."
  read -k1 -s 2>/dev/null || true
  exit "${1:-0}"
}

print -r -- ""
say "BOGO 원클릭 런처 시작"
say "위치: $REPO"
print -r -- ""

if [[ ! -f "$ONECLICK" ]]; then
  fail "bogo_oneclick.sh 를 찾지 못했습니다: $ONECLICK"
  fail "이 .command 파일은 'app' 폴더가 있는 프로젝트 루트에 두어야 합니다."
  pause_exit 1
fi
chmod +x "$ONECLICK" 2>/dev/null || true

# 전 구성요소 기동(멱등). 반환코드로 결과 분기.
"$ONECLICK" start
rc=$?

print -r -- ""
if [[ $rc -eq 0 ]]; then
  ok "전 구성요소 활성화 완료. 위 URL 로 접속해 테스트하세요."
elif [[ $rc -eq 2 ]]; then
  fail "Mattermost 통신 백본(Docker/Colima)이 준비되지 않아 중단됐습니다."
  say "할 일 1가지:  터미널에서  colima start  실행(또는 Docker Desktop 기동) 후 이 파일을 다시 더블클릭."
else
  fail "기동 중 문제가 발생했습니다. 위 로그를 확인하세요."
  say "수동 진단:  cd \"$REPO\" && ./bogo_oneclick.sh status"
fi

pause_exit $rc
