"""ui_handlers のうち「Twitter (X) 連携」ドメイン。

ui_handlers パッケージから再エクスポートされ、呼び出し側は従来どおり
ui_handlers.<関数名> でアクセスできる。
"""

from typing import Optional, Tuple, List, Dict, Union, Any
from pathlib import Path
import filetype
import gradio as gr
import hashlib
import os
import pandas as pd
import shutil
import tempfile

from ._common import _is_blank, _normalize_file_paths


TWITTER_DRAFT_PREVIEW_CACHE_DIR = os.path.join(tempfile.gettempdir(), "nexus_ark_twitter_draft_previews")


def handle_save_twitter_settings(room_name, enabled, auth_mode, api_key, api_secret, access_token, access_token_secret, posting_summary, posting_guidelines, auto_post, notify_on_approval_request, is_premium, enable_privacy_filter, fetch_thread_enabled, thread_fetch_count):
    """Twitter連携設定を保存する"""
    print(f"DEBUG: Save Twitter settings for {room_name}")
    print(f"DEBUG: auth_mode={auth_mode}, enabled={enabled}, auto_post={auto_post}")
    print(f"DEBUG: api_key={'exists' if api_key else 'None/Empty'}")

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return

    import room_manager
    room_config = room_manager.get_room_config(room_name) or {}
    overrides = room_config.get("override_settings", {}) if isinstance(room_config, dict) else {}
    existing_twitter_settings = overrides.get("twitter_settings", {}) or {}
    existing_api_config = existing_twitter_settings.get("api_config", {}) or {}

    if _twitter_save_looks_like_unloaded_defaults(
        existing_twitter_settings,
        enabled,
        auth_mode,
        api_key,
        api_secret,
        access_token,
        access_token_secret,
        posting_summary,
        posting_guidelines,
        auto_post,
        notify_on_approval_request,
        is_premium,
        enable_privacy_filter,
        fetch_thread_enabled,
        thread_fetch_count,
    ):
        gr.Warning("Twitter設定がまだUIへ読み込まれていないため、空の値では保存しませんでした。Twitterタブを開き直してから保存してください。")
        return

    api_config = {
        "api_key": api_key if not _is_blank(api_key) else existing_api_config.get("api_key", ""),
        "api_secret": api_secret if not _is_blank(api_secret) else existing_api_config.get("api_secret", ""),
        "access_token": access_token if not _is_blank(access_token) else existing_api_config.get("access_token", ""),
        "access_token_secret": access_token_secret if not _is_blank(access_token_secret) else existing_api_config.get("access_token_secret", ""),
    }

    settings = {
        "twitter_settings": {
            "enabled": bool(enabled),
            "use_api": (auth_mode == "api"),
            "auth_mode": auth_mode,
            "posting_summary": posting_summary or "",
            "posting_guidelines": posting_guidelines or "",
            "auto_post": bool(auto_post),
            "notify_on_approval_request": bool(notify_on_approval_request),
            "is_premium": bool(is_premium),
            "enable_privacy_filter": bool(enable_privacy_filter),
            "fetch_thread_enabled": bool(fetch_thread_enabled),
            "thread_fetch_count": int(thread_fetch_count),
            "api_config": api_config
        }
    }

    result = room_manager.update_room_config(room_name, settings)
    if result == True:
        gr.Info("Twitter連携設定を保存しました。")
    elif result == "no_change":
        gr.Info("設定に変更はありません。")
    else:
        gr.Error("設定の保存中にエラーが発生しました。")


def handle_twitter_auth_mode_change(mode):
    """認証方式の切り替えに合わせてUIの表示を切り替える"""
    return gr.update(visible=(mode == "browser")), gr.update(visible=(mode == "api"))


def handle_test_twitter_api(api_key, api_secret, access_token, access_token_secret):
    """Twitter APIの接続テストを実行する"""
    if not all([api_key, api_secret, access_token, access_token_secret]):
        return "⚠️ **エラー**: 全てのAPIキーを入力してください。"

    from twitter_api import TwitterAPI
    api = TwitterAPI(api_key, api_secret, access_token, access_token_secret)

    # tweepy がない場合はエラーメッセージを返す
    import logging
    logger = logging.getLogger("twitter_api")
    if not hasattr(api, "client") or api.client is None:
        return "❌ **失敗**: クライアントの初期化に失敗しました。`tweepy` がインストールされているか確認してください。"

    success = api.test_connection()
    if success:
        return "✅ **成功**: API接続テストに合格しました！"
    else:
        return "❌ **失敗**: 認証エラーが発生しました。キーが正しいか、および App Permissions が 'Read and Write' になっているか確認してください。"


