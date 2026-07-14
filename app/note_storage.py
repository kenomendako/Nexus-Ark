"""Shared note storage helpers for Gradio handlers and the Lite API."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

import constants
import file_lock_utils
import room_manager
from file_lock_utils import safe_text_read, safe_text_write


NOTE_WRITE_MAX_CHARS = 150_000
NoteTypeName = Literal["research", "creative"]


def normalize_note_type(note_type: str) -> NoteTypeName:
    normalized = str(note_type or "").strip().lower()
    if normalized not in {"research", "creative"}:
        raise ValueError("note_type must be research or creative")
    return normalized  # type: ignore[return-value]


def content_hash(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def get_room_note_path(room_name: str, default_filename: str, filename: str | None = None) -> str:
    """Resolve a note path. Archive names stay inside notes/archives."""
    if not filename:
        filename = default_filename

    normalized_filename = str(filename).replace("\\", "/").strip("/")
    notes_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME)

    if normalized_filename.startswith("archives/"):
        return os.path.join(notes_dir, normalized_filename)

    if normalized_filename.startswith("archive_"):
        return os.path.join(notes_dir, "archives", os.path.basename(normalized_filename))

    return os.path.join(notes_dir, os.path.basename(normalized_filename))


def get_creative_notes_path(room_name: str, filename: str | None = None) -> str:
    return get_room_note_path(room_name, constants.CREATIVE_NOTES_FILENAME, filename)


def get_research_notes_path(room_name: str, filename: str | None = None) -> str:
    return get_room_note_path(room_name, constants.RESEARCH_NOTES_FILENAME, filename)


def get_note_path(room_name: str, note_type: str, filename: str | None = None) -> str:
    normalized_type = normalize_note_type(note_type)
    if normalized_type == "research":
        return get_research_notes_path(room_name, filename)
    return get_creative_notes_path(room_name, filename)


def read_note_content(room_name: str, note_type: str, filename: str | None = None) -> str:
    if not room_name:
        return ""
    path = get_note_path(room_name, note_type, filename)
    if os.path.exists(path):
        return safe_text_read(path)
    return ""


class NoteWriteConflictError(ValueError):
    """The file changed after the caller loaded it."""


def write_note_content(
    room_name: str,
    note_type: str,
    content: str,
    filename: str | None = None,
    expected_hash: str | None = None,
) -> str:
    """Write a supported user-editable note with the same protection used by the UI.

    Only research and creative notes are supported. Append-only/system-managed stores
    such as diary entries are intentionally excluded because full-file replacement
    conflicts with their data model.
    """
    normalized_type = normalize_note_type(note_type)
    if content is None or str(content).strip() == "None":
        raise ValueError("無効な内容(None)が検知されたため、保存を中止しました。")
    if not room_name:
        raise ValueError("ルームが選択されていません。")

    content = str(content)
    if len(content) > NOTE_WRITE_MAX_CHARS:
        raise ValueError(f"ノート本文は{NOTE_WRITE_MAX_CHARS}文字以内にしてください。")

    if normalized_type == "research":
        default_filename = constants.RESEARCH_NOTES_FILENAME
        backup_kind = "research_notes"
    else:
        default_filename = constants.CREATIVE_NOTES_FILENAME
        backup_kind = ""

    path = Path(get_note_path(room_name, normalized_type, filename))
    with file_lock_utils.locked_file(path.as_posix()) as locked_path:
        current_content = ""
        if locked_path.exists():
            current_content = locked_path.read_text(encoding="utf-8")
        if expected_hash and expected_hash != content_hash(current_content):
            raise NoteWriteConflictError("Note was updated elsewhere. Reload before saving.")
        # アーカイブ・バックアップはハッシュ照合の後に行うこと。照合前に
        # archive_large_note が走ると本体が空化され base_hash が必ず不一致になる
        # （200KB超ノートの保存が常に409になり、Lite側の下書きが失われる）。
        # ロック保持中の再取得は共有ロックの同一スレッド再入で安全。
        if not filename or filename == default_filename:
            room_manager.archive_large_note(room_name, default_filename)
        if backup_kind:
            room_manager.create_backup(room_name, backup_kind)
        file_lock_utils._atomic_text_write(locked_path.as_posix(), content)
    return content
