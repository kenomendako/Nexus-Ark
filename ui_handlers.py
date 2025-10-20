import sys
import subprocess
import gradio as gr
import tempfile
import shutil
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
import sys
import locale
import subprocess
from pathlib import Path
import tempfile
from typing import List, Optional, Dict, Any, Tuple, Iterator
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
import gradio as gr
import datetime
from PIL import Image
import threading
import filetype
import base64
import io
import uuid
import shutil
import base64 
import io      
from tools.image_tools import generate_image as generate_image_tool_func
import pytz
import ijson
import time

from pathlib import Path
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.docstore.document import Document

import gemini_api, config_manager, alarm_manager, room_manager, utils, constants, chatgpt_importer, claude_importer, generic_importer
from utils import _overwrite_log_file
from tools import timer_tools, memory_tools
from agent.graph import generate_scenery_context
from room_manager import get_room_files_paths, get_world_settings_path
from memory_manager import load_memory_data_safe, save_memory_data


DAY_MAP_EN_TO_JA = {"mon": "月", "tue": "火", "wed": "水", "thu": "木", "fri": "金", "sat": "土", "sun": "日"}
DAY_MAP_JA_TO_EN = {v: k for k, v in DAY_MAP_EN_TO_JA.items()}


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
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    has_valid_key = api_key and not api_key.startswith("YOUR_API_KEY")

    if not has_valid_key:
        # (オンボーディングモードのコードは変更なし)
        return (
            room_name, [], [], gr.update(interactive=False, placeholder="まず、左の「設定」からAPIキーを設定してください。"),
            None, "", "", "", "",
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
            gr.update(choices=[], value=None),
            "（APIキーが設定されていません）",
            list(config_manager.SUPPORTED_VOICES.values())[0], "", True, 0.01,
            0.8, 0.95, "高リスクのみブロック", "高リスクのみブロック", "高リスクのみブロック", "高リスクのみブロック",
            False, True, True, False, True, True, True, False,
            f"ℹ️ *現在選択中のルーム「{room_name}」にのみ適用される設定です。*", None,
            True, gr.update(open=True)
        )

    # --- 【通常モード】 ---
    if not room_name:
        room_list = room_manager.get_room_list_for_ui()
        room_name = room_list[0][1] if room_list else "Default"

    # ▼▼▼【ここからが修正の核心】▼▼▼
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
    # ▲▲▲【修正はここまで】▲▲▲

    # --- 以降、取得した値を使ってUI更新値を構築する ---
    effective_settings = config_manager.get_effective_settings(room_name)
    # (以降のコードは、location_dropdownの更新値以外はほぼ変更なし)
    chat_history, mapping_list = reload_chat_log(
        room_name=room_name,
        api_history_limit_value=config_manager.initial_api_history_limit_option_global,
        add_timestamp=effective_settings.get("add_timestamp", False)
    )
    _, _, img_p, mem_p, notepad_p = get_room_files_paths(room_name)
    memory_str = ""
    if mem_p and os.path.exists(mem_p):
        with open(mem_p, "r", encoding="utf-8") as f: memory_str = f.read()
    profile_image = img_p if img_p and os.path.exists(img_p) else None
    notepad_content = load_notepad_content(room_name)
    
    # ▼▼▼【ここが修正の核心】▼▼▼
    # location_dd_val を、ファイルから読み込んだ（または初期化した）値に修正
    location_dd_val = current_location_from_file
    # ▲▲▲【修正はここまで】▲▲▲

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

    return (
        room_name, chat_history, mapping_list,
        gr.update(interactive=True, placeholder="メッセージを入力してください (Shift+Enterで送信)。添付するにはファイルをドロップまたはクリップボタンを押してください..."),
        profile_image,
        memory_str, notepad_content, load_system_prompt_content(room_name),
        core_memory_content,
        gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
        gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
        gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
        gr.update(choices=room_manager.get_room_list_for_ui(), value=room_name),
        gr.update(choices=locations_for_ui, value=location_dd_val), # choicesとvalueを同期して返す
        scenery_text,
        voice_display_name, voice_style_prompt_val,
        effective_settings["enable_typewriter_effect"],
        effective_settings["streaming_speed"],
        effective_settings.get("temperature", 0.8), effective_settings.get("top_p", 0.95),
        harassment_val, hate_val, sexual_val, dangerous_val,
        effective_settings["add_timestamp"], effective_settings.get("send_current_time", False), effective_settings["send_thoughts"],
        effective_settings["send_notepad"], effective_settings["use_common_prompt"],
        effective_settings["send_core_memory"], effective_settings["send_scenery"],
        effective_settings["auto_memory_enabled"],
        f"ℹ️ *現在選択中のルーム「{room_name}」にのみ適用される設定です。*",
        scenery_image_path,
        effective_settings.get("enable_scenery_system", True),
        gr.update(open=effective_settings.get("enable_scenery_system", True))
    )

def _update_all_tabs_for_room_change(room_name: str, api_key_name: str):
    """
    【v4】ルーム切り替え時に、全ての関連タブのUIを更新する。
    戻り値の数は `all_room_change_outputs` の51個と一致する。
    """
    # chat_tab_updatesは36個の更新値を持つ
    chat_tab_updates = _update_chat_tab_for_room_change(room_name, api_key_name)

    wb_state, wb_area_selector, wb_raw_editor, wb_place_selector = handle_world_builder_load(room_name)
    world_builder_updates = (wb_state, wb_area_selector, wb_raw_editor, wb_place_selector)

    all_rooms = room_manager.get_room_list_for_ui()
    other_rooms_for_checkbox = sorted(
        [(display, folder) for display, folder in all_rooms if folder != room_name]
    )
    participant_checkbox_update = gr.update(choices=other_rooms_for_checkbox, value=[])
    session_management_updates = ([], "現在、1対1の会話モードです。", participant_checkbox_update)

    rules = config_manager.load_redaction_rules()
    rules_df_for_ui = _create_redaction_df_from_rules(rules)

    archive_dates = _get_date_choices_from_memory(room_name)
    archive_date_dropdown_update = gr.update(choices=archive_dates, value=archive_dates[0] if archive_dates else None)

    time_settings = _load_time_settings_for_room(room_name)
    time_settings_updates = (
        gr.update(value=time_settings.get("mode", "リアル連動")),
        gr.update(value=time_settings.get("fixed_season_ja", "秋")),
        gr.update(value=time_settings.get("fixed_time_of_day_ja", "夜")),
        gr.update(visible=(time_settings.get("mode", "リアル連動") == "選択する"))
    )

    # 戻り値の総数: 36 + 3 + 3 + 1 + 1 + 4 = 48個 -> 49個
    return (
        *chat_tab_updates,
        *world_builder_updates,
        *session_management_updates,
        rules_df_for_ui,
        archive_date_dropdown_update,
        *time_settings_updates
    )


def handle_initial_load(initial_room_to_load: str, initial_api_key_name: str):
    """
    【v5: 司令塔アーキテクチャ最終版】
    UIの初期化処理。司令塔アーキテクチャに基づき、戻り値の生成パスを完全に一元化し、
    いかなる条件分岐でも戻り値の数が変わらないことを保証する。
    """
    print("--- UI初期化処理(handle_initial_load)を開始します ---")

    # --- ステップ1: 司令塔アーキテクチャへの変更 ---
    # まず、APIキーの有無を内部で判断してくれるヘルパー関数を呼び出し、
    # チャットタブ関連の更新値(37個)を確定させる。
    chat_tab_updates = _update_chat_tab_for_room_change(initial_room_to_load, initial_api_key_name)

    # --- ステップ2: チャットタブ以外の共通UIコンポーネントの更新値を生成 ---
    df_with_ids = render_alarms_as_dataframe()
    display_df, feedback_text = get_display_df(df_with_ids), "アラームを選択してください"
    rules = config_manager.load_redaction_rules()
    rules_df_for_ui = _create_redaction_df_from_rules(rules)
    api_key_choices = list(config_manager.GEMINI_API_KEYS.keys())
    api_key_dd_update = gr.update(choices=api_key_choices, value=initial_api_key_name)
    world_data_for_state = get_world_data(initial_room_to_load)
    time_settings = _load_time_settings_for_room(initial_room_to_load)
    time_settings_updates = (
        gr.update(value=time_settings.get("mode", "リアル連動")),
        gr.update(value=time_settings.get("fixed_season_ja", "秋")),
        gr.update(value=time_settings.get("fixed_time_of_day_ja", "夜")),
        gr.update(visible=(time_settings.get("mode", "リアル連動") == "選択する"))
    )

    # --- ステップ3: オンボーディング状態によってのみ変化する部分を条件分岐で決定 ---
    has_valid_key = config_manager.has_valid_api_key()
    if has_valid_key:
        token_calc_kwargs = config_manager.get_effective_settings(initial_room_to_load)
        token_count_text = gemini_api.count_input_tokens(
            room_name=initial_room_to_load, api_key_name=initial_api_key_name,
            api_history_limit=config_manager.initial_api_history_limit_option_global,
            parts=[], **token_calc_kwargs
        )
        onboarding_guide_update = gr.update(visible=False)
    else:
        token_count_text = "トークン数: (APIキー未設定)"
        onboarding_guide_update = gr.update(visible=True)

    # --- ステップ4: 全ての更新値を結合し、ただ一つのreturn文で返す ---
    # 戻り値の総数: 49個
    # (display_df, df_with_ids, feedback_text) : 3個
    # chat_tab_updates : 37個
    # (rules_df_for_ui, token_count_text, api_key_dd_update) : 3個
    # (world_data_for_state,) : 1個
    # time_settings_updates : 4個
    # (onboarding_guide_update,) : 1個
    # = 3 + 37 + 3 + 1 + 4 + 1 = 49個
    return (
        (display_df, df_with_ids, feedback_text) +
        chat_tab_updates +
        (rules_df_for_ui, token_count_text, api_key_dd_update) +
        (world_data_for_state,) +
        time_settings_updates +
        (onboarding_guide_update,)
    )

