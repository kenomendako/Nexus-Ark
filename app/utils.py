# utils.py (完全最終版)

class ModelSpecificResourceExhausted(Exception):
    """
    特定のモデルで 429 (ResourceExhausted) が発生したことを示す例外。
    上位の invoke_nexus_agent_stream で正しいモデルに対して枯渇マークを付けるために使用する。
    """
    def __init__(self, original_exception: Exception, model_name: str):
        self.original_exception = original_exception
        self.model_name = model_name
        super().__init__(str(original_exception))

import datetime
import os
import re
import traceback
import html
from typing import List, Dict, Optional, Tuple, Union, Any, Callable
import constants
import sys
try:
    import psutil
except ImportError:
    psutil = None
from pathlib import Path
import json
import time
import uuid
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
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
        if pid and psutil and psutil.pid_exists(pid):
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

# --- ログ読み込みキャッシュ ---
# キー: ファイルパス, 値: (mtime, メッセージリスト)
_file_log_cache: Dict[str, Tuple[float, List[Dict[str, str]]]] = {}

def invalidate_chat_log_cache(file_path: str):
    """指定ファイルに関連するログキャッシュを無効化する。"""
    # ファイル単位キャッシュなので、該当ファイルのキーを削除するだけでよい
    # (念のため logs_dir 全体のキャッシュ削除ロジックも残すが、_file_log_cache が主)
    if file_path in _file_log_cache:
        del _file_log_cache[file_path]
        # print(f"[DEBUG:LogCache] ファイルキャッシュ無効化: {os.path.basename(file_path)}")

# ... (backup_and_repair_json は省略) ...

