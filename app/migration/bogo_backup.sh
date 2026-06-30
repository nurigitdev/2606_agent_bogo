#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO 데이터 내보내기 (A) — 기존 PC 의 대화/계정/채널/보고 DB 를 단일 백업본으로
# ════════════════════════════════════════════════════════════════════════
#  WHY  폴더(코드·설정·인프라 정의)는 git 으로 따라가지만, 실제 대화·계정·채널·보고
#    데이터는 Docker named volume(bogo-pg-data / bogo-mm-data ...) 에만 있다. 폴더만
#    옮기면 빈 Mattermost 가 새로 만들어질 뿐 기존 데이터는 따라오지 않는다. 이 스크립트가
#    그 공백을 메운다 — 컨테이너가 마운트한 볼륨을 통째로 떠내 단일 .tar.gz 산출물 하나로
#    만든다(새 PC 의 복원 스크립트가 이 파일 하나만 보면 됨).
#
#  WHAT (전부 멱등·비파괴 — 읽기 전용 덤프만, 원본 볼륨/컨테이너 무손상):
#    1) docker/colima 가 떠 있고 bogo-pg/bogo-mm 가 존재하는지 점검(없으면 명확한 1줄 안내)
#    2) bogo-pg: pg_dump --format=custom 으로 논리 덤프(이식성 최상, 버전 차이 흡수)
#       + 안전망으로 PG 데이터 볼륨 원본도 tar 로 동봉(논리 복원 실패 시 물리 복원 대비)
#    3) bogo-mm: data/config/plugins 볼륨을 tar 로 떠냄(첨부파일·설정·플러그인 보존)
#    4) 위 전부를 app/migration/bogo_backup_<날짜시각>.tar.gz 단일 산출물로 묶고,
#       무결성 검증(tar -t)·매니페스트(manifest.json) 동봉. 최신본은 bogo_backup_latest.tar.gz
#       심볼릭/복사로도 남겨 복원 스크립트가 자동으로 집어가게 한다.
#
#  계약(infra_up.sh / docker-compose.yml 과 1:1):
#    - 컨테이너 이름 고정: bogo-pg / bogo-mm  (볼륨 이름이 PC마다 달라도 컨테이너 기준으로 접근)
#    - PG 자격증명: docker-compose.yml 의 ${BOGO_PG_USER:-mmuser}/${BOGO_PG_DB:-mattermost}
#    - 볼륨 경로: PG=/var/lib/postgresql/data, MM=/mattermost/{data,config,plugins}
#
#  안전: 외부 네트워크/포트 안 건드림. 시크릿(비밀번호)은 컨테이너 env 에서만 읽고 출력 안 함.
#        원본 컨테이너·볼륨은 절대 삭제/수정하지 않는다(순수 read-only 덤프).
#  사용:
#    ./bogo_backup.sh                  # 백업 생성(기본)
#    ./bogo_backup.sh --out <경로>     # 산출물 디렉터리 지정(기본 app/migration)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# ── 컨테이너 이름 계약(docker-compose.yml / infra_up.sh 와 동일) ──────────
PG_NAME="bogo-pg"
MM_NAME="bogo-mm"
# PG 자격증명 기본값(compose 기본과 일치). 컨테이너 env 에서 실제값을 읽어 덮어쓴다.
PG_USER_DEFAULT="mmuser"
PG_DB_DEFAULT="mattermost"

OUT_DIR="$HERE"
# 보관 개수(최근 N개만 남기고 자동 정리 — 폴더 비대화/디스크 통제). 환경변수로 조정 가능.
RETAIN="${BOGO_BACKUP_RETAIN:-3}"
# --quiet: 자동(launchd) 호출 시 색/장식 출력을 줄이고 로그를 간결히(사람 대면 X).
QUIET=0
# --out <dir> 옵션 파싱.
while [ $# -gt 0 ]; do
  case "$1" in
    --out)    OUT_DIR="${2:-$HERE}"; shift 2 ;;
    --retain) RETAIN="${2:-3}"; shift 2 ;;
    --quiet)  QUIET=1; shift ;;
    *) shift ;;
  esac
done

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { [ "${QUIET:-0}" -eq 1 ] && return 0; printf '%s[backup]%s %s\n' "$C_INFO" "$C_RST" "$*"; }
ok()   { printf '%s[backup:OK]%s %s\n'   "$C_OK"   "$C_RST" "$*"; }
warn() { printf '%s[backup:경고]%s %s\n' "$C_WARN" "$C_RST" "$*" >&2; }
err()  { printf '%s[backup:오류]%s %s\n' "$C_ERR"  "$C_RST" "$*" >&2; }

