#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO one-click orchestrator — activate every component with a single button
# ════════════════════════════════════════════════════════════════════════
#  WHY  The existing path only brought up (a) the communication backbone
#    (infra_up.sh) and (b) the bot launchd (bogo_ctl.sh). The CEO dashboard (8642)
#    and Vault RAG reindex were not auto-started anywhere, so a person had to
#    launch them by hand every time — a leftover toil. This script brings up all
#    5 layers at once with "fixed order + health check + idempotency + port-conflict guard".
#
#  WHAT (order. On each step failure, stop with a clear blocker; no false completion):
#    1) venv/dependency check (bootstrap if missing)
#    2) Vault RAG reindex (reflect visibility schema, once)
#    3) Mattermost communication backbone (Colima→container→MM readiness)  ← reuses infra_up.sh
#    4) Register the 4 agent-bot roles + CEO dashboard launchd/systemd ← bogo_ctl.sh→install_service.sh
#    5) CEO dashboard (127.0.0.1:8642) health check                ← only verifies what launchd started
#
#  Regression prevention (key): the dashboard is no longer a one-shot nohup process of oneclick.
#    Just like the 4 bot roles, launchd (com.bogo.dashboard) / systemd (bogo@dashboard) owns it
#    permanently via KeepAlive → it auto-revives even on terminal close/sleep/manual kill. oneclick
#    guarantees registration and only health-checks (no direct launch, no duplicate nohup). A real
#    stop is stop (deregister the service).
#
#  Idempotency: re-running reuses whatever is already up (no duplicate launch). install_service.sh
#    boots out the existing registration and re-registers (idempotent), and safely cleans up any
#    past nohup dashboard holding the port.
#
#  Security: the dashboard MUST listen only on 127.0.0.1 (loopback). External exposure is forbidden.
#  Logs/PID: written only under app/logs/ (handled by .gitignore).
#  Usage:
#    ./bogo_oneclick.sh start     # bring up everything (default)
#    ./bogo_oneclick.sh stop      # stop the dashboard (bot launchd is separate via the stop arg)
#    ./bogo_oneclick.sh stop --all  # bring down the dashboard + bot launchd as well
#    ./bogo_oneclick.sh status    # status of all components
#    ./bogo_oneclick.sh restart   # stop then restart
set -uo pipefail

# ── Own location = app root (safe for Hangul/space paths) ──────────────────────────
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$HERE"

LOGS="$HERE/logs"
mkdir -p "$LOGS"

# Dashboard loopback-only + port (overridable via env var, default 8642).
DASH_HOST="127.0.0.1"
DASH_PORT="${BOGO_DASHBOARD_PORT:-8642}"
DASH_PID_FILE="$LOGS/dashboard.pid"
DASH_OUT="$LOGS/dashboard.out.log"
DASH_ERR="$LOGS/dashboard.err.log"
DASH_HEALTH_TIMEOUT="${BOGO_DASH_WAIT_TIMEOUT:-30}"

MM_HOST="127.0.0.1"
MM_PORT="8065"

# Network auto-detection result (filled in by step_netdetect). So the health check probes
# the actual NIC IP rather than loopback on multihome/single-network setups, we keep the
# detected representative binding host.
# Default is loopback (before detection / loopback mode) — identical to the legacy single-PC
# behavior (zero regression).
DETECTED_MODE="loopback"
DETECTED_HOST="127.0.0.1"

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf '%s[oneclick]%s %s\n'    "$C_INFO" "$C_RST" "$*"; }
ok()   { printf '%s[oneclick:OK]%s %s\n' "$C_OK"   "$C_RST" "$*"; }
warn() { printf '%s[oneclick:WARN]%s %s\n' "$C_WARN" "$C_RST" "$*" >&2; }
err()  { printf '%s[oneclick:ERROR]%s %s\n' "$C_ERR"  "$C_RST" "$*" >&2; }

VENV_PY="$HERE/.venv/bin/python"