def load_chat_log(file_path: str, single_file_only: bool = False) -> List[Dict[str, str]]:
    """
    (Definitive Edition v7: File-level Caching)
    Reads log files and returns a unified list of dictionaries.
    ファイル単位でキャッシュを行うため、一部のファイルが更新されても
    他の過去ログファイルのキャッシュは有効活用され、読み込みが高速化される。
    """
    messages: List[Dict[str, str]] = []
    if not file_path:
        return messages

    # --- [NEW] 月次分割対応: ルームディレクトリの特定 ---
    room_dir = _get_room_dir_from_path(file_path)
    logs_dir = os.path.join(room_dir, constants.LOGS_DIR_NAME)
    
    # --- [NEW] 月次分割対応: ターゲットファイルの決定 ---
    target_files = []
    
    # 移行（マイグレーション）を先に実行する。実行後に target_files を選定することで
    # リネーム後のファイルを正しく参照できるようにする。
    legacy_log = os.path.join(room_dir, "log.txt")
    if os.path.exists(legacy_log):
        _migrate_chat_logs(room_dir)

    # single_file_only かつ 指定パスがファイルとして存在する場合
    if single_file_only and os.path.isfile(file_path):
        target_files = [file_path]
    elif os.path.exists(logs_dir) and os.path.isdir(logs_dir):
        # logs/ 内の全ファイルを日付順に取得
        target_files = sorted(glob.glob(os.path.join(logs_dir, "*.txt")))
        
        # logs/ 内にファイルがなく、かつ log.txt が存在する場合（移行失敗などの保険）
        if not target_files and os.path.exists(legacy_log):
             target_files = [legacy_log]
             
    elif os.path.exists(file_path):
        target_files = [file_path]

    if not target_files:
        # --- [NEW] 移行直後への配慮 ---
        # 移行直後は log.txt が消えているが、logs/ 内のファイルとして読み込まれるべき。
        # single_file_only=True の場合でも、移行済みなら logs/ 全体から読み込むように緩和する。
        if single_file_only and os.path.exists(logs_dir):
            target_files = sorted(glob.glob(os.path.join(logs_dir, "*.txt")))
        
        if not target_files:
            return messages

    # --- [読み込みループ (ファイル単位キャッシュ)] ---
    # ヘッダーパターンコンパイル
    header_pattern = re.compile(r'^## (USER|AGENT|SYSTEM):(.+?)$', re.MULTILINE)

    for f_path in target_files:
        try:
            mtime = os.path.getmtime(f_path)
        except OSError:
            continue

        # キャッシュチェック
        file_messages = None
        if f_path in _file_log_cache:
            cached_mtime, cached_msgs = _file_log_cache[f_path]
            if cached_mtime == mtime:
                file_messages = cached_msgs
        
        if file_messages is None:
            # キャッシュミスまたは更新検知 -> 読み込み
            try:
                with open(f_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception as e:
                print(f"エラー: ログファイル '{f_path}' 読込エラー: {e}")
                continue

            if not content.strip():
                file_messages = []
            else:
                current_file_msgs = []
                matches = list(header_pattern.finditer(content))
                for i, match in enumerate(matches):
                    role = match.group(1).upper()
                    responder = match.group(2).strip()
                    if role == "USER":
                        responder = "user"
                    start_of_content = match.end()
                    end_of_content = matches[i + 1].start() if i + 1 < len(matches) else len(content)
                    message_content = content[start_of_content:end_of_content].strip()
                    current_file_msgs.append({"role": role, "responder": responder, "content": message_content})
                file_messages = current_file_msgs
            
            # キャッシュ更新
            _file_log_cache[f_path] = (mtime, file_messages)

        # 全体リストに追加
        if file_messages:
            messages.extend(file_messages)

    # --- [デバッグログ出力] ---
    # ファイル単位キャッシュの効果を可視化
    # 呼び出し元が single_file_only=True の場合は冗長になるので表示しない
    if not single_file_only and target_files:
        try:
            cache_hits = 0
            for f in target_files:
                if f in _file_log_cache and _file_log_cache[f][0] == os.path.getmtime(f):
                    cache_hits += 1
            
            room_name = os.path.basename(os.path.dirname(logs_dir))
            print(f"[DEBUG:LogCache] 📥 読み込み: {room_name} ({len(messages)}件, {len(target_files)}ファイル, Hit:{cache_hits})")
        except:
            pass

    return messages

    return messages

def load_chat_log_lazy(
    room_dir: str, 
    limit: Optional[int] = None, 
    min_turns: int = 0,
    cutoff_date: Optional[str] = None,
    limit_validator: Optional[Callable[[Dict[str, Any]], bool]] = None,
    return_full_info: bool = False
) -> Union[Tuple[List[Dict[str, Any]], bool], Tuple[List[Dict[str, Any]], bool, int]]:
    import time
    perf_start = time.time()
    """
    チャットログを新しい順に読み込み、指定された制限(limit)や日付(cutoff_date)に達したら停止する。
    
    Args:
        room_dir: ルームディレクトリのパス (または logs ディレクトリそのもの)
        limit: 読み込む最大メッセージ数（limit_validator適用後の件数）
        min_turns: (Deprecated) 最小ターン数。現在は limit を優先して使用する。
        cutoff_date: この日付(YYYY-MM-DD string)より前のログは読み込まない
        limit_validator: limitカウント対象とするメッセージを判定する関数 (msg -> bool)。
                         指定された場合、この関数がTrueを返すメッセージのみをlimit件数としてカウントする。
                         （非表示のシステムログなどを件数に含めないために使用）
    
    Returns:
        (messages, has_more)
        messages: 時系列順（古い -> 新しい）のメッセージリスト
        has_more: さらに過去のログが存在するかどうか
    """
    # room_dir を正規化（末尾のスラッシュを除去してOS不問にする）
    room_dir = room_dir.replace("\\", "/").rstrip("/")
    logs_dir = os.path.join(room_dir, constants.LOGS_DIR_NAME).replace("\\", "/")
    
    if not os.path.exists(logs_dir):
        # もし渡された room_dir 自体が logs フォルダを指している場合の救済措置
        if room_dir.endswith(f"/{constants.LOGS_DIR_NAME}") and os.path.exists(room_dir):
            logs_dir = room_dir
        else:
            legacy_log = os.path.join(room_dir, "log.txt")
            if os.path.exists(legacy_log):
                _migrate_chat_logs(room_dir) # [Fix] lazy時も確実に移行を試みる
                # 移行後は logs_dir が作成されているはずなので、この if ブロックを抜けて
                # 下方の標準的な logs/ 読み込みロジックに合流させる
            else:
                # log.txt も logs/ もない場合のみ logs_dir を作成して空の結果を返す
                os.makedirs(logs_dir, exist_ok=True)
                return ([], False, 0) if return_full_info else ([], False)

    # 全ファイルを昇順（古い順）で取得
    all_files = sorted(glob.glob(os.path.join(logs_dir, "*.txt")))
    
    # --- [NEW] 移行処理の割り込み (logs/ が既にあっても log.txt が残っている場合) ---
    legacy_log = os.path.join(room_dir, "log.txt")
    if os.path.exists(legacy_log):
        _migrate_chat_logs(room_dir)
        # 移行が成功すれば all_files が更新される必要があるため、再度 glob する
        all_files = sorted(glob.glob(os.path.join(logs_dir, "*.txt")))

    # 絶対インデックス計算のためのオフセット
    # [v2] cutoff_date による「ファイル単位のスキップ」を廃止し、全てのファイルを逆順にチェックする。
    # これにより、min_turns を満たすまで月を遡ってスキャンが可能になる。
    files_to_scan = all_files
    offset_from_skipped_files = 0

    # --- [True Lazy Loading] 新しい順にファイルをロードして limit に達したら停止 ---
    full_log_buffer = []
    slice_start_index = 0
    has_more = False
    
    intra_file_skipped_count = 0
    has_more = False
    
    # 日付フィルタを適用したファイル群
    current_f_idx = -1
    for f_idx, f_path in enumerate(reversed(files_to_scan)):
        current_f_idx = f_idx
        current_file_msgs = load_chat_log(f_path, single_file_only=True)
        original_file_len = len(current_file_msgs)
        
        # cutoff_date によるフィルタリングをファイル単位で実施
        if cutoff_date:
            filtered = []
            # 元の修正案(d30424c)に合わせ、メッセージ末尾の日付を確実に捉える
            date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})')
            
            # ファイル内のメッセージを逆順にチェック
            for msg in reversed(current_file_msgs):
                content = msg.get('content', '')
                matches = date_pattern.findall(content)
                msg_date = matches[-1] if matches else "9999-12-31" # 日付なしは一旦残す
                
                if msg_date < cutoff_date:
                    if min_turns > 0 and (len(full_log_buffer) + len(filtered)) < min_turns:
                        # まだ最低維持件数に満たないので、日付を無視して含める
                        pass
                    else:
                        has_more = True
                        break
                filtered.insert(0, msg)
            
            # このファイル内でスキップされた件数を記録
            intra_file_skipped_count = original_file_len - len(filtered)
            current_file_msgs = filtered

        # バッファの先頭に追加
        full_log_buffer = current_file_msgs + full_log_buffer
        
        # limit チェック
        valid_count = len(full_log_buffer)
        if limit_validator:
            valid_count = sum(1 for m in full_log_buffer if limit_validator(m))
        
        if limit and valid_count >= limit:
            if f_idx < len(files_to_scan) - 1:
                has_more = True
            break
        
        if has_more:
            break

    # 未読み込みの古いファイルがある場合、オフセットに加算
    real_f_idx = len(files_to_scan) - 1 - current_f_idx
    remaining_files = files_to_scan[:real_f_idx]
    for f_rem in remaining_files:
        msgs = load_chat_log(f_rem, single_file_only=True)
        offset_from_skipped_files += len(msgs)

    # スライス処理
    loaded_messages, slice_has_more, slice_start_index = _slice_messages(full_log_buffer, limit, limit_validator)
    has_more = has_more or slice_has_more
    
    # 最終的な絶対開始インデックス = (スキップした過去ファイル全件) + (最後に読み込んだファイル内のスキップ分) + (スライス開始位置)
    absolute_start_index = offset_from_skipped_files + intra_file_skipped_count + slice_start_index

    # perf_end = time.time()
    # print(f"--- [PERF] load_chat_log_lazy: total={perf_end - perf_start:.4f}s (room_dir={room_dir}, limit={limit}, cutoff={cutoff_date}) ---")

    if return_full_info:
        return loaded_messages, has_more, absolute_start_index
    else:
        return loaded_messages, has_more

