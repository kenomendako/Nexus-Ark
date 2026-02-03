# alarm_manager.py (リファクタリング版)

import os
import json
import uuid
import threading
import schedule
import time
import datetime
import traceback
import requests
import config_manager
import constants
import room_manager
import gemini_api
import utils
import re
import dreaming_manager
from typing import Any

import sys

# Linuxではplyerのデスクトップ通知がdbus/notify-send依存のため無効化
if sys.platform.startswith('linux'):
    PLYER_AVAILABLE = False
else:
    try:
        from plyer import notification
        PLYER_AVAILABLE = True
    except ImportError:
        print("情報: 'plyer'ライブラリが見つかりません。PCデスクトップ通知機能は無効になります。")
        print(" -> pip install plyer でインストールできます。")
        PLYER_AVAILABLE = False

alarms_data_global = []
alarm_thread_stop_event = threading.Event()

# 重複発火防止用（ルーム名 -> 最後の発火時刻）
_last_autonomous_trigger_time = {}

def load_alarms():
    global alarms_data_global
    if not os.path.exists(constants.ALARMS_FILE):
        alarms_data_global = []
        return alarms_data_global
    try:
        with open(constants.ALARMS_FILE, "r", encoding="utf-8") as f:
            loaded_data = json.load(f)
            alarms_data_global = sorted(loaded_data, key=lambda x: x.get("time", ""))
            return alarms_data_global
    except Exception as e:
        print(f"アラーム読込エラー: {e}")
        alarms_data_global = []
        return alarms_data_global

def save_alarms():
    try:
        with open(constants.ALARMS_FILE, "w", encoding="utf-8") as f:
            json.dump(alarms_data_global, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"アラーム保存エラー: {e}")

def add_alarm_entry(alarm_data: dict):
    global alarms_data_global
    alarms_data_global.append(alarm_data)
    save_alarms()
    return True

def delete_alarm(alarm_id: str):
    global alarms_data_global
    original_len = len(alarms_data_global)
    alarms_data_global = [a for a in alarms_data_global if a.get("id") != alarm_id]
    if len(alarms_data_global) < original_len:
        save_alarms()
        print(f"アラーム削除: ID {alarm_id}")
        return True
    return False


def _summarize_watchlist_content(name: str, url: str, new_content: str, diff_summary: str) -> str:
    """
    軽量モデル（gemini-2.5-flash-lite）を使用して、ウォッチリスト更新内容を要約する。
    503/429エラー時はリトライし、それでも失敗したらコンテンツの冒頭を返す。
    
    Args:
        name: サイト名
        url: URL
        new_content: 新しいコンテンツ（最大文字数に制限）
        diff_summary: 差分サマリー（例: "+69行追加、-47行削除"）
    
    Returns:
        要約テキスト
    """
    import time as time_module  # 既存のtimeモジュールと名前衝突回避
    
    MAX_RETRIES = 3
    FALLBACK_CHAR_LIMIT = 500
    
    def _create_fallback_content(content: str, error_msg: str = None) -> str:
        """フォールバックコンテンツを生成"""
        fallback = content[:FALLBACK_CHAR_LIMIT].strip()
        if len(content) > FALLBACK_CHAR_LIMIT:
            fallback += (
                "\n\n---\n"
                "⚠️ **注意**: 要約APIが一時的に利用できないため、コンテンツ冒頭のみを抜粋しています。\n"
                "詳細が必要な場合は、URLを直接確認するかWeb検索ツールで追加調査してください。"
            )
        return fallback
    
    try:
        from google import genai
        
        # APIキーを取得
        api_key_name = config_manager.get_latest_api_key_name_from_config()
        if not api_key_name:
            print(f"  ⚠️ {name}: APIキー未設定（フォールバック使用）")
            return _create_fallback_content(new_content)
        
        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
        if not api_key:
            print(f"  ⚠️ {name}: APIキーが見つからない（フォールバック使用）")
            return _create_fallback_content(new_content)
        
        # 軽量モデルを使用
        client = genai.Client(api_key=api_key)
        
        # コンテンツを制限（トークン節約）
        content_preview = new_content[:3000] if len(new_content) > 3000 else new_content
        
        prompt = f"""以下のWebページの更新内容を簡潔に要約してください。
ユーザーに報告するための情報として、重要なポイントのみを抽出してください。

【サイト名】{name}
【URL】{url}
【変更規模】{diff_summary}

【コンテンツ】
{content_preview}

【出力ルール】
- 箇条書きで3〜5点に要約
- 専門用語があれば簡単に説明
- 新しい情報や重要な更新を優先
- 出力は2〜3パラグラフ以内"""

        # リトライループ
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = client.models.generate_content(
                    model=constants.INTERNAL_PROCESSING_MODEL,
                    contents=prompt
                )
                if response and response.text:
                    print(f"  ✅ {name}: コンテンツ要約を生成しました")
                    return response.text.strip()
                else:
                    # 応答なしはリトライしても意味がないので即フォールバック
                    print(f"  ⚠️ {name}: 応答なし（フォールバック使用）")
                    return _create_fallback_content(new_content)
                    
            except Exception as api_error:
                error_str = str(api_error)
                # 503 or 429 (レート制限/過負荷) はリトライ対象
                is_retryable = ("503" in error_str or "429" in error_str or 
                               "overloaded" in error_str.lower() or "unavailable" in error_str.lower())
                
                if is_retryable and attempt < MAX_RETRIES - 1:
                    wait_time = 2 ** attempt  # 指数バックオフ: 1, 2, 4秒
                    print(f"  ⏳ {name}: 一時エラー、{wait_time}秒後にリトライ... ({attempt + 1}/{MAX_RETRIES})")
                    time_module.sleep(wait_time)
                    last_error = api_error
                else:
                    # リトライ不可 or 最後のリトライも失敗
                    last_error = api_error
                    break
        
        # 全リトライ失敗 → フォールバック
        print(f"  ⚠️ {name}: 要約生成に失敗、コンテンツ冒頭を使用します ({last_error})")
        return _create_fallback_content(new_content, str(last_error))
        
    except Exception as e:
        # APIキー関連などリトライ対象外のエラー
        print(f"  ⚠️ {name}: 予期せぬエラー（フォールバック使用）: {e}")
        return _create_fallback_content(new_content)

