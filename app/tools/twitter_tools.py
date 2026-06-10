# tools/twitter_tools.py
from typing import Optional, Dict, Any, Union, List
import logging
from langchain_core.tools import tool

from twitter_manager import twitter_manager
import config_manager
import utils
import room_manager

logger = logging.getLogger(__name__)

@tool
def draft_tweet(content: str, motivation: str = "", room_name: str = "", reply_to_url: Optional[str] = None, reply_to_id: Optional[str] = None, reply_to_list_index: Optional[int] = None, media_paths: Optional[List[str]] = None) -> str:
    """
    Twitter (X) への投稿内容を下書きとして作成し、ユーザーの承認キューに追加します。
    ペルソナが自身の考えや近況を世界に向けて発信したい場合、または特定ツイートへの返信（リプライ）を行いたい場合に使用します。
    画像生成ツール（generate_image）と組み合わせて、画像付きツイートを行うことも可能です。
    
    重要：このツールを呼び出しただけでは実際には投稿されません。ユーザーがUIで承認する必要があります。
    
    Args:
        content: 投稿したい内容（テキスト）。自動的にプライバシーフィルタが適用されます。
        motivation: この投稿をしたい理由や動機（任意。一言で）。活動記録に保存されます。
        room_name: (システムで自動入力)
        reply_to_url: (非推奨・使用禁止) システムが自動生成するため、このフィールドには何も指定しないでください。
        reply_to_id: (非推奨・使用禁止)
        reply_to_list_index: 返信先にするツイートのリスト番号（任意）。直近に「メンション」や「通知」等で取得したリストにある1から始まる番号（1, 2, 3...）のみを指定してください。システムが自動で宛先URLを復元・付加します。
        media_paths: 添付する画像ファイルのパスのリスト（任意。最大4枚）。`generate_image` ツールで生成した画像のパスを指定できます。
    
    Returns:
        処理結果のメッセージ。
    """
    try:
        # リプライ用の @ユーザー名 が本文先頭にある場合は自動で取り除く（TwitterUIが自動補完するため不要）
        import re
        content = re.sub(r'^@[a-zA-Z0-9_]+\s*', '', content).strip()
        
        # 下書き追加前に、文字数やフィルター結果を確認する
        res = twitter_manager.apply_privacy_filter(content, room_name=room_name)
        limit = twitter_manager.get_twitter_post_limit(room_name)
        
        if res.get("twitter_length", 0) > limit:
            return f"❌ エラー: 文字数制限超過 (Twitter換算 {res['twitter_length']}/{limit}文字)。\n短く要約するか、不要な情報を削ってから再実行してください。"

        # 枚数制限チェック
        if media_paths and len(media_paths) > 4:
            return "❌ エラー: 画像は最大4枚までしか添付できません。"

        # リストインデックスを利用して参照元を解決
        if reply_to_list_index is not None:
            if 0 < reply_to_list_index <= len(twitter_manager.last_fetched_tweets):
                target_tweet = twitter_manager.last_fetched_tweets[reply_to_list_index - 1]
                reply_to_url = target_tweet.get("url")
                reply_to_id = str(target_tweet.get("id"))
            else:
                max_len = len(twitter_manager.last_fetched_tweets)
                if max_len == 0:
                    return "❌ エラー: キャッシュされたツイートリストが空です。先に `check_twitter_updates` でリストを取得してください。"
                return f"❌ エラー: 指定されたリスト番号 ({reply_to_list_index}) は無効です。直近に取得したリストの範囲（1〜{max_len}）内で指定してください。"

        # 下書き追加
        draft_id = twitter_manager.add_draft(content, room_name, reply_to_url=reply_to_url, reply_to_id=reply_to_id, media_paths=media_paths)
        
        # --- Twitter活動記録 (External Codex) ---
        try:
            import twitter_activity_logger
            reply_to_info = None
            if reply_to_url or reply_to_id:
                # log_post では url 形式のメタデータを使用
                _url = reply_to_url if reply_to_url else f"https://x.com/i/status/{reply_to_id}"
                reply_to_info = {"url": _url, "author": "", "text": ""}
            twitter_activity_logger.log_post(
                room_name=room_name,
                content=content,
                motivation=motivation,
                reply_to=reply_to_info,
                status="pending",
                draft_id=draft_id,
                media_paths=media_paths # 拡張
            )
        except Exception as log_err:
            logger.warning(f"Twitter活動ログの記録に失敗（投稿処理自体は継続）: {log_err}")
        
        # --- 自動投稿チェック ---
        if twitter_manager.is_auto_post_enabled(room_name):
            # 承認をスキップして即座に投稿
            twitter_manager.approve_tweet(draft_id)
            result = twitter_manager.execute_post(draft_id, room_name)
            if result.get("success"):
                url = result.get("url", "https://x.com/home")
                # 活動ログのステータス更新
                try:
                    import twitter_activity_logger
                    twitter_activity_logger.update_post_status(room_name, draft_id, "posted", url)
                except Exception:
                    pass
                message = f"✅ 自動投稿が完了しました！\nURL: {url}"
                if reply_to_url:
                    message += f"\n🔗 返信先: {reply_to_url}"
                return message
            else:
                error = result.get("error", "不明なエラー")
                # 活動ログのステータス更新
                try:
                    import twitter_activity_logger
                    twitter_activity_logger.update_post_status(room_name, draft_id, "failed")
                except Exception:
                    pass
                return f"❌ 自動投稿に失敗しました: {error}\n下書きは承認キューに差し戻されています。"
        
        # --- 通常フロー（承認待ち） ---
        message = f"Twitter下書き (ID: {draft_id}) を作成し、承認キューに追加しました。"
        if media_paths:
            message += f"（画像 {len(media_paths)} 枚添付）"
            
        if reply_to_url:
            message += f"\n🔗 返信先: {reply_to_url}"
            
        if res["is_modified"]:
            message += f"\n\n🚨 プライバシー保護のため、一部の文言を自動置換しました：\n「{res['filtered']}」"
        
        if res["warnings"]:
            message += "\n\n⚠️ 警告：\n" + "\n".join([f"・{w}" for w in res["warnings"]])
        
        # 承認要請の通知
        if twitter_manager.should_notify_on_approval(room_name):
            # 通知禁止時間帯のチェック
            effective_settings = config_manager.get_effective_settings(room_name)
            auto_settings = effective_settings.get("autonomous_settings", {})
            quiet_start = auto_settings.get("quiet_hours_start", "00:00")
            quiet_end = auto_settings.get("quiet_hours_end", "07:00")
            
            preview = content[:50] + ("..." if len(content) > 50 else "")
            
            if utils.is_in_quiet_hours(quiet_start, quiet_end):
                # 通知禁止時間帯でもログには残す
                log_f, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
                if log_f:
                    utils.save_message_to_log(log_f, "## SYSTEM:notification_blocked", f"📱 **Twitter承認要請通知（送信されず）**\n\n「{preview}」")
                message += "\n\n📱 通知禁止時間帯のため、スマホへの通知は抑制されました（ログには記録されました）。"
            else:
                try:
                    import alarm_manager
                    notification_result = alarm_manager.send_notification(
                        room_name,
                        f"📝 新しいTwitter下書きが承認待ちです:\n「{preview}」",
                        {},
                        notification_kind="notification",
                    )
                    if isinstance(notification_result, dict) and notification_result.get("success"):
                        message += "\n\n📱 承認要請の通知をスマホに送信しました。"
                    else:
                        reason = "通知送信結果を確認できませんでした。"
                        if isinstance(notification_result, dict):
                            reason_parts = []
                            if notification_result.get("message"):
                                reason_parts.append(str(notification_result["message"]))
                            if notification_result.get("status_code") is not None:
                                reason_parts.append(f"HTTP {notification_result['status_code']}")
                            if notification_result.get("request_id"):
                                reason_parts.append(f"request={notification_result['request_id']}")
                            errors = notification_result.get("errors") or []
                            if errors:
                                reason_parts.append(" / ".join(str(error) for error in errors))
                            reason = " / ".join(reason_parts) if reason_parts else "通知送信に失敗しました。"
                        message += f"\n\n📱 承認要請の通知送信に失敗しました: {reason}"
                except Exception as notify_err:
                    logger.warning(f"承認要請通知の送信に失敗: {notify_err}")
                    message += f"\n\n📱 承認要請の通知送信に失敗しました: {notify_err}"
            
        return message
        
    except Exception as e:
        logger.error(f"Error in draft_tweet: {e}")
        return f"エラー: 下書きの作成に失敗しました - {str(e)}"

