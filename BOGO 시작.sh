#!/usr/bin/env bash
# Thin wrapper — delegates to launchers/BOGO 시작.sh (실제 런처)
# 프로젝트 루트에서 더블클릭/터미널 접근성을 보장하기 위한 1줄 위임 셸.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
exec bash "$SELF_DIR/launchers/BOGO 시작.sh" "$@"
