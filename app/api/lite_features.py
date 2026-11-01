"""Lite PWAから必要時に呼び出す軽量な本体機能。

このモジュールはAPI Gatewayのルートから利用するサービス境界を提供する。
本体の既存マネージャーを再利用し、秘密値・他ルームのデータを返さない。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

import config_manager
import constants
import room_manager
from api.schemas import NoteResponse
from dream_journal_display import format_dream_journal_entry
from file_lock_utils import locked_file, safe_json_read, safe_text_read, safe_text_write
from note_storage import content_hash


logger = logging.getLogger(__name__)


LiteKeyScope = Literal["common", "room"]
LiteNoteType = Literal["research", "creative", "diary", "notepad", "dream", "episodic", "episode"]


class LiteApiKeyOption(BaseModel):
    """APIキーの表示用情報。キー本体は意図的に含めない。"""

    name: str
    paid: bool = False
    provider: str = "google"


class LiteApiKeyOptionsResponse(BaseModel):
    room_id: str
    keys: List[LiteApiKeyOption] = Field(default_factory=list)
    selected_key_name: Optional[str] = None
    selected_scope: LiteKeyScope = "common"
    room_key_name: Optional[str] = None
    common_key_name: Optional[str] = None


class LiteApiKeySelectionRequest(BaseModel):
    key_name: str = Field(..., min_length=1, max_length=120)


class LiteApiKeySelectionResponse(LiteApiKeyOptionsResponse):
    status: Literal["saved", "common"] = "saved"


class LiteAutonomySettingsResponse(BaseModel):
    room_id: str
    enabled: bool = False
    inactivity_minutes: int = 120
    schedule_cooldown_minutes: int = 60
    quiet_hours_start: str = "00:00"
    quiet_hours_end: str = "07:00"
    status: str = "ok"


class LiteAutonomySettingsUpdateRequest(BaseModel):
    inactivity_minutes: Optional[int] = Field(default=None, ge=1, le=7 * 24 * 60)
    schedule_cooldown_minutes: Optional[int] = Field(default=None, ge=1, le=7 * 24 * 60)

    @model_validator(mode="after")
    def _require_one_value(self) -> "LiteAutonomySettingsUpdateRequest":
        if self.inactivity_minutes is None and self.schedule_cooldown_minutes is None:
            raise ValueError("at least one autonomy interval is required")
        return self


class LiteUserMemoResponse(BaseModel):
    """Liteと本体が共有する ``user_memo.txt`` の表示用応答。"""

    room_id: str
    body: str = ""
    content_hash: str = ""
    updated_at: Optional[str] = None


class LiteUserMemoUpdateRequest(BaseModel):
    """ユーザーメモの差分更新。空本文はメモを消す操作として扱う。"""

    body: str = Field(..., max_length=20_000)
    base_hash: str = Field(..., min_length=64, max_length=128)

    @model_validator(mode="after")
    def _normalize_base_hash(self) -> "LiteUserMemoUpdateRequest":
        self.base_hash = self.base_hash.strip()
        if not self.base_hash:
            raise ValueError("base_hash is required")
        return self


class LiteMemoConflictError(ValueError):
    """読み込み後に ``user_memo.txt`` が変更された。"""


class LiteFoodItemCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    category: str = Field(default="食べ物", min_length=1, max_length=60)
    amount: int = Field(default=1, ge=1, le=99)
    description: str = Field(default="", max_length=2_000)
    flavor_text: str = Field(default="", max_length=4_000)
    taste_profile: Dict[str, Any] = Field(default_factory=dict)
    physical_sensation: Dict[str, Any] = Field(default_factory=dict)
    synesthesia: Dict[str, Any] = Field(default_factory=dict)


class LiteFoodItemCreateResponse(BaseModel):
    room_id: str
    item_id: str
    name: str
    amount: int
    status: Literal["created"] = "created"


_MAX_ITEM_FIELD_KEYS = 16
_MAX_ITEM_MAPPING_BYTES = 8_000
_item_locks: Dict[str, threading.RLock] = {}


def _require_room(room_id: str) -> str:
    normalized = str(room_id or "").strip()
    if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ValueError("Room not found")
    if not any(folder == normalized for _, folder in room_manager.get_room_list_for_ui()):
        raise ValueError(f"Room not found: {normalized}")
    return normalized


def _room_dir(room_id: str) -> Path:
    """_require_room後だけ呼び、ルーム境界内のパスを組み立てる。"""

    return Path(constants.ROOMS_DIR) / room_id


def _ensure_main_mutation_allowed(room_id: str) -> None:
    """独立した外出中は本体へ書き込まず、帰宅後の同期を待つ。"""

    try:
        import lite_travel

        locked = lite_travel.is_presence_locked(room_id)
    except Exception as exc:
        logger.warning("Failed to inspect Lite presence lock for %s: %s", room_id, exc)
        raise ValueError("独立モードの状態を確認できないため、保存を中止しました") from exc
    if locked:
        raise ValueError("独立モード利用中は本体の設定・記憶を変更できません")


def _valid_registered_keys() -> List[str]:
    values = config_manager.GEMINI_API_KEYS if isinstance(config_manager.GEMINI_API_KEYS, dict) else {}
    return sorted(
        str(name)
        for name, value in values.items()
        if str(name).strip()
        and isinstance(value, str)
        and bool(value.strip())
        and not value.startswith("YOUR_API_KEY")
    )


def _global_key_name(valid_names: List[str]) -> Optional[str]:
    configured = ""
    if isinstance(config_manager.CONFIG_GLOBAL, dict):
        configured = str(config_manager.CONFIG_GLOBAL.get("last_api_key_name") or "").strip()
    if configured in valid_names:
        return configured
    try:
        latest = config_manager.get_latest_api_key_name_from_config()
    except Exception:
        latest = None
    if latest and latest in valid_names:
        return latest
    return valid_names[0] if valid_names else None


def _room_override(room_id: str) -> Dict[str, Any]:
    try:
        config = room_manager.get_room_config(room_id) or {}
    except Exception as exc:
        logger.warning("Failed to read room config for Lite feature: %s", exc)
        return {}
    overrides = config.get("override_settings", {}) if isinstance(config, dict) else {}
    return dict(overrides) if isinstance(overrides, dict) else {}


def _api_key_options(room_id: str) -> LiteApiKeyOptionsResponse:
    room_id = _require_room(room_id)
    names = _valid_registered_keys()
    raw_paid_names = config_manager.CONFIG_GLOBAL.get("paid_api_key_names", []) if isinstance(config_manager.CONFIG_GLOBAL, dict) else []
    paid_names = (
        {str(name).strip() for name in raw_paid_names if str(name).strip()}
        if isinstance(raw_paid_names, (list, tuple, set))
        else set()
    )
    options = [LiteApiKeyOption(name=name, paid=name in paid_names) for name in names]
    common_name = _global_key_name(names)
    overrides = _room_override(room_id)
    room_name = str(overrides.get("api_key_name") or "").strip()
    provider = str(overrides.get("provider") or "").strip().lower()
    if provider == "google" and room_name in names:
        selected_name = room_name
        scope: LiteKeyScope = "room"
        visible_room_name: Optional[str] = room_name
    else:
        selected_name = common_name
        scope = "common"
        visible_room_name = room_name if room_name in names else None
    return LiteApiKeyOptionsResponse(
        room_id=room_id,
        keys=options,
        selected_key_name=selected_name,
        selected_scope=scope,
        room_key_name=visible_room_name,
        common_key_name=common_name,
    )


def list_api_key_options(room_id: str) -> LiteApiKeyOptionsResponse:
    """登録済みキーの名前と、指定ルームでの適用範囲だけを返す。"""

    return _api_key_options(room_id)


def _ensure_google_key_selection_is_consistent(room_id: str) -> None:
    """Geminiキーを非Googleルームや非Geminiモデルへ誤適用しない。"""

    overrides = _room_override(room_id)
    raw_provider = str(overrides.get("provider") or "").strip().lower()
    try:
        provider = config_manager.get_active_provider(room_id)
    except Exception as exc:
        raise ValueError("現在のAIプロバイダを確認できないため、Geminiキーを変更できません") from exc
    if raw_provider and raw_provider not in {"google", "default"}:
        raise ValueError("Google GeminiキーはGoogleプロバイダのルームでのみ選択できます")
    if str(provider or "").strip().lower() != "google":
        raise ValueError("Google GeminiキーはGoogleプロバイダのルームでのみ選択できます")
    model_name = str(overrides.get("model_name") or "").strip().lower()
    if model_name and not model_name.startswith("gemini"):
        raise ValueError("Google Geminiキーを選ぶ前にルームのモデルをGeminiへ変更してください")


def select_room_api_key(room_id: str, request: LiteApiKeySelectionRequest) -> LiteApiKeySelectionResponse:
    room_id = _require_room(room_id)
    _ensure_main_mutation_allowed(room_id)
    requested_name = str(request.key_name or "").strip()
    if requested_name not in _valid_registered_keys():
        raise ValueError("registered API key was not found")
    _ensure_google_key_selection_is_consistent(room_id)
    result = room_manager.update_room_config(
        room_id,
        {"override_settings": {"provider": "google", "api_key_name": requested_name}},
    )
    if result is False:
        raise ValueError("failed to save room API key selection")
    return LiteApiKeySelectionResponse(**_api_key_options(room_id).model_dump(), status="saved")


def reset_room_api_key_to_common(room_id: str) -> LiteApiKeySelectionResponse:
    room_id = _require_room(room_id)
    _ensure_main_mutation_allowed(room_id)
    # Geminiのルームキーだけを個別設定から外す。provider/model/rotationは
    # ルーム側で選んだ関連設定なので保持し、キー名が空になれば本体の
    # get_active_gemini_api_key(_name) が共通設定へフォールバックする。
    result = room_manager.update_room_config(
        room_id,
        {"override_settings": {"api_key_name": None}},
    )
    if result is False:
        raise ValueError("failed to reset room API key selection")
    return LiteApiKeySelectionResponse(**_api_key_options(room_id).model_dump(), status="common")


def _autonomy_payload(room_id: str, *, status: str = "ok") -> LiteAutonomySettingsResponse:
    settings = config_manager.get_effective_settings(room_id).get("autonomous_settings", {}) or {}
    return LiteAutonomySettingsResponse(
        room_id=room_id,
        enabled=bool(settings.get("enabled", False)),
        inactivity_minutes=max(1, int(settings.get("inactivity_minutes", 120) or 120)),
        schedule_cooldown_minutes=max(1, int(settings.get("schedule_cooldown_minutes", 60) or 60)),
        quiet_hours_start=str(settings.get("quiet_hours_start", "00:00") or "00:00"),
        quiet_hours_end=str(settings.get("quiet_hours_end", "07:00") or "07:00"),
        status=status,
    )


def get_autonomy_settings(room_id: str) -> LiteAutonomySettingsResponse:
    room_id = _require_room(room_id)
    return _autonomy_payload(room_id)


def update_autonomy_settings(room_id: str, request: LiteAutonomySettingsUpdateRequest) -> LiteAutonomySettingsResponse:
    room_id = _require_room(room_id)
    _ensure_main_mutation_allowed(room_id)
    updates = request.model_dump(exclude_none=True)
    if not updates:
        raise ValueError("at least one autonomy interval is required")
    result = room_manager.update_room_override_nested(room_id, "autonomous_settings", updates)
    if result is False:
        raise ValueError("failed to save autonomy settings")
    return _autonomy_payload(room_id, status="saved")


def _memo_path(room_id: str) -> Path:
    """本体の ``load_user_memo`` と同じ正本ファイルを返す。"""

    return _room_dir(room_id) / "user_memo.txt"


def _memo_updated_at(path: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except (FileNotFoundError, OSError):
        return None


def _memo_response(room_id: str, body: str, path: Path) -> LiteUserMemoResponse:
    normalized_body = str(body or "").strip()
    return LiteUserMemoResponse(
        room_id=room_id,
        body=normalized_body,
        content_hash=content_hash(normalized_body),
        updated_at=_memo_updated_at(path),
    )


def get_user_memo(room_id: str) -> LiteUserMemoResponse:
    """ルームの置き手紙（``user_memo.txt``）を必要時だけ読む。"""

    room_id = _require_room(room_id)
    path = _memo_path(room_id)
    body = safe_text_read(path.as_posix(), default="").strip()
    return _memo_response(room_id, body, path)


def save_user_memo(room_id: str, request: LiteUserMemoUpdateRequest) -> LiteUserMemoResponse:
    """置き手紙を競合検出付きでアトミック保存する。

    本体UIの ``handle_save_user_memo`` と同じく前後空白は保存しない。空本文は
    メモを消す操作として許可し、文字列 ``None`` はUI値の混入として拒否する。
    比較と保存は同じファイルロックの中で行うため、読み出し後の別更新を検出できる。
    """

    room_id = _require_room(room_id)
    _ensure_main_mutation_allowed(room_id)
    body = str(request.body or "").strip()
    if body == "None":
        raise ValueError("無効な内容(None)が検知されたため、保存を中止しました。")
    path = _memo_path(room_id)
    with locked_file(path.as_posix()) as locked_path:
        current_body = locked_path.read_text(encoding="utf-8").strip() if locked_path.exists() else ""
        current_hash = content_hash(current_body)
        if request.base_hash and request.base_hash != current_hash:
            raise LiteMemoConflictError("Memo was updated elsewhere. Reload before saving.")
        saved = safe_text_write(path.as_posix(), body)
        if not saved:
            raise ValueError("failed to save user memo")
    return _memo_response(room_id, body, path)


def _read_json_records(
    primary_dir: Path,
    legacy_path: Path,
    *,
    predicate: Optional[Any] = None,
    latest_only: bool = False,
) -> tuple[List[Dict[str, Any]], List[Path]]:
    """月次正本を優先し、正本が空のときだけレガシーを読む。

    ``DreamingManager`` / ``EpisodicMemoryManager`` の保存先と同じ場所を使うが、
    GET処理でマネージャーを初期化して移行・修復を発生させない。各ファイルは
    ``safe_json_read`` のロック付き読み込みだけを行う。
    """

    records: List[Dict[str, Any]] = []
    paths: List[Path] = []
    if primary_dir.is_dir():
        for path in sorted(primary_dir.glob("*.json"), key=lambda item: item.name, reverse=True):
            try:
                data = safe_json_read(path.as_posix(), default=[])
            except Exception as exc:
                logger.warning("Lite memory read failed: %s (%s)", path, exc)
                continue
            if not isinstance(data, list):
                continue
            valid = [dict(item) for item in data if isinstance(item, dict)]
            if valid and latest_only and predicate is not None:
                visible = [item for item in valid if predicate(item)]
                if visible:
                    return visible, [path]
                continue
            if valid:
                records.extend(valid)
                paths.append(path)
    if records:
        return records, paths

    if legacy_path.is_file():
        try:
            data = safe_json_read(legacy_path.as_posix(), default=[])
        except Exception as exc:
            logger.warning("Lite legacy memory read failed: %s (%s)", legacy_path, exc)
            return [], []
        if isinstance(data, list):
            records = [dict(item) for item in data if isinstance(item, dict)]
            if predicate is not None:
                records = [item for item in records if predicate(item)]
            return records, [legacy_path] if records else []
    return [], []


def _record_preview(record: Dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = str(record.get(field) or "").strip()
        if value:
            return value.replace("\n", " ")[:30]
    return ""


def _record_content(record: Dict[str, Any], fields: tuple[str, ...]) -> str:
    """表示本文を作る。秘密メタデータを返さず、本文系フィールドだけを使う。"""

    parts: List[str] = []
    for field in fields:
        value = str(record.get(field) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return "\n\n".join(parts)


def _record_stamp(record: Dict[str, Any], kind: str) -> str:
    if kind == "dream":
        return str(record.get("created_at") or record.get("date") or "").strip()
    return str(record.get("date") or record.get("created_at") or "").strip()


def _record_sort_key(record: Dict[str, Any], kind: str) -> tuple[str, str]:
    if kind == "dream":
        return (str(record.get("created_at") or record.get("date") or "").strip(), "")
    date_value = str(record.get("date") or "").strip().replace("～", "~")
    if "~" in date_value:
        date_value = date_value.split("~")[-1].strip()
    return (date_value, str(record.get("created_at") or "").strip())


def _is_real_dream(record: Dict[str, Any]) -> bool:
    """夢ストアに混在する解決済み問い・PP痕跡・traceを除く。"""

    if record.get("source_type"):
        return False
    if record.get("trace") or record.get("is_trace"):
        return False
    if str(record.get("type") or "").strip().lower() in {"trace", "resolution", "insight_trace"}:
        return False
    trigger = str(record.get("trigger_topic") or "").strip()
    if trigger.startswith("解決された問い"):
        return False
    return bool(_record_content(record, ("log_entry", "insight", "strategy")))


def _is_visible_episode(record: Dict[str, Any]) -> bool:
    """Lite帰宅の補足や壊れた要約を通常エピソードから除く。"""

    episode_type = str(record.get("type") or "").strip().lower()
    if episode_type == "lite_return_addendum":
        return False
    if "エラーにより要約できませんでした" in str(record.get("summary") or ""):
        return False
    body = str(record.get("summary") or record.get("content") or record.get("log_entry") or "").strip()
    return bool(str(record.get("date") or "").strip() and body)


def _memory_json_response(
    room_id: str,
    note_type: str,
    records: List[Dict[str, Any]],
    paths: List[Path],
    *,
    headings_only: bool,
    heading: str,
    latest: bool,
) -> NoteResponse:
    visible = [record for record in records if _is_real_dream(record)] if note_type == "dream" else [record for record in records if _is_visible_episode(record)]
    visible.sort(key=lambda record: _record_sort_key(record, note_type), reverse=True)
    content_fields = (
        ("log_entry", "insight", "strategy")
        if note_type == "dream"
        else ("summary", "content", "log_entry")
    )
    headings = []
    for record in visible:
        stamp = _record_stamp(record, note_type) or "日付なし"
        preview = _record_preview(record, content_fields)
        headings.append(f"{stamp} - {preview}" if preview else stamp)
    response_headings = headings[:1] if latest else headings

    selected_index: Optional[int] = None
    if latest and visible:
        selected_index = 0
    elif heading:
        for index, (record, label) in enumerate(zip(visible, headings)):
            stamp = _record_stamp(record, note_type)
            if heading == label or heading == stamp or label.startswith(heading) or stamp.startswith(heading):
                selected_index = index
                break

    def render_record(record: Dict[str, Any]) -> str:
        if note_type == "dream":
            return format_dream_journal_entry(record)
        return _record_content(record, content_fields)

    if headings_only:
        content = ""
    elif selected_index is not None:
        content = render_record(visible[selected_index])
    elif heading:
        content = f"指定されたエントリ「{heading}」が見つかりませんでした。"
    else:
        # 全件本文は明示的に headings_only=False で要求された場合だけ返す。
        content = "\n\n".join(render_record(record) for record in visible)

    updated_at = None
    size = 0
    for path in paths:
        try:
            stat = path.stat()
            size += stat.st_size
            candidate = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if updated_at is None or candidate > updated_at:
                updated_at = candidate
        except OSError:
            continue
    title = "夢日記" if note_type == "dream" else "エピソード記憶"
    return NoteResponse(
        room_id=room_id,
        note_type=note_type,
        title=title,
        content=content,
        content_hash=content_hash(content),
        updated_at=updated_at,
        size=size,
        headings=response_headings,
        editable=False,
    )


def _get_json_memory_note(
    room_id: str,
    note_type: str,
    *,
    headings_only: bool,
    heading: str,
    latest: bool,
) -> NoteResponse:
    base = _room_dir(room_id) / "memory"
    if note_type == "dream":
        predicate = _is_real_dream
        records, paths = _read_json_records(
            base / "dreaming",
            base / "insights.json",
            predicate=predicate,
            latest_only=latest and not heading,
        )
    else:
        predicate = _is_visible_episode
        records, paths = _read_json_records(
            base / "episodic",
            base / "episodic_memory.json",
            predicate=predicate,
            latest_only=latest and not heading,
        )
    return _memory_json_response(
        room_id,
        note_type,
        records,
        paths,
        headings_only=headings_only,
        heading=heading,
        latest=latest,
    )


def get_memory_note(
    room_id: str,
    note_type: LiteNoteType,
    *,
    headings_only: bool = False,
    heading: str = "",
    latest: bool = False,
) -> NoteResponse:
    """本体のNoteResponse契約でノート・夢日記・エピソードを読む。

    通常4種のノートは本体 ``service.get_note`` のパーサーとパスを再利用する。
    夢・エピソードは各マネージャーの月次正本をロック付きで読むだけにし、
    Lite側から移行・修復・AI生成などの副作用を起こさない。
    """

    room_id = _require_room(room_id)
    normalized = str(note_type or "").strip().lower()
    heading = str(heading or "").strip()
    if len(heading) > 512:
        raise ValueError("heading is too long")
    if normalized == "episode":
        # 本体のNoteType正本は ``episodic``。Liteクライアントの旧呼称も受ける。
        normalized = "episodic"
    if normalized not in {"research", "creative", "diary", "notepad", "dream", "episodic"}:
        raise ValueError("unsupported memory note type")
    if normalized in {"dream", "episodic"}:
        return _get_json_memory_note(
            room_id,
            normalized,
            headings_only=headings_only,
            heading=heading,
            latest=latest,
        )

    from api import service

    if latest and not heading:
        headings_response = service.get_note(room_id, normalized, headings_only=True, heading="")
        if getattr(headings_response, "headings", None):
            if headings_only:
                if hasattr(headings_response, "model_copy"):
                    return headings_response.model_copy(update={"headings": list(headings_response.headings[:1])})
                headings_response.headings = list(headings_response.headings[:1])
                return headings_response
            selected = service.get_note(
                room_id,
                normalized,
                headings_only=False,
                heading=headings_response.headings[0],
            )
            if hasattr(selected, "model_copy"):
                return selected.model_copy(update={"headings": list(selected.headings[:1])})
            selected.headings = list(getattr(selected, "headings", [])[:1])
            return selected
        return headings_response
    return service.get_note(room_id, normalized, headings_only=headings_only, heading=heading)


def create_food_item(room_id: str, request: LiteFoodItemCreateRequest) -> LiteFoodItemCreateResponse:
    """利用者が入力した情報だけで食べ物アイテムを作る。LLM・画像生成は呼ばない。"""

    room_id = _require_room(room_id)
    _ensure_main_mutation_allowed(room_id)
    for name, value in {
        "taste_profile": request.taste_profile,
        "physical_sensation": request.physical_sensation,
        "synesthesia": request.synesthesia,
    }.items():
        if len(value) > _MAX_ITEM_FIELD_KEYS:
            raise ValueError(f"{name} has too many fields")
        if any(len(str(key)) > 80 for key in value):
            raise ValueError(f"{name} contains an invalid field name")
        try:
            serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} contains unsupported values") from exc
        if len(serialized.encode("utf-8")) > _MAX_ITEM_MAPPING_BYTES:
            raise ValueError(f"{name} is too large")
    if not request.name.strip() or not request.category.strip():
        raise ValueError("name and category are required")
    from src.features.item_manager import ItemManager

    item_data: Dict[str, Any] = {
        "name": request.name.strip(),
        "category": request.category.strip(),
        "amount": request.amount,
        "description": request.description.strip(),
        "flavor_text": request.flavor_text.strip(),
        "taste_profile": dict(request.taste_profile),
        "physical_sensation": dict(request.physical_sensation),
        "synesthesia": dict(request.synesthesia),
    }
    lock = _item_locks.setdefault(room_id, threading.RLock())
    with lock:
        manager = ItemManager(room_id)
        item_id = manager.create_item(item_data, is_user_creator=True, image_path=None)
        persisted = manager.get_item(str(item_id), is_user=True) if item_id else None
    if not item_id or not persisted:
        raise ValueError("failed to save food item")
    return LiteFoodItemCreateResponse(
        room_id=room_id,
        item_id=str(item_id),
        name=item_data["name"],
        amount=request.amount,
    )
