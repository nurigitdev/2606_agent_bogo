#!/usr/bin/env bash
# BOGO cross-platform bootstrap (macOS / Linux).
#
# WHAT: From a fresh git clone or copy on ANY machine, this single command
#   (1) locates a Python interpreter (python3, falling back to python),
#   (2) (re)creates a PORTABLE .venv (no absolute-path pin survives a move),
#   (3) installs requirements.txt,
#   (4) copies *.example -> real config files only when they are missing
#       (existing config/.env are preserved, never overwritten).
#
# Idempotent: safe to re-run. Path-safe: all paths are resolved from
# this script's own location and quoted, so it works even under a directory
# name containing spaces or Unicode characters.
#
# Usage:  ./bootstrap.sh
set -euo pipefail

# Resolve the directory of this script (the repo root = app/), space/UTF-8 safe.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

say() { printf '\033[0;36m[bootstrap]\033[0m %s\n' "$*"; }
err() { printf '\033[0;31m[bootstrap:error]\033[0m %s\n' "$*" >&2; }

# ── 1. Detect the Python interpreter ──────────────────────────────────────────
# Do not parse, compare, or pin versions. Use python3 if present, otherwise python.
# Only when neither exists do we print install guidance and exit.
PY="$(command -v python3 || command -v python || true)"
if [ -z "${PY:-}" ]; then
  err "Could not find a Python interpreter (neither python3 nor python)."
  case "$(uname -s)" in
    Darwin) err "Install:  brew install python" ;;
    Linux)  err "Install (Debian/Ubuntu):  sudo apt install python3 python3-venv" ;;
  esac
  err "After installing, run this script again."
  exit 1
fi
say "Using Python: $PY"

VENV_PY="$HERE/.venv/bin/python"
REQ_STAMP="$HERE/.venv/.bogo_requirements.sha256"

requirements_hash() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$HERE/requirements.txt" | awk '{print $1}'
  else
    shasum -a 256 "$HERE/requirements.txt" | awk '{print $1}'
  fi
}

venv_usable() {
  [ -x "$VENV_PY" ] && "$VENV_PY" -c "import json, sqlite3, urllib.request, websockets" >/dev/null 2>&1
}

# ── 2. Create or reuse the portable venv ───────────────────────────────────────
# If an existing .venv still runs at this path, keep it. This prevents one-click
# setup from deleting a working venv and re-downloading packages later in the
# same startup. If the venv was copied from another PC and its interpreter is
# pinned to an old absolute path, the usability check fails and we recreate it.
if venv_usable; then
  say "Existing .venv is usable — reusing it."
else
  if [ -d "$HERE/.venv" ]; then
    say "Existing .venv is not usable here → recreating it."
    rm -rf "$HERE/.venv"
  fi
  say "Creating .venv..."
  "$PY" -m venv --copies "$HERE/.venv"
fi

# ── 3. Install dependencies ────────────────────────────────────────────────────
req_hash="$(requirements_hash)"
if [ -f "$REQ_STAMP" ] && [ "$(cat "$REQ_STAMP" 2>/dev/null || true)" = "$req_hash" ]; then
  say "Requirements unchanged — skipping pip install."
else
  say "Upgrading pip + installing requirements..."
  "$VENV_PY" -m pip install --upgrade pip >/dev/null
  "$VENV_PY" -m pip install -r "$HERE/requirements.txt"
  printf '%s\n' "$req_hash" > "$REQ_STAMP"
fi

# ── 4. Copy config / .env (only when missing) ─────────────────────────────────
copy_if_missing() {
  local example="$1" real="$2"
  if [ -f "$HERE/$real" ]; then
    say "Kept: $real (already exists)"
  elif [ -f "$HERE/$example" ]; then
    cp "$HERE/$example" "$HERE/$real"
    say "Created: $real  (from $example, fill in the real values)"
  fi
}

# .env (OpenRouter key) -- if no example exists, create an empty template
if [ ! -f "$HERE/.env" ]; then
  if [ -f "$HERE/.env.example" ]; then
    cp "$HERE/.env.example" "$HERE/.env"
    say "Created: .env  (from .env.example, fill in OPENROUTER_API_KEY)"
  fi
fi

copy_if_missing "config/llm_config.json.example"   "llm_config.json"
copy_if_missing "config/nk_config.json.example"    "nk_config.json"
copy_if_missing "config/genz_config.json.example"  "genz_config.json"
copy_if_missing "config/gyaru_config.json.example" "gyaru_config.json"
copy_if_missing "config/channels.json.example"     "channels.json"

# ── 4b. Git hooks: activate repo hooks so the Hangul/cp949 gate runs on every
#        commit in a fresh clone too (core.hooksPath is a local, uncommitted
#        setting, so it must be (re)applied here). ──────────────────────────
if command -v git >/dev/null 2>&1 && git -C "$HERE/.." rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$HERE/.." config core.hooksPath .githooks
  say "Git hooks activated (core.hooksPath=.githooks)"
fi

# ── 5. Guidance ───────────────────────────────────────────────────────────
say "Bootstrap complete."
say "Next steps:"
say "  1) Fill real tokens/keys/channel IDs into .env / *_config.json / channels.json"
say "  2) Register always-on service:  ./bogo_ctl.sh install"
say "  3) Or run manually:  .venv/bin/python bogo_runtime.py orchestrator"
