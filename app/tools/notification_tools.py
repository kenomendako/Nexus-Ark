# tools/notification_tools.py
# AIペルソナがユーザーに通知を送るためのツール

from langchain_core.tools import tool
import config_manager
import alarm_manager
import utils
import room_manager


@tool
def send_user_notification(message: str, room_name: str) -> str:
    """
    ユーザーにDiscordまたはPushover通知を送信します。
    
    自律行動中、ユーザーに伝えたいことがある場合に使用してください。
    通知が不要な場合（静かに活動したい場合）は、このツールを呼び出さないでください。
    
    ※ 通知禁止時間帯（Quiet Hours）の場合は、送信されません。
    
    message: ユーザーに送りたいメッセージ内容
    """
    # 通知禁止時間帯のチェック
    effective_settings = config_manager.get_effective_settings(room_name)
    auto_settings = effective_settings.get("autonomous_settings", {})
    quiet_start = auto_settings.get("quiet_hours_start", "00:00")
    quiet_end = auto_settings.get("quiet_hours_end", "07:00")
    
    # ログファイルパスを取得
    log_f, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    
    if utils.is_in_quiet_hours(quiet_start, quiet_end):
        # 通知禁止時間帯でもログには残す
        if log_f:
            utils.save_message_to_log(log_f, "## SYSTEM:notification_blocked", f"📱 **通知（送信されず）**\n\n{message}")
        return f"現在は通知禁止時間帯（{quiet_start}〜{quiet_end}）のため、通知は送信されませんでした。ユーザーは後でログを確認できます。"
    
    # 設定から通知サービスを判断して送信
    alarm_manager.send_notification(room_name, message, {})
    
    # チャットログにも通知内容を記録
    if log_f:
        utils.save_message_to_log(log_f, "## SYSTEM:notification_sent", f"📱 **通知を送信しました**\n\n{message}")
    
    return f"通知を送信しました: {message[:50]}..."
