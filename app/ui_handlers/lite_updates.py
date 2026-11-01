"""保存済み接続を使い、本体とLiteの更新確認・実行を同じ欄へまとめる。"""

from __future__ import annotations

from html import escape
import json
from typing import Any

import gradio as gr

import lite_travel_operations as operations
import lite_update_recovery as recovery


LITE_UPDATE_OUTPUT_COUNT = 10


def _view(offer: dict[str, Any], *, message: str = "") -> tuple:
    """既存7出力の後に、認証復旧Group・確認文・確認値を返す。"""
    configured = bool(offer.get("worker_url"))
    ready = (
        offer.get("state") == "update_required" and offer.get("can_update") is True
        and offer.get("auth_recovery_available") is not True
    )
    summary = "### スマホ版Liteの更新\n\n" + escape(str(offer.get("reason") or ""))
    confirmation = ""
    recovery_available = bool(configured and offer.get("auth_recovery_available") is True)
    recovery_confirmation = ""
    if recovery_available:
        failure_message = (
            "前回の更新が途中で止まっています。Cloudflareへの接続と更新権限を確認できます。"
            if offer.get("auth_recovery_unclassified") is True
            else "Cloudflareの更新権限を確認できませんでした。"
        )
        summary += (
            "\n\n" + failure_message +
            "下の「Cloudflare管理認証を再接続」から、既存のクラウドを管理する"
            "アカウントとの接続を確認し、必要な場合だけ接続許可をやり直せます。"
            "接続後は更新先を確認し、新しい更新候補を表示します。"
        )
        prompt = (
            "Cloudflareへの接続を確認し、必要な場合に接続許可をやり直しますか？\n\n"
            f"既存の接続先: {offer.get('worker_url', '')}\n"
            f"保存領域: {offer.get('database_name', '')}\n\n"
            "再接続が必要な場合はCloudflare公式画面で、既存のLite用クラウドを管理するアカウントへ"
            "ログインし、Authorize（接続を許可）を押してください。\n"
            "接続後にアカウントと更新先を確認します。"
            "この操作ではクラウドへの公開や保存領域の復元は行いません。"
        )
        recovery_confirmation = json.dumps(
            {"operation_id": offer.get("auth_recovery_operation_id"), "text": prompt},
            ensure_ascii=False,
        )
    if ready:
        revision = escape(str(offer.get("ui_build") or ""))
        summary += f"\n\n本体に含まれるLite画面 **{revision}** を、保存済みの接続先へ反映します。"
        if offer.get("resume_available"):
            summary += "\n\nクラウドへの反映は済んでいます。ボタンを押すと結果の確認だけを再開します。"
        prompt = (
            "スマホ版Liteの更新を実行しますか？\n\n"
            f"接続先: {offer.get('worker_url', '')}\n"
            f"保存領域: {offer.get('database_name', '')}\n"
            f"Lite画面: {offer.get('ui_build', '')}\n\n"
            "復旧点を取得してからクラウドを更新し、結果を確認します。"
            "失敗時は自動で元へ戻しません。\n"
            "反映済みの操作は、結果の確認だけを再開します。"
        )
        confirmation = json.dumps(
            {"operation_id": offer.get("operation_id"), "text": prompt}, ensure_ascii=False,
        )
    if message:
        summary += "\n\n" + message
    return (
        gr.update(value=summary if configured else ""),
        gr.update(interactive=ready, variant="primary" if ready else "secondary"),
        offer,
        confirmation,
        "",
        gr.update(visible=configured),
        # 診断結果で利用者が選んだ開閉状態を上書きしない。
        gr.update(),
        gr.update(visible=recovery_available),
        recovery_confirmation,
        "",
    )


def handle_lite_update_check(room_name: str = "") -> tuple:
    """起動後／更新確認時の小さな診断。計画作成や外部書込みは行わない。"""
    try:
        offer = recovery.decorate_offer(
            operations.get_update_offer(room_name=str(room_name or "")),
        )
    except Exception:
        # 接続や版の確認ができなくても、チャットの起動は続ける。
        return (
            gr.update(value="スマホ版Liteの更新を確認できませんでした。あとで更新確認を試してください。"),
            gr.update(interactive=False), {}, "", "", gr.update(), gr.update(),
            gr.update(visible=False), "", "",
        )
    return _view(offer)


