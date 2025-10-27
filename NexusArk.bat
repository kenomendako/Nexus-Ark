@echo off
setlocal

rem --- Nexus Ark Launcher (Windows optimized) ---
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
title Nexus Ark

echo Starting Nexus Ark...
echo If the browser does not open automatically, please open it and navigate to: http://127.0.0.1:7860
echo Please keep this window open while the application is running.

rem --- Navigate to the script directory ---
cd /d "%~dp0"
set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

rem --- Locate a Python interpreter (prefer py launcher) ---
set "PYTHON_CMD="
py -3 --version > nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
    py --version > nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py"
)
if not defined PYTHON_CMD (
    python --version > nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo.
    echo [ERROR] Python 3 was not found on this system.
    echo Please install Python from https://www.python.org/downloads/windows/ and try again.
    pause
    exit /b 1
)

rem --- Create virtual environment if needed ---
if not exist "%VENV_PYTHON%" (
    echo Creating virtual environment...
    %PYTHON_CMD% -m venv "%VENV_DIR%"
)
if not exist "%VENV_PYTHON%" (
    echo.
    echo [ERROR] Failed to create the virtual environment at "%VENV_DIR%".
    pause
    exit /b 1
)

rem --- Ensure pip and dependencies are up to date ---
echo Updating pip...
"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
    echo [WARNING] Failed to upgrade pip automatically. Continuing...
)

if exist requirements.txt (
    echo Installing required modules...
    "%VENV_PYTHON%" -m pip install --upgrade -r requirements.txt
) else (
    echo [WARNING] requirements.txt was not found. Skipping dependency installation.
)

rem --- Launch the application ---
echo Launching Nexus Ark...
"%VENV_PYTHON%" nexus_ark.py

echo.
echo The application has been closed. You can now close this window.
endlocal
pause
