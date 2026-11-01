"""本体接続Liteの情景文生成。生成結果は下書きで、現在地は変更しない。"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageOps
from pydantic import BaseModel, Field, model_validator

import constants
from api import lite_features, lite_runtime_controls
from api.lite_location_library import LiteLocationPresenceConflictError
from file_lock_utils import locked_file

logger = logging.getLogger(__name__)


class LiteLocationGenerateRequest(BaseModel):
    image_path: str = Field(..., min_length=1, max_length=4096)
    user_hint: str = Field(default="", max_length=500)
    client_request_id: str = Field(..., min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_nonblank(self):
        if not self.image_path.strip() or not self.client_request_id.strip():
            raise ValueError("image_path and client_request_id must not be blank")
        return self


class LiteLocationGenerationResponse(BaseModel):
    room_id: str
    status: Literal["in_progress", "succeeded", "replayed", "failed"]
    client_request_id: str
    replayed: bool = False
    scenery_text: str = ""
    source_description: str = ""
    image_path: str = ""
    error: str | None = None


def _receipt_path(room_id: str) -> Path:
    room_root = (Path(constants.ROOMS_DIR) / room_id).resolve()
    path = room_root / "cache" / "lite_location_generation_receipts.json"
    try:
        path.parent.resolve().relative_to(room_root)
    except ValueError:
        raise ValueError("location receipt directory is outside this room") from None
    if path.is_symlink():
        raise ValueError("location receipt must not be a symlink")
    return path


def _read_receipts(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("location receipt must not be a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("location generation receipts cannot be read") from None
    if not isinstance(value, dict):
        raise ValueError("location generation receipts are invalid")
    for key, row in value.items():
        if not isinstance(key, str) or not isinstance(row, dict):
            raise ValueError("location generation receipt is invalid")
        if row.get("state") not in {"in_progress", "succeeded", "failed"}:
            raise ValueError("location generation receipt state is invalid")
        if not isinstance(row.get("fingerprint"), str) or len(row["fingerprint"]) != 64:
            raise ValueError("location generation receipt fingerprint is invalid")
        for field, limit in (("scenery_text", 4000), ("source_description", 500), ("image_path", 4096)):
            field_value = row.get(field, "")
            if not isinstance(field_value, str) or len(field_value) > limit:
                raise ValueError("location generation receipt payload is invalid")
        image_path = Path(row.get("image_path", ""))
        if image_path.is_absolute() or ".." in image_path.parts:
            raise ValueError("location generation receipt image path is invalid")
        if row.get("error") is not None and not isinstance(row["error"], str):
            raise ValueError("location generation receipt error is invalid")
    return value


def _write_receipts(path: Path, receipts: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("location receipt must not be a symlink")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(receipts, file, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _response(room_id: str, request_id: str, row: dict, *, replayed: bool):
    state = row["state"]
    status = "replayed" if replayed and state == "succeeded" else state
    return LiteLocationGenerationResponse(
        room_id=room_id, status=status, client_request_id=request_id, replayed=replayed,
        scenery_text=row.get("scenery_text", ""), source_description=row.get("source_description", ""),
        image_path=row.get("image_path", ""), error=row.get("error"),
    )


def _generate_draft(photo: bytes, hint: str, room_id: str) -> str:
    """画像・ヒントの不変snapshotから、既存processing設定で文だけを作る。"""
    from langchain_core.messages import HumanMessage
    from llm_factory import LLMFactory

    with Image.open(io.BytesIO(photo)) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((1024, 1024))
        image = image.convert("RGB")
        encoded = io.BytesIO()
        image.save(encoded, format="JPEG", quality=85)
    prompt = (
        "添付写真から、ペルソナへ今いる場所を伝える日本語の情景文を3〜5文で作成してください。"
        "写真に見える建物・自然・周囲の配置、光や天候、雰囲気を具体的に描写してください。"
        "人物の会話・行動や思考過程は書かず、見えない事実を断定しないでください。"
        "ユーザーの補足がある場合は場所名や状況として考慮し、情景文だけを返してください。"
    )
    if hint:
        prompt += "\nユーザーからの補足情報:\n" + hint
    message = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")}},
    ])
    result, _key = LLMFactory.invoke_internal_llm(
        internal_role="processing", prompt=[message], room_name=room_id,
    )
    content = getattr(result, "content", result)
    if isinstance(content, list):
        content = "\n".join(
            part if isinstance(part, str) else str(part.get("text", ""))
            for part in content
            if isinstance(part, str) or (
                isinstance(part, dict) and part.get("type") == "text" and not part.get("thought")
            )
        )
    if not isinstance(content, str) or not content.strip():
        raise ValueError("AI returned no scenery text")
    text = content.strip()
    if len(text) > lite_runtime_controls.MAX_SCENERY_TEXT_LENGTH:
        raise ValueError("AI scenery text is too long")
    return text


def generate_location(room_id: str, request: LiteLocationGenerateRequest) -> LiteLocationGenerationResponse:
    room_id = lite_features._require_room(room_id)
    try:
        lite_features._ensure_main_mutation_allowed(room_id)
    except ValueError as exc:
        raise LiteLocationPresenceConflictError(str(exc)) from exc
    path = Path(lite_runtime_controls._validated_room_image_path(room_id, request.image_path))
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("photo exceeds 8MB")
    photo = path.read_bytes()
    if len(photo) > 8 * 1024 * 1024:
        raise ValueError("photo exceeds 8MB")
    relative = path.resolve().relative_to((Path(constants.ROOMS_DIR) / room_id).resolve()).as_posix()
    hint = request.user_hint.strip()
    payload = {"image_path": relative, "photo_sha256": hashlib.sha256(photo).hexdigest(), "user_hint": hint}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    request_id = request.client_request_id.strip()
    receipt_path = _receipt_path(room_id)
    with locked_file(str(receipt_path)):
        receipts = _read_receipts(receipt_path)
        existing = receipts.get(request_id)
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("client_request_id was already used with a different request")
            return _response(room_id, request_id, existing, replayed=True)
        receipts[request_id] = {
            "fingerprint": fingerprint, "state": "in_progress", "image_path": relative,
            "source_description": hint, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        _write_receipts(receipt_path, receipts)
    try:
        text = _generate_draft(photo, hint, room_id)
        outcome = {"state": "succeeded", "scenery_text": text}
    except Exception as exc:
        # SDK例外の秘密値・URLをAPI responseや通常ログへ転載しない。
        logger.warning("Lite scenery generation failed (%s)", type(exc).__name__)
        outcome = {"state": "failed", "error": "AI情景文を生成できませんでした。生成設定と接続を確認してください。"}
    with locked_file(str(receipt_path)):
        receipts = _read_receipts(receipt_path)
        row = receipts.get(request_id)
        if not row or row["fingerprint"] != fingerprint:
            raise ValueError("location generation receipt changed during generation")
        row.update(outcome, finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        _write_receipts(receipt_path, receipts)
    return _response(room_id, request_id, row, replayed=False)
