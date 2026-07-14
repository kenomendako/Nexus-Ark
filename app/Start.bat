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
uv sync --no-install-project --inexact
if %errorlevel% NEQ 0 goto :SYNC_FAILED

:START_APP
echo [INFO] Starting Application...
echo.

call :CONFIGURE_TAILSCALE_SERVE
call :CONFIGURE_TAILSCALE_ATELIER_SERVE

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

:CONFIGURE_TAILSCALE_ATELIER_SERVE
if not "%NEXUS_ARK_START_ATELIER_TAILSCALE_SERVE%"=="1" exit /b 0
where tailscale >nul 2>nul
if %errorlevel% NEQ 0 (
    echo [WARN] tailscale command not found. Skipping Atelier HTTPS serve.
    exit /b 0
)
if "%NEXUS_ARK_ATELIER_SERVE_PORT%"=="" (
    for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p=8765; if (Test-Path 'config.json') { $j=Get-Content 'config.json' -Raw | ConvertFrom-Json; if ($null -ne $j.atelier_serve_settings -and $null -ne $j.atelier_serve_settings.port) { $p=$j.atelier_serve_settings.port } }; [string]$p } catch { '8765' }"`) do set "NEXUS_ARK_ATELIER_SERVE_PORT=%%P"
)
if "%NEXUS_ARK_ATELIER_SERVE_HTTPS_PORT%"=="" (
    for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p=8443; if (Test-Path 'config.json') { $j=Get-Content 'config.json' -Raw | ConvertFrom-Json; if ($null -ne $j.atelier_serve_settings -and $null -ne $j.atelier_serve_settings.tailscale_https_port) { $p=$j.atelier_serve_settings.tailscale_https_port } }; [string]$p } catch { '8443' }"`) do set "NEXUS_ARK_ATELIER_SERVE_HTTPS_PORT=%%P"
)
if "%NEXUS_ARK_ATELIER_SERVE_ENABLED%"=="" set "NEXUS_ARK_ATELIER_SERVE_ENABLED=1"
set "NEXUS_ARK_ATELIER_TARGET=http://127.0.0.1:%NEXUS_ARK_ATELIER_SERVE_PORT%"
set "NEXUS_ARK_ATELIER_TS_STATUS=%TEMP%\nexus_ark_tailscale_atelier_serve_status.txt"
set "NEXUS_ARK_ATELIER_TS_LOG=%TEMP%\nexus_ark_tailscale_atelier_serve.txt"
tailscale serve status > "%NEXUS_ARK_ATELIER_TS_STATUS%" 2>&1
findstr /C:"%NEXUS_ARK_ATELIER_TARGET%" "%NEXUS_ARK_ATELIER_TS_STATUS%" >nul 2>nul
if %errorlevel% EQU 0 (
    echo [OK] Tailscale HTTPS serve for Atelier is already configured.
    call :PRINT_TAILSCALE_ATELIER_URL
    exit /b 0
)
echo [INFO] Configuring Tailscale HTTPS for Atelier apps...
tailscale serve --bg --https=%NEXUS_ARK_ATELIER_SERVE_HTTPS_PORT% "%NEXUS_ARK_ATELIER_TARGET%" > "%NEXUS_ARK_ATELIER_TS_LOG%" 2>&1
if %errorlevel% EQU 0 (
    echo [OK] Tailscale HTTPS serve for Atelier configured.
    call :PRINT_TAILSCALE_ATELIER_URL
    exit /b 0
)
echo [WARN] Atelier Tailscale HTTPS serve setup did not complete.
echo        Check: tailscale serve status
type "%NEXUS_ARK_ATELIER_TS_LOG%" 2>nul
exit /b 0

:PRINT_TAILSCALE_ATELIER_URL
set "NEXUS_ARK_TS_DNS="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $j = tailscale status --json | ConvertFrom-Json; ($j.Self.DNSName -replace '\\.$','') } catch { '' }"`) do set "NEXUS_ARK_TS_DNS=%%D"
if not "%NEXUS_ARK_TS_DNS%"=="" (
    echo Atelier HTTPS: https://%NEXUS_ARK_TS_DNS%:%NEXUS_ARK_ATELIER_SERVE_HTTPS_PORT%/atelier/^<room^>/^<app^>/
) else (
    echo Atelier HTTPS: https://^<your-device^>.^<tailnet^>.ts.net:%NEXUS_ARK_ATELIER_SERVE_HTTPS_PORT%/atelier/^<room^>/^<app^>/
)
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

:CONFIGURE_TAILSCALE_SERVE
if not "%NEXUS_ARK_START_TAILSCALE_SERVE%"=="1" exit /b 0
where tailscale >nul 2>nul
if %errorlevel% NEQ 0 (
    echo [WARN] tailscale command not found. Skipping Lite HTTPS serve.
    exit /b 0
)
if "%NEXUS_ARK_API_PORT%"=="" set "NEXUS_ARK_API_PORT=8000"
if "%NEXUS_ARK_API_ENABLED%"=="" set "NEXUS_ARK_API_ENABLED=1"
set "NEXUS_ARK_LITE_TARGET=http://127.0.0.1:%NEXUS_ARK_API_PORT%"
set "NEXUS_ARK_TS_STATUS=%TEMP%\nexus_ark_tailscale_serve_status.txt"
set "NEXUS_ARK_TS_LOG=%TEMP%\nexus_ark_tailscale_serve.txt"
tailscale serve status > "%NEXUS_ARK_TS_STATUS%" 2>&1
findstr /C:"%NEXUS_ARK_LITE_TARGET%" "%NEXUS_ARK_TS_STATUS%" >nul 2>nul
if %errorlevel% EQU 0 (
    echo [OK] Tailscale HTTPS serve is already configured.
    call :PRINT_TAILSCALE_LITE_URL
    exit /b 0
)
echo [INFO] Configuring Tailscale HTTPS for Nexus Ark Lite...
tailscale serve --bg --https=443 "%NEXUS_ARK_LITE_TARGET%" > "%NEXUS_ARK_TS_LOG%" 2>&1
if %errorlevel% EQU 0 (
    echo [OK] Tailscale HTTPS serve configured.
    call :PRINT_TAILSCALE_LITE_URL
    exit /b 0
)
echo [WARN] Tailscale HTTPS serve setup did not complete.
echo        Check: tailscale serve status
type "%NEXUS_ARK_TS_LOG%" 2>nul
exit /b 0

:PRINT_TAILSCALE_LITE_URL
set "NEXUS_ARK_TS_DNS="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $j = tailscale status --json | ConvertFrom-Json; ($j.Self.DNSName -replace '\\.$','') } catch { '' }"`) do set "NEXUS_ARK_TS_DNS=%%D"
if not "%NEXUS_ARK_TS_DNS%"=="" (
    echo Lite HTTPS: https://%NEXUS_ARK_TS_DNS%/lite
) else (
    echo Lite HTTPS: https://^<your-device^>.^<tailnet^>.ts.net/lite
)
exit /b 0
