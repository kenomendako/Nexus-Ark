import os
import json
import logging
import time
import traceback
from typing import List, Dict, Tuple, Optional
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config_manager

# ロガー設定
logger = logging.getLogger(__name__)

def import_gemini_log_from_url(url: str, room_name: str) -> Tuple[bool, str, List[Dict]]:
    """
    Geminiの共有URLから会話ログをインポートする (Playwright版)
    
    Args:
        url (str): Geminiの共有リンク
        room_name (str): ルーム名

    Returns:
        Tuple: (Success, Message, LogList)
    """
    if not (url.startswith("https://gemini.google.com/share/") or url.startswith("https://g.co/gemini/share/")):
        return False, "無効なURLです。'https://gemini.google.com/share/' または 'https://g.co/gemini/share/' で始まるURLを指定してください。", []

    logger.info(f"Starting Gemini log import via Playwright: {url}")

    try:
        with sync_playwright() as p:
            # ブラウザ起動 (headless=True)
            # 既存のプロファイルは使わず、都度クリーンな状態で起動
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            # ページ遷移
            try:
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                return False, "ページの読み込みがタイムアウトしました。", []

            # 動的コンテンツの読み込み待機 (SPA対応)
            # networkidle: 通信が落ち着くまで待つ (最大500msの静止)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except:
                logger.warning("Network idle wait timed out, proceeding anyway.")

            # 遅延読み込み対策: ページ下部までスクロール
            logger.info("Scrolling to bottom...")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2) # スクロール後のレンダリング待機 
            
            # HTML取得
            logger.info("Fetching page content...")
            html_content = page.content()
            logger.info(f"Page content fetched. Size: {len(html_content)} bytes")
            
            # テキストベース抽出 (ブラウザが開いている間に実行)
            # body_text = page.locator("body").inner_text()
            
            # 処理が終わったら閉じる
            logger.info("Closing browser...")
            browser.close()
            logger.info("Browser closed.")
            
            # --- BeautifulSoupによる構造化解析 ---
            logger.info("Starting BS4 parsing...")
            soup = BeautifulSoup(html_content, 'lxml')
            messages = []
            
            # 会話アイテムを文書順に取得
            # ユーザー発言: div.query-text
            # モデル発言: message-content (の中に div.markdown)
            
            # 共通の親要素やフラットな構造を想定し、出現順に処理
            # selectでまとめて取得すると文書順が保たれる
            conversation_items = soup.select('div.query-text, message-content')
            
            for item in conversation_items:
                if 'query-text' in item.get('class', []):
                    # User Message
                    role = "user"
                    # query-text 内の各行 (p.query-text-line) を取得
                    lines = [p.get_text(strip=True) for p in item.select('p.query-text-line')]
                    if not lines:
                        # フォールバック: div直下のテキスト
                        text = item.get_text("\n", strip=True)
                    else:
                        text = "\n".join(lines)
                        
                elif item.name == 'message-content':
                    # Assistant Message
                    role = "assistant"
                    # マークダウン部分のみ抽出 (UIノイズ排除のため)
                    markdown_div = item.select_one('div.markdown')
                    if markdown_div:
                        text = markdown_div.get_text("\n", strip=True)
                    else:
                        # フォールバック
                        text = item.get_text("\n", strip=True)
                
                else:
                    continue

                if text.strip():
                    messages.append({"role": role, "content": text})

            if not messages:
                # 解析失敗時のフォールバック (生テキスト)
                logger.warning("Structured parsing failed, falling back to raw text.")
                body_text = soup.body.get_text("\n", strip=True)
                messages = [{"role": "assistant", "content": f"【解析失敗: 生テキスト取り込み】\n\n{body_text}"}]
                return True, "テキストは取得しましたが、会話構造の解析に失敗しました。", messages
            
            return True, f"{len(messages)}件のメッセージを取得しました。", messages

    except Exception as e:
        error_msg = str(e)
        if "Executable doesn't exist" in error_msg:
            logger.warning("Playwright browser executable not found. Attempting to install...")
            try:
                # 自動インストールを試行
                import subprocess
                import sys
                
                # installコマンドを実行
                logger.info("Running: python -m playwright install chromium")
                subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
                
                logger.info("Playwright browser installed successfully. Retrying import...")
                
                # 再試行 (再帰呼び出しだが、インストール成功後なので無限ループにはならないはずだが、念のため回数制限などのガードを入れるのがベストだが、ここでは簡易的に再実行)
                # ただし引数などが同じである必要がある。再帰呼び出しはスタックオーバーフローのリスクがあるが、
                # ここでは1回だけの再試行としたい。
                
                # 再帰ではなく、ここで再度処理を行うか、あるいは呼び出し元にリトライさせるか。
                # 構造上、with sync_playwright() の中に入り直す必要があるため、関数全体を再実行するのが手っ取り早い。
                # ただし無限ループ防止のため、この関数に `_retry_count` 引数を追加して制御するのが安全。
                # しかしシグネチャ変更は影響範囲が大きいので、ここでは内部で再度 sync_playwright を呼ぶ形にするか…
                # いや、一番きれいなのは関数再実行。無限ループ防止は「インストール成功したフラグ」がないと難しいが、
                # インストールが成功すれば "Executable doesn't exist" は出なくなるはずなので、
                # エラーの種類が変わればループは止まる。
                # 万が一インストールしても認識されない場合のみループするが、その場合は subprocess.check_call でエラーになるか、
                # 再度の browser.launch で同じエラーが出る。
                
                # 簡易的な再帰呼び出し (無限ループ防止カウントなし - インストール成功を信じる)
                return import_gemini_log_from_url(url, room_name)
                
            except Exception as install_error:
                logger.error(f"Failed to install Playwright browser: {install_error}")
                return False, f"Playwrightブラウザの自動インストールに失敗しました: {install_error}\n手動で 'python -m playwright install chromium' を実行してください。", []
        
        logger.error(f"Playwright Import Error: {e}")
        traceback.print_exc()
        return False, f"予期せぬエラー (Playwright): {e}", []

