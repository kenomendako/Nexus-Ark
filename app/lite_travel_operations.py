"""Lite relayの配布前提診断と再開可能な安全更新トランザクション。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import constants
import file_lock_utils
import lite_cloud_setup
import lite_travel


EXPECTED_API_SCHEMA = 10
EXPECTED_D1_SCHEMA = 10
EXPECTED_PWA_SCHEMA = 10
REQUEST_EXPIRY_CRON = "*/5 * * * *"
UPDATE_POSTFLIGHT_SUCCESS_STATES = frozenset({"ready", "maintenance_overdue"})
UPDATE_POSTFLIGHT_RETRYABLE_STATES = frozenset(
    {"worker_update_required", "unreachable", "unknown_schema", "asset_update_propagation_pending"}
)
UPDATE_POSTFLIGHT_RETRY_DELAYS = (2.0, 3.0, 5.0, 8.0)
UI_POSTFLIGHT_RETRY_DELAYS = (*UPDATE_POSTFLIGHT_RETRY_DELAYS, 13.0, 21.0)
UI_MANIFEST_PATH = "/static/lite-build.json"
UI_MANIFEST_FORMAT_VERSION = 1
MAX_UI_MANIFEST_BYTES = 4096
UI_ASSET_DIGEST = re.compile(r"[0-9a-f]{64}")
UPDATE_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
LOCAL_PRESENCE_LOCKING_STATES = frozenset({"armed", "active", "returning"})
CLI_FAILURE_CATEGORIES = frozenset({"authentication", "network", "unknown"})
CLI_AUTHENTICATION_CODES = frozenset({10000})
CLI_RECORDABLE_CODES = frozenset({10000})
CLI_CODE = re.compile(r"[0-9]{1,6}")
CLI_CODE_MARKER = re.compile(r"\[\s*code\s*:\s*([0-9]{1,6})\s*\]", re.I)
UPDATE_STEP_LABELS = {
    "bookmark": "復旧点の取得",
    "migration": "D1マイグレーションの適用",
    "deploy": "Workerの公開",
}
SENSITIVE_KEY = re.compile(
    r"(authorization|token|secret|password|api.?key|ciphertext|nonce|snapshot|payload|body|content|messages?|events?)",
    re.I,
)
ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|Users|mnt|root)/)")
CLI_AUTHENTICATION_MARKERS = (
    "authentication error",
    "authentication failed",
    "authorization failed",
    "unauthorized",
    "forbidden",
    "access denied",
    "permission denied",
    "not authenticated",
    "login required",
    "not logged in",
    "invalid token",
)
CLI_NETWORK_MARKERS = (
    "network error",
    "network request failed",
    "network unavailable",
    "connect timed out",
    "connect timeout",
    "connection refused",
    "connection reset",
    "connection closed",
    "fetch failed",
    "failed to fetch",
    "request timeout",
    "operation timed out",
    "getaddrinfo",
    "enotfound",
    "eai_again",
    "dns resolution failed",
    "name resolution failed",
    "socket hang up",
    "tls handshake",
    "ssl error",
    "sslerror",
    "econnrefused",
    "econnreset",
    "econnaborted",
    "ehostunreach",
    "enetunreach",
)


class LiteTravelOperationError(RuntimeError):
    pass


def relay_root() -> Path:
    return Path(__file__).resolve().parent / "cloud" / "lite-relay"


def runtime_diagnostics(*, run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    return lite_cloud_setup.distribution_preflight(
        root=relay_root(),
        runner=run,
        check_network=False,
        require_wrangler=True,
    )


def _database_name_from_config(config_path: Path) -> str:
    """実運用設定から、単一の安全なD1名だけを返す。"""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        bindings = config.get("d1_databases") if isinstance(config, dict) else None
        if not isinstance(bindings, list) or len(bindings) != 1:
            return ""
        name = str((bindings[0] or {}).get("database_name") or "").strip()
        return name if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,99}", name) else ""
    except (OSError, ValueError, TypeError, lite_travel.LiteTravelError):
        return ""


def _project_relative_config_path(config_path: Path) -> str:
    project_root = Path(lite_travel.__file__).resolve().parent
    try:
        return config_path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return config_path.as_posix()


def _config_matches_current_worker(config_path: Path, worker_url: str) -> bool:
    """生成済み設定が、現在保存されているworkers.dev接続先と一致するか確認する。"""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        worker_name = str(config.get("name") or "").strip().lower()
        from urllib.parse import urlsplit

        hostname = str(urlsplit(str(worker_url or "")).hostname or "").lower()
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return bool(
        worker_name
        and _database_name_from_config(config_path)
        and hostname.startswith(f"{worker_name}.")
        and hostname.endswith(".workers.dev")
    )


def configured_wrangler_config_path() -> str:
    """保存先が旧既定名でも、残っている生成済み実運用設定を安全に見つけ直す。"""
    settings = lite_travel.get_settings() or {}
    configured = str(settings.get("wrangler_config_path") or "").strip()
    try:
        return _project_relative_config_path(
            lite_travel._resolve_wrangler_config(configured)[1]
        )
    except lite_travel.LiteTravelError:
        pass

    worker_url = str(settings.get("worker_url") or "").strip().rstrip("/")
    candidates: list[Path] = []
    try:
        operation = lite_cloud_setup.resume_latest_setup_operation() or {}
        operation_path = str(operation.get("config_path") or "").strip()
        if operation_path:
            candidates.append(Path(lite_travel._resolve_wrangler_config(operation_path)[1]))
    except (
        OSError,
        ValueError,
        TypeError,
        lite_cloud_setup.LiteCloudSetupError,
        lite_travel.LiteTravelError,
    ):
        pass
    candidates.extend(sorted(relay_root().glob("wrangler.setup.*.jsonc")))

    matches: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.resolve().as_posix().lower()
        if key in seen:
            continue
        seen.add(key)
        if _config_matches_current_worker(candidate, worker_url):
            matches.append(candidate)
    if len(matches) != 1:
        return ""
    return _project_relative_config_path(matches[0])


def configured_database_name() -> str:
    """保存済み、または安全に復元したWrangler設定から更新対象D1名を返す。"""
    try:
        config_path = configured_wrangler_config_path()
        if not config_path:
            return ""
        resolved = lite_travel._resolve_wrangler_config(config_path)[1]
        return _database_name_from_config(resolved)
    except lite_travel.LiteTravelError:
        return ""


def _normalize_worker_url(value: Any) -> str:
    """更新対象のWorker originを正規化し、認証情報や任意pathを捨てる。"""
    raw = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return ""
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _validate_ui_metadata(value: Any, *, label: str = "Lite UI版情報") -> dict[str, str | int]:
    """local/remote manifestの秘密を含まない最小契約を検証する。"""
    from lite_ui_build import parse_ui_metadata

    try:
        return parse_ui_metadata(value)
    except ValueError as exc:
        raise LiteTravelOperationError(f"{label}の形式が未対応です。") from exc


def _local_ui_metadata() -> dict[str, str | int]:
    """親側のUI資産メタデータ生成器を遅延importする。"""
    try:
        from lite_ui_build import local_ui_metadata
    except (ImportError, AttributeError) as exc:
        raise LiteTravelOperationError(
            "本体にLite UI版情報の生成機能がありません。Nexus Arkを更新してください。"
        ) from exc
    try:
        value = local_ui_metadata()
    except Exception as exc:
        raise LiteTravelOperationError("Lite UI版情報を生成できませんでした。") from exc
    return _validate_ui_metadata(value, label="本体のLite UI版情報")


def _ui_manifest_url(worker_url: str) -> str:
    return f"{worker_url.rstrip('/')}{UI_MANIFEST_PATH}"


def _fetch_ui_manifest(
    worker_url: str,
    *,
    request_get: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """認証情報を送らず、Workerの公開Lite UI manifestだけを取得する。"""
    url = _ui_manifest_url(worker_url)
    getter = request_get or lite_travel.requests.get
    try:
        response = getter(url, timeout=10, allow_redirects=False, stream=True)
    except TypeError:
        # 合成runnerや旧adapterがstream引数を受けない場合も、後段のbyte上限は維持する。
        try:
            response = getter(url, timeout=10, allow_redirects=False)
        except Exception:
            return {"state": "check_failed", "url": url}
    except Exception:
        return {"state": "check_failed", "url": url}
    try:
        return _read_ui_manifest_response(response, url)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _read_ui_manifest_response(response: Any, url: str) -> dict[str, Any]:
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status in {404, 410}:
        return {"state": "missing", "url": url}
    if status < 200 or status >= 300:
        return {"state": "check_failed", "url": url}
    try:
        headers = getattr(response, "headers", {})
        content_length = headers.get("Content-Length") if hasattr(headers, "get") else None
        if content_length is not None and int(content_length) > MAX_UI_MANIFEST_BYTES:
            return {"state": "invalid", "url": url}
        raw = None
        iterator = getattr(response, "iter_content", None)
        if callable(iterator):
            chunks: list[bytes] = []
            size = 0
            for chunk in iterator(chunk_size=1024):
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                if not isinstance(chunk, (bytes, bytearray)):
                    raise ValueError("manifest_chunk_invalid")
                size += len(chunk)
                if size > MAX_UI_MANIFEST_BYTES:
                    return {"state": "invalid", "url": url}
                chunks.append(bytes(chunk))
            raw = b"".join(chunks)
        else:
            raw = getattr(response, "content", None)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if isinstance(raw, (bytes, bytearray)):
                if len(raw) > MAX_UI_MANIFEST_BYTES:
                    return {"state": "invalid", "url": url}
                raw = bytes(raw)
        if isinstance(raw, bytes):
            body = json.loads(raw.decode("utf-8"))
        else:
            body = response.json()
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_UI_MANIFEST_BYTES:
                return {"state": "invalid", "url": url}
        manifest = _validate_ui_metadata(body, label="Lite用クラウドのUI版情報")
    except Exception:
        return {"state": "invalid", "url": url}
    return {
        "state": "ready",
        "url": url,
        "manifest": manifest,
        "ui_build": manifest["ui_build"],
        "asset_digest": manifest["asset_digest"],
    }


def _remote_update_state(remote: dict[str, Any]) -> str:
    """diagnose_workerのhealthとowner診断を更新可否用の状態へ統合する。"""
    if not isinstance(remote, dict):
        return "unknown_schema"
    # セッション中の更新停止は、API/D1互換番号より先に判定する。古い
    # Workerでも診断情報が返る限り、利用中のルームを更新対象にしない。
    if remote.get("state") == "active_session_blocked":
        return "active_session_blocked"
    if remote.get("state") in {
        "unauthorized", "secret_action_required", "client_update_required", "unknown_schema", "unreachable",
    }:
        return str(remote["state"])
    health = remote.get("health")
    diagnostics = remote.get("diagnostics")
    if isinstance(diagnostics, dict) and "resources" in diagnostics:
        resources = diagnostics.get("resources")
        if resources is None:
            resources = {}
        if not isinstance(resources, dict):
            return "unknown_schema"
        counts: dict[str, int] = {}
        for key in ("active_sessions", "returning_sessions"):
            value = resources.get(key, 0)
            if value is None:
                value = 0
            # JSONのboolはPythonではintのサブクラスなので、明示的に拒否する。
            if type(value) is not int or value < 0:
                return "unknown_schema"
            counts[key] = value
        if counts["active_sessions"] or counts["returning_sessions"]:
            return "active_session_blocked"
    if isinstance(health, dict):
        pwa_schema = health.get("pwa_schema_version")
        if pwa_schema is not None:
            if type(pwa_schema) is not int or pwa_schema < 1:
                return "unknown_schema"
            if pwa_schema > EXPECTED_PWA_SCHEMA:
                return "client_update_required"
        effective_health = dict(health)
        if not isinstance(effective_health.get("d1_schema_version"), int):
            d1 = (diagnostics or {}).get("d1") if isinstance(diagnostics, dict) else None
            if isinstance(d1, dict) and type(d1.get("d1_schema_version")) is int:
                effective_health["d1_schema_version"] = d1["d1_schema_version"]
        api_schema = effective_health.get("api_schema_version")
        d1_schema = effective_health.get("d1_schema_version")
        if api_schema is not None and type(api_schema) is not int:
            return "unknown_schema"
        if d1_schema is not None and type(d1_schema) is not int:
            return "unknown_schema"
        if type(api_schema) is int and type(d1_schema) is int:
            try:
                state = compatibility_state(
                    effective_health,
                    diagnostics if isinstance(diagnostics, dict) else None,
                )
                if state in UPDATE_POSTFLIGHT_SUCCESS_STATES and pwa_schema is not None and pwa_schema < EXPECTED_PWA_SCHEMA:
                    return "worker_update_required"
                return state
            except (AttributeError, TypeError, ValueError):
                return "unknown_schema"
    return str(remote.get("state") or "unknown_schema")


def _presence_state(
    room_name: str,
    presence_checker: Optional[Callable[[str], Any]] = None,
) -> str:
    if not str(room_name or "").strip() and presence_checker is None:
        return ""
    try:
        if presence_checker is not None:
            try:
                value = presence_checker(str(room_name or ""))
            except TypeError:
                # テスト・UI側で引数なしの簡易checkerを渡せるようにする。
                value = presence_checker()  # type: ignore[call-arg]
        else:
            value = lite_travel.presence_status(str(room_name).strip())
    except Exception as exc:
        raise LiteTravelOperationError(
            "現在のお出かけ状態を確認できないため、Lite更新を開始しません。"
        ) from exc
    return str((value or {}).get("status") or "") if isinstance(value, dict) else ""


def _update_operation_lock_path() -> Path:
    return _operation_dir() / ".connected-update"


def _safe_operation_id(value: Any) -> str:
    operation_id = str(value or "").strip()
    if not UPDATE_OPERATION_ID.fullmatch(operation_id):
        raise LiteTravelOperationError("Lite更新操作の識別子が不正です。")
    return operation_id


def _canonical_resource_digest(
    config_path: str,
    *,
    worker_url: str,
    database_name: str,
) -> str:
    """秘密を含めず、実運用資源の同一性だけをdigest化する。"""
    try:
        _root, resolved_path = lite_travel._resolve_wrangler_config(config_path)
        config = json.loads(
            resolved_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_config_object,
        )
    except (OSError, ValueError, TypeError, AttributeError, lite_travel.LiteTravelError) as exc:
        raise LiteTravelOperationError(
            "Lite用クラウドの実運用資源設定を確認できません。"
        ) from exc
    if not isinstance(config, dict):
        raise LiteTravelOperationError("Lite用クラウドの実運用資源設定が不正です。")
    d1_databases = config.get("d1_databases")
    if not isinstance(d1_databases, list) or len(d1_databases) != 1 or not isinstance(d1_databases[0], dict):
        raise LiteTravelOperationError("Lite用クラウドのD1設定を一意に確認できません。")
    d1 = d1_databases[0]
    configured_name = str(d1.get("database_name") or "").strip()
    configured_id = str(d1.get("database_id") or "").strip().lower()
    if configured_name != database_name or not configured_id:
        raise LiteTravelOperationError("Lite用クラウドのD1実体が接続設定と一致しません。")
    worker_name = str(config.get("name") or "").strip()
    if not worker_name:
        raise LiteTravelOperationError("Lite用クラウドのWorker名を確認できません。")
    kv_namespaces = config.get("kv_namespaces")
    canonical_kv: list[dict[str, str]] = []
    if isinstance(kv_namespaces, list):
        for item in kv_namespaces:
            if not isinstance(item, dict):
                raise LiteTravelOperationError("Lite用クラウドのKV設定が不正です。")
            canonical_kv.append(
                {
                    "binding": str(item.get("binding") or "").strip(),
                    "id": str(item.get("id") or "").strip().lower(),
                }
            )
    try:
        setup = lite_cloud_setup.resume_latest_setup_operation() or {}
    except (OSError, ValueError, TypeError, lite_cloud_setup.LiteCloudSetupError) as exc:
        raise LiteTravelOperationError(
            "Lite用クラウドの資源操作記録を確認できません。"
        ) from exc
    setup_account = setup.get("account") if isinstance(setup, dict) and isinstance(setup.get("account"), dict) else {}
    setup_worker = setup.get("worker") if isinstance(setup, dict) and isinstance(setup.get("worker"), dict) else {}
    setup_d1 = setup.get("d1") if isinstance(setup, dict) and isinstance(setup.get("d1"), dict) else {}
    setup_bindings = setup.get("bindings") if isinstance(setup, dict) and isinstance(setup.get("bindings"), dict) else {}
    contract = {
        "contract_version": 1,
        "config_path": str(config_path).replace("\\", "/").strip(),
        "worker_url": worker_url,
        "worker": {
            "name": worker_name,
            "workers_dev": bool(config.get("workers_dev")),
            "account_id": str(config.get("account_id") or "").strip().lower(),
            "compatibility_date": str(config.get("compatibility_date") or "").strip(),
            "main": str(config.get("main") or "").strip(),
        },
        "d1": {
            "binding": str(d1.get("binding") or "").strip(),
            "database_name": configured_name,
            "database_id": configured_id,
        },
        "kv_namespaces": sorted(canonical_kv, key=lambda item: (item["binding"], item["id"])),
        "setup": {
            "account_id": str((setup_account or {}).get("id") or "").strip().lower(),
            "worker_name": str((setup_worker or {}).get("name") or "").strip(),
            "d1_id": str((setup_d1 or {}).get("id") or "").strip().lower(),
            "d1_name": str((setup_d1 or {}).get("name") or "").strip(),
            "d1_binding": str((setup_bindings or {}).get("d1") or "").strip(),
        },
    }
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _update_connection_binding(
    *,
    target_digest: str,
    worker_url: Optional[str] = None,
) -> dict[str, str]:
    """現在の接続先・生成済み設定・D1名を秘密なしで固定する。"""
    settings = lite_travel.get_settings() or {}
    current_worker_url = _normalize_worker_url(
        worker_url if worker_url is not None else settings.get("worker_url")
    )
    config_path = configured_wrangler_config_path()
    database_name = configured_database_name()
    digest = str(target_digest or "").strip().lower()
    if not current_worker_url or not config_path or not database_name or not UI_ASSET_DIGEST.fullmatch(digest):
        raise LiteTravelOperationError("Lite用クラウドの接続設定または更新対象を確認できません。")
    resource_digest = _canonical_resource_digest(
        config_path,
        worker_url=current_worker_url,
        database_name=database_name,
    )
    serialized = json.dumps(
        {
            "worker_url": current_worker_url,
            "config_path": config_path,
            "database_name": database_name,
            "target_digest": digest,
            "resource_digest": resource_digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "worker_url": current_worker_url,
        "database_name": database_name,
        "resource_digest": resource_digest,
        "connection_fingerprint": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def _assert_update_binding(
    binding: dict[str, Any],
    *,
    target_digest: str,
) -> dict[str, str]:
    current = _update_connection_binding(target_digest=target_digest)
    expected_worker = _normalize_worker_url(binding.get("worker_url"))
    expected_database = str(binding.get("database_name") or "").strip()
    expected_resource_digest = str(binding.get("resource_digest") or "").strip().lower()
    expected_fingerprint = str(binding.get("connection_fingerprint") or "").strip().lower()
    if (
        not expected_worker
        or expected_worker != current["worker_url"]
        or expected_database != current["database_name"]
        or not hmac_compare(expected_resource_digest, current["resource_digest"])
        or not hmac_compare(expected_fingerprint, current["connection_fingerprint"])
    ):
        raise LiteTravelOperationError(
            "Lite更新の接続先または保存領域が変わりました。状態を確認してから再計画してください。"
        )
    return current


def hmac_compare(left: str, right: str) -> bool:
    """比較値を固定時間で照合する小さな依存なしhelper。"""
    import hmac

    return bool(left and right and hmac.compare_digest(left, right))


def prerequisite_message(diagnostic: dict[str, Any]) -> str:
    """更新前提の不足を、同梱runtimeの利用者向け表現へ変換する。"""
    failures = set(diagnostic.get("failure_codes") or [])
    if diagnostic.get("runtime_source") == "development":
        return (
            "開発用Lite実行環境を確認できません。開発用Nodeの指定・版と、"
            "Lite用Wranglerとlockの一致を確認してください。"
        )
    reasons: list[str] = []
    if "node_22_required" in failures:
        reasons.append("同梱されたLiteの準備ツールの版を確認できません")
    if "npm_missing" in failures:
        reasons.append("同梱されたLiteの準備ツールが不足しています")
    if "relay_resources_missing" in failures:
        reasons.append("配布relay資源が不足しています")
    if "wrangler_missing" in failures:
        reasons.append("同梱されたLiteの準備ツールが不足しています")
    if "wrangler_version_mismatch" in failures:
        reasons.append("同梱されたLiteの準備ツールの版が一致しません")
    return "、".join(reasons) or "更新前の準備状態を確認できません"


def compatibility_state(health: dict[str, Any], diagnostics: Optional[dict[str, Any]] = None) -> str:
    api_schema = health.get("api_schema_version")
    d1_schema = health.get("d1_schema_version")
    if not isinstance(api_schema, int) or not isinstance(d1_schema, int):
        return "unknown_schema"
    if api_schema > EXPECTED_API_SCHEMA or d1_schema > EXPECTED_D1_SCHEMA:
        return "client_update_required"
    if d1_schema < EXPECTED_D1_SCHEMA:
        return "migration_required"
    if api_schema < EXPECTED_API_SCHEMA:
        return "worker_update_required"
    if diagnostics:
        active = int((diagnostics.get("resources") or {}).get("active_sessions") or 0)
        returning = int((diagnostics.get("resources") or {}).get("returning_sessions") or 0)
        if active or returning:
            return "active_session_blocked"
        return str(diagnostics.get("state") or "unknown_schema")
    return "ready"


def diagnostic_export() -> dict[str, str]:
    payload = _redact({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "local": runtime_diagnostics(),
        "remote": lite_travel.diagnose_worker(),
    })
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    remote_state = str((payload.get("remote") or {}).get("state") or "unknown")
    local_state = str((payload.get("local") or {}).get("state") or "unknown")
    markdown = (
        "# Lite Travel Phase 5 診断\n\n"
        f"- Local: `{local_state}`\n"
        f"- Remote: `{remote_state}`\n\n"
        "本文、snapshot、Token、Secret、Authorization、絶対パスは含めていません。\n"
    )
    return {"json": serialized, "markdown": markdown}


def _operation_dir() -> Path:
    return Path(constants.METADATA_DIR) / "lite_travel" / "operations"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items() if not SENSITIVE_KEY.search(str(key))}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return "[REDACTED]" if ABSOLUTE_PATH.search(value) else value[:1000]
    return value


def _command_output(value: Any) -> str:
    """CLI出力を分類用にだけ文字列化し、保存対象にしない。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _safe_cli_code(value: Any) -> Optional[int]:
    """CLI出力のcode欄から、意味を確認済みの整数だけを記録用に取り出す。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        code = value
    elif isinstance(value, str) and CLI_CODE.fullmatch(value.strip()):
        code = int(value.strip())
    else:
        return None
    return code if code in CLI_RECORDABLE_CODES else None


def _cli_error_codes(result: Any) -> list[int]:
    """JSONのcode欄またはWranglerのcode markerだけを安全に抽出する。"""
    codes: set[int] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key) == "code":
                    code = _safe_cli_code(item)
                    if code is not None:
                        codes.add(code)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for stream_name in ("stdout", "stderr"):
        stream = _command_output(getattr(result, stream_name, ""))
        try:
            visit(json.loads(stream))
        except (TypeError, ValueError):
            pass
        for marker in CLI_CODE_MARKER.finditer(stream):
            code = _safe_cli_code(marker.group(1))
            if code is not None:
                codes.add(code)
    return sorted(codes)


def _classify_cli_failure(result: Any, *, codes: Optional[list[int]] = None) -> str:
    """CLIの生出力を保存せず、利用者向けの安全な失敗区分へ畳み込む。"""
    safe_codes = codes if codes is not None else _cli_error_codes(result)
    if any(code in CLI_AUTHENTICATION_CODES for code in safe_codes):
        return "authentication"
    output = "\n".join(
        _command_output(getattr(result, stream_name, ""))
        for stream_name in ("stdout", "stderr")
    ).casefold()
    if any(marker in output for marker in CLI_AUTHENTICATION_MARKERS):
        return "authentication"
    if any(marker in output for marker in CLI_NETWORK_MARKERS):
        return "network"
    return "unknown"


def _cli_failure_guidance(category: str) -> str:
    """Cloudflare管理用CLIの確認先を示し、Nexus Arkの資格情報と区別する。"""
    if category == "authentication":
        return (
            "Cloudflare管理APIの認証・権限エラーです。"
            "準備時に確認したCloudflare管理アカウントと、対象D1への権限を確認してください。"
            "Nexus Arkの接続TokenやAIキーの問題とは別の確認です。"
        )
    if category == "network":
        return "PCの通信状態を確認してから、保存済みの更新記録を確認してください。"
    return "詳細を特定できないCLIエラーです。保存済みの更新記録と復旧手順を確認してください。"


def _cli_failure_record(
    operation: dict[str, Any],
    *,
    step: str,
    category: str,
    codes: list[int],
) -> str:
    """CLI失敗を秘密なしで操作記録へ反映し、表示用の静的文面を返す。"""
    safe_category = category if category in CLI_FAILURE_CATEGORIES else "unknown"
    label = UPDATE_STEP_LABELS.get(step, "Lite更新処理")
    message = f"{label}に失敗しました。{_cli_failure_guidance(safe_category)}"
    operation.update(
        failed_step_label=label,
        failure_category=safe_category,
        failure_codes=list(codes),
        failure_guidance=message,
    )
    return message


def get_update_offer(
    refresh_remote: bool = True,
    *,
    request_get: Optional[Callable[..., Any]] = None,
    local_metadata: Optional[dict[str, Any]] = None,
    room_name: str = "",
    presence_checker: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    """本体と接続済みLiteのUI版を照合し、一回更新の安全な提案を返す。

    WorkerのAPI/D1互換番号は既存の診断で確認し、静的UIだけの差分は公開manifestの
    digestで判定する。返り値にはTokenやSecretを含めず、旧manifestが404の場合だけ
    更新候補として扱う。通信失敗や不正なmanifestは、誤更新を避けるため確認失敗にする。
    """
    settings = lite_travel.get_settings() or {}
    worker_url = _normalize_worker_url(settings.get("worker_url"))
    owner_token = str(settings.get("owner_token") or "").strip()
    empty: dict[str, Any] = {
        "state": "not_configured",
        "can_update": False,
        "reason": "Lite用クラウドの接続設定が未完了です。",
        "ui_build": "",
        "asset_digest": "",
        "worker_url": worker_url,
        "database_name": "",
        "resource_digest": "",
        "target_digest": "",
        "update_kind": "",
        "operation_id": None,
    }
    if not worker_url or not owner_token:
        return empty
    try:
        target = _validate_ui_metadata(
            local_metadata if local_metadata is not None else _local_ui_metadata(),
            label="本体のLite UI版情報",
        )
    except LiteTravelOperationError as exc:
        return {
            **empty,
            "state": "check_failed",
            "reason": str(exc),
        }
    try:
        binding = _update_connection_binding(
            target_digest=str(target["asset_digest"]), worker_url=worker_url
        )
    except LiteTravelOperationError as exc:
        return {
            **empty,
            "state": "not_configured",
            "reason": str(exc),
            "ui_build": target["ui_build"],
            "asset_digest": target["asset_digest"],
            "target_digest": target["asset_digest"],
        }
    base_offer = {
        "ui_build": target["ui_build"],
        "asset_digest": target["asset_digest"],
        "worker_url": binding["worker_url"],
        "database_name": binding["database_name"],
        "resource_digest": binding["resource_digest"],
        "target_digest": target["asset_digest"],
        "connection_fingerprint": binding["connection_fingerprint"],
        "manifest_url": _ui_manifest_url(binding["worker_url"]),
        "remote_ui_build": "",
        "remote_asset_digest": "",
        "remote_state": "",
        "update_kind": "",
        "operation_id": None,
        "resume_available": False,
    }
    if not refresh_remote:
        return {
            **base_offer,
            "state": "check_failed",
            "can_update": False,
            "reason": "Lite用クラウドの状態確認を更新してから再試行してください。",
        }
    presence = _presence_state(room_name, presence_checker)
    if presence in LOCAL_PRESENCE_LOCKING_STATES:
        return {
            **base_offer,
            "state": "blocked",
            "can_update": False,
            "reason": "現在のお出かけ中はLite用クラウドを更新できません。帰宅後に再試行してください。",
            "remote_state": "active_session_blocked",
        }
    try:
        remote = lite_travel.diagnose_worker()
    except Exception:
        return {
            **base_offer,
            "state": "check_failed",
            "can_update": False,
            "reason": "Lite用クラウドの状態を確認できませんでした。",
        }
    remote_state = _remote_update_state(remote)
    if remote_state in {"worker_update_required", "migration_required"} and not isinstance(remote.get("diagnostics"), dict):
        # 古い互換番号でdiagnose_workerが早期returnした場合も、クラウドの
        # 進行中セッションを所有者診断で確認してから更新候補にする。
        try:
            diagnostics = lite_travel._owner_request("GET", "/v1/admin/diagnostics", timeout=10)
            if not isinstance(diagnostics, dict) or not isinstance(diagnostics.get("resources"), dict):
                raise ValueError("resource_state_missing")
            remote = {**remote, "diagnostics": diagnostics}
            remote_state = _remote_update_state(remote)
        except Exception:
            return {
                **base_offer, "state": "check_failed", "can_update": False,
                "reason": "Lite用クラウドで進行中のデータを確認できないため、更新を開始しません。",
            }
    base_offer["remote_state"] = remote_state
    health = remote.get("health") if isinstance(remote, dict) else None
    diagnostics = remote.get("diagnostics") if isinstance(remote, dict) else None
    if remote_state == "active_session_blocked":
        return {
            **base_offer,
            "state": "blocked",
            "can_update": False,
            "reason": "Lite用クラウドでお出かけ中のデータがあるため、更新を停止しました。",
        }
    if remote_state in {"client_update_required", "unknown_schema", "unauthorized", "secret_action_required"}:
        return {
            **base_offer,
            "state": "blocked",
            "can_update": False,
            "reason": f"Lite用クラウドの互換状態を確認できないため、更新を停止しました（{remote_state}）。",
        }
    if remote_state == "unreachable":
        return {
            **base_offer,
            "state": "check_failed",
            "can_update": False,
            "reason": "Lite用クラウドへ接続できないため、更新を開始しません。",
        }
    manifest = _fetch_ui_manifest(binding["worker_url"], request_get=request_get)
    if manifest.get("state") == "check_failed" or manifest.get("state") == "invalid":
        return {
            **base_offer,
            "state": "check_failed",
            "can_update": False,
            "reason": "Lite用クラウドのUI版情報を確認できませんでした。",
        }
    if manifest.get("state") == "ready":
        base_offer["remote_ui_build"] = manifest.get("ui_build", "")
        base_offer["remote_asset_digest"] = manifest.get("asset_digest", "")
    digest_matches = (
        manifest.get("state") == "ready"
        and manifest.get("asset_digest") == target["asset_digest"]
    )
    if remote_state in {"migration_required", "worker_update_required"}:
        update_kind = "storage" if remote_state == "migration_required" else "worker"
    elif digest_matches:
        update_kind = ""
    else:
        update_kind = "assets"
    base_offer["update_kind"] = update_kind
    if remote_state in {"ready", "maintenance_overdue"} and digest_matches:
        reason = "Lite用クラウドは本体と同じUI版です。"
        if remote_state == "maintenance_overdue":
            reason += "保存期限の確認が必要です。"
        return {
            **base_offer,
            "state": "up_to_date",
            "can_update": False,
            "reason": reason,
        }
    try:
        resumable = _find_resumable_operation(
            base_offer,
            target_digest=str(target["asset_digest"]).lower(),
        )
    except LiteTravelOperationError as exc:
        return {
            **base_offer,
            "state": "blocked",
            "can_update": False,
            "reason": str(exc),
        }
    operation_id = str((resumable or {}).get("operation_id") or uuid.uuid4())
    base_offer["resume_available"] = resumable is not None
    resume_reason = "既存の反映済み操作を再確認できます。" if resumable else ""
    return {
        **base_offer,
        "state": "update_required",
        "can_update": True,
        "reason": resume_reason
        or (
            "Lite用クラウドへ本体と同じUI版を反映できます。"
            if update_kind == "assets"
            else "Lite用クラウドの保存領域・Worker更新を本体と同じ操作で実行できます。"
        ),
        "operation_id": operation_id,
    }


def _save_operation(operation: dict[str, Any]) -> None:
    operation_id = str(operation["operation_id"])
    path = _operation_dir() / f"{operation_id}.json"
    safe_operation = _redact(operation)
    # Worker originは秘密ではなく、target operationのbinding再照合に必要。
    worker_url = _normalize_worker_url(operation.get("worker_url"))
    if worker_url:
        safe_operation["worker_url"] = worker_url
    if not file_lock_utils.safe_json_write(path.as_posix(), safe_operation):
        raise LiteTravelOperationError("更新操作状態を保存できませんでした。")


def _load_operation(operation_id: str) -> Optional[dict[str, Any]]:
    """同じ提案の再実行抑止とpostflight再開に使う安全な操作記録読込。"""
    safe_id = _safe_operation_id(operation_id)
    path = _operation_dir() / f"{safe_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or str(value.get("operation_id") or "") != safe_id:
        return None
    return value


def _find_resumable_operation(
    binding: dict[str, Any],
    *,
    target_digest: str,
) -> Optional[dict[str, Any]]:
    """再読込された候補から、deploy済みpostflightだけを同一bindingで拾う。"""
    try:
        paths = sorted(
            _operation_dir().glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    candidates: list[dict[str, Any]] = []
    for path in paths:
        try:
            operation = _load_operation(path.stem)
        except LiteTravelOperationError:
            continue
        if not operation:
            continue
        if (
            operation.get("status") != "failed"
            or operation.get("failure_code") != "postflight_not_ready"
            or "deploy" not in (operation.get("completed_steps") or [])
            or str(operation.get("target_digest") or "").lower() != target_digest
        ):
            continue
        if any(
            str(operation.get(key) or "") != str(binding.get(key) or "")
            for key in ("worker_url", "database_name", "resource_digest", "connection_fingerprint")
        ):
            continue
        candidates.append(operation)
    if len(candidates) > 1:
        raise LiteTravelOperationError(
            "同じLite更新対象の未完了記録が複数あるため、自動再開を停止しました。"
        )
    return candidates[0] if candidates else None


def plan_update(
    database_name: str,
    *,
    operation_id: Optional[str] = None,
    binding: Optional[dict[str, Any]] = None,
    update_kind: str = "",
    target_digest: str = "",
) -> dict[str, Any]:
    name = str(database_name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,99}", name):
        raise LiteTravelOperationError("D1 database名が不正です。")
    safe_operation_id = _safe_operation_id(operation_id) if operation_id is not None else str(uuid.uuid4())
    if binding is not None:
        digest = str(target_digest or binding.get("target_digest") or "").strip().lower()
        if not UI_ASSET_DIGEST.fullmatch(digest):
            raise LiteTravelOperationError("Lite UIの更新対象を確認できません。")
        _assert_update_binding(binding, target_digest=digest)
    local = runtime_diagnostics()
    if local["state"] != "ready":
        raise LiteTravelOperationError(prerequisite_message(local))
    remote = lite_travel.diagnose_worker()
    remote_state = _remote_update_state(remote)
    if remote_state in {
        "unreachable",
        "unauthorized",
        "unknown_schema",
        "client_update_required",
        "active_session_blocked",
        "secret_action_required",
    }:
        raise LiteTravelOperationError(f"更新を開始できない診断状態です: {remote_state}")
    operation = {
        "operation_id": safe_operation_id,
        "status": "planned",
        "database_name": name,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "steps": ["preflight", "bookmark", "migration", "deploy", "postflight"],
        "completed_steps": [],
        "bookmark": None,
        "recovery": "失敗時は自動restoreせず、記録したTime Travel bookmarkから手動復旧する。",
    }
    if binding is not None:
        operation.update(
            {
                "worker_url": _normalize_worker_url(binding.get("worker_url")),
                "connection_fingerprint": str(binding.get("connection_fingerprint") or ""),
                "resource_digest": str(binding.get("resource_digest") or "").lower(),
                "target_digest": str(target_digest or binding.get("target_digest") or "").lower(),
                "update_kind": str(update_kind or binding.get("update_kind") or "assets"),
            }
        )
    _save_operation(operation)
    return operation


def _unique_config_object(pairs):
    """重複キーの解釈で更新先が曖昧になるJSON設定を拒否する。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_config_key")
        result[key] = value
    return result