# ── 0. 사전 점검: docker 가 살아있고 컨테이너가 존재하는가 ────────────────
preflight() {
  if ! command -v docker >/dev/null 2>&1; then
    err "docker 명령을 찾지 못했습니다."
    err "할 일 1가지: Docker Desktop 설치 또는 'brew install docker colima' 후 다시 실행."
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    err "Docker 데몬에 연결할 수 없습니다(Colima/Docker Desktop 미기동)."
    err "할 일 1가지: 터미널에서 'colima start' 실행(또는 Docker Desktop 기동) 후 다시 실행."
    exit 1
  fi
  local missing=0
  for c in "$PG_NAME" "$MM_NAME"; do
    if [ -z "$(docker ps -aq -f "name=^${c}$" 2>/dev/null)" ]; then
      err "컨테이너 '$c' 가 이 PC 에 없습니다 — 백업할 데이터가 없습니다."
      missing=1
    fi
  done
  [ "$missing" -eq 0 ] || { err "이 PC 가 BOGO 원본(데이터 보유) PC 가 맞는지 확인하세요."; exit 1; }
}

# 컨테이너 env 에서 실제 PG 자격증명 읽기(시크릿은 변수로만, 출력 금지).
read_pg_creds() {
  PG_USER="$(docker exec "$PG_NAME" sh -c 'printf %s "$POSTGRES_USER"' 2>/dev/null || true)"
  PG_DB="$(docker exec "$PG_NAME" sh -c 'printf %s "$POSTGRES_DB"' 2>/dev/null || true)"
  [ -n "${PG_USER:-}" ] || PG_USER="$PG_USER_DEFAULT"
  [ -n "${PG_DB:-}" ]   || PG_DB="$PG_DB_DEFAULT"
}

# 임시 작업공간 — 모든 덤프 조각을 여기 모았다가 단일 tar.gz 로 묶는다.
STAGE=""
cleanup() { [ -n "${STAGE:-}" ] && rm -rf "$STAGE" 2>/dev/null || true; }
trap cleanup EXIT

