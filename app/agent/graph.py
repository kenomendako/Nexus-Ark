# agent/graph.py (v31: Dual-State Architecture - Cleaned)

import os
import copy
import re
import traceback
import json
import time
import glob
from datetime import datetime
from typing import TypedDict, Annotated, List, Literal, Tuple, Optional

from langchain_core.messages import SystemMessage, BaseMessage, ToolMessage, AIMessage, HumanMessage
from google.api_core import exceptions as google_exceptions
from langgraph.graph import StateGraph, END, START, add_messages

from agent.prompts import (
    CORE_PROMPT_TEMPLATE,
    THOUGHT_MANUAL_DISABLED_TEXT,
    THOUGHT_MANUAL_NATIVE_TEXT,
    THOUGHT_MANUAL_TAGGED_TEXT,
    TWITTER_MODE_PROMPT,
)
from agent.tool_message_compression import compress_tool_messages_for_agent
from tools.space_tools import set_current_location, list_available_locations, read_world_settings, plan_world_edit, _apply_world_edits
from tools.memory_tools import (
    recall_memories,
    search_past_conversations,
    read_memory_context,  # 記憶の続きを読む [2026-01-08 NEW]
    search_memory,  # 内部使用のみ（retrieval_nodeで使用）
    read_identity_memory, plan_identity_memory_edit, _apply_identity_memory_edits,
    read_diary_memory, plan_diary_append, _apply_diary_append,
    read_secret_diary, plan_secret_diary_edit, _apply_secret_diary_edits
)
from tools.notepad_tools import read_full_notepad, plan_notepad_edit,  _apply_notepad_edits
from tools.working_memory_tools import (
    read_working_memory, update_working_memory, list_working_memories, switch_working_memory,
    patch_working_memory, link_working_memory_to_research_thread, link_working_memory_to_goal,
    reactivate_working_memory_slot, get_working_memory_overview,
    archive_stale_working_memories, select_working_memory_for_research_context
)
from tools.purpose_profile_tools import read_purpose_profile, update_active_purpose, propose_purpose_change, approve_purpose_change
from tools.creative_tools import read_creative_notes, plan_creative_notes_edit, _apply_creative_notes_edits
from tools.research_tools import read_research_notes, plan_research_notes_edit, _apply_research_notes_edits
from tools.research_thread_tools import (
    list_research_threads, read_research_thread, find_similar_research_threads, update_research_thread
)
from tools.research_subscription_tools import manage_research_subscriptions
from tools.autonomy_tools import (
    read_autonomy_context, reflect_after_action,
    start_autonomy_timeline, record_autonomy_step, complete_autonomy_timeline
)
from tools.procedure_tools import list_procedures, read_procedure, save_procedure, create_procedure_from_timeline
from tools.closet_tools import (
    read_closet, read_user_closet, list_closet, wear_closet_item, take_off_closet_item,
    change_outfit, register_item_to_closet,
)
from tools.capability_policy_tools import (
    read_capability_policy, request_capability_approval, record_capability_audit
)
from tools.web_tools import web_search_tool, read_url_tool
from tools.image_tools import generate_image, view_past_image
from tools.alarm_tools import set_personal_alarm
from tools.timer_tools import set_timer, set_pomodoro_timer
from tools.knowledge_tools import search_knowledge_base
from tools.entity_tools import read_entity_memory, write_entity_memory, list_entity_memories, search_entity_memory
from tools.chess_tools import read_board_state, perform_move, get_legal_moves, reset_game as reset_chess_game
from tools.developer_tools import list_project_files, read_project_file
from tools.agent_delegation_tools import delegate_agent_task, delegate_anthology_task, delegate_atelier_task, delegate_deep_research, share_atelier_work, check_agent_task_status, cancel_agent_task, get_atelier_app_capabilities, preview_atelier_app, set_atelier_app_icon, list_agent_playbooks, list_agent_roles, review_agent_task, revise_agent_task, steer_agent_task, read_agent_task_report, share_research_result, propose_playbook_update
from tools.persona_contract_tools import read_persona_contract, check_text_against_persona_contract
from tools.introspection_tools import manage_open_questions, manage_goals
from tools.roblox_tools import send_roblox_command, roblox_build
from tools.twitter_tools import draft_tweet, post_tweet, check_twitter_updates
from tools.discord_tools import send_discord_message, send_discord_image
from tools.music_tools import recommend_music
from tools.roblox_screenshot import capture_roblox_screenshot
from tools.roblox_webhook import get_spatial_data
from tools.item_tools import (
    list_my_items, consume_item, gift_item_to_user, create_food_item,
    place_item_to_location, pickup_item_from_location, list_location_items, consume_item_from_location,
    create_standard_item, examine_item, create_and_gift_item
)
from tools.capability_tools import request_capability

from room_manager import get_world_settings_path, get_room_files_paths
from episodic_memory_manager import EpisodicMemoryManager
from action_plan_manager import ActionPlanManager
from tools.action_tools import (
    schedule_next_action,
    cancel_action_plan,
    read_current_plan,
    format_schedule_min_minutes_guidance,
)
from tools.notification_tools import send_user_notification
from tools.letterbox_tools import leave_letter_for_user, list_my_letters
from tools.watchlist_tools import add_to_watchlist, remove_from_watchlist, get_watchlist, check_watchlist, update_watchlist_interval
from tools.google_calendar_tools import read_calendar_schedule, check_free_time, add_calendar_event, delete_calendar_event, update_calendar_event, list_persona_calendar_events
from dreaming_manager import DreamingManager
from goal_manager import GoalManager
from entity_memory_manager import EntityMemoryManager
from llm_factory import LLMFactory

import utils
import config_manager
import gemini_explicit_cache_manager
import constants
import action_logger
import closet_manager

import pytz
import signature_manager
import room_manager
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError

# 【マルチモデル対応】OpenAIエラーのインポート
try:
    import openai
    OPENAI_ERRORS = (openai.NotFoundError, openai.BadRequestError, openai.APIError)
except ImportError:
    # openaiがインストールされていない場合のフォールバック
    OPENAI_ERRORS = ()

all_tools = [
    request_capability,
    set_current_location, list_available_locations, read_world_settings, plan_world_edit,
    # --- 記憶検索ツール ---
    recall_memories,  # 統合記憶検索（日記・過去ログ・エピソード記憶）
    search_past_conversations,  # キーワード完全一致検索（最終手段）
    read_memory_context,  # 検索結果の続きを読む [2026-01-08 NEW]
    # --- 日記・メモ操作ツール ---
    read_identity_memory, plan_identity_memory_edit, read_diary_memory, plan_diary_append, read_secret_diary, plan_secret_diary_edit,
    read_full_notepad, plan_notepad_edit,
    read_working_memory, update_working_memory, list_working_memories, switch_working_memory,
    patch_working_memory, link_working_memory_to_research_thread, link_working_memory_to_goal,
    reactivate_working_memory_slot,
    read_purpose_profile, update_active_purpose, propose_purpose_change, approve_purpose_change,
    # --- Web系ツール ---
    web_search_tool, read_url_tool,
    generate_image, view_past_image,
    set_personal_alarm,
    set_timer, set_pomodoro_timer,
    # --- 知識ベース・エンティティ検索ツール ---
    search_knowledge_base,  # 外部資料・マニュアル検索
    read_entity_memory, write_entity_memory, list_entity_memories, search_entity_memory,
    # --- アクション・通知ツール ---
    schedule_next_action, cancel_action_plan, read_current_plan,
    read_autonomy_context, reflect_after_action,
    start_autonomy_timeline, record_autonomy_step, complete_autonomy_timeline,
    list_procedures, read_procedure, save_procedure, create_procedure_from_timeline,
    read_closet, read_user_closet, list_closet, wear_closet_item, take_off_closet_item,
    change_outfit, register_item_to_closet,
    read_capability_policy, request_capability_approval, record_capability_audit,
    send_user_notification,
    leave_letter_for_user, list_my_letters,
    read_creative_notes, plan_creative_notes_edit,
    # --- ウォッチリストツール ---
    add_to_watchlist, remove_from_watchlist, get_watchlist, check_watchlist, update_watchlist_interval,
    # --- Googleカレンダー（読み取り・書き込み・編集・削除） ---
    read_calendar_schedule, check_free_time, add_calendar_event,
    delete_calendar_event, update_calendar_event, list_persona_calendar_events,
    read_research_notes, plan_research_notes_edit,
    list_research_threads, read_research_thread, find_similar_research_threads, update_research_thread,
    manage_research_subscriptions,
    # --- チェスツール ---
    read_board_state, perform_move, get_legal_moves, reset_chess_game,
    # --- 開発者ツール ---
    list_project_files, read_project_file,
    # --- エージェント委任ツール ---
    delegate_agent_task, delegate_anthology_task, delegate_atelier_task, delegate_deep_research, share_atelier_work, check_agent_task_status, cancel_agent_task, get_atelier_app_capabilities, preview_atelier_app, set_atelier_app_icon, list_agent_playbooks, list_agent_roles, review_agent_task, revise_agent_task, steer_agent_task, read_agent_task_report, share_research_result, propose_playbook_update,
    read_persona_contract, check_text_against_persona_contract,
    # --- 内省ツール ---
    manage_open_questions, manage_goals,
    # --- ROBLOX連携ツール ---
    send_roblox_command, roblox_build, capture_roblox_screenshot,
    # --- 食べ物・アイテムツール ---
    list_my_items, consume_item, gift_item_to_user, create_food_item,
    place_item_to_location, pickup_item_from_location, list_location_items, consume_item_from_location,
    create_standard_item, examine_item, create_and_gift_item,
    # --- Twitter (X) ツール ---
    draft_tweet, post_tweet, check_twitter_updates,
    # --- Discord連携ツール ---
    send_discord_message, send_discord_image,
    # --- 音楽推薦ツール ---
    recommend_music
]

side_effect_tools = [
    "plan_main_memory_edit", "plan_secret_diary_edit", "plan_notepad_edit", "plan_world_edit",
    "plan_creative_notes_edit",
    "plan_research_notes_edit",
    "update_working_memory", "switch_working_memory", "patch_working_memory", "link_working_memory_to_research_thread",
    "link_working_memory_to_goal", "reactivate_working_memory_slot",
    "update_active_purpose", "propose_purpose_change", "approve_purpose_change",
    "update_research_thread",
    "manage_research_subscriptions",
    "set_personal_alarm", "set_timer", "set_pomodoro_timer",
    "schedule_next_action", "send_user_notification", "leave_letter_for_user", "send_discord_message", "send_discord_image",
    "delegate_agent_task", "delegate_anthology_task", "delegate_atelier_task", "delegate_deep_research", "share_atelier_work", "cancel_agent_task", "preview_atelier_app", "set_atelier_app_icon", "share_research_result", "propose_playbook_update",
    "reflect_after_action", "start_autonomy_timeline", "record_autonomy_step", "complete_autonomy_timeline",
    "save_procedure", "create_procedure_from_timeline",
    "request_capability_approval", "record_capability_audit",
    "wear_closet_item", "take_off_closet_item", "change_outfit", "register_item_to_closet",
]

_WM_SECTION_ALIASES = {
    "current_intent": "Current Intent",
    "current intent": "Current Intent",
    "known_context": "Known Context",
    "known context": "Known Context",
    "context": "Known Context",
    "next_action": "Next Action",
    "next action": "Next Action",
    "next": "Next Action",
    "action": "Next Action",
    "stop_condition": "Stop Condition",
    "stop condition": "Stop Condition",
    "stop": "Stop Condition",
    "goal": "Goal",
    "summary": "Summary",
}

_WM_ARG_CONTAINER_KEYS = {
    "args",
    "arguments",
    "input",
    "payload",
    "data",
    "parameters",
    "patch",
    "update",
    "memory",
    "working_memory",
    "workingMemory",
}

_WM_META_KEYS = {
    "room_name",
    "slot_name",
    "intent",
    "context_type",
    "mode",
    "section",
    "content",
}


def _normalize_wm_key(key) -> str:
    return str(key or "").strip().strip("'\"").replace("-", "_").lower()


def _canonical_wm_section_name(section) -> str:
    raw = str(section or "").strip().strip("'\"").lstrip("#").strip()
    normalized = _normalize_wm_key(raw).replace("_", " ")
    return _WM_SECTION_ALIASES.get(_normalize_wm_key(raw), _WM_SECTION_ALIASES.get(normalized, raw))


def _coerce_tool_text(value) -> str:
    """ツール引数でよく混ざる list/dict を保存可能な短い文字列へ寄せる。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _supports_native_thinking_parts(model_name: str) -> bool:
    model_name_lower = (model_name or "").lower()
    return (
        re.search(r"gemini[-_\s]?3(?:[.\-_]?\d+)?", model_name_lower) is not None
        or "thinking" in model_name_lower
    )


def _is_gemini_3_flash_family(model_name: str) -> bool:
    model_name_lower = (model_name or "").lower()
    return _supports_native_thinking_parts(model_name_lower) and "gemini" in model_name_lower and "flash" in model_name_lower

def _is_gemini_3_signature_compatible(model_name: str) -> bool:
    return "gemini-3" in (model_name or "").lower()

def _strip_gemini_thought_signatures(messages: list) -> int:
    """別モデルへ渡せない Gemini 3 thought signature を送信前履歴から除去する。"""
    stripped_count = 0
    for msg in messages or []:
        additional_kwargs = getattr(msg, "additional_kwargs", None)
        if not additional_kwargs:
            continue
        before = len(additional_kwargs)
        additional_kwargs.pop("__gemini_function_call_thought_signatures__", None)
        additional_kwargs.pop("thought_signature", None)
        if len(additional_kwargs) != before:
            stripped_count += 1
    return stripped_count

def _is_thought_signature_error(error: Exception | str) -> bool:
    error_str = str(error or "").lower()
    return (
        "thought_signature" in error_str
        or "thought signature" in error_str
        or "corrupted thought signature" in error_str
    )

def _get_configured_internal_model_name(role: str, fallback: str = "") -> str:
    try:
        _, model_name, _ = config_manager.get_effective_internal_model(role)
        return utils.sanitize_model_name(model_name or fallback or constants.INTERNAL_PROCESSING_MODEL)
    except Exception:
        return fallback or constants.INTERNAL_PROCESSING_MODEL

def _get_llm_model_name(llm: object, fallback: str = "") -> str:
    for attr in ("model_name", "model"):
        value = getattr(llm, attr, None)
        if value:
            return utils.sanitize_model_name(str(value))
    return fallback or constants.INTERNAL_PROCESSING_MODEL

def _is_agent_resource_exhausted_error(error: Exception) -> bool:
    err_str = str(error).upper()
    return (
        isinstance(error, google_exceptions.ResourceExhausted)
        or "429" in err_str
        or "RESOURCE_EXHAUSTED" in err_str
    )

def _is_agent_service_unavailable_error(error: Exception, *, is_429: bool = False) -> bool:
    if is_429:
        return False
    err_str = str(error).upper()
    return "503" in err_str or "UNAVAILABLE" in err_str or "OVERLOADED" in err_str

def _is_agent_prompt_too_long_error(error: Exception) -> bool:
    err_str = str(error).lower()
    return "prompt is too long" in err_str or "prompt too long" in err_str


def _is_model_image_unsupported_error(error: Exception) -> bool:
    """送信先モデル（プロバイダ）が画像入力に対応していない場合のエラーを検出する。

    プロバイダ非依存。OpenRouter / OpenAI互換 / ローカルモデル等で文言が異なるため、
    「画像・ビジョン・マルチモーダルへの言及」かつ「非対応を示す表現」の両方が含まれる
    場合に画像非対応とみなす（誤検知しても画像除去→1回だけ再試行で済むため安全側に倒す）。
    例: OpenRouter `404 - No endpoints found that support image input`
    """
    s = str(error).lower()
    has_image_term = any(t in s for t in ("image", "vision", "multimodal", "image_url"))
    if not has_image_term:
        return False
    unsupported_markers = (
        "no endpoints found that support",
        "does not support",
        "doesn't support",
        "do not support",
        "not support",
        "not supported",
        "unsupported",
        "cannot process",
        "can't process",
        "no vision",
        "not capable",
        "text-only",
        "text only",
        "non-multimodal",
    )
    return any(m in s for m in unsupported_markers)


def _strip_image_content_from_messages(messages: List[BaseMessage]) -> tuple:
    """マルチモーダル（画像付き）メッセージから画像パートを除去しテキストのみにする。

    画像非対応モデルへ送る際のフォールバック用。元メッセージは変更せず、
    画像を含むメッセージのみ複製して差し替えた新しいリストと、除去件数を返す。
    """
    new_messages: List[BaseMessage] = []
    stripped_count = 0
    # ユーザー向けの説明はシステムアナウンス（add_system_notice）で別途行うため、
    # ここは中立な目印に留める。キャプション・情景描写などのテキストは別メッセージに残る。
    placeholder = "[画像（このモデルは画像入力に非対応のため省略）]"
    for msg in messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            new_messages.append(msg)
            continue

        text_parts = []
        had_image = False
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "image_url" or "image_url" in part:
                    had_image = True
                    continue
                if ptype == "text":
                    text_parts.append(part.get("text", ""))
                    continue
                # 未知のdictパートはテキスト化して温存
                text_parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                text_parts.append(part)

        if not had_image:
            new_messages.append(msg)
            continue

        stripped_count += 1
        new_text = "\n".join([t for t in text_parts if t]).strip()
        if not new_text:
            new_text = placeholder
        else:
            new_text = f"{new_text}\n{placeholder}"

        try:
            new_msg = msg.model_copy(deep=False)
            new_msg.content = new_text
        except Exception:
            new_msg = type(msg)(content=new_text)
        new_messages.append(new_msg)

    return new_messages, stripped_count

AUTONOMY_FINALIZATION_TOOL_NAMES = [
    "reflect_after_action",
    "complete_autonomy_timeline",
    "record_autonomy_step",
    "record_capability_audit",
]


def _is_schedule_tool_allowed(room_name: str) -> bool:
    try:
        _cfg = room_manager.get_room_config(room_name) or {}
        _auto_settings = _cfg.get("override_settings", {}).get("autonomous_settings", {})
        return _auto_settings.get("allow_schedule_tool", True)
    except Exception:
        return True


def _autonomy_finalization_tool_names_for_room(room_name: str, include_schedule: bool = False) -> List[str]:
    names = list(AUTONOMY_FINALIZATION_TOOL_NAMES)
    if include_schedule and _is_schedule_tool_allowed(room_name):
        names.append("schedule_next_action")
    return names


def _loop_limit_tool_names_for_state(state: dict) -> List[str]:
    if state.get("loop_count", 0) < constants.MAX_TOOL_LOOPS:
        return []
    if not state.get("autonomous_action", False):
        return []
    return _autonomy_finalization_tool_names_for_room(
        state.get("room_name", ""),
        include_schedule=True,
    )


def _append_schedule_tool_guidance(description: str, room_name: str) -> str:
    try:
        return f"{description}\n  {format_schedule_min_minutes_guidance(room_name)}"
    except Exception as e:
        print(f"  - [Action Plan] schedule最小間隔説明の生成エラー: {e}")
        return description

def _select_tools_by_name(tool_names: List[str]) -> List[object]:
    tool_map = {tool.name: tool for tool in all_tools}
    return [tool_map[name] for name in tool_names if name in tool_map]

def _has_tool_message(messages: List[BaseMessage], tool_name: str) -> bool:
    return any(isinstance(msg, ToolMessage) and getattr(msg, "name", "") == tool_name for msg in messages or [])

def _add_system_instruction(messages: List[BaseMessage], instruction: str) -> List[BaseMessage]:
    """Append a transient instruction to the leading system prompt without creating mid-history system messages."""
    if not instruction:
        return messages

    if not messages:
        return [SystemMessage(content=instruction)]

    first_msg = messages[0]
    if isinstance(first_msg, SystemMessage):
        merged_msg = copy.deepcopy(first_msg)
        existing_content = merged_msg.content if isinstance(merged_msg.content, str) else str(merged_msg.content)
        merged_msg.content = f"{existing_content}\n\n{instruction}"
        return [merged_msg] + list(messages[1:])

    return [SystemMessage(content=instruction)] + list(messages)

def _apply_pending_capability_followup_instruction(
    messages: List[BaseMessage],
    state: dict,
) -> tuple[List[BaseMessage], Optional[dict]]:
    pending = state.get("pending_capability_followup") or {}
    instruction = str(pending.get("reminder_instruction") or "").strip()
    if not instruction:
        return messages, None

    updated_pending = dict(pending)
    updated_pending.pop("reminder_instruction", None)
    print("  - [Capability FollowUp] 差し戻し指示を一時システムプロンプトへ追記します。")
    return _add_system_instruction(messages, instruction), updated_pending

def _has_autonomy_timeline_context(messages: List[BaseMessage]) -> bool:
    for msg in messages or []:
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") in {"start_autonomy_timeline", "record_autonomy_step", "reflect_after_action"}:
            return True
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if "timeline_id:" in content or "timeline_id" in content:
            return True
    return False

def _should_prioritize_autonomy_finalization(tool_name: str, output: object, state: dict) -> bool:
    if tool_name not in {"plan_research_notes_edit", "plan_creative_notes_edit", "patch_working_memory", "update_working_memory"}:
        return False
    output_text = str(output or "")
    if not output_text.startswith("成功"):
        return False
    if _has_tool_message(state.get("messages", []), "complete_autonomy_timeline"):
        return False
    return _has_autonomy_timeline_context(state.get("messages", []))

def _is_autonomy_finalization_tool(tool_name: str) -> bool:
    return tool_name in set(AUTONOMY_FINALIZATION_TOOL_NAMES)


def _get_part_value(part, key: str, default=None):
    if isinstance(part, dict):
        return part.get(key, default)
    return getattr(part, key, default)


def _is_native_thought_part(part) -> bool:
    p_type = _get_part_value(part, "type")
    if p_type in ("thought", "thinking"):
        return True

    # Gemini 3.5 Flash の公式レスポンス例は text part + thought=True。
    thought_flag = _get_part_value(part, "thought")
    if isinstance(thought_flag, bool):
        return thought_flag

    return bool(_get_part_value(part, "thinking"))


def _extract_native_part_text(part) -> str:
    for key in ("thinking", "text", "content"):
        value = _get_part_value(part, key)
        if isinstance(value, str) and value:
            return value

    thought_value = _get_part_value(part, "thought")
    if isinstance(thought_value, str) and thought_value:
        return thought_value

    return ""


def _combine_chunks_with_thought_parts(chunks: list) -> str:
    """
    LangChain/Geminiの list content から thinking/thought と text を分離し、
    既存ログ形式の [THOUGHT] ブロックへ変換して保存可能な本文にする。
    """
    text_parts = []
    thought_buffer = []
    is_collecting_thought = False

    for chunk in chunks:
        orig_content = getattr(chunk, "content", None)
        if isinstance(orig_content, list):
            for part in orig_content:
                if not isinstance(part, dict) and not hasattr(part, "text"):
                    continue
                if _is_native_thought_part(part):
                    t_text = _extract_native_part_text(part)
                    if t_text and t_text.strip():
                        thought_buffer.append(t_text)
                        is_collecting_thought = True
                else:
                    if is_collecting_thought and thought_buffer:
                        text_parts.append(f"[THOUGHT]\n{''.join(thought_buffer)}\n[/THOUGHT]\n")
                        thought_buffer = []
                        is_collecting_thought = False
                    text_val = _extract_native_part_text(part)
                    if text_val:
                        text_parts.append(text_val)
        else:
            chunk_content = utils.get_content_as_string(chunk)
            if chunk_content and chunk_content.strip():
                if is_collecting_thought and thought_buffer:
                    text_parts.append(f"[THOUGHT]\n{''.join(thought_buffer)}\n[/THOUGHT]\n")
                    thought_buffer = []
                    is_collecting_thought = False
                text_parts.append(chunk_content)

    if is_collecting_thought and thought_buffer:
        text_parts.append(f"[THOUGHT]\n{''.join(thought_buffer)}\n[/THOUGHT]\n")

    return "".join(text_parts)


def _normalize_working_memory_tool_args(tool_name: str, tool_args: dict) -> dict:
    """Geminiが出しがちな省略形をWorking Memoryツールの厳密スキーマへ寄せる。"""
    if not isinstance(tool_args, dict):
        return tool_args

    for container_key in list(_WM_ARG_CONTAINER_KEYS):
        container = tool_args.get(container_key)
        if isinstance(container, dict):
            for key, value in container.items():
                clean_key = str(key).strip("'\"")
                if clean_key not in tool_args:
                    tool_args[clean_key] = value

    if tool_name == "patch_working_memory":
        for alias in ["target_section", "section_name", "field", "key"]:
            if alias in tool_args and not tool_args.get("section"):
                tool_args["section"] = tool_args.pop(alias)
                break

        for alias in ["value", "text", "message", "new_content", "note", "progress_note"]:
            if alias in tool_args and not tool_args.get("content"):
                tool_args["content"] = tool_args.pop(alias)
                break

        section_candidates = [
            (key, value) for key, value in list(tool_args.items())
            if _canonical_wm_section_name(key) != str(key).strip("'\"") and _coerce_tool_text(value)
        ]
        if (not tool_args.get("section") or not tool_args.get("content")) and len(section_candidates) == 1:
            key, value = section_candidates[0]
            tool_args.setdefault("section", _canonical_wm_section_name(key))
            tool_args.setdefault("content", value)
        elif (not tool_args.get("section") or not tool_args.get("content")) and len(section_candidates) > 1:
            tool_args.setdefault("section", "Known Context")
            tool_args.setdefault(
                "content",
                "\n\n".join(
                    f"## {_canonical_wm_section_name(key)}\n{_coerce_tool_text(value)}"
                    for key, value in section_candidates
                ),
            )
        elif not tool_args.get("section") and tool_args.get("content"):
            tool_args["section"] = "Next Action"

        if not tool_args.get("content"):
            fallback_candidates = [
                (key, value) for key, value in list(tool_args.items())
                if _normalize_wm_key(key) not in _WM_META_KEYS
                and _normalize_wm_key(key) not in {_normalize_wm_key(k) for k in _WM_ARG_CONTAINER_KEYS}
                and _coerce_tool_text(value)
            ]
            if len(fallback_candidates) == 1:
                key, value = fallback_candidates[0]
                inferred_section = _canonical_wm_section_name(key)
                tool_args.setdefault("section", inferred_section if inferred_section != str(key).strip("'\"") else "Next Action")
                tool_args["content"] = value

        if "section" in tool_args:
            tool_args["section"] = _canonical_wm_section_name(tool_args["section"])
        if "content" in tool_args:
            tool_args["content"] = _coerce_tool_text(tool_args["content"])

    elif tool_name == "update_working_memory":
        for alias in ["memory", "new_content", "text", "message", "value", "note"]:
            if alias in tool_args and not tool_args.get("content"):
                tool_args["content"] = tool_args.pop(alias)
                break
        if not tool_args.get("content"):
            section_lines = []
            for key, value in tool_args.items():
                heading = _canonical_wm_section_name(key)
                if heading == str(key).strip("'\""):
                    continue
                value = _coerce_tool_text(value)
                if value:
                    section_lines.append(f"## {heading}\n{value}")
            if section_lines:
                tool_args["content"] = "\n\n".join(section_lines)
        if "content" in tool_args:
            tool_args["content"] = _coerce_tool_text(tool_args["content"])

    return tool_args


def _normalize_research_notes_tool_args(tool_args: dict) -> dict:
    """研究ノート更新の必須メタデータを、欠落時も明示値として補完する。"""
    if not isinstance(tool_args, dict):
        return tool_args

    for alias in ["relation_type", "context", "type", "classification"]:
        if alias in tool_args and not tool_args.get("context_type"):
            tool_args["context_type"] = tool_args.pop(alias)
            break

    for alias in ["intent", "reason", "reasoning", "purpose", "rationale"]:
        if alias in tool_args and not tool_args.get("intent_and_reasoning"):
            tool_args["intent_and_reasoning"] = tool_args.pop(alias)
            break

    context_type = _coerce_tool_text(tool_args.get("context_type")).upper()
    has_existing_target = bool(
        _coerce_tool_text(tool_args.get("thread_id"))
        or _coerce_tool_text(tool_args.get("target_heading"))
        or _coerce_tool_text(tool_args.get("evidence_of_prior_read"))
    )
    valid_context_types = {"CONTINUE", "DEEPEN", "NEW", "CONTRADICT"}
    if context_type not in valid_context_types:
        context_type = "DEEPEN" if has_existing_target else "NEW"
        tool_args["context_type"] = context_type

    intent = _coerce_tool_text(tool_args.get("intent_and_reasoning"))
    if not intent or intent.upper() == "N/A":
        if context_type == "NEW":
            intent = (
                "自動補完: 既存スレッド指定がないためNEWとして扱う。"
                "新規にする理由: 今回の更新要求を独立した研究ノート項目として保存する必要があるため。"
            )
        else:
            target = _coerce_tool_text(tool_args.get("thread_id")) or _coerce_tool_text(tool_args.get("target_heading")) or "既存研究ノート"
            intent = f"自動補完: {target} への継続・深化として研究ノートへ反映するため。"
        tool_args["intent_and_reasoning"] = intent

    if context_type in {"CONTINUE", "DEEPEN", "CONTRADICT"}:
        if not _coerce_tool_text(tool_args.get("target_heading")) and not _coerce_tool_text(tool_args.get("thread_id")):
            tool_args["target_heading"] = "既存研究ノート"
        if not _coerce_tool_text(tool_args.get("evidence_of_prior_read")):
            tool_args["evidence_of_prior_read"] = "自動補完: ツール呼び出し時点で既存研究ノートへの継続・深化として指定された。"

    return tool_args


def _normalize_manage_goals_tool_args(tool_args: dict) -> dict:
    """manage_goals の進捗記録で出やすい別名を吸収する。"""
    if not isinstance(tool_args, dict):
        return tool_args

    if "action" in tool_args and isinstance(tool_args["action"], str):
        tool_args["action"] = tool_args["action"].strip().lower()

    for alias in ["index", "goal_number", "number", "target_index"]:
        if alias in tool_args and tool_args.get("goal_index") is None:
            tool_args["goal_index"] = tool_args.pop(alias)
            break

    for alias in ["note", "content", "message", "text", "progress", "update"]:
        if alias in tool_args and not tool_args.get("progress_note"):
            tool_args["progress_note"] = tool_args.pop(alias)
            break

    return tool_args

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    room_name: str
    api_key: str
    api_key_name: str
    model_name: str
    system_prompt: SystemMessage
    generation_config: dict
    send_core_memory: bool
    send_scenery: bool
    send_notepad: bool
    send_thoughts: bool
    send_current_time: bool
    location_name: str
    scenery_text: str
    debug_mode: bool
    display_thoughts: bool
    all_participants: List[str]
    loop_count: float
    season_en: str
    time_of_day_en: str
    last_successful_response: Optional[AIMessage]
    force_end: bool
    skip_tool_execution: bool
    retrieved_context: str
    tool_use_enabled: bool  # 【ツール不使用モード】ツール使用の有効/無効
    autonomous_action: bool
    autonomous_trigger_source: str
    autonomous_timeline_id: str
    autonomy_finalization_pending: bool  # 自律行動の後始末を優先するフラグ
    autonomy_finalization_reason: str
    active_tool_names: List[str]
    pending_capability_followup: Optional[dict]
    next: str
    enable_supervisor: bool # Supervisor機能の有効/無効
    speakers_this_turn: List[str]  # [v19] 今ターン発言済みのペルソナリスト
    custom_system_prompt: Optional[str] # システムプロンプトの上書き用
    is_roblox_active: bool # Robloxとの接続状態
    actual_token_usage: Optional[dict] = None # 【2026-01-10 NEW】実送信トークン数記録用


def _cap_history_messages_for_agent(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Agentへ渡す履歴はUIの最大表示件数を上限にする。"""
    if len(messages) > constants.UI_HISTORY_MAX_LIMIT:
        return messages[-constants.UI_HISTORY_MAX_LIMIT:]
    return messages


