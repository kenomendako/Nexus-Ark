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
import glob

LOCK_FILE_PATH = Path("nexus_ark.lock")
_MIGRATION_DONE_CACHE = set()
 
# --- [Phase 7] システム通知バッファ ---
# フォールバックなどの警告を一時的に保持し、チャット応答時にユーザーに提示する
SYSTEM_NOTICES: List[Dict[str, str]] = []

def add_system_notice(msg: str, level: str = "warning"):
    """
    システム通知を追加する。
    level: "warning", "info", "error"
    """
    # 重複を避ける
    if any(n["message"] == msg for n in SYSTEM_NOTICES):
        return
    SYSTEM_NOTICES.append({"message": msg, "level": level, "timestamp": datetime.datetime.now().isoformat()})
    print(f"--- [System Notice Added] {msg} (Level: {level}) ---")

def consume_system_notices() -> List[Dict[str, str]]:
    """
    溜まっている通知をすべて返し、バッファをクリアする。
    """
    global SYSTEM_NOTICES
    notices = SYSTEM_NOTICES.copy()
    SYSTEM_NOTICES.clear()
    return notices
def sanitize_model_name(model_name: str) -> str:
    """
    モデル名からお気に入りマークや注釈（カッコ書き）を除去する。
    例: "⭐ glm-4.7-flash (Recommended)" -> "glm-4.7-flash"
    """
    if not model_name:
        return ""
    # 1. お気に入りマークを除去
    sanitized = model_name.replace("⭐ ", "").replace("⭐", "").strip()
    # 2. 注釈（カッコ書き）を除去
    sanitized = sanitized.split(" (")[0].strip()
    return sanitized

def acquire_lock() -> bool:
    print("--- アプリケーションの単一起動をチェックしています... ---")
    try:
        if not LOCK_FILE_PATH.exists():
            _create_lock_file()
            print("--- ロックファイルを新規作成しました。起動処理を続行します。 ---")
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
            print("--- 古いロックファイルを削除し、新しいロックを作成しました。 ---")
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

def load_chat_log(file_path: str, single_file_only: bool = False) -> List[Dict[str, str]]:
    """
    (Definitive Edition v5: Monthly Segmented Loading with Single File option)
    Reads log files and returns a unified list of dictionaries.
    If 'single_file_only' is True and file_path points to a specific month file, only that file is loaded.
    """
    messages: List[Dict[str, str]] = []
    if not file_path:
        return messages

    # --- [NEW] 月次分割対応: ルームディレクトリの特定 ---
    file_path_clean = file_path.replace("\\", "/")
    if f"/{constants.LOGS_DIR_NAME}/" in file_path_clean:
        room_dir = os.path.dirname(os.path.dirname(file_path))
    elif file_path_clean.endswith(f"/{constants.LOGS_DIR_NAME}"):
        room_dir = os.path.dirname(file_path)
    else:
        room_dir = os.path.dirname(file_path)
        
    logs_dir = os.path.join(room_dir, constants.LOGS_DIR_NAME)
    
    # 移行が必要な場合は実行 (初期化時など)
    # 頻繁な実行を防ぐため、セッション内でのキャッシュを確認
    legacy_log = os.path.join(room_dir, "log.txt")
    if os.path.exists(legacy_log):
        _migrate_chat_logs(room_dir)

    target_files = []
    # single_file_only かつ 指定パスがファイルとして存在する場合
    if single_file_only and os.path.isfile(file_path):
        target_files = [file_path]
    elif os.path.exists(logs_dir) and os.path.isdir(logs_dir):
        # logs/ 内の全ファイルを日付順に取得
        target_files = sorted(glob.glob(os.path.join(logs_dir, "*.txt")))
        
        # [Fallback] logs/ フォルダがあるが空の場合（移行失敗など）、
        # legacy_log が残っていればそれを読み込むようにする
        if not target_files and os.path.exists(legacy_log):
             target_files = [legacy_log]
             
    elif os.path.exists(file_path):
        # 指定されたファイルのみ
        target_files = [file_path]

    if not target_files:
        return messages

    # 全ファイルを読み込んで結合
    # TODO: 将来的に非常に重くなる場合は、ここでの制限読み込みを検討
    for f_path in target_files:
        try:
            with open(f_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"エラー: ログファイル '{f_path}' 読込エラー: {e}")
            continue

        if not content.strip():
            continue

        # Regex to find all valid headers
        header_pattern = re.compile(r'^## (USER|AGENT|SYSTEM):(.+?)$', re.MULTILINE)
        matches = list(header_pattern.finditer(content))

        for i, match in enumerate(matches):
            role = match.group(1).upper()
            responder = match.group(2).strip()
            if role == "USER":
                responder = "user"
            start_of_content = match.end()
            end_of_content = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            message_content = content[start_of_content:end_of_content].strip()
            messages.append({"role": role, "responder": responder, "content": message_content})

    return messages

def _get_room_dir_from_path(file_path: str) -> str:
    """
    ファイルパスからルームディレクトリを特定するヘルパー。
    """
    if not file_path: return ""
    file_path_clean = file_path.replace("\\", "/")
    if f"/{constants.LOGS_DIR_NAME}/" in file_path_clean:
        return os.path.dirname(os.path.dirname(file_path))
    elif file_path_clean.endswith(f"/{constants.LOGS_DIR_NAME}"):
        return os.path.dirname(file_path)
    else:
        return os.path.dirname(file_path)

    # 全ファイルを読み込んで結合
    # TODO: 将来的に非常に重くなる場合は、ここでの制限読み込みを検討
    for f_path in target_files:
        try:
            with open(f_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"エラー: ログファイル '{f_path}' 読込エラー: {e}")
            continue

        if not content.strip():
            continue

        # Regex to find all valid headers
        header_pattern = re.compile(r'^## (USER|AGENT|SYSTEM):(.+?)$', re.MULTILINE)
        matches = list(header_pattern.finditer(content))

        for i, match in enumerate(matches):
            role = match.group(1).upper()
            responder = match.group(2).strip()
            if role == "USER":
                responder = "user"
            start_of_content = match.end()
            end_of_content = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            message_content = content[start_of_content:end_of_content].strip()
            messages.append({"role": role, "responder": responder, "content": message_content})

    return messages

