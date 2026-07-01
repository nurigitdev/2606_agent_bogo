#!/bin/zsh
# ════════════════════════════════════════════════════════════════════════
#  BOGO Data Backup (optional, manual, on-demand) -- usually no need to run this
# ════════════════════════════════════════════════════════════════════════
#  Note: you normally do not need to run this. While the bots operate, launchd/systemd
#    periodically (default 6h) auto-backs up the data inside the folder (app/migration/), and
#    also backs up once on a clean shutdown. So the only button a normal user presses is the
#    single 'BOGO_start.command' on the new PC.
#
#  Use this file only when you want to force a snapshot of the current state right now.
#  WHAT  Double-clicking runs app/migration/bogo_backup.sh once immediately, producing a
#    Mattermost conversation/account/channel/report (Postgres + MM volume) backup at
#    app/migration/bogo_backup_latest.tar.gz (read-only, source untouched; old backups auto-pruned).
#  Path-safe: resolves its own location dynamically, so it works under spaced/Unicode paths.
# ════════════════════════════════════════════════════════════════════════
set -u

SELF_DIR="${0:A:h}"
BACKUP="$SELF_DIR/../app/migration/bogo_backup.sh"

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[BOGO]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
ok()   { printf "%s[OK]%s %s\n"    "$C_OK"   "$C_RST" "$*"; }
fail() { printf "%s[ERROR]%s %s\n" "$C_ERR"  "$C_RST" "$*"; }

pause_exit() {
  print -r -- ""
  print -r -- "──────────────────────────────────────────────"
  print -r -- "Press Enter or any key to close this window."
  read -k1 -s 2>/dev/null || true
  exit "${1:-0}"
}

print -r -- ""
say "Starting the BOGO data backup"
say "Location: $SELF_DIR"
print -r -- ""

if [[ ! -f "$BACKUP" ]]; then
  fail "Backup script not found: $BACKUP"
  fail "This .command file must sit in the project root that contains the 'app' folder."
  pause_exit 1
fi
chmod +x "$BACKUP" 2>/dev/null || true

"$BACKUP"
rc=$?

print -r -- ""
if [[ $rc -eq 0 ]]; then
  ok "Backup complete. Move the entire 'agent-bogo' folder to the new PC, then double-click 'BOGO Start'."
  say "Backup location:  ../app/migration/bogo_backup_latest.tar.gz"
else
  fail "A problem occurred during backup. Check the log above."
  say "Common cause: Docker/Colima not running -> run 'colima start' in a terminal, then retry."
fi

pause_exit $rc
