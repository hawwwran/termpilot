@echo off
rem TermPilot Windows installer (dev mode: points the `tp` / `termpilot`
rem shims at THIS checkout). Run install-latest-version.bat instead if
rem you want a fresh download from GitHub.

setlocal
set "SCRIPT_DIR=%~dp0"
rem Strip trailing backslash for cleaner display.
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo TermPilot Windows installer
echo   source: %SCRIPT_DIR%
echo.

rem Delegate to PowerShell for the real work (path manipulation,
rem PATH editing, pip install). -ExecutionPolicy Bypass keeps this
rem working on locked-down policies.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\install.ps1" -Source "%SCRIPT_DIR%"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo install.ps1 exited with code %RC%.
    exit /b %RC%
)

echo.
echo Installation complete. Open a NEW cmd or PowerShell window so the
echo updated PATH takes effect, then:
echo   termpilot --set-relay-url https://your.host/path
echo   termpilot --generate-token
echo   termpilot
endlocal
