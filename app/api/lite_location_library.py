"""Liteから利用する、名前付き一時的現在地の保存・読取契約。

このモジュールはAI生成や現在地の適用を行わない。利用者が確認した情景と
写真を保存済み地点として管理し、読取時に認証済み画像endpointへ渡せる
room相対参照だけを返す。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote

from pydantic import BaseModel, Field, model_validator

import constants
import lite_travel
from agent.temporary_location_manager import (
    TemporaryLocationConflictError,
    TemporaryLocationImageError,
    TemporaryLocationManager,
)


MAX_LOCATION_NAME_LENGTH = 120
MAX_SCENERY_TEXT_LENGTH = 4_000
MAX_SOURCE_DESCRIPTION_LENGTH = 500
MAX_IMAGE_PATH_LENGTH = 2_000


def _require_nonblank(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


class LiteLocationSaveRequest(BaseModel):
    """確認済みの名前付き地点を保存する要求。"""

    name: str = Field(..., max_length=MAX_LOCATION_NAME_LENGTH)
    scenery_text: str = Field(..., max_length=MAX_SCENERY_TEXT_LENGTH)
    source_description: str = Field(default="", max_length=MAX_SOURCE_DESCRIPTION_LENGTH)
    image_path: Optional[str] = Field(default=None, max_length=MAX_IMAGE_PATH_LENGTH)
    clear_image: bool = False
    overwrite: bool = False

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> "LiteLocationSaveRequest":
        self.name = _require_nonblank(self.name, "name")
        self.scenery_text = _require_nonblank(self.scenery_text, "scenery_text")
        self.source_description = str(self.source_description or "").strip()
        image_path = str(self.image_path or "").strip()
        self.image_path = image_path or None
        if self.clear_image and self.image_path is not None:
            raise ValueError("image_path and clear_image cannot be used together")
        return self


class LiteLocationSummary(BaseModel):
    name: str
    has_image: bool = False


class LiteLocationListResponse(BaseModel):
    room_id: str
    locations: List[LiteLocationSummary] = Field(default_factory=list)


class LiteLocationDetail(BaseModel):
    room_id: str
    location_name: str
    scenery_text: str
    source_description: str = ""
    has_image: bool = False
    image_unavailable: bool = False
    image_path: Optional[str] = None
    image_asset_url: Optional[str] = None
    created_at: str = ""


class LiteLocationSaveResponse(LiteLocationDetail):
    status: Literal["saved"] = "saved"


# 呼び出し側が一般的なresponse名を使う場合にも同じ契約を参照できるようにする。
LiteLocationResponse = LiteLocationDetail


class LiteLocationConflictError(ValueError):
    """同名の保存地点が別内容で存在し、明示上書きが必要。"""

    status_code = 409
    code = "saved_location_conflict"


class LiteLocationPresenceConflictError(LiteLocationConflictError):
    """独立モードの利用中で、保存・生成を実行できない。"""

    code = "presence_locked"


class LiteLocationImageError(ValueError):
    """保存地点の画像を安全なroom assetとして公開できない。"""


def _service() -> Any:
    # serviceは広い依存グラフを持つため、request modelのimport時に初期化しない。
    from api import service

    return service


def _manager() -> TemporaryLocationManager:
    return TemporaryLocationManager()


def _room_root(room_id: str) -> Path:
    return (Path(constants.ROOMS_DIR).expanduser() / room_id).resolve()


def _location_name(value: Any) -> str:
    normalized = _require_nonblank(str(value or ""), "name")
    if len(normalized) > MAX_LOCATION_NAME_LENGTH:
        raise ValueError(f"name exceeds {MAX_LOCATION_NAME_LENGTH} characters")
    # 名前はJSONのキーとしてだけ扱い、ファイル名へ連結しない。
    return normalized


def _saved_image_relative_path(room_id: str, image_path: Any) -> str:
    """保存済み画像を検証し、安全なroom相対pathへ変換する。"""

    raw = str(image_path or "").strip()
    if not raw:
        raise LiteLocationImageError("saved location image is unavailable")
    service = _service()
    room_root = _room_root(room_id)
    candidates = [raw]
    if not Path(raw).is_absolute():
        candidates.append(str(room_root / raw))
    last_error: Optional[Exception] = None
    asset_path: Optional[Path] = None
    for candidate in candidates:
        try:
            asset_path = service.resolve_room_asset_path(room_id, candidate)
            break
        except (ValueError, OSError) as exc:
            last_error = exc
    if asset_path is None:
        raise LiteLocationImageError("saved location image is unavailable") from last_error
    try:
        resolved = asset_path.resolve(strict=True)
        relative = resolved.relative_to(room_root).as_posix()
    except (OSError, ValueError) as exc:
        raise LiteLocationImageError("saved location image is unavailable") from exc
    if not relative or relative == ".":
        raise LiteLocationImageError("saved location image is unavailable")
    return relative


def _image_asset_url(room_id: str, location_name: str) -> str:
    return (
        f"/api/v1/rooms/{quote(room_id, safe='')}/temporary-location/saved-image"
        f"?name={quote(location_name, safe='')}"
    )


def _detail_from_entry(room_id: str, entry: Dict[str, Any], *, status: bool = False) -> LiteLocationDetail:
    name = _location_name(entry.get("location_name") or entry.get("name"))
    raw_image_path = str(entry.get("image_path") or "").strip()
    image_path: Optional[str] = None
    image_asset_url: Optional[str] = None
    image_unavailable = False
    if raw_image_path:
        try:
            image_path = _saved_image_relative_path(room_id, raw_image_path)
        except LiteLocationImageError:
            # 旧PC保存のroom外・欠損画像は本文を返しつつ、元pathを漏らさない。
            image_unavailable = True
        else:
            image_asset_url = _image_asset_url(room_id, name)
    payload = {
        "room_id": room_id,
        "location_name": name,
        # 旧PC保存に上限超過の値が残っていても、Lite応答は契約上限に
        # 切り詰めて返し、写真のunsafe判定以外で全地点を読めなくしない。
        "scenery_text": str(entry.get("scenery_text") or "")[:MAX_SCENERY_TEXT_LENGTH],
        "source_description": str(entry.get("source_description") or "")[:MAX_SOURCE_DESCRIPTION_LENGTH],
        "has_image": image_path is not None,
        "image_unavailable": image_unavailable,
        "image_path": image_path,
        "image_asset_url": image_asset_url,
        "created_at": str(entry.get("created_at") or ""),
    }
    if status:
        return LiteLocationSaveResponse(**payload)
    return LiteLocationDetail(**payload)


def _require_room(room_id: str) -> str:
    room = str(room_id or "").strip()
    if not room or room in {".", ".."} or "/" in room or "\\" in room:
        raise ValueError("valid room_id is required")
    _service()._require_room(room)
    return room


def list_saved_locations(room_id: str) -> LiteLocationListResponse:
    """保存済み地点の名前と画像有無だけを返す。"""

    room = _require_room(room_id)
    manager = _manager()
    locations: List[LiteLocationSummary] = []
    for name in manager.list_saved_locations(room):
        entry = manager.get_saved_location(room, name)
        if not entry:
            continue
        has_image = False
        raw_image_path = str(entry.get("image_path") or "").strip()
        if raw_image_path:
            try:
                _saved_image_relative_path(room, raw_image_path)
            except LiteLocationImageError:
                # 旧JSONのunsafe pathは画像の存在を公開しない。
                has_image = False
            else:
                has_image = True
        locations.append(LiteLocationSummary(name=_location_name(name), has_image=has_image))
    return LiteLocationListResponse(room_id=room, locations=locations)


def get_saved_location(room_id: str, name: str) -> LiteLocationDetail:
    """指定した保存地点を読み取る。現在地・activeは変更しない。"""

    room = _require_room(room_id)
    requested_name = _location_name(name)
    entry = _manager().get_saved_location(room, requested_name)
    if entry is None:
        raise ValueError("saved location was not found")
    return _detail_from_entry(room, entry)


def save_location(room_id: str, request: LiteLocationSaveRequest) -> LiteLocationSaveResponse:
    """確認済みの情景を保存地点へ保存する。

    保存時だけpresence lockを確認する。読取関数は独立モード中も利用できる。
    currentの写真を省略時に参照するが、currentそのものやactiveは変更しない。
    """

    room = _require_room(room_id)
    if not isinstance(request, LiteLocationSaveRequest):
        request = LiteLocationSaveRequest.model_validate(request)
    service = _service()
    manager = _manager()
    with service._get_room_lock(room):
        if lite_travel.is_presence_locked(room):
            raise LiteLocationPresenceConflictError("独立モード利用中は保存できません")
        current = manager.get_current_data(room)
        if request.clear_image:
            image_path = ""
        elif request.image_path is not None:
            try:
                image_path = str(
                    __import__("api.lite_runtime_controls", fromlist=["_validated_room_image_path"])
                    ._validated_room_image_path(room, request.image_path)
                )
            except (ValueError, OSError) as exc:
                raise LiteLocationImageError("image_path is not a safe room image") from exc
        else:
            image_path = str(current.get("image_path") or "").strip()

        snapshot = {
            "location_name": request.name,
            "scenery_text": request.scenery_text,
            "source_description": request.source_description,
            "image_path": image_path,
        }
        try:
            entry = manager.save_location_data(
                room,
                request.name,
                snapshot,
                overwrite=request.overwrite,
            )
        except TemporaryLocationConflictError as exc:
            raise LiteLocationConflictError(str(exc)) from exc
        except TemporaryLocationImageError as exc:
            raise LiteLocationImageError("image could not be saved as a room asset") from exc
    result = _detail_from_entry(room, entry, status=True)
    return result  # type: ignore[return-value]


__all__ = [
    "LiteLocationConflictError",
    "LiteLocationDetail",
    "LiteLocationImageError",
    "LiteLocationListResponse",
    "LiteLocationResponse",
    "LiteLocationSaveRequest",
    "LiteLocationSaveResponse",
    "LiteLocationSummary",
    "get_saved_location",
    "list_saved_locations",
    "save_location",
]
