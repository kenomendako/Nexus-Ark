import sys
import glob
import subprocess
import gradio as gr
import tempfile
import shutil
from send2trash import send2trash
import psutil
import ast
import pandas as pd
from pandas import DataFrame
import json
import traceback
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
from PIL import Image
import threading
import filetype
import zipfile
import base64
import io
import uuid
import base64 
import io      
from pathlib import Path
import textwrap
from tools.image_tools import generate_image as generate_image_tool_func
import pytz
import ijson
import time
import rag_manager

from pathlib import Path
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.docstore.document import Document

import gemini_api, config_manager, alarm_manager, room_manager, utils, constants, chatgpt_importer, claude_importer, generic_importer
from tools import gemini_importer, timer_tools, memory_tools
from utils import _overwrite_log_file
from agent.scenery_manager import generate_scenery_context
from room_manager import get_room_files_paths, get_world_settings_path
from memory_manager import load_memory_data_safe, save_memory_data
from episodic_memory_manager import EpisodicMemoryManager
from motivation_manager import MotivationManager

# --- 通知デバウンス用 ---
# 同一ルームへの連続通知を抑制するための変数
_last_save_notification_time = {}  # {room_name: timestamp}
NOTIFICATION_DEBOUNCE_SECONDS = 1.0

# --- 起動時の通知抑制用 ---
# 初期化完了までは通知を抑制（handle_initial_loadで完了時にTrueにする）
_initialization_completed = False
_initialization_completed_time = 0  # 初期化完了時刻
POST_INIT_GRACE_PERIOD_SECONDS = 5  # 初期化完了後も5秒間は通知抑制

# --- トークン数記録用 ---
_LAST_ACTUAL_TOKENS = {} # room_name -> {"prompt": int, "completion": int, "total": int}

def _format_token_display(room_name: str, estimated_count: int) -> str:
    """トークン数表示をフォーマットする。"""
    last_actual = _LAST_ACTUAL_TOKENS.get(room_name, {})
    actual_prompt = last_actual.get("prompt_tokens", 0)  # 入力トークンのみ
    actual_total = last_actual.get("total_tokens", 0)    # 入力＋出力の合計
    
    # 見積もり値のフォーマット
    est_str = f"{estimated_count / 1000:.1f}k" if estimated_count >= 1000 else str(estimated_count)
    
    # 実績値のフォーマット
    if actual_prompt > 0 or actual_total > 0:
        prompt_str = f"{actual_prompt / 1000:.1f}k" if actual_prompt >= 1000 else str(actual_prompt)
        total_str = f"{actual_total / 1000:.1f}k" if actual_total >= 1000 else str(actual_total)
        # 入力推定 / 実入力 / 実合計(入力+出力) の3つを表示
        return f"入力(推定): {est_str} / 実入力: {prompt_str} / 実合計: {total_str}"
    else:
        return f"入力トークン数(推定): {est_str}"

def handle_save_last_room(room_name: str) -> None:
    """
    選択されたルーム名をconfig.jsonに保存するだけの、何も返さない専用ハンドラ。
    Gradioのchangeイベントが不要な戻り値を受け取らないようにするために使用する。
    """
    if room_name:
        config_manager.save_config_if_changed("last_room", room_name)

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
        if len(values_tuple) > 1: # 早期リターン(1個)以外の場合のみログを出すか、文言を和らげる
             print(f"--- [Gradio Sync] 出力数を自動調整しました (返却:{len(values_tuple)} -> 期待:{expected_count}) ---")
        return values_tuple + padding
    else:
        # 多すぎる分を切り捨てる
        print(f"⚠️ [Gradio Safety] 出力数が多すぎます (返却:{len(values_tuple)} > 期待:{expected_count})。超過分を無視します。")
        return values_tuple[:expected_count]

def hex_to_rgba(hex_code, alpha):
    """HexカラーコードをRGBA文字列に変換するヘルパー関数"""
    if not hex_code or not str(hex_code).startswith("#"):
        return hex_code 
    hex_code = hex_code.lstrip('#')
    if len(hex_code) == 3: hex_code = "".join([c*2 for c in hex_code]) 
    if len(hex_code) != 6: return f"#{hex_code}"
    try:
        rgb = tuple(int(hex_code[i:i+2], 16) for i in (0, 2, 4))
        return f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, {alpha})"
    except:
        return f"#{hex_code}"


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
        mode = effective_settings.get("avatar_mode", "video")  # デフォルトは動画優先
    
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
        _, _, profile_image_path, _, _, _ = get_room_files_paths(room_name)
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
    _, _, profile_image_path, _, _, _ = get_room_files_paths(room_name)
    
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
    if config_manager.save_config_if_changed("search_provider", provider):
        config_manager.CONFIG_GLOBAL["search_provider"] = provider
        provider_names = {
            "google": "Google検索 (Gemini Native)",
            "tavily": "Tavily",
            "ddg": "DuckDuckGo",
            "disabled": "無効化"
        }
        gr.Info(f"検索プロバイダを'{provider_names.get(provider, provider)}'に変更しました。")
    
    # Tavilyが選択された場合はAPIキー入力欄を表示
    return gr.update(visible=(provider == "tavily"))


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
        return
    
    api_key = api_key.strip()
    
    # config.jsonに保存
    if config_manager.save_config_if_changed("groq_api_key", api_key):
        # グローバル変数も更新
        config_manager.GROQ_API_KEY = api_key
        gr.Info("Groq APIキーを保存しました。")
    else:
        gr.Info("Groq APIキーは既に保存されています。")

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

def _update_chat_tab_for_room_change(room_name: str, api_key_name: str):
    """
    【v7: 現在地初期化・同期FIX版】
    チャットタブ関連のUIを更新する。現在地が未設定の場合の初期化もここで行う。
    情景関連の処理は、全て司令塔である _get_updated_scenery_and_image に一任する。
    """
    # --- [Refactor] ログ読み込みをAPIキーチェック前に移動 ---
    # ルーム名が空の場合の補完
    if not room_name:
        room_list = room_manager.get_room_list_for_ui()
        room_name = room_list[0][1] if room_list else "Default"

    # 設定読み込み
    effective_settings = config_manager.get_effective_settings(room_name)
    
    # 履歴取得設定
    limit_key = effective_settings.get("api_history_limit", "all")
    add_timestamp_val = effective_settings.get("add_timestamp", False)
    display_thoughts_val = effective_settings.get("display_thoughts", True)

    # ログ読み込み (APIキーが無効でも閲覧は可能にするため)
    chat_history, mapping_list = reload_chat_log(
        room_name=room_name,
        api_history_limit_value=limit_key,
        add_timestamp=add_timestamp_val,
        display_thoughts=display_thoughts_val
    )

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

    # APIキーの決定ロジック (v8: Common Settings Fallback)
    # 1. ルーム個別設定 (override)
    # 2. グローバル設定 (last_api_key_name)
    # 3. 最初の利用可能なキー
    effective_api_key_name = override_settings.get("api_key_name")
    if not effective_api_key_name:
        effective_api_key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name")
    
    # それでもなければ、登録されているキーの最初を使う
    if not effective_api_key_name and config_manager.GEMINI_API_KEYS:
        effective_api_key_name = list(config_manager.GEMINI_API_KEYS.keys())[0]

    api_key = config_manager.GEMINI_API_KEYS.get(effective_api_key_name)
    has_valid_key = api_key and not api_key.startswith("YOUR_API_KEY")

    if not has_valid_key:
        # APIキー無効時: ログは表示するが、入力は無効化
        # 他のUI項目も適切なデフォルト値で埋める (既存のreturn tuple構造を維持)
        return (
            room_name, chat_history, mapping_list, # Logs are now included!
            gr.update(interactive=False, placeholder="まず、左の「設定」からAPIキーを設定してください。"),
            get_avatar_html(room_name, state="idle"), "", "", "", "", "", "",
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(choices=[], value=None), # archive_date_dropdown
            "（APIキーが設定されていません）",
            list(config_manager.SUPPORTED_VOICES.values())[0], # voice_dropdown
            "", True, 0.01,  # voice_style_prompt, enable_typewriter, streaming_speed
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
            f"ℹ️ *現在選択中のルーム「{room_name}」にのみ適用される設定です。*", None,
            True, gr.update(open=True),
            gr.update(value=constants.API_HISTORY_LIMIT_OPTIONS.get(constants.DEFAULT_API_HISTORY_LIMIT_OPTION, "20往復")),  # room_api_history_limit_dropdown
            gr.update(value="既定 (AIに任せる / 通常モデル)"),  # room_thinking_level_dropdown
            constants.DEFAULT_API_HISTORY_LIMIT_OPTION,  # api_history_limit_state
            gr.update(value=constants.EPISODIC_MEMORY_OPTIONS.get(constants.DEFAULT_EPISODIC_MEMORY_DAYS, "なし（無効）")),  # room_episode_memory_days_dropdown
            gr.update(value="昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** 取得エラー"),  # episodic_memory_info_display
            gr.update(value=False),  # room_enable_autonomous_checkbox
            gr.update(value=120),  # room_autonomous_inactivity_slider
            gr.update(value="00:00"),  # room_quiet_hours_start
            gr.update(value="07:00"),  # room_quiet_hours_end
            gr.update(value=None),  # room_model_dropdown (Dropdown)
            # [Phase 3] 個別プロバイダ設定
            gr.update(value="default"),  # room_provider_radio
            gr.update(visible=False),  # room_google_settings_group
            gr.update(visible=False),  # room_openai_settings_group
            gr.update(value=effective_api_key_name),  # room_api_key_dropdown (Corrected!)
            gr.update(value=None),  # room_openai_profile_dropdown
            gr.update(value=""),  # room_openai_base_url_input
            gr.update(value=""),  # room_openai_api_key_input
            gr.update(value=None),  # room_openai_model_dropdown
            gr.update(value=True),  # room_openai_tool_use_checkbox
            gr.update(value=config_manager.CONFIG_GLOBAL.get("enable_api_key_rotation", None)),  # room_rotation_dropdown
            # --- 睡眠時記憶整理 (Default values) ---
            gr.update(value=True),  # sleep_episodic
            gr.update(value=True),  # sleep_memory_index
            gr.update(value=False),  # sleep_current_log
            gr.update(value=True),  # sleep_entity
            gr.update(value=False), # sleep_compress
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
            gr.update(choices=[], value=None), # dream_date_dropdown
            gr.update(value="日付を選択すると、ここに詳細が表示されます。"), # dream_detail_text
            gr.update(choices=["すべて"], value="すべて"), # dream_year_filter
            gr.update(choices=["すべて"], value="すべて"), # dream_month_filter
            # --- [Phase 14] エピソード記憶閲覧リセット ---
            gr.update(choices=[], value=None), # episodic_date_dropdown
            gr.update(value="日付を選択してください"), # episodic_detail_text
            gr.update(choices=["すべて"], value="すべて"), # episodic_year_filter
            gr.update(choices=["すべて"], value="すべて"), # episodic_month_filter
            gr.update(value="待機中"), # episodic_update_status
            gr.update(choices=[], value=None), # entity_dropdown
            gr.update(value=""), # entity_content_editor
            gr.update(value="gemini"), # embedding_provider_radio (旧: embedding_mode_radio)
            # --- [Avatar Expressions] ---
            gr.update(value=refresh_expressions_ui(room_name)), # expressions_html
            gr.update(choices=get_all_expression_choices(room_name), value=None), # expression_target_dropdown
            gr.update(choices=[constants.CREATIVE_NOTES_FILENAME], value=constants.CREATIVE_NOTES_FILENAME), # creative_notes_file_dropdown
            gr.update(choices=[constants.RESEARCH_NOTES_FILENAME], value=constants.RESEARCH_NOTES_FILENAME) # research_notes_file_dropdown
        )

    # --- 【通常モード】 (APIキー有効) ---

    # ステップ1: UIに表示するための場所リストを先に生成
    locations_for_ui = _get_location_choices_for_ui(room_name)
    valid_location_ids = [value for _name, value in locations_for_ui if not value.startswith("__AREA_HEADER_")]

    # ステップ2: 現在地ファイルを確認し、なければ初期化
    current_location_from_file = utils.get_current_location(room_name)
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

    # ステップ3: 司令塔を呼び出す
    scenery_text, scenery_image_path = _get_updated_scenery_and_image(room_name, api_key_name)

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

    _, _, img_p, mem_p, notepad_p, _ = get_room_files_paths(room_name)
    memory_str = ""
    if mem_p and os.path.exists(mem_p):
        with open(mem_p, "r", encoding="utf-8") as f: memory_str = f.read()
    # 動画アバターをサポートするHTML生成関数を使用
    profile_image = get_avatar_html(room_name, state="idle")
    notepad_content = load_notepad_content(room_name)
    creative_notes_content = load_creative_notes_content(room_name)
    research_notes_content = load_research_notes_content(room_name)
    
    # location_dd_val を、ファイルから読み込んだ（または初期化した）値に修正
    location_dd_val = current_location_from_file

    voice_display_name = config_manager.SUPPORTED_VOICES.get(effective_settings.get("voice_id", "iapetus"), list(config_manager.SUPPORTED_VOICES.values())[0])
    voice_style_prompt_val = effective_settings.get("voice_style_prompt", "")
    safety_display_map = {
        "BLOCK_NONE": "ブロックしない", "BLOCK_LOW_AND_ABOVE": "低リスク以上をブロック",
        "BLOCK_MEDIUM_AND_ABOVE": "中リスク以上をブロック", "BLOCK_ONLY_HIGH": "高リスクのみブロック"
    }
    harassment_val = safety_display_map.get(effective_settings.get("safety_block_threshold_harassment"))
    hate_val = safety_display_map.get(effective_settings.get("safety_block_threshold_hate_speech"))
    sexual_val = safety_display_map.get(effective_settings.get("safety_block_threshold_sexually_explicit"))
    dangerous_val = safety_display_map.get(effective_settings.get("safety_block_threshold_dangerous_content"))
    core_memory_content = load_core_memory_content(room_name)

    try:
        manager = EpisodicMemoryManager(room_name)
        latest_date = manager.get_latest_memory_date()
        episodic_info_text = f"昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** {latest_date}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        episodic_info_text = "昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** 取得エラー"

    auto_settings = effective_settings.get("autonomous_settings", {})
    auto_enabled = auto_settings.get("enabled", False)
    auto_inactivity = auto_settings.get("inactivity_minutes", 120)
    quiet_start = auto_settings.get("quiet_hours_start", "00:00")
    quiet_end = auto_settings.get("quiet_hours_end", "07:00")

    # 睡眠時記憶整理設定
    sleep_consolidation = effective_settings.get("sleep_consolidation", {})
    sleep_episodic = sleep_consolidation.get("update_episodic_memory", True)
    sleep_memory_index = sleep_consolidation.get("update_memory_index", True)
    sleep_current_log = sleep_consolidation.get("update_current_log_index", False)
    sleep_entity = sleep_consolidation.get("update_entity_memory", True)
    sleep_compress = sleep_consolidation.get("compress_old_episodes", False)
    # 圧縮状況の詳細を動的に取得
    stats = EpisodicMemoryManager(room_name).get_compression_stats()
    last_date = stats["last_compressed_date"] or "なし"
    pending = stats["pending_count"]
    
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
    if raw_provider in ["zhipu", "groq", "ollama", "local"]:
        raw_provider = "openai"
    elif raw_provider not in ["default", "google", "openai"]:
        raw_provider = "default"
        
    return_provider = raw_provider

    return (
        room_name, chat_history, mapping_list,
        gr.update(interactive=True, placeholder="メッセージを入力してください (Shift+Enterで送信)。添付するにはファイルをドロップまたはクリップボタンを押してください..."),
        profile_image,
        memory_str, notepad_content, creative_notes_content, research_notes_content, load_system_prompt_content(room_name),
        core_memory_content,
        # [Fix] 選択肢が空の場合にvalueを設定してエラーになるのを防ぐ
        gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name if room_manager.get_room_list_for_ui() else None),
        gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name if room_manager.get_room_list_for_ui() else None),
        gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name if room_manager.get_room_list_for_ui() else None),
        gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name if room_manager.get_room_list_for_ui() else None),
        gr.update(choices=locations_for_ui, value=location_dd_val), # choicesとvalueを同期して返す
        scenery_text,
        voice_display_name, voice_style_prompt_val,
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
        gr.update(value=quiet_start),
        gr.update(value=quiet_end),
        gr.update(choices=list(config_manager.AVAILABLE_MODELS_GLOBAL), value=effective_settings.get("model_name", None)),  # room_model_dropdown (Dropdown)
        # [Phase 3] 個別プロバイダ設定
        # null (None) の場合に "default" にフォールバックさせて UI の選択が消えるのを防ぐ
        gr.update(value=return_provider),  # room_provider_radio
        gr.update(visible=(return_provider == "google")),  # room_google_settings_group
        gr.update(visible=(return_provider == "openai")),  # room_openai_settings_group
        gr.update(value=effective_api_key_name),  # room_api_key_dropdown
        gr.update(value=room_openai_settings.get("profile") or None),  # room_openai_profile_dropdown
        gr.update(value=room_openai_settings.get("base_url") or ""),  # room_openai_base_url_input
        gr.update(value=room_openai_settings.get("api_key") or ""),  # room_openai_api_key_input
        gr.update(choices=_room_model_choices, value=room_openai_settings.get("model") or None),  # room_openai_model_dropdown
        gr.update(value=room_openai_settings.get("tool_use_enabled") if room_openai_settings.get("tool_use_enabled") is not None else True),  # room_openai_tool_use_checkbox
        gr.update(value=override_settings.get("enable_api_key_rotation") or None),  # room_rotation_dropdown
        # --- 睡眠時記憶整理 ---
        gr.update(value=sleep_episodic),
        gr.update(value=sleep_memory_index),
        gr.update(value=sleep_current_log),
        gr.update(value=sleep_entity),
        gr.update(value=sleep_compress),
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
        gr.update(value=_generate_style_from_settings(room_name, effective_settings)),
        # --- [Phase 11/12] 夢日記リセット対応 ---
        gr.update(choices=[], value=None), # dream_date_dropdown
        gr.update(value="日付を選択すると、ここに詳細が表示されます。"), # dream_detail_text
        gr.update(choices=["すべて"], value="すべて"), # dream_year_filter
        gr.update(choices=["すべて"], value="すべて"), # dream_month_filter
        # --- [Phase 14] エピソード記憶リセット対応 ---
        gr.update(choices=[], value=None), # episodic_date_dropdown
        gr.update(value="日付を選択してください"), # episodic_detail_text
        gr.update(choices=["すべて"], value="すべて"), # episodic_year_filter
        gr.update(choices=["すべて"], value="すべて"), # episodic_month_filter
        gr.update(value=last_episodic_update), # episodic_update_status
        gr.update(choices=entity_choices, value=None), # entity_dropdown
        gr.update(value=""), # entity_content_editor
        gr.update(value="gemini" if effective_settings.get("embedding_mode", "api") == "api" else effective_settings.get("embedding_mode", "gemini")), # embedding_provider_radio (旧: embedding_mode_radio)
        gr.update(value=last_dream_time), # dream_status_display
        gr.update(value=effective_settings.get("auto_summary_enabled", False)), # room_auto_summary_checkbox
        gr.update(value=effective_settings.get("auto_summary_threshold", constants.AUTO_SUMMARY_DEFAULT_THRESHOLD), visible=effective_settings.get("auto_summary_enabled", False)), # room_auto_summary_threshold_slider
        gr.update(value=project_root), # room_project_root_input
        gr.update(value=project_exclude_dirs), # room_project_exclude_dirs_input
        gr.update(value=project_exclude_files), # room_project_exclude_files_input
        # --- [Avatar Expressions] ---
        gr.update(value=refresh_expressions_ui(room_name)), # expressions_html
        gr.update(choices=get_all_expression_choices(room_name), value=None), # expression_target_dropdown
        _get_safe_dropdown_update(room_name, 'creative', constants.CREATIVE_NOTES_FILENAME), # creative_notes_file_dropdown
        _get_safe_dropdown_update(room_name, 'research', constants.RESEARCH_NOTES_FILENAME) # research_notes_file_dropdown
    )

def _get_safe_dropdown_update(room_name: str, note_type: str, default_filename: str) -> gr.update:
    """ドロップダウンの選択肢に値が含まれているか確認し、安全な更新オブジェクトを返すヘルパー"""
    choices = room_manager.get_note_files(room_name, note_type)
    if default_filename in choices:
        return gr.update(choices=choices, value=default_filename)
    elif choices:
        return gr.update(choices=choices, value=choices[0]) # デフォルトがない場合は先頭
    else:
        # 選択肢がない場合はNoneにする（警告回避）
        return gr.update(choices=[], value=None)


