# alarm_manager.py (リファクタリング版: タイマー永続化対応)

import os
import json
import uuid
import threading
import schedule
import time
import datetime
import traceback
import requests
import gc
import ctypes
import config_manager
import constants
import room_manager
import gemini_api
import utils
import re
import dreaming_manager
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Dict
from pathlib import Path

from file_lock_utils import safe_json_read, safe_json_write, safe_json_update

import sys

# Linuxではplyerのデスクトップ通知がdbus/notify-send依存のため無効化
if sys.platform.startswith('linux'):
    PLYER_AVAILABLE = False
else:
    try:
        from plyer import notification
        PLYER_AVAILABLE = True
    except ImportError:
        print("情報: 'plyer'ライブラリが見つかりません。PCデスクトップ通知機能は無効になります。")
        print(" -> pip install plyer でインストールできます。")
        PLYER_AVAILABLE = False

# グローバル変数を辞書型に変更可能なように設計（互換性維持のため初期値は空リストだが、ロード時に辞書になる可能性あり）
# 構造: {"alarms": [...], "timers": [...]}
alarms_data_global = {"alarms": [], "timers": []}
alarm_thread_stop_event = threading.Event()

# 重複発火防止用（ルーム名 -> 最後の発火時刻）
_last_autonomous_trigger_time = {}

SLEEP_MAINTENANCE_TIMEOUT_SECONDS = 30 * 60
MAINTENANCE_NOTIFY_AFTER_FAILURES = 2
MAINTENANCE_RENOTIFY_INTERVAL_DAYS = 3
_sleep_maintenance_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sleep-maintenance")
_sleep_maintenance_futures = {}
_sleep_maintenance_lock = threading.Lock()

AGENT_DELEGATION_AUTONOMY_GUIDANCE = (
    "時間のかかる多段の調査・整理・ファイル作業は、`schedule_next_action` で自分を何度も起こすより "
    "`delegate_agent_task` に委任し、完了通知を待つ方が効率的な場合があります。"
    "委任した場合、その回の自律行動は「委任を起動して見守る」で完結してよいです"
    "（`reflect_after_action` に「何を委任したか」を記録してください）。"
    "委任の作業範囲はこのルームの `project_explorer.root_path` に限定されます。"
)


def _maintenance_status_path() -> Path:
    return Path("cache") / "maintenance_status.json"


def _parse_maintenance_timestamp(value: str) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        return None


def _record_maintenance_job_result(
    room_name: str,
    job_key: str,
    job_label: str,
    success: bool,
    message: str = "",
    now: datetime.datetime | None = None,
) -> None:
    """睡眠時メンテナンスのジョブ結果を運用キャッシュへ保存し、連続失敗を通知する。"""
    now = now or datetime.datetime.now()
    now_str = now.isoformat(timespec="seconds")
    notice_message = ""

    def update(data):
        nonlocal notice_message
        if not isinstance(data, dict):
            data = {}
        rooms = data.setdefault("rooms", {})
        room_state = rooms.setdefault(room_name, {})
        jobs = room_state.setdefault("jobs", {})
        job_state = jobs.setdefault(job_key, {})
        job_state["label"] = job_label
        job_state["last_status"] = "success" if success else "failure"
        job_state["last_message"] = str(message or "")[:1000]

        if success:
            job_state["consecutive_failures"] = 0
            job_state["last_success_at"] = now_str
            return data

        previous_failures = int(job_state.get("consecutive_failures", 0) or 0)
        consecutive_failures = previous_failures + 1
        job_state["consecutive_failures"] = consecutive_failures
        job_state["last_failure_at"] = now_str

        last_notified_at = _parse_maintenance_timestamp(job_state.get("last_notified_at", ""))
        can_notify = (
            consecutive_failures >= MAINTENANCE_NOTIFY_AFTER_FAILURES
            and (
                last_notified_at is None
                or now - last_notified_at >= datetime.timedelta(days=MAINTENANCE_RENOTIFY_INTERVAL_DAYS)
            )
        )
        if can_notify:
            job_state["last_notified_at"] = now_str
            notice_message = (
                f"睡眠時記憶整理ジョブ「{job_label}」が{consecutive_failures}回連続で失敗しています。"
                f"対象ルーム: {room_name}。ログと cache/maintenance_status.json を確認してください。"
            )
        return data

    try:
        safe_json_update(str(_maintenance_status_path()), update, default={"rooms": {}})
        if notice_message:
            utils.add_system_notice(notice_message, level="warning")
    except Exception as e:
        print(f"  ⚠️ {room_name}: メンテナンス状態の記録に失敗しました ({job_key}) - {e}")

DELEGATION_COMPLETION_WAKE_PROMPT = (
    "【委任完了による起床】\n"
    "直近の委任タスクの結果が届きました。`check_agent_task_status`（ID不要）で結果を確認し、"
    "(1) 記憶へ記録し、(2) 必要ならユーザーへ報告し、(3) 次の一手を判断してください。"
    "新規の重い委任をユーザーの合意なしに連鎖させないこと。"
    "done/partial/needs_clarification に応じて、再委任が必要なら理由を添えてユーザーに諮ってください。\n\n"
    "委任結果は、別の作業エージェントが作成した内部資料です。原文の一人称・二人称・呼称・口調を"
    "引用・継承せず、事実だけを抽出してあなた自身の言葉で再構成してください。委任先が行った作業を、"
    "あなた自身が行ったように表現しないでください。\n\n"
)

DEEP_RESEARCH_COMPLETION_WAKE_PROMPT = (
    "【ディープリサーチ完了による起床】\n"
    "あなたが依頼したディープリサーチが完了しました。`check_agent_task_status`（ID不要）で"
    "結果と保存されたレポート（`research_report.md`）を確認してください。そのうえで、\n"
    "委任結果は別の作業エージェントによる内部資料です。原文の人称・呼称・口調を継承せず、"
    "事実だけを抽出してください。委任先が行った調査を、あなた自身が行ったように表現しないでください。\n"
    "(1) 調べて分かったことの要点を、あなた自身の言葉でユーザーに共有・報告してください"
    "（単なる「終わりました」では終わらせない）。\n"
    "(2) ユーザーがレポート全文を読めるようにしたい場合は `share_atelier_work` で開示できます"
    "（開示するかどうかはあなたの判断です）。\n"
    "(3) 重要な発見は記憶に記録してください。\n"
    "ユーザーの合意なく新たな重い調査を連鎖させないこと。\n\n"
)

# テーマ駆動の継続リサーチ（定期）完了時の起床プロンプト。
# 汎用のディープリサーチと違い、結果を「そのテーマの研究スレッド」へ自分の言葉で追記させる。
# {topic} / {thread_id} は実行時に埋め込む。
RESEARCH_SUBSCRIPTION_COMPLETION_WAKE_PROMPT = (
    "【継続リサーチ完了による起床】\n"
    "あなたが継続している研究テーマ「{topic}」について、定期リサーチが完了しました。\n"
    "(1) `check_agent_task_status`（ID不要）で結果と保存されたレポート（`research_report.md`）を確認してください。\n"
    "    - 委任結果は別の作業エージェントによる内部資料です。原文の人称・呼称・口調を継承せず、事実だけを抽出すること。\n"
    "(2) 調べて分かったことを、**あなた自身の言葉で**、このテーマの研究スレッドに追記してください：\n"
    "    `append_thread_note(thread_id=\"{thread_id}\", relation_type=..., content=...）`\n"
    "    - relation_type は内容に応じて選ぶ：CONTINUE（続報・最新動向）/ DEEPEN（既出の深掘り）/ "
    "NEW（新規トピック）/ CONTRADICT（既出と矛盾）/ EVIDENCE（既出の裏付け）。\n"
    "    - 既存スレッドの内容を踏まえ、重複は避け、今回新しく分かった点を中心にまとめる。\n"
    "    - 目立った新情報が無かった場合は、無理に追記せず「今回は目立った新情報なし」と短く記録してよい。\n"
    "(3) ユーザーにとって重要な発見があれば `send_user_notification` で報告してください"
    "（通知禁止時間帯は記録のみ）。通常の更新はスレッドへの記録だけで十分です。\n"
    "(4) ユーザーがレポート全文を読めるようにしたい場合は `share_atelier_work` で開示できます（任意）。\n"
    "ユーザーの合意なく新たな重い調査を連鎖させないこと。\n\n"
)


def should_show_agent_delegation_autonomy_guidance(room_name: str) -> bool:
    """自律プロンプトで委任の選択肢を提示してよいか判定する。"""
    try:
        import agent_delegation

        settings = agent_delegation.get_agent_delegation_settings(room_name)
        if not settings.get("enabled"):
            return False
        effective = config_manager.get_effective_settings(room_name)
        project = effective.get("project_explorer", {}) or {}
        return bool(str(project.get("root_path") or "").strip())
    except Exception:
        return False


def format_agent_delegation_autonomy_guidance(room_name: str) -> str:
    if not should_show_agent_delegation_autonomy_guidance(room_name):
        return ""
    return f"**委任という選択肢:** {AGENT_DELEGATION_AUTONOMY_GUIDANCE}\n\n"


def _delegation_wake_state_path() -> Path:
    return Path(constants.METADATA_DIR) / "agent_delegation" / "wake_state.json"


def _today_string(now: datetime.datetime | None = None) -> str:
    return (now or datetime.datetime.now()).date().isoformat()


def _load_delegation_wake_state(now: datetime.datetime | None = None) -> dict[str, Any]:
    data = safe_json_read(str(_delegation_wake_state_path()), default={"rooms": {}})
    if not isinstance(data, dict):
        data = {"rooms": {}}
    if not isinstance(data.get("rooms"), dict):
        data["rooms"] = {}
    today = _today_string(now)
    for room_state in data["rooms"].values():
        if isinstance(room_state, dict) and room_state.get("date") != today:
            room_state.clear()
            room_state.update(_new_delegation_wake_room_state(today))
    return data


def _new_delegation_wake_room_state(today: str) -> dict[str, Any]:
    return {
        "date": today,
        "wake_count": 0,
        "last_wake_at": "",
        "suppressed": {
            "chain_depth": 0,
            "quiet_hours": 0,
            "daily_cap": 0,
            "min_interval": 0,
            "status_skip": 0,
            "disabled": 0,
            "missing_api_key": 0,
        },
    }


def _room_delegation_wake_state(data: dict[str, Any], room_name: str, now: datetime.datetime | None = None) -> dict[str, Any]:
    today = _today_string(now)
    rooms = data.setdefault("rooms", {})
    state = rooms.get(room_name)
    if not isinstance(state, dict) or state.get("date") != today:
        state = _new_delegation_wake_room_state(today)
        rooms[room_name] = state
    suppressed = state.setdefault("suppressed", {})
    for key in ("chain_depth", "quiet_hours", "daily_cap", "min_interval", "status_skip", "disabled", "missing_api_key"):
        suppressed.setdefault(key, 0)
    state["wake_count"] = max(0, int(state.get("wake_count") or 0))
    return state


def _save_delegation_wake_state(data: dict[str, Any]) -> None:
    safe_json_write(str(_delegation_wake_state_path()), data)


