"""ui_handlers のうち「サブエージェント委任（タスク一覧・実行ログ・設定・モデルティア）」ドメイン。

ui_handlers パッケージから再エクスポートされ、呼び出し側は従来どおり
ui_handlers.<関数名> でアクセスできる。（モジュール名は上位パッケージ
agent_delegation との混同を避けるため delegation とする。）
"""

from pathlib import Path
import json
import pandas as pd
import gradio as gr
import traceback
import agent_delegation.manager as agent_delegation_manager
import persona_contract
import gemini_api, config_manager, alarm_manager, room_manager, utils, constants, chatgpt_importer, claude_importer, generic_importer
import re

from ._common import _settings_status_message


AGENT_DELEGATION_TASK_COLUMNS = ["task_id", "room", "backend", "status", "tier", "scope", "created_at", "summary"]
_AGENT_DELEGATION_LIMIT_PROFILE_LABELS = {
    "local": "ローカル",
    "cloud_light": "軽量クラウド",
    "cloud_heavy": "高性能クラウド",
}


def _atelier_delegation_readiness_state(room_name: str) -> dict:
    """準備カード表示用の状態を保存値から組み立てる。"""
    room = str(room_name or "").strip()
    if not room:
        return {"room": "", "ready": False}
    effective = config_manager.get_effective_settings(room)
    delegation = effective.get("agent_delegation_settings", {}) or {}
    workspace = effective.get("persona_workspace", {}) or {}
    serve = config_manager.CONFIG_GLOBAL.get("atelier_serve_settings", {}) or {}
    enabled = bool(delegation.get("enabled", False))
    writable = bool(workspace.get("enabled", True)) and str(workspace.get("permission_tier") or "write") in {"write", "full"}
    wake = bool(delegation.get("wake_on_completion", False))
    served = bool(serve.get("enabled", False))
    return {
        "room": room,
        "enabled": enabled,
        "writable": writable,
        "wake": wake,
        "served": served,
        "required_done": sum((enabled, writable, served)),
        "ready": enabled and writable and wake and served,
    }


def build_atelier_delegation_readiness(room_name: str) -> str:
    """PWA制作・利用に必要な設定を、単一の明示カードHTMLで返す。"""
    state = _atelier_delegation_readiness_state(room_name)
    if not state.get("room"):
        return (
            '<div class="atelier-setup-card">'
            '<h3>PWA制作の準備</h3><p>ルームを選択すると準備状況を確認できます。</p>'
            '</div>'
        )
    if state["ready"]:
        return (
            '<div class="atelier-setup-card"><div class="atelier-setup-complete">'
            '<div class="atelier-setup-complete-icon">✅</div>'
            '<div><h3>PWA制作の準備ができています</h3>'
            '<p>チャットでペルソナに、作りたいアプリを頼めます。</p></div>'
            '</div></div>'
        )
    items = [
        (state["enabled"], "作業を別のエージェントへ任せる", "利用できます", "無効です"),
        (state["writable"], "アトリエに作品を作る", "作成できます", "「読み書き」にしてください"),
        (state["wake"], "完成したらペルソナから知らせる", "通知します", "システム報告のみです"),
        (state["served"], "スマホ・PCでアプリを開く", "配信できます", "配信が無効です"),
    ]
    rows = "".join(
        f'<div class="atelier-setup-row"><span>{"✅" if ok else "❌"}</span>'
        f'<strong>{label}</strong><span class="atelier-setup-detail">{yes if ok else no}</span></div>'
        for ok, label, yes, no in items
    )
    return (
        '<div class="atelier-setup-card">'
        '<h3>PWAをペルソナに作ってもらう準備</h3>'
        f'<div class="atelier-setup-list">{rows}</div>'
        f'<p class="atelier-setup-summary">必須の準備: {state["required_done"]}/3'
        '（完成時のペルソナ報告はおすすめ設定です）</p>'
        '<p class="atelier-setup-note">下のボタンで安全な初期設定をまとめて保存できます。'
        '外部データ連携や高度なコマンド操作は有効にしません。</p></div>'
    )