def handle_load_twitter_settings(room_name):
    """ルーム設定からTwitterの認証情報を読み込み、UIに反映させる"""
    if not room_name:
        return [gr.update()] * 16

    import room_manager
    room_config = room_manager.get_room_config(room_name) or {}
    # 設定は override_settings 内に保存されるため、そこから取得する
    overrides = room_config.get("override_settings", {})
    twitter_settings = overrides.get("twitter_settings", {})

    enabled = twitter_settings.get("enabled", False)
    auth_mode = twitter_settings.get("auth_mode", "browser")
    posting_summary = twitter_settings.get("posting_summary", "")
    posting_guidelines = twitter_settings.get("posting_guidelines", "")
    auto_post = twitter_settings.get("auto_post", False)
    notify_on_approval_request = twitter_settings.get("notify_on_approval_request", False)
    is_premium = twitter_settings.get("is_premium", False)
    enable_privacy_filter = twitter_settings.get("enable_privacy_filter", True)
    fetch_thread_enabled = twitter_settings.get("fetch_thread_enabled", False)
    thread_fetch_count = twitter_settings.get("thread_fetch_count", 3)
    api_config = twitter_settings.get("api_config", {})

    return [
        gr.update(value=enabled),
        gr.update(value=auth_mode),
        gr.update(value=posting_summary),
        gr.update(value=posting_guidelines),
        gr.update(value=auto_post),
        gr.update(value=notify_on_approval_request),
        gr.update(value=api_config.get("api_key", ""), type="password"),
        gr.update(value=api_config.get("api_secret", ""), type="password"),
        gr.update(value=api_config.get("access_token", ""), type="password"),
        gr.update(value=api_config.get("access_token_secret", ""), type="password"),
        gr.update(visible=(auth_mode == "browser")), # ブラウザグループの可視性
        gr.update(visible=(auth_mode == "api")),     # APIグループの可視性
        gr.update(value=is_premium),
        gr.update(value=enable_privacy_filter),
        gr.update(value=fetch_thread_enabled),
        gr.update(value=thread_fetch_count)
    ]


def _build_twitter_media_gallery_value(file_values) -> List[str]:
    """
    添付プレビュー用の画像パスを生成する。

    Gradio/ブラウザは同じローカルパスの画像をキャッシュしやすいため、
    mtime/size を含むファイル名で一時コピーを作り、差し替え後のプレビューを確実に更新する。
    """
    preview_paths = []
    source_paths = _normalize_file_paths(file_values)
    if not source_paths:
        return preview_paths

    os.makedirs(TWITTER_DRAFT_PREVIEW_CACHE_DIR, exist_ok=True)

    for source_path in source_paths:
        if not source_path or not os.path.exists(source_path):
            continue

        try:
            kind = filetype.guess(source_path)
            if not (kind and kind.mime.startswith("image/")):
                continue

            stat = os.stat(source_path)
            source_abs = os.path.abspath(source_path)
            digest_src = f"{source_abs}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")
            digest = hashlib.sha256(digest_src).hexdigest()[:16]
            ext = Path(source_path).suffix or f".{kind.extension}"
            preview_path = os.path.join(TWITTER_DRAFT_PREVIEW_CACHE_DIR, f"{Path(source_path).stem}_{digest}{ext}")

            if not os.path.exists(preview_path):
                shutil.copy2(source_path, preview_path)

            preview_paths.append(preview_path)
        except Exception as e:
            print(f"--- [Twitter Draft Preview] 添付画像プレビュー生成エラー ({source_path}): {e} ---")

    return preview_paths


def handle_refresh_twitter_pending(room_name: str = None):
    """承認待ちキューの表示を更新する"""
    from twitter_manager import twitter_manager
    twitter_manager.reload()
    pending = twitter_manager.get_pending_list()
    if room_name:
        pending = [d for d in pending if d.get("room_name") == room_name]
    pending = sorted(pending, key=lambda d: str(d.get("timestamp", "")), reverse=True)

    if not pending:
        return gr.update(value=pd.DataFrame(columns=["ID", "時刻", "画像", "下書き内容", "警告"]))

    data = []
    for d in pending:
        media_count = len(d.get("media_paths", []))
        media_str = f"🖼️x{media_count}" if media_count > 0 else "-"
        data.append([
            d["id"],
            str(d.get("timestamp", "")).replace("T", " ")[:16],
            media_str,
            d["filtered_content"],
            ", ".join(d["warnings"]) if d.get("warnings") else "-"
        ])

    return gr.update(value=pd.DataFrame(data, columns=["ID", "時刻", "画像", "下書き内容", "警告"]))