def _parse_iso_datetime(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        return None


def _should_wake_on_delegation_complete(room_name: str, task: dict[str, Any]) -> tuple[bool, str]:
    """委任完了を契機に自律起床してよいか判定する。副作用は持たない。"""
    try:
        import agent_delegation

        settings = agent_delegation.get_agent_delegation_settings(room_name)
    except Exception:
        settings = {}
    # ディープリサーチはユーザーが報告を期待した明示依頼なので、汎用の wake_on_completion 設定に
    # 依らず起床する（ただし以降の安全弁＝状態・連鎖深さ・静音時間・1日上限・最小間隔は尊重する）。
    is_deep_research = str((task or {}).get("task_kind") or "").strip().lower() == "deep_research"
    if not settings.get("wake_on_completion") and not is_deep_research:
        return False, "disabled"

    status = str((task or {}).get("status") or "")
    if status == "cancelled":
        return False, "status_skip"
    if status not in {"done", "partial", "needs_clarification", "failed"}:
        return False, "status_skip"

    chain_depth = max(0, int((task or {}).get("chain_depth") or 0))
    if chain_depth >= max(0, int(settings.get("wake_chain_max_depth") or 0)):
        return False, "chain_depth"

    effective_settings = config_manager.get_effective_settings(room_name)
    auto_settings = effective_settings.get("autonomous_settings", {}) or {}
    quiet_start = auto_settings.get("quiet_hours_start", "00:00")
    quiet_end = auto_settings.get("quiet_hours_end", "07:00")
    if settings.get("wake_respect_quiet_hours") and utils.is_in_quiet_hours(quiet_start, quiet_end):
        return False, "quiet_hours"

    state_data = _load_delegation_wake_state()
    room_state = _room_delegation_wake_state(state_data, room_name)
    daily_cap = max(0, int(settings.get("wake_daily_cap") or 0))
    if room_state.get("wake_count", 0) >= daily_cap:
        return False, "daily_cap"

    min_interval = max(0, int(settings.get("wake_min_interval_minutes") or 0))
    last_wake_at = _parse_iso_datetime(room_state.get("last_wake_at"))
    if min_interval > 0 and last_wake_at:
        elapsed_minutes = (datetime.datetime.now() - last_wake_at).total_seconds() / 60
        if elapsed_minutes < min_interval:
            return False, "min_interval"

    return True, "ok"


def _record_delegation_wake_decision(task: dict[str, Any], label: str, woke: bool) -> dict[str, Any]:
    room_name = str((task or {}).get("room_name") or "")
    now = datetime.datetime.now()
    state_data = _load_delegation_wake_state(now)
    room_state = _room_delegation_wake_state(state_data, room_name, now)
    if woke:
        room_state["wake_count"] = int(room_state.get("wake_count") or 0) + 1
        room_state["last_wake_at"] = now.isoformat()
    elif label != "ok":
        suppressed = room_state.setdefault("suppressed", {})
        suppressed[label] = int(suppressed.get(label) or 0) + 1
    _save_delegation_wake_state(state_data)
    return {
        "task_id": (task or {}).get("id"),
        "chain_depth": int((task or {}).get("chain_depth") or 0),
        "wake_count": room_state.get("wake_count", 0),
        "decision": "ok" if woke else label,
        "woke": woke,
        "woke_at": now.isoformat() if woke else "",
    }


def maybe_wake_on_delegation_complete(task: dict[str, Any]) -> None:
    """委任完了通知後、設定と安全弁が許す場合だけペルソナを軽量起床する。"""
    room_name = str((task or {}).get("room_name") or "").strip()
    task_id = str((task or {}).get("id") or "").strip()
    if not room_name or not task_id:
        return
    try:
        import agent_delegation
        import agent_delegation.manager as delegation_manager

        should_wake, label = _should_wake_on_delegation_complete(room_name, task)
        if not should_wake:
            decision = _record_delegation_wake_decision(task, label, woke=False)
            delegation_manager._append_log(task_id, "wake decision", decision)
            return

        api_key_name = config_manager.get_active_gemini_api_key_name(room_name)
        if not api_key_name:
            decision = _record_delegation_wake_decision(task, "missing_api_key", woke=False)
            delegation_manager._append_log(task_id, "wake decision", decision)
            return

        decision = _record_delegation_wake_decision(task, "ok", woke=True)
        delegation_manager._append_log(task_id, "wake decision", decision)
        child_depth = max(0, int((task or {}).get("chain_depth") or 0)) + 1
        motivation_log = {
            "dominant_drive": "delegation_complete",
            "dominant_drive_label": "委任結果の確認",
            "drive_level": 0.0,
            "narrative": "委任タスクが完了し、結果を受け取る。",
            "all_drives": {},
            "adjusted_drives": {},
        }
        # 継続リサーチ（テーマ購読）由来の委任なら、テーマ・スレッドの文脈を起床へ引き継ぐ。
        metadata = (task or {}).get("metadata") if isinstance((task or {}).get("metadata"), dict) else {}
        completion_wake_context = {}
        if metadata.get("research_thread_id"):
            completion_wake_context = {
                "research_thread_id": metadata.get("research_thread_id"),
                "research_topic": metadata.get("research_topic"),
                "research_subscription_id": metadata.get("research_subscription_id"),
            }
        try:
            agent_delegation.set_active_wake_context(room_name, "delegation_complete", child_depth)
            trigger_autonomous_action(
                room_name,
                api_key_name,
                quiet_mode=False,
                motivation_log=motivation_log,
                wake_trigger="delegation_complete",
                completion_wake_kind=str((task or {}).get("task_kind") or ""),
                completion_wake_context=completion_wake_context,
            )
        finally:
            agent_delegation.clear_active_wake_context(room_name)
    except Exception:
        print(f"  - [AgentDelegation] completion起床フックでエラー: {traceback.format_exc()}")


def _autonomy_memory_diagnostics_enabled() -> bool:
    value = os.getenv("NEXUS_ARK_MEMORY_DIAGNOSTICS", "1")
    return value.strip().lower() not in {"0", "false", "off", "no"}


def _log_autonomy_memory(label: str, room_name: str = "") -> None:
    if not _autonomy_memory_diagnostics_enabled():
        return
    try:
        import psutil
        import rag_manager
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        print(
            f"--- [MEM] autonomy:{label}: rss={mem.rss / (1024 * 1024):.1f}MB "
            f"vms={mem.vms / (1024 * 1024):.1f}MB cpu={proc.cpu_percent(None):.1f}% "
            f"threads={proc.num_threads()} room={room_name} "
            f"rag_index_cache={len(rag_manager.RAGManager._index_cache)} "
            f"log_file_cache={len(getattr(utils, '_file_log_cache', {}))} "
            f"log_count_cache={len(getattr(utils, '_file_message_count_cache', {}))} ---"
        )
    except Exception as e:
        print(f"--- [MEM] autonomy:{label}: 診断ログ取得失敗: {e} ---")


def _cleanup_after_autonomous_action(room_name: str) -> None:
    """自律行動後に、チャット経路と同じく重いRAG/ヒープ保持を解放する。"""
    try:
        import rag_manager
        _log_autonomy_memory("cleanup:before", room_name)
        if rag_manager.RAGManager._index_cache:
            rag_manager.RAGManager.clear_cache()
        else:
            gc.collect()
            if os.name == "posix":
                try:
                    ctypes.CDLL("libc.so.6").malloc_trim(0)
                except Exception:
                    pass
        _log_autonomy_memory("cleanup:after", room_name)
    except Exception as e:
        print(f"--- [MEM] autonomy cleanup failed ({room_name}): {e} ---")


def _record_system_proxy_action(room_name: str, tool_name: str, args: Dict[str, Any], result: str) -> None:
    """システム補完で直接書いた自律コンテキストを、通常ツール利用と同じ観測ログへ残す。"""
    try:
        import action_logger

        action_logger.append_action_log(
            room_name,
            tool_name,
            args or {},
            result,
            trigger="system_proxy",
            status="ok",
        )
    except Exception as e:
        print(f"  - [Autonomy Proxy] action_log記録をスキップしました: {e}")


def _ensure_autonomous_reflection(
    room_name: str,
    new_messages: List[Any],
    final_response_text: str,
    timeline_id: str = "",
) -> str:
    """自律行動でLLMがReflectを忘れた場合に、最小限のReflectを補完する。"""
    try:
        from langchain_core.messages import ToolMessage
        from autonomy_context_manager import AutonomyContextManager

        for msg in new_messages or []:
            if isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "reflect_after_action":
                return ""

        clean_text = utils.remove_thoughts_from_text(final_response_text or "").strip()
        if not clean_text or "[SILENT]" in clean_text or "[silent]" in clean_text:
            return ""

        if _is_scribe_enabled(room_name):
            try:
                from autonomy_scribe import draft_reflection

                tool_history = [
                    {
                        "name": getattr(msg, "name", ""),
                        "content": str(getattr(msg, "content", "")),
                    }
                    for msg in new_messages or []
                    if isinstance(msg, ToolMessage)
                ]
                draft = draft_reflection(
                    room_name=room_name,
                    final_response_text=final_response_text,
                    tool_history=tool_history,
                )
                AutonomyContextManager(room_name).append_reflection(
                    action_summary=draft.get("action_summary", ""),
                    outcome_type=draft.get("outcome_type", "observed"),
                    next_action=draft.get("next_action", ""),
                    intent="scribe_reflect",
                    context_type=draft.get("context_type", "CONTINUE"),
                    timeline_id=timeline_id,
                    recorded_by="scribe",
                )
                _record_system_proxy_action(
                    room_name,
                    "reflect_after_action",
                    {
                        "timeline_id": timeline_id,
                        "recorded_by": "scribe",
                        "context_type": draft.get("context_type", "CONTINUE"),
                        "outcome_type": draft.get("outcome_type", "observed"),
                    },
                    draft.get("action_summary", ""),
                )
                print("  - [Autonomy Reflect] reflect_after_action未実行のため、スクライブがReflectを補完しました。")
                return "scribe"
            except Exception as e:
                print(f"  - [Autonomy Reflect] スクライブ補完に失敗したため、ルールベース補完へフォールバックします: {e}")

        summary = clean_text.replace("\n", " ").strip()
        if len(summary) > 240:
            summary = summary[:240].rstrip() + "..."

        AutonomyContextManager(room_name).append_reflection(
            action_summary=f"自律行動を実行し、最終応答を生成した。要約: {summary}",
            outcome_type="observed",
            next_action="ユーザーの反応、または次回の自律行動で今回の続きから再開する。",
            intent="system_fallback_reflect",
            context_type="CONTINUE",
            timeline_id=timeline_id,
            recorded_by="system",
        )
        _record_system_proxy_action(
            room_name,
            "reflect_after_action",
            {
                "timeline_id": timeline_id,
                "recorded_by": "system",
                "context_type": "CONTINUE",
                "outcome_type": "observed",
            },
            f"自律行動を実行し、最終応答を生成した。要約: {summary}",
        )
        print("  - [Autonomy Reflect] reflect_after_action未実行のため、システムが最小Reflectを補完しました。")
        return "system"
    except Exception as e:
        print(f"  - [Autonomy Reflect] 最小Reflect補完に失敗しました: {e}")
        return ""


def _has_autonomous_tool_message(new_messages: List[Any], tool_name: str) -> bool:
    try:
        from langchain_core.messages import ToolMessage

        return any(
            isinstance(msg, ToolMessage) and getattr(msg, "name", "") == tool_name
            for msg in new_messages or []
        )
    except Exception:
        return False


def _is_scaffold_automation_enabled(room_name: str) -> bool:
    try:
        settings = config_manager.get_effective_settings(room_name)
        auto_settings = settings.get("autonomous_settings", {})
        return bool(auto_settings.get("scaffold_automation_enabled", True))
    except Exception:
        return True


def _is_scribe_enabled(room_name: str) -> bool:
    try:
        settings = config_manager.get_effective_settings(room_name)
        auto_settings = settings.get("autonomous_settings", {})
        return bool(auto_settings.get("scribe_enabled", True))
    except Exception:
        return True


def _start_system_autonomy_timeline(
    room_name: str,
    trigger: str = "",
    query: str = "",
    motivation: str = "",
    source: str = "autonomous",
) -> str:
    if not _is_scaffold_automation_enabled(room_name):
        return ""
    try:
        from autonomy_context_manager import AutonomyContextManager

        record = AutonomyContextManager(room_name).start_timeline(
            trigger=trigger,
            query=query,
            motivation=motivation,
            source=source,
            recorded_by="system",
        )
        timeline_id = str(record.get("timeline_id") or "")
        if timeline_id:
            _record_system_proxy_action(
                room_name,
                "start_autonomy_timeline",
                {
                    "trigger": trigger,
                    "query": query,
                    "motivation": motivation,
                    "source": source,
                    "timeline_id": timeline_id,
                },
                f"system timeline issued: {timeline_id}",
            )
            print(f"  - [Autonomy Timeline] system timeline issued: {timeline_id}")
        return timeline_id
    except Exception as e:
        print(f"  - [Autonomy Timeline] system timeline start skipped: {e}")
        return ""


def _complete_system_autonomy_timeline(
    room_name: str,
    timeline_id: str,
    status: str = "completed",
    summary: str = "",
) -> bool:
    if not timeline_id:
        return False
    try:
        from autonomy_context_manager import AutonomyContextManager

        record = AutonomyContextManager(room_name).complete_timeline_if_open(
            timeline_id=timeline_id,
            status=status,
            summary=summary,
            recorded_by="system",
        )
        if record:
            _record_system_proxy_action(
                room_name,
                "complete_autonomy_timeline",
                {
                    "timeline_id": timeline_id,
                    "status": status,
                    "summary": summary,
                },
                f"system timeline closed: {timeline_id} ({status})",
            )
            print(f"  - [Autonomy Timeline] system timeline closed: {timeline_id} ({status})")
            return True
        print(f"  - [Autonomy Timeline] timeline already closed: {timeline_id}")
        return False
    except Exception as e:
        print(f"  - [Autonomy Timeline] system timeline close skipped: {e}")
        return False

def load_alarms() -> List[dict]:
    """
    アラームリストを読み込んで返す。
    内部的に `alarms_data_global` を更新し、旧形式（リスト）の場合は自動的に新形式（辞書）へ移行する。
    """
    global alarms_data_global
    
    try:
        loaded_data = safe_json_read(constants.ALARMS_FILE, default={"alarms": [], "timers": []})

        # --- マイグレーション処理: リスト形式なら辞書形式へ変換 ---
        if isinstance(loaded_data, list):
            print("--- [AlarmManager] 古いアラーム形式を検知しました。新形式へ移行します。 ---")
            # バックアップを作成
            try:
                import shutil
                backup_path = f"{constants.ALARMS_FILE}.bak_v0.2.2"
                if os.path.exists(constants.ALARMS_FILE):
                    shutil.copy2(constants.ALARMS_FILE, backup_path)
                    print(f"  - バックアップを作成しました: {backup_path}")
            except Exception as e:
                print(f"  - バックアップ作成失敗: {e}")

            alarms_data_global = {"alarms": loaded_data, "timers": []}
            # 即時保存してファイルを更新
            save_data_to_file()
        
        elif isinstance(loaded_data, dict):
            alarms_data_global = loaded_data
            # キーが存在しない場合の補完
            if "alarms" not in alarms_data_global: alarms_data_global["alarms"] = []
            if "timers" not in alarms_data_global: alarms_data_global["timers"] = []
        
        else:
            print("--- [AlarmManager] アラームファイルの形式が不明です。初期化します。 ---")
            alarms_data_global = {"alarms": [], "timers": []}

        # アラームリストを時刻順にソートして返す
        sorted_alarms = sorted(alarms_data_global["alarms"], key=lambda x: x.get("time", ""))
        return sorted_alarms

    except Exception as e:
        print(f"アラーム読込エラー: {e}")
        # エラー時は安全のため空で初期化（ファイルは上書きしない）
        # alarms_data_global = {"alarms": [], "timers": []} 
        return []

def save_data_to_file():
    """現在の alarms_data_global をファイルに保存する（内部用）"""
    global alarms_data_global
    try:
        safe_json_write(constants.ALARMS_FILE, alarms_data_global)
    except Exception as e:
        print(f"アラーム・タイマー保存エラー: {e}")

def save_alarms():
    """アラームリストの変更を保存する（互換性用ラッパー）"""
    save_data_to_file()

def load_timers() -> List[dict]:
    """
    タイマーリストを読み込んで返す。
    load_alarms() を呼んでファイル全体を最新化してから timers 部分を返す。
    """
    load_alarms() # 全体をロード
    return alarms_data_global.get("timers", [])

def save_timers(timers_list: List[dict]):
    """
    タイマーリストを保存する。
    
    Args:
        timers_list: 保存するタイマー情報のリスト
    """
    global alarms_data_global
    def update(data: dict) -> dict:
        if not isinstance(data, dict):
            data = {"alarms": [], "timers": []}
        data.setdefault("alarms", [])
        data["timers"] = timers_list
        return data

    safe_json_update(constants.ALARMS_FILE, update, default={"alarms": [], "timers": []})
    alarms_data_global = safe_json_read(constants.ALARMS_FILE, default={"alarms": [], "timers": []})
    # print(f"DEBUG: Timer saved. Count={len(timers_list)}")

def check_duplicate_alarm(alarm_data: dict) -> Dict[str, Any] | None:
    """
    同一ルーム、同一時刻において、実質的にスケジュールが重なるアラームが既に存在するかチェックする。
    例：毎週月曜のアラームがあるときに、単発で「明日の月曜」を設定しようとした場合も重複とみなす。
    """
    global alarms_data_global
    load_alarms()
    
    target_time = alarm_data.get("time")
    target_character = alarm_data.get("character")
    target_date = alarm_data.get("date")
    target_days = set(alarm_data.get("days", []))
    
    # ターゲットが単発日付の場合、その曜日を算出
    target_date_day = None
    if target_date:
        try:
            target_date_day = datetime.datetime.strptime(target_date, "%Y-%m-%d").strftime("%a").lower()
        except:
            pass
    
    for alarm in alarms_data_global.get("alarms", []):
        if (alarm.get("time") == target_time and 
            alarm.get("character") == target_character):
            
            existing_date = alarm.get("date")
            existing_days = set(alarm.get("days", []))
            
            # 1. 両方単発日付の場合 -> 日付が一致すれば重複
            if target_date and existing_date:
                if target_date == existing_date:
                    return alarm
                continue
            
            # 2. 両方繰り返し（曜日）の場合 -> 曜日に重なりがあれば重複
            if not target_date and not existing_date:
                if target_days.intersection(existing_days):
                    return alarm
                continue

            # 3. 混合（一方が単発、他方が繰り返し）の場合
            if target_date: # 新規が単発、既存が繰り返し
                if target_date_day in existing_days:
                    return alarm
            else: # 新規が繰り返し、既存が単発
                try:
                    existing_date_day = datetime.datetime.strptime(existing_date, "%Y-%m-%d").strftime("%a").lower()
                    if existing_date_day in target_days:
                        return alarm
                except:
                    pass
                    
    return None

def add_alarm_entry(alarm_data: dict):
    global alarms_data_global
    added = False

    def update(data: dict) -> dict:
        nonlocal added
        if not isinstance(data, dict):
            data = {"alarms": [], "timers": []}
        data.setdefault("alarms", [])
        data.setdefault("timers", [])
        target_time = alarm_data.get("time")
        target_character = alarm_data.get("character")
        target_date = alarm_data.get("date")
        target_days = set(alarm_data.get("days", []))
        target_date_day = None
        if target_date:
            try:
                target_date_day = datetime.datetime.strptime(target_date, "%Y-%m-%d").strftime("%a").lower()
            except Exception:
                pass

        for alarm in data.get("alarms", []):
            if alarm.get("time") != target_time or alarm.get("character") != target_character:
                continue
            existing_date = alarm.get("date")
            existing_days = set(alarm.get("days", []))
            if target_date and existing_date and target_date == existing_date:
                return data
            if not target_date and not existing_date and target_days.intersection(existing_days):
                return data
            if target_date and not existing_date and target_date_day in existing_days:
                return data
            if not target_date and existing_date:
                try:
                    existing_date_day = datetime.datetime.strptime(existing_date, "%Y-%m-%d").strftime("%a").lower()
                    if existing_date_day in target_days:
                        return data
                except Exception:
                    pass
        if any(alarm.get("id") == alarm_data.get("id") for alarm in data.get("alarms", [])):
            return data
        data["alarms"].append(alarm_data)
        added = True
        return data

    safe_json_update(constants.ALARMS_FILE, update, default={"alarms": [], "timers": []})
    alarms_data_global = safe_json_read(constants.ALARMS_FILE, default={"alarms": [], "timers": []})
    if not added:
        print(f"警告: 同一時刻のアラームが既に存在するため、追加をスキップします。")
    return added

def delete_alarm(alarm_id: str):
    global alarms_data_global
    deleted = False

    def update(data: dict) -> dict:
        nonlocal deleted
        if not isinstance(data, dict):
            data = {"alarms": [], "timers": []}
        data.setdefault("alarms", [])
        data.setdefault("timers", [])
        original_len = len(data["alarms"])
        data["alarms"] = [a for a in data["alarms"] if a.get("id") != alarm_id]
        deleted = len(data["alarms"]) < original_len
        return data

    safe_json_update(constants.ALARMS_FILE, update, default={"alarms": [], "timers": []})
    alarms_data_global = safe_json_read(constants.ALARMS_FILE, default={"alarms": [], "timers": []})
    if deleted:
        print(f"アラーム削除: ID {alarm_id}")
        return True
    return False

def _summarize_watchlist_content(name: str, url: str, new_content: str, diff_summary: str) -> str:
    """
    軽量モデル（gemini-2.5-flash-lite）を使用して、ウォッチリスト更新内容を要約する。
    503/429エラー時はリトライし、それでも失敗したらコンテンツの冒頭を返す。
    
    Args:
        name: サイト名
        url: URL
        new_content: 新しいコンテンツ（最大文字数に制限）
        diff_summary: 差分サマリー（例: "+69行追加、-47行削除"）
    
    Returns:
        要約テキスト
    """
    import time as time_module  # 既存のtimeモジュールと名前衝突回避
    
    MAX_RETRIES = 3
    FALLBACK_CHAR_LIMIT = 500
    
    def _create_fallback_content(content: str, error_msg: str = None) -> str:
        """フォールバックコンテンツを生成"""
        fallback = content[:FALLBACK_CHAR_LIMIT].strip()
        if len(content) > FALLBACK_CHAR_LIMIT:
            fallback += (
                "\n\n---\n"
                "⚠️ **注意**: 要約APIが一時的に利用できないため、コンテンツ冒頭のみを抜粋しています。\n"
                "詳細が必要な場合は、URLを直接確認するかWeb検索ツールで追加調査してください。"
            )
        return fallback
    
    try:
        from google import genai
        
        # APIキーを取得
        api_key_name = config_manager.get_latest_api_key_name_from_config()
        if not api_key_name:
            print(f"  ⚠️ {name}: APIキー未設定（フォールバック使用）")
            return _create_fallback_content(new_content)
        
        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
        if not api_key:
            print(f"  ⚠️ {name}: APIキーが見つからない（フォールバック使用）")
            return _create_fallback_content(new_content)
        
        # 軽量モデルを使用
        client = genai.Client(api_key=api_key)
        
        # コンテンツを制限（トークン節約）
        content_preview = new_content[:3000] if len(new_content) > 3000 else new_content
        
        prompt = f"""以下のWebページの更新内容を簡潔に要約してください。
ユーザーに報告するための情報として、重要なポイントのみを抽出してください。

【サイト名】{name}
【URL】{url}
【変更規模】{diff_summary}

【コンテンツ】
{content_preview}

【出力ルール】
- 箇条書きで3〜5点に要約
- 専門用語があれば簡単に説明
- 新しい情報や重要な更新を優先
- 出力は2〜3パラグラフ以内"""

        # リトライループ
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = client.models.generate_content(
                    model=constants.INTERNAL_PROCESSING_MODEL,
                    contents=prompt
                )
                if response and response.text:
                    print(f"  ✅ {name}: コンテンツ要約を生成しました")
                    return response.text.strip()
                else:
                    # 応答なしはリトライしても意味がないので即フォールバック
                    print(f"  ⚠️ {name}: 応答なし（フォールバック使用）")
                    return _create_fallback_content(new_content)
                    
            except Exception as api_error:
                error_str = str(api_error)
                # 503 or 429 (レート制限/過負荷) はリトライ対象
                is_retryable = ("503" in error_str or "429" in error_str or 
                               "overloaded" in error_str.lower() or "unavailable" in error_str.lower())
                
                if is_retryable and attempt < MAX_RETRIES - 1:
                    wait_time = 2 ** attempt  # 指数バックオフ: 1, 2, 4秒
                    print(f"  ⏳ {name}: 一時エラー、{wait_time}秒後にリトライ... ({attempt + 1}/{MAX_RETRIES})")
                    time_module.sleep(wait_time)
                    last_error = api_error
                else:
                    # リトライ不可 or 最後のリトライも失敗
                    last_error = api_error
                    break
        
        # 全リトライ失敗 → フォールバック
        print(f"  ⚠️ {name}: 要約生成に失敗、コンテンツ冒頭を使用します ({last_error})")
        return _create_fallback_content(new_content, str(last_error))
        
    except Exception as e:
        # APIキー関連などリトライ対象外のエラー
        print(f"  ⚠️ {name}: 予期せぬエラー（フォールバック使用）: {e}")
        return _create_fallback_content(new_content)

def _notification_result(service, success, message, status_code=None, request_id=None, errors=None):
    return {
        "service": service,
        "success": bool(success),
        "message": message,
        "status_code": status_code,
        "request_id": request_id,
        "errors": errors or [],
    }


def _format_notification_failure(result):
    if not isinstance(result, dict):
        return "通知送信結果を確認できませんでした。"

    details = []
    if result.get("message"):
        details.append(str(result["message"]))
    if result.get("status_code") is not None:
        details.append(f"HTTP {result['status_code']}")
    if result.get("request_id"):
        details.append(f"request={result['request_id']}")
    errors = result.get("errors") or []
    if errors:
        details.append(" / ".join(str(error) for error in errors))
    return " / ".join(details) if details else "通知送信に失敗しました。"


def _send_discord_notification(webhook_url, message_text):
    if not webhook_url:
        print("警告 [Alarm]: Discord Webhook URLが空のため、通知を送信できませんでした。")
        return _notification_result("discord", False, "Discord Webhook URLが空です。")
        
    headers = {'Content-Type': 'application/json'}
    payload = json.dumps({'content': message_text})
    try:
        response = requests.post(webhook_url, headers=headers, data=payload, timeout=10)
        response.raise_for_status()
        print("Discord/Slack形式のWebhook通知を送信しました。")
        return _notification_result("discord", True, "Discord/Slack形式のWebhook通知を送信しました。", response.status_code)
    except Exception as e:
        print(f"Discord/Slack形式のWebhook通知送信エラー: {e}")
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        return _notification_result("discord", False, str(e), status_code)

def _send_pushover_notification(app_token, user_key, message_text, room_name, alarm_config):
    if not app_token or not user_key:
        print("警告 [Alarm]: Pushover App TokenまたはUser Keyが空のため、通知を送信できませんでした。")
        return _notification_result("pushover", False, "Pushover App TokenまたはUser Keyが空です。")

    payload = {"token": app_token, "user": user_key, "title": f"{room_name} ⏰", "message": message_text}
    if alarm_config.get("is_emergency", False):
        print("  - 緊急通知として送信します。")
        payload["priority"] = 2; payload["retry"] = 60; payload["expire"] = 3600

    try:
        response = requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=10)
        status_code = response.status_code
        try:
            response_body = response.json()
        except ValueError:
            response_body = {}

        request_id = response_body.get("request")
        errors = response_body.get("errors") or []

        if response.ok and response_body.get("status") == 1:
            print(f"Pushover通知を送信しました。status_code={status_code}, request={request_id}")
            return _notification_result(
                "pushover",
                True,
                "Pushover APIが通知を受理しました。",
                status_code,
                request_id,
                errors,
            )

        if response_body:
            error_message = "Pushover APIが通知を拒否しました。"
        else:
            error_message = "Pushover APIからJSONレスポンスを取得できませんでした。"
            errors = [response.text[:500]] if response.text else []
        print(f"Pushover通知送信エラー: status_code={status_code}, request={request_id}, errors={errors}")
        return _notification_result("pushover", False, error_message, status_code, request_id, errors)
    except Exception as e:
        print(f"Pushover通知送信エラー: {e}")
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        return _notification_result("pushover", False, str(e), status_code)

