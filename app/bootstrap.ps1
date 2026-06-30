# BOGO cross-platform bootstrap (Windows / PowerShell).
#
# From a fresh git clone or copy: locates Python >= 3.12 (newest preferred), (re)creates a PORTABLE .venv,
# installs requirements.txt, copies *.example -> real config only when missing.
# Idempotent + Hangul-path safe (paths resolved from script location, UTF-8).
#
# Usage:  pwsh ./bootstrap.ps1     (or)   powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Say($m) { Write-Host "[bootstrap] $m" -ForegroundColor Cyan }
function Die($m) { Write-Host "[bootstrap:오류] $m" -ForegroundColor Red; exit 1 }

# ── 1. Python >=3.12 탐지 ────────────────────────────────────────────
# 3.12 이상이면 채택하되 후보 중 가장 최신 버전을 우선 선택한다(3.12 미만만 거부).
function Test-PyOk($exe, $verArgs) {
  # returns "<major>.<minor>" if >= 3.12 else $null
  $v = (& $exe @verArgs -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null)
  if (-not $v) { return $null }
  $parts = $v.Split('.')
  if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12)) { return $v }
  return $null
}

function Find-Py {
  $best = $null      # array form of the chosen invocation (e.g. @("py","-3.13"))
  $bestVer = $null   # "<major>.<minor>" string of the chosen interpreter

  # 1) py launcher: newest-first explicit versions
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($pv in @("-3.14", "-3.13", "-3.12")) {
      $ver = Test-PyOk "py" @($pv)
      if ($ver -and (-not $bestVer -or ([version]$ver -gt [version]$bestVer))) {
        $best = @("py", $pv); $bestVer = $ver
      }
    }
  }
  # 2) python on PATH: newest-first explicit names then generic
  foreach ($c in @("python3.14", "python3.13", "python3.12", "python3", "python")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
      $ver = Test-PyOk $c @()
      if ($ver -and (-not $bestVer -or ([version]$ver -gt [version]$bestVer))) {
        $best = @($c); $bestVer = $ver
      }
    }
  }
  return $best
}

$Py = Find-Py
if (-not $Py) {
  Die "Python 3.12 이상을 찾지 못했습니다. https://www.python.org/downloads/ 에서 최신 버전 설치(설치 시 'Add to PATH' 체크) 후 다시 실행하세요."
}
$PyVerShown = (& $Py[0] @($Py[1..($Py.Length-1)]) -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null)
Say "Python $PyVerShown 사용: $($Py -join ' ')"

# ── 2. 휴대용 venv (재)생성 ──────────────────────────────────────────
$Venv = Join-Path $Here ".venv"
if (Test-Path $Venv) { Say "기존 .venv 제거 후 재생성"; Remove-Item -Recurse -Force $Venv }
Say ".venv 생성 중..."
& $Py[0] @($Py[1..($Py.Length-1)]) -m venv --copies $Venv

$VenvPy = Join-Path $Venv "Scripts\python.exe"

# ── 3. 의존성 설치 ───────────────────────────────────────────────────
Say "pip 업그레이드 + requirements 설치 중..."
& $VenvPy -m pip install --upgrade pip | Out-Null
& $VenvPy -m pip install -r (Join-Path $Here "requirements.txt")

# ── 4. config / .env 복사 (없을 때만) ───────────────────────────────
function Copy-IfMissing($example, $real) {
  $rp = Join-Path $Here $real
  $ep = Join-Path $Here $example
  if (Test-Path $rp)      { Say "보존: $real (이미 존재)" }
  elseif (Test-Path $ep)  { Copy-Item $ep $rp; Say "생성: $real (← $example, 실제 값 입력)" }
}
if (-not (Test-Path (Join-Path $Here ".env"))) {
  $ee = Join-Path $Here ".env.example"
  if (Test-Path $ee) { Copy-Item $ee (Join-Path $Here ".env"); Say "생성: .env (← .env.example)" }
}
Copy-IfMissing "config\llm_config.json.example"   "llm_config.json"
Copy-IfMissing "config\nk_config.json.example"    "nk_config.json"
Copy-IfMissing "config\genz_config.json.example"  "genz_config.json"
Copy-IfMissing "config\gyaru_config.json.example" "gyaru_config.json"
Copy-IfMissing "config\channels.json.example"     "channels.json"

Say "부트스트랩 완료."
Say "다음 단계:"
Say "  1) .env / *_config.json / channels.json 에 실제 값 입력"
Say "  2) 상시 가동 등록:  .\bogo_ctl.ps1 install"
Say "  3) 또는 수동 실행:  .venv\Scripts\python.exe bogo_runtime.py orchestrator"
