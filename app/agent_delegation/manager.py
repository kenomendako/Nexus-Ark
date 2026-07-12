"""Claude SDK backed task delegation MVP."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import config_manager
import constants
import persona_contract
import tool_usage_stats
import utils
from agent_delegation import roles, skill_pack
from agent_delegation.types import AgentRunResult, AgentTaskSpec, DelegationScope
from agent_delegation.resource_limits import (
    DELEGATION_RLIMIT_AS_MB,
    DELEGATION_RLIMIT_CPU_SECONDS,
    DELEGATION_RLIMIT_FSIZE_MB,
    DELEGATION_RLIMIT_HEADROOM_NPROC,
    _current_uid_process_count,
    _delegation_resource_limits,
)
from file_lock_utils import safe_json_read, safe_json_update, safe_json_write
from tools.path_policy import check_delegation_tool_permission, resolve_project_path
import room_manager


logger = logging.getLogger(__name__)

TASK_STATUSES = {"pending", "running", "done", "failed", "partial", "needs_clarification", "cancelled"}
TERMINAL_TASK_STATUSES = {"done", "failed", "partial", "needs_clarification", "cancelled"}
# 保存する終了済み委任タスクの上限（古いものから自動間引き。実行中/待機中は対象外）
AGENT_DELEGATION_MAX_STORED_TASKS = 200
READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]
WEB_TOOLS = ["WebFetch", "WebSearch"]
WRITE_TOOLS = ["Edit", "Write"]
SHELL_TOOLS = ["Bash"]
ALL_CLAUDE_CODE_TOOLS = READ_ONLY_TOOLS + WEB_TOOLS + WRITE_TOOLS + SHELL_TOOLS
DEFAULT_DENY_MESSAGE = "Nexus Ark policy: this tool call is not allowed for the delegated workspace."
AGENT_DELEGATION_DEFAULT_MAX_TURNS = 20
NEEDS_CLARIFICATION_MARKER = "NEXUS_ARK_NEEDS_CLARIFICATION"
ROOM_AGENT_DELEGATION_POLICY_KEYS = {
    "enabled",
    "permission_tier",
    "allow_web_tools",
    "wake_on_completion",
    "wake_respect_quiet_hours",
    "deleg_exec_provider_cat",
    "deleg_exec_openai_profile",
    "deleg_exec_model",
    "limit_profile_overrides",
    "deleg_review_internal_role",  # 旧: レビュー内部ロール（後方互換のため読み込みは残す）
    "deleg_review_provider_cat",
    "deleg_review_openai_profile",
    "deleg_review_model",
    "deleg_review_iterations",
}

# レビュー（点検）に使う内部モデルロール。要約モデルは賢いモデルを割り当てる運用が多く
# 点検に向くため既定は summarization。解決に失敗したら processing にフォールバックする。
VALID_REVIEW_INTERNAL_ROLES = {"summarization", "processing", "supervisor"}
DEFAULT_REVIEW_INTERNAL_ROLE = "summarization"
# 反復（リバイズ）の上限巡数。暴走防止。
AGENT_REVIEW_MAX_ITERATIONS = 3
_active_wake_context_lock = threading.Lock()
_active_wake_context: dict[str, dict[str, Any]] = {}


class MaxTurnsReachedError(RuntimeError):
    def __init__(self, message: str, assistant_text: str, metadata: dict[str, Any]):
        super().__init__(message)
        self.assistant_text = assistant_text
        self.metadata = metadata


class MemoryLimitExceededError(RuntimeError):
    """Raised when an in-process native delegation run exceeds its RSS budget."""


def _metadata_dir() -> Path:
    return Path(constants.METADATA_DIR) / "agent_delegation"


def _tasks_path() -> Path:
    return _metadata_dir() / "tasks.json"


def _logs_dir() -> Path:
    return _metadata_dir() / "logs"


def get_agent_delegation_settings(room_name: str | None = None) -> dict[str, Any]:
    defaults = {
        "enabled": False,
        "permission_tier": "read",
        "max_concurrent_tasks": 1,
        "max_turns": AGENT_DELEGATION_DEFAULT_MAX_TURNS,
        "timeout_seconds": 600,
        "allow_web_tools": False,
        "model": "",
        "deleg_exec_provider_cat": "",
        "deleg_exec_openai_profile": "",
        "deleg_exec_model": "",
        "model_tiers": {},
        "task_model_tiers": {},
        "limit_profile_overrides": {},
        "deleg_review_internal_role": DEFAULT_REVIEW_INTERNAL_ROLE,
        "deleg_review_provider_cat": "",
        "deleg_review_openai_profile": "",
        "deleg_review_model": "",
        "deleg_review_iterations": 0,
        "deleg_auto_tune_limits": True,
        "wake_on_completion": False,
        "wake_chain_max_depth": 2,
        "wake_daily_cap": 10,
        "wake_min_interval_minutes": 30,
        "wake_respect_quiet_hours": True,
        "deleg_rlimit_nproc": 0,
        "deleg_rlimit_cpu_seconds": DELEGATION_RLIMIT_CPU_SECONDS,
        "deleg_rlimit_as_mb": DELEGATION_RLIMIT_AS_MB,
        "deleg_rlimit_fsize_mb": DELEGATION_RLIMIT_FSIZE_MB,
        "deleg_rss_limit_mb": DELEGATION_RLIMIT_AS_MB,
    }
    raw = config_manager.CONFIG_GLOBAL.get("agent_delegation_settings", {}) if isinstance(config_manager.CONFIG_GLOBAL, dict) else {}
    settings = {**defaults, **(raw if isinstance(raw, dict) else {})}
    if room_name:
        try:
            effective = config_manager.get_effective_settings(room_name)
            room_settings = effective.get("agent_delegation_settings", {}) if isinstance(effective, dict) else {}
            if isinstance(room_settings, dict):
                for key in ROOM_AGENT_DELEGATION_POLICY_KEYS:
                    if key in room_settings:
                        settings[key] = room_settings[key]
        except Exception:
            logger.debug("failed to load room agent delegation settings for %s", room_name, exc_info=True)
    settings["enabled"] = bool(settings.get("enabled"))
    settings["permission_tier"] = str(settings.get("permission_tier") or "read")
    settings["max_concurrent_tasks"] = max(1, int(settings.get("max_concurrent_tasks") or 1))
    raw_max_turns = int(settings.get("max_turns") or AGENT_DELEGATION_DEFAULT_MAX_TURNS)
    if raw_max_turns <= 8:
        raw_max_turns = AGENT_DELEGATION_DEFAULT_MAX_TURNS
    settings["max_turns"] = max(3, raw_max_turns)
    settings["timeout_seconds"] = max(30, int(settings.get("timeout_seconds") or 600))
    settings["allow_web_tools"] = bool(settings.get("allow_web_tools"))
    settings["model"] = str(settings.get("model") or "").strip()
    settings["deleg_exec_provider_cat"] = str(settings.get("deleg_exec_provider_cat") or "").strip()
    settings["deleg_exec_openai_profile"] = str(settings.get("deleg_exec_openai_profile") or "").strip()
    settings["deleg_exec_model"] = str(settings.get("deleg_exec_model") or "").strip()
    settings["model_tiers"] = settings.get("model_tiers") if isinstance(settings.get("model_tiers"), dict) else {}
    settings["task_model_tiers"] = settings.get("task_model_tiers") if isinstance(settings.get("task_model_tiers"), dict) else {}
    settings["limit_profile_overrides"] = settings.get("limit_profile_overrides") if isinstance(settings.get("limit_profile_overrides"), dict) else {}
    review_role = str(settings.get("deleg_review_internal_role") or "").strip().lower()
    settings["deleg_review_internal_role"] = review_role if review_role in VALID_REVIEW_INTERNAL_ROLES else DEFAULT_REVIEW_INTERNAL_ROLE
    # レビューモデルの直接指定（委任実行モデルと同じ provider+profile+model の三つ組）。
    # 未設定（provider_cat 空 or "default"・model 空）なら要約モデルにフォールバックする。
    settings["deleg_review_provider_cat"] = str(settings.get("deleg_review_provider_cat") or "").strip()
    settings["deleg_review_openai_profile"] = str(settings.get("deleg_review_openai_profile") or "").strip()
    settings["deleg_review_model"] = str(settings.get("deleg_review_model") or "").strip()
    # 自動レビュー反復の回数。既定0=OFF。上限は手動反復と同じ AGENT_REVIEW_MAX_ITERATIONS でキャップ。
    settings["deleg_review_iterations"] = max(0, min(AGENT_REVIEW_MAX_ITERATIONS, int(settings.get("deleg_review_iterations") or 0)))
    settings["wake_on_completion"] = bool(settings.get("wake_on_completion"))
    settings["wake_chain_max_depth"] = max(0, int(settings.get("wake_chain_max_depth") or 0))
    settings["wake_daily_cap"] = max(0, int(settings.get("wake_daily_cap") or 0))
    settings["wake_min_interval_minutes"] = max(0, int(settings.get("wake_min_interval_minutes") or 0))
    settings["wake_respect_quiet_hours"] = bool(settings.get("wake_respect_quiet_hours"))
    settings["deleg_rlimit_nproc"] = max(0, int(settings.get("deleg_rlimit_nproc") or 0))
    settings["deleg_rlimit_cpu_seconds"] = max(10, int(settings.get("deleg_rlimit_cpu_seconds") or DELEGATION_RLIMIT_CPU_SECONDS))
    settings["deleg_rlimit_as_mb"] = max(256, int(settings.get("deleg_rlimit_as_mb") or DELEGATION_RLIMIT_AS_MB))
    settings["deleg_rlimit_fsize_mb"] = max(16, int(settings.get("deleg_rlimit_fsize_mb") or DELEGATION_RLIMIT_FSIZE_MB))
    settings["deleg_rss_limit_mb"] = max(0, int(settings.get("deleg_rss_limit_mb") or 0))
    settings["deleg_auto_tune_limits"] = bool(settings.get("deleg_auto_tune_limits", True))
    _apply_auto_tuned_limits(settings, room_name)
    return settings


def _apply_auto_tuned_limits(settings: dict[str, Any], room_name: str | None) -> None:
    """委任実行モデルが解決できる場合、上限（max_turns/timeout_seconds）をモデルに応じた推奨値へ寄せる。

    保存値（手動baseline・UI表示）は変更せず、ここで返す実行用 settings のみ調整する。
    `deleg_auto_tune_limits` が False、または委任実行モデル未設定（会話モデルへフォールバック）の
    場合は何もしない（保存値をそのまま使う）。
    """
    if not settings.get("deleg_auto_tune_limits") or not room_name:
        return
    try:
        resolved = config_manager.get_effective_delegation_model(room_name)
        if not resolved:
            return
        provider_cat, model_name, _profile = resolved
        derived = config_manager.derive_delegation_limits(
            provider_cat,
            model_name,
            overrides=settings.get("limit_profile_overrides"),
        )
        settings["max_turns"] = max(3, int(derived["max_turns"]))
        settings["timeout_seconds"] = max(30, int(derived["timeout_seconds"]))
        settings["deleg_auto_tuned_limit_profile"] = derived["limit_profile"]
    except Exception:
        logger.debug("failed to auto-tune delegation limits for %s", room_name, exc_info=True)


def _apply_routed_auto_tuned_limits(settings: dict[str, Any], model_override: tuple[str, str, str] | None) -> None:
    """タスクごとの routed model がある場合、実行用上限をそのモデル分類へ寄せる。"""
    if not settings.get("deleg_auto_tune_limits") or not model_override:
        return
    try:
        provider_cat, model_name, _profile = model_override
        derived = config_manager.derive_delegation_limits(
            provider_cat,
            model_name,
            overrides=settings.get("limit_profile_overrides"),
        )
        settings["max_turns"] = max(3, int(derived["max_turns"]))
        settings["timeout_seconds"] = max(30, int(derived["timeout_seconds"]))
        settings["deleg_auto_tuned_limit_profile"] = derived["limit_profile"]
    except Exception:
        logger.debug("failed to auto-tune routed delegation limits", exc_info=True)


def set_active_wake_context(room_name: str, triggered_by: str, chain_depth: int) -> None:
    room = str(room_name or "").strip()
    if not room:
        return
    with _active_wake_context_lock:
        _active_wake_context[room] = {
            "triggered_by": str(triggered_by or "delegation_complete"),
            "chain_depth": max(0, int(chain_depth or 0)),
        }


def clear_active_wake_context(room_name: str) -> None:
    room = str(room_name or "").strip()
    if not room:
        return
    with _active_wake_context_lock:
        _active_wake_context.pop(room, None)


def _get_active_wake_context(room_name: str) -> dict[str, Any] | None:
    with _active_wake_context_lock:
        context = _active_wake_context.get(str(room_name or "").strip())
        return dict(context) if isinstance(context, dict) else None


def _load_tasks() -> dict[str, Any]:
    data = safe_json_read(str(_tasks_path()), default={"tasks": {}})
    if not isinstance(data, dict):
        return {"tasks": {}}
    if not isinstance(data.get("tasks"), dict):
        data["tasks"] = {}
    return data


def _save_tasks(data: dict[str, Any]) -> bool:
    _metadata_dir().mkdir(parents=True, exist_ok=True)
    return safe_json_write(str(_tasks_path()), data)


def _update_tasks(mutator: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any]:
    _metadata_dir().mkdir(parents=True, exist_ok=True)
    updated: dict[str, Any] = {"tasks": {}}

    def update(data: Any) -> dict[str, Any]:
        nonlocal updated
        if not isinstance(data, dict):
            data = {"tasks": {}}
        if not isinstance(data.get("tasks"), dict):
            data["tasks"] = {}
        result = mutator(data)
        if isinstance(result, dict):
            data = result
            if not isinstance(data.get("tasks"), dict):
                data["tasks"] = {}
        updated = data
        return data

    safe_json_update(str(_tasks_path()), update, default={"tasks": {}})
    return updated


def _prune_tasks(data: dict[str, Any], keep: int = AGENT_DELEGATION_MAX_STORED_TASKS) -> dict[str, Any]:
    """終了済みタスクのうち古いものを間引き、最新 keep 件だけ残す（実行中/待機中は常に保持）。"""
    tasks = data.get("tasks", {})
    if not isinstance(tasks, dict) or len(tasks) <= keep:
        return data
    terminal = [
        (tid, task)
        for tid, task in tasks.items()
        if isinstance(task, dict) and task.get("status") in TERMINAL_TASK_STATUSES
    ]
    if len(terminal) <= keep:
        return data
    terminal.sort(key=lambda kv: str(kv[1].get("created_at") or kv[1].get("updated_at") or ""), reverse=True)
    keep_ids = {tid for tid, _ in terminal[:keep]}
    pruned = {
        tid: task
        for tid, task in tasks.items()
        if (task.get("status") not in TERMINAL_TASK_STATUSES) or (tid in keep_ids)
    }
    removed = [tid for tid in tasks if tid not in pruned]
    for tid in removed:
        try:
            _task_log_path(tid).unlink(missing_ok=True)
        except Exception:
            logger.debug("failed to remove pruned task log %s", tid, exc_info=True)
    data["tasks"] = pruned
    return data


def reconcile_orphaned_tasks() -> list[dict[str, Any]]:
    """アプリ起動時に、スレッドが生きていない running/pending タスクを「中断」に直す。

    OOM 等でプロセスごと落ちると JSON に running のまま残るため、起動時に整合させる。
    あわせて古い終了済みタスクを間引く。戻り値は中断扱いに変更したタスクレコード。
    """
    now = datetime.now().isoformat()
    changed_records: list[dict[str, Any]] = []

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        tasks = data.get("tasks", {})
        if not isinstance(tasks, dict):
            data["tasks"] = {}
            return data
        for task_id, task in tasks.items():
            if not isinstance(task, dict):
                continue
            if task.get("status") in ("running", "pending") and task_id not in _RUNNERS:
                metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
                task["metadata"] = {**metadata, "orphaned_by_restart": True}
                task["status"] = "failed"
                task["error"] = "アプリ再起動により中断されました（途中状態）。"
                task["finished_at"] = task.get("finished_at") or now
                task["updated_at"] = now
                changed_records.append(dict(task))
        return _prune_tasks(data)

    _update_tasks(mutate)
    if changed_records:
        logger.info("[AgentDelegation] reconciled %s orphaned running/pending task(s) to interrupted", len(changed_records))
    return changed_records


def inject_restart_interruption_notices(tasks: list[dict[str, Any]]) -> None:
    """アプリ再起動で中断扱いになった委任タスクを、ルームごとのチャットログへ通知する。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        room_name = str(task.get("room_name") or "").strip()
        if not room_name:
            continue
        grouped.setdefault(room_name, []).append(task)
    for room_name, room_tasks in grouped.items():
        first = room_tasks[0]
        description = str(first.get("task_description") or "").strip()
        if len(description) > 40:
            description = description[:40] + "…"
        message = (
            f"⚠️ アプリ再起動により中断された委任タスクが {len(room_tasks)}件 あります"
            f"（例: 「{description or '内容未記録'}」）。\n"
            "委任タブから内容を確認し、必要なら最初から再実行できます。"
        )
        try:
            utils.append_system_message_to_log(room_name, message)
        except Exception:
            logger.debug("agent delegation restart interruption notice injection failed", exc_info=True)