# ── HTTP 200 check (either curl/python. python urllib preferred to avoid hook blocking) ──
http_ok() {
  # $1 = url
  "$VENV_PY" - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    r = urllib.request.urlopen(sys.argv[1], timeout=3)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

# ── List of PIDs LISTENing on a port (loopback-only judgment is up to the caller) ──────────
# WHY  macOS has lsof by default, but slim/container Linux (Debian slim, alpine,
#   minimal installs, etc.) may not have lsof. So that port-PID detection works in those
#   environments too, fall back in the order lsof → ss (iproute2) → fuser (psmisc). Whichever
#   path is used, the output is normalized identically to "one PID per line, sorted and deduped".
pids_on_port() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    # macOS default path (keeps legacy behavior). Extract only the PIDs of LISTEN sockets.
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | sort -u
  elif command -v ss >/dev/null 2>&1; then
    # iproute2. -p includes process info. Extract pid from users:(("proc",pid=1234,fd=5)).
    ss -ltnpH "( sport = :$port )" 2>/dev/null \
      | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
  elif command -v fuser >/dev/null 2>&1; then
    # psmisc. Outputs the PIDs holding the TCP port, space-separated, to stderr/stdout → normalize to one per line.
    fuser -n tcp "$port" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u
  fi
  # If none of the three exist, empty output (caller safely treats empty as "no detection tool / not held").
}

# ════════════════════════════════════════════════════════════════════════
# 1) venv/dependency check
# ════════════════════════════════════════════════════════════════════════
step_venv() {
  say "[1/5] Checking venv/dependencies..."
  if [ ! -x "$VENV_PY" ]; then
    warn ".venv missing → running bootstrap.sh (may take a few minutes)"
    if ! "$HERE/bootstrap.sh"; then
      err "bootstrap failed. Check whether Python 3 is installed: brew install python"
      return 1
    fi
  fi
  # Verify core dependency imports (sentence-transformers is optional, so excluded).
  if ! "$VENV_PY" -c "import urllib.request, json, sqlite3" >/dev/null 2>&1; then
    err "The venv python is not working properly: $VENV_PY"
    return 1
  fi
  ok "venv ready: $VENV_PY"
}

# ════════════════════════════════════════════════════════════════════════
# 2) Vault RAG reindex (reflect visibility schema)
# ════════════════════════════════════════════════════════════════════════
step_reindex() {
  say "[2/5] Full Vault RAG reindex (reflecting visibility schema)..."
  if [ ! -f "$HERE/vault_rag.py" ]; then
    warn "vault_rag.py missing → skipping reindex (optional component)."
    return 0
  fi
  if "$VENV_PY" "$HERE/vault_rag.py" reindex >"$LOGS/reindex.out.log" 2>&1; then
    ok "reindex complete (log: logs/reindex.out.log)"
  else
    # A reindex failure does not block the dashboard/bots (only partial search degradation). Warn only.
    warn "reindex failed (search may be partially degraded). Details: logs/reindex.out.log"
  fi
}

