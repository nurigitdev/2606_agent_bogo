# BOGO cross-platform bootstrap (Windows / PowerShell).
#
# From a fresh git clone or copy: locates a Python interpreter (py / python / python3), (re)creates a PORTABLE .venv,
# installs requirements.txt, copies *.example -> real config only when missing.
# Idempotent + Hangul-path safe (paths resolved from script location, UTF-8).
#
# Usage:  pwsh ./bootstrap.ps1     (or)   powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Say($m) { Write-Host "[bootstrap] $m" -ForegroundColor Cyan }
function Die($m) { Write-Host "[bootstrap:ERROR] $m" -ForegroundColor Red; exit 1 }

# ── 1. Detect a Python interpreter ────────────────────────────────────────
# Do not parse/compare/enforce a version. Adopt the first of py / python / python3 found.
# Only when none exist, print install guidance and exit.
function Find-Py {
  # py launcher first (Windows standard), then python / python3 on PATH.
  if (Get-Command py -ErrorAction SilentlyContinue)      { return @("py") }
  if (Get-Command python -ErrorAction SilentlyContinue)  { return @("python") }
  if (Get-Command python3 -ErrorAction SilentlyContinue) { return @("python3") }
  return $null
}

$Py = Find-Py
if (-not $Py) {
  Die "No Python interpreter found (neither python nor py). Install from https://www.python.org/downloads/ (check 'Add to PATH' during install), then run again."
}
Say "Using Python: $($Py -join ' ')"

# ── 2. (Re)create a portable venv ──────────────────────────────────────
$Venv = Join-Path $Here ".venv"
if (Test-Path $Venv) { Say "Removing and recreating the existing .venv"; Remove-Item -Recurse -Force $Venv }
Say "Creating .venv..."
& $Py[0] -m venv --copies $Venv

$VenvPy = Join-Path $Venv "Scripts\python.exe"

# ── 3. Install dependencies ───────────────────────────────────────────────────
Say "Upgrading pip + installing requirements..."
& $VenvPy -m pip install --upgrade pip | Out-Null
& $VenvPy -m pip install -r (Join-Path $Here "requirements.txt")

# ── 4. Copy config / .env (only when missing) ───────────────────────────────
function Copy-IfMissing($example, $real) {
  $rp = Join-Path $Here $real
  $ep = Join-Path $Here $example
  if (Test-Path $rp)      { Say "Kept: $real (already exists)" }
  elseif (Test-Path $ep)  { Copy-Item $ep $rp; Say "Created: $real (<- $example, fill in the real values)" }
}
if (-not (Test-Path (Join-Path $Here ".env"))) {
  $ee = Join-Path $Here ".env.example"
  if (Test-Path $ee) { Copy-Item $ee (Join-Path $Here ".env"); Say "Created: .env (<- .env.example)" }
}
Copy-IfMissing "config\llm_config.json.example"   "llm_config.json"
Copy-IfMissing "config\nk_config.json.example"    "nk_config.json"
Copy-IfMissing "config\genz_config.json.example"  "genz_config.json"
Copy-IfMissing "config\gyaru_config.json.example" "gyaru_config.json"
Copy-IfMissing "config\channels.json.example"     "channels.json"

# Git hooks: activate repo hooks so the Hangul/cp949 gate runs on every commit
# in a fresh clone too (core.hooksPath is a local, uncommitted setting).
if (Get-Command git -ErrorAction SilentlyContinue) {
  $Repo = Split-Path -Parent $Here
  & git -C $Repo rev-parse --git-dir *> $null
  if ($LASTEXITCODE -eq 0) {
    & git -C $Repo config core.hooksPath .githooks
    Say "Git hooks activated (core.hooksPath=.githooks)"
  }
}

Say "Bootstrap complete."
Say "Next steps:"
Say "  1) Fill real values into .env / *_config.json / channels.json"
Say "  2) Register always-on:  .\bogo_ctl.ps1 install"
Say "  3) Or run manually:  .venv\Scripts\python.exe bogo_runtime.py orchestrator"
