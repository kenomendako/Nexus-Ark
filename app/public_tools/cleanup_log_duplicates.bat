@echo off
setlocal
title Nexus Ark Log Cleanup Tool

:: 実行フォルダに移動
cd /d "%~dp0"

:: 引数（ドラッグ&ドロップされたファイル）を取得
set "TARGET_FILE=%~1"

:: 引数がない場合は対話モードへ
if "%TARGET_FILE%" == "" goto InputMode
goto StartProcess

:InputMode
echo -------------------------------------------------------
echo  Nexus Ark ログ重複修復ツール (Windows用)
echo -------------------------------------------------------
echo  使い方: 
echo  このウィンドウの中に、修復したいログファイルを
echo  マウスで直接「ドラッグ＆ドロップ」してください。
echo.
echo  ファイルパスが表示されたら、Enterキーを押すと開始します。
echo -------------------------------------------------------
set /p "TARGET_FILE=修復するファイルをここにドロップしてEnter: "

:StartProcess
:: クオートを除去して正規化
set "TARGET_FILE=%TARGET_FILE:"=%"

if "%TARGET_FILE%" == "" goto NoFileError

echo.
echo [処理開始]
echo 対象: "%TARGET_FILE%"
echo.

:: Python実行環境の検索 (カッコを使わないGOTO形式)
set PY_CMD=

where python >nul 2>nul
if not errorlevel 1 (
    set PY_CMD=python
    goto RunScript
)

where python3 >nul 2>nul
if not errorlevel 1 (
    set PY_CMD=python3
    goto RunScript
)

where py >nul 2>nul
if not errorlevel 1 (
    set PY_CMD=py
    goto RunScript
)

:: Pythonが見つからない場合
echo [エラー] Pythonが見つかりませんでした。
echo.
echo Python 3.12以上 をインストールし、
echo インストール画面の「Add Python to PATH」にチェックを入れてください。
echo.
pause
exit /b 1

:RunScript
echo 実行エンジン: %PY_CMD%
echo.

:: Pythonスクリプトの実行
"%PY_CMD%" "%~dp0cleanup_log_duplicates.py" "%TARGET_FILE%"

if errorlevel 1 (
    echo.
    echo [エラー] 修復処理中に問題が発生しました。
    echo 詳細を確認するため、この画面は閉じません。
    echo.
    pause
) else (
    echo.
    echo [完了] ログの重複修復が終了しました。
)

goto End

:NoFileError
echo.
echo [エラー] ファイルが指定されませんでした。
echo 処理を中断します。
goto End

:End
echo.
echo ウィンドウを閉じるには何かキーを押してください。
pause >nul
exit /b 0