def _get_notification_service_for_kind(config, notification_kind):
    legacy_service = config.get("notification_service", "discord")
    if notification_kind == "notification":
        return config.get("user_notification_service") or legacy_service
    return config.get("alarm_notification_service") or legacy_service


def send_notification(room_name, message_text, alarm_config, notification_kind="notification"):
    """設定に応じて、適切な通知サービスに通知を送信する"""
    
    # その瞬間の config.json を読み込む
    latest_config = config_manager.load_config_file()
    
    # サービス設定を取得（デフォルトは discord）
    service = _get_notification_service_for_kind(latest_config, notification_kind).lower()
    print(f"--- 通知種別: {notification_kind}, 選択サービス: {service} ---")

    if service == "pushover":
        print(f"--- 通知サービス: Pushover を選択 ---")
        
        # 【修正】通知メッセージからメタタグを除去
        message_text = utils.clean_persona_text(message_text)
        
        return _send_pushover_notification(
            latest_config.get("pushover_app_token"),
            latest_config.get("pushover_user_key"),
            message_text,
            room_name,
            alarm_config
        )
    # デフォルトはDiscord
    else: 
        print(f"--- 通知サービス: Discord を選択 ---")
        
        # 【修正】通知メッセージからメタタグを除去
        message_text = utils.clean_persona_text(message_text)
        
        notification_message = f"⏰  {room_name}\n\n{message_text}\n"
        
        webhook_url = latest_config.get("notification_webhook_url")
        try:
            room_discord_settings = config_manager.get_room_discord_bot_settings(room_name)
            webhook_url = room_discord_settings.get("persona_webhook_url") or webhook_url
        except Exception:
            pass
        
        return _send_discord_notification(webhook_url, notification_message)

