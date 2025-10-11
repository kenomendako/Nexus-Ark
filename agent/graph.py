# agent/graph.py (v21: Smart Retry)

import os
import copy
import re
import traceback
import json
import time
from datetime import datetime
from typing import TypedDict, Annotated, List, Literal, Tuple, Optional

from langchain_core.messages import SystemMessage, BaseMessage, ToolMessage, AIMessage, HumanMessage
from google.api_core import exceptions as google_exceptions
from langgraph.graph import StateGraph, END, START, add_messages
import time
import re
from google.api_core import exceptions as google_exceptions
from langchain_core.messages import AIMessage

from agent.prompts import CORE_PROMPT_TEMPLATE
from tools.space_tools import set_current_location, read_world_settings, plan_world_edit, _apply_world_edits
from tools.memory_tools import (
    search_memory,
    search_past_conversations,
    read_main_memory, plan_main_memory_edit, _apply_main_memory_edits,
    read_secret_diary, plan_secret_diary_edit, _apply_secret_diary_edits
)
from tools.notepad_tools import read_full_notepad, plan_notepad_edit, _write_notepad_file
from tools.web_tools import web_search_tool, read_url_tool
from tools.image_tools import generate_image
from tools.alarm_tools import set_personal_alarm
from tools.timer_tools import set_timer, set_pomodoro_timer
from tools.knowledge_tools import search_knowledge_graph
from room_manager import get_world_settings_path, get_room_files_paths
import utils
import config_manager
import constants
import pytz

all_tools = [
    set_current_location, read_world_settings, plan_world_edit,
    search_memory,
    search_past_conversations,
    read_main_memory, plan_main_memory_edit, read_secret_diary, plan_secret_diary_edit,
    read_full_notepad, plan_notepad_edit,
    web_search_tool, read_url_tool,
    generate_image,
    set_personal_alarm,
    set_timer, set_pomodoro_timer,
    search_knowledge_graph
]

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    room_name: str
    api_key: str
    model_name: str
    system_prompt: SystemMessage
    generation_config: dict
    send_core_memory: bool
    send_scenery: bool
    send_notepad: bool
    location_name: str
    scenery_text: str
    debug_mode: bool
    all_participants: List[str]
    loop_count: int # ← この行を追加
    season_en: str
    time_of_day_en: str
    last_successful_response: Optional[AIMessage] # 最後の成功したAI応答を保持
    force_end: bool # グラフの実行を強制的に終了させるためのフラグ

def get_location_list(room_name: str) -> List[str]:
    """
    UIとAIのプロンプトで表示するための、移動可能な場所名のリストを生成する。
    異なるエリアに同じ名前の場所が存在する可能性を考慮し、
    重複を許さずに全てのユニークな場所名を返す。
    """
    if not room_name: return []
    world_settings_path = get_world_settings_path(room_name)
    if not world_settings_path or not os.path.exists(world_settings_path): return []
    world_data = utils.parse_world_file(world_settings_path)
    if not world_data: return []

    # AIが直接 location_id として使用できる、純粋な場所名のセットを作成する
    locations = set()
    for area_name, places in world_data.items():
        for place_name in places.keys():
            if place_name == "__area_description__": continue
            locations.add(place_name)

    return sorted(list(locations))

