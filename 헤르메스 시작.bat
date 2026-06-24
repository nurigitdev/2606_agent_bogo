@echo off
rem ========================================================================
rem  헤르메스 시작 (Windows) — 탐색기 더블클릭 1회로 3개 역할 + CEO 관리봇 상시 가동
rem ------------------------------------------------------------------------
rem  WHAT  이 파일을 더블클릭하면:
rem    1) 미등록  -> hermes_ctl.ps1 setup   (venv+의존성+config+Task Scheduler 등록)
rem    2) 등록됨  -> hermes_ctl.ps1 restart (최신 코드 재배포 + 4역할 재시작)
rem    3) 끝나면 현재 상태(Task State) 표시
rem  mac '헤르메스 시작.command' 의 Windows 등가물. 상시 가동은 Task Scheduler 담당.
rem  Korean path safe: cd /d "%~dp0" 로 자기 위치 고정 → 한글/공백 경로에서 동작.
rem ========================================================================

rem -- UTF-8 코드페이지로 전환 (한글 깨짐 방지) --
chcp 65001 >nul

rem -- 자기(.bat) 위치를 작업 디렉터리로 고정: 한글/공백 경로 안전 --
cd /d "%~dp0"

set "LAUNCHER=%~dp0헤르메스 시작.launcher.ps1"

rem -- 런처 본체 존재 확인 --
if not exist "%LAUNCHER%" (
  echo [오류] 런처 파일을 찾지 못했습니다: "%LAUNCHER%"
  echo        헤르메스 시작.bat 와 헤르메스 시작.launcher.ps1 은 같은 폴더에 함께 있어야 합니다.
  goto :hold
)

rem -- PowerShell 7+(pwsh) 우선, 없으면 Windows 기본 powershell 사용 --
where pwsh >nul 2>nul
if %ERRORLEVEL%==0 (
  set "PS=pwsh"
) else (
  set "PS=powershell"
)

echo.
echo [헤르메스] Windows 런처를 시작합니다 ^(%PS%^)...
echo.

rem -- 런처 본체 호출 (ExecutionPolicy 우회 + 따옴표로 한글/공백 경로 보호) --
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%LAUNCHER%"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo [오류] 런처가 비정상 종료했습니다 ^(코드 %RC%^). 위 로그를 확인하세요.
)

:hold
echo.
echo --------------------------------------------------------------
echo 이 창은 아무 키나 누르면 닫힙니다.
pause >nul
exit /b %RC%
