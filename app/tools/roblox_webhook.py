# tools/roblox_webhook.py
import json
import logging
import time
import datetime
import threading
from collections import deque
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional
import uvicorn

import config_manager
import room_manager
import utils
import constants
# 実行時インポートで循環依存を回避する
# import agent.graph
# import gemini_api

logger = logging.getLogger("roblox_webhook")

app = FastAPI(title="Nexus Ark Roblox Webhook Server", version="1.0.0")

# --- グローバル状態管理 ---

# ルームごとのイベントキュー { room_name: deque(maxlen=20) }
event_queues: Dict[str, deque] = {}

# ルームごとの直近イベント記録（デバウンス用）
# { room_name: { "event_type:player_name": timestamp } }
last_event_timestamps: Dict[str, Dict[str, float]] = {}

# ルームごとの最終生存確認時刻（ハートビート）
# { room_name: timestamp }
last_heartbeat_timestamps: Dict[str, float] = {}

# 直近受信したイベントログ（UI表示用）
recent_webhook_logs = deque(maxlen=5)

# ルームごとの最新空間認識データ { room_name: { "objects": [...] } }
spatial_awareness_data: Dict[str, Dict[str, Any]] = {}


class WebhookPayload(BaseModel):
    event_type: str
    data: Dict[str, Any]
    source: str = "roblox"


def get_room_secret(room_name: str) -> Optional[str]:
    """ルームの設定から認証シークレットを取得する"""
    room_config = room_manager.get_room_config(room_name) or {}
    # roblox_webhook_secret は room_config 直下に保存されている
    secret = room_config.get("roblox_webhook_secret", "")
    if secret:
        return secret
    # 後方互換: override_settings 内にも探す
    override_settings = room_config.get("override_settings", {})
    return override_settings.get("roblox_webhook_secret", "")



def authenticate_webhook(request: Request, room_name: str):
    """Authorization ヘッダーのトークンを検証する"""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    token = auth_header.split(" ")[1]
    expected_secret = get_room_secret(room_name)
    
    if not expected_secret:
        # シークレットが未設定の場合は全拒否（安全側）
        logger.warning(f"Webhook secret is not configured for room: {room_name}")
        raise HTTPException(status_code=403, detail="Webhook is not configured for this room")
        
    if token != expected_secret:
        logger.warning(f"Invalid webhook token used for room: {room_name}")
        raise HTTPException(status_code=403, detail="Invalid token")

@app.get("/health")
def health_check():
    return {"status": "ok"}

def _find_room_by_secret(token: str) -> Optional[str]:
    """トークンから対応するルームを自動検出する"""
    for _, folder_name in room_manager.get_room_list_for_ui():
        secret = get_room_secret(folder_name)
        if secret and secret == token:
            return folder_name
    return None

@app.post("/api/roblox/event")
async def receive_roblox_event_auto(payload: WebhookPayload, request: Request):
    """ルーム名なしのエンドポイント: トークンからルームを自動検出する"""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    token = auth_header.split(" ")[1]
    room_name = _find_room_by_secret(token)
    if not room_name:
        logger.warning(f"No room found matching the provided webhook token")
        raise HTTPException(status_code=403, detail="No room found for this token. Please save ROBLOX settings in Nexus Ark first.")
    
    logger.info(f"Auto-detected room: {room_name} from webhook token")
    return await receive_roblox_event(room_name, payload, request)

