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
    ApiCapability("GET", "/api/v1/rooms/{room_id}/api-key", "登録キーの表示名・共通/個別の選択状態（キー本体は返さない）", "owner_only"),
    ApiCapability("PUT", "/api/v1/rooms/{room_id}/api-key", "ルーム個別APIキー選択", "owner_only"),
    ApiCapability("DELETE", "/api/v1/rooms/{room_id}/api-key", "APIキーを共通設定へ戻す", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/response-settings", "Gemini応答の実効設定・登録済みキー名／モデル・版照合", "owner_only"),
    ApiCapability("PUT", "/api/v1/rooms/{room_id}/response-settings", "ルーム個別のGeminiキー・モデル・ローテーションを一括保存", "owner_only"),
    ApiCapability("DELETE", "/api/v1/rooms/{room_id}/response-settings", "ルームの応答設定全体を共通へ戻す（個別下書きは保持）", "owner_only"),
    ApiCapability("PUT", "/api/v1/rooms/{room_id}/autonomy/settings", "自律行動間隔の個別設定", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/user-memo", "置き手紙のbody・content_hash・更新時刻", "owner_only"),
    ApiCapability("PUT", "/api/v1/rooms/{room_id}/user-memo", "置き手紙保存", "owner_only"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/items/food", "手入力の食べ物アイテム作成", "owner_only"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/items/generate-profile", "AIによる食べ物・一般アイテムのプロファイル生成", "owner_only"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/items/generate-image", "AIによるアイテム画像生成", "owner_only"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/items/generated", "確認済みアイテムの所持品保存（AI補完・手入力）", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/temporary-location", "一時的現在地の適用状態・名前・情景文・写真有無と画像URL", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/temporary-location/image", "一時的現在地の認証済み画像取得", "owner_only"),
    ApiCapability("PUT", "/api/v1/rooms/{room_id}/temporary-location", "一時的現在地適用・解除", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/temporary-location/saved", "保存済み一時地点一覧", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/temporary-location/saved-entry", "nameで指定した保存地点の名前・情景・写真（取得だけでは適用しない）", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/temporary-location/saved-image", "保存済み一時地点の認証画像取得", "owner_only"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/temporary-location/saved", "確認済みの名前付き地点保存（適用なし）", "owner_only"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/temporary-location/generate", "写真からAI情景文の下書き生成（外部送信・費用あり）", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/chat/active", "本体接続チャットの進行中requests（タイマー一覧ではない）", "owner_only"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/chat/stop", "本体接続チャットの再試行停止", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms", "ルームID・表示名・説明・現在地の一覧", "owner_only"),
    ApiCapability("POST", "/api/v1/lite-travel/standby", "待機snapshot準備", "owner_only"),
    ApiCapability("GET", "/api/v1/lite-travel/standby", "待機snapshot状態", "owner_only"),
    ApiCapability("GET", "/api/v1/lite-travel/diagnostics", "Liteお出かけ互換・運用診断", "owner_only"),
    ApiCapability("POST", "/api/v1/lite-travel/return", "署名付きオンライン帰宅", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/capabilities", "API能力・必要権限一覧", "safe"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/status", "現在地・drives・応答時の感情・持続感情の申告/参考値/表示状態・反応スコア本日平均/件数/日付・目標数・プロフィール画像パス（閲覧専用）", "safe"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/locations", "移動先のid・名前・エリアと現在地", "safe"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/chat/history", "会話・音楽カード・通知/ツール案内・添付パス・時刻/モデル（limit、思考は所有者include_thoughtsのみ、RAW非公開）", "approval", "read_chat"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/chat/thoughts/translate", "表示済み思考ログを押下時のみ外部AIへ送って翻訳（費用・外部送信あり、書き込みなし）", "owner_only"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/chat", "チャット送信", "approval", "send_chat"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/chat/regenerate", "最新応答の再生成", "approval", "send_chat"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/uploads", "チャット用画像アップロード", "approval", "send_chat"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/voice/transcribe", "音声文字起こし", "approval", "use_voice"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/tts", "音声合成", "approval", "use_voice"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/audio", "pathで指定したルーム内生成音声のバイナリ", "approval", "use_voice"),
    ApiCapability("GET", "/api/v1/audio", "音声取得（所有者Token用）", "owner_only"),
    ApiCapability("GET", "/api/v1/assets", "画像取得（所有者Token用）", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/assets", "pathで指定したルーム内画像のバイナリ（添付・プロフィール・アイテム画像等）", "approval", "read_items"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/events", "外部イベント記録", "approval", "write_event"),
    ApiCapability("GET", "/api/v1/notifications/events/settings", "外部イベント通知設定", "owner_only"),
    ApiCapability("PUT", "/api/v1/notifications/events/settings", "外部イベント通知設定更新", "owner_only"),
    ApiCapability("GET", "/api/v1/push/vapid-public-key", "Push公開鍵", "owner_only"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/push/vapid-public-key", "Push公開鍵（アプリ用）", "approval", "manage_push"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/push/subscriptions", "Push端末登録", "approval", "manage_push"),
    ApiCapability("DELETE", "/api/v1/rooms/{room_id}/push/subscriptions/{subscription_id}", "Push端末解除", "approval", "manage_push"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/push/status", "Push登録端末数・送信履歴の時刻・失敗数（通知本文一覧ではない）", "approval", "manage_push"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/push/test", "Push通知テスト", "approval", "manage_push"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/memory/search", "queryに合う記憶のcontent・type・metadata（limit指定、検索用埋め込みを伴い得る）", "approval", "read_memory"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/letters", "手紙id・件名・日時・既読状態と未読数（limit指定）", "approval", "read_letters"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/letters/{letter_id}", "手紙本文・日時（既定で既読化、mark_read=falseで抑止）", "approval", "read_letters"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/notes/{note_type}", "研究/創作/日記/メモ帳/夢/エピソードのcontent・headings・content_hash（headings_only/heading/latest対応）", "approval", "read_notes"),
    ApiCapability("PUT", "/api/v1/rooms/{room_id}/notes/{note_type}", "研究・創作ノート更新", "approval", "write_notes"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/items", "target別アイテムのid・名前・所持数・説明・image_path（location指定可）", "approval", "read_items"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/items/actions", "アイテム贈与・使用・移動（本体と同じ通知・入力補助）", "approval", "write_items"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/calendar/events", "共有された予定の件名・開始/終了・終日・場所（days指定）", "approval", "read_calendar"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/calendar/events", "予定追加", "approval", "write_calendar"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/twitter/drafts", "Twitter下書きの本文・警告・返信先・画像パス（取得では投稿しない）", "approval", "read_twitter"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/twitter/drafts/{draft_id}/approve", "Twitter下書き承認・投稿", "approval", "post_twitter"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/twitter/drafts/{draft_id}/reject", "Twitter下書き却下", "approval", "post_twitter"),
    ApiCapability("POST", "/api/v1/rooms/{room_id}/location", "現在地変更", "approval", "write_location"),
    ApiCapability("GET", "/api/v1/rooms/{room_id}/autonomy", "自律行動の有効状態・間隔・quiet hours・preset（行動履歴やタイマー一覧ではない）", "approval", "read_autonomy"),
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
