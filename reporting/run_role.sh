#!/usr/bin/env bash
# Hermes role launcher (macOS manual / Linux / WSL).
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
cd "$HERE"

# Load .env if present (KEY=VALUE lines; ignores comments/blanks).
if [ -f "$HERE/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$HERE/.env"
  set +a
fi

VENV_PY="$HERE/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "[run_role] .venv 가 없습니다. 먼저 ./bootstrap.sh 를 실행하세요." >&2
  exit 1
fi

# 'admin' = CEO 에이전트 업데이트 파이프라인(ceo_admin_runtime.py), 나머지는 hermes_runtime.py.
if [ "$ROLE" = "admin" ]; then
  exec "$VENV_PY" -u "$HERE/ceo_admin_runtime.py"
fi
exec "$VENV_PY" -u "$HERE/hermes_runtime.py" "$ROLE"
