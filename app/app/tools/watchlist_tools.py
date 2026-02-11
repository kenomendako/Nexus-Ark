# tools/watchlist_tools.py - ウォッチリスト管理ツール
"""
AIペルソナがウォッチリストを操作するためのツール群。
"""

from langchain_core.tools import tool
from typing import List, Optional
import traceback

import config_manager
from watchlist_manager import WatchlistManager, CHECK_INTERVAL_OPTIONS

# Tavilyが利用可能かチェック
try:
    from langchain_tavily import TavilyExtract
    TAVILY_AVAILABLE = True
except ImportError:
    TAVILY_AVAILABLE = False


def _fetch_url_content(url: str) -> tuple[bool, str]:
    """
    URLからコンテンツを取得する内部関数
    
    Returns:
        (success: bool, content_or_error: str)
    """
    # Tavilyが利用可能な場合はTavily Extractを使用
    if TAVILY_AVAILABLE and config_manager.TAVILY_API_KEY:
        try:
            extractor = TavilyExtract(
                tavily_api_key=config_manager.TAVILY_API_KEY,
                extract_depth="basic"
            )
            results = extractor.invoke({"urls": [url]})
            
            if results and isinstance(results, dict) and "results" in results:
                for result in results["results"]:
                    content = result.get("raw_content", result.get("content", ""))
                    if content:
                        return True, content[:10000]  # 10000文字に制限
            elif results and isinstance(results, list):
                for result in results:
                    content = result.get("raw_content", result.get("content", ""))
                    if content:
                        return True, content[:10000]
            
            return False, "コンテンツを抽出できませんでした"
            
        except Exception as e:
            print(f"  - Tavily Extractエラー: {e}")
            # フォールバックへ
    
    # BeautifulSoupでフォールバック
    try:
        import requests
        from bs4 import BeautifulSoup
        
        response = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # スクリプトとスタイルを除去
        for script in soup(["script", "style"]):
            script.decompose()
        
        text = soup.get_text(separator='\n', strip=True)
        return True, text[:10000]
        
    except Exception as e:
        return False, f"取得エラー: {e}"


@tool
def add_to_watchlist(url: str, name: str, room_name: str, check_interval: str = "manual") -> str:
    """
    URLをウォッチリストに追加します。
    
    Args:
        url: 監視対象のURL
        name: 表示名（例: "公式ブログ"）
        room_name: ルーム名
        check_interval: 監視頻度 ("manual", "hourly_1", "hourly_3", "hourly_6", "hourly_12", "daily")
    
    Returns:
        結果メッセージ
    """
    try:
        manager = WatchlistManager(room_name)
        
        # 既存チェック
        existing = manager.get_entry_by_url(url)
        if existing:
            return f"このURLは既にウォッチリストに登録されています: {existing['name']}"
        
        # 有効なintervalかチェック
        if check_interval not in CHECK_INTERVAL_OPTIONS:
            check_interval = "manual"
        
        entry = manager.add_entry(url=url, name=name, check_interval=check_interval)
        
        interval_display = CHECK_INTERVAL_OPTIONS.get(check_interval, "手動")
        return f"✅ ウォッチリストに追加しました:\n- 名前: {entry['name']}\n- URL: {url}\n- 監視頻度: {interval_display}"
        
    except Exception as e:
        traceback.print_exc()
        return f"[エラー] ウォッチリストへの追加に失敗しました: {e}"


@tool
def remove_from_watchlist(url_or_name: str, room_name: str) -> str:
    """
    URLまたは名前でウォッチリストからエントリを削除します。
    
    Args:
        url_or_name: 削除対象のURLまたは名前
        room_name: ルーム名
    
    Returns:
        結果メッセージ
    """
    try:
        manager = WatchlistManager(room_name)
        entries = manager.get_entries()
        
        # URLまたは名前で検索
        target = None
        for entry in entries:
            if entry["url"] == url_or_name or entry["name"] == url_or_name:
                target = entry
                break
        
        if not target:
            return f"指定されたURL/名前はウォッチリストに見つかりませんでした: {url_or_name}"
        
        success = manager.remove_entry(target["id"])
        if success:
            return f"✅ ウォッチリストから削除しました: {target['name']}"
        else:
            return "削除に失敗しました"
            
    except Exception as e:
        traceback.print_exc()
        return f"[エラー] 削除に失敗しました: {e}"


