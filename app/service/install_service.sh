#!/usr/bin/env bash
# BOGO always-on service installer (macOS launchd / Linux systemd --user).
#
# OS-detected, username-agnostic (everything derived from ${HOME} and this repo's
# resolved path). No /Users/<name> is ever hardcoded.
#
#   macOS : mirrors the repo to an ASCII path ${HOME}/.bogo-bin/app (REQUIRED --
#           launchd corrupts Unicode paths and TCC blocks ~/Desktop reads), installs
#           4 launchd user agents from the plist template.
#   Linux : installs a systemd --user template unit and enables 4 instances. Runs
#           the repo IN PLACE (Linux handles Unicode paths; no mirror needed).
#
# Usage:
#   ./service/install_service.sh install     # install + start all roles
#   ./service/install_service.sh uninstall   # stop + remove service registration
#   ./service/install_service.sh restart     # redeploy code + restart all roles
#   ./service/install_service.sh status      # show running state
set -euo pipefail

ROLES=(orchestrator hr dev admin)

# CEO dashboard listen port (loopback only). Overridable via environment variable, default 8642.
DASH_PORT="${BOGO_DASHBOARD_PORT:-8642}"

# Automatic data backup interval (seconds) and retention count. While the bots run, periodically
# accumulate the latest backups inside the folder, eliminating the 'double-click backup' step
# (copying the folder carries the backups along → auto-restore in one start on a new PC).
# Default 6 hours, retain 3 (up to 18 hours of history). Adjustable via environment variables.
BACKUP_INTERVAL="${BOGO_BACKUP_INTERVAL:-21600}"
BACKUP_RETAIN="${BOGO_BACKUP_RETAIN:-3}"

# Repo root = app/ (this script lives in app/service/).
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SELF/.." && pwd)"
TPL="$SELF/templates"

say() { printf '\033[0;36m[service]\033[0m %s\n' "$*"; }
err() { printf '\033[0;31m[service:ERROR]\033[0m %s\n' "$*" >&2; }
sed_replacement() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/[&#]/\\&/g'
}

OS="$(uname -s)"
ACTION="${1:-install}"

# ════════════════════════════════════════════════════════════════════════
# macOS — launchd + ASCII mirror
# ════════════════════════════════════════════════════════════════════════
mac_app="${HOME}/.bogo-bin/app"
mac_launcher="${HOME}/.bogo-bin/run_role.sh"
mac_logs="${mac_app}/logs"
mac_la="${HOME}/Library/LaunchAgents"

# Enable the git post-commit hook — the key to eliminating mirror drift at the source.
# The launchd daemon can't read the ~/Desktop original due to TCC, so it can't self-sync. Instead,
# at commit time (= a user session, which passes TCC) .githooks/post-commit automatically
# re-syncs the mirror + restarts the daemon. By pointing core.hooksPath at the version-controlled
# .githooks, it auto-enables with a single install on a new clone/machine (.git/hooks isn't version-controlled).
mac_enable_git_hooks() {
  local groot; groot="$(cd "$REPO/.." && git rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$groot" ] || { say "Not a git worktree → skipping post-commit hook registration"; return 0; }
  if [ -f "$groot/.githooks/post-commit" ]; then
    chmod +x "$groot/.githooks/post-commit" 2>/dev/null || true
    ( cd "$groot" && git config core.hooksPath .githooks )
    say "git post-commit hook enabled: auto-sync mirror + restart daemon on commit"
  fi
}

mac_sync() {
  mkdir -p "$mac_app" "$mac_logs"
  rsync -a \
    --exclude '__pycache__/' \
    --exclude '.ruff_cache/' \
    --exclude '.pytest_cache/' \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '.env' \
    --exclude '*.bak' \
    --exclude 'logs/' \
    "$REPO"/ "$mac_app"/
  # The mirror is an ASCII path → bootstrap the venv THERE (its own pyvenv pins
  # the ASCII path, which is what we want for launchd).
  if [ ! -x "$mac_app/.venv/bin/python" ]; then
    say "Creating venv in the ASCII mirror..."
    ( cd "$mac_app" && ./bootstrap.sh >/dev/null )
  fi
  say "Mirror sync: $REPO -> $mac_app"
}

