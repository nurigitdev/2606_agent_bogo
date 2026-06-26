# BOGO cross-platform bootstrap (Windows / PowerShell).
#
# From a fresh git clone or copy: locates Python 3.12, (re)creates a PORTABLE .venv,
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

# ── 1. Python 3.12 탐지 ──────────────────────────────────────────────
function Find-Py312 {
  # 1) py launcher
  if (Get-Command py -ErrorAction SilentlyContinue) {
    $v = (& py -3.12 -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null)
    if ($v -eq "3.12") { return @("py", "-3.12") }
  }
  # 2) python on PATH
  foreach ($c in @("python3.12", "python", "python3")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
      $v = (& $c -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null)
      if ($v -eq "3.12") { return @($c) }
    }
  }
  return $null
}

$Py = Find-Py312
if (-not $Py) {
  Die "Python 3.12 를 찾지 못했습니다. https://www.python.org/downloads/release/python-3120/ 에서 설치(설치 시 'Add to PATH' 체크) 후 다시 실행하세요."
}
Say "Python 3.12 사용: $($Py -join ' ')"

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
Copy-IfMissing "llm_config.json.example"   "llm_config.json"
Copy-IfMissing "nk_config.json.example"    "nk_config.json"
Copy-IfMissing "genz_config.json.example"  "genz_config.json"
Copy-IfMissing "gyaru_config.json.example" "gyaru_config.json"
Copy-IfMissing "channels.json.example"     "channels.json"

Say "부트스트랩 완료."
Say "다음 단계:"
Say "  1) .env / *_config.json / channels.json 에 실제 값 입력"
Say "  2) 상시 가동 등록:  .\bogo_ctl.ps1 install"
Say "  3) 또는 수동 실행:  .venv\Scripts\python.exe bogo_runtime.py orchestrator"
