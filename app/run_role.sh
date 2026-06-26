#!/usr/bin/env bash
# BOGO role launcher (macOS manual / Linux / WSL).
# Usage: run_role.sh <orchestrator|hr|dev|admin|dashboard>
#
# Loads .env (OPENROUTER_API_KEY etc.) then execs the venv python runtime.
# Path-safe: resolves its own dir, so it works under spaces / Hangul folder names.
# Secrets stay in .env (gitignored); this wrapper is safe to commit.
set -eu

ROLE="${1:?역할 인자가 필요합니다 (orchestrator|hr|dev|admin|dashboard)}"

# Resolve this script's directory (works under any path incl. spaces / Hangul).
SRC="${BASH_SOURCE[0]:-$0}"
HERE="$(cd "$(dirname "$SRC")" && pwd)"

# Layout-aware app root resolution.
#   In-place run    : this script sits AT the app root (.venv/.env beside it)  -> APP=$HERE
#   macOS mirror run : this script is copied to ~/.bogo-bin/run_role.sh while
#                      the app contents are mirrored to ~/.bogo-bin/app/      -> APP=$HERE/app
# Pick whichever directory actually holds the runtime (bogo_runtime.py).
if [ -f "$HERE/bogo_runtime.py" ]; then
  APP="$HERE"
elif [ -f "$HERE/app/bogo_runtime.py" ]; then
  APP="$HERE/app"
else
  echo "[run_role] 앱 루트를 찾지 못했습니다 (bogo_runtime.py 없음): $HERE" >&2
  exit 1
fi
cd "$APP"

# Load .env if present (KEY=VALUE lines; ignores comments/blanks).
if [ -f "$APP/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$APP/.env"
  set +a
fi

VENV_PY="$APP/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "[run_role] .venv 가 없습니다. 먼저 ./bootstrap.sh 를 실행하세요." >&2
  exit 1
fi

# 'dashboard' = CEO 대시보드 웹서버(ceo_dashboard.py, 127.0.0.1:8642 루프백 전용).
# 봇 4역할과 달리 장기 실행 HTTP 서버이며, launchd(com.bogo.dashboard)가 상시 소유한다.
# 루프백 바인딩(HOST=127.0.0.1)은 ceo_dashboard.py 가 하드코딩하므로 여기서 강제하지 않는다.
if [ "$ROLE" = "dashboard" ]; then
  : "${BOGO_DASHBOARD_PORT:=8642}"   # plist EnvironmentVariables 가 주지 않으면 기본 8642.
  export BOGO_DASHBOARD_PORT
  exec "$VENV_PY" -u "$APP/ceo_dashboard.py"
fi
# 'admin' = 역할별 학습방 개조 봇(ceo_admin_runtime.py), 나머지는 bogo_runtime.py.
if [ "$ROLE" = "admin" ]; then
  exec "$VENV_PY" -u "$APP/ceo_admin_runtime.py"
fi
exec "$VENV_PY" -u "$APP/bogo_runtime.py" "$ROLE"
