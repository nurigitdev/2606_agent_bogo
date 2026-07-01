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
#   2) If the bogo-pg, bogo-mm containers are missing, create them via compose; if they
#      exist but are not Up, docker start (data preserved). Raise the restart policy to
#      unless-stopped so they auto-revive when Colima restarts.
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
NET_NAME="bogo-net"
PG_LEGACY_ALIAS="hermes-pg"

# Initial container creation definition (portability). On a machine where the containers
# don't exist at all (like another PC), this compose creates bogo-pg/bogo-mm once. Keep it
# in the same directory as this script.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${BOGO_COMPOSE_FILE:-$HERE/docker-compose.yml}"

# MM healthy wait limit (seconds). An MM cold boot can take tens of seconds.
MM_WAIT_TIMEOUT="${BOGO_MM_WAIT_TIMEOUT:-180}"
# PG healthy wait limit (seconds). The plain Docker fallback has to emulate compose's
# depends_on: service_healthy contract before it starts Mattermost.
PG_WAIT_TIMEOUT="${BOGO_PG_WAIT_TIMEOUT:-90}"
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
container_state() {
  local out
  if ! out="$(docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null)"; then
    printf 'absent\n'
    return 0
  fi
  out="$(printf '%s\n' "$out" | awk 'NF { print; exit }')"
  case "$out" in
    created|restarting|running|removing|paused|exited|dead)
      printf '%s\n' "$out" ;;
    *)
      printf 'absent\n' ;;
  esac
}

container_health() {
  local out
  out="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || echo 'absent')"
  out="$(printf '%s\n' "$out" | awk 'NF { print; exit }')"
  case "$out" in
    healthy|unhealthy|starting|none|absent) printf '%s\n' "$out" ;;
    *) printf 'none\n' ;;
  esac
}

container_networks() {
  docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$1" 2>/dev/null || true
}

container_on_network() {
  local name="$1" net="$2"
  case " $(container_networks "$name") " in
    *" $net "*) return 0 ;;
    *) return 1 ;;
  esac
}

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

docker_volume_ensure() {
  local volume="$1"
  docker volume inspect "$volume" >/dev/null 2>&1 || docker volume create "$volume" >/dev/null
}

docker_network_ensure() {
  docker network inspect "$NET_NAME" >/dev/null 2>&1 || docker network create "$NET_NAME" >/dev/null
}

ensure_pg_network_attachment() {
  [ "$(container_state "$PG_NAME")" != "absent" ] || return 0
  docker_network_ensure
  if container_on_network "$PG_NAME" "$NET_NAME"; then
    return 0
  fi
  say "$PG_NAME is not attached to $NET_NAME → attaching with aliases ($PG_NAME, $PG_LEGACY_ALIAS)."
  if ! docker network connect --alias "$PG_LEGACY_ALIAS" --alias "$PG_NAME" "$NET_NAME" "$PG_NAME" >/dev/null; then
    err "Failed to attach $PG_NAME to $NET_NAME. Mattermost cannot resolve the database without this network."
    exit 1
  fi
}

docker_image_ensure() {
  local image="$1"
  docker image inspect "$image" >/dev/null 2>&1 && return 0
  say "Pulling Docker image: $image (first run may take a while)..."
  if ! docker pull "$image"; then
    err "Could not pull Docker image: $image"
    err "Cause: first-time startup needs internet access, this image must be pre-loaded, or the image may not support this host architecture."
    err "Retry after network access is available, or run: docker pull $image"
    exit 1
  fi
}

wait_pg_ready() {
  local pg_user="$1" pg_db="$2" waited=0 health
  say "Waiting for Postgres healthy (up to ${PG_WAIT_TIMEOUT}s)..."
  while :; do
    health="$(container_health "$PG_NAME")"
    if [ "$health" = "healthy" ] || docker exec "$PG_NAME" pg_isready -U "$pg_user" -d "$pg_db" >/dev/null 2>&1; then
      ok "Postgres ready (${waited}s)."
      return 0
    fi
    sleep 2; waited=$((waited + 2))
    if [ "$waited" -ge "$PG_WAIT_TIMEOUT" ]; then
      err "Postgres was not ready within ${PG_WAIT_TIMEOUT}s (health=$health)."
      err "Diagnose: docker logs --tail 50 $PG_NAME"
      exit 1
    fi
  done
}

