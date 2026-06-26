#!/usr/bin/env bash
# BOGO always-on service installer (macOS launchd / Linux systemd --user).
#
# OS-detected, username-agnostic (everything derived from ${HOME} and this repo's
# resolved path). No /Users/<name> is ever hardcoded.
#
#   macOS : mirrors the repo to an ASCII path ${HOME}/.bogo-bin/app (REQUIRED —
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

# CEO 대시보드 리슨 포트(루프백 전용). 환경변수로 덮어쓰기 가능, 기본 8642.
DASH_PORT="${BOGO_DASHBOARD_PORT:-8642}"

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
mac_app="${HOME}/.bogo-bin/app"
mac_launcher="${HOME}/.bogo-bin/run_role.sh"
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
  local plist="$mac_la/com.bogo.colima.plist"
  sed -e "s#__COLIMA__#$colima_bin#g" \
      -e "s#__BREW_BIN__#$brew_bin#g" \
      -e "s#__HOME__#$HOME#g" \
      -e "s#__LOGS__#$mac_logs#g" \
      "$TPL/com.bogo.colima.plist.template" > "$plist"
  local uid; uid="$(id -u)"
  launchctl bootout "gui/$uid/com.bogo.colima" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$uid" "$plist"
  say "등록: com.bogo.colima (부팅 시 Colima 자동 기동)"
}

# 안전한 (재)등록: 같은 Label 이 아직 완전히 bootout 되지 않은 상태에서 bootstrap 하면
# launchd 가 "Input/output error (5)" 를 던지며 set -e 로 설치가 통째로 중단된다.
# (KeepAlive 봇이 즉시 재시작되며 라벨이 잠시 살아있는 레이스.) → bootout 후 라벨이
# 사라질 때까지 짧게 폴링하고, 그래도 실패하면 1회 재시도한다. $1=label, $2=plist 경로.
mac_bootstrap_safe() {
  local uid; uid="$(id -u)"
  local label="$1" plist="$2"
  launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
  # 라벨이 service DB 에서 빠질 때까지 최대 ~5초 대기(완전 unload 보장).
  local i=0
  while [ "$i" -lt 25 ] && launchctl print "gui/$uid/$label" >/dev/null 2>&1; do
    sleep 0.2; i=$((i + 1))
  done
  if ! launchctl bootstrap "gui/$uid" "$plist" 2>/dev/null; then
    sleep 1
    launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
    sleep 1
    launchctl bootstrap "gui/$uid" "$plist"   # 2차 실패는 진짜 오류 → set -e 로 중단
  fi
}

mac_install() {
  # Place an ASCII-path launcher that launchd calls (run_role.sh from the mirror).
  mkdir -p "${HOME}/.bogo-bin" "$mac_la"
  cp "$REPO/run_role.sh" "$mac_launcher"
  chmod +x "$mac_launcher"
  mac_sync
  # Colima 부팅 자동시작 등록 + 지금 당장 통신 백본 보장(봇 등록 전에 MM 이 떠 있어야 즉사 없음).
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
    say "등록+기동: com.bogo.$r"
  done
  # CEO 대시보드(127.0.0.1:DASH_PORT)도 봇과 동일하게 launchd 상시 소유로 승격.
  # 기존 oneclick nohup 단발 프로세스가 떠 있으면 중복 LISTEN 충돌하므로 먼저 정리한다.
  mac_kill_legacy_dashboard
  local dplist="$mac_la/com.bogo.dashboard.plist"
  sed -e "s#__LAUNCHER__#$mac_launcher#g" \
      -e "s#__APP__#$mac_app#g" \
      -e "s#__REPO__#$REPO#g" \
      -e "s#__DASH_PORT__#$DASH_PORT#g" \
      -e "s#__LOGS__#$mac_logs#g" \
      "$TPL/com.bogo.dashboard.plist.template" > "$dplist"
  mac_bootstrap_safe "com.bogo.dashboard" "$dplist"
  say "등록+기동: com.bogo.dashboard (127.0.0.1:$DASH_PORT)"
  say "macOS launchd 설치 완료. 상태:  ./service/install_service.sh status"
}