@tool
def check_twitter_updates(target: str = "all", count: int = 10, room_name: str = "") -> str:
    """
    Twitter (X) から最新の情報を取得します。前回取得時から新しい情報がない場合は「情報なし」と返します。
    
    Args:
        target: 取得する対象。"all" (通知とタイムライン両方), "mentions" (自身宛のリプライ), "timeline" (ホームタイムライン), "notifications" (全通知) のいずれか。
        count: それぞれの取得件数 (最大10)
        room_name: (システムで自動入力)
        
    Returns:
        取得結果のテキスト。新しい情報がない場合はその旨を通知します。
    """
    valid_targets = ["all", "mentions", "timeline", "notifications"]
    if target not in valid_targets:
        target = "all"
        
    count = min(count, 10)
    output = []
    
    # 処理対象の決定
    fetch_funcs = []
    if target in ["all", "timeline"]:
        fetch_funcs.append(("timeline", twitter_manager.fetch_timeline, "ホームタイムライン"))
    if target == "mentions":
        # 旧 get_twitter_mentions と同じく、同期漏れを避けるため通知取得を使う。
        fetch_funcs.append(("mentions", twitter_manager.fetch_notifications, "メンション"))
    if target in ["all", "notifications"]:
        fetch_funcs.append(("notifications", twitter_manager.fetch_notifications, "通知"))
        
    displayed_tweets = []
    # それぞれ取得してフィルタ
    for feed_type, func, label in fetch_funcs:
        try:
            tweets = func(room_name, count=count)
            if tweets:
                # コンテキスト解決 (通知・メンションのみ)
                if feed_type in ["mentions", "notifications"]:
                    tweets = twitter_manager.resolve_thread_context(tweets, room_name)
                    
                new_tweets = twitter_manager.filter_new_tweets(room_name, feed_type, tweets)
                
                # 活動記録
                if new_tweets:
                    try:
                        import twitter_activity_logger
                        twitter_activity_logger.log_notification_check(room_name, new_tweets, check_type=feed_type)
                    except Exception as log_err:
                        pass
                
                if new_tweets:
                    output.append(f"【最新の{label}】")
                    replied_urls = twitter_manager.get_replied_urls()
                    for t in new_tweets:
                        displayed_tweets.append(t)
                        author = t.get("author", "Unknown USER")
                        text = t.get("text", "").replace("\n", " ")
                        url = t.get("url", "")
                        replied_mark = " （✅ 返信済み）" if url in replied_urls else ""
                        output.append(f"{len(displayed_tweets)}. [{author}]: {text}{replied_mark}")
                    output.append("") # 空行
        except Exception as e:
            logger.error(f"Error fetching {feed_type}: {e}")
            output.append(f"エラー: {label}の取得中に問題が発生しました - {str(e)}\n")

    twitter_manager.last_fetched_tweets = displayed_tweets

    if not displayed_tweets:
        return "現在、新しい情報はありません（前回取得時から更新なし）。"
        
    return "\n".join(output).strip()

@tool
def post_tweet(draft_id: str, room_name: str = "") -> str:
    """
    ユーザーによって既に承認された指定IDの下書きを、実際にTwitter (X) へ投稿します。
    （注：このツールは通常、ユーザーの承認後にシステム内部から自動実行されるか、
    ペルソナが承認済みであることを確認して明示的に実行するために使われます）
    
    Args:
        draft_id: 承認済みの下書きID
        room_name: (システムで自動入力)
        
    Returns:
        投稿結果。
    """
    try:
        # Phase 2: 実際の投稿実行
        result = twitter_manager.execute_post(draft_id)
        
        if result["success"]:
            return (
                f"✅ Twitterへの投稿に成功しました！\n"
                f"URL: {result.get('url', 'https://x.com/home')}"
            )
        else:
            return f"❌ 投稿に失敗しました: {result.get('error', '不明なエラー')}"
        
    except Exception as e:
        logger.error(f"Error in post_tweet: {e}")
        return f"エラー: 投稿処理中に問題が発生しました - {str(e)}"
