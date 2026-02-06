import os
import json
import logging
import time
import traceback
from typing import List, Dict, Tuple, Optional
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

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
    if not url.startswith("https://gemini.google.com/share/"):
        return False, "無効なURLです。'https://gemini.google.com/share/' で始まるURLを指定してください。", []

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
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2) # スクロール後のレンダリング待機 
            
            # HTML取得
            html_content = page.content()
            
            # テキストベース抽出 (ブラウザが開いている間に実行)
            # body_text = page.locator("body").inner_text()
            
            # 処理が終わったら閉じる
            browser.close()
            
            # --- BeautifulSoupによる構造化解析 ---
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
        logger.error(f"Playwright Import Error: {e}")
        traceback.print_exc()
        return False, f"予期せぬエラー (Playwright): {e}", []