def _load_request_signal_config(config_path: Path) -> tuple[dict[str, Any], list[str]]:
    config = json.loads(
        config_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_config_object
    )
    if not isinstance(config, dict):
        raise ValueError("invalid_config")
    flags = config.get("compatibility_flags", [])
    if not isinstance(flags, list) or any(
        not isinstance(flag, str) or not flag.strip() for flag in flags
    ):
        raise ValueError("invalid_compatibility_flags")
    if len(set(flags)) != len(flags):
        raise ValueError("duplicate_compatibility_flag")
    disable_request_signal = config.get("disable_request_signal", False)
    if not isinstance(disable_request_signal, bool):
        raise ValueError("invalid_request_signal_disable")
    if "disable_request_signal" in flags or disable_request_signal:
        raise ValueError("request_signal_disabled")
    return config, flags


def _assert_request_signal_flag_allowed(config_path: Path) -> None:
    """入力signal flagの無効指定を、設定を書き換える前に検証する。"""
    try:
        with file_lock_utils.get_file_lock(config_path.as_posix()):
            _load_request_signal_config(config_path)
    except (OSError, ValueError, TypeError, file_lock_utils.Timeout):
        raise LiteTravelOperationError(
            "通信切断の検知に必要な設定を安全に確認できませんでした。"
            "無効指定・JSON形式・書き込み権限を確認してください。クラウド更新は開始していません。"
        ) from None


