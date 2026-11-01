"""Lite用クラウド初回セットアップの安全な状態機械。

パッケージ1のローカル技術ゲートに加え、パッケージ2では秘密なし操作記録と
Cloudflareの読み取り専用導入診断を扱う。Package 4の変更系は、注入された合成runnerでのみ
未公開versionと明示公開の境界を検証する。
"""

from __future__ import annotations

import json
import copy
import hashlib
import os
import platform
import re
import secrets as secure_secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlsplit

import constants
import file_lock_utils
from lite_development_runtime import (
    LiteDevelopmentRuntimeError,
    LiteDevelopmentRuntimePaths,
    resolve_lite_development_runtime,
)
from lite_runtime import LiteRuntimeError, LiteRuntimePaths, resolve_lite_runtime
from update_host.contracts import is_python_bytecode_cache, validate_release_tree
from update_host.runtime import validate_bound_runtime
from update_manager import legacy_overlay_allowed, protected_update_host_ready
from tls_security import create_tls_context


MINIMUM_NODE_MAJOR = 22
EXPECTED_WRANGLER_VERSION = "4.149.0"
REQUIRED_BOOTSTRAP_SECRETS = (
    "OWNER_AUTH_TOKEN",
    "BUNDLE_SIGNING_KEY",
    "STANDBY_ENCRYPTION_KEY",
)
REQUIRED_RELAY_FILES = (
    "package.json",
    "package-lock.json",
    "src/index.ts",
    "migrations/0001_phase0_core.sql",
    "migrations/0009_phase5_operations_standby.sql",
    "migrations/0010_return_high_water_guards.sql",
    "scripts/build-unified-lite.mjs",
    "scripts/wrangler-secret-stdin-preload.cjs",
    "wrangler.phase2.example.jsonc",
)
# 初回公開時に登録する標準スケジュール。設定に独自Cronがあれば、API側の
# 既存値を先に読み取って保持したうえで、この2件だけを不足分として追加する。
REQUIRED_INITIAL_CRONS = (
    "17 3 * * *",
    "*/5 * * * *",
)
VIRTUAL_SECRET_FILE_NAME = ".nexus-ark-virtual-secrets.json"
_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
SETUP_OPERATION_SCHEMA_VERSION = 1
DEFAULT_RESOURCE_NAME = "nexus-ark-lite-relay"
DEFAULT_D1_BINDING = "DB"
DEFAULT_KV_BINDING = "MODEL_CATALOG_CACHE"
EXPECTED_D1_SCHEMA_VERSION = 10
SETUP_OPERATION_STATES = frozenset(
    {
        "not_started",
        "prerequisites_checking",
        "prerequisites_ready",
        "authentication_required",
        "authentication_pending",
        "authenticated",
        "account_confirmation_required",
        "mode_selected",
        "resource_plan_ready",
        "resources_ready",
        "resources_creating",
        "local_config_ready",
        "bootstrap_secrets_ready",
        "migrated",
        "version_ready",
        "publish_confirmation_required",
        "deployed",
        "verified",
        "connected",
        "provider_ready",
        "paired",
        "standby_ready",
        "local_secret_recovery_required",
        "worker_url_recovery_required",
        "postflight_failed",
        "version_reconciliation_required",
        "worker_container_required",
        "worker_container_reconciliation_required",
        "publish_reconciliation_required",
        "resource_collision",
        "partial_resources",
        "secret_bootstrap_blocked",
        "client_update_required",
        "cancelled",
    }
)
SETUP_MODES = frozenset({"new", "import"})
_OPERATION_ID_PATTERN = re.compile(r"^[a-f0-9-]{32,36}$")
_CLOUDFLARE_ACCOUNT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_RESOURCE_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ALLOWED_FAILURE_CODES = frozenset(
    {
        "operation_record_corrupt",
        "operation_record_schema_invalid",
        "operation_record_state_invalid",
        "account_selection_required",
        "account_changed",
        "cloudflare_authentication_required",
        "cloudflare_inventory_failed",
        "resource_name_invalid",
        "resource_collision_detected",
        "partial_resources_detected",
        "new_plan_requires_unset_resources",
        "import_plan_requires_existing_resources",
        "wrangler_new_worker_required_secrets_unsupported",
        "operation_confirmation_mismatch",
        "external_changes_confirmation_required",
        "resource_plan_confirmation_mismatch",
        "account_confirmation_mismatch",
        "workers_dev_subdomain_required",
        "workers_dev_subdomain_invalid",
        "worker_url_invalid",
        "worker_url_confirmation_mismatch",
        "synthetic_execution_required",
        "d1_create_failed",
        "d1_reconciliation_failed",
        "d1_manual_reconciliation_required",
        "kv_create_failed",
        "kv_reconciliation_failed",
        "kv_manual_reconciliation_required",
        "existing_resource_mismatch",
        "existing_binding_mismatch",
        "existing_schema_unknown",
        "existing_schema_newer",
        "runtime_config_invalid",
        "runtime_config_exists",
        "runtime_config_outside_relay",
        "runtime_config_dry_run_failed",
        "bootstrap_secret_invalid",
        "static_assets_build_failed",
        "initial_migration_failed",
        "initial_schema_mismatch",
        "initial_version_upload_failed",
        "initial_version_id_missing",
        "worker_container_create_failed",
        "worker_container_reconciliation_required",
        "worker_subdomain_reconciliation_required",
        "version_reconciliation_required",
        "publish_confirmation_required",
        "initial_version_deploy_failed",
        "publish_reconciliation_required",
        "postflight_not_ready",
        "synthetic_secret_exposure_detected",
        "local_secret_recovery_required",
        "worker_url_recovery_required",
        "connection_save_failed",
        "provider_registration_failed",
        "connection_diagnostics_failed",
        "pairing_failed",
        "standby_prepare_failed",
        "bundled_runtime_unavailable",
        "development_runtime_path_not_absolute",
        "development_runtime_source_required",
        "development_runtime_node_missing",
        "development_runtime_relay_resources_missing",
        "development_runtime_lock_mismatch",
        "development_runtime_entrypoint_missing",
        "development_runtime_node_version_unavailable",
        "development_runtime_node_22_required",
        "development_runtime_wrangler_version_unavailable",
        "development_runtime_wrangler_version_mismatch",
        "runtime_prepare_confirmation_required",
    }
)


class LiteCloudSetupError(RuntimeError):
    """初回セットアップの安全条件を満たせない場合の例外。"""

    def __init__(self, message: str, *, failure_code: str):
        super().__init__(message)
        self.failure_code = failure_code


class _ResolvedRuntimeNode(str):
    """絶対Node pathと同じruntimeのentry pointを連鎖処理へ引き回す。"""

    runtime: LiteRuntimePaths

    def __new__(cls, runtime: LiteRuntimePaths) -> "_ResolvedRuntimeNode":
        value = str.__new__(cls, str(runtime.node))
        value.runtime = runtime
        return value


def _runtime_install_root(relay: Path) -> Path:
    """配布app／開発rootのどちらでも固定runtime配置のrootを返す。"""

    resolved = relay.absolute()
    if resolved.name == "lite-relay" and resolved.parent.name == "cloud":
        app_root = resolved.parent.parent
    else:
        app_root = resolved
    return app_root.parent if app_root.name == "app" else app_root


def _remove_generated_python_bytecode(app_root: Path) -> None:
    """未署名bytecodeを許容せず、既知の一時生成fileだけを検証前に除く。"""

    for cache_dir in app_root.rglob("__pycache__"):
        if cache_dir.is_symlink() or not cache_dir.is_dir():
            continue
        for candidate in cache_dir.iterdir():
            if candidate.is_symlink() or not candidate.is_file():
                continue
            relative = candidate.relative_to(app_root).as_posix()
            if is_python_bytecode_cache(relative):
                candidate.unlink()


def _validate_installed_runtime_binding(relay: Path, runtime: LiteRuntimePaths) -> None:
    """現在appの署名済みmanifestとruntimeを外部操作前に完全照合する。"""

    install_root = _runtime_install_root(relay)
    installed_app = install_root / "app"
    app_root = installed_app if (installed_app / "release_manifest.json").is_file() else install_root
    try:
        if not (app_root / "release_manifest.json").is_file():
            raise ValueError("installed app release manifest is missing")
        _remove_generated_python_bytecode(app_root)
        exact = validate_release_tree(
            app_root,
            expected_platform="windows",
            expected_cpu="x86_64",
            allow_persistent_state=True,
            allow_unlisted_legacy_overlay=legacy_overlay_allowed(install_root),
        )
        validate_bound_runtime(exact["manifest"], runtime.root)
    except Exception as exc:
        raise LiteCloudSetupError(
            "現在のNexus ArkとLite専用runtimeの組み合わせを確認できません。",
            failure_code="bundled_runtime_unavailable",
        ) from exc


def _command_runtime(
    relay: Path,
    *,
    node_command: str | Path | None,
    runner: Callable[..., Any],
) -> tuple[str, Path, Path, LiteRuntimePaths | LiteDevelopmentRuntimePaths | None]:
    """製品では同梱runtime、明示注入時だけ合成／開発commandを返す。"""

    if isinstance(node_command, _ResolvedRuntimeNode):
        runtime = node_command.runtime
        return node_command, runtime.wrangler, runtime.wrangler_cli, runtime
    if node_command is not None:
        return (
            str(node_command),
            relay / "node_modules" / "wrangler" / "bin" / "wrangler.js",
            relay / "node_modules" / "wrangler" / "wrangler-dist" / "cli.js",
            None,
        )
    try:
        development = resolve_lite_development_runtime(
            relay,
            runner=runner,
            expected_wrangler_version=EXPECTED_WRANGLER_VERSION,
            required_relay_files=REQUIRED_RELAY_FILES,
        )
    except LiteDevelopmentRuntimeError as exc:
        raise LiteCloudSetupError(str(exc), failure_code=exc.failure_code) from exc
    if development is not None:
        return (
            str(development.node),
            development.wrangler,
            development.wrangler_cli,
            development,
        )
    try:
        runtime = resolve_lite_runtime(
            _runtime_install_root(relay) / "runtime",
            runner=runner,
            app_version=constants.APP_VERSION,
        )
    except LiteRuntimeError as exc:
        raise LiteCloudSetupError(
            "Lite専用runtimeを確認できません。準備ツールの修復を実行してください。",
            failure_code="bundled_runtime_unavailable",
        ) from exc
    _validate_installed_runtime_binding(relay, runtime)
    return _ResolvedRuntimeNode(runtime), runtime.wrangler, runtime.wrangler_cli, runtime


def resolve_lite_command_runtime(
    relay: str | Path,
    *,
    node_command: str | Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[str, Path, Path, LiteRuntimePaths | LiteDevelopmentRuntimePaths | None]:
    """Lite command用entry pointを返す。製品既定では検証済みruntimeだけを許可する。"""

    return _command_runtime(Path(relay), node_command=node_command, runner=runner)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def setup_operation_root(*, metadata_root: Optional[Path] = None) -> Path:
    base = Path(metadata_root) if metadata_root is not None else Path(constants.METADATA_DIR)
    return base / "lite_travel" / "setup_operations"


def _operation_path(operation_id: str, *, metadata_root: Optional[Path] = None) -> Path:
    value = str(operation_id or "").lower()
    if not _OPERATION_ID_PATTERN.fullmatch(value):
        raise LiteCloudSetupError(
            "初回セットアップの操作IDが不正です。",
            failure_code="operation_record_schema_invalid",
        )
    return setup_operation_root(metadata_root=metadata_root) / f"{value}.json"


def _safe_resource(value: Any, *, name_key: str, id_key: str) -> Optional[dict[str, str]]:
    if not isinstance(value, Mapping):
        return None
    name = str(value.get(name_key) or "").strip()
    resource_id = str(value.get(id_key) or "").strip()
    if not name and not resource_id:
        return None
    return {name_key: name, id_key: resource_id}


def _safe_worker(value: Any) -> Optional[dict[str, str]]:
    if not isinstance(value, Mapping):
        return None
    safe = {
        "name": str(value.get("name") or "").strip(),
        "deployment_id": str(value.get("deployment_id") or "").strip(),
        "version_id": str(value.get("version_id") or "").strip(),
    }
    return safe if any(safe.values()) else None


def sanitize_setup_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    """操作記録を明示allowlistへ縮退し、秘密や絶対パスを保存しない。"""

    operation_id = str(operation.get("operation_id") or "").lower()
    if not _OPERATION_ID_PATTERN.fullmatch(operation_id):
        raise LiteCloudSetupError(
            "初回セットアップの操作IDが不正です。",
            failure_code="operation_record_schema_invalid",
        )
    state = str(operation.get("state") or "not_started")
    if state not in SETUP_OPERATION_STATES:
        raise LiteCloudSetupError(
            "初回セットアップの状態が不正です。",
            failure_code="operation_record_state_invalid",
        )
    mode = str(operation.get("mode") or "")
    if mode and mode not in SETUP_MODES:
        raise LiteCloudSetupError(
            "初回セットアップの方式が不正です。",
            failure_code="operation_record_schema_invalid",
        )
    config_path = str(operation.get("config_path") or "").replace("\\", "/").strip()
    if config_path and (
        Path(config_path).is_absolute()
        or re.match(r"^[A-Za-z]:/", config_path)
        or ".." in Path(config_path).parts
    ):
        raise LiteCloudSetupError(
            "設定ファイルはプロジェクト相対パスで記録してください。",
            failure_code="operation_record_schema_invalid",
        )
    failure_code = str(operation.get("failure_code") or "")
    if failure_code and failure_code not in _ALLOWED_FAILURE_CODES:
        failure_code = "operation_record_schema_invalid"

    account = operation.get("account") if isinstance(operation.get("account"), Mapping) else {}
    bindings = operation.get("bindings") if isinstance(operation.get("bindings"), Mapping) else {}
    worker = _safe_worker(operation.get("worker"))
    d1 = _safe_resource(operation.get("d1"), name_key="name", id_key="id")
    kv = _safe_resource(operation.get("kv"), name_key="name", id_key="id")
    setup_tag = str(operation.get("setup_tag") or "").strip()
    if setup_tag and not re.fullmatch(r"nexus-ark-setup-[a-f0-9]{12}", setup_tag):
        setup_tag = ""
    safe: dict[str, Any] = {
        "schema_version": SETUP_OPERATION_SCHEMA_VERSION,
        "operation_id": operation_id,
        "created_at": str(operation.get("created_at") or _utc_now()),
        "updated_at": str(operation.get("updated_at") or _utc_now()),
        "state": state,
        "mode": mode or None,
        "account": {
            "id": str(account.get("id") or "").strip(),
            "name": str(account.get("name") or "").strip(),
        },
        "worker": worker,
        "d1": d1,
        "kv": kv,
        "bindings": {
            "d1": DEFAULT_D1_BINDING if bindings.get("d1") == DEFAULT_D1_BINDING else "",
            "kv": DEFAULT_KV_BINDING if bindings.get("kv") == DEFAULT_KV_BINDING else "",
        },
        "d1_schema_version": (
            int(operation.get("d1_schema_version"))
            if isinstance(operation.get("d1_schema_version"), int)
            else None
        ),
        "worker_url": str(operation.get("worker_url") or "").strip(),
        "version_id": str(operation.get("version_id") or "").strip(),
        "completed_steps": [
            str(step) for step in operation.get("completed_steps", []) if isinstance(step, str)
        ],
        "failed_step": str(operation.get("failed_step") or ""),
        "failure_code": failure_code or None,
        "config_path": config_path or None,
        "last_remote_check_at": str(operation.get("last_remote_check_at") or "") or None,
        "can_rollback_worker": bool(operation.get("can_rollback_worker", False)),
        "credential_profile_id": str(operation.get("credential_profile_id") or "").strip(),
        "model_id": str(operation.get("model_id") or "").strip()[:200],
        "resource_plan_digest": str(operation.get("resource_plan_digest") or "").strip(),
        "setup_tag": setup_tag,
    }
    return safe


def create_setup_operation(
    *, mode: Optional[str] = None, metadata_root: Optional[Path] = None
) -> dict[str, Any]:
    now = _utc_now()
    operation = sanitize_setup_operation(
        {
            "operation_id": str(uuid.uuid4()),
            "created_at": now,
            "updated_at": now,
            "state": "not_started",
            "mode": mode,
        }
    )
    save_setup_operation(operation, metadata_root=metadata_root)
    return operation


def save_setup_operation(
    operation: Mapping[str, Any], *, metadata_root: Optional[Path] = None
) -> dict[str, Any]:
    candidate = dict(operation)
    candidate["updated_at"] = _utc_now()
    safe = sanitize_setup_operation(candidate)
    file_lock_utils.safe_json_write(
        _operation_path(safe["operation_id"], metadata_root=metadata_root).as_posix(), safe
    )
    return safe


def load_setup_operation(
    operation_id: str, *, metadata_root: Optional[Path] = None
) -> dict[str, Any]:
    path = _operation_path(operation_id, metadata_root=metadata_root)
    try:
        raw = file_lock_utils.safe_json_read(path.as_posix(), default={})
    except (OSError, ValueError):
        return {
            "operation_id": str(operation_id),
            "state": "cancelled",
            "failure_code": "operation_record_corrupt",
            "remote_recheck_required": True,
        }
    if not isinstance(raw, Mapping):
        return {
            "operation_id": str(operation_id),
            "state": "cancelled",
            "failure_code": "operation_record_corrupt",
            "remote_recheck_required": True,
        }
    try:
        schema_version = int(raw.get("schema_version", 0))
    except (TypeError, ValueError):
        schema_version = 0
    if schema_version > SETUP_OPERATION_SCHEMA_VERSION:
        return {
            "operation_id": str(operation_id),
            "schema_version": schema_version,
            "state": "client_update_required",
            "failure_code": "operation_record_schema_invalid",
            "remote_recheck_required": True,
        }
    try:
        safe = sanitize_setup_operation(raw)
    except LiteCloudSetupError as exc:
        return {
            "operation_id": str(operation_id),
            "state": "cancelled",
            "failure_code": exc.failure_code,
            "remote_recheck_required": True,
        }
    safe["remote_recheck_required"] = True
    return safe


def resume_latest_setup_operation(*, metadata_root: Optional[Path] = None) -> Optional[dict[str, Any]]:
    root = setup_operation_root(metadata_root=metadata_root)
    candidates = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    return load_setup_operation(candidates[0].stem, metadata_root=metadata_root)


def relay_root() -> Path:
    return Path(__file__).resolve().parent / "cloud" / "lite-relay"


def _registry_reachable() -> bool:
    """npm registryへのTCP到達性だけを確認し、HTTP要求は送らない。"""

    try:
        with socket.create_connection(("registry.npmjs.org", 443), timeout=3):
            return True
    except OSError:
        return False


def _strip_jsonc_comments(text: str) -> str:
    """JSONCのコメントを空白へ置換し、文字列中の ``//`` は保持する。"""

    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    in_line_comment = False
    in_block_comment = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_line_comment:
            if char in "\r\n":
                in_line_comment = False
                output.append(char)
            else:
                output.append(" ")
            index += 1
            continue
        if in_block_comment:
            if char == "*" and next_char == "/":
                output.extend((" ", " "))
                in_block_comment = False
                index += 2
            else:
                output.append(char if char in "\r\n" else " ")
                index += 1
            continue
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
        elif char == "/" and next_char == "/":
            output.extend((" ", " "))
            in_line_comment = True
            index += 2
        elif char == "/" and next_char == "*":
            output.extend((" ", " "))
            in_block_comment = True
            index += 2
        else:
            output.append(char)
            index += 1
    if in_block_comment:
        raise ValueError("unterminated JSONC block comment")
    return "".join(output)


def _strip_jsonc_trailing_commas(text: str) -> str:
    """JSONCの配列・オブジェクト末尾カンマを文字列外だけで除去する。"""

    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if char == '"' and not escaped:
            in_string = not in_string
        if char == "," and not in_string:
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue
        output.append(char)
        if in_string and char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
        index += 1
    return "".join(output)


def _read_json(path: Path) -> dict[str, Any]:
    """JSON/JSONCを読み、設定ファイル以外の不正な値は空辞書にする。"""

    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".jsonc":
            text = _strip_jsonc_trailing_commas(_strip_jsonc_comments(text))
        value = json.loads(text)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_version(value: Any) -> Optional[str]:
    match = _VERSION_PATTERN.search(str(value or ""))
    return ".".join(match.groups()) if match else None


def _run_version(command: list[str], *, runner: Callable[..., Any]) -> Optional[str]:
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if int(getattr(result, "returncode", 1)) != 0:
        return None
    return _parse_version(getattr(result, "stdout", ""))


def _lock_contract(root: Path) -> dict[str, Any]:
    package = _read_json(root / "package.json")
    lock = _read_json(root / "package-lock.json")
    package_dependencies = package.get("devDependencies")
    package_dependencies = (
        package_dependencies if isinstance(package_dependencies, Mapping) else {}
    )
    package_version = str(package_dependencies.get("wrangler") or "")
    packages = lock.get("packages")
    packages = packages if isinstance(packages, Mapping) else {}
    lock_root = packages.get("")
    lock_root = lock_root if isinstance(lock_root, Mapping) else {}
    lock_dependencies = lock_root.get("devDependencies")
    lock_dependencies = (
        lock_dependencies if isinstance(lock_dependencies, Mapping) else {}
    )
    lock_version = str(lock_dependencies.get("wrangler") or "")
    installed = packages.get("node_modules/wrangler")
    installed = installed if isinstance(installed, Mapping) else {}
    installed_version = str(installed.get("version") or "")
    exact = all(
        value == EXPECTED_WRANGLER_VERSION
        for value in (package_version, lock_version, installed_version)
    )
    return {
        "present": (root / "package-lock.json").is_file(),
        "exact": exact,
        "package_version": package_version or None,
        "lock_version": lock_version or None,
        "resolved_version": installed_version or None,
    }


def _development_preflight_result(
    *,
    missing: list[str],
    lock: Mapping[str, Any],
    runtime: LiteDevelopmentRuntimePaths | None = None,
    failure_code: str | None = None,
) -> dict[str, Any]:
    """明示opt-in開発runtimeの診断を、配布runtimeと同じallowlistで返す。"""

    failure_codes = [failure_code] if failure_code else []
    ready = runtime is not None and not missing and not failure_codes
    state = "ready" if ready else "prerequisite_missing"
    return {
        "state": state,
        "runtime_source": "development",
        "development_runtime_ready": ready,
        "bundled_runtime_ready": False,
        "runtime_root": None,
        "relay_resources_present": not missing,
        "missing_resources": missing,
        "node_available": runtime is not None,
        "node_version": runtime.node_version if runtime else None,
        "node_major": (
            int(runtime.node_version.split(".", 1)[0]) if runtime else None
        ),
        "node_22_or_newer": runtime is not None,
        "npm_available": False,
        "network_checked": False,
        "npm_registry_reachable": None,
        "wrangler_lock_present": bool(lock.get("present")),
        "wrangler_lock_exact": bool(lock.get("exact")),
        "wrangler_expected_version": EXPECTED_WRANGLER_VERSION,
        "wrangler_installed": runtime is not None,
        "wrangler_version": runtime.wrangler_version if runtime else None,
        "wrangler_ready": runtime is not None,
        "failure_codes": failure_codes,
        "external_changes_enabled": ready,
    }


def _resolve_development_preflight_runtime(
    relay: Path,
    *,
    runner: Callable[..., Any],
) -> tuple[LiteDevelopmentRuntimePaths | None, str | None]:
    """開発envの選択をpreflightでも共有し、失敗を専用codeへ保持する。"""

    try:
        runtime = resolve_lite_development_runtime(
            relay,
            runner=runner,
            expected_wrangler_version=EXPECTED_WRANGLER_VERSION,
            required_relay_files=REQUIRED_RELAY_FILES,
        )
    except LiteDevelopmentRuntimeError as exc:
        return None, exc.failure_code
    return runtime, None


def distribution_preflight(
    *,
    root: Optional[Path] = None,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], Optional[str]] = shutil.which,
    network_probe: Callable[[], bool] = _registry_reachable,
    check_network: bool = True,
    require_wrangler: bool = True,
    use_bundled_runtime: bool = True,
    _resolved_runtime: LiteRuntimePaths | LiteDevelopmentRuntimePaths | None = None,
) -> dict[str, Any]:
    """配布版相当の前提を個別診断する。

    標準出力・標準エラーは結果へ含めず、versionなどallowlist済み情報だけを返す。
    """

    relay = Path(root) if root is not None else relay_root()
    missing = [name for name in REQUIRED_RELAY_FILES if not (relay / name).is_file()]
    lock = _lock_contract(relay)
    bundled = use_bundled_runtime
    if bundled:
        development = (
            _resolved_runtime
            if isinstance(_resolved_runtime, LiteDevelopmentRuntimePaths)
            else None
        )
        if development is None:
            development, development_failure = _resolve_development_preflight_runtime(
                relay, runner=runner
            )
            if development_failure:
                return _development_preflight_result(
                    missing=missing,
                    lock=lock,
                    failure_code=development_failure,
                )
        if development is not None:
            return _development_preflight_result(
                missing=missing,
                lock=lock,
                runtime=development,
            )
        runtime = _resolved_runtime
        runtime_failure = False
        if runtime is None:
            try:
                _node, _wrangler, _wrangler_cli, runtime = _command_runtime(
                    relay, node_command=None, runner=runner
                )
            except LiteCloudSetupError:
                runtime_failure = True
        failure_codes = []
        if missing:
            failure_codes.append("relay_resources_missing")
        if runtime_failure:
            failure_codes.append("bundled_runtime_unavailable")
        state = "ready" if not failure_codes else "prerequisite_missing"
        return {
            "state": state,
            "runtime_source": "bundled",
            "development_runtime_ready": False,
            "bundled_runtime_ready": runtime is not None,
            "runtime_root": str(runtime.root) if runtime else None,
            "relay_resources_present": not missing,
            "missing_resources": missing,
            "node_available": runtime is not None,
            "node_version": runtime.node_version if runtime else None,
            "node_major": int(runtime.node_version.split(".", 1)[0]) if runtime else None,
            "node_22_or_newer": runtime is not None,
            "npm_available": False,
            "network_checked": False,
            "npm_registry_reachable": None,
            "wrangler_lock_present": lock["present"],
            "wrangler_lock_exact": lock["exact"],
            "wrangler_expected_version": EXPECTED_WRANGLER_VERSION,
            "wrangler_installed": runtime is not None,
            "wrangler_version": runtime.wrangler_version if runtime else None,
            "wrangler_ready": runtime is not None,
            "failure_codes": failure_codes,
            "external_changes_enabled": state == "ready",
        }

    # custom whichを渡す開発・合成テストだけに旧PATH診断を残す。
    node = which("node")
    npm = which("npm")
    node_version = _run_version([node, "--version"], runner=runner) if node else None
    node_major = int(node_version.split(".", 1)[0]) if node_version else None
    node_ready = bool(node_major is not None and node_major >= MINIMUM_NODE_MAJOR)
    wrangler_path = relay / "node_modules" / "wrangler" / "bin" / "wrangler.js"
    wrangler_installed = wrangler_path.is_file()
    wrangler_version = None
    if node_ready and wrangler_installed:
        wrangler_version = _run_version([str(node), str(wrangler_path), "--version"], runner=runner)
    wrangler_ready = wrangler_version == EXPECTED_WRANGLER_VERSION
    network_ready: Optional[bool] = None
    if check_network:
        try:
            network_ready = bool(network_probe())
        except OSError:
            network_ready = False

    failure_codes: list[str] = []
    if missing:
        failure_codes.append("relay_resources_missing")
    if not node_ready:
        failure_codes.append("node_22_required")
    if not npm:
        failure_codes.append("npm_missing")
    if not lock["exact"]:
        failure_codes.append("wrangler_lock_mismatch")
    if require_wrangler and not wrangler_installed:
        failure_codes.append("wrangler_missing")
    elif require_wrangler and node_ready and not wrangler_ready:
        failure_codes.append("wrangler_version_mismatch")
    if check_network and not network_ready:
        failure_codes.append("npm_registry_unreachable")

    state = "ready" if not failure_codes else "prerequisite_missing"
    return {
        "state": state,
        "runtime_source": "injected",
        "development_runtime_ready": False,
        "bundled_runtime_ready": False,
        "runtime_root": None,
        "relay_resources_present": not missing,
        "missing_resources": missing,
        "node_available": bool(node),
        "node_version": node_version,
        "node_major": node_major,
        "node_22_or_newer": node_ready,
        "npm_available": bool(npm),
        "network_checked": check_network,
        "npm_registry_reachable": network_ready,
        "wrangler_lock_present": lock["present"],
        "wrangler_lock_exact": lock["exact"],
        "wrangler_expected_version": EXPECTED_WRANGLER_VERSION,
        "wrangler_installed": wrangler_installed,
        "wrangler_version": wrangler_version,
        "wrangler_ready": wrangler_ready,
        "failure_codes": failure_codes,
        "external_changes_enabled": state == "ready",
    }