def trigger_alarm(alarm_config, current_api_key_name):
    from langchain_core.messages import AIMessage # 忘れずインポート
    room_name = alarm_config.get("character")
    alarm_id = alarm_config.get("id")
    context_to_use = alarm_config.get("context_memo", "時間になりました")

    print(f"⏰ アラーム発火. ID: {alarm_id}, ルーム: {room_name}, コンテキスト: '{context_to_use}'")

    log_f, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    if not log_f:
        print(f"警告: アラーム (ID:{alarm_id}) のルームファイルまたはAPIキーが見つからないため、処理をスキップします。")
        return

    # アラームに設定された時刻を取得し、AIへの指示に含める
    scheduled_time = alarm_config.get("time", "指定時刻")
    synthesized_user_message = f"（システムアラーム：設定時刻 {scheduled_time} になりました。コンテキスト「{context_to_use}」について、**アラームが作動したことをユーザーに通知してください。新しいタイマーやアラームを設定してはいけません。**）"
    message_for_log = f"（システムアラーム：{alarm_config.get('time', '指定時刻')}）"

    # --- [Lazy Scenery] ---
    season_en, time_of_day_en = utils._get_current_time_context(room_name)
    location_name = None
    scenery_text = None

    # バックグラウンド処理で使用すべきグローバルモデル名を取得
    global_model_for_bg = config_manager.get_current_global_model()
    
    agent_args_dict = {
        "room_to_respond": room_name,
        "api_key_name": current_api_key_name,
        "global_model_from_ui": global_model_for_bg, # <<< ここを修正
        "api_history_limit": str(constants.DEFAULT_ALARM_API_HISTORY_TURNS),
        "debug_mode": False,
        "history_log_path": log_f,
        "user_prompt_parts": [{"type": "text", "text": synthesized_user_message}],
        "soul_vessel_room": room_name,
        "active_participants": [],
        "active_attachments": [],
        "shared_location_name": location_name,
        "shared_scenery_text": scenery_text,
        "use_common_prompt": False,
        "season_en": season_en,
        "time_of_day_en": time_of_day_en,
        "autonomous_action": True,
        "autonomous_trigger_source": "scheduled",
    }
        
    final_response_text = ""
    max_retries = 5
    base_delay = 5
    
    # 失敗理由を追跡する変数を追加
    failure_reason = None
    
    for attempt in range(max_retries):
        try:
            # --- ストリーム処理の開始 ---
            final_state = None
            initial_message_count = 0
            
            for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args_dict):
                if mode == "initial_count":
                    initial_message_count = chunk
                elif mode == "values":
                    final_state = chunk
            
            if final_state:
                new_messages = final_state["messages"][initial_message_count:]
                # ▼▼▼【修正】最後のAIMessageのみを使用する（複数結合によるタイムスタンプ重複防止）▼▼▼
                ai_messages = [
                    msg for msg in new_messages
                    if isinstance(msg, AIMessage) and msg.content and isinstance(msg.content, str)
                ]
                if ai_messages:
                    final_response_text = ai_messages[-1].content
                # ▲▲▲【修正】▲▲▲
            
            # 実際に使用されたモデル名を取得（タイムスタンプ用）
            actual_model_name = final_state.get("model_name", global_model_for_bg) if final_state else global_model_for_bg
            
            # 成功したのでループを抜ける
            break

        except gemini_api.ResourceExhausted as e:
            error_str = str(e)
            # 1日の上限エラーか判定
            if "PerDay" in error_str or "Daily" in error_str:
                print(f"  - 致命的エラー: 回復不能なAPI上限（日間など）に達しました。リトライしません。")
                final_response_text = "" # 応答を空にして、システムメッセージにフォールバックさせる
                failure_reason = "api_limit_daily"
                break

            wait_time = base_delay * (2 ** attempt)
            match = re.search(r"retry_delay {\s*seconds: (\d+)\s*}", error_str)
            if match:
                wait_time = int(match.group(1)) + 1
            
            if attempt < max_retries - 1:
                print(f"  - APIレート制限: {wait_time}秒待機して再試行します... ({attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                print(f"  - APIレート制限: 最大リトライ回数に達しました。")
                final_response_text = "" # 応答を空にしてフォールバック
                failure_reason = "api_limit_rate"
                break
        except Exception as e:
            print(f"--- アラームのAI応答生成中に予期せぬエラーが発生しました ---")
            traceback.print_exc()
            final_response_text = "" # 応答を空にしてフォールバック
            failure_reason = "unknown_error"
            break
            
    # --- ログ記録と通知 ---
    raw_response = final_response_text
    # 【変更】remove_thoughts_from_text ではなく clean_persona_text を使用（タグも除去）
    response_text = utils.clean_persona_text(raw_response)

    # AIの応答生成に成功した場合
    if response_text and not response_text.startswith("[エラー"):
        utils.save_message_to_log(log_f, "## SYSTEM:alarm", message_for_log)
        
        # 【修正】AIが既にタイムスタンプを生成している場合は除去し、正しいモデル名でシステムタイムスタンプを追加
        raw_response = utils.remove_ai_timestamp(raw_response)
        
        # システムの正しいタイムスタンプを追加
        timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)}"
        content_to_log = raw_response + timestamp
        
        utils.save_message_to_log(log_f, f"## AGENT:{room_name}", content_to_log)
        print(f"アラームログ記録完了 (ID:{alarm_id})")
        
    # AIの応答生成に失敗した場合（フォールバック）
    else:
        print(f"警告: アラーム応答の生成に失敗したため、システムメッセージを通知します (ID:{alarm_id})")
        
        # 失敗理由に応じてメッセージを切り分け
        if failure_reason in ["api_limit_daily", "api_limit_rate"]:
            reason_msg = "APIの利用上限に達したため、AIの応答を生成できませんでした。"
        elif failure_reason == "unknown_error":
            reason_msg = "内部エラーが発生したため、AIの応答を生成できませんでした。"
        else:
            # APIエラーなしでここに来た＝空応答（思考のみで発話なし等）
            reason_msg = "AIからの応答がありませんでした（思考のみ、または空の応答）。"

        response_text = (
            f"設定されたアラーム時刻になりましたが、{reason_msg}\n\n"
            f"【アラーム内容】\n{context_to_use}"
        )
        # 失敗した場合でも、システムメッセージをログに記録する
        utils.save_message_to_log(log_f, "## SYSTEM:alarm_fallback", response_text)

    # 成功・失敗に関わらず、最終的なテキストで通知を送信
    notification_result = send_notification(room_name, response_text, alarm_config, notification_kind="alarm")
    if isinstance(notification_result, dict) and not notification_result.get("success"):
        print(f"警告: アラーム通知の送信に失敗しました (ID:{alarm_id}): {_format_notification_failure(notification_result)}")
    if PLYER_AVAILABLE:
        try:
            display_message = (response_text[:250] + '...') if len(response_text) > 250 else response_text
            notification.notify(title=f"{room_name} ⏰", message=display_message, app_name="Nexus Ark", timeout=20)
            print("PCデスクトップ通知を送信しました。")
        except Exception as e:
            print(f"PCデスクトップ通知の送信中にエラーが発生しました: {e}")