def _ensure_request_signal_flag(config_path: Path) -> bool:
    """生成済みJSON設定の項目を保持し、入力Request.signal flagだけを原子的に追加する。"""
    try:
        with file_lock_utils.get_file_lock(config_path.as_posix()):
            config, flags = _load_request_signal_config(config_path)
            if "enable_request_signal" in flags:
                return False
            config["compatibility_flags"] = [*flags, "enable_request_signal"]
            if not file_lock_utils.safe_text_write(
                config_path.as_posix(), json.dumps(config, ensure_ascii=False, indent=2) + "\n"
            ):
                raise OSError("request_signal_config_write_failed")
            return True
    except (OSError, ValueError, TypeError, file_lock_utils.Timeout):
        raise LiteTravelOperationError(
            "通信切断の検知に必要な設定を安全に更新できませんでした。"
            "無効指定・JSON形式・書き込み権限を確認してください。クラウド更新は開始していません。"
        ) from None


def _ensure_request_expiry_cron(config_path: Path) -> bool:
    """生成済みJSON設定の他項目を保持し、回収Cronだけを原子的に追加する。"""
    try:
        with file_lock_utils.get_file_lock(config_path.as_posix()):
            config = json.loads(
                config_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_config_object
            )
            if not isinstance(config, dict):
                raise ValueError("invalid_config")
            triggers = config.get("triggers", {})
            if not isinstance(triggers, dict):
                raise ValueError("invalid_triggers")
            crons = triggers.get("crons", [])
            if not isinstance(crons, list) or any(
                not isinstance(cron, str) or not cron.strip() for cron in crons
            ):
                raise ValueError("invalid_crons")
            if REQUEST_EXPIRY_CRON in crons:
                return False
            config["triggers"] = {**triggers, "crons": [*crons, REQUEST_EXPIRY_CRON]}
            file_lock_utils.safe_text_write(
                config_path.as_posix(), json.dumps(config, ensure_ascii=False, indent=2) + "\n"
            )
            return True
    except (OSError, ValueError, TypeError, file_lock_utils.Timeout):
        raise LiteTravelOperationError(
            "定期回収の設定を安全に更新できませんでした。"
            "生成済み設定のJSON形式と書き込み権限を確認してください。クラウド更新は開始していません。"
        ) from None