def handle_load_selected_twitter_draft(evt: gr.SelectData, df: pd.DataFrame):
    """選択された行の下書きをエディタに読み込む"""
    if not hasattr(evt, 'index') or evt.index is None or df is None or df.empty:
        return "", "", "", "※ 選択されていません", "", "", [], []

    from twitter_manager import twitter_manager
    # どの列が選択されても、その行の「ID」列からIDを取得する
    row_idx = evt.index[0]
    try:
        draft_id = str(df.iloc[row_idx]["ID"])
    except (IndexError, KeyError):
        return "", "", "", "※ 読み込めませんでした", "", "", [], []

    return handle_load_twitter_draft_by_id(draft_id)


def handle_load_twitter_draft_by_id(draft_id: str):
    """(共通) 指定されたIDの下書きを読み込む"""
    if not draft_id:
        return "", "", "", "※ 選択されていません", "", "", [], []

    from twitter_manager import twitter_manager
    pending = twitter_manager.get_pending_list()
    draft = next((d for d in pending if d["id"] == draft_id), None)

    if draft:
        warnings_text = ""
        if draft.get("warnings"):
            warnings_text = "⚠️ **警告:** " + ", ".join(draft["warnings"])

        reply_url = draft.get("reply_to_url", "")
        reply_id = draft.get("reply_to_id", "")
        reply_preview = f"🔗 返信先: {reply_url}" if reply_url else "（新規投稿）"

        media_paths = draft.get("media_paths", [])
        
        # --- ファイル存在チェック ---
        valid_media_paths = []
        missing_count = 0
        for p in media_paths:
            if p and os.path.exists(p):
                valid_media_paths.append(p)
            else:
                missing_count += 1
        
        if missing_count > 0:
            gr.Warning(f"添付画像の一部（{missing_count}件）が見つかりません。削除された可能性があります。")
        # ---------------------------

        return (
            draft["id"],
            draft["filtered_content"],
            warnings_text,
            reply_preview,
            reply_url,
            reply_id,
            valid_media_paths,
            _build_twitter_media_gallery_value(valid_media_paths),
        )

    return "", "", "", "※ 読み込めませんでした", "", "", [], []


def handle_load_twitter_draft_by_button(draft_id: str):
    """(ボタン用) 指定されたIDの下書きを読み込む"""
    if not draft_id:
        gr.Warning("下書きが選択されていません。リストから選択してください。")
        return "", "", "", "※ 選択されていません", "", "", [], []
    return handle_load_twitter_draft_by_id(draft_id)


