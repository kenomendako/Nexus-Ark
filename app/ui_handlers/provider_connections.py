"""接続先を先に選ぶ共通／個別UIの変換と保存。保存形式は既存providerを維持する。"""

from __future__ import annotations

from typing import Any

import gradio as gr
import gemini_connection as service
from ui_handlers import gemini_connection as connection_handlers

PROVIDER_CHOICES = [
    ("AI Studio（Gemini）", "google"),
    ("Vertex AI（Agent Platform）", "vertex"),
    ("OpenAI互換", "openai"),
    ("Anthropic", "anthropic"),
    ("ローカル", "local"),
]
ROOM_PROVIDER_CHOICES = [("共通設定に従う", "default"), *PROVIDER_CHOICES]
_HIDDEN = ["gemini-connection-hidden"]


def canonical_provider(choice: Any) -> str:
    return "google" if choice == "vertex" else str(choice or "default")


def route_visibility(choice: str) -> tuple[Any, Any]:
    return (
        gr.update(elem_classes=[] if choice == "google" else _HIDDEN),
        gr.update(elem_classes=[] if choice == "vertex" else _HIDDEN),
    )


def global_route_view() -> tuple[Any, ...]:
    try:
        choice = service.provider_choice()
    except Exception:
        gr.Warning("接続先を読み込めませんでした。設定ファイルを確認してください。")
        return (gr.skip(),) * 8
    category = canonical_provider(choice)
    return (
        gr.update(value=choice),
        *(gr.update(visible=category == kind) for kind in ("google", "openai", "anthropic")),
        gr.update(visible=False),
        gr.update(visible=category == "local"),
        *route_visibility(choice),
    )


def room_route_view(room_name: str) -> tuple[Any, ...]:
    try:
        choice = service.provider_choice(room_name) if room_name else "default"
    except Exception:
        return (gr.skip(),) * 9
    category = canonical_provider(choice)
    return (
        gr.update(value=choice),
        *(gr.update(visible=category == kind) for kind in ("google", "openai", "anthropic")),
        gr.update(visible=False),
        gr.update(visible=category == "local"),
        gr.update(visible=False),
        *route_visibility(choice),
    )


def _save(choice: str, state: Any, room_name: str | None = None) -> str:
    try:
        service.select_provider(choice, room_name=room_name,
                                state=connection_handlers._state_for_service(state))
        return "接続先を保存しました。"
    except service.ConnectionConflictError:
        message = "保存できませんでした。別の画面で設定が更新されています。再読込してください。"
    except Exception:
        message = "接続先を保存できませんでした。保存済みの設定を再表示します。"
    gr.Warning(message)
    return message


def select_global(choice: str, state: Any) -> tuple[Any, ...]:
    message = _save(choice, state)
    panel = connection_handlers.load_global()
    return global_route_view() + panel[:8] + (message,)


def select_room(room_name: str, choice: str, state: Any, switching: bool = False) -> tuple[Any, ...]:
    import ui_handlers

    if not room_name or switching or connection_handlers._room_state_is_stale(room_name, state):
        return (gr.skip(),) * 22
    message = _save(choice, state, room_name)
    try:
        actual = service.provider_choice(room_name)
    except Exception:
        return (gr.skip(),) * 20 + (message, message)
    groups = ui_handlers.handle_room_provider_change(canonical_provider(actual), room_name)
    panel = connection_handlers.load_room(room_name)
    return (gr.update(value=actual), *groups, *route_visibility(actual), *panel[:8], message, message)


def save_global_details(backend: Any, auth_mode: Any, project: Any, location: Any,
                        key_ref: Any, state: Any) -> tuple[Any, ...]:
    try:
        active = service.provider_choice()
    except Exception:
        active = None
    if active != "vertex":
        panel = connection_handlers.load_global()
        return panel[:8] + ("接続先が変更されています。再読込してください。",)
    return connection_handlers.save_global("vertex", auth_mode, project, location, key_ref, state)


def save_room_details(room_name: str, inherit: Any, backend: Any, auth_mode: Any, project: Any,
                      location: Any, key_ref: Any, state: Any, provider: Any = None) -> tuple[Any, ...]:
    try:
        active = service.provider_choice(room_name) if room_name else None
    except Exception:
        active = None
    if active != "vertex":
        panel = connection_handlers.load_room(room_name)
        return panel[:8] + ("接続先が変更されています。再読込してください。",)
    return connection_handlers.save_room(room_name, False, "vertex", auth_mode, project,
                                         location, key_ref, state, "google")


def acknowledge_common_change(room_name: str, state: Any) -> Any:
    """同じ画面の共通変更後だけCAS印を同期し、他画面のroom更新は承認しない。"""
    if not room_name or connection_handlers._room_state_is_stale(room_name, state):
        return gr.skip()
    try:
        latest = service.read_state(room_name)
    except Exception:
        return gr.skip()
    if any(state.get(key) != latest.get(key) for key in
           ("room_storage_id", "room_provider", "room_override_present", "room_inherit")):
        return gr.skip()
    return connection_handlers._state_for_ui(latest, room_name)


def initialize_global(initialized: bool) -> tuple[Any, ...]:
    """新しいブラウザセッションで最初に開いた時だけ正本へ同期する。"""
    if initialized:
        return (gr.skip(),) * 18
    return (True, *global_route_view(), *connection_handlers.load_global())


def initialize_room(room_name: str, initialized: bool) -> tuple[Any, ...]:
    if initialized or not room_name:
        return (gr.skip(),) * 19
    return (True, *room_route_view(room_name), *connection_handlers.load_room(room_name))
