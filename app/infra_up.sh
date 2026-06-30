#!/usr/bin/env bash
# BOGO infra bring-up — 봇 기동 전에 통신 백본(Colima VM + Mattermost + Postgres)이
# 반드시 살아있도록 보장하는 멱등 부트 의존성 체인.
#
# WHY: 봇은 ws://127.0.0.1:8065 로 Mattermost 에 붙는다(localhost 는 ::1 로 먼저
#   풀려 colima IPv4-only 포워드에서 refused 가 나므로 IPv4 강제). Colima(도커 런타임 VM)가 꺼져
#   있으면 컨테이너가 Exited 가 되고, MM 이 없으면 봇이 연결 실패로 죽는다. 기존 시작
#   경로(bogo_ctl setup/restart, BOGO 시작.command)는 이 백본 기동을 보장하지
#   않고 곧장 봇만 띄웠다 → 근본 원인. 이 스크립트가 그 공백을 메운다.
#
# WHAT (순서·전부 멱등):
#   1) Colima 가 running 이 아니면 colima start. stale lock 이면 stop --force 후 재기동.
#   2) bogo-pg, bogo-mm 컨테이너가 Up 이 아니면 docker start (데이터 보존).
#      restart 정책을 unless-stopped 로 끌어올려 Colima 재시작 시 자동 부활시킨다.
#   3) MM /api/v4/system/ping 이 200 을 줄 때까지 폴링 대기(타임아웃·명확한 실패 메시지).
#
# 이미 떠 있으면 각 단계를 건너뛴다(중복 기동 없음). 어느 단계든 회복 불가하면
# 0 이 아닌 코드로 종료하여 상위(bogo_ctl/install_service)가 봇을 띄우지 않게 한다.
#
# Korean/space 경로 안전: 컨테이너·VM 은 경로 무관, docker/colima CLI 만 호출.
set -euo pipefail

say()  { printf '\033[0;36m[infra]\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m[infra:OK]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[infra:경고]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[0;31m[infra:오류]\033[0m %s\n' "$*" >&2; }

# 컨테이너 이름(고정). docker-compose.yml 이 이 이름으로 영속 컨테이너를 생성하며,
# 이 스크립트는 같은 이름으로 inspect/start 한다(이름 = 두 파일 사이의 계약).
PG_NAME="bogo-pg"
MM_NAME="bogo-mm"

# 최초 컨테이너 생성 정의(이식성). 다른 PC 처럼 컨테이너가 아예 없는 상태에서
# 이 compose 로 bogo-pg/bogo-mm 를 한 번 만든다. 이 스크립트와 같은 디렉터리에 둔다.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${BOGO_COMPOSE_FILE:-$HERE/docker-compose.yml}"

# MM healthy 대기 한도(초). MM 콜드 부팅은 수십 초 걸릴 수 있다.
MM_WAIT_TIMEOUT="${BOGO_MM_WAIT_TIMEOUT:-180}"
# Colima 부팅 대기 한도(초).
COLIMA_WAIT_TIMEOUT="${BOGO_COLIMA_WAIT_TIMEOUT:-180}"

need() {
  command -v "$1" >/dev/null 2>&1 || { err "$1 명령을 찾지 못했습니다. (brew install $1)"; exit 1; }
}

# Docker 데몬 접근성 점검(OS 인지). Linux native 에서 가장 흔한 실패는 (a)도커 데몬 미기동,
# (b)현재 사용자가 docker 그룹에 없어 소켓 권한 거부 — 둘 다 명확한 1줄 안내로 잡는다.
# macOS(Colima)는 ensure_colima 가 VM 을 띄우므로 여기서는 데몬 ping 만 한다.
ensure_docker_reachable() {
  docker info >/dev/null 2>&1 && return 0
  if [ "$(uname -s)" = "Linux" ]; then
    # 권한 문제인지(소켓은 있으나 거부) 데몬 자체가 죽었는지 구분해 안내.
    if [ -S /var/run/docker.sock ] && ! docker info >/dev/null 2>&1; then
      err "Docker 소켓 접근 거부 — 현재 사용자가 docker 그룹이 아닐 수 있습니다."
      err "할 일 1가지:  sudo usermod -aG docker \"\$USER\"  실행 후 '재로그인'(또는 newgrp docker)."
    else
      err "Docker 데몬에 연결할 수 없습니다(미기동 추정)."
      err "할 일 1가지:  sudo systemctl start docker   (부팅 자동시작: sudo systemctl enable docker)"
    fi
    exit 1
  fi
  # macOS: Colima 가 떠 있어야 도달 가능. ensure_colima 이후에도 실패면 그쪽 안내를 따른다.
  err "Docker 데몬에 연결할 수 없습니다. 'colima start' 후 다시 실행하세요."
  exit 1
}

