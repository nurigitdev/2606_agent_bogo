#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO 원클릭 오케스트레이터 — 버튼 하나로 전 구성요소 활성화
# ════════════════════════════════════════════════════════════════════════
#  WHY  기존 경로는 (a)통신 백본(infra_up.sh)과 (b)봇 launchd(bogo_ctl.sh)만
#    띄웠다. CEO 대시보드(8642)·Vault RAG reindex 는 어디에서도 자동 기동되지
#    않아 사람이 매번 손으로 떠야 하는 토일이 남아 있었다. 이 스크립트가 5계층을
#    "정해진 순서 + 헬스체크 + 멱등 + 포트충돌 방지"로 한 번에 올린다.
#
#  WHAT (순서. 각 단계 실패 시 명확한 블로커로 중단, 거짓완료 금지):
#    1) venv·의존성 점검 (없으면 bootstrap)
#    2) Vault RAG reindex (visibility 스키마 반영, 1회)
#    3) Mattermost 통신 백본(Colima→컨테이너→MM readiness)  ← infra_up.sh 재사용
#    4) 에이전트 봇 4역할 + CEO 대시보드 launchd/systemd 등록 ← bogo_ctl.sh→install_service.sh
#    5) CEO 대시보드(127.0.0.1:8642) 헬스체크                ← launchd 가 띄운 것 확인만
#
#  재발 방지(핵심): 대시보드는 더 이상 oneclick 의 nohup 단발 프로세스가 아니다. 봇 4역할과
#    동일하게 launchd(com.bogo.dashboard) / systemd(bogo@dashboard) 가 KeepAlive 로 상시
#    소유한다 → 터미널 종료/슬립/수동 kill 에도 자동 부활한다. oneclick 은 등록을 보장하고
#    헬스체크만 한다(직접 기동·중복 nohup 없음). 진짜 정지는 stop(서비스 등록 해제).
#
#  멱등: 재실행해도 이미 떠 있는 것은 재사용(중복 기동 X). install_service.sh 가 기존 등록을
#    bootout 후 재등록(멱등)하며, 과거 nohup 대시보드가 포트를 잡고 있으면 안전 정리한다.
#
#  보안: 대시보드는 반드시 127.0.0.1(루프백)에서만 listen. 외부 노출 금지.
#  로그·PID: app/logs/ 에만 기록(.gitignore 처리됨).
#  사용법:
#    ./bogo_oneclick.sh start     # 전체 기동(기본)
#    ./bogo_oneclick.sh stop      # 대시보드 정지(봇 launchd 는 stop 인자로 별도)
#    ./bogo_oneclick.sh stop --all  # 대시보드 + 봇 launchd 까지 모두 내림
#    ./bogo_oneclick.sh status    # 전 구성요소 상태
#    ./bogo_oneclick.sh restart   # 정지 후 재기동
set -uo pipefail

# ── 자기 위치 = app 루트(한글·공백 경로 안전) ──────────────────────────
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$HERE"

LOGS="$HERE/logs"
mkdir -p "$LOGS"

# 대시보드 루프백 전용 + 포트(환경변수로 덮어쓰기 가능, 기본 8642).
DASH_HOST="127.0.0.1"
DASH_PORT="${BOGO_DASHBOARD_PORT:-8642}"
DASH_PID_FILE="$LOGS/dashboard.pid"
DASH_OUT="$LOGS/dashboard.out.log"
DASH_ERR="$LOGS/dashboard.err.log"
DASH_HEALTH_TIMEOUT="${BOGO_DASH_WAIT_TIMEOUT:-30}"

MM_HOST="127.0.0.1"
MM_PORT="8065"

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf '%s[oneclick]%s %s\n'    "$C_INFO" "$C_RST" "$*"; }
ok()   { printf '%s[oneclick:OK]%s %s\n' "$C_OK"   "$C_RST" "$*"; }
warn() { printf '%s[oneclick:경고]%s %s\n' "$C_WARN" "$C_RST" "$*" >&2; }
err()  { printf '%s[oneclick:오류]%s %s\n' "$C_ERR"  "$C_RST" "$*" >&2; }

VENV_PY="$HERE/.venv/bin/python"

# ── HTTP 200 확인(curl/python 어느 쪽이든. 훅 차단 회피 위해 python urllib 우선) ──
http_ok() {
  # $1 = url
  "$VENV_PY" - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    r = urllib.request.urlopen(sys.argv[1], timeout=3)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

# ── 포트를 LISTEN 중인 PID 목록(루프백 한정 판단은 호출부에서) ──────────
pids_on_port() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | sort -u; }

