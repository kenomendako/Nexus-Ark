# timers.py (デスクトップ通知対応版: 永続化対応)

import time
import threading
import traceback
import gemini_api
import alarm_manager
import utils
import constants
import room_manager
import config_manager
# import ui_handlers 
import datetime

# --- plyerのインポートと存在チェック ---
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
        PLYER_AVAILABLE = False
# --- ここまで ---

ACTIVE_TIMERS = []

class UnifiedTimer:
    def __init__(self, timer_type, room_name, api_key_name, **kwargs):
        self.timer_type = timer_type
        self.room_name = room_name
        self.api_key_name = api_key_name
        self.kwargs = kwargs # 保存用に保持

        if self.timer_type == "通常タイマー":
            self.duration = kwargs.get('duration_minutes', 10) * 60
            self.theme = kwargs.get('normal_timer_theme', '時間になりました')
        elif self.timer_type == "ポモドーロタイマー":
            self.work_duration = kwargs.get('work_minutes', 25) * 60
            self.break_duration = kwargs.get('break_minutes', 5) * 60 
            self.cycles = kwargs.get('cycles', 4)
            self.work_theme = kwargs.get('work_theme', '作業終了の時間です')
            self.break_theme = kwargs.get('break_theme', '休憩終了の時間です')

        self._stop_event = threading.Event()
        self.thread = None
        self.start_time = kwargs.get('start_time') # 復元時はここに入る
    
    def to_dict(self):
        """永続化のための辞書表現を返す"""
        return {
            "timer_type": self.timer_type,
            "room_name": self.room_name,
            "api_key_name": self.api_key_name,
            "start_time": self.start_time,
            "kwargs": self.kwargs
        }

    @classmethod
    def from_dict(cls, data):
        """辞書表現からインスタンスを復元する"""
        kwargs = data.get("kwargs", {})
        # start_time を kwargs にマージして __init__ に渡す
        kwargs['start_time'] = data.get("start_time")
        
        return cls(
            timer_type=data.get("timer_type"),
            room_name=data.get("room_name"),
            api_key_name=data.get("api_key_name"),
            **kwargs
        )

    def start(self, restore=False):
        """
        タイマーを開始する。
        restore=True の場合、既存の start_time を使用して途中から再開する。
        """
        if self.timer_type == "通常タイマー":
            self.thread = threading.Thread(target=self._run_single_timer_wrapper, args=(restore,))
        elif self.timer_type == "ポモドーロタイマー":
            self.thread = threading.Thread(target=self._run_pomodoro_wrapper, args=(restore,))

        if self.thread:
            if not restore or self.start_time is None:
                self.start_time = time.time()
            
            self.thread.daemon = True
            self.thread.start()
            
            if self not in ACTIVE_TIMERS:
                ACTIVE_TIMERS.append(self)
            save_active_timers() # 状態保存

    def _run_single_timer_wrapper(self, restore):
        """通常タイマーのラッパー（復元ロジック対応）"""
        duration = self.duration
        
        if restore and self.start_time:
            elapsed = time.time() - self.start_time
            if elapsed >= duration:
                print(f"--- [タイマー復元] 期限切れを検知: {self.theme} ---")
                # 即時終了ログなどを残す
                self._handle_offline_expiration(self.theme)
                self._cleanup()
                return
            else:
                remaining = duration - elapsed
                print(f"--- [タイマー復元] 残り {remaining:.1f}秒 で再開: {self.theme} ---")
                self._run_single_timer(remaining, self.theme, "通常タイマー(復元)")
        else:
            self._run_single_timer(duration, self.theme, "通常タイマー")
        
        self._cleanup()

    def _run_pomodoro_wrapper(self, restore):
        """ポモドーロタイマーのラッパー（復元ロジック対応）"""
        if restore and self.start_time:
            elapsed = time.time() - self.start_time
            total_cycle_duration = self.work_duration + self.break_duration
            total_duration = total_cycle_duration * self.cycles
            
            # 最後の休憩は無いため、正確な総時間は調整が必要だが、簡易的に計算
            # 正確には: (work + break) * (cycles - 1) + work
            actual_total_duration = (self.work_duration + self.break_duration) * (self.cycles - 1) + self.work_duration
            
            if elapsed >= actual_total_duration:
                print(f"--- [ポモドーロ復元] 全サイクル終了済みを検知 ---")
                self._handle_offline_expiration("ポモドーロ終了")
                self._cleanup()
                return
            
            # どのサイクル、どのフェーズにいるか計算
            current_cycle_idx = int(elapsed // total_cycle_duration)
            time_in_cycle = elapsed % total_cycle_duration
            
            print(f"--- [ポモドーロ復元] サイクル {current_cycle_idx+1}/{self.cycles} 途中から再開 ---")
            
            # 途中から実行するためのカスタムロジック
            self._run_pomodoro_from_state(current_cycle_idx, time_in_cycle)
        else:
            self._run_pomodoro()
        
        self._cleanup()

    def _cleanup(self):
        """終了時の処理"""
        if self in ACTIVE_TIMERS:
            ACTIVE_TIMERS.remove(self)
        save_active_timers() # 完了を保存（リストから消える）

    def _handle_offline_expiration(self, theme):
        """オフライン中に期限が切れていた場合の処理"""
        try:
            log_f, _, _, _, _, _, _ = room_manager.get_room_files_paths(self.room_name)
            if log_f:
                timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')}"
                msg = f"（システム通知：オフライン中にタイマー「{theme}」の期限が経過しました。）"
                utils.save_message_to_log(log_f, "## SYSTEM:timer_expired_offline", msg + timestamp)
                
                # 通知も送る
                alarm_manager.send_notification(self.room_name, f"タイマー「{theme}」はオフライン中に終了しました。", {})
        except Exception as e:
            print(f"Expiration handle error: {e}")

    def get_remaining_time(self) -> float:
        """タイマーの残り時間を秒単位で返す。"""
        if self.start_time is None:
            return 0.0
        
        elapsed_time = time.time() - self.start_time
        
        # 現在のフェーズの総時間から経過時間を引く
        if self.timer_type == "通常タイマー":
            current_duration = self.duration
            remaining = current_duration - elapsed_time
            return max(0, remaining)
        else:
            # ポモドーロの場合
            total_cycle_duration = self.work_duration + self.break_duration
            # 全体終了チェック
            actual_total_duration = (self.work_duration + self.break_duration) * (self.cycles - 1) + self.work_duration
            if elapsed_time >= actual_total_duration:
                return 0.0

            time_in_cycle = elapsed_time % total_cycle_duration
            
            if time_in_cycle < self.work_duration:
                # 作業フェーズ中
                current_duration = self.work_duration
                elapsed_in_phase = time_in_cycle
            else:
                # 休憩フェーズ中
                current_duration = self.break_duration
                elapsed_in_phase = time_in_cycle - self.work_duration
                
            return max(0, current_duration - elapsed_in_phase)

    def _run_single_timer(self, duration: float, theme: str, timer_id: str):
        try:
            from langchain_core.messages import AIMessage, ToolMessage 
            import re 

            print(f"--- [タイマー開始: {timer_id}] Duration: {duration:.1f}s, Theme: '{theme}' ---")
            self._stop_event.wait(duration)

            if self._stop_event.is_set():
                print(f"--- [タイマー停止: {timer_id}] ユーザーにより停止されました ---")
                return

            print(f"--- [タイマー終了: {timer_id}] AIに応答生成を依頼します ---")

            message_for_log = "" 

            # プロンプト構築
            if theme.startswith("【自律行動】"):
                # 自律行動モード：計画を実行させる強力な指示
                plan_content = theme.replace("【自律行動】", "").strip()
                synthesized_user_message = (
                    f"（システム通知：行動計画の実行時刻になりました。）\n"
                    f"【予定されていた行動】\n{plan_content}\n\n"
                    f"**直ちに上記の計画を実行に移してください。**\n"
                    f"「〜します」という予告は不要です。対応するツール（Web検索や画像生成など）を即座に呼び出してください。"
                    f"もし、この行動だけで目的が達成されない場合は、ツールの実行結果を確認した後、**`schedule_next_action` を使用して次のステップを予約**してください。"
                )
                log_header = "## SYSTEM:autonomous_action"

                message_for_log = f"（自律行動開始：{plan_content}）"

            else:
                # 通常タイマーモード：ユーザーへの通知指示
                synthesized_user_message = (
                    f"（システムタイマー：時間です。テーマ「{theme}」について、"
                    f"**タイマーが完了したことをユーザーに通知してください。新しいタイマーやアラームを設定してはいけません。**）"
                )
                log_header = "## SYSTEM:timer"

                message_for_log = f"（システムタイマー：{theme}）"

            log_f, _, _, _, _, _, _ = room_manager.get_room_files_paths(self.room_name)
            current_api_key_name = config_manager.get_latest_api_key_name_from_config()
            if not current_api_key_name or not log_f:
                print(f"警告: APIキーまたはログファイルが見つかりません。")
                return


            # --- [Lazy Scenery] ---
            season_en, time_of_day_en = utils._get_current_time_context(self.room_name)
            location_name = None
            scenery_text = None
            global_model_for_bg = config_manager.get_current_global_model()

            agent_args_dict = {
                "room_to_respond": self.room_name,
                "api_key_name": current_api_key_name,
                "global_model_from_ui": global_model_for_bg,
                "api_history_limit": str(constants.DEFAULT_ALARM_API_HISTORY_TURNS),
                "debug_mode": False,
                "history_log_path": log_f,
                "user_prompt_parts": [{"type": "text", "text": synthesized_user_message}],
                "soul_vessel_room": self.room_name,
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
            
            for attempt in range(max_retries):
                try:
                    final_state = None
                    initial_message_count = 0
                    for mode, chunk in gemini_api.invoke_nexus_agent_stream(agent_args_dict):
                        if mode == "initial_count":
                            initial_message_count = chunk
                        elif mode == "values":
                            final_state = chunk
                    
                    if final_state:
                        new_messages = final_state["messages"][initial_message_count:]

                        for msg in new_messages:
                            if isinstance(msg, ToolMessage):
                                # 【アナウンスのみ保存するツール】constants.pyで一元管理
                                if msg.name in constants.TOOLS_SAVE_ANNOUNCEMENT_ONLY:
                                    formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                                    # 生の結果（[RAW_RESULT]）は含めない。アナウンスのみ。
                                    tool_log_content = formatted_tool_result if formatted_tool_result else f"🛠️ ツール「{msg.name}」を実行しました。"
                                    print(f"--- [ログ最適化] '{msg.name}' のアナウンスのみ保存（生の結果は除外） ---")
                                else:
                                    # UI表示用に見やすく整形
                                    formatted_tool_result = utils.format_tool_result_for_ui(msg.name, str(msg.content))
                                    # ログ形式に合わせて整形
                                    tool_log_content = f"{formatted_tool_result}\n\n[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]" if formatted_tool_result else f"[RAW_RESULT]\n{msg.content}\n[/RAW_RESULT]"
                                # ログに保存
                                utils.save_message_to_log(log_f, "## SYSTEM:tool_result", tool_log_content)

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
                    break 

                except gemini_api.ResourceExhausted as e:
                    error_str = str(e)
                    if "PerDay" in error_str or "Daily" in error_str:
                        print(f"  - 致命的エラー: 回復不能なAPI上限（日間など）に達しました。リトライしません。")
                        final_response_text = ""; break
                    
                    wait_time = base_delay * (2 ** attempt)
                    match = re.search(r"retry_delay {\s*seconds: (\d+)\s*}", error_str)
                    if match: wait_time = int(match.group(1)) + 1
                    
                    if attempt < max_retries - 1:
                        print(f"  - APIレート制限: {wait_time}秒待機して再試行します... ({attempt + 1}/{max_retries})")
                        time.sleep(wait_time)
                    else:
                        print(f"  - APIレート制限: 最大リトライ回数に達しました。"); final_response_text = ""; break
                except Exception as e:
                    print(f"--- タイマーのAI応答生成中に予期せぬエラーが発生しました ---"); traceback.print_exc()
                    final_response_text = ""; break
            
            # ログ保存（システムメッセージとAI応答）
            raw_response = final_response_text
            # 【変更】remove_thoughts_from_text ではなく clean_persona_text を使用
            response_text = utils.clean_persona_text(raw_response)

            if response_text and not response_text.startswith("[エラー"):
                # ヘッダー（自律行動 or タイマー）でシステムログを記録
                utils.save_message_to_log(log_f, log_header, message_for_log)
                
                # 【修正】AIが既にタイムスタンプを生成している場合は除去
                raw_response = utils.remove_ai_timestamp(raw_response)
                
                # システムの正しいタイムスタンプを追加
                timestamp = f"\n\n{dt_timers.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)}"
                content_to_log = raw_response + timestamp
                
                utils.save_message_to_log(log_f, f"## AGENT:{self.room_name}", content_to_log)
            else:
                # エラー時
                fallback_text = f"設定された行動（{theme}）を実行しようとしましたが、応答を生成できませんでした。"
                utils.save_message_to_log(log_f, "## SYSTEM:timer_fallback", fallback_text)
                response_text = fallback_text

            # 1. 正しい設定を取得 (room_config ではなく effective_settings を使う)
            effective_settings = config_manager.get_effective_settings(self.room_name)
            auto_settings = effective_settings.get("autonomous_settings", {})
            
            # 2. 時間設定を取得
            quiet_start = auto_settings.get("quiet_hours_start", "00:00")
            quiet_end = auto_settings.get("quiet_hours_end", "07:00")
            
            # 3. 判定
            is_quiet = utils.is_in_quiet_hours(quiet_start, quiet_end)
            
            # 4. 通知送信 (静かな時間でなければ)
            if not is_quiet:
                alarm_manager.send_notification(self.room_name, response_text, {})
                if PLYER_AVAILABLE:
                    try:
                        # タイトルを「アクション」に統一
                        notification.notify(title=f"{self.room_name} アクション", message=response_text[:100], app_name="Nexus Ark", timeout=10)
                    except: pass
            else:
                print(f"  - [Timer] 通知禁止時間帯のため、完了通知はスキップされました。")
                
        except Exception as e:
            print(f"!! [タイマー実行エラー] {timer_id}: {e} !!"); traceback.print_exc()
                                    
    def _run_pomodoro(self):
        """ポモドーロタイマーの通常実行（初回）"""
        try:
            for i in range(self.cycles):
                if self._stop_event.is_set(): break

                print(f"--- [ポモドーロ開始: 作業 {i+1}/{self.cycles}] ---")
                self._run_single_timer(self.work_duration, self.break_theme, f"ポモドーロ作業 {i+1}/{self.cycles}")
                if self._stop_event.is_set(): break

                # 最後のサイクルの後の休憩は実行しない
                if i < self.cycles - 1:
                    print(f"--- [ポモドーロ開始: 休憩 {i+1}/{self.cycles}] ---")
                    self._run_single_timer(self.break_duration, self.work_theme, f"ポモドーロ休憩 {i+1}/{self.cycles}")
            
            print("--- [ポモドーロタイマー] 全サイクル完了 ---")
        finally:
            self._cleanup()

    def _run_pomodoro_from_state(self, start_cycle_idx, elapsed_in_cycle):
        """途中状態からのポモドーロ再開ロジック"""
        try:
            for i in range(start_cycle_idx, self.cycles):
                if self._stop_event.is_set(): break

                # 作業フェーズかどうか
                if elapsed_in_cycle < self.work_duration:
                    # 作業フェーズの途中から
                    remaining_work = self.work_duration - elapsed_in_cycle
                    print(f"--- [ポモドーロ再開: 作業 {i+1}/{self.cycles}] 残り {remaining_work:.1f}秒 ---")
                    self._run_single_timer(remaining_work, self.break_theme, f"ポモドーロ作業(復元) {i+1}/{self.cycles}")
                    
                    if self._stop_event.is_set(): break
                    
                    # 休憩フェーズへ移行（このサイクルが終わっていなければ）
                    if i < self.cycles - 1:
                        print(f"--- [ポモドーロ開始: 休憩 {i+1}/{self.cycles}] ---")
                        self._run_single_timer(self.break_duration, self.work_theme, f"ポモドーロ休憩 {i+1}/{self.cycles}")
                else:
                    # 休憩フェーズの途中から
                    if i < self.cycles - 1: # 最終サイクルの後は休憩なし
                        elapsed_in_break = elapsed_in_cycle - self.work_duration
                        if elapsed_in_break < self.break_duration:
                            remaining_break = self.break_duration - elapsed_in_break
                            print(f"--- [ポモドーロ再開: 休憩 {i+1}/{self.cycles}] 残り {remaining_break:.1f}秒 ---")
                            self._run_single_timer(remaining_break, self.work_theme, f"ポモドーロ休憩(復元) {i+1}/{self.cycles}")
                
                # 次のサイクルのために経過時間をリセット（2周目以降は常に0から）
                elapsed_in_cycle = 0

            print("--- [ポモドーロタイマー] 復元実行完了 ---")
        except Exception as e:
            print(f"Pomodoro restore error: {e}")
            traceback.print_exc()

    def stop(self):
        self._stop_event.set()
        # _cleanup() はスレッド終了時に呼ばれるのでここでは明示的に呼ばないが...
        # 即時反応のためにリストから外して保存をしておく
        if self in ACTIVE_TIMERS:
            ACTIVE_TIMERS.remove(self)
        save_active_timers()

def save_active_timers():
    """現在のアクティブなタイマーをリスト形式に変換して保存"""
    timers_data = [t.to_dict() for t in ACTIVE_TIMERS]
    alarm_manager.save_timers(timers_data)

def load_active_timers():
    """保存されたタイマーを復元して再開"""
    global ACTIVE_TIMERS
    timers_data = alarm_manager.load_timers()
    
    restored_count = 0
    for t_data in timers_data:
        try:
            timer = UnifiedTimer.from_dict(t_data)
            # 復元起動 (restore=True)
            timer.start(restore=True)
            restored_count += 1
        except Exception as e:
            print(f"Failed to restore timer: {e}")
    
    if restored_count > 0:
        print(f"--- [Timers] {restored_count}個のタイマーを復元しました ---")
