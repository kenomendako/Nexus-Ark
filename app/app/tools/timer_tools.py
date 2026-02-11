# tools/timer_tools.py

import threading
from langchain_core.tools import tool
from timers import UnifiedTimer, ACTIVE_TIMERS
import config_manager # api_key_nameなどを取得するためにインポート
from typing import Optional
from datetime import datetime, timedelta

@tool
def set_timer(duration_minutes: int, theme: str, room_name: str) -> str:
    """
    指定された分数後に、特定のテーマで通知するシンプルなタイマーを設定します。
    ただし、同じルームで同じテーマのタイマーが既に実行中の場合は、新しいタイマーを設定せずにその旨を報告します。
    duration_minutes: タイマーの分数。
    theme: タイマー終了時にAIが話す内容のテーマ。
    """
    if not room_name:
        return "【Error】Internal tool error: room_name is required for execution."
    try:
        # --- [門番ロジック] 既に同じタイマーが存在しないかチェック ---
        for timer in ACTIVE_TIMERS:
            if (timer.room_name == room_name and 
                timer.timer_type == "通常タイマー" and 
                hasattr(timer, 'theme') and timer.theme == theme):
                
                remaining_seconds = timer.get_remaining_time()
                return (f"Success (Ignored): 同じテーマ「{theme}」のタイマーが既に実行中です。"
                        f"残り時間は約{remaining_seconds:.0f}秒です。新しいタイマーは設定しません。"
                        "ユーザーにはこの状況を伝えてください。")

        # 現在のAPIキー名などをconfig_managerから取得
        api_key_name = config_manager.initial_api_key_name_global

        timer = UnifiedTimer(
            timer_type="通常タイマー",
            duration_minutes=float(duration_minutes),
            room_name=room_name,
            api_key_name=api_key_name,
            normal_timer_theme=theme
        )
        # バックグラウンドでタイマーを開始
        timer.start()
        return (f"Success: A timer has been set for {duration_minutes} minutes with the theme '{theme}'. "
                f"You will be notified by {room_name}. **この設定タスクは完了しました。**"
                "設定が完了したことだけを簡潔にユーザーに報告してください。")
    except Exception as e:
        return f"Error: Failed to set the timer. Details: {e}"

@tool
def set_pomodoro_timer(work_minutes: int, break_minutes: int, cycles: int, work_theme: str, break_theme: str, room_name: str) -> str:
    """
    作業時間、休憩時間、サイクル数を指定してポモドーロタイマーを設定します。
    ただし、同じルームでポモドーロタイマーが既に実行中の場合は、新しいタイマーを設定しません。
    work_minutes: 1サイクルの作業時間（分）。
    break_minutes: 1サイクルの休憩時間（分）。
    cycles: 作業と休憩を繰り返す回数。
    work_theme: 作業終了時にAIが話す内容のテーマ。
    break_theme: 休憩終了時にAIが話す内容のテーマ。
    """
    if not room_name:
        return "【Error】Internal tool error: room_name is required for execution."
    try:
        # --- [門番ロジック] 既にポモドーロタイマーが存在しないかチェック ---
        for timer in ACTIVE_TIMERS:
            if timer.room_name == room_name and timer.timer_type == "ポモドーロタイマー":
                remaining_seconds = timer.get_remaining_time()
                return (f"Success (Ignored): ポモドーロタイマーは既に実行中です。"
                        f"現在のサイクルの残り時間は約{remaining_seconds:.0f}秒です。新しいタイマーは設定しません。"
                        "ユーザーにはこの状況を伝えてください。")
        
        api_key_name = config_manager.initial_api_key_name_global

        timer = UnifiedTimer(
            timer_type="ポモドーロタイマー",
            work_minutes=float(work_minutes),
            break_minutes=float(break_minutes),
            cycles=int(cycles),
            room_name=room_name,
            work_theme=work_theme,
            break_theme=break_theme,
            api_key_name=api_key_name
        )
        timer.start()
        return f"Success: A Pomodoro timer has been set for {cycles} cycles ({work_minutes} min work, {break_minutes} min break). You will be notified by {room_name}. **この設定タスクは完了しました。**設定が完了したことだけを簡潔にユーザーに報告してください。"
    except Exception as e:
        return f"Error: Failed to set the Pomodoro timer. Details: {e}"
