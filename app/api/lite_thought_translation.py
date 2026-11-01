"""Liteから必要時に呼び出す思考ログ翻訳のサービス境界。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

import config_manager
import gemini_api
import room_manager


_MAX_THOUGHT_TEXT_CHARS = 100_000


class LiteThoughtTranslationRequest(BaseModel):
    """表示済み（必要ならマスク済み）の思考ログだけを受け取る。"""

    text: str = Field(..., min_length=1, max_length=_MAX_THOUGHT_TEXT_CHARS)

    @field_validator("text", mode="before")
    @classmethod
    def _trim_and_validate_text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("text must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be empty")
        if len(normalized) > _MAX_THOUGHT_TEXT_CHARS:
            raise ValueError("text is too long")
        return normalized


class LiteThoughtTranslationResponse(BaseModel):
    text: str


class ThoughtTranslationDisabledError(PermissionError):
    """ルームの本体表示設定が思考ログを無効にしている。"""


class ThoughtTranslationFailedError(RuntimeError):
    """翻訳結果が利用できない（例外・None・空文字を含む）。"""


def _require_room(room_id: str) -> str:
    """ルーム名を正規化し、パス境界と存在を同時に確認する。"""

    normalized = str(room_id or "").strip()
    if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ValueError("Room not found")
    if not any(folder == normalized for _, folder in room_manager.get_room_list_for_ui()):
        raise ValueError("Room not found")
    return normalized


def _display_thoughts_enabled(room_id: str) -> bool:
    try:
        effective_settings = config_manager.get_effective_settings(room_id)
    except Exception:
        return False
    return isinstance(effective_settings, dict) and effective_settings.get("display_thoughts", True) is True


def _agent_name(room_id: str) -> str:
    try:
        room_config = room_manager.get_room_config(room_id) or {}
    except Exception:
        room_config = {}
    if not isinstance(room_config, dict):
        room_config = {}
    return str(
        room_config.get("agent_display_name")
        or room_config.get("agent_name")
        or "このキャラクター"
    ).strip() or "このキャラクター"


def translate_thought_log(room_id: str, text: str) -> str:
    """思考ログを本体と同じ翻訳経路で処理し、成功した本文だけを返す。"""

    normalized_room_id = _require_room(room_id)
    if not _display_thoughts_enabled(normalized_room_id):
        raise ThoughtTranslationDisabledError

    try:
        translated = gemini_api.translate_thought_log_with_ai(text, _agent_name(normalized_room_id))
    except Exception as exc:
        raise ThoughtTranslationFailedError from exc
    if not isinstance(translated, str) or not translated.strip():
        raise ThoughtTranslationFailedError
    return translated.strip()


__all__ = [
    "LiteThoughtTranslationRequest",
    "LiteThoughtTranslationResponse",
    "ThoughtTranslationDisabledError",
    "ThoughtTranslationFailedError",
    "translate_thought_log",
]
