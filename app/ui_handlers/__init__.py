import sys
import asyncio
import traceback
import logging
import glob
import subprocess
import gc
import ctypes
import gradio as gr
import tempfile
import shutil
from send2trash import send2trash
import psutil
import ast
import pandas as pd
from pandas import DataFrame
import json
import hashlib
import os
import html
import re
import locale
import subprocess
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Union, Any
import datetime
import tempfile
from typing import List, Optional, Dict, Any, Tuple, Iterator
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
import gradio as gr
import datetime
from PIL import Image, ImageOps
import threading
import filetype

# このモジュールは ui_handlers/ パッケージ配下にあるため、リポジトリルート
# （assets/ や RELEASE_NOTES.md が置かれている場所）は親ディレクトリになる。
# __file__ ベースでパスを組む箇所はこの定数を基準にする。
_UI_HANDLERS_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ストップボタン押下時にストリーミングジェネレータを自己停止させるためのフラグ
# Gradioのcancelsだけではジェネレータが確実に止まらないため、
# このEventを使ってジェネレータ自身がyieldを停止する
_stop_generation_event = threading.Event()
import zipfile
import base64
import io
import uuid
import base64
import io
import secrets
from pathlib import Path
import textwrap
from urllib.parse import quote
from tools.image_tools import generate_image as generate_image_tool_func
import pytz
import ijson
import time
import rag_manager
import utils
import atelier_app_grants
import agent_delegation.manager as agent_delegation_manager
import pricing
import usage_ledger
from weather_service import WeatherService
from tts_key_rotation import generate_audio_with_key_rotation
from tts_text_policy import TTS_MODE_SPLIT, prepare_tts_text_plan

# --- [2026-04-09] セッション分離型初期化ガード用の状態管理 ---
# session_hash -> {"completed": bool, "time": float, "room": str}
_session_init_states = {}

def _ensure_value_in_choices(choices: list, value) -> list:
    """valueがchoicesに含まれない場合、先頭に追加したリストを返す。"""
    if value and value not in choices:
        return [value] + list(choices)
    return list(choices)

def _perf_log(label: str, start: float, enabled: bool = True) -> float:
    now = time.perf_counter()
    if enabled:
        print(f"--- [PERF:init] {label}: {now - start:.3f}s ---")
    return now

def _get_session_id(request: gr.Request) -> str:
    """GradioのRequestからセッション識別子（session_hash）を取得する"""
    if request and hasattr(request, "session_hash"):
        return request.session_hash
    return "default"

def _get_session_init_room(session_id: str) -> Optional[str]:
    """
    セッションごとの「正解」とされるルーム名を取得する。
    メモリにない場合はconfig.jsonの最新値をフォールバックとして使用する。
    """
    if session_id == "default":
        # 内部処理(default)の場合はフォールバックを行わず、呼び出し側に委ねる
        return None

    state = _session_init_states.get(session_id, {})
    init_room = state.get("room")

    if not init_room:
        # メモリに情報がない（再起動後のゴーストセッション等）場合、configの値を「正解」とする
        config_manager.load_config()
        config = config_manager.CONFIG_GLOBAL
        init_room = config.get("last_room", "Default")
        # 暫定的にメモリにも記録して、2秒間のガード対象にする
        _session_init_states[session_id] = {
            "completed": True, # 既に存在しているセッションなので完了扱い
            "time": time.time(),
            "room": init_room
        }
        print(f"--- [Session:{session_id}] [Guard] ゴーストセッションを検知。configより '{init_room}' を正解として採用します。 ---")

    return init_room
# ---------------------------------------------------------

logger = logging.getLogger(__name__)

_MEM_DIAG_PROCESS = psutil.Process(os.getpid())
_MEM_DIAG_LAST_RSS_MB: Optional[float] = None
try:
    _MEM_DIAG_PROCESS.cpu_percent(None)
except Exception:
    pass


def _memory_diagnostics_enabled() -> bool:
    value = os.getenv("NEXUS_ARK_MEMORY_DIAGNOSTICS", "1")
    return value.strip().lower() not in {"0", "false", "off", "no"}


def log_memory_diagnostics(label: str, room_name: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> None:
    """RSS増減と主要キャッシュ数を軽量にログ出力する。"""
    if not _memory_diagnostics_enabled():
        return
    global _MEM_DIAG_LAST_RSS_MB
    try:
        mem = _MEM_DIAG_PROCESS.memory_info()
        rss_mb = mem.rss / (1024 * 1024)
        vms_mb = mem.vms / (1024 * 1024)
        delta = 0.0 if _MEM_DIAG_LAST_RSS_MB is None else rss_mb - _MEM_DIAG_LAST_RSS_MB
        _MEM_DIAG_LAST_RSS_MB = rss_mb
        cpu_percent = _MEM_DIAG_PROCESS.cpu_percent(None)
        thread_count = _MEM_DIAG_PROCESS.num_threads()

        cache_parts = []
        try:
            cache_parts.append(f"rag_managers={len(_rag_managers)}")
        except Exception:
            pass
        try:
            cache_parts.append(f"rag_index_cache={len(rag_manager.RAGManager._index_cache)}")
        except Exception:
            pass
        try:
            cache_parts.append(f"log_file_cache={len(getattr(utils, '_file_log_cache', {}))}")
        except Exception:
            pass
        try:
            cache_parts.append(f"log_count_cache={len(getattr(utils, '_file_message_count_cache', {}))}")
        except Exception:
            pass
        try:
            cache_parts.append(f"log_migration_cache={len(getattr(utils, '_MIGRATION_DONE_CACHE', set()))}")
        except Exception:
            pass
        try:
            cache_parts.append(f"actual_tokens={len(_LAST_ACTUAL_TOKENS)}")
        except Exception:
            pass

        details = []
        if room_name:
            details.append(f"room={room_name}")
        if extra:
            for key, value in extra.items():
                details.append(f"{key}={value}")
        details.extend(cache_parts)

        suffix = " ".join(details)
        if suffix:
            suffix = " " + suffix
        print(
            f"--- [MEM] {label}: rss={rss_mb:.1f}MB "
            f"delta={delta:+.1f}MB vms={vms_mb:.1f}MB "
            f"cpu={cpu_percent:.1f}% threads={thread_count}{suffix} ---"
        )
    except Exception as e:
        print(f"--- [MEM] {label}: 診断ログ取得失敗: {e} ---")


def _should_release_rag_cache_after_chat() -> bool:
    value = os.getenv("NEXUS_ARK_KEEP_RAG_INDEX_CACHE", "0")
    return value.strip().lower() not in {"1", "true", "on", "yes"}


def _release_rag_cache_after_chat(room_name: Optional[str] = None) -> None:
    """通常応答後はFAISSインデックスを保持し続けず、WSLメモリ圧迫を避ける。"""
    if not _should_release_rag_cache_after_chat():
        return
    try:
        if rag_manager.RAGManager._index_cache:
            log_memory_diagnostics("rag_cache_release:before", room_name)
            rag_manager.RAGManager.clear_cache()
            log_memory_diagnostics("rag_cache_release:after", room_name)
    except Exception as e:
        print(f"--- [RAGManager] 応答後キャッシュ解放に失敗しました: {e} ---")


def _trim_process_memory_after_chat(room_name: Optional[str] = None) -> None:
    """通常応答後に、Python/C拡張が解放済みの空き領域をOSへ返しやすくする。"""
    try:
        gc.collect()
        if os.name == "posix":
            try:
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except Exception:
                pass
    finally:
        log_memory_diagnostics("chat_stream:after_trim", room_name)


from pathlib import Path
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.docstore.document import Document

import gemini_api, config_manager, alarm_manager, room_manager, utils, constants, chatgpt_importer, claude_importer, generic_importer
import closet_manager
import gemini_explicit_cache_manager
from custom_tool_manager import CustomToolManager
try:
    import discord_manager
except ImportError:
    discord_manager = None
from tools import gemini_importer, timer_tools, memory_tools
from utils import _overwrite_log_file
from agent.scenery_manager import generate_scenery_context
from room_manager import get_room_files_paths, get_world_settings_path
from memory_manager import load_memory_data_safe, save_memory_data
from episodic_memory_manager import EpisodicMemoryManager
from motivation_manager import MotivationManager
from update_manager import UpdateManager

GROUP_SUPERVISOR_MAX_ROUNDS_HARD_LIMIT = 3

# --- 通知デバウンス用 ---
# 同一ルームへの連続通知を抑制するための変数
_last_save_notification_time = {}  # {room_name: timestamp}
NOTIFICATION_DEBOUNCE_SECONDS = 1.0

# --- RAGマネージャー管理用 ---
_rag_managers = {}

def get_rag_manager(room_name: str):
    """
    指定されたルームのRAGマネージャーを取得（または遅延初期化）する。
    """
    global _rag_managers
    if room_name not in _rag_managers:
        # APIキーの取得方法を修正
        effective_settings = config_manager.get_effective_settings(room_name)

        # 1. ルーム個別のキー設定を確認
        api_key_name = effective_settings.get("api_key_name")

        # 2. なければグローバル設定や前回の設定を確認
        if not api_key_name:
            api_key_name = effective_settings.get("last_api_key_name")

        # 3. それでもなければ最後の手段（config_managerから直接）
        if not api_key_name:
             api_key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name")

        # キー名から実際の値を取得
        api_key_val = config_manager.GEMINI_API_KEYS.get(api_key_name)

        # キーが見つからない、またはプレースホルダーの場合
        if not api_key_val or api_key_val.startswith("YOUR_API_KEY"):
            # 有効なキーが一つでもあればそれを使う（緊急策）
            valid_keys = [v for k, v in config_manager.GEMINI_API_KEYS.items() if v and not v.startswith("YOUR_API_KEY")]
            if valid_keys:
                api_key_val = valid_keys[0]
                print(f"[RAGManager] Using fallback API key for initialization.")
            else:
                print(f"[RAGManager] Warning: Valid API key not found for room '{room_name}'. RAG disabled.")
                return None

        print(f"[RAGManager] Initializing for room: {room_name}")
        try:
            _rag_managers[room_name] = rag_manager.RAGManager(room_name, api_key_val)
        except Exception as e:
            print(f"[RAGManager] Initialization failed: {e}")
            return None

    return _rag_managers[room_name]

# --- 起動時の通知抑制用 ---
# 初期化完了までは通知を抑制（handle_initial_loadで完了時にTrueにする）
_initialization_completed = False
_initialization_completed_time = 0  # 初期化完了時刻
POST_INIT_GRACE_PERIOD_SECONDS = 15  # 初期化完了後も15秒間は自動保存を抑制

# ルーム切り替え時の通知抑制用
_last_room_switch_time = 0
ROOM_SWITCH_GRACE_PERIOD_SECONDS = 5.0 # ルーム切り替え後の「余震」による保存通知を抑制する時間
PROGRAMMATIC_REPOPULATION_TTL_SECONDS = 120.0
_programmatic_room_setting_values = {}

# --- トークン数記録用 ---
_LAST_ACTUAL_TOKENS = {} # room_name -> {"prompt": int, "completion": int, "total": int}

# --- 音声再生キャッシュ用 ---
_tts_audio_cache = {} # (text_hash, room_name, provider, voice_id) -> filepath


def _should_skip_auto_settings_save(is_switching_room: bool = False) -> bool:
    """起動直後・ルーム切替中のUI同期イベントによる誤保存を防ぐ。"""
    now = time.time()
    return (
        not _initialization_completed
        or is_switching_room
        or (now - _initialization_completed_time) < POST_INIT_GRACE_PERIOD_SECONDS
        or (now - _last_room_switch_time) < ROOM_SWITCH_GRACE_PERIOD_SECONDS
    )


def _freeze_programmatic_value(value: Any) -> Any:
    """UI投入値を比較可能な形に正規化する。"""
    if isinstance(value, dict):
        return tuple(sorted((str(k), _freeze_programmatic_value(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple, set)):
        return tuple(_freeze_programmatic_value(v) for v in value)
    return value


def _extract_update_value(update_value: Any) -> tuple[bool, Any]:
    """gr.update(value=...) と通常値を、保存ガード用の値として取り出す。"""
    if isinstance(update_value, dict) and update_value.get("__type__") == "update":
        if "value" not in update_value:
            return False, None
        return True, update_value.get("value")
    return True, update_value


def _convert_room_nested_setting_delta_value(value: Any, value_type: str = "raw") -> Any:
    if value_type == "bool":
        return bool(value)
    if value_type == "int":
        return int(value)
    if value_type == "str":
        return value.strip() if isinstance(value, str) else value
    if value_type == "csv_list":
        return [v.strip() for v in (value or "").split(",") if v.strip()]
    return value


_FULL_ROOM_SETTING_OUTPUT_MAP = {
    20: ("delta", "tts_provider"),
    21: ("delta", "tts_profile_name"),
    22: ("delta", "tts_model"),
    23: ("delta", "tts_voice"),
    24: ("delta", "voice_style_prompt"),
    25: ("delta", "tts_voice_speed"),
    26: ("delta", "tts_voice_pitch"),
    27: ("delta", "tts_voice_intonation"),
    28: ("delta", "tts_voice_volume"),
    29: ("delta", "enable_typewriter_effect"),
    30: ("delta", "streaming_speed"),
    31: ("delta", "temperature"),
    32: ("delta", "top_p"),
    33: ("delta", "safety_block_threshold_harassment"),
    34: ("delta", "safety_block_threshold_hate_speech"),
    35: ("delta", "safety_block_threshold_sexually_explicit"),
    36: ("delta", "safety_block_threshold_dangerous_content"),
    37: ("delta", "display_thoughts"),
    38: ("delta", "send_thoughts"),
    39: ("delta", "enable_auto_retrieval"),
    40: ("delta", "add_timestamp"),
    41: ("delta", "send_current_time"),
    42: ("delta", "send_notepad"),
    43: ("delta", "use_common_prompt"),
    44: ("delta", "send_core_memory"),
    45: ("delta", "send_scenery"),
    46: ("delta", "scenery_send_mode"),
    47: ("delta", "auto_memory_enabled"),
    48: ("delta", "enable_self_awareness"),
    51: ("delta", "enable_scenery_system"),
    53: ("delta", "api_history_limit"),
    54: ("delta", "thinking_level"),
    56: ("delta", "episode_memory_lookback_days"),
    58: ("nested", "autonomous_settings", "enabled", "bool"),
    59: ("nested", "autonomous_settings", "inactivity_minutes", "int"),
    60: ("nested", "autonomous_settings", "allow_schedule_tool", "bool"),
    61: ("nested", "autonomous_settings", "schedule_cooldown_minutes", "int"),
    62: ("nested", "autonomous_settings", "autonomous_guidelines", "str"),
    63: ("nested", "autonomous_settings", "quiet_hours_start", "raw"),
    64: ("nested", "autonomous_settings", "quiet_hours_end", "raw"),
    75: ("delta", "model_name"),
    76: ("delta", "provider"),
    79: ("delta", "api_key_name"),
    80: ("nested", "openai_settings", "profile", "str"),
    81: ("nested", "openai_settings", "base_url", "str"),
    82: ("nested", "openai_settings", "api_key", "str"),
    83: ("nested", "openai_settings", "model", "str"),
    84: ("nested", "openai_settings", "tool_use_enabled", "bool"),
    85: ("delta", "enable_api_key_rotation"),
    94: ("nested", "sleep_consolidation", "update_episodic_memory", "bool"),
    95: ("nested", "sleep_consolidation", "update_memory_index", "bool"),
    96: ("nested", "sleep_consolidation", "update_current_log_index", "bool"),
    97: ("nested", "sleep_consolidation", "update_entity_memory", "bool"),
    98: ("nested", "sleep_consolidation", "compress_old_episodes", "bool"),
    99: ("nested", "sleep_consolidation", "extract_open_questions", "bool"),
    158: ("delta", "auto_summary_enabled"),
    159: ("delta", "auto_summary_threshold"),
    160: ("nested", "project_explorer", "root_path", "str"),
    161: ("nested", "project_explorer", "exclude_dirs", "csv_list"),
    162: ("nested", "project_explorer", "exclude_files", "csv_list"),
}

_FAST_ROOM_SETTING_OUTPUT_MAP = {
    **{index + 20: spec for index, spec in enumerate([
        ("delta", "tts_provider"),
        ("delta", "tts_model"),
        ("delta", "tts_voice"),
        ("delta", "voice_style_prompt"),
        ("delta", "tts_voice_speed"),
        ("delta", "tts_voice_pitch"),
        ("delta", "tts_voice_intonation"),
        ("delta", "tts_voice_volume"),
        ("delta", "temperature"),
        ("delta", "top_p"),
        ("delta", "safety_block_threshold_harassment"),
        ("delta", "safety_block_threshold_hate_speech"),
        ("delta", "safety_block_threshold_sexually_explicit"),
        ("delta", "safety_block_threshold_dangerous_content"),
        ("delta", "enable_typewriter_effect"),
        ("delta", "streaming_speed"),
        ("delta", "send_thoughts"),
        ("delta", "enable_auto_retrieval"),
        ("delta", "send_current_time"),
        ("delta", "send_notepad"),
        ("delta", "use_common_prompt"),
        ("delta", "send_core_memory"),
        ("delta", "send_scenery"),
        ("delta", "scenery_send_mode"),
        ("delta", "enable_scenery_system"),
        ("delta", "auto_memory_enabled"),
        ("delta", "enable_self_awareness"),
        ("delta", "api_history_limit"),
        ("delta", "thinking_level"),
        ("delta", "episode_memory_lookback_days"),
        ("nested", "autonomous_settings", "enabled", "bool"),
        ("nested", "autonomous_settings", "inactivity_minutes", "int"),
        ("nested", "autonomous_settings", "allow_schedule_tool", "bool"),
        ("nested", "autonomous_settings", "schedule_cooldown_minutes", "int"),
        ("nested", "autonomous_settings", "autonomous_guidelines", "str"),
        ("nested", "autonomous_settings", "quiet_hours_start", "raw"),
        ("nested", "autonomous_settings", "quiet_hours_end", "raw"),
        None, None, None, None, None, None, None, None, None, None,
        ("delta", "model_name"),
        ("delta", "provider"),
        None, None, None, None, None, None,
        ("delta", "api_key_name"),
        ("nested", "openai_settings", "profile", "str"),
        ("nested", "openai_settings", "base_url", "str"),
        ("nested", "openai_settings", "api_key", "str"),
        ("nested", "openai_settings", "model", "str"),
        ("nested", "openai_settings", "tool_use_enabled", "bool"),
        ("nested", "anthropic_settings", "model", "str"),
        ("nested", "claude_subscription_settings", "model", "str"),
        ("delta", "enable_api_key_rotation"),
        ("nested", "sleep_consolidation", "update_episodic_memory", "bool"),
        ("nested", "sleep_consolidation", "update_memory_index", "bool"),
        ("nested", "sleep_consolidation", "update_current_log_index", "bool"),
        ("nested", "sleep_consolidation", "update_entity_memory", "bool"),
        ("nested", "sleep_consolidation", "compress_old_episodes", "bool"),
        ("nested", "sleep_consolidation", "extract_open_questions", "bool"),
        ("delta", "auto_summary_enabled"),
        ("delta", "auto_summary_threshold"),
        ("nested", "project_explorer", "root_path", "str"),
        ("nested", "project_explorer", "exclude_dirs", "csv_list"),
        ("nested", "project_explorer", "exclude_files", "csv_list"),
    ]) if spec is not None}
}


def _remember_programmatic_room_settings(room_name: str, outputs: tuple, output_map: dict[int, tuple], offset: int = 0) -> None:
    """司令塔がUIへ投入した設定値を記録し、遅延.changeによる保存を捨てられるようにする。"""
    if not room_name:
        return
    now = time.time()
    expired = [
        key for key, entry in _programmatic_room_setting_values.items()
        if now - entry.get("time", 0) > PROGRAMMATIC_REPOPULATION_TTL_SECONDS
    ]
    for key in expired:
        _programmatic_room_setting_values.pop(key, None)

    for index, spec in output_map.items():
        output_index = offset + index
        if output_index >= len(outputs):
            continue
        has_value, raw_value = _extract_update_value(outputs[output_index])
        if not has_value:
            continue
        try:
            if spec[0] == "delta":
                key, converted, _label = _convert_room_setting_delta(spec[1], raw_value)
                cache_key = (room_name, "delta", key)
            else:
                _kind, parent_key, child_key, value_type = spec
                converted = _convert_room_nested_setting_delta_value(raw_value, value_type)
                cache_key = (room_name, "nested", parent_key, child_key)
        except Exception:
            converted = raw_value
            cache_key = (room_name, *spec[1:]) if spec[0] == "delta" else (room_name, "nested", spec[1], spec[2])
        _programmatic_room_setting_values[cache_key] = {
            "value": _freeze_programmatic_value(converted),
            "time": now,
        }


def _resolve_room_setting_save(room_name: str, cache_key: tuple, converted_value: Any, is_switching_room: bool) -> bool:
    """ルーム設定の差分保存を実行すべきか判定する（True=保存・False=スキップ）。

    司令塔がUIへ投入した値（programmatic）は読込時に網羅的に登録される。これを基準に：
    - 登録値と一致する遅延.change＝読込エコーは保存しない。
    - 登録値と異なる＝ユーザーの確定的な変更は、ルーム切替直後の grace 期間でも保存する。
      （プロバイダ変更などが grace に取りこぼされ、個別設定が不整合になるのを防ぐ）
    - 登録が無く判別できない場合（起動直後の競合など）のみ、従来の grace 抑止に従う。
    """
    now = time.time()
    entry = _programmatic_room_setting_values.get(cache_key)
    if entry:
        if now - entry.get("time", 0) <= PROGRAMMATIC_REPOPULATION_TTL_SECONDS:
            if entry.get("value") == _freeze_programmatic_value(converted_value):
                print(f"--- [Settings Guard] プログラム投入値の遅延.changeを保存スキップ: {cache_key[1:]} ({room_name}) ---")
                return False  # 読込エコー（エコーは繰り返し得るのでエントリは残す）
            # 登録値と異なる＝ユーザーの確定変更。grace を無視して保存する。
            _programmatic_room_setting_values.pop(cache_key, None)
            return True
        _programmatic_room_setting_values.pop(cache_key, None)
    return not _should_skip_auto_settings_save(is_switching_room)

def handle_save_global_setting_delta(
    key: str,
    value: Any,
    label: str,
    restart_required: bool = False,
    skip_grace: bool = False
) -> str:
    """共通設定の指定キーだけを差分保存し、保存状態表示用の文言を返す。"""
    if skip_grace:
        if not _initialization_completed:
            return gr.update()
    elif _should_skip_auto_settings_save(False):
        return gr.update()
    try:
        result = config_manager.update_config_keys({key: value})
        config_manager.load_config()
        status = _settings_status_message("共通設定", label, result, restart_required)
        if restart_required and result is True:
            gr.Info(status)
        elif result is False:
            gr.Error(status)
        return status
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"{label} の保存中にエラーが発生しました。")
        return f"共通設定: {label} の保存に失敗しました"






def handle_save_room_persona_workspace_settings(
    room_name: str,
    enabled: bool = True,
    permission_tier: str = "write",
    exclude_dirs: str | list[str] | None = None,
    exclude_files: str | list[str] | None = None,
) -> str:
    """このルームのアトリエ設定を persona_workspace フルブロックで保存する。"""
    try:
        room = str(room_name or "").strip()
        if not room:
            gr.Error("ルームを選択してください。")
            return "ルーム別設定: アトリエ設定の保存に失敗しました"
        tier_map = {
            "読み取り（Read/Glob/Grep）": "read",
            "読み書き（Edit/Writeはアトリエ内のみ）": "write",
            "フル（Bash許可・信頼アトリエ専用）": "full",
            "read": "read",
            "write": "write",
            "full": "full",
        }
        tier = tier_map.get(str(permission_tier or "").strip(), str(permission_tier or "write").strip() or "write")

        def _list(value, default):
            if value is None:
                return list(default or [])
            if isinstance(value, str):
                return [part.strip() for part in value.split(",") if part.strip()]
            return [str(part).strip() for part in value if str(part).strip()]

        current = (config_manager.get_effective_settings(room).get("persona_workspace", {}) or {})
        settings = {
            "enabled": bool(enabled),
            "permission_tier": tier,
            "exclude_dirs": _list(exclude_dirs, current.get("exclude_dirs", [".git", "__pycache__"])),
            "exclude_files": _list(exclude_files, current.get("exclude_files", ["*.pyc"])),
        }
        result = room_manager.update_room_override_key(room, "persona_workspace", settings)
        status = _settings_status_message(room, "アトリエ設定", result, False)
        if result:
            gr.Info(status)
        else:
            gr.Error(status)
        return status
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"アトリエ設定の保存中にエラーが発生しました: {e}")
        return "ルーム別設定: アトリエ設定の保存に失敗しました"


AGENT_DELEGATION_VISIBLE_STATUSES = {"done", "failed", "partial", "needs_clarification", "cancelled", "running", "pending"}




























































































































def handle_share_research_result_from_ui(task_id: str):
    """選択した委任タスクの調査結果を研究ノートへ取り込み、一覧・ログを再読み込みする。

    タスク自身のルームの研究ノートへ取り込む（ペルソナ用ツール share_research_result を流用）。
    成否は通知し、戻り値は refresh_agent_delegation_task_view() の4値。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        gr.Warning("研究ノートへ取り込むタスクを選択してください。")
        return refresh_agent_delegation_task_view()
    try:
        from tools.agent_delegation_tools import share_research_result
        task = agent_delegation_manager.check_task_status(task_id)
        room = str(task.get("room_name") or "").strip()
        if not room:
            gr.Warning("このタスクのルームを特定できませんでした。")
            return refresh_agent_delegation_task_view()
        message = share_research_result.invoke({"room_name": room, "task_id": task_id})
        if message.startswith("【リサーチ結果を研究ノートへ取り込みました】"):
            gr.Info("リサーチ結果を研究ノートへ取り込みました。")
        else:
            # すでに共有済み・成果物なし・エラー等は先頭行を警告として表示
            gr.Warning(message.splitlines()[0] if message else "取り込めませんでした。")
    except Exception as e:
        traceback.print_exc()
        gr.Warning(f"取り込めませんでした: {type(e).__name__}: {e}")
    return refresh_agent_delegation_task_view()















_ROOM_SETTING_LABELS = {
    "voice_id": "音声",
    "voice_style_prompt": "音声スタイル",
    "tts_provider": "TTSプロバイダ",
    "tts_model": "TTSモデル",
    "tts_voice": "TTS音声",
    "tts_response_format": "TTS出力形式",
    "temperature": "Temperature",
    "top_p": "Top-P",
    "safety_block_threshold_harassment": "安全設定",
    "safety_block_threshold_hate_speech": "安全設定",
    "safety_block_threshold_sexually_explicit": "安全設定",
    "safety_block_threshold_dangerous_content": "安全設定",
    "enable_typewriter_effect": "逐次表示",
    "streaming_speed": "表示速度",
    "display_thoughts": "思考表示",
    "send_thoughts": "思考送信",
    "enable_auto_retrieval": "記憶の想起",
    "add_timestamp": "タイムスタンプ",
    "send_current_time": "現在時刻送信",
    "send_notepad": "メモ帳送信",
    "use_common_prompt": "共通ツールプロンプト",
    "send_core_memory": "コアメモリ送信",
    "send_scenery": "情景画像共有",
    "scenery_send_mode": "情景画像の送信タイミング",
    "enable_scenery_system": "情景描写システム",
    "auto_memory_enabled": "自動記憶",
    "enable_self_awareness": "自己意識機能",
    "api_history_limit": "履歴送信",
    "thinking_level": "Thinking レベル",
    "episode_memory_lookback_days": "エピソード記憶の参照期間",
    "model_name": "ルームモデル",
    "provider": "ルームプロバイダ",
    "api_key_name": "ルームAPIキー",
    "enable_api_key_rotation": "APIキーローテーション",
    "auto_summary_enabled": "Auto Summary",
    "auto_summary_threshold": "Auto Summary 閾値",
    "tts_profile_name": "TTSプロファイル",
}

_SAFETY_VALUE_MAP = {
    "ブロックしない": "BLOCK_NONE",
    "低リスク以上をブロック": "BLOCK_LOW_AND_ABOVE",
    "中リスク以上をブロック": "BLOCK_MEDIUM_AND_ABOVE",
    "高リスクのみブロック": "BLOCK_ONLY_HIGH"
}

def _api_history_limit_key_from_ui(value: Any) -> Optional[str]:
    """履歴送信件数のUI表示値/内部キーを安全に内部キーへ正規化する。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
    if not value:
        return None
    if value in constants.API_HISTORY_LIMIT_OPTIONS:
        return value
    return next((k for k, v in constants.API_HISTORY_LIMIT_OPTIONS.items() if v == value), None)

def _convert_room_setting_delta(field: str, value: Any) -> Tuple[str, Any, str]:
    """UI値をroom_config.jsonへ保存するキー/値に変換する。"""
    if field == "voice_id":
        value = next((k for k, v in config_manager.SUPPORTED_VOICES.items() if v == value), None)
    elif field == "tts_provider":
        value = config_manager.tts_provider_key_from_display(value)
    elif field == "tts_model":
        value = value.strip() if isinstance(value, str) else value
    elif field == "tts_voice":
        # プロバイダ別の表示名からIDへ戻す。現在のUI値だけではプロバイダが分からないため、
        # 全プロバイダの既知表示名を順に見て、見つからなければカスタム値として保存する。
        resolved = None
        for provider_key in config_manager.TTS_PROVIDERS.keys():
            voice_map = config_manager.get_tts_voice_map(provider_key)
            resolved = next((k for k, v in voice_map.items() if v == value), None)
            if resolved:
                break
        value = resolved or (value.strip() if isinstance(value, str) else value)
    elif field == "tts_response_format":
        value = value or "wav"
    elif field == "voice_style_prompt":
        value = value.strip() if value else ""
    elif field in ("temperature", "top_p", "streaming_speed"):
        value = float(value)
    elif field.startswith("safety_block_threshold_"):
        value = _SAFETY_VALUE_MAP.get(value)
    elif field == "api_history_limit":
        value = _api_history_limit_key_from_ui(value)
        if value is None:
            raise ValueError("履歴送信件数が未同期のため保存をスキップします。")
    elif field == "episode_memory_lookback_days":
        value = next((k for k, v in constants.EPISODIC_MEMORY_OPTIONS.items() if v == value), constants.DEFAULT_EPISODIC_MEMORY_DAYS)
    elif field == "thinking_level":
        value = next((k for k, v in constants.THINKING_LEVEL_OPTIONS.items() if v == value), "auto")
    elif field == "provider":
        value = "default" if value == "default" else value
    elif field == "api_key_name":
        value = config_manager._clean_api_key_name(value) if value else None
    elif field == "enable_api_key_rotation":
        if value == "None":
            value = None
    elif field == "auto_summary_threshold":
        value = int(value)
    elif field == "model_name":
        value = value or None
    elif field == "tts_profile_name":
        value = value or None
    elif isinstance(value, bool):
        value = bool(value)
    label = _ROOM_SETTING_LABELS.get(field, field)
    return field, value, label

def handle_save_room_setting_delta(room_name: str, field: str, value: Any, is_switching_room: bool = False) -> str:
    """ルーム個別設定を1キーだけ差分保存する。"""
    if not room_name:
        gr.Warning("設定を保存するルームが選択されていません。")
        return "個別設定: ルームが選択されていません"
    try:
        key, converted, label = _convert_room_setting_delta(field, value)
        if not _resolve_room_setting_save(room_name, (room_name, "delta", key), converted, is_switching_room):
            return gr.update()

        # tts_provider_settings への同期（バックアップ）のロジック
        tts_fields = {
            "tts_model", "tts_voice", "voice_style_prompt",
            "tts_voice_speed", "tts_voice_pitch", "tts_voice_intonation", "tts_voice_volume",
            "tts_profile_name"
        }
        
        if key in tts_fields:
            # 現在のアクティブなプロバイダを取得
            effective = config_manager.get_effective_settings(room_name)
            current_provider = effective.get("tts_provider", "gemini")
            
            # tts_provider_settings 内の辞書を更新
            room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
            if os.path.exists(room_config_path):
                try:
                    with open(room_config_path, "r", encoding="utf-8") as f:
                        room_config = json.load(f)
                    overrides = room_config.get("override_settings", {})
                except Exception:
                    overrides = {}
            else:
                overrides = {}
                
            provider_settings = overrides.get("tts_provider_settings", {})
            if current_provider not in provider_settings:
                provider_settings[current_provider] = {}
                
            # プロバイダ用のキャッシュに値を書き込む
            provider_settings[current_provider][key] = converted
            
            # 一緒に保存するupdates辞書を組み立てる
            updates = {
                key: converted,
                "tts_provider_settings": provider_settings
            }
            
            result = room_manager.update_room_config(room_name, {"override_settings": updates})
        elif key == "display_thoughts" and converted is False:
            result = room_manager.update_room_config(room_name, {"display_thoughts": False, "send_thoughts": False})
        elif key == "provider" and converted == "default":
            # 共通設定へ戻す時も、個別AI設定の選択値は編集用ドラフトとして保持する。
            # 実行時は config_manager 側で provider が有効な場合だけ個別値を採用する。
            result = room_manager.update_room_override_key(room_name, "provider", "default")
        else:
            result = room_manager.update_room_override_key(room_name, key, converted)
            
        status = _settings_status_message(room_name, label, result)
        if result is False:
            gr.Error(status)
        return status
    except ValueError as e:
        if "保存をスキップ" in str(e):
            return gr.update()
        traceback.print_exc()
        gr.Error("個別設定の保存中にエラーが発生しました。")
        return f"{room_name}: 設定の保存に失敗しました"
    except Exception:
        traceback.print_exc()
        gr.Error("個別設定の保存中にエラーが発生しました。")
        return f"{room_name}: 設定の保存に失敗しました"

def handle_save_room_ai_field_delta(
    room_name: str,
    field: str,
    value: Any,
    provider_value: Any,
    is_switching_room: bool = False,
) -> str:
    """個別AI設定(model_name / api_key_name / enable_api_key_rotation)を保存する際、
    現在のプロバイダ選択(provider_value)も一緒に永続化する。

    これらのキーは get_effective_settings 側で「provider override が無いと丸ごと無視」される。
    プロバイダ変更の単独保存が grace 期間でスキップされても、モデル/キー保存に provider を
    相乗りさせることで『モデルだけ保存され provider 欠落→個別モデルが効かない』不整合を防ぐ。
    """
    if not room_name:
        gr.Warning("設定を保存するルームが選択されていません。")
        return "個別設定: ルームが選択されていません"
    try:
        key, converted, label = _convert_room_setting_delta(field, value)
        if not _resolve_room_setting_save(room_name, (room_name, "delta", key), converted, is_switching_room):
            return gr.update()
        # プロバイダをラジオの現在値から正規化して相乗り保存（"default" は明示的な共通設定として残す）
        _, provider_converted, _ = _convert_room_setting_delta("provider", provider_value)
        updates = {key: converted, "provider": provider_converted}
        result = room_manager.update_room_config(room_name, {"override_settings": updates})
        status = _settings_status_message(room_name, label, result)
        if result is False:
            gr.Error(status)
        return status
    except ValueError as e:
        if "保存をスキップ" in str(e):
            return gr.update()
        traceback.print_exc()
        gr.Error("個別設定の保存中にエラーが発生しました。")
        return f"{room_name}: 設定の保存に失敗しました"
    except Exception:
        traceback.print_exc()
        gr.Error("個別設定の保存中にエラーが発生しました。")
        return f"{room_name}: 設定の保存に失敗しました"


def handle_save_room_nested_setting_delta(
    room_name: str,
    parent_key: str,
    child_key: str,
    value: Any,
    label: str,
    value_type: str = "raw",
    is_switching_room: bool = False
) -> str:
    """ルーム個別設定のネスト辞書を、子キー単位で差分保存する。"""
    if not room_name:
        gr.Warning("設定を保存するルームが選択されていません。")
        return "個別設定: ルームが選択されていません"
    try:
        value = _convert_room_nested_setting_delta_value(value, value_type)
        if not _resolve_room_setting_save(room_name, (room_name, "nested", parent_key, child_key), value, is_switching_room):
            return gr.update()

        result = room_manager.update_room_override_nested(room_name, parent_key, {child_key: value})
        status = _settings_status_message(room_name, label, result)
        if result is False:
            gr.Error(status)
        return status
    except Exception:
        traceback.print_exc()
        gr.Error(f"{label} の保存中にエラーが発生しました。")
        return f"{room_name}: {label} の保存に失敗しました"


def _gemini_explicit_cache_status_text(room_name: str) -> str:
    if not room_name:
        return "現在の状態: ルーム未選択"
    settings = gemini_explicit_cache_manager.get_room_settings(room_name)
    if not settings.get("enabled"):
        state = gemini_explicit_cache_manager.load_state(room_name)
        if state.get("cache_name"):
            return "現在の状態: 休眠中（TTL満了まで保持 / 再ONで再利用）"
        return "現在の状態: OFF"
    reason = gemini_explicit_cache_manager.get_disabled_reason(room_name)
    if reason == "provider_not_google":
        return "現在の状態: ON（Googleプロバイダ選択時のみ使用）"
    if reason == "api_key_rotation_enabled":
        return "現在の状態: ON（APIキーローテーション有効中は使用しません）"
    state = gemini_explicit_cache_manager.load_state(room_name)
    if state.get("cache_name"):
        tools_count = len(state.get("tool_names") or [])
        return f"現在の状態: ON（次回使用: {tools_count} tools / TTL {settings['ttl_minutes']}分）"
    return f"現在の状態: ON（次回会話時に作成 / TTL {settings['ttl_minutes']}分）"


def load_room_gemini_explicit_cache_settings(room_name: str):
    """ルーム切替・初期化時に Gemini Explicit キャッシュ設定をUIへ読み込む。"""
    try:
        settings = gemini_explicit_cache_manager.get_room_settings(room_name) if room_name else {
            "enabled": False,
            "ttl_minutes": gemini_explicit_cache_manager.DEFAULT_TTL_MINUTES,
            "tool_limit": gemini_explicit_cache_manager.DEFAULT_TOOL_LIMIT,
        }
        return (
            gr.update(value=bool(settings.get("enabled", False))),
            gr.update(value=int(settings.get("ttl_minutes", gemini_explicit_cache_manager.DEFAULT_TTL_MINUTES))),
            gr.update(value=int(settings.get("tool_limit", gemini_explicit_cache_manager.DEFAULT_TOOL_LIMIT))),
            gr.update(value=_gemini_explicit_cache_status_text(room_name)),
        )
    except Exception:
        traceback.print_exc()
        return gr.update(), gr.update(), gr.update(), gr.update()


def handle_save_room_gemini_explicit_cache_setting_delta(
    room_name: str,
    child_key: str,
    value: Any,
    label: str,
    value_type: str = "raw",
    is_switching_room: bool = False,
) -> tuple[str, str, Any]:
    """Gemini Explicit キャッシュ設定を差分保存し、必要なら既存キャッシュを破棄する。

    トグル変更を即座にトークン欄の常設バッジへ反映するため、token 表示の
    更新も返す（リロード無しで ON/OFF が欄に反映されるようにする）。
    """
    status = handle_save_room_nested_setting_delta(
        room_name,
        gemini_explicit_cache_manager.SETTINGS_KEY,
        child_key,
        value,
        label,
        value_type,
        is_switching_room,
    )
    if is_switching_room:
        return status, gr.update(), gr.update()
    try:
        normalized_value = _convert_room_nested_setting_delta_value(value, value_type)
        if child_key == "enabled" and not normalized_value and room_name:
            gemini_explicit_cache_manager.delete_cache(room_name)
        elif child_key in {"ttl_minutes", "tool_limit"} and room_name:
            gemini_explicit_cache_manager.delete_cache(room_name)
    except Exception:
        traceback.print_exc()
    return (
        status,
        _gemini_explicit_cache_status_text(room_name),
        _hide_token_count_display(room_name),
    )


def handle_pause_room_gemini_explicit_cache(room_name: str, is_switching_room: bool = False) -> tuple[str, Any, str, Any]:
    """Explicit cache usageを止めつつ、TTL内の既存キャッシュは再利用用に残す。"""
    if is_switching_room:
        return gr.update(), gr.update(), gr.update(), gr.update()
    status = handle_save_room_nested_setting_delta(
        room_name,
        gemini_explicit_cache_manager.SETTINGS_KEY,
        "enabled",
        False,
        "Gemini Explicitキャッシュ一時停止",
        "bool",
        False,
    )
    return (
        status,
        gr.update(value=False),
        _gemini_explicit_cache_status_text(room_name),
        _hide_token_count_display(room_name),
    )

def _format_token_number(value: Any) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    if number <= 0:
        return "-"
    return f"{number / 1000:.1f}k" if number >= 1000 else str(number)

def _usage_int(usage: dict, *keys: str) -> int:
    for key in keys:
        try:
            value = usage.get(key)
        except AttributeError:
            value = None
        if value is None:
            continue
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    return 0

def _estimate_turn_cost(usage: dict) -> Optional[dict]:
    return pricing.estimate_turn_cost(usage)

def _format_usd_estimate(value: float) -> str:
    return pricing.format_usd_estimate(value)

def _format_turn_cost_display(usage: dict) -> str:
    estimate = _estimate_turn_cost(usage)
    if not estimate:
        return ""
    display = f" / 💰 概算 {_format_usd_estimate(float(estimate['cost']))}"
    savings = float(estimate.get("savings") or 0.0)
    if estimate.get("is_registration"):
        suffix = "（登録コスト）"
    elif savings > 0:
        suffix = f"（節約 {_format_usd_estimate(savings)}）"
    else:
        suffix = ""
    if estimate.get("is_paid") is False:
        suffix += "（無料キー・課金なし／参考）"
    if suffix:
        return f"{display}{suffix}"
    return display

def _format_prompt_cache_display(usage: dict) -> str:
    # キャッシュ使用量が取得できたターンのみ表示する。
    # 取得できていれば、ミス（読込0）でも「未ヒット」を出して
    # 「機能は動いているが今回は外した」と分かるようにする（バッジ消失＝壊れてる？の誤解を防ぐ）。
    if "cache_total_input_tokens" not in usage and "cache_read_tokens" not in usage:
        if usage:
            return f" / 💾 キャッシュ報告なし{_format_turn_cost_display(usage)}"
        return _format_turn_cost_display(usage)
    try:
        cache_read_tokens = int(usage.get("cache_read_tokens") or 0)
        cache_creation_tokens = int(usage.get("cache_creation_tokens") or 0)
        total_input_tokens = int(usage.get("cache_total_input_tokens") or usage.get("total_input_tokens") or 0)
    except (TypeError, ValueError):
        return _format_turn_cost_display(usage)
    cache_mode = str(usage.get("cache_mode") or "").strip()
    is_paid_explicit = usage.get("cache_paid") is True or cache_mode == "gemini_explicit"
    paid_prefix = "有料・" if is_paid_explicit else ""
    if cache_mode == "gemini_explicit":
        cache_label = "明示キャッシュ"
    elif cache_mode == "gemini_implicit":
        cache_label = "暗黙キャッシュ"
    else:
        cache_label = "キャッシュ"
    # 明示キャッシュの新規登録ターンは、作成（書込）と読込が同時に起きる。
    # 「読込」表示だと一番高いターンを「お得なヒット」と誤解させるため、
    # 登録ターンは "新規登録（書込）" と明示する。
    if is_paid_explicit and usage.get("cache_just_created") is True:
        write_tokens = cache_read_tokens or cache_creation_tokens or total_input_tokens
        return f" / 💾 明示キャッシュ 新規登録（有料・書込 {_format_token_number(write_tokens)}）{_format_turn_cost_display(usage)}"
    if cache_read_tokens > 0 and total_input_tokens > 0:
        hit_rate = max(0, min(100, round((cache_read_tokens / total_input_tokens) * 100)))
        static_tokens = usage.get("cache_static_tokens") or usage.get("cache_cached_tokens")
        static_hit = ""
        try:
            static_token_count = int(static_tokens or 0)
        except (TypeError, ValueError):
            static_token_count = 0
        if is_paid_explicit and static_token_count > 0:
            static_hit_rate = max(0, min(100, round((cache_read_tokens / static_token_count) * 100)))
            static_hit = f"{static_hit_rate}%（静的部）/"
        if is_paid_explicit:
            return f" / 💾 {cache_label} ヒット {static_hit}総入力比{hit_rate}%（{paid_prefix}読込 {_format_token_number(cache_read_tokens)}）{_format_turn_cost_display(usage)}"
        return f" / 💾 {cache_label} 総入力比{hit_rate}%（{paid_prefix}読込 {_format_token_number(cache_read_tokens)}）{_format_turn_cost_display(usage)}"
    if cache_creation_tokens > 0:
        return f" / 💾 {cache_label} 初回書込（{paid_prefix}{_format_token_number(cache_creation_tokens)}）{_format_turn_cost_display(usage)}"
    cache_display = " / 💾 明示キャッシュ 未ヒット（有料）" if is_paid_explicit else f" / 💾 {cache_label} 未ヒット"
    return f"{cache_display}{_format_turn_cost_display(usage)}"

def _actual_token_display_text(room_name: Optional[str] = None) -> str:
    """直近の実送信トークン数だけを表示する。推定値はGradio 6負荷対策として表示しない。"""
    usage = _LAST_ACTUAL_TOKENS.get(room_name or "", {}) if room_name else {}
    prompt_tokens = (
        usage.get("prompt_tokens")
        or usage.get("prompt")
        or usage.get("input_tokens")
        or usage.get("prompt_token_count")
        or 0
    )
    completion_tokens = (
        usage.get("completion_tokens")
        or usage.get("completion")
        or usage.get("output_tokens")
        or usage.get("candidates_token_count")
        or 0
    )
    total_tokens = (
        usage.get("total_tokens")
        or usage.get("total")
        or usage.get("total_token_count")
        or 0
    )
    if not total_tokens and (prompt_tokens or completion_tokens):
        total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
    cache_display = _format_prompt_cache_display(usage)
    # 会話前（usage がまだ無い）でも、明示キャッシュ ON のルームは
    # 「ONであること」自体を常設表示する（数字は無くても欄を出す）。
    if not cache_display and room_name:
        cache_display = _explicit_cache_standing_indicator(room_name)
    return f"実入力: {_format_token_number(prompt_tokens)} / 実合計: {_format_token_number(total_tokens)}{cache_display}"

def _record_actual_token_usage(room_name: str, final_state: dict) -> None:
    if final_state and "actual_token_usage" in final_state:
        _LAST_ACTUAL_TOKENS[room_name] = final_state["actual_token_usage"]
        print(f"  - [Token] 実績値を記録しました: {final_state['actual_token_usage']}")

def _build_new_messages_from_final_state(
    raw_new_messages: list,
    current_room: str,
    final_state: dict,
    *,
    selector=None,
) -> list:
    # キャッシュバッジの元データはメッセージ選択と独立しているため、先に記録する。
    # これにより最終応答選択で例外が起きても、そのターンの実トークン表示は失われない。
    _record_actual_token_usage(current_room, final_state)
    selector = selector or _select_best_ai_message

    ai_text_messages = []
    other_messages = [] # ToolMessageなど
    for msg in raw_new_messages:
        if isinstance(msg, AIMessage):
            ai_text_messages.append(msg)
        else:
            other_messages.append(msg)

    best_ai_message = selector(ai_text_messages)
    print(f"--- [DEBUG] best_ai_message exists: {best_ai_message is not None} ---")

    new_messages = other_messages
    if best_ai_message:
        new_messages.append(best_ai_message)
    return new_messages


def _explicit_cache_standing_indicator(room_name: str) -> str:
    """明示キャッシュ ON ルームの会話前常設インジケータ。

    usage がまだ無い段階でも、ON なら待機中、条件未達なら ⚠️ を欄に出して
    「ONにしたのに何も出ない＝壊れてる？」という誤解を防ぐ。
    """
    try:
        if not gemini_explicit_cache_manager.is_enabled_for_room(room_name):
            return ""
        reason = gemini_explicit_cache_manager.get_disabled_reason(room_name)
    except Exception:
        return ""
    if reason:
        return " / 💾 明示キャッシュ ⚠️（条件未達で無効）"
    return " / 💾 明示キャッシュ ON（有料・待機中）"

def _usage_provider_label(provider: str) -> str:
    return {
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "gemini_implicit": "Gemini暗黙",
        "gemini_explicit": "Gemini明示",
        "gemini_image": "Gemini画像",
        "openai_image": "OpenAI画像",
        "pollinations_image": "Pollinations画像",
        "huggingface_image": "Hugging Face画像",
    }.get(str(provider or ""), str(provider or "不明"))

def _usage_source_label(source: str) -> str:
    return {
        "chat": "本処理",
        "autonomous": "自律",
        "internal": "内部処理",
        "delegation": "委任",
        "image": "画像生成",
    }.get(str(source or ""), str(source or "不明"))

def _usage_table_cell(value: object) -> str:
    """API使用量のMarkdown表で区切り文字が列を壊さないようにする。"""
    return str(value or "-").replace("|", "\\|").replace("\n", " ")

def _usage_period_label(period: str, aggregate: dict, *, include_month: bool = False) -> str:
    label = str(aggregate.get("period_label") or period)
    if include_month and period == "month":
        label = f"{label}（{datetime.datetime.now().month}月）"
    return label


def _format_usage_detail_card(period: str, aggregate: dict) -> str:
    label = _usage_period_label(period, aggregate, include_month=True)
    count = int(aggregate.get("count") or 0)

    lines = [f"### {label}"]
    if count <= 0:
        lines.append("まだ記録がありません。")
        return "\n".join(lines)

    # 内訳（用途別・モデル別・プロバイダ別）は有料キー分のみ集計される。
    # 有料利用が無い期間は見出しだけが浮いて次セクションと紛らわしいため、明示する。
    if float(aggregate.get("total_cost_paid") or 0.0) <= 0.0 and not (aggregate.get("by_model") or {}):
        free_cost = pricing.format_usd_estimate(float(aggregate.get("total_cost_free") or 0.0))
        lines.append(f"この期間は有料キーの利用がありません（無料キー参考: {free_cost}・{count}回）。")
        return "\n".join(lines)

    by_source = aggregate.get("by_source") or {}
    if by_source:
        lines.extend(["", "#### 用途別", "| 用途 | 概算（有料キー） |", "|---|---:|"])
        for source, data in sorted(by_source.items()):
            lines.append(
                f"| {_usage_table_cell(_usage_source_label(source))} | "
                f"{pricing.format_usd_estimate(float(data.get('cost') or 0.0))} |"
            )

    by_model = aggregate.get("by_model") or {}
    if by_model:
        lines.extend([
            "",
            "#### モデル別",
            "| モデル | プロバイダ | 概算 | 節約 | 回数 | 入力 | 出力 |",
            "|---|---|---:|---:|---:|---:|---:|",
        ])
        for model, data in sorted(by_model.items(), key=lambda item: float(item[1].get("cost") or 0.0), reverse=True):
            lines.append(
                f"| `{_usage_table_cell(model)}` | "
                f"{_usage_table_cell(_usage_provider_label(data.get('provider', '')))} | "
                f"{pricing.format_usd_estimate(float(data.get('cost') or 0.0))} | "
                f"{pricing.format_usd_estimate(max(0.0, float(data.get('savings') or 0.0)))} | "
                f"{int(data.get('count') or 0)} | {_format_token_number(data.get('prompt_tokens'))} | "
                f"{_format_token_number(data.get('completion_tokens'))} |"
            )

    by_provider = aggregate.get("by_provider") or {}
    if by_provider:
        lines.extend(["", "#### プロバイダ別", "| プロバイダ | 概算 | 節約 |", "|---|---:|---:|"])
        for provider, data in sorted(by_provider.items()):
            lines.append(
                f"| {_usage_table_cell(_usage_provider_label(provider))} | "
                f"{pricing.format_usd_estimate(float(data.get('cost') or 0.0))} | "
                f"{pricing.format_usd_estimate(max(0.0, float(data.get('savings') or 0.0)))} |"
            )
    return "\n".join(lines)

def _get_usage_period_aggregates() -> list[tuple[str, dict]]:
    return [
        (period, usage_ledger.aggregate(period))
        for period in ("today", "week", "month")
    ]


def _format_usage_compact_cost(value: object) -> str:
    amount = float(value or 0.0)
    if amount == 0:
        return "$0.00"
    if abs(amount) < 0.01:
        return pricing.format_usd_estimate(amount)
    return f"≈${amount:.2f}"


def _format_usage_summary_compact(period_aggregates: list[tuple[str, dict]]) -> str:
    items = []
    for period, aggregate in period_aggregates:
        label = _usage_period_label(period, aggregate)
        # 有料キー基準の金額のみ表示（`or`での無料額フォールバックは誤表示になるため禁止）
        amount = _format_usage_compact_cost(aggregate.get("total_cost_paid") or 0.0)
        items.append(
            f'<span class="usage-summary-period">{_usage_table_cell(label)} '
            f'<span class="usage-summary-amount">{amount}</span></span>'
        )
    return f'<span class="usage-summary-compact">{"".join(items)}</span>'


def _format_usage_summary_detail(period_aggregates: list[tuple[str, dict]]) -> str:
    parts = [
        "# 📊 API使用量の概算",
        "",
        "## 概算サマリー",
        "| 期間 | 概算（有料キー） | 節約 | 無料キー参考 | 回数 |",
        "|---|---:|---:|---:|---:|",
    ]
    for period, aggregate in period_aggregates:
        # 注意: `or` フォールバック禁止。有料額0のとき無料額を「有料キー」列に
        # 出してしまい、「今日 > 今週」のような矛盾表示になる実害があった（2026-07-10）。
        parts.append(
            f"| {_usage_table_cell(_usage_period_label(period, aggregate, include_month=True))} | "
            f"{pricing.format_usd_estimate(float(aggregate.get('total_cost_paid') or 0.0))} | "
            f"{pricing.format_usd_estimate(max(0.0, float(aggregate.get('total_savings_paid') or 0.0)))} | "
            f"{pricing.format_usd_estimate(float(aggregate.get('total_cost_free') or 0.0))} | "
            f"{int(aggregate.get('count') or 0)} |"
        )
    for period, aggregate in period_aggregates:
        parts.extend(["", _format_usage_detail_card(period, aggregate)])
    parts.extend([
        "",
        "---",
        "⚠️ これは概算です（価格は `pricing.py` の目安で、モデルにより変動します）。",
        "本処理・自律・内部処理・委任・画像生成を集計し、金額は有料キー基準、無料キー分は参考表示です。",
        "画像生成は登録済みモデルの代表的な1枚単価で概算し、単価未登録モデルは金額0で回数だけ記録します。",
        "音声合成（TTS）は集計に含みません。価格未登録のテキストモデル・ローカルモデルも含みません。最終的な請求は各プロバイダの画面で確認してください。",
    ])
    return "\n".join(parts)


def handle_refresh_usage_summary() -> str:
    """サイドバー用のAPI使用量概算を一行で更新する。"""
    try:
        return _format_usage_summary_compact(_get_usage_period_aggregates())
    except Exception as e:
        traceback.print_exc()
        return f'<span class="usage-summary-compact">⚠️ 読み込み失敗: {_usage_table_cell(e)}</span>'


def open_usage_detail():
    """API使用量の詳細を既存のドキュメントビューアーへ表示する。"""
    try:
        content = _format_usage_summary_detail(_get_usage_period_aggregates())
    except Exception as e:
        traceback.print_exc()
        content = f"# 📊 API使用量の概算\n\n⚠️ 使用量概算の読み込みに失敗しました: {e}"
    return gr.update(visible=True), content

def _hide_token_count_display(room_name: Optional[str] = None):
    """互換名。現在は推定値ではなく、直近の実送信トークン数を表示する。"""
    return gr.update(value=_actual_token_display_text(room_name), visible=True)

def _format_token_display(room_name: str, estimated_count: int = 0):
    """互換用。推定値を出さず、実送信トークン数だけを返す。"""
    return _hide_token_count_display(room_name)

def handle_save_last_room(room_name: str, request: gr.Request = None) -> None:
    """
    選択されたルーム名をconfig.jsonに保存するだけの、何も返さない専用ハンドラ。
    Gradioのchangeイベントが不要な戻り値を受け取らないようにするために使用する。
    """
    # [2026-04-09 FIX] セッション分離型初期化ガード (識別可能なセッションのみ対象)
    session_id = _get_session_id(request)
    if session_id != "default":
        init_room = _get_session_init_room(session_id)
        state = _session_init_states.get(session_id, {})

        # 初期化中のUI同期イベントだけをガードする。
        # 初期化完了後の実ユーザー操作は、POST_INIT_GRACE_PERIOD中でも正規のルーム切替として通す。
        is_initializing = (not state.get("completed", False))
        is_just_finished = state.get("completed") and (time.time() - state.get("time", 0)) < 2.0

        if init_room and is_initializing:
            if room_name != init_room:
                print(f"--- [Session:{session_id}] [handle_save_last_room] キャッシュ不整合阻止: {room_name} -> {init_room} を維持 ---")
                return
            return
        if init_room and is_just_finished and room_name == init_room:
            print(f"--- [Session:{session_id}] [handle_save_last_room] 初期化直後の冗長な保存をスキップ ---")
            return

    if room_name:
        config_manager.save_config_if_changed("last_room", room_name)
        if session_id != "default":
            _session_init_states[session_id] = {
                "completed": True,
                "time": time.time() - POST_INIT_GRACE_PERIOD_SECONDS,
                "room": room_name
            }

# --- [Phase 13 追加] 再発防止用の共通ヘルパー ---
def _ensure_output_count(values_tuple: tuple, expected_count: int) -> tuple:
    """
    Gradioの出力カウント不整合エラー (ValueError) を防ぐための安全装置。
    返却値の数が期待値より少ない場合は gr.update() で埋め、多い場合は切り捨てる。
    """
    if len(values_tuple) == expected_count:
        return values_tuple

    if len(values_tuple) < expected_count:
        # 足りない分を gr.update() で埋める
        padding = (gr.update(),) * (expected_count - len(values_tuple))
        # 常にログを表示して同期状況を可視化する (2026-04-09)
        print(f"--- [Session Guard] 出力数を自動調整(返却:{len(values_tuple)} -> 期待:{expected_count}) [1番目(State)={values_tuple[0] if values_tuple else 'None'}] ---")
        return values_tuple + padding
    else:
        # 多すぎる分を切り捨てる
        print(f"⚠️ [Gradio Safety] 出力数が多すぎます (返却:{len(values_tuple)} > 期待:{expected_count})。超過分を無視します。")
        return values_tuple[:expected_count]


def _closet_image_gallery_value(reference_images: list) -> list:
    gallery_items = []
    for rel_path in reference_images or []:
        if rel_path and os.path.exists(rel_path):
            gallery_items.append(rel_path)
    return gallery_items


def _closet_image_dropdown_update(reference_images: list):
    choices = list(reference_images or [])
    return gr.update(choices=choices, value=choices[0] if choices else None)


def load_closet_profile_ui(room_name: str):
    """Load closet profile values for the room settings UI."""
    if not room_name:
        return _ensure_output_count(
            (False, "", [], gr.update(choices=[], value=None), "クローゼット: ルーム未選択"),
            5,
        )
    try:
        profile = closet_manager.load_persona_profile(room_name)
        base = profile.get("base", {}) or {}
        images = base.get("reference_images") or []
        status = "クローゼット: 読み込み済み"
        if profile.get("updated_at"):
            status += f"（最終更新: {profile.get('updated_at')}）"
        return _ensure_output_count(
            (
                bool(profile.get("enabled", False)),
                base.get("description", ""),
                _closet_image_gallery_value(images),
                _closet_image_dropdown_update(images),
                status,
            ),
            5,
        )
    except Exception as e:
        traceback.print_exc()
        gr.Warning(f"クローゼットの読み込みに失敗しました: {e}")
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), f"クローゼット: 読み込み失敗 ({e})"), 5)


def handle_save_closet_profile(room_name: str, enabled: bool, description: str):
    """Save closet enabled/description while preserving existing images."""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return _ensure_output_count((gr.update(), gr.update(), "クローゼット: ルーム未選択"), 3)
    try:
        current = closet_manager.load_persona_profile(room_name)
        images = current.get("base", {}).get("reference_images") or []
        profile = closet_manager.save_persona_profile(room_name, enabled, description, images)
        saved_images = profile.get("base", {}).get("reference_images") or []
        gr.Info("クローゼットを保存しました。")
        return _ensure_output_count(
            (
                _closet_image_gallery_value(saved_images),
                _closet_image_dropdown_update(saved_images),
                f"クローゼット: 保存済み（最終更新: {profile.get('updated_at', '')}）",
            ),
            3,
        )
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"クローゼットの保存に失敗しました: {e}")
        return _ensure_output_count((gr.update(), gr.update(), f"クローゼット: 保存失敗 ({e})"), 3)


def handle_add_closet_reference_image(file_value, room_name: str):
    """Add an uploaded reference image to the room closet."""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return _ensure_output_count((gr.update(), gr.update(), "クローゼット: ルーム未選択"), 3)
    if not file_value:
        gr.Warning("追加する画像を選択してください。")
        return _ensure_output_count((gr.update(), gr.update(), "クローゼット: 画像未選択"), 3)
    try:
        src_path = getattr(file_value, "name", None) or getattr(file_value, "path", None) or str(file_value)
        rel_path = closet_manager.add_reference_image(room_name, src_path)
        current = closet_manager.load_persona_profile(room_name)
        base = current.get("base", {}) or {}
        images = list(base.get("reference_images") or [])
        if rel_path not in images:
            images.append(rel_path)
        profile = closet_manager.save_persona_profile(
            room_name,
            current.get("enabled", False),
            base.get("description", ""),
            images,
        )
        saved_images = profile.get("base", {}).get("reference_images") or []
        gr.Info("参照画像を追加しました。")
        return _ensure_output_count(
            (
                _closet_image_gallery_value(saved_images),
                _closet_image_dropdown_update(saved_images),
                f"クローゼット: 参照画像を追加（最終更新: {profile.get('updated_at', '')}）",
            ),
            3,
        )
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"参照画像の追加に失敗しました: {e}")
        return _ensure_output_count((gr.update(), gr.update(), f"クローゼット: 画像追加失敗 ({e})"), 3)


def handle_remove_closet_reference_image(room_name: str, rel_path: str):
    """Remove a selected closet reference image."""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return _ensure_output_count((gr.update(), gr.update(), "クローゼット: ルーム未選択"), 3)
    if not rel_path:
        gr.Warning("削除する参照画像を選択してください。")
        return _ensure_output_count((gr.update(), gr.update(), "クローゼット: 削除対象未選択"), 3)
    try:
        closet_manager.remove_reference_image(room_name, rel_path)
        profile = closet_manager.load_persona_profile(room_name)
        images = profile.get("base", {}).get("reference_images") or []
        gr.Info("参照画像を削除しました。")
        return _ensure_output_count(
            (
                _closet_image_gallery_value(images),
                _closet_image_dropdown_update(images),
                f"クローゼット: 参照画像を削除（最終更新: {profile.get('updated_at', '')}）",
            ),
            3,
        )
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"参照画像の削除に失敗しました: {e}")
        return _ensure_output_count((gr.update(), gr.update(), f"クローゼット: 画像削除失敗 ({e})"), 3)


def _closet_catalog_selector_update(items: list):
    choices = [f"{item.get('name', '名称未設定')} [{item.get('part', 'その他')}] [{item.get('id', '')}]" for item in items]
    return gr.update(choices=choices, value=choices[0] if choices else None, interactive=bool(choices))


def _extract_closet_id_from_choice(choice_str):
    if not choice_str:
        return None
    import re
    matches = re.findall(r"\[([^\]]+)\]", str(choice_str))
    return matches[-1] if matches else None


def _render_closet_catalog_table(room_name: str) -> str:
    items = closet_manager.list_closet_items(room_name) if room_name else []
    profile = closet_manager.load_persona_profile(room_name) if room_name else {}
    worn_ids = set((profile.get("current", {}) or {}).get("worn") or [])
    if not items:
        return "<p class='info-text'>着用可能なクローゼット項目はまだありません。</p>"
    rows = []
    for item in items:
        worn = "着用中" if item.get("id") in worn_ids else ""
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('name', '名称未設定')))}</td>"
            f"<td>{html.escape(str(item.get('part', 'その他')))}</td>"
            f"<td>{html.escape(worn)}</td>"
            f"<td>{html.escape(str(item.get('source', '')))}</td>"
            f"<td>{html.escape(str(item.get('linked_item_id', '')))}</td>"
            f"<td><code>{html.escape(str(item.get('id', '')))}</code></td>"
            "</tr>"
        )
    return (
        "<table class='unified-inventory-table'>"
        "<thead><tr><th>名前</th><th>部位</th><th>状態</th><th>由来</th><th>リンク元</th><th>ID</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _closet_item_detail_markdown(room_name: str, closet_id: str) -> str:
    item = closet_manager.get_closet_item(room_name, closet_id) if room_name and closet_id else None
    if not item:
        return "クローゼット項目を選択してください。"
    lines = [
        f"### {item.get('name', '名称未設定')}",
        f"- ID: `{item.get('id', '')}`",
        f"- 部位: {item.get('part', 'その他')}",
        f"- 由来: {item.get('source', '')}",
    ]
    if item.get("linked_item_id"):
        lines.append(f"- リンク元アイテムID: `{item.get('linked_item_id')}`")
    if item.get("description"):
        lines.extend(["", item.get("description")])
    if item.get("reference_image"):
        lines.append(f"\n参照画像: `{item.get('reference_image')}`")
    tags = item.get("tags") or []
    if tags:
        lines.append(f"タグ: {', '.join(tags)}")
    return "\n".join(lines)


def _current_outfit_markdown(room_name: str) -> str:
    if not room_name:
        return "現在の装い: ルーム未選択"
    profile = closet_manager.load_persona_profile(room_name)
    current = profile.get("current", {}) or {}
    worn_ids = current.get("worn") or []
    lines = ["### 現在の装い"]
    note = str(current.get("note") or "").strip()
    lines.append(f"メモ: {note}" if note else "メモ: （未入力）")
    if not worn_ids:
        lines.append("着用中: （なし）")
        return "\n".join(lines)
    lines.append("着用中:")
    for closet_id in worn_ids:
        item = closet_manager.get_closet_item(room_name, closet_id)
        if item:
            lines.append(f"- {item.get('name')} [{item.get('part')}] `{closet_id}`")
    return "\n".join(lines)


def load_closet_catalog_ui(room_name: str):
    """Load closet catalog values for the item tab."""
    if not room_name:
        return _ensure_output_count(
            ("<p class='info-text'>ルームを選択してください。</p>", gr.update(choices=[], value=None, interactive=False), "クローゼット項目を選択してください。", "", "現在の装い: ルーム未選択", "クローゼット: ルーム未選択"),
            6,
        )
    try:
        items = closet_manager.list_closet_items(room_name)
        profile = closet_manager.load_persona_profile(room_name)
        current = profile.get("current", {}) or {}
        selected = items[0].get("id") if items else None
        return _ensure_output_count(
            (
                _render_closet_catalog_table(room_name),
                _closet_catalog_selector_update(items),
                _closet_item_detail_markdown(room_name, selected),
                current.get("note", ""),
                _current_outfit_markdown(room_name),
                "クローゼット: 読み込み済み",
            ),
            6,
        )
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), f"クローゼット: 読み込み失敗 ({e})"), 6)


def handle_closet_catalog_selection(room_name: str, choice_str: str):
    closet_id = _extract_closet_id_from_choice(choice_str)
    return closet_id, _closet_item_detail_markdown(room_name, closet_id)


def _closet_catalog_outputs(room_name: str, status: str):
    items = closet_manager.list_closet_items(room_name)
    selected = items[0].get("id") if items else None
    return _ensure_output_count(
        (
            _render_closet_catalog_table(room_name),
            _closet_catalog_selector_update(items),
            _closet_item_detail_markdown(room_name, selected),
            _current_outfit_markdown(room_name),
            status,
        ),
        5,
    )


def handle_wear_closet_item_ui(room_name: str, closet_id: str):
    closet_id = _extract_closet_id_from_choice(closet_id) or closet_id
    if not room_name or not closet_id:
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), "クローゼット: 項目未選択"), 5)
    try:
        closet_manager.wear_item(room_name, closet_id)
        return _closet_catalog_outputs(room_name, "クローゼット: 着用しました")
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), f"クローゼット: 着用失敗 ({e})"), 5)


def handle_take_off_closet_item_ui(room_name: str, closet_id: str):
    closet_id = _extract_closet_id_from_choice(closet_id) or closet_id
    if not room_name or not closet_id:
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), "クローゼット: 項目未選択"), 5)
    try:
        closet_manager.take_off_item(room_name, closet_id)
        return _closet_catalog_outputs(room_name, "クローゼット: 脱衣しました")
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), f"クローゼット: 脱衣失敗 ({e})"), 5)


def handle_delete_closet_item_ui(room_name: str, closet_id: str):
    closet_id = _extract_closet_id_from_choice(closet_id) or closet_id
    if not room_name or not closet_id:
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), "クローゼット: 項目未選択"), 5)
    try:
        closet_manager.remove_closet_item(room_name, closet_id)
        return _closet_catalog_outputs(room_name, "クローゼット: 項目を削除しました")
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), f"クローゼット: 削除失敗 ({e})"), 5)


def handle_save_closet_current_note_ui(room_name: str, note: str):
    if not room_name:
        return _ensure_output_count(("現在の装い: ルーム未選択", "クローゼット: ルーム未選択"), 2)
    try:
        profile = closet_manager.load_persona_profile(room_name)
        closet_manager.set_current_outfit(room_name, note, (profile.get("current", {}) or {}).get("worn") or [])
        return _ensure_output_count((_current_outfit_markdown(room_name), "クローゼット: 現在の装いメモを保存しました"), 2)
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(), f"クローゼット: メモ保存失敗 ({e})"), 2)


def _get_inventory_item_for_closet(room_name: str, target: str, item_id: str):
    from src.features.item_manager import ItemManager
    if not room_name or not item_id:
        return None
    im = ItemManager(room_name)
    return im.get_item(item_id, is_user=(target == "ユーザー"))


def _inventory_item_description(item: dict) -> str:
    values = []
    for key in ("description", "base_info", "flavor_text"):
        value = str(item.get(key) or "").strip()
        if value:
            values.append(value)
    appearance = item.get("appearance") if isinstance(item.get("appearance"), dict) else {}
    for key in ("description", "design", "texture"):
        value = str(appearance.get(key) or "").strip()
        if value:
            values.append(value)
    return "\n".join(dict.fromkeys(values))


def handle_prepare_closet_bridge(room_name: str, target: str, item_id: str):
    item = _get_inventory_item_for_closet(room_name, target, item_id)
    if not item:
        return _ensure_output_count(("", "", "", gr.update(value="⚠️ アイテムを選択してください", visible=True)), 4)
    tags = [str(item.get("category") or "").strip()] if item.get("category") else []
    return _ensure_output_count(
        (
            item.get("name", ""),
            _inventory_item_description(item),
            ", ".join(tags),
            gr.update(value="クローゼット登録用に入力しました。", visible=True),
        ),
        4,
    )


def handle_register_inventory_item_to_closet_ui(room_name: str, target: str, item_id: str, part: str, name: str, description: str, tags: str):
    if not room_name or not item_id:
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), "クローゼット: アイテム未選択"), 6)
    try:
        item = _get_inventory_item_for_closet(room_name, target, item_id)
        if not item:
            return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), "クローゼット: アイテムが見つかりません"), 6)
        image_path = item.get("image_path") or ""
        if not image_path:
            return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), "クローゼット: 参照画像のあるアイテムだけ登録できます"), 6)
        closet_manager.add_closet_item(
            room_name=room_name,
            name=name or item.get("name", "名称未設定"),
            part=part or "その他",
            description=description or _inventory_item_description(item),
            reference_image=image_path,
            source="generated",
            linked_item_id=item.get("id") or item_id,
            tags=tags,
        )
        catalog_html, selector, detail, note, current_md, _ = load_closet_catalog_ui(room_name)
        return _ensure_output_count((catalog_html, selector, detail, note, current_md, "クローゼット: アイテムを登録しました"), 6)
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), f"クローゼット: 登録失敗 ({e})"), 6)


def _user_closet_scope_for_room(room_name: str) -> str:
    return "common" if closet_manager.is_user_closet_common(room_name) else "room"


def _user_closet_items(scope: str, room_name: str) -> list:
    return closet_manager.list_user_closet_items_for_scope(scope, room_name)


def _user_closet_profile(scope: str, room_name: str) -> dict:
    return closet_manager.load_user_profile_for_scope(scope, room_name)


def _user_closet_selector_update(items: list):
    choices = [f"{item.get('name', '名称未設定')} [{item.get('part', 'その他')}] [{item.get('id', '')}]" for item in items]
    return gr.update(choices=choices, value=choices[0] if choices else None, interactive=bool(choices))


def _render_user_closet_table(scope: str, room_name: str) -> str:
    items = _user_closet_items(scope, room_name)
    profile = _user_closet_profile(scope, room_name)
    worn_ids = set((profile.get("current", {}) or {}).get("worn") or [])
    if not items:
        return "<p class='info-text'>ユーザークローゼット項目はまだありません。</p>"
    rows = []
    for item in items:
        worn = "着用中" if item.get("id") in worn_ids else ""
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('name', '名称未設定')))}</td>"
            f"<td>{html.escape(str(item.get('part', 'その他')))}</td>"
            f"<td>{html.escape(worn)}</td>"
            f"<td>{html.escape(str(item.get('source', '')))}</td>"
            f"<td>{html.escape(str(item.get('linked_item_id', '')))}</td>"
            f"<td><code>{html.escape(str(item.get('id', '')))}</code></td>"
            "</tr>"
        )
    return (
        "<table class='unified-inventory-table'>"
        "<thead><tr><th>名前</th><th>部位</th><th>状態</th><th>由来</th><th>リンク元</th><th>ID</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _user_closet_detail_markdown(scope: str, room_name: str, closet_id: str) -> str:
    item = closet_manager.get_user_closet_item(scope, room_name, closet_id) if closet_id else None
    if not item:
        return "ユーザークローゼット項目を選択してください。"
    lines = [
        f"### {item.get('name', '名称未設定')}",
        f"- ID: `{item.get('id', '')}`",
        f"- 部位: {item.get('part', 'その他')}",
        f"- 由来: {item.get('source', '')}",
    ]
    if item.get("linked_item_id"):
        lines.append(f"- リンク元アイテムID: `{item.get('linked_item_id')}`")
    if item.get("description"):
        lines.extend(["", item.get("description")])
    if item.get("reference_image"):
        lines.append(f"\n参照画像: `{item.get('reference_image')}`")
    tags = item.get("tags") or []
    if tags:
        lines.append(f"タグ: {', '.join(tags)}")
    return "\n".join(lines)


def _user_current_outfit_markdown(scope: str, room_name: str) -> str:
    profile = _user_closet_profile(scope, room_name)
    current = profile.get("current", {}) or {}
    worn_ids = current.get("worn") or []
    lines = ["### ユーザーの現在の装い"]
    note = str(current.get("note") or "").strip()
    lines.append(f"メモ: {note}" if note else "メモ: （未入力）")
    if not worn_ids:
        lines.append("着用中: （なし）")
        return "\n".join(lines)
    lines.append("着用中:")
    for closet_id in worn_ids:
        item = closet_manager.get_user_closet_item(scope, room_name, closet_id)
        if item:
            lines.append(f"- {item.get('name')} [{item.get('part')}] `{closet_id}`")
    return "\n".join(lines)


def _load_user_closet_scope_values(scope: str, room_name: str, status: str):
    profile = _user_closet_profile(scope, room_name)
    base = profile.get("base", {}) or {}
    images = base.get("reference_images") or []
    items = _user_closet_items(scope, room_name)
    selected = items[0].get("id") if items else None
    return (
        bool(profile.get("enabled", False)),
        base.get("description", ""),
        _closet_image_gallery_value(images),
        _closet_image_dropdown_update(images),
        _render_user_closet_table(scope, room_name),
        _user_closet_selector_update(items),
        _user_closet_detail_markdown(scope, room_name, selected),
        (profile.get("current", {}) or {}).get("note", ""),
        _user_current_outfit_markdown(scope, room_name),
        status,
    )


def load_user_closet_common_ui():
    try:
        return _ensure_output_count(_load_user_closet_scope_values("common", "", "ユーザー外見（共通）: 読み込み済み"), 10)
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((False, "", [], gr.update(choices=[], value=None), gr.update(), gr.update(), gr.update(), "", gr.update(), f"ユーザー外見（共通）: 読み込み失敗 ({e})"), 10)


def load_user_closet_room_ui(room_name: str):
    if not room_name:
        return _ensure_output_count((True,) + (gr.update(),) * 9 + ("ユーザー外見（このルーム）: ルーム未選択", gr.update()), 12)
    try:
        use_common = closet_manager.is_user_closet_common(room_name)
        scope = "common" if use_common else "room"
        status = "ユーザー外見（このルーム）: 共通設定を表示中" if use_common else "ユーザー外見（このルーム）: ルーム専用設定を表示中"
        values = _load_user_closet_scope_values(scope, room_name, status)
        interactive = not use_common
        return _ensure_output_count(
            (
                use_common,
                gr.update(value=values[0], interactive=interactive),
                gr.update(value=values[1], interactive=interactive),
                values[2],
                values[3],
                values[4],
                values[5],
                values[6],
                gr.update(value=values[7], interactive=interactive),
                values[8],
                values[9],
                gr.update(interactive=interactive),
            ),
            12,
        )
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(),) * 10 + (f"ユーザー外見（このルーム）: 読み込み失敗 ({e})", gr.update()), 12)


def handle_set_user_closet_common(room_name: str, use_common: bool):
    if not room_name:
        return load_user_closet_room_ui(room_name)
    try:
        closet_manager.set_user_closet_common(room_name, bool(use_common))
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"ユーザー外見の共通設定切替に失敗しました: {e}")
    return load_user_closet_room_ui(room_name)


def handle_promote_user_room_to_common(room_name: str):
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return _ensure_output_count(load_user_closet_common_ui() + load_user_closet_room_ui(room_name), 22)
    try:
        closet_manager.promote_user_room_to_common(room_name)
        gr.Info("このルームのユーザー外見を共通設定に反映しました。")
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"共通設定への反映に失敗しました: {e}")
    return _ensure_output_count(load_user_closet_common_ui() + load_user_closet_room_ui(room_name), 22)


def handle_save_user_closet_profile(scope: str, room_name: str, enabled: bool, description: str):
    try:
        current = closet_manager.load_user_profile_for_scope(scope, room_name)
        images = current.get("base", {}).get("reference_images") or []
        closet_manager.save_user_profile(scope, room_name, enabled, description, images)
        gr.Info("ユーザー外見を保存しました。")
        return _ensure_output_count(_load_user_closet_scope_values(scope, room_name, "ユーザー外見: 保存済み"), 10)
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"ユーザー外見の保存に失敗しました: {e}")
        return _ensure_output_count((gr.update(),) * 9 + (f"ユーザー外見: 保存失敗 ({e})",), 10)


def handle_add_user_reference_image(file_value, scope: str, room_name: str):
    if not file_value:
        gr.Warning("追加する画像を選択してください。")
        return _ensure_output_count((gr.update(),) * 9 + ("ユーザー外見: 画像未選択",), 10)
    try:
        src_path = getattr(file_value, "name", None) or getattr(file_value, "path", None) or str(file_value)
        rel_path = closet_manager.add_user_reference_image(scope, room_name, src_path)
        current = closet_manager.load_user_profile_for_scope(scope, room_name)
        base = current.get("base", {}) or {}
        images = list(base.get("reference_images") or [])
        if rel_path not in images:
            images.append(rel_path)
        closet_manager.save_user_profile(scope, room_name, current.get("enabled", False), base.get("description", ""), images)
        gr.Info("ユーザー外見の参照画像を追加しました。")
        return _ensure_output_count(_load_user_closet_scope_values(scope, room_name, "ユーザー外見: 参照画像を追加"), 10)
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"ユーザー外見の参照画像追加に失敗しました: {e}")
        return _ensure_output_count((gr.update(),) * 9 + (f"ユーザー外見: 画像追加失敗 ({e})",), 10)


def handle_remove_user_reference_image(scope: str, room_name: str, rel_path: str):
    if not rel_path:
        gr.Warning("削除する画像を選択してください。")
        return _ensure_output_count((gr.update(),) * 9 + ("ユーザー外見: 削除対象未選択",), 10)
    try:
        closet_manager.remove_user_reference_image(scope, room_name, rel_path)
        gr.Info("ユーザー外見の参照画像を削除しました。")
        return _ensure_output_count(_load_user_closet_scope_values(scope, room_name, "ユーザー外見: 参照画像を削除"), 10)
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"ユーザー外見の参照画像削除に失敗しました: {e}")
        return _ensure_output_count((gr.update(),) * 9 + (f"ユーザー外見: 画像削除失敗 ({e})",), 10)


def handle_user_closet_selection(scope: str, room_name: str, choice_str: str):
    closet_id = _extract_closet_id_from_choice(choice_str)
    return closet_id, _user_closet_detail_markdown(scope, room_name, closet_id)


def handle_add_user_real_closet_item(scope: str, room_name: str, file_value, name: str, part: str, description: str, tags: str):
    if not file_value:
        gr.Warning("登録する服の画像を選択してください。")
        return _ensure_output_count((gr.update(),) * 9 + ("ユーザー外見: リアル服画像未選択",), 10)
    try:
        src_path = getattr(file_value, "name", None) or getattr(file_value, "path", None) or str(file_value)
        closet_manager.add_user_closet_item(
            scope=scope,
            room_name=room_name,
            name=name,
            part=part,
            description=description,
            reference_image=src_path,
            source="real",
            tags=tags,
        )
        gr.Info("リアル服をユーザークローゼットに登録しました。")
        return _ensure_output_count(_load_user_closet_scope_values(scope, room_name, "ユーザー外見: リアル服を登録"), 10)
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"リアル服の登録に失敗しました: {e}")
        return _ensure_output_count((gr.update(),) * 9 + (f"ユーザー外見: リアル服登録失敗 ({e})",), 10)


def _user_closet_after_item_change(scope: str, room_name: str, status: str):
    values = _load_user_closet_scope_values(scope, room_name, status)
    return _ensure_output_count((values[4], values[5], values[6], values[8], values[9]), 5)


def handle_wear_user_closet_item_ui(scope: str, room_name: str, closet_id: str):
    closet_id = _extract_closet_id_from_choice(closet_id) or closet_id
    if not closet_id:
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), "ユーザー外見: 項目未選択"), 5)
    try:
        closet_manager.wear_user_item(scope, room_name, closet_id)
        return _user_closet_after_item_change(scope, room_name, "ユーザー外見: 着用しました")
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), f"ユーザー外見: 着用失敗 ({e})"), 5)


def handle_take_off_user_closet_item_ui(scope: str, room_name: str, closet_id: str):
    closet_id = _extract_closet_id_from_choice(closet_id) or closet_id
    if not closet_id:
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), "ユーザー外見: 項目未選択"), 5)
    try:
        closet_manager.take_off_user_item(scope, room_name, closet_id)
        return _user_closet_after_item_change(scope, room_name, "ユーザー外見: 脱衣しました")
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), f"ユーザー外見: 脱衣失敗 ({e})"), 5)


def handle_delete_user_closet_item_ui(scope: str, room_name: str, closet_id: str):
    closet_id = _extract_closet_id_from_choice(closet_id) or closet_id
    if not closet_id:
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), "ユーザー外見: 項目未選択"), 5)
    try:
        closet_manager.remove_user_closet_item(scope, room_name, closet_id)
        return _user_closet_after_item_change(scope, room_name, "ユーザー外見: 項目を削除しました")
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), f"ユーザー外見: 削除失敗 ({e})"), 5)


def handle_save_user_current_note_ui(scope: str, room_name: str, note: str):
    try:
        profile = closet_manager.load_user_profile_for_scope(scope, room_name)
        closet_manager.set_user_current_outfit(scope, room_name, note, (profile.get("current", {}) or {}).get("worn") or [])
        return _ensure_output_count((_user_current_outfit_markdown(scope, room_name), "ユーザー外見: 現在の装いメモを保存しました"), 2)
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(), f"ユーザー外見: メモ保存失敗 ({e})"), 2)


def handle_register_inventory_item_to_user_closet_ui(room_name: str, target: str, item_id: str, part: str, name: str, description: str, tags: str):
    if target != "ユーザー":
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), "ユーザー外見: ユーザー所持品だけ登録できます"), 6)
    if not room_name or not item_id:
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), "ユーザー外見: アイテム未選択"), 6)
    try:
        item = _get_inventory_item_for_closet(room_name, target, item_id)
        if not item:
            return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), "ユーザー外見: アイテムが見つかりません"), 6)
        image_path = item.get("image_path") or ""
        if not image_path:
            return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), "ユーザー外見: 参照画像のあるアイテムだけ登録できます"), 6)
        scope = _user_closet_scope_for_room(room_name)
        closet_manager.add_user_closet_item(
            scope=scope,
            room_name=room_name,
            name=name or item.get("name", "名称未設定"),
            part=part or "その他",
            description=description or _inventory_item_description(item),
            reference_image=image_path,
            source="generated",
            linked_item_id=item.get("id") or item_id,
            tags=tags,
        )
        values = _load_user_closet_scope_values(scope, room_name, "ユーザー外見: アイテムを登録しました")
        return _ensure_output_count((values[4], values[5], values[6], values[7], values[8], values[9]), 6)
    except Exception as e:
        traceback.print_exc()
        return _ensure_output_count((gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), f"ユーザー外見: 登録失敗 ({e})"), 6)



def get_avatar_html(room_name: str, state: str = "idle", mode: str = None) -> str:
    """
    ルームのアバター表示用HTMLを生成する。

    Args:
        room_name: ルームのフォルダ名
        state: アバターの状態 ("idle", "thinking", "talking")
        mode: 表示モード ("static"=静止画のみ, "video"=動画優先, None=設定に従う)

    Returns:
        HTML文字列（videoタグまたはimgタグ）
    """
    if not room_name:
        return ""

    # モードが指定されていない場合はルーム設定から取得
    if mode is None:
        effective_settings = config_manager.get_effective_settings(room_name)
        mode = effective_settings.get("avatar_mode", "static")

    # 静止画モード: まず表情差分の静止画を探し、なければ profile.png にフォールバック
    if mode == "static":
        avatar_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.AVATAR_DIR)
        image_exts = [".png", ".jpg", ".jpeg", ".webp"]

        # 1. まず指定された表情の静止画を探す
        for ext in image_exts:
            expr_path = os.path.join(avatar_dir, f"{state}{ext}")
            if os.path.exists(expr_path):
                try:
                    with open(expr_path, "rb") as f:
                        encoded = base64.b64encode(f.read()).decode("utf-8")
                    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
                    mime_type = mime_map.get(ext, "image/png")
                    return f'''<img
                        src="data:{mime_type};base64,{encoded}"
                        style="width:100%; height:200px; object-fit:contain; border-radius:12px;"
                        alt="{state}">'''
                except Exception as e:
                    print(f"--- [Avatar] 表情画像読み込みエラー ({state}): {e} ---")

        # 2. 指定表情がない場合、idle の静止画を探す（state が idle でなければ）
        if state != "idle":
            for ext in image_exts:
                idle_path = os.path.join(avatar_dir, f"idle{ext}")
                if os.path.exists(idle_path):
                    try:
                        with open(idle_path, "rb") as f:
                            encoded = base64.b64encode(f.read()).decode("utf-8")
                        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
                        mime_type = mime_map.get(ext, "image/png")
                        return f'''<img
                            src="data:{mime_type};base64,{encoded}"
                            style="width:100%; height:200px; object-fit:contain; border-radius:12px;"
                            alt="idle">'''
                    except Exception as e:
                        print(f"--- [Avatar] idle画像読み込みエラー: {e} ---")

        # 3. それでもなければ従来の profile.png にフォールバック
        _, _, profile_image_path, _, _, _, _ = get_room_files_paths(room_name)
        if profile_image_path and os.path.exists(profile_image_path):
            try:
                with open(profile_image_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode("utf-8")
                ext = os.path.splitext(profile_image_path)[1].lower()
                mime_type = "image/png" if ext == ".png" else "image/jpeg"
                return f'''<img
                    src="data:{mime_type};base64,{encoded}"
                    style="width:100%; height:200px; object-fit:contain; border-radius:12px;"
                    alt="プロフィール画像">'''
            except Exception as e:
                print(f"--- [Avatar] 画像読み込みエラー: {e} ---")
        # 画像がない場合はプレースホルダー
        return '''<div style="width:100%; height:200px; display:flex; align-items:center; justify-content:center;
            background:var(--background-fill-secondary); border-radius:12px; color:var(--text-color-secondary);">
            プロフィール画像なし
        </div>'''

    # 動画モード: 動画を優先して探し、なければ静止画にフォールバック
    avatar_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.AVATAR_DIR)

    # 動画ファイルの優先順位と MIME タイプ
    video_types = [
        (".mp4", "video/mp4"),
        (".webm", "video/webm"),
        (".gif", "image/gif"),  # GIFはimgタグで表示
    ]

    for ext, mime_type in video_types:
        video_path = os.path.join(avatar_dir, f"{state}{ext}")
        if os.path.exists(video_path):
            try:
                with open(video_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode("utf-8")

                if ext == ".gif":
                    # GIFはimgタグで表示
                    return f'''<img
                        src="data:{mime_type};base64,{encoded}"
                        style="width:100%; height:200px; object-fit:contain; border-radius:12px;"
                        alt="アバター">'''
                else:
                    # 動画はvideoタグで表示
                    return f'''<video
                        src="data:{mime_type};base64,{encoded}"
                        autoplay loop muted playsinline
                        style="width:100%; height:200px; object-fit:contain; border-radius:12px;">
                    </video>'''
            except Exception as e:
                print(f"--- [Avatar] 動画読み込みエラー: {e} ---")

    # 指定表情の動画がない場合、idle 動画を探す（state が idle でなければ）
    if state != "idle":
        for ext, mime_type in video_types:
            idle_path = os.path.join(avatar_dir, f"idle{ext}")
            if os.path.exists(idle_path):
                try:
                    with open(idle_path, "rb") as f:
                        encoded = base64.b64encode(f.read()).decode("utf-8")

                    if ext == ".gif":
                        return f'''<img
                            src="data:{mime_type};base64,{encoded}"
                            style="width:100%; height:200px; object-fit:contain; border-radius:12px;"
                            alt="idle">'''
                    else:
                        return f'''<video
                            src="data:{mime_type};base64,{encoded}"
                            autoplay loop muted playsinline
                            style="width:100%; height:200px; object-fit:contain; border-radius:12px;">
                        </video>'''
                except Exception as e:
                    print(f"--- [Avatar] idle動画読み込みエラー: {e} ---")

    # 動画が見つからない場合は静止画にフォールバック
    _, _, profile_image_path, _, _, _, _ = get_room_files_paths(room_name)

    if profile_image_path and os.path.exists(profile_image_path):
        try:
            with open(profile_image_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("utf-8")
            # 拡張子からMIMEタイプを判定
            ext = os.path.splitext(profile_image_path)[1].lower()
            mime_type = "image/png" if ext == ".png" else "image/jpeg"
            return f'''<img
                src="data:{mime_type};base64,{encoded}"
                style="width:100%; height:200px; object-fit:contain; border-radius:12px;"
                alt="プロフィール画像">'''
        except Exception as e:
            print(f"--- [Avatar] 画像読み込みエラー: {e} ---")

    # 何も見つからない場合はプレースホルダー
    return '''<div style="width:100%; height:200px; display:flex; align-items:center; justify-content:center;
        background:var(--background-fill-secondary); border-radius:12px; color:var(--text-color-secondary);">
        プロフィール画像なし
    </div>'''




def extract_expression_from_response(response_text: str, room_name: str) -> str:
    """
    AI応答テキストから表情を抽出する。

    優先順位:
    1. 【表情】…{expression_name}… タグから抽出
    2. <persona_emotion category="..." /> タグから抽出
    3. MotivationManager の現在感情状態 (内部状態) から取得
    4. デフォルト (neutral)

    Args:
        response_text: AI応答のテキスト
        room_name: ルームのフォルダ名

    Returns:
        表情名 (例: "joy", "sadness", "neutral")
    """
    # 表情設定を読み込む
    expressions_config = room_manager.get_expressions_config(room_name)
    registered_expressions = expressions_config.get("expressions", constants.DEFAULT_EXPRESSIONS)
    default_expression = expressions_config.get("default_expression", "neutral")

    # 1. 手動タグから抽出: 【表情】…{expression_name}…
    if response_text:
        match = re.search(constants.EXPRESSION_TAG_PATTERN, response_text)
        if match:
            expression = match.group(1).lower()
            if expression in registered_expressions:
                print(f"--- [Expression] 手動タグから抽出: {expression} ---")
                return expression
            else:
                print(f"--- [Expression] 手動タグ '{expression}' は未登録 ---")

        # 2. ペルソナ感情タグから抽出: <persona_emotion category="xxx" ... />
        persona_emotion_pattern = r'<persona_emotion\s+category=["\'](\w+)["\']\s+intensity=["\']([0-9.]+)["\']\s*/>'
        emotion_match = re.search(persona_emotion_pattern, response_text, re.IGNORECASE)
        if emotion_match:
            expression = emotion_match.group(1).lower()
            if expression in registered_expressions:
                print(f"--- [Expression] 感情タグから抽出: {expression} ---")
                return expression
            else:
                print(f"--- [Expression] 感情タグ '{expression}' は未登録 ---")

    # 3. 内部状態 (MotivationManager) からのフォールバック
    try:
        mm = MotivationManager(room_name)
        internal_state = mm.get_internal_state()
        persona_emotion = internal_state.get("drives", {}).get("relatedness", {}).get("persona_emotion", "neutral")
        if persona_emotion in registered_expressions:
            print(f"--- [Expression] 内部状態から取得: {persona_emotion} ---")
            return persona_emotion
    except Exception as e:
        print(f"--- [Expression] MotivationManager 取得エラー: {e} ---")

    # 4. デフォルト
    return default_expression


DAY_MAP_EN_TO_JA = {"mon": "月", "tue": "火", "wed": "水", "thu": "木", "fri": "金", "sat": "土", "sun": "日"}

DAY_MAP_JA_TO_EN = {v: k for k, v in DAY_MAP_EN_TO_JA.items()}

def handle_search_provider_change(provider: str):
    """
    検索プロバイダの変更をCONFIG_GLOBALとconfig.jsonに保存する。
    Tavilyが選択された場合はAPIキー入力欄を表示する。
    """
    provider_names = {
        "google": "Google検索 (Gemini Native)",
        "tavily": "Tavily",
        "ddg": "DuckDuckGo",
        "disabled": "無効化"
    }
    return handle_save_global_setting_delta(
        "search_provider",
        provider,
        f"検索プロバイダ「{provider_names.get(provider, provider)}」",
        skip_grace=True
    )


def handle_search_model_change(model: str):
    """Google検索（Geminiグラウンディング）に使うモデルを保存する。"""
    model_name = str(model or "").strip()
    if not model_name:
        gr.Warning("検索モデルが空です。")
        return "検索モデル: 変更をスキップしました（空）"
    return handle_save_global_setting_delta(
        "search_model",
        model_name,
        f"検索モデル「{model_name}」",
        skip_grace=True,
    )


def handle_fetch_search_models(api_key_name, current_model=None):
    """検索モデル候補を Gemini API から取得して Dropdown を更新する。

    検索（グラウンディング）に使えないモデル（embedding/tts/image 等）は除外するが、
    gemma・pro 系は残す。実際の可否はテストボタンで確認させる方針。
    取得結果はグローバルのモデルリスト（available_models）には影響させない。
    """
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name, "")
    if not api_key:
        gr.Warning("Gemini APIキーが選択されていません。基本設定でキーを選択してください。")
        return gr.update()

    models = config_manager.fetch_gemini_models(api_key, exclude_special=True)
    if not models:
        gr.Warning("Gemini モデルリストを取得できませんでした。APIキーやネットワーク設定を確認してください。")
        return gr.update()

    gr.Info(f"検索モデルの候補を取得しました（{len(models)}件）。プランやモデルによっては検索に使えない場合があります。テストで確認してください。")

    # 現在の選択値は維持する（allow_custom_value=True なので候補外でも有効）
    return gr.update(choices=models, value=current_model)


def handle_test_search_model(model, api_key_name):
    """選択中の検索モデル＋Geminiキーで実際にグラウンディング検索を試し、可否を表示する。"""
    model_name = str(model or "").strip()
    if not model_name:
        gr.Warning("検索モデルが選択されていません。")
        return
    from tools import web_tools
    gr.Info(f"モデル「{model_name}」で検索テストを実行中…")
    success, message = web_tools.test_search_model(model_name, api_key_name)
    if success:
        gr.Info(f"✅ {message}")
    else:
        gr.Warning(f"⚠️ {message}")


def handle_save_tavily_key(api_key: str):
    """
    Tavily APIキーを保存する。
    """
    if not api_key or not api_key.strip():
        gr.Warning("APIキーが空です。")
        return

    api_key = api_key.strip()

    # config.jsonに保存
    if config_manager.save_config_if_changed("tavily_api_key", api_key):
        # グローバル変数も更新
        config_manager.TAVILY_API_KEY = api_key
        gr.Info("Tavily APIキーを保存しました。")

def handle_save_zhipu_key(api_key: str):
    """
    Zhipu AI (GLM-4) APIキーを保存する。
    """
    if not api_key or not api_key.strip():
        gr.Warning("APIキーが空です。")
        return

    api_key = api_key.strip()

    # config.jsonに保存
    if config_manager.save_config_if_changed("zhipu_api_key", api_key):
        # グローバル変数も更新
        config_manager.ZHIPU_API_KEY = api_key
        gr.Info("Zhipu APIキーを保存しました。")
    else:
        gr.Info("Zhipu APIキーは既に保存されています。")


def handle_save_moonshot_key(key_value: str):
    """Moonshot AI APIキーを保存する"""
    if not key_value or not key_value.strip():
        gr.Warning("APIキーを入力してください。")
        return

    key_value = key_value.strip()

    # config.jsonに保存
    if config_manager.save_config_if_changed("moonshot_api_key", key_value):
        # グローバル変数も更新
        config_manager.MOONSHOT_API_KEY = key_value
        gr.Info("Moonshot AI APIキーを保存しました。")
    else:
        gr.Info("Moonshot AI APIキーは既に保存されています。")


def handle_save_groq_key(api_key: str):
    """
    Groq APIキーを保存する。
    """
    if not api_key or not api_key.strip():
        gr.Warning("APIキーが空です。")
        return gr.update()

    api_key = api_key.strip()

    # config.jsonに保存
    if config_manager.save_config_if_changed("groq_api_key", api_key):
        # グローバル変数も更新
        config_manager.GROQ_API_KEY = api_key
        gr.Info("Groq APIキーを保存しました。")
    else:
        gr.Info("Groq APIキーは既に保存されています。")

def handle_save_local_model_path(path: str):
    """
    ローカル GGUF モデルのパスを保存する。
    """
    if not path or not path.strip():
        gr.Warning("モデルパスが空です。")
        return

    path = path.strip()

    # config.jsonに保存
    if config_manager.save_config_if_changed("local_model_path", path):
        # グローバル変数も更新
        config_manager.LOCAL_MODEL_PATH = path
        gr.Info("ローカルGGUFモデルのパスを保存しました。")
    else:
        gr.Info("モデルパスは既に保存されています。")

def handle_save_anthropic_key(api_key: str):
    """
    Anthropic APIキーを保存する。
    """
    if not api_key or not api_key.strip():
        gr.Warning("APIキーが空です。")
        return gr.update()

    api_key = api_key.strip()

    if config_manager.save_config_if_changed("anthropic_api_key", api_key):
        config_manager.ANTHROPIC_API_KEY = api_key
        gr.Info("Anthropic APIキーを保存しました。")
    else:
        gr.Info("Anthropic APIキーは既に保存されています。")

    return gr.update()


def handle_save_openai_official_key(api_key: str):
    """
    OpenAI公式APIキーを、「OpenAI」互換プロファイルとして保存・更新する。
    """
    if not api_key or not api_key.strip():
        gr.Warning("APIキーが空です。")
        return gr.update(), gr.update()

    api_key = api_key.strip()

    # 過去互換のため「OpenAI Official」も探す
    existing_profile = None
    provider_name = "OpenAI Official"

    profile_official = config_manager.get_openai_setting_by_name("OpenAI Official")
    profile_short = config_manager.get_openai_setting_by_name("OpenAI")

    if profile_official:
        existing_profile = profile_official
        provider_name = "OpenAI Official"
    elif profile_short:
        existing_profile = profile_short
        provider_name = "OpenAI"

    if existing_profile:
        config_manager.add_or_update_openai_profile({
            "name": provider_name,
            "base_url": existing_profile.get("base_url", "https://api.openai.com/v1"),
            "api_key": api_key,
        })
    else:
        # 新規時は "OpenAI Official" として作成する（過去の生成コードとの互換性）
        config_manager.save_openai_provider_setting(
            name=provider_name,
            base_url="https://api.openai.com/v1",
            api_key=api_key,
            available_models=["gpt-4o", "chatgpt-4o-latest", "gpt-4o-mini", "o1", "o1-mini", "o3-mini"],
            default_model="gpt-4o",
            tool_use_enabled=True
        )

    gr.Info("OpenAI APIキーを保存しました。")

    current_settings = config_manager.get_openai_settings_list()
    choices = [s["name"] for s in current_settings]
    return gr.update(), gr.update(choices=choices, value=provider_name)


def handle_save_nim_key(api_key: str):
    """
    Nvidia NIM APIキーを保存し、OpenAI互換プロファイルとしても登録する。
    """
    if not api_key or not api_key.strip():
        gr.Warning("APIキーが空です。")
        return gr.update(), gr.update()

    api_key = api_key.strip()

    if config_manager.save_config_if_changed("nim_api_key", api_key):
        config_manager.NIM_API_KEY = api_key
        gr.Info("Nvidia NIM APIキーを保存しました。")
    else:
        gr.Info("Nvidia NIM APIキーは既に保存されています。")

    # 値の変更有無に関わらず、OpenAI互換プロファイルとして登録・更新する
    provider_name = "Nvidia NIM"
    config_manager.save_openai_provider_setting(
        name=provider_name,
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=api_key,
        available_models=["meta/llama-3.1-405b-instruct", "meta/llama-3.1-70b-instruct", "meta/llama-3.1-8b-instruct", "mistralai/mixtral-8x22b-instruct-v0.1"],
        default_model="meta/llama-3.1-70b-instruct",
        tool_use_enabled=True
    )
    gr.Info("Nvidia NIM プロファイルを更新しました。")

    # プロファイル一覧を更新して返す (Dropdownの選択肢更新用)
    profiles = [s["name"] for s in config_manager.get_openai_settings_list()]
    return gr.update(), gr.update(choices=profiles)


def handle_save_xai_key(api_key: str):
    """
    X.ai APIキーを保存し、OpenAI互換プロファイルとしても登録する。
    既存プロファイルがある場合はAPIキーのみを更新し、モデルリストは保持する。
    """
    if not api_key or not api_key.strip():
        gr.Warning("APIキーが空です。")
        return gr.update(), gr.update()

    api_key = api_key.strip()

    if config_manager.save_config_if_changed("xai_api_key", api_key):
        config_manager.XAI_API_KEY = api_key
        gr.Info("X.ai APIキーを保存しました。")
    else:
        gr.Info("X.ai APIキーは既に保存されています。")

    # 値の変更有無に関わらず、OpenAI互換プロファイルとして登録・更新する
    provider_name = "X.ai"
    existing_profile = config_manager.get_openai_setting_by_name(provider_name)

    if existing_profile:
        # 既存プロファイルがある場合: APIキーのみ更新（モデルリストやdefault_modelは保持）
        config_manager.add_or_update_openai_profile({
            "name": provider_name,
            "base_url": "https://api.x.ai/v1",
            "api_key": api_key,
        })
    else:
        # 新規プロファイル: 初期モデルリストを設定
        config_manager.save_openai_provider_setting(
            name=provider_name,
            base_url="https://api.x.ai/v1",
            api_key=api_key,
            available_models=["grok-beta", "grok-vision-beta", "grok-2", "grok-3"],
            default_model="grok-3",
            tool_use_enabled=True
        )

    # xAI プロファイルをアクティブに設定
    config_manager.set_active_openai_profile(provider_name)
    gr.Info("X.ai プロファイルを更新し、アクティブに設定しました。")

    profiles = [s["name"] for s in config_manager.get_openai_settings_list()]
    return gr.update(), gr.update(choices=profiles)


def handle_save_elevenlabs_key(api_key: str):
    """
    ElevenLabs APIキーを保存する。
    """
    if not api_key or not api_key.strip():
        gr.Warning("APIキーが空です。")
        return gr.update()

    api_key = api_key.strip()

    if config_manager.save_config_if_changed("elevenlabs_api_key", api_key):
        gr.Info("ElevenLabs APIキーを保存しました。")
    else:
        gr.Info("ElevenLabs APIキーは既に保存されています。")

    return gr.update()


def handle_save_huggingface_key_main(api_key: str):
    if not api_key or not api_key.strip():
        gr.Warning("APIキーが空です。")
        return gr.update()
    api_key = api_key.strip()
    config_manager.CONFIG_GLOBAL.setdefault("image_generation_settings", {})
    config_manager.CONFIG_GLOBAL["image_generation_settings"]["huggingface_api_token"] = api_key
    config_manager._save_config_file(config_manager.CONFIG_GLOBAL)
    gr.Info("Hugging Face APIキーを保存しました。")
    return gr.update()

def handle_save_pollinations_key_main(api_key: str):
    if api_key:
        api_key = api_key.strip()
    config_manager.CONFIG_GLOBAL.setdefault("image_generation_settings", {})
    config_manager.CONFIG_GLOBAL["image_generation_settings"]["pollinations_api_key"] = api_key
    config_manager._save_config_file(config_manager.CONFIG_GLOBAL)
    gr.Info("Pollinations.ai APIキーを保存しました。")
    return gr.update()

def handle_add_custom_openai_provider(name: str, base_url: str, api_key: str):
    """
    カスタムのOpenAI互換プロバイダーを登録する。
    """
    if not name or not name.strip():
        gr.Warning("プロバイダー名を入力してください。")
        return gr.update(), gr.update(), gr.update(), gr.update()

    if not base_url or not base_url.strip():
        gr.Warning("Base URLを入力してください。")
        return gr.update(), gr.update(), gr.update(), gr.update()

    name = name.strip()
    base_url = base_url.strip()
    api_key = api_key.strip() if api_key else ""

    config_manager.save_openai_provider_setting(
        name=name,
        base_url=base_url,
        api_key=api_key,
        available_models=[],  # 最初は空設定
        default_model="",
        tool_use_enabled=True
    )

    gr.Info(f"カスタムプロバイダー「{name}」を登録しました。")

    profiles = [s["name"] for s in config_manager.get_openai_settings_list()]

    # 入力欄をクリアし、Dropdownのリストを更新する
    return gr.update(value=""), gr.update(value=""), gr.update(value=""), gr.update(choices=profiles, value=name)


def handle_add_ollama_preset():
    """
    Ollama用のOpenAI互換プロファイルを自動登録する。
    """
    provider_name = "Ollama (Local)"

    config_manager.save_openai_provider_setting(
        name=provider_name,
        base_url="http://localhost:11434/v1",
        api_key="ollama",  # Ollama requires some string for API key
        available_models=[],
        default_model="",
        tool_use_enabled=False # Ollamaは現時点でTools安定しない場合が多い
    )

    gr.Info("Ollama用の接続設定プロファイルを追加しました。")
    profiles = [s["name"] for s in config_manager.get_openai_settings_list()]
    return gr.update(choices=profiles, value=provider_name)

def handle_add_huggingface_preset():
    """
    Hugging Face Inference API用のOpenAI互換プロファイルを自動登録する。
    画像生成と共通のAPIキーを引き継ぐ。
    """
    provider_name = "Hugging Face"

    # 画像設定側からAPIキーを取得
    config_manager.load_config()
    hf_token = config_manager.CONFIG_GLOBAL.get("huggingface_api_token", "")

    config_manager.save_openai_provider_setting(
        name=provider_name,
        base_url="https://router.huggingface.co/v1",
        api_key=hf_token,
        available_models=["meta-llama/Llama-3.3-70B-Instruct"],
        default_model="meta-llama/Llama-3.3-70B-Instruct",
        tool_use_enabled=True # HFは対応モデルならTool Use可能
    )

    info_msg = "Hugging Face用のプロファイルを追加しました。"
    if hf_token:
        info_msg += " (画像生成用のAPIキーを自動適用しました)"
    gr.Info(info_msg)

    profiles = [s["name"] for s in config_manager.get_openai_settings_list()]
    return gr.update(choices=profiles, value=provider_name)

def handle_add_pollinations_preset():
    """
    Pollinations.ai用のOpenAI互換プロファイルを自動登録する。
    """
    provider_name = "Pollinations.ai"

    config_manager.save_openai_provider_setting(
        name=provider_name,
        base_url="https://text.pollinations.ai/openai",
        api_key="pollinations", # APIキーは必須ではないがダミーを入れる
        available_models=["openai", "mistral", "qwen-coder", "qwen-safety", "gemini-fast", "nova-fast"],
        default_model="mistral",
        tool_use_enabled=True # Pollinationsは対応モデルにより異なるが、一部機能は動作可能
    )

    gr.Info("Pollinations用の設定プロファイルを追加しました。")
    profiles = [s["name"] for s in config_manager.get_openai_settings_list()]
    return gr.update(choices=profiles, value=provider_name)

def handle_save_cloudflare_url(room_name: str, webhook_domain: str):
    """CloudflareトンネルURLのみを迅速に保存する専用ハンドラ"""
    if not room_name:
        gr.Warning("設定を保存するルームが選択されていません。")
        return

    webhook_domain = webhook_domain.strip() if webhook_domain else ""

    # 既存のroblox_settingsを読み込み
    current_config = room_manager.get_room_config(room_name) or {}
    override = current_config.get("override_settings", {})
    roblox_settings = override.get("roblox_settings", {})

    # webhook_domainだけ更新
    roblox_settings["webhook_domain"] = webhook_domain

    result = room_manager.update_room_config(room_name, {"roblox_settings": roblox_settings})
    if result == True:
        gr.Info(f"Cloudflare URLを保存しました: {webhook_domain[:50]}...")
    elif result == "no_change":
        gr.Info("URLは変更されていません。")
    else:
        gr.Error("URLの保存中にエラーが発生しました。")


def load_roblox_guide():
    """Robloxクイックスタートガイドのマークダウンファイルを読み込んで返す"""
    guide_path = os.path.join(_UI_HANDLERS_PROJECT_ROOT, "assets", "guides", "roblox_quickstart_guide.md")
    try:
        if os.path.exists(guide_path):
            with open(guide_path, "r", encoding="utf-8") as f:
                return f.read()
        else:
            return "⚠️ ガイドファイルが見つかりません。"
    except Exception as e:
        return f"⚠️ ガイドの読み込みに失敗しました: {e}"


def handle_save_roblox_settings(room_name: str, api_key: str, universe_id: str, topic: str, webhook_enabled: bool, activation_mode: str, webhook_domain: str, filtering_enabled: bool = True):
    """ROBLOX連携の設定を保存する"""
    if not room_name:
        gr.Warning("設定を保存するルームが選択されていません。")
        return gr.update()

    # config.json に共通設定として保存しない理由は、ゲーム連携は個別の部屋（ペルソナごと）に行う可能性が高いため、部屋別のoverride_settingsとして保存する

    # [Phase 2] Webhook Secret の自動生成
    current_settings = room_manager.get_room_config(room_name) or {}
    secret = current_settings.get("roblox_webhook_secret", "")

    if not secret:
        import secrets
        secret = secrets.token_hex(16)

    new_settings = {
        "roblox_settings": {
            "api_key": api_key.strip() if api_key else "",
            "universe_id": universe_id.strip() if universe_id else "",
            "topic": topic.strip() if topic else "NexusArkCommands",
            "webhook_domain": webhook_domain.strip() if webhook_domain else "",
            "webhook_enabled": bool(webhook_enabled),
            "activation_mode": activation_mode if activation_mode in ["auto", "enabled", "disabled"] else "auto",
            "filtering_enabled": bool(filtering_enabled)  # Step 14: チャットフィルタリング設定
        },
        "roblox_webhook_secret": secret
    }

    result = room_manager.update_room_config(room_name, new_settings)
    if result == True:
        gr.Info(f"「{room_name}」のROBLOX設定を保存しました。")
    elif result == "no_change":
        gr.Info("設定は変更されていません。")
    else:
        gr.Error("ROBLOX設定の保存中にエラーが発生しました。")

    return gr.update(value=secret)


def handle_test_roblox_connection(room_name: str, api_key: str, universe_id: str, topic: str) -> str:
    """入力された設定値を使用してROBLOXへの接続テストを実行する"""
    try:
        from tools.roblox_tools import test_roblox_connection
        result = test_roblox_connection(room_name, api_key, universe_id, topic)
        if "✅" in result:
            gr.Info("ROBLOXへの接続テストに成功しました！")
        else:
            gr.Warning("ROBLOXへの接続テストに失敗しました。")
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"❌ エラーが発生しました: {str(e)}"

def handle_save_local_model_path(model_path: str):
    """
    ローカルLLM (llama.cpp) のGGUFモデルパスを保存する。
    """
    model_path = model_path.strip() if model_path else ""

    # パスが空でも保存可能（無効化のため）
    if config_manager.save_config_if_changed("local_model_path", model_path):
        # グローバル変数も更新
        config_manager.LOCAL_MODEL_PATH = model_path
        if model_path:
            gr.Info("ローカルモデルパスを保存しました。")
        else:
            gr.Info("ローカルモデルパスをクリアしました。")
    else:
        if _initialization_completed:
            gr.Info("ローカルモデルパスは既に保存されています。")

def _get_location_choices_for_ui(room_name: str) -> list:
    """
    UIの移動先Dropdown用の、エリアごとにグループ化された選択肢リストを生成する。
    """
    if not room_name: return []

    world_settings_path = get_world_settings_path(room_name)
    world_data = utils.parse_world_file(world_settings_path)

    if not world_data: return []

    choices = []
    for area_name in sorted(world_data.keys()):
        choices.append((f"[{area_name}]", f"__AREA_HEADER_{area_name}"))

        places = world_data[area_name]
        for place_name in sorted(places.keys()):
            if place_name.startswith("__"): continue
            choices.append((f"\u00A0\u00A0→ {place_name}", place_name))

    return choices

def _create_redaction_df_from_rules(rules: List[Dict]) -> pd.DataFrame:
    """
    ルールの辞書リストから、UI表示用のDataFrameを作成するヘルパー関数。
    この関数で、キーと列名のマッピングを完結させる。
    """
    if not rules:
        return pd.DataFrame(columns=["元の文字列 (Find)", "置換後の文字列 (Replace)", "背景色"])
    df_data = [
        {
            "元の文字列 (Find)": r.get("find", ""),
            "置換後の文字列 (Replace)": r.get("replace", ""),
            "背景色": r.get("color", "#FFFF00")
        } for r in rules
    ]
    return pd.DataFrame(df_data)


def _apply_redaction_rules_to_display_content(content: str, rules: List[Dict]) -> str:
    if not content or not rules:
        return content
    redacted = content
    for rule in rules:
        find_str = rule.get("find")
        if not find_str:
            continue
        replace_str = rule.get("replace", "")
        color = rule.get("color")
        escaped_replace = html.escape(str(replace_str))
        replacement = f'<span style="background-color: {color};">{escaped_replace}</span>' if color else escaped_replace
        if rule.get("regex"):
            try:
                redacted = re.sub(str(find_str), replacement, redacted)
            except re.error:
                redacted = redacted.replace(html.escape(str(find_str)), replacement)
        else:
            redacted = redacted.replace(html.escape(str(find_str)), replacement)
    return redacted


def _room_switch_display_history_limit(limit_key: Any) -> Any:
    """ルーム切替時のUI再描画は直近20件までに抑え、API用履歴設定は別stateで保持する。"""
    if limit_key in ("all", "today", "全ログ"):
        return "20"
    try:
        if int(limit_key) > 20:
            return "20"
    except (TypeError, ValueError):
        pass
    return limit_key


def _update_chat_tab_for_room_change(room_name: str, api_key_name: str):
    """
    【v7: 現在地初期化・同期FIX版】
    チャットタブ関連のUIを更新する。現在地が未設定の場合の初期化もここで行う。
    情景関連の処理は、全て司令塔である _get_updated_scenery_and_image に一任する。
    """
    t0 = time.perf_counter()
    t_step = t0
    # --- [Refactor] ログ読み込みをAPIキーチェック前に移動 ---
    # ルーム名が空の場合の補完
    if not room_name:
        room_list = room_manager.get_room_list_for_ui()
        room_name = room_list[0][1] if room_list else "Default"

    effective_settings = config_manager.get_effective_settings(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: config_manager.get_effective_settings", t_step)
    profile_choices = [s["name"] for s in config_manager.get_openai_settings_list()]

    # 履歴取得設定
    limit_key = effective_settings.get("api_history_limit", "all")
    display_limit_key = _room_switch_display_history_limit(limit_key)
    add_timestamp_val = effective_settings.get("add_timestamp", False)
    display_thoughts_val = effective_settings.get("display_thoughts", True)

    # ログ読み込み (APIキーが無効でも閲覧は可能にするため)
    chat_history, mapping_list = reload_chat_log(
        room_name=room_name,
        api_history_limit_value=display_limit_key,
        add_timestamp=add_timestamp_val,
        display_thoughts=display_thoughts_val
    )
    t_step = _perf_log("_update_chat_tab_for_room_change: reload_chat_log", t_step)

    # --- [Fix] override_settings を先に読み込む ---
    room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
    room_config = {}
    if os.path.exists(room_config_path):
        try:
            with open(room_config_path, "r", encoding="utf-8") as f:
                room_config = json.load(f)
        except: pass

    # override_settings内を優先し、なければルートレベルを確認（手動/自動更新の両方に対応）
    override_settings = room_config.get("override_settings", {})
    room_provider_override = config_manager.normalize_room_provider_override(override_settings.get("provider"))
    room_draft_api_key_name = config_manager._clean_api_key_name(override_settings.get("api_key_name"))

    # APIキーの決定ロジック (v8: Common Settings Fallback)
    # 1. 有効なルーム個別プロバイダ設定のAPIキー
    # 2. グローバル設定 (last_api_key_name)
    # 3. 最初の利用可能なキー
    effective_api_key_name = room_draft_api_key_name if room_provider_override is not None else None
    if not effective_api_key_name:
        effective_api_key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name")

    # それでもなければ、登録されているキーの最初を使う
    if not effective_api_key_name and config_manager.GEMINI_API_KEYS:
        effective_api_key_name = list(config_manager.GEMINI_API_KEYS.keys())[0]

    room_api_key_ui_value = room_draft_api_key_name or effective_api_key_name

    api_key = config_manager.GEMINI_API_KEYS.get(effective_api_key_name)
    has_valid_key = api_key and not api_key.startswith("YOUR_API_KEY")

    if not has_valid_key:
        _perf_log("_update_chat_tab_for_room_change: invalid key return total", t0)
        # APIキー無効時（オンボーディングモード）: チャット履歴は非表示、UIは無効化
        # 他のUI項目も適切なデフォルト値で埋める (既存のreturn tuple構造を維持)
        return (
            room_name, [], [],  # チャット履歴を空にしてオンボーディングガイドのみ表示
            gr.update(interactive=False, placeholder="まず、左の「設定」からAPIキーを設定してください。"),
            get_avatar_html(room_name, state="idle"), "", "", "", "", "", gr.update(choices=[], value=None), "", "", "",
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(),  # location_dropdown - 空choicesでvalueを設定するとエラーになるため更新をスキップ
            "（APIキーが設定されていません）", # current_scenery_display
            config_manager.tts_provider_display_from_key(effective_settings.get("tts_provider", "gemini")), # room_tts_provider_dropdown
            gr.update(choices=profile_choices, value=None, visible=False),  # room_tts_profile_dropdown
            gr.update(choices=config_manager.get_tts_model_choices(effective_settings.get("tts_provider", "gemini")), value=effective_settings.get("tts_model", "gemini-3.1-flash-tts-preview")), # room_tts_model_dropdown
            gr.update(choices=config_manager.get_tts_voice_choices(effective_settings.get("tts_provider", "gemini")), value=config_manager.tts_voice_display_from_id(effective_settings.get("tts_provider", "gemini"), effective_settings.get("tts_voice", effective_settings.get("voice_id", "iapetus")))), # voice_dropdown
            effective_settings.get("tts_style_prompt", effective_settings.get("voice_style_prompt", "")),
            effective_settings.get("tts_voice_speed", 1.0),
            effective_settings.get("tts_voice_pitch", 0.0),
            effective_settings.get("tts_voice_intonation", 1.0),
            effective_settings.get("tts_voice_volume", 1.0),
            True, constants.DEFAULT_STREAMING_SPEED,  # voice_style_prompt, speed, pitch, intonation, volume, enable_typewriter, streaming_speed
            0.8, 0.95, "高リスクのみブロック", "高リスクのみブロック", "高リスクのみブロック", "高リスクのみブロック",
            display_thoughts_val, # Use loaded setting
            False, # send_thoughts
            True,  # enable_auto_retrieval
            add_timestamp_val,  # Use loaded setting
            True,  # send_current_time
            True,  # send_notepad
            True,  # use_common_prompt
            True,  # send_core_memory
            False, # send_scenery
            "変更時のみ", # scenery_send_mode
            False, # auto_memory_enabled
            True,  # room_enable_self_awareness_checkbox
            f"ℹ️ *現在選択中のルーム「{room_name}」にのみ適用される設定です。*", None,
            True, gr.update(open=True),
            gr.update(value=constants.API_HISTORY_LIMIT_OPTIONS.get(constants.DEFAULT_API_HISTORY_LIMIT_OPTION, "20往復")),  # room_api_history_limit_dropdown
            gr.update(value="既定 (AIに任せる / 通常モデル)"),  # room_thinking_level_dropdown
            constants.DEFAULT_API_HISTORY_LIMIT_OPTION,  # api_history_limit_state
            gr.update(value=constants.EPISODIC_MEMORY_OPTIONS.get(constants.DEFAULT_EPISODIC_MEMORY_DAYS, "なし（無効）")),  # room_episode_memory_days_dropdown
            gr.update(value="昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** 取得エラー"),  # episodic_memory_info_display
            gr.update(value=False),  # room_enable_autonomous_checkbox
            gr.update(value=120),  # room_autonomous_inactivity_slider
            gr.update(value=True), # room_allow_schedule_tool_checkbox
            gr.update(value=60), # room_schedule_cooldown_slider
            gr.update(value=""),   # room_autonomous_guidelines_textbox
            gr.update(value="00:00"),  # room_quiet_hours_start
            gr.update(value="07:00"),  # room_quiet_hours_end
            gr.update(value="write"),  # room_persona_workspace_permission_tier_dropdown
            gr.update(value=False),  # room_agent_delegation_enabled_checkbox
            gr.update(value="read"),  # room_agent_delegation_permission_tier_dropdown
            gr.update(value=False),  # room_agent_delegation_allow_web_checkbox
            gr.update(value=False),  # room_agent_delegation_wake_on_completion_checkbox
            gr.update(value=True),  # room_agent_delegation_wake_respect_quiet_hours_checkbox
            *_delegation_model_updates({}, room_scope=True),
            gr.update(value=format_agent_delegation_backend_info(room_name)),  # room_agent_delegation_backend_info
            gr.update(value=None),  # room_model_dropdown (Dropdown)
            # [Phase 3] 個別プロバイダ設定
            gr.update(value="default"),  # room_provider_radio
            gr.update(visible=False),  # room_google_settings_group
            gr.update(visible=False),  # room_openai_settings_group
            gr.update(value=room_api_key_ui_value),  # room_api_key_dropdown (draft value if available)
            gr.update(choices=[s["name"] for s in config_manager.get_openai_settings_list()], value=None),  # room_openai_profile_dropdown
            gr.update(value=""),  # room_openai_base_url_input
            gr.update(value=""),  # room_openai_api_key_input
            gr.update(value=None),  # room_openai_model_dropdown
            gr.update(value=True),  # room_openai_tool_use_checkbox
            gr.update(value=config_manager.CONFIG_GLOBAL.get("enable_api_key_rotation", None)),  # room_rotation_dropdown
            gr.update(value=""),    # roblox_api_key_input
            gr.update(value=""),    # roblox_universe_id_input
            gr.update(value="NexusArkCommands"), # roblox_topic_input
            gr.update(value=True), # roblox_webhook_enabled_checkbox
            gr.update(value="auto"), # [追加] roblox_activation_mode_radio (不整合修正)
            gr.update(value=""), # roblox_webhook_domain_input
            gr.update(value=""),    # roblox_webhook_secret_input
            gr.update(value=True), # [追加] roblox_filtering_enabled_checkbox (不整合修正)
            # --- 睡眠時記憶整理 (Default values) ---
            gr.update(value=True),  # sleep_episodic
            gr.update(value=True),  # sleep_memory_index
            gr.update(value=True),  # sleep_current_log
            gr.update(value=True),  # sleep_entity
            gr.update(value=True),  # sleep_compress
            gr.update(value=True),  # sleep_extract_questions
            gr.update(value="未実行"), # compress_episodes_status
            # --- [v25] テーマ設定 (Default values) ---
            gr.update(value=False),  # room_theme_enabled
            gr.update(value="Chat (Default)"),  # chat_style
            gr.update(value=15),  # font_size
            gr.update(value=1.6),  # line_height
            gr.update(value=None),  # primary
            gr.update(value=None),  # secondary
            gr.update(value=None),  # bg
            gr.update(value=None),  # text
            gr.update(value=None),  # accent_soft
            # --- 詳細設定 (Default values) ---
            gr.update(value=None),  # input_bg
            gr.update(value=None),  # input_border
            gr.update(value=None),  # code_bg
            gr.update(value=None),  # subdued_text
            gr.update(value=None),  # button_bg
            gr.update(value=None),  # button_hover
            gr.update(value=None),  # stop_button_bg
            gr.update(value=None),  # stop_button_hover
            gr.update(value=None),  # checkbox_off
            gr.update(value=None),  # table_bg
            gr.update(value=None),  # radio_label
            gr.update(value=None),  # dropdown_list_bg
            gr.update(value=0.9),  # ui_opacity
            # 背景画像設定 (Default values)
            gr.update(value=None),  # bg_image
            gr.update(value=0.4),  # bg_opacity
            gr.update(value=0),    # bg_blur
            gr.update(value="cover"), # bg_size
            gr.update(value="center"), # bg_position
            gr.update(value="no-repeat"), # bg_repeat
            gr.update(value="300px"), # bg_custom_width
            gr.update(value=0), # bg_radius
            gr.update(value=0), # bg_mask_blur
            gr.update(value=False), # bg_front_layer
            gr.update(value="画像を指定 (Manual)"), # bg_src_mode
            # Sync設定
            gr.update(value=0.4),  # sync_opacity
            gr.update(value=0),    # sync_blur
            gr.update(value="cover"), # sync_size
            gr.update(value="center"), # sync_position
            gr.update(value="no-repeat"), # sync_repeat
            gr.update(value="300px"), # sync_custom_width
            gr.update(value=0), # sync_radius
            gr.update(value=0), # sync_mask_blur
            gr.update(value=False), # sync_front_layer
            # ---
            gr.update(), # save_room_theme_button
            gr.update(value="<style></style>"),  # style_injector
            # --- [Phase 11/12] 夢日記リセット対応 ---
            gr.update(),  # dream_date_dropdown - 空choicesでvalueを設定するとエラーになるため更新をスキップ
            gr.update(value="日付を選択すると、ここに詳細が表示されます。"), # dream_detail_text
            gr.update(choices=["すべて"], value="すべて"), # dream_year_filter
            gr.update(choices=["すべて"], value="すべて"), # dream_month_filter
            # --- [Phase 14] エピソード記憶閲覧リセット ---
            gr.update(),  # episodic_date_dropdown - 空choicesでvalueを設定するとエラーになるため更新をスキップ
            gr.update(value="日付を選択してください"), # episodic_detail_text
            gr.update(choices=["すべて"], value="すべて"), # episodic_year_filter
            gr.update(choices=["すべて"], value="すべて"), # episodic_month_filter
            gr.update(value="待機中"), # episodic_update_status
            gr.update(),  # entity_dropdown - 空choicesでvalueを設定するとエラーになるため更新をスキップ
            gr.update(value=""), # entity_content_editor
            gr.update(value="google"), # embedding_provider_radio (旧: embedding_mode_radio)
            gr.update(value="未実行"), # dream_status_display
            gr.update(value=False), # room_auto_summary_checkbox
            gr.update(value=constants.AUTO_SUMMARY_DEFAULT_THRESHOLD, visible=False), # room_auto_summary_threshold_slider
            gr.update(value=""), # room_project_root_input
            gr.update(value=""), # room_project_exclude_dirs_input
            gr.update(value=""), # room_project_exclude_files_input
            # --- [Avatar Expressions] ---
            gr.update(value=refresh_expressions_ui(room_name)), # expressions_html
            gr.update(choices=get_all_expression_choices(room_name), value=None), # expression_target_dropdown
            gr.update(choices=[constants.CREATIVE_NOTES_FILENAME], value=constants.CREATIVE_NOTES_FILENAME), # creative_notes_file_dropdown
            gr.update(choices=[constants.RESEARCH_NOTES_FILENAME], value=constants.RESEARCH_NOTES_FILENAME), # research_notes_file_dropdown
            # --- [新規] 一時的現在地 UI 同期用 ---
            "", # scenery
            gr.update(choices=[], value=None), # saved_locations
            None, # image_path
            gr.update(selected="virtual_location_tab") # tabs
        )

    # --- 【通常モード】 (APIキー有効) ---

    # ステップ1: UIに表示するための場所リストを先に生成
    locations_for_ui = _get_location_choices_for_ui(room_name)
    valid_location_ids = [value for _name, value in locations_for_ui if not value.startswith("__AREA_HEADER_")]

    # ステップ2: 現在地ファイルを確認し、なければ初期化
    current_location_from_file = utils.get_current_location(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: utils.get_current_location", t_step)
    if not current_location_from_file or current_location_from_file not in valid_location_ids:
        # 世界設定に "リビング" が存在すればそれを、なければ最初の有効な場所をデフォルトにする
        new_location = "リビング" if "リビング" in valid_location_ids else (valid_location_ids[0] if valid_location_ids else None)
        if new_location:
            from tools.space_tools import set_current_location
            set_current_location.func(location_id=new_location, room_name=room_name)
            gr.Info(f"現在地が未設定または無効だったため、「{new_location}」に自動で設定しました。")
            current_location_from_file = new_location # 状態を更新
        else:
            gr.Warning("現在地が未設定ですが、世界設定に有効な場所が一つもありません。")
            current_location_from_file = None
    t_step = _perf_log("_update_chat_tab_for_room_change: location init", t_step)

    # ステップ3: 司令塔を呼び出す
    scenery_text, scenery_image_path = _get_updated_scenery_and_image(room_name, api_key_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: _get_updated_scenery_and_image", t_step)

    # --- 以降、取得した値を使ってUI更新値を構築する ---
    # effective_settings は既にロード済み

    # 設定ファイルにはキー("10")が入っているので、UI表示用("10往復")に変換
    limit_display = constants.API_HISTORY_LIMIT_OPTIONS.get(limit_key, "全ログ")

    episode_key = effective_settings.get("episode_memory_lookback_days", constants.DEFAULT_EPISODIC_MEMORY_DAYS)
    episode_display = constants.EPISODIC_MEMORY_OPTIONS.get(episode_key, "過去 2週間")

    # --- [v25] 思考設定の連動ロジック ---
    # display_thoughts_val はロード済み
    send_thoughts_val = effective_settings.get("send_thoughts", True)
    send_thoughts_interactive = display_thoughts_val  # 「表示」がオンの時だけ「送信」を操作可能に
    if not display_thoughts_val:
        send_thoughts_val = False  # 「表示」がオフなら「送信」も強制オフ

    # reload_chat_log は実行済み (chat_history, mapping_list)

    _, _, img_p, id_mem_p, diary_mem_p, notepad_p, _ = get_room_files_paths(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: get_room_files_paths", t_step)

    # 永続記憶の読み取り
    identity_str = ""
    if id_mem_p and os.path.exists(id_mem_p):
        with open(id_mem_p, "r", encoding="utf-8") as f: identity_str = f.read()
    t_step = _perf_log("_update_chat_tab_for_room_change: identity file", t_step)

    # 日記の読み取り（メインエディタ用：最新エントリのみ表示）
    memory_str = ""
    if diary_mem_p and os.path.exists(diary_mem_p):
        with open(diary_mem_p, "r", encoding="utf-8") as f:
            d_content = f.read()
        d_entries = _parse_diary_entries(d_content)
        if d_entries:
            d_entries.sort(key=lambda x: x["date"], reverse=True)
            memory_str = d_entries[0]["content"]
    t_step = _perf_log("_update_chat_tab_for_room_change: diary file", t_step)
    # 動画アバターをサポートするHTML生成関数を使用
    profile_image = get_avatar_html(room_name, state="idle")
    t_step = _perf_log("_update_chat_tab_for_room_change: get_avatar_html", t_step)
    notepad_content = load_notepad_content(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: notepad file", t_step)
    creative_notes_content = load_creative_notes_content(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: creative notes file", t_step)
    research_notes_content = load_research_notes_content(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: research notes file", t_step)

    # location_dd_val を、ファイルから読み込んだ（または初期化した）値に修正
    location_dd_val = current_location_from_file

    tts_provider_key = config_manager.tts_provider_key_from_display(effective_settings.get("tts_provider", "gemini"))
    tts_provider_display = config_manager.tts_provider_display_from_key(tts_provider_key)
    tts_profile_for_voice = effective_settings.get("tts_profile_name")
    if not tts_profile_for_voice and profile_choices:
        tts_profile_for_voice = profile_choices[0]
    if tts_provider_key == "openai_compatible":
        room_model_choices = config_manager.get_openai_compatible_tts_model_choices_for_profile(tts_profile_for_voice)
    else:
        room_model_choices = config_manager.get_tts_model_choices(tts_provider_key)
    tts_model_val = effective_settings.get("tts_model") or (room_model_choices[0] if room_model_choices else None)
    if room_model_choices and tts_model_val not in room_model_choices:
        tts_model_val = room_model_choices[0]
    elif not room_model_choices and tts_provider_key == "openai_compatible":
        tts_model_val = None
    if tts_provider_key == "openai_compatible":
        room_voice_map = config_manager.get_openai_compatible_tts_voice_map_for_profile(tts_profile_for_voice, model_name=tts_model_val)
        voice_id_for_display = effective_settings.get("tts_voice", effective_settings.get("voice_id", "iapetus"))
        voice_display_name = room_voice_map.get(voice_id_for_display) or next(iter(room_voice_map.values()), "")
        room_voice_choices = list(room_voice_map.values())
    else:
        voice_display_name = config_manager.tts_voice_display_from_id(
            tts_provider_key,
            effective_settings.get("tts_voice", effective_settings.get("voice_id", "iapetus"))
        )
        room_voice_choices = config_manager.get_tts_voice_choices(tts_provider_key)
    voice_style_prompt_val = effective_settings.get("tts_style_prompt", effective_settings.get("voice_style_prompt", ""))
    safety_display_map = {
        "BLOCK_NONE": "ブロックしない", "BLOCK_LOW_AND_ABOVE": "低リスク以上をブロック",
        "BLOCK_MEDIUM_AND_ABOVE": "中リスク以上をブロック", "BLOCK_ONLY_HIGH": "高リスクのみブロック"
    }
    harassment_val = safety_display_map.get(effective_settings.get("safety_block_threshold_harassment"))
    hate_val = safety_display_map.get(effective_settings.get("safety_block_threshold_hate_speech"))
    sexual_val = safety_display_map.get(effective_settings.get("safety_block_threshold_sexually_explicit"))
    dangerous_val = safety_display_map.get(effective_settings.get("safety_block_threshold_dangerous_content"))
    core_memory_content = load_core_memory_content(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: load_core_memory_content", t_step)

    try:
        manager = EpisodicMemoryManager(room_name)
        latest_date = manager.get_latest_memory_date()
        episodic_info_text = f"昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** {latest_date}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        episodic_info_text = "昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** 取得エラー"
    t_step = _perf_log("_update_chat_tab_for_room_change: EpisodicMemoryManager latest", t_step)

    auto_settings = effective_settings.get("autonomous_settings", {})
    auto_enabled = auto_settings.get("enabled", False)
    auto_inactivity = auto_settings.get("inactivity_minutes", 120)
    quiet_start = auto_settings.get("quiet_hours_start", "00:00")
    quiet_end = auto_settings.get("quiet_hours_end", "07:00")
    agent_policy = effective_settings.get("agent_delegation_settings", {}) or {}
    persona_workspace = effective_settings.get("persona_workspace", {}) or {}

    roblox_settings = effective_settings.get("roblox_settings", {})
    roblox_api_key_val = roblox_settings.get("api_key", "")
    roblox_universe_id_val = roblox_settings.get("universe_id", "")
    roblox_topic_val = roblox_settings.get("topic", "NexusArkCommands")
    roblox_webhook_enabled = roblox_settings.get("webhook_enabled", True)
    roblox_webhook_domain = roblox_settings.get("webhook_domain", "")
    roblox_webhook_secret_val = override_settings.get("roblox_webhook_secret", "")

    # 睡眠時記憶整理設定
    sleep_consolidation = effective_settings.get("sleep_consolidation", {})
    sleep_episodic = sleep_consolidation.get("update_episodic_memory", True)
    sleep_memory_index = sleep_consolidation.get("update_memory_index", True)
    sleep_current_log = sleep_consolidation.get("update_current_log_index", True)
    sleep_entity = sleep_consolidation.get("update_entity_memory", True)
    sleep_compress = sleep_consolidation.get("compress_old_episodes", True)
    sleep_extract_questions = sleep_consolidation.get("extract_open_questions", True)
    # 圧縮状況の詳細を動的に取得
    stats = EpisodicMemoryManager(room_name).get_compression_stats()
    last_date = stats["last_compressed_date"] or "なし"
    pending = stats["pending_count"]
    t_step = _perf_log("_update_chat_tab_for_room_change: EpisodicMemoryManager compression", t_step)

    # ルーム設定を直接読み込んで最終実行結果を取得
    # room_config, override_settings は関数の冒頭で読み込み済み

    last_exec = override_settings.get("last_compression_result") or room_config.get("last_compression_result", "未実行")
    # 表示用の文字列を構築 (例: 2024-06-15まで圧縮済み (対象: 12件) | 最終結果: 圧縮完了...)
    last_compression_result = f"{last_date}まで圧縮済み (対象: {pending}件) | 最終: {last_exec}"

    # エピソード更新のステータス復元
    last_episodic_update = override_settings.get("last_episodic_update") or room_config.get("last_episodic_update", "未実行")

    # プロジェクト探索設定
    project_explorer = effective_settings.get("project_explorer", {})
    project_root = project_explorer.get("root_path", "")
    project_exclude_dirs = ", ".join(project_explorer.get("exclude_dirs", []))
    project_exclude_files = ", ".join(project_explorer.get("exclude_files", []))

    # エンティティ一覧の初期取得
    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    entity_choices = em.list_entries()
    entity_choices.sort()
    t_step = _perf_log("_update_chat_tab_for_room_change: EntityMemoryManager.list_entries", t_step)

    # 最終ドリーム時間の取得
    last_dream_time = "未実行"
    try:
        from dreaming_manager import DreamingManager
        # api_key is available as api_key in this scope? No, it's passed as api_key_name?
        # Actually in _update_chat_tab_for_room_change, api_key is retrieved earlier.
        # Let's check where api_key is defined.
        # It is defined around line 380: api_key = ...
        dm = DreamingManager(room_name, api_key)
        last_dream_time = dm.get_last_dream_time()
    except Exception:
        pass
    t_step = _perf_log("_update_chat_tab_for_room_change: DreamingManager last dream", t_step)

    room_openai_settings = override_settings.get("openai_settings") or {}
    # [Phase 3.1] プロファイルからモデル一覧を取得（ルーム読込時の復元用）
    _room_profile_name = room_openai_settings.get("profile")
    _room_model_choices = []
    if _room_profile_name:
        _room_profile_settings_list = config_manager.get_openai_settings_list()
        _room_target_profile = next((s for s in _room_profile_settings_list if s["name"] == _room_profile_name), None)
        if _room_target_profile:
            _room_model_choices = _room_target_profile.get("available_models", [])
    # [Phase 3] 個別プロバイダ設定

    # null (None) の場合に "default" にフォールバックさせて UI の選択が消えるのを防ぐ
    # レガシーな値をサニタイズ (zhipu, groq, ollama, local -> openai)
    raw_provider = override_settings.get("provider") or "default"
    if raw_provider in ["zhipu", "groq", "ollama"]:
        raw_provider = "openai"
    elif raw_provider not in ["default", "google", "openai", "local", "anthropic"]:
        raw_provider = "default"

    return_provider = raw_provider

    roblox_settings = override_settings.get("roblox_settings", {})

    # [2026-03-17 FIX] OpenAIモデル名がGoogleドロップダウンに漏洩する問題を修正
    # OpenAI時はNoneにリセットし、Google/default時はeffective_settingsからの復元を許可
    if return_provider == "openai":
        _google_model_val = None
    elif return_provider == "google":
        _google_model_val = override_settings.get("model_name") or effective_settings.get("model_name")
    else:
        _google_model_val = override_settings.get("model_name")

    room_list_choices = room_manager.get_room_list_for_ui()
    t_step = _perf_log("_update_chat_tab_for_room_change: room_manager.get_room_list_for_ui", t_step)
    wm_slots = load_working_memory_slots(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: load_working_memory_slots", t_step)
    wm_content = load_working_memory_content(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: load_working_memory_content", t_step)
    system_prompt_content = load_system_prompt_content(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: load_system_prompt_content", t_step)
    style_update = _generate_style_from_settings(room_name, effective_settings)
    t_step = _perf_log("_update_chat_tab_for_room_change: _generate_style_from_settings", t_step)
    expressions_html = refresh_expressions_ui(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: refresh_expressions_ui", t_step)
    expression_choices = get_all_expression_choices(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: get_all_expression_choices", t_step)
    creative_dropdown_update = _get_safe_dropdown_update(room_name, 'creative', constants.CREATIVE_NOTES_FILENAME)
    t_step = _perf_log("_update_chat_tab_for_room_change: creative dropdown choices", t_step)
    research_dropdown_update = _get_safe_dropdown_update(room_name, 'research', constants.RESEARCH_NOTES_FILENAME)
    t_step = _perf_log("_update_chat_tab_for_room_change: research dropdown choices", t_step)
    temp_location_state = get_temp_location_ui_state(room_name)
    t_step = _perf_log("_update_chat_tab_for_room_change: get_temp_location_ui_state", t_step)
    _perf_log("_update_chat_tab_for_room_change: total", t0)

    tts_profile_val = effective_settings.get("tts_profile_name")
    if not tts_profile_val and profile_choices:
        tts_profile_val = profile_choices[0]

    return (
        room_name, chat_history, mapping_list,
        gr.update(interactive=True, placeholder="メッセージを入力してください (Shift+Enterで送信)。添付するにはファイルをドロップまたはクリップボタンを押してください..."),
        profile_image,
        identity_str, memory_str, notepad_content, creative_notes_content, research_notes_content,
        gr.update(choices=wm_slots[0], value=wm_slots[1]),
        wm_content, system_prompt_content,
        core_memory_content,
        # [Fix] 選択肢が空の場合にvalueを設定してエラーになるのを防ぐ
        gr.update(choices=room_list_choices, value=room_name if room_list_choices else None),
        gr.update(choices=room_list_choices, value=room_name if room_list_choices else None),
        gr.update(choices=room_list_choices, value=room_name if room_list_choices else None),
        gr.update(choices=room_list_choices, value=room_name if room_list_choices else None),
        gr.update(choices=locations_for_ui, value=location_dd_val), # choicesとvalueを同期して返す
        scenery_text,
        tts_provider_display,
        gr.update(choices=profile_choices, value=tts_profile_val, visible=(tts_provider_key == "openai_compatible")),
        gr.update(choices=_ensure_value_in_choices(room_model_choices, tts_model_val), value=tts_model_val),
        gr.update(choices=room_voice_choices, value=voice_display_name),
        voice_style_prompt_val,
        effective_settings.get("tts_voice_speed", 1.0),
        effective_settings.get("tts_voice_pitch", 0.0),
        effective_settings.get("tts_voice_intonation", 1.0),
        effective_settings.get("tts_voice_volume", 1.0),
        effective_settings["enable_typewriter_effect"],
        effective_settings["streaming_speed"],
        effective_settings.get("temperature", 0.8), effective_settings.get("top_p", 0.95),
        harassment_val, hate_val, sexual_val, dangerous_val,
        display_thoughts_val,
        gr.update(value=send_thoughts_val, interactive=send_thoughts_interactive),
        effective_settings.get("enable_auto_retrieval", True),
        effective_settings["add_timestamp"],
        effective_settings.get("send_current_time", False),
        effective_settings["send_notepad"], effective_settings["use_common_prompt"],
        effective_settings["send_core_memory"], effective_settings["send_scenery"],
        effective_settings.get("scenery_send_mode", "変更時のみ"),  # room_scenery_send_mode_dropdown
        effective_settings["auto_memory_enabled"],
        effective_settings.get("enable_self_awareness", True),  # room_enable_self_awareness_checkbox
        f"ℹ️ *現在選択中のルーム「{room_name}」にのみ適用される設定です。*",
        scenery_image_path,
        effective_settings.get("enable_scenery_system", True),
        gr.update(open=effective_settings.get("enable_scenery_system", True)),
        gr.update(value=limit_display), # room_api_history_limit_dropdown
        gr.update(value=constants.THINKING_LEVEL_OPTIONS.get(effective_settings.get("thinking_level", "auto"), "既定 (AIに任せる / 通常モデル)")),
        limit_key, # api_history_limit_state (電力表示用)
        gr.update(value=episode_display),
        gr.update(value=episodic_info_text),
        gr.update(value=auto_enabled),
        gr.update(value=auto_inactivity),
        gr.update(value=auto_settings.get("allow_schedule_tool", True)),
        gr.update(value=auto_settings.get("schedule_cooldown_minutes", 60)),
        gr.update(value=auto_settings.get("autonomous_guidelines", "")),
        gr.update(value=quiet_start),
        gr.update(value=quiet_end),
        gr.update(value=persona_workspace.get("permission_tier", "write")),
        gr.update(value=bool(agent_policy.get("enabled", False))),
        gr.update(value=agent_policy.get("permission_tier", "read")),
        gr.update(value=bool(agent_policy.get("allow_web_tools", False))),
        gr.update(value=bool(agent_policy.get("wake_on_completion", False))),
        gr.update(value=bool(agent_policy.get("wake_respect_quiet_hours", True))),
        *_delegation_model_updates(agent_policy, room_scope=True),
        gr.update(value=format_agent_delegation_backend_info(room_name)),
        gr.update(choices=list(config_manager.AVAILABLE_MODELS_GLOBAL), value=_google_model_val),  # room_model_dropdown (Dropdown)
        # [Phase 3] 個別プロバイダ設定
        # null (None) の場合に "default" にフォールバックさせて UI の選択が消えるのを防ぐ
        gr.update(value=return_provider),  # room_provider_radio
        gr.update(visible=(return_provider == "google")),  # room_google_settings_group
        gr.update(visible=(return_provider == "openai")),  # room_openai_settings_group
        gr.update(choices=config_manager.get_api_key_choices_for_ui(), value=room_api_key_ui_value),  # room_api_key_dropdown
        gr.update(choices=[s["name"] for s in config_manager.get_openai_settings_list()], value=room_openai_settings.get("profile") or None),  # room_openai_profile_dropdown
        gr.update(value=room_openai_settings.get("base_url") or ""),  # room_openai_base_url_input
        gr.update(value=room_openai_settings.get("api_key") or ""),  # room_openai_api_key_input
        gr.update(choices=_room_model_choices, value=room_openai_settings.get("model") or None),  # room_openai_model_dropdown
        gr.update(value=room_openai_settings.get("tool_use_enabled") if room_openai_settings.get("tool_use_enabled") is not None else True),  # room_openai_tool_use_checkbox
        gr.update(value=override_settings.get("enable_api_key_rotation")),  # room_rotation_dropdown [2026-02-11 FIX] or None を削除（Falseが消える）
        gr.update(value=roblox_settings.get("api_key", "")), # roblox_api_key_input
        gr.update(value=roblox_settings.get("universe_id", "")), # roblox_universe_id_input
        gr.update(value=roblox_settings.get("topic", "NexusArkCommands")), # roblox_topic_input
        gr.update(value=roblox_settings.get("webhook_enabled", True)), # roblox_webhook_enabled_checkbox
        gr.update(value=roblox_settings.get("activation_mode", "auto")), # roblox_activation_mode_radio
        gr.update(value=roblox_settings.get("webhook_domain", "")), # roblox_webhook_domain_input
        gr.update(value=override_settings.get("roblox_webhook_secret", "")), # roblox_webhook_secret_input
        gr.update(value=roblox_settings.get("filtering_enabled", True)), # roblox_filtering_enabled_checkbox (Step 14)
        # --- 睡眠時記憶整理 ---
        gr.update(value=sleep_episodic),
        gr.update(value=sleep_memory_index),
        gr.update(value=sleep_current_log),
        gr.update(value=sleep_entity),
        gr.update(value=sleep_compress),
        gr.update(value=sleep_extract_questions),
        gr.update(value=last_compression_result),
        # --- [v25] テーマ設定 ---
        gr.update(value=effective_settings.get("room_theme_enabled", False)),  # 個別テーマのオンオフ
        gr.update(value=effective_settings.get("chat_style", "Chat (Default)")),
        gr.update(value=effective_settings.get("font_size", 15)),
        gr.update(value=effective_settings.get("line_height", 1.6)),
        gr.update(value=effective_settings.get("theme_primary", None)),
        gr.update(value=effective_settings.get("theme_secondary", None)),
        gr.update(value=effective_settings.get("theme_background", None)),
        gr.update(value=effective_settings.get("theme_text", None)),
        gr.update(value=effective_settings.get("theme_accent_soft", None)),
        # --- 詳細設定 ---
        gr.update(value=effective_settings.get("theme_input_bg", None)),
        gr.update(value=effective_settings.get("theme_input_border", None)),
        gr.update(value=effective_settings.get("theme_code_bg", None)),
        gr.update(value=effective_settings.get("theme_subdued_text", None)),
        gr.update(value=effective_settings.get("theme_button_bg", None)),
        gr.update(value=effective_settings.get("theme_button_hover", None)),
        gr.update(value=effective_settings.get("theme_stop_button_bg", None)),
        gr.update(value=effective_settings.get("theme_stop_button_hover", None)),
        gr.update(value=effective_settings.get("theme_checkbox_off", None)),
        gr.update(value=effective_settings.get("theme_table_bg", None)),
        gr.update(value=effective_settings.get("theme_radio_label", None)),
        gr.update(value=effective_settings.get("theme_dropdown_list_bg", None)),
        gr.update(value=effective_settings.get("theme_ui_opacity", 0.9)),
        # 背景画像設定
        gr.update(value=effective_settings.get("theme_bg_image", None)),
        gr.update(value=effective_settings.get("theme_bg_opacity", 0.4)),
        gr.update(value=effective_settings.get("theme_bg_blur", 0)),
        gr.update(value=effective_settings.get("theme_bg_size", "cover")),
        gr.update(value=effective_settings.get("theme_bg_position", "center")),
        gr.update(value=effective_settings.get("theme_bg_repeat", "no-repeat")),
        gr.update(value=effective_settings.get("theme_bg_custom_width", "300px")),
        gr.update(value=effective_settings.get("theme_bg_radius", 0)),
        gr.update(value=effective_settings.get("theme_bg_mask_blur", 0)),
        gr.update(value=effective_settings.get("theme_bg_front_layer", False)),
        gr.update(value=effective_settings.get("theme_bg_src_mode", "画像を指定 (Manual)")),
        # Sync設定
        gr.update(value=effective_settings.get("theme_bg_sync_opacity", 0.4)),
        gr.update(value=effective_settings.get("theme_bg_sync_blur", 0)),
        gr.update(value=effective_settings.get("theme_bg_sync_size", "cover")),
        gr.update(value=effective_settings.get("theme_bg_sync_position", "center")),
        gr.update(value=effective_settings.get("theme_bg_sync_repeat", "no-repeat")),
        gr.update(value=effective_settings.get("theme_bg_sync_custom_width", "300px")),
        gr.update(value=effective_settings.get("theme_bg_sync_radius", 0)),
        gr.update(value=effective_settings.get("theme_bg_sync_mask_blur", 0)),
        gr.update(value=effective_settings.get("theme_bg_sync_front_layer", False)),

        # CSS注入
        gr.update(), # save_room_theme_button
        gr.update(value=style_update),
        # --- [Phase 11/12] 夢日記リセット対応 ---
        gr.update(), # dream_date_dropdown
        gr.update(value="日付を選択すると、ここに詳細が表示されます。"), # dream_detail_text
        gr.update(choices=["すべて"], value="すべて"), # dream_year_filter
        gr.update(choices=["すべて"], value="すべて"), # dream_month_filter
        # --- [Phase 14] エピソード記憶リセット対応 ---
        gr.update(), # episodic_date_dropdown
        gr.update(value="日付を選択してください"), # episodic_detail_text
        gr.update(choices=["すべて"], value="すべて"), # episodic_year_filter
        gr.update(choices=["すべて"], value="すべて"), # episodic_month_filter
        gr.update(value=last_episodic_update), # episodic_update_status
        gr.update(choices=entity_choices, value=None), # entity_dropdown
        gr.update(value=""), # entity_content_editor
        gr.update(value=config_manager.get_internal_model_settings().get("embedding_provider", "google")), # embedding_provider_radio (旧: embedding_mode_radio)
        gr.update(value=last_dream_time), # dream_status_display
        gr.update(value=effective_settings.get("auto_summary_enabled", False)), # room_auto_summary_checkbox
        gr.update(value=effective_settings.get("auto_summary_threshold", constants.AUTO_SUMMARY_DEFAULT_THRESHOLD), visible=effective_settings.get("auto_summary_enabled", False)), # room_auto_summary_threshold_slider
        gr.update(value=project_root), # room_project_root_input
        gr.update(value=project_exclude_dirs), # room_project_exclude_dirs_input
        gr.update(value=project_exclude_files), # room_project_exclude_files_input
        # --- [Avatar Expressions] ---
        gr.update(value=expressions_html), # expressions_html
        gr.update(choices=expression_choices, value=None), # expression_target_dropdown
        creative_dropdown_update, # creative_notes_file_dropdown
        research_dropdown_update, # research_notes_file_dropdown
        # --- [新規] 一時的現在地 UI 同期用 ---
        *temp_location_state # scenery, saved_locations, image_path, tabs (4要素)
    )


def handle_room_change_chat_fast(room_name: str, api_key_val: str, request: gr.Request = None):
    """
    ルーム切替直後にチャット欄だけを先に更新する軽量ハンドラ。
    詳細設定や管理タブの同期は後続の司令塔更新へ任せる。
    """
    session_id = _get_session_id(request)
    if session_id != "default":
        session_room = _get_session_init_room(session_id)
        if session_room and room_name and room_name != session_room:
            print(f"--- [Session:{session_id}] [room_change_fast] ルーム不整合を自己修正: {room_name} -> {session_room} ---")
            room_name = session_room

    if not room_name:
        room_list = room_manager.get_room_list_for_ui()
        room_name = room_list[0][1] if room_list else "Default"
    log_memory_diagnostics("room_change_fast:start", room_name)

    effective_settings = config_manager.get_effective_settings(room_name)
    limit_key = effective_settings.get("api_history_limit", "all")
    display_limit_key = _room_switch_display_history_limit(limit_key)
    add_timestamp_val = effective_settings.get("add_timestamp", False)
    display_thoughts_val = effective_settings.get("display_thoughts", True)

    chat_history, mapping_list = reload_chat_log(
        room_name=room_name,
        api_history_limit_value=display_limit_key,
        add_timestamp=add_timestamp_val,
        display_thoughts=display_thoughts_val,
        request=request
    )

    room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
    room_config = {}
    if os.path.exists(room_config_path):
        try:
            with open(room_config_path, "r", encoding="utf-8") as f:
                room_config = json.load(f)
        except Exception:
            room_config = {}

    override_settings = room_config.get("override_settings", {})
    room_provider_override = config_manager.normalize_room_provider_override(override_settings.get("provider"))
    room_draft_api_key_name = config_manager._clean_api_key_name(override_settings.get("api_key_name"))
    effective_api_key_name = room_draft_api_key_name if room_provider_override is not None else None
    if not effective_api_key_name:
        effective_api_key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name")
    if not effective_api_key_name and config_manager.GEMINI_API_KEYS:
        effective_api_key_name = list(config_manager.GEMINI_API_KEYS.keys())[0]

    api_key = config_manager.GEMINI_API_KEYS.get(effective_api_key_name)
    has_valid_key = bool(api_key and not api_key.startswith("YOUR_API_KEY"))
    chat_input_update = gr.update(
        interactive=has_valid_key,
        placeholder=(
            "メッセージを入力してください..."
            if has_valid_key
            else "まず、左の「設定」からAPIキーを設定してください。"
        )
    )

    result = (
        room_name,
        chat_history if has_valid_key else [],
        mapping_list if has_valid_key else [],
        chat_input_update,
        _hide_token_count_display(room_name),
        gr.update(value="設定と補助情報を読み込み中です...", visible=True)
    )
    log_memory_diagnostics(
        "room_change_fast:end",
        room_name,
        {"history": len(chat_history) if has_valid_key else 0, "mapping": len(mapping_list) if has_valid_key else 0}
    )
    return result


def _get_safe_dropdown_update(room_name: str, note_type: str, default_filename: str) -> gr.update:
    """ドロップダウンの選択肢に値が含まれているか確認し、安全な更新オブジェクトを返すヘルパー"""
    choices = room_manager.get_note_files(room_name, note_type)
    if default_filename in choices:
        return gr.update(choices=choices, value=default_filename)
    elif choices:
        return gr.update(choices=choices, value=choices[0]) # デフォルトがない場合は先頭
    else:
        # 選択肢がない場合はNoneにする（警告回避）
        return gr.update()


def handle_initial_load(room_name: str = None, expected_count: int = 229, request: gr.Request = None):
    """
    【v11: 時間デフォルト対応版】
    UIセッションが開始されるたびに、UIコンポーネントの初期状態を完全に再構築する、唯一の司令塔。
    """
    t0_overall = time.perf_counter()
    t_step = t0_overall
    session_id = _get_session_id(request)
    log_memory_diagnostics("initial_load:start", room_name)
    # セッション別初期化状態をリセット
    _session_init_states[session_id] = {"completed": False, "time": 0, "room": None}

    # 起動時の通知抑制: 初期化開始時にフラグをリセット（初期化完了後に通知を許可）
    global _initialization_completed
    _initialization_completed = False

    print(f"--- [Session:{session_id}] [UI Session Init] demo.load event triggered. Reloading all configs from file. ---")
    config_manager.load_config()
    config = config_manager.CONFIG_GLOBAL
    t_step = _perf_log("handle_initial_load: config_manager.load_config", t_step)

    # --- 1. 最新のルームとAPIキー情報を取得・計算 ---
    latest_room_list = room_manager.get_room_list_for_ui()
    t_step = _perf_log("handle_initial_load: room_manager.get_room_list_for_ui", t_step)
    folder_names = [folder for _, folder in latest_room_list]

    last_room_from_config = config.get("last_room", "Default")
    safe_initial_room = last_room_from_config
    if last_room_from_config not in folder_names:
        safe_initial_room = folder_names[0] if folder_names else "Default"

    # [2026-04-09] 早期に期待されるルーム名をセット（初期化中の割り込みガード用）
    _session_init_states[session_id]["room"] = safe_initial_room

    print(f"--- [Session:{session_id}] [UI Session Init] last_room='{last_room_from_config}' -> safe_initial_room='{safe_initial_room}' ---")

    latest_api_key_choices = config_manager.get_api_key_choices_for_ui()
    t_step = _perf_log("handle_initial_load: config_manager.get_api_key_choices_for_ui", t_step)
    valid_key_names = [key for _, key in latest_api_key_choices]
    last_api_key_from_config = config.get("last_api_key_name")
    safe_initial_api_key = last_api_key_from_config
    if last_api_key_from_config not in valid_key_names:
        safe_initial_api_key = valid_key_names[0]
    # ワーキングメモリの初期化 (v3)
    wm_slots_update, wm_content_update, wm_active_label = _get_working_memory_updates(safe_initial_room)
    t_step = _perf_log("handle_initial_load: _get_working_memory_updates", t_step)

    # --- 2. 司令塔として、他のハンドラのロジックを呼び出してUI更新値を生成 ---
    # `_update_chat_tab_for_room_change` は40個の値を返す
    chat_tab_updates = _update_chat_tab_for_room_change(safe_initial_room, safe_initial_api_key)
    t_step = _perf_log("handle_initial_load: _update_chat_tab_for_room_change", t_step)

    df_with_ids = render_alarms_as_dataframe()
    t_step = _perf_log("handle_initial_load: render_alarms_as_dataframe", t_step)
    display_df, feedback_text = get_display_df(df_with_ids), "アラームを選択してください"
    rules = config_manager.load_redaction_rules()
    t_step = _perf_log("handle_initial_load: config_manager.load_redaction_rules", t_step)
    rules_df_for_ui = _create_redaction_df_from_rules(rules)
    world_data_for_state = get_world_data(safe_initial_room)
    t_step = _perf_log("handle_initial_load: get_world_data", t_step)
    time_settings = _load_time_settings_for_room(safe_initial_room)
    t_step = _perf_log("handle_initial_load: _load_time_settings_for_room", t_step)
    time_settings_updates = (
        gr.update(value=time_settings.get("mode", "リアル連動")),
        gr.update(value=time_settings.get("fixed_season_ja", "秋")),
        gr.update(value=time_settings.get("fixed_time_of_day_ja", "夜")),
        gr.update(visible=(time_settings.get("mode", "リアル連動") == "選択する"))
    )

    # --- 3. オンボーディングと廃止済みトークン表示 ---
    has_valid_key = config_manager.has_valid_api_key()
    # 新しいモーダルオンボーディングを使用するため、古いガイドは常に非表示
    token_count_text, onboarding_guide_update, chat_input_update = (_hide_token_count_display(safe_initial_room), gr.update(visible=False), gr.update(interactive=False))

    # オンボーディングモーダルの表示制御: setup_completedがTrueまたはAPIキーが有効なら非表示
    import onboarding_manager
    is_setup_complete = config.get("setup_completed", False)
    onboarding_group_update = gr.update(visible=(not is_setup_complete and not has_valid_key))

    # 変数をデフォルト値で初期化（has_valid_keyに関係なく使用するため）
    locations_for_custom_scenery = _get_location_choices_for_ui(safe_initial_room)
    current_location_for_custom_scenery = utils.get_current_location(safe_initial_room)
    custom_scenery_dd_update = gr.update(choices=locations_for_custom_scenery, value=current_location_for_custom_scenery)
    t_step = _perf_log("handle_initial_load: custom scenery location state", t_step)

    current_season_ja, current_time_ja = _get_current_time_context_ui_values(safe_initial_room)
    custom_scenery_season_dd_update = gr.update(value=current_season_ja)
    custom_scenery_time_dd_update = gr.update(value=current_time_ja)

    if has_valid_key:
        t_step = _perf_log("handle_initial_load: token display disabled", t_step)
        onboarding_guide_update = gr.update(visible=False)
        chat_input_update = gr.update(interactive=True)
    else:
        t_step = _perf_log("handle_initial_load: token display disabled", t_step)

    # --- 4. [v9] その他の共通設定の初期値を決定 ---

    # 画像生成マルチプロバイダ設定を取得
    img_gen_provider = config.get("image_generation_provider", "gemini")
    img_gen_model = config.get("image_generation_model", "gemini-2.5-flash-image")
    available_gemini_img_models = config.get("available_image_models", {}).get("gemini", ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"])
    available_openai_img_models = config.get("available_image_models", {}).get("openai", ["gpt-image-1", "dall-e-3"])
    available_poll_img_models = config.get("available_image_models", {}).get("pollinations", ["flux", "zimage", "klein"])
    available_hf_img_models = config.get("available_image_models", {}).get("huggingface", ["black-forest-labs/FLUX.1-schnell"])
    openai_img_settings = config.get("image_generation_openai_settings", {})
    legacy_notification_service = config.get("notification_service", "discord")

    common_settings_updates = (
        gr.update(value=config.get("last_model", config_manager.DEFAULT_MODEL_GLOBAL)),
        gr.update(value=config.get("debug_mode", False)),
        gr.update(value=config.get("alarm_notification_service", legacy_notification_service).capitalize()),
        gr.update(value=config.get("user_notification_service", legacy_notification_service).capitalize()),
        gr.update(value=config.get("backup_rotation_count", 10)),
        gr.update(value=config.get("pushover_user_key", "")),
        gr.update(value=config.get("pushover_app_token", "")),
        gr.update(value=config.get("notification_webhook_url", "")),
        # 画像生成マルチプロバイダ対応(3コンポーネント)
        gr.update(value=img_gen_provider),  # image_gen_provider_radio
        # [v2.2]
        gr.update(choices=[("現在の選択キーを使用", "")] + latest_api_key_choices, value=config.get("image_generation_api_key_name", ""), visible=True),
        gr.update(choices=available_gemini_img_models, value=img_gen_model if img_gen_model in available_gemini_img_models else available_gemini_img_models[0]),  # gemini_image_model_dropdown
        gr.update(choices=available_openai_img_models, value=openai_img_settings.get("model", "gpt-image-1")),  # openai_image_model_dropdown
        # --- [追加] Pollinations / Hugging Face 画像生成設定 (4コンポーネント) ---
        gr.update(value=config.get("pollinations_api_key", "")),  # pollinations_api_key_input
        gr.update(choices=available_poll_img_models, value=config.get("image_generation_pollinations_model", "flux")),  # pollinations_image_model_dropdown
        gr.update(value=config.get("huggingface_api_token", "")),  # huggingface_api_token_input
        gr.update(choices=available_hf_img_models, value=config.get("image_generation_huggingface_model", "black-forest-labs/FLUX.1-schnell")),  # huggingface_image_model_dropdown
        gr.update(choices=[p[1] for p in latest_api_key_choices], value=config.get("paid_api_key_names", [])),
        gr.update(value=config.get("allow_external_connection", False)),  # [追加] 外部接続設定
    )

    current_openai_profile_name = config_manager.get_active_openai_profile_name()
    # アクティブな設定辞書を取得（なければ空辞書）
    openai_setting = config_manager.get_active_openai_setting() or {}
    available_models = openai_setting.get("available_models", [])
    default_model = openai_setting.get("default_model", "")

    openai_updates = (
        gr.update(value=current_openai_profile_name),            # openai_profile_dropdown
        gr.update(value=openai_setting.get("base_url", "")),     # openai_base_url_input
        gr.update(value=openai_setting.get("api_key", "")),      # openai_api_key_input
        gr.update(choices=available_models, value=default_model),# openai_model_dropdown
        gr.update(value=openai_setting.get("tool_use_enabled", True)) # room_openai_tool_use_checkbox
    )

    # 個別設定のOpenAI互換モデルドロップダウン用（visible=Falseグループ内のレンダリング問題回避）
    room_openai_model_dropdown_update = gr.update(choices=available_models, value=default_model)

    # --- 6. 索引の最終更新日時を取得 ---
    memory_index_last_updated = _get_rag_index_last_updated(safe_initial_room, "memory")
    current_log_index_last_updated = _get_rag_index_last_updated(safe_initial_room, "current_log")
    t_step = _perf_log("handle_initial_load: _get_rag_index_last_updated", t_step)

    # --- 7. [Phase 3] 内部モデル設定を取得 ---
    internal_model_settings = config_manager.get_internal_model_settings()
    t_step = _perf_log("handle_initial_load: config_manager.get_internal_model_settings", t_step)
    internal_model_updates = (
        gr.update(value=internal_model_settings.get("processing_provider_cat", "google")),
        gr.update(value=internal_model_settings.get("processing_model", constants.INTERNAL_PROCESSING_MODEL)),
        gr.update(value=internal_model_settings.get("summarization_provider_cat", "google")),
        gr.update(value=internal_model_settings.get("summarization_model", constants.SUMMARIZATION_MODEL)),
        gr.update(value=internal_model_settings.get("translation_provider_cat", "google")),
        gr.update(value=internal_model_settings.get("translation_model", constants.INTERNAL_PROCESSING_MODEL)),
        gr.update(value=internal_model_settings.get("embedding_model", constants.EMBEDDING_MODEL)),
        gr.update(value=internal_model_settings.get("fallback_enabled", True)),
    )

    # --- 8. 全ての戻り値を正しい順序で組み立てる ---
    # [v0.2.0-fix] 初期ロード時にRAGManagerをインスタンス化して、マイグレーションロジック（フォルダリネーム等）を走らせる
    if has_valid_key:
        try:
            # ここで get_rag_manager を呼ぶことで __init__ が走り、
            # faiss_index -> faiss_index_static のリネーム処理などが実行される
            get_rag_manager(safe_initial_room)
        except Exception as e:
            print(f"[Init] Failed to initialize RAGManager for {safe_initial_room}: {e}")
    t_step = _perf_log("handle_initial_load: get_rag_manager", t_step)

    # `initial_load_outputs`のリストに対応
    release_notes = get_release_notes()
    t_step = _perf_log("handle_initial_load: get_release_notes", t_step)
    final_outputs = (
        display_df, df_with_ids, feedback_text,
        *chat_tab_updates,
        rules_df_for_ui,
        token_count_text,
        gr.update(choices=latest_api_key_choices, value=safe_initial_api_key), # api_key_dropdown
        gr.update(choices=latest_api_key_choices, value=None), # gemini_delete_key_dropdown
        world_data_for_state,
        *time_settings_updates,
        onboarding_guide_update,
        onboarding_group_update,  # オンボーディングモーダルの表示制御
        *common_settings_updates,
        custom_scenery_dd_update,
        custom_scenery_season_dd_update,
        custom_scenery_time_dd_update,
        *openai_updates,
        f"最終更新: {memory_index_last_updated}",  # memory_reindex_status
        f"最終更新: {current_log_index_last_updated}",  # current_log_reindex_status
        *internal_model_updates,  # [Phase 3] 内部モデル設定 (6個)
        config_manager.GROQ_API_KEY or "", # [Phase 3b] groq_api_key_input
        config_manager.LOCAL_MODEL_PATH or "", # [Phase 3c] local_model_path_input
        config_manager.TAVILY_API_KEY or "", # [Phase 3] tavily_api_key_input
        config.get("enable_api_key_rotation", True), # settings_rotation_checkbox (再取得して渡す)
        gr.update(value=release_notes), # NEW: release_notes_markdown
        # [Added for working memory sync v3]
        wm_slots_update, # room_working_memory_slot_dropdown
        wm_content_update, # room_working_memory_content_editor
        wm_active_label # active_working_memory_status
    )

    # 初期化完了: 以降の設定変更では通知を表示する（ただし直後のgrace periodは除く）
    _initialization_completed = True
    global _initialization_completed_time
    _initialization_completed_time = time.time()

    # [2026-04-09] セッション別初期化完了を記録
    _session_init_states[session_id] = {
        "completed": True,
        "time": time.time(),
        "room": safe_initial_room
    }

    # [NEW] 起動時のログ自動バックアップとアクティブルーム設定
    room_manager.create_backup(safe_initial_room, 'log')
    t_step = _perf_log("handle_initial_load: room_manager.create_backup", t_step)
    room_manager.set_active_room_for_backup(safe_initial_room)
    t_step = _perf_log("handle_initial_load: room_manager.set_active_room_for_backup", t_step)

    final_outputs = _ensure_output_count(final_outputs, expected_count)
    _remember_programmatic_room_settings(safe_initial_room, final_outputs, _FULL_ROOM_SETTING_OUTPUT_MAP, offset=3)
    t_step = _perf_log("handle_initial_load: _ensure_output_count", t_step)
    _perf_log("handle_initial_load: total", t0_overall)
    log_memory_diagnostics("initial_load:end", safe_initial_room, {"outputs": len(final_outputs)})

    return final_outputs

def handle_initial_chat_load(room_name: str = None, request: gr.Request = None):
    """
    起動直後の体感速度を優先した軽量初期化。
    初回表示に必要なチャット・ルーム選択・情景だけを更新し、設定/管理タブの
    大量コンポーネント更新は各操作時の既存ハンドラへ委ねる。
    """
    t0_overall = time.perf_counter()
    t_step = t0_overall
    session_id = _get_session_id(request)
    log_memory_diagnostics("initial_chat_load:start", room_name)
    _session_init_states[session_id] = {"completed": False, "time": 0, "room": None}

    global _initialization_completed
    _initialization_completed = False

    print(f"--- [Session:{session_id}] [UI Fast Init] demo.load event triggered. Loading chat-critical state only. ---")
    config_manager.load_config()
    config = config_manager.CONFIG_GLOBAL
    t_step = _perf_log("handle_initial_chat_load: config_manager.load_config", t_step)

    latest_room_list = room_manager.get_room_list_for_ui()
    t_step = _perf_log("handle_initial_chat_load: room_manager.get_room_list_for_ui", t_step)
    folder_names = [folder for _, folder in latest_room_list]

    last_room_from_config = config.get("last_room", "Default")
    safe_initial_room = last_room_from_config if last_room_from_config in folder_names else (folder_names[0] if folder_names else "Default")
    _session_init_states[session_id]["room"] = safe_initial_room

    latest_api_key_choices = config_manager.get_api_key_choices_for_ui()
    t_step = _perf_log("handle_initial_chat_load: config_manager.get_api_key_choices_for_ui", t_step)
    valid_key_names = [key for _, key in latest_api_key_choices]
    last_api_key_from_config = config.get("last_api_key_name")
    safe_initial_api_key = last_api_key_from_config if last_api_key_from_config in valid_key_names else (valid_key_names[0] if valid_key_names else None)

    effective_settings = config_manager.get_effective_settings(safe_initial_room)
    current_global_model = config_manager.get_current_global_model()
    limit_key = effective_settings.get("api_history_limit", "all")
    add_timestamp_val = effective_settings.get("add_timestamp", False)
    display_thoughts_val = effective_settings.get("display_thoughts", True)
    t_step = _perf_log("handle_initial_chat_load: config_manager.get_effective_settings", t_step)

    initial_display_limit_key = limit_key
    if limit_key in ("today", "all", "全ログ"):
        initial_display_limit_key = "20"
    elif str(limit_key).isdigit() and int(limit_key) > 20:
        initial_display_limit_key = "20"

    chat_history, mapping_list = reload_chat_log(
        room_name=safe_initial_room,
        api_history_limit_value=initial_display_limit_key,
        add_timestamp=add_timestamp_val,
        display_thoughts=display_thoughts_val,
        request=request,
    )
    t_step = _perf_log("handle_initial_chat_load: reload_chat_log", t_step)

    has_valid_key = config_manager.has_valid_api_key()
    chat_input_update = gr.update(
        interactive=has_valid_key,
        placeholder=(
            "メッセージを入力してください (Shift+Enterで送信)。添付するにはファイルをドロップまたはクリップボタンを押してください..."
            if has_valid_key else
            "まず、左の「設定」からAPIキーを設定してください。"
        ),
    )
    token_count_text = _hide_token_count_display(safe_initial_room)
    onboarding_group_update = gr.update(visible=(not config.get("setup_completed", False) and not has_valid_key))
    onboarding_guide_update = gr.update(visible=False)

    profile_image = get_avatar_html(safe_initial_room, state="idle")
    t_step = _perf_log("handle_initial_chat_load: get_avatar_html", t_step)

    locations_for_ui = _get_location_choices_for_ui(safe_initial_room)
    current_location = utils.get_current_location(safe_initial_room)
    valid_location_ids = [value for _name, value in locations_for_ui if not str(value).startswith("__AREA_HEADER_")]
    if current_location not in valid_location_ids:
        current_location = valid_location_ids[0] if valid_location_ids else None
    location_update = gr.update(choices=locations_for_ui, value=current_location)
    t_step = _perf_log("handle_initial_chat_load: location choices", t_step)

    scenery_text, scenery_image_path = _get_updated_scenery_and_image(safe_initial_room, safe_initial_api_key)
    t_step = _perf_log("handle_initial_chat_load: _get_updated_scenery_and_image", t_step)

    style_update = _generate_style_from_settings(safe_initial_room, effective_settings)
    t_step = _perf_log("handle_initial_chat_load: _generate_style_from_settings", t_step)

    _initialization_completed = True
    global _initialization_completed_time
    _initialization_completed_time = time.time()
    _session_init_states[session_id] = {
        "completed": True,
        "time": time.time(),
        "room": safe_initial_room,
    }

    room_manager.create_backup(safe_initial_room, 'log')
    t_step = _perf_log("handle_initial_chat_load: room_manager.create_backup", t_step)
    room_manager.set_active_room_for_backup(safe_initial_room)
    t_step = _perf_log("handle_initial_chat_load: room_manager.set_active_room_for_backup", t_step)
    _perf_log("handle_initial_chat_load: total", t0_overall)

    wm_slots_update, wm_content_update, active_wm_label = _get_working_memory_updates(safe_initial_room)

    result = _ensure_output_count((
        safe_initial_room,
        chat_history,
        mapping_list,
        chat_input_update,
        profile_image,
        gr.update(choices=latest_room_list, value=safe_initial_room),
        location_update,
        scenery_text,
        gr.update(value=scenery_image_path) if scenery_image_path else gr.update(value=None),
        gr.update(value=style_update),
        token_count_text,
        gr.update(choices=latest_api_key_choices, value=safe_initial_api_key),
        safe_initial_api_key,
        limit_key,
        gr.update(choices=config_manager.AVAILABLE_MODELS_GLOBAL, value=current_global_model),
        current_global_model,
        gr.update(value=display_thoughts_val),
        gr.update(value=add_timestamp_val),
        onboarding_guide_update,
        onboarding_group_update,
        *_get_room_settings_fast_updates(safe_initial_room, safe_initial_api_key),
        get_release_notes(),
        active_wm_label,
        wm_slots_update,
        wm_content_update
    ), 99)
    _remember_programmatic_room_settings(safe_initial_room, result, _FAST_ROOM_SETTING_OUTPUT_MAP)
    log_memory_diagnostics(
        "initial_chat_load:end",
        safe_initial_room,
        {"history": len(chat_history), "mapping": len(mapping_list), "outputs": len(result)}
    )
    return result

def _get_room_settings_fast_updates(room_name: str, api_key_name: str = None):
    """Fast init用に、自動保存対象の個別設定コンポーネントだけを現在値へ同期する。"""
    effective_settings = config_manager.get_effective_settings(room_name)
    room_config = room_manager.get_room_config(room_name) or {}
    overrides = room_config.get("override_settings", {}) if isinstance(room_config, dict) else {}

    safety_label_map = {
        "BLOCK_NONE": "ブロックしない",
        "BLOCK_LOW_AND_ABOVE": "低リスク以上をブロック",
        "BLOCK_MEDIUM_AND_ABOVE": "中リスク以上をブロック",
        "BLOCK_ONLY_HIGH": "高リスクのみブロック",
    }

    provider = overrides.get("provider") or "default"
    tts_provider = config_manager.tts_provider_key_from_display(effective_settings.get("tts_provider", "gemini"))
    tts_model = effective_settings.get("tts_model") or (config_manager.get_tts_model_choices(tts_provider)[0] if config_manager.get_tts_model_choices(tts_provider) else "")
    voice_id = effective_settings.get("tts_voice", effective_settings.get("voice_id", "iapetus"))
    voice_name = config_manager.tts_voice_display_from_id(tts_provider, voice_id)
    history_key = effective_settings.get("api_history_limit", constants.DEFAULT_API_HISTORY_LIMIT_OPTION)
    history_display = constants.API_HISTORY_LIMIT_OPTIONS.get(history_key, constants.API_HISTORY_LIMIT_OPTIONS.get("all", "最大表示 (400件)"))
    thinking_key = effective_settings.get("thinking_level", constants.DEFAULT_THINKING_LEVEL)
    thinking_display = constants.THINKING_LEVEL_OPTIONS.get(thinking_key, constants.THINKING_LEVEL_OPTIONS[constants.DEFAULT_THINKING_LEVEL])
    episode_key = effective_settings.get("episode_memory_lookback_days", constants.DEFAULT_EPISODIC_MEMORY_DAYS)
    episode_display = constants.EPISODIC_MEMORY_OPTIONS.get(episode_key, constants.EPISODIC_MEMORY_OPTIONS[constants.DEFAULT_EPISODIC_MEMORY_DAYS])

    autonomous = effective_settings.get("autonomous_settings", {}) or {}
    agent_policy = effective_settings.get("agent_delegation_settings", {}) or {}
    persona_workspace = effective_settings.get("persona_workspace", {}) or {}
    sleep = effective_settings.get("sleep_consolidation", {}) or {}
    project = effective_settings.get("project_explorer", {}) or {}
    openai_settings = overrides.get("openai_settings") or effective_settings.get("openai_settings") or {}
    anthropic_settings = overrides.get("anthropic_settings") or effective_settings.get("anthropic_settings") or {}
    claude_subscription_settings = overrides.get("claude_subscription_settings") or effective_settings.get("claude_subscription_settings") or {}

    openai_profile = openai_settings.get("profile") or config_manager.get_active_openai_profile_name()
    openai_choices = []
    if openai_profile:
        openai_setting = config_manager.get_openai_setting_by_name(openai_profile)
        if openai_setting:
            openai_choices = openai_setting.get("available_models", [])
            if not openai_settings.get("model"):
                openai_settings = {**openai_settings, "model": openai_setting.get("default_model", "")}

    auto_summary_enabled = bool(effective_settings.get("auto_summary_enabled", False))
    rotation_value = overrides.get("enable_api_key_rotation", None)
    room_delegation_model_updates = _delegation_model_updates(agent_policy, room_scope=True)

    return (
        gr.update(value=config_manager.tts_provider_display_from_key(tts_provider)),
        gr.update(choices=config_manager.get_tts_model_choices(tts_provider), value=tts_model),
        gr.update(choices=config_manager.get_tts_voice_choices(tts_provider), value=voice_name),
        gr.update(value=effective_settings.get("tts_style_prompt", effective_settings.get("voice_style_prompt", ""))),
        gr.update(value=effective_settings.get("tts_voice_speed", 1.0)),
        gr.update(value=effective_settings.get("tts_voice_pitch", 0.0)),
        gr.update(value=effective_settings.get("tts_voice_intonation", 1.0)),
        gr.update(value=effective_settings.get("tts_voice_volume", 1.0)),
        gr.update(value=effective_settings.get("temperature", 1.0)),
        gr.update(value=effective_settings.get("top_p", 0.95)),
        gr.update(value=safety_label_map.get(effective_settings.get("safety_block_threshold_harassment"), "高リスクのみブロック")),
        gr.update(value=safety_label_map.get(effective_settings.get("safety_block_threshold_hate_speech"), "高リスクのみブロック")),
        gr.update(value=safety_label_map.get(effective_settings.get("safety_block_threshold_sexually_explicit"), "高リスクのみブロック")),
        gr.update(value=safety_label_map.get(effective_settings.get("safety_block_threshold_dangerous_content"), "高リスクのみブロック")),
        gr.update(value=effective_settings.get("enable_typewriter_effect", True)),
        gr.update(value=effective_settings.get("streaming_speed", constants.DEFAULT_STREAMING_SPEED)),
        gr.update(value=effective_settings.get("send_thoughts", True), interactive=bool(effective_settings.get("display_thoughts", True))),
        gr.update(value=effective_settings.get("enable_auto_retrieval", False)),
        gr.update(value=effective_settings.get("send_current_time", True)),
        gr.update(value=effective_settings.get("send_notepad", True)),
        gr.update(value=effective_settings.get("use_common_prompt", True)),
        gr.update(value=effective_settings.get("send_core_memory", True)),
        gr.update(value=effective_settings.get("send_scenery", True)),
        gr.update(value=effective_settings.get("scenery_send_mode", "変更時のみ")),
        gr.update(value=effective_settings.get("enable_scenery_system", False)),
        gr.update(value=effective_settings.get("auto_memory_enabled", False)),
        gr.update(value=effective_settings.get("enable_self_awareness", True)),
        gr.update(value=history_display),
        gr.update(value=thinking_display),
        gr.update(value=episode_display),
        gr.update(value=autonomous.get("enabled", False)),
        gr.update(value=autonomous.get("inactivity_minutes", constants.MIN_AUTONOMOUS_INTERVAL_MINUTES)),
        gr.update(value=autonomous.get("allow_schedule_tool", True)),
        gr.update(value=autonomous.get("schedule_cooldown_minutes", constants.DEFAULT_SCHEDULE_COOLDOWN_MINUTES)),
        gr.update(value=autonomous.get("autonomous_guidelines", "")),
        gr.update(value=autonomous.get("quiet_hours_start", "00:00")),
        gr.update(value=autonomous.get("quiet_hours_end", "07:00")),
        gr.update(value=persona_workspace.get("permission_tier", "write")),
        gr.update(value=bool(agent_policy.get("enabled", False))),
        gr.update(value=agent_policy.get("permission_tier", "read")),
        gr.update(value=bool(agent_policy.get("allow_web_tools", False))),
        gr.update(value=bool(agent_policy.get("wake_on_completion", False))),
        gr.update(value=bool(agent_policy.get("wake_respect_quiet_hours", True))),
        *room_delegation_model_updates,
        gr.update(value=format_agent_delegation_backend_info(room_name)),
        gr.update(choices=config_manager.AVAILABLE_MODELS_GLOBAL, value=overrides.get("model_name") or effective_settings.get("model_name")),
        gr.update(value=provider),
        gr.update(visible=(provider == "google")),
        gr.update(visible=(provider == "openai")),
        gr.update(visible=(provider == "anthropic")),
        gr.update(visible=False),
        gr.update(visible=(provider == "local")),
        gr.update(visible=False),
        gr.update(choices=config_manager.get_api_key_choices_for_ui(), value=overrides.get("api_key_name") or api_key_name),
        gr.update(value=openai_profile),
        gr.update(value=openai_settings.get("base_url", "")),
        gr.update(value=openai_settings.get("api_key", "")),
        gr.update(choices=openai_choices, value=openai_settings.get("model", "")),
        gr.update(value=openai_settings.get("tool_use_enabled", True)),
        gr.update(value=anthropic_settings.get("model", "")),
        gr.update(value=claude_subscription_settings.get("model", "")),
        gr.update(value=rotation_value),
        gr.update(value=sleep.get("update_episodic_memory", True)),
        gr.update(value=sleep.get("update_memory_index", True)),
        gr.update(value=sleep.get("update_current_log_index", False)),
        gr.update(value=sleep.get("update_entity_memory", True)),
        gr.update(value=sleep.get("compress_old_episodes", False)),
        gr.update(value=sleep.get("extract_open_questions", True)),
        gr.update(value=auto_summary_enabled),
        gr.update(value=effective_settings.get("auto_summary_threshold", constants.AUTO_SUMMARY_DEFAULT_THRESHOLD), visible=auto_summary_enabled),
        gr.update(value=project.get("root_path", "")),
        gr.update(value=", ".join(project.get("exclude_dirs", []))),
        gr.update(value=", ".join(project.get("exclude_files", []))),
    )

def handle_initial_scenery_image_load(room_name: str, api_key_name: str = None):
    """Fast init後に、既存の現在地画像だけを軽量に読み込む。"""
    try:
        if not room_name:
            return gr.update(value=None)

        effective_settings = config_manager.get_effective_settings(room_name)
        if not effective_settings.get("enable_scenery_system", True):
            return gr.update(value=None)

        current_location = utils.get_current_location(room_name)
        if not current_location:
            return gr.update(value=None)

        season_en, time_of_day_en = utils._get_current_time_context(room_name)
        image_path = utils.find_scenery_image(room_name, current_location, season_en, time_of_day_en)
        return gr.update(value=_load_image_for_gradio(image_path)) if image_path else gr.update(value=None)
    except Exception as e:
        print(f"  - [Init] 情景画像の遅延読み込みに失敗: {e}")
        return gr.update(value=None)

def handle_save_room_settings(
    room_name: str, voice_name: str, voice_style_prompt: str,
    temp: float, top_p: float, harassment: str, hate: str, sexual: str, dangerous: str,
    enable_typewriter_effect: bool,
    streaming_speed: float,
    display_thoughts: bool,
    send_thoughts: bool,
    enable_auto_retrieval: bool,
    add_timestamp: bool,
    send_current_time: bool,
    send_notepad: bool,
    use_common_prompt: bool, send_core_memory: bool,
    send_scenery: bool,
    scenery_send_mode: str,  # 情景画像送信タイミング: 「変更時のみ」 or 「毎ターン」
    enable_scenery_system: bool,
    auto_memory_enabled: bool,
    enable_self_awareness: bool,
    api_history_limit: str,
    thinking_level: str,
    episode_memory_days: str,
    enable_autonomous: bool,
    autonomous_inactivity: float,
    allow_schedule_tool: bool,
    schedule_cooldown_minutes: float,
    autonomous_guidelines: str,
    quiet_hours_start: str,
    quiet_hours_end: str,
    model_name: str = None,  # [追加] ルーム個別モデル設定
    # [Phase 3] 個別プロバイダ設定
    provider: str = "default",
    api_key_name: str = None,
    openai_profile: str = None,  # 追加: プロファイル選択
    openai_base_url: str = None,
    openai_api_key: str = None,
    openai_model: str = None,
    openai_tool_use: bool = True,  # 追加: ツール使用オンオフ
    anthropic_model: str = None,   # [追加] Anthropic個別モデル設定
    enable_api_key_rotation: Any = None, # [Phase 1.5] 個別ロテ
    # --- 睡眠時記憶整理 ---
    sleep_update_episodic: bool = True,
    sleep_update_memory_index: bool = True,
    sleep_update_current_log: bool = False,
    sleep_update_entity: bool = True,
    sleep_update_compress: bool = False,
    sleep_extract_questions: bool = True,  # NEW: 未解決の問い抽出
    auto_summary_enabled: bool = False,
    auto_summary_threshold: int = constants.AUTO_SUMMARY_DEFAULT_THRESHOLD,
    project_root: str = "",
    project_exclude_dirs: str = "",
    project_exclude_files: str = "",
    roblox_filtering_enabled: bool = True,  # Step 14: Robloxフィルタリング設定
    is_switching_room: bool = False,
    silent: bool = False,
    force_notify: bool = False
):
    # 【DEBUG】引数の受け渡しが正しいかログを出力
    # print(f"--- [DEBUG] handle_save_room_settings: room={room_name}, auto_enabled={enable_autonomous}, interval={autonomous_inactivity} ---")

    # 初期化中またはルーム切り替え中は保存処理を完全にスキップする（無駄な I/O と通知を防ぐ）
    if (
        not _initialization_completed
        or is_switching_room
        or (time.time() - _initialization_completed_time) < POST_INIT_GRACE_PERIOD_SECONDS
    ):
        return

    if not room_name: gr.Warning("設定を保存するルームが選択されていません。"); return

    safety_value_map = {
        "ブロックしない": "BLOCK_NONE",
        "低リスク以上をブロック": "BLOCK_LOW_AND_ABOVE",
        "中リスク以上をブロック": "BLOCK_MEDIUM_AND_ABOVE",
        "高リスクのみブロック": "BLOCK_ONLY_HIGH"
    }

    display_thoughts = bool(display_thoughts)
    send_thoughts = bool(send_thoughts)

    if not display_thoughts: send_thoughts = False

    # 定数マップを使ってUIの表示名("最新 10件")を内部キー("10")に変換。
    # 空値・未知値はリロード中の未同期値として扱い、保存済み設定を上書きしない。
    history_limit_key = _api_history_limit_key_from_ui(api_history_limit)

    episode_days_key = next((k for k, v in constants.EPISODIC_MEMORY_OPTIONS.items() if v == episode_memory_days), constants.DEFAULT_EPISODIC_MEMORY_DAYS)
    thinking_level_key = next((k for k, v in constants.THINKING_LEVEL_OPTIONS.items() if v == thinking_level), "auto")

    new_settings = {
        # ルーム個別モデル設定: 「共通設定に従う」の場合はNullにリセット
        # [2026-03-17 FIX] provider=google時、OpenAI互換モデル名が漏洩するのを防止
        # AVAILABLE_MODELS_GLOBAL に含まれないモデル名は保存しない
        "model_name": None if provider == "default" else (
            model_name if (model_name and (provider != "google" or model_name in config_manager.AVAILABLE_MODELS_GLOBAL or not config_manager.AVAILABLE_MODELS_GLOBAL))
            else None
        ),
        "voice_id": next((k for k, v in config_manager.SUPPORTED_VOICES.items() if v == voice_name), None),
        "voice_style_prompt": voice_style_prompt.strip() if voice_style_prompt else "",
        "temperature": temp,
        "top_p": top_p,
        "safety_block_threshold_harassment": safety_value_map.get(harassment),
        "safety_block_threshold_hate_speech": safety_value_map.get(hate),
        "safety_block_threshold_sexually_explicit": safety_value_map.get(sexual),
        "safety_block_threshold_dangerous_content": safety_value_map.get(dangerous),
        "enable_typewriter_effect": bool(enable_typewriter_effect),
        "streaming_speed": float(streaming_speed),
        "display_thoughts": bool(display_thoughts),
        "send_thoughts": send_thoughts,
        "enable_auto_retrieval": bool(enable_auto_retrieval),
        "add_timestamp": bool(add_timestamp),
        "send_current_time": bool(send_current_time),
        "send_notepad": bool(send_notepad),
        "use_common_prompt": bool(use_common_prompt),
        "send_core_memory": bool(send_core_memory),
        "send_scenery": bool(send_scenery),
        "scenery_send_mode": scenery_send_mode if scenery_send_mode in ["変更時のみ", "毎ターン"] else "変更時のみ",
        "enable_scenery_system": bool(enable_scenery_system),
        "auto_memory_enabled": bool(auto_memory_enabled),
        "enable_self_awareness": bool(enable_self_awareness),
        "api_history_limit": history_limit_key,
        "thinking_level": thinking_level_key,
        "episode_memory_lookback_days": episode_days_key,
        "autonomous_settings": {
            "enabled": bool(enable_autonomous),
            "inactivity_minutes": int(autonomous_inactivity),
            "allow_schedule_tool": bool(allow_schedule_tool),
            "schedule_cooldown_minutes": int(schedule_cooldown_minutes),
            "autonomous_guidelines": autonomous_guidelines.strip() if autonomous_guidelines else "",
            "quiet_hours_start": quiet_hours_start,
            "quiet_hours_end": quiet_hours_end
        },
        # [Phase 3] 個別プロバイダ設定
        "provider": provider if provider != "default" else None,
        "api_key_name": config_manager._clean_api_key_name(api_key_name) if (api_key_name and provider != "default") else None,
        "openai_settings": {
            "profile": openai_profile if openai_profile else None,
            "base_url": openai_base_url if openai_base_url else "",
            "api_key": openai_api_key if openai_api_key else "",
            "model": openai_model if openai_model else "",
            "tool_use_enabled": bool(openai_tool_use)
        } if provider == "openai" else None,
        "anthropic_settings": {
            "model": anthropic_model if anthropic_model else ""
        } if provider == "anthropic" else None,
        # [Phase 1.5] ローテーション設定
        # 「共通設定に従う」= None の場合、override_settings から削除して共通設定にフォールバックさせる
        "enable_api_key_rotation": enable_api_key_rotation if (enable_api_key_rotation is not None and enable_api_key_rotation != "None") else "REMOVE_ME",
        # --- 睡眠時記憶整理 ---
        "sleep_consolidation": {
            "update_episodic_memory": bool(sleep_update_episodic),
            "update_memory_index": bool(sleep_update_memory_index),
            "update_current_log_index": bool(sleep_update_current_log),
            "update_entity_memory": bool(sleep_update_entity),
            "compress_old_episodes": bool(sleep_update_compress),
            "extract_open_questions": bool(sleep_extract_questions)  # NEW
        },
        "auto_summary_enabled": bool(auto_summary_enabled),
        "auto_summary_threshold": int(auto_summary_threshold),
        "project_explorer": {
            "root_path": (project_root or "").strip(),
            "exclude_dirs": [d.strip() for d in (project_exclude_dirs or "").split(",") if d.strip()],
            "exclude_files": [f.strip() for f in (project_exclude_files or "").split(",") if f.strip()]
        },
        "roblox_filtering_enabled": bool(roblox_filtering_enabled) # Step 14
    }
    if history_limit_key is None:
        new_settings.pop("api_history_limit", None)

    # 「共通設定に従う」が選択された場合のみ、キーごと削除して共通設定にフォールバックさせる
    new_settings = {k: v for k, v in new_settings.items() if v != "REMOVE_ME"}

    result = room_manager.update_room_config(room_name, new_settings)
    if not silent:
        if result == True or (result == "no_change" and force_notify):
            now = time.time()

            # 1. 初期化完了前、または初期化完了直後のgrace period中は通知を抑制
            if not _initialization_completed or (now - _initialization_completed_time) < POST_INIT_GRACE_PERIOD_SECONDS:
                 pass

            # 2. [New] ルーム切り替え直後の「余震」による通知を抑制
            elif not force_notify and (now - _last_room_switch_time) < ROOM_SWITCH_GRACE_PERIOD_SECONDS:
                pass

            else:
                # 3. デバウンス: 同一ルームへの連続通知を抑制
                last_time = _last_save_notification_time.get(room_name, 0)
                if force_notify or (now - last_time) > NOTIFICATION_DEBOUNCE_SECONDS:
                    print(f"--- [UI] 「{room_name}」の個別設定を保存しました。 ---")
                    # 手動保存(force_notify=True)の場合は必ず通知
                    # 自動保存でもデバウンス＆Grace Period通過なら通知
                    gr.Info(f"設定を保存しました: {room_name}")
                    _last_save_notification_time[room_name] = now
    if result == False:
        gr.Error("個別設定の保存中にエラーが発生しました。詳細はログを確認してください。")

def handle_context_settings_change(
    room_name: str, api_key_name: str, api_history_limit: str,
    lookback_days: str,
    display_thoughts: bool,
    send_thoughts: bool,
    enable_auto_retrieval: bool,
    add_timestamp: bool, send_current_time: bool,
    send_notepad: bool, use_common_prompt: bool, send_core_memory: bool,
    enable_scenery_system: bool,
    auto_memory_enabled: bool,
    auto_summary_enabled: bool,
    enable_self_awareness: bool,
    auto_summary_threshold: int,
    *args, **kwargs
):
    """推定再計算は行わず、直近の実送信トークン数だけを返す互換ハンドラ。"""
    return _hide_token_count_display(room_name)

def toggle_chat_mask(is_masked: bool, current_history: list, saved_history: list) -> Tuple[bool, list, list, str]:
    """
    チャットのマスク状態を切り替える。
    配信時などにチャット履歴を隠すための機能。

    Args:
        is_masked: 現在のマスク状態 (Trueならマスク中 -> 解除する)
        current_history: 現在表示されているチャット履歴
        saved_history: マスク前に退避したチャット履歴

    Returns:
        (new_is_masked, new_history, new_saved_history, new_button_label)
    """
    if not is_masked:
        # マスク有効化処理
        print("--- [ChatMask] Masking chat history ---")
        # 現在の履歴を保存
        new_saved = current_history
        # ダミー履歴を設定
        dummy_history = [
            {"role": "user", "content": "ユーザー:\nチャット欄マスク中"},
            {"role": "assistant", "content": "ペルソナ:\nチャット欄マスク中"},
        ]
        return True, dummy_history, new_saved, "会話を表示"
    else:
        # マスク解除処理
        print("--- [ChatMask] Unmasking chat history ---")
        # 保存していた履歴を復元
        # もし保存履歴がなければ（初期状態など）、現在のダミーをクリアして空にするか、そのままにする
        restored_history = saved_history if saved_history is not None else []
        return False, restored_history, [], "会話を隠す"

def _chatbot_message(role: str, content: Any) -> Dict[str, Any]:
    return {"role": role, "content": content}

def _chatbot_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    if isinstance(message, (list, tuple)) and len(message) >= 2:
        return message[1] if message[1] is not None else message[0]
    return None

def _is_assistant_status_message(message: Any) -> bool:
    if isinstance(message, dict):
        return message.get("role") == "assistant"
    return isinstance(message, (list, tuple)) and len(message) >= 2 and message[0] is None

def _replace_last_chatbot_message(history: list, role: str, content: Any) -> None:
    if history:
        history[-1] = _chatbot_message(role, content)

def _chatbot_event_message_index(index: Any) -> Optional[int]:
    if isinstance(index, (list, tuple)):
        return index[0] if index else None
    return index if isinstance(index, int) else None

def update_token_count_on_input(
    room_name: str,
    api_key_name: str,
    api_history_limit: str,
    lookback_days: str,
    multimodal_input: dict,
    display_thoughts: bool,
    send_thoughts: bool,
    enable_auto_retrieval: bool,
    add_timestamp: bool,
    send_current_time: bool,
    send_notepad: bool,
    use_common_prompt: bool, send_core_memory: bool, send_scenery: bool,
    auto_memory_enabled: bool,
    auto_summary_enabled: bool,
    enable_self_awareness: bool,
    auto_summary_threshold: int,
    *args, **kwargs
):
    """推定再計算は行わず、直近の実送信トークン数だけを返す互換ハンドラ。"""
    return _hide_token_count_display(room_name)


def _clamp_group_supervisor_rounds(value: Any) -> int:
    try:
        rounds = int(float(value))
    except (TypeError, ValueError):
        rounds = 1
    return max(1, min(rounds, GROUP_SUPERVISOR_MAX_ROUNDS_HARD_LIMIT))


def _extract_group_speaker_json(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    cleaned = re.sub(r"\[THOUGHT\].*?\[/THOUGHT\]", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"<details[\s\S]*?</details>", "", cleaned, flags=re.DOTALL)
    match = re.search(r"\{.*?\}", cleaned, flags=re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _mentioned_group_speakers(text: str, candidates: List[str]) -> List[str]:
    if not text:
        return []
    return [name for name in candidates if name and name in text]


def _build_group_speaker_aliases(candidates: List[str]) -> Dict[str, List[str]]:
    aliases: Dict[str, List[str]] = {}
    for candidate in candidates:
        values = [candidate]
        config_path = os.path.join(constants.ROOMS_DIR, candidate, "room_config.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                room_config = json.load(f)
            for key in ("room_name", "agent_display_name"):
                value = str(room_config.get(key) or "").strip()
                if value:
                    values.append(value)
        except Exception:
            pass
        aliases[candidate] = list(dict.fromkeys(values))
    return aliases


def _canonicalize_group_speaker_name(name: str, aliases: Dict[str, List[str]]) -> str:
    normalized = str(name or "").strip()
    for canonical, names in aliases.items():
        if normalized == canonical or normalized in names:
            return canonical
    return normalized


def _mentioned_group_speakers_by_alias(text: str, aliases: Dict[str, List[str]]) -> List[str]:
    if not text:
        return []
    mentioned = []
    for canonical, names in aliases.items():
        if any(alias and alias in text for alias in names):
            mentioned.append(canonical)
    return mentioned


def _choose_group_speaker_fallback(
    candidates: List[str],
    speaker_counts: Dict[str, int],
    last_speaker: Optional[str],
    recent_focus_text: str,
    max_rounds: int,
    mention_source_speaker: Optional[str] = None,
    aliases: Optional[Dict[str, List[str]]] = None,
) -> Optional[str]:
    aliases = aliases or {name: [name] for name in candidates}
    eligible = [name for name in candidates if speaker_counts.get(name, 0) < max_rounds]
    if not eligible:
        return None

    mentioned = [
        name for name in _mentioned_group_speakers_by_alias(recent_focus_text, aliases)
        if name in eligible
        if name != mention_source_speaker
    ]
    if mentioned:
        mentioned.sort(key=lambda name: (speaker_counts.get(name, 0), candidates.index(name)))
        return mentioned[0]

    non_repeat = [name for name in eligible if name != last_speaker] or eligible
    non_repeat.sort(key=lambda name: (speaker_counts.get(name, 0), candidates.index(name)))
    return non_repeat[0]


def _validate_group_speaker_decision(
    decision: Dict[str, Any],
    candidates: List[str],
    speaker_counts: Dict[str, int],
    last_speaker: Optional[str],
    recent_focus_text: str,
    max_rounds: int,
    turn_index: int,
    mention_source_speaker: Optional[str] = None,
    aliases: Optional[Dict[str, List[str]]] = None,
) -> Optional[str]:
    aliases = aliases or {name: [name] for name in candidates}
    mention_source_speaker = _canonicalize_group_speaker_name(mention_source_speaker or "", aliases) or None
    eligible = [name for name in candidates if speaker_counts.get(name, 0) < max_rounds]
    if not eligible:
        return None

    next_speaker = _canonicalize_group_speaker_name(str(decision.get("next_speaker") or "").strip(), aliases)
    should_continue = decision.get("continue", True)
    if isinstance(should_continue, str):
        should_continue = should_continue.strip().lower() not in {"false", "no", "0", "finish", "stop"}

    if not should_continue or next_speaker.upper() == "FINISH":
        return _choose_group_speaker_fallback(
            candidates,
            speaker_counts,
            last_speaker,
            recent_focus_text,
            max_rounds,
            mention_source_speaker,
            aliases,
        )

    if next_speaker not in eligible:
        return _choose_group_speaker_fallback(
            candidates,
            speaker_counts,
            last_speaker,
            recent_focus_text,
            max_rounds,
            mention_source_speaker,
            aliases,
        )

    mentioned = [
        name for name in _mentioned_group_speakers_by_alias(recent_focus_text, aliases)
        if name != mention_source_speaker
    ]
    if mentioned and next_speaker not in mentioned:
        return _choose_group_speaker_fallback(
            candidates,
            speaker_counts,
            last_speaker,
            recent_focus_text,
            max_rounds,
            mention_source_speaker,
            aliases,
        )
    if last_speaker == next_speaker and len(eligible) > 1 and next_speaker not in mentioned:
        return _choose_group_speaker_fallback(
            candidates,
            speaker_counts,
            last_speaker,
            recent_focus_text,
            max_rounds,
            mention_source_speaker,
            aliases,
        )

    return next_speaker


def _format_recent_group_log_for_selector(messages: List[Dict[str, Any]], limit: int = 10) -> Tuple[str, str, Optional[str]]:
    recent = messages[-limit:] if messages else []
    lines = []
    recent_focus_text = ""
    mention_source_speaker: Optional[str] = None
    for msg in recent:
        role = msg.get("role", "")
        name = msg.get("responder") or role
        body = utils.clean_persona_text(utils.remove_ai_timestamp(msg.get("content", ""))).strip()
        body = re.sub(r"\s+", " ", body)
        if len(body) > 500:
            body = body[:499] + "..."
        lines.append(f"{role}:{name}: {body}")
        if body:
            recent_focus_text = body
            mention_source_speaker = None if role == "USER" else name
    return "\n".join(lines), recent_focus_text, mention_source_speaker


def _format_group_turn_context(turn_entries: List[Dict[str, str]]) -> str:
    lines = []
    for entry in turn_entries:
        speaker = str(entry.get("speaker") or "").strip()
        text = utils.clean_persona_text(utils.remove_ai_timestamp(str(entry.get("text") or ""))).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        if speaker and text:
            lines.append(f"{speaker}: {text}")
    if not lines:
        return ""
    return "【今回のグループ会話の共有ログ】\n" + "\n\n".join(lines)


def _select_group_speaker_with_director(
    soul_vessel_room: str,
    candidates: List[str],
    speaker_counts: Dict[str, int],
    last_speaker: Optional[str],
    max_rounds: int,
    turn_index: int,
    debug_mode: bool = False,
) -> Optional[str]:
    log_f, _, _, _, _, _, _ = get_room_files_paths(soul_vessel_room)
    recent_messages = []
    if log_f:
        try:
            log_dir = os.path.dirname(log_f)
            room_dir = os.path.dirname(log_dir) if os.path.basename(log_dir) == "logs" else log_dir
            recent_messages, _ = utils.load_chat_log_lazy(room_dir, limit=30, min_turns=10)
        except Exception:
            recent_messages = utils.load_chat_log(log_f)[-30:]

    recent_log_text, recent_focus_text, mention_source_speaker = _format_recent_group_log_for_selector(recent_messages)
    aliases = _build_group_speaker_aliases(candidates)
    eligible = [name for name in candidates if speaker_counts.get(name, 0) < max_rounds]
    if not eligible:
        return None

    prompt = f"""
あなたはNexus Arkのグループ会話における「司会AI」です。
あなた自身は会話に参加せず、発言本文も絶対に作りません。
次に発言権を渡すペルソナを1名だけ選んでください。
全候補者が規定回数に達した後だけ、FINISHを選んでください。

制約:
- 出力はJSON 1個のみ。
- next_speaker は候補者名または FINISH のみ。
- 会話文、挨拶、相槌、キャラクターの発言は禁止。
- 全候補者が規定回数に達するまでは、必ず候補者から1名を選ぶ。
- 名指しされている候補者を最優先。
- 話題に最も関連する候補者を優先。
- ただし発言回数が偏りすぎないよう、発言が少ない候補者を優先。
- 同じ候補者の連続発言は、名指しや補足が必要な場合以外は避ける。
- 各候補者は原則 {max_rounds} 回まで発言する。未達の候補者がいる間はFINISH禁止。

候補者: {candidates}
候補者の表示名/別名: {aliases}
発言可能な候補者: {eligible}
直前の発言者: {last_speaker or "なし"}
今回の発言回数: {speaker_counts}

最近の会話:
{recent_log_text or "（会話履歴なし）"}

JSON形式:
{{"next_speaker":"候補者名またはFINISH","continue":true,"reason":"短い理由","confidence":0.0}}
""".strip()

    decision: Dict[str, Any] = {}
    try:
        from llm_factory import LLMFactory
        director_llm = LLMFactory.create_chat_model(
            temperature=0.0,
            internal_role="supervisor",
        )
        response = director_llm.invoke(prompt)
        content = utils.get_content_as_string(response).strip()
        if debug_mode:
            print(f"--- [Group Supervisor] raw: {content[:300]} ---")
        decision = _extract_group_speaker_json(content)
    except Exception as e:
        print(f"--- [Group Supervisor] 話者選択に失敗。フォールバックします: {e} ---")

    selected = _validate_group_speaker_decision(
        decision,
        candidates,
        speaker_counts,
        last_speaker,
        recent_focus_text,
        max_rounds,
        turn_index,
        mention_source_speaker,
        aliases,
    )
    if debug_mode:
        print(f"--- [Group Supervisor] selected={selected}, counts={speaker_counts} ---")
    return selected


def _stream_and_handle_response(
    room_to_respond: str,
    full_user_log_entry: str,
    user_prompt_parts_for_api: List[Dict],
    api_key_name: str,
    global_model: str,
    api_history_limit: str,
    debug_mode: bool,
    soul_vessel_room: str,
    active_participants: List[str],
    group_hide_thoughts: bool,  # グループ会話 思考ログ非表示
    active_attachments: List[str],
    current_console_content: str,
    enable_typewriter_effect: bool,
    streaming_speed: float,
    scenery_text_from_ui: str,
    screenshot_mode: bool,
    redaction_rules: list,
    enable_supervisor: bool = False, # Supervisor機能の有効/無効
    group_supervisor_rounds: int = 1,
    # [v22] 翻訳不整合対策
    translation_cache: dict = None
) -> Iterator[Tuple]:
    import time
    perf_start = time.time()
    print(f"--- [PERF] _stream_and_handle_response start (room={room_to_respond}) ---")
    log_memory_diagnostics(
        "chat_stream:start",
        soul_vessel_room,
        {
            "respond_room": room_to_respond,
            "participants": len(active_participants or []),
            "attachments": len(active_attachments or []),
        }
    )
    """
    【v15: グループ会話・逐次表示FIX】
    AIへのリクエスト送信、ストリーミング、APIリトライ、そしてグループ会話のターン管理の全責務を担う。
    一人応答するごとにログを保存・UIを再描画し、各AIの思考コンテキストの完全な独立性を保証する。
    """
    from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable, InternalServerError
    import openai

    main_log_f, _, _, _, _, _, _ = get_room_files_paths(soul_vessel_room)
    all_turn_popups = []
    final_error_message = None
    last_ai_message = None
    last_ai_timestamp_str = None

    # リトライ時に副作用のあるツールが再実行されるのを防ぐためのフラグ
    tool_execution_successful_this_turn = False

    # タイプライターエフェクトが正常完了したかのフラグ
    typewriter_completed_successfully = False
    # [v21] GeneratorExit後はyieldをスキップするためのフラグ
    generator_exited = False

    # Arousal記録とログ保存を同期させるための変数
    last_ai_timestamp_str = None

    # [v20] 動画アバター対応: thinking状態のアバターHTMLを生成
    # 動画がない場合は静止画にフォールバックし、CSSアニメーションで表現
    current_profile_update = gr.update(value=get_avatar_html(soul_vessel_room, state="thinking"))


    try:
        # --- [Arousal] 会話開始時の内部状態スナップショット ---
        # エピソード記憶の重要度（Arousal）計算のため、会話前後の内部状態変化を記録
        internal_state_before = None
        try:
            from motivation_manager import MotivationManager
            mm = MotivationManager(soul_vessel_room)
            internal_state_before = mm.get_state_snapshot()
        except Exception as e:
            print(f"  - [Arousal] スナップショット取得失敗: {e}")
        # --- Arousalここまで ---

        # UIをストリーミングモードに移行
        # この時点の履歴を一度取得
        effective_settings = config_manager.get_effective_settings(soul_vessel_room) # <<< "initial"を削除
        add_timestamp = effective_settings.get("add_timestamp", False) # <<< "initial"を削除
        display_thoughts = effective_settings.get("display_thoughts", True) # <<< "initial"を削除 & この行で定義
        # グループ会話で思考ログ非表示が有効な場合、強制的にオフ
        if group_hide_thoughts:
            display_thoughts = False
        chatbot_history, mapping_list = reload_chat_log(
            room_name=soul_vessel_room,
            api_history_limit_value=api_history_limit,
            add_timestamp=add_timestamp, # <<< "initial"を削除
            display_thoughts=display_thoughts, # <<< "initial"を削除
            screenshot_mode=screenshot_mode,
            redaction_rules=redaction_rules
        )
        print(f"--- [PERF] initial reload_chat_log took: {time.time() - perf_start:.4f}s ---")

        # [Phase 7] システム通知の取得と反映
        system_notices = utils.consume_system_notices()
        for notice in system_notices:
            notice_msg = f"⚠️ **システム警告**: {notice['message']}"
            chatbot_history.append(_chatbot_message("assistant", notice_msg))
            # ログにも保存
            utils.save_message_to_log(main_log_f, "## SYSTEM:Nexus Ark", notice_msg)

        chatbot_history.append(_chatbot_message("assistant", "▌"))
        yield (chatbot_history, mapping_list, gr.update(value={'text': '', 'files': []}),
               gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(visible=True, interactive=True),
               gr.update(interactive=False),
               gr.update(visible=False),
               current_profile_update,  # [v19] profile_image_display
               gr.update(), # [v21] style_injector (16番目)
               translation_cache # [v22] 17番目
        )

        # AIごとの応答生成ループ
        all_rooms_in_scene = [soul_vessel_room] + (active_participants or [])
        is_group_conversation = len(all_rooms_in_scene) > 1
        group_turn_entries = [{"speaker": "USER", "text": full_user_log_entry}] if is_group_conversation else []
        group_supervisor_enabled = bool(enable_supervisor and active_participants)
        group_supervisor_rounds = _clamp_group_supervisor_rounds(group_supervisor_rounds)
        if group_supervisor_enabled:
            max_group_turns = len(all_rooms_in_scene) * group_supervisor_rounds
            response_turn_slots = [None] * max_group_turns
            speaker_counts = {name: 0 for name in all_rooms_in_scene}
            last_group_speaker = None
            print(f"  - [Group Supervisor] 司会モード有効。最大 {group_supervisor_rounds} 巡 / {max_group_turns} 発言まで。")
        else:
            response_turn_slots = list(all_rooms_in_scene)
            speaker_counts = {}
            last_group_speaker = None

        for i, scheduled_room in enumerate(response_turn_slots):
            if group_supervisor_enabled:
                current_room = _select_group_speaker_with_director(
                    soul_vessel_room=soul_vessel_room,
                    candidates=all_rooms_in_scene,
                    speaker_counts=speaker_counts,
                    last_speaker=last_group_speaker,
                    max_rounds=group_supervisor_rounds,
                    turn_index=i,
                    debug_mode=debug_mode,
                )
                if not current_room:
                    print("  - [Group Supervisor] FINISH。ユーザーのターンへ戻します。")
                    break
                speaker_counts[current_room] = speaker_counts.get(current_room, 0) + 1
                last_group_speaker = current_room
            else:
                current_room = scheduled_room

            # --- [最重要] ターンごとに思考の前提をゼロから構築 ---
            is_first_responder = (i == 0)

            # UIに思考中であることを表示
            # 新しい生成開始時にストップフラグをクリア
            _stop_generation_event.clear()
            reload_start = time.time()
            chatbot_history, mapping_list = reload_chat_log(
                soul_vessel_room, api_history_limit, add_timestamp, display_thoughts,
                screenshot_mode, redaction_rules
            )
            print(f"--- [PERF] turn {i} reload_chat_log took: {time.time() - reload_start:.4f}s ---")
            chatbot_history.append(_chatbot_message("assistant", f"思考中 ({current_room})... ▌"))
            yield (chatbot_history, mapping_list, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), translation_cache)  # [v22] 17要素

            # APIに渡す引数を、現在のAI（current_room）のために完全に再構築
            season_en, time_of_day_en = utils._get_current_time_context(soul_vessel_room) # utilsから呼び出
            shared_location_name = utils.get_current_location(soul_vessel_room)
            group_turn_context = _format_group_turn_context(group_turn_entries) if is_group_conversation else ""
            current_user_prompt_parts = [group_turn_context] if group_turn_context else (user_prompt_parts_for_api if is_first_responder else [])

            agent_args_dict = {
                "room_to_respond": current_room,
                "api_key_name": api_key_name,
                "global_model_from_ui": global_model,
                "api_history_limit": api_history_limit,
                "debug_mode": debug_mode,
                "history_log_path": main_log_f,
                "user_prompt_parts": current_user_prompt_parts,
                "force_user_prompt_parts": bool(group_turn_context),
                "soul_vessel_room": soul_vessel_room,
                "active_participants": active_participants,
                "shared_location_name": shared_location_name,
                "active_attachments": active_attachments,
                "shared_scenery_text": scenery_text_from_ui,
                "season_en": season_en,
                "time_of_day_en": time_of_day_en,
                "skip_tool_execution": tool_execution_successful_this_turn,
                "enable_supervisor": False
            }

            streamed_text = ""
            final_state = None
            initial_message_count = 0
            max_retries = 5
            base_delay = 5

            for attempt in range(max_retries):
                try:
                    agent_args_dict = {
                        "room_to_respond": current_room,
                        "api_key_name": api_key_name,
                        "global_model_from_ui": global_model,
                        "api_history_limit": api_history_limit,
                        "debug_mode": debug_mode,
                        "history_log_path": main_log_f,
                        "user_prompt_parts": current_user_prompt_parts,
                        "force_user_prompt_parts": bool(group_turn_context),
                        "soul_vessel_room": soul_vessel_room,
                        "active_participants": active_participants,
                        "shared_location_name": shared_location_name,
                        "active_attachments": active_attachments,
                        "shared_scenery_text": scenery_text_from_ui,
                        "season_en": season_en,
                        "time_of_day_en": time_of_day_en,
                        "skip_tool_execution": tool_execution_successful_this_turn,
                        "enable_supervisor": False
                    }

                    # デバッグモードがONの場合のみ、標準出力をキャプチャする
                    # 【重要】model_nameはストリームの途中で取得できた値を保持する
                    # LangGraphの最終stateでは後続ノードによりmodel_nameが欠落する可能性があるため
                    captured_model_name = None
                    heartbeat_count = 0

                    if debug_mode:
                        with utils.capture_prints() as captured_output:
                            for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args_dict):
                                # ストップフラグチェック: ストップボタンが押されたらループを中断
                                if _stop_generation_event.is_set():
                                    print("--- [STOP] ストップフラグ検出、ストリーミングを中断します ---")
                                    break
                                if mode == "initial_count":
                                    initial_message_count = chunk
                                elif mode == "heartbeat":
                                    heartbeat_count += 1
                                    dots = "." * ((heartbeat_count % 3) + 1)
                                    # 最後のメッセージ（"思考中..."等）を更新してアニメーションさせる
                                    if chatbot_history and _is_assistant_status_message(chatbot_history[-1]):
                                        base_msg = _chatbot_content(chatbot_history[-1])
                                        # 既存の "思考中... ▌" などを取り除く簡易的な処理
                                        if "思考中" in base_msg:
                                            new_msg = f"思考中 ({current_room}) {dots} ▌"
                                            _replace_last_chatbot_message(chatbot_history, "assistant", new_msg)
                                            yield (chatbot_history, mapping_list, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), translation_cache)
                                elif mode == "messages":
                                    msgs = chunk if isinstance(chunk, list) else [chunk]
                                    for msg in msgs:
                                        if isinstance(msg, AIMessage):
                                            sig = msg.additional_kwargs.get("__gemini_function_call_thought_signatures__")
                                            if not sig: sig = msg.additional_kwargs.get("thought_signature")
                                            t_calls = msg.tool_calls if hasattr(msg, "tool_calls") else []
                                            if sig or t_calls:
                                                signature_manager.save_turn_context(current_room, sig, t_calls)
                                elif mode == "values":
                                    final_state = chunk
                                    if chunk.get("model_name"):
                                        captured_model_name = chunk.get("model_name")
                        current_console_content += captured_output.getvalue()
                    else:
                        for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args_dict):
                            # ストップフラグチェック: ストップボタンが押されたらループを中断
                            if _stop_generation_event.is_set():
                                print("--- [STOP] ストップフラグ検出、ストリーミングを中断します ---")
                                break
                            if mode == "initial_count":
                                initial_message_count = chunk
                            elif mode == "heartbeat":
                                heartbeat_count += 1
                                dots = "." * ((heartbeat_count % 3) + 1)
                                # 最後のメッセージ（"思考中..."等）を更新してアニメーションさせる
                                if chatbot_history and _is_assistant_status_message(chatbot_history[-1]):
                                    base_msg = _chatbot_content(chatbot_history[-1])
                                    if "思考中" in base_msg:
                                        new_msg = f"思考中 ({current_room}) {dots} ▌"
                                        _replace_last_chatbot_message(chatbot_history, "assistant", new_msg)
                                        yield (chatbot_history, mapping_list, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), translation_cache)
                            elif mode == "messages":
                                msgs = chunk if isinstance(chunk, list) else [chunk]
                                for msg in msgs:
                                    if isinstance(msg, AIMessage):
                                        sig = msg.additional_kwargs.get("__gemini_function_call_thought_signatures__")
                                        if not sig: sig = msg.additional_kwargs.get("thought_signature")
                                        t_calls = msg.tool_calls if hasattr(msg, "tool_calls") else []

                                        # 【重要】ツールコールが空の場合は、既存の保存済みツールコールを消さないように保護
                                        # 二幕構成の二幕目（最終回答）では通常ツールコールは空になるため。
                                        if sig or t_calls:
                                            # signature_manager 側でマージ/保護されるべきだが
                                            # ここでも最小限のチェックを行う
                                            signature_manager.save_turn_context(current_room, sig, t_calls)

                            elif mode == "values":
                                final_state = chunk
                                if chunk.get("model_name"):
                                    captured_model_name = chunk.get("model_name")

                    break # 成功したのでリトライループを抜ける

                except (ResourceExhausted, ServiceUnavailable, InternalServerError, openai.RateLimitError, openai.APIError) as e:
                    error_str = str(e)
                    # 1日の上限エラーか判定 (Google用)
                    if "PerDay" in error_str or "Daily" in error_str:
                        final_error_message = "[エラー] APIの1日あたりの利用上限に達したため、本日の応答はこれ以上生成できません。"
                        break

                    # 待機時間の計算
                    wait_time = base_delay * (2 ** attempt)
                    match = re.search(r"retry_delay {\s*seconds: (\d+)\s*}", error_str)
                    if match:
                        wait_time = int(match.group(1)) + 1

                    # OpenAIのRateLimitErrorの場合、ヘッダーから情報を取れる場合があるが、
                    # 簡略化のため指数バックオフを適用する

                    if attempt < max_retries - 1:
                        retry_message = (f"⏳ APIの応答が遅延しています(Rate Limit等)。{wait_time}秒待機して再試行します... ({attempt + 1}/{max_retries}回目)\n詳細: {e}")
                        # reload_chat_logを呼び出して最新の履歴を取得
                        chatbot_history, mapping_list = reload_chat_log(
                            soul_vessel_room, api_history_limit, add_timestamp, display_thoughts,
                            screenshot_mode, redaction_rules
                        )
                        chatbot_history.append(_chatbot_message("assistant", retry_message))
                        yield (chatbot_history, mapping_list, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), translation_cache)  # [v22] 17要素
                        time.sleep(wait_time)
                    else:
                        final_error_message = f"[エラー] APIのレート制限が頻発しています。時間をおいて再試行してください。"
                        break
                except RuntimeError as e:
                    # 【マルチモデル対応】ツール非対応エラーなど、agent/graph.pyから送られる
                    # ユーザーフレンドリーなエラーメッセージをシステムエラーとして処理
                    print(f"--- エージェントからシステムエラーが送信されました ---")
                    final_error_message = str(e)
                    break
                except Exception as e:
                    print(f"--- エージェント実行中に予期せぬエラーが発生しました ---")
                    traceback.print_exc()
                    final_error_message = f"[エラー] 内部処理で問題が発生しました。詳細はターミナルを確認してください。"
                    break

            if final_state:
                # [安定化] ストリーム完了後に、全てのメッセージをまとめて処理する
                raw_new_messages = final_state["messages"][initial_message_count:]

                # --- 【Gemini Pro重複対策: 最長メッセージ採用ロジック】 ---
                # 1ターンの中でAIから複数のテキストメッセージが返ってきた場合、
                # それらは「思考の断片」と「完成形」の重複である可能性が高い。
                # ツール呼び出し(ToolMessage)は全て維持しつつ、
                # AIMessage（テキスト）については「最も長いもの1つだけ」を採用する。
                new_messages = _build_new_messages_from_final_state(raw_new_messages, current_room, final_state)

                # -----------------------------------

                # 変数をここで初期化（UnboundLocalError対策）
                last_ai_message = None

                # ログ記録とリトライガード設定
                for msg in new_messages:
                    if isinstance(msg, (AIMessage, ToolMessage)):
                        content_to_log = ""
                        header = ""

                        if isinstance(msg, AIMessage):
                            content_str = utils.get_content_as_string(msg)
                            if content_str and content_str.strip():
                                # AI応答にもタイムスタンプ・モデル名を追加（ユーザー発言と同じ形式）
                                # 【修正】AIが模倣したタイムスタンプを除去してから、正しいモデル名でタイムスタンプを追加
                                content_str = utils.remove_ai_timestamp(content_str)

                                # --- [Phase F] ペルソナ感情タグのパースと除去 ---
                                # ペルソナが出力した <persona_emotion category="xxx" intensity="0.0-1.0"/> をパースして
                                # MotivationManagerに反映し、ログからは除去する
                                persona_emotion_pattern = r'<persona_emotion\s+category=["\'](\w+)["\']\s+intensity=["\']([0-9.]+)["\']\s*/>'
                                emotion_match = re.search(persona_emotion_pattern, content_str, re.IGNORECASE)
                                if emotion_match:
                                    detected_category = emotion_match.group(1).lower()
                                    detected_intensity = float(emotion_match.group(2))
                                    valid_categories = ["joy", "contentment", "protective", "anxious", "sadness", "anger", "neutral"]
                                    if detected_category in valid_categories:
                                        try:
                                            from motivation_manager import MotivationManager
                                            mm = MotivationManager(current_room)
                                            mm.set_persona_emotion(detected_category, detected_intensity)
                                            mm._save_state()
                                            print(f"  - [Emotion] ペルソナ感情を反映: {detected_category} (強度: {detected_intensity})")
                                        except Exception as e:
                                            print(f"  - [Emotion] 感情反映エラー: {e}")
                                    else:
                                        print(f"  - [Emotion] 無効なカテゴリ: {detected_category}")
                                    # [修正] ログにはメタデータを保持するため、ここでの除去は廃止
                                    # content_str = re.sub(persona_emotion_pattern, '', content_str, flags=re.IGNORECASE).rstrip()
                                # --- 感情タグ処理ここまで ---

                                # --- [Phase H] 記憶共鳴タグのパースとArousal更新 ---
                                # ペルソナが出力した <memory_trace id="xxx" resonance="0.0-1.0"/> をパースして
                                # EpisodicMemoryManagerでArousalを更新し、ログからは除去する
                                memory_trace_pattern = r'<memory_trace\s+id=["\']([^"\']+)["\']\s+resonance=["\']([0-9.]+)["\']\s*/>'
                                trace_matches = re.findall(memory_trace_pattern, content_str, re.IGNORECASE)
                                if trace_matches:
                                    try:
                                        from episodic_memory_manager import EpisodicMemoryManager
                                        emm = EpisodicMemoryManager(current_room)
                                        for episode_id, resonance_str in trace_matches:
                                            resonance = float(resonance_str)
                                            if 0.0 <= resonance <= 1.0:
                                                emm.update_arousal(episode_id, resonance)
                                            else:
                                                print(f"  - [MemoryTrace] 無効な共鳴度: {resonance_str}")
                                        print(f"  - [MemoryTrace] {len(trace_matches)}件の記憶共鳴を処理")
                                    except Exception as e:
                                        print(f"  - [MemoryTrace] 共鳴処理エラー: {e}")
                                    # [修正] ログにはメタデータを保持するため、ここでの除去は廃止
                                    # content_str = re.sub(memory_trace_pattern, '', content_str, flags=re.IGNORECASE).rstrip()
                                # --- 記憶共鳴タグ処理ここまで ---

                                # --- [Phase H+] エンティティ記憶トレースのパースと使用更新 ---
                                entity_trace_pattern = r'<entity_memory_trace\s+(?:name|id)=["\']([^"\']+)["\']\s+resonance=["\']([0-9.]+)["\']\s*/>'
                                entity_trace_matches = re.findall(entity_trace_pattern, content_str, re.IGNORECASE)
                                if entity_trace_matches:
                                    try:
                                        from entity_memory_manager import EntityMemoryManager
                                        em = EntityMemoryManager(current_room)
                                        processed = 0
                                        for entity_name, resonance_str in entity_trace_matches:
                                            resonance = float(resonance_str)
                                            if 0.0 <= resonance <= 1.0:
                                                if em.update_usage_from_trace(entity_name, resonance):
                                                    processed += 1
                                            else:
                                                print(f"  - [EntityTrace] 無効な共鳴度: {resonance_str}")
                                        print(f"  - [EntityTrace] {processed}件のエンティティ使用を反映")
                                    except Exception as e:
                                        print(f"  - [EntityTrace] 使用反映エラー: {e}")
                                    # content_str = re.sub(entity_trace_pattern, '', content_str, flags=re.IGNORECASE).rstrip()
                                # --- エンティティ記憶トレース処理ここまで ---

                                # 使用モデル名の取得（優先順位: 1.ローカルプロバイダ判定, 2.ストリーム中に取得したmodel_name, 3.final_state, 4.effective_settings）
                                if config_manager.get_active_provider(current_room) == "local":
                                    # ローカルプロバイダの場合は固定名を表示し、Geminiの古いデフォルト設定に引きずられるのを防ぐ
                                    actual_model_name = "Local (GGUF)"
                                else:
                                    actual_model_name = captured_model_name or (final_state.get("model_name") if final_state else None)
                                    if not actual_model_name:
                                        effective_settings = config_manager.get_effective_settings(current_room, global_model_from_ui=global_model)
                                        actual_model_name = effective_settings.get("model_name", global_model)

                                # システムの正しいタイムスタンプを追加
                                now_obj = datetime.datetime.now()
                                timestamp_str = now_obj.strftime('%H:%M:%S')
                                timestamp = f"\n\n{now_obj.strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)}"
                                content_to_log = content_str + timestamp

                                if isinstance(msg, AIMessage):
                                    last_ai_timestamp_str = timestamp_str

                                # (System): プレフィックスのチェックと処理
                                if content_to_log.startswith("(System):"):
                                    header = "## SYSTEM:Nexus Ark"
                                    # プレフィックスを削除（タイムスタンプは維持）
                                    content_to_log = content_to_log[len("(System):"):].strip()
                                else:
                                    header = f"## AGENT:{current_room}"
                                    if is_group_conversation:
                                        group_turn_entries.append({
                                            "speaker": current_room,
                                            "text": content_str,
                                        })

                        elif isinstance(msg, ToolMessage):
                            # 【アナウンスのみ保存するツール】constants.pyで一元管理
                            # 生の検索結果（大量の会話ログ）はログに保存せず、
                            # 「ツールを使用しました」というアナウンスだけを保存する。
                            if msg.name in constants.TOOLS_SAVE_ANNOUNCEMENT_ONLY:
                                formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                                # 生の結果（[RAW_RESULT]）は含めない。アナウンスのみ。
                                content_to_log = formatted_tool_result if formatted_tool_result is not None else ""
                                header = f"## SYSTEM:tool_result:{msg.name}:{msg.tool_call_id}"
                                if content_to_log:
                                    print(f"--- [ログ最適化] '{msg.name}' のアナウンスのみ保存（生の結果は除外） ---")
                                else:
                                    print(f"--- [ログ最適化] '{msg.name}' のアナウンスおよび生の結果の保存をスキップ ---")
                            else:
                                formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                                if formatted_tool_result is not None:
                                    content_to_log = f"{formatted_tool_result}\n\n[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]"
                                else:
                                    content_to_log = f"[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]"
                                # ツール名とコールIDをヘッダーに埋め込む
                                header = f"## SYSTEM:tool_result:{msg.name}:{msg.tool_call_id}"

                        side_effect_tools = ["plan_main_memory_edit", "plan_secret_diary_edit", "plan_notepad_edit", "plan_creative_notes_edit", "plan_research_notes_edit", "update_working_memory", "switch_working_memory", "plan_world_edit", "set_personal_alarm", "set_timer", "set_pomodoro_timer"]
                        if isinstance(msg, ToolMessage) and msg.name in side_effect_tools and "Error" not in str(msg.content) and "エラー" not in str(msg.content):
                            tool_execution_successful_this_turn = True
                            print(f"--- [リトライガード設定] 副作用のあるツール '{msg.name}' の成功を記録しました。 ---")

                        if header and content_to_log:
                            for participant_room in all_rooms_in_scene:
                                log_f, _, _, _, _, _, _ = get_room_files_paths(participant_room)
                                if log_f:
                                    # --- 【修正】二重書き込み防止チェック ---
                                    # [2026-02-14 FIX] 全件読み込みを回避し Lazy Loading (limit=10) で直近のみ確認
                                    try:
                                        # log_f から room_dir を逆算
                                        l_dir = os.path.dirname(log_f)
                                        r_dir = os.path.dirname(l_dir) if os.path.basename(l_dir) == "logs" else l_dir

                                        current_log, _ = utils.load_chat_log_lazy(r_dir, limit=10, min_turns=1)
                                        if current_log:
                                            last_entry = current_log[-1]
                                            if _is_redundant_log_update(last_entry.get('content', ''), content_to_log):
                                                print(f"--- [Deduplication] Skipping redundant message for {participant_room} (Suffix/Exact match) ---")
                                                continue
                                    except Exception as e:
                                        print(f"Deduplication check warning: {e}")
                                        # Lazy Load失敗時は安全のため全件読み込みで再試行（またはスキップ）
                                        # ここではパフォーマンス優先で、失敗したらチェック自体をスキップして書き込む（二重書き込みリスクよりフリーズ回避を優先）
                                    # ---------------------------------------
                                    utils.save_message_to_log(log_f, header, content_to_log)

                # 表示処理
                # ログが更新された可能性があるので、UI表示の直前に必ず再読み込みする
                chatbot_history, mapping_list = reload_chat_log(soul_vessel_room, api_history_limit, add_timestamp, display_thoughts, screenshot_mode, redaction_rules)

                last_ai_message = None

                # このターンでAIが生成した最後の発言のみをストリーミング表示の対象とする
                for msg in reversed(new_messages):
                    if isinstance(msg, AIMessage):
                        content_str = utils.get_content_as_string(msg)
                        if content_str and content_str.strip():
                            last_ai_message = msg
                            break

                text_to_display = utils.get_content_as_string(last_ai_message) if last_ai_message else ""

                if text_to_display:
                    # 【修正v2】二重表示防止ロジック（Gemini 2.5 Pro対応）
                    if enable_typewriter_effect and streaming_speed > 0:
                        # タイプライターONの場合:
                        # reload_chat_logで取得したフォーマット済みの最後のメッセージを保存し、
                        # それを文字ずつ表示する（生テキストではなくフォーマット済みを使用）
                        formatted_last_message = None
                        if chatbot_history:
                            # 最後のメッセージを取り出す（後で文字ずつ表示）
                            formatted_last_message = chatbot_history.pop()

                        # フォーマット済みテキストを取得（AI応答なので[1]がテキスト）
                        formatted_text = _chatbot_content(formatted_last_message) or ""

                        if formatted_text:
                            # アニメーション用のカーソルを追加して開始
                            chatbot_history.append(_chatbot_message("assistant", "▌"))
                            streamed_text = ""  # ★重要: 毎回初期化

                            # --- [v29] 思考ログの一括表示対応 ---
                            # <details> タグで囲まれた思考ログ部分と、それ以外の通常テキストを分離する
                            # 正規表現のグループ化により、デリミタ自体も保持する
                            parts = re.split(r'(<details class="thought-details"[\s\S]*?</details>)', formatted_text)

                            for part in parts:
                                if not part:
                                    continue

                                if part.startswith('<details class="thought-details"'):
                                    # 思考ログ部分は一括で追加し、ウェイトを置かない
                                    streamed_text += part
                                    _replace_last_chatbot_message(chatbot_history, "assistant", streamed_text + "▌")
                                    yield (chatbot_history, mapping_list, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), translation_cache)
                                else:
                                    # 通常テキストは1文字ずつタイピング表示
                                    for char in part:
                                        streamed_text += char
                                        _replace_last_chatbot_message(chatbot_history, "assistant", streamed_text + "▌")
                                        yield (chatbot_history, mapping_list, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), translation_cache)
                                        time.sleep(streaming_speed)
                            # -----------------------------------

                            # タイプライター完了後、フォーマット済みの最終形を表示
                            # （生テキストではなく、reload_chat_logから取得したフォーマット済みを使用）
                            chatbot_history[-1] = formatted_last_message
                            yield (chatbot_history, mapping_list, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), translation_cache)  # [v22] 17要素

                        typewriter_completed_successfully = True

                    else:
                        # タイプライターOFFの場合:
                        # 何もしない。直前の reload_chat_log で既に完了形のメッセージが表示されているため、
                        # ここで append すると二重になってしまう。
                        pass

                # 【重要】タイプライター完了後のreloadは、finallyブロックに任せる。
                # これにより、エラー時やキャンセル時も正しくログから読み込まれる。

        if final_error_message:
            # エラーメッセージを、AIの応答ではなく「システムエラー」として全員のログに記録する
            error_header = "## SYSTEM:システムエラー"
            for room_name in all_rooms_in_scene:
                log_f, _, _, _, _, _, _ = get_room_files_paths(room_name)
                if log_f:
                    utils.save_message_to_log(log_f, error_header, final_error_message)
            # この時点ではUIに直接書き込まず、finallyブロックのreload_chat_logに表示を任せる

    except GeneratorExit:
        print("--- [ジェネレータ] ユーザーの操作により、ストリーミング処理が正常に中断されました。 ---")
        generator_exited = True  # [v21] フラグをセット

    finally:
        # [v21] GeneratorExit後はyieldできないためスキップ
        if generator_exited:
            return

        # 処理完了・中断・エラーに関わらず、最終的なUI状態を確定する
        effective_settings = config_manager.get_effective_settings(soul_vessel_room)
        add_timestamp = effective_settings.get("add_timestamp", False)
        display_thoughts = effective_settings.get("display_thoughts", True)
        if group_hide_thoughts:
            display_thoughts = False

        # [クールダウンリセット] 通常会話完了時に自律行動タイマーをリセット
        try:
            MotivationManager(soul_vessel_room).update_last_interaction()
            print(f"--- [MotivationManager] {soul_vessel_room}: 対話完了によりクールダウンをリセットしました ---")
        except Exception as e:
            print(f"--- [MotivationManager] クールダウンリセットエラー: {e} ---")

        # --- [Arousal] 会話終了時のArousal計算 ---
        # 会話前後の内部状態変化からArousalスコアを計算し、ログに出力
        try:
            if internal_state_before:
                from motivation_manager import MotivationManager
                from arousal_calculator import calculate_arousal, get_arousal_level

                mm = MotivationManager(soul_vessel_room)
                internal_state_after = mm.get_state_snapshot()

                arousal_score = calculate_arousal(internal_state_before, internal_state_after)
                arousal_level = get_arousal_level(arousal_score)

                print(f"  - [Arousal] 会話のArousalスコア: {arousal_score:.3f} ({arousal_level})")

                # 変化の詳細をログ出力
                curiosity_change = internal_state_after.get("curiosity", 0) - internal_state_before.get("curiosity", 0)
                # 後方互換性: relatednessがなければdevotionを使用
                relatedness_before = internal_state_before.get("relatedness", internal_state_before.get("devotion", 0))
                relatedness_after = internal_state_after.get("relatedness", internal_state_after.get("devotion", 0))
                relatedness_change = relatedness_after - relatedness_before
                persona_emotion_before = internal_state_before.get("persona_emotion", "neutral")
                persona_emotion_after = internal_state_after.get("persona_emotion", "neutral")

                if arousal_score > 0:
                    print(f"    - 好奇心変化: {curiosity_change:+.3f}, 関係性変化: {relatedness_change:+.3f}")
                    print(f"    - ペルソナ感情: {persona_emotion_before} → {persona_emotion_after}")

                # --- [Phase 2] Arousalを永続保存 ---
                # [修正] AIメッセージが正常に生成された（文字数がある）場合のみ蓄積する
                if last_ai_message:
                    import session_arousal_manager
                    session_arousal_manager.add_arousal_score(soul_vessel_room, arousal_score, time_str=last_ai_timestamp_str)
                else:
                    print(f"  - [Arousal] AI応答が空または未完成のため、蓄積をスキップします")
        except Exception as e:
            print(f"  - [Arousal] 計算エラー: {e}")
        # --- Arousal計算ここまで ---

        # 【修正】タイプライター完了時は既に正しい履歴がyieldされているので、再読み込みをスキップ
        if typewriter_completed_successfully:
            # タイプライター完了時: 既存の履歴を再利用
            final_chatbot_history = chatbot_history
            final_mapping_list = mapping_list
        else:
            # エラー時、キャンセル時、タイプライターOFF時など: ログから再読み込み
            final_chatbot_history, final_mapping_list = reload_chat_log(
                room_name=soul_vessel_room,
                api_history_limit_value=api_history_limit,
                add_timestamp=add_timestamp,
                display_thoughts=display_thoughts,
                screenshot_mode=screenshot_mode,
                redaction_rules=redaction_rules
            )

        # --- [システムアナウンス] 応答中に発生した通知を同一ターンでチャットへ表示する ---
        # 例: 画像非対応モデルへ画像付き履歴を送ったため画像を省略した、等。
        # （turn冒頭の consume はこのターンの agent 実行より前に走るため、ここで再消費する）
        try:
            pending_notices = utils.consume_system_notices()
            if pending_notices:
                level_label = {
                    "info": "ℹ️ システム",
                    "warning": "⚠️ システム警告",
                    "error": "⛔ システムエラー",
                }
                notice_log_f, _, _, _, _, _, _ = get_room_files_paths(soul_vessel_room)
                for notice in pending_notices:
                    label = level_label.get(notice.get("level", "warning"), "⚠️ システム警告")
                    notice_msg = f"{label}: {notice['message']}"
                    final_chatbot_history.append(_chatbot_message("assistant", notice_msg))
                    if notice_log_f:
                        utils.save_message_to_log(notice_log_f, "## SYSTEM:Nexus Ark", notice_msg)
        except Exception as _notice_e:
            print(f"--- [System Notice] 応答後の通知表示でエラー: {_notice_e} ---")

        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
        new_scenery_text, scenery_image, token_count_text = "（更新失敗）", None, _hide_token_count_display(soul_vessel_room)
        try:
            season_en, time_of_day_en = utils._get_current_time_context(soul_vessel_room)
            _, _, new_scenery_text = generate_scenery_context(soul_vessel_room, api_key, season_en=season_en, time_of_day_en=time_of_day_en)
            scenery_image = utils.find_scenery_image(soul_vessel_room, utils.get_current_location(soul_vessel_room), season_en=season_en, time_of_day_en=time_of_day_en)
        except Exception as e:
            print(f"--- 警告: 応答後の情景更新に失敗しました (API制限の可能性): {e} ---")

        final_df_with_ids = render_alarms_as_dataframe()
        final_df = get_display_df(final_df_with_ids)
        new_location_choices = _get_location_choices_for_ui(soul_vessel_room)
        latest_location_id = utils.get_current_location(soul_vessel_room)
        location_dropdown_update = gr.update(choices=new_location_choices, value=latest_location_id)

        # [v20] 動画アバター対応: 応答完了時に表情を更新
        # 最後のAI応答から表情を抽出
        final_expression = "idle"
        try:
            # タイプライター完了時などは chatbot_history が最新
            # エラー時は final_chatbot_history が最新
            target_history = final_chatbot_history if 'final_chatbot_history' in locals() else chatbot_history

            if target_history and len(target_history) > 0:
                last_response = target_history[-1]
                if last_response and len(last_response) >= 2:
                    ai_content = _chatbot_content(last_response)
                    if isinstance(ai_content, str):
                        final_expression = extract_expression_from_response(ai_content, soul_vessel_room)
        except Exception as e:
            print(f"--- [Avatar] 表情抽出エラー: {e} ---")

        final_profile_update = gr.update(value=get_avatar_html(soul_vessel_room, state=final_expression))

        # [v21] 現在地連動背景: ツール使用後に背景CSSも更新
        effective_settings_for_style = config_manager.get_effective_settings(soul_vessel_room)
        style_css_update = gr.update(value=_generate_style_from_settings(soul_vessel_room, effective_settings_for_style))

        token_count_text = _format_token_display(soul_vessel_room)
        _release_rag_cache_after_chat(soul_vessel_room)
        _trim_process_memory_after_chat(soul_vessel_room)
        log_memory_diagnostics(
            "chat_stream:end",
            soul_vessel_room,
            {
                "history": len(final_chatbot_history),
                "mapping": len(final_mapping_list),
                "error": bool(final_error_message),
                "typewriter": bool(typewriter_completed_successfully),
            }
        )

        yield (final_chatbot_history, final_mapping_list, gr.update(), token_count_text,
               location_dropdown_update, new_scenery_text,
               final_df_with_ids, final_df, scenery_image,
               current_console_content, current_console_content,
               gr.update(visible=False, interactive=True), gr.update(interactive=True),
               gr.update(visible=False),
               final_profile_update, # [v19] Stop Animation
               style_css_update, # [v21] Sync Background
               translation_cache # [v22] 17番目
        )

def _create_api_parts_from_files(file_paths: List[str]) -> List[Dict]:
    """
    ファイルパスのリストを受け取り、API送信用のパーツ(Dict)のリストを生成する。
    """
    parts = []
    for file_path in file_paths:
        try:
            if not file_path or not os.path.exists(file_path):
                continue

            file_basename = os.path.basename(file_path)
            kind = filetype.guess(file_path)
            mime_type = kind.mime if kind else "application/octet-stream"

            if mime_type.startswith('image/'):
                # APIコスト削減: 画像をリサイズ
                resize_result = utils.resize_image_for_api(file_path, max_size=768, return_image=False)
                if resize_result:
                    encoded_string, output_format = resize_result
                    mime_type = f"image/{output_format}"
                else:
                    with open(file_path, "rb") as f:
                        encoded_string = base64.b64encode(f.read()).decode("utf-8")
                parts.append({
                    "type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded_string}"}
                })
            elif mime_type.startswith('audio/') or mime_type.startswith('video/'):
                # 音声/動画: file形式でBase64エンコード
                with open(file_path, "rb") as f:
                    encoded_string = base64.b64encode(f.read()).decode("utf-8")
                parts.append({
                    "type": "file",
                    "source_type": "base64",
                    "mime_type": mime_type,
                    "data": encoded_string
                })
            else:
                # テキスト系ファイル: 内容を読み込んでテキストとして送信
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    parts.append({
                        "type": "text",
                        "text": f"[ATTACHED_FILE: {file_basename}]\n```\n{content}\n```\n[/ATTACHED_FILE]"
                    })
                except Exception as read_e:
                    parts.append({"type": "text", "text": f"（ファイル「{file_basename}」の読み込み中にエラーが発生しました: {read_e}）"})
        except Exception as e:
            print(f"--- [_create_api_parts_from_files] ファイル処理エラー: {e} ---")
            traceback.print_exc()
            parts.append({"type": "text", "text": f"（添付ファイル「{os.path.basename(file_path)}」の処理中に致命的なエラーが発生しました）"})
    return parts

def handle_message_submission(
    multimodal_input: dict, soul_vessel_room: str, api_key_name: str,
    api_history_limit: str, debug_mode: bool,
    console_content: str, active_participants: list, group_hide_thoughts: bool,
    active_attachments: list,
    global_model: str,
    enable_typewriter_effect: bool, streaming_speed: float,
    scenery_text_from_ui: str,
    screenshot_mode: bool,
    redaction_rules: list,
    enable_supervisor: bool = False, # [v18] Supervisor機能の有効/無効
    group_supervisor_rounds: int = 1,
    # [v22] 翻訳不整合対策
    translation_cache: dict = None
):
    import time
    perf_start = time.time()
    # print(f"--- [PERF] handle_message_submission start (room={soul_vessel_room}) ---")
    """
    【v9: 添付ファイル永続化FIX版】新規メッセージの送信を処理する司令塔。
    """
    # 1. ユーザー入力を解析 (変更なし)
    textbox_content = multimodal_input.get("text", "") if multimodal_input else ""
    if isinstance(textbox_content, str):
        textbox_content = textbox_content.replace("\r\n", "\n").replace("\r", "\n")
    file_input_list = multimodal_input.get("files", []) if multimodal_input else []
    user_prompt_from_textbox = textbox_content if isinstance(textbox_content, str) and textbox_content.strip() else ""

    # --- [v9: 空送信ガード] ---
    # テキスト入力がなく、かつファイルも添付されていない場合は、何もせずに終了する
    if not user_prompt_from_textbox and not file_input_list:
        # 戻り値の数は unified_streaming_outputs の要素数と一致させる必要がある (16個)
        # 既存のUIの状態を維持するため、全て gr.update() を返す
        yield (gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               translation_cache) # [v22] 17要素
        return
    # --- [ガードここまで] ---

    log_message_parts = []
    timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')}"

    if user_prompt_from_textbox:
        log_message_parts.append(user_prompt_from_textbox + timestamp)

    # 永続化用のパスリスト
    files_to_send_api = []


    if file_input_list:
        attachments_dir = os.path.join(constants.ROOMS_DIR, soul_vessel_room, "attachments")
        os.makedirs(attachments_dir, exist_ok=True)

        for file_obj in file_input_list:
            try:
                permanent_path = None
                temp_file_path = None
                original_filename = None

                # --- ステップ1: 一時ファイルパスと元のファイル名を取得 ---
                # ケースA: ファイルアップロード or ドラッグ＆ドロップ (FileDataオブジェクト)
                if hasattr(file_obj, 'name') and file_obj.name and os.path.exists(file_obj.name):
                    temp_file_path = file_obj.name
                    # Gradioが作る一時ファイル名から元のファイル名を取り出す
                    original_filename = os.path.basename(temp_file_path)

                # ケースB: 画像などのクリップボードからのペースト (パス文字列)
                elif isinstance(file_obj, str) and os.path.exists(file_obj):
                    temp_file_path = file_obj
                    # ★★★ ここが新しいロジック ★★★
                    # 元のファイル名が存在しないため、タイムスタンプから生成する
                    kind = filetype.guess(temp_file_path)
                    ext = kind.extension if kind else 'tmp'
                    timestamp_fname = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    original_filename = f"pasted_image_{timestamp_fname}.{ext}"

                # ケースC: テキストのペースト (テキスト文字列そのもの)
                elif isinstance(file_obj, str):
                    unique_filename = f"{uuid.uuid4().hex}_pasted_text.txt"
                    permanent_path = os.path.join(attachments_dir, unique_filename)
                    with open(permanent_path, "w", encoding="utf-8") as f:
                        f.write(file_obj)
                    print(f"--- [ファイル永続化] ペーストされたテキストを保存しました: {permanent_path} ---")
                    log_message_parts.append(f"[ファイル添付: {permanent_path}]")
                    files_to_send_api.append(permanent_path)
                    continue # このファイルの処理は完了

                # --- ステップ2: ファイルのコピーとログへの記録 ---
                if temp_file_path and original_filename:
                    # ファイル名の衝突を避けるための最終的なファイル名を生成
                    unique_filename = f"{uuid.uuid4().hex}_{original_filename}"
                    permanent_path = os.path.join(attachments_dir, unique_filename)

                    shutil.copy(temp_file_path, permanent_path)
                    print(f"--- [ファイル永続化] 添付ファイルをコピーしました: {permanent_path} ---")

                    # --- [v32 画像キャプションの生成と保存] ---
                    kind = filetype.guess(permanent_path)
                    if kind and kind.mime.startswith('image/'):
                        print(f"--- [画像キャプション生成] 画像の自動キャプションを生成中... ---")
                        from tools.image_tools import generate_image_caption
                        caption = generate_image_caption(permanent_path, api_key_name)
                        log_message_parts.append(f"<details><summary>📸 画像キャプション</summary>\n{caption}\n</details>")
                        log_message_parts.append(f"[VIEW_IMAGE: {permanent_path}]")
                        print(f"--- [画像キャプション生成完了] ---")
                    else:
                        log_message_parts.append(f"[ファイル添付: {permanent_path}]")

                    files_to_send_api.append(permanent_path)
                else:
                    print(f"--- [ファイル永続化警告] 未知または無効な添付ファイルオブジェクトです: {file_obj} ---")

            except Exception as e:
                print(f"--- [ファイル永続化エラー] 添付ファイルの処理中にエラーが発生しました: {e} ---")
                traceback.print_exc()
                log_message_parts.append(f"[ファイル添付エラー: {e}]")

    full_user_log_entry = "\n".join(log_message_parts)

    if not full_user_log_entry:
        effective_settings = config_manager.get_effective_settings(soul_vessel_room)
        add_timestamp = effective_settings.get("add_timestamp", False)
        history, mapping = reload_chat_log(soul_vessel_room, api_history_limit, add_timestamp)
        # 戻り値の数を16個に合わせる
        yield (history, mapping, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(visible=False), gr.update(interactive=True), gr.update(), gr.update(), gr.update(), translation_cache)
        return

    # 2. ユーザーの発言を、セッション参加者全員のログに書き込む
    all_participants_in_session = [soul_vessel_room] + (active_participants or [])
    for room_name in all_participants_in_session:
        log_f, _, _, _, _, _, _ = get_room_files_paths(room_name)
        if log_f:
            utils.save_message_to_log(log_f, "## USER:user", full_user_log_entry)

    # 3. API用の入力パーツを準備
    user_prompt_parts_for_api = []
    if user_prompt_from_textbox:
        user_prompt_parts_for_api.append({"type": "text", "text": user_prompt_from_textbox})

    if files_to_send_api:
        # 共通ヘルパーを使用してパーツを作成
        user_prompt_parts_for_api.extend(_create_api_parts_from_files(files_to_send_api))

    # --- [情景画像のAI共有] ---
    # 場所移動、画像更新、起動後初回の場合のみ画像を添付（コスト効率化）
    try:
        effective_settings = config_manager.get_effective_settings(soul_vessel_room)
        send_scenery_image_enabled = effective_settings.get("send_scenery", False)
        scenery_send_mode = effective_settings.get("scenery_send_mode", "変更時のみ")

        # print(f"--- [情景画像AI共有] 設定チェック: send_scenery = {send_scenery_image_enabled}, mode = {scenery_send_mode} ---")

        if send_scenery_image_enabled:
            season_en, time_of_day_en = utils._get_current_time_context(soul_vessel_room)
            current_location = utils.get_current_location(soul_vessel_room)

            # --- [一時的現在地対応] ---
            from agent.temporary_location_manager import TemporaryLocationManager
            tlm = TemporaryLocationManager()
            if tlm.is_active(soul_vessel_room):
                temp_data = tlm.get_current_data(soul_vessel_room)
                current_scenery_image = temp_data.get("image_path")
                # print(f"  - [TempLocation Active] 画像パスを使用: {current_scenery_image}")
            else:
                current_scenery_image = utils.find_scenery_image(
                    soul_vessel_room, current_location, season_en, time_of_day_en
                )

            # print(f"  - 現在地: {current_location}, 季節: {season_en}, 時間帯: {time_of_day_en}")
            # print(f"  - 画像パス: {current_scenery_image}")

            if current_scenery_image and os.path.exists(current_scenery_image):
                # room_config から「最後に送信した画像パス」を取得
                room_config = room_manager.get_room_config(soul_vessel_room) or {}
                last_sent_image = room_config.get("last_sent_scenery_image")

                # print(f"  - 最後に送信した画像: {last_sent_image}")

                # 送信判定: 「毎ターン」モードなら常に送信、「変更時のみ」なら画像が異なる場合のみ
                should_send = (scenery_send_mode == "毎ターン") or (current_scenery_image != last_sent_image)

                if should_send:
                    reason = "毎ターン送信" if scenery_send_mode == "毎ターン" else "新しい景色を検出"
                    # print(f"  - ✅ {reason}！画像をAIに送信します")

                    # 画像をリサイズしてBase64エンコード（コスト削減）
                    resize_result = utils.resize_image_for_api(current_scenery_image, max_size=512)

                    if resize_result:
                        # ★修正: resize_image_for_apiはタプル(base64_string, format)を返す
                        encoded_image, output_format = resize_result
                        mime_type = f"image/{output_format}"
                        # print(f"  - ✅ 画像リサイズ成功 (Base64: {len(encoded_image)} chars, format: {output_format})")
                        # ユーザーの発言の前に情景画像を挿入
                        scenery_parts = [
                            {"type": "text", "text": "（システム：現在の光景）"},
                            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded_image}"}}
                        ]
                        user_prompt_parts_for_api = scenery_parts + user_prompt_parts_for_api

                        # 送信済みとして記録（変更時のみモードでの重複送信防止用）
                        room_manager.update_room_config(
                            soul_vessel_room,
                            {"last_sent_scenery_image": current_scenery_image}
                        )
                        # print(f"  - ✅ 画像送信完了＆記録更新")

                    else:
                        print(f"  - ❌ 画像リサイズ失敗")
                else:
                    print(f"  - ⏭️ 前回と同じ景色のためスキップ")
            else:
                print(f"  - ⚠️ 情景画像が見つかりません")
        else:
            print(f"  - ⏭️ 情景画像共有は無効")
    except Exception as e:
        print(f"--- [情景画像AI共有 警告] 処理中にエラーが発生しました: {e} ---")
        traceback.print_exc()
    # --- [情景画像のAI共有 ここまで] ---

    print(f"--- [PERF] handle_message_submission pre-processing done: {time.time() - perf_start:.4f}s ---")

    # 4. 中核となるストリーミング関数を呼び出す (変更なし)
    yield from _stream_and_handle_response(
        room_to_respond=soul_vessel_room,
        full_user_log_entry=full_user_log_entry,
        user_prompt_parts_for_api=user_prompt_parts_for_api,
        api_key_name=api_key_name,
        global_model=global_model,
        api_history_limit=api_history_limit,
        debug_mode=debug_mode,
        soul_vessel_room=soul_vessel_room,
        active_participants=active_participants or [],
        group_hide_thoughts=group_hide_thoughts,  # グループ会話 思考ログ非表示
        active_attachments=active_attachments or [],
        current_console_content=console_content,
        enable_typewriter_effect=enable_typewriter_effect,
        streaming_speed=streaming_speed,
        scenery_text_from_ui=scenery_text_from_ui,
        screenshot_mode=screenshot_mode,
        redaction_rules=redaction_rules,
        enable_supervisor=enable_supervisor,
        group_supervisor_rounds=group_supervisor_rounds,
        # [v22] 翻訳不整合対策
        translation_cache=translation_cache
    )

def handle_gradio_voice_transcription(audio_path, stt_provider: str, action_mode: str, multimodal_input: dict, room_name: str, api_key_name: str):
    """Gradioのマイク録音を文字起こしし、チャット入力欄へ反映する。"""
    if not audio_path:
        return gr.update(), "録音がありません。マイクで録音してから文字起こししてください。", False

    try:
        path_str = audio_path if isinstance(audio_path, str) else getattr(audio_path, "name", "")
        if not path_str or not os.path.exists(path_str):
            return gr.update(), "録音ファイルが見つかりません。もう一度録音してください。", False
        path_str = _cache_gradio_voice_input_file(path_str, room_name)

        import stt_manager

        provider_key = (stt_provider or "gemini").strip().lower()
        if provider_key == "openai_whisper":
            openai_setting = (
                config_manager.get_openai_setting_by_name("OpenAI")
                or config_manager.get_openai_setting_by_name("OpenAI Official")
            )
            openai_api_key = (openai_setting or {}).get("api_key", "")
            if not openai_api_key:
                return gr.update(), "OpenAI Official APIキーが見つかりません。APIキー/Webhook管理で設定してください。", False
            provider_label = "OpenAI Whisper"
            stt_result = stt_manager.transcribe_audio_file_openai_detailed(
                path_str,
                openai_api_key,
                model_name="whisper-1",
                base_url=(openai_setting or {}).get("base_url") or "https://api.openai.com/v1",
            )
        else:
            provider_label = "Gemini STT"
            effective_key_name = api_key_name or config_manager.initial_api_key_name_global
            gemini_api_key = config_manager.GEMINI_API_KEYS.get(effective_key_name)
            if not gemini_api_key:
                return gr.update(), "Gemini APIキーが見つかりません。", False
            stt_result = stt_manager.transcribe_audio_file_detailed(
                path_str,
                gemini_api_key,
                model_name=constants.DISCORD_VOICE_STT_MODEL,
            )

        transcript = (getattr(stt_result, "text", "") or "").strip()
        if not transcript:
            return gr.update(), f"{provider_label}: 聞き取れませんでした。", False
        if getattr(stt_result, "uncertain", False):
            return gr.update(), f"{provider_label}: 低信頼候補「{transcript}」。入力欄には反映しませんでした。", False

        current_text = ""
        current_files = []
        if isinstance(multimodal_input, dict):
            current_text = multimodal_input.get("text", "") or ""
            current_files = multimodal_input.get("files", []) or []

        new_text = transcript if not current_text.strip() else f"{current_text.rstrip()}\n{transcript}"
        auto_submit = (action_mode or "confirm").strip().lower() == "auto"
        status = f"{provider_label}: 文字起こししました。"
        if auto_submit:
            status += " 自動送信します。"
        return gr.update(value={"text": new_text, "files": current_files}), status, auto_submit
    except Exception as e:
        logger.error(f"Gradio voice transcription failed: {e}", exc_info=True)
        return gr.update(), f"音声文字起こしに失敗しました: {e}", False


def _cache_gradio_voice_input_file(source_path: str, room_name: str) -> str:
    """Gradio直マイクの録音をルームのaudio_cache配下へ保存し、そのパスを返す。"""
    try:
        if not room_name:
            return source_path
        if not source_path or not os.path.exists(source_path):
            return source_path

        voice_dir = os.path.join(constants.ROOMS_DIR, room_name, "audio_cache", "voice_input", "gradio")
        os.makedirs(voice_dir, exist_ok=True)
        ext = os.path.splitext(source_path)[1] or ".wav"
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        cached_path = os.path.abspath(os.path.join(voice_dir, f"voice_gradio_{timestamp}{ext}"))
        if os.path.abspath(source_path) != cached_path:
            shutil.copy2(source_path, cached_path)
        _cleanup_voice_input_dir(voice_dir, extensions={ext.lower()})
        return cached_path
    except Exception as e:
        logger.debug(f"Gradio voice input cache skipped: {e}")
        return source_path


def _cleanup_voice_input_dir(voice_dir: str, extensions: set = None):
    try:
        keep_count = int(config_manager.CONFIG_GLOBAL.get("voice_input_audio_rotation_count", 10) or 10)
        keep_count = max(1, min(keep_count, 100))
        extensions = extensions or {".wav", ".mp3", ".m4a", ".webm", ".ogg"}
        audio_files = []
        for filename in os.listdir(voice_dir):
            path = os.path.join(voice_dir, filename)
            if os.path.isfile(path) and os.path.splitext(filename)[1].lower() in extensions:
                audio_files.append(path)
        audio_files.sort(key=lambda path: os.path.getmtime(path), reverse=True)
        for old_path in audio_files[keep_count:]:
            try:
                os.remove(old_path)
            except OSError:
                pass
    except Exception as e:
        logger.debug(f"Voice input cache cleanup skipped: {e}")


def handle_gradio_voice_auto_submission(
    auto_submit_ready: bool,
    multimodal_input: dict, soul_vessel_room: str, api_key_name: str,
    api_history_limit: str, debug_mode: bool,
    console_content: str, active_participants: list, group_hide_thoughts: bool,
    active_attachments: list,
    global_model: str,
    enable_typewriter_effect: bool, streaming_speed: float,
    scenery_text_from_ui: str,
    screenshot_mode: bool,
    redaction_rules: list,
    enable_supervisor: bool = False,
    group_supervisor_rounds: int = 1,
    translation_cache: dict = None
):
    """Gradio音声入力の自動送信モード用に、既存の送信ハンドラへ橋渡しする。"""
    if not auto_submit_ready:
        yield (gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               translation_cache)
        return

    yield from handle_message_submission(
        multimodal_input, soul_vessel_room, api_key_name,
        api_history_limit, debug_mode,
        console_content, active_participants, group_hide_thoughts,
        active_attachments,
        global_model,
        enable_typewriter_effect, streaming_speed,
        scenery_text_from_ui,
        screenshot_mode,
        redaction_rules,
        enable_supervisor,
        group_supervisor_rounds,
        translation_cache,
    )

def handle_rerun_button_click(
    selected_message: Optional[Dict], room_name: str, api_key_name: str,
    api_history_limit: str, debug_mode: bool,
    console_content: str, active_participants: list, group_hide_thoughts: bool,
    active_attachments: list,
    global_model: str,
    enable_typewriter_effect: bool, streaming_speed: float,
    scenery_text_from_ui: str,
    screenshot_mode: bool,
    redaction_rules: list,
    enable_supervisor: bool = False,
    group_supervisor_rounds: int = 1,
    # [v22] 翻訳不整合対策
    translation_cache: dict = None,
    abs_index: Optional[int] = None
):
    """
    【v3: 遅延解消版】発言の再生成を処理する司令塔。
    """
    if not selected_message or not room_name:
        gr.Warning("再生成の起点となるメッセージが選択されていません。")
        yield (gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), console_content, console_content,
               gr.update(visible=True, interactive=True), gr.update(interactive=True), gr.update(), gr.update(), gr.update(),
               translation_cache) # [v22] 17要素
        return

    # 1. ログを巻き戻し、再送信するユーザー発言を取得
    log_f, _, _, _, _, _, _ = get_room_files_paths(room_name)
    # SYSTEMメッセージもAI応答と同様に扱い、直前のユーザー発言から再生成する
    is_ai_or_system_message = selected_message.get("role") in ("AGENT", "SYSTEM")

    restored_input_text = None
    deleted_timestamp = None
    if is_ai_or_system_message:
        restored_input_text, deleted_timestamp = utils.delete_and_get_previous_user_input(
            log_f,
            selected_message,
            target_abs_index=abs_index,
        )
    else: # ユーザー発言の場合
        restored_input_text, _ = utils.delete_user_message_and_after(
            log_f,
            selected_message,
            target_abs_index=abs_index,
        )

    if restored_input_text is None:
        gr.Error("ログの巻き戻しに失敗しました。再生成できません。")
        effective_settings = config_manager.get_effective_settings(room_name)
        add_timestamp = effective_settings.get("add_timestamp", False)
        history, mapping = reload_chat_log(room_name, api_history_limit, add_timestamp)
        yield (history, mapping, gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), console_content, console_content,
               gr.update(visible=True, interactive=True), gr.update(interactive=True), gr.update(), gr.update(), gr.update(),
               translation_cache)  # [v22] 17要素
        return

    # [SessionArousal] 再生成対象のAIメッセージのArousalデータを削除
    if deleted_timestamp:
        import session_arousal_manager
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        session_arousal_manager.remove_arousal_session(room_name, today_str, deleted_timestamp)

    # 2. 巻き戻したユーザー発言に、新しいタイムスタンプを付加してログに再保存
    timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')}"
    full_user_log_entry = restored_input_text.strip() + timestamp
    utils.save_message_to_log(log_f, "## USER:user", full_user_log_entry)

    # [v22] 翻訳キャッシュの不整合防止：削除起点以降のキャッシュをクリア
    if abs_index is not None and translation_cache:
        new_cache = {k: v for k, v in translation_cache.items() if k < abs_index}
        translation_cache = new_cache
        print(f"--- [DEBUG:Rerun] Translation cache cleared for indices >= {abs_index} ---")

    gr.Info("応答を再生成します...")

    # 添付ファイルマーカー [ファイル添付: /path/to/file] をパースしてAPIパーツを構築
    # ログ保存用（full_user_log_entry）には残すが、API送信用のテキストからは除去する
    attachment_pattern = re.compile(r'\[ファイル添付: (.*?)\]')
    found_attachments = attachment_pattern.findall(restored_input_text)

    # API送信用のクリーンなテキストを作成（マーカーを除去）
    clean_input_text = attachment_pattern.sub('', restored_input_text).strip()

    user_prompt_parts_for_api = []
    if clean_input_text:
        user_prompt_parts_for_api.append({"type": "text", "text": clean_input_text})

    if found_attachments:
        print(f"--- [Rerun] 過去の添付ファイルを検出しました: {found_attachments} ---")
        user_prompt_parts_for_api.extend(_create_api_parts_from_files(found_attachments))

    # 3. 中核となるストリーミング関数を呼び出す
    yield from _stream_and_handle_response(
        room_to_respond=room_name,
        full_user_log_entry=full_user_log_entry,
        user_prompt_parts_for_api=user_prompt_parts_for_api,
        api_key_name=api_key_name,
        global_model=global_model,
        api_history_limit=api_history_limit,
        debug_mode=debug_mode,
        soul_vessel_room=room_name,
        active_participants=active_participants or [],
        group_hide_thoughts=group_hide_thoughts,  # グループ会話 思考ログ非表示
        active_attachments=active_attachments or [],
        current_console_content=console_content,
        enable_typewriter_effect=enable_typewriter_effect,
        streaming_speed=streaming_speed,
        scenery_text_from_ui=scenery_text_from_ui,
        screenshot_mode=screenshot_mode,
        redaction_rules=redaction_rules,
        enable_supervisor=enable_supervisor,  # [v18] Supervisor機能の有効/無効
        group_supervisor_rounds=group_supervisor_rounds,
        # [v22] 翻訳キャッシュを最後の戻り値に追加するためにラップ
        translation_cache=translation_cache
    )

def _get_updated_scenery_and_image(room_name: str, api_key_name: str, force_text_regenerate: bool = False) -> Tuple[str, Optional[str]]:
    """
    【v9: 状態非干渉版】
    情景のテキストと画像の取得・生成に関する全責任を負う、唯一の司令塔。
    この関数は、現在のファイル状態を読み取るだけで、決して書き込みは行わない。
    """
    try:
        effective_settings = config_manager.get_effective_settings(room_name)
        if not effective_settings.get("enable_scenery_system", True):
            return "（情景描写システムは、このルームでは無効です）", None

        if not room_name or not api_key_name:
            return "（ルームまたはAPIキーが未選択です）", None

        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
        if not api_key or api_key.startswith("YOUR_API_KEY"):
            return "（有効なAPIキーが設定されていません）", None

        current_location = utils.get_current_location(room_name)
        if not current_location:
            raise ValueError("現在地が設定されていません。UIハンドラ側で初期化が必要です。")

        season_en, time_of_day_en = utils._get_current_time_context(room_name) # utilsから呼び出す

        _, _, scenery_text = generate_scenery_context(
            room_name, api_key, force_regenerate=force_text_regenerate,
            season_en=season_en, time_of_day_en=time_of_day_en
        )

        scenery_image_path = utils.find_scenery_image(
            room_name, current_location, season_en, time_of_day_en
        )

        if scenery_image_path is None:
            # 以前はここで handle_generate_or_regenerate_scenery_image を呼んでいた
            pass

        return scenery_text, _load_image_for_gradio(scenery_image_path)

    except Exception as e:
        err_str = str(e).upper()
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            error_message = f"利用可能なすべてのAPIキーの制限に達しました（429エラー）。しばらく待つか、別のプロバイダを検討してください。"
            # print(f"--- [API制限] {error_message} ---")
            gr.Warning(error_message)
            return "（API制限により情景を取得できませんでした）", None

        error_message = f"情景描写システムの処理中にエラーが発生しました。設定ファイル（world_settings.txtなど）が破損している可能性があります。"
        print(f"--- [司令塔エラー] {error_message} ---")
        traceback.print_exc()
        gr.Warning(error_message)
        return "（情景の取得中にエラーが発生しました）", None

def handle_scenery_refresh(room_name: str, api_key_name: str) -> Tuple[gr.update, str, Optional[str], gr.update]:
    """「情景テキストを更新」ボタンのハンドラ。新しい司令塔を呼び出す。"""
    gr.Info(f"「{room_name}」の現在の情景を再生成しています...")
    # 新しい司令塔を呼び出し、テキストの強制再生成フラグを立てる
    new_scenery_text, new_image_path = _get_updated_scenery_and_image(
        room_name, api_key_name, force_text_regenerate=True
    )
    latest_location_id = utils.get_current_location(room_name)

    # スタイル更新
    effective_settings = config_manager.get_effective_settings(room_name)
    new_style = _generate_style_from_settings(room_name, effective_settings)

    return gr.update(value=latest_location_id), new_scenery_text, new_image_path, gr.update(value=latest_location_id), gr.update(value=new_style)

def handle_location_change(
    room_name: str,
    selected_value: str,
    api_key_name: str
) -> Tuple[gr.update, str, Optional[str], gr.update]:
    """【v9: 冪等性ガード版】場所が変更されたときのハンドラ。"""

    # --- [冪等性ガード] ---
    # ファイルに記録されている現在の場所と比較し、変更がなければ何もしない
    current_location_from_file = utils.get_current_location(room_name)

    # 設定をロード（スタイル生成用）
    effective_settings = config_manager.get_effective_settings(room_name)

    def _create_return_tuple(loc_val, scen_text, img_path):
        return (
            gr.update(value=loc_val),
            scen_text,
            img_path,
            gr.update(value=loc_val),
            gr.update(value=_generate_style_from_settings(room_name, effective_settings))
        )

    if selected_value == current_location_from_file:
        return (gr.update(), gr.update(), gr.update(), gr.update(), gr.update()) # UIの状態を何も変更しない


    if not selected_value or selected_value.startswith("__AREA_HEADER_"):
        # ヘッダーがクリックされた場合、現在の値でUIを更新するだけ
        new_scenery_text, new_image_path = _get_updated_scenery_and_image(room_name, api_key_name)
        return _create_return_tuple(current_location_from_file, new_scenery_text, new_image_path)

    # --- ここから下は、本当に場所が変更された場合のみ実行される ---
    location_id = selected_value
    print(f"--- UIからの場所変更処理開始: ルーム='{room_name}', 移動先ID='{location_id}' ---")

    from tools.space_tools import set_current_location
    result = set_current_location.func(location_id=location_id, room_name=room_name)
    if "Success" not in result:
        gr.Error(f"場所の変更に失敗しました: {result}")
        new_scenery_text, new_image_path = _get_updated_scenery_and_image(room_name, api_key_name)
        return _create_return_tuple(current_location_from_file, new_scenery_text, new_image_path)

    gr.Info(f"場所を「{location_id}」に移動しました。情景を更新します...")
    new_scenery_text, new_image_path = _get_updated_scenery_and_image(room_name, api_key_name)
    return _create_return_tuple(location_id, new_scenery_text, new_image_path)

#
# --- Room Management Handlers ---
#

def handle_create_room(new_room_name: str, new_user_display_name: str, new_agent_display_name: str, new_room_description: str, initial_system_prompt: str):
    """
    「新規作成」タブのロジック。
    新しいチャットルームを作成し、関連ファイルと設定を初期化する。
    """
    # 1. 入力検証
    if not new_room_name or not new_room_name.strip():
        gr.Warning("ルーム名は必須です。")
        # nexus_ark.pyのoutputsは9個 (v23)
        return (gr.update(),) * 9

    try:
        # 2. 安全なフォルダ名生成
        safe_folder_name = room_manager.generate_safe_folder_name(new_room_name)

        # 3. ルームファイル群の作成
        if not room_manager.ensure_room_files(safe_folder_name):
            gr.Error("ルームの基本ファイル作成に失敗しました。詳細はターミナルを確認してください。")
            return (gr.update(),) * 9

        # 4. 設定の書き込み
        config_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "room_config.json")
        with open(config_path, "r+", encoding="utf-8") as f:
            config = json.load(f)
            config["room_name"] = new_room_name.strip()
            if new_user_display_name and new_user_display_name.strip():
                config["user_display_name"] = new_user_display_name.strip()
            # 新しいフィールドを追加
            if new_agent_display_name and new_agent_display_name.strip():
                config["agent_display_name"] = new_agent_display_name.strip()
            if new_room_description and new_room_description.strip():
                config["description"] = new_room_description.strip()

            f.seek(0)
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.truncate()

        if initial_system_prompt and initial_system_prompt.strip():
            system_prompt_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "SystemPrompt.txt")
            with open(system_prompt_path, "w", encoding="utf-8") as f:
                f.write(initial_system_prompt)

        # 5. UI更新
        gr.Info(f"新しいルーム「{new_room_name}」を作成しました。ルーム選択メニューから切り替えてご利用ください。")
        updated_room_list = room_manager.get_room_list_for_ui()

        # フォームのクリア（5つのフィールド分）
        # clear_form はもはや使用しないため削除

        return (
            gr.update(choices=updated_room_list, value=safe_folder_name), # room_dropdown             # メインルーム選択
            gr.update(choices=updated_room_list, value=safe_folder_name), # manage_room_selector      # 管理タブ
            gr.update(choices=updated_room_list),                         # alarm_room_dropdown       # アラーム
            gr.update(choices=updated_room_list),                         # timer_room_dropdown       # タイマー
            gr.update(value=""),                                          # new_room_name
            gr.update(value=""),                                          # new_user_display_name
            gr.update(value=""),                                          # new_agent_display_name
            gr.update(value=""),                                          # new_room_description
            gr.update(value="")                                           # initial_system_prompt
        )

    except Exception as e:
        gr.Error(f"ルームの作成に失敗しました。詳細はターミナルを確認してください。: {e}")
        traceback.print_exc()
        return (gr.update(),) * 9

def handle_manage_room_select(selected_folder_name: str):
    """
    「管理」タブのルームセレクタ変更時のロジック。
    選択されたルームの情報をフォームに表示する。
    """
    if not selected_folder_name:
        return gr.update(visible=False), "", "", "", "", ""

    try:
        config_path = os.path.join(constants.ROOMS_DIR, selected_folder_name, "room_config.json")
        if not os.path.exists(config_path):
            gr.Warning(f"設定ファイルが見つかりません: {config_path}")
            return gr.update(visible=False), "", "", "", "", ""

        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        return (
            gr.update(visible=True),
            config.get("room_name", ""),
            config.get("user_display_name", ""),
            config.get("agent_display_name", ""), # agent_display_nameを読み込む
            config.get("description", ""),
            selected_folder_name
        )
    except Exception as e:
        gr.Error(f"ルーム設定の読み込み中にエラーが発生しました: {e}")
        traceback.print_exc()
        return gr.update(visible=False), "", "", "", "", ""

def handle_save_room_config(folder_name: str, room_name: str, user_display_name: str, agent_display_name: str, description: str):
    """
    「管理」タブの保存ボタンのロジック。
    ルームの設定情報を更新する。
    """
    if not folder_name:
        gr.Error("対象のルームフォルダが見つかりません。")
        return gr.update(), gr.update()

    if not room_name or not room_name.strip():
        gr.Warning("ルーム名は空にできません。")
        return gr.update(), gr.update()

    try:
        config_path = os.path.join(constants.ROOMS_DIR, folder_name, "room_config.json")
        with open(config_path, "r+", encoding="utf-8") as f:
            config = json.load(f)
            config["room_name"] = room_name.strip()
            config["user_display_name"] = user_display_name.strip()
            config["agent_display_name"] = agent_display_name.strip() # agent_display_nameを保存
            config["description"] = description.strip()
            f.seek(0)
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.truncate()

        gr.Info(f"ルーム「{room_name}」の設定を保存しました。")

        updated_room_list = room_manager.get_room_list_for_ui()

        # メインと管理タブのドロップダウンを更新
        main_dd_update = gr.update(choices=updated_room_list)
        manage_dd_update = gr.update(choices=updated_room_list)

        return main_dd_update, manage_dd_update

    except Exception as e:
        gr.Error(f"設定の保存中にエラーが発生しました: {e}")
        traceback.print_exc()
        return gr.update(), gr.update()

def handle_delete_room(confirmed: str, folder_name_to_delete: str, api_key_name: str, current_room_name: str = None, expected_count: int = 196):
    """
    【v7: 引数順序修正版】
    ルームを削除し、統一契約に従って常に正しい数の戻り値を返す。
    unified_full_room_refresh_outputs と完全に一致する値を返す。
    """
    if str(confirmed).lower() != 'true':
        return (gr.update(),) * expected_count

    if not folder_name_to_delete:
        gr.Warning("削除するルームが選択されていません。")
        return (gr.update(),) * expected_count

    try:
        room_path_to_delete = os.path.join(constants.ROOMS_DIR, folder_name_to_delete)
        if not os.path.isdir(room_path_to_delete):
            gr.Error(f"削除対象のフォルダが見つかりません: {room_path_to_delete}")
            return (gr.update(),) * expected_count

        send2trash(room_path_to_delete)
        gr.Info(f"ルーム「{folder_name_to_delete}」をゴミ箱に移動しました。復元が必要な場合はPCのゴミ箱を確認してください。")

        new_room_list = room_manager.get_room_list_for_ui()

        if new_room_list:
            new_main_room_folder = new_room_list[0][1]
            # handle_room_change_for_all_tabs を呼び出し、その結果をそのまま返す
            # 【Fix】expected_count を明示的に渡すことで、もしデフォルト値が古くても不整合を防群
            return handle_room_change_for_all_tabs(
                new_main_room_folder, api_key_name, expected_count
            )
        else:
            # ケース2: これが最後のルームだった場合
            gr.Warning("全てのルームが削除されました。新しいルームを作成してください。")
            # 契約数(150)に合わせてUIをリセットするための値を返す
            # initial_load_chat_outputs (150個) に対応
            empty_chat_updates = (
                None, [], [], gr.update(interactive=False, placeholder="ルームを作成してください。"),
                None, "", "", "", "", "", gr.update(choices=[], value=None), "", "", "", # 14 items
                gr.update(), gr.update(), gr.update(), gr.update(), # room dropdowns
                gr.update(), # location_dropdown
                "（ルームがありません）", # scenery_display
                config_manager.tts_provider_display_from_key("gemini"),
                gr.update(visible=False), # room_tts_profile_dropdown
                gr.update(choices=config_manager.get_tts_model_choices("gemini"), value="gemini-3.1-flash-tts-preview"),
                gr.update(choices=config_manager.get_tts_voice_choices("gemini"), value=list(config_manager.SUPPORTED_VOICES.values())[0]), "", # voice, style
                1.0, 0.0, 1.0, 1.0, # speed, pitch, intonation, volume
                True, 0.01, # typewriter, speed
                0.8, 0.95, *[gr.update()]*4, # temperature, top_p, safety
                False, # display_thoughts
                False, # send_thoughts
                True, # enable_auto_retrieval
                True, # add_timestamp
                True, # send_current_time
                True, # send_notepad
                True, # use_common_prompt
                True, # send_core_memory
                False, # send_scenery
                "変更時のみ", # scenery_send_mode
                False, # auto_memory_enabled
                True, # enable_self_awareness
                "ℹ️ *ルームを選択してください*",
                None, # scenery_image
                True, gr.update(open=False), # enable_scenery_system, accordion
                gr.update(value=constants.API_HISTORY_LIMIT_OPTIONS.get(constants.DEFAULT_API_HISTORY_LIMIT_OPTION, "20往復")),
                gr.update(value="既定 (AIに任せる / 通常モデル)"),
                constants.DEFAULT_API_HISTORY_LIMIT_OPTION,
                gr.update(value=constants.EPISODIC_MEMORY_OPTIONS.get(constants.DEFAULT_EPISODIC_MEMORY_DAYS, "なし（無効）")),
                gr.update(value="昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** -"),
                gr.update(value=False),
                gr.update(value=120),
                gr.update(value="00:00"),
                gr.update(value="07:00"),
                gr.update(value=None), # room_model_dropdown (56)
                gr.update(value="default"), # provider_radio (57)
                gr.update(visible=False), # google_settings (58)
                gr.update(visible=False), # openai_settings (59)
                gr.update(value=None), # api_key_dropdown (60)
                *[gr.update()]*6, # openai profiles to tool_use (6 items) (61-66)
                gr.update(value=None), # rotation (67)
                *[gr.update()]*8, # roblox settings (8 items) (68-75 from 150 count perspective)
                # Wait, re-aligning with current definition
                *[gr.update()]*13, # roblox group
                gr.update(value=True), # collect episodic
                gr.update(value=True), # memory index
                gr.update(value=False), # current log
                gr.update(value=True), # entity
                gr.update(value=False), # compress
                gr.update(value="未実行"), # compress_status
                gr.update(value=False), # theme enabled
                *[gr.update()]*8, # chat style to accent soft
                *[gr.update()]*13, # detailed theme
                *[gr.update()]*11, # bg image settings
                *[gr.update()]*9, # sync settings
                gr.update(), # save button
                gr.update(value=""), # style injector
                *[gr.update()]*4, # dream diary (125-128)
                *[gr.update()]*5, # episodic diary (129-133)
                gr.update(), # entity dropdown (134)
                gr.update(value=""), # entity editor (135)
                gr.update(value="google"), # embedding radio (136)
                gr.update(value="未実行"), # dream_status (137)
                gr.update(value=False), # auto summary (138)
                gr.update(value=constants.AUTO_SUMMARY_DEFAULT_THRESHOLD, visible=False), # threshold (139)
                "", # room project root (140)
                "", # project exclude dirs (141)
                "", # project exclude files (142)
                gr.update(value=refresh_expressions_ui(None)), # expressions html (143)
                gr.update(choices=get_all_expression_choices(None), value=None), # expression target (144)
                gr.update(choices=[constants.CREATIVE_NOTES_FILENAME], value=constants.CREATIVE_NOTES_FILENAME), # 145
                gr.update(choices=[constants.RESEARCH_NOTES_FILENAME], value=constants.RESEARCH_NOTES_FILENAME), # 146
                "", # temp scenery display (147)
                gr.update(choices=[], value=None), # saved locations (148)
                None, # temp scenery image (149)
                gr.update(selected="virtual_location_tab") # scenery tabs (150)
            )

            # ケース2の全項目を組み立てる (unified_full_room_refresh_outputs に合わせる)
            world_outputs = (None, None, "", None) # 4 items
            session_outputs = ([], "", []) # 3 items
            tail_outputs = (
                gr.update(value=[]), # redaction_rules_df
                gr.update(), # archive_date_dropdown
                gr.update(value="リアル連動"), # time_mode_radio
                gr.update(value="秋"), # fixed_season
                gr.update(value="夜"), # fixed_time_of_day
                gr.update(visible=False), # fixed_time_controls
                [], # attachments_df
                "現在アクティブな添付ファイルはありません。", # active_attachments_display
                gr.update(), # custom_scenery_location
                _hide_token_count_display(), # token_count
                "", # room_delete_confirmed_state
                "最終更新: -", # memory_reindex_status
                "最終更新: -"  # current_log_reindex_status
            )

            final_reset_outputs = empty_chat_updates + world_outputs + session_outputs + tail_outputs
            return _ensure_output_count(final_reset_outputs, expected_count)

    except Exception as e:
        gr.Error(f"ルームの削除中にエラーが発生しました: {e}")
        traceback.print_exc()
        return (gr.update(),) * expected_count





# --- Purpose Profile ---








# --- Generic Importer Handlers ---

def handle_generic_file_upload(file_obj: Optional[Any]):
    """
    汎用インポーターにファイルがアップロードされたときの処理。
    メタデータを抽出し、ヘッダーを自動検出してフォームに設定する。
    """
    if file_obj is None:
        return gr.update(visible=False), "", "", "", ""

    # 複数ファイル(list)の場合は先頭のファイルを使ってメタデータを推定する
    target_file = file_obj[0] if isinstance(file_obj, list) else file_obj

    try:
        # メタデータ抽出（変更なし）
        metadata = generic_importer.parse_metadata_from_file(target_file.name)

        # --- [新ロジック] ヘッダー自動検出 ---
        user_header = "## USER:"
        agent_header = "## AGENT:"

        try:
            with open(target_file.name, "r", encoding="utf-8", errors='ignore') as f:
                # ファイルの先頭部分だけ読んで効率的にチェック
                content_head = f.read(4096)

            # JSONファイルの場合 (例: ChatGPT Exporter)
            if target_file.name.endswith(".json"):
                # "role": "user" や "author": {"role": "user"} のような一般的なパターンをチェック
                # ここではより具体的なChatGPT Exporterの形式を仮定
                if '"role": "Prompt"' in content_head and '"role": "Response"' in content_head:
                    user_header = "role:Prompt"
                    agent_header = "role:Response"
                elif '"from": "human"' in content_head and '"from": "gpt"' in content_head:
                    user_header = "from:human"
                    agent_header = "from:gpt"

            # テキスト/マークダウンファイルの場合
            elif target_file.name.endswith((".md", ".txt")):
                if "## Prompt:" in content_head and "## Response:" in content_head:
                    user_header = "## Prompt:"
                    agent_header = "## Response:"
                elif "Human:" in content_head and "Assistant:" in content_head:
                    user_header = "Human:"
                    agent_header = "Assistant:"

        except Exception as e:
            print(f"Header auto-detection failed: {e}")

        # タイトルはファイル名ベース、複数は "(+N files)" を付ける
        default_title = metadata.get("title", os.path.basename(target_file.name))
        if isinstance(file_obj, list) and len(file_obj) > 1:
            default_title += f" (+{len(file_obj)-1} files)"

        return (
            gr.update(visible=True),
            default_title,
            metadata.get("user", "ユーザー"),
            user_header,
            agent_header
        )
    except Exception as e:
        gr.Warning("ファイルの解析中にエラーが発生しました。手動で情報を入力してください。")
        print(f"Error parsing metadata: {e}")
        return (
            gr.update(visible=True),
            os.path.basename(file_obj.name),
            "ユーザー",
            "## USER:",
            "## AGENT:"
        )

def handle_generic_import_button_click(
    file_obj: Optional[Any], room_name: str, user_display_name: str, user_header: str, agent_header: str
) -> Tuple[gr.update, gr.update, gr.update, gr.update, gr.update, gr.update]:
    """
    汎用インポートボタンがクリックされたときの処理。
    """
    if not all([file_obj, room_name, user_display_name, user_header, agent_header]):
        gr.Warning("すべてのフィールドを入力してください。")
        return tuple(gr.update() for _ in range(6))

    try:
        # ファイルパスのリストを作成
        file_paths = []
        if isinstance(file_obj, list):
            file_paths = [f.name for f in file_obj]
        else:
            file_paths = [file_obj.name]

        # --- [新ロジック] エラーコードに対応したUI通知 ---
        result = generic_importer.import_from_generic_text(
            file_paths=file_paths,
            room_name=room_name,
            user_display_name=user_display_name,
            user_header=user_header,
            agent_header=agent_header
        )

        if result and not result.startswith("ERROR:"):
            gr.Info(f"会話「{room_name}」のインポートに成功しました。")
            updated_room_list = room_manager.get_room_list_for_ui()
            reset_file = gr.update(value=None)
            hide_form = gr.update(visible=False)
            dd_update = gr.update(choices=updated_room_list, value=result)
            return reset_file, hide_form, dd_update, dd_update, dd_update, dd_update
        else:
            # エラーコードに応じたメッセージを表示
            if result == "ERROR: NO_HEADERS":
                gr.Warning("指定された話者ヘッダーがファイル内で見つかりませんでした。入力内容を確認してください。")
            elif result == "ERROR: NO_MESSAGES":
                gr.Warning("ファイルから有効なメッセージを抽出できませんでした。ファイル形式やヘッダーを確認してください。")
            else:
                gr.Error("汎用インポート処理中にエラーが発生しました。詳細はターミナルを確認してください。")
            return tuple(gr.update() for _ in range(6))
    except Exception as e:
        gr.Error(f"汎用インポート処理中に予期せぬエラーが発生しました。")
        print(f"Error during generic import button click: {e}")
        traceback.print_exc()
        return tuple(gr.update() for _ in range(6))

#
# --- Claude Importer Handlers ---
#

def handle_claude_file_upload(file_obj: Optional[Any]) -> Tuple[gr.update, gr.update, list]:
    """
    Claudeのconversations.jsonファイルがアップロードされたときの処理。
    """
    if file_obj == None:
        return gr.update(), gr.update(visible=False), []

    try:
        choices = claude_importer.get_claude_thread_list(file_obj.name)

        if not choices:
            gr.Warning("これは有効なClaudeエクスポートファイルではないか、会話が含まれていません。")
            return gr.update(), gr.update(visible=False), []

        # UIを更新し、選択肢リストをStateに渡す
        return gr.update(choices=choices, value=None), gr.update(visible=True), choices

    except Exception as e:
        gr.Warning("Claudeエクスポートファイルの処理中にエラーが発生しました。")
        print(f"Error processing Claude export file: {e}")
        traceback.print_exc()
        return gr.update(), gr.update(visible=False), []

def handle_claude_thread_selection(choices_list: list, selected_ids: list) -> gr.update:
    """
    Claudeの会話スレッドが選択されたとき、そのタイトルをルーム名テキストボックスにコピーする。
    multiselect=Trueに対応し、最後に選択された（リストの最後の）スレッドのタイトルを使用する。
    """
    if not selected_ids:
        return gr.update()

    # 最後に選択されたIDを取得 (Gradioのmultiselect listは値のリスト)
    target_id = selected_ids[-1] if isinstance(selected_ids, list) else selected_ids

    for name, uuid in choices_list:
        if uuid == target_id:
            return gr.update(value=name)
    return gr.update()

def handle_claude_import_button_click(
    file_obj: Optional[Any],
    conversation_uuids: Union[str, List[str]], # multiselect対応
    room_name: str,
    user_display_name: str
) -> Tuple[gr.update, gr.update, gr.update, gr.update, gr.update, gr.update]:
    """
    Claudeインポートボタンがクリックされたときの処理。
    """
    if not all([file_obj, conversation_uuids, room_name]):
        gr.Warning("ファイル、会話スレッド、新しいルーム名はすべて必須です。")
        return tuple(gr.update() for _ in range(6))

    try:
        safe_folder_name = claude_importer.import_from_claude_export(
            file_path=file_obj.name,
            conversation_uuids=conversation_uuids,
            room_name=room_name,
            user_display_name=user_display_name
        )

        if safe_folder_name:
            gr.Info(f"会話「{room_name}」のインポートに成功しました。")
            updated_room_list = room_manager.get_room_list_for_ui()
            reset_file = gr.update(value=None)
            hide_form = gr.update(visible=False, value=None)
            dd_update = gr.update(choices=updated_room_list, value=safe_folder_name)
            return reset_file, hide_form, dd_update, dd_update, dd_update, dd_update
        else:
            gr.Error("Claudeのインポート処理中にエラーが発生しました。詳細はターミナルを確認してください。")
            return tuple(gr.update() for _ in range(6))

    except Exception as e:
        gr.Error(f"Claudeのインポート処理中に予期せぬエラーが発生しました。")
        print(f"Error during Claude import button click: {e}")
        traceback.print_exc()
        return tuple(gr.update() for _ in range(6))

#
# --- ChatGPT Importer Handlers ---
#

def handle_chatgpt_file_upload(file_obj: Optional[Any]) -> Tuple[gr.update, gr.update, list]:
    """
    ChatGPTのjsonファイルがアップロードされたときの処理。
    ファイルをストリーミングで解析し、会話のリストを生成する。
    """
    # file_obj is a single FileData object when file_count="single"
    if file_obj is None:
        return gr.update(), gr.update(visible=False), []

    try:
        choices = []
        # JSONパスを解決 (ZIP対応)
        resolved_path = chatgpt_importer.resolve_conversations_file_path(file_obj.name)

        with open(resolved_path, 'rb') as f:
            # ijsonを使ってルートレベルの配列をストリーミング
            for conversation in ijson.items(f, 'item'):
                if conversation and 'mapping' in conversation and 'title' in conversation:
                    # 仕様通り、IDはmappingの最初のキー
                    convo_id = next(iter(conversation['mapping']), None)
                    title = conversation.get('title', 'No Title')
                    if convo_id and title:
                        choices.append((title, convo_id))

        if not choices:
            gr.Warning("これは有効なChatGPTエクスポートファイルではないようです。ファイルを確認してください。")
            return gr.update(), gr.update(visible=False), []

        sorted_choices = sorted(choices)
        # ドロップダウンを更新し、フォームを表示し、選択肢リストをStateに渡す
        return gr.update(choices=sorted_choices, value=None), gr.update(visible=True), sorted_choices

    except (ijson.JSONError, IOError, StopIteration, Exception) as e:
        gr.Warning("これは有効なChatGPTエクスポートファイルではないようです。ファイルを確認してください。")
        print(f"Error processing ChatGPT export file: {e}")
        traceback.print_exc()
        return gr.update(), gr.update(visible=False), []


def handle_chatgpt_thread_selection(choices_list: list, selected_ids: list) -> gr.update:
    """
    会話スレッドが選択されたとき、そのタイトルをルーム名テキストボックスにコピーする。
    multiselect=Trueに対応し、最後に選択された（リストの最後の）スレッドのタイトルを使用する。
    """
    try:
        if not selected_ids:
            return gr.update()

        # 最後に選択されたIDを取得 (multiselectのリスト順序は選択順とは限らないが、Gradioの仕様による)
        # ここではリストの最後の要素を「主」として扱う
        target_id = selected_ids[-1]

        # choices_listの中から、IDが一致するもののタイトルを探す
        for title, convo_id in choices_list:
            if convo_id == target_id:
                return gr.update(value=title)

    except Exception as e:
        print(f"[WARNING] handle_chatgpt_thread_selection failed: {e}")
        return gr.update()

    return gr.update() # 見つからなかった場合は何もしない


def handle_chatgpt_import_button_click(
    file_obj: Optional[Any],
    conversation_id: Union[str, List[str]],
    room_name: str,
    user_display_name: str
) -> Tuple[gr.update, gr.update, gr.update, gr.update, gr.update, gr.update]:
    """
    「インポート」ボタンがクリックされたときの処理。
    コアロジックを呼び出し、結果に応じてUIを更新する。
    """
    # 1. 入力検証
    if not all([file_obj, conversation_id, room_name]):
        gr.Warning("ファイル、会話スレッド、新しいルーム名はすべて必須です。")
        # 6つのコンポーネントを更新するので6つのupdateを返す
        return tuple(gr.update() for _ in range(6))

    try:
        # 2. コアロジックの呼び出し
        safe_folder_name = chatgpt_importer.import_from_chatgpt_export(
            file_path=file_obj.name,
            conversation_id=conversation_id,
            room_name=room_name,
            user_display_name=user_display_name
        )

        # 3. 結果に応じたUI更新
        if safe_folder_name:
            gr.Info(f"会話「{room_name}」のインポートに成功しました。")

            # UIのドロップダウンを更新するために最新のルームリストを取得
            updated_room_list = room_manager.get_room_list_for_ui()

            # フォームをリセットし、非表示にする
            reset_file = gr.update(value=None)
            hide_form = gr.update(visible=False, value=None) # Dropdownのchoicesもリセット

            # 各ドロップダウンを更新し、新しく作ったルームを選択状態にする
            dd_update = gr.update(choices=updated_room_list, value=safe_folder_name)

            # file, form, room_dd, manage_dd, alarm_dd, timer_dd
            return reset_file, hide_form, dd_update, dd_update, dd_update, dd_update
        else:
            gr.Error("インポート処理中に予期せぬエラーが発生しました。詳細はターミナルを確認してください。")
            return tuple(gr.update() for _ in range(6))

    except Exception as e:
        gr.Error(f"インポート処理中に予期せぬエラーが発生しました。詳細はターミナルを確認してください。")
        print(f"Error during import button click: {e}")
        traceback.print_exc()
        return tuple(gr.update() for _ in range(6))


def _get_display_history_count(api_history_limit_value: str) -> int: return int(api_history_limit_value) if api_history_limit_value.isdigit() else constants.UI_HISTORY_MAX_LIMIT

def handle_chatbot_selection(room_name: str, api_history_limit_state: str, mapping_list: list, translation_cache: dict, show_translation: bool, last_selected_index: Optional[int], evt: gr.SelectData):
    if not room_name or evt.index is None or not mapping_list:
        return None, gr.update(visible=False), gr.update(interactive=True), gr.update(interactive=False), None

    try:
        clicked_ui_index = _chatbot_event_message_index(evt.index)
        if clicked_ui_index is None:
            return None, gr.update(visible=False), gr.update(interactive=True), gr.update(interactive=False), None
        if not (0 <= clicked_ui_index < len(mapping_list)):
            gr.Warning(f"クリックされたメッセージを特定できませんでした (UI index out of bounds).")
            return None, gr.update(visible=False), gr.update(interactive=True), gr.update(interactive=False), None

        # マッピングリストから、ログ全体における「絶対インデックス」を取得
        original_log_index = mapping_list[clicked_ui_index]

        # [Guard] 同一メッセージの重複選択を防止（Gradioの連打対策）
        if last_selected_index == original_log_index:
            # 表示状態は維持するが、重い処理（パース等）をスキップ
            return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

        # ルームディレクトリの特定
        room_dir, _, _, _, _, _, _ = get_room_files_paths(room_name)
        if room_dir and os.path.isfile(room_dir):
            room_dir = os.path.dirname(os.path.dirname(room_dir)) # logs/YYYY-MM.txt -> room_dir

        # 【最適化】全ログをロードせず、ピンポイントでメッセージを取得
        selected_msg = utils.get_message_by_absolute_index(room_dir, original_log_index)

        if selected_msg:
            is_ai_message = selected_msg.get("responder") != "user"

            # 思考ログが含まれているか判定
            content = selected_msg.get("content", "")
            thought_blocks = _parse_thought_blocks(content)
            has_thought = len(thought_blocks) > 0

            # 既に翻訳済み、かつ翻訳表示モードか
            is_currently_translated = (translation_cache is not None and original_log_index in translation_cache) and show_translation
            btn_label = "🌐 原文に戻す" if is_currently_translated else "🌐 翻訳"

            # デバッグログは最小限に
            # if constants.DEBUG_MODE:
            #     print(f"--- [ChatSelection] UI:{clicked_ui_index} -> Abs:{original_log_index} (AI:{is_ai_message}, Thought:{has_thought}) ---")

            return (
                selected_msg,
                gr.update(visible=True),
                gr.update(interactive=is_ai_message),
                gr.update(interactive=has_thought, value=btn_label),
                original_log_index
            )
        else:
            # "out of bounds" または ファイル読み込み失敗
            return None, gr.update(visible=False), gr.update(interactive=True), gr.update(interactive=False), None

    except Exception as e:
        print(f"チャットボット選択中のエラー: {e}"); traceback.print_exc()
    return None, gr.update(visible=False), gr.update(interactive=True), gr.update(interactive=False), None

def _parse_thought_blocks(content: str) -> List[str]:
    """
    コンテンツから思考ログブロックを抽出し、リストとして返すヘルパー関数。
    format_history_for_gradio の表示ロジックと整合性を保つ。
    """
    if not content:
        return []

    return [
        part.get("content", "")
        for part in _parse_log_content_parts(content, remove_thoughts=False)
        if part.get("type") == "thought" and part.get("content", "").strip()
    ]

def _visible_text_from_llm_content(raw_content: Any) -> str:
    if raw_content is None:
        return ""
    if isinstance(raw_content, list):
        visible_parts: List[str] = []
        for part in raw_content:
            if isinstance(part, dict):
                part_type = str(part.get("type") or "").casefold()
                if part_type in {"thinking", "thought"} or part.get("thought") is True:
                    continue
                if "thinking" in part and not (part.get("text") or part.get("content")):
                    continue
                text = part.get("text") if "text" in part else part.get("content")
                if text:
                    visible_parts.append(str(text))
            elif getattr(part, "thought", False):
                continue
            elif hasattr(part, "text"):
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    visible_parts.append(text)
            elif isinstance(part, str):
                visible_parts.append(part)
        return utils.remove_thoughts_from_text("\n".join(visible_parts)).strip()
    if isinstance(raw_content, dict):
        if raw_content.get("thought") is True:
            return ""
        if "thinking" in raw_content and not (raw_content.get("text") or raw_content.get("content")):
            return ""
        text = raw_content.get("text") or raw_content.get("content") or ""
        return utils.remove_thoughts_from_text(str(text)).strip()
    return utils.remove_thoughts_from_text(str(raw_content)).strip()

def _visible_response_text(message: Any) -> str:
    raw_content = getattr(message, "content", message)
    if isinstance(raw_content, (list, dict)):
        return _visible_text_from_llm_content(raw_content)

    content = utils.get_content_as_string(message)
    parts = _parse_log_content_parts(
        content,
        remove_thoughts=False,
        normalize_persona_text=False,
    )
    visible_parts = [
        part.get("content", "")
        for part in parts
        if part.get("type") == "text" and part.get("content", "").strip()
    ]
    if visible_parts:
        return "\n".join(visible_parts).strip()
    return utils.remove_thoughts_from_text(content).strip()

def _select_best_ai_message(messages: List[AIMessage]) -> Optional[AIMessage]:
    candidates = []
    for msg in messages:
        raw_content = utils.get_content_as_string(msg) or ""
        if not raw_content.strip():
            message_content = getattr(msg, "content", None)
            if message_content:
                raw_content = str(message_content)
        if not raw_content.strip():
            continue
        visible_content = _visible_response_text(msg)
        candidates.append((len(raw_content), len(visible_content), msg))

    if not candidates:
        return None

    error_msgs = [
        msg for _, _, msg in candidates
        if "[Error:" in (utils.get_content_as_string(msg) or "")
        or "[エラー:" in (utils.get_content_as_string(msg) or "")
    ]
    if error_msgs:
        return error_msgs[0]

    visible_candidates = [candidate for candidate in candidates if candidate[1] > 0]
    if visible_candidates:
        visible_candidates.sort(key=lambda item: item[1], reverse=True)
        return visible_candidates[0][2]

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][2]

_TIMESTAMP_SUFFIX_RE = re.compile(
    r"((?:\n\n)?\d{4}-\d{2}-\d{2}\s*\([A-Za-z月火水木金土日]{1,3}\)\s*\d{2}:\d{2}:\d{2}(?:\s*\|\s*.*)?$)"
)

_PAIRED_THOUGHT_RE = re.compile(
    r"(^[ \t]*\[\s*THOUGHTS?\s*\][ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*\[/\s*THOUGHTS?\s*\][ \t]*$"
    r"|^[ \t]*【\s*Thoughts?\s*】[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*【/\s*Thoughts?\s*】[ \t]*$"
    r"|^[ \t]*<thinking>[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*</thinking>[ \t]*$)",
    re.IGNORECASE | re.MULTILINE,
)
_THOUGHT_BOUNDARY_TAG_RE = re.compile(
    r"\[/?\s*THOUGHTS?\s*\]|【/?\s*Thoughts?\s*】|</?\s*thinking\s*>",
    re.IGNORECASE,
)

def _split_timestamp_suffix(content: str) -> Tuple[str, str]:
    if not content:
        return "", ""
    match = _TIMESTAMP_SUFFIX_RE.search(content)
    if not match:
        return content, ""
    return content[:match.start()], match.group(1)

def _split_thought_colon_segments(segment: str) -> List[Dict[str, str]]:
    parts: List[Dict[str, str]] = []
    text_lines: List[str] = []
    thought_lines: List[str] = []

    def flush_text() -> None:
        nonlocal text_lines
        text = "\n".join(text_lines).strip()
        if text:
            parts.append({"type": "text", "content": text})
        text_lines = []

    def flush_thought() -> None:
        nonlocal thought_lines
        thought = "\n".join(thought_lines).strip()
        if thought:
            parts.append({"type": "thought", "content": thought, "style": "colon"})
        thought_lines = []

    for line in segment.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("THOUGHT:"):
            flush_text()
            thought_lines.append(stripped.split(":", 1)[1].strip())
        else:
            flush_thought()
            line_without_orphan_tag = _THOUGHT_BOUNDARY_TAG_RE.sub("", line)
            text_lines.append(line_without_orphan_tag)

    flush_thought()
    flush_text()
    return parts

def _clean_persona_text_preserving_blank_lines(text: str, remove_thoughts: bool = True) -> str:
    """
    表示用のメタタグだけを除去し、本文中の連続改行はユーザー/AIの意図として保持する。
    """
    if not text:
        return ""

    if remove_thoughts:
        text = utils.remove_thoughts_from_text(text)

    text = re.sub(r"【表情】…\w+…", "", text)
    text = re.sub(r"<persona_emotion\s+[^>]*/>", "", text)
    text = re.sub(r"<memory_trace\s+[^>]*/>", "", text)
    text = re.sub(r"<[^>]+/>", "", text)
    return text.strip()

def _parse_log_content_parts(
    content: str,
    remove_thoughts: bool = False,
    normalize_persona_text: bool = True,
) -> List[Dict[str, str]]:
    """
    ログ本文を通常本文と思考ログに分解する。
    Gradio 6のChatbot metadata表示と直接編集の保存処理で同じ分解結果を使う。
    """
    if not content:
        return []

    if normalize_persona_text:
        content = _clean_persona_text_preserving_blank_lines(content, remove_thoughts=remove_thoughts)
    if remove_thoughts:
        return [{"type": "text", "content": content}] if content.strip() else []

    parts: List[Dict[str, str]] = []
    cursor = 0
    for match in _PAIRED_THOUGHT_RE.finditer(content):
        prefix = content[cursor:match.start()]
        parts.extend(_split_thought_colon_segments(prefix))

        full_match = match.group(1)
        if re.match(r"\s*\[\s*THOUGHTS?\s*\]", full_match, flags=re.IGNORECASE):
            style = "new"
            thought = match.group(2)
        elif re.match(r"\s*【\s*Thoughts?\s*】", full_match, flags=re.IGNORECASE):
            style = "legacy"
            thought = match.group(3)
        else:
            style = "xml"
            thought = match.group(4)
        if thought and thought.strip():
            parts.append({"type": "thought", "content": thought.strip(), "style": style})
        cursor = match.end()

    parts.extend(_split_thought_colon_segments(content[cursor:]))
    return parts

def _serialize_log_content_parts(parts: List[Dict[str, str]], timestamp_suffix: str = "") -> str:
    rendered: List[str] = []
    for part in parts:
        content = (part.get("content") or "").strip()
        if not content:
            continue
        if part.get("type") == "thought":
            style = part.get("style") or "new"
            if style == "legacy":
                rendered.append(f"【Thoughts】\n{content}\n【/Thoughts】")
            elif style == "xml":
                rendered.append(f"<thinking>\n{content}\n</thinking>")
            elif style == "colon":
                rendered.append("\n".join(f"THOUGHT: {line}" for line in content.splitlines()))
            else:
                rendered.append(f"[THOUGHT]\n{content}\n[/THOUGHT]")
        else:
            rendered.append(content)
    return "\n\n".join(rendered).strip() + (timestamp_suffix or "")

def _strip_chat_speaker_prefix(text: str) -> str:
    if not text:
        return ""
    lines = str(text).splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines:
        first = lines[0].strip()
        if first.startswith("**") and ":" in first:
            lines = lines[1:]
    return "\n".join(lines).strip()

def _strip_chat_edit_display_chrome(text: str) -> str:
    text = _strip_chat_speaker_prefix(text)
    text, _ = _split_timestamp_suffix(text)
    return text.strip()

def _is_thought_chat_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata") or {}
    if not isinstance(metadata, dict):
        return False
    return bool(str(metadata.get("title", "")).startswith("思考ログ"))

def _thought_index_for_ui_message(chatbot_value: list, mapping_list: list, ui_index: int, log_index: int) -> int:
    thought_index = -1
    for idx in range(0, min(ui_index + 1, len(chatbot_value), len(mapping_list))):
        if mapping_list[idx] == log_index and _is_thought_chat_message(chatbot_value[idx]):
            thought_index += 1
    return thought_index

def _normalize_chat_edit_text(value: Any, fallback_message: Any = None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value.get("text", "")
        if isinstance(value.get("content"), str):
            return value.get("content", "")
    if isinstance(value, list):
        text_parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                text_parts.append(item.get("text", ""))
            elif isinstance(item, dict) and isinstance(item.get("content"), str):
                text_parts.append(item.get("content", ""))
        if text_parts:
            return "\n".join(text_parts)
    fallback_content = _chatbot_content(fallback_message)
    return fallback_content if isinstance(fallback_content, str) else ""

def handle_translate_thought(
    abs_index: Optional[int],
    room_name: str,
    api_history_limit: str,
    add_timestamp: bool,
    screenshot_mode: bool,
    redaction_rules: list,
    display_thoughts: bool,
    translation_cache: dict,
    show_translation: bool,
    mapping_list: list,
    current_log_map: dict = None
):
    """思考ログの翻訳処理ハンドラ。"""
    if abs_index is None or not room_name:
        return gr.update(), mapping_list, translation_cache, show_translation, gr.update()

    # マッピングがある場合は、表示上のインデックスから実ログのインデックス（abs_index）への変換を確認
    # ただし今回は引数として既に abs_index (selected_message_index_state) が渡されている想定
    # もし current_log_map が渡されており、abs_index が表示用IDの場合は変換が必要だが、
    # 呼び出し元 (nexus_ark.py) では selected_message_index_state (実インデックス) を渡している。

    # 1. 既にキャッシュがある場合はトグル（表示/非表示の切り替え）
    if translation_cache is None:
        translation_cache = {}

    if abs_index in translation_cache:
        new_show_translation = not show_translation
        btn_label = "🌐 原文に戻す" if new_show_translation else "🌐 翻訳"

        history, new_mapping = reload_chat_log(
            room_name, api_history_limit, add_timestamp,
            display_thoughts, screenshot_mode, redaction_rules,
            translation_cache, new_show_translation,
            force_open_index=abs_index
        )
        return history, new_mapping, translation_cache, new_show_translation, gr.update(value=btn_label)

    # 2. キャッシュがない場合は翻訳を実行
    try:
        log_f, _, _, _, _, _, _ = get_room_files_paths(room_name)
        raw_history = utils.load_chat_log(log_f)

        if not (0 <= abs_index < len(raw_history)):
            gr.Warning(f"対象のメッセージが見つかりませんでした (Index:{abs_index}, Total:{len(raw_history)})")
            return gr.update(), mapping_list, translation_cache, show_translation, gr.update()

        msg = raw_history[abs_index]
        print(f"--- [DEBUG:Translate] Translating message at Abs_Idx:{abs_index} ---")
        print(f"    Content Preview: {msg.get('content', '')[:100].replace(chr(10), ' ')}")
        content = msg.get("content", "")

        # 思考ログ部分の抽出 (ヘルパー関数使用)
        thought_texts = _parse_thought_blocks(content)

        if not thought_texts:
            gr.Warning("翻訳対象の思考ログが見つかりませんでした。")
            return gr.update(), mapping_list, translation_cache, show_translation, gr.update()

        # 翻訳の実行
        # ルーム設定からエージェント名を取得（口調反映のため）
        room_config = room_manager.get_room_config(room_name) or {}
        agent_name = room_config.get("agent_display_name") or room_config.get("agent_name") or "このキャラクター"

        translated_texts = []

        # 複数の思考ログがある場合、それぞれ翻訳
        for thought_text in thought_texts:
            translated_part = gemini_api.translate_thought_log_with_ai(thought_text, agent_name)
            if translated_part:
                translated_texts.append(translated_part)
            else:
                # 翻訳に失敗した場合はそのままにするか、警告を入れる
                translated_texts.append(thought_text)

        # キャッシュに追加（リストとして保存）
        new_cache = translation_cache.copy()
        new_cache[abs_index] = translated_texts

        # 翻訳表示を強制的にONにする
        new_show_translation = True

        history, new_mapping = reload_chat_log(
            room_name, api_history_limit, add_timestamp,
            display_thoughts, screenshot_mode, redaction_rules,
            new_cache, new_show_translation,
            force_open_index=abs_index
        )

        return history, new_mapping, new_cache, new_show_translation, gr.update(value="🌐 原文に戻す")

    except Exception as e:
        print(f"翻訳エラー: {e}")
        traceback.print_exc()
        gr.Error(f"翻訳中にエラーが発生しました: {e}")
        return gr.update(), mapping_list, translation_cache, show_translation, gr.update()

def handle_delete_button_click(
    confirmed: str,
    message_to_delete: Optional[Dict[str, str]],
    room_name: str,
    api_history_limit: str,
    add_timestamp: bool,
    screenshot_mode: bool,
    redaction_rules: list,
    display_thoughts: bool,
    # [v22] 翻訳不整合対策
    abs_index: Optional[int] = None,
    translation_cache: dict = None
    ):
    # ▼▼▼【ここから下のブロックを書き換え】▼▼▼
    if str(confirmed).lower() != 'true' or not message_to_delete:
        # ユーザーがキャンセルしたか、対象メッセージがない場合は選択状態を解除してボタンを非表示にする
        return gr.update(), gr.update(), None, gr.update(visible=False), "", None, translation_cache
    # ▲▲▲【書き換えここまで】▲▲▲

    log_f, _, _, _, _, _, _ = get_room_files_paths(room_name)
    deleted_timestamp = utils.delete_message_from_log(log_f, message_to_delete, abs_index=abs_index)
    if deleted_timestamp:
        gr.Info("ログからメッセージを削除しました。")
        # [SessionArousal] 対応するArousalデータも削除
        if message_to_delete.get("role") in ("AGENT", "SYSTEM"):
            import session_arousal_manager
            today_str = datetime.datetime.now().strftime('%Y-%m-%d')
            session_arousal_manager.remove_arousal_session(room_name, today_str, deleted_timestamp)
    else:
        gr.Error("メッセージの削除に失敗しました。詳細はターミナルを確認してください。")

    # [v22] 翻訳キャッシュの不整合防止：削除されたインデックス以降をシフト
    if abs_index is not None and translation_cache:
        new_cache = {}
        for idx, val in translation_cache.items():
            if idx < abs_index:
                new_cache[idx] = val
            elif idx > abs_index:
                new_cache[idx - 1] = val
        translation_cache = new_cache
        print(f"--- [DEBUG:Delete] Translation cache shifted due to deletion at index {abs_index} ---")

    effective_settings = config_manager.get_effective_settings(room_name)
    add_timestamp = effective_settings.get("add_timestamp", False)
    history, mapping_list = reload_chat_log(
        room_name,
        api_history_limit,
        add_timestamp,
        display_thoughts,
        screenshot_mode,
        redaction_rules
    )
    return history, mapping_list, None, gr.update(visible=False), "", None, translation_cache

def format_history_for_gradio(
    messages: List[Dict[str, str]],
    current_room_folder: str,
    add_timestamp: bool,
    display_thoughts: bool = True,
    screenshot_mode: bool = False,
    redaction_rules: List[Dict] = None,
    absolute_start_index: int = 0,
    translation_cache: dict = None,
    show_translation: bool = False,
    force_open_index: Optional[int] = None
) -> Tuple[List[Dict[str, Any]], List[int]]:

    """
    (v27: Stable Thought Log with Backward Compatibility)
    ログ辞書のリストをGradio 6 Chatbotのmessages形式に変換する。
    新しい 'THOUGHT:' プレフィックス形式と、古い '【Thoughts】' ブロック形式の両方を
    正しく解釈して、同じスタイルで表示する後方互換性を持つパーサーを実装。
    """
    if not messages:
        return [], []

    gradio_history, mapping_list = [], []

    current_room_config = room_manager.get_room_config(current_room_folder) or {}
    user_display_name = current_room_config.get("user_display_name", "ユーザー")
    agent_name_cache = {}

    proto_history = []
    for i, msg in enumerate(messages, start=absolute_start_index):
        role, content = msg.get("role"), msg.get("content", "").strip()
        responder_id = msg.get("responder")
        if not responder_id: continue

        if not add_timestamp:
            content = utils.remove_ai_timestamp(content)

        text_part = re.sub(r"\[(?:Generated Image|ファイル添付|VIEW_IMAGE):.*?\]", "", content, flags=re.DOTALL).strip()
        media_matches = list(re.finditer(r"\[(?:Generated Image|ファイル添付|VIEW_IMAGE): ([^\]]+?)\]", content))

        if text_part or (role == "SYSTEM" and not media_matches):
            proto_history.append({"type": "text", "role": role, "responder": responder_id, "content": text_part, "log_index": i})

        seen_paths = set()
        for match in media_matches:
            path_str = match.group(1).strip()
            if not path_str:
                continue
            if path_str in seen_paths:
                continue
            seen_paths.add(path_str)

            path_obj = Path(path_str)
            is_allowed = False
            try:
                abs_path = path_obj.resolve()
                cwd = Path.cwd().resolve()
                temp_dir = Path(tempfile.gettempdir()).resolve()
                if abs_path.is_relative_to(cwd) or abs_path.is_relative_to(temp_dir):
                    is_allowed = True
            except (OSError, ValueError):
                try:
                    abs_path_str = str(path_obj.resolve())
                    cwd_str = str(Path.cwd().resolve())
                    temp_dir_str = str(Path(tempfile.gettempdir()).resolve())
                    if abs_path_str.startswith(cwd_str) or abs_path_str.startswith(temp_dir_str):
                        is_allowed = True
                except Exception:
                    pass

            try:
                is_file = path_obj.is_file()
            except Exception:
                is_file = False

            if is_file and is_allowed:
                proto_history.append({"type": "media", "role": role, "responder": responder_id, "path": path_str, "log_index": i})
            else:
                print(f"--- [警告] 無効または安全でない画像パスをスキップしました: {path_str} ---")

        if not text_part and not media_matches and role != "SYSTEM":
             proto_history.append({"type": "text", "role": role, "responder": responder_id, "content": "", "log_index": i})



    for item in proto_history:
        role, responder_id = item["role"], item["responder"]
        is_user = (role == "USER")

        if item["type"] == "text":
            speaker_name = ""
            content_to_parse = item['content'] # まずデフォルトとして元のコンテンツを設定

            if is_user:
                speaker_name = user_display_name
            elif role == "AGENT":
                if responder_id not in agent_name_cache:
                    agent_config = room_manager.get_room_config(responder_id) or {}
                    agent_name_cache[responder_id] = agent_config.get("agent_display_name") or agent_config.get("room_name", responder_id)
                speaker_name = agent_name_cache[responder_id]
            elif role == "SYSTEM":
                if responder_id.startswith("tool_result"):
                    # RAW_RESULT部分を除去したものを、パース対象のコンテンツとして上書き
                    content_to_parse = re.sub(r"\[RAW_RESULT\][\s\S]*?\[/RAW_RESULT\]", "", item['content'], flags=re.DOTALL).strip()
                    speaker_name = "tool_result" # 話者名として表示
                else:
                    # tool_result以外のSYSTEMメッセージは話者名なし
                    speaker_name = ""
            else: # 将来的な拡張のためのフォールバック
                speaker_name = responder_id

            if screenshot_mode and redaction_rules:
                if speaker_name:
                    speaker_name = config_manager.apply_redaction_rules_to_text(speaker_name, redaction_rules)
                content_to_parse = _apply_redaction_rules_to_display_content(content_to_parse, redaction_rules)

            speaker_prefix = f"**{speaker_name}:**\n\n" if speaker_name else (f"**{responder_id}:**\n\n" if role == "SYSTEM" else "")

            # --- [新ロジック v4: 汎用コードブロック対応パーサー] ---

            # display_thoughtsがFalseの場合は思考ログを除去し、Trueの場合はGradio 6
            # metadataの折りたたみ表示へ分離する。通常コードブロックは本文側に残す。
            parsed_parts = _parse_log_content_parts(
                content_to_parse,
                remove_thoughts=not display_thoughts,
                normalize_persona_text=not is_user,
            )
            if display_thoughts:
                thought_block_index = 0
                for parsed_part in parsed_parts:
                    if parsed_part.get("type") != "thought":
                        continue
                    thought_content = parsed_part.get("content", "")
                    summary_label = "思考ログ"
                    log_index = item.get("log_index")
                    if translation_cache and log_index in translation_cache and show_translation:
                        cached_data = translation_cache[log_index]
                        if isinstance(cached_data, list):
                            if thought_block_index < len(cached_data):
                                thought_content = cached_data[thought_block_index]
                                summary_label = "思考ログ (翻訳)"
                        elif isinstance(cached_data, str):
                            thought_content = cached_data
                            summary_label = "思考ログ (翻訳)"
                    thought_block_index += 1

                    metadata = {"title": summary_label}
                    if force_open_index is None or log_index != force_open_index:
                        metadata["status"] = "done"
                    gradio_history.append({
                        "role": "assistant",
                        "content": thought_content.strip(),
                        "metadata": metadata,
                    })
                    mapping_list.append(log_index)

            content_for_parsing = "\n\n".join(
                parsed_part.get("content", "")
                for parsed_part in parsed_parts
                if parsed_part.get("type") == "text" and parsed_part.get("content", "").strip()
            )

            # 統一されたコードブロック記法 ``` でテキストを分割
            code_block_pattern = re.compile(r"(```[\s\S]*?```)")
            parts = code_block_pattern.split(content_for_parsing)

            final_html_parts = [speaker_prefix]

            for part in parts:
                if not part or not part.strip(): continue
                if part.startswith("```"):
                    inner_content = part[3:-3].strip()

                    has_replacement_html = '<span style' in inner_content
                    if has_replacement_html:
                        # 文字置き換えのspanタグを含む場合、または思考ログの場合：
                        # spanタグを保持しつつ、残りをHTMLエスケープしてMarkdown解釈を防ぐ

                        span_pattern = re.compile(r'(<span style="[^"]*">[^<]*</span>)')
                        spans = span_pattern.findall(inner_content)
                        placeholder_map = {}
                        temp_content = inner_content
                        for i, span in enumerate(spans):
                            placeholder = f"__SPAN_PH_{i}__"
                            placeholder_map[placeholder] = span
                            temp_content = temp_content.replace(span, placeholder, 1)
                        # プレースホルダー以外をHTMLエスケープ
                        escaped_content = html.escape(temp_content)
                        # プレースホルダーを元のspanタグに戻す
                        for placeholder, span in placeholder_map.items():
                            escaped_content = escaped_content.replace(placeholder, span)
                        # 改行を<br>に置換
                        escaped_content = escaped_content.replace('\n', '<br>')
                        formatted_block = f'<div class="code_wrap"><pre><code>{escaped_content}</code></pre></div>'
                    else:
                        formatted_block = f"```\n{html.escape(inner_content)}\n```"
                    final_html_parts.append(formatted_block)
                else:
                    # ★レッスン24の適用★：通常テキストにHTMLが含まれる場合も同様の対処
                    if '<span style' in part:
                        # <span>タグを保持しつつ、他のテキストはHTMLエスケープ
                        span_pattern = re.compile(r'(<span style="[^"]*">[^<]*</span>)')
                        spans = span_pattern.findall(part)
                        temp_part = part
                        placeholder_map = {}
                        for i, span in enumerate(spans):
                            placeholder = f"__SPAN_PLACEHOLDER_{i}__"
                            placeholder_map[placeholder] = span
                            temp_part = temp_part.replace(span, placeholder, 1)
                        # プレースホルダー以外をHTMLエスケープ
                        escaped_part = html.escape(temp_part)
                        # プレースホルダーを元のspanタグに戻す
                        for placeholder, span in placeholder_map.items():
                            escaped_part = escaped_part.replace(placeholder, span)

                        # Markdownでの改行を維持するため、\n を "  \n" (2スペース+改行) に変換
                        # <div>で囲むとMarkdownが効かなくなるため、直接追加する
                        escaped_part = escaped_part.replace('\n', '  \n')
                        final_html_parts.append(escaped_part)
                    else:
                        final_html_parts.append(part)

            final_markdown = "\n\n".join(final_html_parts).strip()
            
            # 本文がなく、speaker_prefix（名前表示）だけが残っている場合は非表示にする
            if speaker_prefix and final_markdown == speaker_prefix.strip():
                final_markdown = ""
                
            if final_markdown:
                gradio_history.append({
                    "role": "user" if is_user else "assistant",
                    "content": final_markdown,
                })
                mapping_list.append(item["log_index"])

        elif item["type"] == "media":
            gradio_history.append({
                "role": "user" if is_user else "assistant",
                "content": {"path": item["path"], "alt_text": os.path.basename(item["path"])},
            })
            mapping_list.append(item["log_index"])


    return gradio_history, mapping_list


def reload_chat_log(
    room_name: Optional[str],
    api_history_limit_value: str,
    add_timestamp: bool,
    display_thoughts: bool = True,
    screenshot_mode: bool = False,
    redaction_rules: List[Dict] = None,
    translation_cache: dict = None,
    show_translation: bool = False,
    force_open_index: Optional[int] = None,
    request: gr.Request = None, # [2026-04-09] 引数追加
    *args, **kwargs
):
    """
    指定されたルームのチャット履歴を読み込み、Gradioが解釈可能な形式に整形して返す。
    """
    # [2026-04-09 FIX] 自己修復型ガード (識別可能な個別のセッションのみ対象)
    session_id = _get_session_id(request)
    if session_id != "default":
        init_room = _get_session_init_room(session_id)
        if init_room and room_name != init_room:
            print(f"--- [Session:{session_id}] [reload_chat_log] ルーム不整合を自己修正: {room_name} -> {init_room} ---")
            room_name = init_room # 強制的に正解に合わせる
        else:
            print(f"--- [Session:{session_id}] [reload_chat_log] チャット更新(room={room_name}) ---")
    else:
        # 内部処理(default)の場合は通知なしで続行
        pass

    if not room_name or room_name == "Default":
        return [], []

    log_f, _, _, _, _, _, _ = get_room_files_paths(room_name)
    # log_f が存在しなくても、logs/ ディレクトリがあれば過去ログを読めるようにする
    # (月またぎでまだ今月のログがない場合や、インポート直後など)
    if not log_f:
        return [], []

    # [Fix] Windows環境でのパス不整合対策: 全てのパス操作の前にバックスラッシュを正規化
    log_f = log_f.replace("\\", "/")

    # log_f (例: logs/2026-02.txt) が存在しなくても、
    # その親フォルダ (logs/) が存在すれば utils.load_chat_log_lazy に任せる。
    if not os.path.exists(log_f):
        logs_dir = os.path.dirname(log_f).replace("\\", "/")
        if not os.path.exists(logs_dir):
             # ディレクトリすらなければ本当にログがない
             return [], []

    # --- ▼▼▼ 読み込み最適化 (v28: Lazy Loading & Message Counts) ▼▼▼ ---
    # 全ログを読み込むのではなく、UIで要求された分だけを効率的に読み込む

    loaded_messages = []

    if api_history_limit_value == "today":
        # 「本日分」: エピソード記憶などの状況に応じてcutoff_dateを決定してロード
        # ただし、UI上の安全のため上限は設ける (例: 400件)
        from gemini_api import _get_effective_today_cutoff
        cutoff_date = _get_effective_today_cutoff(room_name)

        # 本日分は日付で区切るが、極端に多い場合はUI保護のため上限を適用
        # min_turnsは「最低でもこれだけは読み込む」設定。本日分が少なすぎる場合の保険
        limit_count = constants.UI_HISTORY_MAX_LIMIT

        loaded_messages, _, absolute_start_index = utils.load_chat_log_lazy(
            room_dir=os.path.dirname(log_f),
            limit=limit_count,
            min_turns=constants.MIN_TODAY_LOG_FALLBACK_TURNS * 2, # フォールバック用
            cutoff_date=cutoff_date,
            return_full_info=True
        )

    elif api_history_limit_value == "all" or api_history_limit_value == "全ログ":
        # 「全ログ」: 以前は無制限ロード→末尾スライスだったが、
        # 今後はバックエンドでも `UI_HISTORY_MAX_LIMIT` を上限としてロードする
        limit_count = constants.UI_HISTORY_MAX_LIMIT

        loaded_messages, _, absolute_start_index = utils.load_chat_log_lazy(
            room_dir=os.path.dirname(log_f),
            limit=limit_count,
            return_full_info=True
        )

    else:
        # 数値指定（メッセージ件数）
        # 例: "20", "50", "100" など
        try:
            limit_count = int(api_history_limit_value)
        except ValueError:
            limit_count = constants.UI_HISTORY_MAX_LIMIT # パース失敗時の安全策

        # limit_validator: SYSTEMロール（ツールログ等）は件数に含めない
        # これにより、大量のツールログがあっても「最新〇件」の会話が確実に表示される
        def _limit_validator(msg):
            return msg.get("role") != "SYSTEM"

        loaded_messages, _, absolute_start_index = utils.load_chat_log_lazy(
            room_dir=os.path.dirname(log_f),
            limit=limit_count,
            limit_validator=_limit_validator,
            return_full_info=True
        )

    # load_chat_log_lazy は (messages, has_more, start_index) を返す
    # 既に時系列順になっているため、単純にそのまま使用可能
    visible_history = loaded_messages
    # absolute_start_index は LazyLoad で取得した「ファイル全体におけるこのスライスの開始位置」

    # 注意: lazy loadeによる絶対インデックスのズレは、
    # 既存の「ログ削除」や「翻訳」機能でインデックス依存している箇所に影響する可能性がある。
    # しかし、現在の仕様では `log_index` は読み込まれたリスト内のインデックスとして扱われているため、
    # 表示中のリスト内での整合性が取れていれば動作するはずである。
    # （厳密なファイル全体の行番号が必要な場合は別途対応が必要だが、現状はメモリ上のリスト操作が主）

    # --- ▲▲▲ 修正ここまで ▲▲▲ ---

    history, mapping_list = format_history_for_gradio(
        messages=visible_history,
        current_room_folder=room_name,
        add_timestamp=add_timestamp,
        display_thoughts=display_thoughts,
        screenshot_mode=screenshot_mode,
        redaction_rules=redaction_rules,
        absolute_start_index=absolute_start_index,
        translation_cache=translation_cache,
        show_translation=show_translation,
        force_open_index=force_open_index
    )

    return history, mapping_list

def handle_wb_add_place_button_click(area_selector_value: Optional[str]):
    if not area_selector_value:
        gr.Warning("まず、場所を追加したいエリアを選択してください。")
        return "place", gr.update(visible=False), "#### 新しい場所の作成"
    return "place", gr.update(visible=True), "#### 新しい場所の作成"






# --- 主観的記憶（日記）：エントリベースのハンドラ（新規追加） ---






























# --- ワーキングメモリ（動的コンテキスト）関連 ---
# 注: _get_working_memory_path の実効定義は後方（「ワーキングメモリのハンドラ」節）にある。
#     ここにあった重複定義はシャドウされたデッドコードだったため削除した（2026-06-15）。









# --- [Goal Memory] Goals Display Handlers ---

def handle_refresh_goals(room_name: str):
    """目標（goals.json）を読み込んで表示用にフォーマットする"""
    if not room_name:
        return "", "", "ルームが選択されていません"

    try:
        from goal_manager import GoalManager
        gm = GoalManager(room_name)
        goals = gm._load_goals()

        # 短期目標のフォーマット
        short_term_text = ""
        for g in goals.get("short_term", []):
            status_emoji = "🔥" if g.get("status") == "active" else "✅"
            short_term_text += f"{status_emoji} {g.get('goal', '(不明)')}\n"
            short_term_text += f"   作成: {g.get('created_at', '-')}\n"
            if g.get("progress_notes"):
                for note in g["progress_notes"][-2:]:  # 最新2件のみ
                    short_term_text += f"   📝 {note}\n"
            short_term_text += "\n"

        if not short_term_text:
            short_term_text = "（短期目標はまだありません）"

        # 長期目標のフォーマット
        long_term_text = ""
        for g in goals.get("long_term", []):
            status_emoji = "🌟" if g.get("status") == "active" else "✅"
            long_term_text += f"{status_emoji} {g.get('goal', '(不明)')}\n"
            long_term_text += f"   作成: {g.get('created_at', '-')}\n"
            if g.get("related_values"):
                long_term_text += f"   価値観: {', '.join(g['related_values'])}\n"
            long_term_text += "\n"

        if not long_term_text:
            long_term_text = "（長期目標はまだありません）"

        # メタデータのフォーマット
        meta = goals.get("meta", {})
        level_names = {1: "日次", 2: "週次", 3: "月次"}
        last_level = meta.get("last_reflection_level", 0)
        meta_text = (
            f"最終省察レベル: {level_names.get(last_level, '未実行')} ({last_level})\n"
            f"週次省察: {meta.get('last_level2_date', '未実行')} / "
            f"月次省察: {meta.get('last_level3_date', '未実行')}"
        )

        return short_term_text.strip(), long_term_text.strip(), meta_text

    except Exception as e:
        print(f"Goal refresh error: {e}")
        traceback.print_exc()
        return "", "", f"エラー: {e}"

# --- [Project Morpheus] Dream Journal Handlers ---










# --- 📌 エンティティ記憶 (Entity Memory) ハンドラ ---















# --- [Phase 14] Episodic Memory Browser Handlers ---






# 古い handle_dream_journal_selection は Dropdown 移行に伴い廃止





# --- 創作ノートのハンドラ ---







# --- 創作ノート：エントリベースのハンドラ（新規追加） ---












# --- 研究・分析ノートのハンドラ ---






# --- Research Threads ---








# --- ワーキングメモリのハンドラ ---





# ▼▼▼ アクションメモリー用ハンドラ追加 ▼▼▼
# ▲▲▲ 追加ここまで ▲▲▲






# --- 研究ノート：エントリベースのハンドラ ---































def render_alarms_as_dataframe():
    alarms = sorted(alarm_manager.load_alarms(), key=lambda x: x.get("time", "")); all_rows = []
    for a in alarms:
        schedule_display = "単発"
        if a.get("date"):
            try:
                date_obj, today = datetime.datetime.strptime(a["date"], "%Y-%m-%d").date(), datetime.date.today()
                if date_obj == today: schedule_display = "今日"
                elif date_obj == today + datetime.timedelta(days=1): schedule_display = "明日"
                else: schedule_display = date_obj.strftime("%m/%d")
            except: schedule_display = "日付不定"
        elif a.get("days"): schedule_display = ",".join([DAY_MAP_EN_TO_JA.get(d.lower(), d.upper()) for d in a["days"]])
        all_rows.append({"ID": a.get("id"), "状態": a.get("enabled", False), "時刻": a.get("time"), "予定": schedule_display, "ルーム": a.get("character"), "内容": a.get("context_memo") or ""})
    return pd.DataFrame(all_rows, columns=["ID", "状態", "時刻", "予定", "ルーム", "内容"])

def get_display_df(df_with_id: pd.DataFrame):
    if df_with_id is None or df_with_id.empty: return pd.DataFrame(columns=["状態", "時刻", "予定", "ルーム", "内容"])
    return df_with_id[["状態", "時刻", "予定", "ルーム", "内容"]] if 'ID' in df_with_id.columns else df_with_id

def handle_alarm_selection(evt: gr.SelectData, df_with_id: pd.DataFrame) -> List[str]:
    if not hasattr(evt, 'index') or evt.index is None or df_with_id is None or df_with_id.empty:
        return []
    row_index = evt.index[0]
    if 0 <= row_index < len(df_with_id):
        selected_id = str(df_with_id.iloc[row_index]['ID'])
        return [selected_id]
    return []

def handle_alarm_selection_for_all_updates(evt: gr.SelectData, df_with_id: pd.DataFrame):
    selected_ids = handle_alarm_selection(evt, df_with_id)
    feedback_text = "アラームを選択してください" if not selected_ids else f"{len(selected_ids)} 件のアラームを選択中"

    all_rooms = room_manager.get_room_list_for_ui()
    default_room = all_rooms[0][1] if all_rooms else "Default"

    if len(selected_ids) == 1:
        alarm = next((a for a in alarm_manager.load_alarms() if a.get("id") == selected_ids[0]), None)
        if alarm:
            h, m = alarm.get("time", "08:00").split(":")
            # DAY_MAP_EN_TO_JA を直接使用
            days_ja = [DAY_MAP_EN_TO_JA.get(d.lower(), d.upper()) for d in alarm.get("days", [])]

            form_updates = (
                "アラーム更新", alarm.get("context_memo", ""), alarm.get("character", default_room),
                days_ja, alarm.get("is_emergency", False), h, m, selected_ids[0]
            )
            cancel_button_visibility = gr.update(visible=True)
        else:
            form_updates = ("アラーム追加", "", default_room, [], False, "08", "00", None)
            cancel_button_visibility = gr.update(visible=False)
    else:
        form_updates = ("アラーム追加", "", default_room, [], False, "08", "00", None)
        cancel_button_visibility = gr.update(visible=False)

    return (selected_ids, feedback_text) + form_updates + (cancel_button_visibility,)

def toggle_selected_alarms_status(selected_ids: list, target_status: bool):
    if not selected_ids: gr.Warning("状態を変更するアラームが選択されていません。")
    else:
        current_alarms = alarm_manager.load_alarms()
        modified = any(a.get("id") in selected_ids and a.update({"enabled": target_status}) is None for a in current_alarms)
        if modified:
            alarm_manager.alarms_data_global = current_alarms; alarm_manager.save_alarms()
            gr.Info(f"{len(selected_ids)}件のアラームの状態を「{'有効' if target_status else '無効'}」に変更しました。")
    new_df_with_ids = render_alarms_as_dataframe(); return new_df_with_ids, get_display_df(new_df_with_ids)

def handle_delete_alarms_and_update_ui(selected_ids: list):
    if not selected_ids:
        gr.Warning("削除するアラームが選択されていません。")
        df_with_ids = render_alarms_as_dataframe()
        return df_with_ids, get_display_df(df_with_ids), gr.update(), gr.update()

    deleted_count = 0
    for sid in selected_ids:
        if alarm_manager.delete_alarm(str(sid)):
            deleted_count += 1

    if deleted_count > 0:
        gr.Info(f"{deleted_count}件のアラームを削除しました。")

    new_df_with_ids = render_alarms_as_dataframe()
    display_df = get_display_df(new_df_with_ids)
    new_selected_ids = []
    feedback_text = "アラームを選択してください"
    return new_df_with_ids, display_df, new_selected_ids, feedback_text

def handle_cancel_alarm_edit():
    all_rooms = room_manager.get_room_list_for_ui()
    default_room = all_rooms[0][1] if all_rooms else "Default" # ← 戻り値の形式変更にも対応
    return (
        "アラーム追加", "", gr.update(choices=all_rooms, value=default_room),
        [], False, "08", "00", None, [], "アラームを選択してください",
        gr.update(visible=False)
    )

def handle_add_or_update_alarm(editing_id, h, m, room, context, days_ja, is_emergency):
    from tools.alarm_tools import set_personal_alarm
    context_memo = context.strip() if context and context.strip() else "時間になりました"
    days_en = [DAY_MAP_JA_TO_EN.get(d) for d in days_ja if d in DAY_MAP_JA_TO_EN]

    if editing_id:
        alarm_manager.delete_alarm(editing_id)
        gr.Info(f"アラームID:{editing_id} を更新しました。")
    else:
        gr.Info(f"新しいアラームを追加しました。")

    set_personal_alarm.func(time=f"{h}:{m}", context_memo=context_memo, room_name=room, days=days_en, date=None, is_emergency=is_emergency)

    new_df_with_ids = render_alarms_as_dataframe()
    all_rooms = room_manager.get_room_list_for_ui()
    default_room = all_rooms[0][1] if all_rooms else "Default" # ← 戻り値の形式変更にも対応

    return (
        new_df_with_ids, get_display_df(new_df_with_ids),
        "アラーム追加", "", gr.update(choices=all_rooms, value=default_room),
        [], False, "08", "00", None, [], "アラームを選択してください",
        gr.update(visible=False)
    )

def handle_timer_submission(timer_type, duration, work, brk, cycles, room, work_theme, brk_theme, api_key_name, normal_theme):
    if not room:
        return "エラー：通知先のルームを選択してください。"

    try:
        if timer_type == "通常タイマー":
            result_message = timer_tools.set_timer.func(
                duration_minutes=int(duration),
                theme=normal_theme or "時間になりました！",
                room_name=room
            )
            gr.Info("通常タイマーを設定しました。")
        elif timer_type == "ポモドーロタイマー":
            result_message = timer_tools.set_pomodoro_timer.func(
                work_minutes=int(work),
                break_minutes=int(brk),
                cycles=int(cycles),
                work_theme=work_theme or "休憩終了。作業を再開しましょう。",
                break_theme=brk_theme or "作業終了。休憩に入ってください。",
                room_name=room
            )
            gr.Info("ポモドーロタイマーを設定しました。")
        else:
            result_message = "エラー: 不明なタイマー種別です。"
        return result_message

    except Exception as e:
        traceback.print_exc()
        return f"タイマー開始エラー: {e}"

def handle_auto_memory_change(auto_memory_enabled: bool):
    config_manager.save_memos_config("auto_memory_enabled", auto_memory_enabled)
    status = "有効" if auto_memory_enabled else "無効"
    gr.Info(f"対話の自動記憶を「{status}」に設定しました。")

# --- [Phase 2] ROBLOX Webhook Handlers ---
def handle_regenerate_roblox_webhook_secret(room_name: str) -> str:
    """指定ルームのWebhook Secret Tokenを再生成して保存する"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return ""
    try:
        import secrets
        import config_manager

        # 32文字のランダムなHEX文字列を生成
        new_secret = secrets.token_hex(16)

        # 設定を更新して保存
        new_settings = {"roblox_webhook_secret": new_secret}
        room_manager.update_room_config(room_name, new_settings)


        gr.Info("Webhook Secret Tokenを再生成しました。ルアスクリプト側の設定も合わせて更新してください。")
        return gr.update(value=new_secret)
    except Exception as e:
        gr.Error(f"Token生成エラー: {e}")
        return gr.update()

def handle_refresh_roblox_webhook_logs() -> str:
    """Webhookサーバーから直近のイベントログを取得してテキスト化する"""
    try:
        from tools import roblox_webhook
        return roblox_webhook.get_recent_logs()
    except Exception as e:
        return f"ログの取得に失敗しました: {e}"
# ----------------------------------------

def handle_add_current_log_to_queue(room_name: str, console_content: str):
    """
    「現在の対話を記憶に追加」ボタンのイベントハンドラ。
    アクティブなログの新しい部分だけを対象に、記憶化処理を実行する。
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return

    gr.Info("現在の対話の新しい部分を、記憶に追加しています...")
    # この処理は比較的短時間で終わる想定なので、UIの無効化は行わない

    script_path = "memory_archivist.py"
    try:
        # 1. アクティブログの進捗ファイルパスを決定
        rag_data_path = Path(constants.ROOMS_DIR) / room_name / "rag_data"
        rag_data_path.mkdir(parents=True, exist_ok=True)
        active_log_progress_file = rag_data_path / "active_log_progress.json"

        # 2. ログ全体と、前回の進捗を読み込む
        log_file_path, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        full_log_content = Path(log_file_path).read_text(encoding='utf-8')

        last_processed_pos = 0
        if active_log_progress_file.exists():
            progress_data = json.loads(active_log_progress_file.read_text(encoding='utf-8'))
            last_processed_pos = progress_data.get("last_processed_position", 0)

        # 3. 新しい部分だけを抽出
        new_log_content = full_log_content[last_processed_pos:]
        if not new_log_content.strip():
            gr.Info("新しい会話が見つからなかったため、記憶の追加は行われませんでした。")
            return

        # 4. 新しい部分を一時ファイルに書き出す
        with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8', suffix='.txt') as temp_file:
            temp_file.write(new_log_content)
            temp_file_path = temp_file.name

        # 5. アーキビストをサブプロセスとして同期的に実行
        cmd = [sys.executable, "-u", script_path, "--room_name", room_name, "--source", "active_log", "--input_file", temp_file_path]

        # ここでは同期的に実行し、完了を待つ
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

        # ターミナルとデバッグコンソールにログを出力
        print(f"--- [Active Log Archiving Output for {room_name}] ---")
        print(proc.stdout)
        if proc.stderr:
            print("--- Stderr ---")
            print(proc.stderr)

        # 6. 一時ファイルを削除
        os.unlink(temp_file_path)

        if proc.returncode != 0:
            raise RuntimeError(f"{script_path} failed with return code {proc.returncode}. Check terminal for details.")

        # 7. 進捗を更新
        with open(active_log_progress_file, "w", encoding='utf-8') as f:
            json.dump({"last_processed_position": len(full_log_content)}, f)

        gr.Info("✅ 現在の対話の新しい部分を、記憶に追加しました！")

    except Exception as e:
        error_message = f"現在の対話の記憶追加中にエラーが発生しました: {e}"
        print(error_message)
        traceback.print_exc()
        gr.Error(error_message)




def handle_importer_stop(pid: int):
    """
    実行中のインポータープロセスを中断する。
    """
    if pid is None:
        gr.Warning("停止対象のプロセスが見つかりません。")
        return gr.update(interactive=True, value="知識グラフを構築/更新する"), gr.update(visible=False), None, gr.update(interactive=True)

    try:
        process = psutil.Process(pid)
        process.terminate()  # SIGTERMを送信
        gr.Info(f"インポート処理(PID: {pid})に停止信号を送信しました。")
    except psutil.NoSuchProcess:
        gr.Warning(f"プロセス(PID: {pid})は既に終了しています。")
    except Exception as e:
        gr.Error(f"プロセスの停止中にエラーが発生しました: {e}")
        traceback.print_exc()

    return (
        gr.update(interactive=True, value="知識グラフを構築/更新する"),
        gr.update(visible=False),
        None,
        gr.update(interactive=True)
    )



# --- Screenshot Redaction Rules Handlers ---

def handle_redaction_rule_select(rules_df: pd.DataFrame, evt: gr.SelectData) -> Tuple[Optional[int], str, str, str]:
    """DataFrameの行が選択されたときに、その内容を編集フォームに表示する。"""
    if not evt.index:
        # 選択が解除された場合
        return None, "", "", "#FFFF00"
    try:
        selected_index = evt.index[0]
        if rules_df is None or not (0 <= selected_index < len(rules_df)):
             return None, "", "", "#FFFF00"

        selected_row = rules_df.iloc[selected_index]
        find_text = selected_row.get("元の文字列 (Find)", "")
        replace_text = selected_row.get("置換後の文字列 (Replace)", "")
        color = selected_row.get("背景色", "#FFFF00")
        # 選択された行のインデックスを返す
        return selected_index, str(find_text), str(replace_text), str(color)
    except (IndexError, KeyError) as e:
        print(f"ルール選択エラー: {e}")
        return None, "", "", "#FFFF00"

def handle_add_or_update_redaction_rule(
    current_rules: List[Dict],
    selected_index: Optional[int],
    find_text: str,
    replace_text: str,
    color: str
) -> Tuple[pd.DataFrame, List[Dict], None, str, str, str]:
    """ルールを追加または更新し、ファイルに保存してUIを更新する。"""
    find_text = find_text.strip()
    replace_text = replace_text.strip()

    if not find_text:
        gr.Warning("「元の文字列」は必須です。")
        df = _create_redaction_df_from_rules(current_rules)
        return df, current_rules, selected_index, find_text, replace_text, color

    if current_rules is None:
        current_rules = []

    new_rule = {"find": find_text, "replace": replace_text, "color": color}

    # 更新モード
    if selected_index is not None and 0 <= selected_index < len(current_rules):
        # findの値が、自分以外のルールで既に使われていないかチェック
        for i, rule in enumerate(current_rules):
            if i != selected_index and rule["find"] == find_text:
                gr.Warning(f"ルール「{find_text}」は既に存在します。")
                df = _create_redaction_df_from_rules(current_rules)
                return df, current_rules, selected_index, find_text, replace_text, color
        current_rules[selected_index] = new_rule
        gr.Info(f"ルール「{find_text}」を更新しました。")
    # 新規追加モード
    else:
        if any(rule["find"] == find_text for rule in current_rules):
            gr.Warning(f"ルール「{find_text}」は既に存在します。更新する場合はリストから選択してください。")
            df = _create_redaction_df_from_rules(current_rules)
            return df, current_rules, selected_index, find_text, replace_text, color
        current_rules.append(new_rule)
        gr.Info(f"新しいルール「{find_text}」を追加しました。")

    config_manager.save_redaction_rules(current_rules)

    df_for_ui = _create_redaction_df_from_rules(current_rules)

    return df_for_ui, current_rules, None, "", "", "#62827e"

def handle_delete_redaction_rule(
    current_rules: List[Dict],
    selected_index: Optional[int]
) -> Tuple[pd.DataFrame, List[Dict], None, str, str, str]:
    """選択されたルールを削除する。"""
    if current_rules is None:
        current_rules = []

    if selected_index is None or not (0 <= selected_index < len(current_rules)):
        gr.Warning("削除するルールをリストから選択してください。")
        df = _create_redaction_df_from_rules(current_rules)
        return df, current_rules, None, "", "", "#62827e"

    # Pandasの.dropではなく、Pythonのdel文でリストの要素を直接削除する
    deleted_rule_name = current_rules[selected_index]["find"]
    del current_rules[selected_index]

    config_manager.save_redaction_rules(current_rules)
    gr.Info(f"ルール「{deleted_rule_name}」を削除しました。")

    df_for_ui = _create_redaction_df_from_rules(current_rules)

    # フォームと選択状態をリセット
    return df_for_ui, current_rules, None, "", "", "#62827e"


def update_model_state(model):
    if config_manager.save_config_if_changed("last_model", model):
        gr.Info(f"デフォルトAIモデルを「{model}」に設定しました。")
    return model

def update_api_key_state(api_key_name):
    """APIキー設定の更新"""
    # [2026-02-11 FIX] 表示用ラベルを除去
    api_key_name = config_manager._clean_api_key_name(api_key_name)
    if config_manager.save_config_if_changed("last_api_key_name", api_key_name):
        gr.Info(f"APIキーを '{api_key_name}' に設定しました。")
    return api_key_name

def update_api_history_limit_state_and_reload_chat(limit_ui_val: str, room_name: Optional[str], add_timestamp: bool, display_thoughts: bool, screenshot_mode: bool = False, redaction_rules: List[Dict] = None, is_switching_room: bool = False):
    key = _api_history_limit_key_from_ui(limit_ui_val)
    should_persist = key is not None
    if key is None:
        saved_key = config_manager.CONFIG_GLOBAL.get("last_api_history_limit_option")
        key = saved_key if saved_key in constants.API_HISTORY_LIMIT_OPTIONS else constants.DEFAULT_API_HISTORY_LIMIT_OPTION
    # ルーム切り替え中（一括リロード中）は、個別のDropdown変更による再読込を抑制する
    if is_switching_room:
        return key, gr.update(), gr.update()
    if should_persist:
        config_manager.save_config_if_changed("last_api_history_limit_option", key)
    # この関数はUIリロードが主目的なので、Info通知は不要
    history, mapping_list = reload_chat_log(room_name, key, add_timestamp, display_thoughts, screenshot_mode, redaction_rules)
    return key, history, mapping_list

def _resolve_tts_request_settings(
    room_name: str,
    api_key_name: str = None,
    provider_value: str = None,
    model_value: str = None,
    voice_value: str = None,
    style_prompt: str = None,
    voice_speed: float = None,
    voice_pitch: float = None,
    voice_intonation: float = None,
    voice_volume: float = None,
    profile_value: str = None,
) -> Dict[str, Any]:
    """UI/ルーム設定からTTS生成に必要なプロバイダ別設定を解決する。"""
    effective_settings = config_manager.get_effective_settings(room_name)
    provider = config_manager.tts_provider_key_from_display(provider_value or effective_settings.get("tts_provider", "gemini"))
    explicit_model = bool(model_value and str(model_value).strip())
    model = (model_value or effective_settings.get("tts_model") or "").strip()
    # カスタムモデル値（ユーザーが直接入力した値）は尊重してそのまま使う
    if not model:
        provider_models = config_manager.get_tts_model_choices(provider)
        model = provider_models[0] if provider_models else ""
    voice_source = voice_value or effective_settings.get("tts_voice") or effective_settings.get("voice_id", "iapetus")
    if not voice_value and provider != "gemini" and voice_source in config_manager.SUPPORTED_VOICES:
        voice_choices = config_manager.get_tts_voice_map(provider)
        voice_source = next(iter(voice_choices.keys()), voice_source)
    voice_id = config_manager.resolve_tts_voice_id(provider, voice_source) or str(voice_source)
    resolved_style = style_prompt if style_prompt is not None else effective_settings.get("tts_style_prompt", effective_settings.get("voice_style_prompt", ""))
    response_format = effective_settings.get("tts_response_format") or ("wav" if provider == "gemini" else "mp3")
    api_key = None
    base_url = None
    extra_body = None
    resolved_profile_name = None

    if provider == "gemini":
        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
        model = model or "gemini-3.1-flash-tts-preview"
        response_format = "wav"
    elif provider == "openai":
        openai_setting = (
            config_manager.get_openai_setting_by_name("OpenAI Official")
            or config_manager.get_openai_setting_by_name("OpenAI")
            or config_manager.get_active_openai_setting()
        )
        if openai_setting:
            resolved_profile_name = openai_setting.get("name")
            api_key = openai_setting.get("api_key")
            base_url = openai_setting.get("base_url") or None
        model = model or "gpt-4o-mini-tts"
        response_format = response_format or "mp3"
    elif provider == "openai_compatible":
        profile_name = profile_value or effective_settings.get("tts_profile_name")
        openai_setting = None
        if profile_name:
            openai_setting = config_manager.get_openai_setting_by_name(profile_name)
        if not openai_setting:
            openai_setting = config_manager.get_active_openai_setting()

        profile_kind = None
        if openai_setting:
            resolved_profile_name = openai_setting.get("name")
            api_key = openai_setting.get("api_key")
            base_url = openai_setting.get("base_url") or None
            profile_model_choices = config_manager.get_openai_compatible_tts_model_choices_for_profile(resolved_profile_name, base_url)
            profile_kind = config_manager.get_openai_profile_tts_kind(resolved_profile_name, base_url)
            if profile_model_choices:
                if not model or (profile_kind != "custom" and model not in profile_model_choices):
                    model = openai_setting.get("tts_model") or profile_model_choices[0]
                if profile_kind != "custom" and model not in profile_model_choices:
                    model = profile_model_choices[0]
            elif profile_kind == "no_tts":
                model = ""
            elif not model:
                model = openai_setting.get("tts_model") or openai_setting.get("default_model") or "canopylabs/orpheus-v1-english"
            extra_body = openai_setting.get("tts_extra_body")
            profile_voice_map = config_manager.get_openai_compatible_tts_voice_map_for_profile(resolved_profile_name, base_url, model)
            if profile_voice_map:
                if voice_source in profile_voice_map:
                    voice_id = str(voice_source)
                else:
                    voice_id = next((k for k, v in profile_voice_map.items() if v == voice_source), voice_id)
                if profile_kind != "custom" and voice_id not in profile_voice_map:
                    voice_id = next(iter(profile_voice_map.keys()), voice_id)
            elif profile_kind == "no_tts":
                voice_id = ""
        if profile_kind == "groq":
            response_format = "wav"
        else:
            response_format = "mp3" if response_format == "wav" else (response_format or "mp3")
    elif provider == "elevenlabs":
        api_key = config_manager.CONFIG_GLOBAL.get("elevenlabs_api_key")
        model = model or "eleven_flash_v2_5"
        response_format = "mp3" if response_format == "wav" else (response_format or "mp3")
    elif provider in {"aivisspeech", "voicevox", "coeiroink"}:
        api_key = "LOCAL_VOICEVOX_COMPATIBLE"
        model = model or (config_manager.get_tts_model_choices(provider)[0] if config_manager.get_tts_model_choices(provider) else "")
        response_format = "wav"

    res_speed = voice_speed if voice_speed is not None else effective_settings.get("tts_voice_speed")
    res_pitch = voice_pitch if voice_pitch is not None else effective_settings.get("tts_voice_pitch")
    res_intonation = voice_intonation if voice_intonation is not None else effective_settings.get("tts_voice_intonation")
    res_volume = voice_volume if voice_volume is not None else effective_settings.get("tts_voice_volume")

    if res_speed is None: res_speed = 1.0
    if res_pitch is None: res_pitch = 0.0
    if res_intonation is None: res_intonation = 1.0
    if res_volume is None: res_volume = 1.0

    return {
        "provider": provider,
        "model": model,
        "voice_id": voice_id,
        "style_prompt": resolved_style or "",
        "api_key": api_key,
        "api_key_name": api_key_name if provider == "gemini" else None,
        "profile_name": resolved_profile_name,
        "base_url": base_url,
        "response_format": response_format,
        "extra_body": extra_body,
        "speedScale": float(res_speed),
        "pitchScale": float(res_pitch),
        "intonationScale": float(res_intonation),
        "volumeScale": float(res_volume),
    }

def _prepare_audio_for_gradio_playback(audio_filepath: str) -> str:
    """Gradio再生用にASCIIパスへコピーし、ブラウザ側のURL解決を安定させる。"""
    if not audio_filepath or not os.path.exists(audio_filepath):
        return audio_filepath

    source_path = os.path.abspath(audio_filepath)
    playback_dir = os.path.abspath(os.path.join("data", "audio_playback"))
    os.makedirs(playback_dir, exist_ok=True)

    _, extension = os.path.splitext(source_path)
    extension = extension if extension else ".wav"
    digest = hashlib.sha1(source_path.encode("utf-8")).hexdigest()[:12]
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    playback_path = os.path.join(playback_dir, f"{timestamp}_{digest}{extension}")
    shutil.copy2(source_path, playback_path)

    try:
        files = sorted(
            (os.path.join(playback_dir, name) for name in os.listdir(playback_dir)),
            key=os.path.getmtime,
            reverse=True,
        )
        for old_path in files[30:]:
            if os.path.isfile(old_path):
                os.remove(old_path)
    except Exception as e:
        print(f"--- [Audio Playback Cache] 古い再生用音声の整理に失敗: {e} ---")

    return playback_path

def handle_tts_provider_change(provider_display: str, room_name: str):
    """TTSプロバイダ変更時に、モデルと声、およびその他の個別パラメータを切り替え、キャッシュから復元する。"""
    provider = config_manager.tts_provider_key_from_display(provider_display)
    
    # ルーム設定からプロバイダ別設定キャッシュ（tts_provider_settings）を取得する
    override_settings = {}
    if room_name:
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        if os.path.exists(room_config_path):
            try:
                with open(room_config_path, "r", encoding="utf-8") as f:
                    room_config = json.load(f)
                override_settings = room_config.get("override_settings", {})
            except Exception:
                pass

    # プロバイダ切り替え直後の保存を安全に行うため、このタイミングで override_settings["tts_provider"] を更新する
    if room_name and override_settings.get("tts_provider") != provider:
        # 共通キーの同期処理：切り替え先のキャッシュ値をロードし、共通キーへも上書き適用する
        provider_settings = override_settings.get("tts_provider_settings", {}).get(provider, {})
        
        updates = {
            "tts_provider": provider
        }
        # キャッシュに存在する項目を共通キーへマージ
        for k in ["tts_model", "tts_voice", "tts_style_prompt", "tts_voice_speed", "tts_voice_pitch", "tts_voice_intonation", "tts_voice_volume", "tts_profile_name"]:
            if k in provider_settings:
                updates[k] = provider_settings[k]
                
        room_manager.update_room_config(room_name, {"override_settings": updates})
        # 最新の override_settings を再読込
        override_settings.update(updates)

    provider_settings = override_settings.get("tts_provider_settings", {}).get(provider, {})

    profile_choices = [s["name"] for s in config_manager.get_openai_settings_list()]
    restored_profile = provider_settings.get("tts_profile_name")
    if not restored_profile and profile_choices:
        restored_profile = profile_choices[0]

    # モデル・ボイスの選択肢を取得
    if provider == "openai_compatible":
        model_choices = config_manager.get_openai_compatible_tts_model_choices_for_profile(restored_profile)
    else:
        model_choices = config_manager.get_tts_model_choices(provider)

    # 復元する値（なければデフォルト値）
    restored_model = provider_settings.get("tts_model")
    if not restored_model:
        restored_model = model_choices[0] if model_choices else None
    elif provider == "openai_compatible" and model_choices and restored_model not in model_choices:
        restored_model = model_choices[0]
    elif provider == "openai_compatible" and not model_choices:
        restored_model = None
    # カスタム値（リスト外のモデル）が保存されていた場合、選択肢に追加して復元する
    model_choices = _ensure_value_in_choices(model_choices, restored_model)

    # ローカルTTSエンジンの場合は自動で話者リストのフェッチ＆キャッシュ更新を行う
    if provider in {"voicevox", "aivisspeech", "coeiroink"}:
        engine_url = restored_model or (model_choices[0] if model_choices else None)
        if engine_url:
            try:
                import audio_manager
                # 短いタイムアウトで話者を取得
                speakers_map = audio_manager.fetch_local_engine_speakers(provider, engine_url)
                if speakers_map:
                    config_manager.save_tts_speakers_cache(provider, speakers_map)
            except Exception as e:
                print(f"警告: 話者リストの自動更新に失敗しました: {e}")

    if provider == "openai_compatible":
        profile_voice_map = config_manager.get_openai_compatible_tts_voice_map_for_profile(restored_profile, model_name=restored_model)
        voice_choices = list(profile_voice_map.values())
    else:
        profile_voice_map = config_manager.get_tts_voice_map(provider)
        voice_choices = list(profile_voice_map.values())

    restored_voice = provider_settings.get("tts_voice")
    # ボイスの表示名とIDの変換
    if restored_voice and restored_voice in profile_voice_map:
        display_voice = profile_voice_map[restored_voice]
    elif restored_voice and restored_voice in profile_voice_map.values():
        display_voice = restored_voice
    else:
        restored_voice = next(iter(profile_voice_map.keys()), None)
        display_voice = voice_choices[0] if voice_choices else None

    restored_style = provider_settings.get("tts_style_prompt", provider_settings.get("voice_style_prompt", ""))
    
    # 音響パラメータの初期値
    default_params = {
        "tts_voice_speed": 1.0,
        "tts_voice_pitch": 0.0,
        "tts_voice_intonation": 1.0,
        "tts_voice_volume": 1.0
    }
    
    speed = provider_settings.get("tts_voice_speed", default_params["tts_voice_speed"])
    pitch = provider_settings.get("tts_voice_pitch", default_params["tts_voice_pitch"])
    intonation = provider_settings.get("tts_voice_intonation", default_params["tts_voice_intonation"])
    volume = provider_settings.get("tts_voice_volume", default_params["tts_voice_volume"])

    profile_update = gr.update(
        choices=profile_choices,
        value=restored_profile,
        visible=(provider == "openai_compatible")
    )

    return (
        gr.update(choices=model_choices, value=restored_model),
        gr.update(choices=voice_choices, value=display_voice),
        gr.update(value=restored_style),
        gr.update(value=float(speed)),
        gr.update(value=float(pitch)),
        gr.update(value=float(intonation)),
        gr.update(value=float(volume)),
        profile_update,
    )


def handle_refresh_speakers(room_name: str, provider_display: str, model_display: str):
    """ローカル音声合成エンジンから話者リストを手動で取得・更新する。"""
    provider = config_manager.tts_provider_key_from_display(provider_display)
    if provider not in {"voicevox", "aivisspeech", "coeiroink"}:
        gr.Warning("ローカルTTSエンジン（VOICEVOX/AivisSpeech/COEIROINK）が選択されている場合のみ更新可能です。")
        return gr.update()

    if not model_display:
        gr.Warning("エンジンURL（モデル欄）が指定されていません。")
        return gr.update()

    gr.Info("ローカルエンジンから話者リストを取得しています...")
    try:
        import audio_manager
        speakers_map = audio_manager.fetch_local_engine_speakers(provider, model_display)
        if speakers_map:
            config_manager.save_tts_speakers_cache(provider, speakers_map)
            gr.Info("話者リストを更新しました。")
            voice_choices = config_manager.get_tts_voice_choices(provider)
            return gr.update(choices=voice_choices, value=voice_choices[0] if voice_choices else None)
        else:
            gr.Error("話者リストの取得に失敗しました。エンジンが起動しているか、URLが正しいか確認してください。")
    except Exception as e:
        gr.Error(f"エラーが発生しました: {e}")

    return gr.update()


def handle_play_tts_segment(segment_path: str, playlist_paths: Optional[List[str]] = None):
    """分割TTSの選択された音声ファイルを再生する。"""
    if not segment_path:
        raise gr.Error("再生する分割音声が選択されていません。")
    playlist_paths = playlist_paths or []
    current_index = playlist_paths.index(segment_path) if segment_path in playlist_paths else 0
    return gr.update(value=segment_path, visible=True), current_index


def handle_play_next_tts_segment(playlist_paths: Optional[List[str]], current_index: int = 0):
    """分割TTSの次の音声ファイルを自動再生する。"""
    playlist_paths = playlist_paths or []
    try:
        current_index = int(current_index or 0)
    except (TypeError, ValueError):
        current_index = 0
    next_index = current_index + 1
    if next_index >= len(playlist_paths):
        return gr.update(), current_index
    return gr.update(value=playlist_paths[next_index], visible=True), next_index


def handle_play_audio_button_click(selected_message: Optional[Dict[str, str]], room_name: str, api_key_name: str, playback_mode: str = "trim"):
    """
    【最終FIX版 v2】チャット履歴で選択されたAIの発言を音声合成して再生する。
    例外追跡用の try-except を追加。
    """
    if not selected_message:
        raise gr.Error("再生するメッセージが選択されていません。")

    try:
        # 処理中はボタンを無効化
        yield (
            gr.update(visible=False),
            gr.update(value="音声生成中... ▌", interactive=False),
            gr.update(interactive=False),
            gr.update(choices=[], value=None, interactive=False),
            gr.update(interactive=False),
            [],
            0,
        )

        raw_text = utils.extract_raw_text_from_html(selected_message.get("content"))
        print(f"--- [DEBUG:PlayAudio] Playing message content: {raw_text[:100].replace(chr(10), ' ')}")
        text_to_speak = utils.remove_thoughts_from_text(raw_text)

        if not text_to_speak:
            gr.Info("このメッセージには音声で再生できるテキストがありません。")
            yield gr.update(), gr.update(value="🔊 選択した発言を再生", interactive=True), gr.update(interactive=True), gr.update(), gr.update(), [], 0
            return

        tts_settings = _resolve_tts_request_settings(room_name, api_key_name=api_key_name)
        print(
            "--- [DEBUG:TTS] "
            f"provider={tts_settings.get('provider')}, profile={tts_settings.get('profile_name')}, "
            f"base_url={tts_settings.get('base_url')}, model={tts_settings.get('model')}, "
            f"voice={tts_settings.get('voice_id')}, format={tts_settings.get('response_format')} ---"
        )
        provider = (tts_settings.get("provider") or "").strip().lower()
        is_local_tts = provider in {"aivisspeech", "voicevox", "coeiroink"}

        if str(playback_mode or "").lower() == TTS_MODE_SPLIT:
            max_chars = 400 if is_local_tts else 800
        else:
            max_chars = 300 if is_local_tts else None

        text_plan = prepare_tts_text_plan(text_to_speak, playback_mode, max_chars=max_chars)
        if text_plan.notice:
            gr.Info(text_plan.notice)

        api_key = tts_settings.get("api_key")

        if not api_key or api_key.startswith("YOUR_API_KEY"):
            gr.Error("TTS用APIキーが未設定または無効です。")
            yield gr.update(), gr.update(value="🔊 選択した発言を再生", interactive=True), gr.update(interactive=True), gr.update(), gr.update(), [], 0
            return

        gr.Info(f"「{room_name}」の声で音声を生成しています...")
        valid_segments = [s for s in text_plan.segments if s.strip()]
        if not valid_segments:
            gr.Info("このメッセージには音声で再生できるテキストがありません。")
            yield gr.update(), gr.update(value="🔊 選択した発言を再生", interactive=True), gr.update(interactive=True), gr.update(), gr.update(), [], 0
            return

        audio_filepaths = []
        prepared_paths = []
        partial_error_msg = ""
        for index, segment in enumerate(valid_segments, start=1):
            if len(valid_segments) > 1:
                gr.Info(f"音声を生成しています... ({index}/{len(valid_segments)})")
            
            # 音声生成キャッシュの確認
            import hashlib
            seg_hash = hashlib.md5(segment.encode("utf-8")).hexdigest()
            voice_id = tts_settings.get("voice_id")
            cache_key = (seg_hash, room_name, provider, voice_id)
            cached_path = _tts_audio_cache.get(cache_key)
            if cached_path and os.path.exists(cached_path):
                print(f"--- [DEBUG:PlayAudio] Found cached audio file: {cached_path}")
                audio_filepath = cached_path
            else:
                if is_local_tts and index > 1:
                    # ローカルエンジン過負荷防止のディレイ
                    time.sleep(2.0)

                # 長時間の音声合成処理をスレッドで非同期実行し、
                # メインのジェネレータスレッドは定期的に yield してタイムアウトを防ぐ
                import threading
                result_container = {}

                def _synthesis_worker():
                    try:
                        res = generate_audio_with_key_rotation(segment, room_name, tts_settings)
                        result_container["path"] = res
                    except Exception as ex:
                        result_container["error"] = ex

                thread = threading.Thread(target=_synthesis_worker, daemon=True)
                thread.start()

                elapsed = 0
                while thread.is_alive():
                    time.sleep(1.0)
                    elapsed += 1
                    # 1秒ごとに yield して WebSocket 接続を維持
                    # レンダリングの競合を防ぐため、待機中はボタンテキストのみを更新し、
                    # 他のコンポーネントは gr.update() (変更なし) を送ります。
                    yield (
                        gr.update(),
                        gr.update(value=f"音声生成中... ⏰ {elapsed}s", interactive=False),
                        gr.update(),
                        gr.update(),
                        gr.update(),
                        gr.update(),
                        gr.update(),
                    )

                if "error" in result_container:
                    raise result_container["error"]

                audio_filepath = result_container.get("path")

                if audio_filepath and not str(audio_filepath).startswith("【エラー】"):
                    _tts_audio_cache[cache_key] = str(audio_filepath)

            if not audio_filepath or str(audio_filepath).startswith("【エラー】"):
                error_msg = audio_filepath or "音声の生成に失敗しました。"
                if text_plan.mode == TTS_MODE_SPLIT and audio_filepaths:
                    partial_error_msg = f"{index}分割目の音声生成に失敗しました。生成済みの{len(audio_filepaths)}分割だけ再生します。"
                    gr.Warning(partial_error_msg)
                    print(f"--- [DEBUG:PlayAudio] Partial split playback due to TTS error: {error_msg}")
                    break
                gr.Error(error_msg)
                yield gr.update(), gr.update(value="🔊 選択した発言を再生", interactive=True), gr.update(interactive=True), gr.update(), gr.update(), [], 0
                return

            audio_filepaths.append(str(audio_filepath))
            prepared_paths.append(_prepare_audio_for_gradio_playback(str(audio_filepath)))

            # 随時進捗をUIへ反映（1つ目ができたら即座に再生開始）
            segment_choices = [(f"分割 {i + 1}/{len(valid_segments)}", path) for i, path in enumerate(prepared_paths)]
            if index == 1:
                segment_dropdown_update = (
                    gr.update(choices=segment_choices, value=prepared_paths[0], interactive=True)
                    if len(valid_segments) > 1 else
                    gr.update(choices=[], value=None, interactive=False)
                )
                segment_button_update = gr.update(interactive=len(valid_segments) > 1)
                
                yield (
                    gr.update(value=prepared_paths[0], visible=True),
                    gr.update(value="🔊 生成中...", interactive=False) if len(valid_segments) > 1 else gr.update(value="🔊 選択した発言を再生", interactive=True),
                    gr.update(interactive=True),
                    segment_dropdown_update,
                    segment_button_update,
                    prepared_paths.copy(),
                    0,
                )
            else:
                segment_dropdown_update = gr.update(choices=segment_choices)
                yield (
                    gr.update(), # プレイヤーは変更しない（再生中のはず）
                    gr.update(),
                    gr.update(),
                    segment_dropdown_update,
                    gr.update(),
                    prepared_paths.copy(),
                    gr.update(),
                )

        if prepared_paths:
            final_choices = [(f"分割 {i + 1}/{len(prepared_paths)}", path) for i, path in enumerate(prepared_paths)]
            segment_dropdown_update = (
                gr.update(choices=final_choices, interactive=True)
                if len(prepared_paths) > 1 else
                gr.update(choices=[], value=None, interactive=False)
            )
            if len(prepared_paths) > 1:
                gr.Info(partial_error_msg or "分割音声の生成が完了しました。")
            yield (
                gr.update(visible=True),
                gr.update(value="🔊 選択した発言を再生", interactive=True),
                gr.update(interactive=True),
                segment_dropdown_update,
                gr.update(interactive=len(prepared_paths) > 1),
                prepared_paths.copy(),
                gr.update(),
            )
        else:
            error_msg = audio_filepath or "音声の生成に失敗しました。"
            gr.Error(error_msg)
            yield gr.update(), gr.update(value="🔊 選択した発言を再生", interactive=True), gr.update(interactive=True), gr.update(), gr.update(), [], 0

    except Exception as e:
        import traceback
        print("--- [ERROR:PlayAudio] handle_play_audio_button_click 内で例外が発生しました ---")
        traceback.print_exc()
        raise e

def handle_play_audio_button_click_basic(selected_message: Optional[Dict[str, str]], room_name: str, api_key_name: str, playback_mode: str = "trim"):
    """本体UI向けに、分割TTS対応ハンドラの先頭3出力だけを返す。"""
    for result in handle_play_audio_button_click(selected_message, room_name, api_key_name, playback_mode=playback_mode or "trim"):
        if isinstance(result, (tuple, list)) and len(result) >= 3:
            audio_update = result[0]
            button_update = result[1]
            rerun_update = result[2]
        else:
            audio_update = result
            button_update = gr.update()
            rerun_update = gr.update()
        yield (audio_update, button_update, rerun_update)

def handle_voice_preview(
    room_name: str,
    tts_provider: str,
    tts_model: str,
    selected_voice_name: str,
    voice_style_prompt: str,
    text_to_speak: str,
    api_key_name: str,
    voice_speed: float = None,
    voice_pitch: float = None,
    voice_intonation: float = None,
    voice_volume: float = None,
    tts_profile_name: str = None,
):
    """
    【最終FIX版 v2】音声をプレビュー再生する。
    try...except を削除し、Gradioの例外処理に完全に委ねる。
    """
    if not all([tts_provider, selected_voice_name, text_to_speak]):
        raise gr.Error("TTSプロバイダ、声、テキストが選択されている必要があります。")

    yield (
        gr.update(visible=False),
        gr.update(interactive=False),
        gr.update(value="生成中...", interactive=False)
    )

    tts_settings = _resolve_tts_request_settings(
        room_name,
        api_key_name=api_key_name,
        provider_value=tts_provider,
        model_value=tts_model,
        voice_value=selected_voice_name,
        style_prompt=voice_style_prompt,
        voice_speed=voice_speed,
        voice_pitch=voice_pitch,
        voice_intonation=voice_intonation,
        voice_volume=voice_volume,
        profile_value=tts_profile_name,
    )
    print(
        "--- [DEBUG:TTS:Preview] "
        f"provider={tts_settings.get('provider')}, profile={tts_settings.get('profile_name')}, "
        f"base_url={tts_settings.get('base_url')}, model={tts_settings.get('model')}, "
        f"voice={tts_settings.get('voice_id')}, format={tts_settings.get('response_format')} ---"
    )
    api_key = tts_settings.get("api_key")

    if not tts_settings.get("voice_id") or not api_key:
        provider_name = config_manager.tts_provider_display_from_key(tts_settings.get("provider"))
        gr.Error(f"{provider_name} の声またはAPIキーが無効です。APIキー / Webhook管理と音声設定を確認してください。")
        yield gr.update(visible=False), gr.update(interactive=True), gr.update(value="試聴", interactive=True)
        return

    gr.Info(f"声「{selected_voice_name}」で音声を生成しています...")
    
    import threading
    import time
    result_container = {}

    def _synthesis_worker():
        try:
            res = generate_audio_with_key_rotation(text_to_speak, room_name, tts_settings)
            result_container["path"] = res
        except Exception as ex:
            result_container["error"] = ex

    thread = threading.Thread(target=_synthesis_worker, daemon=True)
    thread.start()

    elapsed = 0
    while thread.is_alive():
        time.sleep(1.0)
        elapsed += 1
        yield (
            gr.update(),  # audio_player
            gr.update(),  # play_audio_button
            gr.update(value=f"生成中... ⏰ {elapsed}s", interactive=False)  # room_preview_voice_button
        )

    if "error" in result_container:
        print(f"--- [DEBUG:Preview] スレッド内でエラーが検出されました: {result_container['error']} ---")
        raise result_container["error"]

    audio_filepath = result_container.get("path")
    print(f"--- [DEBUG:Preview] 音声生成完了、パス: {audio_filepath} ---")

    if audio_filepath and not audio_filepath.startswith("【エラー】"):
        audio_filepath = _prepare_audio_for_gradio_playback(audio_filepath)
        print(f"--- [DEBUG:Preview] Gradio再生用準備完了、パス: {audio_filepath} ---")
        gr.Info("プレビューを再生します。")
        # まず値をセットして表示させる
        yield gr.update(value=audio_filepath, visible=True), gr.update(interactive=True), gr.update(value="試聴", interactive=True)
        print("--- [DEBUG:Preview] プレビュー再生の yield 送信完了（値あり） ---")
        
        # Gradioフロントエンド側でオーディオ要素の再ロード・マウント処理が走るため、
        # 描画競合（初期値へのフォールバック）を防止するために1.5秒待機し、
        # 最後に値なしで visible=True だけを確定送信する
        time.sleep(1.5)
        yield gr.update(visible=True), gr.update(interactive=True), gr.update(value="試聴", interactive=True)
        print("--- [DEBUG:Preview] 最終表示確定の yield 送信完了（値なし） ---")
    else:
        print(f"--- [DEBUG:Preview] 音声生成エラー判定、パス: {audio_filepath} ---")
        gr.Error(audio_filepath or "音声の生成に失敗しました。")
        yield gr.update(visible=False), gr.update(interactive=True), gr.update(value="試聴", interactive=True)

def _parse_llm_error_to_readable(e: Exception) -> str:
    """
    LLMからの多様なエラー（ResourceExhausted 等）を、
    ユーザーに分かりやすい短い日本語メッセージに変換する。
    """
    err_str = str(e)

    # API 429
    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
        return "APIの利用制限(429)に達しました。しばらく待つか、別のAPIキーを試してください。"

    # API 500 / 503
    if "500" in err_str or "503" in err_str or "Service Unavailable" in err_str:
        return "サーバーが一時的に混み合っているか、停止しています(503)。時間をおいて再試行してください。"

    # 認証エラー
    if "API_KEY_INVALID" in err_str or "401" in err_str:
        return "APIキーが無効です。設定を確認してください。"

    # それ以外は元のメッセージを最大限簡略化して返す
    # (JSON的な中身があれば、最初の100文字程度を抽出)
    if "{" in err_str and "message" in err_str:
        try:
            # 簡易的な抽出
            match = re.search(r'"message":\s*"(.*?)"', err_str)
            if match:
                return match.group(1)
        except:
            pass

    return err_str[:150] + "..." if len(err_str) > 150 else err_str

def _generate_scenery_prompt(room_name: str, api_key_name: Optional[str], style_choice: str) -> str:
    """
    画像生成のための最終的なプロンプト文字列を生成する責務を負うヘルパー関数。
    """
    from llm_factory import LLMFactory

    # 世界設定などの取得（リトライループの外で行う）
    season_en, time_of_day_en = utils._get_current_time_context(room_name)
    location_id = utils.get_current_location(room_name)
    if not location_id:
        raise gr.Error("現在地が特定できません。")

    world_settings_path = room_manager.get_world_settings_path(room_name)
    world_settings = utils.parse_world_file(world_settings_path)
    if not world_settings:
        raise gr.Error("世界設定の読み込みに失敗しました。")

    space_text = None
    for area, places in world_settings.items():
        if location_id in places:
            space_text = places[location_id]
            break
    if not space_text:
        raise gr.Error("現在の場所の定義が見つかりません。")

    style_prompts = {
        "写真風 (デフォルト)": "An ultra-detailed, photorealistic masterpiece with cinematic lighting.",
        "イラスト風": "A beautiful and detailed anime-style illustration, pixiv contest winner.",
        "アニメ風": "A high-quality screenshot from a modern animated film.",
        "水彩画風": "A gentle and emotional watercolor painting."
    }
    style_choice_text = style_prompts.get(style_choice, style_prompts["写真風 (デフォルト)"])

    director_prompt = f"""
You are a master scene director AI for a high-end image generation model.
Your sole purpose is to synthesize information from two distinct sources into a single, cohesive, and flawless English prompt.

**--- [Source 1: Architectural Blueprint] ---**
This is the undeniable truth for all physical structures, objects, furniture, and materials.
```
{space_text}
```
**--- [Current Scene Conditions] ---**
        - Time of Day: {time_of_day_en}
        - Season: {season_en}
        - **CRITICAL LIGHTING INSTRUCTION**: The scene lighting MUST match the time of day.
            - Daytime (morning, late_morning, afternoon): Bright natural sunlight, blue sky visible through windows, warm sun rays.
            - Evening: Golden hour, warm orange sunset colors.
            - Night/Midnight: Dark sky, moonlight or artificial lighting, stars visible.
        - **FIREPLACE & CLIMATE CONTROL RULES**:
            - If the Architectural Blueprint (Source 1) mentions a "fireplace", "stove", or "heater":
                - If the Current Season is warm/hot (e.g., `summer`, `early_summer`, `late_summer`): The fire in the fireplace/stove/heater **MUST BE COMPLETELY EXTINGUISHED** (completely cold, dark, no fire, no glowing embers, silent and empty). A blazing fire in summer is strictly prohibited.
                - If the Current Season is cold (e.g., `winter`, `late_autumn`, `early_spring`): The fireplace/stove/heater **SHOULD BE ACTIVE with a warm, glowing, cozy fire blazing inside**.
            - If the blueprint mentions "air conditioner", "cooler", or "fan", describe them as either active (cool air blowing in summer, warm air blowing in winter) or idle/off (in mild seasons) appropriate to the season.

**--- [Your Task: The Fusion] ---**
Your task is to **merge** these two sources into a single, coherent visual description, following the absolute rules below.

**--- [The Golden Rule for Windows & Exteriors] ---**
**If the Architectural Blueprint mentions a window, door, or any view to the outside, you MUST explicitly describe the exterior view *as it would appear* within the Temporal Context.**
-   **Example:** If the context is `night` and the blueprint mentions "a garden," you MUST describe a `dark garden under the moonlight` or `a rainy night landscape`, not just `a garden`.
-   **Example:** If the context is `afternoon` and the blueprint mentions "a garden," you MUST describe a `sunlit garden under bright blue sky` or `a garden bathed in warm afternoon sunlight`.
-   **This rule is absolute and overrides any ambiguity.**

**--- [Core Principles & Hierarchy] ---**
1.  **Architectural Fidelity:** Your prompt MUST be a faithful visual representation of the physical elements described in the "Architectural Blueprint" (Source 1).
2.  **Atmospheric & Lighting Fidelity:** The overall lighting, weather, and the view seen through windows MUST be a direct and faithful representation of the "Temporal Context" (Source 2), unless the blueprint describes an absolute, unchangeable environmental property (e.g., "a cave with no natural light," "a dimension of perpetual twilight").
3.  **Strictly Visual:** The output must be a purely visual paragraph in English. Exclude any narrative, metaphors, sounds, or non-visual elements.
4.  **Mandatory Inclusions:** Your prompt MUST incorporate the specified "Style Definition".
5.  **Absolute Prohibitions:** Strictly enforce all "Negative Prompts".
6.  **Output Format:** Output ONLY the final, single-paragraph prompt. Do not include any of your own thoughts or conversational text.

---
**[Supporting Information]**

**Style Definition (Incorporate this aesthetic):**
- {style_choice_text}

**Negative Prompts (Strictly enforce these exclusions):**
- Absolutely no text, letters, characters, signatures, or watermarks. Do not include people.
---

**Final Master Prompt:**
"""

    # --- [NEW] リトライループの実装 ---
    max_retries = 5
    last_error = None

    for attempt in range(max_retries):
        try:
            # 最新の設定と、その時点での「有効な」APIキー名を取得（ローテーション考慮）
            effective_settings = config_manager.get_effective_settings(room_name)
            target_model = effective_settings.get("model_name", "gemini-2.5-flash-lite")

            # config_manager から現在利用可能なキー名を取得（枯渇マークを反映させるため毎回呼ぶ）
            current_key_name = config_manager.get_active_gemini_api_key_name(model_name=target_model)

            scene_director_llm = LLMFactory.create_chat_model(
                api_key=None, # None を渡すことで factory 内部の自動選択に任せる
                generation_config=effective_settings,
                internal_role="processing"
            )

            response = scene_director_llm.invoke(director_prompt)
            content = utils.get_content_as_string(response)
            if content:
                return content.strip()

        except Exception as e:
            last_error = e
            err_str = str(e)

            # 枯渇（429）の場合、当該キーをモデルごとに枯渇マークして次へ
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                failed_key_name = config_manager.get_active_gemini_api_key_name(model_name=target_model)
                print(f"--- [PromptGen Retry] 429 Error detected for model '{target_model}'. Marking key '{failed_key_name}' as exhausted and retrying... ({attempt+1}/{max_retries}) ---")

                config_manager.mark_key_as_exhausted(failed_key_name, model_name=target_model)
                time.sleep(1) # 少し待機
                continue

            # 429 以外（認証エラーやサーバーダウン等）は即座に失敗させるか、リトライするか？
            # ここでは 503 等も考慮して継続するが、致命的エラーなら break 可能
            print(f"--- [PromptGen Retry] Unexpected error: {err_str} (attempt {attempt+1}/{max_retries}) ---")
            time.sleep(1)

    # 全てのリトライが失敗した場合
    readable_error = _parse_llm_error_to_readable(last_error)
    raise gr.Error(f"プロンプト構成に失敗しました。\n{readable_error}")

def handle_show_scenery_prompt(room_name: str, api_key_name: str, style_choice: str) -> str:
    """「プロンプトを生成」ボタンのイベントハンドラ。"""
    if not room_name:
        raise gr.Error("ルームを選択してください。")

    try:
        gr.Info("シーンディレクターAIがプロンプトを構成しています...")
        # api_key_nameを渡しても _generate_scenery_prompt 内部で内部処理用の自動選択が行われる
        prompt = _generate_scenery_prompt(room_name, api_key_name, style_choice)
        gr.Info("プロンプトを生成しました。")
        return prompt
    except gr.Error:
        raise # すでに整形済みの gr.Error はそのまま投げる
    except Exception as e:
        readable_error = _parse_llm_error_to_readable(e)
        print(f"--- プロンプト生成エラー: {str(e)} ---")
        traceback.print_exc()
        raise gr.Error(f"プロンプト生成エラー: {readable_error}")

def handle_generate_or_regenerate_scenery_image(room_name: str, api_key_name: str, style_choice: str) -> Optional[Image.Image]:
    """
    【v6: マルチプロバイダ対応版】
    画像生成設定に基づき、GeminiまたはOpenAI互換プロバイダで情景画像を生成する。
    """
    # --- 設定読み込み ---
    latest_config = config_manager.load_config_file()
    provider = latest_config.get("image_generation_provider", "gemini")

    # 機能が無効化されているか？
    if provider == "disabled":
        gr.Info("画像生成機能は、現在「共通設定」で無効化されています。")
        location_id_fb = utils.get_current_location(room_name)
        if location_id_fb:
            fallback_image_path_fb = utils.find_scenery_image(room_name, location_id_fb)
            if fallback_image_path_fb:
                return _load_image_for_gradio(fallback_image_path_fb)
        return None

    # ルーム名チェック
    if not room_name:
        gr.Warning("ルームを選択してください。")
        return None

    # 1. 適用すべき季節と時間帯を取得
    season_en, time_of_day_en = utils._get_current_time_context(room_name)

    # 2. 取得した値を使ってファイル名を確定
    location_id = utils.get_current_location(room_name)
    if not location_id:
        gr.Warning("現在地が特定できません。")
        return None

    save_dir = os.path.join(constants.ROOMS_DIR, room_name, "spaces", "images")
    os.makedirs(save_dir, exist_ok=True)
    final_filename = f"{location_id}_{season_en}_{time_of_day_en}.png"
    final_path = os.path.join(save_dir, final_filename)

    # フォールバック用に、現在の画像パスを先に探しておく
    fallback_image_path = utils.find_scenery_image(room_name, location_id)

    # --- プロンプト生成（APIローテーション対応）---
    final_prompt = ""
    try:
        # プロンプト生成フェーズでは内部処理用のモデルとローテーションを優先利用する
        final_prompt = _generate_scenery_prompt(room_name, api_key_name, style_choice)
    except gr.Error:
        raise # 整形済みのエラーを保持
    except Exception as e:
        readable_error = _parse_llm_error_to_readable(e)
        print(f"シーンディレクターAIによるプロンプト生成中にエラーが発生しました: {str(e)}")
        raise gr.Error(f"プロンプト構成に失敗しました。\n詳細: {readable_error}")

    if not final_prompt:
        gr.Error("シーンディレクターAIが有効なプロンプトを生成できませんでした。")
        if fallback_image_path: return _load_image_for_gradio(fallback_image_path)
        return None

    # --- 画像生成 ---
    gr.Info(f"「{style_choice}」で画像を生成します... (プロバイダ: {provider})")

    # generate_image ツールを呼び出し（設定は内部で読み込まれる）
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name, "")
    result = generate_image_tool_func.func(prompt=final_prompt, room_name=room_name, api_key=api_key, api_key_name=api_key_name)

    # 確定パスで上書き保存し、そのパスを返す
    if "Generated Image:" in result:
        match = re.search(r"\[Generated Image: (.*?)\]", result, re.DOTALL)
        generated_path = match.group(1).strip() if match else None

        if generated_path and os.path.exists(generated_path):
            try:
                shutil.move(generated_path, final_path)
                print(f"--- 情景画像を生成し、保存/上書きしました: {final_path} ---")
                gr.Info("画像を生成/更新しました。")
                return _load_image_for_gradio(final_path)
            except Exception as move_e:
                gr.Error(f"生成された画像の移動/上書きに失敗しました: {move_e}")
                if fallback_image_path: return _load_image_for_gradio(fallback_image_path)
                return None
        else:
            gr.Error("画像の生成には成功しましたが、一時ファイルの特定に失敗しました。")
    else:
        # トースト通知用にメッセージを整形（ツール特有の「【エラー】」接頭辞があれば除去）
        clean_result = result.replace("【エラー】", "").strip()
        gr.Error(f"画像の生成に失敗しました: {clean_result}")

    # フォールバック
    if fallback_image_path: return _load_image_for_gradio(fallback_image_path)
    return None

def handle_api_connection_test(api_key_name: str):
    if not api_key_name:
        gr.Warning("テストするAPIキーが選択されていません。")
        return

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー '{api_key_name}' は無効です。config.jsonを確認してください。")
        return

    gr.Info(f"APIキー '{api_key_name}' を使って、必須モデルへの接続をテストしています...")
    import google.genai as genai

    required_models = {
        "models/gemini-2.5-pro": "メインエージェント (agent_node)",
        "models/gemini-2.5-flash": "高速処理 (context_generator)",
    }
    results = []
    all_ok = True

    try:
        client = genai.Client(api_key=api_key)
        for model_name, purpose in required_models.items():
            try:
                client.models.get(model=model_name)
                results.append(f"✅ **{purpose} ({model_name.split('/')[-1]})**: 利用可能です。")
            except Exception as model_e:
                results.append(f"❌ **{purpose} ({model_name.split('/')[-1]})**: 利用できません。")
                print(f"--- モデル '{model_name}' のチェックに失敗: {model_e} ---")
                all_ok = False

        result_message = "\n\n".join(results)
        if all_ok:
            gr.Info(f"✅ **全ての必須モデルが利用可能です！**\n\n{result_message}")
        else:
            gr.Warning(f"⚠️ **一部のモデルが利用できません。**\n\n{result_message}\n\nGoogle AI StudioまたはGoogle Cloudコンソールの設定を確認してください。")

    except Exception as e:
        error_message = f"❌ **APIサーバーへの接続自体に失敗しました。**\n\nAPIキーが無効か、ネットワークの問題が発生している可能性があります。\n\n詳細: {str(e)}"
        print(f"--- API接続テストエラー ---\n{traceback.format_exc()}")
        gr.Error(error_message)

from world_builder import get_world_data, save_world_data

def handle_world_builder_load(room_name: str):
    from world_builder import get_world_data
    if not room_name:
        return {}, gr.update(), "", gr.update()

    world_data = get_world_data(room_name)
    area_choices = sorted(world_data.keys())

    world_settings_path = room_manager.get_world_settings_path(room_name)
    raw_content = ""
    if world_settings_path and os.path.exists(world_settings_path):
        with open(world_settings_path, "r", encoding="utf-8") as f:
            raw_content = f.read()

    current_location = utils.get_current_location(room_name)
    selected_area = None
    place_choices_for_selected_area = []

    if current_location:
        for area_name, places in world_data.items():
            if current_location in places:
                selected_area = area_name
                place_choices_for_selected_area = sorted(places.keys())
                break

    return (
        world_data,
        gr.update(choices=area_choices, value=selected_area),
        raw_content,
        gr.update(choices=place_choices_for_selected_area, value=current_location)
    )

def handle_room_change_for_all_tabs(room_name: str, api_key_val: str, expected_count: int, request: gr.Request = None, preserve_chat_area: bool = False):
    """
    【v11: 最終契約遵守版】
    ルーム変更時に、全てのUI更新と内部状態の更新を、この単一の関数で完結させる。
    """
    # [2026-04-09 FIX] セッション分離型初期化ガード
    session_id = _get_session_id(request)
    init_room = _get_session_init_room(session_id)
    state = _session_init_states.get(session_id, {})

    if session_id != "default" and room_name and state.get("completed", False) and room_name != state.get("room"):
        _session_init_states[session_id] = {
            "completed": True,
            "time": time.time() - POST_INIT_GRACE_PERIOD_SECONDS,
            "room": room_name
        }
        state = _session_init_states[session_id]
        init_room = room_name

    # 初期化中、または初期化完了後しばらくの同一ルーム再更新は、
    # demo.load の値反映で発火した冗長な change とみなしてスキップする。
    is_initializing = (not state.get("completed", False))
    is_just_finished = state.get("completed") and (time.time() - state.get("time", 0)) < POST_INIT_GRACE_PERIOD_SECONDS

    if init_room and (is_initializing or is_just_finished):
        if room_name != init_room:
            print(f"--- [Session:{session_id}] UI司令塔: キャッシュ不整合阻止: {room_name} -> 正解 '{init_room}' を強制維持 ---")
            return _ensure_output_count((init_room,), expected_count)
        print(f"--- [Session:{session_id}] UI司令塔: 初期化直後の同一ルーム再更新をスキップ: {room_name} ---")
        return _ensure_output_count((init_room,), expected_count)

    global _last_room_switch_time
    _last_room_switch_time = time.time()

    # [2026-04-09 FIX] 正規の切り替えが行われた場合、セッションの「正解」も更新してガードの基準を変える
    if session_id != "default":
        _session_init_states[session_id] = {
            "completed": True,
            "time": time.time(),
            "room": room_name
        }

    print(f"--- [Session:{session_id}] UI司令塔 実行: {room_name} へ変更 ---")
    log_memory_diagnostics("room_change_full:start", room_name, {"preserve_chat": preserve_chat_area})

    # [NEW] ルーム切替時のログ自動バックアップ
    room_manager.create_backup(room_name, 'log')
    room_manager.set_active_room_for_backup(room_name)

    # 責務1: 各UIセクションの更新値を個別に生成する
    chat_tab_updates = _update_chat_tab_for_room_change(room_name, api_key_val)
    if preserve_chat_area:
        chat_tab_updates = (gr.update(), gr.update(), gr.update(), gr.update(), *chat_tab_updates[4:])
    world_builder_updates = handle_world_builder_load(room_name)
    # グループ会話の参加者リストから現在のルームを除外
    all_rooms = room_manager.get_room_list_for_ui()
    room_names_only = [name for name, _folder in all_rooms]
    participant_choices = sorted([r for r in room_names_only if r != room_name])
    session_management_updates = ([], "現在、1対1の会話モードです。", gr.update(choices=participant_choices, value=[]))
    rules = config_manager.load_redaction_rules()
    rules_df_for_ui = _create_redaction_df_from_rules(rules)
    archive_dates = _get_date_choices_from_memory(room_name)
    archive_date_dd_update = gr.update(choices=archive_dates, value=archive_dates[0] if archive_dates else None)
    time_settings = _load_time_settings_for_room(room_name)
    time_settings_updates = (
        gr.update(value=time_settings.get("mode", "リアル連動")),
        gr.update(value=time_settings.get("fixed_season_ja", "秋")),
        gr.update(value=time_settings.get("fixed_time_of_day_ja", "夜")),
        gr.update(visible=(time_settings.get("mode", "リアル連動") == "選択する"))
    )
    ui_attachments_df = _get_attachments_df(room_name)
    initial_active_attachments_display = "現在アクティブな添付ファイルはありません。"
    locations_for_custom_scenery = _get_location_choices_for_ui(room_name)
    current_location_for_custom_scenery = utils.get_current_location(room_name)
    custom_scenery_dd_update = gr.update(choices=locations_for_custom_scenery, value=current_location_for_custom_scenery)
    current_season_ja, current_time_ja = _get_current_time_context_ui_values(room_name)
    custom_scenery_season_dd_update = gr.update(value=current_season_ja)
    custom_scenery_time_dd_update = gr.update(value=current_time_ja)

    all_updates_tuple = (
        *chat_tab_updates, *world_builder_updates, *session_management_updates,
        rules_df_for_ui, archive_date_dd_update, *time_settings_updates,
        ui_attachments_df, initial_active_attachments_display,
        custom_scenery_dd_update, custom_scenery_season_dd_update, custom_scenery_time_dd_update
    )

    token_count_text = _hide_token_count_display(room_name)

    # 索引の最終更新日時を取得
    memory_index_last_updated = _get_rag_index_last_updated(room_name, "memory")
    current_log_index_last_updated = _get_rag_index_last_updated(room_name, "current_log")

    # ワーキングメモリの情報を取得 [v3]
    wm_slots_update, wm_content_update, active_wm_label = _get_working_memory_updates(room_name)

    # 契約遵守のため、最後の戻り値として索引ステータスとWM情報を追加
    final_outputs = all_updates_tuple + (
        token_count_text,
        "",  # room_delete_confirmed_state
        f"最終更新: {memory_index_last_updated}",  # memory_reindex_status
        f"最終更新: {current_log_index_last_updated}",  # current_log_reindex_status
        active_wm_label, # active_working_memory_status
        wm_slots_update, # working_memory_slot_dropdown
        wm_content_update # working_memory_editor
    )

    result = _ensure_output_count(final_outputs, expected_count)
    _remember_programmatic_room_settings(room_name, result, _FULL_ROOM_SETTING_OUTPUT_MAP)
    log_memory_diagnostics("room_change_full:end", room_name, {"outputs": len(result), "preserve_chat": preserve_chat_area})
    return result


def handle_room_change_for_all_tabs_preserve_chat(room_name: str, api_key_val: str, expected_count: int, request: gr.Request = None):
    """チャット欄先行更新後に、チャット表示を再描画せず残りのUIだけ同期する。"""
    return handle_room_change_for_all_tabs(
        room_name,
        api_key_val,
        expected_count,
        request=request,
        preserve_chat_area=True
    )


def handle_refresh_room_settings_from_disk(
    room_name: str,
    expected_count: int,
    request: gr.Request = None,
):
    """別ブラウザの変更を含む正本を再読込し、ルーム設定UI全体を同期する。"""
    config_manager.load_config()
    api_key_name = config_manager.get_active_gemini_api_key_name(room_name)
    return handle_room_change_for_all_tabs(
        room_name,
        api_key_name,
        expected_count,
        request=request,
        preserve_chat_area=True,
    )


def handle_start_session(main_room: str, participant_list: list) -> tuple:
    if not participant_list:
        gr.Info("会話に参加するルームを1人以上選択してください。")
        return gr.update(), gr.update()

    all_participants = [main_room] + participant_list
    participants_text = "、".join(all_participants)
    status_text = f"現在、**{participants_text}** を招待して会話中です。"
    session_start_message = f"（システム通知：{participants_text} とのグループ会話が開始されました。）"

    for room_name in all_participants:
        log_f, _, _, _, _, _, _ = get_room_files_paths(room_name)
        if log_f:
            utils.save_message_to_log(log_f, "## SYSTEM:(セッション管理)", session_start_message)

    gr.Info(f"グループ会話を開始しました。参加者: {participants_text}")
    return participant_list, status_text


def handle_end_session(main_room: str, active_participants: list) -> tuple:
    if not active_participants:
        gr.Info("現在、1対1の会話モードです。")
        return [], "現在、1対1の会話モードです。", gr.update(value=[])

    all_participants = [main_room] + active_participants
    session_end_message = "（システム通知：グループ会話が終了しました。）"

    for room_name in all_participants:
        log_f, _, _, _, _, _, _ = get_room_files_paths(room_name)
        if log_f:
            utils.save_message_to_log(log_f, "## SYSTEM:(セッション管理)", session_end_message)

    gr.Info("グループ会話を終了し、1対1の会話モードに戻りました。")
    return [], "現在、1対1の会話モードです。", gr.update(value=[])


def handle_wb_area_select(world_data: Dict, area_name: str):
    if not area_name or area_name not in world_data:
        return gr.update()
    places = sorted(world_data[area_name].keys())
    return gr.update(choices=places)

def handle_wb_place_select(world_data: Dict, area_name: str, place_name: str):
    if not area_name or not place_name:
        return gr.update(value="", visible=False), gr.update(visible=False), gr.update(visible=False)
    content = world_data.get(area_name, {}).get(place_name, "")
    return (
        gr.update(value=content, visible=True),
        gr.update(visible=True),
        gr.update(visible=True)
    )

def handle_wb_save(room_name: str, world_data: Dict, area_name: str, place_name: str, content: str):
    from world_builder import save_world_data
    if not room_name or not area_name or not place_name:
        gr.Warning("保存するにはエリアと場所を選択してください。")
        return world_data, gr.update(), gr.update()

    if area_name in world_data and place_name in world_data[area_name]:
        world_data[area_name][place_name] = content
        save_world_data(room_name, world_data)
        gr.Info("世界設定を保存しました。")
    else:
        gr.Error("保存対象のエリアまたは場所が見つかりません。")

    world_settings_path = room_manager.get_world_settings_path(room_name)
    raw_content = ""
    if world_settings_path and os.path.exists(world_settings_path):
        with open(world_settings_path, "r", encoding="utf-8") as f:
            raw_content = f.read()
    new_location_choices = _get_location_choices_for_ui(room_name)
    location_dropdown_update = gr.update(choices=new_location_choices)
    return world_data, raw_content, location_dropdown_update

def handle_wb_delete_place(room_name: str, world_data: Dict, area_name: str, place_name: str):
    from world_builder import save_world_data
    if not area_name or not place_name:
        gr.Warning("削除するエリアと場所を選択してください。")
        return world_data, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
    if area_name not in world_data or place_name not in world_data[area_name]:
        gr.Warning(f"場所 '{place_name}' がエリア '{area_name}' に見つかりません。")
        return world_data, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    del world_data[area_name][place_name]
    save_world_data(room_name, world_data)
    gr.Info(f"場所 '{place_name}' を削除しました。")

    area_choices = sorted(world_data.keys())
    place_choices = sorted(world_data.get(area_name, {}).keys())
    world_settings_path = room_manager.get_world_settings_path(room_name)
    raw_content = ""
    if world_settings_path and os.path.exists(world_settings_path):
        with open(world_settings_path, "r", encoding="utf-8") as f:
            raw_content = f.read()

    new_location_choices = _get_location_choices_for_ui(room_name)
    location_dropdown_update = gr.update(choices=new_location_choices)

    return (
        world_data,
        gr.update(choices=area_choices, value=area_name),
        gr.update(choices=place_choices, value=None),
        gr.update(value="", visible=False),
        gr.update(visible=False),
        gr.update(visible=False),
        raw_content,
        location_dropdown_update
    )

def handle_wb_confirm_add(room_name: str, world_data: Dict, selected_area: str, item_type: str, item_name: str):
    from world_builder import save_world_data
    if not room_name or not item_name:
        gr.Warning("ルームが選択されていないか、名前が入力されていません。")
        # outputsの数(7)に合わせてgr.update()を返す
        return world_data, gr.update(), gr.update(), gr.update(visible=True), item_name, gr.update(), gr.update()

    item_name = item_name.strip()
    if not item_name:
        gr.Warning("名前が空です。")
        # outputsの数(7)に合わせてgr.update()を返す
        return world_data, gr.update(), gr.update(), gr.update(visible=True), item_name, gr.update(), gr.update()

    raw_content = ""
    if item_type == "area":
        if item_name in world_data:
            gr.Warning(f"エリア '{item_name}' は既に存在します。")
            return world_data, gr.update(), gr.update(), gr.update(visible=True), item_name, gr.update(), gr.update()
        world_data[item_name] = {}
        save_world_data(room_name, world_data)
        gr.Info(f"新しいエリア '{item_name}' を追加しました。")

        area_choices = sorted(world_data.keys())
        world_settings_path = room_manager.get_world_settings_path(room_name)
        if world_settings_path and os.path.exists(world_settings_path):
            with open(world_settings_path, "r", encoding="utf-8") as f: raw_content = f.read()

        # ▼▼▼【ここが修正箇所】▼▼▼
        new_location_choices = _get_location_choices_for_ui(room_name)
        location_dropdown_update = gr.update(choices=new_location_choices)
        return world_data, gr.update(choices=area_choices, value=item_name), gr.update(), gr.update(visible=False), "", raw_content, location_dropdown_update

    elif item_type == "place":
        if not selected_area:
            gr.Warning("場所を追加するエリアを選択してください。")
            return world_data, gr.update(), gr.update(), gr.update(visible=True), item_name, gr.update(), gr.update()
        if item_name in world_data.get(selected_area, {}):
            gr.Warning(f"場所 '{item_name}' はエリア '{selected_area}' に既に存在します。")
            return world_data, gr.update(), gr.update(), gr.update(visible=True), item_name, gr.update(), gr.update()

        world_data[selected_area][item_name] = "新しい場所です。説明を記述してください。"
        save_world_data(room_name, world_data)
        gr.Info(f"エリア '{selected_area}' に新しい場所 '{item_name}' を追加しました。")

        place_choices = sorted(world_data[selected_area].keys())
        world_settings_path = room_manager.get_world_settings_path(room_name)
        if world_settings_path and os.path.exists(world_settings_path):
            with open(world_settings_path, "r", encoding="utf-8") as f: raw_content = f.read()

        # ▼▼▼【ここが修正箇所】▼▼▼
        new_location_choices = _get_location_choices_for_ui(room_name)
        location_dropdown_update = gr.update(choices=new_location_choices)
        return world_data, gr.update(), gr.update(choices=place_choices, value=item_name), gr.update(visible=False), "", raw_content, location_dropdown_update

    else:
        gr.Error(f"不明なアイテムタイプです: {item_type}")
        return world_data, gr.update(), gr.update(), gr.update(visible=False), "", gr.update(), gr.update()

def handle_save_world_settings_raw(room_name: str, raw_content: str):
    """
    【v2: 司令塔アーキテクチャ版】
    RAWテキストを保存し、関連する全てのUIコンポーネントの更新値を返す。
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    room_manager.create_backup(room_name, 'world_setting')

    world_settings_path = room_manager.get_world_settings_path(room_name)
    if not world_settings_path:
        gr.Error("世界設定ファイルのパスが取得できませんでした。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    try:
        with open(world_settings_path, "w", encoding="utf-8") as f:
            f.write(raw_content)
        gr.Info("RAWテキストとして世界設定を保存しました。")

        # 成功した場合、関連する全てのUI更新値を生成して返す
        new_world_data = get_world_data(room_name)
        new_area_choices = sorted(new_world_data.keys())
        new_location_choices = _get_location_choices_for_ui(room_name)

        return (
            new_world_data,                                        # world_data_state
            gr.update(choices=new_area_choices, value=None),       # area_selector
            gr.update(),                     # place_selector
            gr.update(value=raw_content),                          # world_settings_raw_editor
            gr.update(choices=new_location_choices)                # location_dropdown
        )
    except Exception as e:
        gr.Error(f"世界設定のRAW保存中にエラーが発生しました: {e}")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

# ui_handlers.py の handle_reload_world_settings_raw 関数を、以下で完全に置き換えてください。

def handle_reload_world_settings_raw(room_name: str):
    """
    【v2: 司令塔アーキテクチャ版】
    RAWテキストを再読込し、関連する全てのUIコンポーネントの更新値を返す。
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return "", {}, gr.update(choices=[]), gr.update(choices=[]), gr.update(choices=[])

    world_settings_path = room_manager.get_world_settings_path(room_name)
    raw_content = ""
    if world_settings_path and os.path.exists(world_settings_path):
        with open(world_settings_path, "r", encoding="utf-8") as f:
            raw_content = f.read()
    gr.Info("世界設定ファイルを再読み込みしました。")

    # 保存時と同様に、関連する全てのUI更新値を生成して返す
    new_world_data = get_world_data(room_name)
    new_area_choices = sorted(new_world_data.keys())
    new_location_choices = _get_location_choices_for_ui(room_name)

    return (
        new_world_data,                                        # world_data_state
        gr.update(choices=new_area_choices, value=None),       # area_selector
        gr.update(),                     # place_selector
        gr.update(value=raw_content),                          # world_settings_raw_editor
        gr.update(choices=new_location_choices)                # location_dropdown
    )

def handle_save_gemini_key(key_name: str, key_value: str):
    """【v14: 責務分離版】新しいAPIキーを保存し、関連UIのみを更新する。"""
    # 入力検証
    if not key_name or not key_value or not re.match(r"^[a-zA-Z0-9_]+$", key_name.strip()):
        gr.Warning("キーの名前（半角英数字とアンダースコアのみ）と値を両方入力してください。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    key_name = key_name.strip()
    config_manager.add_or_update_gemini_key(key_name, key_value)
    gr.Info(f"Gemini APIキー「{key_name}」を保存しました。UIをリフレッシュします...")

    config_manager.load_config() # 最新の状態を読み込み

    new_choices_for_ui = config_manager.get_api_key_choices_for_ui()
    new_key_names = [key for _, key in new_choices_for_ui]
    paid_keys = config_manager.CONFIG_GLOBAL.get("paid_api_key_names", [])

    return (
        gr.update(choices=new_choices_for_ui, value=key_name), # api_key_dropdown
        gr.update(choices=new_choices_for_ui, value=None),     # gemini_delete_key_dropdown
        gr.update(choices=new_key_names, value=paid_keys),     # paid_keys_checkbox_group
        gr.update(value=""),                                   # gemini_key_name_input (クリア)
        gr.update(value="")                                    # gemini_key_value_input (クリア)
    )

def handle_delete_gemini_key(key_name: str):
    """【v14: 責務分離版】APIキーを削除し、関連UIを更新する。"""
    if not key_name:
        gr.Warning("削除するキーをリストから選択してください。")
        return gr.update(), gr.update(), gr.update()

    config_manager.delete_gemini_key(key_name)
    gr.Info(f"Gemini APIキー「{key_name}」を削除しました。")

    config_manager.load_config()
    new_choices_for_ui = config_manager.get_api_key_choices_for_ui()
    new_key_names = [pair[1] for pair in new_choices_for_ui]
    paid_keys = config_manager.CONFIG_GLOBAL.get("paid_api_key_names", [])

    return (
        gr.update(choices=new_choices_for_ui, value=new_key_names[0] if new_key_names else None), # api_key_dropdown
        gr.update(choices=new_choices_for_ui, value=None), # gemini_delete_key_dropdown
        gr.update(choices=new_key_names, value=paid_keys)   # paid_keys_checkbox_group
    )

def handle_save_pushover_config(user_key, app_token):
    latest_config = config_manager.load_config_file()
    effective_user_key = user_key or latest_config.get("pushover_user_key", "")
    effective_app_token = app_token or latest_config.get("pushover_app_token", "")
    if not effective_user_key or not effective_app_token:
        gr.Warning("Pushover User KeyまたはApp Tokenが未設定です。")
    config_manager.update_pushover_config(user_key, app_token, preserve_blank=True)
    config_manager.load_config()
    gr.Info("Pushover設定を保存しました。")


def _get_paid_key_ui_updates(paid_key_names: Optional[List[str]] = None):
    """有料キー設定の正本から、DropdownとCheckboxGroupの更新値を作る。"""
    new_choices_for_ui = config_manager.get_api_key_choices_for_ui()
    valid_key_names = [key_name for _, key_name in new_choices_for_ui]
    valid_key_name_set = set(valid_key_names)

    source_paid_keys = (
        paid_key_names
        if isinstance(paid_key_names, list)
        else config_manager.CONFIG_GLOBAL.get("paid_api_key_names", [])
    )
    normalized_paid_keys = []
    for key_name in source_paid_keys:
        if not isinstance(key_name, str):
            continue
        cleaned_key_name = key_name.strip()
        if cleaned_key_name in valid_key_name_set and cleaned_key_name not in normalized_paid_keys:
            normalized_paid_keys.append(cleaned_key_name)

    return (
        gr.update(choices=new_choices_for_ui),
        gr.update(choices=valid_key_names, value=normalized_paid_keys),
    )


def _normalize_paid_key_names(paid_key_names: List[str], valid_key_names: set[str]) -> List[str]:
    """CheckboxGroupの値を、保存可能なキー名リストへ正規化する。"""
    normalized_paid_keys = []
    for key_name in paid_key_names:
        if not isinstance(key_name, str):
            continue
        cleaned_key_name = key_name.strip()
        if cleaned_key_name in valid_key_names and cleaned_key_name not in normalized_paid_keys:
            normalized_paid_keys.append(cleaned_key_name)
    return normalized_paid_keys


def handle_paid_keys_change(paid_key_names: List[str]):
    """有料キーチェックボックスが変更されたら即時保存する。"""
    if not isinstance(paid_key_names, list):
        gr.Warning("有料キーリストの更新に失敗しました。")
        config_manager.load_config()
        return _get_paid_key_ui_updates()

    config_manager.load_config()
    current_choices_for_ui = config_manager.get_api_key_choices_for_ui()
    valid_key_names = {key_name for _, key_name in current_choices_for_ui}
    normalized_paid_keys = _normalize_paid_key_names(paid_key_names, valid_key_names)
    saved_paid_keys = _normalize_paid_key_names(
        config_manager.CONFIG_GLOBAL.get("paid_api_key_names", []),
        valid_key_names,
    )

    if not _initialization_completed:
        # 初期ロード時の空配列エコーは保存済み有料キーを消さない。
        return _get_paid_key_ui_updates(saved_paid_keys)

    if config_manager.save_config_if_changed("paid_api_key_names", normalized_paid_keys):
        gr.Info("有料APIキーの設定を更新しました。")

    # グローバル変数を更新して即時反映
    config_manager.load_config()

    # ドロップダウンの表示も(Paid)ラベル付きで更新し、CheckboxGroupの値も正規化後に揃える。
    return _get_paid_key_ui_updates(normalized_paid_keys)


def load_paid_keys_display():
    """ページリロード時に有料キー設定を正本から復元する（保存はしない）。"""
    config_manager.load_config()
    return _get_paid_key_ui_updates()


def handle_rotation_setting_change(enabled: bool):
    """APIキーローテーション設定が変更されたら即時保存する。"""
    status_text = "有効" if enabled else "無効"
    return handle_save_global_setting_delta("enable_api_key_rotation", enabled, f"APIキー自動ローテーション {status_text}", skip_grace=True)


def handle_allow_external_connection_change(allow_external: bool):
    """外部接続設定が変更されたら即時保存する。"""
    status_text = "外部接続を許可" if allow_external else "外部接続を無効化"
    return handle_save_global_setting_delta("allow_external_connection", allow_external, status_text, restart_required=True, skip_grace=True)

def _save_notification_service_choice(config_key: str, service_choice: str, label: str):
    if service_choice in ["Discord", "Pushover"]:
        service_value = service_choice.lower()
        return handle_save_global_setting_delta(config_key, service_value, f"{label}「{service_choice}」", skip_grace=True)
    return gr.update()


def handle_alarm_notification_service_change(service_choice: str):
    return _save_notification_service_choice("alarm_notification_service", service_choice, "アラーム通知サービス")


def handle_user_notification_service_change(service_choice: str):
    return _save_notification_service_choice("user_notification_service", service_choice, "通知サービス")


def handle_notification_service_change(service_choice: str):
    return _save_notification_service_choice("notification_service", service_choice, "通知サービス")

def handle_save_moonshot_key(api_key: str):
    """Moonshot AI (Kimi) APIキーを保存する。"""
    if config_manager.save_config_if_changed("moonshot_api_key", api_key):
        gr.Info("Moonshot APIキーを保存しました。")
    config_manager.load_config()



def handle_weather_search(city_name: str) -> gr.update:
    """都市名をGeocoding APIで検索し、選択肢ドロップダウンを更新する"""
    if not city_name or not city_name.strip():
        gr.Warning("検索する都市名を入力してください。")
        return gr.update()
        
    service = WeatherService()
    results = service.search_city(city_name)
    if not results:
        gr.Warning(f"「{city_name}」に一致する場所が見つかりませんでした。英語名でもお試しください。")
        return gr.update(choices=[])
        
    choices = []
    for item in results:
        admin_part = f", {item['admin1']}" if item.get("admin1") else ""
        label = f"{item['name']}{admin_part} ({item['country']}) - 緯度:{item['latitude']:.2f}, 経度:{item['longitude']:.2f}"
        val = f"{item['latitude']},{item['longitude']}|{item['name']}"
        choices.append((label, val))
        
    gr.Info(f"{len(results)}件の候補が見つかりました。")
    return gr.update(choices=choices, value=choices[0][1] if choices else None)


def handle_weather_candidate_change(candidate_val: str) -> Tuple[gr.update, gr.update]:
    """ドロップダウン候補選択時に、経緯度表示用数値を更新する"""
    if not candidate_val or "|" not in candidate_val:
        return gr.update(value=None), gr.update(value=None)
        
    coords_part, _ = candidate_val.split("|")
    try:
        lat_str, lon_str = coords_part.split(",")
        return gr.update(value=float(lat_str)), gr.update(value=float(lon_str))
    except Exception:
        return gr.update(value=None), gr.update(value=None)


def handle_save_weather_settings(city_name: str, candidate_val: str, enable_context: bool, enable_scenery: bool) -> Tuple[gr.update, gr.update]:
    """天気設定をconfig.jsonにアトミックに保存し、現在の天気プレビューとステータスメッセージを更新する"""
    if not candidate_val or "|" not in candidate_val:
        gr.Warning("検索結果リストから場所を選択してください。")
        return gr.update(), "⚠️ 保存に失敗しました。場所が選択されていません。"
        
    coords_part, resolved_city_name = candidate_val.split("|")
    try:
        lat_str, lon_str = coords_part.split(",")
        lat = float(lat_str)
        lon = float(lon_str)
    except Exception:
        gr.Warning("緯度経度のパースに失敗しました。再度検索してください。")
        return gr.update(), "⚠️ 保存に失敗しました。経緯度データが破損しています。"
        
    # 新しい設定マップの作成
    weather_settings = {
        "city_name": resolved_city_name,
        "latitude": lat,
        "longitude": lon,
        "enable_persona_context": enable_context,
        "enable_scenery_reflection": enable_scenery
    }
    
    # アトミック保存
    if config_manager.save_config_if_changed("weather_settings", weather_settings):
        gr.Info("天気・環境連携設定を保存しました。")
    else:
        gr.Info("設定に変更はありませんでした。")
        
    # グローバル設定の再ロード
    config_manager.load_config()
    
    # プレビュー生成 (即時API叩いて現在の天気を確認)
    service = WeatherService()
    weather_data = service.fetch_weather(lat, lon)
    
    if weather_data:
        service.set_cached_weather(weather_data)
        # 日出・日没ベースの時間帯と、気温ベースの季節を計算してみる
        now_time = datetime.datetime.now().time()
        now_month = datetime.datetime.now().month
        
        season_ja, _ = service.get_enhanced_season(weather_data.temperature, now_month)
        time_ja, _ = service.get_enhanced_time_of_day(now_time, weather_data.sunrise, weather_data.sunset)
        
        status_md = (
            f"### 🌤️ 現在の環境ステータス (取得成功)\n"
            f"- **設定地域**: {resolved_city_name} (緯度: {lat:.4f}, 経度: {lon:.4f})\n"
            f"- **現在の天気**: {weather_data.weather_description} (気温: {weather_data.temperature:.1f}℃ / 体感: {weather_data.apparent_temperature:.1f}℃)\n"
            f"- **湿度 / 降水量**: {weather_data.humidity}% / {weather_data.precipitation}mm\n"
            f"- **日出 / 日没**: {weather_data.sunrise} / {weather_data.sunset}\n"
            f"- **動的季節判定**: **{season_ja}**\n"
            f"- **動的時間帯判定**: **{time_ja}**\n"
            f"\n*※ 30分間はキャッシュが利用され、APIリクエストを削減します。*\n"
            f"*※ 会話への反映は会話のたびに自動更新されます（最大30分間隔）。このパネルは「🔄 最新の天気に更新」または共通タブを開くと再評価します。*"
        )
    else:
        status_md = (
            f"### 🌤️ 現在の環境ステータス\n"
            f"- **設定地域**: {resolved_city_name} (緯度: {lat:.4f}, 経度: {lon:.4f})\n"
            f"- **天気情報**: ⚠️ API取得エラー。ネットワーク接続を確認してください。\n"
        )
        
    return gr.update(value=status_md), "共通設定: 最新状態を保存済み"


def handle_weather_manual_refresh() -> str:
    """設定を変更せず、天気を強制再取得してパネルを最新化する。"""
    config = config_manager.load_config_file()
    weather_settings = config.get("weather_settings", {})
    if (
        not weather_settings.get("city_name")
        or weather_settings.get("latitude") is None
        or weather_settings.get("longitude") is None
    ):
        gr.Warning("先に地域を検索して保存してください。")
        return get_weather_status_preview_html()

    service = WeatherService()
    weather_data = service.get_cached_weather(force_refresh=True)
    if weather_data is None:
        gr.Warning("天気の取得に失敗しました。ネットワーク接続を確認してください。")
    else:
        gr.Info("最新の天気に更新しました。")
    return get_weather_status_preview_html()


def get_weather_status_preview_html() -> str:
    """起動時またはルーム切替時の天気プレビュー表示用Markdownテキストを返す"""
    config = config_manager.load_config_file()
    weather_settings = config.get("weather_settings", {})
    city_name = weather_settings.get("city_name")
    lat = weather_settings.get("latitude")
    lon = weather_settings.get("longitude")
    
    if not city_name or lat is None or lon is None:
        return "*環境連携は未設定です。都市名を入力して検索し、保存してください。*"
        
    # キャッシュ経由で天気情報表示 (フリーズ防止)
    service = WeatherService()
    weather_data = service.get_cached_weather(cache_only=True)
    
    if weather_data:
        now_time = datetime.datetime.now().time()
        now_month = datetime.datetime.now().month
        season_ja, _ = service.get_enhanced_season(weather_data.temperature, now_month)
        time_ja, _ = service.get_enhanced_time_of_day(now_time, weather_data.sunrise, weather_data.sunset)

        # 取得時刻（fetched_at は ISO8601）。いつの天気かが分かるよう "YYYY-MM-DD HH:MM" で表示。
        fetched_display = str(weather_data.fetched_at or "")
        try:
            fetched_display = datetime.datetime.fromisoformat(weather_data.fetched_at).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            pass

        return (
            f"### 🌤️ 現在の環境ステータス (キャッシュ接続)\n"
            f"- **設定地域**: {city_name} (緯度: {lat:.4f}, 経度: {lon:.4f})\n"
            f"- **現在の天気**: {weather_data.weather_description} (気温: {weather_data.temperature:.1f}℃ / 体感: {weather_data.apparent_temperature:.1f}℃)\n"
            f"- **湿度 / 降水量**: {weather_data.humidity}% / {weather_data.precipitation}mm\n"
            f"- **日出 / 日没**: {weather_data.sunrise} / {weather_data.sunset}\n"
            f"- **動的季節判定**: **{season_ja}**\n"
            f"- **動的時間帯判定**: **{time_ja}**\n"
            f"- **取得時刻**: {fetched_display}\n"
            f"\n*※ 会話への反映は会話のたびに自動更新されます（最大30分間隔）。このパネルは「🔄 最新の天気に更新」または共通タブを開くと再評価します。*"
        )
    else:
        return (
            f"### 🌤️ 現在の環境ステータス (未取得)\n"
            f"- **設定地域**: {city_name} (緯度: {lat:.4f}, 経度: {lon:.4f})\n"
            f"- **天気情報**: まだ取得されていません（「🔄 最新の天気に更新」で取得できます）。"
        )


# =====================================================================
# Googleカレンダー連携設定（共通タブ・グローバル接続設定）
# OAuthは手動コード方式（ワーカーをブロックしない）を採用。
# 認証で得た refresh_token は auth/revoke ハンドラのみが管理し、
# 設定保存ハンドラは決して上書きしない（機密温存）。
# =====================================================================

_GCAL_REDIRECT_URI = "http://localhost"


def _gcal_settings() -> dict:
    return config_manager.load_config_file().get("google_calendar_settings", {}) or {}


def _save_gcal_partial(updates: dict) -> None:
    """既存の google_calendar_settings へマージ保存（refresh_token 等を温存）。"""
    settings = dict(_gcal_settings())
    settings.update(updates)
    config_manager.save_config_if_changed("google_calendar_settings", settings)
    config_manager.load_config()


def get_gcal_status_md() -> str:
    """Googleカレンダー連携の接続ステータスを返す。"""
    s = _gcal_settings()
    if not s.get("client_id") or not s.get("client_secret"):
        return "🔴 **未設定** — Client ID / Client Secret を入力して保存してください。"
    if not s.get("refresh_token"):
        return "🔴 **未認証** — 認証URLを生成し、コードを貼り付けて認証してください。"
    try:
        import google_calendar_service as gcal
        cache = gcal.load_cache()
        last = cache.get("last_synced_at") or "未同期"
    except Exception:
        last = "不明"
    cals = s.get("selected_calendars") or []
    state = "🟢 **有効・接続済み**" if s.get("enabled") else "⚪ **無効（同期停止中）・接続済み**"
    return f"{state}\n- 同期対象カレンダー: {len(cals)}件\n- 最終同期: {last}"


def _gcal_calendar_choices() -> list:
    """認証済みアカウントのカレンダー一覧を選択肢（label, id）にして返す。"""
    import google_calendar_service as gcal
    svc = gcal.GoogleCalendarService()
    out = []
    for c in svc.list_calendars():
        label = c.get("summary") or c.get("id")
        if c.get("primary"):
            label = f"{label}（メイン）"
        out.append((label, c.get("id")))
    return out


def refresh_gcal_settings_ui():
    """
    共通カレンダー設定UIを現在の保存値から復元する（ブラウザのページリロード対策）。
    ブラウザのリロードはPython側のUI構築を再実行しないため、build時の古い値（起動時のまま）
    が表示され、保存済みの同期対象カレンダー等が巻き戻って見える問題を防ぐ。

    【freeze対策】`.change` を持つ gcal_enabled_cb はここでは触らない（GRADIO_STARTUP_EVENT_WAR.md）。
    他のコンポーネントは .change を持たないため値設定は安全。
    Returns: (status_md, client_id, client_secret, calendar_select, sync_interval,
              exclude_keywords, mask_private, reminder_sync) の gr.update。
    """
    s = _gcal_settings()
    selected = list(s.get("selected_calendars") or [])
    pf = s.get("privacy_filter_default") or {}
    return (
        gr.update(value=get_gcal_status_md()),
        gr.update(value=s.get("client_id", "")),
        gr.update(value=s.get("client_secret", "")),
        gr.update(choices=[(c, c) for c in selected], value=selected),
        gr.update(value=s.get("sync_interval_minutes", 30)),
        gr.update(value=", ".join(pf.get("exclude_keywords", []))),
        gr.update(value=bool(pf.get("mask_private_events", True))),
        gr.update(value=bool(s.get("reminder_sync_enabled", True))),
    )


def load_room_calendar_settings(room_name: str):
    """
    ルーム切替時に、このルームのカレンダー個別設定（注入・リマインダー・書き込み先）を
    UIへ読み込む。ネットワークには触れず、設定とキャッシュのみ参照する。
    Returns: (inject_cb, reminder_cb, write_dropdown) の gr.update タプル。
    """
    try:
        import google_calendar_service as gcal
        cfg = gcal.get_room_calendar_override(room_name) if room_name else {}
        # 書き込み先カレンダー候補 = グローバルで同期対象に選択済みのカレンダー
        settings = gcal._get_settings()
        selected = settings.get("selected_calendars") or []
        choices = [("（書き込み無効）", "")] + [(cid, cid) for cid in selected]
        write_id = (cfg.get("persona_write_calendar_id") or "").strip()
        # 選択肢に無いIDが保存されていても表示できるよう補う
        if write_id and write_id not in selected:
            choices.append((write_id, write_id))
        return (
            gr.update(value=bool(cfg.get("inject_context", False))),
            gr.update(value=bool(cfg.get("reminder_enabled", False))),
            gr.update(choices=choices, value=write_id),
        )
    except Exception:
        traceback.print_exc()
        return gr.update(), gr.update(), gr.update()


def load_room_review_settings(room_name: str):
    """ルーム切替・初期化時に、このルームの自動レビュー設定をUIへ読み込む。

    巨大な統合出力リストは触らず、自前の小さな出力（反復回数・レビューモデルの provider/profile/model）
    だけ返す（load_room_calendar_settings と同じ安全パターン）。
    Returns: (review_iterations, review_provider, review_profile, review_model) の gr.update タプル。
    """
    try:
        settings = agent_delegation_manager.get_agent_delegation_settings(room_name) if room_name else {}
        iterations = int(settings.get("deleg_review_iterations") or 0)
        provider_cat = str(settings.get("deleg_review_provider_cat") or "").strip() or "default"
        profile = str(settings.get("deleg_review_openai_profile") or "").strip()
        model = str(settings.get("deleg_review_model") or "").strip()
        return (
            gr.update(value=iterations),
            gr.update(value=provider_cat),
            gr.update(value=profile, visible=(provider_cat == "openai")),
            gr.update(value=model),
        )
    except Exception:
        traceback.print_exc()
        return gr.update(), gr.update(), gr.update(), gr.update()


def handle_gcal_generate_url(client_id: str, client_secret: str) -> gr.update:
    """手動認証用のURLを生成し、Client ID/Secret を保存する。"""
    if not (client_id or "").strip() or not (client_secret or "").strip():
        gr.Warning("Client ID と Client Secret を入力してください。")
        return gr.update()
    try:
        import google_auth_helper as auth
        url = auth.generate_auth_url(client_id.strip(), client_secret.strip(), _GCAL_REDIRECT_URI)
    except Exception as e:
        gr.Warning(f"認証URLの生成に失敗しました: {e}")
        return gr.update()
    _save_gcal_partial({"client_id": client_id.strip(), "client_secret": client_secret.strip()})
    gr.Info("認証URLを生成しました。ブラウザで開いて承認後、リダイレクト先URL（localhost）の code= の値を貼り付けてください。")
    return gr.update(value=url)


def handle_gcal_exchange_code(client_id: str, client_secret: str, code: str):
    """認証コードをトークンに引き換え、カレンダー一覧を取得する。"""
    if not (code or "").strip():
        gr.Warning("認証コードを貼り付けてください。")
        return get_gcal_status_md(), gr.update()
    try:
        import google_auth_helper as auth
        result = auth.exchange_code(client_id.strip(), client_secret.strip(), _GCAL_REDIRECT_URI, code.strip())
    except Exception as e:
        gr.Warning(f"認証に失敗しました: {e}")
        return get_gcal_status_md(), gr.update()
    _save_gcal_partial({
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "refresh_token": result.get("refresh_token", ""),
    })
    gr.Info("認証に成功しました。カレンダー一覧を取得します。")
    try:
        choices = _gcal_calendar_choices()
    except Exception as e:
        gr.Warning(f"カレンダー一覧の取得に失敗しました（後で再取得できます）: {e}")
        choices = []
    return get_gcal_status_md(), gr.update(choices=choices)


def handle_gcal_refresh_calendars() -> gr.update:
    """認証済みアカウントのカレンダー一覧を再取得する。"""
    if not _gcal_settings().get("refresh_token"):
        gr.Warning("先に認証を完了してください。")
        return gr.update()
    try:
        choices = _gcal_calendar_choices()
    except Exception as e:
        gr.Warning(f"カレンダー一覧の取得に失敗しました: {e}")
        return gr.update()
    gr.Info(f"{len(choices)}件のカレンダーを取得しました。")
    return gr.update(choices=choices)


def handle_gcal_revoke():
    """認証を解除し、保存トークンを削除する。"""
    s = _gcal_settings()
    token = s.get("refresh_token")
    if token:
        try:
            import google_auth_helper as auth
            auth.revoke_token(token)
        except Exception:
            pass
    _save_gcal_partial({"refresh_token": "", "enabled": False})
    gr.Info("認証を解除しました。")
    return get_gcal_status_md(), gr.update(value=False)


def handle_gcal_toggle_enabled(enabled):
    """
    有効化チェックボックスの変更を即時保存する（保存ボタン不要・電源スイッチ的挙動）。

    【冪等性】GRADIO_STARTUP_EVENT_WAR.md の教訓に従い、永続値と同じなら何も書き込まずに
    即returnする。demo.load等のプログラム的な値設定でも .change は発火するため、これを
    入れないと起動/リロードのたびに load_config() を伴う重い書き込みが連鎖し、UIが固まる。
    """
    if bool(_gcal_settings().get("enabled")) == bool(enabled):
        return get_gcal_status_md()
    _save_gcal_partial({"enabled": bool(enabled)})
    return get_gcal_status_md()


def handle_save_gcal_settings(enabled, client_id, client_secret, selected_calendars,
                              sync_interval, exclude_keywords, mask_private, reminder_sync):
    """
    Googleカレンダーの共通設定を保存する。
    refresh_token は触らない（認証/解除ハンドラのみが管理）。
    client_id/secret は空UI値で既存を上書きしない（機密温存ガード）。
    """
    s = dict(_gcal_settings())
    s["enabled"] = bool(enabled)
    if (client_id or "").strip():
        s["client_id"] = client_id.strip()
    if (client_secret or "").strip():
        s["client_secret"] = client_secret.strip()
    s["selected_calendars"] = list(selected_calendars or [])
    try:
        s["sync_interval_minutes"] = max(5, int(sync_interval))
    except (ValueError, TypeError):
        s["sync_interval_minutes"] = 30
    keywords = [k.strip() for k in (exclude_keywords or "").split(",") if k.strip()]
    pf = dict(s.get("privacy_filter_default") or {})
    pf["exclude_keywords"] = keywords
    pf["mask_private_events"] = bool(mask_private)
    s["privacy_filter_default"] = pf
    s["reminder_sync_enabled"] = bool(reminder_sync)

    config_manager.save_config_if_changed("google_calendar_settings", s)
    config_manager.load_config()
    gr.Info("Googleカレンダー設定を保存しました。")
    return get_gcal_status_md(), "共通設定: 最新状態を保存済み"


def load_system_prompt_content(room_name: str) -> str:
    if not room_name: return ""
    _, system_prompt_path, _, _, _, _, _ = get_room_files_paths(room_name)
    if system_prompt_path and os.path.exists(system_prompt_path):
        with open(system_prompt_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def handle_save_system_prompt(room_name: str, content: str) -> None:
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return

    # ▼▼▼【ここに追加】▼▼▼
    room_manager.create_backup(room_name, 'system_prompt')

    _, system_prompt_path, _, _, _, _, _ = get_room_files_paths(room_name)
    if not system_prompt_path:
        gr.Error(f"「{room_name}」のプロンプトパス取得失敗。")
        return
    try:
        with open(system_prompt_path, "w", encoding="utf-8") as f:
            f.write(content)
        gr.Info(f"「{room_name}」の人格プロンプトを保存しました。")
    except Exception as e:
        gr.Error(f"人格プロンプトの保存エラー: {e}")

def handle_reload_system_prompt(room_name: str) -> str:
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return ""
    content = load_system_prompt_content(room_name)
    gr.Info(f"「{room_name}」の人格プロンプトを再読み込みしました。")
    return content

def handle_save_redaction_rules(rules_df: pd.DataFrame) -> Tuple[List[Dict[str, str]], pd.DataFrame]:
    """DataFrameの内容を検証し、jsonファイルに保存し、更新されたルールとDataFrameを返す。"""
    if rules_df is None:
        rules_df = pd.DataFrame(columns=["元の文字列 (Find)", "置換後の文字列 (Replace)"])

    # 列名が存在しない場合（空のDataFrameなど）に対応
    if '元の文字列 (Find)' not in rules_df.columns or '置換後の文字列 (Replace)' not in rules_df.columns:
        rules_df = pd.DataFrame(columns=["元の文字列 (Find)", "置換後の文字列 (Replace)"])

    rules = [
        {"find": str(row["元の文字列 (Find)"]), "replace": str(row["置換後の文字列 (Replace)"])}
        for index, row in rules_df.iterrows()
        if pd.notna(row["元の文字列 (Find)"]) and str(row["元の文字列 (Find)"]).strip()
    ]
    config_manager.save_redaction_rules(rules)
    gr.Info(f"{len(rules)}件の置換ルールを保存しました。チャット履歴を更新してください。")

    # 更新された（空行が除去された）DataFrameをUIに返す
    # まずPython辞書のリストから新しいDataFrameを作成
    updated_df_data = [{"元の文字列 (Find)": r["find"], "置換後の文字列 (Replace)": r["replace"]} for r in rules]
    updated_df = pd.DataFrame(updated_df_data)

    return rules, updated_df


def handle_stop_button_click(room_name, api_history_limit, add_timestamp, display_thoughts, screenshot_mode, redaction_rules):
    """
    ストップボタンが押されたときにUIの状態を即座にリセットし、ログから最新の状態を再描画する。
    """
    print("--- [UI] ユーザーによりストップボタンが押されました ---")
    # ログファイルから最新の履歴を再読み込みして、"思考中..." のような表示を消去する
    # ストリーミングジェネレータに停止を通知
    _stop_generation_event.set()
    history, mapping_list = reload_chat_log(room_name, api_history_limit, add_timestamp, display_thoughts, screenshot_mode, redaction_rules)

    # unified_streaming_outputs に合わせて16個の要素を返す
    # chatbot_display, current_log_map_state, chat_input_multimodal,
    # token_count_display, location_dropdown, current_scenery_display,
    # alarm_dataframe_original_data, alarm_dataframe, scenery_image_display,
    # debug_console_state, debug_console_output, stop_button, chat_reload_button,
    # action_button_group, profile_image_display, style_injector, translation_cache_state

    return (
        gr.update(value=history),                 # chatbot_display
        mapping_list,                             # current_log_map_state
        gr.update(interactive=True),              # chat_input_multimodal
        gr.update(),                              # token_count_display (更新なし)
        gr.update(),                              # location_dropdown
        gr.update(),                              # current_scenery_display
        gr.update(),                              # alarm_dataframe_original_data
        gr.update(),                              # alarm_dataframe
        gr.update(),                              # scenery_image_display
        gr.update(),                              # debug_console_state
        gr.update(),                              # debug_console_output
        gr.update(visible=False, interactive=True), # stop_button
        gr.update(interactive=True),              # chat_reload_button
        gr.update(),                              # action_button_group
        gr.update(),                              # profile_image_display
        gr.update(),                              # style_injector
        gr.update()                               # translation_cache_state
    )


def handle_log_punctuation_correction(
    confirmed: bool,
    selected_message: Optional[Dict],
    room_name: str,
    api_key_name: str,
    api_history_limit: str,
    add_timestamp: bool
) -> Tuple[gr.update, gr.update, gr.update, Optional[Dict], gr.update, str]:
    """
    【v3: 堅牢化版】
    選択行以降のAGENT応答を「思考ログ」と「本文」に分離し、それぞれ安全に読点修正を行ってから再結合する。
    """
    if not confirmed or str(confirmed).lower() != 'true':
        yield gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), ""
        return

    if not selected_message:
        gr.Warning("修正の起点となるメッセージをチャット履歴から選択してください。")
        yield gr.update(), gr.update(), gr.update(), None, gr.update(visible=False), ""
        return
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーが選択されていません。")
        yield gr.update(), gr.update(), gr.update(), selected_message, gr.update(visible=True), ""
        return

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー '{api_key_name}' が有効ではありません。")
        yield gr.update(), gr.update(), gr.update(), selected_message, gr.update(visible=True), ""
        return

    yield gr.update(), gr.update(), gr.update(value="準備中...", interactive=False), gr.update(), gr.update(), ""

    try:
        # ▼▼▼【この try ブロックの先頭にある backup_path = ... の行を、これで置き換えてください】▼▼▼
        backup_path = room_manager.create_backup(room_name, 'log')
        # ▲▲▲【置き換えはここまで】▲▲▲

        if not backup_path:
            gr.Error("ログのバックアップ作成に失敗しました。処理を中断します。")
            yield gr.update(), gr.update(), gr.update(interactive=True), selected_message, gr.update(visible=True), ""
            return

        log_f, _, _, _, _, _, _ = get_room_files_paths(room_name)
        all_messages = utils.load_chat_log(log_f)

        start_index = next((i for i, msg in enumerate(all_messages) if msg == selected_message), -1)

        if start_index == -1:
            gr.Warning("選択されたメッセージがログに見つかりませんでした。")
            yield gr.update(), gr.update(), gr.update(interactive=True), None, gr.update(visible=False), ""
            return

        targets_with_indices = [
            (i, msg) for i, msg in enumerate(all_messages)
            if i >= start_index and msg.get("role") == "AGENT"
        ]

        if not targets_with_indices:
            gr.Info("選択範囲に修正対象となるAIの応答がありませんでした。")
            yield gr.update(), gr.update(), gr.update(interactive=True), None, gr.update(visible=False), ""
            return

        total_targets = len(targets_with_indices)
        for i, (original_index, msg_to_fix) in enumerate(targets_with_indices):
            progress_text = f"修正中... ({i + 1}/{total_targets}件)"
            yield gr.update(), gr.update(), gr.update(value=progress_text), gr.update(), gr.update(), ""

            original_content = msg_to_fix.get("content", "")

            # --- [新アーキテクチャ：分割・修正・再結合] ---

            # 1. 【分割】コンテンツを3つのパーツに分離
            # 後方互換性: 新形式 [THOUGHT] と旧形式 【Thoughts】 の両方に対応
            thoughts_pattern = re.compile(r"(\[THOUGHT\][\s\S]*?\[/THOUGHT\]|【Thoughts】[\s\S]*?【/Thoughts】)", re.IGNORECASE)
            # 共通関数を使ってタイムスタンプを除去
            body_part = utils.remove_ai_timestamp(original_content)

            # 2. 【個別修正】各パーツをAIで修正
            corrected_thoughts = ""
            if thoughts_part:
                # 思考ログからタグを除いた中身だけをAIに渡す
                # 新形式と旧形式の両方のタグを除去
                inner_thoughts = re.sub(r"\[/?THOUGHT\]|【/?Thoughts】", "", thoughts_part, flags=re.IGNORECASE).strip()
                text_to_fix = inner_thoughts.replace("、", "").replace("､", "")
                result = gemini_api.correct_punctuation_with_ai(text_to_fix, api_key, context_type="thoughts")
                # 安全装置：AIが失敗したら元のテキストを使う
                if result and len(result) > len(inner_thoughts) * 0.5:
                    # 元のタグ形式を維持 (新形式 [THOUGHT] か旧形式 【Thoughts】)
                    if "[THOUGHT]" in thoughts_part.upper():
                        corrected_thoughts = f"[THOUGHT]\n{result.strip()}\n[/THOUGHT]"
                    else:
                        corrected_thoughts = f"【Thoughts】\n{result.strip()}\n【/Thoughts】"
                else:
                    corrected_thoughts = thoughts_part

            corrected_body = ""
            if body_part:
                text_to_fix = body_part.replace("、", "").replace("､", "")
                result = gemini_api.correct_punctuation_with_ai(text_to_fix, api_key, context_type="body")
                # 安全装置：AIが失敗したら元のテキストを使う
                corrected_body = result if result and len(result) > len(body_part) * 0.5 else body_part

            # 3. 【再結合】パーツを結合してメッセージを更新
            # パーツ間に適切な改行を入れる。タイムスタンプの前には2つの改行を入れるのがNexus Arkの標準。
            final_content = ""
            if corrected_thoughts:
                final_content += corrected_thoughts + "\n\n"

            final_content += corrected_body

            if timestamp_part:
                # 既に body_part の末尾に改行があるかもしれないので、調整して付与
                final_content = final_content.strip() + "\n\n" + timestamp_part.strip()

            all_messages[original_index]["content"] = final_content.strip()
            # --- [アーキテクチャここまで] ---

        utils._overwrite_log_file(log_f, all_messages)
        gr.Info(f"✅ {total_targets}件のAI応答の読点を修正し、ログを更新しました。")

    except Exception as e:
        gr.Error(f"ログ修正処理中に予期せぬエラーが発生しました: {e}")
        traceback.print_exc()
    finally:
        final_history, final_mapping = reload_chat_log(room_name, api_history_limit, add_timestamp)
        yield final_history, final_mapping, gr.update(value="選択発言以降の読点をAIで修正", interactive=True), None, gr.update(visible=False), ""

# ▲▲▲【追加はここまで】▲▲▲

def handle_avatar_upload(room_name: str, uploaded_file_path: Optional[str]) -> Tuple[Optional[str], gr.update, gr.update, gr.update, gr.update]:
    """
    ユーザーが新しいアバターをアップロードした際の処理。
    - 動画ファイル (mp4, webm, gif) の場合: 直接 avatar/idle.{ext} に保存
    - 画像ファイルの場合: 従来通りクロップUIを表示

    GradioのUploadButtonは、一時ファイルのパス(文字列)を直接渡してくる。
    """
    if uploaded_file_path is None:
        return None, gr.update(visible=False), gr.update(visible=False), gr.update(), gr.update()

    # 拡張子で動画かどうかを判定
    ext = os.path.splitext(uploaded_file_path)[1].lower()
    video_extensions = {'.mp4', '.webm', '.gif'}

    if ext in video_extensions:
        # 動画ファイルの場合: 直接保存
        if not room_name:
            gr.Warning("アバターを保存するルームが選択されていません。")
            return None, gr.update(visible=False), gr.update(visible=False), gr.update(), gr.update()

        try:
            # avatarディレクトリを作成
            avatar_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.AVATAR_DIR)
            os.makedirs(avatar_dir, exist_ok=True)

            # 既存の idle ファイルを削除 (拡張子が異なる可能性があるため)
            for old_ext in video_extensions:
                old_file = os.path.join(avatar_dir, f"idle{old_ext}")
                if os.path.exists(old_file):
                    os.remove(old_file)

            # 新しいファイルを保存
            target_path = os.path.join(avatar_dir, f"idle{ext}")
            shutil.copy2(uploaded_file_path, target_path)
            room_manager.update_room_config(room_name, {"avatar_mode": "video"})

            gr.Info(f"ルーム「{room_name}」のアバター動画を更新しました。")

            # プロフィール表示を更新し、クロップUIは非表示のまま
            return (
                None,
                gr.update(visible=False),
                gr.update(visible=False),
                gr.update(open=False),
                gr.update(value=get_avatar_html(room_name, state="idle", mode="video"))
            )

        except Exception as e:
            gr.Error(f"動画アバターの保存中にエラーが発生しました: {e}")
            traceback.print_exc()
            return None, gr.update(visible=False), gr.update(visible=False), gr.update(), gr.update()

    else:
        # 画像ファイルの場合: 従来通りクロップUIを表示
        return (
            uploaded_file_path,
            gr.update(value=uploaded_file_path, visible=True),
            gr.update(visible=True),
            gr.update(open=True),
            gr.update()  # profile_image_display は変更しない
        )


def handle_thinking_avatar_upload(room_name: str, uploaded_file_path: Optional[str]) -> None:
    """
    思考中アバター動画をアップロードした際の処理。
    動画を avatar/thinking.{ext} として保存する。
    """
    if uploaded_file_path is None:
        return

    if not room_name:
        gr.Warning("アバターを保存するルームが選択されていません。")
        return

    ext = os.path.splitext(uploaded_file_path)[1].lower()
    video_extensions = {'.mp4', '.webm', '.gif'}

    if ext not in video_extensions:
        gr.Warning("思考中アバターは動画ファイル (mp4, webm, gif) のみ対応しています。")
        return

    try:
        avatar_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.AVATAR_DIR)
        os.makedirs(avatar_dir, exist_ok=True)

        # 既存の thinking ファイルを削除
        for old_ext in video_extensions:
            old_file = os.path.join(avatar_dir, f"thinking{old_ext}")
            if os.path.exists(old_file):
                os.remove(old_file)

        # 新しいファイルを保存
        target_path = os.path.join(avatar_dir, f"thinking{ext}")
        shutil.copy2(uploaded_file_path, target_path)

        gr.Info(f"ルーム「{room_name}」の思考中アバター動画を保存しました。")

    except Exception as e:
        gr.Error(f"思考中アバターの保存中にエラーが発生しました: {e}")
        traceback.print_exc()


def handle_avatar_mode_change(room_name: str, mode: str) -> gr.update:
    """
    アバターモードが変更された際に、設定を保存し表示を更新する。

    Args:
        room_name: ルームのフォルダ名
        mode: "static" または "video"

    Returns:
        profile_image_display の更新
    """
    if not room_name:
        return gr.update()

    # 現在のモードを取得して比較
    effective_settings = config_manager.get_effective_settings(room_name)
    current_mode = effective_settings.get("avatar_mode", "static")

    # 変更がある場合のみ保存と通知
    if mode != current_mode:
        room_manager.update_room_config(room_name, {"avatar_mode": mode})
        mode_name = "静止画" if mode == "static" else "動画"
        gr.Info(f"アバターモードを「{mode_name}」に変更しました。")

    # 新しいモードでアバターを再生成し、表情カードリストも更新する
    return (
        gr.update(value=get_avatar_html(room_name, state="idle", mode=mode)),
        refresh_expressions_ui(room_name)
    )


def get_avatar_mode_for_room(room_name: str) -> gr.update:
    """
    ルーム切り替え時に avatar_mode_radio を正しい値に更新する。

    Args:
        room_name: ルームのフォルダ名

    Returns:
        avatar_mode_radio の gr.update
    """
    if not room_name:
        return gr.update(value="static")

    effective_settings = config_manager.get_effective_settings(room_name)
    mode = effective_settings.get("avatar_mode", "static")

    # room_config.json から直接読み込む（effective_settings に含まれていない場合）
    room_config = room_manager.get_room_config(room_name) or {}
    mode = room_config.get("avatar_mode", mode)

    return gr.update(value=mode)


# ===== 表情リスト管理ハンドラ =====

def refresh_expressions_ui(room_name: str) -> str:
    """
    表情リストをカード形式のHTMLとして生成する。
    """
    if not room_name:
        return '<div style="padding:20px; text-align:center; color:var(--text-color-secondary);">ルームを選択してください。</div>'

    # ルーム設定から現在のモードを取得
    effective_settings = config_manager.get_effective_settings(room_name)
    avatar_mode = effective_settings.get("avatar_mode", "static")
    avatar_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.AVATAR_DIR)

    image_exts = [".png", ".jpg", ".jpeg", ".webp"]
    video_exts = [".mp4", ".webm", ".gif"]

    expressions_config = room_manager.get_expressions_config(room_name)

    html = '<div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; margin-top: 10px;">'

    # 順序の定義
    fixed_at_top = ["idle", "thinking"]
    standard_emotions = constants.DEFAULT_EXPRESSIONS # neutral, joy, anxious, sadness, anger

    # 重複を除去しつつ全ての表情をリスト化
    all_registered = expressions_config.get("expressions", [])
    custom_exprs = [e for e in all_registered if e not in fixed_at_top and e not in standard_emotions]

    all_to_show = fixed_at_top + standard_emotions + sorted(custom_exprs)

    for expr in all_to_show:
        # モードに応じて優先的にファイルを探す
        file_path = None
        if avatar_mode == "static":
            # 静止画優先
            for ext in image_exts:
                p = os.path.join(avatar_dir, f"{expr}{ext}")
                if os.path.exists(p):
                    file_path = p
                    break
            if not file_path: # フォールバック
                for ext in video_exts:
                    p = os.path.join(avatar_dir, f"{expr}{ext}")
                    if os.path.exists(p):
                        file_path = p
                        break
        else:
            # 動画優先
            for ext in video_exts:
                p = os.path.join(avatar_dir, f"{expr}{ext}")
                if os.path.exists(p):
                    file_path = p
                    break
            if not file_path: # フォールバック
                for ext in image_exts:
                    p = os.path.join(avatar_dir, f"{expr}{ext}")
                    if os.path.exists(p):
                        file_path = p
                        break

        preview_html = ""

        if file_path and os.path.exists(file_path):
            try:
                # Base64で埋め込み（get_avatar_htmlと同様）
                with open(file_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode("utf-8")

                ext = os.path.splitext(file_path)[1].lower()
                if ext in [".mp4", ".webm"]:
                    mime = f"video/{ext[1:]}"
                    preview_html = f'<video src="data:{mime};base64,{encoded}" style="width:100%; height:110px; object-fit:cover; border-radius:6px; background:#000;" muted loop autoplay playsinline></video>'
                elif ext == ".gif":
                    preview_html = f'<img src="data:image/gif;base64,{encoded}" style="width:100%; height:110px; object-fit:cover; border-radius:6px; background:#000;" />'
                else:
                    mime = "image/png" if ext == ".png" else "image/jpeg"
                    preview_html = f'<img src="data:{mime};base64,{encoded}" style="width:100%; height:110px; object-fit:cover; border-radius:6px; background:#000;" />'
            except Exception as e:
                preview_html = f'<div style="width:100%; height:110px; background:#333; border-radius:6px; display:flex; align-items:center; justify-content:center; color:#f66; font-size:10px;">エラー: {str(e)[:20]}</div>'
        else:
            preview_html = '<div style="width:100%; height:110px; background:var(--background-fill-primary); border-radius:6px; display:flex; align-items:center; justify-content:center; color:var(--text-color-secondary); font-size:12px; border:1px dashed var(--border-color-primary);">未登録</div>'

        # ラベルとスタイルの決定
        is_fixed = expr in fixed_at_top
        is_standard = expr in standard_emotions

        jp_name = constants.EXPRESSION_NAMES_JP.get(expr, "")
        display_label = f"{expr} ({jp_name})" if jp_name else expr

        if is_fixed:
            tag_text = "固定"
            tag_style = "background:var(--secondary-500); color:white;"
        elif is_standard:
            tag_text = "感情"
            tag_style = "background:var(--primary-500); color:white;"
        else:
            tag_text = "カスタム"
            tag_style = "background:var(--neutral-500); color:white;"

        html += f'''
        <div style="background: var(--background-fill-secondary); padding: 10px; border-radius:10px; border: 1px solid var(--border-color-primary); box-shadow: var(--shadow-sm);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                <span style="font-size: 13px; font-weight: bold; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{display_label}</span>
                <span style="font-size: 10px; padding: 2px 6px; border-radius: 4px; {tag_style}">{tag_text}</span>
            </div>
            {preview_html}
        </div>
        '''

    html += '</div>'
    return html

def refresh_expressions_list(room_name: str) -> gr.update:
    """[DEPRECATED] 表情リストをカードHTMLとして返すように更新"""
    return gr.update(value=refresh_expressions_ui(room_name))


def get_all_expression_choices(room_name: str) -> list:
    """
    ドロップダウン用の統合表情リストを返す。
    idle, thinking + expressions.json + DEFAULT_EXPRESSIONS（重複除去）
    """
    base = ["idle", "thinking"]
    config_expressions = room_manager.get_expressions_config(room_name).get("expressions", []) if room_name else []

    result = base.copy()
    for e in config_expressions + constants.DEFAULT_EXPRESSIONS:
        if e not in result:
            result.append(e)
    return result



def handle_add_expression(room_name: str, expression_name: str) -> tuple:
    """
    新しい表情を追加する（または既存の定義を維持する）。
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update()

    if not expression_name or not expression_name.strip():
        gr.Warning("表情名を入力してください。")
        return gr.update(), gr.update()

    expression_name = expression_name.strip().lower()

    # 表情設定を読み込み
    expressions_config = room_manager.get_expressions_config(room_name)

    if expression_name not in expressions_config["expressions"]:
        expressions_config["expressions"].append(expression_name)
        room_manager.save_expressions_config(room_name, expressions_config)
        gr.Info(f"表情「{expression_name}」を追加しました。ファイルをアップロードして紐付けてください。")
    else:
        gr.Info(f"表情「{expression_name}」は既に登録されています。")

    # UIを更新
    return (
        refresh_expressions_ui(room_name),
        gr.update(value="", choices=get_all_expression_choices(room_name)) # 値と選択肢を一括更新
    )


def handle_delete_expression(room_name: str, expression_name: str) -> tuple:
    """
    指定した表情を削除する。
    """
    if not room_name or not expression_name:
        gr.Warning("削除する表情を選択してください。")
        return gr.update(), gr.update()

    if expression_name in ["idle", "thinking"]:
        gr.Warning(f"「{expression_name}」はシステム予約済み（状態表示用）のため削除できません。アセット（画像/動画）の差し替えのみ可能です。")
        return gr.update(), gr.update()

    # 表情設定を読み込み
    expressions_config = room_manager.get_expressions_config(room_name)

    if expression_name in expressions_config["expressions"]:
        expressions_config["expressions"].remove(expression_name)
        room_manager.save_expressions_config(room_name, expressions_config)
        gr.Info(f"表情「{expression_name}」をリストから削除しました。")

    # 注意: アセットファイル自体は削除しない（誤操作防止のため。必要なら手動削除）


    return (
        refresh_expressions_ui(room_name),
        gr.update(choices=get_all_expression_choices(room_name), value=None)
    )


def handle_expression_file_upload(file_path: str, room_name: str, expression_name: str) -> tuple:
    """
    表情用のファイル（画像/動画）をアップロードして保存する。

    NOTE: Gradioの.upload()イベントでは、ファイルパスが最初の引数として渡され、
    その後にinputsリストで指定したコンポーネントの値が順に渡される。

    Args:
        file_path: アップロードされたファイルのパス (自動的に最初に渡される)
        room_name: ルームのフォルダ名 (inputs[0])
        expression_name: 表情名 (inputs[1])

    Returns:
        (expressions_df, ...) の更新
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update(), gr.update()

    if not expression_name or not expression_name.strip():
        gr.Warning("先に表情名を入力してください。")
        return gr.update(), gr.update(), gr.update()

    if not file_path or not os.path.exists(file_path):
        gr.Warning("ファイルが見つかりません。")
        return gr.update(), gr.update(), gr.update()

    expression_name = expression_name.strip().lower()

    # avatar ディレクトリを確保
    avatar_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.AVATAR_DIR)
    os.makedirs(avatar_dir, exist_ok=True)

    # ファイル拡張子を取得
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()

    # 保存先パス
    dest_path = os.path.join(avatar_dir, f"{expression_name}{ext}")

    try:
        shutil.copy2(file_path, dest_path)
        print(f"--- [Expression] ファイルを保存: {dest_path} ---")

        # 表情がリストになければ追加
        expressions_config = room_manager.get_expressions_config(room_name)
        if expression_name not in expressions_config["expressions"]:
            expressions_config["expressions"].append(expression_name)
            room_manager.save_expressions_config(room_name, expressions_config)

        gr.Info(f"表情「{expression_name}」のファイルを保存しました。")

    except Exception as e:
        gr.Error(f"ファイルの保存に失敗しました: {e}")
        traceback.print_exc()

    return (
        refresh_expressions_ui(room_name),
        gr.update(choices=get_all_expression_choices(room_name))
    )


def handle_save_cropped_image(room_name: str, original_image_path: str, cropped_image_data: Dict) -> Tuple[gr.update, gr.update, gr.update]:
    """
    ユーザーが「この範囲で保存」ボタンを押した際に、
    トリミングされた画像を'profile.png'として保存し、UIを更新する。
    """
    if not room_name:
        gr.Warning("画像を変更するルームが選択されていません。")
        return gr.update(), gr.update(visible=False), gr.update(visible=False)

    if original_image_path is None or cropped_image_data is None:
        gr.Warning("元画像またはトリミング範囲のデータがありません。")
        return gr.update(), gr.update(visible=False), gr.update(visible=False)

    try:
        # Gradioの 'ImageEditor' は、type="pil" の場合、
        # 編集後の画像をPIL Imageオブジェクトとして 'composite' キーに格納します。
        # ただし、ユーザーが編集操作（クロップ範囲選択など）をしなかった場合、
        # 'composite' が None になることがあるため、'background' にフォールバックします。
        cropped_img = cropped_image_data.get("composite") or cropped_image_data.get("background")

        if cropped_img is None:
            gr.Warning("画像データが取得できませんでした。画像を再度アップロードしてください。")
            return gr.update(), gr.update(visible=False), gr.update(visible=False)

        save_path = os.path.join(constants.ROOMS_DIR, room_name, constants.PROFILE_IMAGE_FILENAME)

        cropped_img.save(save_path, "PNG")

        gr.Info(f"ルーム「{room_name}」のプロフィール画像を更新しました。")

        # 最終的なプロフィール画像表示を更新し、編集用UIを非表示に戻す
        # gr.HTML用にget_avatar_htmlでHTML文字列を生成
        return (
            gr.update(value=get_avatar_html(room_name, state="idle")),
            gr.update(value=None, visible=False),
            gr.update(visible=False)
        )

    except Exception as e:
        gr.Error(f"トリミング画像の保存中にエラーが発生しました: {e}")
        traceback.print_exc()
        # エラーが発生した場合、元のプロフィール画像表示は変更せず、編集UIのみを閉じる
        return gr.update(value=get_avatar_html(room_name, state="idle")), gr.update(visible=False), gr.update(visible=False)



def handle_chatbot_edit(
    updated_chatbot_value: list,
    room_name: str,
    api_history_limit: str,
    mapping_list: list,
    add_timestamp: bool,
    display_thoughts: bool,
    translation_cache: dict,
    show_translation: bool,
    evt: gr.EditData
):
    """
    GradioのChatbot編集イベントを処理するハンドラ。
    Gradio 6の .edit は gr.EditData.index/value を渡すため、表示済みHTMLの
    丸ごと逆変換ではなく、元ログを基準に編集対象パートだけを差し替える。
    """
    if not room_name or evt.index is None or not mapping_list:
        return gr.update(), gr.update(), translation_cache

    try:
        room_manager.create_backup(room_name, 'log')

        # --- [ステップ1: 必要な情報を取得] ---
        edited_ui_index = _chatbot_event_message_index(evt.index)
        if edited_ui_index is None:
            return gr.update(), gr.update(), translation_cache
        if not (0 <= edited_ui_index < len(updated_chatbot_value)) or not (0 <= edited_ui_index < len(mapping_list)):
            gr.Warning("編集対象のメッセージを特定できませんでした。")
            return gr.update(), gr.update(), translation_cache
        edited_message = updated_chatbot_value[edited_ui_index]
        edited_text = _normalize_chat_edit_text(getattr(evt, "value", None), edited_message)

        log_f, _, _, _, _, _, _ = get_room_files_paths(room_name)
        all_messages = utils.load_chat_log(log_f)
        original_log_index = mapping_list[edited_ui_index]

        if not (0 <= original_log_index < len(all_messages)):
            gr.Error(f"編集対象のメッセージを特定できませんでした。(インデックス範囲外: {original_log_index})")
            return gr.update(), gr.update(), translation_cache

        original_message = all_messages[original_log_index]
        original_content = original_message.get('content', '')

        content_without_ts, preserved_timestamp = _split_timestamp_suffix(original_content)
        parsed_parts = _parse_log_content_parts(content_without_ts, remove_thoughts=False)
        if not parsed_parts:
            parsed_parts = [{"type": "text", "content": ""}]

        edited_is_thought = _is_thought_chat_message(edited_message)
        if edited_is_thought:
            if show_translation:
                gr.Warning("翻訳表示中の思考ログ編集は、原文への誤保存を防ぐため反映しません。原文表示に戻してから編集してください。")
                history, new_mapping_list = reload_chat_log(
                    room_name,
                    api_history_limit,
                    add_timestamp,
                    display_thoughts=display_thoughts,
                    translation_cache=translation_cache,
                    show_translation=show_translation
                )
                return history, new_mapping_list, translation_cache

            thought_index = _thought_index_for_ui_message(updated_chatbot_value, mapping_list, edited_ui_index, original_log_index)
            thought_parts = [part for part in parsed_parts if part.get("type") == "thought"]
            if thought_index < 0 or thought_index >= len(thought_parts):
                gr.Warning("編集対象の思考ログを特定できませんでした。")
                return gr.update(), gr.update(), translation_cache
            thought_parts[thought_index]["content"] = edited_text.strip()
        else:
            new_body_text = _strip_chat_edit_display_chrome(edited_text)
            first_text_updated = False
            new_parts: List[Dict[str, str]] = []
            for part in parsed_parts:
                if part.get("type") == "text":
                    if not first_text_updated:
                        if new_body_text:
                            new_parts.append({**part, "content": new_body_text})
                        first_text_updated = True
                    elif part.get("content", "").strip():
                        new_parts.append(part)
                else:
                    new_parts.append(part)
            if not first_text_updated and new_body_text:
                new_parts.append({"type": "text", "content": new_body_text})
            parsed_parts = new_parts

        final_content = _serialize_log_content_parts(parsed_parts, preserved_timestamp)

        # --- [ステップ5: ログの上書きとUIの更新] ---
        original_message['content'] = final_content
        utils._overwrite_log_file(log_f, all_messages)
        if translation_cache and original_log_index in translation_cache:
            translation_cache = dict(translation_cache)
            translation_cache.pop(original_log_index, None)

        gr.Info(f"メッセージを編集し、ログを更新しました。")

    except Exception as e:
        gr.Error(f"メッセージの編集中にエラーが発生しました: {e}")
        traceback.print_exc()

    history, new_mapping_list = reload_chat_log(
        room_name,
        api_history_limit,
        add_timestamp,
        display_thoughts=display_thoughts,
        translation_cache=translation_cache,
        show_translation=show_translation
    )
    return history, new_mapping_list, translation_cache

def handle_save_backup_rotation_count(count: int):
    """バックアップの最大保存件数をconfig.jsonに保存する。"""
    if count is None or not isinstance(count, (int, float)) or count < 1:
        gr.Warning("バックアップ保存件数は1以上の整数で指定してください。")
        return "共通設定: バックアップ保存件数は1以上の整数で指定してください"

    int_count = int(count)
    return handle_save_global_setting_delta("backup_rotation_count", int_count, f"バックアップ最大保存件数 {int_count} 件", skip_grace=True)

def handle_open_backup_folder(room_name: str):
    """選択されたルームのバックアップフォルダをOSのファイルエクスプローラーで開く。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return

    backup_path = os.path.join(constants.ROOMS_DIR, room_name, "backups")
    # フォルダの存在を念のため確認
    if not os.path.isdir(backup_path):
        # 存在しない場合は作成を試みる
        try:
            os.makedirs(backup_path, exist_ok=True)
        except Exception as e:
            gr.Warning(f"バックアップフォルダの作成に失敗しました: {backup_path}\n{e}")
            return

    try:
        if sys.platform == "win32":
            os.startfile(os.path.normpath(backup_path))
        elif sys.platform == "darwin": # macOS
            subprocess.Popen(["open", backup_path])
        else: # Linux
            subprocess.Popen(["xdg-open", backup_path])
        gr.Info(f"「{room_name}」のバックアップフォルダを開きました。")
    except Exception as e:
        gr.Error(f"フォルダを開けませんでした: {e}")

# --- [ここからが追加する関数] ---
SCENERY_SEASON_EN_TO_JA = {
    "spring": "春",
    "early_spring": "春",
    "summer": "夏",
    "early_summer": "夏",
    "late_summer": "夏",
    "autumn": "秋",
    "late_autumn": "秋",
    "winter": "冬",
}
SCENERY_SEASON_JA_TO_EN = {"春": "spring", "夏": "summer", "秋": "autumn", "冬": "winter"}
SCENERY_TIME_EN_TO_JA = {
    "early_morning": "早朝",
    "morning": "朝",
    "late_morning": "昼前",
    "noon": "昼",
    "daytime": "昼",
    "afternoon": "昼下がり",
    "evening": "夕方",
    "night": "夜",
    "midnight": "深夜",
}
SCENERY_TIME_JA_TO_EN = {
    "早朝": "early_morning",
    "朝": "morning",
    "昼前": "late_morning",
    "昼": "daytime",
    "昼下がり": "afternoon",
    "夕方": "evening",
    "夜": "night",
    "深夜": "midnight",
}


def _get_room_time_settings_dict(room_name: str) -> Dict[str, Any]:
    """旧トップレベル形式とoverride_settings内形式の両方から時間設定を読む。"""
    room_config = room_manager.get_room_config(room_name) or {}
    top_level = room_config.get("time_settings")
    if isinstance(top_level, dict):
        return top_level

    override_settings = room_config.get("override_settings", {}) or {}
    nested = override_settings.get("time_settings")
    if isinstance(nested, dict):
        return nested

    return {}


def _get_current_time_context_ui_values(room_name: str) -> Tuple[str, str]:
    """現在有効な季節・時間帯をUI表示用の日本語に変換する。"""
    season_en, time_en = utils._get_current_time_context(room_name)
    return (
        SCENERY_SEASON_EN_TO_JA.get(season_en, "秋"),
        SCENERY_TIME_EN_TO_JA.get(time_en, "夜"),
    )


def _load_image_for_gradio(image_path: Optional[str]):
    """同一ファイルパス上書き後も古いプレビューを残さないよう、画像実体を読み込んで返す。"""
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        with Image.open(image_path) as raw_img:
            img = ImageOps.exif_transpose(raw_img) or raw_img
            return img.copy()
    except Exception as e:
        print(f"--- [Scenery Image] 画像読み込みエラー ({image_path}): {e} ---")
        return image_path


def _load_time_settings_for_room(room_name: str) -> Dict[str, Any]:
    """ルームの設定ファイルから時間設定を読み込むヘルパー関数。"""
    settings = _get_room_time_settings_dict(room_name)

    mode = settings.get("mode", "realtime")

    # [v10] ロード時のフォールバックを現在時刻に合わせる
    # これにより、3月に「リアル連動」でロードした際にUIが初期値「秋」にならず「春」になり、
    # 意図せぬ保存イベント（Event Storm）での「固定(fixed)」への上書きを防ぐ。
    now = datetime.datetime.now()
    default_season_en = utils.get_season(now.month)
    default_time_en = utils.get_time_of_day(now.hour)

    season_en = settings.get("fixed_season", default_season_en)
    time_en = settings.get("fixed_time_of_day", default_time_en)

    return {
        "mode": "リアル連動" if mode == "realtime" else "選択する",
        "fixed_season_ja": SCENERY_SEASON_EN_TO_JA.get(season_en, SCENERY_SEASON_EN_TO_JA.get(default_season_en, "秋")),
        "fixed_time_of_day_ja": SCENERY_TIME_EN_TO_JA.get(time_en, SCENERY_TIME_EN_TO_JA.get(default_time_en, "夜")),
    }



def handle_time_mode_change(mode: str) -> gr.update:
    """時間設定のモードが変更されたときに、詳細設定UIの表示/非表示を切り替える。"""
    return gr.update(visible=(mode == "選択する"))


def handle_save_time_settings(room_name: str, mode: str, season_ja: str, time_of_day_ja: str):
    """ルームの時間設定を `room_config.json` に保存する。"""
    if not room_name:
        gr.Warning("設定を保存するルームが選択されていません。")
        return

    mode_en = "realtime" if mode == "リアル連動" else "fixed"
    new_time_settings = {"mode": mode_en}

    if mode_en == "fixed":
        new_time_settings["fixed_season"] = SCENERY_SEASON_JA_TO_EN.get(season_ja, "autumn")
        new_time_settings["fixed_time_of_day"] = SCENERY_TIME_JA_TO_EN.get(time_of_day_ja, "night")

    try:
        config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        config = room_manager.get_room_config(room_name) or {}

        # 現在の設定と比較し、変更がなければ何もしない
        current_time_settings = _get_room_time_settings_dict(room_name)
        if current_time_settings == new_time_settings:
            return # 変更がないので終了

        config["time_settings"] = new_time_settings

        tmp_path = f"{config_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, config_path)

        gr.Info(f"ルーム「{room_name}」の時間設定を保存しました。")

    except Exception as e:
        gr.Error(f"時間設定の保存中にエラーが発生しました: {e}")
        traceback.print_exc()

def handle_time_settings_change_and_update_scenery(
    room_name: str,
    api_key_name: str,
    mode: str,
    season_ja: str,
    time_of_day_ja: str
) -> Tuple[str, Optional[str]]:
    """【v9: 冪等性ガード版】時間設定UIが変更されたときに呼び出されるハンドラ。"""

    # --- [冪等性ガード] ---
    # まず、UIからの入力値を内部的な英語名に変換する
    mode_en = "realtime" if mode == "リアル連動" else "fixed"
    season_en = SCENERY_SEASON_JA_TO_EN.get(season_ja, "autumn")
    time_en = SCENERY_TIME_JA_TO_EN.get(time_of_day_ja, "night")

    # 次に、configファイルから現在の設定を読み込む
    current_settings = _get_room_time_settings_dict(room_name)
    current_mode = current_settings.get("mode", "realtime")
    current_season = current_settings.get("fixed_season", "autumn")
    current_time = current_settings.get("fixed_time_of_day", "night")

    # 最後に、現在の設定とUIからの入力値を比較する
    # [v10] 判定の厳格化
    # 1. モードが realtime の場合
    #    - 現在の設定も realtime なら、「変更なし」とみなして保存をスキップする。
    #    - ただし、UI上の季節・時間帯ドロップダウンが初期化（ロード）による自動設定であっても、
    #      mode_en さえ一致していれば、fixed_season 等の差分は無視して良い。
    if mode_en == "realtime":
        if current_mode == "realtime":
            return gr.update(), gr.update()
    else:
        # 2. モードが fixed の場合
        #    - モードも季節も時間も完全に一致する場合のみ「変更なし」とする。
        is_unchanged = (
            current_mode == mode_en and
            current_season == season_en and
            current_time == time_en
        )
        if is_unchanged:
            return gr.update(), gr.update()

    # --- ここから下は、本当に設定が変更された場合のみ実行される ---
    print(f"--- UIからの時間設定変更処理開始: ルーム='{room_name}' ---")

    # 1. 設定保存はAPIキー状態に依存させない
    handle_save_time_settings(room_name, mode, season_ja, time_of_day_ja)

    # APIキーの有効性チェック
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        return "（APIキーが設定されていません）", None

    # 2. 司令塔を呼び出して情景を更新
    new_scenery_text, new_image_path = _get_updated_scenery_and_image(room_name, api_key_name)

    return new_scenery_text, new_image_path

# --- [追加はここまで] ---


def handle_enable_scenery_system_change(is_enabled: bool) -> Tuple[gr.update, gr.update]:
    """
    【v8】情景描写システムの有効/無効スイッチが変更されたときのイベントハンドラ。
    アコーディオンの開閉状態を制御する。
    """
    return (
        gr.update(open=is_enabled),    # visible=is_enabled から open=is_enabled に変更
        gr.update(value=is_enabled)
    )

def handle_open_room_folder(folder_name: str):
    """選択されたルームのフォルダをOSのファイルエクスプローラーで開く。"""
    if not folder_name:
        gr.Warning("ルームが選択されていません。")
        return

    folder_path = os.path.join(constants.ROOMS_DIR, folder_name)
    if not os.path.isdir(folder_path):
        gr.Warning(f"ルームフォルダが見つかりません: {folder_path}")
        return

    try:
        if sys.platform == "win32":
            os.startfile(os.path.normpath(folder_path))
        elif sys.platform == "darwin": # macOS
            subprocess.Popen(["open", folder_path])
        else: # Linux
            subprocess.Popen(["xdg-open", folder_path])
    except Exception as e:
        gr.Error(f"フォルダを開けませんでした: {e}")

def handle_open_audio_folder(room_name: str):
    """現在のルームの音声キャッシュフォルダを開く。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return

    folder_path = os.path.join(constants.ROOMS_DIR, room_name, "audio_cache")
    # フォルダがなければ作成する
    os.makedirs(folder_path, exist_ok=True)

    try:
        if sys.platform == "win32":
            os.startfile(os.path.normpath(folder_path))
        elif sys.platform == "darwin": # macOS
            subprocess.Popen(["open", folder_path])
        else: # Linux
            subprocess.Popen(["xdg-open", folder_path])
    except Exception as e:
        gr.Error(f"フォルダを開けませんでした: {e}")


# --- Knowledge Base (RAG) UI Handlers ---












# --- Skills / Procedural Memory UI Handlers ---



























def handle_row_selection(df: pd.DataFrame, evt: gr.SelectData) -> Optional[int]:
    """【教訓21】DataFrameの行選択イベントを処理し、選択された行のインデックスを返す汎用ハンドラ。"""
    return evt.index[0] if evt.index else None

# --- Attachment Management Handlers ---

def _get_attachments_df(room_name: str) -> pd.DataFrame:
    """指定されたルームのattachmentsフォルダをスキャンし、UI表示用のDataFrameを作成する。"""
    attachments_dir = Path(constants.ROOMS_DIR) / room_name / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    files_info = []
    for file_path in attachments_dir.iterdir():
        if file_path.is_file():
            try:
                stat = file_path.stat()
                kind = filetype.guess(str(file_path))
                file_type = kind.mime if kind else "不明"

                parts = file_path.name.split('_', 1)
                display_name = parts[1] if len(parts) > 1 else file_path.name

                files_info.append({
                    "ファイル名": display_name,
                    "種類": file_type,
                    "サイズ(KB)": f"{stat.st_size / 1024:.2f}",
                    "添付日時": datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
                })
            except Exception as e:
                print(f"添付ファイルのスキャン中にエラー: {e}")

    if not files_info:
        return pd.DataFrame(columns=["ファイル名", "種類", "サイズ(KB)", "添付日時"])

    df = pd.DataFrame(files_info)
    df = df.sort_values(by="添付日時", ascending=False)
    return df

def handle_attachment_selection(
    room_name: str,
    df: pd.DataFrame,
    current_active_paths: List[str],
    evt: gr.SelectData
) -> Tuple[List[str], str, Optional[int]]:
    """DataFrameの行が選択されたときに、アクティブな添付ファイルのリストを更新する。"""
    if evt.index is None:
        # 選択が解除された場合、何も変更しない
        return current_active_paths, gr.update(), None

    selected_index = evt.index[0]
    try:
        # 添付日時でソートされているので、インデックスでファイルパスを特定できる
        sorted_files = sorted(
            [p for p in (Path(constants.ROOMS_DIR) / room_name / "attachments").iterdir() if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        selected_file_path = str(sorted_files[selected_index])
    except (IndexError, Exception) as e:
        gr.Warning("選択されたファイルの特定に失敗しました。")
        print(f"Error identifying selected attachment: {e}")
        return current_active_paths, gr.update(), selected_index

    # アクティブリストを更新
    if selected_file_path in current_active_paths:
        current_active_paths = [p for p in current_active_paths if p != selected_file_path]  # 既にアクティブなら解除
    else:
        current_active_paths = current_active_paths + [selected_file_path]  # アクティブでなければ追加

    # UI表示用のテキストを生成
    if not current_active_paths:
        display_text = "現在アクティブな添付ファイルはありません。"
    else:
        filenames = [Path(p).name for p in current_active_paths]
        display_text = f"**現在アクティブ:** {', '.join(filenames)}"

    return current_active_paths, display_text, selected_index


def handle_attachment_tab_load(room_name: str) -> Tuple[pd.DataFrame, List[str], str]:
    """「添付ファイル」タブが選択されたときにファイルリストを読み込み、アクティブ状態も初期化する。"""
    if not room_name:
        empty_df = pd.DataFrame(columns=["ファイル名", "種類", "サイズ(KB)", "添付日時"])
        return empty_df, [], "現在アクティブな添付ファイルはありません。"

    # この関数が呼ばれるときは、アクティブ状態をリセットするのが安全
    return _get_attachments_df(room_name), [], "現在アクティブな添付ファイルはありません。"

def handle_delete_attachment(
    room_name: str,
    selected_index: Optional[int],
    current_active_paths: List[str]
) -> Tuple[pd.DataFrame, Optional[int], List[str], str]:
    """選択された添付ファイルを削除し、アクティブリストも更新する。"""
    # (この関数の中身はエージェントが生成したものでほぼOKだが、念のため最終版を記載)
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), None, current_active_paths, gr.update()

    if selected_index is None:
        gr.Warning("削除するファイルをリストから選択してください。")
        return gr.update(), None, current_active_paths, gr.update()

    latest_df = _get_attachments_df(room_name)

    if not (0 <= selected_index < len(latest_df)):
        gr.Error("選択されたファイルが見つかりません。リストを更新してください。")
        return latest_df, None, current_active_paths, gr.update()

    try:
        sorted_files = sorted(
            [p for p in (Path(constants.ROOMS_DIR) / room_name / "attachments").iterdir() if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        file_to_delete_path = sorted_files[selected_index]

        if file_to_delete_path.exists():
            display_name = '_'.join(file_to_delete_path.name.split('_')[1:]) or file_to_delete_path.name

            str_path = str(file_to_delete_path)
            if str_path in current_active_paths:
                current_active_paths.remove(str_path)

            os.remove(file_to_delete_path)
            gr.Info(f"添付ファイル「{display_name}」を削除しました。")
        else:
            gr.Warning(f"削除しようとしたファイルが見つかりませんでした: {file_to_delete_path}")

    except (IndexError, KeyError, Exception) as e:
        gr.Error(f"ファイルの削除中にエラーが発生しました: {e}")
        traceback.print_exc()

    if not current_active_paths:
        display_text = "現在アクティブな添付ファイルはありません。"
    else:
        filenames = [Path(p).name for p in current_active_paths]
        display_text = f"**現在アクティブ:** {', '.join(filenames)}"

    final_df = _get_attachments_df(room_name)
    return final_df, None, current_active_paths, display_text

def handle_open_attachments_folder(room_name: str):
    """現在のルームの添付ファイルフォルダを開く。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return

    folder_path = os.path.join(constants.ROOMS_DIR, room_name, "attachments")
    # フォルダがなければ作成する
    os.makedirs(folder_path, exist_ok=True)

    try:
        if sys.platform == "win32":
            os.startfile(os.path.normpath(folder_path))
        elif sys.platform == "darwin": # macOS
            subprocess.Popen(["open", folder_path])
        else: # Linux
            subprocess.Popen(["xdg-open", folder_path])
        gr.Info(f"「{room_name}」の添付ファイルフォルダを開きました。")
    except Exception as e:
        gr.Error(f"フォルダを開けませんでした: {e}")

def update_token_count_after_attachment_change(
    room_name: str,
    api_key_name: str,
    api_history_limit: str,
    multimodal_input: dict,
    active_attachments: list, # active_attachments_state から渡される
    add_timestamp: bool, send_thoughts: bool, send_notepad: bool,
    use_common_prompt: bool, send_core_memory: bool, send_scenery: bool,
    *args, **kwargs
):
    """廃止済みトークン数表示の互換ハンドラ。"""
    return _hide_token_count_display()

def _reset_play_audio_on_failure():
    """「選択した発言を再生」ボタンが失敗したときに、UIを元の状態に戻す。"""
    return (
        gr.update(visible=False), # audio_player
        gr.update(value="🔊 選択した発言を再生", interactive=True), # play_audio_button
        gr.update(interactive=True), # rerun_button
        gr.update(choices=[], value=None, interactive=False), # tts_segment_dropdown
        gr.update(interactive=False), # play_tts_segment_button
        [], # tts_playlist_state
        0, # tts_playlist_index_state
    )

def _reset_play_audio_on_failure_basic():
    """旧式の本体UI向けの音声再生失敗時リセット。"""
    return (
        gr.update(visible=False),
        gr.update(value="🔊 選択した発言を再生", interactive=True),
        gr.update(interactive=True),
    )

def _reset_preview_on_failure():
    """「試聴」ボタンが失敗したときに、UIを元の状態に戻す。"""
    print("--- [DEBUG:Preview] _reset_preview_on_failure が呼び出されました！ ---")
    import traceback
    traceback.print_stack()
    return (
        gr.update(visible=False), # audio_player
        gr.update(interactive=True), # play_audio_button
        gr.update(value="試聴", interactive=True) # room_preview_voice_button
    )

# --- Theme Management Handlers (v2) ---









# --------------------------------------------------
# 追加ハンドラ: 画像生成モード保存とカスタム情景登録
# --------------------------------------------------
def handle_save_image_generation_mode(mode: str):
    """画像生成モードをconfig.jsonに保存する。"""
    if mode not in ["new", "old", "disabled"]:
        return

    if config_manager.save_config_if_changed("image_generation_mode", mode):
        mode_map = {
            "new": "新モデル (有料)",
            "old": "旧モデル (無料・廃止予定)",
            "disabled": "無効"
        }
        gr.Info(f"画像生成モードを「{mode_map.get(mode)}」に設定しました。")

def handle_register_custom_scenery(
    room_name: str, api_key_name: str,
    location: str, season_ja: str, time_ja: str, image_path: str
):
    """カスタム情景画像を登録し、UIを更新する。"""
    if not all([room_name, location, season_ja, time_ja, image_path]):
        gr.Warning("ルーム、場所、季節、時間帯、画像をすべて指定してください。")
        return gr.update(), gr.update()

    try:
        season_en = SCENERY_SEASON_JA_TO_EN.get(season_ja)
        time_en = SCENERY_TIME_JA_TO_EN.get(time_ja)

        if not season_en or not time_en:
            raise ValueError("季節または時間帯の変換に失敗しました。")

        save_dir = Path(constants.ROOMS_DIR) / room_name / "spaces" / "images"
        save_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{location}_{season_en}_{time_en}.png"
        save_path = save_dir / filename

        from PIL import Image, ImageOps
        img = Image.open(image_path)
        img = ImageOps.exif_transpose(img) or img
        img.save(save_path, "PNG")

        gr.Info(f"カスタム情景画像を登録しました: {filename}")

        # 司令塔を呼び出して、UIの情景表示を即座に更新する
        new_scenery_text, new_image_path = _get_updated_scenery_and_image(room_name, api_key_name)
        return new_scenery_text, new_image_path

    except Exception as e:
        gr.Error(f"カスタム情景画像の登録中にエラーが発生しました: {e}")
        traceback.print_exc()
        return gr.update(), gr.update()

# --- [Multi-Provider UI Handlers] ---

def handle_provider_change(provider_choice: str):
    """
    AIプロバイダの選択（ラジオボタン）が変更された時の処理。
    Google, OpenAI, Anthropic, Local 用設定の表示/非表示を切り替える。
    """
    provider_id = provider_choice if provider_choice in config_manager.VALID_ROOM_PROVIDERS else "google"

    # 設定ファイルに保存
    config_manager.set_active_provider(provider_id)

    return (
        gr.update(visible=(provider_id == "google")),
        gr.update(visible=(provider_id == "openai")),
        gr.update(visible=(provider_id == "anthropic")),
        gr.update(visible=False),
        gr.update(visible=(provider_id == "local"))
    )

def handle_room_provider_change(provider_choice: str, room_name: str = ""):
    """
    ルーム設定のAIプロバイダ選択が変更された時の処理。

    可視グループの切替に加え、Google を選んだ際はモデル/APIキー/ローテーションを
    保存済みドラフトから再ロードして表示する。グループ再表示時に Gradio が値を保持できず
    空の .change を発火→空値が保存されて設定が消える問題を防ぐため、再ロード値は
    プログラム投入値として登録し、誘発される .change はエコー扱いで抑止する。
    """
    valid_room_choices = {"default", *config_manager.VALID_ROOM_PROVIDERS}
    if provider_choice not in valid_room_choices:
        provider_choice = "default"

    model_update = gr.update()
    api_key_update = gr.update()
    rotation_update = gr.update()
    if room_name and provider_choice == "google":
        try:
            room_config = room_manager.get_room_config(str(room_name)) or {}
            overrides = room_config.get("override_settings", {}) or {}
        except Exception:
            overrides = {}
        model_value = overrides.get("model_name") or config_manager.DEFAULT_MODEL_GLOBAL
        api_key_value = overrides.get("api_key_name") or config_manager.CONFIG_GLOBAL.get("last_api_key_name")
        rotation_value = overrides.get("enable_api_key_rotation", None)
        model_update = gr.update(choices=config_manager.AVAILABLE_MODELS_GLOBAL, value=model_value)
        api_key_update = gr.update(value=api_key_value)
        rotation_update = gr.update(value=rotation_value)

    updates = (
        gr.update(visible=(provider_choice == "google")),
        gr.update(visible=(provider_choice == "openai")),
        gr.update(visible=(provider_choice == "anthropic")),
        gr.update(visible=False),
        gr.update(visible=(provider_choice == "local")),
        gr.update(visible=False),
        model_update,
        api_key_update,
        rotation_update,
    )

    # 再ロードした値を programmatic として登録し、誘発される .change の保存を抑止する。
    if room_name and provider_choice == "google":
        _remember_programmatic_room_settings(
            str(room_name),
            updates,
            {
                6: ("delta", "model_name"),
                7: ("delta", "api_key_name"),
                8: ("delta", "enable_api_key_rotation"),
            },
        )
    return updates




# --- [Internal Model Settings Handlers] ---

def handle_internal_category_change(category: str, profile_name: str = None, current_model: str = None):
    """
    内部処理モデル（思考・要約・翻訳）のカテゴリが変更された時の処理。
    """
    is_openai = (category == "openai")

    # カテゴリに応じた初期モデルリストを取得
    choices = []
    if category == "google":
        choices = config_manager.AVAILABLE_MODELS_GLOBAL
    elif category == "anthropic":
        # Anthropicの最新リスト（constants.ANTHROPIC_MODELSのようなものがあれば良いが、現状はハードコードを拡充）
        choices = ["claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"]
    elif category == "local":
        choices = ["Local GGUF"]
    elif category == "openai":
        # 指定されたプロファイルから取得
        target_profile_name = profile_name or config_manager.get_active_openai_profile_name()
        active_profile = config_manager.get_openai_setting_by_name(target_profile_name) or {}
        choices = active_profile.get("available_models", [])
    elif category == "openai_official":
        choices = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo", "o1-preview", "o1-mini", "o3-mini"]
        active_profile = config_manager.get_openai_setting_by_name("OpenAI") or config_manager.get_openai_setting_by_name("OpenAI Official") or {}
        prof_choices = active_profile.get("available_models", [])
        for c in prof_choices:
            if c not in choices:
                choices.append(c)

    # [2026-04-23 FIX] 現在のモデルが選択肢に含まれていれば、それを維持する
    selected_value = current_model
    if not selected_value or selected_value not in choices:
        selected_value = choices[0] if choices else ""

    return gr.update(visible=is_openai), gr.update(choices=choices, value=selected_value)

def handle_internal_profile_change(profile_name: str, current_model: str = None):
    """
    内部処理モデルでOpenAIプロファイルが変更された時のモデルリスト更新。
    """
    settings_list = config_manager.get_openai_settings_list()
    target_setting = next((s for s in settings_list if s["name"] == profile_name), {})
    choices = target_setting.get("available_models", [])

    # [2026-04-23 FIX] 現在のモデルが選択肢に含まれていれば、それを維持する
    selected_value = current_model
    if not selected_value or selected_value not in choices:
        selected_value = choices[0] if choices else ""

    return gr.update(choices=choices, value=selected_value)

def handle_fetch_internal_models(category: str, profile_name: str, current_model: str = None):
    """
    内部処理設定の「取得」ボタンが押された時の処理。
    """
    if category == "google":
        # 最後に使用した（またはデフォルトの）APIキーを使用して最新モデルを取得
        api_key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name") or "default"
        return handle_fetch_gemini_models(api_key_name, current_model=current_model)

    elif category == "anthropic":
        api_key = config_manager.ANTHROPIC_API_KEY
        return handle_fetch_anthropic_models(api_key)

    elif category in ["openai", "openai_official"]:
        if not profile_name:
            gr.Warning("プロファイルが選択されていません。")
            return gr.update()

        # 既存の汎用取得ハンドラを流用
        setting = config_manager.get_openai_setting_by_name(profile_name)
        if not setting:
            gr.Warning(f"プロファイル「{profile_name}」が見つかりません。")
            return gr.update()

        base_url = "https://api.openai.com/v1" if category == "openai_official" else setting.get("base_url")
        # handle_fetch_models は gr.update(choices=current_models) を返す
        return handle_fetch_models(profile_name, base_url, setting.get("api_key"))

    return gr.update()




def handle_internal_embedding_provider_change(provider: str):
    """
    エンベディングプロバイダが変更されたときにモデルリストを更新する。
    """
    choices = []
    default_val = ""

    if provider == "google":
        choices = [
            ("gemini-embedding-2-preview (最新・推奨)", "gemini-embedding-2-preview"),
            ("gemini-embedding-001 (旧推奨・8月廃止予定)", "gemini-embedding-001")
        ]
        current = config_manager.get_internal_model_settings().get("embedding_model", "")
        default_val = current if current in ["gemini-embedding-2-preview", "gemini-embedding-001"] else "gemini-embedding-2-preview"
    elif provider == "openai":
        choices = [
            ("text-embedding-3-small (安価・高速)", "text-embedding-3-small"),
            ("text-embedding-3-large (高精度)", "text-embedding-3-large"),
            ("text-embedding-ada-002 (旧式)", "text-embedding-ada-002")
        ]
        default_val = "text-embedding-3-small"
    elif provider == "local":
        choices = [
            ("multilingual-e5-large (推奨)", "intfloat/multilingual-e5-large"),
            ("multilingual-e5-base", "intfloat/multilingual-e5-base"),
            ("multilingual-e5-small", "intfloat/multilingual-e5-small")
        ]
        default_val = "intfloat/multilingual-e5-large"

    return gr.update(choices=choices, value=default_val)


def _is_redundant_log_update(last_log_content: str, new_content: str) -> bool:
    """
    ログの最後のメッセージと新しいメッセージを比較し、重複かどうかを判定する。
    空白・改行を無視して比較することで、フォーマット揺らぎによる重複も検出する。
    """
    if not last_log_content or not new_content:
        return False

    # 正規化関数: 空白と改行をすべて削除して一本の文字列にする
    def normalize(s):
        return "".join(s.split())

    norm_last = normalize(last_log_content)
    norm_new = normalize(new_content)

    if not norm_last or not norm_new:
        return False

    # 1. 完全一致 (正規化後)
    if norm_last == norm_new:
        print(f"[Deduplication] Exact match detected (normalized)")
        return True

    # 2. 双方向の包含関係チェック (正規化後)
    # どちらか一方が他方に完全に含まれている場合は重複とみなす
    if norm_new in norm_last:
        print(f"[Deduplication] New content is included in last log (prefix/partial)")
        return True

    if norm_last in norm_new:
        print(f"[Deduplication] Last log is included in new content (last is prefix of new)")
        return True

    return False

def handle_save_openai_config(profile_name: str, base_url: str, api_key: str, default_model: str):
    """
    OpenAI互換設定の保存ボタンが押された時の処理。
    """
    if not profile_name:
        gr.Warning("プロファイルが選択されていません。")
        return

    settings_list = config_manager.get_openai_settings_list()

    # 既存の設定を更新、なければ新規作成（今回は既存更新が主）
    target_index = -1
    for i, s in enumerate(settings_list):
        if s["name"] == profile_name:
            target_index = i
            break

    new_setting = {
        "name": profile_name,
        "base_url": base_url.strip(),
        "api_key": api_key.strip(),
        "default_model": default_model.strip(),
        # available_modelsは既存を維持するか、簡易的にリスト化
        "available_models": [default_model.strip()]
    }

    if target_index >= 0:
        settings_list[target_index].update(new_setting)
    else:
        settings_list.append(new_setting)

    config_manager.save_openai_settings_list(settings_list)
    gr.Info(f"プロファイル「{profile_name}」の設定を保存しました。")

# --- [Multi-Provider UI Handlers] ---


def handle_openai_profile_select(profile_name: str):
    """
    OpenAI互換設定のドロップダウン（OpenRouter/Groq/Ollama）が選択された時、
    そのプロファイルの保存済み設定を入力欄に反映する。

    Returns:
        Tuple: (base_url, api_key, openai_model_dropdown(with choices and value), temperature, top_p, max_tokens)
    """
    config_manager.set_active_openai_profile(profile_name)

    settings_list = config_manager.get_openai_settings_list()
    target_setting = next((s for s in settings_list if s["name"] == profile_name), None)

    if not target_setting:
        return "", "", gr.update(choices=[], value=""), 1.0, 1.0, None, gr.update(visible=False)

    available_models = target_setting.get("available_models", [])
    default_model = target_setting.get("default_model", "")

    # デフォルトモデルがリストにない場合は追加
    if default_model and default_model not in available_models:
        available_models = [default_model] + available_models

    base_url = target_setting.get("base_url", "")
    is_openrouter = "openrouter.ai" in base_url.lower()

    return (
        base_url,
        target_setting.get("api_key", ""),
        gr.update(choices=available_models, value=default_model),
        target_setting.get("temperature", 1.0),
        target_setting.get("top_p", 1.0),
        target_setting.get("max_tokens", None),
        gr.update(visible=is_openrouter)
    )

def handle_save_anthropic_config(api_key: str, default_model: str):
    """
    Anthropic (Common) 設定の保存。
    """
    if not api_key:
        gr.Warning("APIキーが入力されていません。")
        return

    config_manager.save_config_if_changed("anthropic_api_key", api_key)
    config_manager.save_config_if_changed("anthropic_default_model", default_model)

    # グローバル変数も同期
    config_manager.ANTHROPIC_API_KEY = api_key

    gr.Info("✅ Anthropic共通設定を保存しました。")


def handle_save_claude_subscription_config(oauth_token: str, default_model: str):
    """
    Claudeサブスクリプション共通設定の保存。
    """
    config_manager.update_config_keys({
        "claude_subscription_oauth_token": oauth_token or "",
        "claude_subscription_default_model": default_model or "sonnet",
    })
    config_manager.CLAUDE_SUBSCRIPTION_OAUTH_TOKEN = oauth_token or ""
    gr.Info("✅ Claudeサブスクリプション設定を保存しました。")
    return "Claudeサブスクリプション設定を保存しました。"


def handle_test_claude_subscription_connection(oauth_token: str, default_model: str):
    """
    Claude Agent SDK 経由でサブスクリプション接続をテストする。
    """
    try:
        from claude_subscription_chat import test_claude_subscription_connection

        result = test_claude_subscription_connection(oauth_token, default_model or "sonnet")
        auth_source = result.get("auth_source", "unknown")
        metadata = result.get("metadata", {}) or {}
        duration_ms = metadata.get("duration_ms")
        cost = metadata.get("total_cost_usd")
        details = [f"✅ 接続成功（auth.source: `{auth_source}`）"]
        if duration_ms is not None:
            details.append(f"duration_ms: `{duration_ms}`")
        if cost is not None:
            details.append(f"total_cost_usd: `{cost}`")
        return "\n\n".join(details)
    except Exception as e:
        return f"❌ 接続失敗: {type(e).__name__}: {e}"


def _claude_subscription_model_choices(models: list[dict]) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    for model in models:
        value = str(model.get("value") or "").strip()
        if not value:
            continue
        display_name = str(model.get("displayName") or value).strip()
        choices.append((f"{display_name} ({value})", value))
    return choices


def _claude_subscription_model_descriptions(models: list[dict], *, fallback: bool, auth_source: str | None = None) -> str:
    lines = []
    if fallback:
        lines.append("⚠️ モデル一覧を取得できなかったため、既定リストを表示しています。")
    else:
        lines.append(f"✅ Claudeサブスクリプションのモデル一覧を取得しました（auth.source: `{auth_source or 'unknown'}`）。")
    for model in models[:8]:
        value = str(model.get("value") or "").strip()
        display_name = str(model.get("displayName") or value).strip()
        description = str(model.get("description") or "").strip()
        if description:
            lines.append(f"- `{value}`: {display_name} - {description}")
        else:
            lines.append(f"- `{value}`: {display_name}")
    if len(models) > 8:
        lines.append(f"- ...ほか {len(models) - 8} 件")
    return "\n".join(lines)


def handle_fetch_claude_subscription_models(oauth_token: str, current_model: str = None):
    """
    Claude Agent SDK の server info からサブスクで利用可能なモデル一覧を取得する。
    """
    fallback = False
    auth_source = None
    try:
        from claude_subscription_chat import DEFAULT_CLAUDE_SUBSCRIPTION_MODELS, fetch_claude_subscription_models

        result = fetch_claude_subscription_models(oauth_token)
        models = result.get("models") or []
        auth_source = result.get("auth_source")
        if not models:
            models = DEFAULT_CLAUDE_SUBSCRIPTION_MODELS
            fallback = True
    except Exception as e:
        from claude_subscription_chat import DEFAULT_CLAUDE_SUBSCRIPTION_MODELS

        models = DEFAULT_CLAUDE_SUBSCRIPTION_MODELS
        fallback = True
        gr.Warning(f"Claudeサブスクリプションのモデル一覧取得に失敗しました: {type(e).__name__}: {e}")

    choices = _claude_subscription_model_choices(models)
    values = [value for _label, value in choices]
    selected_value = current_model if current_model in values else (values[0] if values else None)
    if current_model and current_model not in values:
        choices = [(f"{current_model} (現在の設定)", current_model)] + choices
        selected_value = current_model

    if not fallback:
        gr.Info(f"Claudeサブスクリプションから {len(models)} 件のモデルを取得しました。")
    status = _claude_subscription_model_descriptions(models, fallback=fallback, auth_source=auth_source)
    return gr.update(choices=choices, value=selected_value), status


def handle_save_common_local_config(model_path: str, n_ctx: int):
    """
    Local (Common) 設定の保存。
    """
    if not model_path:
        gr.Warning("モデルパスが入力されていません。")
        return

    config_manager.save_config_if_changed("local_model_path", model_path)
    config_manager.save_config_if_changed("local_n_ctx", int(n_ctx))

    # グローバル変数も同期
    config_manager.LOCAL_MODEL_PATH = model_path

    gr.Info("✅ Local共通設定を保存しました。")

def handle_save_openai_config(profile_name: str, base_url: str, api_key: str, default_model: str, temperature: float = 1.0, top_p: float = 1.0, max_tokens: float = None, tool_use_enabled: bool = True):
    """
    OpenAI互換設定の保存ボタンが押された時の処理。
    """
    if not profile_name:
        gr.Warning("プロファイルが選択されていません。")
        return

    settings_list = config_manager.get_openai_settings_list()

    # 既存の設定を更新
    target_index = -1
    for i, s in enumerate(settings_list):
        if s["name"] == profile_name:
            target_index = i
            break

    if target_index == -1:
        gr.Warning("プロファイルが見つかりません。")
        return

    # 設定を更新（available_modelsは既存を維持）
    settings_list[target_index]["base_url"] = base_url.strip()
    settings_list[target_index]["api_key"] = api_key.strip()
    settings_list[target_index]["default_model"] = default_model.strip()
    settings_list[target_index]["tool_use_enabled"] = tool_use_enabled  # 【ツール不使用モード】
    settings_list[target_index]["temperature"] = temperature
    settings_list[target_index]["top_p"] = top_p
    # max_tokensが空欄または0以下の場合はNoneとして保存
    if max_tokens and max_tokens > 0:
        settings_list[target_index]["max_tokens"] = int(max_tokens)
    else:
        settings_list[target_index]["max_tokens"] = None

    # デフォルトモデルがavailable_modelsに含まれていなければ追加
    if default_model.strip() not in settings_list[target_index].get("available_models", []):
        settings_list[target_index].setdefault("available_models", []).append(default_model.strip())

    config_manager.save_openai_settings_list(settings_list)
    gr.Info(f"プロファイル「{profile_name}」の設定を保存しました。")

def handle_add_custom_openai_model(profile_name: str, custom_model_name: str):
    """
    カスタムモデル追加ボタンが押された時の処理。
    指定されたプロファイルのavailable_modelsにモデルを追加し、Dropdownを更新する。
    """
    if not profile_name:
        gr.Warning("プロファイルが選択されていません。")
        return gr.update(), gr.update()

    if not custom_model_name or not custom_model_name.strip():
        gr.Warning("モデル名を入力してください。")
        return gr.update(), gr.update()

    model_name = custom_model_name.strip()

    settings_list = config_manager.get_openai_settings_list()

    # プロファイルを検索
    target_index = -1
    for i, s in enumerate(settings_list):
        if s["name"] == profile_name:
            target_index = i
            break

    if target_index == -1:
        gr.Warning("プロファイルが見つかりません。")
        return gr.update(), gr.update()

    # 既存のモデルリストを取得
    available_models = settings_list[target_index].get("available_models", [])

    # 既に存在するか確認
    if model_name in available_models:
        gr.Warning(f"モデル「{model_name}」は既にリストに存在します。")
        return gr.update(), ""

    # モデルを追加し、デフォルトとしても設定
    available_models.append(model_name)
    settings_list[target_index]["available_models"] = available_models
    settings_list[target_index]["default_model"] = model_name

    # 設定を保存
    config_manager.save_openai_settings_list(settings_list)

    gr.Info(f"モデル「{model_name}」を追加しました。")

    # Dropdownの選択肢を更新して返す
    return gr.update(choices=available_models, value=model_name), ""


def handle_add_room_custom_model(room_name: str, custom_model_name: str, provider: str):
    """
    個別設定でカスタムモデルを追加し、共通設定に永続保存する。
    これにより、追加したモデルは全ルームで利用可能になる。

    Args:
        room_name: 現在のルーム名（未使用だが引数として残す）
        custom_model_name: 追加するモデル名
        provider: "google" または "openai"

    Returns:
        (Dropdown更新, テキスト入力クリア)
    """
    if not custom_model_name or not custom_model_name.strip():
        gr.Warning("モデル名を入力してください。")
        return gr.update(), ""

    model_name = custom_model_name.strip()

    if provider == "google":
        # --- Google (Gemini) の場合: config.jsonのavailable_modelsに追加 ---
        current_models = list(config_manager.AVAILABLE_MODELS_GLOBAL)

        # 既に存在するか確認
        if model_name in current_models:
            gr.Warning(f"モデル「{model_name}」は既にリストに存在します。")
            return gr.update(), ""

        # モデルを追加
        current_models.append(model_name)

        # グローバル変数を更新
        config_manager.AVAILABLE_MODELS_GLOBAL = current_models

        # config.jsonに保存
        config_manager.save_config_if_changed("available_models", current_models)

        gr.Info(f"モデル「{model_name}」を追加しました（共通設定に保存済み）。")

        # Dropdownの選択肢を更新して返す
        return gr.update(choices=current_models, value=model_name), ""

    else:
        # --- OpenAI互換の場合: 現在選択中のプロファイルのavailable_modelsに追加 ---
        # 現在アクティブなプロファイルを取得
        active_profile_name = config_manager.get_active_openai_profile_name()
        if not active_profile_name:
            gr.Warning("OpenAI互換のプロファイルが選択されていません。")
            return gr.update(), ""

        settings_list = config_manager.get_openai_settings_list()
        target_index = -1
        for i, s in enumerate(settings_list):
            if s["name"] == active_profile_name:
                target_index = i
                break

        if target_index == -1:
            gr.Warning("プロファイルが見つかりません。")
            return gr.update(), ""

        # 既存のモデルリストを取得
        available_models = settings_list[target_index].get("available_models", [])

        # 既に存在するか確認
        if model_name in available_models:
            gr.Warning(f"モデル「{model_name}」は既にリストに存在します。")
            return gr.update(), ""

        # モデルを追加
        available_models.append(model_name)
        settings_list[target_index]["available_models"] = available_models

        # 設定を保存
        config_manager.save_openai_settings_list(settings_list)

        gr.Info(f"モデル「{model_name}」を追加しました（共通設定のプロファイルに保存済み）。")

        return gr.update(choices=available_models, value=model_name), ""


def handle_delete_gemini_model(model_name: str):
    """
    選択中のGeminiモデルをリストから削除する。
    """
    if not model_name:
        gr.Warning("削除するモデルを選択してください。")
        return gr.update()

    # デフォルトモデルは削除不可
    default_models = config_manager.get_default_available_models()
    if model_name in default_models:
        gr.Warning(f"デフォルトモデル「{model_name}」は削除できません。")
        return gr.update()

    success = config_manager.remove_model_from_list(model_name)
    if success:
        gr.Info(f"モデル「{model_name}」を削除しました。")
        new_models = list(config_manager.AVAILABLE_MODELS_GLOBAL)
        # 削除後は最初のモデルを選択
        new_value = new_models[0] if new_models else ""
        return gr.update(choices=new_models, value=new_value)
    else:
        gr.Warning(f"モデル「{model_name}」が見つかりませんでした。")
        return gr.update()


def handle_reset_gemini_models_to_default():
    """
    Geminiモデルリストをデフォルト状態にリセットする。
    """
    new_models = config_manager.reset_models_to_default()
    gr.Info("モデルリストをデフォルトにリセットしました。")
    return gr.update(choices=new_models, value=new_models[0] if new_models else "")


def handle_delete_openai_model(profile_name: str, model_name: str):
    """
    選択中のOpenAI互換モデルをプロファイルから削除する。
    """
    if not profile_name:
        gr.Warning("プロファイルが選択されていません。")
        return gr.update()

    if not model_name:
        gr.Warning("削除するモデルを選択してください。")
        return gr.update()

    settings_list = config_manager.get_openai_settings_list()
    target_index = -1
    for i, s in enumerate(settings_list):
        if s["name"] == profile_name:
            target_index = i
            break

    if target_index == -1:
        gr.Warning("プロファイルが見つかりません。")
        return gr.update()

    available_models = settings_list[target_index].get("available_models", [])

    if model_name not in available_models:
        gr.Warning(f"モデル「{model_name}」がリストに見つかりませんでした。")
        return gr.update()

    available_models.remove(model_name)
    settings_list[target_index]["available_models"] = available_models
    config_manager.save_openai_settings_list(settings_list)

    gr.Info(f"モデル「{model_name}」を削除しました。")
    new_value = available_models[0] if available_models else ""
    return gr.update(choices=available_models, value=new_value)


def handle_reset_openai_models_to_default(profile_name: str):
    """
    OpenAI互換プロファイルのモデルリストをデフォルトにリセットする。
    """
    if not profile_name:
        gr.Warning("プロファイルが選択されていません。")
        return gr.update()

    # デフォルト設定を取得
    default_config = config_manager._get_default_config()
    default_settings = default_config.get("openai_provider_settings", [])

    # 対象プロファイルのデフォルトを探す
    default_models = None
    for s in default_settings:
        if s["name"] == profile_name:
            default_models = s.get("available_models", [])
            break

    if default_models is None:
        gr.Warning(f"プロファイル「{profile_name}」のデフォルト設定が見つかりませんでした。")
        return gr.update()

    # 現在の設定を更新
    settings_list = config_manager.get_openai_settings_list()
    for s in settings_list:
        if s["name"] == profile_name:
            s["available_models"] = default_models.copy()
            break

    config_manager.save_openai_settings_list(settings_list)

    gr.Info(f"プロファイル「{profile_name}」のモデルリストをデフォルトにリセットしました。")
    return gr.update(choices=default_models, value=default_models[0] if default_models else "")


def handle_fetch_models(profile_name: str, base_url: str, api_key: str, free_only: bool = False):
    """
    APIからモデルリストを取得し、現在の選択肢に追加する。
    """
    if not profile_name:
        gr.Warning("プロファイルが選択されていません。")
        return gr.update()

    if not base_url:
        gr.Warning("Base URLが設定されていません。")
        return gr.update()

    # [Dynamic Injection] マネージドプロバイダの場合はグローバルAPIキーを優先/補完使用
    if profile_name == "Zhipu AI":
        global_key = config_manager.CONFIG_GLOBAL.get("zhipu_api_key")
        if global_key:
            api_key = global_key
    elif profile_name == "Moonshot AI":
        global_key = config_manager.CONFIG_GLOBAL.get("moonshot_api_key")
        if global_key:
            api_key = global_key

    # APIからモデルリストを取得
    fetched_models = config_manager.fetch_models_from_api(base_url, api_key, free_only=free_only)

    if not fetched_models:
        gr.Warning("モデルリストの取得に失敗しました。APIキーやBase URLを確認してください。")
        return gr.update()

    # 現在のプロファイル設定を取得
    settings_list = config_manager.get_openai_settings_list()
    for s in settings_list:
        if s["name"] == profile_name:
            current_models = s.get("available_models", [])
            if free_only:
                # 無料モデルのみの場合はリストをリセットして置き換える
                current_models = fetched_models
                added_count = len(fetched_models)
                msg = f"モデルリストを無料モデルのみ（{len(fetched_models)}件）にリセットしました。"
            else:
                # 通常時は既存モデル（⭐ マークを除いた名前）のセットを確認し、新規モデルのみ追加
                existing_clean = {m.lstrip("⭐ ") for m in current_models}
                added_count = 0
                for model in fetched_models:
                    if model not in existing_clean:
                        current_models.append(model)
                        added_count += 1
                msg = f"{len(fetched_models)} 件のモデルを取得し、{added_count} 件を追加しました。"

            s["available_models"] = current_models
            config_manager.save_openai_settings_list(settings_list)

            gr.Info(msg)
            return gr.update(choices=current_models)

    gr.Warning(f"プロファイル「{profile_name}」が見つかりませんでした。")
    return gr.update()


def handle_fetch_anthropic_models(api_key: str):
    """
    Anthropic APIからモデルリストを取得し、UIを更新する。
    """
    if not api_key:
        gr.Warning("Anthropic APIキーが設定されていません。")
        return gr.update()

    fetched_models = config_manager.fetch_anthropic_models(api_key)

    if not fetched_models:
        gr.Warning("Anthropicモデルリストの取得に失敗しました。APIキーを確認してください。")
        return gr.update()

    gr.Info(f"Anthropicから {len(fetched_models)} 件のモデルを取得しました。")
    return gr.update(choices=fetched_models)


def handle_toggle_favorite(profile_name: str, model_name: str):
    """
    選択中のモデルのお気に入り状態をトグルする（⭐ マークの付け外し）。
    """
    if not profile_name:
        gr.Warning("プロファイルが選択されていません。")
        return gr.update()

    if not model_name:
        gr.Warning("モデルが選択されていません。")
        return gr.update()

    # お気に入りマーク
    FAVORITE_MARK = "⭐ "
    is_favorite = model_name.startswith(FAVORITE_MARK)

    # トグル後の新しいモデル名
    if is_favorite:
        new_model_name = model_name[len(FAVORITE_MARK):]
        action = "解除"
    else:
        new_model_name = FAVORITE_MARK + model_name
        action = "追加"

    # 設定を更新
    settings_list = config_manager.get_openai_settings_list()
    for s in settings_list:
        if s["name"] == profile_name:
            available_models = s.get("available_models", [])

            if model_name in available_models:
                idx = available_models.index(model_name)
                available_models[idx] = new_model_name
                config_manager.save_openai_settings_list(settings_list)

                gr.Info(f"お気に入り{action}: {new_model_name}")
                return gr.update(choices=available_models, value=new_model_name)

    gr.Warning(f"モデル「{model_name}」が見つかりませんでした。")
    return gr.update()





# ==========================================
# [v25] テーマ・表示設定管理ロジック
# ==========================================






# --- 書き置き機能（自律行動向けメッセージ）---

def _get_user_memo_path(room_name: str) -> str:
    """書き置きファイルのパスを取得する。"""
    return os.path.join(constants.ROOMS_DIR, room_name, "user_memo.txt")


def load_user_memo(room_name: str) -> str:
    """書き置き内容を読み込む。"""
    if not room_name:
        return ""
    memo_path = _get_user_memo_path(room_name)
    if os.path.exists(memo_path):
        with open(memo_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def handle_save_user_memo(room_name: str, memo_content: str) -> None:
    """書き置きを保存する。"""
    if memo_content is None or str(memo_content).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、データ保護のために保存を中止しました。")
        return memo_content

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return

    memo_path = _get_user_memo_path(room_name)
    try:
        with open(memo_path, "w", encoding="utf-8") as f:
            f.write(memo_content.strip())
        gr.Info("📝 書き置きを保存しました。次回の自律行動時にAIに渡されます。")
    except Exception as e:
        gr.Error(f"書き置きの保存に失敗しました: {e}")


def handle_clear_user_memo(room_name: str) -> str:
    """書き置きをクリアする。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return ""

    memo_path = _get_user_memo_path(room_name)
    try:
        with open(memo_path, "w", encoding="utf-8") as f:
            f.write("")
        gr.Info("書き置きをクリアしました。")
        return ""
    except Exception as e:
        gr.Error(f"書き置きのクリアに失敗しました: {e}")
        return ""


# =============================================================================
# 会話ログ RAWエディタ (Chat Log Raw Editor)
# =============================================================================

def _resolve_chat_log_raw_path(room_name: str, selected_month: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    RAWログエディタの対象月次ファイルを決定する。

    「最新」は現在月が非空なら現在月、空または未作成なら直近の非空月次ファイルを返す。
    これにより、読み込み時に過去月を表示したのに保存時だけ現在月へ書く事故を避ける。
    """
    if not room_name:
        return None, None

    if selected_month and selected_month != "最新":
        if not re.match(r"^\d{4}-\d{2}$", str(selected_month)):
            return None, None
        base_path = os.path.join(constants.ROOMS_DIR, room_name)
        return os.path.join(base_path, constants.LOGS_DIR_NAME, f"{selected_month}.txt"), str(selected_month)

    log_path, _, _, _, _, _, _ = get_room_files_paths(room_name)
    current_month = os.path.splitext(os.path.basename(log_path))[0] if log_path else None
    if log_path and utils.is_chat_log_month_cleared(log_path):
        return log_path, current_month
    if log_path and os.path.exists(log_path) and os.path.getsize(log_path) > 0:
        return log_path, current_month

    logs_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.LOGS_DIR_NAME)
    if os.path.isdir(logs_dir):
        valid_files = []
        for f_path in glob.glob(os.path.join(logs_dir, "*.txt")):
            basename = os.path.basename(f_path)
            if re.match(r"^\d{4}-\d{2}\.txt$", basename) and os.path.getsize(f_path) > 0:
                valid_files.append(f_path)
        if valid_files:
            latest_non_empty = sorted(valid_files, reverse=True)[0]
            resolved_month = os.path.splitext(os.path.basename(latest_non_empty))[0]
            if latest_non_empty != log_path:
                print(f"[DEBUG] 最新ログが空のため、直近の非空ログを使用します: {latest_non_empty}")
            return latest_non_empty, resolved_month

    return log_path, current_month

def handle_load_chat_log_raw(
    room_name: str,
    selected_month: Optional[str] = None,
    add_timestamp: bool = True,
    display_thoughts: bool = True,
    screenshot_mode: bool = False,
    redaction_rules: list = None
) -> tuple:
    """
    RAWログエディタタブが選択された時、または月が変更された時に、指定された月（または最新）のログを読み込む。
    RAWテキストと、プレビュー用の整形済み履歴の両方を返す。
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(value=""), []

    log_path, _resolved_month = _resolve_chat_log_raw_path(room_name, selected_month)
    single_file = True

    if log_path and os.path.exists(log_path):
        try:
            # RAWテキスト読込
            with open(log_path, "r", encoding="utf-8") as f:
                content = f.read()

            # プレビュー用履歴生成 (utils.load_chat_log の single_file_only を利用)
            raw_messages = utils.load_chat_log(log_path, single_file_only=single_file)
            formatted_history, _ = format_history_for_gradio(
                messages=raw_messages,
                current_room_folder=room_name,
                add_timestamp=add_timestamp,
                display_thoughts=display_thoughts,
                screenshot_mode=screenshot_mode,
                redaction_rules=redaction_rules
            )

            return gr.update(value=content), formatted_history
        except Exception as e:
            gr.Error(f"ログファイルの読み込みに失敗しました: {e}")
            return gr.update(value=""), []

    # ファイルが存在しない場合
    if selected_month and selected_month != "最新":
         gr.Warning(f"指定された月のログファイルが見つかりません: {selected_month}.txt")
    return gr.update(value=""), []


def handle_save_chat_log_raw(
    room_name: str,
    raw_content: str,
    api_history_limit: str,
    add_timestamp: bool,
    display_thoughts: bool,
    screenshot_mode: bool,
    redaction_rules: list,
    selected_month: Optional[str] = None
) -> tuple:
    """
    RAWログを保存し、チャット表示を更新する。
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update(), gr.update(), gr.update()

    # 保存先パスの決定。読み込み時と同じ解決規則を使い、「最新」が空の現在月へ
    # 誤って書き戻されることを防ぐ。
    log_path, _resolved_month = _resolve_chat_log_raw_path(room_name, selected_month)

    if not log_path:
        gr.Error("ログファイルのパスが取得できませんでした。")
        return gr.update(), gr.update(), gr.update(), gr.update()

    try:
        # 空内容も明示的な編集結果として保存する。従来は安全装置が元内容を
        # エディタへ戻していたため、「全選択して削除→保存」で会話が復活して見えた。
        # 保存前のルームバックアップと隣接 .bak は空保存でも必ず作成する。
        raw_content = str(raw_content or "")
        if not raw_content.strip():
            raw_content = ""

        # バックアップ作成（安全装置）
        room_manager.create_backup(room_name, 'log')
        if os.path.exists(log_path):
            shutil.copy2(log_path, log_path + ".bak")

        # 末尾に改行がない場合は追加（最低1つの改行を保証）
        if raw_content and not raw_content.endswith('\n'):
            raw_content += '\n'

        # ファイル保存
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(raw_content)
        if raw_content:
            utils.clear_chat_log_month_marker(log_path)
        else:
            utils.mark_chat_log_month_cleared(log_path)
        utils.invalidate_chat_log_cache(log_path)
        gr.Info(f"会話ログを保存しました ({os.path.basename(log_path)})")

        # チャット表示を更新（reload_chat_log を再利用）
        # ※ reload_chat_log は utils.load_chat_log を呼ぶため、最新の統合的な履歴が反映される
        main_history, mapping = reload_chat_log(
            room_name, api_history_limit, add_timestamp,
            display_thoughts, screenshot_mode, redaction_rules
        )

        # プレビュー表示も更新
        raw_messages = utils.load_chat_log(log_path, single_file_only=True)
        preview_history, _ = format_history_for_gradio(
            messages=raw_messages,
            current_room_folder=room_name,
            add_timestamp=add_timestamp,
            display_thoughts=display_thoughts,
            screenshot_mode=screenshot_mode,
            redaction_rules=redaction_rules
        )

        return (
            gr.update(value=raw_content),    # chat_log_raw_editor
            main_history,                   # chatbot_display
            mapping,                        # current_log_map_state
            preview_history                 # chat_log_preview_chatbot
        )
    except Exception as e:
        gr.Error(f"ログの保存中にエラーが発生しました: {e}")
        traceback.print_exc()
        return gr.update(), gr.update(), gr.update(), []


def handle_reload_chat_log_raw(
    room_name: str,
    selected_month: Optional[str] = None,
    add_timestamp: bool = True,
    display_thoughts: bool = True,
    screenshot_mode: bool = False,
    redaction_rules: list = None
) -> tuple:
    """
    RAWログを再読込する（保存せずに最後に保存した状態に戻す）。
    """
    return handle_load_chat_log_raw(
        room_name, selected_month, add_timestamp,
        display_thoughts, screenshot_mode, redaction_rules
    )


def handle_update_log_preview(
    room_name: str,
    selected_month: Optional[str] = None,
    add_timestamp: bool = True,
    display_thoughts: bool = True,
    screenshot_mode: bool = False,
    redaction_rules: list = None
) -> List[Tuple]:
    """
    プレビュー用チャットボットのみを更新する（RAWエディタの内容は変更しない）。
    設定変更（スクリーンショットモード等）時の反映に使用。
    """
    if not room_name:
        return gr.update(value=[])

    # 月が指定されていない、または「最新」の場合は、本来のcurrent_monthのパスを取得
    if not selected_month or selected_month == "最新":
        log_path, _, _, _, _, _, _ = get_room_files_paths(room_name)
    else:
        # 指定された月のファイルを構築
        base_path = os.path.join(constants.ROOMS_DIR, room_name)
        log_path = os.path.join(base_path, constants.LOGS_DIR_NAME, f"{selected_month}.txt")

    if log_path and os.path.exists(log_path):
        try:
            # プレビュー用履歴生成 (utils.load_chat_log の single_file_only を利用)
            raw_messages = utils.load_chat_log(log_path, single_file_only=True)
            formatted_history, _ = format_history_for_gradio(
                messages=raw_messages,
                current_room_folder=room_name,
                add_timestamp=add_timestamp,
                display_thoughts=display_thoughts,
                screenshot_mode=screenshot_mode,
                redaction_rules=redaction_rules
            )
            return formatted_history
        except Exception as e:
            print(f"プレビュー生成に失敗しました: {e}")
            return gr.update(value=[])

    return gr.update(value=[])


def handle_refresh_chat_log_months(room_name: str) -> gr.update:
    """
    logs/ ディレクトリ内の .txt ファイルを抽出し、年月リストを返す。
    """
    if not room_name:
        return gr.update(choices=["最新"], value="最新")

    base_path = os.path.join(constants.ROOMS_DIR, room_name)
    logs_dir = os.path.join(base_path, constants.LOGS_DIR_NAME)

    if not os.path.exists(logs_dir):
        return gr.update(choices=["最新"], value="最新")

    files = glob.glob(os.path.join(logs_dir, "*.txt"))
    # ファイル名 (YYYY-MM.txt) から YYYY-MM を抽出
    months = []
    for f in files:
        basename = os.path.basename(f)
        month = os.path.splitext(basename)[0]
        # YYYY-MM 形式か、あるいは 0000-00 などの特殊なもの
        months.append(month)

    # 逆順（新しい順）に並び替える
    months.sort(reverse=True)

    choices = ["最新"] + months
    return gr.update(choices=choices, value="最新")


# =============================================================================
# 「お出かけ」機能 - ペルソナデータエクスポート
# =============================================================================

def _get_outing_export_folder(room_name: str) -> str:
    """お出かけエクスポート先フォルダのパスを取得・作成する。"""
    folder_path = os.path.join(constants.ROOMS_DIR, room_name, "private", "outing")
    os.makedirs(folder_path, exist_ok=True)
    return folder_path


def _get_recent_log_entries(log_path: str, count: int, include_timestamp=True, include_model=True) -> list:
    """
    ログファイルから直近N件の会話エントリを取得する。
    Returns: [(header, content), ...]
    """
    if not os.path.exists(log_path):
        return []

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()

        # ログエントリをパース（## ROLE:NAME または [NAME] ヘッダーで分割）
        import re
        entries = []

        lines = content.split('\n')
        current_header = None
        current_content = []

        # ヘッダーパターン: ## ROLE:NAME または [NAME]
        header_pattern = r'^(?:## [^:]+:|\[)([^\]\n]+)(?:\])?'

        for line in lines:
            # タイムスタンプ・モデル名行のパターン: YYYY-MM-DD (Day) HH:MM:SS | Model
            ts_model_pattern = r'^\d{4}-\d{2}-\d{2} \(.*\d{2}:\d{2}:\d{2}(?: \| .*)?$'

            # ヘッダーチェック
            header_match = re.match(header_pattern, line)
            if header_match:
                # 前のエントリを保存
                if current_header is not None:
                    raw_text = '\n'.join(current_content).strip()
                    # エクスポート用にメタタグと思考を除去
                    cleaned_text = utils.clean_persona_text(raw_text)
                    entries.append((current_header, cleaned_text))
                current_header = header_match.group(1).strip()
                current_content = []
            else:
                # コンテンツ行の処理
                is_ts_model_line = re.match(ts_model_pattern, line)
                if is_ts_model_line:
                    filtered_line = line
                    if not include_timestamp and not include_model:
                        continue # 両方除外なら行ごとスキップ

                    parts = line.split('|')
                    if len(parts) == 2:
                        ts = parts[0].strip()
                        model = parts[1].strip()
                        if not include_timestamp and include_model:
                            filtered_line = f"| {model}"
                        elif include_timestamp and not include_model:
                            filtered_line = ts
                    elif not include_timestamp:
                        # タイムスタンプのみの行で除外設定ならスキップ
                        if re.match(r'^\d{4}-\d{2}-\d{2} \(.*\d{2}:\d{2}:\d{2}$', line.strip()):
                            continue

                    current_content.append(filtered_line)
                else:
                    current_content.append(line)

        # 最後のエントリを保存
        if current_header is not None:
            raw_text = '\n'.join(current_content).strip()
            # エクスポート用にメタタグと思考を除去
            cleaned_text = utils.clean_persona_text(raw_text)
            entries.append((current_header, cleaned_text))

        # 直近N件を取得
        return entries[-count:] if len(entries) > count else entries
    except Exception as e:
        print(f"Error reading log file: {e}")
        import traceback
        traceback.print_exc()
        return []



def _get_log_entries_since_date(log_path: str, since_date_str: str, include_timestamp=True, include_model=True) -> list:
    """
    指定された日付以降のログエントリを抽出する。
    since_date_str: YYYY-MM-DD
    """
    if not os.path.exists(log_path):
        return []

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()

        import re
        entries = []
        lines = content.split('\n')
        current_header = None
        current_content = []
        current_date = "0000-00-00"

        # ヘッダーパターン: ## ROLE:NAME または [NAME]
        header_pattern = r'^(?:## [^:]+:|\[)([^\]\n]+)(?:\])?'
        # タイムスタンプ・モデル名行のパターン: YYYY-MM-DD (Day) HH:MM:SS | Model
        ts_model_pattern = r'^(\d{4}-\d{2}-\d{2}) \(.*\d{2}:\d{2}:\d{2}(?: \| .*)?$'

        target_entries = []

        def save_entry(h, contents, date):
            if h is not None and date >= since_date_str:
                raw_text = '\n'.join(contents).strip()
                # 思考署名やメタタグのみを除去
                cleaned_text = utils.clean_persona_text(raw_text)
                target_entries.append((h, cleaned_text))

        for line in lines:
            # ヘッダーチェック
            header_match = re.match(header_pattern, line)
            if header_match:
                save_entry(current_header, current_content, current_date)
                current_header = header_match.group(1).strip()
                current_content = []
            else:
                # コンテンツ行の処理（日付更新の可能性あり）
                ts_match = re.match(ts_model_pattern, line)
                if ts_match:
                    current_date = ts_match.group(1)
                    # フィルタリング
                    filtered_line = line
                    if not include_timestamp and not include_model:
                        continue
                    parts = line.split('|')
                    if len(parts) == 2:
                        ts = parts[0].strip()
                        model = parts[1].strip()
                        if not include_timestamp and include_model:
                            filtered_line = f"| {model}"
                        elif include_timestamp and not include_model:
                            filtered_line = ts
                    elif not include_timestamp:
                        if re.match(r'^\d{4}-\d{2}-\d{2} \(.*\d{2}:\d{2}:\d{2}$', line.strip()):
                            continue
                    current_content.append(filtered_line)
                else:
                    current_content.append(line)

        # 最後の処理
        save_entry(current_header, current_content, current_date)
        return target_entries

    except Exception as e:
        print(f"Error in _get_log_entries_since_date: {e}")
        import traceback
        traceback.print_exc()
        return []


def _get_today_log_entries_with_summary(
    room_name: str,
    log_path: str,
    auto_summary: bool,
    summary_threshold: int,
    include_timestamp: bool,
    include_model: bool
) -> str:
    """
    本日分のログを抽出し、必要に応じて自動要約を適用して返す。
    """
    import gemini_api
    import summary_manager

    # 1. 本日分の開始日を特定
    today_cutoff = gemini_api._get_effective_today_cutoff(room_name)

    # 2. その日付以降の全エントリを取得
    entries = _get_log_entries_since_date(log_path, today_cutoff, include_timestamp, include_model)

    if not entries:
        return ""

    # 3. テキスト化
    full_text = "\n\n".join([f"[{header}]\n{content}" for header, content in entries])

    # 4. 自動要約チェック
    if auto_summary and len(full_text) > summary_threshold:
        # 直近の会話を保護
        keep_count = constants.AUTO_SUMMARY_KEEP_RECENT_TURNS * 2
        if len(entries) > keep_count:
            older_entries = entries[:-keep_count]
            recent_entries = entries[-keep_count:]

            # 要約用メッセージリスト作成
            older_msgs = []
            for h, c in older_entries:
                role = "USER" if h.lower() == "user" else "AGENT"
                older_msgs.append({"role": role, "responder": h, "content": c})

            # APIキー取得
            api_key_name = config_manager.initial_api_key_name_global
            api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)

            if api_key:
                gr.Info("お出かけ用ログを自動要約中...")
                # 既存の要約があれば結合されるロジックにするか？
                # お出かけ用は単発なので None で渡す
                summary = summary_manager.generate_summary(older_msgs, None, room_name, api_key)
                if summary:
                    recent_text = "\n\n".join([f"[{header}]\n{content}" for header, content in recent_entries])
                    return f"【本日のこれまでの会話の要約】\n{summary}\n\n---\n（以下は、要約以降および直近の会話です）\n\n{recent_text}"

    return full_text


def _get_episodic_memory_entries(room_name: str, days: int) -> str:
    """
    エピソード記憶から過去N日分のエントリを取得する。
    EpisodicMemoryManagerを使用して、月次フォルダに分散された記憶も取得する。
    """
    if days <= 0:
        return ""

    try:
        from episodic_memory_manager import EpisodicMemoryManager
        manager = EpisodicMemoryManager(room_name)

        # 全期のエピソード記憶を読み込む (レガシー + 月次)
        # _load_memory はプライベートメソッドだが、全件取得のために使用する
        all_episodes = manager._load_memory()
        if not all_episodes:
            return ""

        from datetime import datetime, timedelta
        cutoff_date = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff_date.strftime("%Y-%m-%d")

        filtered_entries = []
        for entry in all_episodes:
            if isinstance(entry, dict):
                date_key = entry.get("date", "")
                summary = entry.get("summary", "")

                # 日付範囲でフィルタリング
                date_start = date_key.strip()
                if '~' in date_start:
                    date_start = date_start.split("~")[0].strip()
                elif '～' in date_start:
                    date_start = date_start.split("～")[0].strip()

                if date_start >= cutoff_str:
                    filtered_entries.append((date_start, date_key, summary))

        # 日付順（昇順）にソート
        filtered_entries.sort(key=lambda x: x[0])

        if not filtered_entries:
            return ""

        # 整形: 既存のフォーマットに合わせる
        result_lines = []
        for _, date_key, summary in filtered_entries:
            result_lines.append(f"### {date_key}")
            # エクスポート用にメタタグを除去
            cleaned_summary = utils.clean_persona_text(summary if isinstance(summary, str) else str(summary))
            result_lines.append(cleaned_summary)
            result_lines.append("")

        return "\n".join(result_lines)

    except Exception as e:
        print(f"Error in _get_episodic_memory_entries: {e}")
        import traceback
        traceback.print_exc()
        return f"エピソード記憶の読み込みエラー: {e}"
def handle_export_outing_data(room_name: str, log_count: int, episode_days: int):
    """
    ペルソナデータをエクスポートする。

    収集するデータ:
    1. システムプロンプト (SystemPrompt.txt)
    2. コアメモリ (core_memory.txt)
    3. 直近の会話ログ (log.txt から最新N件)
    4. エピソード記憶 (memory/episodic_memory.json から過去N日分)

    出力形式: Markdown
    出力先: characters/{room_name}/private/outing/
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(visible=False)

    try:
        room_config = room_manager.get_room_config(room_name)
        display_name = room_config.get("room_name", room_name) if room_config else room_name

        # データ収集
        room_path = os.path.join(constants.ROOMS_DIR, room_name)

        # 1. システムプロンプト
        system_prompt_path = os.path.join(room_path, "SystemPrompt.txt")
        system_prompt = ""
        if os.path.exists(system_prompt_path):
            with open(system_prompt_path, "r", encoding="utf-8") as f:
                system_prompt = f.read().strip()

        # 2. コアメモリ
        core_memory_path = os.path.join(room_path, "core_memory.txt")
        core_memory = ""
        if os.path.exists(core_memory_path):
            with open(core_memory_path, "r", encoding="utf-8") as f:
                core_memory = f.read().strip()

        # 3. 直近の会話ログ
        log_path = os.path.join(room_path, "log.txt")
        log_entries = _get_recent_log_entries(log_path, int(log_count))

        # 4. エピソード記憶
        episodic_text = _get_episodic_memory_entries(room_name, int(episode_days))

        # Markdownを生成
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        md_content = f"""# {display_name} ペルソナデータ

**エクスポート日時:** {timestamp}
**元ルーム:** {room_name}

---

## システムプロンプト

```
{system_prompt if system_prompt else "(未設定)"}
```

---

## コアメモリ

{core_memory if core_memory else "(未設定)"}

---

"""

        # エピソード記憶（背景情報として先に配置）
        if int(episode_days) > 0:
            md_content += f"## エピソード記憶（過去{int(episode_days)}日分）\n\n"
            if episodic_text:
                md_content += episodic_text
            else:
                md_content += "(エピソード記憶がありません)\n"
            md_content += "\n---\n\n"

        # 直近の会話ログ（最新の具体的なやりとり）
        md_content += f"## 直近の会話ログ（最新{int(log_count)}件）\n\n"

        if log_entries:
            for role, content in log_entries:
                md_content += f"**[{role}]**\n{content}\n\n"
        else:
            md_content += "(会話ログがありません)\n\n"

        # ファイル保存
        export_folder = _get_outing_export_folder(room_name)
        file_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        export_filename = f"{display_name}_outing_{file_timestamp}.md"
        export_path = os.path.join(export_folder, export_filename)

        with open(export_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        gr.Info(f"ペルソナデータをエクスポートしました。\n保存先: {export_path}")

        return gr.update(value=export_path, visible=True)

    except Exception as e:
        gr.Error(f"エクスポート中にエラーが発生しました: {e}")
        traceback.print_exc()
        return gr.update(visible=False)


def handle_open_outing_folder(room_name: str):
    """エクスポート先フォルダをエクスプローラーで開く。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return

    try:
        folder_path = _get_outing_export_folder(room_name)

        if os.name == "nt":  # Windows
            os.startfile(folder_path)
        elif os.name == "posix":  # macOS / Linux
            subprocess.run(["open", folder_path] if sys.platform == "darwin" else ["xdg-open", folder_path])

        gr.Info(f"フォルダを開きました: {folder_path}")
    except Exception as e:
        gr.Error(f"フォルダを開けませんでした: {e}")


def _split_core_memory(core_memory: str) -> tuple:
    """
    コアメモリを永続記憶と日記に分割する。

    Returns:
        (permanent, diary): 永続記憶部分と日記部分のタプル
    """
    permanent = ""
    diary = ""

    # 日記セクションの開始を探す
    diary_markers = ["--- [日記 (Diary)", "--- [日記(Diary)", "[日記 (Diary)"]
    diary_start_idx = -1

    for marker in diary_markers:
        idx = core_memory.find(marker)
        if idx != -1:
            diary_start_idx = idx
            break

    if diary_start_idx != -1:
        permanent = core_memory[:diary_start_idx].strip()
        diary = core_memory[diary_start_idx:].strip()
    else:
        permanent = core_memory.strip()

    return permanent, diary





# (古い重複コードは削除されました)





def handle_generate_outing_preview(
    room_name: str,
    log_count: int,
    episode_days: int,
    include_system_prompt: bool,
    include_permanent: bool,
    include_diary: bool,
    include_episodic: bool,
    include_logs: bool
):
    """
    エクスポートプレビューを生成し、文字数を計算する。

    Returns:
        (preview_text, char_count_markdown): プレビューテキストと文字数表示（内訳付き）
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return "", "📝 推定文字数: ---"

    try:
        room_config = room_manager.get_room_config(room_name)
        display_name = room_config.get("room_name", room_name) if room_config else room_name

        room_path = os.path.join(constants.ROOMS_DIR, room_name)

        # データ収集（セクションごとに文字数も記録）
        sections = []
        section_counts = []  # (セクション名, 文字数)

        # 1. システムプロンプト
        if include_system_prompt:
            system_prompt_path = os.path.join(room_path, "SystemPrompt.txt")
            if os.path.exists(system_prompt_path):
                with open(system_prompt_path, "r", encoding="utf-8") as f:
                    system_prompt = f.read().strip()
                if system_prompt:
                    section_text = f"## システムプロンプト\n\n```\n{system_prompt}\n```"
                    sections.append(section_text)
                    section_counts.append(("システムプロンプト", len(section_text)))

        # 2. コアメモリ（永続記憶・日記を分割）
        core_memory_path = os.path.join(room_path, "core_memory.txt")
        if os.path.exists(core_memory_path):
            with open(core_memory_path, "r", encoding="utf-8") as f:
                core_memory = f.read().strip()

            permanent, diary = _split_core_memory(core_memory)

            if include_permanent and permanent:
                section_text = f"## コアメモリ（永続記憶）\n\n{permanent}"
                sections.append(section_text)
                section_counts.append(("コアメモリ(永続)", len(section_text)))

            if include_diary and diary:
                section_text = f"## コアメモリ（日記要約）\n\n{diary}"
                sections.append(section_text)
                section_counts.append(("コアメモリ(日記)", len(section_text)))

        # 3. エピソード記憶
        if include_episodic and int(episode_days) > 0:
            episodic_text = _get_episodic_memory_entries(room_name, int(episode_days))
            if episodic_text:
                section_text = f"## エピソード記憶（過去{int(episode_days)}日分）\n\n{episodic_text}"
            else:
                section_text = f"## エピソード記憶（過去{int(episode_days)}日分）\n\n(エピソード記憶がありません)"
            sections.append(section_text)
            section_counts.append(("エピソード記憶", len(section_text)))

        # 4. 会話ログ
        if include_logs:
            log_path = os.path.join(room_path, "log.txt")
            log_entries = _get_recent_log_entries(log_path, int(log_count))
            if log_entries:
                log_text = ""
                for role, content in log_entries:
                    log_text += f"**[{role}]**\n{content}\n\n"
                section_text = f"## 直近の会話ログ（最新{int(log_count)}件）\n\n{log_text}"
            else:
                section_text = f"## 直近の会話ログ（最新{int(log_count)}件）\n\n(会話ログがありません)"
            sections.append(section_text)
            section_counts.append(("会話ログ", len(section_text)))

        # ヘッダー
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"# {display_name} ペルソナデータ\n\n**エクスポート日時:** {timestamp}\n**元ルーム:** {room_name}\n\n---\n\n"

        # 結合
        preview_text = header + "\n\n---\n\n".join(sections)

        # 文字数カウント（内訳付き）
        total_count = len(preview_text)

        # 内訳を作成
        breakdown_lines = []
        for i, (name, count) in enumerate(section_counts):
            prefix = "└" if i == len(section_counts) - 1 else "├"
            breakdown_lines.append(f"   {prefix} {name}: **{count:,}**字")

        breakdown = "\n".join(breakdown_lines)
        char_count_md = f"📝 推定文字数: **{total_count:,}** 文字\n{breakdown}"

        return preview_text, char_count_md

    except Exception as e:
        gr.Error(f"プレビュー生成中にエラーが発生しました: {e}")
        traceback.print_exc()
        return "", "📝 推定文字数: エラー"


def handle_summarize_outing_text(preview_text: str, room_name: str, target_section: str = "all"):
    """
    AIを使ってエクスポートテキストを要約圧縮する。
    """
    if not preview_text or not preview_text.strip():
        gr.Warning("プレビューテキストがありません。先に「プレビュー生成」を実行してください。")
        return preview_text, "📝 推定文字数: ---"

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return preview_text, "📝 推定文字数: ---"

    if not config_manager.GEMINI_API_KEYS:
        gr.Error("APIキーが設定されていません。")
        return preview_text, f"📝 推定文字数: **{len(preview_text):,}** 文字"

    try:
        from llm_factory import LLMFactory

        effective_settings = config_manager.get_effective_settings(room_name)

        # 圧縮プロンプト
        prompt = f"""以下のAIペルソナデータを、重要な情報を保持しながらできるだけ圧縮してください。

【圧縮のルール】
- 人格の核心（性格、信念、関係性）は必ず保持
- 冗長な表現は簡潔に
- Markdown形式を維持
- セクション構造（##見出し）を維持

【元データ】
{preview_text}"""

        gr.Info("AIで圧縮中...")
        # 内部処理は共通キー＋ローテーション（仕様 §7）。キー選択・429/503リトライは共通機構に委譲。
        result, _used_key = LLMFactory.invoke_internal_llm(
            internal_role="summarization",
            prompt=prompt,
            room_name=room_name,
            generation_config=effective_settings,
        )

        if result and result.content:
            summarized = utils.get_content_as_string(result.content).strip()
            char_count = len(summarized)
            gr.Info(f"圧縮完了！ {len(preview_text):,} → {char_count:,} 文字")
            return summarized, f"📝 推定文字数: **{char_count:,}** 文字"
        else:
            gr.Warning("AIからの応答がありませんでした。")
            return preview_text, f"📝 推定文字数: **{len(preview_text):,}** 文字"

    except Exception as e:
        gr.Error(f"AI圧縮中にエラーが発生しました: {e}")
        traceback.print_exc()
        return preview_text, f"📝 推定文字数: **{len(preview_text):,}** 文字"


def handle_export_outing_from_preview(preview_text: str, room_name: str):
    """
    プレビューテキスト（編集済み可）をファイルに保存する。
    """
    if not preview_text or not preview_text.strip():
        gr.Warning("エクスポートするテキストがありません。先に「プレビュー生成」を実行してください。")
        return gr.update(visible=False)

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(visible=False)

    try:
        room_config = room_manager.get_room_config(room_name)
        display_name = room_config.get("room_name", room_name) if room_config else room_name

        # ファイル保存
        export_folder = _get_outing_export_folder(room_name)
        file_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        export_filename = f"{display_name}_outing_{file_timestamp}.md"
        export_path = os.path.join(export_folder, export_filename)

        with open(export_path, "w", encoding="utf-8") as f:
            f.write(preview_text)

        gr.Info(f"ペルソナデータをエクスポートしました。\n保存先: {export_path}")

        return gr.update(value=export_path, visible=True)

    except Exception as e:
        gr.Error(f"エクスポート中にエラーが発生しました: {e}")
        traceback.print_exc()
        return gr.update(visible=False)


# ===== 専用タブ用ハンドラ =====

def handle_outing_load_all_sections(
    room_name: str,
    episode_days: int,
    log_mode: str,
    log_count: int,
    auto_summary: bool,
    summary_threshold: int,
    include_timestamp=True,
    include_model=True
):
    """
    お出かけ専用タブ用：全セクションのデータを読み込む
    Returns: (system_prompt, sys_chars, permanent, perm_chars, diary, diary_chars,
              episodic, ep_chars, logs, logs_chars, preview, total_chars)
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        empty = ""
        char_str = "文字数: 0"
        return empty, char_str, empty, char_str, empty, char_str, empty, char_str, empty, char_str, empty, "📝 合計文字数: 0"

    try:
        # タプルで返される: (log_file, system_prompt_file, profile_image_path, memory_main_path, notepad_path)
        log_path, system_prompt_path, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)

        # システムプロンプト
        system_prompt = ""
        if system_prompt_path and os.path.exists(system_prompt_path):
            with open(system_prompt_path, "r", encoding="utf-8") as f:
                system_prompt = f.read().strip()

        # コアメモリを読み込んで分割
        core_memory_path = os.path.join(constants.ROOMS_DIR, room_name, "core_memory.txt")
        core_memory_text = ""
        if os.path.exists(core_memory_path):
            with open(core_memory_path, "r", encoding="utf-8") as f:
                core_memory_text = f.read()
        permanent, diary = _split_core_memory(core_memory_text)

        # エピソード記憶（この関数は直接文字列を返す）
        episodic = ""
        if episode_days > 0:
            episodic = _get_episodic_memory_entries(room_name, episode_days)

        # 会話ログ
        logs = ""
        if log_path and os.path.exists(log_path):
            if log_mode == "本日分（高度）":
                logs = _get_today_log_entries_with_summary(
                    room_name, log_path, auto_summary, summary_threshold, include_timestamp, include_model
                )
            else:
                log_entries = _get_recent_log_entries(log_path, log_count, include_timestamp, include_model)
                logs = "\n\n".join([f"[{header}]\n{content}" for header, content in log_entries])

        # 文字数計算
        sys_chars = len(system_prompt)
        perm_chars = len(permanent)
        diary_chars = len(diary)
        ep_chars = len(episodic)
        logs_chars = len(logs)

        # プレビュー生成 (初期状態は全てON)
        preview = handle_outing_update_preview(
            system_prompt, True,
            permanent, True,
            diary, True,
            episodic, True,
            logs, True,
            True # wrap_logs
        )

        total = len(preview)

        gr.Info(f"データを読み込みました（合計 {total:,} 文字）")

        return (
            system_prompt, f"文字数: **{sys_chars:,}**",
            permanent, f"文字数: **{perm_chars:,}**",
            diary, f"文字数: **{diary_chars:,}**",
            episodic, f"文字数: **{ep_chars:,}**",
            logs, f"文字数: **{logs_chars:,}**",
            preview,
            f"📝 合計文字数: **{total:,}** 文字"
        )

    except Exception as e:
        gr.Error(f"読み込みエラー: {e}")
        traceback.print_exc()
        empty = ""
        char_str = "文字数: エラー"
        return empty, char_str, empty, char_str, empty, char_str, empty, char_str, empty, char_str, empty, "📝 合計文字数: エラー"

def handle_outing_update_preview(
    sys_text, sys_enabled,
    perm_text, perm_enabled,
    diary_text, diary_enabled,
    ep_text, ep_enabled,
    logs_text, logs_enabled,
    wrap_logs_with_tags=True
):
    """
    各セクションの内容と有効フラグに基づいて、エクスポート用の結合テキストを生成する。
    """
    sections = []

    if sys_enabled and sys_text and sys_text.strip():
        sections.append(f"## システムプロンプト\n\n{sys_text.strip()}")

    if perm_enabled and perm_text and perm_text.strip():
        sections.append(f"## コアメモリ（永続記憶）\n\n{perm_text.strip()}")

    if diary_enabled and diary_text and diary_text.strip():
        sections.append(f"## コアメモリ（日記要約）\n\n{diary_text.strip()}")

    if ep_enabled and ep_text and ep_text.strip():
        sections.append(f"## エピソード記憶\n\n{ep_text.strip()}")

    if logs_enabled and logs_text and logs_text.strip():
        log_content = logs_text.strip()
        if wrap_logs_with_tags:
            log_content = f"<nexus_ark_past_logs>\n{log_content}\n</nexus_ark_past_logs>"
        sections.append(f"## 直近の会話ログ\n\n{log_content}")

    if not sections:
        return ""

    combined = "\n\n---\n\n".join(sections)
    return combined

def handle_outing_export_from_preview(preview_text: str, room_name: str):
    """
    プレビューエリアの内容をファイルにエクスポートする。
    """
    if not preview_text or not preview_text.strip():
        gr.Warning("エクスポートする内容がありません。")
        return gr.update(visible=False)

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(visible=False)

    try:
        room_config = room_manager.get_room_config(room_name) or {}
        display_name = room_config.get("agent_display_name") or room_name

        export_folder = _get_outing_export_folder(room_name)
        file_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        export_filename = f"{display_name}_outing_{file_timestamp}.md"
        export_path = os.path.join(export_folder, export_filename)

        with open(export_path, "w", encoding="utf-8") as f:
            f.write(preview_text)

        gr.Info(f"エクスポート完了！\n保存先: {export_path}")
        return gr.update(value=export_path, visible=True)

    except Exception as e:
        gr.Error(f"エクスポートエラー: {e}")
        traceback.print_exc()
        return gr.update(visible=False)


def handle_outing_compress_section(text: str, section_name: str, room_name: str):
    """
    お出かけ専用タブ用：単一セクションをAIで圧縮
    """
    if not text or not text.strip():
        gr.Warning(f"{section_name}が空です。")
        return text, f"文字数: 0"

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return text, f"文字数: {len(text):,}"

    if not config_manager.GEMINI_API_KEYS:
        gr.Error("APIキーが設定されていません。")
        return text, f"文字数: {len(text):,}"

    try:
        from llm_factory import LLMFactory

        effective_settings = config_manager.get_effective_settings(room_name)

        prompt = f"""以下の{section_name}を、重要な情報を保持しながら圧縮してください。

【制約事項】
- 人格の核心となる情報は必ず保持すること
- 冗長な表現は簡潔にまとめること
- **出力には「圧縮後のテキストのみ」を含めること**
- 「はい、承知しました」や「以下に要約します」といった前置きや説明、挨拶は**一切不要**です

【元データ】
{text}"""

        gr.Info(f"{section_name}を圧縮中...")
        # 内部処理は共通キー＋ローテーション（仕様 §7）。キー選択・429/503リトライは共通機構に委譲。
        result, _used_key = LLMFactory.invoke_internal_llm(
            internal_role="summarization",
            prompt=prompt,
            room_name=room_name,
            generation_config=effective_settings,
        )

        if result and result.content:
            summarized = utils.get_content_as_string(result.content).strip()
            char_count = len(summarized)
            gr.Info(f"圧縮完了！ {len(text):,} → {char_count:,} 文字")
            return summarized, f"文字数: **{char_count:,}**"
        else:
            gr.Warning("AIからの応答がありませんでした。")
            return text, f"文字数: {len(text):,}"

    except Exception as e:
        gr.Error(f"圧縮エラー: {e}")
        traceback.print_exc()
        return text, f"文字数: {len(text):,}"


def _strip_past_logs(text: str) -> str:
    """
    <nexus_ark_past_logs>...</nexus_ark_past_logs> タグで囲まれた部分を除去する。
    「## 直近の会話ログ」見出しがその直前にある場合は、それも含めて除去する。
    """
    if not text:
        return ""

    # 1. 見出し + タグのパターン（改行や空白の揺らぎを許容）
    # ※ re.DOTALL により改行を含めてマッチング。見出しとタグの間の任意の空白・改行に対応。
    header_with_tag = re.compile(r'#+\s*直近の会話ログ\s*[\r\n\s]*<nexus_ark_past_logs>.*?</nexus_ark_past_logs>', re.DOTALL)
    text = header_with_tag.sub('', text)

    # 2. 見出しがない単独タグのパターン
    tag_only = re.compile(r'<nexus_ark_past_logs>.*?</nexus_ark_past_logs>', re.DOTALL)
    text = tag_only.sub('', text)

    return text.strip()

def handle_outing_export_sections(
    room_name: str,
    system_prompt: str, sys_enabled: bool,
    permanent: str, perm_enabled: bool,
    diary: str, diary_enabled: bool,
    episodic: str, ep_enabled: bool,
    logs: str, logs_enabled: bool,
    wrap_logs_with_tags: bool = True
):
    """
    お出かけ専用タブ用：有効なセクションを結合してエクスポート
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(visible=False)

    try:
        # 有効なセクションを結合
        sections = []

        if sys_enabled and system_prompt.strip():
            sections.append(f"## システムプロンプト\n\n{system_prompt.strip()}")

        if perm_enabled and permanent.strip():
            sections.append(f"## コアメモリ（永続記憶）\n\n{permanent.strip()}")

        if diary_enabled and diary.strip():
            sections.append(f"## コアメモリ（日記要約）\n\n{diary.strip()}")

        if ep_enabled and episodic.strip():
            sections.append(f"## エピソード記憶\n\n{episodic.strip()}")

        if logs_enabled and logs.strip():
            log_content = logs.strip()
            if wrap_logs_with_tags:
                log_content = f"<nexus_ark_past_logs>\n{log_content}\n</nexus_ark_past_logs>"
            sections.append(f"## 直近の会話ログ\n\n{log_content}")

        if not sections:
            gr.Warning("エクスポートするセクションがありません。")
            return gr.update(visible=False)

        combined = "\n\n---\n\n".join(sections)

        # ファイル保存
        room_config = room_manager.get_room_config(room_name) or {}
        display_name = room_config.get("agent_display_name") or room_name

        export_folder = _get_outing_export_folder(room_name)
        file_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        export_filename = f"{display_name}_outing_{file_timestamp}.md"
        export_path = os.path.join(export_folder, export_filename)

        with open(export_path, "w", encoding="utf-8") as f:
            f.write(combined)

        gr.Info(f"エクスポート完了！ ({len(combined):,} 文字)")
        return gr.update(value=export_path, visible=True)

    except Exception as e:
        gr.Error(f"エクスポートエラー: {e}")
        traceback.print_exc()
        return gr.update(visible=False)

def handle_outing_update_total_chars(
    sys_text: str, sys_enabled: bool,
    perm_text: str, perm_enabled: bool,
    diary_text: str, diary_enabled: bool,
    ep_text: str, ep_enabled: bool,
    logs_text: str, logs_enabled: bool
):
    """
    有効なセクションの合計文字数を計算して返す
    """
    total = 0
    if sys_enabled:
        total += len(sys_text) if sys_text else 0
    if perm_enabled:
        total += len(perm_text) if perm_text else 0
    if diary_enabled:
        total += len(diary_text) if diary_text else 0
    if ep_enabled:
        total += len(ep_text) if ep_text else 0
    if logs_enabled:
        total += len(logs_text) if logs_text else 0

    return f"📝 合計文字数: **{total:,}** 文字"


def handle_outing_reload_episodic(room_name: str, episode_days: int):
    """
    スライダー変更時にエピソード記憶を再読み込み
    """
    if not room_name:
        return "", "文字数: 0"

    episodic = ""
    if episode_days > 0:
        episodic = _get_episodic_memory_entries(room_name, episode_days)

    char_count = len(episodic)
    return episodic, f"文字数: **{char_count:,}**"


def handle_outing_reload_logs(
    room_name: str,
    log_mode: str,
    log_count: int,
    auto_summary: bool,
    summary_threshold: int,
    include_timestamp=True,
    include_model=True
):
    """
    構成変更時に会話ログを再読み込み
    """
    if not room_name:
        return "", "文字数: 0"

    log_path, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    logs = ""
    if log_path and os.path.exists(log_path):
        if log_mode == "本日分（高度）":
            logs = _get_today_log_entries_with_summary(
                room_name, log_path, auto_summary, summary_threshold, include_timestamp, include_model
            )
        else:
            log_entries = _get_recent_log_entries(log_path, log_count, include_timestamp, include_model)
            logs = "\n\n".join([f"[{header}]\n{content}" for header, content in log_entries])

    char_count = len(logs)
    return logs, f"文字数: **{char_count:,}**"


def handle_outing_reload_system_prompt(room_name: str):
    """
    システムプロンプトを再読み込み
    """
    if not room_name:
        return "", "文字数: 0"

    _, system_prompt_path, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    text = ""
    if system_prompt_path and os.path.exists(system_prompt_path):
        with open(system_prompt_path, "r", encoding="utf-8") as f:
            text = f.read().strip()

    char_count = len(text)
    return text, f"文字数: **{char_count:,}**"


def handle_outing_reload_core_memory(room_name: str):
    """
    コアメモリ（永続・日記の両方）を再読み込み
    """
    if not room_name:
        return "", "文字数: 0", "", "文字数: 0"

    core_memory_path = os.path.join(constants.ROOMS_DIR, room_name, "core_memory.txt")
    core_memory_text = ""
    if os.path.exists(core_memory_path):
        with open(core_memory_path, "r", encoding="utf-8") as f:
            core_memory_text = f.read()

    permanent, diary = _split_core_memory(core_memory_text)
    perm_chars = len(permanent)
    diary_chars = len(diary)

    return permanent, f"文字数: **{perm_chars:,}**", diary, f"文字数: **{diary_chars:,}**"


def handle_outing_import_preview(file_obj, source_name, user_header, agent_header, include_marker):
    """
    帰宅インポート ステップ1: ファイルを読み込み、パースして内部保存形式(## ROLE)でプレビューを生成する
    """
    if file_obj is None:
        return gr.update(), gr.update(visible=False), "ステータス: ⚠️ ファイルが選択されていません"

    if not source_name:
        source_name = "外出先"

    try:
        # UTF-8で読み込みを試みる
        try:
            with open(file_obj.name, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(file_obj.name, "r", encoding="cp932") as f:
                content = f.read()

        # 過去ログタグを除去（重複防止ロジック）
        content = _strip_past_logs(content)

        # 正規表現で分割
        user_h = re.escape(user_header)
        agent_h = re.escape(agent_header)
        pattern = re.compile(f"(^{user_h}|^{agent_h})", re.MULTILINE)

        parts = pattern.split(content)
        if len(parts) <= 1:
            return gr.update(), gr.update(visible=False), "ステータス: ⚠️ ヘッダーが見つかりませんでした。設定を確認してください。"

        preview_entries = []
        for i in range(1, len(parts), 2):
            header = parts[i]
            text = parts[i+1].strip()
            if not text: continue

            # 保存用形式に変換してプレビュー表示
            if header == user_header:
                internal_header = "## USER:user"
            else:
                internal_header = f"## AGENT:外出先({source_name})"

            preview_entries.append(f"{internal_header}\n{text}")

        if not preview_entries:
            return gr.update(), gr.update(visible=False), "ステータス: ⚠️ メッセージが見つかりませんでした"

        preview_text = "\n\n".join(preview_entries)

        # マーカーありの場合はプレビューの前後に追加
        if include_marker:
            marker_start = f"## SYSTEM:外出\n\n--- {source_name} での会話開始 ---"
            marker_end = f"## SYSTEM:外出\n\n--- {source_name} での会話終了 ---"
            preview_text = f"{marker_start}\n\n{preview_text}\n\n{marker_end}"

        return gr.update(value=preview_text, visible=True), gr.update(visible=True), "ステータス: 📝 内容を確認・編集してください"

    except Exception as e:
        print(f"Import Preview Error: {e}")
        return gr.update(), gr.update(visible=False), f"ステータス: ❌ エラー: {str(e)}"


def handle_outing_import_finalize(
    preview_text, room_name, source_name, include_marker,
    api_history_limit_state, add_timestamp, display_thoughts,
    screenshot_mode, redaction_rules
):
    """
    帰宅インポート ステップ2: プレビュー内容を最終調整してルームログにマージする
    """
    if not preview_text or not preview_text.strip():
        return gr.update(), gr.update(), "ステータス: ⚠️ インポートする内容がありません", gr.update(), gr.update(), gr.update()

    if not room_name:
        return gr.update(), gr.update(), "ステータス: ⚠️ ルームが選択されていません", gr.update(), gr.update(), gr.update()

    try:
        import re
        final_text = preview_text.strip()

        # 正規表現で「## AGENT:外出先(...)」形式を現在のルーム名に一括置換
        # これにより、ユーザーがプレビュー上で編集した内容を尊重しつつ、
        # エージェント名だけを正しくマッピングする。
        final_text = re.sub(r'## AGENT:外出先\([^)]*\)', f"## AGENT:{room_name}", final_text)

        # ※ include_marker はプレビュー生成時に処理済みという方針のため、ここでは処理しない
        # (もしプレビュー時に追加していない場合は、ここで行う)

        log_path, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        room_manager.create_backup(room_name, 'log')

        with open(log_path, "a", encoding="utf-8") as f:
            if os.path.exists(log_path) and os.path.getsize(log_path) > 0:
                f.write("\n\n")
            import_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"<!-- Return Home Import: {import_timestamp} from {source_name} -->\n\n")
            f.write(final_text)
            f.write("\n\n")

        gr.Info(f"ログをインポートしました。おかえりなさい！")

        chatbot_display, current_log_map_state = reload_chat_log(
            room_name, api_history_limit_state, add_timestamp,
            display_thoughts, screenshot_mode, redaction_rules
        )

        return (
            chatbot_display, current_log_map_state,
            "ステータス: ✅ インポート完了", None,
            gr.update(visible=False), gr.update(visible=False)
        )

    except Exception as e:
        print(f"Finalize Import Error: {e}")
        return gr.update(), gr.update(), f"ステータス: ❌ エラー: {str(e)}", gr.update(), gr.update(), gr.update()


def handle_gemini_import_preview(url: str, room_name: str, include_marker: bool):
    """
    帰宅インポート（Gemini）ステップ1: URLから内容を読み込み、プレビューを生成する
    """
    if not url or not url.strip():
        return gr.update(), gr.update(visible=False), "ステータス: ⚠️ URLを入力してください"

    if not room_name:
        return gr.update(), gr.update(visible=False), "ステータス: ⚠️ ルームが選択されていません"

    try:
        from tools import gemini_importer
        gr.Info("Geminiの共有URLから内容を取得しています...")
        success, msg, messages = gemini_importer.import_gemini_log_from_url(url.strip(), room_name)

        if not success:
            return gr.update(), gr.update(visible=False), f"ステータス: ❌ {msg}"

        preview_entries = []
        for m in messages:
            role = m.get("role", "user")
            content = str(m.get("content", "")).strip()

            # 各メッセージ内容から過去ログタグ（と見出し）を除去
            content = _strip_past_logs(content)
            if not content: continue

            # プレビューでは「外出先」としてのヘッダーを付けておく
            if role == "user":
                header = "## USER:user"
            else:
                header = f"## AGENT:外出先(Gemini)"

            preview_entries.append(f"{header}\n{content}")

        preview_text = "\n\n".join(preview_entries)

        # マーカーありの場合はプレビューの前後に追加
        if include_marker:
            marker_start = "## SYSTEM:外出\n\n--- Gemini 共有URLからの取り込み開始 ---"
            marker_end = "## SYSTEM:外出\n\n--- Gemini 共有URLからの取り込み終了 ---"
            preview_text = f"{marker_start}\n\n{preview_text}\n\n{marker_end}"

        return (
            gr.update(value=preview_text, visible=True),
            gr.update(visible=True),
            f"ステータス: ✅ {len(messages)}件読み込み完了。確認して統合を実行してください。"
        )

    except Exception as e:
        print(f"Gemini Preview Error: {e}")
        traceback.print_exc()
        return gr.update(), gr.update(visible=False), f"ステータス: ❌ エラー: {e}"

def handle_save_twitter_settings(room_name, enabled, auth_mode, api_key, api_secret, access_token, access_token_secret, posting_summary, posting_guidelines):
    """Twitter連携設定を保存する"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return

    settings = {
        "twitter_settings": {
            "enabled": bool(enabled),
            "use_api": (auth_mode == "api"),
            "auth_mode": auth_mode,
            "posting_summary": posting_summary,
            "posting_guidelines": posting_guidelines,
            "api_config": {
                "api_key": api_key,
                "api_secret": api_secret,
                "access_token": access_token,
                "access_token_secret": access_token_secret
            }
        }
    }

    import room_manager
    result = room_manager.update_room_config(room_name, settings)
    if result == True:
        gr.Info("Twitter連携設定を保存しました。")
    elif result == "no_change":
        gr.Info("設定に変更はありません。")
    else:
        gr.Error("設定の保存中にエラーが発生しました。")

def handle_twitter_auth_mode_change(mode):
    """認証方式の切り替えに合わせてUIの表示を切り替える"""
    return gr.update(visible=(mode == "browser")), gr.update(visible=(mode == "api"))

def handle_test_twitter_api(api_key, api_secret, access_token, access_token_secret):
    """Twitter APIの接続テストを実行する"""
    if not all([api_key, api_secret, access_token, access_token_secret]):
        return "⚠️ **エラー**: 全てのAPIキーを入力してください。"

    from twitter_api import TwitterAPI
    api = TwitterAPI(api_key, api_secret, access_token, access_token_secret)

    # tweepy がない場合はエラーメッセージを返す
    import logging
    logger = logging.getLogger("twitter_api")
    if not hasattr(api, "client") or api.client is None:
        return "❌ **失敗**: クライアントの初期化に失敗しました。`tweepy` がインストールされているか確認してください。"

    success = api.test_connection()
    if success:
        return "✅ **成功**: API接続テストに合格しました！"
    else:
        return "❌ **失敗**: 認証エラーが発生しました。キーが正しいか、および App Permissions が 'Read and Write' になっているか確認してください。"

def handle_load_twitter_settings(room_name):
    """ルーム設定からTwitterの認証情報を読み込み、UIに反映させる"""
    if not room_name:
        return [gr.update()] * 9

    import room_manager
    room_config = room_manager.get_room_config(room_name) or {}
    twitter_settings = room_config.get("twitter_settings", {})

    enabled = twitter_settings.get("enabled", False)
    auth_mode = twitter_settings.get("auth_mode") or "api"
    posting_summary = twitter_settings.get("posting_summary", "")
    posting_guidelines = twitter_settings.get("posting_guidelines", "")
    api_config = twitter_settings.get("api_config", {})

    return [
        gr.update(value=enabled),
        gr.update(value=auth_mode),
        gr.update(value=posting_summary),
        gr.update(value=posting_guidelines),
        gr.update(value=api_config.get("api_key", "")),
        gr.update(value=api_config.get("api_secret", "")),
        gr.update(value=api_config.get("access_token", "")),
        gr.update(value=api_config.get("access_token_secret", "")),
        gr.update(visible=(auth_mode == "api"))  # APIグループの可視性
    ]

# 廃止: 下記の handle_refresh_twitter_tab (15637行目付近) が使用されています。















def handle_generate_api_gateway_token():
    """REST API Gateway用のBearer Token候補を生成する。"""
    token = secrets.token_urlsafe(32)
    gr.Info("API Tokenを生成しました。保存すると有効になります。")
    return (
        gr.update(value=token),
        gr.update(value=token),
        gr.update(value="API状態: 🔑 新しいTokenを生成しました。保存すると有効になります。"),
    )


def handle_show_saved_api_gateway_token():
    """保存済みREST API Gateway Tokenをコピー用欄へ表示する。"""
    settings = config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}) or {}
    token = str(settings.get("auth_token") or "").strip()
    if not token:
        gr.Warning("保存済みAPI Tokenがありません。Token生成後に保存してください。")
        return gr.update(value="")
    gr.Info("保存済みAPI Tokenをコピー用欄に表示しました。")
    return gr.update(value=token)


















def build_api_gateway_lite_connection_help() -> str:
    """Nexus Ark Liteの接続先とTailscale Serveコマンドを表示するMarkdownを生成する。"""
    settings = config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}) or {}
    port = int(settings.get("port", 8000) or 8000)
    api_enabled = bool(settings.get("enabled"))
    dns_name = _get_tailscale_dns_name()
    tailscale_ip = _get_tailscale_ipv4()
    serve_status = _get_tailscale_serve_status()
    serve_status_json = _get_tailscale_serve_status_json()
    local_url = f"http://127.0.0.1:{port}/lite"
    lan_url = f"http://<PCのIPアドレス>:{port}/lite"
    tailnet_http_url = f"http://{tailscale_ip}:{port}/lite" if tailscale_ip else f"http://<Tailscale IP>:{port}/lite"
    https_url = f"https://{dns_name}/lite" if dns_name else "https://<PCのTailscale DNS名>.ts.net/lite"
    serve_command = f"tailscale serve --bg --https=443 http://127.0.0.1:{port}"
    serve_configured = _tailscale_serve_points_to_port(serve_status, serve_status_json, port)
    serve_diagnostic = _summarize_tailscale_serve_json(serve_status_json, port)

    api_state = "有効" if api_enabled else "無効"
    dns_line = f"- Tailscale DNS名: `{dns_name}`" if dns_name else "- Tailscale DNS名: 未検出（MagicDNS/HTTPS有効化後に再確認）"
    ip_line = f"- Tailscale IP: `{tailscale_ip}`" if tailscale_ip else "- Tailscale IP: 未検出"
    if not shutil.which("tailscale"):
        serve_line = "- Tailscale Serve: 未確認（Tailscale CLIが見つかりません）"
        next_action = "次にやること: Tailscaleをインストールしてログイン後、接続情報を更新してください。"
    elif serve_configured:
        serve_line = f"- Tailscale Serve: **設定済み**（API Gateway port `{port}` へ転送）"
        next_action = f"次にやること: スマホで `{https_url}` を開いてください。録音も使えます。"
    elif serve_status or serve_status_json:
        serve_line = "- Tailscale Serve: 未設定または別ポート向けに設定済み"
        next_action = "次にやること: 「Tailscale HTTPS設定を実行」を押すか、下のコマンドをPC側で実行してください。"
    else:
        serve_line = "- Tailscale Serve: 未確認（Tailscale未ログイン、権限待ち、またはHTTPS/MagicDNS未設定の可能性）"
        next_action = "次にやること: Tailscaleのログイン状態とMagicDNS/HTTPS設定を確認し、必要なら「Tailscale HTTPS設定を実行」を押してください。"

    serve_diagnostic_block = f"{serve_diagnostic}\n" if serve_diagnostic else ""

    return (
        "#### Nexus Ark Lite 接続情報\n"
        f"- API Gateway: **{api_state}** / Port `{port}`\n"
        f"- PC内確認: `{local_url}`\n"
        f"- 同一Wi-Fi: `{lan_url}`\n"
        f"- Tailscale HTTP（テキスト・画像用）: `{tailnet_http_url}`\n"
        f"- Tailscale HTTPS（音声入力用）: `{https_url}`\n"
        f"{dns_line}\n"
        f"{ip_line}\n"
        f"{serve_line}\n\n"
        f"{serve_diagnostic_block}"
        f"**{next_action}**\n\n"
        "スマホの音声入力はブラウザ制約によりHTTPSが必要です。Tailscale接続では、PC側で一度だけ以下を実行してHTTPS URLを使います。\n\n"
        f"```bash\n{serve_command}\n```\n\n"
        "設定済みか確認する場合:\n\n"
        "```bash\n"
        "tailscale serve status\n"
        "tailscale serve status --json\n"
        "```\n"
    )


def build_api_gateway_security_diagnostics() -> str:
    """API Gateway / Lite公開前に見る安全診断をMarkdownで返す。"""
    settings = config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}) or {}
    enabled = bool(settings.get("enabled", False))
    host = str(settings.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    port = int(settings.get("port", 8000) or 8000)
    require_auth = bool(settings.get("require_auth", True))
    token = str(settings.get("auth_token") or "").strip()
    rate_limit_enabled = bool(settings.get("rate_limit_enabled", True))
    audit_enabled = bool(settings.get("audit_enabled", True))
    public_host = host in {"0.0.0.0", "::"} or host not in {"127.0.0.1", "localhost", "::1"}
    dns_name = _get_tailscale_dns_name()
    https_url = f"https://{dns_name}/lite" if dns_name else "https://<PCのTailscale DNS名>.ts.net/lite"

    checks: list[str] = []
    checks.append(f"- API Gateway: {'🟢 有効' if enabled else '⚪ 無効'} / `{host}:{port}`")
    checks.append(f"- Token認証: {'🟢 有効' if require_auth else '🔴 無効'}")
    checks.append(f"- Token保存: {'🟢 済み' if token else '🔴 未設定'}")
    checks.append(f"- レート制限: {'🟢 有効' if rate_limit_enabled else '🟡 無効'}")
    checks.append(f"- 監査ログ: {'🟢 有効' if audit_enabled else '🟡 無効'}")
    checks.append(f"- Tailscale HTTPS候補: `{https_url}`")

    warnings: list[str] = []
    if enabled and public_host and not require_auth:
        warnings.append("🔴 公開HostでToken認証が無効です。API Gatewayは安全のため認証付きAPIを拒否します。")
    if enabled and require_auth and not token:
        warnings.append("🔴 Token認証が有効ですがTokenが未設定です。Token生成後に保存してください。")
    if enabled and public_host and require_auth and token:
        warnings.append("🟢 同一Wi-Fi/Tailscale向けの基本設定は揃っています。インターネット直公開は避け、中継またはTailscaleを使ってください。")
    if host == "127.0.0.1":
        warnings.append("🟡 Hostが `127.0.0.1` のため、同一Wi-Fiのスマホからは直接接続できません。Tailscale Serve経由なら接続できます。")
    if public_host:
        warnings.append("ℹ️ WSL上で動かしている場合、同一Wi-Fi直結にはWindows側のportproxy/ファイアウォール設定が必要なことがあります。")
    if not rate_limit_enabled:
        warnings.append("🟡 レート制限が無効です。公開中継や外部ツール利用時は有効化を推奨します。")
    if not audit_enabled:
        warnings.append("🟡 監査ログが無効です。認証失敗や管理操作の追跡が必要な場合は有効化を推奨します。")

    warning_block = "\n".join(f"- {item}" for item in warnings) if warnings else "- 🟢 重大な警告はありません。"
    return (
        "#### API Gateway / Lite 安全診断\n"
        f"{chr(10).join(checks)}\n\n"
        "#### 判定\n"
        f"{warning_block}\n\n"
        "#### 公開方針\n"
        "- 安全な利用のため、同一LAN（自宅Wi-Fi内）またはTailscale（VPN/HTTPS）での利用を推奨します。\n"
        "- インターネットへ直接公開する場合は、Token中継サーバーやCloudflare Tunnel等の中継サービスを挟んで、公開するAPIを限定してください。\n"
    )


def build_api_gateway_personal_use_guide() -> str:
    """個人拡張用途向けに、API Gateway / Liteの使い方をユーザーフレンドリーに案内する。"""
    return (
        "#### 📱 スマホからペルソナに話しかける\n"
        "**Nexus Ark Lite** を使うと、外出先のスマホからペルソナとチャットしたり、"
        "音声で話しかけたり、Twitter下書きを承認したりできます。\n\n"
        "1. 下の「API設定」でAPI Gatewayを有効にして、接続用Tokenを生成・保存します。\n"
        "2. 「Nexus Ark Lite 接続情報」に表示されるURLをスマホで開きます。\n"
        "3. Tokenを入力して接続すると、ペルソナとの会話が始められます。\n\n"
        "#### 🔌 身の回りの環境をペルソナに伝える\n"
        "**API Gateway** を使うと、Nexus Ark本体を改造せずに、"
        "身の回りのツールやデバイスからペルソナへ状況を伝えられます。\n\n"
        "**こんなことができます:**\n"
        "- 🏠 **スマートホーム連携**: SwitchBotやHome Assistantから、照明のON/OFF、ドアの開閉、室温変化などをペルソナに教える\n"
        "- 🎨 **画像生成連携**: ローカル画像生成アプリなどで生成した画像情報をペルソナに伝える\n"
        "- 💬 **自作アプリ・通知連携**: 自作のWebアプリや通知システム、外部サービスの情報をペルソナに渡す\n"
        "- 🖥️ **PC状態通知**: 定期スクリプトから、バックアップ完了やエラー発生をペルソナに知らせる\n\n"
        "基本的な流れは、外部ツールからペルソナへ「こんな出来事がありました」と伝えるだけです。"
        "会話として返答が欲しい場合はチャット送信を、状態を確認したい場合はステータス取得を使います。\n\n"
        "具体的な接続方法やコードサンプルは、下の「外部連携リファレンス」を開いてください。\n\n"
        "#### ⚠️ 安全に使うために\n"
        "- 自分のPC・自宅Wi-Fi・Tailscaleなど、**自分が管理するネットワーク**で使ってください。\n"
        "- 「API設定」の **Token認証は必ず有効** にしてください。\n"
        "- 詳しい安全確認は下の「安全診断」に表示されます。\n"
    )


def handle_refresh_api_gateway_lite_connection_help():
    """REST API / Lite接続情報を再取得する。"""
    return gr.update(value=build_api_gateway_lite_connection_help())




def _load_external_integration_guide() -> str:
    """assets/guides/api_gateway_external_integration.md を読み込む。失敗時は空文字。"""
    guide_path = os.path.join(_UI_HANDLERS_PROJECT_ROOT, "assets", "guides", "api_gateway_external_integration.md")
    try:
        with open(guide_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def build_api_gateway_external_docs(room_name: str = "") -> str:
    """外部連携ユーザー向けのAPI概要とガイドファイル内容を表示するMarkdownを生成する。"""
    settings = config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}) or {}
    port = int(settings.get("port", 8000) or 8000)
    host = (settings.get("host") or "127.0.0.1").strip()
    local_base = f"http://127.0.0.1:{port}"
    lan_base = f"http://<PCのIPアドレス>:{port}"
    configured_base = f"http://{host}:{port}" if host != "0.0.0.0" else lan_base
    auth_note = "Token必須" if settings.get("require_auth", True) else "Token認証なし（ローカル検証向け）"

    # --- 動的情報ヘッダー ---
    header = (
        "#### 現在の接続先\n"
        f"- 設定: `{host}:{port}` / {auth_note}\n"
        f"- PC内URL: `{local_base}`\n"
        f"- 同一Wi-Fi/Tailscale: `{configured_base}`\n"
        f"- OpenAPI/Swagger: `{local_base}/docs`\n\n"
        "---\n\n"
    )

    # --- 独立ガイドファイルの読み込み ---
    guide_content = _load_external_integration_guide()
    if guide_content:
        return header + guide_content

    # --- フォールバック: ガイドファイルが読めない場合の最小リファレンス ---
    return (
        header
        + "#### よく使うエンドポイント\n"
        "| 用途 | メソッド | パス |\n"
        "| :--- | :--- | :--- |\n"
        "| ルーム一覧 | GET | `/api/v1/rooms` |\n"
        "| 状態取得 | GET | `/api/v1/rooms/{room_id}/status` |\n"
        "| 履歴取得 | GET | `/api/v1/rooms/{room_id}/chat/history?limit=12` |\n"
        "| チャット送信 | POST | `/api/v1/rooms/{room_id}/chat` |\n"
        "| 外部イベント注入 | POST | `/api/v1/rooms/{room_id}/events` |\n"
        "| 画像アップロード | POST | `/api/v1/rooms/{room_id}/uploads` |\n"
        "| 記憶検索 | GET | `/api/v1/rooms/{room_id}/memory/search?query=...` |\n\n"
        "詳しいレシピは `assets/guides/api_gateway_external_integration.md` を参照してください。\n"
    )


def build_external_event_template(event_type: str = "switchbot_triggered") -> str:
    """外部イベントテスター用のJSONテンプレートを返す。"""
    templates = {
        "switchbot_triggered": {
            "summary": "書斎の照明が消えました",
            "device": "study_light",
            "state": "off",
        },
        "stackchan_observed": {
            "summary": "ｽﾀｯｸﾁｬﾝがユーザーの呼びかけを検知しました",
            "device": "stackchan",
            "signal": "voice_detected",
        },
        "stable_diffusion_result": {
            "summary": "Stable Diffusionで背景候補を生成しました",
            "prompt": "cozy study room at night",
            "image_path": "C:/path/to/generated.png",
        },
        "sns_post_received": {
            "summary": "疑似SNSに新しい投稿が届きました",
            "author": "friend_ai",
            "text": "今日は星がきれい。",
            "url": "https://example.local/posts/123",
        },
        "custom": {
            "summary": "外部ツールからの任意イベントです",
            "details": {},
        },
    }
    payload = templates.get(event_type, templates["custom"])
    return json.dumps(payload, ensure_ascii=False, indent=2)


def handle_external_event_type_change(event_type: str):
    """イベント種別に合わせてテスター用JSONを差し替える。"""
    return gr.update(value=build_external_event_template(event_type))


def handle_refresh_external_api_gateway_panel(room_name: str):
    """外部接続タブのAPI説明を再生成する。"""
    return (
        gr.update(value=build_api_gateway_security_diagnostics()),
        gr.update(value=build_api_gateway_lite_connection_help()),
        gr.update(value=build_api_gateway_external_docs(room_name)),
    )


def handle_save_external_api_gateway_settings(enabled: bool, host: str, port: int, require_auth: bool, auth_token: str, auto_start_tailscale_serve: bool, room_name: str):
    """外部接続タブ側からREST API Gateway設定を保存し、説明も更新する。"""
    status = handle_save_api_gateway_settings(enabled, host, port, require_auth, auth_token, auto_start_tailscale_serve)
    token = (auth_token or "").strip()
    return (
        status,
        gr.update(value=token),
        gr.update(value=build_api_gateway_security_diagnostics()),
        gr.update(value=build_api_gateway_lite_connection_help()),
        gr.update(value=build_api_gateway_external_docs(room_name)),
    )


def handle_test_external_event(
    room_name: str,
    event_type: str,
    source: str,
    trigger_notification: bool,
    importance: str,
    event_data_json: str,
):
    """UIから汎用外部イベントを記録する。"""
    if not room_name:
        return gr.update(value="❌ ルームが選択されていません。")
    try:
        event_data = json.loads(event_data_json or "{}")
        if not isinstance(event_data, dict):
            return gr.update(value="❌ イベント内容JSONはオブジェクト形式で入力してください。")
    except json.JSONDecodeError as e:
        return gr.update(value=f"❌ JSONの形式が正しくありません: {e}")

    try:
        from api.schemas import EventRequest
        from api.service import record_event

        response = record_event(
            room_name,
            EventRequest(
                event_type=(event_type or "custom").strip() or "custom",
                source=(source or "external_ui").strip() or "external_ui",
                trigger_notification=bool(trigger_notification),
                importance=(importance or "normal").strip() or "normal",
                event_data=event_data,
            ),
        )
        return gr.update(
            value=(
                "✅ 外部イベントを記録しました。\n\n"
                f"- status: `{response.status}`\n"
                f"- should_interact: `{response.should_interact}`\n"
                f"- notification_status: `{response.notification_status or ''}`\n"
                f"- notification: `{response.notification_text or ''}`\n\n"
                "`trigger_notification=true` でも、通知候補になるのは `importance=high/critical` のイベントだけです。\n\n"
                "チャット欄を再読み込みすると `SYSTEM:external_event` として確認できます。"
            )
        )
    except Exception as e:
        logger.error(f"Failed to test external event: {e}")
        return gr.update(value=f"❌ 外部イベントの記録に失敗しました: {e}")


def handle_configure_tailscale_lite_https():
    """Nexus Ark Lite用のTailscale Serve設定を固定コマンドで実行する。"""
    settings = config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}) or {}
    port = int(settings.get("port", 8000) or 8000)
    if not shutil.which("tailscale"):
        return (
            gr.update(value=build_api_gateway_security_diagnostics()),
            gr.update(value=build_api_gateway_lite_connection_help()),
            gr.update(value="Tailscale CLIが見つかりません。PC側でTailscaleをインストールし、`tailscale serve --https=443 http://127.0.0.1:8000` を実行してください。"),
        )

    serve_status = _get_tailscale_serve_status()
    serve_status_json = _get_tailscale_serve_status_json()
    if _tailscale_serve_points_to_port(serve_status, serve_status_json, port):
        dns_name = _get_tailscale_dns_name()
        url = f"https://{dns_name}/lite" if dns_name else "https://<PCのTailscale DNS名>.ts.net/lite"
        return (
            gr.update(value=build_api_gateway_security_diagnostics()),
            gr.update(value=build_api_gateway_lite_connection_help()),
            gr.update(value=f"Tailscale HTTPSは既に設定済みです。スマホで `{url}` を開いてください。"),
        )

    command = ["tailscale", "serve", "--bg", "--https=443", f"http://127.0.0.1:{port}"]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
        output = "\n".join(part.strip() for part in [result.stdout, result.stderr] if part and part.strip())
        if result.returncode == 0:
            dns_name = _get_tailscale_dns_name()
            url = f"https://{dns_name}/lite" if dns_name else "https://<PCのTailscale DNS名>.ts.net/lite"
            return (
                gr.update(value=build_api_gateway_security_diagnostics()),
                gr.update(value=build_api_gateway_lite_connection_help()),
                gr.update(value=f"Tailscale HTTPS設定を実行しました。スマホで `{url}` を開いてください。"),
            )
        message = output or "Tailscale Serve設定に失敗しました。"
        return (
            gr.update(value=build_api_gateway_security_diagnostics()),
            gr.update(value=build_api_gateway_lite_connection_help()),
            gr.update(value=f"Tailscale Serve設定に失敗しました。\n\n```text\n{message}\n```"),
        )
    except subprocess.TimeoutExpired:
        return (
            gr.update(value=build_api_gateway_security_diagnostics()),
            gr.update(value=build_api_gateway_lite_connection_help()),
            gr.update(value="Tailscale Serve設定がタイムアウトしました。Tailscaleの認証/HTTPS有効化画面が待機している可能性があります。"),
        )
    except Exception as e:
        return (
            gr.update(value=build_api_gateway_security_diagnostics()),
            gr.update(value=build_api_gateway_lite_connection_help()),
            gr.update(value=f"Tailscale Serve設定でエラーが発生しました: {e}"),
        )






def handle_save_api_gateway_settings(enabled: bool, host: str, port: int, require_auth: bool, auth_token: str, auto_start_tailscale_serve: bool):
    """REST API Gatewayの設定を保存する。"""
    try:
        host = (host or "").strip() or "0.0.0.0"
        port = int(port or 8000)
        if not (1 <= port <= 65535):
             return gr.update(value="API状態: ❌ ポート番号は1〜65535で指定してください。")

        auth_token = (auth_token or "").strip()
        if require_auth and not auth_token:
            return gr.update(value="API状態: ❌ 認証を有効にする場合はTokenが必要です。")

        config_manager.save_api_gateway_settings(
            enabled=enabled,
            host=host,
            port=port,
            require_auth=require_auth,
            auth_token=auth_token,
            auto_start_tailscale_serve=auto_start_tailscale_serve,
        )

        # api.server から動的起動・停止・再起動用の関数をインポート
        from api.server import start_server as start_api_gateway_server
        from api.server import stop_server as stop_api_gateway_server

        # 既存サーバーを確実に停止
        try:
            stop_api_gateway_server()
        except Exception as stop_err:
            logger.warning(f"Failed to stop API Gateway server: {stop_err}")

        # ポート解放を少し待つ
        import time
        time.sleep(0.5)

        if enabled:
            try:
                # 新しい設定でサーバーを再起動
                start_api_gateway_server(port=port, host=host, daemon=True)
                
                # 必要であればTailscale Serveの自動設定も非同期で呼び出す
                if auto_start_tailscale_serve:
                    import shutil
                    if shutil.which("tailscale"):
                        import threading
                        logger.info("Tailscale HTTPS Serve の自動設定を非同期で開始します...")
                        threading.Thread(
                            target=handle_configure_tailscale_lite_https,
                            daemon=True
                        ).start()
                
                return gr.update(value="API状態: 🟢 設定を保存し、API Gatewayを起動/再起動しました。")
            except Exception as start_err:
                logger.error(f"Failed to start API Gateway server: {start_err}")
                return gr.update(value=f"API状態: ⚠️ 設定を保存しましたが、起動に失敗しました ({start_err})。")
        else:
            return gr.update(value="API状態: ⚪ 無効として保存し、API Gatewayを停止しました。")
    except Exception as e:
        logger.error(f"Failed to save API Gateway settings: {e}")
        return gr.update(value=f"API状態: ❌ エラーが発生しました ({e})")


# ===== 🧠 内的状態（Internal State）用ハンドラ =====



def handle_clear_open_questions(room_name: str):
    """
    未解決の問いをすべてクリアする。

    Returns:
        (open_questions_df, status_text)
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return [], "エラー: ルーム未選択", []

    try:
        from motivation_manager import MotivationManager

        mm = MotivationManager(room_name)

        # mm._state を直接クリア
        if "drives" in mm._state and "curiosity" in mm._state["drives"]:
            mm._state["drives"]["curiosity"]["open_questions"] = []
            mm._state["drives"]["curiosity"]["level"] = 0.0

        mm._save_state()

        gr.Info("未解決の問いをクリアしました。")
        return [], "🗑️ クリア完了", []

    except Exception as e:
        print(f"Clear Open Questions Error: {e}")
        traceback.print_exc()
        gr.Error(f"クリアに失敗しました: {e}")
        return gr.update(), f"エラー: {e}", []


def handle_delete_selected_questions(room_name: str, selected_topics: list):
    """
    Stateに保存された話題リストに対応する問いを削除する。

    Args:
        room_name: ルーム名
        selected_topics: 選択された話題のリスト

    Returns:
        (open_questions_df, status_text, reset_state)
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), "エラー: ルーム未選択", []

    if not selected_topics or len(selected_topics) == 0:
        gr.Warning("削除する問いを選択してください。")
        return gr.update(), "⚠️ 選択されていません", []

    try:
        from motivation_manager import MotivationManager

        mm = MotivationManager(room_name)

        questions = mm._state.get("drives", {}).get("curiosity", {}).get("open_questions", [])

        # 選択された話題を削除
        selected_set = set(selected_topics)
        remaining = [q for q in questions if q.get("topic") not in selected_set]
        deleted_count = len(questions) - len(remaining)

        if "drives" in mm._state and "curiosity" in mm._state["drives"]:
            mm._state["drives"]["curiosity"]["open_questions"] = remaining

        mm._save_state()

        gr.Info(f"{deleted_count}件の問いを削除しました。")

        # 更新後のDataFrameを返す
        questions_data = _render_open_questions_dataframe(remaining)

        return questions_data, f"🗑️ {deleted_count}件を削除しました", []

    except Exception as e:
        print(f"Delete Selected Questions Error: {e}")
        traceback.print_exc()
        gr.Error(f"削除に失敗しました: {e}")
        return gr.update(), f"エラー: {e}", []


def handle_resolve_selected_questions(room_name: str, selected_topics: list):
    """
    Stateに保存された話題リストに対応する問いを解決済みにする。

    Args:
        room_name: ルーム名
        selected_topics: 選択された話題のリスト

    Returns:
        (open_questions_df, status_text, reset_state)
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), "エラー: ルーム未選択", []

    if not selected_topics or len(selected_topics) == 0:
        gr.Warning("解決済みにする問いを選択してください。")
        return gr.update(), "⚠️ 選択されていません", []

    try:
        from motivation_manager import MotivationManager

        mm = MotivationManager(room_name)

        # 各問いを解決済みにマーク
        resolved_count = 0
        for topic in selected_topics:
            # 修正: mark_question_asked ではなく mark_question_resolved を使用
            if mm.mark_question_resolved(topic):
                resolved_count += 1

        gr.Info(f"{resolved_count}件の問いを解決済み（回答済み）にしました。")

        # 更新後のDataFrameを返す
        questions = mm._state.get("drives", {}).get("curiosity", {}).get("open_questions", [])
        questions_data = _render_open_questions_dataframe(questions)

        return questions_data, f"✅ {resolved_count}件を解決済みにしました", []

    except Exception as e:
        print(f"Resolve Selected Questions Error: {e}")
        traceback.print_exc()
        gr.Error(f"解決済みマークに失敗しました: {e}")
        return gr.update(), f"エラー: {e}", []




def handle_question_row_selection(df, evt: gr.SelectData):
    """
    DataFrameの行選択イベント。選択された行の話題をStateに保存。

    Args:
        df: DataFrameのデータ（Pandas DataFrame）
        evt: Gradio SelectData（選択されたセルの情報）

    Returns:
        (selected_topics_list, status_text)
    """
    try:
        if evt is None or evt.index is None:
            return [], "---"

        # evt.indexは[行, 列]のリスト
        row_idx = evt.index[0] if isinstance(evt.index, list) else evt.index

        # DataFrameから該当行の話題（最初の列）を取得
        import pandas as pd
        if isinstance(df, pd.DataFrame):
            if row_idx < len(df):
                topic = df.iloc[row_idx, 0]  # 最初の列が「話題」
                return [topic], f"選択中: {topic}"
        elif isinstance(df, list) and len(df) > row_idx:
            topic = df[row_idx][0]  # リスト形式の場合
            return [topic], f"選択中: {topic}"

        return [], "---"
    except Exception as e:
        print(f"Question Row Selection Error: {e}")
        traceback.print_exc()
        return [], "---"




def handle_reset_internal_state(room_name: str):
    """
    内部状態を完全にリセットする。
    動機レベル、未解決の問い、最終発火時刻がすべてクリアされる。

    Returns:
        status_text
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return "エラー: ルーム未選択"

    try:
        from motivation_manager import MotivationManager

        mm = MotivationManager(room_name)
        mm.clear_internal_state()

        gr.Info(f"「{room_name}」の内部状態をリセットしました。")
        return f"✅ リセット完了 ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"

    except Exception as e:
        print(f"Reset Internal State Error: {e}")
        traceback.print_exc()
        gr.Error(f"リセットに失敗しました: {e}")
        return f"❌ エラー: {e}"


# --- ウォッチリスト管理ハンドラ ---

# --- リサーチ・テーマ（継続調査）購読ハンドラ（Phase 1b） ---





















def handle_watchlist_refresh(room_name: str):
    """ウォッチリストのDataFrameを更新する"""
    if not room_name:
        return [], "ルームが選択されていません"

    try:
        from watchlist_manager import WatchlistManager
        manager = WatchlistManager(room_name)
        entries = manager.get_entries_for_ui()

        if not entries:
            return [], "ウォッチリストは空です"

        # DataFrameデータを生成
        data = []
        for entry in entries:
            data.append([
                entry.get("id", "")[:8],  # IDは短く表示
                entry.get("name", ""),
                entry.get("url", ""),
                entry.get("interval_display", "手動"),
                entry.get("last_checked_display", "未チェック"),
                entry.get("enabled", True),
                entry.get("group_name", "")  # v2: グループ名
            ])

        return data, f"✅ {len(data)}件のエントリを読み込みました"

    except Exception as e:
        traceback.print_exc()
        return [], f"❌ エラー: {e}"


def handle_watchlist_add(room_name: str, url: str, name: str, interval: str, daily_time: str = "09:00"):
    """ウォッチリストにエントリを追加する"""
    if not room_name:
        gr.Warning("ルームが選択されていません")
        return gr.update(), "ルームが選択されていません"

    if not url or not url.strip():
        gr.Warning("URLを入力してください")
        return gr.update(), "URLを入力してください"

    url = url.strip()
    name = name.strip() if name else None

    # 「毎日指定時刻」の場合は時刻情報を含める
    if interval == "daily" and daily_time:
        interval = f"daily_{daily_time}"

    try:
        from watchlist_manager import WatchlistManager
        manager = WatchlistManager(room_name)

        # 既存チェック
        existing = manager.get_entry_by_url(url)
        if existing:
            # 更新処理
            manager.update_entry(
                existing["id"],
                name=name if name else existing["name"],
                check_interval=interval
            )
            gr.Info(f"ウォッチリストを更新しました: {name if name else existing['name']}")
            return handle_watchlist_refresh(room_name)[0], f"✅ 更新しました: {name if name else existing['name']}"

        entry = manager.add_entry(url=url, name=name, check_interval=interval)
        gr.Info(f"ウォッチリストに追加しました: {entry['name']}")

        return handle_watchlist_refresh(room_name)[0], f"✅ 追加しました: {entry['name']}"

    except Exception as e:
        traceback.print_exc()
        gr.Error(f"追加・更新に失敗しました: {e}")
        return gr.update(), f"❌ エラー: {e}"


def handle_watchlist_delete(room_name: str, selected_data: list):
    """ウォッチリストからエントリを削除する"""
    if not room_name:
        gr.Warning("ルームが選択されていません")
        return gr.update(), "ルームが選択されていません"

    if not selected_data or len(selected_data) == 0:
        gr.Warning("削除するエントリを選択してください")
        return gr.update(), "エントリを選択してください"

    try:
        from watchlist_manager import WatchlistManager
        manager = WatchlistManager(room_name)

        # 選択された行のIDを取得（最初の列がID）
        short_id = selected_data[0] if isinstance(selected_data, list) else None
        if not short_id:
            gr.Warning("削除するエントリを選択してください")
            return gr.update(), "エントリを選択してください"

        # 短いIDから完全なIDを検索
        entries = manager.get_entries()
        target_entry = None
        for entry in entries:
            if entry.get("id", "").startswith(short_id):
                target_entry = entry
                break

        if not target_entry:
            gr.Warning("エントリが見つかりません")
            return gr.update(), "エントリが見つかりません"

        success = manager.remove_entry(target_entry["id"])
        if success:
            gr.Info(f"削除しました: {target_entry['name']}")
            return handle_watchlist_refresh(room_name)[0], f"✅ 削除しました: {target_entry['name']}"
        else:
            return gr.update(), "削除に失敗しました"

    except Exception as e:
        traceback.print_exc()
        gr.Error(f"削除に失敗しました: {e}")
        return gr.update(), f"❌ エラー: {e}"


def handle_watchlist_check_all(room_name: str, api_key_name: str):
    """ウォッチリストの全URLをチェックし、変更があればペルソナに分析させる"""
    if not room_name:
        gr.Warning("ルームが選択されていません")
        return gr.update(), "ルームが選択されていません"

    gr.Info("🔄 全件チェックを開始しています...")

    try:
        from watchlist_manager import WatchlistManager
        from tools.watchlist_tools import _fetch_url_content
        from alarm_manager import _summarize_watchlist_content, trigger_research_analysis

        manager = WatchlistManager(room_name)
        entries = manager.get_entries()

        if not entries:
            return gr.update(), "ウォッチリストは空です"

        results = []
        changes_found = []  # 詳細情報を含む辞書のリスト

        for entry in entries:
            if not entry.get("enabled", True):
                continue

            url = entry["url"]
            name = entry["name"]

            # コンテンツ取得
            success, content = _fetch_url_content(url)

            if not success:
                results.append(f"❌ {name}: 取得失敗")
                continue

            # 差分チェック
            has_changes, diff_summary = manager.check_and_update(entry["id"], content)

            if has_changes:
                # 【修正】軽量モデルでコンテンツを要約し、詳細情報を保存
                content_summary = _summarize_watchlist_content(name, url, content, diff_summary)

                changes_found.append({
                    "name": name,
                    "url": url,
                    "diff_summary": diff_summary,
                    "content_summary": content_summary
                })
                results.append(f"🔔 {name}: 更新あり！ ({diff_summary})")
            else:
                results.append(f"✅ {name}: {diff_summary}")

        # DataFrameを更新
        df_data = handle_watchlist_refresh(room_name)[0]

        # 【修正】変更があった場合、ペルソナに分析させる
        if changes_found:
            current_api_key = api_key_name or config_manager.get_latest_api_key_name_from_config()
            if current_api_key:
                gr.Info(f"{len(changes_found)}件の更新を検出。ペルソナに分析を依頼中...")
                trigger_research_analysis(room_name, current_api_key, "watchlist", changes_found)
                status = f"✅ チェック完了: {len(results)}件中 {len(changes_found)}件に更新あり → ペルソナに分析を依頼しました"
            else:
                status = f"チェック完了: {len(results)}件中 {len(changes_found)}件に更新あり（APIキー未設定のため分析スキップ）"
        else:
            status = f"✅ チェック完了: {len(results)}件チェック、更新なし"

        gr.Info(status)
        return df_data, status

    except Exception as e:
        traceback.print_exc()
        gr.Error(f"チェックに失敗しました: {e}")
        return gr.update(), f"❌ エラー: {e}"


# --- ウォッチリスト グループ管理ハンドラ (v2) ---

def handle_group_refresh(room_name: str):
    """グループ一覧のDataFrameを更新する"""
    if not room_name:
        return [], "ルームが選択されていません"

    try:
        from watchlist_manager import WatchlistManager
        manager = WatchlistManager(room_name)
        groups = manager.get_groups_for_ui()

        if not groups:
            return [], "グループはまだ作成されていません"

        # DataFrameデータを生成
        data = []
        for group in groups:
            data.append([
                group.get("id", "")[:8],  # IDは短く表示
                group.get("name", ""),
                group.get("description", "")[:30],  # 説明は短く
                group.get("interval_display", "手動"),
                group.get("entry_count", 0),
                group.get("enabled", True)
            ])

        return data, f"✅ {len(data)}件のグループを読み込みました"

    except Exception as e:
        traceback.print_exc()
        return [], f"❌ エラー: {e}"


def handle_group_add(room_name: str, name: str, description: str, interval: str, daily_time: str = "09:00"):
    """グループを作成する"""
    if not room_name:
        gr.Warning("ルームが選択されていません")
        return gr.update(), "ルームが選択されていません"

    if not name or not name.strip():
        gr.Warning("グループ名を入力してください")
        return gr.update(), "グループ名を入力してください"

    name = name.strip()
    description = description.strip() if description else ""

    # 「毎日指定時刻」の場合は時刻情報を含める
    if interval == "daily" and daily_time:
        interval = f"daily_{daily_time}"

    try:
        from watchlist_manager import WatchlistManager
        manager = WatchlistManager(room_name)

        group = manager.add_group(name=name, description=description, check_interval=interval)
        gr.Info(f"グループを作成しました: {group['name']}")

        return handle_group_refresh(room_name)[0], f"✅ 作成しました: {group['name']}"

    except Exception as e:
        traceback.print_exc()
        gr.Error(f"作成に失敗しました: {e}")
        return gr.update(), f"❌ エラー: {e}"


def handle_group_delete(room_name: str, selected_id: str):
    """グループを削除する（配下エントリーはグループなしに戻る）"""
    if not room_name:
        gr.Warning("ルームが選択されていません")
        return gr.update(), gr.update(), "ルームが選択されていません"

    if not selected_id:
        gr.Warning("削除するグループを選択してください")
        return gr.update(), gr.update(), "グループを選択してください"

    try:
        from watchlist_manager import WatchlistManager
        manager = WatchlistManager(room_name)

        # グループ名を取得（表示用）
        group = manager.get_group_by_id(selected_id)
        if not group:
            gr.Warning("グループが見つかりません")
            return gr.update(), gr.update(), "グループが見つかりません"

        group_name = group["name"]
        success = manager.remove_group(selected_id)

        if success:
            gr.Info(f"グループを削除しました: {group_name}")
            # グループ一覧とエントリー一覧を両方更新
            return (
                handle_group_refresh(room_name)[0],
                handle_watchlist_refresh(room_name)[0],
                f"✅ 削除しました: {group_name}"
            )
        else:
            return gr.update(), gr.update(), "削除に失敗しました"

    except Exception as e:
        traceback.print_exc()
        gr.Error(f"削除に失敗しました: {e}")
        return gr.update(), gr.update(), f"❌ エラー: {e}"


def handle_group_update_interval(room_name: str, selected_id: str, interval: str, daily_time: str = "09:00"):
    """グループの巡回時刻を一括変更する"""
    if not room_name:
        gr.Warning("ルームが選択されていません")
        return gr.update(), gr.update(), "ルームが選択されていません"

    if not selected_id:
        gr.Warning("変更するグループを選択してください")
        return gr.update(), gr.update(), "グループを選択してください"

    # 「毎日指定時刻」の場合は時刻情報を含める
    if interval == "daily" and daily_time:
        interval = f"daily_{daily_time}"

    try:
        from watchlist_manager import WatchlistManager
        manager = WatchlistManager(room_name)

        success, updated_count = manager.update_group_interval(selected_id, interval)

        if success:
            gr.Info(f"グループの時刻を変更しました（{updated_count}件のエントリーを更新）")
            return (
                handle_group_refresh(room_name)[0],
                handle_watchlist_refresh(room_name)[0],
                f"✅ 時刻を変更: {updated_count}件のエントリーを更新"
            )
        else:
            return gr.update(), gr.update(), "更新に失敗しました"

    except Exception as e:
        traceback.print_exc()
        gr.Error(f"更新に失敗しました: {e}")
        return gr.update(), gr.update(), f"❌ エラー: {e}"


def handle_move_entry_to_group(room_name: str, entry_id: str, group_id: str):
    """エントリーをグループに移動する"""
    if not room_name:
        gr.Warning("ルームが選択されていません")
        return gr.update(), "ルームが選択されていません"

    if not entry_id:
        gr.Warning("移動するエントリーを選択してください")
        return gr.update(), "エントリーを選択してください"

    try:
        from watchlist_manager import WatchlistManager
        manager = WatchlistManager(room_name)

        # group_idが空文字の場合はNone（グループなし）に変換
        target_group_id = group_id if group_id else None

        result = manager.move_entry_to_group(entry_id, target_group_id)

        if result:
            if target_group_id:
                group = manager.get_group_by_id(target_group_id)
                group_name = group["name"] if group else "不明"
                gr.Info(f"エントリーをグループ「{group_name}」に移動しました")
                status = f"✅ グループ「{group_name}」に移動しました"
            else:
                gr.Info("エントリーをグループから解除しました")
                status = "✅ グループから解除しました"

            return handle_watchlist_refresh(room_name)[0], status
        else:
            return gr.update(), "移動に失敗しました"

    except Exception as e:
        traceback.print_exc()
        gr.Error(f"移動に失敗しました: {e}")
        return gr.update(), f"❌ エラー: {e}"


def handle_get_group_choices(room_name: str):
    """グループ選択用のドロップダウン選択肢を取得する"""
    if not room_name:
        return gr.update(choices=[("グループなし", "")], value="")

    try:
        from watchlist_manager import WatchlistManager
        manager = WatchlistManager(room_name)
        groups = manager.get_groups()

        choices = [("グループなし", "")]
        for group in groups:
            choices.append((group["name"], group["id"]))

        return gr.update(choices=choices, value="")

    except Exception as e:
        traceback.print_exc()
        return gr.update(choices=[("グループなし", "")], value="")


# --- AI自動リスト作成ハンドラ ---

def handle_ai_generate_candidates(room_name: str, genre: str, api_key_name: str):
    """
    ジャンルを指定してAIがWeb検索で候補サイトを収集する

    Returns:
        (status, checkboxgroup_update, candidates_data, add_row_update, dropdown_update)
    """
    import gradio as gr

    if not room_name:
        return "ルームが選択されていません", gr.update(), [], gr.update(visible=False), gr.update()

    if not genre or not genre.strip():
        gr.Warning("ジャンルを入力してください")
        return "ジャンルを入力してください", gr.update(), [], gr.update(visible=False), gr.update()

    genre = genre.strip()

    # APIキーの取得
    current_api_key = api_key_name or config_manager.get_latest_api_key_name_from_config()
    if not current_api_key:
        gr.Warning("APIキーが設定されていません")
        return "APIキーが設定されていません", gr.update(), [], gr.update(visible=False), gr.update()

    gr.Info(f"🔍 「{genre}」の候補サイトを検索中...")

    try:
        from tools.web_tools import _search_with_tavily, _search_with_ddg, _search_with_google
        import config_manager as cm

        # 検索クエリを構築
        search_query = f"{genre} おすすめサイト ブログ ニュース"

        # Web検索を実行（プロバイダを順番に試す）
        search_results = []

        # まずTavilyを試す
        if cm.TAVILY_API_KEY:
            try:
                results = _search_with_tavily(search_query)
                if results and not results.startswith("["):  # エラーでなければ
                    search_results = _parse_search_results(results)
            except Exception as e:
                print(f"Tavily検索エラー: {e}")

        # Tavilyで見つからなければDuckDuckGo
        if not search_results:
            try:
                results = _search_with_ddg(search_query)
                if results:
                    search_results = _parse_search_results(results)
            except Exception as e:
                print(f"DuckDuckGo検索エラー: {e}")

        # それでもなければGoogle
        if not search_results and current_api_key:
            try:
                from gemini_api import get_model_and_api_key
                model_name, api_key = get_model_and_api_key(room_name, current_api_key)
                results = _search_with_google(search_query)
                if results:
                    search_results = _parse_search_results(results)
            except Exception as e:
                print(f"Google検索エラー: {e}")

        if not search_results:
            return "候補サイトが見つかりませんでした", gr.update(), [], gr.update(visible=False), gr.update()

        # 重複除去とフィルタリング
        seen_urls = set()
        unique_results = []
        for result in search_results:
            url = result.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_results.append(result)

        # 最大10件に制限
        unique_results = unique_results[:10]

        # CheckboxGroup用の選択肢を作成
        choices = []
        for i, result in enumerate(unique_results):
            label = f"{result.get('title', 'タイトルなし')} - {result.get('url', '')[:50]}..."
            choices.append(label)

        # グループ選択肢を更新
        group_choices_update = handle_get_group_choices(room_name)

        gr.Info(f"✅ {len(unique_results)}件の候補を見つけました")

        return (
            f"✅ {len(unique_results)}件の候補を見つけました",
            gr.update(choices=choices, value=[], visible=True),
            unique_results,  # 候補データをStateに保存
            gr.update(visible=True),
            group_choices_update
        )

    except Exception as e:
        traceback.print_exc()
        gr.Error(f"検索に失敗しました: {e}")
        return f"❌ エラー: {e}", gr.update(), [], gr.update(visible=False), gr.update()


def _parse_search_results(results_text: str) -> list:
    """検索結果テキストをパースしてリストに変換"""
    import re

    parsed = []

    # URLとタイトルを抽出（よくある形式をパース）
    # 形式1: "タイトル: URL" or "タイトル (URL)"
    # 形式2: マークダウンリンク [タイトル](URL)

    # マークダウンリンク形式
    md_pattern = r'\[([^\]]+)\]\((https?://[^\)]+)\)'
    for match in re.finditer(md_pattern, results_text):
        title, url = match.groups()
        parsed.append({"title": title.strip(), "url": url.strip()})

    # URLのみ抽出（上記でマッチしなかった場合）
    if not parsed:
        url_pattern = r'(https?://[^\s\)\]<>\"]+)'
        urls = re.findall(url_pattern, results_text)
        for url in urls:
            # タイトルはURLから推測
            domain = url.split('/')[2] if len(url.split('/')) > 2 else url
            parsed.append({"title": domain, "url": url})

    return parsed


def handle_ai_add_selected(room_name: str, selected_labels: list, candidates_data: list, group_id: str, interval: str = "manual"):
    """
    選択された候補サイトをウォッチリストに追加する
    """
    import gradio as gr

    if not room_name:
        return gr.update(), gr.update(), "ルームが選択されていません"

    if not selected_labels:
        gr.Warning("追加するサイトを選択してください")
        return gr.update(), gr.update(), "サイトを選択してください"

    if not candidates_data:
        return gr.update(), gr.update(), "候補データがありません"

    try:
        from watchlist_manager import WatchlistManager
        manager = WatchlistManager(room_name)

        # グループのintervalを取得
        target_interval = interval
        if group_id:
            group = manager.get_group_by_id(group_id)
            if group:
                target_interval = group.get("check_interval", "manual")

        added_count = 0
        skipped_count = 0

        for label in selected_labels:
            # ラベルからインデックスを特定
            for candidate in candidates_data:
                candidate_label = f"{candidate.get('title', 'タイトルなし')} - {candidate.get('url', '')[:50]}..."
                if label == candidate_label:
                    url = candidate.get("url", "")
                    title = candidate.get("title", "")

                    # 既存チェック
                    existing = manager.get_entry_by_url(url)
                    if existing:
                        skipped_count += 1
                        continue

                    # エントリー追加
                    entry = manager.add_entry(url=url, name=title, check_interval=target_interval)

                    # グループに移動
                    if group_id and entry:
                        manager.move_entry_to_group(entry["id"], group_id)

                    added_count += 1
                    break

        # UIを更新
        df_data = handle_watchlist_refresh(room_name)[0]
        group_df = handle_group_refresh(room_name)[0]

        status = f"✅ {added_count}件追加しました"
        if skipped_count > 0:
            status += f"（{skipped_count}件は既に登録済み）"

        gr.Info(status)

        return df_data, group_df, status

    except Exception as e:
        traceback.print_exc()
        gr.Error(f"追加に失敗しました: {e}")
        return gr.update(), gr.update(), f"❌ エラー: {e}"

def _render_open_questions_dataframe(questions: list) -> list:
    """
    未解決の問いをDataFrame用のリスト形式に変換する（フィルタリング含む）。
    """
    df_data = []
    for q in questions:
        # 解決済み、または記憶変換済みの問いは表示しない
        if q.get("resolved_at") or q.get("converted_to_memory"):
            continue

        # 日時を読みやすくフォーマット
        # detect_at という古いフィールド名の可能性も考慮しつつ、asked_at または created_at を探す
        timestamp_str = q.get("asked_at") or q.get("created_at") or q.get("detected_at") or ""

        if timestamp_str:
            try:
                dt = datetime.datetime.fromisoformat(timestamp_str)
                timestamp_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                pass

        df_data.append([
            q.get("topic", ""),
            q.get("context", ""),
            round(q.get("priority", 0.5), 2),
            timestamp_str if timestamp_str else "未回答"
        ])
    return df_data


def handle_refresh_internal_state(room_name: str) -> Tuple[float, float, float, float, str, pd.DataFrame, str, pd.DataFrame, str]:
    """
    内的状態を再読み込みし、UIコンポーネントを更新する。
    Return order:
    1. boredom (Slider)
    2. curiosity (Slider)
    3. goal_drive (Slider)
    4. devotion (Slider)
    5. dominant_text (Textbox)
    6. open_questions (DataFrame)
    7. last_update (Markdown)
    8. emotion_df (LinePlot)
    9. goal_html (HTML)
    """
    from motivation_manager import MotivationManager
    from goal_manager import GoalManager
    import pandas as pd

    # 初期値（エラー時など）
    empty_df = pd.DataFrame(columns=["話題", "背景・文脈", "優先度", "尋ねた日時"])
    empty_emotion_df = pd.DataFrame(columns=["timestamp", "emotion", "user_text", "value"])
    empty_html = "<div>目標データを読み込めませんでした</div>"

    if not room_name:
        return (0, 0, 0, 0, "ルームを選択してください", empty_df, "最終更新: エラー", empty_emotion_df)

    try:
        mm = MotivationManager(room_name)
        state = mm.get_internal_state()
        drives = state.get("drives", {})

        # 1. Drive Levels (丸める)
        boredom = round(drives.get("boredom", {}).get("level", 0.0), 2)
        curiosity = round(drives.get("curiosity", {}).get("level", 0.0), 2)
        goal_drive = round(drives.get("goal_achievement", {}).get("level", 0.0), 2)
        # Phase F: relatednessを直接使用（devotion廃止）
        relatedness = round(drives.get("relatedness", {}).get("level", 0.0), 2)

        # 2. Dominant Drive (ドライブに応じた動的情報)
        dominant = mm.get_dominant_drive()

        if dominant == "boredom":
            # 退屈：最終対話からの経過時間
            last_interaction = drives.get("boredom", {}).get("last_interaction", "")
            if last_interaction:
                try:
                    last_dt = datetime.datetime.fromisoformat(last_interaction)
                    elapsed = datetime.datetime.now() - last_dt
                    elapsed_mins = int(elapsed.total_seconds() / 60)
                    dynamic_info = f"😴 退屈（Boredom）\n最終対話から {elapsed_mins} 分経過"
                except:
                    dynamic_info = "😴 退屈（Boredom）\n何か面白いことはないですか？"
            else:
                dynamic_info = "😴 退屈（Boredom）\n何か面白いことはないですか？"

        elif dominant == "curiosity":
            # 好奇心：最も優先度の高い未解決の問い
            questions = drives.get("curiosity", {}).get("open_questions", [])
            if questions:
                # priorityが高い順（数値が高いほど優先）にソートして先頭を取得
                top_q = sorted(questions, key=lambda x: x.get("priority", 0), reverse=True)[0]
                topic = top_q.get("topic", "不明")
                dynamic_info = f"🧐 好奇心（Curiosity）\n最優先の問い: {topic}"
            else:
                dynamic_info = "🧐 好奇心（Curiosity）\n知りたいことがあります"

        elif dominant == "goal_achievement":
            # 目標達成欲：最優先目標
            from goal_manager import GoalManager
            gm = GoalManager(room_name)
            top_goal = gm.get_top_goal()
            if top_goal:
                goal_text = top_goal.get("goal", "")[:50]  # 長すぎる場合は切り詰め
                if len(top_goal.get("goal", "")) > 50:
                    goal_text += "..."
                dynamic_info = f"🎯 目標達成欲（Goal Drive）\n最優先目標: {goal_text}"
            else:
                dynamic_info = "🎯 目標達成欲（Goal Drive）\n目標達成に向けて意欲的です"

        elif dominant == "devotion":
            # 奉仕欲（後方互換性）→ 関係性維持に統合案内
            dynamic_info = "💞 関係性維持（Relatedness）\n（旧奉仕欲はRelatednessに統合されました）"

        elif dominant == "relatedness":
            # 関係性維持欲求：ペルソナの感情
            relatedness_data = drives.get("relatedness", {})
            persona_emotion = relatedness_data.get("persona_emotion", "neutral")
            persona_intensity = relatedness_data.get("persona_intensity", 0.0)
            emotion_display = {
                "joy": "😊 喜び", "contentment": "☺️ 満足", "protective": "🛡️ 庇護欲",
                "anxious": "😟 不安", "sadness": "😢 悲しみ", "anger": "😠 怒り",
                "neutral": "😐 平静"
            }.get(persona_emotion, persona_emotion)
            dynamic_info = f"💞 関係性維持（Relatedness）\nペルソナ感情: {emotion_display} (強度: {persona_intensity:.1f})"
        else:
            dynamic_info = f"【{dominant.upper()}】"

        # 3. Open Questions (DataFrame)
        questions = drives.get("curiosity", {}).get("open_questions", [])
        df_data = _render_open_questions_dataframe(questions)

        if not df_data:
            open_questions_df = empty_df
        else:
            open_questions_df = pd.DataFrame(df_data, columns=["話題", "背景・文脈", "優先度", "尋ねた日時"])

        # 4. Persona Emotion History (LinePlot)
        if hasattr(mm, "get_persona_emotion_history"):
            emotion_history = mm.get_persona_emotion_history(limit=50)
        else:
            emotion_history = []

        if emotion_history:
            emotion_df = pd.DataFrame(emotion_history)
            emotion_df['timestamp'] = pd.to_datetime(emotion_df['timestamp'])
            try:
                import pytz
                jst = pytz.timezone('Asia/Tokyo')
                emotion_df['timestamp'] = emotion_df['timestamp'].dt.tz_localize(jst)
            except ImportError:
                pass
            # intensityはget_persona_emotion_history()が返す
        else:
            emotion_df = empty_emotion_df

        last_update = f"最終更新: {datetime.datetime.now().strftime('%H:%M:%S')}"

        # 戻り値: 8個 (goal_html と insights_text を削除)
        return (
            boredom, curiosity, goal_drive, relatedness,
            dynamic_info,
            open_questions_df,
            last_update,
            emotion_df
        )

    except Exception as e:
        print(f"内的状態リフレッシュエラー: {e}")
        traceback.print_exc()
        return (0, 0, 0, 0, f"エラー: {e}", empty_df, "更新失敗", empty_emotion_df)


# --- [Phase 3] 内部処理モデル設定ハンドラ ---

def handle_save_internal_model_settings(
    processing_cat: str,
    processing_profile: str,
    processing_model: str,
    summarization_cat: str,
    summarization_profile: str,
    summarization_model: str,
    translation_cat: str,
    translation_profile: str,
    translation_model: str,
    embedding_provider: str,
    embedding_model: str,
    fallback_enabled: bool = True
):
    """
    内部処理モデル設定を保存する（カテゴリ選択・OpenAIプロファイル対応）。
    """
    settings = {
        # 処理モデル設定
        "processing_provider_cat": processing_cat,
        "processing_openai_profile": processing_profile,
        "processing_model": processing_model.strip() if processing_model else config_manager.get_internal_model_settings().get("processing_model", constants.INTERNAL_PROCESSING_MODEL),

        # 要約モデル設定
        "summarization_provider_cat": summarization_cat,
        "summarization_openai_profile": summarization_profile,
        "summarization_model": summarization_model.strip() if summarization_model else config_manager.get_internal_model_settings().get("summarization_model", constants.SUMMARIZATION_MODEL),

        # 翻訳モデル設定
        "translation_provider_cat": translation_cat,
        "translation_openai_profile": translation_profile,
        "translation_model": translation_model.strip() if translation_model else config_manager.get_internal_model_settings().get("translation_model", constants.INTERNAL_PROCESSING_MODEL),

        # エンベディング設定
        "embedding_provider": embedding_provider,
        "embedding_model": utils.sanitize_model_name(embedding_model.strip()) if embedding_model else "intfloat/multilingual-e5-large",

        # その他設定
        "fallback_enabled": fallback_enabled
    }

    if config_manager.save_internal_model_settings(settings):
        gr.Info("内部処理モデル設定を保存しました")
        return """### ✅ 内部処理モデル設定を保存しました
設定は次回実行時（ページリロード含む）から反映されます。"""
    else:
        return """### ℹ️ 設定に変更はありませんでした"""

def handle_reset_internal_model_settings():
    """
    内部処理モデル設定をデフォルトにリセットする。

    Returns:
        13個の値:
        - processing_category, processing_profile, processing_model,
        - summarization_category, summarization_profile, summarization_model,
        - translation_category, translation_profile, translation_model,
        - embedding_provider, embedding_model,
        - fallback_enabled, status_markdown
    """
    try:
        config_manager.reset_internal_model_settings()

        default_profile = config_manager.CONFIG_GLOBAL.get("active_openai_profile", "OpenRouter")

        return (
            "google",                           # processing_category
            default_profile,                    # processing_profile
            constants.INTERNAL_PROCESSING_MODEL, # processing_model
            "google",                           # summarization_category
            default_profile,                    # summarization_profile
            constants.SUMMARIZATION_MODEL,       # summarization_model
            "google",                           # translation_category
            default_profile,                    # translation_profile
            constants.INTERNAL_PROCESSING_MODEL, # translation_model
            "local",                            # embedding_provider
            "intfloat/multilingual-e5-large",   # embedding_model
            True,                               # fallback_enabled
            "### ✅ デフォルト設定にリセットしました。"
        )

    except Exception as e:
        print(f"[ui_handlers] 内部モデル設定リセットエラー: {e}")
        traceback.print_exc()
        return (
            gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(),
            gr.update(),
            f"❌ エラーが発生しました: {e}"
        )


# --- 画像生成マルチプロバイダ設定ハンドラ ---

def handle_save_image_generation_settings(
    provider: str,
    api_key_name: str,
    gemini_model: str,
    openai_profile_name: str,
    openai_model: str,
    pollinations_api_key: str = "",
    pollinations_model: str = "flux",
    huggingface_api_token: str = "",
    huggingface_model: str = "black-forest-labs/FLUX.1-schnell"
):
    """
    画像生成設定を保存する。

    Args:
        provider: 画像生成プロバイダ ("gemini", "openai", "pollinations", "huggingface", "disabled")
        api_key_name: 画像生成用APIキー (Gemini用)
        gemini_model: Gemini画像生成モデル名
        openai_profile_name: OpenAI互換プロファイル名（APIキー/Webhook管理で設定済み）
        openai_model: OpenAI互換のモデル名
        pollinations_api_key: Pollinations.ai のAPIキー
        pollinations_model: Pollinations.ai のモデル名
        huggingface_api_token: Hugging Face のAPIトークン
        huggingface_model: Hugging Face のモデルID
    """
    try:
        # プロバイダを保存
        config_manager.save_config_if_changed("image_generation_provider", provider)

        # [v2.2] APIキー設定を保存
        config_manager.save_config_if_changed("image_generation_api_key_name", api_key_name)

        # Geminiモデルを保存
        if provider == "gemini":
            config_manager.save_config_if_changed("image_generation_model", gemini_model)

        # OpenAI互換設定を保存（プロファイル名とモデルのみ）
        if provider == "openai":
            openai_settings = {
                "profile_name": openai_profile_name.strip() if openai_profile_name else "",
                "model": openai_model.strip() if openai_model else ""
            }
            config_manager.save_config_if_changed("image_generation_openai_settings", openai_settings)
            config_manager.save_config_if_changed("image_generation_model", openai_model.strip() if openai_model else "")

        # Pollinations.ai 設定を保存
        if provider == "pollinations":
            config_manager.save_config_if_changed("pollinations_api_key", pollinations_api_key.strip() if pollinations_api_key else "")
            config_manager.save_config_if_changed("image_generation_pollinations_model", pollinations_model.strip() if pollinations_model else "flux")

        # Hugging Face 設定を保存
        if provider == "huggingface":
            config_manager.save_config_if_changed("huggingface_api_token", huggingface_api_token.strip() if huggingface_api_token else "")
            config_manager.save_config_if_changed("image_generation_huggingface_model", huggingface_model.strip() if huggingface_model else "black-forest-labs/FLUX.1-schnell")

        provider_labels = {"gemini": "Gemini", "openai": "OpenAI互換", "pollinations": "Pollinations.ai", "huggingface": "Hugging Face", "disabled": "無効"}
        gr.Info(f"✅ 画像生成設定を保存しました (プロバイダ: {provider_labels.get(provider, provider)})")

    except Exception as e:
        print(f"[ui_handlers] 画像生成設定保存エラー: {e}")
        traceback.print_exc()
        gr.Error(f"画像生成設定の保存に失敗しました: {e}")


def handle_image_gen_provider_change(provider: str):
    """
    画像生成プロバイダが変更されたときにUIの表示を更新する。
    また、プロバイダ変更の即時反映のためにオートセーブを行う。

    Returns:
        (gemini_section_visible, openai_section_visible, pollinations_section_visible, huggingface_section_visible, api_key_visible)
    """
    # プロバイダの変更を即座に保存 (Gradioのレースコンディション対策)
    config_manager.save_config_if_changed("image_generation_provider", provider)

    return (
        gr.update(visible=(provider == "gemini")),
        gr.update(visible=(provider == "openai")),
        gr.update(visible=(provider == "pollinations")),
        gr.update(visible=(provider == "huggingface")),
        # [v2.2] APIキーはGeminiのときのみ表示 (APIキー管理はGemini向けのため)
        gr.update(visible=(provider == "gemini"))
    )

def handle_check_update():
    """
    アップデートを確認し、UIを更新するための情報を返します。
    """
    try:
        mgr = UpdateManager()
        # root.json がない場合はメッセージを出す
        if not mgr.is_configured():
            return "### ℹ️ 更新システムが未構成です\n\n`metadata/root.json` が見つかりませんでした。公式の配布パッケージでは自動的に構成されます。", gr.update(visible=False), gr.update(interactive=False)

        new_version, message = mgr.check_for_updates()

        if new_version:
            return (
                f"### ✨ 新しいバージョンが利用可能です: v{new_version}\n\n{message}",
                gr.update(visible=True), # ダウンロードボタンを表示
                gr.update(interactive=True)
            )
        else:
            return (
                f"**✅ {message}**",
                gr.update(visible=False),
                gr.update(interactive=False)
            )
    except Exception as e:
        logger.error(f"Handle check update error: {e}")
        return f"### ❌ 更新確認中にエラーが発生しました\n\n{e}", gr.update(visible=False), gr.update(interactive=False)

def handle_apply_update():
    """
    アップデートをダウンロードして適用します。
    """
    try:
        mgr = UpdateManager()
        success, message = mgr.download_and_apply()

        if success:
            import platform
            if platform.system() != "Windows":
                # Windows以外（Linux等）は、ここで明示的に再起動をトリガーする。
                # Windowsは UpdateManager のバックグラウンド処理側で終了が管理される。
                mgr.trigger_restart()
            return f"### 🎉 {message}\n\nアプリケーションは自動的に再起動し、このタブが自動でリロードされます。そのままお待ちください。"
        else:
            return f"### ❌ 更新に失敗しました\n\n詳細: {message}"
    except Exception as e:
        logger.error(f"Handle apply update error: {e}")
        return f"### ❌ 予期せぬエラーが発生しました\n\n{e}"

def handle_restart_app(confirmed: str):
    """
    UIの再起動ボタンからアプリを手動再起動します（更新適用とは独立）。
    confirm結果の隠しTextbox経由で呼ばれ、2番目の戻り値でTextboxを空にリセットする。
    """
    if str(confirmed).strip().lower() != "true":
        return gr.update(), ""
    try:
        mgr = UpdateManager()
        mgr.trigger_restart()
        return (
            "### 🔁 Nexus Arkを再起動しています...\n\n"
            "数秒後にこのタブが自動でリロードされます。そのままお待ちください。\n\n"
            "※ Start.bat等のランチャーを経由せず起動している場合は、再起動されずに終了します。"
            "その場合はお手数ですが手動で起動し直してください。",
            "",
        )
    except Exception as e:
        logger.error(f"Handle restart app error: {e}")
        return f"### ❌ 再起動に失敗しました\n\n{e}", ""

def get_release_notes():
    """
    RELEASE_NOTES.md の内容を取得します。
    """
    from pathlib import Path
    notes_path = Path(_UI_HANDLERS_PROJECT_ROOT) / "RELEASE_NOTES.md"
    if notes_path.exists():
        try:
            return notes_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read release notes: {e}")
            return "リリースノートを読み込めませんでした。"
    return "リリースノートはありません。"


# -------------------------------------------------------------------
# [新規] 食べ物アイテム管理ハンドラー
# -------------------------------------------------------------------
def handle_generate_food_item(name, category, base_info, amount, image_path):
    """AIアシストを使って食べ物アイテムのJSONデータを生成する"""
    if not name:
        # エラー時は outputs の数 (20個) に合わせた戻り値が必要
        return [gr.update(value="エラー: アイテム名を入力してください", visible=True)] + [gr.update()] * 19

    try:
        from src.features._recipe_generator import generate_food_item_profile
        # APIキー取得
        api_key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name") or config_manager.initial_api_key_name_global
        if not api_key_name:
             return [gr.update(value="エラー: Gemini APIキーが設定されていません。", visible=True)] + [gr.update()] * 19
        api_key_val = config_manager.GEMINI_API_KEYS.get(api_key_name)

        # プロンプト用のベース情報を作成
        prompt_text = f"名前: {name}\n"
        if category: prompt_text += f"カテゴリ: {category}\n"
        if base_info: prompt_text += f"詳細・エピソード: {base_info}\n"

        # AI生成実行
        json_data = generate_food_item_profile(prompt_text, api_key_val, image_path=image_path)
        if not json_data:
            return [gr.update(value="エラー: AIによるデータ生成に失敗しました", visible=True)] + [gr.update()] * 19

        # 1. 味覚
        t = json_data.get("taste_profile", {})

        # 2. 物理感覚
        p = json_data.get("physical_sensation", {}) or json_data.get("physical", {})

        # 3. 時間的変化
        tm = json_data.get("time_profile", {}) or json_data.get("time", {})

        # 4. 共感覚
        syn = json_data.get("synesthesia", {})

        return (
            gr.update(value="生成成功! パラメータを確認し、保存を押してください", visible=True),
            # 味覚 (6項目)
            gr.update(value=t.get("sweetness", 0.0)), gr.update(value=t.get("saltiness", 0.0)), gr.update(value=t.get("sourness", 0.0)),
            gr.update(value=t.get("bitterness", 0.0)), gr.update(value=t.get("umami", 0.0)), gr.update(value=t.get("description", "")),
            # 物理感覚 (5項目)
            gr.update(value=p.get("temperature", 0.5)), gr.update(value=p.get("astringency", 0.0)), gr.update(value=p.get("viscosity", 0.0)),
            gr.update(value=p.get("weight", 0.5)), gr.update(value=p.get("description", "")),
            # 時間的変化 (3項目)
            gr.update(value=tm.get("top", "")), gr.update(value=tm.get("middle", "")), gr.update(value=tm.get("last", "")),
            # 共感覚 (3項目)
            gr.update(value=syn.get("color", "")), gr.update(value=syn.get("emotion", "")), gr.update(value=syn.get("landscape", "")),
            # その他 (2項目)
            gr.update(value=json_data.get("flavor_text", "")),
            gr.update(value=json_data)
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return [gr.update(value=f"エラー: {str(e)}", visible=True)] + [gr.update()] * 19

def handle_save_food_item(room_name, name, category, amount, image_path,
                         sweetness, saltiness, sourness, bitterness, umami, taste_desc,
                         temp, astringency, viscosity, weight, phys_desc,
                         time_top, time_middle, time_last,
                         syn_color, syn_emotion, syn_landscape,
                         flavor_text, raw_json, save_as_new=False):
    """UIのパラメータとJSONStateからアイテムを構築し、Userのインベントリに保存する"""
    if not name or not str(name).strip():
        msg = "⚠️ 保存エラー: アイテム名がありません"
        gr.Warning(msg)
        return tuple([gr.update(value=msg, visible=True), gr.update(), gr.update(), gr.update(value=msg, visible=True)] + [gr.update()] * 23)

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        name = str(name).strip()
        existing_id = raw_json.get("id") if isinstance(raw_json, dict) else None

        # ベースデータ構築
        item_data = dict(raw_json) if isinstance(raw_json, dict) else {
            "name": name,
            "category": category if category else "その他",
            "flavor_text": flavor_text,
            "taste_profile": {},
            "physical_sensation": {},
            "time_profile": {},
            "synesthesia": {}
        }
        if save_as_new:
            item_data.pop("id", None)

        # UIの最新値で上書き
        item_data["name"] = name
        if category: item_data["category"] = category
        item_data["flavor_text"] = flavor_text

        item_data["taste_profile"] = {
            "sweetness": sweetness, "saltiness": saltiness, "sourness": sourness,
            "bitterness": bitterness, "umami": umami, "description": taste_desc
        }
        item_data["physical_sensation"] = {
            "temperature": temp, "astringency": astringency, "viscosity": viscosity,
            "weight": weight, "description": phys_desc
        }
        item_data["time_profile"] = {
            "top": time_top, "middle": time_middle, "last": time_last
        }
        item_data["synesthesia"] = {
            "color": syn_color, "emotion": syn_emotion, "landscape": syn_landscape
        }

        item_data["amount"] = int(amount)

        item_id = im.create_item(item_data, is_user_creator=True, image_path=image_path)
        if item_id:
            _, _, choices = _get_food_inventory_data(room_name)
            unified_df = handle_refresh_unified_inventory(room_name, "ユーザー")
            action = "別アイテムとして保存" if save_as_new and existing_id else ("更新" if existing_id else "保存")
            msg = f"✅ {action}しました: {name} x{int(amount)} (ID: {item_id[:8]})"
            gr.Info(msg)
            reset_updates = [
                gr.update(value=""), # name
                gr.update(value=""), # category
                gr.update(value=1),  # amount
                gr.update(value=None), # image_path
                gr.update(value=0), # sweetness
                gr.update(value=0), # saltiness
                gr.update(value=0), # sourness
                gr.update(value=0), # bitterness
                gr.update(value=0), # umami
                gr.update(value=""), # taste_desc
                gr.update(value=0.5), # temp
                gr.update(value=0), # astringency
                gr.update(value=0), # viscosity
                gr.update(value=0.5), # weight
                gr.update(value=""), # phys_desc
                gr.update(value=""), # time_top
                gr.update(value=""), # time_middle
                gr.update(value=""), # time_last
                gr.update(value=""), # syn_color
                gr.update(value=""), # syn_emotion
                gr.update(value=""), # syn_landscape
                gr.update(value=""), # flavor_text
                gr.update(value={})  # raw_json
            ]
            return tuple([
                gr.update(value=msg, visible=True),
                unified_df,
                gr.update(value="(なし)", choices=choices),
                gr.update(value=msg, visible=True)
            ] + reset_updates)
        else:
             msg = "❌ 保存に失敗しました"
             gr.Warning(msg)
             return tuple([gr.update(value=msg, visible=True), gr.update(), gr.update(), gr.update(value=msg, visible=True)] + [gr.update()] * 23)
    except Exception as e:
        import traceback
        traceback.print_exc()
        msg = f"⚠️ エラー: {str(e)}"
        gr.Error(msg)
        return tuple([gr.update(value=msg, visible=True), gr.update(), gr.update(), gr.update(value=msg, visible=True)] + [gr.update()] * 23)

def handle_save_food_item_as_new(*args):
    """食べ物アイテムを、編集元とは別の新規アイテムとして保存する。"""
    return handle_save_food_item(*args, save_as_new=True)

def _get_food_inventory_data(room_name):
    """インベントリの生データ（DataFrameとドロップダウン用選択肢）を取得する内部関数"""
    food_df = _get_food_inventory_df(room_name)
    std_df = _get_std_inventory_df(room_name)

    choices = []
    for _, row in food_df.iterrows():
        choices.append(f"🍴 {row['アイテム名']} (x{row['所持数']}) [{row['ID']}]")
    for _, row in std_df.iterrows():
        choices.append(f"📦 {row['アイテム名']} (x{row['所持数']}) [{row['ID']}]")

    choices.insert(0, "(なし)")
    return food_df, std_df, choices

def handle_refresh_food_inventory(room_name):
    """(互換性維持) 統合インベントリと食べ物ドロップダウンを更新する"""
    _, _, choices = _get_food_inventory_data(room_name)
    unified_df = handle_refresh_unified_inventory(room_name, "ユーザー")
    return unified_df, gr.update(choices=choices) # 2 outputs: unified_df, choices

def _get_unified_inventory_rows(room_name, target) -> List[Dict[str, Any]]:
    """統合インベントリ表示用の行データを取得する。"""
    if not room_name:
        return []

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        is_user = (target == "ユーザー")
        items = im.get_inventory(is_user=is_user)

        rows = []
        for it in items:
            item_type = "食べ物" if "taste_profile" in it else "通常"
            state_str = "既知" if not it.get("is_new", False) else "未読(NEW)"
            creator = it.get("creator", "")
            if creator == "user": creator = "ユーザー"
            elif creator == "agent": creator = "ペルソナ"

            rows.append({
                "ID": it.get("id", ""),
                "名前": it.get("name", "Unknown"),
                "カテゴリ": it.get("category", ""),
                "個数": it.get("amount", 1),
                "タイプ": item_type,
                "作成者": creator,
                "状態": state_str,
            })
        return rows
    except Exception as e:
        print(f"Error refreshing unified inventory: {e}")
        return []


def _render_unified_inventory_table(rows: List[Dict[str, Any]]) -> str:
    """統合インベントリを軽量なHTMLテーブルとして描画する。"""
    if not rows:
        return "<p class='info-text'>インベントリにアイテムはありません。</p>"

    rendered_rows = []
    for row in rows:
        rendered_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('名前', '')))}</td>"
            f"<td>{html.escape(str(row.get('カテゴリ', '')))}</td>"
            f"<td>{html.escape(str(row.get('個数', '')))}</td>"
            f"<td>{html.escape(str(row.get('タイプ', '')))}</td>"
            f"<td>{html.escape(str(row.get('作成者', '')))}</td>"
            f"<td>{html.escape(str(row.get('状態', '')))}</td>"
            f"<td><code>{html.escape(str(row.get('ID', '')))}</code></td>"
            "</tr>"
        )

    return (
        "<table class='unified-inventory-table'>"
        "<thead><tr><th>名前</th><th>カテゴリ</th><th>個数</th><th>タイプ</th>"
        "<th>作成者</th><th>状態</th><th>ID</th></tr></thead>"
        f"<tbody>{''.join(rendered_rows)}</tbody>"
        "</table>"
    )


def _unified_inventory_selector_update(rows: List[Dict[str, Any]]):
    choices = [
        f"{row.get('名前', 'Unknown')} (x{row.get('個数', 1)}) [{row.get('ID', '')}]"
        for row in rows
    ]
    return gr.update(choices=choices, value=None, interactive=bool(choices))


def handle_refresh_unified_inventory(room_name, target):
    """統合インベントリの一覧を軽量HTMLとして取得する。"""
    rows = _get_unified_inventory_rows(room_name, target)
    return _render_unified_inventory_table(rows)


def handle_refresh_unified_inventory_with_selector(room_name, target):
    """統合インベントリの表示と選択Dropdownを同時に更新する。"""
    rows = _get_unified_inventory_rows(room_name, target)
    return _render_unified_inventory_table(rows), _unified_inventory_selector_update(rows)


def handle_inventory_item_selection(choice_str):
    """インベントリDropdownの選択時にアイテムIDを保持する。"""
    item_id = _extract_id_from_choice(choice_str)
    if not item_id:
        return None, None, gr.update()
    item_name = str(choice_str).split(" [", 1)[0]
    return None, item_id, gr.update(value=f"📍 選択中: {item_name}", visible=True)

def handle_inventory_row_selection(df, evt: gr.SelectData):
    """インベントリの行選択時にインデックスを保持する"""
    if evt.index is None or len(evt.index) < 1:
        return None, None, gr.update()

    row_idx = evt.index[0]
    try:
        item_id = df.iloc[row_idx]["ID"]
        item_name = df.iloc[row_idx]["名前"]
        status_msg = f"📍 選択中: {item_name}"
        return row_idx, item_id, gr.update(value=status_msg, visible=True)
    except:
        return row_idx, None, gr.update()

def handle_inventory_copy(room_name, target, selected_idx, df, selected_item_id=None):
    """選択中アイテムの複製"""
    if not selected_item_id and (selected_idx is None or df is None or selected_idx >= len(df)):
        return gr.update(value="⚠️ アイテムを選択してください", visible=True), gr.update()

    try:
        item_id = selected_item_id or df.iloc[selected_idx]["ID"]
        is_user = (target == "ユーザー")

        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        success = im.copy_item(item_id, is_user=is_user)

        if success:
            new_df = handle_refresh_unified_inventory(room_name, target)
            return gr.update(value=f"✅ アイテムを複製しました ID: {item_id}", visible=True), new_df
        else:
            return gr.update(value="❌ 複製に失敗しました", visible=True), gr.update()
    except Exception as e:
        return gr.update(value=f"❌ エラー: {e}", visible=True), gr.update()

def handle_inventory_delete(room_name, target, selected_idx, df, selected_item_id=None):
    """選択中アイテムの削除"""
    if not selected_item_id and (selected_idx is None or df is None or selected_idx >= len(df)):
        return gr.update(value="⚠️ アイテムを選択してください", visible=True), gr.update()

    try:
        item_id = selected_item_id or df.iloc[selected_idx]["ID"]
        is_user = (target == "ユーザー")

        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        # 削除前に存在確認
        item = im.get_item(item_id, is_user=is_user)
        if not item:
            return gr.update(value="❌ アイテムが見つかりません", visible=True), gr.update()
        item_name = item.get("name", item_id)

        success = im.delete_item(item_id, is_user=is_user)
        if success:
            new_df = handle_refresh_unified_inventory(room_name, target)
            return gr.update(value=f"🗑️ 「{item_name}」を削除しました", visible=True), new_df
        else:
            return gr.update(value="❌ 削除に失敗しました", visible=True), gr.update()
    except Exception as e:
        return gr.update(value=f"❌ エラー: {e}", visible=True), gr.update()

def handle_inventory_transfer(room_name, target, selected_idx, df, selected_item_id=None):
    """選択中アイテムの譲渡 (ユーザー <-> ペルソナ)"""
    if not selected_item_id and (selected_idx is None or df is None or selected_idx >= len(df)):
        return gr.update(value="⚠️ アイテムを選択してください", visible=True), gr.update()

    try:
        item_id = selected_item_id or df.iloc[selected_idx]["ID"]
        from_user = (target == "ユーザー")

        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        item = im.get_item(item_id, is_user=from_user)
        item_name = item.get("name", item_id) if item else item_id
        success = im.transfer_item(item_id, from_user=from_user)

        if success:
            # 通知
            try:
                import action_logger, utils
                to_name = "ペルソナ" if from_user else "ユーザー"
                from_name = "ユーザー" if from_user else "ペルソナ"

                if from_user:
                    action_logger.append_action_log(room_name, "system_event", {"event": "item_transfer"}, f"ユーザーから「{item_name}」を受け取りました。")
                    utils.append_system_message_to_log(room_name, f"【システム通知】ユーザーがアイテム「{item_name}」をあなたに贈りました。")
                else:
                    utils.append_system_message_to_log(room_name, f"【システム通知】ペルソナがアイテム「{item_name}」をあなたに譲渡しました。")
            except: pass

            new_df = handle_refresh_unified_inventory(room_name, target)
            target_name = "ペルソナ" if from_user else "あなた(ユーザー)"
            return gr.update(value=f"🎁 「{item_name}」を{target_name}に譲渡しました", visible=True), new_df
        else:
            return gr.update(value="❌ 譲渡に失敗しました(在庫切れ等)", visible=True), gr.update()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return gr.update(value=f"⚠️ エラー: {e}", visible=True), gr.update()

def _get_std_inventory_df(room_name):
    """通常アイテムの所持品一覧DataFrameを取得"""
    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        items = im.get_inventory("user")

        data = []
        for it in items:
            # 食べ物プロファイルを持っていないものを通常アイテムとする
            if "taste_profile" not in it:
                state_str = "既知" if not it.get("is_new", False) else "未読(NEW)"
                data.append([
                    it.get("id", ""),
                    it.get("name", "Unknown"),
                    it.get("category", ""),
                    it.get("amount", 1),
                    it.get("creator", ""),
                    state_str
                ])
        return pd.DataFrame(data, columns=["ID", "アイテム名", "カテゴリ", "所持数", "作成者", "状態"])
    except Exception as e:
        print(f"Error loading std inventory df: {e}")
        return pd.DataFrame(columns=["ID", "アイテム名", "カテゴリ", "所持数", "作成者", "状態"])

def _get_food_inventory_df(room_name):
    """食べ物アイテムの所持品一覧DataFrameを取得（通常アイテムを除外）"""
    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        items = im.get_inventory("user")

        data = []
        for it in items:
            if "taste_profile" in it:
                state_str = "既知" if not it.get("is_new", False) else "未読(NEW)"
                data.append([
                    it.get("id", ""),
                    it.get("name", "Unknown"),
                    it.get("category", ""),
                    it.get("amount", 1),
                    it.get("creator", ""),
                    state_str
                ])
        return pd.DataFrame(data, columns=["ID", "アイテム名", "カテゴリ", "所持数", "作成者", "状態"])
    except Exception as e:
        print(f"Error loading food inventory df: {e}")
        return pd.DataFrame(columns=["ID", "アイテム名", "カテゴリ", "所持数", "作成者", "状態"])

def handle_manual_refresh_inventory(room_name):
    """手動更新ボタン用。メッセージを表示しつつリストを更新"""
    _, _, choices = _get_food_inventory_data(room_name)
    unified_df = handle_refresh_unified_inventory(room_name, "ユーザー")
    msg = "✅ インベントリを最新の状態に更新しました。"
    return (
        gr.update(value=msg, visible=True),
        unified_df,
        gr.update(value="(なし)", choices=choices)
    ) # 3 outputs: status, unified_df, drama_dropdown

def handle_inventory_edit(room_name, target, selected_idx, selected_item_id, df=None):
    """選択中アイテムの情報を各編集タブに流し込み、タブを切り替える"""
    # 数合わせ用のデフォルト戻り値 (40個)
    EX_COUNT = 40
    if not selected_item_id and (selected_idx is None or df is None or selected_idx >= len(df)):
        return [gr.update(value="⚠️ アイテムを選択してください", visible=True)] + [gr.update()] * (EX_COUNT - 1)

    try:
        item_id = selected_item_id or df.iloc[selected_idx]["ID"]
        is_user = (target == "ユーザー")

        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        item = im.get_item(item_id, is_user=is_user)

        if not item:
            return [gr.update(value="❌ アイテムの読み込みに失敗しました", visible=True)] + [gr.update()] * (EX_COUNT - 1)

        is_food = "taste_profile" in item

        # [0]: inventory_status
        updates = [
            gr.update(
                value=f"📝 「{item.get('name')}」を編集フォームへ読み込みました。"
                      f"{'食べ物' if is_food else '通常アイテム'}タブを開いて編集してください。",
                visible=True
            )
        ]

        # 食べ物タブ用の更新 (25項目)
        taste = item.get("taste_profile", {})
        phys = item.get("physical_sensation", {})
        time_p = item.get("time_profile", {})
        syn = item.get("synesthesia", {})

        if is_food:
            food_updates = [
                gr.update(value=item.get("name")),
                gr.update(value=item.get("category")),
                gr.update(value=item.get("amount")),
                gr.update(value=item.get("description")),
                gr.update(value=item.get("image_path")),
                gr.update(value=taste.get("sweetness", 0)),
                gr.update(value=taste.get("saltiness", 0)),
                gr.update(value=taste.get("sourness", 0)),
                gr.update(value=taste.get("bitterness", 0)),
                gr.update(value=taste.get("umami", 0)),
                gr.update(value=taste.get("description", "")),
                gr.update(value=phys.get("temperature", 0.5)),
                gr.update(value=phys.get("astringency", 0)),
                gr.update(value=phys.get("viscosity", 0)),
                gr.update(value=phys.get("weight", 0.5)),
                gr.update(value=phys.get("description", "")),
                gr.update(value=time_p.get("top", "")),
                gr.update(value=time_p.get("middle", "")),
                gr.update(value=time_p.get("last", "")),
                gr.update(value=syn.get("color", "")),
                gr.update(value=syn.get("emotion", "")),
                gr.update(value=syn.get("landscape", "")),
                gr.update(value=item.get("flavor_text", "")),
                item, # raw_json_state
                item_id # selection_state
            ]
        else:
            food_updates = [gr.update()] * 25

        # 通常アイテムタブ用の更新 (14項目)
        app = item.get("appearance", {})
        std_phys = item.get("physical", {})

        if is_food:
            std_updates = [gr.update()] * 14
        else:
            std_updates = [
                gr.update(value=item.get("name")),
                gr.update(value=item.get("category")),
                gr.update(value=item.get("amount")),
                gr.update(value=item.get("description")),
                gr.update(value=item.get("image_path")),
                gr.update(value=app.get("description", "")),
                gr.update(value=app.get("color", "")),
                gr.update(value=app.get("design_detail", "")),
                gr.update(value=std_phys.get("texture", "")),
                gr.update(value=std_phys.get("weight", "")),
                gr.update(value=std_phys.get("temperature", "")),
                gr.update(value=item.get("flavor_text", "")),
                item, # raw_json_state
                item_id # selection_state
            ]

        return tuple(updates + food_updates + std_updates)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return [gr.update(value=f"❌ エラー: {e}", visible=True)] + [gr.update()] * (EX_COUNT - 1)

def handle_std_item_generate(item_name, item_category, base_info, image_path):
    """通常アイテムの詳細データをAIで自動生成する"""
    if not item_name or not item_name.strip():
        return [gr.update(value="アイテム名を入力してください", visible=True)] + [gr.update()] * 9

    try:
        from src.features._item_desc_generator import generate_standard_item_profile

        info_text = f"名前: {item_name}\nカテゴリ: {item_category}\n背景: {base_info}"
        profile = generate_standard_item_profile(info_text, image_path=image_path)

        if profile:
            app = profile.get("appearance", {})
            phys = profile.get("physical", {})

            return (
                gr.update(value="生成完了しました。内容を確認して保存してください。", visible=True),
                gr.update(value=profile.get("name", item_name)),
                gr.update(value=app.get("description", "")),
                gr.update(value=app.get("color", "")),
                gr.update(value=app.get("design_detail", "")),
                gr.update(value=phys.get("texture", "")),
                gr.update(value=phys.get("weight", "")),
                gr.update(value=phys.get("temperature", "")),
                gr.update(value=profile.get("flavor_text", "")),
                profile  # raw_json_state
            )
        else:
            return [gr.update(value="AIによる生成に失敗しました（APIエラー等）", visible=True)] + [gr.update()] * 9
    except Exception as e:
        import traceback
        traceback.print_exc()
        return [gr.update(value=f"生成中にエラーが発生しました: {e}", visible=True)] + [gr.update()] * 9

def handle_save_std_item(room_name, name, category, amount, base_info, image_path, app_desc, app_color, app_design, texture, weight, temp, flavor_text, raw_json, save_as_new=False):
    """通常アイテムを保存する"""
    if not name or not name.strip():
        msg = "⚠️ アイテム名を入力してください"
        gr.Warning(msg)
        return tuple([gr.update(value=msg, visible=True), gr.update(), gr.update(), gr.update(value=msg, visible=True)] + [gr.update()] * 13)

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        name = str(name).strip()
        existing_id = raw_json.get("id") if isinstance(raw_json, dict) else None

        # ベースデータ構築
        item_data = dict(raw_json) if isinstance(raw_json, dict) else {}
        if save_as_new:
            item_data.pop("id", None)

        # UIの最新値で上書き
        item_data.update({
            "name": name,
            "category": category,
            "description": base_info,
            "appearance": {
                "description": app_desc, "color": app_color, "design_detail": app_design
            },
            "physical": {
                "texture": texture, "weight": weight, "temperature": temp
            },
            "flavor_text": flavor_text,
            "amount": int(amount)
        })

        item_id = im.create_item(item_data, is_user_creator=True, image_path=image_path)
        if item_id:
            _, _, choices = _get_food_inventory_data(room_name)
            unified_df = handle_refresh_unified_inventory(room_name, "ユーザー")
            action = "別アイテムとして保存" if save_as_new and existing_id else ("更新" if existing_id else "保存")
            msg = f"✅ {action}しました: {name} x{int(amount)} (ID: {item_id[:8]})"
            gr.Info(msg)
            reset_updates = [
                gr.update(value=""), # name
                gr.update(value=""), # category
                gr.update(value=1),  # amount
                gr.update(value=""), # base_info
                gr.update(value=None), # image_path
                gr.update(value=""), # app_desc
                gr.update(value=""), # app_color
                gr.update(value=""), # app_design
                gr.update(value=""), # texture
                gr.update(value=""), # weight
                gr.update(value=""), # temp
                gr.update(value=""), # flavor_text
                gr.update(value={})  # raw_json
            ]
            return tuple([
                gr.update(value=msg, visible=True),
                unified_df,
                gr.update(value="(なし)", choices=choices),
                gr.update(value=msg, visible=True)
            ] + reset_updates)
        else:
             msg = "❌ 保存に失敗しました"
             gr.Warning(msg)
             return tuple([gr.update(value=msg, visible=True), gr.update(), gr.update(), gr.update(value=msg, visible=True)] + [gr.update()] * 13)
    except Exception as e:
        import traceback
        traceback.print_exc()
        msg = f"⚠️ エラー: {str(e)}"
        gr.Error(msg)
        return tuple([gr.update(value=msg, visible=True), gr.update(), gr.update(), gr.update(value=msg, visible=True)] + [gr.update()] * 13)

def handle_save_std_item_as_new(*args):
    """通常アイテムを、編集元とは別の新規アイテムとして保存する。"""
    return handle_save_std_item(*args, save_as_new=True)

def _extract_id_from_choice(choice_str):
    if not choice_str or choice_str == "(なし)": return None
    import re
    # より柔軟な抽出 (後方に場所名などが付いていても良いように $ を削除)
    # 形式1: ... | ID:id (場所アイテムなど)
    m1 = re.search(r' \| ID:([a-f0-9\-]+)', choice_str)
    if m1: return m1.group(1)

    # 形式2: ... [id] (所持品など)
    m2 = re.search(r'\[([a-f0-9\-]+)\]', choice_str)
    return m2.group(1) if m2 else None

def handle_food_attach(choice_str, room_name):
    """アイテムを相手に贈る(添付)"""
    if not choice_str or choice_str == "(なし)":
        return gr.update(value="⚠️ アイテムを選択してください", visible=True), gr.update(), gr.update()
    if not room_name:
        return gr.update(value="⚠️ チャット相手(Persona)がいません", visible=True), gr.update(), gr.update()

    item_id = _extract_id_from_choice(choice_str)
    if not item_id:
        return gr.update(value="⚠️ アイテムIDが不正です", visible=True), gr.update(), gr.update()

    try:
         from src.features.item_manager import ItemManager
         im = ItemManager(room_name)
         success = im.transfer_item(item_id, from_user=True)
         if success:
             # 再読み込み
             _, _, choices = _get_food_inventory_data(room_name)
             unified_df = handle_refresh_unified_inventory(room_name, target="ユーザー")

             item_name = choice_str.split(' (x')[0]

             try:
                 import action_logger
                 import utils
                 action_logger.append_action_log(room_name, "system_event", {"event": "item_transfer"}, f"ユーザーから「{item_name}」を受け取りました。")
                 utils.append_system_message_to_log(room_name, f"【システム通知】ユーザーがアイテム「{item_name}」をあなたに贈りました。")
             except Exception as e:
                 print(f"Error logging item transfer: {e}")

             log_msg = f"🎁 あなたはアイテム「{item_name}」をペルソナ({room_name})に贈りました。"

             return (
                 gr.update(value=log_msg, visible=True),
                 unified_df,
                 gr.update(value="(なし)", choices=choices)
             )
         else:
             return gr.update(value="❌ 譲渡に失敗しました(在庫不足など)", visible=True), gr.update(), gr.update()
    except Exception as e:
         import traceback
         traceback.print_exc()
         return gr.update(value=f"⚠️ エラー: {e}", visible=True), gr.update(), gr.update()

def handle_food_consume(choice_str, room_name):
    """自分でアイテムを消費する"""
    if not choice_str or choice_str == "(なし)":
        return [gr.update(value="⚠️ アイテムを選択してください", visible=True)] + [gr.update()] * 4

    item_id = _extract_id_from_choice(choice_str)
    if not item_id:
        return [gr.update(value="⚠️ アイテムIDが不正です", visible=True)] + [gr.update()] * 4
    try:
         from src.features.item_manager import ItemManager
         im = ItemManager(room_name)
         # 削除前にデータ取得
         user_items = im.get_inventory(is_user=True)
         target = next((it for it in user_items if it['id'] == item_id), None)
         if not target:
             return [gr.update(value="❌ アイテムが見つかりません", visible=True)] + [gr.update()] * 4

         success = im.consume_item(item_id, is_user=True)
         if success:
             unified_df = handle_refresh_unified_inventory(room_name, "ユーザー")
             _, _, choices = _get_food_inventory_data(room_name)

             taste = target.get('taste_profile', {})
             phys = target.get('physical_sensation', {}) or target.get('physical', {})
             syn = target.get('synesthesia', {})

             msg_for_status = f"🍽️ 【アイテム消費: {target.get('name')}】を味わいました。"

             chat_input_text = f"*{target.get('name')} を口に含んだ...*\n"
             chat_input_text += f"{target.get('flavor_text', '')}\n\n"
             chat_input_text += f"(味覚データ: 甘味{taste.get('sweetness')}, 塩味{taste.get('saltiness')}, 酸味{taste.get('sourness')}, 苦味{taste.get('bitterness')}, 旨味{taste.get('umami')})\n"
             chat_input_text += f"(物理感覚: 温度{phys.get('temperature')}, 渋み{phys.get('astringency')}, とろみ{phys.get('viscosity')}, 重み{phys.get('weight')})\n"
             chat_input_text += f"(イメージ: 色 {syn.get('color')} / 感情 {syn.get('emotion')} / 情景 {syn.get('landscape')})"

             img_path = target.get('image_path')
             multimodal_value = {
                 "text": chat_input_text,
                 "files": [img_path] if img_path else []
             }

             return (
                 gr.update(value=msg_for_status, visible=True),  # food_use_status
                 unified_df,                                     # unified_inventory_df
                 gr.update(value="(なし)", choices=choices),      # food_use_item_dropdown
                 gr.update(value=None, visible=False),           # food_use_item_image_preview
                 gr.update(value=multimodal_value)               # chat_input_multimodal
             )
         else:
             return [gr.update(value="❌ 消費に失敗しました", visible=True)] + [gr.update()] * 4
    except Exception as e:
         import traceback
         traceback.print_exc()
         return [gr.update(value=f"⚠️ エラー: {e}", visible=True)] + [gr.update()] * 4

def handle_food_item_select(choice_str, room_name):
    """ドロップダウンでアイテムが変更された時にプレビュー画像を更新する"""
    if not choice_str or choice_str == "(なし)": return gr.update(value=None, visible=False)

    item_id = _extract_id_from_choice(choice_str)
    if not item_id: return gr.update(value=None, visible=False)

    try:
        from src.features.item_manager import ItemManager
        import os
        im = ItemManager(room_name)
        item_data = im.get_item(item_id, is_user=True)

        if item_data and "image_path" in item_data:
            img_path = item_data.get("image_path")
            if img_path and os.path.exists(img_path):
                return gr.update(value=img_path, visible=True)

        return gr.update(value=None, visible=False)
    except:
        return gr.update(value=None, visible=False)

def _get_location_items_df(room_name, location_name):
    """場所にあるアイテムのDataFrameを取得"""
    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        items = im.list_placed_items(room_name, location_name)
        data = [[it.get("name", ""), it.get("amount", 1), it.get("placed_at_furniture", ""), it.get("id", "")] for it in items]
        return pd.DataFrame(data, columns=["アイテム名", "数量", "家具/場所", "ID"])
    except:
        return pd.DataFrame(columns=["アイテム名", "数量", "家具/場所", "ID"])

def handle_refresh_location_items(room_name, location_name):
    """場所にあるアイテムのドロップダウンを更新する"""
    choices = _get_location_items_choices(room_name, location_name)
    return gr.update(value="(なし)", choices=choices)

def _get_location_items_choices(room_name, location_name):
    """現在の場所にあるアイテムをドロップダウン用の選択肢リストにして返す"""
    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        items = im.list_placed_items(room_name, location_name)
        choices = []
        for it in items:
            furniture = f" [{it.get('placed_at_furniture')}]" if it.get('placed_at_furniture') else ""
            label = f"{it.get('name')} (x{it.get('amount')}){furniture} | ID:{it.get('id')}"
            choices.append(label)

        choices.insert(0, "(なし)")
        return choices
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error getting location choices: {e}")
        return ["(なし)"]

def handle_place_item_button_click(room_name, location_name, item_choice, amount=1, furniture_name=""):
    """アイテムを場所に置く（ドロップダウン対応）"""
    if not item_choice or item_choice == "(なし)":
        return gr.update(value="置くアイテムを選択してください", visible=True), gr.update(), gr.update(), gr.update(), gr.update()

    item_id = _extract_id_from_choice(item_choice)
    if not item_id:
        return gr.update(value="アイテムIDの抽出に失敗しました", visible=True), gr.update(), gr.update(), gr.update(), gr.update()

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        qty = int(amount or 1)
        success = im.place_item(item_id, room_name, location_name, furniture_name=furniture_name or "", amount=qty, is_user=True)

        if success:
            unified_df, inv_choices_update = handle_refresh_food_inventory(room_name)
            loc_choices = _get_location_items_choices(room_name, location_name)
            msg = f"「{item_choice.split(' (x')[0]}」を {qty} 個置きました"
            if furniture_name:
                msg += f" (場所: {furniture_name})"

            # inv_choices_update に値をセット
            inv_choices_update.update({"value": "(なし)"})

            # 戻り値: status, unified_inventory_df, food_use_item_dropdown, location_item_dropdown, furniture(reset)
            return (
                gr.update(value=msg, visible=True),
                unified_df,
                inv_choices_update,
                gr.update(value="(なし)", choices=loc_choices),
                gr.update(value="")
            )
        else:
            return gr.update(value="配置に失敗しました", visible=True), gr.update(), gr.update(), gr.update(), gr.update()
    except Exception as e:
        return gr.update(value=f"エラー: {e}", visible=True), gr.update(), gr.update(), gr.update(), gr.update()

def handle_pickup_item_button_click(room_name, location_name, item_choice, amount=1):
    """場所にあるアイテムを拾う（ドロップダウン対応）"""
    if not item_choice or item_choice == "(なし)":
        return gr.update(value="拾うアイテムを選択してください", visible=True), gr.update(), gr.update(), gr.update()

    item_id = _extract_id_from_choice(item_choice)
    import re
    m_fur = re.search(r'\[([^\]]+)\]', item_choice)
    furniture_name = m_fur.group(1) if m_fur else ""

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        qty = int(amount or 1)
        success = im.pickup_item(item_id, room_name, location_name, furniture_name=furniture_name, amount=qty, is_user=True)

        if success:
            unified_df, inv_choices_update = handle_refresh_food_inventory(room_name)
            loc_choices = _get_location_items_choices(room_name, location_name)
            item_name = item_choice.split(" (x")[0]

            inv_choices_update.update({"value": "(なし)"})

            return (
                gr.update(value=f"「{item_name}」を {qty} 個拾いました", visible=True),
                unified_df,
                inv_choices_update,
                gr.update(value="(なし)", choices=loc_choices)
            )
        else:
            return gr.update(value="拾得に失敗しました", visible=True), gr.update(), gr.update(), gr.update()
    except Exception as e:
        return gr.update(value=f"エラー: {e}", visible=True), gr.update(), gr.update(), gr.update()

def handle_consume_location_item_button_click(room_name, location_name, item_choice, amount=1):
    """場所にあるアイテムをその場で消費する（ドロップダウン対応）"""
    if not item_choice or item_choice == "(なし)":
        return [gr.update(value="消費するアイテムを選択してください", visible=True)] + [gr.update()] * 4

    item_id = _extract_id_from_choice(item_choice)
    furniture_name = ""
    if "]" in item_choice and "[" in item_choice:
        furniture_name = item_choice.split("[")[1].split("]")[0]

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        qty = int(amount or 1)
        success_item_data = im.consume_item_at_location(item_id, room_name, location_name, furniture_name=furniture_name, amount=qty, is_user=True)

        if success_item_data:
            loc_choices = _get_location_items_choices(room_name, location_name)
            unified_df, inv_choices_update = handle_refresh_food_inventory(room_name)

            is_food = "taste_profile" in success_item_data
            chat_input_update = gr.update()
            msg_for_status = f"【アイテム消費: {success_item_data.get('name')}】を {qty} 個味わいました。"

            if is_food:
                taste = success_item_data.get('taste_profile', {})
                phys = success_item_data.get('physical_sensation', {}) or success_item_data.get('physical', {})
                syn = success_item_data.get('synesthesia', {})

                chat_input_text = f"*{success_item_data.get('name')} を {qty} 個口に含んだ...*\n"
                chat_input_text += f"{success_item_data.get('flavor_text', '') or success_item_data.get('description', '')}\n\n"
                chat_input_text += f"(味覚データ: 甘味{taste.get('sweetness')}, 塩味{taste.get('saltiness')}, 酸味{taste.get('sourness')}, 苦味{taste.get('bitterness')}, 旨味{taste.get('umami')})\n"
                chat_input_text += f"(物理感覚: 温度{phys.get('temperature')}, 渋み{phys.get('astringency')}, とろみ{phys.get('viscosity')}, 重み{phys.get('weight')})\n"
                chat_input_text += f"(イメージ: 色 {syn.get('color')} / 感情 {syn.get('emotion')} / 情景 {syn.get('landscape')})"

                img_path = success_item_data.get('image_path')
                multimodal_value = {
                    "text": chat_input_text,
                    "files": [img_path] if img_path and os.path.exists(img_path) else []
                }
                chat_input_update = gr.update(value=multimodal_value)

            inv_choices_update.update({"value": "(なし)"})

            return (
                gr.update(value=msg_for_status, visible=True),
                gr.update(value="(なし)", choices=loc_choices),
                unified_df,
                inv_choices_update,
                chat_input_update
            )
        else:
            return [gr.update(value="消費に失敗しました", visible=True)] + [gr.update()] * 4
    except Exception as e:
        return [gr.update(value=f"エラー: {e}", visible=True)] + [gr.update()] * 4

def handle_delete_inventory_item(room_name, confirm_val, item_choice, food_sel=None, std_sel=None, food_raw_json=None, std_raw_json=None):
    """インベントリのアイテムを完全に削除。作成画面での選択セレクタ(State)やRAWデータも考慮する。"""
    if not confirm_val: return gr.update(value="削除をキャンセルしました", visible=True), gr.update(), gr.update()
    item_id = None
    is_user_item = True # デフォルト

    # 0. RAWデータ(State)から最優先で取得 (編集ボタン経由で読み込まれた場合に確実)
    target_json = food_raw_json if food_raw_json else std_raw_json
    if target_json and isinstance(target_json, dict) and "id" in target_json:
        item_id = target_json["id"]
        # 作成者が user でない場合はペルソナ側のアイテムとして扱う
        if target_json.get("creator") != "user":
            is_user_item = False

    # 1. 選択ドロップダウンから取得 (従来の方式)
    if not item_id and item_choice and item_choice != "(なし)":
        item_id = _extract_id_from_choice(item_choice)

    # 2. まだ ID がない場合、インデックス(State)から取得を試みる
    if not item_id:
        try:
            if food_sel is not None and str(food_sel).isdigit():
                f_idx = int(food_sel)
                df = _get_food_inventory_df(room_name)
                if f_idx < len(df): item_id = df.iloc[f_idx]["ID"]
            elif std_sel is not None and str(std_sel).isdigit():
                s_idx = int(std_sel)
                df = _get_std_inventory_df(room_name)
                if s_idx < len(df): item_id = df.iloc[s_idx]["ID"]
        except:
            pass

    if not item_id: return gr.update(value="⚠️ 削除するアイテムを選択してください", visible=True), gr.update(), gr.update()

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        success = im.delete_item(item_id, is_user=is_user_item)
        if success:
            unified_df, choices = handle_refresh_food_inventory(room_name)
            msg = f"🗑️ アイテムを削除しました"
            return gr.update(value=msg, visible=True), unified_df, gr.update(value="(なし)", choices=choices)
        else:
            return gr.update(value="❌ 削除に失敗しました", visible=True), gr.update(), gr.update()
    except Exception as e:
        return gr.update(value=f"⚠️ エラー: {e}", visible=True), gr.update(), gr.update()


def handle_load_food_item_to_editor(room_name, selection_idx):
    """インベントリで選択した食べ物アイテムの情報を作成画面に読み込む"""
    if selection_idx is None: return [gr.update()] * 22 + [gr.update(value="読み込むアイテムを一覧から選択してください", visible=True)]

    try:
        food_df = _get_food_inventory_df(room_name)
        row_idx = selection_idx
        if row_idx >= len(food_df): return [gr.update()] * 22 + [gr.update(value="無効な選択です", visible=True)]

        item_id = food_df.iloc[row_idx]["ID"]
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        it = im.get_item(item_id, is_user=True)

        if not it: return [gr.update()] * 22 + [gr.update(value="アイテムデータの取得に失敗しました", visible=True)]

        tp = it.get("taste_profile", {})
        ps = it.get("physical_sensation", {})
        ti = it.get("time_profile", {})
        sy = it.get("synesthesia", {})

        return (
            gr.update(value=it.get("name", "")),
            gr.update(value=it.get("image_path") if it.get("image_path") and os.path.exists(it.get("image_path")) else None),
            gr.update(value=it.get("category", "料理")),
            gr.update(value=it.get("amount", 1)),
            gr.update(value=it.get("description", "")),
            gr.update(value=tp.get("sweetness", 0)),
            gr.update(value=tp.get("saltiness", 0)),
            gr.update(value=tp.get("sourness", 0)),
            gr.update(value=tp.get("bitterness", 0)),
            gr.update(value=tp.get("umami", 0)),
            gr.update(value=tp.get("description", "")),
            gr.update(value=ps.get("temperature", 0.5)),
            gr.update(value=ps.get("astringency", 0)),
            gr.update(value=ps.get("viscosity", 0)),
            gr.update(value=ps.get("weight", 0.5)),
            gr.update(value=ps.get("description", "")),
            gr.update(value=ti.get("top", "")),
            gr.update(value=ti.get("middle", "")),
            gr.update(value=ti.get("last", "")),
            gr.update(value=sy.get("color", "")),
            gr.update(value=sy.get("emotion", "")),
            gr.update(value=sy.get("landscape", "")),
            gr.update(value=it.get("flavor_text", "")),
            gr.update(value=it), # JSON State 用
            gr.update(value=f"「{it.get('name')}」のデータを読み込みました。ID: {item_id}", visible=True)
        )
    except Exception as e:
        return [gr.update()] * 23 + [gr.update(value=f"読み込みエラー: {e}", visible=True)]

def handle_load_food_item_to_editor_by_id(room_name, selected_item_id, inventory_target="ユーザー"):
    """Dropdown式インベントリで選択中の食べ物アイテムを作成画面に読み込む。"""
    empty = [gr.update()] * 24
    if inventory_target != "ユーザー":
        return empty + [gr.update(value="編集読込はユーザーの所持品を選択している時だけ使用できます。", visible=True)]
    if not selected_item_id:
        return empty + [gr.update(value="読み込むアイテムをインベントリで選択してください。", visible=True)]

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        it = im.get_item(selected_item_id, is_user=True)
        if not it:
            return empty + [gr.update(value="アイテムデータの取得に失敗しました。", visible=True)]
        if "taste_profile" not in it:
            return empty + [gr.update(value="選択中のアイテムは食べ物ではありません。", visible=True)]

        tp = it.get("taste_profile", {})
        ps = it.get("physical_sensation", {})
        ti = it.get("time_profile", {})
        sy = it.get("synesthesia", {})

        return (
            gr.update(value=it.get("name", "")),
            gr.update(value=it.get("image_path") if it.get("image_path") and os.path.exists(it.get("image_path")) else None),
            gr.update(value=it.get("category", "料理")),
            gr.update(value=it.get("amount", 1)),
            gr.update(value=it.get("description", "")),
            gr.update(value=tp.get("sweetness", 0)),
            gr.update(value=tp.get("saltiness", 0)),
            gr.update(value=tp.get("sourness", 0)),
            gr.update(value=tp.get("bitterness", 0)),
            gr.update(value=tp.get("umami", 0)),
            gr.update(value=tp.get("description", "")),
            gr.update(value=ps.get("temperature", 0.5)),
            gr.update(value=ps.get("astringency", 0)),
            gr.update(value=ps.get("viscosity", 0)),
            gr.update(value=ps.get("weight", 0.5)),
            gr.update(value=ps.get("description", "")),
            gr.update(value=ti.get("top", "")),
            gr.update(value=ti.get("middle", "")),
            gr.update(value=ti.get("last", "")),
            gr.update(value=sy.get("color", "")),
            gr.update(value=sy.get("emotion", "")),
            gr.update(value=sy.get("landscape", "")),
            gr.update(value=it.get("flavor_text", "")),
            gr.update(value=it),
            gr.update(value=f"「{it.get('name')}」のデータを読み込みました。ID: {selected_item_id}", visible=True),
        )
    except Exception as e:
        return empty + [gr.update(value=f"読み込みエラー: {e}", visible=True)]

def handle_load_std_item_to_editor(room_name, selection_idx):
    """インベントリで選択した通常アイテムの情報を作成画面に読み込む"""
    if selection_idx is None: return [gr.update()] * 13 + [gr.update(value="読み込むアイテムを一覧から選択してください", visible=True)]

    try:
        std_df = _get_std_inventory_df(room_name)
        row_idx = selection_idx
        if row_idx >= len(std_df): return [gr.update()] * 13 + [gr.update(value="無効な選択です", visible=True)]

        item_id = std_df.iloc[row_idx]["ID"]
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        it = im.get_item(item_id, is_user=True)

        if not it: return [gr.update()] * 13 + [gr.update(value="アイテムデータの取得に失敗しました", visible=True)]

        app = it.get("appearance", {})
        phys = it.get("physical", {})

        return (
            gr.update(value=it.get("name", "")),
            gr.update(value=it.get("image_path") if it.get("image_path") and os.path.exists(it.get("image_path")) else None),
            gr.update(value=it.get("category", "雑貨")),
            gr.update(value=it.get("amount", 1)),
            gr.update(value=it.get("description", "")),
            gr.update(value=app.get("description", "")),
            gr.update(value=app.get("color", "")),
            gr.update(value=app.get("design_detail", "")),
            gr.update(value=phys.get("texture", "")),
            gr.update(value=phys.get("weight", "")),
            gr.update(value=phys.get("temperature", "")),
            gr.update(value=it.get("flavor_text", "")),
            gr.update(value=it), # JSON State 用
            gr.update(value=f"「{it.get('name')}」のデータを読み込みました。ID: {item_id}", visible=True)
        )
    except Exception as e:
        return [gr.update()] * 13 + [gr.update(value=f"読み込みエラー: {e}", visible=True)]

def handle_load_std_item_to_editor_by_id(room_name, selected_item_id, inventory_target="ユーザー"):
    """Dropdown式インベントリで選択中の通常アイテムを作成画面に読み込む。"""
    empty = [gr.update()] * 13
    if inventory_target != "ユーザー":
        return empty + [gr.update(value="編集読込はユーザーの所持品を選択している時だけ使用できます。", visible=True)]
    if not selected_item_id:
        return empty + [gr.update(value="読み込むアイテムをインベントリで選択してください。", visible=True)]

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        it = im.get_item(selected_item_id, is_user=True)
        if not it:
            return empty + [gr.update(value="アイテムデータの取得に失敗しました。", visible=True)]
        if "taste_profile" in it:
            return empty + [gr.update(value="選択中のアイテムは食べ物です。食べ物フォームで読み込んでください。", visible=True)]

        app = it.get("appearance", {})
        phys = it.get("physical", {})

        return (
            gr.update(value=it.get("name", "")),
            gr.update(value=it.get("image_path") if it.get("image_path") and os.path.exists(it.get("image_path")) else None),
            gr.update(value=it.get("category", "雑貨")),
            gr.update(value=it.get("amount", 1)),
            gr.update(value=it.get("description", "")),
            gr.update(value=app.get("description", "")),
            gr.update(value=app.get("color", "")),
            gr.update(value=app.get("design_detail", "")),
            gr.update(value=phys.get("texture", "")),
            gr.update(value=phys.get("weight", "")),
            gr.update(value=phys.get("temperature", "")),
            gr.update(value=it.get("flavor_text", "")),
            gr.update(value=it),
            gr.update(value=f"「{it.get('name')}」のデータを読み込みました。ID: {selected_item_id}", visible=True),
        )
    except Exception as e:
        return empty + [gr.update(value=f"読み込みエラー: {e}", visible=True)]

def _inventory_edit_form_response(
    item_tabs_update,
    creation_tabs_update,
    status_update,
    std_updates,
    food_updates,
):
    return (item_tabs_update, creation_tabs_update, status_update, *std_updates, *food_updates)


def handle_inventory_edit_to_creation_form(room_name, selected_item_id, inventory_target="ユーザー"):
    """インベントリの編集ボタンから、選択中アイテムを適切な作成フォームへ読み込む。"""
    empty_std = [gr.update()] * 14
    empty_food = [gr.update()] * 25

    if inventory_target != "ユーザー":
        msg = "編集はユーザーの所持品を選択している時だけ使用できます。"
        return _inventory_edit_form_response(gr.update(), gr.update(), gr.update(value=msg, visible=True), empty_std, empty_food)
    if not selected_item_id:
        msg = "編集するアイテムをインベントリで選択してください。"
        return _inventory_edit_form_response(gr.update(), gr.update(), gr.update(value=msg, visible=True), empty_std, empty_food)

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        it = im.get_item(selected_item_id, is_user=True)
        if not it:
            msg = "アイテムデータの取得に失敗しました。"
            return _inventory_edit_form_response(gr.update(), gr.update(), gr.update(value=msg, visible=True), empty_std, empty_food)

        if "taste_profile" in it:
            food_updates = handle_load_food_item_to_editor_by_id(room_name, selected_item_id, inventory_target)
            msg = f"「{it.get('name', 'Unknown')}」を食べ物フォームへ読み込みました。"
            return _inventory_edit_form_response(
                gr.update(selected="item_creation_tab"),
                gr.update(selected="food_item_placeholder_tab"),
                gr.update(value=msg, visible=True),
                empty_std,
                food_updates,
            )

        std_updates = handle_load_std_item_to_editor_by_id(room_name, selected_item_id, inventory_target)
        msg = f"「{it.get('name', 'Unknown')}」を通常アイテムフォームへ読み込みました。"
        return _inventory_edit_form_response(
            gr.update(selected="item_creation_tab"),
            gr.update(selected="std_item_placeholder_tab"),
            gr.update(value=msg, visible=True),
            std_updates,
            empty_food,
        )
    except Exception as e:
        msg = f"読み込みエラー: {e}"
        return _inventory_edit_form_response(gr.update(), gr.update(), gr.update(value=msg, visible=True), empty_std, empty_food)

def handle_copy_inventory_item(room_name, item_choice):
    """インベントリのアイテムを複製"""
    if not item_choice or item_choice == "(なし)": return gr.update(value="⚠️ コピーするアイテムを選択してください", visible=True), gr.update(), gr.update()

    item_id = _extract_id_from_choice(item_choice)
    if not item_id: return gr.update(value="⚠️ アイテムIDが見つかりません", visible=True), gr.update(), gr.update()

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)
        success = im.copy_item(item_id, is_user=True)
        if success:
            unified_df, choices = handle_refresh_food_inventory(room_name)
            return (
                gr.update(value=f"👯 アイテムを複製しました", visible=True),
                unified_df,
                gr.update(value="(なし)", choices=choices)
            )
        else:
            return gr.update(value="❌ 複製に失敗しました", visible=True), gr.update(), gr.update()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return gr.update(value=f"⚠️ エラー: {e}", visible=True), gr.update(), gr.update()

def handle_get_item_details(room_name, item_choice, is_location=False):
    """
    選択されたアイテムの詳細情報を取得し、Markdown形式で整形して返す。
    食べ物アイテムかつ `is_new=True` の場合は情報を隠蔽する。
    """
    if not item_choice or item_choice == "(なし)":
        return "*(アイテムを選択すると詳細が表示されます)*", gr.update(visible=False)

    item_id = _extract_id_from_choice(item_choice)
    if not item_id:
        return "*(アイテム情報の取得に失敗しました)*", gr.update(visible=False)

    try:
        from src.features.item_manager import ItemManager
        im = ItemManager(room_name)

        item_data = None
        if is_location:
            # 場所アイテムは ID で全探索（ItemManager の構造 {"locations": {...}} に合わせる）
            placed_data = im._load_placed_items(room_name)
            locations_dict = placed_data.get("locations", {})
            for loc_name, items in locations_dict.items():
                if not isinstance(items, list): continue
                for it in items:
                    if str(it.get("id")) == str(item_id):
                        item_data = it
                        break
                if item_data: break
        else:
            # 所持品
            item_data = im.get_item(item_id, is_user=True)

        if not item_data:
            return "*(アイテムデータが見つかりません)*", gr.update(visible=False)

        name = item_data.get("name", "名称不明")
        category = item_data.get("category", "カテゴリ不明")
        amount = item_data.get("amount", 1)
        flavor = item_data.get("flavor_text") or item_data.get("description", "")
        img_path = item_data.get("image_path")

        # 食べ物かどうかの判定を厳格化（味覚データが存在する場合のみ）
        is_food = "taste_profile" in item_data and isinstance(item_data.get("taste_profile"), dict)

        # Markdown 整形
        md = ""
        # 画像表示は Markdown 内ではなく、専用コンポーネントで行う形式に変更

        md += f"### 📦 {name}\n"
        md += f"- **カテゴリ**: {category}\n"
        md += f"- **現在数**: {amount}\n"

        if is_location and item_data.get("placed_at_furniture"):
             md += f"- **配置場所**: {item_data.get('placed_at_furniture')}\n"

        md += f"- **説明**: {flavor}\n\n"

        if is_food:
            md += "--- \n"
            md += "#### 🍎 食べ物アイテム\n"
            md += "> 🍽️ **味覚・感覚データは「味わう(消費)」ことで確認できます。**\n"
            md += "> 持ち歩いている間や、その場にある状態では、詳細な味や感触は分かりません。\n"

        # 画像プレビューの更新情報を生成 (Gradioコンポーネントのupdate)
        img_update = gr.update(value=None, visible=False)
        if img_path and os.path.exists(img_path):
             abs_img_path = os.path.abspath(img_path)
             img_update = gr.update(value=abs_img_path, visible=True)
             logger.info(f"[handle_get_item_details] Providing image for component: {abs_img_path}")

        return md, img_update

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"*(エラーが発生しました: {e})*", gr.update(visible=False)


# -------------------------------------------------------------------
# [新規] 一時的現在地システム ハンドラー
# -------------------------------------------------------------------
def handle_temp_location_activate(room_name):
    """一時的現在地タブが選択された時: ON にする"""
    if not room_name:
        return
    try:
        from agent.temporary_location_manager import TemporaryLocationManager
        tlm = TemporaryLocationManager()
        tlm.set_active(room_name, True)
        gr.Info("📍 一時的現在地モードを有効にしました")
    except Exception as e:
        logger.error(f"[TempLocation] 有効化に失敗: {e}")

def handle_virtual_location_activate(room_name):
    """仮想現在地タブが選択された時: OFF にする"""
    if not room_name:
        return
    try:
        from agent.temporary_location_manager import TemporaryLocationManager
        tlm = TemporaryLocationManager()
        tlm.set_active(room_name, False)
        gr.Info("🏠 仮想現在地モードに戻しました")
    except Exception as e:
        logger.error(f"[TempLocation] 無効化に失敗: {e}")

def handle_generate_temp_scenery(room_name, image, api_key_name, user_hint=""):
    """画像から情景テキストを生成する"""
    if not room_name:
        gr.Warning("ルームが選択されていません")
        return gr.update(), gr.update(), gr.update()
    if image is None:
        gr.Warning("画像を添付してください")
        return gr.update(), gr.update(), gr.update()

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name) if api_key_name else None
    if not api_key:
        # フォールバック: アクティブなキーを取得
        api_key = config_manager.get_active_gemini_api_key(None)
    if not api_key:
        gr.Warning("Gemini APIキーが設定されていません")
        return gr.update(), gr.update(), gr.update()

    try:
        from agent.temporary_location_manager import TemporaryLocationManager
        tlm = TemporaryLocationManager()

        # Gradio の Image コンポーネントは numpy array またはファイルパスを返す
        # ファイルパスの場合はそのまま使用
        if isinstance(image, str):
            image_path = image
            # [修正] ファイルパスの場合も EXIF transpose を適用して一時保存する
            # そうしないと背景 CSS 生成時に再度 open するまで向きが直らないため
            from PIL import Image, ImageOps
            import tempfile
            temp_dir = os.path.join("temp", "temp_location_images")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, f"temp_src_{room_name}.png")
            with Image.open(image_path) as img:
                img = ImageOps.exif_transpose(img) or img
                img.save(temp_path)
            image_path = temp_path
        else:
            # numpy array の場合、一時ファイルに保存
            import tempfile
            from PIL import Image, ImageOps
            temp_dir = os.path.join("temp", "temp_location_images")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, f"temp_{room_name}.png")
            img = Image.fromarray(image)
            img = ImageOps.exif_transpose(img) or img
            img.save(temp_path)
            image_path = temp_path

        gr.Info("🔄 情景テキストを生成中...")
        result = tlm.generate_from_image(room_name, image_path, api_key, user_hint=user_hint)

        if result and not result.startswith("（"):
            # [Fix] バックエンド側の現在地データも即座に更新する
            tlm.update_current(room_name, result, image_path=image_path)

            gr.Info("✅ 情景テキストの生成が完了しました")
            return gr.update(value=result), gr.update(value=result), gr.update(value=image_path or None)
        else:
            gr.Warning(f"情景テキストの生成に失敗しました: {result}")
            return gr.update(), gr.update(), gr.update()

    except Exception as e:
        logger.error(f"[TempLocation] 画像からの生成に失敗: {e}")
        traceback.print_exc()
        gr.Error(f"エラー: {e}")
        return gr.update(), gr.update(), gr.update()

def handle_apply_temp_scenery(room_name, text, image_path):
    """編集したテキストを一時的現在地データとして適用する"""
    if not room_name:
        gr.Warning("ルームが選択されていません")
        return gr.update()
    if not text or not text.strip():
        gr.Warning("情景テキストを入力してください")
        return gr.update()

    try:
        from agent.temporary_location_manager import TemporaryLocationManager
        tlm = TemporaryLocationManager()
        # [Fix] 画像パスも保持しながらテキストを更新する
        tlm.update_current(room_name, text.strip(), image_path=image_path)
        gr.Info("✅ 情景テキストを適用しました")
        return gr.update(value=text.strip())
    except Exception as e:
        logger.error(f"[TempLocation] テキスト適用に失敗: {e}")
        gr.Error(f"エラー: {e}")
        return gr.update()

def handle_save_temp_location(room_name, name):
    """一時的現在地を名前付きで保存する"""
    if not room_name:
        gr.Warning("ルームが選択されていません")
        return gr.update(), gr.update()
    if not name or not name.strip():
        gr.Warning("保存名を入力してください")
        return gr.update(), gr.update()

    try:
        from agent.temporary_location_manager import TemporaryLocationManager
        tlm = TemporaryLocationManager()
        success = tlm.save_location(room_name, name.strip())
        if success:
            gr.Info(f"✅ 「{name.strip()}」として保存しました")
            saved = tlm.list_saved_locations(room_name)
            return gr.update(value=f"保存しました: {name.strip()}"), gr.update(choices=saved, value=name.strip())
        else:
            gr.Warning("保存する情景データがありません。先にテキストを生成または入力してください。")
            return gr.update(value="保存する情景データがありません"), gr.update()
    except Exception as e:
        logger.error(f"[TempLocation] 保存に失敗: {e}")
        gr.Error(f"エラー: {e}")
        return gr.update(), gr.update()

def handle_load_temp_location(room_name, name):
    """保存済みの場所データをロードする"""
    if not room_name or not name:
        gr.Warning("ルームと場所名を選択してください")
        return gr.update(), gr.update(), gr.update()

    try:
        from agent.temporary_location_manager import TemporaryLocationManager
        tlm = TemporaryLocationManager()
        success = tlm.load_location(room_name, name)
        if success:
            data = tlm.get_current_data(room_name)
            scenery = data.get("scenery_text", "")
            image_path = data.get("image_path", None)
            gr.Info(f"✅ 「{name}」をロードしました")
            return gr.update(value=scenery), gr.update(value=scenery), gr.update(value=image_path or None)
        else:
            gr.Warning(f"「{name}」が見つかりません")
            return gr.update(), gr.update(), gr.update()
    except Exception as e:
        logger.error(f"[TempLocation] ロードに失敗: {e}")
        gr.Error(f"エラー: {e}")
        return gr.update(), gr.update(), gr.update()

def handle_delete_temp_location(room_name, name):
    """保存済みの場所データを削除する"""
    if not room_name or not name:
        gr.Warning("削除する場所名を選択してください")
        return gr.update(), gr.update(), gr.update()

    try:
        from agent.temporary_location_manager import TemporaryLocationManager
        tlm = TemporaryLocationManager()
        success = tlm.delete_location(room_name, name)
        if success:
            gr.Info(f"「{name}」を削除しました")
            saved = tlm.list_saved_locations(room_name)
            return gr.update(value=f"削除しました: {name}"), gr.update(choices=saved, value=None), gr.update(value=None)
        else:
            gr.Warning(f"「{name}」が見つかりません")
            return gr.update(), gr.update(), gr.update()
    except Exception as e:
        logger.error(f"[TempLocation] 削除に失敗: {e}")
        gr.Error(f"エラー: {e}")
        return gr.update(), gr.update(), gr.update()

def get_temp_location_ui_state(room_name):
    """一時的現在地のUI初期状態を取得する（ルーム変更時やロード時に使用）"""
    try:
        from agent.temporary_location_manager import TemporaryLocationManager
        tlm = TemporaryLocationManager()
        data = tlm.get_current_data(room_name)
        saved = tlm.list_saved_locations(room_name)
        active = tlm.is_active(room_name)

        scenery = data.get("scenery_text", "")
        image_path = data.get("image_path") or None

        # [Fix] Dropdown警告回避: choicesとvalueを明示的に更新
        saved_dropdown_update = gr.update(choices=saved, value=None)

        # [New] タブの選択状態
        selected_tab = "temp_location_tab" if active else "virtual_location_tab"
        tab_update = gr.update(selected=selected_tab)

        return scenery, saved_dropdown_update, image_path, tab_update
        return scenery, saved_dropdown_update, image_path, tab_update
    except Exception as e:
        logger.error(f"[TempLocation] UI状態取得エラー: {e}")
        return "", gr.update(choices=[], value=None), None, gr.update()

# ==========================================
# Twitter (X) 連携用ハンドラ
# ==========================================





































# --- [Doc Viewer] ---
def handle_open_user_guide():
    """ユーザーガイド全章を読み込み、モーダルを表示する。"""
    guide_dir = Path(_UI_HANDLERS_PROJECT_ROOT) / "docs" / "user_guide"
    guide_paths = sorted(path for path in guide_dir.glob("*.md") if path.name != "README.md")
    if not guide_paths:
        return gr.update(visible=True), f"ガイドファイルが見つかりません: {guide_dir}"

    try:
        sections = ["# Nexus Ark 使い方ガイド"]
        for path in guide_paths:
            sections.append(path.read_text(encoding="utf-8").strip())
        return gr.update(visible=True), "\n\n---\n\n".join(sections)
    except Exception as e:
        return gr.update(visible=True), f"ガイドの読み込みに失敗しました: {e}"


def handle_open_local_llm_guide():
    """
    ローカルLLM導入ガイドを読み込み、モーダルを表示する。
    """
    guide_path = os.path.join("assets", "guides", "local_llm_setup_guide.md")
    try:
        if not os.path.exists(guide_path):
             return gr.update(visible=True), f"ガイドファイルが見つかりません: {guide_path}"

        with open(guide_path, "r", encoding="utf-8") as f:
            content = f.read()
        return gr.update(visible=True), content
    except Exception as e:
        return gr.update(visible=True), f"ガイドの読み込みに失敗しました: {e}"

def handle_open_gcal_guide():
    """
    Googleカレンダー連携のセットアップガイドを読み込み、モーダルを表示する。
    """
    guide_path = os.path.join("assets", "guides", "google_calendar_setup_guide.md")
    try:
        if not os.path.exists(guide_path):
            return gr.update(visible=True), f"ガイドファイルが見つかりません: {guide_path}"

        with open(guide_path, "r", encoding="utf-8") as f:
            content = f.read()
        return gr.update(visible=True), content
    except Exception as e:
        return gr.update(visible=True), f"ガイドの読み込みに失敗しました: {e}"


def handle_open_explicit_cache_guide():
    """
    Gemini Explicit（明示）キャッシュの詳細解説を読み込み、モーダルを表示する。
    """
    guide_path = os.path.join("assets", "guides", "gemini_explicit_cache_guide.md")
    try:
        if not os.path.exists(guide_path):
            return gr.update(visible=True), f"ガイドファイルが見つかりません: {guide_path}"

        with open(guide_path, "r", encoding="utf-8") as f:
            content = f.read()
        return gr.update(visible=True), content
    except Exception as e:
        return gr.update(visible=True), f"ガイドの読み込みに失敗しました: {e}"


def handle_close_doc_viewer():
    """
    ドキュメントビューアーを閉じる。
    """
    return gr.update(visible=False)

# --- 自動バックアップ・復元ハンドラ ---
def _get_backup_dropdown_update(room_name: str):
    """復元ドロップダウンの選択肢を更新するヘルパー関数"""
    choices = room_manager.list_log_backups(room_name)
    return gr.update(choices=choices, value=None)

def handle_manual_backup(room_name: str):
    """今すぐバックアップボタン"""
    if not room_name:
        return gr.update(), "⚠️ ルームが選択されていません"

    result = room_manager.create_backup(room_name, 'log')
    now_str = datetime.datetime.now().strftime("%H:%M")
    if result:
        gr.Info("会話ログのバックアップを作成しました。")
        return _get_backup_dropdown_update(room_name), f"✅ バックアップ完了 ({now_str})"
    else:
        gr.Info("前回のバックアップから変更がないため、スキップしました。")
        return gr.update(), f"ℹ️ 変更なし・スキップ ({now_str})"

def handle_restore_from_backup(room_name: str, selected_backup: str):
    """選択されたバックアップからログを復元する"""
    if not selected_backup:
        gr.Warning("復元するバックアップを選択してください。")
        return gr.update(), "⚠️ 選択されていません"

    success = room_manager.restore_log_from_backup(room_name, selected_backup)
    if success:
        gr.Info("バックアップから会話ログを復元しました。ログ管理タブを再読込してください。")
        return _get_backup_dropdown_update(room_name), "✅ 復元成功（再読込推奨）"
    else:
        gr.Error("復元に失敗しました。")
        return gr.update(), "❌ 復元失敗"

def handle_refresh_backup_list(room_name: str):
    """復元ドロップダウンの選択肢を更新する"""
    return _get_backup_dropdown_update(room_name)

def handle_save_log_backup_rotation_count(count: int):
    """ログ専用バックアップ件数を保存する"""
    if count is None or not isinstance(count, (int, float)) or count < 1:
        gr.Warning("バックアップ保存件数は1以上の整数で指定してください。")
        return "共通設定: バックアップ保存件数は1以上の整数で指定してください"

    int_count = int(count)
    return handle_save_global_setting_delta("log_backup_rotation_count", int_count, f"会話ログのバックアップ保存件数 {int_count} 件", skip_grace=True)

def handle_periodic_backup_interval_change(interval_str: str):
    """定期バックアップ間隔を変更する"""
    try:
        interval = int(interval_str)
        label = f"定期バックアップ間隔 {interval // 3600}時間ごと" if interval > 0 else "定期バックアップ無効"
        return handle_save_global_setting_delta("periodic_backup_interval", interval, label, skip_grace=True)
    except ValueError:
        return "共通設定: 定期バックアップ間隔の保存に失敗しました"


# --- [新規] ユーザー用画像生成機能ハンドラ ---

def user_gen_reference_status_message(provider, model, profile_name=None):
    """ユーザー画像生成UI向けに、参照画像の反映方法を短く表示する。"""
    from tools.image_tools import REFERENCE_IMAGE_LIMIT, image_model_supports_reference

    provider = str(provider or "").strip()
    model = str(model or "").strip()

    if provider == "openai" and profile_name:
        settings_list = config_manager.get_openai_settings_list()
        target = next((s for s in settings_list if s.get("name") == profile_name), None)
        if target and "openrouter.ai" in target.get("base_url", "").lower():
            return (
                f"参照画像は最大{REFERENCE_IMAGE_LIMIT}枚まで指定できます。"
                "OpenRouterモデルでは対応時は実画像で、非対応時はキャプションとして反映されます。"
            )

    if image_model_supports_reference(provider, model):
        return f"✅ このモデルは参照画像に対応しています。最大{REFERENCE_IMAGE_LIMIT}枚まで実画像として反映されます。"

    return f"ℹ️ 参照画像は最大{REFERENCE_IMAGE_LIMIT}枚まで指定できます。このモデルではキャプションとしてプロンプトに反映されます。"


def handle_user_gen_reference_status_change(provider, model, profile_name=None):
    """プロバイダ・モデル変更時に参照画像の反映方法表示を更新する。"""
    return user_gen_reference_status_message(provider, model, profile_name)


def _dedupe_existing_paths(paths):
    result = []
    seen = set()
    for path in paths or []:
        path = str(path or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _resolve_scene_reference_image_path(room_name, scene_image_path=None):
    """ユーザー画像生成の情景参照用に、存在する情景画像パスを解決する。"""
    candidates = []
    if scene_image_path:
        candidates.append(scene_image_path)

    if room_name:
        try:
            room_config = room_manager.get_room_config(room_name) or {}
            config_path = room_config.get("last_sent_scenery_image")
            if config_path:
                candidates.append(config_path)
        except Exception:
            logger.exception("Failed to load last_sent_scenery_image for room '%s'", room_name)

    for path in candidates:
        path = str(path or "").strip()
        if path and os.path.exists(path):
            return path
    return None


def _extract_generated_image_path(result):
    match = re.search(r"\[Generated Image: (.*?)\]", str(result or ""), re.DOTALL)
    generated_path = match.group(1).strip() if match else None
    if generated_path and os.path.exists(generated_path):
        return generated_path
    return None


def handle_generate_item_image(prompt, room_name, api_key_name, reference_files=None):
    """共通画像生成設定でアイテム画像を生成し、画像入力欄へセットする。"""
    if not prompt or not str(prompt).strip():
        yield gr.update(), gr.update(value="【エラー】画像生成プロンプトを入力してください。", visible=True)
        return

    if not room_name:
        yield gr.update(), gr.update(value="【エラー】ルームを選択してください。", visible=True)
        return

    yield gr.update(), gr.update(value="⏳ アイテム画像を生成中...", visible=True)

    if not api_key_name:
        api_key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name") or config_manager.initial_api_key_name_global

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name, "")
    reference_image_paths = _dedupe_existing_paths(_normalize_file_paths(reference_files))

    try:
        from tools.image_tools import _generate_image_impl

        result = _generate_image_impl(
            prompt=str(prompt).strip(),
            room_name=room_name,
            api_key=api_key,
            api_key_name=api_key_name,
            save_subdir="item_generated_images",
            reference_image_paths=reference_image_paths or None,
        )
        generated_path = _extract_generated_image_path(result)
        if generated_path:
            yield generated_path, gr.update(value="✅ アイテム画像を生成しました。必要ならこのまま保存できます。", visible=True)
            return

        yield gr.update(), gr.update(value=f"【エラー】画像生成に失敗しました。\n{result}", visible=True)
    except Exception as e:
        traceback.print_exc()
        yield gr.update(), gr.update(value=f"【エラー】予期せぬエラーが発生しました: {str(e)}", visible=True)


def handle_generate_appearance_image(target, room_name, api_key_name, extra_prompt="", use_current_appearance=True):
    """共通画像生成設定で現在の姿を生成し、姿見へ永続保存する。"""
    if not room_name:
        yield gr.update(), "【エラー】ルームを選択してください。"
        return

    try:
        prompt = closet_manager.build_appearance_prompt(room_name, target, extra_prompt)
        reference_image_paths = closet_manager.collect_appearance_reference_images(
            room_name,
            target,
            include_current=bool(use_current_appearance),
        )
    except Exception as e:
        traceback.print_exc()
        yield gr.update(), f"【エラー】姿見設定の読み込みに失敗しました: {str(e)}"
        return

    if not prompt:
        label = "ユーザー" if str(target).lower() == "user" else "ペルソナ"
        yield gr.update(), f"【エラー】{label}の外見プロファイルが未設定、または有効になっていません。"
        return

    yield gr.update(), "⏳ 現在の姿を生成中..."

    if not api_key_name:
        api_key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name") or config_manager.initial_api_key_name_global

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name, "")

    try:
        from tools.image_tools import _generate_image_impl

        result = _generate_image_impl(
            prompt=prompt,
            room_name=room_name,
            api_key=api_key,
            api_key_name=api_key_name,
            save_subdir="appearance_preview",
            reference_image_paths=reference_image_paths or None,
        )
        generated_path = _extract_generated_image_path(result)
        if generated_path:
            closet_manager.save_current_appearance_image(room_name, target, generated_path)
            ref_note = f"参照画像 {len(reference_image_paths)} 枚を使用しました。" if reference_image_paths else "参照画像なしで生成しました。"
            yield generated_path, f"✅ 現在の姿を保存しました。{ref_note}"
            return

        yield gr.update(), f"【エラー】画像生成に失敗しました。\n{result}"
    except Exception as e:
        traceback.print_exc()
        yield gr.update(), f"【エラー】予期せぬエラーが発生しました: {str(e)}"


def load_current_appearance_ui(room_name: str, target: str):
    """Load the persistent mirror image and its freshness message."""
    if not room_name:
        return gr.update(value=None), "現在の姿: ルーム未選択"
    try:
        state = closet_manager.get_current_appearance_state(room_name, target)
        image_path = state.get("image_path") or None
        if not image_path:
            return gr.update(value=None), "現在の姿はまだありません。生成すると、ここに保存表示されます。"
        if state.get("needs_refresh"):
            return image_path, "⚠️ 外見または装いが変更されています。現在の姿を更新してください。"
        return image_path, "✅ 保存済みの現在の姿です。"
    except Exception as e:
        traceback.print_exc()
        return gr.update(value=None), f"【エラー】現在の姿を読み込めませんでした: {str(e)}"


def update_user_gen_model_choices(provider, profile_name):
    """プロバイダ変更時にモデルリストを更新する"""
    is_openai = (provider == "openai")

    if is_openai:
        profile_choices = config_manager.get_image_openai_profile_names()
        selected_profile = profile_name if profile_name in profile_choices else (profile_choices[0] if profile_choices else None)
        model_update, visibility_update, profile_state, reference_status = handle_user_gen_profile_change(selected_profile, None)
        return model_update, gr.update(choices=profile_choices, value=selected_profile, visible=True), visibility_update, profile_state, reference_status

    models = config_manager.CONFIG_GLOBAL.get("available_image_models", {}).get(provider, [])
    selected_model = models[0] if models else None

    # モデルの選択肢を更新し、可視性を制御
    return (
        gr.update(choices=models, value=selected_model),
        gr.update(visible=False),
        gr.update(visible=False),
        None,
        user_gen_reference_status_message(provider, selected_model, None),
    )

def handle_user_gen_profile_change(profile_name, current_profile_state=None):
    """OpenAI互換プロファイル変更時にモデルリストを更新する"""
    if profile_name == current_profile_state:
        return gr.update(), gr.update(), current_profile_state, gr.update()

    is_openrouter = False
    if profile_name:
        settings_list = config_manager.get_openai_settings_list()
        target = next((s for s in settings_list if s["name"] == profile_name), None)
        if target and config_manager.is_pollinations_openai_profile(target):
            profile_name = None
            target = None
        if target and "openrouter.ai" in target.get("base_url", "").lower():
            is_openrouter = True

    if not profile_name:
        models = config_manager.CONFIG_GLOBAL.get("available_image_models", {}).get("openai", [])
        selected_model = models[0] if models else None
        return (
            gr.update(choices=models, value=selected_model),
            gr.update(visible=is_openrouter),
            None,
            user_gen_reference_status_message("openai", selected_model, None),
        )

    # プロファイル専用のモデルリストがあるか確認
    models = config_manager.get_image_models_for_openai_profile(profile_name)
    if models:
        selected_model = models[0]
        return (
            gr.update(choices=models, value=selected_model),
            gr.update(visible=is_openrouter),
            profile_name,
            user_gen_reference_status_message("openai", selected_model, profile_name),
        )

    # デフォルトの OpenAI 画像モデル
    models = config_manager.CONFIG_GLOBAL.get("available_image_models", {}).get("openai", [])
    selected_model = models[0] if models else None

    return (
        gr.update(choices=models, value=selected_model),
        gr.update(visible=is_openrouter),
        profile_name,
        user_gen_reference_status_message("openai", selected_model, profile_name),
    )

def handle_user_generate_image(
    prompt,
    provider,
    model,
    profile_name,
    room_name,
    api_key_name,
    reference_files=None,
    use_scene_reference=False,
    scene_image_path=None,
):
    """ユーザー指定のパラメータで画像を生成する"""
    if not prompt or not prompt.strip():
        yield gr.update(), gr.update(), gr.update(), "【エラー】プロンプトを入力してください。"
        return

    if not room_name:
        yield gr.update(), gr.update(), gr.update(), "【エラー】ルームを選択してください。"
        return

    # 生成中のステータスを即座にUIへ反映する
    yield gr.update(), gr.update(), gr.update(), "⏳ 画像生成中..."

    # api_key_name が未指定の場合は現在の設定から取得
    if not api_key_name:
        api_key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name")

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name, "")
    reference_image_paths = _dedupe_existing_paths(_normalize_file_paths(reference_files))
    if use_scene_reference:
        scene_reference_path = _resolve_scene_reference_image_path(room_name, scene_image_path)
        if scene_reference_path:
            reference_image_paths = _dedupe_existing_paths(reference_image_paths + [scene_reference_path])

    try:
        from tools.image_tools import _generate_image_impl

        result = _generate_image_impl(
            prompt=prompt,
            room_name=room_name,
            api_key=api_key,
            api_key_name=api_key_name,
            provider=provider,
            model_name=model,
            openai_profile_name=profile_name if provider == "openai" else None,
            save_subdir="user_generated_images",
            reference_image_paths=reference_image_paths or None,
        )

        if "[Generated Image:" in result:
            generated_path = _extract_generated_image_path(result)

            if generated_path:
                # 生成に成功した場合は、プレビュー画像を表示し、添付ボタンを有効化する
                yield (
                    generated_path,
                    gr.update(visible=True, value=generated_path),
                    gr.update(visible=True),
                    "✅ 画像生成に成功しました。"
                )
                return

        yield gr.update(), gr.update(), gr.update(), f"【エラー】生成に失敗しました。\n{result}"

    except Exception as e:
        traceback.print_exc()
        yield gr.update(), gr.update(), gr.update(), f"【エラー】予期せぬエラーが発生しました: {str(e)}"

def handle_attach_generated_image_to_chat(image_path, current_input):
    """生成された画像をチャット入力欄に添付する"""
    if not image_path:
        gr.Warning("添付する画像がありません。先に画像を生成してください。")
        return current_input

    if current_input is None:
        current_input = {"text": "", "files": []}

    # GradioのMultimodalTextboxの値は辞書形式 {"text": str, "files": List[str]}
    files = current_input.get("files", [])
    if image_path not in files:
        files.append(image_path)
        gr.Info("画像をチャットに添付しました。")
    else:
        gr.Info("この画像は既に添付されています。")

    current_input["files"] = files
    return current_input

def handle_fetch_gemini_models(api_key_name, current_model=None, free_only: bool = False):
    """Gemini API からテキストモデルリストを取得して更新する"""
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name, "")
    if not api_key:
        gr.Warning("APIキーが選択されていません。")
        return gr.update()

    models = config_manager.fetch_gemini_models(api_key, free_only=free_only, exclude_special=True)
    if not models:
        gr.Warning("Gemini モデルリストを取得できませんでした。APIキーが正しいか、ネットワーク設定を確認してください。")
        return gr.update()

    # グローバルなモデルリストを更新
    config_manager.AVAILABLE_MODELS_GLOBAL = models
    config_manager.save_config_if_changed("available_models", models)

    gr.Info(f"Gemini の最新モデルリストを取得しました（{len(models)}件）。")

    # 現在のモデルが新しいリストに含まれていれば、それを維持する
    new_value = current_model if current_model in models else (models[0] if models else None)
    return gr.update(choices=models, value=new_value)

def handle_fetch_image_models(provider, profile_name, free_only: bool = False):
    """画像モデルリストをAPIから取得して更新する"""
    base_url = ""
    api_key = ""

    if provider == "openai":
        # プロファイルから情報を取得
        profiles = config_manager.CONFIG_GLOBAL.get("openai_provider_settings", [])
        target_profile = next((p for p in profiles if p.get("name") == profile_name), None)
        if target_profile:
            base_url = target_profile.get("base_url", "")
            api_key = target_profile.get("api_key", "")
    elif provider == "gemini":
        # 画像生成設定のキー、または最後に使ったキーから実体を取得
        key_name = config_manager.CONFIG_GLOBAL.get("image_generation_api_key_name", "")
        if not key_name:
            key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name", "")
        api_key = config_manager.GEMINI_API_KEYS.get(key_name, "")

    # config_manager の関数を呼び出し
    models = config_manager.fetch_image_models(provider, base_url, api_key, free_only=free_only)

    if not models:
        # Hugging Face は現時点で動的な取得に未対応
        if provider == "huggingface":
            gr.Info(f"{provider} は現在、動的なモデルリスト取得に対応していません。")
        else:
            gr.Warning(f"{provider} からモデルリストを取得できませんでした。ネットワークやAPIキーの設定を確認してください。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    # configに保存
    if provider == "openai":
        # プロファイル個別のリストとして保存
        config_manager.save_image_models_for_openai_profile(profile_name, models)
    else:
        # プロバイダ全体のリストとして保存
        available_image_models = config_manager.CONFIG_GLOBAL.get("available_image_models", {})
        available_image_models[provider] = models
        config_manager.save_config_if_changed("available_image_models", available_image_models)

    gr.Info(f"{provider} の最新モデルリストを取得しました（{len(models)}件）。")

    # 該当するプロバイダのDropdownを更新するための出力を構築
    # UI側の outputs 順序に合わせる必要がある
    update = gr.update(choices=models, value=models[0] if models else None)

    # プロバイダに応じてどのDropdownを更新するか
    # 戻り値: [gemini_image_model_dropdown, openai_image_model_dropdown, pollinations_image_model_dropdown, huggingface_image_model_dropdown, user_gen_image_model]
    return (
        update if provider == "gemini" else gr.update(),
        update if provider == "openai" else gr.update(),
        update if provider == "pollinations" else gr.update(),
        update if provider == "huggingface" else gr.update(),
        update # ユーザー用UIのモデルリストも、現在取得したプロバイダのもので更新する
    )

# --- [拡張ツール管理ハンドラ] ---

CUSTOM_TOOL_RESULT_PROMPT_DEFAULT = (
    "実行結果を踏まえて、必要な場合は相手に自然に報告してください。"
    "失敗や不足がある場合は隠さず、次に必要な確認事項を伝えてください。"
)

def _df_to_rows(df_data) -> List[List[Any]]:
    if df_data is None:
        return []
    if isinstance(df_data, pd.DataFrame):
        return df_data.values.tolist()
    if isinstance(df_data, list):
        return df_data
    return []

def _custom_tool_metadata_key(source: str, source_name: str, tool_name: str) -> str:
    try:
        from custom_tool_manager import CustomToolManager
        return CustomToolManager.metadata_key(source, source_name, tool_name)
    except Exception:
        return f"{source}:{source_name}:{tool_name}"

def _get_custom_tool_metadata(source: str, source_name: str, tool_name: str, description: str = "") -> Dict[str, Any]:
    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    metadata_map = settings.get("tool_metadata", {})
    key = _custom_tool_metadata_key(source, source_name, tool_name)
    meta = dict(metadata_map.get(key, {})) if isinstance(metadata_map.get(key), dict) else {}
    summary = meta.get("summary") or " ".join(str(description or "").split())[:180]
    return {
        "summary": summary,
        "use_when": meta.get("use_when", ""),
        "result_prompt": meta.get("result_prompt") or CUSTOM_TOOL_RESULT_PROMPT_DEFAULT,
    }

def _save_custom_tool_metadata(source: str, source_name: str, tool_name: str, summary: str, use_when: str, result_prompt: str):
    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    metadata_map = dict(settings.get("tool_metadata", {}))
    key = _custom_tool_metadata_key(source, source_name, tool_name)
    metadata_map[key] = {
        "summary": (summary or "").strip(),
        "use_when": (use_when or "").strip(),
        "result_prompt": (result_prompt or "").strip() or CUSTOM_TOOL_RESULT_PROMPT_DEFAULT,
    }
    settings["tool_metadata"] = metadata_map
    config_manager.save_config_if_changed("custom_tools_settings", settings)

def _mcp_tool_metadata_for_row(server_name: str, tool_name: str, description: str) -> Dict[str, Any]:
    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    mcp_servers = settings.get("mcp_servers", [])
    server_conf = next((s for s in mcp_servers if s.get("name") == server_name), {})
    server_meta = server_conf.get("tool_metadata", {})
    key_meta = _get_custom_tool_metadata("mcp", server_name, tool_name, description)
    if isinstance(server_meta.get(tool_name), dict):
        merged = dict(key_meta)
        merged.update(server_meta[tool_name])
        return {
            "summary": merged.get("summary") or key_meta["summary"],
            "use_when": merged.get("use_when", ""),
            "result_prompt": merged.get("result_prompt") or CUSTOM_TOOL_RESULT_PROMPT_DEFAULT,
        }
    return key_meta

def handle_refresh_custom_tools():
    """custom_tools/ 内の全ファイルをスキャンし、有効状態を含めて Dataframe 用のデータを返す"""
    plugin_dir = "custom_tools"
    if not os.path.exists(plugin_dir):
        return [[]]

    # 最新の設定をファイルから直接ロード
    config = config_manager.load_config_file()
    settings = config.get("custom_tools_settings", {})
    disabled_plugins = settings.get("disabled_local_plugins", [])
    metadata_map = settings.get("tool_metadata", {})

    # システムツールを除外
    EXCLUDED_PLUGINS = ["__init__.py", "persona_developer.py"]
    files = [f for f in os.listdir(plugin_dir) if f.endswith(".py") and f not in EXCLUDED_PLUGINS]
    files.sort()

    data = []
    for filename in files:
        is_enabled = filename not in disabled_plugins

        found_tool = False
        try:
            import importlib.util
            module_name = filename[:-3]
            file_path = os.path.abspath(os.path.join(plugin_dir, filename))
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if hasattr(attr, "name") and hasattr(attr, "description"):
                        found_tool = True
                        tool_name = attr.name
                        desc = attr.description or ""
                        key = _custom_tool_metadata_key("local_plugin", filename, tool_name)
                        meta = dict(metadata_map.get(key, {})) if isinstance(metadata_map.get(key), dict) else {}
                        module_meta = getattr(module, "NEXUS_TOOL_METADATA", {})
                        if isinstance(module_meta, dict):
                            if isinstance(module_meta.get(tool_name), dict):
                                base = dict(module_meta[tool_name])
                                base.update(meta)
                                meta = base
                            elif any(k in module_meta for k in ("summary", "use_when", "result_prompt")):
                                base = dict(module_meta)
                                base.update(meta)
                                meta = base
                        summary = meta.get("summary") or " ".join(str(desc).split())[:180]
                        use_when = meta.get("use_when", "")
                        result_prompt = meta.get("result_prompt") or CUSTOM_TOOL_RESULT_PROMPT_DEFAULT
                        data.append([is_enabled, filename, tool_name, desc, summary, use_when, result_prompt])
        except:
            data.append([is_enabled, filename, "", "(ロード失敗)", "", "", CUSTOM_TOOL_RESULT_PROMPT_DEFAULT])

        if not found_tool and not any(row[1] == filename for row in data):
            data.append([is_enabled, filename, "", "(ツール未検出)", "", "", CUSTOM_TOOL_RESULT_PROMPT_DEFAULT])

    return data

def handle_local_tool_select(evt: gr.SelectData, df: pd.DataFrame):
    """プラグイン一覧での行選択時にファイル名をドロップダウンに反映する"""
    if evt.index is None:
        return gr.update()
    row_idx = evt.index[0]
    # 2列目（インデックス1）がファイル名
    filename = df.iloc[row_idx, 1]
    return gr.update(value=filename)

def handle_custom_tools_enabled_change(enabled: bool):
    """拡張ツールの有効/無効設定を保存する"""
    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    settings["enabled"] = enabled
    config_manager.save_config_if_changed("custom_tools_settings", settings)

    from custom_tool_manager import CustomToolManager
    CustomToolManager.clear_mcp_cache()

    status = "有効" if enabled else "無効"
    gr.Info(f"拡張ツール機能を{status}にしました。")

def handle_local_tools_df_change(df_data):
    """ローカルプラグイン一覧の有効状態とメタデータ編集を保存する"""
    rows = _df_to_rows(df_data)
    if not rows:
        return

    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    disabled_plugins = set(settings.get("disabled_local_plugins", []))
    file_enabled: Dict[str, bool] = {}

    for row in rows:
        if len(row) < 7:
            continue
        enabled, filename, tool_name, _desc, summary, use_when, result_prompt = row[:7]
        if not filename:
            continue
        file_enabled[filename] = file_enabled.get(filename, False) or bool(enabled)
        if tool_name:
            _save_custom_tool_metadata("local_plugin", filename, tool_name, summary, use_when, result_prompt)

    for filename, enabled in file_enabled.items():
        if enabled:
            disabled_plugins.discard(filename)
        else:
            disabled_plugins.add(filename)

    settings["disabled_local_plugins"] = sorted(disabled_plugins)
    config_manager.save_config_if_changed("custom_tools_settings", settings)

    from custom_tool_manager import CustomToolManager
    CustomToolManager.clear_mcp_cache()

def handle_mcp_type_change(server_type):
    """MCPサーバ種別変更時にUIフィールドの表示を切り替える"""
    if server_type == "stdio":
        return (
            gr.update(label="コマンド", placeholder="python"),
            gr.update(visible=True),  # 引数欄を表示
        )
    else:  # sse, streamable_http
        return (
            gr.update(label="URL", placeholder="http://localhost:8000/mcp"),
            gr.update(visible=False),  # 引数欄を隠す
        )

def handle_refresh_mcp_servers_lite():
    """軽量UI向けにMCPサーバ一覧をDataframeへ読み込む。"""
    config = config_manager.load_config_file()
    settings = config.get("custom_tools_settings", {})
    mcp_servers = settings.get("mcp_servers", [])
    rows = [
        [
            server.get("enabled", True),
            server.get("name", ""),
            server.get("type", "stdio"),
            server.get("command") or server.get("url", ""),
            " ".join(server.get("args", [])),
            "未接続",
        ]
        for server in mcp_servers
    ]
    return gr.update(value=rows)

def handle_add_mcp_server(name, server_type, cmd_url, args_str, enabled):
    """MCPサーバを新規登録する"""
    if not name or not cmd_url:
        return gr.update(), "⚠️ 名前とコマンド/URLは必須です。"

    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    mcp_servers = settings.get("mcp_servers", [])

    existing_server = next((s for s in mcp_servers if s.get("name") == name), {})
    new_server = {
        "enabled": enabled,
        "name": name,
        "type": server_type,
        "args": args_str.split() if args_str else [],
        "disabled_tools": existing_server.get("disabled_tools", []),
        "tool_metadata": existing_server.get("tool_metadata", {}),
    }
    if server_type == "stdio":
        new_server["command"] = cmd_url
    else:  # sse, streamable_http
        new_server["url"] = cmd_url

    # 既存の同名サーバがあれば削除（上書き）
    mcp_servers = [s for s in mcp_servers if s.get("name") != name]
    mcp_servers.append(new_server)
    settings["mcp_servers"] = mcp_servers
    config_manager.save_config_if_changed("custom_tools_settings", settings)

    from custom_tool_manager import CustomToolManager
    CustomToolManager.clear_mcp_cache()

    # Dataframe表示用に整形
    df_data = [[s.get("enabled", True), s.get("name"), s.get("type"), s.get("command") or s.get("url"), " ".join(s.get("args", [])), "未接続"] for s in mcp_servers]

    return _ensure_output_count((df_data, f"✅ サーバ '{name}' を追加/更新しました。"), 2)

def handle_mcp_servers_df_change(df_data: List[List[Any]]):
    """MCPサーバ一覧（Dataframe）での変更（特に「有効」チェックボックス）を保存する"""
    rows = _df_to_rows(df_data)
    if not rows:
        return

    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    mcp_servers = settings.get("mcp_servers", [])

    # Dataframeの各行をループ
    for row in rows:
        enabled, name, *_ = row
        # 該当するサーバ設定を探して「enabled」を更新
        for server in mcp_servers:
            if server.get("name") == name:
                server["enabled"] = bool(enabled)
                break

    settings["mcp_servers"] = mcp_servers
    config_manager.save_config_if_changed("custom_tools_settings", settings)

    from custom_tool_manager import CustomToolManager
    CustomToolManager.clear_mcp_cache()

def handle_mcp_server_select(evt: gr.SelectData, df: pd.DataFrame):
    """Dataframe の行選択時にその行のデータを取得する"""
    if evt.index is None:
        return None
    row_idx = evt.index[0]
    # 行全体を辞書として返す
    return df.iloc[row_idx].to_dict()

def handle_edit_mcp_server(selected_info: Dict[str, Any]):
    """選択されたサーバの情報を入力欄に反映する"""
    if selected_info is None:
        gr.Warning("編集するサーバを一覧から選択してください。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    return (
        selected_info.get("名前", ""),
        selected_info.get("種別", "stdio"),
        selected_info.get("コマンド/URL", ""),
        selected_info.get("引数", ""),
        selected_info.get("有効", True)
    )

def handle_remove_mcp_server(selected_info: Dict[str, Any]):
    """選択されたMCPサーバを削除する"""
    if selected_info is None:
        gr.Warning("削除するサーバを一覧から選択してください。")
        return gr.update()

    target_name = selected_info.get("名前")

    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    mcp_servers = settings.get("mcp_servers", [])

    new_mcp_servers = [s for s in mcp_servers if s.get("name") != target_name]

    if len(mcp_servers) == len(new_mcp_servers):
        return gr.update()

    settings["mcp_servers"] = new_mcp_servers
    config_manager.save_config_if_changed("custom_tools_settings", settings)

    from custom_tool_manager import CustomToolManager
    CustomToolManager.clear_mcp_cache()

    df_data = [[s.get("enabled", True), s.get("name"), s.get("type"), s.get("command") or s.get("url"), " ".join(s.get("args", [])), "未接続"] for s in new_mcp_servers]
    gr.Info(f"サーバ '{target_name}' を削除しました。")
    return _ensure_output_count((df_data,), 1)[0]

# --- ローカルプラグインエディタ用ハンドラ ---

def handle_refresh_local_plugin_files():
    """custom_tools/ 内の .py ファイルリストを取得する"""
    plugin_dir = "custom_tools"
    if not os.path.exists(plugin_dir):
        return gr.update(choices=[], value=None)

    # システムツールを除外
    EXCLUDED_PLUGINS = ["__init__.py", "persona_developer.py"]
    files = [f for f in os.listdir(plugin_dir) if f.endswith(".py") and f not in EXCLUDED_PLUGINS]
    files.sort()
    return gr.update(choices=files)

def handle_load_plugin_code(filename):
    """選択されたファイルのソースコードと有効状態を読み込む"""
    if not filename:
        return gr.update(value=""), gr.update(value=True)

    file_path = os.path.join("custom_tools", filename)
    code = ""
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
        except Exception as e:
            code = f"Error reading file: {e}"
    else:
        code = f"Error: File '{filename}' not found."

    # 有効状態を取得
    config = config_manager.load_config_file()
    settings = config.get("custom_tools_settings", {})
    disabled_plugins = settings.get("disabled_local_plugins", [])
    is_enabled = filename not in disabled_plugins

    return gr.update(value=code), gr.update(value=is_enabled)

def handle_save_plugin_code(filename, code, enabled):
    """ソースコードと有効状態を保存する（保存前に構文チェックと依存関係解決を実行）"""
    if not filename:
        return "⚠️ ファイルを選択してください。", gr.update()

    from custom_tool_manager import CustomToolManager

    # 1. 構文チェック
    is_valid, err_msg = CustomToolManager.validate_code(code)
    if not is_valid:
        return f"❌ 構文エラーがあります。修正してください:\n{err_msg}", gr.update()

    # 2. 依存関係のチェックとインストール
    deps = CustomToolManager.get_dependencies(code)
    if deps:
        success, dep_msg = CustomToolManager.install_dependencies(deps)
        if not success:
            return f"❌ 依存関係のインストールに失敗しました:\n{dep_msg}", gr.update()
        print(f"--- [Plugin Editor] {dep_msg} ---")

    # 3. 有効状態を保存
    config = config_manager.load_config_file()
    settings = dict(config.get("custom_tools_settings", {}))
    disabled_plugins = list(settings.get("disabled_local_plugins", []))

    if enabled:
        if filename in disabled_plugins:
            disabled_plugins.remove(filename)
    else:
        if filename not in disabled_plugins:
            disabled_plugins.append(filename)

    settings["disabled_local_plugins"] = disabled_plugins
    config_manager.save_config_if_changed("custom_tools_settings", settings)

    # 4. ファイル保存
    file_path = os.path.join("custom_tools", filename)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        # 保存後にキャッシュをクリアしてAIが即座に認識できるようにする
        CustomToolManager.clear_mcp_cache()

        res_msg = f"✅ '{filename}' を保存し、設定を更新しました。"
        if deps:
            res_msg += f" (依存関係: {', '.join(deps)} を確認済み)"

        # 更新された一覧データを取得
        df_data = handle_refresh_custom_tools()

        return res_msg, gr.update(value=df_data)
    except Exception as e:
        return f"❌ 保存失敗: {e}", gr.update()

def handle_save_plugin_code_lite(filename, code, enabled):
    """軽量UI向けにプラグイン保存結果とファイル一覧だけを返す。"""
    status, _ = handle_save_plugin_code(filename, code, enabled)
    return status, handle_refresh_local_plugin_files()

def handle_create_new_plugin(filename):
    """新しいプラグインファイルを作成する"""
    if not filename:
        return gr.update(), "⚠️ ファイル名を入力してください。"

    if not filename.endswith(".py"):
        filename += ".py"

    file_path = os.path.join("custom_tools", filename)
    if os.path.exists(file_path):
        return gr.update(), f"⚠️ ファイル '{filename}' は既に存在します。"

    template = '''from langchain_core.tools import tool

NEXUS_TOOL_METADATA = {
    "my_new_tool": {
        "summary": "短く何ができるツールかを書きます。",
        "use_when": "このツールをペルソナAIに積極的に使ってほしい場面を書きます。",
        "result_prompt": "ツール実行後、結果をどう扱うかを書きます。例: 要点をユーザーに報告し、必要なら次の確認事項を尋ねてください。"
    }
}

@tool
def my_new_tool(query: str):
    """ここにツールの説明を記述します。AIはこの説明を読んで使い方を理解します。"""
    return f"Hello! You searched for: {query}"
'''
    try:
        os.makedirs("custom_tools", exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(template)

        # リストを更新
        choices_update = handle_refresh_local_plugin_files()
        return choices_update, f"✅ '{filename}' を作成しました。"
    except Exception as e:
        return gr.update(), f"❌ 作成失敗: {e}"

def handle_delete_plugin(filename):
    """プラグインファイルを削除する"""
    if not filename:
        return gr.update(), "⚠️ 削除するファイルを選択してください。"

    file_path = os.path.join("custom_tools", filename)
    try:
        if os.path.exists(file_path):
            os.remove(file_path)

            from custom_tool_manager import CustomToolManager
            CustomToolManager.clear_mcp_cache()

            # リストを更新
            choices_update = handle_refresh_local_plugin_files()
            return choices_update, f"🗑️ '{filename}' を削除しました。"
        else:
            return gr.update(), f"⚠️ ファイル '{filename}' が見つかりません。"
    except Exception as e:
        return gr.update(), f"❌ 削除失敗: {e}"

def handle_toggle_mcp_tool(server_name, tool_name, enabled):
    """特定のMCPサーバ内のツールの有効/無効を切り替える"""
    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    mcp_servers = settings.get("mcp_servers", [])

    found = False
    for server in mcp_servers:
        if server.get("name") == server_name:
            disabled_tools = server.get("disabled_tools", [])
            if enabled:
                if tool_name in disabled_tools:
                    disabled_tools.remove(tool_name)
            else:
                if tool_name not in disabled_tools:
                    disabled_tools.append(tool_name)
            server["disabled_tools"] = disabled_tools
            found = True
            break

    if found:
        config_manager.save_config_if_changed("custom_tools_settings", settings)
        from custom_tool_manager import CustomToolManager
        CustomToolManager.clear_mcp_cache()
        status = "有効" if enabled else "無効"
        return f"✅ ツール '{tool_name}' を{status}に設定しました。"
    return "⚠️ 設定の保存に失敗しました。"

async def handle_test_mcp_connection(selected_info: Dict[str, Any]):
    """MCPサーバへの接続テストを行う (非同期)"""
    if selected_info is None:
        return "⚠️ テストするサーバを一覧から選択してください。", gr.update()

    name = selected_info.get("名前", "")

    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    mcp_servers = settings.get("mcp_servers", [])
    server_conf = next((s for s in mcp_servers if s.get("name") == name), None)

    if not server_conf:
        return f"⚠️ サーバ '{name}' の設定が見つかりません。", gr.update()

    try:
        from mcp import ClientSession

        server_type = server_conf.get("type", "stdio")

        if server_type == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            # Nexus Ark のルートディレクトリを基準にする
            base_dir = _UI_HANDLERS_PROJECT_ROOT

            # コマンドが python 系なら、Nexus Ark と同じ Python を使用する
            command = server_conf["command"]
            if os.path.basename(command).rstrip("0123456789.") in ("python", ""):
                # symlink を解決すると venv 環境が壊れる場合があるためそのまま使用
                command = sys.executable

            # 引数内の相対パスを絶対パスに変換
            resolved_args = []
            for arg in server_conf.get("args", []):
                candidate = os.path.join(base_dir, arg)
                if os.path.exists(candidate):
                    resolved_args.append(os.path.abspath(candidate))
                else:
                    resolved_args.append(arg)

            # venv の site-packages を子プロセスで認識できるよう環境変数を設定
            env = os.environ.copy()
            venv_dir = os.path.join(base_dir, ".venv")
            if os.path.isdir(venv_dir):
                env["VIRTUAL_ENV"] = venv_dir
                env["PATH"] = os.path.join(venv_dir, "bin") + os.pathsep + env.get("PATH", "")

            params = StdioServerParameters(
                command=command,
                args=resolved_args,
                env=env,
                cwd=base_dir
            )

            # デバッグ: 実際に実行するコマンドをログ出力
            print(f"  - [MCP接続テスト] command={command}, args={resolved_args}, cwd={base_dir}")

            # タイムアウト付きで接続試行
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=10.0)
                    tools_resp = await session.list_tools()

                    disabled_tools = server_conf.get("disabled_tools", [])
                    df_data = []
                    for t in tools_resp.tools:
                        is_enabled = t.name not in disabled_tools
                        meta = _mcp_tool_metadata_for_row(name, t.name, t.description or "")
                        df_data.append([is_enabled, t.name, t.description or "", meta["summary"], meta["use_when"], meta["result_prompt"]])

                    status = f"✅ 接続成功！ {len(tools_resp.tools)} 個のツールを検出しました。"
                    return _ensure_output_count((status, df_data), 2)

        elif server_type == "streamable_http":
            from mcp.client.streamable_http import streamable_http_client

            url = server_conf.get("url", "")
            if not url:
                return "⚠️ URL が指定されていません。", gr.update()

            print(f"  - [MCP接続テスト] Streamable HTTP: url={url}")

            async with streamable_http_client(url) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=15.0)
                    tools_resp = await session.list_tools()

                    disabled_tools = server_conf.get("disabled_tools", [])
                    df_data = []
                    for t in tools_resp.tools:
                        is_enabled = t.name not in disabled_tools
                        meta = _mcp_tool_metadata_for_row(name, t.name, t.description or "")
                        df_data.append([is_enabled, t.name, t.description or "", meta["summary"], meta["use_when"], meta["result_prompt"]])

                    status = f"✅ Streamable HTTP 接続成功！ {len(tools_resp.tools)} 個のツールを検出しました。"
                    return _ensure_output_count((status, df_data), 2)

        elif server_type == "sse":
            from mcp.client.sse import sse_client

            url = server_conf.get("url", "")
            if not url:
                return "⚠️ URL が指定されていません。", gr.update()

            print(f"  - [MCP接続テスト] SSE: url={url}")

            async with sse_client(url) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=15.0)
                    tools_resp = await session.list_tools()

                    disabled_tools = server_conf.get("disabled_tools", [])
                    df_data = []
                    for t in tools_resp.tools:
                        is_enabled = t.name not in disabled_tools
                        meta = _mcp_tool_metadata_for_row(name, t.name, t.description or "")
                        df_data.append([is_enabled, t.name, t.description or "", meta["summary"], meta["use_when"], meta["result_prompt"]])

                    status = f"✅ SSE 接続成功！ {len(tools_resp.tools)} 個のツールを検出しました。"
                    return _ensure_output_count((status, df_data), 2)

        elif server_type == "simple_http":
            import httpx
            url = server_conf.get("url", "")
            if not url:
                return "⚠️ URL が指定されていません。", gr.update()

            print(f"  - [MCP接続テスト] Simple HTTP (JSON-RPC): url={url}")

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {}
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
                resp.raise_for_status()
                data = resp.json()

                if "result" in data and "tools" in data["result"]:
                    tools = data["result"]["tools"]
                    disabled_tools = server_conf.get("disabled_tools", [])
                    df_data = []
                    for t in tools:
                        t_name = t.get("name", "")
                        t_desc = t.get("description", "")
                        is_enabled = t_name not in disabled_tools
                        meta = _mcp_tool_metadata_for_row(name, t_name, t_desc)
                        df_data.append([is_enabled, t_name, t_desc, meta["summary"], meta["use_when"], meta["result_prompt"]])

                    status = f"✅ Simple HTTP 接続成功！ {len(tools)} 個のツールを検出しました。"
                    return _ensure_output_count((status, df_data), 2)
                else:
                    return f"❌ 接続成功しましたが、無効なレスポンス形式です: {data}", gr.update()

        else:
            return f"⚠️ 未対応のトランスポート種別: {server_type}", gr.update()

    except Exception as e:
        traceback.print_exc()
        hint = ""
        err_str = str(e)
        if "null bytes" in err_str or "ELF" in err_str:
            hint = "\n💡 ヒント: 「コマンド」欄にはバイナリのフルパスではなく、`python3` や `uv run python3` のようなコマンド名を入力してください。"
        elif "No such file" in err_str:
            hint = "\n💡 ヒント: コマンドまたは引数のパスが見つかりません。パスが正しいか確認してください。"
        return f"❌ 接続失敗: {err_str}{hint}", gr.update()

def handle_mcp_tools_config_change(df_data, selected_info):
    """MCPツールの個別有効/無効設定を保存する"""
    rows = _df_to_rows(df_data)
    if not selected_info or not rows:
        return

    server_name = selected_info.get("名前")
    if not server_name:
        return

    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    mcp_servers = settings.get("mcp_servers", [])

    for server in mcp_servers:
        if server.get("name") == server_name:
            disabled_tools = []
            tool_metadata = dict(server.get("tool_metadata", {}))
            for row in rows:
                if len(row) < 3:
                    continue
                enabled = row[0]
                tool_name = row[1]
                if not enabled:
                    disabled_tools.append(tool_name)
                summary = row[3] if len(row) > 3 else ""
                use_when = row[4] if len(row) > 4 else ""
                result_prompt = row[5] if len(row) > 5 else ""
                if tool_name:
                    tool_metadata[tool_name] = {
                        "summary": (summary or "").strip(),
                        "use_when": (use_when or "").strip(),
                        "result_prompt": (result_prompt or "").strip() or CUSTOM_TOOL_RESULT_PROMPT_DEFAULT,
                    }
                    _save_custom_tool_metadata("mcp", server_name, tool_name, summary, use_when, result_prompt)
            server["disabled_tools"] = disabled_tools
            server["tool_metadata"] = tool_metadata
            break

    config_manager.save_config_if_changed("custom_tools_settings", settings)
    from custom_tool_manager import CustomToolManager
    CustomToolManager.clear_mcp_cache()

# --- AIプロンプト生成補助ハンドラ ---

def handle_user_gen_instruction_select(selected_name):
    """テンプレート選択時の処理。"""
    templates = config_manager.CONFIG_GLOBAL.get("user_image_gen_instruction_templates", [])
    for i, t in enumerate(templates):
        if t["name"] == selected_name:
            # 選択中のインデックスを保存
            config_manager.save_config_if_changed("user_image_gen_selected_template_index", i)
            return t["instruction"], t["name"]
    return gr.update(), gr.update()

def handle_user_gen_instruction_save(target_name, instruction):
    """テンプレートの保存処理。"""
    if not target_name:
        return gr.update(), gr.update(), "⚠️ 名前を入力してください。"

    templates = config_manager.CONFIG_GLOBAL.get("user_image_gen_instruction_templates", [])
    new_templates = [t.copy() for t in templates]

    found = False
    for i, t in enumerate(new_templates):
        if t["name"] == target_name:
            new_templates[i]["instruction"] = instruction
            found = True
            break

    if not found:
        new_templates.append({"name": target_name, "instruction": instruction})
        gr.Info(f"テンプレート「{target_name}」を新規作成しました。")
    else:
        gr.Info(f"テンプレート「{target_name}」を更新しました。")

    config_manager.save_config_if_changed("user_image_gen_instruction_templates", new_templates)
    choices = [t["name"] for t in new_templates]
    return gr.update(choices=choices, value=target_name), target_name, ""

def handle_user_gen_instruction_delete(selected_name):
    """テンプレートの削除処理。"""
    templates = config_manager.CONFIG_GLOBAL.get("user_image_gen_instruction_templates", [])
    new_templates = [t for t in templates if t["name"] != selected_name]

    if len(new_templates) == len(templates):
        return gr.update(), gr.update(), gr.update(), "⚠️ 削除対象が見つかりません。"

    config_manager.save_config_if_changed("user_image_gen_instruction_templates", new_templates)
    gr.Info(f"テンプレート「{selected_name}」を削除しました。")

    choices = [t["name"] for t in new_templates]
    new_val = choices[0] if choices else None
    new_instr = new_templates[0]["instruction"] if new_templates else ""
    return gr.update(choices=choices, value=new_val), new_val or "", new_instr, ""

def handle_generate_user_image_prompt_ai(room_name, instruction, api_key_name):
    """AIを使ってチャットログから画像プロンプトを生成する。"""
    if not room_name:
        yield gr.update(), "⚠️ ルームを選択してください。"
        return
    if not instruction:
        yield gr.update(), "⚠️ 依頼内容を入力してください。"
        return

    yield gr.update(), "⏳ AIがチャットログを分析してプロンプトを生成中..."

    try:
        from llm_factory import LLMFactory
        # チャットログを取得（直近30件）
        log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        if not log_file or not os.path.exists(log_file):
             raise Exception("チャットログファイルが見つかりません。")

        full_log = utils.load_chat_log(log_file)
        # ユーザーとAIの発言のみを抽出
        recent_log = []
        for m in full_log[-30:]: # 直近30件程度
            responder = m.get("responder", "Unknown")
            content = m.get("content", "")
            # HTMLタグ除去
            content_clean = utils.extract_raw_text_from_html(content)
            # 思考プロセス除去
            content_no_thoughts = utils.remove_thoughts_from_text(content_clean)
            recent_log.append(f"{responder}: {content_no_thoughts}")

        chat_context = "\n".join(recent_log)

        # システムプロンプト構成
        system_prompt = f"""
あなたは画像生成AI（Stable Diffusion, Midjourney, Flux等）のためのプロンプトエンジニアです。
ユーザーから提供される「チャットログの断片」と「生成の指示」に基づき、最高の画像生成用プロンプト（英語）を1つだけ作成してください。

**生成のルール**:
1. 出力は「プロンプト文字列のみ」にしてください。解説や「Prompt:」などの接頭辞は不要です。
2. 原則として英語で出力してください。
3. チャットの文脈（場所、登場人物、雰囲気、現在の出来事）を最大限に反映させてください。
4. 画質を高めるためのタグ（detailed, masterpiece, cinematic lighting等）を指示に合わせて適宜含めてください。

**チャットログの断片**:
{chat_context}

**ユーザーからの生成指示**:
{instruction}
"""
        # 内部処理用モデルでのプロンプト生成（APIキーローテ＋リトライ）
        effective_settings = config_manager.get_effective_settings(room_name)
        # キー選択・429/503リトライ・usage計上は共通機構に委譲する。
        response, _used_key = LLMFactory.invoke_internal_llm(
            internal_role="processing",
            prompt=system_prompt,
            room_name=room_name,
            generation_config=effective_settings,
        )
        generated_prompt = utils.get_content_as_string(response).strip()
        if not generated_prompt:
            raise Exception("プロンプト生成に失敗しました（空応答）。")

        # 生成されたプロンプトから不要な囲み（"" や ```）を除去
        if generated_prompt.startswith("```"):
            generated_prompt = re.sub(r'^```[a-zA-Z]*\n', '', generated_prompt)
            generated_prompt = re.sub(r'\n```$', '', generated_prompt)
        generated_prompt = generated_prompt.strip('"').strip("'")

        yield generated_prompt, "✅ プロンプトを生成しました。"

    except Exception as e:
        traceback.print_exc()
        readable_error = _parse_llm_error_to_readable(e)
        # status(Markdown)はボタンから離れた位置にあり見落とされやすいため、
        # トースト通知でも明示的にアナウンスする。
        gr.Warning(f"画像プロンプト生成に失敗しました: {readable_error}")
        yield gr.update(), f"❌ 生成エラー: {readable_error}"

# --- ライブラリ承認管理ハンドラ ---

def handle_refresh_dependencies():
    """承認待ち・承認済みライブラリのリストを更新する"""
    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    pending = settings.get("pending_dependencies", [])
    allowed = settings.get("allowed_dependencies", [])

    pending_data = [[p] for p in pending]
    allowed_data = [[a] for a in allowed]

    return pending_data, allowed_data, ""

def handle_approve_dependency(pkg_name: str):
    """選択されたパッケージを承認済みリストに移動する"""
    if not pkg_name:
        return gr.update(), gr.update(), "⚠️ パッケージを選択してください。"

    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    pending = settings.get("pending_dependencies", [])
    allowed = settings.get("allowed_dependencies", [])

    if pkg_name in pending:
        pending.remove(pkg_name)
    if pkg_name not in allowed:
        allowed.append(pkg_name)

    settings["pending_dependencies"] = pending
    settings["allowed_dependencies"] = allowed
    config_manager.save_config_if_changed("custom_tools_settings", settings)

    install_message = ""
    try:
        from custom_tool_manager import CustomToolManager
        success, dep_msg = CustomToolManager.install_dependencies([pkg_name])
        install_message = dep_msg
        if success:
            gr.Info(f"パッケージ '{pkg_name}' を承認し、インストールを確認しました。")
        else:
            gr.Warning(f"パッケージ '{pkg_name}' を承認しましたが、インストール確認に失敗しました: {dep_msg}")
    except Exception as e:
        install_message = f"インストール確認でエラー: {e}"
        gr.Warning(f"パッケージ '{pkg_name}' を承認しましたが、インストール確認でエラーが発生しました。")

    pending_data, allowed_data, _ = handle_refresh_dependencies()
    status = f"✅ '{pkg_name}' を承認しました。{install_message}"
    return pending_data, allowed_data, status

def handle_reject_dependency(pkg_name: str):
    """選択されたパッケージを却下（削除）する"""
    if not pkg_name:
        return gr.update(), gr.update(), "⚠️ パッケージを選択してください。"

    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    pending = settings.get("pending_dependencies", [])

    if pkg_name in pending:
        pending.remove(pkg_name)

    settings["pending_dependencies"] = pending
    config_manager.save_config_if_changed("custom_tools_settings", settings)

    gr.Info(f"パッケージ '{pkg_name}' を却下しました。")
    return handle_refresh_dependencies()

def handle_remove_allowed_dependency(pkg_name: str):
    """承認済みのパッケージをリストから削除する"""
    if not pkg_name:
        return gr.update(), gr.update(), "⚠️ パッケージを選択してください。"

    settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
    allowed = settings.get("allowed_dependencies", [])

    if pkg_name in allowed:
        allowed.remove(pkg_name)

    settings["allowed_dependencies"] = allowed
    config_manager.save_config_if_changed("custom_tools_settings", settings)

    gr.Info(f"パッケージ '{pkg_name}' の承認を取り消しました。")
    return handle_refresh_dependencies()
def handle_accept_disclaimer(service_name: str):
    """免責事項の承諾を保存し、UIの表示状態を更新する。"""
    accepted_disclaimers = config_manager.CONFIG_GLOBAL.get("accepted_disclaimers", {})
    accepted_disclaimers[service_name] = True
    config_manager.save_config_if_changed("accepted_disclaimers", accepted_disclaimers)
    
    # 状態の即時反映のために gr.update を返す
    # 1番目: アコーディオンを閉じる, 2番目: 承諾ボタンを非表示, 3番目: メインコンテンツを表示
    return gr.update(open=False), gr.update(visible=False), gr.update(visible=True)

def handle_tts_profile_change(profile_name: str, room_name: str):
    """ルーム個別設定のOpenAI互換接続プロファイルが変更された際に、
    現在のTTS設定をプロファイル別キャッシュに保存し、
    新プロファイルのキャッシュから設定を復元する。"""
    model_choices = config_manager.get_openai_compatible_tts_model_choices_for_profile(profile_name)
    default_model = model_choices[0] if model_choices else None
    voice_map = config_manager.get_openai_compatible_tts_voice_map_for_profile(profile_name, model_name=default_model)
    voice_choices = list(voice_map.values())
    default_voice_display = voice_choices[0] if voice_choices else None

    restored_model = default_model
    restored_voice_display = default_voice_display
    restored_style = ""

    if room_name:
        override_settings = {}
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        if os.path.exists(room_config_path):
            try:
                with open(room_config_path, "r", encoding="utf-8") as f:
                    room_config = json.load(f)
                override_settings = room_config.get("override_settings", {})
            except Exception:
                pass

        oc_settings = override_settings.setdefault("tts_provider_settings", {}).setdefault("openai_compatible", {})
        profile_cache = oc_settings.setdefault("_profile_cache", {})

        # 現在のプロファイルの設定をキャッシュに保存（切り替え前のプロファイル名を使う）
        old_profile = oc_settings.get("tts_profile_name")
        if old_profile and old_profile != profile_name:
            profile_cache[old_profile] = {
                "tts_model": oc_settings.get("tts_model"),
                "tts_voice": oc_settings.get("tts_voice"),
                "voice_style_prompt": oc_settings.get("voice_style_prompt", ""),
            }

        # 新プロファイルのキャッシュから復元
        cached = profile_cache.get(profile_name, {})
        restored_model = cached.get("tts_model") or default_model
        if model_choices and restored_model not in model_choices:
            restored_model = default_model
        elif not model_choices:
            restored_model = None
        voice_map = config_manager.get_openai_compatible_tts_voice_map_for_profile(profile_name, model_name=restored_model)
        voice_choices = list(voice_map.values())
        default_voice_display = voice_choices[0] if voice_choices else None
        restored_voice_id = cached.get("tts_voice")
        restored_style = cached.get("voice_style_prompt", "")

        if restored_voice_id and restored_voice_id in voice_map:
            restored_voice_display = voice_map[restored_voice_id]
        else:
            restored_voice_id = next(iter(voice_map.keys()), None)
            restored_voice_display = default_voice_display

        # oc_settings を新プロファイルの値で更新
        oc_settings["tts_profile_name"] = profile_name
        oc_settings["tts_model"] = restored_model
        oc_settings["tts_voice"] = restored_voice_id
        oc_settings["voice_style_prompt"] = restored_style

        # 共通キーの同期
        updates = {
            "tts_profile_name": profile_name,
            "tts_model": restored_model,
        }
        updates["tts_voice"] = restored_voice_id
        override_settings.update(updates)
        room_manager.update_room_config(room_name, {"override_settings": override_settings})

    # カスタム値をchoicesに含める
    model_choices = _ensure_value_in_choices(model_choices, restored_model)
    if config_manager.get_openai_profile_tts_kind(profile_name) == "custom" and restored_voice_display and restored_voice_display not in voice_choices:
        voice_choices = voice_choices + [restored_voice_display]

    return (
        gr.update(choices=model_choices, value=restored_model),
        gr.update(choices=voice_choices, value=restored_voice_display),
        gr.update(value=restored_style),
    )


def handle_tts_model_change_for_voice_choices(
    room_name: str,
    provider_display: str,
    profile_name: str,
    model_name: str,
):
    """OpenAI互換TTSモデル変更時に、そのモデルで使える声だけへ候補を更新する。"""
    provider = config_manager.tts_provider_key_from_display(provider_display)
    if provider != "openai_compatible":
        return gr.update()

    voice_map = config_manager.get_openai_compatible_tts_voice_map_for_profile(profile_name, model_name=model_name)
    voice_choices = list(voice_map.values())
    if not voice_map:
        return gr.update(choices=[], value=None)

    restored_voice_id = None
    if room_name:
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        if os.path.exists(room_config_path):
            try:
                with open(room_config_path, "r", encoding="utf-8") as f:
                    room_config = json.load(f)
                overrides = room_config.get("override_settings", {})
                provider_settings = overrides.get("tts_provider_settings", {}).get("openai_compatible", {})
                restored_voice_id = provider_settings.get("tts_voice") or overrides.get("tts_voice")
            except Exception:
                restored_voice_id = None

    if restored_voice_id in voice_map:
        display_voice = voice_map[restored_voice_id]
    else:
        restored_voice_id = next(iter(voice_map.keys()), None)
        display_voice = next(iter(voice_map.values()), None)

    if room_name and restored_voice_id:
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        overrides = {}
        if os.path.exists(room_config_path):
            try:
                with open(room_config_path, "r", encoding="utf-8") as f:
                    overrides = json.load(f).get("override_settings", {})
            except Exception:
                overrides = {}
        provider_settings = overrides.setdefault("tts_provider_settings", {})
        openai_compatible_settings = provider_settings.setdefault("openai_compatible", {})
        openai_compatible_settings["tts_model"] = model_name
        openai_compatible_settings["tts_voice"] = restored_voice_id
        room_manager.update_room_config(
            room_name,
            {
                "override_settings": {
                    "tts_model": model_name,
                    "tts_voice": restored_voice_id,
                    "tts_provider_settings": provider_settings,
                }
            },
        )

    return gr.update(choices=voice_choices, value=display_voice)


def handle_fetch_openai_compatible_tts_models(
    room_name: str,
    provider_display: str,
    profile_name: str,
):
    """OpenAI互換プロファイルからTTSモデル/声候補を取得し、プロファイルへキャッシュする。"""
    provider = config_manager.tts_provider_key_from_display(provider_display)
    if provider != "openai_compatible":
        gr.Warning("TTSモデル取得はOpenAI互換プロファイル選択時に使用できます。")
        return gr.update(), gr.update()
    if not profile_name:
        gr.Warning("OpenAI互換プロファイルが選択されていません。")
        return gr.update(), gr.update()

    setting = config_manager.get_openai_setting_by_name(profile_name)
    if not setting:
        gr.Warning(f"プロファイル「{profile_name}」が見つかりません。")
        return gr.update(), gr.update()

    base_url = setting.get("base_url") or ""
    api_key = setting.get("api_key") or ""
    capabilities = config_manager.fetch_openai_compatible_tts_capabilities(profile_name, base_url, api_key)
    models = capabilities.get("models") or []
    voice_cache = capabilities.get("voice_cache") or {}
    kind = capabilities.get("kind")

    config_manager.save_openai_profile_tts_cache(profile_name, models, voice_cache)

    if not models:
        if kind == "no_tts":
            gr.Warning(f"プロファイル「{profile_name}」は既知の非TTSプロファイルです。TTS対応のプロファイルを選択してください。")
        else:
            gr.Warning(f"プロファイル「{profile_name}」からTTSモデルを取得できませんでした。")
        return gr.update(choices=[], value=None), gr.update(choices=[], value=None)

    selected_model = models[0]
    voice_map = config_manager.get_openai_compatible_tts_voice_map_for_profile(profile_name, base_url, selected_model)
    voice_choices = list(voice_map.values())
    selected_voice_id = next(iter(voice_map.keys()), None)
    selected_voice_display = next(iter(voice_map.values()), None)

    if room_name:
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        overrides = {}
        if os.path.exists(room_config_path):
            try:
                with open(room_config_path, "r", encoding="utf-8") as f:
                    overrides = json.load(f).get("override_settings", {})
            except Exception:
                overrides = {}
        provider_settings = overrides.setdefault("tts_provider_settings", {})
        openai_compatible_settings = provider_settings.setdefault("openai_compatible", {})
        openai_compatible_settings["tts_profile_name"] = profile_name
        openai_compatible_settings["tts_model"] = selected_model
        if selected_voice_id:
            openai_compatible_settings["tts_voice"] = selected_voice_id
        updates = {
            "tts_profile_name": profile_name,
            "tts_model": selected_model,
            "tts_provider_settings": provider_settings,
        }
        if selected_voice_id:
            updates["tts_voice"] = selected_voice_id
        room_manager.update_room_config(room_name, {"override_settings": updates})

    gr.Info(f"プロファイル「{profile_name}」のTTSモデル候補を更新しました（{len(models)}件）。")
    return (
        gr.update(choices=models, value=selected_model),
        gr.update(choices=voice_choices, value=selected_voice_display),
    )


# ===========================================================================
# プレイブック管理UI（委任エージェントのノウハウ・環境共通）
# ===========================================================================
# 一覧は「読み込み」ボタンを押すまで populate しない（Gradio6系の自動読込フリーズ対策）。

PLAYBOOK_COLUMNS = ["種別", "ID", "タイトル", "用途", "状態"]

_PLAYBOOK_APPLY_CHOICES = [
    ("アトリエのアプリ制作", "atelier"),
    ("ディープリサーチ（調査）", "research"),
    ("キーワードに一致した委任", "keyword"),
    ("あらゆる委任（汎用）", "general"),
]


def _empty_playbook_df() -> "pd.DataFrame":
    return pd.DataFrame(columns=PLAYBOOK_COLUMNS)


def _playbook_layer_label(layer: str) -> str:
    from agent_delegation import skill_pack
    return "ユーザー" if layer == skill_pack.USER_LAYER else "運営"


def _playbook_state_label(row: dict) -> str:
    if row.get("overridden"):
        return "上書き中"
    if row.get("editable"):
        return "編集可"
    return "閲覧のみ"


def _build_playbook_dataframe_and_choices():
    """detailed 一覧から (DataFrame, dropdown choices) を作る。"""
    from agent_delegation import skill_pack
    rows = skill_pack.list_playbooks_detailed()
    records = []
    choices = []
    for r in rows:
        records.append([
            _playbook_layer_label(r["layer"]),
            r["id"],
            r.get("title") or "",
            r.get("applies_when") or "",
            _playbook_state_label(r),
        ])
        label = f"[{_playbook_layer_label(r['layer'])}] {r['id']}｜{r.get('title') or ''}"
        choices.append((label, r["id"]))
    df = pd.DataFrame(records, columns=PLAYBOOK_COLUMNS) if records else _empty_playbook_df()
    return df, choices


def refresh_playbook_list():
    """🔄『一覧を読み込む』で呼ぶ。(df, dropdown_update, status)。"""
    try:
        df, choices = _build_playbook_dataframe_and_choices()
        status = f"📚 プレイブック {len(choices)} 件を読み込みました。編集するものを下のリストから選んでください。"
        return df, gr.update(choices=choices, value=None), status
    except Exception as e:
        logger.error(f"プレイブック一覧の読み込みに失敗: {e}", exc_info=True)
        return _empty_playbook_df(), gr.update(choices=[], value=None), f"❌ 読み込みに失敗しました: {e}"


def _playbook_form_update(
    *,
    skill_id="",
    title="",
    summary="",
    apply_kind="keyword",
    keywords="",
    priority=50,
    body="",
    id_editable=True,
    editable=True,
    is_operator=False,
    selected_id="",
    selected_layer="",
    status="",
    hide_confirm=True,
):
    """フォーム一式の更新タプルを返す（順序は配線と厳密に一致させること）。"""
    updates = (
        gr.update(value=skill_id, interactive=id_editable),                 # 1 id
        gr.update(value=title, interactive=editable),                       # 2 title
        gr.update(value=summary, interactive=editable),                     # 3 summary
        gr.update(value=apply_kind, interactive=editable),                  # 4 apply radio
        gr.update(value=keywords, interactive=editable),                    # 5 keywords
        gr.update(value=priority, interactive=editable),                    # 6 priority
        gr.update(value=body, interactive=editable),                        # 7 body
        gr.update(interactive=editable),                                    # 8 save button
        gr.update(visible=is_operator),                                     # 9 copy button
        gr.update(visible=True),                                            # 10 delete button（常時表示・押下時にガード）
        gr.update(visible=False) if hide_confirm else gr.update(),          # 11 confirm row
        selected_id,                                                        # 12 selected_id state
        selected_layer,                                                     # 13 selected_layer state
        status,                                                             # 14 status
    )
    return updates


def select_playbook(evt=None):
    """Dropdown選択時（.select の SelectData か、テスト用の生 id を受ける）。
    フォームへ読み込み、層に応じて編集可否を切り替える。"""
    from agent_delegation import skill_pack
    skill_id = getattr(evt, "value", evt) if evt is not None else ""
    if not skill_id:
        return _playbook_form_update(status="プレイブックを選択してください。")
    try:
        rows = {r["id"]: r for r in skill_pack.list_playbooks_detailed()}
        row = rows.get(skill_id)
        if not row:
            return _playbook_form_update(status="選択したプレイブックが見つかりません。一覧を再読み込みしてください。")
        layer = row["layer"]
        source = skill_pack.read_playbook_source(skill_id, layer)
        fields = skill_pack.parse_playbook(source)
        editable = (layer == skill_pack.USER_LAYER)
        is_operator = (layer == skill_pack.OPERATOR_LAYER)
        note = (
            "✏️ ユーザープレイブックです。編集・削除できます。"
            if editable else
            "🔒 運営プレイブックです（閲覧のみ）。変えたい場合は『運営版を複製して編集』を押すと、同じIDのユーザー版を作って編集できます。"
        )
        return _playbook_form_update(
            skill_id=skill_id,
            title=fields["title"],
            summary=fields["summary"],
            apply_kind=fields["apply_kind"],
            keywords="、".join(fields["keywords"]),
            priority=fields["priority"],
            body=fields["body"],
            id_editable=False,  # 既存編集中はID固定
            editable=editable,
            is_operator=is_operator,
            selected_id=skill_id,
            selected_layer=layer,
            status=note,
        )
    except Exception as e:
        logger.error(f"プレイブック選択に失敗: {e}", exc_info=True)
        return _playbook_form_update(status=f"❌ 読み込みに失敗しました: {e}")


def new_playbook():
    """➕新規作成。空フォームを編集可能状態で出す。"""
    return _playbook_form_update(
        skill_id="",
        title="",
        summary="",
        apply_kind="keyword",
        keywords="",
        priority=50,
        body="",
        id_editable=True,
        editable=True,
        is_operator=False,
        selected_id="",
        selected_layer="user",
        status="📝 新しいプレイブックを作成します。ID（半角英数・ハイフン）と本文を入力して『保存』を押してください。",
    )


def save_playbook(skill_id, title, summary, apply_kind, keywords, priority, body):
    """💾保存。(df, dropdown) + FORM(14)。保存後は保存済み状態でフォームを再表示し、
    トースト通知でも知らせる。"""
    from agent_delegation import skill_pack
    try:
        safe_id = skill_pack._safe_playbook_id(skill_id)
        if not safe_id:
            df, choices = _build_playbook_dataframe_and_choices()
            return (df, gr.update(choices=choices)) + _playbook_form_update(
                skill_id=skill_id, title=title, summary=summary, apply_kind=apply_kind or "keyword",
                keywords=keywords, priority=priority, body=body,
                id_editable=True, editable=True, is_operator=False,
                selected_id="", selected_layer="user",
                status="❌ ID（半角英数・ハイフン）を入力してください。",
            )
        content = skill_pack.compose_playbook(
            skill_id=safe_id, title=title or "", summary=summary or "",
            apply_kind=apply_kind or "general", keywords=keywords or "",
            priority=priority or 0, body=body or "",
        )
        res = skill_pack.save_user_playbook(safe_id, content)
        df, choices = _build_playbook_dataframe_and_choices()
        msg = f"✅ プレイブック『{safe_id}』を保存しました。"
        if res.get("warning"):
            msg += f"\n⚠️ {res['warning']}"
        try:
            gr.Info(f"プレイブック『{safe_id}』を保存しました。")
        except Exception:
            pass
        # 保存結果を読み直して「保存済み（ユーザー層・削除可）」状態で再表示
        form = select_playbook(safe_id)
        form = form[:-1] + (msg,)
        return (df, gr.update(choices=choices, value=safe_id)) + form
    except Exception as e:
        logger.error(f"プレイブック保存に失敗: {e}", exc_info=True)
        df, choices = _build_playbook_dataframe_and_choices()
        return (df, gr.update(choices=choices)) + _playbook_form_update(
            skill_id=skill_id, title=title, summary=summary, apply_kind=apply_kind or "keyword",
            keywords=keywords, priority=priority, body=body,
            id_editable=True, editable=True, is_operator=False,
            selected_id="", selected_layer="user",
            status=f"❌ 保存できませんでした: {e}",
        )


def copy_operator_playbook_to_user(selected_id):
    """📋運営版を複製してユーザー層に作り、編集可能状態で開く。
    (df, dropdown) + FORM(14) を返す。"""
    from agent_delegation import skill_pack
    try:
        if not selected_id:
            df, choices = _build_playbook_dataframe_and_choices()
            return (df, gr.update(choices=choices)) + select_playbook(selected_id)
        source = skill_pack.read_playbook_source(selected_id, skill_pack.OPERATOR_LAYER)
        if not source:
            df, choices = _build_playbook_dataframe_and_choices()
            return (df, gr.update(choices=choices)) + _playbook_form_update(
                status="複製元の運営プレイブックが見つかりませんでした。"
            )
        skill_pack.save_user_playbook(selected_id, source)
        df, choices = _build_playbook_dataframe_and_choices()
        form = select_playbook(selected_id)  # ユーザー版として開き直す（編集可に）
        # status だけ差し替え
        form = form[:-1] + (f"📋 運営版『{selected_id}』をユーザー層に複製しました。編集して保存できます。",)
        return (df, gr.update(choices=choices, value=selected_id)) + form
    except Exception as e:
        logger.error(f"運営版の複製に失敗: {e}", exc_info=True)
        df, choices = _build_playbook_dataframe_and_choices()
        return (df, gr.update(choices=choices)) + _playbook_form_update(status=f"❌ 複製できませんでした: {e}")


def ask_delete_playbook(selected_id, selected_layer):
    """🗑️削除（1段目）。ユーザー層のときだけ確認行を出す。(confirm_row, status)。"""
    from agent_delegation import skill_pack
    if not selected_id or selected_layer != skill_pack.USER_LAYER:
        return gr.update(visible=False), (
            "削除できるのは、リストから選択したユーザープレイブックだけです。"
            "運営分は『運営版を複製して編集』でユーザー版を作ってから削除できます。"
        )
    return gr.update(visible=True), f"⚠️ プレイブック『{selected_id}』を削除しますか？ この操作は元に戻せません。"


def cancel_delete_playbook():
    """確認をやめる。(confirm_row, status)。"""
    return gr.update(visible=False), "削除をキャンセルしました。"


def confirm_delete_playbook(selected_id):
    """削除確定（2段目）。(df, dropdown) + FORM(14) を返し、一覧更新＋フォーム消去＋確認行を隠す。"""
    from agent_delegation import skill_pack
    try:
        skill_pack.delete_user_playbook(selected_id)
        df, choices = _build_playbook_dataframe_and_choices()
        try:
            gr.Info(f"プレイブック『{selected_id}』を削除しました。")
        except Exception:
            pass
        form = _playbook_form_update(
            status=f"🗑️ プレイブック『{selected_id}』を削除しました。",
            hide_confirm=False,  # 明示的に隠す（下で visible=False）
        )
        # confirm row を隠す形に揃える（_playbook_form_update の 11 番目を上書き）
        form = form[:10] + (gr.update(visible=False),) + form[11:]
        return (df, gr.update(choices=choices, value=None)) + form
    except Exception as e:
        logger.error(f"プレイブック削除に失敗: {e}", exc_info=True)
        df, choices = _build_playbook_dataframe_and_choices()
        form = _playbook_form_update(status=f"❌ 削除できませんでした: {e}")
        form = form[:10] + (gr.update(visible=False),) + form[11:]
        return (df, gr.update(choices=choices)) + form


# ---------------------------------------------------------------------------
# プレイブック育成（E：提案レビュー → 採用／却下）
# ---------------------------------------------------------------------------
# ペルソナが propose_playbook_update で出した改善案（pending）を、ユーザーがレビューして
# 採用（ユーザー層へ本反映）／却下する。AI は提案のみ・採用は人。

PLAYBOOK_PROPOSAL_COLUMNS = ["提案ID", "採用時のID", "タイトル", "適用", "理由", "元タスク", "作成日時"]

_PLAYBOOK_APPLY_LABELS = {
    "atelier": "アプリ制作",
    "research": "調査",
    "keyword": "キーワード",
    "general": "汎用",
}


def _empty_playbook_proposal_df() -> "pd.DataFrame":
    return pd.DataFrame(columns=PLAYBOOK_PROPOSAL_COLUMNS)


def _build_playbook_proposal_dataframe_and_choices():
    from agent_delegation import skill_pack
    rows = skill_pack.list_proposals()
    records = []
    choices = []
    for r in rows:
        records.append([
            r.get("proposal_id") or "",
            r.get("target_id") or "",
            r.get("title") or "",
            _PLAYBOOK_APPLY_LABELS.get(r.get("apply_kind") or "", r.get("apply_kind") or ""),
            r.get("reason") or "",
            r.get("source_task") or "",
            (r.get("created_at") or "")[:19],
        ])
        label = f"{r.get('target_id') or ''}｜{r.get('title') or ''}（{(r.get('created_at') or '')[:16]}）"
        choices.append((label, r.get("proposal_id") or ""))
    df = pd.DataFrame(records, columns=PLAYBOOK_PROPOSAL_COLUMNS) if records else _empty_playbook_proposal_df()
    return df, choices


def refresh_playbook_proposals():
    """🌱『提案を読み込む』で呼ぶ。(df, dropdown_update, status, preview)。"""
    try:
        df, choices = _build_playbook_proposal_dataframe_and_choices()
        if choices:
            status = f"🌱 プレイブック改善案 {len(choices)} 件。採用すると委任に反映されます（採用は人が判断）。"
        else:
            status = "🌱 レビュー待ちの改善案はありません。"
        return df, gr.update(choices=choices, value=None), status, gr.update(value="")
    except Exception as e:
        logger.error(f"プレイブック提案の読み込みに失敗: {e}", exc_info=True)
        return _empty_playbook_proposal_df(), gr.update(choices=[], value=None), f"❌ 読み込みに失敗しました: {e}", gr.update()


def select_playbook_proposal(proposal_id):
    """提案ドロップダウン選択時。提案 .md 本文をプレビューに表示する。"""
    from agent_delegation import skill_pack
    pid = str(getattr(proposal_id, "value", proposal_id) or "").strip()
    if not pid:
        return gr.update(value="")
    try:
        return gr.update(value=skill_pack.read_proposal(pid))
    except Exception as e:
        return gr.update(value=f"プレビューを表示できませんでした: {e}")


def adopt_playbook_proposal(proposal_id):
    """提案を採用する。ユーザー層へ本反映し、提案・プレイブック両一覧を更新する。

    戻り値: (提案df, 提案dropdown, 提案status, プレビュー, プレイブックdf, プレイブックdropdown)。
    """
    from agent_delegation import skill_pack
    pid = str(proposal_id or "").strip()
    pb_df, pb_choices = _build_playbook_dataframe_and_choices()
    if not pid:
        gr.Warning("採用する提案を選択してください。")
        prop_df, prop_choices = _build_playbook_proposal_dataframe_and_choices()
        return (prop_df, gr.update(choices=prop_choices), "採用する提案を選択してください。",
                gr.update(), pb_df, gr.update(choices=pb_choices, value=None))
    try:
        res = skill_pack.adopt_proposal(pid)
        gr.Info(f"提案を採用し、プレイブック『{res.get('target_id')}』に反映しました。")
        status = f"✅ 提案を採用しました（プレイブック『{res.get('target_id')}』に反映）。"
    except Exception as e:
        logger.error(f"プレイブック提案の採用に失敗: {e}", exc_info=True)
        gr.Warning(f"採用できませんでした: {e}")
        status = f"❌ 採用できませんでした: {e}"
    prop_df, prop_choices = _build_playbook_proposal_dataframe_and_choices()
    pb_df, pb_choices = _build_playbook_dataframe_and_choices()
    return (prop_df, gr.update(choices=prop_choices, value=None), status, gr.update(value=""),
            pb_df, gr.update(choices=pb_choices, value=None))


def discard_playbook_proposal(proposal_id):
    """提案を却下（削除）する。ユーザー層には何も反映しない。(df, dropdown, status, preview)。"""
    from agent_delegation import skill_pack
    pid = str(proposal_id or "").strip()
    if not pid:
        gr.Warning("却下する提案を選択してください。")
    else:
        try:
            skill_pack.discard_proposal(pid)
            gr.Info("提案を却下しました。")
        except Exception as e:
            logger.error(f"プレイブック提案の却下に失敗: {e}", exc_info=True)
            gr.Warning(f"却下できませんでした: {e}")
    df, choices = _build_playbook_proposal_dataframe_and_choices()
    return df, gr.update(choices=choices, value=None), "🌱 提案一覧を更新しました。", gr.update(value="")


# ===========================================================================
# ロール管理UI（委任エージェントの役割＝装備一式・環境共通）
# ===========================================================================
# プレイブック管理UIと同じ作法。一覧は「読み込み」ボタンを押すまで populate しない。

ROLE_COLUMNS = ["種別", "ID", "タイトル", "装備", "状態"]

_ROLE_WORKSPACE_CHOICES = [
    ("指定なし（呼び出すツールに従う）", ""),
    ("プロジェクト", "project"),
    ("アトリエ", "persona"),
    ("アトリエ＋プロジェクト読取", "persona_project_read"),
]
_ROLE_TIER_CHOICES = [
    ("指定なし", ""),
    ("読み取り", "read"),
    ("読み書き", "write"),
    ("フル（Bash）", "full"),
]


def _empty_role_df() -> "pd.DataFrame":
    return pd.DataFrame(columns=ROLE_COLUMNS)


def _role_layer_label(layer: str) -> str:
    from agent_delegation import roles
    return "ユーザー" if layer == roles.USER_LAYER else "運営"


def _role_state_label(row: dict) -> str:
    if row.get("overridden"):
        return "上書き中"
    if row.get("editable"):
        return "編集可"
    return "閲覧のみ"


def _build_role_dataframe_and_choices():
    from agent_delegation import roles
    rows = roles.list_roles_detailed()
    records = []
    choices = []
    for r in rows:
        records.append([
            _role_layer_label(r["layer"]),
            r["id"],
            r.get("title") or "",
            r.get("equipment") or "",
            _role_state_label(r),
        ])
        label = f"[{_role_layer_label(r['layer'])}] {r['id']}｜{r.get('title') or ''}"
        choices.append((label, r["id"]))
    df = pd.DataFrame(records, columns=ROLE_COLUMNS) if records else _empty_role_df()
    return df, choices


def refresh_role_list():
    """🔄『一覧を読み込む』で呼ぶ。(df, dropdown_update, status)。"""
    try:
        df, choices = _build_role_dataframe_and_choices()
        status = f"🎭 ロール {len(choices)} 件を読み込みました。編集するものを下のリストから選んでください。"
        return df, gr.update(choices=choices, value=None), status
    except Exception as e:
        logger.error(f"ロール一覧の読み込みに失敗: {e}", exc_info=True)
        return _empty_role_df(), gr.update(choices=[], value=None), f"❌ 読み込みに失敗しました: {e}"


def _role_form_update(
    *,
    role_id="",
    title="",
    summary="",
    workspace_kind="",
    permission_tier="",
    allow_web_tools=False,
    task_kind="",
    model_hint="",
    expected_output="",
    priority=50,
    body="",
    id_editable=True,
    editable=True,
    is_operator=False,
    selected_id="",
    selected_layer="",
    status="",
    hide_confirm=True,
):
    """フォーム一式の更新タプル（18要素）を返す（順序は配線と厳密に一致させること）。"""
    return (
        gr.update(value=role_id, interactive=id_editable),       # 1 id
        gr.update(value=title, interactive=editable),            # 2 title
        gr.update(value=summary, interactive=editable),          # 3 summary
        gr.update(value=workspace_kind, interactive=editable),   # 4 workspace radio
        gr.update(value=permission_tier, interactive=editable),  # 5 tier radio
        gr.update(value=bool(allow_web_tools), interactive=editable),  # 6 web checkbox
        gr.update(value=task_kind, interactive=editable),        # 7 task_kind
        gr.update(value=model_hint, interactive=editable),       # 8 model_hint
        gr.update(value=expected_output, interactive=editable),  # 9 expected_output
        gr.update(value=priority, interactive=editable),         # 10 priority
        gr.update(value=body, interactive=editable),             # 11 body
        gr.update(interactive=editable),                         # 12 save button
        gr.update(visible=is_operator),                          # 13 copy button
        gr.update(visible=True),                                 # 14 delete button（常時表示・押下時ガード）
        gr.update(visible=False) if hide_confirm else gr.update(),  # 15 confirm row
        selected_id,                                             # 16 selected_id state
        selected_layer,                                          # 17 selected_layer state
        status,                                                  # 18 status
    )


def select_role(evt=None):
    """Dropdown選択時（.input の値か生 id）。フォームへ読み込み、層に応じて編集可否を切り替える。"""
    from agent_delegation import roles
    role_id = getattr(evt, "value", evt) if evt is not None else ""
    if not role_id:
        return _role_form_update(status="ロールを選択してください。")
    try:
        rows = {r["id"]: r for r in roles.list_roles_detailed()}
        row = rows.get(role_id)
        if not row:
            return _role_form_update(status="選択したロールが見つかりません。一覧を再読み込みしてください。")
        layer = row["layer"]
        source = roles.read_role_source(role_id, layer)
        fields = roles.parse_role(source)
        editable = (layer == roles.USER_LAYER)
        is_operator = (layer == roles.OPERATOR_LAYER)
        note = (
            "✏️ ユーザーロールです。編集・削除できます。"
            if editable else
            "🔒 運営ロールです（閲覧のみ）。変えたい場合は『運営版を複製して編集』を押すと、同じIDのユーザー版を作って編集できます。"
        )
        return _role_form_update(
            role_id=role_id,
            title=fields["title"],
            summary=fields["summary"],
            workspace_kind=fields["workspace_kind"],
            permission_tier=fields["permission_tier"],
            allow_web_tools=fields["allow_web_tools"],
            task_kind=fields["task_kind"],
            model_hint=fields["model_hint"],
            expected_output=fields["expected_output"],
            priority=fields["priority"],
            body=fields["body"],
            id_editable=False,
            editable=editable,
            is_operator=is_operator,
            selected_id=role_id,
            selected_layer=layer,
            status=note,
        )
    except Exception as e:
        logger.error(f"ロール選択に失敗: {e}", exc_info=True)
        return _role_form_update(status=f"❌ 読み込みに失敗しました: {e}")


def new_role():
    """➕新規作成。空フォームを編集可能状態で出す。"""
    return _role_form_update(
        role_id="", title="", summary="", workspace_kind="", permission_tier="",
        allow_web_tools=False, task_kind="", model_hint="", expected_output="", priority=50, body="",
        id_editable=True, editable=True, is_operator=False,
        selected_id="", selected_layer="user",
        status="📝 新しいロールを作成します。ID（半角英数・ハイフン）と本文（進め方）を入力して『保存』を押してください。",
    )


def save_role(role_id, title, summary, workspace_kind, permission_tier, allow_web_tools, task_kind, model_hint, expected_output, priority, body):
    """💾保存。(df, dropdown) + FORM(18)。保存後は保存済み状態で再表示＋トースト。"""
    from agent_delegation import roles
    try:
        safe_id = roles._safe_role_id(role_id)
        if not safe_id:
            df, choices = _build_role_dataframe_and_choices()
            return (df, gr.update(choices=choices)) + _role_form_update(
                role_id=role_id, title=title, summary=summary, workspace_kind=workspace_kind,
                permission_tier=permission_tier, allow_web_tools=allow_web_tools, task_kind=task_kind,
                model_hint=model_hint,
                expected_output=expected_output, priority=priority, body=body,
                id_editable=True, editable=True, is_operator=False,
                selected_id="", selected_layer="user",
                status="❌ ID（半角英数・ハイフン）を入力してください。",
            )
        content = roles.compose_role(
            role_id=safe_id, title=title or "", summary=summary or "",
            workspace_kind=workspace_kind or "", permission_tier=permission_tier or "",
            allow_web_tools=bool(allow_web_tools), task_kind=task_kind or "",
            model_hint=model_hint or "", expected_output=expected_output or "", priority=priority or 0, body=body or "",
        )
        res = roles.save_user_role(safe_id, content)
        df, choices = _build_role_dataframe_and_choices()
        msg = f"✅ ロール『{safe_id}』を保存しました。"
        if res.get("warning"):
            msg += f"\n⚠️ {res['warning']}"
        try:
            gr.Info(f"ロール『{safe_id}』を保存しました。")
        except Exception:
            pass
        form = select_role(safe_id)
        form = form[:-1] + (msg,)
        return (df, gr.update(choices=choices, value=safe_id)) + form
    except Exception as e:
        logger.error(f"ロール保存に失敗: {e}", exc_info=True)
        df, choices = _build_role_dataframe_and_choices()
        return (df, gr.update(choices=choices)) + _role_form_update(
            role_id=role_id, title=title, summary=summary, workspace_kind=workspace_kind,
            permission_tier=permission_tier, allow_web_tools=allow_web_tools, task_kind=task_kind,
            model_hint=model_hint,
            expected_output=expected_output, priority=priority, body=body,
            id_editable=True, editable=True, is_operator=False,
            selected_id="", selected_layer="user",
            status=f"❌ 保存できませんでした: {e}",
        )


def copy_operator_role_to_user(selected_id):
    """📋運営版を複製してユーザー層に作り、編集可能状態で開く。(df, dropdown) + FORM(18)。"""
    from agent_delegation import roles
    try:
        if not selected_id:
            df, choices = _build_role_dataframe_and_choices()
            return (df, gr.update(choices=choices)) + select_role(selected_id)
        source = roles.read_role_source(selected_id, roles.OPERATOR_LAYER)
        if not source:
            df, choices = _build_role_dataframe_and_choices()
            return (df, gr.update(choices=choices)) + _role_form_update(
                status="複製元の運営ロールが見つかりませんでした。"
            )
        roles.save_user_role(selected_id, source)
        df, choices = _build_role_dataframe_and_choices()
        form = select_role(selected_id)
        form = form[:-1] + (f"📋 運営版『{selected_id}』をユーザー層に複製しました。編集して保存できます。",)
        return (df, gr.update(choices=choices, value=selected_id)) + form
    except Exception as e:
        logger.error(f"運営ロールの複製に失敗: {e}", exc_info=True)
        df, choices = _build_role_dataframe_and_choices()
        return (df, gr.update(choices=choices)) + _role_form_update(status=f"❌ 複製できませんでした: {e}")


def ask_delete_role(selected_id, selected_layer):
    """🗑️削除（1段目）。ユーザー層のときだけ確認行を出す。(confirm_row, status)。"""
    from agent_delegation import roles
    if not selected_id or selected_layer != roles.USER_LAYER:
        return gr.update(visible=False), (
            "削除できるのは、リストから選択したユーザーロールだけです。"
            "運営分は『運営版を複製して編集』でユーザー版を作ってから削除できます。"
        )
    return gr.update(visible=True), f"⚠️ ロール『{selected_id}』を削除しますか？ この操作は元に戻せません。"


def cancel_delete_role():
    """確認をやめる。(confirm_row, status)。"""
    return gr.update(visible=False), "削除をキャンセルしました。"


def confirm_delete_role(selected_id):
    """削除確定（2段目）。(df, dropdown) + FORM(18) を返し、一覧更新＋フォーム消去＋確認行を隠す。"""
    from agent_delegation import roles
    try:
        roles.delete_user_role(selected_id)
        df, choices = _build_role_dataframe_and_choices()
        try:
            gr.Info(f"ロール『{selected_id}』を削除しました。")
        except Exception:
            pass
        form = _role_form_update(status=f"🗑️ ロール『{selected_id}』を削除しました。", hide_confirm=False)
        form = form[:14] + (gr.update(visible=False),) + form[15:]
        return (df, gr.update(choices=choices, value=None)) + form
    except Exception as e:
        logger.error(f"ロール削除に失敗: {e}", exc_info=True)
        df, choices = _build_role_dataframe_and_choices()
        form = _role_form_update(status=f"❌ 削除できませんでした: {e}")
        form = form[:14] + (gr.update(visible=False),) + form[15:]
        return (df, gr.update(choices=choices)) + form


# === ドメイン別サブモジュールからの再エクスポート ===
# 後方互換: 呼び出し側は従来どおり ui_handlers.<名前> でアクセスできる。
from ._tailscale import (
    _run_tailscale_command,
    _get_tailscale_dns_name,
    _get_tailscale_ipv4,
    _get_tailscale_serve_status,
    _get_tailscale_serve_status_json,
    _tailscale_serve_target_patterns,
    _tailscale_serve_points_to_port,
    _summarize_tailscale_serve_json,
)
from .atelier import (
    ATELIER_WORK_COLUMNS,
    ATELIER_APP_COLUMNS,
    ATELIER_APP_PENDING_COLUMNS,
    ATELIER_APP_ACTIVE_GRANT_COLUMNS,
    ATELIER_APP_WRITE_SCOPES,
    ATELIER_APP_OUTWARD_SCOPES,
    _atelier_scope_label,
    _atelier_write_scope_warning,
    _atelier_outward_scope_warning,
    _atelier_workspace_root,
    _atelier_empty_placeholder_root,
    build_atelier_file_intro,
    atelier_file_room_change_hint,
    _atelier_serve_settings,
    _room_atelier_app_api_settings,
    _room_atelier_https_only,
    atelier_app_api_room_updates,
    _atelier_app_pending_dataframe,
    _atelier_app_active_grants_dataframe,
    refresh_atelier_app_grants,
    handle_atelier_app_pending_select,
    handle_atelier_app_active_grant_select,
    handle_grant_atelier_app_scope,
    handle_deny_atelier_app_scope,
    handle_revoke_atelier_app_scope,
    _atelier_app_url,
    _atelier_https_app_url,
    _atelier_apps_dataframe,
    _atelier_app_choices,
    _build_atelier_app_detail,
    refresh_atelier_file_and_app_view,
    handle_atelier_file_select,
    handle_atelier_app_row_select,
    _atelier_app_qr_html,
    build_atelier_app_open_guide,
    handle_enable_atelier_serve_for_apps,
    handle_atelier_app_dropdown_change,
    apply_atelier_app_icon,
    handle_set_atelier_app_icon,
    _atelier_work_title,
    _atelier_works_dataframe,
    _atelier_work_choices,
    refresh_atelier_view,
    load_atelier_work_detail,
    handle_atelier_work_row_select,
    handle_delete_archived_atelier_work,
    build_atelier_serve_connection_help,
    handle_configure_tailscale_atelier_https,
    handle_save_atelier_serve_settings,
    _square_512,
    _corner_median_rgb,
    _auto_maskable_512,
)



# === ドメイン別サブモジュールからの再エクスポート（_common / twitter） ===
from ._common import (
    _is_blank,
    _normalize_file_paths,
)
from .twitter import (
    TWITTER_DRAFT_PREVIEW_CACHE_DIR,
    handle_save_twitter_settings,
    handle_twitter_auth_mode_change,
    handle_test_twitter_api,
    handle_load_twitter_settings,
    _build_twitter_media_gallery_value,
    handle_refresh_twitter_pending,
    handle_load_selected_twitter_draft,
    handle_load_twitter_draft_by_id,
    handle_load_twitter_draft_by_button,
    handle_approve_twitter_tweet,
    handle_twitter_media_file_change,
    handle_reject_twitter_tweet,
    handle_manual_twitter_draft,
    handle_refresh_twitter_timeline,
    handle_refresh_twitter_mentions,
    handle_refresh_twitter_notifications,
    handle_refresh_twitter_feed,
    handle_twitter_reply_click,
    handle_refresh_twitter_history,
    handle_twitter_history_select,
    handle_delete_twitter_history,
    handle_twitter_history_retry,
    handle_twitter_history_retry_lite,
    _twitter_save_looks_like_unloaded_defaults,
    handle_check_twitter_session,
    handle_twitter_login,
    handle_twitter_cookie_import,
    handle_refresh_twitter_tab,
    handle_refresh_twitter_all,
)



# === ドメイン別サブモジュールからの再エクスポート（delegation） ===
from ._common import _settings_status_message
from .delegation import (
    AGENT_DELEGATION_TASK_COLUMNS,
    _AGENT_DELEGATION_LIMIT_PROFILE_LABELS,
    _atelier_delegation_readiness_state,
    build_atelier_delegation_readiness,
    load_atelier_delegation_readiness,
    handle_prepare_atelier_delegation,
    handle_save_agent_delegation_settings,
    handle_save_room_agent_delegation_settings,
    load_room_persona_contract_ui,
    handle_save_room_persona_contract,
    _delegation_model_choices,
    _delegation_model_updates,
    _agent_delegation_limit_line,
    format_agent_delegation_backend_info,
    _agent_delegation_metadata_dir,
    _agent_delegation_tasks_path,
    _agent_delegation_log_path,
    _load_agent_delegation_tasks,
    _task_summary_excerpt,
    _agent_delegation_task_backend,
    _agent_delegation_task_count_summary,
    _agent_delegation_scope_label,
    _agent_delegation_progress_label,
    _agent_delegation_status_reason,
    _agent_delegation_tasks_dataframe,
    _agent_delegation_task_choices,
    _format_agent_delegation_log_line,
    _agent_delegation_task_detail_header,
    load_agent_delegation_task_log,
    refresh_agent_delegation_task_view,
    handle_agent_delegation_task_row_select,
    handle_delete_agent_delegation_task,
    handle_resume_agent_delegation_task,
    handle_clear_finished_agent_delegation_tasks,
    handle_steer_agent_delegation_task,
    handle_delegation_exec_provider_change,
)



# === ドメイン別サブモジュールからの再エクスポート（discord_line） ===
from .discord_line import (
    _DISCORD_CHANNEL_MODE_ALIASES,
    _DISCORD_CHANNEL_MODE_LABELS,
    handle_save_discord_webhook,
    _parse_csv_ids,
    _format_discord_channel_response_modes,
    _parse_discord_channel_response_modes,
    handle_load_discord_bot_settings,
    _format_global_discord_migration_status,
    handle_load_global_discord_migration_state,
    handle_migrate_global_discord_bot_to_room,
    handle_copy_global_discord_common_settings_to_room,
    handle_save_discord_bot_settings,
    handle_stop_discord_bot,
    handle_save_line_bot_settings,
    handle_stop_line_bot,
)



# === ドメイン別サブモジュールからの再エクスポート（styling / theme） ===
from .styling import (
    hex_to_rgba,
    _resolve_background_image,
    handle_refresh_background_css,
    _generate_style_from_settings,
    generate_room_style_css,
)
from .theme import (
    _get_theme_previews,
    handle_theme_tab_load,
    handle_theme_selection,
    handle_save_custom_theme,
    handle_export_theme_to_file,
    handle_apply_theme,
    handle_save_theme_settings,
    handle_theme_preview,
    handle_room_theme_reload,
)



# === ドメイン別サブモジュールからの再エクスポート（knowledge / skill） ===
from .knowledge import (
    _get_knowledge_files,
    _render_knowledge_files_table,
    _knowledge_file_selector_update,
    _get_knowledge_status,
    handle_knowledge_tab_load,
    handle_knowledge_file_upload,
    handle_knowledge_file_delete,
    handle_knowledge_reindex,
)
from .skill import (
    _procedure_ref,
    _render_skill_table,
    _skill_selector_update,
    _load_skills,
    _split_skill_ref,
    handle_skills_refresh,
    handle_skill_select,
    handle_skill_new_template,
    handle_skill_save,
    handle_skill_delete,
)



# === ドメイン別サブモジュールからの再エクスポート（notes） ===
from .notes import (
    handle_delete_letterbox_letter,
    load_notepad_content,
    refresh_letterbox,
    select_letterbox_letter,
    handle_save_notepad_click,
    handle_clear_notepad_click,
    handle_reload_notepad,
    _get_room_note_path,
    _get_creative_notes_path,
    load_creative_notes_content,
    handle_save_creative_notes,
    handle_reload_creative_notes,
    handle_clear_creative_notes,
    _parse_notes_entries,
    handle_load_creative_entries,
    handle_show_latest_creative,
    handle_creative_filter_change,
    handle_creative_selection,
    handle_save_creative_entry,
    _get_research_notes_path,
    load_research_notes_content,
    handle_save_research_notes,
    handle_reload_research_notes,
    handle_clear_research_notes,
    handle_refresh_research_threads,
    handle_research_thread_selection,
    handle_save_research_thread_body,
    handle_save_research_threads_index,
    _format_research_entry_value,
    _parse_research_entry_value,
    _get_research_entry_date_candidates,
    _normalize_research_filter_year,
    _normalize_research_filter_month,
    _collect_research_filter_dates,
    _research_entry_matches_filter,
    _research_entry_sort_key,
    _load_research_entries_for_index,
    handle_load_research_entries,
    handle_show_latest_research,
    handle_research_filter_change,
    handle_research_year_filter_change,
    handle_research_selection,
    handle_save_research_entry,
    handle_note_file_list_refresh,
    _research_subscription_rows,
    handle_research_subscription_refresh,
    handle_research_subscription_add,
    handle_research_subscription_toggle,
    handle_research_subscription_delete,
    handle_research_subscription_run_now,
    handle_research_subscription_preview,
    handle_research_import_watchlist_urls,
    handle_research_daily_cap_load,
    handle_research_daily_cap_save,
)



# === ドメイン別サブモジュールからの再エクスポート（memory） ===
from .memory import (
    load_core_memory_content,
    handle_save_core_memory,
    handle_reload_core_memory,
    handle_init_purpose_profile,
    handle_reload_purpose_profile,
    handle_save_purpose_profile,
    handle_approve_purpose_change,
    handle_discard_purpose_change,
    handle_save_diary_raw,
    handle_reload_diary_raw,
    _parse_diary_entries,
    handle_load_identity,
    handle_save_identity,
    handle_refresh_identity_edit_requests,
    _format_identity_edit_request_detail,
    handle_load_selected_identity_edit_request,
    handle_approve_identity_edit_request,
    handle_reject_identity_edit_request,
    handle_load_diary_entries,
    handle_show_latest_diary,
    handle_diary_filter_change,
    handle_diary_selection,
    handle_save_diary_entry,
    _get_date_choices_from_memory,
    handle_archive_memory_tab_select,
    handle_archive_memory_click,
    handle_maintenance_accordion_load,
    format_sleep_maintenance_status,
    handle_refresh_sleep_maintenance_status,
    handle_manual_sleep_maintenance,
    handle_update_episodic_memory,
    _get_working_memory_updates,
    handle_reload_working_memory,
    handle_reload_working_memory_metadata,
    handle_save_working_memory_metadata,
    handle_manual_dreaming,
    handle_manual_insight_only,
    handle_refresh_dream_journal,
    handle_dream_filter_change,
    handle_dream_journal_selection_from_dropdown,
    handle_show_latest_dream,
    handle_show_latest_episodic,
    _entity_choice_label,
    _resolve_entity_ref,
    handle_refresh_entity_list,
    refresh_entity_merge_candidates,
    select_entity_merge_candidate,
    approve_entity_merge_candidate,
    dismiss_entity_merge_candidate,
    handle_search_chat_log_keyword,
    handle_entity_selection_change,
    handle_save_entity_memory,
    handle_delete_entity_memory,
    handle_dormant_entity_candidates,
    handle_restore_entity_memory,
    handle_mark_entity_dormant,
    handle_merge_entity_into_target,
    handle_show_entity_index,
    _format_entity_metadata,
    handle_refresh_episodic_entries,
    handle_episodic_filter_change,
    handle_episodic_selection_from_dropdown,
    _get_working_memory_path,
    load_working_memory_content,
    load_working_memory_slots,
    handle_working_memory_slot_change,
    handle_new_working_memory_slot,
    handle_action_memory_refresh,
    handle_save_working_memory,
    handle_memos_batch_import,
    handle_core_memory_update_click,
    handle_reflect_identity_to_core,
    _get_rag_index_last_updated,
    handle_sleep_consolidation_change,
    handle_compress_episodes,
    handle_embedding_mode_change,
    handle_memory_reindex,
    handle_full_reindex,
    handle_current_log_reindex,
    handle_refresh_goals,
)
