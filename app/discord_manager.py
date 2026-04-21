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

        # 3. [Arousal] 会話開始時の内部状態スナップショット
        # エピソード記憶の重要度（Arousal）計算のため、会話前後の内部状態変化を記録
        internal_state_before = None
        try:
            from motivation_manager import MotivationManager
            mm = MotivationManager(room_name)
            internal_state_before = mm.get_state_snapshot()
        except Exception as e:
            logger.error(f"  - [Arousal] スナップショット取得失敗: {e}")

        # 4. AIエージェントの呼び出し
        # 必要な引数を構築
        effective_settings = config_manager.get_effective_settings(room_name)
        
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
            "global_model_from_ui": config_manager.CONFIG_GLOBAL.get("last_model"), # UIで最後に使われたモデルを優先
            "skip_tool_execution": False,
            "enable_supervisor": False
        }

        # タイピングインジケータを開始
        async with message.channel.typing():
            try:
                # 思考中メッセージの初期化
                full_response = ""
                final_text = ""
                generated_images = []

                # 非同期スレッドでジェネレータを回す
                def run_agent():
                    return list(gemini_api.invoke_nexus_agent_stream(agent_args))

                # ブロッキングなループをスレッドで実行
                loop = asyncio.get_event_loop()
                results = await loop.run_in_executor(None, run_agent)

                # 4. 結果を解析して最終回答とモデル名を抽出
                captured_model_name = None
                for mode, payload in results:
                    # AIの最終メッセージを取得
                    if mode == "values" and "messages" in payload:
                        last_msg = payload["messages"][-1]
                        if isinstance(last_msg, AIMessage):
                            full_response = last_msg.content
                    # 使用モデル名を取得
                    if mode == "values" and payload.get("model_name"):
                        captured_model_name = payload.get("model_name")

                # 5. 応答のパースと副作用処理 (ui_handlers.py と同等)
                if full_response:
                    content_str = full_response
                    
                    # AIが模倣したタイムスタンプを除去
                    content_str = utils.remove_ai_timestamp(content_str)
                    
                    # --- [Phase F] ペルソナ感情タグのパースと反映 ---
                    persona_emotion_pattern = r'<persona_emotion\s+category=["\'](\w+)["\']\s+intensity=["\']([0-9.]+)["\']\s*/>'
                    emotion_match = re.search(persona_emotion_pattern, content_str, re.IGNORECASE)
                    if emotion_match:
                        detected_category = emotion_match.group(1).lower()
                        detected_intensity = float(emotion_match.group(2))
                        valid_categories = ["joy", "contentment", "protective", "anxious", "sadness", "anger", "neutral"]
                        if detected_category in valid_categories:
                            try:
                                from motivation_manager import MotivationManager
                                mm = MotivationManager(room_name)
                                mm.set_persona_emotion(detected_category, detected_intensity)
                                mm._save_state()
                                logger.info(f"  - [Emotion] Discord経由でペルソナ感情を反映: {detected_category} ({detected_intensity})")
                            except Exception as e:
                                logger.error(f"  - [Emotion] 感情反映エラー: {e}")
                    
                    # --- [Phase H] 記憶共鳴タグのパースとArousal更新 ---
                    memory_trace_pattern = r'<memory_trace\s+id=["\']([^"\']+)["\']\s+resonance=["\']([0-9.]+)["\']\s*/>'
                    trace_matches = re.findall(memory_trace_pattern, content_str, re.IGNORECASE)
                    if trace_matches:
                        try:
                            from episodic_memory_manager import EpisodicMemoryManager
                            emm = EpisodicMemoryManager(room_name)
                            for episode_id, resonance_str in trace_matches:
                                resonance = float(resonance_str)
                                if 0.0 <= resonance <= 1.0:
                                    emm.update_arousal(episode_id, resonance)
                            logger.info(f"  - [MemoryTrace] Discord経由で {len(trace_matches)}件の記憶共鳴を処理")
                        except Exception as e:
                            logger.error(f"  - [MemoryTrace] 共鳴処理エラー: {e}")

                    # 使用モデル名の取得（ステップ4で抽出済みの captured_model_name を使用）
                    actual_model_name = captured_model_name or config_manager.CONFIG_GLOBAL.get("last_model") or "Gemini"
                    
                    # ログ用のタイムスタンプ付与 (Nexus Ark仕様)
                    timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)}"
                    content_to_log = content_str + timestamp
                    
                    # UI側のログファイル（Web UI用）に保存
                    try:
                        utils.save_message_to_log(log_file, f"## AGENT:{room_name}", content_to_log)
                    except Exception as e:
                        logger.error(f"Failed to save AI response to log: {e}")

                # 5. Discord送信用テキストのクリーンアップ
                # タグをすべて除去した綺麗な文章のみを送信
                final_text = utils.clean_persona_text(full_response)
                
                # 画像生成チェック
                img_matches = re.findall(r'\[(?:VIEW_IMAGE|GENERATED_IMAGE):\s*(.*?)\]', full_response)
                for img_path in img_matches:
                    img_path = img_path.strip()
                    if os.path.exists(img_path) and img_path not in generated_images:
                        generated_images.append(img_path)

                # 6. Discordへ返信
                if final_text:
                    if len(final_text) > 1900:
                        parts = [final_text[i:i+1900] for i in range(0, len(final_text), 1900)]
                        for p in parts:
                            await message.reply(p)
                    else:
                        files = [discord.File(path) for path in generated_images if os.path.exists(path)]
                        await message.reply(final_text, files=files if files else None)
                elif generated_images:
                    files = [discord.File(path) for path in generated_images if os.path.exists(path)]
                    await message.reply(files=files)
                else:
                    await message.reply("（AIからの応答が空でした）")

                # --- 後処理: 通常チャットと同等の内部状態更新 ---

                # [1] クールダウンリセット（退屈度リセット＆自律行動タイマー管理）
                try:
                    from motivation_manager import MotivationManager
                    MotivationManager(room_name).update_last_interaction()
                    print(f"--- [MotivationManager] {room_name}: 対話完了によりクールダウンをリセットしました ---")
                except Exception as e:
                    logger.error(f"クールダウンリセットエラー: {e}")

                # [2] Arousal計算（会話の感情的インパクトを数値化）
                try:
                    if internal_state_before:
                        from motivation_manager import MotivationManager
                        from arousal_calculator import calculate_arousal, get_arousal_level
                        
                        mm = MotivationManager(room_name)
                        internal_state_after = mm.get_state_snapshot()
                        
                        arousal_score = calculate_arousal(internal_state_before, internal_state_after)
                        arousal_level = get_arousal_level(arousal_score)
                        
                        print(f"  - [Arousal] Discord会話のArousalスコア: {arousal_score:.3f} ({arousal_level})")
                        
                        # 変化の詳細をログ出力
                        curiosity_change = internal_state_after.get("curiosity", 0) - internal_state_before.get("curiosity", 0)
                        relatedness_before = internal_state_before.get("relatedness", internal_state_before.get("devotion", 0))
                        relatedness_after = internal_state_after.get("relatedness", internal_state_after.get("devotion", 0))
                        relatedness_change = relatedness_after - relatedness_before
                        persona_emotion_before = internal_state_before.get("persona_emotion", "neutral")
                        persona_emotion_after = internal_state_after.get("persona_emotion", "neutral")
                        
                        if arousal_score > 0:
                            print(f"    - 好奇心変化: {curiosity_change:+.3f}, 関係性変化: {relatedness_change:+.3f}")
                            print(f"    - ペルソナ感情: {persona_emotion_before} → {persona_emotion_after}")
                        
                        # [3] SessionArousal 蓄積
                        if full_response:
                            import session_arousal_manager
                            ai_timestamp_str = datetime.datetime.now().strftime('%H:%M:%S')
                            session_arousal_manager.add_arousal_score(room_name, arousal_score, time_str=ai_timestamp_str)
                        else:
                            print(f"  - [Arousal] AI応答が空のため、蓄積をスキップします")
                except Exception as e:
                    logger.error(f"Arousal計算エラー: {e}")
                
            except Exception as e:
                logger.error(f"AI invocation error: {e}")
                traceback.print_exc()
                await message.reply(f"⚠️ AIの呼び出し中にエラーが発生しました: {e}")

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