@tool
def get_watchlist(room_name: str) -> str:
    """
    現在のウォッチリストを取得します。
    
    Args:
        room_name: ルーム名
    
    Returns:
        ウォッチリストの内容
    """
    try:
        manager = WatchlistManager(room_name)
        entries = manager.get_entries_for_ui()
        
        if not entries:
            return "ウォッチリストは空です。`add_to_watchlist`ツールでURLを追加できます。"
        
        lines = ["📋 **ウォッチリスト**\n"]
        for i, entry in enumerate(entries, 1):
            status = "✅" if entry.get("enabled", True) else "⏸️"
            lines.append(f"{i}. {status} **{entry['name']}**")
            lines.append(f"   - URL: {entry['url']}")
            lines.append(f"   - 頻度: {entry.get('interval_display', '手動')}")
            lines.append(f"   - 最終確認: {entry.get('last_checked_display', '未チェック')}")
            lines.append("")
        
        return "\n".join(lines)
        
    except Exception as e:
        traceback.print_exc()
        return f"[エラー] ウォッチリストの取得に失敗しました: {e}"


@tool
def check_watchlist(room_name: str, url_filter: str = None) -> str:
    """
    ウォッチリストのURLをチェックし、更新があれば報告します。
    
    Args:
        room_name: ルーム名
        url_filter: 特定のURLまたは名前のみをチェック（省略時は全件）
    
    Returns:
        チェック結果レポート
    """
    try:
        manager = WatchlistManager(room_name)
        entries = manager.get_entries()
        
        if not entries:
            return "ウォッチリストは空です。"
        
        # フィルタリング
        if url_filter:
            entries = [e for e in entries if url_filter in e["url"] or url_filter in e["name"]]
            if not entries:
                return f"指定されたURL/名前が見つかりません: {url_filter}"
        
        results = []
        changes_found = 0
        
        for entry in entries:
            if not entry.get("enabled", True):
                continue
            
            url = entry["url"]
            name = entry["name"]
            
            # コンテンツ取得
            success, content = _fetch_url_content(url)
            
            if not success:
                results.append(f"❌ **{name}**: 取得失敗 - {content}")
                continue
            
            # 差分チェック
            has_changes, diff_summary = manager.check_and_update(entry["id"], content)
            
            if has_changes:
                changes_found += 1
                results.append(f"🔔 **{name}**: 更新あり！ ({diff_summary})")
            else:
                results.append(f"✅ **{name}**: {diff_summary}")
        
        # レポート生成
        header = f"📊 **ウォッチリストチェック完了** ({len(results)}件中 {changes_found}件に更新あり)\n"
        return header + "\n".join(results)
        
    except Exception as e:
        traceback.print_exc()
        return f"[エラー] チェック中に問題が発生しました: {e}"


@tool
def update_watchlist_interval(url_or_name: str, check_interval: str, room_name: str) -> str:
    """
    ウォッチリストエントリの監視頻度を変更します。
    
    Args:
        url_or_name: 対象のURLまたは名前
        check_interval: 新しい監視頻度 ("manual", "hourly_1", "hourly_3", "hourly_6", "hourly_12", "daily")
        room_name: ルーム名
    
    Returns:
        結果メッセージ
    """
    try:
        if check_interval not in CHECK_INTERVAL_OPTIONS:
            valid_options = ", ".join(CHECK_INTERVAL_OPTIONS.keys())
            return f"[エラー] 無効な監視頻度です。有効なオプション: {valid_options}"
        
        manager = WatchlistManager(room_name)
        entries = manager.get_entries()
        
        target = None
        for entry in entries:
            if entry["url"] == url_or_name or entry["name"] == url_or_name:
                target = entry
                break
        
        if not target:
            return f"指定されたURL/名前が見つかりません: {url_or_name}"
        
        manager.update_entry(target["id"], check_interval=check_interval)
        interval_display = CHECK_INTERVAL_OPTIONS.get(check_interval, check_interval)
        
        return f"✅ 監視頻度を変更しました: {target['name']} → {interval_display}"
        
    except Exception as e:
        traceback.print_exc()
        return f"[エラー] 更新に失敗しました: {e}"