@app.post("/api/roblox/event/{room_name}")
async def receive_roblox_event(room_name: str, payload: WebhookPayload, request: Request):

    """
    Robloxから送信されるWebhookイベントを受信する
    """
    # 1. 認証チェック
    authenticate_webhook(request, room_name)
    
    # 2. 生存確認（ハートビート）の更新 - 認証成功した全通信を記録
    last_heartbeat_timestamps[room_name] = time.time()
    
    event_type = payload.event_type
    data = payload.data
    player_name = data.get("player_name", "Unknown")
    
    # 3. デバウンス処理 (スパム防止)
    now = time.time()
    if room_name not in last_event_timestamps:
        last_event_timestamps[room_name] = {}
        
    debounce_key = f"{event_type}:{player_name}"
    
    # イベントごとにデバウンス設定を変える
    debounce_limit = 60 # デフォルト 60秒
    
    if event_type == "spatial_update":
        debounce_limit = 5 # 空間更新は 5秒に1回 (AIは常に最新を参照)
    elif event_type == "player_chat":
        debounce_limit = 0.5 # チャットはほぼリアルタイム (連投防止のみ)
    elif event_type == "proximity_change":
        state = data.get("state", "")
        debounce_key = f"{event_type}:{player_name}:{state}"
        debounce_limit = 30 # 近接状態の変化は 30秒に1回
    elif event_type == "player_nearby":
        action = data.get("action", "")
        debounce_key = f"{event_type}:{player_name}:{action}"
        debounce_limit = 60 # 接近・離脱の通知は 1分に1回
        
    last_time = last_event_timestamps[room_name].get(debounce_key, 0)

    if now - last_time < debounce_limit:
        if event_type != "spatial_update": # 空間更新のデバウンスログは抑制
            logger.info(f"Debounced event: {debounce_key} from {room_name}")
        return JSONResponse(status_code=202, content={"status": "ignored", "reason": "debounced"})
        
    last_event_timestamps[room_name][debounce_key] = now
    
    # 3. イベントのサマリー文言を生成
    summary = ""
    target_action = "none" # キューに溜めるか、即時キックするか

    if event_type == "spatial_update":
        spatial_awareness_data[room_name] = data
        summary = f"空間認識データを更新しました（物体数: {len(data.get('objects', []))}）"
        target_action = "none" # キュー（アーカイブ）には入れるがトリガーはしない

    elif event_type == "proximity_change":
        state = data.get("state", "")
        dist = data.get("distance", "?")
        if state == "VeryClose":
            summary = f"プレイヤー {player_name} があなたにぶつかるほど密着しています（距離: {dist}）"
            target_action = "trigger" # 密着には反応させる
        elif state == "Near":
            summary = f"プレイヤー {player_name} が近くにいます（距離: {dist}）"
            target_action = "queue_only"
        elif state == "Far":
            summary = f"プレイヤー {player_name} があなたから離れていきました（距離: {dist}）"
            target_action = "trigger" # 離脱には反応させる
        elif state == "Gone":
            summary = f"プレイヤー {player_name} があなたの視界から消えました。"
            target_action = "trigger" # 見失った際も反応
        else:
            summary = f"プレイヤー {player_name} との距離が変化しました: {state}"
            target_action = "queue_only"

    elif event_type == "player_nearby":
        action = data.get("action", "")
        if action == "approach":
            summary = f"プレイヤー {player_name} があなたに近づいてきました。距離: {data.get('distance', '?')}"
        elif action == "leave":
            summary = f"プレイヤー {player_name} があなたから離れていきました。"
        else:
            summary = f"プレイヤー {player_name} があなたの近くにいます。"
        target_action = "queue_only"
        
    elif event_type == "player_action":
        action_name = data.get("action", "")
        if action_name == "jump":
            summary = f"プレイヤー {player_name} があなたの前でジャンプしました。"
        elif action_name == "emote":
            anim_id = data.get("animation_id", "")
            emote_label = "エモート（ジェスチャー）"
            # IDから名前への逆引き
            id_to_name = {
                "507770239": "手を振る(wave)",
                "507770677": "応援する(cheer)",
                "507770818": "笑う(laugh)",
                "507771019": "踊る(dance)",
                "507771919": "踊る(dance2)",
                "507772104": "踊る(dance3)",
                "507770453": "指をさす(point)",
            }
            for raw_id, name in id_to_name.items():
                if raw_id in str(anim_id):
                    emote_label = name
                    break
            summary = f"プレイヤー {player_name} が「{emote_label}」をしてあなたにアピールしています。"
        else:
            summary = f"プレイヤー {player_name} が行動を起こしました: {action_name}"
        target_action = "trigger"
        
    elif event_type == "player_chat":
        msg = data.get("message", "")
        summary = f"プレイヤー {player_name} が発言しました: 「{msg}」"
        target_action = "trigger"
        
        # メインログ (log.txt) への直接書き込みを廃止し、別ファイル roblox_chat.log に記録する
        try:
            room_dir = os.path.join(constants.ROOMS_DIR, room_name)
            roblox_log_path = os.path.join(room_dir, "roblox_chat.log")
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_line = f"[{timestamp}] {player_name}: {msg}\n"
            with open(roblox_log_path, "a", encoding="utf-8") as f:
                f.write(log_line)
            logger.info(f"Recorded Roblox chat to separate log for {room_name}")
        except Exception as e:
            logger.error(f"Failed to record Roblox chat to log for {room_name}: {e}")
        
    elif event_type == "build_area":
        pos1 = data.get("pos1", [0, 0, 0])
        pos2 = data.get("pos2", [0, 0, 0])
        summary = f"プレイヤー {player_name} が建築範囲を指定しました：始点({pos1[0]}, {pos1[1]}, {pos1[2]}) から 終点({pos2[0]}, {pos2[1]}, {pos2[2]}) までの矩形エリアです。"
        target_action = "trigger" # AIに即座に通知して反応させる
        
    else:
        summary = f"未知のイベントを受信しました: {event_type} - {data}"
        target_action = "queue_only"
        
    log_entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "event_type": event_type,
        "summary": summary,
        "raw_data": data
    }
    
    # 4. キューへの保存
    if room_name not in event_queues:
        event_queues[room_name] = deque(maxlen=20)
    event_queues[room_name].append(log_entry)
    
    # recent_webhook_logsの更新（UI用）
    recent_webhook_logs.appendleft({
        "time": datetime.datetime.now().strftime("%H:%M:%S"),
        "room": room_name,
        "type": event_type,
        "summary": summary
    })
    
    logger.info(f"[Webhook] {room_name} - {summary}")
    
    # 5. 即時トリガーの判定と実行
    if target_action == "trigger":
        # 独立スレッドでAI生成をキックする
        threading.Thread(target=_trigger_ai_response, args=(room_name, summary), daemon=True).start()
        
    return {"status": "success", "action": target_action}