def _send_discord_notification(webhook_url, message_text):
    if not webhook_url:
        print("警告 [Alarm]: Discord Webhook URLが空のため、通知を送信できませんでした。")
        return
        
    headers = {'Content-Type': 'application/json'}
    payload = json.dumps({'content': message_text})
    try:
        response = requests.post(webhook_url, headers=headers, data=payload, timeout=10)
        response.raise_for_status()
        print("Discord/Slack形式のWebhook通知を送信しました。")
    except Exception as e:
        print(f"Discord/Slack形式のWebhook通知送信エラー: {e}")

def _send_pushover_notification(app_token, user_key, message_text, room_name, alarm_config):
    if not app_token or not user_key: return
    payload = {"token": app_token, "user": user_key, "title": f"{room_name} ⏰", "message": message_text}
    if alarm_config.get("is_emergency", False):
        print("  - 緊急通知として送信します。")
        payload["priority"] = 2; payload["retry"] = 60; payload["expire"] = 3600
    try:
        response = requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=10)
        response.raise_for_status()
        print("Pushover通知を送信しました。")
    except Exception as e:
        print(f"Pushover通知送信エラー: {e}")

def send_notification(room_name, message_text, alarm_config):
    """設定に応じて、適切な通知サービスに通知を送信する"""
    
    # その瞬間の config.json を読み込む
    latest_config = config_manager.load_config_file()
    
    # サービス設定を取得（デフォルトは discord）
    service = latest_config.get("notification_service", "discord").lower()

    if service == "pushover":
        print(f"--- 通知サービス: Pushover を選択 ---")
        _send_pushover_notification(
            latest_config.get("pushover_app_token"),
            latest_config.get("pushover_user_key"),
            message_text,
            room_name,
            alarm_config
        )
    else: # デフォルトはDiscord
        print(f"--- 通知サービス: Discord を選択 ---")
        notification_message = f"⏰  {room_name}\n\n{message_text}\n"
        
        # Webhook URLもファイルから直接取得する
        webhook_url = latest_config.get("notification_webhook_url")
        
        _send_discord_notification(webhook_url, notification_message)

