# discord_manager.py
import discord
import asyncio
import threading
import logging
import os
import re
import datetime
import httpx
import traceback
from typing import Optional, List
import config_manager
import gemini_api
from langchain_core.messages import AIMessage
import room_manager
import utils
import constants

logger = logging.getLogger(__name__)

# Discord Botのスレッドとクライアントを管理するグローバル変数
_bot_thread: Optional[threading.Thread] = None
_bot_client: Optional['NexusDiscordClient'] = None
_loop: Optional[asyncio.AbstractEventLoop] = None

class NexusDiscordClient(discord.Client):
    def __init__(self, *args, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, *args, **kwargs)
        self.message_lock = asyncio.Lock() # 連続メッセージによる競合防止

    async def on_ready(self):
        print(f"--- [Discord Bot] '{self.user}' としてログインしました ---")
        logger.info(f"Discord Bot logged in as {self.user}")

    async def on_message(self, message: discord.Message):
        # 自身のメッセージやBotのメッセージは無視
        if message.author.bot:
            return

        # 許可されたユーザーのみ反応
        auth_ids = config_manager.DISCORD_AUTHORIZED_USER_IDS
        # 文字列として比較
        if str(message.author.id) not in [str(aid) for aid in auth_ids]:
            return

        # コマンド処理: /room ルーム名
        if message.content.startswith("/room "):
            await self.handle_room_command(message)
            return

        # コマンド処理: /retry
        if message.content.strip() == "/retry":
            async with self.message_lock:
                await self.handle_retry_command(message)
            return

        # 非同期でチャット処理を実行
        async with self.message_lock:
            await self.handle_chat(message)

    async def handle_room_command(self, message: discord.Message):
        try:
            requested_room = message.content[6:].strip()
            # ルーム一覧を取得して検証
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
                config_manager.save_discord_bot_settings(linked_room=target_folder)
                await message.reply(f"✅ 対話対象をルーム「{display_name}」に切り替えました。")
                print(f"--- [Discord Bot] ルーム切り替え: {target_folder} ---")
            else:
                await message.reply(f"❌ ルーム「{requested_room}」が見つかりませんでした。")
        except Exception as e:
            logger.error(f"Room command error: {e}")
            await message.reply(f"⚠️ エラーが発生しました: {e}")

    async def handle_chat(self, message: discord.Message):
        # 現在のルームを取得
        room_name = config_manager.DISCORD_BOT_LINKED_ROOM or config_manager.initial_room_global
        if not room_name:
            await message.reply("⚠️ ルームが設定されていません。Web UIでルームを選択するか、`/room` コマンドを使用してください。")
            return

        print(f"--- [Discord Bot] メッセージ受信 (Room: {room_name}): {message.content[:50]}... ---")

        # 1. 画像アタッチメントの処理
        user_content = message.content
        attachments_paths = []
        if message.attachments:
            # ログディレクトリの images フォルダを確保
            room_log_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.LOGS_DIR_NAME)
            images_dir = os.path.join(room_log_dir, "images")
            os.makedirs(images_dir, exist_ok=True)

            for i, attachment in enumerate(message.attachments):
                if any(attachment.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]):
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_filename = f"discord_{timestamp}_{i}_{attachment.filename}"
                    file_path = os.path.join(images_dir, safe_filename)
                    
                    try:
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(attachment.url)
                            if resp.status_code == 200:
                                with open(file_path, "wb") as f:
                                    f.write(resp.content)
                                
                                # トークン節約のため画像をリサイズ（Web UIと同じ処理）
                                try:
                                    resized_img = utils.resize_image_for_api(file_path, max_size=512, return_image=True)
                                    if resized_img is not None:
                                        resized_img.save(file_path)
                                        resized_img.close()
                                except Exception as img_err:
                                    logger.warning(f"Image resize failed for {attachment.filename}, using original: {img_err}")
                                
                                attachments_paths.append(file_path)
                                # メッセージ内容に画像タグを挿入
                                user_content += f"\n[VIEW_IMAGE: {file_path}]"
                    except Exception as e:
                        logger.error(f"Attachment download error: {attachment.filename}, {e}")

        # 2. ログへの記録 (Nexus Ark通常チャット仕様に完全同期)
        log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        
        # タイムスタンプの書式を通常チャットと合わせる
        timestamp_str = datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')
        full_user_log_entry = f"{user_content}\n\n{timestamp_str} | Discord"
        
        try:
            # ヘッダーを固定の '## USER:user' とすることで、Web UI側で設定済みの名前が正しく反映される
            utils.save_message_to_log(log_file, "## USER:user", full_user_log_entry)
        except Exception as e:
            logger.error(f"Logging error: {e}")

        # 3. AI実行と返信
        await self._execute_ai_interaction(room_name, user_content, attachments_paths, message)

    async def handle_retry_command(self, message: discord.Message):
        """直前のAI応答を削除し、再生成を行うコマンド"""
        room_name = config_manager.DISCORD_BOT_LINKED_ROOM or config_manager.initial_room_global
        if not room_name:
            await message.reply("⚠️ ルームが設定されていません。")
            return

        log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        
        # ログをロードして最後のリプライを特定
        all_messages = utils.load_chat_log(log_file)
        if not all_messages:
            await message.reply("⚠️ ログが空のため、再生成できません。")
            return
            
        # 最後から逆順に、再生成の起点となる（AGENT/SYSTEM）メッセージを探す
        target_msg = None
        for msg in reversed(all_messages):
            if msg.get("role") in ("AGENT", "SYSTEM"):
                target_msg = msg
                break
        
        if not target_msg:
            await message.reply("⚠️ 再生成対象（AIの応答）が見つかりませんでした。")
            return

        # ログの巻き戻し
        restored_input, deleted_timestamp = utils.delete_and_get_previous_user_input(log_file, target_msg)
        
        if restored_input is None:
            await message.reply("⚠️ ログの巻き戻しに失敗しました。")
            return
            
        print(f"--- [Discord Bot] 再生成開始 (Room: {room_name}, Previous TS: {deleted_timestamp}) ---")
        
        # restored_input から画像パスなどのタグを抽出
        attachment_pattern = re.compile(r'\[(?:VIEW_IMAGE|GENERATED_IMAGE|ファイル添付):\s*(.*?)\]')
        found_attachments = attachment_pattern.findall(restored_input)
        
        # API送信用にはタグを除去したテキストを使用
        clean_user_content = attachment_pattern.sub('', restored_input).strip()
        
        # ユーザー発言を新しいタイムスタンプで再保存（Web UIの handle_rerun_button_click と同様）
        timestamp_str = datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')
        full_user_log_entry = f"{restored_input}\n\n{timestamp_str} | Discord (Retry)"
        utils.save_message_to_log(log_file, "## USER:user", full_user_log_entry)
        
        # AI実行
        await message.reply(f"🔄 直前の応答を破棄し、再生成を開始します... (Room: {room_name})")
        await self._execute_ai_interaction(room_name, clean_user_content, found_attachments, message)

    async def _execute_ai_interaction(self, room_name: str, user_content: str, attachments_paths: List[str], reply_target: discord.Message):
        """AIの呼び出し、ログ保存、Discordへの返信を一括で行う内部メソッド"""
        log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)

        # 1. [Arousal] 会話開始時の内部状態スナップショット
        internal_state_before = None
        try:
            from motivation_manager import MotivationManager
            mm = MotivationManager(room_name)
            internal_state_before = mm.get_state_snapshot()
        except Exception as e:
            logger.error(f"  - [Arousal] スナップショット取得失敗: {e}")

        # 2. AIエージェントの呼び出し
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

        # タイピングインジケータを開始
        async with reply_target.channel.typing():
            try:
                full_response = ""
                generated_images = []

                # 非同期スレッドでジェネレータを回す
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
                    
                    # ペルソナ感情タグの反映
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
                    
                    # 記憶共鳴タグの反映
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

                # Discord送信用テキストの構築
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

                if final_text:
                    if len(final_text) > 1900:
                        parts = [final_text[i:i+1900] for i in range(0, len(final_text), 1900)]
                        for p in parts:
                            await reply_target.reply(p)
                    else:
                        files = [discord.File(path) for path in generated_images if os.path.exists(path)]
                        await reply_target.reply(final_text, files=files if files else None)
                elif generated_images:
                    files = [discord.File(path) for path in generated_images if os.path.exists(path)]
                    await reply_target.reply(files=files)
                else:
                    await reply_target.reply("（AIからの応答が空でした）")

                # Arousal計算とクールダウンリセット
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
                await reply_target.reply(f"⚠️ AIの呼び出し中にエラーが発生しました: {e}")


