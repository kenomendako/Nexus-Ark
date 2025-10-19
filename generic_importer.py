# generic_importer.py

import json
import os
import re
import traceback
from typing import Optional, Dict, Any, List

import room_manager
import constants

def parse_metadata_from_file(file_path: str) -> Dict[str, str]:
    """
    アップロードされたファイルから、メタ情報（タイトル、ユーザー名など）を自動抽出する。
    ChatGPT ExporterのJSON, MD形式に対応。
    """
    metadata = {"title": os.path.basename(file_path), "user": "ユーザー"}
    file_content = ""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            file_content = f.read()
    except Exception:
        return metadata

    if file_path.endswith(".json"):
        try:
            data = json.loads(file_content)
            if "metadata" in data and isinstance(data["metadata"], dict):
                meta = data["metadata"]
                metadata["title"] = meta.get("title", metadata["title"])
                if "user" in meta and isinstance(meta["user"], dict) and "name" in meta["user"]:
                    metadata["user"] = meta["user"]["name"]
            elif "title" in data: # ChatGPT公式エクスポート形式
                 metadata["title"] = data.get("title", metadata["title"])
            return metadata
        except json.JSONDecodeError:
            pass

    title_match = re.search(r"^#\s+(.+)$", file_content, re.MULTILINE)
    user_match = re.search(r"^\*\*User:\*\*\s+(.+?)\s*\(", file_content, re.MULTILINE)
    if title_match:
        metadata["title"] = title_match.group(1).strip()
    if user_match:
        metadata["user"] = user_match.group(1).strip()

    return metadata

def _import_chatgpt_exporter_json(
    file_path: str, safe_folder_name: str
) -> List[str]:
    """ChatGPT ExporterのJSONファイル形式を特別に処理する。"""
    log_entries = []
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    messages = data.get("messages", [])
    for message in messages:
        role = message.get("role")
        content = message.get("say", "").strip()
        if not content:
            continue
        
        if role == "Prompt":
            log_entries.append(f"## USER:user\n{content}")
        elif role == "Response":
            log_entries.append(f"## AGENT:{safe_folder_name}\n{content}")
    return log_entries

def import_from_generic_text(
    file_path: str, room_name: str, user_display_name: str, user_header: str, agent_header: str
) -> Optional[str]:
    """
    任意のテキストファイルと話者ヘッダー指定から、新しいルームを作成する。
    """
    print(f"--- [Generic Importer] Starting import for file: {os.path.basename(file_path)} ---")
    if not all([file_path, room_name, user_display_name, user_header, agent_header]):
        return "ERROR: MISSING_ARGS"
        
    try:
        # ルームの骨格を作成
        safe_folder_name = room_manager.generate_safe_folder_name(room_name)
        if not room_manager.ensure_room_files(safe_folder_name):
            return "ERROR: ROOM_CREATION_FAILED"
        print(f"--- [Generic Importer] Created room skeleton: {safe_folder_name} ---")

        log_entries = []
        
        # --- [新ロジック] ChatGPT ExporterのJSON形式を特別扱い ---
        if file_path.endswith('.json') and user_header == "role:Prompt" and agent_header == "role:Response":
            print("--- [Generic Importer] Detected ChatGPT Exporter JSON format. ---")
            log_entries = _import_chatgpt_exporter_json(file_path, safe_folder_name)
        else:
            # --- 既存のテキストベースの処理 ---
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # --- [新ロジック] ChatGPT Exporterのフッターを除去 ---
            exporter_footer = "Powered by ChatGPT Exporter (https://www.chatgptexporter.com)"
            if exporter_footer in content:
                content = content.split(exporter_footer)[0].strip()

            user_h = re.escape(user_header)
            agent_h = re.escape(agent_header)
            pattern = re.compile(f"(^{user_h}|^{agent_h})", re.MULTILINE)
            
            parts = pattern.split(content)
            if len(parts) <= 1:
                print("[Generic Importer] ERROR: No speaker headers found in the file.")
                # UIにフィードバックするための特別なエラーコードを返す
                return "ERROR: NO_HEADERS"

            for i in range(1, len(parts), 2):
                header = parts[i]
                text = parts[i+1].strip()
                if not text: continue

                if header == user_header:
                    log_entries.append(f"## USER:user\n{text}")
                elif header == agent_header:
                    log_entries.append(f"## AGENT:{safe_folder_name}\n{text}")

        if not log_entries:
            return "ERROR: NO_MESSAGES"

        # ファイルへの書き込み (共通処理)
        log_file_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "log.txt")
        full_log_content = "\n\n".join(log_entries)
        if full_log_content:
            full_log_content += "\n\n"
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write(full_log_content)
        print(f"--- [Generic Importer] Wrote {len(log_entries)} entries to log.txt ---")

        # room_config.json の更新 (共通処理)
        config_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "room_config.json")
        with open(config_path, "r+", encoding="utf-8") as f:
            config = json.load(f)
            config["room_name"] = room_name
            config["user_display_name"] = user_display_name
            config["description"] = f"汎用インポーターから取り込まれた会話ログです。\nOriginal File: {os.path.basename(file_path)}"
            f.seek(0)
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.truncate()
        print(f"--- [Generic Importer] Updated room_config.json ---")

        print(f"--- [Generic Importer] Successfully imported file to room: {safe_folder_name} ---")
        return safe_folder_name

    except Exception as e:
        print(f"[Generic Importer] An unexpected error occurred: {e}")
        traceback.print_exc()
        return "ERROR: UNEXPECTED"