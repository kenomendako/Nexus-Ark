# room_manager.py

import os
import json
import re
import shutil
import traceback
import datetime
from typing import Optional, List, Tuple
import constants

def generate_safe_folder_name(room_name: str) -> str:
    """
    ユーザーが入力したルーム名から、安全でユニークなフォルダ名を生成する。
    仕様:
    - 空白をアンダースコア `_` に置換
    - OSのファイル名として不正な文字 (`\\/:*?\"<>|`) を除去
    - 重複をチェックし、末尾に `_2`, `_3` ... と連番を付与
    """
    # 1. 空白をアンダースコアに置換
    safe_name = room_name.replace(" ", "_")

    # 2. OSのファイル名として不正な文字を除去
    safe_name = re.sub(r'[\\/:*?"<>|]', '', safe_name)

    # 3. 重複をチェックし、連番を付与
    base_path = constants.ROOMS_DIR
    if not os.path.exists(base_path):
        os.makedirs(base_path)

    final_name = safe_name
    counter = 2
    while os.path.exists(os.path.join(base_path, final_name)):
        final_name = f"{safe_name}_{counter}"
        counter += 1

    return final_name

def ensure_room_files(room_name: str) -> bool:
    """
    指定されたルーム名のディレクトリと、その中に必要なファイル群を生成・保証する。
    """
    if not room_name or not isinstance(room_name, str) or not room_name.strip(): return False
    if ".." in room_name or "/" in room_name or "\\" in room_name: return False
    try:
        base_path = os.path.join(constants.ROOMS_DIR, room_name)
        spaces_dir = os.path.join(base_path, "spaces")
        cache_dir = os.path.join(base_path, "cache")

        # 必須ディレクトリのリスト
        dirs_to_create = [
            base_path,
            os.path.join(base_path, "attachments"),
            os.path.join(base_path, "audio_cache"), # ← この行を追加
            os.path.join(base_path, "generated_images"),
            spaces_dir,
            os.path.join(spaces_dir, "images"),
            cache_dir,
            os.path.join(base_path, "log_archives", "processed"),
            os.path.join(base_path, "log_import_source", "processed"),
            os.path.join(base_path, "memory"),
            os.path.join(base_path, "memory", "backups"), # <-- この行を追加
            os.path.join(base_path, "private")
        ]
        # ▼▼▼【ここから下のブロックをまるごと追加】▼▼▼
        # バックアップ用のサブディレクトリを追加
        backup_base_dir = os.path.join(base_path, "backups")
        backup_sub_dirs = [
            os.path.join(backup_base_dir, "logs"),
            os.path.join(backup_base_dir, "memories"),
            os.path.join(backup_base_dir, "notepads"),
            os.path.join(backup_base_dir, "world_settings"),
            os.path.join(backup_base_dir, "system_prompts"),
            os.path.join(backup_base_dir, "core_memories"),
            os.path.join(backup_base_dir, "secret_diaries"),
        ]
        dirs_to_create.append(backup_base_dir)
        dirs_to_create.extend(backup_sub_dirs)
        # ▲▲▲【追加はここまで】▲▲▲

        for path in dirs_to_create:
            os.makedirs(path, exist_ok=True)

        # テキストベースのファイル
        world_settings_content = "## 共有リビング\n\n### リビング\n広々としたリビングルーム。大きな窓からは柔らかな光が差し込み、快適なソファが置かれている。\n"

        memory_template_content = (
            "## 永続記憶 (Permanent)\n"
            "### 自己同一性 (Self Identity)\n\n\n"
            "## 日記 (Diary)\n"
            f"### {datetime.datetime.now().strftime('%Y-%m-%d')}\n\n\n"
            "## アーカイブ要約 (Archive Summary)\n"
        )

        text_files_to_create = {
            os.path.join(base_path, "SystemPrompt.txt"): "",
            os.path.join(base_path, "log.txt"): "",
            os.path.join(base_path, constants.NOTEPAD_FILENAME): "",
            os.path.join(base_path, "current_location.txt"): "リビング",
            os.path.join(spaces_dir, "world_settings.txt"): world_settings_content,
            os.path.join(base_path, "memory", "memory_main.txt"): memory_template_content, # <-- パス変更
            os.path.join(base_path, "private", "secret_diary.txt"): "" # <-- 追加
        }
        for file_path, content in text_files_to_create.items():
            if not os.path.exists(file_path):
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)

        # JSONベースのファイル
        json_files_to_create = {
            os.path.join(cache_dir, "scenery.json"): {},
            os.path.join(cache_dir, "image_prompts.json"): {"prompts": {}},
        }
        for file_path, content in json_files_to_create.items():
            if not os.path.exists(file_path):
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(content, f, indent=2, ensure_ascii=False)

        # room_config.json の設定（後方互換性も考慮）
        config_file_path = os.path.join(base_path, "room_config.json")
        if not os.path.exists(config_file_path) or os.path.getsize(config_file_path) == 0:
            default_config = {
                "room_name": room_name, # デフォルトはフォルダ名
                "user_display_name": "ユーザー",
                "description": "",
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "version": 1
            }
            with open(config_file_path, "w", encoding="utf-8") as f:
                json.dump(default_config, f, indent=2, ensure_ascii=False)

        return True
    except Exception as e:
        print(f"ルーム '{room_name}' ファイル作成/確認エラー: {e}"); traceback.print_exc()
        return False

