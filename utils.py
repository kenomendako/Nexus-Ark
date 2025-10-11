# utils.py (完全最終版)

import datetime
import os
import re
import traceback
import html
from typing import List, Dict, Optional, Tuple, Union
import constants
import sys
import psutil
from pathlib import Path
import json
import time
import uuid
from bs4 import BeautifulSoup
import io
import contextlib

_model_token_limits_cache: Dict[str, Dict[str, int]] = {}
LOCK_FILE_PATH = Path.home() / ".nexus_ark.global.lock"

def acquire_lock() -> bool:
    print("--- グローバル・ロックの取得を試みます ---")
    try:
        if not LOCK_FILE_PATH.exists():
            _create_lock_file()
            print("--- ロックを取得しました (新規作成) ---")
            return True
        with open(LOCK_FILE_PATH, "r", encoding="utf-8") as f:
            lock_info = json.load(f)
        pid = lock_info.get('pid')
        if pid and psutil.pid_exists(pid):
            print("\n" + "="*60)
            print("!!! エラー: Nexus Arkの別プロセスが既に実行中です。")
            print(f"    - 実行中のPID: {pid}")
            print(f"    - パス: {lock_info.get('path', '不明')}")
            print("    多重起動はできません。既存のプロセスを終了するか、")
            print("    タスクマネージャーからプロセスを強制終了してください。")
            print("="*60 + "\n")
            return False
        else:
            print("\n" + "!"*60)
            print("警告: 古いロックファイルを検出しました。")
            print(f"  - 記録されていたPID: {pid or '不明'} (このプロセスは現在実行されていません)")
            print("  古いロックファイルを自動的に削除して、処理を続行します。")
            print("!"*60 + "\n")
            LOCK_FILE_PATH.unlink()
            time.sleep(0.5)
            _create_lock_file()
            print("--- ロックを取得しました (自動クリーンアップ後) ---")
            return True
    except (json.JSONDecodeError, IOError) as e:
        print(f"警告: ロックファイル '{LOCK_FILE_PATH}' が破損しているようです。エラー: {e}")
        print("破損したロックファイルを削除して、処理を続行します。")
        try:
            LOCK_FILE_PATH.unlink()
            time.sleep(0.5)
            _create_lock_file()
            print("--- ロックを取得しました (破損ファイル削除後) ---")
            return True
        except Exception as delete_e:
            print(f"!!! エラー: 破損したロックファイルの削除に失敗しました: {delete_e}")
            return False
    except Exception as e:
        print(f"!!! エラー: ロック処理中に予期せぬ問題が発生しました: {e}")
        traceback.print_exc()
        return False

def _create_lock_file():
    with open(LOCK_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "path": os.path.abspath(os.path.dirname(__file__))}, f)

def release_lock():
    try:
        if not LOCK_FILE_PATH.exists():
            return
        with open(LOCK_FILE_PATH, "r", encoding="utf-8") as f:
            lock_info = json.load(f)
        if lock_info.get('pid') == os.getpid():
            LOCK_FILE_PATH.unlink()
            print("\n--- グローバル・ロックを解放しました ---")
        else:
            print(f"\n警告: 自分のものではないロックファイル (PID: {lock_info.get('pid')}) を解放しようとしましたが、スキップしました。")
    except Exception as e:
        print(f"\n警告: ロックファイルの解放中にエラーが発生しました: {e}")

def load_chat_log(file_path: str) -> List[Dict[str, str]]:
    """
    (Definitive Edition v3)
    Reads a log file and returns a unified list of dictionaries.
    This version uses a robust finditer approach to prevent message content
    from being misinterpreted as a new speaker header.
    """
    messages: List[Dict[str, str]] = []
    if not file_path or not os.path.exists(file_path):
        return messages
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"エラー: ログファイル '{file_path}' 読込エラー: {e}")
        return messages

    if not content.strip():
        return messages

    # Regex to find all valid headers
    header_pattern = re.compile(r'^## (USER|AGENT|SYSTEM):(.+?)$', re.MULTILINE)

    matches = list(header_pattern.finditer(content))

    for i, match in enumerate(matches):
        # Extract header info
        role = match.group(1).upper()
        responder = match.group(2).strip()
        if role == "USER":
            responder = "user"

        # Determine content span
        start_of_content = match.end()
        end_of_content = matches[i + 1].start() if i + 1 < len(matches) else len(content)

        # Extract and clean content
        message_content = content[start_of_content:end_of_content].strip()

        messages.append({"role": role, "responder": responder, "content": message_content})

    return messages


