"""保存済みLite更新のCloudflare管理OAuth復旧を安全に仲介する。

このモジュールは、認証失敗または分類のない旧失敗更新をそのまま再送せず、同じ接続先の読み取り確認後に
新しい更新提案を作るための境界だけを提供する。Workerの公開やD1変更は行わない。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import file_lock_utils
import lite_cloud_setup
import lite_travel_operations as operations


_BINDING_KEYS = (
    "worker_url",
    "database_name",
    "resource_digest",
    "connection_fingerprint",
    "target_digest",
)
_DIGEST_KEYS = frozenset({"resource_digest", "connection_fingerprint", "target_digest"})
_AUTH_FAILURE_CATEGORY = "authentication"
_VALIDATED_STATUS = "validated"
_REQUIRED_STATUS = "required"
_PENDING_STATUS = "pending"
_SAFE_ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")
_LEGACY_CLI_FAILURE = re.compile(
    r"^(?:preflight|bookmark|migration|deploy)_(?:failed|execution_failed)$"
)


def _target_digest(value: Mapping[str, Any]) -> str:
    return str(value.get("target_digest") or value.get("asset_digest") or "").strip().lower()


def _binding_snapshot(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    snapshot: dict[str, str] = {}
    for key in _BINDING_KEYS:
        raw = value.get(key)
        if key == "target_digest":
            raw = raw or value.get("asset_digest")
        text = str(raw or "").strip()
        if key == "worker_url":
            text = operations._normalize_worker_url(text)
        elif key in _DIGEST_KEYS:
            text = text.lower()
        snapshot[key] = text
    return snapshot


def _same_binding(left: Any, right: Any) -> bool:
    left_snapshot = _binding_snapshot(left)
    right_snapshot = _binding_snapshot(right)
    return bool(
        left_snapshot
        and right_snapshot
        and all(
            left_snapshot.get(key)
            and left_snapshot.get(key) == right_snapshot.get(key)
            for key in _BINDING_KEYS
        )
    )


def _is_postflight_resume(value: Mapping[str, Any]) -> bool:
    return bool(
        value.get("resume_available") is True
        or value.get("failed_step") == "postflight"
        or (
            value.get("status") == "failed"
            and value.get("failure_code") == "postflight_not_ready"
            and "deploy" in (value.get("completed_steps") or [])
        )
    )


def _recovery_kind(value: Mapping[str, Any]) -> str:
    """認証断定と、分類のない旧CLI失敗を分ける。"""

    if value.get("failure_category") == _AUTH_FAILURE_CATEGORY:
        return _AUTH_FAILURE_CATEGORY
    if value.get("failure_category"):
        return ""
    failure_code = str(value.get("failure_code") or "").strip()
    return "unclassified" if _LEGACY_CLI_FAILURE.fullmatch(failure_code) else ""


def _authentication_candidates(offer: Mapping[str, Any]) -> list[dict[str, Any]]:
    """同じbindingの未復旧認証失敗または旧未分類CLI失敗を拾う。"""

    try:
        paths = sorted(
            operations._operation_dir().glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    candidates: list[dict[str, Any]] = []
    for path in paths:
        try:
            operation = operations._load_operation(path.stem)
        except (OSError, TypeError, ValueError, operations.LiteTravelOperationError):
            continue
        if not isinstance(operation, dict):
            continue
        if (
            operation.get("status") != "failed"
            or not _recovery_kind(operation)
            or operation.get("auth_recovery_status") == _VALIDATED_STATUS
            or _is_postflight_resume(operation)
            or not _same_binding(operation, offer)
        ):
            continue
        candidates.append(operation)
    return candidates


def decorate_offer(offer: Mapping[str, Any]) -> dict[str, Any]:
    """認証復旧が必要な更新提案へ、秘密なしの復旧状態を付加する。"""

    if not isinstance(offer, Mapping):
        raise operations.LiteTravelOperationError("Lite更新の提案が不正です。")
    decorated = dict(offer)
    if decorated.get("state") != "update_required":
        return decorated

    decorated.update(
        {
            "auth_recovery_available": False,
            "auth_recovery_operation_id": None,
            "auth_recovery_unclassified": False,
            "auth_recovery_status": None,
        }
    )
    # deploy済みpostflightの既存再開を、認証復旧候補で上書きしない。
    if decorated.get("resume_available") is True:
        return decorated

    candidates = _authentication_candidates(decorated)
    if not candidates:
        return decorated

    operation = candidates[0]
    operation_id = str(operation.get("operation_id") or "").strip()
    try:
        operation_id = operations._safe_operation_id(operation_id)
    except operations.LiteTravelOperationError:
        return decorated
    recovery_kind = _recovery_kind(operation)
    unclassified = recovery_kind == "unclassified"
    decorated.update(
        {
            "auth_recovery_available": True,
            "auth_recovery_operation_id": operation_id,
            "auth_recovery_unclassified": unclassified,
            "auth_recovery_status": "unclassified" if unclassified else _REQUIRED_STATUS,
            "can_update": False,
            "reason": (
                "前回のLite更新が途中で停止しています。"
                "同じ接続先の認証と権限を確認してから、新しい更新提案を作成します。"
                if unclassified
                else "Cloudflare管理アカウントの再認証が必要です。"
                "同じ接続先を確認してから、新しい更新提案を作成します。"
            ),
        }
    )
    return decorated


def _load_recovery_operation(offer: Mapping[str, Any]) -> dict[str, Any]:
    operation_id = str(offer.get("auth_recovery_operation_id") or "").strip()
    try:
        operation_id = operations._safe_operation_id(operation_id)
    except operations.LiteTravelOperationError:
        raise operations.LiteTravelOperationError(
            "認証復旧の対象操作を確認できません。保存済みの更新記録を確認してください。"
        ) from None
    try:
        operation = operations._load_operation(operation_id)
    except operations.LiteTravelOperationError:
        operation = None
    if not isinstance(operation, dict):
        raise operations.LiteTravelOperationError(
            "認証復旧の対象操作を確認できません。保存済みの更新記録を確認してください。"
        )
    if operation.get("auth_recovery_status") == _VALIDATED_STATUS:
        raise operations.LiteTravelOperationError(
            "この接続復旧記録は既に確認済みです。新しい更新提案を取得してください。"
        )
    if operation.get("status") != "failed" or not _recovery_kind(operation):
        raise operations.LiteTravelOperationError(
            "この更新記録は接続復旧の対象として確認できません。"
            "保存済みの更新記録と復旧手順を確認してください。"
        )
    if _is_postflight_resume(operation):
        raise operations.LiteTravelOperationError(
            "この更新記録は公開後の再開対象です。認証復旧として扱いません。"
        )
    if not _same_binding(operation, offer):
        raise operations.LiteTravelOperationError(
            "接続復旧の接続先またはUI版が変わりました。状態を確認して再計画してください。"
        )
    return operation


def _configured_resource_identity() -> dict[str, str]:
    """現行Wrangler設定から、復旧時に固定する公開資源IDだけを読む。"""

    try:
        config_path = operations.configured_wrangler_config_path()
        if not config_path:
            raise ValueError("wrangler_config_missing")
        _root, resolved_path = operations.lite_travel._resolve_wrangler_config(config_path)
        config = json.loads(
            resolved_path.read_text(encoding="utf-8"),
            object_pairs_hook=operations._unique_config_object,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        operations.lite_travel.LiteTravelError,
    ):
        raise operations.LiteTravelOperationError(
            "現在のLite用Wrangler設定を確認できません。保存済みの接続設定を確認してください。"
        ) from None
    if not isinstance(config, Mapping):
        raise operations.LiteTravelOperationError(
            "現在のLite用Wrangler設定の形式を確認できません。"
        )

    raw_account_id = config.get("account_id")
    if raw_account_id is not None and not isinstance(raw_account_id, str):
        raise operations.LiteTravelOperationError(
            "現在のLite用Wrangler設定の管理アカウントIDが不正です。"
        )
    account_id = str(raw_account_id or "").strip().lower()
    if account_id and not _SAFE_ACCOUNT_ID.fullmatch(account_id):
        raise operations.LiteTravelOperationError(
            "現在のLite用Wrangler設定の管理アカウントIDが不正です。"
        )
    worker_name = str(config.get("name") or "").strip()
    d1_databases = config.get("d1_databases")
    kv_namespaces = config.get("kv_namespaces")
    if (
        not worker_name
        or not isinstance(d1_databases, list)
        or len(d1_databases) != 1
        or not isinstance(d1_databases[0], Mapping)
        or not isinstance(kv_namespaces, list)
    ):
        raise operations.LiteTravelOperationError(
            "現在のLite用Wrangler設定でWorker／D1／KVを一意に確認できません。"
        )
    d1 = d1_databases[0]
    d1_id = str(d1.get("database_id") or "").strip()
    d1_name = str(d1.get("database_name") or "").strip()
    kv_binding = str(lite_cloud_setup.DEFAULT_KV_BINDING)
    matching_kv = [
        item
        for item in kv_namespaces
        if isinstance(item, Mapping) and str(item.get("binding") or "").strip() == kv_binding
    ]
    if len(matching_kv) != 1:
        raise operations.LiteTravelOperationError(
            "現在のLite用Wrangler設定で対象KVを一意に確認できません。"
        )
    kv_id = str(matching_kv[0].get("id") or "").strip()
    if not d1_id or not d1_name or not kv_id:
        raise operations.LiteTravelOperationError(
            "現在のLite用Wrangler設定でWorker／D1／KVのIDを確認できません。"
        )
    return {
        "account_id": account_id,
        "worker_name": worker_name,
        "d1_id": d1_id,
        "d1_name": d1_name,
        "kv_id": kv_id,
    }


def _saved_resource_identity() -> dict[str, Any]:
    identity = _configured_resource_identity()
    try:
        setup = lite_cloud_setup.resume_latest_setup_operation() or {}
    except (OSError, TypeError, ValueError, lite_cloud_setup.LiteCloudSetupError):
        setup = {}
    if isinstance(setup, Mapping):
        raw_account_id = (
            (setup.get("account") or {}).get("id")
            if isinstance(setup.get("account"), Mapping)
            else None
        )
        if raw_account_id is not None and not isinstance(raw_account_id, str):
            raise operations.LiteTravelOperationError(
                "保存済みの接続記録の管理アカウントIDが不正です。"
            )
        saved = {
            "account_id": str(raw_account_id or "").strip().lower(),
            "worker_name": str(
                (setup.get("worker") or {}).get("name") or ""
                if isinstance(setup.get("worker"), Mapping)
                else ""
            ).strip(),
            "d1_id": str(
                (setup.get("d1") or {}).get("id") or ""
                if isinstance(setup.get("d1"), Mapping)
                else ""
            ).strip(),
            "kv_id": str(
                (setup.get("kv") or {}).get("id") or ""
                if isinstance(setup.get("kv"), Mapping)
                else ""
            ).strip(),
        }
        if saved["account_id"] and not _SAFE_ACCOUNT_ID.fullmatch(saved["account_id"]):
            raise operations.LiteTravelOperationError(
                "保存済みの接続記録の管理アカウントIDが不正です。"
            )
        # 正規生成設定はaccount_idを省略する。既存の資源digestには設定と
        # setup accountの両方が含まれるため、提案時と同じ保存済みIDだけを補う。
        # この後のローカルbinding再照合が通るまでは外部診断を開始しない。
        if not identity["account_id"]:
            identity["account_id"] = saved["account_id"]
        for key in ("account_id", "worker_name", "d1_id", "kv_id"):
            if saved[key] and saved[key] != identity[key]:
                raise operations.LiteTravelOperationError(
                    "保存済みの接続記録と現在のLite用Wrangler設定が一致しません。"
                    "対象資源を確認してから再認証してください。"
                )
    if not _SAFE_ACCOUNT_ID.fullmatch(identity["account_id"]):
        raise operations.LiteTravelOperationError(
            "保存済みのLite接続設定から管理アカウントIDを確認できません。"
            "接続記録を確認してから再認証してください。"
        )
    return identity


def _assert_local_recovery_state(
    offer: Mapping[str, Any], operation: Mapping[str, Any], *, room_name: str
) -> str:
    target_digest = _target_digest(operation)
    if not operations.UI_ASSET_DIGEST.fullmatch(target_digest):
        raise operations.LiteTravelOperationError("Lite UIの更新対象を確認できません。")
    try:
        local = operations._validate_ui_metadata(
            operations._local_ui_metadata(), label="本体のLite UI版情報"
        )
    except operations.LiteTravelOperationError:
        raise
    if (
        str(local.get("asset_digest") or "").lower() != target_digest
        or str(local.get("ui_build") or "") != str(offer.get("ui_build") or "")
    ):
        raise operations.LiteTravelOperationError(
            "本体のLite UI版が認証復旧の提案と変わりました。状態を再確認してください。"
        )

    current = operations._assert_update_binding(operation, target_digest=target_digest)
    current_with_target = {**current, "target_digest": target_digest}
    if not _same_binding(current_with_target, operation) or not _same_binding(
        current_with_target, offer
    ):
        raise operations.LiteTravelOperationError(
            "Lite更新の接続先または保存領域が変わりました。状態を確認して再計画してください。"
        )
    try:
        presence = operations._presence_state(room_name)
    except operations.LiteTravelOperationError:
        raise
    if presence in operations.LOCAL_PRESENCE_LOCKING_STATES:
        raise operations.LiteTravelOperationError(
            "現在のお出かけ中はLite用クラウドを更新できません。帰宅後に再試行してください。"
        )
    return target_digest


def _inventory_error(
    failure_code: str = "", failure_category: str = "unknown"
) -> operations.LiteTravelOperationError:
    if failure_code == "bundled_runtime_unavailable" or failure_code.startswith(
        "development_runtime_"
    ):
        message = (
            "Liteの準備ツールを確認できないため、Cloudflareへの接続診断を開始できません。"
            "「Liteの準備ツール」の「状態を確認」で本体側の実行環境を確認してください。"
        )
    elif failure_category == "network":
        message = "Cloudflareの接続診断に失敗しました。通信状態を確認してから再試行してください。"
    elif failure_code in {"cloudflare_authentication_required", "account_selection_required"}:
        message = "Cloudflare管理アカウントの再認証を確認できませんでした。"
    elif failure_code in {"account_changed", "account_confirmation_mismatch"}:
        message = "Cloudflareの接続先アカウントが保存済みのaccountと異なります。"
    elif failure_code in {
        "cloudflare_inventory_failed",
        "existing_binding_mismatch",
        "existing_schema_unknown",
    }:
        message = (
            "同じCloudflare accountのWorker／D1／KVを確認できませんでした。"
            "Cloudflare管理権限と通信状態を確認してください。"
        )
    elif failure_code == "worker_version_missing":
        message = "現在のCloudflare Worker版を確認できないため、認証復旧を停止しました。"
    elif failure_category == "unknown":
        message = "Cloudflareの接続診断を確認できませんでした。状態を確認してから再試行してください。"
    else:
        message = "保存済みのCloudflare Worker／D1／KVと一致しないため、認証復旧を停止しました。"
    error = operations.LiteTravelOperationError(message)
    # LiteTravelOperationErrorの公開型は増やさず、loginへ進めるかの内部判断だけに使う。
    error.failure_code = failure_code  # type: ignore[attr-defined]
    error.failure_category = failure_category  # type: ignore[attr-defined]
    return error


def _runner_with_failure_category(
    runner: Callable[..., Any], categories: list[str]
) -> Callable[..., Any]:
    """CLI原文を保存せず、失敗区分だけを読み取り診断の外へ返す。"""

    def run(command: list[str], **kwargs: Any) -> Any:
        try:
            result = runner(command, **kwargs)
        except subprocess.TimeoutExpired:
            categories.append("network")
            raise
        except (OSError, subprocess.SubprocessError):
            categories.append("unknown")
            raise
        if int(getattr(result, "returncode", 0)) != 0:
            codes = operations._cli_error_codes(result)
            categories.append(operations._classify_cli_failure(result, codes=codes))
        return result

    return run


def _verify_inventory(
    identity: Mapping[str, Any], *, runner: Callable[..., Any], node_command: str | Path | None
) -> dict[str, Any]:
    expected: dict[str, str] = {
        "account_id": str(identity["account_id"]),
        "d1_id": str(identity["d1_id"]),
        "kv_id": str(identity["kv_id"]),
    }
    categories: list[str] = []
    diagnostic_runner = _runner_with_failure_category(runner, categories)
    try:
        diagnostic = lite_cloud_setup.read_only_cloudflare_diagnostics(
            selected_account_id=str(identity["account_id"]),
            resource_name=str(identity["worker_name"]),
            expected=expected,
            root=lite_cloud_setup.relay_root(),
            runner=diagnostic_runner,
            node_command=node_command,
        )
    except lite_cloud_setup.LiteCloudSetupError as exc:
        raise _inventory_error(
            str(getattr(exc, "failure_code", "")), categories[-1] if categories else "unknown"
        ) from None
    except Exception:
        raise _inventory_error(
            "cloudflare_inventory_failed", categories[-1] if categories else "unknown"
        ) from None

    if not isinstance(diagnostic, Mapping):
        raise _inventory_error(
            "cloudflare_inventory_failed", categories[-1] if categories else "unknown"
        )
    state = str(diagnostic.get("state") or "")
    failure_code = str(diagnostic.get("failure_code") or "")
    category = categories[-1] if categories else ""
    if state == "authentication_required":
        # read_only_cloudflare_diagnostics はwhoamiの通信／JSON失敗も同じstateへ
        # 畳み込むため、CLI runnerの分類を先に尊重して認証を断定しない。
        if category and category != _AUTH_FAILURE_CATEGORY:
            raise _inventory_error("cloudflare_inventory_failed", category)
        raise _inventory_error("cloudflare_authentication_required", "authentication")
    if state == "account_confirmation_required":
        if failure_code == "account_changed":
            raise _inventory_error(failure_code, _AUTH_FAILURE_CATEGORY)
        raise _inventory_error(failure_code or "account_changed", "unknown")
    if failure_code == "cloudflare_inventory_failed":
        raise _inventory_error(
            failure_code, categories[-1] if categories else "unknown"
        )
    account = diagnostic.get("account") if isinstance(diagnostic.get("account"), Mapping) else {}
    if str(account.get("id") or "").strip().lower() != str(identity["account_id"]):
        raise _inventory_error("account_changed", _AUTH_FAILURE_CATEGORY)
    if diagnostic.get("classification") != "existing":
        raise _inventory_error(failure_code, "unknown")
    resources = diagnostic.get("resources") if isinstance(diagnostic.get("resources"), Mapping) else {}
    worker = resources.get("worker") if isinstance(resources.get("worker"), Mapping) else {}
    d1 = resources.get("d1") if isinstance(resources.get("d1"), Mapping) else {}
    kv = resources.get("kv") if isinstance(resources.get("kv"), Mapping) else {}
    if (
        str(worker.get("name") or "") != str(identity["worker_name"])
        or str(d1.get("id") or "") != str(identity["d1_id"])
        or str(kv.get("id") or "") != str(identity["kv_id"])
    ):
        raise _inventory_error("resource_collision_detected", "unknown")
    return dict(diagnostic)


def _verify_remote_bindings(
    identity: Mapping[str, Any],
    diagnostic: Mapping[str, Any],
    *,
    runner: Callable[..., Any],
    node_command: str | Path | None,
) -> None:
    """診断で得た現行versionを使い、Worker bindingとD1 schemaを読み取る。"""

    resources = diagnostic.get("resources") if isinstance(diagnostic.get("resources"), Mapping) else {}
    worker = resources.get("worker") if isinstance(resources.get("worker"), Mapping) else {}
    version_id = str(worker.get("version_id") or "").strip()
    if not version_id:
        raise _inventory_error("worker_version_missing", "unknown")
    account_id = str(identity["account_id"])
    categories: list[str] = []
    diagnostic_runner = _runner_with_failure_category(runner, categories)
    fixed_runner = lite_cloud_setup._account_fixed_runner(diagnostic_runner, account_id)
    try:
        bindings = lite_cloud_setup.inspect_existing_worker_bindings_synthetic(
            account_id=account_id,
            worker_name=str(identity["worker_name"]),
            version_id=version_id,
            runner=fixed_runner,
            root=lite_cloud_setup.relay_root(),
            node_command=node_command,
        )
        schema = lite_cloud_setup.inspect_existing_d1_schema_synthetic(
            account_id=account_id,
            database_id=str(identity["d1_id"]),
            runner=fixed_runner,
            root=lite_cloud_setup.relay_root(),
            node_command=node_command,
        )
        lite_cloud_setup.validate_existing_resource_import(
            diagnostic=diagnostic,
            selected_d1_id=str(identity["d1_id"]),
            selected_kv_id=str(identity["kv_id"]),
            worker_bindings=bindings,
            d1_schema_version=schema,
        )
    except lite_cloud_setup.LiteCloudSetupError as exc:
        raise _inventory_error(
            str(getattr(exc, "failure_code", "")),
            categories[-1] if categories else "unknown",
        ) from None
    except Exception:
        raise _inventory_error(
            "existing_binding_mismatch", categories[-1] if categories else "unknown"
        ) from None


def _login(
    *, runner: Callable[..., Any], node_command: str | Path | None
) -> None:
    # 固定Wranglerと同じく、APIキー認証はキーとメールの組で成立する。
    if os.environ.get("CLOUDFLARE_API_TOKEN") or (
        os.environ.get("CLOUDFLARE_API_KEY") and os.environ.get("CLOUDFLARE_EMAIL")
    ):
        raise operations.LiteTravelOperationError(
            "環境変数の管理認証を使用中です。起動元の認証と権限を確認してください。"
        )
    try:
        result = lite_cloud_setup.start_cloudflare_login(
            confirmed=True,
            root=lite_cloud_setup.relay_root(),
            runner=runner,
            node_command=node_command,
            replace_existing_connection=False,
        )
    except lite_cloud_setup.LiteCloudSetupError:
        raise operations.LiteTravelOperationError(
            "Cloudflare管理アカウントの再認証を完了できませんでした。"
            "ブラウザで許可した後、もう一度お試しください。"
        ) from None
    except Exception:
        raise operations.LiteTravelOperationError(
            "Cloudflareのログイン画面を開始できませんでした。"
        ) from None
    if not isinstance(result, Mapping) or result.get("authenticated") is not True:
        raise operations.LiteTravelOperationError(
            "Cloudflare管理アカウントの再認証を確認できませんでした。"
        )


def _offer_safely(*, room_name: str) -> dict[str, Any]:
    try:
        # 既存の呼び出し互換を保ちつつ、UIから渡されたroomでは在室状態を再照合する。
        if room_name:
            fresh = operations.get_update_offer(True, room_name=room_name)
        else:
            fresh = operations.get_update_offer(True)
    except operations.LiteTravelOperationError:
        raise operations.LiteTravelOperationError(
            "再認証後のLite接続状態を確認できませんでした。通信状態を確認してください。"
        ) from None
    except Exception:
        raise operations.LiteTravelOperationError(
            "再認証後のLite接続状態を確認できませんでした。"
        ) from None
    if not isinstance(fresh, dict):
        raise operations.LiteTravelOperationError(
            "再認証後のLite更新提案を確認できませんでした。"
        )
    return fresh


def _pending_offer(
    offer: Mapping[str, Any], operation_id: str, *, unclassified: bool
) -> dict[str, Any]:
    pending = dict(offer)
    pending.update(
        {
            # blocked/check_failedでは復旧操作を再表示せず、通常の更新確認で
            # 状態が解除された後に同じ記録を再び候補化する。
            "auth_recovery_available": False,
            "auth_recovery_operation_id": operation_id,
            "auth_recovery_unclassified": unclassified,
            "auth_recovery_status": _PENDING_STATUS,
            "can_update": False,
        }
    )
    if not str(pending.get("reason") or "").strip():
        pending["reason"] = "再認証後のLite接続状態を確認できないため、更新を停止しました。"
    return pending


def _mark_validated(operation: dict[str, Any]) -> None:
    operation["auth_recovery_status"] = _VALIDATED_STATUS
    try:
        operations._save_operation(operation)
    except operations.LiteTravelOperationError:
        raise
    except Exception:
        raise operations.LiteTravelOperationError(
            "認証復旧の確認結果を保存できませんでした。更新は開始していません。"
        ) from None


def _mark_validated_history(operation: dict[str, Any]) -> None:
    """同一bindingの認証復旧対象だけを検証済みにする。"""

    for candidate in _authentication_candidates(operation):
        if candidate.get("status") != "failed" or _is_postflight_resume(candidate):
            continue
        _mark_validated(candidate)


def _validated_offer(offer: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(offer)
    result.update(
        {
            "auth_recovery_available": False,
            "auth_recovery_operation_id": None,
            "auth_recovery_unclassified": False,
            "auth_recovery_status": _VALIDATED_STATUS,
        }
    )
    return result


def _finish_fresh_offer(
    fresh: Mapping[str, Any],
    operation: dict[str, Any],
    *,
    operation_id: str,
    unclassified: bool,
) -> dict[str, Any]:
    state = str(fresh.get("state") or "")
    if state in {"blocked", "check_failed"}:
        return _pending_offer(fresh, operation_id, unclassified=unclassified)
    if state == "not_configured":
        raise operations.LiteTravelOperationError(
            "再認証後のLite接続設定を確認できません。保存済みの接続先を確認してください。"
        )
    if state not in {"up_to_date", "update_required"}:
        return _pending_offer(fresh, operation_id, unclassified=unclassified)
    if not _same_binding(fresh, operation):
        raise operations.LiteTravelOperationError(
            "再認証後のLite接続先またはUI版が保存済み記録と異なります。再計画してください。"
        )
    if state == "update_required" and fresh.get("can_update") is not True:
        return _pending_offer(fresh, operation_id, unclassified=unclassified)
    _mark_validated_history(operation)
    # freshのresume_available=Trueは、既存deploy後postflightの再開を優先する。
    return _validated_offer(fresh)


def _recover_locked(
    offer: Mapping[str, Any],
    *,
    room_name: str,
    runner: Callable[..., Any],
    node_command: str | Path | None,
) -> dict[str, Any]:
    requested_id = str(offer.get("auth_recovery_operation_id") or "").strip()
    try:
        requested_id = operations._safe_operation_id(requested_id)
    except operations.LiteTravelOperationError:
        raise operations.LiteTravelOperationError(
            "接続復旧の対象操作を確認できません。画面を更新して再計画してください。"
        ) from None

    decorated = decorate_offer(offer)
    if decorated.get("state") != "update_required" or not decorated.get(
        "auth_recovery_available"
    ):
        raise operations.LiteTravelOperationError(
            "現在のLite更新提案は認証復旧の対象ではありません。状態を再確認してください。"
        )
    current_id = str(decorated.get("auth_recovery_operation_id") or "").strip()
    try:
        current_id = operations._safe_operation_id(current_id)
    except operations.LiteTravelOperationError:
        current_id = ""
    if current_id != requested_id:
        raise operations.LiteTravelOperationError(
            "接続復旧の対象操作が更新されました。画面を更新して再計画してください。"
        )
    operation = _load_recovery_operation(decorated)
    operation_id = str(operation.get("operation_id") or "").strip()
    unclassified = _recovery_kind(operation) == "unclassified"
    identity = _saved_resource_identity()
    target_digest = _assert_local_recovery_state(
        decorated, operation, room_name=room_name
    )

    # 明示的な分類がある記録も含め、まず同じaccount／資源を読み取り確認する。
    # 診断が正常なら、Cloudflare管理OAuthを再度開かず新しい提案へ進む。
    try:
        diagnostic = _verify_inventory(identity, runner=runner, node_command=node_command)
        _verify_remote_bindings(
            identity,
            diagnostic,
            runner=runner,
            node_command=node_command,
        )
    except operations.LiteTravelOperationError as exc:
        # loginへ進めるのは、CLIの分類が明示的にauthenticationの場合だけ。
        if str(getattr(exc, "failure_category", "")) != _AUTH_FAILURE_CATEGORY:
            raise
        _login(runner=runner, node_command=node_command)
        # 公式画面の操作中に保存済みaccount／資源が変わった場合は、
        # 新しい設定での外部再診断へ進む前に元の提案との不一致を検出する。
        _assert_local_recovery_state(decorated, operation, room_name=room_name)
        if _saved_resource_identity() != identity:
            raise operations.LiteTravelOperationError(
                "再認証中に保存済みの管理アカウントまたは資源が変わりました。"
                "状態を確認して再計画してください。"
            )
        diagnostic = _verify_inventory(identity, runner=runner, node_command=node_command)
        _verify_remote_bindings(
            identity,
            diagnostic,
            runner=runner,
            node_command=node_command,
        )

    # OAuth後にもローカルbindingを再照合し、login中の設定差替えを許可しない。
    current = operations._assert_update_binding(operation, target_digest=target_digest)
    current_with_target = {**current, "target_digest": target_digest}
    if not _same_binding(current_with_target, operation):
        raise operations.LiteTravelOperationError(
            "再認証後のLite接続先または保存領域が変わりました。再計画してください。"
        )

    fresh = _offer_safely(room_name=room_name)
    return _finish_fresh_offer(
        fresh,
        operation,
        operation_id=operation_id,
        unclassified=unclassified,
    )


def recover_authentication(
    offer: Mapping[str, Any],
    *,
    confirmed: bool,
    room_name: str = "",
    runner: Callable[..., Any] = subprocess.run,
    node_command: str | Path | None = None,
) -> dict[str, Any]:
    """同一接続を読み取り確認して新しい更新提案を返す。公開処理は行わない。"""

    if not confirmed:
        raise operations.LiteTravelOperationError("外部変更の確認が必要です。")
    if not isinstance(offer, Mapping):
        raise operations.LiteTravelOperationError("Lite更新の提案が不正です。")
    try:
        with file_lock_utils.locked_file(
            operations._update_operation_lock_path().as_posix(), timeout=0.0
        ):
            return _recover_locked(
                offer,
                room_name=room_name,
                runner=runner,
                node_command=node_command,
            )
    except file_lock_utils.Timeout:
        raise operations.LiteTravelOperationError(
            "別のLite更新または認証復旧が実行中です。完了後に状態を確認してください。"
        ) from None
