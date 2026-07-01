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

VENV_PY="$HERE/.venv/bin/python"
REQ_STAMP="$HERE/.venv/.bogo_requirements.sha256"
PY=""
PY_VERSION=""
PY_COMPAT=""
REQ_HASH=""
REQUIREMENTS_INSTALLED=0

python_version_for() {
  "$1" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'
}

python_compat_for() {
  "$1" -c 'import sys; v=sys.version_info; print("supported" if (v.major, v.minor) >= (3, 10) and (v.major, v.minor) < (3, 14) else ("too_old" if (v.major, v.minor) < (3, 10) else "future"))'
}

resolve_python_candidate() {
  local candidate="$1"
  if [[ "$candidate" == */* ]]; then
    [ -x "$candidate" ] && printf '%s\n' "$candidate"
  else
    command -v "$candidate" 2>/dev/null || true
  fi
}

python_candidate_commands() {
  if [ -n "${BOGO_PYTHON:-}" ]; then
    printf '%s\n' "$BOGO_PYTHON"
    return 0
  fi
  printf '%s\n' python3 python python3.13 python3.12 python3.11 python3.10
}

print_python_install_guidance() {
  case "$(uname -s)" in
    Darwin)
      err "Install a Python with venv support, e.g. brew install python, or set BOGO_PYTHON=/path/to/python."
      ;;
    Linux)
      err "Install a Python with venv support, e.g. sudo apt install python3 python3-venv."
      err "If your distro's python3 is ahead of PyPI packages, install python3.13-venv or python3.12-venv and rerun."
      err "You can also set BOGO_PYTHON=/path/to/python3.13 before launching."
      ;;
    *)
      err "Install Python 3.10+ with venv support, or set BOGO_PYTHON=/path/to/python."
      ;;
  esac
}

requirements_hash() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$HERE/requirements.txt" | awk '{print $1}'
  else
    shasum -a 256 "$HERE/requirements.txt" | awk '{print $1}'
  fi
}

venv_usable() {
  [ -x "$VENV_PY" ] && validate_runtime_dependencies >/dev/null 2>&1
}

validate_runtime_dependencies() {
  "$VENV_PY" - <<'PY'
import importlib
import importlib.metadata as metadata
import sys

missing = []
for dist in ("hermes-agent",):
    try:
        metadata.version(dist)
    except metadata.PackageNotFoundError:
        missing.append(dist)

try:
    importlib.import_module("run_agent")
except Exception:
    missing.append("run_agent")

try:
    import websockets  # noqa: F401
except Exception:
    missing.append("websockets")

if missing:
    print("missing runtime dependency: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
PY
}

install_requirements_current_venv() {
  say "Upgrading pip + installing requirements..."
  if ! "$VENV_PY" -m pip install --upgrade pip >/dev/null; then
    err "pip upgrade failed. Check Python/pip installation or network access, then retry."
    return 1
  fi
  if ! "$VENV_PY" -m pip install -r "$HERE/requirements.txt"; then
    err "Dependency installation failed."
    err "This is usually a Python-version/package compatibility issue or network/package-index problem."
    err "Python in use: $("$VENV_PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo unknown)"
    err "Expected hermes-agent selection: Python 3.10 -> 0.15.x; Python 3.11+ -> latest compatible hermes-agent from PyPI."
    return 1
  fi
  if ! validate_runtime_dependencies; then
    err "Runtime dependency validation failed after pip install."
    err "Python in use: $("$VENV_PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo unknown)"
    return 1
  fi
  if ! "$VENV_PY" -m pip check; then
    err "pip check failed; installed packages have incompatible dependencies."
    return 1
  fi
  printf '%s\n' "$REQ_HASH" > "$REQ_STAMP"
}

create_venv_and_install_with_available_python() {
  local candidate path version compat seen_paths=":" tried_any=0
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    path="$(resolve_python_candidate "$candidate")"
    [ -n "$path" ] || continue
    case "$seen_paths" in
      *":$path:"*) continue ;;
    esac
    seen_paths="${seen_paths}${path}:"
    tried_any=1

    version="$(python_version_for "$path" 2>/dev/null || echo unknown)"
    compat="$(python_compat_for "$path" 2>/dev/null || echo unusable)"
    case "$compat" in
      supported|future) ;;
      too_old)
        say "Skipping Python $version at $path (too old for BOGO dependencies)."
        continue
        ;;
      *)
        say "Skipping Python at $path (could not verify version)."
        continue
        ;;
    esac

    if [ "$compat" = "future" ]; then
      say "Trying future Python $version at $path; pip will decide whether dependencies support it."
    else
      say "Trying Python: $path ($version)"
    fi

    rm -rf "$HERE/.venv"
    if ! "$path" -m venv --copies "$HERE/.venv"; then
      rm -rf "$HERE/.venv"
      if [ -n "${BOGO_PYTHON:-}" ]; then
        err "BOGO_PYTHON was set to $path, but it could not create .venv."
        print_python_install_guidance
        exit 1
      fi
      say "Python $version at $path could not create .venv; trying another interpreter."
      continue
    fi

    PY="$path"
    PY_VERSION="$version"
    PY_COMPAT="$compat"
    say "Using Python: $PY ($PY_VERSION)"
    if install_requirements_current_venv; then
      REQUIREMENTS_INSTALLED=1
      return 0
    fi

    rm -rf "$HERE/.venv"
    if [ -n "${BOGO_PYTHON:-}" ]; then
      print_python_install_guidance
      exit 1
    fi
    say "Python $version at $path could not satisfy BOGO dependencies; trying another interpreter."
  done < <(python_candidate_commands)

  if [ "$tried_any" = "0" ]; then
    err "Could not find a usable Python interpreter."
  else
    err "No available Python candidate could create a working BOGO venv."
  fi
  print_python_install_guidance
  exit 1
}

# ── 2. Create or reuse the portable venv ───────────────────────────────────────
# If an existing .venv still runs at this path, keep it. This prevents one-click
# setup from deleting a working venv and re-downloading packages later in the
# same startup. If the venv was copied from another PC and its interpreter is
# pinned to an old absolute path, the usability check fails and we recreate it.
REQ_HASH="$(requirements_hash)"
if venv_usable; then
  say "Existing .venv is usable — reusing it."
else
  if [ -d "$HERE/.venv" ]; then
    say "Existing .venv is not usable here → recreating it."
    rm -rf "$HERE/.venv"
  fi
  create_venv_and_install_with_available_python
fi

# ── 3. Install dependencies ────────────────────────────────────────────────────
if [ "$REQUIREMENTS_INSTALLED" = "1" ]; then
  :
elif [ -f "$REQ_STAMP" ] && [ "$(cat "$REQ_STAMP" 2>/dev/null || true)" = "$REQ_HASH" ]; then
  say "Requirements unchanged — skipping pip install."
else
  if ! install_requirements_current_venv; then
    exit 1
  fi
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