def generate_scenery_context(
    room_name: str, 
    api_key: str, 
    force_regenerate: bool = False, 
    season_en: 'Optional[str]' = None, 
    time_of_day_en: 'Optional[str]' = None
) -> Tuple[str, str, str]:
    from gemini_api import get_configured_llm
    scenery_text = "（現在の場所の情景描写は、取得できませんでした）"
    space_def = "（現在の場所の定義・設定は、取得できませんでした）"
    location_display_name = "（不明な場所）"
    try:
        current_location_name = utils.get_current_location(room_name)
        if not current_location_name:
            current_location_name = "リビング"
            location_display_name = "リビング"

        world_settings_path = get_world_settings_path(room_name)
        world_data = utils.parse_world_file(world_settings_path)
        found_location = False
        for area, places in world_data.items():
            if current_location_name in places:
                space_def = places[current_location_name]
                location_display_name = f"[{area}] {current_location_name}"
                found_location = True
                break
        if not found_location:
            space_def = f"（場所「{current_location_name}」の定義が見つかりません）"

        from utils import get_season, get_time_of_day, load_scenery_cache, save_scenery_cache
        import hashlib
        import datetime

        # --- [ここからが修正の核心] ---
        # 1. 適用すべき季節と時間帯を決定する
        now = datetime.datetime.now()
        effective_season = season_en or get_season(now.month)
        effective_time_of_day = time_of_day_en or get_time_of_day(now.hour)

        # 2. 決定した値を使ってキャッシュキーを生成
        content_hash = hashlib.md5(space_def.encode('utf-8')).hexdigest()[:8]
        cache_key = f"{current_location_name}_{content_hash}_{effective_season}_{effective_time_of_day}"
        # --- [修正はここまで] ---

        if not force_regenerate:
            scenery_cache = load_scenery_cache(room_name)
            if cache_key in scenery_cache:
                cached_data = scenery_cache[cache_key]
                print(f"--- [有効な情景キャッシュを発見] ({cache_key})。APIコールをスキップします ---")
                return location_display_name, space_def, cached_data["scenery_text"]

        if not space_def.startswith("（"):
            log_message = "情景を強制的に再生成します" if force_regenerate else "情景をAPIで生成します"
            print(f"--- {log_message} ({cache_key}) ---")

            effective_settings = config_manager.get_effective_settings(room_name)
            llm_flash = get_configured_llm(constants.INTERNAL_PROCESSING_MODEL, api_key, effective_settings)

            # --- [ここからが修正の核心] ---
            # 3. AIへのプロンプトも、決定した値（日本語）を使って生成する
            season_map_en_to_ja = {"spring": "春", "summer": "夏", "autumn": "秋", "winter": "冬"}
            time_map_en_to_ja = {"morning": "朝", "daytime": "昼", "evening": "夕方", "night": "夜"}

            season_ja = season_map_en_to_ja.get(effective_season, "不明な季節")
            time_of_day_ja = time_map_en_to_ja.get(effective_time_of_day, "不明な時間帯")

            scenery_prompt = (
                "あなたは、与えられた二つの情報源から、一つのまとまった情景を描き出す、情景描写の専門家です。\n\n"
                f"【情報源1：適用すべき時間・季節】\n- 時間帯: {time_of_day_ja}\n- 季節: {season_ja}\n\n"
                f"【情報源2：この空間が持つ固有の設定】\n---\n{space_def}\n---\n\n"
                "【あなたのタスク】\n"
                "まず、心の中で【情報源1】と【情報源2】を比較し、矛盾があるかないかを判断してください。\n"
                "その判断に基づき、**最終的な情景描写の文章のみを、2〜3文で生成してください。**\n\n"
                "  - **矛盾がある場合** (例: 現実は昼なのに、空間は常に夜の設定など):\n"
                "    その**『にも関わらず』**という感覚や、その空間だけが持つ**不思議な空気感**に焦点を当てて描写してください。\n\n"
                "  - **矛盾がない場合**:\n"
                "    二つの情報を自然に**統合・融合**させ、その場のリアルな雰囲気をそのまま描写してください。\n\n"
                "【厳守すべきルール】\n"
                "- **あなたの思考過程や判断理由は、絶対に出力に含めないでください。**\n"
                "- 具体的な時刻（例：「23時42分」）は文章に含めないでください。\n"
                "- 人物やキャラクターの描写は絶対に含めないでください。\n"
                "- 五感に訴えかける、**空気感まで伝わるような**精緻で写実的な描写を重視してください。"
            )
            # --- [修正はここまで] ---
            scenery_text = llm_flash.invoke(scenery_prompt).content
            save_scenery_cache(room_name, cache_key, location_display_name, scenery_text)
        else:
            scenery_text = "（場所の定義がないため、情景を描写できません）"
    except Exception as e:
        print(f"--- 警告: 情景描写の生成中にエラーが発生しました ---\n{traceback.format_exc()}")
        location_display_name = "（エラー）"
        scenery_text = "（情景描写の生成中にエラーが発生しました）"
        space_def = "（エラー）"
    return location_display_name, space_def, scenery_text

