#!/bin/zsh
# Top-level double-click entry point -- the real launcher lives in launchers/
exec zsh "$(dirname "$0:A")/launchers/BOGO_start.command"