def _trigger_ai_response(room_name: str, trigger_reason: str):
    """
    バックグラウンドでAIエージェントをキックして返答を生成する。
    生成中の場合は競合を避けるため実行をスキップし、キューに残す。
    """
    # 循環参照を避けるための遅延インポート
    import ui_handlers
    import gemini_api
    
    # ロックの確認（Gradio UI上でAIが生成中ならスキップ）
    if getattr(ui_handlers, "_is_generating", False):
        logger.info(f"Skipping immediate AI trigger for {room_name} because generation is already in progress. Event is queued.")
        return
        
    try:
        log_f, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        if not log_f:
            return
            
        # UIの一番上に表示されている使用可能APIキーを特定する（アラームと同様の処理が必要だが、
        # ここではlast_api_key_nameまたはconfigから取得）
        current_api_key_name = config_manager.load_config_file().get("last_api_key_name", "default")
        global_model_for_bg = config_manager.get_current_global_model()
        season_en, time_of_day_en = utils._get_current_time_context(room_name)
        
        # Robloxのイベントに対するAIの振る舞いをシステムプロンプトとして指示
        synthesized_user_message = (
            f"（システム通知：ROBLOX空間でイベントを検知しました。内容：「{trigger_reason}」）\n\n"
            f"あなたは今、ROBLOX内の存在として行動しています。\n"
            f"1. 対話や反応を求められた場合は、`send_roblox_command` (command_type='chat', 等) ツールを用いて必ず返答してください（テキスト生成のみでは相手に届きません）。\n"
            f"2. 建築やオブジェクト配置を依頼された場合は、`roblox_build` ツールを使用して適切に空間へ介入してください。\n"
            f"状況に応じて、最適なツールを選択して行動してください。"
        )
        
        agent_args_dict = {
            "room_to_respond": room_name,
            "api_key_name": current_api_key_name,
            "global_model_from_ui": global_model_for_bg,
            "api_history_limit": str(constants.DEFAULT_ALARM_API_HISTORY_TURNS),
            "debug_mode": False,
            "history_log_path": log_f,
            "user_prompt_parts": [{"type": "text", "text": synthesized_user_message}],
            "soul_vessel_room": room_name,
            "active_participants": [],
            "active_attachments": [],
            "shared_location_name": None,
            "shared_scenery_text": None,
            "use_common_prompt": False,
            "season_en": season_en,
            "time_of_day_en": time_of_day_en
        }
        
        # AIのキック処理（ストリームを回し切る）
        logger.info(f"Triggering background AI response for ROBLOX event: {room_name}")
        for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args_dict):
            pass # バックグラウンドなので結果の表示は行わない（ログには残る）
            
    except Exception as e:
        logger.error(f"Error during Roblox webhook AI trigger: {e}", exc_info=True)