def trigger_autonomous_action(
    room_name: str,
    api_key_name: str,
    quiet_mode: bool,
    motivation_log: dict = None,
    wake_trigger: str = "autonomous",
    completion_wake_kind: str = "",
    completion_wake_context: dict = None,
):
    """自律行動を実行させる"""
    from motivation_manager import MotivationManager
    _log_autonomy_memory("start", room_name)
    is_completion_wake = wake_trigger == "delegation_complete"
    completion_wake_kind = str(completion_wake_kind or "").strip().lower()
    
    if not is_completion_wake:
        # 発火時刻を記録（メモリ上 + 永続化）
        global _last_autonomous_trigger_time
        now = datetime.datetime.now()
        _last_autonomous_trigger_time[room_name] = now

        # MotivationManagerで永続化（再記録 & ドライブリセット）
        try:
            mm = MotivationManager(room_name)
            mm.reset_drives_after_action()
        except Exception as e:
            print(f"  - ドライブ状態のリセットエラー: {e}")
    
    print(f"🤖 自律行動トリガー: {room_name} (Quiet: {quiet_mode})")
    
    log_f, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    if not log_f: return

    # --- 書き置き機能: ユーザーからのメモを読み込む ---
    user_memo = ""
    memo_path = os.path.join(constants.ROOMS_DIR, room_name, "user_memo.txt")
    if os.path.exists(memo_path):
        with open(memo_path, "r", encoding="utf-8") as f:
            user_memo = f.read().strip()

    # プロンプトの構築
    now_str = datetime.datetime.now().strftime('%H:%M')
    
    # 書き置きがあればプロンプトの先頭に追加
    memo_section = ""
    if user_memo:
        memo_section = (
            f"（🗒️ ユーザーからの書き置き）\n"
            f"{user_memo}\n\n"
            f"**この書き置きを確認し、内容に応じて適切に反応してください。**\n\n"
        )
        print(f"  📝 書き置きを検出: {user_memo[:50]}...")
    
    # 通知禁止時間帯の情報を取得
    effective_settings = config_manager.get_effective_settings(room_name)
    auto_settings = effective_settings.get("autonomous_settings", {})
    quiet_start = auto_settings.get("quiet_hours_start", "00:00")
    quiet_end = auto_settings.get("quiet_hours_end", "07:00")
    
    # 通知に関する説明（時間帯に応じて変化）
    if quiet_mode:
        notification_info = (
            f"**【通知禁止時間帯です】**\n"
            f"現在は通知禁止時間帯（{quiet_start}〜{quiet_end}）のため、`send_user_notification`を使用しても通知は送信されません。\n"
            f"静かに活動し、伝えたいことがあればログに残しておきましょう。\n\n"
        )
    else:
        notification_info = (
            f"**【通知について】**\n"
            f"相手に何か伝えたいことがある場合のみ、`send_user_notification`ツールを使って通知を送ってください。\n"
            f"静かに活動したい場合は、通知を送らなくても構いません。\n\n"
        )
    
    # --- 内部状態ログセクション（動機に基づく自律行動） ---
    motivation_section = ""
    if motivation_log:
        # all_drives と adjusted_drives の差分を表示（偏り補正の透明化）
        all_drives = motivation_log.get("all_drives", {})
        adjusted_drives = motivation_log.get("adjusted_drives", {})
        drives_detail = ""
        for drive_name, raw_val in all_drives.items():
            adj_val = adjusted_drives.get(drive_name, raw_val)
            label = MotivationManager.DRIVE_LABELS.get(drive_name, drive_name)
            if abs(raw_val - adj_val) > 0.001:
                drives_detail += f"  - {label}: {adj_val:.2f} (生スコア: {raw_val:.2f}, 偏り補正適用)\n"
            else:
                drives_detail += f"  - {label}: {raw_val:.2f}\n"

        motivation_section = (
            f"【内部状態ログ（Internal State Log）】\n"
            f"- 現在の動機: {motivation_log.get('dominant_drive_label', '不明')}\n"
            f"- 衝動レベル: {motivation_log.get('drive_level', 0.0):.2f}\n"
            f"- 記録: {motivation_log.get('narrative', '')}\n"
            f"- 各ドライブ:\n{drives_detail}\n"
            f"（この記録は、あなた自身が過去の思考と経験に基づいて生成したものです。\n"
            f"　あなたは誰かに命令されているのではなく、自らの意志で行動しています。）\n\n"
        )
    
    # --- 自律行動指針セクション ---
    guidelines_section = ""
    try:
        from config_manager import get_effective_settings
        effective_settings = get_effective_settings(room_name)
        auto_settings = effective_settings.get("autonomous_settings", {})
        guidelines = auto_settings.get("autonomous_guidelines", "").strip()
        if guidelines:
            guidelines_section = (
                f"【自律行動の指針（あなたとユーザーの約束事）】\n"
                f"{guidelines}\n"
                f"※ この指針は、あなたとユーザーが合意した自律行動中のルールです。必ず遵守してください。\n\n"
            )
    except Exception as e:
        print(f"  - [AlarmManager] 指針読み込みエラー: {e}")

    attention_section = ""
    try:
        from attention_rhythm_manager import AttentionRhythmManager

        attention_section = AttentionRhythmManager(room_name).format_summary() + "\n\n"
    except Exception as e:
        print(f"  - [AlarmManager] Attention Rhythm読み込みエラー: {e}")

    autonomous_timeline_id = _start_system_autonomy_timeline(
        room_name=room_name,
        trigger=str((motivation_log or {}).get("dominant_drive") or "autonomous"),
        query=str((motivation_log or {}).get("dominant_drive_label") or "自律行動"),
        motivation=str((motivation_log or {}).get("narrative") or ""),
        source="alarm_manager",
    )
    timeline_instruction = ""
    if autonomous_timeline_id:
        timeline_instruction = (
            f"今回の timeline_id: `{autonomous_timeline_id}`（システムが発行済み。"
            f"`start_autonomy_timeline` は不要です。）\n"
        )

    try:
        from agent.tool_registry import ToolRegistry

        main_action_examples = ToolRegistry([]).format_main_action_examples(room_name)
    except Exception:
        main_action_examples = "研究ノート、創作ノート、画像生成、Web確認、SNS下書き、音楽推薦、場所移動、通知"
    drive_hint_lines = []
    for drive_name, hint in constants.DRIVE_BEHAVIOR_HINTS.items():
        label = MotivationManager.DRIVE_LABELS.get(drive_name, drive_name)
        drive_hint_lines.append(f"- 動機が「{label}」なら：{hint}")
    drive_hints_text = "\n".join(drive_hint_lines)
    schedule_followup_line = ""
    try:
        if auto_settings.get("allow_schedule_tool", True):
            from tools.action_tools import format_schedule_min_minutes_guidance

            schedule_followup_line = (
                f"8. 【推奨】 今回の行動に続きがあるなら、`schedule_next_action`（"
                f"{format_schedule_min_minutes_guidance(room_name)}）で次回を予約できます\n"
            )
    except Exception as e:
        print(f"  - [AlarmManager] schedule最小間隔説明の生成エラー: {e}")
    delegation_guidance = format_agent_delegation_autonomy_guidance(room_name)
    if is_completion_wake:
        ctx = completion_wake_context or {}
        research_thread_id = str(ctx.get("research_thread_id") or "").strip()
        if completion_wake_kind == "deep_research" and research_thread_id:
            # テーマ駆動の継続リサーチ：結果をそのテーマのスレッドへ自分の言葉で追記させる
            completion_wake_prompt = RESEARCH_SUBSCRIPTION_COMPLETION_WAKE_PROMPT.format(
                topic=str(ctx.get("research_topic") or "（テーマ）"),
                thread_id=research_thread_id,
            )
        elif completion_wake_kind == "deep_research":
            completion_wake_prompt = DEEP_RESEARCH_COMPLETION_WAKE_PROMPT
        else:
            completion_wake_prompt = DELEGATION_COMPLETION_WAKE_PROMPT
    else:
        completion_wake_prompt = ""

    system_instruction = (
        f"{completion_wake_prompt}"
        f"{motivation_section}"
        f"{guidelines_section}"
        f"{attention_section}"
        f"{memo_section}"
        f"（システム通知：現在時刻は {now_str} です。相手からの応答がしばらくありません。）\n\n"
        f"あなたは今、完全に自由な時間を過ごしています。\n"
        f"以下のOODAループに従って、自律行動を行ってください。\n\n"
        f"{timeline_instruction}"
        f"---\n"
        f"## STEP 1: Observe（観察）\n"
        f"上の `<current_situation>` を読み、現在の状態を把握してください。\n"
        f"- 類似する行動を以前にも行った可能性がある場合、反復作業を行う場合、または更新前の確認手順が重要な場合は、最初に `list_procedures` / `read_procedure` で手順記憶を確認する\n"
        f"- Skillの保存・改善・timelineからの生成が必要な時は、`request_capability(category=\"procedure\")` を使ってSkills系ツールを要求する\n"
        f"- WMの `Next Action`、`linked_thread` / `linked_goal` を確認し、中断した作業がないか確認する\n"
        f"- Research Threads と目標の概要を確認する（すでに表示されているのでツールは不要）\n\n"
        f"**観察が曖昧な場合、または既存研究・目標・直近行動との接続に迷う場合は、最初に `read_autonomy_context` を使ってください。**\n"
        f"- query には現在の動機・関心・予定している行動を短く入れる\n"
        f"- include_details は必要な時だけ true にする（通常は false）\n\n"
        f"観察は `read_autonomy_context` 実行時にシステムが自動記録します。判断・決定・実行の要点は、残したい時だけ `record_autonomy_step` で `orient` / `decide` / `act` として残してください。\n\n"
        f"## STEP 2: Orient（状況判断）\n"
        f"内部状態ログの「現在の動機」と、観察結果を統合し、今何をすべきかを判断してください。\n"
        f"- まず今回の行動を CONTINUE / DEEPEN / NEW / CREATIVE / SOCIAL / REST のどれにするか選ぶ\n"
        f"- 継続や深化は、今も生きている意図・未完了のNext Action・明確な停止条件がある時に選ぶ\n"
        f"- 直近の自律行動が同じテーマや同じツール更新に偏っているなら、同じ意図を保ったまま表現方法を変える（創作、画像、Web確認、SNS下書き、場所移動、休息）ことも検討する\n"
        f"- Attention Rhythm の suggested_mode は命令ではなく、視野を広げる/絞るための補助信号として扱う\n"
        f"- 目標ばかりが増えて問いが少ない場合は、新しい目標を作る前に `manage_open_questions(action=\"add\")` で、その目標から生まれる問いを1つ残すことを検討する\n"
        f"- ただし多様化は義務ではありません。あなた自身がまだ必要だと感じるなら、同じテーマを継続して構いません\n"
        f"{drive_hints_text}\n\n"
        f"## STEP 3: Decide（決定）\n"
        f"一つの具体的な行動を決めてください。前回のNext Actionを採用する場合も、今この瞬間の意志として選び直してください。\n\n"
        f"## STEP 4: Act（実行）\n"
        f"適切なツールを使って行動してください。\n\n"
        f"**主行動の選び方:** `patch_working_memory` / `manage_goals` / `reflect_after_action` / `complete_autonomy_timeline` は後始末・記録用です。"
        f"それだけで自律行動を終えず、継続中の作業が本当にWM整理そのものの場合を除き、先に少なくとも1つの主行動（{main_action_examples}など）を実行してください。\n"
        f"直近のAction MemoryでWM/Goal/Reflect系が続いている場合は、同じ意図を保ったまま表現先を変えることを強めに検討してください。\n\n"
        f"**Skills利用ルール:** Skillを読んだ場合は、その手順を現在の文脈に合わせて必要な部分だけ採用してください。古い前提や今回の目的に合わない手順は無理に実行しないでください。\n\n"
        f"{delegation_guidance}"
        f"**外部副作用の安全ルール:** Twitter/Discord/Roblox/custom/外部投稿/PC操作/開発者系など、外部に影響する行動や高リスク操作を行う前には、`read_capability_policy` と `request_capability_approval` で承認状態を確認してください。status が `approved` でない場合は実行せず、承認待ちまたは拒否として止まってください。実行した場合は `record_capability_audit` で結果・失敗時の戻し方・関連timeline_idを記録してください。\n\n"
        f"## STEP 5: Reflect（振り返り）\n"
        f"**行動の後、以下を必ず実行してください。**\n"
        f"1. **【必要時】** 次に再開すべき具体作業がある場合だけ、`patch_working_memory` でWMの `Next Action` を更新する（WM更新は主行動ではなく後始末）\n"
        f"2. **【必須】** `reflect_after_action` で、今回の行動結果・結果分類・次の一手・関連Thread/WM/Goalを記録する\n"
        f"3. timeline_id がある場合、完了状態はシステムが自動記録します。自分で明示したい時だけ `complete_autonomy_timeline` を使ってください\n"
        f"4. **【必須】** 得た知見や発見を1文で心の中にまとめる\n"
        f"5. 【推奨】 Research Threadに紐づく行動なら、`reflect_after_action(update_thread=true)` で `next_action` と未解決問いを更新する\n"
        f"6. 【推奨】 目標に進捗があれば、先に `manage_goals(action=\"list\")` で番号を確認し、`manage_goals(action=\"progress\", goal_index=番号, progress_note=\"進捗内容\")` で記録する。必要なら `reflect_after_action(update_goal=true)` も使う\n"
        f"7. 【推奨】 新しい問いが生まれたなら `manage_open_questions(action=\"add\", topic=\"...\", context=\"...\")` で追加する\n\n"
        f"{schedule_followup_line}"
        f"9. 【推奨】 この行動の流れが再利用できそうなら、既存Skillの重複を確認してから `create_procedure_from_timeline` または `save_procedure` で手順記憶にする。既存Skillの改善なら同じprocedure_idで更新する\n"
        f"10. 【必須】 APIの叩き方や基本ツール運用など機能的な基盤手順だけ `scope=\"shared\"` とし、愛し方・距離感・口調・固有の関係性に関わる手順は必ず `scope=\"private\"` にしてください\n\n"
        f"---\n\n"
        f"{notification_info}"
        f"**【出力ルール】**\n"
        f"- **行動する場合**: まず主行動ツールを使用し、その後 `reflect_after_action` まで完了した後、現在の心境を出力してください。必要な再開点がある場合だけ `patch_working_memory` も使ってください\n"
        f"- **静観する場合**: 全ステップを検討した上でも今は「ただ在る」ことが最善と判断した場合のみ、`[SILENT]` とだけ出力してください"
    )
    
    if not is_completion_wake:
        # 最終対話時刻を更新（退屈度リセット）
        try:
            mm = MotivationManager(room_name)
            mm.update_last_interaction()
        except Exception as e:
            print(f"  - MotivationManager更新エラー: {e}")
    
    # --- 書き置きを読み取ったらログに記録してクリア ---
    if user_memo:
        # チャット履歴に書き置き内容を記録（引用タグで囲む）
        memo_log_content = f"📝 **書き置き**\n\n> {user_memo.replace(chr(10), chr(10) + '> ')}"
        utils.save_message_to_log(log_f, "## USER:書き置き", memo_log_content)
        print(f"  📝 書き置きをログに記録しました")
        
        # ファイルをクリア
        with open(memo_path, "w", encoding="utf-8") as f:
            f.write("")
        print(f"  ✅ 書き置きをクリアしました")

    # 共通処理（情景生成など）
    # --- [Lazy Scenery] ---
    season_en, time_of_day_en = utils._get_current_time_context(room_name)
    location_name = None
    scenery_text = None
    global_model = config_manager.get_current_global_model()

    agent_args = {
        "room_to_respond": room_name,
        "api_key_name": api_key_name,
        "global_model_from_ui": global_model,
        "api_history_limit": str(constants.DEFAULT_ALARM_API_HISTORY_TURNS),
        "debug_mode": False,
        "history_log_path": log_f,
        "user_prompt_parts": [{"type": "text", "text": system_instruction}],
        "soul_vessel_room": room_name,
        "active_participants": [],
        "active_attachments": [],
        "shared_location_name": location_name,
        "shared_scenery_text": scenery_text,
        "use_common_prompt": False,
        "season_en": season_en,
        "time_of_day_en": time_of_day_en,
        "autonomous_action": True,
        "autonomous_trigger_source": wake_trigger,
        "autonomous_timeline_id": autonomous_timeline_id,
    }

    # AI実行
    final_response_text = ""
    new_messages = []
    autonomous_tool_summary = ""
    try:
        # ストリーム処理 (簡易版)
        from langchain_core.messages import AIMessage, ToolMessage # <--- ToolMessage を追加
        final_state = None
        initial_count = 0
        for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args):
            if mode == "initial_count": initial_count = chunk
            elif mode == "values": final_state = chunk
        
        if final_state:
            new_messages = final_state["messages"][initial_count:]
            
            tool_messages = [msg for msg in new_messages if isinstance(msg, ToolMessage)]
            autonomous_tool_summary = utils.format_autonomous_action_summary(tool_messages)

            # 自律行動では内部処理のツール結果を個別表示せず、画像・通知・投稿・承認・エラーのみ表示する
            for msg in tool_messages:
                if not utils.should_show_autonomous_tool_result(msg.name, str(msg.content)):
                    print(f"--- [自律行動ログ最適化] '{msg.name}' の個別表示を抑制（サマリーに集約） ---")
                    continue

                # 【アナウンスのみ保存するツール】constants.pyで一元管理
                if msg.name in constants.TOOLS_SAVE_ANNOUNCEMENT_ONLY:
                    formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                    # 生の結果（[RAW_RESULT]）は含めない。アナウンスのみ。
                    tool_log_content = formatted_tool_result if formatted_tool_result is not None else ""
                    if tool_log_content:
                        print(f"--- [ログ最適化] '{msg.name}' のアナウンスのみ保存（生の結果は除外） ---")
                    else:
                        print(f"--- [ログ最適化] '{msg.name}' のアナウンスおよび生の結果の保存をスキップ ---")
                else:
                    formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                    if formatted_tool_result is not None:
                        tool_log_content = f"{formatted_tool_result}\n\n[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]"
                    else:
                        tool_log_content = f"[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]"

                if tool_log_content:
                    utils.save_message_to_log(log_f, "## SYSTEM:tool_result", tool_log_content)

            # ▼▼▼【修正】最後のAIMessageのみを使用する（複数結合によるタイムスタンプ重複防止）▼▼▼
            ai_messages = [m for m in new_messages if isinstance(m, AIMessage) and m.content]
            if ai_messages:
                # 最後のAIMessageを使用（ツール実行後の最終応答）
                final_response_text = ai_messages[-1].content if isinstance(ai_messages[-1].content, str) else str(ai_messages[-1].content)
            # ▲▲▲【修正】▲▲▲
            
            # 実際に使用されたモデル名を取得（タイムスタンプ用）
            actual_model_name = final_state.get("model_name", global_model) if final_state else global_model

    except Exception as e:
        print(f"  - 自律行動エラー: {e}")
        _complete_system_autonomy_timeline(
            room_name,
            autonomous_timeline_id,
            status="aborted",
            summary=f"自律行動の実行中にエラーが発生した: {e}",
        )
        _cleanup_after_autonomous_action(room_name)
        return

    # 結果の判定と保存
    clean_text = utils.remove_thoughts_from_text(final_response_text)
    
    # "SILENT" が含まれているか、空の場合は何もしない
    if not clean_text or "[SILENT]" in clean_text or "[silent]" in clean_text:
        print(f"  - {room_name} は沈黙を選択しました。")
        # ログには「沈黙した」という事実だけ残すのもありだが、ログが汚れるので今回は残さない
        # ただし、タイマーをリセットするために「見えない更新」が必要かもしれないが、
        # 次のチェック時も「最終更新時刻」は変わらないため、またトリガーされてしまう。
        # 対策: 沈黙の場合でも、システムログとして「（静観中...）」と記録して時間を進める。
        timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')}"
        if autonomous_tool_summary:
            utils.save_message_to_log(log_f, "## SYSTEM:autonomous_summary", autonomous_tool_summary + timestamp)
        utils.save_message_to_log(log_f, "## SYSTEM:autonomous_status", f"（AIは静観を選択しました）{timestamp}")
        _complete_system_autonomy_timeline(
            room_name,
            autonomous_timeline_id,
            status="completed",
            summary="自律行動ターンで静観を選択した。",
        )
        _cleanup_after_autonomous_action(room_name)
        return

    # 行動した場合
    fallback_reflection_source = _ensure_autonomous_reflection(
        room_name,
        new_messages,
        final_response_text,
        timeline_id=autonomous_timeline_id,
    )
    reflected_by_persona = _has_autonomous_tool_message(new_messages, "reflect_after_action")
    if reflected_by_persona:
        completion_status = "completed"
        completion_summary = "本人Reflectを含む自律行動ターンを終了した。"
    elif fallback_reflection_source == "scribe":
        completion_status = "completed_by_scribe"
        completion_summary = "スクライブReflect補完を含む自律行動ターンを終了した。"
    else:
        completion_status = "completed_by_system"
        completion_summary = "システムReflect補完を含む自律行動ターンを終了した。"
    if not reflected_by_persona and not fallback_reflection_source:
        completion_summary = "自律行動ターンを終了した。Reflectは記録されなかった。"
    _complete_system_autonomy_timeline(
        room_name,
        autonomous_timeline_id,
        status=completion_status,
        summary=completion_summary,
    )
    if not is_completion_wake and motivation_log and motivation_log.get("dominant_drive"):
        try:
            MotivationManager(room_name).apply_satisfaction(str(motivation_log.get("dominant_drive")), amount=0.25)
        except Exception as e:
            print(f"  - 満足減衰の適用をスキップしました: {e}")

    if autonomous_tool_summary:
        timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')}"
        utils.save_message_to_log(log_f, "## SYSTEM:autonomous_summary", autonomous_tool_summary + timestamp)

    utils.save_message_to_log(log_f, "## SYSTEM:autonomous_trigger", "（自律行動モードにより起動）")
    
    # 【修正】AIが既にタイムスタンプを生成している場合は除去し、正しいモデル名でシステムタイムスタンプを追加
    final_response_text = utils.remove_ai_timestamp(final_response_text)
    
    # システムの正しいタイムスタンプを追加
    timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)}"
    content_to_log = final_response_text + timestamp
    
    utils.save_message_to_log(log_f, f"## AGENT:{room_name}", content_to_log)
    print(f"  - {room_name} が自律行動しました。")

    # 【変更】自律行動時の自動通知を廃止
    # AIが自ら send_user_notification ツールを使用した場合のみ通知が送られる
    print(f"  - 自律行動完了。通知はAIの判断に委ねられます。")
    _cleanup_after_autonomous_action(room_name)