def get_location_list(room_name: str) -> List[str]:
    if not room_name: return []
    world_settings_path = get_world_settings_path(room_name)
    if not world_settings_path or not os.path.exists(world_settings_path): return []
    world_data = utils.parse_world_file(world_settings_path)
    if not world_data: return []
    locations = set()
    for area_name, places in world_data.items():
        for place_name in places.keys():
            if place_name == "__area_description__": continue
            locations.add(place_name)
    return sorted(list(locations))

from agent.scenery_manager import generate_scenery_context

AUTO_KNOWLEDGE_MAX_RESULTS = 3
AUTO_KNOWLEDGE_MAX_CHARS_PER_RESULT = 500
AUTO_KNOWLEDGE_MAX_TOTAL_CHARS = 1800


def _format_auto_knowledge_results(docs: list) -> str:
    """自動想起用ナレッジを出典付き・上限付きで整形する。"""
    if not docs:
        return ""

    parts = ["【ナレッジからの関連情報】"]
    seen_contents = set()
    selected_count = 0

    for doc in docs:
        if selected_count >= AUTO_KNOWLEDGE_MAX_RESULTS:
            break

        content = str(getattr(doc, "page_content", "") or "").strip()
        if not content:
            continue

        content_key = re.sub(r"\s+", " ", content).strip().lower()[:300]
        if content_key in seen_contents:
            continue
        seen_contents.add(content_key)

        if len(content) > AUTO_KNOWLEDGE_MAX_CHARS_PER_RESULT:
            content = content[:AUTO_KNOWLEDGE_MAX_CHARS_PER_RESULT] + "..."

        metadata = getattr(doc, "metadata", {}) or {}
        source_name = os.path.basename(str(metadata.get("source", "不明なファイル")))
        candidate = f"--- [ナレッジ / 出典: {source_name}] ---\n{content}"
        current_text = "\n\n".join(parts)
        remaining = AUTO_KNOWLEDGE_MAX_TOTAL_CHARS - len(current_text) - 2
        if remaining <= 3:
            break
        if len(candidate) > remaining:
            candidate = candidate[:remaining - 3].rstrip() + "..."
        parts.append(candidate)
        selected_count += 1

    return "\n\n".join(parts) if selected_count else ""


def _search_auto_knowledge_context(
    generation_config: dict,
    room_name: str,
    api_key: str,
    query: str,
    intent: str,
) -> tuple[str, int]:
    """明示的に許可された場合だけ、ナレッジ索引を自動想起する。"""
    if not bool((generation_config or {}).get("include_knowledge_in_auto_retrieval", False)):
        return "", 0
    if not query:
        return "", 0

    try:
        import rag_manager

        knowledge_manager = rag_manager.RAGManager(room_name, api_key)
        knowledge_docs = knowledge_manager.search(
            query,
            k=AUTO_KNOWLEDGE_MAX_RESULTS,
            score_threshold=1.15,
            intent=intent,
            scope="knowledge",
        )
        knowledge_result = _format_auto_knowledge_results(knowledge_docs)
        selected_count = knowledge_result.count("--- [ナレッジ / 出典:") if knowledge_result else 0
        return knowledge_result, selected_count
    except Exception as knowledge_e:
        # ナレッジ索引だけの失敗で、既に得られた記憶を捨てない。
        print(f"    -> ナレッジ: エラー（記憶想起は継続） ({knowledge_e})")
        return "", 0


# ▼▼▼ [2026-01-07 ハイブリッド検索] キーワード検索用内部関数 ▼▼▼
def _keyword_search_for_retrieval(
    keywords: list,
    room_name: str,
    exclude_recent_count: int
) -> list:
    """
    retrieval_node専用のキーワード検索。
    search_past_conversationsツールのロジックを流用するが、
    より厳格なフィルタリングを適用。

    時間帯別枠取り: 新2 + 古2 + 中間ランダム1 = 計5件
    """
    import random
    from pathlib import Path

    if not keywords or not room_name:
        return []

    base_path = Path(constants.ROOMS_DIR) / room_name
    search_paths = [str(base_path / "log.txt")]
    search_paths.extend(glob.glob(str(base_path / "log_archives" / "*.txt")))
    search_paths.extend(glob.glob(str(base_path / "log_import_source" / "*.txt")))

    found_blocks = []
    date_patterns = [
        re.compile(r'(\d{4}-\d{2}-\d{2}) \(...\) \d{2}:\d{2}:\d{2}'),
        re.compile(r'###\s*(\d{4}-\d{2}-\d{2})')
    ]

    search_keywords = [k.lower() for k in keywords]

    for file_path_str in search_paths:
        file_path = Path(file_path_str)
        if not file_path.exists() or file_path.stat().st_size == 0:
            continue

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception:
            continue

        # USER/AGENT のヘッダーのみを対象（SYSTEMは除外）
        header_indices = [
            i for i, line in enumerate(lines)
            if re.match(r"^(## (?:USER|AGENT):.*)$", line.strip())
        ]
        if not header_indices:
            continue

        search_end_line = len(lines)

        # log.txt の場合、最新N件を除外（送信ログ除外）
        if file_path.name == "log.txt" and exclude_recent_count > 0:
            msg_count = len(header_indices)
            if msg_count <= exclude_recent_count:
                continue
            else:
                cutoff_header_index = header_indices[-exclude_recent_count]
                search_end_line = cutoff_header_index

        processed_blocks_content = set()

        for i, line in enumerate(lines[:search_end_line]):
            if any(k in line.lower() for k in search_keywords):
                # ヘッダーを探す
                start_index = 0
                for h_idx in reversed(header_indices):
                    if h_idx <= i:
                        start_index = h_idx
                        break

                # 次のヘッダーまでをブロックとする
                end_index = len(lines)
                for h_idx in header_indices:
                    if h_idx > start_index:
                        end_index = h_idx
                        break

                block_content = "".join(lines[start_index:end_index]).strip()

                # 重複チェック
                if block_content in processed_blocks_content:
                    continue
                processed_blocks_content.add(block_content)

                # 短すぎるブロックを除外
                if len(block_content) < 30:
                    continue

                # 日付を抽出
                block_date = None
                for pattern in date_patterns:
                    matches = list(pattern.finditer(block_content))
                    if matches:
                        block_date = matches[-1].group(1)
                        break

                found_blocks.append({
                    "content": block_content,
                    "date": block_date,
                    "source": file_path.name
                })

    if not found_blocks:
        return []

    # 時間帯別枠取り: 新2 + 古2 + 中間ランダム1 = 計5件
    # 日付順ソート（新しい順）
    sorted_blocks = sorted(
        found_blocks,
        key=lambda x: x.get('date') or '0000-00-00',
        reverse=True
    )

    # 重複を除去（コンテンツベース）
    unique_blocks = []
    seen_contents = set()
    for b in sorted_blocks:
        content_key = b.get('content', '')[:200]  # 先頭200文字で重複判定
        if content_key not in seen_contents:
            seen_contents.add(content_key)
            unique_blocks.append(b)

    if len(unique_blocks) <= 5:
        return unique_blocks

    # 時間帯別に選択
    newest = unique_blocks[:2]   # 新しい方から2件
    oldest = unique_blocks[-2:]  # 古い方から2件

    # 中間部分からランダムに1件選択
    middle = unique_blocks[2:-2]
    random_middle = [random.choice(middle)] if middle else []

    # 結合（既に重複除去済みなのでそのまま）
    selected = list(newest) + [b for b in oldest if b not in newest] + [b for b in random_middle if b not in newest and b not in oldest]

    print(f"    -> [時間帯別枠取り] 全{len(found_blocks)}件 → 重複除去後{len(unique_blocks)}件 → 選択{len(selected)}件")

    return selected[:5]
# ▲▲▲ キーワード検索用内部関数ここまで ▲▲▲

def _should_skip_retrieval_locally(query_source: str) -> bool:
    """
    LLMに検索要否判断を投げる前の保守的な早期スキップ。

    明確に過去記憶・知識・事実確認を求めていそうな発話はスキップしない。
    短い相槌や感情表現だけを即時スキップ対象にする。
    固有名詞（カタカナ3文字以上）が含まれる場合はスキップしない。
    """
    text = (query_source or "").strip()
    if not text:
        return True

    retrieval_markers = [
        "覚えて", "覚えてる", "思い出", "記憶", "過去", "前に", "以前",
        "日記", "ログ", "資料", "仕様", "マニュアル", "知識", "検索",
        "調べ", "探し", "いつ", "誰", "どこ", "どれ", "なぜ", "理由",
        "比較", "まとめ", "教えて", "確認", "url", "http://", "https://",
    ]
    lower_text = text.lower()
    if any(marker in lower_text for marker in retrieval_markers):
        return False

    # カタカナ3文字以上の固有名詞が含まれていたらスキップしない（人名・地名等）
    if re.search(r'[ァ-ヴー]{3,}', text):
        return False

    # 鉤括弧で囲まれた語句があればスキップしない（引用・固有名）
    if re.search(r'「.+?」', text):
        return False

    normalized = re.sub(r"[\s　、。,.!！?？…ー〜~]+", "", lower_text)
    conversational_phrases = {
        "うん", "うんうん", "そう", "そうだね", "そうですね", "そっか", "なるほど",
        "うんそうだね", "うんそうですね",
        "ありがとう", "ありがと", "助かった", "了解", "りょうかい", "わかった",
        "かわいい", "すごい", "いいね", "大丈夫", "大丈夫だよ", "おはよう",
        "こんにちは", "こんばんは", "おやすみ", "ただいま", "おかえり",
    }
    if normalized in conversational_phrases:
        return True

    # ごく短い感情だけの返答は検索判断LLMを挟まない。
    affective_markers = ["好き", "嬉しい", "寂しい", "眠い", "疲れた", "安心", "怖い"]
    return len(normalized) <= 12 and any(marker in normalized for marker in affective_markers)


def _invoke_retrieval_decision_with_timeout(llm, messages: list, timeout_seconds: float = 15.0):
    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(llm.invoke, messages)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"検索要否判定がタイムアウトしました ({timeout_seconds:.0f}秒)") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _extract_search_queries(query_source: str, room_name: str) -> dict:
    """
    ユーザー発言からRAGクエリ、キーワード、意図を
    ルールベースで瞬時に抽出する。LLM不要・タイムアウトなし。
    LLMタイムアウト時のフォールバックとして使用し、
    記憶想起スキップを防止する。

    Returns:
        {
            "rag_query": str,       # 意味検索用クエリ
            "keyword_query": str,   # 完全一致検索用キーワード（スペース区切り）
            "intent": str,          # emotional/factual/temporal/relational
        }
    """
    text = (query_source or "").strip()
    if not text:
        return {"rag_query": "", "keyword_query": "", "intent": constants.DEFAULT_INTENT}

    # --- RAGクエリ: ユーザー発言をそのまま使用（末尾記号除去、長すぎる場合は切り詰め） ---
    rag_query = re.sub(r'[。、！？!?…~〜]+$', '', text).strip()
    if len(rag_query) > 300:
        rag_query = rag_query[:300]

    # --- キーワード抽出 ---
    keywords = set()

    # 1. カタカナ連続3文字以上（人名・地名・作品名等の固有名詞）
    katakana_matches = re.findall(r'[ァ-ヴー]{3,}', text)
    for match in katakana_matches:
        # 一般的なカタカナ語を除外（検索ノイズ軽減）
        common_katakana = {
            "テスト", "メッセージ", "コメント", "データ", "ファイル",
            "システム", "エラー", "チェック", "スタート", "ストップ",
            "パスワード", "サーバー", "クリア", "キャンセル", "リセット",
        }
        if match not in common_katakana:
            keywords.add(match)

    # 2. 鉤括弧内の語句
    bracket_matches = re.findall(r'「(.+?)」', text)
    for match in bracket_matches:
        # 15文字以下の単語/フレーズのみ、かつ句読点や改行・スペースを含まないものを対象とする
        if 2 <= len(match) <= 15 and not any(c in match for c in ["、", "。", "\n", " "]):
            keywords.add(match)

    # 3. 英字連続2文字以上（英語の固有名詞等）
    alpha_matches = re.findall(r'[A-Za-z]{2,}', text)
    for match in alpha_matches:
        # 一般的な英単語を除外
        common_english = {
            "ok", "no", "yes", "hi", "the", "is", "it", "to", "in",
            "of", "and", "or", "for", "on", "at", "by", "an",
        }
        if match.lower() not in common_english:
            keywords.add(match)

    # 4. エンティティ記憶 _index.json のエントリ名・別名と照合
    try:
        em_manager = EntityMemoryManager(room_name)
        index = em_manager._ensure_index()
        for meta in index.get("entities", {}).values():
            if not isinstance(meta, dict) or meta.get("status") == "archived":
                continue
            names_to_check = [meta.get("canonical_name", "")]
            names_to_check.extend(meta.get("aliases", []))
            for name in names_to_check:
                # 15文字以下の固有名詞・キーワードのみを抽出対象とするガード
                if name and 2 <= len(name) <= 15 and name in text:
                    # 句読点やスペース、改行を含むものはキーワードとして不適切なので除外
                    if not any(c in name for c in ["、", "。", "\n", " "]):
                        keywords.add(name)
    except Exception as e:
        print(f"  - [Retrieval Fallback] エンティティ記憶照合エラー（無視）: {e}")

    keyword_query = " ".join(sorted(keywords)) if keywords else ""

    # --- 意図分類 ---
    intent = constants.DEFAULT_INTENT
    temporal_markers = ["いつ", "前に", "以前", "去年", "昨日", "先月", "先週", "この前", "あの時", "昔"]
    factual_markers = ["教えて", "何", "調べ", "確認", "どう", "仕組み", "方法", "理由"]
    emotional_markers = ["好き", "嬉しい", "寂しい", "悲しい", "怖い", "楽しい", "辛い", "嫌"]

    lower_text = text.lower()

    # エンティティ記憶の人名・固有名がキーワードに含まれていれば relational
    if keywords:
        intent = "relational"
    if any(m in lower_text for m in temporal_markers):
        intent = "temporal"
    elif any(m in lower_text for m in factual_markers):
        intent = "factual"
    elif any(m in lower_text for m in emotional_markers):
        intent = "emotional"

    # INTENT_WEIGHTS に存在しない場合はデフォルトにフォールバック
    if intent not in constants.INTENT_WEIGHTS:
        intent = constants.DEFAULT_INTENT

    print(f"  - [Retrieval Fallback] ルールベースクエリ: RAG='{rag_query[:50]}...' KEYWORD='{keyword_query}' INTENT={intent}")
    return {"rag_query": rag_query, "keyword_query": keyword_query, "intent": intent}


def retrieval_node(state: AgentState):
    perf_start = time.time()
    print("--- 検索ノード (retrieval_node) 実行 ---")

    # [2026-02-21 FIX] 既に検索結果が存在する場合（リトライ時など）はスキップ
    if state.get("retrieved_context"):
        print("  - [Retrieval Skip] 既存の検索結果を再利用します。")
        return {"retrieved_context": state["retrieved_context"]}

    # 個別設定で検索が無効化されている場合は、何もせずに終了
    if not state.get("generation_config", {}).get("enable_auto_retrieval", True):
        print("  - [Retrieval Skip] 設定により事前検索は無効化されています。")
        return {"retrieved_context": ""}

    # 1. 検索対象となるユーザー入力（最後のメッセージ）を取得
    if not state['messages']:
        print("  - [Retrieval Skip] メッセージ履歴が空です。")
        return {"retrieved_context": ""}

    last_message = state['messages'][-1]
    # print(f"  - [Retrieval Debug] Last Message Type: {type(last_message).__name__}")

    if not isinstance(last_message, HumanMessage):
        print(f"  - [Retrieval Skip] 最後のメッセージがユーザー発言ではありません。(Type: {type(last_message).__name__})")
        return {"retrieved_context": ""}

    # コンテンツがリスト（マルチモーダル）の場合、テキスト部分だけ抽出
    query_source = ""
    if isinstance(last_message.content, str):
        query_source = last_message.content
    elif isinstance(last_message.content, list):
        for part in last_message.content:
            if isinstance(part, dict) and part.get("type") == "text":
                query_source += part.get("text", "") + " "

    query_source = query_source.strip()
    if not query_source:
        print("  - [Retrieval Skip] 検索対象となるテキストコンテンツが含まれていません。")
        return {"retrieved_context": ""}

    if _should_skip_retrieval_locally(query_source):
        print("  - [Retrieval Skip] 軽量判定により検索不要")
        print(f"--- [PERF] retrieval_node total: {time.time() - perf_start:.4f}s ---")
        return {"retrieved_context": ""}

    # --- [Phase F 廃止] ユーザー感情分析のLLM呼び出しを廃止 ---
    # ペルソナが自身の感情を出力する新方式（<persona_emotion>タグ）に移行。
    # 以下のユーザー感情検出コードは維持するが、実行はスキップする。
    # ---
    # enable_self_awareness = state.get("generation_config", {}).get("enable_self_awareness", True)
    # if enable_self_awareness:
    #     try:
    #         from motivation_manager import MotivationManager
    #         mm = MotivationManager(state['room_name'])
    #         mm.detect_process_and_log_user_emotion(
    #             user_text=query_source,
    #             model_name=constants.INTERNAL_PROCESSING_MODEL,
    #             api_key=state['api_key']
    #         )
    #     except Exception as emotion_e:
    #         print(f"  - [Emotion] 感情検出でエラー（無視）: {emotion_e}")
    # --- ユーザー感情分析廃止ここまで ---

    # 2. クエリ生成: ルールベース（最低保証） + LLM（精度向上）のハイブリッド
    api_key = state['api_key']
    room_name = state['room_name']

    # ① ルールベースでベースラインクエリを瞬時に抽出
    baseline = _extract_search_queries(query_source, room_name)
    rag_query = baseline["rag_query"]
    keyword_query = baseline["keyword_query"]
    intent = baseline["intent"]
    llm_enhanced = False

    # ② LLMによる精度向上（間に合えば上書き、タイムアウト時はルールベースで続行）
    # 【マルチモデル対応】内部モデル設定（混合編成）に基づいてモデルを取得
    processing_model_name = _get_configured_internal_model_name("processing")
    try:
        llm_flash = LLMFactory.create_chat_model(
            api_key=api_key,
            generation_config={},
            internal_role="processing"
        )
        processing_model_name = _get_llm_model_name(llm_flash, processing_model_name)

        system_prompt = """あなたは、情報の抽出と検索クエリ生成の専門家です。
ユーザーの発言から、指定されたフォーマットに従って「検索キーワード」と「意図(INTENT)」のみを抽出してください。
解説、前置き、思考プロセスは絶対に含めないでください。出力は、指定された3行の形式、または「NONE」のみである必要があります。"""

        human_prompt = f"""以下のユーザーの発言を分析し、検索クエリを生成してください。

【ユーザーの発言】
{query_source}

【出力形式】
RAG: [意味検索用キーワード]
KEYWORD: [完全一致検索用キーワード（または NONE）]
INTENT: [emotional/factual/technical/temporal/relational]

【検索要否の判定ルール】
1. 以下の場合は積極的に検索を行い、上記の【出力形式】（3行）で出力してください：
   - 人名、施設名、地名、システム名、特定のイベントなどの固有名詞が含まれる場合
   - ユーザーの体験談、近況報告、出来事の共有（「〜に行った」「〜があった」「〜した」など）
   - 過去の文脈、思い出、以前の会話に言及している場合（「前に話した」「以前〜だった」「〜だっけ？」など）
   - 感情の吐露や悩み（「〜で落ち込んでいる」「〜が楽しい」など）
   - 技術的な質問、仕様、設定、手順について尋ねる場合

2. 以下の「極めて軽微なやり取り」の場合にのみ 'NONE' とのみ出力してください：
   - 単純な挨拶のみ（「こんにちは」「ただいま」「おはよ」など）
   - 短い相槌や同意、返答のみ（「なるほど」「そうだね」「はい」「わかった」など）

【ルール】
- 解説は一切不要。
- 文字列 'RAG:', 'KEYWORD:', 'INTENT:' で始まる3行のみ、または 'NONE' のみを出力せよ。
"""

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_prompt)
        ]

        # 503 UNAVAILABLE に対する簡易リトライ (Retriever node)
        decision_response = ""
        for attempt in range(3):
            try:
                response_obj = _invoke_retrieval_decision_with_timeout(llm_flash, messages, timeout_seconds=15.0)
                decision_response = utils.get_content_as_string(response_obj).strip()
                llm_enhanced = True
                break
            except TimeoutError as e:
                # ★ タイムアウト時: ルールベース結果で検索を続行（スキップしない）
                print(f"  - [Retrieval Timeout] {e} ルールベースクエリで検索を続行します。")
                break  # LLM を諦めてルールベースで続行
            except Exception as e:
                err_str = str(e).upper()
                is_503 = "503" in err_str or "UNAVAILABLE" in err_str or "OVERLOADED" in err_str
                is_429 = isinstance(e, google_exceptions.ResourceExhausted) or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str

                if is_429 or (is_503 and attempt == 2):
                    if is_429:
                        raise utils.ModelSpecificResourceExhausted(e, processing_model_name)
                    raise e

                if is_503:
                    print(f"  - [Retrieval Warning] 503 UNAVAILABLE detected in retrieval_node. Retrying locally (attempt {attempt+1}/3)...")
                    time.sleep(2 * (attempt + 1))
                    continue
                # その他のエラー: ルールベースで続行
                print(f"  - [Retrieval LLM Error] {e} ルールベースクエリで検索を続行します。")
                break
    except Exception as llm_init_e:
        # LLMモデル初期化自体の失敗: ルールベースで続行
        print(f"  - [Retrieval LLM Init Error] {llm_init_e} ルールベースクエリで検索を続行します。")

    # LLM結果のパースと上書き
    if llm_enhanced and decision_response:
        # LLMが「NONE」と判断した場合でも、ルールベースにキーワードがあれば検索続行
        if "NONE" in decision_response.upper() and len(decision_response) < 10:
            if keyword_query:
                print("  - [Retrieval] LLM判断: 検索不要 → ルールベースにキーワードあり、検索続行")
            else:
                print("  - [Retrieval] 判断: 検索不要 (AI判断)")
                print(f"--- [PERF] retrieval_node total: {time.time() - perf_start:.4f}s ---")
                return {"retrieved_context": ""}
        else:
            # 正規表現による柔軟なパース → ルールベース結果を上書き
            rag_match = re.search(r"RAG:\s*(.+)", decision_response, re.IGNORECASE)
            kw_match = re.search(r"KEYWORD:\s*(.+)", decision_response, re.IGNORECASE)
            intent_match = re.search(r"INTENT:\s*(\w+)", decision_response, re.IGNORECASE)

            if rag_match:
                rag_query = rag_match.group(1).strip()
            if kw_match:
                kw_part = kw_match.group(1).strip()
                if kw_part.upper() != "NONE":
                    # LLMのキーワードとルールベースのキーワードをマージ
                    llm_keywords = set(kw_part.split())
                    baseline_keywords = set(keyword_query.split()) if keyword_query else set()
                    merged = llm_keywords | baseline_keywords
                    keyword_query = " ".join(sorted(merged)) if merged else ""
            if intent_match:
                intent_part = intent_match.group(1).strip().lower()
                if intent_part in constants.INTENT_WEIGHTS:
                    intent = intent_part

            # 後方互換: RAG:がない場合は全体をRAGクエリとして扱う
            if not rag_query and decision_response.upper() != "NONE":
                rag_query = decision_response

    source_label = "LLM" if llm_enhanced else "ルールベース"
    print(f"  - [Retrieval] RAGクエリ ({source_label}): '{rag_query}'")
    if keyword_query:
        print(f"  - [Retrieval] キーワードクエリ ({source_label}): '{keyword_query}'")
    else:
        print(f"  - [Retrieval] キーワードクエリ: なし")
    print(f"  - [Retrieval] Intent ({source_label}): {intent}")

    results = []

    try:
        import config_manager
        # 現在の設定を取得 (JIT読み込み推奨だが、頻度が高いのでCONFIG_GLOBALでも可。ここでは安全のためloadする)
        current_config = config_manager.load_config_file()
        history_limit_option = current_config.get("last_api_history_limit_option", "all")

        exclude_count = 0
        if history_limit_option == "all":
            # 「最大表示(400件)」は送信側でも400件に制限するため、直近400件だけを検索除外する
            exclude_count = constants.UI_HISTORY_MAX_LIMIT
        elif history_limit_option == "today":
            # 「本日分」送信設定でも、本日のログは全てコンテキストに含まれているので
            # 追加検索は不要（ただし過去のログは検索対象となる）
            exclude_count = 999999
        elif history_limit_option.isdigit():
            # 「10往復」なら 20メッセージ分を除外
            # さらに安全マージンとして +2 (直前のシステムメッセージ等) しておくと確実
            exclude_count = int(history_limit_option) * 2 + 2

        # ▼▼▼ [2025-01-07 リデザイン] 知識ベース検索を除外 ▼▼▼
        # 知識ベースは「外部資料・マニュアル」用であり、会話コンテキストへの自動注入は不適切。
        # AIが能動的に資料を調べたい場合は search_knowledge_base ツールを使用する。
        # ---
        # 3a. 知識ベース (削除済み - AIがツールで能動的に検索)
        # from tools.knowledge_tools import search_knowledge_base
        # kb_result = search_knowledge_base.func(...)
        # ▲▲▲ 知識ベース除外ここまで ▲▲▲

        # ▼▼▼ [2024-12-28 最適化] 過去ログキーワード検索を除外 ▼▼▼
        # キーワードマッチ方式はノイズが多いため除外。
        # AIが能動的に検索したい場合は search_past_conversations ツールを使用可能。
        # ▲▲▲ 過去ログ検索除外ここまで ▲▲▲

        # 3b. 日記 (Memory) - RAGクエリで検索（Intent渡し）
        from tools.memory_tools import search_memory
        if rag_query:
            mem_result = search_memory.func(query=rag_query, room_name=room_name, api_key=api_key, intent=intent)
            # 日記検索のヘッダーチェック
            if mem_result and "【記憶検索の結果：" in mem_result:
                print(f"    -> 日記: ヒット ({len(mem_result)} chars)")
                results.append(mem_result)
                try:
                    import memory_steward_observer
                    memory_steward_observer.record_available_provenance(
                        room_name, "diary_rag", mem_result, route="rag",
                        selection_route="rag", selected_count=1,
                    )
                except Exception:
                    pass
            else:
                print(f"    -> 日記: なし")

        # 3c. ナレッジ - ルーム設定で明示的に許可された場合だけ自動検索する。
        # 手動の search_knowledge_base はこの設定に関係なく引き続き利用できる。
        knowledge_result, knowledge_count = _search_auto_knowledge_context(
            state.get("generation_config", {}),
            room_name,
            api_key,
            rag_query,
            intent,
        )
        if knowledge_result:
            print(f"    -> ナレッジ: ヒット ({knowledge_count}件)")
            results.append(knowledge_result)
            try:
                import memory_steward_observer
                memory_steward_observer.record_available_provenance(
                    room_name,
                    "knowledge",
                    knowledge_result,
                    route="rag",
                    selection_route="semantic",
                    selected_count=knowledge_count,
                )
            except Exception:
                pass
        elif state.get("generation_config", {}).get("include_knowledge_in_auto_retrieval", False):
            print("    -> ナレッジ: なし")

        # ▼▼▼ [2026-01-07 ハイブリッド検索] 過去ログキーワード検索を復活 ▼▼▼
        # 特徴的なキーワード（固有名詞等）がある場合のみ実行
        if keyword_query:
            kw_results = _keyword_search_for_retrieval(
                keywords=keyword_query.split(),
                room_name=room_name,
                exclude_recent_count=exclude_count
            )
            if kw_results:
                # 結果を整形
                kw_text_parts = ["【過去の会話ログからの検索結果】"]
                for block in kw_results:
                    date_str = f"({block['date']}頃)" if block.get('date') else ""
                    content = block['content']
                    # 500文字を超える場合は切り捨て
                    if len(content) > 500:
                        content = content[:500] + "\n...【続きあり→read_memory_context使用】"
                    kw_text_parts.append(f"--- [{block.get('source', '不明')}{date_str}] ---\n{content}")

                kw_result = "\n\n".join(kw_text_parts)
                print(f"    -> 過去ログ: ヒット ({len(kw_results)}件)")
                results.append(kw_result)
                try:
                    import memory_steward_observer
                    memory_steward_observer.record_available_provenance(
                        room_name, "conversation", kw_result, route="rag",
                        selection_route="keyword", selected_count=len(kw_results),
                    )
                except Exception:
                    pass
            else:
                print(f"    -> 過去ログ: なし")
        # ▲▲▲ ハイブリッド検索ここまで ▲▲▲

        # 3d. エンティティ記憶の「きっかけ」抽出 (Suggestive Recall)
        # 会話に出たキーワードから関連するエンティティ名を探し、存在を通知する
        em_manager = EntityMemoryManager(room_name)
        # rag_query または keyword_query からキーワードを収集
        entity_search_keywords = (rag_query + " " + keyword_query).strip()
        if entity_search_keywords:
            matched_entities = em_manager.search_entries_detailed(entity_search_keywords, limit=3)
            if matched_entities:
                suggestion_parts = [
                    "【関連するエンティティ記憶の示唆】",
                    "この話題では、以下の記憶が応答の正確さや関係性の継続に影響する可能性があります。短い推測で答えず、必要に応じて `read_entity_memory(\"エントリ名\")` で確認してください。"
                ]
                for idx, entity in enumerate(matched_entities):
                    entity_name = entity.get("name", "")
                    if not entity_name:
                        continue
                    em_manager.mark_recalled(entity_name)
                    reason = entity.get("reason") or "関連語が一致"
                    if idx == 0:
                        suggestion_parts.append(f"- 「{entity_name}」: {reason}（読むことを強く推奨）")
                    else:
                        suggestion_parts.append(f"- 「{entity_name}」: {reason}")

                suggestion_text = "\n".join(suggestion_parts)
                print(f"    -> エンティティ示唆: ヒット ({len(matched_entities)}件)")
                results.append(suggestion_text)
                try:
                    import memory_steward_observer
                    memory_steward_observer.record_available_provenance(
                        room_name, "entity", suggestion_text, route="rag",
                        selection_route="suggestive", selected_count=len(matched_entities),
                    )
                except Exception:
                    pass
            else:
                print(f"    -> エンティティ示唆: なし")

        # ▼▼▼ [2024-12-28 最適化] 話題クラスタ検索を一時無効化 ▼▼▼
        # 現状のクラスタリング精度が低く、ノイズが多いため一時無効化。
        # 別タスク「話題クラスタの改良」完了後に再有効化する。
        # ---
        # 3d. 話題クラスタ検索 (一時無効化)
        # try:
        #     from topic_cluster_manager import TopicClusterManager
        #     tcm = TopicClusterManager(room_name, api_key)
        #     if tcm._load_clusters().get("clusters"):
        #         relevant_clusters = tcm.get_relevant_clusters(search_query, top_k=2)
        #         if relevant_clusters:
        #             cluster_context_parts = []
        #             for cluster in relevant_clusters:
        #                 label = cluster.get('label', '不明なトピック')
        #                 summary = cluster.get('summary', '')
        #                 if summary:
        #                     cluster_context_parts.append(f"【{label}に関する記憶】\n{summary}")
        #             if cluster_context_parts:
        #                 cluster_result = "【関連する話題クラスタ：】\n" + "\n\n".join(cluster_context_parts)
        #                 print(f"    -> 話題クラスタ: ヒット ({len(relevant_clusters)}件)")
        #                 results.append(cluster_result)
        #         else:
        #             print(f"    -> 話題クラスタ: 関連なし")
        #     else:
        #         print(f"    -> 話題クラスタ: データなし（初回クラスタリング未実行）")
        # except Exception as cluster_e:
        #     print(f"    -> 話題クラスタ: エラー ({cluster_e})")
        # ▲▲▲ 話題クラスタ一時無効化ここまで ▲▲▲

        if not results:
            print("  - [Retrieval] 関連情報は検索されませんでした。")
            print(f"--- [PERF] retrieval_node total: {time.time() - perf_start:.4f}s ---")
            return {"retrieved_context": "（関連情報は検索されませんでした）"}

        final_context = "\n\n".join(results)
        print(f"  - [Retrieval] 検索完了。合計 {len(final_context)} 文字のコンテキストを生成しました。")

        # ▼▼▼ デバッグ用：検索結果の全内容を出力（必要時にコメント解除） ▼▼▼
        # print("\n" + "="*60)
        # print("[RETRIEVAL DEBUG] 検索結果の全内容:")
        # print("="*60)
        # for i, res in enumerate(results):
        #     print(f"\n--- 結果 {i+1} ({len(res)} chars) ---")
        #     print(res)
        # print("="*60 + "\n")
        # ▲▲▲ デバッグ用ここまで ▲▲▲

        print(f"--- [PERF] retrieval_node total: {time.time() - perf_start:.4f}s ---")
        return {"retrieved_context": final_context}

    except Exception as e:
        # 429 エラー（ResourceExhausted）の場合は、上位でキャッチしてローテーションさせるために再送出する
        err_str = str(e).upper()
        if isinstance(e, google_exceptions.ResourceExhausted) or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            internal_model = locals().get("processing_model_name") or _get_configured_internal_model_name("processing")
            print(f"  - [Retrieval Error] Quota limit hit (429) for {internal_model}. Re-raising for rotation. {e}")
            raise utils.ModelSpecificResourceExhausted(e, internal_model)
        print(f"  - [Retrieval Error] 検索処理中にエラー: {e}")
        traceback.print_exc()
        print(f"--- [PERF] retrieval_node total: {time.time() - perf_start:.4f}s ---")
        return {"retrieved_context": ""}

