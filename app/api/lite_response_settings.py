"""Lite PWA向けのモデル・キー・ローテーション設定サービス。

このサービスは既存の共通設定とルーム個別設定を一つの読み取りモデルに
まとめる。APIキー本体は境界を越えず、保存時はルーム設定managerの既存ロック・
バックアップ・アトミック保存を通して、古いスマホ画面による巻き戻しを防ぐ。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, model_validator
from file_lock_utils import locked_file

import config_manager
import constants
import room_manager
from api import lite_features


logger = logging.getLogger(__name__)

_REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
_GOOGLE_MODEL_PREFIXES = ("gemini", "learnlm", "gemma")
_SPECIAL_MODEL_MARKERS = (
    "embedding",
    "tts",
    "speech",
    "audio",
    "computer",
    "customtools",
    "robotics",
)


class LiteResponseSettingsKeyOption(BaseModel):
    """表示可能なキー情報。キー本体はこのモデルに含めない。"""

    name: str
    paid: bool = False
    provider: str = "google"


class LiteResponseSettingsResponse(BaseModel):
    room_id: str
    scope: str = "common"
    effective_provider: str = "google"
    effective_model_name: str
    effective_key_name: Optional[str] = None
    effective_rotation: bool = True
    common_provider: str = "google"
    common_model_name: str
    common_key_name: Optional[str] = None
    common_rotation: bool = True
    keys: List[LiteResponseSettingsKeyOption] = Field(default_factory=list)
    models: List[str] = Field(default_factory=list)
    room_key_name: Optional[str] = None
    room_model_name: Optional[str] = None
    room_rotation: Optional[bool] = None
    revision: str
    supported: bool = True


class LiteResponseSettingsUpdateRequest(BaseModel):
    key_name: str = Field(..., min_length=1, max_length=120)
    model_name: str = Field(..., min_length=1, max_length=240)
    enable_api_key_rotation: Optional[bool] = None
    base_revision: str = Field(..., min_length=64, max_length=64)

    @model_validator(mode="after")
    def _normalize_values(self) -> "LiteResponseSettingsUpdateRequest":
        self.key_name = self.key_name.strip()
        self.model_name = self.model_name.strip()
        self.base_revision = self.base_revision.strip().lower()
        if not self.key_name or not self.model_name:
            raise ValueError("key_name and model_name are required")
        if not _REVISION_RE.fullmatch(self.base_revision):
            raise ValueError("base_revision must be a 64-character hexadecimal digest")
        return self


class LiteResponseSettingsDeleteRequest(BaseModel):
    base_revision: str = Field(..., min_length=64, max_length=64)

    @model_validator(mode="after")
    def _normalize_revision(self) -> "LiteResponseSettingsDeleteRequest":
        self.base_revision = self.base_revision.strip().lower()
        if not _REVISION_RE.fullmatch(self.base_revision):
            raise ValueError("base_revision must be a 64-character hexadecimal digest")
        return self


class LiteResponseSettingsConflictError(ValueError):
    """保存前に共通設定または対象ルームが変更されていた。"""

    code = "response_settings_conflict"


def _memory_config() -> Dict[str, Any]:
    value = getattr(config_manager, "CONFIG_GLOBAL", {})
    return value if isinstance(value, dict) else {}


def _disk_config() -> Dict[str, Any]:
    """現在のconfig.jsonを読む。失敗時はメモリ設定へ静かにフォールバックする。"""

    try:
        value = config_manager.load_config_file()
    except Exception as exc:
        logger.warning("共通設定の読込に失敗しました: %s", exc)
        return {}
    return value if isinstance(value, dict) else {}


def _config_value(disk: Dict[str, Any], memory: Dict[str, Any], key: str, default: Any = None) -> Any:
    if key in disk:
        return disk.get(key)
    if key in memory:
        return memory.get(key)
    return default


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _provider(value: Any) -> Optional[str]:
    normalized = _text(value)
    valid = getattr(config_manager, "VALID_ROOM_PROVIDERS", set())
    return normalized.lower() if normalized and normalized.lower() in valid else None


def _canonical_model_name(value: Any) -> Optional[str]:
    normalized = _text(value)
    if not normalized:
        return None
    return normalized.removeprefix("models/")


def _is_google_response_model(value: Any) -> bool:
    model_name = _canonical_model_name(value)
    if not model_name:
        return False
    lowered = model_name.lower()
    if not lowered.startswith(_GOOGLE_MODEL_PREFIXES):
        return False
    if any(marker in lowered for marker in _SPECIAL_MODEL_MARKERS):
        return False
    try:
        return not bool(config_manager.is_image_generation_model(model_name))
    except Exception:
        return "image" not in lowered


def _cached_google_models(disk: Dict[str, Any], memory: Dict[str, Any]) -> List[str]:
    raw_models = _config_value(disk, memory, "available_models", [])
    if not isinstance(raw_models, (list, tuple)):
        return []
    result: List[str] = []
    seen = set()
    for value in raw_models:
        model_name = _canonical_model_name(value)
        if not model_name or model_name in seen or not _is_google_response_model(model_name):
            continue
        seen.add(model_name)
        result.append(model_name)
    return result


def _key_options(disk: Dict[str, Any], memory: Dict[str, Any]) -> List[LiteResponseSettingsKeyOption]:
    names = set()
    key_sources = []
    if "gemini_api_keys" in disk:
        key_sources.append(disk.get("gemini_api_keys"))
    elif "gemini_api_keys" in memory:
        key_sources.append(memory.get("gemini_api_keys"))
    else:
        key_sources.append(getattr(config_manager, "GEMINI_API_KEYS", {}))
    for source in key_sources:
        if isinstance(source, dict):
            for name, value in source.items():
                if _text(name) and isinstance(value, str) and value.strip() and not value.startswith("YOUR_API_KEY"):
                    names.add(str(name).strip())

    raw_paid = _config_value(disk, memory, "paid_api_key_names", [])
    paid_names = {
        str(name).strip()
        for name in raw_paid
        if _text(name)
    } if isinstance(raw_paid, (list, tuple, set)) else set()
    return [
        LiteResponseSettingsKeyOption(name=name, paid=name in paid_names)
        for name in sorted(names)
    ]


def _common_state(
    disk: Dict[str, Any], memory: Dict[str, Any]
) -> Tuple[str, str, Optional[str], bool, List[LiteResponseSettingsKeyOption], List[str]]:
    provider = _provider(_config_value(disk, memory, "active_provider", "google")) or "google"
    model_name = _current_global_model_from_snapshot(disk, memory)
    keys = _key_options(disk, memory)
    configured_key = _text(_config_value(disk, memory, "last_api_key_name", None))
    key_names = {option.name for option in keys}
    key_name = configured_key if configured_key in key_names else (keys[0].name if keys else None)
    raw_rotation = _config_value(disk, memory, "enable_api_key_rotation", True)
    rotation = raw_rotation if isinstance(raw_rotation, bool) else True
    return provider, model_name, key_name, rotation, keys, _cached_google_models(disk, memory)


def _current_global_model_from_snapshot(disk: Dict[str, Any], memory: Dict[str, Any]) -> str:
    """``get_current_global_model``のディスクスナップショット版。"""

    # config_manager.get_current_global_model()と同じく、設定ファイルが
    # 存在する場合はその一つのsnapshotだけを正本として扱う。テストや初回
    # 起動でファイルがない場合だけ、ロード済みメモリ設定へフォールバックする。
    source = disk if disk else memory
    available_models = source.get("available_models", [])
    last_model = source.get("last_model")
    if last_model and isinstance(available_models, (list, tuple)) and last_model in available_models:
        return _canonical_model_name(last_model) or str(last_model)
    default_model = source.get("default_model", getattr(config_manager, "DEFAULT_MODEL_GLOBAL", ""))
    return _canonical_model_name(default_model) or "gemini-3.1-flash-lite"


def _read_room_config(room_id: str) -> Dict[str, Any]:
    config = room_manager.get_room_config(room_id)
    if not isinstance(config, dict):
        raise ValueError("Room settings could not be read")
    return config


def _room_state(room_config: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[bool], Optional[str], Dict[str, Any]]:
    overrides = room_config.get("override_settings", {})
    if not isinstance(overrides, dict):
        overrides = {}
    raw_provider = _text(overrides.get("provider"))
    provider = _provider(raw_provider)
    room_key = _text(overrides.get("api_key_name"))
    room_model = _canonical_model_name(overrides.get("model_name"))
    room_rotation = overrides.get("enable_api_key_rotation")
    if not isinstance(room_rotation, bool):
        room_rotation = None
    return provider, room_key, room_rotation, room_model, overrides


def _revision(
    room_id: str,
    common_provider: str,
    common_model: str,
    common_key: Optional[str],
    common_rotation: bool,
    room_config: Dict[str, Any],
    keys: List[LiteResponseSettingsKeyOption],
    models: List[str],
) -> str:
    provider, room_key, room_rotation, room_model, overrides = _room_state(room_config)
    payload = {
        "room_id": room_id,
        "common": {
            "provider": common_provider,
            "model": common_model,
            "key": common_key,
            "rotation": common_rotation,
        },
        "room": {
            "provider": provider,
            "provider_raw": _text(overrides.get("provider")),
            "model": room_model,
            "key": room_key,
            "rotation": room_rotation,
        },
        "keys": [option.model_dump() for option in keys],
        "models": models,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_response(room_id: str, room_config: Dict[str, Any]) -> LiteResponseSettingsResponse:
    disk = _disk_config()
    memory = _memory_config()
    common_provider, common_model, common_key, common_rotation, keys, models = _common_state(disk, memory)
    room_provider, room_key, room_rotation, room_model, overrides = _room_state(room_config)

    effective_provider = room_provider or common_provider
    room_google = room_provider == "google"
    effective_model = common_model
    if room_provider is not None and room_model:
        # 本体は明示providerのroom modelをcache有無に関係なく使用する。
        # cacheから消えた現行モデルでも、キー・rotationだけを更新できるよう
        # response側で現在モデルを表示候補へ追加する。
        effective_model = room_model
    effective_key = common_key
    if room_google and room_key and room_key in {option.name for option in keys}:
        effective_key = room_key
    if effective_provider != "google":
        effective_key = None
    effective_rotation = room_rotation if room_google and room_rotation is not None else common_rotation
    scope = "room" if room_provider is not None else "common"
    supported = effective_provider == "google" and _is_google_response_model(effective_model)
    display_models = list(models)
    if supported and effective_model not in display_models:
        display_models.append(effective_model)
    revision = _revision(
        room_id,
        common_provider,
        common_model,
        common_key,
        common_rotation,
        room_config,
        keys,
        display_models,
    )
    return LiteResponseSettingsResponse(
        room_id=room_id,
        scope=scope,
        effective_provider=effective_provider,
        effective_model_name=effective_model,
        effective_key_name=effective_key,
        effective_rotation=effective_rotation,
        common_provider=common_provider,
        common_model_name=common_model,
        common_key_name=common_key,
        common_rotation=common_rotation,
        keys=keys,
        models=display_models,
        room_key_name=room_key,
        room_model_name=room_model,
        room_rotation=room_rotation,
        revision=revision,
        supported=supported,
    )


def _validate_base_revision(base_revision: str) -> str:
    normalized = str(base_revision or "").strip().lower()
    if not _REVISION_RE.fullmatch(normalized):
        raise ValueError("base_revision must be a 64-character hexadecimal digest")
    return normalized


def _require_google_state(response: LiteResponseSettingsResponse, *, include_common: bool = False) -> None:
    if response.effective_provider != "google" or not response.supported:
        raise ValueError("Google response settings are unavailable for the current provider or model")
    if include_common and response.common_provider != "google":
        raise ValueError("Google response settings cannot restore a non-Google common provider")


def _mutate_room(
    room_id: str,
    base_revision: str,
    override_updates: Dict[str, Any],
) -> LiteResponseSettingsResponse:
    room_id = lite_features._require_room(room_id)
    lite_features._ensure_main_mutation_allowed(room_id)
    expected_revision = _validate_base_revision(base_revision)
    # 既存room保存と同じroom→config順。共通設定の保存も版比較から
    # room保存完了まで排他し、比較直後の共通変更を取りこぼさない。
    with room_manager.room_config_write_guard(room_id), locked_file(
        constants.CONFIG_FILE, timeout=config_manager.CONFIG_LOCK_TIMEOUT,
    ):
        room_config = _read_room_config(room_id)
        current = _build_response(room_id, room_config)
        if current.revision != expected_revision:
            raise LiteResponseSettingsConflictError("response settings changed; reload before saving")
        result = room_manager.update_room_config(
            room_id,
            {"override_settings": dict(override_updates)},
        )
        if result is False:
            raise ValueError("room response settings could not be saved")
        return _build_response(room_id, _read_room_config(room_id))


def get_response_settings(room_id: str) -> LiteResponseSettingsResponse:
    """指定ルームの実効設定と保存済みドラフトを返す。"""

    room_id = lite_features._require_room(room_id)
    return _build_response(room_id, _read_room_config(room_id))


def update_response_settings(
    room_id: str,
    request: LiteResponseSettingsUpdateRequest,
) -> LiteResponseSettingsResponse:
    """Googleのキー・モデル・ローテーションをルーム単位で保存する。"""

    room_id = lite_features._require_room(room_id)
    current = get_response_settings(room_id)
    _require_google_state(current)
    selected_key = request.key_name.strip()
    selected_model = _canonical_model_name(request.model_name)
    allowed_keys = {option.name for option in current.keys}
    if selected_key not in allowed_keys:
        raise ValueError("registered API key was not found")
    if not selected_model or selected_model not in current.models:
        raise ValueError("Google response model was not found in the cached model list")

    return _mutate_room(
        room_id,
        request.base_revision,
        {
            "provider": "google",
            "api_key_name": selected_key,
            "model_name": selected_model,
            "enable_api_key_rotation": request.enable_api_key_rotation,
        },
    )


def reset_response_settings(
    room_id: str,
    request: LiteResponseSettingsDeleteRequest,
) -> LiteResponseSettingsResponse:
    """プロバイダだけを共通へ戻し、ルームのキー・モデル・回転ドラフトを保持する。"""

    room_id = lite_features._require_room(room_id)
    current = get_response_settings(room_id)
    _require_google_state(current, include_common=True)

    return _mutate_room(room_id, request.base_revision, {"provider": None})


__all__ = [
    "LiteResponseSettingsConflictError",
    "LiteResponseSettingsDeleteRequest",
    "LiteResponseSettingsKeyOption",
    "LiteResponseSettingsResponse",
    "LiteResponseSettingsUpdateRequest",
    "get_response_settings",
    "reset_response_settings",
    "update_response_settings",
]