def context_generator_node(state: AgentState):
    """
    【v23: プロンプト工場アーキテクチャ】
    マスターテンプレートの全てのプレースホルダを埋め、
    完成された単一のシステムプロンプトを生成する責務を負う。
    """
    room_name = state['room_name']

    # --- パート1: 状況プロンプト ({situation_prompt}) を生成 ---
    situation_prompt_parts = []
    if not state.get("send_scenery", True):
        situation_prompt_parts.append("【現在の場所と情景】\n（空間描写は設定により無効化されています）")
    else:
        # (この部分は変更なし)
        season_en = state.get("season_en", "autumn")
        time_of_day_en = state.get("time_of_day_en", "night")
        season_map_en_to_ja = {"spring": "春", "summer": "夏", "autumn": "秋", "winter": "冬"}
        time_map_en_to_ja = {"morning": "朝", "daytime": "昼", "evening": "夕方", "night": "夜"}
        season_ja = season_map_en_to_ja.get(season_en, "不明な季節")
        time_of_day_ja = time_map_en_to_ja.get(time_of_day_en, "不明な時間帯")
        location_display_name = state.get("location_name", "（不明な場所）")
        scenery_text = state.get("scenery_text", "（情景描写を取得できませんでした）")
        soul_vessel_room = state['all_participants'][0] if state['all_participants'] else state['room_name']
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
            else: space_def = "（エラー：世界設定のデータ構造が不正です）"
        available_locations = get_location_list(state['room_name'])
        location_list_str = "\n".join([f"- {loc}" for loc in available_locations]) if available_locations else "（現在、定義されている移動先はありません）"
        situation_prompt_parts.extend([
            "【現在の状況】", f"- 季節: {season_ja}", f"- 時間帯: {time_of_day_ja}\n",
            "【現在の場所と情景】", f"- 場所: {location_display_name}", f"- 今の情景: {scenery_text}",
            f"- 場所の設定（自由記述）: \n{space_def}\n", "【移動可能な場所】", location_list_str
        ])
    situation_prompt = "\n".join(situation_prompt_parts)

    # --- パート2: その他のプレースホルダを埋める ---
    # (この部分は以前のロジックとほぼ同じ)
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
            _, _, _, _, notepad_path = get_room_files_paths(room_name)
            if notepad_path and os.path.exists(notepad_path):
                with open(notepad_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    notepad_content = content if content else "（メモ帳は空です）"
            else: notepad_content = "（メモ帳ファイルが見つかりません）"
            notepad_section = f"\n### 短期記憶（メモ帳）\n{notepad_content}\n"
        except Exception as e:
            print(f"--- 警告: メモ帳の読み込み中にエラー: {e}")
            notepad_section = "\n### 短期記憶（メモ帳）\n（メモ帳の読み込み中にエラーが発生しました）\n"
    all_participants = state.get('all_participants', [])
    tools_list_str = "\n".join([f"- `{tool.name}({', '.join(tool.args.keys())})`: {tool.description}" for tool in all_tools])
    if len(all_participants) > 1: tools_list_str = "（グループ会話中はツールを使用できません）"

    # --- パート3: 最終的なプロンプトを組み立てて返す ---
    class SafeDict(dict):
        def __missing__(self, key): return f'{{{key}}}'

    prompt_vars = {
        'situation_prompt': situation_prompt,
        'character_prompt': character_prompt,
        'core_memory': core_memory,
        'notepad_section': notepad_section,
        'tools_list': tools_list_str
    }
    final_system_prompt_text = CORE_PROMPT_TEMPLATE.format_map(SafeDict(prompt_vars))

    return {"system_prompt": SystemMessage(content=final_system_prompt_text)}

def agent_node(state: AgentState):
    from gemini_api import get_configured_llm
    print("--- エージェントノード (agent_node) 実行 ---")
    loop_count = state.get("loop_count", 0)
    print(f"  - 現在の再思考ループカウント: {loop_count}")

    # --- [v23] 新アーキテクチャ対応 ---
    # 1. 完成済みのシステムプロンプトを取得
    base_system_prompt_text = state['system_prompt'].content
    final_system_prompt_text = base_system_prompt_text

    # 2. グループ会話用のペルソナロックプロンプトを注入（必要な場合）
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

    # 3. 履歴を取得 (state['messages'] は純粋な会話履歴)
    history_messages = state['messages']

    # 4. 最終的なメッセージリストを構築
    messages_for_agent = [final_system_prompt_message] + history_messages
    # --- [v23] 修正ここまで ---

    print(f"  - 使用モデル: {state['model_name']}")
    print(f"  - 最終システムプロンプト長: {len(final_system_prompt_text)} 文字")
    if state.get("debug_mode", False):
        print("--- [DEBUG MODE] 最終システムプロンプトの内容 ---")
        print(final_system_prompt_text)
        print("-----------------------------------------")

    llm = get_configured_llm(state['model_name'], state['api_key'], state['generation_config'])
    llm_with_tools = llm.bind_tools(all_tools)

    import pprint
    print("\n--- [DEBUG] AIに渡される直前のメッセージリスト (最終確認) ---")
    for i, msg in enumerate(messages_for_agent):
        msg_type = type(msg).__name__
        content_for_length_check = ""
        if hasattr(msg, 'content'):
            if isinstance(msg.content, str): content_for_length_check = msg.content
            elif isinstance(msg.content, list): content_for_length_check = "".join(part.get('text', '') if isinstance(part, dict) else str(part) for part in msg.content)
        print(f"[{i}] {msg_type} (Content Length: {len(content_for_length_check)})")
        if isinstance(msg, SystemMessage):
            print(f"  - Content (Head): {msg.content[:300]}...")
            print(f"  - Content (Tail): ...{msg.content[-300:]}")
        elif hasattr(msg, 'content'):
            print("  - Content:"); pprint.pprint(msg.content, indent=4)
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            print("  - Tool Calls:"); pprint.pprint(msg.tool_calls, indent=4)
        print("-" * 20)
    print("--------------------------------------------------\n")

    response = None
    try:
        response = llm_with_tools.invoke(messages_for_agent)
        
        print("\n--- [DEBUG] AIから返ってきた生の応答 ---")
        import copy
        response_for_log = copy.deepcopy(response)
        if hasattr(response_for_log, 'tool_calls') and response_for_log.tool_calls:
            for tool_call in response_for_log.tool_calls:
                if 'api_key' in tool_call.get('args', {}): tool_call['args']['api_key'] = '<REDACTED>'
        pprint.pprint(response_for_log)
        print("---------------------------------------\n")

        loop_count += 1
        # ツール呼び出しを含まない、純粋なテキスト応答の場合のみ、
        # 「最後の成功応答」として保存する
        if not getattr(response, "tool_calls", None):
            return {
                "messages": [response],
                "loop_count": loop_count,
                "last_successful_response": response
            }
        else:
            # ツール呼び出しの場合は、last_successful_response を更新しない
            return {
                "messages": [response],
                "loop_count": loop_count
            }

    except (google_exceptions.ResourceExhausted, google_exceptions.ServiceUnavailable, google_exceptions.InternalServerError) as e:
        print(f"--- [警告] agent_nodeでAPIエラーを捕捉しました: {e} ---")
        # 再思考中(2ループ目)の失敗か？
        if loop_count > 0:
            last_successful_response = state.get("last_successful_response")
            if last_successful_response:
                print("  - 再思考中にエラーが発生。前回の成功した応答を復元し、グラフを終了します。")
                # 前回の成功応答を復元し、強制終了フラグを立てる
                return {
                    "messages": [last_successful_response],
                    "force_end": True
                }
        
        # 1ループ目の失敗、または復元対象がない場合は、例外を再送出してUIハンドラに処理を任せる
        print("  - 1ループ目でのエラー、または復元可能な応答がないため、例外を上位に伝播させます。")
        raise e

import room_manager # ← 関数の先頭でインポートを追加

def safe_tool_executor(state: AgentState):
    """
    AIのツール呼び出しを仲介し、計画されたファイル編集タスクを実行する。
    APIのレート制限エラーに対して、賢くリトライまたは中断を行う。
    """
    from gemini_api import get_configured_llm
    print("--- ツール実行ノード (safe_tool_executor) 実行 ---")
    last_message = state['messages'][-1]
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        return {}

    tool_call = last_message.tool_calls[0]
    tool_name = tool_call["name"]
    tool_args = tool_call["args"]
    room_name = state.get('room_name')
    api_key = state.get('api_key')

    is_plan_main_memory = tool_name == "plan_main_memory_edit"
    is_plan_secret_diary = tool_name == "plan_secret_diary_edit"
    is_plan_notepad = tool_name == "plan_notepad_edit"
    is_plan_world = tool_name == "plan_world_edit"

    if is_plan_main_memory or is_plan_secret_diary or is_plan_notepad or is_plan_world:
        try:
            print(f"  - ファイル編集プロセスを開始: {tool_name}")

            # ▼▼▼【ここから下のブロックをまるごと追加】▼▼▼
            # 実際のファイル操作の前にバックアップを作成
            if is_plan_main_memory:
                room_manager.create_backup(room_name, 'memory')
            elif is_plan_secret_diary:
                room_manager.create_backup(room_name, 'secret_diary')
            elif is_plan_notepad:
                room_manager.create_backup(room_name, 'notepad')
            elif is_plan_world:
                room_manager.create_backup(room_name, 'world_setting')
            # ▲▲▲【追加はここまで】▲▲▲

            read_tool = None
            if is_plan_main_memory: read_tool = read_main_memory
            elif is_plan_secret_diary: read_tool = read_secret_diary
            elif is_plan_notepad: read_tool = read_full_notepad
            elif is_plan_world: read_tool = read_world_settings

            raw_content = read_tool.invoke({"room_name": room_name})

            if is_plan_main_memory or is_plan_secret_diary:
                lines = raw_content.split('\n')
                numbered_lines = [f"{i+1}: {line}" for i, line in enumerate(lines)]
                current_content = "\n".join(numbered_lines)
            else:
                current_content = raw_content

            print(f"  - ペルソナAI ({state['model_name']}) に編集タスクを依頼します。")
            llm_persona = get_configured_llm(state['model_name'], state['api_key'], state['generation_config'])

            instruction_templates = {
                "plan_main_memory_edit": (
                    "【最重要指示：これは『対話』ではなく『設計タスク』です】\n"
                    "あなたは今、自身の記憶ファイル(`memory_main.txt`)を更新するための『設計図』を作成しています。\n"
                    "このファイルは自由な書式のテキストファイルです。提示された【行番号付きデータ】とあなたの【変更要求】に基づき、完璧な【差分指示のリスト】を生成してください。\n\n"
                    "【行番号付きデータ（memory_main.txt全文）】\n---\n{current_content}\n---\n\n"
                    "【あなたの変更要求】\n「{modification_request}」\n\n"
                    "【絶対的な出力ルール】\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\n"
                    "- 各指示は \"operation\" ('replace', 'delete', 'insert_after'), \"line\" (対象行番号), \"content\" (新しい内容) のキーを持つ辞書です。\n\n"
                    "- **【操作方法】**\n"
                    "  - **`delete` (削除):** 指定した`line`番号の行を削除します。`content`は不要です。\n"
                    "  - **`replace` (置換):** 指定した`line`番号の行を、新しい`content`に置き換えます。\n"
                    "  - **`insert_after` (挿入):** 指定した`line`番号の**直後**に、新しい行として`content`を挿入します。\n"
                    "  - **複数行の操作:** 複数行をまとめて削除・置換する場合は、**各行に対して**個別の指示を生成してください。\n\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                 "plan_secret_diary_edit": (
                    "【最重要指示：これは『対話』ではなく『設計タスク』です】\n"
                    "あなたは今、自身の秘密の日記(`secret_diary.txt`)を更新するための『設計図』を作成しています。\n"
                    "このファイルは自由な書式のテキストファイルです。提示された【行番号付きデータ】とあなたの【変更要求】に基づき、完璧な【差分指示のリスト】を生成してください。\n\n"
                    "【行番号付きデータ（secret_diary.txt全文）】\n---\n{current_content}\n---\n\n"
                    "【あなたの変更要求】\n「{modification_request}」\n\n"
                    "【絶対的な出力ルール】\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\n"
                    "- 各指示は \"operation\" ('replace', 'delete', 'insert_after'), \"line\" (対象行番号), \"content\" (新しい内容) のキーを持つ辞書です。\n\n"
                    "- **【操作方法】**\n"
                    "  - **`delete` (削除):** 指定した`line`番号の行を削除します。`content`は不要です。\n"
                    "  - **`replace` (置換):** 指定した`line`番号の行を、新しい`content`に置き換えます。\n"
                    "  - **`insert_after` (挿入):** 指定した`line`番号の**直後**に、新しい行として`content`を挿入します。\n"
                    "  - **複数行の操作:** 複数行をまとめて削除・置換する場合は、**各行に対して**個別の指示を生成してください。\n\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_world_edit": (
                    "【最重要指示：これは『対話』ではなく『世界構築タスク』です】\n"
                    "あなたは今、世界設定を更新するための『設計図』を作成しています。\n"
                    "提示された【既存のデータ】とあなたの【変更要求】に基づき、完璧な【差分指示のリスト】を生成してください。\n\n"
                    "【既存のデータ（world_settings.txt全文）】\n---\n{current_content}\n---\n\n"
                    "【あなたの変更要求】\n「{modification_request}」\n\n"
                    "【絶対的な出力ルール】\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\n"
                    "- 各指示は \"operation\" ('update_place_description', 'add_place', 'delete_place'), \"area_name\", \"place_name\", \"value\" のキーを持つ辞書です。\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_notepad_edit": (
                    "【最重要指示：これは『対話』ではなく『編集タスク』です】\n"
                    "あなたは今、自身のメモ帳を更新しています。\n"
                    "提示された【既存のデータ】とあなたの【変更要求】に基づき、最終的にファイルに書き込むべき、完璧な【全文】を生成してください。\n\n"
                    "【既存のデータ（notepad.md全文）】\n---\n{current_content}\n---\n\n"
                    "【あなたの変更要求】\n「{modification_request}」\n\n"
                    "【絶対的な出力ルール】\n"
                    "- 思考や挨拶は含めず、最終的なファイル全文のみを出力してください。"
                )
            }
            formatted_instruction = instruction_templates[tool_name].format(
                current_content=current_content,
                modification_request=tool_args.get('modification_request')
            )
            edit_instruction_message = HumanMessage(content=formatted_instruction)

            history_for_editing = [msg for msg in state['messages'] if msg is not last_message]
            final_context_for_editing = [state['system_prompt']] + history_for_editing + [edit_instruction_message]

            if state.get("debug_mode", True): # デバッグモード中は常に出力
                print("\n--- [DEBUG] AIへの最終編集タスクプロンプト (完全版) ---")
                for i, msg in enumerate(final_context_for_editing):
                    msg_type = type(msg).__name__
                    content_preview = str(msg.content)[:500].replace('\n', ' ')
                    print(f"[{i}] {msg_type} (Content Length: {len(str(msg.content))})")
                    if i == len(final_context_for_editing) - 1: # 最後の指示メッセージは全文表示
                        print(f"  - Content (Full):\n{msg.content}")
                    else:
                        print(f"  - Content (Preview): {content_preview}...")
                print("----------------------------------------------------------\n")

            edited_content_document = None
            max_retries = 5
            base_delay = 5
            for attempt in range(max_retries):
                try:
                    response = llm_persona.invoke(final_context_for_editing)
                    edited_content_document = response.content.strip()
                    break # 成功したらループを抜ける
                except google_exceptions.ResourceExhausted as e:
                    error_str = str(e)
                    if "PerDay" in error_str or "Daily" in error_str:
                        print(f"  - 致命的エラー: 回復不能なAPI上限（日間など）に達しました。処理を中断します。")
                        raise RuntimeError("回復不能なAPIレート上限（日間など）に達したため、処理を中断しました。") from e

                    wait_time = base_delay * (2 ** attempt)
                    match = re.search(r"retry_delay {\s*seconds: (\d+)\s*}", error_str)
                    if match:
                        wait_time = int(match.group(1)) + 1
                        print(f"  - APIレート制限: APIの推奨に従い {wait_time}秒 待機します...")
                    else:
                        print(f"  - APIレート制限: 指数バックオフで {wait_time}秒 待機します...")

                    if attempt < max_retries - 1:
                        time.sleep(wait_time)
                    else:
                        raise e
                except (google_exceptions.ServiceUnavailable, google_exceptions.InternalServerError) as e:
                    if attempt < max_retries - 1:
                        wait_time = base_delay * (2 ** attempt)
                        print(f"  - 警告: 編集AIが応答不能です ({e.args[0]})。{wait_time}秒待機して再試行します... ({attempt + 1}/{max_retries})")
                        time.sleep(wait_time)
                    else:
                        raise e

            if edited_content_document is None:
                raise RuntimeError("編集AIからの応答が、リトライ後も得られませんでした。")

            print("  - AIからの応答を受け、ファイル書き込みを実行します。")

            if is_plan_main_memory or is_plan_secret_diary or is_plan_world:
                json_match = re.search(r'```json\s*([\s\S]*?)\s*```', edited_content_document, re.DOTALL)
                content_to_process = json_match.group(1).strip() if json_match else edited_content_document
                instructions = json.loads(content_to_process)

                print(f"--- [DEBUG] AIが生成した差分指示リスト ---\n{json.dumps(instructions, indent=2, ensure_ascii=False)}\n------------------------------------")

                if is_plan_main_memory:
                    output = _apply_main_memory_edits(instructions=instructions, room_name=room_name)
                elif is_plan_secret_diary:
                    output = _apply_secret_diary_edits(instructions=instructions, room_name=room_name)
                else: # is_plan_world
                    output = _apply_world_edits(instructions=instructions, room_name=room_name)
            else:
                text_match = re.search(r'```(?:.*\n)?([\s\S]*?)```', edited_content_document, re.DOTALL)
                content_to_process = text_match.group(1).strip() if text_match else edited_content_document
                output = _write_notepad_file(full_content=content_to_process, room_name=room_name, modification_request=tool_args.get('modification_request'))

            if "成功" in output:
                output += " **このファイル編集タスクは完了しました。これから編集するというような前置きはせず、**編集が完了したことや、その結果について簡潔にユーザーに報告してください。"

        except Exception as e:
            output = f"ファイル編集プロセス中にエラーが発生しました ('{tool_name}'): {e}"
            traceback.print_exc()
    else:
        print(f"  - 通常ツール実行: {tool_name}")
        # 引数ログ用コピーにAPIキーがあればマスク
        tool_args_for_log = tool_args.copy()
        if 'api_key' in tool_args_for_log:
            tool_args_for_log['api_key'] = '<REDACTED>'
        # 必要に応じて以下のように利用
        # print(f"    - 引数: {tool_args_for_log}")

        tool_args['room_name'] = room_name
        if tool_name in ['generate_image', 'search_past_conversations']:
            tool_args['api_key'] = api_key

        selected_tool = next((t for t in all_tools if t.name == tool_name), None)
        if not selected_tool:
            output = f"Error: Tool '{tool_name}' not found."
        else:
            try:
                output = selected_tool.invoke(tool_args)
            except Exception as e:
                output = f"Error executing tool '{tool_name}': {e}"
                traceback.print_exc()

    return {"messages": [ToolMessage(content=str(output), tool_call_id=tool_call["id"], name=tool_name)], "loop_count": state.get("loop_count", 0)}

def route_after_agent(state: AgentState) -> Literal["__end__", "safe_tool_node", "agent"]:
    print("--- エージェント後ルーター (route_after_agent) 実行 ---")

    if state.get("force_end"):
        print("  - force_endフラグを検出。グラフの実行を強制終了します。")
        return "__end__"

    last_message = state["messages"][-1]
    loop_count = state.get("loop_count", 0)

    if last_message.tool_calls:
        print("  - ツール呼び出しあり。ツール実行ノードへ。")
        for tool_call in last_message.tool_calls:
            # 引数ログのAPIキーをマスク
            args_for_log = dict(tool_call['args']) if isinstance(tool_call.get('args'), dict) else tool_call.get('args')
            if isinstance(args_for_log, dict) and 'api_key' in args_for_log:
                args_for_log = args_for_log.copy()
                args_for_log['api_key'] = '<REDACTED>'
            print(f"    🛠️ ツール呼び出し: {tool_call['name']} | 引数: {args_for_log}")
        return "safe_tool_node"

    # 1回までの再思考を許容する
    if loop_count < 2:
        print(f"  - ツール呼び出しなし。再思考します。(ループカウント: {loop_count})")
        return "agent" # agentノードにループバック

    print(f"  - ツール呼び出しなし。最大ループ回数({loop_count})に達したため、グラフを終了します。")
    return "__end__"

workflow = StateGraph(AgentState)

# ノードを定義
workflow.add_node("context_generator", context_generator_node)
workflow.add_node("agent", agent_node)
workflow.add_node("safe_tool_node", safe_tool_executor)

# エッジ（処理の流れ）を定義
workflow.set_entry_point("context_generator")
workflow.add_edge("context_generator", "agent")

workflow.add_conditional_edges(
    "agent",
    route_after_agent,
    {
        "safe_tool_node": "safe_tool_node",
        "agent": "agent", # ← この行を追加
        "__end__": END,
    },
)

# ツール実行後は、必ずエージェントの再思考に戻る
workflow.add_edge("safe_tool_node", "agent")

# グラフをコンパイル
app = workflow.compile()
print("--- [分離思考型] グラフがコンパイルされました ---")