# ════════════════════════════════════════════════════════════════════════
# 2.5) Network auto-provisioning (just plug in the LAN cable → auto-detect NIC/private IP → inject into .env)
# ════════════════════════════════════════════════════════════════════════
#  WHY  To use a multihome central server (3 NICs directly connected to intra-company networks A/B/C),
#    the operator had to hand-write BOGO_MULTIHOME / MM_BIND_HOST / MM_SITE_URL / MM_ALLOW_CORS_FROM /
#    BOGO_DASHBOARD_HOST into .env. Having a person memorize and type private IPs is a constant source
#    of typos and omissions. This step reads the NICs and private IPs on its own, decides the mode,
#    and idempotently injects only the 'network keys' of .env (secrets/manual values are non-destructive).
#  Security  If a public (globally routable) IP NIC is detected, it halts the multihome 0.0.0.0
#    auto-activation and only warns (net_autodetect decides guard mode → no change to network keys = loopback kept).
#    Even on failure it does not block bot startup (continues with the existing .env values).
step_netdetect() {
  say "[2.5/5] Network auto-detect (NIC/private IP) → auto-configure .env..."
  if [ ! -f "$HERE/net_autodetect.py" ]; then
    warn "net_autodetect.py missing → skipping network auto-config (using existing .env values)."
    return 0
  fi
  # Idempotently upsert only the network keys of .env. If .env is missing, seed from .env.example then inject.
  local summary rc
  summary="$("$VENV_PY" "$HERE/net_autodetect.py" apply \
    --env "$HERE/.env" --example "$HERE/.env.example" 2>>"$LOGS/netdetect.err.log")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    warn "Network auto-config failed (continuing with existing .env values). Details: logs/netdetect.err.log"
    return 0
  fi
  # One-time human-readable summary output (so the operator sees at a glance which network config came up).
  printf '%s' "$summary" | while IFS= read -r line; do say "$line"; done

  # Extract mode/host so the health check probes the actual NIC IP rather than loopback on multihome/single-network.
  # (Calls detect once more — same function as apply, so results match. Parse mode/representative IP from JSON.)
  local detect_json
  detect_json="$("$VENV_PY" "$HERE/net_autodetect.py" detect 2>/dev/null)"
  DETECTED_MODE="$(printf '%s' "$detect_json" | "$VENV_PY" -c \
    'import sys,json; print(json.load(sys.stdin).get("mode","loopback"))' 2>/dev/null || echo loopback)"
  # Health-check target host: multihome/single-network use the representative NIC private IP, otherwise loopback.
  DETECTED_HOST="$(printf '%s' "$detect_json" | "$VENV_PY" -c \
    'import sys,json; d=json.load(sys.stdin); rep=d.get("rep"); print(rep[1] if rep and d.get("mode") in ("multihome","lan") else "127.0.0.1")' \
    2>/dev/null || echo 127.0.0.1)"
  ok "Network configuration complete (mode: $DETECTED_MODE, health-check host: $DETECTED_HOST)."
}

# ════════════════════════════════════════════════════════════════════════
# 2.6) Multihome network-segregation guard (keep the server from becoming a router bridging company networks A↔B↔C)
# ════════════════════════════════════════════════════════════════════════
#  WHY  A multihome central server is directly connected to 'different company networks' A/B/C via 3 NICs.
#    If the kernel has net.ipv4.ip_forward=1, the server forwards packets between NICs and becomes a
#    'router/bridge', so the 3 company networks that must be physically separated can reach each other
#    through the server (segregation defeated — serious, since it is traffic between different companies).
#    So in multihome mode we must enforce ① net.ipv4.ip_forward=0 ② iptables FORWARD default policy DROP.
#  Design  Idempotent, non-blocking to startup. If forwarding is on, attempt to turn it off non-interactively
#    via sudo (sudo -n: only without a password prompt), but if there is no permission or it fails, do not
#    force it — just warn + print copy-paste commands (do not forcibly change the system without operator consent).
#    In non-multihome modes (NIC ≤ 1), inter-network forwarding cannot occur, so skip this step entirely.
step_netseg() {
  # Only meaningful when multihome (single-network LAN/loopback/guard modes do not apply).
  [ "$DETECTED_MODE" = "multihome" ] || return 0
  [ -f "$HERE/net_autodetect.py" ] || return 0

  say "[2.6/5] Multihome network-segregation guard (checking that the server does not become an A↔B↔C router)..."

  # Obtain the segregation-diagnosis JSON from a pure function (net_autodetect.assess_segregation).
  local seg_json risk ipf
  seg_json="$("$VENV_PY" "$HERE/net_autodetect.py" segregation 2>/dev/null)"
  risk="$(printf '%s' "$seg_json" | "$VENV_PY" -c \
    'import sys,json; print(json.load(sys.stdin)["assessment"]["risk"])' 2>/dev/null || echo unknown)"
  ipf="$(printf '%s' "$seg_json" | "$VENV_PY" -c \
    'import sys,json; v=json.load(sys.stdin)["ip_forward"]; print("" if v is None else ("1" if v else "0"))' 2>/dev/null || echo "")"

  case "$risk" in
    ok)
      ok "IP forwarding off (0) — the server is not a router (segregation maintained)." ;;
    danger)
      warn "IP forwarding is on (1) → the server becomes a router bridging company networks A↔B↔C (segregation defeated)."
      # Attempt to safely turn it off only non-interactively via sudo (-n). If no permission, do not force — guide instead.
      if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        if sudo -n sysctl -w net.ipv4.ip_forward=0 >/dev/null 2>&1; then
          # Persist across reboots (drop-in). Even if this fails, the runtime block is already applied.
          echo 'net.ipv4.ip_forward=0' | sudo -n tee /etc/sysctl.d/99-bogo-no-forward.conf >/dev/null 2>&1 || true
          sudo -n iptables -P FORWARD DROP >/dev/null 2>&1 || true
          sudo -n iptables -F FORWARD >/dev/null 2>&1 || true
          ok "IP forwarding block applied (net.ipv4.ip_forward=0 + FORWARD DROP). Segregation restored."
        else
          warn "Auto-block of forwarding failed — run the commands below directly with admin privileges (startup continues):"
          _netseg_print_fix
        fi
      else
        warn "No sudo privilege (or absent) — not forcing the auto-block. Run the commands below with admin privileges (startup continues):"
        _netseg_print_fix
      fi
      ;;
    *)
      warn "Could not determine the IP forwarding value (value='${ipf:-?}'). Check the below with admin privileges (startup continues):"
      _netseg_print_fix
      ;;
  esac
}

