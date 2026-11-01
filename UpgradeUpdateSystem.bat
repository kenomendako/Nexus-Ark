@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo ---------------------------------------------------
echo  Nexus Ark 更新ランチャー移行
echo ---------------------------------------------------
echo.
echo 完全 ZIP の中にあるこのファイルを実行してください。
echo 旧 Nexus Ark フォルダーを選択すると、安全確認後に一度だけ移行します。
echo.

REM -STA: FolderBrowserDialogを安定して表示するためのWindows PowerShell指定
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0UpgradeUpdateSystem.ps1" %*
set EXIT_CODE=%ERRORLEVEL%

if not "%EXIT_CODE%"=="0" (
    echo.
    echo 移行は完了していません。表示された案内を確認してください。
    pause
)
exit /b %EXIT_CODE%

