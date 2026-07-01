#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO data restore (B-core) — inject the backup into the new PC's bogo-pg/bogo-mm volumes
# ════════════════════════════════════════════════════════════════════════
#  WHY  Right after infra_up.sh freshly creates empty bogo-pg/bogo-mm on the new PC, the old PC's
#    chat/account/channel/report data must be pushed into them for a migration that "brings the data
#    along" to be complete. This script handles that injection — but it acts only when a backup exists,
#    and otherwise quietly leaves things empty (the automatic branching that doesn't ask a human is done by the calling deploy).
#
#  WHAT (idempotent auto-restore branch):
#    1) Auto-discover the backup artifact (if no argument, app/migration/bogo_backup_latest.tar.gz)
#    2) If an already-restored marker (.bogo_restored) exists and it's not --force, skip (safe to run twice)
#    3) Extract the artifact and read the manifest
#    4) Stop MM (prevent write conflicts during data injection) → restore PG → restore MM volumes → restart MM
#       PG: prefer logical dump (pg_restore --clean --if-exists); if absent, physical volume restore
#       MM: extract data/config/plugins tars into the volumes (overwrite on top of the existing empty data)
#    5) Record a restore-complete marker → prevent duplicate restore on redeploy
#
#  Contract: fixed container names bogo-pg/bogo-mm. PG credentials read from the container env.
#  Safety: if no backup exists, exit non-destructively (rc=0, "proceed empty"). Secrets are not printed.
#        Without --force it does not overwrite an already-restored environment (safe even if run by mistake on the old PC).
#  Usage:
#    ./bogo_restore.sh                  # auto-discover latest then restore (if none, pass through empty)
#    ./bogo_restore.sh <backup.tar.gz>  # specify a particular backup
#    ./bogo_restore.sh --force [<file>] # force re-restore even on an already-restored environment (caution: overwrite)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

PG_NAME="bogo-pg"
MM_NAME="bogo-mm"
PG_USER_DEFAULT="mmuser"
PG_DB_DEFAULT="mattermost"

# Restore-complete marker (idempotency guard). Records the backup file path/hash too, so switching to a different backup allows re-restore.
MARKER="$HERE/.bogo_restored"

FORCE=0
BACKUP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    *) BACKUP="$1"; shift ;;
  esac
done
# If no argument, auto-discover the latest pointer.
[ -n "$BACKUP" ] || BACKUP="$HERE/bogo_backup_latest.tar.gz"

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf '%s[restore]%s %s\n'      "$C_INFO" "$C_RST" "$*"; }
ok()   { printf '%s[restore:OK]%s %s\n'   "$C_OK"   "$C_RST" "$*"; }
warn() { printf '%s[restore:WARN]%s %s\n' "$C_WARN" "$C_RST" "$*" >&2; }
err()  { printf '%s[restore:ERROR]%s %s\n' "$C_ERR"  "$C_RST" "$*" >&2; }

read_pg_creds() {
  PG_USER="$(docker exec "$PG_NAME" sh -c 'printf %s "$POSTGRES_USER"' 2>/dev/null || true)"
  PG_DB="$(docker exec "$PG_NAME" sh -c 'printf %s "$POSTGRES_DB"' 2>/dev/null || true)"
  [ -n "${PG_USER:-}" ] || PG_USER="$PG_USER_DEFAULT"
  [ -n "${PG_DB:-}" ]   || PG_DB="$PG_DB_DEFAULT"
}