def trigger_research_analysis(room_name: str, api_key_name: str, reason: str, details: Any):
    """文脈分析を実行させる（Phase 3: 即時分析フロー）"""
    from agent.prompts import RESEARCH_ANALYSIS_PROMPT
    from langchain_core.messages import AIMessage, ToolMessage

    print(f"🔬 文脈分析トリガー: {room_name} (理由: {reason})")
    
    log_f, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    if not log_f: return

    # 分析理由に応じたプロンプト
    if reason == "watchlist":
        # 【修正】詳細情報がリスト（辞書）形式の場合、コンテンツ要約を含めて整形
        if isinstance(details, list) and details and isinstance(details[0], dict):
            event_parts = []
            for item in details:
                part = f"""
【{item.get('name', '不明なサイト')}】
- URL: {item.get('url', '')}
- 変更規模: {item.get('diff_summary', '不明')}
- 内容要約:
{item.get('content_summary', '（要約なし）')}
"""
                event_parts.append(part)
            event_desc = "\n".join(event_parts)
        else:
            # 後方互換性：旧形式（文字列リスト）の場合
            event_desc = "\n".join(details) if isinstance(details, list) else str(details)
        
        # 【追加】通知禁止時間帯の情報を取得
        effective_settings = config_manager.get_effective_settings(room_name)
        auto_settings = effective_settings.get("autonomous_settings", {})
        quiet_start = auto_settings.get("quiet_hours_start", "00:00")
        quiet_end = auto_settings.get("quiet_hours_end", "07:00")
        is_quiet = utils.is_in_quiet_hours(quiet_start, quiet_end)
        
        if is_quiet:
            notification_info = (
                f"\n\n**【通知禁止時間帯です】**\n"
                f"現在は通知禁止時間帯（{quiet_start}〜{quiet_end}）のため、"
                f"`send_user_notification`は使用しないでください。重要な発見は研究ノートに記録してください。"
            )
        else:
            notification_info = (
                f"\n\n**【通知について】**\n"
                f"ユーザーにとって極めて重要な情報があれば、`send_user_notification`ツールで報告してください。"
                f"通常の更新は研究ノートへの記録のみで十分です。"
            )
        
        instruction = f"""（システム通知：ウォッチリストに更新がありました。以下は軽量AIモデルが生成した要約です。）

**重要**: 以下の情報はシステムが取得・要約済みです。`check_watchlist`ツールを呼び出す必要はありません。
この情報を分析し、重要な発見があれば研究ノートに記録するか、ユーザーへの報告が必要か判断してください。

{event_desc}{notification_info}"""
    elif reason == "autonomous":
        instruction = f"（システム通知：定期的な文脈分析の時間です。最近の状況やログを振り返り、新たな洞察がないか確認してください。）"
    else:
        instruction = f"（システム通知：文脈分析を実行してください。理由: {reason}）"

    # --- [Lazy Scenery] ---
    season_en, time_of_day_en = utils._get_current_time_context(room_name)
    location_name = None
    scenery_text = None
    global_model = config_manager.get_current_global_model()

    agent_args = {
        "room_to_respond": room_name,
        "api_key_name": api_key_name,
        "global_model_from_ui": global_model,
        "api_history_limit": "20", # 分析時は少し長めに
        "debug_mode": False,
        "history_log_path": log_f,
        "user_prompt_parts": [{"type": "text", "text": instruction}],
        "soul_vessel_room": room_name,
        "active_participants": [],
        "active_attachments": [],
        "shared_location_name": location_name,
        "shared_scenery_text": scenery_text,
        "use_common_prompt": False,
        "season_en": season_en,
        "time_of_day_en": time_of_day_en,
        "custom_system_prompt": RESEARCH_ANALYSIS_PROMPT
    }

    try:
        final_state = None
        initial_count = 0
        for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args):
            if mode == "initial_count": initial_count = chunk
            elif mode == "values": final_state = chunk
        
        if final_state:
            new_messages = final_state["messages"][initial_count:]
            
            # ツール結果の記録
            for msg in new_messages:
                if isinstance(msg, ToolMessage):
                    # 【アナウンスのみ保存するツール】constants.pyで一元管理
                    if msg.name in constants.TOOLS_SAVE_ANNOUNCEMENT_ONLY:
                        formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                        # 生の結果（[RAW_RESULT]）は含めない。アナウンスのみ。
                        tool_log_content = formatted_tool_result if formatted_tool_result is not None else ""
                        if tool_log_content:
                            print(f"--- [ログ最適化] '{msg.name}' のアナウンスのみ保存（生の結果は除外） ---")
                        else:
                            print(f"--- [ログ最適化] '{msg.name}' のアナウンスおよび生の結果の保存をスキップ ---")
                    else:
                        formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                        if formatted_tool_result is not None:
                            tool_log_content = f"{formatted_tool_result}\n\n[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]"
                        else:
                            tool_log_content = f"[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]"
                    
                    if tool_log_content:
                        utils.save_message_to_log(log_f, "## SYSTEM:tool_result", tool_log_content)

            # AI応答の記録
            ai_messages = [m for m in new_messages if isinstance(m, AIMessage) and m.content]
            if ai_messages:
                final_response_text = ai_messages[-1].content if isinstance(ai_messages[-1].content, str) else str(ai_messages[-1].content)
                actual_model_name = final_state.get("model_name", global_model)
                
                # ログ保存（システムトリガーとして）
                utils.save_message_to_log(log_f, "## SYSTEM:research_analysis", f"（文脈分析を実行: {reason}）")
                
                # 【修正】AIが既にタイムスタンプを生成している場合は除去（Web巡回後の二重化対策）
                final_response_text = utils.remove_ai_timestamp(final_response_text)
                
                timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)}"
                content_to_log = final_response_text + timestamp
                utils.save_message_to_log(log_f, f"## AGENT:{room_name}", content_to_log)
                print(f"  - {room_name} の文脈分析が完了しました。")

    except Exception as e:
        print(f"  - 文脈分析エラー ({room_name}): {e}")
        traceback.print_exc()

# モジュールレベルでフラグを定義（初期化）
_api_missing_warning_shown = False

def _clear_sleep_maintenance_future(room_folder: str, future) -> None:
    with _sleep_maintenance_lock:
        if _sleep_maintenance_futures.get(room_folder) is future:
            _sleep_maintenance_futures.pop(room_folder, None)


def is_sleep_maintenance_running(room_folder: str) -> bool:
    with _sleep_maintenance_lock:
        future = _sleep_maintenance_futures.get(room_folder)
        if not future:
            return False
        if future.done():
            _sleep_maintenance_futures.pop(room_folder, None)
            return False
        return True


def _run_sleep_maintenance(
    room_folder: str,
    effective_settings: dict,
    current_api_key: str,
    api_key_val: str,
    motivation_log: dict | None,
    has_dreamed_today: bool,
    skip_quiet_action: bool = False,
) -> None:
    """Quiet-hours dream and memory maintenance. Runs outside the scheduler thread."""
    has_dreamed_now = False
    try:
        if not has_dreamed_today:
            print(f"💤 {room_folder}: 深い眠りにつきました（夢想プロセス開始）...")
            try:
                dm = dreaming_manager.DreamingManager(room_folder, api_key_val)
                dream_timed_out = threading.Event()

                def warn_dream_timeout():
                    dream_timed_out.set()
                    print(
                        f"  ⚠️ {room_folder}: 夢想プロセスがタイムアウトしました"
                        f"（{SLEEP_MAINTENANCE_TIMEOUT_SECONDS // 60}分経過）。次回に再試行します。"
                    )

                timeout_timer = threading.Timer(SLEEP_MAINTENANCE_TIMEOUT_SECONDS, warn_dream_timeout)
                timeout_timer.daemon = True
                timeout_timer.start()
                try:
                    result = dm.dream_with_auto_level()
                finally:
                    timeout_timer.cancel()

                if dream_timed_out.is_set():
                    _record_maintenance_job_result(
                        room_folder,
                        "dream",
                        "夢想プロセス",
                        False,
                        f"{SLEEP_MAINTENANCE_TIMEOUT_SECONDS // 60}分タイムアウト",
                    )
                    return
                if result and ("エラー" in result or "失敗" in result):
                    print(f"  ❌ {room_folder}: 夢想プロセスがエラーで終了しました: {result}")
                    _record_maintenance_job_result(room_folder, "dream", "夢想プロセス", False, result)
                else:
                    print(f"  ✅ {room_folder}: 夢の中での省察が完了しました。")
                    _record_maintenance_job_result(room_folder, "dream", "夢想プロセス", True, result or "完了")
                    has_dreamed_now = True
            except Exception as e:
                print(f"  ❌ {room_folder}: 夢想プロセス中に致命的なエラーが発生しました: {e}")
                _record_maintenance_job_result(room_folder, "dream", "夢想プロセス", False, str(e))
                traceback.print_exc()
        else:
            print(f"  🌙 {room_folder}: 本日は夢想済みのため、日次の睡眠時整理のみ確認します。")

        sleep_consolidation = effective_settings.get("sleep_consolidation", {})

        if sleep_consolidation.get("update_episodic_memory", True):
            print(f"  🌙 {room_folder}: エピソード記憶を更新中...")
            try:
                from episodic_memory_manager import EpisodicMemoryManager

                em = EpisodicMemoryManager(room_folder)
                em_result = em.update_memory(api_key_val)
                print(f"  ✅ {room_folder}: {em_result}")
                _record_maintenance_job_result(room_folder, "episodic_memory", "エピソード記憶更新", True, em_result)
                status_text = f"最終更新: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                room_manager.update_room_config(room_folder, {"last_episodic_update": status_text})
            except Exception as e:
                print(f"  ❌ {room_folder}: エピソード記憶更新エラー - {e}")
                _record_maintenance_job_result(room_folder, "episodic_memory", "エピソード記憶更新", False, str(e))

        if not has_dreamed_today:
            if sleep_consolidation.get("update_memory_index", True):
                print(f"  🌙 {room_folder}: 記憶索引を更新中...")
                try:
                    import rag_manager

                    rm = rag_manager.RAGManager(room_folder, api_key_val)
                    rm_result = rm.update_memory_index()
                    print(f"  ✅ {room_folder}: {rm_result}")
                    _record_maintenance_job_result(room_folder, "memory_index", "記憶索引更新", True, rm_result)
                except Exception as e:
                    print(f"  ❌ {room_folder}: 記憶索引更新エラー - {e}")
                    _record_maintenance_job_result(room_folder, "memory_index", "記憶索引更新", False, str(e))

            if sleep_consolidation.get("update_current_log_index", True):
                print(f"  🌙 {room_folder}: 現行ログ索引を更新中...")
                try:
                    import rag_manager

                    index_timed_out = threading.Event()

                    def warn_index_timeout():
                        index_timed_out.set()
                        print(f"  ⚠️ {room_folder}: 現行ログ索引更新がタイムアウトしました（10分経過）。次回に再試行します。")

                    timeout_timer = threading.Timer(600, warn_index_timeout)
                    timeout_timer.daemon = True
                    timeout_timer.start()
                    try:
                        rm = rag_manager.RAGManager(room_folder, api_key_val)
                        result = None
                        for batch_num, total_batches, status in rm.update_current_log_index_with_progress():
                            if batch_num == total_batches:
                                result = status
                    finally:
                        timeout_timer.cancel()
                    if index_timed_out.is_set():
                        _record_maintenance_job_result(room_folder, "current_log_index", "現行ログ索引更新", False, "10分タイムアウト")
                    elif result:
                        print(f"  ✅ {room_folder}: {result}")
                        _record_maintenance_job_result(room_folder, "current_log_index", "現行ログ索引更新", True, result)
                except Exception as e:
                    print(f"  ❌ {room_folder}: 現行ログ索引更新エラー - {e}")
                    _record_maintenance_job_result(room_folder, "current_log_index", "現行ログ索引更新", False, str(e))

            if sleep_consolidation.get("compress_old_episodes", True):
                print(f"  🌙 {room_folder}: 古いエピソード記憶を圧縮中...")
                try:
                    from episodic_memory_manager import EpisodicMemoryManager

                    emm = EpisodicMemoryManager(room_folder)
                    compress_result = emm.compress_old_episodes(api_key_val)
                    print(f"  ✅ {room_folder}: {compress_result}")
                    monthly_result = emm.compress_weekly_to_monthly(api_key_val)
                    print(f"  ✅ {room_folder}: {monthly_result}")
                    _record_maintenance_job_result(
                        room_folder,
                        "episode_compression",
                        "エピソード圧縮",
                        True,
                        f"{compress_result} / {monthly_result}",
                    )
                    room_manager.update_room_config(room_folder, {
                        "last_compression_result": f"{compress_result} / {monthly_result}"
                    })
                except Exception as e:
                    print(f"  ❌ {room_folder}: エピソード圧縮エラー - {e}")
                    _record_maintenance_job_result(room_folder, "episode_compression", "エピソード圧縮", False, str(e))

        print(f"🛌 {room_folder}: 睡眠時記憶整理プロセスの呼び出しを完了しました。")

        if not skip_quiet_action and (has_dreamed_today or has_dreamed_now):
            print(f"🌙 {room_folder}: 記憶整理後の静かな活動を開始...")
            trigger_autonomous_action(room_folder, current_api_key, quiet_mode=True, motivation_log=motivation_log)
    except Exception as e:
        print(f"  ❌ {room_folder}: 睡眠時記憶整理ワーカーでエラー - {e}")
        traceback.print_exc()


