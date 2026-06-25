#!/usr/bin/env bash
# Hermes always-on service installer (macOS launchd / Linux systemd --user).
#
# OS-detected, username-agnostic (everything derived from ${HOME} and this repo's
# resolved path). No /Users/<name> is ever hardcoded.
#
#   macOS : mirrors the repo to an ASCII path ${HOME}/.hermes-bin/app (REQUIRED —
#           launchd corrupts Hangul paths and TCC blocks ~/Desktop reads), installs
#           4 launchd user agents from the plist template.
#   Linux : installs a systemd --user template unit and enables 4 instances. Runs
#           the repo IN PLACE (Linux handles Hangul paths; no mirror needed).
#
# Usage:
#   ./service/install_service.sh install     # install + start all roles
#   ./service/install_service.sh uninstall   # stop + remove service registration
#   ./service/install_service.sh restart     # redeploy code + restart all roles
#   ./service/install_service.sh status      # show running state
set -euo pipefail

ROLES=(orchestrator hr dev admin)

# Repo root = app/ (this script lives in app/service/).
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SELF/.." && pwd)"
TPL="$SELF/templates"

say() { printf '\033[0;36m[service]\033[0m %s\n' "$*"; }
err() { printf '\033[0;31m[service:오류]\033[0m %s\n' "$*" >&2; }

OS="$(uname -s)"
ACTION="${1:-install}"

# ════════════════════════════════════════════════════════════════════════
# macOS — launchd + ASCII mirror
# ════════════════════════════════════════════════════════════════════════
mac_app="${HOME}/.hermes-bin/app"
mac_launcher="${HOME}/.hermes-bin/run_role.sh"
mac_logs="${mac_app}/logs"
mac_la="${HOME}/Library/LaunchAgents"

mac_sync() {
  mkdir -p "$mac_app" "$mac_logs"
  rsync -a \
    --exclude '__pycache__/' \
    --exclude '.ruff_cache/' \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '*.bak' \
    --exclude 'logs/' \
    "$REPO"/ "$mac_app"/
  # The mirror is an ASCII path → bootstrap the venv THERE (its own pyvenv pins
  # the ASCII path, which is what we want for launchd).
  if [ ! -x "$mac_app/.venv/bin/python" ]; then
    say "ASCII 미러에 venv 생성 중..."
    ( cd "$mac_app" && ./bootstrap.sh >/dev/null )
  fi
  say "미러 동기화: $REPO -> $mac_app"
}

# Colima 부팅 자동시작 LaunchAgent 등록(멱등). macOS 로그인/부팅 시 도커 런타임 VM 을
# 자동 기동해, 봇이 의존하는 통신 백본 컨테이너가 unless-stopped 정책으로 부활하게 한다.
mac_install_colima_agent() {
  local colima_bin; colima_bin="$(command -v colima 2>/dev/null || true)"
  if [ -z "$colima_bin" ]; then
    say "colima 미설치 → Colima 부팅 자동시작 등록 생략(봇 인프라는 infra_up.sh 가 보장)."
    return 0
  fi
  local brew_bin; brew_bin="$(dirname "$colima_bin")"
  local plist="$mac_la/com.hermes.colima.plist"
  sed -e "s#__COLIMA__#$colima_bin#g" \
      -e "s#__BREW_BIN__#$brew_bin#g" \
      -e "s#__HOME__#$HOME#g" \
      -e "s#__LOGS__#$mac_logs#g" \
      "$TPL/com.hermes.colima.plist.template" > "$plist"
  local uid; uid="$(id -u)"
  launchctl bootout "gui/$uid/com.hermes.colima" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$uid" "$plist"
  say "등록: com.hermes.colima (부팅 시 Colima 자동 기동)"
}

mac_install() {
  # Place an ASCII-path launcher that launchd calls (run_role.sh from the mirror).
  mkdir -p "${HOME}/.hermes-bin" "$mac_la"
  cp "$REPO/run_role.sh" "$mac_launcher"
  chmod +x "$mac_launcher"
  mac_sync
  # Colima 부팅 자동시작 등록 + 지금 당장 통신 백본 보장(봇 등록 전에 MM 이 떠 있어야 즉사 없음).
  mac_install_colima_agent
  "$mac_app/infra_up.sh"
  local uid; uid="$(id -u)"
  for r in "${ROLES[@]}"; do
    local plist="$mac_la/com.hermes.$r.plist"
    sed -e "s#__ROLE__#$r#g" \
        -e "s#__LAUNCHER__#$mac_launcher#g" \
        -e "s#__APP__#$mac_app#g" \
        -e "s#__REPO__#$REPO#g" \
        -e "s#__LOGS__#$mac_logs#g" \
        "$TPL/com.hermes.ROLE.plist.template" > "$plist"
    launchctl bootout "gui/$uid/com.hermes.$r" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$uid" "$plist"
    say "등록+기동: com.hermes.$r"
  done
  say "macOS launchd 설치 완료. 상태:  ./service/install_service.sh status"
}