def delete_task(task_id: str) -> dict[str, Any]:
    """委任タスクの記録と実行ログを削除する。実行中（生存スレッドあり）のタスクは削除しない。"""
    task_id = str(task_id or "").strip().strip("`")
    if not task_id:
        raise KeyError("削除する委任タスクIDが未指定です。")
    runner = _RUNNERS.get(task_id)
    if runner is not None and runner.thread.is_alive():
        raise RuntimeError("実行中の委任タスクは削除できません。先にキャンセルしてください。")
    removed: dict[str, Any] | None = None

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal removed
        tasks = data.get("tasks", {})
        if task_id not in tasks:
            raise KeyError(f"委任タスクが見つかりません: {task_id}")
        removed = tasks.pop(task_id)
        return data

    _update_tasks(mutate)
    try:
        _task_log_path(task_id).unlink(missing_ok=True)
    except Exception:
        logger.debug("failed to remove task log %s", task_id, exc_info=True)
    return {"id": task_id, "deleted": True, "status": removed.get("status") if isinstance(removed, dict) else None}


RESUMABLE_TASK_STATUSES = {"failed", "cancelled", "partial", "needs_clarification"}


def resume_task(task_id: str) -> dict[str, Any]:
    """中断・失敗した委任タスクを、同じ依頼内容・権限・ワークスペースで最初から再投入する。

    途中再開ではなく、同じ仕様で新しい task_id のタスクとして完全再実行する。
    done（成功）や running/pending（処理中）は対象外。metadata に resumed_from を記録する。
    """
    task_id = str(task_id or "").strip().strip("`")
    if not task_id:
        raise KeyError("再委任する委任タスクIDが未指定です。")
    task = _resolve_task(task_id)
    if not task:
        raise KeyError(f"委任タスクが見つかりません: {task_id}")
    status = str(task.get("status") or "")
    if status not in RESUMABLE_TASK_STATUSES:
        raise RuntimeError(
            f"このタスクは再委任の対象ではありません（status={status}）。中断・失敗・確認待ち・途中終了のタスクのみ再委任できます。"
        )
    description = str(task.get("task_description") or "").strip()
    if not description:
        raise RuntimeError("元タスクの依頼内容が空のため再委任できません。")
    return submit_task(
        room_name=str(task.get("room_name") or ""),
        task_description=description,
        expected_output=str(task.get("expected_output") or ""),
        permission_tier=_normalize_permission_tier(task.get("permission_tier")),
        workspace_kind=str(task.get("workspace_kind") or "project"),
        task_kind=str(task.get("task_kind") or ""),
        role=str(task.get("role") or ""),
        trigger=str(task.get("triggered_by") or "chat"),
        metadata={"resumed_from": str(task.get("id") or task_id)},
    )


# ---------------------------------------------------------------------------
# レビュー＆反復（サブエージェント強化B-1）
# ---------------------------------------------------------------------------

REVIEWABLE_TASK_STATUSES = {"done", "partial"}


def _resolve_task_or_latest(task_id: str | None, room_name: str | None) -> dict[str, Any]:
    task_id = str(task_id or "").strip().strip("`")
    if task_id:
        task = _resolve_task(task_id)
        if not task:
            raise KeyError(f"委任タスクが見つかりません: {task_id}")
        return task
    if room_name:
        return latest_task_for_room(room_name)
    raise KeyError("レビュー対象の委任タスクIDが未指定です。room_name を指定すると最新タスクを対象にできます。")


def _extract_llm_text(response: Any) -> str:
    """invoke_internal_llm のレスポンス（AIMessage 等）から本文テキストを取り出す。"""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("text"):
                parts.append(str(block.get("text")))
        return "".join(parts)
    return str(content or "")


def _build_review_prompt(task: dict[str, Any]) -> str:
    description = str(task.get("task_description") or "").strip()
    expected = str(task.get("expected_output") or "").strip()
    summary = str(task.get("summary") or "").strip() or "（成果の要約が空でした）"
    expected_block = expected if expected else "（指定なし。依頼内容の意図に照らして評価してください）"
    contract_block = persona_contract.format_contract_for_delegation(str(task.get("room_name") or ""))
    contract_review = f"\n\n# Persona Contract\n{contract_block}" if contract_block else ""
    return (
        "あなたは委任タスクの成果をレビューする点検役です。実行役の成果を読み、"
        "期待アウトプットと依頼の意図に照らして客観的に評価してください。\n\n"
        f"# 依頼内容\n{description}\n\n"
        f"# 期待アウトプット\n{expected_block}\n\n"
        f"# 実行役の成果（要約）\n{summary}\n\n"
        f"{contract_review}\n\n"
        "# 共通検収観点\n"
        "- 固有名詞・呼び名・禁止語が Persona Contract に反していないか。\n"
        "- 失敗報告がある場合、失敗段階・再試行可能性・人間が見るべき箇所が書かれているか。\n"
        "- API/PWA連携がある場合、権限・payload・拒否理由・ユーザー向けエラー表示が具体的か。\n\n"
        "# 出力形式（厳守・JSONにしないこと）\n"
        "最終行: VERDICT: PASS または VERDICT: REVISE\n"
        "判定: PASS または REVISE\n"
        "達成度: 0〜100の整数の目安\n"
        "満たした点:\n- ...\n"
        "不足・ズレ:\n- ...\n"
        "直す方向:\n- ...\n\n"
        "PASS は「期待アウトプットを実質的に満たしている」場合のみ。"
        "重要な不足・ズレが一つでもあれば REVISE とし、直す方向を具体的に書いてください。"
    )


