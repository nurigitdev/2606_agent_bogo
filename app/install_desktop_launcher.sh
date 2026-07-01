#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  Linux GUI launcher install (recommended entry point) -- self-healing
# ════════════════════════════════════════════════════════════════════════
#  WHAT  Substitutes __START_SH__ in 'launchers/BOGO_start.desktop.template' with the real absolute
#    path and installs a .desktop into ~/.local/share/applications, so clicking the 'BOGO Start'
#    icon from the app menu / file manager launches the full core (bogo_oneclick.sh start).
#
#  Root-cause self-heal (removes the common reasons a non-developer cannot launch with one double-click):
#    (a) .sh execute bit lost on git clone/copy -> chmod +x all relevant .sh automatically
#    (b) .desktop not marked trusted -> gio set metadata::trusted true (when possible) + chmod +x
#    (c) app-menu cache not refreshed -> update-desktop-database
#    (d) log visibility -> keep template Terminal=true (install/start logs are shown to the user)
#
#  macOS uses .command, so this script is Linux-only -- other OSes only print a notice and exit
#    (no regression, idempotent). Safe to re-run (overwrites with the same result).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"   # project root (= parent of app)
START_SH="$ROOT/launchers/BOGO_start.sh"
TPL="$ROOT/launchers/BOGO_start.desktop.template"

say() { printf '\033[0;36m[desktop]\033[0m %s\n' "$*"; }
err() { printf '\033[0;31m[desktop:ERROR]\033[0m %s\n' "$*" >&2; }

if [ "$(uname -s)" != "Linux" ]; then
  say "Not Linux ($(uname -s)) -> on macOS use the 'BOGO_start.command' double-click. Skipping install."
  exit 0
fi
[ -f "$TPL" ]      || { err "Template not found: $TPL"; exit 1; }
[ -f "$START_SH" ] || { err "Launcher not found: $START_SH"; exit 1; }

# ── (a) Execute-permission self-heal -- restore the +x bit lost on clone/copy ─────────────
# Guarantee +x on the double-click entry points plus all the core .sh files they call.
heal_chmod() {
  local f
  for f in \
    "$START_SH" \
    "$ROOT/launchers/BOGO_stop.sh" \
    "$HERE/start_linux.sh" \
    "$HERE/bogo_oneclick.sh" \
    "$HERE/bogo_ctl.sh" \
    "$HERE/bootstrap.sh" \
    "$HERE/infra_up.sh" \
    "$HERE/run_role.sh" \
    "$HERE/migration/bogo_restore.sh" \
    "$HERE/service/install_service.sh"
  do
    [ -f "$f" ] && chmod +x "$f" 2>/dev/null || true
  done
}
heal_chmod
say "Execute-permission (+x) self-heal complete -- set the execute bit on core .sh files (in case it was lost on clone)."

# ── (b) Install the .desktop + substitute the absolute path ────────────────────────────
dest_dir="${HOME}/.local/share/applications"
mkdir -p "$dest_dir"
dest="$dest_dir/bogo-start.desktop"
# Substitute the absolute path (space/Unicode safe: the quotes in Exec are already in the template). Use '#' as the sed delimiter.
# START_SH is unlikely to contain '#', but as a precaution keep '#' (paths normally have no '#').
sed -e "s#__START_SH__#${START_SH}#g" "$TPL" > "$dest"
chmod +x "$dest" 2>/dev/null || true

# ── (c) Trusted flag (where the environment supports it) ─────────────────────────────────
# GNOME (Nautilus) needs metadata::trusted so a double-click becomes 'run' rather than 'edit'.
if command -v gio >/dev/null 2>&1; then
  gio set "$dest" "metadata::trusted" true >/dev/null 2>&1 \
    && say "Trusted flag set (gio metadata::trusted)." \
    || say "Attempted to set the trusted flag (some environments ignore it) -- menu launch still works."
fi

# ── (d) Refresh the app-menu cache ───────────────────────────────────────────────────────
command -v update-desktop-database >/dev/null 2>&1 \
  && update-desktop-database "$dest_dir" >/dev/null 2>&1 \
  && say "App-menu cache refreshed (update-desktop-database)." || true

say "Install complete: $dest"
say "You can launch it by clicking the 'BOGO Start' icon in the app menu / file manager (full core, terminal logs shown)."
say "If it does not appear, log out and back in once to refresh the menu cache."