# ── 1. Colima 보장 ────────────────────────────────────────────────────────
ensure_colima() {
  # Colima 는 macOS 의 도커 런타임 VM. Linux 네이티브 docker 환경엔 colima 가 없고
  # 필요도 없으므로(데몬은 systemd 가 관리), 미발견 시 이 단계를 건너뛴다.
  if ! command -v colima >/dev/null 2>&1; then
    say "colima 미발견(Linux 네이티브 docker 추정) → Colima 단계 건너뜀."
    return 0
  fi
  # colima status 는 running 이면 0, 아니면 0 이 아님(메시지는 stderr).
  if colima status >/dev/null 2>&1; then
    ok "Colima 이미 running — 건너뜀."
    return 0
  fi

  say "Colima 미기동 → 시작 시도..."
  if colima start >/dev/null 2>&1; then
    : # started
  else
    # 흔한 실패: 비정상 종료 후 남은 stale lock/소켓 → 강제 정지 후 재기동.
    warn "colima start 실패 → stale lock 가능성. stop --force 후 재시도."
    colima stop --force >/dev/null 2>&1 || true
    sleep 2
    if ! colima start >/dev/null 2>&1; then
      err "Colima 기동 실패. 수동 확인: colima status / colima start"
      exit 1
    fi
  fi

  # running 확정까지 폴링(VM 부팅 시간 흡수).
  local waited=0
  while ! colima status >/dev/null 2>&1; do
    sleep 2; waited=$((waited + 2))
    if [ "$waited" -ge "$COLIMA_WAIT_TIMEOUT" ]; then
      err "Colima 가 ${COLIMA_WAIT_TIMEOUT}s 안에 running 되지 않았습니다."
      exit 1
    fi
  done
  ok "Colima running (${waited}s 소요)."
}

# ── 2. 컨테이너 보장 ──────────────────────────────────────────────────────
container_state() { docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo "absent"; }

# docker compose 호출자(신형 'docker compose' / 구형 'docker-compose' 자동 선택).
# --project-directory 를 compose 파일이 있는 디렉토리(=app)로 못박는다. 이렇게 하면
# 호출 cwd 나 compose 구현(v2/구형 docker-compose)과 무관하게 '항상' app/.env 가
# 자동 로드된다 → net_autodetect 가 주입한 멀티홈 네트워크 키(${MM_BIND_HOST} 등)가
# 어떤 진입점·cwd 에서 호출해도 compose 변수 보간에 끊김 없이 전달된다(폴백 누락 차단).
COMPOSE_DIR="$(cd "$(dirname "$COMPOSE_FILE")" && pwd)"
compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_FILE" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_FILE" "$@"
  else
    return 127
  fi
}

# 컨테이너가 하나라도 없으면 compose 로 둘 다 최초 생성한다(멱등: 이미 있으면 무변경).
# 다른 PC 이식성의 핵심 — 이 단계가 없으면 'absent' 에서 멈춰 MM 자체가 못 뜬다.
COMPOSE_CREATED=0
ensure_created_via_compose() {
  [ "$COMPOSE_CREATED" = "1" ] && return 0   # 한 번만 시도
  COMPOSE_CREATED=1
  if [ ! -f "$COMPOSE_FILE" ]; then
    err "컨테이너가 없고 compose 정의도 없습니다: $COMPOSE_FILE"
    err "(docker-compose.yml 이 저장소에 포함돼야 다른 PC 에서 최초 생성이 가능합니다.)"
    exit 1
  fi
  say "컨테이너 부재 감지 → docker-compose.yml 로 최초 생성/기동 (bogo-pg, bogo-mm)..."
  if ! compose up -d; then
    local rc=$?
    if [ "$rc" = "127" ]; then
      err "docker compose 를 찾지 못했습니다. Docker Desktop/Compose 플러그인 설치 필요."
    else
      err "compose up 실패(rc=$rc). 진단: docker compose -f \"$COMPOSE_FILE\" logs"
    fi
    exit 1
  fi
  ok "compose 로 컨테이너 생성/기동 완료."
}

