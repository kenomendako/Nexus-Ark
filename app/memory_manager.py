# memory_manager.py (リファクタリング版)
import json
import os
import traceback
import datetime
import gradio as gr
from room_manager import get_room_files_paths
import constants

def save_memory_data(room_name, json_string_data):
    if not room_name: gr.Error("ルーム名未指定"); return gr.update()
    _, _, _, memory_json_path, _, _ = get_room_files_paths(room_name)
    if not memory_json_path: gr.Error(f"'{room_name}' 記憶パス取得失敗"); return gr.update()
    try:
        memory_data_to_save = json.loads(json_string_data)
        if not isinstance(memory_data_to_save, dict): gr.Error("記憶データ形式不正"); return gr.update()
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        memory_data_to_save["last_updated"] = now_str
        with open(memory_json_path, "w", encoding="utf-8") as f: json.dump(memory_data_to_save, f, indent=2, ensure_ascii=False)
        gr.Info(f"'{room_name}' 記憶保存完了 ({now_str})")
        return gr.update(value=json.dumps(memory_data_to_save, indent=2, ensure_ascii=False))
    except Exception as e: gr.Error(f"記憶保存エラー: {e}"); traceback.print_exc(); return gr.update()

def load_memory_data_safe(memory_json_path):
    if memory_json_path and os.path.exists(memory_json_path):
        try:
            with open(memory_json_path, "r", encoding="utf-8") as f: data = json.load(f)
            return data if isinstance(data, dict) else {"error": "Invalid Format", "message": "記憶ファイルの形式がJSONオブジェクトではありません。"}
        except json.JSONDecodeError as e:
            return {"error": "JSON Decode Error", "message": f"記憶ファイルのJSON形式が正しくありません: {e}"}
        except Exception as e:
            return {"error": "Read Error", "message": f"記憶ファイルの読み込み中にエラーが発生しました: {e}"}
    return {}