def handle_save_room_settings(
    room_name: str, voice_name: str, voice_style_prompt: str,
    temp: float, top_p: float, harassment: str, hate: str, sexual: str, dangerous: str,
    enable_typewriter_effect: bool,
    streaming_speed: float,
    add_timestamp: bool, send_current_time: bool, send_thoughts: bool, send_notepad: bool,
    use_common_prompt: bool, send_core_memory: bool,
    enable_scenery_system: bool, # room_send_scenery_checkbox から変更
    auto_memory_enabled: bool
):
    if not room_name: gr.Warning("設定を保存するルームが選択されていません。"); return

    safety_value_map = {
        "ブロックしない": "BLOCK_NONE",
        "低リスク以上をブロック": "BLOCK_LOW_AND_ABOVE",
        "中リスク以上をブロック": "BLOCK_MEDIUM_AND_ABOVE",
        "高リスクのみブロック": "BLOCK_ONLY_HIGH"
    }

    new_settings = {
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
        "add_timestamp": bool(add_timestamp),
        "send_current_time": bool(send_current_time),
        "send_thoughts": bool(send_thoughts),
        "send_notepad": bool(send_notepad),
        "use_common_prompt": bool(use_common_prompt),
        "send_core_memory": bool(send_core_memory),
        # ここで2つの設定を連動させる
        "enable_scenery_system": bool(enable_scenery_system),
        "send_scenery": bool(enable_scenery_system),
        "auto_memory_enabled": bool(auto_memory_enabled),
    }
    try:
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        config = {}
        if os.path.exists(room_config_path):
             if os.path.getsize(room_config_path) > 0:
                with open(room_config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
        else:
            gr.Warning(f"設定ファイルが見つからなかったため、新しく作成します: {room_config_path}")
            config = {
                "version": 1, "room_name": room_name, "user_display_name": "ユーザー",
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "description": "自動生成された設定ファイルです"
            }

        if "override_settings" not in config: config["override_settings"] = {}
        config["override_settings"].update(new_settings)
        config["last_updated"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(room_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        gr.Info(f"「{room_name}」の個別設定を保存しました。")
    except Exception as e: gr.Error(f"個別設定の保存中にエラーが発生しました: {e}"); traceback.print_exc()

def handle_context_settings_change(
    room_name: str, api_key_name: str, api_history_limit: str, 
    add_timestamp: bool, send_current_time: bool, send_thoughts: bool, 
    send_notepad: bool, use_common_prompt: bool, send_core_memory: bool, 
    enable_scenery_system: bool, *args, **kwargs
):
    """
    【v2: 修正版】
    個別設定のチェックボックスが変更されたときにトークン数を再計算する。
    """
    if not room_name or not api_key_name: 
        return "入力トークン数: -"
    
    return gemini_api.count_input_tokens(
        room_name=room_name, api_key_name=api_key_name, parts=[],
        api_history_limit=api_history_limit,
        add_timestamp=add_timestamp, 
        send_current_time=send_current_time, # ◀◀◀ ここが修正点
        send_thoughts=send_thoughts, 
        send_notepad=send_notepad,
        use_common_prompt=use_common_prompt, 
        send_core_memory=send_core_memory,
        send_scenery=enable_scenery_system
    )

def update_token_count_on_input(
    room_name: str,
    api_key_name: str,
    api_history_limit: str,
    multimodal_input: dict,
    add_timestamp: bool, 
    send_current_time: bool, # ◀◀◀ ここが修正点
    send_thoughts: bool, send_notepad: bool,
    use_common_prompt: bool, send_core_memory: bool, send_scenery: bool,
    *args, **kwargs
):
    """
    【v2: 修正版】
    チャット入力欄の内容が変更されたときにトークン数を再計算する。
    """
    if not room_name or not api_key_name: return "トークン数: -"
    # ... (この関数内の以降のロジックは変更なし) ...
    textbox_content = multimodal_input.get("text", "") if multimodal_input else ""
    file_list = multimodal_input.get("files", []) if multimodal_input else []
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
    effective_settings = config_manager.get_effective_settings(
        room_name,
        add_timestamp=add_timestamp, 
        send_current_time=send_current_time, # ◀◀◀ ここが修正点
        send_thoughts=send_thoughts,
        send_notepad=send_notepad, use_common_prompt=use_common_prompt,
        send_core_memory=send_core_memory, send_scenery=send_scenery
    )
    return gemini_api.count_input_tokens(
        room_name=room_name, api_key_name=api_key_name,
        api_history_limit=api_history_limit, parts=parts_for_api, **effective_settings
    )

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
    active_attachments: List[str],
    current_console_content: str,
    enable_typewriter_effect: bool,
    streaming_speed: float,
    scenery_text_from_ui: str
) -> Iterator[Tuple]:
    """
    【v15: グループ会話・逐次表示FIX】
    AIへのリクエスト送信、ストリーミング、APIリトライ、そしてグループ会話のターン管理の全責務を担う。
    一人応答するごとにログを保存・UIを再描画し、各AIの思考コンテキストの完全な独立性を保証する。
    """
    from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable, InternalServerError

    main_log_f, _, _, _, _ = get_room_files_paths(soul_vessel_room)
    all_turn_popups = []
    final_error_message = None

    try:
        # UIをストリーミングモードに移行
        # この時点の履歴を一度取得
        effective_settings_initial = config_manager.get_effective_settings(soul_vessel_room)
        add_timestamp_initial = effective_settings_initial.get("add_timestamp", False)
        chatbot_history, mapping_list = reload_chat_log(
            room_name=soul_vessel_room, api_history_limit_value=api_history_limit, add_timestamp=add_timestamp_initial
        )
        chatbot_history.append((None, "▌"))
        yield (chatbot_history, mapping_list, gr.update(value={'text': '', 'files': []}),
               *([gr.update()] * 8),
               gr.update(visible=True, interactive=True),
               gr.update(interactive=False),
               gr.update(visible=False)
        )

        # AIごとの応答生成ループ
        all_rooms_in_scene = [soul_vessel_room] + (active_participants or [])
        for i, current_room in enumerate(all_rooms_in_scene):
            
            # --- [最重要] ターンごとに思考の前提をゼロから構築 ---
            is_first_responder = (i == 0)
            
            # UIに思考中であることを表示
            chatbot_history, mapping_list = reload_chat_log(soul_vessel_room, api_history_limit, add_timestamp_initial)
            chatbot_history.append((None, f"思考中 ({current_room})... ▌"))
            yield (chatbot_history, mapping_list, *([gr.update()] * 12))

            # APIに渡す引数を、現在のAI（current_room）のために完全に再構築
            season_en, time_of_day_en = _get_current_time_context(soul_vessel_room)
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
                "time_of_day_en": time_of_day_en
            }

            streamed_text = ""
            final_state = None
            initial_message_count = 0
            max_retries = 5
            base_delay = 5
            
            for attempt in range(max_retries):
                try:
                    if debug_mode:
                        # デバッグモードがONの場合のみ、標準出力をキャプチャする
                        with utils.capture_prints() as captured_output:
                            for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args_dict):
                                if mode == "initial_count":
                                    initial_message_count = chunk
                                elif mode == "values":
                                    final_state = chunk
                        current_console_content += captured_output.getvalue()
                    else:
                        # デバッグモードがOFFの場合は、キャプチャせず直接実行する
                        for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args_dict):
                            if mode == "initial_count":
                                initial_message_count = chunk
                            elif mode == "values":
                                final_state = chunk
                    break
                except (ResourceExhausted, ServiceUnavailable, InternalServerError) as e:
                    # (リトライ処理は変更なし)
                    error_str = str(e)
                    if "PerDay" in error_str or "Daily" in error_str:
                        final_error_message = "[エラー] APIの1日あたりの利用上限に達したため、本日の応答はこれ以上生成できません。モデルかキーを変更するか、制限解除までお待ちください。"
                        break
                    wait_time = base_delay * (2 ** attempt)
                    match = re.search(r"retry_delay {\s*seconds: (\d+)\s*}", error_str)
                    if match: wait_time = int(match.group(1)) + 1
                    if attempt < max_retries - 1:
                        retry_message = (f"⏳ APIの応答が遅延しています。{wait_time}秒待機して再試行します... ({attempt + 1}/{max_retries}回目)")
                        chatbot_history[-1] = (None, retry_message)
                        yield (chatbot_history, mapping_list, *([gr.update()] * 12))
                        time.sleep(wait_time)
                        chatbot_history[-1] = (None, f"思考中 ({current_room})... ▌")
                        yield (chatbot_history, mapping_list, *([gr.update()] * 12))
                    else:
                        final_error_message = f"[エラー] APIのレート制限が頻発しています。時間をおいて再試行してください。"
                        break
                except Exception as e:
                    print(f"--- エージェント実行中に予期せぬエラーが発生しました ---")
                    traceback.print_exc()
                    final_error_message = f"[エラー] 内部処理で問題が発生しました。詳細はターミナルを確認してください。"
                    break
            
            if final_error_message: break

            if final_state:
                new_messages = final_state["messages"][initial_message_count:]

                # LangGraphが末尾に追加する空のAIMessage（メタデータのみ）を除去
                if new_messages and isinstance(new_messages[-1], AIMessage):
                    if not new_messages[-1].content or not new_messages[-1].content.strip():
                        new_messages.pop()

                last_ai_message = next((msg for msg in reversed(new_messages) if isinstance(msg, AIMessage)), None)
                has_tool_calls_in_turn = bool(last_ai_message and last_ai_message.tool_calls)

                # --- 応答の記録 ---
                # このAIが生成した全てのメッセージを、参加者全員のログに書き込む
                for msg in new_messages:
                    if isinstance(msg, (AIMessage, ToolMessage)):
                        content_to_log = ""
                        header = ""
                        if isinstance(msg, AIMessage) and msg.content and msg.content.strip():
                            content_to_log = msg.content
                            header = f"## AGENT:{current_room}"
                        elif isinstance(msg, ToolMessage):
                            content_to_log = utils.format_tool_result_for_ui(msg.name, str(msg.content)) or f"ツール「{msg.name}」を実行しました。"
                            header = f"## SYSTEM:tool_result"
                            all_turn_popups.append(content_to_log)
                        
                        if header and content_to_log:
                            for participant_room in all_rooms_in_scene:
                                log_f, _, _, _, _ = get_room_files_paths(participant_room)
                                if log_f: utils.save_message_to_log(log_f, header, content_to_log)
                
                # --- 表示処理 ---
                text_to_display = ""
                if has_tool_calls_in_turn:
                    text_to_display = last_ai_message.content if last_ai_message else ""
                else:
                    all_ai_contents = [msg.content for msg in new_messages if isinstance(msg, AIMessage) and msg.content and isinstance(msg.content, str)]
                    text_to_display = "\n\n".join(all_ai_contents)

                if text_to_display and text_to_display.strip():
                    if enable_typewriter_effect and streaming_speed > 0:
                        for char in text_to_display:
                            streamed_text += char
                            chatbot_history[-1] = (None, streamed_text + "▌")
                            yield (chatbot_history, mapping_list, *([gr.update()] * 12))
                            time.sleep(streaming_speed)
                    else:
                        streamed_text = text_to_display
                
                # ストリーミング完了後、最終的なテキストで一度更新
                chatbot_history[-1] = (None, streamed_text)
                yield (chatbot_history, mapping_list, *([gr.update()] * 12))

            # --- UIの再描画と確定 ---
            # このAIのターンが完了したので、ログから完全に再描画して表示を「確定」させる
            chatbot_history, mapping_list = reload_chat_log(soul_vessel_room, api_history_limit, add_timestamp_initial)
            yield (chatbot_history, mapping_list, *([gr.update()] * 12))

        if final_error_message:
            # エラーメッセージを、AIの応答ではなく「システムエラー」として全員のログに記録する
            error_header = "## SYSTEM:システムエラー"
            for room_name in all_rooms_in_scene:
                log_f, _, _, _, _ = get_room_files_paths(room_name)
                if log_f:
                    utils.save_message_to_log(log_f, error_header, final_error_message)
            # この時点ではUIに直接書き込まず、finallyブロックのreload_chat_logに表示を任せる

        for popup_message in all_turn_popups:
            gr.Info(popup_message)

    except GeneratorExit:
        print("--- [ジェネレータ] ユーザーの操作により、ストリーミング処理が正常に中断されました。 ---")
    
    finally:
        # (finallyブロックの中身は変更なし)
        # 処理完了・中断・エラーに関わらず、必ずログから最新の状態を再描画する
        final_chatbot_history, final_mapping_list = reload_chat_log(
            room_name=soul_vessel_room,
            api_history_limit_value=api_history_limit,
            add_timestamp=add_timestamp_initial
        )
        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
        new_scenery_text, scenery_image, token_count_text = "（更新失敗）", None, "トークン数: (更新失敗)"
        try:
            season_en, time_of_day_en = _get_current_time_context(soul_vessel_room)
            _, _, new_scenery_text = generate_scenery_context(soul_vessel_room, api_key, season_en=season_en, time_of_day_en=time_of_day_en)
            scenery_image = utils.find_scenery_image(soul_vessel_room, utils.get_current_location(soul_vessel_room), season_en=season_en, time_of_day_en=time_of_day_en)
        except Exception as e:
            print(f"--- 警告: 応答後の情景更新に失敗しました (API制限の可能性): {e} ---")
        try:
            token_calc_kwargs = config_manager.get_effective_settings(soul_vessel_room, global_model_from_ui=global_model)
            token_count_text = gemini_api.count_input_tokens(room_name=soul_vessel_room, api_key_name=api_key_name, api_history_limit=api_history_limit, parts=[], **token_calc_kwargs)
        except Exception as e:
            print(f"--- 警告: 応答後のトークン数更新に失敗しました (API制限の可能性): {e} ---")

        final_df_with_ids = render_alarms_as_dataframe()
        final_df = get_display_df(final_df_with_ids)
        new_location_choices = _get_location_choices_for_ui(soul_vessel_room)
        latest_location_id = utils.get_current_location(soul_vessel_room)
        location_dropdown_update = gr.update(choices=new_location_choices, value=latest_location_id)
        yield (final_chatbot_history, final_mapping_list, gr.update(), token_count_text,
               location_dropdown_update, new_scenery_text,
               final_df_with_ids, final_df, scenery_image,
               current_console_content, current_console_content,
               gr.update(visible=False, interactive=True), gr.update(interactive=True),
               gr.update(visible=False)
        )

