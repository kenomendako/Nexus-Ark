"""Lite帰宅ログの元日時と、既存import journal内の記憶整理待ちを扱う。

会話本文や記憶本文の新しい正本は作らない。要約前の対象markerを保持し、
記憶の保存成功後にそのsnapshotだけを完了扱いにする。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import constants
import file_lock_utils

PENDING_KEY = "episodic_pending_events"
COMPLETED_KEY = "episodic_completed_events"
_MARKER_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_event_datetime(value: Any) -> dt.datetime | None:
    """offset付きISO日時だけを、本体ローカル時刻に変換する。"""
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone()
    except (ValueError, OverflowError, OSError):
        return None


def event_marker(session_id: str, room_name: str, event_id: str) -> str:
    canonical = "\x00".join((str(session_id), str(room_name), str(event_id)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pending_from_state(state: dict) -> dict[str, dict[str, str | None]]:
    raw = state.get(PENDING_KEY, {})
    if not isinstance(raw, dict):
        raise ValueError("Lite帰宅の記憶整理待ち記録が不正です。")
    result = {}
    for marker, descriptor in raw.items():
        if not isinstance(marker, str) or not _MARKER_PATTERN.fullmatch(marker) or not isinstance(descriptor, dict):
            raise ValueError("Lite帰宅の記憶整理待ちmarkerが不正です。")
        date = descriptor.get("date")
        if date is not None:
            if not isinstance(date, str) or not _DATE_PATTERN.fullmatch(date):
                raise ValueError("Lite帰宅の記憶整理対象日が不正です。")
            try:
                dt.date.fromisoformat(date)
            except ValueError as exc:
                raise ValueError("Lite帰宅の記憶整理対象日が不正です。") from exc
        result[marker] = dict(descriptor)
    return result


def _completed_from_state(state: dict) -> set[str]:
    raw = state.get(COMPLETED_KEY, [])
    if not isinstance(raw, list) or any(not isinstance(marker, str) or not _MARKER_PATTERN.fullmatch(marker) for marker in raw):
        raise ValueError("Lite帰宅の記憶整理済み記録が不正です。")
    return set(raw)


def carry_return_pending(old_state: dict, new_state: dict) -> dict:
    """新しい外出へ切り替えても、未整理分と完了markerを保持する。"""
    result = {**old_state, **new_state}
    pending = _pending_from_state(old_state)
    completed = _completed_from_state(old_state)
    if pending or PENDING_KEY in old_state:
        result[PENDING_KEY] = pending
    if completed or COMPLETED_KEY in old_state:
        result[COMPLETED_KEY] = sorted(completed)
    return result


def merge_return_pending(state: dict, events: Iterable[dict], session_id: str, room_name: str) -> dict:
    """取り込み済み本文の会話markerを、LLMなしで整理待ちへ差分登録する。"""
    result = dict(state)
    pending = _pending_from_state(state)
    completed = _completed_from_state(state)
    for event in events:
        if event.get("type") not in {"user_message", "assistant_message"}:
            continue
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ValueError("Lite帰宅の記憶整理対象eventが不正です。")
        marker = event_marker(session_id, room_name, event_id)
        if marker in completed:
            continue
        timestamp = parse_event_datetime(event.get("created_at"))
        date = timestamp.date().isoformat() if timestamp else None
        if marker in pending:
            if pending[marker].get("date") != date:
                raise ValueError("Lite帰宅の記憶整理対象日が既存記録と一致しません。")
        else:
            pending[marker] = {"date": date}
    result[PENDING_KEY] = pending
    result[COMPLETED_KEY] = sorted(completed)
    return result


def _state_paths(room_name: str) -> tuple[Path, Path]:
    if not isinstance(room_name, str) or not room_name.strip() or room_name in {".", ".."} or any(c in room_name for c in ("/", "\\", "\x00")):
        raise ValueError("不正なペルソナIDです。")
    key = hashlib.sha256(room_name.encode("utf-8")).hexdigest()[:24]
    root = Path(constants.METADATA_DIR) / "lite_travel" / "imports"
    return root / f"{key}.json", root / f"{key}.operation"


def _read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Lite帰宅の記憶整理記録を読み込めません。") from exc
    if not isinstance(value, dict):
        raise ValueError("Lite帰宅の記憶整理記録が不正です。")
    return value


def pending_return_memory(room_name: str) -> dict[str, dict[str, str | None]]:
    path, operation = _state_paths(room_name)
    if not path.exists():
        return {}
    with file_lock_utils.locked_file(str(operation)):
        return _pending_from_state(_read_state(path))


def acknowledge_return_memory(room_name: str, markers: Iterable[str]) -> None:
    """保存済みsnapshotのmarkerだけを完了にし、後発追加や別fieldを残す。"""
    accepted = set(markers)
    if not accepted:
        return
    if any(not isinstance(marker, str) or not _MARKER_PATTERN.fullmatch(marker) for marker in accepted):
        raise ValueError("Lite帰宅の記憶整理完了markerが不正です。")
    path, operation = _state_paths(room_name)
    with file_lock_utils.locked_file(str(operation)):
        state = _read_state(path)
        pending = _pending_from_state(state)
        completed = _completed_from_state(state)
        completed.update(marker for marker in accepted if marker in pending)
        state[PENDING_KEY] = {marker: descriptor for marker, descriptor in pending.items() if marker not in accepted}
        state[COMPLETED_KEY] = sorted(completed)
        if not file_lock_utils.safe_json_write(str(path), state):
            raise OSError("Lite帰宅の記憶整理完了を保存できません。")