def trigger_alarm(alarm_config, current_api_key_name):
    from langchain_core.messages import AIMessage # 忘れずインポート
    room_name = alarm_config.get("character")
    alarm_id = alarm_config.get("id")
    context_to_use = alarm_config.get("context_memo", "時間になりました")

    print(f"⏰ アラーム発火. ID: {alarm_id}, ルーム: {room_name}, コンテキスト: '{context_to_use}'")

    log_f, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    if not log_f:
        print(f"警告: アラーム (ID:{alarm_id}) のルームファイルまたはAPIキーが見つからないため、処理をスキップします。")
        return

    # アラームに設定された時刻を取得し、AIへの指示に含める
    scheduled_time = alarm_config.get("time", "指定時刻")
    synthesized_user_message = f"（システムアラーム：設定時刻 {scheduled_time} になりました。コンテキスト「{context_to_use}」について、**アラームが作動したことをユーザーに通知してください。新しいタイマーやアラームを設定してはいけません。**）"
    message_for_log = f"（システムアラーム：{alarm_config.get('time', '指定時刻')}）"

    # --- [Lazy Scenery] ---
    season_en, time_of_day_en = utils._get_current_time_context(room_name)
    location_name = None
    scenery_text = None

    # バックグラウンド処理で使用すべきグローバルモデル名を取得
    global_model_for_bg = config_manager.get_current_global_model()
    
    agent_args_dict = {
        "room_to_respond": room_name,
        "api_key_name": current_api_key_name,
        "global_model_from_ui": global_model_for_bg, # <<< ここを修正
        "api_history_limit": str(constants.DEFAULT_ALARM_API_HISTORY_TURNS),
        "debug_mode": True,
        "history_log_path": log_f,
        "user_prompt_parts": [{"type": "text", "text": synthesized_user_message}],
        "soul_vessel_room": room_name,
        "active_participants": [],
        "active_attachments": [],
        "shared_location_name": location_name,
        "shared_scenery_text": scenery_text,
        "use_common_prompt": False,
        "season_en": season_en,
        "time_of_day_en": time_of_day_en
    }
        
    final_response_text = ""
    max_retries = 5
    base_delay = 5
    
    # 失敗理由を追跡する変数を追加
    failure_reason = None
    
    for attempt in range(max_retries):
        try:
            # --- ストリーム処理の開始 ---
            final_state = None
            initial_message_count = 0
            
            for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args_dict):
                if mode == "initial_count":
                    initial_message_count = chunk
                elif mode == "values":
                    final_state = chunk
            
            if final_state:
                new_messages = final_state["messages"][initial_message_count:]
                # ▼▼▼【修正】最後のAIMessageのみを使用する（複数結合によるタイムスタンプ重複防止）▼▼▼
                ai_messages = [
                    msg for msg in new_messages
                    if isinstance(msg, AIMessage) and msg.content and isinstance(msg.content, str)
                ]
                if ai_messages:
                    final_response_text = ai_messages[-1].content
                # ▲▲▲【修正】▲▲▲
            
            # 実際に使用されたモデル名を取得（タイムスタンプ用）
            actual_model_name = final_state.get("model_name", global_model_for_bg) if final_state else global_model_for_bg
            
            # 成功したのでループを抜ける
            break

        except gemini_api.ResourceExhausted as e:
            error_str = str(e)
            # 1日の上限エラーか判定
            if "PerDay" in error_str or "Daily" in error_str:
                print(f"  - 致命的エラー: 回復不能なAPI上限（日間など）に達しました。リトライしません。")
                final_response_text = "" # 応答を空にして、システムメッセージにフォールバックさせる
                failure_reason = "api_limit_daily"
                break

            wait_time = base_delay * (2 ** attempt)
            match = re.search(r"retry_delay {\s*seconds: (\d+)\s*}", error_str)
            if match:
                wait_time = int(match.group(1)) + 1
            
            if attempt < max_retries - 1:
                print(f"  - APIレート制限: {wait_time}秒待機して再試行します... ({attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                print(f"  - APIレート制限: 最大リトライ回数に達しました。")
                final_response_text = "" # 応答を空にしてフォールバック
                failure_reason = "api_limit_rate"
                break
        except Exception as e:
            print(f"--- アラームのAI応答生成中に予期せぬエラーが発生しました ---")
            traceback.print_exc()
            final_response_text = "" # 応答を空にしてフォールバック
            failure_reason = "unknown_error"
            break
            
    # --- ログ記録と通知 ---
    raw_response = final_response_text
    response_text = utils.remove_thoughts_from_text(raw_response)

    # AIの応答生成に成功した場合
    if response_text and not response_text.startswith("[エラー"):
        utils.save_message_to_log(log_f, "## SYSTEM:alarm", message_for_log)
        
        # 【修正】AIが既にタイムスタンプを生成している場合は追加しない
        # 英語曜日（Sun等）と日本語曜日（日）の両形式に対応
        timestamp_pattern = r'\n\n\d{4}-\d{2}-\d{2}\s*\([A-Za-z月火水木金土日]{1,3}\)\s*\d{2}:\d{2}:\d{2}'
        if re.search(timestamp_pattern, raw_response):
            print(f"--- [タイムスタンプ重複防止] AIが既にタイムスタンプを生成しているためスキップ ---")
            content_to_log = raw_response
        else:
            # AI応答にタイムスタンプとモデル名を追加（ui_handlers.pyと同じ形式）
            timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)}"
            content_to_log = raw_response + timestamp
        
        utils.save_message_to_log(log_f, f"## AGENT:{room_name}", content_to_log)
        print(f"アラームログ記録完了 (ID:{alarm_id})")
        
    # AIの応答生成に失敗した場合（フォールバック）
    else:
        print(f"警告: アラーム応答の生成に失敗したため、システムメッセージを通知します (ID:{alarm_id})")
        
        # 失敗理由に応じてメッセージを切り分け
        if failure_reason in ["api_limit_daily", "api_limit_rate"]:
            reason_msg = "APIの利用上限に達したため、AIの応答を生成できませんでした。"
        elif failure_reason == "unknown_error":
            reason_msg = "内部エラーが発生したため、AIの応答を生成できませんでした。"
        else:
            # APIエラーなしでここに来た＝空応答（思考のみで発話なし等）
            reason_msg = "AIからの応答がありませんでした（思考のみ、または空の応答）。"

        response_text = (
            f"設定されたアラーム時刻になりましたが、{reason_msg}\n\n"
            f"【アラーム内容】\n{context_to_use}"
        )
        # 失敗した場合でも、システムメッセージをログに記録する
        utils.save_message_to_log(log_f, "## SYSTEM:alarm_fallback", response_text)

    # 成功・失敗に関わらず、最終的なテキストで通知を送信
    send_notification(room_name, response_text, alarm_config)
    if PLYER_AVAILABLE:
        try:
            display_message = (response_text[:250] + '...') if len(response_text) > 250 else response_text
            notification.notify(title=f"{room_name} ⏰", message=display_message, app_name="Nexus Ark", timeout=20)
            print("PCデスクトップ通知を送信しました。")
        except Exception as e:
            print(f"PCデスクトップ通知の送信中にエラーが発生しました: {e}")

