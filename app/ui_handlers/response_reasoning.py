"""応答モデル推論設定パネルのread/preview/saveハンドラ。

Gradioのレイアウトは親側へ残し、このモジュールは次の5出力だけを返す。

``(effort_update, budget_update, summary_markdown, state, status_markdown)``

モデル本体の選択・保存は既存のプロファイル／ルーム設定UIが担当する。この
パネルは受け取った選択からtargetを計算し、推論設定のpreviewと明示保存だけを
行う。設定のCAS基準は ``state`` に含め、古い画面からの巻き戻しを拒否する。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

import config_manager
import response_reasoning_settings as settings


OUTPUT_COUNT = 5
LEGACY_EFFORT = "legacy"
INHERIT_EFFORT = "inherit"

_EFFORT_LABELS = {
    "default": "モデルの既定に従う",
    INHERIT_EFFORT: "共通設定に従う",
    LEGACY_EFFORT: "既存のGemini設定を引き継ぐ",
    "none": "推論なし（none）",
    "minimal": "最小（minimal）",
    "low": "低（low）",
    "medium": "中（medium）",
    "high": "高（high）",
    "xhigh": "非常に高い（xhigh）",
    "max": "最大（max）",
    "budget": "詳細: 思考トークン予算",
}


def _update(**kwargs: Any) -> dict[str, Any]:
    """Gradio update互換の軽量dict（テスト時にGradioを起動しない）。"""

    return {"__type__": "update", **kwargs}


def _noop_update() -> dict[str, str]:
    return {"__type__": "update"}


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _global_selection() -> dict[str, str]:
    """保存済み共通選択値を読む。モデル一覧取得や外部通信は行わない。"""

    # CONFIG_GLOBALは別画面の更新前キャッシュになり得るため、diskを正本に
    # 最新値を読む。初回起動やテストでdiskが空の場合だけ既存キャッシュを
    # 補助的に使い、同じキーはdisk側を優先する。
    cached_config = config_manager.CONFIG_GLOBAL if isinstance(config_manager.CONFIG_GLOBAL, Mapping) else {}
    try:
        disk_config = config_manager.load_config_file()
    except Exception:
        disk_config = {}
    global_config = dict(cached_config)
    if isinstance(disk_config, Mapping):
        global_config.update(disk_config)
    provider = _as_text(global_config.get("active_provider")) or "google"
    google_model = _as_text(global_config.get("last_model")) or _as_text(config_manager.DEFAULT_MODEL_GLOBAL)
    profile = _as_text(global_config.get("active_openai_profile")) or "OpenRouter"
    openai_model = ""
    base_url = ""
    try:
        profiles = global_config.get("openai_provider_settings", [])
        if isinstance(profiles, list):
            active_profile = next(
                (
                    item
                    for item in profiles
                    if isinstance(item, Mapping) and _as_text(item.get("name")) == profile
                ),
                {},
            )
            if isinstance(active_profile, Mapping):
                openai_model = _as_text(active_profile.get("default_model"))
                base_url = _as_text(active_profile.get("base_url"))
    except Exception:
        pass
    anthropic_model = _as_text(global_config.get("anthropic_default_model"))
    return {
        "provider": provider,
        "google_model": google_model,
        "profile": profile,
        "openai_model": openai_model,
        "base_url": base_url,
        "anthropic_model": anthropic_model,
    }


def _room_selection(room_name: str) -> dict[str, str]:
    """保存済みルーム選択値を読む。room configが無い場合も共通へ戻す。"""

    global_selection = _global_selection()
    config = None
    try:
        config = __import__("room_manager").get_room_config(room_name)
    except Exception:
        config = None
    overrides = config.get("override_settings", {}) if isinstance(config, Mapping) else {}
    if not isinstance(overrides, Mapping):
        overrides = {}
    provider = _as_text(overrides.get("provider")) or "default"
    openai_settings = overrides.get("openai_settings")
    if not isinstance(openai_settings, Mapping):
        openai_settings = {}
    anthropic_settings = overrides.get("anthropic_settings")
    if not isinstance(anthropic_settings, Mapping):
        anthropic_settings = {}
    model_name = _as_text(overrides.get("model_name"))
    return {
        "provider": provider,
        "google_model": model_name or global_selection["google_model"],
        "profile": _as_text(openai_settings.get("profile")),
        "openai_model": _as_text(openai_settings.get("model")),
        "base_url": _as_text(openai_settings.get("base_url")),
        "anthropic_model": _as_text(anthropic_settings.get("model")),
    }


def _with_gemini_connection(target: dict[str, Any], room_name: str | None) -> dict[str, Any]:
    if target.get("provider") == "google":
        return settings.make_gemini_target(target.get("model"), room_name)
    return target


def _target_for_selection(
    provider: Any,
    google_model: Any,
    profile: Any,
    openai_model: Any,
    base_url: Any,
    anthropic_model: Any,
    *,
    room_name: str | None = None,
) -> dict[str, Any]:
    # 「共通設定に従う」は保存済み共通targetを使う。画面上の未保存previewの
    # model/base_urlが、room側の継承targetへ混入することを防ぐ。
    provider_text = _as_text(provider).lower()
    if room_name and provider_text in {"default", "inherit", "共通設定に従う"}:
        common = _global_selection()
        target = settings.build_target(
            common["provider"],
            common["google_model"],
            common["profile"],
            common["openai_model"],
            common["base_url"],
            common["anthropic_model"],
        )
        return _with_gemini_connection(target, room_name)
    target = settings.build_target(
        "google" if provider_text == "vertex" else provider,
        google_model,
        profile,
        openai_model,
        base_url,
        anthropic_model,
    )

    return _with_gemini_connection(target, room_name)


def _legacy_level_for_view(target: Mapping[str, Any], state: Mapping[str, Any]) -> str | None:
    if state.get("source") != "legacy":
        return None
    if str(target.get("provider") or "").lower() not in {"google", "gemini"}:
        return None
    value = state.get("legacy_thinking_level")
    return _as_text(value).lower() if value else None


def _effort_choice(value: str) -> tuple[str, str]:
    return (_EFFORT_LABELS.get(value, value), value)


def _effort_choices(capabilities: Mapping[str, Any], *, room: bool, legacy: bool) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    if room:
        choices.append(_effort_choice(INHERIT_EFFORT))
    if legacy:
        choices.append(_effort_choice(LEGACY_EFFORT))
    choices.append(_effort_choice("default"))
    for value in capabilities.get("efforts", []) if isinstance(capabilities, Mapping) else []:
        value = _as_text(value).lower()
        if value and value not in {choice[1] for choice in choices} and value != "budget":
            choices.append(_effort_choice(value))
    if capabilities.get("budget") and "budget" not in {choice[1] for choice in choices}:
        choices.append(_effort_choice("budget"))
    return choices


def _supported_effort_values(
    capabilities: Mapping[str, Any], *, room: bool, legacy: bool,
) -> set[str]:
    return {
        value
        for _label, value in _effort_choices(capabilities, room=room, legacy=legacy)
    }


def _raw_saved_effort(state: Mapping[str, Any], *, room: bool) -> str | None:
    """現在対象に保存されている生のeffort値を返す。"""

    baseline = state.get("baseline") if isinstance(state.get("baseline"), Mapping) else {}
    if baseline.get("kind") == "value":
        value = baseline.get("value")
        if isinstance(value, Mapping) and isinstance(value.get("effort"), str):
            return value["effort"].strip().lower()
    if room:
        global_baseline = state.get("global_baseline")
        if isinstance(global_baseline, Mapping) and global_baseline.get("kind") == "value":
            value = global_baseline.get("value")
            if isinstance(value, Mapping) and isinstance(value.get("effort"), str):
                return value["effort"].strip().lower()
    return None


def _display_effort(
    state: Mapping[str, Any],
    *,
    room: bool,
    capabilities: Mapping[str, Any],
) -> str:
    legacy = _legacy_level_for_view(state.get("target", {}), state)
    supported = _supported_effort_values(
        capabilities,
        room=room,
        legacy=bool(legacy),
    )
    baseline = state.get("baseline") if isinstance(state.get("baseline"), Mapping) else {}
    local_value = baseline.get("value") if baseline.get("kind") == "value" else None
    if room and baseline.get("kind") in {"missing", "inherit"}:
        return INHERIT_EFFORT
    if isinstance(local_value, Mapping) and isinstance(local_value.get("effort"), str):
        value = local_value["effort"].strip().lower()
        return value if value in supported else "default"
    effective = state.get("effective")
    if isinstance(effective, Mapping) and isinstance(effective.get("effort"), str):
        value = str(effective["effort"]).strip().lower()
        return value if value in supported else "default"
    if legacy:
        return LEGACY_EFFORT
    return "default"


def _unsupported_saved_effort(
    state: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    *,
    room: bool,
) -> str | None:
    raw = _raw_saved_effort(state, room=room)
    if not raw:
        return None
    legacy = bool(_legacy_level_for_view(state.get("target", {}), state))
    supported = _supported_effort_values(capabilities, room=room, legacy=legacy)
    return raw if raw not in supported else None


def _effective_text(
    target: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    room: bool,
) -> str:
    legacy = _legacy_level_for_view(target, state)
    if legacy:
        return f"{_EFFORT_LABELS.get(legacy, legacy)}（既存Gemini設定）"
    effective = state.get("effective") if isinstance(state.get("effective"), Mapping) else {}
    effort = _as_text(effective.get("effort")) or "default"
    if _unsupported_saved_effort(state, capabilities, room=room):
        effort = "default"
    if effort == "budget":
        applied = f"{effective['budget_tokens']:,}トークン"
    else:
        applied = _EFFORT_LABELS.get(effort, effort)
    if room and state.get("source") == "global":
        applied += "（共通）"
    return applied


def _summary(
    target: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    room: bool,
    active_target: Mapping[str, Any] | None = None,
) -> str:
    provider = _as_text(target.get("provider")) or "unknown"
    model = _as_text(target.get("model")) or "(モデル未選択)"
    profile = _as_text(target.get("profile"))
    target_text = f"{provider} / {model}"
    if profile:
        target_text += f" / {profile}"
    if active_target is not None and settings.target_key(active_target) != settings.target_key(target):
        target_text += "（編集中）"
        active_state = settings.read_state(active_target, state.get("room_name") if room else None)
        applied = _effective_text(
            active_target, settings.get_capabilities(active_target), active_state, room=room,
        )
        applied += f"（{_as_text(active_target.get('model')) or 'モデル未選択'}）"
    else:
        applied = _effective_text(target, capabilities, state, room=room)
    return f"対象モデル: `{target_text}`  \n現在の実効値: **{applied}**"


def _view(
    target: Mapping[str, Any],
    *,
    room_name: str | None,
    status: str,
) -> dict[str, Any]:
    canonical = settings.make_target(
        target.get("provider"), target.get("model"), target.get("base_url"), target.get("profile")
    )
    capabilities = settings.get_capabilities(canonical)
    state = settings.read_state(canonical, room_name)
    room = bool(room_name)
    effort = _display_effort(state, room=room, capabilities=capabilities)
    effective = state.get("effective") if isinstance(state.get("effective"), Mapping) else {}
    budget = effective.get("budget_tokens") if effort == "budget" else None
    choices = _effort_choices(
        capabilities,
        room=room,
        legacy=bool(_legacy_level_for_view(canonical, state)),
    )
    selector_interactive = bool(
        room
        or capabilities.get("efforts")
        or capabilities.get("budget")
        or _legacy_level_for_view(canonical, state)
    )
    # プロファイルのモデル選択は未保存でpreviewされる場合がある。
    # 選択対象の保存値を、実際に応答するモデルの値と誤認させない。
    saved_selection = _room_selection(room_name) if room_name else _global_selection()
    active_target = _target_for_selection(
        saved_selection["provider"], saved_selection["google_model"], saved_selection["profile"],
        saved_selection["openai_model"], saved_selection["base_url"], saved_selection["anthropic_model"],
        room_name=room_name,
    )
    summary = _summary(canonical, capabilities, state, room=room, active_target=active_target)
    return {
        "effort": _update(choices=choices, value=effort, interactive=selector_interactive),
        "budget": _update(
            value=budget,
            visible=bool(capabilities.get("budget")),
            interactive=bool(capabilities.get("budget")) and effort == "budget",
        ),
        "summary": summary,
        "state": state,
        "status": status,
        "target": canonical,
        "target_key": settings.target_key(canonical),
        "capabilities": capabilities,
        "effort_value": effort,
        "budget_value": budget,
    }


def update_budget(effort: Any, state: Any) -> dict[str, Any]:
    """effort変更時にbudget入力の表示・編集可否だけを更新する。"""

    selected = _as_text(effort).lower()
    state_mapping = state if isinstance(state, Mapping) else {}
    target = state_mapping.get("target")
    if not isinstance(target, Mapping):
        return _noop_update()
    capabilities = settings.get_capabilities(target)
    supports_budget = bool(capabilities.get("budget"))
    effective = state_mapping.get("effective")
    value = None
    if supports_budget and selected == "budget" and isinstance(effective, Mapping):
        value = effective.get("budget_tokens")
    if supports_budget and selected == "budget" and value is None:
        value = 1024
    return _update(
        value=value,
        visible=supports_budget,
        interactive=supports_budget and selected == "budget",
    )


def _outputs(view: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        view["effort"],
        view["budget"],
        view["summary"],
        view["state"],
        view["status"],
    )


def _error_outputs(message: str, state: Any = None) -> tuple[Any, ...]:
    return (_noop_update(), _noop_update(), _noop_update(), state, message)


def _load(
    provider: Any,
    google_model: Any,
    profile: Any,
    openai_model: Any,
    base_url: Any,
    anthropic_model: Any,
    *,
    room_name: str | None,
) -> tuple[Any, ...]:
    try:
        target = _target_for_selection(
            provider,
            google_model,
            profile,
            openai_model,
            base_url,
            anthropic_model,
            room_name=room_name,
        )
        return _outputs(_view(target, room_name=room_name, status=""))
    except Exception as exc:
        return _error_outputs(f"推論設定の読み込みに失敗しました: {exc}")


def load_global(
    provider: Any,
    google_model: Any,
    profile: Any,
    openai_model: Any,
    base_url: Any,
    anthropic_model: Any,
) -> tuple[Any, ...]:
    """共通targetをread-onlyでpreviewする。"""

    return _load(provider, google_model, profile, openai_model, base_url, anthropic_model, room_name=None)


def load_room(
    room_name: str,
    provider: Any,
    google_model: Any,
    profile: Any,
    openai_model: Any,
    base_url: Any,
    anthropic_model: Any,
) -> tuple[Any, ...]:
    """ルームtargetをread-onlyでpreviewする。"""

    return _load(provider, google_model, profile, openai_model, base_url, anthropic_model, room_name=room_name)


def _budget_value(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("budget_tokensは整数で指定してください。")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value <= 0:
        raise ValueError("budget_tokensは1以上の整数で指定してください。")
    return value


def _preference_from_inputs(effort: Any, budget: Any, *, room: bool) -> dict[str, Any] | None:
    selected = _as_text(effort).lower()
    if selected in {INHERIT_EFFORT, LEGACY_EFFORT}:
        if not room and selected == INHERIT_EFFORT:
            raise ValueError("共通設定ではinheritを指定できません。")
        return None
    if selected == "budget":
        return {"effort": "budget", "budget_tokens": _budget_value(budget)}
    if not selected:
        selected = "default"
    return {"effort": selected}


def _save(
    provider: Any,
    google_model: Any,
    profile: Any,
    openai_model: Any,
    base_url: Any,
    anthropic_model: Any,
    effort: Any,
    budget: Any,
    state: Any,
    *,
    room_name: str | None,
) -> tuple[Any, ...]:
    try:
        target = _target_for_selection(
            provider,
            google_model,
            profile,
            openai_model,
            base_url,
            anthropic_model,
            room_name=room_name,
        )
        preference = _preference_from_inputs(effort, budget, room=bool(room_name))
        next_state = settings.save_preference(target, preference, room_name=room_name, state=state)
        view = _view(target, room_name=room_name, status="推論設定を保存しました。")
        # save_preferenceの再読結果を使うことで、保存後のstateを画面へ返す。
        view["state"] = next_state
        return _outputs(view)
    except settings.ResponseReasoningConflictError as exc:
        return _error_outputs(f"保存できませんでした: {exc}", state)
    except (ValueError, OSError, TypeError) as exc:
        return _error_outputs(f"推論設定を保存できませんでした: {exc}", state)


def save_global(
    provider: Any,
    google_model: Any,
    profile: Any,
    openai_model: Any,
    base_url: Any,
    anthropic_model: Any,
    effort: Any,
    budget: Any,
    state: Any,
) -> tuple[Any, ...]:
    """明示保存ボタンから共通targetだけをCAS付き保存する。"""

    return _save(
        provider,
        google_model,
        profile,
        openai_model,
        base_url,
        anthropic_model,
        effort,
        budget,
        state,
        room_name=None,
    )


def save_room(
    room_name: str,
    provider: Any,
    google_model: Any,
    profile: Any,
    openai_model: Any,
    base_url: Any,
    anthropic_model: Any,
    effort: Any,
    budget: Any,
    state: Any,
) -> tuple[Any, ...]:
    """明示保存ボタンから対象ルーム・targetだけをCAS付き保存する。"""

    return _save(
        provider,
        google_model,
        profile,
        openai_model,
        base_url,
        anthropic_model,
        effort,
        budget,
        state,
        room_name=room_name,
    )


def initial_panel_values(room_name: str | None = None) -> dict[str, Any]:
    """設定画面生成時に使う初期5出力を返す（外部通信なし）。"""

    selection = _room_selection(room_name) if room_name else _global_selection()
    if room_name:
        output = load_room(
            room_name,
            selection["provider"],
            selection["google_model"],
            selection["profile"],
            selection["openai_model"],
            selection["base_url"],
            selection["anthropic_model"],
        )
    else:
        output = load_global(
            selection["provider"],
            selection["google_model"],
            selection["profile"],
            selection["openai_model"],
            selection["base_url"],
            selection["anthropic_model"],
        )
    return {
        "selection": selection,
        "effort": output[0],
        "budget": output[1],
        "summary": output[2],
        "state": output[3],
        "status": output[4],
        "outputs": output,
    }


__all__ = [
    "OUTPUT_COUNT",
    "load_global",
    "load_room",
    "save_global",
    "save_room",
    "update_budget",
    "initial_panel_values",
]