mac_uninstall() {
  local uid; uid="$(id -u)"
  for r in "${ROLES[@]}"; do
    launchctl bootout "gui/$uid/com.hermes.$r" >/dev/null 2>&1 || true
    rm -f "$mac_la/com.hermes.$r.plist"
    say "해제: com.hermes.$r"
  done
  # Colima 부팅 자동시작 LaunchAgent 도 함께 해제(콜리마 VM 자체는 건드리지 않음).
  launchctl bootout "gui/$uid/com.hermes.colima" >/dev/null 2>&1 || true
  rm -f "$mac_la/com.hermes.colima.plist"
  say "해제: com.hermes.colima"
  say "launchd 등록 해제 완료. (미러 $mac_app 는 보존 — 수동 삭제 가능)"
}

mac_restart() {
  cp "$REPO/run_role.sh" "$mac_launcher"; chmod +x "$mac_launcher"
  mac_sync
  # 재시작 전에도 통신 백본을 보장한다(Colima/컨테이너가 내려가 있으면 봇이 또 즉사하므로).
  "$mac_app/infra_up.sh"
  local uid; uid="$(id -u)"
  for r in "${ROLES[@]}"; do
    launchctl kickstart -k "gui/$uid/com.hermes.$r" && say "재시작: com.hermes.$r"
  done
}

mac_status() {
  launchctl list | grep hermes || say "(실행 중인 com.hermes.* 없음)"
}

# ════════════════════════════════════════════════════════════════════════
# Linux — systemd --user (in-place, Hangul-safe)
# ════════════════════════════════════════════════════════════════════════
sd_dir="${HOME}/.config/systemd/user"
sd_unit="$sd_dir/hermes@.service"

linux_install() {
  command -v systemctl >/dev/null 2>&1 || { err "systemctl 미발견 — systemd 환경이 아닙니다."; exit 1; }
  chmod +x "$REPO/run_role.sh"
  mkdir -p "$sd_dir"
  sed -e "s#__WORKDIR__#$REPO#g" "$TPL/hermes@.service.template" > "$sd_unit"
  systemctl --user daemon-reload
  # Lingering so user services survive logout / run at boot.
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || \
    say "참고: 'sudo loginctl enable-linger $(id -un)' 를 실행하면 로그아웃 후에도 유지됩니다."
  for r in "${ROLES[@]}"; do
    systemctl --user enable --now "hermes@$r.service"
    say "등록+기동: hermes@$r"
  done
  say "Linux systemd 설치 완료. 로그:  journalctl --user -u hermes@orchestrator -f"
}

linux_uninstall() {
  for r in "${ROLES[@]}"; do
    systemctl --user disable --now "hermes@$r.service" >/dev/null 2>&1 || true
    say "해제: hermes@$r"
  done
  rm -f "$sd_unit"
  systemctl --user daemon-reload || true
  say "systemd 등록 해제 완료."
}

linux_restart() {
  chmod +x "$REPO/run_role.sh"
  for r in "${ROLES[@]}"; do
    systemctl --user restart "hermes@$r.service" && say "재시작: hermes@$r"
  done
}

linux_status() {
  for r in "${ROLES[@]}"; do
    printf '%-14s ' "hermes@$r"
    systemctl --user is-active "hermes@$r.service" 2>/dev/null || true
  done
}

# ════════════════════════════════════════════════════════════════════════
# Dispatch
# ════════════════════════════════════════════════════════════════════════
case "$OS" in
  Darwin) fn="mac" ;;
  Linux)  fn="linux" ;;
  *) err "지원하지 않는 OS: $OS (Windows 는 install_service.ps1 사용)"; exit 1 ;;
esac

case "$ACTION" in
  install)   "${fn}_install" ;;
  uninstall) "${fn}_uninstall" ;;
  restart)   "${fn}_restart" ;;
  status)    "${fn}_status" ;;
  *) err "알 수 없는 명령: $ACTION (install|uninstall|restart|status)"; exit 1 ;;
esac
