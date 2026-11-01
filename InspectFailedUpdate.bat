@echo off
"%SystemRoot%\System32\chcp.com" 65001 >nul
setlocal
cd /d "%~dp0"
echo [Nexus Ark] 更新停止の記録を読み取ります。本体・lock・世代は変更しません。
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0app\release\RecoverFailedUpdate.ps1" -InspectOnly
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