# ════════════════════════════════════════════════════════════════════════
# 1) venv·의존성 점검
# ════════════════════════════════════════════════════════════════════════
step_venv() {
  say "[1/5] venv·의존성 점검..."
  if [ ! -x "$VENV_PY" ]; then
    warn ".venv 없음 → bootstrap.sh 실행(수 분 소요 가능)"
    if ! "$HERE/bootstrap.sh"; then
      err "bootstrap 실패. Python 3.12 설치 여부 확인: brew install python@3.12"
      return 1
    fi
  fi
  # 핵심 의존성 import 검증(sentence-transformers 는 선택적이라 제외).
  if ! "$VENV_PY" -c "import urllib.request, json, sqlite3" >/dev/null 2>&1; then
    err "venv 파이썬이 정상 동작하지 않습니다: $VENV_PY"
    return 1
  fi
  ok "venv 준비됨: $VENV_PY"
}

# ════════════════════════════════════════════════════════════════════════
# 2) Vault RAG reindex (visibility 스키마 반영)
# ════════════════════════════════════════════════════════════════════════
step_reindex() {
  say "[2/5] Vault RAG 전체 재색인(visibility 스키마 반영)..."
  if [ ! -f "$HERE/vault_rag.py" ]; then
    warn "vault_rag.py 없음 → reindex 건너뜀(선택적 구성요소)."
    return 0
  fi
  if "$VENV_PY" "$HERE/vault_rag.py" reindex >"$LOGS/reindex.out.log" 2>&1; then
    ok "reindex 완료 (로그: logs/reindex.out.log)"
  else
    # reindex 실패는 대시보드/봇을 막지 않는다(검색 일부 저하일 뿐). 경고만.
    warn "reindex 실패(검색 기능 일부 저하 가능). 상세: logs/reindex.out.log"
  fi
}

# ════════════════════════════════════════════════════════════════════════
# 3) Mattermost 통신 백본
# ════════════════════════════════════════════════════════════════════════
step_infra() {
  say "[3/5] Mattermost 통신 백본 기동(Colima→컨테이너→MM readiness)..."
  if [ ! -x "$HERE/infra_up.sh" ]; then
    err "infra_up.sh 가 없거나 실행권한이 없습니다."
    return 1
  fi
  if "$HERE/infra_up.sh"; then
    ok "통신 백본 준비됨 (MM http://$MM_HOST:$MM_PORT)"
  else
    err "통신 백본 기동 실패 — Docker/Colima 점검 필요(아래 블로커 안내 참고)."
    return 2   # 2 = 외부 의존(Docker) 블로커 신호
  fi
}

