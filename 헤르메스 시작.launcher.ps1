# ════════════════════════════════════════════════════════════════════════
#  헤르메스 시작 (Windows 런처 본체) — .bat 이 호출하는 감지·분기 로직
# ════════════════════════════════════════════════════════════════════════
#  WHAT  더블클릭 → 이 스크립트가:
#    1) 미등록 → hermes_ctl.ps1 setup (venv+의존성+config+Task Scheduler 등록+기동)
#    2) 이미 상시 가동 중 → hermes_ctl.ps1 restart (최신 코드 재배포 + 4역할 재시작)
#    3) 끝나면 현재 상태(Task State)를 표시
#  설계 근거: mac '헤르메스 시작.command' 와 1:1 등가. 상시 가동은 Task Scheduler 가
#    담당하므로, 매번 pwsh ./hermes_ctl.ps1 {setup|restart|status} 를 타이핑하던
#    토일을 1클릭으로 제거. 이미 등록돼 있으면 중복 등록 없이 재배포+재시작만(중복 방지).
#  Korean path safe: $PSScriptRoot 로 자기 위치를 동적 해석 → 한글/공백 경로 안전.
# ════════════════════════════════════════════════════════════════════════
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ── 0. 자기 위치 기준으로 reporting/ 디렉터리 고정 (한글·공백 경로 안전) ──
$SelfDir = $PSScriptRoot
$Repo    = Join-Path $SelfDir "reporting"
$Ctl     = Join-Path $Repo "hermes_ctl.ps1"

function Say($m)  { Write-Host "[헤르메스] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[성공] $m"     -ForegroundColor Green }
function Fail($m) { Write-Host "[오류] $m"     -ForegroundColor Red }

# 종료 코드를 .bat 으로 전달 (.bat 이 이 값을 보고 pause 여부 결정)
function Stop-With($code) { exit [int]$code }

Write-Host ""
Say "헤르메스 상시 가동 런처 시작 (Windows)"
Say "위치: $Repo"
Write-Host ""

# ── 1. 사전 점검: 컨트롤러 + PowerShell 호스트 확인 ─────────────────────
if (-not (Test-Path $Ctl)) {
  Fail "hermes_ctl.ps1 을 찾지 못했습니다: $Ctl"
  Fail "이 런처 파일은 'reporting' 폴더가 있는 프로젝트 루트에 두어야 합니다."
  Stop-With 1
}

# pwsh(7+) 우선, 없으면 Windows 기본 powershell 5.1 사용
$PwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue)?.Source
if (-not $PwshExe) { $PwshExe = (Get-Command powershell -ErrorAction SilentlyContinue)?.Source }
if (-not $PwshExe) {
  Fail "PowerShell 실행기를 찾지 못했습니다 (pwsh/powershell 둘 다 없음)."
  Stop-With 1
}

# hermes_ctl.ps1 한 명령을 자식 PowerShell 로 호출 (ExecutionPolicy 우회 + 인코딩 보존)
function Invoke-Ctl([string]$SubCmd) {
  & $PwshExe -NoProfile -ExecutionPolicy Bypass -File $Ctl $SubCmd
  return $LASTEXITCODE
}

# ── 2. 현재 가동 상태 판별 (이미 등록돼 있는가?) ────────────────────────
#   Task Scheduler 에 Hermes_* 작업이 하나라도 있으면 = 이미 설치/등록됨.
#   (mac launchctl list | grep com.hermes 의 Windows 등가)
$HermesTasks = @(Get-ScheduledTask -TaskName "Hermes_*" -ErrorAction SilentlyContinue)
$RegisteredCount = $HermesTasks.Count

if ($RegisteredCount -ge 1) {
  # ── 2-a. 이미 등록됨 → 중복 등록 금지, 재배포+재시작만 ──────────────
  Say "이미 상시 가동 등록됨 (등록된 역할 $RegisteredCount개)."
  Say "최신 코드를 재배포하고 4개 역할을 재시작합니다..."
  Write-Host ""
  $rc = Invoke-Ctl "restart"
  if ($rc -eq 0) {
    Write-Host ""
    Ok "재배포 + 재시작 완료."
  } else {
    Write-Host ""
    Fail "재시작 중 문제가 발생했습니다. 위 로그를 확인하세요."
    Say "수동 진단:  cd `"$Repo`"; .\hermes_ctl.ps1 status"
    Stop-With 1
  }
} else {
  # ── 2-b. 미등록 → 최초 설치(부트스트랩 + Task Scheduler 등록 + 기동) ──
  Say "아직 상시 가동이 등록되지 않았습니다. 최초 설치를 진행합니다."
  Say "(venv 생성 + 의존성 설치 + config 준비 + Task Scheduler 등록 — 수 분 걸릴 수 있음)"
  Write-Host ""
  $rc = Invoke-Ctl "setup"
  if ($rc -eq 0) {
    Write-Host ""
    Ok "설치 + 상시 가동 등록 완료."
    Say "처음이라면 reporting\.env 와 *_config.json, channels.json 에 실제 토큰/키/채널ID 입력 후"
    Say "이 파일을 한 번 더 더블클릭하면 새 설정으로 재시작됩니다."
  } else {
    Write-Host ""
    Fail "설치 중 문제가 발생했습니다. 위 로그를 확인하세요."
    Say "흔한 원인: Python 3.12 미설치 → https://www.python.org/downloads/release/python-3120/ 에서"
    Say "           설치(설치 시 'Add to PATH' 체크) 후 다시 더블클릭"
    Stop-With 1
  }
}

# ── 3. 최종 상태 표시 (역할별 Task State) ───────────────────────────────
Write-Host ""
Say "현재 상태 (역할 / 상태):"
$rc = Invoke-Ctl "status"

# 살아있는(Running/Ready) 역할 수 카운트 — 재조회로 정확도 보장
$After = @(Get-ScheduledTask -TaskName "Hermes_*" -ErrorAction SilentlyContinue)
$Alive = @($After | Where-Object { $_.State -in @("Running", "Ready") }).Count
Write-Host ""
if ($After.Count -ge 1) {
  Ok "상시 가동 등록된 역할: $($After.Count)개 (가동/대기 $Alive개; orchestrator/hr/dev/admin 4개가 정상)."
  Say "로그 보기:  Get-Content reporting\logs\orchestrator.out.log -Tail 50 -Wait"
} else {
  Fail "등록된 헤르메스 역할이 없습니다. 위 로그에서 원인을 확인하세요."
  Stop-With 1
}

Stop-With 0
