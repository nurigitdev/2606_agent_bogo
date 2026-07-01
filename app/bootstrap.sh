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
BOOTSTRAP_HELPER_DIR="$HERE/.bogo-bootstrap"
BOOTSTRAP_HELPER_PY="$BOOTSTRAP_HELPER_DIR/bin/python"
PY=""
PY_VERSION=""
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

bootstrap_helper_candidate_commands() {
  if [ -n "${BOGO_BOOTSTRAP_PYTHON:-}" ]; then
    printf '%s\n' "$BOGO_BOOTSTRAP_PYTHON"
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
  if ! pip_check_ok; then
    err "pip check reported a genuine dependency conflict (see the lines above)."
    return 1
  fi
  printf '%s\n' "$REQ_HASH" > "$REQ_STAMP"
}

# ── pip check gate (platform-note tolerant) ────────────────────────────────
# WHY (root cause): 'pip check' lumps two different classes under the same rc=1.
#   (a) Real dependency conflict: "X requires Y, which is not installed" / "has requirement ...,
#       but you have ..." — this genuinely breaks the runtime, so it MUST fail.
#   (b) Platform support note: "<pkg> <ver> is not supported on this platform" — an optional
#       GPU/accelerator transitive (torch -> nvidia-cusparselt-cu13, etc.) reporting itself as
#       'unsupported' on a CPU-only/unsupported architecture (arm64, x86 servers without GPU).
#       This is a normal informational note. The actual BOGO runtime
#       (websockets/hermes-agent/run_agent) does not use these packages, and
#       validate_runtime_dependencies already verified they can be imported.
# Previously any non-zero 'pip check' rc was treated as a fatal failure, so a single (b) note
# from a GPU transitive pulled in by sentence-transformers (optional embeddings) aborted the
# whole fresh-install pipeline at [1/5] (observed on a fresh container). Now only class (b) is
# filtered out and only class (a) real conflicts fail
# → structurally eliminating this bug class (bootstrap abort caused by an optional GPU
#   transitive's platform note).
pip_check_ok() {
  local out rc real
  out="$("$VENV_PY" -m pip check 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    return 0
  fi
  # Keep only the 'real conflict' lines, excluding "... is not supported on this platform" notes.
  # If nothing remains (=platform notes only) pass; otherwise show those lines and fail.
  real="$(printf '%s\n' "$out" | grep -v -E 'is not supported on this platform' | grep -E '.' || true)"
  if [ -z "$real" ]; then
    say "pip check: only platform-unsupported notes from optional GPU/accelerator packages (runtime-irrelevant) — passing."
    return 0
  fi
  printf '%s\n' "$real" >&2
  return 1
}

ensure_virtualenv_helper() {
  local candidate path version compat seen_paths=":"
  if [ -x "$BOOTSTRAP_HELPER_PY" ] && "$BOOTSTRAP_HELPER_PY" -m virtualenv --version >/dev/null 2>&1; then
    return 0
  fi

  rm -rf "$BOOTSTRAP_HELPER_DIR"
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    path="$(resolve_python_candidate "$candidate")"
    [ -n "$path" ] || continue
    case "$seen_paths" in
      *":$path:"*) continue ;;
    esac
    seen_paths="${seen_paths}${path}:"

    version="$(python_version_for "$path" 2>/dev/null || echo unknown)"
    compat="$(python_compat_for "$path" 2>/dev/null || echo unusable)"
    case "$compat" in
      supported|future) ;;
      *) continue ;;
    esac

    say "Preparing virtualenv helper with Python: $path ($version)"
    rm -rf "$BOOTSTRAP_HELPER_DIR"
    if "$path" -m venv --copies "$BOOTSTRAP_HELPER_DIR" \
      && "$BOOTSTRAP_HELPER_PY" -m pip install --upgrade pip virtualenv; then
      return 0
    fi
  done < <(bootstrap_helper_candidate_commands)

  rm -rf "$BOOTSTRAP_HELPER_DIR"
  return 1
}

create_venv_with_virtualenv_helper() {
  local target_python="$1" version="$2"
  say "Python $version at $target_python could not seed pip via venv; trying virtualenv helper."
  rm -rf "$HERE/.venv"
  if ensure_virtualenv_helper \
    && "$BOOTSTRAP_HELPER_PY" -m virtualenv --clear --copies -p "$target_python" "$HERE/.venv"; then
    say "Created .venv for Python $version via virtualenv helper."
    return 0
  fi
  rm -rf "$HERE/.venv"
  return 1
}

venv_has_pip() {
  [ -x "$VENV_PY" ] && "$VENV_PY" -m pip --version >/dev/null 2>&1
}

seed_pip_current_venv() {
  local creator="$1"
  if venv_has_pip; then
    return 0
  fi

  say "Created .venv without pip; trying to seed pip."
  if "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1 && venv_has_pip; then
    return 0
  fi
  if "$creator" -m pip --version >/dev/null 2>&1 && "$creator" -m pip --python "$HERE/.venv" install --upgrade pip >/dev/null 2>&1 && venv_has_pip; then
    return 0
  fi
  return 1
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
    if "$path" -m venv --copies "$HERE/.venv" && seed_pip_current_venv "$path"; then
      :
    else
      rm -rf "$HERE/.venv"
      say "Python $version at $path could not create .venv with seeded pip; retrying without pip seed."
      if "$path" -m venv --copies --without-pip "$HERE/.venv" && seed_pip_current_venv "$path"; then
        :
      elif create_venv_with_virtualenv_helper "$path" "$version"; then
        :
      else
        rm -rf "$HERE/.venv"
        if [ -n "${BOGO_PYTHON:-}" ]; then
          err "BOGO_PYTHON was set to $path, but it could not create .venv with pip."
          err "Tried pip seed methods: venv ensurepip, base pip --python, base virtualenv helper."
          print_python_install_guidance
          exit 1
        fi
        say "Python $version at $path could not create .venv with pip; trying another interpreter."
        continue
      fi
    fi

    PY="$path"
    PY_VERSION="$version"
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