def load_atelier_delegation_readiness(room_name: str):
    """準備カード本文と、一括設定ボタンの表示状態を同期する。"""
    state = _atelier_delegation_readiness_state(room_name)
    return (
        gr.update(value=build_atelier_delegation_readiness(room_name)),
        gr.update(visible=not bool(state.get("ready"))),
    )


def handle_prepare_atelier_delegation(room_name: str):
    """初心者向けの安全な推奨値だけを差分保存する。"""
    room = str(room_name or "").strip()
    if not room:
        gr.Error("ルームを選択してください。")
        return (gr.update(),) * 8
    try:
        room_manager.update_room_override_nested(room, "agent_delegation_settings", {
            "enabled": True,
            "wake_on_completion": True,
            "wake_respect_quiet_hours": True,
        })
        room_manager.update_room_override_nested(room, "persona_workspace", {
            "enabled": True,
            "permission_tier": "write",
        })
        config_manager.save_atelier_serve_settings(enabled=True)
        config_manager.load_config()
        start_note = ""
        try:
            from atelier_serve.server import start_server
            serve = config_manager.CONFIG_GLOBAL.get("atelier_serve_settings", {}) or {}
            start_server(
                port=int(serve.get("port", 8765) or 8765),
                host=str(serve.get("host") or "0.0.0.0"),
                daemon=True,
            )
        except Exception as exc:
            start_note = f" 配信設定は保存しましたが、サーバー起動を確認できませんでした: {exc}"
        message = "おすすめ設定を保存しました。ペルソナへPWA制作を頼めます。" + start_note
        gr.Info(message)
        return (
            gr.update(value=build_atelier_delegation_readiness(room)),
            gr.update(value=True),
            gr.update(value=True),
            gr.update(value=True),
            gr.update(value="write"),
            gr.update(value=True),
            gr.update(value=message),
            gr.update(visible=False),
        )
    except Exception as exc:
        traceback.print_exc()
        gr.Error(f"おすすめ設定の保存に失敗しました: {exc}")
        return (gr.update(),) * 8


def handle_save_agent_delegation_settings(
    max_concurrent_tasks: int,
    max_turns: int,
    timeout_seconds: int,
    deleg_auto_tune_limits: bool = True,
    deleg_exec_provider_cat: str = "",
    deleg_exec_openai_profile: str = "",
    deleg_exec_model: str = "",
    wake_chain_max_depth: int = 2,
    wake_daily_cap: int = 10,
    wake_min_interval_minutes: int = 30,
    tier_fast_provider_cat: str = "",
    tier_fast_openai_profile: str = "",
    tier_fast_model: str = "",
    tier_balanced_provider_cat: str = "",
    tier_balanced_openai_profile: str = "",
    tier_balanced_model: str = "",
    tier_deep_provider_cat: str = "",
    tier_deep_openai_profile: str = "",
    tier_deep_model: str = "",
    task_tier_deep_research: str = "",
    task_tier_anthology: str = "",
    task_tier_review: str = "",
) -> str:
    """エージェント委任の全体既定を保存する。ルーム別ポリシーキーは保存しない。"""
    try:
        current = config_manager.CONFIG_GLOBAL.get("agent_delegation_settings", {}) if isinstance(config_manager.CONFIG_GLOBAL, dict) else {}
        settings = dict(current) if isinstance(current, dict) else {}

        def _tier(provider_cat: str, openai_profile: str, model: str) -> dict:
            return {
                "provider_cat": str(provider_cat or "").strip(),
                "model": str(model or "").strip(),
                "openai_profile": str(openai_profile or "").strip(),
            }

        task_model_tiers = dict(settings.get("task_model_tiers") or {}) if isinstance(settings.get("task_model_tiers"), dict) else {}
        for task_kind, tier_name in {
            "deep_research": task_tier_deep_research,
            "anthology": task_tier_anthology,
            "review": task_tier_review,
        }.items():
            normalized_tier = str(tier_name or "").strip().lower()
            if normalized_tier in {"fast", "balanced", "deep"}:
                task_model_tiers[task_kind] = normalized_tier
            else:
                task_model_tiers.pop(task_kind, None)

        settings.update({
            "max_concurrent_tasks": max(1, int(max_concurrent_tasks or 1)),
            "max_turns": max(3, int(max_turns or 20)),
            "timeout_seconds": max(30, int(timeout_seconds or 600)),
            "deleg_auto_tune_limits": bool(deleg_auto_tune_limits),
            "deleg_exec_provider_cat": str(deleg_exec_provider_cat or "").strip(),
            "deleg_exec_openai_profile": str(deleg_exec_openai_profile or "").strip(),
            "deleg_exec_model": str(deleg_exec_model or "").strip(),
            "wake_chain_max_depth": max(0, int(wake_chain_max_depth or 0)),
            "wake_daily_cap": max(0, int(wake_daily_cap or 0)),
            "wake_min_interval_minutes": max(0, int(wake_min_interval_minutes or 0)),
            "model_tiers": {
                "fast": _tier(tier_fast_provider_cat, tier_fast_openai_profile, tier_fast_model),
                "balanced": _tier(tier_balanced_provider_cat, tier_balanced_openai_profile, tier_balanced_model),
                "deep": _tier(tier_deep_provider_cat, tier_deep_openai_profile, tier_deep_model),
            },
            "task_model_tiers": task_model_tiers,
        })
        result = config_manager.update_config_keys({"agent_delegation_settings": settings})
        config_manager.load_config()
        status = _settings_status_message("共通設定", "エージェント委任の全体既定", result, False)
        gr.Info(status)
        return status
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"エージェント委任設定の保存中にエラーが発生しました: {e}")
        return "共通設定: エージェント委任設定の保存に失敗しました"