def bundled_runtime_status(
    *,
    root: Optional[Path] = None,
    runner: Callable[..., Any] = subprocess.run,
    system: Callable[[], str] = platform.system,
    machine: Callable[[], str] = platform.machine,
) -> dict[str, Any]:
    """UI診断用にruntime自己検証と現在appのbindingを一組で確認する。"""

    relay = Path(root) if root is not None else relay_root()
    development, development_failure = _resolve_development_preflight_runtime(
        relay, runner=runner
    )
    if development_failure:
        result = _development_preflight_result(
            missing=[
                name
                for name in REQUIRED_RELAY_FILES
                if not (relay / name).is_file()
            ],
            lock=_lock_contract(relay),
            failure_code=development_failure,
        )
        return {**result, "app_runtime_binding_ready": False}
    if development is not None:
        result = distribution_preflight(
            root=relay,
            runner=runner,
            check_network=False,
            require_wrangler=True,
            use_bundled_runtime=True,
            _resolved_runtime=development,
        )
        return {**result, "app_runtime_binding_ready": result.get("state") == "ready"}
    if system().lower() != "windows" or machine().lower() not in {"amd64", "x86_64"}:
        return {
            "state": "unsupported_platform",
            "runtime_source": "bundled",
            "development_runtime_ready": False,
            "bundled_runtime_ready": False,
            "failure_codes": ["unsupported_platform"],
            "external_changes_enabled": False,
        }
    result = distribution_preflight(
        root=relay,
        runner=runner,
        check_network=False,
        require_wrangler=True,
        use_bundled_runtime=True,
    )
    failure_codes = list(result.get("failure_codes") or [])
    if "bundled_runtime_unavailable" in failure_codes:
        install_root = _runtime_install_root(relay)
        if not protected_update_host_ready(install_root):
            failure_codes.append("legacy_update_host_migration_required")
            result = {
                **result,
                "state": "legacy_update_host_migration_required",
                "failure_codes": failure_codes,
            }
        else:
            runtime_root = install_root / "runtime"
            if not runtime_root.exists() and not runtime_root.is_symlink():
                failure_codes.append("bundled_runtime_missing")
                result = {
                    **result,
                    "state": "runtime_bootstrap_required",
                    "failure_codes": failure_codes,
                }
    return {**result, "app_runtime_binding_ready": result.get("state") == "ready"}


