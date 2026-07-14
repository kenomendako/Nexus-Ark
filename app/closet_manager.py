"""Closet profile persistence for persona appearance and outfit state."""

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import constants
import room_manager


PROFILE_FILENAME = "persona_profile.json"
CLOSET_FILENAME = "persona_closet.json"
USER_COMMON_DIR = "user_closet"
USER_PROFILE_FILENAME = "profile.json"
USER_CATALOG_FILENAME = "catalog.json"
USER_ROOM_PROFILE_FILENAME = "user_profile.json"
USER_ROOM_CLOSET_FILENAME = "user_closet.json"
USER_CLOSET_USE_COMMON_KEY = "user_closet_use_common"
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
CLOSET_PARTS = ("トップス", "ボトムス", "ワンピース", "アウター", "靴", "アクセサリー", "髪型", "その他")
CLOSET_SOURCES = ("real", "generated")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_profile() -> Dict[str, Any]:
    return {
        "enabled": False,
        "base": {
            "description": "",
            "reference_images": [],
        },
        "current": {
            "note": "",
            "worn": [],
            "appearance_image": "",
            "appearance_generated_at": "",
            "appearance_source_updated_at": "",
            "appearance_source_signature": "",
        },
        "updated_at": "",
    }


def _default_catalog() -> Dict[str, Any]:
    return {"items": []}


def _safe_room_name(room_name: str) -> str:
    room_name = str(room_name or "").strip()
    if not room_name:
        raise ValueError("room_name が必要です。")
    if ".." in room_name or "/" in room_name or "\\" in room_name:
        raise ValueError("不正な room_name です。")
    return room_name


def _room_dir(room_name: str) -> Path:
    return Path(constants.ROOMS_DIR) / _safe_room_name(room_name)


def _closet_dir(room_name: str) -> Path:
    return _room_dir(room_name) / "closet"


def _profile_path(room_name: str) -> Path:
    return _closet_dir(room_name) / PROFILE_FILENAME


def _image_dir(room_name: str) -> Path:
    return _closet_dir(room_name) / "images"


def _catalog_path(room_name: str) -> Path:
    return _closet_dir(room_name) / CLOSET_FILENAME


def _user_common_dir() -> Path:
    return Path(USER_COMMON_DIR)


def _user_common_image_dir() -> Path:
    return _user_common_dir() / "images"


def _validate_user_scope(scope: str) -> str:
    scope = str(scope or "").strip()
    if scope not in {"common", "room"}:
        raise ValueError('scope は "common" または "room" を指定してください。')
    return scope


def _user_profile_path(scope: str, room_name: str = "") -> Path:
    scope = _validate_user_scope(scope)
    if scope == "common":
        return _user_common_dir() / USER_PROFILE_FILENAME
    return _closet_dir(_safe_room_name(room_name)) / USER_ROOM_PROFILE_FILENAME


def _user_catalog_path(scope: str, room_name: str = "") -> Path:
    scope = _validate_user_scope(scope)
    if scope == "common":
        return _user_common_dir() / USER_CATALOG_FILENAME
    return _closet_dir(_safe_room_name(room_name)) / USER_ROOM_CLOSET_FILENAME


def _user_image_dir(scope: str, room_name: str = "") -> Path:
    scope = _validate_user_scope(scope)
    if scope == "common":
        return _user_common_image_dir()
    return _image_dir(_safe_room_name(room_name))


def _normalize_reference_images(reference_images) -> List[str]:
    if not reference_images:
        return []
    if isinstance(reference_images, str):
        items = [line.strip() for line in reference_images.splitlines()]
    else:
        items = [str(item or "").strip() for item in reference_images]
    result = []
    for item in items:
        if not item or item in result:
            continue
        if ".." in Path(item).parts:
            continue
        result.append(item)
    return result


def _normalize_part(part: str) -> str:
    part = str(part or "").strip()
    return part if part in CLOSET_PARTS else "その他"