def handle_save_room_agent_delegation_settings(
    room_name: str,
    enabled: bool,
    permission_tier: str,
    allow_web_tools: bool,
    wake_on_completion: bool,
    wake_respect_quiet_hours: bool,
    deleg_exec_provider_cat: str = "default",
    deleg_exec_openai_profile: str = "",
    deleg_exec_model: str = "",
    deleg_review_iterations: int = 0,
    deleg_review_provider_cat: str = "default",
    deleg_review_openai_profile: str = "",
    deleg_review_model: str = "",
) -> str:
    """このルームのエージェント委任ポリシーをフルブロックで保存する。"""
    try:
        room = str(room_name or "").strip()
        if not room:
            gr.Error("ルームを選択してください。")
            return "ルーム別設定: エージェント委任ポリシーの保存に失敗しました"
        tier_map = {
            "読み取り（Read/Glob/Grep）": "read",
            "読み書き（Edit/Writeはroot_path内のみ）": "write",
            "フル（Bash許可・信頼フォルダ専用）": "full",
            "read": "read",
            "write": "write",
            "full": "full",
        }
        tier = tier_map.get(str(permission_tier or "").strip(), str(permission_tier or "read").strip() or "read")
        settings = {
            "enabled": bool(enabled),
            "permission_tier": tier,
            "allow_web_tools": bool(allow_web_tools),
            "wake_on_completion": bool(wake_on_completion),
            "wake_respect_quiet_hours": bool(wake_respect_quiet_hours),
            "deleg_exec_provider_cat": str(deleg_exec_provider_cat or "default").strip(),
            "deleg_exec_openai_profile": str(deleg_exec_openai_profile or "").strip(),
            "deleg_exec_model": str(deleg_exec_model or "").strip(),
            "deleg_review_iterations": max(0, min(3, int(deleg_review_iterations or 0))),
            "deleg_review_provider_cat": str(deleg_review_provider_cat or "default").strip(),
            "deleg_review_openai_profile": str(deleg_review_openai_profile or "").strip(),
            "deleg_review_model": str(deleg_review_model or "").strip(),
        }
        result = room_manager.update_room_override_key(room, "agent_delegation_settings", settings)
        status = _settings_status_message(room, "エージェント委任ポリシー", result, False)
        if result:
            gr.Info(status)
        else:
            gr.Error(status)
        return status
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"エージェント委任ポリシーの保存中にエラーが発生しました: {e}")
        return "ルーム別設定: エージェント委任ポリシーの保存に失敗しました"


