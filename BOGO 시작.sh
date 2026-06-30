#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO 시작 (Linux/범용) — 터미널 또는 파일관리자 실행 1회로 전 구성요소 활성화
# ════════════════════════════════════════════════════════════════════════
#  WHY  macOS 는 'BOGO 시작.command'(Finder 더블클릭)를 쓰지만 Linux 파일관리자는
#    .command 를 실행하지 못한다. 이 .sh 가 같은 코어(app/bogo_oneclick.sh)를 호출하는
#    Linux/범용 진입점이다 — macOS 의 .command 와 동일한 코어를 공유(중복 로직 없음).
#    bogo_oneclick.sh 내부의 step_restore 가 폴더 안 백업본을 자동 복원하므로, 사용자가
#    누르는 것은 '폴더 복사 후 이 파일 실행 1번'뿐이다(데이터까지 자동으로 따라온다).
#  사용:  ./BOGO\ 시작.sh   또는 파일관리자에서 더블클릭(실행권한 필요).
#  Korean/space 경로 안전: 자기 위치를 동적 해석.
# ════════════════════════════════════════════════════════════════════════
set -u

# 자기 위치(심볼릭/공백/한글 경로 안전). BASH_SOURCE 기준 절대경로.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SELF_PATH="$SELF_DIR/$(basename "${BASH_SOURCE[0]:-$0}")"
REPO="$SELF_DIR/app"
ONECLICK="$REPO/bogo_oneclick.sh"

# ── 터미널 자동탐지 폴백 ────────────────────────────────────────────────
# WHY  파일관리자 더블클릭은 TTY 없이 실행되는 경우가 많다(.desktop Terminal=true 가
#   안 먹는 환경 포함). 그러면 로그가 안 보여 비개발자가 진행/실패를 판단 못 한다.
#   stdout 이 터미널이 아니고(=GUI 더블클릭 추정) 재귀 가드가 없으면, 설치된 터미널
#   에뮬레이터를 탐지해 그 안에서 자기 자신을 다시 띄운다(로그 가시성 확보).
#   탐지 실패 시 로그파일로 폴백하고 위치를 안내한다. BOGO_IN_TERM 가드로 무한재귀 방지.
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
  # 터미널 에뮬레이터를 못 찾음 → 로그파일로 폴백(거짓 무반응 방지).
  LOGF="$SELF_DIR/BOGO_시작_log.txt"
  printf '[BOGO] 터미널을 찾지 못해 로그를 파일로 남깁니다: %s\n' "$LOGF"
  BOGO_IN_TERM=1 bash "$SELF_PATH" >"$LOGF" 2>&1
  exit $?
fi

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[BOGO]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
ok()   { printf "%s[성공]%s %s\n"   "$C_OK"   "$C_RST" "$*"; }
fail() { printf "%s[오류]%s %s\n"   "$C_ERR"  "$C_RST" "$*"; }

# 파일관리자 더블클릭(=터미널 없이 실행)일 때 창이 즉시 닫히지 않게 잠시 대기.
# 터미널에서 직접 실행한 경우엔 read 가 입력을 기다리되, 비대화형이면 즉시 통과.
pause_exit() {
  printf '\n──────────────────────────────────────────────\n'
  printf 'Enter 또는 아무 키나 누르면 닫힙니다.\n'
  read -r -n1 -s 2>/dev/null || true
  exit "${1:-0}"
}

printf '\n'; say "BOGO 원클릭 런처 시작 (Linux/범용)"; say "위치: $REPO"; printf '\n'

if [ ! -f "$ONECLICK" ]; then
  fail "bogo_oneclick.sh 를 찾지 못했습니다: $ONECLICK"
  fail "이 파일은 'app' 폴더가 있는 프로젝트 루트에 두어야 합니다."
  pause_exit 1
fi
# 실행권한 자기치유: git clone/폴더 복사 시 +x 비트가 사라져도 진입점·코어가 돌게.
chmod +x "$ONECLICK" "$SELF_PATH" 2>/dev/null || true
for s in "$REPO"/*.sh "$REPO"/service/*.sh; do
  [ -f "$s" ] && chmod +x "$s" 2>/dev/null || true
done

bash "$ONECLICK" start
rc=$?

printf '\n'
if [ "$rc" -eq 0 ]; then
  ok "전 구성요소 활성화 완료. 위 URL 로 접속해 테스트하세요."
elif [ "$rc" -eq 2 ]; then
  fail "Docker 가 준비되지 않아 중단됐습니다."
  say "할 일 1가지: Docker 데몬 기동 후 다시 실행."
  say "  - Linux native:  sudo systemctl start docker  (권한 오류면: sudo usermod -aG docker \"\$USER\" 후 재로그인)"
  say "  - colima 사용 시: colima start"
else
  fail "기동 중 문제가 발생했습니다. 위 로그를 확인하세요."
  say "수동 진단:  cd \"$REPO\" && ./bogo_oneclick.sh status"
fi

pause_exit "$rc"