# Print the segregation-recovery commands so the operator can copy-paste them (same as net_autodetect.segregation_commands).
_netseg_print_fix() {
  printf '    %ssudo sysctl -w net.ipv4.ip_forward=0%s\n' "$C_WARN" "$C_RST" >&2
  printf '    %secho '\''net.ipv4.ip_forward=0'\'' | sudo tee /etc/sysctl.d/99-bogo-no-forward.conf%s\n' "$C_WARN" "$C_RST" >&2
  printf '    %ssudo sysctl --system%s\n' "$C_WARN" "$C_RST" >&2
  printf '    %ssudo iptables -P FORWARD DROP && sudo iptables -F FORWARD%s\n' "$C_WARN" "$C_RST" >&2
  printf '    %s(for detailed rationale/persistence, see docs/DEPLOY_NETWORK.md 1-7 "maintaining multihome network segregation")%s\n' "$C_INFO" "$C_RST" >&2
}

# ════════════════════════════════════════════════════════════════════════
# 3) Mattermost communication backbone
# ════════════════════════════════════════════════════════════════════════
step_infra() {
  say "[3/5] Bringing up the Mattermost communication backbone (Colima→container→MM readiness)..."
  if [ ! -x "$HERE/infra_up.sh" ]; then
    err "infra_up.sh is missing or not executable."
    return 1
  fi
  if "$HERE/infra_up.sh"; then
    ok "Communication backbone ready (MM http://$MM_HOST:$MM_PORT)"
  else
    err "Communication backbone startup failed — Docker/Colima check needed (see the blocker guidance below)."
    return 2   # 2 = external-dependency (Docker) blocker signal
  fi
}

# ════════════════════════════════════════════════════════════════════════
# 3.5) Automatic data restore (the heart of unattended migration) — when you move just the folder
#   from another PC, auto-inject the old PC's conversation/account/channel/report DBs. If a backup
#   (app/migration/bogo_backup_latest.tar.gz) 'exists' restore it, 'otherwise' pass through empty
#   (an automatic branch that does not ask the human).
#   Idempotency: bogo_restore.sh prevents duplicate restores via a restore marker (.bogo_restored),
#   so re-running does not overwrite existing data (safe even on an already-operating PC).
# ════════════════════════════════════════════════════════════════════════
step_restore() {
  say "[3.5/5] Checking automatic data restore (inject if a backup exists, otherwise proceed empty)..."
  local restore="$HERE/migration/bogo_restore.sh"
  if [ ! -f "$restore" ]; then
    say "migration/bogo_restore.sh missing → skipping restore step (data migration not in use)."
    return 0
  fi
  chmod +x "$restore" 2>/dev/null || true
  # A restore failure does not block bot startup (the service must come up even if empty). Warn only.
  if "$restore"; then
    ok "Data restore step passed (restored or proceeding empty)."
  else
    warn "Warning/error during data restore — continuing with empty or partial restore. See the log above for details."
  fi
}