run_pg_with_docker_cli() {
  local pg_user="$1" pg_pass="$2" pg_db="$3"
  docker_image_ensure "postgres:15-alpine"
  say "$PG_NAME absent → docker run (Postgres, persistent volume)."
  if ! docker run -d \
    --name "$PG_NAME" \
    --restart unless-stopped \
    --network "$NET_NAME" \
    --network-alias "$PG_LEGACY_ALIAS" \
    -e "POSTGRES_USER=$pg_user" \
    -e "POSTGRES_PASSWORD=$pg_pass" \
    -e "POSTGRES_DB=$pg_db" \
    -v bogo-pg-data:/var/lib/postgresql/data \
    --health-cmd "pg_isready -U $pg_user -d $pg_db" \
    --health-interval 10s \
    --health-timeout 5s \
    --health-retries 10 \
    postgres:15-alpine >/dev/null; then
    err "Failed to create $PG_NAME via Docker CLI. Diagnose: docker logs --tail 50 $PG_NAME"
    exit 1
  fi
}

run_mm_with_docker_cli() {
  local pg_user="$1" pg_pass="$2" pg_db="$3" bind_host="$4" site_url="$5" cors_from="$6"
  local datasource
  docker_image_ensure "mattermost/mattermost-team-edition:9.11"
  datasource="postgres://${pg_user}:${pg_pass}@${PG_LEGACY_ALIAS}:5432/${pg_db}?sslmode=disable&connect_timeout=10"
  say "$MM_NAME absent → docker run (Mattermost, persistent volumes, bind $bind_host:8065)."
  if ! docker run -d \
    --name "$MM_NAME" \
    --restart unless-stopped \
    --network "$NET_NAME" \
    -p "${bind_host}:8065:8065" \
    -e "MM_SQLSETTINGS_DRIVERNAME=postgres" \
    -e "MM_SQLSETTINGS_DATASOURCE=$datasource" \
    -e "MM_SERVICESETTINGS_SITEURL=$site_url" \
    -e "MM_SERVICESETTINGS_ALLOWCORSFROM=$cors_from" \
    -e "MM_SERVICESETTINGS_ENABLELOCALMODE=true" \
    -v bogo-mm-config:/mattermost/config \
    -v bogo-mm-data:/mattermost/data \
    -v bogo-mm-logs:/mattermost/logs \
    -v bogo-mm-plugins:/mattermost/plugins \
    -v bogo-mm-client-plugins:/mattermost/client/plugins \
    --health-cmd "curl -fsS http://localhost:8065/api/v4/system/ping || exit 1" \
    --health-interval 10s \
    --health-timeout 5s \
    --health-retries 20 \
    --health-start-period 60s \
    mattermost/mattermost-team-edition:9.11 >/dev/null; then
    err "Failed to create $MM_NAME via Docker CLI. Diagnose: docker logs --tail 50 $MM_NAME"
    exit 1
  fi
}

docker_cli_up_or_exit() {
  local recreate_mm="${1:-0}"
  local pg_user pg_pass pg_db bind_host site_url cors_from st
  pg_user="$(env_file_value BOGO_PG_USER)"; pg_user="${pg_user:-mmuser}"
  pg_pass="$(env_file_value BOGO_PG_PASSWORD)"; pg_pass="${pg_pass:-mmuser_password}"
  pg_db="$(env_file_value BOGO_PG_DB)"; pg_db="${pg_db:-mattermost}"
  bind_host="$(env_file_value MM_BIND_HOST)"; bind_host="${bind_host:-127.0.0.1}"
  site_url="$(env_file_value MM_SITE_URL)"; site_url="${site_url:-http://127.0.0.1:8065}"
  cors_from="$(env_file_value MM_ALLOW_CORS_FROM)"

  say "Docker Compose is not available → using plain Docker CLI fallback (no package install)."
  docker_network_ensure
  for volume in bogo-pg-data bogo-mm-config bogo-mm-data bogo-mm-logs bogo-mm-plugins bogo-mm-client-plugins; do
    docker_volume_ensure "$volume"
  done

  st="$(container_state "$PG_NAME")"
  if [ "$st" = "absent" ]; then
    run_pg_with_docker_cli "$pg_user" "$pg_pass" "$pg_db"
  elif [ "$st" != "running" ]; then
    say "$PG_NAME state=$st → docker start"
    docker start "$PG_NAME" >/dev/null
  fi
  ensure_pg_network_attachment
  wait_pg_ready "$pg_user" "$pg_db"

  if [ "$recreate_mm" = "1" ] && [ "$(container_state "$MM_NAME")" != "absent" ]; then
    say "$MM_NAME configuration changed → recreating container with existing named volumes via Docker CLI."
    docker stop "$MM_NAME" >/dev/null 2>&1 || true
    docker rm "$MM_NAME" >/dev/null
  fi

  st="$(container_state "$MM_NAME")"
  if [ "$st" = "absent" ]; then
    run_mm_with_docker_cli "$pg_user" "$pg_pass" "$pg_db" "$bind_host" "$site_url" "$cors_from"
  elif [ "$st" != "running" ]; then
    say "$MM_NAME state=$st → docker start"
    docker start "$MM_NAME" >/dev/null
  fi
}

