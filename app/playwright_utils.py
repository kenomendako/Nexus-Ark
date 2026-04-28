# playwright_utils.py
import subprocess
import sys
import logging
import traceback

logger = logging.getLogger("playwright_utils")

def ensure_playwright_browsers(browser_type: str = "chromium") -> bool:
    """
    Playwrightのブラウザバイナリがインストールされているか確認し、
    不足していれば自動的にインストールを試行する。
    
    Args:
        browser_type (str): インストールするブラウザの種類 ("chromium", "firefox", "webkit", "all")
        
    Returns:
        bool: インストールに成功（または既に存在）した場合は True、失敗した場合は False
    """
    try:
        # まずはインストールを試行（Playwright側で既存チェックが行われるため、
        # 明示的な存在チェックより install コマンドを呼ぶ方が確実）
        logger.info(f"Playwrightのブラウザ ({browser_type}) を確認/インストール中...")
        
        # Windows環境等でパスが通っていない可能性を考慮し、sys.executable を使用
        cmd = [sys.executable, "-m", "playwright", "install", browser_type]
        
        # 実行
        process = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        
        logger.info(f"Playwrightブラウザのセットアップが完了しました: {browser_type}")
        return True
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Playwrightブラウザのインストールに失敗しました (code {e.returncode}):")
        logger.error(f"STDOUT: {e.stdout}")
        logger.error(f"STDERR: {e.stderr}")
        return False
    except Exception as e:
        logger.error(f"Playwrightユーティリティで予期せぬエラーが発生しました: {e}")
        traceback.print_exc()
        return False

def is_executable_missing_error(exception: Exception) -> bool:
    """
    発生した例外が「ブラウザ実行ファイルが見つからない」という Playwright 固有のものか判定する。
    """
    err_msg = str(exception)
    # Playwrightの標準的なエラーメッセージパターン
    return "Executable doesn't exist" in err_msg or "playwright install" in err_msg.lower()