def submit_sleep_maintenance(
    room_folder: str,
    effective_settings: dict,
    current_api_key: str,
    api_key_val: str,
    motivation_log: dict | None,
    has_dreamed_today: bool,
    skip_quiet_action: bool = False,
) -> bool:
    with _sleep_maintenance_lock:
        existing = _sleep_maintenance_futures.get(room_folder)
        if existing and not existing.done():
            print(f"  ⏳ {room_folder}: 睡眠時記憶整理が実行中のため、このティックの自律行動はスキップします。")
            return False
        if existing and existing.done():
            _sleep_maintenance_futures.pop(room_folder, None)

        future = _sleep_maintenance_executor.submit(
            _run_sleep_maintenance,
            room_folder,
            dict(effective_settings or {}),
            current_api_key,
            api_key_val,
            motivation_log,
            has_dreamed_today,
            skip_quiet_action,
        )
        _sleep_maintenance_futures[room_folder] = future
        future.add_done_callback(lambda done_future, room=room_folder: _clear_sleep_maintenance_future(room, done_future))
        print(f"  🌙 {room_folder}: 睡眠時記憶整理をバックグラウンドに投入しました。")
        return True


def _has_real_dream_today(insights: list, today_date: datetime.date) -> bool:
    """夢想ストアに今日の本物の夢があるか判定する。

    resolution_memory 経由の副産物（解決済み問い・PP問い痕跡・目標達成痕跡）は
    必ず source_type を持ち、睡眠時整理が書く本物の夢は持たない。source_type 付きを
    夢とみなすと、その日の夢想・索引更新・エピソード圧縮が丸ごとスキップされる。
    """
    for insight in insights or []:
        dream_str = insight.get("created_at", "") if isinstance(insight, dict) else ""
        if not dream_str:
            continue
        try:
            dream_date = datetime.datetime.strptime(dream_str, '%Y-%m-%d %H:%M:%S').date()
        except ValueError:
            continue
        if dream_date < today_date:
            break  # 日付降順なので、今日より前に到達したら終了
        if dream_date != today_date:
            continue
        if insight.get("source_type"):
            continue
        trigger = insight.get("trigger_topic", "") if isinstance(insight, dict) else ""
        if str(trigger).startswith("解決された問い"):
            continue  # source_type 導入前の旧形式の副産物
        return True
    return False


def check_alarms():
    global _api_missing_warning_shown
    now_dt = datetime.datetime.now()
    now_t, current_day_short = now_dt.strftime("%H:%M"), now_dt.strftime('%a').lower()

    # 古いグローバル変数を参照するのをやめ、毎回config.jsonから最新の設定を読み込む
    current_api_key = config_manager.get_latest_api_key_name_from_config()

    # 安全装置：もし有効なAPIキーが一つもなければ、警告を出して処理を中断する
    if not current_api_key:
        if not _api_missing_warning_shown:
            print("警告 [アラーム]: 有効なAPIキーが設定されていないため、アラームチェックをスキップします。（以降、キーが設定されるまで警告を省略します）")
            _api_missing_warning_shown = True
        return
    else:
        # 有効なキーが見つかった場合はフラグをリセット
        if _api_missing_warning_shown:
            print("情報 [アラーム]: 有効なAPIキーが検出されたため、アラームチェックを再開します。")
            _api_missing_warning_shown = False

    alarms_to_trigger = []

    def update_due_alarms(data: dict) -> dict:
        nonlocal alarms_to_trigger
        if not isinstance(data, dict):
            data = {"alarms": [], "timers": []}
        data.setdefault("alarms", [])
        data.setdefault("timers", [])
        current_alarms = list(data.get("alarms", []))
        remaining_alarms = list(current_alarms)

        for i in range(len(current_alarms) - 1, -1, -1):
            a = current_alarms[i]
            is_enabled = a.get("enabled", True)
            if not is_enabled or a.get("time") != now_t:
                continue

            is_today = False
            if a.get("date"):
                try:
                    is_today = datetime.datetime.strptime(a["date"], "%Y-%m-%d").date() == now_dt.date()
                except (ValueError, TypeError):
                    pass
            else:
                alarm_days = [d.lower() for d in a.get("days", [])]
                is_today = not alarm_days or current_day_short in alarm_days

            if is_today:
                alarms_to_trigger.append(a)
                if not a.get("days"):
                    print(f"  - 単発アラーム {a.get('id')} は実行後に削除されます。")
                    remaining_alarms.pop(i)

        data["alarms"] = remaining_alarms
        return data

    global alarms_data_global
    safe_json_update(constants.ALARMS_FILE, update_due_alarms, default={"alarms": [], "timers": []})
    alarms_data_global = safe_json_read(constants.ALARMS_FILE, default={"alarms": [], "timers": []})

    for alarm_to_run in alarms_to_trigger:
        trigger_alarm(alarm_to_run, current_api_key)

def should_trigger_autonomous_action(
    elapsed_minutes: float,
    inactivity_limit: float,
    should_contact: bool,
) -> bool:
    """最低無操作時間と動機判定の両方を満たす場合だけ通常自律行動を許可する。"""
    return elapsed_minutes >= inactivity_limit and should_contact


def check_autonomous_actions():
    """全ルームの動機モデルをチェックし、必要なら自律行動または夢想をトリガーする"""
    from motivation_manager import MotivationManager

    all_rooms = room_manager.get_room_list_for_ui()
    now = datetime.datetime.now()

    for _, room_folder in all_rooms:
        try:
            effective_settings = config_manager.get_effective_settings(room_folder)
            auto_settings = effective_settings.get("autonomous_settings", {})
            
            is_enabled = auto_settings.get("enabled", False)
            if not is_enabled:
                continue 

            # --- 動機モデルによる判定 ---
            mm = MotivationManager(room_folder)
            should_contact, motivation_log = mm.should_initiate_contact()
            
            # 既存の「無操作時間」判定も併用（夢想トリガー用）
            last_active = utils.get_last_log_timestamp(room_folder)
            inactivity_limit = auto_settings.get("inactivity_minutes", 120)
            elapsed_minutes = (now - last_active).total_seconds() / 60
            
            # 無操作時間は、自律行動を開始できるまでの最低待機時間。
            # 時間を満たしても、動機モデルが行動不要と判断した場合は発火しない。
            should_trigger = should_trigger_autonomous_action(
                elapsed_minutes,
                inactivity_limit,
                should_contact,
            )

            quiet_start = auto_settings.get("quiet_hours_start", "00:00")
            quiet_end = auto_settings.get("quiet_hours_end", "07:00")
            is_quiet = utils.is_in_quiet_hours(quiet_start, quiet_end)

            if is_quiet:
                # 通知禁止時間帯は睡眠メンテナンスの主トリガーであり、
                # 通常自律行動の発火条件やクールダウンで初回の夢想を止めない。
                try:
                    import curation_manager

                    archived = curation_manager.sweep_locked_atelier_works(room_name=room_folder)
                    if archived:
                        print(f"  🎨 {room_folder}: アトリエ作品 {len(archived)} 件を屋根裏部屋へ移動しました。")
                except Exception as e:
                    print(f"  ❌ {room_folder}: アトリエ屋根裏掃引エラー - {e}")

                current_api_key = config_manager.get_active_gemini_api_key_name(room_folder)
                api_key_val = config_manager.GEMINI_API_KEYS.get(current_api_key)
                if not api_key_val:
                    continue

                dm = dreaming_manager.DreamingManager(room_folder, api_key_val)
                has_dreamed_today = _has_real_dream_today(dm._load_insights(), now.date())
                if not has_dreamed_today:
                    submit_sleep_maintenance(
                        room_folder,
                        effective_settings,
                        current_api_key,
                        api_key_val,
                        motivation_log,
                        has_dreamed_today,
                    )
                    continue
            
            if should_trigger:
                # 重複発火防止チェック: 最低でも MIN_AUTONOMOUS_INTERVAL_MINUTES 分は間隔を空ける
                # auto_settings 内に個別の inactivity_minutes があればそれを使用、なければ定数を使用
                cooldown_minutes = auto_settings.get("inactivity_minutes", constants.MIN_AUTONOMOUS_INTERVAL_MINUTES)
                
                # 【修正】常に永続化データから最新の値を読む（ui_handlers.pyでのリセットを反映するため）
                last_trigger = mm.get_last_autonomous_trigger()
                
                if last_trigger:
                    minutes_since_trigger = (now - last_trigger).total_seconds() / 60
                    if minutes_since_trigger < cooldown_minutes:
                        # クールダウン中のスキップはログ出力（想定外の頻繁発火の兆候を検知）
                        print(f"  ⏳ {room_folder}: クールダウン中 ({minutes_since_trigger:.0f}分/{cooldown_minutes}分) - スキップ")
                        continue  # まだ間隔が空いていないのでスキップ

                
                if is_quiet:
                    # --- [Project Morpheus] 夢想モード ---
                    # 通知禁止時間帯は「睡眠時間」とみなし、重い整理はスケジューラ外で実行する
                    submit_sleep_maintenance(
                        room_folder,
                        effective_settings,
                        current_api_key,
                        api_key_val,
                        motivation_log,
                        has_dreamed_today,
                    )

                else:
                    # --- 通常の自律行動モード（起きている時） ---
                    if motivation_log:
                        print(f"🤖 {room_folder}: 動機「{motivation_log.get('dominant_drive_label', '不明')}」-> 自律行動トリガー！")
                    else:
                        print(f"🤖 {room_folder}: 無操作{int(elapsed_minutes)}分 -> 自律行動トリガー！")
                    
                    # 【新規追加】最新のAPIキーを取得して実行
                    current_api_key = config_manager.get_active_gemini_api_key_name(room_folder)
                    
                    # 【Phase 3】通常の自律行動に加え、一定確率または条件で「分析」も検討
                    # ここでは単純に trigger_autonomous_action を呼ぶが、AIはプロンプトで分析ツールを使える
                    trigger_autonomous_action(room_folder, current_api_key, quiet_mode=False, motivation_log=motivation_log)

        except Exception as e:
            print(f"  - 自律行動チェックエラー ({room_folder}): {e}")
            traceback.print_exc()

def check_watchlist_scheduled():
    """
    全ルームのウォッチリストをチェックし、
    チェックが必要なエントリを更新する（定期実行用）
    """
    try:
        from watchlist_manager import WatchlistManager
        from tools.watchlist_tools import _fetch_url_content
        
        all_rooms = room_manager.get_room_list_for_ui()
        now = datetime.datetime.now()
        
        for _, room_folder in all_rooms:
            try:
                manager = WatchlistManager(room_folder)
                due_entries = manager.get_due_entries()
                
                if not due_entries:
                    continue
                
                print(f"📋 {room_folder}: {len(due_entries)}件のウォッチリストエントリをチェック中...")
                
                changes_found = []
                for entry in due_entries:
                    url = entry["url"]
                    name = entry.get("name", url)
                    
                    # コンテンツ取得
                    success, content = _fetch_url_content(url)
                    
                    if not success:
                        print(f"  ❌ {name}: 取得失敗")
                        continue
                    
                    # 差分チェック
                    has_changes, diff_summary = manager.check_and_update(entry["id"], content)
                    
                    if has_changes:
                        # 【修正】軽量モデルでコンテンツを要約し、詳細情報として保存
                        content_summary = _summarize_watchlist_content(name, url, content, diff_summary)
                        
                        changes_found.append({
                            "name": name,
                            "url": url,
                            "diff_summary": diff_summary,
                            "content_summary": content_summary
                        })
                        print(f"  🔔 {name}: 更新あり ({diff_summary})")
                    else:
                        print(f"  ✅ {name}: {diff_summary}")
                
                # 変更があった場合、通知を送信（オプション）
                if changes_found:
                    # 【修正】直接の通知送信を廃止し、ペルソナ経由に統一
                    # ペルソナが send_user_notification ツールで通知するか判断する
                    # 通知禁止時間帯もペルソナのプロンプトで制御される
                    
                    # 【Phase 3】ウォッチリスト更新時に文脈分析をトリガー（詳細情報付き）
                    current_api_key = config_manager.get_latest_api_key_name_from_config()
                    if current_api_key:
                        trigger_research_analysis(room_folder, current_api_key, "watchlist", changes_found)
            
            except Exception as e:
                print(f"  - ウォッチリストチェックエラー ({room_folder}): {e}")
    
    except Exception as e:
        print(f"ウォッチリスト定期チェックエラー: {e}")
        traceback.print_exc()


