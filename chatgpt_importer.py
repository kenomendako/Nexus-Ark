import ijson
import json
import os
import traceback
from typing import Optional, Dict, Any, List

import room_manager
import constants

def _find_conversation_data(file_path: str, conversation_id: str) -> Optional[Dict[str, Any]]:
    """
    指定されたJSONファイルから、特定のconversation_idに一致する会話データをストリーミングで検索して返す。
    """
    try:
        with open(file_path, 'rb') as f:
            for conversation in ijson.items(f, 'item'):
                if conversation and 'mapping' in conversation:
                    # mappingの最初のキーがIDであるという仕様
                    first_key = next(iter(conversation['mapping']), None)
                    if first_key == conversation_id:
                        return conversation
    except (ijson.JSONError, IOError, StopIteration) as e:
        print(f"[ChatGPT Importer] Error reading or parsing JSON file: {e}")
        traceback.print_exc()
    return None

def _reconstruct_thread(mapping: Dict[str, Any], start_node_id: str) -> List[Dict[str, Any]]:
    """
    mappingデータと開始ノードIDから、会話のメインスレッドを再構築する。
    """
    thread = []
    current_id = start_node_id
    while current_id and current_id in mapping:
        node = mapping[current_id]
        message = node.get("message")
        if message and message.get("author") and message.get("content"):
            thread.append(message)

        # ほとんどの会話は分岐しないため、最初の子供をたどる
        children = node.get("children", [])
        if children:
            current_id = children[0]
        else:
            break # スレッドの終わり
    return thread

def import_from_chatgpt_export(file_path: str, conversation_id: str, room_name: str, user_display_name: str) -> Optional[str]:
    """
    ChatGPTのエクスポートファイルから指定された会話をインポートし、新しいルームを作成する。

    Args:
        file_path: conversations.json のパス
        conversation_id: インポートする会話のID
        room_name: 新しいルームの表示名
        user_display_name: ユーザーの表示名

    Returns:
        成功した場合は新しいルームのフォルダ名、失敗した場合はNone
    """
    print(f"--- [ChatGPT Importer] Starting import for conversation_id: {conversation_id} ---")
    try:
        # 1. 指定された会話データをファイルから見つけ出す
        conversation_data = _find_conversation_data(file_path, conversation_id)
        if not conversation_data:
            print(f"[ChatGPT Importer] ERROR: Conversation with ID '{conversation_id}' not found in '{file_path}'.")
            return None

        # 2. 会話スレッドを再構築
        mapping = conversation_data.get("mapping", {})
        thread_messages = _reconstruct_thread(mapping, conversation_id)
        if not thread_messages:
            print(f"[ChatGPT Importer] ERROR: No valid messages found in conversation '{conversation_id}'.")
            return None

        # 3. ルームのフォルダ名と基本ファイルを作成
        safe_folder_name = room_manager.generate_safe_folder_name(room_name)
        if not room_manager.ensure_room_files(safe_folder_name):
            print(f"[ChatGPT Importer] ERROR: Failed to create room files for '{safe_folder_name}'.")
            return None
        print(f"--- [ChatGPT Importer] Created room skeleton: {safe_folder_name} ---")

        # 4. ログ形式への変換とSystemPromptの準備
        log_entries = []
        first_user_prompt = None

        for message in thread_messages:
            author_role = message.get("author", {}).get("role")
            content_parts = message.get("content", {}).get("parts", [])

            # content.partsが空、またはNoneの場合をスキップ
            if not content_parts or not isinstance(content_parts, list):
                continue

            # content.parts の中身が文字列でない場合も考慮
            text_content = "".join(str(p) for p in content_parts if isinstance(p, str) and p.strip()).strip()

            if not text_content:
                continue

            if author_role == "user":
                log_entries.append(f"## USER:user\n{text_content}")
                if first_user_prompt is None:
                    first_user_prompt = text_content
            elif author_role == "assistant":
                log_entries.append(f"## AGENT:{safe_folder_name}\n{text_content}")

        # 5. ファイルへの書き込み
        # 5a. log.txt
        log_file_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "log.txt")
        full_log_content = "\n\n".join(log_entries)
        # コンテンツがある場合のみ、末尾に改行を追加して次の追記に備える
        if full_log_content:
            full_log_content += "\n\n"
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write(full_log_content)
        print(f"--- [ChatGPT Importer] Wrote {len(log_entries)} entries to log.txt ---")

        # 5b. SystemPrompt.txt
        # 仕様: 最初のメッセージがユーザー発言であった場合のみ書き込む
        first_message = thread_messages[0]
        if first_message.get("author", {}).get("role") == "user":
            system_prompt_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "SystemPrompt.txt")
            with open(system_prompt_path, "w", encoding="utf-8") as f:
                f.write(first_user_prompt or "") # first_user_promptがNoneのケースもカバー
            print(f"--- [ChatGPT Importer] Wrote first user prompt to SystemPrompt.txt ---")
        else:
            print(f"--- [ChatGPT Importer] First message was not from user, SystemPrompt.txt left empty. ---")

        # 5c. room_config.json の更新
        config_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "room_config.json")
        with open(config_path, "r+", encoding="utf-8") as f:
            config = json.load(f)
            config["room_name"] = room_name
            config["user_display_name"] = user_display_name if user_display_name else "ユーザー"
            config["description"] = f"ChatGPTからインポートされた会話ログです。\nOriginal Title: {conversation_data.get('title', 'N/A')}"
            f.seek(0)
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.truncate()
        print(f"--- [ChatGPT Importer] Updated room_config.json ---")

        print(f"--- [ChatGPT Importer] Successfully imported conversation to room: {safe_folder_name} ---")
        return safe_folder_name

    except Exception as e:
        print(f"[ChatGPT Importer] An unexpected error occurred during import: {e}")
        traceback.print_exc()
        return None
