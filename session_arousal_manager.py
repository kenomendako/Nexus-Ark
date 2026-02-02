# session_arousal_manager.py
"""
セッションArousal管理モジュール。
会話ごとのArousalスコアを蓄積し、日次平均を提供する。
"""

import os
import json
import datetime
from typing import Dict, List, Optional
from pathlib import Path

import constants


def get_arousal_file_path(room_name: str) -> Path:
    """Arousal蓄積ファイルのパスを返す"""
    return Path(constants.ROOMS_DIR) / room_name / "memory" / "session_arousal.json"


def _load_arousal_data(room_name: str) -> Dict:
    """Arousal蓄積データを読み込む（ロック付き）"""
    from file_lock_utils import safe_json_read
    
    path = get_arousal_file_path(room_name)
    if not path.exists():
        return {}
    
    try:
        data = safe_json_read(str(path), default={})
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[SessionArousal] 読み込みエラー: {e}")
        return {}


def _save_arousal_data(room_name: str, data: Dict):
    """Arousal蓄積データを保存する（ロック付き）"""
    from file_lock_utils import safe_json_write
    
    path = get_arousal_file_path(room_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    if not safe_json_write(str(path), data):
        print(f"[SessionArousal] 保存タイムアウト - 他のプロセスが使用中")


def add_arousal_score(room_name: str, arousal_score: float, time_str: Optional[str] = None):
    """
    会話のArousalスコアを蓄積する。
    
    Args:
        room_name: ルーム名
        arousal_score: 会話のArousalスコア（0.0〜1.0）
        time_str: 指定のタイムスタンプ (HH:MM:SS) 。指定がない場合は現在時刻を使用。
    """
    now = datetime.datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    if not time_str:
        time_str = now.strftime('%H:%M:%S')
    
    data = _load_arousal_data(room_name)
    
    # 今日のデータがなければ初期化（新形式）
    if today_str not in data:
        data[today_str] = {
            "sessions": []
        }
    
    # 旧形式からの移行（scores配列があればsessionsに変換）
    if "scores" in data[today_str] and "sessions" not in data[today_str]:
        old_scores = data[today_str]["scores"]
        data[today_str]["sessions"] = [
            {"time": "00:00:00", "arousal": s, "processed": True}
            for s in old_scores
        ]
        del data[today_str]["scores"]
    
    # セッションを追加
    data[today_str]["sessions"].append({
        "time": time_str,
        "arousal": round(arousal_score, 3),
        "processed": False
    })
    
    # 古いデータを削除（7日以上前）
    cutoff_date = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
    data = {date: values for date, values in data.items() if date >= cutoff_date}
    
    _save_arousal_data(room_name, data)
    session_count = len(data[today_str]["sessions"])
    print(f"  - [SessionArousal] 蓄積: {arousal_score:.3f} (本日{session_count}件)")


def get_daily_average(room_name: str, date_str: Optional[str] = None) -> float:
    """
    指定日のArousal平均値を取得する。
    
    Args:
        room_name: ルーム名
        date_str: 日付文字列（デフォルト: 今日）
        
    Returns:
        平均Arousalスコア（0.0〜1.0）。データがなければ0.5を返す。
    """
    if date_str is None:
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')
    
    data = _load_arousal_data(room_name)
    
    if date_str not in data:
        return 0.5  # デフォルト値
    
    day_data = data[date_str]
    
    # 新形式（sessions配列）を優先
    if "sessions" in day_data:
        scores = [s["arousal"] for s in day_data["sessions"]]
    elif "scores" in day_data:
        scores = day_data["scores"]
    else:
        return 0.5
    
    if not scores:
        return 0.5
    
    return round(sum(scores) / len(scores), 3)


def get_daily_max(room_name: str, date_str: Optional[str] = None) -> float:
    """
    指定日のArousal最大値を取得する。
    
    Args:
        room_name: ルーム名
        date_str: 日付文字列（デフォルト: 今日）
        
    Returns:
        最大Arousalスコア。データがなければ0.5を返す。
    """
    if date_str is None:
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')
    
    data = _load_arousal_data(room_name)
    
    if date_str not in data:
        return 0.5
    
    day_data = data[date_str]
    
    # 新形式（sessions配列）を優先
    if "sessions" in day_data:
        scores = [s["arousal"] for s in day_data["sessions"]]
    elif "scores" in day_data:
        scores = day_data["scores"]
    else:
        return 0.5
    
    if not scores:
        return 0.5
    
    return max(scores)


def get_sessions_for_date(room_name: str, date_str: str) -> List[Dict]:
    """
    指定日の未処理セッション一覧を取得する。
    
    Args:
        room_name: ルーム名
        date_str: 日付文字列
        
    Returns:
        セッション情報のリスト [{"time": "HH:MM:SS", "arousal": float, "processed": bool}, ...]
    """
    data = _load_arousal_data(room_name)
    
    if date_str not in data:
        return []
    
    day_data = data[date_str]
    
    # 新形式
    if "sessions" in day_data:
        return [s for s in day_data["sessions"] if not s.get("processed", False)]
    
    # 旧形式の場合は空を返す（移行が必要）
    return []


def get_sessions_for_date_all(room_name: str, date_str: str) -> List[Dict]:
    """
    指定日の全セッション（処理済み含む）を取得する。
    Arousalアノテーション用。
    
    Args:
        room_name: ルーム名
        date_str: 日付文字列
        
    Returns:
        セッション情報のリスト [{"time": "HH:MM:SS", "arousal": float, "processed": bool}, ...]
    """
    data = _load_arousal_data(room_name)
    
    if date_str not in data:
        return []
    
    day_data = data[date_str]
    
    if "sessions" in day_data:
        return day_data["sessions"]
    
    return []


def mark_sessions_processed(room_name: str, date_str: str, times: List[str]):
    """
    指定したセッションを処理済みとしてマークする。
    
    Args:
        room_name: ルーム名
        date_str: 日付文字列
        times: マークするセッションの時刻リスト
    """
    data = _load_arousal_data(room_name)
    
    if date_str not in data:
        return
    
    day_data = data[date_str]
    
    if "sessions" in day_data:
        for session in day_data["sessions"]:
            if session["time"] in times:
                session["processed"] = True
        
        _save_arousal_data(room_name, data)
        print(f"  - [SessionArousal] {len(times)}件のセッションを処理済みにマーク")


def clear_daily_data(room_name: str, date_str: Optional[str] = None):
    """
    指定日のArousalデータをクリアする（エピソード記憶生成後に呼び出し）。
    
    Args:
        room_name: ルーム名
        date_str: 日付文字列（デフォルト: 今日）
    """
    if date_str is None:
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')
    
    data = _load_arousal_data(room_name)
    
    if date_str in data:
        del data[date_str]
        _save_arousal_data(room_name, data)
        print(f"  - [SessionArousal] {date_str}のデータをクリア")


def remove_arousal_session(room_name: str, date_str: str, time_str: str) -> bool:
    """
    指定した時刻のセッションをArousalデータから削除する。
    
    Args:
        room_name: ルーム名
        date_str: 日付文字列 (YYYY-MM-DD)
        time_str: 時刻文字列 (HH:MM:SS)
        
    Returns:
        削除に成功したかどうか
    """
    data = _load_arousal_data(room_name)
    
    if date_str not in data or "sessions" not in data[date_str]:
        return False
    
    sessions = data[date_str]["sessions"]
    original_len = len(sessions)
    
    # 時刻が一致するセッションを除外
    data[date_str]["sessions"] = [s for s in sessions if s.get("time") != time_str]
    
    if len(data[date_str]["sessions"]) < original_len:
        _save_arousal_data(room_name, data)
        print(f"  - [SessionArousal] {date_str} {time_str} のセッションを削除しました")
        return True
    
    return False
