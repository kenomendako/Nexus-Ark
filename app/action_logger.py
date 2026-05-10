import os
import json
import datetime
import re
import traceback
from pathlib import Path
from typing import List, Dict, Optional

import constants
import config_manager
import utils
import room_manager
from filelock import FileLock

def _get_action_log_dir(room_name: str) -> Path:
    """ルームのアクションログ保存先ディレクトリを取得する"""
    rooms_dir = Path(constants.ROOMS_DIR)
    room_dir = rooms_dir / room_name
    log_dir = room_dir / "memory" / "run_logs"
    os.makedirs(log_dir, exist_ok=True)
    return log_dir

def _get_daily_log_path(room_name: str, date_str: str) -> Path:
    """指定日のアクションログファイルのパスを取得する (YYYY-MM-DD.jsonl)"""
    log_dir = _get_action_log_dir(room_name)
    return log_dir / f"action_log_{date_str}.jsonl"

def append_action_log(room_name: str, tool_name: str, args: dict, result: str) -> bool:
    """
    ツールの実行結果をアクションログに追記する。
    結果テキストが長すぎる場合は切り詰める。
    """
    try:
        now = datetime.datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M:%S')
        
        # 不要な引数（APIキー等）や長すぎるテキストを除外/切り詰め
        safe_args = args.copy()
        if 'api_key' in safe_args:
            safe_args['api_key'] = '<REDACTED>'
        
        # 不要なシステムメッセージの除去
        summary_result = str(result)
        internal_msg_patterns = [
            r'\*\*このファイル編集タスクは完了しました。.*',
            r'\*\*このタスクの実行を宣言するような前置きは不要です。.*'
        ]
        for pattern in internal_msg_patterns:
            summary_result = re.sub(pattern, '', summary_result, flags=re.DOTALL).strip()

        # ファイル編集系ツールの場合はmodification_requestを優先表記
        if tool_name.startswith("plan_") and "modification_request" in safe_args:
            mod_req = safe_args["modification_request"]
            if len(mod_req) > 100: mod_req = mod_req[:100] + "..."
            summary_result = f"【編集】{mod_req} | 【結果】{summary_result}"

        # 結果テキストの切り詰め (約200文字)
        if len(summary_result) > 200:
            summary_result = summary_result[:200] + "..."
            
        # インテントとコンテキストタイプの抽出
        context_type = safe_args.pop('context_type', None)
        intent = safe_args.pop('intent', safe_args.pop('intent_and_reasoning', None))
            
        record = {
            "timestamp": now.isoformat(),
            "time": time_str,
            "tool_name": tool_name,
            "context_type": context_type,
            "intent": intent,
            "args": safe_args,
            "result_summary": summary_result
        }
        
        log_path = _get_daily_log_path(room_name, date_str)
        
        # 排他制御付きで追記
        lock_path = str(log_path) + ".lock"
        with FileLock(lock_path):
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
                
        return True
    except Exception as e:
        print(f"Error saving action log: {e}")
        traceback.print_exc()
        return False