def handle_lite_update_run(
    offer: dict[str, Any],
    confirmed: str,
    room_name: str = "",
    progress=gr.Progress(track_tqdm=False),
) -> tuple:
    """ブラウザで対象を明示確認した一候補だけを、既存の更新処理へ渡す。"""
    if not str(confirmed or "").strip():
        # 自己リセットのchangeでも通信・更新を始めない。
        return (
            gr.update(), gr.update(), gr.update(), gr.update(), "", gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(),
        )
    if not isinstance(offer, dict):
        return _view({}, message="更新候補を確認し直してください。")
    if str(confirmed) != str(offer.get("operation_id") or ""):
        return _view(offer, message="確認中に更新候補が変わりました。現在の対象を確認してから実行してください。")

    def show_wait(event):
        attempt = max(1, int(event.get("attempt") or 1))
        total = max(attempt, int(event.get("total_attempts") or attempt))
        progress((attempt, total), desc="スマホ版Liteへの反映を確認しています…")

    try:
        result = operations.run_connected_update(
            offer,
            confirmed=True,
            room_name=str(room_name or ""),
            on_postflight_retry=show_wait,
        )
        if not isinstance(result, dict) or result.get("status") not in {"completed", "up_to_date"}:
            return _view(
                offer,
                message="更新の完了を確認できませんでした。詳細・復旧手順で操作記録を確認してください。",
            )
        completed = {
            **offer, "state": "up_to_date", "can_update": False,
            "reason": "スマホ版Liteを、本体と同じUI版へ更新しました。",
        }
        if result.get("status") == "up_to_date":
            completed["reason"] = "スマホ版Liteは本体と同じUI版へ更新済みです。"
        message = "スマホに更新案内が出たら、会話が落ち着いた時に「画面を更新」を押してください。"
        warning = str(result.get("postflight_warning") or "").strip()
        if warning:
            message += "\n\n" + escape(warning)
        return _view(completed, message=message)
    except operations.LiteTravelOperationError as exc:
        # 同じ候補を保持し、deploy済みならbackendの記録から確認だけ再開する。
        try:
            failed_offer = recovery.decorate_offer(offer)
        except Exception:
            failed_offer = offer
        return _view(failed_offer, message="更新を完了できませんでした。" + escape(str(exc)))
    except Exception:
        return _view(offer, message="更新を完了できませんでした。詳細・復旧手順で操作記録を確認してください。")


def handle_lite_update_auth_recovery(
    offer: dict[str, Any],
    confirmed: str,
    room_name: str = "",
    progress=gr.Progress(track_tqdm=False),
):
    """確認された認証復旧だけを実行し、公開を始めず更新候補へ戻る。"""
    if not str(confirmed or "").strip():
        # 確認取消・自己リセットでは認証や診断を始めない。
        yield (
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(), gr.update(), "",
        )
        return
    if (
        not isinstance(offer, dict)
        or offer.get("auth_recovery_available") is not True
        or str(confirmed) != str(offer.get("auth_recovery_operation_id") or "")
    ):
        yield _view(
            offer if isinstance(offer, dict) else {},
            message="認証復旧の対象が変わりました。更新状態を確認し直してください。",
        )
        return
    yield _view(
        {**offer, "auth_recovery_available": False, "can_update": False},
        message=(
            "Cloudflareへの接続を確認しています。再接続が必要な場合は、公式画面で同じ管理アカウントへ"
            "ログインし、**Authorize（接続を許可）** を押してください。"
            "完了したらNexus Arkへ戻り、更新先の確認結果をお待ちください。"
        ),
    )
    progress(0, desc="Cloudflareへの接続許可と更新先を確認しています…")
    try:
        refreshed = recovery.recover_authentication(
            offer, confirmed=True, room_name=str(room_name or ""),
        )
        completed = refreshed.get("auth_recovery_status") == "validated"
        yield _view(
            refreshed,
            message=(
                "接続と更新先を確認しました。更新が必要な場合は、改めて対象を確認して実行してください。"
                if completed else
                "更新へ戻るための確認がまだ完了していません。画面の理由を確認してから再試行してください。"
            ),
        )
    except operations.LiteTravelOperationError as exc:
        yield _view(offer, message="認証復旧を完了できませんでした。" + escape(str(exc)))
    except Exception:
        yield _view(
            offer,
            message="認証復旧を完了できませんでした。通信状態とCloudflareの接続許可を確認して再試行してください。",
        )