def _migrate_chat_logs(room_dir: str):
    """
    既存の log.txt および log_archives を新形式 (logs/YYYY-MM.txt) に自動移行する。
    """
    if not room_dir:
        return
        
    # グローバルキャッシュでチェック (セッションごとの重複実行防止)
    global _MIGRATION_DONE_CACHE
    if room_dir in _MIGRATION_DONE_CACHE:
        return

    legacy_log = os.path.join(room_dir, "log.txt")
    legacy_archives_dir = os.path.join(room_dir, "log_archives")
    new_logs_dir = os.path.join(room_dir, constants.LOGS_DIR_NAME)
    
    if not os.path.exists(legacy_log) and not os.path.exists(legacy_archives_dir):
        return

    print(f"--- [Log Migration] 既存のログを新しい月次形式に整理しています: {room_dir} ---")
    os.makedirs(new_logs_dir, exist_ok=True)
    
    all_files = []
    if os.path.exists(legacy_log): all_files.append(legacy_log)
    if os.path.exists(legacy_archives_dir):
        all_files.extend(glob.glob(os.path.join(legacy_archives_dir, "*.txt")))
    
    # タイムスタンプ抽出用
    # メッセージ内の "YYYY-MM-DD (Tue) HH:MM:SS" 形式を優先
    date_pattern = re.compile(r'(\d{4}-\d{2})-\d{2}')
    
    for f_path in all_files:
        messages_by_month = {}
        try:
            # 既存の読み込みロジックを一時的に流用して個別のメッセージに分解
            raw_content = ""
            with open(f_path, "r", encoding="utf-8") as f:
                raw_content = f.read()
            
            if not raw_content.strip(): continue
            
            header_pattern = re.compile(r'^(## (?:USER|AGENT|SYSTEM):.+?)$', re.MULTILINE)
            parts = header_pattern.split(raw_content)
            
            current_month = "0000-00" # 日付不明用
            
            # header_pattern.split は、[空, header1, body1, header2, body2, ...] を返す
            for i in range(1, len(parts), 2):
                header = parts[i]
                body = parts[i+1] if i+1 < len(parts) else ""
                
                # ボディから日付を探す
                match = date_pattern.search(body)
                if match:
                    current_month = match.group(1)
                
                if current_month not in messages_by_month:
                    messages_by_month[current_month] = []
                messages_by_month[current_month].append(f"{header}{body}")
            
            # 月ごとに保存
            for month, contents in messages_by_month.items():
                target_path = os.path.join(new_logs_dir, f"{month}.txt")
                with open(target_path, "a", encoding="utf-8") as f:
                    f.write("".join(contents))
            
            # 元ファイルをリネーム（バックアップ化）
            backup_path = f"{f_path}.migrated"
            os.rename(f_path, backup_path)
            
        except Exception as e:
            print(f"!!! [Log Migration Error] {f_path} の移行中にエラー: {e}")
            traceback.print_exc()


    print(f"--- [Log Migration] 完了 ---")
    _MIGRATION_DONE_CACHE.add(room_dir)