def handle_initial_load(room_name: str = None, expected_count: int = 178):
    """
    【v11: 時間デフォルト対応版】
    UIセッションが開始されるたびに、UIコンポーネントの初期状態を完全に再構築する、唯一の司令塔。
    """
    # 起動時の通知抑制: 初期化開始時にフラグをリセット（初期化完了後に通知を許可）
    global _initialization_completed
    _initialization_completed = False
    
    print("--- [UI Session Init] demo.load event triggered. Reloading all configs from file. ---")
    config_manager.load_config()
    config = config_manager.CONFIG_GLOBAL

    # --- 1. 最新のルームとAPIキー情報を取得・計算 ---
    latest_room_list = room_manager.get_room_list_for_ui()
    folder_names = [folder for _, folder in latest_room_list]
    
    last_room_from_config = config.get("last_room", "Default")
    safe_initial_room = last_room_from_config
    if last_room_from_config not in folder_names:
        safe_initial_room = folder_names[0] if folder_names else "Default"

    latest_api_key_choices = config_manager.get_api_key_choices_for_ui()
    valid_key_names = [key for _, key in latest_api_key_choices]
    last_api_key_from_config = config.get("last_api_key_name")
    safe_initial_api_key = last_api_key_from_config
    if last_api_key_from_config not in valid_key_names:
        safe_initial_api_key = valid_key_names[0] if valid_key_names else None
    
    # --- 2. 司令塔として、他のハンドラのロジックを呼び出してUI更新値を生成 ---
    # `_update_chat_tab_for_room_change` は39個の値を返す
    chat_tab_updates = _update_chat_tab_for_room_change(safe_initial_room, safe_initial_api_key)
    
    df_with_ids = render_alarms_as_dataframe()
    display_df, feedback_text = get_display_df(df_with_ids), "アラームを選択してください"
    rules = config_manager.load_redaction_rules()
    rules_df_for_ui = _create_redaction_df_from_rules(rules)
    world_data_for_state = get_world_data(safe_initial_room)
    time_settings = _load_time_settings_for_room(safe_initial_room)
    time_settings_updates = (
        gr.update(value=time_settings.get("mode", "リアル連動")),
        gr.update(value=time_settings.get("fixed_season_ja", "秋")),
        gr.update(value=time_settings.get("fixed_time_of_day_ja", "夜")),
        gr.update(visible=(time_settings.get("mode", "リアル連動") == "選択する"))
    )

    # --- 3. オンボーディングとトークン計算 ---
    has_valid_key = config_manager.has_valid_api_key()
    token_count_text, onboarding_guide_update, chat_input_update = ("トークン数: (APIキー未設定)", gr.update(visible=True), gr.update(interactive=False))
    
    # 変数をデフォルト値で初期化（has_valid_keyに関係なく使用するため）
    locations_for_custom_scenery = _get_location_choices_for_ui(safe_initial_room)
    current_location_for_custom_scenery = utils.get_current_location(safe_initial_room)
    custom_scenery_dd_update = gr.update(choices=locations_for_custom_scenery, value=current_location_for_custom_scenery)
    
    time_map_en_to_ja = {"early_morning": "早朝", "morning": "朝", "late_morning": "昼前", "afternoon": "昼下がり", "evening": "夕方", "night": "夜", "midnight": "深夜"}
    now = datetime.datetime.now()
    current_time_en = utils.get_time_of_day(now.hour)
    current_time_ja = time_map_en_to_ja.get(current_time_en, "夜")
    custom_scenery_time_dd_update = gr.update(value=current_time_ja)
    
    if has_valid_key:
        token_calc_kwargs = config_manager.get_effective_settings(safe_initial_room)
        # api_key_nameが重複しないように削除（明示的に渡すため）
        token_calc_kwargs.pop("api_key_name", None)
        estimated_count = gemini_api.count_input_tokens(
            room_name=safe_initial_room, api_key_name=safe_initial_api_key,
            parts=[], **token_calc_kwargs
        )
        token_count_text = _format_token_display(safe_initial_room, estimated_count)
        onboarding_guide_update = gr.update(visible=False)
        chat_input_update = gr.update(interactive=True)

    # --- 4. [v9] その他の共通設定の初期値を決定 ---
    
    # 画像生成マルチプロバイダ設定を取得
    img_gen_provider = config.get("image_generation_provider", "gemini")
    img_gen_model = config.get("image_generation_model", "gemini-2.5-flash-image")
    available_gemini_img_models = config.get("available_image_models", {}).get("gemini", ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"])
    available_openai_img_models = config.get("available_image_models", {}).get("openai", ["gpt-image-1", "dall-e-3"])
    openai_img_settings = config.get("image_generation_openai_settings", {})
    
    common_settings_updates = (
        gr.update(value=config.get("last_model", config_manager.DEFAULT_MODEL_GLOBAL)),
        gr.update(value=config.get("debug_mode", False)),
        gr.update(value=config.get("notification_service", "discord").capitalize()),
        gr.update(value=config.get("backup_rotation_count", 10)),
        gr.update(value=config.get("pushover_user_key", "")),
        gr.update(value=config.get("pushover_app_token", "")),
        gr.update(value=config.get("notification_webhook_url", "")),
        # 画像生成マルチプロバイダ対応(3コンポーネント)
        gr.update(value=img_gen_provider),  # image_gen_provider_radio
        gr.update(choices=available_gemini_img_models, value=img_gen_model if img_gen_model in available_gemini_img_models else available_gemini_img_models[0]),  # gemini_image_model_dropdown
        gr.update(choices=available_openai_img_models, value=openai_img_settings.get("model", "gpt-image-1")),  # openai_image_model_dropdown
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

    # --- 7. [Phase 3] 内部モデル設定を取得 ---
    internal_model_settings = config_manager.get_internal_model_settings()
    internal_model_updates = (
        gr.update(value=internal_model_settings.get("processing_provider", "google")),
        gr.update(value=internal_model_settings.get("processing_model", constants.INTERNAL_PROCESSING_MODEL)),
        gr.update(value=internal_model_settings.get("summarization_provider", "google")),
        gr.update(value=internal_model_settings.get("summarization_model", constants.SUMMARIZATION_MODEL)),
        gr.update(value=internal_model_settings.get("embedding_model", "gemini-embedding-001")),
        gr.update(value=internal_model_settings.get("fallback_enabled", True)),  # [Phase 4]
    )

    # --- 8. 全ての戻り値を正しい順序で組み立てる ---
    # `initial_load_outputs`のリストに対応
    final_outputs = (
        display_df, df_with_ids, feedback_text,
        *chat_tab_updates,
        rules_df_for_ui,
        token_count_text,
        gr.update(choices=latest_api_key_choices, value=safe_initial_api_key), # api_key_dropdown
        world_data_for_state,
        *time_settings_updates,
        onboarding_guide_update,
        *common_settings_updates,
        custom_scenery_dd_update,
        custom_scenery_time_dd_update,
        *openai_updates,
        f"最終更新: {memory_index_last_updated}",  # memory_reindex_status
        f"最終更新: {current_log_index_last_updated}",  # current_log_reindex_status
        *internal_model_updates,  # [Phase 3] 内部モデル設定 (6個)
        config_manager.GROQ_API_KEY or "", # [Phase 3b] groq_api_key_input
        config_manager.LOCAL_MODEL_PATH or "", # [Phase 3c] local_model_path_input
        config_manager.TAVILY_API_KEY or "", # [Phase 3] tavily_api_key_input
        config.get("enable_api_key_rotation", True), # settings_rotation_checkbox (再取得して渡す)
    )
    
    # 初期化完了: 以降の設定変更では通知を表示する（ただし直後のgrace periodは除く）
    _initialization_completed = True
    global _initialization_completed_time
    _initialization_completed_time = time.time()
    
    return _ensure_output_count(final_outputs, expected_count)

# ルーム切り替え時の通知抑制用
_last_room_switch_time = 0
ROOM_SWITCH_GRACE_PERIOD_SECONDS = 5.0 # ルーム切り替え後の「余震」による保存通知を抑制する時間

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
    silent: bool = False,
    force_notify: bool = False
):
    # 初期化中は保存処理を完全にスキップする（無駄な I/O と通知を防ぐ）
    if not _initialization_completed:
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

    # 定数マップを使ってUIの表示名("10往復")を内部キー("10")に変換
    history_limit_key = next((k for k, v in constants.API_HISTORY_LIMIT_OPTIONS.items() if v == api_history_limit), "all")

    episode_days_key = next((k for k, v in constants.EPISODIC_MEMORY_OPTIONS.items() if v == episode_memory_days), constants.DEFAULT_EPISODIC_MEMORY_DAYS)
    thinking_level_key = next((k for k, v in constants.THINKING_LEVEL_OPTIONS.items() if v == thinking_level), "auto")

    new_settings = {
        # ルーム個別モデル設定: 「共通設定に従う」の場合はNullにリセット
        "model_name": None if provider == "default" else (model_name if model_name else None),
        "voice_id": next((k for k, v in config_manager.SUPPORTED_VOICES.items() if v == voice_name), None),
        "voice_style_prompt": voice_style_prompt.strip(),
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
            "quiet_hours_start": quiet_hours_start,
            "quiet_hours_end": quiet_hours_end
        },
        # [Phase 3] 個別プロバイダ設定
        "provider": provider if provider != "default" else None,
        "api_key_name": api_key_name if (api_key_name and provider != "default") else None,
        "openai_settings": {
            "profile": openai_profile if openai_profile else None,
            "base_url": openai_base_url if openai_base_url else "",
            "api_key": openai_api_key if openai_api_key else "",
            "model": openai_model if openai_model else "",
            "tool_use_enabled": bool(openai_tool_use)
        } if provider == "openai" else None,
        # [Phase 1.5] ローテーション設定
        "enable_api_key_rotation": enable_api_key_rotation if enable_api_key_rotation != "None" else None, 
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
            "root_path": project_root.strip(),
            "exclude_dirs": [d.strip() for d in project_exclude_dirs.split(",") if d.strip()],
            "exclude_files": [f.strip() for f in project_exclude_files.split(",") if f.strip()]
        }
    }
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
    """
    【v2: 修正版】
    個別設定のチェックボックスが変更されたときにトークン数を再計算する。
    """
    if not room_name or not api_key_name: 
        return "入力トークン数: -"
    
    estimated_count = gemini_api.count_input_tokens(
        room_name=room_name, api_key_name=api_key_name, parts=[],
        api_history_limit=api_history_limit,
        lookback_days=lookback_days,
        display_thoughts=display_thoughts, add_timestamp=add_timestamp, 
        send_current_time=send_current_time, send_thoughts=send_thoughts,
        send_notepad=send_notepad, use_common_prompt=use_common_prompt,
        send_core_memory=send_core_memory, send_scenery=enable_scenery_system,
        enable_auto_retrieval=enable_auto_retrieval,
        auto_memory_enabled=auto_memory_enabled,
        auto_summary_enabled=auto_summary_enabled,
        enable_self_awareness=enable_self_awareness,
        auto_summary_threshold=auto_summary_threshold
    )
    return _format_token_display(room_name, estimated_count)

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
    """
    【v2: 修正版】
    チャット入力欄の内容が変更されたときにトークン数を再計算する。
    """
    if not room_name or not api_key_name: return "トークン数: -"
    # ... (この関数内の以降のロジックは変更なし) ...
    textbox_content = multimodal_input.get("text", "") if isinstance(multimodal_input, dict) else ""
    file_list = multimodal_input.get("files", []) if isinstance(multimodal_input, dict) else []
    parts_for_api = []
    if textbox_content: parts_for_api.append(textbox_content)
    if file_list:
        for file_obj in file_list:
            try:
                if isinstance(file_obj, str):
                    parts_for_api.append(file_obj)
                else:
                    file_path = file_obj.name
                    file_basename = os.path.basename(file_path)
                    kind = filetype.guess(file_path)
                    if kind and kind.mime.startswith('image/'):
                        parts_for_api.append(Image.open(file_path))
                    else:
                        file_size = os.path.getsize(file_path)
                        parts_for_api.append(f"[ファイル添付: {file_basename}, サイズ: {file_size} bytes]")
            except Exception as e:
                print(f"トークン計算中のファイル処理エラー: {e}")
                error_source = "ペーストされたテキスト" if isinstance(file_obj, str) else f"ファイル「{os.path.basename(file_obj.name)}」"
                parts_for_api.append(f"[ファイル処理エラー: {error_source}]")
    estimated_count = gemini_api.count_input_tokens(
        room_name=room_name, api_key_name=api_key_name, parts=parts_for_api,
        api_history_limit=api_history_limit,
        lookback_days=lookback_days,
        display_thoughts=display_thoughts, add_timestamp=add_timestamp,
        send_current_time=send_current_time, send_thoughts=send_thoughts,
        send_notepad=send_notepad, use_common_prompt=use_common_prompt,
        send_core_memory=send_core_memory, send_scenery=send_scenery,
        enable_auto_retrieval=enable_auto_retrieval,
        auto_memory_enabled=auto_memory_enabled,
        auto_summary_enabled=auto_summary_enabled,
        enable_self_awareness=enable_self_awareness,
        auto_summary_threshold=auto_summary_threshold
    )
    return _format_token_display(room_name, estimated_count)

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
    enable_supervisor: bool = False # Supervisor機能の有効/無効
) -> Iterator[Tuple]:
    """
    【v15: グループ会話・逐次表示FIX】
    AIへのリクエスト送信、ストリーミング、APIリトライ、そしてグループ会話のターン管理の全責務を担う。
    一人応答するごとにログを保存・UIを再描画し、各AIの思考コンテキストの完全な独立性を保証する。
    """
    from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable, InternalServerError
    import openai

    main_log_f, _, _, _, _, _ = get_room_files_paths(soul_vessel_room)
    all_turn_popups = []
    final_error_message = None

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
        
        # [Phase 7] システム通知の取得と反映
        system_notices = utils.consume_system_notices()
        for notice in system_notices:
            notice_msg = f"⚠️ **システム警告**: {notice['message']}"
            chatbot_history.append((None, notice_msg))
            # ログにも保存
            utils.save_message_to_log(main_log_f, "## SYSTEM:Nexus Ark", notice_msg)
            
        chatbot_history.append((None, "▌"))
        yield (chatbot_history, mapping_list, gr.update(value={'text': '', 'files': []}),
               *([gr.update()] * 8),
               gr.update(visible=True, interactive=True),
               gr.update(interactive=False),
               gr.update(visible=False),
               current_profile_update,  # [v19] profile_image_display
               gr.update()  # [v21] style_injector (16番目)
        )

        # [v19] 司会AI機能の一時封印 (Force disable)
        enable_supervisor = False
        
        # AIごとの応答生成ループ
        all_rooms_in_scene = [soul_vessel_room] + (active_participants or [])
        
        # [v19 Fix] Supervisorが有効な場合、Supervisor自身が全参加者の管理を行うため、
        # ここで各参加者を個別にループ回すと多重ループが発生する。
        # 司会AIモード時は、主ルーム（soul_vessel_room）からの統合パスのみを実行する。
        if enable_supervisor:
            print("  - [Supervisor] 司会AIモード有効。統合パスを実行します。")
            all_rooms_in_scene = [soul_vessel_room]

        for i, current_room in enumerate(all_rooms_in_scene):
            
            # --- [最重要] ターンごとに思考の前提をゼロから構築 ---
            is_first_responder = (i == 0)
            
            # UIに思考中であることを表示
            chatbot_history, mapping_list = reload_chat_log(
                soul_vessel_room, api_history_limit, add_timestamp, display_thoughts,
                screenshot_mode, redaction_rules
            )
            chatbot_history.append((None, f"思考中 ({current_room})... ▌"))
            yield (chatbot_history, mapping_list, *([gr.update()] * 14))  # [v21] 16要素

            # APIに渡す引数を、現在のAI（current_room）のために完全に再構築
            season_en, time_of_day_en = utils._get_current_time_context(soul_vessel_room) # utilsから呼び出
            shared_location_name = utils.get_current_location(soul_vessel_room)
            
            agent_args_dict = {
                "room_to_respond": current_room, 
                "api_key_name": api_key_name,
                "global_model_from_ui": global_model, 
                "api_history_limit": api_history_limit,
                "debug_mode": debug_mode, 
                "history_log_path": main_log_f,
                "user_prompt_parts": user_prompt_parts_for_api if is_first_responder else [],
                "soul_vessel_room": soul_vessel_room,
                "active_participants": active_participants, 
                "shared_location_name": shared_location_name,
                "active_attachments": active_attachments,
                "shared_scenery_text": scenery_text_from_ui, 
                "season_en": season_en, 
                "time_of_day_en": time_of_day_en,
                "skip_tool_execution": tool_execution_successful_this_turn,
                "enable_supervisor": enable_supervisor # フラグを渡す
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
                        "user_prompt_parts": user_prompt_parts_for_api if is_first_responder else [],
                        "soul_vessel_room": soul_vessel_room,
                        "active_participants": active_participants,
                        "shared_location_name": shared_location_name,
                        "active_attachments": active_attachments,
                        "shared_scenery_text": scenery_text_from_ui,
                        "season_en": season_en,
                        "time_of_day_en": time_of_day_en,
                        "skip_tool_execution": tool_execution_successful_this_turn,
                        "enable_supervisor": enable_supervisor # フラグを渡す
                    }
                    
                    # デバッグモードがONの場合のみ、標準出力をキャプチャする
                    # 【重要】model_nameはストリームの途中で取得できた値を保持する
                    # LangGraphの最終stateでは後続ノードによりmodel_nameが欠落する可能性があるため
                    captured_model_name = None
                    if debug_mode:
                        with utils.capture_prints() as captured_output:
                            for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args_dict):
                                if mode == "initial_count":
                                    initial_message_count = chunk
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
                            if mode == "initial_count":
                                initial_message_count = chunk
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
                        chatbot_history.append((None, retry_message))
                        yield (chatbot_history, mapping_list, *([gr.update()] * 14))  # [v21] 16要素
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
                
                ai_text_messages = []
                other_messages = [] # ToolMessageなど
                
                for msg in raw_new_messages:
                    if isinstance(msg, AIMessage):
                        content = utils.get_content_as_string(msg)
                        if content and content.strip():
                            ai_text_messages.append((len(content), msg))
                    else:
                        other_messages.append(msg)
                
                # AIメッセージがあれば、最も長いものを1つ選ぶ
                best_ai_message = None
                if ai_text_messages:
                    # 長さで降順ソートして先頭を取得
                    ai_text_messages.sort(key=lambda x: x[0], reverse=True)
                    best_ai_message = ai_text_messages[0][1]
                
                print(f"--- [DEBUG] best_ai_message exists: {best_ai_message is not None} ---")
                
                # リストを再構築（順序は Tool -> AI の順が自然だが、元の順序をなるべく保つ）
                # ここではシンプルに [ツール実行報告たち] + [AIの最終回答] とする
                new_messages = other_messages
                if best_ai_message:
                    new_messages.append(best_ai_message)
                
                # [2026-01-10] 実送信トークン量の記録
                if final_state and "actual_token_usage" in final_state:
                    _LAST_ACTUAL_TOKENS[current_room] = final_state["actual_token_usage"]
                    print(f"  - [Token] 実績値を記録しました: {final_state['actual_token_usage']}")
                
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
                                # 英語曜日（Sun等）と日本語曜日（日）の両形式に対応
                                # 形式1: 2025-12-21 (Sun) 10:59:45（英語、括弧前スペース）
                                # 形式2: 2025-12-21(日) 10:59:30（日本語、括弧前スペースなし）
                                timestamp_pattern = r'\n\n\d{4}-\d{2}-\d{2}\s*\([A-Za-z月火水木金土日]{1,3}\)\s*\d{2}:\d{2}:\d{2}(?: \| .*)?$'
                                existing_timestamp_match = re.search(timestamp_pattern, content_str)
                                if existing_timestamp_match:
                                    # AIが模倣したタイムスタンプを検出・除去
                                    print(f"--- [AI模倣タイムスタンプ除去] ---")
                                    print(f"  - 除去されたパターン: {existing_timestamp_match.group()}")
                                    content_str = re.sub(timestamp_pattern, '', content_str)
                                
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
                                
                                # 使用モデル名を取得（実際に推論に使用されたモデル名が final_state に格納されている）
                                # 使用モデル名の取得（優先順位: 1.ストリーム中に取得したmodel_name, 2.final_state, 3.effective_settings）
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
                        
                        elif isinstance(msg, ToolMessage):
                            # 【アナウンスのみ保存するツール】constants.pyで一元管理
                            # 生の検索結果（大量の会話ログ）はログに保存せず、
                            # 「ツールを使用しました」というアナウンスだけを保存する。
                            if msg.name in constants.TOOLS_SAVE_ANNOUNCEMENT_ONLY:
                                formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                                # 生の結果（[RAW_RESULT]）は含めない。アナウンスのみ。
                                content_to_log = formatted_tool_result if formatted_tool_result else f"🛠️ ツール「{msg.name}」を実行しました。"
                                header = f"## SYSTEM:tool_result:{msg.name}:{msg.tool_call_id}"
                                print(f"--- [ログ最適化] '{msg.name}' のアナウンスのみ保存（生の結果は除外） ---")
                            else:
                                formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                                content_to_log = f"{formatted_tool_result}\n\n[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]" if formatted_tool_result else f"[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]"
                                # ツール名とコールIDをヘッダーに埋め込む
                                header = f"## SYSTEM:tool_result:{msg.name}:{msg.tool_call_id}"
                        
                        side_effect_tools = ["plan_main_memory_edit", "plan_secret_diary_edit", "plan_notepad_edit", "plan_world_edit", "set_personal_alarm", "set_timer", "set_pomodoro_timer"]
                        if isinstance(msg, ToolMessage) and msg.name in side_effect_tools and "Error" not in str(msg.content) and "エラー" not in str(msg.content):
                            tool_execution_successful_this_turn = True
                            print(f"--- [リトライガード設定] 副作用のあるツール '{msg.name}' の成功を記録しました。 ---")
                        
                        if header and content_to_log:
                            for participant_room in all_rooms_in_scene:
                                log_f, _, _, _, _, _ = get_room_files_paths(participant_room)
                                if log_f:
                                    # --- 【修正】二重書き込み防止チェック ---
                                    try:
                                        current_log = utils.load_chat_log(log_f)
                                        if current_log:
                                            last_entry = current_log[-1]
                                            if _is_redundant_log_update(last_entry.get('content', ''), content_to_log):
                                                print(f"--- [Deduplication] Skipping redundant message for {participant_room} (Suffix/Exact match) ---")
                                                continue
                                    except Exception as e:
                                        print(f"Deduplication check failed: {e}")
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
                        formatted_text = ""
                        if formatted_last_message and formatted_last_message[1]:
                            if isinstance(formatted_last_message[1], str):
                                formatted_text = formatted_last_message[1]
                            else:
                                # タプル（画像など）の場合はタイプライターをスキップ
                                chatbot_history.append(formatted_last_message)
                                yield (chatbot_history, mapping_list, *([gr.update()] * 14))  # [v21] 16要素
                                typewriter_completed_successfully = True
                                continue
                        
                        if formatted_text:
                            # アニメーション用のカーソルを追加して開始
                            chatbot_history.append((None, "▌"))
                            streamed_text = ""  # ★重要: 毎回初期化（前回の応答が引き継がれないように）
                            
                            for char in formatted_text:
                                streamed_text += char
                                chatbot_history[-1] = (None, streamed_text + "▌")
                                yield (chatbot_history, mapping_list, *([gr.update()] * 14))  # [v21] 16要素
                                time.sleep(streaming_speed)
                            
                            # タイプライター完了後、フォーマット済みの最終形を表示
                            # （生テキストではなく、reload_chat_logから取得したフォーマット済みを使用）
                            chatbot_history[-1] = formatted_last_message
                            yield (chatbot_history, mapping_list, *([gr.update()] * 14))  # [v21] 16要素
                        
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
                log_f, _, _, _, _, _ = get_room_files_paths(room_name)
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
        
        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
        new_scenery_text, scenery_image, token_count_text = "（更新失敗）", None, "トークン数: (更新失敗)"
        try:
            season_en, time_of_day_en = utils._get_current_time_context(soul_vessel_room)
            _, _, new_scenery_text = generate_scenery_context(soul_vessel_room, api_key, season_en=season_en, time_of_day_en=time_of_day_en)
            scenery_image = utils.find_scenery_image(soul_vessel_room, utils.get_current_location(soul_vessel_room), season_en=season_en, time_of_day_en=time_of_day_en)
        except Exception as e:
            print(f"--- 警告: 応答後の情景更新に失敗しました (API制限の可能性): {e} ---")
        try:
            token_calc_kwargs = config_manager.get_effective_settings(soul_vessel_room, global_model_from_ui=global_model)
            
            # トークン計算用のAPIキー決定: ルーム個別設定があればそれを優先
            token_api_key_name = token_calc_kwargs.get("api_key_name", api_key_name)
            
            token_calc_kwargs.pop("api_history_limit", None)
            token_calc_kwargs.pop("api_history_limit", None)
            token_calc_kwargs.pop("api_key_name", None)
            
            estimated_count = gemini_api.count_input_tokens(
            room_name=soul_vessel_room, 
            api_key_name=api_key_name, 
            api_history_limit=api_history_limit, 
            parts=[], 
            **token_calc_kwargs
        )
            token_count_text = _format_token_display(soul_vessel_room, estimated_count)
        except Exception as e:
            print(f"--- 警告: 応答後のトークン数更新に失敗しました: {e} ---")

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
                    ai_content = last_response[1]
                    if isinstance(ai_content, str):
                        final_expression = extract_expression_from_response(ai_content, soul_vessel_room)
        except Exception as e:
            print(f"--- [Avatar] 表情抽出エラー: {e} ---")

        final_profile_update = gr.update(value=get_avatar_html(soul_vessel_room, state=final_expression))

        # [v21] 現在地連動背景: ツール使用後に背景CSSも更新
        effective_settings_for_style = config_manager.get_effective_settings(soul_vessel_room)
        style_css_update = gr.update(value=_generate_style_from_settings(soul_vessel_room, effective_settings_for_style))

        yield (final_chatbot_history, final_mapping_list, gr.update(), token_count_text,
               location_dropdown_update, new_scenery_text,
               final_df_with_ids, final_df, scenery_image,
               current_console_content, current_console_content,
               gr.update(visible=False, interactive=True), gr.update(interactive=True),
               gr.update(visible=False),
               final_profile_update, # [v19] Stop Animation
               style_css_update # [v21] Sync Background
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
    enable_supervisor: bool = False  # [v18] Supervisor機能の有効/無効
):
    """
    【v9: 添付ファイル永続化FIX版】新規メッセージの送信を処理する司令塔。
    """
    # 1. ユーザー入力を解析 (変更なし)
    textbox_content = multimodal_input.get("text", "") if multimodal_input else ""
    file_input_list = multimodal_input.get("files", []) if multimodal_input else []
    user_prompt_from_textbox = textbox_content.strip() if textbox_content else ""

    # --- [v9: 空送信ガード] ---
    # テキスト入力がなく、かつファイルも添付されていない場合は、何もせずに終了する
    if not user_prompt_from_textbox and not file_input_list:
        # 戻り値の数は unified_streaming_outputs の要素数と一致させる必要がある (16個)
        # 既存のUIの状態を維持するため、全て gr.update() を返す
        yield (gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), gr.update(), gr.update())
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
                    log_message_parts.append(f"[ファイル添付: {permanent_path}]")
                    files_to_send_api.append(permanent_path)
                else:
                    print(f"--- [ファイル永続化警告] 未知または無効な添付ファイルオブジェクトです: {file_obj} ---")

            except Exception as e:
                print(f"--- [ファイル永続化エラー] 添付ファイルの処理中にエラーが発生しました: {e} ---")
                traceback.print_exc()
                log_message_parts.append(f"[ファイル添付エラー: {e}]")
                
    full_user_log_entry = "\n".join(log_message_parts).strip()

    if not full_user_log_entry:
        effective_settings = config_manager.get_effective_settings(soul_vessel_room)
        add_timestamp = effective_settings.get("add_timestamp", False)
        history, mapping = reload_chat_log(soul_vessel_room, api_history_limit, add_timestamp)
        # 戻り値の数を16個に合わせる
        yield (history, mapping, *([gr.update()] * 10), gr.update(visible=False), gr.update(interactive=True), gr.update(), gr.update())
        return

    # 2. ユーザーの発言を、セッション参加者全員のログに書き込む
    all_participants_in_session = [soul_vessel_room] + (active_participants or [])
    for room_name in all_participants_in_session:
        log_f, _, _, _, _, _ = get_room_files_paths(room_name)
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
        
        print(f"--- [情景画像AI共有] 設定チェック: send_scenery = {send_scenery_image_enabled}, mode = {scenery_send_mode} ---")
        
        if send_scenery_image_enabled:
            # 現在の情景画像パスを取得
            season_en, time_of_day_en = utils._get_current_time_context(soul_vessel_room)
            current_location = utils.get_current_location(soul_vessel_room)
            current_scenery_image = utils.find_scenery_image(
                soul_vessel_room, current_location, season_en, time_of_day_en
            )
            
            print(f"  - 現在地: {current_location}, 季節: {season_en}, 時間帯: {time_of_day_en}")
            print(f"  - 画像パス: {current_scenery_image}")
            
            if current_scenery_image and os.path.exists(current_scenery_image):
                # room_config から「最後に送信した画像パス」を取得
                room_config = room_manager.get_room_config(soul_vessel_room) or {}
                last_sent_image = room_config.get("last_sent_scenery_image")
                
                print(f"  - 最後に送信した画像: {last_sent_image}")
                
                # 送信判定: 「毎ターン」モードなら常に送信、「変更時のみ」なら画像が異なる場合のみ
                should_send = (scenery_send_mode == "毎ターン") or (current_scenery_image != last_sent_image)
                
                if should_send:
                    reason = "毎ターン送信" if scenery_send_mode == "毎ターン" else "新しい景色を検出"
                    print(f"  - ✅ {reason}！画像をAIに送信します")
                    
                    # 画像をリサイズしてBase64エンコード（コスト削減）
                    resize_result = utils.resize_image_for_api(current_scenery_image, max_size=512)
                    
                    if resize_result:
                        # ★修正: resize_image_for_apiはタプル(base64_string, format)を返す
                        encoded_image, output_format = resize_result
                        mime_type = f"image/{output_format}"
                        print(f"  - ✅ 画像リサイズ成功 (Base64: {len(encoded_image)} chars, format: {output_format})")
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
                        print(f"  - ✅ 画像送信完了＆記録更新")

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
        enable_supervisor=enable_supervisor  # [v18] Supervisor機能の有効/無効
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
    enable_supervisor: bool = False  # [v18] Supervisor機能の有効/無効
):
    """
    【v3: 遅延解消版】発言の再生成を処理する司令塔。
    """
    if not selected_message or not room_name:
        gr.Warning("再生成の起点となるメッセージが選択されていません。")
        yield (gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), console_content, console_content,
               gr.update(visible=True, interactive=True), gr.update(interactive=True), gr.update(), gr.update(), gr.update())
        return

    # 1. ログを巻き戻し、再送信するユーザー発言を取得
    log_f, _, _, _, _, _ = get_room_files_paths(room_name)
    # SYSTEMメッセージもAI応答と同様に扱い、直前のユーザー発言から再生成する
    is_ai_or_system_message = selected_message.get("role") in ("AGENT", "SYSTEM")

    restored_input_text = None
    deleted_timestamp = None
    if is_ai_or_system_message:
        restored_input_text, deleted_timestamp = utils.delete_and_get_previous_user_input(log_f, selected_message)
    else: # ユーザー発言の場合
        restored_input_text, _ = utils.delete_user_message_and_after(log_f, selected_message)

    if restored_input_text is None:
        gr.Error("ログの巻き戻しに失敗しました。再生成できません。")
        effective_settings = config_manager.get_effective_settings(room_name)
        add_timestamp = effective_settings.get("add_timestamp", False)
        history, mapping = reload_chat_log(room_name, api_history_limit, add_timestamp)
        yield (history, mapping, gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), console_content, console_content,
               gr.update(visible=True, interactive=True), gr.update(interactive=True), gr.update(), gr.update(), gr.update())  # [v21] 16要素
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
        enable_supervisor=enable_supervisor  # [v18] Supervisor機能の有効/無効
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
            # [修正] 画像がない場合でも自動生成を行わずにテキストのみ更新する（APIコスト削減）
            # 以前はここで handle_generate_or_regenerate_scenery_image を呼んでいた
            pass

        return scenery_text, scenery_image_path

    except Exception as e:
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
        # nexus_ark.pyのoutputsは9個
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
        clear_form = (gr.update(value=""), gr.update(value=""), gr.update(value=""), gr.update(value=""), gr.update(value=""))

        # ドロップダウンの選択肢を更新（4つの主要ドロップダウン分）
        dd_updates = (gr.update(choices=updated_room_list),) * 4

        return (*dd_updates, *clear_form)

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

