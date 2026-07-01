#!/usr/bin/env bash
# Thin wrapper -- delegates to launchers/BOGO_start.sh (the real launcher).
# A one-line delegating shell to guarantee double-click / terminal access from the project root.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
exec bash "$SELF_DIR/launchers/BOGO_start.sh" "$@"
