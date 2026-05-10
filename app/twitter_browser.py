# twitter_browser.py
"""
Playwrightを使用したTwitter (X) のブラウザ操作を管理するモジュール。
セッション（Cookie）の管理と、実際の投稿処理を行う。

注意: Gradioサーバー内（既存のイベントループあり）から呼び出されるため、
asyncio.run() ではなく、新しいスレッドで専用イベントループを作成して実行する。
"""
import os
import json
import logging
import asyncio
import threading
from typing import Optional
from playwright.async_api import async_playwright
import playwright_utils

logger = logging.getLogger("twitter_browser")

# ボット検知回避用の標準的なデスクトップChromeの設定
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
DEFAULT_VIEWPORT = {"width": 1280, "height": 720}

def _run_async_in_new_loop(coro):
    """
    新しいスレッドにイベントループを作成して非同期関数を実行する。
    Gradioのイベントループと競合しないための安全なラッパー。
    """
    result = [None]
    exception = [None]

    def _thread_target():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result[0] = loop.run_until_complete(coro)
            loop.close()
        except Exception as e:
            exception[0] = e

    t = threading.Thread(target=_thread_target)
    t.start()
    t.join()

    if exception[0]:
        raise exception[0]
    return result[0]


async def _safe_launch_browser(p, headless: bool = True):
    """
    ブラウザを安全に起動する。バイナリ不足時は自動インストールを試行する。
    """
    try:
        return await p.chromium.launch(headless=headless)
    except Exception as e:
        if playwright_utils.is_executable_missing_error(e):
            logger.warning("Playwrightブラウザが見つかりません。自動インストールを試行します...")
            # インストール実行（ブロッキングだが専用スレッド内なので許容）
            if playwright_utils.ensure_playwright_browsers("chromium"):
                logger.info("ブラウザのインストールに成功しました。再試行します。")
                return await p.chromium.launch(headless=headless)
            else:
                raise Exception("Playwrightブラウザの自動インストールに失敗しました。手動で 'python -m playwright install chromium' を実行してください。") from e
        raise e


def _sanitize_cookies(cookies: list) -> list:
    """
    Playwrightが受け入れ可能な形式にCookieを調整する。
    特に sameSite 属性の形式エラーを防止する。
    """
    valid_samesite = ["Strict", "Lax", "None"]
    for cookie in cookies:
        if "sameSite" in cookie:
            # 文字列化して先頭大文字に (e.g. "lax" -> "Lax", "no_restriction" -> "No_restriction")
            ss = str(cookie["sameSite"]).capitalize()
            if ss not in valid_samesite:
                # 不明な値（"Unspecified" 等）は "None" にフォールバック
                cookie["sameSite"] = "None"
            else:
                cookie["sameSite"] = ss
    return cookies


async def _login_ui_async(cookies_path: str) -> bool:
    """
    Headedブラウザを起動し、ユーザーに手動ログインを促す。
    """
    async with async_playwright() as p:
        # ボット検知を避けるために User-Agent を設定
        browser = await _safe_launch_browser(p, headless=False)
        context = await browser.new_context(
            user_agent=DEFAULT_UA,
            viewport=DEFAULT_VIEWPORT
        )
        page = await context.new_page()

        logger.info("Twitterログイン用ブラウザを起動します。")
        # ログインページヘ直接行かずに、まずトップページを開いて人間らしさを出す
        await page.goto("https://x.com/")
        await asyncio.sleep(2)
        await page.goto("https://x.com/i/flow/login")

        print("--- ログイン操作を行ってください。ブラウザを閉じるとセッションが保存されます。 ---")

        # ブラウザが閉じられるのを待つ
        try:
            while browser.is_connected():
                await asyncio.sleep(1)
        except Exception:
            pass

        # 状態を保存
        try:
            state = await context.storage_state()
            os.makedirs(os.path.dirname(cookies_path), exist_ok=True)
            # 保存時にサニタイズしておく
            cookies = _sanitize_cookies(state.get('cookies', []))
            with open(cookies_path, 'w') as f:
                json.dump(cookies, f, indent=2)
            logger.info(f"セッション情報を {cookies_path} に保存しました。")
            return True
        except Exception as e:
            logger.error(f"セッション保存エラー: {e}")
            return False