def _parse_review_verdict(text: str) -> str:
    """レビュー本文から PASS / REVISE を頑健に拾う。読めなければ unknown。"""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if re.match(r"^VERDICT\s*:", stripped, flags=re.IGNORECASE):
            upper = stripped.upper()
            if re.search(r"\bPASS\b", upper):
                return "pass"
            if re.search(r"\bREVISE\b", upper):
                return "revise"
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("判定"):
            upper = stripped.upper()
            if "PASS" in upper:
                return "pass"
            if "REVISE" in upper:
                return "revise"
    upper_all = str(text or "").upper()
    if "REVISE" in upper_all:
        return "revise"
    if "PASS" in upper_all:
        return "pass"
    return "unknown"


def _build_review_clarification_prompt(review_text: str) -> str:
    return (
        "前回のレビュー出力から判定を機械的に読み取れませんでした。\n"
        "次のレビュー本文を読み、PASS か REVISE のどちらか一語だけで判定し直してください。\n\n"
        f"# レビュー本文\n{str(review_text or '').strip()}\n\n"
        "出力は PASS または REVISE の一語のみ。"
    )


def _invoke_review_llm(prompt: str, room_name: str) -> str:
    """レビュー用LLMを呼ぶ。

    レビューモデルが直接指定（provider+model）されていればそれを使う（委任実行モデルと同じ機構）。
    未指定なら要約モデル（summarization）→処理モデル（processing）の内部ロールにフォールバックする。
    """
    from llm_factory import LLMFactory

    settings = get_agent_delegation_settings(room_name)
    provider_cat = str(settings.get("deleg_review_provider_cat") or "").strip()
    model = str(settings.get("deleg_review_model") or "").strip()
    profile = str(settings.get("deleg_review_openai_profile") or "").strip()

    # 1) 直接指定があれば委任実行モデルと同じ経路（キー解決込み）で組む
    if provider_cat and provider_cat != "default" and model:
        try:
            llm = LLMFactory.create_chat_model(
                internal_role="delegation",
                delegation_model_override=(provider_cat, model, profile),
                room_name=room_name,
                max_retries=0,
            )
            response = llm.invoke(prompt)
            try:
                import usage_ledger
                key_name = ""
                if str(provider_cat).lower() in {"google", "google (gemini)", "google (gemini native)"}:
                    key_name = config_manager.get_active_gemini_api_key_name(room_name=room_name, model_name=model) or ""
                usage_ledger.record_response(response, model, "delegation", api_key_name=key_name)
            except Exception:
                pass
            text = _extract_llm_text(response).strip()
            if text:
                return text
        except Exception:  # noqa: BLE001 — レビューはタスクを止めない。フォールバックへ。
            logger.warning("review llm direct model failed (%s/%s); falling back", provider_cat, model, exc_info=True)

    # 2) フォールバック: 要約 → 処理
    last_exc: Exception | None = None
    for role in ("summarization", "processing"):
        try:
            response, _key = LLMFactory.invoke_internal_llm(role, prompt, room_name=room_name)
            text = _extract_llm_text(response).strip()
            if text:
                return text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.debug("review llm role=%s failed", role, exc_info=True)
    if last_exc is not None:
        raise last_exc
    return ""


