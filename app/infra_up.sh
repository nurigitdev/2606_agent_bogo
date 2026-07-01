#!/usr/bin/env bash
# BOGO infra bring-up — an idempotent boot dependency chain that guarantees the
# communication backbone (Colima VM + Mattermost + Postgres) is alive before the bots start.
#
# WHY: the bots connect to Mattermost at ws://127.0.0.1:8065 (localhost resolves to
#   ::1 first, which is refused on colima's IPv4-only forwarding, so IPv4 is forced). If
#   Colima (the Docker runtime VM) is off, the containers go Exited, and if MM is missing the
#   bots die on connection failure. The previous startup paths (bogo_ctl setup/restart,
#   BOGO_start.command) did not guarantee this backbone was up and launched the bots directly
#   → root cause. This script fills that gap.
#
# WHAT (ordered, all idempotent):
#   1) If Colima is not running, colima start. On a stale lock, stop --force then restart.
#   2) If the bogo-pg, bogo-mm containers are not Up, docker start (data preserved).
#      Raise the restart policy to unless-stopped so they auto-revive when Colima restarts.
#   3) Poll-wait until MM /api/v4/system/ping returns 200 (timeout + clear failure message).
#
# If already up, each step is skipped (no duplicate startup). If any step is unrecoverable,
# exit with a non-zero code so the caller (bogo_ctl/install_service) does not launch the bots.
#
# Korean/space path safety: containers/VMs are path-agnostic; only docker/colima CLIs are called.
set -euo pipefail

say()  { printf '\033[0;36m[infra]\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m[infra:OK]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[infra:WARN]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[0;31m[infra:ERROR]\033[0m %s\n' "$*" >&2; }

# Container names (fixed). docker-compose.yml creates the persistent containers with these
# names, and this script inspects/starts them by the same names (name = the contract between
# the two files).
PG_NAME="bogo-pg"
MM_NAME="bogo-mm"

# Initial container creation definition (portability). On a machine where the containers
# don't exist at all (like another PC), this compose creates bogo-pg/bogo-mm once. Keep it
# in the same directory as this script.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${BOGO_COMPOSE_FILE:-$HERE/docker-compose.yml}"

# MM healthy wait limit (seconds). An MM cold boot can take tens of seconds.
MM_WAIT_TIMEOUT="${BOGO_MM_WAIT_TIMEOUT:-180}"
# Colima boot wait limit (seconds).
COLIMA_WAIT_TIMEOUT="${BOGO_COLIMA_WAIT_TIMEOUT:-180}"

need() {
  command -v "$1" >/dev/null 2>&1 || { err "Could not find the '$1' command. Install Docker/Compose, then try again."; exit 2; }
}

# Check Docker daemon reachability (OS-aware). On Linux native the most common failures are
# (a) the Docker daemon is not running, (b) the current user is not in the docker group so the
# socket permission is denied — both are caught with a clear one-line hint.
# On macOS (Colima), ensure_colima brings up the VM, so here we only ping the daemon.
ensure_docker_reachable() {
  docker info >/dev/null 2>&1 && return 0
  if [ "$(uname -s)" = "Linux" ]; then
    # Distinguish a permission issue (socket exists but denied) from a dead daemon and advise.
    local docker_sock="${BOGO_DOCKER_SOCK:-/var/run/docker.sock}"
    if [ -S "$docker_sock" ] && ! docker info >/dev/null 2>&1; then
      err "Docker socket access denied — the current user may not be in the docker group."
      err "One thing to do:  run  sudo usermod -aG docker \"\$USER\"  then 're-login' (or newgrp docker)."
    else
      err "Cannot connect to the Docker daemon (likely not running)."
      err "One thing to do:  sudo systemctl start docker   (auto-start on boot: sudo systemctl enable docker)"
    fi
    exit 2
  fi
  # macOS: Colima must be up to be reachable. If it still fails after ensure_colima, follow that guidance.
  err "Cannot connect to the Docker daemon. Run 'colima start' and try again."
  exit 2
}

