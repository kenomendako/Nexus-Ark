# room_manager.py

import os
import json
import re
import shutil
import traceback
import datetime
import threading
import time
from typing import Optional, List, Tuple
from send2trash import send2trash
import constants

# スレッドセーフなファイル操作のためのロック
_room_config_lock = threading.Lock()

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
            os.path.join(base_path, "audio_cache"),
            os.path.join(base_path, "generated_images"),
            spaces_dir,
            os.path.join(spaces_dir, "images"),
            cache_dir,
            os.path.join(base_path, constants.LOGS_DIR_NAME), # [NEW] チャットログ分割
            os.path.join(base_path, "log_archives", "processed"),
            os.path.join(base_path, "log_import_source", "processed"),
            os.path.join(base_path, "memory"),
            os.path.join(base_path, "private"),
            os.path.join(base_path, constants.NOTES_DIR_NAME),
            os.path.join(base_path, constants.NOTES_DIR_NAME, "archives")
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
            os.path.join(backup_base_dir, "configs"),
            os.path.join(backup_base_dir, "research_notes"),
            os.path.join(backup_base_dir, "creative_notes"),
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

        # レガシーなノートファイルを新しい場所へ移動
        _migrate_legacy_notes(room_name)

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


def _restore_room_config_from_backup(folder_name: str) -> bool:
    """最も新しいバックアップからroom_config.jsonを復元する。"""
    backup_dir = os.path.join(constants.ROOMS_DIR, folder_name, "backups", "configs")
    config_file = os.path.join(constants.ROOMS_DIR, folder_name, "room_config.json")
    
    if not os.path.isdir(backup_dir):
        return False

    try:
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.endswith(".bak")],
            key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)),
            reverse=True
        )
        if not backups:
            return False

        latest_backup = os.path.join(backup_dir, backups[0])
        print(f"--- [自己修復] 破損したルーム設定をバックアップ '{backups[0]}' から復元します ---")
        shutil.copy2(latest_backup, config_file)
        return True
    except Exception as e:
        print(f"!!! エラー: バックアップからの復元に失敗しました ({folder_name}): {e}")
        return False

def get_room_config(folder_name: str) -> Optional[dict]:
    """
    指定されたフォルダ名のルーム設定ファイル(room_config.json)を安全に読み込み、辞書として返す。
    破損している場合はバックアップからの復元を試みる。
    """
    if not folder_name:
        return None

    config_file = os.path.join(constants.ROOMS_DIR, folder_name, "room_config.json")
    
    def _read_json():
        if os.path.exists(config_file):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    content = f.read()
                if not content.strip():
                    raise json.JSONDecodeError("File is empty", "", 0)
                return json.loads(content)
            except (json.JSONDecodeError, IOError) as e:
                print(f"警告: ルーム '{folder_name}' の設定ファイルが破損しています: {e}")
                return None
        return None

    # 1. 通常の読み込み試行
    config = _read_json()
    if config is not None:
        return config

    # 2. 破損している場合、復元を試みる
    if os.path.exists(config_file):
        if _restore_room_config_from_backup(folder_name):
            return _read_json()
            
    return None


def get_room_files_paths(room_name: str) -> Optional[Tuple[str, str, Optional[str], str, str, str]]:
    """
    ルームの主要ファイルパスを取得する。
    
    Returns:
        (log_file, system_prompt_file, profile_image_path, memory_main_path, notepad_path, research_notes_path)
    """
    if not room_name or not ensure_room_files(room_name): return None, None, None, None, None, None
    base_path = os.path.join(constants.ROOMS_DIR, room_name)
    current_month = datetime.datetime.now().strftime("%Y-%m")
    log_file = os.path.join(base_path, constants.LOGS_DIR_NAME, f"{current_month}.txt")
    system_prompt_file = os.path.join(base_path, "SystemPrompt.txt")
    profile_image_path = os.path.join(base_path, constants.PROFILE_IMAGE_FILENAME)
    # memory.txt へのパスを memory/memory_main.txt に変更
    memory_main_path = os.path.join(base_path, "memory", "memory_main.txt")
    
    # ノート類を notes/ フォルダへ集約 (v2026-02-02)
    notes_base = os.path.join(base_path, constants.NOTES_DIR_NAME)
    notepad_path = os.path.join(notes_base, constants.NOTEPAD_FILENAME)
    research_notes_path = os.path.join(notes_base, constants.RESEARCH_NOTES_FILENAME)
    
    if not os.path.exists(profile_image_path): profile_image_path = None
    return log_file, system_prompt_file, profile_image_path, memory_main_path, notepad_path, research_notes_path

