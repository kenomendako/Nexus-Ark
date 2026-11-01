@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo ---------------------------------------------------
echo  Nexus Ark Launching...
echo ---------------------------------------------------

REM Force Python to use UTF-8 mode (Safety net)
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM Repair the downloader before any dependency or Python download.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
if not exist "app\release\RepairUpdateCertificates.ps1" goto :UV_INSTALL_FAILED
powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\app\release\RepairUpdateCertificates.ps1"
if errorlevel 1 goto :UV_INSTALL_FAILED

:FOUND_UV
REM Check for app directory
if not exist "app" goto :MISSING_APP_DIR
if not exist "updater\current\update_host\supervisor.py" goto :MISSING_UPDATE_HOST

echo [INFO] Starting the protected launcher...
echo [INFO] First startup may take several minutes. Please keep this window open.
echo.
set "PYTHONPATH=%CD%\updater\current"
uv run --no-project --managed-python --python cpython@3.12.16 python -m update_host.supervisor --root "%CD%"
set EXIT_CODE=%errorlevel%

if %EXIT_CODE% EQU 123 (
    echo [INFO] Restart requested. Checking the protected update state...
    goto :FOUND_UV
)
if %EXIT_CODE% NEQ 0 goto :APP_CRASHED

echo.
echo ---------------------------------------------------
echo  Application Closed Normally
echo ---------------------------------------------------
pause
exit /b 0

:UV_INSTALL_FAILED
echo.
echo [ERROR] uv installation failed or could not be found in PATH.
echo Please install 'uv' manually from https://github.com/astral-sh/uv
echo.
pause
exit /b 1

:MISSING_APP_DIR
echo.
echo [ERROR] 'app' directory not found!
echo Please ensure you have extracted all files correctly.
echo.
pause
exit /b 1

:MISSING_UPDATE_HOST
echo.
echo [ERROR] The protected update launcher is missing.
echo Please extract the complete Nexus Ark ZIP again.
echo.
pause
exit /b 1

:APP_CRASHED
echo.
echo [ERROR] Application crashed!
echo.
pause
exit /b 1