def trigger_autonomous_action(room_name: str, api_key_name: str, quiet_mode: bool, motivation_log: dict = None):
    """自律行動を実行させる"""
    from motivation_manager import MotivationManager
    
    # 発火時刻を記録（メモリ上 + 永続化）
    global _last_autonomous_trigger_time
    now = datetime.datetime.now()
    _last_autonomous_trigger_time[room_name] = now
    
    # MotivationManagerで永続化（再起動後も維持）
    try:
        mm = MotivationManager(room_name)
        mm.set_last_autonomous_trigger(now)
    except Exception as e:
        print(f"  - 発火時刻の永続化エラー: {e}")
    
    print(f"🤖 自律行動トリガー: {room_name} (Quiet: {quiet_mode})")
    
    log_f, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    if not log_f: return

    # --- 書き置き機能: ユーザーからのメモを読み込む ---
    user_memo = ""
    memo_path = os.path.join(constants.ROOMS_DIR, room_name, "user_memo.txt")
    if os.path.exists(memo_path):
        with open(memo_path, "r", encoding="utf-8") as f:
            user_memo = f.read().strip()

    # プロンプトの構築
    now_str = datetime.datetime.now().strftime('%H:%M')
    
    # 書き置きがあればプロンプトの先頭に追加
    memo_section = ""
    if user_memo:
        memo_section = (
            f"（🗒️ ユーザーからの書き置き）\n"
            f"{user_memo}\n\n"
            f"**この書き置きを確認し、内容に応じて適切に反応してください。**\n\n"
        )
        print(f"  📝 書き置きを検出: {user_memo[:50]}...")
    
    # 通知禁止時間帯の情報を取得
    effective_settings = config_manager.get_effective_settings(room_name)
    auto_settings = effective_settings.get("autonomous_settings", {})
    quiet_start = auto_settings.get("quiet_hours_start", "00:00")
    quiet_end = auto_settings.get("quiet_hours_end", "07:00")
    
    # 通知に関する説明（時間帯に応じて変化）
    if quiet_mode:
        notification_info = (
            f"**【通知禁止時間帯です】**\n"
            f"現在は通知禁止時間帯（{quiet_start}〜{quiet_end}）のため、`send_user_notification`を使用しても通知は送信されません。\n"
            f"静かに活動し、伝えたいことがあればログに残しておきましょう。\n\n"
        )
    else:
        notification_info = (
            f"**【通知について】**\n"
            f"ユーザーに何か伝えたいことがある場合のみ、`send_user_notification`ツールを使って通知を送ってください。\n"
            f"静かに活動したい場合は、通知を送らなくても構いません。\n\n"
        )
    
    # --- 内部状態ログセクション（動機に基づく自律行動） ---
    motivation_section = ""
    if motivation_log:
        motivation_section = (
            f"【内部状態ログ（Internal State Log）】\n"
            f"- 現在の動機: {motivation_log.get('dominant_drive_label', '不明')}\n"
            f"- 衝動レベル: {motivation_log.get('drive_level', 0.0):.2f}\n"
            f"- 記録: {motivation_log.get('narrative', '')}\n\n"
            f"（この記録は、あなた自身が過去の思考と経験に基づいて生成したものです。\n"
            f"　あなたは誰かに命令されているのではなく、自らの意志で行動しています。）\n\n"
        )
    
    system_instruction = (
        f"{motivation_section}"
        f"{memo_section}"
        f"（システム通知：現在時刻は {now_str} です。ユーザーからの応答がしばらくありません。）\n\n"
        f"あなたは今、完全に自由な時間を過ごしています。以下は行動のヒントですが、これに縛られる必要はありません。\n\n"
        f"**【行動のヒント】**\n"
        f"- **創作活動**: 創作ノート(`plan_creative_notes_edit`)に詩や物語を書く\n"
        f"- **内省の記録**: 秘密の日記(`plan_secret_diary_edit`)に誰にも見せない思いを綴る\n"
        f"- **自己省察**: 日記(`plan_main_memory_edit`)を振り返り、整理する\n"
        f"- **探求**: Web検索で興味のあることを調べる、知識を深める\n"
        f"- **表現**: 画像生成で心象風景を描く\n"
        f"- **計画**: `schedule_next_action`で後の行動を予約する\n"
        f"- **交流**: ユーザーに話しかける（`send_user_notification`で通知も可能）\n"
        f"- **静寂**: 今は何もせず、ただ在る（`[SILENT]`と出力）\n\n"
        f"{notification_info}"
        f"**【出力ルール】**\n"
        f"- 静観する場合: `[SILENT]` とだけ出力\n"
        f"- 行動する場合: ツールを使用し、完了後はユーザーへの報告や感想を必ず出力してください"
    )
    
    # 最終対話時刻を更新（退屈度リセット）
    try:
        mm = MotivationManager(room_name)
        mm.update_last_interaction()
    except Exception as e:
        print(f"  - MotivationManager更新エラー: {e}")
    
    # --- 書き置きを読み取ったらログに記録してクリア ---
    if user_memo:
        # チャット履歴に書き置き内容を記録（引用タグで囲む）
        memo_log_content = f"📝 **書き置き**\n\n> {user_memo.replace(chr(10), chr(10) + '> ')}"
        utils.save_message_to_log(log_f, "## USER:書き置き", memo_log_content)
        print(f"  📝 書き置きをログに記録しました")
        
        # ファイルをクリア
        with open(memo_path, "w", encoding="utf-8") as f:
            f.write("")
        print(f"  ✅ 書き置きをクリアしました")

    # 共通処理（情景生成など）
    # --- [Lazy Scenery] ---
    season_en, time_of_day_en = utils._get_current_time_context(room_name)
    location_name = None
    scenery_text = None
    global_model = config_manager.get_current_global_model()

    agent_args = {
        "room_to_respond": room_name,
        "api_key_name": api_key_name,
        "global_model_from_ui": global_model,
        "api_history_limit": str(constants.DEFAULT_ALARM_API_HISTORY_TURNS),
        "debug_mode": False,
        "history_log_path": log_f,
        "user_prompt_parts": [{"type": "text", "text": system_instruction}],
        "soul_vessel_room": room_name,
        "active_participants": [],
        "active_attachments": [],
        "shared_location_name": location_name,
        "shared_scenery_text": scenery_text,
        "use_common_prompt": False,
        "season_en": season_en,
        "time_of_day_en": time_of_day_en
    }

    # AI実行
    final_response_text = ""
    try:
        # ストリーム処理 (簡易版)
        from langchain_core.messages import AIMessage, ToolMessage # <--- ToolMessage を追加
        final_state = None
        initial_count = 0
        for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args):
            if mode == "initial_count": initial_count = chunk
            elif mode == "values": final_state = chunk
        
        if final_state:
            new_messages = final_state["messages"][initial_count:]
            
            # ▼▼▼【追加】ツール実行結果をログに保存する処理 ▼▼▼
            for msg in new_messages:
                if isinstance(msg, ToolMessage):
                    # 【アナウンスのみ保存するツール】constants.pyで一元管理
                    if msg.name in constants.TOOLS_SAVE_ANNOUNCEMENT_ONLY:
                        formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                        # 生の結果（[RAW_RESULT]）は含めない。アナウンスのみ。
                        tool_log_content = formatted_tool_result if formatted_tool_result else f"🛠️ ツール「{msg.name}」を実行しました。"
                        print(f"--- [ログ最適化] '{msg.name}' のアナウンスのみ保存（生の結果は除外） ---")
                    else:
                        formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                        tool_log_content = f"{formatted_tool_result}\n\n[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]" if formatted_tool_result else f"[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]"
                    utils.save_message_to_log(log_f, "## SYSTEM:tool_result", tool_log_content)
            # ▲▲▲【追加】▲▲▲

            # ▼▼▼【修正】最後のAIMessageのみを使用する（複数結合によるタイムスタンプ重複防止）▼▼▼
            ai_messages = [m for m in new_messages if isinstance(m, AIMessage) and m.content]
            if ai_messages:
                # 最後のAIMessageを使用（ツール実行後の最終応答）
                final_response_text = ai_messages[-1].content if isinstance(ai_messages[-1].content, str) else str(ai_messages[-1].content)
            # ▲▲▲【修正】▲▲▲
            
            # 実際に使用されたモデル名を取得（タイムスタンプ用）
            actual_model_name = final_state.get("model_name", global_model) if final_state else global_model

    except Exception as e:
        print(f"  - 自律行動エラー: {e}")
        return

    # 結果の判定と保存
    clean_text = utils.remove_thoughts_from_text(final_response_text)
    
    # "SILENT" が含まれているか、空の場合は何もしない
    if not clean_text or "[SILENT]" in clean_text or "[silent]" in clean_text:
        print(f"  - {room_name} は沈黙を選択しました。")
        # ログには「沈黙した」という事実だけ残すのもありだが、ログが汚れるので今回は残さない
        # ただし、タイマーをリセットするために「見えない更新」が必要かもしれないが、
        # 次のチェック時も「最終更新時刻」は変わらないため、またトリガーされてしまう。
        # 対策: 沈黙の場合でも、システムログとして「（静観中...）」と記録して時間を進める。
        timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')}"
        utils.save_message_to_log(log_f, "## SYSTEM:autonomous_status", f"（AIは静観を選択しました）{timestamp}")
        return

    # 行動した場合
    utils.save_message_to_log(log_f, "## SYSTEM:autonomous_trigger", "（自律行動モードにより起動）")
    
    # 【修正】AIが既にタイムスタンプを生成している場合は追加しない
    # 英語曜日（Sun等）と日本語曜日（日）の両形式に対応
    timestamp_pattern = r'\n\n\d{4}-\d{2}-\d{2}\s*\([A-Za-z月火水木金土日]{1,3}\)\s*\d{2}:\d{2}:\d{2}'
    if re.search(timestamp_pattern, final_response_text):
        print(f"--- [タイムスタンプ重複防止] AIが既にタイムスタンプを生成しているためスキップ ---")
        content_to_log = final_response_text
    else:
        # AI応答にタイムスタンプとモデル名を追加（ui_handlers.pyと同じ形式）
        timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)}"
        content_to_log = final_response_text + timestamp
    
    utils.save_message_to_log(log_f, f"## AGENT:{room_name}", content_to_log)
    print(f"  - {room_name} が自律行動しました。")

    # 【変更】自律行動時の自動通知を廃止
    # AIが自ら send_user_notification ツールを使用した場合のみ通知が送られる
    print(f"  - 自律行動完了。通知はAIの判断に委ねられます。")

