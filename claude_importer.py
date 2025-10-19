# claude_importer.py

import ijson
import json
import os
import traceback
from typing import Optional, Dict, Any, List, Tuple

import room_manager
import constants

def get_claude_thread_list(file_path: str) -> List[Tuple[str, str]]:
    """
    Claudeのconversations.jsonから、UI表示用の(スレッド名, UUID)のリストを取得する。
    """
    threads = []
    try:
        with open(file_path, 'rb') as f:
            for conversation in ijson.items(f, 'item'):
                uuid = conversation.get("uuid")
                name = conversation.get("name")
                if uuid and name:
                    threads.append((name, uuid))
    except Exception as e:
        print(f"[Claude Importer] Error reading or parsing JSON file: {e}")
        traceback.print_exc()
    # 名前でソートして返す
    return sorted(threads, key=lambda x: x[0])

def import_from_claude_export(file_path: str, conversation_uuid: str, room_name: str, user_display_name: str) -> Optional[str]:
    """
    Claudeのエクスポートファイルから指定された会話をインポートし、新しいルームを作成する。
    """
    print(f"--- [Claude Importer] Starting import for conversation_uuid: {conversation_uuid} ---")
    try:
        # 1. 指定された会話データをファイルから見つけ出す
        conversation_data = None
        with open(file_path, 'rb') as f:
            for conversation in ijson.items(f, 'item'):
                if conversation.get("uuid") == conversation_uuid:
                    conversation_data = conversation
                    break
        
        if not conversation_data:
            print(f"[Claude Importer] ERROR: Conversation with UUID '{conversation_uuid}' not found in '{file_path}'.")
            return None

        # 2. 会話メッセージを取得
        messages = conversation_data.get("chat_messages", [])
        if not messages:
            print(f"[Claude Importer] ERROR: No messages found in conversation '{conversation_uuid}'.")
            return None

        # 3. ルームの骨格を作成
        safe_folder_name = room_manager.generate_safe_folder_name(room_name)
        if not room_manager.ensure_room_files(safe_folder_name):
            print(f"[Claude Importer] ERROR: Failed to create room files for '{safe_folder_name}'.")
            return None
        print(f"--- [Claude Importer] Created room skeleton: {safe_folder_name} ---")
        
        # 4. ログ形式への変換
        log_entries = []
        for message in messages:
            sender = message.get("sender")
            text_content = message.get("text", "").strip()
            if not text_content:
                continue

            if sender == "human":
                log_entries.append(f"## USER:user\n{text_content}")
            elif sender == "assistant":
                log_entries.append(f"## AGENT:{safe_folder_name}\n{text_content}")

        # 5. ファイルへの書き込み
        log_file_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "log.txt")
        full_log_content = "\n\n".join(log_entries)
        if full_log_content:
            full_log_content += "\n\n"
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write(full_log_content)
        print(f"--- [Claude Importer] Wrote {len(log_entries)} entries to log.txt ---")

        # SystemPrompt.txt は空のままにする（ChatGPTと仕様を合わせる）

        # 6. room_config.json の更新
        config_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "room_config.json")
        with open(config_path, "r+", encoding="utf-8") as f:
            config = json.load(f)
            config["room_name"] = room_name
            config["user_display_name"] = user_display_name if user_display_name else "ユーザー"
            config["description"] = f"Claudeからインポートされた会話ログです。\nOriginal Name: {conversation_data.get('name', 'N/A')}"
            f.seek(0)
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.truncate()
        print(f"--- [Claude Importer] Updated room_config.json ---")

        print(f"--- [Claude Importer] Successfully imported conversation to room: {safe_folder_name} ---")
        return safe_folder_name

    except Exception as e:
        print(f"[Claude Importer] An unexpected error occurred during import: {e}")
        traceback.print_exc()
        return None