def handle_message_submission(
    multimodal_input: dict, soul_vessel_room: str, api_key_name: str,
    api_history_limit: str, debug_mode: bool,
    console_content: str, active_participants: list, active_attachments: list,
    global_model: str,
    enable_typewriter_effect: bool, streaming_speed: float,
    scenery_text_from_ui: str
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
        # 戻り値の数は unified_streaming_outputs の要素数と一致させる必要がある (14個)
        # 既存のUIの状態を維持するため、全て gr.update() を返す
        yield (gr.update(),) * 14
        return
    # --- [ガードここまで] ---

    log_message_parts = []
    timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')}"

    if user_prompt_from_textbox:
        log_message_parts.append(user_prompt_from_textbox + timestamp)

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
                    continue # このファイルの処理は完了

                # --- ステップ2: ファイルのコピーとログへの記録 ---
                if temp_file_path and original_filename:
                    # ファイル名の衝突を避けるための最終的なファイル名を生成
                    unique_filename = f"{uuid.uuid4().hex}_{original_filename}"
                    permanent_path = os.path.join(attachments_dir, unique_filename)

                    shutil.copy(temp_file_path, permanent_path)
                    print(f"--- [ファイル永続化] 添付ファイルをコピーしました: {permanent_path} ---")
                    log_message_parts.append(f"[ファイル添付: {permanent_path}]")
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
        # 戻り値の数を14個に合わせる
        yield (history, mapping, *([gr.update()] * 10), gr.update(visible=False), gr.update(interactive=True))
        return

    # ▼▼▼【ここからが修正の核心】▼▼▼
    # 2. ユーザーの発言を、セッション参加者全員のログに書き込む
    all_participants_in_session = [soul_vessel_room] + (active_participants or [])
    for room_name in all_participants_in_session:
        log_f, _, _, _, _ = get_room_files_paths(room_name)
        if log_f:
            utils.save_message_to_log(log_f, "## USER:user", full_user_log_entry)
    # ▲▲▲【修正はここまで】▲▲▲

    # 3. API用の入力パーツを準備 (変更なし)
    user_prompt_parts_for_api = []
    if user_prompt_from_textbox:
        user_prompt_parts_for_api.append({"type": "text", "text": user_prompt_from_textbox})

    if file_input_list:
        for file_obj in file_input_list:
            try:
                file_path = None
                if hasattr(file_obj, 'name') and os.path.exists(file_obj.name):
                    file_path = file_obj.name
                elif isinstance(file_obj, str) and os.path.exists(file_obj):
                    file_path = file_obj
                else:
                    content = file_obj if isinstance(file_obj, str) else str(file_obj)
                    user_prompt_parts_for_api.append({"type": "text", "text": f"添付されたテキストの内容:\n---\n{content}\n---"})
                    continue

                if file_path:
                    file_basename = os.path.basename(file_path)
                    kind = filetype.guess(file_path)
                    mime_type = kind.mime if kind else "application/octet-stream"

                    if mime_type.startswith('image/'):
                        with open(file_path, "rb") as f:
                            encoded_string = base64.b64encode(f.read()).decode("utf-8")
                        user_prompt_parts_for_api.append({
                            "type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded_string}"}
                        })
                    else:
                        try:
                            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f: content = f.read()
                            user_prompt_parts_for_api.append({"type": "text", "text": f"添付ファイル「{file_basename}」の内容:\n---\n{content}\n---"})
                        except Exception as read_e:
                            user_prompt_parts_for_api.append({"type": "text", "text": f"（ファイル「{file_basename}」の読み込み中にエラーが発生しました: {read_e}）"})
            except Exception as e:
                print(f"--- ファイル処理中に致命的なエラー: {e} ---")
                traceback.print_exc()
                user_prompt_parts_for_api.append({"type": "text", "text": f"（添付ファイルの処理中に致命的なエラーが発生しました）"})

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
        active_attachments=active_attachments or [],
        current_console_content=console_content,
        enable_typewriter_effect=enable_typewriter_effect,
        streaming_speed=streaming_speed,
        scenery_text_from_ui=scenery_text_from_ui
    )

