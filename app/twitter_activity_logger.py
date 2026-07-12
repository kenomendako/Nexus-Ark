# twitter_activity_logger.py
"""
Twitter活動記憶システム（External Codex）

ペルソナのTwitter活動（投稿、リプライ受信、通知確認）を
イベント単位で構造化記録し、記憶システムに統合する。

データ保存先: characters/<room>/memory/twitter_activity/YYYY-MM.json
"""
import os
import json
import datetime
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

import constants
from filelock import FileLock

logger = logging.getLogger("twitter_activity_logger")

# --- コンテキスト保持設定 ---
TWITTER_CONTEXT_TURNS_DEFAULT = 3  # デフォルトの保持ターン数


def _get_activity_dir(room_name: str) -> Path:
    """Twitter活動ログの保存先ディレクトリを取得する"""
    rooms_dir = Path(constants.ROOMS_DIR)
    activity_dir = rooms_dir / room_name / "memory" / "twitter_activity"
    os.makedirs(activity_dir, exist_ok=True)
    return activity_dir


def _get_monthly_file_path(room_name: str, year_month: str) -> Path:
    """月次ファイルパスを取得する (YYYY-MM.json)"""
    return _get_activity_dir(room_name) / f"{year_month}.json"


def _get_context_state_path(room_name: str) -> Path:
    """コンテキスト保持用の状態ファイルパスを取得する"""
    return _get_activity_dir(room_name) / "_context_state.json"


def _load_monthly_file(room_name: str, year_month: str) -> List[Dict]:
    """月次ファイルを読み込む"""
    path = _get_monthly_file_path(room_name, year_month)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Twitter活動ログの読み込みエラー ({path}): {e}")
        return []


def _save_monthly_file(room_name: str, year_month: str, data: List[Dict]):
    """月次ファイルに保存する（排他制御付き）"""
    path = _get_monthly_file_path(room_name, year_month)
    lock_path = str(path) + ".lock"
    try:
        with FileLock(lock_path, timeout=5):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Twitter活動ログの保存エラー ({path}): {e}")


def _generate_event_id(event_type: str) -> str:
    """イベントIDを生成する (tw_YYYYMMDD_HHMMSS_<type>)"""
    now = datetime.datetime.now()
    return f"tw_{now.strftime('%Y%m%d_%H%M%S')}_{event_type}"


# =============================================================================
# 公開API: 記録
# =============================================================================

def log_post(
    room_name: str,
    content: str,
    motivation: str = "",
    reply_to: Optional[Dict[str, str]] = None,
    url: str = "",
    status: str = "pending",
    draft_id: str = "",
    media_paths: Optional[List[str]] = None
) -> str:
    """
    投稿/リプライの活動を記録する。

    Args:
        room_name: ルーム名
        content: 投稿内容
        motivation: ペルソナの投稿動機（任意）
        reply_to: リプライ先情報 {"author": "...", "url": "...", "text": "..."}
        url: 投稿URL（投稿済みの場合）
        status: ステータス (pending / posted / failed)
        draft_id: 下書きID
        media_paths: 添付画像パスのリスト

    Returns:
        生成されたイベントID
    """
    now = datetime.datetime.now()
    year_month = now.strftime("%Y-%m")
    event_type = "reply" if reply_to else "post"
    event_id = _generate_event_id(event_type)

    entry = {
        "id": event_id,
        "timestamp": now.isoformat(),
        "event_type": event_type,
        "content": content,
        "motivation": motivation,
        "reply_to": reply_to,
        "url": url,
        "status": status,
        "draft_id": draft_id,
        "media_paths": media_paths or []
    }

    data = _load_monthly_file(room_name, year_month)
    data.append(entry)
    _save_monthly_file(room_name, year_month, data)

    # コンテキスト保持ターンカウンタをリセット
    _reset_context_counter(room_name)

    logger.info(f"Twitter活動を記録しました: {event_type} (ID: {event_id})")
    return event_id


def update_post_status(room_name: str, draft_id: str, status: str, url: str = ""):
    """
    既存の投稿記録のステータスを更新する（承認→投稿完了）。
    draft_idで検索し、最新の一致するエントリを更新する。
    """
    now = datetime.datetime.now()
    year_month = now.strftime("%Y-%m")
    data = _load_monthly_file(room_name, year_month)

    updated = False
    for entry in reversed(data):
        if entry.get("draft_id") == draft_id:
            entry["status"] = status
            if url:
                entry["url"] = url
            entry["updated_at"] = now.isoformat()
            updated = True
            break

    if updated:
        _save_monthly_file(room_name, year_month, data)
        logger.info(f"Twitter活動のステータスを更新: draft_id={draft_id}, status={status}")


