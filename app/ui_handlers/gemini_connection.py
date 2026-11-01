"""Gemini Developer API / Vertex AI 接続設定のUIハンドラ。

接続設定の保存・読込そのものは ``gemini_connection`` が担当し、このモジュールは
Gradioの固定出力契約、画面用の下書き、ルーム切替時の世代ガードだけを担当する。
APIキーは表示用のsummary/statusへ含めない。
"""

from __future__ import annotations

from copy import deepcopy
import importlib
from collections.abc import Mapping, Sequence
from typing import Any

import gradio as gr


OUTPUT_KEYS = (
    "inherit",
    "backend",
    "auth_mode",
    "project",
    "location",
    "key_ref",
    "summary",
    "state",
    "status",
)
OUTPUT_COUNT = len(OUTPUT_KEYS)

_DEFAULT_CONNECTION = {
    "backend": "developer",
    "auth_mode": "adc",
    "project": "",
    "location": "global",
    "key_ref": "shared",
}
_GOOGLE_PROVIDER_VALUES = {"vertex", "google", "gemini", "google (gemini native)"}
_MISSING = object()
_INVALID_CONNECTION = {
    "backend": "",
    "auth_mode": "",
    "project": "",
    "location": "global",
    "key_ref": "shared",
}
_INVALID_CONNECTION_MESSAGE = "保存されたGemini接続のbackendが不正です。再設定して保存してください。"
_SAVE_SUCCESS_MESSAGE = "Gemini接続設定を保存しました。"
_KEY_SAVE_SUCCESS_MESSAGE = "Vertex AIのAPIキーを保存しました。"
_STATE_SECRET_KEYS = {"api_key", "apikey", "api-key", "secret", "access_token", "token"}


class _InvalidConnectionError(ValueError):
    """保存済み接続を別の接続先へ暗黙変換せず停止するための内部例外。"""


