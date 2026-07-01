# ════════════════════════════════════════════════════════════════════════
#  BOGO Start (Windows launcher body) -- the detect/branch logic invoked by the .bat
# ════════════════════════════════════════════════════════════════════════
#  WHAT  Double-click -> this script:
#    1) not registered -> bogo_ctl.ps1 setup (venv+deps+config+Task Scheduler registration+start)
#    2) already always-on -> bogo_ctl.ps1 restart (redeploy latest code + restart 4 roles)
#    3) then prints the current status (Task State)
#  Design rationale: 1:1 equivalent of mac 'BOGO_start.command'. Since Task Scheduler owns
#    the always-on lifecycle, this removes the toil of typing pwsh ./bogo_ctl.ps1
#    {setup|restart|status} every time, replacing it with one click. If already registered,
#    it only redeploys+restarts without re-registering (duplicate prevention).
#  Path-safe: $PSScriptRoot resolves its own location dynamically -> spaced/Unicode path safe.
# ════════════════════════════════════════════════════════════════════════
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ── 0. Pin the app/ directory relative to this script's location (spaced/Unicode path safe) ──
$SelfDir = $PSScriptRoot
$Repo    = [System.IO.Path]::GetFullPath((Join-Path $SelfDir "..\app"))
$Ctl     = Join-Path $Repo "bogo_ctl.ps1"

function Say($m)  { Write-Host "[BOGO] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m"    -ForegroundColor Green }
function Fail($m) { Write-Host "[ERROR] $m" -ForegroundColor Red }

# Pass the exit code back to the .bat (which decides whether to pause based on this value)
function Stop-With($code) { exit [int]$code }

Write-Host ""
Say "Starting the BOGO always-on launcher (Windows)"
Say "Location: $Repo"
Write-Host ""

# ── 1. Preflight: verify the controller + a PowerShell host ─────────────
if (-not (Test-Path $Ctl)) {
  Fail "bogo_ctl.ps1 not found: $Ctl"
  Fail "This launcher file must sit in the project root that contains the 'app' folder."
  Stop-With 1
}

# Prefer pwsh (7+); fall back to the built-in Windows powershell 5.1
$PwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue)?.Source
if (-not $PwshExe) { $PwshExe = (Get-Command powershell -ErrorAction SilentlyContinue)?.Source }
if (-not $PwshExe) {
  Fail "No PowerShell executable found (neither pwsh nor powershell)."
  Stop-With 1
}

# Invoke a single bogo_ctl.ps1 command via a child PowerShell (bypass ExecutionPolicy + preserve encoding)
function Invoke-Ctl([string]$SubCmd) {
  & $PwshExe -NoProfile -ExecutionPolicy Bypass -File $Ctl $SubCmd
  return $LASTEXITCODE
}

# ── 2. Determine current state (already registered?) ────────────────────
#   If Task Scheduler has any BOGO_* task, it is already installed/registered.
#   (Windows equivalent of mac 'launchctl list | grep com.bogo')
$BogoTasks = @(Get-ScheduledTask -TaskName "BOGO_*" -ErrorAction SilentlyContinue)
$RegisteredCount = $BogoTasks.Count

if ($RegisteredCount -ge 1) {
  # ── 2-a. Already registered -> no re-registration, redeploy+restart only ──
  Say "Already registered as always-on (registered roles: $RegisteredCount)."
  Say "Redeploying the latest code and restarting the 4 roles..."
  Write-Host ""
  $rc = Invoke-Ctl "restart"
  if ($rc -eq 0) {
    Write-Host ""
    Ok "Redeploy + restart complete."
  } else {
    Write-Host ""
    Fail "A problem occurred during restart. Check the log above."
    Say "Manual diagnosis:  cd `"$Repo`"; .\bogo_ctl.ps1 status"
    Stop-With 1
  }
} else {
  # ── 2-b. Not registered -> first-time install (bootstrap + Task Scheduler registration + start) ──
  Say "Always-on is not registered yet. Running the first-time install."
  Say "(create venv + install deps + prepare config + register Task Scheduler -- may take a few minutes)"
  Write-Host ""
  $rc = Invoke-Ctl "setup"
  if ($rc -eq 0) {
    Write-Host ""
    Ok "Install + always-on registration complete."
    Say "If this is the first run, fill in the real tokens/keys/channel IDs in app\.env, *_config.json, channels.json, then"
    Say "double-click this file once more to restart with the new settings."
  } else {
    Write-Host ""
    Fail "A problem occurred during install. Check the log above."
    Say "Common cause: Python 3 not installed -> install from https://www.python.org/downloads/"
    Say "           (check 'Add to PATH' during install), then double-click again"
    Stop-With 1
  }
}

# ── 3. Print the final status (per-role Task State) ─────────────────────
Write-Host ""
Say "Current status (role / state):"
$rc = Invoke-Ctl "status"

# Count the live (Running/Ready) roles -- re-query for accuracy
$After = @(Get-ScheduledTask -TaskName "BOGO_*" -ErrorAction SilentlyContinue)
$Alive = @($After | Where-Object { $_.State -in @("Running", "Ready") }).Count
Write-Host ""
if ($After.Count -ge 1) {
  Ok "Always-on registered roles: $($After.Count) (running/ready $Alive; orchestrator/hr/dev/admin = 4 is healthy)."
  Say "View logs:  Get-Content app\logs\orchestrator.out.log -Tail 50 -Wait"
} else {
  Fail "No registered BOGO roles found. Check the log above for the cause."
  Stop-With 1
}

Stop-With 0