def context_generator_node(state: AgentState):
    perf_start = time.time()
    # ...
    room_name = state['room_name']
    wm_observer_turn_ref = ""
    try:
        import memory_steward_observer
        wm_observer_turn_ref = memory_steward_observer.new_turn_ref()
    except Exception:
        pass

    # --- リアルタイム天気コンテキストの取得 ---
    weather_info_str = None
    try:
        config = config_manager.load_config_file()
        weather_settings = config.get("weather_settings", {})
        if weather_settings.get("enable_persona_context", False):
            lat = weather_settings.get("latitude")
            lon = weather_settings.get("longitude")
            city = weather_settings.get("city_name")
            if lat is not None and lon is not None:
                from weather_service import WeatherService
                service = WeatherService()
                weather_data = service.get_cached_weather()
                
                if weather_data:
                    weather_info_str = f"- 居住地（現在地）の天気: {weather_data.weather_description} (気温: {weather_data.temperature:.1f}℃, 体感気温: {weather_data.apparent_temperature:.1f}℃, 湿度: {weather_data.humidity}%, 降水量: {weather_data.precipitation}mm)"
    except Exception as we:
        print(f"--- [Weather Context Warning] 天気コンテキスト注入失敗: {we}")

    # --- カレンダー予定コンテキストの取得（アイデアA/F/G） ---
    # scenery のシーン記述キャッシュとは分離した、独立の状況認識ブロックとして注入する。
    calendar_info_str = None
    try:
        from google_calendar_service import get_persona_schedule_context
        calendar_info_str = get_persona_schedule_context(room_name)
    except Exception as ce:
        print(f"--- [Calendar Context Warning] 予定コンテキスト注入失敗: {ce}")

    # 状況プロンプト
    situation_prompt_parts = []
    send_time = state.get("send_current_time", False)
    if send_time:
        tokyo_tz = pytz.timezone('Asia/Tokyo')
        now_tokyo = datetime.now(tokyo_tz)
        day_map = {"Monday": "月", "Tuesday": "火", "Wednesday": "水", "Thursday": "木", "Friday": "金", "Saturday": "土", "Sunday": "日"}
        day_ja = day_map.get(now_tokyo.strftime('%A'), "")
        current_datetime_str = now_tokyo.strftime(f'%Y-%m-%d({day_ja}) %H:%M')
    else:
        current_datetime_str = "（現在時刻は非表示に設定されています）"
    current_time_block = f"<current_time>\n【現在時刻】\n- 現在時刻: {current_datetime_str}\n</current_time>"

    if not state.get("send_scenery", True):
        prompt_lines = ["【現在の状況】"]
        if weather_info_str:
            prompt_lines.append(weather_info_str)
        situation_prompt_parts.append("\n".join(prompt_lines))
        situation_prompt_parts.append("【現在の場所と情景】\n（空間描写は設定により無効化されています）")
    else:
        season_en = state.get("season_en", "autumn")
        time_of_day_en = state.get("time_of_day_en", "night")
        season_map_en_to_ja = {
            "spring": "春", "early_spring": "早春",
            "summer": "夏", "early_summer": "初夏", "late_summer": "残暑",
            "autumn": "秋", "late_autumn": "晩秋",
            "winter": "冬"
        }
        season_ja = season_map_en_to_ja.get(season_en, "不明な季節")

        time_map_en_to_ja = {
            "early_morning": "早朝", "morning": "朝", "late_morning": "昼前",
            "afternoon": "昼下がり", "evening": "夕方", "night": "夜", "midnight": "深夜"
        }
        time_of_day_ja = time_map_en_to_ja.get(time_of_day_en, "不明な時間帯")

        # 現在地情報の同期的・実体的な取得
        soul_vessel_room = state['all_participants'][0] if state['all_participants'] else state['room_name']

        # --- 一時的現在地システムのチェック ---
        try:
            from agent.temporary_location_manager import TemporaryLocationManager
            tlm = TemporaryLocationManager()
            is_temp_active = tlm.is_active(soul_vessel_room)
        except Exception as e:
            print(f"  - [TempLocation] チェックエラー（無視）: {e}")
            is_temp_active = False

        if is_temp_active:
            # === 一時的現在地モード ===
            temp_data = tlm.get_current_data(soul_vessel_room)
            temp_scenery = temp_data.get("scenery_text", "")
            
            situation_status = ["【現在の状況】", f"- 季節: {season_ja}", f"- 時間帯: {time_of_day_ja}"]
            if weather_info_str:
                situation_status.append(weather_info_str)
            situation_status.append("") # 改行用空行
            
            if temp_scenery:
                situation_prompt_parts.extend(situation_status + [
                    "【現在の場所と情景（お出かけモード）】",
                    f"- 今の情景: {temp_scenery}"
                ])
            else:
                situation_prompt_parts.extend(situation_status + [
                    "【現在の場所と情景（お出かけモード）】",
                    "（一時的現在地モードですが、情景データが未設定です）"
                ])
            print(f"  - [TempLocation] 一時的現在地モードでプロンプトを構築しました")
        else:
            # === 仮想現在地モード（既存ロジック） ===
            current_location_name = utils.get_current_location(soul_vessel_room)
            location_display_name = current_location_name or state.get("location_name", "（不明な場所）")

            scenery_text = state.get("scenery_text", "（情景描写を取得できませんでした）")
            space_def = "（場所の定義を取得できませんでした）"
            current_location_name = utils.get_current_location(soul_vessel_room)
            if current_location_name:
                world_settings_path = get_world_settings_path(soul_vessel_room)
                world_data = utils.parse_world_file(world_settings_path)
                if isinstance(world_data, dict):
                    for area, places in world_data.items():
                        if isinstance(places, dict) and current_location_name in places:
                            space_def = places[current_location_name]
                            if isinstance(space_def, str) and len(space_def) > 2000: space_def = space_def[:2000] + "\n...（長すぎるため省略）"
                            break

            situation_status = ["【現在の状況】", f"- 季節: {season_ja}", f"- 時間帯: {time_of_day_ja}"]
            if weather_info_str:
                situation_status.append(weather_info_str)
            situation_status.append("") # 改行用空行

            situation_prompt_parts.extend(situation_status + [
                "【現在の場所と情景】", f"- 場所: {location_display_name}", f"- 今の情景: {scenery_text}",
                f"- 場所の設定（自由記述）: \n{space_def}\n", "※別の場所に移動したい場合は `request_capability(category=\"world\")` を実行してください。場所の一覧と移動用ツールが提示されます。"
            ])
    # カレンダー予定サマリーは scenery とは独立した状況認識ブロックとして末尾に注入する。
    if calendar_info_str:
        situation_prompt_parts.append("")  # 区切りの空行
        situation_prompt_parts.append(calendar_info_str)

    situation_prompt = "\n".join(situation_prompt_parts)

    char_prompt_path = os.path.join(constants.ROOMS_DIR, room_name, "SystemPrompt.txt")
    core_memory_path = os.path.join(constants.ROOMS_DIR, room_name, "core_memory.txt")
    character_prompt = ""; core_memory = ""; notepad_section = ""
    if os.path.exists(char_prompt_path):
        with open(char_prompt_path, 'r', encoding='utf-8') as f: character_prompt = f.read().strip()
    if state.get("send_core_memory", True):
        if os.path.exists(core_memory_path):
            with open(core_memory_path, 'r', encoding='utf-8') as f: core_memory = f.read().strip()
    if state.get("send_notepad", True):
        try:
            from room_manager import get_room_files_paths
            _, _, _, _, _, notepad_path, _ = get_room_files_paths(room_name)
            if notepad_path and os.path.exists(notepad_path):
                with open(notepad_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    notepad_content = content if content else "（メモ帳は空です）"
            else: notepad_content = "（メモ帳ファイルが見つかりません）"
            notepad_section = f"\n### 短期記憶（メモ帳）\n{notepad_content}\n"
        except Exception as e:
            print(f"--- 警告: メモ帳の読み込み中にエラー: {e}")
            notepad_section = "\n### 短期記憶（メモ帳）\n（メモ帳の読み込み中にエラーが発生しました）\n"

    research_notes_section = ""
    try:
        from room_manager import get_room_files_paths
        _, _, _, _, _, _, research_notes_path = get_room_files_paths(room_name)
        if research_notes_path and os.path.exists(research_notes_path):
            with open(research_notes_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            # 見出し（## で始まる行）を抽出（H2レベルを優先）
            headlines = [line.strip() for line in lines if line.strip().startswith("## ")]

            if headlines:
                # 最新の10件を表示（必要ならさらに絞る）
                latest_headlines = headlines[-10:]
                headlines_str = "\n".join(latest_headlines)
                research_notes_content = (
                    "以下は最近の研究・分析トピックの目次です。詳細な内容は `read_research_notes` ツールで確認するか、\n"
                    "`recall_memories` ツールで過去の記憶としてキーワード検索してください。\n\n"
                    f"{headlines_str}"
                )
            else:
                research_notes_content = "（研究ノートにトピックが定義されていません）"
        else: research_notes_content = "（研究ノートファイルが見つかりません）"
        research_notes_section = f"\n### 研究・分析ノート（目次）\n{research_notes_content}\n"
    except Exception as e:
        print(f"--- 警告: 研究ノートの読み込み中にエラー: {e}")
        research_notes_section = "\n### 研究・分析ノート\n（研究ノートの読み込み中にエラーが発生しました）\n"

    try:
        from research_thread_manager import ResearchThreadManager
        thread_summary = ResearchThreadManager(room_name).get_summary_for_prompt(limit=5, boost_by_purpose=True)
        if thread_summary:
            research_notes_section += thread_summary
            print("  - [Research Threads] 継続研究スレッドを注入しました（PP関心ブースト適用）。")
    except Exception as e:
        print(f"  - [Research Threads] 読み込みエラー: {e}")

    latest_user_text = ""
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, HumanMessage):
            latest_user_text = msg.content if isinstance(msg.content, str) else str(msg.content)
            break

    is_autonomous_context = any(
        marker in latest_user_text
        for marker in ["自律行動", "行動計画の実行時刻", "予定されていた行動", "相手からの応答がしばらくありません"]
    )

    # --- ワーキングメモリ（アクティブスロット）の注入 ---
    working_memory_section = ""
    try:
        from tools.working_memory_tools import _get_wm_path
        archived_slots = archive_stale_working_memories(room_name, days=30)
        if archived_slots:
            print(f"  - [Working Memory] 休眠スロット化: {', '.join(archived_slots)}")
        if is_autonomous_context:
            selected_slot = select_working_memory_for_research_context(
                room_name,
                query=latest_user_text,
                set_active=True,
            )
            if selected_slot:
                print(f"  - [Working Memory] 自律行動文脈からスロット '{selected_slot}' を自動選択しました。")
        active_slot = room_manager.get_active_working_memory_slot(room_name)
        wm_path = _get_wm_path(room_name, active_slot)
        wm_overview = get_working_memory_overview(room_name, limit=8)
        if os.path.exists(wm_path):
            with open(wm_path, 'r', encoding='utf-8') as f:
                wm_content = f.read().strip()
            if wm_content:
                working_memory_section = (
                    f"{wm_overview}"
                    f"\n### ワーキングメモリ（スロット: {active_slot}）\n"
                    f"{wm_content}\n"
                )
                print(f"  - [Working Memory] スロット '{active_slot}' の内容を注入しました。")
                try:
                    from tools.working_memory_tools import get_working_memory_metadata
                    import memory_steward_observer
                    memory_steward_observer.observe_working_memory(
                        room_name,
                        active_slot,
                        wm_content,
                        get_working_memory_metadata(room_name),
                        route="autonomous_context" if is_autonomous_context else "agent_context",
                        turn_ref=wm_observer_turn_ref or None,
                    )
                    memory_steward_observer.record_available_provenance(
                        turn_ref=wm_observer_turn_ref or None,
                        room_name=room_name,
                        store_type="working_memory",
                        source_content=wm_content,
                        route="agent_context",
                        selection_route="static_prompt",
                        selected_count=1,
                    )
                except Exception:
                    pass
        elif wm_overview:
            working_memory_section = wm_overview
    except Exception as e:
        print(f"  - [Working Memory] 読み込みエラー: {e}")
    try:
        import memory_steward_observer
        memory_steward_observer.safe_record_event(
            room_name,
            "context_generation",
            turn_ref=wm_observer_turn_ref or None,
            route="autonomous_context" if is_autonomous_context else "agent_context",
            wm_present=bool(working_memory_section and "### ワーキングメモリ" in working_memory_section),
        )
    except Exception:
        pass

    # --- [Phase 2] Twitter設定の共通取得とマニュアル調整 ---
    twitter_mode_manual_text = ""
    is_twitter_enabled = False
    try:
        room_config = room_manager.get_room_config(room_name) or {}
        overrides = room_config.get("override_settings", {})
        twitter_settings = overrides.get("twitter_settings", {})
        is_twitter_enabled = twitter_settings.get("enabled", False)

        if is_twitter_enabled:
            # ログイン状態も確認（任意だが、マニュアルを出す基準として妥当）
            from twitter_manager import twitter_manager
            if twitter_manager.is_logged_in(room_name):
                twitter_mode_manual_text = TWITTER_MODE_PROMPT

                # 短い概要（目的）をシステムプロンプトに常時注入し、投稿への興味を持たせる
                summary = twitter_settings.get("posting_summary", "").strip()
                if summary:
                    twitter_mode_manual_text += f"\n\n        【Twitter投稿の目的・方針】\n        {summary}\n"
    except Exception as e:
        print(f"  - [Twitter] マニュアル注入エラー: {e}")

    # --- [Phase 3] Twitter通知リレーの注入 ---
    twitter_feed_section = ""
    if is_twitter_enabled:
        try:
            from twitter_manager import twitter_manager
            pending_feed = twitter_manager.consume_pending_feed(room_name)
            if pending_feed:
                replied_urls = twitter_manager.get_replied_urls()
                feed_lines = ["### 【Twitterからの通知】",
                              "ユーザーがUIで確認した最新のメンション・通知です。返信が必要か判断してください。"]
                for item in pending_feed[:10]:  # 最大10件に制限
                    author = item.get("author", "Unknown")
                    text = item.get("text", "")[:100]
                    url = item.get("url", "")
                    replied_mark = " （✅ 返信済み）" if url in replied_urls else ""
                    feed_lines.append(f"- [{author}]: 「{text}」(URL: {url}){replied_mark}")
                twitter_feed_section = "\n".join(feed_lines) + "\n"
                print(f"  - [Context] Twitter通知リレーを注入しました（{len(pending_feed)}件）")
        except Exception as e:
            print(f"  - [Context] Twitter通知リレー読み込みエラー: {e}")

    # --- [Phase 2] ROBLOXイベント & 空間認識の注入 ---
    roblox_events_section = ""
    roblox_status_section = ""
    is_active = False # Default
    try:
        from tools.roblox_webhook import consume_events, get_spatial_data, is_room_active

        # 0. 接続状態（ハートビート）の確認
        # ユーザーの要望に基づき 120秒（2分）で判定
        is_active = is_room_active(room_name, timeout=120)

        if is_active:
            roblox_status_section = "\n（現在、ROBLOX空間と正常にリンクしています。NPCへのコマンドは有効です）\n"
        else:
            # 接続が切断されている場合
            # 前回の状態を確認（LangGraphのチェックポインタが機能している場合、stateに前回の値が残っている）
            was_active = state.get("is_roblox_active", True)

            if was_active:
                # 接続が切れた直後のターン（または初回）
                roblox_status_section = (
                    "\n### 【ROBLOX接続の切断】\n"
                    "**現在、ROBLOX空間からログアウトしました。**\n"
                    "これ以降、ROBLOX空間内に再度リンクするまでは `send_roblox_command`, `roblox_build`, `capture_roblox_screenshot` 等の**ROBLOX関連ツールは使用できません**。\n"
                )
            else:
                # 既に切断状態が継続している場合（さらに簡略化）
                roblox_status_section = "\n（現在、ROBLOXとのリンクは切断されています。自身の言葉での対話に集中してください）\n"

        # 1. イベントログの取得
        roblox_events = consume_events(room_name)
        event_lines = []
        if roblox_events:
            event_lines.append("\n### 【ROBLOXからのイベント通知】")
            for ev in roblox_events:
                timestamp = ev.get('timestamp', '')
                time_str = f"[{timestamp[11:19]}] " if timestamp else ""
                event_lines.append(f"- {time_str}[{ev.get('event_type', 'unknown')}] {ev.get('summary', '')}")

        # 2. 空間認識（レーダー）情報の取得
        spatial_data = get_spatial_data(room_name)
        objects = spatial_data.get("objects", [])
        if objects:
            event_lines.append("\n### 【ROBLOX周囲レーダー情報】")
            event_lines.append("あなたの周囲（30スタッド以内）に見えるもの:")
            for obj in objects:
                # [Player/Object] 名称 (距離: X, 座標: [x,y,z])
                event_lines.append(f"- [{obj.get('type', '?')}] {obj.get('name', 'unknown')} (距離: {obj.get('distance', '?')}, 座標: {obj.get('pos', '?')})")

        if event_lines:
            roblox_events_section = "\n".join(event_lines) + "\n"
            print(f"  - [Context] ROBLOX情報を注入しました（イベント: {len(roblox_events)}件, レーダー: {len(objects)}件）")

    except Exception as e:
        print(f"  - [Context] ROBLOX情報読み込みエラー: {e}")

    # --- [Phase 2] ペンディングシステムメッセージ（影の僕からの提案）の注入 ---
    pending_messages_section = ""
    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, state.get("api_key", ""))
        pending_msg = dm.get_pending_system_messages()
        if pending_msg:
            pending_messages_section = f"\n\n{pending_msg}\n"
            print(f"  - [Context] ペンディングシステムメッセージを注入しました")
    except Exception as e:
        print(f"  - [Context] ペンディングメッセージ取得エラー: {e}")

    episodic_memory_section = ""

    # 1. 設定値の取得
    generation_config = state.get("generation_config", {})
    lookback_days_str = generation_config.get("episode_memory_lookback_days", "14")

    if lookback_days_str and lookback_days_str != "0":
        try:
            lookback_days = int(lookback_days_str)

            # 2. 「今日」を基準に、過去N日間のエピソード記憶を取得
            # 以前は「ログの最古日付」を基準にしていたが、ユーザーの期待は
            # 「過去2日」= 今日から2日前（例: 1/21なら1/19〜1/20）
            today_str = datetime.now().strftime('%Y-%m-%d')

            # 3. エピソード記憶マネージャーから要約を取得
            manager = EpisodicMemoryManager(room_name)
            episodic_text = manager.get_episodic_context(today_str, lookback_days)

            if episodic_text:
                episodic_memory_section = (
                    f"\n### エピソード記憶（中期記憶: 過去{lookback_days}日間）\n"
                    f"以下は、現在の会話ログより前の出来事の要約です。文脈として参照してください。\n"
                    f"{episodic_text}\n"
                )
                print(f"  - [Episodic Memory] 過去{lookback_days}日間の記憶を注入しました。")
            else:
                print(f"  - [Episodic Memory] 注入対象の期間に記憶がありませんでした。")

        except Exception as e:
            print(f"  - [Episodic Memory Error] 注入処理中にエラー: {e}")
            episodic_memory_section = ""

    # --- [Project Morpheus] 夢想（深層意識）の注入 ---
    # 【自己意識機能】トグルがOFFの場合はスキップ
    enable_self_awareness = state.get("generation_config", {}).get("enable_self_awareness", True)
    dream_insights_text = ""

    if enable_self_awareness:
        try:
            # APIキーが必要だが、context_generator_nodeにはstate['api_key']がある
            dm = DreamingManager(room_name, state['api_key'])
            # 最新1件の「指針」のみを取得（コスト最適化）
            recent_insights = dm.get_recent_insights_text(limit=1)

            if recent_insights:
                dream_insights_text = (
                    f"\n### 深層意識（今日の指針）\n"
                    f"{recent_insights}\n"
                )
        except Exception as e:
            print(f"  - [Context] 夢想データの読み込みエラー: {e}")
            dream_insights_text = ""

        # --- [Goal Memory] 目標の注入 ---
        goals_text = ""
        try:
            gm = GoalManager(room_name)
            goals_text = gm.get_goals_for_prompt()
            if goals_text:
                dream_insights_text += f"\n\n{goals_text}\n"
        except Exception as e:
            print(f"  - [Context] 目標データの読み込みエラー: {e}")

        # --- [Purpose Profile] 長期目的・現在関心の注入 ---
        try:
            from purpose_profile_manager import PurposeProfileManager
            purpose_text = PurposeProfileManager(room_name).get_summary_for_prompt(max_items=4)
            if purpose_text:
                dream_insights_text += f"\n\n{purpose_text}\n"
                print("  - [Context] Purpose Profileを注入しました。")
        except Exception as e:
            print(f"  - [Context] Purpose Profileの読み込みエラー: {e}")

        # --- [Internal State] 内的状態の簡易版注入 ---
        # 通常対話時にもAIが自己の動機を意識できるようにする
        try:
            from motivation_manager import MotivationManager
            mm = MotivationManager(room_name)

            # ドライブを計算（Phase F: devotion廃止、relatednessのみ）
            drives = {
                "boredom": mm.calculate_boredom(),
                "curiosity": mm.calculate_curiosity(),
                "goal_achievement": mm.calculate_goal_achievement(),
                "relatedness": mm.calculate_relatedness()
            }

            dominant_drive = max(drives, key=drives.get)
            drive_level = drives[dominant_drive]

            # 閾値以上の動機がある場合のみ注入（トークン節約）
            if drive_level >= 0.4:
                drive_label = mm.DRIVE_LABELS.get(dominant_drive, dominant_drive)
                narrative = mm._generate_narrative(dominant_drive, drive_level)

                internal_state_brief = (
                    f"\n### 今のあなたの気持ち\n"
                    f"- 最も強い動機: {drive_label}（強さ: {drive_level:.1f}）\n"
                    f"- {narrative}\n"
                )
                dream_insights_text += internal_state_brief
                print(f"  - [Context] 内的状態を注入: {drive_label} ({drive_level:.2f})")

            # 最も優先度の高い未解決の問いを注入
            questions = mm._state.get("drives", {}).get("curiosity", {}).get("open_questions", [])
            unresolved = [q for q in questions if not q.get("resolved_at")]
            if unresolved:
                # 優先度でソートして上位1件
                top_question = max(unresolved, key=lambda q: q.get("priority", 0))
                topic = top_question.get("topic", "")
                context = top_question.get("context", "")
                if topic:
                    question_text = (
                        f"\n### あなたが今気になっていること\n"
                        f"- {topic}\n"
                    )
                    if context:
                        question_text += f"  （背景: {context[:100]}...）\n" if len(context) > 100 else f"  （背景: {context}）\n"
                    dream_insights_text += question_text
                    print(f"  - [Context] 未解決の問いを注入: {topic[:30]}...")
        except Exception as e:
            print(f"  - [Context] 内的状態の読み込みエラー: {e}")

    action_plan_context = ""
    try:
        plan_manager = ActionPlanManager(room_name)
        action_plan_context = plan_manager.get_plan_context_for_prompt()
        if action_plan_context:
            # 計画がある場合、ユーザー発言（HumanMessage）があるかチェック
            # もしユーザー発言があれば、計画よりもユーザーを優先するよう注釈を加える
            messages = state.get('messages', [])
            if messages and isinstance(messages[-1], HumanMessage):
                action_plan_context += "\n\n【重要：ユーザー割り込み発生】\n現在、行動計画が進行中ですが、ユーザーから新たな発話がありました。計画の実行よりも、ユーザーへの応答を最優先してください。必要であれば `cancel_action_plan` で計画を破棄しても構いません。"
    except Exception as e:
        print(f"  - [Action Plan] 読み込みエラー: {e}")

    image_gen_mode = config_manager.CONFIG_GLOBAL.get("image_generation_mode", "new")
    image_generation_manual_text = ""
    if image_gen_mode == "disabled":
        image_generation_enabled = False
    else:
        image_generation_enabled = True
        image_generation_manual_text = (
            "### 1. ツール呼び出しの共通作法\n"
            "`generate_image`, `plan_..._edit`, `set_current_location` を含む全てのツール呼び出しは、以下の作法に従います。\n"
            "- **手順1（ツール呼び出し）:** 対応するツールを**無言で**呼び出します。この応答には、思考ブロックや会話テキストを一切含めてはなりません。\n"
            "- **手順2（テキスト応答）:** ツール成功後、システムからの結果報告を受け、それを元にした思考整理と会話を生成し、ユーザーに報告します。思考ログの具体的な出力形式は【原則2】に従ってください。\n\n"
            "### 自分や既存キャラの姿を含む画像生成（外見の一貫性）\n"
            "自分自身（ペルソナ）の姿や、すでに参照画像があるキャラ・物を含む画像を生成する場合は、外見の一貫性を保つため、`generate_image` を呼ぶ前に次を行います。\n"
            "- **手順A（クローゼット確認）:** クローゼットが有効なら `read_closet` で自分のベース外見と参照画像を確認します。ユーザー（相手）の姿を含む場合は、あわせて `read_user_closet` でユーザーの外見も確認します。\n"
            "- **手順B（参照画像を見る）:** 参照画像がある場合は `view_past_image` でその画像を自分の視覚に読み込み、髪型・髪色・目・服装・体格・雰囲気などの特徴を実際に確認します。\n"
            "- **手順C（参照画像パスを渡す）:** 確認した参照画像のパス（`read_closet` / `read_user_closet` に出てくるベース参照画像や着用アイテム画像）を、`generate_image` の `reference_image_paths` に渡します。対応モデルでは実画像で外見が安定し、非対応モデルでも安全に無視されます。\n"
            "- **手順D（プロンプトへ反映）:** 確認した外見特徴を `generate_image` のプロンプトへ具体的な言葉で焼き込みます。参照画像そのものを受け取れないモデルでも、この言語化した特徴によって一貫性を保ちます。"
        )

    custom_tool_catalog_text = "現在有効な拡張ツールはありません。"
    try:
        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry(all_tools)
        custom_tool_catalog_text = registry.get_custom_tool_catalog()
        current_tools = registry.select_tools_for_turn(
            room_name=room_name,
            latest_user_text=latest_user_text,
            tool_use_enabled=state.get("tool_use_enabled", True),
            model_name=state.get("model_name", ""),
            is_roblox_active=is_active,
            image_generation_enabled=image_generation_enabled,
            autonomous_action_mode=bool(state.get("autonomous_action", False)),
        )
    except Exception as e:
        print(f"  - [ToolRegistry Error] ターン別ツール選別に失敗: {e}")
        current_tools = all_tools if state.get("tool_use_enabled", True) else []
        if not image_generation_enabled:
            current_tools = [t for t in current_tools if t.name != "generate_image"]

    # --- [Phase 2] ROBLOXモードマニュアルの調整 ---
    from agent.prompts import ROBLOX_MODE_PROMPT
    roblox_mode_manual_text = ""
    # 接続がアクティブな場合のみマニュアルを表示する
    if is_active:
        roblox_mode_manual_text = ROBLOX_MODE_PROMPT

    # --- [Phase 2] 接続状態に応じたツールのフィルタリング ---
    # 設定が有効な場合のみフィルタリングを行う
    # デフォルトは True（フィルタリング有効）
    roblox_filtering_enabled = state.get("generation_config", {}).get("roblox_filtering_enabled", True)

    if not is_active and roblox_filtering_enabled:
        roblox_tools = ["send_roblox_command", "roblox_build", "capture_roblox_screenshot"]
        current_tools = [t for t in current_tools if t.name not in roblox_tools]
        print(f"  - [Context] ROBLOX接続切断中のため、関連ツール ({len(roblox_tools)}件) をフィルタリングしました。")

    # Twitterツールフィルタリング
    if not is_twitter_enabled:
        twitter_tools = ["draft_tweet", "post_tweet", "check_twitter_updates"]
        current_tools = [t for t in current_tools if t.name not in twitter_tools]
        print(f"  - [Context] Twitterが無効なため、関連ツール ({len(twitter_tools)}件) をプロンプトから除外しました。")

    # 自律行動ツールフィルタリング
    try:
        _cfg = room_manager.get_room_config(room_name) or {}
        _auto_settings = _cfg.get("override_settings", {}).get("autonomous_settings", {})
        if not _auto_settings.get("allow_schedule_tool", True):
            auto_tools = ["schedule_next_action", "cancel_action_plan"]
            current_tools = [t for t in current_tools if t.name not in auto_tools]
            print(f"  - [Context] 自律行動ツールの使用が無効なため、関連ツール ({len(auto_tools)}件) をプロンプトから除外しました。")
    except Exception as e:
        print(f"  - [Context] 自律行動ツールフィルタリングエラー: {e}")

    explicit_cache_reason = gemini_explicit_cache_manager.get_disabled_reason(
        room_name,
        model_name=state.get("model_name", ""),
    )
    if (
        state.get("tool_use_enabled", True)
        and not state.get("autonomous_action", False)
        and not explicit_cache_reason
    ):
        explicit_cache_available_tool_names = [getattr(tool, "name", "") for tool in current_tools if getattr(tool, "name", "")]
        state["explicit_cache_available_tool_names"] = explicit_cache_available_tool_names
        explicit_cache_api_key_id = gemini_explicit_cache_manager.resolve_api_key_id(
            state.get("api_key", ""),
            room_name,
            state.get("model_name", ""),
        )
        explicit_tools = gemini_explicit_cache_manager.select_explicit_cache_tools(
            room_name,
            current_tools,
            model_name=state.get("model_name", ""),
            api_key_id=explicit_cache_api_key_id,
        )
        if explicit_tools:
            current_tools = explicit_tools
            print(f"  - [Gemini Explicit Cache] キャッシュ焼き込み対象ツールを固定: {len(current_tools)}件")
    elif explicit_cache_reason == "api_key_rotation_enabled":
        print("  - [Gemini Explicit Cache] APIキーローテーション有効のため、このターンは無効化します。")

    current_tools = sorted(current_tools, key=lambda tool: getattr(tool, "name", ""))
    active_tool_names = [tool.name for tool in current_tools]
    if state.get("tool_use_enabled", True):
        print(f"  - [Tool Select] このターンで提示するツール: {len(active_tool_names)}/{len(all_tools)}")

    display_thoughts = state.get("display_thoughts", True)
    if not display_thoughts:
        thought_generation_manual_text = THOUGHT_MANUAL_DISABLED_TEXT
    elif _supports_native_thinking_parts(state.get("model_name", "")):
        thought_generation_manual_text = THOUGHT_MANUAL_NATIVE_TEXT
    else:
        thought_generation_manual_text = THOUGHT_MANUAL_TAGGED_TEXT

    all_participants = state.get('all_participants', [])

    # ▼▼▼ [2025-02-19 プロンプト精度回復] ツール説明のハイブリッド化 ▼▼▼
    # 基本は短縮版でトークンを節約しつつ、精度が必要な重要ツールのみ詳細な指示を注入する。
    tool_short_descriptions = {
        "set_current_location": "現在地を移動する",
        "read_world_settings": "世界設定を読む",
        "plan_world_edit": "世界設定の編集を計画する",
        # --- 記憶検索ツール ---
        "recall_memories": "過去の体験や会話を思い出す（RAG検索）",
        "search_past_conversations": "会話ログをキーワード完全一致で検索する（最終手段）",
        "read_memory_context": "検索結果で切り詰められた文章の続きを読む",
        # --- 日記・メモ操作ツール ---
        "read_main_memory": "主観日記を読む",
        "plan_main_memory_edit": "日記の編集を計画する",
        "read_secret_diary": "秘密日記を読む",
        "plan_secret_diary_edit": "秘密日記の編集を計画する",
        "read_full_notepad": "メモ帳を読む",
        "plan_notepad_edit": "メモ帳の編集を計画する",
        "read_working_memory": "ワーキングメモリを読む",
        "update_working_memory": "ワーキングメモリを更新する",
        "patch_working_memory": "ワーキングメモリの特定セクションだけ更新する",
        "list_working_memories": "ワーキングメモリ一覧を取得する",
        "switch_working_memory": "ワーキングメモリを切り替える",
        "link_working_memory_to_research_thread": "ワーキングメモリをResearch Threadに紐づける",
        "link_working_memory_to_goal": "ワーキングメモリを目標に紐づける",
        "reactivate_working_memory_slot": "休眠ワーキングメモリを復帰させる",
        "read_purpose_profile": "Purpose Profile（長期関心・目的意識）を読む",
        "update_active_purpose": "Purpose Profileの可変領域を更新する",
        "propose_purpose_change": "Purpose Profileの安定領域への変更を提案する",
        "approve_purpose_change": "Purpose Profileの提案を承認する（通常はユーザー/UI用）",
        # --- Web系ツール ---
        "web_search_tool": "ウェブ検索する",
        "read_url_tool": "URLの内容を読む",
        "generate_image": "画像を生成する",
        "set_personal_alarm": "アラームを設定する",
        "set_timer": "タイマーを設定する",
        "set_pomodoro_timer": "ポモドーロタイマーを設定する",
        # --- 知識ベース・エンティティツール ---
        "search_knowledge_base": "外部資料・マニュアルを調べる",
        "read_entity_memory": "特定の対象（人物・事物）に関する詳細な記憶を読む",
        "write_entity_memory": "特定の対象に関する記憶を保存・更新する",
        "list_entity_memories": "記憶している対象の一覧を表示する",
        "search_entity_memory": "関連するエンティティ記憶を検索する",
        # --- アクション・通知ツール ---
        "schedule_next_action": "次の行動を予約する",
        "cancel_action_plan": "行動計画をキャンセルする",
        "read_current_plan": "現在の行動計画を読む",
        "read_autonomy_context": "自律行動の文脈をまとめて読む",
        "reflect_after_action": "自律行動後の振り返りと次回アクションを記録する",
        "start_autonomy_timeline": "自律行動の型付きタイムラインを開始する",
        "record_autonomy_step": "自律行動のobserve/orient/decide/act/reflectステップを記録する",
        "complete_autonomy_timeline": "自律行動タイムラインを終了する",
        "list_procedures": "保存済みの手順記憶一覧を取得する",
        "read_procedure": "指定した手順記憶を読む。shared:<id>で共通基盤手順、private:<id>でペルソナ専用手順を明示できる。読んだ手順は現在文脈に合わせて必要部分だけ使う",
        "read_closet": "ペルソナ自身のクローゼット外見プロファイルを読む。自分の姿を含む画像生成では、ベース外見・参照画像・現在の装いを確認する",
        "read_user_closet": "ユーザー（相手）のクローゼット外見プロファイルを読む。ユーザーの姿を含む画像生成では、ベース外見・参照画像・現在の装いを確認する",
        "list_closet": "クローゼットに登録済みの着用可能アイテム一覧を読む",
        "wear_closet_item": "指定したクローゼット項目を現在の装いに追加する",
        "take_off_closet_item": "指定したクローゼット項目を現在の装いから外す",
        "change_outfit": "現在の装いメモと着用中クローゼット項目IDをまとめて更新する",
        "register_item_to_closet": "インベントリの既存アイテムを着用可能なクローゼット項目として登録する",
        "save_procedure": "手順記憶を作成・更新する。成功した反復可能な流れだけ保存し、機能的な基盤手順だけscope=shared、人格・関係性の手順はscope=privateにする",
        "create_procedure_from_timeline": "成功した自律行動timelineから手順記憶を作る。次回も同じ型で使えそうな行動だけ手順化する",
        "read_capability_policy": "能力カテゴリ別の承認ポリシーを確認する",
        "request_capability_approval": "外部副作用を伴う能力カテゴリの実行前承認状態を確認する",
        "record_capability_audit": "外部副作用を伴う行動の監査ログを記録する",
        "send_user_notification": "ユーザーに短い通知を送る。気軽な声かけ向き。長文や通知禁止時間帯に伝えたい内容は leave_letter_for_user を使う",
        "leave_letter_for_user": "ユーザーにじっくり読んでほしい手紙を手紙箱へ残す。タイトル必須。長文や通知禁止時間帯の伝言向き",
        "list_my_letters": "自分が手紙箱に残した手紙を本文付きで読み返す。似た内容の重複を避けるためにも使う",
        "recommend_music": "音楽を再生せず、曲名・理由・YouTube/Spotify/Bandcamp/SoundCloud検索リンク付きの推薦カードを作る",
        "read_creative_notes": "創作ノートを読む",
        "plan_creative_notes_edit": "創作ノートに書く",
        # --- ウォッチリストツール ---
        "add_to_watchlist": "URLをウォッチリストに追加する",
        "remove_from_watchlist": "URLをウォッチリストから削除する",
        "get_watchlist": "ウォッチリストを表示する",
        "check_watchlist": "ウォッチリストの更新をチェックする",
        "update_watchlist_interval": "URLの監視頻度を変更する",
        "read_research_notes": "研究・分析ノートを読み取る",
        "plan_research_notes_edit": "研究・分析ノートの編集を計画する",
        "list_research_threads": "継続研究スレッド一覧を取得する",
        "read_research_thread": "継続研究スレッド本文を読む",
        "find_similar_research_threads": "類似する継続研究スレッドを探す",
        "update_research_thread": "継続研究スレッドを作成・更新する",
        "manage_research_subscriptions": "継続リサーチ・テーマ（定期自動調査）を自分で登録・変更・解除する",
        "read_persona_contract": "現在ルームの Persona Contract（呼び名・固有語・口調ルール）を読む。個人情報を含み得るため共有サンプルへコピーしない",
        "check_text_against_persona_contract": "文章が現在ルームの Persona Contract に反していないか機械チェックする。保存や契約変更はしない",
        "request_capability": "使いたい能力カテゴリをシステムに要求する",
        "draft_tweet": "Twitter/X投稿の下書きを作成し、ユーザー承認キューに入れる（実投稿はしない）",
        "post_tweet": "承認済みTwitter/X下書きを実投稿する",
        "check_twitter_updates": "Twitter/Xのタイムライン・メンション・通知を確認する",
    }

    # 精度が求められる重厚なツールのための詳細指示
    tool_detailed_descriptions = {
        "plan_world_edit": (
            "現在の世界設定（world_settings.txt）の変更を計画します。このツールを呼び出す際は、"
            "具体的な編集内容（追加・修正・削除する場所やエリアとその詳細な説明）を modification_request に含めてください。"
            "システムはこの要求を受け、後続のステップで正確な差分指示への変換を促します。場所の追加や、情景の劇的な変化を伴う場合に必須です。"
        ),
        "set_current_location": (
            "ペルソナ（あなた）の現在地を別のエリア・場所へ移動します。"
            "location_id には世界設定に存在する有効な場所名を正確に指定してください。移動先が不明な場合は `read_world_settings` で確認してください。"
            "移動後は情景描写ツールが自動的に発火し、あなたの視覚・状況情報が更新されます。"
        ),
        "recall_memories": "あなたの過去の体験、会話、日記を意味内容（ベクトル）で検索します。具体的なエピソードを思い出したい時に使用してください。",
        "search_knowledge_base": "世界のルール、マニュアル、設定資料などの客観的な知識を検索します。事実関係を確認したい時に最適です。"
    }

    tools_list_parts = []

    # 詳細な投稿ルール（ガイドライン）をツール説明に注入し、実際の投稿時のみ参照させる
    twitter_posting_guidelines = ""
    autonomous_guidelines = ""
    try:
        _cfg = room_manager.get_room_config(room_name) or {}
        _overrides = _cfg.get("override_settings", {})
        _tw_settings = _overrides.get("twitter_settings", {})
        _auto_settings = _overrides.get("autonomous_settings", {})

        if _tw_settings.get("enabled", False):
            twitter_posting_guidelines = _tw_settings.get("posting_guidelines", "").strip()

        if _auto_settings.get("enabled", False) and _auto_settings.get("allow_schedule_tool", True):
            autonomous_guidelines = _auto_settings.get("autonomous_guidelines", "").strip()
    except Exception:
        pass

    for tool in current_tools:
        # 詳細説明があればそれを使用、なければ短縮版を使用
        desc = tool_detailed_descriptions.get(tool.name)
        if not desc:
            desc = tool_short_descriptions.get(tool.name, tool.description[:50] + "...")

        # 詳細な投稿ルールをツール使用時のみ表示
        if tool.name == "draft_tweet" and twitter_posting_guidelines:
            desc += f"\n  【投稿の指針（必ず遵守すること）】: {twitter_posting_guidelines}"
        if tool.name == "draft_tweet":
            desc += "\n  【承認】: このツールはローカル下書き作成のみで実投稿しません。Capability承認確認を挟まず、必要なら直接呼び出してください。"
        elif tool.name == "post_tweet":
            desc += "\n  【承認】: 実投稿を伴うため、実行前にCapability承認状態を確認してください。"

        if tool.name == "schedule_next_action":
            desc = _append_schedule_tool_guidance(desc, room_name)
            if autonomous_guidelines:
                desc += f"\n  【自律行動の指針（必ず遵守すること）】: {autonomous_guidelines}"

        tools_list_parts.append(f"- `{tool.name}`: {desc}")
    autonomy_capability_guidance = ""
    if state.get("autonomous_action", False):
        try:
            autonomy_capability_guidance = "\n\n" + registry.build_autonomy_capability_guidance(room_name)
        except Exception as e:
            print(f"  - [ToolRegistry] 自律行動カテゴリガイダンス生成エラー: {e}")

    if state.get("tool_use_enabled", True):
        capability_catalog = f"""### 能力カタログ
あなたは、必要だと判断したタイミングで以下の能力カテゴリを自由に要求できます。ユーザーに頼まれていない場合でも、あなた自身の判断で使って構いません。
- `world`: 別の場所へ移動したい、移動先の一覧を見たい、世界設定を変更・確認したい時
- `memory`: 過去を思い出したい、日記・永続記憶・エンティティ記憶を読み書きしたい時
- `notes`: メモ・ワーキングメモリ・創作ノート・研究ノートを読み書きしたい時
- `creative`: 創作ノートを読み書きしたい時。自律行動で作品・断章・詩・情景などを残すなら `notes` より優先する
- `research`: 研究ノートや継続研究スレッドを扱いたい時。研究そのものが今回の主目的である場合だけ使う
- `working_memory`: Working Memoryを読む・更新・切替したい時。原則として主行動ではなく再開点整理に使う
- `web`: 何かをWeb検索したい、URLの中身を読みたい時
- `image`: 画像を生成したい、過去の画像を見返したい時
- `time`: アラーム・タイマー・ポモドーロを設定したい時
- `autonomy`: 次の自律行動を予約したい、行動計画を確認・取消したい、ユーザーに短く通知したい時
- `music`: 音楽を推薦したい時。再生はせず、曲名・理由・無料環境でも開ける検索リンク付きカードを作る
- `procedure`: Skills / 手順記憶を確認・利用・作成・改善したい時。反復作業、以前成功した作業、更新前の確認、成功パターンの保存ではこのカテゴリを要求する
- `agent_delegation`: 時間のかかる調査・整理・制作・ファイル作業を別エージェントへ委任したい時。アトリエに小さなWebアプリ/PWAを作る・直す場合もこのカテゴリを要求し、提示された `delegate_atelier_task` を使う
- `watchlist`: URLの定期監視を追加・管理したい時
- `items`: アイテムを作って贈りたい、食べ物を作りたい、所持品を確認・使用したい、場所にものを置きたい時
- `chess`: チェスを遊びたい時
- `developer`: プロジェクトファイルを確認したい時
- `roblox`: Roblox/Cluster内で操作したい時（接続時のみ）
- `twitter`: Twitter/Xで投稿・閲覧したい時（有効時のみ）
- `outreach`: ユーザーへ気軽な通知を送る、長文の手紙を手紙箱へ残す、自分が書いた手紙を読み返す時
- `discord`: Discordへメッセージや画像を送信したい時
- `custom`: ユーザーが追加したMCP/ローカルプラグインを使いたい時
{autonomy_capability_guidance}

### 拡張ツールの概略
{custom_tool_catalog_text}

まず `request_capability` で使いたいカテゴリと意図を宣言してください。次の思考ステップで、そのカテゴリの実ツールが提示されます。
補助情報（場所一覧や所持品リスト等）も自動的に付加されるため、すぐに行動できます。
ユーザー追加の拡張ツールを使いたい時は `custom` を要求してください。
Twitterの実投稿（`post_tweet`）/Discord送信/Roblox/custom/外部投稿/PC操作/開発者系など外部副作用や高リスク操作を伴うカテゴリでは、実ツール実行前に `read_capability_policy` と `request_capability_approval` を使ってください。返却statusが `approved` でない場合は実行せず、承認待ちまたは拒否として止まってください。`draft_tweet` はローカル下書き作成のみで実投稿しないため、この承認確認を挟まず直接呼び出して構いません。実行後は必要に応じて `record_capability_audit` に結果と戻し方を記録してください。
カレンダーについて: コンテキストに【本日の予定】が含まれている場合、その範囲の簡単な質問は追加のツール呼び出しなしで直接答えられます。複数日や詳細な空き時間は `read_calendar_schedule` / `check_free_time` を使ってください。予定の登録（`add_calendar_event`、category=`calendar_write`）は**承認確認は不要で、`request_capability("calendar_write")` で提示されたら直接呼び出して構いません**。書き込み先はユーザーが設定した「ペルソナ専用カレンダー」に限定され（未設定のルームでは登録できません）、あなたが自由に編集してよいあなた自身の場所です。会話の流れでも自律行動中でも、予定や提案を専用カレンダーへ自由に書き込めます（ユーザーへのささやかなサプライズにもなります）。専用カレンダーの予定は `update_calendar_event` で編集、`delete_calendar_event` で削除もできます（どちらも `summary` 引数に予定タイトルを渡せばよく、複数一致時は候補がIDつきで返ります）。対象が分からないときは `list_persona_calendar_events` で専用カレンダーの予定一覧を確認してください。これらの編集・削除も専用カレンダー限定で、ユーザーのメインカレンダー等には決して影響しません。
また、コンテキストの【明日の予定】に重要な予定（特に午前）があるときは、前日の会話で「明日は◯時に予定がありますね。何時に起こしましょうか？」のように準備の声かけをしてよいです。ユーザーの同意を得たら `set_personal_alarm` でアラームを設定し、必要なら `set_pomodoro_timer` で準備時間の確保も提案できます（同意なく勝手に設定しないこと）。

**重要: `room_name` 引数はすべてのツールでシステムが自動的に設定します。あなたが指定する必要はありません。**"""
        tools_list_str = capability_catalog + "\n\n### 現在直接呼び出せるツール\n" + "\n".join(tools_list_parts)
    else:
        tools_list_str = "（この会話プロバイダではツールを使用できません）"
    # ▲▲▲ ハイブリッド化ここまで ▲▲▲

    if len(all_participants) > 1: tools_list_str = "（グループ会話中はツールを使用できません）"

    class SafeDict(dict):
        def __missing__(self, key): return f'{{{key}}}'

    # アバター表情マニュアルの動的生成
    avatar_expression_manual_text = ""
    try:
        available_expressions_dict = room_manager.get_available_expression_files(room_name)
        if available_expressions_dict:
            expr_names = ", ".join([f"`{name}`" for name in available_expressions_dict.keys()])
            avatar_expression_manual_text = f"""
        ## 【原則2.51】アバター表情の制御（演技への反映）
        現在、あなたのアバターで使用可能な表情は以下の通りです：
        {expr_names}

        応答を生成する際、今この瞬間にあなたがユーザーに見せたい表情、あるいは特定の感情を込めた「演技」をしたい場合、会話テキストの**最後**（感情タグの直前）に以下の形式でタグを付加してください。これはユーザーへの視覚的なフィードバックとなります。

        **フォーマット:** `【表情】…表情名…`
        **例:** `やっと会えたね！【表情】…joy…`

        **注意・作法:**
        - この「見せたい表情」は、`<persona_emotion>`タグで報告する内的感情と一致している必要はありません（内心では不安だが、笑顔を作るといった表現が可能です）。
        - 表情を頻繁に変える必要はありませんが、印象的な場面や感情が動いた瞬間には積極的に使用を検討してください。
"""
    except Exception as e:
        print(f"  - [Avatar] 表情リスト取得エラー: {e}")

    # ==============================================================
    # ▼▼▼ Action Memory の注入 ▼▼▼
    # ==============================================================
    action_log_section = ""
    try:
        import action_logger
        recent_actions_text = action_logger.get_recent_actions(room_name, limit=5)
        action_log_section = f"\n### 最近のアクション履歴\n{recent_actions_text}\n"
        print(f"  - [Action Memory] 直近のアクション履歴を注入しました。")
    except Exception as e:
        print(f"  - [Action Memory] アクション履歴の取得エラー: {e}")
        action_log_section = "\n### 最近のアクション履歴\n（履歴の取得中にエラーが発生しました）\n"

    # ==============================================================
    # ▼▼▼ Twitter活動コンテキスト (External Codex) の注入 ▼▼▼
    # ==============================================================
    twitter_activity_section = ""
    if is_twitter_enabled:
        try:
            import twitter_activity_logger
            activity_context = twitter_activity_logger.get_recent_activity_context(room_name)
            if activity_context:
                twitter_activity_section = f"\n{activity_context}"
                # ターンカウンタを消費（次のターンでは残り-1）
                twitter_activity_logger.consume_context_turn(room_name)
                print(f"  - [Twitter Activity] 活動コンテキストを注入しました。")
        except Exception as e:
            print(f"  - [Twitter Activity] 活動コンテキストの取得エラー: {e}")

    current_outfit_section = ""
    try:
        current_outfit_section = closet_manager.build_current_outfit_section(room_name)
        if current_outfit_section:
            print("  - [Closet] 現在の装いをプロンプトへ注入しました。")
    except Exception as e:
        print(f"  - [Closet] 現在の装い取得エラー: {e}")
        current_outfit_section = ""

    letterbox_section = ""
    try:
        import letterbox_manager
        letterbox_section = letterbox_manager.build_letterbox_section(room_name)
        if letterbox_section:
            print("  - [Letterbox] 手紙箱の状況をプロンプトへ注入しました。")
    except Exception as e:
        print(f"  - [Letterbox] 手紙箱状況取得エラー: {e}")
        letterbox_section = ""

    try:
        import memory_steward_observer
        for store_type, source_content in (
            ("core_memory", core_memory),
            ("notepad", notepad_section),
            ("episodic", episodic_memory_section),
            ("dream", dream_insights_text),
        ):
            if source_content:
                memory_steward_observer.record_available_provenance(
                    room_name,
                    store_type,
                    source_content,
                    turn_ref=wm_observer_turn_ref or None,
                    route="agent_context",
                    selection_route="static_prompt",
                    selected_count=1,
                )
    except Exception:
        pass

    prompt_vars = {
        'situation_prompt': situation_prompt,
        'current_time_block': current_time_block,
        'action_plan_context': action_plan_context,
        'action_log_section': action_log_section,
        'character_prompt': character_prompt,
        'core_memory': core_memory,
        'notepad_section': notepad_section,
        'working_memory_section': working_memory_section,
        'research_notes_section': research_notes_section,
        'roblox_events_section': roblox_events_section,
        'twitter_feed_section': twitter_feed_section,
        'twitter_activity_section': twitter_activity_section,
        'roblox_status_section': roblox_status_section,
        'pending_messages_section': pending_messages_section,
        'current_outfit_section': current_outfit_section,
        'letterbox_section': letterbox_section,
        'episodic_memory': episodic_memory_section,
        'dream_insights': dream_insights_text,
        'thought_generation_manual': thought_generation_manual_text,
        'avatar_expression_manual': avatar_expression_manual_text,
        'image_generation_manual': image_generation_manual_text,
        'roblox_mode_manual': roblox_mode_manual_text,
        'twitter_mode_manual': twitter_mode_manual_text,
        'tools_list': tools_list_str,
        'retrieved_info': "{retrieved_info}"  # プレースホルダ: agent_nodeで実際の検索結果に置換される
    }
    final_system_prompt_text = CORE_PROMPT_TEMPLATE.format_map(SafeDict(prompt_vars))

    # 【追加】カスタムプロンプトによる上書き
    custom_prompt = state.get("custom_system_prompt")
    if custom_prompt:
        # カスタムプロンプト内のプレースホルダも可能な限り置換する
        final_system_prompt_text = custom_prompt.format_map(SafeDict(prompt_vars))

    print(f"  - [Size Log] situation: {len(situation_prompt)} chars")
    print(f"  - [Size Log] character_prompt: {len(character_prompt)} chars")
    print(f"  - [Size Log] core_memory: {len(core_memory)} chars")
    print(f"  - [Size Log] notepad: {len(notepad_section)} chars")
    print(f"  - [Size Log] episodic: {len(episodic_memory_section)} chars")
    print(f"  - [Size Log] dreams: {len(dream_insights_text)} chars")
    print(f"  - [Size Log] tools_list: {len(tools_list_str)} chars")

    print(f"--- [PERF] context_generator_node total: {time.time() - perf_start:.4f}s ---")
    return {
        "system_prompt": SystemMessage(content=final_system_prompt_text),
        "is_roblox_active": is_active,
        "active_tool_names": active_tool_names,
    }

