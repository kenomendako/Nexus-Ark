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

REM Check if uv is installed
where uv >nul 2>nul
if %errorlevel% EQU 0 goto :FOUND_UV

echo [INFO] 'uv' tool not found. Installing...
echo.

REM Install uv via PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

REM Add install paths to PATH for this session
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%USERPROFILE%\AppData\Roaming\uv\bin;%PATH%"

REM Verify installation
where uv >nul 2>nul
if %errorlevel% NEQ 0 goto :UV_INSTALL_FAILED

:FOUND_UV
REM Check for app directory
if not exist "app" goto :MISSING_APP_DIR
cd app

echo [INFO] uv found. Syncing dependencies...
echo.
echo ============================================================
echo  NOTE: First-time startup may take several minutes
echo        while downloading dependencies.
echo        Please do not close this window.
echo ============================================================
echo.
REM CRITICAL FIX: --no-install-project prevents creating a .pth file with Japanese paths
uv sync --no-install-project
if %errorlevel% NEQ 0 goto :SYNC_FAILED

:START_APP
echo [INFO] Starting Application...
echo.

REM Invoke python directly. 
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" nexus_ark.py
) else (
    uv run nexus_ark.py
)

set EXIT_CODE=%errorlevel%
if %EXIT_CODE% EQU 123 (
    echo.
    echo [INFO] Update signal received.
    REM --- Apply staged update files ---
    if exist "..\update_staging" (
        echo [INFO] Applying update from staging area...
        robocopy "..\update_staging" "." /E /XD characters memories logs metadata backups .venv __pycache__ /XF config.json alarms.json redaction_rules.json .gemini_key_states.json *.log /NFL /NDL /NJH /NJS /R:1 /W:1
        REM robocopy returns 0-7 for success, 8+ for errors
        if %errorlevel% GEQ 8 (
            echo [WARNING] Some files could not be copied. Update may be incomplete.
        ) else (
            echo [INFO] Update files applied successfully.
        )
        rmdir /S /Q "..\update_staging" 2>nul
    )
    echo [INFO] Restarting application...
    echo.
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

:SYNC_FAILED
echo.
echo [ERROR] Failed to sync dependencies.
echo Please check your internet connection.
echo.
pause
exit /b 1

:APP_CRASHED
echo.
echo [ERROR] Application crashed!
echo.
pause
exit /b 1
