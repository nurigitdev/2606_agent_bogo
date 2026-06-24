# Hermes single entry point (Windows / PowerShell).
# Wraps bootstrap + service install/uninstall/restart/status + manual run.
#
# Usage:
#   pwsh ./hermes_ctl.ps1 setup            # bootstrap then install service (권장: 새 PC 첫 실행)
#   pwsh ./hermes_ctl.ps1 bootstrap        # venv + deps + config copy only
#   pwsh ./hermes_ctl.ps1 install          # register Task Scheduler tasks
#   pwsh ./hermes_ctl.ps1 uninstall        # remove tasks
#   pwsh ./hermes_ctl.ps1 restart          # restart all roles
#   pwsh ./hermes_ctl.ps1 status           # task state
#   pwsh ./hermes_ctl.ps1 run <role>       # foreground run one role
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
    if (-not $Arg) { Write-Host "역할 인자 필요 (orchestrator|hr|dev|admin)" -ForegroundColor Red; exit 1 }
    & (Join-Path $Here "run_role.ps1") $Arg
  }
  default {
    @"
Hermes 컨트롤러 (Windows)
  .\hermes_ctl.ps1 setup        부트스트랩 후 상시 가동 등록 (권장: 새 PC 첫 실행)
  .\hermes_ctl.ps1 bootstrap    venv/의존성/config 준비만
  .\hermes_ctl.ps1 install      Task Scheduler 등록
  .\hermes_ctl.ps1 uninstall    등록 해제
  .\hermes_ctl.ps1 restart      전체 역할 재시작
  .\hermes_ctl.ps1 status       상태 확인
  .\hermes_ctl.ps1 run <role>   단일 역할 포그라운드 실행
"@ | Write-Host
  }
}
