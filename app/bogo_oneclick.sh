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
#    4) CEO 대시보드(127.0.0.1:8642) 기동 + 헬스체크          ← 본 스크립트가 관리
#    5) 에이전트 봇 4역할(launchd/systemd)                    ← bogo_ctl.sh 재사용
#
#  멱등: 재실행해도 이미 떠 있는 것은 재사용(중복 기동 X). 대시보드가 죽어 있으면
#    좀비 PID 정리 후 재기동. 포트 점유 시 그 PID 가 우리 대시보드면 재사용,
#    아니면 안전 종료 후 우리 것으로 재기동.
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
# 4) CEO 대시보드 (127.0.0.1:8642) — 멱등·포트충돌 안전·헬스체크
# ════════════════════════════════════════════════════════════════════════
dashboard_running_pid() {
  # PID 파일이 가리키는 프로세스가 살아있고 우리 대시보드면 그 PID 출력.
  if [ -f "$DASH_PID_FILE" ]; then
    local p; p="$(cat "$DASH_PID_FILE" 2>/dev/null || true)"
    if [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null; then
      if ps -p "$p" -o command= 2>/dev/null | grep -q "ceo_dashboard.py"; then
        echo "$p"; return 0
      fi
    fi
  fi
  return 1
}

step_dashboard() {
  say "[4/5] CEO 대시보드 기동 ($DASH_HOST:$DASH_PORT, 루프백 전용)..."

  # 4-a. 이미 우리 대시보드가 살아있고 헬스 OK면 재사용(중복 기동 금지).
  if dashboard_running_pid >/dev/null; then
    local rp; rp="$(dashboard_running_pid)"
    if http_ok "http://$DASH_HOST:$DASH_PORT/login"; then
      ok "대시보드 이미 정상 가동 중 (PID $rp) — 재사용."
      return 0
    fi
    warn "대시보드 PID $rp 존재하나 응답 없음 → 좀비로 간주, 정리 후 재기동."
    kill "$rp" 2>/dev/null || true; sleep 1; kill -9 "$rp" 2>/dev/null || true
    rm -f "$DASH_PID_FILE"
  fi

  # 4-b. 포트 점유 검사. 우리 ceo_dashboard 면 재사용, 아니면 안전 종료.
  local holders; holders="$(pids_on_port "$DASH_PORT")"
  if [ -n "$holders" ]; then
    local mine="" foreign=""
    while IFS= read -r pid; do
      [ -z "$pid" ] && continue
      if ps -p "$pid" -o command= 2>/dev/null | grep -q "ceo_dashboard.py"; then
        mine="$pid"
      else
        foreign="$foreign $pid"
      fi
    done <<< "$holders"
    if [ -n "$mine" ] && http_ok "http://$DASH_HOST:$DASH_PORT/login"; then
      ok "포트 $DASH_PORT 를 기존 대시보드(PID $mine)가 사용 중 — 재사용."
      echo "$mine" > "$DASH_PID_FILE"
      return 0
    fi
    if [ -n "${foreign// /}" ]; then
      warn "포트 $DASH_PORT 를 BOGO 외 프로세스($foreign)가 점유 → 안전 종료 시도."
      for p in $foreign; do kill "$p" 2>/dev/null || true; done
      sleep 1
      for p in $foreign; do kill -9 "$p" 2>/dev/null || true; done
    fi
    if [ -n "$mine" ]; then
      kill "$mine" 2>/dev/null || true; sleep 1; kill -9 "$mine" 2>/dev/null || true
    fi
  fi

  # 4-c. 기동(루프백 바인딩은 ceo_dashboard.py 가 HOST=127.0.0.1 로 하드코딩).
  if [ ! -f "$HERE/ceo_dashboard.py" ]; then
    err "ceo_dashboard.py 가 없습니다."
    return 1
  fi
  # .env 로드(봇 토큰·키). nk_config.json 의 bot_token 이 비면 대시보드가 SystemExit.
  if [ -f "$HERE/.env" ]; then set -a; . "$HERE/.env"; set +a; fi
  BOGO_DASHBOARD_PORT="$DASH_PORT" nohup "$VENV_PY" -u "$HERE/ceo_dashboard.py" \
    >"$DASH_OUT" 2>"$DASH_ERR" &
  local newpid=$!
  echo "$newpid" > "$DASH_PID_FILE"

  # 4-d. 헬스체크 폴링.
  local waited=0
  while [ "$waited" -lt "$DASH_HEALTH_TIMEOUT" ]; do
    if ! kill -0 "$newpid" 2>/dev/null; then
      err "대시보드가 기동 직후 종료됨. 원인(흔히 nk_config.json bot_token 누락):"
      tail -n 15 "$DASH_ERR" >&2 || true
      rm -f "$DASH_PID_FILE"
      return 1
    fi
    if http_ok "http://$DASH_HOST:$DASH_PORT/login"; then
      ok "대시보드 정상 (PID $newpid) — http://$DASH_HOST:$DASH_PORT"
      return 0
    fi
    sleep 1; waited=$((waited + 1))
  done
  err "대시보드가 ${DASH_HEALTH_TIMEOUT}s 안에 응답하지 않음. 로그: logs/dashboard.err.log"
  tail -n 15 "$DASH_ERR" >&2 || true
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
do_stop() {
  local all="${1:-}"
  say "대시보드 정지..."
  local stopped=0
  if dashboard_running_pid >/dev/null; then
    local p; p="$(dashboard_running_pid)"
    kill "$p" 2>/dev/null || true; sleep 1; kill -9 "$p" 2>/dev/null || true
    stopped=1
  fi
  # PID 파일과 무관하게 포트를 잡은 우리 대시보드도 청소.
  for p in $(pids_on_port "$DASH_PORT"); do
    if ps -p "$p" -o command= 2>/dev/null | grep -q "ceo_dashboard.py"; then
      kill "$p" 2>/dev/null || true; sleep 1; kill -9 "$p" 2>/dev/null || true; stopped=1
    fi
  done
  rm -f "$DASH_PID_FILE"
  [ "$stopped" = 1 ] && ok "대시보드 정지됨." || say "대시보드는 떠 있지 않았습니다."

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
  step_dashboard || { err "4단계(대시보드) 실패 — 중단(봇은 띄우지 않음)."; return 1; }
  step_bots      || { err "5단계(봇) 실패 — 대시보드는 떠 있음."; return 1; }

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
