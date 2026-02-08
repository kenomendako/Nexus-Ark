@echo off
setlocal
cd /d "%~dp0"

echo ---------------------------------------------------
echo  Nexus Ark - Local LLM Setup
echo ---------------------------------------------------
echo This script will install additional components required to run
echo Local LLMs (GGUF format) on your system.
echo.

REM Check if uv is installed
where uv >nul 2>nul
if %errorlevel% NEQ 0 (
    echo [INFO] 'uv' tool not found. Trying to install via PowerShell...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%USERPROFILE%\AppData\Roaming\uv\bin;%PATH%"
)

REM Verify uv again
where uv >nul 2>nul
if %errorlevel% NEQ 0 (
    echo [ERROR] uv installation failed. Please install 'uv' manually.
    pause
    exit /b 1
)

REM Change directory to 'app' if exists (for running the python script)
if exist "app" (
    cd app
) else (
    REM If we are already in the app/tools folder or similar? No, this launcher is at root.
    REM The python script is in tools/setup_local_llm.py relative to app root.
    echo [ERROR] 'app' folder not found. Please run this script from the Nexus Ark root folder.
    pause
    exit /b 1
)

echo [INFO] Starting setup tool...
echo.
uv run python tools/setup_local_llm.py

echo.
echo ---------------------------------------------------
echo  Setup Finished
echo ---------------------------------------------------
pause
