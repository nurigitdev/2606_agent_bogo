# BOGO single entry point (Windows / PowerShell).
# Wraps bootstrap + service install/uninstall/restart/status + manual run.
#
# Usage:
#   pwsh ./bogo_ctl.ps1 setup            # bootstrap then install service (recommended: first run on a new PC)
#   pwsh ./bogo_ctl.ps1 bootstrap        # venv + deps + config copy only
#   pwsh ./bogo_ctl.ps1 install          # register Task Scheduler tasks
#   pwsh ./bogo_ctl.ps1 uninstall        # remove tasks
#   pwsh ./bogo_ctl.ps1 restart          # restart all roles
#   pwsh ./bogo_ctl.ps1 status           # task state
#   pwsh ./bogo_ctl.ps1 run <role>       # foreground run one role
param([string]$Cmd = "help", [string]$Arg = "")

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Svc  = Join-Path $Here "service\install_service.ps1"

switch ($Cmd) {
  "setup"     { & (Join-Path $Here "bootstrap.ps1"); & $Svc install }
  "bootstrap" { & (Join-Path $Here "bootstrap.ps1") }
  "install"   { & $Svc install }
  "uninstall" { & $Svc uninstall }
  "restart"   { & $Svc restart }
  "status"    { & $Svc status }
  "run" {
    if (-not $Arg) { Write-Host "Role argument required (orchestrator|hr|dev|admin)" -ForegroundColor Red; exit 1 }
    & (Join-Path $Here "run_role.ps1") $Arg
  }
  default {
    @"
BOGO controller (Windows)
  .\bogo_ctl.ps1 setup        Bootstrap then register always-on (recommended: first run on a new PC)
  .\bogo_ctl.ps1 bootstrap    Prepare venv/deps/config only
  .\bogo_ctl.ps1 install      Register Task Scheduler
  .\bogo_ctl.ps1 uninstall    Unregister
  .\bogo_ctl.ps1 restart      Restart all roles
  .\bogo_ctl.ps1 status       Check status
  .\bogo_ctl.ps1 run <role>   Foreground run a single role
"@ | Write-Host
  }
}