def get_creative_notes_path(room_name: str) -> str:
    """創作ノートの最新ファイルパスを返す"""
    return os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME, constants.CREATIVE_NOTES_FILENAME)

def _migrate_legacy_notes(room_name: str):
    """ルーム直下の古いノートファイルを notes/ へ移動する"""
    base_path = os.path.join(constants.ROOMS_DIR, room_name)
    notes_dir = os.path.join(base_path, constants.NOTES_DIR_NAME)
    
    legacy_files = [
        constants.NOTEPAD_FILENAME,
        constants.RESEARCH_NOTES_FILENAME,
        constants.CREATIVE_NOTES_FILENAME
    ]
    
    for filename in legacy_files:
        old_path = os.path.join(base_path, filename)
        new_path = os.path.join(notes_dir, filename)
        if os.path.exists(old_path) and not os.path.exists(new_path):
            print(f"--- [移行] {filename} を {constants.NOTES_DIR_NAME}/ へ移動します ---")
            shutil.move(old_path, new_path)

def get_note_files(room_name: str, note_type: str) -> List[str]:
    """
    指定されたノート種別のファイルリスト（最新 + アーカイブ）を返す。
    最新がリストの先頭になります。
    note_type: 'notepad', 'research', 'creative'
    """
    if not room_name: return []
    notes_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME)
    archives_dir = os.path.join(notes_dir, "archives")
    
    filename_map = {
        'notepad': constants.NOTEPAD_FILENAME,
        'research': constants.RESEARCH_NOTES_FILENAME,
        'creative': constants.CREATIVE_NOTES_FILENAME
    }
    base_filename = filename_map.get(note_type)
    if not base_filename: return []
    
    files = []
    # 最新
    main_file = os.path.join(notes_dir, base_filename)
    if os.path.exists(main_file):
        files.append(base_filename)
        
    # アーカイブ
    if os.path.exists(archives_dir):
        # archive_YYYYMMDD_HHMMSS_filename.md のような形式を想定
        prefix = f"archive_"
        suffix = f"_{base_filename}"
        archives = [f for f in os.listdir(archives_dir) if f.startswith(prefix) and f.endswith(suffix)]
        # 新しい順にソート
        archives.sort(reverse=True)
        files.extend(archives)
        
    return files

def archive_large_note(room_name: str, filename: str) -> bool:
    """
    指定されたノートファイルが上限サイズを超えている場合、
    内容をアーカイブフォルダへ移動（分割）する。
    """
    if not room_name or not filename: return False
    
    notes_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME)
    archives_dir = os.path.join(notes_dir, "archives")
    file_path = os.path.join(notes_dir, filename)
    
    if not os.path.exists(file_path):
        return False
        
    if os.path.getsize(file_path) < constants.NOTES_MAX_SIZE_BYTES:
        return False
        
    try:
        os.makedirs(archives_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_name = f"archive_{timestamp}_{filename}"
        archive_path = os.path.join(archives_dir, archive_name)
        
        print(f"--- [アーカイブ] {filename} が制限を超えたため、{archive_name} へ退避します ---")
        shutil.copy2(file_path, archive_path)
        
        # 元ファイルをクリア（またはヘッダーだけ残すなどの処理も検討可能だが、基本は空にする）
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("")
            
        return True
    except Exception as e:
        print(f"!!! アーカイブ処理失敗: {e}")
        return False

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

    log_file_path, _, _, _, _, _ = get_room_files_paths(main_room_name)
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
        'secret_diary': ("secret_diary.txt", os.path.join(constants.ROOMS_DIR, room_name, "private", "secret_diary.txt")),
        'room_config': ("room_config.json", os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")),
        'creative_notes': (constants.CREATIVE_NOTES_FILENAME, get_creative_notes_path(room_name)),
        'research_notes': (constants.RESEARCH_NOTES_FILENAME, os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME, constants.RESEARCH_NOTES_FILENAME)),
        'notepad': (constants.NOTEPAD_FILENAME, os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME, constants.NOTEPAD_FILENAME))
    }
    folder_map = {
        'log': "logs", 'memory': "memories", 'notepad': "notepads",
        'world_setting': "world_settings", 'system_prompt': "system_prompts",
        'core_memory': "core_memories", 'secret_diary': "secret_diaries",
        'room_config': "configs",
        'creative_notes': "creative_notes",
        'research_notes': "research_notes"
    }

    if file_type not in file_map:
        error_msg = f"致命的エラー: 不明なバックアップファイルタイプです: {file_type}"
        print(error_msg)
        raise ValueError(error_msg)

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
        error_msg = f"!!! 致命的エラー: バックアップ作成中に予期せぬエラーが発生しました ({file_type}): {e}"
        print(error_msg)
        traceback.print_exc()
        raise IOError(error_msg) from e

