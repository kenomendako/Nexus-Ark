# twitter_manager.py
import json
import os
import logging
import datetime
import re
import unicodedata
from collections import deque
from typing import List, Dict, Any, Optional

import config_manager
import config_manager
import constants
from twitter_browser import run_login_ui, run_post_tweet, run_check_session
from twitter_api import TwitterAPI

logger = logging.getLogger("twitter_manager")

# Twitter関連データの保存パス
TWITTER_DATA_DIR = os.path.join(constants.METADATA_DIR, "twitter")
PENDING_TWEETS_FILE = os.path.join(TWITTER_DATA_DIR, "pending_tweets.json")
TWEET_HISTORY_FILE = os.path.join(TWITTER_DATA_DIR, "tweet_history.json")
COOKIES_FILE = os.path.join(TWITTER_DATA_DIR, "cookies.json")

# 初期化
if not os.path.exists(TWITTER_DATA_DIR):
    os.makedirs(TWITTER_DATA_DIR, exist_ok=True)

class TwitterManager:
    """
    Twitter (X) 連携のバックエンドロジックを管理するクラス。
    承認待ちキュー、プライバシーフィルタ、投稿履歴を扱う。
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TwitterManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
            
        self.pending_tweets = self._load_json(PENDING_TWEETS_FILE, [])
        self.tweet_history = self._load_json(TWEET_HISTORY_FILE, [])
        self._pending_feed = []  # UI経由の通知リレー用バッファ
        self.last_fetched_tweets = [] # AIがリストの番号でリプライ先を選べるようにするための参照キャッシュ
        self._initialized = True

    def _load_json(self, file_path: str, default: Any) -> Any:
        try:
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load Twitter data from {file_path}: {e}")
        return default

    def _save_json(self, file_path: str, data: Any):
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save Twitter data to {file_path}: {e}")

    @staticmethod
    def calculate_twitter_length(text: str) -> int:
        """
        Twitterの文字数カウント方式に近い計算を行う。
        - 半角英数・基本ASCII: 1文字
        - 全角文字（日本語、絵文字等）: 2文字
        - URL: 23文字固定（t.co短縮）
        """
        # まずURLを抽出して一旦除去し、各URLを23文字としてカウント
        url_pattern = re.compile(r'https?://\S+')
        urls = url_pattern.findall(text)
        text_without_urls = url_pattern.sub('', text)
        url_weight = len(urls) * 23

        char_count = 0
        for char in text_without_urls:
            # East Asian Width: W (Wide), F (Fullwidth), A (Ambiguous) は2文字扱いとする（アンダーカウントによるエラー防止のため）
            ea = unicodedata.east_asian_width(char)
            if ea in ('W', 'F', 'A'):
                char_count += 2
            else:
                char_count += 1
        
        return char_count + url_weight

    def _get_full_twitter_settings(self, room_name: str) -> Dict[str, Any]:
        """ルーム設定からTwitter設定全体を取得するヘルパー"""
        if room_name:
            try:
                import room_manager
                room_config = room_manager.get_room_config(room_name) or {}
                twitter_settings = room_config.get("override_settings", {}).get("twitter_settings")
                if twitter_settings is None:
                    twitter_settings = room_config.get("twitter_settings", {})
                return twitter_settings
            except Exception as e:
                logger.warning(f"Failed to load room config for {room_name}: {e}")
        return {}

    def apply_privacy_filter(self, text: str, room_name: Optional[str] = None) -> Dict[str, Any]:
        """
        投稿内容にプライバシーフィルタを適用する。
        """
        original_text = text
        modified_text = text
        warnings = []
        
        settings = self._get_full_twitter_settings(room_name) if room_name else {}
        is_premium = settings.get("is_premium", False)
        enable_filter = settings.get("enable_privacy_filter", True)
        
        # 1. 既存の redaction_rules.json を適用 (名前等の置換)
        if enable_filter:
            try:
                rules = config_manager.load_redaction_rules()
                for rule in rules:
                    find_str = rule.get("find", "")
                    replace_str = rule.get("replace", "")
                    if find_str and find_str in modified_text:
                        modified_text = modified_text.replace(find_str, replace_str)
            except Exception as e:
                logger.warning(f"Error applying redaction rules: {e}")

            # 2. パターン検知 (URL, 電話番号, 住所などの簡易チェック)
            # URL (簡易)
            if re.search(r'https?://[^\s一-龠ぁ-んァ-ヶ]+', modified_text):
                warnings.append("URLが含まれています。誤ってプライベートなリンクを共有していないか確認してください。")
                
            # 電話番号 (簡易)
            if re.search(r'\d{2,4}-\d{2,4}-\d{4}', modified_text):
                warnings.append("電話番号のようなパターンが検出されました。")
            
        # 3. 文字数チェック (Twitter weighted length)
        tw_length = self.calculate_twitter_length(modified_text)
        limit = 25000 if is_premium else 280
        warn_limit = 24900 if is_premium else 260
        
        if tw_length > limit:
            warnings.append(f"⚠️ 文字数制限超過: Twitter換算 {tw_length}/{limit}文字です。投稿前に短縮してください。")
        elif tw_length > warn_limit:
            warnings.append(f"文字数が制限に近づいています ({tw_length}/{limit}文字)。")

        return {
            "original": original_text,
            "filtered": modified_text,
            "is_modified": original_text != modified_text,
            "warnings": warnings,
            "twitter_length": tw_length
        }

    def add_draft(self, content: str, room_name: str, author_type: str = "persona", reply_to_url: Optional[str] = None, reply_to_id: Optional[str] = None) -> str:
        """
        承認待ちキューに下書きを追加する。
        """
        filter_result = self.apply_privacy_filter(content, room_name=room_name)
        
        # URLがなくIDがある場合、汎用フォーマットのURLを構築する
        if reply_to_id and not reply_to_url:
            reply_to_url = f"https://x.com/i/status/{reply_to_id}"
        
        draft_id = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
        draft_entry = {
            "id": draft_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "room_name": room_name,
            "author_type": author_type,
            "original_content": filter_result["original"],
            "filtered_content": filter_result["filtered"],
            "status": "pending",
            "warnings": filter_result["warnings"],
            "reply_to_url": reply_to_url,
            "reply_to_id": reply_to_id
        }
        
        self.pending_tweets.append(draft_entry)
        self._save_json(PENDING_TWEETS_FILE, self.pending_tweets)
        return draft_id

    def approve_tweet(self, draft_id: str, edited_content: Optional[str] = None, edited_reply_url: Optional[str] = None) -> bool:
        """
        下書きを承認する。
        """
        for i, draft in enumerate(self.pending_tweets):
            if draft["id"] == draft_id:
                target = self.pending_tweets.pop(i)
                target["status"] = "approved"
                if edited_content:
                    target["final_content"] = edited_content
                else:
                    target["final_content"] = target["filtered_content"]
                
                # 新しく編集されたURLがあれば上書き、空文字ならNoneにする
                if edited_reply_url is not None:
                    target["reply_to_url"] = edited_reply_url.strip() if edited_reply_url.strip() else None
                    if not target["reply_to_url"]:
                        target["reply_to_id"] = None
                
                target["approved_at"] = datetime.datetime.now().isoformat()
                
                # リプライ情報の引き継ぎ
                # (target には既に reply_to_url 等が含まれている可能性がある)
                
                # 履歴に追加 (まだ投稿はされていない)
                self.tweet_history.append(target)
                self._save_json(PENDING_TWEETS_FILE, self.pending_tweets)
                self._save_json(TWEET_HISTORY_FILE, self.tweet_history)
                return True
        return False

    def reject_tweet(self, draft_id: str):
        """
        下書きを却下する。
        """
        self.pending_tweets = [d for d in self.pending_tweets if d["id"] != draft_id]
        self._save_json(PENDING_TWEETS_FILE, self.pending_tweets)

    def mark_as_posted(self, draft_id: str, post_url: str = ""):
        """
        投稿完了としてマークする。
        """
        for tweet in self.tweet_history:
            if tweet["id"] == draft_id:
                tweet["status"] = "posted"
                tweet["posted_at"] = datetime.datetime.now().isoformat()
                tweet["post_url"] = post_url
                break
        self._save_json(TWEET_HISTORY_FILE, self.tweet_history)

    def mark_as_failed(self, draft_id: str, error_msg: str):
        """
        投稿失敗としてマークする。
        """
        for tweet in self.tweet_history:
            if tweet["id"] == draft_id:
                tweet["status"] = "failed"
                tweet["error"] = error_msg
                tweet["failed_at"] = datetime.datetime.now().isoformat()
                break
        self._save_json(TWEET_HISTORY_FILE, self.tweet_history)

    def delete_history_item(self, draft_id: str):
        """
        投稿履歴から項目を削除する。
        """
        self.tweet_history = [d for d in self.tweet_history if d["id"] != draft_id]
        self._save_json(TWEET_HISTORY_FILE, self.tweet_history)

    def move_back_to_drafts(self, history_id: str) -> bool:
        """
        投稿履歴（失敗・投稿済み）から下書きキューに差し戻す。
        内容を引き継ぎ、ステータスを 'pending' にリセットする。
        """
        target = None
        for i, tweet in enumerate(self.tweet_history):
            if tweet["id"] == history_id:
                target = self.tweet_history.pop(i)
                break
        
        if not target:
            return False
        
        error_msg = target.get("error")
        warnings = []
        if error_msg:
            warnings.append(f"前回投稿失敗: {error_msg}")

        # 新しいIDを振り直して下書きとして追加
        new_id = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
        draft_entry = {
            "id": new_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "room_name": target.get("room_name", ""),
            "author_type": target.get("author_type", "user"),
            "original_content": target.get("final_content") or target.get("filtered_content") or target.get("original_content", ""),
            "filtered_content": target.get("final_content") or target.get("filtered_content") or target.get("original_content", ""),
            "status": "pending",
            "warnings": warnings,
            "reply_to_url": target.get("reply_to_url"),
            "reply_to_id": target.get("reply_to_id")
        }
        
        self.pending_tweets.append(draft_entry)
        self._save_json(PENDING_TWEETS_FILE, self.pending_tweets)
        self._save_json(TWEET_HISTORY_FILE, self.tweet_history)
        logger.info(f"履歴 {history_id} を下書き {new_id} に差し戻しました。")
        return True

    def get_pending_list(self) -> List[Dict]:
        return self.pending_tweets

    def get_history_list(self) -> List[Dict]:
        return sorted(self.tweet_history, key=lambda x: x.get("timestamp", ""), reverse=True)

    # --- 通知リレー (Pending Feed) ---

    def set_pending_feed(self, feed_type: str, items: List[Dict]):
        """UI経由で取得したフィード（メンション・通知）をペルソナ通知用に蓄積する"""
        for item in items:
            entry = {
                "feed_type": feed_type,
                "author": item.get("author", "Unknown"),
                "text": item.get("text", ""),
                "url": item.get("url", ""),
                "id": item.get("id", "")
            }
            # 重複チェック（同じURLが既にバッファにあればスキップ）
            if not any(e["url"] == entry["url"] and entry["url"] for e in self._pending_feed):
                self._pending_feed.append(entry)
        logger.info(f"通知リレー: {feed_type} から {len(items)} 件を蓄積 (合計 {len(self._pending_feed)} 件)")

    def consume_pending_feed(self, room_name: str = None) -> List[Dict]:
        """蓄積されたフィードを取得し、バッファをクリアする"""
        items = self._pending_feed[:]
        self._pending_feed.clear()
        
        if items and room_name:
            items = self.resolve_thread_context(items, room_name)
            
        return items

    def resolve_thread_context(self, items: List[Dict], room_name: str) -> List[Dict]:
        """通知（メンション）の親ツイートを遡って取得し、スレッド履歴として文脈を付加する"""
        if not room_name or not items:
            return items
            
        try:
            import room_manager
            room_config = room_manager.get_room_config(room_name) or {}
            twitter_settings = room_config.get("override_settings", {}).get("twitter_settings", {})
            
            # スレッド取得がOFFの場合は何もしない
            if not twitter_settings.get("fetch_thread_enabled", False):
                return items
                
            use_api = twitter_settings.get("use_api", False)
            max_depth = twitter_settings.get("thread_fetch_count", 3)
            
            # APIモードの場合の初期化
            api_client = None
            if use_api:
                api_config = twitter_settings.get("api_config", {})
                if all(k in api_config and api_config[k] for k in ["api_key", "api_secret", "access_token", "access_token_secret"]):
                    from twitter_api import TwitterAPI
                    api_client = TwitterAPI(
                        api_config["api_key"], api_config["api_secret"],
                        api_config["access_token"], api_config["access_token_secret"]
                    )
            
            for item in items:
                # 対象のURLかIDを取得
                url = item.get("url")
                tweet_id = item.get("id")
                
                if not url and not tweet_id:
                    continue
                    
                thread_texts = []
                
                if use_api and api_client and tweet_id:
                    logger.info(f"APIモードでスレッドを取得中... (ID: {tweet_id}, depth: {max_depth})")
                    thread_data = api_client.get_tweet_thread(tweet_id, max_depth)
                    if thread_data and len(thread_data) > 1: # 自身しかない場合は無視
                        for t in thread_data:
                            author_name = t.get('author') if t.get('author') else 'Unknown'
                            thread_texts.append(f"{author_name}: {t.get('text')}")
                elif not use_api and url:
                    logger.info(f"ブラウザモードでスレッドを取得中... (URL: {url}, depth: {max_depth})")
                    from twitter_browser import run_get_tweet_thread
                    thread_data = run_get_tweet_thread(COOKIES_FILE, url, max_depth)
                    if thread_data and len(thread_data) > 1:
                        for t in thread_data:
                            author_name = t.get('author') if t.get('author') else 'Unknown'
                            thread_texts.append(f"{author_name}: {t.get('text')}")
                
                if thread_texts:
                    # スレッド文字列を構成し、元のtextを書き換える
                    thread_str = "\n".join(thread_texts)
                    item["text"] = f"【会話スレッド】\n{thread_str}"
                    
            return items
            
        except Exception as e:
            logger.error(f"スレッドコンテキスト解決エラー: {e}")
            return items

    # --- リプライ済みトラッキング ---

    def get_replied_urls(self) -> set:
        """履歴からリプライ済みのURLセットを返す"""
        urls = set()
        for tweet in self.tweet_history:
            reply_url = tweet.get("reply_to_url")
            if reply_url and tweet.get("status") in ("posted", "approved"):
                urls.add(reply_url)
        return urls

    def is_already_replied(self, tweet_url: str) -> bool:
        """指定URLに既にリプライ済みかどうかを判定する"""
        if not tweet_url:
            return False
        return tweet_url in self.get_replied_urls()

    # --- 設定ヘルパー ---

    def is_auto_post_enabled(self, room_name: str) -> bool:
        """自動投稿（承認なし投稿）が有効かどうかを判定する"""
        try:
            import room_manager
            room_config = room_manager.get_room_config(room_name) or {}
            twitter_settings = room_config.get("override_settings", {}).get("twitter_settings", {})
            return twitter_settings.get("auto_post", False)
        except Exception:
            return False

    def should_notify_on_approval(self, room_name: str) -> bool:
        """承認要請時にスマホ通知を送信すべきかどうかを判定する"""
        try:
            import room_manager
            room_config = room_manager.get_room_config(room_name) or {}
            twitter_settings = room_config.get("override_settings", {}).get("twitter_settings", {})
            return twitter_settings.get("notify_on_approval_request", False)
        except Exception:
            return False

    # --- Phase 3: タイムライン取得統合 ---

    def fetch_timeline(self, room_name: str, count: int = 10) -> List[Dict[str, Any]]:
        """現在の設定に基づいてタイムラインを取得する"""
        use_api, api_config = self._get_twitter_config(room_name)
        
        if use_api:
            api = TwitterAPI(
                consumer_key=api_config.get("api_key", ""),
                consumer_secret=api_config.get("api_secret", ""),
                access_token=api_config.get("access_token", ""),
                access_token_secret=api_config.get("access_token_secret", "")
            )
            res = api.get_home_timeline(count)
        else:
            from twitter_browser import run_get_timeline
            res = run_get_timeline(COOKIES_FILE, count)
            
        self.last_fetched_tweets = res
        return res

    def fetch_mentions(self, room_name: str, count: int = 10) -> List[Dict[str, Any]]:
        """現在の設定に基づいてメンションを取得する"""
        use_api, api_config = self._get_twitter_config(room_name)
        
        if use_api:
            api = TwitterAPI(
                consumer_key=api_config.get("api_key", ""),
                consumer_secret=api_config.get("api_secret", ""),
                access_token=api_config.get("access_token", ""),
                access_token_secret=api_config.get("access_token_secret", "")
            )
            res = api.get_mentions(count)
        else:
            from twitter_browser import run_get_mentions
            res = run_get_mentions(COOKIES_FILE, count)
        
        self.last_fetched_tweets = res
        return res

    def fetch_notifications(self, room_name: str, count: int = 10) -> List[Dict[str, Any]]:
        """現在の設定に基づいて全通知（引用RT等含む）を取得する"""
        use_api, api_config = self._get_twitter_config(room_name)
        
        # 通知全体（引用RT含む）はブラウザモードでのみ完全に取得可能
        if not use_api:
            from twitter_browser import run_get_notifications
            res = run_get_notifications(COOKIES_FILE, count)
        else:
            # APIモード時はメンション取得で代用
            res = self.fetch_mentions(room_name, count)
            
        self.last_fetched_tweets = res
        return res

    def _get_twitter_config(self, room_name: str):
        """ルーム設定からTwitter設定を取得する内部ヘルパー"""
        use_api = False
        api_config = {}
        if room_name:
            try:
                import room_manager
                room_config = room_manager.get_room_config(room_name) or {}
                twitter_settings = room_config.get("override_settings", {}).get("twitter_settings")
                if twitter_settings is None:
                    twitter_settings = room_config.get("twitter_settings", {})
                
                use_api = twitter_settings.get("use_api", False)
                api_config = twitter_settings.get("api_config", {})
            except Exception as e:
                logger.warning(f"Failed to load room config for {room_name}: {e}")
        return use_api, api_config

    # --- Phase 2: ブラウザ操作統合 ---

    def is_logged_in(self) -> bool:
        """現在のセッションが有効か確認する"""
        from twitter_browser import run_check_session
        return run_check_session(COOKIES_FILE)

    def start_login(self) -> bool:
        """ログインUI（ブラウザ）を起動する"""
        from twitter_browser import run_login_ui
        return run_login_ui(COOKIES_FILE)

    def import_cookies(self, cookies_json: str) -> bool:
        """手動で取得したCookieをインポートする"""
        try:
            cookies = json.loads(cookies_json)
            # 形式チェック (リストであることを確認)
            if not isinstance(cookies, list):
                logger.error("Invalid cookie format: expected a list.")
                return False
            
            # 保存
            self._save_json(COOKIES_FILE, cookies)
            logger.info("Successfully imported cookies manually.")
            return True
        except Exception as e:
            logger.error(f"Failed to import cookies: {e}")
            return False

    def execute_post(self, draft_id: str, room_name: Optional[str] = None) -> Dict[str, Any]:
        """
        承認済み下書きを実際にTwitterに投稿する。
        """
        # 下書きを探す
        target_tweet = None
        for tweet in self.tweet_history:
            if tweet["id"] == draft_id and tweet["status"] == "approved":
                target_tweet = tweet
                break
        
        if not target_tweet:
            return {"success": False, "error": "承認された下書きが見つかりません。"}
        
        content = target_tweet.get("final_content", "")
        if not content:
            return {"success": False, "error": "投稿内容が空です。"}

        # ルーム名が指定されていない場合はツイートデータから取得
        if not room_name:
            room_name = target_tweet.get("room_name")

        # 設定の読み込み
        use_api, api_config = self._get_twitter_config(room_name)

        if use_api:
            # APIモードで投稿
            api = TwitterAPI(
                consumer_key=api_config.get("api_key", ""),
                consumer_secret=api_config.get("api_secret", ""),
                access_token=api_config.get("access_token", ""),
                access_token_secret=api_config.get("access_token_secret", "")
            )
            # リプライIDがあれば渡す
            reply_to_id = target_tweet.get("reply_to_id")
            tweet_id = api.post_tweet(content, in_reply_to_tweet_id=reply_to_id)
            if tweet_id:
                post_url = f"https://x.com/i/status/{tweet_id}"
                self.mark_as_posted(draft_id, post_url)
                return {"success": True, "method": "api", "url": post_url}
            else:
                error_msg = "Twitter API 経由の投稿に失敗しました。キー設定やクレジット残高を確認してください。"
                self.mark_as_failed(draft_id, error_msg)
                return {"success": False, "error": error_msg}
        else:
            # ブラウザモードで投稿
            try:
                from twitter_browser import run_post_tweet
                # リプライURLがあれば渡す
                reply_to_url = target_tweet.get("reply_to_url")
                result = run_post_tweet(COOKIES_FILE, content, reply_to_url=reply_to_url)
                
                if result == "SUCCESS":
                    self.mark_as_posted(draft_id, reply_to_url or "https://x.com/home")
                    return {"success": True, "url": reply_to_url or "https://x.com/home", "method": "browser"}
                else:
                    error_msg = f"ブラウザ投稿中にエラーが発生しました: {result}"
                    self.mark_as_failed(draft_id, error_msg)
                    return {"success": False, "error": error_msg}
            except Exception as e:
                error_msg = f"ブラウザ操作中に例外が発生しました: {str(e)}"
                self.mark_as_failed(draft_id, error_msg)
                return {"success": False, "error": error_msg}

# グローバルインスタンス
twitter_manager = TwitterManager()