# ── 1. Ensure Colima ──────────────────────────────────────────────────────
ensure_colima() {
  # Colima is macOS's Docker runtime VM. A Linux native docker environment has no colima and
  # doesn't need one (the daemon is managed by systemd), so if not found, skip this step.
  if ! command -v colima >/dev/null 2>&1; then
    say "colima not found (assuming Linux native docker) → skipping the Colima step."
    return 0
  fi
  # colima status returns 0 if running, non-zero otherwise (message on stderr).
  if colima status >/dev/null 2>&1; then
    ok "Colima already running — skipping."
    return 0
  fi

  say "Colima not running → attempting to start..."
  if colima start >/dev/null 2>&1; then
    : # started
  else
    # Common failure: a stale lock/socket left after an abnormal shutdown → force stop then restart.
    warn "colima start failed → possible stale lock. Retrying after stop --force."
    colima stop --force >/dev/null 2>&1 || true
    sleep 2
    if ! colima start >/dev/null 2>&1; then
      err "Colima failed to start. Check manually: colima status / colima start"
      exit 2
    fi
  fi

  # Poll until running is confirmed (absorb VM boot time).
  local waited=0
  while ! colima status >/dev/null 2>&1; do
    sleep 2; waited=$((waited + 2))
    if [ "$waited" -ge "$COLIMA_WAIT_TIMEOUT" ]; then
      err "Colima did not reach running within ${COLIMA_WAIT_TIMEOUT}s."
      exit 2
    fi
  done
  ok "Colima running (took ${waited}s)."
}

# ── 2. Ensure containers ──────────────────────────────────────────────────
container_state() { docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo "absent"; }

# docker compose invoker (auto-selects the new 'docker compose' / legacy 'docker-compose').
# Pins --project-directory to the directory containing the compose file (=app). This way,
# regardless of the calling cwd or the compose implementation (v2 / legacy docker-compose),
# app/.env is 'always' auto-loaded → the multi-homed network keys injected by net_autodetect
# (${MM_BIND_HOST} etc.) are passed to compose variable interpolation without a break no matter
# which entrypoint/cwd calls it (prevents missing fallbacks).
COMPOSE_DIR="$(cd "$(dirname "$COMPOSE_FILE")" && pwd)"
compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_FILE" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_FILE" "$@"
  else
    return 127
  fi
}

env_file_value() {
  local key="$1" file="$COMPOSE_DIR/.env"
  if [ "${!key+x}" = "x" ]; then
    printf '%s' "${!key}"
    return 0
  fi
  [ -f "$file" ] || return 0
  awk -v key="$key" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    {
      line=$0
      sub(/^[[:space:]]*/, "", line)
      if (line ~ "^" key "[[:space:]]*=") {
        sub(/^[^=]*=/, "", line)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
        if (substr(line, 1, 1) == "\"" && substr(line, length(line), 1) == "\"") {
          line=substr(line, 2, length(line) - 2)
          gsub(/\\"/, "\"", line)
        }
        print line
      }
    }
  ' "$file" | tail -n 1
}

container_env_value() {
  local name="$1" key="$2" env_lines
  env_lines="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$name" 2>/dev/null || true)"
  printf '%s\n' "$env_lines" | awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); print; exit }'
}

port_binding_has_host() {
  local desired="$1" line host
  while IFS= read -r line; do
    case "$line" in
      *:8065) ;;
      *) continue ;;
    esac
    host="${line%:8065}"
    host="${host#[}"
    host="${host%]}"
    if [ "$host" = "$desired" ]; then
      return 0
    fi
    if [ "$desired" = "0.0.0.0" ] && [ "$host" = "::" ]; then
      return 0
    fi
  done
  return 1
}

compose_up_or_exit() {
  local rc
  set +e
  compose up -d
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    return 0
  fi
  if [ "$rc" = "127" ]; then
    err "Could not find docker compose. Docker Desktop/Compose plugin must be installed."
    exit 3
  fi
  err "compose up failed (rc=$rc). Diagnose: docker compose -f \"$COMPOSE_FILE\" logs"
  exit 1
}

# If any container is missing, create both from scratch via compose (idempotent: no change if
# they already exist). The key to portability across PCs — without this step it stalls at
# 'absent' and MM itself can't come up.
COMPOSE_CREATED=0
ensure_created_via_compose() {
  [ "$COMPOSE_CREATED" = "1" ] && return 0   # try only once
  COMPOSE_CREATED=1
  if [ ! -f "$COMPOSE_FILE" ]; then
    err "No containers and no compose definition either: $COMPOSE_FILE"
    err "(docker-compose.yml must be included in the repo for first-time creation on another PC.)"
    exit 1
  fi
  say "Detected missing containers → creating/starting from scratch via docker-compose.yml (bogo-pg, bogo-mm)..."
  compose_up_or_exit
  ok "Container creation/startup via compose complete."
}