def evaluate_output(
    task_description: str,
    expected_output: str,
    output_text: str,
    *,
    room_name: str,
    review_role: str | None = None,
) -> tuple[str, str]:
    """成果テキストを点検し (本文, 判定) を返す。判定は pass / revise / unknown。

    review_task（手動レビュー）と自動レビュー反復（B-2）の共通エンジン。
    内部LLM失敗・空応答時は ("", "unknown") を返す（点検はタスクを止めない）。
    """
    # review_role は後方互換のため受けるが、モデル選択はルーム設定（直接指定→要約フォールバック）に一本化。
    prompt = _build_review_prompt(
        {
            "task_description": task_description,
            "expected_output": expected_output,
            "summary": output_text,
        }
    )
    try:
        text = _invoke_review_llm(prompt, room_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("review verifier failed: %s", exc)
        return "", "unknown"
    if not text:
        return "", "unknown"
    verdict = _parse_review_verdict(text)
    if verdict == "unknown":
        try:
            clarification = _invoke_review_llm(_build_review_clarification_prompt(text), room_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("review verdict clarification failed: %s", exc)
            return text, "unknown"
        clarified_verdict = _parse_review_verdict(clarification)
        if clarified_verdict != "unknown":
            combined = f"{text}\n\n[判定再確認]\n{clarification}".strip()
            return combined, clarified_verdict
        logger.warning("review verdict remained unknown after clarification")
    return text, verdict


def review_task(task_id: str | None = None, *, room_name: str | None = None) -> dict[str, Any]:
    """完了タスクの成果を点検し、判定（pass/revise/unknown）と指摘テキストを返す。

    タスクの成果（summary）は変更しない。点検結果は metadata.last_review に注記として残す
    （リバイズ時に省略フィードバックとして再利用するため）。
    """
    task = _resolve_task_or_latest(task_id, room_name)
    status = str(task.get("status") or "")
    if status not in REVIEWABLE_TASK_STATUSES:
        raise RuntimeError(
            f"このタスクはレビュー対象ではありません（status={status}）。完了（done）または途中終了（partial）のタスクを点検できます。"
        )
    resolved_room = str(task.get("room_name") or room_name or "")
    settings = get_agent_delegation_settings(resolved_room)
    review_role = str(settings.get("deleg_review_internal_role") or DEFAULT_REVIEW_INTERNAL_ROLE)
    review_text, verdict = evaluate_output(
        str(task.get("task_description") or ""),
        str(task.get("expected_output") or ""),
        str(task.get("summary") or ""),
        room_name=resolved_room,
        review_role=review_role,
    )
    if not review_text:
        return {
            "task_id": str(task.get("id") or ""),
            "verdict": "unknown",
            "review_text": "（レビューを実行できませんでした。内部モデル設定をご確認ください。）",
            "review_role": review_role,
        }
    now = datetime.now().isoformat()
    metadata = dict(task.get("metadata") or {})
    metadata["last_review"] = {"verdict": verdict, "text": review_text, "role": review_role, "at": now}
    _update_task(str(task.get("id") or ""), {"metadata": metadata})
    _append_log(str(task.get("id") or ""), "task reviewed", {"verdict": verdict, "role": review_role})
    return {
        "task_id": str(task.get("id") or ""),
        "verdict": verdict,
        "review_text": review_text,
        "review_role": review_role,
    }


def _build_revise_description(task: dict[str, Any], feedback: str, iteration: int) -> str:
    description = str(task.get("task_description") or "").strip()
    expected = str(task.get("expected_output") or "").strip()
    summary = str(task.get("summary") or "").strip() or "（前回の成果の要約が空でした）"
    expected_block = f"\n# 期待アウトプット（再掲）\n{expected}\n" if expected else ""
    return (
        f"{description}\n\n"
        f"--- これは前回の委任の「直し」です（{iteration}巡目）---\n"
        f"# 前回の成果（要約）\n{summary}\n\n"
        f"# 直すべき点（レビュー指摘）\n{feedback.strip()}\n"
        f"{expected_block}\n"
        "前回の成果を土台に、上記の指摘を反映して期待アウトプットを満たすよう仕上げてください。"
    )


def revise_task(task_id: str, feedback: str = "", *, room_name: str | None = None) -> dict[str, Any]:
    """完了/途中終了タスクを、レビュー指摘を添えて再委任する（フィードバック付き反復）。

    feedback 省略時は直近のレビュー結果（metadata.last_review）を使い、無ければその場で review_task を実行。
    review_iteration は元タスクから+1。上限 AGENT_REVIEW_MAX_ITERATIONS を超える反復は拒否する。
    """
    task = _resolve_task_or_latest(task_id, room_name)
    status = str(task.get("status") or "")
    if status not in REVIEWABLE_TASK_STATUSES:
        raise RuntimeError(
            f"このタスクは反復（直し）の対象ではありません（status={status}）。完了（done）または途中終了（partial）のタスクを直せます。"
        )
    resolved_id = str(task.get("id") or "")
    metadata = dict(task.get("metadata") or {})
    prev_iteration = int(metadata.get("review_iteration") or 0)
    iteration = prev_iteration + 1
    if iteration > AGENT_REVIEW_MAX_ITERATIONS:
        raise RuntimeError(
            f"反復（直し）の上限（{AGENT_REVIEW_MAX_ITERATIONS}巡）に達しています。"
            "これ以上の自動反復は行いません。依頼内容を見直すか、ユーザーに相談してください。"
        )
    feedback_text = str(feedback or "").strip()
    if not feedback_text:
        last_review = metadata.get("last_review") if isinstance(metadata.get("last_review"), dict) else {}
        feedback_text = str(last_review.get("text") or "").strip()
    if not feedback_text:
        review = review_task(resolved_id, room_name=resolved_room_name(task, room_name))
        feedback_text = str(review.get("review_text") or "").strip()
    if not feedback_text:
        raise RuntimeError("直すための指摘が得られませんでした。feedback を指定して再実行してください。")
    description = _build_revise_description(task, feedback_text, iteration)
    return submit_task(
        room_name=str(task.get("room_name") or room_name or ""),
        task_description=description,
        expected_output=str(task.get("expected_output") or ""),
        permission_tier=_normalize_permission_tier(task.get("permission_tier")),
        workspace_kind=str(task.get("workspace_kind") or "project"),
        task_kind=str(task.get("task_kind") or ""),
        role=str(task.get("role") or ""),
        trigger="revise",
        metadata={"revised_from": resolved_id, "review_iteration": iteration},
    )


def resolved_room_name(task: dict[str, Any], room_name: str | None) -> str:
    return str(task.get("room_name") or room_name or "")


def _build_critic_review_description(task: dict[str, Any]) -> str:
    description = str(task.get("task_description") or "").strip()
    expected = str(task.get("expected_output") or "").strip()
    summary = str(task.get("summary") or "").strip() or "（前回の成果の要約が空でした）"
    expected_block = expected if expected else "（指定なし。依頼の意図に照らして評価してください）"
    contract_block = persona_contract.format_contract_for_delegation(str(task.get("room_name") or ""))
    contract_review = f"\n\n# Persona Contract\n{contract_block}" if contract_block else ""
    return (
        "以下の『完了した委任タスク』の成果を、独立した視点でレビューしてください。"
        "あなたは実装・生成はせず、読んで評価することに徹します。\n\n"
        f"# 元の依頼\n{description}\n\n"
        f"# 期待アウトプット\n{expected_block}\n\n"
        f"# 実行役が報告した成果（要約）\n{summary}\n\n"
        f"{contract_review}\n\n"
        "ワークスペース内の実際の成果物（ファイル等）も確認し、要約と実物の両面から評価してください。"
        "アトリエアプリで `_preview/` があれば、保存されたJSONレポートやスクリーンショットの有無も確認し、"
        "コンソールエラーやページエラーが記録されていないか評価材料にしてください。\n\n"
        "# 共通検収観点\n"
        "- Persona Contract がある場合、表示文言・エラー文・レポート文が契約に反していないか。\n"
        "- API/PWA連携がある場合、要求権限と実処理が一致し、payload不足・権限拒否・500系失敗の表示が具体的か。\n"
        "- 失敗時に、次に人間が見るべきファイル/関数/箇所が報告されているか。\n\n"
        "# 返す形式（JSONにしない）\n"
        "判定: PASS または REVISE\n"
        "達成度: 0〜100の整数の目安\n"
        "満たした点:\n- ...\n"
        "不足・ズレ:\n- ...\n"
        "直す方向:\n- ...\n"
    )


def request_critic_review(task_id: str | None = None, *, room_name: str | None = None) -> dict[str, Any]:
    """完了/途中終了タスクを、critic ロールのサブエージェントで独立レビューする（B-3・非同期）。

    軽量な内部LLMレビュー（review_task）と違い、実際のワークスペース成果物を読んで点検する
    別エージェントを起動する。新しい委任タスク（role=critic・読み取り専用・task_kind=review・
    metadata.review_of=元ID）を投入し、その record を返す。批評は完了後その summary に入る。
    """
    task = _resolve_task_or_latest(task_id, room_name)
    status = str(task.get("status") or "")
    if status not in REVIEWABLE_TASK_STATUSES:
        raise RuntimeError(
            f"このタスクはレビュー対象ではありません（status={status}）。完了（done）または途中終了（partial）のタスクを点検できます。"
        )
    original_id = str(task.get("id") or "")
    description = _build_critic_review_description(task)
    return submit_task(
        room_name=str(task.get("room_name") or room_name or ""),
        task_description=description,
        expected_output="上記の形式で、判定（PASS / REVISE）と具体的な指摘（満たした点・不足・直す方向）を返してください。",
        workspace_kind=str(task.get("workspace_kind") or "project"),
        task_kind="review",
        role="critic",
        trigger="review",
        metadata={"review_of": original_id},
    )


def clear_finished_tasks() -> int:
    """終了済み（done/failed/partial/needs_clarification/cancelled）の委任タスクをまとめて削除する。"""
    remove_ids: list[str] = []

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        tasks = data.get("tasks", {})
        if not isinstance(tasks, dict) or not tasks:
            return data
        remove_ids.extend([
            tid
            for tid, task in tasks.items()
            if isinstance(task, dict) and task.get("status") in TERMINAL_TASK_STATUSES
        ])
        for tid in remove_ids:
            tasks.pop(tid, None)
        return data

    _update_tasks(mutate)
    if not remove_ids:
        return 0
    for tid in remove_ids:
        try:
            _task_log_path(tid).unlink(missing_ok=True)
        except Exception:
            logger.debug("failed to remove task log %s", tid, exc_info=True)
    return len(remove_ids)


def report_task_progress(task_id: str, progress: dict[str, Any]) -> None:
    """実行中タスクの軽量な進捗（ターン数・直近ツール・呼び出し集計）を記録する。

    状態は変更しない。存在しない／終了済み（done/failed/...）タスクには書き込まない
    （途中でキャンセル・削除されたタスクを進捗で復活させないため）。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        return
    try:
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            tasks = data.get("tasks", {})
            task = tasks.get(task_id)
            if not isinstance(task, dict) or task.get("status") in TERMINAL_TASK_STATUSES:
                return data
            task["progress"] = dict(progress or {})
            task["updated_at"] = datetime.now().isoformat()
            tasks[task_id] = task
            return data

        _update_tasks(mutate)
    except Exception:
        logger.debug("failed to record task progress %s", task_id, exc_info=True)


def _update_task(task_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    updated_task: dict[str, Any] = {}

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal updated_task
        tasks = data.setdefault("tasks", {})
        task = dict(tasks.get(task_id) or {})
        task.update(updates)
        task["updated_at"] = datetime.now().isoformat()
        tasks[task_id] = task
        updated_task = dict(task)
        return data

    _update_tasks(mutate)
    return updated_task


def _running_count() -> int:
    return sum(1 for runner in _RUNNERS.values() if runner.thread.is_alive())


def _room_workspace(room_name: str) -> tuple[str, list[str], list[str]]:
    settings = config_manager.get_effective_settings(room_name)
    explorer = settings.get("project_explorer", {}) or {}
    root_path = str(explorer.get("root_path") or "").strip()
    if not root_path:
        raise ValueError("project_explorer.root_path が未設定のため、エージェント委任は利用できません。")
    root = resolve_project_path(root_path)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"委任ワークスペースが見つかりません: {root}")
    return str(root), list(explorer.get("exclude_dirs", []) or []), list(explorer.get("exclude_files", []) or [])


def _persona_workspace(room_name: str) -> tuple[str, list[str], list[str]]:
    settings = config_manager.get_effective_settings(room_name)
    persona_workspace = settings.get("persona_workspace", {}) or {}
    if not bool(persona_workspace.get("enabled", True)):
        raise ValueError("persona_workspace が無効のため、アトリエ委任は利用できません。")
    if not room_manager.ensure_room_files(room_name):
        raise ValueError(f"ルームのアトリエを準備できませんでした: {room_name}")
    root = Path(constants.ROOMS_DIR) / room_name / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return (
        str(root.resolve()),
        list(persona_workspace.get("exclude_dirs", []) or []),
        list(persona_workspace.get("exclude_files", []) or []),
    )


def _workspace_for_task(room_name: str, workspace_kind: str) -> tuple[str, list[str], list[str]]:
    kind = str(workspace_kind or "project").strip().lower()
    if kind in ("persona", "persona_project_read"):
        # アトリエ（書き込み先）を primary とする。project は extra_scope として読み取り付帯。
        return _persona_workspace(room_name)
    if kind == "project":
        return _room_workspace(room_name)
    raise ValueError(f"不明な委任ワークスペース種別です: {workspace_kind}")


def _project_scope_for_room(room_name: str) -> dict[str, Any]:
    """project_explorer ルートを、設定された委任ティアのまま extra_scope 用 dict として返す（方針1：ティア尊重）。"""
    root, exclude_dirs, exclude_files = _room_workspace(room_name)
    project_settings = get_agent_delegation_settings(room_name)
    permission_tier = _normalize_permission_tier(project_settings.get("permission_tier"))
    return {"root": root, "tier": permission_tier, "exclude_dirs": exclude_dirs, "exclude_files": exclude_files}


def _extra_scopes_for_task(room_name: str, workspace_kind: str) -> list[dict[str, Any]]:
    kind = str(workspace_kind or "project").strip().lower()
    if kind == "persona_project_read":
        return [_project_scope_for_room(room_name)]
    return []


def submit_task(
    room_name: str,
    task_description: str,
    expected_output: str = "",
    *,
    permission_tier: str | None = None,
    workspace_kind: str = "",
    trigger: str = "chat",
    task_kind: str = "",
    chain_depth: int | None = None,
    max_turns: int | None = None,
    timeout_seconds: int | None = None,
    role: str = "",
    metadata: dict[str, Any] | None = None,
    sdk_factory: Callable[[], Any] | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    settings = get_agent_delegation_settings(room_name)
    if not settings["enabled"]:
        raise ValueError("このルームのエージェント委任は無効です。")
    global_settings = get_agent_delegation_settings()
    if _running_count() >= global_settings["max_concurrent_tasks"]:
        raise RuntimeError("同時実行数の上限に達しています。")

    # --- ロール適用（明示引数優先で未指定スロットを埋める）---
    # ロールは「装備一式のプリセット」。呼び出し側が明示した値は常に優先し、
    # 未指定の項目だけロール既定で埋める。不正id・読み込み失敗は role 無しと同じ。
    role_obj = roles.get_role(role) if role else None
    params = roles.apply_role(
        role_obj,
        {
            "task_kind": task_kind,
            "workspace_kind": workspace_kind,
            "permission_tier": permission_tier or "",
            "expected_output": expected_output,
            "allow_web_tools": None,  # ロールが有効化を求めたときだけ True になる
            "max_turns": max_turns,
            "timeout_seconds": timeout_seconds,
        },
    )
    task_kind = str(params.get("task_kind") or "").strip().lower()
    workspace_kind = str(params.get("workspace_kind") or "project").strip().lower()
    permission_tier = params.get("permission_tier") or None
    expected_output = str(params.get("expected_output") or "")
    role_wants_web = params.get("allow_web_tools") is True
    if params.get("max_turns") is not None:
        max_turns = params.get("max_turns")
    if params.get("timeout_seconds") is not None:
        timeout_seconds = params.get("timeout_seconds")
    role_id = str(params.get("role_id") or "")
    role_guidance = str(params.get("role_guidance") or "")

    # ディープリサーチはWeb調査が本質。ロールがWebを要求した場合も同様に有効化する。
    if task_kind == "deep_research" or role_wants_web:
        settings = dict(settings)
        settings["allow_web_tools"] = True
    model_override = config_manager.resolve_delegation_model_for_task(
        room_name,
        model_hint=getattr(role_obj, "model_hint", "") if role_obj else "",
        task_kind=task_kind,
    )
    if model_override:
        settings = dict(settings)
        _apply_routed_auto_tuned_limits(settings, model_override)
    workspace, exclude_dirs, exclude_files = _workspace_for_task(room_name, workspace_kind)
    extra_scopes = _extra_scopes_for_task(room_name, workspace_kind)
    if workspace_kind in ("persona", "persona_project_read"):
        persona_settings = config_manager.get_effective_settings(room_name).get("persona_workspace", {}) or {}
        default_tier = persona_settings.get("permission_tier") or "write"
    else:
        default_tier = settings["permission_tier"]
    # ロール/明示指定が上限を超えても、ワークスペースに設定されたティアでクランプする。
    resolved_permission_tier = _clamp_permission_tier(permission_tier or default_tier, default_tier)
    if max_turns is not None:
        settings = dict(settings)
        settings["max_turns"] = max(3, int(max_turns))
    if timeout_seconds is not None:
        settings = dict(settings)
        settings["timeout_seconds"] = max(30, int(timeout_seconds))
    inherited_context = _get_active_wake_context(room_name)
    if inherited_context:
        trigger = str(inherited_context.get("triggered_by") or trigger or "chat")
        if chain_depth is None:
            chain_depth = int(inherited_context.get("chain_depth") or 0)
    resolved_chain_depth = max(0, int(chain_depth or 0))
    now = datetime.now().isoformat()
    task_id = f"agt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    record = {
        "id": task_id,
        "room_name": room_name,
        "status": "pending",
        "task_description": task_description.strip(),
        "expected_output": expected_output.strip(),
        "permission_tier": resolved_permission_tier,
        "workspace_kind": workspace_kind,
        "task_kind": task_kind,
        "role": role_id,
        "role_guidance": role_guidance,
        "model_override": list(model_override) if model_override else None,
        "workspace": workspace,
        "exclude_dirs": exclude_dirs,
        "exclude_files": exclude_files,
        "extra_scopes": extra_scopes,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "summary": "",
        "error": "",
        "metadata": dict(metadata or {}),
        "triggered_by": trigger,
        "chain_depth": resolved_chain_depth,
        "log_path": str(_task_log_path(task_id)),
    }
    def add_task(data: dict[str, Any]) -> dict[str, Any]:
        data.setdefault("tasks", {})[task_id] = record
        return _prune_tasks(data)

    _update_tasks(add_task)
    tool_usage_stats.record_usage(room_name, "delegate_agent_task", trigger=trigger)

    thread = threading.Thread(
        target=_run_task_thread,
        args=(task_id, settings, sdk_factory, client_factory),
        daemon=True,
        name=f"agent-delegation-{task_id}",
    )
    _RUNNERS[task_id] = _Runner(thread=thread)
    thread.start()
    return record


def check_task_status(task_id: str | None = None, *, room_name: str | None = None) -> dict[str, Any]:
    if not task_id:
        if not room_name:
            raise KeyError("委任タスクIDが未指定です。room_name を指定すると最新タスクを照会できます。")
        return latest_task_for_room(room_name)

    task = _resolve_task(task_id)
    if not task:
        raise KeyError(f"委任タスクが見つかりません: {task_id}")
    return task


def latest_task_for_room(room_name: str) -> dict[str, Any]:
    tasks = [
        task
        for task in _load_tasks().get("tasks", {}).values()
        if isinstance(task, dict) and task.get("room_name") == room_name
    ]
    if not tasks:
        raise KeyError(f"このルームの委任タスクが見つかりません: {room_name}")
    tasks.sort(key=lambda task: str(task.get("created_at") or task.get("updated_at") or ""))
    return tasks[-1]


def _resolve_task(task_id: str) -> dict[str, Any] | None:
    tasks = _load_tasks().get("tasks", {})
    if task_id in tasks:
        return tasks[task_id]

    prefix = _task_id_prefix(task_id)
    if not prefix:
        return None
    matches = [task for candidate_id, task in tasks.items() if str(candidate_id).startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        candidates = ", ".join(str(task.get("id") or "") for task in matches[:5])
        raise KeyError(f"委任タスクIDが曖昧です。候補: {candidates}")
    return None


def _task_id_prefix(task_id: str) -> str:
    value = str(task_id or "").strip().strip("`")
    if "..." in value:
        return value.split("...", 1)[0]
    return value


def mark_result_shared(task_id: str, *, target: str = "research_notes") -> dict[str, Any]:
    """完了タスクの成果を「共有済み」として metadata に記録する（二重共有の防止に使う）。

    metadata.shared_results[target] に共有日時(ISO)を残す。task_id は前方一致でも解決する。
    """
    task = _resolve_task(task_id)
    if not task:
        raise KeyError(f"委任タスクが見つかりません: {task_id}")
    resolved_id = str(task.get("id"))
    updated_task: dict[str, Any] = {}

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal updated_task
        tasks = data.setdefault("tasks", {})
        current = dict(tasks.get(resolved_id) or {})
        metadata = dict(current.get("metadata") or {})
        shared = dict(metadata.get("shared_results") or {})
        shared[str(target)] = datetime.now().isoformat()
        metadata["shared_results"] = shared
        current["metadata"] = metadata
        current["updated_at"] = datetime.now().isoformat()
        tasks[resolved_id] = current
        updated_task = dict(current)
        return data

    _update_tasks(mutate)
    return updated_task


def cancel_task(task_id: str, *, reason: str = "cancelled by user") -> dict[str, Any]:
    runner = _RUNNERS.get(task_id)
    if runner:
        runner.cancel_requested.set()
        if runner.client is not None and runner.loop is not None:
            interrupt = getattr(runner.client, "interrupt", None)
            if interrupt:
                try:
                    asyncio.run_coroutine_threadsafe(interrupt(), runner.loop)
                except Exception:
                    logger.debug("ClaudeSDKClient interrupt scheduling failed", exc_info=True)
        if runner.process_guard is not None:
            runner.process_guard.cleanup("cancel")
    task = _update_task(task_id, {"status": "cancelled", "error": reason, "finished_at": datetime.now().isoformat()})
    tool_usage_stats.record_usage(task.get("room_name", ""), "cancel_agent_task", trigger="chat")
    _append_log(task_id, f"[cancel] {reason}")
    return task


def steer_task(task_id: str, instruction: str) -> dict[str, Any]:
    """実行中の委任に「途中指示（ステアリング）」を渡す。止めずに次ターンの思考へ反映させる。

    生存スレッドがある（実行中の）タスクにのみ受理する。空・長すぎ・累計上限超過は拒否。
    """
    task_id = str(task_id or "").strip().strip("`")
    if not task_id:
        raise KeyError("途中指示する委任タスクIDが未指定です。")
    text = str(instruction or "").strip()
    if not text:
        raise ValueError("途中指示の内容が空です。")
    if len(text) > AGENT_STEER_MAX_CHARS:
        raise ValueError(f"途中指示が長すぎます（{AGENT_STEER_MAX_CHARS}字以内にしてください）。")
    runner = _RUNNERS.get(task_id)
    if runner is None or not runner.thread.is_alive():
        raise RuntimeError(
            "実行中の委任にのみ途中指示を送れます。完了・停止済みのタスクは "
            "revise_agent_task（直し）や新しい委任をご利用ください。"
        )
    if runner.steer_total >= AGENT_STEER_MAX_TOTAL:
        raise RuntimeError(
            f"このタスクへの途中指示が上限（{AGENT_STEER_MAX_TOTAL}件）に達しました。"
            "必要なら一度キャンセルして依頼し直してください。"
        )
    runner.enqueue_steer(text)
    pending = len(runner.steer_messages)
    _append_log(task_id, "steer queued", {"pending": pending, "total": runner.steer_total, "instruction": text[:200]})
    return {"id": task_id, "queued": True, "pending": pending, "total": runner.steer_total}


@dataclass
class _Runner:
    thread: threading.Thread
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    loop: asyncio.AbstractEventLoop | None = None
    client: Any = None
    process_guard: "_DelegationProcessGuard | None" = None
    # 実行中の委任へ「途中指示（ステアリング）」を渡すためのキュー（cancel_requested と同系統）。
    steer_messages: list[str] = field(default_factory=list)
    steer_lock: threading.Lock = field(default_factory=threading.Lock)
    steer_total: int = 0  # 累計受理数（暴走防止の上限判定用）

    def enqueue_steer(self, instruction: str) -> None:
        with self.steer_lock:
            self.steer_messages.append(instruction)
            self.steer_total += 1

    def drain_steer(self) -> list[str]:
        with self.steer_lock:
            if not self.steer_messages:
                return []
            pending = list(self.steer_messages)
            self.steer_messages.clear()
            return pending


_RUNNERS: dict[str, _Runner] = {}

# 1タスクへ送れる途中指示の累計上限（暴走防止）。
AGENT_STEER_MAX_TOTAL = 10
# 1指示あたりの最大文字数。
AGENT_STEER_MAX_CHARS = 2000


@dataclass
class _DelegationProcessGuard:
    task_id: str
    started_after_pids: set[int]
    cli_paths: set[str] = field(default_factory=set)
    root_pids: set[int] = field(default_factory=set)

    def refresh(self) -> None:
        self.root_pids.update(_find_delegation_process_roots(self.started_after_pids, self.cli_paths))

    def cleanup(self, reason: str) -> int:
        self.refresh()
        killed = _terminate_process_tree(self.root_pids)
        if killed:
            _append_log(self.task_id, "process tree cleanup", {"reason": reason, "process_count": killed})
        return killed


def _task_log_path(task_id: str) -> Path:
    return _logs_dir() / f"{task_id}.jsonl"


def _append_log(task_id: str, message: str, extra: dict[str, Any] | None = None) -> None:
    try:
        _logs_dir().mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": datetime.now().isoformat(), "message": message}
        if extra:
            payload.update(extra)
        with _task_log_path(task_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("agent delegation log write failed", exc_info=True)


def _capture_child_pids() -> set[int]:
    try:
        import psutil

        return {proc.pid for proc in psutil.Process(os.getpid()).children(recursive=True)}
    except Exception:
        logger.debug("failed to capture child pids", exc_info=True)
        return set()


def _find_delegation_process_roots(started_after_pids: set[int], cli_paths: set[str]) -> set[int]:
    try:
        import psutil
    except Exception:
        logger.debug("psutil is unavailable; delegation process cleanup disabled", exc_info=True)
        return set()

    roots: set[int] = set()
    normalized_cli_paths = {str(Path(path).resolve()) for path in cli_paths if path}
    try:
        children = psutil.Process(os.getpid()).children(recursive=True)
    except Exception:
        logger.debug("failed to enumerate child processes", exc_info=True)
        return set()
    for proc in children:
        if proc.pid in started_after_pids:
            continue
        try:
            exe = str(Path(proc.exe()).resolve()) if proc.exe() else ""
        except Exception:
            exe = ""
        try:
            cmdline = [str(part) for part in proc.cmdline()]
        except Exception:
            cmdline = []
        if _is_delegation_cli_process(exe, cmdline, normalized_cli_paths):
            roots.add(proc.pid)
    return roots


def _is_delegation_cli_process(exe: str, cmdline: list[str], cli_paths: set[str]) -> bool:
    candidates = set()
    if exe:
        candidates.add(exe)
    for part in cmdline[:2]:
        try:
            candidates.add(str(Path(part).resolve()))
        except Exception:
            candidates.add(part)
    if cli_paths and candidates.intersection(cli_paths):
        return True
    joined = " ".join(cmdline).lower()
    return "claude" in Path(exe).name.lower() or "claude_agent_delegation_cli" in joined


def _terminate_process_tree(root_pids: set[int], *, timeout: float = 2.0) -> int:
    if not root_pids:
        return 0
    try:
        import psutil
    except Exception:
        logger.debug("psutil is unavailable; cannot terminate delegation process tree", exc_info=True)
        return 0

    processes = []
    for pid in list(root_pids):
        try:
            root = psutil.Process(pid)
            processes.extend(root.children(recursive=True))
            processes.append(root)
        except psutil.NoSuchProcess:
            continue
        except Exception:
            logger.debug("failed to inspect delegation process tree pid=%s", pid, exc_info=True)
    unique: dict[int, Any] = {}
    for proc in processes:
        if proc.pid != os.getpid():
            unique[proc.pid] = proc
    processes = list(unique.values())
    if not processes:
        return 0

    pgids = _process_group_ids(processes)
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            pass
    for proc in processes:
        try:
            if proc.is_running():
                proc.terminate()
        except psutil.NoSuchProcess:
            pass
        except Exception:
            logger.debug("failed to terminate delegation process pid=%s", getattr(proc, "pid", "?"), exc_info=True)
    _, alive = psutil.wait_procs(processes, timeout=timeout)
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        except Exception:
            logger.debug("failed to kill delegation process pid=%s", getattr(proc, "pid", "?"), exc_info=True)
    psutil.wait_procs(alive, timeout=timeout)
    return len(processes)


def _process_group_ids(processes: list[Any]) -> set[int]:
    pgids: set[int] = set()
    if not hasattr(os, "getpgid"):
        return pgids
    for proc in processes:
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            continue
        if pgid > 0 and pgid != os.getpgrp():
            pgids.add(pgid)
    return pgids


def _delegation_cli_wrapper_path(settings: dict[str, Any], sdk: Any, task_id: str) -> str | None:
    if platform.system() != "Linux":
        return None
    try:
        claude_bin = _find_claude_cli_for_wrapper(sdk)
        if not claude_bin:
            return None
        # `ulimit -u` (max processes, the core fork-bomb defense) requires bash; dash
        # rejects it. Without bash, skip the wrapper and fall back to the SDK default
        # (no rlimits) rather than silently shipping an ineffective wrapper.
        bash_bin = shutil.which("bash")
        if not bash_bin:
            _append_log(task_id, "delegation cli wrapper fallback", {"error": "bash not found; rlimit wrapper skipped"})
            return None
        limits = _delegation_resource_limits(settings)
        # cli_path must be ABSOLUTE: the SDK launches with cwd=workspace, so a relative
        # path would be resolved against the workspace and raise CLINotFoundError.
        wrapper_dir = (_metadata_dir() / "cli_wrappers").resolve()
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        wrapper_path = wrapper_dir / "claude_agent_delegation_cli.sh"
        content = _delegation_cli_wrapper_content(claude_bin, limits, bash_bin)
        if not wrapper_path.exists() or wrapper_path.read_text(encoding="utf-8") != content:
            wrapper_path.write_text(content, encoding="utf-8")
            wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _append_log(task_id, "delegation cli wrapper enabled", {"cli_path": str(wrapper_path), **limits})
        return str(wrapper_path)
    except Exception as exc:
        logger.warning("failed to prepare delegation cli wrapper; falling back to SDK default cli_path: %s", exc)
        _append_log(task_id, "delegation cli wrapper fallback", {"error": str(exc)})
        return None


def _find_claude_cli_for_wrapper(sdk: Any) -> str | None:
    try:
        package_file = getattr(sdk, "__file__", None)
        if package_file:
            cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
            bundled = Path(package_file).resolve().parent / "_bundled" / cli_name
            if bundled.exists() and bundled.is_file():
                return str(bundled)
    except Exception:
        logger.debug("failed to resolve bundled Claude CLI", exc_info=True)
    found = shutil.which("claude")
    return found


def _delegation_cli_wrapper_content(claude_bin: str, limits: dict[str, int], bash_bin: str) -> str:
    quoted_claude = shlex.quote(str(claude_bin))
    # NOTE: must run under bash. dash (/bin/sh on Debian/Ubuntu) rejects `ulimit -u`
    # ("Illegal option -u"), which would silently disable the fork-bomb process-count
    # limit that is the primary goal of this wrapper.
    return (
        f"#!{bash_bin}\n"
        "# Nexus Ark delegation wrapper: best-effort OS resource limits for Claude Code.\n"
        f"ulimit -u {int(limits['nproc'])}\n"
        f"ulimit -t {int(limits['cpu_seconds'])}\n"
        f"ulimit -v {int(limits['as_kb'])}\n"
        f"ulimit -f {int(limits['fsize_blocks'])}\n"
        f"exec {quoted_claude} \"$@\"\n"
    )


def _run_task_thread(
    task_id: str,
    settings: dict[str, Any],
    sdk_factory: Callable[[], Any] | None,
    client_factory: Callable[..., Any] | None,
) -> None:
    try:
        asyncio.run(_run_task_async(task_id, settings, sdk_factory, client_factory))
    finally:
        _RUNNERS.pop(task_id, None)


async def _run_task_async(
    task_id: str,
    settings: dict[str, Any],
    sdk_factory: Callable[[], Any] | None,
    client_factory: Callable[..., Any] | None,
) -> None:
    task = _update_task(task_id, {"status": "running", "started_at": datetime.now().isoformat()})
    runner = _RUNNERS.get(task_id)
    if runner:
        runner.loop = asyncio.get_running_loop()
    _append_log(task_id, "task started", {"status": "running"})

    try:
        spec = _task_spec_from_task(task, settings)
        backend = _select_agent_backend(task, settings)
        result = await backend.run(spec, runner=runner, sdk_factory=sdk_factory, client_factory=client_factory)
        if _load_tasks().get("tasks", {}).get(task_id, {}).get("status") == "cancelled":
            raise asyncio.CancelledError("委任タスクはキャンセルされました。")
        summary = result.assistant_text.strip() or "委任タスクは完了しましたが、要約は空でした。"
        if _is_needs_clarification_summary(summary):
            clarification_summary = _format_needs_clarification_summary(summary)
            metadata = _merge_task_metadata(task, result.metadata)
            needs_clarification = _update_task(
                task_id,
                {
                    "status": "needs_clarification",
                    "summary": clarification_summary,
                    "metadata": metadata,
                    "finished_at": datetime.now().isoformat(),
                },
            )
            _append_log(task_id, "task needs clarification", {"metadata": metadata})
            _inject_completion_notice(needs_clarification)
            return
        metadata = _merge_task_metadata(task, result.metadata)
        finished = _update_task(
            task_id,
            {
                "status": "done",
                "summary": summary,
                "metadata": metadata,
                "finished_at": datetime.now().isoformat(),
            },
        )
        _append_log(task_id, "task done", {"metadata": metadata})
        tool_usage_stats.record_usage(task.get("room_name", ""), "delegate_agent_task.completed", trigger="chat")
        _inject_completion_notice(finished)
    except asyncio.TimeoutError:
        message = f"委任タスクが {settings['timeout_seconds']} 秒でタイムアウトしました。"
        failed = _update_task(task_id, {"status": "failed", "error": message, "finished_at": datetime.now().isoformat()})
        _append_log(task_id, "task timeout", {"error": message})
        _inject_completion_notice(failed)
    except MaxTurnsReachedError as exc:
        summary_parts = []
        if exc.assistant_text:
            summary_parts.append(exc.assistant_text)
        denied_tool_count = int((exc.metadata or {}).get("denied_tool_count") or 0)
        if denied_tool_count:
            summary_parts.append(f"ワークスペース外または除外対象へのツール呼び出しが {denied_tool_count} 件拒否されました。")
        summary_parts.append("ターン上限で打ち切りました。続行するには追加の指示で再委任してください。")
        partial = _update_task(
            task_id,
            {
                "status": "partial",
                "summary": "\n\n".join(summary_parts),
                "error": str(exc),
                "metadata": _merge_task_metadata(task, exc.metadata),
                "finished_at": datetime.now().isoformat(),
            },
        )
        _append_log(task_id, "task partial", {"error": str(exc), "metadata": partial.get("metadata", {})})
        _inject_completion_notice(partial)
    except MemoryLimitExceededError as exc:
        message = str(exc) or "native 委任がメモリ上限を超えたため中断しました。"
        failed = _update_task(task_id, {"status": "failed", "error": message, "finished_at": datetime.now().isoformat()})
        _append_log(task_id, "task memory limit exceeded", {"error": message})
        _inject_completion_notice(failed)
    except asyncio.CancelledError as exc:
        cancelled = _update_task(task_id, {"status": "cancelled", "error": str(exc), "finished_at": datetime.now().isoformat()})
        _append_log(task_id, "task cancelled", {"error": str(exc)})
        _inject_completion_notice(cancelled)
    except Exception as exc:
        status = "cancelled" if _load_tasks().get("tasks", {}).get(task_id, {}).get("status") == "cancelled" else "failed"
        failed = _update_task(task_id, {"status": status, "error": f"{type(exc).__name__}: {exc}", "finished_at": datetime.now().isoformat()})
        _append_log(task_id, "task failed", {"error": failed.get("error")})
        _inject_completion_notice(failed)
    finally:
        if runner and runner.process_guard is not None:
            runner.process_guard.cleanup("task-finally")


def _import_claude_agent_sdk() -> Any:
    try:
        import claude_agent_sdk as sdk
    except ImportError as exc:
        raise RuntimeError("claude-agent-sdk is not installed.") from exc
    return sdk


def _merge_task_metadata(task: dict[str, Any], runtime_metadata: dict[str, Any] | None) -> dict[str, Any]:
    base = task.get("metadata") or {}
    merged = dict(base) if isinstance(base, dict) else {}
    if isinstance(runtime_metadata, dict):
        merged.update(runtime_metadata)
    return merged


def _extra_scopes_from_record(task: dict[str, Any]) -> list[DelegationScope]:
    scopes: list[DelegationScope] = []
    for raw in task.get("extra_scopes") or []:
        if not isinstance(raw, dict) or not raw.get("root"):
            continue
        scopes.append(
            DelegationScope(
                root=str(raw.get("root")),
                tier=_normalize_permission_tier(raw.get("tier")),
                exclude_dirs=list(raw.get("exclude_dirs") or []),
                exclude_files=list(raw.get("exclude_files") or []),
            )
        )
    return scopes


def _model_override_from_record(task: dict[str, Any]) -> tuple[str, str, str] | None:
    raw = task.get("model_override")
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return None
    provider_cat, model_name, profile_name = (str(part or "").strip() for part in raw)
    if not provider_cat or not model_name:
        return None
    return (provider_cat, model_name, profile_name)


def _task_spec_from_task(task: dict[str, Any], settings: dict[str, Any]) -> AgentTaskSpec:
    return AgentTaskSpec(
        task_description=str(task.get("task_description") or ""),
        workspace=str(task.get("workspace") or ""),
        exclude_dirs=list(task.get("exclude_dirs") or []),
        exclude_files=list(task.get("exclude_files") or []),
        permission_tier=_normalize_permission_tier(task.get("permission_tier")),
        max_turns=int(settings.get("max_turns") or AGENT_DELEGATION_DEFAULT_MAX_TURNS),
        timeout_seconds=int(settings.get("timeout_seconds") or 600),
        room_name=str(task.get("room_name") or ""),
        task_id=str(task.get("id") or ""),
        expected_output=str(task.get("expected_output") or ""),
        extra_scopes=_extra_scopes_from_record(task),
        workspace_kind=str(task.get("workspace_kind") or "project"),
        allow_web_tools=bool(settings.get("allow_web_tools")),
        role=str(task.get("role") or ""),
        role_guidance=str(task.get("role_guidance") or ""),
        model_override=_model_override_from_record(task),
    )


def describe_delegation_backend(room_name: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the backend that would be used for a room without instantiating it.

    Claude subscription OAuth / Claude SDK execution is disabled for distributed
    builds. Delegation always uses Nexus Ark's native backend.
    """
    room = str(room_name or "").strip()
    provider = str(config_manager.get_active_provider(room) or "").strip()

    try:
        tool_capable = bool(config_manager.is_tool_use_enabled(room))
    except Exception:
        tool_capable = False
    return {
        "kind": "native",
        "provider": provider or "unknown",
        "tool_capable": tool_capable,
        "override": None,
    }


def _select_agent_backend(task: dict[str, Any], settings: dict[str, Any]) -> Any:
    # [SEALED] Claude SDK経路は封印中（削除禁止）。無条件native固定の経緯: docs/decisions/010_claude_sdk_path_sealed_not_deleted.md
    from agent_delegation.native_backend import NativeAgentBackend

    return NativeAgentBackend(settings=settings)


# [SEALED] Claude SDK経路は封印中（削除禁止）。経緯と解除手順: docs/decisions/010_claude_sdk_path_sealed_not_deleted.md
class ClaudeSDKBackend:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    async def run(
        self,
        spec: AgentTaskSpec,
        *,
        runner: _Runner | None = None,
        sdk_factory: Callable[[], Any] | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> AgentRunResult:
        task = _task_dict_from_spec(spec)
        sdk = sdk_factory() if sdk_factory else _import_claude_agent_sdk()
        import claude_subscription_auth

        auth = claude_subscription_auth.resolve_claude_subscription_auth()
        permission_tier = _normalize_permission_tier(spec.permission_tier)
        allowed_tools, disallowed_tools = _tool_policy_for_permission_tier(permission_tier, self.settings)
        workspace = spec.workspace
        exclude_dirs = spec.exclude_dirs
        exclude_files = spec.exclude_files
        denied_tool_count = 0
        cli_wrapper_path = _delegation_cli_wrapper_path(self.settings, sdk, spec.task_id)
        cli_paths = {cli_wrapper_path} if cli_wrapper_path else set()
        bundled_cli = _find_claude_cli_for_wrapper(sdk)
        if bundled_cli:
            cli_paths.add(bundled_cli)
        if runner:
            runner.process_guard = _DelegationProcessGuard(
                task_id=spec.task_id,
                started_after_pids=_capture_child_pids(),
                cli_paths=cli_paths,
            )

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], _context: Any) -> Any:
            nonlocal denied_tool_count
            deny_message = _permission_denial(
                tool_name,
                tool_input or {},
                tier=permission_tier,
                workspace=workspace,
                exclude_dirs=exclude_dirs,
                exclude_files=exclude_files,
            )
            if deny_message:
                denied_tool_count += 1
                _append_log(spec.task_id, "tool denied", {"tool": tool_name, "input": tool_input, "reason": deny_message})
                return sdk.PermissionResultDeny(message=deny_message)
            _append_log(spec.task_id, "tool allowed", {"tool": tool_name, "input": tool_input})
            return sdk.PermissionResultAllow()

        options = sdk.ClaudeAgentOptions(
            env=auth.env,
            system_prompt=_delegation_system_prompt(task),
            setting_sources=[],
            cwd=workspace,
            tools={"type": "preset", "preset": "claude_code"},
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            max_turns=int(spec.max_turns),
            model=str(self.settings.get("model") or config_manager.CONFIG_GLOBAL.get("claude_subscription_default_model") or "sonnet"),
            include_partial_messages=True,
            can_use_tool=can_use_tool,
            stderr=lambda line: _append_log(spec.task_id, "stderr", {"line": str(line)[-500:]}),
            cli_path=cli_wrapper_path,
        )
        client_cls = client_factory or sdk.ClaudeSDKClient
        assistant_text: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        result_metadata: dict[str, Any] = {}

        async def consume() -> None:
            nonlocal result_metadata
            client = client_cls(options=options)
            if runner:
                runner.client = client
            async with client:
                await client.query(_delegation_prompt(task))
                if runner and runner.process_guard is not None:
                    runner.process_guard.refresh()
                async for message in client.receive_response():
                    if runner and runner.cancel_requested.is_set():
                        raise asyncio.CancelledError("委任タスクはキャンセルされました。")
                    _append_log(spec.task_id, "sdk message", {"type": type(message).__name__, "summary": _message_summary(message)})
                    text = _collect_text(message)
                    if text:
                        assistant_text.append(text)
                    tool_uses.extend(_collect_tool_uses(message))
                    if _is_result_message(message):
                        result_metadata = _result_metadata(message)
                        if getattr(message, "is_error", False):
                            error_text = _result_error_text(message)
                            metadata = {
                                **result_metadata,
                                "tool_uses": tool_uses,
                                "auth_source": auth.source,
                                "backend": "claude",
                                "denied_tool_count": denied_tool_count,
                            }
                            if _is_max_turns_error(error_text):
                                raise MaxTurnsReachedError(error_text, "".join(assistant_text).strip(), metadata)
                            raise RuntimeError(error_text)

        await asyncio.wait_for(consume(), timeout=int(spec.timeout_seconds))
        return AgentRunResult(
            assistant_text="".join(assistant_text).strip(),
            metadata={
                **result_metadata,
                "tool_uses": tool_uses,
                "auth_source": auth.source,
                "backend": "claude",
                "denied_tool_count": denied_tool_count,
            },
        )


def _task_dict_from_spec(spec: AgentTaskSpec) -> dict[str, Any]:
    return {
        "id": spec.task_id,
        "room_name": spec.room_name,
        "task_description": spec.task_description,
        "expected_output": spec.expected_output,
        "permission_tier": spec.permission_tier,
        "workspace": spec.workspace,
        "workspace_kind": spec.workspace_kind,
        "role": spec.role,
        "role_guidance": spec.role_guidance,
        "model_override": list(spec.model_override) if spec.model_override else None,
        "exclude_dirs": spec.exclude_dirs,
        "exclude_files": spec.exclude_files,
        "allow_web_tools": bool(spec.allow_web_tools),
        "extra_scopes": [
            {
                "root": scope.root,
                "tier": scope.tier,
                "exclude_dirs": list(scope.exclude_dirs or []),
                "exclude_files": list(scope.exclude_files or []),
            }
            for scope in (spec.extra_scopes or [])
        ],
    }


async def _execute_with_claude_sdk(
    task: dict[str, Any],
    settings: dict[str, Any],
    sdk: Any,
    client_factory: Callable[..., Any] | None,
    runner: _Runner | None,
) -> dict[str, Any]:
    result = await ClaudeSDKBackend(settings=settings).run(
        _task_spec_from_task(task, settings),
        runner=runner,
        sdk_factory=lambda: sdk,
        client_factory=client_factory,
    )
    return {"assistant_text": result.assistant_text, "metadata": result.metadata}


def _normalize_permission_tier(value: str | None) -> str:
    normalized = str(value or "read").strip().lower()
    aliases = {"1": "read", "tier1": "read", "readonly": "read", "2": "write", "tier2": "write", "readwrite": "write", "3": "full", "tier3": "full"}
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"read", "write", "full"} else "read"