ensure_container() {
  local name="$1"
  local st; st="$(container_state "$name")"
  case "$st" in
    running)
      ok "$name 이미 running — 건너뜀." ;;
    absent)
      # 최초 생성 경로: compose 로 만든 뒤 상태를 재평가한다.
      ensure_created_via_compose
      st="$(container_state "$name")"
      if [ "$st" = "absent" ]; then
        err "$name 가 compose 생성 후에도 부재. 진단: docker compose -f \"$COMPOSE_FILE\" ps"
        exit 1
      fi
      if [ "$st" != "running" ]; then
        say "$name 상태=$st → docker start"
        docker start "$name" >/dev/null
      fi
      ok "$name 준비(컨테이너 생성 경로)." ;;
    *)
      say "$name 상태=$st → docker start"
      docker start "$name" >/dev/null
      ok "$name 기동" ;;
  esac
  # Colima/머신 재부팅 시 컨테이너가 스스로 부활하도록 restart 정책을 끌어올린다(멱등).
  local pol; pol="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$name" 2>/dev/null || echo '')"
  if [ "$pol" != "unless-stopped" ] && [ "$pol" != "always" ]; then
    docker update --restart unless-stopped "$name" >/dev/null 2>&1 \
      && say "$name restart 정책 → unless-stopped (재부팅 자동 부활)" \
      || warn "$name restart 정책 갱신 실패(무시 가능)."
  fi
}

# MM 컨테이너의 DB DataSource 가 가리키는 호스트명(레거시 hermes 리브랜딩 잔재). MM 환경변수
# MM_SQLSETTINGS_DATASOURCE 는 'hermes-pg' 를 참조하는데 실제 PG 컨테이너 이름은 'bogo-pg'
# 라, MM 재시작/재생성 때 Docker DNS 가 hermes-pg 를 못 찾아 부팅이 무한 실패한다(no such host).
# 컨테이너 env 를 바꾸려면 MM 재생성이 필요해 침습적이므로, PG 에 네트워크 별칭을 멱등 부여해
# 'hermes-pg' 가 'bogo-pg' 로 해석되게 한다. 별칭은 컨테이너 재생성 시 사라지므로 매 부팅 보장.
PG_LEGACY_ALIAS="hermes-pg"

ensure_pg_legacy_alias() {
  # PG 가 붙어 있는 네트워크를 찾아 거기에 hermes-pg 별칭이 없으면 부여한다(멱등).
  local nets
  nets="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$PG_NAME" 2>/dev/null || echo '')"
  for net in $nets; do
    # 이미 hermes-pg 별칭이 있으면 건너뛴다.
    local aliases
    aliases="$(docker inspect -f "{{range \$k,\$v := .NetworkSettings.Networks}}{{if eq \$k \"$net\"}}{{range \$v.Aliases}}{{.}} {{end}}{{end}}{{end}}" "$PG_NAME" 2>/dev/null || echo '')"
    case " $aliases " in
      *" $PG_LEGACY_ALIAS "*) ok "$PG_NAME ($net) 에 $PG_LEGACY_ALIAS 별칭 이미 존재 — 건너뜀." ;;
      *)
        # disconnect→connect 로 별칭 부여(기존 별칭 보존: 컨테이너 이름 별칭은 자동 유지).
        if docker network disconnect "$net" "$PG_NAME" >/dev/null 2>&1 \
           && docker network connect --alias "$PG_LEGACY_ALIAS" --alias "$PG_NAME" "$net" "$PG_NAME" >/dev/null 2>&1; then
          ok "$PG_NAME ($net) 에 $PG_LEGACY_ALIAS 별칭 부여 — MM DataSource 해석 보장."
        else
          warn "$PG_NAME 에 $PG_LEGACY_ALIAS 별칭 부여 실패. MM 이 DB 연결 못 하면 수동 확인 필요."
        fi
        ;;
    esac
  done
}

ensure_containers() {
  need docker
  # Docker 데몬 도달성 확인(Linux: 데몬/권한, macOS: Colima 경유). 실패 시 명확 안내 후 종료.
  ensure_docker_reachable
  # DB 먼저(데이터 계층), 그 다음 MM(앱 계층).
  ensure_container "$PG_NAME"
  # MM 을 띄우기 전에 레거시 호스트명 별칭을 보장한다(MM 이 hermes-pg 로 DB 를 찾으므로).
  ensure_pg_legacy_alias
  ensure_container "$MM_NAME"
}

