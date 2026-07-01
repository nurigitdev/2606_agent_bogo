# BOGO role launcher (Windows / PowerShell).
# Usage:  pwsh ./run_role.ps1 <orchestrator|hr|dev|admin>
# Loads .env then execs the venv python runtime. Path-safe.
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
  Write-Host "[run_role] .venv is missing. Run .\bootstrap.ps1 first." -ForegroundColor Red
  exit 1
}

# admin = CEO update pipeline (ceo_admin_runtime.py); everything else = bogo_runtime.py.
if ($Role -eq "admin") {
  & $VenvPy -u (Join-Path $Here "ceo_admin_runtime.py")
} else {
  & $VenvPy -u (Join-Path $Here "bogo_runtime.py") $Role
}
