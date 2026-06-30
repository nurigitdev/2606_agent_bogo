#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO 데이터 복원 (B-코어) — 백업본을 새 PC 의 bogo-pg/bogo-mm 볼륨으로 주입
# ════════════════════════════════════════════════════════════════════════
#  WHY  새 PC 에서 infra_up.sh 가 빈 bogo-pg/bogo-mm 를 막 만든 직후, 기존 PC 의
#    대화·계정·채널·보고 데이터를 그 안으로 밀어넣어야 '데이터까지 따라오는' 이전이
#    완성된다. 이 스크립트는 그 주입을 담당한다 — 단, 백업본이 있을 때만 동작하고
#    없으면 조용히 빈 상태로 둔다(사람에게 묻지 않는 자동 분기는 호출부 deploy 가 한다).
#
#  WHAT (멱등·자동복원 분기):
#    1) 백업 산출물 자동 탐색(인자 없으면 app/migration/bogo_backup_latest.tar.gz)
#    2) 이미 복원됨 표식(.bogo_restored)이 있고 --force 가 아니면 skip(두 번 돌려도 안전)
#    3) 산출물 풀어 manifest 읽기
#    4) MM 정지(데이터 주입 중 쓰기 충돌 방지) → PG 복원 → MM 볼륨 복원 → MM 재기동
#       PG: 논리 덤프 우선(pg_restore --clean --if-exists), 없으면 물리 볼륨 복원
#       MM: data/config/plugins tar 를 볼륨에 풀기(기존 빈 데이터 위에 덮어쓰기)
#    5) 복원 완료 표식 기록 → 재배포 시 중복 복원 방지
#
#  계약: 컨테이너 이름 bogo-pg/bogo-mm 고정. PG 자격증명은 컨테이너 env 에서 읽음.
#  안전: 백업본이 없으면 비파괴 종료(rc=0, "빈 상태로 진행"). 시크릿 출력 안 함.
#        --force 없이는 이미 복원된 환경을 덮어쓰지 않는다(기존 PC 에서 실수로 돌려도 안전).
#  사용:
#    ./bogo_restore.sh                  # latest 자동 탐색 후 복원(없으면 빈 상태로 통과)
#    ./bogo_restore.sh <백업.tar.gz>    # 특정 백업본 지정
#    ./bogo_restore.sh --force [<파일>] # 이미 복원된 환경에도 강제 재복원(주의: 덮어쓰기)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

PG_NAME="bogo-pg"
MM_NAME="bogo-mm"
PG_USER_DEFAULT="mmuser"
PG_DB_DEFAULT="mattermost"

# 복원 완료 표식(멱등 가드). 백업 파일 경로/해시까지 기록해 다른 백업으로 바뀌면 재복원 허용.
MARKER="$HERE/.bogo_restored"

FORCE=0
BACKUP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    *) BACKUP="$1"; shift ;;
  esac
done
# 인자 없으면 latest 포인터 자동 탐색.
[ -n "$BACKUP" ] || BACKUP="$HERE/bogo_backup_latest.tar.gz"

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf '%s[restore]%s %s\n'      "$C_INFO" "$C_RST" "$*"; }
ok()   { printf '%s[restore:OK]%s %s\n'   "$C_OK"   "$C_RST" "$*"; }
warn() { printf '%s[restore:경고]%s %s\n' "$C_WARN" "$C_RST" "$*" >&2; }
err()  { printf '%s[restore:오류]%s %s\n' "$C_ERR"  "$C_RST" "$*" >&2; }

read_pg_creds() {
  PG_USER="$(docker exec "$PG_NAME" sh -c 'printf %s "$POSTGRES_USER"' 2>/dev/null || true)"
  PG_DB="$(docker exec "$PG_NAME" sh -c 'printf %s "$POSTGRES_DB"' 2>/dev/null || true)"
  [ -n "${PG_USER:-}" ] || PG_USER="$PG_USER_DEFAULT"
  [ -n "${PG_DB:-}" ]   || PG_DB="$PG_DB_DEFAULT"
}

# 백업 파일 지문(경로+크기+mtime). 같은 백업이면 동일 → 중복 복원 skip 판정에 사용.
backup_fingerprint() {
  local f="$1"
  # 해시 도구 선호 순서: shasum(macOS 기본) → sha256sum(GNU/Linux 기본). 둘 다 없으면
  # 크기+mtime 로 근사(BSD stat -f → GNU stat -c 폴백). OS 무관하게 항상 지문을 만든다.
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$f" 2>/dev/null | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$f" 2>/dev/null | awk '{print $1}'
  else
    stat -f '%z-%m' "$f" 2>/dev/null || stat -c '%s-%Y' "$f" 2>/dev/null || echo "nofp"
  fi
}

STAGE=""
cleanup() { [ -n "${STAGE:-}" ] && rm -rf "$STAGE" 2>/dev/null || true; }
trap cleanup EXIT