async def _is_logged_in_async(cookies_path: str) -> bool:
    """現在のCookieでログイン状態にあるか確認する。"""
    if not os.path.exists(cookies_path):
        return False

    async with async_playwright() as p:
        browser = await _safe_launch_browser(p, headless=True)
        try:
            with open(cookies_path, 'r') as f:
                cookies = json.load(f)
            # 読み込み時にサニタイズを適用（手動インポートされたCookie対策）
            cookies = _sanitize_cookies(cookies)
            context = await browser.new_context(
                storage_state={"cookies": cookies},
                user_agent=DEFAULT_UA,
                viewport=DEFAULT_VIEWPORT
            )
        except Exception as e:
            logger.error(f"Cookieの読み込みに失敗しました: {e}")
            await browser.close()
            return False

        page = await context.new_page()
        try:
            await page.goto("https://x.com/home", wait_until="networkidle", timeout=15000)
            is_home = "login" not in page.url and "flow" not in page.url
            return is_home
        except Exception as e:
            logger.debug(f"ログイン状態の確認中にタイムアウト（無視可能）: {e}")
            return "login" not in page.url
        finally:
            await browser.close()


async def _post_tweet_async(cookies_path: str, text: str, reply_to_url: Optional[str] = None, media_paths: Optional[list] = None) -> str:
    """自動投稿を実行する。reply_to_url が指定されている場合はリプライとして投稿する。media_pathsがある場合は画像を添付する。"""
    if not os.path.exists(cookies_path):
        print(f"DEBUG [Twitter]: Cookieファイルが見つかりません: {cookies_path}")
        return "ERROR: Cookieファイルが見つかりません。再ログインしてください。"

    print(f"DEBUG [Twitter]: ブラウザを起動しています (reply_to={reply_to_url})...")
    async with async_playwright() as p:
        browser = await _safe_launch_browser(p, headless=True)
        try:
            with open(cookies_path, 'r') as f:
                cookies = json.load(f)
            cookies = _sanitize_cookies(cookies)
            context = await browser.new_context(
                storage_state={"cookies": cookies},
                user_agent=DEFAULT_UA,
                viewport=DEFAULT_VIEWPORT
            )
        except Exception as e:
            msg = f"Cookieの読み込みに失敗しました: {e}"
            await browser.close()
            return f"ERROR: {msg}"

        page = await context.new_page()
        try:
            if reply_to_url:
                print(f"DEBUG [Twitter]: リプライ先 ({reply_to_url}) へ移動中...")
                await page.goto(reply_to_url, wait_until="domcontentloaded", timeout=30000)
                # リプライボックスを探す (data-testid="tweetTextarea_0" はリプライ時も共通の場合が多い)
                # または「返信をツイート」プレースホルダーを持つ要素をクリックする必要がある場合もある
                textarea_selector = '[data-testid="tweetTextarea_0"]'
                try:
                    await page.wait_for_selector(textarea_selector, timeout=15000)
                except Exception:
                    # ページ下部の返信欄が見つからない場合、一度クリックが必要かもしれない
                    print("DEBUG [Twitter]: 直接テキストエリアが見つかりません。返信欄を探します...")
                    # 簡易的な対応：ページ内のそれっぽい場所をクリック
                    await page.click('div[role="textbox"]', force=True) 
                    await page.wait_for_selector(textarea_selector, timeout=10000)
            else:
                print(f"DEBUG [Twitter]: 投稿画面 (https://x.com/compose/post) へ移動中...")
                await page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=30000)
                textarea_selector = 'div[role="dialog"] [data-testid="tweetTextarea_0"]'
                await page.wait_for_selector(textarea_selector, timeout=20000)

            print("DEBUG [Twitter]: テキストを入力中...")
            await page.focus(textarea_selector)
            # 全体を一度に fill するのではなく、keyboard.type で一文字ずつ打つことで React 等のイベントを確実に発火させる
            await page.keyboard.type(text, delay=10)
            await asyncio.sleep(1)

            # 画像添付
            if media_paths:
                print(f"DEBUG [Twitter]: 画像を添付中 ({len(media_paths)}枚)...")
                # 投稿ダイアログまたはインライン返信欄の中のファイル入力を探す
                file_input_selector = 'input[data-testid="fileInput"]'
                try:
                    # 複数ある可能性があるが、Playwrightは最初に見つかったものを使う
                    await page.wait_for_selector(file_input_selector, timeout=10000)
                    # 絶対パスに変換
                    abs_paths = [os.path.abspath(p) for p in media_paths if os.path.exists(p)]
                    if abs_paths:
                        await page.set_input_files(file_input_selector, abs_paths)
                        print(f"DEBUG [Twitter]: ファイルセット完了: {abs_paths}")
                        # アップロード完了待ち（インジケータが出るため、少し待機が必要）
                        await asyncio.sleep(3)
                    else:
                        print("DEBUG [Twitter]: 有効な画像パスが見つかりませんでした。")
                except Exception as e:
                    print(f"DEBUG [Twitter]: 画像添付中にエラーが発生しました（継続します）: {e}")

            # 投稿ボタンの設定 (リプライ時はInline等、新規作成時はモーダル内を厳密に指定)
            if reply_to_url:
                btn_selector = '[data-testid="tweetButtonInline"], [data-testid="tweetButton"]'
            else:
                btn_selector = 'div[role="dialog"] [data-testid="tweetButton"]'
            
            # ボタンが有効化されるのを待つ (aria-disabled="true" が外れるのを待つ)
            target_element = None
            for _ in range(15): # 最大15秒ポーリング
                btns = await page.query_selector_all(btn_selector)
                for btn in btns:
                    try:
                        if await btn.is_visible():
                            is_disabled = await btn.get_attribute("aria-disabled") == "true"
                            if not is_disabled:
                                target_element = btn
                                break
                    except Exception:
                        pass # 要素がDOMから消えた等の例外は無視
                        
                if target_element:
                    break
                await asyncio.sleep(1)
                
            if not target_element:
                return "ERROR: 投稿ボタンが有効になりませんでした。文字数制限（全角140文字）を超えているか、制限の可能性があります。"
            
            print("DEBUG [Twitter]: 投稿ボタンをクリック...")
            # Playwrightのネイティブclick()はマウス座標ベースのため、
            # モーダルの背景オーバーレイに吸い込まれる問題がある。
            # JavaScriptのelement.click()でDOM上で直接イベントを発火させる。
            await target_element.evaluate("el => el.click()")

            print("DEBUG [Twitter]: 投稿完了を検証中...")
            # モーダル（投稿ダイアログ）が閉じたかどうかで成否を判定する
            posted = False
            for i in range(10): # 最大10秒待機
                await asyncio.sleep(1)
                dialog = await page.query_selector('div[role="dialog"] [data-testid="tweetTextarea_0"]')
                if not dialog:
                    posted = True
                    print(f"DEBUG [Twitter]: モーダルが閉じました (投稿成功と判定, {i+1}秒後)")
                    break
            
            if posted:
                logger.info("投稿が完了しました。")
                return "SUCCESS"
            else:
                msg = "投稿ボタンをクリックしましたが、モーダルが閉じませんでした。投稿が実際に送信されなかった可能性があります。"
                logger.error(msg)
                return f"ERROR: {msg}"

        except Exception as e:
            if "Timeout" in str(e):
                msg = f"投稿実行中にタイムアウトしました。ログインセッションが切れているか、通信状況が不安定な可能性があります。"
            else:
                msg = f"投稿実行中にエラーが発生しました: {str(e)}"
            logger.error(msg)
            return msg
        finally:
            await browser.close()