def log_notification_check(
    room_name: str,
    items: List[Dict[str, Any]],
    check_type: str = "notifications"
) -> str:
    """
    通知/メンション/タイムライン確認の活動を記録する。

    Args:
        room_name: ルーム名
        items: 取得した通知/ツイートのリスト
        check_type: 確認種別 (timeline / mentions / notifications)

    Returns:
        生成されたイベントID
    """
    now = datetime.datetime.now()
    year_month = now.strftime("%Y-%m")
    event_id = _generate_event_id("check")

    # 主な交流相手を抽出（最大5件）
    notable_interactions = []
    for item in items[:5]:
        interaction = {
            "author": item.get("author", "Unknown"),
            "text": (item.get("text", "") or "")[:100],
            "type": check_type
        }
        if item.get("url"):
            interaction["url"] = item["url"]
        notable_interactions.append(interaction)

    entry = {
        "id": event_id,
        "timestamp": now.isoformat(),
        "event_type": "notification_check",
        "check_type": check_type,
        "items_found": len(items),
        "notable_interactions": notable_interactions
    }

    data = _load_monthly_file(room_name, year_month)
    data.append(entry)
    _save_monthly_file(room_name, year_month, data)

    # コンテキスト保持ターンカウンタをリセット
    _reset_context_counter(room_name)

    logger.info(f"Twitter通知確認を記録: {check_type} ({len(items)}件)")
    return event_id


# =============================================================================
# 公開API: コンテキスト注入
# =============================================================================