# launchd 가 대시보드를 소유하기 전에, oneclick 이 띄운 단발 nohup 대시보드(원본 Desktop
# 경로 또는 미러)를 안전 종료한다. 우리 ceo_dashboard.py 프로세스만 골라 죽인다(포트 점유
# 충돌·이중 LISTEN 방지). 외부 프로세스는 건드리지 않는다.
mac_kill_legacy_dashboard() {
  local holders; holders="$(lsof -nP -iTCP:"$DASH_PORT" -sTCP:LISTEN -t 2>/dev/null | sort -u || true)"
  for p in $holders; do
    if ps -p "$p" -o command= 2>/dev/null | grep -q "ceo_dashboard.py"; then
      kill "$p" 2>/dev/null || true; sleep 1; kill -9 "$p" 2>/dev/null || true
      say "기존 nohup 대시보드(PID $p) 정리 → launchd 소유로 이관."
    fi
  done
  rm -f "$mac_app/logs/dashboard.pid" "$REPO/logs/dashboard.pid" 2>/dev/null || true
}

mac_uninstall() {
  local uid; uid="$(id -u)"
  for r in "${ROLES[@]}"; do
    launchctl bootout "gui/$uid/com.bogo.$r" >/dev/null 2>&1 || true
    rm -f "$mac_la/com.bogo.$r.plist"
    say "해제: com.bogo.$r"
  done
  # CEO 대시보드 launchd 해제(미러·로그는 보존).
  launchctl bootout "gui/$uid/com.bogo.dashboard" >/dev/null 2>&1 || true
  rm -f "$mac_la/com.bogo.dashboard.plist"
  say "해제: com.bogo.dashboard"
  # Colima 부팅 자동시작 LaunchAgent 도 함께 해제(콜리마 VM 자체는 건드리지 않음).
  launchctl bootout "gui/$uid/com.bogo.colima" >/dev/null 2>&1 || true
  rm -f "$mac_la/com.bogo.colima.plist"
  say "해제: com.bogo.colima"
  say "launchd 등록 해제 완료. (미러 $mac_app 는 보존 — 수동 삭제 가능)"
}

mac_restart() {
  cp "$REPO/run_role.sh" "$mac_launcher"; chmod +x "$mac_launcher"
  mac_sync
  # 재시작 전에도 통신 백본을 보장한다(Colima/컨테이너가 내려가 있으면 봇이 또 즉사하므로).
  "$mac_app/infra_up.sh"
  local uid; uid="$(id -u)"
  for r in "${ROLES[@]}"; do
    launchctl kickstart -k "gui/$uid/com.bogo.$r" && say "재시작: com.bogo.$r"
  done
  # 대시보드가 아직 등록 안 됐을 수 있다(구버전에서 올린 경우) → 없으면 등록, 있으면 재시작.
  if launchctl print "gui/$uid/com.bogo.dashboard" >/dev/null 2>&1; then
    launchctl kickstart -k "gui/$uid/com.bogo.dashboard" && say "재시작: com.bogo.dashboard"
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
    say "등록+기동: com.bogo.dashboard (127.0.0.1:$DASH_PORT)"
  fi
}