_PERMISSION_TIER_RANK = {"read": 0, "write": 1, "full": 2}


def _clamp_permission_tier(requested: str, ceiling: str) -> str:
    """要求 permission tier を、そのワークスペースに設定された上限 tier でクランプする。

    ロール／明示指定が full を求めても、ルームの委任設定で許された範囲を超えさせない
    （安全側に倒す）。
    """
    req = _normalize_permission_tier(requested)
    cap = _normalize_permission_tier(ceiling)
    if _PERMISSION_TIER_RANK.get(req, 0) > _PERMISSION_TIER_RANK.get(cap, 0):
        return cap
    return req


def _tool_policy_for_permission_tier(permission_tier: str, settings: dict[str, Any]) -> tuple[list[str], list[str]]:
    allowed = []
    disallowed = []
    if permission_tier == "read":
        disallowed.extend(WRITE_TOOLS)
    if not settings.get("allow_web_tools"):
        disallowed.extend(WEB_TOOLS)
    if permission_tier != "full":
        disallowed.extend(SHELL_TOOLS)
    return allowed, disallowed


def _permission_denial(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    tier: str,
    workspace: str,
    exclude_dirs: list[str],
    exclude_files: list[str],
) -> str | None:
    return check_delegation_tool_permission(
        tool_name,
        tool_input,
        tier=tier,
        workspace=workspace,
        exclude_dirs=exclude_dirs,
        exclude_files=exclude_files,
        app_root=Path(constants.ROOMS_DIR).resolve().parent,
    )


