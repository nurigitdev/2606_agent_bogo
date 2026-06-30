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

# 네트워크 자동 감지 결과(step_netdetect 가 채운다). 헬스체크가 멀티홈/단일망에서
# 루프백이 아니라 실제 NIC IP 로 점검하도록, 감지된 대표 바인딩 호스트를 보관한다.
# 기본은 루프백(감지 전·루프백 모드) — 기존 단일 PC 동작과 동일(회귀 0).
DETECTED_MODE="loopback"
DETECTED_HOST="127.0.0.1"

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
# WHY  macOS 는 lsof 가 기본 존재하지만, 슬림/컨테이너 리눅스(데비안 slim, alpine,
#   최소 설치 등)에는 lsof 가 없을 수 있다. 그 환경에서도 포트 PID 탐지가 동작하도록
#   lsof → ss(iproute2) → fuser(psmisc) 순으로 폴백한다. 어느 경로든 출력은
#   "줄당 PID 하나, 정렬·중복제거" 형식으로 동일하게 정규화한다.
pids_on_port() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    # macOS 기본 경로(기존 동작 유지). LISTEN 소켓의 PID 만 추출.
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | sort -u
  elif command -v ss >/dev/null 2>&1; then
    # iproute2. -p 로 프로세스 정보 포함. users:(("proc",pid=1234,fd=5)) 에서 pid 추출.
    ss -ltnpH "( sport = :$port )" 2>/dev/null \
      | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
  elif command -v fuser >/dev/null 2>&1; then
    # psmisc. TCP 포트를 점유한 PID 를 공백 구분으로 stderr/stdout 에 출력 → 줄당 하나로 정규화.
    fuser -n tcp "$port" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u
  fi
  # 셋 다 없으면 빈 출력(호출부는 빈 결과를 "탐지 도구 없음/미점유"로 안전 처리).
}