main() {
  say "BOGO 데이터 백업 시작 (컨테이너 기준 read-only 덤프)."
  preflight
  read_pg_creds

  mkdir -p "$OUT_DIR"
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  STAGE="$(mktemp -d "${TMPDIR:-/tmp}/bogo_backup.XXXXXX")"
  local payload="$STAGE/payload"
  mkdir -p "$payload"

  # ── 1. PG 논리 덤프(이식성 최상) ─────────────────────────────────────
  say "[1/4] Postgres 논리 덤프 (pg_dump custom, db=$PG_DB)..."
  if docker exec "$PG_NAME" pg_dump -U "$PG_USER" -d "$PG_DB" -F c -Z 6 \
        > "$payload/pg_dump.custom" 2>"$STAGE/pg_dump.err"; then
    ok "PG 논리 덤프 완료 ($(du -h "$payload/pg_dump.custom" | cut -f1))."
  else
    warn "pg_dump 실패 — 물리 볼륨 복원 경로로 대체 가능(아래 PG 볼륨 tar 동봉). 상세: $(cat "$STAGE/pg_dump.err" 2>/dev/null | tail -1)"
    rm -f "$payload/pg_dump.custom"
  fi

  # ── 2. PG 데이터 볼륨 물리 tar(안전망) ───────────────────────────────
  say "[2/4] Postgres 데이터 볼륨 물리 백업(안전망)..."
  if docker run --rm --volumes-from "$PG_NAME" -v "$payload":/backup alpine \
        sh -c 'cd /var/lib/postgresql/data && tar czf /backup/pg_volume.tar.gz .' \
        >/dev/null 2>"$STAGE/pgvol.err"; then
    ok "PG 볼륨 물리 백업 완료 ($(du -h "$payload/pg_volume.tar.gz" | cut -f1))."
  else
    warn "PG 볼륨 물리 백업 실패(논리 덤프가 있으면 무방). 상세: $(tail -1 "$STAGE/pgvol.err" 2>/dev/null)"
  fi

  # ── 3. MM 볼륨(data/config/plugins) tar ──────────────────────────────
  say "[3/4] Mattermost 볼륨 백업 (data·config·plugins — 첨부·설정 보존)..."
  # MM data 가 핵심(파일 업로드), config·plugins 는 부가. 각각 별 tar 로 떠 멱등 복원.
  for spec in "data:/mattermost/data" "config:/mattermost/config" "plugins:/mattermost/plugins"; do
    local label="${spec%%:*}" path="${spec#*:}"
    if docker run --rm --volumes-from "$MM_NAME" -v "$payload":/backup alpine \
          sh -c "cd '$path' 2>/dev/null && tar czf /backup/mm_${label}.tar.gz . " \
          >/dev/null 2>>"$STAGE/mm.err"; then
      ok "MM $label 백업 완료 ($(du -h "$payload/mm_${label}.tar.gz" 2>/dev/null | cut -f1))."
    else
      warn "MM $label 백업 건너뜀(해당 볼륨 없음 가능)."
    fi
  done

  # ── 4. 매니페스트 + 단일 산출물 묶기 ─────────────────────────────────
  say "[4/4] 매니페스트 작성 + 단일 산출물 압축..."
  cat > "$payload/manifest.json" <<JSON
{
  "schema": "bogo-backup/v1",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source_host": "$(hostname 2>/dev/null || echo unknown)",
  "pg": { "container": "$PG_NAME", "user": "$PG_USER", "db": "$PG_DB",
          "logical_dump": $( [ -f "$payload/pg_dump.custom" ] && echo true || echo false ),
          "volume_tar":   $( [ -f "$payload/pg_volume.tar.gz" ] && echo true || echo false ) },
  "mm": { "container": "$MM_NAME",
          "data":    $( [ -f "$payload/mm_data.tar.gz" ] && echo true || echo false ),
          "config":  $( [ -f "$payload/mm_config.tar.gz" ] && echo true || echo false ),
          "plugins": $( [ -f "$payload/mm_plugins.tar.gz" ] && echo true || echo false ) }
}
JSON

  # 최소 하나의 PG 백업(논리 또는 물리)은 있어야 의미가 있다.
  if [ ! -f "$payload/pg_dump.custom" ] && [ ! -f "$payload/pg_volume.tar.gz" ]; then
    err "PG 백업이 논리·물리 모두 실패했습니다 — 산출물을 만들지 않습니다(빈 백업 방지)."
    exit 1
  fi

  local final="$OUT_DIR/bogo_backup_${stamp}.tar.gz"
  ( cd "$STAGE" && tar czf "$final" -C "$payload" . )

  # 무결성 검증(목록 출력 가능해야 정상).
  if ! tar tzf "$final" >/dev/null 2>&1; then
    err "산출물 무결성 검증 실패: $final"
    exit 1
  fi

  # 복원 스크립트가 자동으로 집어갈 'latest' 포인터(복사본 — 심볼릭은 USB/타 FS 에서 깨질 수 있음).
  cp -f "$final" "$OUT_DIR/bogo_backup_latest.tar.gz"

  # ── 보관 개수 제한(자동 정리 — 자동 백업이 폴더를 비대화시키지 않게) ──
  # bogo_backup_<stamp>.tar.gz 중 최신 RETAIN 개만 남기고 나머지 삭제. _latest 포인터는
  # 별도 파일이라 이 정리 대상이 아니다(항상 최신본을 가리킨 채 유지). 멱등: 개수 이하면 무동작.
  rotate_backups

  ok "백업 완료 → $final"
  ok "최신 포인터  → $OUT_DIR/bogo_backup_latest.tar.gz ($(du -h "$final" | cut -f1))"
  [ "$QUIET" -eq 1 ] || say "이 파일(또는 폴더 전체)을 새 PC 로 옮긴 뒤, 새 PC 에서 'BOGO 시작'을 더블클릭하면 자동 복원됩니다."
}

# 타임스탬프 백업본을 최신순 정렬해 RETAIN 개 초과분을 삭제한다(_latest 는 제외).
rotate_backups() {
  [ "$RETAIN" -ge 1 ] 2>/dev/null || RETAIN=3
  # ls -t 로 mtime 최신순. _latest 포인터는 glob 패턴에 안 걸린다(bogo_backup_<stamp> 만 매칭).
  local kept=0 f
  # shellcheck disable=SC2012  # 파일명에 개행 없음(우리가 stamp 로만 생성) → ls 안전.
  for f in $(ls -t "$OUT_DIR"/bogo_backup_[0-9]*.tar.gz 2>/dev/null); do
    kept=$((kept + 1))
    if [ "$kept" -gt "$RETAIN" ]; then
      rm -f "$f" 2>/dev/null && say "오래된 백업 정리: $(basename "$f")"
    fi
  done
}

main "$@"
