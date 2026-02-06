@echo off
echo Starting Nexus Ark...
echo Ensuring dependencies are up to date...
uv sync
if %errorlevel% neq 0 (
    echo [ERROR] Dependency installation failed. Please check if uv is installed.
    pause
    exit /b %errorlevel%
)

echo Launching Application...
uv run nexus_ark.py
pause
