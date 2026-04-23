# twitter_api.py
"""
Twitter API (v2) を使用してツイートを投稿するためのモジュール。
tweepy を使用して OAuth 1.0a 認証を行い、API v2 の投稿エンドポイントを叩く。
"""
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger("twitter_api")

try:
    import tweepy
except ImportError:
    logger.error("tweepy がインストールされていません。'uv pip install tweepy' を実行してください。")
    tweepy = None


class TwitterAPI:
    def __init__(self, consumer_key: str, consumer_secret: str, access_token: str, access_token_secret: str):
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret
        self.client = None

        if tweepy and all([consumer_key, consumer_secret, access_token, access_token_secret]):
            try:
                # Twitter API v2 クライアントの初期化
                # (POST /2/tweets には OAuth 1.0a User Context が必要)
                self.client = tweepy.Client(
                    consumer_key=self.consumer_key,
                    consumer_secret=self.consumer_secret,
                    access_token=self.access_token,
                    access_token_secret=self.access_token_secret
                )
                logger.info("Twitter API v2 クライアントを初期化しました。")
            except Exception as e:
                logger.error(f"Twitter API クライアント初期化エラー: {e}")

    def post_tweet(self, text: str, in_reply_to_tweet_id: Optional[str] = None) -> Optional[str]:
        """ツイートを投稿し、成功した場合はツイートIDを返す"""
        if not self.client:
            logger.error("Twitter API クライアントが初期化されていません。キーの設定を確認してください。")
            return None

        try:
            logger.info(f"API経由でツイートを投稿します (reply_to={in_reply_to_tweet_id}): {text[:30]}...")
            response = self.client.create_tweet(text=text, in_reply_to_tweet_id=in_reply_to_tweet_id)
            
            # response.data には投稿されたツイートの情報 (id, text) が含まれる
            if response and response.data:
                tweet_id = str(response.data.get("id"))
                logger.info(f"ツイートの投稿に成功しました。ID: {tweet_id}")
                return tweet_id
            else:
                logger.error("ツイートの投稿に失敗しました（レスポンスが空です）。")
                return None

        except Exception as e:
            logger.error(f"Twitter API 投稿エラー: {e}")
            return None

    def get_home_timeline(self, count: int = 20) -> List[Dict[str, Any]]:
        """ホームタイムラインを取得する"""
        if not self.client:
            return []

        try:
            # API v2 では get_home_timeline が利用可能（特定の権限が必要）
            # もし失敗する場合は代替手段を検討
            response = self.client.get_home_timeline(max_results=min(100, max(10, count)), user_auth=False)
            tweets = []
            if response and response.data:
                for tweet in response.data:
                    tweets.append({
                        "id": str(tweet.id),
                        "text": tweet.text,
                        "author_id": str(tweet.author_id) if hasattr(tweet, "author_id") else None,
                        "created_at": tweet.created_at.isoformat() if hasattr(tweet, "created_at") and tweet.created_at else None
                    })
            return tweets
        except Exception as e:
            logger.error(f"Twitter API タイムライン取得エラー: {e}")
            return []

    def get_mentions(self, count: int = 20) -> List[Dict[str, Any]]:
        """自分宛のメンションを取得する"""
        if not self.client:
            return []

        try:
            me = self.client.get_me()
            if not me or not me.data:
                return []
            
            my_id = me.data.id
            response = self.client.get_users_mentions(id=my_id, max_results=min(100, max(5, count)))
            
            mentions = []
            if response and response.data:
                for tweet in response.data:
                    mentions.append({
                        "id": str(tweet.id),
                        "text": tweet.text,
                        "author_id": str(tweet.author_id) if hasattr(tweet, "author_id") else None,
                        "created_at": tweet.created_at.isoformat() if hasattr(tweet, "created_at") and tweet.created_at else None
                    })
            return mentions
        except Exception as e:
            logger.error(f"Twitter API メンション取得エラー: {e}")
            return []

    def test_connection(self) -> bool:
        """認証情報の有効性をテストする (自分のユーザー情報を取得してみる)"""
        if not self.client:
            return False

        try:
            # me() に相当する v2 get_me()
            response = self.client.get_me()
            if response and response.data:
                user = response.data
                logger.info(f"API接続テスト成功: @{user.get('username')}")
                return True
            return False
        except Exception as e:
            logger.error(f"API接続テスト失敗: {e}")
            return False

    def get_tweet_thread(self, tweet_id: str, max_depth: int = 3) -> List[Dict[str, Any]]:
        """
        特定のツイートIDから遡るように親ツイートを再帰的に取得し、
        時系列順（会話の発端が先頭、対象ツイートが末尾）になるようリストを返す。
        """
        if not self.client:
            return []
            
        thread = []
        current_id = tweet_id
        depth = 0
        
        try:
            while current_id and depth <= max_depth:
                response = self.client.get_tweet(
                    id=current_id,
                    expansions=["referenced_tweets.id", "author_id"],
                    tweet_fields=["created_at"]
                )
                
                if not response or not response.data:
                    break
                    
                tweet = response.data
                author_username = "Unknown"
                
                if response.includes and "users" in response.includes:
                    for user in response.includes["users"]:
                        if str(user.id) == str(tweet.author_id):
                            author_username = f"@{user.username}"
                            break
                
                tweet_data = {
                    "id": str(tweet.id),
                    "text": tweet.text,
                    "author": author_username,
                    "url": f"https://x.com/{author_username[1:] if author_username.startswith('@') else 'twitter'}/status/{tweet.id}"
                }
                
                thread.insert(0, tweet_data)  # 先頭に挿入することで自然に時系列順になる
                
                parent_id = None
                if tweet.referenced_tweets:
                    for ref in tweet.referenced_tweets:
                        if ref.type == "replied_to":
                            parent_id = str(ref.id)
                            break
                            
                if parent_id:
                    current_id = parent_id
                    depth += 1
                else:
                    break
                    
            return thread
            
        except Exception as e:
            logger.error(f"Twitter API スレッド取得エラー: {e}")
            return thread
