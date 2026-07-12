# tools/alarm_tools.py の内容を、以下のコードで完全に置き換えてください

import uuid
import datetime
from langchain_core.tools import tool
from dateutil.parser import parse, ParserError
import alarm_manager
from typing import List # ★ 変更点1: Listをインポート

def _parse_flexible_date(date_str: str) -> datetime.date:
    """ "tomorrow", "next monday" などの曖昧な日付表現を、深夜の文脈を考慮して解釈する """
    now = datetime.datetime.now()
    today = now.date()

    if not date_str or date_str.lower() in ["today", "今日"]:
        return today

    # まずは普通に日付を解釈
    try:
        if date_str.lower() in ["tomorrow", "明日"]:
            parsed_date = today + datetime.timedelta(days=1)
        else:
            parsed_date = parse(date_str, default=now.replace(hour=0, minute=0, second=0, microsecond=0)).date()

        # 深夜補正ロジック
        # AIが「明日」と解釈し、かつ現在時刻が午前4時より前の場合、
        # それは「今日の朝」のことだと判断し、日付を1日戻す
        if parsed_date == today + datetime.timedelta(days=1) and now.hour < 4:
            print(f"  - 深夜補正: '明日' ({parsed_date}) を '今日' ({today}) として扱います。")
            return today

        return parsed_date

    except (ValueError, TypeError, ParserError):
        return today

@tool
def set_personal_alarm(
    time: str,
    context_memo: str,
    room_name: str,
    date: str = None,
    days: List[str] = None,
    is_emergency: bool = False
) -> str:
    """
    ユーザーとの対話の中で、未来の特定の日時に送信するためのアラームを設定する。
    time: "HH:MM"形式の時刻。
    context_memo: アラームの目的や背景を要約した短いメモ。
    date: "YYYY-MM-DD"や"tomorrow"など、アラームを設定する単発の日付。
    days: ["Monday", "Friday"]など、アラームを繰り返す曜日のリスト。
    is_emergency: Trueの場合、緊急通知として送信する。
    """
    try:
        alarm_dt_obj = parse(time)
        time_str = alarm_dt_obj.strftime("%H:%M")

        new_alarm = {
            "id": str(uuid.uuid4()),
            "time": time_str,
            "character": room_name,
            "context_memo": context_memo,
            "enabled": True,
            "is_emergency": is_emergency
        }

        if days and isinstance(days, list) and len(days) > 0:
            valid_days = [d.lower()[:3] for d in days]
            new_alarm["days"] = valid_days
            schedule_info = f"every {', '.join(days)}"
        else:
            alarm_date_obj = _parse_flexible_date(date)

            now = datetime.datetime.now()
            if date is None and alarm_dt_obj.time() < now.time():
                alarm_date_obj += datetime.timedelta(days=1)

            alarm_date_str = alarm_date_obj.strftime("%Y-%m-%d")
            new_alarm["date"] = alarm_date_str
            new_alarm["days"] = []
            schedule_info = f"for {alarm_date_str}"

        # 重複チェックを実行
        duplicate = alarm_manager.check_duplicate_alarm(new_alarm)
        if duplicate:
            existing_memo = duplicate.get("context_memo", "なし")
            return f"INFO: その時刻（{time_str} {schedule_info}）には既にアラームが設定されています（メモ: '{existing_memo}'）。**二重に登録する必要はありません。** 既に設定済みであることをユーザーに伝えてください。"

        if alarm_manager.add_alarm_entry(new_alarm):
            return f"Success: アラームをセットしました。設定時刻: {time_str} ({schedule_info}), 内容: '{context_memo}'。 **この設定タスクは完了しました。**設定内容を簡潔にユーザーに報告してください。"
        else:
            return "Error: Failed to save the alarm entry."

    except Exception as e:
        return f"Error: An unexpected error occurred. Details: {e}"
