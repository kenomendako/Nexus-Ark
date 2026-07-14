# file_lock_utils.py
"""
ファイルロックユーティリティ

睡眠時処理と会話処理の競合を防止するための排他ロック機構。
重要なJSONファイル（episodic_memory.json等）への同時書き込みを防止する。
"""

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from filelock import FileLock, Timeout

# デフォルトのタイムアウト（秒）
DEFAULT_LOCK_TIMEOUT = 10.0
_LOCK_REGISTRY: dict[str, FileLock] = {}
_LOCK_REGISTRY_GUARD = threading.Lock()


def get_file_lock(file_path: str, timeout: float = DEFAULT_LOCK_TIMEOUT) -> FileLock:
    """
    ファイルパスに対応するFileLockを取得する。
    """
    # パスを正規化（バックスラッシュ対策）
    norm_path = Path(file_path).as_posix()
    lock_path = f"{norm_path}.lock"
    with _LOCK_REGISTRY_GUARD:
        lock = _LOCK_REGISTRY.get(lock_path)
        if lock is None:
            lock = FileLock(lock_path, timeout=timeout)
            _LOCK_REGISTRY[lock_path] = lock
        else:
            lock.timeout = timeout
        return lock


def _atomic_json_write(file_path: str, data: Any, indent: int = 2) -> None:
    path_obj = Path(file_path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=str(path_obj.parent), prefix=f"{path_obj.name}.tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
        os.replace(temp_path, file_path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def safe_append_text(file_path: str, text: str, encoding: str = "utf-8", timeout: float = DEFAULT_LOCK_TIMEOUT) -> bool:
    """ロック付きでテキストファイルへ追記する。"""
    file_path = Path(file_path).as_posix()
    lock = get_file_lock(file_path, timeout)
    try:
        with lock:
            path_obj = Path(file_path)
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "a", encoding=encoding) as f:
                f.write(text)
        return True
    except Timeout:
        print(f"⚠️ [FileLock] 追記タイムアウト: {file_path}")
        raise
    except Exception as e:
        print(f"❌ [FileLock] 追記エラー: {file_path} - {e}")
        raise


def _atomic_text_write(file_path: str, text: str, encoding: str = "utf-8") -> None:
    path_obj = Path(file_path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=str(path_obj.parent), prefix=f"{path_obj.name}.tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(temp_path, file_path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def safe_text_read(
    file_path: str,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    default: str = "",
    encoding: str = "utf-8",
) -> str:
    """ロック付きでテキストファイルを読み込む。"""
    file_path = Path(file_path).as_posix()
    path_obj = Path(file_path)
    if not path_obj.exists():
        return default

    lock = get_file_lock(file_path, timeout)
    try:
        with lock:
            with open(file_path, "r", encoding=encoding) as f:
                return f.read()
    except Timeout:
        print(f"⚠️ [FileLock] テキスト読み込みタイムアウト: {file_path}")
        raise
    except Exception as e:
        print(f"❌ [FileLock] テキスト読み込みエラー: {file_path} - {e}")
        raise


def safe_text_write(
    file_path: str,
    text: str,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    encoding: str = "utf-8",
) -> bool:
    """ロック付きでテキストファイルへアトミックに書き込む。"""
    file_path = Path(file_path).as_posix()
    lock = get_file_lock(file_path, timeout)
    try:
        with lock:
            _atomic_text_write(file_path, text, encoding=encoding)
        return True
    except Timeout:
        print(f"⚠️ [FileLock] テキスト書き込みタイムアウト: {file_path}")
        raise
    except Exception as e:
        print(f"❌ [FileLock] テキスト書き込みエラー: {file_path} - {e}")
        raise


@contextmanager
def locked_file(file_path: str, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[Path]:
    """対象ファイルのロックを保持したまま呼び出し側で読み書きする。"""
    file_path = Path(file_path).as_posix()
    lock = get_file_lock(file_path, timeout)
    try:
        with lock:
            path_obj = Path(file_path)
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            yield path_obj
    except Timeout:
        print(f"⚠️ [FileLock] ロック取得タイムアウト: {file_path}")
        raise


def safe_json_write(file_path: str, data: Any, timeout: float = DEFAULT_LOCK_TIMEOUT, indent: int = 2) -> bool:
    """
    ロック付きでJSONファイルに書き込む（アトミック書き出し）。
    
    Args:
        file_path: 書き込み先ファイルパス
        data: 書き込むデータ（JSON serializable）
        timeout: ロック取得のタイムアウト（秒）
        indent: JSONインデント
        
    Returns:
        成功時True、ロックタイムアウト時False
    """
    # パスを正規化
    file_path = Path(file_path).as_posix()
    lock = get_file_lock(file_path, timeout)
    
    try:
        with lock:
            # --- [アトミック書き出し] ---
            # 直接 open(file_path, 'w') すると、書き込み途中でOSやプロセスが落ちた際に
            # ファイルが空(0 bytes)や破損状態になる。
            # 一時ファイルに書き込んでからリネーム(os.replace)することで、この問題を回避する。
            _atomic_json_write(file_path, data, indent=indent)
                
            return True
            
    except Timeout:
        print(f"⚠️ [FileLock] タイムアウト: {file_path} (他のプロセスが使用中、{timeout}秒待機)")
        raise
    except Exception as e:
        print(f"❌ [FileLock] 書き出しエラー: {file_path} - {e}")
        raise


def safe_json_read(file_path: str, timeout: float = DEFAULT_LOCK_TIMEOUT, default: Any = None) -> Any:
    """
    ロック付きでJSONファイルを読み込む。
    
    Args:
        file_path: 読み込み元ファイルパス
        timeout: ロック取得のタイムアウト（秒）
        default: ファイルが存在しない場合のデフォルト値
        
    Returns:
        読み込んだデータ、またはデフォルト値
    """
    # パスを正規化
    file_path = Path(file_path).as_posix()
    path_obj = Path(file_path)
    if not path_obj.exists():
        return default if default is not None else {}
    
    # 0バイトファイルの場合はJSON読込をスキップ（Expecting value エラー防止）
    if path_obj.stat().st_size == 0:
        return default if default is not None else {}
    
    lock = get_file_lock(file_path, timeout)
    
    try:
        with lock:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
                
    except Timeout:
        print(f"⚠️ [FileLock] 読み込みタイムアウト: {file_path} (他のプロセスが使用中)")
        raise
    except json.JSONDecodeError as e:
        print(f"⚠️ [FileLock] JSONパースエラー: {file_path} - {e}")
        raise
    except Exception as e:
        print(f"❌ [FileLock] 読み込みエラー: {file_path} - {e}")
        raise


def safe_json_update(file_path: str, update_func, timeout: float = DEFAULT_LOCK_TIMEOUT, default: Any = None) -> bool:
    """
    ロック付きでJSONファイルを読み込み、更新して書き込む（アトミック操作）。
    
    Args:
        file_path: 対象ファイルパス
        update_func: データを受け取り、更新後のデータを返す関数
        timeout: ロック取得のタイムアウト（秒）
        default: ファイルが存在しない場合のデフォルト値
        
    Returns:
        成功時True
        
    Example:
        def add_item(data):
            data.append({"new": "item"})
            return data
        
        safe_json_update("data.json", add_item, default=[])
    """
    lock = get_file_lock(file_path, timeout)
    
    try:
        with lock:
            # 読み込み
            if Path(file_path).exists():
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                data = default if default is not None else {}
            
            # 更新
            updated_data = update_func(data)
            
            # 書き込み
            _atomic_json_write(Path(file_path).as_posix(), updated_data)
            
            return True
            
    except Timeout:
        print(f"⚠️ [FileLock] 更新タイムアウト: {file_path}")
        raise
    except Exception as e:
        print(f"❌ [FileLock] 更新エラー: {file_path} - {e}")
        raise
