@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
title Nexus Ark

echo Starting Nexus Ark...
echo.
echo Please keep this window open while the application is running.
echo To close the application, please close this window.
echo.

REM --- Pythonの実行ファイルを探す ---
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found.
    echo Please install Python 3.10 or later and make sure it is in your PATH.
    pause
    exit /b
)

REM --- メインのPythonスクリプトを実行 ---
python nexus_ark.py

echo.
echo Application has been closed.
pause