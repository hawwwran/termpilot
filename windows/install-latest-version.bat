@echo off
rem TermPilot Windows end-user installer.
rem
rem Downloads the latest GitHub release zip, extracts the `windows/`
rem subdirectory to %LOCALAPPDATA%\Programs\termpilot\windows\, then
rem runs the bundled install.bat from that location.
rem
rem For development (point at THIS checkout), use install.bat directly.

setlocal
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\install-latest-version.ps1"
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