def _slice_messages(messages: List[Dict], limit: Optional[int], limit_validator: Optional[Callable]) -> Tuple[List[Dict], bool, int]:
    """メッセージリストを制限に従ってスライスし、(切り出されたリスト, has_more, リスト内開始位置) を返す。"""
    if not limit or len(messages) == 0:
        return messages, False, 0
    if limit_validator:
        valid_count = 0
        slice_index = 0
        found_limit = False
        for idx in range(len(messages) - 1, -1, -1):
            if limit_validator(messages[idx]):
                valid_count += 1
            if valid_count >= limit:
                slice_index = idx
                found_limit = True
                break
        if found_limit:
            return messages[slice_index:], (slice_index > 0), slice_index
        else:
            return messages, False, 0
    else:
        if limit and len(messages) > limit:
            slice_index = len(messages) - limit
            return messages[slice_index:], True, slice_index
        else:
            return messages, False, 0


def get_message_by_absolute_index(room_dir: str, target_index: int) -> Optional[Dict[str, Any]]:
    """
    指定された絶対インデックス(target_index)に対応するメッセージを、
    全ログをメモリに展開することなく、キャッシュを活用して効率的に取得する。
    
    Args:
        room_dir: ルームディレクトリのパス
        target_index: 取得したいメッセージの絶対インデックス（0開始）
        
    Returns:
        メッセージ(Dict) または 見つからない場合は None
    """
    if target_index < 0:
        return None

    room_dir = room_dir.replace("\\", "/").rstrip("/")
    logs_dir = os.path.join(room_dir, constants.LOGS_DIR_NAME).replace("\\", "/")
    
    if not os.path.exists(logs_dir):
        return None

    # 全ファイルを古い順に取得
    target_files = sorted(glob.glob(os.path.join(logs_dir, "*.txt")))
    
    current_base_index = 0
    for f_path in target_files:
        # load_chat_log(f_path, single_file_only=True) はキャッシュを最大限活用する
        # (内部で _file_log_cache を参照し、mtimeに変更がなければディスクI/Oをスキップする)
        file_msgs = load_chat_log(f_path, single_file_only=True)
        file_len = len(file_msgs)
        
        if current_base_index <= target_index < current_base_index + file_len:
            # このファイルの中に目的のメッセージがある
            relative_index = target_index - current_base_index
            return file_msgs[relative_index]
        
        current_base_index += file_len
        
    return None