def _perform_log_archiving(log_file_path: str, character_name: str, threshold_bytes: int, keep_bytes: int) -> Optional[str]:
    # Import locally to avoid circular dependencies
    import room_manager
    try:
        if os.path.getsize(log_file_path) <= threshold_bytes:
            return None

        # Create a backup before modifying the log file
        room_manager.create_backup(character_name, 'log')

        print(f"--- [ログアーカイブ開始] {log_file_path} が {threshold_bytes / 1024 / 1024:.1f}MB を超えました ---")
        with open(log_file_path, "r", encoding="utf-8") as f: content = f.read()
        log_parts = re.split(r'^(## .*?:)$', content, flags=re.MULTILINE)
        messages = []
        header = None
        for part in log_parts:
            part_strip = part.strip()
            if not part_strip: continue
            if part_strip.startswith("## ") and part_strip.endswith(":"): header = part
            elif header: messages.append(header + part); header = None
        current_size = 0
        split_index = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            current_size += len(messages[i].encode('utf-8'))
            if current_size >= keep_bytes:
                if messages[i].strip().startswith(f"## {character_name}:"): split_index = i + 1
                else: split_index = i
                break
        if split_index >= len(messages) or split_index == 0:
            print("--- [ログアーカイブ] 適切な分割点が見つからなかったため、処理を中断しました ---")
            return None
        content_to_archive = "".join(messages[:split_index])
        content_to_keep = "".join(messages[split_index:])
        archive_dir = os.path.join(os.path.dirname(log_file_path), "log_archives")
        os.makedirs(archive_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = os.path.join(archive_dir, f"log_archive_{timestamp}.txt")
        with open(archive_path, "w", encoding="utf-8") as f: f.write(content_to_archive.strip())
        with open(log_file_path, "w", encoding="utf-8") as f: f.write(content_to_keep.strip() + "\n\n")
        archive_size_mb = os.path.getsize(archive_path) / 1024 / 1024
        message = f"古いログをアーカイブしました ({archive_size_mb:.2f}MB)"
        print(f"--- [ログアーカイブ完了] {message} -> {archive_path} ---")
        return message
    except Exception as e:
        print(f"!!! [ログアーカイブエラー] {e}"); traceback.print_exc()
        return None

def save_message_to_log(log_file_path: str, header: str, text_content: str) -> Optional[str]:
    import config_manager
    if not all([log_file_path, header, text_content, text_content.strip()]): return None
    try:
        content_to_append = f"{header.strip()}\n{text_content.strip()}\n\n"
        if not os.path.exists(log_file_path) or os.path.getsize(log_file_path) == 0:
             content_to_append = content_to_append.lstrip()
        with open(log_file_path, "a", encoding="utf-8") as f: f.write(content_to_append)
        character_name = os.path.basename(os.path.dirname(log_file_path))
        threshold_mb = config_manager.CONFIG_GLOBAL.get("log_archive_threshold_mb", 10)
        keep_mb = config_manager.CONFIG_GLOBAL.get("log_keep_size_mb", 5)
        threshold_bytes = threshold_mb * 1024 * 1024
        keep_bytes = keep_mb * 1024 * 1024
        return _perform_log_archiving(log_file_path, character_name, threshold_bytes, keep_bytes)
    except Exception as e:
        print(f"エラー: ログファイル '{log_file_path}' 書き込みエラー: {e}"); traceback.print_exc()
        return None

def delete_message_from_log(log_file_path: str, message_to_delete: Dict[str, str]) -> bool:
    if not log_file_path or not os.path.exists(log_file_path) or not message_to_delete: return False
    try:
        all_messages = load_chat_log(log_file_path)
        original_len = len(all_messages)
        all_messages = [msg for msg in all_messages if not (msg.get("content") == message_to_delete.get("content") and msg.get("responder") == message_to_delete.get("responder"))]
        if len(all_messages) >= original_len:
            print(f"警告: ログファイル内に削除対象のメッセージが見つかりませんでした。"); return False
        log_content_parts = []
        for msg in all_messages:
            role = msg.get("role", "AGENT").upper(); responder_id = msg.get("responder", "不明")
            header = f"## {role}:{responder_id}"
            content = msg.get('content', '').strip()
            if content: log_content_parts.append(f"{header}\n{content}")
        new_log_content = "\n\n".join(log_content_parts)
        with open(log_file_path, "w", encoding="utf-8") as f: f.write(new_log_content)
        if new_log_content:
            with open(log_file_path, "a", encoding="utf-8") as f: f.write("\n\n")
        print("--- Successfully deleted message from log ---"); return True
    except Exception as e:
        print(f"エラー: ログからのメッセージ削除中に予期せぬエラー: {e}"); traceback.print_exc()
        return False

def remove_thoughts_from_text(text: str) -> str:
    if not text: return ""
    # 新しい <thinking> タグに対応
    thoughts_pattern = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL | re.IGNORECASE)
    return thoughts_pattern.sub("", text).strip()