ensure_container() {
  local name="$1"
  local st; st="$(container_state "$name")"
  case "$st" in
    running)
      ok "$name already running — skipping." ;;
    absent)
      # First-time creation path: create via compose then re-evaluate the state.
      ensure_created_via_compose
      st="$(container_state "$name")"
      if [ "$st" = "absent" ]; then
        err "$name still absent after compose creation. Diagnose: docker compose -f \"$COMPOSE_FILE\" ps"
        exit 1
      fi
      if [ "$st" != "running" ]; then
        say "$name state=$st → docker start"
        docker start "$name" >/dev/null
      fi
      ok "$name ready (container creation path)." ;;
    *)
      say "$name state=$st → docker start"
      docker start "$name" >/dev/null
      ok "$name started" ;;
  esac
  # Raise the restart policy so the container self-revives when Colima/the machine reboots (idempotent).
  local pol; pol="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$name" 2>/dev/null || echo '')"
  if [ "$pol" != "unless-stopped" ] && [ "$pol" != "always" ]; then
    docker update --restart unless-stopped "$name" >/dev/null 2>&1 \
      && say "$name restart policy → unless-stopped (auto-revive on reboot)" \
      || warn "$name restart policy update failed (safe to ignore)."
  fi
}

# The hostname the MM container's DB DataSource points to (a leftover from the legacy hermes
# rebranding). The MM env var MM_SQLSETTINGS_DATASOURCE references 'hermes-pg', but the actual
# PG container name is 'bogo-pg', so on MM restart/recreation Docker DNS can't find hermes-pg
# and the boot fails indefinitely (no such host). Changing the container env would require
# recreating MM (invasive), so instead we idempotently assign a network alias to PG so that
# 'hermes-pg' resolves to 'bogo-pg'. The alias disappears when the container is recreated, so
# it is guaranteed on every boot.
PG_LEGACY_ALIAS="hermes-pg"

ensure_pg_legacy_alias() {
  # Find the network PG is attached to and, if the hermes-pg alias is absent, assign it (idempotent).
  local nets
  nets="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$PG_NAME" 2>/dev/null || echo '')"
  for net in $nets; do
    # Skip if the hermes-pg alias already exists.
    local aliases
    aliases="$(docker inspect -f "{{range \$k,\$v := .NetworkSettings.Networks}}{{if eq \$k \"$net\"}}{{range \$v.Aliases}}{{.}} {{end}}{{end}}{{end}}" "$PG_NAME" 2>/dev/null || echo '')"
    case " $aliases " in
      *" $PG_LEGACY_ALIAS "*) ok "$PG_NAME ($net) already has the $PG_LEGACY_ALIAS alias — skipping." ;;
      *)
        # Assign the alias via disconnect→connect (preserve existing aliases: the container-name alias is kept automatically).
        if docker network disconnect "$net" "$PG_NAME" >/dev/null 2>&1 \
           && docker network connect --alias "$PG_LEGACY_ALIAS" --alias "$PG_NAME" "$net" "$PG_NAME" >/dev/null 2>&1; then
          ok "$PG_NAME ($net) assigned the $PG_LEGACY_ALIAS alias — guarantees MM DataSource resolution."
        else
          warn "Failed to assign the $PG_LEGACY_ALIAS alias to $PG_NAME. If MM can't connect to the DB, check manually."
        fi
        ;;
    esac
  done
}

ensure_compose_config_current() {
  [ "$(container_state "$MM_NAME")" != "absent" ] || return 0

  local desired_bind desired_site desired_cors actual_ports actual_site actual_cors needs_reconcile=0
  desired_bind="$(env_file_value MM_BIND_HOST)"
  desired_bind="${desired_bind:-127.0.0.1}"
  desired_site="$(env_file_value MM_SITE_URL)"
  desired_site="${desired_site:-http://127.0.0.1:8065}"
  desired_cors="$(env_file_value MM_ALLOW_CORS_FROM)"

  actual_ports="$(docker port "$MM_NAME" 8065/tcp 2>/dev/null || true)"
  if ! printf '%s\n' "$actual_ports" | port_binding_has_host "$desired_bind"; then
    say "$MM_NAME published port does not match MM_BIND_HOST=$desired_bind → reconciling via compose."
    needs_reconcile=1
  fi

  actual_site="$(container_env_value "$MM_NAME" MM_SERVICESETTINGS_SITEURL)"
  if [ -n "$actual_site" ] && [ "$actual_site" != "$desired_site" ]; then
    say "$MM_NAME SiteURL differs from .env ($actual_site → $desired_site) → reconciling via compose."
    needs_reconcile=1
  fi

  actual_cors="$(container_env_value "$MM_NAME" MM_SERVICESETTINGS_ALLOWCORSFROM)"
  if [ "$actual_cors" != "$desired_cors" ]; then
    say "$MM_NAME AllowCorsFrom differs from .env → reconciling via compose."
    needs_reconcile=1
  fi

  [ "$needs_reconcile" = "1" ] || return 0
  compose_up_or_exit
  ok "Compose configuration reconciled with .env."
}