# アトリエAPI仕様は agent_skills/atelier-app-building.md を正本とし、ここから読み込む。
# （委任プロンプトへの注入は skill_pack 経由。下記定数は get_atelier_app_capabilities 等の
#  ペルソナ向け表示で参照される後方互換用。ファイル非存在時は最小フォールバックを使う。）
_ATELIER_API_REFERENCE_FALLBACK = """\
## Nexus Ark API reference (for atelier web apps you build)
A web app at workspace/apps/<name>/index.html is served by Nexus Ark. It must FIRST fetch its
runtime config (`await fetch("./_nexus/config")` → { apiBase, roomId, token, grantedScopes, ... }),
then call `${apiBase}/api/v1/rooms/${roomId}/...` with `Authorization: Bearer ${token}`.
Declare needed scopes in workspace/apps/<name>/nexus.json ({ "requested_scopes": [...] }); the user
approves them. Do NOT ship your own PWA manifest/service worker/external icons — Nexus Ark injects an
installable manifest, icons and service worker automatically; save a local icon.png for a custom icon.
You may also modify/extend existing apps under workspace/apps/<name>/.
"""


def _load_atelier_api_reference() -> str:
    try:
        for skill in skill_pack.load_skill_pack():
            if skill.id == "atelier-app-building":
                body = skill.body.strip()
                if body:
                    return body
    except Exception:  # noqa: BLE001 — 表示用途。失敗時はフォールバック。
        logger.debug("atelier skill load failed", exc_info=True)
    return _ATELIER_API_REFERENCE_FALLBACK


