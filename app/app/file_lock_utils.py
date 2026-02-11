# file_lock_utils.py
"""
ファイルロックユーティリティ

睡眠時処理と会話処理の競合を防止するための排他ロック機構。
重要なJSONファイル（episodic_memory.json等）への同時書き込みを防止する。
"""

import json
from pathlib import Path
from typing import Any, Optional
from filelock import FileLock, Timeout

# デフォルトのタイムアウト（秒）
DEFAULT_LOCK_TIMEOUT = 10.0


def get_file_lock(file_path: str, timeout: float = DEFAULT_LOCK_TIMEOUT) -> FileLock:
    """
    ファイルパスに対応するFileLockを取得する。
    
    Args:
        file_path: ロック対象のファイルパス
        timeout: ロック取得のタイムアウト（秒）
        
    Returns:
        FileLockインスタンス
    """
    lock_path = f"{file_path}.lock"
    return FileLock(lock_path, timeout=timeout)


def safe_json_write(file_path: str, data: Any, timeout: float = DEFAULT_LOCK_TIMEOUT, indent: int = 2) -> bool:
    """
    ロック付きでJSONファイルに書き込む。
    
    Args:
        file_path: 書き込み先ファイルパス
        data: 書き込むデータ（JSON serializable）
        timeout: ロック取得のタイムアウト（秒）
        indent: JSONインデント
        
    Returns:
        成功時True、ロックタイムアウト時False
    """
    lock = get_file_lock(file_path, timeout)
    
    try:
        with lock:
            # 親ディレクトリが存在しない場合は作成
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=indent, ensure_ascii=False)
            return True
            
    except Timeout:
        print(f"⚠️ [FileLock] タイムアウト: {file_path} (他のプロセスが使用中、{timeout}秒待機)")
        return False
    except Exception as e:
        print(f"❌ [FileLock] 書き込みエラー: {file_path} - {e}")
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
    if not Path(file_path).exists():
        return default if default is not None else {}
    
    lock = get_file_lock(file_path, timeout)
    
    try:
        with lock:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
                
    except Timeout:
        print(f"⚠️ [FileLock] 読み込みタイムアウト: {file_path} (他のプロセスが使用中)")
        return default if default is not None else {}
    except json.JSONDecodeError as e:
        print(f"⚠️ [FileLock] JSONパースエラー: {file_path} - {e}")
        return default if default is not None else {}
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
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(updated_data, f, indent=2, ensure_ascii=False)
            
            return True
            
    except Timeout:
        print(f"⚠️ [FileLock] 更新タイムアウト: {file_path}")
        return False
    except Exception as e:
        print(f"❌ [FileLock] 更新エラー: {file_path} - {e}")
        raise