# ════════════════════════════════════════════════════════════════════════
# 4·5) CEO dashboard (127.0.0.1:8642) — permanently owned by launchd, oneclick only health-checks
# ════════════════════════════════════════════════════════════════════════
dashboard_running_pid() {
  # When launchd owns it there is no PID file, so treat our ceo_dashboard.py LISTENing on the port
  # as the source of truth (also check the PID file for legacy nohup compatibility).
  if [ -f "$DASH_PID_FILE" ]; then
    local p; p="$(cat "$DASH_PID_FILE" 2>/dev/null || true)"
    if [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null; then
      if ps -p "$p" -o command= 2>/dev/null | grep -q "ceo_dashboard.py"; then
        echo "$p"; return 0
      fi
    fi
  fi
  for p in $(pids_on_port "$DASH_PORT"); do
    if ps -p "$p" -o command= 2>/dev/null | grep -q "ceo_dashboard.py"; then
      echo "$p"; return 0
    fi
  done
  return 1
}

# Decide the health-check target host: on multihome/single-network the dashboard responds not on
# loopback but on the detected NIC IP (or the representative IP of a 0.0.0.0 binding), so looking
# only at 127.0.0.1 yields a false failure. A 0.0.0.0 binding also responds on loopback, but to
# verify "is the NIC IP employees actually connect to alive" it is more accurate to probe the
# representative NIC IP. Fixes the previous task's 'defect of looking only at 127.0.0.1'.
dash_health_host() {
  case "$DETECTED_MODE" in
    multihome|lan) echo "$DETECTED_HOST" ;;
    *) echo "$DASH_HOST" ;;   # loopback/guard = loopback (keeps legacy behavior, zero regression)
  esac
}

step_dashboard() {
  local hhost; hhost="$(dash_health_host)"
  say "[after 5/5] CEO dashboard health check ($hhost:$DASH_PORT, mode:$DETECTED_MODE, owned by launchd/systemd)..."

  # Design change (regression prevention): the dashboard is no longer a one-shot nohup process of
  # oneclick — launchd (com.bogo.dashboard) / systemd (bogo@dashboard) owns it permanently via KeepAlive.
  # That registration is done together with the bots in step_bots → bogo_ctl.sh → install_service.sh.
  # So here we do not "launch directly"; we only health-check whether the launchd-started dashboard is
  # alive and responding (launchd auto-revives it even on terminal close/sleep/manual kill).

  # If a one-shot nohup dashboard PID file left by a past version exists, ignore it (launchd is source of truth).
  rm -f "$DASH_PID_FILE" 2>/dev/null || true

  local waited=0
  while [ "$waited" -lt "$DASH_HEALTH_TIMEOUT" ]; do
    if http_ok "http://$hhost:$DASH_PORT/login"; then
      local pid; pid="$(pids_on_port "$DASH_PORT" | head -1)"
      ok "Dashboard healthy (owned by launchd/systemd, PID ${pid:-?}) — http://$hhost:$DASH_PORT"
      return 0
    fi
    sleep 1; waited=$((waited + 1))
  done
  err "Dashboard did not respond within ${DASH_HEALTH_TIMEOUT}s (check launchd com.bogo.dashboard)."
  err "  Diagnostics: launchctl print gui/\$(id -u)/com.bogo.dashboard ; tail logs/dashboard.err.log"
  tail -n 15 "$DASH_ERR" 2>/dev/null >&2 || true
  return 1
}

# ════════════════════════════════════════════════════════════════════════
# 5) The 4 agent-bot roles (launchd/systemd, reuses bogo_ctl.sh)
# ════════════════════════════════════════════════════════════════════════
bots_loaded_count() {
  case "$(uname -s)" in
    Darwin) launchctl list 2>/dev/null | grep -c "com.bogo.\(orchestrator\|hr\|dev\|admin\)" || true ;;
    Linux)  systemctl --user list-units 'bogo@*' --no-legend 2>/dev/null | grep -c bogo || true ;;
    *) echo 0 ;;
  esac
}

