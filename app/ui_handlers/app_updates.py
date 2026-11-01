"""共通更新画面から行う、旧Windows更新方式の一度だけの移行。"""

import html
import logging

import gradio as gr

from update_manager import UpdateManager, inspect_update_host_status

logger = logging.getLogger(__name__)
_READY_STATES = {"not_required", "ready"}


def update_host_guidance(status):
    """本体更新・移行表示で共通の案内を返す。"""
    state = status.get("state")
    message = html.escape(str(status.get("message", "更新方式の状態を確認できませんでした。")))
    if state == "migration_pending":
        return (
            "### ⏳ 移行の準備ができました。本体を終了してください\n\n"
            "Nexus Arkを通常の方法で終了してください。別の移行状況ウィンドウで"
            "**移行完了**の表示を確認してから、今まで使っているフォルダーの"
            " **Start.bat** から起動し直してください。\n\n"
            "この画面の再起動ボタンは、移行が完了するまで使用できません。"
        )
    if state == "ready":
        return (
            "### ✅ 更新方式の準備は完了しています\n\n"
            "新しい更新方式を利用できます。移行操作のやり直しは不要です。"
            "今後も、今まで使っているフォルダーの **Start.bat** から起動してください。"
        )
    if state == "migration_required":
        return (
            "### 🔄 初回の更新方式移行が必要です\n\n"
            f"{message}\n\n"
            "今後の本体更新を行うため、一度だけ移行してください。"
            "独立モードやスマホ版Liteを使うかどうかにかかわらず必要です。"
            "現在の本体はそのまま利用できます。"
        )
    if state not in _READY_STATES:
        return (
            "### ⚠️ 更新方式の状態を確認してください\n\n"
            f"{message}\n\n"
            "安全のため、本体更新とこの画面の再起動を停止しています。"
            "Nexus Arkを通常の方法で終了し、同じ **Start.bat** から起動し直してください。"
            "状態が変わらない場合は、更新の復旧が必要です。"
        )
    return message


def _host_view(status):
    state = status.get("state")
    needs_attention = state not in _READY_STATES
    return (
        gr.update(value=update_host_guidance(status)),
        gr.update(visible=needs_attention or state == "ready"),
        gr.update(
            visible=state == "migration_required",
            interactive=state == "migration_required" and status.get("can_prepare") is True,
        ),
        "",
        gr.update(interactive=state not in {"migration_pending", "migration_blocked"} and state in _READY_STATES | {"migration_required"}),
        gr.update(open=True) if needs_attention else gr.update(),
    )


def handle_app_update_host_check():
    """起動・更新確認時にローカルの移行状態だけを照会する。"""
    try:
        status = inspect_update_host_status()
    except Exception as exc:
        logger.error("Update host inspection failed: %s", type(exc).__name__)
        status = {"state": "migration_blocked", "can_prepare": False}
    return _host_view(status)


def handle_app_update_host_migration(confirmed, *, manager_factory=None):
    """確認済み操作だけで署名済みhelperの準備を行い、通常終了を待つ。"""
    if str(confirmed).strip().lower() != "true":
        return gr.update(), gr.update(), gr.update(), "", gr.update(), gr.update()
    try:
        status = inspect_update_host_status()
        if status.get("state") != "migration_required" or not status.get("can_prepare"):
            return _host_view(status)
        manager = manager_factory() if manager_factory else UpdateManager(cleanup_old_archives=False)
        status = manager.update_host_status()
        if status.get("state") != "migration_required" or not status.get("can_prepare"):
            return _host_view(status)
        success, _message = manager.prepare_legacy_update_host_migration()
        if success:
            return _host_view({"state": "migration_pending", "can_prepare": False})
        status = manager.update_host_status()
        result = list(_host_view(status))
        result[0] = gr.update(value="### ❌ 更新方式の移行を準備できませんでした\n\n" + update_host_guidance(status))
        return tuple(result)
    except Exception as exc:
        logger.error("Update host migration failed: %s", type(exc).__name__)
        return _host_view({"state": "migration_blocked", "can_prepare": False})