def get_current_location(character_name: str) -> Optional[str]:
    try:
        location_file_path = os.path.join("characters", character_name, "current_location.txt")
        if os.path.exists(location_file_path):
            with open(location_file_path, 'r', encoding='utf-8') as f: return f.read().strip()
    except Exception as e:
        print(f"警告: 現在地ファイルの読み込みに失敗しました: {e}")
    return None

def extract_raw_text_from_html(html_content: Union[str, tuple, None]) -> str:
    if not html_content or not isinstance(html_content, str): return ""
    soup = BeautifulSoup(html_content, 'html.parser')
    thoughts_text = ""
    thoughts_div = soup.find('div', class_='thoughts')
    if thoughts_div:
        for br in thoughts_div.find_all("br"): br.replace_with("\n")
        thoughts_content = thoughts_div.get_text()
        if thoughts_content: thoughts_text = f"【Thoughts】\n{thoughts_content.strip()}\n【/Thoughts】\n\n"
        thoughts_div.decompose()
    for nav_div in soup.find_all('div', style=lambda v: v and 'text-align: right' in v): nav_div.decompose()
    for anchor_span in soup.find_all('span', id=lambda v: v and v.startswith('msg-anchor-')): anchor_span.decompose()
    for br in soup.find_all("br"): br.replace_with("\n")
    main_text = soup.get_text()
    return (thoughts_text + main_text).strip()

def load_scenery_cache(room_name: str) -> dict:
    if not room_name: return {}
    cache_path = os.path.join(constants.ROOMS_DIR, room_name, "cache", "scenery.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                content = f.read()
                if not content.strip(): return {}
                data = json.loads(content)
                return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, IOError): return {}
    return {}

def save_scenery_cache(room_name: str, cache_key: str, location_name: str, scenery_text: str):
    if not room_name or not cache_key: return
    cache_path = os.path.join(constants.ROOMS_DIR, room_name, "cache", "scenery.json")
    try:
        existing_cache = load_scenery_cache(room_name)
        data_to_save = {"location_name": location_name, "scenery_text": scenery_text, "timestamp": datetime.datetime.now().isoformat()}
        existing_cache[cache_key] = data_to_save
        with open(cache_path, "w", encoding="utf-8") as f: json.dump(existing_cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"!! エラー: 情景キャッシュの保存に失敗しました: {e}")

def format_tool_result_for_ui(tool_name: str, tool_result: str) -> Optional[str]:
    if not tool_name or not tool_result: return None
    if "Error" in tool_result or "エラー" in tool_result: return f"⚠️ ツール「{tool_name}」の実行に失敗しました。"
    display_text = ""
    if tool_name == 'set_current_location':
        location_match = re.search(r"現在地は '(.*?)' に設定されました", tool_result)
        if location_match: display_text = f'現在地を「{location_match.group(1)}」に設定しました。'
    elif tool_name == 'set_timer':
        duration_match = re.search(r"for (\d+) minutes", tool_result)
        if duration_match: display_text = f"タイマーをセットしました（{duration_match.group(1)}分）"
    elif tool_name == 'set_pomodoro_timer':
        match = re.search(r"(\d+) cycles \((\d+) min work, (\d+) min break\)", tool_result)
        if match: display_text = f"ポモドーロタイマーをセットしました（{match.group(2)}分・{match.group(3)}分・{match.group(1)}セット）"
    elif tool_name == 'web_search_tool': display_text = 'Web検索を実行しました。'
    elif tool_name == 'add_to_notepad':
        entry_match = re.search(r'entry "(.*?)" was added', tool_result)
        if entry_match: display_text = f'メモ帳に「{entry_match.group(1)[:30]}...」を追加しました。'
    elif tool_name == 'update_notepad':
        entry_match = re.search(r'updated to "(.*?)"', tool_result)
        if entry_match: display_text = f'メモ帳を「{entry_match.group(1)[:30]}...」に更新しました。'
    elif tool_name == 'delete_from_notepad':
        entry_match = re.search(r'deleted from the notepad', tool_result)
        if entry_match: display_text = f'メモ帳から項目を削除しました。'
    elif tool_name == 'generate_image': display_text = '新しい画像を生成しました。'
    return f"🛠️ {display_text}" if display_text else f"🛠️ ツール「{tool_name}」を実行しました。"