def handle_delete_room(confirmed: str, folder_name_to_delete: str, api_key_name: str, current_room_name: str = None, expected_count: int = 155):
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
            # 【Fix】expected_count を明示的に渡すことで、もしデフォルト値が古くても不整合を防ぐ
            return handle_room_change_for_all_tabs(
                new_main_room_folder, api_key_name, "", expected_count=expected_count
            )
        else:
            # ケース2: これが最後のルームだった場合
            gr.Warning("全てのルームが削除されました。新しいルームを作成してください。")
            # 契約数(65)に合わせてUIをリセットするための値を返す
            # initial_load_chat_outputs (47個) に対応
            empty_chat_updates = (
                None, [], [], gr.update(interactive=False, placeholder="ルームを作成してください。"), 
                None, "", "", "", "",  # room_name, chatbot, mapping, input, profile, memory, notepad, system_prompt, core_memory
                gr.update(choices=[], value=None), gr.update(choices=[], value=None), 
                gr.update(choices=[], value=None), gr.update(choices=[], value=None),  # room_dropdown, alarm_dd, timer_dd, manage_dd
                gr.update(choices=[], value=None),  # location_dropdown
                "（ルームがありません）",  # current_scenery_display
                list(config_manager.SUPPORTED_VOICES.values())[0], "", True, 0.01,  # voice_dd, voice_style, typewriter, speed
                0.8, 0.95, *[gr.update()]*4,  # temp, top_p, 4 safety settings
                False, # display_thoughts
                False, # send_thoughts
                True,  # enable_auto_retrieval
                True,  # add_timestamp
                True,  # send_current_time
                True,  # send_notepad
                True,  # use_common_prompt
                True,  # send_core_memory
                False, # send_scenery
                "変更時のみ", # scenery_send_mode
                False, # auto_memory_enabled
                True,  # enable_self_awareness
                "ℹ️ *ルームを選択してください*", None,  # room_settings_info, scenery_image
                True, gr.update(open=False),  # enable_scenery_system, profile_scenery_accordion
                gr.update(value=constants.API_HISTORY_LIMIT_OPTIONS.get(constants.DEFAULT_API_HISTORY_LIMIT_OPTION, "20往復")),  # room_api_history_limit_dropdown
                constants.DEFAULT_API_HISTORY_LIMIT_OPTION,  # api_history_limit_state
                gr.update(value=constants.EPISODIC_MEMORY_OPTIONS.get(constants.DEFAULT_EPISODIC_MEMORY_DAYS, "なし（無効）")),  # room_episode_memory_days_dropdown
                gr.update(value="昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** -"),  # episodic_memory_info_display
                gr.update(value=False),  # room_enable_autonomous_checkbox
                gr.update(value=120),  # room_autonomous_inactivity_slider
                gr.update(value="00:00"),  # room_quiet_hours_start
                gr.update(value="07:00"),  # room_quiet_hours_end
                *[gr.update()]*10,  # room_model_dropdown, provider_radio, google_group, openai_group, api_key_dd, openai_profile, base_url, api_key, model_dd, tool_use
                # --- 睡眠時記憶整理 ---
                gr.update(value=True),  # sleep_consolidation_episodic_cb
                gr.update(value=True),  # sleep_consolidation_memory_index_cb
                gr.update(value=False), # sleep_consolidation_current_log_cb
                gr.update(value=True),  # sleep_consolidation_entity_memory_cb
                gr.update(value=False), # sleep_consolidation_compress_cb
                gr.update(value="未実行"), # compress_episodes_status
                # --- [v25] テーマ設定 ---
                gr.update(value=False), # room_theme_enabled_checkbox
                *[gr.update()]*8,       # chat_style to accent_soft (9 items total)
                *[gr.update()]*13,      # theme detailed (13 items)
                *[gr.update()]*11,      # bg images (11 items)
                *[gr.update()]*9,       # bg sync (9 items)
                gr.update(), # save_room_theme_button
                gr.update(value=""), # style_injector
                *[gr.update()]*4, # dream diary (4 items)
                gr.update(value="未実行"), # dream_status_display
                gr.update(value=False), # room_auto_summary_checkbox
                gr.update(value=constants.AUTO_SUMMARY_DEFAULT_THRESHOLD, visible=False), # room_auto_summary_threshold_slider
                "", # room_project_root_input
                "", # room_project_exclude_dirs_input
                "", # room_project_exclude_files_input
                gr.update(value=refresh_expressions_ui(None)), # expressions_html
                gr.update(choices=get_all_expression_choices(None), value=None) # expression_target_dropdown
            )

            # ケース2の全項目を組み立てる (unified_full_room_refresh_outputs に合わせる)
            world_outputs = (None, None, "", None) # 4 items
            session_outputs = ([], "", []) # 3 items
            tail_outputs = (
                gr.update(value=[]), # redaction_rules_df
                gr.update(choices=[], value=None), # archive_date_dropdown
                gr.update(value="リアル連動"), # time_mode_radio
                gr.update(value="秋"), # fixed_season
                gr.update(value="夜"), # fixed_time_of_day
                gr.update(visible=False), # fixed_time_controls
                [], # attachments_df
                "現在アクティブな添付ファイルはありません。", # active_attachments_display
                gr.update(choices=[], value=None), # custom_scenery_location
                "トークン数: (ルーム未選択)", # token_count
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
    
def load_core_memory_content(room_name: str) -> str:
    """core_memory.txtの内容を安全に読み込むヘルパー関数。"""
    if not room_name: return ""
    core_memory_path = os.path.join(constants.ROOMS_DIR, room_name, "core_memory.txt")
    # core_memory.txt は ensure_room_files で作成されない場合があるため、ここで存在チェックと作成を行う
    if not os.path.exists(core_memory_path):
        try:
            with open(core_memory_path, "w", encoding="utf-8") as f:
                f.write("") # 空ファイルを作成
            return ""
        except Exception as e:
            print(f"コアメモリファイルの作成に失敗: {e}")
            return "（コアメモリファイルの作成に失敗しました）"

    with open(core_memory_path, "r", encoding="utf-8") as f:
        return f.read()

def handle_save_core_memory(room_name: str, content: str) -> str:
    """コアメモリの保存ボタンのイベントハンドラ。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return content

    # ▼▼▼【ここに追加】▼▼▼
    room_manager.create_backup(room_name, 'core_memory')

    core_memory_path = os.path.join(constants.ROOMS_DIR, room_name, "core_memory.txt")
    try:
        with open(core_memory_path, "w", encoding="utf-8") as f:
            f.write(content)
        gr.Info(f"「{room_name}」のコアメモリを保存しました。")
        return content
    except Exception as e:
        gr.Error(f"コアメモリの保存エラー: {e}")
        return content

def handle_reload_core_memory(room_name: str) -> str:
    """コアメモリの再読込ボタンのイベントハンドラ。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return ""
    content = load_core_memory_content(room_name)
    gr.Info(f"「{room_name}」のコアメモリを再読み込みしました。")
    return content

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
    if file_obj is None:
        return gr.update(choices=[], value=None), gr.update(visible=False), []

    try:
        choices = claude_importer.get_claude_thread_list(file_obj.name)

        if not choices:
            gr.Warning("これは有効なClaudeエクスポートファイルではないか、会話が含まれていません。")
            return gr.update(choices=[], value=None), gr.update(visible=False), []

        # UIを更新し、選択肢リストをStateに渡す
        return gr.update(choices=choices, value=None), gr.update(visible=True), choices

    except Exception as e:
        gr.Warning("Claudeエクスポートファイルの処理中にエラーが発生しました。")
        print(f"Error processing Claude export file: {e}")
        traceback.print_exc()
        return gr.update(choices=[], value=None), gr.update(visible=False), []

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
        return gr.update(choices=[], value=None), gr.update(visible=False), []

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
            return gr.update(choices=[], value=None), gr.update(visible=False), []

        sorted_choices = sorted(choices)
        # ドロップダウンを更新し、フォームを表示し、選択肢リストをStateに渡す
        return gr.update(choices=sorted_choices, value=None), gr.update(visible=True), sorted_choices

    except (ijson.JSONError, IOError, StopIteration, Exception) as e:
        gr.Warning("これは有効なChatGPTエクスポートファイルではないようです。ファイルを確認してください。")
        print(f"Error processing ChatGPT export file: {e}")
        traceback.print_exc()
        return gr.update(choices=[], value=None), gr.update(visible=False), []


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

def handle_chatbot_selection(room_name: str, api_history_limit_state: str, mapping_list: list, evt: gr.SelectData):
    if not room_name or evt.index is None or not mapping_list:
        return None, gr.update(visible=False), gr.update(interactive=True)

    try:
        clicked_ui_index = evt.index[0]
        if not (0 <= clicked_ui_index < len(mapping_list)):
            gr.Warning(f"クリックされたメッセージを特定できませんでした (UI index out of bounds).")
            return None, gr.update(visible=False), gr.update(interactive=True)

        log_f, _, _, _, _, _ = get_room_files_paths(room_name)
        # 全ログをロードする
        raw_history = utils.load_chat_log(log_f)

        # マッピングリストから、ログ全体における「絶対インデックス」を取得
        original_log_index = mapping_list[clicked_ui_index]

        # 絶対インデックスが、全ログの範囲内にあるかチェック
        if 0 <= original_log_index < len(raw_history):
            # 全ログに対して、絶対インデックスで直接アクセスする
            selected_msg = raw_history[original_log_index]
            is_ai_message = selected_msg.get("responder") != "user"
            return (
                selected_msg,
                gr.update(visible=True),
                gr.update(interactive=is_ai_message)
            )
        else:
            # こちらが本当の "out of bounds"
            gr.Warning(f"クリックされたメッセージを特定できませんでした (Original log index out of bounds). Index: {original_log_index}, Log Length: {len(raw_history)}")
            return None, gr.update(visible=False), gr.update(interactive=True)

    except Exception as e:
        print(f"チャットボット選択中のエラー: {e}"); traceback.print_exc()
        return None, gr.update(visible=False), gr.update(interactive=True)

def handle_delete_button_click(
    confirmed: str, 
    message_to_delete: Optional[Dict[str, str]], 
    room_name: str, 
    api_history_limit: str,
    add_timestamp: bool,
    screenshot_mode: bool,
    redaction_rules: list,
    display_thoughts: bool
    ):
    # ▼▼▼【ここから下のブロックを書き換え】▼▼▼
    if str(confirmed).lower() != 'true' or not message_to_delete:
        # ユーザーがキャンセルしたか、対象メッセージがない場合は選択状態を解除してボタンを非表示にする
        return gr.update(), gr.update(), None, gr.update(visible=False), "" # 最後にリセット用の "" を追加
    # ▲▲▲【書き換えここまで】▲▲▲

    log_f, _, _, _, _, _ = get_room_files_paths(room_name)
    deleted_timestamp = utils.delete_message_from_log(log_f, message_to_delete)
    if deleted_timestamp:
        gr.Info("ログからメッセージを削除しました。")
        # [SessionArousal] 対応するArousalデータも削除
        if message_to_delete.get("role") in ("AGENT", "SYSTEM"):
            import session_arousal_manager
            today_str = datetime.datetime.now().strftime('%Y-%m-%d')
            session_arousal_manager.remove_arousal_session(room_name, today_str, deleted_timestamp)
    else:
        gr.Error("メッセージの削除に失敗しました。詳細はターミナルを確認してください。")

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
    return history, mapping_list, None, gr.update(visible=False), "" # 最後にリセット用の "" を追加

def format_history_for_gradio(
    messages: List[Dict[str, str]],
    current_room_folder: str,
    add_timestamp: bool,
    display_thoughts: bool = True, 
    screenshot_mode: bool = False,
    redaction_rules: List[Dict] = None,
    absolute_start_index: int = 0
) -> Tuple[List[Tuple], List[int]]:

    """
    (v27: Stable Thought Log with Backward Compatibility)
    ログ辞書のリストをGradioのChatbotコンポーネントが要求する形式に変換する。
    新しい 'THOUGHT:' プレフィックス形式と、古い '【Thoughts】' ブロック形式の両方を
    正しく解釈して、同じスタイルで表示する後方互換性を持つパーサーを実装。
    """
    if not messages:
        return [], []

    gradio_history, mapping_list = [], []

    if not add_timestamp:
        timestamp_pattern = re.compile(r'\n\n\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}$')
    
    current_room_config = room_manager.get_room_config(current_room_folder) or {}
    user_display_name = current_room_config.get("user_display_name", "ユーザー")
    agent_name_cache = {}

    proto_history = []
    for i, msg in enumerate(messages, start=absolute_start_index):
        role, content = msg.get("role"), msg.get("content", "").strip()
        responder_id = msg.get("responder")
        if not responder_id: continue

        if not add_timestamp:
            content = timestamp_pattern.sub('', content)

        text_part = re.sub(r"\[(?:Generated Image|ファイル添付):.*?\]", "", content, flags=re.DOTALL).strip()
        media_matches = list(re.finditer(r"\[(?:Generated Image|ファイル添付): ([^\]]+?)\]", content))

        if text_part or (role == "SYSTEM" and not media_matches):
            proto_history.append({"type": "text", "role": role, "responder": responder_id, "content": text_part, "log_index": i})

        for match in media_matches:
            path_str = match.group(1).strip()
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

            if path_obj.exists() and is_allowed:
                proto_history.append({"type": "media", "role": role, "responder": responder_id, "path": path_str, "log_index": i})
            else:
                print(f"--- [警告] 無効または安全でない画像パスをスキップしました: {path_str} ---")

        if not text_part and not media_matches and role != "SYSTEM":
             proto_history.append({"type": "text", "role": role, "responder": responder_id, "content": "", "log_index": i})



    for item in proto_history:
        mapping_list.append(item["log_index"])
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
                for rule in redaction_rules:
                    find_str = rule.get("find")
                    if find_str:
                        replace_str = rule.get("replace", "")
                        color = rule.get("color")
                        escaped_find = html.escape(find_str)
                        escaped_replace = html.escape(replace_str)

                        if speaker_name:
                            speaker_name = speaker_name.replace(find_str, replace_str)

                        if color:
                            replacement_html = f'<span style="background-color: {color};">{escaped_replace}</span>'
                            content_to_parse = content_to_parse.replace(escaped_find, replacement_html)
                        else:
                            content_to_parse = content_to_parse.replace(escaped_find, escaped_replace)

            # --- [新ロジック v3: [THOUGHT]タグ対応・最終版パーサー] ---
            final_markdown = ""
            speaker_prefix = f"**{speaker_name}:**\n\n" if speaker_name else (f"**{responder_id}:**\n\n" if role == "SYSTEM" else "")

            # --- [新ロジック v4: 汎用コードブロック対応パーサー] ---

            # display_thoughtsがFalseの場合、思考ログを物理的に除去する
            content_for_parsing = content_to_parse
            if not display_thoughts:
                content_for_parsing = re.sub(r"(\[THOUGHT\][\s\S]*?\[/THOUGHT\])", "", content_for_parsing, flags=re.IGNORECASE)
                content_for_parsing = re.sub(r"【/?Thoughts】", "", content_for_parsing, flags=re.IGNORECASE)
                lines = content_for_parsing.split('\n')
                content_for_parsing = "\n".join([line for line in lines if not line.strip().upper().startswith("THOUGHT:")])

            # --- [新ロジック v5: メタデータタグの非表示化] ---
            # 【表情】…表情名…、<persona_emotion.../>、<memory_trace.../> を表示から除去
            content_for_parsing = re.sub(r"【表情】…\w+…", "", content_for_parsing)
            content_for_parsing = re.sub(r"<persona_emotion\s+[^>]*/>", "", content_for_parsing)
            content_for_parsing = re.sub(r"<memory_trace\s+[^>]*/>", "", content_for_parsing)

            # --- [新ロジック v6: THOUGHTタグの整合性修正] ---
            # [THOUGHT] があるが [/THOUGHT] がない（閉じ忘れ）場合、
            # 冒頭の [THOUGHT] を除去して表示崩れを防ぐ
            if re.search(r"\[THOUGHT\]", content_for_parsing, flags=re.IGNORECASE) and not re.search(r"\[/THOUGHT\]", content_for_parsing, flags=re.IGNORECASE):
                content_for_parsing = re.sub(r"^\[THOUGHT\]\s*", "", content_for_parsing, flags=re.IGNORECASE)

            content_for_parsing = content_for_parsing.strip()

            # 思考ログのタグを、標準的なコードブロック記法に統一する
            content_for_parsing = re.sub(r"\[/?THOUGHT\]", "```", content_for_parsing, flags=re.IGNORECASE)
            content_for_parsing = re.sub(r"【/?Thoughts】", "```", content_for_parsing, flags=re.IGNORECASE)
            
            lines = content_for_parsing.split('\n')
            processed_lines = []
            in_thought_block = False
            for line in lines:
                if line.strip().upper().startswith("THOUGHT:"):
                    if not in_thought_block:
                        processed_lines.append("```")
                        in_thought_block = True
                    processed_lines.append(line.split(":", 1)[1].strip())
                else:
                    if in_thought_block:
                        processed_lines.append("```")
                        in_thought_block = False
                    processed_lines.append(line)
            if in_thought_block:
                processed_lines.append("```")
            content_for_parsing = "\n".join(processed_lines)

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
                        # 文字置き換えのspanタグを含む場合：
                        # spanタグを保持しつつ、残りをHTMLエスケープしてMarkdown解釈を防ぐ
                        span_pattern = re.compile(r'(<span style="[^"]*">[^<]*</span>)')
                        spans = span_pattern.findall(inner_content)
                        placeholder_map = {}
                        for i, span in enumerate(spans):
                            placeholder = f"__SPAN_PH_{i}__"
                            placeholder_map[placeholder] = span
                            inner_content = inner_content.replace(span, placeholder, 1)
                        # プレースホルダー以外をHTMLエスケープ
                        escaped_content = html.escape(inner_content)
                        # プレースホルダーを元のspanタグに戻す
                        for placeholder, span in placeholder_map.items():
                            escaped_content = escaped_content.replace(placeholder, span)
                        # 改行を<br>に置換（\nは削除して二重改行を防ぐ）
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
            if is_user:
                gradio_history.append((final_markdown, None))
            else:
                gradio_history.append((None, final_markdown))

        elif item["type"] == "media":
            media_tuple = (item["path"], os.path.basename(item["path"]))
            gradio_history.append((media_tuple, None) if is_user else (None, media_tuple))


    return gradio_history, mapping_list


def reload_chat_log(
    room_name: Optional[str],
    api_history_limit_value: str,
    add_timestamp: bool,
    display_thoughts: bool = True,
    screenshot_mode: bool = False,
    redaction_rules: List[Dict] = None,
    *args, **kwargs
):
    if not room_name:
        return [], []

    log_f,_,_,_,_,_ = get_room_files_paths(room_name)
    # log_f が存在しなくても、logs/ ディレクトリがあれば過去ログを読めるようにする
    # (月またぎでまだ今月のログがない場合や、インポート直後など)
    if not log_f:
        return [], []
        
    # log_f (例: logs/2026-02.txt) が存在しなくても、
    # その親フォルダ (logs/) が存在すれば utils.load_chat_log に任せる。
    # utils.load_chat_log は logs/ があれば全ロードしてくれる。
    if not os.path.exists(log_f):
        logs_dir = os.path.dirname(log_f)
        if not os.path.exists(logs_dir):
             # ディレクトリすらなければ本当にログがない
             return [], []

    full_raw_history = utils.load_chat_log(log_f)


    # --- ▼▼▼ 「本日分」対応 ▼▼▼ ---
    if api_history_limit_value == "today":
        # エピソード記憶の有無に応じて適切な日付でフィルタ
        from gemini_api import _get_effective_today_cutoff
        cutoff_date = _get_effective_today_cutoff(room_name)
        date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})')
        
        # cutoff_date以降の最初のメッセージを探す
        today_start_index = len(full_raw_history)  # デフォルトは末尾（何も見つからない場合）
        
        for i, item in enumerate(full_raw_history):
            content = item.get('content', '')
            if isinstance(content, str):
                match = date_pattern.search(content)
                if match:
                    msg_date = match.group(1)
                    if msg_date >= cutoff_date:
                        today_start_index = i
                        break  # 最初に見つかったcutoff_date以降のメッセージで停止
        
        absolute_start_index = today_start_index
        visible_history = full_raw_history[absolute_start_index:]
        
        # 【最低表示数の保証】エピソード記憶作成後でも最低N往復分は表示
        min_messages = constants.MIN_TODAY_LOG_FALLBACK_TURNS * 2
        if len(visible_history) < min_messages:
            # 本日分が不足 → ログ末尾から最低数を確保
            absolute_start_index = max(0, len(full_raw_history) - min_messages)
            visible_history = full_raw_history[absolute_start_index:]
    else:
        # 従来のロジック：往復数または全ログ
        display_turns = _get_display_history_count(api_history_limit_value)
        absolute_start_index = max(0, len(full_raw_history) - (display_turns * 2))
        visible_history = full_raw_history[absolute_start_index:]

    # --- ▲▲▲ 修正ここまで ▲▲▲ ---

    history, mapping_list = format_history_for_gradio(
        messages=visible_history,
        current_room_folder=room_name,
        add_timestamp=add_timestamp,
        display_thoughts=display_thoughts,
        screenshot_mode=screenshot_mode,
        redaction_rules=redaction_rules,
        absolute_start_index=absolute_start_index
    )
    print(f"[DEBUG:format_history] Formatted history count: {len(history)}")

    return history, mapping_list

def handle_wb_add_place_button_click(area_selector_value: Optional[str]):
    if not area_selector_value:
        gr.Warning("まず、場所を追加したいエリアを選択してください。")
        return "place", gr.update(visible=False), "#### 新しい場所の作成"
    return "place", gr.update(visible=True), "#### 新しい場所の作成"

def handle_save_memory_click(room_name, text_content):
    if not room_name: gr.Warning("ルームが選択されていません。"); return gr.update()

    # ▼▼▼【ここに追加】▼▼▼
    room_manager.create_backup(room_name, 'memory')

    _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if not memory_txt_path: gr.Error(f"「{room_name}」の記憶パス取得失敗。"); return gr.update()
    try:
        with open(memory_txt_path, "w", encoding="utf-8") as f:
            f.write(text_content)
        # room_config.json にも更新日時を記録
        config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        config = room_manager.get_room_config(room_name) or {}
        config["memory_last_updated"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        gr.Info(f"'{room_name}' の記憶を保存しました。")
        return gr.update(value=text_content)
    except Exception as e: gr.Error(f"記憶保存エラー: {e}"); traceback.print_exc(); return gr.update()

def handle_reload_memory(room_name: str) -> Tuple[str, gr.update]:
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return "", gr.update(choices=[], value=None)

    gr.Info(f"「{room_name}」の記憶を再読み込みしました。")

    memory_content = ""
    _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if memory_txt_path and os.path.exists(memory_txt_path):
        with open(memory_txt_path, "r", encoding="utf-8") as f:
            memory_content = f.read()

    # 日付選択肢も同時に更新する
    new_dates = _get_date_choices_from_memory(room_name)
    date_dropdown_update = gr.update(choices=new_dates, value=new_dates[0] if new_dates else None)

    return memory_content, date_dropdown_update


# --- 主観的記憶（日記）のRAW再読込 ---
def handle_reload_memory_raw(room_name: str) -> Tuple[str, gr.update]:
    """日記のRAWエディタ用に全文を読み込む"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return "", gr.update(choices=[], value=None)

    gr.Info(f"「{room_name}」の日記を再読み込みしました。")

    memory_content = ""
    _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if memory_txt_path and os.path.exists(memory_txt_path):
        with open(memory_txt_path, "r", encoding="utf-8") as f:
            memory_content = f.read()

    new_dates = _get_date_choices_from_memory(room_name)
    date_dropdown_update = gr.update(choices=new_dates, value=new_dates[0] if new_dates else None)

    return memory_content, date_dropdown_update


# --- 主観的記憶（日記）：エントリベースのハンドラ（新規追加） ---

def _parse_diary_entries(content: str) -> list:
    """
    日記からタイムスタンプセクションをパースしてエントリリストを返す。
    形式: ### YYYY-MM-DD や ** YYYY-MM-DD ** などの見出し
    """
    entries = []
    
    # 日付パターン（様々な形式に対応）
    # ### 2026-01-15 形式
    # ** 2026-01-15 ** 形式
    # 2026-01-15 のみの行
    date_pattern = re.compile(r'^(?:###|\*\*|##)?\s*(\d{4}-\d{2}-\d{2})(?:\s*\*\*)?.*$', re.MULTILINE)
    
    # 日付でセクションを分割
    matches = list(date_pattern.finditer(content))
    
    for i, match in enumerate(matches):
        date_str = match.group(1)
        start_pos = match.start()
        
        # 次のマッチまでまたは終端まで
        if i + 1 < len(matches):
            end_pos = matches[i + 1].start()
        else:
            end_pos = len(content)
        
        section = content[start_pos:end_pos].strip()
        # 見出し行を除いたコンテンツ
        header_end = match.end() - match.start()
        entry_content = section[header_end:].strip()
        
        if entry_content:
            entries.append({
                "timestamp": date_str,
                "date": date_str,
                "content": entry_content,
                "raw_section": section
            })
    
    return entries


def handle_load_diary_entries(room_name: str):
    """日記のエントリを読み込み、UIを更新"""
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), ""
    
    _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if not memory_txt_path or not os.path.exists(memory_txt_path):
        gr.Info("日記はまだありません。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), ""
    
    with open(memory_txt_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    if not content.strip():
        gr.Info("日記は空です。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), content
    
    entries = _parse_diary_entries(content)
    
    if not entries:
        # エントリが見つからない場合は全文を1エントリとして扱う
        gr.Info("日付形式のエントリが見つかりません。RAW編集を使用してください。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), content
    
    # 年・月リストを抽出
    years = set()
    months = set()
    choices = []
    
    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")
        if len(date_str) >= 7:
            years.add(date_str[:4])
            months.add(date_str[5:7])
        
        # ラベル作成
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{date_str} - {preview}"
        choices.append((label, str(i)))
    
    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))
    
    print(f"--- [UI] {len(entries)}件のエントリを読み込みました。 ---")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value=None),
        content  # RAWエディタにも反映
    )


def handle_show_latest_diary(room_name: str):
    """日記を読み込み、最新のエントリを自動的に選択して表示する。
    
    Returns:
        (year_filter, month_filter, entry_dropdown, editor_content, raw_editor_content)
    """
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), "", ""
    
    _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if not memory_txt_path or not os.path.exists(memory_txt_path):
        gr.Info("日記はまだありません。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), "", ""
    
    with open(memory_txt_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    if not content.strip():
        gr.Info("日記は空です。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), "", content
    
    entries = _parse_diary_entries(content)
    
    if not entries:
        gr.Info("日付形式のエントリが見つかりません。RAW編集を使用してください。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), "", content
    
    # 年・月リストを抽出
    years = set()
    months = set()
    choices = []
    
    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")
        if len(date_str) >= 7:
            years.add(date_str[:4])
            months.add(date_str[5:7])
        
        # ラベル作成
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{date_str} - {preview}"
        choices.append((label, str(i)))
    
    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))
    
    # 最新のエントリ（インデックス0）を選択して詳細を表示
    latest_entry = entries[0]
    latest_content = latest_entry.get("content", "")
    
    gr.Info("最新の日記を表示しています。")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value="0"),  # 最新エントリを選択
        latest_content,  # エディタに最新エントリの内容を表示
        content  # RAWエディタにも反映
    )


def handle_diary_filter_change(room_name: str, year: str, month: str):
    """日記のフィルタ変更時にドロップダウン選択肢を更新"""
    if not room_name:
        return gr.update(choices=[])
    
    _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if not memory_txt_path or not os.path.exists(memory_txt_path):
        return gr.update(choices=[])
    
    with open(memory_txt_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    entries = _parse_diary_entries(content)
    
    choices = []
    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")
        
        match_year = (year == "すべて" or (len(date_str) >= 4 and date_str[:4] == year))
        match_month = (month == "すべて" or (len(date_str) >= 7 and date_str[5:7] == month))
        
        if match_year and match_month:
            preview = entry["content"][:30].replace("\n", " ")
            if len(entry["content"]) > 30:
                preview += "..."
            label = f"{date_str} - {preview}"
            choices.append((label, str(i)))
    
    return gr.update(choices=choices, value=None)


def handle_diary_selection(room_name: str, selected_idx: str):
    """日記のエントリ選択時に詳細を表示"""
    if not room_name or selected_idx is None:
        return ""
    
    try:
        idx = int(selected_idx)
        _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
        if not memory_txt_path or not os.path.exists(memory_txt_path):
            return ""
        
        with open(memory_txt_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        entries = _parse_diary_entries(content)
        
        if 0 <= idx < len(entries):
            entry = entries[idx]
            return entry["content"]
        return ""
    except (ValueError, IndexError) as e:
        print(f"日記エントリ選択エラー: {e}")
        return ""


def handle_save_diary_entry(room_name: str, selected_idx: str, new_content: str):
    """選択された日記エントリを保存（エントリ内容のみ更新）"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return new_content
    
    if selected_idx is None:
        gr.Warning("エントリが選択されていません。RAW編集から全文を編集してください。")
        return new_content
    
    try:
        idx = int(selected_idx)
        _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
        if not memory_txt_path or not os.path.exists(memory_txt_path):
            return new_content
        
        with open(memory_txt_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        entries = _parse_diary_entries(content)
        
        if 0 <= idx < len(entries):
            old_section = entries[idx]["raw_section"]
            date_str = entries[idx]["date"]
            # 日付ヘッダーを保持して内容のみ更新
            new_section = f"### {date_str}\n{new_content.strip()}"
            
            updated_content = content.replace(old_section, new_section, 1)
            
            with open(memory_txt_path, "w", encoding="utf-8") as f:
                f.write(updated_content)
            
            gr.Info(f"日記エントリを保存しました。")
            return new_content
        else:
            gr.Warning("選択されたエントリが見つかりません。")
            return new_content
    except Exception as e:
        gr.Error(f"保存エラー: {e}")
        return new_content

def _get_date_choices_from_memory(room_name: str) -> List[str]:
    """memory_main.txtの日記セクションから日付見出しを抽出する。"""
    if not room_name:
        return []
    try:
        _, _, _, memory_main_path, _, _ = get_room_files_paths(room_name)
        if not memory_main_path or not os.path.exists(memory_main_path):
            return []

        with open(memory_main_path, 'r', encoding='utf-8') as f:
            content = f.read()

        diary_match = re.search(r'##\s*(?:日記|Diary).*?(?=^##\s+|$)', content, re.DOTALL | re.IGNORECASE)
        if not diary_match:
            return []

        diary_content = diary_match.group(0)
        date_pattern = r'(?:###|\*\*)?\s*(\d{4}-\d{2}-\d{2})'
        dates = re.findall(date_pattern, diary_content)

        # 重複を除き、降順で返す
        return sorted(list(set(dates)), reverse=True)
    except Exception as e:
        print(f"日記の日付抽出中にエラー: {e}")
        return []

def handle_archive_memory_tab_select(room_name: str):
    """「記憶」タブが表示されたときに、日付選択肢を更新する。"""
    dates = _get_date_choices_from_memory(room_name)
    return gr.update(choices=dates, value=dates[0] if dates else None)

def handle_archive_memory_click(
    confirmed: any, # Gradioから渡される型が不定なため、anyで受け取る
    room_name: str,
    api_key_name: str,
    archive_date: str
):
    """「アーカイブ実行」ボタンのイベントハンドラ。"""
    # ▼▼▼ 修正点1: キャンセル判定をより厳格に ▼▼▼
    if str(confirmed).lower() != 'true':
        gr.Info("アーカイブ処理をキャンセルしました。")
        return gr.update(), gr.update()

    if not all([room_name, api_key_name, archive_date]):
        gr.Warning("ルーム、APIキー、アーカイブする日付をすべて選択してください。")
        return gr.update(), gr.update()

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Warning(f"APIキー '{api_key_name}' が有効ではありません。")
        return gr.update(), gr.update()

    gr.Info("古い日記のアーカイブ処理を開始します。この処理には少し時間がかかります...")

    from tools import memory_tools
    result = memory_tools.archive_old_diary_entries.func(
        room_name=room_name,
        api_key=api_key,
        archive_until_date=archive_date
    )

    if "成功" in result:
        gr.Info(f"✅ {result}")
    else:
        gr.Error(f"アーカイブ処理に失敗しました。詳細: {result}")

    # ▼▼▼ 修正点2: 戻り値を自身で正しく構築する ▼▼▼
    # handle_reload_memoryを呼び出さず、必要な処理を直接行う
    new_memory_content = ""
    _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if memory_txt_path and os.path.exists(memory_txt_path):
        with open(memory_txt_path, "r", encoding="utf-8") as f:
            new_memory_content = f.read()

    new_dates = _get_date_choices_from_memory(room_name)
    date_dropdown_update = gr.update(choices=new_dates, value=new_dates[0] if new_dates else None)

    return new_memory_content, date_dropdown_update

def handle_update_episodic_memory(room_name: str, api_key_name: str):
    """エピソード記憶の更新ボタンのハンドラ"""
    # 初期状態の戻り値 (何も変更しない)
    no_change = (gr.update(), gr.update(), gr.update())

    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        yield no_change
        return

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        yield no_change
        return

    # 1. UIをロック (ボタン:更新中..., チャット欄:無効化)
    yield (
        gr.update(value="⏳ 更新中...", interactive=False), 
        gr.update(interactive=False, placeholder="エピソード記憶を更新中です...お待ちください"),
        gr.update() 
    )

    gr.Info(f"「{room_name}」のエピソード記憶（要約）を作成・更新しています...")
    
    try:
        manager = EpisodicMemoryManager(room_name)
        result_msg = manager.update_memory(api_key)
        gr.Info(f"✅ {result_msg}")
    except Exception as e:
        error_msg = f"エピソード記憶の更新中にエラーが発生しました: {e}"
        print(error_msg)
        traceback.print_exc()
        gr.Error(error_msg)
    
    try:
        latest_date = manager.get_latest_memory_date()
        new_info_text = f"昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** {latest_date}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        new_info_text = "昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** 取得エラー"

    # 2. UIのロックを解除 (ボタン:元通り, チャット欄:有効化)
    status_text = f"最終更新: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    
    # 実行結果を room_config.json に保存
    try:
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        if os.path.exists(room_config_path):
            with open(room_config_path, "r", encoding="utf-8") as f:
                room_config = json.load(f)
            room_config["last_episodic_update"] = status_text
            with open(room_config_path, "w", encoding="utf-8") as f:
                json.dump(room_config, f, indent=2, ensure_ascii=False)
    except:
        pass

    yield gr.update(value="エピソード記憶を作成 / 更新", interactive=True), gr.update(interactive=True, placeholder="メッセージを入力してください (Shift+Enterで送信)..."), gr.update(value=status_text)

def handle_manual_dreaming(room_name: str, api_key_name: str):
    """睡眠時記憶整理（夢想プロセス）を手動で実行する"""
    if not room_name:
        return gr.update(), "ルーム名が指定されていません。"
    
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        return gr.update(), "⚠️ 有効なAPIキーが設定されていません。"

    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, api_key)
        
        # 夢を見る（洞察生成 & エンティティ更新 & 目標更新）
        result_msg = dm.dream_with_auto_level()
        
        # 最終実行日時を取得
        last_time = dm.get_last_dream_time()
        
        return gr.update(), last_time

    except Exception as e:
        print(f"Manual dreaming error: {e}")
        traceback.print_exc()
        return gr.update(), f"エラーが発生しました: {e}"

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

def handle_refresh_dream_journal(room_name: str):
    """夢日記（insights.json）を読み込み、Dropdown の選択肢とフィルタの選択肢を返す"""
    if not room_name:
        return gr.update(choices=[]), "", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])

    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, "dummy_key")
        insights = dm._load_insights()
        
        # 最新順にソート (created_at は YYYY-MM-DD HH:MM:SS 形式)
        insights.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        
        choices = []
        years = set()
        months = set()
        
        for item in insights:
            created_at = item.get("created_at", "")
            if not created_at:
                continue
            
            date_part = created_at.split(" ")[0] # YYYY-MM-DD
            y, m, d = date_part.split("-")
            years.add(y)
            months.add(m)
            
            topic = item.get("trigger_topic", "話題なし")
            # トピックを15文字で短縮
            topic_short = (topic[:15] + "..") if len(topic) > 15 else topic
            
            # ラベルは「日付 (トピック短縮)」、値は「created_at (一意なキー)」
            label = f"{date_part} ({topic_short})"
            choices.append((label, created_at))
            
        year_choices = ["すべて"] + sorted(list(years), reverse=True)
        month_choices = ["すべて"] + sorted(list(months))
        
        gr.Info(f"{len(choices)}件の夢日記を読み込みました。")
        return (
            gr.update(choices=choices, value=None),
            "日付を選択すると、ここに詳細が表示されます。",
            gr.update(choices=year_choices, value="すべて"),
            gr.update(choices=month_choices, value="すべて")
        )
        
    except Exception as e:
        print(f"夢日記読み込みエラー: {e}")
        return gr.update(choices=[]), f"エラー: {e}", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])

def handle_dream_filter_change(room_name: str, year: str, month: str):
    """年・月のフィルタ変更に合わせて、日付ドロップダウンの選択肢を絞り込む"""
    if not room_name:
        return gr.update(choices=[])
    
    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, "dummy_key")
        insights = dm._load_insights()
        insights.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        
        filtered_choices = []
        for item in insights:
            created_at = item.get("created_at", "")
            if not created_at: continue
            
            date_part = created_at.split(" ")[0]
            y, m, _d = date_part.split("-")
            
            if year != "すべて" and y != year:
                continue
            if month != "すべて" and m != month:
                continue
                
            topic = item.get("trigger_topic", "話題なし")
            topic_short = (topic[:15] + "..") if len(topic) > 15 else topic
            label = f"{date_part} ({topic_short})"
            filtered_choices.append((label, created_at))
            
        return gr.update(choices=filtered_choices, value=None)
    except Exception as e:
        print(f"夢日記フィルタリングエラー: {e}")
        return gr.update(choices=[])

def handle_dream_journal_selection_from_dropdown(room_name: str, selected_created_at: str):
    """夢日記のドロップダウンから選択した際、詳細を表示する"""
    if not room_name or not selected_created_at:
        return ""
    
    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, "dummy_key")
        insights = dm._load_insights()
        
        # created_at が一意のキーとして動作する
        selected_dream = next((item for item in insights if item.get("created_at") == selected_created_at), None)
        
        if selected_dream:
            # 詳細テキストを構築
            details = (
                f"【日付】 {selected_dream.get('created_at')}\n"
                f"【トリガー】 {selected_dream.get('trigger_topic')}\n\n"
                f"## 💡 得られた洞察 (Insight)\n"
                f"{selected_dream.get('insight', '（記録なし）')}\n\n"
                f"## 💭 夢の日記 (Dream Log)\n"
                f"{selected_dream.get('log_entry', '（記録なし）')}\n\n"
                f"## 🧭 今後の指針 (Strategy)\n"
                f"{selected_dream.get('strategy', '（記録なし）')}"
            )
            return details
            
        return "選択された日記が見つかりませんでした。"
    except Exception as e:
        return f"詳細表示エラー: {e}"


