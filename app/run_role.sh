#!/usr/bin/env bash
# BOGO role launcher (macOS manual / Linux / WSL).
# Usage: run_role.sh <orchestrator|hr|dev|admin>
#
# Loads .env (OPENROUTER_API_KEY etc.) then execs the venv python runtime.
# Path-safe: resolves its own dir, so it works under spaces / Hangul folder names.
# Secrets stay in .env (gitignored); this wrapper is safe to commit.
set -eu

ROLE="${1:?역할 인자가 필요합니다 (orchestrator|hr|dev|admin)}"

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

# 'admin' = 역할별 학습방 개조 봇(ceo_admin_runtime.py), 나머지는 bogo_runtime.py.
if [ "$ROLE" = "admin" ]; then
  exec "$VENV_PY" -u "$APP/ceo_admin_runtime.py"
fi
exec "$VENV_PY" -u "$APP/bogo_runtime.py" "$ROLE"
