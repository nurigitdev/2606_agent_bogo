#!/bin/zsh
# ════════════════════════════════════════════════════════════════════════
#  헤르메스 시작 — Finder 더블클릭 1회로 3개 역할 + CEO 관리봇 상시 가동
# ════════════════════════════════════════════════════════════════════════
#  WHAT  이 파일을 더블클릭하면:
#    1) 아직 설치 안 됨 → hermes_ctl.sh setup (venv+의존성+config+launchd 등록)
#    2) 이미 상시 가동 중 → 최신 코드 재배포 + 4역할 재시작 (restart)
#    3) 끝나면 현재 상태(PID/종료코드)를 표시
#  설계 근거: 상시 가동은 launchd가 담당하므로, 사람이 매번 터미널을 열어
#    ./hermes_ctl.sh {setup|restart|status} 를 타이핑하던 토일을 1클릭으로 제거.
#    이미 떠 있으면 중복 기동하지 않고 재배포+재시작만 한다(중복 방지).
#  Korean path safe: 자기 위치를 동적 해석하므로 한글/공백 경로에서 동작.
# ════════════════════════════════════════════════════════════════════════
set -u

# ── 0. 자기 위치 기준으로 app/ 디렉터리 고정 (한글·공백 경로 안전) ──
SELF_DIR="${0:A:h}"
REPO="$SELF_DIR/app"
CTL="$REPO/hermes_ctl.sh"

# 색상 (터미널 가독성)
C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[헤르메스]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
ok()   { printf "%s[성공]%s %s\n"   "$C_OK"   "$C_RST" "$*"; }
fail() { printf "%s[오류]%s %s\n"   "$C_ERR"  "$C_RST" "$*"; }

# 더블클릭 시 창이 즉시 닫히지 않도록: 종료 전 항상 키 입력 대기
pause_exit() {
  print -r -- ""
  print -r -- "──────────────────────────────────────────────"
  print -r -- "이 창은 Enter 또는 아무 키나 누르면 닫힙니다."
  # read 1글자(타임아웃 없이) — 결과 확인 시간 보장
  read -k1 -s 2>/dev/null || true
  exit "${1:-0}"
}

print -r -- ""
say "헤르메스 상시 가동 런처 시작"
say "위치: $REPO"
print -r -- ""

# ── 1. 사전 점검: 컨트롤러 존재 확인 ──────────────────────────────────
if [[ ! -f "$CTL" ]]; then
  fail "hermes_ctl.sh 를 찾지 못했습니다: $CTL"
  fail "이 .command 파일은 'app' 폴더가 있는 프로젝트 루트에 두어야 합니다."
  pause_exit 1
fi
chmod +x "$CTL" 2>/dev/null || true

# ── 2. 현재 가동 상태 판별 (이미 떠 있는가?) ──────────────────────────
#   launchctl 목록에 com.hermes.* 가 있으면 = 이미 설치/로드됨.
LOADED_COUNT=$(launchctl list 2>/dev/null | grep -c "com\.hermes\." || true)

if [[ "${LOADED_COUNT:-0}" -ge 1 ]]; then
  # ── 2-a. 이미 상시 가동 중 → 중복 기동 금지, 재배포+재시작만 ──────
  say "이미 상시 가동 중입니다 (등록된 역할 ${LOADED_COUNT}개)."
  say "최신 코드를 재배포하고 4개 역할을 재시작합니다..."
  print -r -- ""
  if "$CTL" restart; then
    print -r -- ""
    ok "재배포 + 재시작 완료."
  else
    print -r -- ""
    fail "재시작 중 문제가 발생했습니다. 위 로그를 확인하세요."
    say "수동 진단:  cd \"$REPO\" && ./hermes_ctl.sh status"
    pause_exit 1
  fi
else
  # ── 2-b. 미설치 → 최초 설치(부트스트랩 + launchd 등록 + 기동) ──────
  say "아직 상시 가동이 등록되지 않았습니다. 최초 설치를 진행합니다."
  say "(venv 생성 + 의존성 설치 + config 준비 + launchd 등록 — 수 분 걸릴 수 있음)"
  print -r -- ""
  if "$CTL" setup; then
    print -r -- ""
    ok "설치 + 상시 가동 등록 완료."
    say "처음이라면 app/.env 와 *_config.json, channels.json 에 실제 토큰/키/채널ID 입력 후"
    say "이 파일을 한 번 더 더블클릭하면 새 설정으로 재시작됩니다."
  else
    print -r -- ""
    fail "설치 중 문제가 발생했습니다. 위 로그를 확인하세요."
    say "흔한 원인: Python 3.12 미설치 →  brew install python@3.12  후 다시 더블클릭"
    pause_exit 1
  fi
fi

# ── 3. 최종 상태 표시 (PID / 마지막 종료코드 / 라벨) ──────────────────
print -r -- ""
say "현재 상태 (1열=PID, 2열=마지막종료코드, 3열=역할):"
STATUS_OUT=$(launchctl list 2>/dev/null | grep "com\.hermes\." || true)
if [[ -n "$STATUS_OUT" ]]; then
  print -r -- "$STATUS_OUT"
  # 살아있는 PID 개수 카운트 (1열이 숫자 = 실행 중)
  ALIVE=$(print -r -- "$STATUS_OUT" | awk '$1 ~ /^[0-9]+$/' | wc -l | tr -d ' ')
  print -r -- ""
  ok "상시 가동 중인 역할: ${ALIVE}개 (orchestrator/hr/dev/admin 4개가 정상)."
  say "로그 보기:  tail -f ~/.hermes-bin/app/logs/orchestrator.out.log"
else
  fail "실행 중인 헤르메스 역할이 없습니다. 위 로그에서 원인을 확인하세요."
  pause_exit 1
fi

pause_exit 0