# Register the Colima boot auto-start LaunchAgent (idempotent). Auto-starts the Docker runtime VM
# on macOS login/boot so the communication-backbone containers the bots depend on revive under the
# unless-stopped policy.
mac_install_colima_agent() {
  local colima_bin; colima_bin="$(command -v colima 2>/dev/null || true)"
  if [ -z "$colima_bin" ]; then
    say "colima not installed → skipping Colima boot auto-start registration (bot infra is guaranteed by infra_up.sh)."
    return 0
  fi
  local brew_bin; brew_bin="$(dirname "$colima_bin")"
  local plist="$mac_la/com.bogo.colima.plist"
  sed -e "s#__COLIMA__#$colima_bin#g" \
      -e "s#__BREW_BIN__#$brew_bin#g" \
      -e "s#__HOME__#$HOME#g" \
      -e "s#__LOGS__#$mac_logs#g" \
      "$TPL/com.bogo.colima.plist.template" > "$plist"
  local uid; uid="$(id -u)"
  launchctl bootout "gui/$uid/com.bogo.colima" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$uid" "$plist"
  say "Registered: com.bogo.colima (auto-starts Colima on boot)"
}

# Safe (re)registration: if you bootstrap while the same Label has not been fully booted out,
# launchd throws "Input/output error (5)" and set -e aborts the whole install.
# (A race where the KeepAlive bot restarts immediately and the label stays briefly alive.) → After
# bootout, poll briefly until the label disappears, and if it still fails, retry once. $1=label, $2=plist path.
mac_bootstrap_safe() {
  local uid; uid="$(id -u)"
  local label="$1" plist="$2"
  launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
  # Wait up to ~5s until the label drops from the service DB (guarantees a full unload).
  local i=0
  while [ "$i" -lt 25 ] && launchctl print "gui/$uid/$label" >/dev/null 2>&1; do
    sleep 0.2; i=$((i + 1))
  done
  if ! launchctl bootstrap "gui/$uid" "$plist" 2>/dev/null; then
    sleep 1
    launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
    sleep 1
    launchctl bootstrap "gui/$uid" "$plist"   # a second failure is a real error → aborts via set -e
  fi
}

# Register the automatic data backup LaunchAgent (idempotent). While the bots run, every
# BACKUP_INTERVAL it accumulates a read-only pg_dump + MM volume backup into the 'original repo's'
# migration/ (__REPO__ = the folder-copy target). No one needs to press backup — just copy the
# folder to a new PC and the latest backup comes along.
mac_install_backup_agent() {
  local plist="$mac_la/com.bogo.backup.plist"
  sed -e "s#__APP__#$mac_app#g" \
      -e "s#__REPO__#$REPO#g" \
      -e "s#__LOGS__#$mac_logs#g" \
      -e "s#__INTERVAL__#$BACKUP_INTERVAL#g" \
      -e "s#__RETAIN__#$BACKUP_RETAIN#g" \
      "$TPL/com.bogo.backup.plist.template" > "$plist"
  mac_bootstrap_safe "com.bogo.backup" "$plist"
  say "Registered+started: com.bogo.backup (auto-backup every ${BACKUP_INTERVAL}s → $REPO/migration)"
}