def get_season(month: int) -> str:
    if month in [3, 4, 5]: return "spring"
    if month in [6, 7, 8]: return "summer"
    if month in [9, 10, 11]: return "autumn"
    return "winter"

def get_time_of_day(hour: int) -> str:
    if 5 <= hour < 10: return "morning"
    if 10 <= hour < 17: return "daytime"
    if 17 <= hour < 19: return "evening"
    return "night"

def find_scenery_image(room_name: str, location_id: str, season_en: str = None, time_of_day_en: str = None) -> Optional[str]:
    """
    【v2: 時間指定対応】
    指定された場所と時間コンテキストに最も一致する情景画像を検索する。
    完全一致 -> 季節一致 -> 場所のみ一致 の優先順位でフォールバックする。
    """
    if not room_name or not location_id: return None
    image_dir = os.path.join(constants.ROOMS_DIR, room_name, "spaces", "images")
    if not os.path.isdir(image_dir): return None

    # --- 適用すべき時間コンテキストを決定 ---
    # 引数で渡されなかった場合は、現在時刻から取得する
    now = datetime.datetime.now()
    effective_season = season_en or get_season(now.month)
    effective_time_of_day = time_of_day_en or get_time_of_day(now.hour)

    # 1. 完全一致の検索 (場所_季節_時間帯.png)
    target_filename = f"{location_id}_{effective_season}_{effective_time_of_day}.png"
    target_path = os.path.join(image_dir, target_filename)
    if os.path.exists(target_path):
        print(f"--- 最適な情景画像を発見 (完全一致): {target_path} ---"); return target_path

    # 2. 季節一致の検索 (場所_季節_*.png)
    try:
        for filename in os.listdir(image_dir):
            if filename.startswith(f"{location_id}_{effective_season}_") and filename.lower().endswith('.png'):
                found_path = os.path.join(image_dir, filename)
                print(f"--- 最適な情景画像を発見 (季節一致): {found_path} ---"); return found_path
    except FileNotFoundError: pass

    # 3. 場所のみ一致の検索 (場所.png または 場所_*.png)
    try:
        # まずは単純な `場所.png` を探す
        fallback_path = os.path.join(image_dir, f"{location_id}.png")
        if os.path.exists(fallback_path):
            print(f"--- 最適な情景画像を発見 (場所一致): {fallback_path} ---"); return fallback_path

        # それもなければ `場所_` で始まるものを探す
        for filename in os.listdir(image_dir):
            if filename.startswith(f"{location_id}_") and filename.lower().endswith('.png'):
                found_path = os.path.join(image_dir, filename)
                print(f"--- 最適な情景画像を発見 (場所一致): {found_path} ---"); return found_path
    except FileNotFoundError: pass

    # 4. それでも見つからない場合はNoneを返す
    return None

def parse_world_file(file_path: str) -> dict:
    if not os.path.exists(file_path): return {}
    with open(file_path, "r", encoding="utf-8") as f: content = f.read()
    world_data = {}; current_area_key = None; current_place_key = None
    lines = content.split('\n')
    for line in lines:
        line_strip = line.strip()
        if line_strip.startswith("## "):
            current_area_key = line_strip[3:].strip()
            if current_area_key not in world_data: world_data[current_area_key] = {}
            current_place_key = None
        elif line_strip.startswith("### "):
            if current_area_key:
                current_place_key = line_strip[4:].strip()
                world_data[current_area_key][current_place_key] = ""
            else: print(f"警告: エリアが定義される前に場所 '{line_strip}' が見つかりました。")
        else:
            if current_area_key and current_place_key:
                if world_data[current_area_key][current_place_key]: world_data[current_area_key][current_place_key] += "\n" + line
                else: world_data[current_area_key][current_place_key] = line
    for area, places in world_data.items():
        for place, text in places.items(): world_data[area][place] = text.strip()
    return world_data

