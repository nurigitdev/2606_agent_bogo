#!/usr/bin/env bash
# BOGO cross-platform bootstrap (macOS / Linux).
#
# WHAT: From a fresh git clone or copy on ANY machine, this single command
#   (1) locates ANY available Python 3 interpreter (newest preferred; no hard version gate),
#   (2) (re)creates a PORTABLE .venv (no absolute-path pin survives a move),
#   (3) installs requirements.txt,
#   (4) copies *.example -> real config files only when they are missing
#       (existing config/.env are preserved, never overwritten).
#
# Idempotent: safe to re-run. Korean path safe: all paths are resolved from
# this script's own location and quoted, so it works even under a directory
# name containing spaces or Hangul.
#
# Usage:  ./bootstrap.sh
set -euo pipefail

# Resolve the directory of this script (the repo root = app/), space/UTF-8 safe.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

say() { printf '\033[0;36m[bootstrap]\033[0m %s\n' "$*"; }
err() { printf '\033[0;31m[bootstrap:오류]\033[0m %s\n' "$*" >&2; }

# ── 1. Python 3 탐지 (버전 강제 없음) ──────────────────────────────────
# 특정 버전을 강제하지 않는다. 시스템에서 발견되는 python3/python 중 가장 최신을 채택한다.
# 권장 버전(3.12+)은 일부 의존성 wheel 가용성 때문이며, 미만이어도 거부하지 않고 경고만 출력한다.
# 실패는 "Python 인터프리터를 전혀 못 찾았을 때"뿐이다.
RECOMMENDED_MINOR=12   # recommended minimum minor for the 3.x line (advisory only)
find_py() {
  # newest-first candidate list; explicit-version names take precedence over generic.
  local cands="python3.14 python3.13 python3.12 python3 python"
  local best="" best_ver=""
  for cand in $cands; do
    if command -v "$cand" >/dev/null 2>&1; then
      ver="$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "")"
      [ -z "$ver" ] && continue
      # accept ANY working Python interpreter; keep the highest version seen.
      if [ -z "$best" ] || [ "$(printf '%s\n%s\n' "$best_ver" "$ver" | sort -V | tail -1)" = "$ver" ]; then
        best="$cand"; best_ver="$ver"
      fi
    fi
  done
  [ -n "$best" ] && { echo "$best"; return 0; }
  return 1
}

PY="$(find_py || true)"
if [ -z "${PY:-}" ]; then
  err "Python 인터프리터를 찾지 못했습니다(python3/python 모두 없음)."
  case "$(uname -s)" in
    Darwin) err "설치:  brew install python  (가능하면 3.12 이상 권장)" ;;
    Linux)  err "설치(Debian/Ubuntu):  sudo apt install python3 python3-venv  (가능하면 3.12 이상 권장)" ;;
  esac
  err "설치 후 이 스크립트를 다시 실행하세요."
  exit 1
fi
PY_VER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "?.?")"
say "Python $PY_VER 사용: $("$PY" -c 'import sys;print(sys.executable)')"
# advisory-only: warn (do NOT abort) when below the recommended 3.12 line.
if "$PY" -c "import sys;sys.exit(0 if sys.version_info[:2] >= (3,$RECOMMENDED_MINOR) else 1)" 2>/dev/null; then
  :
else
  err "경고: 권장 Python 3.$RECOMMENDED_MINOR+ 미만(현재 $PY_VER) — 일부 의존성 wheel 이 없을 수 있습니다. 계속 진행합니다."
fi

# ── 2. 휴대용 venv (재)생성 ────────────────────────────────────────────
# 기존 .venv 가 다른 PC에서 복사돼 왔거나 절대경로가 핀되어 있으면 신뢰 불가 →
# 무조건 새로 만든다. --copies 로 심볼릭링크 대신 바이너리를 복사해 이식성을 높인다.
if [ -d "$HERE/.venv" ]; then
  say "기존 .venv 제거 후 재생성(절대경로 핀 제거 목적)"
  rm -rf "$HERE/.venv"
fi
say ".venv 생성 중..."
"$PY" -m venv --copies "$HERE/.venv"

VENV_PY="$HERE/.venv/bin/python"

# ── 3. 의존성 설치 ────────────────────────────────────────────────────
say "pip 업그레이드 + requirements 설치 중..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install -r "$HERE/requirements.txt"

# ── 4. config / .env 복사 (없을 때만) ─────────────────────────────────
copy_if_missing() {
  local example="$1" real="$2"
  if [ -f "$HERE/$real" ]; then
    say "보존: $real (이미 존재)"
  elif [ -f "$HERE/$example" ]; then
    cp "$HERE/$example" "$HERE/$real"
    say "생성: $real  (← $example, 실제 값으로 채우세요)"
  fi
}

# .env (OpenRouter 키) — example 없으면 빈 템플릿 생성
if [ ! -f "$HERE/.env" ]; then
  if [ -f "$HERE/.env.example" ]; then
    cp "$HERE/.env.example" "$HERE/.env"
    say "생성: .env  (← .env.example, OPENROUTER_API_KEY 채우세요)"
  fi
fi

copy_if_missing "config/llm_config.json.example"   "llm_config.json"
copy_if_missing "config/nk_config.json.example"    "nk_config.json"
copy_if_missing "config/genz_config.json.example"  "genz_config.json"
copy_if_missing "config/gyaru_config.json.example" "gyaru_config.json"
copy_if_missing "config/channels.json.example"     "channels.json"

# ── 5. 안내 ───────────────────────────────────────────────────────────
say "부트스트랩 완료."
say "다음 단계:"
say "  1) .env / *_config.json / channels.json 에 실제 토큰·키·채널ID 입력"
say "  2) 상시 가동 등록:  ./bogo_ctl.sh install"
say "  3) 또는 수동 실행:  .venv/bin/python bogo_runtime.py orchestrator"