mac_install() {
  # Place an ASCII-path launcher that launchd calls (run_role.sh from the mirror).
  mkdir -p "${HOME}/.bogo-bin" "$mac_la"
  cp "$REPO/run_role.sh" "$mac_launcher"
  chmod +x "$mac_launcher"
  mac_sync
  mac_enable_git_hooks   # auto-sync mirror on commit (eliminate drift at the source)
  # Register Colima boot auto-start + guarantee the communication backbone right now (MM must be up before bot registration to avoid instant death).
  mac_install_colima_agent
  "$mac_app/infra_up.sh"
  local uid; uid="$(id -u)"
  for r in "${ROLES[@]}"; do
    local plist="$mac_la/com.bogo.$r.plist"
    sed -e "s#__ROLE__#$r#g" \
        -e "s#__LAUNCHER__#$mac_launcher#g" \
        -e "s#__APP__#$mac_app#g" \
        -e "s#__REPO__#$REPO#g" \
        -e "s#__LOGS__#$mac_logs#g" \
        "$TPL/com.bogo.ROLE.plist.template" > "$plist"
    mac_bootstrap_safe "com.bogo.$r" "$plist"
    say "Registered+started: com.bogo.$r"
  done
  # Promote the CEO dashboard (127.0.0.1:DASH_PORT) to permanent launchd ownership, same as the bots.
  # If an existing oneclick nohup one-off process is up it would conflict on a duplicate LISTEN, so clean it up first.
  mac_kill_legacy_dashboard
  local dplist="$mac_la/com.bogo.dashboard.plist"
  sed -e "s#__LAUNCHER__#$mac_launcher#g" \
      -e "s#__APP__#$mac_app#g" \
      -e "s#__REPO__#$REPO#g" \
      -e "s#__DASH_PORT__#$DASH_PORT#g" \
      -e "s#__LOGS__#$mac_logs#g" \
      "$TPL/com.bogo.dashboard.plist.template" > "$dplist"
  mac_bootstrap_safe "com.bogo.dashboard" "$dplist"
  say "registered+started: com.bogo.dashboard (127.0.0.1:$DASH_PORT)"
  # Register the automatic data-backup job (accumulates the latest backup inside the folder while the bots run -> unattended migration complete).
  mac_install_backup_agent
  say "macOS launchd install complete. Status:  ./service/install_service.sh status"
}

# Before launchd owns the dashboard, safely stop the one-shot nohup dashboard started by oneclick
# (original Desktop path or the mirror). Kill only our own ceo_dashboard.py process (prevents port
# contention / double LISTEN). Do not touch external processes.
mac_kill_legacy_dashboard() {
  local holders; holders="$(lsof -nP -iTCP:"$DASH_PORT" -sTCP:LISTEN -t 2>/dev/null | sort -u || true)"
  for p in $holders; do
    if ps -p "$p" -o command= 2>/dev/null | grep -q "ceo_dashboard.py"; then
      kill "$p" 2>/dev/null || true; sleep 1; kill -9 "$p" 2>/dev/null || true
      say "cleaned up legacy nohup dashboard (PID $p) -> handing ownership to launchd."
    fi
  done
  rm -f "$mac_app/logs/dashboard.pid" "$REPO/logs/dashboard.pid" 2>/dev/null || true
}

mac_uninstall() {
  local uid; uid="$(id -u)"
  for r in "${ROLES[@]}"; do
    launchctl bootout "gui/$uid/com.bogo.$r" >/dev/null 2>&1 || true
    rm -f "$mac_la/com.bogo.$r.plist"
    say "unregistered: com.bogo.$r"
  done
  # Unregister the CEO dashboard from launchd (mirror and logs are preserved).
  launchctl bootout "gui/$uid/com.bogo.dashboard" >/dev/null 2>&1 || true
  rm -f "$mac_la/com.bogo.dashboard.plist"
  say "unregistered: com.bogo.dashboard"
  # Unregister the automatic data-backup LaunchAgent (already-produced backup artifacts are preserved — for data migration).
  launchctl bootout "gui/$uid/com.bogo.backup" >/dev/null 2>&1 || true
  rm -f "$mac_la/com.bogo.backup.plist"
  say "unregistered: com.bogo.backup"
  # Also unregister the Colima boot auto-start LaunchAgent (the Colima VM itself is left untouched).
  launchctl bootout "gui/$uid/com.bogo.colima" >/dev/null 2>&1 || true
  rm -f "$mac_la/com.bogo.colima.plist"
  say "unregistered: com.bogo.colima"
  say "launchd unregistration complete. (mirror $mac_app is preserved — delete manually if desired)"
}