# Backup file fingerprint (path+size+mtime). Identical for the same backup → used to decide duplicate-restore skip.
backup_fingerprint() {
  local f="$1"
  # Hash tool preference order: shasum (macOS default) → sha256sum (GNU/Linux default). If neither exists,
  # approximate with size+mtime (BSD stat -f → GNU stat -c fallback). Always produces a fingerprint regardless of OS.
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
  # ── Auto-branch: if no backup exists, pass through non-destructively (initial setup with empty state) ────────
  if [ ! -f "$BACKUP" ]; then
    say "No backup ($BACKUP) → skipping data restore (proceeding with empty-state initial setup)."
    exit 0
  fi

  # ── Idempotency guard: if already restored from the same backup, skip ────────────────────
  local fp; fp="$(backup_fingerprint "$BACKUP")"
  if [ "$FORCE" -ne 1 ] && [ -f "$MARKER" ] && grep -q "$fp" "$MARKER" 2>/dev/null; then
    ok "Already restored from this backup (marker matches) → skipping duplicate restore. Force: --force"
    exit 0
  fi

  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    err "Cannot connect to the Docker daemon. Run 'colima start', then retry."
    exit 1
  fi
  for c in "$PG_NAME" "$MM_NAME"; do
    if [ -z "$(docker ps -aq -f "name=^${c}$" 2>/dev/null)" ]; then
      err "Container '$c' does not exist — create the infra first with infra_up.sh, then restore."
      exit 1
    fi
  done

  read_pg_creds
  STAGE="$(mktemp -d "${TMPDIR:-/tmp}/bogo_restore.XXXXXX")"
  say "Extracting backup: $(basename "$BACKUP")"
  if ! tar xzf "$BACKUP" -C "$STAGE" 2>/dev/null; then
    err "Failed to extract backup (possibly corrupt): $BACKUP"
    exit 1
  fi
  local P="$STAGE"   # payload root (the backup holds the payload contents at the root)

  # ── Stop MM (prevent write conflicts during injection) ──────────────────────────────────
  say "Pausing Mattermost (prevent conflicts during data injection)..."
  docker stop "$MM_NAME" >/dev/null 2>&1 || true

  # ── PG restore: prefer logical dump, fall back to physical volume ─────────────────────────
  if [ -f "$P/pg_dump.custom" ]; then
    say "[PG] Logical dump restore (pg_restore --clean --if-exists, db=$PG_DB)..."
    # PG must be up for pg_restore. If it isn't up, start it.
    docker start "$PG_NAME" >/dev/null 2>&1 || true
    # Wait for PG ready.
    local i=0
    until docker exec "$PG_NAME" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; do
      sleep 1; i=$((i+1)); [ "$i" -ge 30 ] && { err "PG did not become ready (30s)."; exit 1; }
    done
    if docker exec -i "$PG_NAME" pg_restore -U "$PG_USER" -d "$PG_DB" \
          --clean --if-exists --no-owner --no-acl < "$P/pg_dump.custom" \
          >"$STAGE/pg_restore.log" 2>&1; then
      ok "PG logical restore complete."
    else
      # pg_restore may exit non-zero on --clean due to DROP warnings for non-existent objects → judge by the log.
      if grep -qiE 'error|fatal' "$STAGE/pg_restore.log"; then
        warn "PG logical restore had warnings/errors (many are harmless DROP warnings). Last detail lines:"
        tail -3 "$STAGE/pg_restore.log" >&2 || true
      else
        ok "PG logical restore complete (warnings only)."
      fi
    fi
  elif [ -f "$P/pg_volume.tar.gz" ]; then
    say "[PG] Physical volume restore (no logical dump → safety-net path)..."
    docker stop "$PG_NAME" >/dev/null 2>&1 || true
    docker run --rm --volumes-from "$PG_NAME" -v "$P":/backup alpine \
      sh -c 'cd /var/lib/postgresql/data && rm -rf ./* ./.[!.]* 2>/dev/null; tar xzf /backup/pg_volume.tar.gz' \
      >/dev/null 2>"$STAGE/pgvol.err" \
      && ok "PG physical restore complete." \
      || { err "PG physical restore failed: $(tail -1 "$STAGE/pgvol.err" 2>/dev/null)"; exit 1; }
    docker start "$PG_NAME" >/dev/null 2>&1 || true
  else
    err "The backup has no PG data (logical/physical) — cannot restore."
    exit 1
  fi

  # ── MM volume restore (data/config/plugins) ────────────────────────────────
  for spec in "data:/mattermost/data" "config:/mattermost/config" "plugins:/mattermost/plugins"; do
    local label="${spec%%:*}" path="${spec#*:}"
    local tarf="$P/mm_${label}.tar.gz"
    [ -f "$tarf" ] || { say "[MM] No $label backup → skipping."; continue; }
    say "[MM] Restoring $label volume..."
    # Overwrite on top of the empty (newly created) volume. Idempotent: reapplying the same content is safe too.
    docker run --rm --volumes-from "$MM_NAME" -v "$P":/backup alpine \
      sh -c "mkdir -p '$path' && cd '$path' && tar xzf /backup/mm_${label}.tar.gz" \
      >/dev/null 2>>"$STAGE/mm.err" \
      && ok "[MM] $label restore complete." \
      || warn "[MM] $label restore failed: $(tail -1 "$STAGE/mm.err" 2>/dev/null)"
  done

  # ── Restart MM ────────────────────────────────────────────────────────
  say "Restarting Mattermost..."
  docker start "$MM_NAME" >/dev/null 2>&1 || true

  # ── Restore-complete marker (refresh idempotency guard) ──────────────────────────────
  {
    echo "restored_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "backup_file=$(basename "$BACKUP")"
    echo "fingerprint=$fp"
  } > "$MARKER"

  ok "Data restore complete. The old PC's chats/accounts/channels/reports have been migrated to this PC."
  say "MM may take tens of seconds to become healthy (the bot connects automatically afterward)."
}

main "$@"
