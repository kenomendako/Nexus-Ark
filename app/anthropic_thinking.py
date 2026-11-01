"""Anthropic native thinking blockの同一tool loop内復元ヘルパー。

Anthropicの``thinking`` / ``redacted_thinking`` blockは、画面表示用の本文へ
変換すると署名を失う。そこで、表示へ変換する直前のcontent全体を
``AIMessage.additional_kwargs``へ一時保存し、次のtool往復で同じモデル・同じ
tool call IDのときだけ送信用contentへ戻す。

このモジュールは設定・永続store・API通信を持たず、LangGraphメッセージ内の
同一ターンだけを対象にする。他モデルの履歴へ署名を流用しないことを重要な
境界として扱う。
"""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from typing import Any


ANTHROPIC_THINKING_KEY = "__nexus_anthropic_thinking__"
_NATIVE_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


def _model_key(model_name: Any) -> str:
    """モデル名の比較用キーを作る。別名への推測変換は行わない。"""

    return str(model_name or "").strip().casefold()


def _tool_call_ids(message: Any) -> list[str]:
    """LangChain AIMessageのtool_callsからIDを順序付きで取得する。"""

    calls = getattr(message, "tool_calls", None)
    if not isinstance(calls, (list, tuple)):
        return []

    ids: list[str] = []
    for call in calls:
        if isinstance(call, Mapping):
            call_id = call.get("id")
        else:
            call_id = getattr(call, "id", None)
        if call_id is not None and str(call_id).strip():
            ids.append(str(call_id))
    return ids


def _is_ai_message(message: Any) -> bool:
    """AIMessage系だけを復元対象にする。Human/Tool markerは信用しない。"""

    message_type = str(getattr(message, "type", "") or "").strip().casefold()
    return message_type in {"ai", "assistant", "aimessagechunk"}


def _has_native_thinking_block(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, Mapping)
        and str(block.get("type") or "").strip().lower() in _NATIVE_THINKING_TYPES
        for block in content
    )


def capture_thinking_content(message: Any, model_name: str) -> dict[str, Any] | None:
    """送信前に復元対象のnative content全体を保存する。

    ``content``がnative thinking blockを含むlistでない場合は、通常の本文や
    既にUI用文字列へ変換された本文を誤保存しないため``None``を返す。
    """

    normalized_model = str(model_name or "").strip()
    content = getattr(message, "content", None)
    if not _is_ai_message(message) or not normalized_model or not _has_native_thinking_block(content):
        return None

    return {
        "content": deepcopy(content),
        "model_name": normalized_model,
        "tool_call_ids": deepcopy(_tool_call_ids(message)),
    }


def _valid_capture(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    content = value.get("content")
    model_name = str(value.get("model_name") or "").strip()
    tool_call_ids = value.get("tool_call_ids")
    if not model_name or not _has_native_thinking_block(content):
        return None
    if not isinstance(tool_call_ids, list) or any(
        not isinstance(call_id, str) or not call_id.strip() for call_id in tool_call_ids
    ):
        return None
    return {
        "content": content,
        "model_name": model_name,
        "tool_call_ids": tool_call_ids,
    }


def _copy_with_content(message: Any, content: Any) -> Any:
    """元messageへ代入せず、contentだけ差し替えた複製を返す。"""

    model_copy = getattr(message, "model_copy", None)
    if callable(model_copy):
        try:
            return model_copy(deep=True, update={"content": deepcopy(content)})
        except (TypeError, AttributeError):
            # LangChainの世代差でmodel_copyの引数が異なる場合はdeepcopyへ戻る。
            pass

    copied = deepcopy(message)
    copied.content = deepcopy(content)
    return copied


def restore_thinking_messages(messages: list[Any], model_name: str) -> list[Any]:
    """同じモデル・tool call IDのcaptureだけを送信用contentへ復元する。

    返却listと復元対象messageは複製し、入力listおよび入力messageを変更しない。
    markerの形が壊れている、モデル名が異なる、tool call IDが異なる場合は
    そのmessageをそのまま返す。
    """

    requested_model = _model_key(model_name)
    restored: list[Any] = list(messages or [])
    if not requested_model:
        return restored

    for index, message in enumerate(restored):
        if not _is_ai_message(message):
            continue
        additional_kwargs = getattr(message, "additional_kwargs", None)
        if not isinstance(additional_kwargs, Mapping):
            continue
        capture = _valid_capture(additional_kwargs.get(ANTHROPIC_THINKING_KEY))
        if capture is None:
            continue
        if _model_key(capture["model_name"]) != requested_model:
            continue
        if capture["tool_call_ids"] != _tool_call_ids(message):
            continue
        restored[index] = _copy_with_content(message, capture["content"])

    return restored


__all__ = [
    "ANTHROPIC_THINKING_KEY",
    "capture_thinking_content",
    "restore_thinking_messages",
]