mac_restart() {
  cp "$REPO/run_role.sh" "$mac_launcher"; chmod +x "$mac_launcher"
  mac_sync
  mac_enable_git_hooks   # idempotent: keep the hook active on restart too
  # Guarantee the communication backbone before restart too (if Colima/containers are down, the bots die instantly again).
  "$mac_app/infra_up.sh"
  local uid; uid="$(id -u)"
  for r in "${ROLES[@]}"; do
    launchctl kickstart -k "gui/$uid/com.bogo.$r" && say "restarted: com.bogo.$r"
  done
  # The dashboard may not be registered yet (if brought up from an older version) -> register if absent, restart if present.
  if launchctl print "gui/$uid/com.bogo.dashboard" >/dev/null 2>&1; then
    launchctl kickstart -k "gui/$uid/com.bogo.dashboard" && say "restarted: com.bogo.dashboard"
  else
    mac_kill_legacy_dashboard
    local dplist="$mac_la/com.bogo.dashboard.plist"
    sed -e "s#__LAUNCHER__#$mac_launcher#g" \
        -e "s#__APP__#$mac_app#g" \
        -e "s#__REPO__#$REPO#g" \
        -e "s#__DASH_PORT__#$DASH_PORT#g" \
        -e "s#__LOGS__#$mac_logs#g" \
        "$TPL/com.bogo.dashboard.plist.template" > "$dplist"
    mac_bootstrap_safe "com.bogo.dashboard" "$dplist"
    say "registered+started: com.bogo.dashboard (127.0.0.1:$DASH_PORT)"
  fi
  # (Re)register the automatic backup job (idempotent) — guarantees the backup job even on environments brought up from an older version.
  mac_install_backup_agent
}

# Mirror-sync drift detection: warn when core code files diverge between the original (REPO) and
# the ASCII mirror (mac_app). Because the launchd daemon runs the mirror copy, fixing only the
# original without a restart leaves the mirror stale, silently causing "dashboard/bots run old code"
# incidents (e.g. if mm_client's MM_BASE localhost->127.0.0.1 fix is not reflected, the Mattermost
# connection fails with an ::1 refusal). Surface this drift immediately at the status step.
mac_check_mirror_sync() {
  [ -d "$mac_app" ] || { say "no mirror (not installed yet): $mac_app"; return 0; }
  local drift=0 f
  for f in ceo_dashboard.py mm_client.py agent_schema.py bogo_runtime.py \
           ceo_admin_runtime.py teams.json channels.json; do
    [ -f "$REPO/$f" ] || continue
    if [ ! -f "$mac_app/$f" ] || ! cmp -s "$REPO/$f" "$mac_app/$f"; then
      printf '\033[0;33m[service]\033[0m   \xe2\x9a\xa0 mirror mismatch: %s\n' "$f"
      drift=1
    fi
  done
  if [ "$drift" -eq 1 ]; then
    printf '\033[0;33m[service]\033[0m The mirror has diverged from the original -> the daemon is running old code.\n'
    printf '\033[0;33m[service]\033[0m Recovery: ./service/install_service.sh restart\n'
  else
    say "mirror sync OK (original <-> $mac_app core files match)"
  fi
}

mac_status() {
  launchctl list | grep bogo || say "(no com.bogo.* running)"
  mac_check_mirror_sync
}

# ════════════════════════════════════════════════════════════════════════
# Linux — systemd --user (in-place, Hangul-safe)
# ════════════════════════════════════════════════════════════════════════
sd_dir="${HOME}/.config/systemd/user"
sd_unit="$sd_dir/bogo@.service"

linux_require_systemd_user() {
  command -v systemctl >/dev/null 2>&1 || { err "systemctl not found — this is not a systemd environment."; exit 4; }
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    err "systemd --user is not reachable for this login session."
    err "One thing to do: run from a normal Linux desktop/login session with user systemd enabled, then retry."
    err "Diagnostics: systemctl --user status ; loginctl user-status \"$(id -un)\""
    exit 4
  fi
}

