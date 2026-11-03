@echo off
"%SystemRoot%\System32\chcp.com" 65001 >nul
setlocal
cd /d "%~dp0"
echo [Nexus Ark] Copy data into this NEW installation. Keep the original folder.
if not exist "%~dp0app\release\ImportUserData.ps1" (
    echo [Nexus Ark] Extract the complete ZIP to a new folder first.
    pause
    exit /b 1
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0app\release\ImportUserData.ps1"
set "IMPORT_EXIT=%ERRORLEVEL%"
pause
exit /b %IMPORT_EXIT%