def handle_show_latest_dream(room_name: str):
    """
    夢日記を読み込み、最新のエントリを自動的に選択して表示する。
    
    Returns:
        (date_dropdown, detail_text, year_filter, month_filter)
    """
    if not room_name:
        return gr.update(choices=[]), "", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])
    
    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, "dummy_key")
        insights = dm._load_insights()
        
        if not insights:
            gr.Info("夢日記がありません。")
            return gr.update(choices=[]), "夢日記がまだありません。", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])
        
        # 最新順にソート
        insights.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        
        choices = []
        years = set()
        months = set()
        
        for item in insights:
            created_at = item.get("created_at", "")
            if not created_at:
                continue
            
            date_part = created_at.split(" ")[0]
            y, m, d = date_part.split("-")
            years.add(y)
            months.add(m)
            
            topic = item.get("trigger_topic", "話題なし")
            topic_short = (topic[:15] + "..") if len(topic) > 15 else topic
            label = f"{date_part} ({topic_short})"
            choices.append((label, created_at))
        
        year_choices = ["すべて"] + sorted(list(years), reverse=True)
        month_choices = ["すべて"] + sorted(list(months))
        
        # 最新のエントリを選択して詳細を表示
        latest = insights[0]
        latest_created_at = latest.get("created_at", "")
        
        details = (
            f"【日付】 {latest.get('created_at')}\\n"
            f"【トリガー】 {latest.get('trigger_topic')}\\n\\n"
            f"## 💡 得られた洞察 (Insight)\\n"
            f"{latest.get('insight', '（記録なし）')}\\n\\n"
            f"## 💭 夢の日記 (Dream Log)\\n"
            f"{latest.get('log_entry', '（記録なし）')}\\n\\n"
            f"## 🧭 今後の指針 (Strategy)\\n"
            f"{latest.get('strategy', '（記録なし）')}"
        )
        
        gr.Info("最新の夢日記を表示しています。")
        return (
            gr.update(choices=choices, value=latest_created_at),
            details,
            gr.update(choices=year_choices, value="すべて"),
            gr.update(choices=month_choices, value="すべて")
        )
        
    except Exception as e:
        print(f"夢日記最新表示エラー: {e}")
        traceback.print_exc()
        return gr.update(choices=[]), f"エラー: {e}", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])


def handle_show_latest_episodic(room_name: str):
    """
    エピソード記憶を読み込み、最新のエントリを自動的に選択して表示する。
    
    Returns:
        (date_dropdown, detail_text, year_filter, month_filter)
    """
    if not room_name:
        return gr.update(choices=[]), "", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])
    
    try:
        # EpisodicMemoryManagerを使用（月次ファイル対応）
        manager = EpisodicMemoryManager(room_name)
        episodes = manager._load_memory()
        
        if not episodes:
            gr.Info("エピソード記憶がありません。")
            return gr.update(choices=[]), "エピソード記憶がまだありません。", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])
        
        # 最新順にソート
        episodes.sort(key=lambda x: x.get("date", ""), reverse=True)
        
        choices_set = set()
        years = set()
        months = set()
        
        for ep in episodes:
            date_str = ep.get("date", "")
            if not date_str:
                continue
            
            parts = date_str.split("-")
            if len(parts) >= 2:
                years.add(parts[0])
                months.add(parts[1])
            
            choices_set.add(date_str)
        
        choices = sorted(list(choices_set), reverse=True)
        year_choices = ["すべて"] + sorted(list(years), reverse=True)
        month_choices = ["すべて"] + sorted(list(months))
        
        # 最新のエントリを選択して詳細を表示
        latest = episodes[0]
        latest_date = latest.get("date", "")
        summary = latest.get("summary", "（なし）")
        
        gr.Info("最新のエピソード記憶を表示しています。")
        return (
            gr.update(choices=choices, value=latest_date),
            summary,
            gr.update(choices=year_choices, value="すべて"),
            gr.update(choices=month_choices, value="すべて")
        )
        
    except Exception as e:
        print(f"エピソード記憶最新表示エラー: {e}")
        traceback.print_exc()
        return gr.update(choices=[]), f"エラー: {e}", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])



# --- 📌 エンティティ記憶 (Entity Memory) ハンドラ ---

def handle_refresh_entity_list(room_name: str):
    """エンティティの一覧を取得してドロップダウンを更新する"""
    if not room_name:
        return gr.update(choices=[], value=None), ""
    
    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    entities = em.list_entries()

    if not entities:
        return gr.update(choices=[], value=None), "エンティティがまだ登録されていません。"
    
    # 名称順に並び替える
    entities.sort()
    
    return gr.update(choices=entities, value=None), "エンティティを選択してください。"

def handle_search_chat_log_keyword(room_name: str, keyword: str) -> gr.update:
    """
    指定されたキーワードを含むログ月を検索し、ドロップダウンの選択肢をフィルタリングする。
    キーワードが空の場合は全件表示に戻す。
    """
    if not room_name:
        return gr.update()
        
    base_path = os.path.join(constants.ROOMS_DIR, room_name)
    logs_dir = os.path.join(base_path, constants.LOGS_DIR_NAME)
    
    if not os.path.exists(logs_dir):
        return gr.update(choices=["最新"], value="最新")

    # 全ファイル取得 (年月リスト構築ロジックの再利用)
    all_files = glob.glob(os.path.join(logs_dir, "*.txt"))
    month_map = {} # "YYYY-MM" -> path
    for fpath in all_files:
        filename = os.path.basename(fpath)
        if re.match(r"\d{4}-\d{2}\.txt", filename):
            month = filename.replace(".txt", "")
            month_map[month] = fpath
    
    # 検索実行
    if not keyword or not keyword.strip():
        # キーワードなし -> 全件表示
        choices = ["最新"] + sorted(list(month_map.keys()), reverse=True)
        return gr.update(choices=choices, value="最新")
    
    matched_months = []
    
    # キーワード検索
    for month, fpath in month_map.items():
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                if keyword in content:
                    matched_months.append(month)
        except:
            continue
            
    if not matched_months:
        gr.Info(f"キーワード「{keyword}」を含むログは見つかりませんでした。")
        # 0件でも空にするよりは全件表示する方が親切か？ あるいは空リストにするか。
        # ここではリストはそのまま（あるいは "該当なし" を出す？）だが、
        # フィルタ結果として空を返すと操作不能になるので、Warningを出してリセットしない、あるいは空にする。
        # 直感的には「絞り込み結果0件」を表示すべき。
        return gr.update(choices=[], value=None)
    
    # ヒットした月 + (ヒットした中に最新月が含まれるかは不明だが、「最新」という概念はファイルではないので検索対象外)
    # だが、「最新」ログ（=現在進行中の月）も当然検索対象に含めたい。
    # 現在の月を特定して検索結果に含める必要がある。
    # しかし month_map には全ての月が含まれているはずなので、current_month がヒットしていればそれでよい。
    # UI上、「最新」という選択肢を残すかどうか。
    # 「最新」は便利ショートカットなので、検索時は具体的な月 "YYYY-MM" を指定させる形で良い。
    
    choices = sorted(matched_months, reverse=True)
    gr.Info(f"{len(choices)} 件のログファイルがヒットしました。")
    return gr.update(choices=choices, value=choices[0] if choices else None)

def handle_entity_selection_change(room_name: str, entity_name: str):
    """選択されたエンティティの内容を読み込む"""
    if not room_name or not entity_name:
        return ""
    
    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    content = em.read_entry(entity_name)
    
    if content is None or content.startswith("Error:"):
        return content or "読み込みに失敗しました。"
    
    return content

def handle_save_entity_memory(room_name: str, entity_name: str, content: str):
    """エンティティの内容を保存する"""
    if not room_name or not entity_name:
        return
    
    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    # 手動保存時は上書きモード
    path = em._get_entity_path(entity_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def handle_delete_entity_memory(room_name: str, entity_name: str):
    """エンティティを削除する"""
    if not room_name or not entity_name:
        return gr.update(), gr.update()
    
    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    
    success = em.delete_entry(entity_name)
    
    if success:
        gr.Info(f"エンティティ '{entity_name}' を削除しました。")
        # リストを再取得
        entities = em.list_entries()
        return gr.update(choices=entities, value=None), ""
    else:
        gr.Error(f"エンティティ '{entity_name}' の削除に失敗しました。")
        return gr.update(), gr.update()

# --- [Phase 14] Episodic Memory Browser Handlers ---

def handle_refresh_episodic_entries(room_name: str):
    """エピソード記憶（episodic_memory.json）を読み込み、Dropdown の選択肢とフィルタの選択肢を返す"""
    if not room_name:
        return gr.update(choices=[], value=None), gr.update(value="日付を選択してください"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて")
        
    try:
        manager = EpisodicMemoryManager(room_name)
        data = manager._load_memory()
        
        if not data:
            return gr.update(choices=[], value=None), gr.update(value="エピソード記憶がまだ作成されていません。"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて")
            
        # 日付リスト（最新順）- 重複を排除
        entries_set = set()
        years = set()
        months = set()
        
        for item in data:
            d = item.get('date', '').strip()
            if not d: continue
            
            entries_set.add(d)
            
            # 年・月抽出 (YYYY-MM-DD or YYYY-MM-DD~YYYY-MM-DD)
            # 範囲の場合は開始日を使う
            base_date = d.split('~')[0].split('～')[0].strip()
            if len(base_date) >= 7:
                years.add(base_date[:4])
                months.add(base_date[5:7])
        
        entries = sorted(list(entries_set), reverse=True)
        year_choices = ["すべて"] + sorted(list(years), reverse=True)
        month_choices = ["すべて"] + sorted(list(months))
        
        return (
            gr.update(choices=entries, value=None),
            gr.update(value="日付を選択すると、ここに内容が表示されます。"),
            gr.update(choices=year_choices, value="すべて"),
            gr.update(choices=month_choices, value="すべて")
        )
    except Exception as e:
        print(f"Error refreshing episodic entries: {e}")
        return gr.update(choices=[], value=None), gr.update(value=f"読み込みエラー: {e}"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて")

def handle_episodic_filter_change(room_name: str, year: str, month: str):
    """年・月のフィルタ変更に合わせて、エピソードドロップダウンの選択肢を絞り込む"""
    if not room_name:
        return gr.update(choices=[], value=None)
        
    try:
        manager = EpisodicMemoryManager(room_name)
        data = manager._load_memory()
        
        filtered_entries_set = set()
        for item in data:
            d = item.get('date', '').strip()
            if not d: continue
            
            # 判定用日付（範囲なら開始日）
            base_date = d.split('~')[0].split('～')[0].strip()
            
            match_year = (year == "すべて" or base_date.startswith(year))
            match_month = (month == "すべて" or (len(base_date) >= 7 and base_date[5:7] == month))
            
            if match_year and match_month:
                filtered_entries_set.add(d)
                
        filtered_entries = sorted(list(filtered_entries_set), reverse=True)
        return gr.update(choices=filtered_entries, value=None)
    except Exception as e:
        print(f"Error filtering episodic entries: {e}")
        return gr.update(choices=[], value=None)

def handle_episodic_selection_from_dropdown(room_name: str, selected_date: str):
    """エピソードのドロップダウンから選択した際、詳細を表示する"""
    if not room_name or not selected_date:
        return ""
        
    try:
        manager = EpisodicMemoryManager(room_name)
        data = manager._load_memory()
        
        # 同じ日付の全エピソードを収集
        matching_episodes = []
        for item in data:
            if item.get('date', '').strip() == selected_date.strip():
                matching_episodes.append(item)
        
        if not matching_episodes:
            return "選択されたエピソードが見つかりませんでした。"
        
        # created_at順でソート（古いものが先）
        matching_episodes.sort(key=lambda x: x.get('created_at', ''))
        
        # 全エピソードを表示
        all_details = []
        
        # 複数エピソードがある場合は冒頭に案内を追加
        if len(matching_episodes) > 1:
            header = f"📌 この日には {len(matching_episodes)} 件のエピソードがあります（作成順に表示）\n"
            header += "=" * 50 + "\n\n"
            all_details.append(header)
        
        for idx, item in enumerate(matching_episodes, 1):
            summary = item.get('summary', '')
            created_at = item.get('created_at', '不明')
            episode_type = item.get('type', '日次要約')
            
            # タイプのラベル変換
            type_labels = {
                'achievement': '🏆 目標達成',
                'bonding': '💕 絆確認',
                'discovery': '💡 発見'
            }
            type_label = type_labels.get(episode_type, '📝 日次要約')
            
            details = f"【{type_label}】\n"
            details += f"【日付】 {selected_date}\n"
            details += f"【記録日時】 {created_at}\n"
            if item.get('compressed'):
                details += f"【種別】 統合済みエピソード（元ログ数: {item.get('original_count', '?')}）\n"
            details += "-" * 30 + "\n\n"
            details += summary
            all_details.append(details)
        
        # 複数ある場合は区切り線で分離
        separator = "\n\n" + "=" * 50 + "\n\n"
        return separator.join(all_details)
                
    except Exception as e:
        return f"エピソード表示エラー: {e}"



# 古い handle_dream_journal_selection は Dropdown 移行に伴い廃止

def load_notepad_content(room_name: str) -> str:
    if not room_name: return ""
    _, _, _, _, notepad_path, _ = get_room_files_paths(room_name)
    if notepad_path and os.path.exists(notepad_path):
        with open(notepad_path, "r", encoding="utf-8") as f: return f.read()
    return ""

def handle_save_notepad_click(room_name: str, content: str) -> str:
    if not room_name: gr.Warning("ルームが選択されていません。"); return content

    # ▼▼▼【ここに追加】▼▼▼
    room_manager.create_backup(room_name, 'notepad')

    _, _, _, _, notepad_path, _ = room_manager.get_room_files_paths(room_name)
    if not notepad_path: gr.Error(f"「{room_name}」のメモ帳パス取得失敗。"); return content
    lines = [f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}] {line.strip()}" if line.strip() and not re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]", line.strip()) else line.strip() for line in content.strip().split('\n') if line.strip()]
    final_content = "\n".join(lines)
    try:
        with open(notepad_path, "w", encoding="utf-8") as f: f.write(final_content + ('\n' if final_content else ''))
        gr.Info(f"「{room_name}」のメモ帳を保存しました。"); return final_content
    except Exception as e: gr.Error(f"メモ帳の保存エラー: {e}"); return content

def handle_clear_notepad_click(room_name: str) -> str:
    if not room_name: gr.Warning("ルームが選択されていません。"); return ""
    _, _, _, _, notepad_path, _ = room_manager.get_room_files_paths(room_name)
    if not notepad_path: gr.Error(f"「{room_name}」のメモ帳パス取得失敗。"); return ""
    try:
        with open(notepad_path, "w", encoding="utf-8") as f: f.write("")
        gr.Info(f"「{room_name}」のメモ帳を空にしました。"); return ""
    except Exception as e: gr.Error(f"メモ帳クリアエラー: {e}"); return f"エラー: {e}"

def handle_reload_notepad(room_name: str) -> str:
    if not room_name: gr.Warning("ルームが選択されていません。"); return ""
    content = load_notepad_content(room_name); gr.Info(f"「{room_name}」のメモ帳を再読み込みしました。"); return content

# --- 創作ノートのハンドラ ---
def _get_creative_notes_path(room_name: str, filename: str = None) -> str:
    """創作ノートのパスを取得"""
    if not filename:
        filename = constants.CREATIVE_NOTES_FILENAME
    return os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME, filename)

def load_creative_notes_content(room_name: str, filename: str = None) -> str:
    """創作ノートの内容を読み込む"""
    if not room_name: return ""
    path = _get_creative_notes_path(room_name, filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f: return f.read()
    return ""

def handle_save_creative_notes(room_name: str, content: str, filename: str = None) -> str:
    """創作ノートを保存"""
    if not room_name: gr.Warning("ルームが選択されていません。"); return content
    # 書き込み前にアーカイブ判定 (最新ファイルの場合のみ)
    if not filename or filename == constants.CREATIVE_NOTES_FILENAME:
        room_manager.archive_large_note(room_name, constants.CREATIVE_NOTES_FILENAME)
    
    path = _get_creative_notes_path(room_name, filename)
    try:
        with open(path, "w", encoding="utf-8") as f: f.write(content)
        gr.Info(f"「{room_name}」の創作ノートを保存しました。"); return content
    except Exception as e: gr.Error(f"創作ノートの保存エラー: {e}"); return content

def handle_reload_creative_notes(room_name: str, filename: str = None) -> str:
    """創作ノートを再読み込み"""
    if not room_name: gr.Warning("ルームが選択されていません。"); return ""
    content = load_creative_notes_content(room_name, filename); gr.Info(f"「{room_name}」の創作ノートを再読み込みしました。"); return content

def handle_clear_creative_notes(room_name: str, filename: str = None) -> str:
    """創作ノートを空にする"""
    if not room_name: gr.Warning("ルームが選択されていません。"); return ""
    path = _get_creative_notes_path(room_name, filename)
    try:
        with open(path, "w", encoding="utf-8") as f: f.write("")
        gr.Info(f"「{room_name}」の創作ノートを空にしました。"); return ""
    except Exception as e: gr.Error(f"創作ノートクリアエラー: {e}"); return f"エラー: {e}"


# --- 創作ノート：エントリベースのハンドラ（新規追加） ---

def _parse_notes_entries(content: str) -> list:
    """
    タイムスタンプセクションでノートをパースしてエントリリストを返す。
    形式: --- で始まり、📝 YYYY-MM-DD HH:MM のヘッダーがあるセクション
    または --- で始まり、[YYYY-MM-DD HH:MM] のヘッダーがあるセクション
    """
    import re
    entries = []
    
    # 区切り線(---)の後にタイムスタンプが続く場合のみ分割（本文中の罫線による誤分割を防止）
    # \s* を追加して区切り線とアイコンの間の不必要な空白・改行を許容する
    sections = re.split(r'\n---+\n\s*(?=📝|\[)', content)
    
    for section in sections:
        section = section.strip()
        if not section:
            continue
        
        # タイムスタンプを探す (📝 YYYY-MM-DD HH:MM 形式)
        match1 = re.search(r'📝\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})', section)
        # [YYYY-MM-DD HH:MM] 形式
        match2 = re.search(r'\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\]', section)
        
        if match1:
            date_str = match1.group(1)
            time_str = match1.group(2)
            timestamp = f"{date_str} {time_str}"
            # ヘッダー行を除いたコンテンツ
            content_start = match1.end()
            entry_content = section[content_start:].strip()
        elif match2:
            date_str = match2.group(1)
            time_str = match2.group(2)
            timestamp = f"{date_str} {time_str}"
            content_start = match2.end()
            entry_content = section[content_start:].strip()
        else:
            # タイムスタンプがない場合はセクション全体を1つのエントリとして扱う
            timestamp = "日付なし"
            date_str = ""
            entry_content = section
        
        if entry_content:
            entries.append({
                "timestamp": timestamp,
                "date": date_str,
                "content": entry_content,
                "raw_section": section
            })
    
    return entries[::-1]


def handle_load_creative_entries(room_name: str, filename: str = None):
    """創作ノートのエントリを読み込み、UIを更新"""
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), ""
    
    content = load_creative_notes_content(room_name, filename)
    if not content.strip():
        print("--- [UI] 対象の創作ノートは空です。 ---")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), content
    
    entries = _parse_notes_entries(content)
    
    # 年・月リストを抽出
    years = set()
    months = set()
    choices = []
    
    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")
        if len(date_str) >= 7:
            years.add(date_str[:4])
            months.add(date_str[5:7])
        
        # ラベル作成（タイムスタンプ + 内容のプレビュー）
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{entry['timestamp']} - {preview}"
        # 値はインデックス（文字列として）
        choices.append((label, str(i)))
    
    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))
    
    print(f"--- [UI] {len(entries)}件のエントリを読み込みました。 ---")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value=None),
        content  # RAWエディタにも反映
    )


def handle_show_latest_creative(room_name: str, filename: str = None):
    """創作ノートを読み込み、最新のエントリを自動的に選択して表示する。
    
    Returns:
        (year_filter, month_filter, entry_dropdown, editor_content, raw_editor_content)
    """
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), "", ""
    
    content = load_creative_notes_content(room_name, filename)
    if not content.strip():
        print("--- [UI] 対象の創作ノートは空です。 ---")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), "", content
    
    entries = _parse_notes_entries(content)
    
    if not entries:
        gr.Info("エントリが見つかりません。RAW編集を使用してください。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), "", content
    
    # 年・月リストを抽出
    years = set()
    months = set()
    choices = []
    
    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")
        if len(date_str) >= 7:
            years.add(date_str[:4])
            months.add(date_str[5:7])
        
        # ラベル作成
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{entry['timestamp']} - {preview}"
        choices.append((label, str(i)))
    
    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))
    
    # 最新のエントリ（インデックス0）を選択して詳細を表示
    latest_entry = entries[0]
    latest_content = latest_entry.get("content", "")
    
    gr.Info("最新エントリを表示しています。")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value="0"),  # 最新エントリを選択
        latest_content,  # エディタに最新エントリの内容を表示
        content  # RAWエディタにも反映
    )


def handle_creative_filter_change(room_name: str, year: str, month: str, filename: str = None):
    """創作ノートのフィルタ変更時にドロップダウン選択肢を更新"""
    if not room_name:
        return gr.update(choices=[])
    
    content = load_creative_notes_content(room_name, filename)
    entries = _parse_notes_entries(content)
    
    choices = []
    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")
        
        # フィルタ条件チェック
        match_year = (year == "すべて" or (len(date_str) >= 4 and date_str[:4] == year))
        match_month = (month == "すべて" or (len(date_str) >= 7 and date_str[5:7] == month))
        
        if match_year and match_month:
            preview = entry["content"][:30].replace("\n", " ")
            if len(entry["content"]) > 30:
                preview += "..."
            label = f"{entry['timestamp']} - {preview}"
            choices.append((label, str(i)))
    
    return gr.update(choices=choices, value=None)


def handle_creative_selection(room_name: str, selected_idx: str, filename: str = None):
    """創作ノートのエントリ選択時に詳細を表示"""
    if not room_name or selected_idx is None:
        return ""
    
    try:
        idx = int(selected_idx)
        content = load_creative_notes_content(room_name, filename)
        entries = _parse_notes_entries(content)
        
        if 0 <= idx < len(entries):
            entry = entries[idx]
            return entry["content"]
        return ""
    except (ValueError, IndexError) as e:
        print(f"エントリ選択エラー: {e}")
        return ""


def handle_save_creative_entry(room_name: str, selected_idx: str, new_content: str, filename: str = None):
    """選択された創作ノートエントリを保存（エントリ内容のみ更新）"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return new_content
    
    if selected_idx is None:
        gr.Warning("エントリが選択されていません。RAW編集から全文を編集してください。")
        return new_content
    
    try:
        idx = int(selected_idx)
        content = load_creative_notes_content(room_name, filename)
        entries = _parse_notes_entries(content)
        
        if 0 <= idx < len(entries):
            # 元のセクションを新しい内容で置き換え
            old_section = entries[idx]["raw_section"]
            # タイムスタンプヘッダーを保持して内容のみ更新
            timestamp = entries[idx]["timestamp"]
            if timestamp != "日付なし":
                new_section = f"📝 {timestamp}\n{new_content.strip()}"
            else:
                new_section = new_content.strip()
            
            # 全文の中で置き換え
            updated_content = content.replace(old_section, new_section, 1)
            
            # 最新ファイルの場合のみ、保存直前にアーカイブチェックを行う (handle_save_creative_notesと同様のロジックを期待するなら)
            # ただし、ここでは特定エントリの更新なので、そのまま上書きで良い。
            
            path = _get_creative_notes_path(room_name, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(updated_content)
            
            gr.Info(f"エントリを保存しました。")
            return new_content
        else:
            gr.Warning("選択されたエントリが見つかりません。")
            return new_content
    except Exception as e:
        gr.Error(f"保存エラー: {e}")
        return new_content

# --- 研究・分析ノートのハンドラ ---
def _get_research_notes_path(room_name: str, filename: str = None) -> str:
    """研究ノートのパスを取得"""
    if not filename:
        filename = constants.RESEARCH_NOTES_FILENAME
    return os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME, filename)

def load_research_notes_content(room_name: str, filename: str = None) -> str:
    """研究ノートの内容を読み込む"""
    if not room_name: return ""
    path = _get_research_notes_path(room_name, filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def handle_save_research_notes(room_name: str, content: str, filename: str = None) -> str:
    """研究ノートを保存"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return content
    
    # 書き込み前にアーカイブ判定 (最新ファイルの場合のみ)
    if not filename or filename == constants.RESEARCH_NOTES_FILENAME:
        room_manager.archive_large_note(room_name, constants.RESEARCH_NOTES_FILENAME)
        
    path = _get_research_notes_path(room_name, filename)
    try:
        # バックアップ作成
        room_manager.create_backup(room_name, 'research_notes')
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        gr.Info(f"「{room_name}」の研究ノートを保存しました。")
        return content
    except Exception as e:
        gr.Error(f"研究ノートの保存エラー: {e}")
        return content

def handle_reload_research_notes(room_name: str, filename: str = None) -> str:
    """研究ノートを再読み込み"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return ""
    content = load_research_notes_content(room_name, filename)
    gr.Info(f"「{room_name}」の研究ノートを再読み込みしました。")
    return content

def handle_clear_research_notes(room_name: str, filename: str = None) -> str:
    """研究ノートを空にする"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return ""
    path = _get_research_notes_path(room_name, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        gr.Info(f"「{room_name}」の研究ノートを空にしました。")
        return ""
    except Exception as e:
        gr.Error(f"研究ノートクリアエラー: {e}")
        return f"エラー: {e}"


# --- 研究ノート：エントリベースのハンドラ ---

def handle_load_research_entries(room_name: str, filename: str = None):
    """研究ノートのエントリを読み込み、UIを更新"""
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), ""
    
    content = load_research_notes_content(room_name, filename)
    if not content.strip():
        print("--- [UI] 対象の研究ノートは空です。 ---")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), content
    
    entries = _parse_notes_entries(content)
    
    # 年・月リストを抽出
    years = set()
    months = set()
    choices = []
    
    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")
        if len(date_str) >= 7:
            years.add(date_str[:4])
            months.add(date_str[5:7])
        
        # ラベル作成（タイムスタンプ + 内容のプレビュー）
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{entry['timestamp']} - {preview}"
        choices.append((label, str(i)))
    
    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))
    
    print(f"--- [UI] {len(entries)}件のエントリを読み込みました。 ---")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value=None),
        content  # RAWエディタにも反映
    )


def handle_show_latest_research(room_name: str, filename: str = None):
    """研究ノートを読み込み、最新のエントリを自動的に選択して表示する。
    
    Returns:
        (year_filter, month_filter, entry_dropdown, editor_content, raw_editor_content)
    """
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), "", ""
    
    content = load_research_notes_content(room_name, filename)
    if not content.strip():
        print("--- [UI] 対象の研究ノートは空です。 ---")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), "", content
    
    entries = _parse_notes_entries(content)
    
    if not entries:
        gr.Info("エントリが見つかりません。RAW編集を使用してください。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[], value=None), "", content
    
    # 年・月リストを抽出
    years = set()
    months = set()
    choices = []
    
    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")
        if len(date_str) >= 7:
            years.add(date_str[:4])
            months.add(date_str[5:7])
        
        # ラベル作成
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{entry['timestamp']} - {preview}"
        choices.append((label, str(i)))
    
    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))
    
    # 最新のエントリ（インデックス0）を選択して詳細を表示
    latest_entry = entries[0]
    latest_content = latest_entry.get("content", "")
    
    gr.Info("最新エントリを表示しています。")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value="0"),  # 最新エントリを選択
        latest_content,  # エディタに最新エントリの内容を表示
        content  # RAWエディタにも反映
    )


def handle_research_filter_change(room_name: str, year: str, month: str, filename: str = None):
    """研究ノートのフィルタ変更時にドロップダウン選択肢を更新"""
    if not room_name:
        return gr.update(choices=[])
    
    content = load_research_notes_content(room_name, filename)
    entries = _parse_notes_entries(content)
    
    choices = []
    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")
        
        match_year = (year == "すべて" or (len(date_str) >= 4 and date_str[:4] == year))
        match_month = (month == "すべて" or (len(date_str) >= 7 and date_str[5:7] == month))
        
        if match_year and match_month:
            preview = entry["content"][:30].replace("\n", " ")
            if len(entry["content"]) > 30:
                preview += "..."
            label = f"{entry['timestamp']} - {preview}"
            choices.append((label, str(i)))
    
    return gr.update(choices=choices, value=None)


def handle_research_selection(room_name: str, selected_idx: str, filename: str = None):
    """研究ノートのエントリ選択時に詳細を表示"""
    if not room_name or selected_idx is None:
        return ""
    
    try:
        idx = int(selected_idx)
        content = load_research_notes_content(room_name, filename)
        entries = _parse_notes_entries(content)
        
        if 0 <= idx < len(entries):
            entry = entries[idx]
            return entry["content"]
        return ""
    except (ValueError, IndexError) as e:
        print(f"エントリ選択エラー: {e}")
        return ""