def _service() -> Any:
    """基盤の接続設定モジュールを遅延 import する。"""

    return importlib.import_module("gemini_connection")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _bool(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"})


def _provider_is_google(value: Any) -> bool:
    text = _text(value).lower()
    return text in _GOOGLE_PROVIDER_VALUES or text.startswith("google") or text.startswith("gemini")


def _normalise_connection(value: Any, *, require_valid: bool = False) -> dict[str, Any]:
    """基盤の正規化を使い、UIへ返す値を固定フィールドへ絞る。"""

    service = _service()
    try:
        normalized = service.normalize_connection(value, require_valid=require_valid)
    except TypeError:
        # 開発途中の基盤でも、旧シグネチャなら位置引数で利用できるようにする。
        normalized = service.normalize_connection(value, require_valid)
    result = dict(_DEFAULT_CONNECTION)
    if isinstance(normalized, Mapping):
        for key in result:
            if key in normalized:
                result[key] = normalized[key]
    # core.normalize_connection()の実効値は秘密を解決した5フィールドで、
    # key_refを含まない。保存失敗時や未保存draftの表示では入力側の参照を
    # 維持しないと、ADC必須項目エラーなどから復帰する際にsharedへ戻ってしまう。
    raw_key_ref = value.get("key_ref", _MISSING) if isinstance(value, Mapping) else _MISSING
    if raw_key_ref is not _MISSING:
        result["key_ref"] = _text(raw_key_ref, "shared")
    else:
        result["key_ref"] = _text(result.get("key_ref"), "shared")
    backend = _text(result.get("backend"))
    # core.normalize_connection(require_valid=False) は不正 backend を保持する。
    # ここで Developer へ丸めると、壊れた保存値を別の課金先へ静かに切り替える
    # ため、画面には明示エラーを返して保存も行わない。
    if backend not in {"developer", "vertex"}:
        raise _InvalidConnectionError(_INVALID_CONNECTION_MESSAGE)
    result["backend"] = backend
    auth_mode = _text(result.get("auth_mode"), "adc")
    if auth_mode not in {"adc", "api_key"}:
        raise _InvalidConnectionError(
            "保存されたGemini接続のauth_modeが不正です。再設定して保存してください。"
        )
    result["auth_mode"] = auth_mode
    result["project"] = _text(result.get("project"))
    result["location"] = _text(result.get("location"), "global")
    result["key_ref"] = _text(result.get("key_ref"), "shared")
    if result["backend"] == "developer":
        result.update({"auth_mode": "adc", "project": "", "location": "global"})
    # Vertex APIキーでも project/location を指定できる。project が空なら
    # Express、入力されていれば指定プロジェクト／ロケーション接続として
    # そのまま画面へ返す。ADC用の保持値との混同はcore側で分離する。
    return result


def _state_for_ui(state: Any, room_name: str | None) -> dict[str, Any]:
    """基盤stateへUI側のルーム世代印を付ける。secretはsummary/statusへ流さない。"""

    result = _redact_state(state)
    result["_gemini_ui_room_name"] = room_name
    return result


def _redact_state(value: Any) -> Any:
    """ブラウザへ返すstateから接続秘密を再帰的に除去する。"""

    if isinstance(value, Mapping):
        return {
            key: _redact_state(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _STATE_SECRET_KEYS
        }
    if isinstance(value, list):
        return [_redact_state(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_state(item) for item in value)
    return deepcopy(value)


def _state_for_service(state: Any) -> Any:
    if not isinstance(state, Mapping):
        return state
    result = _redact_state(state)
    result.pop("_gemini_ui_room_name", None)
    return result


def _read_state(room_name: str | None) -> dict[str, Any]:
    state = _service().read_state(room_name=room_name)
    return _state_for_ui(state, room_name)


def _room_state_is_stale(room_name: str, state: Any) -> bool:
    if not isinstance(state, Mapping):
        return False
    marker = state.get("_gemini_ui_room_name", _MISSING)
    return marker is not _MISSING and marker != room_name


def _connection_has_inherit(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if _bool(value.get("inherit")):
        return True
    for key in ("value", "connection", "baseline", "local"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and _bool(nested.get("inherit")):
            return True
    return False


def _with_vertex_form(connection: dict[str, Any], room_name: str | None) -> dict[str, Any]:
    """実効接続に非秘密の保存済みVertexフォーム値を添える。"""

    result = dict(connection)
    result["_vertex_form"] = _service().get_vertex_settings(room_name=room_name)
    return result


def _saved_connection(room_name: str | None, provider: Any = None) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    service = _service()
    state = _read_state(room_name)
    global_value = service.get_connection(room_name=None, force_global=True)
    if room_name is None:
        return _with_vertex_form(_normalise_connection(global_value), None), False, state

    # roomのproviderがdefault／他providerなら接続先は常に共通設定を使う。
    if provider is not None and not _provider_is_google(provider):
        return _with_vertex_form(_normalise_connection(global_value), room_name), True, state

    room_value = service.get_connection(room_name=room_name, force_global=False)
    # 未登録 override は core の実効値が共通設定になり、read_state の
    # room_inherit/local.inherit は False のままでも room_override_present=False
    # で表される。未登録を個別設定として表示しない。
    if state.get("room_override_present", _MISSING) is False:
        inherit = True
    else:
        local = state.get("local") if isinstance(state, Mapping) else None
        inherit = (
            _connection_has_inherit(room_value)
            or _connection_has_inherit(state)
            or _bool(state.get("room_inherit"))
            or (isinstance(local, Mapping) and _bool(local.get("inherit")))
        )
    if inherit:
        return _with_vertex_form(_normalise_connection(global_value), room_name), True, state
    return _with_vertex_form(_normalise_connection(room_value), room_name), False, state


def _visibility(backend: Any, auth_mode: Any) -> tuple[bool, bool, bool]:
    is_vertex = _text(backend).lower() == "vertex"
    is_adc = is_vertex and _text(auth_mode).lower() == "adc"
    is_api_key = is_vertex and _text(auth_mode).lower() == "api_key"
    return is_vertex, is_adc, is_api_key


def _vertex_settings(connection: Mapping[str, Any], state: Any = None) -> Mapping[str, Any]:
    remembered = _mapping(connection.get("_vertex_form"))
    if remembered:
        return remembered
    room_name = _mapping(state).get("_gemini_ui_room_name")
    try:
        key_ref = connection.get("key_ref")
        if key_ref is None:
            return _service().get_vertex_settings(room_name=room_name)
        return _service().get_vertex_settings(room_name=room_name, key_ref=key_ref)
    except Exception:
        return {}


def _key_choices(settings: Mapping[str, Any]) -> list[tuple[str, str]]:
    choices = settings.get("key_choices")
    if not isinstance(choices, (list, tuple)):
        try:
            choices = _service().get_vertex_key_choices()
        except Exception:
            choices = []
    result: list[tuple[str, str]] = []
    for choice in choices or []:
        if isinstance(choice, (list, tuple)) and len(choice) >= 2:
            result.append((str(choice[0]), str(choice[1])))
    return result or [("共通Vertexキー", "shared")]


def _summary(connection: Mapping[str, Any], *, inherit: bool = False, state: Any = None) -> str:
    raw_backend = connection.get("backend", _MISSING)
    backend = "developer" if raw_backend is _MISSING or raw_backend is None else str(raw_backend).strip().lower()
    if inherit:
        return "共通設定を使用"
    if backend == "developer":
        return "Developer API"
    if backend != "vertex":
        return "保存された接続設定が不正です"
    auth_mode = _text(connection.get("auth_mode"), "adc")
    if auth_mode == "api_key":
        settings = _vertex_settings(connection, state)
        configured = "設定済み" if settings.get("api_key_configured") else "未設定"
        project = _text(connection.get("project"))
        location = _text(connection.get("location"), "global")
        target = f"{project} / {location}" if project else "Express"
        message = f"Vertex AI（APIキー）: {configured} / {target}"
        if settings.get("legacy_key_pending"):
            message += "（保存時にAPIキー管理へ引き継ぎます）"
        return message
    auth_text = "Vertex AI（ADC）"
    project = _text(connection.get("project"))
    location = _text(connection.get("location"), "global")
    suffix = f" / {project} / {location}" if project else f" / {location}"
    return f"{auth_text}{suffix}"


def _update(
    value: Any = _MISSING,
    *,
    visible: Any = _MISSING,
    interactive: Any = _MISSING,
    choices: Any = _MISSING,
    elem_classes: Any = _MISSING,
) -> dict[str, Any]:
    result: dict[str, Any] = {"__type__": "update"}
    if value is not _MISSING:
        result["value"] = value
    if visible is not _MISSING:
        result["visible"] = bool(visible)
    if interactive is not _MISSING:
        result["interactive"] = bool(interactive)
    if choices is not _MISSING:
        result["choices"] = choices
    if elem_classes is not _MISSING:
        result["elem_classes"] = list(elem_classes)
    return result


def _view_outputs(
    connection: Mapping[str, Any],
    *,
    inherit: bool,
    state: Mapping[str, Any],
    status: str = "",
    interactive: bool = True,
) -> tuple[Any, ...]:
    raw_backend = connection.get("backend", _MISSING)
    backend = "developer" if raw_backend is _MISSING or raw_backend is None else str(raw_backend).strip().lower()
    remembered = _mapping(connection.get("_vertex_form"))
    auth_mode = _text(connection.get("auth_mode"), "adc")
    if backend == "developer" and remembered:
        auth_mode = _text(remembered.get("auth_mode"), "adc")
    is_vertex, _is_adc, is_api_key = _visibility(backend, auth_mode)
    settings = _vertex_settings(connection, state)
    choices = _key_choices(settings)
    if remembered and settings.get("key_ref"):
        # get_connection()は参照を含まない実効5フィールドを返すため、
        # 保存済みの参照は非秘密metadataから復元する。
        key_ref = _text(settings.get("key_ref"), "shared")
    else:
        key_ref = _text(connection.get("key_ref"), _text(settings.get("key_ref"), "shared"))
    if not any(value == key_ref for _label, value in choices):
        key_ref = choices[0][1]
    form_project = connection.get("project")
    form_location = connection.get("location", "global")
    if remembered and backend == "developer":
        form_project = remembered.get("project", "")
        form_location = remembered.get("location", "global")
    form_interactive = bool(interactive)
    show_key_ref = bool(is_api_key and len(choices) > 1)
    return _ensure_output_count(
        (
            _update(value=bool(inherit), interactive=form_interactive),
            _update(value=backend, interactive=form_interactive),
            _update(
                value=auth_mode,
                interactive=form_interactive,
                elem_classes=[] if is_vertex else ["gemini-connection-hidden"],
            ),
            _update(
                value=_text(form_project),
                interactive=form_interactive and is_vertex,
                elem_classes=[] if is_vertex else ["gemini-connection-hidden"],
            ),
            _update(
                value=_text(form_location, "global"),
                interactive=form_interactive and is_vertex,
                elem_classes=[] if is_vertex else ["gemini-connection-hidden"],
            ),
            _update(
                value=key_ref,
                choices=choices,
                interactive=form_interactive and show_key_ref,
                elem_classes=[] if show_key_ref else ["gemini-connection-hidden"],
            ),
            _summary(connection, inherit=inherit, state=state),
            _redact_state(state),
            status,
        )
    )


def _ensure_output_count(values: Sequence[Any]) -> tuple[Any, ...]:
    """Gradio配線へ渡す戻り値を常に固定9個へそろえる。"""

    values = tuple(values)
    if len(values) >= OUTPUT_COUNT:
        return values[:OUTPUT_COUNT]
    return values + (gr.update(),) * (OUTPUT_COUNT - len(values))


def _error_outputs(
    message: str,
    *,
    connection: Mapping[str, Any] | None = None,
    inherit: bool = False,
    state: Any = None,
) -> tuple[Any, ...]:
    safe_connection = _error_connection(connection)
    safe_state = state if isinstance(state, Mapping) else {}
    return _view_outputs(
        safe_connection,
        inherit=inherit,
        state=_redact_state(safe_state),
        status=message,
    )


def _error_connection(value: Any) -> dict[str, Any]:
    """エラー画面へ安全に出す値を作る（不正値や秘密をstatusへ流さない）。"""

    if not isinstance(value, Mapping):
        return dict(_DEFAULT_CONNECTION)
    backend = _text(value.get("backend"))
    if backend not in {"developer", "vertex"}:
        # 不正値を Developer と表示すると、保存値の意図しない切替に見える。
        return dict(_INVALID_CONNECTION)
    auth_mode = _text(value.get("auth_mode"), "adc")
    if auth_mode not in {"adc", "api_key"}:
        return dict(_INVALID_CONNECTION)
    result = dict(_DEFAULT_CONNECTION)
    result["backend"] = backend
    result["auth_mode"] = auth_mode
    result["project"] = _text(value.get("project"))
    result["location"] = _text(value.get("location"), "global")
    result["key_ref"] = _text(value.get("key_ref"), "shared")
    if backend == "developer":
        result.update({"auth_mode": "adc", "project": "", "location": "global"})
    return result


def _draft_connection(
    backend: Any,
    auth_mode: Any,
    project: Any,
    location: Any,
    key_ref: Any,
    *,
    inherit: bool = False,
) -> dict[str, Any]:
    if inherit:
        return {"inherit": True}
    backend_value = _text(backend, "developer").lower()
    if backend_value == "developer":
        result = dict(_DEFAULT_CONNECTION)
        result["key_ref"] = _text(key_ref, "shared")
        return result
    auth_value = _text(auth_mode, "adc").lower()
    if auth_value == "api_key":
        return {
            "backend": "vertex",
            "auth_mode": "api_key",
            "project": _text(project),
            "location": _text(location, "global"),
            "key_ref": _text(key_ref, "shared"),
        }
    return {
        "backend": "vertex",
        "auth_mode": "adc",
        "project": _text(project),
        "location": _text(location, "global"),
        "key_ref": _text(key_ref, "shared"),
    }


def _draft_view(
    backend: Any,
    auth_mode: Any,
    project: Any,
    location: Any,
    key_ref: Any,
    *,
    inherit: bool,
    state: Any,
    interactive: bool = True,
) -> tuple[Any, ...]:
    draft = _draft_connection(
        backend,
        auth_mode,
        project,
        location,
        key_ref,
        inherit=inherit,
    )
    # 表示値は正規化後の保存値へ置き換えず、ユーザーの未保存draftを保持する。
    # 例えばAPIキー方式へ一度切り替えても、ADC用project/locationを戻せるようにする。
    backend_value = _text(backend, "developer").lower()
    if backend_value not in {"developer", "vertex"}:
        backend_value = "developer"
    auth_value = _text(auth_mode, "adc").lower()
    if auth_value not in {"adc", "api_key"}:
        auth_value = "adc"
    display = {
        "backend": backend_value,
        "auth_mode": auth_value,
        "project": _text(project),
        "location": _text(location, "global"),
        "key_ref": _text(key_ref, "shared"),
    }
    try:
        normalised = _normalise_connection(draft)
    except Exception:
        normalised = dict(_DEFAULT_CONNECTION)
    outputs = _view_outputs(
        display,
        inherit=bool(inherit),
        state=state if isinstance(state, Mapping) else {},
        interactive=interactive,
    )
    return outputs[:6] + (_summary(normalised, inherit=bool(inherit), state=state),) + outputs[7:]


def update_form(
    inherit: Any,
    backend: Any,
    auth_mode: Any,
    project: Any,
    location: Any,
    key_ref: Any,
    state: Any,
) -> tuple[Any, ...]:
    """下書きの選択変更に応じてVertex入力欄だけを表示切替する。"""

    result = _draft_view(
        backend,
        auth_mode,
        project,
        location,
        key_ref,
        inherit=_bool(inherit),
        state=state,
    )
    return result


def _recovery_state(room_name: str | None = None) -> dict[str, Any]:
    """不正な接続でも、読める保存内容のCAS印を復旧用に保持する。"""
    try:
        return _read_state(room_name)
    except Exception:
        return _state_for_ui({}, room_name)


def load_global(provider: Any = None) -> tuple[Any, ...]:
    """共通設定を保存値から読み込む（保存は行わない）。"""

    try:
        connection, _inherit, state = _saved_connection(None, provider)
        return _view_outputs(connection, inherit=False, state=state)
    except _InvalidConnectionError as exc:
        return _error_outputs(str(exc), connection=_INVALID_CONNECTION, state=_recovery_state())
    except Exception:
        return _error_outputs("Gemini接続設定を読み込めませんでした。", state=_recovery_state())


def load_room(room_name: Any, provider: Any = None) -> tuple[Any, ...]:
    """対象ルームの保存値を読み込む。ルーム名をstateへ記録して旧画面を拒否する。"""

    room = _text(room_name)
    if not room:
        return _error_outputs("ルームが選択されていません。")
    try:
        connection, inherit, state = _saved_connection(room, provider)
        return _view_outputs(connection, inherit=inherit, state=state)
    except _InvalidConnectionError as exc:
        return _error_outputs(
            str(exc),
            connection=_INVALID_CONNECTION,
            state=_recovery_state(room),
        )
    except Exception:
        return _error_outputs("Gemini接続設定を読み込めませんでした。", state=_recovery_state(room))


def _is_conflict(exc: BaseException) -> bool:
    conflict = getattr(_service(), "ConnectionConflictError", None)
    return bool(conflict and isinstance(exc, conflict)) or exc.__class__.__name__ == "ConnectionConflictError"


def _save(
    value: Mapping[str, Any],
    *,
    room_name: str | None,
    state: Any,
    inherit: bool,
    fallback_connection: Mapping[str, Any],
    provider: Any = None,
) -> tuple[Any, ...]:
    if room_name and _room_state_is_stale(room_name, state):
        return _error_outputs(
            "保存できませんでした。画面を再読込してからやり直してください。",
            connection=fallback_connection,
            inherit=inherit,
            state=state,
        )
    if room_name and provider is not None and not _provider_is_google(provider):
        # default/他providerのルームは共通設定を使う境界をUIでも守る。
        value = {"inherit": True}
        inherit = True
    try:
        service = _service()
        # roomの継承マーカーは接続辞書ではなくcore.save_connectionの専用値。
        # 先にnormalizeするとbackend不正として拒否されるため、そのまま渡す。
        if room_name and isinstance(value, Mapping) and value.get("inherit") is True:
            canonical = {"inherit": True}
        else:
            # 空欄APIキーの保持可否は、同じ保存対象の既存値を見られるcoreへ委譲する。
            # UIでrequire_valid=Trueを先に呼ぶと、新規設定だけでなく既存キー更新も
            # 拒否してしまう。
            canonical = value
        next_state = service.save_connection(
            canonical,
            room_name=room_name,
            state=_state_for_service(state),
        )
        ui_state = _state_for_ui(next_state, room_name)
        # save_connectionの戻りstateを正本にし、別ブラウザの更新を上書きしない。
        if inherit:
            shown = _with_vertex_form(dict(_DEFAULT_CONNECTION), room_name)
        else:
            try:
                effective = service.get_connection(
                    room_name=room_name,
                    force_global=room_name is None,
                )
            except Exception:
                effective = canonical
            shown = _with_vertex_form(_normalise_connection(effective), room_name)
        return _view_outputs(shown, inherit=inherit, state=ui_state, status=_SAVE_SUCCESS_MESSAGE)
    except Exception as exc:
        if _is_conflict(exc):
            message = "保存できませんでした。別の画面で設定が更新されています。再読込してください。"
        elif isinstance(exc, (ValueError, TypeError)):
            message = "保存できませんでした。必須項目を確認してください。"
        else:
            message = "Gemini接続設定を保存できませんでした。"
        return _error_outputs(
            message,
            connection=fallback_connection,
            inherit=inherit,
            state=state,
        )


def save_global(
    backend: Any,
    auth_mode: Any,
    project: Any,
    location: Any,
    key_ref: Any,
    state: Any,
) -> tuple[Any, ...]:
    """共通設定をCAS付きで明示保存する。"""

    value = _draft_connection(backend, auth_mode, project, location, key_ref)
    fallback = _normalise_connection(value)
    return _save(value, room_name=None, state=state, inherit=False, fallback_connection=fallback)


def save_room(
    room_name: Any,
    inherit: Any,
    backend: Any,
    auth_mode: Any,
    project: Any,
    location: Any,
    key_ref: Any,
    state: Any,
    provider: Any = None,
) -> tuple[Any, ...]:
    """ルーム設定をCAS付きで明示保存する。旧画面のstateは保存しない。"""

    room = _text(room_name)
    use_inherit = _bool(inherit)
    value = _draft_connection(
        backend,
        auth_mode,
        project,
        location,
        key_ref,
        inherit=use_inherit,
    )
    fallback = dict(_DEFAULT_CONNECTION) if use_inherit else _normalise_connection(value)
    if not room:
        return _error_outputs("ルームが選択されていません。", connection=fallback, inherit=use_inherit, state=state)
    return _save(
        value,
        room_name=room,
        state=state,
        inherit=use_inherit,
        fallback_connection=fallback,
        provider=provider,
    )


def _vertex_key_outputs(
    *, state: Any = None, status: str = "", key_ref: Any = _MISSING,
) -> tuple[Any, ...]:
    """共通台帳の選択・登録状態を返す（秘密値は常に空にする）。"""

    if key_ref is _MISSING:
        settings = _service().get_vertex_settings()
    else:
        settings = _service().get_vertex_settings(key_ref=_text(key_ref, "shared"))
    choices = _key_choices(settings)
    key_ref = _text(settings.get("key_ref"), "shared")
    if not any(value == key_ref for _label, value in choices):
        key_ref = choices[0][1]
    show_selector = len(choices) > 1
    summary = "登録状態: " + ("設定済み" if settings.get("api_key_configured") else "未登録")
    if settings.get("legacy_key_pending"):
        summary += "（保存時にAPIキー管理へ引き継ぎます）"
    return (
        _update(
            value=key_ref,
            choices=choices,
            interactive=show_selector,
            elem_classes=[] if show_selector else ["gemini-connection-hidden"],
        ),
        _update(value="", interactive=True),
        summary,
        _read_state(None) if state is None else _state_for_ui(state, None),
        status,
    )


def load_vertex_key() -> tuple[Any, ...]:
    try:
        return _vertex_key_outputs()
    except Exception:
        return (
            _update(value="shared", choices=[("共通Vertexキー", "shared")], interactive=False,
                    elem_classes=["gemini-connection-hidden"]),
            _update(value="", interactive=True),
            "登録状態を読み込めませんでした。",
            {},
            "",
        )


def load_vertex_key_for_ref(key_ref: Any, state: Any = None) -> tuple[Any, ...]:
    """登録先Dropdownの選択変更時に、対象キーの状態だけを読み込む。"""

    try:
        return _vertex_key_outputs(state=state, key_ref=key_ref)
    except Exception:
        # 選択されたref自体を再読込で失わず、二次的なcore例外で
        # ハンドラの出力数まで崩さない。秘密値は返さない。
        safe_state = _state_for_ui(state, None) if isinstance(state, Mapping) else {}
        return (
            _update(
                value=_text(key_ref, "shared"),
                choices=[("共通Vertexキー", "shared")],
                interactive=False,
                elem_classes=["gemini-connection-hidden"],
            ),
            _update(value="", interactive=True),
            "登録先のVertexキー状態を読み込めませんでした。",
            safe_state,
            "",
        )


def load_vertex_key_and_connection() -> tuple[Any, ...]:
    """明示再読込でキー管理とプロバイダ欄を同時に更新する（14出力）。"""

    return load_vertex_key() + load_global()


def save_vertex_key(api_key: Any, key_ref: Any, state: Any) -> tuple[Any, ...]:
    """選択した共通Vertexキーだけを登録し、接続先フォームの下書きを保持する。"""

    try:
        next_state = _service().save_vertex_api_key(
            api_key,
            state=_state_for_service(state),
            key_ref=_text(key_ref, "shared"),
        )
        key_outputs = _vertex_key_outputs(
            state=next_state,
            status=_KEY_SAVE_SUCCESS_MESSAGE,
            key_ref=key_ref,
        )
        connection_outputs = load_global()
        # プロバイダ側の未保存入力を変更せず、choices・summary・stateだけ同期する。
        choices = _key_choices(_service().get_vertex_settings())
        provider_sync = (
            # キー更新ではentry数は変わらない。未保存の認証方式に応じた
            # 表示状態・選択値を保存済み接続の値で巻き戻さない。
            _update(choices=choices),
            connection_outputs[6],
            _state_for_ui(next_state, None),
            _update(),
        )
        return key_outputs + tuple(_update() for _ in range(5)) + provider_sync
    except Exception as exc:
        if _is_conflict(exc):
            message = "保存できませんでした。別の画面で設定が更新されています。再読込してください。"
        elif not _text(api_key):
            message = "保存できませんでした。新しいAPIキーを入力してください。"
        else:
            message = "Vertex AIのAPIキーを保存できませんでした。"
        return (
            _update(value=_text(key_ref, "shared"), interactive=True),
            _update(value="", interactive=True),
            _update(),
            state,
            message,
        ) + tuple(_update() for _ in range(OUTPUT_COUNT))


def sync_vertex_key_after_connection_save(status: Any) -> tuple[Any, ...]:
    """接続先の保存成功時だけ、独立したキー登録状態を更新する。"""

    if status != _SAVE_SUCCESS_MESSAGE:
        return tuple(_update() for _ in range(5))
    return load_vertex_key()


def sync_vertex_key_choices_after_room_save(status: Any, key_ref: Any = None) -> tuple[Any, ...]:
    """ルーム保存で旧個別キーが台帳へ移行された時だけ選択肢を再同期する。"""

    if status != _SAVE_SUCCESS_MESSAGE:
        return tuple(_update() for _ in range(5))
    try:
        # ルーム保存で新しい旧キーentryが追加されても、管理パネルで選択中の
        # 登録先を共通キーへ戻さず、そのrefの登録状態とCAS stateを再読込する。
        outputs = _vertex_key_outputs(key_ref=_text(key_ref, "shared"))
        selector = outputs[0]
        return (
            _update(
                value=selector.get("value", _text(key_ref, "shared")),
                choices=selector.get("choices", []),
                interactive=selector.get("interactive", False),
                elem_classes=selector.get("elem_classes", ["gemini-connection-hidden"]),
            ),
            _update(),
            outputs[2],
            outputs[3],
            _update(),
        )
    except Exception:
        return tuple(_update() for _ in range(5))


def initial_panel_values(room_name: str | None = None, provider: Any = None) -> dict[str, Any]:
    """パネル生成時の値を保存値から一度だけ取得する。"""

    if room_name:
        outputs = load_room(room_name, provider)
    else:
        outputs = load_global(provider)
    return {
        key: outputs[index]
        for index, key in enumerate(OUTPUT_KEYS)
    } | {"outputs": outputs}


__all__ = [
    "OUTPUT_KEYS",
    "OUTPUT_COUNT",
    "_ensure_output_count",
    "initial_panel_values",
    "load_global",
    "load_vertex_key",
    "load_vertex_key_for_ref",
    "load_vertex_key_and_connection",
    "save_vertex_key",
    "sync_vertex_key_after_connection_save",
    "sync_vertex_key_choices_after_room_save",
    "load_room",
    "save_global",
    "save_room",
    "update_form",
]
