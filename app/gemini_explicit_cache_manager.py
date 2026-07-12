"""Room-scoped Gemini Explicit cache lifecycle helpers."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

import constants
import config_manager
import file_lock_utils
import room_manager
import tool_usage_stats

try:
    from google import genai
    from google.genai import types
except Exception:  # pragma: no cover - import availability depends on optional env
    genai = None
    types = None

try:
    from langchain_google_genai._function_utils import convert_to_genai_function_declarations
except Exception:  # pragma: no cover
    convert_to_genai_function_declarations = None


SETTINGS_KEY = "gemini_explicit_cache"
STATE_FILE_NAME = "gemini_explicit_cache_state.json"
DEFAULT_TTL_MINUTES = 30
DEFAULT_TOOL_LIMIT = 12
MIN_TTL_MINUTES = 5
MAX_TTL_MINUTES = 60
MIN_TOOL_LIMIT = 3
MAX_TOOL_LIMIT = 20

DEFAULT_CORE_TOOL_NAMES = [
    "request_capability",
    "recall_memories",
    "search_past_conversations",
    "read_memory_context",
    "list_available_locations",
    "set_current_location",
    "list_procedures",
    "read_procedure",
]
TOOL_SELECTION_HYSTERESIS_RATIO = 1.25


@dataclass(frozen=True)
class ExplicitCacheContext:
    cache_name: str
    content_hash: str
    tool_names: list[str]
    ttl_minutes: int
    expires_at: str
    created: bool = False
    refreshed: bool = False


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[_dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.astimezone(_dt.timezone.utc)
    except ValueError:
        return None


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        intval = int(value)
    except (TypeError, ValueError):
        intval = default
    return max(minimum, min(maximum, intval))


def get_room_settings(room_name: str) -> dict[str, Any]:
    config = room_manager.get_room_config(room_name) if room_name else None
    overrides = (config or {}).get("override_settings", {})
    raw = overrides.get(SETTINGS_KEY, {}) if isinstance(overrides, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "ttl_minutes": _clamp_int(raw.get("ttl_minutes"), DEFAULT_TTL_MINUTES, MIN_TTL_MINUTES, MAX_TTL_MINUTES),
        "tool_limit": _clamp_int(raw.get("tool_limit"), DEFAULT_TOOL_LIMIT, MIN_TOOL_LIMIT, MAX_TOOL_LIMIT),
    }


def is_enabled_for_room(room_name: str) -> bool:
    return bool(get_room_settings(room_name).get("enabled"))


def is_room_rotation_enabled(room_name: str, model_name: Optional[str] = None) -> bool:
    rotation_enabled = bool(config_manager.CONFIG_GLOBAL.get("enable_api_key_rotation", True))
    if not room_name:
        return rotation_enabled
    config = room_manager.get_room_config(room_name) or {}
    overrides = config.get("override_settings", {}) if isinstance(config, dict) else {}
    room_provider = config_manager.normalize_room_provider_override(overrides.get("provider"))
    room_value = overrides.get("enable_api_key_rotation")
    if room_provider is not None and room_value is not None:
        rotation_enabled = bool(room_value)
    try:
        if model_name and config_manager.is_image_generation_model(model_name):
            return False
    except Exception:
        pass
    return rotation_enabled


def get_disabled_reason(room_name: str, model_name: Optional[str] = None) -> Optional[str]:
    if not is_enabled_for_room(room_name):
        return "room_disabled"
    if config_manager.get_active_provider(room_name) != "google":
        return "provider_not_google"
    if is_room_rotation_enabled(room_name, model_name=model_name):
        return "api_key_rotation_enabled"
    return None


def _state_path(room_name: str) -> str:
    return os.path.join(constants.ROOMS_DIR, room_name, "memory", STATE_FILE_NAME)


def load_state(room_name: str) -> dict[str, Any]:
    if not room_name:
        return {}
    data = file_lock_utils.safe_json_read(_state_path(room_name), default={})
    return data if isinstance(data, dict) else {}


def save_state(room_name: str, state: dict[str, Any]) -> bool:
    if not room_name:
        return False
    return file_lock_utils.safe_json_write(_state_path(room_name), state)


def clear_state(room_name: str) -> None:
    path = _state_path(room_name)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        print(f"⚠️ [GeminiExplicitCache] 状態ファイル削除失敗: {path} ({e})")


def _api_key_id(api_key: str, room_name: str, model_name: str) -> str:
    key_name = config_manager.get_key_name_by_value(api_key)
    if key_name and key_name != "Unknown":
        return key_name
    active_name = config_manager.get_active_gemini_api_key_name(room_name, model_name)
    return active_name or "Unknown"


def build_content_hash(static_system_text: str, tool_names: Iterable[str]) -> str:
    payload = {
        "static_system_text": static_system_text or "",
        # 集合ベース（順序非依存）でハッシュ化する。
        # 同じツール集合がランク順の入れ替えだけで別ハッシュになると、
        # まだ有効なキャッシュを不要に再作成してしまい課金が増えるため。
        "tool_names": sorted({str(name) for name in tool_names if name}),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_api_key_id(api_key: str, room_name: str, model_name: str) -> str:
    return _api_key_id(api_key, room_name, model_name)


def select_explicit_cache_tools(
    room_name: str,
    all_tools: list[Callable],
    limit: Optional[int] = None,
    *,
    model_name: Optional[str] = None,
    api_key_id: Optional[str] = None,
) -> list[Callable]:
    settings = get_room_settings(room_name)
    tool_limit = _clamp_int(limit if limit is not None else settings["tool_limit"], DEFAULT_TOOL_LIMIT, MIN_TOOL_LIMIT, MAX_TOOL_LIMIT)
    tool_map = {getattr(tool, "name", None): tool for tool in all_tools}
    tool_map = {name: tool for name, tool in tool_map.items() if name and tool is not None}

    try:
        from agent.tool_registry import ToolRegistry

        registry = ToolRegistry(all_tools)
        active_tools = registry.get_active_tools(room_name)
        active_names = {getattr(tool, "name", None) for tool in active_tools}
    except Exception:
        active_names = set(tool_map.keys())
    active_names.discard(None)

    state = load_state(room_name)
    snapshot_names = _valid_tool_snapshot(
        state,
        active_names,
        model_name=model_name,
        api_key_id=api_key_id,
    )
    if snapshot_names:
        return [tool_map[name] for name in snapshot_names if name in tool_map]

    stats = tool_usage_stats.get_stats(room_name).get("tools", {})
    stats = stats if isinstance(stats, dict) else {}
    previous_names = _state_tool_names(state)
    selected = _select_tool_names_with_hysteresis(
        tool_map=tool_map,
        active_names=active_names,
        stats=stats,
        limit=tool_limit,
        previous_names=previous_names,
    )
    return [tool_map[name] for name in selected]


def _state_tool_names(state: dict[str, Any]) -> list[str]:
    tool_names = state.get("tool_names") if isinstance(state, dict) else None
    if not isinstance(tool_names, list):
        return []
    return [str(name) for name in tool_names if name]


def _valid_tool_snapshot(
    state: dict[str, Any],
    active_names: set[str],
    *,
    model_name: Optional[str],
    api_key_id: Optional[str],
) -> list[str]:
    snapshot_names = _state_tool_names(state)
    expires_at = _parse_iso(state.get("expires_at")) if isinstance(state, dict) else None
    if not (
        snapshot_names
        and state.get("cache_name")
        and expires_at
        and expires_at > _now_utc()
    ):
        return []
    if model_name is not None and state.get("model_name") != model_name:
        return []
    if api_key_id is not None and state.get("api_key_id") != api_key_id:
        return []
    if not set(snapshot_names).issubset(active_names):
        return []
    return snapshot_names


def _tool_count(stats: dict[str, Any], name: str) -> int:
    data = stats.get(name)
    data = data if isinstance(data, dict) else {}
    return int(data.get("count_total") or data.get("count") or 0)


def _ranked_tool_names(stats: dict[str, Any], tool_map: dict[str, Callable], active_names: set[str]) -> list[str]:
    def _rank(name: str) -> tuple[int, str]:
        # タイブレークは last_used_at（使うたびに変動）ではなく名前（決定論的）にする。
        # 同数ツールが境界で出入りするとキャッシュが再作成され課金が増えるため、選択を安定させる。
        return (_tool_count(stats, name), name)

    stat_names = [name for name in stats if name in tool_map and name in active_names]
    return sorted(stat_names, key=_rank, reverse=True)


def _select_tool_names_with_hysteresis(
    *,
    tool_map: dict[str, Callable],
    active_names: set[str],
    stats: dict[str, Any],
    limit: int,
    previous_names: list[str],
) -> list[str]:
    ranked_names = _ranked_tool_names(stats, tool_map, active_names)
    selected: list[str] = []
    for name in previous_names + ranked_names + DEFAULT_CORE_TOOL_NAMES:
        if name in tool_map and name in active_names and name not in selected:
            selected.append(name)
        if len(selected) >= limit:
            break

    # 再計算時も前回メンバーを優先し、挑戦側が明確に上回る場合だけ入れ替える。
    # 25%の余裕を置くことで、境界付近の使用頻度ゆらぎによるキャッシュ再作成を抑える。
    for challenger in ranked_names:
        if challenger in selected:
            continue
        if len(selected) < limit:
            selected.append(challenger)
            continue
        incumbent = min(selected, key=lambda name: (_tool_count(stats, name), name))
        if _tool_count(stats, challenger) > _tool_count(stats, incumbent) * TOOL_SELECTION_HYSTERESIS_RATIO:
            selected[selected.index(incumbent)] = challenger

    return selected[:limit]


def _convert_tools(tools_list: list[Callable]) -> Optional[list[Any]]:
    if not tools_list:
        return None
    if convert_to_genai_function_declarations is None:
        raise RuntimeError("langchain_google_genai tool conversion is unavailable")
    return convert_to_genai_function_declarations(tools_list)


def _client(api_key: str) -> Any:
    if genai is None:
        raise RuntimeError("google-genai is unavailable")
    return genai.Client(api_key=api_key)


def _ttl_seconds(ttl_minutes: int) -> int:
    return _clamp_int(ttl_minutes, DEFAULT_TTL_MINUTES, MIN_TTL_MINUTES, MAX_TTL_MINUTES) * 60


def _delete_remote_cache(cache_name: str, api_key: str) -> None:
    if not cache_name or not api_key:
        return
    try:
        _client(api_key).caches.delete(name=cache_name)
    except Exception as e:
        print(f"⚠️ [GeminiExplicitCache] リモート削除失敗: {cache_name} ({e})")


def delete_cache(room_name: str, api_key: Optional[str] = None, model_name: Optional[str] = None) -> None:
    state = load_state(room_name)
    cache_name = state.get("cache_name")
    api_key = api_key or config_manager.get_active_gemini_api_key(room_name, model_name)
    if cache_name and api_key:
        _delete_remote_cache(cache_name, api_key)
    clear_state(room_name)


def delete_all_known_caches() -> None:
    """Best-effort cleanup for app shutdown."""
    rooms_dir = constants.ROOMS_DIR
    if not os.path.isdir(rooms_dir):
        return
    for room_name in os.listdir(rooms_dir):
        state = load_state(room_name)
        if not state.get("cache_name"):
            continue
        api_key = None
        api_key_id = state.get("api_key_id")
        if isinstance(api_key_id, str) and api_key_id:
            api_key = config_manager.GEMINI_API_KEYS.get(api_key_id)
        if not api_key:
            api_key = config_manager.get_active_gemini_api_key(room_name, state.get("model_name"))
        delete_cache(room_name, api_key=api_key, model_name=state.get("model_name"))


def _refresh_cache(client: Any, cache_name: str, ttl_minutes: int) -> bool:
    if types is None:
        return False
    try:
        client.caches.update(
            name=cache_name,
            config=types.UpdateCachedContentConfig(ttl=f"{_ttl_seconds(ttl_minutes)}s"),
        )
        return True
    except Exception as e:
        print(f"⚠️ [GeminiExplicitCache] TTL更新失敗: {cache_name} ({e})")
        return False


def ensure_cache(
    *,
    room_name: str,
    model_name: str,
    api_key: str,
    static_system_text: str,
    tools_list: list[Callable],
    available_tool_names: Optional[Iterable[str]] = None,
) -> Optional[ExplicitCacheContext]:
    reason = get_disabled_reason(room_name, model_name=model_name)
    if reason:
        if reason == "api_key_rotation_enabled":
            clear_state(room_name)
        return None
    if not api_key:
        return None

    settings = get_room_settings(room_name)
    ttl_minutes = settings["ttl_minutes"]
    tool_names = [getattr(tool, "name", "") for tool in tools_list if getattr(tool, "name", "")]
    content_hash = build_content_hash(static_system_text, tool_names)
    api_key_id = _api_key_id(api_key, room_name, model_name)
    state = load_state(room_name)
    expires_at = _parse_iso(state.get("expires_at"))
    still_valid = bool(
        state.get("cache_name")
        and state.get("model_name") == model_name
        and state.get("api_key_id") == api_key_id
        and state.get("content_hash") == content_hash
        and expires_at
        and expires_at > _now_utc()
    )

    client = None
    if still_valid:
        try:
            client = _client(api_key)
            if _refresh_cache(client, state["cache_name"], ttl_minutes):
                new_expires_at = _iso(_now_utc() + _dt.timedelta(seconds=_ttl_seconds(ttl_minutes)))
                state["expires_at"] = new_expires_at
                state["last_used_at"] = _iso(_now_utc())
                save_state(room_name, state)
                return ExplicitCacheContext(
                    cache_name=state["cache_name"],
                    content_hash=content_hash,
                    tool_names=tool_names,
                    ttl_minutes=ttl_minutes,
                    expires_at=new_expires_at,
                    refreshed=True,
                )
        except Exception as e:
            print(f"⚠️ [GeminiExplicitCache] TTL更新準備失敗: {state.get('cache_name')} ({e})")
        clear_state(room_name)

    if state.get("cache_name"):
        _delete_remote_cache(state["cache_name"], api_key)

    if types is None:
        return None

    try:
        client = client or _client(api_key)
        _log_cache_tool_selection(tool_names, available_tool_names)
        cache = client.caches.create(
            model=model_name,
            config=types.CreateCachedContentConfig(
                system_instruction=static_system_text,
                tools=_convert_tools(tools_list),
                ttl=f"{_ttl_seconds(ttl_minutes)}s",
            ),
        )
    except Exception as e:
        print(f"⚠️ [GeminiExplicitCache] 作成失敗: room={room_name} model={model_name} ({e})")
        clear_state(room_name)
        return None

    expires_at_text = _iso(_now_utc() + _dt.timedelta(seconds=_ttl_seconds(ttl_minutes)))
    new_state = {
        "cache_name": getattr(cache, "name", ""),
        "model_name": model_name,
        "api_key_id": api_key_id,
        "content_hash": content_hash,
        "tool_names": tool_names,
        "ttl_minutes": ttl_minutes,
        "created_at": _iso(_now_utc()),
        "last_used_at": _iso(_now_utc()),
        "expires_at": expires_at_text,
    }
    if not new_state["cache_name"]:
        return None
    save_state(room_name, new_state)
    return ExplicitCacheContext(
        cache_name=new_state["cache_name"],
        content_hash=content_hash,
        tool_names=tool_names,
        ttl_minutes=ttl_minutes,
        expires_at=expires_at_text,
        created=True,
    )


def _log_cache_tool_selection(tool_names: list[str], available_tool_names: Optional[Iterable[str]]) -> None:
    available = [str(name) for name in (available_tool_names or []) if name]
    excluded = sorted(set(available) - set(tool_names))
    print(
        "[GeminiExplicitCache] 作成ツール snapshot: "
        f"selected={sorted(tool_names)} excluded_available={excluded}"
    )