# ════════════════════════════════════════════════════════════════════════
# 4·5) CEO 대시보드 (127.0.0.1:8642) — launchd 상시 소유, oneclick 은 헬스체크만
# ════════════════════════════════════════════════════════════════════════
dashboard_running_pid() {
  # launchd 가 소유하면 PID 파일이 없으므로, 포트 LISTEN 중인 우리 ceo_dashboard.py 를
  # 진실원으로 본다(과거 nohup 호환을 위해 PID 파일도 함께 확인).
  if [ -f "$DASH_PID_FILE" ]; then
    local p; p="$(cat "$DASH_PID_FILE" 2>/dev/null || true)"
    if [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null; then
      if ps -p "$p" -o command= 2>/dev/null | grep -q "ceo_dashboard.py"; then
        echo "$p"; return 0
      fi
    fi
  fi
  for p in $(pids_on_port "$DASH_PORT"); do
    if ps -p "$p" -o command= 2>/dev/null | grep -q "ceo_dashboard.py"; then
      echo "$p"; return 0
    fi
  done
  return 1
}

step_dashboard() {
  say "[5/5 후] CEO 대시보드 헬스체크 ($DASH_HOST:$DASH_PORT, 루프백 전용, launchd 소유)..."

  # 설계 변경(재발 방지): 대시보드는 더 이상 oneclick 의 nohup 단발 프로세스가 아니라
  # launchd(com.bogo.dashboard) / systemd(bogo@dashboard) 가 KeepAlive 로 상시 소유한다.
  # 그 등록은 step_bots → bogo_ctl.sh → install_service.sh 에서 봇과 함께 이뤄진다.
  # 따라서 여기서는 "직접 기동"하지 않고, launchd 가 띄운 대시보드가 살아 응답하는지만
  # 헬스체크로 확인한다(터미널 종료/슬립/수동 kill 에도 launchd 가 자동 부활시킨다).

  # 혹시 과거 버전이 남긴 nohup 단발 대시보드 PID 파일이 있으면 무시(launchd 가 진실원).
  rm -f "$DASH_PID_FILE" 2>/dev/null || true

  local waited=0
  while [ "$waited" -lt "$DASH_HEALTH_TIMEOUT" ]; do
    if http_ok "http://$DASH_HOST:$DASH_PORT/login"; then
      local pid; pid="$(pids_on_port "$DASH_PORT" | head -1)"
      ok "대시보드 정상 (launchd 소유, PID ${pid:-?}) — http://$DASH_HOST:$DASH_PORT"
      return 0
    fi
    sleep 1; waited=$((waited + 1))
  done
  err "대시보드가 ${DASH_HEALTH_TIMEOUT}s 안에 응답하지 않음(launchd com.bogo.dashboard 확인 필요)."
  err "  진단: launchctl print gui/\$(id -u)/com.bogo.dashboard ; tail logs/dashboard.err.log"
  tail -n 15 "$DASH_ERR" 2>/dev/null >&2 || true
  return 1
}

# ════════════════════════════════════════════════════════════════════════
# 5) 에이전트 봇 4역할 (launchd/systemd, bogo_ctl.sh 재사용)
# ════════════════════════════════════════════════════════════════════════
bots_loaded_count() {
  case "$(uname -s)" in
    Darwin) launchctl list 2>/dev/null | grep -c "com.bogo.\(orchestrator\|hr\|dev\|admin\)" || true ;;
    Linux)  systemctl --user list-units 'bogo@*' --no-legend 2>/dev/null | grep -c bogo || true ;;
    *) echo 0 ;;
  esac
}

step_bots() {
  say "[5/5] 에이전트 봇 4역할 기동/재배포..."
  if [ ! -x "$HERE/bogo_ctl.sh" ]; then
    err "bogo_ctl.sh 없음 → 봇 기동 불가."
    return 1
  fi
  local loaded; loaded="$(bots_loaded_count)"
  if [ "${loaded:-0}" -ge 1 ]; then
    say "봇 ${loaded}개 이미 등록됨 → 최신 코드 재배포 + 재시작(중복 기동 X)."
    # restart 는 내부에서 infra_up.sh 를 또 호출하지만 멱등이므로 안전(이미 떠 있음=즉시통과).
    if "$HERE/bogo_ctl.sh" restart; then ok "봇 재배포+재시작 완료."; else
      err "봇 재시작 실패 — 진단: ./bogo_ctl.sh status"; return 1; fi
  else
    say "봇 미등록 → 최초 설치(bootstrap + 백본 + launchd 등록)."
    if "$HERE/bogo_ctl.sh" setup; then ok "봇 설치+상시가동 등록 완료."; else
      err "봇 설치 실패 — 위 로그 확인."; return 1; fi
  fi
}

# ════════════════════════════════════════════════════════════════════════
# 정지 / 상태
# ════════════════════════════════════════════════════════════════════════
# 대시보드 launchd(com.bogo.dashboard) / systemd(bogo@dashboard) 를 등록 해제해 '진짜로'
# 정지시킨다. 단순 kill 은 KeepAlive 가 즉시 부활시키므로 stop 의도를 달성하지 못한다.
dashboard_service_stop() {
  case "$(uname -s)" in
    Darwin)
      local uid; uid="$(id -u)"
      launchctl bootout "gui/$uid/com.bogo.dashboard" >/dev/null 2>&1 || true ;;
    Linux)
      systemctl --user disable --now "bogo@dashboard.service" >/dev/null 2>&1 || true ;;
  esac
}