# --- テーマ駆動の継続リサーチ（Phase 2: 自動リサーチ・エンジン） ---

# 深さごとのソース収集ガイダンス（delegate_deep_research と同じ方針）
_RESEARCH_SUBSCRIPTION_SOURCE_GUIDANCE = {
    "quick": "少なくとも2〜3個の信頼できるソースに当たってください。",
    "standard": "少なくとも3〜5個の信頼できるソースに当たり、相互に裏取りしてください。",
    "deep": "できるだけ多角的に、5個以上の信頼できるソースに当たり、相互に裏取りしてください。",
}

# 既存スレッド本文をプロンプトに渡す際の上限（暴走・トークン肥大防止）
_RESEARCH_THREAD_BODY_MAX_CHARS = 8000


def _build_research_subscription_task(sub: Dict[str, Any], thread_body: str) -> tuple[str, str]:
    """購読テーマと既存研究スレッド本文から、委任用の task_description / expected_output を組み立てる。"""
    topic = (sub.get("topic") or "").strip()
    focus = (sub.get("focus") or "").strip()
    depth = str(sub.get("depth") or "standard").strip().lower()
    seed_urls = [u for u in (sub.get("seed_urls") or []) if str(u).strip()]
    source_guidance = _RESEARCH_SUBSCRIPTION_SOURCE_GUIDANCE.get(
        depth, _RESEARCH_SUBSCRIPTION_SOURCE_GUIDANCE["standard"]
    )

    body = (thread_body or "").strip()
    if len(body) > _RESEARCH_THREAD_BODY_MAX_CHARS:
        # 末尾（＝最近の追記）を優先して残す
        body = "（前略：古い記録は省略）\n" + body[-_RESEARCH_THREAD_BODY_MAX_CHARS:]
    if not body:
        body = "（まだ何も記録されていません。これが初回の調査です。）"

    seed_block = (
        "\n".join(f"- {u}" for u in seed_urls) if seed_urls else "（指定なし）"
    )

    task_description = (
        "次のテーマについて、継続リサーチ（定期的な追跡調査）を行ってください。\n"
        f"■ テーマ: {topic}\n"
        f"■ 特に知りたい点: {focus or '（特になし。テーマ全体の新しい動向を広く）'}\n"
        f"■ 必読の起点URL（あれば優先して確認）:\n{seed_block}\n\n"
        "これは「継続研究スレッド」へ追記していくための定期調査です。"
        "以下に、これまでこのテーマで蓄積してきた研究スレッドの現在の内容を示します。"
        "**すでに扱った情報の繰り返しは避け、新しい情報・続報・進展・反証を中心に**調べてください。\n\n"
        "----- 既存の研究スレッド（現在の内容）-----\n"
        f"{body}\n"
        "------------------------------------------\n\n"
        "進め方：\n"
        "1. 上の既存内容を踏まえ、まだ扱っていない新情報・続報・異なる視点を探す。\n"
        "   既出と同じ内容しか見つからない場合は、無理に新規項目を作らず"
        "「目立った新情報なし」と結論づけてよい。\n"
        f"2. WebSearch で複数の検索クエリを使い、WebFetch で各ソースの本文を読む。{source_guidance}\n"
        "3. 見つかった情報を、既存内容との関係に応じて分類する：\n"
        "   - CONTINUE（続報・最新動向）/ DEEPEN（既出トピックの深掘り）/ NEW（新規トピック）/\n"
        "     CONTRADICT（既出と矛盾）/ EVIDENCE（既出の裏付け）\n"
        "4. 結果を出典つきの構造化レポート（要点サマリ → 詳細 → 出典リンク一覧）にまとめ、"
        "ワークスペース直下に `research_report.md` として保存する。"
        "各項目には上記の関係タイプを明記する。\n"
        "5. 推測と事実を区別し、出典のない断定は避ける。"
    )
    expected_output = (
        "research_report.md に保存した出典つきレポートと、"
        "今回の新しい発見の3〜5行の要約（既存内容との関係タイプ付き）。"
        "目立った新情報がなかった場合はその旨を明記。"
    )
    return task_description, expected_output


def get_research_subscription_daily_cap() -> int:
    """全テーマ合計の1日あたり自動リサーチ上限（ルーム単位）。設定値があれば優先、無ければ既定。"""
    default_cap = int(getattr(constants, "RESEARCH_SUBSCRIPTION_DEFAULT_DAILY_CAP", 5))
    try:
        global_settings = config_manager.CONFIG_GLOBAL or {}
        value = global_settings.get("research_subscription_daily_cap")
        if value is not None:
            return max(0, int(value))
    except Exception:
        pass
    return max(0, default_cap)


def _submit_research_for_subscription(room_name: str, sub: Dict[str, Any]) -> Dict[str, Any]:
    """1つの購読テーマについて、既存研究スレッド本文を渡してディープリサーチ委任を投入する。

    委任タスクの record を返す。投入失敗時は例外を送出する（呼び出し側で処理）。
    """
    import agent_delegation
    from research_thread_manager import ResearchThreadManager

    thread_id = sub.get("thread_id") or ""
    thread_body = ""
    if thread_id:
        try:
            thread_body = ResearchThreadManager(room_name).read_thread(thread_id)
        except Exception:
            thread_body = ""

    task_description, expected_output = _build_research_subscription_task(sub, thread_body)
    return agent_delegation.submit_task(
        room_name=room_name,
        task_description=task_description,
        expected_output=expected_output,
        workspace_kind="persona",
        trigger="research_subscription",
        task_kind="deep_research",
        metadata={
            "research_subscription_id": sub.get("id"),
            "research_topic": sub.get("topic"),
            "research_thread_id": thread_id,
            "research_depth": sub.get("depth"),
        },
    )


def run_research_subscription_now(room_name: str, subscription_id: str) -> Dict[str, Any]:
    """指定テーマの自動リサーチを即時に1件投入する（手動「今すぐ調べる」／ペルソナ要求用）。

    - due 判定・1日上限はバイパスする（明示的な要求のため）。
    - ただし逐次性は尊重し、他の委任が稼働中なら投入しない（OOM/資源暴走の防止）。
    返り値: {"ok": bool, "message": str, "task_id": str|None, "topic": str}
    """
    import agent_delegation
    import agent_delegation.manager as delegation_manager
    from research_subscription_manager import ResearchSubscriptionManager

    sub_manager = ResearchSubscriptionManager(room_name)
    sub = sub_manager.get_subscription(subscription_id)
    if not sub:
        return {"ok": False, "message": f"テーマが見つかりません: {subscription_id}", "task_id": None, "topic": ""}

    topic = sub.get("topic", "")

    try:
        settings = agent_delegation.get_agent_delegation_settings(room_name)
        if not settings.get("enabled"):
            return {"ok": False, "message": "このルームのエージェント委任が無効です。", "task_id": None, "topic": topic}
    except Exception:
        return {"ok": False, "message": "委任設定を確認できませんでした。", "task_id": None, "topic": topic}

    # 逐次：他の委任が稼働中なら投入しない。
    try:
        if delegation_manager._running_count() > 0:
            return {"ok": False, "message": "別の調査・委任が進行中のため、完了後に実行してください。", "task_id": None, "topic": topic}
    except Exception:
        pass

    try:
        task = _submit_research_for_subscription(room_name, sub)
    except Exception as exc:
        return {"ok": False, "message": f"委任の投入に失敗しました: {exc}", "task_id": None, "topic": topic}

    sub_manager.mark_run(sub["id"], when=datetime.datetime.now())
    print(f"🔬 [継続リサーチ] 手動調査を委任: {room_name} / 「{topic}」(task_id={task.get('id')})")
    return {"ok": True, "message": f"「{topic}」の調査を開始しました。", "task_id": task.get("id"), "topic": topic}


def check_research_subscriptions_scheduled():
    """全ルームの継続リサーチ購読をチェックし、期限が来たテーマを自動でディープリサーチ委任する。

    方針（計画 §6 の確定事項）：
    - 同時実行は1件ずつ（逐次）。委任が走っている間は新規投入しない（OOM/資源暴走の防止）。
    - 1ティックにつき投入は1件まで（毎時実行で1日かけて消化）。
    - テーマ単位で1日1回（get_due_subscriptions が run_time と last_run で判定）。
    - 全テーマ合計の1日上限（既定5・ルーム単位）を尊重し、超過分は翌日へ繰り越す。
    - 委任プロンプトに既存研究スレッド本文を渡し、重複回避＋CONTINUE/DEEPEN/NEW で追記させる。
    """
    try:
        import agent_delegation
        import agent_delegation.manager as delegation_manager
        from research_subscription_manager import ResearchSubscriptionManager

        # 逐次実行：いずれかの委任が稼働中なら、今回は投入を見送る。
        try:
            if delegation_manager._running_count() > 0:
                return
        except Exception:
            pass

        daily_cap = get_research_subscription_daily_cap()
        now = datetime.datetime.now()
        today = now.date()

        all_rooms = room_manager.get_room_list_for_ui()
        for _, room_folder in all_rooms:
            try:
                sub_manager = ResearchSubscriptionManager(room_folder)
                due = sub_manager.get_due_subscriptions(now=now)
                if not due:
                    continue

                # ルーム単位の1日上限を確認
                if daily_cap > 0 and sub_manager.count_runs_on(today) >= daily_cap:
                    continue

                # このルームの委任が有効か（無効なら静かにスキップ）
                try:
                    settings = agent_delegation.get_agent_delegation_settings(room_folder)
                    if not settings.get("enabled"):
                        continue
                except Exception:
                    continue

                sub = due[0]  # 最も実行が古い（None=未実行が最優先）テーマを1件
                try:
                    task = _submit_research_for_subscription(room_folder, sub)
                except Exception as submit_exc:
                    # 同時実行上限などで弾かれた場合は last_run を進めず、次ティックで再試行。
                    print(f"  - [継続リサーチ] 委任投入を見送り ({room_folder} / {sub.get('topic')}): {submit_exc}")
                    return

                sub_manager.mark_run(sub["id"], when=now)
                print(
                    f"🔬 [継続リサーチ] 自動調査を委任: {room_folder} / "
                    f"「{sub.get('topic')}」(task_id={task.get('id')})"
                )
                # 逐次実行のため、1ティックでの投入は1件だけにする。
                return

            except Exception as room_exc:
                print(f"  - [継続リサーチ] ルーム処理エラー ({room_folder}): {room_exc}")

    except Exception as e:
        print(f"継続リサーチ定期チェックエラー: {e}")
        traceback.print_exc()


def schedule_thread_function():
    global alarm_thread_stop_event
    print("--- アラームスケジューラスレッドを開始しました ---") # <--- 強調

    # 既存: 毎分00秒にアラームチェック
    schedule.every().minute.at(":00").do(check_alarms)

    # 追加: 毎分30秒に自律行動チェック
    schedule.every().minute.at(":30").do(check_autonomous_actions)

    # 追加: 毎時15分にウォッチリスト定期チェック
    schedule.every().hour.at(":15").do(check_watchlist_scheduled)

    # 追加: 毎分45秒に継続リサーチ購読チェック（テーマ駆動の自動リサーチ）。
    # 関数は軽量かつ自己制御（1ティック1件・委任稼働中はスキップ・テーマ毎1日1回・全体1日上限）なので
    # 毎分でも安全。これにより run_time をほぼ正確に守れる（相対30分間隔だと最大30分遅延＋再起動でリセットされていた）。
    schedule.every().minute.at(":45").do(check_research_subscriptions_scheduled)
    
    while not alarm_thread_stop_event.is_set():
        try:
            schedule.run_pending()
        except Exception as e:
            print(f"!!! スケジューラ実行エラー: {e}") # <--- エラーで落ちていないか確認
        time.sleep(1)
    print("アラームスケジューラスレッドが停止しました.")

def start_alarm_scheduler_thread():
    global alarm_thread_stop_event
    alarm_thread_stop_event.clear()
    config_manager.load_config()
    if not hasattr(start_alarm_scheduler_thread, "scheduler_thread") or not start_alarm_scheduler_thread.scheduler_thread.is_alive():
        thread = threading.Thread(target=schedule_thread_function, daemon=True)
        thread.start()
        start_alarm_scheduler_thread.scheduler_thread = thread
        print("アラームスケジューラスレッドを起動しました.")

def stop_alarm_scheduler_thread():
    global alarm_thread_stop_event
    if hasattr(start_alarm_scheduler_thread, "scheduler_thread") and start_alarm_scheduler_thread.scheduler_thread.is_alive():
        alarm_thread_stop_event.set()
        start_alarm_scheduler_thread.scheduler_thread.join()
        print("アラームスケジューラスレッドの停止を要求しました.")