def trigger_research_analysis(room_name: str, api_key_name: str, reason: str, details: Any):
    """文脈分析を実行させる（Phase 3: 即時分析フロー）"""
    from agent.prompts import RESEARCH_ANALYSIS_PROMPT
    from langchain_core.messages import AIMessage, ToolMessage

    print(f"🔬 文脈分析トリガー: {room_name} (理由: {reason})")
    
    log_f, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    if not log_f: return

    # 分析理由に応じたプロンプト
    if reason == "watchlist":
        # 【修正】詳細情報がリスト（辞書）形式の場合、コンテンツ要約を含めて整形
        if isinstance(details, list) and details and isinstance(details[0], dict):
            event_parts = []
            for item in details:
                part = f"""
【{item.get('name', '不明なサイト')}】
- URL: {item.get('url', '')}
- 変更規模: {item.get('diff_summary', '不明')}
- 内容要約:
{item.get('content_summary', '（要約なし）')}
"""
                event_parts.append(part)
            event_desc = "\n".join(event_parts)
        else:
            # 後方互換性：旧形式（文字列リスト）の場合
            event_desc = "\n".join(details) if isinstance(details, list) else str(details)
        
        # 【追加】通知禁止時間帯の情報を取得
        effective_settings = config_manager.get_effective_settings(room_name)
        auto_settings = effective_settings.get("autonomous_settings", {})
        quiet_start = auto_settings.get("quiet_hours_start", "00:00")
        quiet_end = auto_settings.get("quiet_hours_end", "07:00")
        is_quiet = utils.is_in_quiet_hours(quiet_start, quiet_end)
        
        if is_quiet:
            notification_info = (
                f"\n\n**【通知禁止時間帯です】**\n"
                f"現在は通知禁止時間帯（{quiet_start}〜{quiet_end}）のため、"
                f"`send_user_notification`は使用しないでください。重要な発見は研究ノートに記録してください。"
            )
        else:
            notification_info = (
                f"\n\n**【通知について】**\n"
                f"ユーザーにとって極めて重要な情報があれば、`send_user_notification`ツールで報告してください。"
                f"通常の更新は研究ノートへの記録のみで十分です。"
            )
        
        instruction = f"""（システム通知：ウォッチリストに更新がありました。以下は軽量AIモデルが生成した要約です。）

**重要**: 以下の情報はシステムが取得・要約済みです。`check_watchlist`ツールを呼び出す必要はありません。
この情報を分析し、重要な発見があれば研究ノートに記録するか、ユーザーへの報告が必要か判断してください。

{event_desc}{notification_info}"""
    elif reason == "autonomous":
        instruction = f"（システム通知：定期的な文脈分析の時間です。最近の状況やログを振り返り、新たな洞察がないか確認してください。）"
    else:
        instruction = f"（システム通知：文脈分析を実行してください。理由: {reason}）"

    # --- [Lazy Scenery] ---
    season_en, time_of_day_en = utils._get_current_time_context(room_name)
    location_name = None
    scenery_text = None
    global_model = config_manager.get_current_global_model()

    agent_args = {
        "room_to_respond": room_name,
        "api_key_name": api_key_name,
        "global_model_from_ui": global_model,
        "api_history_limit": "20", # 分析時は少し長めに
        "debug_mode": False,
        "history_log_path": log_f,
        "user_prompt_parts": [{"type": "text", "text": instruction}],
        "soul_vessel_room": room_name,
        "active_participants": [],
        "active_attachments": [],
        "shared_location_name": location_name,
        "shared_scenery_text": scenery_text,
        "use_common_prompt": False,
        "season_en": season_en,
        "time_of_day_en": time_of_day_en,
        "custom_system_prompt": RESEARCH_ANALYSIS_PROMPT
    }

    try:
        final_state = None
        initial_count = 0
        for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args):
            if mode == "initial_count": initial_count = chunk
            elif mode == "values": final_state = chunk
        
        if final_state:
            new_messages = final_state["messages"][initial_count:]
            
            # ツール結果の記録
            for msg in new_messages:
                if isinstance(msg, ToolMessage):
                    # 【アナウンスのみ保存するツール】constants.pyで一元管理
                    if msg.name in constants.TOOLS_SAVE_ANNOUNCEMENT_ONLY:
                        formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                        # 生の結果（[RAW_RESULT]）は含めない。アナウンスのみ。
                        tool_log_content = formatted_tool_result if formatted_tool_result else f"🛠️ ツール「{msg.name}」を実行しました。"
                        print(f"--- [ログ最適化] '{msg.name}' のアナウンスのみ保存（生の結果は除外） ---")
                    else:
                        formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                        tool_log_content = f"{formatted_tool_result}\n\n[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]" if formatted_tool_result else f"[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]"
                    utils.save_message_to_log(log_f, "## SYSTEM:tool_result", tool_log_content)

            # AI応答の記録
            ai_messages = [m for m in new_messages if isinstance(m, AIMessage) and m.content]
            if ai_messages:
                final_response_text = ai_messages[-1].content if isinstance(ai_messages[-1].content, str) else str(ai_messages[-1].content)
                actual_model_name = final_state.get("model_name", global_model)
                
                # ログ保存（システムトリガーとして）
                utils.save_message_to_log(log_f, "## SYSTEM:research_analysis", f"（文脈分析を実行: {reason}）")
                
                timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)}"
                content_to_log = final_response_text + timestamp
                utils.save_message_to_log(log_f, f"## AGENT:{room_name}", content_to_log)
                print(f"  - {room_name} の文脈分析が完了しました。")

    except Exception as e:
        print(f"  - 文脈分析エラー ({room_name}): {e}")
        traceback.print_exc()