def _get_room_dir_from_path(file_path: str) -> str:
    """
    ファイルパスからルームディレクトリを特定するヘルパー。
    logs ディレクトリを基準に親ディレクトリを返す。
    """
    if not file_path: return ""
    
    # パスを正規化してパーツに分割
    clean_path = file_path.replace("\\", "/").rstrip("/")
    parts = clean_path.split("/")
    
    # 逆順に 'logs' フォルダを探す（最新のパス構成を優先）
    if constants.LOGS_DIR_NAME in parts:
        idx = parts.index(constants.LOGS_DIR_NAME)
        # 'logs' より前のパーツを再結合して room_dir とする
        room_dir = "/".join(parts[:idx])
        return room_dir if room_dir else "."
    
    # 見つからない場合は単純に親ディレクトリを返す
    return os.path.dirname(file_path)

def _migrate_chat_logs(room_dir: str):
    """
    既存の log.txt および log_archives を新形式 (logs/YYYY-MM.txt) に自動移行する。
    
    【安全設計】
    - 月次ファイルが1つでも存在すれば移行済みと判断し、何もしない
    - マーカーファイル (.migration_done) による重複実行防止
    - 月の再配分以外の余計な処理（ソート、内容変更等）は一切行わない
    """
    if not room_dir:
        return
        
    # グローバルキャッシュでチェック (セッションごとの重複実行防止)
    global _MIGRATION_DONE_CACHE
    if room_dir in _MIGRATION_DONE_CACHE:
        return

    new_logs_dir = os.path.join(room_dir, constants.LOGS_DIR_NAME)

    # 【安全策1】マーカーファイルが存在すれば移行済み → 即座に return
    marker_file = os.path.join(new_logs_dir, ".migration_done")
    if os.path.exists(marker_file):
        _MIGRATION_DONE_CACHE.add(room_dir)
        return

    # 【安全策2】月次ファイルが1つでも存在すれば移行済みとみなす
    if os.path.isdir(new_logs_dir):
        existing_monthly = glob.glob(os.path.join(new_logs_dir, "20??-??.txt"))
        if existing_monthly:
            print(f"--- [Log Migration] 月次ファイルが既に存在するため移行をスキップ: {room_dir} ---")
            # マーカーファイルを作成して次回以降のチェックを高速化
            try:
                with open(marker_file, "w") as f:
                    f.write(f"migrated_at={datetime.datetime.now().isoformat()}\n")
            except Exception:
                pass
            _MIGRATION_DONE_CACHE.add(room_dir)
            return

    legacy_log = os.path.join(room_dir, "log.txt")
    legacy_archives_dir = os.path.join(room_dir, "log_archives")
    
    # legacy_log が存在しない場合は移行の必要なし
    # (log_archives だけが存在しても、 *.txt が無ければ対象なし)
    has_legacy_log = os.path.exists(legacy_log)
    has_archive_files = False
    if os.path.exists(legacy_archives_dir):
        archive_txt_files = glob.glob(os.path.join(legacy_archives_dir, "*.txt"))
        has_archive_files = len(archive_txt_files) > 0
    
    if not has_legacy_log and not has_archive_files:
        # 移行対象なし → マーカーを作成してスキップ
        os.makedirs(new_logs_dir, exist_ok=True)
        try:
            with open(marker_file, "w") as f:
                f.write(f"migrated_at={datetime.datetime.now().isoformat()}\nno_legacy_files=true\n")
        except Exception:
            pass
        _MIGRATION_DONE_CACHE.add(room_dir)
        return

    print(f"--- [Log Migration] 既存のログを新しい月次形式に整理しています: {room_dir} ---")
    os.makedirs(new_logs_dir, exist_ok=True)
    
    # 【追加: 二重実行防止】ファイルを即座にリネームして、他プロセスが対象を見失うようにする
    migration_targets = []
    
    # 1. log.txt の処理
    if has_legacy_log:
        try:
            # log.txt -> log.txt.migrating
            tmp_legacy = legacy_log + ".migrating"
            if os.path.exists(tmp_legacy):
                # 既にリネーム済みファイルがある場合は、以前の処理が中断された可能性があるためそれを使う
                migration_targets.append(tmp_legacy)
            else:
                os.rename(legacy_log, tmp_legacy)
                migration_targets.append(tmp_legacy)
        except Exception as e:
            print(f"--- [Log Migration Warning] log.txt のリネームに失敗（他プロセスが処理中？）: {e} ---")

    # 2. log_archives/*.txt の処理
    if has_archive_files:
        for f_path in glob.glob(os.path.join(legacy_archives_dir, "*.txt")):
            try:
                # archive.txt -> archive.txt.migrating
                tmp_archive = f_path + ".migrating"
                if os.path.exists(tmp_archive):
                    migration_targets.append(tmp_archive)
                else:
                    os.rename(f_path, tmp_archive)
                    migration_targets.append(tmp_archive)
            except Exception as e:
                print(f"--- [Log Migration Warning] {os.path.basename(f_path)} のリネームに失敗: {e} ---")

    if not migration_targets:
        print("--- [Log Migration] 移行対象がなくなりました（処理済み） ---")
        return

    # タイムスタンプ抽出用
    # メッセージ内の "YYYY-MM-DD (Tue) HH:MM:SS" 形式を優先
    date_pattern = re.compile(r'(\d{4}-\d{2})-\d{2}')
    
    for f_path in migration_targets:
        messages_by_month = {}
        try:
            # 既存の読み込みロジックを一時的に流用して個別のメッセージに分解
            raw_content = ""
            with open(f_path, "r", encoding="utf-8") as f:
                raw_content = f.read()
            
            if not raw_content.strip():
                # 空ファイルの場合はそのまま消す
                os.remove(f_path)
                continue
            
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
            # .migrating を除去してから .migrated を付与
            backup_path = f_path.replace(".migrating", "") + ".migrated"
            
            # 同名のバックアップが既にある場合は、既存のものを上書きするか削除する
            if os.path.exists(backup_path):
                try: os.remove(backup_path)
                except: pass
            
            os.rename(f_path, backup_path)
            
        except Exception as e:
            print(f"!!! [Log Migration Error] {f_path} の移行中にエラー: {e}")
            traceback.print_exc()

    # マーカーファイルを作成
    try:
        with open(marker_file, "w") as f:
            f.write(f"migrated_at={datetime.datetime.now().isoformat()}\n")
    except Exception:
        pass

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
        
        # ログ書き込み後にキャッシュを無効化
        invalidate_chat_log_cache(log_file_path)
        
        return None
    except Exception as e:
        print(f"エラー: ログ保存エラー: {e}"); traceback.print_exc()
        return None

