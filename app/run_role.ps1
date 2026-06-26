# BOGO role launcher (Windows / PowerShell).
# Usage:  pwsh ./run_role.ps1 <orchestrator|hr|dev|admin>
# Loads .env then execs the venv python runtime. Hangul-path safe.
param([Parameter(Mandatory=$true)][string]$Role)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Load .env (KEY=VALUE; ignore comments/blanks) into process environment.
$envFile = Join-Path $Here ".env"
if (Test-Path $envFile) {
  Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
      $k, $v = $line.Split("=", 2)
      [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
    }
  }
}

$VenvPy = Join-Path $Here ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
  Write-Host "[run_role] .venv 가 없습니다. 먼저 .\bootstrap.ps1 를 실행하세요." -ForegroundColor Red
  exit 1
}

# admin = CEO 업데이트 파이프라인(ceo_admin_runtime.py), 나머지는 bogo_runtime.py.
if ($Role -eq "admin") {
  & $VenvPy -u (Join-Path $Here "ceo_admin_runtime.py")
} else {
  & $VenvPy -u (Join-Path $Here "bogo_runtime.py") $Role
}
