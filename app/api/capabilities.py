"""API Gateway の公開能力カタログ。

エンドポイント追加時はこの一覧へ access/scope/説明を必ず登録する。
認可、ユーザー向け一覧、ペルソナ能力一覧、漏れ検出テストの共通正本として使う。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Optional


ApiAccess = Literal["safe", "approval", "owner_only"]


@dataclass(frozen=True)
class ApiCapability:
    method: str
    path: str
    summary: str
    access: ApiAccess
    scope: Optional[str] = None

    def as_dict(self, room_id: str = "{room_id}") -> dict:
        data = asdict(self)
        data["path"] = self.path.replace("{room_id}", room_id)
        return data


API_CAPABILITIES: tuple[ApiCapability, ...] = (
    ApiCapability("GET", "/api/v1/rooms", "ルーム一覧", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/capabilities", "API能力・必要権限一覧", "safe"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/status", "ルーム状態", "safe"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/locations", "場所一覧", "safe"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/chat/history", "会話履歴", "approval", "read_chat"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/chat", "チャット送信", "approval", "send_chat"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/chat/regenerate", "最新応答の再生成", "approval", "send_chat"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/uploads", "チャット用画像アップロード", "approval", "send_chat"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/voice/transcribe", "音声文字起こし", "approval", "use_voice"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/tts", "音声合成", "approval", "use_voice"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/audio", "ルーム音声取得", "approval", "use_voice"),
    ApiCapability("GET", "/api/v1/audio", "音声取得（所有者Token用）", "owner_only"),
    ApiCapability("GET", "/api/v1/assets", "画像取得（所有者Token用）", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/assets", "ルーム画像取得", "approval", "read_items"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/events", "外部イベント記録", "approval", "write_event"),
    ApiCapability("GET", "/api/v1/notifications/events/settings", "外部イベント通知設定", "owner_only"),
    ApiCapability("PUT", "/api/v1/notifications/events/settings", "外部イベント通知設定更新", "owner_only"),
    ApiCapability("GET", "/api/v1/push/vapid-public-key", "Push公開鍵", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/push/vapid-public-key", "Push公開鍵（アプリ用）", "approval", "manage_push"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/push/subscriptions", "Push端末登録", "approval", "manage_push"),
    ApiCapability("DELETE", "/api/v1/rooms/{room_id}/push/subscriptions/{subscription_id}", "Push端末解除", "approval", "manage_push"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/push/status", "Push状態", "approval", "manage_push"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/push/test", "Push通知テスト", "approval", "manage_push"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/memory/search", "記憶検索", "approval", "read_memory"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/letters", "手紙箱一覧", "approval", "read_letters"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/letters/{letter_id}", "手紙本文", "approval", "read_letters"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/notes/{note_type}", "ノート閲覧", "approval", "read_notes"),
    ApiCapability("PUT", "/api/v1/rooms/{room_id}/notes/{note_type}", "研究・創作ノート更新", "approval", "write_notes"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/items", "アイテム一覧", "approval", "read_items"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/items/actions", "アイテム使用・移動", "approval", "write_items"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/calendar/events", "予定一覧", "approval", "read_calendar"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/calendar/events", "予定追加", "approval", "write_calendar"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/twitter/drafts", "Twitter下書き一覧", "approval", "read_twitter"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/twitter/drafts/{draft_id}/approve", "Twitter下書き承認・投稿", "approval", "post_twitter"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/twitter/drafts/{draft_id}/reject", "Twitter下書き却下", "approval", "post_twitter"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/location", "現在地変更", "approval", "write_location"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/autonomy", "自律行動状態", "approval", "read_autonomy"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/autonomy/preset", "自律行動プリセット変更", "approval", "write_autonomy"),
)


def _path_tail(path: str) -> Optional[str]:
    marker = "/api/v1/rooms/{room_id}/"
    if marker not in path:
        return None
    return path.split(marker, 1)[1].strip("/")


def _tail_matches(template: str, actual: str) -> bool:
    template_parts = template.strip("/").split("/")
    actual_parts = actual.strip("/").split("/")
    if len(template_parts) != len(actual_parts):
        return False
    return all(
        (part.startswith("{") and part.endswith("}")) or part == value
        for part, value in zip(template_parts, actual_parts)
    )


def atelier_access_for(tail: str, method: str) -> tuple[Optional[ApiAccess], Optional[str]]:
    normalized_method = str(method or "").upper()
    for capability in API_CAPABILITIES:
        template_tail = _path_tail(capability.path)
        if template_tail is None or capability.method != normalized_method:
            continue
        if _tail_matches(template_tail, tail):
            return capability.access, capability.scope
    return None, None


def capability_rows(room_id: str = "{room_id}") -> list[dict]:
    return [capability.as_dict(room_id) for capability in API_CAPABILITIES]


def render_capabilities_markdown(room_id: str = "{room_id}") -> str:
    access_labels = {"safe": "安全", "approval": "要承認", "owner_only": "所有者Token限定"}
    lines = [
        "### API能力一覧（正本カタログ）",
        "",
        "| メソッド | パス | 用途 | 利用条件 |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for capability in API_CAPABILITIES:
        path = capability.path.replace("{room_id}", room_id)
        condition = access_labels[capability.access]
        if capability.scope:
            condition += f": `{capability.scope}`"
        lines.append(f"| {capability.method} | `{path}` | {capability.summary} | {condition} |")
    return "\n".join(lines)
