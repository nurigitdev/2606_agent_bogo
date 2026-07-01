#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO data export (A) — bundle the old PC's chat/account/channel/report DB into a single backup
# ════════════════════════════════════════════════════════════════════════
#  WHY  The folder (code, config, infra definitions) is tracked by git, but the actual
#    chat/account/channel/report data lives only in the Docker named volumes
#    (bogo-pg-data / bogo-mm-data ...). Moving the folder alone just spawns a fresh empty
#    Mattermost — the existing data does not come along. This script fills that gap: it dumps
#    the container-mounted volumes wholesale into a single .tar.gz artifact (so the new PC's
#    restore script only needs to look at this one file).
#
#  WHAT (fully idempotent and non-destructive — read-only dump only, original volumes/containers untouched):
#    1) Check that docker/colima is up and bogo-pg/bogo-mm exist (if not, a clear one-line notice)
#    2) bogo-pg: logical dump via pg_dump --format=custom (best portability, absorbs version diffs)
#       + as a safety net, also bundle the raw PG data volume as a tar (fallback physical restore if logical restore fails)
#    3) bogo-mm: tar the data/config/plugins volumes (preserve attachments, settings, plugins)
#    4) Wrap all of the above into a single artifact app/migration/bogo_backup_<datetime>.tar.gz, with
#       integrity verification (tar -t) and a manifest (manifest.json) included. Also leave the latest as
#       bogo_backup_latest.tar.gz (symlink/copy) so the restore script picks it up automatically.
#
#  Contract (1:1 with infra_up.sh / docker-compose.yml):
#    - Fixed container names: bogo-pg / bogo-mm  (access by container even if volume names differ per PC)
#    - PG credentials: ${BOGO_PG_USER:-mmuser}/${BOGO_PG_DB:-mattermost} from docker-compose.yml
#    - Volume paths: PG=/var/lib/postgresql/data, MM=/mattermost/{data,config,plugins}
#
#  Safety: Does not touch external networks/ports. Secrets (passwords) are read only from the container env and never printed.
#        Original containers/volumes are never deleted or modified (pure read-only dump).
#  Usage:
#    ./bogo_backup.sh                  # create backup (default)
#    ./bogo_backup.sh --out <path>     # specify artifact directory (default app/migration)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# ── Container name contract (same as docker-compose.yml / infra_up.sh) ──────────
PG_NAME="bogo-pg"
MM_NAME="bogo-mm"
# PG credential defaults (matching compose defaults). Overridden by reading the actual values from the container env.
PG_USER_DEFAULT="mmuser"
PG_DB_DEFAULT="mattermost"

OUT_DIR="$HERE"
# Retention count (keep only the latest N and auto-prune — controls folder bloat/disk). Adjustable via env var.
RETAIN="${BOGO_BACKUP_RETAIN:-3}"
# --quiet: for automated (launchd) invocation, reduce color/decoration output and keep logs concise (not human-facing).
QUIET=0
# Parse the --out <dir> option.
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
warn() { printf '%s[backup:WARN]%s %s\n' "$C_WARN" "$C_RST" "$*" >&2; }
err()  { printf '%s[backup:ERROR]%s %s\n' "$C_ERR"  "$C_RST" "$*" >&2; }

# ── 0. Preflight: is docker alive and do the containers exist ────────────────
preflight() {
  if ! command -v docker >/dev/null 2>&1; then
    err "Could not find the docker command."
    err "One thing to do: install Docker Desktop or run 'brew install docker colima', then retry."
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    err "Cannot connect to the Docker daemon (Colima/Docker Desktop not running)."
    err "One thing to do: run 'colima start' in the terminal (or launch Docker Desktop), then retry."
    exit 1
  fi
  local missing=0
  for c in "$PG_NAME" "$MM_NAME"; do
    if [ -z "$(docker ps -aq -f "name=^${c}$" 2>/dev/null)" ]; then
      err "Container '$c' does not exist on this PC — there is no data to back up."
      missing=1
    fi
  done
  [ "$missing" -eq 0 ] || { err "Check whether this PC is really the BOGO source (data-holding) PC."; exit 1; }
}

# Read the actual PG credentials from the container env (secrets stay in variables only, never printed).
read_pg_creds() {
  PG_USER="$(docker exec "$PG_NAME" sh -c 'printf %s "$POSTGRES_USER"' 2>/dev/null || true)"
  PG_DB="$(docker exec "$PG_NAME" sh -c 'printf %s "$POSTGRES_DB"' 2>/dev/null || true)"
  [ -n "${PG_USER:-}" ] || PG_USER="$PG_USER_DEFAULT"
  [ -n "${PG_DB:-}" ]   || PG_DB="$PG_DB_DEFAULT"
}

# Temp workspace — gather all dump pieces here, then bundle into a single tar.gz.
STAGE=""
cleanup() { [ -n "${STAGE:-}" ] && rm -rf "$STAGE" 2>/dev/null || true; }
trap cleanup EXIT