# ── 3. MM healthy 대기 ────────────────────────────────────────────────────
# 외부 호스트 curl 대신 컨테이너 내부에서 ping 한다(호스트 도구·훅 의존 제거,
# 컨테이너가 자기 자신 8065 를 응답할 수 있는지가 진짜 readiness 신호).
wait_mm_ready() {
  say "Mattermost healthy 대기 (최대 ${MM_WAIT_TIMEOUT}s)..."
  local waited=0 code
  local ping_path="/api/v4/system/ping"
  while :; do
    # docker 내부 healthcheck 가 healthy 면 즉시 통과(가장 신뢰도 높은 신호).
    local health
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$MM_NAME" 2>/dev/null || echo 'none')"
    if [ "$health" = "healthy" ]; then
      ok "Mattermost healthy (docker healthcheck, ${waited}s)."
      return 0
    fi
    # healthcheck 가 없거나 미정이면 컨테이너 내부에서 직접 ping.
    if [ "$health" = "none" ] || [ "$health" = "starting" ]; then
      code="$(docker exec "$MM_NAME" sh -c "curl -s -o /dev/null -w '%{http_code}' http://localhost:8065${ping_path} 2>/dev/null || wget -q -O /dev/null -S http://localhost:8065${ping_path} 2>&1 | awk '/HTTP\\//{print \$2; exit}'" 2>/dev/null || echo "")"
      if [ "$code" = "200" ]; then
        ok "Mattermost ping 200 (${waited}s)."
        return 0
      fi
    fi
    sleep 3; waited=$((waited + 3))
    if [ "$waited" -ge "$MM_WAIT_TIMEOUT" ]; then
      err "Mattermost 가 ${MM_WAIT_TIMEOUT}s 안에 준비되지 않았습니다 (health=$health)."
      err "진단: docker logs --tail 50 $MM_NAME"
      exit 1
    fi
  done
}

# ── 4. 무인 프로비저닝 (새 PC: 토큰·팀·채널 자동 발급) ───────────────────
# WHY: 통신 백본(MM)만 떠 있고 (a)관리자/팀/봇 계정 (b)봇 Access Token
#   (c)채널 ID 가 없으면 봇이 못 붙는다. 그 시크릿들은 git 제외라 폴더만 옮긴
#   새 PC 에선 비어 있다. mmctl --local(컨테이너 로컬 소켓, 인증 불필요)로 전부
#   멱등 생성·발급해 *_config.json / channels.json 에 기록한다.
#   이미 토큰이 채워진 PC 에선 실재 검증 후 보존(불필요 재발급·회귀 없음).
#   BOGO_SKIP_PROVISION=1 이면 건너뛴다(수동 관리 환경 탈출구).
ensure_provisioned() {
  if [ "${BOGO_SKIP_PROVISION:-0}" = "1" ]; then
    say "BOGO_SKIP_PROVISION=1 → 무인 프로비저닝 건너뜀."
    return 0
  fi
  local prov="$HERE/provision_mm.py"
  if [ ! -f "$prov" ]; then
    say "provision_mm.py 없음 → 프로비저닝 건너뜀(레거시 호환)."
    return 0
  fi
  # venv 파이썬 우선(agent_schema import 필요). 없으면 시스템 python3 폴백.
  local py="$HERE/.venv/bin/python"
  [ -x "$py" ] || py="$(command -v python3 || true)"
  if [ -z "$py" ]; then
    warn "python 을 찾지 못해 프로비저닝을 건너뜁니다(봇 토큰이 비어 있으면 기동 실패 가능)."
    return 0
  fi
  say "무인 프로비저닝 실행(토큰·팀·채널 멱등 발급)..."
  if "$py" "$prov"; then
    ok "프로비저닝 완료(또는 기존 자격증명 재사용)."
  else
    # 프로비저닝 실패는 치명. 토큰이 없으면 봇/대시보드가 못 뜬다 → 거짓완료 방지 위해 중단.
    err "무인 프로비저닝 실패. 진단: docker exec $MM_NAME mmctl --local system version"
    return 1
  fi
}

main() {
  say "통신 백본 부트 의존성 체인 시작 (Colima → 컨테이너 → MM readiness → 프로비저닝)."
  ensure_colima
  ensure_containers
  wait_mm_ready
  ensure_provisioned
  ok "통신 백본 준비 완료. 봇 기동 가능."
}

main "$@"