def handle_approve_twitter_tweet(draft_id: str, edited_content: str, edited_reply_url: str, edited_media_paths: List[str]):
    """下書きを承認してTwitterへ投稿する"""
    if not draft_id:
        gr.Warning("操作対象の下書きが選択されていません。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    from twitter_manager import twitter_manager

    pending = twitter_manager.get_pending_list()
    draft = next((d for d in pending if d["id"] == draft_id), None)
    if not draft:
        gr.Warning("対象の下書きが見つかりません。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    room_name = draft.get("room_name")
    limit = twitter_manager.get_twitter_post_limit(room_name)

    # 承認前に文字数チェックを厳密に行う (警告のみでなくブロック)
    tw_length = twitter_manager.calculate_twitter_length(edited_content)
    if tw_length > limit:
        gr.Warning(f"文字数制限超過 ({tw_length}/{limit}文字)。短縮してから承認してください。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    success = twitter_manager.approve_tweet(
        draft_id,
        edited_content,
        edited_reply_url,
        _normalize_file_paths(edited_media_paths),
    )

    if success:
        gr.Info("下書きを承認しました。投稿プロセスを開始します...")

        # 即座に投稿実行
        post_result = twitter_manager.execute_post(draft_id)

        # 詳細テキストを生成
        detail_text = f"【内容】\n{edited_content}\n\n【ステータス】: "
        if post_result["success"]:
            detail_text += "posted ✅"
            if post_result.get("url"):
                detail_text += f"\n\n🔗 URL: {post_result['url']}"
            gr.Info(f"✅ Twitterへの投稿に成功しました！ ({post_result.get('method', 'unknown')} mode)")
        else:
            err_msg = post_result.get("error", "不明なエラー")
            detail_text += f"failed ❌\n\n🚨 【エラー内容】:\n{err_msg}"
            gr.Error(f"🚨 承認はされましたが、投稿に失敗しました: {err_msg}")

            # --- 失敗時は自動で下書きに差し戻す ---
            twitter_manager.move_back_to_drafts(draft_id)
            gr.Info("投稿に失敗したため、キュー（下書き）に自動で差し戻しました。")

        # 更新後の表示を返す
        pending_df = handle_refresh_twitter_pending()
        history_df = handle_refresh_twitter_history()
        return pending_df, history_df, "", "", detail_text, [], [] # 戻り値: pending, history, id_state, editor, detail, media_file, media_gallery

    gr.Error("承認に失敗しました。")
    return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()


def handle_twitter_media_file_change(file_values):
    """添付画像欄の差し替えに合わせてプレビューだけを更新する。"""
    return _build_twitter_media_gallery_value(file_values)


def handle_reject_twitter_tweet(draft_id: str):
    """下書きを却下（削除）する"""
    if not draft_id:
        gr.Warning("操作対象の下書きが選択されていません。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    from twitter_manager import twitter_manager
    twitter_manager.reject_tweet(draft_id)
    gr.Info("下書きを削除しました。")

    pending_df = handle_refresh_twitter_pending()
    history_df = handle_refresh_twitter_history()
    return pending_df, history_df, "", "", "下書きを削除しました。", [], []


def handle_manual_twitter_draft(content: str, room_name: str, reply_to_url: Optional[str] = None, reply_to_id: Optional[str] = None, media_paths: Optional[List[str]] = None):
    """手動で下書きを追加する。リプライ先がある場合はそれも保持する。"""
    if not content or not content.strip():
        # 失敗時は現状維持
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    from twitter_manager import twitter_manager
    draft_id = twitter_manager.add_draft(content, room_name, author_type="user", reply_to_url=reply_to_url, reply_to_id=reply_to_id, media_paths=media_paths)

    gr.Info(f"下書き (ID: {draft_id}) をキューに追加しました。")

    # キューを更新して、エディタとリプライ情報を空にする
    pending_df = handle_refresh_twitter_pending()
    history_df = handle_refresh_twitter_history()
    return pending_df, history_df, "", "", "", [], [] # 戻り値: pending, history, editor, reply_url_state, reply_id_state, media_file, media_gallery


def handle_refresh_twitter_timeline(room_name: str):
    """タイムラインの表示を更新する"""
    if not room_name:
        return pd.DataFrame(columns=["時刻", "投稿者", "内容", "URL"])

    from twitter_manager import twitter_manager
    try:
        timeline = twitter_manager.fetch_timeline(room_name, count=20)
        if not timeline:
            gr.Warning("タイムラインが空か、取得に失敗しました。")
            return pd.DataFrame(columns=["時刻", "投稿者", "内容", "URL"])

        data = []
        for t in timeline:
            data.append([
                t.get("created_at", "").replace("T", " ")[:16] if t.get("created_at") else "--:--",
                t.get("author", "Unknown"),
                t.get("text", ""),
                t.get("url", "")
            ])
        return pd.DataFrame(data, columns=["時刻", "投稿者", "内容", "URL"])
    except Exception as e:
        gr.Error(f"タイムライン取得中にエラー: {e}")
        return pd.DataFrame(columns=["時刻", "投稿者", "内容", "URL"])


def handle_refresh_twitter_mentions(room_name: str):
    """メンションの表示を更新する"""
    if not room_name:
        return pd.DataFrame(columns=["時刻", "投稿者", "内容", "URL"])

    from twitter_manager import twitter_manager
    try:
        mentions = twitter_manager.fetch_mentions(room_name, count=20)
        if not mentions:
            gr.Info("新しいメンションはありません。")
            return pd.DataFrame(columns=["時刻", "投稿者", "内容", "URL"])

        data = []
        for t in mentions:
            data.append([
                t.get("created_at", "").replace("T", " ")[:16] if t.get("created_at") else "--:--",
                t.get("author", "Unknown"),
                t.get("text", ""),
                t.get("url", "")
            ])
        return pd.DataFrame(data, columns=["時刻", "投稿者", "内容", "URL"])
    except Exception as e:
        gr.Error(f"メンション取得中にエラー: {e}")
        return pd.DataFrame(columns=["時刻", "投稿者", "内容", "URL"])


def handle_refresh_twitter_notifications(room_name: str):
    """通知一覧（引用RT含む）の表示を更新する"""
    if not room_name:
        return pd.DataFrame(columns=["時刻", "投稿者", "内容", "URL"])

    from twitter_manager import twitter_manager
    try:
        notifications = twitter_manager.fetch_notifications(room_name, count=20)
        if not notifications:
            gr.Info("新しい通知はありません。")
            return pd.DataFrame(columns=["時刻", "投稿者", "内容", "URL"])

        data = []
        for n in notifications:
            data.append([
                n.get("created_at", "").replace("T", " ")[:16] if n.get("created_at") else "--:--",
                n.get("author", "Unknown"),
                n.get("text", ""),
                n.get("url", "")
            ])
        return pd.DataFrame(data, columns=["時刻", "投稿者", "内容", "URL"])
    except Exception as e:
        gr.Error(f"通知取得中にエラー: {e}")
        return pd.DataFrame(columns=["時刻", "投稿者", "内容", "URL"])


def handle_refresh_twitter_feed(room_name: str, feed_type: str):
    """統合フィードハンドラ: feed_typeに応じてタイムライン/メンション/通知を取得"""
    from twitter_manager import twitter_manager

    if feed_type == "メンション":
        df = handle_refresh_twitter_mentions(room_name)
        # メンションはペルソナ通知用に蓄積
        try:
            mentions = twitter_manager.fetch_mentions(room_name, count=20)
            if mentions:
                twitter_manager.set_pending_feed("メンション", mentions)
        except Exception:
            pass
        return df
    elif feed_type == "通知":
        df = handle_refresh_twitter_notifications(room_name)
        # 通知もペルソナ通知用に蓄積
        try:
            notifications = twitter_manager.fetch_notifications(room_name, count=20)
            if notifications:
                twitter_manager.set_pending_feed("通知", notifications)
        except Exception:
            pass
        return df
    else:
        # デフォルト: タイムライン（ペルソナ通知には蓄積しない）
        return handle_refresh_twitter_timeline(room_name)


def handle_twitter_reply_click(ev: gr.SelectData, df: pd.DataFrame, current_draft: str = ""):
    """タイムライン/メンションで返信ボタンが押されたとき(?)の処理
    GradioのDataframe.select を想定。
    """
    row_idx = ev.index[0]
    tweet_text = df.iloc[row_idx]["内容"]
    tweet_author = df.iloc[row_idx]["投稿者"]
    tweet_url = df.iloc[row_idx]["URL"]

    # 投稿IDをURLから抽出
    tweet_id = tweet_url.split("/")[-1] if "/status/" in tweet_url else ""

    # UIへのフィードバック
    display_info = f"↪️ 返信先: {tweet_author}\n「{tweet_text[:30]}...」"

    # エディタは空にするか、@ユーザー名を入れる
    # author が "@user (Name)" 形式の場合は "@user " を抽出
    prefix = ""
    if "@" in tweet_author:
        import re
        m = re.search(r'(@\w+)', tweet_author)
        if m:
            prefix = m.group(1) + " "

    # 既存のドラフト本文があればそれを維持し、空の場合のみテンプレを挿入
    returned_draft = current_draft if current_draft.strip() else prefix

    # 返回値: reply_preview, editor, reply_url_state, reply_id_state, tab_switch(投稿タブへ)
    return display_info, returned_draft, tweet_url, tweet_id, gr.Tabs(selected="twitter_post_subtab")


def handle_refresh_twitter_history():
    """投稿履歴の表示を更新する"""
    from twitter_manager import twitter_manager
    twitter_manager.reload()
    history = twitter_manager.get_history_list()

    if not history:
        return gr.update(value=pd.DataFrame(columns=["ID", "時刻", "内容", "ステータス", "URL"]))

    data = []
    for h in history:
        status = h.get("status", "unknown")
        # 失敗時はステータスにエラー内容を付加（短く）
        if status == "failed":
            err = h.get("error", "")
            if err:
                status = f"❌ failed ({err[:15]}...)"
            else:
                status = "❌ failed"
        elif status == "posted":
            status = "✅ posted"

        data.append([
            h["id"],
            h["timestamp"].replace("T", " ")[:16],
            h.get("final_content", h.get("filtered_content", "")),
            status,
            h.get("post_url", "-")
        ])

    return gr.update(value=pd.DataFrame(data, columns=["ID", "時刻", "内容", "ステータス", "URL"]))


def handle_twitter_history_select(evt: gr.SelectData, df: pd.DataFrame):
    """(履歴用) 選択された行の詳細情報を取得してStateに保存し、詳細ビューに返す"""
    if not hasattr(evt, 'index') or evt.index is None or df is None or df.empty:
        return "", ""

    row_idx = evt.index[0]
    try:
        draft_id = str(df.iloc[row_idx]["ID"])
    except (IndexError, KeyError):
        return "", ""

    from twitter_manager import twitter_manager
    history = twitter_manager.get_history_list()
    item = next((h for h in history if h["id"] == draft_id), None)

    detail_text = ""
    if item:
        content = item.get("final_content", item.get("filtered_content", ""))
        status = item.get("status", "unknown")

        detail_text = f"【内容】\n{content}\n\n【ステータス】: {status}"
        if item.get("posted_at"):
            detail_text += f"\n【投稿日時】: {item['posted_at'].replace('T', ' ')[:19]}"
        if item.get("error"):
            detail_text += f"\n\n🚨 【エラー内容】:\n{item['error']}"
        if item.get("post_url") and item["post_url"] != "-":
            detail_text += f"\n\n🔗 URL: {item['post_url']}"

    return draft_id, detail_text


def handle_delete_twitter_history(draft_id: str):
    """選択された履歴を削除する"""
    if not draft_id:
        gr.Warning("削除対象の履歴が選択されていません。リストから選択してください。")
        return gr.update()

    from twitter_manager import twitter_manager
    twitter_manager.delete_history_item(draft_id)
    gr.Info("選択した履歴を削除しました。")

    return handle_refresh_twitter_history()


def handle_twitter_history_retry(draft_id: str):
    """選択された履歴を下書きに差し戻す"""
    if not draft_id:
        gr.Warning("差し戻す履歴が選択されていません。リストから選択してください。")
        return gr.update(), gr.update(), gr.update(), gr.update()

    from twitter_manager import twitter_manager
    success = twitter_manager.move_back_to_drafts(draft_id)

    if success:
        gr.Info("履歴を下書きに戻しました。「承認待ち」タブで確認してください。")
    else:
        gr.Error("下書きへの差し戻しに失敗しました。対象が存在しない可能性があります。")

    pending_df = handle_refresh_twitter_pending()
    history_df = handle_refresh_twitter_history()

    return pending_df, history_df, "", gr.update(selected="twitter_post_subtab")


def handle_twitter_history_retry_lite(draft_id: str):
    """軽量Twitter承認UI向けに履歴を下書きへ戻す。"""
    pending_df, history_df, detail_text, _ = handle_twitter_history_retry(draft_id)
    return pending_df, history_df, detail_text


def _twitter_save_looks_like_unloaded_defaults(
    existing: dict,
    enabled,
    auth_mode,
    api_key,
    api_secret,
    access_token,
    access_token_secret,
    posting_summary,
    posting_guidelines,
    auto_post,
    notify_on_approval_request,
    is_premium,
    enable_privacy_filter,
    fetch_thread_enabled,
    thread_fetch_count,
) -> bool:
    """Fast init直後の空UI値で保存済みTwitter設定を潰す事故を検出する。"""
    if not existing:
        return False

    has_existing_user_values = any([
        not _is_blank(existing.get("posting_summary")),
        not _is_blank(existing.get("posting_guidelines")),
        bool(existing.get("enabled")),
        bool(existing.get("auto_post")),
        bool(existing.get("notify_on_approval_request")),
        bool(existing.get("is_premium")),
        bool(existing.get("fetch_thread_enabled")),
        any(not _is_blank(v) for v in (existing.get("api_config") or {}).values()),
    ])
    if not has_existing_user_values:
        return False

    return (
        bool(enabled) is False
        and (auth_mode or "browser") == "browser"
        and _is_blank(api_key)
        and _is_blank(api_secret)
        and _is_blank(access_token)
        and _is_blank(access_token_secret)
        and _is_blank(posting_summary)
        and _is_blank(posting_guidelines)
        and bool(auto_post) is False
        and bool(notify_on_approval_request) is False
        and bool(is_premium) is False
        and bool(enable_privacy_filter) is True
        and bool(fetch_thread_enabled) is False
        and int(thread_fetch_count or 3) == 3
    )


def handle_check_twitter_session(room_name: str = None):
    """Twitterのセッション状態を確認し、Markdown用のテキストを返す"""
    from twitter_manager import twitter_manager
    is_logged_in = twitter_manager.is_logged_in(room_name)

    if is_logged_in:
        return "セッション状態: ✅ **ログイン済み**"
    else:
        return "セッション状態: ❌ **未ログイン** (またはセッション切れ)"


def handle_twitter_login(room_name: str = None):
    """Twitterログイン用のブラウザを起動する"""
    from twitter_manager import twitter_manager
    gr.Info("ログイン用ブラウザを起動します。操作完了後にブラウザを閉じてください。")

    # ログイン起動
    success = twitter_manager.start_login(room_name)

    if success:
        # 再確認して状態を返す
        return handle_check_twitter_session(room_name)
    else:
        return "セッション状態: ⚠️ **ログイン起動失敗**"


def handle_twitter_cookie_import(cookies_json: str, room_name: str = None):
    """手動で貼り付けられたCookieをインポートする"""
    if not cookies_json or not cookies_json.strip():
        return "⚠️ **エラー**: Cookieが入力されていません。"

    from twitter_manager import twitter_manager
    success = twitter_manager.import_cookies(cookies_json, room_name)

    if success:
        return "✅ **成功**: Cookieをインポートしました。「状態を再確認」を押して反映を確認してください。"
    else:
        return "❌ **失敗**: JSONの形式が正しくないか、インポート中にエラーが発生しました。"


def handle_refresh_twitter_tab(room_name):
    """Twitterタブの設定情報を更新する（セッション状態の自動チェックは重いためスキップ）"""
    session_placeholder = "セッション状態: ❓ 未確認（ブラウザ起動に時間がかかるため、手動で「状態を再確認」を押してください）"
    settings = handle_load_twitter_settings(room_name)
    # settings 15個 + session 1個 = 計16個の要素を返す
    return [session_placeholder] + settings


def handle_refresh_twitter_all(room_name):
    """Twitterタブ選択時に、設定・下書き・履歴をまとめて更新する（通信回数削減によるフリーズ防止）"""
    from twitter_manager import twitter_manager
    # 1. 設定取得
    session_placeholder = "セッション状態: ❓ 未確認（ブラウザ起動に時間がかかるため、手動で「状態を再確認」を押してください）"
    settings = handle_load_twitter_settings(room_name)
    
    # 2. 最新データをディスクから1回だけロード
    twitter_manager.reload()
    
    # 3. 下書き取得
    drafts = [d for d in twitter_manager.get_pending_list() if not room_name or d.get("room_name") == room_name]
    drafts.sort(key=lambda d: str(d.get("timestamp", "")), reverse=True)
    draft_data = []
    for d in drafts:
        draft_data.append([
            d["id"],
            d["timestamp"].replace("T", " ")[:16],
            ", ".join(d.get("media_paths", [])) if d.get("media_paths") else "なし",
            d.get("filtered_content", ""),
            "\n".join(d.get("warnings", []))
        ])
    draft_df = pd.DataFrame(draft_data, columns=["ID", "時刻", "画像", "下書き内容", "警告"])

    # 4. 履歴取得（最新50件に制限してフリーズを防止）
    history = [h for h in twitter_manager.get_history_list() if not room_name or h.get("room_name") == room_name]
    history_data = []
    for h in history[:50]: # 最新50件のみ
        status = h.get("status", "unknown")
        if status == "failed":
            err = h.get("error", "")
            status = f"❌ failed ({err[:15]}...)" if err else "❌ failed"
        elif status == "posted":
            status = "✅ posted"

        history_data.append([
            h["id"],
            h["timestamp"].replace("T", " ")[:16],
            h.get("final_content", h.get("filtered_content", "")),
            status,
            h.get("post_url", "-")
        ])
    history_df = pd.DataFrame(history_data, columns=["ID", "時刻", "内容", "ステータス", "URL"])

    # 返却値の構成: session(1) + settings(15) + pending_df(1) + history_df(1) = 18個
    return [session_placeholder] + settings + [gr.update(value=draft_df), gr.update(value=history_df)]
