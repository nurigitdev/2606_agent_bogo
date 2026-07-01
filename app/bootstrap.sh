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
# Use python3 if present, otherwise python. Dependency support is checked only
# when this script needs to create/recreate the local venv; an already working
# .venv can continue to run without caring about the system interpreter.
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

python_version() {
  "$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'
}

python_compat() {
  "$PY" -c 'import sys; v=sys.version_info; print("ok" if (v.major, v.minor) >= (3, 10) and (v.major, v.minor) < (3, 14) else ("too_old" if (v.major, v.minor) < (3, 10) else "too_new"))'
}

require_supported_python_for_new_venv() {
  local py_ver compat
  py_ver="$(python_version 2>/dev/null || echo unknown)"
  compat="$(python_compat 2>/dev/null || echo too_old)"
  case "$compat" in
    ok) return 0 ;;
    too_old)
      err "Python $py_ver is too old for BOGO dependencies."
      err "Use Python 3.10-3.13. Python 3.10 selects hermes-agent==0.15.2; Python 3.11-3.13 selects hermes-agent==0.17.0."
      ;;
    too_new)
      err "Python $py_ver is newer than the supported hermes-agent range."
      err "Use Python 3.10-3.13 until hermes-agent publishes support for this Python version."
      ;;
    *)
      err "Could not verify Python compatibility for: $PY"
      ;;
  esac
  exit 1
}

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
  require_supported_python_for_new_venv
  say "Creating .venv..."
  if ! "$PY" -m venv --copies "$HERE/.venv"; then
    err "Could not create .venv with Python $(python_version 2>/dev/null || echo unknown) at: $PY"
    case "$(uname -s)" in
      Linux) err "If Debian/Ubuntu reports ensurepip or venv missing, install the matching venv package, e.g. sudo apt install python3-venv." ;;
      Darwin) err "Install or repair Python with venv support, e.g. brew install python." ;;
    esac
    exit 1
  fi
fi

# ── 3. Install dependencies ────────────────────────────────────────────────────
req_hash="$(requirements_hash)"
if [ -f "$REQ_STAMP" ] && [ "$(cat "$REQ_STAMP" 2>/dev/null || true)" = "$req_hash" ]; then
  say "Requirements unchanged — skipping pip install."
else
  say "Upgrading pip + installing requirements..."
  if ! "$VENV_PY" -m pip install --upgrade pip >/dev/null; then
    err "pip upgrade failed. Check Python/pip installation or network access, then retry."
    exit 1
  fi
  if ! "$VENV_PY" -m pip install -r "$HERE/requirements.txt"; then
    err "Dependency installation failed."
    err "This is usually a Python-version/package compatibility issue or network/package-index problem."
    err "Python in use: $("$VENV_PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo unknown)"
    err "Expected hermes-agent selection: Python 3.10 -> 0.15.2; Python 3.11-3.13 -> 0.17.0."
    err "If pip tries hermes-agent==0.17.0 on Python 3.10, use this updated requirements.txt with Python markers and retry."
    exit 1
  fi
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
[ -f "$HERE/.env" ] && chmod go-rwx "$HERE/.env" 2>/dev/null || true

copy_if_missing "config/llm_config.json.example"   "llm_config.json"
copy_if_missing "config/nk_config.json.example"    "nk_config.json"
copy_if_missing "config/genz_config.json.example"  "genz_config.json"
copy_if_missing "config/gyaru_config.json.example" "gyaru_config.json"
copy_if_missing "config/channels.json.example"     "channels.json"
for secret_file in "$HERE"/*_config.json "$HERE/channels.json" "$HERE/employees.json"; do
  [ -f "$secret_file" ] && chmod go-rwx "$secret_file" 2>/dev/null || true
done

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
