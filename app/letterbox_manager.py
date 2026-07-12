"""Persona-to-user letterbox storage and prompt helpers."""

from __future__ import annotations

import datetime as _dt
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import constants
from file_lock_utils import safe_json_read, safe_json_update, safe_json_write
from utils import normalized_text_similarity


LETTERBOX_DIR_NAME = "letterbox"
LETTERS_FILENAME = "letters.json"
RECENT_READ_WINDOW_HOURS = 24
LETTER_DEDUP_LOOKBACK_DAYS = 7
LETTER_DEDUP_SIMILARITY_THRESHOLD = 0.85
LETTERBOX_SOFT_LIMIT = 100


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso_now() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _parse_datetime(value: Any) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = _dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.astimezone(_dt.timezone.utc)
    except Exception:
        return None


def _letters_path(room_name: str) -> Path:
    return Path(constants.ROOMS_DIR) / room_name / LETTERBOX_DIR_NAME / LETTERS_FILENAME


def _clean_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _normalize_letter(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    letter_id = _clean_text(raw.get("id"))
    title = _clean_text(raw.get("title"))
    body = _clean_text(raw.get("body"))
    created_at = _clean_text(raw.get("created_at"))
    if not letter_id or not title or not body or not created_at:
        return None
    return {
        "id": letter_id,
        "title": title,
        "body": body,
        "created_at": created_at,
        "read_at": raw.get("read_at") or None,
    }


def _load_letters(room_name: str) -> List[Dict[str, Any]]:
    path = _letters_path(room_name)
    try:
        data = safe_json_read(str(path), default=[])
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("letters", [])
    if not isinstance(data, list):
        return []
    letters = []
    for item in data:
        normalized = _normalize_letter(item)
        if normalized:
            letters.append(normalized)
    letters.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return letters


def _save_letters(room_name: str, letters: List[Dict[str, Any]]) -> bool:
    return safe_json_write(str(_letters_path(room_name)), letters)


def _update_letters(room_name: str, mutator) -> List[Dict[str, Any]]:
    updated: List[Dict[str, Any]] = []

    def update(data: Any) -> List[Dict[str, Any]]:
        nonlocal updated
        raw_letters = data.get("letters", []) if isinstance(data, dict) else data
        if not isinstance(raw_letters, list):
            raw_letters = []
        letters = []
        for item in raw_letters:
            normalized = _normalize_letter(item)
            if normalized:
                letters.append(normalized)
        letters.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        result = mutator(letters)
        if isinstance(result, list):
            letters = result
        letters.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        updated = [dict(letter) for letter in letters]
        return letters

    safe_json_update(str(_letters_path(room_name)), update, default=[])
    return updated


def _find_similar_recent_letter(
    letters: List[Dict[str, Any]],
    title: str,
    body: str,
) -> Optional[Dict[str, Any]]:
    cutoff = _now_utc() - _dt.timedelta(days=LETTER_DEDUP_LOOKBACK_DAYS)
    for letter in letters:
        created_at = _parse_datetime(letter.get("created_at"))
        if not created_at or created_at < cutoff:
            continue
        title_similarity = normalized_text_similarity(title, letter.get("title", ""))
        body_similarity = normalized_text_similarity(body, letter.get("body", ""))
        if (
            title_similarity >= LETTER_DEDUP_SIMILARITY_THRESHOLD
            or body_similarity >= LETTER_DEDUP_SIMILARITY_THRESHOLD
        ):
            return {
                "id": letter.get("id", ""),
                "title": letter.get("title", ""),
                "created_at": letter.get("created_at", ""),
                "read_at": letter.get("read_at") or None,
                "title_similarity": title_similarity,
                "body_similarity": body_similarity,
            }
    return None


def add_letter(room_name: str, title: str, body: str, allow_similar: bool = False) -> Dict[str, Any]:
    if not room_name:
        raise ValueError("room_name is required")
    clean_title = _clean_text(title)
    clean_body = _clean_text(body)
    if not clean_title:
        raise ValueError("title is required")
    if not clean_body:
        raise ValueError("body is required")

    letter: Dict[str, Any] = {}

    def mutate(letters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        nonlocal letter
        similar = None if allow_similar else _find_similar_recent_letter(letters, clean_title, clean_body)
        if similar:
            letter = {
                "id": similar.get("id", ""),
                "title": similar.get("title", ""),
                "body": "",
                "created_at": similar.get("created_at", ""),
                "read_at": similar.get("read_at"),
                "_dedup_skipped": True,
                "_similar_letter": similar,
            }
            return letters
        letter = {
            "id": f"ltr_{_now_utc().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
            "title": clean_title,
            "body": clean_body,
            "created_at": _iso_now(),
            "read_at": None,
        }
        letters.insert(0, letter)
        return letters

    letters = _update_letters(room_name, mutate)
    if not letter.get("_dedup_skipped") and len(letters) > LETTERBOX_SOFT_LIMIT:
        letter["_letterbox_over_limit"] = True
        letter["_letterbox_count"] = len(letters)
    return letter


def list_letters(room_name: str, limit: int = 50) -> List[Dict[str, Any]]:
    letters = _load_letters(room_name)
    try:
        safe_limit = max(1, int(limit))
    except Exception:
        safe_limit = 50
    return letters[:safe_limit]


def get_letter(room_name: str, letter_id: str) -> Optional[Dict[str, Any]]:
    for letter in _load_letters(room_name):
        if letter.get("id") == letter_id:
            return letter
    return None


def mark_read(room_name: str, letter_id: str) -> Optional[Dict[str, Any]]:
    updated = None

    def mutate(letters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        nonlocal updated
        for letter in letters:
            if letter.get("id") == letter_id:
                if not letter.get("read_at"):
                    letter["read_at"] = _iso_now()
                updated = dict(letter)
                break
        return letters

    _update_letters(room_name, mutate)
    return updated


def delete_letter(room_name: str, letter_id: str) -> Optional[Dict[str, Any]]:
    """指定IDの手紙を削除し、削除した手紙を返す（存在しなければNone）。"""
    deleted = None

    def mutate(letters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        nonlocal deleted
        kept = []
        for letter in letters:
            if letter.get("id") == letter_id and deleted is None:
                deleted = dict(letter)
            else:
                kept.append(letter)
        return kept

    _update_letters(room_name, mutate)
    return deleted


def unread_count(room_name: str) -> int:
    return sum(1 for letter in _load_letters(room_name) if not letter.get("read_at"))


def build_letterbox_section(room_name: str) -> str:
    """Build a one-line prompt section without mutating letter state."""
    letters = _load_letters(room_name)
    unread = [letter for letter in letters if not letter.get("read_at")]
    now = _now_utc()
    recent_read = []
    for letter in letters:
        read_at = _parse_datetime(letter.get("read_at"))
        if read_at and now - read_at <= _dt.timedelta(hours=RECENT_READ_WINDOW_HOURS):
            recent_read.append(letter)

    if not unread and not recent_read:
        return ""

    parts = []
    if unread:
        titles = "、".join(f"「{letter.get('title', '')}」" for letter in unread[:3])
        parts.append(f"📮 手紙箱: 未読{len(unread)}通（{titles}）")
    if recent_read:
        titles = "、".join(f"「{letter.get('title', '')}」" for letter in recent_read[:3])
        parts.append(f"（{titles}は読まれました）")
    return "\n" + " ".join(parts) + "\n"


def recent_letter_titles(room_name: str, limit: int = 5) -> List[str]:
    return [letter.get("title", "") for letter in list_letters(room_name, limit=limit)]