def handle_save_research_entry(room_name: str, selected_idx: str, new_content: str, filename: str = None):
    """選択された研究ノートエントリを保存（エントリ内容のみ更新）"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return new_content
    
    if selected_idx is None:
        gr.Warning("エントリが選択されていません。RAW編集から全文を編集してください。")
        return new_content
    
    try:
        idx = int(selected_idx)
        content = load_research_notes_content(room_name, filename)
        entries = _parse_notes_entries(content)
        
        if 0 <= idx < len(entries):
            old_section = entries[idx]["raw_section"]
            timestamp = entries[idx]["timestamp"]
            if timestamp != "日付なし":
                new_section = f"📝 {timestamp}\n{new_content.strip()}"
            else:
                new_section = new_content.strip()
            
            updated_content = content.replace(old_section, new_section, 1)
            
            path = _get_research_notes_path(room_name, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(updated_content)
            
            gr.Info(f"エントリを保存しました。")
            return new_content
        else:
            gr.Warning("選択されたエントリが見つかりません。")
            return new_content
    except Exception as e:
        gr.Error(f"保存エラー: {e}")
        return new_content

def handle_note_file_list_refresh(room_name: str, note_type: str):
    """指定されたノート種別のファイルリストを更新してDropdownを返す"""
    if not room_name:
        return gr.update(choices=[], value=None)
    
    files = room_manager.get_note_files(room_name, note_type)
    if not files:
        # フォールバック: デフォルトファイル名を表示
        default_filenam_map = {
            'notepad': constants.NOTEPAD_FILENAME,
            'research': constants.RESEARCH_NOTES_FILENAME,
            'creative': constants.CREATIVE_NOTES_FILENAME
        }
        files = [default_filenam_map.get(note_type, "notes.md")]
    
    return gr.update(choices=files, value=files[0])

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
    default_room = all_rooms[0][1] if all_rooms else "Default" # ← 戻り値の形式変更にも対応

    if len(selected_ids) == 1:
        alarm = next((a for a in alarm_manager.load_alarms() if a.get("id") == selected_ids[0]), None)
        if alarm:
            h, m = alarm.get("time", "08:00").split(":")
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
                work_theme=work_theme or "作業終了の時間です。",
                break_theme=brk_theme or "休憩終了の時間です。",
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

def handle_memory_archiving(room_name: str, console_content: str):
    """
    「過去ログから記憶を構築」ボタンのイベントハンドラ。
    memory_archivist.py をサブプロセスとして起動し、UIを非同期で更新する。
    """
    yield (
        gr.update(value="記憶構築中...", interactive=False),
        gr.update(visible=True, interactive=True), # 中断ボタンを表示・有効化
        None, # PIDをリセット
        console_content,
        console_content,
        gr.update(interactive=False),
        gr.update(interactive=False)
    )

    full_log_output = console_content
    script_path = "memory_archivist.py"
    pid = None

    try:
        gr.Info("過去ログからの記憶構築を開始します...")

        cmd = [sys.executable, "-u", script_path, "--room_name", room_name, "--source", "import"]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='ignore')
        pid = proc.pid

        # UIにPIDを即座に反映
        yield (
            gr.update(), gr.update(), pid, full_log_output, full_log_output,
            gr.update(), gr.update()
        )

        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            print(line)
            full_log_output += line + "\n"
            yield (
                gr.update(), gr.update(), pid, full_log_output, full_log_output,
                gr.update(), gr.update()
            )

        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"{script_path} failed with return code {proc.returncode}")

        gr.Info("✅ 過去ログからの記憶構築が、正常に完了しました！")

    except Exception as e:
        error_message = f"記憶の構築中にエラーが発生しました: {e}"
        print(error_message)
        traceback.print_exc()
        gr.Error(error_message)

    finally:
        yield (
            gr.update(value="過去ログから記憶を構築", interactive=True),
            gr.update(visible=False),
            None, # PIDをクリア
            full_log_output,
            full_log_output,
            gr.update(interactive=True),
            gr.update(interactive=True)
        )

def handle_archivist_stop(pid: int):
    """
    実行中の記憶アーキビストのプロセスを中断する。
    """
    if pid is None:
        gr.Warning("停止対象のプロセスが見つかりません。")
        return gr.update(), gr.update(visible=False), None, gr.update()

    try:
        process = psutil.Process(pid)
        # 子プロセスも含めて停止させる
        for child in process.children(recursive=True):
            child.terminate()
        process.terminate()
        gr.Info(f"記憶構築プロセス(PID: {pid})に停止信号を送信しました。")
    except psutil.NoSuchProcess:
        gr.Warning(f"プロセス(PID: {pid})は既に終了しています。")
    except Exception as e:
        gr.Error(f"プロセスの停止中にエラーが発生しました: {e}")
        traceback.print_exc()

    return (
        gr.update(interactive=True, value="過去ログから記憶を構築"),
        gr.update(visible=False),
        None, # PIDをクリア
        gr.update(interactive=True)
    )

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
        log_file_path, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
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


def handle_memos_batch_import(room_name: str, console_content: str):
    """
    【v3: 最終FIX版】
    知識グラフの構築を、2段階のサブプロセスとして、堅牢に実行する。
    いかなる状況でも、UIがフリーズしないことを保証する。
    """
    # UIコンポーネントの数をハードコードするのではなく、動的に取得するか、
    # 確実な数（今回は6）を返すようにする。
    NUM_OUTPUTS = 6

    # 処理中のUI更新を定義
    # ★★★ あなたの好みに合わせてテキストを修正 ★★★
    yield (
        gr.update(value="知識グラフ構築中...", interactive=False), # Button
        gr.update(visible=True), # Stop Button (今回は実装しないが将来のため)
        None, # Process State
        console_content, # Console State
        console_content, # Console Output
        gr.update(interactive=False)  # Chat Input
    )

    full_log_output = console_content
    script_path_1 = "batch_importer.py"
    script_path_2 = "soul_injector.py"

    try:
        # --- ステージ1: 骨格の作成 ---
        gr.Info("ステージ1/2: 知識グラフの骨格を作成しています...")

        # ▼▼▼【ここからが修正箇所】▼▼▼
        # text=True を削除し、stdoutを直接扱う
        proc1 = subprocess.run(
            [sys.executable, "-X", "utf8", script_path_1, room_name],
            capture_output=True
        )
        # バイトストリームを、エラーを無視して強制的にデコードする
        output_log = proc1.stdout.decode('utf-8', errors='replace')
        error_log = proc1.stderr.decode('utf-8', errors='replace')
        log_chunk = f"\n--- [{script_path_1} Output] ---\n{output_log}\n{error_log}"
        # ▲▲▲【修正ここまで】▲▲▲

        full_log_output += log_chunk
        yield (
            gr.update(), gr.update(), None,
            full_log_output, full_log_output, gr.update()
        )

        if proc1.returncode != 0:
            raise RuntimeError(f"{script_path_1} failed with return code {proc1.returncode}")

        gr.Info("ステージ1/2: 骨格の作成に成功しました。")

        # --- ステージ2: 魂の注入 ---
        # ★★★ あなたの好みに合わせてテキストを修正 ★★★
        gr.Info("ステージ2/2: 知識グラフを構築中です...")

        # ▼▼▼【ここからが修正箇所】▼▼▼
        proc2 = subprocess.run(
            [sys.executable, "-X", "utf8", script_path_2, room_name],
            capture_output=True
        )
        output_log = proc2.stdout.decode('utf-8', errors='replace')
        error_log = proc2.stderr.decode('utf-8', errors='replace')
        log_chunk = f"\n--- [{script_path_2} Output] ---\n{output_log}\n{error_log}"
        # ▲▲▲【修正ここまで】▲▲▲
        full_log_output += log_chunk
        yield (
            gr.update(), gr.update(), None,
            full_log_output, full_log_output, gr.update()
        )

        if proc2.returncode != 0:
            raise RuntimeError(f"{script_path_2} failed with return code {proc2.returncode}")

        gr.Info("✅ 知識グラフの構築が、正常に完了しました！")

    except Exception as e:
        error_message = f"知識グラフの構築中にエラーが発生しました: {e}"
        logging.error(error_message)
        logging.error(traceback.format_exc())
        gr.Error(error_message)

    finally:
        # --- 最終処理: UIを必ず元の状態に戻す ---
        yield (
            gr.update(value="知識グラフを構築/更新する", interactive=True), # Button
            gr.update(visible=False), # Stop Button
            None, # Process State
            full_log_output, # Console State
            full_log_output, # Console Output
            gr.update(interactive=True) # Chat Input
        )


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

def handle_core_memory_update_click(room_name: str, api_key_name: str):
    """
    コアメモリの更新を同期的に実行し、完了後にUIのテキストエリアを更新する。
    """
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        return gr.update() # 何も更新しない

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Warning(f"APIキー '{api_key_name}' が有効ではありません。")
        return gr.update()

    gr.Info(f"「{room_name}」のコアメモリ更新を開始しました...")
    try:
        from tools import memory_tools
        result = memory_tools.summarize_and_update_core_memory.func(room_name=room_name, api_key=api_key)

        if "成功" in result:
            gr.Info(f"✅ コアメモリの更新が正常に完了しました。")
            # 成功した場合、更新された内容を読み込んで返す
            updated_content = load_core_memory_content(room_name)
            return gr.update(value=updated_content)
        else:
            gr.Error(f"コアメモリの更新に失敗しました。詳細: {result}")
            return gr.update() # 失敗時はUIを更新しない

    except Exception as e:
        gr.Error(f"コアメモリ更新中に予期せぬエラーが発生しました: {e}")
        traceback.print_exc()
        return gr.update()

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
    if config_manager.save_config_if_changed("last_api_key_name", api_key_name):
        gr.Info(f"APIキーを '{api_key_name}' に設定しました。")
    return api_key_name

def update_api_history_limit_state_and_reload_chat(limit_ui_val: str, room_name: Optional[str], add_timestamp: bool, display_thoughts: bool, screenshot_mode: bool = False, redaction_rules: List[Dict] = None):
    key = next((k for k, v in constants.API_HISTORY_LIMIT_OPTIONS.items() if v == limit_ui_val), "all")
    config_manager.save_config_if_changed("last_api_history_limit_option", key)
    # この関数はUIリロードが主目的なので、Info通知は不要
    history, mapping_list = reload_chat_log(room_name, key, add_timestamp, display_thoughts, screenshot_mode, redaction_rules)
    return key, history, mapping_list

def handle_play_audio_button_click(selected_message: Optional[Dict[str, str]], room_name: str, api_key_name: str):
    """
    【最終FIX版 v2】チャット履歴で選択されたAIの発言を音声合成して再生する。
    try...except を削除し、Gradioの例外処理に完全に委ねる。
    """
    if not selected_message:
        raise gr.Error("再生するメッセージが選択されていません。")

    # 処理中はボタンを無効化
    yield (
        gr.update(visible=False),
        gr.update(value="音声生成中... ▌", interactive=False),
        gr.update(interactive=False)
    )

    raw_text = utils.extract_raw_text_from_html(selected_message.get("content"))
    text_to_speak = utils.remove_thoughts_from_text(raw_text)

    if not text_to_speak:
        gr.Info("このメッセージには音声で再生できるテキストがありません。")
        yield gr.update(), gr.update(value="🔊 選択した発言を再生", interactive=True), gr.update(interactive=True)
        return

    effective_settings = config_manager.get_effective_settings(room_name)
    voice_id, voice_style_prompt = effective_settings.get("voice_id", "iapetus"), effective_settings.get("voice_style_prompt", "")
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)

    if not api_key or api_key.startswith("YOUR_API_KEY"):
        raise gr.Error(f"APIキー '{api_key_name}' が無効です。")

    from audio_manager import generate_audio_from_text
    gr.Info(f"「{room_name}」の声で音声を生成しています...")
    audio_filepath = generate_audio_from_text(text_to_speak, api_key, voice_id, room_name, voice_style_prompt)

    if audio_filepath and not audio_filepath.startswith("【エラー】"):
        gr.Info("再生します。")
        yield gr.update(value=audio_filepath, visible=True), gr.update(value="🔊 選択した発言を再生", interactive=True), gr.update(interactive=True)
    else:
        raise gr.Error(audio_filepath or "音声の生成に失敗しました。")

def handle_voice_preview(room_name: str, selected_voice_name: str, voice_style_prompt: str, text_to_speak: str, api_key_name: str):
    """
    【最終FIX版 v2】音声をプレビュー再生する。
    try...except を削除し、Gradioの例外処理に完全に委ねる。
    """
    if not all([selected_voice_name, text_to_speak, api_key_name]):
        raise gr.Error("声、テキスト、APIキーがすべて選択されている必要があります。")

    yield (
        gr.update(visible=False),
        gr.update(interactive=False),
        gr.update(value="生成中...", interactive=False)
    )

    voice_id = next((key for key, value in config_manager.SUPPORTED_VOICES.items() if value == selected_voice_name), None)
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)

    if not voice_id or not api_key:
        raise gr.Error("声またはAPIキーが無効です。")

    from audio_manager import generate_audio_from_text
    gr.Info(f"声「{selected_voice_name}」で音声を生成しています...")
    audio_filepath = generate_audio_from_text(text_to_speak, api_key, voice_id, room_name, voice_style_prompt)

    if audio_filepath and not audio_filepath.startswith("【エラー】"):
        gr.Info("プレビューを再生します。")
        yield gr.update(value=audio_filepath, visible=True), gr.update(interactive=True), gr.update(value="試聴", interactive=True)
    else:
        raise gr.Error(audio_filepath or "音声の生成に失敗しました。")

def _generate_scenery_prompt(room_name: str, api_key_name: str, style_choice: str) -> str:
    """
    画像生成のための最終的なプロンプト文字列を生成する責務を負うヘルパー関数。
    """
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or (isinstance(api_key, str) and api_key.startswith("YOUR_API_KEY")):
        raise gr.Error(f"APIキー '{api_key_name}' が見つかりません。")

    season_en, time_of_day_en = utils._get_current_time_context(room_name)
    location_id = utils.get_current_location(room_name)
    if not location_id:
        raise gr.Error("現在地が特定できません。")

    style_prompts = {
        "写真風 (デフォルト)": "An ultra-detailed, photorealistic masterpiece with cinematic lighting.",
        "イラスト風": "A beautiful and detailed anime-style illustration, pixiv contest winner.",
        "アニメ風": "A high-quality screenshot from a modern animated film.",
        "水彩画風": "A gentle and emotional watercolor painting."
    }
    style_choice_text = style_prompts.get(style_choice, style_prompts["写真風 (デフォルト)"])

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

    from llm_factory import LLMFactory
    effective_settings = config_manager.get_effective_settings(room_name)
    scene_director_llm = LLMFactory.create_chat_model(
        api_key=api_key,
        generation_config=effective_settings,
        internal_role="processing"
    )

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
    final_prompt = scene_director_llm.invoke(director_prompt).content.strip()
    return final_prompt

def handle_show_scenery_prompt(room_name: str, api_key_name: str, style_choice: str) -> str:
    """「プロンプトを生成」ボタンのイベントハンドラ。"""
    if not room_name or not api_key_name:
        raise gr.Error("ルームとAPIキーを選択してください。")

    try:
        gr.Info("シーンディレクターAIがプロンプトを構成しています...")
        prompt = _generate_scenery_prompt(room_name, api_key_name, style_choice)
        gr.Info("プロンプトを生成しました。")
        return prompt
    except Exception as e:
        # gr.ErrorはGradioが自動で処理するので、ここではprintでログを残す
        print(f"--- プロンプト生成エラー: {e} ---")
        traceback.print_exc()
        # UIにはGradioがエラーメッセージを表示してくれる
        raise

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
                return Image.open(fallback_image_path_fb)
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

    # --- プロンプト生成（Gemini APIキーが必要）---
    final_prompt = ""
    try:
        # プロンプト生成はGemini APIを使用
        if not api_key_name:
            gr.Warning("プロンプト生成用のAPIキーを選択してください。")
            if fallback_image_path: return Image.open(fallback_image_path)
            return None
        final_prompt = _generate_scenery_prompt(room_name, api_key_name, style_choice)
    except Exception as e:
        print(f"シーンディレクターAIによるプロンプト生成中にエラーが発生しました: {e}")
        if fallback_image_path: return Image.open(fallback_image_path)
        return None
    
    if not final_prompt:
        gr.Error("シーンディレクターAIが有効なプロンプトを生成できませんでした。")
        if fallback_image_path: return Image.open(fallback_image_path)
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
                return Image.open(final_path)
            except Exception as move_e:
                gr.Error(f"生成された画像の移動/上書きに失敗しました: {move_e}")
                if fallback_image_path: return Image.open(fallback_image_path)
                return None
        else:
            gr.Error("画像の生成には成功しましたが、一時ファイルの特定に失敗しました。")
    else:
        gr.Error(f"画像の生成/更新に失敗しました。AIの応答: {result}")

    # フォールバック
    if fallback_image_path: return Image.open(fallback_image_path)
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
        return {}, gr.update(choices=[], value=None), "", gr.update(choices=[], value=None)

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

def handle_room_change_for_all_tabs(room_name: str, api_key_name: str, expected_count: int = 155):
    """
    【v11: 最終契約遵守版】
    ルーム変更時に、全てのUI更新と内部状態の更新を、この単一の関数で完結させる。
     expected_count を UI側 (gr.State) から受け取ることで、不整合を自動的に解消する仕組みを導入。
    """
    # [Fix] 以前は current_room_state と比較してリターンしていたが、
    # 状態不整合（UIは変わったがStateが変わっていない、あるいはその逆）を防ぐため、
    # 常に強制的にリロードを実行する。
    
    global _last_room_switch_time
    _last_room_switch_time = time.time()

    print(f"--- UI司令塔 実行: {room_name} へ変更 ---")

    # 責務1: 各UIセクションの更新値を個別に生成する
    chat_tab_updates = _update_chat_tab_for_room_change(room_name, api_key_name)
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
    
    all_updates_tuple = (
        *chat_tab_updates, *world_builder_updates, *session_management_updates,
        rules_df_for_ui, archive_date_dd_update, *time_settings_updates,
        ui_attachments_df, initial_active_attachments_display, custom_scenery_dd_update
    )
    
    effective_settings = config_manager.get_effective_settings(room_name)
    
    # トークン計算用のAPIキー決定: ルーム個別設定があればそれを優先
    token_api_key_name = effective_settings.get("api_key_name", api_key_name)
    
    api_history_limit_key = config_manager.CONFIG_GLOBAL.get("last_api_history_limit_option", "all")
    token_calc_kwargs = {k: effective_settings.get(k) for k in [
        "display_thoughts", "add_timestamp", "send_current_time", "send_thoughts", 
        "send_notepad", "use_common_prompt", "send_core_memory", "send_scenery"
    ]}
    estimated_count = gemini_api.count_input_tokens(
        room_name=room_name, api_key_name=token_api_key_name, parts=[],
        api_history_limit=api_history_limit_key, **token_calc_kwargs
    )
    token_count_text = _format_token_display(room_name, estimated_count)

    # 索引の最終更新日時を取得
    memory_index_last_updated = _get_rag_index_last_updated(room_name, "memory")
    current_log_index_last_updated = _get_rag_index_last_updated(room_name, "current_log")
    
    # 契約遵守のため、最後の戻り値として索引ステータスを追加
    final_outputs = all_updates_tuple + (
        token_count_text, 
        "",  # room_delete_confirmed_state
        f"最終更新: {memory_index_last_updated}",  # memory_reindex_status
        f"最終更新: {current_log_index_last_updated}"  # current_log_reindex_status
    )
    
    return _ensure_output_count(final_outputs, expected_count)


def handle_start_session(main_room: str, participant_list: list) -> tuple:
    if not participant_list:
        gr.Info("会話に参加するルームを1人以上選択してください。")
        return gr.update(), gr.update()

    all_participants = [main_room] + participant_list
    participants_text = "、".join(all_participants)
    status_text = f"現在、**{participants_text}** を招待して会話中です。"
    session_start_message = f"（システム通知：{participants_text} とのグループ会話が開始されました。）"

    for room_name in all_participants:
        log_f, _, _, _, _, _ = get_room_files_paths(room_name)
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
        log_f, _, _, _, _, _ = get_room_files_paths(room_name)
        if log_f:
            utils.save_message_to_log(log_f, "## SYSTEM:(セッション管理)", session_end_message)

    gr.Info("グループ会話を終了し、1対1の会話モードに戻りました。")
    return [], "現在、1対1の会話モードです。", gr.update(value=[])


def handle_wb_area_select(world_data: Dict, area_name: str):
    if not area_name or area_name not in world_data:
        return gr.update(choices=[], value=None)
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
        return world_data, gr.update(choices=area_choices, value=item_name), gr.update(choices=[], value=None), gr.update(visible=False), "", raw_content, location_dropdown_update

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
            gr.update(choices=[], value=None),                     # place_selector
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
        gr.update(choices=[], value=None),                     # place_selector
        gr.update(value=raw_content),                          # world_settings_raw_editor
        gr.update(choices=new_location_choices)                # location_dropdown
    )

def handle_save_gemini_key(key_name: str, key_value: str):
    """【v14: 責務分離版】新しいAPIキーを保存し、関連UIのみを更新する。"""
    # 入力検証
    if not key_name or not key_value or not re.match(r"^[a-zA-Z0-9_]+$", key_name.strip()):
        gr.Warning("キーの名前（半角英数字とアンダースコアのみ）と値を両方入力してください。")
        return gr.update(), gr.update(), gr.update(), gr.update()

    key_name = key_name.strip()
    config_manager.add_or_update_gemini_key(key_name, key_value)
    gr.Info(f"Gemini APIキー「{key_name}」を保存しました。UIをリフレッシュします...")

    config_manager.load_config() # 最新の状態を読み込み

    new_choices_for_ui = config_manager.get_api_key_choices_for_ui()
    new_key_names = [key for _, key in new_choices_for_ui]
    paid_keys = config_manager.CONFIG_GLOBAL.get("paid_api_key_names", [])

    return (
        gr.update(choices=new_choices_for_ui, value=key_name), # api_key_dropdown
        gr.update(choices=new_key_names, value=paid_keys),     # paid_keys_checkbox_group
        gr.update(value=""),                                   # gemini_key_name_input (クリア)
        gr.update(value="")                                    # gemini_key_value_input (クリア)
    )

def handle_delete_gemini_key(key_name):
    if not key_name:
        gr.Warning("削除するキーの名前を入力してください。")
        return gr.update(), gr.update()
    config_manager.delete_gemini_key(key_name)
    gr.Info(f"Gemini APIキー「{key_name}」を削除しました。")
    # configを再読み込みして最新の状態を反映
    config_manager.load_config()
    new_choices_for_ui = config_manager.get_api_key_choices_for_ui()
    new_key_names = [pair[1] for pair in new_choices_for_ui]
    paid_keys = config_manager.CONFIG_GLOBAL.get("paid_api_key_names", [])

    api_key_dd_update = gr.update(choices=new_choices_for_ui, value=new_key_names[0] if new_key_names else None)
    paid_keys_cb_update = gr.update(choices=new_key_names, value=paid_keys)
    return (api_key_dd_update, paid_keys_cb_update)

def handle_save_pushover_config(user_key, app_token):
    config_manager.update_pushover_config(user_key, app_token)
    gr.Info("Pushover設定を保存しました。")


def handle_paid_keys_change(paid_key_names: List[str]):
    """有料キーチェックボックスが変更されたら即時保存する。"""
    if not isinstance(paid_key_names, list):
        gr.Warning("有料キーリストの更新に失敗しました。")
        return gr.update()
    
    if config_manager.save_config_if_changed("paid_api_key_names", paid_key_names):
        gr.Info("有料APIキーの設定を更新しました。")

    # グローバル変数を更新して即時反映
    config_manager.load_config()
    
    # ドロップダウンの表示も(Paid)ラベル付きで更新するために、新しい選択肢リストを返す
    new_choices_for_ui = config_manager.get_api_key_choices_for_ui()
    return gr.update(choices=new_choices_for_ui)


def handle_rotation_setting_change(enabled: bool):
    """APIキーローテーション設定が変更されたら即時保存する。"""
    config_manager.save_config_if_changed("enable_api_key_rotation", enabled)
    
    # グローバル変数を更新して即時反映
    config_manager.load_config()
    
    status_text = "有効" if enabled else "無効"
    gr.Info(f"APIキー自動ローテーションを【{status_text}】に設定しました。")
    return


def handle_allow_external_connection_change(allow_external: bool):
    """外部接続設定が変更されたら即時保存する。"""
    if config_manager.save_config_if_changed("allow_external_connection", allow_external):
        if allow_external:
            gr.Info("外部接続を許可しました。アプリを再起動すると反映されます。")
        else:
            gr.Info("外部接続を無効にしました。アプリを再起動すると反映されます。")
    config_manager.load_config()

def handle_notification_service_change(service_choice: str):
    if service_choice in ["Discord", "Pushover"]:
        service_value = service_choice.lower()
        if config_manager.save_config_if_changed("notification_service", service_value):
            gr.Info(f"通知サービスを「{service_choice}」に設定しました。")

def handle_save_moonshot_key(api_key: str):
    """Moonshot AI (Kimi) APIキーを保存する。"""
    if config_manager.save_config_if_changed("moonshot_api_key", api_key):
        gr.Info("Moonshot APIキーを保存しました。")
    config_manager.load_config()


def handle_save_discord_webhook(webhook_url: str):
    if config_manager.save_config_if_changed("notification_webhook_url", webhook_url):
        gr.Info("Discord Webhook URLを保存しました。")
def load_system_prompt_content(room_name: str) -> str:
    if not room_name: return ""
    _, system_prompt_path, _, _, _, _ = get_room_files_paths(room_name)
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

    _, system_prompt_path, _, _, _, _ = get_room_files_paths(room_name)
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

def handle_visualize_graph(room_name: str):
    """
    【新規作成】
    現在の知識グラフを可視化し、その結果をUIに表示する。
    """
    if not room_name:
        gr.Warning("可視化するルームが選択されていません。")
        return gr.update(visible=False)

    script_path = "visualize_graph.py"

    gr.Info(f"ルーム「{room_name}」の知識グラフを可視化しています...")

    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", script_path, room_name],
            capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore'
        )

        # スクリプトの標準出力を解析して、画像のパスを取得
        output = proc.stdout.strip()
        image_path = None
        # 出力の最後の行にパスが含まれていると仮定
        for line in reversed(output.splitlines()):
            if line.strip().endswith(".png"):
                image_path = line.strip()
                break

        if image_path and os.path.exists(image_path):
            gr.Info("知識グラフの可視化に成功しました。")
            return gr.update(value=image_path, visible=True)
        else:
            # スクリプトは成功したが、パスの取得に失敗した場合
            gr.Error("可視化画像のパス取得に失敗しました。詳細はコンソールログを確認してください。")
            print(f"--- [可視化エラー] ---")
            print(f"visualize_graph.pyからの出力:\n{output}")
            return gr.update(visible=False)

    except subprocess.CalledProcessError as e:
        # スクリプト自体がエラーで終了した場合
        error_message = "知識グラフの可視化スクリプト実行中にエラーが発生しました。"
        gr.Error(error_message)
        print(f"--- [可視化エラー] ---")
        print(f"Stderr:\n{e.stderr}")
        return gr.update(visible=False)
    except Exception as e:
        gr.Error(f"可視化処理中に予期せぬエラーが発生しました: {e}")
        return gr.update(visible=False)


def handle_stop_button_click(room_name, api_history_limit, add_timestamp, display_thoughts, screenshot_mode, redaction_rules):
    """
    ストップボタンが押されたときにUIの状態を即座にリセットし、ログから最新の状態を再描画する。
    """
    print("--- [UI] ユーザーによりストップボタンが押されました ---")
    # ログファイルから最新の履歴を再読み込みして、"思考中..." のような表示を消去する
    history, mapping_list = reload_chat_log(room_name, api_history_limit, add_timestamp, display_thoughts, screenshot_mode, redaction_rules)
    return (
        gr.update(visible=False, interactive=True), # ストップボタンを非表示に
        gr.update(interactive=True),              # 更新ボタンを有効に       
        history,                                  # チャット履歴を最新の状態に
        mapping_list                              # マッピングリストも更新
    )


# ▼▼▼【ここからが新しく追加するブロック】▼▼▼
def _overwrite_log_file(file_path: str, messages: List[Dict]):
    """
    メッセージ辞書のリストからログファイルを完全に上書きする。
    """
    log_content_parts = []
    for msg in messages:
        # 新しいログ形式 `ROLE:NAME` に完全準拠して書き出す
        role = msg.get("role", "AGENT").upper()
        responder_id = msg.get("responder", "不明")
        header = f"## {role}:{responder_id}"
        content = msg.get('content', '').strip()
        # contentが空でもヘッダーは記録されるべき場合があるため、
        # responder_idが存在すればエントリを作成する
        if responder_id:
             log_content_parts.append(f"{header}\n{content}")

    new_log_content = "\n\n".join(log_content_parts)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_log_content)
    # ファイルの末尾に追記用の改行を追加
    if new_log_content:
        with open(file_path, "a", encoding="utf-8") as f:
            f.write("\n\n")

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

        log_f, _, _, _, _, _ = get_room_files_paths(room_name)
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
            # タイムスタンプの正規表現: YYYY-MM-DD (...) HH:MM:SS | モデル名
            # 修正: \n\n がなくてもマッチするようにし、末尾のアンカーを調整
            timestamp_pattern = re.compile(r'(\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}(?: \| .*)?)$')

            thoughts_match = thoughts_pattern.search(original_content)
            # 全体の中からタイムスタンプを探す
            timestamp_match = timestamp_pattern.search(original_content)

            thoughts_part = thoughts_match.group(1) if thoughts_match else ""
            timestamp_part = timestamp_match.group(1) if timestamp_match else ""

            body_part = original_content
            if thoughts_part: body_part = body_part.replace(thoughts_part, "")
            if timestamp_part: body_part = body_part.replace(timestamp_part, "")
            body_part = body_part.strip()

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

        _overwrite_log_file(log_f, all_messages)
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

            gr.Info(f"ルーム「{room_name}」のアバター動画を更新しました。")

            # プロフィール表示を更新し、クロップUIは非表示のまま
            return (
                None,
                gr.update(visible=False),
                gr.update(visible=False),
                gr.update(open=False),
                gr.update(value=get_avatar_html(room_name, state="idle"))
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
    current_mode = effective_settings.get("avatar_mode", "video")
    
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
    mode = effective_settings.get("avatar_mode", "video")  # デフォルトは動画優先
    
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
    avatar_mode = effective_settings.get("avatar_mode", "video")
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
        return gr.update(), gr.update(), gr.update()
    
    if not expression_name or not expression_name.strip():
        gr.Warning("表情名を入力してください。")
        return gr.update(), gr.update(), gr.update()
    
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
        gr.update(value=""),  # テキストボックスをクリア
        gr.update(choices=get_all_expression_choices(room_name)) # 削除/編集用ドロップダウンを更新
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

# --- Theme Management Handlers ---

def handle_theme_tab_load():
    """テーマタブが選択されたときに、設定を読み込んでUIを初期化する。"""
    theme_settings = config_manager.CONFIG_GLOBAL.get("theme_settings", {})
    custom_themes = theme_settings.get("custom_themes", {})
    active_theme = theme_settings.get("active_theme", "Soft")

    # Gradioのプリセットテーマ名
    preset_themes = ["Soft", "Default", "Monochrome", "Glass"]
    custom_theme_names = sorted(custom_themes.keys())

    all_choices = ["--- プリセット ---"] + preset_themes + ["--- カスタム ---"] + custom_theme_names
    # 現在のテーマが存在しない場合はデフォルトに戻す
    current_selection = active_theme if active_theme in (preset_themes + custom_theme_names) else "Soft"

    return theme_settings, gr.update(choices=all_choices, value=current_selection)

def handle_theme_selection(theme_settings, selected_theme_name):
    """ドロップダウンでテーマが選択されたときに、プレビューUIを更新する。"""
    if selected_theme_name.startswith("---"):
        # 区切り線が選択された場合は何もしない
        return gr.update(), gr.update(), gr.update(), gr.update()

    # プリセットテーマの定義（値はHUEの名前）
    preset_themes = {
        "Soft": {"primary_hue": "blue", "secondary_hue": "sky", "neutral_hue": "slate", "font": "Noto Sans JP"},
        "Default": {"primary_hue": "orange", "secondary_hue": "amber", "neutral_hue": "gray", "font": "Noto Sans JP"},
        "Monochrome": {"primary_hue": "neutral", "secondary_hue": "neutral", "neutral_hue": "neutral", "font": "IBM Plex Mono"},
        "Glass": {"primary_hue": "teal", "secondary_hue": "cyan", "neutral_hue": "gray", "font": "Quicksand"},
    }

    if selected_theme_name in preset_themes:
        params = preset_themes[selected_theme_name]
        return (
            gr.update(value=params["primary_hue"]),
            gr.update(value=params["secondary_hue"]),
            gr.update(value=params["neutral_hue"]),
            gr.update(value=params["font"])
        )
    elif selected_theme_name in theme_settings.get("custom_themes", {}):
        params = theme_settings["custom_themes"][selected_theme_name]
        # カスタムテーマのフォントはリストで保存されている
        font_name = params.get("font", ["Noto Sans JP"])[0]
        return (
            gr.update(value=params.get("primary_hue")),
            gr.update(value=params.get("secondary_hue")),
            gr.update(value=params.get("neutral_hue")),
            gr.update(value=font_name)
        )
    return gr.update(), gr.update(), gr.update(), gr.update()

def handle_save_custom_theme(
    theme_settings, new_name,
    primary_hue, secondary_hue, neutral_hue, font
):
    """「カスタムテーマとして保存」ボタンのロジック。"""
    if not new_name or not new_name.strip():
        gr.Warning("新しいテーマ名を入力してください。")
        return theme_settings, gr.update(), gr.update()

    new_name = new_name.strip()
    if new_name.startswith("---") or new_name in ["Soft", "Default", "Monochrome", "Glass"]:
        gr.Warning("その名前はプリセットテーマ用に予約されています。")
        return theme_settings, gr.update(), gr.update(value="")

    custom_themes = theme_settings.get("custom_themes", {})
    custom_themes[new_name] = {
        "primary_hue": primary_hue,
        "secondary_hue": secondary_hue,
        "neutral_hue": neutral_hue,
        "font": [font] # フォントはリスト形式で保存
    }
    theme_settings["custom_themes"] = custom_themes
    config_manager.save_theme_settings(theme_settings.get("active_theme", "Soft"), custom_themes)

    gr.Info(f"カスタムテーマ「{new_name}」を保存しました。")

    # ドロップダウンの選択肢を更新
    preset_themes = ["Soft", "Default", "Monochrome", "Glass"]
    custom_theme_names = sorted(custom_themes.keys())
    all_choices = ["--- プリセット ---"] + preset_themes + ["--- カスタム ---"] + custom_theme_names

    # 保存したテーマを選択状態にする
    return theme_settings, gr.update(choices=all_choices, value=new_name), ""

def handle_apply_theme(theme_settings, selected_theme_name):
    """「このテーマを適用」ボタンのロジック。"""
    if selected_theme_name.startswith("---"):
        gr.Warning("適用する有効なテーマを選択してください。")
        return

    custom_themes = theme_settings.get("custom_themes", {})
    config_manager.save_theme_settings(selected_theme_name, custom_themes)
    gr.Info(f"テーマ「{selected_theme_name}」を適用設定にしました。アプリケーションを再起動してください。")


def handle_chatbot_edit(
    updated_chatbot_value: list,
    room_name: str,
    api_history_limit: str,
    mapping_list: list,
    add_timestamp: bool,
    evt: gr.SelectData
):
    """
    GradioのChatbot編集イベントを処理するハンドラ (v9: The Final Truth)。
    """
    if not room_name or evt.index is None or not mapping_list:
        return gr.update(), gr.update()

    try:
        # ▼▼▼【この try ブロックの先頭に追加】▼▼▼
        room_manager.create_backup(room_name, 'log')

        # --- [ステップ1: 必要な情報を取得] ---
        edited_ui_index = evt.index[0]
        edited_markdown_string = updated_chatbot_value[edited_ui_index][evt.index[1]]

        log_f, _, _, _, _, _ = get_room_files_paths(room_name)
        all_messages = utils.load_chat_log(log_f)
        original_log_index = mapping_list[edited_ui_index]

        if not (0 <= original_log_index < len(all_messages)):
            gr.Error(f"編集対象のメッセージを特定できませんでした。(インデックス範囲外: {original_log_index})")
            return gr.update(), gr.update()

        original_message = all_messages[original_log_index]
        original_content = original_message.get('content', '')

        # --- [ステップ2: タイムスタンプと思考ログを分離・保持] ---
        timestamp_match = re.search(r'(\n\n\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}$)', original_content)
        preserved_timestamp = timestamp_match.group(1) if timestamp_match else ""

        # 元のログから思考ログのタグ形式を検出（新形式 [THOUGHT] か旧形式 【Thoughts】）
        original_uses_new_format = bool(re.search(r"\[THOUGHT\]", original_content, re.IGNORECASE))

        # UI側のMarkdownコードブロック（```...```）から思考ログを抽出
        thoughts_pattern = re.compile(r"```\n([\s\S]*?)\n```")
        thoughts_match = thoughts_pattern.search(edited_markdown_string)
        new_thoughts_block = ""
        if thoughts_match:
            inner_thoughts = thoughts_match.group(1).strip()
            # 元のログ形式に合わせてタグを生成
            if original_uses_new_format:
                new_thoughts_block = f"[THOUGHT]\n{inner_thoughts}\n[/THOUGHT]"
            else:
                new_thoughts_block = f"【Thoughts】\n{inner_thoughts}\n【/Thoughts】"

        temp_string = thoughts_pattern.sub("", edited_markdown_string)

        # --- [ステップ3: 最終確定版 - 行ベースでの話者名除去] ---
        new_body_text = ""
        lines = temp_string.splitlines() # 文字列を行のリストに分割

        if lines:
            first_line = lines[0].strip()
            # 最初の行が話者名行のパターン（**で始まり、:を含む）に一致するかチェック
            if first_line.startswith('**') and ':' in first_line:
                # 2行目以降を結合して本文とする
                new_body_text = "\n".join(lines[1:]).strip()
            else:
                # パターンに一致しない場合、全行を本文とする
                new_body_text = "\n".join(lines).strip()
        else:
             new_body_text = ""

        # --- [ステップ4: 全てのパーツを再結合] ---
        final_parts = [part.strip() for part in [new_thoughts_block, new_body_text] if part.strip()]
        new_content_without_ts = "\n\n".join(final_parts)
        final_content = new_content_without_ts + preserved_timestamp

        # --- [ステップ5: ログの上書きとUIの更新] ---
        original_message['content'] = final_content
        utils._overwrite_log_file(log_f, all_messages)

        gr.Info(f"メッセージを編集し、ログを更新しました。")

    except Exception as e:
        gr.Error(f"メッセージの編集中にエラーが発生しました: {e}")
        traceback.print_exc()

    history, new_mapping_list = reload_chat_log(room_name, api_history_limit, add_timestamp)
    return history, new_mapping_list

def handle_save_backup_rotation_count(count: int):
    """バックアップの最大保存件数をconfig.jsonに保存する。"""
    if count is None or not isinstance(count, (int, float)) or count < 1:
        gr.Warning("バックアップ保存件数は1以上の整数で指定してください。")
        return

    int_count = int(count)
    if config_manager.save_config_if_changed("backup_rotation_count", int_count):
        gr.Info(f"バックアップの最大保存件数を {int_count} 件に設定しました。")

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
def _load_time_settings_for_room(room_name: str) -> Dict[str, Any]:
    """ルームの設定ファイルから時間設定を読み込むヘルパー関数。"""
    room_config = room_manager.get_room_config(room_name)
    settings = (room_config or {}).get("time_settings", {})

    season_map_en_to_ja = {"spring": "春", "summer": "夏", "autumn": "秋", "winter": "冬"}
    time_map_en_to_ja = {"morning": "朝", "daytime": "昼", "evening": "夕方", "night": "夜"}

    mode = settings.get("mode", "realtime")
    season_en = settings.get("fixed_season", "autumn")
    time_en = settings.get("fixed_time_of_day", "night")

    return {
        "mode": "リアル連動" if mode == "realtime" else "選択する",
        "fixed_season_ja": season_map_en_to_ja.get(season_en, "秋"),
        "fixed_time_of_day_ja": time_map_en_to_ja.get(time_en, "夜"),
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
        season_map_ja_to_en = {"春": "spring", "夏": "summer", "秋": "autumn", "冬": "winter"}
        time_map_ja_to_en = {"朝": "morning", "昼": "daytime", "夕方": "evening", "夜": "night"}
        new_time_settings["fixed_season"] = season_map_ja_to_en.get(season_ja, "autumn")
        new_time_settings["fixed_time_of_day"] = time_map_ja_to_en.get(time_of_day_ja, "night")

    try:
        config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        config = room_manager.get_room_config(room_name) or {}
        
        # 現在の設定と比較し、変更がなければ何もしない
        current_time_settings = config.get("time_settings", {})
        if current_time_settings == new_time_settings:
            return # 変更がないので終了

        config["time_settings"] = new_time_settings
        
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            
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
    season_map_ja_to_en = {"春": "spring", "夏": "summer", "秋": "autumn", "冬": "winter"}
    time_map_ja_to_en = {"朝": "morning", "昼": "daytime", "夕方": "evening", "夜": "night"}
    season_en = season_map_ja_to_en.get(season_ja, "autumn")
    time_en = time_map_ja_to_en.get(time_of_day_ja, "night")

    # 次に、configファイルから現在の設定を読み込む
    current_config = room_manager.get_room_config(room_name) or {}
    current_settings = current_config.get("time_settings", {})
    current_mode = current_settings.get("mode", "realtime")
    current_season = current_settings.get("fixed_season", "autumn")
    current_time = current_settings.get("fixed_time_of_day", "night")

    # 最後に、現在の設定とUIからの入力値を比較する
    is_unchanged = (
        current_mode == mode_en and
        (mode_en == "realtime" or (current_season == season_en and current_time == time_en))
    )
    if is_unchanged:
        return gr.update(), gr.update() # 変更がなければ何もしない

    # --- ここから下は、本当に設定が変更された場合のみ実行される ---
    print(f"--- UIからの時間設定変更処理開始: ルーム='{room_name}' ---")
    
    # APIキーの有効性チェック
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        return "（APIキーが設定されていません）", None
        
    # 1. 設定を保存 (内部で差分をチェックするので冗長ではない)
    handle_save_time_settings(room_name, mode, season_ja, time_of_day_ja)

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

def _get_knowledge_files(room_name: str) -> List[Dict]:
    """指定されたルームのknowledgeフォルダ内のファイル情報をリストで取得する。"""
    knowledge_dir = Path(constants.ROOMS_DIR) / room_name / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    files_info = []
    for file_path in knowledge_dir.iterdir():
        if file_path.is_file():
            stat = file_path.stat()
            files_info.append({
                "ファイル名": file_path.name,
                "サイズ (KB)": f"{stat.st_size / 1024:.2f}",
                "最終更新日時": datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            })
    # ファイル名でソートして返す
    return sorted(files_info, key=lambda x: x["ファイル名"])

def _get_knowledge_status(room_name: str) -> str:
    """知識ベースの現在の状態（索引の有無など）を示す文字列を返す。"""
    base_dir = Path(constants.ROOMS_DIR) / room_name / "rag_data"
    static_index = base_dir / "faiss_index_static"
    dynamic_index = base_dir / "faiss_index_dynamic"
    
    # 静的または動的、どちらかのインデックスが存在すれば「作成済み」とみなす
    is_created = (static_index.exists() and any(static_index.iterdir())) or \
                 (dynamic_index.exists() and any(dynamic_index.iterdir()))

    if is_created:
        return "✅ 索引は作成済みです。（知識ベースやログが更新された場合は、再構築ボタンを押してください）"
    else:
        return "⚠️ 索引がまだ作成されていません。「索引を作成 / 更新」ボタンを押してください。"

def handle_knowledge_tab_load(room_name: str):
    """「知識」タブが選択されたときの初期化処理。"""
    if not room_name:
        return pd.DataFrame(), "ルームが選択されていません。"

    files_df = pd.DataFrame(_get_knowledge_files(room_name))
    status_text = _get_knowledge_status(room_name)

    return files_df, status_text

def handle_knowledge_file_upload(room_name: str, files: List[Any]):
    """知識ベースにファイルをアップロードする処理。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update()
    if not files:
        return gr.update(), gr.update()

    knowledge_dir = Path(constants.ROOMS_DIR) / room_name / "knowledge"

    for temp_file in files:
        original_filename = Path(temp_file.name).name
        target_path = knowledge_dir / original_filename
        shutil.move(temp_file.name, str(target_path))
        print(f"--- [Knowledge] ファイルをアップロードしました: {target_path} ---")

    gr.Info(f"{len(files)}個のファイルを知識ベースに追加しました。索引の更新が必要です。")

    files_df = pd.DataFrame(_get_knowledge_files(room_name))
    return files_df, "⚠️ 索引の更新が必要です。「索引を作成 / 更新」ボタンを押してください。"