main() {
  say "Starting BOGO data backup (container-based read-only dump)."
  preflight
  read_pg_creds

  mkdir -p "$OUT_DIR"
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  STAGE="$(mktemp -d "${TMPDIR:-/tmp}/bogo_backup.XXXXXX")"
  local payload="$STAGE/payload"
  mkdir -p "$payload"

  # ── 1. PG logical dump (best portability) ─────────────────────────────────
  say "[1/4] Postgres logical dump (pg_dump custom, db=$PG_DB)..."
  if docker exec "$PG_NAME" pg_dump -U "$PG_USER" -d "$PG_DB" -F c -Z 6 \
        > "$payload/pg_dump.custom" 2>"$STAGE/pg_dump.err"; then
    ok "PG logical dump complete ($(du -h "$payload/pg_dump.custom" | cut -f1))."
  else
    warn "pg_dump failed — physical volume restore path can be used instead (PG volume tar bundled below). Details: $(cat "$STAGE/pg_dump.err" 2>/dev/null | tail -1)"
    rm -f "$payload/pg_dump.custom"
  fi

  # ── 2. PG data volume physical tar (safety net) ───────────────────────────
  say "[2/4] Postgres data volume physical backup (safety net)..."
  if docker run --rm --volumes-from "$PG_NAME" -v "$payload":/backup alpine \
        sh -c 'cd /var/lib/postgresql/data && tar czf /backup/pg_volume.tar.gz .' \
        >/dev/null 2>"$STAGE/pgvol.err"; then
    ok "PG volume physical backup complete ($(du -h "$payload/pg_volume.tar.gz" | cut -f1))."
  else
    warn "PG volume physical backup failed (fine if the logical dump exists). Details: $(tail -1 "$STAGE/pgvol.err" 2>/dev/null)"
  fi

  # ── 3. MM volumes (data/config/plugins) tar ──────────────────────────────
  say "[3/4] Mattermost volume backup (data/config/plugins — preserve attachments/settings)..."
  # MM data is the core (file uploads); config/plugins are supplementary. Tar each separately for idempotent restore.
  for spec in "data:/mattermost/data" "config:/mattermost/config" "plugins:/mattermost/plugins"; do
    local label="${spec%%:*}" path="${spec#*:}"
    if docker run --rm --volumes-from "$MM_NAME" -v "$payload":/backup alpine \
          sh -c "cd '$path' 2>/dev/null && tar czf /backup/mm_${label}.tar.gz . " \
          >/dev/null 2>>"$STAGE/mm.err"; then
      ok "MM $label backup complete ($(du -h "$payload/mm_${label}.tar.gz" 2>/dev/null | cut -f1))."
    else
      warn "MM $label backup skipped (volume may not exist)."
    fi
  done

  # ── 4. Manifest + bundle single artifact ─────────────────────────────────
  say "[4/4] Writing manifest + compressing single artifact..."
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

  # At least one PG backup (logical or physical) must exist to be meaningful.
  if [ ! -f "$payload/pg_dump.custom" ] && [ ! -f "$payload/pg_volume.tar.gz" ]; then
    err "PG backup failed both logically and physically — not creating an artifact (avoids an empty backup)."
    exit 1
  fi

  local final="$OUT_DIR/bogo_backup_${stamp}.tar.gz"
  ( cd "$STAGE" && tar czf "$final" -C "$payload" . )

  # Integrity verification (must be able to list contents to be valid).
  if ! tar tzf "$final" >/dev/null 2>&1; then
    err "Artifact integrity verification failed: $final"
    exit 1
  fi

  # 'latest' pointer that the restore script picks up automatically (a copy — symlinks can break on USB/other filesystems).
  cp -f "$final" "$OUT_DIR/bogo_backup_latest.tar.gz"

  # ── Retention limit (auto-prune — so automated backups don't bloat the folder) ──
  # Keep only the latest RETAIN of the bogo_backup_<stamp>.tar.gz files and delete the rest. The _latest pointer is
  # a separate file and not subject to this pruning (it always keeps pointing at the latest). Idempotent: no-op if at or below the count.
  rotate_backups

  ok "Backup complete → $final"
  ok "Latest pointer → $OUT_DIR/bogo_backup_latest.tar.gz ($(du -h "$final" | cut -f1))"
  [ "$QUIET" -eq 1 ] || say "Move this file (or the whole folder) to the new PC, then double-click 'BOGO_start' on the new PC to restore automatically."
}

# Sort the timestamped backups by name (= chronological, since stamp is YYYYMMDD-HHMMSS so lexical=chronological)
# and delete the excess over RETAIN (the oldest). The _latest pointer is not caught by the glob.
rotate_backups() {
  [ "$RETAIN" -ge 1 ] 2>/dev/null || RETAIN=3
  # Collect safely via glob (avoid parsing ls). If nothing matches, check existence in case nullglob isn't set.
  local files=() f
  for f in "$OUT_DIR"/bogo_backup_[0-9]*.tar.gz; do
    [ -f "$f" ] && files+=("$f")
  done
  local total="${#files[@]}"
  [ "$total" -gt "$RETAIN" ] || return 0
  # The filename consists of the stamp only → the front of the lexical sort is the oldest. Sort with sort.
  local sorted; sorted="$(printf '%s\n' "${files[@]}" | sort)"
  local remove=$((total - RETAIN)) i=0
  while IFS= read -r f; do
    [ "$i" -ge "$remove" ] && break
    rm -f "$f" 2>/dev/null && say "Pruned old backup: $(basename "$f")"
    i=$((i + 1))
  done <<< "$sorted"
}

main "$@"
