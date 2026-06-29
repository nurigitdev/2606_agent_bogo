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

# ── 미러 자동 재동기화 (드리프트 원천 차단; TCC 한계 인지) ───────────────────
# launchd 데몬은 ASCII 미러(~/.bogo-bin/app)를 실행한다. 원본(BOGO_REPO=Desktop)만
# 고치고 미러 동기화를 빠뜨리면 데몬이 옛 코드로 도는 stale 사고가 난다(예: mm_client
# 의 localhost→127.0.0.1 수정 누락 → Mattermost ::1 Errno 61 연결 실패).
#
# ★macOS TCC 현실: launchd 가 띄운 데몬은 ~/Desktop "파일 내용 read" 가 TCC 로 차단된다
#   (디렉토리 stat 은 되지만 rsync 의 파일 read 는 실패). 그래서 '데몬 자신이 시작 시
#   원본을 끌어오는' 방식은 launchd 컨텍스트에선 구조적으로 불가능하다. 미러 동기화의
#   '진짜' 보장은 사용자 세션(TCC 통과)에서 도는 git post-commit 훅이 담당한다
#   (.githooks/post-commit → 커밋 시 미러 재동기화 + 데몬 재시작). 이로써 코드 변경이
#   커밋되는 순간 미러 드리프트가 구조적으로 제거된다.
#
# 아래 동기화는 그 보강(best-effort)이다: 원본을 read 할 수 있는 컨텍스트(= 사용자가
# 직접 run_role.sh 를 미러에서 호출하거나, 향후 TCC 권한이 부여된 환경)에서만 성공하고,
# 데몬처럼 read 가 막힌 컨텍스트에선 조용히 미러 기존 사본으로 기동한다(데몬을 막지 않음).
if [ "$APP" = "$HERE/app" ] && [ -n "${BOGO_REPO:-}" ] && [ -d "$BOGO_REPO" ] \
   && [ -r "$BOGO_REPO/bogo_runtime.py" ] && command -v rsync >/dev/null 2>&1; then
  # -r 테스트로 '내용 read 가능' 을 먼저 확인 → TCC 로 막힌 데몬에선 이 블록을 건너뛰어
  # 불필요한 rsync 실패 로그 소음을 내지 않는다(데몬은 githook 동기화 결과를 그대로 씀).
  _sync_mirror() {
    # .venv/.env/logs/캐시는 미러 고유 자산이라 제외(원본 venv 는 Hangul 경로에 핀됨).
    rsync -a \
      --exclude '__pycache__/' --exclude '.ruff_cache/' --exclude '.pytest_cache/' \
      --exclude '.git/' --exclude '.venv/' --exclude '.env' --exclude '*.bak' \
      --exclude 'logs/' \
      "$BOGO_REPO"/ "$APP"/ 2>/dev/null \
      && echo "[run_role] 미러 자동 동기화: $BOGO_REPO -> $APP" >&2
  }
  _lock="${HOME}/.bogo-bin/.sync.lock"
  if command -v flock >/dev/null 2>&1; then
    # 동시성: 봇 4역할 + 대시보드 동시 기동 시 같은 미러 rsync 가 겹친다 → flock 직렬화.
    # 최대 30초 대기 후 락 실패해도 기존 미러로 기동(데몬을 막지 않는다). 재동기화는 멱등.
    ( flock -w 30 9 || exit 0; _sync_mirror ) 9>"$_lock" || true
  else
    _sync_mirror
  fi
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
