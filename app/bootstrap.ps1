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
function Die($m) { Write-Host "[bootstrap:오류] $m" -ForegroundColor Red; exit 1 }

# ── 1. Python 인터프리터 탐지 ────────────────────────────────────────
# 버전을 파싱·비교·강제하지 않는다. py / python / python3 중 먼저 발견되는 것을 채택한다.
# 모두 없을 때만 설치 안내 후 종료한다.
function Find-Py {
  # py launcher first (Windows standard), then python / python3 on PATH.
  if (Get-Command py -ErrorAction SilentlyContinue)      { return @("py") }
  if (Get-Command python -ErrorAction SilentlyContinue)  { return @("python") }
  if (Get-Command python3 -ErrorAction SilentlyContinue) { return @("python3") }
  return $null
}

$Py = Find-Py
if (-not $Py) {
  Die "Python 인터프리터를 찾지 못했습니다(python/py 모두 없음). https://www.python.org/downloads/ 에서 설치(설치 시 'Add to PATH' 체크) 후 다시 실행하세요."
}
Say "Python 사용: $($Py -join ' ')"

# ── 2. 휴대용 venv (재)생성 ──────────────────────────────────────────
$Venv = Join-Path $Here ".venv"
if (Test-Path $Venv) { Say "기존 .venv 제거 후 재생성"; Remove-Item -Recurse -Force $Venv }
Say ".venv 생성 중..."
& $Py[0] -m venv --copies $Venv

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