def get_recent_actions(room_name: str, limit: int = 5) -> str:
    """
    直近のアクション履歴を文字列として返す（プロンプト注入用）。
    今日のファイルから末尾数件、足りなければ昨日のファイルから取得する。
    """
    try:
        now = datetime.datetime.now()
        today_str = now.strftime('%Y-%m-%d')
        yesterday_str = (now - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        
        records = []
        
        # 今日と昨日のファイルを読み込む
        for date_str in [today_str, yesterday_str]:
            path = _get_daily_log_path(room_name, date_str)
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    for line in reversed(lines):  # 新しい順に取得
                        if not line.strip(): continue
                        try:
                            records.append(json.loads(line))
                            if len(records) >= limit:
                                break
                        except json.JSONDecodeError:
                            pass
            if len(records) >= limit:
                 break
                 
        if not records:
            return "（直近のアクションログはありません）"
            
        # 古い順に整形して返す
        records.reverse()
        lines = []
        for rec in records:
            time_str = rec.get("time", "")[:5] # "HH:MM"
            tool = rec.get("tool_name", "")
            
            # ツール名に基づく簡単な説明の付与
            action_desc = tool
            if tool == "web_search_tool":
                query = rec.get("args", {}).get("query", "")
                action_desc = f"Web検索 ({query})"
            elif tool == "read_url_tool":
                action_desc = f"Web閲覧"
            elif tool in ("update_working_memory", "switch_working_memory"):
                action_desc = f"ワーキングメモリの整理"
            elif tool == "search_past_conversations":
                action_desc = f"過去の会話検索"
            elif tool == "recall_memories":
                action_desc = f"関連する記憶の検索"
            elif tool == "generate_image":
                action_desc = f"画像の生成"
            elif tool in ("read_identity_memory", "read_diary_memory", "read_secret_diary", "read_full_notepad", "read_creative_notes", "read_research_notes", "read_world_settings"):
                action_desc = f"記憶・ノートの参照"
            elif tool in ("plan_identity_memory_edit", "plan_diary_append", "plan_secret_diary_edit", "plan_notepad_edit", "plan_creative_notes_edit", "plan_research_notes_edit", "plan_world_edit"):
                action_desc = f"記憶・ノートの更新計画"
            elif tool == "write_entity_memory":
                action_desc = f"エンティティ記憶の更新"
            elif "diary" in tool or "notes" in tool:
                action_desc = f"記憶・ノートの機能"
            elif tool == "set_personal_alarm" or tool == "set_timer":
                action_desc = f"タイマー/アラーム設定"
                
            # 意図とコンテキストタイプの反映
            context_type = rec.get("context_type")
            intent = rec.get("intent")
            
            prefix = ""
            if context_type:
                prefix = f"[{context_type}] "
            
            intent_str = ""
            if intent:
                # インテントが長い場合は短縮
                display_intent = intent[:30] + "..." if len(intent) > 30 else intent
                intent_str = f"({display_intent}) "

            lines.append(f"- [{time_str}] {prefix}{action_desc} {intent_str}: {rec.get('result_summary', '')[:50]}...")
            
        return "\n".join(lines)
        
    except Exception as e:
        print(f"Error reading recent actions: {e}")
        return "（アクションログの取得に失敗しました）"

def get_actions_by_date(room_name: str, target_date_str: str) -> List[Dict]:
    """
    指定日のアクション一覧をリスト形式で返す（エピソード記憶統合用）。
    """
    try:
        path = _get_daily_log_path(room_name, target_date_str)
        if not path.exists():
            return []
            
        records = []
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip(): continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records
    except Exception as e:
        print(f"Error reading actions for date {target_date_str}: {e}")
        return []

def truncate_actions_after(room_name: str, target_timestamp_str: str) -> int:
    """
    指定した時刻（HH:MM:SS）以降のアクションログをその日のログファイルから特定し削除する。
    戻り値は削除されたレコード数。巻き戻し用のユーティリティ。
    """
    try:
        now = datetime.datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        path = _get_daily_log_path(room_name, date_str)
        if not path.exists():
            return 0
            
        # target_timestamp_str は "18:48:09" 等の形式と想定
        target_time = target_timestamp_str.strip()
        if not re.match(r'^\d{2}:\d{2}:\d{2}$', target_time):
            # 万一日付付きなら抽出
            match = re.search(r'(\d{2}:\d{2}:\d{2})', target_time)
            if match:
                target_time = match.group(1)
            else:
                return 0

        kept_records = []
        deleted_count = 0
        
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
            for line in lines:
                if not line.strip(): continue
                try:
                    rec = json.loads(line)
                    rec_time = rec.get("time", "")
                    
                    if rec_time and rec_time >= target_time:
                        deleted_count += 1
                        continue # 対象時刻以降は保持しない
                        
                    kept_records.append(line)
                except json.JSONDecodeError:
                    kept_records.append(line)
                    
            if deleted_count > 0:
                with open(path, 'w', encoding='utf-8') as f:
                    # kept_recordsには改行が含まれている前提
                    f.writelines(kept_records)
                    
        if deleted_count > 0:
            print(f"  - [ActionLog] {target_time} 以降の {deleted_count} 件の履歴を取り消しました。")
            
        return deleted_count
        
    except Exception as e:
        print(f"Error truncating actions after {target_timestamp_str}: {e}")
        return 0