def _checked_update_assets_root(root: Path, operation_id: str) -> Path:
    """操作cache内の実体だけへ生成し、別操作や個人ファイルへのリンクを拒否する。"""
    destination = lite_cloud_setup._operation_assets_root(root, operation_id)
    cache = root.resolve().parent.parent / "cache" / "lite_cloud_setup"
    expected = cache / str(operation_id).lower() / "public"
    if destination != expected or any(path.is_symlink() for path in (
        cache.parent, cache, expected.parent, expected,
    )):
        raise ValueError("linked_assets_directory")
    if destination.exists() and any(path.is_symlink() for path in destination.rglob("*")):
        raise ValueError("linked_asset_file")
    return destination


def _build_update_assets(
    config_path: Path, *, node: str | Path, runner: Callable[..., Any], root: Path,
    operation_id: str | None = None,
) -> None:
    """生成済み設定の配信先へ新版PWAを再生成し、deploy前に失敗を確定する。"""
    try:
        with file_lock_utils.get_file_lock(config_path.as_posix()):
            original = config_path.read_bytes()
        config = json.loads(original.decode("utf-8"), object_pairs_hook=_unique_config_object)
        relative = config["assets"]["directory"]
        if not isinstance(relative, str):
            raise ValueError("invalid_assets_directory")
        match = re.fullmatch(
            r"\.\./\.\./cache/lite_cloud_setup/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/public",
            relative,
        )
        legacy_public = relative in {"./public", "public"}
        if legacy_public:
            # 旧設定だけを移行し、署名済み配布資源のpublicは上書きしない。
            if config_path.resolve().parent != root.resolve() or (
                config_path.parent / relative
            ).resolve() != root.resolve() / "public":
                raise ValueError("legacy_assets_directory_mismatch")
            destination = _checked_update_assets_root(root, operation_id)
            updated_relative = lite_cloud_setup._operation_assets_directory(operation_id)
        else:
            if match is None:
                raise ValueError("unexpected_assets_directory")
            destination = _checked_update_assets_root(root, match.group(1))
            if (config_path.parent / relative).resolve() != destination:
                raise ValueError("assets_directory_mismatch")
        script = root / "scripts" / "build-unified-lite.mjs"
        if not script.is_file():
            raise OSError("assets_builder_missing")
        result = runner(
            [str(node), str(script), "--output-root", str(destination)],
            cwd=root.as_posix(), capture_output=True, text=True, timeout=300, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("assets_build_failed")
        with file_lock_utils.get_file_lock(config_path.as_posix()):
            if config_path.read_bytes() != original:
                raise ValueError("config_changed_during_assets_build")
            if legacy_public:
                config["assets"]["directory"] = updated_relative
                if not file_lock_utils.safe_text_write(
                    config_path.as_posix(), json.dumps(config, ensure_ascii=False, indent=2) + "\n"
                ):
                    raise OSError("assets_config_write_failed")
    except (
        OSError, ValueError, KeyError, TypeError, RuntimeError,
        subprocess.SubprocessError, lite_cloud_setup.LiteCloudSetupError, file_lock_utils.Timeout,
    ):
        raise LiteTravelOperationError(
            "新版PWAの配信資産を安全に準備できませんでした。クラウド更新は開始していません。"
        ) from None


def run_update(
    operation: dict[str, Any],
    *,
    confirmed: bool,
    runner: Callable[..., Any] = subprocess.run,
    postflight: Callable[[], dict[str, Any]] = lite_travel.diagnose_worker,
    node_command: str | Path | None = None,
    postflight_manifest_get: Optional[Callable[..., Any]] = None,
    postflight_sleep: Callable[[float], None] = time.sleep,
    on_postflight_retry: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    if not confirmed:
        raise LiteTravelOperationError("外部変更の確認が必要です。")
    if operation.get("status") not in {"planned", "failed"}:
        raise LiteTravelOperationError("更新操作の状態が不正です。")
    target_digest = str(operation.get("target_digest") or "").strip().lower()
    if target_digest:
        if not UI_ASSET_DIGEST.fullmatch(target_digest):
            raise LiteTravelOperationError("更新操作のLite UI資産識別子が不正です。")
        _assert_update_binding(operation, target_digest=target_digest)
        worker_url = _normalize_worker_url(operation.get("worker_url"))
        if not worker_url:
            raise LiteTravelOperationError("更新操作のWorker接続先が不正です。")
        base_postflight = postflight
        postflight = lambda: _postflight_with_ui_manifest(
            base_postflight,
            worker_url=worker_url,
            target_digest=target_digest,
            request_get=postflight_manifest_get,
        )
    if (
        operation.get("status") == "failed"
        and operation.get("failure_code") == "postflight_not_ready"
        and "deploy" in (operation.get("completed_steps") or [])
    ):
        return _finish_update_postflight_with_retry(
            operation,
            postflight,
            sleeper=postflight_sleep,
            on_retry=on_postflight_retry,
        )
    root = relay_root()
    try:
        node, wrangler, _wrangler_cli, _runtime = (
            lite_cloud_setup.resolve_lite_command_runtime(
                root, node_command=node_command, runner=runner
            )
        )
    except lite_cloud_setup.LiteCloudSetupError as exc:
        message = (
            prerequisite_message({"runtime_source": "development"})
            if exc.failure_code.startswith("development_runtime_")
            else "Lite専用runtimeを確認できません。準備ツールの修復を実行してください。"
        )
        raise LiteTravelOperationError(message) from exc
    recovered_config_path = configured_wrangler_config_path()
    if not recovered_config_path:
        raise LiteTravelOperationError("Lite用クラウドの接続設定を自動確認できませんでした。")
    config = lite_travel._resolve_wrangler_config(recovered_config_path)[1]
    # 明示的な無効指定や壊れた設定は、資産生成を含む更新準備の前に停止する。
    _assert_request_signal_flag_allowed(config)
    _build_update_assets(
        config, node=node, runner=runner, root=root, operation_id=operation.get("operation_id")
    )
    _ensure_request_expiry_cron(config)
    _ensure_request_signal_flag(config)
    database = str(operation["database_name"])
    steps = [
        ("bookmark", [node, str(wrangler), "d1", "time-travel", "info", database, "--json", "--config", config.as_posix()]),
        ("migration", [node, str(wrangler), "d1", "migrations", "apply", database, "--remote", "--config", config.as_posix()]),
        ("deploy", [node, str(wrangler), "deploy", "--config", config.as_posix()]),
    ]
    operation["status"] = "running"
    operation.setdefault("completed_steps", []).append("preflight")
    _save_operation(operation)
    for step, command in steps:
        try:
            result = runner(command, cwd=root.as_posix(), capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            category = "network" if isinstance(exc, subprocess.TimeoutExpired) else "unknown"
            operation.update(status="failed", failed_step=step, failure_code=f"{step}_execution_failed")
            message = _cli_failure_record(
                operation,
                step=step,
                category=category,
                codes=[],
            )
            _save_operation(operation)
            raise LiteTravelOperationError(f"{message}自動restoreは行いません。") from None
        if result.returncode != 0:
            codes = _cli_error_codes(result)
            operation.update(status="failed", failed_step=step, failure_code=f"{step}_failed")
            message = _cli_failure_record(
                operation,
                step=step,
                category=_classify_cli_failure(result, codes=codes),
                codes=codes,
            )
            _save_operation(operation)
            raise LiteTravelOperationError(f"{message}自動restoreは行いません。")
        if step == "bookmark":
            try:
                parsed = json.loads(result.stdout or "{}")
                operation["bookmark"] = str(parsed.get("bookmark") or parsed.get("timestamp") or "recorded")[:200]
            except (ValueError, AttributeError):
                operation["bookmark"] = "recorded"
        operation["completed_steps"].append(step)
        _save_operation(operation)
    return _finish_update_postflight_with_retry(
        operation,
        postflight,
        sleeper=postflight_sleep,
        on_retry=on_postflight_retry,
    )


def _postflight_with_ui_manifest(
    postflight: Callable[[], dict[str, Any]],
    *,
    worker_url: str,
    target_digest: str,
    request_get: Optional[Callable[..., Any]],
) -> dict[str, Any]:
    diagnostics = postflight()
    if not isinstance(diagnostics, dict):
        diagnostics = {"state": "unknown_schema"}
    else:
        diagnostics = dict(diagnostics)
    manifest_result = _fetch_ui_manifest(worker_url, request_get=request_get)
    manifest = manifest_result.get("manifest") if manifest_result.get("state") == "ready" else None
    diagnostics["ui_manifest"] = manifest or {
        "format_version": UI_MANIFEST_FORMAT_VERSION,
        "ui_build": "",
        "asset_digest": "",
        "state": manifest_result.get("state", "check_failed"),
    }
    state = str(diagnostics.get("state") or "unknown")
    actual_digest = str((manifest or {}).get("asset_digest") or "").lower()
    if state in UPDATE_POSTFLIGHT_SUCCESS_STATES and actual_digest != target_digest:
        diagnostics["state"] = "asset_update_propagation_pending"
    return diagnostics


def run_connected_update(
    offer: dict[str, Any],
    *,
    confirmed: bool,
    runner: Callable[..., Any] = subprocess.run,
    postflight: Callable[[], dict[str, Any]] = lite_travel.diagnose_worker,
    node_command: str | Path | None = None,
    request_get: Optional[Callable[..., Any]] = None,
    postflight_sleep: Callable[[float], None] = time.sleep,
    on_postflight_retry: Optional[Callable[[dict[str, Any]], None]] = None,
    room_name: str = "",
    presence_checker: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    """接続済みLiteの版照合から計画・実行・事後確認までを一回に束ねる。

    ``confirmed`` が偽の場合は接続確認を含む変更処理を行わず停止する。offer取得後に
    接続先、D1名、UI資産digest、ローカルpresenceを再確認し、途中で変わった候補を
    実行しない。同じWorkerが既に目標digestを返した場合はdeployせず、その状態を返す。
    """
    if not confirmed:
        raise LiteTravelOperationError("外部変更の確認が必要です。")
    if not isinstance(offer, dict):
        raise LiteTravelOperationError("Lite更新の提案が不正です。")
    if offer.get("state") != "update_required" or offer.get("can_update") is not True:
        raise LiteTravelOperationError("現在のLite更新提案は実行できません。状態を再確認してください。")
    target_digest = str(offer.get("target_digest") or offer.get("asset_digest") or "").strip().lower()
    if not UI_ASSET_DIGEST.fullmatch(target_digest):
        raise LiteTravelOperationError("Lite UIの更新対象を確認できません。")
    operation_id = _safe_operation_id(offer.get("operation_id"))
    try:
        with file_lock_utils.locked_file(_update_operation_lock_path().as_posix(), timeout=0.0):
            target = _local_ui_metadata()
            if (
                str(target.get("asset_digest") or "").lower() != target_digest
                or str(target.get("ui_build") or "") != str(offer.get("ui_build") or "")
            ):
                raise LiteTravelOperationError(
                    "本体のLite UI版が更新提案と変わりました。状態を再確認してください。"
                )
            binding = _assert_update_binding(offer, target_digest=target_digest)
            presence = _presence_state(room_name, presence_checker)
            if presence in LOCAL_PRESENCE_LOCKING_STATES:
                raise LiteTravelOperationError(
                    "現在のお出かけ中はLite用クラウドを更新できません。帰宅後に再試行してください。"
                )
            refreshed = get_update_offer(
                True,
                request_get=request_get,
                local_metadata=target,
                room_name=room_name,
                presence_checker=presence_checker,
            )
            if refreshed.get("state") == "up_to_date":
                return {
                    **refreshed,
                    "status": "up_to_date",
                    "operation_id": operation_id,
                    "reason": "Lite用クラウドは別の操作で本体と同じUI版へ更新済みです。",
                }
            if refreshed.get("state") != "update_required" or refreshed.get("can_update") is not True:
                raise LiteTravelOperationError(
                    str(refreshed.get("reason") or "更新前の接続状態を確認できませんでした。")
                )
            if refreshed.get("resume_available") is True:
                operation_id = _safe_operation_id(refreshed.get("operation_id") or operation_id)
            for key in (
                "worker_url",
                "database_name",
                "resource_digest",
                "target_digest",
                "connection_fingerprint",
            ):
                if str(refreshed.get(key) or "") != str(offer.get(key) or ""):
                    raise LiteTravelOperationError(
                        "更新提案の接続先またはUI版が変わりました。状態を確認して再計画してください。"
                    )
            existing = _load_operation(operation_id)
            if existing is not None:
                for key in (
                    "worker_url",
                    "database_name",
                    "resource_digest",
                    "target_digest",
                    "connection_fingerprint",
                ):
                    if str(existing.get(key) or "") != str(offer.get(key) or ""):
                        raise LiteTravelOperationError(
                            "既存のLite更新記録と接続先またはUI版が一致しません。再計画してください。"
                        )
                if (
                    existing.get("status") == "failed"
                    and existing.get("failure_code") == "postflight_not_ready"
                    and "deploy" in (existing.get("completed_steps") or [])
                ):
                    return run_update(
                        existing,
                        confirmed=True,
                        runner=runner,
                        postflight=postflight,
                        node_command=node_command,
                        postflight_manifest_get=request_get,
                        postflight_sleep=postflight_sleep,
                        on_postflight_retry=on_postflight_retry,
                    )
                raise LiteTravelOperationError(
                    "同じLite更新操作は既に記録されています。状態を確認して新しい提案を作成してください。"
                )
            operation = plan_update(
                binding["database_name"],
                operation_id=operation_id,
                binding={
                    **binding,
                    "target_digest": target_digest,
                    "update_kind": refreshed.get("update_kind") or offer.get("update_kind") or "assets",
                },
                update_kind=str(refreshed.get("update_kind") or offer.get("update_kind") or "assets"),
                target_digest=target_digest,
            )

            return run_update(
                operation,
                confirmed=True,
                runner=runner,
                postflight=postflight,
                node_command=node_command,
                postflight_manifest_get=request_get,
                postflight_sleep=postflight_sleep,
                on_postflight_retry=on_postflight_retry,
            )
    except file_lock_utils.Timeout as exc:
        raise LiteTravelOperationError(
            "別のLite更新が実行中です。完了後に状態を確認してください。"
        ) from exc


def _finish_update_postflight_with_retry(
    operation: dict[str, Any],
    postflight: Callable[[], dict[str, Any]],
    *,
    sleeper: Callable[[float], None],
    on_retry: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Cloudflareの公開伝播を待ち、更新処理を繰り返さず事後診断だけ再試行する。"""
    retry_delays = (
        UI_POSTFLIGHT_RETRY_DELAYS
        if str(operation.get("target_digest") or "").strip()
        else UPDATE_POSTFLIGHT_RETRY_DELAYS
    )
    total_attempts = len(retry_delays) + 1
    diagnostics: dict[str, Any] = {}
    for attempt in range(1, total_attempts + 1):
        diagnostics = postflight()
        state = str(diagnostics.get("state") or "unknown")
        operation["postflight_attempts"] = attempt
        if state in UPDATE_POSTFLIGHT_SUCCESS_STATES:
            return _finish_update_postflight(operation, diagnostics)
        if state not in UPDATE_POSTFLIGHT_RETRYABLE_STATES or attempt >= total_attempts:
            return _finish_update_postflight(operation, diagnostics)

        delay = retry_delays[attempt - 1]
        operation.update(
            status="failed",
            failed_step="postflight",
            failure_code="postflight_not_ready",
            postflight_state=state,
        )
        _save_operation(operation)
        if on_retry is not None:
            try:
                on_retry(
                    {
                        "attempt": attempt,
                        "total_attempts": total_attempts,
                        "state": state,
                        "delay_seconds": delay,
                    }
                )
            except Exception:
                # 表示側の進捗通知失敗で、更新の成立確認まで止めない。
                pass
        sleeper(delay)

    return _finish_update_postflight(operation, diagnostics)


def _finish_update_postflight(operation: dict[str, Any], diagnostics: dict[str, Any]) -> dict[str, Any]:
    """更新の成立条件と、利用を妨げない運用警告を分けて記録する。"""
    state = str(diagnostics.get("state") or "unknown")
    operation["postflight_state"] = state
    if "ui_manifest" in diagnostics:
        manifest = diagnostics.get("ui_manifest")
        operation["postflight_asset_digest"] = (
            str(manifest.get("asset_digest") or "").lower()
            if isinstance(manifest, dict)
            else ""
        )
    if state not in UPDATE_POSTFLIGHT_SUCCESS_STATES:
        operation.update(status="failed", failed_step="postflight", failure_code="postflight_not_ready")
        _save_operation(operation)
        attempts = max(1, int(operation.get("postflight_attempts") or 1))
        raise LiteTravelOperationError(
            f"Cloudflareへの反映を{attempts}回確認しましたが、"
            f"事後診断が完了条件を満たしていません（{state}）。"
            "更新操作記録を保ったまま、接続状態を再診断してください。"
        )
    operation.pop("failed_step", None)
    operation.pop("failure_code", None)
    operation.pop("failed_step_label", None)
    operation.pop("failure_category", None)
    operation.pop("failure_codes", None)
    operation.pop("failure_guidance", None)
    if state == "maintenance_overdue":
        resources = (diagnostics.get("diagnostics") or {}).get("resources") or {}
        try:
            sessions = max(0, int(resources.get("overdue_sessions") or 0))
            standby = max(0, int(resources.get("overdue_standby") or 0))
        except (TypeError, ValueError):
            sessions = 0
            standby = 0
        operation["postflight_warning"] = (
            "更新は完了しました。削除期限を迎えたデータがあります"
            f"（帰宅後{sessions}件・お出かけ前{standby}件）。接続状態カードから確認してください。"
        )
    else:
        operation.pop("postflight_warning", None)
    if "postflight" not in operation.setdefault("completed_steps", []):
        operation["completed_steps"].append("postflight")
    operation.update(status="completed", completed_at=dt.datetime.now(dt.timezone.utc).isoformat())
    _save_operation(operation)
    return operation
