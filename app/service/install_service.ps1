# BOGO always-on service installer (Windows / Task Scheduler).
#
# Registers 4 scheduled tasks (BOGO_<role>) that start at logon and restart on
# failure. Username-agnostic (uses $env:USERNAME / current principal) and runs the
# repo IN PLACE — Windows handles Hangul paths, no ASCII mirror needed.
#
# Usage:
#   pwsh ./service/install_service.ps1 install
#   pwsh ./service/install_service.ps1 uninstall
#   pwsh ./service/install_service.ps1 restart
#   pwsh ./service/install_service.ps1 status
param([string]$Action = "install")

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Self = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Split-Path -Parent $Self
$Roles = @("orchestrator", "hr", "dev", "admin")

function Say($m) { Write-Host "[service] $m" -ForegroundColor Cyan }

# Resolve a PowerShell executable to host the launcher (pwsh preferred).
$PwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue)?.Source
if (-not $PwshExe) { $PwshExe = (Get-Command powershell).Source }
$Runner = Join-Path $Repo "run_role.ps1"

function Install-All {
  foreach ($r in $Roles) {
    $name = "BOGO_$r"
    $args = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" $r"
    $action  = New-ScheduledTaskAction -Execute $PwshExe -Argument $args -WorkingDirectory $Repo
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    # Restart on failure (crash / memory): up to 999 times every 1 minute, run indefinitely.
    $settings = New-ScheduledTaskSettingsSet -RestartInterval (New-TimeSpan -Minutes 1) `
                  -RestartCount 999 -ExecutionTimeLimit ([TimeSpan]::Zero) `
                  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
      -Settings $settings -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName $name
    Say "등록+기동: $name"
  }
  Say "Windows Task Scheduler 설치 완료. 상태:  .\service\install_service.ps1 status"
}

function Uninstall-All {
  foreach ($r in $Roles) {
    $name = "BOGO_$r"
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
      Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
      Unregister-ScheduledTask -TaskName $name -Confirm:$false
      Say "해제: $name"
    }
  }
  Say "Task Scheduler 등록 해제 완료."
}

function Restart-All {
  foreach ($r in $Roles) {
    $name = "BOGO_$r"
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $name
    Say "재시작: $name"
  }
}

function Status-All {
  foreach ($r in $Roles) {
    $name = "BOGO_$r"
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($t) { Write-Host ("{0,-18} {1}" -f $name, $t.State) }
    else    { Write-Host ("{0,-18} (미등록)" -f $name) }
  }
}

switch ($Action) {
  "install"   { Install-All }
  "uninstall" { Uninstall-All }
  "restart"   { Restart-All }
  "status"    { Status-All }
  default     { Write-Host "알 수 없는 명령: $Action (install|uninstall|restart|status)" -ForegroundColor Red; exit 1 }
}
