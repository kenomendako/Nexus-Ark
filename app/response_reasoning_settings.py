"""応答モデル単位の推論設定を解決・永続化する境界。

このモジュールは、プロバイダ固有の能力判定（``response_reasoning``）と
設定ファイルの読み書きを分離する。保存単位は必ず target key の1項目だけに
限定し、画面に残った古い値が別の target を上書きしないようにする。

``response_reasoning_defaults`` は ``config.json`` のネスト辞書、
``response_reasoning_overrides`` は各ルームの ``override_settings`` に保存する。
新しい設定が無い場合は ``None`` を返し、既存の Gemini ``thinking_level`` の
解決を呼び出し側へ委ねる。
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import config_manager
import constants
import file_lock_utils
import response_reasoning as contract
import room_manager


GLOBAL_SETTINGS_KEY = "response_reasoning_defaults"
ROOM_SETTINGS_KEY = "response_reasoning_overrides"
INHERIT_MARKER: dict[str, bool] = {"inherit": True}
STATE_VERSION = 1


class ResponseReasoningConflictError(ValueError):
    """画面表示時点から対象設定が変わっている場合に送出する。"""


def _safe_base_url(value: Any) -> str:
    """target識別用URLから認証情報・query・fragmentを除く。"""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if not parts.scheme and not parts.netloc:
            return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        # userinfoを落とし、host/portだけを残す。
        hostname = parts.hostname or ""
        netloc = hostname
        if parts.port:
            netloc = f"{hostname}:{parts.port}"
        if ":" in hostname and not hostname.startswith("["):
            netloc = f"[{hostname}]" + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", ""))
    except ValueError:
        return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")


_SECRET_KEY_RE = re.compile(r"(?:api[_-]?key|token|secret|password|credential)", re.IGNORECASE)


def _strip_secret_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_secret_fields(item)
            for key, item in value.items()
            if not _SECRET_KEY_RE.search(str(key))
        }
    if isinstance(value, list):
        return [_strip_secret_fields(item) for item in value]
    return value


def _canonical_target(target: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(target, Mapping):
        raise ValueError("推論設定のtargetが不正です。")
    cleaned = _strip_secret_fields(dict(target))
    cleaned["provider"] = str(cleaned.get("provider") or "").strip().lower()
    cleaned["model"] = str(cleaned.get("model") or "").strip()
    cleaned["base_url"] = _safe_base_url(cleaned.get("base_url"))
    cleaned["profile"] = str(cleaned.get("profile") or "").strip()
    return cleaned


def make_target(provider: Any, model: Any, base_url: Any = "", profile: Any = "") -> dict[str, Any]:
    """純粋target builderを呼び、保存用の秘密情報を除去したtargetを返す。"""

    safe_base_url = _safe_base_url(base_url)
    target = contract.make_target(provider, model, safe_base_url, profile)
    return _canonical_target(target)



def make_gemini_target(model: Any, room_name: str | None = None, connection=None) -> dict[str, Any]:
    """Developerの既存keyを保ち、Vertexの課金先ごとに推論設定を分ける。"""
    import gemini_connection
    selected = connection if connection is not None else gemini_connection.get_connection(room_name)
    selected = gemini_connection.normalize_connection(selected, require_valid=True)
    if selected.get("backend") == "vertex":
        identity = gemini_connection.connection_id(selected)
        return make_target("google", model, f"https://aiplatform.googleapis.com/connections/{identity}", "Vertex AI")
    return make_target("google", model)


def target_key(target: Mapping[str, Any]) -> str:
    """target keyを作成する。キーには認証情報を含めない。"""

    canonical = _canonical_target(target)
    value = contract.target_key(canonical)
    return str(value or "").strip()


def get_capabilities(target: Mapping[str, Any]) -> dict[str, Any]:
    """targetの能力を純粋モジュールから取得し、最低限の形へ正規化する。"""

    canonical = _canonical_target(target)
    raw = contract.get_capabilities(canonical)
    if not isinstance(raw, Mapping):
        raw = {}
    efforts = raw.get("efforts", [])
    if not isinstance(efforts, list):
        efforts = list(efforts) if isinstance(efforts, (tuple, set)) else []
    return {
        "efforts": [str(item) for item in efforts if str(item).strip()],
        "budget": bool(raw.get("budget", False)),
        "transport": str(raw.get("transport") or "unsupported"),
        "note": str(raw.get("note") or ""),
    }


def validate_preference(target: Mapping[str, Any], pref: Mapping[str, Any]) -> dict[str, Any]:
    """新設定として保存可能なprefへ正規化する。"""

    canonical = _canonical_target(target)
    normalized = contract.validate_preference(canonical, pref)
    if not isinstance(normalized, Mapping):
        raise ValueError("推論設定の検証結果が辞書ではありません。")
    result = {"effort": str(normalized.get("effort") or "default").strip().lower()}
    if result["effort"] == "budget":
        budget_tokens = normalized.get("budget_tokens", pref.get("budget_tokens"))
        if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens <= 0:
            raise ValueError("budget_tokensは1以上の整数で指定してください。")
        result["budget_tokens"] = budget_tokens
    return result


def _copy_mapping(value: Any) -> dict[str, Any]:
    return deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _raw_record(value: Any) -> dict[str, Any]:
    """CAS比較用にmissing/inherit/valueを区別したJSON互換値へする。"""

    if value is None:
        return {"kind": "missing", "value": None}
    if isinstance(value, Mapping) and value.get("inherit") is True:
        return {"kind": "inherit", "value": deepcopy(dict(INHERIT_MARKER))}
    if isinstance(value, Mapping):
        return {"kind": "value", "value": deepcopy(dict(value))}
    return {"kind": "invalid", "value": deepcopy(value)}


def _record_value(record: Mapping[str, Any]) -> Any:
    kind = record.get("kind")
    if kind == "missing":
        return None
    return deepcopy(record.get("value"))


def _valid_saved_preference(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("inherit") is True:
        return None
    effort = value.get("effort")
    if not isinstance(effort, str):
        return None
    result = {"effort": effort.strip().lower()}
    if result["effort"] == "budget":
        budget_tokens = value.get("budget_tokens")
        if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens <= 0:
            return None
        result["budget_tokens"] = budget_tokens
    elif result["effort"] == "default":
        return result
    elif "budget_tokens" in value:
        # level設定に古いbudget値を混ぜない。
        return result
    return result


def _latest_global_map() -> dict[str, Any]:
    config = config_manager.load_config_file()
    return _copy_mapping(config.get(GLOBAL_SETTINGS_KEY))


def _latest_room_map(room_name: str) -> dict[str, Any]:
    config = room_manager.get_room_config(room_name)
    overrides = config.get("override_settings", {}) if isinstance(config, Mapping) else {}
    return _copy_mapping(overrides.get(ROOM_SETTINGS_KEY))


def _lookup_raw(target: Mapping[str, Any], room_name: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    key = target_key(target)
    global_map = _latest_global_map()
    global_record = _raw_record(global_map.get(key))
    if not room_name:
        return global_record, global_record

    room_map = _latest_room_map(room_name)
    room_record = _raw_record(room_map.get(key))
    return room_record, global_record


def _effective_from_records(
    room_record: Mapping[str, Any],
    global_record: Mapping[str, Any],
    room_name: str | None,
) -> tuple[dict[str, Any] | None, str]:
    if room_name and room_record.get("kind") == "value":
        preference = _valid_saved_preference(room_record.get("value"))
        if preference is not None:
            return preference, "room"
    if global_record.get("kind") == "value":
        preference = _valid_saved_preference(global_record.get("value"))
        if preference is not None:
            return preference, "global"
    return None, "legacy"


def _legacy_thinking_level(room_name: str | None = None) -> str | None:
    """新設定未登録時だけ表示する既存Gemini設定。"""

    if room_name:
        try:
            effective = config_manager.get_effective_settings(room_name)
            level = effective.get("thinking_level") if isinstance(effective, Mapping) else None
            if level:
                return str(level)
        except Exception:
            pass
    value = config_manager.CONFIG_GLOBAL.get("thinking_level") if isinstance(config_manager.CONFIG_GLOBAL, Mapping) else None
    return str(value) if value else None


def resolve_preference(target: Mapping[str, Any], room_name: str | None = None) -> dict[str, Any] | None:
    """targetの実効推論設定を読む。未登録時はNone（Gemini旧設定を維持）。"""

    room_record, global_record = _lookup_raw(target, room_name)
    preference, _source = _effective_from_records(room_record, global_record, room_name)
    return deepcopy(preference) if preference is not None else None


def read_state(target: Mapping[str, Any], room_name: str | None = None) -> dict[str, Any]:
    """現在のtargetとCAS基準をUI State向けに返す。"""

    canonical = _canonical_target(target)
    key = target_key(canonical)
    room_record, global_record = _lookup_raw(canonical, room_name)
    effective, source = _effective_from_records(room_record, global_record, room_name)
    return {
        "version": STATE_VERSION,
        "scope": "room" if room_name else "global",
        "room_name": str(room_name or ""),
        "target": deepcopy(canonical),
        "target_key": key,
        "baseline": deepcopy(room_record if room_name else global_record),
        "global_baseline": deepcopy(global_record),
        "effective": deepcopy(effective),
        "source": source,
        "legacy_thinking_level": _legacy_thinking_level(room_name) if source == "legacy" else None,
    }


def _assert_state_matches(
    state: Mapping[str, Any], target: Mapping[str, Any], room_name: str | None,
) -> None:
    if not isinstance(state, Mapping):
        raise ResponseReasoningConflictError("画面の推論設定基準がありません。最新設定を再読してください。")
    expected_scope = "room" if room_name else "global"
    if state.get("scope") != expected_scope or str(state.get("room_name") or "") != str(room_name or ""):
        raise ResponseReasoningConflictError("画面の対象範囲が変わっています。最新設定を再読してください。")
    current_key = target_key(target)
    if state.get("target_key") != current_key:
        raise ResponseReasoningConflictError("モデルまたは接続先が変わっています。対象を再読してから保存してください。")
    expected_target = _canonical_target(target)
    if state.get("target") != expected_target:
        raise ResponseReasoningConflictError("推論設定の対象が変わっています。対象を再読してから保存してください。")

    current_room_record, current_global_record = _lookup_raw(target, room_name)
    current_record = current_room_record if room_name else current_global_record
    baseline = state.get("baseline")
    if not isinstance(baseline, Mapping) or dict(baseline) != current_record:
        raise ResponseReasoningConflictError("最新の推論設定が変わっています。再読してから保存してください。")
    if room_name:
        global_baseline = state.get("global_baseline")
        if not isinstance(global_baseline, Mapping) or dict(global_baseline) != current_global_record:
            raise ResponseReasoningConflictError("共通の推論設定が変わっています。再読してから保存してください。")


def _save_value(
    target: Mapping[str, Any],
    preference: Mapping[str, Any] | None,
    room_name: str | None,
    state: Mapping[str, Any],
) -> dict[str, Any] | bool | str:
    # CAS確認と既存の差分保存APIを同じ外側lockで囲み、確認直後の
    # 別セッション更新によるtarget単位の巻き戻しを防ぐ。room保存では
    # 継承元のglobalも同時に確認するため、lock順をglobal→roomに統一する。
    with ExitStack() as stack:
        stack.enter_context(
            file_lock_utils.locked_file(
                constants.CONFIG_FILE,
                timeout=getattr(config_manager, "CONFIG_LOCK_TIMEOUT", file_lock_utils.DEFAULT_LOCK_TIMEOUT),
            )
        )
        if room_name:
            stack.enter_context(room_manager.room_config_write_guard(room_name))

        _assert_state_matches(state, target, room_name)
        key = target_key(target)
        if room_name:
            # roomの汎用APIは深いmergeなので、継承markerを明示解除する。
            # inherit→上書きへ戻した際に古いTrueが残らないようにする。
            value: dict[str, Any] = (
                deepcopy(dict(INHERIT_MARKER)) if preference is None
                else {**dict(preference), "inherit": False}
            )
            result = room_manager.update_room_override_nested(room_name, ROOM_SETTINGS_KEY, {key: value})
        else:
            # globalは削除APIを持たないため、legacy resetはinherit markerで表現する。
            value = deepcopy(dict(INHERIT_MARKER)) if preference is None else dict(preference)
            result = config_manager.update_nested_config_keys(GLOBAL_SETTINGS_KEY, {key: value})
        if result is False:
            return result
        # 保存後の再読もlock中に行い、stateに返すbaselineを同じ版から作る。
        return read_state(target, room_name)


def save_preference(
    target: Mapping[str, Any],
    preference: Mapping[str, Any] | None,
    *,
    room_name: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """CAS確認後にtarget単位で保存し、再読したstateを返す。"""

    canonical = _canonical_target(target)
    normalized = None if preference is None else validate_preference(canonical, preference)
    if state is None:
        raise ResponseReasoningConflictError("保存基準がありません。先に設定を読み込んでください。")
    result = _save_value(canonical, normalized, room_name, state)
    if result is False:
        raise OSError("推論設定を保存できませんでした。")
    return result if isinstance(result, Mapping) else read_state(canonical, room_name)


def build_target(
    provider: Any,
    google_model: Any = "",
    profile: Any = "",
    openai_model: Any = "",
    base_url: Any = "",
    anthropic_model: Any = "",
) -> dict[str, Any]:
    """UIの複数モデル欄から、現在providerに対応するtargetを作る。"""

    provider_value = str(provider or "").strip()
    normalized = provider_value.lower()
    if normalized in {"default", "inherit", "共通設定に従う"}:
        # roomのinheritは共通targetを選択した画面でもtarget識別が必要なため、
        # 呼び出し側で渡されたGoogle欄を基準にする。
        normalized = "google"
    if normalized in {"google (gemini)", "gemini", "google"}:
        return make_target("google", google_model)
    if normalized in {"anthropic (claude)", "claude", "anthropic"}:
        return make_target("anthropic", anthropic_model)
    if normalized in {"openai (公式)", "openai_official"}:
        return make_target("openai_official", openai_model, base_url, profile)
    # OpenRouter／xAIを含むOpenAI互換プロファイルはbase_url/profileで分離する。
    if normalized in {"openai", "openrouter", "open_router", "xai", "grok"}:
        return make_target("openai", openai_model, base_url, profile)
    # local等の選択中に、非表示のOpenAI欄を別対象として誤保存しない。
    return make_target(normalized, "")


__all__ = [
    "GLOBAL_SETTINGS_KEY",
    "ROOM_SETTINGS_KEY",
    "INHERIT_MARKER",
    "ResponseReasoningConflictError",
    "make_target",
    "target_key",
    "get_capabilities",
    "validate_preference",
    "build_target",
    "resolve_preference",
    "read_state",
    "save_preference",
]