def _delegation_model_choices(provider_cat: str, profile_name: str = "") -> list:
    provider = str(provider_cat or "").strip()
    if provider in {"", "default"}:
        return []
    if provider == "google":
        return list(config_manager.AVAILABLE_MODELS_GLOBAL)
    if provider == "anthropic":
        return ["claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"]
    if provider == "openai":
        setting = config_manager.get_openai_setting_by_name(profile_name) or {}
        return list(setting.get("available_models", []) or [])
    if provider == "openai_official":
        return ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo", "o1-preview", "o1-mini", "o3-mini"]
    if provider == "local":
        return ["Local GGUF"]
    return []


def _delegation_model_updates(settings: dict, *, room_scope: bool) -> tuple:
    provider_default = "default" if room_scope else ""
    provider = str((settings or {}).get("deleg_exec_provider_cat") or provider_default).strip()
    if room_scope and provider in {"", "default"}:
        provider = "default"
    profile = str((settings or {}).get("deleg_exec_openai_profile") or config_manager.get_active_openai_profile_name() or "").strip()
    model = str((settings or {}).get("deleg_exec_model") or "").strip()
    choices = _delegation_model_choices(provider, profile)
    if model and model not in choices:
        choices = [model] + choices
    return (
        gr.update(value=provider),
        gr.update(choices=[s.get("name", "") for s in config_manager.get_openai_settings_list()], value=profile, visible=(provider in ["openai", "openai_official"])),
        gr.update(choices=choices, value=model),
    )


