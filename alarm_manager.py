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
import ui_handlers # ← この行を追加

try:
    from plyer import notification
    PLYER_AVAILABLE = True
except ImportError:
    print("情報: 'plyer'ライブラリが見つかりません。PCデスクトップ通知機能は無効になります。")
    print(" -> pip install plyer でインストールできます。")
    PLYER_AVAILABLE = False

alarms_data_global = []
alarm_thread_stop_event = threading.Event()

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

def _send_discord_notification(webhook_url, message_text):
    if not webhook_url: return
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
    service = config_manager.NOTIFICATION_SERVICE_GLOBAL.lower()

    if service == "pushover":
        print(f"--- 通知サービス: Pushover を選択 ---")
        _send_pushover_notification(
            config_manager.PUSHOVER_CONFIG.get("app_token"),
            config_manager.PUSHOVER_CONFIG.get("user_key"),
            message_text,
            room_name,
            alarm_config
        )
    else: # デフォルトはDiscord
        print(f"--- 通知サービス: Discord を選択 ---")
        notification_message = f"⏰  {room_name}\n\n{message_text}\n"
        _send_discord_notification(config_manager.NOTIFICATION_WEBHOOK_URL_GLOBAL, notification_message)

def trigger_alarm(alarm_config, current_api_key_name):
    from langchain_core.messages import AIMessage # 忘れずインポート
    room_name = alarm_config.get("character")
    alarm_id = alarm_config.get("id")
    context_to_use = alarm_config.get("context_memo", "時間になりました")

    print(f"⏰ アラーム発火. ID: {alarm_id}, ルーム: {room_name}, コンテキスト: '{context_to_use}'")

    log_f, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    api_key = config_manager.GEMINI_API_KEYS.get(current_api_key_name)

    if not log_f or not api_key:
        print(f"警告: アラーム (ID:{alarm_id}) のルームファイルまたはAPIキーが見つからないため、処理をスキップします。")
        return

    # アラームに設定された時刻を取得し、AIへの指示に含める
    scheduled_time = alarm_config.get("time", "指定時刻")
    synthesized_user_message = f"（システムアラーム：設定時刻 {scheduled_time} になりました。コンテキスト「{context_to_use}」について、アラームメッセージを伝えてください）"
    message_for_log = f"（システムアラーム：{alarm_config.get('time', '指定時刻')}）"

    from agent.graph import generate_scenery_context
    # ▼▼▼【ここから下のブロックを書き換え】▼▼▼
    # 1. 適用すべき時間コンテキストを取得
    season_en, time_of_day_en = ui_handlers._get_current_time_context(room_name)
    # 2. 情景生成時に時間コンテキストを渡す
    location_name, _, scenery_text = generate_scenery_context(
        room_name, api_key, season_en=season_en, time_of_day_en=time_of_day_en
    )

    agent_args_dict = {
        "room_to_respond": room_name,
        "api_key_name": current_api_key_name,
        "api_history_limit": str(constants.DEFAULT_ALARM_API_HISTORY_TURNS),
        "debug_mode": True, # アラーム発火時はデバッグ情報を常に出力
        "history_log_path": log_f,
        "user_prompt_parts": [{"type": "text", "text": synthesized_user_message}],
        "soul_vessel_room": room_name,
        "active_participants": [],
        "shared_location_name": location_name,
        "shared_scenery_text": scenery_text,
        "use_common_prompt": False, # ← 思考をシンプルにするため、ツールプロンプトを無効化
        # 3. AIの引数にも時間コンテキストを追加
        "season_en": season_en,
        "time_of_day_en": time_of_day_en
    }
    # ▲▲▲【書き換えはここまで】▲▲▲


    # ▼▼▼【ここから下のブロックを、既存のストリーム処理ロジックと完全に置き換えてください】▼▼▼
    final_response_text = ""
    final_state = None
    initial_message_count = 0 # 履歴の初期数を保持

    # gemini_api.pyからストリームデータを受け取る
    for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args_dict):
        if mode == "initial_count":
            initial_message_count = chunk
        elif mode == "values":
            final_state = chunk # valuesの最後のものが最終状態になる

    # ストリーム完了後、最終状態からAIの応答を再構築する
    if final_state:
        # 新しく追加されたメッセージ（AIの応答）のみを抽出
        new_messages = final_state["messages"][initial_message_count:]
        # AIMessageのcontentをすべて結合する
        all_ai_contents = [
            msg.content for msg in new_messages
            if isinstance(msg, AIMessage) and msg.content and isinstance(msg.content, str)
        ]
        final_response_text = "\n\n".join(all_ai_contents).strip()
    # ▲▲▲【置き換えはここまで】▲▲▲

    # 思考ログを含む完全な応答を raw_response とする（ログ記録用）
    raw_response = final_response_text
    # 表示・通知用には思考ログを除去する
    response_text = utils.remove_thoughts_from_text(raw_response)

    if response_text and not response_text.startswith("[エラー"):
        # ログヘッダーを新しい形式 `ROLE:NAME` に準拠させる
        utils.save_message_to_log(log_f, "## SYSTEM:alarm", message_for_log)
        utils.save_message_to_log(log_f, f"## AGENT:{room_name}", raw_response)
        print(f"アラームログ記録完了 (ID:{alarm_id})")
        send_notification(room_name, response_text, alarm_config)
        if PLYER_AVAILABLE:
            try:
                display_message = (response_text[:250] + '...') if len(response_text) > 250 else response_text
                notification.notify(title=f"{room_name} ⏰", message=display_message, app_name="Nexus Ark", timeout=20)
                print("PCデスクトップ通知を送信しました。")
            except Exception as e:
                print(f"PCデスクトップ通知の送信中にエラーが発生しました: {e}")
    else:
        # 失敗した場合でも、取得できた生の応答をログに出力する
        print(f"警告: アラーム応答の生成に失敗 (ID:{alarm_id}). AIからの生応答: '{raw_response}'")

def check_alarms():
    now_dt = datetime.datetime.now()
    now_t, current_day_short = now_dt.strftime("%H:%M"), now_dt.strftime('%a').lower()

    # ▼▼▼【このブロックを全面的に書き換え】▼▼▼
    # 古いグローバル変数を参照するのをやめ、毎回config.jsonから最新の設定を読み込む
    current_api_key = config_manager.get_latest_api_key_name_from_config()

    # 安全装置：もし有効なAPIキーが一つもなければ、警告を出して処理を中断する
    if not current_api_key:
        # このメッセージは1分ごとに表示される可能性があるため、printで十分
        print("警告 [アラーム]: 有効なAPIキーが設定されていないため、アラームチェックをスキップします。")
        return
    # ▲▲▲【書き換えはここまで】▲▲▲

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

    # if current_api_key: # このif文は上の安全装置に統合されたので不要
    for alarm_to_run in alarms_to_trigger:
        trigger_alarm(alarm_to_run, current_api_key)

def schedule_thread_function():
    global alarm_thread_stop_event
    print("アラームスケジューラスレッドを開始します.")
    schedule.every().minute.at(":00").do(check_alarms)
    while not alarm_thread_stop_event.is_set():
        schedule.run_pending(); time.sleep(1)
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
