# watchlist_manager.py - ウォッチリスト管理モジュール
"""
ルームごとに監視対象URLを管理し、更新を検出する機能を提供。
"""

import os
import json
import uuid
import hashlib
import datetime
from typing import List, Dict, Optional, Tuple
from pathlib import Path

import constants
import config_manager

# --- 定数 ---
WATCHLIST_FILENAME = "watchlist.json"
WATCHLIST_CACHE_DIR = "watchlist_cache"

# 監視頻度オプション
CHECK_INTERVAL_OPTIONS = {
    "manual": "手動のみ",
    "daily": "毎日指定時刻",
    "hourly_1": "1時間ごと",
    "hourly_3": "3時間ごと",
    "hourly_6": "6時間ごと",
    "hourly_12": "12時間ごと",
}


class WatchlistManager:
    """ウォッチリスト管理クラス"""
    
    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = os.path.join(constants.ROOMS_DIR, room_name)
        self.watchlist_path = os.path.join(self.room_dir, WATCHLIST_FILENAME)
        self.cache_dir = os.path.join(self.room_dir, WATCHLIST_CACHE_DIR)
        
        # キャッシュディレクトリを確保
        os.makedirs(self.cache_dir, exist_ok=True)
    
    def _load_watchlist(self) -> dict:
        """ウォッチリストを読み込む（version 2への自動マイグレーション対応）"""
        data = None
        if os.path.exists(self.watchlist_path):
            try:
                with open(self.watchlist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        
        if data is None:
            # デフォルト構造 (version 2)
            data = {
                "version": 2,
                "entries": [],
                "groups": [],
                "settings": {
                    "default_interval": "manual",
                    "scheduled_time": None
                }
            }
        
        # version 1からversion 2へのマイグレーション
        if data.get("version", 1) < 2:
            data = self._migrate_to_v2(data)
            self._save_watchlist(data)
        
        return data
    
    def _migrate_to_v2(self, data: dict) -> dict:
        """version 1からversion 2にマイグレーションする"""
        data["version"] = 2
        if "groups" not in data:
            data["groups"] = []
        # 既存エントリーにgroup_idがなければNoneを追加
        for entry in data.get("entries", []):
            if "group_id" not in entry:
                entry["group_id"] = None
        return data
    
    def _save_watchlist(self, data: dict) -> None:
        """ウォッチリストを保存する"""
        with open(self.watchlist_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def _url_to_hash(self, url: str) -> str:
        """URLをハッシュ値に変換（キャッシュファイル名用）"""
        return hashlib.md5(url.encode()).hexdigest()[:16]
    
    def _get_cache_path(self, url: str, suffix: str = "latest") -> str:
        """キャッシュファイルのパスを取得"""
        url_hash = self._url_to_hash(url)
        return os.path.join(self.cache_dir, f"{url_hash}_{suffix}.txt")
    
    # --- CRUD操作 ---
    
    def get_entries(self) -> List[dict]:
        """全エントリを取得"""
        data = self._load_watchlist()
        return data.get("entries", [])
    
    def get_entry_by_id(self, entry_id: str) -> Optional[dict]:
        """IDでエントリを取得（短縮IDにも対応）"""
        for entry in self.get_entries():
            full_id = entry.get("id", "")
            if full_id == entry_id or full_id.startswith(entry_id):
                return entry
        return None
    
    def get_entry_by_url(self, url: str) -> Optional[dict]:
        """URLでエントリを取得"""
        for entry in self.get_entries():
            if entry.get("url") == url:
                return entry
        return None
    
    def add_entry(self, url: str, name: str = None, check_interval: str = "manual") -> dict:
        """
        新しいエントリを追加
        
        Returns:
            追加されたエントリ（既存の場合はそのエントリ）
        """
        # 既存チェック
        existing = self.get_entry_by_url(url)
        if existing:
            return existing
        
        # 名前が指定されていない場合はURLから生成
        if not name:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            name = parsed.netloc + (parsed.path[:30] if parsed.path else "")
        
        entry = {
            "id": str(uuid.uuid4()),
            "url": url,
            "name": name,
            "added_at": datetime.datetime.now().isoformat(),
            "last_checked": None,
            "check_interval": check_interval,
            "enabled": True,
            "group_id": None  # v2: グループID（未所属はNone）
        }
        
        data = self._load_watchlist()
        data["entries"].append(entry)
        self._save_watchlist(data)
        
        return entry
    
    def remove_entry(self, entry_id: str) -> bool:
        """エントリを削除"""
        data = self._load_watchlist()
        original_count = len(data["entries"])
        
        # エントリを削除前にURLを取得（キャッシュ削除用）
        entry = self.get_entry_by_id(entry_id)
        if not entry:
            return False
            
        full_id = entry["id"]
        data["entries"] = [e for e in data["entries"] if e.get("id") != full_id]
        
        if len(data["entries"]) < original_count:
            self._save_watchlist(data)
            
            # キャッシュも削除
            if entry:
                self._clear_cache(entry["url"])
            
            return True
        return False
    
    def update_entry(self, entry_id: str, **kwargs) -> Optional[dict]:
        """エントリを更新"""
        data = self._load_watchlist()
        
        for entry in data["entries"]:
            full_id = entry.get("id", "")
            if full_id == entry_id or full_id.startswith(entry_id):
                for key, value in kwargs.items():
                    # group_idは新規キーとして追加可能
                    if key in entry or key == "group_id":
                        entry[key] = value
                self._save_watchlist(data)
                return entry
        
        return None
    
    def update_settings(self, **kwargs) -> dict:
        """設定を更新"""
        data = self._load_watchlist()
        for key, value in kwargs.items():
            if key in data["settings"]:
                data["settings"][key] = value
        self._save_watchlist(data)
        return data["settings"]
    
    def get_settings(self) -> dict:
        """設定を取得"""
        data = self._load_watchlist()
        return data.get("settings", {})
    
    # --- グループ管理 (v2) ---
    
    def get_groups(self) -> List[dict]:
        """全グループを取得"""
        data = self._load_watchlist()
        return data.get("groups", [])
    
    def get_group_by_id(self, group_id: str) -> Optional[dict]:
        """IDでグループを取得（短縮IDにも対応）"""
        for group in self.get_groups():
            full_id = group.get("id", "")
            if full_id == group_id or full_id.startswith(group_id):
                return group
        return None
    
    def get_group_by_name(self, name: str) -> Optional[dict]:
        """名前でグループを取得"""
        for group in self.get_groups():
            if group.get("name") == name:
                return group
        return None
    
    def add_group(self, name: str, description: str = "", check_interval: str = "manual") -> dict:
        """
        新しいグループを作成
        
        Args:
            name: グループ名
            description: グループの説明
            check_interval: デフォルトの監視頻度
        
        Returns:
            作成されたグループ
        """
        # 同名グループが存在する場合はそれを返す
        existing = self.get_group_by_name(name)
        if existing:
            return existing
        
        group = {
            "id": str(uuid.uuid4()),
            "name": name,
            "description": description,
            "check_interval": check_interval,
            "enabled": True,
            "created_at": datetime.datetime.now().isoformat()
        }
        
        data = self._load_watchlist()
        data["groups"].append(group)
        self._save_watchlist(data)
        
        return group
    
    def remove_group(self, group_id: str) -> bool:
        """
        グループを削除（配下エントリーは「グループなし」に戻す）
        """
        data = self._load_watchlist()
        
        # グループを検索
        group = self.get_group_by_id(group_id)
        if not group:
            return False
        
        full_id = group["id"]
        
        # 配下エントリーのgroup_idをNoneに設定
        for entry in data["entries"]:
            if entry.get("group_id") == full_id:
                entry["group_id"] = None
        
        # グループを削除
        data["groups"] = [g for g in data["groups"] if g.get("id") != full_id]
        self._save_watchlist(data)
        
        return True
    
    def update_group(self, group_id: str, **kwargs) -> Optional[dict]:
        """グループ情報を更新"""
        data = self._load_watchlist()
        
        for group in data["groups"]:
            full_id = group.get("id", "")
            if full_id == group_id or full_id.startswith(group_id):
                for key, value in kwargs.items():
                    if key in group:
                        group[key] = value
                self._save_watchlist(data)
                return group
        
        return None
    
    def get_entries_by_group(self, group_id: Optional[str]) -> List[dict]:
        """
        特定グループに属するエントリーを取得
        
        Args:
            group_id: グループID（Noneの場合は未所属エントリー）
        """
        entries = self.get_entries()
        if group_id is None:
            return [e for e in entries if e.get("group_id") is None]
        
        # 短縮ID対応
        group = self.get_group_by_id(group_id)
        if not group:
            return []
        
        full_id = group["id"]
        return [e for e in entries if e.get("group_id") == full_id]
    
    def move_entry_to_group(self, entry_id: str, group_id: Optional[str]) -> Optional[dict]:
        """
        エントリーをグループに移動
        
        Args:
            entry_id: エントリーID
            group_id: 移動先グループID（Noneで「グループなし」に戻す）
        
        Note:
            グループに移動すると、エントリーのcheck_intervalはグループの設定を継承します。
        """
        # グループの存在確認（Noneでない場合）
        full_group_id = None
        group_interval = None
        if group_id is not None:
            group = self.get_group_by_id(group_id)
            if not group:
                return None
            full_group_id = group["id"]
            group_interval = group.get("check_interval", "manual")
        
        # グループに移動する場合は時刻も継承
        if group_interval:
            return self.update_entry(entry_id, group_id=full_group_id, check_interval=group_interval)
        else:
            return self.update_entry(entry_id, group_id=full_group_id)
    
    def update_group_interval(self, group_id: str, check_interval: str) -> Tuple[bool, int]:
        """
        グループの巡回時刻を一括変更（配下エントリーも更新）
        
        Args:
            group_id: グループID
            check_interval: 新しい監視頻度
        
        Returns:
            (成功したか, 更新したエントリー数)
        """
        group = self.get_group_by_id(group_id)
        if not group:
            return False, 0
        
        full_id = group["id"]
        data = self._load_watchlist()
        
        # グループの設定を更新
        for g in data["groups"]:
            if g.get("id") == full_id:
                g["check_interval"] = check_interval
                break
        
        # 配下エントリーの時刻も更新
        updated_count = 0
        for entry in data["entries"]:
            if entry.get("group_id") == full_id:
                entry["check_interval"] = check_interval
                updated_count += 1
        
        self._save_watchlist(data)
        return True, updated_count
    
    def get_groups_for_ui(self) -> List[dict]:
        """UI表示用のグループリストを取得"""
        groups = self.get_groups()
        for group in groups:
            # グループ内エントリー数をカウント
            entries = self.get_entries_by_group(group["id"])
            group["entry_count"] = len(entries)
            
            # 監視頻度を人間可読形式に変換
            interval = group.get("check_interval", "manual")
            if interval.startswith("daily_"):
                time_part = interval.split("_")[1]
                group["interval_display"] = f"毎日 {time_part}"
            else:
                group["interval_display"] = CHECK_INTERVAL_OPTIONS.get(interval, interval)
        
        return groups

    # --- キャッシュ操作 ---
    
    def _save_cache(self, url: str, content: str) -> None:
        """コンテンツをキャッシュに保存（前回分をバックアップ）"""
        latest_path = self._get_cache_path(url, "latest")
        previous_path = self._get_cache_path(url, "previous")
        
        # 現在のlatestをpreviousに移動
        if os.path.exists(latest_path):
            try:
                os.replace(latest_path, previous_path)
            except OSError:
                pass
        
        # 新しいコンテンツを保存
        with open(latest_path, "w", encoding="utf-8") as f:
            f.write(content)
    
    def _load_cache(self, url: str, suffix: str = "latest") -> Optional[str]:
        """キャッシュからコンテンツを読み込む"""
        cache_path = self._get_cache_path(url, suffix)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return f.read()
            except IOError:
                pass
        return None
    
    def _clear_cache(self, url: str) -> None:
        """特定URLのキャッシュを削除"""
        for suffix in ["latest", "previous"]:
            cache_path = self._get_cache_path(url, suffix)
            if os.path.exists(cache_path):
                try:
                    os.remove(cache_path)
                except OSError:
                    pass
    
    # --- 差分検出 ---
    
    def detect_changes(self, url: str, new_content: str) -> Tuple[bool, str]:
        """
        新しいコンテンツと前回のコンテンツを比較し、変更を検出
        
        Returns:
            (has_changes: bool, diff_summary: str)
        """
        previous_content = self._load_cache(url, "latest")
        
        if previous_content is None:
            # 初回取得 → 変更ありとして扱う（ペルソナに報告するため）
            return True, "初回取得（新規コンテンツ）"
        
        if previous_content == new_content:
            return False, "変更なし"
        
        # 簡易差分計算
        prev_lines = set(previous_content.split('\n'))
        new_lines = set(new_content.split('\n'))
        
        added = new_lines - prev_lines
        removed = prev_lines - new_lines
        
        added_count = len(added)
        removed_count = len(removed)
        
        # 差分サマリーを生成
        summary_parts = []
        if added_count > 0:
            summary_parts.append(f"+{added_count}行追加")
        if removed_count > 0:
            summary_parts.append(f"-{removed_count}行削除")
        
        diff_summary = "、".join(summary_parts) if summary_parts else "軽微な変更"
        
        return True, diff_summary
    
    def check_and_update(self, entry_id: str, new_content: str) -> Tuple[bool, str]:
        """
        エントリの更新をチェックし、キャッシュを更新
        
        Returns:
            (has_changes: bool, diff_summary: str)
        """
        entry = self.get_entry_by_id(entry_id)
        if not entry:
            return False, "エントリが見つかりません"
        
        url = entry["url"]
        has_changes, diff_summary = self.detect_changes(url, new_content)
        
        # キャッシュを更新
        self._save_cache(url, new_content)
        
        # last_checkedを更新
        self.update_entry(entry_id, last_checked=datetime.datetime.now().isoformat())
        
        return has_changes, diff_summary
    
    def _time_diff_minutes(self, time1: str, time2: str) -> int:
        """2つの時刻文字列（HH:MM形式）の差分を分で返す"""
        try:
            h1, m1 = map(int, time1.split(":"))
            h2, m2 = map(int, time2.split(":"))
            minutes1 = h1 * 60 + m1
            minutes2 = h2 * 60 + m2
            return minutes1 - minutes2
        except:
            return 9999  # パース失敗時は大きな値を返す
    
    # --- ヘルパー ---
    
    def get_entries_for_ui(self) -> List[dict]:
        """UI表示用のエントリリストを取得"""
        entries = self.get_entries()
        groups = {g["id"]: g["name"] for g in self.get_groups()}
        
        for entry in entries:
            # 最終チェック時刻を人間可読形式に変換
            if entry.get("last_checked"):
                try:
                    dt = datetime.datetime.fromisoformat(entry["last_checked"])
                    entry["last_checked_display"] = dt.strftime("%m/%d %H:%M")
                except:
                    entry["last_checked_display"] = "不明"
            else:
                entry["last_checked_display"] = "未チェック"
            
            # 監視頻度を人間可読形式に変換
            interval = entry.get("check_interval", "manual")
            if interval.startswith("daily_"):
                # daily_HH:MM形式
                time_part = interval.split("_")[1]
                entry["interval_display"] = f"毎日 {time_part}"
            else:
                entry["interval_display"] = CHECK_INTERVAL_OPTIONS.get(
                    interval, 
                    interval
                )
            
            # グループ名を取得 (v2)
            group_id = entry.get("group_id")
            entry["group_name"] = groups.get(group_id, "") if group_id else ""
        
        return entries
    
    def get_due_entries(self) -> List[dict]:
        """
        チェックが必要なエントリを取得（定期実行用）
        
        Returns:
            チェックすべきエントリのリスト
        """
        entries = self.get_entries()
        now = datetime.datetime.now()
        due_entries = []
        
        for entry in entries:
            if not entry.get("enabled", True):
                continue
            
            interval = entry.get("check_interval", "manual")
            if interval == "manual":
                continue
            
            last_checked = entry.get("last_checked")
            if not last_checked:
                # 一度もチェックされていない
                due_entries.append(entry)
                continue
            
            try:
                last_dt = datetime.datetime.fromisoformat(last_checked)
            except:
                due_entries.append(entry)
                continue
            
            # 間隔に応じてチェック
            if interval.startswith("hourly_"):
                hours = int(interval.split("_")[1])
                if (now - last_dt).total_seconds() >= hours * 3600:
                    due_entries.append(entry)
            elif interval.startswith("daily"):
                if "_" in interval:
                    # daily_HH:MM形式: 指定時刻を過ぎていて、今日まだチェックしていなければdue
                    scheduled_time = interval.split("_")[1]  # "07:00"など
                    try:
                        scheduled_hour, scheduled_minute = map(int, scheduled_time.split(":"))
                        today_scheduled_dt = now.replace(
                            hour=scheduled_hour, minute=scheduled_minute, second=0, microsecond=0
                        )
                        # 今日の指定時刻を過ぎており、かつ最後のチェックがそれより前であればdue
                        if now >= today_scheduled_dt and last_dt < today_scheduled_dt:
                            due_entries.append(entry)
                    except ValueError:
                        # パース失敗時は24時間ルールにフォールバック
                        if (now - last_dt).total_seconds() >= 24 * 3600:
                            due_entries.append(entry)
                else:
                    # 旧形式daily: 24時間経過でdue
                    if (now - last_dt).total_seconds() >= 24 * 3600:
                        due_entries.append(entry)
        
        return due_entries


# --- モジュールレベルのヘルパー関数 ---

def get_watchlist_manager(room_name: str) -> WatchlistManager:
    """指定ルームのWatchlistManagerを取得"""
    return WatchlistManager(room_name)