def _parse_contract_list(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    raw_items = []
    for line in text.splitlines():
        raw_items.extend(part.strip() for part in line.split(","))
    items = []
    for item in raw_items:
        if item and item not in items:
            items.append(item)
    return items


def _format_contract_list(values: list[str]) -> str:
    return "\n".join(str(item).strip() for item in values or [] if str(item).strip())


def load_room_persona_contract_ui(room_name: str):
    """ルーム切替・初期化時に Persona Contract UI を現在値へ同期する。"""
    try:
        contract = persona_contract.get_room_persona_contract(room_name) if room_name else {}
        address_terms = contract.get("address_terms") or {}
        validation = contract.get("validation") or {}
        return (
            gr.update(value=bool(contract.get("enabled", False))),
            gr.update(value=str(contract.get("persona_name") or "")),
            gr.update(value=str(contract.get("user_display_name") or "")),
            gr.update(value=_format_contract_list(address_terms.get("preferred") or [])),
            gr.update(value=_format_contract_list(address_terms.get("forbidden") or [])),
            gr.update(value=_format_contract_list(contract.get("required_terms") or [])),
            gr.update(value=_format_contract_list(contract.get("forbidden_terms") or [])),
            gr.update(value=_format_contract_list(contract.get("tone_rules") or [])),
            gr.update(value=str(validation.get("forbidden_terms") or "error")),
            gr.update(value=str(validation.get("required_terms") or "warning")),
            gr.update(value=str(validation.get("address_terms") or "warning")),
        )
    except Exception:
        traceback.print_exc()
        return tuple(gr.update() for _ in range(11))


def handle_save_room_persona_contract(
    room_name: str,
    enabled: bool,
    persona_name: str,
    user_display_name: str,
    preferred_address_terms: str,
    forbidden_address_terms: str,
    required_terms: str,
    forbidden_terms: str,
    tone_rules: str,
    forbidden_terms_severity: str,
    required_terms_severity: str,
    address_terms_severity: str,
) -> str:
    """Persona Contract を明示保存する。AIによる自動確定保存は行わない。"""
    try:
        room = str(room_name or "").strip()
        if not room:
            gr.Error("ルームを選択してください。")
            return "Persona Contract: 保存に失敗しました"
        contract = persona_contract.normalize_contract({
            "enabled": bool(enabled),
            "persona_name": str(persona_name or "").strip(),
            "user_display_name": str(user_display_name or "").strip(),
            "address_terms": {
                "preferred": _parse_contract_list(preferred_address_terms),
                "forbidden": _parse_contract_list(forbidden_address_terms),
            },
            "required_terms": _parse_contract_list(required_terms),
            "forbidden_terms": _parse_contract_list(forbidden_terms),
            "tone_rules": _parse_contract_list(tone_rules),
            "validation": {
                "forbidden_terms": str(forbidden_terms_severity or "error").strip(),
                "required_terms": str(required_terms_severity or "warning").strip(),
                "address_terms": str(address_terms_severity or "warning").strip(),
                "tone_rules": "warning",
            },
        })
        result = room_manager.update_room_override_key(room, "persona_contract", contract)
        status = _settings_status_message(room, "Persona Contract", result, False)
        if result:
            gr.Info(status)
        else:
            gr.Error(status)
        return status
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Persona Contract の保存中にエラーが発生しました: {e}")
        return "Persona Contract: 保存に失敗しました"


def _agent_delegation_limit_line(settings: dict) -> str:
    """委任の実効上限（最大ターン・タイムアウト）を1行で説明する。自動調整時はその旨を添える。"""
    max_turns = settings.get("max_turns")
    timeout = settings.get("timeout_seconds")
    if not max_turns or not timeout:
        return ""
    base = f"実効上限: 最大{max_turns}ターン / タイムアウト{timeout}秒"
    if settings.get("deleg_auto_tune_limits"):
        limit_profile = _AGENT_DELEGATION_LIMIT_PROFILE_LABELS.get(str(settings.get("deleg_auto_tuned_limit_profile") or ""), "")
        if limit_profile:
            return base + f"（モデルに応じて自動調整: {limit_profile}）"
        return base + "（自動調整: 委任実行モデル未設定のため保存値を使用）"
    return base + "（手動設定）"


def format_agent_delegation_backend_info(room_name: str) -> str:
    try:
        settings = agent_delegation_manager.get_agent_delegation_settings(room_name)
        info = agent_delegation_manager.describe_delegation_backend(room_name, settings)
        provider = str(info.get("provider") or "unknown")
        delegation_model = config_manager.get_effective_delegation_model(room_name)
        if delegation_model:
            provider_cat, model_name, _profile = delegation_model
            prefix = f"このルームの委任実行: **native**（{provider_cat} / {model_name}） ｜ 委任専用モデルで実行"
        else:
            prefix = f"このルームの委任実行: **native**（{provider}） ｜ 会話モデルで実行"
        limit_line = _agent_delegation_limit_line(settings)
        if limit_line:
            prefix = prefix + "\n\n" + limit_line
        if not info.get("tool_capable"):
            return prefix + "\n\n" + (
                "⚠️ 現在の会話プロバイダはツール呼び出しに対応していないため、"
                "このルームの会話中に委任ツールを呼べません。"
            )
        return prefix
    except Exception as exc:
        return f"委任バックエンド判定: 取得エラー（{type(exc).__name__}: {exc}）"


def _agent_delegation_metadata_dir() -> Path:
    return Path(constants.METADATA_DIR) / "agent_delegation"


def _agent_delegation_tasks_path() -> Path:
    return _agent_delegation_metadata_dir() / "tasks.json"


def _agent_delegation_log_path(task_id: str) -> Path:
    safe_task_id = Path(str(task_id or "").strip()).name
    return _agent_delegation_metadata_dir() / "logs" / f"{safe_task_id}.jsonl"


def _load_agent_delegation_tasks() -> list[dict]:
    path = _agent_delegation_tasks_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        raw_tasks = data.get("tasks", {}) if isinstance(data, dict) else {}
        if not isinstance(raw_tasks, dict):
            return []
        tasks = [task for task in raw_tasks.values() if isinstance(task, dict)]
        tasks.sort(key=lambda task: str(task.get("created_at") or task.get("updated_at") or ""), reverse=True)
        return tasks
    except Exception:
        traceback.print_exc()
        return []


def _task_summary_excerpt(task: dict, max_chars: int = 120) -> str:
    summary = str(task.get("summary") or task.get("error") or task.get("task_description") or "").strip()
    summary = re.sub(r"\s+", " ", summary)
    if len(summary) > max_chars:
        return summary[:max_chars - 1] + "…"
    return summary


def _agent_delegation_task_backend(task: dict) -> str:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    backend = str(metadata.get("backend") or "").strip()
    if backend:
        return backend
    auth_source = str(metadata.get("auth_source") or "").strip().lower()
    if auth_source.startswith("native:"):
        return "native"
    if auth_source:
        return "claude"
    return ""


def _agent_delegation_task_count_summary(tasks: list[dict]) -> str:
    """委任タスクの件数概要（状態別）。ネイティブ実行はコストを報告しないためコスト集計は廃止。"""
    total = len(tasks)
    running = sum(1 for t in tasks if t.get("status") in ("running", "pending"))
    done = sum(1 for t in tasks if t.get("status") == "done")
    failed = sum(1 for t in tasks if t.get("status") in ("failed", "cancelled", "partial"))
    clarify = sum(1 for t in tasks if t.get("status") == "needs_clarification")
    detail = []
    if running:
        detail.append(f"実行中/待機 {running}")
    if done:
        detail.append(f"完了 {done}")
    if failed:
        detail.append(f"失敗・中断 {failed}")
    if clarify:
        detail.append(f"確認待ち {clarify}")
    if detail:
        return f"委任タスク: {total}件（" + " / ".join(detail) + "）"
    return f"委任タスク: {total}件"


def _agent_delegation_scope_label(task: dict) -> str:
    """委任タスクのワークスペース種別を分かりやすいラベルにする（デュアルスコープ可視化）。"""
    kind = str(task.get("workspace_kind") or "project").strip().lower()
    return {
        "project": "プロジェクト探索",
        "persona": "アトリエ",
        "persona_project_read": "アトリエ＋project参照",
    }.get(kind, kind or "?")


def _agent_delegation_progress_label(task: dict) -> str:
    """実行中タスクの進捗（ターン数・直近ツール）を短い人間可読ラベルにする。"""
    progress = task.get("progress") if isinstance(task, dict) else None
    if not isinstance(progress, dict):
        return ""
    parts = []
    turn = progress.get("turn")
    max_turns = progress.get("max_turns")
    if turn:
        parts.append(f"ターン{turn}/{max_turns}" if max_turns else f"ターン{turn}")
    phase = str(progress.get("phase") or "")
    last_tool = str(progress.get("last_tool") or "")
    if phase == "thinking":
        parts.append("思考中")
    elif last_tool:
        parts.append(f"直近{last_tool}")
    total = progress.get("tool_call_total")
    if total:
        parts.append(f"ツール{total}回")
    denied = progress.get("denied_tool_count")
    if denied:
        parts.append(f"拒否{denied}")
    return "・".join(parts)


def _agent_delegation_status_reason(task: dict) -> str:
    """状態を、失敗・中断の理由まで分かる人間可読な文言にする。"""
    status = str(task.get("status") or "").strip()
    error = str(task.get("error") or "")
    lowered = error.lower()
    labels = {
        "done": "完了",
        "running": "実行中",
        "pending": "待機中",
        "partial": "途中終了（ターン上限）",
        "needs_clarification": "確認待ち（ワークスペース外の可能性）",
        "failed": "失敗",
        "cancelled": "キャンセル",
    }
    if status == "running":
        progress_label = _agent_delegation_progress_label(task)
        if progress_label:
            return f"実行中（{progress_label}）"
    if status == "failed":
        if "アプリ再起動" in error:
            return "中断（アプリ再起動）"
        if "メモリ上限" in error or "memorylimit" in lowered or "memory limit" in lowered:
            return "失敗（メモリ上限超過で中断）"
        if "タイムアウト" in error or "timeout" in lowered:
            return "失敗（タイムアウト）"
    if status == "cancelled" and ("メモリ逼迫" in error or "mem watchdog" in lowered):
        return "キャンセル（メモリ逼迫で自動停止）"
    return labels.get(status, status or "不明")


def _agent_delegation_tasks_dataframe(tasks: list[dict]) -> pd.DataFrame:
    rows = []
    for task in tasks:
        rows.append({
            "task_id": str(task.get("id") or ""),
            "room": str(task.get("room_name") or ""),
            "backend": _agent_delegation_task_backend(task),
            "status": _agent_delegation_status_reason(task),
            "tier": str(task.get("permission_tier") or ""),
            "scope": _agent_delegation_scope_label(task),
            "created_at": str(task.get("created_at") or ""),
            "summary": _task_summary_excerpt(task),
        })
    return pd.DataFrame(rows, columns=AGENT_DELEGATION_TASK_COLUMNS)


def _agent_delegation_task_choices(tasks: list[dict]) -> list[tuple[str, str]]:
    choices = []
    for task in tasks:
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        status = str(task.get("status") or "unknown")
        room_name = str(task.get("room_name") or "")
        created_at = str(task.get("created_at") or "")
        label = f"{task_id} | {status} | {room_name} | {created_at}"
        choices.append((label, task_id))
    return choices


def _format_agent_delegation_log_line(payload: dict) -> str:
    timestamp = str(payload.get("timestamp") or "")
    message = str(payload.get("message") or "")
    extra = {k: v for k, v in payload.items() if k not in {"timestamp", "message"}}
    if extra:
        return f"[{timestamp}] {message}\n{json.dumps(extra, ensure_ascii=False, indent=2, default=str)}"
    return f"[{timestamp}] {message}"


def _agent_delegation_task_detail_header(task: dict) -> str:
    """選択タスクの状態・ティア・スコープ・理由を、ログ本文の前に出す要約ヘッダ。"""
    lines = [
        f"状態: {_agent_delegation_status_reason(task)}",
        f"権限ティア: {task.get('permission_tier') or '-'}",
        f"スコープ: {_agent_delegation_scope_label(task)}",
        f"ワークスペース: {task.get('workspace') or '-'}",
    ]
    for scope in task.get("extra_scopes") or []:
        if isinstance(scope, dict) and scope.get("root"):
            lines.append(f"  参照スコープ: {scope.get('root')} [tier={scope.get('tier')}]")
    if str(task.get("status") or "") == "running":
        progress_label = _agent_delegation_progress_label(task)
        if progress_label:
            lines.append(f"進捗: {progress_label}")
    error = str(task.get("error") or "").strip()
    if error:
        lines.append(f"理由/エラー: {error}")
    return "\n".join(lines)


def load_agent_delegation_task_log(task_id: str) -> str:
    """選択された委任タスクの状態要約＋ JSONL 実行ログを表示用に整形する。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        return "タスクを選択してください。"
    task = next((t for t in _load_agent_delegation_tasks() if str(t.get("id") or "") == task_id), None)
    header = f"{_agent_delegation_task_detail_header(task)}\n\n――― 実行ログ ―――\n" if task else ""
    path = _agent_delegation_log_path(task_id)
    if not path.exists():
        return header + f"実行ログが見つかりません: {task_id}"
    lines = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    lines.append(raw_line)
                    continue
                if isinstance(payload, dict):
                    lines.append(_format_agent_delegation_log_line(payload))
                else:
                    lines.append(str(payload))
        return header + ("\n\n".join(lines) or f"実行ログは空です: {task_id}")
    except Exception as e:
        traceback.print_exc()
        return header + f"実行ログの読み込みに失敗しました: {type(e).__name__}: {e}"


def refresh_agent_delegation_task_view():
    """委任タスク一覧と選択肢を再読み込みする。"""
    tasks = _load_agent_delegation_tasks()
    df = _agent_delegation_tasks_dataframe(tasks)
    choices = _agent_delegation_task_choices(tasks)
    selected = choices[0][1] if choices else None
    log_text = load_agent_delegation_task_log(selected) if selected else "委任タスクはまだありません。"
    return df, gr.update(choices=choices, value=selected), _agent_delegation_task_count_summary(tasks), log_text


def handle_agent_delegation_task_row_select(df: pd.DataFrame, evt: gr.SelectData):
    """委任タスク一覧の行選択からタスクIDを取り出してログを表示する。"""
    try:
        if df is None or evt is None:
            return gr.update(), gr.update()
        row_index = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        if row_index is None:
            return gr.update(), gr.update()
        task_id = str(df.iloc[int(row_index)]["task_id"] or "").strip()
        if not task_id:
            return gr.update(), "タスクIDを取得できませんでした。"
        return gr.update(value=task_id), load_agent_delegation_task_log(task_id)
    except Exception as e:
        traceback.print_exc()
        return gr.update(), f"タスク選択の処理に失敗しました: {type(e).__name__}: {e}"


def handle_delete_agent_delegation_task(task_id: str):
    """選択中の委任タスクを削除して一覧を再読み込みする（実行中は保護）。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        gr.Warning("削除するタスクを選択してください。")
    else:
        try:
            agent_delegation_manager.delete_task(task_id)
            gr.Info(f"委任タスクを削除しました: {task_id}")
        except Exception as e:
            gr.Warning(f"削除できませんでした: {type(e).__name__}: {e}")
    return refresh_agent_delegation_task_view()


def handle_resume_agent_delegation_task(task_id: str):
    """中断・失敗した委任タスクを同じ依頼内容で最初から再実行し、一覧を再読み込みする。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        gr.Warning("最初から再実行するタスクを選択してください。")
    else:
        try:
            original_task = agent_delegation_manager.check_task_status(task_id)
            new_task = agent_delegation_manager.resume_task(task_id)
            warning = ""
            if str(original_task.get("permission_tier") or "").strip().lower() in {"write", "full"}:
                warning = (
                    "⚠️ このタスクは書き込み権限付きです。前回実行の途中成果が workspace に残った状態で、"
                    "最初から再実行されます。\n"
                )
            gr.Info(f"{warning}同じ依頼で最初から再実行しました: {new_task.get('id')}")
        except Exception as e:
            gr.Warning(f"最初から再実行できませんでした: {type(e).__name__}: {e}")
    return refresh_agent_delegation_task_view()


def handle_clear_finished_agent_delegation_tasks():
    """終了済みの委任タスクをまとめて削除して一覧を再読み込みする。"""
    try:
        removed = agent_delegation_manager.clear_finished_tasks()
        if removed:
            gr.Info(f"終了済みの委任タスク {removed} 件を削除しました。")
        else:
            gr.Info("削除対象の終了済みタスクはありませんでした。")
    except Exception as e:
        gr.Warning(f"一括削除に失敗しました: {type(e).__name__}: {e}")
    return refresh_agent_delegation_task_view()


def handle_steer_agent_delegation_task(task_id: str, instruction: str):
    """実行中の委任タスクへ途中指示（ステアリング）を送り、一覧・ログを再読み込みする。

    実行中（生存スレッドあり）のタスクのみ受理する（受理可否は manager.steer_task が判定）。
    成功時は入力欄をクリアし、失敗時は入力内容を残す。戻り値は
    refresh_agent_delegation_task_view() の4値＋途中指示テキストの更新（計5値）。
    """
    task_id = str(task_id or "").strip()
    text = str(instruction or "").strip()
    cleared = gr.update()
    if not task_id:
        gr.Warning("途中指示を送るタスクを選択してください。")
    elif not text:
        gr.Warning("途中指示の内容を入力してください。")
    else:
        try:
            result = agent_delegation_manager.steer_task(task_id, text)
            gr.Info(
                f"実行中の委任に途中指示を送りました（保留 {result.get('pending')} 件）。"
                "次の思考から反映されます。"
            )
            cleared = gr.update(value="")
        except Exception as e:
            gr.Warning(f"途中指示を送れませんでした: {type(e).__name__}: {e}")
    df, dropdown, cost_summary, log_text = refresh_agent_delegation_task_view()
    return df, dropdown, cost_summary, log_text, cleared


def handle_delegation_exec_provider_change(category: str, profile_name: str, current_model: str = None):
    choices = _delegation_model_choices(category, profile_name)
    if current_model and current_model not in choices:
        choices = [current_model] + choices
    # Gradio fires Dropdown.change for programmatic load/room-switch updates too.
    # When current_model is still empty during reload, returning value="" can run
    # after the saved room value is restored and blank the delegation model again.
    # Update choices only; preserve the component's current value until a user or
    # the saved-state loader supplies an explicit model.
    model_update = gr.update(choices=choices)
    if current_model:
        model_update = gr.update(choices=choices, value=current_model)
    return (
        gr.update(visible=(category in ["openai", "openai_official"])),
        model_update,
    )