def _normalize_tags(tags) -> List[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        items = [item.strip() for item in tags.replace("\n", ",").split(",")]
    else:
        items = [str(item or "").strip() for item in tags]
    result = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def _normalize_closet_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    item_id = str(item.get("id") or "").strip()
    if not item_id:
        return None
    source = str(item.get("source") or "generated").strip()
    if source not in CLOSET_SOURCES:
        source = "generated"
    return {
        "id": item_id,
        "name": str(item.get("name") or "名称未設定").strip() or "名称未設定",
        "part": _normalize_part(item.get("part")),
        "description": str(item.get("description") or "").strip(),
        "reference_image": _normalize_reference_images([item.get("reference_image")])[0] if item.get("reference_image") else "",
        "source": source,
        "linked_item_id": str(item.get("linked_item_id") or "").strip(),
        "tags": _normalize_tags(item.get("tags")),
        "created_at": str(item.get("created_at") or ""),
    }


def _normalize_catalog(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return _default_catalog()
    items = []
    seen = set()
    for item in data.get("items") or []:
        normalized = _normalize_closet_item(item)
        if not normalized or normalized["id"] in seen:
            continue
        seen.add(normalized["id"])
        items.append(normalized)
    return {"items": items}


def _valid_closet_ids(room_name: str) -> set:
    return {item.get("id") for item in list_closet_items(room_name)}


def _normalize_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    profile = _default_profile()
    if not isinstance(data, dict):
        return profile
    base = data.get("base") if isinstance(data.get("base"), dict) else {}
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    profile["enabled"] = bool(data.get("enabled", False))
    profile["base"]["description"] = str(base.get("description") or "")
    profile["base"]["reference_images"] = _normalize_reference_images(base.get("reference_images"))
    valid_ids = set(data.get("_valid_closet_ids") or [])
    if not valid_ids and data.get("_room_name"):
        valid_ids = _valid_closet_ids(data.get("_room_name", ""))
    worn = []
    for closet_id in current.get("worn") or []:
        closet_id = str(closet_id or "").strip()
        if closet_id and closet_id not in worn and (not valid_ids or closet_id in valid_ids):
            worn.append(closet_id)
    profile["current"]["note"] = str(current.get("note") or "")
    profile["current"]["worn"] = worn
    profile["current"]["appearance_image"] = str(current.get("appearance_image") or "").strip()
    profile["current"]["appearance_generated_at"] = str(current.get("appearance_generated_at") or "")
    profile["current"]["appearance_source_updated_at"] = str(current.get("appearance_source_updated_at") or "")
    profile["current"]["appearance_source_signature"] = str(current.get("appearance_source_signature") or "")
    profile["updated_at"] = str(data.get("updated_at") or "")
    return profile


def load_persona_profile(room_name: str) -> Dict[str, Any]:
    """Load a persona closet profile, returning a default profile when missing."""
    room_name = _safe_room_name(room_name)
    path = _profile_path(room_name)
    if not path.exists():
        return _default_profile()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data["_room_name"] = room_name
        return _normalize_profile(data)
    except (json.JSONDecodeError, OSError):
        return _default_profile()


def save_persona_profile(room_name: str, enabled, description, reference_images) -> Dict[str, Any]:
    """
    Save the base appearance profile.

    Empty UI values do not erase a previously saved description or reference image list.
    This protects saved settings from startup/light-load events that still hold blank
    component values.
    """
    room_name = _safe_room_name(room_name)
    room_manager.ensure_room_files(room_name)
    current = load_persona_profile(room_name)

    description_text = str(description or "").strip()
    next_images = _normalize_reference_images(reference_images)

    if description_text:
        current["base"]["description"] = description_text
    elif not current["base"].get("description"):
        current["base"]["description"] = ""

    if next_images:
        current["base"]["reference_images"] = next_images
    elif not current["base"].get("reference_images"):
        current["base"]["reference_images"] = []

    current["enabled"] = bool(enabled)
    current["updated_at"] = _now_iso()

    path = _profile_path(room_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return current


def load_closet_catalog(room_name: str) -> Dict[str, Any]:
    room_name = _safe_room_name(room_name)
    path = _catalog_path(room_name)
    if not path.exists():
        return _default_catalog()
    try:
        return _normalize_catalog(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return _default_catalog()


def _save_closet_catalog(room_name: str, catalog: Dict[str, Any]) -> Dict[str, Any]:
    room_name = _safe_room_name(room_name)
    catalog = _normalize_catalog(catalog)
    path = _catalog_path(room_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return catalog


def list_closet_items(room_name: str) -> List[Dict[str, Any]]:
    return list(load_closet_catalog(room_name).get("items") or [])


def get_closet_item(room_name: str, closet_id: str) -> Optional[Dict[str, Any]]:
    closet_id = str(closet_id or "").strip()
    if not closet_id:
        return None
    for item in list_closet_items(room_name):
        if item.get("id") == closet_id:
            return item
    return None


def _is_closet_image_path(room_name: str, rel_path: str) -> bool:
    if not rel_path:
        return False
    try:
        Path(rel_path).resolve().relative_to(_image_dir(room_name).resolve())
        return True
    except ValueError:
        return False


def _copy_image_to_closet(room_name: str, src_path: str) -> str:
    return _copy_image_to_dir(src_path, _image_dir(room_name))


def _copy_image_to_dir(src_path: str, target_dir: Path) -> str:
    if not src_path:
        return ""
    source = Path(str(src_path))
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"画像ファイルが見つかりません: {src_path}")
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_IMAGE_SUFFIXES:
        raise ValueError("対応していない画像形式です。png/jpg/jpeg/webp/gif を指定してください。")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}{suffix}"
    shutil.copy2(source, target)
    return str(target).replace("\\", "/")


def add_closet_item(
    room_name: str,
    name: str,
    part: str,
    description: str = "",
    reference_image: str = "",
    source: str = "generated",
    linked_item_id: str = "",
    tags=None,
) -> Dict[str, Any]:
    room_name = _safe_room_name(room_name)
    room_manager.ensure_room_files(room_name)
    catalog = load_closet_catalog(room_name)
    linked_item_id = str(linked_item_id or "").strip()
    if linked_item_id:
        existing = next((item for item in catalog["items"] if item.get("linked_item_id") == linked_item_id), None)
        if existing:
            return existing

    reference_copy = ""
    if reference_image:
        reference_copy = _copy_image_to_closet(room_name, reference_image)

    item = {
        "id": f"c_{uuid.uuid4().hex[:12]}",
        "name": str(name or "").strip() or "名称未設定",
        "part": _normalize_part(part),
        "description": str(description or "").strip(),
        "reference_image": reference_copy,
        "source": source if source in CLOSET_SOURCES else "generated",
        "linked_item_id": linked_item_id,
        "tags": _normalize_tags(tags),
        "created_at": _now_iso(),
    }
    catalog["items"].append(item)
    _save_closet_catalog(room_name, catalog)
    return item


def remove_closet_item(room_name: str, closet_id: str) -> None:
    room_name = _safe_room_name(room_name)
    closet_id = str(closet_id or "").strip()
    if not closet_id:
        return
    catalog = load_closet_catalog(room_name)
    target = next((item for item in catalog["items"] if item.get("id") == closet_id), None)
    catalog["items"] = [item for item in catalog["items"] if item.get("id") != closet_id]
    _save_closet_catalog(room_name, catalog)

    profile = load_persona_profile(room_name)
    worn = [item_id for item_id in profile["current"].get("worn", []) if item_id != closet_id]
    if worn != profile["current"].get("worn", []):
        set_current_outfit(room_name, profile["current"].get("note", ""), worn)

    ref_path = (target or {}).get("reference_image")
    if ref_path and _is_closet_image_path(room_name, ref_path):
        still_used = any(item.get("reference_image") == ref_path for item in catalog["items"])
        base_uses = ref_path in profile.get("base", {}).get("reference_images", [])
        if not still_used and not base_uses:
            path = Path(ref_path)
            if path.exists() and path.is_file():
                path.unlink()


def set_current_outfit(room_name: str, note: str = "", worn_ids=None) -> Dict[str, Any]:
    room_name = _safe_room_name(room_name)
    valid_ids = _valid_closet_ids(room_name)
    worn = []
    for closet_id in worn_ids or []:
        closet_id = str(closet_id or "").strip()
        if closet_id and closet_id in valid_ids and closet_id not in worn:
            worn.append(closet_id)

    profile = load_persona_profile(room_name)
    profile["current"]["note"] = str(note or "").strip()
    profile["current"]["worn"] = worn
    profile["updated_at"] = _now_iso()
    path = _profile_path(room_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return profile


def wear_item(room_name: str, closet_id: str) -> Dict[str, Any]:
    room_name = _safe_room_name(room_name)
    closet_id = str(closet_id or "").strip()
    if not get_closet_item(room_name, closet_id):
        raise ValueError(f"クローゼット項目が見つかりません: {closet_id}")
    profile = load_persona_profile(room_name)
    worn = list(profile["current"].get("worn") or [])
    if closet_id not in worn:
        worn.append(closet_id)
    return set_current_outfit(room_name, profile["current"].get("note", ""), worn)


def take_off_item(room_name: str, closet_id: str) -> Dict[str, Any]:
    room_name = _safe_room_name(room_name)
    closet_id = str(closet_id or "").strip()
    profile = load_persona_profile(room_name)
    worn = [item_id for item_id in profile["current"].get("worn", []) if item_id != closet_id]
    return set_current_outfit(room_name, profile["current"].get("note", ""), worn)


def _load_profile_from_path(path: Path, valid_ids=None) -> Dict[str, Any]:
    if not path.exists():
        return _default_profile()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and valid_ids is not None:
            data["_valid_closet_ids"] = list(valid_ids)
        return _normalize_profile(data)
    except (json.JSONDecodeError, OSError):
        return _default_profile()


def _save_profile_to_path(path: Path, profile: Dict[str, Any]) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_profile(profile)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


def _load_user_catalog(scope: str, room_name: str = "") -> Dict[str, Any]:
    path = _user_catalog_path(scope, room_name)
    if not path.exists():
        return _default_catalog()
    try:
        return _normalize_catalog(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return _default_catalog()


def _save_user_catalog(scope: str, room_name: str, catalog: Dict[str, Any]) -> Dict[str, Any]:
    catalog = _normalize_catalog(catalog)
    path = _user_catalog_path(scope, room_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return catalog


def is_user_closet_common(room_name: str) -> bool:
    """Return whether the room uses the common user closet profile."""
    room_name = _safe_room_name(room_name)
    config = room_manager.get_room_config(room_name) or {}
    overrides = config.get("override_settings", {}) if isinstance(config, dict) else {}
    return bool(overrides.get(USER_CLOSET_USE_COMMON_KEY, True))


def set_user_closet_common(room_name: str, use_common: bool) -> None:
    room_name = _safe_room_name(room_name)
    room_manager.update_room_override_key(room_name, USER_CLOSET_USE_COMMON_KEY, bool(use_common))


def _effective_user_scope(room_name: str) -> str:
    return "common" if is_user_closet_common(room_name) else "room"


def load_user_profile_for_scope(scope: str, room_name: str = "") -> Dict[str, Any]:
    scope = _validate_user_scope(scope)
    if scope == "room":
        room_name = _safe_room_name(room_name)
    items = _load_user_catalog(scope, room_name).get("items") or []
    valid_ids = {item.get("id") for item in items}
    return _load_profile_from_path(_user_profile_path(scope, room_name), valid_ids=valid_ids)


def load_user_profile(room_name: str) -> Dict[str, Any]:
    """Load the effective user appearance profile for a room."""
    room_name = _safe_room_name(room_name)
    return load_user_profile_for_scope(_effective_user_scope(room_name), room_name)


def save_user_profile(scope: str, room_name: str, enabled, description, reference_images) -> Dict[str, Any]:
    """Save a user appearance profile for the common or room scope."""
    scope = _validate_user_scope(scope)
    if scope == "room":
        room_name = _safe_room_name(room_name)
        room_manager.ensure_room_files(room_name)
    current = load_user_profile_for_scope(scope, room_name)

    description_text = str(description or "").strip()
    next_images = _normalize_reference_images(reference_images)

    if description_text:
        current["base"]["description"] = description_text
    elif not current["base"].get("description"):
        current["base"]["description"] = ""

    if next_images:
        current["base"]["reference_images"] = next_images
    elif not current["base"].get("reference_images"):
        current["base"]["reference_images"] = []

    current["enabled"] = bool(enabled)
    current["updated_at"] = _now_iso()
    return _save_profile_to_path(_user_profile_path(scope, room_name), current)


def list_user_closet_items_for_scope(scope: str, room_name: str = "") -> List[Dict[str, Any]]:
    scope = _validate_user_scope(scope)
    if scope == "room":
        room_name = _safe_room_name(room_name)
    return list(_load_user_catalog(scope, room_name).get("items") or [])


def list_user_closet_items(room_name: str) -> List[Dict[str, Any]]:
    room_name = _safe_room_name(room_name)
    return list_user_closet_items_for_scope(_effective_user_scope(room_name), room_name)


def get_user_closet_item(scope: str, room_name: str, closet_id: str) -> Optional[Dict[str, Any]]:
    closet_id = str(closet_id or "").strip()
    if not closet_id:
        return None
    for item in list_user_closet_items_for_scope(scope, room_name):
        if item.get("id") == closet_id:
            return item
    return None


def get_effective_user_closet_item(room_name: str, closet_id: str) -> Optional[Dict[str, Any]]:
    room_name = _safe_room_name(room_name)
    return get_user_closet_item(_effective_user_scope(room_name), room_name, closet_id)


def _copy_image_to_user_closet(scope: str, room_name: str, src_path: str) -> str:
    return _copy_image_to_dir(src_path, _user_image_dir(scope, room_name))


def add_user_reference_image(scope: str, room_name: str, src_path: str) -> str:
    scope = _validate_user_scope(scope)
    if not src_path:
        raise ValueError("画像ファイルが選択されていません。")
    if scope == "room":
        room_manager.ensure_room_files(_safe_room_name(room_name))
    return _copy_image_to_user_closet(scope, room_name, src_path)


def _is_user_closet_image_path(scope: str, room_name: str, rel_path: str) -> bool:
    if not rel_path:
        return False
    try:
        Path(rel_path).resolve().relative_to(_user_image_dir(scope, room_name).resolve())
        return True
    except ValueError:
        return False


def remove_user_reference_image(scope: str, room_name: str, rel_path: str) -> None:
    scope = _validate_user_scope(scope)
    if scope == "room":
        room_name = _safe_room_name(room_name)
    rel_path = str(rel_path or "").strip()
    if not rel_path:
        return
    if not _is_user_closet_image_path(scope, room_name, rel_path):
        raise ValueError("ユーザークローゼット配下の画像だけ削除できます。")

    profile = load_user_profile_for_scope(scope, room_name)
    images = [path for path in profile["base"].get("reference_images", []) if path != rel_path]
    profile["base"]["reference_images"] = images
    profile["updated_at"] = _now_iso()
    _save_profile_to_path(_user_profile_path(scope, room_name), profile)

    used_by_items = any(item.get("reference_image") == rel_path for item in list_user_closet_items_for_scope(scope, room_name))
    if not used_by_items:
        path = Path(rel_path)
        if path.exists() and path.is_file():
            path.unlink()


def add_user_closet_item(
    scope: str,
    room_name: str,
    name: str,
    part: str,
    description: str = "",
    reference_image: str = "",
    source: str = "generated",
    linked_item_id: str = "",
    tags=None,
) -> Dict[str, Any]:
    scope = _validate_user_scope(scope)
    if scope == "room":
        room_name = _safe_room_name(room_name)
        room_manager.ensure_room_files(room_name)
    catalog = _load_user_catalog(scope, room_name)
    linked_item_id = str(linked_item_id or "").strip()
    if linked_item_id:
        existing = next((item for item in catalog["items"] if item.get("linked_item_id") == linked_item_id), None)
        if existing:
            return existing

    reference_copy = ""
    if reference_image:
        reference_copy = _copy_image_to_user_closet(scope, room_name, reference_image)

    item = {
        "id": f"u_{uuid.uuid4().hex[:12]}",
        "name": str(name or "").strip() or "名称未設定",
        "part": _normalize_part(part),
        "description": str(description or "").strip(),
        "reference_image": reference_copy,
        "source": source if source in CLOSET_SOURCES else "generated",
        "linked_item_id": linked_item_id,
        "tags": _normalize_tags(tags),
        "created_at": _now_iso(),
    }
    catalog["items"].append(item)
    _save_user_catalog(scope, room_name, catalog)
    return item


def remove_user_closet_item(scope: str, room_name: str, closet_id: str) -> None:
    scope = _validate_user_scope(scope)
    if scope == "room":
        room_name = _safe_room_name(room_name)
    closet_id = str(closet_id or "").strip()
    if not closet_id:
        return
    catalog = _load_user_catalog(scope, room_name)
    target = next((item for item in catalog["items"] if item.get("id") == closet_id), None)
    catalog["items"] = [item for item in catalog["items"] if item.get("id") != closet_id]
    _save_user_catalog(scope, room_name, catalog)

    profile = load_user_profile_for_scope(scope, room_name)
    worn = [item_id for item_id in profile["current"].get("worn", []) if item_id != closet_id]
    if worn != profile["current"].get("worn", []):
        set_user_current_outfit(scope, room_name, profile["current"].get("note", ""), worn)

    ref_path = (target or {}).get("reference_image")
    if ref_path and _is_user_closet_image_path(scope, room_name, ref_path):
        still_used = any(item.get("reference_image") == ref_path for item in catalog["items"])
        base_uses = ref_path in profile.get("base", {}).get("reference_images", [])
        if not still_used and not base_uses:
            path = Path(ref_path)
            if path.exists() and path.is_file():
                path.unlink()


def set_user_current_outfit(scope: str, room_name: str, note: str = "", worn_ids=None) -> Dict[str, Any]:
    scope = _validate_user_scope(scope)
    if scope == "room":
        room_name = _safe_room_name(room_name)
    valid_ids = {item.get("id") for item in list_user_closet_items_for_scope(scope, room_name)}
    worn = []
    for closet_id in worn_ids or []:
        closet_id = str(closet_id or "").strip()
        if closet_id and closet_id in valid_ids and closet_id not in worn:
            worn.append(closet_id)
    profile = load_user_profile_for_scope(scope, room_name)
    profile["current"]["note"] = str(note or "").strip()
    profile["current"]["worn"] = worn
    profile["updated_at"] = _now_iso()
    return _save_profile_to_path(_user_profile_path(scope, room_name), profile)


def wear_user_item(scope: str, room_name: str, closet_id: str) -> Dict[str, Any]:
    scope = _validate_user_scope(scope)
    closet_id = str(closet_id or "").strip()
    if not get_user_closet_item(scope, room_name, closet_id):
        raise ValueError(f"ユーザークローゼット項目が見つかりません: {closet_id}")
    profile = load_user_profile_for_scope(scope, room_name)
    worn = list(profile["current"].get("worn") or [])
    if closet_id not in worn:
        worn.append(closet_id)
    return set_user_current_outfit(scope, room_name, profile["current"].get("note", ""), worn)


def take_off_user_item(scope: str, room_name: str, closet_id: str) -> Dict[str, Any]:
    scope = _validate_user_scope(scope)
    closet_id = str(closet_id or "").strip()
    profile = load_user_profile_for_scope(scope, room_name)
    worn = [item_id for item_id in profile["current"].get("worn", []) if item_id != closet_id]
    return set_user_current_outfit(scope, room_name, profile["current"].get("note", ""), worn)


def promote_user_room_to_common(room_name: str) -> Dict[str, Any]:
    """Copy room-local user closet profile/catalog/images to the common user closet."""
    room_name = _safe_room_name(room_name)
    room_profile = load_user_profile_for_scope("room", room_name)
    room_catalog = list_user_closet_items_for_scope("room", room_name)
    id_map = {}
    new_catalog = {"items": []}

    for item in room_catalog:
        new_id = f"u_{uuid.uuid4().hex[:12]}"
        id_map[item.get("id")] = new_id
        copied_ref = ""
        if item.get("reference_image"):
            copied_ref = _copy_image_to_user_closet("common", "", item.get("reference_image"))
        new_item = dict(item)
        new_item["id"] = new_id
        new_item["reference_image"] = copied_ref
        new_catalog["items"].append(new_item)

    common_images = []
    for ref_path in room_profile.get("base", {}).get("reference_images") or []:
        if ref_path:
            common_images.append(_copy_image_to_user_closet("common", "", ref_path))

    new_profile = _default_profile()
    new_profile["enabled"] = bool(room_profile.get("enabled"))
    new_profile["base"]["description"] = room_profile.get("base", {}).get("description", "")
    new_profile["base"]["reference_images"] = common_images
    new_profile["current"]["note"] = room_profile.get("current", {}).get("note", "")
    new_profile["current"]["worn"] = [
        id_map[item_id]
        for item_id in room_profile.get("current", {}).get("worn", [])
        if item_id in id_map
    ]
    new_profile["updated_at"] = _now_iso()

    _save_user_catalog("common", "", new_catalog)
    saved = _save_profile_to_path(_user_profile_path("common", ""), new_profile)
    set_user_closet_common(room_name, True)
    return saved


def _current_outfit_line(display_name: str, profile: Dict[str, Any], item_lookup) -> Optional[str]:
    if not profile.get("enabled"):
        return None

    current = profile.get("current") if isinstance(profile.get("current"), dict) else {}
    note = str(current.get("note") or "").strip()
    names = []
    for closet_id in current.get("worn") or []:
        item = item_lookup(closet_id)
        name = str((item or {}).get("name") or "").strip()
        if name and name not in names:
            names.append(name)

    if note and names:
        content = f"{note}（着用: {', '.join(names)}）"
    elif note:
        content = note
    elif names:
        content = f"着用: {', '.join(names)}"
    else:
        content = "特に指定なし"

    return f"- {display_name}: {content}"


def build_current_outfit_section(room_name: str) -> str:
    """Build a compact current outfit Markdown fragment for chat prompt context."""
    room_name = _safe_room_name(room_name)
    try:
        config = room_manager.get_room_config(room_name) or {}
    except Exception:
        config = {}
    if not isinstance(config, dict):
        config = {}

    persona_name = str(config.get("agent_display_name") or room_name).strip() or room_name
    user_name = str(config.get("user_display_name") or "ユーザー").strip() or "ユーザー"

    lines = []
    try:
        persona_profile = load_persona_profile(room_name)
        persona_line = _current_outfit_line(
            persona_name,
            persona_profile,
            lambda closet_id: get_closet_item(room_name, closet_id),
        )
        if persona_line:
            lines.append(persona_line)
    except Exception:
        pass

    try:
        user_profile = load_user_profile(room_name)
        user_line = _current_outfit_line(
            user_name,
            user_profile,
            lambda closet_id: get_effective_user_closet_item(room_name, closet_id),
        )
        if user_line:
            lines.append(user_line)
    except Exception:
        pass

    if not lines:
        return ""
    return "\n### 現在の装い\n" + "\n".join(lines) + "\n"


def _appearance_target_profile_and_lookup(room_name: str, target: str):
    room_name = _safe_room_name(room_name)
    target = str(target or "").strip().lower()
    if target == "persona":
        return load_persona_profile(room_name), lambda closet_id: get_closet_item(room_name, closet_id)
    if target == "user":
        return load_user_profile(room_name), lambda closet_id: get_effective_user_closet_item(room_name, closet_id)
    raise ValueError("target は 'persona' または 'user' を指定してください。")


def get_current_appearance_state(room_name: str, target: str) -> Dict[str, Any]:
    """Return the persisted mirror image and whether appearance settings changed later."""
    profile, _ = _appearance_target_profile_and_lookup(room_name, target)
    current = profile.get("current") if isinstance(profile.get("current"), dict) else {}
    image_path = str(current.get("appearance_image") or "").strip()
    if image_path and not Path(image_path).is_file():
        image_path = ""
    generated_at = str(current.get("appearance_generated_at") or "")
    source_updated_at = str(current.get("appearance_source_updated_at") or generated_at)
    source_signature = str(current.get("appearance_source_signature") or "")
    updated_at = str(profile.get("updated_at") or "")
    current_signature = _appearance_source_signature(profile)
    return {
        "image_path": image_path,
        "generated_at": generated_at,
        "needs_refresh": bool(
            image_path
            and (
                (source_signature and source_signature != current_signature)
                or (not source_signature and updated_at and source_updated_at and updated_at != source_updated_at)
            )
        ),
    }


def save_current_appearance_image(room_name: str, target: str, image_path: str) -> Dict[str, Any]:
    """Persist a generated image as the current mirror image for the effective target."""
    room_name = _safe_room_name(room_name)
    image_path = str(image_path or "").strip()
    if not image_path or not Path(image_path).is_file():
        raise ValueError("保存する現在の姿画像が見つかりません。")

    target = str(target or "").strip().lower()
    if target == "persona":
        profile = load_persona_profile(room_name)
        path = _profile_path(room_name)
    elif target == "user":
        scope = _effective_user_scope(room_name)
        profile = load_user_profile_for_scope(scope, room_name)
        path = _user_profile_path(scope, room_name)
    else:
        raise ValueError("target は 'persona' または 'user' を指定してください。")

    generated_at = _now_iso()
    profile["current"]["appearance_image"] = image_path.replace("\\", "/")
    profile["current"]["appearance_generated_at"] = generated_at
    profile["current"]["appearance_source_updated_at"] = str(profile.get("updated_at") or "")
    profile["current"]["appearance_source_signature"] = _appearance_source_signature(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return get_current_appearance_state(room_name, target)


def _appearance_source_signature(profile: Dict[str, Any]) -> str:
    """Build a stable signature of settings that affect the visible current appearance."""
    base = profile.get("base") if isinstance(profile.get("base"), dict) else {}
    current = profile.get("current") if isinstance(profile.get("current"), dict) else {}
    payload = {
        "enabled": bool(profile.get("enabled")),
        "description": str(base.get("description") or ""),
        "reference_images": list(base.get("reference_images") or []),
        "note": str(current.get("note") or ""),
        "worn": list(current.get("worn") or []),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _appearance_item_line(item: Optional[Dict[str, Any]]) -> Optional[str]:
    if not item:
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    part = str(item.get("part") or "").strip()
    description = str(item.get("description") or "").strip()
    line = name
    if part:
        line += f"（{part}）"
    if description:
        line += f": {description}"
    return line


def build_appearance_prompt(room_name: str, target: str, extra: str = "") -> str:
    """Build an image prompt from the enabled appearance profile and current outfit."""
    profile, item_lookup = _appearance_target_profile_and_lookup(room_name, target)
    if not profile.get("enabled"):
        return ""

    base = str(profile.get("base", {}).get("description") or "").strip()
    current = profile.get("current") if isinstance(profile.get("current"), dict) else {}
    note = str(current.get("note") or "").strip()
    extra = str(extra or "").strip()

    item_lines = []
    for closet_id in current.get("worn") or []:
        line = _appearance_item_line(item_lookup(closet_id))
        if line and line not in item_lines:
            item_lines.append(line)

    prompt_lines = [
        "Generate a full-body appearance image based on the current appearance profile and outfit.",
    ]
    if base:
        prompt_lines.append(f"Base appearance: {base}")
    if note:
        prompt_lines.append(f"Current outfit note: {note}")
    if item_lines:
        prompt_lines.append("Worn items:")
        prompt_lines.extend(f"- {line}" for line in item_lines)
    if extra:
        prompt_lines.append(f"Additional request: {extra}")

    if len(prompt_lines) == 1:
        return ""
    return "\n".join(prompt_lines)


def _append_existing_reference_image(paths: List[str], ref_path: str) -> None:
    ref_path = str(ref_path or "").strip()
    if not ref_path or ref_path in paths:
        return
    if Path(ref_path).exists():
        paths.append(ref_path)


def _append_profile_reference_images(paths: List[str], profile: Dict[str, Any]) -> None:
    for ref_path in profile.get("base", {}).get("reference_images") or []:
        _append_existing_reference_image(paths, ref_path)


def collect_reference_images(room_name: str, include_user: bool = False) -> List[str]:
    """Collect existing closet reference image paths from enabled profiles."""
    room_name = _safe_room_name(room_name)
    paths = []

    persona_profile = load_persona_profile(room_name)
    if persona_profile.get("enabled"):
        _append_profile_reference_images(paths, persona_profile)
        for closet_id in persona_profile.get("current", {}).get("worn") or []:
            item = get_closet_item(room_name, closet_id)
            _append_existing_reference_image(paths, (item or {}).get("reference_image", ""))

    if include_user:
        user_profile = load_user_profile(room_name)
        if user_profile.get("enabled"):
            _append_profile_reference_images(paths, user_profile)
            for closet_id in user_profile.get("current", {}).get("worn") or []:
                item = get_effective_user_closet_item(room_name, closet_id)
                _append_existing_reference_image(paths, (item or {}).get("reference_image", ""))

    return paths


def collect_appearance_reference_images(room_name: str, target: str, include_current: bool = True) -> List[str]:
    """Collect mirror-first references, then worn items and a stable base reference."""
    profile, item_lookup = _appearance_target_profile_and_lookup(room_name, target)
    if not profile.get("enabled"):
        return []

    paths = []
    current = profile.get("current") if isinstance(profile.get("current"), dict) else {}
    if include_current:
        _append_existing_reference_image(paths, current.get("appearance_image", ""))
    has_current_appearance = bool(paths)
    base_images = profile.get("base", {}).get("reference_images") or []
    if has_current_appearance:
        for closet_id in profile.get("current", {}).get("worn") or []:
            item = item_lookup(closet_id)
            _append_existing_reference_image(paths, (item or {}).get("reference_image", ""))
        for ref_path in base_images[:1]:
            _append_existing_reference_image(paths, ref_path)
    else:
        _append_profile_reference_images(paths, profile)
        for closet_id in profile.get("current", {}).get("worn") or []:
            item = item_lookup(closet_id)
            _append_existing_reference_image(paths, (item or {}).get("reference_image", ""))
    return paths[:4]


def _same_reference_path(left: str, right: str) -> bool:
    left = str(left or "").strip()
    right = str(right or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


def _description_for_base_reference(profile: Dict[str, Any], ref_path: str) -> Optional[str]:
    for base_ref in profile.get("base", {}).get("reference_images") or []:
        if _same_reference_path(base_ref, ref_path):
            description = str(profile.get("base", {}).get("description") or "").strip()
            return description or None
    return None


def _description_for_item_reference(items: List[Dict[str, Any]], ref_path: str) -> Optional[str]:
    for item in items:
        if _same_reference_path(item.get("reference_image"), ref_path):
            description = str(item.get("description") or "").strip()
            return description or None
    return None


def describe_reference_image(room_name: str, ref_path: str) -> Optional[str]:
    """Return curated closet text for a reference image path when available."""
    room_name = _safe_room_name(room_name)
    ref_path = str(ref_path or "").strip()
    if not ref_path:
        return None

    persona_profile = load_persona_profile(room_name)
    description = _description_for_base_reference(persona_profile, ref_path)
    if description:
        return description
    description = _description_for_item_reference(list_closet_items(room_name), ref_path)
    if description:
        return description

    user_profile = load_user_profile(room_name)
    description = _description_for_base_reference(user_profile, ref_path)
    if description:
        return description
    description = _description_for_item_reference(list_user_closet_items(room_name), ref_path)
    if description:
        return description

    return None


def add_reference_image(room_name: str, src_path: str) -> str:
    """Copy a reference image into the room closet and return its relative path."""
    room_name = _safe_room_name(room_name)
    if not src_path:
        raise ValueError("画像ファイルが選択されていません。")
    source = Path(str(src_path))
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"画像ファイルが見つかりません: {src_path}")
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_IMAGE_SUFFIXES:
        raise ValueError("対応していない画像形式です。png/jpg/jpeg/webp/gif を指定してください。")

    room_manager.ensure_room_files(room_name)
    target_dir = _image_dir(room_name)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}{suffix}"
    shutil.copy2(source, target)
    return str(target).replace("\\", "/")


def remove_reference_image(room_name: str, rel_path: str) -> None:
    """Remove a closet reference image and detach it from the saved profile."""
    room_name = _safe_room_name(room_name)
    rel_path = str(rel_path or "").strip()
    if not rel_path:
        return

    closet_root = _closet_dir(room_name).resolve()
    resolved = Path(rel_path).resolve()
    try:
        resolved.relative_to(closet_root)
    except ValueError:
        raise ValueError("クローゼット配下の画像だけ削除できます。")

    if resolved.exists() and resolved.is_file():
        resolved.unlink()

    current = load_persona_profile(room_name)
    images = [path for path in current["base"].get("reference_images", []) if path != rel_path]
    current["base"]["reference_images"] = images
    current["updated_at"] = _now_iso()
    path = _profile_path(room_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