def delete_and_get_previous_user_input(log_file_path: str, ai_message_to_delete: Dict[str, str]) -> Optional[str]:
    if not all([log_file_path, os.path.exists(log_file_path), ai_message_to_delete]): return None
    try:
        all_messages = load_chat_log(log_file_path)
        target_start_index = -1
        for i, msg in enumerate(all_messages):
            if (msg.get("content") == ai_message_to_delete.get("content") and msg.get("responder") == ai_message_to_delete.get("responder")):
                target_start_index = i; break
        if target_start_index == -1: return None
        last_user_message_index = -1
        for i in range(target_start_index - 1, -1, -1):
            if all_messages[i].get("role") == "USER":
                last_user_message_index = i; break
        if last_user_message_index == -1: return None
        user_message_content = all_messages[last_user_message_index].get("content", "")
        messages_to_keep = all_messages[:last_user_message_index]
        log_content_parts = []
        for msg in messages_to_keep:
            header = f"## {msg.get('role', 'AGENT').upper()}:{msg.get('responder', '不明')}"
            content = msg.get('content', '').strip()
            if content: log_content_parts.append(f"{header}\n{content}")
        new_log_content = "\n\n".join(log_content_parts)
        with open(log_file_path, "w", encoding="utf-8") as f: f.write(new_log_content)
        if new_log_content:
            with open(log_file_path, "a", encoding="utf-8") as f: f.write("\n\n")
        content_without_timestamp = re.sub(r'\n\n\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}$', '', user_message_content, flags=re.MULTILINE)
        restored_input = content_without_timestamp.strip()
        print("--- Successfully reset conversation to the last user input for rerun ---")
        return restored_input
    except Exception as e:
        print(f"エラー: 再生成のためのログ削除中に予期せぬエラー: {e}"); traceback.print_exc()
        return None

@contextlib.contextmanager
def capture_prints():
    original_stdout = sys.stdout; original_stderr = sys.stderr
    string_io = io.StringIO()
    sys.stdout = string_io; sys.stderr = string_io
    try: yield string_io
    finally: sys.stdout = original_stdout; sys.stderr = original_stderr

def delete_user_message_and_after(log_file_path: str, user_message_to_delete: Dict[str, str]) -> Optional[str]:
    if not all([log_file_path, os.path.exists(log_file_path), user_message_to_delete]): return None
    try:
        all_messages = load_chat_log(log_file_path)
        target_index = -1
        for i, msg in enumerate(all_messages):
            if (msg.get("content") == user_message_to_delete.get("content") and msg.get("responder") == user_message_to_delete.get("responder")):
                target_index = i; break
        if target_index == -1: return None
        user_message_content = all_messages[target_index].get("content", "")
        messages_to_keep = all_messages[:target_index]
        log_content_parts = []
        for msg in messages_to_keep:
            header = f"## {msg.get('role', 'AGENT').upper()}:{msg.get('responder', '不明')}"
            content = msg.get('content', '').strip()
            if content: log_content_parts.append(f"{header}\n{content}")
        new_log_content = "\n\n".join(log_content_parts)
        with open(log_file_path, "w", encoding="utf-8") as f: f.write(new_log_content)
        if new_log_content:
            with open(log_file_path, "a", encoding="utf-8") as f: f.write("\n\n")
        content_without_timestamp = re.sub(r'\n\n\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}$', '', user_message_content, flags=re.MULTILINE)
        restored_input = content_without_timestamp.strip()
        print("--- Successfully reset conversation to before the selected user input for rerun ---")
        return restored_input
    except Exception as e:
        print(f"エラー: ユーザー発言以降のログ削除中に予期せぬエラー: {e}"); traceback.print_exc()
        return None