def start_bot():
    global _bot_thread, _bot_client, _loop
    token = config_manager.DISCORD_BOT_TOKEN
    enabled = config_manager.DISCORD_BOT_ENABLED
    
    if not enabled or not token:
        print("--- [Discord Bot] 無効、またはトークンが設定されていないため起動しません ---")
        return

    if _bot_thread and _bot_thread.is_alive():
        print("--- [Discord Bot] 既に実行中です ---")
        return

    def run_event_loop():
        global _loop, _bot_client
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _bot_client = NexusDiscordClient()
        
        try:
            _loop.run_until_complete(_bot_client.start(token))
        except discord.LoginFailure:
            print("--- [Discord Bot] ログイン失敗: トークンが無効です ---")
        except Exception as e:
            print(f"--- [Discord Bot] エラー: {e} ---")
            traceback.print_exc()
        finally:
            _loop.close()

    _bot_thread = threading.Thread(target=run_event_loop, daemon=True)
    _bot_thread.start()
    print("--- [Discord Bot] 起動スレッドを開始しました ---")

def stop_bot():
    global _bot_client, _loop
    if _bot_client and _loop:
        future = asyncio.run_coroutine_threadsafe(_bot_client.close(), _loop)
        try:
            future.result(timeout=10)
        except:
            pass
        print("--- [Discord Bot] 停止しました ---")