def prepare_wrangler(
    *,
    confirmed: bool,
    root: Optional[Path] = None,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], Optional[str]] = shutil.which,
    network_probe: Callable[[], bool] = _registry_reachable,
    use_bundled_runtime: bool = True,
) -> dict[str, Any]:
    """明示確認後に専用runtimeを検証する。開発注入時だけ旧npm手順を使う。"""

    if not confirmed:
        raise LiteCloudSetupError(
            "Lite専用runtimeの準備確認が必要です。",
            failure_code=(
                "runtime_prepare_confirmation_required"
                if use_bundled_runtime
                else "npm_ci_confirmation_required"
            ),
        )
    relay = Path(root) if root is not None else relay_root()
    bundled = use_bundled_runtime
    if bundled:
        result = distribution_preflight(
            root=relay,
            runner=runner,
            check_network=False,
            require_wrangler=True,
            use_bundled_runtime=True,
        )
        if result["state"] != "ready":
            failure_codes = list(result.get("failure_codes") or [])
            raise LiteCloudSetupError(
                "Lite専用runtimeを確認できません。準備ツールの修復を実行してください。",
                failure_code=failure_codes[0] if failure_codes else "bundled_runtime_unavailable",
            )
        return {
            **result,
            "runtime_managed": result.get("runtime_source") == "bundled",
            "npm_ci_completed": False,
            "npm_audit_high_clear": True,
        }

    before = distribution_preflight(
        root=relay,
        runner=runner,
        which=which,
        network_probe=network_probe,
        check_network=True,
        require_wrangler=False,
        use_bundled_runtime=False,
    )
    if before["state"] != "ready":
        raise LiteCloudSetupError(
            "Node.js 22以上、npm、lockfile、relay資源、ネットワークを先に準備してください。",
            failure_code="npm_ci_prerequisite_missing",
        )
    npm = which("npm")
    assert npm is not None  # preflightで確認済み
    try:
        installed = runner(
            [npm, "ci"],
            cwd=str(relay),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiteCloudSetupError(
            "準備ツールをインストールできませんでした。",
            failure_code="npm_ci_execution_failed",
        ) from exc
    if int(getattr(installed, "returncode", 1)) != 0:
        raise LiteCloudSetupError(
            "lockfile固定の準備ツールをインストールできませんでした。",
            failure_code="npm_ci_failed",
        )

    after = distribution_preflight(
        root=relay,
        runner=runner,
        which=which,
        network_probe=network_probe,
        check_network=True,
        require_wrangler=True,
        use_bundled_runtime=False,
    )
    if after["state"] != "ready":
        raise LiteCloudSetupError(
            "インストール後の固定Wrangler検査に失敗しました。",
            failure_code="wrangler_postinstall_validation_failed",
        )
    try:
        audit = runner(
            [npm, "audit", "--audit-level=high", "--json"],
            cwd=str(relay),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiteCloudSetupError(
            "依存関係の安全監査を実行できませんでした。",
            failure_code="npm_audit_execution_failed",
        ) from exc
    if int(getattr(audit, "returncode", 1)) != 0:
        raise LiteCloudSetupError(
            "high以上の監査項目があるため外部変更を開始できません。",
            failure_code="npm_audit_high_failed",
        )
    return {**after, "npm_ci_completed": True, "npm_audit_high_clear": True}


def start_cloudflare_login(
    *,
    confirmed: bool,
    root: Optional[Path] = None,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], Optional[str]] = shutil.which,
    node_command: str | Path | None = None,
    use_bundled_runtime: bool = True,
    replace_existing_connection: bool = False,
) -> dict[str, Any]:
    """明示確認後に固定Wranglerの公式OAuthログインを開始する。"""

    if not confirmed:
        raise LiteCloudSetupError(
            "Cloudflareへの接続にはユーザー確認が必要です。",
            failure_code="cloudflare_authentication_required",
        )
    relay = Path(root) if root is not None else relay_root()
    bundled = use_bundled_runtime
    resolved_runtime: LiteRuntimePaths | LiteDevelopmentRuntimePaths | None = None
    resolved_node: str | None = None
    resolved_wrangler: Path | None = None
    if bundled:
        resolved_node, resolved_wrangler, _wrangler_cli, resolved_runtime = _command_runtime(
            relay, node_command=None, runner=runner
        )
    preflight = distribution_preflight(
        root=relay,
        runner=runner,
        which=which,
        check_network=False,
        require_wrangler=True,
        use_bundled_runtime=bundled,
        _resolved_runtime=resolved_runtime,
    )
    if preflight["state"] != "ready":
        failure_codes = list(preflight.get("failure_codes") or [])
        raise LiteCloudSetupError(
            "Lite専用runtimeを確認できません。準備ツールの修復を実行してください。",
            failure_code=(
                failure_codes[0]
                if failure_codes
                else ("bundled_runtime_unavailable" if bundled else "npm_ci_prerequisite_missing")
            ),
        )
    if bundled:
        assert resolved_node is not None and resolved_wrangler is not None
        node, wrangler_path = resolved_node, resolved_wrangler
    else:
        node = str(node_command or which("node") or "")
        wrangler_path = relay / "node_modules" / "wrangler" / "bin" / "wrangler.js"
    if replace_existing_connection:
        try:
            logout = runner(
                [str(node), str(wrangler_path), "logout"],
                cwd=str(relay),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LiteCloudSetupError(
                "現在のCloudflare接続を切り替えられませんでした。",
                failure_code="cloudflare_logout_failed",
            ) from exc
        if int(getattr(logout, "returncode", 1)) != 0:
            raise LiteCloudSetupError(
                "現在のCloudflare接続を切り替えられませんでした。",
                failure_code="cloudflare_logout_failed",
            )
    try:
        result = runner(
            [str(node), str(wrangler_path), "login"],
            cwd=str(relay),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LiteCloudSetupError(
            "Cloudflareへの接続待ちが終了しました。登録・ログインを済ませてから、"
            "「別の／新しいアカウントで接続」をもう一度押してください。",
            failure_code="cloudflare_authentication_required",
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiteCloudSetupError(
            "Cloudflareのログイン画面を開始できませんでした。",
            failure_code="cloudflare_authentication_required",
        ) from exc
    if int(getattr(result, "returncode", 1)) != 0:
        raise LiteCloudSetupError(
            "Cloudflareへの接続が完了しませんでした。ブラウザで許可してから、もう一度お試しください。",
            failure_code="cloudflare_authentication_required",
        )
    return {"state": "authenticated", "authenticated": True}


def secret_bootstrap_contract(*, worker_exists: bool) -> dict[str, Any]:
    """固定Wrangler 4.149.0で成立するSecret bootstrap契約を返す。"""

    if worker_exists:
        return {
            "state": "ready",
            "failure_code": None,
            "worker_exists": True,
            "versions_upload_unpublished": True,
            "versions_secret_put_stdin": True,
            "explicit_version_deployment": True,
            "plaintext_secret_file_required": False,
            "external_changes_enabled": True,
        }
    return {
        "state": "ready",
        "failure_code": None,
        "worker_exists": False,
        "versions_upload_unpublished": True,
        "versions_secret_put_stdin": False,
        "explicit_version_deployment": True,
        "plaintext_secret_file_required": False,
        "secret_transport": "stdin_virtual_file_preload",
        "wrangler_version": EXPECTED_WRANGLER_VERSION,
        "external_changes_enabled": True,
    }


def initial_setup_technical_gate(**preflight_kwargs: Any) -> dict[str, Any]:
    """preflightと新規Worker Secret契約を統合し、後続操作の可否を返す。"""

    preflight = distribution_preflight(**preflight_kwargs)
    contract = secret_bootstrap_contract(worker_exists=False)
    if preflight["state"] != "ready":
        return {
            **preflight,
            "preflight_state": preflight["state"],
            "secret_bootstrap_state": contract["state"],
            "external_changes_enabled": False,
        }
    return {
        **preflight,
        "state": contract["state"],
        "preflight_state": "ready",
        "secret_bootstrap_state": contract["state"],
        "failure_codes": [],
        "external_changes_enabled": True,
    }


def build_initial_secret_upload_command(
    *,
    root: Optional[Path] = None,
    node_command: str | Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
    config_name: str = "wrangler.phase2.jsonc",
    dry_run: bool = False,
    version_tag: str = "",
) -> list[str]:
    """stdin-backed仮想Secret読込を使う固定Wranglerのuploadコマンドを作る。"""

    relay = Path(root) if root is not None else relay_root()
    preload = relay / "scripts" / "wrangler-secret-stdin-preload.cjs"
    node_command, _wrangler, wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    virtual_secret_path = relay / VIRTUAL_SECRET_FILE_NAME
    command = [
        node_command,
        "--require",
        str(preload),
        str(wrangler_cli),
        "versions",
        "upload",
        "--config",
        str(relay / config_name),
        "--secrets-file",
        str(virtual_secret_path),
    ]
    if version_tag:
        if not re.fullmatch(r"nexus-ark-setup-[a-f0-9]{12}", version_tag):
            raise LiteCloudSetupError(
                "初回versionの照合タグが不正です。",
                failure_code="operation_record_schema_invalid",
            )
        command.extend(["--tag", version_tag])
    if dry_run:
        command.append("--dry-run")
    return command


def run_initial_secret_transport_synthetic_gate(
    *,
    runner: Callable[..., Any],
    secrets: Mapping[str, str],
    root: Optional[Path] = None,
    node_command: str | Path | None = "node",
    config_name: str = "wrangler.phase2.jsonc",
) -> dict[str, Any]:
    """合成canaryで仮想Secret uploadのdry-runと非露出を検査する。"""

    if runner is subprocess.run:
        raise LiteCloudSetupError(
            "技術ゲートは注入した合成runnerでだけ実行できます。",
            failure_code="synthetic_execution_required",
        )
    if tuple(secrets.keys()) != REQUIRED_BOOTSTRAP_SECRETS:
        raise LiteCloudSetupError(
            "合成Secretの名前が必須3件と一致しません。",
            failure_code="synthetic_secret_names_invalid",
        )
    secret_values = tuple(str(value) for value in secrets.values())
    if any(not value for value in secret_values) or len(set(secret_values)) != len(secret_values):
        raise LiteCloudSetupError(
            "合成Secretは空でなく、それぞれ異なる値にしてください。",
            failure_code="synthetic_secret_values_invalid",
        )

    relay = Path(root) if root is not None else relay_root()
    virtual_secret_path = relay / VIRTUAL_SECRET_FILE_NAME
    if virtual_secret_path.exists():
        raise LiteCloudSetupError(
            "仮想Secretパスに実ファイルが存在します。",
            failure_code="virtual_secret_path_exists",
        )
    before_files = {
        path.relative_to(relay).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in relay.rglob("*")
        if path.is_file()
    }
    command = build_initial_secret_upload_command(
        root=relay,
        node_command=node_command,
        runner=runner,
        config_name=config_name,
        dry_run=True,
    )
    secret_input = json.dumps(dict(secrets), ensure_ascii=False, separators=(",", ":"))
    result = runner(
        command,
        input=secret_input,
        cwd=str(relay),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    if int(getattr(result, "returncode", 1)) != 0:
        raise LiteCloudSetupError(
            "stdin-backed Secret upload dry-runに失敗しました。",
            failure_code="virtual_secret_upload_dry_run_failed",
        )
    after_files = {
        path.relative_to(relay).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in relay.rglob("*")
        if path.is_file()
    }
    serialized_command = json.dumps(command, ensure_ascii=False)
    if any(value in serialized_command or value in stdout or value in stderr for value in secret_values):
        raise LiteCloudSetupError(
            "合成Secretがargvまたは出力へ露出しました。",
            failure_code="synthetic_secret_exposure_detected",
        )
    if virtual_secret_path.exists():
        raise LiteCloudSetupError(
            "Secret transportが平文ファイルを生成しました。",
            failure_code="virtual_secret_file_created",
        )
    changed_files = [
        relay / relative
        for relative, fingerprint in after_files.items()
        if before_files.get(relative) != fingerprint
    ]
    secret_bytes = tuple(value.encode("utf-8") for value in secret_values)
    for path in changed_files:
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if any(value in content for value in secret_bytes):
            raise LiteCloudSetupError(
                "合成Secretが生成物へ露出しました。",
                failure_code="synthetic_secret_file_exposure_detected",
            )
    return {
        "state": "ready",
        "secret_transport": "stdin_virtual_file_preload",
        "wrangler_version": EXPECTED_WRANGLER_VERSION,
        "secret_names": list(REQUIRED_BOOTSTRAP_SECRETS),
        "plaintext_secret_file_created": False,
        "deployment_created": False,
        "dry_run": True,
        "external_changes_enabled": True,
    }


def _synthetic_version_id(result: Any, fallback: str) -> str:
    explicit = getattr(result, "version_id", None)
    if explicit:
        return str(explicit)
    try:
        parsed = json.loads(str(getattr(result, "stdout", "") or "{}"))
    except ValueError:
        return fallback
    return str(parsed.get("version_id") or fallback)


def run_secret_bootstrap_synthetic_dry_run(
    *,
    runner: Callable[..., Any],
    secrets: Mapping[str, str],
    root: Optional[Path] = None,
    node_command: str | Path | None = "node",
    config_name: str = "wrangler.phase2.example.jsonc",
) -> dict[str, Any]:
    """合成runnerだけでversion→Secret→deploymentの順序と漏えいを検査する。

    runnerにsubprocess.runの既定値を持たせないことで、実Cloudflare APIへの誤接続を防ぐ。
    """

    if tuple(secrets.keys()) != REQUIRED_BOOTSTRAP_SECRETS:
        raise LiteCloudSetupError(
            "合成Secretの名前が必須3件と一致しません。",
            failure_code="synthetic_secret_names_invalid",
        )
    secret_values = tuple(str(value) for value in secrets.values())
    if any(not value for value in secret_values) or len(set(secret_values)) != len(secret_values):
        raise LiteCloudSetupError(
            "合成Secretは空でなく、それぞれ異なる値にしてください。",
            failure_code="synthetic_secret_values_invalid",
        )

    relay = Path(root) if root is not None else relay_root()
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    config = relay / config_name
    common = ["--config", str(config)]
    commands: list[list[str]] = []
    safe_outputs: list[str] = []

    upload = [node_command, str(wrangler), "versions", "upload", *common]
    commands.append(upload)
    result = runner(
        upload,
        input=None,
        cwd=str(relay),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if int(getattr(result, "returncode", 1)) != 0:
        raise LiteCloudSetupError(
            "合成version uploadに失敗しました。",
            failure_code="synthetic_versions_upload_failed",
        )
    version_id = _synthetic_version_id(result, "synthetic-upload-version")
    safe_outputs.extend([str(getattr(result, "stdout", "") or ""), str(getattr(result, "stderr", "") or "")])

    for index, (name, value) in enumerate(secrets.items(), start=1):
        command = [node_command, str(wrangler), "versions", "secret", "put", name, *common]
        commands.append(command)
        result = runner(
            command,
            input=value + "\n",
            cwd=str(relay),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if int(getattr(result, "returncode", 1)) != 0:
            raise LiteCloudSetupError(
                "合成version Secret登録に失敗しました。",
                failure_code="synthetic_versions_secret_put_failed",
            )
        version_id = _synthetic_version_id(result, f"synthetic-secret-version-{index}")
        safe_outputs.extend([str(getattr(result, "stdout", "") or ""), str(getattr(result, "stderr", "") or "")])

    deployment = [
        node_command,
        str(wrangler),
        "versions",
        "deploy",
        f"{version_id}@100%",
        "--yes",
        "--dry-run",
        *common,
    ]
    commands.append(deployment)
    result = runner(
        deployment,
        input=None,
        cwd=str(relay),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if int(getattr(result, "returncode", 1)) != 0:
        raise LiteCloudSetupError(
            "合成version deployment dry-runに失敗しました。",
            failure_code="synthetic_versions_deploy_failed",
        )
    safe_outputs.extend([str(getattr(result, "stdout", "") or ""), str(getattr(result, "stderr", "") or "")])

    serialized_commands = json.dumps(commands, ensure_ascii=False)
    serialized_outputs = "\n".join(safe_outputs)
    if any(value in serialized_commands or value in serialized_outputs for value in secret_values):
        raise LiteCloudSetupError(
            "合成Secretがargvまたは出力へ露出しました。",
            failure_code="synthetic_secret_exposure_detected",
        )

    operation_record = {
        "state": "synthetic_dry_run_completed",
        "completed_steps": [
            "versions_upload",
            "versions_secret_put",
            "versions_deploy_dry_run",
        ],
        "secret_names": list(REQUIRED_BOOTSTRAP_SECRETS),
        "final_version_id": version_id,
        "deployment_created": False,
    }
    serialized_record = json.dumps(operation_record, ensure_ascii=False)
    if any(value in serialized_record for value in secret_values):
        raise LiteCloudSetupError(
            "合成Secretが操作記録へ露出しました。",
            failure_code="synthetic_secret_record_exposure_detected",
        )
    return {
        "state": "synthetic_dry_run_completed",
        "commands": commands,
        "operation_record": operation_record,
        "secret_transport": "stdin",
        "temporary_secret_file_created": False,
        "deployment_created": False,
    }


def _json_output(result: Any) -> Any:
    try:
        return json.loads(str(getattr(result, "stdout", "") or ""))
    except (TypeError, ValueError):
        return None


def _structured_error_codes(result: Any) -> set[int]:
    """Wranglerエラーに含まれるCloudflareの数値codeだけを抽出する。"""

    values: list[Any] = []
    for stream_name in ("stdout", "stderr"):
        try:
            values.append(json.loads(str(getattr(result, stream_name, "") or "")))
        except (TypeError, ValueError):
            continue
    codes: set[int] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "code":
                    try:
                        codes.add(int(item))
                    except (TypeError, ValueError):
                        pass
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for value in values:
        visit(value)
    # Wrangler 4.149.0は--json指定時も「Workerなし」を装飾済みstderrへ
    # `[code: 10007]` として出す。メッセージ本文には依存せず数値markerだけ読む。
    stderr = str(getattr(result, "stderr", "") or "")
    codes.update(int(value) for value in re.findall(r"\[code:\s*(\d+)\]", stderr))
    return codes


def _run_read_only_command(
    command: list[str],
    *,
    runner: Callable[..., Any],
    cwd: Path,
    timeout: int = 60,
    env: Optional[Mapping[str, str]] = None,
) -> Any:
    try:
        return runner(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **({"env": dict(env)} if env is not None else {}),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiteCloudSetupError(
            "Cloudflareの準備状態を読み取れませんでした。",
            failure_code="cloudflare_inventory_failed",
        ) from exc


def _safe_accounts(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    accounts: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        account_id = str(item.get("id") or "").strip()
        if not account_id:
            continue
        accounts.append({"id": account_id, "name": str(item.get("name") or "").strip()})
    return accounts


def _safe_manually_confirmed_account(value: Any) -> Optional[dict[str, str]]:
    """公式Dashboardで照合済みのaccountだけを手動復旧候補として受け付ける。"""

    if not isinstance(value, Mapping):
        return None
    account_id = str(value.get("id") or "").strip().lower()
    name = str(value.get("name") or "").strip()
    if not _CLOUDFLARE_ACCOUNT_ID_PATTERN.fullmatch(account_id):
        return None
    if not name or len(name) > 100:
        return None
    return {"id": account_id, "name": name}


def _safe_d1(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {"id": str(item.get("uuid") or item.get("id") or "").strip(), "name": str(item.get("name") or "").strip()}
        for item in value
        if isinstance(item, Mapping) and (item.get("uuid") or item.get("id"))
    ]


def _safe_kv(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {"id": str(item.get("id") or "").strip(), "name": str(item.get("title") or item.get("name") or "").strip()}
        for item in value
        if isinstance(item, Mapping) and item.get("id")
    ]


def classify_resource_inventory(
    *,
    account: Mapping[str, Any],
    d1_databases: list[Mapping[str, Any]],
    kv_namespaces: list[Mapping[str, Any]],
    workers: list[Mapping[str, Any]],
    resource_name: str = DEFAULT_RESOURCE_NAME,
    expected: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """account内の候補資源を、名前だけで暗黙採用せず4分類する。"""

    if not _RESOURCE_NAME_PATTERN.fullmatch(str(resource_name or "")):
        raise LiteCloudSetupError(
            "Lite用クラウドの資源名が不正です。",
            failure_code="resource_name_invalid",
        )
    expected = expected if isinstance(expected, Mapping) else {}
    expected_d1_id = str(expected.get("d1_id") or "")
    expected_kv_id = str(expected.get("kv_id") or "")
    d1_name_matches = [
        dict(item) for item in d1_databases if str(item.get("name") or "") == resource_name
    ]
    kv_name_matches = [
        dict(item) for item in kv_namespaces if str(item.get("name") or "") == resource_name
    ]
    # 新規候補の衝突診断は名前、既存取込は保存済みresource IDを正本にする。
    # D1／KVの実資源名がWorker名と異なっていても、IDが一致すれば暗黙推測せず
    # 選択済み資源として扱える。
    d1_matches = (
        [dict(item) for item in d1_databases if str(item.get("id") or "") == expected_d1_id]
        if expected_d1_id
        else d1_name_matches
    )
    kv_matches = (
        [dict(item) for item in kv_namespaces if str(item.get("id") or "") == expected_kv_id]
        if expected_kv_id
        else kv_name_matches
    )
    worker_matches = [dict(item) for item in workers if str(item.get("name") or "") == resource_name]
    matches = {"d1": d1_matches, "kv": kv_matches, "worker": worker_matches}

    duplicate = any(len(items) > 1 for items in matches.values())
    mismatch = False
    expected_account_id = str(expected.get("account_id") or "")
    if expected_account_id and expected_account_id != str(account.get("id") or ""):
        mismatch = True
    if expected_d1_id and not d1_matches and d1_name_matches:
        mismatch = True
    if expected_kv_id and not kv_matches and kv_name_matches:
        mismatch = True
    expected_deployment_id = str(expected.get("worker_deployment_id") or "")
    if (
        expected_deployment_id
        and worker_matches
        and expected_deployment_id != str(worker_matches[0].get("deployment_id") or "")
    ):
        mismatch = True

    present_count = sum(bool(items) for items in matches.values())
    if duplicate or mismatch:
        classification = "resource_collision"
        state = "resource_collision"
        failure_code = "resource_collision_detected"
    elif present_count == 0:
        classification = "unset"
        state = "mode_selected"
        failure_code = None
    elif present_count == 3:
        classification = "existing"
        state = "resource_plan_ready"
        failure_code = None
    else:
        classification = "partial_resources"
        state = "partial_resources"
        failure_code = "partial_resources_detected"

    return {
        "state": state,
        "classification": classification,
        "failure_code": failure_code,
        "account": {"id": str(account.get("id") or ""), "name": str(account.get("name") or "")},
        "resource_name": resource_name,
        "resources": {
            key: (items[0] if len(items) == 1 else None) for key, items in matches.items()
        },
        "match_counts": {key: len(items) for key, items in matches.items()},
        "external_changes_enabled": False,
        "secret_gate_state": "ready",
    }


def read_only_cloudflare_diagnostics(
    *,
    selected_account_id: Optional[str] = None,
    resource_name: str = DEFAULT_RESOURCE_NAME,
    expected: Optional[Mapping[str, Any]] = None,
    root: Optional[Path] = None,
    runner: Callable[..., Any] = subprocess.run,
    node_command: str | Path | None = None,
    manually_confirmed_account: Optional[Mapping[str, Any]] = None,
    require_empty_storage: bool = False,
) -> dict[str, Any]:
    """Wranglerの構造化出力だけを使い、Cloudflareを読み取り専用診断する。"""

    relay = Path(root) if root is not None else relay_root()
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    base = [node_command, str(wrangler)]
    whoami_result = _run_read_only_command([*base, "whoami", "--json"], runner=runner, cwd=relay)
    whoami = _json_output(whoami_result)
    if int(getattr(whoami_result, "returncode", 1)) != 0 or not isinstance(whoami, Mapping) or not whoami.get("loggedIn"):
        return {
            "state": "authentication_required",
            "failure_code": "cloudflare_authentication_required",
            "accounts": [],
            "external_changes_enabled": False,
        }
    accounts = _safe_accounts(whoami.get("accounts"))
    if not selected_account_id:
        return {
            "state": "account_confirmation_required",
            "failure_code": "account_selection_required",
            "accounts": accounts,
            "external_changes_enabled": False,
        }
    selected_account_id = str(selected_account_id or "").strip().lower()
    selected = next((item for item in accounts if item["id"] == selected_account_id), None)
    manual_account = _safe_manually_confirmed_account(manually_confirmed_account)
    manual_account_used = False
    # Wranglerが認証済みなのに候補を0件で返す場合だけ、公式Dashboardで本人が
    # 照合したaccountを復旧候補にできる。候補が1件でもある時の迂回には使わない。
    if selected is None and not accounts and manual_account is not None:
        if manual_account["id"] == selected_account_id:
            selected = manual_account
            accounts = [manual_account]
            manual_account_used = True
    if selected is None:
        return {
            "state": "account_confirmation_required",
            "failure_code": "account_changed",
            "accounts": accounts,
            "external_changes_enabled": False,
        }

    # Wrangler 4.149.0 の D1／KV／deployments list は --account-id を受け付けない。
    # OAuthが複数accountへ到達できる場合も選択済みIDを曖昧にしないよう、Wranglerの
    # 公式account選択環境変数を子プロセスだけへ渡す。
    account_env = dict(os.environ)
    account_env["CLOUDFLARE_ACCOUNT_ID"] = selected_account_id
    d1_result = _run_read_only_command(
        [*base, "d1", "list", "--json"], runner=runner, cwd=relay, env=account_env
    )
    kv_result = _run_read_only_command(
        [*base, "kv", "namespace", "list"], runner=runner, cwd=relay, env=account_env
    )
    worker_result = _run_read_only_command(
        [*base, "deployments", "list", "--name", resource_name, "--json"],
        runner=runner,
        cwd=relay,
        env=account_env,
    )
    if int(getattr(d1_result, "returncode", 1)) != 0 or int(getattr(kv_result, "returncode", 1)) != 0:
        raise LiteCloudSetupError(
            "D1またはKVの一覧を読み取れませんでした。",
            failure_code="cloudflare_inventory_failed",
        )
    if int(getattr(worker_result, "returncode", 1)) == 0:
        deployments = _json_output(worker_result) or []
        latest = deployments[-1] if isinstance(deployments, list) and deployments else {}
        versions = latest.get("versions") if isinstance(latest, Mapping) else []
        version = versions[0] if isinstance(versions, list) and versions else {}
        workers = [
            {
                "name": resource_name,
                "deployment_id": str(latest.get("id") or "") if isinstance(latest, Mapping) else "",
                "version_id": str(version.get("version_id") or "") if isinstance(version, Mapping) else "",
            }
        ]
    elif bool(getattr(worker_result, "not_found", False)) or _structured_error_codes(worker_result) & {10007, 10090}:
        workers = []
    else:
        raise LiteCloudSetupError(
            "候補Workerの状態を読み取れませんでした。",
            failure_code="cloudflare_inventory_failed",
        )
    d1_databases = _safe_d1(_json_output(d1_result))
    kv_namespaces = _safe_kv(_json_output(kv_result))
    result = classify_resource_inventory(
        account=selected,
        d1_databases=d1_databases,
        kv_namespaces=kv_namespaces,
        workers=workers,
        resource_name=resource_name,
        expected=expected,
    )
    result["accounts"] = accounts
    result["manual_account_confirmation_used"] = manual_account_used
    result["inventory_counts"] = {
        "d1": len(d1_databases),
        "kv": len(kv_namespaces),
        "target_worker": len(workers),
    }
    if require_empty_storage and (d1_databases or kv_namespaces or workers):
        result.update(
            {
                "state": "resource_collision",
                "classification": "resource_collision",
                "failure_code": "resource_collision_detected",
                "external_changes_enabled": False,
            }
        )
    result["checked_commands"] = ["whoami", "d1_list", "kv_namespace_list", "worker_deployments_list"]
    return result


def build_resource_plan(*, mode: str, diagnostic: Mapping[str, Any]) -> dict[str, Any]:
    """読み取り結果から新規／取込計画を作る。外部変更は常に無効のまま。"""

    if mode not in SETUP_MODES:
        raise LiteCloudSetupError("方式を選んでください。", failure_code="operation_record_schema_invalid")
    classification = str(diagnostic.get("classification") or "")
    if mode == "new" and classification != "unset":
        raise LiteCloudSetupError(
            "既存または部分的な資源があるため、新規作成計画には進めません。",
            failure_code="new_plan_requires_unset_resources",
        )
    if mode == "import" and classification != "existing":
        raise LiteCloudSetupError(
            "3種類の既存資源を確認できないため、取込計画には進めません。",
            failure_code="import_plan_requires_existing_resources",
        )
    account = diagnostic.get("account") if isinstance(diagnostic.get("account"), Mapping) else {}
    resources = diagnostic.get("resources") if isinstance(diagnostic.get("resources"), Mapping) else {}
    resource_name = str(diagnostic.get("resource_name") or DEFAULT_RESOURCE_NAME)
    plan = {
        "state": "resource_plan_ready",
        "mode": mode,
        "account": {"id": str(account.get("id") or ""), "name": str(account.get("name") or "")},
        "worker": resources.get("worker") if mode == "import" else {"name": resource_name},
        "d1": resources.get("d1") if mode == "import" else {"name": resource_name},
        "kv": resources.get("kv") if mode == "import" else {"name": resource_name},
        "bindings": {"d1": DEFAULT_D1_BINDING, "kv": DEFAULT_KV_BINDING},
        "failure_code": None,
        "external_changes_enabled": False,
        "commands": [],
        "notice": "この計画は確認用です。Cloudflare資源は変更されません。",
    }
    plan["plan_digest"] = resource_plan_digest(plan)
    return plan


def _resource_plan_contract(plan: Mapping[str, Any]) -> dict[str, Any]:
    """実行中も変化しない資源計画の識別項目だけを正規化する。"""
    account = plan.get("account") if isinstance(plan.get("account"), Mapping) else {}
    worker = plan.get("worker") if isinstance(plan.get("worker"), Mapping) else {}
    d1 = plan.get("d1") if isinstance(plan.get("d1"), Mapping) else {}
    kv = plan.get("kv") if isinstance(plan.get("kv"), Mapping) else {}
    bindings = plan.get("bindings") if isinstance(plan.get("bindings"), Mapping) else {}
    return {
        "mode": str(plan.get("mode") or ""),
        "account_id": str(account.get("id") or "").strip(),
        "worker_name": str(worker.get("name") or DEFAULT_RESOURCE_NAME).strip(),
        "d1_name": str(d1.get("name") or DEFAULT_RESOURCE_NAME).strip(),
        "kv_name": str(kv.get("name") or DEFAULT_RESOURCE_NAME).strip(),
        "d1_binding": str(bindings.get("d1") or ""),
        "kv_binding": str(bindings.get("kv") or ""),
        "worker_url": str(plan.get("worker_url") or "").strip().rstrip("/"),
    }


def resource_plan_digest(plan: Mapping[str, Any]) -> str:
    contract = _resource_plan_contract(plan)
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def restore_uncreated_worker_plan_name(operation: Mapping[str, Any]) -> dict[str, Any]:
    """旧checkpointで欠落した未作成Worker名を、確認済み計画から限定復旧する。

    Worker名を単に既定値で補うのではなく、保存済みdigest、D1／KV名、bindings、
    workers.dev URLが同じ固定名を示す場合だけ復旧する。Workerまたはversionの作成完了後は
    名前を推測せず停止する。
    """

    current = sanitize_setup_operation(operation)
    worker = current.get("worker") if isinstance(current.get("worker"), Mapping) else {}
    worker_name = str(worker.get("name") or "").strip()
    if worker_name:
        return current

    completed_steps = set(current.get("completed_steps") or [])
    recovery_states = {
        "resource_plan_ready",
        "resources_creating",
        "partial_resources",
        "resources_ready",
        "local_config_ready",
        "bootstrap_secrets_ready",
        "migrated",
        "worker_container_required",
        "worker_container_reconciliation_required",
    }
    d1 = current.get("d1") if isinstance(current.get("d1"), Mapping) else {}
    kv = current.get("kv") if isinstance(current.get("kv"), Mapping) else {}
    bindings = current.get("bindings") if isinstance(current.get("bindings"), Mapping) else {}
    account = current.get("account") if isinstance(current.get("account"), Mapping) else {}
    saved_digest = str(current.get("resource_plan_digest") or "").strip()
    unsafe_completed_steps = {
        "worker_container_created",
        "initial_version_upload_requested",
        "initial_version_uploaded",
        "deployment_created",
    }

    try:
        validated_url = _validated_worker_url(
            str(current.get("worker_url") or ""), DEFAULT_RESOURCE_NAME
        )
    except LiteCloudSetupError as exc:
        raise LiteCloudSetupError(
            "保存済みのWorker計画名を安全に復旧できません。外部操作を行わず停止しました。",
            failure_code="resource_plan_confirmation_mismatch",
        ) from exc

    if (
        current.get("mode") != "new"
        or current.get("state") not in recovery_states
        or not str(account.get("id") or "").strip()
        or str(d1.get("name") or "").strip() != DEFAULT_RESOURCE_NAME
        or not str(d1.get("id") or "").strip()
        or str(kv.get("name") or "").strip() != DEFAULT_RESOURCE_NAME
        or not str(kv.get("id") or "").strip()
        or bindings.get("d1") != DEFAULT_D1_BINDING
        or bindings.get("kv") != DEFAULT_KV_BINDING
        or not saved_digest
        or resource_plan_digest(current) != saved_digest
        or unsafe_completed_steps & completed_steps
        or current.get("version_id")
        or validated_url != str(current.get("worker_url") or "").strip().rstrip("/")
    ):
        raise LiteCloudSetupError(
            "保存済みのWorker計画名を安全に復旧できません。外部操作を行わず停止しました。",
            failure_code="resource_plan_confirmation_mismatch",
        )

    current["worker"] = {"name": DEFAULT_RESOURCE_NAME}
    if resource_plan_digest(current) != saved_digest:
        raise LiteCloudSetupError(
            "復旧したWorker計画が確認済み計画と一致しません。外部操作を行わず停止しました。",
            failure_code="resource_plan_confirmation_mismatch",
        )
    return current


def create_resource_operation_from_plan(
    plan: Mapping[str, Any], *, metadata_root: Optional[Path] = None
) -> dict[str, Any]:
    """Package 2の確認計画を秘密なし操作記録へ固定する。"""

    mode = str(plan.get("mode") or "")
    if mode not in SETUP_MODES:
        raise LiteCloudSetupError(
            "資源準備の方式が不正です。", failure_code="operation_record_schema_invalid"
        )
    operation = create_setup_operation(mode=mode, metadata_root=metadata_root)
    operation.update(
        {
            "state": "resource_plan_ready",
            "account": plan.get("account") or {},
            "worker": plan.get("worker"),
            "d1": plan.get("d1"),
            "kv": plan.get("kv"),
            "bindings": plan.get("bindings") or {},
            "failure_code": None,
            "completed_steps": ["resource_plan_confirmed"],
            "resource_plan_digest": resource_plan_digest(plan),
            "worker_url": str(plan.get("worker_url") or "").strip().rstrip("/"),
        }
    )
    return save_setup_operation(operation, metadata_root=metadata_root)


def confirm_resource_plan_for_execution(
    plan: Mapping[str, Any],
    *,
    worker_url: str = "",
    metadata_root: Optional[Path] = None,
) -> dict[str, Any]:
    """表示済み資源計画を実行対象operationへ固定する本番用入口。"""
    original_digest = resource_plan_digest(plan)
    supplied = str(plan.get("plan_digest") or original_digest)
    if supplied != original_digest:
        raise LiteCloudSetupError(
            "確認した資源計画の内容が変わりました。もう一度計画を確認してください。",
            failure_code="resource_plan_confirmation_mismatch",
        )
    executable_plan = dict(plan)
    if str(plan.get("mode") or "") == "new":
        account_id = str((plan.get("account") or {}).get("id") or "")
        if not str(worker_url or "").strip():
            dashboard_url = (
                "https://dash.cloudflare.com/"
                f"{quote(account_id, safe='')}/workers-and-pages"
            )
            raise LiteCloudSetupError(
                "Cloudflareでworkers.devサブドメインを有効にし、表示されたLite公開URLを入力してください。"
                f"設定画面: {dashboard_url}",
                failure_code="workers_dev_subdomain_required",
            )
        executable_plan["worker_url"] = _validated_worker_url(
            worker_url,
            str(((plan.get("worker") or {}) if isinstance(plan.get("worker"), Mapping) else {}).get("name") or ""),
        )
    elif worker_url:
        executable_plan["worker_url"] = _validated_worker_url(
            worker_url,
            str(((plan.get("worker") or {}) if isinstance(plan.get("worker"), Mapping) else {}).get("name") or ""),
        )
    executable_plan["plan_digest"] = resource_plan_digest(executable_plan)
    return create_resource_operation_from_plan(executable_plan, metadata_root=metadata_root)


def _validated_worker_url(value: str, worker_name: str) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(url)
    host = str(parsed.hostname or "").lower()
    expected_prefix = f"{str(worker_name or '').strip().lower()}."
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not expected_prefix.strip(".")
        or not host.startswith(expected_prefix)
        or not host.endswith(".workers.dev")
    ):
        raise LiteCloudSetupError(
            "Lite公開URLは確認したWorker名で始まるhttps://…workers.dev形式を入力してください。",
            failure_code="worker_url_invalid",
        )
    return url


def _confirmed_external_operation(
    operation: Mapping[str, Any],
    *,
    confirmed_operation_id: str,
    confirmed_resource_plan_digest: str,
    confirmed_account_id: str,
    allow_external_changes: bool,
) -> dict[str, Any]:
    if not allow_external_changes:
        raise LiteCloudSetupError(
            "Cloudflareへ変更を行う確認が必要です。",
            failure_code="external_changes_confirmation_required",
        )
    current = sanitize_setup_operation(operation)
    if str(confirmed_operation_id or "") != current["operation_id"]:
        raise LiteCloudSetupError(
            "確認した操作IDと実行対象が一致しません。",
            failure_code="operation_confirmation_mismatch",
        )
    saved_digest = str(current.get("resource_plan_digest") or "")
    actual_digest = resource_plan_digest(current)
    if (
        not saved_digest
        or str(confirmed_resource_plan_digest or "") != saved_digest
        or actual_digest != saved_digest
    ):
        raise LiteCloudSetupError(
            "確認した資源計画と実行対象が一致しません。もう一度計画を確認してください。",
            failure_code="resource_plan_confirmation_mismatch",
        )
    account_id = str((current.get("account") or {}).get("id") or "")
    if not account_id or str(confirmed_account_id or "") != account_id:
        raise LiteCloudSetupError(
            "確認したCloudflareアカウントと実行対象が一致しません。",
            failure_code="account_confirmation_mismatch",
        )
    return current


def _account_fixed_runner(
    runner: Callable[..., Any],
    account_id: str,
    *,
    observe: Optional[Callable[[list[str], Any], None]] = None,
) -> Callable[..., Any]:
    """全child processで選択済みaccountを環境変数の正本として固定する。"""

    def run(command: list[str], **kwargs: Any) -> Any:
        safe_command: list[str] = []
        index = 0
        while index < len(command):
            token = str(command[index])
            if token == "--account-id":
                if index + 1 >= len(command) or str(command[index + 1]) != account_id:
                    raise LiteCloudSetupError(
                        "実行コマンドのCloudflareアカウントが確認内容と一致しません。",
                        failure_code="account_confirmation_mismatch",
                    )
                index += 2
                continue
            safe_command.append(token)
            index += 1
        supplied = kwargs.pop("env", None)
        child_env = dict(os.environ)
        if isinstance(supplied, Mapping):
            child_env.update({str(key): str(value) for key, value in supplied.items()})
        child_env["CLOUDFLARE_ACCOUNT_ID"] = account_id
        result = runner(safe_command, env=child_env, **kwargs)
        if observe is not None:
            observe(safe_command, result)
        return result

    return run


def _append_completed_step(operation: dict[str, Any], step: str) -> None:
    completed = operation.setdefault("completed_steps", [])
    if step not in completed:
        completed.append(step)


def _setup_version_tag(operation_id: str) -> str:
    compact = str(operation_id or "").lower().replace("-", "")
    if not re.fullmatch(r"[a-f0-9]{32}", compact):
        raise LiteCloudSetupError(
            "初回セットアップの操作IDから照合タグを作れません。",
            failure_code="operation_record_schema_invalid",
        )
    return f"nexus-ark-setup-{compact[:12]}"


def _save_resource_checkpoint(
    operation: dict[str, Any],
    checkpoint_name: str,
    *,
    metadata_root: Optional[Path],
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]],
) -> dict[str, Any]:
    saved = save_setup_operation(operation, metadata_root=metadata_root)
    operation.clear()
    operation.update(saved)
    if checkpoint is not None:
        checkpoint(checkpoint_name, dict(saved))
    return operation


def _synthetic_json_command(
    command: list[str], *, runner: Callable[..., Any], cwd: Path, failure_code: str
) -> Any:
    try:
        result = runner(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiteCloudSetupError(
            "合成資源一覧を取得できませんでした。", failure_code=failure_code
        ) from exc
    if int(getattr(result, "returncode", 1)) != 0:
        raise LiteCloudSetupError(
            "合成資源一覧を取得できませんでした。", failure_code=failure_code
        )
    parsed = _json_output(result)
    if not isinstance(parsed, list):
        raise LiteCloudSetupError(
            "合成資源一覧の形式が不正です。", failure_code=failure_code
        )
    return parsed


def _confirm_synthetic_account(
    *,
    account_id: str,
    runner: Callable[..., Any],
    base: list[str],
    cwd: Path,
) -> None:
    result = _run_read_only_command([*base, "whoami", "--json"], runner=runner, cwd=cwd)
    parsed = _json_output(result)
    accounts = _safe_accounts(parsed.get("accounts")) if isinstance(parsed, Mapping) else []
    if int(getattr(result, "returncode", 1)) != 0 or account_id not in {item["id"] for item in accounts}:
        raise LiteCloudSetupError(
            "確認時とCloudflareアカウントが異なります。",
            failure_code="account_changed",
        )


def _matching_resource(items: list[dict[str, str]], name: str, *, failure_code: str) -> Optional[dict[str, str]]:
    matches = [item for item in items if item.get("name") == name]
    if len(matches) > 1:
        raise LiteCloudSetupError("同名資源が複数あります。", failure_code=failure_code)
    return matches[0] if matches else None


def _created_resource_id(result: Any, key: str) -> str:
    labels = {
        "d1": (
            "database_id",
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        ),
        "kv": ("id", r"[0-9a-fA-F]{32}"),
    }
    contract = labels.get(key)
    if contract is None:
        return ""
    label, identifier_pattern = contract
    toml_line = re.compile(
        rf'^[ \t]*{label}[ \t]*=[ \t]*"({identifier_pattern})"[ \t]*\r?$',
        re.MULTILINE,
    )
    json_line = re.compile(
        rf'^[ \t]*"{label}"[ \t]*:[ \t]*"({identifier_pattern})"[ \t]*,?[ \t]*\r?$',
        re.MULTILINE,
    )
    candidates: list[str] = []
    for stream_name in ("stdout", "stderr"):
        stream = str(getattr(result, stream_name, "") or "")
        candidates.extend(toml_line.findall(stream))
        candidates.extend(json_line.findall(stream))
    return candidates[0].lower() if len(candidates) == 1 else ""


def provision_resources_synthetic(
    operation: Mapping[str, Any],
    *,
    confirmed_operation_id: str,
    runner: Callable[..., Any],
    root: Optional[Path] = None,
    metadata_root: Optional[Path] = None,
    node_command: str | Path | None = "node",
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
) -> dict[str, Any]:
    """合成runnerだけでD1／KV作成と全中断点の再開契約を検証する。

    Package 1の技術ゲートが閉じている間、実subprocessは明示的に拒否する。
    """

    if runner is subprocess.run:
        raise LiteCloudSetupError(
            "実Cloudflareへの資源作成は安全ゲートにより無効です。",
            failure_code="synthetic_execution_required",
        )
    current = sanitize_setup_operation(operation)
    if str(confirmed_operation_id or "") != current["operation_id"]:
        raise LiteCloudSetupError(
            "確認した操作IDと実行対象が一致しません。",
            failure_code="operation_confirmation_mismatch",
        )
    if current.get("mode") != "new":
        raise LiteCloudSetupError(
            "新規資源準備の操作記録ではありません。",
            failure_code="operation_record_schema_invalid",
        )
    if "resource_plan_confirmed" not in current.get("completed_steps", []):
        raise LiteCloudSetupError(
            "資源作成計画が確認されていません。",
            failure_code="operation_confirmation_mismatch",
        )
    relay = Path(root) if root is not None else relay_root()
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    base = [node_command, str(wrangler)]
    account = current.get("account") or {}
    account_id = str(account.get("id") or "")
    if not account_id:
        raise LiteCloudSetupError(
            "Cloudflareアカウントが未確定です。", failure_code="account_selection_required"
        )
    try:
        _confirm_synthetic_account(
            account_id=account_id, runner=runner, base=base, cwd=relay
        )
    except LiteCloudSetupError as exc:
        current.update(
            state="resource_plan_ready",
            failed_step="account_reconfirm",
            failure_code=exc.failure_code,
        )
        _save_resource_checkpoint(
            current,
            "account_reconfirm_failed",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise
    _append_completed_step(current, "account_reconfirmed")
    current["state"] = "resources_creating"
    _save_resource_checkpoint(
        current,
        "account_reconfirmed",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    account_args = ["--account-id", account_id]

    resource_specs = (
        (
            "d1",
            "d1_create_requested",
            "d1_ready",
            [*base, "d1", "create"],
            [*base, "d1", "list", "--json", *account_args],
            _safe_d1,
            "d1_create_failed",
            "d1_reconciliation_failed",
        ),
        (
            "kv",
            "kv_create_requested",
            "kv_ready",
            [*base, "kv", "namespace", "create"],
            [*base, "kv", "namespace", "list", *account_args],
            _safe_kv,
            "kv_create_failed",
            "kv_reconciliation_failed",
        ),
    )
    for (
        key,
        requested_step,
        ready_step,
        create_prefix,
        list_command,
        sanitizer,
        create_failure,
        reconciliation_failure,
    ) in resource_specs:
        resource = current.get(key) if isinstance(current.get(key), Mapping) else {}
        name = str(resource.get("name") or DEFAULT_RESOURCE_NAME)
        requested_without_id = (
            requested_step in current.get("completed_steps", [])
            and not str(resource.get("id") or "")
            and str(current.get("failed_step") or "")
            in {f"{key}_create", f"{key}_create_response_id", f"{key}_manual_reconciliation"}
        )
        if requested_without_id:
            manual_failure = f"{key}_manual_reconciliation_required"
            current.update(
                state="partial_resources",
                failed_step=f"{key}_manual_reconciliation",
                failure_code=manual_failure,
            )
            _save_resource_checkpoint(
                current,
                f"{key}_manual_reconciliation_required",
                metadata_root=metadata_root,
                checkpoint=checkpoint,
            )
            raise LiteCloudSetupError(
                "作成コマンド開始後の資源IDが不明です。createを再送せず手動確認してください。",
                failure_code=manual_failure,
            )

        def stop_collision(message: str, stage: str) -> None:
            current.update(
                state="resource_collision",
                failed_step=f"{key}_{stage}",
                failure_code="resource_collision_detected",
            )
            _save_resource_checkpoint(
                current,
                f"{key}_{stage}_collision",
                metadata_root=metadata_root,
                checkpoint=checkpoint,
            )
            raise LiteCloudSetupError(
                message, failure_code="resource_collision_detected"
            )

        def load_remote_items(stage: str) -> list[dict[str, str]]:
            try:
                return sanitizer(
                    _synthetic_json_command(
                        list_command,
                        runner=runner,
                        cwd=relay,
                        failure_code=reconciliation_failure,
                    )
                )
            except LiteCloudSetupError:
                current.update(
                    state="partial_resources",
                    failed_step=f"{key}_{stage}",
                    failure_code=reconciliation_failure,
                )
                _save_resource_checkpoint(
                    current,
                    f"{key}_{stage}_failed",
                    metadata_root=metadata_root,
                    checkpoint=checkpoint,
                )
                raise

        remote_items = load_remote_items("list")
        try:
            match = _matching_resource(
                remote_items, name, failure_code="resource_collision_detected"
            )
        except LiteCloudSetupError:
            stop_collision("同名資源が複数あります。", "list")
        saved_id = str(resource.get("id") or "")
        if saved_id:
            if not match or str(match.get("id") or "") != saved_id:
                stop_collision("保存済み資源IDと一覧が一致しません。", "saved_id")
            if ready_step not in current.get("completed_steps", []):
                if requested_step not in current.get("completed_steps", []):
                    stop_collision("未確認の保存済み資源IDを検出しました。", "saved_id")
                current[key] = match
                _append_completed_step(current, ready_step)
                current["failed_step"] = ""
                current["failure_code"] = None
                _save_resource_checkpoint(
                    current,
                    f"{key}_candidate_reconciled",
                    metadata_root=metadata_root,
                    checkpoint=checkpoint,
                )
        elif match:
            if requested_step not in current.get("completed_steps", []):
                stop_collision("未確認の同名資源を検出しました。", "unconfirmed_name")
            manual_failure = f"{key}_manual_reconciliation_required"
            current.update(
                state="partial_resources",
                failed_step=f"{key}_manual_reconciliation",
                failure_code=manual_failure,
            )
            _save_resource_checkpoint(
                current,
                f"{key}_manual_reconciliation_required",
                metadata_root=metadata_root,
                checkpoint=checkpoint,
            )
            raise LiteCloudSetupError(
                "作成応答の資源IDがないため、同名資源を自動採用できません。手動確認してください。",
                failure_code=manual_failure,
            )
        else:
            _append_completed_step(current, requested_step)
            current["failed_step"] = ""
            _save_resource_checkpoint(
                current,
                requested_step,
                metadata_root=metadata_root,
                checkpoint=checkpoint,
            )
            command = [*create_prefix, name, *account_args]
            try:
                created = runner(
                    command,
                    cwd=str(relay),
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                current.update(
                    state="partial_resources", failed_step=f"{key}_create", failure_code=create_failure
                )
                _save_resource_checkpoint(
                    current,
                    f"{key}_create_interrupted",
                    metadata_root=metadata_root,
                    checkpoint=checkpoint,
                )
                raise LiteCloudSetupError(
                    "合成資源作成が中断されました。再開時に一覧を照合します。",
                    failure_code=create_failure,
                ) from exc
            if int(getattr(created, "returncode", 1)) != 0:
                current.update(
                    state="partial_resources", failed_step=f"{key}_create", failure_code=create_failure
                )
                _save_resource_checkpoint(
                    current,
                    f"{key}_create_failed",
                    metadata_root=metadata_root,
                    checkpoint=checkpoint,
                )
                raise LiteCloudSetupError(
                    "合成資源作成に失敗しました。再開時に一覧を照合します。",
                    failure_code=create_failure,
                )
            created_id = _created_resource_id(created, key)
            if not created_id:
                manual_failure = f"{key}_manual_reconciliation_required"
                current.update(
                    state="partial_resources",
                    failed_step=f"{key}_create_response_id",
                    failure_code=manual_failure,
                )
                _save_resource_checkpoint(
                    current,
                    f"{key}_create_response_id_missing",
                    metadata_root=metadata_root,
                    checkpoint=checkpoint,
                )
                raise LiteCloudSetupError(
                    "資源作成応答からIDを確定できません。同名資源を自動採用しません。",
                    failure_code=manual_failure,
                )
            current[key] = {"name": name, "id": created_id}
            _save_resource_checkpoint(
                current,
                f"{key}_create_response_id_saved",
                metadata_root=metadata_root,
                checkpoint=checkpoint,
            )
            remote_items = load_remote_items("post_create_list")
            try:
                match = _matching_resource(
                    remote_items, name, failure_code="resource_collision_detected"
                )
            except LiteCloudSetupError:
                stop_collision("作成後に同名資源が複数あります。", "post_create")
            if not match or str(match.get("id") or "").lower() != created_id:
                current.update(
                    state="partial_resources",
                    failed_step=f"{key}_reconcile",
                    failure_code=reconciliation_failure,
                )
                _save_resource_checkpoint(
                    current,
                    f"{key}_reconciliation_failed",
                    metadata_root=metadata_root,
                    checkpoint=checkpoint,
                )
                raise LiteCloudSetupError(
                    "作成後の合成一覧から資源IDを確定できません。",
                    failure_code=reconciliation_failure,
                )
            current[key] = match
            _append_completed_step(current, ready_step)
            current["failed_step"] = ""
            _save_resource_checkpoint(
                current,
                ready_step,
                metadata_root=metadata_root,
                checkpoint=checkpoint,
            )

    current.update(
        state="resources_ready",
        failed_step="",
        failure_code=None,
    )
    _append_completed_step(current, "resources_ready")
    _save_resource_checkpoint(
        current,
        "resources_ready",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    current["external_changes_enabled"] = False
    return current


def provision_resources(
    operation: Mapping[str, Any],
    *,
    confirmed_operation_id: str,
    confirmed_resource_plan_digest: str,
    confirmed_account_id: str,
    allow_external_changes: bool,
    runner: Callable[..., Any] = subprocess.run,
    root: Optional[Path] = None,
    metadata_root: Optional[Path] = None,
    node_command: str | Path | None = None,
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
) -> dict[str, Any]:
    """確認済み計画だけを使い、実Cloudflare D1／KVを冪等準備する。"""
    current = _confirmed_external_operation(
        operation,
        confirmed_operation_id=confirmed_operation_id,
        confirmed_resource_plan_digest=confirmed_resource_plan_digest,
        confirmed_account_id=confirmed_account_id,
        allow_external_changes=allow_external_changes,
    )
    account_id = str((current.get("account") or {}).get("id") or "")
    fixed_runner = _account_fixed_runner(runner, account_id)
    result = provision_resources_synthetic(
        current,
        confirmed_operation_id=current["operation_id"],
        runner=fixed_runner,
        root=root,
        metadata_root=metadata_root,
        node_command=node_command,
        checkpoint=checkpoint,
    )
    return {**result, "external_changes_enabled": True}


def validate_existing_resource_import(
    *,
    diagnostic: Mapping[str, Any],
    selected_d1_id: str,
    selected_kv_id: str,
    worker_bindings: Mapping[str, Any],
    d1_schema_version: Optional[int],
) -> dict[str, Any]:
    """既存資源をaccount／ID／binding／schemaの組で検証する。"""

    if diagnostic.get("classification") != "existing":
        raise LiteCloudSetupError(
            "既存資源3種類が揃っていません。", failure_code="existing_resource_mismatch"
        )
    resources = diagnostic.get("resources") if isinstance(diagnostic.get("resources"), Mapping) else {}
    d1 = resources.get("d1") if isinstance(resources.get("d1"), Mapping) else {}
    kv = resources.get("kv") if isinstance(resources.get("kv"), Mapping) else {}
    worker = resources.get("worker") if isinstance(resources.get("worker"), Mapping) else {}
    if (
        str(d1.get("id") or "") != str(selected_d1_id or "")
        or str(kv.get("id") or "") != str(selected_kv_id or "")
        or str(worker.get("name") or "") != str(diagnostic.get("resource_name") or "")
    ):
        raise LiteCloudSetupError(
            "選択した既存資源IDが診断結果と一致しません。",
            failure_code="existing_resource_mismatch",
        )
    expected_bindings = {
        DEFAULT_D1_BINDING: ("d1", str(selected_d1_id)),
        DEFAULT_KV_BINDING: ("kv", str(selected_kv_id)),
    }
    for binding, (resource_type, resource_id) in expected_bindings.items():
        value = worker_bindings.get(binding)
        if not isinstance(value, Mapping) or (
            str(value.get("type") or "") != resource_type
            or str(value.get("id") or "") != resource_id
        ):
            raise LiteCloudSetupError(
                "既存Workerの必須bindingが一致しません。",
                failure_code="existing_binding_mismatch",
            )
    if not isinstance(d1_schema_version, int):
        raise LiteCloudSetupError(
            "既存D1のschemaを確認できません。", failure_code="existing_schema_unknown"
        )
    if d1_schema_version > EXPECTED_D1_SCHEMA_VERSION:
        raise LiteCloudSetupError(
            "既存D1はこの本体より新しいschemaです。", failure_code="existing_schema_newer"
        )
    return {
        "state": "resources_ready",
        "mode": "import",
        "account": dict(diagnostic.get("account") or {}),
        "worker": dict(worker),
        "d1": dict(d1),
        "kv": dict(kv),
        "bindings": {
            "d1": DEFAULT_D1_BINDING,
            "kv": DEFAULT_KV_BINDING,
        },
        "d1_schema_version": d1_schema_version,
        "schema_state": (
            "ready" if d1_schema_version == EXPECTED_D1_SCHEMA_VERSION else "migration_required"
        ),
        "failure_code": None,
        "external_changes_enabled": False,
    }


def inspect_existing_worker_bindings_synthetic(
    *,
    account_id: str,
    worker_name: str,
    version_id: str,
    runner: Callable[..., Any],
    root: Optional[Path] = None,
    node_command: str | Path | None = "node",
) -> dict[str, dict[str, str]]:
    """合成runnerでversion JSONのD1／KV bindingだけをallowlist取得する。"""

    if runner is subprocess.run:
        raise LiteCloudSetupError(
            "実APIによる既存Worker取込検証は無効です。",
            failure_code="synthetic_execution_required",
        )
    relay = Path(root) if root is not None else relay_root()
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    command = [
        node_command,
        str(wrangler),
        "versions",
        "view",
        version_id,
        "--name",
        worker_name,
        "--json",
        "--account-id",
        account_id,
    ]
    result = _run_read_only_command(command, runner=runner, cwd=relay)
    parsed = _json_output(result)
    resources = parsed.get("resources") if isinstance(parsed, Mapping) else {}
    raw_bindings = resources.get("bindings") if isinstance(resources, Mapping) else []
    if int(getattr(result, "returncode", 1)) != 0 or not isinstance(raw_bindings, list):
        raise LiteCloudSetupError(
            "既存Workerのbindingを確認できません。",
            failure_code="existing_binding_mismatch",
        )
    bindings: dict[str, dict[str, str]] = {}
    for item in raw_bindings:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or item.get("binding") or "")
        raw_type = str(item.get("type") or "")
        if raw_type in {"d1", "d1_database"}:
            resource_type = "d1"
            resource_id = str(item.get("id") or item.get("database_id") or "")
        elif raw_type in {"kv", "kv_namespace"}:
            resource_type = "kv"
            resource_id = str(item.get("id") or item.get("namespace_id") or "")
        else:
            continue
        if name and resource_id:
            bindings[name] = {"type": resource_type, "id": resource_id}
    return bindings


def inspect_existing_d1_schema_synthetic(
    *,
    account_id: str,
    database_id: str,
    runner: Callable[..., Any],
    root: Optional[Path] = None,
    node_command: str | Path | None = "node",
) -> int:
    """合成runnerで既存D1のschema正本テーブルだけを読み取る。"""

    if runner is subprocess.run:
        raise LiteCloudSetupError(
            "実APIによる既存D1取込検証は無効です。",
            failure_code="synthetic_execution_required",
        )
    relay = Path(root) if root is not None else relay_root()
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    command = [
        node_command,
        str(wrangler),
        "d1",
        "execute",
        database_id,
        "--remote",
        "--command",
        "SELECT d1_schema_version FROM relay_schema_state WHERE singleton_id = 1",
        "--json",
        "--account-id",
        account_id,
    ]
    result = _run_read_only_command(command, runner=runner, cwd=relay)
    parsed = _json_output(result)
    batches = parsed if isinstance(parsed, list) else []
    rows = batches[0].get("results") if batches and isinstance(batches[0], Mapping) else []
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], Mapping) else {}
    schema = row.get("d1_schema_version") if isinstance(row, Mapping) else None
    if int(getattr(result, "returncode", 1)) != 0 or not isinstance(schema, int):
        raise LiteCloudSetupError(
            "既存D1のschemaを確認できません。", failure_code="existing_schema_unknown"
        )
    return schema


def create_import_operation_from_validation(
    validation: Mapping[str, Any], *, metadata_root: Optional[Path] = None
) -> dict[str, Any]:
    """検証済み既存資源を秘密なし操作記録へ保存する。"""

    if validation.get("mode") != "import" or validation.get("state") != "resources_ready":
        raise LiteCloudSetupError(
            "既存資源の検証結果が不正です。", failure_code="existing_resource_mismatch"
        )
    operation = create_setup_operation(mode="import", metadata_root=metadata_root)
    operation.update(
        {
            "state": "resources_ready",
            "account": validation.get("account") or {},
            "worker": validation.get("worker"),
            "d1": validation.get("d1"),
            "kv": validation.get("kv"),
            "bindings": validation.get("bindings") or {},
            "d1_schema_version": validation.get("d1_schema_version"),
            "completed_steps": [
                "resource_plan_confirmed",
                "existing_ids_verified",
                "bindings_verified",
                "schema_verified",
                "resources_ready",
            ],
            "failure_code": None,
        }
    )
    return save_setup_operation(operation, metadata_root=metadata_root)


def generate_runtime_wrangler_config(
    plan: Mapping[str, Any],
    *,
    allowed_origin: str,
    build_id: str,
    assets_directory: str = "./public",
    destination: str = "wrangler.phase2.jsonc",
    root: Optional[Path] = None,
    dry_run: bool = True,
    overwrite: bool = False,
    reuse_if_identical: bool = False,
    migrate_legacy_assets_directory: bool = False,
) -> dict[str, Any]:
    """exampleからallowlist項目だけを置換し、実運用設定を生成する。

    ``reuse_if_identical`` は、書込み成功後かつ操作記録保存前の中断から同じ操作を
    再開するためだけに使う。既存内容が今回の生成結果とbyte単位で一致する場合に限り、
    再書込みせず生成済みとして扱う。

    ``migrate_legacy_assets_directory`` は、同一operation用に生成済みの設定が旧来の
    ``./public`` だけを参照している場合に限り、現在のoperation cache参照へ移す。
    その他の差分が一つでもあれば既存設定を変更しない。
    """

    relay = Path(root) if root is not None else relay_root()
    relative = Path(str(destination or ""))
    if relative.is_absolute() or ".." in relative.parts or re.match(r"^[A-Za-z]:", str(destination)):
        raise LiteCloudSetupError(
            "実運用設定の出力先はrelay配下に限定されます。",
            failure_code="runtime_config_outside_relay",
        )
    output_path = (relay / relative).resolve()
    try:
        output_path.relative_to(relay.resolve())
    except ValueError as exc:
        raise LiteCloudSetupError(
            "実運用設定の出力先はrelay配下に限定されます。",
            failure_code="runtime_config_outside_relay",
        ) from exc
    if output_path.name == "wrangler.phase2.example.jsonc":
        raise LiteCloudSetupError(
            "example設定は上書きできません。", failure_code="runtime_config_invalid"
        )
    existed_before = output_path.exists()
    template = _read_json(relay / "wrangler.phase2.example.jsonc")
    resources = {
        key: plan.get(key) if isinstance(plan.get(key), Mapping) else {}
        for key in ("worker", "d1", "kv")
    }
    worker_name = str(resources["worker"].get("name") or DEFAULT_RESOURCE_NAME)
    d1_name = str(resources["d1"].get("name") or "")
    d1_id = str(resources["d1"].get("id") or "")
    kv_id = str(resources["kv"].get("id") or "")
    parsed_origin = urlsplit(str(allowed_origin or ""))
    normalized_assets_directory = str(assets_directory or "").replace("\\", "/")
    if (
        not template
        or not _RESOURCE_NAME_PATTERN.fullmatch(worker_name)
        or not d1_name
        or not d1_id
        or not kv_id
        or parsed_origin.scheme != "https"
        or not parsed_origin.netloc
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,99}", str(build_id or ""))
        or not (
            normalized_assets_directory == "./public"
            or re.fullmatch(
                r"\.\./\.\./cache/lite_cloud_setup/"
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/public",
                normalized_assets_directory,
            )
        )
    ):
        raise LiteCloudSetupError(
            "実運用設定の入力値が不正です。", failure_code="runtime_config_invalid"
        )
    config = copy.deepcopy(template)
    config["name"] = worker_name
    d1_bindings = config.get("d1_databases")
    kv_bindings = config.get("kv_namespaces")
    config_vars = config.get("vars")
    secret_contract = config.get("secrets")
    if (
        not isinstance(d1_bindings, list)
        or len(d1_bindings) != 1
        or not isinstance(d1_bindings[0], Mapping)
        or d1_bindings[0].get("binding") != DEFAULT_D1_BINDING
        or not isinstance(kv_bindings, list)
        or len(kv_bindings) != 1
        or not isinstance(kv_bindings[0], Mapping)
        or kv_bindings[0].get("binding") != DEFAULT_KV_BINDING
        or not isinstance(config_vars, dict)
        or not isinstance(secret_contract, Mapping)
        or tuple(secret_contract.get("required") or ()) != REQUIRED_BOOTSTRAP_SECRETS
        or not isinstance(config.get("assets"), Mapping)
        or not isinstance(config.get("triggers"), Mapping)
    ):
        raise LiteCloudSetupError(
            "example設定の固定bindingが不正です。", failure_code="runtime_config_invalid"
        )
    d1_bindings[0]["database_name"] = d1_name
    d1_bindings[0]["database_id"] = d1_id
    kv_bindings[0]["id"] = kv_id
    config_vars["LITE_ALLOWED_ORIGIN"] = str(allowed_origin).rstrip("/")
    config_vars["BUILD_ID"] = str(build_id)
    config["assets"]["directory"] = normalized_assets_directory
    content = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    if "REPLACE_WITH_" in content:
        raise LiteCloudSetupError(
            "実運用設定に未置換項目があります。", failure_code="runtime_config_invalid"
        )
    reused_existing = False
    migrated_legacy_assets_directory = False
    if existed_before and not overwrite:
        existing_content = ""
        if not dry_run and (reuse_if_identical or migrate_legacy_assets_directory):
            try:
                existing_content = file_lock_utils.safe_text_read(output_path.as_posix())
                reused_existing = reuse_if_identical and existing_content == content
            except (OSError, UnicodeError):
                reused_existing = False
        if (
            not dry_run
            and not reused_existing
            and migrate_legacy_assets_directory
        ):
            assets_match = re.fullmatch(
                r"\.\./\.\./cache/lite_cloud_setup/"
                r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/public",
                normalized_assets_directory,
            )
            same_operation_destination = bool(
                assets_match
                and relative.name == f"wrangler.setup.{assets_match.group(1)}.jsonc"
            )
            try:
                existing_config = json.loads(existing_content)
            except (TypeError, json.JSONDecodeError):
                existing_config = None
            expected_legacy_config = copy.deepcopy(config)
            expected_legacy_config["assets"]["directory"] = "./public"
            if same_operation_destination and existing_config == expected_legacy_config:
                file_lock_utils.safe_text_write(output_path.as_posix(), content)
                migrated_legacy_assets_directory = True
        if not reused_existing and not migrated_legacy_assets_directory:
            raise LiteCloudSetupError(
                "実運用設定が既にあります。無確認では上書きしません。",
                failure_code="runtime_config_exists",
            )
    if not dry_run and not reused_existing and not migrated_legacy_assets_directory:
        file_lock_utils.safe_text_write(output_path.as_posix(), content)
    return {
        "state": "local_config_ready" if not dry_run else "runtime_config_dry_run_ready",
        "config_path": f"cloud/lite-relay/{relative.as_posix()}",
        "content": content,
        "written": not dry_run,
        "reused_existing": reused_existing,
        "migrated_legacy_assets_directory": migrated_legacy_assets_directory,
        "overwrote_existing": bool(existed_before and overwrite and not dry_run),
        "external_changes_enabled": False,
    }


def validate_runtime_config_dry_run(
    config_path: str,
    *,
    runner: Callable[..., Any],
    root: Optional[Path] = None,
    node_command: str | Path | None = None,
) -> dict[str, Any]:
    """生成済み設定を使うローカルdeploy dry-run契約。"""

    relay = Path(root) if root is not None else relay_root()
    relative = Path(str(config_path or ""))
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or re.match(r"^[A-Za-z]:", str(config_path))
    ):
        raise LiteCloudSetupError(
            "dry-run設定はrelay配下に限定されます。",
            failure_code="runtime_config_outside_relay",
        )
    path = relay / relative
    try:
        path.resolve().relative_to(relay.resolve())
    except ValueError as exc:
        raise LiteCloudSetupError(
            "dry-run設定はrelay配下に限定されます。",
            failure_code="runtime_config_outside_relay",
        ) from exc
    if not path.is_file():
        raise LiteCloudSetupError(
            "dry-run設定がありません。", failure_code="runtime_config_invalid"
        )
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    command = [node_command, str(wrangler), "deploy", "--dry-run", "--config", str(path)]
    try:
        result = runner(
            command,
            cwd=str(relay),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiteCloudSetupError(
            "実運用設定のdry-runを実行できません。",
            failure_code="runtime_config_dry_run_failed",
        ) from exc
    if int(getattr(result, "returncode", 1)) != 0:
        raise LiteCloudSetupError(
            "実運用設定のdry-runに失敗しました。",
            failure_code="runtime_config_dry_run_failed",
        )
    return {
        "state": "runtime_config_dry_run_ready",
        "checked_command": "wrangler_deploy_dry_run",
        "external_changes_enabled": False,
        "deployment_created": False,
    }


def record_runtime_config_ready(
    operation: Mapping[str, Any],
    generation_result: Mapping[str, Any],
    dry_run_result: Mapping[str, Any],
    *,
    metadata_root: Optional[Path] = None,
) -> dict[str, Any]:
    """書込み・dry-run済み設定の相対パスだけを操作記録へ保存する。"""

    if (
        not generation_result.get("written")
        or generation_result.get("state") != "local_config_ready"
        or dry_run_result.get("state") != "runtime_config_dry_run_ready"
        or dry_run_result.get("deployment_created") is not False
    ):
        raise LiteCloudSetupError(
            "実運用設定の生成が完了していません。", failure_code="runtime_config_invalid"
        )
    current = sanitize_setup_operation(operation)
    if current.get("state") != "resources_ready":
        raise LiteCloudSetupError(
            "資源準備完了前には設定を確定できません。", failure_code="runtime_config_invalid"
        )
    current["config_path"] = str(generation_result.get("config_path") or "")
    current["state"] = "local_config_ready"
    current["failure_code"] = None
    _append_completed_step(current, "runtime_config_generated")
    _append_completed_step(current, "runtime_config_dry_run")
    return save_setup_operation(current, metadata_root=metadata_root)


def generate_bootstrap_secrets(
    *, token_bytes: int = 48, generator: Callable[[int], str] = secure_secrets.token_urlsafe
) -> dict[str, str]:
    """初回起動に必要な3値を個別の暗号学的乱数としてメモリ上だけに生成する。"""

    if token_bytes < 32:
        raise LiteCloudSetupError(
            "bootstrap Secretの強度が不足しています。", failure_code="bootstrap_secret_invalid"
        )
    values = {name: str(generator(token_bytes)) for name in REQUIRED_BOOTSTRAP_SECRETS}
    if any(len(value) < 32 for value in values.values()) or len(set(values.values())) != len(values):
        raise LiteCloudSetupError(
            "bootstrap Secretを安全に生成できませんでした。",
            failure_code="bootstrap_secret_invalid",
        )
    return values


def _validated_bootstrap_secrets(values: Mapping[str, str]) -> dict[str, str]:
    if tuple(values.keys()) != REQUIRED_BOOTSTRAP_SECRETS:
        raise LiteCloudSetupError(
            "bootstrap Secretの構成が不正です。", failure_code="bootstrap_secret_invalid"
        )
    safe = {name: str(values[name]) for name in REQUIRED_BOOTSTRAP_SECRETS}
    if any(len(value) < 32 for value in safe.values()) or len(set(safe.values())) != len(safe):
        raise LiteCloudSetupError(
            "bootstrap Secretの値が不正です。", failure_code="bootstrap_secret_invalid"
        )
    return safe


def _relay_config_name(operation: Mapping[str, Any]) -> str:
    value = str(operation.get("config_path") or "").replace("\\", "/")
    prefix = "cloud/lite-relay/"
    if not value.startswith(prefix):
        raise LiteCloudSetupError(
            "実運用設定の場所が不正です。", failure_code="runtime_config_outside_relay"
        )
    relative = value[len(prefix) :]
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts:
        raise LiteCloudSetupError(
            "実運用設定の場所が不正です。", failure_code="runtime_config_outside_relay"
        )
    return path.as_posix()


def _operation_assets_directory(operation_id: str) -> str:
    """署名済みappを変更しないoperation専用Static Assets相対pathを返す。"""

    value = str(operation_id or "").lower()
    if not _OPERATION_ID_PATTERN.fullmatch(value):
        raise LiteCloudSetupError(
            "初回セットアップの操作IDが不正です。",
            failure_code="operation_record_schema_invalid",
        )
    return f"../../cache/lite_cloud_setup/{value}/public"


def _operation_assets_root(relay: Path, operation_id: str) -> Path:
    """operation用assetsをappの永続cache配下へ固定する。"""

    relative = _operation_assets_directory(operation_id)
    destination = (relay / relative).resolve()
    app_root = relay.resolve().parent.parent
    cache_root = (app_root / "cache" / "lite_cloud_setup").resolve()
    try:
        destination.relative_to(cache_root)
    except ValueError as exc:
        raise LiteCloudSetupError(
            "Static Assetsの出力先を確認できません。",
            failure_code="static_assets_build_failed",
        ) from exc
    return destination


def ensure_operation_assets_directory(
    operation_id: str, *, root: Optional[Path] = None
) -> Path:
    """Wrangler dry-run前にoperation専用Static Assets出力先を準備する。"""

    relay = Path(root) if root is not None else relay_root()
    destination = _operation_assets_root(relay, operation_id)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LiteCloudSetupError(
            "Static Assetsの出力先を準備できません。",
            failure_code="static_assets_build_failed",
        ) from exc
    if not destination.is_dir():
        raise LiteCloudSetupError(
            "Static Assetsの出力先を準備できません。",
            failure_code="static_assets_build_failed",
        )
    return destination


def _package4_run(
    command: list[str],
    *,
    runner: Callable[..., Any],
    cwd: Path,
    input_text: Optional[str] = None,
    failure_code: str,
) -> Any:
    try:
        return runner(
            command,
            input=input_text,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiteCloudSetupError(
            "外部操作の完了状態を確定できません。",
            failure_code=failure_code,
        ) from exc


def _result_identifier(result: Any, key: str) -> str:
    explicit = getattr(result, key, None)
    if explicit:
        return str(explicit).strip()
    parsed = _json_output(result)
    if isinstance(parsed, Mapping):
        value = str(parsed.get(key) or "").strip()
        if value:
            return value

    labels = {
        "version_id": "Worker Version ID",
        "deployment_id": "Deployment ID",
    }
    label = labels.get(key)
    if not label:
        return ""
    labelled_line = re.compile(
        rf"^[ \t]*{re.escape(label)}[ \t]*:[ \t]*(.*?)[ \t]*\r?$",
        re.MULTILINE,
    )
    candidates: list[str] = []
    for stream_name in ("stdout", "stderr"):
        stream = str(getattr(result, stream_name, "") or "")
        candidates.extend(labelled_line.findall(stream))
    if len(candidates) != 1:
        return ""
    candidate = candidates[0].strip()
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        candidate,
    ):
        return ""
    return candidate.lower()


def _cloudflare_api_json(
    method: str,
    url: str,
    *,
    token: str,
    payload: Optional[Mapping[str, Any] | list[Mapping[str, Any]]] = None,
    failure_code: str = "worker_container_create_failed",
    uncertain_failure_code: str = "worker_container_reconciliation_required",
    action_label: str = "Workerの登録",
) -> Mapping[str, Any]:
    """OAuth tokenをログやargvへ出さずCloudflare JSON APIを呼ぶ。"""

    body = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else None
    )
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60, context=create_tls_context()) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8", "replace"))
        except (TypeError, ValueError):
            error_payload = {}
        codes = (
            [
                str(item.get("code") or "")
                for item in error_payload.get("errors", [])
                if isinstance(item, Mapping)
            ]
            if isinstance(error_payload, Mapping)
            else []
        )
        raise LiteCloudSetupError(
            f"Cloudflareで{action_label}に失敗しました。"
            + (f" (code: {', '.join(code for code in codes if code)})" if codes else ""),
            failure_code=failure_code,
        ) from exc
    except (OSError, TimeoutError, ValueError) as exc:
        raise LiteCloudSetupError(
            f"{action_label}の完了状態を確定できません。再送せず照合が必要です。",
            failure_code=uncertain_failure_code,
        ) from exc
    if not isinstance(result, Mapping) or result.get("success") is not True:
        raise LiteCloudSetupError(
            f"Cloudflareの{action_label}応答を確認できませんでした。",
            failure_code=failure_code,
        )
    return result


def _schedule_values(
    value: Any, *, failure_code: str, allow_strings: bool = False
) -> list[str]:
    """Cloudflareのschedules応答を、重複を除いたCron文字列へ縮退する。"""

    if not isinstance(value, list):
        raise LiteCloudSetupError(
            "CloudflareのCron設定を確認できません。",
            failure_code=failure_code,
        )
    schedules: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            cron = str(item.get("cron") or "").strip()
        elif allow_strings and isinstance(item, str):
            # 設定ファイルはCron文字列を許可し、API応答ではobjectを要求する。
            cron = item.strip()
        else:
            cron = ""
        if not cron:
            raise LiteCloudSetupError(
                "CloudflareのCron設定に不正な項目があります。",
                failure_code=failure_code,
            )
        if cron not in schedules:
            schedules.append(cron)
    return schedules


def _unique_initial_schedule_config_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    """初回公開の設定で重複キーを拒否し、解釈の揺れを防ぐ。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_config_key")
        result[key] = value
    return result


def _read_initial_schedule_config(path: Path) -> dict[str, Any]:
    """生成済みCron設定をロック内で読み、JSONCの重複キーも拒否する。"""

    with file_lock_utils.get_file_lock(path.as_posix()):
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".jsonc":
            text = _strip_jsonc_trailing_commas(_strip_jsonc_comments(text))
        value = json.loads(
            text, object_pairs_hook=_unique_initial_schedule_config_object
        )
    if not isinstance(value, dict):
        raise ValueError("invalid_config")
    return value


def _configured_initial_schedules(
    config_path: Path, *, relay: Path, worker_name: str
) -> list[str]:
    """生成済み設定から初回公開に必要なCronを読み取る。"""

    try:
        config_path.resolve().relative_to(relay.resolve())
    except ValueError as exc:
        raise LiteCloudSetupError(
            "初回Cron設定の場所が不正です。",
            failure_code="publish_reconciliation_required",
        ) from exc
    try:
        config = _read_initial_schedule_config(config_path)
    except (OSError, ValueError, TypeError, file_lock_utils.Timeout) as exc:
        raise LiteCloudSetupError(
            "初回Cron設定を安全に読み取れません。",
            failure_code="publish_reconciliation_required",
        ) from exc
    if str(config.get("name") or "").strip() != worker_name:
        raise LiteCloudSetupError(
            "初回Cron設定のWorker名が確認済みWorkerと一致しません。",
            failure_code="publish_reconciliation_required",
        )
    triggers = config.get("triggers") if isinstance(config, Mapping) else None
    crons = triggers.get("crons") if isinstance(triggers, Mapping) else None
    schedules = _schedule_values(
        crons,
        failure_code="publish_reconciliation_required",
        allow_strings=True,
    )
    missing = [cron for cron in REQUIRED_INITIAL_CRONS if cron not in schedules]
    if missing:
        raise LiteCloudSetupError(
            "初回公開に必要なCronが生成済み設定にありません。",
            failure_code="publish_reconciliation_required",
        )
    return schedules


def ensure_cloudflare_worker_schedules(
    *,
    account_id: str,
    worker_name: str,
    config_path: Path,
    runner: Callable[..., Any],
    relay: Path,
    node_command: str | Path | None = None,
    api_request: Callable[..., Mapping[str, Any]] = _cloudflare_api_json,
) -> dict[str, Any]:
    """初回公開後のCronをGET→不足分PUT→GETで冪等に確定する。

    ``versions upload``／``versions deploy``はWorker versionの公開だけを行い、
    Cron Triggerは反映しないため、専用Schedules APIを使う。既存の独自Cronは
    最初のGET結果へ含めてPUTするので削除しない。GET失敗やPUT後の照合失敗は、
    再送をせず ``publish_reconciliation_required`` へ戻す。
    """

    if not account_id or not _RESOURCE_NAME_PATTERN.fullmatch(worker_name):
        raise LiteCloudSetupError(
            "初回Cron登録対象が不正です。",
            failure_code="operation_record_schema_invalid",
        )
    relay_path = Path(relay)
    configured = _configured_initial_schedules(
        Path(config_path), relay=relay_path, worker_name=worker_name
    )
    token = _wrangler_auth_token(
        runner=runner,
        relay=relay_path,
        node_command=node_command,
    )
    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{quote(account_id, safe='')}/workers/scripts/"
        f"{quote(worker_name, safe='')}/schedules"
    )
    request_options = {
        "token": token,
        "failure_code": "publish_reconciliation_required",
        "uncertain_failure_code": "publish_reconciliation_required",
        "action_label": "初回Cron登録",
    }
    current = api_request("GET", endpoint, **request_options)
    current_result = current.get("result") if isinstance(current, Mapping) else None
    current_values = _schedule_values(
        current_result.get("schedules") if isinstance(current_result, Mapping) else None,
        failure_code="publish_reconciliation_required",
    )
    missing = [cron for cron in configured if cron not in current_values]
    if not missing:
        return {
            "verified": True,
            "changed": False,
            "schedules": current_values,
            "added": [],
        }

    merged = [*current_values, *missing]
    api_request(
        "PUT",
        endpoint,
        payload=[{"cron": cron} for cron in merged],
        **request_options,
    )
    confirmed = api_request("GET", endpoint, **request_options)
    confirmed_result = confirmed.get("result") if isinstance(confirmed, Mapping) else None
    confirmed_values = _schedule_values(
        confirmed_result.get("schedules") if isinstance(confirmed_result, Mapping) else None,
        failure_code="publish_reconciliation_required",
    )
    if any(cron not in confirmed_values for cron in merged):
        raise LiteCloudSetupError(
            "初回Cron登録の完了を確認できません。再公開は行わず照合を続けてください。",
            failure_code="publish_reconciliation_required",
        )
    return {
        "verified": True,
        "changed": True,
        "schedules": confirmed_values,
        "added": missing,
    }


def _wrangler_auth_token(
    *,
    runner: Callable[..., Any],
    relay: Path,
    node_command: str | Path | None,
) -> str:
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    result = _package4_run(
        [node_command, str(wrangler), "auth", "token", "--json"],
        runner=runner,
        cwd=relay,
        failure_code="cloudflare_authentication_required",
    )
    parsed = _json_output(result)
    token = str(parsed.get("token") or "") if isinstance(parsed, Mapping) else ""
    if int(getattr(result, "returncode", 1)) != 0 or not token:
        raise LiteCloudSetupError(
            "Cloudflareのログイン情報を取得できませんでした。",
            failure_code="cloudflare_authentication_required",
        )
    return token


def ensure_cloudflare_worker_container(
    *,
    account_id: str,
    worker_name: str,
    setup_tag: str,
    runner: Callable[..., Any],
    relay: Path,
    node_command: str | Path | None = None,
    api_request: Callable[..., Mapping[str, Any]] = _cloudflare_api_json,
) -> dict[str, Any]:
    """Beta Worker APIでコード／version／deploymentなしのWorker名だけを冪等登録する。"""

    if not _RESOURCE_NAME_PATTERN.fullmatch(worker_name) or not re.fullmatch(
        r"nexus-ark-setup-[a-f0-9]{12}", setup_tag
    ):
        raise LiteCloudSetupError(
            "Worker登録対象が不正です。",
            failure_code="operation_record_schema_invalid",
        )
    token = _wrangler_auth_token(
        runner=runner,
        relay=relay,
        node_command=node_command,
    )
    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{quote(account_id, safe='')}/workers/workers"
    )
    inventory = api_request("GET", endpoint, token=token)
    raw_records = inventory.get("result", [])
    records = raw_records if isinstance(raw_records, list) else []
    matches = [
        item
        for item in records
        if isinstance(item, Mapping) and str(item.get("name") or "") == worker_name
    ]
    if len(matches) > 1:
        raise LiteCloudSetupError(
            "同名Workerを複数検出したため停止しました。",
            failure_code="resource_collision_detected",
        )
    if matches:
        tags = matches[0].get("tags")
        if isinstance(tags, list) and setup_tag in {str(tag) for tag in tags}:
            return {
                "worker_id": str(matches[0].get("id") or ""),
                "created": False,
                "reconciled": True,
            }
        raise LiteCloudSetupError(
            "この操作が作成したものではない同名Workerを検出しました。",
            failure_code="resource_collision_detected",
        )
    created = api_request(
        "POST",
        endpoint,
        token=token,
        payload={"name": worker_name, "tags": [setup_tag]},
    )
    record = created.get("result")
    if not isinstance(record, Mapping) or str(record.get("name") or "") != worker_name:
        raise LiteCloudSetupError(
            "CloudflareのWorker登録結果が対象名と一致しません。",
            failure_code="worker_container_reconciliation_required",
        )
    return {
        "worker_id": str(record.get("id") or ""),
        "created": True,
        "reconciled": False,
    }


def ensure_cloudflare_worker_subdomain(
    *,
    account_id: str,
    worker_name: str,
    runner: Callable[..., Any],
    relay: Path,
    node_command: str | Path | None = None,
    api_request: Callable[..., Mapping[str, Any]] = _cloudflare_api_json,
) -> dict[str, bool]:
    """確認済みWorkerのworkers.devだけを有効にし、Preview URLは無効に保つ。"""

    if not account_id or not _RESOURCE_NAME_PATTERN.fullmatch(worker_name):
        raise LiteCloudSetupError(
            "workers.dev公開対象が不正です。",
            failure_code="operation_record_schema_invalid",
        )
    token = _wrangler_auth_token(
        runner=runner,
        relay=relay,
        node_command=node_command,
    )
    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{quote(account_id, safe='')}/workers/scripts/"
        f"{quote(worker_name, safe='')}/subdomain"
    )
    request_options = {
        "token": token,
        "failure_code": "worker_subdomain_reconciliation_required",
        "uncertain_failure_code": "worker_subdomain_reconciliation_required",
        "action_label": "workers.dev公開設定",
    }
    current = api_request("GET", endpoint, **request_options)
    current_result = current.get("result")
    if isinstance(current_result, Mapping) and (
        current_result.get("enabled") is True
        and current_result.get("previews_enabled") is False
    ):
        return {"enabled": True, "previews_enabled": False, "changed": False}
    updated = api_request(
        "POST",
        endpoint,
        payload={"enabled": True, "previews_enabled": False},
        **request_options,
    )
    updated_result = updated.get("result")
    if not isinstance(updated_result, Mapping) or not (
        updated_result.get("enabled") is True
        and updated_result.get("previews_enabled") is False
    ):
        raise LiteCloudSetupError(
            "workers.dev公開設定の完了状態を確認できません。",
            failure_code="worker_subdomain_reconciliation_required",
        )
    return {"enabled": True, "previews_enabled": False, "changed": True}


def prepare_initial_version_synthetic(
    operation: Mapping[str, Any],
    *,
    confirmed_operation_id: str,
    bootstrap_secrets: Mapping[str, str],
    runner: Callable[..., Any],
    root: Optional[Path] = None,
    metadata_root: Optional[Path] = None,
    node_command: str | Path | None = "node",
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
    persist_connection_secrets: Optional[Callable[[Mapping[str, str]], bool]] = None,
    confirm_worker_absent: bool = False,
    worker_container_creator: Optional[Callable[[str, str], Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """合成runnerでbuild・初期migration・Secret付き未公開versionを順に準備する。"""

    if runner is subprocess.run:
        raise LiteCloudSetupError(
            "Package 4の検証は合成runnerでだけ実行できます。",
            failure_code="synthetic_execution_required",
        )
    current = sanitize_setup_operation(operation)
    if confirmed_operation_id != current["operation_id"]:
        raise LiteCloudSetupError(
            "確認した操作IDと実行対象が一致しません。",
            failure_code="operation_confirmation_mismatch",
        )
    if current.get("state") == "version_reconciliation_required" or (
        current.get("state") not in {
            "worker_container_required",
            "worker_container_reconciliation_required",
        }
        and "initial_version_upload_requested" in current.get("completed_steps", [])
        and "initial_version_uploaded" not in current.get("completed_steps", [])
    ):
        current.update(
            state="version_reconciliation_required",
            failed_step="initial_version_upload",
            failure_code="version_reconciliation_required",
        )
        _save_resource_checkpoint(
            current,
            "version_reconciliation_required",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "未公開versionの完了状態をremote再診断してください。自動再送は行いません。",
            failure_code="version_reconciliation_required",
        )
    resumable_states = {
        "local_config_ready",
        "bootstrap_secrets_ready",
        "migrated",
        "worker_container_required",
        "worker_container_reconciliation_required",
        "version_ready",
    }
    if current.get("state") not in resumable_states or current.get("mode") != "new":
        raise LiteCloudSetupError(
            "新規資源と実運用設定の準備が完了していません。",
            failure_code="operation_record_state_invalid",
        )
    worker_container_reconciliation_pending = (
        current.get("state") == "worker_container_reconciliation_required"
    )
    if current.get("state") == "version_ready":
        current["state"] = "publish_confirmation_required"
        _append_completed_step(current, "publish_summary_ready")
        return _save_resource_checkpoint(
            current,
            "publish_confirmation_required",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
    secret_values = _validated_bootstrap_secrets(bootstrap_secrets)
    relay = Path(root) if root is not None else relay_root()
    config_name = _relay_config_name(current)
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    account_id = str((current.get("account") or {}).get("id") or "")
    database_id = str((current.get("d1") or {}).get("id") or "")
    if not account_id or not database_id:
        raise LiteCloudSetupError(
            "対象アカウントまたはD1 IDがありません。",
            failure_code="operation_record_schema_invalid",
        )
    _confirm_synthetic_account(
        account_id=account_id,
        runner=runner,
        base=[node_command, str(wrangler)],
        cwd=relay,
    )

    if (
        persist_connection_secrets is not None
        and "local_connection_keys_saved" not in current.get("completed_steps", [])
    ):
        local_values = {
            "OWNER_AUTH_TOKEN": secret_values["OWNER_AUTH_TOKEN"],
            "BUNDLE_SIGNING_KEY": secret_values["BUNDLE_SIGNING_KEY"],
        }
        try:
            persisted = persist_connection_secrets(local_values) is True
        except Exception:
            persisted = False
        if not persisted:
            current.update(
                state="local_config_ready",
                failed_step="local_connection_keys",
                failure_code="local_secret_recovery_required",
            )
            _save_resource_checkpoint(
                current,
                "local_connection_secrets_save_failed",
                metadata_root=metadata_root,
                checkpoint=checkpoint,
            )
            raise LiteCloudSetupError(
                "Cloudflareへ送る前に本体の接続キーを保存できませんでした。外部変更は行っていません。",
                failure_code="local_secret_recovery_required",
            )
        _append_completed_step(current, "local_connection_keys_saved")
        _save_resource_checkpoint(
            current,
            "local_connection_keys_saved",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )

    if current.get("state") == "local_config_ready":
        current.update(state="bootstrap_secrets_ready", failed_step="", failure_code=None)
        _append_completed_step(current, "bootstrap_secrets_generated")
        _save_resource_checkpoint(
            current, "bootstrap_secrets_ready", metadata_root=metadata_root, checkpoint=checkpoint
        )

    try:
        assets_root = _operation_assets_root(relay, current["operation_id"])
        build = _package4_run(
            [
                node_command,
                str(relay / "scripts" / "build-unified-lite.mjs"),
                "--output-root",
                str(assets_root),
            ],
            runner=runner,
            cwd=relay,
            failure_code="static_assets_build_failed",
        )
    except LiteCloudSetupError:
        current.update(failed_step="static_assets_build", failure_code="static_assets_build_failed")
        _save_resource_checkpoint(
            current, "static_assets_build_failed", metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise
    if int(getattr(build, "returncode", 1)) != 0:
        current.update(failed_step="static_assets_build", failure_code="static_assets_build_failed")
        _save_resource_checkpoint(
            current, "static_assets_build_failed", metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise LiteCloudSetupError(
            "Worker Static Assets buildに失敗しました。",
            failure_code="static_assets_build_failed",
        )
    _append_completed_step(current, "static_assets_built")

    try:
        migration = _package4_run(
            [
                node_command,
                str(wrangler),
                "d1",
                "migrations",
                "apply",
                DEFAULT_D1_BINDING,
                "--remote",
                "--config",
                str(relay / config_name),
                "--account-id",
                account_id,
            ],
            runner=runner,
            cwd=relay,
            failure_code="initial_migration_failed",
        )
    except LiteCloudSetupError:
        current.update(failed_step="initial_migration", failure_code="initial_migration_failed")
        _save_resource_checkpoint(
            current, "initial_migration_failed", metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise
    if int(getattr(migration, "returncode", 1)) != 0:
        current.update(failed_step="initial_migration", failure_code="initial_migration_failed")
        _save_resource_checkpoint(
            current, "initial_migration_failed", metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise LiteCloudSetupError(
            "D1初期migrationに失敗しました。", failure_code="initial_migration_failed"
        )
    schema = inspect_existing_d1_schema_synthetic(
        account_id=account_id,
        database_id=database_id,
        runner=runner,
        root=relay,
        node_command=node_command,
    )
    if schema != EXPECTED_D1_SCHEMA_VERSION:
        current.update(failed_step="initial_schema_verify", failure_code="initial_schema_mismatch")
        _save_resource_checkpoint(
            current, "initial_schema_mismatch", metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise LiteCloudSetupError(
            "D1 schemaが初期migrationの期待値と一致しません。",
            failure_code="initial_schema_mismatch",
        )
    current.update(state="migrated", d1_schema_version=schema)
    _append_completed_step(current, "initial_migration_applied")
    _append_completed_step(current, "initial_schema_verified")
    _save_resource_checkpoint(current, "migrated", metadata_root=metadata_root, checkpoint=checkpoint)

    if confirm_worker_absent and "worker_container_created" not in current.get("completed_steps", []):
        worker_name = str((current.get("worker") or {}).get("name") or "")
        if not worker_container_reconciliation_pending:
            collision_check = _package4_run(
                [
                    node_command,
                    str(wrangler),
                    "deployments",
                    "list",
                    "--name",
                    worker_name,
                    "--json",
                ],
                runner=runner,
                cwd=relay,
                failure_code="cloudflare_inventory_failed",
            )
            if int(getattr(collision_check, "returncode", 1)) == 0:
                current.update(
                    state="resource_collision",
                    failed_step="initial_worker_absence_check",
                    failure_code="resource_collision_detected",
                )
                _save_resource_checkpoint(
                    current,
                    "initial_worker_collision",
                    metadata_root=metadata_root,
                    checkpoint=checkpoint,
                )
                raise LiteCloudSetupError(
                    "初回upload直前に同名Workerを検出しました。既存Workerへは送信しません。",
                    failure_code="resource_collision_detected",
                )
            if not (
                bool(getattr(collision_check, "not_found", False))
                or _structured_error_codes(collision_check) & {10007, 10090}
            ):
                raise LiteCloudSetupError(
                    "初回upload直前に同名Workerの不存在を確認できませんでした。",
                    failure_code="cloudflare_inventory_failed",
                )

        if not callable(worker_container_creator):
            raise LiteCloudSetupError(
                "未公開version用Workerの登録処理がありません。",
                failure_code="worker_container_create_failed",
            )
        setup_tag = _setup_version_tag(current["operation_id"])
        current["setup_tag"] = setup_tag
        _append_completed_step(current, "worker_container_create_requested")
        _save_resource_checkpoint(
            current,
            "worker_container_create_requested",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        try:
            container_result = worker_container_creator(worker_name, setup_tag)
        except LiteCloudSetupError as exc:
            if exc.failure_code == "resource_collision_detected":
                current.update(
                    state="resource_collision",
                    failed_step="worker_container_create",
                    failure_code="resource_collision_detected",
                )
                event = "worker_container_collision"
            else:
                current.update(
                    state="worker_container_reconciliation_required",
                    failed_step="worker_container_create",
                    failure_code="worker_container_reconciliation_required",
                )
                event = "worker_container_reconciliation_required"
            _save_resource_checkpoint(
                current,
                event,
                metadata_root=metadata_root,
                checkpoint=checkpoint,
            )
            raise
        if not isinstance(container_result, Mapping):
            current.update(
                state="worker_container_reconciliation_required",
                failed_step="worker_container_create",
                failure_code="worker_container_reconciliation_required",
            )
            _save_resource_checkpoint(
                current,
                "worker_container_reconciliation_required",
                metadata_root=metadata_root,
                checkpoint=checkpoint,
            )
            raise LiteCloudSetupError(
                "Worker登録の完了状態を確認できません。",
                failure_code="worker_container_reconciliation_required",
            )
        current.update(state="migrated", failed_step="", failure_code=None)
        _append_completed_step(current, "worker_container_created")
        if container_result.get("reconciled") is True:
            _append_completed_step(current, "worker_container_reconciled")
        _save_resource_checkpoint(
            current,
            "worker_container_ready",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )

    setup_tag = _setup_version_tag(current["operation_id"])
    current["setup_tag"] = setup_tag
    upload_command = build_initial_secret_upload_command(
        root=relay,
        node_command=node_command,
        config_name=config_name,
        version_tag=setup_tag,
    )
    secret_input = json.dumps(secret_values, ensure_ascii=False, separators=(",", ":"))
    _append_completed_step(current, "initial_version_upload_requested")
    _save_resource_checkpoint(
        current,
        "initial_version_upload_requested",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    try:
        upload = _package4_run(
            upload_command,
            runner=runner,
            cwd=relay,
            input_text=secret_input,
            failure_code="version_reconciliation_required",
        )
    except LiteCloudSetupError as exc:
        current.update(
            state="version_reconciliation_required",
            failed_step="initial_version_upload",
            failure_code="version_reconciliation_required",
        )
        _save_resource_checkpoint(
            current, "version_reconciliation_required", metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise exc
    outputs = "\n".join(
        str(getattr(upload, name, "") or "") for name in ("stdout", "stderr")
    )
    if any(value in outputs or value in json.dumps(upload_command) for value in secret_values.values()):
        raise LiteCloudSetupError(
            "Secretがコマンドまたは出力へ露出しました。",
            failure_code="synthetic_secret_exposure_detected",
        )
    if int(getattr(upload, "returncode", 1)) != 0:
        current.update(
            state="version_reconciliation_required",
            failed_step="initial_version_upload",
            failure_code="version_reconciliation_required",
        )
        _save_resource_checkpoint(
            current, "version_reconciliation_required", metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise LiteCloudSetupError(
            "未公開versionの完了状態をremote再診断してください。自動再試行は行いません。",
            failure_code="version_reconciliation_required",
        )
    version_id = _result_identifier(upload, "version_id")
    if not version_id:
        current.update(failed_step="initial_version_id", failure_code="initial_version_id_missing")
        _save_resource_checkpoint(
            current, "initial_version_id_missing", metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise LiteCloudSetupError(
            "未公開version IDを確認できません。", failure_code="initial_version_id_missing"
        )
    current.update(state="version_ready", version_id=version_id, failed_step="", failure_code=None)
    worker = dict(current.get("worker") or {})
    worker["version_id"] = version_id
    current["worker"] = worker
    _append_completed_step(current, "bootstrap_secrets_uploaded")
    _append_completed_step(current, "initial_version_uploaded")
    _save_resource_checkpoint(current, "version_ready", metadata_root=metadata_root, checkpoint=checkpoint)
    current["state"] = "publish_confirmation_required"
    _append_completed_step(current, "publish_summary_ready")
    return _save_resource_checkpoint(
        current,
        "publish_confirmation_required",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )


def prepare_initial_version(
    operation: Mapping[str, Any],
    *,
    confirmed_operation_id: str,
    confirmed_resource_plan_digest: str,
    confirmed_account_id: str,
    allow_external_changes: bool,
    bootstrap_secrets: Mapping[str, str],
    persist_connection_secrets: Callable[[Mapping[str, str]], bool],
    runner: Callable[..., Any] = subprocess.run,
    root: Optional[Path] = None,
    metadata_root: Optional[Path] = None,
    node_command: str | Path | None = None,
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
    worker_container_creator: Optional[Callable[[str, str], Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """migrationとSecret付き未公開versionまでを準備し、公開直前で停止する。"""
    current = _confirmed_external_operation(
        operation,
        confirmed_operation_id=confirmed_operation_id,
        confirmed_resource_plan_digest=confirmed_resource_plan_digest,
        confirmed_account_id=confirmed_account_id,
        allow_external_changes=allow_external_changes,
    )
    if current.get("state") == "publish_confirmation_required":
        return {**current, "external_changes_enabled": True, "deployment_created": False}
    if not callable(persist_connection_secrets):
        raise LiteCloudSetupError(
            "本体接続キーの保存処理がありません。",
            failure_code="local_secret_recovery_required",
        )
    account_id = str((current.get("account") or {}).get("id") or "")
    worker_name = str((current.get("worker") or {}).get("name") or "")
    _validated_worker_url(str(current.get("worker_url") or ""), worker_name)
    fixed_runner = _account_fixed_runner(runner, account_id)
    relay = Path(root) if root is not None else relay_root()
    if worker_container_creator is None:

        def worker_container_creator(name: str, setup_tag: str) -> Mapping[str, Any]:
            return ensure_cloudflare_worker_container(
                account_id=account_id,
                worker_name=name,
                setup_tag=setup_tag,
                runner=fixed_runner,
                relay=relay,
                node_command=node_command,
            )
    result = prepare_initial_version_synthetic(
        current,
        confirmed_operation_id=current["operation_id"],
        bootstrap_secrets=bootstrap_secrets,
        runner=fixed_runner,
        root=root,
        metadata_root=metadata_root,
        node_command=node_command,
        checkpoint=checkpoint,
        persist_connection_secrets=persist_connection_secrets,
        confirm_worker_absent=True,
        worker_container_creator=worker_container_creator,
    )
    return {**result, "external_changes_enabled": True, "deployment_created": False}


def _reconciliation_json(
    command: list[str],
    *,
    runner: Callable[..., Any],
    cwd: Path,
    failure_code: str,
) -> Any:
    result = _package4_run(
        command,
        runner=runner,
        cwd=cwd,
        failure_code=failure_code,
    )
    if int(getattr(result, "returncode", 1)) != 0:
        raise LiteCloudSetupError(
            "Cloudflareの現在状態を読み取れませんでした。外部変更は行っていません。",
            failure_code=failure_code,
        )
    parsed = _json_output(result)
    if not isinstance(parsed, (list, Mapping)):
        raise LiteCloudSetupError(
            "Cloudflareの照合結果が不正です。外部変更は行っていません。",
            failure_code=failure_code,
        )
    return parsed


def _uuid_identifier(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        candidate,
    ) else ""


def reconcile_initial_version(
    operation: Mapping[str, Any],
    *,
    confirmed_operation_id: str,
    confirmed_resource_plan_digest: str,
    confirmed_account_id: str,
    allow_external_changes: bool,
    runner: Callable[..., Any] = subprocess.run,
    root: Optional[Path] = None,
    metadata_root: Optional[Path] = None,
    node_command: str | Path | None = None,
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
) -> dict[str, Any]:
    """操作固有tagを読み取り照合し、未公開versionを再送せず確定する。"""
    current = _confirmed_external_operation(
        operation,
        confirmed_operation_id=confirmed_operation_id,
        confirmed_resource_plan_digest=confirmed_resource_plan_digest,
        confirmed_account_id=confirmed_account_id,
        allow_external_changes=allow_external_changes,
    )
    if current.get("state") == "publish_confirmation_required" and current.get("version_id"):
        return {**current, "external_changes_enabled": True, "deployment_created": False}
    if current.get("state") != "version_reconciliation_required" and not (
        "initial_version_upload_requested" in current.get("completed_steps", [])
        and "initial_version_uploaded" not in current.get("completed_steps", [])
    ):
        raise LiteCloudSetupError(
            "未公開versionの照合が必要な状態ではありません。",
            failure_code="operation_record_state_invalid",
        )
    expected_tag = _setup_version_tag(current["operation_id"])
    if str(current.get("setup_tag") or "") != expected_tag:
        raise LiteCloudSetupError(
            "操作固有tagを確認できないため、自動再送せず停止します。",
            failure_code="version_reconciliation_required",
        )
    relay = Path(root) if root is not None else relay_root()
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    account_id = str((current.get("account") or {}).get("id") or "")
    worker_name = str((current.get("worker") or {}).get("name") or "")
    fixed_runner = _account_fixed_runner(runner, account_id)
    version_list = _package4_run(
        [node_command, str(wrangler), "versions", "list", "--name", worker_name, "--json"],
        runner=fixed_runner,
        cwd=relay,
        failure_code="version_reconciliation_required",
    )
    if int(getattr(version_list, "returncode", 1)) != 0:
        if (
            bool(getattr(version_list, "not_found", False))
            or _structured_error_codes(version_list) & {10007, 10090}
        ):
            current.update(
                state="worker_container_required",
                failed_step="",
                failure_code=None,
                last_remote_check_at=_utc_now(),
            )
            saved = _save_resource_checkpoint(
                current,
                "worker_container_required",
                metadata_root=metadata_root,
                checkpoint=checkpoint,
            )
            return {**saved, "external_changes_enabled": True, "deployment_created": False}
        raise LiteCloudSetupError(
            "Cloudflareの現在状態を読み取れませんでした。外部変更は行っていません。",
            failure_code="version_reconciliation_required",
        )
    payload = _json_output(version_list)
    if not isinstance(payload, (list, Mapping)):
        raise LiteCloudSetupError(
            "Cloudflareの照合結果が不正です。外部変更は行っていません。",
            failure_code="version_reconciliation_required",
        )
    records = payload if isinstance(payload, list) else payload.get("versions", [])
    matches: list[str] = []
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, Mapping):
                continue
            annotations = record.get("annotations")
            annotation_tag = (
                str(annotations.get("workers/tag") or "")
                if isinstance(annotations, Mapping)
                else ""
            )
            legacy_tag = str(record.get("tag") or "")
            if annotation_tag and legacy_tag and annotation_tag != legacy_tag:
                continue
            effective_tag = annotation_tag or legacy_tag
            if effective_tag != expected_tag:
                continue
            version_id = _uuid_identifier(record.get("id") or record.get("version_id"))
            if version_id:
                matches.append(version_id)
    if len(matches) != 1:
        current.update(
            state="version_reconciliation_required",
            failed_step="initial_version_reconcile",
            failure_code="version_reconciliation_required",
            last_remote_check_at=_utc_now(),
        )
        _save_resource_checkpoint(
            current,
            "version_reconciliation_unresolved",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "操作固有tagに一致する未公開versionを一意に確認できません。再送は行いません。",
            failure_code="version_reconciliation_required",
        )
    version_id = matches[0]
    worker = dict(current.get("worker") or {})
    worker["version_id"] = version_id
    current.update(
        state="publish_confirmation_required",
        worker=worker,
        version_id=version_id,
        failed_step="",
        failure_code=None,
        last_remote_check_at=_utc_now(),
    )
    for step in (
        "bootstrap_secrets_uploaded",
        "initial_version_uploaded",
        "initial_version_reconciled",
        "publish_summary_ready",
    ):
        _append_completed_step(current, step)
    saved = _save_resource_checkpoint(
        current,
        "initial_version_reconciled",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    return {**saved, "external_changes_enabled": True, "deployment_created": False}


def build_initial_publish_confirmation(operation: Mapping[str, Any]) -> dict[str, Any]:
    """公開ボタンの直前に表示する秘密なし最終確認を生成する。"""

    current = sanitize_setup_operation(operation)
    if current.get("state") != "publish_confirmation_required" or not current.get("version_id"):
        raise LiteCloudSetupError(
            "公開可能な未公開versionがありません。",
            failure_code="publish_confirmation_required",
        )
    return {
        "operation_id": current["operation_id"],
        "account": current["account"],
        "worker": current["worker"],
        "d1": current["d1"],
        "kv": current["kv"],
        "d1_schema_version": current["d1_schema_version"],
        "version_id": current["version_id"],
        "worker_url_candidate": current.get("worker_url") or "公開後に確定",
        "changes": [
            "未公開versionを100% trafficで明示公開",
            (
                "初回Cronを登録（毎日「17 3 * * *」・5分ごと「*/5 * * * *」、"
                "Cloudflareの利用量・契約により料金の可能性）"
            ),
        ],
        "fee_notice": "Cloudflareの利用量や契約によって料金が発生する可能性があります。",
        "confirmation_required": True,
        "deployment_created": False,
    }


def _enable_confirmed_worker_public_url(
    current: dict[str, Any],
    *,
    public_url_enabler: Optional[Callable[[str], Mapping[str, Any]]],
    metadata_root: Optional[Path],
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]],
) -> dict[str, Any]:
    """deployment後に確認済みWorkerのworkers.devだけを冪等有効化する。"""

    if public_url_enabler is None:
        return current
    worker_name = str((current.get("worker") or {}).get("name") or "")
    _append_completed_step(current, "worker_subdomain_enable_requested")
    _save_resource_checkpoint(
        current,
        "worker_subdomain_enable_requested",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    try:
        result = public_url_enabler(worker_name)
    except LiteCloudSetupError:
        current.update(
            state="postflight_failed",
            failed_step="worker_subdomain_enable",
            failure_code="worker_subdomain_reconciliation_required",
            can_rollback_worker=False,
        )
        _save_resource_checkpoint(
            current,
            "worker_subdomain_reconciliation_required",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise
    if not isinstance(result, Mapping) or not (
        result.get("enabled") is True and result.get("previews_enabled") is False
    ):
        current.update(
            state="postflight_failed",
            failed_step="worker_subdomain_enable",
            failure_code="worker_subdomain_reconciliation_required",
            can_rollback_worker=False,
        )
        _save_resource_checkpoint(
            current,
            "worker_subdomain_reconciliation_required",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "workers.dev公開設定を確認できません。再deployは行っていません。",
            failure_code="worker_subdomain_reconciliation_required",
        )
    current.update(state="deployed", failed_step="", failure_code=None)
    _append_completed_step(current, "worker_subdomain_enabled")
    return _save_resource_checkpoint(
        current,
        "worker_subdomain_enabled",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )


def _run_postflight_diagnostics(
    postflight: Callable[[], Mapping[str, Any]],
    *,
    attempts: int,
    retry_waiter: Callable[[float], None],
) -> dict[str, Any]:
    """公開直後の伝播中だけ上限付きで再診断し、両系統readyを同時確認する。"""

    count = max(1, min(int(attempts), 5))
    diagnostics: dict[str, Any] = {}
    for index in range(count):
        try:
            diagnostics = dict(postflight() or {})
        except Exception:
            diagnostics = {}
        if (
            diagnostics.get("state") == "ready"
            and diagnostics.get("public_ready") is True
            and diagnostics.get("owner_ready") is True
        ):
            return diagnostics
        if index + 1 < count:
            retry_waiter(1.0)
    return diagnostics


def publish_initial_version_synthetic(
    operation: Mapping[str, Any],
    *,
    confirmed_operation_id: str,
    runner: Callable[..., Any],
    postflight: Callable[[], Mapping[str, Any]],
    schedule_deployer: Callable[[], Mapping[str, Any]],
    root: Optional[Path] = None,
    metadata_root: Optional[Path] = None,
    node_command: str | Path | None = "node",
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
    public_url_enabler: Optional[Callable[[str], Mapping[str, Any]]] = None,
    postflight_attempts: int = 1,
    postflight_retry_waiter: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """専用確認後だけ合成deploymentを作り、public／owner postflightを検査する。"""

    if runner is subprocess.run:
        raise LiteCloudSetupError(
            "Package 4の検証は合成runnerでだけ実行できます。",
            failure_code="synthetic_execution_required",
        )
    current = sanitize_setup_operation(operation)
    if confirmed_operation_id != current["operation_id"]:
        raise LiteCloudSetupError(
            "確認した操作IDと実行対象が一致しません。",
            failure_code="operation_confirmation_mismatch",
        )
    if current.get("state") == "publish_reconciliation_required" or (
        "initial_version_deploy_requested" in current.get("completed_steps", [])
        and "initial_version_deployed" not in current.get("completed_steps", [])
    ):
        current.update(
            state="publish_reconciliation_required",
            failed_step="initial_version_deploy",
            failure_code="publish_reconciliation_required",
        )
        _save_resource_checkpoint(
            current,
            "publish_reconciliation_required",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "公開の完了状態をremote再診断してください。自動再実行は行いません。",
            failure_code="publish_reconciliation_required",
        )
    build_initial_publish_confirmation(current)
    relay = Path(root) if root is not None else relay_root()
    config_name = _relay_config_name(current)
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    account_id = str((current.get("account") or {}).get("id") or "")
    _confirm_synthetic_account(
        account_id=account_id,
        runner=runner,
        base=[node_command, str(wrangler)],
        cwd=relay,
    )
    command = [
        node_command,
        str(wrangler),
        "versions",
        "deploy",
        f"{current['version_id']}@100%",
        "--yes",
        "--config",
        str(relay / config_name),
        "--account-id",
        account_id,
    ]
    _append_completed_step(current, "initial_version_deploy_requested")
    _save_resource_checkpoint(
        current,
        "initial_version_deploy_requested",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    try:
        deployed = _package4_run(
            command,
            runner=runner,
            cwd=relay,
            failure_code="publish_reconciliation_required",
        )
    except LiteCloudSetupError as exc:
        current.update(
            state="publish_reconciliation_required",
            failed_step="initial_version_deploy",
            failure_code="publish_reconciliation_required",
        )
        _save_resource_checkpoint(
            current, "publish_reconciliation_required", metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise exc
    if int(getattr(deployed, "returncode", 1)) != 0:
        current.update(
            state="publish_reconciliation_required",
            failed_step="initial_version_deploy",
            failure_code="publish_reconciliation_required",
        )
        _save_resource_checkpoint(
            current, "publish_reconciliation_required", metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise LiteCloudSetupError(
            "公開の完了状態をremote再診断してください。自動再実行は行いません。",
            failure_code="publish_reconciliation_required",
        )
    deployment_id = _result_identifier(deployed, "deployment_id")
    worker = dict(current.get("worker") or {})
    worker["deployment_id"] = deployment_id
    current.update(state="deployed", worker=worker, failed_step="", failure_code=None)
    _append_completed_step(current, "initial_version_deployed")
    _save_resource_checkpoint(current, "deployed", metadata_root=metadata_root, checkpoint=checkpoint)

    _append_completed_step(current, "initial_schedules_deploy_requested")
    _save_resource_checkpoint(
        current,
        "initial_schedules_deploy_requested",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    try:
        schedules = schedule_deployer()
    except LiteCloudSetupError as exc:
        current.update(
            state="publish_reconciliation_required",
            failed_step="initial_schedules_deploy",
            failure_code="publish_reconciliation_required",
        )
        _save_resource_checkpoint(
            current,
            "initial_schedules_reconciliation_required",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise exc
    except Exception as exc:
        current.update(
            state="publish_reconciliation_required",
            failed_step="initial_schedules_deploy",
            failure_code="publish_reconciliation_required",
        )
        _save_resource_checkpoint(
            current,
            "initial_schedules_reconciliation_required",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "初回Cron登録の完了状態を確認できません。versionは再公開しません。",
            failure_code="publish_reconciliation_required",
        ) from exc
    if not isinstance(schedules, Mapping) or schedules.get("verified") is not True:
        current.update(
            state="publish_reconciliation_required",
            failed_step="initial_schedules_deploy",
            failure_code="publish_reconciliation_required",
        )
        _save_resource_checkpoint(
            current,
            "initial_schedules_reconciliation_required",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "初回Cron登録の完了を確認できません。versionは再公開しません。",
            failure_code="publish_reconciliation_required",
        )
    _append_completed_step(current, "initial_schedules_deployed")
    _save_resource_checkpoint(
        current,
        "initial_schedules_deployed",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    current = _enable_confirmed_worker_public_url(
        current,
        public_url_enabler=public_url_enabler,
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )

    diagnostics = _run_postflight_diagnostics(
        postflight,
        attempts=postflight_attempts,
        retry_waiter=postflight_retry_waiter,
    )
    ready = (
        diagnostics.get("state") == "ready"
        and diagnostics.get("public_ready") is True
        and diagnostics.get("owner_ready") is True
    )
    if not ready:
        current.update(
            state="postflight_failed",
            failed_step="postflight",
            failure_code="postflight_not_ready",
            can_rollback_worker=False,
        )
        _save_resource_checkpoint(
            current, "postflight_failed", metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise LiteCloudSetupError(
            "公開後診断がreadyになりませんでした。自動rollbackは行いません。",
            failure_code="postflight_not_ready",
        )
    current.update(state="verified", failed_step="", failure_code=None, can_rollback_worker=False)
    _append_completed_step(current, "public_postflight_ready")
    _append_completed_step(current, "owner_postflight_ready")
    return _save_resource_checkpoint(
        current, "verified", metadata_root=metadata_root, checkpoint=checkpoint
    )


def _worker_url_from_result(result: Any) -> str:
    """Wranglerの明示出力に含まれるworkers.dev URLだけを採用する。"""
    candidates: list[str] = []
    parsed = _json_output(result)

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key).lower())
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and key in {
            "url", "worker_url", "workers_dev_url", "workers_dev"
        }:
            candidates.append(value.strip())

    visit(parsed)
    for stream_name in ("stdout", "stderr"):
        stream = str(getattr(result, stream_name, "") or "")
        candidates.extend(re.findall(r"https://[A-Za-z0-9.-]+\.workers\.dev/?", stream))
    valid: list[str] = []
    for candidate in candidates:
        parsed_url = urlsplit(candidate.strip().rstrip("/"))
        host = str(parsed_url.hostname or "").lower()
        if (
            parsed_url.scheme == "https"
            and host.endswith(".workers.dev")
            and not parsed_url.username
            and not parsed_url.password
            and parsed_url.port is None
            and parsed_url.path in {"", "/"}
            and not parsed_url.query
            and not parsed_url.fragment
        ):
            valid.append(candidate.strip().rstrip("/"))
    return valid[0] if len(set(valid)) == 1 else ""


def publish_initial_version(
    operation: Mapping[str, Any],
    *,
    confirmed_operation_id: str,
    confirmed_resource_plan_digest: str,
    confirmed_account_id: str,
    allow_external_changes: bool,
    allow_publish: bool,
    postflight: Callable[[], Mapping[str, Any]],
    runner: Callable[..., Any] = subprocess.run,
    root: Optional[Path] = None,
    metadata_root: Optional[Path] = None,
    node_command: str | Path | None = None,
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
    public_url_enabler: Optional[Callable[[str], Mapping[str, Any]]] = None,
    schedule_deployer: Optional[Callable[[], Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """別の公開確認後だけ、準備済みversionを100% trafficへ公開する。"""
    current = _confirmed_external_operation(
        operation,
        confirmed_operation_id=confirmed_operation_id,
        confirmed_resource_plan_digest=confirmed_resource_plan_digest,
        confirmed_account_id=confirmed_account_id,
        allow_external_changes=allow_external_changes,
    )
    if current.get("state") == "verified":
        return {**current, "external_changes_enabled": True, "deployment_created": True}
    if not allow_publish:
        raise LiteCloudSetupError(
            "未公開versionを公開する確認が必要です。",
            failure_code="publish_confirmation_required",
        )
    account_id = str((current.get("account") or {}).get("id") or "")
    worker_name = str((current.get("worker") or {}).get("name") or "")
    confirmed_worker_url = _validated_worker_url(
        str(current.get("worker_url") or ""), worker_name
    )
    deploy_results: list[Any] = []

    def observe(command: list[str], result: Any) -> None:
        if "versions" in command and "deploy" in command:
            deploy_results.append(result)

    fixed_runner = _account_fixed_runner(runner, account_id, observe=observe)
    relay = Path(root) if root is not None else relay_root()
    if public_url_enabler is None:

        def public_url_enabler(name: str) -> Mapping[str, Any]:
            return ensure_cloudflare_worker_subdomain(
                account_id=account_id,
                worker_name=name,
                runner=fixed_runner,
                relay=relay,
                node_command=node_command,
            )

    if schedule_deployer is None:
        config_name = _relay_config_name(current)

        def schedule_deployer() -> Mapping[str, Any]:
            return ensure_cloudflare_worker_schedules(
                account_id=account_id,
                worker_name=worker_name,
                config_path=relay / config_name,
                runner=fixed_runner,
                relay=relay,
                node_command=node_command,
            )

    result = publish_initial_version_synthetic(
        current,
        confirmed_operation_id=current["operation_id"],
        runner=fixed_runner,
        postflight=postflight,
        schedule_deployer=schedule_deployer,
        root=root,
        metadata_root=metadata_root,
        node_command=node_command,
        checkpoint=checkpoint,
        public_url_enabler=public_url_enabler,
        postflight_attempts=5,
    )
    reported_url = _worker_url_from_result(deploy_results[-1]) if deploy_results else ""
    if reported_url and reported_url != confirmed_worker_url:
        failed = dict(result)
        failed.update(
            state="worker_url_recovery_required",
            failed_step="worker_url_verify",
            failure_code="worker_url_recovery_required",
        )
        save_setup_operation(failed, metadata_root=metadata_root)
        raise LiteCloudSetupError(
            "公開されたWorker URLが確認済みURLと一致しません。自動で推測・変更せず手動確認してください。",
            failure_code="worker_url_recovery_required",
        )
    result["worker_url"] = confirmed_worker_url
    saved = save_setup_operation(result, metadata_root=metadata_root)
    return {**saved, "external_changes_enabled": True, "deployment_created": True}


def _finish_reconciled_postflight(
    current: dict[str, Any],
    *,
    postflight: Callable[[], Mapping[str, Any]],
    public_url_enabler: Optional[Callable[[str], Mapping[str, Any]]],
    metadata_root: Optional[Path],
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]],
    postflight_attempts: int = 5,
    postflight_retry_waiter: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    current = _enable_confirmed_worker_public_url(
        current,
        public_url_enabler=public_url_enabler,
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    diagnostics = _run_postflight_diagnostics(
        postflight,
        attempts=postflight_attempts,
        retry_waiter=postflight_retry_waiter,
    )
    ready = (
        diagnostics.get("state") == "ready"
        and diagnostics.get("public_ready") is True
        and diagnostics.get("owner_ready") is True
    )
    if not ready:
        current.update(
            state="postflight_failed",
            failed_step="postflight",
            failure_code="postflight_not_ready",
            can_rollback_worker=False,
        )
        _save_resource_checkpoint(
            current,
            "postflight_failed",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "公開後診断がreadyになりませんでした。外部変更や再deployは行っていません。",
            failure_code="postflight_not_ready",
        )
    current.update(
        state="verified",
        failed_step="",
        failure_code=None,
        can_rollback_worker=False,
    )
    _append_completed_step(current, "public_postflight_ready")
    _append_completed_step(current, "owner_postflight_ready")
    return _save_resource_checkpoint(
        current,
        "verified",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )


def _active_deployment_match(payload: Any, version_id: str) -> Optional[dict[str, str]]:
    if isinstance(payload, Mapping) and isinstance(payload.get("deployments"), list):
        records = payload["deployments"]
    elif isinstance(payload, Mapping):
        records = [payload]
    elif isinstance(payload, list):
        records = payload
    else:
        records = []
    matches: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(record.get("versions"), list):
            continue
        deployment_id = _uuid_identifier(record.get("id") or record.get("deployment_id"))
        percentages: list[tuple[str, float]] = []
        valid = bool(deployment_id)
        for item in record["versions"]:
            if not isinstance(item, Mapping):
                valid = False
                break
            item_version = _uuid_identifier(item.get("version_id") or item.get("id"))
            percentage = item.get("percentage")
            if not item_version or isinstance(percentage, bool) or not isinstance(percentage, (int, float)):
                valid = False
                break
            percentages.append((item_version, float(percentage)))
        if not valid or len(percentages) == 0:
            continue
        target = [item for item in percentages if item[0] == version_id and item[1] == 100.0]
        if len(target) == 1 and all(
            item_version == version_id or percentage == 0.0
            for item_version, percentage in percentages
        ):
            matches.append({"deployment_id": deployment_id, "version_id": version_id})
    return matches[0] if len(matches) == 1 else None


def resume_initial_schedules(
    operation: Mapping[str, Any],
    *,
    confirmed_operation_id: str,
    confirmed_resource_plan_digest: str,
    confirmed_account_id: str,
    allow_external_changes: bool,
    schedule_deployer: Optional[Callable[[], Mapping[str, Any]]] = None,
    runner: Callable[..., Any] = subprocess.run,
    root: Optional[Path] = None,
    metadata_root: Optional[Path] = None,
    node_command: str | Path | None = None,
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
) -> dict[str, Any]:
    """公開済みversionを再deployせず、初回Cron登録だけを明示再開する。

    公開確認とaccount固定を再確認した後、active deploymentをGETで照合し、
    ``schedule_deployer`` のGET→不足分PUT→GETだけを実行する。通常の
    ``reconcile_initial_publish_or_postflight`` から暗黙に呼び出してはならない。
    """

    current = _confirmed_external_operation(
        operation,
        confirmed_operation_id=confirmed_operation_id,
        confirmed_resource_plan_digest=confirmed_resource_plan_digest,
        confirmed_account_id=confirmed_account_id,
        allow_external_changes=allow_external_changes,
    )
    if "initial_schedules_deployed" in current.get("completed_steps", []):
        return {**current, "external_changes_enabled": True, "deployment_created": True}
    if current.get("state") not in {
        "publish_reconciliation_required",
        "deployed",
        "postflight_failed",
    } or "initial_version_deployed" not in current.get("completed_steps", []):
        raise LiteCloudSetupError(
            "初回Cron登録を再開できる公開状態ではありません。",
            failure_code="operation_record_state_invalid",
        )
    version_id = _uuid_identifier(current.get("version_id"))
    if not version_id:
        raise LiteCloudSetupError(
            "照合対象version IDが不正です。再deployは行いません。",
            failure_code="publish_reconciliation_required",
        )
    relay = Path(root) if root is not None else relay_root()
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    account_id = str((current.get("account") or {}).get("id") or "")
    worker_name = str((current.get("worker") or {}).get("name") or "")
    fixed_runner = _account_fixed_runner(runner, account_id)
    payload = _reconciliation_json(
        [node_command, str(wrangler), "deployments", "status", "--name", worker_name, "--json"],
        runner=fixed_runner,
        cwd=relay,
        failure_code="publish_reconciliation_required",
    )
    match = _active_deployment_match(payload, version_id)
    if match is None:
        current.update(
            state="publish_reconciliation_required",
            failed_step="initial_version_deploy_reconcile",
            failure_code="publish_reconciliation_required",
            last_remote_check_at=_utc_now(),
        )
        _save_resource_checkpoint(
            current,
            "publish_reconciliation_unresolved",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "対象versionが100% activeであることを確認できません。Cron登録は行いません。",
            failure_code="publish_reconciliation_required",
        )

    if schedule_deployer is None:
        config_name = _relay_config_name(current)

        def schedule_deployer() -> Mapping[str, Any]:
            return ensure_cloudflare_worker_schedules(
                account_id=account_id,
                worker_name=worker_name,
                config_path=relay / config_name,
                runner=fixed_runner,
                relay=relay,
                node_command=node_command,
            )

    current.update(
        state="publish_reconciliation_required",
        failed_step="initial_schedules_deploy",
        failure_code="publish_reconciliation_required",
        last_remote_check_at=_utc_now(),
    )
    _append_completed_step(current, "initial_schedules_deploy_requested")
    _save_resource_checkpoint(
        current,
        "initial_schedules_deploy_requested",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    try:
        schedules = schedule_deployer()
    except LiteCloudSetupError:
        _save_resource_checkpoint(
            current,
            "initial_schedules_reconciliation_required",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise
    except Exception as exc:
        _save_resource_checkpoint(
            current,
            "initial_schedules_reconciliation_required",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "初回Cron登録の完了状態を確認できません。versionは再公開しません。",
            failure_code="publish_reconciliation_required",
        ) from exc
    if not isinstance(schedules, Mapping) or schedules.get("verified") is not True:
        _save_resource_checkpoint(
            current,
            "initial_schedules_reconciliation_required",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "初回Cron登録の完了を確認できません。versionは再公開しません。",
            failure_code="publish_reconciliation_required",
        )

    worker = dict(current.get("worker") or {})
    worker["deployment_id"] = match["deployment_id"]
    current.update(
        state="deployed",
        worker=worker,
        failed_step="",
        failure_code=None,
        last_remote_check_at=_utc_now(),
    )
    _append_completed_step(current, "initial_schedules_deployed")
    _append_completed_step(current, "initial_publish_reconciled")
    saved = _save_resource_checkpoint(
        current,
        "initial_schedules_deployed",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    return {**saved, "external_changes_enabled": True, "deployment_created": True}


def reconcile_initial_publish_or_postflight(
    operation: Mapping[str, Any],
    *,
    confirmed_operation_id: str,
    confirmed_resource_plan_digest: str,
    confirmed_account_id: str,
    allow_external_changes: bool,
    postflight: Callable[[], Mapping[str, Any]],
    runner: Callable[..., Any] = subprocess.run,
    root: Optional[Path] = None,
    metadata_root: Optional[Path] = None,
    node_command: str | Path | None = None,
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
    public_url_enabler: Optional[Callable[[str], Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """公開状態を読み取り照合し、再deployせずpostflightだけを完了する。

    初回Cron登録が未確定の場合は、ここでは外部変更を行わず明示的な
    ``resume_initial_schedules`` に戻す。読み取り照合関数へ暗黙のPUTを混ぜない。
    """
    current = _confirmed_external_operation(
        operation,
        confirmed_operation_id=confirmed_operation_id,
        confirmed_resource_plan_digest=confirmed_resource_plan_digest,
        confirmed_account_id=confirmed_account_id,
        allow_external_changes=allow_external_changes,
    )
    if current.get("state") == "verified":
        return {**current, "external_changes_enabled": True, "deployment_created": True}
    if current.get("state") not in {
        "publish_reconciliation_required",
        "deployed",
        "postflight_failed",
    } and not (
        "initial_version_deploy_requested" in current.get("completed_steps", [])
        and "initial_version_deployed" not in current.get("completed_steps", [])
    ):
        raise LiteCloudSetupError(
            "公開状態の照合が必要な状態ではありません。",
            failure_code="operation_record_state_invalid",
        )
    version_id = _uuid_identifier(current.get("version_id"))
    if not version_id:
        raise LiteCloudSetupError(
            "照合対象version IDが不正です。再deployは行いません。",
            failure_code="publish_reconciliation_required",
        )
    relay = Path(root) if root is not None else relay_root()
    node_command, wrangler, _wrangler_cli, _runtime = _command_runtime(
        relay, node_command=node_command, runner=runner
    )
    account_id = str((current.get("account") or {}).get("id") or "")
    worker_name = str((current.get("worker") or {}).get("name") or "")
    fixed_runner = _account_fixed_runner(runner, account_id)
    if public_url_enabler is None:

        def public_url_enabler(name: str) -> Mapping[str, Any]:
            return ensure_cloudflare_worker_subdomain(
                account_id=account_id,
                worker_name=name,
                runner=fixed_runner,
                relay=relay,
                node_command=node_command,
            )

    payload = _reconciliation_json(
        [node_command, str(wrangler), "deployments", "status", "--name", worker_name, "--json"],
        runner=fixed_runner,
        cwd=relay,
        failure_code="publish_reconciliation_required",
    )
    match = _active_deployment_match(payload, version_id)
    if match is None:
        current.update(
            state="publish_reconciliation_required",
            failed_step="initial_version_deploy_reconcile",
            failure_code="publish_reconciliation_required",
            last_remote_check_at=_utc_now(),
        )
        _save_resource_checkpoint(
            current,
            "publish_reconciliation_unresolved",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "対象versionが100% activeであることを一意に確認できません。再deployは行いません。",
            failure_code="publish_reconciliation_required",
        )
    worker = dict(current.get("worker") or {})
    worker["deployment_id"] = match["deployment_id"]
    current.update(
        state="deployed",
        worker=worker,
        failed_step="",
        failure_code=None,
        last_remote_check_at=_utc_now(),
    )
    _append_completed_step(current, "initial_version_deployed")
    if "initial_schedules_deployed" not in current.get("completed_steps", []):
        current.update(
            state="publish_reconciliation_required",
            failed_step="initial_schedules_reconcile",
            failure_code="publish_reconciliation_required",
        )
        _save_resource_checkpoint(
            current,
            "initial_version_reconciled",
            metadata_root=metadata_root,
            checkpoint=checkpoint,
        )
        raise LiteCloudSetupError(
            "公開済みversionを確認しました。初回Cron登録が未確認のため、Cron登録の再開を明示してください。",
            failure_code="publish_reconciliation_required",
        )
    _append_completed_step(current, "initial_publish_reconciled")
    _save_resource_checkpoint(
        current,
        "initial_publish_reconciled",
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    saved = _finish_reconciled_postflight(
        current,
        postflight=postflight,
        public_url_enabler=public_url_enabler,
        metadata_root=metadata_root,
        checkpoint=checkpoint,
    )
    return {**saved, "external_changes_enabled": True, "deployment_created": True}


def _connection_secret_values(values: Mapping[str, str]) -> tuple[str, str]:
    owner = str(values.get("OWNER_AUTH_TOKEN") or "").strip()
    signing = str(values.get("BUNDLE_SIGNING_KEY") or "").strip()
    if len(owner) < 16 or len(signing) < 16 or owner == signing:
        raise LiteCloudSetupError(
            "本体接続に必要な秘密値を確認できません。勝手に再生成しません。",
            failure_code="local_secret_recovery_required",
        )
    return owner, signing


def complete_lite_setup_synthetic(
    operation: Mapping[str, Any],
    *,
    confirmed_operation_id: str,
    connection_secrets: Mapping[str, str],
    worker_url: str,
    save_connection: Callable[..., Any],
    register_provider: Callable[[], Mapping[str, Any]],
    save_initial_route: Callable[..., Any],
    diagnose_four_states: Callable[[], Mapping[str, Any]],
    pair_device: Callable[[], Mapping[str, Any]],
    prepare_standby: Callable[[], Mapping[str, Any]],
    metadata_root: Optional[Path] = None,
    checkpoint: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
) -> dict[str, Any]:
    """合成依存だけで既存Lite導線へ合流し、standby_readyまで進める。"""

    current = sanitize_setup_operation(operation)
    if confirmed_operation_id != current["operation_id"]:
        raise LiteCloudSetupError(
            "確認した操作IDと実行対象が一致しません。",
            failure_code="operation_confirmation_mismatch",
        )
    resumable_states = {
        "verified",
        "local_secret_recovery_required",
        "connected",
        "provider_ready",
        "paired",
        "standby_ready",
    }
    if current.get("state") not in resumable_states or current.get("mode") not in SETUP_MODES:
        raise LiteCloudSetupError(
            "公開後の接続準備が完了していません。",
            failure_code="operation_record_state_invalid",
        )
    if current.get("state") == "standby_ready":
        return current

    def stop(state: str, step: str, code: str, message: str) -> None:
        current.update(state=state, failed_step=step, failure_code=code)
        _save_resource_checkpoint(
            current, code, metadata_root=metadata_root, checkpoint=checkpoint
        )
        raise LiteCloudSetupError(message, failure_code=code)

    if current.get("state") in {"verified", "local_secret_recovery_required"}:
        parsed_worker_url = urlsplit(str(worker_url or "").strip())
        if (
            parsed_worker_url.scheme != "https"
            or not parsed_worker_url.netloc
            or parsed_worker_url.username
            or parsed_worker_url.password
            or parsed_worker_url.path not in {"", "/"}
            or parsed_worker_url.query
            or parsed_worker_url.fragment
        ):
            stop(
                "verified",
                "connection_settings_save",
                "connection_save_failed",
                "Lite用クラウドのURLが不正です。",
            )
        try:
            owner_token, signing_key = _connection_secret_values(connection_secrets)
        except LiteCloudSetupError:
            stop(
                "local_secret_recovery_required",
                "local_connection_secrets",
                "local_secret_recovery_required",
                "本体接続用の秘密値を復旧してから再開してください。勝手に再生成しません。",
            )
        try:
            saved = save_connection(
                worker_url=str(worker_url or ""),
                owner_token=owner_token,
                bundle_signing_key=signing_key,
                wrangler_config_path=str(current.get("config_path") or ""),
            )
        except Exception:
            stop(
                "verified",
                "connection_settings_save",
                "connection_save_failed",
                "本体接続設定を保存できませんでした。",
            )
        if saved is False:
            stop(
                "verified",
                "connection_settings_save",
                "connection_save_failed",
                "本体接続設定を保存できませんでした。",
            )
        current.update(
            state="connected",
            worker_url=str(worker_url or "").strip().rstrip("/"),
            failed_step="",
            failure_code=None,
        )
        _append_completed_step(current, "connection_settings_saved")
        _save_resource_checkpoint(
            current, "connected", metadata_root=metadata_root, checkpoint=checkpoint
        )

    if current.get("state") == "connected":
        try:
            provider_result = dict(register_provider() or {})
        except Exception:
            stop(
                "connected",
                "provider_registration",
                "provider_registration_failed",
                "選択したAIサービスを登録できませんでした。",
            )
        profile_id = str(provider_result.get("credential_profile_id") or "").strip()
        model_id = str(provider_result.get("model_id") or "").strip()
        if provider_result.get("registered") is not True or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{2,99}", profile_id
        ) or not model_id or len(model_id) > 200:
            stop(
                "connected",
                "provider_registration",
                "provider_registration_failed",
                "AIサービス登録の完了を確認できませんでした。",
            )
        try:
            route_saved = save_initial_route(
                credential_profile_id=profile_id,
                model_id=model_id,
            )
        except Exception:
            route_saved = False
        if route_saved is False:
            stop(
                "connected",
                "initial_route_save",
                "provider_registration_failed",
                "AIサービスの初期接続とモデルを保存できませんでした。",
            )
        current.update(
            state="provider_ready",
            credential_profile_id=profile_id,
            model_id=model_id,
            failed_step="",
            failure_code=None,
        )
        _append_completed_step(current, "provider_registered")
        _save_resource_checkpoint(
            current, "provider_ready", metadata_root=metadata_root, checkpoint=checkpoint
        )

    if "four_state_diagnostics_ready" not in current.get("completed_steps", []):
        try:
            diagnostics = dict(diagnose_four_states() or {})
        except Exception:
            diagnostics = {}
        if (
            diagnostics.get("home_state") != "ready"
            or diagnostics.get("worker_state") != "ready"
            or diagnostics.get("device_state") not in {"missing", "re_pair", "ready"}
            or diagnostics.get("standby_state") not in {"missing", "expired", "ready"}
        ):
            stop(
                "provider_ready",
                "four_state_diagnostics",
                "connection_diagnostics_failed",
                "Liteの4状態診断が接続可能になりませんでした。",
            )
        _append_completed_step(current, "four_state_diagnostics_ready")
        _save_resource_checkpoint(
            current, "four_state_diagnostics_ready", metadata_root=metadata_root, checkpoint=checkpoint
        )

    if current.get("state") == "provider_ready":
        try:
            pairing = dict(pair_device() or {})
        except Exception:
            pairing = {}
        if pairing.get("state") != "paired":
            stop(
                "provider_ready",
                "short_pairing",
                "pairing_failed",
                "短期ペアリングの完了を確認できませんでした。",
            )
        current.update(state="paired", failed_step="", failure_code=None)
        _append_completed_step(current, "short_pairing_completed")
        _save_resource_checkpoint(
            current, "paired", metadata_root=metadata_root, checkpoint=checkpoint
        )

    if current.get("state") == "paired":
        try:
            standby = dict(prepare_standby() or {})
        except Exception:
            standby = {}
        if standby.get("status") != "ready":
            stop(
                "paired",
                "standby_snapshot",
                "standby_prepare_failed",
                "最初のお出かけ前データを準備できませんでした。",
            )
        current.update(state="standby_ready", failed_step="", failure_code=None)
        _append_completed_step(current, "initial_standby_ready")
        return _save_resource_checkpoint(
            current, "standby_ready", metadata_root=metadata_root, checkpoint=checkpoint
        )
    return current
