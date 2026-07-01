@echo off
rem ========================================================================
rem  BOGO Start (Windows) -- one Explorer double-click keeps 3 roles + the CEO admin bot always-on
rem ------------------------------------------------------------------------
rem  WHAT  Double-clicking this file:
rem    1) not registered -> bogo_ctl.ps1 setup   (venv+deps+config+Task Scheduler registration)
rem    2) registered     -> bogo_ctl.ps1 restart (redeploy latest code + restart 4 roles)
rem    3) then prints the current status (Task State)
rem  Windows equivalent of mac 'BOGO_start.command'. Always-on is handled by Task Scheduler.
rem  Path-safe: cd /d "%~dp0" pins its own location -> works under spaced/Unicode paths.
rem ========================================================================

rem -- Switch to the UTF-8 code page (prevents garbled output) --
chcp 65001 >nul

rem -- Pin the working directory to this .bat's own location: spaced/Unicode path safe --
cd /d "%~dp0"

set "LAUNCHER=%~dp0BOGO_start.launcher.ps1"

rem -- Verify the launcher body exists --
if not exist "%LAUNCHER%" (
  echo [ERROR] Launcher file not found: "%LAUNCHER%"
  echo         BOGO_start.bat and BOGO_start.launcher.ps1 must sit in the same folder.
  goto :hold
)

rem -- Prefer PowerShell 7+ (pwsh); fall back to the built-in Windows powershell --
where pwsh >nul 2>nul
if %ERRORLEVEL%==0 (
  set "PS=pwsh"
) else (
  set "PS=powershell"
)

echo.
echo [BOGO] Starting the Windows launcher ^(%PS%^)...
echo.

rem -- Invoke the launcher body (bypass ExecutionPolicy + quote to protect spaced/Unicode paths) --
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%LAUNCHER%"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo [ERROR] The launcher exited abnormally ^(code %RC%^). Check the log above.
)

:hold
echo.
echo --------------------------------------------------------------
echo Press any key to close this window.
pause >nul
exit /b %RC%
