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

# 컨테이너 이름(고정). compose 가 아니라 docker run 으로 만들어진 영속 컨테이너다.
PG_NAME="bogo-pg"
MM_NAME="bogo-mm"

# MM healthy 대기 한도(초). MM 콜드 부팅은 수십 초 걸릴 수 있다.
MM_WAIT_TIMEOUT="${BOGO_MM_WAIT_TIMEOUT:-180}"
# Colima 부팅 대기 한도(초).
COLIMA_WAIT_TIMEOUT="${BOGO_COLIMA_WAIT_TIMEOUT:-180}"

need() {
  command -v "$1" >/dev/null 2>&1 || { err "$1 명령을 찾지 못했습니다. (brew install $1)"; exit 1; }
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

ensure_container() {
  local name="$1"
  local st; st="$(container_state "$name")"
  case "$st" in
    running)
      ok "$name 이미 running — 건너뜀." ;;
    absent)
      err "$name 컨테이너가 존재하지 않습니다. 최초 컨테이너 생성은 별도 절차 필요."
      err "(이 스크립트는 기존 영속 컨테이너의 기동만 보장합니다.)"
      exit 1 ;;
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

main() {
  say "통신 백본 부트 의존성 체인 시작 (Colima → 컨테이너 → MM readiness)."
  ensure_colima
  ensure_containers
  wait_mm_ready
  ok "통신 백본 준비 완료. 봇 기동 가능."
}

main "$@"
