import ijson
import json
import os
import traceback
import zipfile
import tempfile
import datetime
from typing import Optional, Dict, Any, List, Union

import room_manager
import constants

def resolve_conversations_file_path(file_path: str) -> str:
    """
    ファイルパスを受け取り、ZIPであれば解凍して conversations.json のパスを返す。
    ZIPでない場合はそのまま返す。
    注意: 解凍された一時ファイルは明示的に削除されないため、OSのクリーンアップに依存するか、
    プロセス終了時に削除される場所を使用することを推奨。
    """
    if not file_path.lower().endswith('.zip'):
        return file_path

    try:
        # 解凍先の一時ディレクトリを作成
        temp_dir = tempfile.mkdtemp()
        print(f"[ChatGPT Importer] Extracting ZIP file to: {temp_dir}")
        
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
            
        # conversations.json を探す
        json_path = os.path.join(temp_dir, 'conversations.json')
        if os.path.exists(json_path):
            print(f"[ChatGPT Importer] Found conversations.json at: {json_path}")
            return json_path
            
        # 見つからない場合、ルートにある .json ファイルを探索してみる
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                if file.endswith('.json'):
                     potential_path = os.path.join(root, file)
                     print(f"[ChatGPT Importer] conversations.json not found, using alternative JSON: {potential_path}")
                     return potential_path
        
        print(f"[ChatGPT Importer] WARNING: No JSON file found in ZIP.")
        return file_path # 失敗したので元のパスを返す
        
    except zipfile.BadZipFile:
        print(f"[ChatGPT Importer] Error: File is not a valid ZIP file.")
        return file_path
    except Exception as e:
        print(f"[ChatGPT Importer] Error extracting ZIP: {e}")
        return file_path

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
            # create_time を含める
            message_data = message.copy()
            if "create_time" not in message_data:
                 message_data["create_time"] = message.get("create_time", 0.0)
            thread.append(message_data)

        # ほとんどの会話は分岐しないため、最初の子供をたどる
        children = node.get("children", [])
        if children:
            current_id = children[0]
        else:
            break # スレッドの終わり
    return thread