main() {
  # ── 자동 분기: 백업본이 없으면 비파괴 통과(빈 상태로 초기 셋업) ────────
  if [ ! -f "$BACKUP" ]; then
    say "백업본 없음($BACKUP) → 데이터 복원 건너뜀(빈 상태로 초기 셋업 진행)."
    exit 0
  fi

  # ── 멱등 가드: 이미 같은 백업으로 복원했으면 skip ────────────────────
  local fp; fp="$(backup_fingerprint "$BACKUP")"
  if [ "$FORCE" -ne 1 ] && [ -f "$MARKER" ] && grep -q "$fp" "$MARKER" 2>/dev/null; then
    ok "이미 이 백업본으로 복원됨(표식 일치) → 중복 복원 skip. 강제: --force"
    exit 0
  fi

  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    err "Docker 데몬에 연결할 수 없습니다. 'colima start' 후 다시 실행하세요."
    exit 1
  fi
  for c in "$PG_NAME" "$MM_NAME"; do
    if [ -z "$(docker ps -aq -f "name=^${c}$" 2>/dev/null)" ]; then
      err "컨테이너 '$c' 가 없습니다 — 먼저 infra_up.sh 로 인프라를 만든 뒤 복원하세요."
      exit 1
    fi
  done

  read_pg_creds
  STAGE="$(mktemp -d "${TMPDIR:-/tmp}/bogo_restore.XXXXXX")"
  say "백업본 해제: $(basename "$BACKUP")"
  if ! tar xzf "$BACKUP" -C "$STAGE" 2>/dev/null; then
    err "백업본 압축 해제 실패(손상 가능): $BACKUP"
    exit 1
  fi
  local P="$STAGE"   # payload 루트(백업이 payload 내용을 루트로 담음)

  # ── MM 정지(주입 중 쓰기 충돌 방지) ──────────────────────────────────
  say "Mattermost 일시 정지(데이터 주입 중 충돌 방지)..."
  docker stop "$MM_NAME" >/dev/null 2>&1 || true

  # ── PG 복원: 논리 덤프 우선, 없으면 물리 볼륨 ─────────────────────────
  if [ -f "$P/pg_dump.custom" ]; then
    say "[PG] 논리 덤프 복원(pg_restore --clean --if-exists, db=$PG_DB)..."
    # PG 가 떠 있어야 pg_restore 가능. 떠 있지 않으면 기동.
    docker start "$PG_NAME" >/dev/null 2>&1 || true
    # PG ready 대기.
    local i=0
    until docker exec "$PG_NAME" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; do
      sleep 1; i=$((i+1)); [ "$i" -ge 30 ] && { err "PG 가 준비되지 않음(30s)."; exit 1; }
    done
    if docker exec -i "$PG_NAME" pg_restore -U "$PG_USER" -d "$PG_DB" \
          --clean --if-exists --no-owner --no-acl < "$P/pg_dump.custom" \
          >"$STAGE/pg_restore.log" 2>&1; then
      ok "PG 논리 복원 완료."
    else
      # pg_restore 는 --clean 시 존재하지 않는 객체 DROP 경고로 비0 종료할 수 있다 → 로그로 판단.
      if grep -qiE 'error|fatal' "$STAGE/pg_restore.log"; then
        warn "PG 논리 복원에 경고/오류가 있었습니다(상당수는 무해한 DROP 경고). 상세 마지막 줄:"
        tail -3 "$STAGE/pg_restore.log" >&2 || true
      else
        ok "PG 논리 복원 완료(경고만)."
      fi
    fi
  elif [ -f "$P/pg_volume.tar.gz" ]; then
    say "[PG] 물리 볼륨 복원(논리 덤프 부재 → 안전망 경로)..."
    docker stop "$PG_NAME" >/dev/null 2>&1 || true
    docker run --rm --volumes-from "$PG_NAME" -v "$P":/backup alpine \
      sh -c 'cd /var/lib/postgresql/data && rm -rf ./* ./.[!.]* 2>/dev/null; tar xzf /backup/pg_volume.tar.gz' \
      >/dev/null 2>"$STAGE/pgvol.err" \
      && ok "PG 물리 복원 완료." \
      || { err "PG 물리 복원 실패: $(tail -1 "$STAGE/pgvol.err" 2>/dev/null)"; exit 1; }
    docker start "$PG_NAME" >/dev/null 2>&1 || true
  else
    err "백업본에 PG 데이터(논리/물리)가 없습니다 — 복원 불가."
    exit 1
  fi

  # ── MM 볼륨 복원(data/config/plugins) ────────────────────────────────
  for spec in "data:/mattermost/data" "config:/mattermost/config" "plugins:/mattermost/plugins"; do
    local label="${spec%%:*}" path="${spec#*:}"
    local tarf="$P/mm_${label}.tar.gz"
    [ -f "$tarf" ] || { say "[MM] $label 백업 없음 → 건너뜀."; continue; }
    say "[MM] $label 볼륨 복원..."
    # 빈(새로 만든) 볼륨 위에 덮어쓴다. 멱등: 같은 내용 재적용도 안전.
    docker run --rm --volumes-from "$MM_NAME" -v "$P":/backup alpine \
      sh -c "mkdir -p '$path' && cd '$path' && tar xzf /backup/mm_${label}.tar.gz" \
      >/dev/null 2>>"$STAGE/mm.err" \
      && ok "[MM] $label 복원 완료." \
      || warn "[MM] $label 복원 실패: $(tail -1 "$STAGE/mm.err" 2>/dev/null)"
  done

  # ── MM 재기동 ────────────────────────────────────────────────────────
  say "Mattermost 재기동..."
  docker start "$MM_NAME" >/dev/null 2>&1 || true

  # ── 복원 완료 표식(멱등 가드 갱신) ──────────────────────────────────
  {
    echo "restored_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "backup_file=$(basename "$BACKUP")"
    echo "fingerprint=$fp"
  } > "$MARKER"

  ok "데이터 복원 완료. 기존 PC 의 대화/계정/채널/보고가 이 PC 로 이전되었습니다."
  say "MM 이 healthy 가 될 때까지 수십 초 걸릴 수 있습니다(이후 봇이 자동 연결)."
}

main "$@"
