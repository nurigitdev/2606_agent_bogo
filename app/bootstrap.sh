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

# ── 2. (Re)create the portable venv ────────────────────────────────────────────
# If an existing .venv was copied from another PC or has an absolute path pinned, it cannot be
# trusted → recreate it unconditionally. --copies copies binaries instead of symlinks for portability.
if [ -d "$HERE/.venv" ]; then
  say "Removing and recreating the existing .venv (to drop pinned absolute paths)"
  rm -rf "$HERE/.venv"
fi
say "Creating .venv..."
"$PY" -m venv --copies "$HERE/.venv"

VENV_PY="$HERE/.venv/bin/python"

# ── 3. Install dependencies ────────────────────────────────────────────────────
say "Upgrading pip + installing requirements..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install -r "$HERE/requirements.txt"

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

# ── 5. Guidance ───────────────────────────────────────────────────────────
say "Bootstrap complete."
say "Next steps:"
say "  1) Fill real tokens/keys/channel IDs into .env / *_config.json / channels.json"
say "  2) Register always-on service:  ./bogo_ctl.sh install"
say "  3) Or run manually:  .venv/bin/python bogo_runtime.py orchestrator"