async def _get_timeline_async(cookies_path: str, url: str = "https://x.com/home", count: int = 10) -> list:
    """タイムラインからツイートを取得する(スクレイピング)"""
    if not os.path.exists(cookies_path):
        return []

    async with async_playwright() as p:
        browser = await _safe_launch_browser(p, headless=True)
        try:
            with open(cookies_path, 'r') as f:
                cookies = json.load(f)
            context = await browser.new_context(storage_state={"cookies": _sanitize_cookies(cookies)}, user_agent=DEFAULT_UA)
            page = await context.new_page()
            
            print(f"DEBUG [Twitter]: {url} を取得中...")
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            
            # JSの描画やネットワーク待ちのための猶予
            await page.wait_for_timeout(3000)
            
            # ツイート要素が読み込まれるのを待つ
            await page.wait_for_selector('[data-testid="tweet"]', timeout=15000)
            
            # 念のためもう一度猶予（上部に新しいツイートが挿入されるアニメーション等を待つ）
            await page.wait_for_timeout(1000)
            
            tweet_elements = await page.query_selector_all('[data-testid="tweet"]')
            results = []
            
            for el in tweet_elements[:count]:
                try:
                    # テキスト
                    text_el = await el.query_selector('[data-testid="tweetText"]')
                    text = await text_el.inner_text() if text_el else ""
                    
                    # ユーザー名
                    user_el = await el.query_selector('[data-testid="User-Name"]')
                    user_info = await user_el.inner_text() if user_el else ""
                    
                    # 個別URL (aタグのhrefから推測)
                    # 通常 status/ID 形式のリンクが含まれる
                    links = await el.query_selector_all('a')
                    tweet_url = ""
                    for link in links:
                        href = await link.get_attribute("href")
                        if href and "/status/" in href:
                            tweet_url = f"https://x.com{href.split('?')[0]}"
                            break
                    
                    results.append({
                        "text": text,
                        "author": user_info.replace("\n", " "),
                        "url": tweet_url,
                        "id": tweet_url.split("/")[-1] if "/status/" in tweet_url else ""
                    })
                except Exception as e:
                    logger.debug(f"ツイート要素の解析失敗: {e}")
                    continue
                    
            return results
        except Exception as e:
            logger.error(f"タイムライン取得エラー: {e}")
            return []
        finally:
            await browser.close()