def handle_knowledge_file_select(df: pd.DataFrame, evt: gr.SelectData) -> Optional[int]:
    """
    knowledge_file_dfで項目が選択されたときに、そのインデックスを返す。
    デバッグ用のprint文も含む。
    """
    if evt.index is None:
        selected_index = None
    else:
        selected_index = evt.index[0]
    
    return selected_index


def handle_knowledge_file_delete(room_name: str, selected_index: Optional[int]):
    """選択された知識ベースのファイルを削除する処理。"""
    
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update(), None

    # ▼▼▼【evt.index を selected_index に変更】▼▼▼
    if selected_index is None:
        gr.Warning("削除するファイルをリストから選択してください。")
        return gr.update(), gr.update(), None # 3つの値を返す
    # ▲▲▲【変更はここまで】▲▲▲

    try:
        current_files = _get_knowledge_files(room_name)
        if not (0 <= selected_index < len(current_files)):
            gr.Error("選択されたファイルが見つかりません。リストが古い可能性があります。")
            # 失敗した場合でも、最新のリストでUIを更新して終了
            return pd.DataFrame(current_files), _get_knowledge_status(room_name), None

        filename_to_delete = current_files[selected_index]["ファイル名"]

        file_path_to_delete = Path(constants.ROOMS_DIR) / room_name / "knowledge" / filename_to_delete

        if file_path_to_delete.exists():
            file_path_to_delete.unlink()
            gr.Info(f"ファイル「{filename_to_delete}」を削除しました。索引の更新が必要です。")
        else:
            gr.Warning(f"ファイル「{filename_to_delete}」が見つかりませんでした。")
            
    except (IndexError, KeyError) as e:
        gr.Error(f"ファイルの特定に失敗しました: {e}")

    # 処理後、再度ファイルリストを読み込んでUIを更新
    updated_files_df = pd.DataFrame(_get_knowledge_files(room_name))
    # 削除後は選択状態を解除するために None を返す
    return updated_files_df, "⚠️ 索引の更新が必要です。「索引を作成 / 更新」ボタンを押してください。", None

def handle_knowledge_reindex(room_name: str, api_key_name: str):
    """知識ベースの索引を作成/更新する。RAGManagerを使用。"""
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        yield gr.update(), gr.update()
        return

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        yield gr.update(), gr.update()
        return

    # 処理開始を通知
    yield "処理中: 知識ドキュメントのインデックスを構築しています...", gr.update(interactive=False)

    try:
        manager = rag_manager.RAGManager(room_name, api_key)
        # 知識索引のみ更新
        result_message = manager.update_knowledge_index()
        
        gr.Info(f"✅ {result_message}")
        yield f"ステータス: {result_message}", gr.update(interactive=True)

    except Exception as e:
        error_msg = f"索引の作成中にエラーが発生しました: {e}"
        gr.Error(error_msg)
        print(f"--- [知識索引作成エラー] ---")
        traceback.print_exc()
        yield error_msg, gr.update(interactive=True)
        return

    yield _get_knowledge_status(room_name), gr.update(interactive=True)

def _get_rag_index_last_updated(room_name: str, index_type: str = "memory") -> str:
    """指定された索引の最終更新日時を取得する"""
    from pathlib import Path
    import datetime
    
    if index_type == "memory":
        index_path = Path("characters") / room_name / "rag_data" / "faiss_index_static"
    elif index_type == "current_log":
        index_path = Path("characters") / room_name / "rag_data" / "current_log_index"
    else:
        return "不明"
    
    if not index_path.exists():
        return "未作成"
    
    try:
        # フォルダの最終更新時刻を取得
        mtime = index_path.stat().st_mtime
        dt = datetime.datetime.fromtimestamp(mtime)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "取得失敗"

def handle_sleep_consolidation_change(room_name: str, update_episodic: bool, update_memory_index: bool, update_current_log: bool, update_entity: bool = True, compress_episodes: bool = False):
    """睡眠時記憶整理設定を即座に保存する"""
    if not room_name:
        return
    
    try:
        updates = {
            "sleep_consolidation": {
                "update_episodic_memory": bool(update_episodic),
                "update_memory_index": bool(update_memory_index),
                "update_current_log_index": bool(update_current_log),
                "update_entity_memory": bool(update_entity),
                "compress_old_episodes": bool(compress_episodes)
            }
        }
        room_manager.update_room_config(room_name, updates)
        # print(f"--- [睡眠時記憶整理] 設定保存: {room_name} ---")
    except Exception as e:
        print(f"--- [睡眠時記憶整理] 設定保存エラー: {e} ---")