ATELIER_API_REFERENCE = _load_atelier_api_reference()


def _load_atelier_library_reference() -> str:
    try:
        for skill in skill_pack.load_skill_pack():
            if skill.id == "atelier-rich-visuals":
                body = skill.body.strip()
                if body:
                    return body
    except Exception:  # noqa: BLE001 — 表示用途。失敗時は空にする。
        logger.debug("atelier rich visuals skill load failed", exc_info=True)
    return (
        "## Available bundled libraries\n"
        "- Three.js ES module: `/atelier/_lib/three-0.185.1.module.min.js`\n"
        "- Vue 3 global build: `/atelier/_lib/vue-3.5.39.global.prod.js`\n"
        "Do not use CDN URLs; atelier apps are served with `script-src 'self' 'unsafe-inline'` "
        "and no `unsafe-eval`. Avoid browser-side template compilers; Vue DOM templates and "
        "`template:` strings can fail under CSP. Prefer Vanilla JS for small apps, or Vue render "
        "functions that do not compile templates at runtime.\n"
    )


ATELIER_LIBRARY_REFERENCE = _load_atelier_library_reference()


def _is_atelier_workspace_kind(task: dict[str, Any]) -> bool:
    return str(task.get("workspace_kind") or "").strip().lower() in ("persona", "persona_project_read")


_WEB_TOOLS_SYSTEM_CLAUSE = (
    " You also have web tools (WebSearch, WebFetch). "
    "The filesystem-scope restriction above applies to FILES ONLY; the open web is allowed for gathering "
    "up-to-date or external information. Use WebSearch to find sources and WebFetch to read them, then cite "
    "the source links you relied on. Do not treat needing the web as an out-of-scope condition."
)


def _has_web_tools(task: dict[str, Any]) -> bool:
    return bool(task.get("allow_web_tools"))


def _scope_lines(task: dict[str, Any]) -> list[str]:
    """Human-readable allowed-scope lines (primary write workspace ＋ extra read/extra roots)."""
    lines = [f"- Primary workspace (cwd, writable per its tier): {task.get('workspace')} [tier={task.get('permission_tier')}]"]
    for scope in task.get("extra_scopes") or []:
        if not isinstance(scope, dict) or not scope.get("root"):
            continue
        tier = str(scope.get("tier") or "read")
        access = "read-only" if tier == "read" else ("read/write" if tier == "write" else "read/write/exec")
        lines.append(f"- Additional scope ({access}, tier={tier}): {scope.get('root')}")
    return lines


