# tools/space_tools.py (v20: Final Architecture)

import os
import re
from langchain_core.tools import tool
from room_manager import get_world_settings_path
import utils
import constants
from typing import List, Dict, Any
import traceback

@tool
def set_current_location(location_id: str, room_name: str) -> str:
    """AIの現在地を設定する。location_idには、利用可能な場所の名前だけを正確に指定する必要がある。"""
    if not location_id or not room_name:
        return "【Error】Internal tool error: Location ID and room name are required for execution."

    # 1. 有効な場所名のリストを世界設定から取得する
    world_settings_path = get_world_settings_path(room_name)
    world_data = utils.parse_world_file(world_settings_path)
    if not world_data:
        return f"【Error】世界設定ファイルが見つからないか、空です。ルーム '{room_name}' を確認してください。"

    valid_locations = []
    for area in world_data:
        for place in world_data[area]:
            if not place.startswith("__"):
                valid_locations.append(place)

    if not valid_locations:
        return "【Error】世界設定ファイルに、移動可能な場所が一つも定義されていません。"

    # 2. AIからの入力を解釈し、正しい場所名に補正する
    original_input = location_id.strip()
    corrected_location_id = None

    # 2a. 完全一致を試みる
    if original_input in valid_locations:
        corrected_location_id = original_input

    # 2b. それでも見つからない場合、一般的な間違いパターンを吸収する
    if not corrected_location_id:
        # `[エリア名] 場所名` や `[エリア名]場所名` の形式を想定し、括弧とエリア名を除去
        cleaned_input = re.sub(r'\[.*?\]', '', original_input).strip()
        if cleaned_input in valid_locations:
            corrected_location_id = cleaned_input
        else:
            # `エリア名 場所名` の形式を想定し、最後の単語を試す
            parts = cleaned_input.split()
            if len(parts) > 1 and parts[-1] in valid_locations:
                corrected_location_id = parts[-1]
            else:
                 # `エリア名場所名` のような連結形式を想定し、後方一致で探す
                 for loc in valid_locations:
                     if cleaned_input.endswith(loc):
                         corrected_location_id = loc
                         break

    # 3. 補正後のIDでファイルに書き込む
    if corrected_location_id:
        try:
            base_path = os.path.join(constants.ROOMS_DIR, room_name)
            location_file_path = os.path.join(base_path, "current_location.txt")
            with open(location_file_path, "w", encoding="utf-8") as f:
                f.write(corrected_location_id)
            return f"Success: 現在地は '{corrected_location_id}' に設定されました。この移動タスクは完了です。**このタスクの実行を宣言するような前置きは不要です。**完了したという事実と、移動後の情景などを簡潔にユーザーに報告してください。"
        except Exception as e:
            return f"【Error】現在地のファイル書き込みに失敗しました: {e}"
    else:
        # 4. 全ての試みが失敗した場合、AIに明確なエラーと選択肢を返す
        valid_locations_str = ", ".join(f"'{loc}'" for loc in sorted(list(set(valid_locations))))
        return (f"【Error】指定された場所 '{original_input}' は見つかりませんでした。"
                f"location_idには、以下のいずれかを正確に指定してください: {valid_locations_str}")

def _apply_world_edits(instructions: List[Dict[str, Any]], room_name: str) -> str:
    """【内部専用】AIが生成した世界設定への差分編集指示リストを解釈し、world_settings.txtに適用する。"""
    if not room_name: return "【エラー】ルーム名が指定されていません。"

    world_settings_path = get_world_settings_path(room_name)
    world_data = utils.parse_world_file(world_settings_path)

    try:
        for i, inst in enumerate(instructions):
            op = inst.get("operation", "").lower()
            area = inst.get("area_name")
            place = inst.get("place_name")
            value = inst.get("value")

            if not op or not area:
                return f"【エラー】指示 {i+1} に 'operation' または 'area_name' がありません。"

            if op == "update_place_description":
                if not place or value is None: return f"【エラー】指示 {i+1} (update) に 'place_name' または 'value' がありません。"
                world_data.setdefault(area, {})[place] = str(value)
            elif op == "add_place":
                if not place or value is None: return f"【エラー】指示 {i+1} (add_place) に 'place_name' または 'value' がありません。"
                world_data.setdefault(area, {})[place] = str(value)
            elif op == "delete_place":
                if not place: return f"【エラー】指示 {i+1} (delete_place) に 'place_name' がありません。"
                if area in world_data and place in world_data[area]:
                    del world_data[area][place]
            else:
                return f"【エラー】指示 {i+1} の操作 '{op}' は無効です。"

        # world_builderのsave_world_dataを再利用してファイルに書き込む
        from world_builder import save_world_data
        save_world_data(room_name, world_data) # save_world_dataはGradioの通知を出すが、ツール実行では無視されるので問題ない

        return f"成功: {len(instructions)}件の指示に基づき、世界設定(world_settings.txt)を更新しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】世界設定の編集中に予期せぬエラーが発生しました: {e}"

@tool
def plan_world_edit(modification_request: str, room_name: str) -> str:
    """
    【ステップ1：計画】現在の世界設定（world_settings.txt）の変更を計画します。
    このツールは、あなたが世界に対してどのような変更を行いたいかの「意図」をシステムに伝えるために、最初に呼び出します。
    """
    return f"システムへの世界設定編集計画を受け付けました。意図:「{modification_request}」"

@tool
def read_world_settings(room_name: str) -> str:
    """
    現在の世界設定（world_settings.txt）の全文をテキスト形式で読み取る。
    主に、編集以外の目的で内容を確認したい場合に使用します。
    """
    if not room_name: return "【Error】Internal tool error: room_name is required for execution."
    world_settings_path = get_world_settings_path(room_name)
    if not world_settings_path or not os.path.exists(world_settings_path):
        return f"【Error】Could not find world settings file for room '{room_name}'."
    try:
        with open(world_settings_path, "r", encoding="utf-8") as f: content = f.read()
        return content if content.strip() else "【情報】世界設定ファイルは空です。"
    except Exception as e: return f"【Error】Failed to read world settings file: {e}"