def get_room_list_for_ui() -> List[Tuple[str, str]]:
    """
    UIのドロップダウン表示用に、有効なルームのリストを `[('表示名', 'フォルダ名'), ...]` の形式で返す。
    room_config.json が存在するフォルダのみを有効なルームとみなす。
    """
    rooms_dir = constants.ROOMS_DIR
    if not os.path.exists(rooms_dir) or not os.path.isdir(rooms_dir):
        return []

    valid_rooms = []
    for folder_name in os.listdir(rooms_dir):
        room_path = os.path.join(rooms_dir, folder_name)
        if os.path.isdir(room_path):
            config_file = os.path.join(room_path, "room_config.json")
            if os.path.exists(config_file):
                try:
                    with open(config_file, "r", encoding="utf-8") as f:
                        config = json.load(f)
                        display_name = config.get("room_name", folder_name)
                        valid_rooms.append((display_name, folder_name))
                except (json.JSONDecodeError, IOError) as e:
                    print(f"警告: ルーム '{folder_name}' の設定ファイルが読めません: {e}")

    # 表示名でソートして返す
    return sorted(valid_rooms, key=lambda x: x[0])


def get_room_config(folder_name: str) -> Optional[dict]:
    """
    指定されたフォルダ名のルーム設定ファイル(room_config.json)を読み込み、辞書として返す。
    見つからない場合はNoneを返す。
    """
    if not folder_name:
        return None

    config_file = os.path.join(constants.ROOMS_DIR, folder_name, "room_config.json")
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"警告: ルーム '{folder_name}' の設定ファイルが読めません: {e}")
            return None
    return None


def get_room_files_paths(room_name: str) -> Optional[Tuple[str, str, Optional[str], str, str]]:
    if not room_name or not ensure_room_files(room_name): return None, None, None, None, None
    base_path = os.path.join(constants.ROOMS_DIR, room_name)
    log_file = os.path.join(base_path, "log.txt")
    system_prompt_file = os.path.join(base_path, "SystemPrompt.txt")
    profile_image_path = os.path.join(base_path, constants.PROFILE_IMAGE_FILENAME)
    # memory.txt へのパスを memory/memory_main.txt に変更
    memory_main_path = os.path.join(base_path, "memory", "memory_main.txt")
    notepad_path = os.path.join(base_path, constants.NOTEPAD_FILENAME)
    if not os.path.exists(profile_image_path): profile_image_path = None
    return log_file, system_prompt_file, profile_image_path, memory_main_path, notepad_path

def get_world_settings_path(room_name: str):
    if not room_name or not ensure_room_files(room_name): return None
    return os.path.join(constants.ROOMS_DIR, room_name, "spaces", "world_settings.txt")

