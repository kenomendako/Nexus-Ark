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
    log_f, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    
    if utils.is_in_quiet_hours(quiet_start, quiet_end):
        # 通知禁止時間帯でもログには残す
        if log_f:
            utils.save_message_to_log(log_f, "## SYSTEM:notification_blocked", f"📱 **通知（送信されず）**\n\n{message}")
        return f"現在は通知禁止時間帯（{quiet_start}〜{quiet_end}）のため、通知は送信されませんでした。ユーザーは後でログを確認できます。"
    
    # チャットログにも通知内容を記録（送信成否に関わらず）
    if log_f:
        utils.save_message_to_log(log_f, "## SYSTEM:notification_sent", f"📱 **通知を送信しました**\n\n{message}")
        
    # 設定から通知サービスを判断して送信
    notification_result = alarm_manager.send_notification(room_name, message, {})
    if isinstance(notification_result, dict) and not notification_result.get("success"):
        reason_parts = []
        if notification_result.get("message"):
            reason_parts.append(str(notification_result["message"]))
        if notification_result.get("status_code") is not None:
            reason_parts.append(f"HTTP {notification_result['status_code']}")
        if notification_result.get("request_id"):
            reason_parts.append(f"request={notification_result['request_id']}")
        errors = notification_result.get("errors") or []
        if errors:
            reason_parts.append(" / ".join(str(error) for error in errors))
        reason = " / ".join(reason_parts) if reason_parts else "通知送信に失敗しました。"
        return f"通知の送信に失敗しました: {reason}"
    
    result_suffix = ""
    if isinstance(notification_result, dict) and notification_result.get("service") == "pushover":
        request_id = notification_result.get("request_id")
        if request_id:
            result_suffix = f" (Pushover request: {request_id})"

    return f"通知の送信に成功しました{result_suffix}"