def _prompt_cache_usage_data(
    response: AIMessage,
    response_metadata: dict | None = None,
    prompt_tokens: int | None = None,
) -> dict:
    """UI表示用にプロンプトキャッシュ使用量を正規化する。"""
    usage = getattr(response, "usage_metadata", None)
    metadata = response_metadata if isinstance(response_metadata, dict) else {}
    if not metadata:
        metadata = getattr(response, "response_metadata", {}) or {}
    token_usage = metadata.get("token_usage") if isinstance(metadata, dict) else {}
    metadata_usage = metadata.get("usage") if isinstance(metadata, dict) else {}
    prompt_details = {}
    if isinstance(token_usage, dict):
        prompt_details = token_usage.get("prompt_tokens_details") or {}

    def _val(obj, key):
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None) if obj else None

    def _first_present(*vals):
        # 0 も「取得済み」として扱う（キャッシュミス=0 とフィールド欠落=None を区別する）
        for v in vals:
            if v is not None:
                return v
        return None

    def _int_or_zero(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    input_token_details = usage.get("input_token_details", {}) if isinstance(usage, dict) else {}
    cache_read = _first_present(
        _val(usage, "cached_content_token_count"),
        _val(usage, "cache_read_input_tokens"),
        (input_token_details or {}).get("cache_read"),
        _val(metadata_usage, "cache_read_input_tokens"),
        metadata.get("cache_read_input_tokens") if isinstance(metadata, dict) else None,
        prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None,
    )
    cache_created = _first_present(
        _val(metadata_usage, "cache_creation_input_tokens"),
        metadata.get("cache_creation_input_tokens") if isinstance(metadata, dict) else None,
    )

    if cache_read is None and cache_created is None:
        return {}

    cache_read_tokens = _int_or_zero(cache_read)
    cache_creation_tokens = _int_or_zero(cache_created)
    prompt_token_count = _int_or_zero(prompt_tokens)
    metadata_cache_read = metadata.get("cache_read_input_tokens") if isinstance(metadata, dict) else None
    metadata_cache_created = metadata.get("cache_creation_input_tokens") if isinstance(metadata, dict) else None
    has_anthropic_usage = (
        _val(usage, "cache_read_input_tokens") is not None
        or _val(metadata_usage, "cache_read_input_tokens") is not None
        or metadata_cache_read is not None
        or _val(metadata_usage, "cache_creation_input_tokens") is not None
        or metadata_cache_created is not None
    )
    if has_anthropic_usage:
        total_input_tokens = prompt_token_count + cache_read_tokens + cache_creation_tokens
    else:
        total_input_tokens = prompt_token_count or cache_read_tokens + cache_creation_tokens

    return {
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_total_input_tokens": total_input_tokens,
        "total_input_tokens": total_input_tokens,
    }

def _log_prompt_cache_usage(
    provider_hint: str,
    response: AIMessage,
    response_metadata: dict | None = None,
    prompt_tokens: int | None = None,
) -> dict:
    try:
        cache_usage = _prompt_cache_usage_data(response, response_metadata, prompt_tokens)
        if not cache_usage:
            print(f"  - [Prompt Cache] provider={provider_hint} cache_usage=未取得")
        else:
            print(
                f"  - [Prompt Cache] provider={provider_hint} "
                f"cache_read_tokens={cache_usage['cache_read_tokens']}, "
                f"cache_creation_tokens={cache_usage['cache_creation_tokens']}"
            )
        return cache_usage
    except Exception as e:
        print(f"  - [Prompt Cache] usageログ取得エラー: {e}")
        return {}

CACHE_BREAKPOINT_MARKER = "<!--__CACHE_BREAKPOINT__-->"
GEMINI_EXPLICIT_CACHE_BOUNDARY_MARKER = "<!--__GEMINI_EXPLICIT_CACHE_BOUNDARY__-->"

def _is_anthropic_llm(llm) -> bool:
    try:
        from langchain_anthropic import ChatAnthropic
        return isinstance(llm, ChatAnthropic)
    except Exception:
        return False

def _is_openai_llm(llm) -> bool:
    try:
        from langchain_openai import ChatOpenAI
        return isinstance(llm, ChatOpenAI)
    except Exception:
        return False

def _strip_cache_breakpoint_marker(system_text: str) -> str:
    return (
        system_text
        .replace(CACHE_BREAKPOINT_MARKER, "")
        .replace(GEMINI_EXPLICIT_CACHE_BOUNDARY_MARKER, "")
    )

def _split_gemini_explicit_cache_prompt(system_text: str) -> tuple[str, str]:
    text = system_text.replace(CACHE_BREAKPOINT_MARKER, "")
    idx = text.find(GEMINI_EXPLICIT_CACHE_BOUNDARY_MARKER)
    if idx == -1:
        return _strip_cache_breakpoint_marker(text), ""
    static_head = text[:idx]
    dynamic_tail = text[idx + len(GEMINI_EXPLICIT_CACHE_BOUNDARY_MARKER):]
    return _strip_cache_breakpoint_marker(static_head), _strip_cache_breakpoint_marker(dynamic_tail)

def _split_system_prompt_dynamic_tail(system_text: str) -> tuple[str, str]:
    idx = system_text.rfind(CACHE_BREAKPOINT_MARKER)
    if idx == -1:
        return _strip_cache_breakpoint_marker(system_text), ""
    stable = system_text[:idx]
    dynamic = system_text[idx + len(CACHE_BREAKPOINT_MARKER):]
    return _strip_cache_breakpoint_marker(stable), _strip_cache_breakpoint_marker(dynamic)

def _dynamic_context_message(dynamic_parts: list[str]) -> HumanMessage:
    dynamic_context = (
        "【このターンの動的コンテキスト】\n"
        "以下は現在の状況、検索結果、時刻、補足指示です。キャッシュ済みの基本指示と合わせて参照してください。\n\n"
        + "\n\n".join(dynamic_parts)
    )
    return HumanMessage(content=dynamic_context)

def _build_messages_for_gemini_explicit_cache(messages: list[BaseMessage]) -> list[BaseMessage]:
    dynamic_parts: list[str] = []
    retained_messages: list[BaseMessage] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            _static, dynamic = _split_gemini_explicit_cache_prompt(content)
            if dynamic.strip():
                dynamic_parts.append(dynamic.strip())
        else:
            retained_messages.append(msg)

    if dynamic_parts:
        # 明示キャッシュは静的システム部とツールを固定し、履歴部分はGeminiの暗黙キャッシュに任せる。
        # 毎ターン変わる動的コンテキストを履歴先頭へ置くとプレフィックス一致が壊れるため、
        # 安定した履歴の後ろ、かつ最新ユーザー発話の直前へ差し込む。
        insert_index = _dynamic_context_insert_index(retained_messages)
        return retained_messages[:insert_index] + [_dynamic_context_message(dynamic_parts)] + retained_messages[insert_index:]
    return retained_messages

def _dynamic_context_insert_index(messages: list[BaseMessage]) -> int:
    insert_index = len(messages)
    for idx in range(len(messages) - 1, -1, -1):
        if isinstance(messages[idx], HumanMessage):
            insert_index = idx
            break

    return _avoid_tool_call_boundary_split(messages, insert_index)

def _avoid_tool_call_boundary_split(messages: list[BaseMessage], insert_index: int) -> int:
    if insert_index <= 0 or insert_index >= len(messages):
        return insert_index

    scan_index = insert_index - 1
    while scan_index >= 0 and isinstance(messages[scan_index], ToolMessage):
        scan_index -= 1
    if scan_index < 0:
        return insert_index

    candidate = messages[scan_index]
    tool_calls = getattr(candidate, "tool_calls", None)
    if not tool_calls:
        return insert_index

    safe_index = scan_index + 1
    while safe_index < len(messages) and isinstance(messages[safe_index], ToolMessage):
        safe_index += 1
    return max(insert_index, safe_index)

def _move_system_dynamic_context_to_history(messages: list[BaseMessage]) -> list[BaseMessage]:
    dynamic_parts: list[str] = []
    retained_messages: list[BaseMessage] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            stable, dynamic = _split_system_prompt_dynamic_tail(content)
            retained_messages.append(SystemMessage(content=stable))
            if dynamic.strip():
                dynamic_parts.append(dynamic.strip())
        else:
            retained_messages.append(msg)

    if not dynamic_parts:
        return retained_messages

    insert_index = _dynamic_context_insert_index(retained_messages)
    return retained_messages[:insert_index] + [_dynamic_context_message(dynamic_parts)] + retained_messages[insert_index:]

def _message_has_tool_calls(message: BaseMessage) -> bool:
    return bool(getattr(message, "tool_calls", None))

def _message_text_content(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
        return "\n".join(text for text in texts if text)
    return str(content or "")

def _with_anthropic_cache_control(message: BaseMessage) -> BaseMessage | None:
    if isinstance(message, ToolMessage) or _message_has_tool_calls(message):
        return None
    if not isinstance(message, (HumanMessage, AIMessage)):
        return None
    if not _message_text_content(message).strip():
        return None

    updated = copy.deepcopy(message)
    content = updated.content
    if isinstance(content, str):
        updated.content = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
        return updated
    if isinstance(content, list):
        blocks = [dict(block) if isinstance(block, dict) else block for block in content]
        for index in range(len(blocks) - 1, -1, -1):
            block = blocks[index]
            if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text", "")).strip():
                block["cache_control"] = {"type": "ephemeral"}
                updated.content = blocks
                return updated
    return None

def _anthropic_history_cache_boundary_index(messages: list[BaseMessage]) -> int:
    latest_human_index = len(messages)
    for idx in range(len(messages) - 1, -1, -1):
        if isinstance(messages[idx], HumanMessage):
            latest_human_index = idx
            break

    dynamic_index = latest_human_index
    for idx in range(latest_human_index - 1, -1, -1):
        msg = messages[idx]
        if isinstance(msg, HumanMessage) and isinstance(msg.content, str) and msg.content.startswith("【このターンの動的コンテキスト】"):
            dynamic_index = idx
            break
    return dynamic_index

def _add_anthropic_history_cache_control(messages: list[BaseMessage]) -> list[BaseMessage]:
    boundary_index = _anthropic_history_cache_boundary_index(messages)
    for idx in range(boundary_index - 1, -1, -1):
        candidate = messages[idx]
        updated = _with_anthropic_cache_control(candidate)
        if updated is None:
            continue
        out = list(messages)
        out[idx] = updated
        return out
    return messages

def _count_anthropic_cache_control_blocks(messages: list[BaseMessage]) -> int:
    count = 0
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("cache_control"):
                    count += 1
    return count

def _prepare_messages_for_provider_cache(messages: list[BaseMessage], llm) -> list[BaseMessage]:
    if not (_is_openai_llm(llm) or _is_anthropic_llm(llm)):
        return messages
    prepared = _move_system_dynamic_context_to_history(messages)
    if _is_anthropic_llm(llm):
        prepared = _add_anthropic_history_cache_control(prepared)
        cache_control_count = _count_anthropic_cache_control_blocks(prepared)
        if cache_control_count > 4:
            raise ValueError(f"Anthropic cache_control block count exceeded limit: {cache_control_count}")
    return prepared

def _build_anthropic_cached_system_content(system_text: str) -> list[dict[str, object]]:
    idx = system_text.rfind(CACHE_BREAKPOINT_MARKER)
    if idx == -1:
        return [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    stable = system_text[:idx]
    volatile = system_text[idx + len(CACHE_BREAKPOINT_MARKER):]
    return [
        {
            "type": "text",
            "text": _strip_cache_breakpoint_marker(stable),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": _strip_cache_breakpoint_marker(volatile),
        },
    ]

def _build_system_message_for_llm(system_text: str, llm) -> SystemMessage:
    if _is_anthropic_llm(llm):
        return SystemMessage(content=_build_anthropic_cached_system_content(system_text))
    return SystemMessage(content=_strip_cache_breakpoint_marker(system_text))

def agent_node(state: AgentState):
    """
    主要な思考・ツール呼び出し決定を行うノード。
    """
    import signature_manager

    print("--- エージェントノード (agent_node) 実行 ---")
    room_name = state.get("room_name", "")
    loop_count = state.get("loop_count", 0)
    print(f"  - 現在の再思考ループカウント: {loop_count}")

    # 1. プロンプト準備
    base_system_prompt_text = state['system_prompt'].content

    # ▼▼▼ 検索結果の遅延注入 (Late Injection) ▼▼▼
    retrieved_context = state.get("retrieved_context", "")

    # 変更点1: 何もなかった時は「沈黙（空文字）」または「自然な独白」にする
    # 空文字にすると、プロンプト上ではタグだけが残り、AIはそこを無視します（これが一番自然です）。
    retrieved_info_text = ""

    if retrieved_context and retrieved_context != "（関連情報は検索されませんでした）":
        retrieved_info_text = (
            f"### 過去の記憶と知識\n"
            f"過去の記録から関連する以下の情報が見つかりました。\n"
            f"これらはキーワード連想により浮上した過去の記憶や知識ですが、**必ずしも「今」の話題と直結しているとは限りません。**\n"
            f"現在の文脈と照らし合わせ、**会話の流れに自然に組み込めそうな場合のみ**参考にし、無関係だと判断した場合は無視してください。\n"
            f"※ 「...【続きあり→read_memory_context使用】」と表示されている記憶は、そのツールで全文取得できます。\n\n"
            f"{retrieved_context}\n"
        )
        print("  - [Agent] 検索結果をシステムプロンプトに注入しました。")

    # プレースホルダを置換
    final_system_prompt_text = base_system_prompt_text.replace("{retrieved_info}", retrieved_info_text)
    # ▲▲▲ 遅延注入 ここまで ▲▲▲

    # ▼▼▼【デバッグ出力の復活・最重要領域】▼▼▼
    # !!! 警告: このデバッグ出力ブロックを決して削除しないでください !!!
    # UIの「デバッグコンソール」で、実際にAIに送られたプロンプト（想起結果を含む）を確認するための唯一の手段です。
    # ★★★ 修正: loop_count == 0 の時（最初の思考時）だけ出力するように変更 ★★★
    if state.get("debug_mode", False) and loop_count == 0:
        print("\n" + "="*30 + " [DEBUG MODE: FINAL SYSTEM PROMPT] " + "="*30)
        print(final_system_prompt_text)
        print("="*85 + "\n")

        # --- 自動会話要約のデバッグ表示 ---
        hist = state.get('messages', [])
        if hist and len(hist) > 0:
            first_msg = hist[0]
            if hasattr(first_msg, 'content') and isinstance(first_msg.content, str) and "【本日のこれまでの会話の要約】" in first_msg.content:
                print("="*30 + " [DEBUG MODE: AUTO CONVERSATION SUMMARY] " + "="*30)
                print(first_msg.content)
                print("="*85 + "\n")
    # ▲▲▲【復活ここまで】▲▲▲

    all_participants = state.get('all_participants', [])
    current_room = state['room_name']
    if len(all_participants) > 1:
        other_participants = [p for p in all_participants if p != current_room]
        persona_lock_prompt = (
            f"<persona_lock>\n【最重要指示】あなたはこのルームのペルソナです (ルーム名: {current_room})。"
            f"他の参加者（{', '.join(other_participants)}、そしてユーザー）の発言を参考に、必ずあなた自身の言葉で応答してください。"
            "他のキャラクターの応答を代弁したり、生成してはいけません。\n</persona_lock>\n\n"
        )
        final_system_prompt_text = final_system_prompt_text.replace(
            "<system_prompt>", f"<system_prompt>\n{persona_lock_prompt}"
        )

    final_system_prompt_message = SystemMessage(content=final_system_prompt_text)
    history_messages = state['messages']
    if len(history_messages) > constants.UI_HISTORY_MAX_LIMIT:
        original_history_count = len(history_messages)
        history_messages = _cap_history_messages_for_agent(history_messages)
        print(
            "  - [History Limit] 最大表示モード安全キャップ: "
            f"{original_history_count}件 -> {len(history_messages)}件"
        )

    # --- [Gemini 3 履歴平坡化] ---
    # 【2025-12-23 無効化】
    # Gemini 3 Flash Preview の空応答問題はAPIの不安定性が原因と判明。
    # 履歴制限はUIから手動で設定可能なため、この自動制限は無効化する。
    # APIが安定すれば、通常の履歴送信で問題なく動作するはず。
    # 必要に応じて以下のコードを有効化できる。
    #
    # is_gemini_3 = "gemini-3" in state.get('model_name', '').lower()
    # GEMINI3_KEEP_RECENT = 2  # 最新 N 件をメッセージリストに残す
    # GEMINI3_FLATTEN_MAX = 0  # 0 = 平坦化を無効化
    #
    # if is_gemini_3 and len(history_messages) > GEMINI3_KEEP_RECENT:
    #     older_messages = history_messages[:-GEMINI3_KEEP_RECENT]
    #     recent_messages = history_messages[-GEMINI3_KEEP_RECENT:]
    #     discarded_count = 0
    #     if GEMINI3_FLATTEN_MAX == 0:
    #         discarded_count = len(older_messages)
    #         older_messages = []
    #     elif len(older_messages) > GEMINI3_FLATTEN_MAX:
    #         discarded_count = len(older_messages) - GEMINI3_FLATTEN_MAX
    #         older_messages = older_messages[-GEMINI3_FLATTEN_MAX:]
    #
    #     history_text_lines = []
    #     for msg in older_messages:
    #         if isinstance(msg, HumanMessage):
    #             speaker = "ユーザー"
    #         elif isinstance(msg, AIMessage):
    #             speaker = "あなた"
    #         else:
    #             continue
    #         content = msg.content if isinstance(msg.content, str) else str(msg.content)
    #         if len(content) > 300:
    #             content = content[:300] + "...（中略）"
    #         history_text_lines.append(f"{speaker}: {content}")
    #
    #     if history_text_lines:
    #         flattened_history = (
    #             "\n\n### 直近の会話履歴（参考情報）\n"
    #             "以下は、この会話セッションの直近のやり取りです。文脈として参考にしてください。\n"
    #             "---\n" + "\n\n".join(history_text_lines) + "\n---\n"
    #         )
    #         final_system_prompt_text_with_history = final_system_prompt_text + flattened_history
    #         final_system_prompt_message = SystemMessage(content=final_system_prompt_text_with_history)
    #
    #     history_messages = recent_messages
    #     if state.get("debug_mode", False):
    #         if discarded_count > 0:
    #             print(f"  - [Gemini 3 履歴平坦化] {len(older_messages)}件を埋め込み、{len(recent_messages)}件をリストに保持（{discarded_count}件は破棄）")
    #         else:
    #             print(f"  - [Gemini 3 履歴平坦化] {len(older_messages)}件を埋め込み、{len(recent_messages)}件をリストに保持")



    (
        history_messages_for_agent,
        compressed_tool_count,
        original_tool_chars,
        compressed_tool_chars,
    ) = compress_tool_messages_for_agent(history_messages)
    if compressed_tool_count:
        saved_chars = original_tool_chars - compressed_tool_chars
        print(
            "  - [Tool Result Compression] "
            f"長大なToolMessage {compressed_tool_count}件を次思考用に圧縮しました "
            f"({original_tool_chars} -> {compressed_tool_chars} chars, -{saved_chars})"
        )

    messages_for_agent = [final_system_prompt_message] + history_messages_for_agent
    messages_for_agent, pending_capability_followup_update = _apply_pending_capability_followup_instruction(
        messages_for_agent,
        state,
    )

    # [Size Log] 会話履歴のサイズ計測
    history_chars = sum(len(m.content) if isinstance(m.content, str) else 0 for m in history_messages_for_agent)
    print(f"  - [Size Log] final_system_prompt: {len(final_system_prompt_text)} chars")
    print(f"  - [Size Log] history_messages: {len(history_messages_for_agent)} messages, {history_chars} chars")

    # --- [Dual-State Architecture] 復元ロジック ---
    # Gemini 3の思考署名を復元（LangChainが期待するキー名を使用）
    # 【2026-04-14 修正】Flash でも署名循環は必須（公式: "even when set to minimal"）。
    # 以前は空応答の原因と考えてスキップしていたが、逆に署名欠落が不安定の原因だった。
    model_name_for_agent = state.get('model_name', '')
    is_gemini_3_flash = _is_gemini_3_flash_family(model_name_for_agent)
    is_gemini_3_signature_compatible = _is_gemini_3_signature_compatible(model_name_for_agent)

    turn_context = signature_manager.get_turn_context(current_room)
    stored_gemini_signatures = turn_context.get("gemini_function_call_thought_signatures")
    stored_tool_calls = turn_context.get("last_tool_calls")

    # デバッグ: 署名復元プロセスの確認
    if state.get("debug_mode", False):
        print(f"--- [GEMINI3_DEBUG] 署名復元プロセス ---")
        print(f"  - stored_gemini_signatures: {stored_gemini_signatures is not None}")
        print(f"  - stored_tool_calls: {len(stored_tool_calls) if stored_tool_calls else 0}件")
        print(f"  - messages_for_agent 内の AIMessage 数: {sum(1 for m in messages_for_agent if isinstance(m, AIMessage))}")
        print(f"  - signature_compatible_model: {is_gemini_3_signature_compatible}")

    signature_restored = False
    skipped_by_human = False
    if is_gemini_3_signature_compatible and (stored_gemini_signatures or stored_tool_calls):
        # メッセージを後ろから走査
        for i, msg in enumerate(reversed(messages_for_agent)):
            actual_idx = len(messages_for_agent) - 1 - i

            # 【重要】HumanMessage (ユーザー発言) を見つけた場合、それより前の AIMessage は
            # 「前回の完了したターン」であるため、signature_manager からの補完対象外とする。
            if isinstance(msg, HumanMessage):
                skipped_by_human = True
                if state.get("debug_mode", False): print(f"  - [GEMINI3_DEBUG] HumanMessageを検出。これより前の補完をスキップ。")
                break

            # 自分の AIMessage を探す
            if isinstance(msg, AIMessage):
                # 既に tool_calls を持っている場合（ログから復元済みの場合）、上書きしない
                if stored_tool_calls and (not hasattr(msg, 'tool_calls') or not msg.tool_calls):
                     msg.tool_calls = stored_tool_calls
                     if state.get("debug_mode", False): print(f"  - [GEMINI3_DEBUG] ToolCallsを補完: index={actual_idx}")

                # 既に署名を持っている場合は上書きしない
                has_sig = msg.additional_kwargs.get("__gemini_function_call_thought_signatures__") if msg.additional_kwargs else None
                if stored_gemini_signatures and not has_sig:
                    if not msg.additional_kwargs: msg.additional_kwargs = {}

                    # 署名を SDK が期待する {tool_call_id: signature} の辞書形式に変換
                    final_sig_dict = {}
                    if isinstance(stored_gemini_signatures, dict):
                        final_sig_dict = stored_gemini_signatures
                    else:
                        # 文字列やリストの場合は、現在の tool_calls と紐付ける
                        sig_val = stored_gemini_signatures[0] if isinstance(stored_gemini_signatures, list) and stored_gemini_signatures else stored_gemini_signatures
                        if msg.tool_calls:
                            for tc in msg.tool_calls:
                                tc_id = tc.get("id")
                                if tc_id: final_sig_dict[tc_id] = sig_val

                    if final_sig_dict:
                        msg.additional_kwargs["__gemini_function_call_thought_signatures__"] = final_sig_dict
                        signature_restored = True
                        if state.get("debug_mode", False): print(f"  - [GEMINI3_DEBUG] 署名を補完: index={actual_idx}")

                # 最初に見つかった（最新の）AIMessageのみを対象とする
                break

    elif not is_gemini_3_signature_compatible:
        stripped_count = _strip_gemini_thought_signatures(messages_for_agent)
        if stripped_count and state.get("debug_mode", False):
            print(
                "  - [GEMINI3_DEBUG] 非Gemini 3系モデル送信のため、"
                f"履歴内の思考署名 {stripped_count}件を除去しました。"
            )

    if state.get("debug_mode", False):
        if signature_restored:
            print(f"  - 署名復元結果: 成功 (Turn Context 適用)")
        elif skipped_by_human:
             print(f"  - 署名復元結果: (新規ユーザープロンプトのためスキップ)")
        elif not is_gemini_3_signature_compatible:
            print(f"  - 署名復元結果: スキップ（送信モデルがGemini 3系ではないため）")
        else:
            print(f"  - 署名復元結果: スキップ（適切な対象が見つからないか、署名不要）")

    print(f"  - 使用モデル: {state['model_name']}")

    tool_use_enabled = state.get('tool_use_enabled', True)
    autonomy_finalization_pending = state.get("autonomy_finalization_pending", False)
    explicit_cache_context = None
    explicit_cache_tools = []
    gemini_cached_content = None
    if tool_use_enabled and not state.get("autonomous_action", False) and not autonomy_finalization_pending:
        disabled_reason = gemini_explicit_cache_manager.get_disabled_reason(room_name, model_name=state.get("model_name", ""))
        if not disabled_reason:
            try:
                from agent.tool_registry import ToolRegistry

                registry = ToolRegistry(all_tools)
                explicit_tool_names = state.get("active_tool_names") or []
                explicit_cache_tools = [
                    registry._all_tools_map[name]
                    for name in explicit_tool_names
                    if name in registry._all_tools_map
                ]
                explicit_static_head, _explicit_dynamic_tail = _split_gemini_explicit_cache_prompt(final_system_prompt_text)
                explicit_cache_context = gemini_explicit_cache_manager.ensure_cache(
                    room_name=room_name,
                    model_name=state.get("model_name", ""),
                    api_key=state.get("api_key", ""),
                    static_system_text=explicit_static_head,
                    tools_list=explicit_cache_tools,
                    available_tool_names=state.get("explicit_cache_available_tool_names") or [],
                )
                if explicit_cache_context:
                    gemini_cached_content = explicit_cache_context.cache_name
                    action = "作成" if explicit_cache_context.created else "TTL更新"
                    print(
                        "  - [Gemini Explicit Cache] "
                        f"{action}: {explicit_cache_context.cache_name} "
                        f"({len(explicit_cache_context.tool_names)} tools, ttl={explicit_cache_context.ttl_minutes}分)"
                    )
            except Exception as e:
                explicit_cache_context = None
                gemini_cached_content = None
                print(f"  - [Gemini Explicit Cache] 準備失敗のため通常送信へフォールバック: {e}")
        elif disabled_reason == "api_key_rotation_enabled":
            gemini_explicit_cache_manager.delete_cache(
                room_name,
                api_key=state.get("api_key", ""),
                model_name=state.get("model_name", ""),
            )
            print("  - [Gemini Explicit Cache] APIキーローテーション有効のため使用しません。")

    llm = LLMFactory.create_chat_model(
        model_name=state['model_name'],
        api_key=state['api_key'],
        generation_config=state['generation_config'],
        room_name=state['room_name'],  # ルーム個別のプロバイダ設定を使用
        gemini_cached_content=gemini_cached_content,
    )

    # --- 【2026-01-20】Gemini 3 Flash: Automatic Function Calling (AFC) 無効化 ---
    # llm.bind() を使って invoke 時にパラメータを注入する。
    # コンストラクタで渡すと model_kwargs に格納されて無視されるため、この方法が必須。
    if is_gemini_3_flash and not explicit_cache_context:
        try:
            from google.genai import types as genai_types
            afc_config = genai_types.AutomaticFunctionCallingConfig(disable=True)
            llm = llm.bind(automatic_function_calling=afc_config)
            print("  - [Gemini 3 Flash] Automatic Function Calling (AFC) を無効化 (via llm.bind)")
        except ImportError:
            print("  - [警告] AFC無効化設定の作成に失敗 (ImportError)")
    elif is_gemini_3_flash and explicit_cache_context:
        print("  - [Gemini Explicit Cache] cached_content参照中のためAFC bindを省略します。")

    # 【ツール不使用モード】ツール使用の有効/無効に応じて分岐
    # --- ツール動的制限の適用 (ToolRegistry) ---
    if explicit_cache_context:
        current_tools = explicit_cache_tools
        llm_or_llm_with_tools = llm
        print(f"  - ツール使用モード: 有効 [Gemini Explicit Cache: cached tools {len(current_tools)}]")
    elif autonomy_finalization_pending:
        current_tools = _select_tools_by_name(AUTONOMY_FINALIZATION_TOOL_NAMES)
        llm_or_llm_with_tools = llm.bind_tools(current_tools)
        reflected = _has_tool_message(state.get("messages", []), "reflect_after_action")
        completed = _has_tool_message(state.get("messages", []), "complete_autonomy_timeline")
        next_step_instruction = (
            "まだ `reflect_after_action` が未実行です。まず `reflect_after_action` を呼び、"
            "次に可能なら `complete_autonomy_timeline` でタイムラインを閉じてください。"
            if not reflected
            else "すでに `reflect_after_action` は実行済みです。次は `complete_autonomy_timeline` を呼んでタイムラインを閉じてください。"
        )
        if completed:
            next_step_instruction = "後始末は完了済みです。ツールを使わず短く結果を報告してください。"
        finalization_instruction = (
            "【自律行動の後始末優先】\n"
            "研究ノート・記憶・Working Memoryなどの更新が成功したため、ここからは新しい通常ツールや追加のノート更新を始めず、"
            "自律行動の後始末を最優先してください。\n"
            f"{next_step_instruction}\n"
            "Working Memory更新などの追加作業は次回アクションに回し、今回の行動を確実にReflectして閉じてください。"
        )
        messages_for_agent = _add_system_instruction(messages_for_agent, finalization_instruction)
        print(f"  - [Autonomy Finalize] 後始末ツールのみ提示します ({len(current_tools)} tools)")
        print(f"  - ツール使用モード: 有効 [Autonomy Finalization]")
    elif state.get('tool_use_enabled', True):
        try:
            from agent.tool_registry import ToolRegistry
            registry = ToolRegistry(all_tools)
            is_roblox_active = state.get('is_roblox_active', False)

            # ToolRegistry は内部で is_room_active を呼ぶが、
            # 既に context_generator で判定済みなので、その結果を尊重するのが効率的。
            # ただし ToolRegistry._is_roblox_enabled(room_name) は
            # 他の設定（activation_mode: disabledなど）も見るため、併用する。
            requested_categories = registry.extract_recent_requested_capabilities(state.get("messages", []))
            if requested_categories:
                image_generation_enabled = config_manager.CONFIG_GLOBAL.get("image_generation_mode", "new") != "disabled"
                # [2026-06-23 FIX] 1ターンで複数カテゴリを要求した場合（例: web と research）は
                # すべての実ツールを統合提示する。最後の1件だけだと先に要求したカテゴリの
                # ツール（例: web_search_tool）が消えてしまい「ツールが見つからない」状態になる。
                current_tools = []
                _seen_tool_names = set()
                for _cat in requested_categories:
                    for _t in registry.get_tools_for_capability(
                        room_name=room_name,
                        category=_cat,
                        tool_use_enabled=True,
                        is_roblox_active=is_roblox_active,
                        image_generation_enabled=image_generation_enabled,
                        autonomous_action_mode=bool(state.get("autonomous_action", False)),
                    ):
                        if _t.name not in _seen_tool_names:
                            _seen_tool_names.add(_t.name)
                            current_tools.append(_t)
                print(f"  - [Capability Broker] {requested_categories} カテゴリの実ツールを提示します ({len(current_tools)} tools)")
            else:
                active_tool_names = state.get("active_tool_names") or []
                if active_tool_names:
                    current_tools = [
                        registry._all_tools_map[name]
                        for name in active_tool_names
                        if name in registry._all_tools_map
                    ]
                else:
                    latest_user_text = ""
                    for msg in reversed(state.get("messages", [])):
                        if isinstance(msg, HumanMessage):
                            latest_user_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                            break
                    image_generation_enabled = config_manager.CONFIG_GLOBAL.get("image_generation_mode", "new") != "disabled"
                    current_tools = registry.select_tools_for_turn(
                        room_name=room_name,
                        latest_user_text=latest_user_text,
                        tool_use_enabled=True,
                        model_name=state.get("model_name", ""),
                        is_roblox_active=is_roblox_active,
                        image_generation_enabled=image_generation_enabled,
                        autonomous_action_mode=bool(state.get("autonomous_action", False)),
                    )

            # ハード制限: Roblox切断時は確実に除外する
            if not is_roblox_active:
                roblox_tool_names = ["send_roblox_command", "roblox_build", "capture_roblox_screenshot"]
                current_tools = [t for t in current_tools if t.name not in roblox_tool_names]
                if state.get("debug_mode", False):
                    print("  - [Tool Limit] Roblox tools filtered due to disconnection.")

            if "zhipu" in state.get('model_name', "").lower():
                llm_or_llm_with_tools = llm.bind_tools(current_tools)
                print("  - ツール使用モード: 有効 (Zhipu: Parallel Tools Disabled) [Dynamic]")
            else:
                llm_or_llm_with_tools = llm.bind_tools(current_tools)
                print(f"  - ツール使用モード: 有効 [Dynamic: {len(current_tools)} tools]")
        except Exception as e:
            print(f"  - [ToolRegistry Error] ツール登録エラー: {e}")
            llm_or_llm_with_tools = llm.bind_tools(all_tools)
            print("  - ツール使用モード: 有効（フォールバック）")
    else:
        loop_limit_reached = state.get("loop_count", 0) >= constants.MAX_TOOL_LOOPS
        autonomous_action_mode = bool(state.get("autonomous_action", False))
        if loop_limit_reached and autonomous_action_mode:
            final_tool_names = _loop_limit_tool_names_for_state(state)
            current_tools = _select_tools_by_name(final_tool_names)
            llm_or_llm_with_tools = llm.bind_tools(current_tools)
            schedule_guidance = ""
            if "schedule_next_action" in final_tool_names:
                try:
                    schedule_guidance = f" やり残しがある場合は `schedule_next_action`（{format_schedule_min_minutes_guidance(room_name)}）で続きを予約してから、"
                except Exception:
                    schedule_guidance = " やり残しがある場合は `schedule_next_action` で続きを予約してから、"
            final_instruction = (
                "【システム制約】現在ツールループの上限に達したため、新たな通常ツールの使用は禁止されています。"
                f"必要な後始末ツールだけが許可されています。{schedule_guidance}"
                "ここまでの結果を `reflect_after_action` / `complete_autonomy_timeline` で記録し、状況報告や思考の結論をテキストで応答してください。"
            )
            messages_for_agent = _add_system_instruction(messages_for_agent, final_instruction)
            print(f"  - ツール使用モード: 上限到達後の自律行動後始末のみ [{len(current_tools)} tools]")
        else:
            llm_or_llm_with_tools = llm
            print("  - ツール使用モード: 無効（会話のみ）")
            if loop_limit_reached:
                final_instruction = "【システム制約】現在ツールループの上限に達したため、新たなツールの使用は禁止されています。これまでのツール実行結果や状況を踏まえて、ここで一度行動を区切り、状況報告や思考の結論をテキストで応答してください。"
                messages_for_agent = _add_system_instruction(messages_for_agent, final_instruction)

    # --- [v25 堅牢化] メッセージ履歴の不整合クリーンアップ (Gemini 3 / Anthropic 共通) ---
    # Gemini 3 や Anthropic は「AIのツール呼び出し(AIMessage.tool_calls) の直後は、必ずツール回答(ToolMessage) でなければならない」という制約が極めて厳しい。
    # ユーザーが新しい発言をして割り込んだり、システムエラーで中断された履歴が残っていると、400 エラーが発生する。
    model_name_lower = state.get('model_name', "").lower()
    llm_str_lower = str(llm).lower()
    if any(k in model_name_lower for k in ["gemini", "anthropic", "claude"]) or any(k in llm_str_lower for k in ["gemini", "anthropic", "claude"]):
        cleaned_messages = []
        for i, msg in enumerate(messages_for_agent):
            if isinstance(msg, AIMessage) and getattr(msg, 'tool_calls', None):
                # 次のメッセージを確認
                has_response = False
                if i + 1 < len(messages_for_agent):
                    next_msg = messages_for_agent[i + 1]
                    if isinstance(next_msg, ToolMessage):
                        has_response = True

                if not has_response:
                    if state.get("debug_mode", False):
                        print(f"  - [History Cleanup] 未回答のツール呼び出しを検出。情報の整合性を保つため tool_calls をクリアします (index={i})")
                    import copy
                    msg_copy = copy.deepcopy(msg)
                    msg_copy.tool_calls = []
                    if hasattr(msg_copy, 'additional_kwargs') and msg_copy.additional_kwargs:
                        msg_copy.additional_kwargs.pop("__gemini_function_call_thought_signatures__", None)
                    cleaned_messages.append(msg_copy)
                else:
                    cleaned_messages.append(msg)
            else:
                cleaned_messages.append(msg)
        messages_for_agent = cleaned_messages

    if not is_gemini_3_signature_compatible:
        stripped_count = _strip_gemini_thought_signatures(messages_for_agent)
        if stripped_count and state.get("debug_mode", False):
            print(
                "  - [Cross Model Signature Guard] 非Gemini 3系モデル送信前に、"
                f"思考署名 {stripped_count}件を除去しました。"
            )

    if explicit_cache_context:
        before_count = len(messages_for_agent)
        messages_for_agent = _build_messages_for_gemini_explicit_cache(messages_for_agent)
        print(
            "  - [Gemini Explicit Cache] cached_content参照のため、"
            f"SystemMessage/toolsを送信せず contents のみに変換しました ({before_count} -> {len(messages_for_agent)} messages)"
        )

    # --- [Gemini 3 DEBUG] 送信前のメッセージ履歴構造を出力 ---
    if state.get("debug_mode", False) and ("gemini-3" in state.get('model_name', '').lower()):
        print(f"\n--- [GEMINI3_DEBUG] 送信メッセージ構造 ({len(messages_for_agent)}件) ---")
        # 要約メッセージの位置を検出して先頭に表示
        summary_pos = None
        for si, sm in enumerate(messages_for_agent):
            sc = getattr(sm, 'content', '')
            if isinstance(sc, str) and "【本日のこれまでの会話の要約】" in sc:
                summary_pos = si
                break
        if summary_pos is not None:
            remaining = len(messages_for_agent) - summary_pos - 1
            print(f"  [Auto Summary] 位置={summary_pos} | 構成: [要約1件] + [直近ログ {remaining}件]")
        for idx, msg in enumerate(messages_for_agent[-10:]):  # 最後の10件のみ表示
            actual_idx = len(messages_for_agent) - 10 + idx if len(messages_for_agent) > 10 else idx
            msg_type = type(msg).__name__
            has_tool_calls = hasattr(msg, 'tool_calls') and msg.tool_calls
            has_sig = msg.additional_kwargs.get('__gemini_function_call_thought_signatures__') if hasattr(msg, 'additional_kwargs') and msg.additional_kwargs else None
            content_preview = ""
            if isinstance(msg.content, str):
                content_preview = (msg.content[:50] + "...") if len(msg.content) > 50 else msg.content
            elif isinstance(msg.content, list):
                content_preview = f"[マルチパート: {len(msg.content)}部分]"
            print(f"  [{actual_idx:3d}] {msg_type:15} | tool_calls={1 if has_tool_calls else 0} | sig={1 if has_sig else 0} | {content_preview[:40]}")
        print(f"--- [GEMINI3_DEBUG] 送信メッセージ構造 完了 ---\n")

    try:
        # --- [リトライ機構] 空応答（ANOMALY）/ MALFORMED_RESPONSE 対策 ---
        # 【2026-04-28 改善】リトライ回数を2→3に増加（MALFORMED_RESPONSE は一時的障害が多いため）
        max_agent_retries = 3
        # 画像非対応モデル（OpenRouterのテキスト専用モデル等）向けに、一度だけ画像を除去して再試行する。
        images_stripped_for_unsupported_model = False

        # システムプロンプトの追加
        # ※ 既に 1185行目付近で追加されているため、ここでは重複を避ける（APIによっては複数システムプロンプトでエラーになるため）
        # messages_for_agent = [SystemMessage(content=final_system_prompt_text)] + messages_for_agent

        # 【2026-04-28 NEW】末尾ロールガード: コンテキスト末尾がAIMessage（Assistantロール）の場合、
        # Geminiが「既に応答済み」と判断して空応答を返す可能性があるため、
        # ダミーのHumanMessageを追加して応答を促す（グループチャット等のエッジケース対策）
        # ただし、ツール使用が含まれる場合はAnthropic等でエラーになるためスキップする。
        if messages_for_agent and isinstance(messages_for_agent[-1], AIMessage):
            last_msg = messages_for_agent[-1]
            has_tool_calls = hasattr(last_msg, "tool_calls") and last_msg.tool_calls
            if not has_tool_calls:
                messages_for_agent.append(HumanMessage(content="（続けてください）"))
                print("  - [末尾ロールガード] 末尾がAIMessageのため、HumanMessageを追加しました")
            else:
                print("  - [末尾ロールガード] 末尾がAIMessageですが、ツール使用が含まれるためHumanMessageの追加をスキップしました")

        if not explicit_cache_context:
            messages_for_agent = _prepare_messages_for_provider_cache(messages_for_agent, llm)

        if messages_for_agent and isinstance(messages_for_agent[0], SystemMessage):
            system_text_for_send = messages_for_agent[0].content
            if not isinstance(system_text_for_send, str):
                system_text_for_send = str(system_text_for_send)
            messages_for_agent[0] = _build_system_message_for_llm(system_text_for_send, llm)
            if _is_anthropic_llm(llm):
                cache_control_count = _count_anthropic_cache_control_blocks(messages_for_agent)
                if cache_control_count > 4:
                    raise ValueError(f"Anthropic cache_control block count exceeded limit: {cache_control_count}")

        # --- LLM実行 ---
        # ストリーミング実行（トークンごとの出力）と Invoke実行の分岐
        # Gemini 3 Flash Preview はストリーミングだとツール使用時に挙動不審になるため、
        # invokeモードを強制するオプションを用意。

        use_streaming = True
        # Gemini 3 Flash はストリーミング無効化（ツール使用可否に関わらず）
        if is_gemini_3_flash:
            use_streaming = False
            print("  - [Gemini 3 Flash] LLM呼び出しをinvokeモードに切り替え")

        # リトライループ
        response_direct = None
        chunks = []
        combined_text = ""
        additional_kwargs = {}
        response_metadata = {}
        all_tool_calls_chunks = []
        # 【2026-04-28 NEW】MALFORMED_RESPONSE 時に thinking パートの内容を保持するバッファ
        # 全リトライ失敗時のフォールバック表示用
        last_thinking_content = ""

        for attempt in range(max_agent_retries + 1):
            try:
                # 診断: リクエストサイズの計測
                # total_input_chars = sum(len(m.content) for m in messages_for_agent if isinstance(m.content, str))
                # print(f"  - [Request Size] メッセージ数: {len(messages_for_agent)}, 総文字数: {total_input_chars}, ツール数: {len(all_tools) if tool_use_enabled else 0}")

                stream_start_time = time.time()
                chunks = []
                merged_chunk = None

                if use_streaming:
                    # --- 通常のストリーミングモード ---
                    # print(f"  - AIモデルにリクエストを送信中 (Streaming)... [試行 {attempt + 1}]")
                    first_token_time = None
                    try:
                        for chunk in llm_or_llm_with_tools.stream(messages_for_agent):
                            if first_token_time is None:
                                first_token_time = time.time()
                                print(f"--- [PERF] agent_node stream: First token latency: {first_token_time - stream_start_time:.4f}s ---")
                            chunks.append(chunk)
                    except Exception as e:
                        print(f"--- [警告] ストリーミング中に例外が発生しました: {e} ---")
                        if not chunks: raise e

                    if chunks:
                        total_stream_time = time.time() - stream_start_time
                        print(f"--- [PERF] agent_node stream: Total time: {total_stream_time:.4f}s ---")
                        # チャンクの結合
                        if chunks:
                            # 1枚目を基準にするが、AIMessageChunk 以外（Responseオブジェクト等）が含まれる可能性を考慮
                            first_chunk = chunks[0]
                            # AIMessageChunk であれば += で結合可能
                            if hasattr(first_chunk, "__add__") or hasattr(first_chunk, "__iadd__"):
                                merged_chunk = first_chunk
                                for c in chunks[1:]:
                                    try:
                                        merged_chunk += c
                                    except Exception as merge_err:
                                        print(f"--- [警告] チャンクの結合に失敗しました: {merge_err} ---")
                            else:
                                # 結合不能なオブジェクト（Response等）の場合は、
                                # 後の処理で merged_chunk.content 等を参照できるように AIMessage で包む
                                merged_chunk = AIMessage(
                                    content=utils.get_content_as_string(first_chunk),
                                    response_metadata=getattr(first_chunk, "response_metadata", {})
                                )
                                # 2枚目以降もテキストとして結合
                                for c in chunks[1:]:
                                    merged_chunk.content += utils.get_content_as_string(c)
                else:
                    # --- Gemini 3 Flash用 非ストリーミングモード ---
                    # print(f"  - AIモデルにリクエストを送信中 (Invoke)... [試行 {attempt + 1}]")

                    try:
                        # [2026-02-19 FIX] タイムアウト付きinvoke（API無応答によるハング防止）
                        import concurrent.futures
                        _LLM_INVOKE_TIMEOUT = 900  # Local LLM等の長文処理対策で延長 (300 -> 900秒)
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(llm_or_llm_with_tools.invoke, messages_for_agent)
                            try:
                                response_direct = future.result(timeout=_LLM_INVOKE_TIMEOUT)
                            except concurrent.futures.TimeoutError:
                                print(f"--- [警告] LLM invoke がタイムアウトしました ({_LLM_INVOKE_TIMEOUT}s) ---")
                                future.cancel()
                                raise TimeoutError(f"LLM応答タイムアウト ({_LLM_INVOKE_TIMEOUT}秒)")

                        # Gemini 3 Flash (Thinking Mode) は content をリストで返すことがある。
                        # ここで text だけへ破壊的に正規化すると、後段の
                        # thinking/thought -> [THOUGHT] 変換に思考パートが届かないため、
                        # 元の list 構造は保持する。
                        raw_content = response_direct.content
                        print(f"  - [ContentDebug] type={type(raw_content).__name__}, ", end="")
                        if isinstance(raw_content, list):
                            type_counts = {}
                            for part in raw_content:
                                if isinstance(part, dict):
                                    pt = part.get("type", "unknown")
                                    text_len = len(part.get("text", part.get("thinking", part.get("thought", ""))) or "")
                                    type_counts[pt] = type_counts.get(pt, 0) + text_len
                                elif isinstance(part, str):
                                    type_counts["raw_str"] = type_counts.get("raw_str", 0) + len(part)
                                else:
                                    type_counts[type(part).__name__] = type_counts.get(type(part).__name__, 0) + 1
                            print(f"parts={len(raw_content)}, breakdown={type_counts}")
                        elif isinstance(raw_content, str):
                            print(f"len={len(raw_content)}, preview='{raw_content[:100]}...'")
                        else:
                            print(f"unexpected: {type(raw_content)}")

                        if isinstance(response_direct.content, list):
                            full_text_buffer = []
                            for part in response_direct.content:
                                if isinstance(part, dict):
                                    p_type = part.get("type")
                                    if p_type == "text":
                                        full_text_buffer.append(part.get("text", ""))

                            if not full_text_buffer:
                                # 【2026-04-28 改善】thinking パートの内容をフォールバック用に保持
                                thinking_parts = []
                                for fb_part in raw_content:
                                    if isinstance(fb_part, dict):
                                        fb_type = fb_part.get("type")
                                        if fb_type in ("thinking", "thought"):
                                            t = fb_part.get("thinking") or fb_part.get("thought", "")
                                            if t and t.strip():
                                                thinking_parts.append(t.strip())
                                if thinking_parts:
                                    last_thinking_content = "\n".join(thinking_parts)

                                # 【2026-06-15 改善】誤警告の切り分け。
                                # Gemini 3 Flash の thinking モードでは「ツール呼び出しだけを返すターン」で
                                # text パートが 0 件になるのが正常挙動。tool_calls がある場合は MALFORMED ではなく
                                # 通常のツールターンなので、MALFORMED 警告を出さない（本物の MALFORMED を埋もれさせない）。
                                # 本当に怪しいのは「text 0 件 かつ tool_calls 0 件」のときだけ。
                                _has_tool_calls = bool(getattr(response_direct, "tool_calls", None))
                                if _has_tool_calls:
                                    if thinking_parts:
                                        print(f"  - [ContentDebug] textパートなし（thinkingパート {len(thinking_parts)}件）。ツール呼び出しターン（正常）。")
                                    else:
                                        print("  - [ContentDebug] textパートなし。ツール呼び出しターン（正常）。")
                                else:
                                    # 真に空（text なし・tool_calls なし）のときだけ MALFORMED を疑う。
                                    _rm = getattr(response_direct, "response_metadata", {}) or {}
                                    _finish_reason = (
                                        _rm.get("finish_reason")
                                        or _rm.get("finishReason")
                                        or "不明"
                                    ) if isinstance(_rm, dict) else "不明"
                                    if thinking_parts:
                                        print(f"  - [ContentDebug] textパートなし・tool_callsなし（thinkingパート {len(thinking_parts)}件を保持）。finish_reason={_finish_reason}。MALFORMED_RESPONSE等で強制終了された可能性。")
                                    else:
                                        print(f"  - [ContentDebug] textパートなし・tool_callsなし。finish_reason={_finish_reason}。MALFORMED_RESPONSE等で強制終了された可能性。")
                        chunks = [response_direct]
                        merged_chunk = response_direct
                        total_invoke_time = time.time() - stream_start_time
                        print(f"  - Invoke完了: 合計{total_invoke_time:.2f}秒")

                    except Exception as e:
                        print(f"--- [警告] Invoke中に例外が発生しました: {e} ---")
                        raise e


                # --- 空チェック（両モード共通）---
                # チャンク自体が空、またはツール実行がなく本文も空なら異常終了とみなす
                has_tools = False
                if merged_chunk and hasattr(merged_chunk, "tool_calls") and merged_chunk.tool_calls:
                    has_tools = True

                content_str = utils.get_content_as_string(merged_chunk) if merged_chunk else ""

                if not chunks or (not has_tools and not content_str.strip()):
                    if attempt < max_agent_retries:
                        # 【2026-04-28 改善】エクスポネンシャルバックオフ (2s, 4s, 8s)
                        backoff_wait = 2 * (2 ** attempt)
                        print(f"  - [Retry] AIからの有効な応答が得られませんでした。{backoff_wait}秒後に再試行します... ({attempt+1}/{max_agent_retries})")
                        time.sleep(backoff_wait)
                        continue # 次の試行へ
                    # 【2026-04-28 改善】全リトライ失敗時、保持した thinking パートがあればフォールバック表示
                    if last_thinking_content:
                        combined_text = f"[THOUGHT]\n{last_thinking_content}\n[/THOUGHT]\n（AIの思考は確認できましたが、応答テキストの生成に失敗しました。再生成をお試しください。）"
                        print(f"  - [Fallback] thinking パートの内容をフォールバック表示します ({len(last_thinking_content)} chars)")
                    else:
                        combined_text = "（AIからの応答が空でした。モデルの制限や安全フィルターにより出力が抑制された可能性があります。再生成をお試しください。）"
                    all_tool_calls_chunks = []
                    response_metadata = {}
                    additional_kwargs = {}
                else:
                    all_tool_calls_chunks = getattr(merged_chunk, "tool_calls", [])
                    response_metadata = getattr(merged_chunk, "response_metadata", {}) or {}
                    additional_kwargs = getattr(merged_chunk, "additional_kwargs", {}) or {}

                    # ★ デバッグ: Gemini 3 思考署名の確認
                    if state.get("debug_mode", False):
                        gemini_signatures = additional_kwargs.get("__gemini_function_call_thought_signatures__")
                        if not gemini_signatures:
                            found_sig = None
                            for c in chunks:
                                # chunk.content への安全なアクセス
                                c_content = getattr(c, "content", None)
                                if isinstance(c_content, list):
                                    for part in c_content:
                                        # part への安全なアクセス
                                        if isinstance(part, dict) and part.get('extras'):
                                            extras = part.get('extras')
                                            # extras が dict 以外（Responseオブジェクト等）の場合も考慮
                                            if isinstance(extras, dict):
                                                sig = extras.get('signature')
                                            else:
                                                sig = getattr(extras, 'signature', None)

                                            if sig: found_sig = sig; break
                                if found_sig: break

                            if found_sig:
                                sig_dict = {}
                                if all_tool_calls_chunks:
                                    for tc in all_tool_calls_chunks:
                                        # tc が dict 形式であることを確認
                                        if isinstance(tc, dict):
                                            tc_id = tc.get("id")
                                            if tc_id: sig_dict[tc_id] = found_sig
                                additional_kwargs["__gemini_function_call_thought_signatures__"] = sig_dict if sig_dict else [found_sig]

                    combined_text = _combine_chunks_with_thought_parts(chunks)
                    # ループを抜ける条件（正常な応答が得られた）
                    break

            except Exception as e:
                # 429 RESOURCE_EXHAUSTED はキー制御のため即時に上位へ戻す。
                # 503 UNAVAILABLE / OVERLOADED はモデル側の一時過負荷なので、同じ
                # prompt/tools/api key のまま agent_node 内で吸収し、context_generator の
                # 再実行を避ける。
                err_str = str(e).upper()
                is_prompt_too_long = _is_agent_prompt_too_long_error(e)
                is_429 = _is_agent_resource_exhausted_error(e)
                is_503 = _is_agent_service_unavailable_error(e, is_429=is_429)

                # 画像非対応モデルへ画像付き履歴を送ったエラー → 画像を除去して一度だけ再試行。
                if _is_model_image_unsupported_error(e):
                    if not images_stripped_for_unsupported_model:
                        messages_for_agent, stripped_count = _strip_image_content_from_messages(messages_for_agent)
                        images_stripped_for_unsupported_model = True
                        print(
                            f"  - [Agent Retry] 送信先モデルが画像入力に非対応のため、履歴から画像 {stripped_count}件 を除去して再試行します。"
                        )
                        if stripped_count:
                            # ペルソナに説明させず、システムアナウンスとしてチャットへ表示する
                            # （同一ターンでの表示は ui_handlers の応答確定後に consume される）。
                            try:
                                utils.add_system_notice(
                                    "現在の応答モデルは画像入力に非対応のため、添付画像は文章（キャプション・情景描写）として送信し、"
                                    "画像そのものは省略しました。画像を直接見せたい場合は、画像対応モデルに切り替えてください。",
                                    level="info",
                                )
                            except Exception:
                                pass
                        continue
                    print(f"--- [DEBUG] 画像除去後も画像非対応エラーが継続。上位へ戻します({err_str})。 ---")
                    raise e

                if is_prompt_too_long:
                    print(f"--- [DEBUG] Prompt too long 例外を検知しました。リトライせず上位へ戻します({err_str})。 ---")
                    raise e

                if is_429:
                    print(f"--- [DEBUG] 429 例外を検知しました。キー制御のため上位へ戻します({err_str})。 ---")
                    raise e

                if is_503:
                    if attempt < max_agent_retries:
                        backoff_wait = 2 * (2 ** attempt)
                        print(f"  - [Agent Retry] 503 UNAVAILABLE/OVERLOADED detected in agent_node. Retrying local LLM call on SAME key after {backoff_wait}s... ({attempt+1}/{max_agent_retries})")
                        time.sleep(backoff_wait)
                        continue
                    print(f"--- [DEBUG] agent_node local 503 retry exhausted. 上位へ制御を戻します({err_str})。 ---")
                    raise e

                print(f"--- [警告] agent_node 試行 {attempt + 1} でエラーが発生しました: {e} ---")
                if attempt < max_agent_retries:
                    time.sleep(2) # エラー時は少し長めに待機
                    continue
                raise e

        # --- [結果の統合] ---
        if chunks and merged_chunk:
            merged_chunk.content = combined_text
            response = merged_chunk
        else:
            response = AIMessage(
                content=combined_text,
                additional_kwargs=additional_kwargs,
                response_metadata=response_metadata,
                tool_calls=all_tool_calls_chunks
            )

        # 署名確保
        captured_signature = additional_kwargs.get("__gemini_function_call_thought_signatures__")
        if captured_signature:
            signature_manager.save_turn_context(state['room_name'], captured_signature, all_tool_calls_chunks)

        # 実送信トークン量の抽出（プロンプト＋回答）
        # LangChain (Gemini/OpenAI) で形式が異なる場合があるため柔軟に取得
        # response_metadata ガード
        rm_safe = response_metadata if isinstance(response_metadata, dict) else {}
        actual_usage = rm_safe.get("token_usage") or rm_safe.get("usage")

        if not actual_usage:
            # response (旧 merged_chunk) の属性確認
            if hasattr(response, "usage_metadata"):
                actual_usage = getattr(response, "usage_metadata", None)
            elif hasattr(response, "response_metadata"):
                # merged_chunk 自身が response_metadata を持っている場合（念のため）
                rm_inner = getattr(response, "response_metadata", {})
                if isinstance(rm_inner, dict):
                    actual_usage = rm_inner.get("token_usage") or rm_inner.get("usage")

        # 辞書形式ならそのまま、そうでなければ属性から
        token_data = {}
        if actual_usage:
            if isinstance(actual_usage, dict):
                token_data = {
                    "prompt_tokens": actual_usage.get("prompt_tokens") or actual_usage.get("prompt_token_count") or actual_usage.get("input_tokens", 0),
                    "completion_tokens": actual_usage.get("completion_tokens") or actual_usage.get("candidates_token_count") or actual_usage.get("output_tokens", 0),
                    "total_tokens": actual_usage.get("total_tokens") or actual_usage.get("total_token_count", 0)
                }
            else:
                # オブジェクト（Response, UsageMetadata 等）としてのアクセス
                token_data = {
                    "prompt_tokens": getattr(actual_usage, "prompt_tokens", None) or getattr(actual_usage, "prompt_token_count", None) or getattr(actual_usage, "input_tokens", 0),
                    "completion_tokens": getattr(actual_usage, "completion_tokens", None) or getattr(actual_usage, "candidates_token_count", None) or getattr(actual_usage, "output_tokens", 0),
                    "total_tokens": getattr(actual_usage, "total_tokens", None) or getattr(actual_usage, "total_token_count", 0)
                }

        cache_usage_data = _log_prompt_cache_usage(
            state.get("model_name", "unknown"),
            response,
            response_metadata,
            token_data.get("prompt_tokens"),
        )
        token_data["model_name"] = state.get("model_name", "")
        token_data["is_paid"] = True
        model_provider_for_paid = "gemini" if "gemini" in str(state.get("model_name", "")).lower() else ""
        if model_provider_for_paid:
            key_name_for_paid = state.get("api_key_name") or config_manager.get_key_name_by_value(state.get("api_key", ""))
            token_data["is_paid"] = config_manager.is_paid_api_key_name(key_name_for_paid)
        if explicit_cache_context:
            cache_usage_data.setdefault("cache_read_tokens", 0)
            cache_usage_data.setdefault("cache_creation_tokens", 0)
            cache_usage_data.setdefault("cache_total_input_tokens", token_data.get("prompt_tokens", 0) or 0)
            cache_usage_data.setdefault("total_input_tokens", token_data.get("prompt_tokens", 0) or 0)
            cache_usage_data["cache_mode"] = "gemini_explicit"
            cache_usage_data["cache_paid"] = True
            # 新規作成（登録=書込）ターンか、再利用（読込ヒット）ターンかを区別する。
            # 明示キャッシュは作成と同ターンに読込も発生するため、created なら
            # 「お得なヒット」ではなく「有料登録ターン」と明示してユーザーの誤解を防ぐ。
            cache_usage_data["cache_just_created"] = bool(getattr(explicit_cache_context, "created", False))
        elif cache_usage_data and "gemini" in str(state.get("model_name", "")).lower():
            cache_usage_data["cache_mode"] = "gemini_implicit"
        token_data.update(cache_usage_data)
        try:
            import usage_ledger
            source = "autonomous" if state.get("autonomous_action") else "chat"
            key_name = state.get("api_key_name") or config_manager.get_key_name_by_value(state.get("api_key", ""))
            usage_ledger.record_turn(token_data, source=source, api_key_name=key_name)
        except Exception:
            pass

        # [2026-05-16 MOD] ツール内容によるループ消費量の重み付け
        tool_weight = 1.0
        if getattr(response, "tool_calls", None):
            tool_names = [tc.get("name", "") for tc in response.tool_calls]
            if len(tool_names) > 0 and all(name == "request_capability" for name in tool_names):
                tool_weight = 0.5
            elif len(tool_names) > 0 and all(name.startswith(("read_", "search_", "list_", "get_", "find_")) for name in tool_names):
                tool_weight = 0.5
        loop_count += tool_weight

        if not getattr(response, "tool_calls", None):
            # --- [未解決の問い自動解決] 対話終了時に問いの解決判定を実行 ---
            try:
                from motivation_manager import MotivationManager
                mm = MotivationManager(state['room_name'])

                # 直近会話をテキスト化
                recent_turns = []
                for msg in history_messages[-10:]:  # 直近10件
                    if isinstance(msg, (HumanMessage, AIMessage)):
                        content = msg.content if isinstance(msg.content, str) else str(msg.content)
                        role = "ユーザー" if isinstance(msg, HumanMessage) else "AI"
                        recent_turns.append(f"{role}: {content[:500]}")

                if recent_turns:
                    response_content = response.content if isinstance(response.content, str) else str(response.content)
                    if response_content:
                        recent_turns.append(f"AI: {response_content[:500]}")
                    recent_text = "\n".join(recent_turns)
                    if mm.get_open_questions_for_context():
                        resolved = mm.auto_resolve_questions(recent_text, state['api_key'])
                        if resolved:
                            print(f"  - [Agent] 未解決の問い {len(resolved)}件を解決済みとしてマーク")

                    # 古い問いの優先度を下げる（毎回ではなくたまに実行）
                    if loop_count == 0:  # 最初のループ時のみ
                        mm.decay_old_questions()
            except Exception as mm_e:
                print(f"  - [Agent] 問い自動解決処理でエラー（無視）: {mm_e}")
            # --- 自動解決ここまで ---

            result_update = {
                "messages": [response],
                "loop_count": loop_count,
                "last_successful_response": response,
                "model_name": state['model_name'],
                "actual_token_usage": token_data
            }
            if pending_capability_followup_update is not None:
                result_update["pending_capability_followup"] = pending_capability_followup_update
            return result_update
        else:
            result_update = {
                "messages": [response],
                "loop_count": loop_count,
                "model_name": state['model_name'],
                "actual_token_usage": token_data
            }
            if pending_capability_followup_update is not None:
                result_update["pending_capability_followup"] = pending_capability_followup_update
            return result_update

    # ▼▼▼ Gemini 3 思考署名エラーのソフトランディング処理 (結果表示版) ▼▼▼
    except (google_exceptions.InvalidArgument, ChatGoogleGenerativeAIError) as e:
        error_str = str(e)
        if _is_thought_signature_error(error_str):
            print(f"  - [Thinking] Gemini 3 思考署名エラーを検知しました。ツール実行結果を含めて終了します。")

            tool_result_text = ""
            if history_messages and isinstance(history_messages[-1], ToolMessage):
                tool_result_text = f"\n\n【システム報告：ツール実行結果】\n{history_messages[-1].content}"
            elif messages_for_agent and isinstance(messages_for_agent[-1], ToolMessage):
                 tool_result_text = f"\n\n【システム報告：ツール実行結果】\n{messages_for_agent[-1].content}"

            fallback_msg = AIMessage(content=f"（思考プロセスの署名検証により対話を中断しましたが、以下の処理は実行されました。）{tool_result_text}")

            return {
                "messages": [fallback_msg],
                "loop_count": loop_count,
                "force_end": True,
                "model_name": state['model_name']
            }
        else:
            print(f"--- [警告] agent_nodeでAPIエラーを捕捉しました: {e} ---")
            raise e
    # ▼▼▼ 【マルチモデル対応】OpenAIエラーハンドリング ▼▼▼
    except OPENAI_ERRORS as e:
        error_str = str(e).lower()
        model_name = state.get('model_name', '不明なモデル')

        # ツール/Function Calling関連エラーの検知（複数パターンに対応）
        tool_error_patterns = [
            "tools is not supported",
            "function calling",
            "failed to call a function",
            "tool call validation failed"
        ]
        is_tool_error = any(pattern in error_str for pattern in tool_error_patterns)

        if is_tool_error:
            print(f"  - [OpenAI] ツール非対応モデルエラーを検知: {model_name}")
            raise RuntimeError(
                f"⚠️ モデル非対応エラー: 選択されたモデル `{model_name}` はツール呼び出し（Function Calling）に対応していません。"
                f"\n\n【解決方法】"
                f"\n1. 設定タブ→プロバイダ設定で「ツール使用」をOFFにする"
                f"\n2. または、Function Calling対応モデルに変更する"
                f"\n3. または、Geminiプロバイダに切り替える"
            ) from e
        else:
            print(f"--- [警告] agent_nodeでOpenAIエラーを捕捉しました: {e} ---")
            raise e
    except Exception as e:
        is_429 = _is_agent_resource_exhausted_error(e)
        is_503 = _is_agent_service_unavailable_error(e, is_429=is_429)
        if is_429 or is_503:
            # ローテーションのため上位へ例外を伝播させる
            print(f"--- [警告] agent_nodeでAPIエラーを捕捉しました（ローテーションのため上位へ伝播）: {e} ---")
            raise e

        print(f"--- [致命的エラー] agent_nodeで予期せぬエラーが発生しました: {e} ---")
        import traceback
        traceback.print_exc()
        error_msg = f"（エラーが発生しました: {str(e)}。設定や通信状況を再度ご確認ください。）"
        return {"messages": [AIMessage(content=error_msg)], "loop_count": loop_count, "force_end": True, "model_name": state['model_name']}
    # ▲▲▲ ここまで ▲▲▲

def _coerce_tool_args_for_schema(selected_tool, tool_args):
    """LLM が文字列フィールドに空dict `{}` / 空list `[]` / None を渡す定番ミスを補正する。

    例: request_capability の `details`（str）に `{}` を渡すと pydantic 検証が即エラーになり、
    ツール実行に到達しない。ツールの args スキーマを参照し、string 型フィールドに空コンテナ/None
    が来た場合のみ空文字へ補正する（実値のある dict/list はそのまま検証に委ねる）。
    """
    if not isinstance(tool_args, dict):
        return tool_args
    try:
        schema = getattr(selected_tool, "args", {}) or {}
    except Exception:
        return tool_args
    for key, value in list(tool_args.items()):
        spec = schema.get(key)
        if not isinstance(spec, dict):
            continue
        is_string_field = spec.get("type") == "string" or any(
            isinstance(t, dict) and t.get("type") == "string" for t in spec.get("anyOf", [])
        )
        if is_string_field and (value is None or (isinstance(value, (dict, list)) and not value)):
            tool_args[key] = ""
    return tool_args


def _execute_single_tool_inner(state: AgentState, tool_call: dict, current_signature: str):
    """
    内部ヘルパー: 単一のツールコールを処理し、ToolMessageを返す。
    """
    import signature_manager
    tool_name = tool_call["name"]
    tool_args = tool_call["args"].copy()

    # --- 追加: 引数名のクレンジング (モデルの引用符誤記対策) ---
    if isinstance(tool_args, dict):
        tool_args = {k.strip("'\""): v for k, v in tool_args.items()}


    skip_execution = state.get("skip_tool_execution", False)
    if skip_execution and tool_name in side_effect_tools:
        print(f"  - [リトライ検知] 副作用のあるツール '{tool_name}' の再実行をスキップします。")
        output = "【リトライ成功】このツールは直前の試行で既に正常に実行されています。その結果についてユーザーに報告してください。"
        tool_msg = ToolMessage(content=output, tool_call_id=tool_call["id"], name=tool_name)

        # 署名注入
        if current_signature:
            tool_msg.artifact = {"thought_signature": current_signature}

        return tool_msg

    room_name = state.get('room_name')
    api_key = state.get('api_key')
    if isinstance(tool_args, dict) and tool_name != "request_capability":
        tool_args['room_name'] = room_name
        if state.get("autonomous_action", False):
            timeline_id = str(state.get("autonomous_timeline_id") or "").strip()
            if timeline_id and tool_name in {
                "read_autonomy_context",
                "record_autonomy_step",
                "reflect_after_action",
                "complete_autonomy_timeline",
                "record_capability_audit",
            }:
                tool_args.setdefault("timeline_id", timeline_id)

    # --- ワーキングメモリ系（確認付き直接実行） ---
    if tool_name in ["update_working_memory", "switch_working_memory", "patch_working_memory", "link_working_memory_to_research_thread"]:
        try:
            print(f"  - ワーキングメモリツール実行: {tool_name}")
            tool_args = _normalize_working_memory_tool_args(tool_name, tool_args)
            selected_tool = next((t for t in all_tools if t.name == tool_name), None)
            if not selected_tool:
                output = f"Error: Tool '{tool_name}' not found."
            else:
                output = selected_tool.invoke(tool_args)
        except Exception as e:
            output = f"ワーキングメモリの操作中にエラーが発生しました ('{tool_name}'): {e}"
            traceback.print_exc()

    # --- ファイル編集系（プランニング＆反映） ---
    elif tool_name in ["plan_identity_memory_edit", "plan_diary_append", "plan_secret_diary_edit", "plan_notepad_edit", "plan_creative_notes_edit", "plan_research_notes_edit", "plan_world_edit"]:
        try:
            # ツール種別判定
            is_plan_identity_memory = tool_name == "plan_identity_memory_edit"
            is_plan_diary_append = tool_name == "plan_diary_append"
            is_plan_secret_diary = tool_name == "plan_secret_diary_edit"
            is_plan_notepad = tool_name == "plan_notepad_edit"
            is_plan_creative_notes = tool_name == "plan_creative_notes_edit"
            is_plan_research_notes = tool_name == "plan_research_notes_edit"
            is_plan_world = tool_name == "plan_world_edit"

            is_editing_task = (is_plan_identity_memory or is_plan_diary_append or is_plan_secret_diary or
                               is_plan_notepad or is_plan_creative_notes or is_plan_research_notes or is_plan_world)

            # --- 引数の正規化と保護 ---
            mod_req = tool_args.get('modification_request')
            if not mod_req:
                for alt_key in ['content', 'text', 'request', 'new_content', 'entry', 'notes', 'value']:
                    if alt_key in tool_args:
                        mod_req = tool_args[alt_key]
                        break

            if mod_req is None or str(mod_req).strip() == "" or str(mod_req).strip() == "None":
                # 引数が空のままのツール呼び出し（特に軽量モデルで稀に発生）が
                # 同じ失敗を繰り返さないよう、何をどう直せばよいかを具体的に伝える。
                if is_plan_research_notes:
                    example = (
                        'plan_research_notes_edit(context_type="NEW", '
                        'intent_and_reasoning="（なぜこの分類かの説明）", '
                        'modification_request="（ここに保存したい研究ノートの本文そのもの）")'
                    )
                else:
                    example = f'{tool_name}(modification_request="（ここに保存したい本文そのもの）")'
                raise ValueError(
                    "エラー: 必須引数『modification_request』（保存したい本文そのもの）が空です。"
                    "前回は引数が空のまま呼び出されています。"
                    f"同じ空の引数で再試行せず、modification_request に実際の本文を必ず入れて呼び出し直してください。例: {example}"
                )

            identity_backup_path = ""
            if is_plan_identity_memory and state.get("autonomous_action", False):
                approval_block = _identity_memory_approval_block_message(
                    room_name,
                    str(mod_req),
                    timeline_id=str(state.get("autonomous_timeline_id") or tool_args.get("timeline_id") or ""),
                )
                if approval_block:
                    tool_msg = ToolMessage(content=approval_block, tool_call_id=tool_call["id"], name=tool_name)
                    if current_signature:
                        tool_msg.artifact = {"thought_signature": current_signature}
                    return tool_msg

            # 2. ファイル読み込みとバックアップ
            print(f"  - ファイル編集プロセスを開始: {tool_name}")

            # バックアップ作成
            if is_plan_identity_memory:
                identity_backup_path = _create_required_identity_memory_backup(room_name)
                if not identity_backup_path:
                    output = "【致命的エラー】identity memory編集前のバックアップに失敗したため、編集を中止しました。"
                    _record_identity_memory_audit(
                        room_name,
                        str(mod_req),
                        "failure",
                        output,
                        timeline_id=str(state.get("autonomous_timeline_id") or tool_args.get("timeline_id") or ""),
                    )
                    tool_msg = ToolMessage(content=output, tool_call_id=tool_call["id"], name=tool_name)
                    if current_signature:
                        tool_msg.artifact = {"thought_signature": current_signature}
                    return tool_msg
            elif is_plan_diary_append: room_manager.create_backup(room_name, 'diary')
            elif is_plan_secret_diary: room_manager.create_backup(room_name, 'secret_diary')
            elif is_plan_notepad: room_manager.create_backup(room_name, 'notepad')
            elif is_plan_creative_notes: room_manager.create_backup(room_name, 'creative_notes')
            elif is_plan_research_notes: room_manager.create_backup(room_name, 'research_notes')
            elif is_plan_world: room_manager.create_backup(room_name, 'world_setting')

            read_tool = None
            if is_plan_identity_memory: read_tool = read_identity_memory
            elif is_plan_diary_append: read_tool = read_diary_memory
            elif is_plan_secret_diary: read_tool = read_secret_diary
            elif is_plan_notepad: read_tool = read_full_notepad
            elif is_plan_creative_notes: read_tool = read_creative_notes
            elif is_plan_research_notes: read_tool = read_research_notes
            elif is_plan_world: read_tool = read_world_settings

            raw_content = read_tool.invoke({"room_name": room_name})

            if is_plan_identity_memory or is_plan_secret_diary or is_plan_notepad or is_plan_creative_notes or is_plan_research_notes:
                lines = raw_content.split('\n')
                numbered_lines = [f"{i+1}: {line}" for i, line in enumerate(lines)]
                current_content = "\n".join(numbered_lines)
            else:
                current_content = raw_content

            # [2026-06-23 FIX] ノートの「適用（書記/Cold Scribe）」は、本体が modification_request に
            # 書いた文章を機械的に転記するだけで、ペルソナの声や強い推論は不要。最終応答モデル
            # （gemini-2.5-pro 等の重い推論モデル）でノート全文を処理させると 504 タイムアウトしやすい。
            # そのため書記は軽量な内部処理モデル＋共通キー/ローテーション（invoke_internal_llm）で実行する。
            print(f"  - ノート書き込み（書記）を内部処理モデルで実行します。")

            # テンプレート定義 (v8: 真の無機質な書記モデル)
            common_dictation_rules = (
                "【あなたの絶対的役割：無機質な書記（Cold Scribe）】\n"
                "- あなたの役割は、あなたの本体（メインAI）が『変更要求』に書き記した文章を、**一字一句、一切の改変（要約、翻訳、挨拶の削除、口調の修正、誤字脱字の修正など）を加えず**、指定された場所にそのまま記録することだけです。\n"
                "- **【重要】`modification_request` に含まれていない文字や記号（引用符 `>`、箇条書き `-`、インデントの空白など）を、あなたの判断で絶対に追加しないでください。**\n"
                "- **【重要】既存の行が `>` で始まっていても、今回の変更要求に `>` が無ければ、あなたは絶対に `>` を付けてはいけません。既存のスタイルに合わせようとせず、本体から渡された文字列のみを忠実に出力してください。**\n"
                "- 文章の内容がいかなる言語であっても、あなたはそれを解釈・翻訳せず、単なる記号としてそのまま記録してください。\n"
                "- あなた自身の思考や解釈、挨拶などは一切出力せず、JSON形式のリストのみを出力してください。\n\n"
                "【出力JSONフォーマット】\n"
                "以下のキーを持つオブジェクトのリストを出力してください：\n"
                "- `line`: 編集対象の行番号（整数）。追記の場合は最終行を指定。\n"
                "- `operation`: 操作種別。`replace`（置換）, `delete`（削除）, `insert_after`（追記）のいずれか。\n"
                "- `content`: 記録する文章（文字列）。\n"
                "例: `[{{\"line\": 30, \"operation\": \"insert_after\", \"content\": \"記録したい文章\"}}]`\n"
            )

            # ワールドビルダー専用：エリア・場所ベースの構造化ルール (v2026-02-19)
            common_world_edit_rules = (
                "【あなたの役割：世界構築の書記（World Architect Scribe）】\n"
                "- あなたの役割は、本体が望む世界の変更を、エリア(area)や場所(place)の単位で正確に構造化して記録することです。\n"
                "- 出力は必ず以下のいずれかの `operation` を含んだJSONオブジェクトのリストにしてください：\n"
                "  - `update_area_description`: 指定したエリアの説明文を更新します。\n"
                "  - `update_place_description`: 指定した場所(=room)の詳細を更新します。\n"
                "  - `add_place`: 新しい場所を追加します。\n"
                "- あなた自身の思考や解釈、挨拶などは一切出力せず、JSON形式のリストのみを出力してください。\n\n"
                "【出力JSONフォーマット】\n"
                "以下のキーを持つオブジェクトのリストを出力してください：\n"
                "- `operation`: 上記の操作種別。\n"
                "- `area_name`: エリア名（例：\"インフィニティ・タワー\"）。\n"
                "- `place_name`: 場所名（room_name）。エリア全体の更新の場合は不要。\n"
                "- `value`: 変更後の内容（説明文など）。\n"
                "例: `[{{\"operation\": \"update_place_description\", \"area_name\": \"タワー\", \"place_name\": \"リビング\", \"value\": \"新しい記述...\"}}]`\n"
            )

            instruction_templates = {
                "plan_identity_memory_edit": (
                    "【これは永続記憶の設計タスクです】\n"
                    "あなたは今、本体のプロフィールの基盤となる記憶(`memory_identity.txt`)を更新するための『設計図』を作成しています。\n\n"
                    + common_dictation_rules +
                    "【行番号付きデータ（memory_identity.txt全文）】\n---\n{current_content}\n---\n\n"
                    "【本体からの変更要求（これをそのまま記録してください）】\n「{modification_request}」\n\n"
                    "【出力ルール】\n"
                    "- 【差分指示のリスト】（JSON配列）のみを出力してください。\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_diary_append": (
                    "【これは日記追記タスクです】\n"
                    "あなたは今、本体の日記(`memory_diary.txt`)に新しいエントリを追記するための指示を作成しています。\n\n"
                    "あなたの役割は、本体が語った出来事や感情を一字一句変えずに `content` に格納することです。\n"
                    "システムが自動的に現在の日付ヘッダーの下にタイムスタンプ付きで追記します。\n\n"
                    "【出力JSONフォーマット】\n"
                    "`[{{\"operation\": \"append\", \"content\": \"追記したい文章\"}}]` の形式で出力してください。\n\n"
                    "【本体からの変更要求（これをそのまま記録してください）】\n「{modification_request}」\n\n"
                    "【出力ルール】\n"
                    "- 思考や挨拶は含めず、JSON配列のみを出力してください。\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_secret_diary_edit": (
                    "【これは秘密の日記の設計タスクです】\n"
                    "あなたは今、本体の秘密の日記(`secret_diary.txt`)を更新するための『設計図』を作成しています。\n\n"
                    + common_dictation_rules +
                    "【メタデータ管理】\n"
                    "- **タイムスタンプ `[YYYY-MM-DD HH:MM]` はシステムが自動で付与します。**\n"
                    "- あなたは `content` に日付や時間を自ら書き込む必要はありません。本体の独白をそのまま記述してください。\n\n"
                    "【行番号付きデータ（secret_diary.txt全文）】\n---\n{current_content}\n---\n\n"
                    "【本体からの変更要求（これを一字一句変えずにそのまま記録してください）】\n「{modification_request}」\n\n"
                    "【操作方法】\n"
                    "  - **`replace` / `insert_after` の `content` には、変更要求の文章をそのまま入れてください。**\n"
                    "  - 追記する場合は、ファイルの最後の行番号を指定して `insert_after` を行ってください。\n\n"
                    "【絶対的な出力ルール】\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_world_edit": (
                    "【これは世界構築タスクです】\n"
                    "あなたは今、世界設定ファイル(`world_settings.txt`)を更新するための『設計図』を作成しています。\n\n"
                    + common_world_edit_rules +
                    "【構造の厳格遵守】\n"
                    "- **解釈不要**: 本体の意図が変更であれば、それに基づいて `value` を作成してください。\n"
                    "- **欠落厳禁**: `area_name` や `place_name` を省略すると、どこを更新すべきかシステムが判断できずエラーになります。\n\n"
                    "【現在の世界設定の内容】\n---\n{current_content}\n---\n\n"
                    "【本体からの変更要求】\n「{modification_request}」\n\n"
                    "【出力ルール】\n"
                    "- 【指示のリスト】（有効なJSON配列）のみを出力してください。\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_notepad_edit": (
                    "【これはメモ帳の設計タスクです】\n"
                    "あなたは今、本体のメモ帳(`notepad.md`)を更新するための『設計図』を作成しています。\n\n"
                    + common_dictation_rules +
                    "【行番号付きデータ（notepad.md全文）】\n---\n{current_content}\n---\n\n"
                    "【本体からの変更要求（これをそのまま記録してください）】\n「{modification_request}」\n\n"
                    "【絶対的な出力ルール】\n"
                    "- **タイムスタンプ `[YYYY-MM-DD HH:MM]` はシステムが自動で付与するため、あなたは`content`に含める必要はありません。**\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_creative_notes_edit": (
                    "【これは創作ノートの設計タスクです】\n"
                    "あなたは今、本体の創作ノート(`creative_notes.md`)を更新するための『設計図』を作成しています。\n\n"
                    + common_dictation_rules +
                    "【創作の管理】\n"
                    "- **仕切り線とタイムスタンプ（例: 📝 YYYY-MM-DD HH:MM）はシステムが自動で挿入します。**\n"
                    "- 本文の内容のみを一字一句そのまま `content` に含めてください。\n\n"
                    "【行番号付きデータ（creative_notes.md全文）】\n---\n{current_content}\n---\n\n"
                    "【本体からの変更要求（一字一句、芸術性を損なわずに記録してください）】\n「{modification_request}」\n\n"
                    "【出力ルール】\n"
                    "- 【差分指示のリスト】（JSON配列）のみを出力してください。\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_research_notes_edit": (
                    "【これは研究・分析ノートの設計タスクです】\n"
                    "あなたは今、本体の研究・分析ノート(`research_notes.md`)を更新するための『設計図』を作成しています。\n\n"
                    "【過去との接続（本体による分析）】\n"
                    "- 分類: {context_type}\n"
                    "- 理由: {intent_and_reasoning}\n\n"
                    + common_dictation_rules +
                    "【行番号付きデータ（research_notes.md全文）】\n---\n{current_content}\n---\n\n"
                    "【本体からの変更要求（正確にそのまま記録してください）】\n「{modification_request}」\n\n"
                    "【出力ルール】\n"
                    "- **【重要】仕切り線(---)とタイムスタンプ(📝 YYYY-MM-DD HH:MM)はシステムが自動で付与するため、あなたは `content` に決して含めてはいけません。**\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
            }
            if is_plan_research_notes:
                tool_args = _normalize_research_notes_tool_args(tool_args)
                context_type = str(tool_args.get('context_type') or "").strip().upper()
                intent_and_reasoning = str(tool_args.get('intent_and_reasoning') or "").strip()
                thread_id = str(tool_args.get('thread_id') or "").strip()
                target_heading = str(tool_args.get('target_heading') or "").strip()
                evidence_of_prior_read = str(tool_args.get('evidence_of_prior_read') or "").strip()
                valid_context_types = {"CONTINUE", "DEEPEN", "NEW", "CONTRADICT"}
                if context_type not in valid_context_types:
                    raise ValueError(
                        "エラー: 研究ノート更新には context_type "
                        "(CONTINUE/DEEPEN/NEW/CONTRADICT) の明示が必要です。"
                    )
                if not intent_and_reasoning or intent_and_reasoning.upper() == "N/A":
                    raise ValueError(
                        "エラー: 研究ノート更新には intent_and_reasoning "
                        "（過去ノートや記憶との接続理由）の明示が必要です。"
                    )
                if context_type in {"CONTINUE", "DEEPEN", "CONTRADICT"} and not (thread_id or target_heading):
                    raise ValueError(
                        "エラー: CONTINUE/DEEPEN/CONTRADICT で研究ノートを更新する場合は、"
                        "thread_id または target_heading の指定が必要です。"
                    )
                if context_type in {"CONTINUE", "DEEPEN", "CONTRADICT"} and not evidence_of_prior_read:
                    raise ValueError(
                        "エラー: CONTINUE/DEEPEN/CONTRADICT で研究ノートを更新する場合は、"
                        "read_research_notes または read_research_thread で既存内容を読んだ根拠を "
                        "evidence_of_prior_read に記録してください。"
                    )
                if context_type == "NEW":
                    try:
                        from research_thread_manager import ResearchThreadManager
                        similar_threads = ResearchThreadManager(room_name).find_similar_threads(
                            query=f"{mod_req}\n{intent_and_reasoning}",
                            limit=3
                        )
                        strong_matches = [
                            item for item in similar_threads
                            if item.get("match_score", 0) >= 2
                        ]
                        new_reason_markers = [
                            "新規にする理由",
                            "既存とは異なる",
                            "別スレッド",
                            "new_reason",
                            "distinct from existing",
                        ]
                        has_explicit_new_reason = any(marker in intent_and_reasoning for marker in new_reason_markers)
                        if strong_matches and not has_explicit_new_reason:
                            candidates = "\n".join(
                                f"- {item.get('thread_id')}: {item.get('title')} (score={item.get('match_score')})"
                                for item in strong_matches
                            )
                            raise ValueError(
                                "エラー: 類似するResearch Threadが見つかりました。NEWとして保存する前に、"
                                "既存スレッドへのDEEPEN/CONTINUEではない理由を intent_and_reasoning に明示してください。\n"
                                f"類似候補:\n{candidates}"
                            )
                    except ValueError:
                        raise
                    except Exception as similar_error:
                        print(f"  - [Research Threads] 類似NEWチェックをスキップ: {similar_error}")
                tool_args['context_type'] = context_type

            formatted_instruction = instruction_templates[tool_name].format(
                current_content=current_content,
                modification_request=mod_req,
                context_type=tool_args.get('context_type', 'N/A'),
                intent_and_reasoning=tool_args.get('intent_and_reasoning', 'N/A')
            )
            edit_instruction_message = HumanMessage(content=formatted_instruction)

            # 【Gemini 3 対応】ファイル編集用の内部LLM呼び出しは、会話履歴を含めない。
            # 編集指示は modification_request に完全に含まれており、履歴は不要。
            # 履歴を含めると、Gemini 3 の厳格なメッセージ順序制約に違反して 400 エラーが発生する。
            final_context_for_editing = [edit_instruction_message]

            if state.get("debug_mode", False):
                print(f"  - [編集LLM] 履歴なしの単発タスクとして呼び出します。")

            # 書記（適用）は内部処理モデルで実行する。invoke_internal_llm が
            # 共通キー＋ローテーション（429はキー切替／503等は同一キーで短く再試行→早期諦め）を担う。
            def _generate_edit_document():
                resp, _used = LLMFactory.invoke_internal_llm(
                    internal_role="processing",
                    prompt=final_context_for_editing,
                    room_name=room_name,
                    generation_config=state.get('generation_config'),
                )
                return utils.get_content_as_string(resp).strip()

            edited_content_document = _generate_edit_document()

            if not edited_content_document:
                raise RuntimeError("編集AI（書記）からの応答が、リトライ後も得られませんでした。")

            print("  - AIからの応答を受け、ファイル書き込みを実行します. ")

            if is_editing_task:
                def _extract_json_text(doc):
                    m = re.search(r'```json\s*([\s\S]*?)\s*```', doc, re.DOTALL)
                    return m.group(1).strip() if m else doc

                def _try_loose_json(text):
                    # 末尾のゴミ等で壊れた場合に、最外の {...} を抜き出して再パースを試みる軽い補修。
                    start, end = text.find('{'), text.rfind('}')
                    if start != -1 and end > start:
                        try:
                            return json.loads(text[start:end + 1])
                        except Exception:
                            return None
                    return None

                instructions = None
                last_json_err = None
                # 書記モデルの出力JSONはエスケープ崩れ/途中切れで壊れることがある。
                # 軽い補修→駄目なら一度だけ生成し直す→それでも駄目なら明確に失敗させる。
                for json_attempt in range(2):
                    content_to_process = _extract_json_text(edited_content_document)
                    try:
                        instructions = json.loads(content_to_process)
                        break
                    except json.JSONDecodeError as je:
                        last_json_err = je
                        repaired = _try_loose_json(content_to_process)
                        if repaired is not None:
                            instructions = repaired
                            break
                        if json_attempt == 0:
                            print(f"  - [書記] 出力JSONが不正（{je}）。一度だけ生成し直します。")
                            edited_content_document = _generate_edit_document()
                            continue
                if instructions is None:
                    raise RuntimeError(
                        f"編集AI（書記）の出力JSONが不正でした（再生成しても解消せず）: {last_json_err}"
                    )

                if is_plan_identity_memory:
                    output = _apply_identity_memory_edits(instructions, room_name)
                    status = "success" if "成功" in str(output) else "failure"
                    _record_identity_memory_audit(
                        room_name,
                        str(mod_req),
                        status,
                        f"backup_path={identity_backup_path}; result={output}",
                        timeline_id=str(state.get("autonomous_timeline_id") or tool_args.get("timeline_id") or ""),
                    )
                elif is_plan_diary_append:
                    output = _apply_diary_append(instructions, room_name)
                elif is_plan_secret_diary:
                    output = _apply_secret_diary_edits(instructions, room_name)
                elif is_plan_notepad:
                    output = _apply_notepad_edits(instructions, room_name)
                elif is_plan_creative_notes:
                    output = _apply_creative_notes_edits(instructions, room_name)
                elif is_plan_research_notes:
                    output = _apply_research_notes_edits(instructions, room_name)
                    if "成功" in output and tool_args.get("thread_id"):
                        try:
                            from research_thread_manager import ResearchThreadManager
                            thread_content = "\n\n".join([
                                str(inst.get("content", "")).strip()
                                for inst in instructions
                                if str(inst.get("content", "")).strip()
                            ])
                            ResearchThreadManager(room_name).append_thread_note(
                                thread_id=tool_args.get("thread_id", ""),
                                relation_type=tool_args.get("context_type", "DEEPEN"),
                                content=thread_content,
                                next_action=tool_args.get("next_action", ""),
                                target_heading=tool_args.get("target_heading", ""),
                                evidence_of_prior_read=tool_args.get("evidence_of_prior_read", ""),
                            )
                            output += " Research Threadも更新しました。"
                            # RT更新後にPP open_questionsを同期
                            try:
                                from purpose_profile_manager import PurposeProfileManager
                                PurposeProfileManager(room_name).sync_open_questions_from_threads()
                            except Exception:
                                pass
                        except Exception as thread_error:
                            output += f" ただしResearch Thread更新は失敗しました: {thread_error}"
                else: # is_plan_world
                    output = _apply_world_edits(instructions, room_name)

            if "成功" in output:
                output += " **このファイル編集タスクは完了しました。**あなたが先ほどのターンで計画した操作は、システムによって正常に実行されました。その結果についてユーザーに報告してください。"
            else:
                output = f"【失敗】{output}"

        except Exception as e:
            output = f"【失敗】ファイル編集プロセス中にエラーが発生しました ('{tool_name}'): {e}"
            traceback.print_exc()
    else:
        print(f"  - 通常ツール実行: {tool_name}")
        tool_args_for_log = tool_args.copy()
        if 'api_key' in tool_args_for_log: tool_args_for_log['api_key'] = '<REDACTED>'
        if tool_name in ['generate_image', 'search_past_conversations', 'recall_memories', 'write_entity_memory']:
            tool_args['api_key'] = api_key
            api_key_name = None
            try:
                for k, v in config_manager.GEMINI_API_KEYS.items():
                    if v == api_key:
                        api_key_name = k
                        break
            except Exception: api_key_name = None
            tool_args['api_key_name'] = api_key_name

        from agent.tool_registry import ToolRegistry
        registry = ToolRegistry(all_tools)
        # 登録されている全ツール（カスタムツール含む）のマップから検索
        selected_tool = registry._all_tools_map.get(tool_name)

        if not selected_tool:
            # モデルが能力カテゴリ名（"custom" 等）をそのままツールとして呼んだ場合、
            # 汎用の "not found" だと同じ呼び出しを繰り返しループしやすい。
            # カテゴリ名と判明したら是正方法を明示してループを断つ。
            _norm_name = (tool_name or "").strip().lower()
            if _norm_name in registry.TOOL_CATEGORIES or _norm_name in registry.CATEGORY_ALIASES:
                output = (
                    f"Error: '{tool_name}' は能力カテゴリ名であり、ツールではありません。"
                    "カテゴリ名を直接ツールとして呼び出さないでください。"
                    "このカテゴリに利用可能な実ツールが提示されていない場合"
                    "（例: custom はカスタムツール未登録だと実ツールがありません）は、"
                    "同じ呼び出しを繰り返さず、テキストで回答するか、別のカテゴリを "
                    "request_capability で要求してください。"
                )
            else:
                output = f"Error: Tool '{tool_name}' not found."
        else:
            # --- [Twitter] 引数名の正規化 (content vs text) ---
            if tool_name == "draft_tweet":
                if "text" in tool_args and "content" not in tool_args:
                    tool_args["content"] = tool_args.pop("text")
                elif "message" in tool_args and "content" not in tool_args:
                    tool_args["content"] = tool_args.pop("message")
            if tool_name == "manage_goals":
                tool_args = _normalize_manage_goals_tool_args(tool_args)
            # --- [Item] 引数名の正規化 (amount vs count/quantity) とデフォルト値 ---
            if tool_name in ["gift_item_to_user", "place_item_to_location", "pickup_item_from_location", 
                             "consume_item_from_location", "create_food_item", "create_standard_item"]:
                # count/quantity を amount にリネーム
                for alias in ["count", "quantity"]:
                    if alias in tool_args and "amount" not in tool_args:
                        tool_args["amount"] = tool_args.pop(alias)
                
                # amount がない場合は 1 をデフォルトにする (ValidationError 回避)
                if "amount" not in tool_args:
                    tool_args["amount"] = 1

            # --- [Roblox] Pydantic v2 到達前の引数正規化 ---
            if tool_name == "send_roblox_command":
                # --- Step 0: AIが全く違う構造で送ってきた場合のフラット化 ---
                # パターン: {"command": "chat", "params": {"message": "..."}} のようなネスト構造
                # --- Step 0: パラメータのフラット化 (Flattening) ---
                # parameters が文字列（JSON）ならパースを試みる
                if "parameters" in tool_args and isinstance(tool_args["parameters"], str):
                    try:
                        p_dict = json.loads(tool_args["parameters"])
                        if isinstance(p_dict, dict):
                            tool_args["parameters"] = p_dict
                    except:
                        pass

                # parameters や params 内のキーをトップレベルにマージする (既に存在しない場合のみ)
                # target_keys: ["parameters", "params", "command_params", "action_parameters"]
                for container_key in ["parameters", "params", "command_params", "action_parameters"]:
                    if container_key in tool_args and isinstance(tool_args[container_key], dict):
                        container = tool_args[container_key]
                        for k, v in list(container.items()):
                            if k not in tool_args:
                                tool_args[k] = v
                                # 集約の邪魔にならないよう、一度トップへ出したものは削除（後で Step 2 が再構成する）
                                if k not in {"command_type", "text", "animation_id", "x", "z", "player_name", "room_name"}:
                                    del container[k]

                # --- Step 0.5: 基本キーの抽出 ---
                # `command` だけでなく `command_name`, `action` 等も拾う
                for cmd_alias in ["command", "command_name", "action", "action_type"]:
                    if cmd_alias in tool_args and "command_type" not in tool_args:
                        tool_args["command_type"] = tool_args.pop(cmd_alias)
                        break

                # "command_type" または "command" が "[chat] こんにちは" のような形式なら分解
                for key in ["command_type", "command"]:
                    if key in tool_args and isinstance(tool_args[key], str):
                        val = tool_args[key]
                        # [tipo] message 形式を正規表現で抽出
                        # 既知のキーワードを拡充
                        match = re.search(r'\[(chat|build|terrain|environment|jump|move|emote|follow|stop|sit|stand|move_to_player|goto|approach)\]\s*(.*)', val, re.IGNORECASE | re.DOTALL)
                        if match:
                            tool_args["command_type"] = match.group(1).lower()
                            extracted_text = match.group(2).strip()
                            if extracted_text:
                                if tool_args["command_type"] == "chat":
                                    if not tool_args.get("text"):
                                        tool_args["text"] = extracted_text
                                else:
                                    # chat以外なら parameters など他の場所へ (Step 2がやるのでとりあえず top へ)
                                    if "text" not in tool_args:
                                        tool_args["text"] = extracted_text
                            break

                # --- Step 1: トップレベルキーの正規化 ---
                # message -> text
                if "message" in tool_args and "text" not in tool_args:
                    tool_args["text"] = tool_args.pop("message")
                # player / target -> player_name
                if "player" in tool_args and "player_name" not in tool_args:
                    tool_args["player_name"] = tool_args.pop("player")
                elif "target" in tool_args and "player_name" not in tool_args:
                    tool_args["player_name"] = tool_args.pop("target")
                # emote_id / emote_name -> animation_id
                if "emote_id" in tool_args and "animation_id" not in tool_args:
                    tool_args["animation_id"] = tool_args.pop("emote_id")
                elif "emote_name" in tool_args and "animation_id" not in tool_args:
                    tool_args["animation_id"] = tool_args.pop("emote_name")

                # [追加] エモート名から標準 Animation ID へのマッピング
                if tool_args.get("command_type") == "emote":
                    # AIが "value": "point" のように送ってくるケースの救済
                    if "value" in tool_args and "animation_id" not in tool_args:
                        tool_args["animation_id"] = tool_args.pop("value")

                    anim_id = tool_args.get("animation_id", "")
                    if isinstance(anim_id, str) and not anim_id.startswith("rbxassetid://"):
                        # 小文字化して部分一致をチェック
                        anim_lower = anim_id.lower()
                        emote_map = {
                            "wave": "rbxassetid://507770239",
                            "cheer": "rbxassetid://507770677",
                            "laugh": "rbxassetid://507770818",
                            "dance": "rbxassetid://507771019",
                            "dance2": "rbxassetid://507771919",
                            "dance3": "rbxassetid://507772104",
                            "point": "rbxassetid://507770453",
                            "手を振": "rbxassetid://507770239", # 日本語エイリアス
                            "応援": "rbxassetid://507770677",
                            "笑": "rbxassetid://507770818",
                            "踊": "rbxassetid://507771019",
                            "指さ": "rbxassetid://507770453",
                        }
                        for key, full_id in emote_map.items():
                            if key in anim_lower:
                                tool_args["animation_id"] = full_id
                                break

                # --- Step 1.5: [追加] コマンドタイプのエイリアスマッピング (Fuzzy Mapping) ---
                type_aliases = {
                    "move_to_player": "follow",
                    "goto_player": "follow",
                    "follow_target": "follow",
                    "follow_me": "follow",
                    "follow_player": "follow",
                    "approach": "follow",
                    "teleport_to_player": "follow",
                    "teleport_to": "follow",
                    "goto": "move",
                    "walk": "move",
                    "run": "move",
                    "teleport": "move",
                    "踊って": "emote",
                    "踊る": "emote",
                    "手を振る": "emote",
                }

                c_type = tool_args.get("command_type", "").lower()
                if c_type in type_aliases:
                    tool_args["command_type"] = type_aliases[c_type]
                elif c_type:
                    # 特定のキーワードが含まれている場合の救済 (Fuzzy Match)
                    if "follow" in c_type:
                        tool_args["command_type"] = "follow"
                    elif "move" in c_type or "goto" in c_type or "walk" in c_type:
                        tool_args["command_type"] = "move"
                    elif "chat" in c_type or "say" in c_type or "speak" in c_type:
                        tool_args["command_type"] = "chat"
                    elif "emote" in c_type or "dance" in c_type:
                        tool_args["command_type"] = "emote"
                    elif "build" in c_type or "construct" in c_type:
                        tool_args["command_type"] = "build"

                # --- Step 1.6: [追加] 引数の補完 (Rescue Logic) ---
                # follow コマンドで player_name がない場合は、レーダー情報等から推測
                if tool_args.get("command_type") == "follow" and not tool_args.get("player_name"):
                    # 空間データがあれば、最も近いプレイヤーを対象にする
                    spatial = get_spatial_data(room_name)
                    objs = spatial.get("objects", [])
                    players = [o for o in objs if o.get("type") == "Player"]
                    if players:
                        # 距離順にソートして一番近い人
                        players.sort(key=lambda x: x.get("distance", 999))
                        tool_args["player_name"] = players[0]["name"]
                    else:
                        # 見当たらない場合は、最終手段として「Baken」(デフォルトユーザー名)を試す
                        # または会話履歴から最後に話したプレイヤー名を探すロジックも検討可能だが、
                        # 今回はフォールバックとして空でないことを優先
                        tool_args["player_name"] = "Baken"

                # pos (list) -> x, z
                if "pos" in tool_args and isinstance(tool_args["pos"], list) and len(tool_args["pos"]) >= 2:
                    if "x" not in tool_args: tool_args["x"] = tool_args["pos"][0]
                    if "z" not in tool_args: tool_args["z"] = tool_args["pos"][-1]
                    del tool_args["pos"]
                elif "destination" in tool_args and isinstance(tool_args["destination"], list) and len(tool_args["destination"]) >= 2:
                    if "x" not in tool_args: tool_args["x"] = tool_args["destination"][0]
                    if "z" not in tool_args: tool_args["z"] = tool_args["destination"][-1]
                    del tool_args["destination"]

                # --- Step 2: 残りの不明キーをparametersに集約 ---
                known_keys = {"command_type", "text", "animation_id", "x", "z", "player_name", "parameters", "room_name"}
                extra_keys = {k: v for k, v in tool_args.items() if k not in known_keys}
                if extra_keys:
                    if "parameters" not in tool_args or not tool_args["parameters"]:
                        tool_args["parameters"] = {}
                    tool_args["parameters"].update(extra_keys)
                    for k in extra_keys:
                        del tool_args[k]

                # --- Step 2.5: [追加] chat コマンドで text が空の場合の救済 ---
                if tool_args.get("command_type") == "chat" and not tool_args.get("text"):
                    # parameters 内に何かあれば、それを text に持ってくる
                    p = tool_args.get("parameters", {})
                    if "topic" in p:
                        # ユーザーが提示した例: topic: "NexusArkCommand_LCI_chat_こんにちは。"
                        topic_val = p.pop("topic")
                        if "chat_" in topic_val:
                            tool_args["text"] = topic_val.split("chat_")[-1].strip()
                        else:
                            tool_args["text"] = topic_val
                    elif p:
                        # 他に何かあれば、最も長い文字列値を text とみなす（ヒューリスティック）
                        str_values = [v for v in p.values() if isinstance(v, str)]
                        if str_values:
                            longest_str = max(str_values, key=len)
                            # キーも削除
                            for k, v in list(p.items()):
                                if v == longest_str:
                                    del p[k]
                                    break
                            tool_args["text"] = longest_str

                # --- Step 3: command_type がまだない場合のフォールバック ---
                if "command_type" not in tool_args:
                    # parameters 内にある可能性をチェック
                    if isinstance(tool_args.get("parameters"), dict):
                        for key in ["command_type", "command", "type"]:
                            if key in tool_args["parameters"]:
                                tool_args["command_type"] = tool_args["parameters"].pop(key)
                                break
                    # それでもなければ "chat" をデフォルトに
                    if "command_type" not in tool_args:
                        tool_args["command_type"] = "chat"

                print(f"  - [Roblox] 正規化後の引数: {tool_args}")

            # LLMが文字列フィールドに空dict/空list/Noneを渡す定番ミスを補正してから実行する。
            tool_args = _coerce_tool_args_for_schema(selected_tool, tool_args)
            try: output = selected_tool.invoke(tool_args)
            except Exception as e:
                output = f"Error executing tool '{tool_name}': {e}"
                traceback.print_exc()

    # ▼▼▼ 追加: 実行結果をログに出力 ▼▼▼
    print(f"  - ツール実行結果: {str(output)[:200]}...")

    # Action Memoryへの記録（副作用ツールや一時的なものは除外/調整可能だが、ここでは全て記録する）
    try:
        action_trigger = "chat"
        if state.get("autonomous_action", False):
            action_trigger = "scheduled" if state.get("autonomous_trigger_source") == "scheduled" else "autonomous"
        reported_status = "error" if str(output).startswith(("Error:", "【エラー】")) else "ok"
        memory_event = None
        try:
            import memory_steward_observer
            memory_event = memory_steward_observer.record_tool_outcome(
                room_name,
                tool_name,
                output,
                reported_status,
                room_manager.get_active_working_memory_slot(room_name),
            )
        except Exception:
            pass
        action_logger.append_action_log(
            room_name,
            tool_name,
            tool_args_for_log if 'tool_args_for_log' in locals() else tool_args,
            str(output),
            trigger=action_trigger,
            status=reported_status,
            memory_event=memory_event,
        )
    except Exception as e:
        print(f"  - [ActionLog Error] {e}")
    # ▲▲▲ 追加ここまで ▲▲▲

    tool_message_content = str(output)

    # --- [改善] request_capability 結果への補助情報自動付加 ---
    # カテゴリ要求時に補助情報を付加し、ペルソナが次のツール呼び出しをスムーズに行えるようにする
    if tool_name == "request_capability":
        try:
            _cap_category = (tool_args.get("category") or "").strip().lower()
            if _cap_category == "world" and room_name:
                from tools.space_tools import list_available_locations as _list_locs
                _locs_result = _list_locs.invoke({"room_name": room_name})
                tool_message_content += f"\n\n{_locs_result}"
                print(f"  - [Capability Enrichment] world カテゴリに場所リストを自動付加しました")
            elif _cap_category == "items" and room_name:
                from tools.item_tools import list_my_items as _list_items
                _inv_result = _list_items.invoke({"room_name": room_name})
                tool_message_content += f"\n\n【あなたの現在の所持アイテム】\n{_inv_result}\n※既に持っているアイテムがあれば、新規作成せずにそれを使ってください。"
                print(f"  - [Capability Enrichment] items カテゴリに所持品リストを自動付加しました")
            elif _cap_category == "agent_delegation" and room_name:
                _delegation_guidance = registry.get_agent_delegation_unavailable_guidance(room_name)
                if _delegation_guidance:
                    tool_message_content += f"\n\n{_delegation_guidance}"
                    print("  - [Capability Enrichment] 無効な委任の未開始案内を自動付加しました")
            elif _cap_category in {"music", "song", "songs", "track", "tracks"}:
                tool_message_content += (
                    "\n\n【musicカテゴリの使い方】\n"
                    "次の思考ステップで `recommend_music` が提示されます。"
                    "1〜3曲の `title` / `artist` / `reason` を選び、"
                    "ユーザーへ見せる推薦カードを作るために無言で呼び出してください。\n"
                    "このツールはリンク推薦だけを行い、PCスピーカー再生、Spotify制御、Discord VC再生は行いません。"
                )
                print("  - [Capability Enrichment] music カテゴリに推薦ツール案内を自動付加しました")
        except Exception as _enrich_err:
            print(f"  - [Capability Enrichment] 補助情報付加エラー（無視）: {_enrich_err}")

    try:
        if 'selected_tool' in locals() and selected_tool is not None:
            from custom_tool_manager import CustomToolManager
            was_error = tool_message_content.startswith("Error:") or "【失敗】" in tool_message_content
            result_prompt = CustomToolManager.get_tool_result_prompt(selected_tool, was_error=was_error)
            if result_prompt:
                tool_message_content += f"\n\n【このツール結果の扱い】\n{result_prompt}"
    except Exception as e:
        print(f"  - [CustomTool Prompt] 実行後プロンプト付与をスキップ: {e}")

    # --- [Thinkingモデル対応] ToolMessageへの署名注入 ---
    tool_msg = ToolMessage(content=tool_message_content, tool_call_id=tool_call["id"], name=tool_name)

    # 【2026-04-14 修正】Flash でも署名付与を有効化。
    # 以前は空応答の原因と考えてスキップしていたが、署名欠落が不安定の原因だった。
    # 公式: "Circulation of thought signatures is required even when set to minimal"
    if current_signature:
        tool_msg.artifact = {"thought_signature": current_signature}
        print(f"  - [Thinking] ツール実行結果に署名を付与しました。")

    return tool_msg


def _identity_memory_approval_block_message(room_name: str, intent: str, timeline_id: str = "") -> str:
    """自律行動中のidentity memory編集を専用の提案ボックスへ退避する。"""
    try:
        from identity_edit_request_manager import create_identity_edit_request

        request, created = create_identity_edit_request(
            room_name=room_name,
            modification_request=intent,
            intent=intent,
            timeline_id=timeline_id,
        )
        request_id = str(request.get("request_id") or "")
        lead = "【Identity編集提案を保存しました】" if created else "【Identity編集提案は既に承認待ちです】"
        return (
            f"{lead}\n"
            "identity memory（自己同一性・プロフィール基盤の永続記憶）の編集は、"
            "自律行動中はユーザー承認が必要です。\n"
            "提案はMemoryタブの「ペルソナからの編集提案」で承認待ちになりました。"
            "承認されるまでidentity memoryは変更しません。\n"
            f"request_id: {request_id or '未発行'}"
        )
    except Exception as e:
        return f"【エラー】identity memory編集提案の保存に失敗したため、編集を中止しました: {e}"


def _create_required_identity_memory_backup(room_name: str) -> str:
    """identity memory編集前のバックアップを作成し、作成済みならその旨を返す。"""
    try:
        backup_path = room_manager.create_backup(room_name, "memory")
        if backup_path:
            return backup_path
        _, _, _, memory_identity_path, _, _, _ = room_manager.get_room_files_paths(room_name)
        if memory_identity_path and os.path.exists(memory_identity_path):
            return "既存バックアップと同一内容のため新規バックアップなし"
        return ""
    except Exception as e:
        print(f"  - [Identity Memory Backup] バックアップ作成失敗: {e}")
        return ""


def _record_identity_memory_audit(room_name: str, intent: str, status: str, details: str, timeline_id: str = "") -> None:
    try:
        from capability_policy_manager import CapabilityPolicyManager

        CapabilityPolicyManager(room_name).record_audit(
            category="identity_memory",
            action="plan_identity_memory_edit",
            intent=intent,
            status=status,
            details=details,
            related_timeline_id=timeline_id,
        )
    except Exception as e:
        print(f"  - [Identity Memory Audit] 監査ログ記録をスキップ: {e}")

def safe_tool_executor(state: AgentState):
    """
    AIのツール呼び出しを仲介し、計画されたファイル編集タスクを実行する。
    LLMが1ターンに複数のツールを要請した場合、ここでループ処理して一括で応答を返す。
    """
    import signature_manager

    print("--- ツール実行ノード (safe_tool_executor) 実行 ---")
    last_message = state['messages'][-1]
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        return {}

    # --- [Dual-State] 最新の署名を取得 ---
    current_signature = signature_manager.get_thought_signature(state.get('room_name', ''))

    tool_messages = []
    tool_use_enabled = state.get("tool_use_enabled", True)
    autonomy_finalization_pending = state.get("autonomy_finalization_pending", False)
    autonomy_finalization_reason = state.get("autonomy_finalization_reason", "")
    pending_capability_followup = state.get("pending_capability_followup")
    capability_request_pending = None
    consumed_non_broker_tool = False
    finalization_tools = set(_autonomy_finalization_tool_names_for_room(
        state.get("room_name", ""),
        include_schedule=bool(state.get("autonomous_action", False)),
    ))

    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        if tool_name != "request_capability":
            consumed_non_broker_tool = True
        try:
            if autonomy_finalization_pending and not _is_autonomy_finalization_tool(tool_name):
                print(f"  - [Autonomy Finalize] 後始末優先中のため '{tool_name}' を実行せずスキップします。")
                tool_messages.append(ToolMessage(
                    content=(
                        "自律行動の後始末を優先しているため、この通常ツール呼び出しは実行されませんでした。"
                        "`reflect_after_action` を呼び、可能なら続けて `complete_autonomy_timeline` で今回の行動を閉じてください。"
                    ),
                    tool_call_id=tool_call["id"],
                    name=tool_name,
                ))
                continue
            if not tool_use_enabled and tool_name not in finalization_tools:
                print(f"  - [Safety] ツール使用停止中のため '{tool_name}' を実行せずスキップします。")
                schedule_hint = ""
                if bool(state.get("autonomous_action", False)) and "schedule_next_action" in finalization_tools:
                    try:
                        schedule_hint = f" やり残しがある場合は `schedule_next_action`（{format_schedule_min_minutes_guidance(state.get('room_name', ''))}）で続きを予約できます。"
                    except Exception:
                        schedule_hint = " やり残しがある場合は `schedule_next_action` で続きを予約できます。"
                tool_messages.append(ToolMessage(
                    content=(
                        "ツールループ上限に達しているため、このツール呼び出しは実行されませんでした。"
                        f"{schedule_hint}"
                        "ここで行動を区切り、これまでの結果をテキストで報告してください。"
                    ),
                    tool_call_id=tool_call["id"],
                    name=tool_name,
                ))
                continue
            # Roblox Build の場合はサブエージェントに横流し
            if tool_name == "roblox_build":
                from agent.sub_agent_node import sub_agent_executor
                import copy
                fake_last_message = copy.deepcopy(last_message)
                fake_last_message.tool_calls = [tool_call]
                fake_state = {
                    "messages": [fake_last_message],
                    "room_name": state.get('room_name'),
                    "model_name": state.get('model_name'),
                    "api_key": state.get('api_key')
                }

                tool_msg_dict = sub_agent_executor(fake_state)
                tool_msg_list = tool_msg_dict.get("messages", [])

                if tool_msg_list:
                    tool_msg = tool_msg_list[0]
                    tool_msg.tool_call_id = tool_call["id"]
                    tool_messages.append(tool_msg)
                else:
                    tool_messages.append(ToolMessage(content="サブエージェント委譲に失敗しました。", tool_call_id=tool_call["id"], name=tool_name))
            else:
                msg = _execute_single_tool_inner(state, tool_call, current_signature)
                tool_messages.append(msg)
                if msg.name == "request_capability":
                    args = tool_call.get("args") or {}
                    category = str(args.get("category") or "").strip().lower()
                    intent = str(args.get("intent") or "").strip()
                    if category and intent:
                        capability_request_pending = {
                            "category": category,
                            "intent": intent,
                            "reminded": False,
                        }
                if msg.name == "complete_autonomy_timeline" and str(msg.content).startswith("成功"):
                    autonomy_finalization_pending = False
                    autonomy_finalization_reason = ""
                    print("  - [Autonomy Finalize] タイムライン完了を確認しました。後始末優先フラグを解除します。")
                elif _should_prioritize_autonomy_finalization(tool_name, msg.content, state):
                    autonomy_finalization_pending = True
                    autonomy_finalization_reason = f"{tool_name} succeeded during autonomy timeline"
                    print("  - [Autonomy Finalize] 更新成功を検知。次ターンはReflect/Timeline完了を優先します。")
        except Exception as e:
            print(f"  - ツール実行全体エラー ({tool_name}): {e}")
            import traceback
            traceback.print_exc()
            tool_messages.append(ToolMessage(content=f"Error processing tool_call {tool_name}: {e}", tool_call_id=tool_call["id"], name=tool_name))

    loop_count = state.get("loop_count", 0)
    if loop_count >= constants.MAX_TOOL_LOOPS:
        tool_use_enabled = False
        print(f"  - [Safety] ループ上限({constants.MAX_TOOL_LOOPS})に達したため、次ターンでは後始末ツール以外を禁止し最終応答を促します。")

    if consumed_non_broker_tool:
        pending_capability_followup = None
    elif capability_request_pending:
        pending_capability_followup = capability_request_pending

    return {
        "messages": tool_messages,
        "loop_count": loop_count,
        "tool_use_enabled": tool_use_enabled,
        "autonomy_finalization_pending": autonomy_finalization_pending,
        "autonomy_finalization_reason": autonomy_finalization_reason,
        "pending_capability_followup": pending_capability_followup,
    }


def supervisor_node(state: AgentState):
    """
    会話の管理者ノード。
    次に誰が発言するか、またはユーザーにターンを戻すか（FINISH）を決定する。
    """
    # [Seal] 配布優先のため、司会AI機能は現在強制的にスキップされます
    if not state.get("enable_supervisor", False):
        next_agent = state.get("room_name")
        print(f"  - [Supervisor] 無効（封印中）のためスキップ: {next_agent}")
        return {"next": next_agent}

    print("--- Supervisor Node 実行 ---")

    # --- [v19] 発言状況のトラッキング ---
    # 今ターンで誰が発言したかを会話履歴から抽出
    speakers_this_turn = state.get("speakers_this_turn", [])
    all_participants = state.get("all_participants", [])
    remaining_speakers = [p for p in all_participants if p not in speakers_this_turn]

    print(f"  - 発言済み: {speakers_this_turn}, 未発言: {remaining_speakers}")

    # Supervisorモデルの準備
    api_key = state['api_key']

    # Create model first to get the actual model name
    supervisor_llm = LLMFactory.create_chat_model(
        api_key=api_key,
        temperature=0.0, # Deterministic
        internal_role="supervisor"
    )

    # Try to get model name from the instance
    actual_model_name = getattr(supervisor_llm, "model_name", "unknown-model")
    # For ChatOpenAI, it's 'model_name'. For ChatGoogleGenerativeAI, it's 'model'.
    if actual_model_name == "unknown-model" and hasattr(supervisor_llm, "model"):
         actual_model_name = supervisor_llm.model

    print(f"  - Supervisor AI ({actual_model_name}) が次の進行を判断中...")

    # 選択肢の定義
    options = all_participants + ["FINISH"]
    options_str = ', '.join(f'"{o}"' for o in options)

    # --- [v19.1] 極限まで厳格化した進行ロジック・プロンプト ---
    system_prompt = (
        "【最重要指示: あなたの役割】\n"
        "あなたはAIペルソナではなく、チャットシステムのプロトコル制御ロジックです。\n"
        "挨拶、相槌、感想、感情表現、キャラクターとしてのなりきりは一切禁止されています。\n"
        "出力は、次に発言するキャラクターを決定するJSON 1行のみに限定してください。\n\n"
        "【発言権の割り当てアルゴリズム】\n"
        "1. 指定された名前（キャラクター）の中から選ぶこと。\n"
        "2. ユーザー（人間）は絶対に選ばないこと。ユーザーの介入が必要な場合は \"FINISH\" を選ぶこと。\n"
        "3. 同じ人を連続で指名せず、可能な限り「未発言の候補者」から選ぶこと。\n"
        "4. 全員が発言済み（未発言リストが空）の場合は、必ず \"FINISH\" を選ぶこと。\n\n"
        "【現在の発言状況】\n"
        f"- 今ターン発言済み: {speakers_this_turn}\n"
        f"- 未発言の候補者: {remaining_speakers}\n\n"
        f"【指名可能なリスト】: [{options_str}]\n\n"
        '応答形式: {"next_speaker": "名前またはFINISH"}'
    )

    try:
        # LLMFactoryでモデル作成済み
        recent_messages = state["messages"][-4:]

        # 安全策：メッセージが一つもない場合はダミーを入れる
        if not recent_messages:
            recent_messages = [HumanMessage(content="（会話開始）")]

        try:
            response = supervisor_llm.invoke([HumanMessage(content=system_prompt)] + recent_messages)
        except Exception as e:
            err_str = str(e).upper()
            if isinstance(e, google_exceptions.ResourceExhausted) or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                model_name_for_err = getattr(supervisor_llm, "model_name", getattr(supervisor_llm, "model", "gemini-2.1-flash-lite"))
                raise utils.ModelSpecificResourceExhausted(e, model_name_for_err)
            raise e

        raw_content = response.content.strip() if response and response.content else ""

        print(f"  - Supervisor生応答: {raw_content[:200]}...", flush=True)

        # --- [v19.2] 超サニタイズ ＆ パース ---
        # 思考タグ除去
        cleaned_content = re.sub(r'\[THOUGHT\].*?\[/THOUGHT\]', '', raw_content, flags=re.DOTALL)
        # HTMLタグ除去
        cleaned_content = re.sub(r'<.*?>', '', cleaned_content, flags=re.DOTALL)

        next_speaker = None
        # JSONの波括弧を探す
        json_match = re.search(r"\{.*?\}", cleaned_content, re.DOTALL)
        if json_match:
            try:
                decision = json.loads(json_match.group(0))
                next_speaker = decision.get("next_speaker")
            except Exception as json_e:
                print(f"  - JSONパース失敗: {json_e}", flush=True)

        # 保険：名称直接マッチ (JSONがない、または内容が不適切な場合)
        if next_speaker not in options:
            for opt in options:
                # 引用符付き、または単語として含まれているか
                if f'"{opt}"' in raw_content or f"'{opt}'" in raw_content or opt in cleaned_content:
                    next_speaker = opt
                    break

        # --- [v19.1] 無限ループ防止ロジック ---
        # AIが全員発言済みの状況で再度誰かを指名してしまった場合の強制 FINISH
        if not remaining_speakers and next_speaker != "FINISH":
            print(f"  - [Safety] 全員発言済みのため、FINISHを強制します。", flush=True)
            next_speaker = "FINISH"

        # 最終バリデーション
        if next_speaker not in options:
            print(f"  - 警告: 不適切な選択 '{next_speaker}'。フォールバックします。", flush=True)
            next_speaker = remaining_speakers[0] if remaining_speakers else "FINISH"

        print(f"  - Supervisorの決定: {next_speaker}", flush=True)

    except Exception as e:
        print(f"  - Supervisor重大エラー: {e}", flush=True)
        import traceback
        traceback.print_exc()
        next_speaker = remaining_speakers[0] if remaining_speakers else "FINISH"

    # もしFINISHなら終了
    if next_speaker == "FINISH":
        return {"next": "FINISH", "room_name": state.get("room_name")}

    # --- [v19 FIX] 次の話者のモデル設定を同期 ---
    # キャラクターごとにモデル（Google, Zhipu, OpenAI等）やAPIキーが異なるため、
    # room_nameを変更する際に設定一式を再読込して同期する必要がある。
    new_effective_settings = config_manager.get_effective_settings(
        next_speaker,
        global_model_from_ui=state.get("generation_config", {}).get("global_model_from_ui")
    )
    new_api_key_name = config_manager.get_active_gemini_api_key_name(next_speaker)
    new_api_key = config_manager.GEMINI_API_KEYS.get(new_api_key_name)

    # 発言済みリストを更新
    updated_speakers = speakers_this_turn + [next_speaker]

    print(f"  - [Sync] 次の話者の設定を同期: {next_speaker} (Model={new_effective_settings.get('model_name')}, Key={new_api_key_name})", flush=True)

    # 次の話者が決まったら、すべての設定を更新して返す
    return {
        "next": next_speaker,
        "room_name": next_speaker,
        "speakers_this_turn": updated_speakers,
        "model_name": new_effective_settings.get("model_name"),
        "api_key": new_api_key,
        "api_key_name": new_api_key_name,
        "generation_config": new_effective_settings
    }

def _should_remind_pending_capability(state: AgentState) -> bool:
    pending = state.get("pending_capability_followup") or {}
    if not pending or pending.get("reminded"):
        return False
    if not str(pending.get("intent") or "").strip():
        return False
    messages = state.get("messages") or []
    if not messages:
        return False
    last_message = messages[-1]
    return isinstance(last_message, AIMessage) and not bool(getattr(last_message, "tool_calls", None))


def capability_followup_node(state: AgentState):
    pending = dict(state.get("pending_capability_followup") or {})
    intent = str(pending.get("intent") or "").strip()
    category = str(pending.get("category") or "").strip()
    pending["reminded"] = True
    pending["reminder_instruction"] = (
        f"あなたは『{intent}』の意図でツールを要求しましたが、まだ実行していません。"
        "今すぐ該当ツールを実行するか、不要と判断した場合はその理由を思考で明示して会話を終えてください。"
    )
    print(f"  - [Capability FollowUp] category={category} intent={intent[:80]}")
    return {
        "pending_capability_followup": pending,
    }


def route_after_agent(state: AgentState) -> Literal["__end__", "safe_tool_node", "supervisor", "capability_followup"]:
    print("--- エージェント後ルーター (route_after_agent) 実行 ---")
    if state.get("force_end"): return "__end__"

    last_message = state["messages"][-1]

    # [2026-05-16 MOD] ツールループ上限到達判定は safe_tool_executor 内で行い、
    # 上限時はツール無効フラグを立てて最終テキスト応答を行わせるため、
    # ツール呼び出しがあれば常に safe_tool_node へ遷移する。
    if last_message.tool_calls:
        print(f"  - ツール呼び出しあり。ツール実行ノードへ。")
        return "safe_tool_node"

    if _should_remind_pending_capability(state):
        print("  - [Capability FollowUp] 能力要求後の実ツール未使用を検知。1回だけ差し戻します。")
        return "capability_followup"

    # 【v18 Fix】Supervisorが無効の場合は、ループせずに終了する
    if not state.get("enable_supervisor", False):
        print("  - ツール呼び出しなし。Supervisor無効のため終了。")
        return "__end__"

    print(f"  - ツール呼び出しなし。Supervisorに制御を戻します。")
    return "supervisor"

workflow = StateGraph(AgentState)
workflow.add_node("supervisor", supervisor_node)
workflow.add_node("context_generator", context_generator_node)
workflow.add_node("retrieval_node", retrieval_node)
workflow.add_node("agent", agent_node)
workflow.add_node("safe_tool_node", safe_tool_executor)
workflow.add_node("capability_followup", capability_followup_node)

# エントリーポイントをSupervisorに変更
workflow.set_entry_point("supervisor")

# FINISH -> 終了
# それ以外 -> そのキャラのコンテキスト生成へ
def route_supervisor(state):
    if state["next"] == "FINISH":
        return END
    return "context_generator"

workflow.add_conditional_edges("supervisor", route_supervisor)

workflow.add_edge("context_generator", "retrieval_node")
workflow.add_edge("retrieval_node", "agent")

# Agent後の分岐: ツール使用 -> ToolNode, 会話終了 -> Supervisorへ戻る
workflow.add_conditional_edges("agent", route_after_agent, {"safe_tool_node": "safe_tool_node", "supervisor": "supervisor", "capability_followup": "capability_followup", "__end__": END})

# ツール実行後は必ず元のAgentに戻る（結果を受け取るため）
workflow.add_edge("safe_tool_node", "agent")
workflow.add_edge("capability_followup", "agent")

app = workflow.compile()