def _perform_log_archiving(log_file_path: str, character_name: str, threshold_bytes: int, keep_bytes: int) -> Optional[str]:
    # Import locally to avoid circular dependencies
    import room_manager
    try:
        if os.path.getsize(log_file_path) <= threshold_bytes:
            return None

        print(f"--- [ログアーカイブ開始] {log_file_path} が {threshold_bytes / 1024 / 1024:.1f}MB を超えました ---")

        # Create a backup before modifying the log file
        room_manager.create_backup(character_name, 'log')

        with open(log_file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # メッセージの区切り（ヘッダー行）のインデックスを全て探す
        header_indices = [i for i, line in enumerate(lines) if line.startswith("## ") and ":" in line]
        
        if not header_indices:
            print("--- [ログアーカイブ警告] ヘッダーが見つかりませんでした。アーカイブを中止します。 ---")
            return None

        # 後ろからサイズを積み上げていき、keep_bytes を超える境界を探す
        current_size = 0
        split_line_index = 0
        
        # 最後の行から逆順にスキャン
        for i in range(len(lines) - 1, -1, -1):
            current_size += len(lines[i].encode('utf-8'))
            
            # keep_bytes を超えた時点で、その行より手前にある「直近のヘッダー」を探す
            if current_size >= keep_bytes:
                # 現在行(i)より前にあるヘッダーの中で、最も大きいインデックスを探す
                valid_headers = [h for h in header_indices if h <= i]
                if valid_headers:
                    split_line_index = valid_headers[-1]
                else:
                    # ヘッダーが見つからない場合は安全のため半分で切る（フォールバック）
                    split_line_index = header_indices[len(header_indices) // 2]
                break
        
        # 分割点が先頭(0)になってしまった場合（全部残すことになってしまう場合）、強制的に古い方1/3をアーカイブする
        if split_line_index == 0 and len(header_indices) > 10:
             print("--- [ログアーカイブ] 適切な分割点が見つからなかったため、強制的に古いログの約1/3をアーカイブします ---")
             split_line_index = header_indices[len(header_indices) // 3]

        if split_line_index == 0:
             print("--- [ログアーカイブ] 分割できませんでした ---")
             return None

        content_to_archive = "".join(lines[:split_line_index])
        content_to_keep = "".join(lines[split_line_index:])

        archive_dir = os.path.join(os.path.dirname(log_file_path), "log_archives")
        os.makedirs(archive_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = os.path.join(archive_dir, f"log_archive_{timestamp}.txt")

        with open(archive_path, "w", encoding="utf-8") as f:
            f.write(content_to_archive.strip())
        
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write(content_to_keep.strip() + "\n\n")

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
        # --- [NEW] 月次分割対応: 保存先を現在月に変更 ---
        file_path_clean = log_file_path.replace("\\", "/")
        if f"/{constants.LOGS_DIR_NAME}/" in file_path_clean:
            room_dir = os.path.dirname(os.path.dirname(log_file_path))
        else:
            room_dir = os.path.dirname(log_file_path)
            
        current_month = datetime.datetime.now().strftime("%Y-%m")
        target_log_file = os.path.join(room_dir, constants.LOGS_DIR_NAME, f"{current_month}.txt")
        os.makedirs(os.path.dirname(target_log_file), exist_ok=True)
        
        content_to_append = f"{header.strip()}\n{text_content.strip()}\n\n"
        # ファイルが新規作成される場合は、先頭の改行を削除
        if not os.path.exists(target_log_file) or os.path.getsize(target_log_file) == 0:
             content_to_append = content_to_append.lstrip()
             
        with open(target_log_file, "a", encoding="utf-8") as f: 
            f.write(content_to_append)
        
        # 従来のサイズベース・アーカイブ処理は月次分割に統合されたため不要（互換性のために形だけ残すか空にする）
        # _perform_log_archiving の戻り値を利用している箇所がないか確認
        return None
    except Exception as e:
        print(f"エラー: ログ保存エラー: {e}"); traceback.print_exc()
        return None

def delete_message_from_log(log_file_path: str, message_to_delete: Dict[str, str]) -> Optional[str]:
    """
    ログからメッセージを削除し、成功した場合は削除されたメッセージのタイムスタンプ(HH:MM:SS)を返す。
    """
    if not log_file_path or not message_to_delete: return None
    room_dir = _get_room_dir_from_path(log_file_path)
    if not os.path.exists(room_dir): return None
    
    try:
        all_messages = load_chat_log(log_file_path)
        original_len = len(all_messages)
        
        deleted_timestamp = None
        new_messages = []
        for msg in all_messages:
            if (msg.get("content") == message_to_delete.get("content") and msg.get("responder") == message_to_delete.get("responder")):
                # タイムスタンプを抽出
                content = msg.get("content", "")
                responder = msg.get("responder", "")
                
                # 1. コンテンツ末尾から抽出 (標準形式)
                match = re.search(r'(\d{2}:\d{2}:\d{2})(?: \| .*)?$', content)
                if match:
                    deleted_timestamp = match.group(1)
                else:
                    # 2. レスポンダー表示値（ヘッダー行）から抽出 (フォールバック)
                    match = re.search(r'(\d{2}:\d{2}:\d{2})$', responder)
                    if match:
                        deleted_timestamp = match.group(1)
                continue
            new_messages.append(msg)
            
        if len(new_messages) >= original_len:
            print(f"警告: ログファイル内に削除対象のメッセージが見つかりませんでした。"); return None
            
        # 月次分割対応: 削除後のログを各月に振り分けて書き戻す
        messages_by_month = {}
        date_pattern = re.compile(r'(\d{4}-\d{2})-\d{2}')
        current_month = "0000-00"
        
        for msg in new_messages:
            content = msg.get('content', '')
            match = date_pattern.search(content)
            if match:
                current_month = match.group(1)
            
            if current_month not in messages_by_month:
                messages_by_month[current_month] = []
            
            role = msg.get("role", "AGENT").upper()
            responder_id = msg.get("responder", "不明")
            header = f"## {role}:{responder_id}"
            messages_by_month[current_month].append(f"{header}\n{content.strip()}\n\n")

        logs_dir = os.path.join(room_dir, constants.LOGS_DIR_NAME)

        # 全ファイルを一旦空にする（または全削除して書き直す）
        # 安全のため、移行後の形式 (logs/*.txt) にのみ保存
        for month, contents in messages_by_month.items():
            target_path = os.path.join(logs_dir, f"{month}.txt")
            # 既に存在する場合は上書き (w)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write("".join(contents).lstrip())
                
        print("--- Successfully deleted message and re-distributed logs ---")
        return deleted_timestamp or "00:00:00"
    except Exception as e:
        print(f"エラー: ログからのメッセージ削除中に予期せぬエラー: {e}"); traceback.print_exc()
        return None

def _write_segmented_logs(room_dir: str, messages: List[Dict[str, str]]):
    """
    メッセージリストを月ごとに分割して、logs/ フォルダ内の各ファイルに書き込む。
    (w)モードで上書きするため、削除処理後の反映に使用する。
    """
    logs_dir = os.path.join(room_dir, constants.LOGS_DIR_NAME)
    os.makedirs(logs_dir, exist_ok=True)
    
    messages_by_month = {}
    date_pattern = re.compile(r'(\d{4}-\d{2})-\d{2}')
    current_month = "0000-00"
    
    for msg in messages:
        content = msg.get('content', '')
        match = date_pattern.search(content)
        if match:
            current_month = match.group(1)
        
        if current_month not in messages_by_month:
            messages_by_month[current_month] = []
        
        role = msg.get("role", "AGENT").upper()
        responder_id = msg.get("responder", "不明")
        header = f"## {role}:{responder_id}"
        messages_by_month[current_month].append(f"{header}\n{content.strip()}\n\n")

    # v0.2.0-fix: 既存のログファイルを全て確認し、
    # 新しいメッセージリストに含まれない月（＝全削除された月）はファイルを削除するか空にする。
    
    # 1. 既存のログファイル一覧を取得
    existing_log_files = set()
    if os.path.exists(logs_dir):
        for f in os.listdir(logs_dir):
            if f.endswith(".txt"):
                existing_log_files.add(f)
                
    # 2. 書き込み対象の月（ファイル名）
    target_months = set(f"{m}.txt" for m in messages_by_month.keys())
    
    # 3. 更新（上書き）
    for month, contents in messages_by_month.items():
        target_path = os.path.join(logs_dir, f"{month}.txt")
        with open(target_path, "w", encoding="utf-8") as f:
            f.write("".join(contents).lstrip())
            
    # 4. 削除された月のファイルをクリーンアップ（または空にする）
    # メッセージが1つも無くなった月は、ファイルごと削除するのが自然。
    for log_file in existing_log_files:
        if log_file not in target_months:
            # 新しいリストにこの月のデータがない ＝ 全削除された
            file_path = os.path.join(logs_dir, log_file)
            try:
                os.remove(file_path)
                print(f"  [Log Cleanup] Deleted empty log file: {log_file}")
            except Exception as e:
                print(f"  [Log Cleanup] Failed to delete {log_file}: {e}")

def remove_thoughts_from_text(text: str) -> str:
    """
    (v2: The Definitive Thought Remover)
    テキストから、新しい `THOUGHT:` プレフィックス形式と、古い `【Thoughts】` ブロック形式の
    両方の思考ログを除去する。
    """
    if not text:
        return ""

    # 1. ブロック形式（【Thoughts】, [THOUGHT]）を除去
    # 大文字小文字を区別せず、タグとその中身を除去
    text = re.sub(r"【Thoughts】[\s\S]*?【/Thoughts】\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[THOUGHT\][\s\S]*?\[/THOUGHT\]\s*", "", text, flags=re.IGNORECASE)

    # 2. プレフィックス行形式（THOUGHT:）を除去
    lines = text.split('\n')
    cleaned_lines = [line for line in lines if not line.strip().upper().startswith("THOUGHT:")]

    return "\n".join(cleaned_lines).strip()

def clean_persona_text(text: str, remove_thoughts: bool = True) -> str:
    """
    AIの出力に含まれるメタデータタグや内部状態タグを除去し、
    ユーザー向けのクリーンなテキストを返す。
    
    除去対象:
    - 【表情】…表情名…
    - <persona_emotion ... />
    - <memory_trace ... />
    - その他 XML 形式のタグ (タグそのもののみ除去し、中身は残す場合は別途検討)
    - 思考ログ (remove_thoughts=True の場合)
    """
    if not text:
        return ""

    # 1. 思考ログの除去
    if remove_thoughts:
        text = remove_thoughts_from_text(text)

    # 2. 特殊なメタタグの除去 (タグとその周囲の空白・改行を適切に処理)
    # 前後の空白（改行含む）も含めてマッチさせ、適切な改行に置換または削除する方針
    # ここでは単純化のため、一旦タグのみを除去し、後で改行を正規化する
    text = re.sub(r"【表情】…\w+…", "", text)
    text = re.sub(r"<persona_emotion\s+[^>]*/>", "", text)
    text = re.sub(r"<memory_trace\s+[^>]*/>", "", text)
    
    # 3. 汎用的なXMLタグの除去
    # タグそのもののみを除去
    text = re.sub(r"<[^>]+/>", "", text)
    
    # 4. 改行の正規化
    # 3つ以上の連続する改行を2つ（1行の空行）にまとめる
    text = re.sub(r"\n{3,}", "\n\n", text)
    
    return text.strip()

def get_current_location(character_name: str) -> Optional[str]:
    try:
        location_file_path = os.path.join("characters", character_name, "current_location.txt")
        if os.path.exists(location_file_path):
            with open(location_file_path, 'r', encoding='utf-8') as f: return f.read().strip()
    except Exception as e:
        print(f"警告: 現在地ファイルの読み込みに失敗しました: {e}")
    return None

def extract_raw_text_from_html(html_content: Union[str, tuple, None]) -> str:
    """
    (v3: Stable Thought Log Compatible)
    GradioのChatbotが表示するHTML文字列から、元の構造化されたテキストを復元する。
    新しいコードブロックベースの思考ログ表示と、<br>タグによる改行に対応。
    思考ログは、編集時の互換性を維持するため、常に古い【Thoughts】形式で復元する。
    """
    if not html_content or not isinstance(html_content, str): return ""
    
    # BeautifulSoupは<br>を改行として扱わないため、手動で置換
    html_content = html_content.replace("<br>", "\n")
    
    soup = BeautifulSoup(html_content, 'html.parser')
    
    thoughts_text = ""
    # 思考ログは<pre><code>ブロックとしてレンダリングされる
    code_block = soup.find('code')
    if code_block:
        thoughts_content = code_block.get_text()
        if thoughts_content:
            # 常に古い【Thoughts】形式で復元することで、編集時の互換性を最大化する
            thoughts_text = f"【Thoughts】\n{thoughts_content.strip()}\n【/Thoughts】\n\n"
        
        # パース済みなので削除 (親の<pre>ごと消すのが安全)
        if code_block.parent and code_block.parent.name == 'pre':
            code_block.parent.decompose()
        else:
            code_block.decompose()

    for nav_div in soup.find_all('div', style=lambda v: v and 'text-align: right' in v): nav_div.decompose()
    for anchor_span in soup.find_all('span', id=lambda v: v and v.startswith('msg-anchor-')): anchor_span.decompose()
    for br in soup.find_all("br"): br.replace_with("\n")
    main_text = soup.get_text()
    
    # 話者名を除去
    main_text = re.sub(r"^\*\*.*?\*\*\s*", "", main_text.strip()).strip()

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
    
    # 開発者ツールには特別なエラー検知ロジックを適用
    # ファイル内容に "Exception:" や "Error:" などが含まれることが頻繁にあるため、
    # ツール自体のエラーメッセージ（「【エラー】」で始まる行）のみを検出する
    is_developer_tool = tool_name in ["list_project_files", "read_project_file"]
    
    if is_developer_tool:
        # 開発者ツールの標準エラーフォーマットのみ検出
        if re.search(r"^【エラー】", tool_result, re.MULTILINE):
            return f"⚠️ ツール「{tool_name}」の実行に失敗しました。"
    else:
        # 他のツール向けのエラー検知パターン
        error_patterns = [
            r"^Error:",           # 行頭の "Error:"
            r"^【エラー】",        # 行頭の "【エラー】"
            r"^エラー:",           # 行頭の "エラー:"
            r"Exception:",         # Python例外
        ]
        
        generic_error_patterns = [
            r"ツールエラー",        # ツール実行時のエラー
            r"実行エラー",          # 実行時エラー
            r"failed to",          # 失敗パターン（英語）
            r"に失敗しました",      # 失敗パターン（日本語）
            r"could not",          # 失敗パターン（英語）
            r"できませんでした",    # 失敗パターン（日本語）
        ]
        
        for pattern in error_patterns:
            if re.search(pattern, tool_result, re.IGNORECASE | re.MULTILINE):
                return f"⚠️ ツール「{tool_name}」の実行に失敗しました。"
                
        for pattern in generic_error_patterns:
            if re.search(pattern, tool_result, re.IGNORECASE | re.MULTILINE):
                return f"⚠️ ツール「{tool_name}」の実行に失敗しました。"
    
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
    elif tool_name == 'generate_image':
        # プロンプトを抽出して表示（省略版）
        prompt_match = re.search(r'📝 Prompt: (.+?)(?:\n|$)', tool_result, re.DOTALL)
        if prompt_match:
            prompt_text = prompt_match.group(1).strip()
            # 100文字で切り詰め（全文はログの[RAW_RESULT]に保存されている）
            if len(prompt_text) > 100:
                prompt_text = prompt_text[:100] + "..."
            display_text = f'新しい画像を生成しました。\n📝 Prompt: {prompt_text}'
        else:
            display_text = '新しい画像を生成しました。'
    # 記憶検索ツール用のカスタムアナウンス
    elif tool_name == 'recall_memories':
        display_text = '過去の記憶を思い出しました。'
    elif tool_name == 'search_past_conversations':
        # クエリを抽出して表示
        query_match = re.search(r'「(.+?)」', tool_result)
        if query_match:
            display_text = f'過去の会話を検索しました（キーワード: 「{query_match.group(1)}」）'
        else:
            display_text = '過去の会話を検索しました。'
    elif tool_name == 'list_project_files':
        display_text = 'プロジェクトのファイル一覧を取得しました。'
    elif tool_name == 'read_project_file':
        # ファイル名と言い範囲（Lxx-Lyy）を抽出
        file_match = re.search(r'【ファイル内容: (.*?) \((.*?)\ / 全(\d+)行\)】', tool_result)
        if file_match:
            display_text = f'ファイル「{file_match.group(1)}」の {file_match.group(2)} （全{file_match.group(3)}行）を読み取りました。'
        else:
            display_text = 'ファイルを読み取りました。'
    elif tool_name == 'plan_world_edit':
        # 変更箇所を抽出してサマリーを表示
        changes = re.findall(r'- \[(.*?)\] (.*?) > (.*)', tool_result)
        if changes:
            change_texts = [f'[{c[0]}] {c[1]}>{c[2]}' for c in changes]
            summary = "、".join(change_texts)
            if len(summary) > 60: summary = summary[:57] + "..."
            display_text = f'世界設定を更新しました（{summary}）'
        else:
            display_text = '世界設定の更新を計画・実行しました。'
    return f"🛠️ {display_text}" if display_text else f"🛠️ ツール「{tool_name}」を実行しました。"


def get_season(month: int) -> str:
    if month in [3, 4, 5]: return "spring"
    if month in [6, 7, 8]: return "summer"
    if month in [9, 10, 11]: return "autumn"
    return "winter"

def get_time_of_day(hour: int) -> str:
    """
    時刻(hour)から、7つの区分（早朝, 朝, 昼前, 昼下がり, 夕方, 夜, 深夜）の時間帯名を返す。
    """
    if 4 <= hour < 6: return "early_morning"  # 早朝
    if 6 <= hour < 10: return "morning"        # 朝
    if 10 <= hour < 12: return "late_morning"  # 昼前
    if 12 <= hour < 16: return "afternoon"      # 昼下がり
    if 16 <= hour < 19: return "evening"        # 夕方
    if 19 <= hour < 23: return "night"          # 夜
    return "midnight"                         # 深夜 (23, 0, 1, 2, 3)

def find_scenery_image(room_name: str, location_id: str, season_en: str = None, time_of_day_en: str = None) -> Optional[str]:
    """
    【v5: 時間帯・季節両方のフォールバック】
    指定された場所と時間コンテキストに最も一致する情景画像を検索する。
    
    優先順位（INBOXの要件に基づく改善版）:
    1. 場所_季節_時間帯 (完全一致)
    2. 場所_季節_時間帯(簡略) (時間帯名の簡略版でフォールバック)
    3. 場所_[他の季節]_時間帯 (同じ時間帯で季節を遡る)
    4. 場所_時間帯 (時間帯のみ)
    5. 場所_季節 (季節のみ)
    6. 場所 (デフォルト)
    
    例: 
    - 「冬の昼」がなければ「秋の昼」→「夏の昼」→「春の昼」を検索
    - 「late_morning」がなければ「morning」で検索
    """
    if not room_name or not location_id: return None
    image_dir = os.path.join(constants.ROOMS_DIR, room_name, "spaces", "images")
    if not os.path.isdir(image_dir): return None

    # --- 適用すべき時間コンテキストを決定 ---
    now = datetime.datetime.now()
    effective_season = season_en or get_season(now.month)
    effective_time_of_day = time_of_day_en or get_time_of_day(now.hour)
    
    # --- 季節フォールバック順序を生成（現在季節から逆順に遡る）---
    SEASONS_ORDER = ["spring", "summer", "autumn", "winter"]
    def get_season_fallback_order(current_season: str) -> list:
        if current_season not in SEASONS_ORDER:
            return SEASONS_ORDER
        idx = SEASONS_ORDER.index(current_season)
        # 現在季節から逆順に並べる（例: winter → autumn → summer → spring）
        return [SEASONS_ORDER[(idx - i) % 4] for i in range(4)]
    
    # --- 時間帯フォールバックマッピング（明るさベース）---
    # 昼間の時間帯（明るい）→ 最終的に morning までフォールバック
    # 夜の時間帯（暗い）→ night にフォールバック
    # これにより「昼下がりの画像がなくても夜の画像より朝の画像を優先」を実現
    TIME_FALLBACK_MAP = {
        # 昼間時間帯グループ（明るい画像を優先）
        # [v27] daytime (昼間) をフォールバックに追加
        "early_morning": ["morning", "daytime"],           # 早朝 → 朝 → 昼間
        "late_morning": ["morning", "daytime"],            # 昼前 → 朝 → 昼間
        "afternoon": ["noon", "late_morning", "morning", "daytime"],  # 昼下がり → 昼 → 昼前 → 朝 → 昼間
        "noon": ["late_morning", "morning", "daytime"],    # 昼 → 昼前 → 朝 → 昼間
        # 夜間時間帯グループ（暗い画像を優先）
        "evening": ["night"],                              # 夕方 → 夜
        "midnight": ["night"],                             # 深夜 → 夜
        # 基本時間帯（変換不要）
        "morning": ["daytime"],                            # 朝 → 昼間
        "night": [],
        "daytime": [],                                     # 昼間（基本）
    }
    
    def get_time_fallbacks(time_name: str) -> list:
        """時間帯名のフォールバックリストを生成（元の名前を含む）"""
        result = [time_name]
        if time_name in TIME_FALLBACK_MAP:
            result.extend(TIME_FALLBACK_MAP[time_name])
        return result
    
    season_fallback = get_season_fallback_order(effective_season)
    time_fallbacks = get_time_fallbacks(effective_time_of_day)
    
    # --- 検索対象候補（優先順位順）を構築 ---
    candidates = []
    
    # 1. 季節 + 時間帯の組み合わせ（時間帯フォールバック対応）
    for time_name in time_fallbacks:
        # 1a. 現在季節 + 時間帯
        candidates.append(f"{location_id}_{effective_season}_{time_name}.png")
        # 1b. 他の季節 + 同じ時間帯
        for season in season_fallback[1:]:
            candidates.append(f"{location_id}_{season}_{time_name}.png")
    
    # 2. 時間帯のみ（フォールバック対応）
    for time_name in time_fallbacks:
        candidates.append(f"{location_id}_{time_name}.png")
    
    # 3. 季節のみ（現在季節）
    candidates.append(f"{location_id}_{effective_season}.png")
    
    # 4. デフォルト
    candidates.append(f"{location_id}.png")

    # 直接一致を確認
    for cand in candidates:
        path = os.path.join(image_dir, cand)
        if os.path.exists(path):
            return path

    # ワイルドカード検索（接頭辞一致。例: '書斎_night_2.png' なども許容）
    try:
        files = os.listdir(image_dir)
        search_prefixes = []
        
        # 季節 + 時間帯パターン（時間帯フォールバック対応）
        for time_name in time_fallbacks:
            search_prefixes.append(f"{location_id}_{effective_season}_{time_name}_")
            for season in season_fallback[1:]:
                search_prefixes.append(f"{location_id}_{season}_{time_name}_")
        
        # 時間帯のみパターン
        for time_name in time_fallbacks:
            search_prefixes.append(f"{location_id}_{time_name}_")
        
        # 【修正v2】「季節のみ」パターンを削除
        # 理由: `書斎_winter_` が `書斎_winter_midnight.png` にマッチし、
        # 昼間に夜画像が選ばれてしまう問題があったため
        
        # 既知の時間帯名（フィルタリング用）
        ALL_TIME_NAMES = {"early_morning", "morning", "late_morning", "noon", 
                          "afternoon", "evening", "night", "midnight", "daytime"}
        
        for prefix in search_prefixes:
            for f in files:
                if f.lower().startswith(prefix.lower()) and f.lower().endswith('.png'):
                    return os.path.join(image_dir, f)
        
        # 場所のみパターン（最低優先度）
        # 時間帯名を含むファイルは除外（例: 書斎_winter_midnight.pngを拾わない）
        location_prefix = f"{location_id}_"
        for f in files:
            if f.lower().startswith(location_prefix.lower()) and f.lower().endswith('.png'):
                # ファイル名から時間帯名を含むかチェック
                basename_lower = f.lower()
                contains_time = any(f"_{t}_" in basename_lower or f"_{t}." in basename_lower 
                                    for t in ALL_TIME_NAMES)
                if not contains_time:
                    return os.path.join(image_dir, f)

        # 【修正v3: 最終手段 (Desperation Fallback)】
        # 上記すべてで見つからず、それでも画像がある場合は、とにかく何かを表示する。
        # 例: 昼間に `_daytime.png` しかなく、daytimeフォールバックも漏れた場合など。
        # 特定の場所に来ているのに、画像があるのに何も出ないよりはマシ。
        for f in files:
            if f.lower().startswith(location_prefix.lower()) and f.lower().endswith('.png'):
                print(f"[Scenery Fallback] 最終手段として画像を選択: {f}")
                return os.path.join(image_dir, f)

    except Exception as e:
        print(f"警告: 情景画像検索中にエラー: {e}")

    return None

def parse_world_file(file_path: str) -> dict:
    if not os.path.exists(file_path): return {}
    with open(file_path, "r", encoding="utf-8") as f: content = f.read()
    world_data = {}; current_area_key = None; current_place_key = None
    lines = content.splitlines()
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

def delete_and_get_previous_user_input(log_file_path: str, ai_message_to_delete: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """
    AIメッセージを削除し、その直前のユーザー入力を取得して返す。
    Returns: (restored_input, deleted_timestamp)
    """
    if not log_file_path or not ai_message_to_delete: return None, None
    room_dir = _get_room_dir_from_path(log_file_path)
    if not os.path.exists(room_dir): return None, None
    
    try:
        all_messages = load_chat_log(log_file_path)
        target_start_index = -1
        deleted_timestamp = None
        for i, msg in enumerate(all_messages):
            if (msg.get("content") == ai_message_to_delete.get("content") and msg.get("responder") == ai_message_to_delete.get("responder")):
                target_start_index = i
                # タイムスタンプを抽出
                content = msg.get("content", "")
                responder = msg.get("responder", "")
                
                # 1. コンテンツ末尾から抽出
                match = re.search(r'(\d{2}:\d{2}:\d{2})(?: \| .*)?$', content)
                if match:
                    deleted_timestamp = match.group(1)
                else:
                    # 2. レスポンダー表示値（ヘッダー行）から抽出
                    match = re.search(r'(\d{2}:\d{2}:\d{2})$', responder)
                    if match:
                        deleted_timestamp = match.group(1)
                break
        if target_start_index == -1: return None, None
        
        last_user_message_index = -1
        for i in range(target_start_index - 1, -1, -1):
            if all_messages[i].get("role") == "USER":
                last_user_message_index = i; break
        if last_user_message_index == -1: return None, deleted_timestamp
        
        user_message_content = all_messages[last_user_message_index].get("content", "")
        messages_to_keep = all_messages[:last_user_message_index]
        
        # 月次分割対応: 書き戻し処理の共通化
        _write_segmented_logs(room_dir, messages_to_keep)

        content_without_timestamp = re.sub(r'\n\n\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}$', '', user_message_content, flags=re.MULTILINE)
        restored_input = content_without_timestamp.strip()
        print("--- Successfully reset conversation to the last user input for rerun ---")
        return restored_input, deleted_timestamp
    except Exception as e:
        print(f"エラー: 再生成のためのログ削除中に予期せぬエラー: {e}"); traceback.print_exc()
        return None, None

@contextlib.contextmanager
def capture_prints():
    original_stdout = sys.stdout; original_stderr = sys.stderr
    string_io = io.StringIO()
    sys.stdout = string_io; sys.stderr = string_io
    try: yield string_io
    finally: sys.stdout = original_stdout; sys.stderr = original_stderr

def delete_user_message_and_after(log_file_path: str, user_message_to_delete: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """
    ユーザーメッセージ以降をすべて削除する。
    Returns: (restored_input, None) ※戻り値の型を合わせるためのダミー
    """
    if not log_file_path or not user_message_to_delete: return None, None
    room_dir = _get_room_dir_from_path(log_file_path)
    if not os.path.exists(room_dir): return None, None
    
    try:
        all_messages = load_chat_log(log_file_path)
        target_index = -1
        for i, msg in enumerate(all_messages):
            if (msg.get("content") == user_message_to_delete.get("content") and msg.get("responder") == user_message_to_delete.get("responder")):
                target_index = i; break
        if target_index == -1: return None, None
        user_message_content = all_messages[target_index].get("content", "")
        messages_to_keep = all_messages[:target_index]
        
        # 月次分割対応: 書き戻し処理の共通化
        _write_segmented_logs(room_dir, messages_to_keep)

        content_without_timestamp = re.sub(r'\n\n\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}$', '', user_message_content, flags=re.MULTILINE)
        restored_input = content_without_timestamp.strip()
        print("--- Successfully reset conversation to before the selected user input for rerun ---")
        return restored_input, None
    except Exception as e:
        print(f"エラー: ユーザー発言以降のログ削除中に予期せぬエラー: {e}"); traceback.print_exc()
        return None, None

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

def _get_current_time_context(room_name: str) -> Tuple[str, str]:
    """
    ルームの時間設定を読み込み、現在適用すべき季節と時間帯の「英語名」を返す。
    循環参照を避けるため、utils.pyに配置する。
    戻り値: (season_en, time_of_day_en)
    """
    # 循環参照を避けるため、ここでローカルインポート
    import room_manager
    import datetime

    room_config = room_manager.get_room_config(room_name)
    settings = (room_config or {}).get("time_settings", {})
    
    mode = settings.get("mode", "realtime")

    if mode == "fixed":
        season_en = settings.get("fixed_season", "autumn")
        time_en = settings.get("fixed_time_of_day", "night")
        return season_en, time_en
    else:
        now = datetime.datetime.now()
        season_en = get_season(now.month)
        time_en = get_time_of_day(now.hour)
        return season_en, time_en

def get_last_log_timestamp(room_name: str) -> datetime.datetime:
    """
    指定されたルームのログの「最後のメッセージ」のタイムスタンプを取得する。
    取得できない場合は、現在時刻を返す（無限ループ防止のため）。
    """
    import room_manager # 循環参照回避
    log_path, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    
    if not log_path or not os.path.exists(log_path):
        return datetime.datetime.now()

    try:
        # ファイルの末尾から少しだけ読み込む（効率化）
        # ※平均的なメッセージサイズを考慮し、末尾4KB程度を読む
        file_size = os.path.getsize(log_path)
        read_size = min(4096, file_size)
        
        with open(log_path, 'rb') as f:
            if file_size > read_size:
                f.seek(file_size - read_size)
            content = f.read().decode('utf-8', errors='ignore')

        # タイムスタンプパターン (YYYY-MM-DD (Day) HH:MM:SS)
        # ログ形式: 2025-12-03 (Wed) 17:26:19
        matches = list(re.finditer(r'(\d{4}-\d{2}-\d{2}) \(...\) (\d{2}:\d{2}:\d{2})', content))
        
        if matches:
            last_match = matches[-1]
            date_str = last_match.group(1)
            time_str = last_match.group(2)
            dt_str = f"{date_str} {time_str}"
            return datetime.datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
            
    except Exception as e:
        print(f"タイムスタンプ取得エラー ({room_name}): {e}")
        
    # 取得失敗時は「今」とみなしてトリガーを防ぐ
    return datetime.datetime.now()

def is_in_quiet_hours(start_str: str, end_str: str) -> bool:
    """現在時刻が通知禁止時間帯（開始〜終了）に含まれるか判定する"""
    if not start_str or not end_str:
        return False
        
    now = datetime.datetime.now().time()
    try:
        start = datetime.datetime.strptime(start_str, "%H:%M").time()
        end = datetime.datetime.strptime(end_str, "%H:%M").time()
        
        if start <= end:
            # 例: 01:00 〜 05:00
            return start <= now <= end
        else:
            # 例: 23:00 〜 07:00 (日付またぎ)
            return start <= now or now <= end
    except ValueError:
        return False

# utils.py の末尾に追加

def get_content_as_string(message) -> str:
    """
    LangChainのメッセージオブジェクトまたは文字列から、テキストコンテンツを安全に抽出する。
    マルチモーダル（リスト形式）のコンテンツにも対応。
    """
    if isinstance(message, str):
        return message
    
    content = getattr(message, 'content', '')
    
    if isinstance(content, str):
        return content
    
    if isinstance(content, list):
        # リストの場合（マルチモーダル）、type='text' の部分を結合する
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'text':
                text_parts.append(part.get('text', ''))
            elif isinstance(part, str):
                text_parts.append(part)
        return "\n".join(text_parts)
        
    return str(content)


def resize_image_for_api(
    image_source: Union[str, "Image.Image"], 
    max_size: int = 512,
    return_image: bool = False
) -> Optional[Union[str, "Image.Image"]]:
    """
    画像をリサイズし、Base64エンコードした文字列またはPIL Imageを返す。
    APIへの送信前に呼び出すことで、トークン消費を削減できる。
    
    Args:
        image_source: 画像ファイルのパス または PIL.Imageオブジェクト
        max_size: 最大辺のピクセル数（デフォルト512）
        return_image: Trueの場合、Base64ではなくPIL Imageを返す
    
    Returns:
        Base64エンコードされた画像文字列 または PIL Image。失敗時はNone。
    """
    try:
        from PIL import Image
        import base64
        
        # 入力がパスかPIL Imageかを判定
        if isinstance(image_source, str):
            if not image_source or not os.path.exists(image_source):
                return None
            img = Image.open(image_source)
            should_close = True
        elif hasattr(image_source, 'size') and hasattr(image_source, 'mode'):
            # PIL Imageオブジェクト
            img = image_source.copy()  # 元の画像を変更しないようにコピー
            should_close = False
        else:
            print(f"警告: resize_image_for_api: 不明な入力タイプ: {type(image_source)}")
            return None
        
        try:
            # リサイズが必要かチェック
            original_size = max(img.size)
            if original_size > max_size:
                # アスペクト比を維持してリサイズ
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
                print(f"  - [Image Resize] {original_size}px -> {max(img.size)}px")
            
            # RGBAの場合はRGBに変換（PNGの透過対応）
            if img.mode == 'RGBA':
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3])
                img = background
            
            if return_image:
                return img
            
            # Base64エンコードして返す（元の形式を維持）
            buffer = io.BytesIO()
            # 元の形式を取得（不明な場合はPNGにフォールバック）
            output_format = img.format or "PNG"
            # JPEGの場合はRGBモードが必要
            if output_format.upper() in ("JPEG", "JPG") and img.mode != "RGB":
                img = img.convert("RGB")
            img.save(buffer, format=output_format, optimize=True)
            return base64.b64encode(buffer.getvalue()).decode("utf-8"), output_format.lower()
        finally:
            if should_close and hasattr(img, 'close'):
                img.close()
            
    except Exception as e:
        source_info = image_source if isinstance(image_source, str) else f"PIL Image ({type(image_source)})"
        print(f"警告: 画像のリサイズに失敗しました ({source_info}): {e}")
        return None