ensure_containers() {
  need docker
  # Check Docker daemon reachability (Linux: daemon/permissions, macOS: via Colima). On failure, advise clearly then exit.
  ensure_docker_reachable
  # DB first (data layer), then MM (app layer).
  ensure_container "$PG_NAME"
  # Ensure the legacy hostname alias before bringing up MM (since MM looks up the DB as hermes-pg).
  ensure_pg_legacy_alias
  ensure_container "$MM_NAME"
  # Existing containers keep their original published ports/env. If net_autodetect changed
  # MM_BIND_HOST/SiteURL/CORS in .env, reconcile now so Linux LAN mode cannot false-succeed.
  ensure_compose_config_current
}

# ── 3. Wait for MM healthy ─────────────────────────────────────────────────
# Ping from inside the container instead of curling from the external host (removes dependence
# on host tools/hooks; whether the container can respond to its own 8065 is the real readiness signal).
wait_mm_ready() {
  say "Waiting for Mattermost healthy (up to ${MM_WAIT_TIMEOUT}s)..."
  local waited=0 code
  local ping_path="/api/v4/system/ping"
  while :; do
    # If docker's internal healthcheck is healthy, pass immediately (the most reliable signal).
    local health
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$MM_NAME" 2>/dev/null || echo 'none')"
    if [ "$health" = "healthy" ]; then
      ok "Mattermost healthy (docker healthcheck, ${waited}s)."
      return 0
    fi
    # If there is no healthcheck or it's undetermined, ping directly from inside the container.
    if [ "$health" = "none" ] || [ "$health" = "starting" ]; then
      code="$(docker exec "$MM_NAME" sh -c "curl -s -o /dev/null -w '%{http_code}' http://localhost:8065${ping_path} 2>/dev/null || wget -q -O /dev/null -S http://localhost:8065${ping_path} 2>&1 | awk '/HTTP\\//{print \$2; exit}'" 2>/dev/null || echo "")"
      if [ "$code" = "200" ]; then
        ok "Mattermost ping 200 (${waited}s)."
        return 0
      fi
    fi
    sleep 3; waited=$((waited + 3))
    if [ "$waited" -ge "$MM_WAIT_TIMEOUT" ]; then
      err "Mattermost was not ready within ${MM_WAIT_TIMEOUT}s (health=$health)."
      err "Diagnose: docker logs --tail 50 $MM_NAME"
      exit 1
    fi
  done
}

# ── 4. Unattended provisioning (new PC: auto-issue token/team/channels) ────
# WHY: even if the communication backbone (MM) is up, the bots can't connect without (a) the
#   admin/team/bot accounts (b) the bot Access Token (c) the channel IDs. Those secrets are
#   git-excluded, so on a new PC where only the folder was moved they are empty. Using mmctl
#   --local (the container's local socket, no auth required) we idempotently create/issue all
#   of them and record them into *_config.json / channels.json.
#   On a PC where the token is already filled in, verify it actually exists then preserve it
#   (no needless reissue/regression).
#   If BOGO_SKIP_PROVISION=1, skip (escape hatch for a manually managed environment).
ensure_provisioned() {
  if [ "${BOGO_SKIP_PROVISION:-0}" = "1" ]; then
    say "BOGO_SKIP_PROVISION=1 → skipping unattended provisioning."
    return 0
  fi
  local prov="$HERE/provision_mm.py"
  if [ ! -f "$prov" ]; then
    say "provision_mm.py missing → skipping provisioning (legacy compatibility)."
    return 0
  fi
  # Prefer the venv python (agent_schema import needed). If absent, fall back to system python3.
  local py="$HERE/.venv/bin/python"
  [ -x "$py" ] || py="$(command -v python3 || true)"
  if [ -z "$py" ]; then
    warn "Could not find python, so skipping provisioning (if the bot token is empty, startup may fail)."
    return 0
  fi
  say "Running unattended provisioning (idempotent issuance of token/team/channels)..."
  if "$py" "$prov"; then
    ok "Provisioning complete (or existing credentials reused)."
  else
    # A provisioning failure is fatal. Without the token, the bots/dashboard can't come up → abort to prevent a false completion.
    err "Unattended provisioning failed. Diagnose: docker exec $MM_NAME mmctl --local system version"
    return 1
  fi
}

main() {
  say "Starting the communication backbone boot dependency chain (Colima → containers → MM readiness → provisioning)."
  ensure_colima
  ensure_containers
  wait_mm_ready
  ensure_provisioned
  ok "Communication backbone ready. Bots can start."
}

main "$@"
