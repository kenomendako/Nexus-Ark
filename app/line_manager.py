import os
import re
import datetime
import logging
import traceback
import threading
from typing import Optional, List
import asyncio
import httpx

import uvicorn
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    ImageMessage,
    PushMessageRequest,
    MessagingApiBlob
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent
)

import config_manager
import room_manager
import utils
import constants
import gemini_api
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

app = FastAPI(title="Nexus Ark LINE Webhook Server", version="1.0.0")
handler = WebhookHandler("dummy_secret_for_initialization")
configuration = None
api_client = None
messaging_api = None

_server = None
_server_thread = None

# 競合防止用ロック
_message_lock = asyncio.Lock()

def init_line_bot():
    global configuration, api_client, messaging_api
    token = config_manager.LINE_CHANNEL_ACCESS_TOKEN
    secret = config_manager.LINE_CHANNEL_SECRET
    
    if not token or not secret:
        return False
        
    configuration = Configuration(access_token=token)
    api_client = ApiClient(configuration)
    messaging_api = MessagingApi(api_client)
    handler.parser.signature_validator.channel_secret = secret.encode('utf-8')
    return True

@app.post("/api/line/webhook")
async def line_webhook(request: Request, background_tasks: BackgroundTasks):
    if not init_line_bot():
        raise HTTPException(status_code=500, detail="LINE Bot is not configured properly.")

    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body_text = body.decode('utf-8')

    try:
        handler.handle(body_text, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    auth_ids = config_manager.LINE_AUTHORIZED_USER_IDS
    if user_id not in auth_ids:
        logger.warning(f"Unauthorized LINE user: {user_id}")
        return

    text = event.message.text
    reply_token = event.reply_token

    # コマンド処理
    if text.startswith("/room "):
        asyncio.run_coroutine_threadsafe(handle_room_command(text, reply_token), asyncio.get_event_loop())
        return

    if text.strip() == "/retry":
        asyncio.run_coroutine_threadsafe(handle_retry_command(reply_token, user_id), asyncio.get_event_loop())
        return

    # 通常チャット処理
    asyncio.run_coroutine_threadsafe(handle_chat(text, [], reply_token, user_id), asyncio.get_event_loop())

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id
    auth_ids = config_manager.LINE_AUTHORIZED_USER_IDS
    if user_id not in auth_ids:
        logger.warning(f"Unauthorized LINE user: {user_id}")
        return

    message_id = event.message.id
    reply_token = event.reply_token
    
    asyncio.run_coroutine_threadsafe(handle_image_chat(message_id, reply_token, user_id), asyncio.get_event_loop())

async def handle_room_command(text: str, reply_token: str):
    try:
        requested_room = text[6:].strip()
        rooms = room_manager.get_room_list_for_ui()
        folder_names = [r[1] for r in rooms]
        display_names = [r[0] for r in rooms]
        
        target_folder = None
        if requested_room in folder_names:
            target_folder = requested_room
            display_name = next(r[0] for r in rooms if r[1] == requested_room)
        elif requested_room in display_names:
            target_folder = next(r[1] for r in rooms if r[0] == requested_room)
            display_name = requested_room
        
        if target_folder:
            config_manager.save_line_bot_settings(linked_room=target_folder)
            reply_text(reply_token, f"✅ 対話対象をルーム「{display_name}」に切り替えました。")
            print(f"--- [LINE Bot] ルーム切り替え: {target_folder} ---")
        else:
            reply_text(reply_token, f"❌ ルーム「{requested_room}」が見つかりませんでした。")
    except Exception as e:
        logger.error(f"Room command error: {e}")
        reply_text(reply_token, f"⚠️ エラーが発生しました: {e}")

async def handle_retry_command(reply_token: str, user_id: str):
    async with _message_lock:
        room_name = config_manager.LINE_BOT_LINKED_ROOM or config_manager.initial_room_global
        if not room_name:
            reply_text(reply_token, "⚠️ ルームが設定されていません。")
            return

        log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        all_messages = utils.load_chat_log(log_file)
        if not all_messages:
            reply_text(reply_token, "⚠️ ログが空のため、再生成できません。")
            return
            
        target_msg = None
        for msg in reversed(all_messages):
            if msg.get("role") in ("AGENT", "SYSTEM"):
                target_msg = msg
                break
        
        if not target_msg:
            reply_text(reply_token, "⚠️ 再生成対象（AIの応答）が見つかりませんでした。")
            return

        restored_input, deleted_timestamp = utils.delete_and_get_previous_user_input(log_file, target_msg)
        
        if restored_input is None:
            reply_text(reply_token, "⚠️ ログの巻き戻しに失敗しました。")
            return
            
        print(f"--- [LINE Bot] 再生成開始 (Room: {room_name}, Previous TS: {deleted_timestamp}) ---")
        
        attachment_pattern = re.compile(r'\[(?:VIEW_IMAGE|GENERATED_IMAGE|ファイル添付):\s*(.*?)\]')
        found_attachments = attachment_pattern.findall(restored_input)
        clean_user_content = attachment_pattern.sub('', restored_input).strip()
        
        timestamp_str = datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')
        full_user_log_entry = f"{restored_input}\n\n{timestamp_str} | LINE (Retry)"
        utils.save_message_to_log(log_file, "## USER:user", full_user_log_entry)
        
        reply_text(reply_token, f"🔄 直前の応答を破棄し、再生成を開始します... (Room: {room_name})")
        
        await _execute_ai_interaction(room_name, clean_user_content, found_attachments, reply_token=None, user_id=user_id)


async def handle_image_chat(message_id: str, reply_token: str, user_id: str):
    room_name = config_manager.LINE_BOT_LINKED_ROOM or config_manager.initial_room_global
    if not room_name:
        reply_text(reply_token, "⚠️ ルームが設定されていません。")
        return

    messaging_api_blob = MessagingApiBlob(api_client)
    image_data = messaging_api_blob.get_message_content(message_id)
    
    room_log_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.LOGS_DIR_NAME)
    images_dir = os.path.join(room_log_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_filename = f"line_{timestamp}_{message_id}.jpg"
    file_path = os.path.join(images_dir, safe_filename)
    
    with open(file_path, "wb") as f:
        f.write(image_data)
        
    try:
        resized_img = utils.resize_image_for_api(file_path, max_size=512, return_image=True)
        if resized_img is not None:
            resized_img.save(file_path)
            resized_img.close()
    except Exception as img_err:
        logger.warning(f"Image resize failed for LINE image: {img_err}")

    user_content = f"[VIEW_IMAGE: {file_path}]"
    
    async with _message_lock:
        await handle_chat(user_content, [file_path], reply_token, user_id, is_image_only=True)


async def handle_chat(user_content: str, attachments_paths: List[str], reply_token: str, user_id: str, is_image_only: bool = False):
    room_name = config_manager.LINE_BOT_LINKED_ROOM or config_manager.initial_room_global
    if not room_name:
        reply_text(reply_token, "⚠️ ルームが設定されていません。")
        return

    print(f"--- [LINE Bot] メッセージ受信 (Room: {room_name}): {user_content[:50]}... ---")

    log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    timestamp_str = datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')
    
    full_user_log_entry = f"{user_content}\n\n{timestamp_str} | LINE"
    
    try:
        utils.save_message_to_log(log_file, "## USER:user", full_user_log_entry)
    except Exception as e:
        logger.error(f"Logging error: {e}")

    await _execute_ai_interaction(room_name, user_content, attachments_paths, reply_token, user_id)


async def _execute_ai_interaction(room_name: str, user_content: str, attachments_paths: List[str], reply_token: str, user_id: str):
    log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)

    internal_state_before = None
    try:
        from motivation_manager import MotivationManager
        mm = MotivationManager(room_name)
        internal_state_before = mm.get_state_snapshot()
    except Exception as e:
        logger.error(f"  - [Arousal] スナップショット取得失敗: {e}")

    effective_settings = config_manager.get_effective_settings(room_name)
    display_thoughts = effective_settings.get("display_thoughts", True)
    
    agent_args = {
        "room_to_respond": room_name,
        "api_key_name": effective_settings.get("api_key_name") or config_manager.initial_api_key_name_global,
        "api_history_limit": effective_settings.get("api_history_limit_option", constants.DEFAULT_API_HISTORY_LIMIT_OPTION),
        "debug_mode": False,
        "history_log_path": log_file,
        "user_prompt_parts": [user_content],
        "soul_vessel_room": room_name,
        "active_participants": [],
        "active_attachments": attachments_paths,
        "shared_location_name": None,
        "shared_scenery_text": None,
        "season_en": None,
        "time_of_day_en": None,
        "global_model_from_ui": config_manager.CONFIG_GLOBAL.get("last_model"),
        "skip_tool_execution": False,
        "enable_supervisor": False
    }

    try:
        full_response = ""
        generated_images = []

        def run_agent():
            return list(gemini_api.invoke_nexus_agent_stream(agent_args))

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, run_agent)

        captured_model_name = None
        for mode, payload in results:
            if mode == "values" and "messages" in payload:
                last_msg = payload["messages"][-1]
                if isinstance(last_msg, AIMessage):
                    full_response = last_msg.content
            if mode == "values" and payload.get("model_name"):
                captured_model_name = payload.get("model_name")

        if full_response:
            content_str = full_response
            content_str = utils.remove_ai_timestamp(content_str)
            
            persona_emotion_pattern = r'<persona_emotion\s+category=["\'](\w+)["\']\s+intensity=["\']([0-9.]+)["\']\s*/>'
            emotion_match = re.search(persona_emotion_pattern, content_str, re.IGNORECASE)
            if emotion_match:
                try:
                    detected_category = emotion_match.group(1).lower()
                    detected_intensity = float(emotion_match.group(2))
                    from motivation_manager import MotivationManager
                    mm = MotivationManager(room_name)
                    mm.set_persona_emotion(detected_category, detected_intensity)
                    mm._save_state()
                except Exception as e:
                    logger.error(f"  - [Emotion] 感情反映エラー: {e}")
            
            memory_trace_pattern = r'<memory_trace\s+id=["\']([^"\']+)["\']\s+resonance=["\']([0-9.]+)["\']\s*/>'
            trace_matches = re.findall(memory_trace_pattern, content_str, re.IGNORECASE)
            if trace_matches:
                try:
                    from episodic_memory_manager import EpisodicMemoryManager
                    emm = EpisodicMemoryManager(room_name)
                    for episode_id, resonance_str in trace_matches:
                        emm.update_arousal(episode_id, float(resonance_str))
                except Exception as e:
                    logger.error(f"  - [MemoryTrace] 共鳴処理エラー: {e}")

            actual_model_name = captured_model_name or config_manager.CONFIG_GLOBAL.get("last_model") or "Gemini"
            timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)}"
            content_to_log = content_str + timestamp
            
            try:
                utils.save_message_to_log(log_file, f"## AGENT:{room_name}", content_to_log)
            except Exception as e:
                logger.error(f"Failed to save AI response to log: {e}")

        final_text = utils.clean_persona_text(full_response)
        if display_thoughts:
            thoughts = utils.extract_thoughts_from_text(full_response)
            if thoughts:
                quoted_thoughts = "\n".join([f"> {line}" for line in thoughts.split("\n") if line.strip()])
                if quoted_thoughts:
                    final_text = f"{quoted_thoughts}\n\n{final_text}"
        
        img_matches = re.findall(r'\[(?:VIEW_IMAGE|GENERATED_IMAGE):\s*(.*?)\]', full_response)
        for img_path in img_matches:
            if os.path.exists(img_path.strip()):
                generated_images.append(img_path.strip())

        messages_to_send = []
        if final_text:
            if len(final_text) > 4900:
                parts = [final_text[i:i+4900] for i in range(0, len(final_text), 4900)]
                for p in parts:
                    messages_to_send.append(TextMessage(text=p))
            else:
                messages_to_send.append(TextMessage(text=final_text))
                
        if generated_images:
            messages_to_send.append(TextMessage(text="（画像が生成されましたが、LINE上の表示にはパブリックURLが必要です。Web UIで確認してください。）"))

        if not messages_to_send:
            messages_to_send.append(TextMessage(text="（AIからの応答が空でした）"))

        if reply_token:
            messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=messages_to_send[:5]
                )
            )
        elif user_id:
            messaging_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=messages_to_send[:5]
                )
            )

        try:
            from motivation_manager import MotivationManager
            mm = MotivationManager(room_name)
            mm.update_last_interaction()
            
            if internal_state_before:
                from arousal_calculator import calculate_arousal, get_arousal_level
                internal_state_after = mm.get_state_snapshot()
                arousal_score = calculate_arousal(internal_state_before, internal_state_after)
                if full_response:
                    import session_arousal_manager
                    session_arousal_manager.add_arousal_score(room_name, arousal_score, time_str=datetime.datetime.now().strftime('%H:%M:%S'))
        except Exception as e:
            logger.error(f"Post-interaction processing error: {e}")
        
    except Exception as e:
        logger.error(f"AI invocation error: {e}")
        error_msg = f"⚠️ AIの呼び出し中にエラーが発生しました: {e}"
        if reply_token:
            reply_text(reply_token, error_msg)
        elif user_id:
            push_text(user_id, error_msg)

def reply_text(reply_token: str, text: str):
    if not messaging_api: return
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )
    except Exception as e:
        logger.error(f"LINE reply error: {e}")

def push_text(user_id: str, text: str):
    if not messaging_api: return
    try:
        messaging_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=text)]
            )
        )
    except Exception as e:
        logger.error(f"LINE push error: {e}")

def start_bot(port: int = 7862, daemon: bool = True):
    global _server, _server_thread
    enabled = config_manager.LINE_BOT_ENABLED
    
    if not enabled:
        print("--- [LINE Bot] 無効のため起動しません ---")
        return

    if not init_line_bot():
        print("--- [LINE Bot] トークンまたはシークレットが設定されていないため起動しません ---")
        return

    if _server_thread and _server_thread.is_alive():
        print("--- [LINE Bot] 既に実行中です ---")
        return

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
        print(f"--- [LINE Bot] Webhookサーバーをポート {port} で開始しました ---")
        _server.run()
        
    _server_thread = threading.Thread(target=run_server, daemon=daemon)
    _server_thread.start()

def stop_bot():
    global _server
    if _server:
        _server.should_exit = True
        print("--- [LINE Bot] 停止しました ---")