def import_from_chatgpt_export(file_path: str, conversation_id: Any, room_name: str, user_display_name: str) -> Optional[str]:
    """
    ChatGPTのエクスポートファイルから指定された会話をインポートし、新しいルームを作成する。

    Args:
        file_path: conversations.json のパス
        conversation_id: インポートする会話のID（単一文字列 または 文字列のリスト）
        room_name: 新しいルームの表示名
        user_display_name: ユーザーの表示名

    Returns:
        成功した場合は新しいルームのフォルダ名、失敗した場合はNone
    """
    
    # IDをリストリスト形式に統一
    target_ids = []
    if isinstance(conversation_id, str):
        target_ids.append(conversation_id)
    elif isinstance(conversation_id, list):
        target_ids.extend(conversation_id)
    else:
         print(f"[ChatGPT Importer] ERROR: Invalid conversation_id type: {type(conversation_id)}")
         return None

    print(f"--- [ChatGPT Importer] Starting import for conversation_ids: {target_ids} ---")
    
    try:
        # 0. ファイルパスの解決 (ZIP対応)
        resolved_file_path = resolve_conversations_file_path(file_path)
        
        all_thread_messages = []
        original_title = "N/A"

        # 1. 指定された各IDについて、データを検索して収集
        for cid in target_ids:
            conversation_data = _find_conversation_data(resolved_file_path, cid)
            if not conversation_data:
                print(f"[ChatGPT Importer] WARNING: Conversation with ID '{cid}' not found in '{file_path}'. Skipping.")
                continue
            
            # タイトルは（複数ある場合）最初に見つかったものをベースにする（あるいは結合する手もあるが今回は最初優先）
            if original_title == "N/A":
                original_title = conversation_data.get('title', 'N/A')

            # 2. 会話スレッドを再構築
            mapping = conversation_data.get("mapping", {})
            thread_messages = _reconstruct_thread(mapping, cid)
            if thread_messages:
                all_thread_messages.extend(thread_messages)
            else:
                 print(f"[ChatGPT Importer] WARNING: No valid messages found in conversation '{cid}'.")

        if not all_thread_messages:
            print(f"[ChatGPT Importer] ERROR: No valid messages found in any of the specified conversations.")
            return None

        # 3. メッセージを create_time でソート (古い順)
        # create_time がない場合(None)は 0.0 として扱い先頭にする
        all_thread_messages.sort(key=lambda x: x.get("create_time") or 0.0)

        # 4. ルームのフォルダ名と基本ファイルを作成
        safe_folder_name = room_manager.generate_safe_folder_name(room_name)
        if not room_manager.ensure_room_files(safe_folder_name):
            print(f"[ChatGPT Importer] ERROR: Failed to create room files for '{safe_folder_name}'.")
            return None
        print(f"--- [ChatGPT Importer] Created room skeleton: {safe_folder_name} ---")

        # 5. ログ形式への変換とSystemPromptの準備
        log_entries = []
        first_user_prompt = None

        for message in all_thread_messages:
            author_role = message.get("author", {}).get("role")
            content_parts = message.get("content", {}).get("parts", [])

            # content.partsが空、またはNoneの場合をスキップ
            if not content_parts or not isinstance(content_parts, list):
                continue

            # content.parts の中身が文字列でない場合も考慮
            text_content = "".join(str(p) for p in content_parts if isinstance(p, str) and p.strip()).strip()

            if not text_content:
                continue

            # 日付情報を付与 (ログの月次分割などで必要になるため)
            create_time = message.get("create_time", 0.0)
            if create_time:
                # タイムゾーンは考慮せず、単純な変換とする (またはJST変換など)
                # ijsonは数値をDecimalで返すことがあるため、floatに変換
                dt = datetime.datetime.fromtimestamp(float(create_time))
                timestamp_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                # Nexus Arkの標準的なタイムスタンプ付きフォーマットに合わせる
                # メッセージの先頭に付与することで、マイグレーションロジックが日付を認識できるようにする
                text_content = f"{timestamp_str}\n{text_content}"

            if author_role == "user":
                log_entries.append(f"## USER:user\n{text_content}")
                if first_user_prompt is None:
                    first_user_prompt = text_content
            elif author_role == "assistant":
                log_entries.append(f"## AGENT:{safe_folder_name}\n{text_content}")

        # 6. ファイルへの書き込み
        # 6a. log.txt
        log_file_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "log.txt")
        full_log_content = "\n\n".join(log_entries)
        # コンテンツがある場合のみ、末尾に改行を追加して次の追記に備える
        if full_log_content:
            full_log_content += "\n\n"
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write(full_log_content)
        print(f"--- [ChatGPT Importer] Wrote {len(log_entries)} entries to log.txt ---")

        # 6b. SystemPrompt.txt
        # 仕様: ソート後の最初のメッセージがユーザー発言であった場合のみ書き込む
        if all_thread_messages and all_thread_messages[0].get("author", {}).get("role") == "user":
             # 念のため first_user_prompt が設定されているか確認（ループ内で設定されるはずだが）
            system_prompt_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "SystemPrompt.txt")
            with open(system_prompt_path, "w", encoding="utf-8") as f:
                f.write(first_user_prompt or "") 
            print(f"--- [ChatGPT Importer] Wrote first user prompt to SystemPrompt.txt ---")
        else:
            print(f"--- [ChatGPT Importer] First message was not from user, SystemPrompt.txt left empty. ---")

        # 6c. room_config.json の更新
        config_path = os.path.join(constants.ROOMS_DIR, safe_folder_name, "room_config.json")
        with open(config_path, "r+", encoding="utf-8") as f:
            config = json.load(f)
            config["room_name"] = room_name
            config["user_display_name"] = user_display_name if user_display_name else "ユーザー"
            config["description"] = f"ChatGPTからインポートされた会話ログです。\nOriginal Title: {original_title} (+{len(target_ids)-1} threads)"
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