def get_recent_activity_context(room_name: str, limit: int = 5) -> str:
    """
    コンテキスト注入用の直近活動サマリーを生成する。
    ターンカウンタが保持期間内であれば活動サマリーを返す。

    Args:
        room_name: ルーム名
        limit: 取得する最大イベント数

    Returns:
        プロンプト注入用のテキスト（空文字列 = 注入不要）
    """
    # ターンカウンタチェック
    if not _should_inject_context(room_name):
        return ""

    # 直近のイベントを取得（今月 + 先月を合算）
    now = datetime.datetime.now()
    current_month = now.strftime("%Y-%m")
    events = _load_monthly_file(room_name, current_month)

    # 先月分も見る（月初にまたがるケースに対応）
    if now.day <= 2:
        prev_month = (now.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
        prev_events = _load_monthly_file(room_name, prev_month)
        events = prev_events + events

    if not events:
        return ""

    # 直近limitイベントを取得
    recent = events[-limit:]

    lines = [
        "### 【最近のTwitter活動メモ】",
        "あなたが最近行ったTwitterでの活動の要約です。会話の中で関連する話題が出たら、自然に触れてください。"
    ]

    for event in recent:
        timestamp = event.get("timestamp", "")
        time_str = timestamp[11:16] if len(timestamp) >= 16 else "??:??"
        event_type = event.get("event_type", "unknown")

        if event_type in ("post", "reply"):
            content_preview = (event.get("content", "") or "")[:60]
            if len(event.get("content", "")) > 60:
                content_preview += "…"
            motivation = event.get("motivation", "")

            if event_type == "reply":
                reply_to = event.get("reply_to") or {}
                reply_author = reply_to.get("author", "不明")
                line = f"- [{time_str}] リプライ → {reply_author}: 「{content_preview}」"
            else:
                line = f"- [{time_str}] 投稿: 「{content_preview}」"

            if motivation:
                line += f"（動機: {motivation[:40]}）"
            
            media_paths = event.get("media_paths", [])
            if media_paths:
                line += f" [画像 {len(media_paths)}枚添付]"
                
            lines.append(line)

        elif event_type == "notification_check":
            check_type = event.get("check_type", "通知")
            items_found = event.get("items_found", 0)
            notable = event.get("notable_interactions", [])

            line = f"- [{time_str}] {check_type}確認: {items_found}件"
            if notable:
                # 最も注目すべき交流を1件だけ表示
                top = notable[0]
                top_text = (top.get("text", "") or "")[:40]
                line += f"。{top.get('author', '?')} 「{top_text}」"
            lines.append(line)

    return "\n".join(lines) + "\n"


# =============================================================================
# 公開API: 睡眠時処理用
# =============================================================================

def get_daily_activity(room_name: str, date_str: str) -> List[Dict]:
    """
    指定日のTwitter活動を取得する（睡眠時処理用）。

    Args:
        room_name: ルーム名
        date_str: 日付文字列 (YYYY-MM-DD)

    Returns:
        その日の活動イベントのリスト
    """
    # 月次ファイルから対象日のイベントをフィルタ
    year_month = date_str[:7]  # YYYY-MM
    events = _load_monthly_file(room_name, year_month)
    return [e for e in events if e.get("timestamp", "").startswith(date_str)]


def get_daily_activity_summary_for_dreaming(room_name: str, date_str: str) -> str:
    """
    睡眠時の日次要約生成に渡すための、その日のTwitter活動の構造化テキストを返す。

    Returns:
        LLMプロンプトに注入できる活動サマリー（空ならTwitter活動なし）
    """
    events = get_daily_activity(room_name, date_str)
    if not events:
        return ""

    lines = ["## 本日のTwitter (X) 活動"]
    post_count = 0
    reply_count = 0
    check_count = 0
    interacted_users = set()

    for event in events:
        event_type = event.get("event_type", "")

        if event_type == "post":
            post_count += 1
            content = (event.get("content", "") or "")[:100]
            motivation = event.get("motivation", "")
            line = f"- 投稿: 「{content}」"
            if motivation:
                line += f"（動機: {motivation}）"
            
            media_paths = event.get("media_paths", [])
            if media_paths:
                line += f"（画像 {len(media_paths)}枚添付）"
                
            lines.append(line)

        elif event_type == "reply":
            reply_count += 1
            reply_to = event.get("reply_to") or {}
            author = reply_to.get("author", "不明")
            interacted_users.add(author)
            content = (event.get("content", "") or "")[:100]
            motivation = event.get("motivation", "")
            reply_text = (reply_to.get("text", "") or "")[:60]
            line = f"- {author} へのリプライ: 「{content}」"
            if reply_text:
                line += f"（相手の発言: 「{reply_text}」）"
            if motivation:
                line += f"（動機: {motivation}）"
            
            media_paths = event.get("media_paths", [])
            if media_paths:
                line += f"（画像 {len(media_paths)}枚添付）"
                
            lines.append(line)

        elif event_type == "notification_check":
            check_count += 1
            notable = event.get("notable_interactions", [])
            for n in notable:
                author = n.get("author", "")
                if author:
                    interacted_users.add(author)
            items_found = event.get("items_found", 0)
            if items_found > 0 and notable:
                notable_text = ", ".join(
                    [f"{n.get('author', '?')}: 「{(n.get('text', '') or '')[:40]}」" for n in notable[:3]]
                )
                lines.append(f"- 通知確認 ({items_found}件): {notable_text}")

    # サマリー統計
    summary_parts = []
    if post_count:
        summary_parts.append(f"投稿{post_count}件")
    if reply_count:
        summary_parts.append(f"リプライ{reply_count}件")
    if check_count:
        summary_parts.append(f"通知確認{check_count}回")
    if interacted_users:
        summary_parts.append(f"交流相手: {', '.join(interacted_users)}")

    if summary_parts:
        lines.insert(1, f"（概要: {', '.join(summary_parts)}）")

    return "\n".join(lines)


def get_interacted_users(room_name: str, date_str: str) -> List[str]:
    """
    指定日にTwitterで交流したユーザー名一覧を返す（エンティティ候補抽出用）。
    """
    events = get_daily_activity(room_name, date_str)
    users = set()

    for event in events:
        if event.get("event_type") == "reply":
            reply_to = event.get("reply_to") or {}
            author = reply_to.get("author", "")
            if author:
                users.add(author)

        if event.get("event_type") == "notification_check":
            for n in event.get("notable_interactions", []):
                author = n.get("author", "")
                if author:
                    users.add(author)

    return list(users)


# =============================================================================
# 内部: コンテキスト保持ターンカウンタ管理
# =============================================================================

def _load_context_state(room_name: str) -> Dict:
    """コンテキスト状態を読み込む"""
    path = _get_context_state_path(room_name)
    if not path.exists():
        return {"remaining_turns": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"remaining_turns": 0}


def _save_context_state(room_name: str, state: Dict):
    """コンテキスト状態を保存する"""
    path = _get_context_state_path(room_name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"コンテキスト状態の保存エラー: {e}")


def _reset_context_counter(room_name: str):
    """Twitter活動記録時にカウンタをリセットする"""
    state = _load_context_state(room_name)
    state["remaining_turns"] = TWITTER_CONTEXT_TURNS_DEFAULT
    state["last_activity_at"] = datetime.datetime.now().isoformat()
    _save_context_state(room_name, state)


def _should_inject_context(room_name: str) -> bool:
    """コンテキスト注入が必要か判定する"""
    state = _load_context_state(room_name)
    return state.get("remaining_turns", 0) > 0


def consume_context_turn(room_name: str):
    """
    1ターンの消費を記録する。
    context_generator_node から呼び出されることを想定。
    """
    state = _load_context_state(room_name)
    remaining = state.get("remaining_turns", 0)
    if remaining > 0:
        state["remaining_turns"] = remaining - 1
        _save_context_state(room_name, state)
        logger.debug(f"Twitterコンテキスト残りターン: {remaining - 1}")
