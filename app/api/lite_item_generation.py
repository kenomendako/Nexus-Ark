"""本体接続Lite向けのアイテム生成・確認済み保存境界。

AI補完は既存の本体ジェネレーター／画像生成器へ委譲し、手入力または生成結果の確認と
ItemManagerへの保存を別のAPI操作として扱う。生成・保存の再送はルーム内receiptで冪等化し、
API Gatewayから秘密値や他ルームのファイルを返さない。
"""

from __future__ import annotations

import copy
import datetime as _datetime
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, model_validator

import config_manager
import constants
from api import lite_features, service
from file_lock_utils import locked_file


logger = logging.getLogger(__name__)

ItemKind = Literal["food", "standard"]
GenerationState = Literal["in_progress", "succeeded", "failed"]
GenerationStatus = Literal["in_progress", "succeeded", "failed", "replayed", "saved"]

_MAX_REFERENCE_IMAGES = 4
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_PROFILE_KEYS = 64
_MAX_PROFILE_BYTES = 64 * 1024
_MAX_RECEIPT_ID = 160
_MAX_PREPARE_CHAT_CONTEXT = 500
_IMAGE_TAG_RE = re.compile(r"\[Generated Image:\s*(.*?)\]", re.DOTALL)
_RECEIPT_STATES = {"in_progress", "succeeded", "failed"}
_RECEIPT_KINDS = {"food", "standard"}


def _require_nonblank(value: str, field_name: str) -> None:
    if not str(value or "").strip():
        raise ValueError(f"{field_name} must not be blank")


class LiteItemProfileGenerateRequest(BaseModel):
    """食べ物／一般アイテムのAIプロファイル生成要求。"""

    kind: ItemKind
    name: str = Field(..., min_length=1, max_length=120)
    category: str = Field(default="", max_length=80)
    base_description: str = Field(default="", max_length=4_000)
    image_path: Optional[str] = Field(default=None, max_length=2_000)
    client_request_id: str = Field(..., min_length=1, max_length=_MAX_RECEIPT_ID)

    @model_validator(mode="after")
    def _validate_required_text(self) -> "LiteItemProfileGenerateRequest":
        _require_nonblank(self.name, "name")
        _require_nonblank(self.client_request_id, "client_request_id")
        return self


class LiteItemImageGenerateRequest(BaseModel):
    """本体の共通画像生成設定を使う画像生成要求。"""

    prompt: str = Field(..., min_length=1, max_length=4_000)
    client_request_id: str = Field(..., min_length=1, max_length=_MAX_RECEIPT_ID)
    reference_image_path: Optional[str] = Field(default=None, max_length=2_000)
    reference_image_paths: List[str] = Field(default_factory=list, max_length=_MAX_REFERENCE_IMAGES)

    @model_validator(mode="after")
    def _merge_reference_image_paths(self) -> "LiteItemImageGenerateRequest":
        _require_nonblank(self.prompt, "prompt")
        _require_nonblank(self.client_request_id, "client_request_id")
        paths: List[str] = []
        if self.reference_image_path:
            paths.append(self.reference_image_path)
        paths.extend(self.reference_image_paths)
        deduped: List[str] = []
        for path in paths:
            normalized = str(path or "").strip()
            if normalized and normalized not in deduped:
                deduped.append(normalized)
        if len(deduped) > _MAX_REFERENCE_IMAGES:
            raise ValueError(f"reference images are limited to {_MAX_REFERENCE_IMAGES}")
        self.reference_image_paths = deduped
        return self


class LiteGeneratedItemSaveRequest(BaseModel):
    """AI補完または手入力で確認したアイテムプロファイルを所持品へ保存する要求。"""

    kind: ItemKind
    name: str = Field(..., min_length=1, max_length=120)
    category: str = Field(..., min_length=1, max_length=80)
    amount: int = Field(default=1, ge=1, le=99)
    profile: Dict[str, Any] = Field(default_factory=dict)
    image_path: Optional[str] = Field(default=None, max_length=2_000)
    base_description: str = Field(default="", max_length=4_000)
    prepare_chat: bool = False
    client_request_id: str = Field(..., min_length=1, max_length=_MAX_RECEIPT_ID)

    @model_validator(mode="after")
    def _validate_required_text(self) -> "LiteGeneratedItemSaveRequest":
        _require_nonblank(self.name, "name")
        _require_nonblank(self.category, "category")
        _require_nonblank(self.client_request_id, "client_request_id")
        return self