compose_up_or_exit() {
  local recreate_mm="${1:-0}" rc
  set +e
  compose up -d
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    return 0
  fi
  if [ "$rc" = "127" ]; then
    if [ "${BOGO_REQUIRE_COMPOSE:-0}" = "1" ]; then
      err "Could not find docker compose. Docker Desktop/Compose plugin must be installed."
      exit 3
    fi
    docker_cli_up_or_exit "$recreate_mm"
    return 0
  fi
  err "compose up failed (rc=$rc). Diagnose: docker compose -f \"$COMPOSE_FILE\" logs"
  exit 1
}

# If any container is missing, create both from scratch via compose or plain Docker CLI
# fallback (idempotent: no change if they already exist). The key to portability across PCs
# — without this step it stalls at 'absent' and MM itself can't come up.
COMPOSE_CREATED=0
ensure_created_via_compose() {
  [ "$COMPOSE_CREATED" = "1" ] && return 0   # try only once
  COMPOSE_CREATED=1
  if [ ! -f "$COMPOSE_FILE" ]; then
    err "No containers and no compose definition either: $COMPOSE_FILE"
    err "(docker-compose.yml must be included in the repo for first-time creation on another PC.)"
    exit 1
  fi
  say "Detected missing containers → creating/starting backbone (compose if available, Docker CLI fallback otherwise)..."
  if [ "$(container_state "$PG_NAME")" != "absent" ] && [ "$(container_health "$PG_NAME")" = "none" ]; then
    warn "$PG_NAME already exists without a Docker healthcheck → using Docker CLI fallback to avoid compose depends_on failure."
    docker_cli_up_or_exit 0
    ok "Backbone container creation/startup complete."
    return 0
  fi
  compose_up_or_exit 0
  ok "Backbone container creation/startup complete."
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
ensure_pg_legacy_alias() {
  ensure_pg_network_attachment
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
  compose_up_or_exit 1
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
    # For ANY non-healthy state (none/starting/unhealthy), probe readiness directly.
    #  WHY include 'unhealthy' too (root cause): the docker healthcheck (the
    #    `curl -fsS http://localhost:8065/...` injected by compose) only works if curl exists in
    #    the container image. On minimal/distroless-style MM images (observed on a fresh run: no
    #    sh/curl) that healthcheck command cannot even run, so it counts as a failure every time,
    #    and once the retry limit is exceeded the state hardens from starting -> 'unhealthy'. That
    #    is, MM is serving 8065 fine but gets falsely judged 'unhealthy' solely because the
    #    healthcheck tools are absent. Previously this branch was limited to none/starting only, so
    #    the moment it hardened to unhealthy both fallbacks below were skipped and it false-aborted
    #    at [3/5] after burning ${MM_WAIT_TIMEOUT}s.
    #    → For every non-healthy state, check real readiness directly, independent of image tools.
    if [ "$health" != "healthy" ]; then
      # (a) HTTP ping from inside the container (needs sh + curl/wget in the image).
      code="$(docker exec "$MM_NAME" sh -c "curl -s -o /dev/null -w '%{http_code}' http://localhost:8065${ping_path} 2>/dev/null || wget -q -O /dev/null -S http://localhost:8065${ping_path} 2>&1 | awk '/HTTP\\//{print \$2; exit}'" 2>/dev/null || echo "")"
      if [ "$code" = "200" ]; then
        ok "Mattermost ping 200 (${waited}s)."
        return 0
      fi
      # (b) mmctl --local readiness — shell/curl-independent authoritative fallback.
      #    The mmctl binary always exists in every MM image, and provisioning already depends on
      #    this path. If mmctl --local (local unix socket, ENABLELOCALMODE=true) responds, the
      #    server/DB are actually up and able to process commands → a true signal independent of
      #    image tools (sh/curl) and the healthcheck verdict.
      if docker exec "$MM_NAME" mmctl --local system version >/dev/null 2>&1; then
        ok "Mattermost ready (mmctl --local, ${waited}s)."
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
