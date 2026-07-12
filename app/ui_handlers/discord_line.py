"""ui_handlers のうち「Discord / LINE 連携」ドメイン。

ui_handlers パッケージから再エクスポートされ、呼び出し側は従来どおり
ui_handlers.<関数名> でアクセスできる。
"""

import logging
from typing import Dict, List

import gradio as gr

import config_manager
import constants
import room_manager

try:
    import discord_manager
except ImportError:
    discord_manager = None

logger = logging.getLogger(__name__)


_DISCORD_CHANNEL_MODE_ALIASES = {
    "always": "always",
    "常時反応": "always",
    "常時": "always",
    "all": "always",
    "mention": "mention",
    "mentions": "mention",
    "メンション時のみ": "mention",
    "メンション": "mention",
    "メンションのみ": "mention",
    "ignore": "ignore",
    "無視": "ignore",
    "off": "ignore",
}
_DISCORD_CHANNEL_MODE_LABELS = {
    "always": "常時反応",
    "mention": "メンション時のみ",
    "ignore": "無視",
}


def handle_save_discord_webhook(webhook_url: str):
    if config_manager.save_config_if_changed("notification_webhook_url", webhook_url):
        gr.Info("Discord Webhook URLを保存しました。")


def _parse_csv_ids(value: str) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _format_discord_channel_response_modes(modes: Dict[str, str]) -> str:
    if not isinstance(modes, dict) or not modes:
        return ""
    lines = []
    for channel_id, mode in sorted(modes.items()):
        label = _DISCORD_CHANNEL_MODE_LABELS.get(mode, mode)
        lines.append(f"{channel_id}={label}")
    return "\n".join(lines)


def _parse_discord_channel_response_modes(value: str) -> Dict[str, str]:
    modes = {}
    if not value:
        return modes
    for raw_line in str(value).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" in line:
            channel_id, mode = line.split("=", 1)
        elif ":" in line:
            channel_id, mode = line.split(":", 1)
        else:
            raise ValueError(f"チャンネル別反応モードの形式が正しくありません: {line}")
        channel_id = channel_id.strip()
        mode_key = _DISCORD_CHANNEL_MODE_ALIASES.get(mode.strip())
        if not channel_id or not mode_key:
            raise ValueError(f"チャンネル別反応モードの値が正しくありません: {line}")
        modes[channel_id] = mode_key
    return modes


def handle_load_discord_bot_settings(room_name: str):
    """現在のルーム個別Discord Bot設定をUIへ反映する"""
    settings = config_manager.get_room_discord_bot_settings(room_name)
    enabled = settings.get("enabled", False)
    has_token = bool(settings.get("token"))
    if enabled and has_token:
        status_text = "Botの状態: 🟢 有効（起動中または起動対象）"
    elif has_token:
        status_text = "Botの状態: ⚪ 無効（Botトークン保存済み・有効化チェックがOFF）"
    else:
        status_text = "Botの状態: ⚪ 無効"
    return [
        gr.update(value=enabled),
        gr.update(value=settings.get("token", ""), type="password"),
        gr.update(value=", ".join([str(v) for v in settings.get("authorized_user_ids", [])])),
        gr.update(value=", ".join([str(v) for v in settings.get("allowed_channel_ids", [])])),
        gr.update(value=settings.get("default_channel_id", "")),
        gr.update(value=settings.get("mention_only", False)),
        gr.update(value=_format_discord_channel_response_modes(settings.get("channel_response_modes", {}))),
        gr.update(value=settings.get("allow_autonomous_send", False)),
        gr.update(value=settings.get("persona_webhook_url", ""), type="password"),
        gr.update(value=", ".join([str(v) for v in settings.get("approval_command_allowlist", [])])),
        gr.update(value=settings.get("voice_input_enabled", False)),
        gr.update(value=settings.get("voice_input_confirm_transcript", True)),
        gr.update(value=int(settings.get("voice_input_timeout_minutes", 10) or 10)),
        gr.update(value=str(settings.get("voice_input_stt_model") or constants.DISCORD_VOICE_STT_MODEL)),
        gr.update(value=status_text),
    ]


def _format_global_discord_migration_status() -> str:
    settings = config_manager.get_global_discord_bot_settings()
    has_token = bool(settings.get("token"))
    enabled = bool(settings.get("enabled"))
    linked_room = settings.get("linked_room") or "未指定"
    auth_count = len(settings.get("authorized_user_ids", []))
    channel_count = len(settings.get("allowed_channel_ids", []))
    token_text = "あり" if has_token else "なし"
    enabled_text = "有効" if enabled else "無効"
    return (
        "旧共通Bot設定: "
        f"{enabled_text} / Botトークン: {token_text} / "
        f"許可ユーザー: {auth_count}件 / 許可チャンネル: {channel_count}件 / "
        f"紐付けルーム: {linked_room}"
    )