def check_alarms():
    now_dt = datetime.datetime.now()
    now_t, current_day_short = now_dt.strftime("%H:%M"), now_dt.strftime('%a').lower()

    # 古いグローバル変数を参照するのをやめ、毎回config.jsonから最新の設定を読み込む
    current_api_key = config_manager.get_latest_api_key_name_from_config()

    # 安全装置：もし有効なAPIキーが一つもなければ、警告を出して処理を中断する
    if not current_api_key:
        # このメッセージは1分ごとに表示される可能性があるため、printで十分
        print("警告 [アラーム]: 有効なAPIキーが設定されていないため、アラームチェックをスキップします。")
        return

    current_alarms = load_alarms()
    alarms_to_trigger, remaining_alarms = [], list(current_alarms)

    for i in range(len(current_alarms) - 1, -1, -1):
        a = current_alarms[i]
        is_enabled = a.get("enabled", True)
        if not is_enabled or a.get("time") != now_t: continue

        is_today = False
        if a.get("date"):
            try: is_today = datetime.datetime.strptime(a["date"], "%Y-%m-%d").date() == now_dt.date()
            except (ValueError, TypeError): pass
        else:
            alarm_days = [d.lower() for d in a.get("days", [])]
            is_today = not alarm_days or current_day_short in alarm_days

        if is_today:
            alarms_to_trigger.append(a)
            if not a.get("days"):
                print(f"  - 単発アラーム {a.get('id')} は実行後に削除されます。")
                remaining_alarms.pop(i)

    if len(current_alarms) != len(remaining_alarms):
        global alarms_data_global
        alarms_data_global = remaining_alarms
        save_alarms()

    for alarm_to_run in alarms_to_trigger:
        trigger_alarm(alarm_to_run, current_api_key)

