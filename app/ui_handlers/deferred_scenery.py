"""会話の完了と独立した情景更新。UIの結果はsession state経由で返す。"""
from __future__ import annotations

from dataclasses import dataclass
import os

import gradio as gr

import config_manager
import utils
from agent.scenery_manager import generate_scenery_context, get_scenery_refresh_identity
from scenery_refresh_jobs import SceneryRefreshState
from weather_service import WeatherService


@dataclass(frozen=True)
class SceneryPresentation:
    room: str
    fingerprint: str
    text: str | None
    image: str | None
    style: str
    assets: tuple[tuple[str, int, int], ...]


def create_deferred_scenery_state():
    return SceneryRefreshState()


def close_deferred_scenery_state(state):
    if isinstance(state, SceneryRefreshState):
        state.close()


def handle_cancel_deferred_scenery(state):
    if isinstance(state, SceneryRefreshState):
        state.invalidate()
    return gr.update(active=False)


def handle_after_chat_timers(state):
    active = isinstance(state, SceneryRefreshState) and state.needs_polling()
    return gr.update(active=True), gr.update(active=active)


def _asset_stamp(path):
    if not path:
        return None
    try:
        info = os.stat(path)
        return str(path), info.st_size, info.st_mtime_ns
    except OSError:
        return None


def _compute_scenery(state, token, room, api_key):
    """外部取得・PIL処理を含むため、必ずbounded workerからだけ呼ぶ。"""
    from .styling import _generate_style_from_settings, _resolve_background_image

    if not state.is_current(token, room):
        return None
    settings = config_manager.get_effective_settings(room)
    enabled = settings.get("enable_scenery_system", False)
    weather = config_manager.load_config_file().get("weather_settings", {})
    if enabled and weather.get("enable_scenery_reflection", False):
        # 更新が必要なweather取得もUIスレッドへ持ち込まない。
        WeatherService().get_cached_weather()
    identity = get_scenery_refresh_identity(room)
    if identity is None:
        return None

    def current():
        return state.is_current(token, room) and get_scenery_refresh_identity(room) == identity

    if not current():
        return None
    text, image = None, None
    if enabled:
        scene = generate_scenery_context(
            room, api_key, season_en=identity.season_en,
            time_of_day_en=identity.time_of_day_en, commit_guard=current,
        )
        if scene is None or not current():
            return None
        label, _, text = scene
        if not text or label.startswith("（"):
            # 通信失敗時のplaceholderで、それまでの表示を置き換えない。
            return None
        image = utils.find_scenery_image(
            room, identity.location_name, identity.season_en, identity.time_of_day_en,
        )
    settings = config_manager.get_effective_settings(room)
    background = _resolve_background_image(room, settings)
    assets = tuple(stamp for path in (image, background) if (stamp := _asset_stamp(path)) is not None)
    style = _generate_style_from_settings(room, settings)
    if not current() or any(_asset_stamp(path) != (path, size, mtime) for path, size, mtime in assets):
        return None
    return SceneryPresentation(room, identity.fingerprint, text, image, style, assets)


def schedule_deferred_scenery(state, token, room, api_key, probe=None):
    if not isinstance(state, SceneryRefreshState):
        return False

    def work():
        if probe is not None:
            return probe.run("deferred_scenery", _compute_scenery, state, token, room, api_key)
        return _compute_scenery(state, token, room, api_key)

    return state.schedule(token, room, work)


def handle_deferred_scenery_poll(selected_room, current_room, state):
    """短い受取処理だけ。取得待ち・生成・ファイル保存はしない。"""
    unchanged = (gr.update(), gr.update(), gr.update())
    if not isinstance(state, SceneryRefreshState):
        return (*unchanged, gr.update(active=False))
    room = str(selected_room or "")
    if not room or room != str(current_room or "") or not state.matches_room(room):
        state.invalidate()
        return (*unchanged, gr.update(active=False))
    if not state.has_result(room):
        return (*unchanged, gr.update(active=state.needs_polling()))

    def accept(result):
        if not isinstance(result, SceneryPresentation) or result.room != room:
            return False
        identity = get_scenery_refresh_identity(room)
        return (
            identity is not None and identity.fingerprint == result.fingerprint
            and all(_asset_stamp(path) == (path, size, mtime) for path, size, mtime in result.assets)
        )

    result = state.take(room, accept)
    if result is None:
        return (*unchanged, gr.update(active=state.needs_polling()))
    return (
        result.text if result.text is not None else gr.update(),
        gr.update(value=result.image) if result.text is not None else gr.update(),
        gr.update(value=result.style),
        gr.update(active=state.needs_polling()),
    )