# 미러 동기화 드리프트 감지: 원본(REPO)과 ASCII 미러(mac_app)의 핵심 코드 파일이
# 어긋나면 경고한다. launchd 데몬은 미러본을 실행하므로, 원본만 고치고 restart 를
# 안 하면 미러가 stale 채로 남아 "대시보드/봇이 옛 코드로 동작"하는 사고가 조용히
# 발생한다(예: mm_client 의 MM_BASE localhost→127.0.0.1 수정 미반영 시 Mattermost
# 연결이 ::1 거부로 실패). status 단계에서 이 드리프트를 즉시 가시화한다.
mac_check_mirror_sync() {
  [ -d "$mac_app" ] || { say "미러 없음(아직 install 안 됨): $mac_app"; return 0; }
  local drift=0 f
  for f in ceo_dashboard.py mm_client.py agent_schema.py bogo_runtime.py \
           ceo_admin_runtime.py teams.json channels.json; do
    [ -f "$REPO/$f" ] || continue
    if [ ! -f "$mac_app/$f" ] || ! cmp -s "$REPO/$f" "$mac_app/$f"; then
      printf '\033[0;33m[service]\033[0m   ⚠ 미러 불일치: %s\n' "$f"
      drift=1
    fi
  done
  if [ "$drift" -eq 1 ]; then
    printf '\033[0;33m[service]\033[0m 미러가 원본과 어긋났습니다 → 데몬이 옛 코드를 실행 중입니다.\n'
    printf '\033[0;33m[service]\033[0m 복구: ./service/install_service.sh restart\n'
  else
    say "미러 동기화 OK (원본 ↔ $mac_app 핵심 파일 일치)"
  fi
}

mac_status() {
  launchctl list | grep bogo || say "(실행 중인 com.bogo.* 없음)"
  mac_check_mirror_sync
}

# ════════════════════════════════════════════════════════════════════════
# Linux — systemd --user (in-place, Hangul-safe)
# ════════════════════════════════════════════════════════════════════════
sd_dir="${HOME}/.config/systemd/user"
sd_unit="$sd_dir/bogo@.service"

linux_install() {
  command -v systemctl >/dev/null 2>&1 || { err "systemctl 미발견 — systemd 환경이 아닙니다."; exit 1; }
  chmod +x "$REPO/run_role.sh"
  mkdir -p "$sd_dir"
  sed -e "s#__WORKDIR__#$REPO#g" "$TPL/bogo@.service.template" > "$sd_unit"
  systemctl --user daemon-reload
  # Lingering so user services survive logout / run at boot.
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || \
    say "참고: 'sudo loginctl enable-linger $(id -un)' 를 실행하면 로그아웃 후에도 유지됩니다."
  for r in "${ROLES[@]}"; do
    systemctl --user enable --now "bogo@$r.service"
    say "등록+기동: bogo@$r"
  done
  # CEO 대시보드(127.0.0.1:DASH_PORT)도 동일 템플릿 인스턴스로 상시 가동. run_role.sh 가
  # 'dashboard' 인자를 받아 ceo_dashboard.py 를 exec 하며, BOGO_DASHBOARD_PORT 기본 8642.
  systemctl --user enable --now "bogo@dashboard.service"
  say "등록+기동: bogo@dashboard (127.0.0.1:$DASH_PORT)"
  say "Linux systemd 설치 완료. 로그:  journalctl --user -u bogo@orchestrator -f"
}

linux_uninstall() {
  for r in "${ROLES[@]}"; do
    systemctl --user disable --now "bogo@$r.service" >/dev/null 2>&1 || true
    say "해제: bogo@$r"
  done
  systemctl --user disable --now "bogo@dashboard.service" >/dev/null 2>&1 || true
  say "해제: bogo@dashboard"
  rm -f "$sd_unit"
  systemctl --user daemon-reload || true
  say "systemd 등록 해제 완료."
}

linux_restart() {
  chmod +x "$REPO/run_role.sh"
  for r in "${ROLES[@]}"; do
    systemctl --user restart "bogo@$r.service" && say "재시작: bogo@$r"
  done
  # 대시보드 인스턴스가 아직 enable 안 됐으면(구버전) 등록까지, 있으면 재시작.
  systemctl --user enable --now "bogo@dashboard.service" 2>/dev/null || true
  systemctl --user restart "bogo@dashboard.service" && say "재시작: bogo@dashboard"
}

linux_status() {
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
  *) err "지원하지 않는 OS: $OS (Windows 는 install_service.ps1 사용)"; exit 1 ;;
esac

case "$ACTION" in
  install)   "${fn}_install" ;;
  uninstall) "${fn}_uninstall" ;;
  restart)   "${fn}_restart" ;;
  status)    "${fn}_status" ;;
  *) err "알 수 없는 명령: $ACTION (install|uninstall|restart|status)"; exit 1 ;;
esac