def create_dynamic_sanctuary(main_log_path: str, user_start_phrase: str) -> Optional[str]:
    if not main_log_path or not os.path.exists(main_log_path) or not user_start_phrase: return None
    try:
        with open(main_log_path, "r", encoding="utf-8") as f: full_content = f.read()
        cleaned_phrase = re.sub(r'\n\n\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}$', '', user_start_phrase, flags=re.MULTILINE).strip()
        pattern = re.compile(r"(^## ユーザー:\s*" + re.escape(cleaned_phrase) + r".*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE)
        match = pattern.search(full_content)
        if not match:
            print(f"警告：動的聖域の起点となるユーザー発言が見つかりませんでした。完全なログを聖域として使用します。")
            sanctuary_content = full_content
        else: sanctuary_content = full_content[match.start():]
        temp_dir = os.path.join("temp", "sanctuaries"); os.makedirs(temp_dir, exist_ok=True)
        sanctuary_path = os.path.join(temp_dir, f"sanctuary_{uuid.uuid4().hex}.txt")
        with open(sanctuary_path, "w", encoding="utf-8") as f: f.write(sanctuary_content)
        return sanctuary_path
    except Exception as e:
        print(f"エラー：動的聖域の作成中にエラーが発生しました: {e}"); traceback.print_exc()
        return None

def cleanup_sanctuaries():
    temp_dir = os.path.join("temp", "sanctuaries")
    if not os.path.exists(temp_dir): return

def create_turn_snapshot(main_log_path: str, user_start_phrase: str) -> Optional[str]:
    if not main_log_path or not os.path.exists(main_log_path) or not user_start_phrase: return None
    try:
        with open(main_log_path, "r", encoding="utf-8") as f: full_content = f.read()
        cleaned_phrase = re.sub(r'\[ファイル添付:.*?\]', '', user_start_phrase, flags=re.DOTALL).strip()
        cleaned_phrase = re.sub(r'\n\n\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}$', '', cleaned_phrase, flags=re.MULTILINE).strip()
        pattern = re.compile(r"(^## (?:ユーザー|ユーザー):" + re.escape(cleaned_phrase) + r".*?)(?=^## (?:ユーザー|ユーザー):|\Z)", re.DOTALL | re.MULTILINE)
        matches = [m for m in pattern.finditer(full_content)]
        if not matches: snapshot_content = f"## ユーザー:\n{user_start_phrase.strip()}\n\n"
        else: last_match = matches[-1]; snapshot_content = full_content[last_match.start():]
        temp_dir = os.path.join("temp", "snapshots"); os.makedirs(temp_dir, exist_ok=True)
        snapshot_path = os.path.join(temp_dir, f"snapshot_{uuid.uuid4().hex}.txt")
        with open(snapshot_path, "w", encoding="utf-8") as f: f.write(snapshot_content)
        return snapshot_path
    except Exception as e:
        print(f"エラー：スナップショットの作成中にエラーが発生しました: {e}"); traceback.print_exc()
        return None

def is_character_name(name: str) -> bool:
    if not name or not isinstance(name, str) or not name.strip(): return False
    if ".." in name or "/" in name or "\\" in name: return False
    room_dir = os.path.join(constants.ROOMS_DIR, name)
    return os.path.isdir(room_dir)

# ▼▼▼【ここからが新しく追加する関数】▼▼▼
def _overwrite_log_file(file_path: str, messages: List[Dict]):
    """
    メッセージ辞書のリストからログファイルを完全に上書きする。
    """
    log_content_parts = []
    for msg in messages:
        # 新しいログ形式 `ROLE:NAME` に完全準拠して書き出す
        role = msg.get("role", "AGENT").upper()
        responder_id = msg.get("responder", "不明")
        header = f"## {role}:{responder_id}"
        content = msg.get('content', '').strip()
        # contentが空でもヘッダーは記録されるべき場合があるため、
        # responder_idが存在すればエントリを作成する
        if responder_id:
             log_content_parts.append(f"{header}\n{content}")

    new_log_content = "\n\n".join(log_content_parts)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_log_content)
    # ファイルの末尾に追記用の改行を追加
    if new_log_content:
        with open(file_path, "a", encoding="utf-8") as f:
            f.write("\n\n")

# ▲▲▲【追加はここまで】▲▲▲

def load_html_cache(room_name: str) -> Dict[str, str]:
    """指定されたルームのHTMLキャッシュを読み込む。"""
    if not room_name:
        return {}
    cache_path = os.path.join(constants.ROOMS_DIR, room_name, "cache", "html_cache.json")
    if os.path.exists(cache_path):
        try:
            # パフォーマンスのため、ファイルサイズが0でないこともチェック
            if os.path.getsize(cache_path) > 0:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, IOError):
            pass # エラーの場合は新しいキャッシュを作成
    return {}

def save_html_cache(room_name: str, cache_data: Dict[str, str]):
    """指定されたルームのHTMLキャッシュを保存する。"""
    if not room_name:
        return
    cache_dir = os.path.join(constants.ROOMS_DIR, room_name, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "html_cache.json")
    try:
        # 新しいキャッシュファイルを、一時ファイルに書き出してからリネームすることで、書き込み中のクラッシュによるファイル破損を防ぐ
        temp_path = cache_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f) # パフォーマンスのため、インデントなしで保存
        os.replace(temp_path, cache_path)
    except Exception as e:
        print(f"!! エラー: HTMLキャッシュの保存に失敗しました: {e}")