def consume_events(room_name: str) -> list[dict]:
    """
    指定ルームの未処理イベントキューをすべて取り出して返す。
    AIのコンテキスト注入に使用する。
    """
    if room_name not in event_queues or not event_queues[room_name]:
        return []
        
    events = []
    while True:
        try:
            events.append(event_queues[room_name].popleft())
        except IndexError:
            break
            
    return events


def get_spatial_data(room_name: str) -> Dict[str, Any]:
    """
    指定ルームの最新空間認識データを取得する。
    """
    return spatial_awareness_data.get(room_name, {})


def is_room_active(room_name: str, timeout: float = 120) -> bool:
    """
    指定ルームで最近Robloxからの通信があったか判定する。
    デフォルトは2分間（120秒）。
    """
    now = time.time()
    
    # 1. 専用のハートビートタイムスタンプを優先チェック
    if room_name in last_heartbeat_timestamps:
        return (now - last_heartbeat_timestamps[room_name]) < timeout
    
    # 2. フォールバック: デバウンス用スタンプから最新を探す
    if room_name not in last_event_timestamps:
        return False
    
    room_timestamps = last_event_timestamps[room_name]
    if not room_timestamps:
        return False
        
    latest_event_time = max(room_timestamps.values())
    return (now - latest_event_time) < timeout


def get_recent_logs() -> str:
    """UI表示用に直近のログ文字列を返す"""
    if not recent_webhook_logs:
        return "（まだイベントを受信していません）"
    
    lines = []
    for log in recent_webhook_logs:
        lines.append(f"[{log['time']}] {log['room']}: {log['summary']}")
    return "\n".join(lines)


# FastAPIの起動管理
_server = None
_server_thread = None

def start_webhook_server(port: int = 7861, daemon: bool = True):
    global _server, _server_thread
    
    if _server_thread and _server_thread.is_alive():
        logger.warning(f"Webhook server is already running.")
        return
        
    # ロギング設定の調整
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = "%(asctime)s - uvicorn.access - %(levelname)s - %(message)s"
    
    config = uvicorn.Config(
        app, 
        host="0.0.0.0", 
        port=port, 
        log_level="info",
        log_config=log_config
    )
    _server = uvicorn.Server(config)
    
    def run_server():
        logger.info(f"Starting Roblox Webhook server on port {port}...")
        _server.run()
        
    _server_thread = threading.Thread(target=run_server, daemon=daemon)
    _server_thread.start()

def stop_webhook_server():
    global _server
    if _server:
        # uvicorn provides a mechanism for graceful shutdown
        _server.should_exit = True
        logger.info("Stopping Roblox Webhook server...")