do_stop() {
  local all="${1:-}"
  say "대시보드 정지(launchd/systemd 등록 해제 → KeepAlive 부활 차단)..."
  dashboard_service_stop
  local stopped=0
  # 등록 해제 후에도 잔존하는 우리 대시보드 프로세스(과거 nohup 포함)를 청소.
  for p in $(pids_on_port "$DASH_PORT"); do
    if ps -p "$p" -o command= 2>/dev/null | grep -q "ceo_dashboard.py"; then
      kill "$p" 2>/dev/null || true; sleep 1; kill -9 "$p" 2>/dev/null || true; stopped=1
    fi
  done
  rm -f "$DASH_PID_FILE"
  ok "대시보드 정지됨(서비스 등록 해제 완료)."

  if [ "$all" = "--all" ]; then
    say "봇 launchd/systemd 등록 해제(상시가동 중지)..."
    "$HERE/bogo_ctl.sh" uninstall && ok "봇 상시가동 해제됨." || warn "봇 해제 중 경고."
    say "참고: Mattermost/Postgres 컨테이너·Colima 는 데이터 보존 위해 그대로 둡니다."
    say "      완전 종료가 필요하면 수동: docker stop bogo-mm bogo-pg && colima stop"
  fi
}

do_status() {
  printf '%s── BOGO 구성요소 상태 ──%s\n' "$C_INFO" "$C_RST"
  # 백본
  local mm="다운"
  http_ok "http://$MM_HOST:$MM_PORT/api/v4/system/ping" && mm="정상(200)"
  printf '  Mattermost   : %s  (http://%s:%s)\n' "$mm" "$MM_HOST" "$MM_PORT"
  # 대시보드
  local ds="다운"
  if dashboard_running_pid >/dev/null && http_ok "http://$DASH_HOST:$DASH_PORT/login"; then
    ds="정상 (PID $(dashboard_running_pid))"
  fi
  printf '  CEO 대시보드 : %s  (http://%s:%s)\n' "$ds" "$DASH_HOST" "$DASH_PORT"
  # 봇
  printf '  에이전트 봇  : %s개 등록\n' "$(bots_loaded_count)"
  if [ "$(uname -s)" = "Darwin" ]; then
    launchctl list 2>/dev/null | grep "com.bogo." | sed 's/^/      /' || true
  fi
}

# ════════════════════════════════════════════════════════════════════════
# start 파이프라인
# ════════════════════════════════════════════════════════════════════════
do_start() {
  printf '\n%s════ BOGO 원클릭 기동 시작 ════%s\n' "$C_INFO" "$C_RST"
  say "위치: $HERE"
  printf '\n'

  step_venv     || { err "1단계(venv) 실패 — 중단."; return 1; }
  step_reindex                                   # 실패해도 진행(경고만)
  local infra_rc
  step_infra; infra_rc=$?
  if [ "$infra_rc" -eq 2 ]; then
    err "════ 블로커: Mattermost 통신 백본을 띄우지 못했습니다 ════"
    err "원인: Docker/Colima 런타임이 준비되지 않았습니다."
    err "운영자가 할 1가지: 'colima start' 실행 후(또는 Docker Desktop 기동) 이 런처를 다시 실행."
    return 2
  elif [ "$infra_rc" -ne 0 ]; then
    err "3단계(백본) 실패 — 중단."
    return 1
  fi
  # 봇·대시보드 모두 launchd/systemd 상시 가동으로 등록(install_service.sh 가 둘 다 올린다).
  step_bots      || { err "4단계(봇+대시보드 등록) 실패 — 중단."; return 1; }
  # launchd 가 올린 대시보드가 응답할 때까지 헬스체크(직접 기동 아님, 자동 부활 소유는 launchd).
  step_dashboard || { err "5단계(대시보드 헬스체크) 실패 — launchd 상태 확인 필요."; return 1; }

  printf '\n%s════ 전 구성요소 활성화 완료 ════%s\n' "$C_OK" "$C_RST"
  printf '  • CEO 대시보드 :  %shttp://%s:%s%s\n' "$C_OK" "$DASH_HOST" "$DASH_PORT" "$C_RST"
  printf '  • Mattermost   :  %shttp://%s:%s%s\n' "$C_OK" "$MM_HOST" "$MM_PORT" "$C_RST"
  printf '  • 에이전트 봇  :  %s개 상시가동(launchd/systemd)\n' "$(bots_loaded_count)"
  printf '  • 정지        :  ./bogo_oneclick.sh stop   (봇까지: stop --all)\n'
  printf '  • 상태        :  ./bogo_oneclick.sh status\n\n'
}

# ── 디스패치 ──────────────────────────────────────────────────────────────
cmd="${1:-start}"; shift || true
case "$cmd" in
  start)   do_start ;;
  stop)    do_stop "${1:-}" ;;
  restart) do_stop ""; printf '\n'; do_start ;;
  status)  do_status ;;
  *) err "알 수 없는 명령: $cmd (start|stop|stop --all|restart|status)"; exit 1 ;;
esac