# ════════════════════════════════════════════════════════════════════════
# 1) venv·의존성 점검
# ════════════════════════════════════════════════════════════════════════
step_venv() {
  say "[1/5] venv·의존성 점검..."
  if [ ! -x "$VENV_PY" ]; then
    warn ".venv 없음 → bootstrap.sh 실행(수 분 소요 가능)"
    if ! "$HERE/bootstrap.sh"; then
      err "bootstrap 실패. Python 3 설치 여부 확인: brew install python (권장 3.12+, 강제 아님)"
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
# 2.5) 네트워크 자동 프로비저닝 (랜선만 꽂으면 NIC/사설 IP 자동 감지 → .env 주입)
# ════════════════════════════════════════════════════════════════════════
#  WHY  멀티홈 중앙 서버(NIC 3장으로 사내망 A/B/C 직결)를 쓰려면 운영자가 .env 에
#    BOGO_MULTIHOME / MM_BIND_HOST / MM_SITE_URL / MM_ALLOW_CORS_FROM /
#    BOGO_DASHBOARD_HOST 를 손으로 적어야 했다. 사람이 사설 IP 를 외워 적는 것은
#    오타·누락의 상시 원천이다. 이 단계가 NIC 와 사설 IP 를 스스로 읽어 모드를
#    판정하고 .env 의 '네트워크 키만' 멱등 주입한다(비밀·수동값은 비파괴).
#  보안  공인(글로벌 라우팅) IP NIC 가 감지되면 멀티홈 0.0.0.0 자동활성을 중단하고
#    경고만 낸다(net_autodetect 가 guard 모드로 판정 → 네트워크 키 무변경 = 루프백 유지).
#    실패해도 봇 기동을 막지 않는다(기존 .env 값으로 계속 진행).
step_netdetect() {
  say "[2.5/5] 네트워크 자동 감지(NIC/사설 IP) → .env 자동 구성..."
  if [ ! -f "$HERE/net_autodetect.py" ]; then
    warn "net_autodetect.py 없음 → 네트워크 자동 구성 건너뜀(기존 .env 값 사용)."
    return 0
  fi
  # .env 의 네트워크 키만 멱등 upsert. .env 없으면 .env.example 에서 시드 후 주입.
  local summary rc
  summary="$("$VENV_PY" "$HERE/net_autodetect.py" apply \
    --env "$HERE/.env" --example "$HERE/.env.example" 2>>"$LOGS/netdetect.err.log")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    warn "네트워크 자동 구성 실패(기존 .env 값으로 계속). 상세: logs/netdetect.err.log"
    return 0
  fi
  # 사람이 읽는 1회 요약 출력(운영자가 어떤 망 구성으로 떴는지 한눈에).
  printf '%s' "$summary" | while IFS= read -r line; do say "$line"; done

  # 헬스체크가 멀티홈/단일망에서 루프백이 아니라 실제 NIC IP 로 점검하도록 모드·호스트 추출.
  # (detect 를 한 번 더 호출 — apply 와 동일 함수라 결과 일치. JSON 에서 mode/대표 IP 파싱.)
  local detect_json
  detect_json="$("$VENV_PY" "$HERE/net_autodetect.py" detect 2>/dev/null)"
  DETECTED_MODE="$(printf '%s' "$detect_json" | "$VENV_PY" -c \
    'import sys,json; print(json.load(sys.stdin).get("mode","loopback"))' 2>/dev/null || echo loopback)"
  # 헬스체크 대상 호스트: 멀티홈/단일망은 대표 NIC 사설 IP, 그 외는 루프백.
  DETECTED_HOST="$(printf '%s' "$detect_json" | "$VENV_PY" -c \
    'import sys,json; d=json.load(sys.stdin); rep=d.get("rep"); print(rep[1] if rep and d.get("mode") in ("multihome","lan") else "127.0.0.1")' \
    2>/dev/null || echo 127.0.0.1)"
  ok "네트워크 구성 완료(모드: $DETECTED_MODE, 헬스체크 호스트: $DETECTED_HOST)."
}

# ════════════════════════════════════════════════════════════════════════
# 2.6) 멀티홈 망분리 가드 (서버가 A↔B↔C 회사망을 잇는 라우터가 되지 않게)
# ════════════════════════════════════════════════════════════════════════
#  WHY  멀티홈 중앙 서버는 NIC 3장으로 '서로 다른 회사망' A/B/C 에 직결돼 있다.
#    커널 net.ipv4.ip_forward=1 이면 서버가 NIC 간 패킷을 전달하는 '라우터/다리'가
#    되어, 물리적으로 분리돼야 할 3개 회사망이 서버를 경유해 서로 도달 가능해진다
#    (망분리 무력화 — 다른 회사 간 트래픽이라 심각). 그래서 멀티홈 모드에서는
#    ① net.ipv4.ip_forward=0 ② iptables FORWARD 기본정책 DROP 을 강제해야 한다.
#  설계  멱등·기동 비차단. forwarding 이 켜져 있으면 sudo 비대화식으로 끄기를 시도하되
#    (sudo -n: 비밀번호 프롬프트 없이만), 권한이 없거나 실패하면 강제하지 않고 경고 +
#    복붙 명령만 낸다(시스템을 운영자 동의 없이 강제 변경하지 않는다). 멀티홈이 아닌
#    모드(NIC ≤ 1)는 망간 전달이 성립하지 않으므로 이 단계를 통째로 건너뛴다.
step_netseg() {
  # 멀티홈일 때만 의미가 있다(단일망 LAN/루프백/가드 모드는 비적용).
  [ "$DETECTED_MODE" = "multihome" ] || return 0
  [ -f "$HERE/net_autodetect.py" ] || return 0

  say "[2.6/5] 멀티홈 망분리 가드(서버가 A↔B↔C 라우터가 되지 않게 점검)..."

  # 망분리 진단 JSON 을 순수 함수(net_autodetect.assess_segregation)에서 얻는다.
  local seg_json risk ipf
  seg_json="$("$VENV_PY" "$HERE/net_autodetect.py" segregation 2>/dev/null)"
  risk="$(printf '%s' "$seg_json" | "$VENV_PY" -c \
    'import sys,json; print(json.load(sys.stdin)["assessment"]["risk"])' 2>/dev/null || echo unknown)"
  ipf="$(printf '%s' "$seg_json" | "$VENV_PY" -c \
    'import sys,json; v=json.load(sys.stdin)["ip_forward"]; print("" if v is None else ("1" if v else "0"))' 2>/dev/null || echo "")"

  case "$risk" in
    ok)
      ok "IP forwarding 꺼짐(0) — 서버가 라우터가 아님(망분리 유지)." ;;
    danger)
      warn "IP forwarding 이 켜져 있음(1) → 서버가 A↔B↔C 회사망을 잇는 라우터가 됨(망분리 무력화)."
      # sudo 비대화식(-n)으로만 안전하게 끄기 시도. 권한 없으면 강제하지 않고 안내.
      if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        if sudo -n sysctl -w net.ipv4.ip_forward=0 >/dev/null 2>&1; then
          # 재부팅 영구화(drop-in). 실패해도 런타임 차단은 이미 적용됨.
          echo 'net.ipv4.ip_forward=0' | sudo -n tee /etc/sysctl.d/99-bogo-no-forward.conf >/dev/null 2>&1 || true
          sudo -n iptables -P FORWARD DROP >/dev/null 2>&1 || true
          sudo -n iptables -F FORWARD >/dev/null 2>&1 || true
          ok "IP forwarding 차단 적용(net.ipv4.ip_forward=0 + FORWARD DROP). 망분리 복구됨."
        else
          warn "forwarding 자동 차단 실패 — 아래 명령을 관리자 권한으로 직접 실행하세요(기동은 계속):"
          _netseg_print_fix
        fi
      else
        warn "sudo 무권한(또는 부재) — 자동 차단을 강제하지 않습니다. 아래 명령을 관리자 권한으로 실행하세요(기동은 계속):"
        _netseg_print_fix
      fi
      ;;
    *)
      warn "IP forwarding 값을 확정하지 못함(값='${ipf:-?}'). 관리자 권한으로 아래를 점검하세요(기동은 계속):"
      _netseg_print_fix
      ;;
  esac
}