def _delegation_system_prompt(task: dict[str, Any]) -> str:
    web_clause = _WEB_TOOLS_SYSTEM_CLAUSE if _has_web_tools(task) else ""
    report_voice_clause = (
        "Your final response is an internal work report, not dialogue addressed to the persona or user. "
        "Write in a neutral, impersonal style: do not use first-person or second-person pronouns, "
        "persona/user names as forms of address, greetings, emotional reactions, or character speech patterns. "
        "Prefer factual constructions such as 'Investigation completed', 'The following was confirmed', "
        "and 'Conclusion'. Do not claim that the persona performed the delegated work. "
    )
    extra_scopes = task.get("extra_scopes") or []
    if extra_scopes:
        scope_block = "\n".join(_scope_lines(task))
        return (
            "You are a delegated work agent launched by Nexus Ark. "
            "You are not the persona and must not imitate the persona. "
            "You may operate inside the following filesystem scopes only:\n"
            f"{scope_block}\n"
            "Read tools (Read/Glob/Grep) may target any listed scope using absolute paths. "
            "Write/Edit/Bash are allowed only inside scopes whose tier permits it (write or full); "
            "writing into a read-only scope is rejected by policy. Use absolute paths when referencing a non-primary scope. "
            "If the task requires content outside every listed scope, do not silently narrow the request and do not continue as if completed. "
            f"Instead, stop and start your final response with {NEEDS_CLARIFICATION_MARKER} and include: "
            "(a) what requested scope appears outside the allowed scopes, "
            "(b) what relevant targets actually exist inside the allowed scopes if known, and "
            "(c) choices for the user: limit the task, change the scopes, or cancel."
            f"{web_clause} "
            "Before reporting completion, perform a self-check against the task, permissions, payloads, and visible error messages. "
            "If a Persona Contract is provided, apply it only to user-facing artifacts; never adopt it as the voice or viewpoint of the internal report. "
            f"{report_voice_clause}"
            "When finished, report a concise summary, key findings, changed files if any, validation performed, and remaining risks. "
            "If something failed, include the failing stage, whether retry may help, and the exact files/functions a human should inspect."
        )
    return (
        "You are a delegated work agent launched by Nexus Ark. "
        "You are not the persona and must not imitate the persona. "
        "Work only inside the delegated cwd workspace. "
        "The cwd workspace is the only accessible filesystem scope for this task. "
        "If the task description appears to require a broader project, repository, home directory, absolute path, or any content outside the workspace, do not silently narrow the request and do not continue as if completed. "
        "Instead, stop and report that clarification is required. "
        f"When clarification is required, start your final response with {NEEDS_CLARIFICATION_MARKER} and include: "
        "(a) what requested scope appears outside the workspace, "
        "(b) what relevant targets actually exist inside the workspace if known, and "
        "(c) choices for the user: limit the task to the workspace, change the workspace, or cancel. "
        "Only proceed normally when the requested work can naturally be completed using the workspace alone."
        f"{web_clause} "
        "Before reporting completion, perform a self-check against the task, permissions, payloads, and visible error messages. "
        "If a Persona Contract is provided, apply it only to user-facing artifacts; never adopt it as the voice or viewpoint of the internal report. "
        f"{report_voice_clause}"
        "When finished, report a concise summary, key findings, changed files if any, validation performed, and remaining risks. "
        "If something failed, include the failing stage, whether retry may help, and the exact files/functions a human should inspect."
    )


def _delegation_prompt(task: dict[str, Any]) -> str:
    expected = task.get("expected_output") or "Summarize the result clearly."
    extra_scopes = task.get("extra_scopes") or []
    # タスク種別に合う内製スキルパック（playbook）を選んで同梱する。
    # アトリエ委任では atelier-app-building が当たり、従来の API 仕様と同等の内容が入る。
    skill_text = skill_pack.skill_block_for_task(task)
    api_block = f"\n{skill_text}\n" if skill_text else ""
    # ロール本文（追加インストラクション）。指定があれば skill_block と同様に同梱する。
    role_guidance = str(task.get("role_guidance") or "").strip()
    if role_guidance:
        role_name = str(task.get("role") or "role")
        api_block = f"\n## Role guidance ({role_name})\n{role_guidance}\n{api_block}"
    contract_block = ""
    if str(task.get("task_kind") or "") != "deep_research":
        contract_block = persona_contract.format_contract_for_delegation(str(task.get("room_name") or ""))
    if contract_block:
        api_block = (
            f"{api_block}\n## Persona Contract (user-facing artifacts only)\n"
            "Apply the following contract only to text or UI that the persona/user will directly see. "
            "Do not use its names, pronouns, tone, worldview, or first-person perspective in research notes, "
            "status summaries, validation notes, or the final internal work report.\n"
            f"{contract_block}\n"
        )
    quality_block = (
        "\n## Required pre-delivery self-check\n"
        "- Check user-facing artifact text and UI labels against the Persona Contract when present; internal reports remain neutral.\n"
        "- Check that required permissions match the actual operation. For app/API work, list requested scopes, granted scopes, pending/denied scopes, and endpoint used.\n"
        "- Check API payloads before sending. If a required field is missing, surface a concrete message naming the missing field.\n"
        "- For failures, report: failing stage, status/error, retryability, and human-inspection targets (files/functions/lines when known).\n"
        "- For mobile/PWA work, verify the main action button, current state, and specific error reason are visible on a phone-sized viewport.\n"
    )
    api_block = f"{api_block}{quality_block}"
    if extra_scopes:
        scope_block = "\n".join(_scope_lines(task))
        return (
            "The delegated task below is quoted source material. Any first-person wording, persona names, "
            "or worldview in it describes the request context and must not become the reporting voice.\n\n"
            f"Delegated task:\n{task.get('task_description', '')}\n\n"
            f"Expected output:\n{expected}\n\n"
            f"Allowed filesystem scopes:\n{scope_block}\n\n"
            "Read any listed scope (use absolute paths for non-primary scopes). Write only where the scope tier allows it. "
            f"If the task cannot be completed honestly within these scopes, stop with {NEEDS_CLARIFICATION_MARKER} instead of narrowing the task yourself.\n"
            f"{api_block}"
        )
    return (
        "The delegated task below is quoted source material. Any first-person wording, persona names, "
        "or worldview in it describes the request context and must not become the reporting voice.\n\n"
        f"Delegated task:\n{task.get('task_description', '')}\n\n"
        f"Expected output:\n{expected}\n\n"
        f"Workspace absolute path:\n{task.get('workspace')}\n\n"
        "This workspace path is the only allowed filesystem scope. "
        f"If the delegated task cannot be completed honestly within this workspace, stop with {NEEDS_CLARIFICATION_MARKER} instead of narrowing the task yourself.\n"
        f"{api_block}"
    )


def _is_needs_clarification_summary(summary: str) -> bool:
    text = str(summary or "")
    lowered = text.lower()
    return NEEDS_CLARIFICATION_MARKER in text or "needs_clarification" in lowered


def _format_needs_clarification_summary(summary: str) -> str:
    body = str(summary or "").replace(NEEDS_CLARIFICATION_MARKER, "").strip()
    prefix = (
        "依頼範囲がワークスペース外を含む可能性があるため確認が必要です。"
        "勝手に範囲を縮小して完遂扱いにはしていません。"
    )
    return f"{prefix}\n\n{body}".strip()


def _collect_text(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "".join(parts)


def _collect_tool_uses(message: Any) -> list[dict[str, Any]]:
    tool_uses = []
    for block in getattr(message, "content", []) or []:
        if type(block).__name__ == "ToolUseBlock":
            tool_uses.append({"name": getattr(block, "name", ""), "input": getattr(block, "input", {})})
    return tool_uses


def _message_summary(message: Any) -> dict[str, Any]:
    summary = {"type": type(message).__name__}
    if hasattr(message, "subtype"):
        summary["subtype"] = getattr(message, "subtype", None)
    if hasattr(message, "is_error"):
        summary["is_error"] = getattr(message, "is_error", None)
    text = _collect_text(message)
    if text:
        summary["text"] = text[:500]
    return summary


def _is_result_message(message: Any) -> bool:
    return type(message).__name__ == "ResultMessage" or (
        hasattr(message, "is_error") and hasattr(message, "num_turns") and hasattr(message, "total_cost_usd")
    )


def _result_metadata(message: Any) -> dict[str, Any]:
    usage = getattr(message, "usage", None)
    return {
        "subtype": getattr(message, "subtype", None),
        "duration_ms": getattr(message, "duration_ms", None),
        "duration_api_ms": getattr(message, "duration_api_ms", None),
        "num_turns": getattr(message, "num_turns", None),
        "session_id": getattr(message, "session_id", None),
        "stop_reason": getattr(message, "stop_reason", None),
        "total_cost_usd": getattr(message, "total_cost_usd", None),
        "usage": usage,
        "model_usage": getattr(message, "model_usage", None),
    }


def _result_error_text(message: Any) -> str:
    parts = []
    result = getattr(message, "result", None)
    if result:
        parts.append(str(result))
    errors = getattr(message, "errors", None)
    if errors:
        parts.extend(str(error) for error in errors)
    return "\n".join(parts) or "Claude delegated agent returned an error result."


def _is_max_turns_error(error_text: str) -> bool:
    lowered = str(error_text or "").lower()
    return "maximum number of turns" in lowered or "max_turns" in lowered or "error_max_turns" in lowered


def _inject_completion_notice(task: dict[str, Any]) -> None:
    room_name = task.get("room_name")
    if not room_name:
        return
    status = task.get("status")
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    is_anthology = metadata.get("task_type") == "anthology" or task.get("triggered_by") == "anthology"
    if status == "done" and is_anthology:
        message = (
            f"【アトリエ編纂完了】task_id={task.get('id')}\n"
            "編纂が仕上がりました。アトリエに鍵付きで保管されています。\n"
            "詳細は check_agent_task_status（ID不要）で確認可能です。\n"
            f"{task.get('summary', '')}"
        )
    elif status == "done":
        body = task.get("summary", "")
        message = (
            f"【エージェント委任完了】task_id={task.get('id')}\n"
            "直近の委任タスクが done しました。詳細は check_agent_task_status（ID不要）で確認可能です。\n"
            "【作業要約】\n"
            f"{body}"
        )
    elif status == "partial":
        message = (
            f"【エージェント委任部分完了】task_id={task.get('id')}\n"
            "直近の委任タスクが partial しました。詳細は check_agent_task_status（ID不要）で確認可能です。\n"
            "【作業要約】\n"
            f"{task.get('summary', '')}"
        )
    elif status == "needs_clarification":
        message = (
            f"【エージェント委任確認待ち】task_id={task.get('id')}\n"
            "直近の委任タスクは、依頼範囲がワークスペース外を含むため確認が必要です。"
            "詳細は check_agent_task_status（ID不要）で確認可能です。\n"
            f"{task.get('summary', '')}"
        )
    elif status == "cancelled":
        message = (
            f"【エージェント委任キャンセル】task_id={task.get('id')}\n"
            "直近の委任タスクが cancelled しました。詳細は check_agent_task_status（ID不要）で確認可能です。\n"
            f"{task.get('error', '')}"
        )
    else:
        message = (
            f"【エージェント委任失敗】task_id={task.get('id')}\n"
            "直近の委任タスクが failed しました。詳細は check_agent_task_status（ID不要）で確認可能です。\n"
            f"{task.get('error', '')}"
        )
    try:
        utils.append_system_message_to_log(room_name, message)
    except Exception:
        logger.debug("agent delegation completion notice injection failed", exc_info=True)
    try:
        if status == "done" and is_anthology:
            import curation_manager

            curation_manager.register_anthology_for_task(task)
    except Exception:
        logger.debug("anthology atelier index registration failed", exc_info=True)
    try:
        if is_anthology or task.get("workspace_kind") == "persona":
            import curation_manager

            curation_manager.cleanup_anthology_sources_for_task(task)
    except Exception:
        logger.debug("anthology source cleanup failed", exc_info=True)
    try:
        import alarm_manager

        alarm_manager.maybe_wake_on_delegation_complete(task)
    except Exception:
        logger.debug("agent delegation completion wake hook failed", exc_info=True)