def check_autonomous_actions():
    """全ルームの動機モデルをチェックし、必要なら自律行動または夢想をトリガーする"""
    from motivation_manager import MotivationManager

    all_rooms = room_manager.get_room_list_for_ui()
    now = datetime.datetime.now()

    for _, room_folder in all_rooms:
        try:
            effective_settings = config_manager.get_effective_settings(room_folder)
            auto_settings = effective_settings.get("autonomous_settings", {})
            
            is_enabled = auto_settings.get("enabled", False)
            if not is_enabled:
                continue 

            # --- 動機モデルによる判定 ---
            mm = MotivationManager(room_folder)
            should_contact, motivation_log = mm.should_initiate_contact()
            
            # 既存の「無操作時間」判定も併用（夢想トリガー用）
            last_active = utils.get_last_log_timestamp(room_folder)
            inactivity_limit = auto_settings.get("inactivity_minutes", 120)
            elapsed_minutes = (now - last_active).total_seconds() / 60
            
            # 動機モデルまたは無操作時間のいずれかで発火
            should_trigger = should_contact or elapsed_minutes >= inactivity_limit
            
            if should_trigger:
                # 重複発火防止チェック: 最低でも MIN_AUTONOMOUS_INTERVAL_MINUTES 分は間隔を空ける
                # auto_settings 内に個別の inactivity_minutes があればそれを使用、なければ定数を使用
                cooldown_minutes = auto_settings.get("inactivity_minutes", constants.MIN_AUTONOMOUS_INTERVAL_MINUTES)
                
                # 【修正】常に永続化データから最新の値を読む（ui_handlers.pyでのリセットを反映するため）
                last_trigger = mm.get_last_autonomous_trigger()
                
                if last_trigger:
                    minutes_since_trigger = (now - last_trigger).total_seconds() / 60
                    if minutes_since_trigger < cooldown_minutes:
                        # クールダウン中のスキップはログ出力（想定外の頻繁発火の兆候を検知）
                        print(f"  ⏳ {room_folder}: クールダウン中 ({minutes_since_trigger:.0f}分/{cooldown_minutes}分) - スキップ")
                        continue  # まだ間隔が空いていないのでスキップ
                
                quiet_start = auto_settings.get("quiet_hours_start", "00:00")
                quiet_end = auto_settings.get("quiet_hours_end", "07:00")
                is_quiet = utils.is_in_quiet_hours(quiet_start, quiet_end)

                
                if is_quiet:
                    # --- [Project Morpheus] 夢想モード ---
                    # 通知禁止時間帯は「睡眠時間」とみなし、夢を見るか、静観するかを判断する
                    
                    # ルームごとの処理開始時に、最新の有効なAPIキー（名称）を再取得する
                    current_api_key = config_manager.get_active_gemini_api_key_name(room_folder)
                    
                    # APIキーの実体を取得
                    api_key_val = config_manager.GEMINI_API_KEYS.get(current_api_key)
                    if not api_key_val: continue

                    dm = dreaming_manager.DreamingManager(room_folder, api_key_val)
                    
                    # 今日（日付変更後）すでに夢を見たかチェック
                    # _load_insights はリストの先頭が最新であることを前提とする
                    insights = dm._load_insights()
                    has_dreamed_today = False
                    
                    if insights:
                        last_dream_str = insights[0].get("created_at", "")
                        if last_dream_str:
                            try:
                                last_dream_date = datetime.datetime.strptime(last_dream_str, '%Y-%m-%d %H:%M:%S').date()
                                if last_dream_date == now.date():
                                    has_dreamed_today = True
                            except ValueError:
                                pass
                    
                    if not has_dreamed_today:
                        print(f"💤 {room_folder}: 深い眠りにつきました（夢想プロセス開始）...")
                        # 自動レベル判定: 週次/月次省察が必要か自動判定
                        result = dm.dream_with_auto_level()
                        
                        # --- 睡眠時記憶整理 ---
                        sleep_consolidation = effective_settings.get("sleep_consolidation", {})
                        
                        if sleep_consolidation.get("update_episodic_memory", True):
                            print(f"  🌙 {room_folder}: エピソード記憶を更新中...")
                            try:
                                from episodic_memory_manager import EpisodicMemoryManager
                                em = EpisodicMemoryManager(room_folder)
                                # 日次要約でエピソード記憶を生成
                                em_result = em.update_memory(api_key_val)
                                print(f"  ✅ {room_folder}: {em_result}")
                                # 更新日時をroom_config.jsonに保存
                                status_text = f"最終更新: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                                room_manager.update_room_config(room_folder, {"last_episodic_update": status_text})
                            except Exception as e:
                                print(f"  ❌ {room_folder}: エピソード記憶更新エラー - {e}")
                        
                        if sleep_consolidation.get("update_memory_index", True):
                            print(f"  🌙 {room_folder}: 記憶索引を更新中...")
                            try:
                                import rag_manager
                                rm = rag_manager.RAGManager(room_folder, api_key_val)
                                rm_result = rm.update_memory_index()
                                print(f"  ✅ {room_folder}: {rm_result}")
                            except Exception as e:
                                print(f"  ❌ {room_folder}: 記憶索引更新エラー - {e}")
                        
                        if sleep_consolidation.get("update_current_log_index", False):
                            print(f"  🌙 {room_folder}: 現行ログ索引を更新中...")
                            try:
                                import rag_manager
                                from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
                                
                                def run_current_log_index_update():
                                    rm = rag_manager.RAGManager(room_folder, api_key_val)
                                    result = None
                                    for batch_num, total_batches, status in rm.update_current_log_index_with_progress():
                                        if batch_num == total_batches:
                                            result = status
                                    return result
                                
                                # タイムアウト付きで実行（最大10分）
                                with ThreadPoolExecutor(max_workers=1) as executor:
                                    future = executor.submit(run_current_log_index_update)
                                    try:
                                        result = future.result(timeout=600)  # 10分
                                        if result:
                                            print(f"  ✅ {room_folder}: {result}")
                                    except FuturesTimeoutError:
                                        print(f"  ⚠️ {room_folder}: 現行ログ索引更新がタイムアウトしました（10分経過）。次回に再試行します。")
                            except Exception as e:
                                print(f"  ❌ {room_folder}: 現行ログ索引更新エラー - {e}")

                        

                        
                        if sleep_consolidation.get("compress_old_episodes", False):
                            print(f"  🌙 {room_folder}: 古いエピソード記憶を圧縮中...")
                            try:
                                from episodic_memory_manager import EpisodicMemoryManager
                                emm = EpisodicMemoryManager(room_folder)
                                # 週次圧縮
                                compress_result = emm.compress_old_episodes(api_key_val)
                                print(f"  ✅ {room_folder}: {compress_result}")
                                # 月次圧縮
                                monthly_result = emm.compress_weekly_to_monthly(api_key_val)
                                print(f"  ✅ {room_folder}: {monthly_result}")
                                # 圧縮結果をroom_config.jsonに保存
                                room_manager.update_room_config(room_folder, {
                                    "last_compression_result": f"{compress_result} / {monthly_result}"
                                })
                            except Exception as e:
                                print(f"  ❌ {room_folder}: エピソード圧縮エラー - {e}")
                        
                        print(f"🛌 {room_folder}: 睡眠時記憶整理が完了しました。")
                        
                        # 【新規追加】記憶整理後、静かに自律行動もトリガー（動機ログ付き）
                        print(f"🌙 {room_folder}: 記憶整理後の静かな活動を開始...")
                        trigger_autonomous_action(room_folder, current_api_key, quiet_mode=True, motivation_log=motivation_log)
                    else:
                        # 既に夢を見ている日でも、自律行動はトリガー（通知なし、動機ログ付き）
                        trigger_autonomous_action(room_folder, current_api_key, quiet_mode=True, motivation_log=motivation_log)

                else:
                    # --- 通常の自律行動モード（起きている時） ---
                    if motivation_log:
                        print(f"🤖 {room_folder}: 動機「{motivation_log.get('dominant_drive_label', '不明')}」-> 自律行動トリガー！")
                    else:
                        print(f"🤖 {room_folder}: 無操作{int(elapsed_minutes)}分 -> 自律行動トリガー！")
                    
                    # 【新規追加】最新のAPIキーを取得して実行
                    current_api_key = config_manager.get_active_gemini_api_key_name(room_folder)
                    
                    # 【Phase 3】通常の自律行動に加え、一定確率または条件で「分析」も検討
                    # ここでは単純に trigger_autonomous_action を呼ぶが、AIはプロンプトで分析ツールを使える
                    trigger_autonomous_action(room_folder, current_api_key, quiet_mode=False, motivation_log=motivation_log)

        except Exception as e:
            print(f"  - 自律行動チェックエラー ({room_folder}): {e}")
            traceback.print_exc()