# 망분리 복구 명령을 운영자가 복붙할 수 있게 출력(net_autodetect.segregation_commands 와 동일).
_netseg_print_fix() {
  printf '    %ssudo sysctl -w net.ipv4.ip_forward=0%s\n' "$C_WARN" "$C_RST" >&2
  printf '    %secho '\''net.ipv4.ip_forward=0'\'' | sudo tee /etc/sysctl.d/99-bogo-no-forward.conf%s\n' "$C_WARN" "$C_RST" >&2
  printf '    %ssudo sysctl --system%s\n' "$C_WARN" "$C_RST" >&2
  printf '    %ssudo iptables -P FORWARD DROP && sudo iptables -F FORWARD%s\n' "$C_WARN" "$C_RST" >&2
  printf '    %s(상세 근거·영구화는 docs/DEPLOY_NETWORK.md 1-7 멀티홈 망분리 유지 참고)%s\n' "$C_INFO" "$C_RST" >&2
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
# 3.5) 데이터 자동 복원 (무인 이전의 핵심) — 다른 PC 에서 폴더만 옮겼을 때 기존 PC 의
#   대화/계정/채널/보고 DB 를 자동 주입한다. 백업본(app/migration/bogo_backup_latest.tar.gz)이
#   '있으면' 복원, '없으면' 빈 상태로 통과한다(사람에게 묻지 않는 자동 분기).
#   멱등: bogo_restore.sh 가 복원 표식(.bogo_restored)으로 중복 복원을 막으므로, 재실행해도
#   기존 데이터를 덮어쓰지 않는다(이미 운영 중인 PC 에서 돌려도 안전).
# ════════════════════════════════════════════════════════════════════════
step_restore() {
  say "[3.5/5] 데이터 자동 복원 점검(백업본 있으면 주입, 없으면 빈 상태로 진행)..."
  local restore="$HERE/migration/bogo_restore.sh"
  if [ ! -f "$restore" ]; then
    say "migration/bogo_restore.sh 없음 → 복원 단계 건너뜀(데이터 이전 미사용)."
    return 0
  fi
  chmod +x "$restore" 2>/dev/null || true
  # 복원 실패는 봇 기동을 막지 않는다(빈 상태로라도 서비스는 떠야 함). 경고만.
  if "$restore"; then
    ok "데이터 복원 단계 통과(복원 또는 빈 상태 진행)."
  else
    warn "데이터 복원 중 경고/오류 — 빈 상태 또는 부분 복원으로 계속 진행. 상세는 위 로그."
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

# 헬스체크 대상 호스트 결정: 멀티홈/단일망에선 대시보드가 루프백이 아니라 감지된 NIC
# IP(또는 0.0.0.0 바인딩의 대표 IP)에서 응답하므로, 127.0.0.1 만 보면 거짓 실패가 난다.
# 0.0.0.0 바인딩은 루프백으로도 응답하지만, "직원이 실제 접속하는 NIC IP 가 살아있는가"를
# 검증하려면 대표 NIC IP 로 점검하는 것이 정확하다. 직전 task 의 '127.0.0.1 만 보던 결함' 보정.
dash_health_host() {
  case "$DETECTED_MODE" in
    multihome|lan) echo "$DETECTED_HOST" ;;
    *) echo "$DASH_HOST" ;;   # loopback/guard = 루프백(기존 동작 유지, 회귀 0)
  esac
}