def delete_message_from_log(log_file_path: str, message_to_delete: Dict[str, str]) -> Optional[str]:
    """
    ログからメッセージを削除し、成功した場合は削除されたメッセージのタイムスタンプ(HH:MM:SS)を返す。

    【安全設計】対象メッセージが含まれる単一ファイルのみを修正する。
    他の月のログファイルには一切触れない。月を跨いだ再配分は行わない。
    """
    if not log_file_path or not message_to_delete: return None
    room_dir = _get_room_dir_from_path(log_file_path)
    if not os.path.exists(room_dir): return None

    logs_dir = os.path.join(room_dir, constants.LOGS_DIR_NAME)
    target_content = message_to_delete.get("content", "")
    target_responder = message_to_delete.get("responder", "")

    if not target_content:
        print("警告: 削除対象メッセージのコンテンツが空です。")
        return None

    try:
        # 月次ログファイルを1つずつスキャンし、対象メッセージを含むファイルを特定する
        log_files = sorted(glob.glob(os.path.join(logs_dir, "*.txt")))
        if not log_files:
            print("警告: ログファイルが見つかりませんでした。")
            return None

        header_pattern = re.compile(r'^## (USER|AGENT|SYSTEM):(.+?)$', re.MULTILINE)

        for f_path in log_files:
            try:
                with open(f_path, "r", encoding="utf-8") as f:
                    raw_content = f.read()
            except Exception as e:
                print(f"警告: ファイル読み込みエラー ({os.path.basename(f_path)}): {e}")
                continue

            if not raw_content.strip():
                continue

            # このファイル内のメッセージを解析
            matches = list(header_pattern.finditer(raw_content))
            found_index = -1  # 削除対象のメッセージインデックス (finditer結果内)

            for i, match in enumerate(matches):
                role = match.group(1).upper()
                responder = match.group(2).strip()
                if role == "USER":
                    responder = "user"
                start_of_content = match.end()
                end_of_content = matches[i + 1].start() if i + 1 < len(matches) else len(raw_content)
                message_content = raw_content[start_of_content:end_of_content].strip()

                if message_content == target_content and responder == target_responder:
                    found_index = i
                    break

            if found_index < 0:
                continue  # このファイルには対象メッセージがない → 次のファイルへ

            # --- 対象メッセージを発見！このファイルのみを修正する ---
            print(f"--- [Log Delete] 対象メッセージを {os.path.basename(f_path)} で発見 ---")

            # タイムスタンプを抽出
            deleted_timestamp = None
            ts_match = re.search(r'(\d{2}:\d{2}:\d{2})(?: \| .*)?$', target_content)
            if ts_match:
                deleted_timestamp = ts_match.group(1)
            else:
                ts_match = re.search(r'(\d{2}:\d{2}:\d{2})$', target_responder)
                if ts_match:
                    deleted_timestamp = ts_match.group(1)

            # バックアップを作成（このファイルのみ）
            import shutil
            bak_path = f_path + ".bak"
            try:
                shutil.copy2(f_path, bak_path)
            except Exception as e:
                print(f"警告: バックアップ作成失敗 ({os.path.basename(f_path)}): {e}")

            # 対象メッセージをテキストから除去して書き戻す
            target_match = matches[found_index]
            delete_start = target_match.start()
            if found_index + 1 < len(matches):
                delete_end = matches[found_index + 1].start()
            else:
                delete_end = len(raw_content)

            new_content = raw_content[:delete_start] + raw_content[delete_end:]
            # 先頭・末尾の余分な空行を整理
            new_content = new_content.strip() + "\n\n" if new_content.strip() else ""

            with open(f_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            print(f"--- [Log Delete] {os.path.basename(f_path)} からメッセージを削除しました ---")
            return deleted_timestamp or "00:00:00"

        # どのファイルにも見つからなかった場合
        print("警告: ログファイル内に削除対象のメッセージが見つかりませんでした。")
        return None

    except Exception as e:
        print(f"エラー: ログからのメッセージ削除中に予期せぬエラー: {e}"); traceback.print_exc()
        return None

def _write_segmented_logs(room_dir: str, messages: List[Dict[str, str]]):
    """
    【非推奨・使用禁止】
    全メッセージを日付で月別に再配分して全ファイルを上書きする関数。
    この関数は月の誤分類やデータ破壊の原因となるため、呼び出してはならない。
    互換性のために関数定義のみ残すが、実行時にエラーを出力して何もしない。
    """
    print("!!! [CRITICAL] _write_segmented_logs は非推奨です。この関数は呼び出さないでください。 !!!")
    print("!!! ログファイルの書き換えは個別ファイル単位で行ってください。 !!!")
    traceback.print_stack()
    return


def remove_thoughts_from_text(text: str) -> str:
    """
    (v2: The Definitive Thought Remover)
    テキストから、新しい `THOUGHT:` プレフィックス形式と、古い `【Thoughts】` ブロック形式の
    両方の思考ログを除去する。
    """
    if not text:
        return ""

    # 1. ブロック形式（【Thoughts】, [THOUGHT], <thinking>）を除去
    # 大文字小文字を区別せず、タグとその中身を除去
    text = re.sub(r"【Thoughts】[\s\S]*?【/Thoughts】\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[THOUGHT\][\s\S]*?\[/THOUGHT\]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<thinking>[\s\S]*?</thinking>\s*", "", text, flags=re.IGNORECASE)

    # 2. プレフィックス行形式（THOUGHT:）を除去
    lines = text.split('\n')
    cleaned_lines = [line for line in lines if not line.strip().upper().startswith("THOUGHT:")]
    text = "\n".join(cleaned_lines)

    # 3. 対にならずに残った開始タグ・終了タグのみを物理的に除去（閉じ忘れ対策）
    unpaired_tags = [r"\[THOUGHT\]", r"\[/THOUGHT\]", r"【Thoughts】", r"【/Thoughts】", r"<thinking>", r"</thinking>"]
    for tag_pat in unpaired_tags:
        text = re.sub(tag_pat, "", text, flags=re.IGNORECASE)

    return text.strip()

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
    if not tool_name: return None # tool_nameがない場合は表示しない
    if not tool_result: return f"🛠️ ツール「{tool_name}」を実行しました。"
    
    # AIへの内部的な指示（システムプロンプト的なメッセージ）を除去
    internal_msg_patterns = [
        r'\*\*このファイル編集タスクは完了しました。.*',
        r'\*\*このタスクの実行を宣言するような前置きは不要です。.*'
    ]
    for pattern in internal_msg_patterns:
        tool_result = re.sub(pattern, '', tool_result, flags=re.DOTALL).strip()
    
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
        # プロンプトを抽出して表示
        prompt_match = re.search(r'📝 Prompt: (.+?)(?:\n画像生成|$)', tool_result, re.DOTALL)
        if prompt_match:
            prompt_text = prompt_match.group(1).strip()
            # プロンプト全文をアコーディオンとして表示し、UIに露出させる
            display_text = f'新しい画像を生成しました。\n<details><summary>🖼️ 画像詳細</summary>\n{prompt_text}\n</details>'
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

def delete_and_get_previous_user_input(log_file_path: str, ai_message_to_delete: Dict[str, str], target_abs_index: Optional[int] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    AIメッセージを削除し、その直前のユーザー入力を取得して返す。
    target_abs_index が指定されている場合は、内容一致検索ではなくインデックスを優先する。
    Returns: (restored_input, deleted_timestamp)
    """
    if not log_file_path or not ai_message_to_delete: return None, None
    room_dir = _get_room_dir_from_path(log_file_path)
    if not os.path.exists(room_dir): return None, None
    
    try:
        # 【安全策】破壊的操作の前にバックアップを作成
        _create_temporary_log_backup(room_dir)

        all_messages = load_chat_log(log_file_path)
        target_start_index = -1
        deleted_timestamp = None

        # インデックスが直接指定されている場合 (v23)
        if target_abs_index is not None and 0 <= target_abs_index < len(all_messages):
            msg = all_messages[target_abs_index]
            # 念のため、指定されたインデックスの内容と対象が矛盾していないかチェック
            # (完全に一致しなくても、インデックスを優先するがログには残す)
            if msg.get("content") != ai_message_to_delete.get("content"):
                print(f"--- [WARNING:Rerun] Index mismatch? UI_Idx:{target_abs_index}, UI_Content[:20]:{ai_message_to_delete.get('content', '')[:20]}, Log_Content[:20]:{msg.get('content', '')[:20]} ---")
            
            target_start_index = target_abs_index
        else:
            # 従来の内容一致検索 (フォールバック)
            for i, msg in enumerate(all_messages):
                if (msg.get("content") == ai_message_to_delete.get("content") and msg.get("responder") == ai_message_to_delete.get("responder")):
                    target_start_index = i; break

        if target_start_index == -1:
            print(f"警告：削除対象のメッセージが見つかりませんでした。")
            return None, None
            
        # タイムスタンプ抽出
        target_msg = all_messages[target_start_index]
        content = target_msg.get("content", "")
        responder = target_msg.get("responder", "")
        match = re.search(r'(\d{2}:\d{2}:\d{2})(?: \| .*)?$', content)
        if match:
            deleted_timestamp = match.group(1)
        else:
            match = re.search(r'(\d{2}:\d{2}:\d{2})$', responder)
            if match: deleted_timestamp = match.group(1)

        # 直前のユーザー発言を探す
        last_user_message_index = -1
        for i in range(target_start_index - 1, -1, -1):
            if all_messages[i].get("role") == "USER":
                last_user_message_index = i; break
        if last_user_message_index == -1: return None, deleted_timestamp
        
        user_message_content = all_messages[last_user_message_index].get("content", "")
        
        # 月次分割対応: 書き戻し (truncate_chat_logs を使用)
        truncate_chat_logs(room_dir, last_user_message_index)

        # Action Memoryの履歴も巻き戻す
        if deleted_timestamp and deleted_timestamp != "00:00:00":
            try:
                import action_logger
                action_logger.truncate_actions_after(os.path.basename(room_dir), deleted_timestamp)
            except Exception as e:
                print(f"警告: Action Memoryの巻き戻しに失敗しました: {e}")

        content_without_timestamp = re.sub(r'\n\n\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}$', '', user_message_content, flags=re.MULTILINE)
        restored_input = content_without_timestamp.strip()
        print(f"--- Successfully reset conversation to index {last_user_message_index} for rerun ---")
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

def _create_temporary_log_backup(room_dir: str):
    """ログ編集などの破壊的操作の前に、全月次ログファイルの .bak を作成する。"""
    logs_dir = os.path.join(room_dir, constants.LOGS_DIR_NAME)
    if not os.path.isdir(logs_dir):
        return
    import shutil
    for f_name in os.listdir(logs_dir):
        if f_name.endswith(".txt"):
            src_f = os.path.join(logs_dir, f_name)
            bak_f = src_f + ".bak"
            try:
                shutil.copy2(src_f, bak_f)
            except Exception as e:
                print(f"警告: ログ編集前のバックアップ作成失敗 ({f_name}): {e}")

def delete_user_message_and_after(log_file_path: str, user_message_to_delete: Dict[str, str], target_abs_index: Optional[int] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    ユーザーメッセージ以降をすべて削除する。
    target_abs_index が指定されている場合はインデックスを優先。
    Returns: (restored_input, None)
    """
    if not log_file_path or not user_message_to_delete: return None, None
    room_dir = _get_room_dir_from_path(log_file_path)
    if not os.path.exists(room_dir): return None, None
    
    try:
        _create_temporary_log_backup(room_dir)

        all_messages = load_chat_log(log_file_path)
        target_index = -1

        if target_abs_index is not None and 0 <= target_abs_index < len(all_messages):
            target_index = target_abs_index
        else:
            for i, msg in enumerate(all_messages):
                if (msg.get("content") == user_message_to_delete.get("content") and msg.get("responder") == user_message_to_delete.get("responder")):
                    target_index = i; break

        if target_index == -1: return None, None
        user_message_content = all_messages[target_index].get("content", "")
        
        # タイムスタンプ抽出
        match = re.search(r'(\d{2}:\d{2}:\d{2})(?: \| .*)?$', user_message_content)
        deleted_timestamp = match.group(1) if match else "00:00:00"

        # truncate_chat_logs を使用
        truncate_chat_logs(room_dir, target_index)

        # Action Memoryの履歴も巻き戻す
        if deleted_timestamp and deleted_timestamp != "00:00:00":
            try:
                import action_logger
                action_logger.truncate_actions_after(os.path.basename(room_dir), deleted_timestamp)
            except Exception as e:
                print(f"警告: Action Memoryの巻き戻しに失敗しました: {e}")

        content_without_timestamp = re.sub(r'\n\n\d{4}-\d{2}-\d{2} \(...\) \d{2}:\d{2}:\d{2}$', '', user_message_content, flags=re.MULTILINE)
        restored_input = content_without_timestamp.strip()
        print(f"--- Successfully reset conversation to index {target_index} for rerun ---")
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

def _overwrite_log_file(file_path: str, messages: List[Dict]):
    """
    メッセージ辞書のリストからログファイルを完全に上書きする。
    上書き前に自動バックアップ (.bak) を作成する。
    """
    # 【安全策】上書き前にバックアップを作成
    if os.path.exists(file_path):
        bak_path = file_path + ".bak"
        try:
            import shutil
            shutil.copy2(file_path, bak_path)
        except Exception as e:
            print(f"警告: ログバックアップの作成に失敗 ({bak_path}): {e}")

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

    # ログ上書き後にキャッシュを無効化
    invalidate_chat_log_cache(file_path)

def truncate_chat_logs(room_dir: str, target_index: int):
    """
    【v24: 外科的切り詰め】
    指定された絶対インデックス(target_index)以降のメッセージをログファイルから物理的に削除する。
    1. 前半の不変な月次ファイルには一切手を付けない。
    2. インデックスが含まれるファイルのみ読み込み、切り詰めて上書きする。
    3. それより後の未来の月次ファイルは削除する。
    
    これにより、ユーザーが手動整理した過去ログを破壊せず、再生成のための巻き戻しを実現する。
    """
    logs_dir = os.path.join(room_dir, constants.LOGS_DIR_NAME)
    if not os.path.isdir(logs_dir):
        return

    # 日付順にログファイルを取得
    log_files = sorted(glob.glob(os.path.join(logs_dir, "*.txt")))
    if not log_files:
        return

    current_msg_count = 0
    header_pattern = re.compile(r'^## (USER|AGENT|SYSTEM):(.+?)$', re.MULTILINE)
    
    file_to_truncate = None
    truncate_at_msg_idx = -1
    files_to_delete = []

    # 1. どのファイルで target_index に達するかを特定
    for f_path in log_files:
        try:
            with open(f_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"警告: ログ切り詰め中の読み込みエラー ({f_path}): {e}")
            continue

        matches = list(header_pattern.finditer(content))
        file_msg_count = len(matches)
        
        if file_to_truncate is None:
            # まだ target_index が見つかっていない
            if current_msg_count + file_msg_count > target_index:
                # このファイル内に切り詰め位置がある
                file_to_truncate = f_path
                truncate_at_msg_idx = target_index - current_msg_count
                print(f"--- [Truncate] '{os.path.basename(f_path)}' のメッセージIdx:{truncate_at_msg_idx} 以降を切り詰めます ---")
            else:
                # このファイルは丸ごと「過去」なので維持
                current_msg_count += file_msg_count
        else:
            # 既に切り詰め対象ファイルが見つかった後のファイルはすべて「未来」なので削除
            files_to_delete.append(f_path)

    # 2. ファイルを切り詰める
    if file_to_truncate:
        try:
            with open(file_to_truncate, "r", encoding="utf-8") as f:
                raw_content = f.read()
            
            matches = list(header_pattern.finditer(raw_content))
            if 0 <= truncate_at_msg_idx < len(matches):
                # バックアップ(bak)作成
                bak_path = file_to_truncate + ".bak"
                import shutil
                shutil.copy2(file_to_truncate, bak_path)
                
                # 切り詰め
                cut_pos = matches[truncate_at_msg_idx].start()
                new_content = raw_content[:cut_pos].strip()
                if new_content:
                    new_content += "\n\n"
                    
                with open(file_to_truncate, "w", encoding="utf-8") as f:
                    f.write(new_content)
                
                # キャッシュ無効化
                invalidate_chat_log_cache(file_to_truncate)
        except Exception as e:
            print(f"エラー: ログファイルの切り詰め実行中に失敗 ({file_to_truncate}): {e}")

    # 3. 未来のファイルを物理削除
    for f_del in files_to_delete:
        try:
            print(f"--- [Truncate] 未来のログファイルを削除します: {os.path.basename(f_del)} ---")
            # ログ上書き後にキャッシュを無効化
            invalidate_chat_log_cache(f_del)
            
            try:
                from send2trash import send2trash
                send2trash(f_del)
            except (ImportError, Exception):
                # send2trash が使えない場合は物理削除
                if os.path.exists(f_del):
                    os.remove(f_del)
        except Exception as e:
            print(f"警告: 未来ログファイルの削除に失敗 ({f_del}): {e}")

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

    now = datetime.datetime.now()
    default_season_en = get_season(now.month)
    default_time_en = get_time_of_day(now.hour)

    if mode == "fixed":
        season_en = settings.get("fixed_season", default_season_en)
        time_en = settings.get("fixed_time_of_day", default_time_en)
        return season_en, time_en
    else:
        return default_season_en, default_time_en

def get_last_log_timestamp(room_name: str) -> datetime.datetime:
    """
    指定されたルームのログの「最後のメッセージ」のタイムスタンプを取得する。
    取得できない場合は、現在時刻を返す（無限ループ防止のため）。
    """
    import room_manager # 循環参照回避
    log_path, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    
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
    
    content = getattr(message, 'content', message)
    
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

