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

# ── 미러 드리프트 방지: 동기화 주체는 여기(데몬)가 아니라 git post-commit 훅 ──────
# launchd 데몬은 ASCII 미러(~/.bogo-bin/app)를 실행한다. 원본(BOGO_REPO=Desktop)만
# 고치고 미러 동기화를 빠뜨리면 데몬이 옛 코드로 도는 stale 사고가 난다(예: mm_client
# 의 localhost→127.0.0.1 수정 누락 → Mattermost ::1 Errno 61 연결 실패).
#
# ★데몬은 스스로 동기화할 수 없다(설계상): macOS TCC 가 launchd 데몬의 ~/Desktop
#   "파일 내용 read" 를 차단한다(access/stat 메타데이터는 허용되어 -r 테스트는 거짓
#   양성이 되지만, 실제 rsync read syscall 은 실패한다). 따라서 런처 안에서 원본→미러
#   rsync 를 시도하면 매 기동마다 실패할 뿐 아니라, 그 실패가 KeepAlive 데몬을 죽여
#   재기동 루프를 유발할 위험이 있다. 그래서 동기화 책임을 런처에서 '완전히 제거'한다.
#
# 미러 동기화의 단일 보장점 = git post-commit 훅(.githooks/post-commit). 커밋은 사용자
# 세션 컨텍스트라 TCC 를 통과하므로, 코드가 커밋되는 순간 훅이 미러를 재동기화하고
# 데몬을 무중단 재시작한다(core.hooksPath=.githooks, install_service.sh 가 자동 등록).
# 보조 가드: install_service.sh status 의 mac_check_mirror_sync 가 드리프트를 가시화.
# 결과: 런처는 '미러를 그대로 실행' 만 하면 되고, 드리프트는 커밋 시점에 이미 제거된다.

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