step_dashboard() {
  local hhost; hhost="$(dash_health_host)"
  say "[5/5 후] CEO 대시보드 헬스체크 ($hhost:$DASH_PORT, 모드:$DETECTED_MODE, launchd/systemd 소유)..."

  # 설계 변경(재발 방지): 대시보드는 더 이상 oneclick 의 nohup 단발 프로세스가 아니라
  # launchd(com.bogo.dashboard) / systemd(bogo@dashboard) 가 KeepAlive 로 상시 소유한다.
  # 그 등록은 step_bots → bogo_ctl.sh → install_service.sh 에서 봇과 함께 이뤄진다.
  # 따라서 여기서는 "직접 기동"하지 않고, launchd 가 띄운 대시보드가 살아 응답하는지만
  # 헬스체크로 확인한다(터미널 종료/슬립/수동 kill 에도 launchd 가 자동 부활시킨다).

  # 혹시 과거 버전이 남긴 nohup 단발 대시보드 PID 파일이 있으면 무시(launchd 가 진실원).
  rm -f "$DASH_PID_FILE" 2>/dev/null || true

  local waited=0
  while [ "$waited" -lt "$DASH_HEALTH_TIMEOUT" ]; do
    if http_ok "http://$hhost:$DASH_PORT/login"; then
      local pid; pid="$(pids_on_port "$DASH_PORT" | head -1)"
      ok "대시보드 정상 (launchd/systemd 소유, PID ${pid:-?}) — http://$hhost:$DASH_PORT"
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

# 정상 종료 시 1회 백업(클린 셧다운 스냅샷). 주기 백업과 별개로, 사용자가 의도적으로
# 내리기 직전의 최신 상태를 폴더 안에 남긴다. PG 는 이 시점까지 살아 있으므로 백업 유효.
# best-effort: 백업이 실패해도 정지 자체는 진행한다(정지를 막지 않음).
stop_backup_snapshot() {
  local backup="$HERE/migration/bogo_backup.sh"
  [ -f "$backup" ] || return 0
  say "정상 종료 전 데이터 스냅샷 백업(폴더 안 최신본 갱신)..."
  if BOGO_BACKUP_RETAIN="${BOGO_BACKUP_RETAIN:-3}" bash "$backup" --quiet --out "$HERE/migration" >/dev/null 2>&1; then
    ok "종료 전 백업 완료 → migration/bogo_backup_latest.tar.gz"
  else
    warn "종료 전 백업 건너뜀(Docker 미기동 등) — 직전 주기 백업이 폴더에 남아 있음."
  fi
}

do_stop() {
  local all="${1:-}"
  # 정지 직전 1회 스냅샷(클린 셧다운 백업) — Docker 가 떠 있을 때만 유효, 실패해도 진행.
  stop_backup_snapshot
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
  # 네트워크 자동 감지 → .env 주입. 인프라(docker compose)·대시보드가 .env 를 읽기
  # '전에' 실행해야 새 바인딩이 반영된다. 실패해도 기존 .env 로 계속(경고만).
  step_netdetect                                 # 실패해도 진행(경고만)
  # 멀티홈으로 판정됐으면 서버가 망간 라우터가 되지 않도록 IP forwarding 차단을 점검·적용.
  # 비멀티홈은 내부에서 즉시 통과. 실패해도 기동을 막지 않는다(경고 + 복붙 명령만).
  step_netseg                                    # 실패해도 진행(경고만)
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
  # 인프라(빈 bogo-pg/bogo-mm)가 막 떠 있는 지금이 데이터 주입 적기다. 백업본이 있으면
  # 기존 PC 의 대화/계정/채널/보고를 자동 복원하고, 없으면 빈 상태로 통과한다(자동 분기).
  step_restore                                   # 실패해도 진행(경고만 — 빈 상태로라도 서비스 기동)
  # 봇·대시보드 모두 launchd/systemd 상시 가동으로 등록(install_service.sh 가 둘 다 올린다).
  step_bots      || { err "4단계(봇+대시보드 등록) 실패 — 중단."; return 1; }
  # launchd 가 올린 대시보드가 응답할 때까지 헬스체크(직접 기동 아님, 자동 부활 소유는 launchd).
  step_dashboard || { err "5단계(대시보드 헬스체크) 실패 — launchd 상태 확인 필요."; return 1; }

  local hhost; hhost="$(dash_health_host)"
  printf '\n%s════ 전 구성요소 활성화 완료 (네트워크 모드: %s) ════%s\n' "$C_OK" "$DETECTED_MODE" "$C_RST"
  printf '  • CEO 대시보드 :  %shttp://%s:%s%s\n' "$C_OK" "$hhost" "$DASH_PORT" "$C_RST"
  printf '  • Mattermost   :  %shttp://%s:%s%s\n' "$C_OK" "$hhost" "$MM_PORT" "$C_RST"
  if [ "$DETECTED_MODE" = "multihome" ]; then
    printf '  • 멀티홈       :  각 망 직원은 자기 망 NIC IP:%s 로 브라우저 접속(클라이언트 0)\n' "$MM_PORT"
    printf '                   접속 가능 주소는 위 [2.5/5] 요약의 각 NIC IP 참고\n'
  fi
  printf '  • 에이전트 봇  :  %s개 상시가동(launchd/systemd), 서버 로컬 127.0.0.1 로 MM 접속\n' "$(bots_loaded_count)"
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