def handle_rerun_button_click(
    selected_message: Optional[Dict], room_name: str, api_key_name: str,
    api_history_limit: str, debug_mode: bool,
    console_content: str, active_participants: list, active_attachments: list, # ← active_attachments を追加
    global_model: str,
    enable_typewriter_effect: bool, streaming_speed: float,
    scenery_text_from_ui: str
):
    """
    【v3: 遅延解消版】発言の再生成を処理する司令塔。
    """
    if not selected_message or not room_name:
        gr.Warning("再生成の起点となるメッセージが選択されていません。")
        yield (gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), console_content, console_content,
               gr.update(visible=True, interactive=True), gr.update(interactive=True))
        return

    # 1. ログを巻き戻し、再送信するユーザー発言を取得
    log_f, _, _, _, _ = get_room_files_paths(room_name)
    is_ai_message = selected_message.get("role") == "AGENT"

    restored_input_text = None
    if is_ai_message:
        restored_input_text = utils.delete_and_get_previous_user_input(log_f, selected_message)
    else: # ユーザー発言の場合
        restored_input_text = utils.delete_user_message_and_after(log_f, selected_message)

    if restored_input_text is None:
        gr.Error("ログの巻き戻しに失敗しました。再生成できません。")
        effective_settings = config_manager.get_effective_settings(room_name)
        add_timestamp = effective_settings.get("add_timestamp", False)
        history, mapping = reload_chat_log(room_name, api_history_limit, add_timestamp)
        yield (history, mapping, gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), console_content, console_content,
               gr.update(visible=True, interactive=True), gr.update(interactive=True))
        return

    # 2. 巻き戻したユーザー発言に、新しいタイムスタンプを付加してログに再保存
    timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')}"
    full_user_log_entry = restored_input_text.strip() + timestamp
    utils.save_message_to_log(log_f, "## USER:user", full_user_log_entry)

    gr.Info("応答を再生成します...")
    user_prompt_parts_for_api = [{"type": "text", "text": restored_input_text}]

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
        active_attachments=active_attachments or [],
        current_console_content=console_content,
        enable_typewriter_effect=enable_typewriter_effect, # ← この行を追加
        streaming_speed=streaming_speed,                   # ← この行を追加
        scenery_text_from_ui=scenery_text_from_ui
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

        season_en, time_of_day_en = _get_current_time_context(room_name)

        _, _, scenery_text = generate_scenery_context(
            room_name, api_key, force_regenerate=force_text_regenerate,
            season_en=season_en, time_of_day_en=time_of_day_en
        )

        scenery_image_path = utils.find_scenery_image(
            room_name, current_location, season_en, time_of_day_en
        )

        if scenery_image_path is None:
            gr.Info("現在の情景に一致する画像がないため、自動で生成します...")
            pil_image = handle_generate_or_regenerate_scenery_image(
                room_name=room_name, api_key_name=api_key_name, style_choice="写真風 (デフォルト)"
            )
            if pil_image:
                scenery_image_path = utils.find_scenery_image(
                    room_name, current_location, season_en, time_of_day_en
                )

        return scenery_text, scenery_image_path

    except Exception as e:
        error_message = f"情景描写システムの処理中にエラーが発生しました。設定ファイル（world_settings.txtなど）が破損している可能性があります。"
        print(f"--- [司令塔エラー] {error_message} ---")
        traceback.print_exc()
        gr.Warning(error_message)
        return "（情景の取得中にエラーが発生しました）", None

def handle_scenery_refresh(room_name: str, api_key_name: str) -> Tuple[gr.update, str, Optional[str]]:
    """「情景テキストを更新」ボタンのハンドラ。新しい司令塔を呼び出す。"""
    gr.Info(f"「{room_name}」の現在の情景を再生成しています...")
    # 新しい司令塔を呼び出し、テキストの強制再生成フラグを立てる
    new_scenery_text, new_image_path = _get_updated_scenery_and_image(
        room_name, api_key_name, force_text_regenerate=True
    )
    latest_location_id = utils.get_current_location(room_name)
    return gr.update(value=latest_location_id), new_scenery_text, new_image_path

def handle_location_change(
    room_name: str,
    selected_value: str,
    api_key_name: str
) -> Tuple[gr.update, str, Optional[str]]:
    """【v9: 冪等性ガード版】場所が変更されたときのハンドラ。"""

    # --- [冪等性ガード] ---
    # ファイルに記録されている現在の場所と比較し、変更がなければ何もしない
    current_location_from_file = utils.get_current_location(room_name)
    if selected_value == current_location_from_file:
        return gr.update(), gr.update(), gr.update() # UIの状態を何も変更しない

    if not selected_value or selected_value.startswith("__AREA_HEADER_"):
        # ヘッダーがクリックされた場合、現在の値でUIを更新するだけ
        new_scenery_text, new_image_path = _get_updated_scenery_and_image(room_name, api_key_name)
        return gr.update(value=current_location_from_file), new_scenery_text, new_image_path

    # --- ここから下は、本当に場所が変更された場合のみ実行される ---
    location_id = selected_value
    print(f"--- UIからの場所変更処理開始: ルーム='{room_name}', 移動先ID='{location_id}' ---")

    from tools.space_tools import set_current_location
    result = set_current_location.func(location_id=location_id, room_name=room_name)
    if "Success" not in result:
        gr.Error(f"場所の変更に失敗しました: {result}")
        new_scenery_text, new_image_path = _get_updated_scenery_and_image(room_name, api_key_name)
        return gr.update(value=current_location_from_file), new_scenery_text, new_image_path

    gr.Info(f"場所を「{location_id}」に移動しました。情景を更新します...")
    new_scenery_text, new_image_path = _get_updated_scenery_and_image(room_name, api_key_name)
    return gr.update(value=location_id), new_scenery_text, new_image_path

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
        # nexus_ark.pyのoutputsは9つ
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
        gr.Info(f"新しいルーム「{new_room_name}」を作成しました。")
        updated_room_list = room_manager.get_room_list_for_ui()

        # フォームのクリア（5つのフィールド分）
        clear_form = (gr.update(value=""), gr.update(value=""), gr.update(value=""), gr.update(value=""), gr.update(value=""))

        # 全てのドロップダウンを更新し、新しいルームを選択状態にする
        main_dd = gr.update(choices=updated_room_list, value=safe_folder_name)
        manage_dd = gr.update(choices=updated_room_list, value=safe_folder_name) # 管理タブも更新
        alarm_dd = gr.update(choices=updated_room_list, value=safe_folder_name)
        timer_dd = gr.update(choices=updated_room_list, value=safe_folder_name)

        return main_dd, manage_dd, alarm_dd, timer_dd, *clear_form

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

