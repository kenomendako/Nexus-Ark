@echo off
chcp 65001 > nul
setlocal

echo.
echo   ╔═══════════════════════════════════════╗
echo   ║       Nexus Ark を起動中...          ║
echo   ╚═══════════════════════════════════════╝
echo.

:: Move to app directory
cd /d "%~dp0app"

:: Check if uv is available
where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] uv がインストールされていません。インストール中...
    powershell -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    if %errorlevel% neq 0 (
        echo [ERROR] uv のインストールに失敗しました。
        echo         手動でインストールしてください: https://docs.astral.sh/uv/
        pause
        exit /b 1
    )
    :: Refresh PATH
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

:: Sync dependencies
echo [INFO] 依存関係を確認中...
uv sync --quiet
if %errorlevel% neq 0 (
    echo [ERROR] 依存関係のインストールに失敗しました。
    pause
    exit /b 1
)

echo [OK] 準備完了！アプリケーションを起動します。
echo.
echo ---------------------------------------------------
echo  ブラウザで http://127.0.0.1:7860 を開いてください
echo ---------------------------------------------------
echo.

uv run nexus_ark.py

echo.
echo ---------------------------------------------------
echo  アプリケーションが終了しました。
echo ---------------------------------------------------
pause