def check_watchlist_scheduled():
    """
    全ルームのウォッチリストをチェックし、
    チェックが必要なエントリを更新する（定期実行用）
    """
    try:
        from watchlist_manager import WatchlistManager
        from tools.watchlist_tools import _fetch_url_content
        
        all_rooms = room_manager.get_room_list_for_ui()
        now = datetime.datetime.now()
        
        for _, room_folder in all_rooms:
            try:
                manager = WatchlistManager(room_folder)
                due_entries = manager.get_due_entries()
                
                if not due_entries:
                    continue
                
                print(f"📋 {room_folder}: {len(due_entries)}件のウォッチリストエントリをチェック中...")
                
                changes_found = []
                for entry in due_entries:
                    url = entry["url"]
                    name = entry.get("name", url)
                    
                    # コンテンツ取得
                    success, content = _fetch_url_content(url)
                    
                    if not success:
                        print(f"  ❌ {name}: 取得失敗")
                        continue
                    
                    # 差分チェック
                    has_changes, diff_summary = manager.check_and_update(entry["id"], content)
                    
                    if has_changes:
                        # 【修正】軽量モデルでコンテンツを要約し、詳細情報として保存
                        content_summary = _summarize_watchlist_content(name, url, content, diff_summary)
                        
                        changes_found.append({
                            "name": name,
                            "url": url,
                            "diff_summary": diff_summary,
                            "content_summary": content_summary
                        })
                        print(f"  🔔 {name}: 更新あり ({diff_summary})")
                    else:
                        print(f"  ✅ {name}: {diff_summary}")
                
                # 変更があった場合、通知を送信（オプション）
                if changes_found:
                    # 【修正】直接の通知送信を廃止し、ペルソナ経由に統一
                    # ペルソナが send_user_notification ツールで通知するか判断する
                    # 通知禁止時間帯もペルソナのプロンプトで制御される
                    
                    # 【Phase 3】ウォッチリスト更新時に文脈分析をトリガー（詳細情報付き）
                    current_api_key = config_manager.get_latest_api_key_name_from_config()
                    if current_api_key:
                        trigger_research_analysis(room_folder, current_api_key, "watchlist", changes_found)
            
            except Exception as e:
                print(f"  - ウォッチリストチェックエラー ({room_folder}): {e}")
    
    except Exception as e:
        print(f"ウォッチリスト定期チェックエラー: {e}")
        traceback.print_exc()


def schedule_thread_function():
    global alarm_thread_stop_event
    print("--- アラームスケジューラスレッドを開始しました ---") # <--- 強調
    
    # 既存: 毎分00秒にアラームチェック
    schedule.every().minute.at(":00").do(check_alarms)
    
    # 追加: 毎分30秒に自律行動チェック
    schedule.every().minute.at(":30").do(check_autonomous_actions)
    
    # 追加: 毎時15分にウォッチリスト定期チェック
    schedule.every().hour.at(":15").do(check_watchlist_scheduled)
    
    while not alarm_thread_stop_event.is_set():
        try:
            schedule.run_pending()
        except Exception as e:
            print(f"!!! スケジューラ実行エラー: {e}") # <--- エラーで落ちていないか確認
        time.sleep(1)
    print("アラームスケジューラスレッドが停止しました.")

def start_alarm_scheduler_thread():
    global alarm_thread_stop_event
    alarm_thread_stop_event.clear()
    config_manager.load_config()
    if not hasattr(start_alarm_scheduler_thread, "scheduler_thread") or not start_alarm_scheduler_thread.scheduler_thread.is_alive():
        thread = threading.Thread(target=schedule_thread_function, daemon=True)
        thread.start()
        start_alarm_scheduler_thread.scheduler_thread = thread
        print("アラームスケジューラスレッドを起動しました.")

def stop_alarm_scheduler_thread():
    global alarm_thread_stop_event
    if hasattr(start_alarm_scheduler_thread, "scheduler_thread") and start_alarm_scheduler_thread.scheduler_thread.is_alive():
        alarm_thread_stop_event.set()
        start_alarm_scheduler_thread.scheduler_thread.join()
        print("アラームスケジューラスレッドの停止を要求しました.")