def get_all_personas_in_log(main_room_name: str, api_history_limit_key: str) -> list[str]:
    """
    指定されたルームのログを解析し、指定された履歴範囲内に登場するすべての
    ペルソナ名（ユーザー含む）のユニークなリストを返す。
    """
    import utils # 循環参照を避けるため、ここでローカルインポート
    if not main_room_name:
        return []

    log_file_path, _, _, _, _ = get_room_files_paths(main_room_name)
    if not log_file_path or not os.path.exists(log_file_path):
        # ログファイルがない場合、ルーム名自体をペルソナと見なす
        # これは、room_config.json の main_persona_name を参照する将来の実装への布石
        return [main_room_name]

    # utils.load_chat_log を呼び出す
    full_log = utils.load_chat_log(log_file_path)

    # 履歴制限を適用
    limit = constants.API_HISTORY_LIMIT_OPTIONS.get(api_history_limit_key)
    if limit is not None and limit.isdigit():
        display_turns = int(limit)
        limited_log = full_log[-(display_turns * 2):]
    else: # "全ログ" or other cases
        limited_log = full_log

    # 登場ペルソナを収集
    personas = set()
    for message in limited_log:
        responder = message.get("responder")
        if responder:
            personas.add(responder)

    # メインのペルソナがリストに含まれていることを保証する
    # ここも将来的には room_config.json から取得する
    personas.add(main_room_name)

    # "ユーザー"はペルソナではないので除外
    return sorted([p for p in list(personas) if p != "ユーザー"])


# ▼▼▼【ここから下のブロックを、ファイルの末尾にまるごと追加してください】▼▼▼
def create_backup(room_name: str, file_type: str) -> Optional[str]:
    """
    指定されたファイルタイプのバックアップを作成し、古いバックアップをローテーションする汎用関数。
    成功した場合はバックアップパスを、失敗した場合はNoneを返す。
    """
    import config_manager
    if not room_name:
        return None

    file_map = {
        'log': ("log.txt", os.path.join(constants.ROOMS_DIR, room_name, "log.txt")),
        'memory': ("memory_main.txt", os.path.join(constants.ROOMS_DIR, room_name, "memory", "memory_main.txt")),
        'notepad': (constants.NOTEPAD_FILENAME, os.path.join(constants.ROOMS_DIR, room_name, constants.NOTEPAD_FILENAME)),
        'world_setting': ("world_settings.txt", get_world_settings_path(room_name)),
        'system_prompt': ("SystemPrompt.txt", os.path.join(constants.ROOMS_DIR, room_name, "SystemPrompt.txt")),
        'core_memory': ("core_memory.txt", os.path.join(constants.ROOMS_DIR, room_name, "core_memory.txt")),
        'secret_diary': ("secret_diary.txt", os.path.join(constants.ROOMS_DIR, room_name, "private", "secret_diary.txt"))
    }
    folder_map = {
        'log': "logs", 'memory': "memories", 'notepad': "notepads",
        'world_setting': "world_settings", 'system_prompt': "system_prompts",
        'core_memory': "core_memories", 'secret_diary': "secret_diaries"
    }

    if file_type not in file_map:
        print(f"警告: 不明なバックアップファイルタイプです: {file_type}")
        return None

    original_filename, source_path = file_map[file_type]
    backup_subdir = folder_map[file_type]
    backup_dir = os.path.join(constants.ROOMS_DIR, room_name, "backups", backup_subdir)

    try:
        # ディレクトリの存在を確認・作成
        os.makedirs(backup_dir, exist_ok=True)

        # ソースファイルが存在しない場合はバックアップを作成しない
        if not source_path or not os.path.exists(source_path):
            print(f"情報: バックアップ対象ファイルが見つかりません（初回作成時など）: {source_path}")
            return None

        # バックアップファイル名の生成
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"{timestamp}_{original_filename}.bak"
        backup_path = os.path.join(backup_dir, backup_filename)

        # バックアップの実行
        shutil.copy2(source_path, backup_path)
        print(f"--- バックアップを作成しました: {backup_path} ---")

        # ローテーション処理
        rotation_count = config_manager.CONFIG_GLOBAL.get("backup_rotation_count", 10)
        existing_backups = sorted(
            [f for f in os.listdir(backup_dir) if f.endswith(".bak")],
            key=lambda f: os.path.getmtime(os.path.join(backup_dir, f))
        )

        if len(existing_backups) > rotation_count:
            files_to_delete = existing_backups[:len(existing_backups) - rotation_count]
            for f_del in files_to_delete:
                os.remove(os.path.join(backup_dir, f_del))
                print(f"--- 古いバックアップを削除しました: {f_del} ---")

        return backup_path

    except Exception as e:
        print(f"!!! エラー: バックアップ作成中にエラーが発生しました ({file_type}): {e}")
        traceback.print_exc()
        return None
# ▲▲▲【追加はここまで】▲▲▲
