"""共通更新画面から行う、Windows配布版ブラウザruntimeの準備。"""

from __future__ import annotations

import html
import logging
import subprocess
from collections.abc import Callable
from typing import Any

import gradio as gr

from playwright_node import prepare_playwright_node
from update_manager import UpdateManager, inspect_update_host_status

from .app_updates import update_host_guidance


logger = logging.getLogger(__name__)
_READY_HOST_STATES = {"ready", "not_required"}


def _update_view(
    message: str,
    *,
    group_visible: bool = True,
    prepare_visible: bool = False,
    prepare_interactive: bool = False,
) -> tuple[Any, Any, Any]:
    """ブラウザruntime欄の3出力契約を一か所で保つ。"""

    return (
        gr.update(value=message),
        gr.update(visible=group_visible),
        gr.update(
            visible=prepare_visible,
            interactive=prepare_interactive,
        ),
    )


def _manager(manager_factory: Callable[[], Any] | None) -> Any:
    if manager_factory is not None:
        return manager_factory()
    return UpdateManager(cleanup_old_archives=False)


def _node_version(node_path: str) -> str:
    """検証済みNodeの版だけを表示用に取得する。"""

    try:
        result = subprocess.run(
            [node_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("ブラウザ用Nodeの実versionを確認できません。") from exc
    version = str(result.stdout or "").strip()
    if not version:
        raise RuntimeError("ブラウザ用Nodeの実versionを確認できません。")
    return version


def _host_blocked_view(host_status: dict[str, Any]) -> tuple[Any, Any, Any]:
    guidance = update_host_guidance(host_status)
    if host_status.get("state") == "migration_required":
        next_step = "上の「新しい更新方式へ移行」を押してから、ブラウザ機能の準備を確認してください。"
    else:
        next_step = "更新方式の移行または復旧を先に完了してください。"
    return _update_view(
        "### ⚠️ ブラウザ機能の準備を確認できません\n\n"
        f"{guidance}\n\n{next_step}",
        prepare_visible=False,
        prepare_interactive=False,
    )


def _signed_update_required_view() -> tuple[Any, Any, Any]:
    return _update_view(
        "### ⚠️ ブラウザ機能の修復が必要です\n\n"
        "上の本体更新を確認し、署名済みの対応版を適用してください。"
        "更新後にこの欄の「ブラウザ機能の準備を確認」を押してください。\n\n"
        "この状態確認は外部通信をせず、インストール済みの実行環境と版だけを確認します。",
        prepare_visible=False,
        prepare_interactive=False,
    )


def _ready_view(
    *, acquired: bool = False, failure_on_error: bool = False
) -> tuple[Any, Any, Any]:
    try:
        node_path = prepare_playwright_node()
        version = _node_version(node_path)
    except Exception as exc:
        logger.error("Browser runtime binding check failed: %s", type(exc).__name__)
        return _apply_failure_view() if failure_on_error else _signed_update_required_view()
    safe_version = html.escape(version)
    acquisition = "署名済みの実行環境の取得・検証も完了しました。\n\n" if acquired else ""
    return _update_view(
        "### ✅ ブラウザ機能の準備が完了しました\n\n"
        "Nexus Arkを通常どおりご利用いただけます。\n\n"
        f"準備済み Node {safe_version} を使用します。\n\n"
        "Lite独立モードの導入を続ける場合は、**外部接続 → Lite・お出かけ → 初回設定・接続管理**へ進んでください。"
        "Liteの初回設定は、この後に行います。\n\n"
        f"{acquisition}"
        "署名済みの実行環境と、インストール済みNodeの実際の版を確認しました。"
        "この状態確認は外部通信を行いません。",
        prepare_visible=False,
        prepare_interactive=False,
    )


def _runtime_view(manager: Any, *, mode: str | None = None) -> tuple[Any, Any, Any]:
    mode = str(manager.runtime_repair_mode()) if mode is None else str(mode)
    if mode == "unsupported_platform":
        return _update_view(
            "### ℹ️ ブラウザ機能の準備（Windows）\n\n"
            "署名済みのブラウザ用実行環境の自動準備はWindows x64配布版が対象です。",
            prepare_visible=False,
            prepare_interactive=False,
        )
    if mode == "legacy_update_host_migration_required":
        return _update_view(
            "### 🔄 ブラウザ機能の準備には更新方式の移行が必要です\n\n"
            "上の「新しい更新方式へ移行」を押し、Nexus Arkを通常どおり終了して、"
            "別の移行状況ウィンドウで完了を確認してから、同じStart.batで起動し直してください。PowerShellを手動で操作する必要はありません。",
            prepare_visible=False,
            prepare_interactive=False,
        )
    if mode == "runtime_bootstrap_required":
        return _update_view(
            "### ℹ️ 署名済みブラウザ用Nodeを準備できます\n\n"
            "本体に対応した実行環境を署名検証して取得します。"
            "明示クリック時だけ実行し、CloudflareアカウントやAIキーは不要です。"
            "設定・会話は変更しません。\n\n"
            "既存の実行環境は直接置き換えません。",
            prepare_visible=True,
            prepare_interactive=True,
        )
    if mode == "ready":
        return _ready_view()
    return _signed_update_required_view()


def _apply_failure_view() -> tuple[Any, Any, Any]:
    return _update_view(
        "### ⚠️ ブラウザ機能の準備完了を確認できません\n\n"
        "準備完了を確認できません。状態を確認し直してください。\n\n"
        "必要な場合は上の本体更新を確認してください。",
        prepare_visible=True,
        prepare_interactive=True,
    )


def _status_check_view(
    *, manager_factory: Callable[[], Any] | None = None
) -> tuple[Any, Any, Any]:
    try:
        host_status = inspect_update_host_status()
    except Exception as exc:
        logger.error("Browser runtime host inspection failed: %s", type(exc).__name__)
        return _update_view(
            "### ⚠️ ブラウザ機能の準備を確認できません\n\n"
            "更新方式の状態を確認できませんでした。Nexus Arkを再起動して再確認してください。",
            prepare_visible=False,
            prepare_interactive=False,
        )
    if host_status.get("state") not in _READY_HOST_STATES:
        return _host_blocked_view(host_status)
    try:
        return _runtime_view(_manager(manager_factory))
    except Exception as exc:
        logger.error("Browser runtime status check failed: %s", type(exc).__name__)
        return _update_view(
            "### ⚠️ ブラウザ機能の準備を確認できません\n\n"
            "ローカルの実行環境の状態を確認できませんでした。Nexus Arkを再起動して再確認してください。",
            prepare_visible=False,
            prepare_interactive=False,
        )


def handle_browser_runtime_status_check(*, manager_factory: Callable[[], Any] | None = None):
    """外部通信をせず、ブラウザruntimeの状態と実bindingを確認する。"""

    return _status_check_view(manager_factory=manager_factory)


def handle_browser_runtime_status_check_with_progress(
    *, manager_factory: Callable[[], Any] | None = None
):
    """確認中の表示を先に返してから、既存の状態確認結果を返す。"""

    yield _update_view(
        "ブラウザ機能の状態を確認しています。表示まで少しお待ちください。",
        prepare_visible=False,
        prepare_interactive=False,
    )
    yield handle_browser_runtime_status_check(manager_factory=manager_factory)


def handle_browser_runtime_apply(*, manager_factory: Callable[[], Any] | None = None):
    """明示クリック後だけ、空のruntime枠へ署名済みruntimeを導入する。"""

    try:
        host_status = inspect_update_host_status()
        if host_status.get("state") not in _READY_HOST_STATES:
            yield _host_blocked_view(host_status)
            return
        manager = _manager(manager_factory)
        mode = str(manager.runtime_repair_mode())
        if mode != "runtime_bootstrap_required":
            yield _runtime_view(manager, mode=mode)
            return
        yield _update_view(
            "### ⏳ ブラウザ機能を準備しています\n\n"
            "本体に対応した実行環境を署名検証して取得しています。"
            "完了するまでお待ちください。",
            prepare_visible=True,
            prepare_interactive=False,
        )
        success, _message = manager.bootstrap_bound_runtime()
        if not success:
            yield _apply_failure_view()
            return
        yield _ready_view(acquired=True, failure_on_error=True)
    except Exception as exc:
        logger.error("Browser runtime apply failed: %s", type(exc).__name__)
        yield _apply_failure_view()


__all__ = [
    "handle_browser_runtime_apply",
    "handle_browser_runtime_status_check",
    "handle_browser_runtime_status_check_with_progress",
]
