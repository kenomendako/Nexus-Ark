# claude_importer.py

import ijson
import json
import os
import traceback
from typing import Optional, Dict, Any, List, Tuple

import zipfile
import tempfile
import room_manager
import constants

def resolve_conversations_file_path(file_path: str) -> str:
    """
    ファイルパスを受け取り、ZIPであれば解凍して conversations.json のパスを返す。
    ZIPでない場合はそのまま返す。
    """
    if not file_path.lower().endswith('.zip'):
        return file_path

    try:
        # 解凍先の一時ディレクトリを作成
        temp_dir = tempfile.mkdtemp()
        print(f"[Claude Importer] Extracting ZIP file to: {temp_dir}")
        
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
            
        # conversations.json を探す
        json_path = os.path.join(temp_dir, 'conversations.json')
        if os.path.exists(json_path):
            print(f"[Claude Importer] Found conversations.json at: {json_path}")
            return json_path
            
        # 見つからない場合、ルートにある .json ファイルを探索してみる
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                if file.endswith('.json'):
                     potential_path = os.path.join(root, file)
                     print(f"[Claude Importer] conversations.json not found, using alternative JSON: {potential_path}")
                     return potential_path
        
        print(f"[Claude Importer] WARNING: No JSON file found in ZIP.")
        return file_path # 失敗したので元のパスを返す
        
    except zipfile.BadZipFile:
        print(f"[Claude Importer] Error: File is not a valid ZIP file.")
        return file_path
    except Exception as e:
        print(f"[Claude Importer] Error extracting ZIP: {e}")
        return file_path


def get_claude_thread_list(file_path: str) -> List[Tuple[str, str]]:
    """
    Claudeのconversations.jsonから、UI表示用の(スレッド名, UUID)のリストを取得する。
    """
    threads = []
    try:
        resolved_path = resolve_conversations_file_path(file_path)
        with open(resolved_path, 'rb') as f:
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

def import_from_claude_export(file_path: str, conversation_uuids: Any, room_name: str, user_display_name: str) -> Optional[str]:
    """
    Claudeのエクスポートファイルから指定された会話をインポートし、新しいルームを作成する。
    conversation_uuids: インポートする会話のUUID（単一文字列 または 文字列のリスト）
    """
    
    # UUIDをリスト形式に統一
    target_ids = []
    if isinstance(conversation_uuids, str):
        target_ids.append(conversation_uuids)
    elif isinstance(conversation_uuids, list):
        target_ids.extend(conversation_uuids)
    else:
         print(f"[Claude Importer] ERROR: Invalid conversation_uuids type: {type(conversation_uuids)}")
         return None

    print(f"--- [Claude Importer] Starting import for {len(target_ids)} conversations. ---")

    try:
        resolved_path = resolve_conversations_file_path(file_path)
        
        # 1. 指定された会話データをファイルから収集
        collected_conversations = {}
        original_name = "N/A"
        
        with open(resolved_path, 'rb') as f:
            for conversation in ijson.items(f, 'item'):
                uuid = conversation.get("uuid")
                if uuid in target_ids:
                    collected_conversations[uuid] = conversation
                    if original_name == "N/A":
                        original_name = conversation.get("name", "N/A")
        
        if not collected_conversations:
             print(f"[Claude Importer] ERROR: None of the specified conversations found in '{file_path}'.")
             return None

        # 2. メッセージを収集 (ターゲットID順に結合)
        all_messages = []
        for uuid in target_ids:
            if uuid not in collected_conversations:
                print(f"[Claude Importer] WARNING: Conversation '{uuid}' not found. Skipping.")
                continue
                
            conv_data = collected_conversations[uuid]
            messages = conv_data.get("chat_messages", [])
            if messages:
                # 必要ならタイムスタンプでソートするが、Claudeのエクスポートは通常時系列順
                all_messages.extend(messages)
            
        if not all_messages:
            print(f"[Claude Importer] ERROR: No messages found in any of the specified conversations.")
            return None

        # 3. ルームの骨格を作成
        safe_folder_name = room_manager.generate_safe_folder_name(room_name)
        if not room_manager.ensure_room_files(safe_folder_name):
            print(f"[Claude Importer] ERROR: Failed to create room files for '{safe_folder_name}'.")
            return None
        print(f"--- [Claude Importer] Created room skeleton: {safe_folder_name} ---")
        
        # 4. ログ形式への変換
        log_entries = []
        for message in all_messages:
            sender = message.get("sender")
            text_content = message.get("text", "").strip()
            if not text_content:
                continue

            # (必要であれば) created_at を見て日付行を入れることも検討できるが、
            # Claude JSONの created_at は "2023-10-27T10:00:00.000000Z" 形式など
            # 今回はシンプルに結合する
            
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

        # SystemPrompt.txt は空のままにする

        # 6. room_config.json の更新
        config_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "room_config.json")
        with open(config_path, "r+", encoding="utf-8") as f:
            config = json.load(f)
            config["room_name"] = room_name
            config["user_display_name"] = user_display_name if user_display_name else "ユーザー"
            
            desc_text = f"Claudeからインポートされた会話ログです。\nOriginal Name: {original_name}"
            if len(target_ids) > 1:
                desc_text += f" (+{len(target_ids)-1} threads)"
            config["description"] = desc_text
            
            f.seek(0)
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.truncate()
        print(f"--- [Claude Importer] Updated room_config.json ---")

        print(f"--- [Claude Importer] Successfully imported {len(target_ids)} conversations to room: {safe_folder_name} ---")
        return safe_folder_name

    except Exception as e:
        print(f"[Claude Importer] An unexpected error occurred during import: {e}")
        traceback.print_exc()
        return None