def update_room_config(room_name: str, updates: dict) -> bool:
    """
    ルーム設定ファイル(room_config.json)を安全に更新する。
    - threading.Lock による排他制御
    - ユニークな一時ファイルによるアトミック書き込み
    - 変更がない場合は書き込まない
    """
    if not room_name: return False

    with _room_config_lock:
        config = get_room_config(room_name)
        if not config:
            # Configがない場合（または破損して復旧不能な場合）のフォールバック
            if not ensure_room_files(room_name):
                print(f"エラー: ルーム '{room_name}' の初期化に失敗しました。")
                return False
            config = get_room_config(room_name)
            if not config: return False

        # 変更検知のために元の状態をコピー
        import copy
        old_config = copy.deepcopy(config)

        if "override_settings" not in config:
            config["override_settings"] = {}

        root_keys = ["room_name", "user_display_name", "description", "version", "last_sent_scenery_image"]
        
        overrides = config["override_settings"]
        for k, v in updates.items():
            if k in root_keys:
                config[k] = v
            elif k == "override_settings" and isinstance(v, dict):
                overrides.update(v)
            else:
                # ネストされた辞書の場合のマージ考慮 (現状は単純な上書き)
                overrides[k] = v

        # 変更があるかチェック
        if config == old_config:
            return "no_change"

        config["last_updated"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        config["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # 保存前にバックアップを作成
        try:
            create_backup(room_name, 'room_config')
        except Exception as e:
            print(f"Warning: Backup creation failed for room_config: {e}")

        # アトミックな書き込み処理 (ユニークな一時ファイル名を使用)
        config_file = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        # thread_id と pid を含めて衝突を避ける
        temp_file = f"{config_file}.{os.getpid()}.{threading.get_ident()}.tmp"
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                os.replace(temp_file, config_file)
                print(f"ルーム '{room_name}' の設定を更新しました。")
                return True
            except PermissionError:
                if attempt < max_retries - 1:
                    time.sleep(0.1)
                else:
                    raise
            except Exception as e:
                print(f"ルーム '{room_name}' の設定保存エラー: {e}")
                if os.path.exists(temp_file):
                    try: os.remove(temp_file)
                    except: pass
                return False
        return False

def save_room_override_settings(room_name: str, settings: dict) -> bool:
    """
    [後方互換用] ルーム個別の設定を保存する。
    内部で新しい update_room_config を呼び出すように変更。
    """
    return update_room_config(room_name, {"override_settings": settings})


def delete_room(room_name: str) -> bool:
    """
    指定されたルームのディレクトリをWindowsのゴミ箱に移動する。
    完全削除ではなく、ゴミ箱からの復元が可能。
    
    Args:
        room_name: 削除するルームのフォルダ名
        
    Returns:
        bool: ゴミ箱への移動に成功した場合はTrue、失敗した場合はFalse
    """
    if not room_name:
        print("エラー: ルーム名が指定されていません。")
        return False
    
    # セキュリティチェック: パストラバーサルを防止
    if ".." in room_name or "/" in room_name or "\\" in room_name:
        print(f"エラー: 不正なルーム名です: {room_name}")
        return False
    
    room_path = os.path.join(constants.ROOMS_DIR, room_name)
    
    if not os.path.exists(room_path):
        print(f"警告: ルーム '{room_name}' は存在しません。")
        return False
    
    if not os.path.isdir(room_path):
        print(f"エラー: '{room_name}' はディレクトリではありません。")
        return False
    
    try:
        # ディレクトリ全体をゴミ箱に移動（復元可能）
        send2trash(room_path)
        print(f"--- ルーム '{room_name}' をゴミ箱に移動しました ---")
        return True
    except PermissionError as e:
        print(f"エラー: ルーム '{room_name}' の削除に失敗しました（権限エラー）: {e}")
        traceback.print_exc()
        return False
    except Exception as e:
        print(f"エラー: ルーム '{room_name}' の削除中に予期せぬエラーが発生しました: {e}")
        traceback.print_exc()
        return False


# ===== 表情差分設定管理 =====

def get_expressions_config(room_name: str) -> dict:
    """
    ルームの表情設定を読み込む。
    expressions.json が存在しない場合はデフォルト設定を返す。
    
    Args:
        room_name: ルームのフォルダ名
        
    Returns:
        表情設定の辞書
    """
    if not room_name:
        return _get_default_expressions_config()
    
    expressions_file = os.path.join(constants.ROOMS_DIR, room_name, constants.EXPRESSIONS_FILE)
    
    if os.path.exists(expressions_file):
        try:
            with open(expressions_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"警告: ルーム '{room_name}' の表情設定ファイルが読めません: {e}")
    
    return _get_default_expressions_config()


def _get_default_expressions_config() -> dict:
    """デフォルトの表情設定を返す"""
    return {
        "expressions": constants.DEFAULT_EXPRESSIONS.copy(),
        "default_expression": "neutral"
    }


def save_expressions_config(room_name: str, config: dict) -> bool:
    """
    ルームの表情設定を保存する。
    
    Args:
        room_name: ルームのフォルダ名
        config: 保存する設定辞書
        
    Returns:
        保存に成功した場合はTrue
    """
    if not room_name:
        return False
    
    expressions_file = os.path.join(constants.ROOMS_DIR, room_name, constants.EXPRESSIONS_FILE)
    
    try:
        with open(expressions_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"--- 表情設定を保存しました: {room_name} ---")
        return True
    except Exception as e:
        print(f"エラー: 表情設定の保存に失敗しました: {e}")
        return False


def get_available_expression_files(room_name: str) -> dict:
    """
    avatar/ディレクトリ内の利用可能な表情ファイルを取得する。
    
    Args:
        room_name: ルームのフォルダ名
        
    Returns:
        {表情名: ファイルパス} の辞書
    """
    if not room_name:
        return {}
    
    avatar_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.AVATAR_DIR)
    
    if not os.path.isdir(avatar_dir):
        return {}
    
    # サポートする拡張子
    image_exts = [".png", ".jpg", ".jpeg", ".webp"]
    video_exts = [".mp4", ".webm", ".gif"]
    all_exts = image_exts + video_exts
    
    available = {}
    
    # 【v2】登録済みの表情リストでフィルタリング
    # これにより、UIから削除された表情がプロンプトに残るのを防ぐ
    expressions_config = get_expressions_config(room_name)
    registered_names = ["idle", "thinking"] + expressions_config.get("expressions", []) + constants.DEFAULT_EXPRESSIONS
    registered_names = list(set(registered_names)) # 重複除去
    
    try:
        if os.path.exists(avatar_dir):
            for filename in os.listdir(avatar_dir):
                name, ext = os.path.splitext(filename)
                if ext.lower() in all_exts:
                    # 登録リストにある場合のみ採用
                    if name in registered_names:
                        # 同じ表情名で動画と静止画がある場合、動画を優先
                        if name not in available or ext.lower() in video_exts:
                            available[name] = os.path.join(avatar_dir, filename)
    except Exception as e:
        print(f"警告: アバターディレクトリの読み取りエラー: {e}")
    
    return available


def initialize_expressions_file(room_name: str) -> bool:
    """
    ルームに expressions.json が存在しない場合、デフォルト設定をコピーする。
    新規ルーム作成時に呼び出される。
    
    Args:
        room_name: ルームのフォルダ名
        
    Returns:
        初期化に成功した場合はTrue
    """
    if not room_name:
        return False
    
    expressions_file = os.path.join(constants.ROOMS_DIR, room_name, constants.EXPRESSIONS_FILE)
    
    # 既に存在する場合はスキップ
    if os.path.exists(expressions_file):
        return True
    
    # サンプルファイルからコピー
    sample_file = os.path.join(constants.SAMPLE_PERSONA_DIR, constants.EXPRESSIONS_FILE)
    
    if os.path.exists(sample_file):
        try:
            shutil.copy2(sample_file, expressions_file)
            print(f"--- 表情設定ファイルを初期化しました: {room_name} ---")
            return True
        except Exception as e:
            print(f"警告: 表情設定ファイルのコピーに失敗: {e}")
    
    # サンプルがない場合はデフォルト設定を生成
    return save_expressions_config(room_name, _get_default_expressions_config())
