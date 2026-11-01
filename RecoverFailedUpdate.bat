@echo off
"%SystemRoot%\System32\chcp.com" 65001 >nul
setlocal
cd /d "%~dp0"
echo [Nexus Ark] 旧版へ復旧した更新を確認し、安全に再試行します。
echo [Nexus Ark] Nexus Arkを通常終了し、バックアップを保持してください。
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0app\release\RecoverFailedUpdate.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