step_bots() {
  say "[5/5] Starting/redeploying the 4 agent-bot roles..."
  if [ ! -x "$HERE/bogo_ctl.sh" ]; then
    err "bogo_ctl.sh missing → cannot start bots."
    return 1
  fi
  local loaded; loaded="$(bots_loaded_count)"
  if [ "${loaded:-0}" -ge 1 ]; then
    say "${loaded} bots already registered → redeploy latest code + restart (no duplicate launch)."
    # restart internally calls infra_up.sh again, but it is idempotent so it is safe (already up = passes immediately).
    if "$HERE/bogo_ctl.sh" restart; then ok "Bot redeploy+restart complete."; else
      err "Bot restart failed — diagnostics: ./bogo_ctl.sh status"; return 1; fi
  else
    say "Bots not registered → first install (bootstrap + backbone + launchd registration)."
    if "$HERE/bogo_ctl.sh" setup; then ok "Bot install + always-on registration complete."; else
      err "Bot install failed — check the log above."; return 1; fi
  fi
}

# ════════════════════════════════════════════════════════════════════════
# Stop / status
# ════════════════════════════════════════════════════════════════════════
# Deregister the dashboard launchd (com.bogo.dashboard) / systemd (bogo@dashboard) to 'really' stop it.
# A plain kill is immediately revived by KeepAlive, so it fails to achieve the stop intent.
dashboard_service_stop() {
  case "$(uname -s)" in
    Darwin)
      local uid; uid="$(id -u)"
      launchctl bootout "gui/$uid/com.bogo.dashboard" >/dev/null 2>&1 || true ;;
    Linux)
      systemctl --user disable --now "bogo@dashboard.service" >/dev/null 2>&1 || true ;;
  esac
}

# One-time backup on graceful shutdown (clean-shutdown snapshot). Separate from periodic backups,
# it leaves the latest state inside the folder right before the user intentionally brings it down.
# PG is still alive up to this point, so the backup is valid.
# best-effort: even if the backup fails, the stop itself proceeds (does not block the stop).
stop_backup_snapshot() {
  local backup="$HERE/migration/bogo_backup.sh"
  [ -f "$backup" ] || return 0
  say "Data snapshot backup before graceful shutdown (refreshing the latest copy inside the folder)..."
  if BOGO_BACKUP_RETAIN="${BOGO_BACKUP_RETAIN:-3}" bash "$backup" --quiet --out "$HERE/migration" >/dev/null 2>&1; then
    ok "Pre-shutdown backup complete → migration/bogo_backup_latest.tar.gz"
  else
    warn "Pre-shutdown backup skipped (Docker not running, etc.) — the most recent periodic backup remains in the folder."
  fi
}

do_stop() {
  local all="${1:-}"
  # One-time snapshot right before stopping (clean-shutdown backup) — only valid when Docker is up, proceeds even on failure.
  stop_backup_snapshot
  say "Stopping the dashboard (deregister launchd/systemd → block KeepAlive revival)..."
  dashboard_service_stop
  local stopped=0
  # After deregistration, clean up any remaining dashboard processes of ours (including past nohup).
  for p in $(pids_on_port "$DASH_PORT"); do
    if ps -p "$p" -o command= 2>/dev/null | grep -q "ceo_dashboard.py"; then
      kill "$p" 2>/dev/null || true; sleep 1; kill -9 "$p" 2>/dev/null || true; stopped=1
    fi
  done
  rm -f "$DASH_PID_FILE"
  ok "Dashboard stopped (service deregistration complete)."

  if [ "$all" = "--all" ]; then
    say "Deregistering bot launchd/systemd (stopping always-on)..."
    "$HERE/bogo_ctl.sh" uninstall && ok "Bot always-on deregistered." || warn "Warning during bot deregistration."
    say "Note: the Mattermost/Postgres containers and Colima are left as-is to preserve data."
    say "      If a full shutdown is needed, do it manually: docker stop bogo-mm bogo-pg && colima stop"
  fi
}

do_status() {
  printf '%s── BOGO component status ──%s\n' "$C_INFO" "$C_RST"
  # Backbone
  local mm="down"
  http_ok "http://$MM_HOST:$MM_PORT/api/v4/system/ping" && mm="healthy(200)"
  printf '  Mattermost   : %s  (http://%s:%s)\n' "$mm" "$MM_HOST" "$MM_PORT"
  # Dashboard
  local ds="down"
  if dashboard_running_pid >/dev/null && http_ok "http://$DASH_HOST:$DASH_PORT/login"; then
    ds="healthy (PID $(dashboard_running_pid))"
  fi
  printf '  CEO dashboard: %s  (http://%s:%s)\n' "$ds" "$DASH_HOST" "$DASH_PORT"
  # Bots
  printf '  Agent bots   : %s registered\n' "$(bots_loaded_count)"
  if [ "$(uname -s)" = "Darwin" ]; then
    launchctl list 2>/dev/null | grep "com.bogo." | sed 's/^/      /' || true
  fi
}