linux_install() {
  linux_require_systemd_user
  chmod +x "$REPO/run_role.sh"
  mkdir -p "$sd_dir"
  local repo_sed; repo_sed="$(sed_replacement "$REPO")"
  sed -e "s#__WORKDIR__#$repo_sed#g" "$TPL/bogo@.service.template" > "$sd_unit"
  systemctl --user daemon-reload
  # Lingering so user services survive logout / run at boot.
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || \
    say "note: run 'sudo loginctl enable-linger $(id -un)' to keep it running after logout."
  for r in "${ROLES[@]}"; do
    systemctl --user enable --now "bogo@$r.service"
    say "registered+started: bogo@$r"
  done
  # Keep the CEO dashboard (127.0.0.1:DASH_PORT) always-on via the same template instance. run_role.sh
  # takes a 'dashboard' argument and execs ceo_dashboard.py; BOGO_DASHBOARD_PORT defaults to 8642.
  systemctl --user enable --now "bogo@dashboard.service"
  say "registered+started: bogo@dashboard (127.0.0.1:$DASH_PORT)"
  # Register the automatic data-backup timer (accumulates the latest backup inside the folder while the bots run -> unattended migration complete).
  linux_install_backup_timer
  say "Linux systemd install complete. Logs:  journalctl --user -u bogo@orchestrator -f"
}

# Register a periodic backup via a systemd user timer (idempotent). Generate the service+timer units by substitution, then enable.
linux_install_backup_timer() {
  local repo_sed retain_sed interval_sed
  repo_sed="$(sed_replacement "$REPO")"
  retain_sed="$(sed_replacement "$BACKUP_RETAIN")"
  interval_sed="$(sed_replacement "$BACKUP_INTERVAL")"
  sed -e "s#__WORKDIR__#$repo_sed#g" -e "s#__RETAIN__#$retain_sed#g" \
      "$TPL/bogo-backup.service.template" > "$sd_dir/bogo-backup.service"
  sed -e "s#__INTERVAL_SEC__#$interval_sed#g" \
      "$TPL/bogo-backup.timer.template" > "$sd_dir/bogo-backup.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now "bogo-backup.timer"
  say "registered+started: bogo-backup.timer (auto-backup every ${BACKUP_INTERVAL}s -> $REPO/migration)"
}

linux_uninstall() {
  linux_require_systemd_user
  for r in "${ROLES[@]}"; do
    systemctl --user disable --now "bogo@$r.service" >/dev/null 2>&1 || true
    say "unregistered: bogo@$r"
  done
  systemctl --user disable --now "bogo@dashboard.service" >/dev/null 2>&1 || true
  say "unregistered: bogo@dashboard"
  # Unregister the automatic backup timer (already-produced backup artifacts are preserved — for data migration).
  systemctl --user disable --now "bogo-backup.timer" >/dev/null 2>&1 || true
  rm -f "$sd_dir/bogo-backup.timer" "$sd_dir/bogo-backup.service"
  say "unregistered: bogo-backup.timer"
  rm -f "$sd_unit"
  systemctl --user daemon-reload || true
  say "systemd unregistration complete."
}

linux_restart() {
  linux_require_systemd_user
  chmod +x "$REPO/run_role.sh"
  for r in "${ROLES[@]}"; do
    systemctl --user restart "bogo@$r.service" && say "restarted: bogo@$r"
  done
  # If the dashboard instance is not enabled yet (older version), register it too; if present, restart.
  systemctl --user enable --now "bogo@dashboard.service" 2>/dev/null || true
  systemctl --user restart "bogo@dashboard.service" && say "restarted: bogo@dashboard"
  # (Re)register the automatic backup timer (idempotent) — guarantees the backup timer even on older environments.
  linux_install_backup_timer
}

linux_status() {
  linux_require_systemd_user
  for r in "${ROLES[@]}"; do
    printf '%-14s ' "bogo@$r"
    systemctl --user is-active "bogo@$r.service" 2>/dev/null || true
  done
  printf '%-14s ' "bogo@dashboard"
  systemctl --user is-active "bogo@dashboard.service" 2>/dev/null || true
}

# ════════════════════════════════════════════════════════════════════════
# Dispatch
# ════════════════════════════════════════════════════════════════════════
case "$OS" in
  Darwin) fn="mac" ;;
  Linux)  fn="linux" ;;
  *) err "unsupported OS: $OS (use install_service.ps1 on Windows)"; exit 1 ;;
esac

case "$ACTION" in
  install)   "${fn}_install" ;;
  uninstall) "${fn}_uninstall" ;;
  restart)   "${fn}_restart" ;;
  status)    "${fn}_status" ;;
  *) err "unknown command: $ACTION (install|uninstall|restart|status)"; exit 1 ;;
esac