def handle_load_global_discord_migration_state(current_room: str):
    """旧共通Discord Bot設定の移行UIを更新する。"""
    room_choices = room_manager.get_room_list_for_ui()
    room_values = [choice[1] if isinstance(choice, (list, tuple)) and len(choice) > 1 else choice for choice in room_choices]
    value = current_room if current_room in room_values else (room_values[0] if room_values else None)
    return [
        gr.update(value=_format_global_discord_migration_status()),
        gr.update(choices=room_choices, value=value),
    ]


def handle_migrate_global_discord_bot_to_room(target_room: str, current_room: str):
    """旧共通Discord Bot設定を選択ルームへ移行し、必要なら現在表示中フォームも更新する。"""
    try:
        success, message = config_manager.migrate_global_discord_bot_settings_to_room(target_room)
        status_update = gr.update(value=f"✅ {message}" if success else f"❌ {message}")
        if success and discord_manager is not None:
            try:
                discord_manager.stop_global_bot()
                discord_manager.stop_bot(room_name=target_room)
                settings = config_manager.get_room_discord_bot_settings(target_room)
                if settings.get("enabled") and settings.get("token"):
                    discord_manager.start_bot(room_name=target_room)
            except Exception as e:
                logger.warning(f"Discord global migration bot restart failed: {e}")

        global_status_update = gr.update(value=_format_global_discord_migration_status())
        if success and target_room == current_room:
            room_updates = handle_load_discord_bot_settings(current_room)
        else:
            room_updates = [gr.update() for _ in range(15)]
        return [status_update, global_status_update] + room_updates
    except Exception as e:
        logger.error(f"Failed to migrate global Discord settings: {e}", exc_info=True)
        return [gr.update(value=f"❌ 移行中にエラーが発生しました: {e}"), gr.update(value=_format_global_discord_migration_status())] + [gr.update() for _ in range(15)]


def handle_copy_global_discord_common_settings_to_room(target_room: str, current_room: str):
    """旧共通Discord Bot設定から、Botトークン以外の共通項目だけを選択ルームへコピーする。"""
    try:
        success, message = config_manager.copy_global_discord_common_settings_to_room(target_room)
        status_update = gr.update(value=f"✅ {message}" if success else f"❌ {message}")
        global_status_update = gr.update(value=_format_global_discord_migration_status())
        if success and target_room == current_room:
            room_updates = handle_load_discord_bot_settings(current_room)
        else:
            room_updates = [gr.update() for _ in range(15)]
        return [status_update, global_status_update] + room_updates
    except Exception as e:
        logger.error(f"Failed to copy global Discord common settings: {e}", exc_info=True)
        return [gr.update(value=f"❌ コピー中にエラーが発生しました: {e}"), gr.update(value=_format_global_discord_migration_status())] + [gr.update() for _ in range(15)]