def handle_delete_room(folder_name_to_delete: str, confirmed: bool, api_key_name: str):
    """
    【v4: 戻り値の数FIX・最終版】
    「管理」タブからルームを削除する処理。
    最後のルームが削除された場合にも対応し、常に正しい数の戻り値を返す。
    """
    # nexus_ark.pyのoutputsリストは all_room_change_outputs(54) + room_delete_confirmed_state(1) = 55個
    EXPECTED_OUTPUT_COUNT = 55

    # ユーザーが確認ダイアログで「キャンセル」を押した場合
    if str(confirmed).lower() != 'true':
        return (gr.update(),) * (EXPECTED_OUTPUT_COUNT - 1) + ("",)

    if not folder_name_to_delete:
        gr.Warning("削除するルームが選択されていません。")
        return (gr.update(),) * (EXPECTED_OUTPUT_COUNT - 1) + ("",)

    try:
        room_path_to_delete = os.path.join(constants.ROOMS_DIR, folder_name_to_delete)
        if not os.path.isdir(room_path_to_delete):
            gr.Error(f"削除対象のフォルダが見つかりません: {room_path_to_delete}")
            return (gr.update(),) * (EXPECTED_OUTPUT_COUNT - 1) + ("",)

        shutil.rmtree(room_path_to_delete)
        gr.Info(f"ルーム「{folder_name_to_delete}」を完全に削除しました。")

        new_room_list = room_manager.get_room_list_for_ui()

        if new_room_list:
            # ケース1: まだ他のルームが残っている場合
            new_main_room_folder = new_room_list[0][1]
            all_updates = handle_room_change_for_all_tabs(new_main_room_folder, api_key_name)
            # all_updates (54個) に、確認Stateをリセットするための空文字列 "" (1個) を追加して、合計55個の値を返す
            return all_updates + ("",)
        else:
            # ケース2: これが最後のルームだった場合
            gr.Warning("全てのルームが削除されました。新しいルームを作成してください。")
            
            # UIを「空の状態」にするための戻り値のタプルを作成する (合計53個)
            empty_updates = (
                # initial_load_chat_outputs (36個)
                None, [], [], gr.update(interactive=False, placeholder="ルームを作成してください。"), 
                None, "", "", "", "",
                gr.update(choices=[], value=None), gr.update(choices=[], value=None), 
                gr.update(choices=[], value=None), gr.update(choices=[], value=None),
                gr.update(choices=[], value=None), 
                "（ルームがありません）", 
                list(config_manager.SUPPORTED_VOICES.values())[0], "", True, 0.01,
                0.8, 0.95, "高リスクのみブロック", "高リスクのみブロック", "高リスクのみブロック", "高リスクのみブロック",
                False, True, True, False, True, False, False, 
                "ℹ️ *ルームを選択してください*", None,
                True, gr.update(open=True),
                # world_builder_outputs (4個)
                {}, gr.update(choices=[], value=None), "", gr.update(choices=[], value=None),
                # session_management_outputs (3個)
                [], "ルームがありません", gr.update(choices=[]),
                # redaction_rules_df (1個)
                pd.DataFrame(columns=["元の文字列 (Find)", "置換後の文字列 (Replace)", "背景色"]),
                # archive_date_dropdown (1個)
                gr.update(choices=[]),
                # time_settings_updates (4個)
                gr.update(), gr.update(), gr.update(), gr.update(visible=False),
                # attachments_df, active_attachments_display (2個)
                pd.DataFrame(columns=["ファイル名", "種類", "サイズ(KB)", "添付日時"]), "現在アクティブな添付ファイルはありません。",
                # ★★★ ここに不足していた2つの戻り値を追加 ★★★
                "トークン数: -", # token_count_display
                None             # current_room_name
            )
            # 53個の「空の状態」と、Stateリセット用の "" を結合して54個にする
            return empty_updates + ("",)

    except Exception as e:
        gr.Error(f"ルームの削除中にエラーが発生しました: {e}")
        traceback.print_exc()
        return (gr.update(),) * (EXPECTED_OUTPUT_COUNT - 1) + ("",)

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
    
    try:
        # メタデータ抽出（変更なし）
        metadata = generic_importer.parse_metadata_from_file(file_obj.name)
        
        # --- [新ロジック] ヘッダー自動検出 ---
        user_header = "## USER:"
        agent_header = "## AGENT:"
        
        try:
            with open(file_obj.name, "r", encoding="utf-8", errors='ignore') as f:
                # ファイルの先頭部分だけ読んで効率的にチェック
                content_head = f.read(4096) 
            
            # JSONファイルの場合 (例: ChatGPT Exporter)
            if file_obj.name.endswith(".json"):
                # "role": "user" や "author": {"role": "user"} のような一般的なパターンをチェック
                # ここではより具体的なChatGPT Exporterの形式を仮定
                if '"role": "Prompt"' in content_head and '"role": "Response"' in content_head:
                    user_header = "role:Prompt"
                    agent_header = "role:Response"
                elif '"from": "human"' in content_head and '"from": "gpt"' in content_head:
                    user_header = "from:human"
                    agent_header = "from:gpt"

            # テキスト/マークダウンファイルの場合
            elif file_obj.name.endswith((".md", ".txt")):
                if "## Prompt:" in content_head and "## Response:" in content_head:
                    user_header = "## Prompt:"
                    agent_header = "## Response:"
                elif "Human:" in content_head and "Assistant:" in content_head:
                    user_header = "Human:"
                    agent_header = "Assistant:"

        except Exception as e:
            print(f"Header auto-detection failed: {e}")

        return (
            gr.update(visible=True),
            metadata.get("title", os.path.basename(file_obj.name)),
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
        # --- [新ロジック] エラーコードに対応したUI通知 ---
        result = generic_importer.import_from_generic_text(
            file_path=file_obj.name,
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

def handle_claude_thread_selection(choices_list: list, evt: gr.SelectData) -> gr.update:
    """
    Claudeの会話スレッドが選択されたとき、そのタイトルをルーム名テキストボックスにコピーする。
    """
    if not evt or not choices_list or evt.value is None:
        return gr.update()
    
    selected_uuid = evt.value
    for name, uuid in choices_list:
        if uuid == selected_uuid:
            return gr.update(value=name)
    return gr.update()

def handle_claude_import_button_click(
    file_obj: Optional[Any],
    conversation_uuid: str,
    room_name: str,
    user_display_name: str
) -> Tuple[gr.update, gr.update, gr.update, gr.update, gr.update, gr.update]:
    """
    Claudeインポートボタンがクリックされたときの処理。
    """
    if not all([file_obj, conversation_uuid, room_name]):
        gr.Warning("ファイル、会話スレッド、新しいルーム名はすべて必須です。")
        return tuple(gr.update() for _ in range(6))

    try:
        safe_folder_name = claude_importer.import_from_claude_export(
            file_path=file_obj.name,
            conversation_uuid=conversation_uuid,
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
        with open(file_obj.name, 'rb') as f:
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


def handle_chatgpt_thread_selection(choices_list: list, evt: gr.SelectData) -> gr.update:
    """
    会話スレッドが選択されたとき、そのタイトルをルーム名テキストボックスにコピーする。
    """
    if not evt or not choices_list:
        return gr.update()

    selected_id = evt.value
    # choices_listの中から、IDが一致するもののタイトルを探す
    for title, convo_id in choices_list:
        if convo_id == selected_id:
            return gr.update(value=title)

    return gr.update() # 見つからなかった場合は何もしない


def handle_chatgpt_import_button_click(
    file_obj: Optional[Any],
    conversation_id: str,
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

        log_f, _, _, _, _ = get_room_files_paths(room_name)
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

def handle_delete_button_click(confirmed: str, message_to_delete: Optional[Dict[str, str]], room_name: str, api_history_limit: str):
    # ▼▼▼【ここから下のブロックを書き換え】▼▼▼
    if str(confirmed).lower() != 'true' or not message_to_delete:
        # ユーザーがキャンセルしたか、対象メッセージがない場合は選択状態を解除してボタンを非表示にする
        return gr.update(), gr.update(), None, gr.update(visible=False), "" # 最後にリセット用の "" を追加
    # ▲▲▲【書き換えここまで】▲▲▲

    log_f, _, _, _, _ = get_room_files_paths(room_name)
    if utils.delete_message_from_log(log_f, message_to_delete):
        gr.Info("ログからメッセージを削除しました。")
    else:
        gr.Error("メッセージの削除に失敗しました。詳細はターミナルを確認してください。")

    effective_settings = config_manager.get_effective_settings(room_name)
    add_timestamp = effective_settings.get("add_timestamp", False)
    history, mapping_list = reload_chat_log(room_name, api_history_limit, add_timestamp)
    return history, mapping_list, None, gr.update(visible=False), "" # 最後にリセット用の "" を追加

def format_history_for_gradio(
    messages: List[Dict[str, str]],
    current_room_folder: str,
    add_timestamp: bool,
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
            if is_user:
                speaker_name = user_display_name
            elif role == "AGENT":
                if responder_id not in agent_name_cache:
                    agent_config = room_manager.get_room_config(responder_id) or {}
                    agent_name_cache[responder_id] = agent_config.get("agent_display_name") or agent_config.get("room_name", responder_id)
                speaker_name = agent_name_cache[responder_id]
            elif role == "SYSTEM":
                speaker_name = ""
            else:
                speaker_name = responder_id

            content_to_parse = item['content']

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

            # --- [新ロジック v2: 後方互換性対応パーサー] ---
            if "THOUGHT:" in content_to_parse.upper():
                lines = content_to_parse.split('\n')
                final_html_parts = []
                thought_buffer = []

                def flush_thought_buffer():
                    if thought_buffer:
                        inner_content = "\n".join(thought_buffer)
                        final_html_parts.append(f"```\n{html.escape(inner_content)}\n```")
                        thought_buffer.clear()

                for line in lines:
                    if line.strip().upper().startswith("THOUGHT:"):
                        thought_content = line.split(":", 1)[1] if ":" in line else line
                        thought_buffer.append(thought_content.strip())
                    else:
                        flush_thought_buffer()
                        final_html_parts.append(line)
                
                flush_thought_buffer()

                if speaker_name:
                    final_html_parts.insert(0, f"**{speaker_name}:**")
                elif role == "SYSTEM":
                    final_html_parts.insert(0, f"**{responder_id}:**")

                final_markdown = "\n\n".join(final_html_parts).strip()

            else:
                thoughts_pattern = re.compile(r"(【Thoughts】[\s\S]*?【/Thoughts】)", re.IGNORECASE)
                parts = thoughts_pattern.split(content_to_parse)

                markdown_parts = []
                if speaker_name:
                    markdown_parts.append(f"**{speaker_name}:**")
                
                if role == "SYSTEM" and not speaker_name:
                    markdown_parts.append(f"**{responder_id}:**")

                for part in parts:
                    if not part or not part.strip():
                        continue

                    if thoughts_pattern.match(part):
                        inner_content_match = re.search(r"【Thoughts】([\s\S]*?)【/Thoughts】", part, re.IGNORECASE)
                        inner_content = inner_content_match.group(1).strip() if inner_content_match else ""
                        markdown_parts.append(f"```\n{inner_content}\n```")
                    else:
                        markdown_parts.append(part.strip())

                final_markdown = "\n\n".join(markdown_parts)
            # --- [新ロジックここまで] ---

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
    screenshot_mode: bool = False,
    redaction_rules: List[Dict] = None,
    *args, **kwargs
):
    if not room_name:
        return [], []

    log_f,_,_,_,_ = get_room_files_paths(room_name)
    if not log_f or not os.path.exists(log_f):
        return [], []

    full_raw_history = utils.load_chat_log(log_f)

    # --- ▼▼▼ ここからが修正の核心 ▼▼▼ ---
    display_turns = _get_display_history_count(api_history_limit_value)

    # 1. ログ全体から、表示すべき部分の開始インデックス（絶対座標）を計算
    absolute_start_index = max(0, len(full_raw_history) - (display_turns * 2))

    # 2. そのインデックスを使って、表示用の履歴をスライス
    visible_history = full_raw_history[absolute_start_index:]

    # 3. format_history_for_gradioに、計算した絶対座標の開始位置を渡す
    history, mapping_list = format_history_for_gradio(
        messages=visible_history,
        current_room_folder=room_name,
        add_timestamp=add_timestamp,
        screenshot_mode=screenshot_mode,
        redaction_rules=redaction_rules,
        absolute_start_index=absolute_start_index # ★★★ 新しい引数を追加 ★★★
    )
    # --- ▲▲▲ 修正ここまで ▲▲▲ ---

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

    _, _, _, memory_txt_path, _ = get_room_files_paths(room_name)
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
    _, _, _, memory_txt_path, _ = get_room_files_paths(room_name)
    if memory_txt_path and os.path.exists(memory_txt_path):
        with open(memory_txt_path, "r", encoding="utf-8") as f:
            memory_content = f.read()

    # 日付選択肢も同時に更新する
    new_dates = _get_date_choices_from_memory(room_name)
    date_dropdown_update = gr.update(choices=new_dates, value=new_dates[0] if new_dates else None)

    return memory_content, date_dropdown_update

def _get_date_choices_from_memory(room_name: str) -> List[str]:
    """memory_main.txtの日記セクションから日付見出しを抽出する。"""
    if not room_name:
        return []
    try:
        _, _, _, memory_main_path, _ = get_room_files_paths(room_name)
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
    _, _, _, memory_txt_path, _ = get_room_files_paths(room_name)
    if memory_txt_path and os.path.exists(memory_txt_path):
        with open(memory_txt_path, "r", encoding="utf-8") as f:
            new_memory_content = f.read()

    new_dates = _get_date_choices_from_memory(room_name)
    date_dropdown_update = gr.update(choices=new_dates, value=new_dates[0] if new_dates else None)

    return new_memory_content, date_dropdown_update

def load_notepad_content(room_name: str) -> str:
    if not room_name: return ""
    _, _, _, _, notepad_path = get_room_files_paths(room_name)
    if notepad_path and os.path.exists(notepad_path):
        with open(notepad_path, "r", encoding="utf-8") as f: return f.read()
    return ""

def handle_save_notepad_click(room_name: str, content: str) -> str:
    if not room_name: gr.Warning("ルームが選択されていません。"); return content

    # ▼▼▼【ここに追加】▼▼▼
    room_manager.create_backup(room_name, 'notepad')

    _, _, _, _, notepad_path = room_manager.get_room_files_paths(room_name)
    if not notepad_path: gr.Error(f"「{room_name}」のメモ帳パス取得失敗。"); return content
    lines = [f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}] {line.strip()}" if line.strip() and not re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]", line.strip()) else line.strip() for line in content.strip().split('\n') if line.strip()]
    final_content = "\n".join(lines)
    try:
        with open(notepad_path, "w", encoding="utf-8") as f: f.write(final_content + ('\n' if final_content else ''))
        gr.Info(f"「{room_name}」のメモ帳を保存しました。"); return final_content
    except Exception as e: gr.Error(f"メモ帳の保存エラー: {e}"); return content

def handle_clear_notepad_click(room_name: str) -> str:
    if not room_name: gr.Warning("ルームが選択されていません。"); return ""
    _, _, _, _, notepad_path = room_manager.get_room_files_paths(room_name)
    if not notepad_path: gr.Error(f"「{room_name}」のメモ帳パス取得失敗。"); return ""
    try:
        with open(notepad_path, "w", encoding="utf-8") as f: f.write("")
        gr.Info(f"「{room_name}」のメモ帳を空にしました。"); return ""
    except Exception as e: gr.Error(f"メモ帳クリアエラー: {e}"); return f"エラー: {e}"

def handle_reload_notepad(room_name: str) -> str:
    if not room_name: gr.Warning("ルームが選択されていません。"); return ""
    content = load_notepad_content(room_name); gr.Info(f"「{room_name}」のメモ帳を再読み込みしました。"); return content

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
    if not room or not api_key_name:
        return "エラー：ルームとAPIキーを選択してください。"

    try:
        if timer_type == "通常タイマー":
            result_message = timer_tools.set_timer.func(
                duration_minutes=int(duration),
                theme=normal_theme or "時間になりました！",
                room_name=room
            )
            gr.Info(f"通常タイマーを設定しました。")
        elif timer_type == "ポモドーロタイマー":
            result_message = timer_tools.set_pomodoro_timer.func(
                work_minutes=int(work),
                break_minutes=int(brk),
                cycles=int(cycles),
                work_theme=work_theme or "作業終了の時間です。",
                break_theme=brk_theme or "休憩終了の時間です。",
                room_name=room
            )
            gr.Info(f"ポモドーロタイマーを設定しました。")
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
        log_file_path, _, _, _, _ = room_manager.get_room_files_paths(room_name)
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
    config_manager.save_config("last_model", model)
    gr.Info(f"デフォルトAIモデルを「{model}」に設定しました。")
    return model

def update_api_key_state(api_key_name):
    config_manager.save_config("last_api_key_name", api_key_name)
    gr.Info(f"APIキーを '{api_key_name}' に設定しました。")
    return api_key_name

def update_api_history_limit_state_and_reload_chat(limit_ui_val: str, room_name: Optional[str], add_timestamp: bool, screenshot_mode: bool = False, redaction_rules: List[Dict] = None):
    key = next((k for k, v in constants.API_HISTORY_LIMIT_OPTIONS.items() if v == limit_ui_val), "all")
    config_manager.save_config("last_api_history_limit_option", key)
    history, mapping_list = reload_chat_log(room_name, key, add_timestamp, screenshot_mode, redaction_rules)
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

def handle_generate_or_regenerate_scenery_image(room_name: str, api_key_name: str, style_choice: str) -> Optional[Image.Image]:
    """
    【v5: 最終FIX版】
    現在の時間と季節に一致するファイル名を事前に確定し、そのファイル名で画像を生成・上書き保存する。
    他の季節や時間帯の画像には一切触れず、UIの表示更新を保証する。
    """
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        return None

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key:
        gr.Warning(f"APIキー '{api_key_name}' が見つかりません。")
        return gr.update()

    location_id = utils.get_current_location(room_name)
    if not location_id:
        gr.Warning("現在地が特定できません。")
        return None

    # --- [ここからが修正の核心] ---
    # 1. 適用すべき季節と時間帯を取得
    season_en, time_of_day_en = _get_current_time_context(room_name)

    # 2. 取得した値を使ってファイル名を確定
    save_dir = os.path.join(constants.ROOMS_DIR, room_name, "spaces", "images")
    os.makedirs(save_dir, exist_ok=True)
    final_filename = f"{location_id}_{season_en}_{time_of_day_en}.png"
    final_path = os.path.join(save_dir, final_filename)
    # --- [修正はここまで] ---

    # フォールバック用に、現在の画像パスを先に探しておく
    fallback_image_path = utils.find_scenery_image(room_name, location_id)

    # プロンプト生成
    final_prompt = ""
    gr.Info("シーンディレクターAIがプロンプトを構成しています...")
    try:
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
            gr.Error("世界設定の読み込みに失敗しました。")
            if fallback_image_path: return Image.open(fallback_image_path)
            return None

        space_text = None
        for area, places in world_settings.items():
            if location_id in places:
                space_text = places[location_id]
                break

        if not space_text:
            gr.Error("現在の場所の定義が見つかりません。")
            if fallback_image_path: return Image.open(fallback_image_path)
            return None

        from gemini_api import get_configured_llm
        effective_settings = config_manager.get_effective_settings(room_name)
        scene_director_llm = get_configured_llm(constants.INTERNAL_PROCESSING_MODEL, api_key, effective_settings)

        director_prompt = f"""
You are a master scene director AI for a high-end image generation model.
Your sole purpose is to synthesize information from two distinct sources into a single, cohesive, and flawless English prompt.

**--- [Source 1: Architectural Blueprint] ---**
This is the undeniable truth for all physical structures, objects, furniture, and materials.
```
{space_text}
```

**--- [Current Scene Conditions] ---**
        # Use ONLY if time/lighting is NOT specified in the description above.
        - Time of Day: {time_of_day_en}
        - Season: {season_en}

**--- [Your Task: The Fusion] ---**
Your task is to **merge** these two sources into a single, coherent visual description, following the absolute rules below.

**--- [The Golden Rule for Windows & Exteriors] ---**
**If the Architectural Blueprint mentions a window, door, or any view to the outside, you MUST explicitly describe the exterior view *as it would appear* within the Temporal Context.**
-   **Example:** If the context is `night` and the blueprint mentions "a garden," you MUST describe a `dark garden under the moonlight` or `a rainy night landscape`, not just `a garden`.
-   **This rule is absolute and overrides any ambiguity.**

**--- [Core Principles & Hierarchy] ---**

1.  **Architectural Fidelity:** Your prompt MUST be a faithful visual representation of the physical elements described in the "Architectural Blueprint" (Source 1).

2.  **Atmospheric & Lighting Fidelity:** The overall lighting, weather, and the view seen through windows MUST be a direct and faithful representation of the "Temporal Context" (Source 2), unless the blueprint describes an absolute, unchangeable environmental property (e.g., "a cave with no natural light," "a dimension of perpetual twilight").

3.  **Strictly Visual:** The output must be a purely visual paragraph in English. Exclude any narrative, metaphors, sounds, or non-visual elements.

4.  **Mandatory Inclusions:** Your prompt MUST incorporate the specified "Aspect Ratio" and adhere to the "Style Definition".

5.  **Absolute Prohibitions:** Strictly enforce all "Negative Prompts".

6.  **Output Format:** Output ONLY the final, single-paragraph prompt. Do not include any of your own thoughts or conversational text.

---
**[Supporting Information]**

**Style Definition (Incorporate this aesthetic):**
- {style_choice_text}

**Mandatory Technical Specs:**
- Aspect Ratio: 16:9 landscape aspect ratio.

**Negative Prompts (Strictly enforce these exclusions):**
- Absolutely no text, letters, characters, signatures, or watermarks. Do not include people.
---

**Final Master Prompt:**
"""
        final_prompt = scene_director_llm.invoke(director_prompt).content.strip()

    except Exception as e:
        gr.Error(f"シーンディレクターAIによるプロンプト生成中にエラーが発生しました: {e}")
        traceback.print_exc()
        if fallback_image_path: return Image.open(fallback_image_path)
        return None

    if not final_prompt:
        gr.Error("シーンディレクターAIが有効なプロンプトを生成できませんでした。")
        if fallback_image_path: return Image.open(fallback_image_path)
        return None

    gr.Info(f"「{style_choice}」で画像を生成します...")
    result = generate_image_tool_func.func(prompt=final_prompt, room_name=room_name, api_key=api_key)

    # 確定パスで上書き保存し、そのパスを返す
    if "Generated Image:" in result:
        # [修正] 正規表現を使って、改行を含む文字列からでも正確にパスを抽出する
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

def handle_room_change_for_all_tabs(room_name: str, api_key_name: str):
    """
    【v5: 堅牢化】
    ルーム変更時に、全てのUI更新と内部状態の更新を、この単一の関数で完結させる。
    """
    print(f"--- UI司令塔(handle_room_change_for_all_tabs)実行: {room_name} ---")

    # 責務1: 新しいヘルパーを呼び出してUI更新値のタプルを取得する
    all_ui_updates = _update_all_tabs_for_room_change(room_name, api_key_name)

    # 責務2: トークン数を計算する
    add_timestamp_val = all_ui_updates[24]
    send_thoughts_val = all_ui_updates[25]
    send_notepad_val = all_ui_updates[26]
    use_common_prompt_val = all_ui_updates[27]
    send_core_memory_val = all_ui_updates[28]
    send_scenery_val = all_ui_updates[29]
    api_history_limit_key = config_manager.CONFIG_GLOBAL.get("last_api_history_limit_option", "all")

    token_count_text = gemini_api.count_input_tokens(
        room_name=room_name, api_key_name=api_key_name, parts=[],
        api_history_limit=api_history_limit_key,
        add_timestamp=add_timestamp_val, send_thoughts=send_thoughts_val,
        send_notepad=send_notepad_val, use_common_prompt=use_common_prompt_val,
        send_core_memory=send_core_memory_val, send_scenery=send_scenery_val
    )

    ui_attachments_df = _get_attachments_df(room_name)
    initial_active_attachments_display = "現在アクティブな添付ファイルはありません。"

    # 責務3: 全てのUI更新値と、トークン数の計算結果、そして新しいルーム名をStateに返す
    return (
        *all_ui_updates,
        ui_attachments_df,
        initial_active_attachments_display, # ← この行を追加
        token_count_text,
        room_name
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
        log_f, _, _, _, _ = get_room_files_paths(room_name)
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
        log_f, _, _, _, _ = get_room_files_paths(room_name)
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
        return world_data, gr.update()

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
        return world_data, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
    if area_name not in world_data or place_name not in world_data[area_name]:
        gr.Warning(f"場所 '{place_name}' がエリア '{area_name}' に見つかりません。")
        return world_data, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

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
        raw_content
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

def handle_save_gemini_key(key_name: str, key_value: str, current_room_name: str):
    """
    【v8: 戻り値の数FIX版】
    APIキーを保存し、オンボーディングモードを解除し、UI全体を完全にリフレッシュする。
    """
    # nexus_ark.pyで定義されているoutputsの総数 = 3 + 51 = 54
    EXPECTED_OUTPUT_COUNT = 54
    # all_room_change_outputsの要素数 = 51
    ALL_ROOM_CHANGE_OUTPUTS_COUNT = 51

    if not key_name or not key_value or not re.match(r"^[a-zA-Z0-9_]+$", key_name.strip()):
        if key_name and not re.match(r"^[a-zA-Z0-9_]+$", key_name.strip()):
            gr.Warning("「キーの名前」は半角の英数字とアンダースコア(_)のみ使用できます。")
        else:
            gr.Warning("キーの名前と値の両方を入力してください。")
        return (gr.update(),) * EXPECTED_OUTPUT_COUNT

    key_name = key_name.strip()
    key_value = key_value.strip()

    was_onboarding = not config_manager.has_valid_api_key()

    config_manager.add_or_update_gemini_key(key_name, key_value)
    gr.Info(f"Gemini APIキー「{key_name}」を保存しました。")
    new_keys = list(config_manager.GEMINI_API_KEYS.keys())
    api_key_dd_update = gr.update(choices=new_keys, value=key_name)

    if was_onboarding and config_manager.has_valid_api_key():
        gr.Info("APIキーが設定されました。アプリケーションを初期化します...")
        
        config_manager.initial_api_key_name_global = key_name
        
        # handle_room_change_for_all_tabs は51個の値を返す
        all_updates_tuple = handle_room_change_for_all_tabs(current_room_name, key_name)
        
        # 先頭の3つの更新値と、司令塔からの51個の値を結合して、合計54個の値を返す
        return (
            api_key_dd_update, 
            gr.update(visible=False), 
            gr.update(interactive=True, placeholder="メッセージを入力し、ファイルをドラッグ＆ドロップまたは添付してください...")
        ) + all_updates_tuple
    else:
        # 通常時は、期待される数だけ空のgr.update()を返す
        return (api_key_dd_update, gr.update(), gr.update()) + (gr.update(),) * ALL_ROOM_CHANGE_OUTPUTS_COUNT

def handle_delete_gemini_key(key_name):
    if not key_name:
        gr.Warning("削除するキーの名前を入力してください。")
        return gr.update()
    config_manager.delete_gemini_key(key_name)
    gr.Info(f"Gemini APIキー「{key_name}」を削除しました。")
    new_keys = list(config_manager.GEMINI_API_KEYS.keys())
    # 正しい作法： choicesを更新し、valueはリストの先頭かNoneに設定する
    return gr.update(choices=new_keys, value=new_keys[0] if new_keys else None)

def handle_save_pushover_config(user_key, app_token):
    config_manager.update_pushover_config(user_key, app_token)
    gr.Info("Pushover設定を保存しました。")

def handle_notification_service_change(service_choice: str):
    if service_choice in ["Discord", "Pushover"]:
        config_manager.save_config("notification_service", service_choice.lower())
        gr.Info(f"通知サービスを「{service_choice}」に設定しました。")

def handle_save_discord_webhook(webhook_url: str):
    config_manager.save_config("notification_webhook_url", webhook_url)
    gr.Info("Discord Webhook URLを保存しました。")

def load_system_prompt_content(room_name: str) -> str:
    if not room_name: return ""
    _, system_prompt_path, _, _, _ = get_room_files_paths(room_name)
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

    _, system_prompt_path, _, _, _ = get_room_files_paths(room_name)
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


def handle_stop_button_click(room_name, api_history_limit, add_timestamp, screenshot_mode, redaction_rules):
    """
    ストップボタンが押されたときにUIの状態を即座にリセットし、ログから最新の状態を再描画する。
    """
    print("--- [UI] ユーザーによりストップボタンが押されました ---")
    # ログファイルから最新の履歴を再読み込みして、"思考中..." のような表示を消去する
    history, mapping_list = reload_chat_log(room_name, api_history_limit, add_timestamp, screenshot_mode, redaction_rules)
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
    if not str(confirmed).lower() == 'true':
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

        log_f, _, _, _, _ = get_room_files_paths(room_name)
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
            thoughts_pattern = re.compile(r"(【Thoughts】[\s\S]*?【/Thoughts】)", re.IGNORECASE)
            timestamp_pattern = re.compile(r'(\n\n\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}$)')

            thoughts_match = thoughts_pattern.search(original_content)
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
                inner_thoughts = re.sub(r"【/?Thoughts】", "", thoughts_part, flags=re.IGNORECASE).strip()
                text_to_fix = inner_thoughts.replace("、", "").replace("､", "")
                result = gemini_api.correct_punctuation_with_ai(text_to_fix, api_key, context_type="thoughts")
                # 安全装置：AIが失敗したら元のテキストを使う
                corrected_thoughts = f"【Thoughts】\n{result.strip()}\n【/Thoughts】" if result and len(result) > len(inner_thoughts) * 0.5 else thoughts_part

            corrected_body = ""
            if body_part:
                text_to_fix = body_part.replace("、", "").replace("､", "")
                result = gemini_api.correct_punctuation_with_ai(text_to_fix, api_key, context_type="body")
                # 安全装置：AIが失敗したら元のテキストを使う
                corrected_body = result if result and len(result) > len(body_part) * 0.5 else body_part

            # 3. 【再結合】パーツを結合してメッセージを更新
            final_parts = [part for part in [corrected_thoughts, corrected_body, timestamp_part] if part]
            all_messages[original_index]["content"] = "\n\n".join(final_parts).strip()
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

def handle_staging_image_upload(uploaded_file_path: Optional[str]) -> Tuple[Optional[str], gr.update, gr.update, gr.update]:
    """
    ユーザーが新しい画像をアップロードした際に、編集用プレビューエリアにその画像を表示し、
    UIを編集モードに切り替える。
    GradioのUploadButtonは、一時ファイルのパス(文字列)を直接渡してくる。
    """
    if uploaded_file_path is None:
        return None, gr.update(visible=False), gr.update(visible=False), gr.update()

    # uploaded_file_path は既にファイルパスの文字列なので、そのまま使用する
    return (
        uploaded_file_path,
        gr.update(value=uploaded_file_path, visible=True),
        gr.update(visible=True),
        gr.update(open=True) # アコーディオンを開く
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
        # したがって、NumPy配列からの変換は不要です。
        cropped_img = cropped_image_data["composite"]

        save_path = os.path.join(constants.ROOMS_DIR, room_name, constants.PROFILE_IMAGE_FILENAME)

        cropped_img.save(save_path, "PNG")

        gr.Info(f"ルーム「{room_name}」のプロフィール画像を更新しました。")

        # 最終的なプロフィール画像表示を更新し、編集用UIを非表示に戻す
        return (
            gr.update(value=save_path),
            gr.update(value=None, visible=False),
            gr.update(visible=False)
        )

    except Exception as e:
        gr.Error(f"トリミング画像の保存中にエラーが発生しました: {e}")
        traceback.print_exc()
        # エラーが発生した場合、元のプロフィール画像表示は変更せず、編集UIのみを閉じる
        _, _, current_image_path, _, _ = get_room_files_paths(room_name)
        fallback_path = current_image_path if current_image_path and os.path.exists(current_image_path) else None
        return gr.update(value=fallback_path), gr.update(visible=False), gr.update(visible=False)

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

        log_f, _, _, _, _ = get_room_files_paths(room_name)
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

        thoughts_pattern = re.compile(r"```\n([\s\S]*?)\n```")
        thoughts_match = thoughts_pattern.search(edited_markdown_string)
        new_thoughts_block = ""
        if thoughts_match:
            inner_thoughts = thoughts_match.group(1).strip()
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

    config_manager.save_config("backup_rotation_count", int(count))
    gr.Info(f"バックアップの最大保存件数を {int(count)} 件に設定しました。")

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

def _get_current_time_context(room_name: str) -> Tuple[str, str]:
    """
    ルームの時間設定を読み込み、現在適用すべき季節と時間帯の「英語名」を返す。
    戻り値: (season_en, time_of_day_en)
    """
    room_config = room_manager.get_room_config(room_name)
    settings = (room_config or {}).get("time_settings", {})
    
    mode = settings.get("mode", "realtime")

    if mode == "fixed":
        # 固定モードの場合は、設定ファイルから値を返す
        season_en = settings.get("fixed_season", "autumn")
        time_en = settings.get("fixed_time_of_day", "night")
        return season_en, time_en
    else:
        # リアル連動モードの場合は、現在時刻から計算して返す
        now = datetime.datetime.now()
        season_en = utils.get_season(now.month)
        time_en = utils.get_time_of_day(now.hour)
        return season_en, time_en

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
def handle_open_backup_folder(room_name: str):
    """現在のルームのバックアップ用フォルダ（memory_backupsなど）を開く。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return

    # メモリバックアップフォルダのパスを組み立てる（room_name/memory_backups）
    backup_folder_path = os.path.join(constants.ROOMS_DIR, room_name, "memory_backups")
    # フォルダがなければ作成する
    os.makedirs(backup_folder_path, exist_ok=True)

    try:
        if sys.platform == "win32":
            os.startfile(os.path.normpath(backup_folder_path))
        elif sys.platform == "darwin":  # macOS
            subprocess.Popen(["open", backup_folder_path])
        else:  # Linux
            subprocess.Popen(["xdg-open", backup_folder_path])
    except Exception as e:
        gr.Error(f"フォルダを開けませんでした: {e}")

# ▼▼▼【ここから下のブロックをファイルの末尾にまるごと追加】▼▼▼

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
    index_path = Path(constants.ROOMS_DIR) / room_name / "rag_data" / "faiss_index"
    if index_path.exists() and any(index_path.iterdir()):
        return "✅ 索引は作成済みです。ドキュメントを変更した場合は、索引の更新を推奨します。"
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
    """知識ベースの索引を作成/更新する。時間がかかる可能性がある処理。"""
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        return gr.update(), gr.update()

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        return gr.update(), gr.update()

    yield "処理中: 知識ベースのドキュメントを読み込んでいます...", gr.update(interactive=False)

    try:
        knowledge_dir = Path(constants.ROOMS_DIR) / room_name / "knowledge"

        # 1. ドキュメントの読み込み
        documents = []
        files_to_process = list(knowledge_dir.glob("*.txt")) + list(knowledge_dir.glob("*.md"))
        if not files_to_process:
            gr.Info("索引を作成するドキュメントがありません。")
            # 索引ディレクトリが存在する場合はクリアする
            index_path = Path(constants.ROOMS_DIR) / room_name / "rag_data" / "faiss_index"
            if index_path.exists():
                shutil.rmtree(str(index_path))
            yield _get_knowledge_status(room_name), gr.update(interactive=True)
            return

        for file_path in files_to_process:
            content = file_path.read_text(encoding="utf-8")
            documents.append(Document(page_content=content, metadata={"source": str(file_path)}))

        yield f"処理中: {len(documents)}個のドキュメントをチャンクに分割しています...", gr.update()

        # 2. チャンク分割
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        splits = text_splitter.split_documents(documents)

        yield f"処理中: {len(splits)}個のチャンクをベクトル化しています (この処理には時間がかかります)...", gr.update()

        # 3. エンベディングとFAISSインデックス作成
        embeddings = GoogleGenerativeAIEmbeddings(
            model=constants.EMBEDDING_MODEL,
            google_api_key=api_key,
            task_type="retrieval_document" # ドキュメント埋め込み用のタスクタイプを指定
        )

        # FAISSは一度に100件までのエンベディングを推奨
        batch_size = 100
        db = None
        for i in range(0, len(splits), batch_size):
            batch_splits = splits[i:i+batch_size]
            yield f"処理中: チャンク {i+1}〜{i+len(batch_splits)} / {len(splits)} をベクトル化中...", gr.update()
            if db is None:
                db = FAISS.from_documents(batch_splits, embeddings)
            else:
                db.add_documents(batch_splits)

        yield "処理中: 作成した索引を保存しています...", gr.update()

        # 4. 索引の保存 (日本語パス対応版)
        final_index_path = Path(constants.ROOMS_DIR) / room_name / "rag_data" / "faiss_index"

        # tempfileを使って、ASCII文字のみの一時ディレクトリを安全に作成
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_index_path = Path(temp_dir)
            
            # FAISSには、この安全な一時ディレクトリに書き込ませる
            db.save_local(str(temp_index_path))
            
            # 書き込みが完了したら、古い索引ディレクトリを削除
            if final_index_path.exists():
                shutil.rmtree(str(final_index_path))
            
            # 完成した索引を、一時ディレクトリから本来の場所へ移動させる
            # shutil.moveは、Pythonレベルで動作するため、日本語パスを正しく扱える
            shutil.move(str(temp_index_path), str(final_index_path))

        gr.Info("✅ 知識ベースの索引作成が完了しました。")

    except Exception as e:
        error_msg = f"索引の作成中にエラーが発生しました: {e}"
        gr.Error(error_msg)
        print(f"--- [索引作成エラー] ---")
        traceback.print_exc()
        yield error_msg, gr.update(interactive=True)
        return

    yield _get_knowledge_status(room_name), gr.update(interactive=True)

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
    return gemini_api.count_input_tokens(
        room_name=room_name, api_key_name=api_key_name,
        api_history_limit=api_history_limit, parts=parts_for_api, **effective_settings
    )

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