async def _get_tweet_thread_async(cookies_path: str, url: str, max_depth: int = 5) -> list:
    """指定されたツイートURLへ遷移し、画面上の親ツイート(スレッド)を取得する"""
    if not os.path.exists(cookies_path):
        return []

    async with async_playwright() as p:
        browser = await _safe_launch_browser(p, headless=True)
        try:
            with open(cookies_path, 'r') as f:
                cookies = json.load(f)
            context = await browser.new_context(storage_state={"cookies": _sanitize_cookies(cookies)}, user_agent=DEFAULT_UA)
            page = await context.new_page()
            
            print(f"DEBUG [Twitter]: スレッド取得のため {url} に遷移中...")
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            
            await page.wait_for_timeout(3000)
            await page.wait_for_selector('[data-testid="tweet"]', timeout=15000)
            await page.wait_for_timeout(1000)
            
            tweet_elements = await page.query_selector_all('[data-testid="tweet"]')
            thread_tweets = []
            target_id = url.split('/')[-1].split('?')[0] if '/status/' in url else ""
            
            for el in tweet_elements:
                try:
                    text_el = await el.query_selector('[data-testid="tweetText"]')
                    text = await text_el.inner_text() if text_el else ""
                    
                    user_el = await el.query_selector('[data-testid="User-Name"]')
                    user_info = await user_el.inner_text() if user_el else ""
                    
                    links = await el.query_selector_all('a')
                    tweet_url = ""
                    for link in links:
                        href = await link.get_attribute("href")
                        if href and "/status/" in href:
                            tweet_url = f"https://x.com{href.split('?')[0]}"
                            break
                            
                    tweet_id = tweet_url.split('/')[-1] if '/status/' in tweet_url else ""
                    
                    if text or user_info:
                        thread_tweets.append({
                            "text": text,
                            "author": user_info.replace("\n", " "),
                            "url": tweet_url,
                            "id": tweet_id
                        })
                    
                    # 目的のツイートまで到達した場合は以降のスクレイピングを中止
                    # （リプライ画面の構造上、対象ツイートの下に他の人のリプライが続くため）
                    if target_id and tweet_id == target_id:
                        break
                        
                except Exception as e:
                    logger.debug(f"スレッド要素の解析失敗: {e}")
                    continue
            
            # 指定された深さ（件数）まで絞る（リストの後ろ＝最新・対象に近いもの を残す）
            if len(thread_tweets) > max_depth:
                thread_tweets = thread_tweets[-max_depth:]
                
            return thread_tweets
        except Exception as e:
            logger.error(f"スレッド取得中にエラー: {e}")
            return []
        finally:
            await browser.close()

# === 公開API（同期ラッパー） ===

def run_login_ui(cookies_path: str) -> bool:
    return _run_async_in_new_loop(_login_ui_async(cookies_path))

def run_post_tweet(cookies_path: str, text: str, reply_to_url: Optional[str] = None, media_paths: Optional[list] = None) -> str:
    return _run_async_in_new_loop(_post_tweet_async(cookies_path, text, reply_to_url, media_paths))

def run_check_session(cookies_path: str) -> bool:
    return _run_async_in_new_loop(_is_logged_in_async(cookies_path))

def run_get_timeline(cookies_path: str, count: int = 10) -> list:
    return _run_async_in_new_loop(_get_timeline_async(cookies_path, "https://x.com/home", count))

def run_get_mentions(cookies_path: str, count: int = 10) -> list:
    return _run_async_in_new_loop(_get_timeline_async(cookies_path, "https://x.com/notifications/mentions", count))

def run_get_notifications(cookies_path: str, count: int = 10) -> list:
    return _run_async_in_new_loop(_get_timeline_async(cookies_path, "https://x.com/notifications", count))

def run_get_tweet_thread(cookies_path: str, url: str, max_depth: int = 5) -> list:
    return _run_async_in_new_loop(_get_tweet_thread_async(cookies_path, url, max_depth))
