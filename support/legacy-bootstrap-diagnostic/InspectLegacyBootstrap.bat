@echo off
setlocal
chcp 65001 >nul
echo [Nexus Ark] Read-only inspection v2. Select the existing folder, then the new package folder.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0InspectLegacyBootstrap.ps1"
pause
