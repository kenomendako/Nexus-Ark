from typing import Optional, List
from langchain_core.tools import tool


@tool
def send_discord_message(message: str, room_name: str, channel_id: Optional[str] = None, image_paths: Optional[List[str]] = None) -> str:
    """
    許可されたDiscordチャンネルへメッセージや画像を送信します。

    自律行動中に、ユーザーへDiscord上で直接知らせたいことがある場合に使用します。
    このツールは、対象ペルソナのDiscord Bot設定で「自律行動時のDiscord送信」が許可され、
    送信先チャンネルが許可済みの場合にのみ成功します。

    Args:
        message: Discordへ送信する本文。
        room_name: (システムで自動入力)
        channel_id: 送信先チャンネルID。省略時はペルソナ設定のデフォルト送信チャンネルを使います。
        image_paths: 添付する画像ファイルパスのリスト（任意）。
    """
    try:
        import discord_manager
        result = discord_manager.send_message_to_room(room_name, message, channel_id=channel_id, image_paths=image_paths)
        if result.get("success"):
            return result.get("message", "Discordへ送信しました。")
        return f"Discord送信に失敗しました: {result.get('error', '不明なエラー')}"
    except Exception as e:
        return f"Discord送信に失敗しました: {e}"


@tool
def send_discord_image(message: str, image_paths: List[str], room_name: str, channel_id: Optional[str] = None) -> str:
    """
    許可されたDiscordチャンネルへ画像付きメッセージを送信します。

    Args:
        message: 画像に添える本文。
        image_paths: 添付する画像ファイルパスのリスト。
        room_name: (システムで自動入力)
        channel_id: 送信先チャンネルID。省略時はペルソナ設定のデフォルト送信チャンネルを使います。
    """
    try:
        import discord_manager
        result = discord_manager.send_message_to_room(room_name, message, channel_id=channel_id, image_paths=image_paths)
        if result.get("success"):
            return result.get("message", "Discordへ画像を送信しました。")
        return f"Discord画像送信に失敗しました: {result.get('error', '不明なエラー')}"
    except Exception as e:
        return f"Discord画像送信に失敗しました: {e}"