def handle_compress_episodes(room_name: str, api_key_name: str):
    """エピソード記憶を手動で圧縮する"""
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        return "エラー: ルームとAPIキーを選択してください。"

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        return "エラー: APIキーが無効です。"

    try:
        manager = EpisodicMemoryManager(room_name)
        result = manager.compress_old_episodes(api_key)
        
        # 実行後の最新統計を取得してステータス文字列を更新
        stats = manager.get_compression_stats()
        last_date = stats["last_compressed_date"] or "なし"
        pending = stats["pending_count"]
        full_status = f"{last_date}まで圧縮済み (対象: {pending}件) | 最終: {result}"
        
        # 最終実行結果を room_config.json に保存
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        config = {}
        if os.path.exists(room_config_path):
            with open(room_config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        config["last_compression_result"] = result
        with open(room_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        gr.Info(f"✅ {result}")
        return full_status
    except Exception as e:
        error_msg = f"圧縮中にエラーが発生しました: {e}"
        gr.Error(error_msg)
        traceback.print_exc()
        return error_msg

def handle_embedding_mode_change(room_name: str, embedding_mode: str):
    """エンベディングモード設定を保存する"""
    if not room_name:
        return
    
    try:
        room_manager.update_room_config(room_name, {"embedding_mode": embedding_mode})
        
        mode_name = "ローカル" if embedding_mode == "local" else "Gemini API"
        gr.Info(f"📌 エンベディングモードを「{mode_name}」に変更しました。次回の索引更新から適用されます。")
        print(f"--- [Embedding Mode] {room_name}: {embedding_mode} ---")
    except Exception as e:
        print(f"--- [Embedding Mode] 設定保存エラー: {e} ---")

def handle_memory_reindex(room_name: str, api_key_name: str):
    """記憶の索引（過去ログ、エピソード記憶、夢日記、日記ファイル）を更新する（リアルタイム進捗表示付き）。"""
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        yield gr.update(), gr.update()
        return

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        yield gr.update(), gr.update()
        return

    yield "開始中...", gr.update(interactive=False)

    try:
        manager = rag_manager.RAGManager(room_name, api_key)
        
        last_message = ""
        for current_step, total_steps, status_message in manager.update_memory_index_with_progress():
            last_message = status_message
            yield f"{status_message}", gr.update(interactive=False)
        
        gr.Info(f"✅ {last_message}")
        last_updated = _get_rag_index_last_updated(room_name, "memory")
        yield f"{last_message}（最終更新: {last_updated}）", gr.update(interactive=True)

    except Exception as e:
        error_msg = f"記憶索引の作成中にエラーが発生しました: {e}"
        gr.Error(error_msg)
        print(f"--- [記憶索引作成エラー] ---")
        traceback.print_exc()
        yield error_msg, gr.update(interactive=True)
        return

def handle_full_reindex(room_name: str, api_key_name: str):
    """すべての索引を削除し、現在のモデル設定で完全に作成し直す（リアルタイム進捗表示付き）。"""
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        yield gr.update(), gr.update()
        return

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        yield gr.update(), gr.update()
        return

    yield "インデックス消去中...", gr.update(interactive=False)

    try:
        manager = rag_manager.RAGManager(room_name, api_key)
        
        last_message = ""
        # manager.rebuild_all_indices は内部で進捗を callback で報告するように作る（または generator 化する）
        # 現状の update_memory_index_with_progress 方式を流用するため、直接 rebuild メソッドを generator として定義するか、
        # rebuild メソッド内で yield させる。
        
        # 修正: rebuild_all_indices を generator 化するのは大変なので、まず消去して、そのあと通常の進捗付きを呼ぶ
        def status_callback(msg):
            nonlocal last_message
            last_message = msg
        
        # 索引消去 & 再構築 (これは rag_manager のメソッド)
        # ※ rebuild_all_indices が generator でない場合は、こちらで yield する。
        # rag_manager に追加した rebuild_all_indices は generator ではないため、
        # update_memory_index_with_progress 等を直接呼ぶようにここで展開するか、
        # rag_manager 側を yield 対応にする。
        
        # 面倒なのでここで「消去」を行ってから handle_memory_reindex のロジックを呼ぶ
        manager.rebuild_all_indices(status_callback=lambda m: print(f"[Rebuild Status] {m}"))
        
        # 上記で消去済みなので、改めて進捗付きで実行
        for current_step, total_steps, status_message in manager.update_memory_index_with_progress():
            last_message = status_message
            yield f"再構築中: {status_message}", gr.update(interactive=False)
            
        gr.Info(f"✅ インデックスの完全再構築が完了しました")
        last_updated = _get_rag_index_last_updated(room_name, "memory")
        yield f"再構築完了（最終更新: {last_updated}）", gr.update(interactive=True)

    except Exception as e:
        error_msg = f"再構築中にエラーが発生しました: {e}"
        gr.Error(error_msg)
        traceback.print_exc()
        yield error_msg, gr.update(interactive=True)
        return

def handle_current_log_reindex(room_name: str, api_key_name: str):
    """現行ログ（log.txt）の索引を更新する（リアルタイム進捗表示付き）。"""
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        yield gr.update(), gr.update()
        return

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        yield gr.update(), gr.update()
        return

    yield "開始中...", gr.update(interactive=False)

    try:
        manager = rag_manager.RAGManager(room_name, api_key)
        
        last_message = ""
        for batch_num, total_batches, status_message in manager.update_current_log_index_with_progress():
            last_message = status_message
            yield f"{status_message}", gr.update(interactive=False)
        
        gr.Info(f"✅ {last_message}")
        last_updated = _get_rag_index_last_updated(room_name, "current_log")
        yield f"{last_message}（最終更新: {last_updated}）", gr.update(interactive=True)

    except Exception as e:
        error_msg = f"現行ログ索引の作成中にエラーが発生しました: {e}"
        gr.Error(error_msg)
        print(f"--- [現行ログ索引作成エラー] ---")
        traceback.print_exc()
        yield error_msg, gr.update(interactive=True)
        return

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
    """
    添付ファイルの選択が変更された後にトークン数を更新する専用ハンドラ。
    """

    if not room_name or not api_key_name:
        return "トークン数: -"

    parts_for_api = []

    # 1. テキスト入力欄の現在の内容を追加
    textbox_content = multimodal_input.get("text", "") if multimodal_input else ""
    if textbox_content:
        parts_for_api.append(textbox_content)

    # 2. テキスト入力欄に「添付されているがまだ送信されていない」ファイルを追加
    file_list_in_textbox = multimodal_input.get("files", []) if multimodal_input else []
    if file_list_in_textbox:
        for file_obj in file_list_in_textbox:
            try:
                if hasattr(file_obj, 'name') and file_obj.name and os.path.exists(file_obj.name):
                    file_path = file_obj.name
                    kind = filetype.guess(file_path)
                    if kind and kind.mime.startswith('image/'):
                        parts_for_api.append(Image.open(file_path))
                    else:
                        file_basename = os.path.basename(file_path)
                        file_size = os.path.getsize(file_path)
                        parts_for_api.append(f"[ファイル添付: {file_basename}, サイズ: {file_size} bytes]")
            except Exception as e:
                print(f"トークン計算中のテキストボックス内ファイル処理エラー: {e}")
                parts_for_api.append(f"[ファイル処理エラー]")

    # 3. active_attachments_state から渡された「アクティブな添付ファイル」のリストを処理
    if active_attachments:
        for file_path in active_attachments:
            try:
                kind = filetype.guess(file_path)
                if kind and kind.mime.startswith('image/'):
                    parts_for_api.append(Image.open(file_path))
                else: # 画像以外はテキストとして内容を読み込む
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    parts_for_api.append(content)
            except Exception as e:
                print(f"トークン計算中のアクティブ添付ファイル処理エラー: {e}")
                parts_for_api.append(f"[添付ファイル処理エラー: {os.path.basename(file_path)}]")

    # 4. 最終的なトークン数を計算
    effective_settings = config_manager.get_effective_settings(
        room_name,
        add_timestamp=add_timestamp, send_thoughts=send_thoughts,
        send_notepad=send_notepad, use_common_prompt=use_common_prompt,
        send_core_memory=send_core_memory, send_scenery=send_scenery
    )

    effective_settings.pop("api_history_limit", None)
    effective_settings.pop("api_key_name", None)  # 重複防止

    estimated_count = gemini_api.count_input_tokens(
        room_name=room_name, api_key_name=api_key_name,
        api_history_limit=api_history_limit, parts=parts_for_api, **effective_settings
    )
    return _format_token_display(room_name, estimated_count)

def _reset_play_audio_on_failure():
    """「選択した発言を再生」ボタンが失敗したときに、UIを元の状態に戻す。"""
    return (
        gr.update(visible=False), # audio_player
        gr.update(value="🔊 選択した発言を再生", interactive=True), # play_audio_button
        gr.update(interactive=True) # rerun_button
    )

def _reset_preview_on_failure():
    """「試聴」ボタンが失敗したときに、UIを元の状態に戻す。"""
    return (
        gr.update(visible=False), # audio_player
        gr.update(interactive=True), # play_audio_button
        gr.update(value="試聴", interactive=True) # room_preview_voice_button
    )

# --- Theme Management Handlers (v2) ---

def _get_theme_previews(theme_name: str) -> Tuple[Optional[str], Optional[str]]:
    """指定されたテーマ名のライト/ダーク両方のプレビュー画像パスを返す。なければNoneを返す。"""
    base_path = Path("assets/theme_previews")
    # プレースホルダー画像が存在しない場合も考慮
    placeholder_path = base_path / "no_preview.png"
    placeholder = str(placeholder_path) if placeholder_path.exists() else None

    light_path = base_path / f"{theme_name}_light.png"
    dark_path = base_path / f"{theme_name}_dark.png"

    light_preview = str(light_path) if light_path.exists() else placeholder
    dark_preview = str(dark_path) if dark_path.exists() else placeholder
    
    return light_preview, dark_preview

def handle_theme_tab_load():
    """テーマタブが選択されたときに、設定を読み込んでUIを初期化する。"""
    all_themes_map = config_manager.get_all_themes()
    
    # UIドロップダウン用の選択肢リストを作成
    choices = []
    # カテゴリごとに区切り線と項目を追加
    if any(src == "file" for src in all_themes_map.values()):
        choices.append("--- ファイルベース ---")
        choices.extend([name for name, src in all_themes_map.items() if src == "file"])
    if any(src == "json" for src in all_themes_map.values()):
        choices.append("--- カスタム (JSON) ---")
        choices.extend([name for name, src in all_themes_map.items() if src == "json"])
    if any(src == "preset" for src in all_themes_map.values()):
        choices.append("--- プリセット ---")
        choices.extend([name for name, src in all_themes_map.items() if src == "preset"])
        
    active_theme_name = config_manager.CONFIG_GLOBAL.get("theme_settings", {}).get("active_theme", "nexus_ark_theme")
    
    # 最初のプレビュー画像
    light_preview, dark_preview = _get_theme_previews(active_theme_name)
    
    return gr.update(choices=choices, value=active_theme_name), light_preview, dark_preview

def handle_theme_selection(selected_theme_name: str):
    """ドロップダウンでテーマが選択されたときに、プレビューUIとカスタマイズUIを更新する。"""
    if not selected_theme_name or selected_theme_name.startswith("---"):
        # 区切り線が選択された場合は、何も更新しない
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(interactive=False), gr.update(interactive=False)

    all_themes_map = config_manager.get_all_themes()
    theme_source = all_themes_map.get(selected_theme_name, "preset")

    # サムネイルを更新
    light_preview, dark_preview = _get_theme_previews(selected_theme_name)
    
    # カスタマイズUIの値を更新
    params = {}
    is_editable = True

    # プリセットテーマの定義
    preset_params = {
        "Soft": {"primary_hue": "blue", "secondary_hue": "sky", "neutral_hue": "slate", "font": ["Source Sans Pro"]},
        "Default": {"primary_hue": "orange", "secondary_hue": "amber", "neutral_hue": "gray", "font": ["Noto Sans"]},
        "Monochrome": {"primary_hue": "neutral", "secondary_hue": "neutral", "neutral_hue": "neutral", "font": ["IBM Plex Mono"]},
        "Glass": {"primary_hue": "teal", "secondary_hue": "cyan", "neutral_hue": "gray", "font": ["Quicksand"]},
    }

    if theme_source == "preset":
        params = preset_params.get(selected_theme_name, {})
    elif theme_source == "json":
        params = config_manager.CONFIG_GLOBAL.get("theme_settings", {}).get("custom_themes", {}).get(selected_theme_name, {})
    elif theme_source == "file":
        is_editable = False # ファイルベースのテーマは直接編集不可
        # UI内に説明テキストを配置するため、ポップアップは出さない
        params = preset_params["Soft"]

    font_name = params.get("font", ["Source Sans Pro"])[0]

    return (
        light_preview,
        dark_preview,
        gr.update(value=params.get("primary_hue"), interactive=is_editable),
        gr.update(value=params.get("secondary_hue"), interactive=is_editable),
        gr.update(value=params.get("neutral_hue"), interactive=is_editable),
        gr.update(value=font_name, interactive=is_editable),
        gr.update(interactive=is_editable), # Save button
        gr.update(interactive=is_editable)  # Export button
    )

def handle_save_custom_theme(new_name, primary_hue, secondary_hue, neutral_hue, font):
    """「カスタムテーマとして保存」ボタンのロジック。config.jsonに保存する。"""
    if not new_name or not new_name.strip():
        gr.Warning("新しいテーマ名を入力してください。")
        return gr.update(), gr.update()

    new_name = new_name.strip()
    # プリセットテーマ名やファイルベースのテーマ名との重複もチェック
    all_themes_map = config_manager.get_all_themes()
    if new_name in all_themes_map and all_themes_map[new_name] != "json":
        gr.Warning(f"名前「{new_name}」はファイルテーマまたはプリセットテーマとして既に存在します。")
        return gr.update(), gr.update(value="")
        
    current_config = config_manager.load_config_file()
    theme_settings = current_config.get("theme_settings", {})
    custom_themes = theme_settings.get("custom_themes", {})
    
    custom_themes[new_name] = {
        "primary_hue": primary_hue, "secondary_hue": secondary_hue,
        "neutral_hue": neutral_hue, "font": [font]
    }
    theme_settings["custom_themes"] = custom_themes
    config_manager.save_config("theme_settings", theme_settings)
    
    # グローバル変数を更新して即時反映
    config_manager.load_config()
    
    gr.Info(f"カスタムテーマ「{new_name}」をJSONとして保存しました。")
    
    # ドロップダウンの選択肢を再生成して更新
    updated_choices, _, _ = handle_theme_tab_load()
    
    return updated_choices, gr.update(value="") # フォームをクリア

def handle_export_theme_to_file(new_name, primary_hue, secondary_hue, neutral_hue, font):
    """「ファイルにエクスポート」ボタンのロジック。"""
    if not new_name or not new_name.strip():
        gr.Warning("ファイル名として使用するテーマ名を入力してください。")
        return gr.update()

    file_name = new_name.strip().replace(" ", "_").lower()
    file_name = re.sub(r'[^a-z0-9_]', '', file_name) # 安全なファイル名に
    if not file_name:
        gr.Warning("有効なファイル名を生成できませんでした。")
        return gr.update()

    themes_dir = Path("themes")
    themes_dir.mkdir(exist_ok=True)
    file_path = themes_dir / f"{file_name}.py"

    if file_path.exists():
        gr.Warning(f"テーマファイル '{file_path.name}' は既に存在します。")
        return gr.update()

    # Pythonファイルの内容を生成
    # Gradioのテーマオブジェクトを正しく構築するためのテンプレート
    content = textwrap.dedent(f"""
        import gradio as gr

        def load():
            \"\"\"Gradioテーマオブジェクトを返す。この関数は必須です。\"\"\"
            theme = gr.themes.Default(
                primary_hue="{primary_hue}",
                secondary_hue="{secondary_hue}",
                neutral_hue="{neutral_hue}",
                font=[gr.themes.GoogleFont("{font}")]
            ).set(
                # ここに他の.set()パラメータを追加できます
            )
            return theme
    """)
    
    try:
        file_path.write_text(content.strip(), encoding="utf-8")
        gr.Info(f"テーマをファイル '{file_path.name}' としてエクスポートしました。")
        # グローバルキャッシュをクリアして次回タブを開いたときに再読み込みさせる
        config_manager._file_based_themes_cache.clear()
        return "" # テキストボックスをクリア
    except Exception as e:
        gr.Error(f"テーマファイルのエクスポート中にエラーが発生しました: {e}")
        return gr.update()


def handle_apply_theme(selected_theme_name: str):
    """「このテーマを適用」ボタンのロジック。"""
    if not selected_theme_name or selected_theme_name.startswith("---"):
        gr.Warning("適用する有効なテーマを選択してください。")
        return

    current_config = config_manager.load_config_file()
    theme_settings = current_config.get("theme_settings", {})
    theme_settings["active_theme"] = selected_theme_name
    
    config_manager.save_config_if_changed("theme_settings", theme_settings)
    
    gr.Info(f"テーマ「{selected_theme_name}」を適用設定にしました。アプリケーションを再起動してください。")


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
        season_map = {"春": "spring", "夏": "summer", "秋": "autumn", "冬": "winter"}
        time_map = {"早朝": "early_morning", "朝": "morning", "昼前": "late_morning", "昼下がり": "afternoon", "夕方": "evening", "夜": "night", "深夜": "midnight"}
        season_en = season_map.get(season_ja)
        time_en = time_map.get(time_ja)

        if not season_en or not time_en:
            raise ValueError("季節または時間帯の変換に失敗しました。")

        save_dir = Path(constants.ROOMS_DIR) / room_name / "spaces" / "images"
        save_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{location}_{season_en}_{time_en}.png"
        save_path = save_dir / filename

        img = Image.open(image_path)
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
    Google用設定とOpenAI用設定の表示/非表示を切り替える。
    """
    # ラジオボタンからは内部ID（"google" or "openai"）が渡される
    provider_id = provider_choice
    
    # 設定ファイルに保存
    config_manager.set_active_provider(provider_id)
    
    is_google = (provider_id == "google")
    is_zhipu = (provider_id == "zhipu")
    is_openai = (provider_id == "openai")
    
    # Google(Visible), Zhipu(Visible), OpenAI(Visible) の順で返す
    return gr.update(visible=is_google), gr.update(visible=is_zhipu), gr.update(visible=is_openai)

def handle_room_provider_change(provider_choice: str):
    """
    ルーム設定のAIプロバイダ選択が変更された時の処理。
    """
    is_google = (provider_choice == "google")
    is_openai = (provider_choice == "openai")
    
    # room_google_group, room_openai_group の順でVisibilityを返す
    return gr.update(visible=is_google), gr.update(visible=is_openai)

    # グローバルと同じロジックでリストを更新し、ルーム用のドロップダウンを更新する
    result = handle_fetch_zhipu_models(api_key)
    return result # そのままgr.updateを返す



def handle_openai_profile_select(profile_name: str):
    """
    OpenAI互換設定のドロップダウン（OpenRouter/Groq/Ollama）が選択された時、
    そのプロファイルの保存済み設定を入力欄に反映する。
    """
    config_manager.set_active_openai_profile(profile_name)

    settings_list = config_manager.get_openai_settings_list()
    target_setting = next((s for s in settings_list if s["name"] == profile_name), None)
    
    if not target_setting:
        return "", "", gr.update()
    
    # モデルリスト（choices）も含めて更新
    available_models = target_setting.get("available_models", [])
    default_model = target_setting.get("default_model", "")
    
    return (
        target_setting.get("base_url", ""),
        target_setting.get("api_key", ""),
        gr.update(choices=available_models, value=default_model)
    )

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

def handle_provider_change(provider_choice: str):
    """
    AIプロバイダの選択（ラジオボタン）が変更された時の処理。
    Google用設定とOpenAI用設定の表示/非表示を切り替える。
    """
    # UIの表示名から内部IDへ変換 (Valueが直接渡ってくる場合はそのまま)
    provider_id = provider_choice 
    
    # 設定ファイルに保存
    config_manager.set_active_provider(provider_id)
    
    is_google = (provider_id == "google")
    
    # Google設定(Visible), OpenAI設定(Visible) の順で返す
    return gr.update(visible=is_google), gr.update(visible=not is_google)

def handle_openai_profile_select(profile_name: str):
    """
    OpenAI互換設定のドロップダウン（OpenRouter/Groq/Ollama）が選択された時、
    そのプロファイルの保存済み設定を入力欄に反映する。
    
    Returns:
        Tuple: (base_url, api_key, openai_model_dropdown(with choices and value))
    """
    config_manager.set_active_openai_profile(profile_name)

    settings_list = config_manager.get_openai_settings_list()
    target_setting = next((s for s in settings_list if s["name"] == profile_name), None)
    
    if not target_setting:
        return "", "", gr.update(choices=[], value="")
    
    available_models = target_setting.get("available_models", [])
    default_model = target_setting.get("default_model", "")
    
    # デフォルトモデルがリストにない場合は追加
    if default_model and default_model not in available_models:
        available_models = [default_model] + available_models
        
    return (
        target_setting.get("base_url", ""),
        target_setting.get("api_key", ""),
        gr.update(choices=available_models, value=default_model)
    )

def handle_save_openai_config(profile_name: str, base_url: str, api_key: str, default_model: str, tool_use_enabled: bool = True):
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
    
    # モデルを追加
    available_models.append(model_name)
    settings_list[target_index]["available_models"] = available_models
    
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


def handle_fetch_models(profile_name: str, base_url: str, api_key: str):
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
    fetched_models = config_manager.fetch_models_from_api(base_url, api_key)
    
    if not fetched_models:
        gr.Warning("モデルリストの取得に失敗しました。APIキーやBase URLを確認してください。")
        return gr.update()
    
    # 現在のプロファイル設定を取得
    settings_list = config_manager.get_openai_settings_list()
    for s in settings_list:
        if s["name"] == profile_name:
            current_models = s.get("available_models", [])
            
            # 既存モデル（⭐ マークを除いた名前）のセット
            existing_clean = {m.lstrip("⭐ ") for m in current_models}
            
            # 新規モデルのみ追加
            added_count = 0
            for model in fetched_models:
                if model not in existing_clean:
                    current_models.append(model)
                    added_count += 1
            
            s["available_models"] = current_models
            config_manager.save_openai_settings_list(settings_list)
            
            gr.Info(f"{len(fetched_models)} 件のモデルを取得し、{added_count} 件を追加しました。")
            return gr.update(choices=current_models)
    
    gr.Warning(f"プロファイル「{profile_name}」が見つかりませんでした。")
    return gr.update()


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
    
def _resolve_background_image(room_name: str, settings: dict) -> str:
    """背景画像ソースモードに基づいて、使用すべき画像パスを決定する"""
    mode = settings.get("theme_bg_src_mode", "画像を指定 (Manual)")
    # print(f"DEBUG: Resolving background for {room_name}, Mode: {mode}, Repr: {repr(mode)}")
    
    if mode == "現在地と連動 (Sync)":
        # 現在地から画像を探す
        location_id = utils.get_current_location(room_name)
        if location_id:
            scenery_path = utils.find_scenery_image(room_name, location_id)
            if scenery_path:
                return scenery_path
        # 見つからない場合はNone（背景なし）
        return None
    else:
        # Manualモード: 設定された画像パスを使用
        return settings.get("theme_bg_image", None)

def handle_refresh_background_css(room_name: str) -> str:
    """[v21] 現在地連動背景: 画像生成/登録後にstyle_injectorを更新するためのハンドラ"""
    effective_settings = config_manager.get_effective_settings(room_name)
    return _generate_style_from_settings(room_name, effective_settings)


def _generate_style_from_settings(room_name: str, settings: dict) -> str:
    """設定辞書からCSSを生成するヘルパー（背景画像解決込み）"""
    is_sync = (settings.get("theme_bg_src_mode") == "現在地と連動 (Sync)")
    
    def get_bg_val(key_manual, key_sync, default):
        return settings.get(key_sync if is_sync else key_manual, default)

    return generate_room_style_css(
        settings.get("room_theme_enabled", False),
        settings.get("font_size", 15),
        settings.get("line_height", 1.6),
        settings.get("chat_style", "Chat (Default)"),
        settings.get("theme_primary", None),
        settings.get("theme_secondary", None),
        settings.get("theme_background", None),
        settings.get("theme_text", None),
        settings.get("theme_accent_soft", None),
        settings.get("theme_input_bg", None),
        settings.get("theme_input_border", None),
        settings.get("theme_code_bg", None),
        settings.get("theme_subdued_text", None),
        settings.get("theme_button_bg", None),
        settings.get("theme_button_hover", None),
        settings.get("theme_stop_button_bg", None),
        settings.get("theme_stop_button_hover", None),
        settings.get("theme_checkbox_off", None),
        settings.get("theme_table_bg", None),
        settings.get("theme_radio_label", None),
        settings.get("theme_dropdown_list_bg", None),
        settings.get("theme_ui_opacity", 0.9), # Default 0.9
        _resolve_background_image(room_name, settings),
        get_bg_val("theme_bg_opacity", "theme_bg_sync_opacity", 0.4),
        get_bg_val("theme_bg_blur", "theme_bg_sync_blur", 0),
        get_bg_val("theme_bg_size", "theme_bg_sync_size", "cover"),
        get_bg_val("theme_bg_position", "theme_bg_sync_position", "center"),
        get_bg_val("theme_bg_repeat", "theme_bg_sync_repeat", "no-repeat"),
        get_bg_val("theme_bg_custom_width", "theme_bg_sync_custom_width", "300px"),
        get_bg_val("theme_bg_radius", "theme_bg_sync_radius", 0),
        get_bg_val("theme_bg_mask_blur", "theme_bg_sync_mask_blur", 0),
        get_bg_val("theme_bg_front_layer", "theme_bg_sync_front_layer", False)
    )

# ==========================================
# [v25] テーマ・表示設定管理ロジック
# ==========================================

def generate_room_style_css(enabled=True, font_size=15, line_height=1.6, chat_style="Chat (Default)", 
                             primary=None, secondary=None, bg=None, text=None, accent_soft=None,
                             input_bg=None, input_border=None, code_bg=None, subdued_text=None,
                             button_bg=None, button_hover=None, stop_button_bg=None, stop_button_hover=None, 
                             checkbox_off=None, table_bg=None, radio_label=None, dropdown_list_bg=None, ui_opacity=0.9,
                             bg_image=None, bg_opacity=0.4, bg_blur=0, bg_size="cover", bg_position="center", bg_repeat="no-repeat",
                             bg_custom_width="", bg_radius=0, bg_mask_blur=0, bg_front_layer=False):
    """ルーム個別のCSS（文字サイズ、Novel Mode、テーマカラー）を生成する"""
    
    # 個別テーマが無効の場合は空のCSSを返す
    if not enabled:
        return "<style>#style_injector_component { display: none !important; }</style>"
    
    # Check for None values (Gradio updates might send None)
    if not font_size: font_size = 15
    if not line_height: line_height = 1.6
    
    # 1. Readability & Novel Mode (Common)
    css = f"""
    #chat_output_area .message-bubble, 
    #chat_output_area .message-row .message-bubble,
    #chat_output_area .message-wrap .message,
    #chat_output_area .prose,
    #chat_output_area .prose > *,
    #chat_output_area .prose p,
    #chat_output_area .prose li {{
        font-size: {font_size}px !important;
        line-height: {line_height} !important;
    }}
    #chat_output_area code,
    #chat_output_area pre,
    #chat_output_area pre span {{
        font-size: {int(font_size)*0.9}px !important;
        line-height: {line_height} !important;
    }}
    #style_injector_component {{ display: none !important; }}
    """

    if chat_style == "Novel (Text only)":
        css += """
        #chat_output_area .message-row .message-bubble,
        #chat_output_area .message-row .message-bubble:before,
        #chat_output_area .message-row .message-bubble:after,
        #chat_output_area .message-wrap .message,
        #chat_output_area .message-wrap .message.bot,
        #chat_output_area .message-wrap .message.user,
        #chat_output_area .bot-row .message-bubble,
        #chat_output_area .user-row .message-bubble {
            background: transparent !important;
            background-color: transparent !important;
            border: none !important;
            box-shadow: none !important;
            padding: 0 !important;
            margin: 4px 0 !important;
            border-radius: 0 !important;
        }
        #chat_output_area .message-row,
        #chat_output_area .user-row,
        #chat_output_area .bot-row {
            display: flex !important;
            justify-content: flex-start !important;
            margin-bottom: 12px !important;
            background: transparent !important;
            border: none !important;
            width: 100% !important;
        }
        #chat_output_area .avatar-container { display: none !important; }
        #chat_output_area .message-wrap .message { padding: 0 !important; }
        """

    # 2. Color Theme Overrides
    overrides = []
    
    # メインカラー: Interactive elements (Checkbox, Slider, Loader)
    if primary:
        overrides.append(f"--color-accent: {primary} !important;")
        overrides.append(f"--loader-color: {primary} !important;")
        overrides.append(f"--primary-500: {primary} !important;") # Fallback for some themes
        overrides.append(f"--primary-600: {primary} !important;")

    # サブカラー: Chat bubbles, Panel backgrounds, Item box highlights
    if secondary:
        overrides.append(f"--background-fill-secondary: {secondary} !important;") 
        overrides.append(f"--block-label-background-fill: {secondary} !important;")
        # Custom CSS variable often used for bot bubbles in Nexus Ark
        overrides.append(f"--secondary-500: {secondary} !important;")
        # タブのオーバーフローメニュー（…）のホバー時にサブカラーを適用
        css += f"""
        /* タブのオーバーフローメニューのホバー時 - サブカラーを適用 */
        div.overflow-dropdown button:hover,
        .overflow-dropdown button:hover {{
            background-color: {secondary} !important;
            background: {secondary} !important;
        }}
        /* チャット入力欄全体の背景色（MultiModalTextbox）- サブカラーを適用 */
        #chat_input_multimodal,
        #chat_input_multimodal > div,
        #chat_input_multimodal .block,
        div.block.multimodal-textbox,
        div.block.multimodal-textbox.svelte-1svsvh2,
        div[class*="multimodal-textbox"][class*="block"],
        div.full-container,
        div.full-container.svelte-5gfv2q,
        [aria-label*="ultimedia input field"],
        [aria-label*="ultimedia input field"] > div {{
            background-color: {secondary} !important;
            background: {secondary} !important;
        }}
        """
    
    # タブのオーバーフローメニュー（…）の非ホバー時 - 背景色を適用
    if bg:
        css += f"""
        /* タブのオーバーフローメニュー（…）の背景色 - 非ホバー時 */
        div.overflow-dropdown,
        .overflow-dropdown {{
            background-color: {bg} !important;
            background: {bg} !important;
        }}
        """  

    # 背景色: Overall App Background & Content Boxes
    if bg:
        overrides.append(f"--body-background-fill: {bg} !important;")
        overrides.append(f"--background-fill-primary: {bg} !important;") 
        overrides.append(f"--block-background-fill: {bg} !important;")

    # テキスト色: Body text, labels, headers
    if text:
        overrides.append(f"--body-text-color: {text} !important;")
        overrides.append(f"--block-label-text-color: {text} !important;")
        overrides.append(f"--block-info-text-color: {text} !important;")
        overrides.append(f"--section-header-text-color: {text} !important;")
        overrides.append(f"--prose-text-color: {text} !important;")
        # ダークモード用の変数も追加
        overrides.append(f"--block-label-text-color-dark: {text} !important;")
        # 直接ラベル要素にスタイルを適用（CSS変数が効かない場合の対策）
        # Gradioが生成するdata-testid属性を使用
        css += f"""
        [data-testid="block-info"],
        [data-testid="block-label"],
        span[data-testid="block-info"],
        span[data-testid="block-label"],
        .gradio-container label,
        .gradio-container label span,
        .dark [data-testid="block-info"],
        .dark [data-testid="block-label"],
        .dark label,
        .dark label span {{
            color: {text} !important;
        }}
        """


    # ユーザー発言背景 (Accent Soft)
    if accent_soft:
        overrides.append(f"--color-accent-soft: {accent_soft} !important;")

    # === 詳細設定 ===
    
    # 入力欄の背景色 (Form Background)
    if input_bg:
        overrides.append(f"--input-background-fill: {input_bg} !important;")
        overrides.append(f"--input-background-fill-hover: {input_bg} !important;")
        # スクロールバーも連動させる
        css += f"""
        *::-webkit-scrollbar {{ width: 8px; height: 8px; }}
        *::-webkit-scrollbar-thumb {{
            background-color: {input_bg} !important;
            border-radius: 4px;
        }}
        *::-webkit-scrollbar-track {{ background-color: transparent; }}
        """
    
    # ドロップダウンリストの背景色 (Dropdown List Background)
    if dropdown_list_bg:
        css += f"""
        /* ドロップダウンリストの背景色 */
        ul.options,
        ul.options.svelte-y6qw75,
        .gradio-container ul[role="listbox"],
        .gradio-container .options {{
            background-color: {dropdown_list_bg} !important;
            background: {dropdown_list_bg} !important;
        }}
        """
    
    # 入力欄の枠線色 (Form Border)
    if input_border:
        overrides.append(f"--border-color-primary: {input_border} !important;")
        overrides.append(f"--input-border-color: {input_border} !important;")
        overrides.append(f"--input-border-color-focus: {input_border} !important;")
    
    # コードブロック背景色 (Code Block BG)
    if code_bg:
        overrides.append(f"--code-background-fill: {code_bg} !important;")
        # チャット内のコードブロックにも適用
        css += f"""
        #chat_output_area pre,
        #chat_output_area code,
        .prose pre,
        .prose code {{
            background-color: {code_bg} !important;
        }}
        """
    
    # サブテキスト色（説明文など）
    if subdued_text:
        overrides.append(f"--body-text-color-subdued: {subdued_text} !important;")
        overrides.append(f"--block-info-text-color: {subdued_text} !important;")
        overrides.append(f"--input-placeholder-color: {subdued_text} !important;")
    
    # ボタン背景色（secondaryボタン）
    if button_bg:
        overrides.append(f"--button-secondary-background-fill: {button_bg} !important;")
        overrides.append(f"--button-secondary-background-fill-dark: {button_bg} !important;")
        # 直接セレクターでも適用
        css += f"""
        button.secondary,
        .gradio-container button.secondary {{
            background-color: {button_bg} !important;
        }}
        """
    
    # プライマリーボタン背景色（メインカラーを使用）
    if primary:
        overrides.append(f"--button-primary-background-fill: {primary} !important;")
        overrides.append(f"--button-primary-background-fill-dark: {primary} !important;")
        overrides.append(f"--button-primary-background-fill-hover: {primary} !important;")
        overrides.append(f"--button-primary-background-fill-hover-dark: {primary} !important;")
        css += f"""
        button.primary,
        .gradio-container button.primary {{
            background-color: {primary} !important;
        }}
        button.primary:hover,
        .gradio-container button.primary:hover {{
            background-color: {primary} !important;
            filter: brightness(1.1);
        }}
        """
    
    # ボタンホバー色
    if button_hover:
        overrides.append(f"--button-secondary-background-fill-hover: {button_hover} !important;")
        overrides.append(f"--button-secondary-background-fill-hover-dark: {button_hover} !important;")
        css += f"""
        button.secondary:hover,
        .gradio-container button.secondary:hover {{
            background-color: {button_hover} !important;
        }}
        """
    
    # 停止ボタン背景色（stop/cancelボタン）
    if stop_button_bg:
        overrides.append(f"--button-cancel-background-fill: {stop_button_bg} !important;")
        overrides.append(f"--button-cancel-background-fill-dark: {stop_button_bg} !important;")
        css += f"""
        button.stop,
        button.cancel,
        .gradio-container button.stop,
        .gradio-container button.cancel {{
            background-color: {stop_button_bg} !important;
        }}
        """
    
    # 停止ボタンホバー色
    if stop_button_hover:
        overrides.append(f"--button-cancel-background-fill-hover: {stop_button_hover} !important;")
        overrides.append(f"--button-cancel-background-fill-hover-dark: {stop_button_hover} !important;")
        css += f"""
        button.stop:hover,
        button.cancel:hover,
        .gradio-container button.stop:hover,
        .gradio-container button.cancel:hover {{
            background-color: {stop_button_hover} !important;
        }}
        """

    # チェックボックスオフ時の背景色
    if checkbox_off:
        overrides.append(f"--checkbox-background-color: {checkbox_off} !important;")
        overrides.append(f"--checkbox-background-color-dark: {checkbox_off} !important;")
        css += f"""
        input[type="checkbox"]:not(:checked),
        .gradio-container input[type="checkbox"]:not(:checked),
        .checkbox-container:not(.selected),
        [data-testid="checkbox"]:not(:checked) {{
            background-color: {checkbox_off} !important;
        }}
        """

    # テーブル背景色
    if table_bg:
        overrides.append(f"--table-even-background-fill: {table_bg} !important;")
        overrides.append(f"--table-odd-background-fill: {table_bg} !important;")
        css += f"""
        table,
        .table-container,
        .table-wrap,
        .gradio-container table,
        .gradio-container .table-container,
        [role="grid"] {{
            background-color: {table_bg} !important;
        }}
        table td,
        table th,
        .table-wrap td,
        .table-wrap th {{
            background-color: {table_bg} !important;
        }}
        """

    # ラジオ/チェックボックスのラベル背景色
    if radio_label:
        css += f"""
        /* ラジオボタン・チェックボックスのラベル背景色 */
        label.svelte-1bx8sav,
        .gradio-container label[data-testid*="-radio-label"],
        .gradio-container label[data-testid*="-checkbox-label"] {{
            background-color: {radio_label} !important;
            background: {radio_label} !important;
        }}
        """

    if overrides:
        # Create a more aggressive global override block
        css += f"""
        :root, body, gradio-app, .gradio-container, .dark {{
            {' '.join(overrides)}
        }}
        /* Specific overrides for common containers */
        #chat_output_area, #room_theme_color_settings {{
            {' '.join(overrides)}
        }}
        """

    # 背景画像
    if bg_image:
        import base64
        from PIL import Image
        import io

        bg_image_url = ""
        
        # HTTP URLならそのまま
        if bg_image.startswith("http"):
             bg_image_url = bg_image
        # ローカルファイルならBase64エンコード（リサイズ処理付き）
        elif os.path.exists(bg_image):
            try:
                with Image.open(bg_image) as img:
                    # 最大サイズ制限 (Full HD相当)
                    max_size = 1920
                    if max(img.size) > max_size:
                        ratio = max_size / max(img.size)
                        new_size = (int(img.width * ratio), int(img.height * ratio))
                        img = img.resize(new_size, Image.Resampling.LANCZOS)
                    
                    buffer = io.BytesIO()
                    # JPEG変換して軽量化 (PNGだと重い場合があるが、画質優先ならPNG)
                    # ここでは元のフォーマットに近い形で、ただし透過考慮でPNG推奨
                    img.save(buffer, format="PNG")
                    encoded_string = base64.b64encode(buffer.getvalue()).decode('utf-8')
                    bg_image_url = f"data:image/png;base64,{encoded_string}"
            except Exception as e:
                print(f"Error encoding/resizing background image: {e}")
        
        if bg_image_url:
             # スタンプモード（custom）か壁紙モードか
             is_stamp_mode = (bg_size == "custom" and bg_custom_width)
             
             if is_stamp_mode:
                 # スタンプモード: width/heightを指定し、配置を細かく制御
                 # アスペクト比は維持したいが、CSSのbackground-imageでアスペクト比維持しつつサイズ指定は
                 # containerのサイズを画像に合わせる必要がある。
                 # ここではwidthを基準に、heightはautoにしたいが、fixed要素でheight:auto空だと表示されないことがある。
                 # 正方形またはcontainで表示領域を確保する。
                 
                 size_style = f"width: {bg_custom_width}; height: {bg_custom_width}; background-size: contain;"
                 if bg_repeat == "no-repeat":
                     size_style += " background-repeat: no-repeat;"
                 
                 # 配置ロジック (簡易変換)
                 # ユーザーが "top left" (文字列) を選んだ場合の変換
                 # CSSの background-position は "top left" そのままで有効だが、
                 # fixed要素自体の配置(top, left)とは別。
                 # スタンプモードでは fixed要素自体を動かすのが自然。
                 
                 pos_style = "top: 50%; left: 50%; transform: translate(-50%, -50%);" # Default Center
                 bg_p = bg_position.lower()
                 
                 if bg_p == "top left": pos_style = "top: 20px; left: 20px;"
                 elif bg_p == "top right": pos_style = "top: 20px; right: 20px;"
                 elif bg_p == "bottom left": pos_style = "bottom: 20px; left: 20px;"
                 elif bg_p == "bottom right": pos_style = "bottom: 20px; right: 20px;"
                 elif bg_p == "top": pos_style = "top: 20px; left: 50%; transform: translateX(-50%);"
                 elif bg_p == "bottom": pos_style = "bottom: 20px; left: 50%; transform: translateX(-50%);"
                 elif bg_p == "left": pos_style = "top: 50%; left: 20px; transform: translateY(-50%);"
                 elif bg_p == "right": pos_style = "top: 50%; right: 20px; transform: translateY(-50%);"
                 # center 以外の場合、transformを上書きする形になるので注意
                 
                 # border-radius
                 radius_style = f"border-radius: {bg_radius}%;" if bg_radius else ""
                 bg_p_style = "" # 初期化
                 
             else:
                 # 壁紙モード
                 size_style = f"width: 100%; height: 100%; background-size: {bg_size}; background-repeat: {bg_repeat};"
                 # background-position はCSSプロパティとしてそのまま渡す
                 pos_style = "top: 0; left: 0;"
                 # 壁紙モードでも角丸を適用可能にする
                 radius_style = f"border-radius: {bg_radius}%;" if bg_radius else ""
                 bg_p_style = f"background-position: {bg_position};"

             # エッジぼかし (Mask) - 両方のモードで有効
             mask_style = ""
             if bg_mask_blur > 0:
                 # エッジから内側に向けてぼかす
                 # radial-gradient: circle at center, black (100% - blur), transparent 100%
                 # ただしStampモード(正方形とは限らない)の場合、closest-sideなどが良い
                 mask_style = f"mask-image: radial-gradient(closest-side, black calc(100% - {bg_mask_blur}px), transparent 100%); -webkit-mask-image: radial-gradient(closest-side, black calc(100% - {bg_mask_blur}px), transparent 100%);"
             
             # オーバーレイ設定 (最前面表示)
             if bg_front_layer:
                 z_index_val = 9999
                 # [Safety] フロントレイヤー時は、操作不能になるのを防ぐため不透明度を最大0.4に制限する
                 if bg_opacity > 0.4: bg_opacity = 0.4
             else:
                 z_index_val = 0 # 背景(標準)は0にし、コンテンツを1にする戦略に変更

             # UI Opacity Logic: テーマカラーが指定されている場合はそれを透過し、なければ黒等をベースにする
             sec_color = hex_to_rgba(secondary, ui_opacity) if secondary else f"rgba(0, 0, 0, {ui_opacity})"
             block_color = hex_to_rgba(bg, ui_opacity) if bg else f"rgba(0, 0, 0, {ui_opacity})"
             # ユーザーバブル(Accent Soft)も透過させる
             # 指定がない場合はデフォルト(Generic Theme)の色に合わせるのが難しいが、白かグレーの透過が無難
             accent_soft_color = hex_to_rgba(accent_soft, ui_opacity) if accent_soft else None

             css += f"""
        /* 背景画像レイヤー */
        body::before, .gradio-container::before, gradio-app::before {{
            content: "";
            position: fixed;
            {pos_style}
            {size_style}
            background-image: url('{bg_image_url}');
            {bg_p_style if not is_stamp_mode else ''}
            
            opacity: {bg_opacity};
            filter: blur({bg_blur}px);
            z-index: {z_index_val};
            pointer-events: none;
            {radius_style}
            {mask_style}
        }}
        
        /* 背景画像が見えるようにCSS変数レベルで背景を透明化 */
        :root, body, .gradio-container, .dark, .dark .gradio-container {{
            --background-fill-primary: transparent !important;
            /* UI Opacity Control */
            --background-fill-secondary: {sec_color} !important;
            --block-background-fill: {block_color} !important;
            /* ユーザーバブルが未指定の場合も透過させる (Fallback to dark tint) */
            {f'--color-accent-soft: {accent_soft_color} !important;' if accent_soft_color else f'--color-accent-soft: rgba(0, 0, 0, {ui_opacity}) !important;'}
        }}
        /* コンテンツを背景の上に表示 (標準モード対策, z-index: 1) */
        .gradio-container {{
            position: relative;
            z-index: 1;
        }}
        
        /* コンテナ自体の背景も透明 */
        .gradio-container {{
            background-color: transparent !important;
            background: transparent !important;
        }}
        
        /* サイドバー（左カラム）のスクロール設定を明示的に保証 */
        /* NOTE: .tabs > div はGradioのタブオーバーフローメニュー（…）に干渉するため除外 */
        .gradio-container > div > div,
        .contain > div,
        [class*="column"],
        .tabitem > div {{
            overflow-y: auto !important;
            overflow-x: hidden !important;
            -webkit-overflow-scrolling: touch !important;
        }}
        /* タブのオーバーフローメニュー（…）を正常に表示するため */
        .tabs > div {{
            overflow: visible !important;
        }}

        /* チャットバブルの背景を直接透過 (CSS変数が効かない場合の対策) */
        #chat_output_area .message-bubble,
        #chat_output_area .message-row .message-bubble,
        #chat_output_area .message-wrap .message,
        #chat_output_area .message-wrap .message.bot,
        #chat_output_area .bot-row .message-bubble {{
            background-color: {sec_color} !important;
            background: {sec_color} !important;
        }}
        #chat_output_area .message-wrap .message.user,
        #chat_output_area .user-row .message-bubble {{
            background-color: {f'{accent_soft_color}' if accent_soft_color else f'rgba(0, 0, 0, {ui_opacity})'} !important;
            background: {f'{accent_soft_color}' if accent_soft_color else f'rgba(0, 0, 0, {ui_opacity})'} !important;
        }}
        /* チャット欄全体のコンテナも透過 (より包括的) */
        #chat_output_area,
        #chat_output_area > div,
        #chat_output_area > div > div,
        #chat_output_area .wrap,
        #chat_output_area .chatbot,
        .chatbot,
        .chatbot > div,
        .chatbot .wrap,
        .chatbot .wrapper,
        [data-testid="chatbot"],
        [data-testid="chatbot"] > div,
        div[class*="chatbot"],
        div[class*="chat-"] {{
            background-color: transparent !important;
            background: transparent !important;
        }}
        /* Gradio 4.x 対応: 追加のコンテナセレクタ */
        .message-row,
        .bot-row,
        .user-row,
        .messages-wrapper,
        .scroll-hide {{
            background-color: transparent !important;
            background: transparent !important;
        }}

        /* チャット入力欄（MultiModalTextbox）- 最外側のブロックのみ色を付ける */
        div.block.multimodal-textbox,
        div.block.multimodal-textbox.svelte-1svsvh2,
        div[class*="multimodal-textbox"][class*="block"] {{
            background-color: {block_color} !important;
            background: {block_color} !important;
        }}
        
        /* 内側の要素は透明にして重なりを防止 */
        #chat_input_multimodal > div,
        #chat_input_multimodal .multimodal-input,
        #chat_input_multimodal textarea,
        #chat_input_multimodal .wrap,
        #chat_input_multimodal .full-container,
        #chat_input_multimodal .input-container,
        .multimodal-textbox > div,
        .multimodal-textbox textarea,
        .multimodal-textbox .full-container,
        div.full-container.svelte-5gfv2q,
        div.input-container.svelte-5gfv2q,
        [aria-label*="ultimedia input field"],
        [aria-label*="ultimedia input field"] > div,
        .gradio-container div.full-container,
        .gradio-container div.input-container,
        .gradio-container [role="group"][aria-label*="ultimedia"],
        .gradio-container [role="group"][aria-label*="ultimedia"] > div,
        div[class*="full-container"],
        div[class*="input-container"][class*="svelte"],
        div.wrap.default.full.svelte-btia7y,
        .block.multimodal-textbox div.wrap,
        div.wrap.default.full,
        div.form.svelte-1vd8eap,
        div.form[class*="svelte"] {{
            background-color: transparent !important;
            background: transparent !important;
        }}

        /* ドロップダウンメニュー等の視認性修正 */
        .options, ul.options, .wrap.options, .dropdown-options {{
            background-color: #1f2937 !important; /* ダークグレー */
            color: #f3f4f6 !important;
            opacity: 1 !important;
            z-index: 10000 !important;
        }}
        /* 選択中のアイテム */
        li.item.selected {{
            background-color: #374151 !important;
        }}

        /* ===== Front Layer Mode: コンテンツをオーバーレイより上に表示 ===== */
        /* チャット欄の「テキストと画像だけ」をオーバーレイより上に（吹き出し背景は透過のまま） */
        #chat_output_area .prose,
        #chat_output_area .prose p,
        #chat_output_area .prose span,
        #chat_output_area .prose li,
        #chat_output_area .prose code,
        #chat_output_area .prose pre,
        #chat_output_area .message-bubble p,
        #chat_output_area .message-bubble span {{
            position: relative;
            z-index: 10001 !important;
        }}
        /* チャット欄内の画像も上に */
        #chat_output_area img {{
            position: relative;
            z-index: 10002 !important;
        }}
        /* プロフィール・情景画像も上に */
        #profile_image_display,
        #scenery_image_display {{
            position: relative;
            z-index: 10002 !important;
        }}

        /* ===== モバイル対応: 狭い画面ではz-indexを通常に戻す ===== */
        @media (max-width: 768px) {{
            #chat_output_area .prose,
            #chat_output_area .prose p,
            #chat_output_area .prose span,
            #chat_output_area .prose li,
            #chat_output_area .prose code,
            #chat_output_area .prose pre,
            #chat_output_area .message-bubble p,
            #chat_output_area .message-bubble span,
            #chat_output_area img {{
                z-index: auto !important;
            }}
        }}
        """

    return f"<style>{css}</style>"

def handle_save_theme_settings(*args, silent: bool = False, force_notify: bool = False):
    """詳細なテーマ設定を保存する (Robust Debug Version)"""
    
    try:
        # 必要な引数数: ... + 前面表示1 + 背景ソース1 + Sync設定9 + Opacity1 + radio_label1 + dropdown_list_bg1 = 43
        if len(args) < 43:
            gr.Error(f"内部エラー: 引数が不足しています ({len(args)}/43)")
            return

        room_name = args[0]
        
        # 背景画像の保存処理
        bg_image_temp_path = args[23]
        saved_image_path = None
        
        if bg_image_temp_path:
             try:
                 room_dir = os.path.join(constants.ROOMS_DIR, room_name)
                 os.makedirs(room_dir, exist_ok=True)
                 
                 _, ext = os.path.splitext(bg_image_temp_path)
                 if not ext: ext = ".png"
                 
                 target_filename = f"theme_bg{ext}"
                 destination_path = os.path.join(room_dir, target_filename)
                 
                 # 同じパスでない場合のみコピー（既存パスが渡された場合の無駄なコピー防止）
                 if os.path.abspath(bg_image_temp_path) != os.path.abspath(destination_path):
                    shutil.copy2(bg_image_temp_path, destination_path)
                 
                 saved_image_path = destination_path
             except Exception as img_err:
                 print(f"Error saving background image: {img_err}")
                 gr.Warning(f"背景画像の保存に失敗しました: {img_err}")

        settings = {
            "room_theme_enabled": args[1],  # 個別テーマのオンオフ
            "font_size": args[2],
            "line_height": args[3],
            "chat_style": args[4],
            # 基本配色
            "theme_primary": args[5],
            "theme_secondary": args[6],
            "theme_background": args[7],
            "theme_text": args[8],
            "theme_accent_soft": args[9],
            # 詳細設定
            "theme_input_bg": args[10],
            "theme_input_border": args[11],
            "theme_code_bg": args[12],
            "theme_subdued_text": args[13],
            "theme_button_bg": args[14],
            "theme_button_hover": args[15],
            "theme_stop_button_bg": args[16],
            "theme_stop_button_hover": args[17],
            "theme_checkbox_off": args[18],
            "theme_table_bg": args[19],
            "theme_radio_label": args[20],
            "theme_dropdown_list_bg": args[21],
            "theme_ui_opacity": args[22],
            # 背景画像設定
            "theme_bg_image": saved_image_path,
            "theme_bg_opacity": args[24],
            "theme_bg_blur": args[25],
            "theme_bg_size": args[26],
            "theme_bg_position": args[27],
            "theme_bg_repeat": args[28],
            "theme_bg_custom_width": args[29],
            "theme_bg_radius": args[30],
            "theme_bg_mask_blur": args[31],
            "theme_bg_front_layer": args[32],
            "theme_bg_src_mode": args[33],
            
            # Sync設定 (追加)
            "theme_bg_sync_opacity": args[34],
            "theme_bg_sync_blur": args[35],
            "theme_bg_sync_size": args[36],
            "theme_bg_sync_position": args[37],
            "theme_bg_sync_repeat": args[38],
            "theme_bg_sync_custom_width": args[39],
            "theme_bg_sync_radius": args[40],
            "theme_bg_sync_mask_blur": args[41],
            "theme_bg_sync_front_layer": args[42]
        }
        
        # Use the centralized save function in room_manager
        result = room_manager.save_room_override_settings(room_name, settings)
        if not silent:
            if result == True or (result == "no_change" and force_notify):
                mode_val = settings.get("theme_bg_src_mode")
                gr.Info(f"「{room_name}」のテーマ設定を保存しました。\n保存モード: {mode_val}")
        if result == False:
            gr.Error(f"テーマ保存に失敗しました。コンソールを確認してください。")

    except Exception as e:
        print(f"Error in handle_save_theme_settings: {e}")
        traceback.print_exc()
        gr.Error(f"保存エラー: {e}")

def handle_theme_preview(room_name, enabled, font_size, line_height, chat_style, primary, secondary, bg, text, accent_soft,
                            input_bg, input_border, code_bg, subdued_text,
                            button_bg, button_hover, stop_button_bg, stop_button_hover, 
                            checkbox_off, table_bg, radio_label, dropdown_list_bg, ui_opacity,
                            bg_image, bg_opacity, bg_blur, bg_size, bg_position, bg_repeat,
                         bg_custom_width, bg_radius, bg_mask_blur, bg_front_layer, bg_src_mode,
                         # Sync args
                         sync_opacity, sync_blur, sync_size, sync_position, sync_repeat,
                         sync_custom_width, sync_radius, sync_mask_blur, sync_front_layer):
    """UI変更時に即時CSSを返すだけのヘルパー (Syncモード対応)"""
    
    # プレビュー時でもSyncモードなら画像解決を行う
    mock_settings = { "theme_bg_src_mode": bg_src_mode, "theme_bg_image": bg_image }
    resolved_bg_image = _resolve_background_image(room_name, mock_settings)

    # モードに応じて設定値を切り替え
    is_sync = (bg_src_mode == "現在地と連動 (Sync)")
    
    use_opacity = sync_opacity if is_sync else bg_opacity
    use_blur = sync_blur if is_sync else bg_blur
    use_size = sync_size if is_sync else bg_size
    use_position = sync_position if is_sync else bg_position
    use_repeat = sync_repeat if is_sync else bg_repeat
    use_custom_width = sync_custom_width if is_sync else bg_custom_width
    use_radius = sync_radius if is_sync else bg_radius
    use_mask_blur = sync_mask_blur if is_sync else bg_mask_blur
    use_front_layer = sync_front_layer if is_sync else bg_front_layer

    return generate_room_style_css(enabled, font_size, line_height, chat_style, primary, secondary, bg, text, accent_soft,
                                   input_bg, input_border, code_bg, subdued_text,
                                   button_bg, button_hover, stop_button_bg, stop_button_hover, 
                                   checkbox_off, table_bg, radio_label, dropdown_list_bg, ui_opacity,
                                   resolved_bg_image, 
                                   use_opacity, use_blur, use_size, use_position, use_repeat,
                                   use_custom_width, use_radius, use_mask_blur, use_front_layer)

def handle_room_theme_reload(room_name: str):
    """
    パレットタブが選択されたときに、ルーム個別のテーマ設定を再読み込みしてUIに反映する。
    Gradioは非表示タブのコンポーネントを初回ロードで更新しないため、タブ選択時に明示的に再読み込みが必要。
    
    戻り値の順番:
    0. room_theme_enabled (個別テーマのオンオフ)
    1. chat_style, 2. font_size, 3. line_height,
    4-8. 基本配色5つ (primary, secondary, background, text, accent_soft)
    9-17. 詳細設定9つ (input_bg, input_border, code_bg, subdued_text,        button_bg, button_hover, stop_button_bg, stop_button_hover, 
        checkbox_off, table_bg, ui_opacity,
        resolved_bg_image, bg_opacity, bg_blur, bg_size, bg_position, bg_repeat,)
    24. style_injector
    """
    if not room_name:
        return (gr.update(),) * 43 # Updated count: 31 + 12 = 43
    
    effective_settings = config_manager.get_effective_settings(room_name)
    room_theme_enabled = effective_settings.get("room_theme_enabled", False)
    
    return (
        gr.update(value=room_theme_enabled),  # 個別テーマのオンオフ
        gr.update(value=effective_settings.get("chat_style", "Chat (Default)")),
        gr.update(value=effective_settings.get("font_size", 15)),
        gr.update(value=effective_settings.get("line_height", 1.6)),
        # 基本配色
        gr.update(value=effective_settings.get("theme_primary", None)),
        gr.update(value=effective_settings.get("theme_secondary", None)),
        gr.update(value=effective_settings.get("theme_background", None)),
        gr.update(value=effective_settings.get("theme_text", None)),
        gr.update(value=effective_settings.get("theme_accent_soft", None)),
        # 詳細設定
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
        # CSS生成
        gr.update(value=_generate_style_from_settings(room_name, effective_settings)),
    )


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
    
    # 月が指定されていない、または「最新」の場合は、本来のcurrent_monthのパスを取得
    if not selected_month or selected_month == "最新":
        # まずは標準の「現在の月」のパスを取得
        log_path, _, _, _, _, _ = get_room_files_paths(room_name)
        
        # もしそのファイルが存在しないか空の場合、logs/ 内の最新ファイルを探す
        if not log_path or not os.path.exists(log_path) or os.path.getsize(log_path) == 0:
            base_temp = os.path.join(constants.ROOMS_DIR, room_name, constants.LOGS_DIR_NAME)
            if os.path.exists(base_temp):
                files = sorted(glob.glob(os.path.join(base_temp, "*.txt")), reverse=True)
                if files:
                    log_path = files[0] # 最新のものを使う
                    print(f"[DEBUG] 最新ログが空のため、直近のログを使用します: {log_path}")

        single_file = False # 「最新」の場合は、通常のload_chat_logの挙動（全結合）を許容しても良いが、
                            # エディタに表示するのは「現在の最新ファイル」のみにするべきなので True にする。
        single_file = True
    else:
        # 指定された月のファイルを構築 (YYYY-MM.txt)
        base_path = os.path.join(constants.ROOMS_DIR, room_name)
        log_path = os.path.join(base_path, constants.LOGS_DIR_NAME, f"{selected_month}.txt")
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
        return gr.update(), gr.update(), gr.update()
    
    # 保存先パスの決定
    if not selected_month or selected_month == "最新":
        log_path, _, _, _, _, _ = get_room_files_paths(room_name)
    else:
        base_path = os.path.join(constants.ROOMS_DIR, room_name)
        log_path = os.path.join(base_path, constants.LOGS_DIR_NAME, f"{selected_month}.txt")

    if not log_path:
        gr.Error("ログファイルのパスが取得できませんでした。")
        return gr.update(), gr.update(), gr.update()
    
    try:
        # バックアップ作成（安全装置）
        room_manager.create_backup(room_name, 'log')
        
        # 末尾に改行がない場合は追加（最低1つの改行を保証）
        if raw_content and not raw_content.endswith('\n'):
            raw_content += '\n'

        # ファイル保存
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(raw_content)
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
        return []
    
    # 月が指定されていない、または「最新」の場合は、本来のcurrent_monthのパスを取得
    if not selected_month or selected_month == "最新":
        log_path, _, _, _, _, _ = get_room_files_paths(room_name)
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
            return []
    
    return []


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
                    entries.append((current_header, '\n'.join(current_content).strip()))
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
            entries.append((current_header, '\n'.join(current_content).strip()))
        
        # 直近N件を取得
        return entries[-count:] if len(entries) > count else entries
    except Exception as e:
        print(f"Error reading log file: {e}")
        import traceback
        traceback.print_exc()
        return []


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
            result_lines.append(summary if isinstance(summary, str) else str(summary))
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
    
    # API設定 - 設定された最初の有効なキー名を使用
    api_key_name = config_manager.initial_api_key_name_global
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    
    if not api_key:
        gr.Error("APIキーが設定されていません。")
        return preview_text, f"📝 推定文字数: **{len(preview_text):,}** 文字"
    
    try:
        from llm_factory import LLMFactory
        
        effective_settings = config_manager.get_effective_settings(room_name)
        llm = LLMFactory.create_chat_model(
            api_key=api_key,
            generation_config=effective_settings,
            internal_role="summarization"
        )
        
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
        result = llm.invoke(prompt)
        
        if result and result.content:
            summarized = result.content.strip()
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

def handle_outing_load_all_sections(room_name: str, episode_days: int, log_count: int, include_timestamp=True, include_model=True):
    """
    お出かけ専用タブ用：全セクションのデータを読み込む
    Returns: (system_prompt, sys_chars, permanent, perm_chars, diary, diary_chars,
              episodic, ep_chars, logs, logs_chars, total_chars)
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        empty = ""
        char_str = "文字数: 0"
        return empty, char_str, empty, char_str, empty, char_str, empty, char_str, empty, char_str, "📝 合計文字数: 0"
    
    try:
        # タプルで返される: (log_file, system_prompt_file, profile_image_path, memory_main_path, notepad_path)
        log_path, system_prompt_path, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        
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
            log_entries = _get_recent_log_entries(log_path, log_count, include_timestamp, include_model)
            logs = "\n\n".join([f"[{header}]\n{content}" for header, content in log_entries])
        
        # 文字数計算
        sys_chars = len(system_prompt)
        perm_chars = len(permanent)
        diary_chars = len(diary)
        ep_chars = len(episodic)
        logs_chars = len(logs)
        total = sys_chars + perm_chars + diary_chars + ep_chars + logs_chars
        
        gr.Info(f"データを読み込みました（合計 {total:,} 文字）")
        
        return (
            system_prompt, f"文字数: **{sys_chars:,}**",
            permanent, f"文字数: **{perm_chars:,}**",
            diary, f"文字数: **{diary_chars:,}**",
            episodic, f"文字数: **{ep_chars:,}**",
            logs, f"文字数: **{logs_chars:,}**",
            f"📝 合計文字数: **{total:,}** 文字"
        )
    
    except Exception as e:
        gr.Error(f"読み込みエラー: {e}")
        traceback.print_exc()
        empty = ""
        char_str = "文字数: エラー"
        return empty, char_str, empty, char_str, empty, char_str, empty, char_str, empty, char_str, "📝 合計文字数: エラー"


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
    
    api_key_name = config_manager.initial_api_key_name_global
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    
    if not api_key:
        gr.Error("APIキーが設定されていません。")
        return text, f"文字数: {len(text):,}"
    
    try:
        from llm_factory import LLMFactory
        
        effective_settings = config_manager.get_effective_settings(room_name)
        llm = LLMFactory.create_chat_model(
            api_key=api_key,
            generation_config=effective_settings,
            internal_role="summarization"
        )
        
        prompt = f"""以下の{section_name}を、重要な情報を保持しながら圧縮してください。

【制約事項】
- 人格の核心となる情報は必ず保持すること
- 冗長な表現は簡潔にまとめること
- **出力には「圧縮後のテキストのみ」を含めること**
- 「はい、承知しました」や「以下に要約します」といった前置きや説明、挨拶は**一切不要**です

【元データ】
{text}"""
        
        gr.Info(f"{section_name}を圧縮中...")
        result = llm.invoke(prompt)
        
        if result and result.content:
            summarized = result.content.strip()
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