class LiteItemGenerationResponse(BaseModel):
    room_id: str
    kind: ItemKind
    status: GenerationStatus
    client_request_id: Optional[str] = None
    replayed: bool = False
    profile: Dict[str, Any] = Field(default_factory=dict)
    image_path: Optional[str] = None
    item: Optional[Dict[str, Any]] = None
    chat_context: str = ""
    provider: Optional[str] = None
    model: Optional[str] = None
    error: Optional[str] = None


def _generation_receipts_path(room_id: str) -> Path:
    return Path(constants.ROOMS_DIR) / room_id / "memory" / "lite_item_generation_receipts.json"


def _atomic_json_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_receipts(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        if path.stat().st_size == 0:
            raise ValueError("Lite item generation receipt is empty")
    except FileNotFoundError as exc:
        raise ValueError("Lite item generation receipt disappeared while reading") from exc
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        raise ValueError("Lite item generation receipt is corrupt") from None
    if not isinstance(loaded, dict):
        raise ValueError("Lite item generation receipt must be an object")

    receipts: Dict[str, Dict[str, Any]] = {}
    for key, value in loaded.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Lite item generation receipt has an invalid request key")
        if not isinstance(value, dict):
            raise ValueError("Lite item generation receipt has an invalid entry")
        if value.get("kind") not in _RECEIPT_KINDS:
            raise ValueError("Lite item generation receipt has an invalid kind")
        if not isinstance(value.get("fingerprint"), str) or not value["fingerprint"].strip():
            raise ValueError("Lite item generation receipt has an invalid fingerprint")
        if value.get("state") not in _RECEIPT_STATES:
            raise ValueError("Lite item generation receipt has an invalid state")
        if not isinstance(value.get("created_at"), str) or not value["created_at"].strip():
            raise ValueError("Lite item generation receipt has no creation timestamp")
        for field in ("profile", "item"):
            if field in value and not isinstance(value[field], dict):
                raise ValueError(f"Lite item generation receipt has an invalid {field}")
        if "chat_context" in value and not isinstance(value["chat_context"], str):
            raise ValueError("Lite item generation receipt has an invalid chat_context")
        receipts[key] = value
    return receipts


def _claim_receipt(room_id: str, request_id: str, fingerprint: str, kind: ItemKind) -> Tuple[Optional[Dict[str, Any]], bool]:
    """receiptを作成し、既存要求ならその記録と新規作成かどうかを返す。"""

    path = _generation_receipts_path(room_id)
    with locked_file(path.as_posix()):
        receipts = _read_receipts(path)
        existing = receipts.get(request_id)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise ValueError("client_request_id was already used with a different request")
            return existing, False
        receipts[request_id] = {
            "kind": kind,
            "fingerprint": fingerprint,
            "state": "in_progress",
            "created_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        }
        _atomic_json_write(path, receipts)
    return None, True


def _finish_receipt(
    room_id: str,
    request_id: str,
    fingerprint: str,
    *,
    state: GenerationState,
    profile: Optional[Dict[str, Any]] = None,
    image_path: Optional[str] = None,
    item: Optional[Dict[str, Any]] = None,
    chat_context: str = "",
    provider: Optional[str] = None,
    model: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    path = _generation_receipts_path(room_id)
    with locked_file(path.as_posix()):
        receipts = _read_receipts(path)
        current = receipts.get(request_id)
        if current is None:
            raise ValueError("Lite item generation receipt claim is missing")
        if current.get("fingerprint") != fingerprint:
            raise ValueError("client_request_id was already used with a different request")
        current.update(
            {
                "state": state,
                "finished_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
                "profile": copy.deepcopy(profile or {}),
                "image_path": image_path,
                "item": copy.deepcopy(item or {}),
                "chat_context": str(chat_context or ""),
                "provider": provider,
                "model": model,
                "error": error,
            }
        )
        receipts[request_id] = current
        _atomic_json_write(path, receipts)
        return current


def _replay_response(
    room_id: str,
    kind: ItemKind,
    request_id: str,
    receipt: Dict[str, Any],
    *,
    replayed: bool = True,
) -> LiteItemGenerationResponse:
    state = str(receipt.get("state") or "failed")
    if state == "in_progress":
        status: GenerationStatus = "in_progress"
    elif state == "succeeded":
        status = "replayed"
    else:
        status = "failed"
    return LiteItemGenerationResponse(
        room_id=room_id,
        kind=kind,
        status=status,
        client_request_id=request_id,
        replayed=replayed,
        profile=dict(receipt.get("profile") or {}),
        image_path=receipt.get("image_path") or None,
        item=dict(receipt.get("item") or {}) or None,
        chat_context=str(receipt.get("chat_context") or ""),
        provider=receipt.get("provider") or None,
        model=receipt.get("model") or None,
        error=receipt.get("error") or None,
    )


def _request_fingerprint(kind: str, payload: Dict[str, Any]) -> str:
    encoded = json.dumps(
        {"kind": kind, **payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _room_lock(room_id: str) -> threading.RLock:
    # アイテム操作APIと同じGatewayルームロックを使い、ItemManagerの
    # load→変更→save区間で相互に更新を失わないようにする。
    return service._get_room_lock(room_id)


def _resolve_room_image(room_id: str, path: Optional[str]) -> Optional[str]:
    normalized_path = str(path or "").strip()
    if not normalized_path:
        return None
    requested = Path(normalized_path).expanduser()
    room_root = (Path(constants.ROOMS_DIR) / room_id).resolve()
    try:
        resolved_requested = requested.resolve()
        resolved_requested.relative_to(room_root)
    except ValueError as exc:
        raise ValueError("asset does not belong to this room") from exc
    if not resolved_requested.exists() or not resolved_requested.is_file():
        raise ValueError("asset not found")
    try:
        size = resolved_requested.stat().st_size
    except OSError as exc:
        raise ValueError("image could not be inspected") from exc
    if size <= 0:
        raise ValueError("image is empty")
    if size > _MAX_IMAGE_BYTES:
        raise ValueError("image is too large")
    try:
        resolved = service.resolve_room_asset_path(room_id, str(requested))
    except ValueError:
        raise
    return str(resolved)


def _resolve_reference_images(room_id: str, paths: List[str]) -> List[str]:
    resolved: List[str] = []
    for path in paths:
        image = _resolve_room_image(room_id, path)
        if image and image not in resolved:
            resolved.append(image)
    return resolved


def _validate_image_readable(path: Optional[str]) -> None:
    """保存前に画像データを読めることを確認し、壊れた入力で所持品を作らない。"""

    if not path:
        return
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
    except Exception as exc:
        raise ValueError("image could not be read") from exc


def _active_gemini_key(room_id: str) -> Tuple[Optional[str], str]:
    """本体のルーム／共通設定から選択中のキー名と値を取得する。"""

    try:
        key_name = config_manager.get_active_gemini_api_key_name(room_id)
    except Exception:
        key_name = None
    key_name = str(key_name or "").strip() or None
    value = config_manager.GEMINI_API_KEYS.get(key_name, "") if key_name else ""
    return key_name, str(value or "")


def _common_gemini_key() -> Tuple[Optional[str], str]:
    """食べ物生成UIと同じ共通Geminiキー選択を返す。"""

    configured = ""
    if isinstance(config_manager.CONFIG_GLOBAL, dict):
        configured = str(config_manager.CONFIG_GLOBAL.get("last_api_key_name") or "").strip()
    key_name = configured or str(config_manager.initial_api_key_name_global or "").strip()
    if not key_name:
        return None, ""
    return key_name, str(config_manager.GEMINI_API_KEYS.get(key_name, "") or "")


def _safe_image_settings() -> Tuple[Optional[str], Optional[str]]:
    try:
        settings = config_manager.load_config_file() or {}
    except Exception:
        return None, None
    provider = str(settings.get("image_generation_provider") or "").strip() or None
    model = str(settings.get("image_generation_model") or "").strip() or None
    if provider == "openai":
        image_settings = settings.get("image_generation_openai_settings") or {}
        if isinstance(image_settings, dict):
            model = str(image_settings.get("model") or model or "").strip() or None
    elif provider == "pollinations":
        model = str(settings.get("image_generation_pollinations_model") or model or "").strip() or None
    elif provider == "huggingface":
        model = str(settings.get("image_generation_huggingface_model") or model or "").strip() or None
    return provider, model


def _validate_profile(profile: Any) -> Dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    if len(profile) > _MAX_PROFILE_KEYS:
        raise ValueError("profile has too many fields")
    try:
        encoded = json.dumps(profile, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        raise ValueError("profile contains unsupported values") from exc
    if len(encoded.encode("utf-8")) > _MAX_PROFILE_BYTES:
        raise ValueError("profile is too large")
    return copy.deepcopy(profile)


def _normalize_generated_profile(kind: ItemKind, profile: Any, request: LiteItemProfileGenerateRequest) -> Dict[str, Any]:
    normalized = _validate_profile(profile)
    if kind == "food" and not isinstance(normalized.get("taste_profile"), dict):
        raise ValueError("food profile requires taste_profile")
    normalized.setdefault("name", request.name.strip())
    if request.category.strip():
        normalized.setdefault("category", request.category.strip())
    if request.base_description.strip():
        normalized.setdefault("description", request.base_description.strip())
    if kind == "food" and "physical_sensation" not in normalized and isinstance(normalized.get("physical"), dict):
        normalized["physical_sensation"] = copy.deepcopy(normalized["physical"])
    if kind == "standard" and "physical" not in normalized and isinstance(normalized.get("physical_sensation"), dict):
        normalized["physical"] = copy.deepcopy(normalized["physical_sensation"])
    return _validate_profile(normalized)


def _extract_generated_image_path(result: Any) -> Optional[str]:
    match = _IMAGE_TAG_RE.search(str(result or ""))
    return match.group(1).strip() if match else None


def _profile_prompt(request: LiteItemProfileGenerateRequest) -> str:
    lines = [f"名前: {request.name.strip()}"]
    if request.category.strip():
        lines.append(f"カテゴリ: {request.category.strip()}")
    if request.base_description.strip():
        lines.append(f"詳細・エピソード: {request.base_description.strip()}")
    return "\n".join(lines)


def generate_item_profile(room_id: str, request: LiteItemProfileGenerateRequest) -> LiteItemGenerationResponse:
    room_id = lite_features._require_room(room_id)
    lite_features._ensure_main_mutation_allowed(room_id)
    image_path = _resolve_room_image(room_id, request.image_path)
    fingerprint = _request_fingerprint(
        request.kind,
        {
            "name": request.name.strip(),
            "category": request.category.strip(),
            "base_description": request.base_description.strip(),
            "image_path": image_path or "",
        },
    )
    receipt_key = f"profile:{request.client_request_id}"
    existing, claimed = _claim_receipt(room_id, receipt_key, fingerprint, request.kind)
    if not claimed and existing is not None:
        return _replay_response(room_id, request.kind, request.client_request_id, existing)

    try:
        if request.kind == "food":
            from src.features._recipe_generator import generate_food_item_profile

            _, api_key = _common_gemini_key()
            if not api_key:
                raise RuntimeError("configured AI key is unavailable")
            generated = generate_food_item_profile(_profile_prompt(request), api_key, image_path=image_path)
        else:
            from src.features._item_desc_generator import generate_standard_item_profile

            generated = generate_standard_item_profile(_profile_prompt(request), image_path=image_path)
        profile = _normalize_generated_profile(request.kind, generated, request)
    except Exception:
        logger.exception("Lite item profile generation failed for room %s", room_id)
        receipt = _finish_receipt(
            room_id,
            receipt_key,
            fingerprint,
            state="failed",
            error="AIによるアイテム情報の生成に失敗しました。",
        )
        return _replay_response(room_id, request.kind, request.client_request_id, receipt, replayed=False)

    receipt = _finish_receipt(
        room_id,
        receipt_key,
        fingerprint,
        state="succeeded",
        profile=profile,
    )
    return LiteItemGenerationResponse(
        room_id=room_id,
        kind=request.kind,
        status="succeeded",
        client_request_id=request.client_request_id,
        profile=dict(receipt.get("profile") or profile),
    )


def generate_item_image(room_id: str, request: LiteItemImageGenerateRequest) -> LiteItemGenerationResponse:
    room_id = lite_features._require_room(room_id)
    lite_features._ensure_main_mutation_allowed(room_id)
    reference_paths = _resolve_reference_images(room_id, request.reference_image_paths)
    fingerprint = _request_fingerprint(
        "image",
        {"prompt": request.prompt.strip(), "reference_image_paths": reference_paths},
    )
    receipt_key = f"image:{request.client_request_id}"
    existing, claimed = _claim_receipt(room_id, receipt_key, fingerprint, "standard")
    if not claimed and existing is not None:
        return _replay_response(room_id, "standard", request.client_request_id, existing)

    key_name, api_key = _active_gemini_key(room_id)
    provider, model = _safe_image_settings()
    try:
        from tools.image_tools import _generate_image_impl

        result = _generate_image_impl(
            prompt=request.prompt.strip(),
            room_name=room_id,
            api_key=api_key,
            api_key_name=key_name,
            save_subdir="item_generated_images",
            reference_image_paths=reference_paths or None,
        )
        generated_path = _extract_generated_image_path(result)
        if not generated_path:
            raise RuntimeError("image generator did not return an image")
        image_path = _resolve_room_image(room_id, generated_path)
        if not image_path:
            raise RuntimeError("generated image path is empty")
    except Exception:
        logger.exception("Lite item image generation failed for room %s", room_id)
        receipt = _finish_receipt(
            room_id,
            receipt_key,
            fingerprint,
            state="failed",
            provider=provider,
            model=model,
            error="アイテム画像の生成に失敗しました。",
        )
        return _replay_response(room_id, "standard", request.client_request_id, receipt, replayed=False)

    receipt = _finish_receipt(
        room_id,
        receipt_key,
        fingerprint,
        state="succeeded",
        image_path=image_path,
        provider=provider,
        model=model,
    )
    return LiteItemGenerationResponse(
        room_id=room_id,
        kind="standard",
        status="succeeded",
        client_request_id=request.client_request_id,
        image_path=receipt.get("image_path") or image_path,
        provider=receipt.get("provider") or provider,
        model=receipt.get("model") or model,
    )


def _item_data_from_profile(request: LiteGeneratedItemSaveRequest) -> Dict[str, Any]:
    profile = _validate_profile(request.profile)
    if request.kind == "food" and not isinstance(profile.get("taste_profile"), dict):
        raise ValueError("food profile requires taste_profile")
    reserved = {"id", "creator", "is_new", "amount", "name", "category", "image_path"}
    item_data: Dict[str, Any] = {
        "name": request.name.strip(),
        "category": request.category.strip(),
        "amount": request.amount,
    }
    description = request.base_description.strip() or str(profile.get("description") or "").strip()
    if description:
        item_data["description"] = description
    for key, value in profile.items():
        if key not in reserved:
            item_data[str(key)] = value
    if request.kind == "food":
        if "physical_sensation" not in item_data and isinstance(item_data.get("physical"), dict):
            item_data["physical_sensation"] = copy.deepcopy(item_data["physical"])
    elif "physical" not in item_data and isinstance(item_data.get("physical_sensation"), dict):
        item_data["physical"] = copy.deepcopy(item_data["physical_sensation"])
    return item_data


def _prepare_chat_context(request: LiteGeneratedItemSaveRequest, item_data: Dict[str, Any]) -> str:
    """共有する場合だけ、説明欄の短い本文を返す。感覚値やメタデータは送信しない。"""

    if not request.prepare_chat:
        return ""
    candidates = (
        request.base_description,
        item_data.get("description"),
        item_data.get("flavor_text"),
    )
    parts: List[str] = []
    for value in candidates:
        if not isinstance(value, str):
            continue
        normalized = " ".join(value.split())
        if normalized and normalized not in parts:
            parts.append(normalized)
    return "\n".join(parts)[:_MAX_PREPARE_CHAT_CONTEXT].rstrip()


def _persisted_image_path(room_id: str, persisted: Dict[str, Any]) -> Optional[str]:
    """ItemManagerが正規化して保存した画像をルーム内の実ファイルとして確認する。"""

    raw_path = str(persisted.get("image_path") or "").strip()
    if not raw_path:
        return None
    room_root = (Path(constants.ROOMS_DIR) / room_id).resolve()
    try:
        resolved = Path(raw_path).resolve()
        resolved.relative_to(room_root)
    except (OSError, ValueError) as exc:
        raise ValueError("saved image does not belong to this room") from exc
    if not resolved.is_file():
        raise ValueError("saved image was not written")
    try:
        if resolved.stat().st_size <= 0:
            raise ValueError("saved image is empty")
    except OSError as exc:
        raise ValueError("saved image could not be inspected") from exc
    return str(resolved)


def _rollback_created_item(manager: Any, item_id: Optional[str]) -> None:
    """保存後の画像検証に失敗したとき、作成途中の所持品を残さない。"""

    if not manager or not item_id:
        return
    try:
        manager.delete_item(str(item_id), is_user=True)
        if manager.get_item(str(item_id), is_user=True) is not None:
            logger.error("Lite item rollback left an inventory item: %s", item_id)
    except Exception:
        logger.exception("Lite item rollback failed for %s", item_id)


def save_generated_item(room_id: str, request: LiteGeneratedItemSaveRequest) -> LiteItemGenerationResponse:
    """AI補完または手入力で確認したアイテムを、会話副作用なしで所持品へ保存する。"""

    room_id = lite_features._require_room(room_id)
    lite_features._ensure_main_mutation_allowed(room_id)
    image_path = _resolve_room_image(room_id, request.image_path)
    _validate_image_readable(image_path)
    item_data = _item_data_from_profile(request)
    chat_context = _prepare_chat_context(request, item_data)
    fingerprint = _request_fingerprint(
        "save",
        {
            "name": request.name.strip(),
            "category": request.category.strip(),
            "amount": request.amount,
            "profile": request.profile,
            "image_path": image_path or "",
            "base_description": request.base_description.strip(),
            # 既存UIの共有を要求しない保存receiptと同じfingerprintを保つ。
            **({"prepare_chat": True} if request.prepare_chat else {}),
        },
    )
    receipt_key = f"save:{request.client_request_id}"
    existing, claimed = _claim_receipt(room_id, receipt_key, fingerprint, request.kind)
    if not claimed and existing is not None:
        return _replay_response(room_id, request.kind, request.client_request_id, existing)
    item_id: Optional[str] = None
    persisted: Optional[Dict[str, Any]] = None
    manager = None
    try:
        with _room_lock(room_id):
            from src.features.item_manager import ItemManager

            manager = ItemManager(room_id)
            try:
                item_id = manager.create_item(item_data, is_user_creator=True, image_path=image_path)
                persisted = manager.get_item(str(item_id), is_user=True) if item_id else None
                if not item_id or not persisted:
                    raise RuntimeError("アイテムの保存結果を確認できませんでした。")
                if image_path:
                    normalized_image_path = _persisted_image_path(room_id, persisted)
                    if not normalized_image_path:
                        raise RuntimeError("指定したアイテム画像が保存されませんでした。")
                    persisted = dict(persisted)
                    persisted["image_path"] = normalized_image_path
            except Exception:
                _rollback_created_item(manager, item_id or str(item_data.get("id") or "") or None)
                raise
    except Exception:
        logger.exception("Lite generated item save failed for room %s", room_id)
        receipt = _finish_receipt(
            room_id,
            receipt_key,
            fingerprint,
            state="failed",
            error="アイテムの保存に失敗しました。",
        )
        return _replay_response(room_id, request.kind, request.client_request_id, receipt, replayed=False)
    receipt = _finish_receipt(
        room_id,
        receipt_key,
        fingerprint,
        state="succeeded",
        image_path=persisted.get("image_path") or None,
        item=dict(persisted),
        chat_context=chat_context,
    )
    return LiteItemGenerationResponse(
        room_id=room_id,
        kind=request.kind,
        status="saved",
        client_request_id=request.client_request_id,
        item=dict(receipt.get("item") or persisted),
        image_path=receipt.get("image_path") or persisted.get("image_path") or None,
        chat_context=str(receipt.get("chat_context") or chat_context),
    )
