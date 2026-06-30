#!/usr/bin/env bash
# BOGO cross-platform bootstrap (macOS / Linux).
#
# WHAT: From a fresh git clone or copy on ANY machine, this single command
#   (1) locates a Python >= 3.12 interpreter (newest available preferred),
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

# ── 1. Python >=3.12 탐지 ──────────────────────────────────────────────
# requirements.txt 는 3.12 이상 wheel 을 요구한다(하한 3.12). 3.12 이상이면 채택하되
# 후보 중 가장 최신 버전을 우선 선택한다(3.12 미만만 거부).
find_py() {
  # newest-first candidate list; explicit-version names take precedence over generic.
  local cands="python3.14 python3.13 python3.12 python3 python"
  local best="" best_ver=""
  for cand in $cands; do
    if command -v "$cand" >/dev/null 2>&1; then
      ver="$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "")"
      [ -z "$ver" ] && continue
      # require >= 3.12
      if "$cand" -c 'import sys;sys.exit(0 if sys.version_info[:2] >= (3,12) else 1)' 2>/dev/null; then
        # keep the highest version seen (string compare is safe via sort -V below)
        if [ -z "$best" ] || [ "$(printf '%s\n%s\n' "$best_ver" "$ver" | sort -V | tail -1)" = "$ver" ]; then
          best="$cand"; best_ver="$ver"
        fi
      fi
    fi
  done
  [ -n "$best" ] && { echo "$best"; return 0; }
  return 1
}

PY="$(find_py || true)"
if [ -z "${PY:-}" ]; then
  err "Python 3.12 이상 인터프리터를 찾지 못했습니다."
  case "$(uname -s)" in
    Darwin) err "설치:  brew install python@3.12  (또는 그 이상 버전)" ;;
    Linux)  err "설치(Debian/Ubuntu):  sudo apt install python3.12 python3.12-venv  (또는 그 이상 버전)" ;;
  esac
  err "설치 후 이 스크립트를 다시 실행하세요."
  exit 1
fi
say "Python $("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])') 사용: $("$PY" -c 'import sys;print(sys.executable)')"

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
