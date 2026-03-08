@echo off
setlocal
:: スクリプトがあるフォルダに移動
cd /d %~dp0

if "%~1"=="" (
    echo -------------------------------------------------------
    echo  Nexus Ark ログ重複修復ツール (Windows用)
    echo -------------------------------------------------------
    echo  使い方: 
    echo  修復したいログファイル（2026-03.txt など）を、
    echo  この .bat ファイルの上にドラッグ＆ドロップしてください。
    echo -------------------------------------------------------
    pause
    exit /b
)

echo [Log Cleanup] 処理を開始します: %1
:: 自身と同じディレクトリにある Python スクリプトを実行
python "%~dp0cleanup_log_duplicates.py" "%~1"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [エラー] スクリプトの実行に失敗しました。
    echo Pythonがインストールされているか、パスが通っているか確認してください。
) else (
    echo.
    echo [完了] 処理が終了しました。
)
pause