@tool
def create_watchlist_group(
    name: str,
    room_name: str,
    description: str = "",
    check_interval: str = "manual"
) -> str:
    """
    ウォッチリストのグループを作成します。
    グループを使うと、同じ巡回時刻を持つサイトをまとめて管理できます。
    
    Args:
        name: グループ名（例: "AI技術ニュース"）
        room_name: ルーム名
        description: グループの説明（任意）
        check_interval: 巡回頻度 ("manual", "hourly_1", "hourly_3", "hourly_6", "hourly_12", "daily")
    
    Returns:
        結果メッセージ
    """
    try:
        manager = WatchlistManager(room_name)
        
        # 有効なintervalかチェック
        if check_interval not in CHECK_INTERVAL_OPTIONS:
            check_interval = "manual"
        
        group = manager.add_group(name=name, description=description, check_interval=check_interval)
        
        interval_display = CHECK_INTERVAL_OPTIONS.get(check_interval, "手動")
        return f"✅ グループを作成しました:\n- 名前: {group['name']}\n- 説明: {description or '（なし）'}\n- 巡回頻度: {interval_display}"
        
    except Exception as e:
        traceback.print_exc()
        return f"[エラー] グループの作成に失敗しました: {e}"


@tool
def add_entry_to_group(url_or_name: str, group_name: str, room_name: str) -> str:
    """
    既存のウォッチリストエントリをグループに追加します。
    
    Args:
        url_or_name: 移動するエントリのURLまたは名前
        group_name: 移動先のグループ名
        room_name: ルーム名
    
    Returns:
        結果メッセージ
    """
    try:
        manager = WatchlistManager(room_name)
        
        # エントリを検索
        entries = manager.get_entries()
        target_entry = None
        for entry in entries:
            if entry["url"] == url_or_name or entry["name"] == url_or_name:
                target_entry = entry
                break
        
        if not target_entry:
            return f"指定されたURL/名前が見つかりません: {url_or_name}"
        
        # グループを検索
        group = manager.get_group_by_name(group_name)
        if not group:
            return f"グループが見つかりません: {group_name}"
        
        # 移動
        result = manager.move_entry_to_group(target_entry["id"], group["id"])
        if result:
            return f"✅ 「{target_entry['name']}」をグループ「{group_name}」に移動しました"
        else:
            return "移動に失敗しました"
        
    except Exception as e:
        traceback.print_exc()
        return f"[エラー] エントリの移動に失敗しました: {e}"


@tool
def update_group_schedule(group_name: str, check_interval: str, room_name: str) -> str:
    """
    グループの巡回スケジュールを一括変更します。
    グループ内の全エントリーに新しいスケジュールが適用されます。
    
    Args:
        group_name: グループ名
        check_interval: 新しい巡回頻度 ("manual", "hourly_1", "hourly_3", "hourly_6", "hourly_12", "daily_HH:MM")
        room_name: ルーム名
    
    Returns:
        結果メッセージ
    """
    try:
        # daily_HH:MM形式をサポート
        base_interval = check_interval.split("_")[0] if "_" in check_interval else check_interval
        if base_interval not in ["manual", "hourly", "daily"]:
            if check_interval not in CHECK_INTERVAL_OPTIONS:
                valid_options = ", ".join(CHECK_INTERVAL_OPTIONS.keys())
                return f"[エラー] 無効な巡回頻度です。有効なオプション: {valid_options}"
        
        manager = WatchlistManager(room_name)
        
        # グループを検索
        group = manager.get_group_by_name(group_name)
        if not group:
            return f"グループが見つかりません: {group_name}"
        
        # 時刻一括変更
        success, updated_count = manager.update_group_interval(group["id"], check_interval)
        
        if success:
            interval_display = check_interval
            if check_interval in CHECK_INTERVAL_OPTIONS:
                interval_display = CHECK_INTERVAL_OPTIONS[check_interval]
            elif check_interval.startswith("daily_"):
                time_part = check_interval.split("_")[1]
                interval_display = f"毎日 {time_part}"
            
            return f"✅ グループ「{group_name}」の巡回時刻を変更しました:\n- 新しい頻度: {interval_display}\n- 更新されたエントリー: {updated_count}件"
        else:
            return "更新に失敗しました"
        
    except Exception as e:
        traceback.print_exc()
        return f"[エラー] スケジュールの更新に失敗しました: {e}"


# ツールリスト（graph.pyでインポート用）
WATCHLIST_TOOLS = [
    add_to_watchlist,
    remove_from_watchlist,
    get_watchlist,
    check_watchlist,
    update_watchlist_interval,
    create_watchlist_group,
    add_entry_to_group,
    update_group_schedule,
]
