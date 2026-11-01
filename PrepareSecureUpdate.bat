@echo off
"%SystemRoot%\System32\chcp.com" 65001 >nul
setlocal
cd /d "%~dp0"
echo [Nexus Ark] Starting update preparation.
echo [Nexus Ark] Select your existing Nexus Ark folder in the next window.
if not exist "%~dp0app\release\PrepareExistingUpdate.ps1" (
    echo [Nexus Ark] Preparation tool not found. Extract the complete ZIP to a new folder.
    pause
    exit /b 1
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0app\release\PrepareExistingUpdate.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [Nexus Ark] Preparation did not complete. Check the message shown above.
    pause
)
exit /b %EXIT_CODE%