# ════════════════════════════════════════════════════════════════════════
# start pipeline
# ════════════════════════════════════════════════════════════════════════
do_start() {
  printf '\n%s════ BOGO one-click startup begin ════%s\n' "$C_INFO" "$C_RST"
  say "Location: $HERE"
  printf '\n'

  step_venv     || { err "Step 1 (venv) failed — aborting."; return 1; }
  step_reindex                                   # proceeds even on failure (warn only)
  # Network auto-detect → inject into .env. Must run 'before' the infra (docker compose) and dashboard
  # read .env so the new binding is reflected. Even on failure, continue with existing .env (warn only).
  step_netdetect                                 # proceeds even on failure (warn only)
  # If judged multihome, check/apply the IP forwarding block so the server does not become an inter-network router.
  # Non-multihome passes through immediately internally. Even on failure it does not block startup (warn + copy-paste commands only).
  step_netseg                                    # proceeds even on failure (warn only)
  local infra_rc
  step_infra; infra_rc=$?
  if [ "$infra_rc" -eq 2 ]; then
    err "════ Blocker: could not bring up the Mattermost communication backbone ════"
    err "Cause: the Docker/Colima runtime is not ready."
    err "The one thing the operator should do: run 'colima start' (or start Docker Desktop), then re-run this launcher."
    return 2
  elif [ "$infra_rc" -ne 0 ]; then
    err "Step 3 (backbone) failed — aborting."
    return 1
  fi
  # Now, with the infra (empty bogo-pg/bogo-mm) just up, is the right moment for data injection. If a backup exists,
  # auto-restore the old PC's conversation/account/channel/report; otherwise pass through empty (automatic branch).
  step_restore                                   # proceeds even on failure (warn only — service comes up even if empty)
  # Register both bots and dashboard as always-on launchd/systemd (install_service.sh brings up both).
  step_bots      || { err "Step 4 (bot + dashboard registration) failed — aborting."; return 1; }
  # Health-check until the launchd-started dashboard responds (not a direct launch; auto-revival ownership is launchd's).
  step_dashboard || { err "Step 5 (dashboard health check) failed — need to check launchd status."; return 1; }

  local hhost; hhost="$(dash_health_host)"
  printf '\n%s════ All components activated (network mode: %s) ════%s\n' "$C_OK" "$DETECTED_MODE" "$C_RST"
  printf '  • CEO dashboard:  %shttp://%s:%s%s\n' "$C_OK" "$hhost" "$DASH_PORT" "$C_RST"
  printf '  • Mattermost   :  %shttp://%s:%s%s\n' "$C_OK" "$hhost" "$MM_PORT" "$C_RST"
  if [ "$DETECTED_MODE" = "multihome" ]; then
    printf '  • Multihome    :  employees on each network browse to their own network NIC IP:%s (zero client setup)\n' "$MM_PORT"
    printf '                   for reachable addresses, see each NIC IP in the [2.5/5] summary above\n'
  fi
  printf '  • Agent bots   :  %s always-on (launchd/systemd), connecting to MM over the server-local 127.0.0.1\n' "$(bots_loaded_count)"
  printf '  • Stop         :  ./bogo_oneclick.sh stop   (including bots: stop --all)\n'
  printf '  • Status       :  ./bogo_oneclick.sh status\n\n'
}

# ── Dispatch ──────────────────────────────────────────────────────────────
cmd="${1:-start}"; shift || true
case "$cmd" in
  start)   do_start ;;
  stop)    do_stop "${1:-}" ;;
  restart) do_stop ""; printf '\n'; do_start ;;
  status)  do_status ;;
  *) err "Unknown command: $cmd (start|stop|stop --all|restart|status)"; exit 1 ;;
esac