def handle_outing_export_sections(
    room_name: str,
    system_prompt: str, sys_enabled: bool,
    permanent: str, perm_enabled: bool,
    diary: str, diary_enabled: bool,
    episodic: str, ep_enabled: bool,
    logs: str, logs_enabled: bool
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
            sections.append(f"## 直近の会話ログ\n\n{logs.strip()}")
        
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


def handle_outing_reload_logs(room_name: str, log_count: int, include_timestamp=True, include_model=True):
    """
    スライダー変更時に会話ログを再読み込み
    """
    if not room_name:
        return "", "文字数: 0"
    
    log_path, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    logs = ""
    if log_path and os.path.exists(log_path):
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
    
    _, system_prompt_path, _, _, _, _ = room_manager.get_room_files_paths(room_name)
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
    

def handle_import_return_log(
    file_obj, room_name, source_name, user_header, agent_header,
    api_history_limit_state, add_timestamp, display_thoughts,
    screenshot_mode, redaction_rules
):
    """
    お出かけ先からの会話ログを現在のルームにインポート（追記）する
    """
    if file_obj is None:
        return gr.update(), gr.update(), "ステータス: ⚠️ ファイルが選択されていません", gr.update()
    
    if not room_name:
        return gr.update(), gr.update(), "ステータス: ⚠️ ルームが選択されていません", gr.update()

    if not source_name:
        source_name = "外出先"

    try:
        # UTF-8で読み込みを試みる
        try:
            with open(file_obj.name, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            # 失敗した場合は cp932 (Windows-31J) を試す
            with open(file_obj.name, "r", encoding="cp932") as f:
                content = f.read()

        # 正規表現で分割
        user_h = re.escape(user_header)
        agent_h = re.escape(agent_header)
        pattern = re.compile(f"(^{user_h}|^{agent_h})", re.MULTILINE)
        
        parts = pattern.split(content)
        if len(parts) <= 1:
            return gr.update(), gr.update(), "ステータス: ⚠️ 指定されたヘッダーが見つかりませんでした", gr.update()

        log_entries = []
        for i in range(1, len(parts), 2):
            header = parts[i]
            text = parts[i+1].strip()
            if not text: continue

            if header == user_header:
                log_entries.append(f"## USER:user\n{text}")
            elif header == agent_header:
                log_entries.append(f"## AGENT:{room_name}\n{text}")

        if not log_entries:
            return gr.update(), gr.update(), "ステータス: ⚠️ インポート可能なメッセージが分割後に見つかりませんでした", gr.update()

        # システムマーカーを追加
        final_entries = []
        final_entries.append(f"## SYSTEM:外出\n\n--- {source_name} での会話開始 ---")
        final_entries.extend(log_entries)
        final_entries.append(f"## SYSTEM:外出\n\n--- {source_name} での会話終了 ---")

        # log.txt に追記
        log_path, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        
        # バックアップ作成
        room_manager.create_backup(room_name, 'log')
        
        with open(log_path, "a", encoding="utf-8") as f:
            # 既存のログの末尾に改行がなければ追加
            if os.path.exists(log_path) and os.path.getsize(log_path) > 0:
                f.write("\n\n")
            
            # 分かりやすいようにHTMLコメントで区切りを入れる
            import_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"<!-- Return Home Import: {import_timestamp} from {source_name} -->\n\n")
            
            f.write("\n\n".join(final_entries))
            f.write("\n\n")

        gr.Info(f"{len(log_entries)}件のメッセージをインポートしました。おかえりなさい！")
        
        # チャットログをリロードして最新状態にする
        chatbot_display, current_log_map_state = reload_chat_log(
            room_name, api_history_limit_state, add_timestamp,
            display_thoughts, screenshot_mode, redaction_rules
        )
        
        return chatbot_display, current_log_map_state, f"ステータス: ✅ {len(log_entries)}件インポート完了", None

    except Exception as e:
        print(f"Return Home Import Error: {e}")
        traceback.print_exc()
        return gr.update(), gr.update(), f"ステータス: ❌ エラー: {str(e)}", gr.update()



def handle_gemini_import_button_click(
    url: str,
    room_name: str,
    api_history_limit,
    add_timestamp,
    display_thoughts,
    screenshot_mode,
    redaction_rules
):
    if not url or not url.strip():
        return gr.update(), gr.update(), "ステータス: ⚠️ URLを入力してください", gr.update()
    
    if not room_name:
        return gr.update(), gr.update(), "ステータス: ⚠️ ルームが選択されていません", gr.update()
        
    print(f"Gemini URL Import: {url} for room {room_name}")
    
    try:
        success, msg, messages = gemini_importer.import_gemini_log_from_url(url.strip(), room_name)
        
        if not success:
            return gr.update(), gr.update(), f"ステータス: ❌ {msg}", gr.update()
            
        # --- Log Appending Logic ---
        final_entries = []
        final_entries.append("## SYSTEM:外出\n\n--- Gemini (お出かけ) での会話開始 ---")
        
        for m in messages:
            role = m["role"]
            content = m["content"]
            header = "## USER:user" if role == "user" else f"## AGENT:{room_name}"
            # 内容の文字列化と空白除去
            content_str = str(content).strip()
            final_entries.append(f"{header}\n\n{content_str}")
            
        final_entries.append("## SYSTEM:外出\n\n--- Gemini (お出かけ) での会話終了 ---")
        
        # ログ保存
        log_path, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        room_manager.create_backup(room_name, 'log')
        
        with open(log_path, "a", encoding="utf-8") as f:
            if os.path.exists(log_path) and os.path.getsize(log_path) > 0:
                f.write("\n\n")
            import_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"<!-- Gemini URL Import: {import_timestamp} from {url} -->\n\n")
            f.write("\n\n".join(final_entries))
            f.write("\n\n")
            
        gr.Info(f"{len(messages)}件のメッセージをインポートしました。おかえりなさい！")
        
        # 画面更新
        chatbot, mapping = reload_chat_log(room_name, api_history_limit, add_timestamp, display_thoughts, screenshot_mode, redaction_rules)
        
        return chatbot, mapping, f"ステータス: ✅ {len(messages)}件インポート完了", ""

    except Exception as e:
        print(f"Gemini Import Handler Error: {e}")
        traceback.print_exc()
        return gr.update(), gr.update(), f"ステータス: ❌ エラー: {e}", gr.update()


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


def handle_refresh_goals(room_name: str):
    """
    目標を読み込んで表示用テキストを生成する。
    
    Returns:
        (short_term_text, long_term_text, meta_text)
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return "", "", ""
    
    try:
        import goal_manager
        gm = goal_manager.GoalManager(room_name)
        goals = gm._load_goals()  # get_goals → _load_goals
        
        # 短期目標
        short_term = goals.get("short_term", [])
        short_lines = []
        for g in short_term:
            status_icon = "✅" if g.get("status") == "completed" else "🎯"
            short_lines.append(f"{status_icon} {g.get('goal', '（目標なし）')} [優先度: {g.get('priority', 1)}]")
        short_text = "\n".join(short_lines) if short_lines else "短期目標はありません"
        
        # 長期目標
        long_term = goals.get("long_term", [])
        long_lines = []
        for g in long_term:
            status_icon = "✅" if g.get("status") == "completed" else "🌟"
            long_lines.append(f"{status_icon} {g.get('goal', '（目標なし）')}")
        long_text = "\n".join(long_lines) if long_lines else "長期目標はありません"
        
        # メタデータ
        meta = goals.get("meta", {})
        level = meta.get("last_reflection_level", 1)
        level2_date = meta.get("last_level2_date", "未実施")
        level3_date = meta.get("last_level3_date", "未実施")
        meta_text = f"最終省察レベル: {level} | 週次省察: {level2_date} | 月次省察: {level3_date}"
        
        return short_text, long_text, meta_text
    
    except Exception as e:
        print(f"Refresh Goals Error: {e}")
        traceback.print_exc()
        return "エラー", "エラー", str(e)


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
    processing_provider: str,
    processing_model: str,
    summarization_provider: str,
    summarization_model: str,
    embedding_provider: str,
    embedding_model: str,
    fallback_enabled: bool = True
):
    """
    内部処理モデル設定を保存する（混合編成対応）。
    
    Args:
        processing_provider: 処理モデルのプロバイダ
        processing_model: 処理モデル名
        summarization_provider: 要約モデルのプロバイダ
        summarization_model: 要約モデル名
        embedding_provider: エンベディングプロバイダ
        embedding_model: エンベディングモデル名
        fallback_enabled: フォールバック有効/無効
        
    Returns:
        (status_markdown,) - ステータスメッセージ
    """
    try:
        settings = {
            # 処理モデル設定
            "processing_provider": processing_provider or "google",
            "processing_model": (processing_model.strip() if processing_model else constants.INTERNAL_PROCESSING_MODEL),
            # 要約モデル設定
            "summarization_provider": summarization_provider or "google",
            "summarization_model": (summarization_model.strip() if summarization_model else constants.SUMMARIZATION_MODEL),
            # supervisorは処理モデルと同じ設定を使用
            "supervisor_provider": processing_provider or "google",
            "supervisor_model": (processing_model.strip() if processing_model else constants.INTERNAL_PROCESSING_MODEL),
            # エンベディング設定
            "embedding_provider": embedding_provider or "gemini",
            "embedding_model": (embedding_model.strip() if embedding_model else "gemini-embedding-001"),
            # フォールバック設定
            "fallback_enabled": fallback_enabled,
            "fallback_order": ["google"],
        }
        
        print(f"[Phase 3] 内部モデル設定保存: {settings}")  # DEBUG
        
        success = config_manager.save_internal_model_settings(settings)
        
        print(f"[Phase 3] 保存結果: {success}")  # DEBUG
        
        if success:
            gr.Info("内部モデル設定を保存しました")
            return gr.update(value="✅ 設定を保存しました。", visible=True)
        else:
            return gr.update(value="❌ 保存に失敗しました。", visible=True)
            
    except Exception as e:
        print(f"[ui_handlers] 内部モデル設定保存エラー: {e}")
        traceback.print_exc()
        return gr.update(value=f"❌ エラー: {e}", visible=True)


def handle_reset_internal_model_settings():
    """
    内部処理モデル設定をデフォルトにリセットする（混合編成対応）。
    
    Returns:
        8個の値:
        - processing_provider, processing_model,
        - summarization_provider, summarization_model,
        - embedding_provider, embedding_model,
        - fallback_enabled, status_markdown
    """
    try:
        default_settings = config_manager.reset_internal_model_settings()
        
        return (
            "google",                           # processing_provider
            constants.INTERNAL_PROCESSING_MODEL, # processing_model
            "google",                           # summarization_provider
            constants.SUMMARIZATION_MODEL,       # summarization_model
            "gemini",                           # embedding_provider
            "gemini-embedding-001",             # embedding_model
            default_settings.get("fallback_enabled", True),
            gr.update(value="✅ デフォルト設定にリセットしました。", visible=True)
        )
        
    except Exception as e:
        print(f"[ui_handlers] 内部モデル設定リセットエラー: {e}")
        traceback.print_exc()
        return (
            gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(),
            gr.update(value=f"❌ エラー: {e}", visible=True)
        )


# --- 画像生成マルチプロバイダ設定ハンドラ ---

def handle_save_image_generation_settings(
    provider: str, 
    gemini_model: str,
    openai_profile_name: str,
    openai_model: str
):
    """
    画像生成設定を保存する。
    
    Args:
        provider: 画像生成プロバイダ ("gemini", "openai", "disabled")
        gemini_model: Gemini画像生成モデル名
        openai_profile_name: OpenAI互換プロファイル名（APIキー/Webhook管理で設定済み）
        openai_model: OpenAI互換のモデル名
    """
    try:
        # プロバイダを保存
        config_manager.save_config_if_changed("image_generation_provider", provider)
        
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
        
        provider_labels = {"gemini": "Gemini", "openai": "OpenAI互換", "disabled": "無効"}
        gr.Info(f"✅ 画像生成設定を保存しました (プロバイダ: {provider_labels.get(provider, provider)})")
        
    except Exception as e:
        print(f"[ui_handlers] 画像生成設定保存エラー: {e}")
        traceback.print_exc()
        gr.Error(f"画像生成設定の保存に失敗しました: {e}")


def handle_image_gen_provider_change(provider: str):
    """
    画像生成プロバイダが変更されたときにUIの表示を更新する。
    
    Returns:
        (gemini_section_visible, openai_section_visible)
    """
    return (
        gr.update(visible=(provider == "gemini")),
        gr.update(visible=(provider == "openai"))
    )