def handle_save_discord_bot_settings(
    room_name: str,
    enabled: bool,
    token: str,
    auth_ids_str: str,
    allowed_channel_ids_str: str = "",
    default_channel_id: str = "",
    mention_only: bool = False,
    channel_response_modes_str: str = "",
    allow_autonomous_send: bool = False,
    persona_webhook_url: str = "",
    approval_ids_str: str = "",
    voice_input_enabled: bool = False,
    voice_input_confirm_transcript: bool = True,
    voice_input_timeout_minutes: int = 10,
    voice_input_stt_model: str = "",
):
    """ルーム個別Discord Botの設定を保存し、対象Botを再起動する"""
    try:
        if not room_name:
            return gr.update(value="Botの状態: ❌ ルームが選択されていません。")

        auth_ids = _parse_csv_ids(auth_ids_str)
        allowed_channel_ids = _parse_csv_ids(allowed_channel_ids_str)
        approval_ids = _parse_csv_ids(approval_ids_str)
        channel_response_modes = _parse_discord_channel_response_modes(channel_response_modes_str)

        duplicates = config_manager.find_duplicate_discord_bot_tokens(room_name, token) if enabled and token else []
        migrated_from_global = False
        if duplicates:
            if duplicates == ["global"]:
                can_migrate, migrate_reason = config_manager.can_migrate_global_discord_bot_token_to_room(room_name, token)
                if can_migrate:
                    migrated_from_global = True
                else:
                    return gr.update(value=f"Botの状態: ❌ 同じBotトークンが他の設定で使用されています: global（移行不可: {migrate_reason}）")
            else:
                return gr.update(value=f"Botの状態: ❌ 同じBotトークンが他の設定で使用されています: {', '.join(duplicates)}")

        result = config_manager.save_room_discord_bot_settings(
            room_name=room_name,
            enabled=enabled,
            token=token,
            authorized_user_ids=auth_ids,
            allowed_channel_ids=allowed_channel_ids,
            default_channel_id=(default_channel_id or "").strip(),
            mention_only=mention_only,
            channel_response_modes=channel_response_modes,
            allow_autonomous_send=allow_autonomous_send,
            persona_webhook_url=(persona_webhook_url or "").strip(),
            approval_command_allowlist=approval_ids,
            voice_input_enabled=voice_input_enabled,
            voice_input_confirm_transcript=voice_input_confirm_transcript,
            voice_input_timeout_minutes=int(voice_input_timeout_minutes or 10),
            voice_input_stt_model=(voice_input_stt_model or constants.DISCORD_VOICE_STT_MODEL).strip(),
        )
        if not result:
            return gr.update(value="Botの状態: ❌ 設定の保存に失敗しました。")

        migration_note = ""
        if migrated_from_global:
            if config_manager.disable_global_discord_bot_settings_for_migration():
                migration_note = " / 旧共通設定から移行済み"
            else:
                return gr.update(value="Botの状態: ⚠️ ペルソナ設定は保存しましたが、旧共通設定の無効化に失敗しました。config.jsonを確認してください。")

        if discord_manager is None:
            if enabled:
                gr.Warning("Discord連携ライブラリが見つかりません。設定は保存しましたが、Botを起動できません。アプリを再起動してください。")
                return gr.update(value=f"Botの状態: ⚠️ ライブラリ未検出 (設定保存済み・再起動してください{migration_note})")
            else:
                disabled_reason = "・有効化チェックがOFF" if token else ""
                return gr.update(value=f"Botの状態: ⚪ 停止中 (設定保存済み{disabled_reason})")

        if migrated_from_global:
            discord_manager.stop_global_bot()
        discord_manager.stop_bot(room_name=room_name)
        if enabled and token:
            discord_manager.start_bot(room_name=room_name)
            return gr.update(value=f"Botの状態: 🟢 実行中 (ルーム: {room_name}{migration_note})")
        else:
            disabled_reason = "・有効化チェックがOFF" if token else ""
            return gr.update(value=f"Botの状態: ⚪ 停止中 (設定保存済み{disabled_reason})")
    except Exception as e:
        logger.error(f"Failed to save Discord settings: {e}")
        return gr.update(value=f"Botの状態: ❌ エラーが発生しました ({e})")


def handle_stop_discord_bot(room_name: str):
    """ルーム個別Discord Botを停止する"""
    try:
        if room_name:
            config_manager.save_room_discord_bot_settings(room_name, enabled=False)
        else:
            config_manager.save_discord_bot_settings(enabled=False)
        if discord_manager is not None:
            discord_manager.stop_bot(room_name=room_name)
        return gr.update(value="Botの状態: ⚪ 停止しました (無効化)")
    except Exception as e:
        logger.error(f"Failed to stop Discord bot: {e}")
        return gr.update(value=f"Botの状態: ❌ 停止エラー ({e})")


def handle_save_line_bot_settings(enabled: bool, token: str, secret: str, auth_ids_str: str, linked_room: str):
    """LINE Botの設定を保存し、再起動する"""
    try:
        auth_ids = []
        if auth_ids_str.strip():
            auth_ids = [aid.strip() for aid in auth_ids_str.split(",") if aid.strip()]

        config_manager.save_line_bot_settings(
            enabled=enabled,
            token=token,
            secret=secret,
            authorized_user_ids=auth_ids,
            linked_room=None if linked_room == "自動（現在のUIと連動）" else linked_room
        )

        try:
            import line_manager
        except ImportError:
            if enabled:
                gr.Warning("LINE連携ライブラリが見つかりません。設定は保存しましたが、Botを起動できません。アプリを再起動してください。")
                return gr.update(value="サーバー状態: ⚠️ ライブラリ未検出 (設定保存済み・再起動してください)")
            else:
                return gr.update(value="サーバー状態: ⚪ 停止中 (設定保存済み)")

        line_manager.stop_bot()
        if enabled and token and secret:
            line_manager.start_bot()
            return gr.update(value="サーバー状態: 🟢 実行中 (再起動しました)")
        else:
            return gr.update(value="サーバー状態: ⚪ 停止中 (設定保存済み)")
    except Exception as e:
        logger.error(f"Failed to save LINE settings: {e}")
        return gr.update(value=f"サーバー状態: ❌ エラーが発生しました ({e})")


def handle_stop_line_bot():
    """LINE Botを停止する"""
    try:
        config_manager.save_line_bot_settings(enabled=False)
        try:
            import line_manager
            line_manager.stop_bot()
        except ImportError:
            pass
        return gr.update(value="サーバー状態: ⚪ 停止しました (無効化)")
    except Exception as e:
        logger.error(f"Failed to stop LINE bot: {e}")
        return gr.update(value=f"サーバー状態: ❌ 停止エラー ({e})")
