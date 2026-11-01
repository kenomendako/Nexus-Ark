"""Nexus Ark Lite独立お出かけモードの本体側契約。"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import html
import json
import math
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote, urlparse

import requests

import config_manager
import constants
import file_lock_utils
import lite_cloud_setup
from lite_return_memory import carry_return_pending, merge_return_pending, parse_event_datetime
import room_manager
import utils
import usage_ledger


SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = 3
MULTI_SNAPSHOT_SCHEMA_VERSION = 4
MAX_TRAVEL_PERSONAS = 3
DEFAULT_CREDENTIAL_PROFILE_ID = "gemini-personal-1"
LEGACY_DEFAULT_MAX_OUTPUT_TOKENS = 2048
MAX_MANUAL_OUTPUT_TOKENS = 65536
VALID_PROVIDERS = {"gemini", "openai", "anthropic", "xai", "openrouter"}
PROVIDER_SECRET_METADATA = {
    "gemini": {
        "bindings": ("GEMINI_PERSONAL_1", "GEMINI_PERSONAL_2", "GEMINI_PERSONAL_3"),
        "base_url_id": "gemini-official",
        "default_profile_id": "gemini-personal-1",
        "display_name": "Gemini（個人用1）",
        "registration_slots": (
            {
                "secret_binding_id": "GEMINI_PERSONAL_1",
                "credential_profile_id": "gemini-personal-1",
                "display_name": "Gemini（個人用1）",
            },
            {
                "secret_binding_id": "GEMINI_PERSONAL_2",
                "credential_profile_id": "gemini-personal-2",
                "display_name": "Gemini（個人用2）",
            },
            {
                "secret_binding_id": "GEMINI_PERSONAL_3",
                "credential_profile_id": "gemini-personal-3",
                "display_name": "Gemini（個人用3）",
            },
        ),
    },
    "openai": {
        "bindings": ("OPENAI_PERSONAL_1", "OPENAI_PERSONAL_2"),
        "base_url_id": "openai-official",
        "default_profile_id": "openai-personal-1",
        "display_name": "OpenAI（個人用1）",
        "registration_slots": (
            {
                "secret_binding_id": "OPENAI_PERSONAL_1",
                "credential_profile_id": "openai-personal-1",
                "display_name": "OpenAI（個人用1）",
            },
            {
                "secret_binding_id": "OPENAI_PERSONAL_2",
                "credential_profile_id": "openai-personal-2",
                "display_name": "OpenAI（個人用2）",
            },
        ),
    },
    "anthropic": {
        "bindings": ("ANTHROPIC_PERSONAL_1",),
        "base_url_id": "anthropic-official",
        "default_profile_id": "anthropic-personal-1",
        "display_name": "Anthropic（個人用1）",
        "registration_slots": (
            {
                "secret_binding_id": "ANTHROPIC_PERSONAL_1",
                "credential_profile_id": "anthropic-personal-1",
                "display_name": "Anthropic（個人用1）",
            },
        ),
    },
    "xai": {
        "bindings": ("XAI_PERSONAL_1",),
        "base_url_id": "xai-official",
        "default_profile_id": "xai-personal-1",
        "display_name": "xAI（個人用1）",
        "registration_slots": (
            {
                "secret_binding_id": "XAI_PERSONAL_1",
                "credential_profile_id": "xai-personal-1",
                "display_name": "xAI（個人用1）",
            },
        ),
    },
    "openrouter": {
        "bindings": ("OPENROUTER_PERSONAL_1",),
        "base_url_id": "openrouter-official",
        "default_profile_id": "openrouter-personal-1",
        "display_name": "OpenRouter（個人用1）",
        "registration_slots": (
            {
                "secret_binding_id": "OPENROUTER_PERSONAL_1",
                "credential_profile_id": "openrouter-personal-1",
                "display_name": "OpenRouter（個人用1）",
            },
        ),
    },
}
VALID_RETENTION_DAYS = {0, 7, 30}
LOCKING_STATES = {"armed", "active", "returning"}
MAX_SNAPSHOT_CHARS = 300_000
MAX_SECTION_CHARS = {
    "system_prompt": 100_000,
    "core_memory": 100_000,
    "episodic_summary": 50_000,
}
_SECRET_VALUE_PATTERN = re.compile(
    r"(?:AIza[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z_-]{16,}|Bearer\s+[0-9A-Za-z._~-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?:^|[\s\"'=(])(?:[A-Za-z]:[\\/]|/(?:home|Users|mnt|root|etc)/)")


class LiteTravelError(RuntimeError):
    """安全にユーザーへ説明できるLite travel契約違反。"""


class LiteTravelUncertainRequestError(LiteTravelError):
    """Worker操作の結果確認、または登録後のローカル復旧が必要。"""


class LiteTravelHTTPError(LiteTravelError):
    """Workerが返したHTTPエラーを、登録前の互換性判定にも使う。"""

    def __init__(self, message: str, status_code: int, error_code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _metadata_root() -> Path:
    return Path(constants.METADATA_DIR) / "lite_travel"


def _room_key(room_name: str) -> str:
    return hashlib.sha256(room_name.encode("utf-8")).hexdigest()[:24]


def _validate_room_name(room_name: str) -> str:
    value = str(room_name or "").strip()
    if not value or value in {".", ".."} or any(char in value for char in ("/", "\\", "\x00")):
        raise LiteTravelError("不正なペルソナIDです。")
    return value


def _presence_path(room_name: str) -> Path:
    return _metadata_root() / "presence_locks" / f"{_room_key(room_name)}.json"


def _import_state_path(room_name: str) -> Path:
    return _metadata_root() / "imports" / f"{_room_key(room_name)}.json"


def _return_operation_path(travel_session_id: str) -> Path:
    key = hashlib.sha256(str(travel_session_id).encode("utf-8")).hexdigest()[:32]
    return _metadata_root() / "return_operations" / f"{key}.json"


def _new_return_operation(travel_session_id: str) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "operation_id": str(uuid.uuid4()),
        "operation_type": "online_return",
        "travel_session_id": travel_session_id,
        "status": "started",
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "completed_steps": [],
        "personas": {},
        "close_completed": False,
    }


def _save_return_operation(operation: Dict[str, Any]) -> None:
    operation["updated_at"] = _now_iso()
    _write_json(_return_operation_path(str(operation["travel_session_id"])), operation)


def _complete_return_step(operation: Dict[str, Any], step: str) -> None:
    completed = operation.setdefault("completed_steps", [])
    if step not in completed:
        completed.append(step)


def _return_journal_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """再開に必要な非本文・非ローカルパス項目だけをoperation journalへ残す。"""
    allowed = {
        "travel_session_id", "persona_id", "through_sequence", "payload_hash",
        "imported_event_count", "imported_receipt_count", "final_route", "ack",
    }
    return {key: value for key, value in result.items() if key in allowed}


_RECOVERY_SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RECOVERY_PAYLOAD_HASH_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


def _restore_completed_return_pending(
    travel_session_id: str,
    room_name: str,
    journal: Dict[str, Any],
) -> None:
    """completed journalのローカルbundleから、未整理markerだけを復元する。

    completed fastpathはクラウドへ再接続せずに終了するため、記憶整理待ちの
    付与だけを補う。journal・bundle・v4 payloadの対応が一つでも崩れている
    場合は、推測で状態を変えず静かに終了する。
    """
    if not isinstance(journal, dict) or journal.get("persona_id") != room_name:
        return
    result = journal.get("result")
    if not isinstance(result, dict):
        return
    if (
        result.get("travel_session_id") != travel_session_id
        or result.get("persona_id") != room_name
    ):
        return
    through_sequence = result.get("through_sequence")
    if isinstance(through_sequence, bool) or not isinstance(through_sequence, int) or through_sequence < 0:
        return
    payload_hash = result.get("payload_hash")
    if not isinstance(payload_hash, str) or not _RECOVERY_PAYLOAD_HASH_PATTERN.fullmatch(payload_hash):
        return
    if not _RECOVERY_SESSION_PATTERN.fullmatch(travel_session_id):
        return

    try:
        safe_room_name = _validate_room_name(room_name)
        safe_root = (_metadata_root() / "recovery_bundles").resolve()
        candidate = safe_root / (
            f"{travel_session_id}_{_room_key(safe_room_name)}_{payload_hash[:16]}.json"
        )
        # session/persona/hashは上で文字種を制限しているが、解決後の親も確認して
        # symlink経由のrecovery bundleをmetadata領域外から読まない。
        if candidate.resolve().parent != safe_root:
            return
        if not candidate.is_file():
            return
        payload = _read_json(candidate, None)
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict) or payload.get("schema_version") != 4:
        return
    try:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        canonical_hash = __import__("base64").urlsafe_b64encode(
            hashlib.sha256(canonical.encode("utf-8")).digest()
        ).decode("ascii").rstrip("=")
        if not hmac.compare_digest(canonical_hash, payload_hash):
            return
        payload_session, payload_room, _after, payload_through = _validate_v4_persona_payload(
            payload, safe_room_name
        )
        if (
            payload_session != travel_session_id
            or payload_room != safe_room_name
            or payload_through != through_sequence
        ):
            return
        events = payload.get("events")
        if not isinstance(events, list):
            return
    except (LiteTravelError, OSError, TypeError, ValueError, OverflowError):
        return

    import_state_path = _import_state_path(safe_room_name)
    import_operation_lock = _metadata_root() / "imports" / f"{_room_key(safe_room_name)}.operation"
    try:
        with file_lock_utils.locked_file(import_operation_lock.as_posix()):
            if import_state_path.exists():
                import_state = _read_json(import_state_path, None)
            else:
                import_state = {}
            if not isinstance(import_state, dict):
                return
            merged = merge_return_pending(
                import_state, events, travel_session_id, safe_room_name
            )
            if merged != import_state:
                _write_json(import_state_path, merged)
    except (LiteTravelError, OSError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return


def _event_import_marker(session_id: str, room_name: str, event_id: str) -> str:
    """payloadの再生成時にも変わらない、帰宅event単位のログjournal marker。"""
    canonical = "\x00".join((str(session_id), str(room_name), str(event_id)))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"<!-- Lite Travel Event: {digest} -->"


def _logged_event_ids(
    existing_log: str,
    session_id: str,
    room_name: str,
    events: Iterable[Dict[str, Any]],
) -> set[str]:
    """現在の帰宅差分について、ログ追記済みのevent IDを安定markerから復元する。"""
    return {
        str(event["event_id"])
        for event in events
        if _event_import_marker(session_id, room_name, str(event["event_id"])) in existing_log
    }


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _write_json(path: Path, value: Any) -> None:
    if not file_lock_utils.safe_json_write(path.as_posix(), value):
        raise LiteTravelError("ローカル状態を保存できませんでした。")


def _normalize_max_output_tokens(value: Any) -> Optional[int]:
    """空欄はモデル自動、数値は利用者が明示した上限として正規化する。"""
    if value in (None, "", 0, "0", "auto"):
        return None
    result = int(value)
    if not 1 <= result <= MAX_MANUAL_OUTPUT_TOKENS:
        raise LiteTravelError(
            f"1回答の最大長は空欄（自動）または1〜{MAX_MANUAL_OUTPUT_TOKENS}で指定してください。"
        )
    return result


def get_settings() -> Dict[str, Any]:
    defaults = {
        "worker_url": "",
        "owner_token": "",
        "bundle_signing_key": "",
        "bundle_signing_key_previous": [],
        "credential_profile_id": DEFAULT_CREDENTIAL_PROFILE_ID,
        "model_id": "",
        "retention_days": 7,
        "wrangler_config_path": "cloud/lite-relay/wrangler.phase2.jsonc",
        "budget_daily_limit_usd": 1.0,
        "budget_session_limit_usd": 0.5,
        "budget_warning_ratio": 0.8,
        "budget_allow_unknown_price": False,
        "budget_max_output_tokens": None,
        "budget_timezone": "Asia/Tokyo",
        "cache_policy": "auto",
        "standby_home_instance_id": "",
        "standby_retention_days": 7,
        "standby_refresh_on_lite_start": False,
        "standby_refresh_min_interval_hours": 6,
        "registered_provider_profiles": [],
    }
    raw = config_manager.CONFIG_GLOBAL.get("lite_travel_settings", {}) or {}
    if isinstance(raw, dict):
        defaults.update(raw)
        # 2,048は旧版の固定初期値。明示設定との区別がなかったため自動へ移行する。
        if raw.get("budget_max_output_tokens") == LEGACY_DEFAULT_MAX_OUTPUT_TOKENS:
            defaults["budget_max_output_tokens"] = None
    # 初回設定ではGeminiの最新候補を提示する。既存の明示モデルは上書きしない。
    # カタログでの利用可否確認は、出発前のvalidate_snapshot_initial_routesが担当する。
    if not str(defaults.get("model_id") or "").strip():
        profile_id = str(defaults.get("credential_profile_id") or "").strip()
        provider = profile_id.split("-", 1)[0].lower()
        defaults["model_id"] = config_manager.get_lite_default_model(provider)
    return defaults


def get_lite_initial_model_candidate(provider: str, current_model: str = "") -> str:
    """Lite初回候補を返す。既存の選択値を優先し、候補は未確認のままとする。"""
    current = str(current_model or "").strip().removeprefix("⭐ ").strip()
    return current or config_manager.get_lite_default_model(provider)


def save_settings(
    worker_url: str,
    owner_token: str,
    bundle_signing_key: str,
    model_id: str,
    retention_days: int,
    credential_profile_id: Optional[str] = None,
    wrangler_config_path: Optional[str] = None,
    budget_daily_limit_usd: Optional[float] = None,
    budget_session_limit_usd: Optional[float] = None,
    budget_warning_ratio: float = 0.8,
    budget_allow_unknown_price: bool = False,
    budget_max_output_tokens: Optional[int] = None,
    budget_timezone: str = "Asia/Tokyo",
    cache_policy: str = "auto",
) -> bool:
    current = get_settings()
    url = str(worker_url or "").strip().rstrip("/")
    parsed = urlparse(url)
    localhost = parsed.hostname in {"127.0.0.1", "localhost"}
    if parsed.scheme not in ({"http", "https"} if localhost else {"https"}) or not parsed.netloc:
        raise LiteTravelError("Worker URLはHTTPSで指定してください。")
    resolved_owner_token = str(owner_token or current.get("owner_token") or "").strip()
    resolved_signing_key = str(bundle_signing_key or current.get("bundle_signing_key") or "").strip()
    if len(resolved_owner_token) < 16:
        raise LiteTravelError("所有者Tokenは16文字以上で指定してください。")
    if len(resolved_signing_key) < 16:
        raise LiteTravelError("帰宅bundle署名鍵は16文字以上で指定してください。")
    profile_id = str(credential_profile_id or get_settings().get("credential_profile_id") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,99}", profile_id):
        raise LiteTravelError("初期資格情報プロファイルIDが不正です。")
    if not str(model_id or "").strip():
        raise LiteTravelError("初期モデルを指定してください。")
    if int(retention_days) not in VALID_RETENTION_DAYS:
        raise LiteTravelError("本文保持日数は0、7、30のいずれかです。")
    daily_limit = None if budget_daily_limit_usd in (None, "") else float(budget_daily_limit_usd)
    session_limit = None if budget_session_limit_usd in (None, "") else float(budget_session_limit_usd)
    warning_ratio = float(budget_warning_ratio)
    max_output_tokens = _normalize_max_output_tokens(budget_max_output_tokens)
    timezone = str(budget_timezone or "").strip()
    cache_policy = str(cache_policy or "").strip()
    if any(value is not None and (not math.isfinite(value) or value <= 0) for value in (daily_limit, session_limit)):
        raise LiteTravelError("日次・セッション予算は空欄または0より大きいUSD額で指定してください。")
    if not math.isfinite(warning_ratio) or not 0 < warning_ratio <= 1:
        raise LiteTravelError("予算警告率は0より大きく1以下で指定してください。")
    try:
        __import__("zoneinfo").ZoneInfo(timezone)
    except Exception as exc:
        raise LiteTravelError("予算タイムゾーンが不正です。") from exc
    if cache_policy not in {"auto", "off", "gemini_explicit"}:
        raise LiteTravelError("キャッシュ方針が不正です。")
    settings = {
        **current,
        "worker_url": url,
        "owner_token": resolved_owner_token,
        "bundle_signing_key": resolved_signing_key,
        "credential_profile_id": profile_id,
        "model_id": str(model_id).strip(),
        "retention_days": int(retention_days),
        "wrangler_config_path": str(
            wrangler_config_path or get_settings().get("wrangler_config_path") or ""
        ).strip(),
        "budget_daily_limit_usd": daily_limit,
        "budget_session_limit_usd": session_limit,
        "budget_warning_ratio": warning_ratio,
        "budget_allow_unknown_price": bool(budget_allow_unknown_price),
        "budget_max_output_tokens": max_output_tokens,
        "budget_timezone": timezone,
        "cache_policy": cache_policy,
    }
    return bool(config_manager.update_config_keys({"lite_travel_settings": settings}))


def save_initial_connection_settings(
    worker_url: str,
    owner_token: str,
    bundle_signing_key: str,
    wrangler_config_path: str,
) -> bool:
    """初回準備で確定した接続4項目だけを、最新設定へ差分保存する。"""

    url = str(worker_url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise LiteTravelError("Worker URLはパスなしのHTTPS URLで指定してください。")
    owner = str(owner_token or "").strip()
    signing = str(bundle_signing_key or "").strip()
    if len(owner) < 16 or len(signing) < 16 or hmac.compare_digest(owner, signing):
        raise LiteTravelError("本体確認キーと帰宅データ確認キーを別々に指定してください。")
    config_path = str(wrangler_config_path or "").strip().replace("\\", "/")
    relative = Path(config_path)
    if (
        not config_path.startswith("cloud/lite-relay/")
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix not in {".json", ".jsonc"}
        or ".example." in relative.name
    ):
        raise LiteTravelError("実運用用のWrangler設定パスが不正です。")
    return bool(
        config_manager.update_nested_config_keys(
            "lite_travel_settings",
            {
                "worker_url": url,
                "owner_token": owner,
                "bundle_signing_key": signing,
                "wrangler_config_path": config_path,
            },
        )
    )


def save_initial_route_settings(credential_profile_id: str, model_id: str) -> bool:
    """初回AI接続とモデルだけを、最新Lite設定へ差分保存する。"""

    profile_id = str(credential_profile_id or "").strip()
    model = str(model_id or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,99}", profile_id):
        raise LiteTravelError("初期資格情報プロファイルIDが不正です。")
    if (
        not model
        or len(model) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in model)
    ):
        raise LiteTravelError("初期モデルIDが不正です。")
    return bool(
        config_manager.update_nested_config_keys(
            "lite_travel_settings",
            {"credential_profile_id": profile_id, "model_id": model},
        )
    )


def get_secret_binding_choices(provider: str) -> list[str]:
    metadata = PROVIDER_SECRET_METADATA.get(str(provider or "").strip().lower())
    return list(metadata["bindings"]) if metadata else []


def get_provider_registration_choices(provider: str) -> list[tuple[str, str]]:
    """UIへ返す登録枠の表示名とSecret binding ID（Secret値は含めない）。"""
    metadata = PROVIDER_SECRET_METADATA.get(str(provider or "").strip().lower())
    if not metadata:
        return []
    return [
        (
            f"{'通常用' if index == 0 else f'予備{index}'} — {slot['display_name']}",
            str(slot["secret_binding_id"]),
        )
        for index, slot in enumerate(metadata.get("registration_slots", ()))
    ]


def get_provider_registration_defaults(provider: str, binding_id: str) -> dict[str, str]:
    """登録枠に対応するprofile IDと表示名を返す。不明な組み合わせは空辞書。"""
    metadata = PROVIDER_SECRET_METADATA.get(str(provider or "").strip().lower())
    binding = str(binding_id or "").strip()
    if not metadata:
        return {}
    for slot in metadata.get("registration_slots", ()):
        if slot["secret_binding_id"] == binding:
            return {
                "credential_profile_id": str(slot["credential_profile_id"]),
                "display_name": str(slot["display_name"]),
            }
    return {}


def get_local_key_choices(provider: str) -> list[tuple[str, str]]:
    """Secret値を返さず、本体内のキー参照名だけをUIへ返す。"""
    provider = str(provider or "").strip().lower()
    if provider == "gemini":
        return [(str(name), f"gemini:{name}") for name, value in config_manager.GEMINI_API_KEYS.items() if value]
    if provider in {"openai", "openrouter"}:
        expected_host = "api.openai.com" if provider == "openai" else "openrouter.ai"
        result = []
        for profile in config_manager.get_openai_settings_list():
            try:
                host = urlparse(str(profile.get("base_url") or "")).hostname
            except ValueError:
                host = None
            if host == expected_host and str(profile.get("api_key") or "").strip():
                name = str(profile.get("name") or "").strip()
                if name:
                    result.append((name, f"{provider}:{name}"))
        return result
    if provider == "anthropic" and str(config_manager.ANTHROPIC_API_KEY or "").strip():
        return [("Anthropic APIキー", "anthropic:default")]
    if provider == "xai" and str(config_manager.XAI_API_KEY or "").strip():
        return [("xAI APIキー", "xai:default")]
    return []


def get_local_key_metadata(provider: str, key_reference: str) -> dict[str, Any]:
    """選択したローカルキーの非秘密メタデータだけを返す。

    ``key_reference`` はUIから渡る値だが、表示名と有料設定は本体の保存済み
    選択肢へ照合して確定する。Secretの実値は返さず、``_resolve_local_key``も
    呼び出さないため、表示名更新の事前確認にも利用できる。
    """
    normalized_provider = str(provider or "").strip().lower()
    normalized_reference = str(key_reference or "").strip()
    if normalized_provider not in VALID_PROVIDERS or not normalized_reference:
        raise LiteTravelError("選択したローカルAPIキーの対応が不正です。")
    for label, reference in get_local_key_choices(normalized_provider):
        if str(reference or "").strip() != normalized_reference:
            continue
        source_name = str(label or "").strip()
        if not source_name:
            break
        metadata: dict[str, Any] = {
            "source_key_reference": normalized_reference,
            "source_key_name": source_name,
        }
        if normalized_provider == "gemini":
            metadata["source_key_is_paid"] = bool(
                config_manager.is_paid_api_key_name(source_name)
            )
        return metadata
    raise LiteTravelError("選択したローカルAPIキーを本体の保存済み一覧で確認できません。")


def build_provider_profile_display_name(
    provider: str,
    key_reference: str,
    existing_display_name: str = "",
) -> str:
    """本体キー名を先頭にした、秘密値を含まないprofile表示名を作る。

    Geminiだけは本体設定の ``paid_api_key_names`` に基づく区分を付ける。
    これは本体の設定区分であり、実際の課金状態を検査した結果ではない。
    既定のLite名は重ねず、利用者が付けた別名だけを後ろへ残す。
    """
    normalized_provider = str(provider or "").strip().lower()
    source = get_local_key_metadata(normalized_provider, key_reference)
    source_name = str(source["source_key_name"])
    if normalized_provider == "gemini":
        paid_label = "有料設定" if source.get("source_key_is_paid") else "無料設定"
        primary = f"{source_name}（{paid_label}）"
    else:
        primary = source_name

    # 100文字制限を守りつつ、Geminiの区分を落とさない。
    if len(primary) > 100:
        suffix = primary[len(source_name):] if primary.startswith(source_name) else ""
        if suffix and len(suffix) < 100:
            primary = f"{source_name[:100 - len(suffix)]}{suffix}"
        else:
            primary = primary[:100]

    alias = str(existing_display_name or "").strip()
    # 以前の区分を含む生成済み表示名は別名として重ねない。区分設定が
    # 有料から無料へ変わった場合も、利用者が後ろへ付けた別名だけを残す。
    for generated_marker in ("（有料設定）", "（無料設定）"):
        generated_prefix = f"{source_name}{generated_marker}"
        if alias.startswith(generated_prefix):
            alias_tail = alias[len(generated_prefix):].strip()
            alias = alias_tail[1:].strip() if alias_tail.startswith("—") else ""
            break
    defaults = {
        str(slot.get("display_name") or "").strip()
        for slot in PROVIDER_SECRET_METADATA.get(normalized_provider, {}).get(
            "registration_slots", ()
        )
    }
    # 以前に生成した同じ名前や固定の「個人用」名は重ねない。既存の
    # 利用者別名は、キー名を先頭にしたまま補助表示として残す。
    if alias and alias not in defaults and alias != primary and not alias.startswith(primary):
        candidate = f"{primary} — {alias}"
        if len(candidate) <= 100:
            return candidate
    return primary


def infer_provider_from_profile_id(profile_id: str) -> str:
    """資格情報プロファイルIDとローカル登録履歴からproviderを推定する。"""
    value = str(profile_id or "").strip()
    for item in get_settings().get("registered_provider_profiles") or []:
        if isinstance(item, dict) and item.get("credential_profile_id") == value:
            provider = str(item.get("provider") or "").strip().lower()
            return provider if provider in VALID_PROVIDERS else ""
    prefix = value.split("-", 1)[0].lower()
    return prefix if prefix in VALID_PROVIDERS else ""


def remember_registered_provider_profile(
    profile_id: str,
    provider: str,
    display_name: str,
    *,
    source_key_reference: str | None = None,
    source_key_name: str | None = None,
    source_key_is_paid: bool | None = None,
) -> None:
    """Secret値を含めず、登録済み接続の表示用情報だけをローカルへ保存する。

    既存項目を丸ごと捨てず、将来追加された非秘密メタデータも保持する。
    ``source_key_is_paid`` はGeminiの本体設定区分を保存する場合だけ指定する。
    """
    provider = str(provider or "").strip().lower()
    profile_id = str(profile_id or "").strip()
    if provider not in VALID_PROVIDERS or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,99}", profile_id):
        raise LiteTravelError("登録済みAI接続の情報が不正です。")
    item = {
        "credential_profile_id": profile_id,
        "provider": provider,
        "display_name": str(display_name or profile_id).strip()[:100],
    }
    if source_key_reference is not None:
        item["source_key_reference"] = str(source_key_reference or "").strip()
    if source_key_name is not None:
        item["source_key_name"] = str(source_key_name or "").strip()
    if source_key_is_paid is not None:
        item["source_key_is_paid"] = bool(source_key_is_paid)
    elif provider != "gemini":
        item.pop("source_key_is_paid", None)
    result = config_manager.upsert_nested_config_list_item(
        "lite_travel_settings",
        "registered_provider_profiles",
        item,
        "credential_profile_id",
        merge_existing=True,
    )
    if result is False:
        raise LiteTravelError("登録済みAI接続のローカル表示を更新できませんでした。")


def _resolve_local_key(provider: str, key_reference: str) -> str:
    prefix, separator, name = str(key_reference or "").partition(":")
    if not separator or prefix != provider:
        raise LiteTravelError("選択したローカルAPIキーの対応が不正です。")
    if provider == "gemini":
        value = config_manager.GEMINI_API_KEYS.get(name)
    elif provider in {"openai", "openrouter"}:
        expected_host = "api.openai.com" if provider == "openai" else "openrouter.ai"
        profile = config_manager.get_openai_setting_by_name(name)
        try:
            valid_host = urlparse(str((profile or {}).get("base_url") or "")).hostname == expected_host
        except ValueError:
            valid_host = False
        value = (profile or {}).get("api_key") if valid_host else None
    elif provider == "anthropic" and name == "default":
        value = config_manager.ANTHROPIC_API_KEY
    elif provider == "xai" and name == "default":
        value = config_manager.XAI_API_KEY
    else:
        value = None
    value = str(value or "").strip()
    if not value:
        raise LiteTravelError("選択したローカルAPIキーを取得できません。")
    return value


def _resolve_wrangler_config(config_path: str) -> tuple[Path, Path]:
    relay_root = (Path(__file__).resolve().parent / "cloud" / "lite-relay").resolve()
    candidate = Path(str(config_path or "").strip())
    if not candidate.is_absolute():
        candidate = (Path(__file__).resolve().parent / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        candidate.relative_to(relay_root)
    except ValueError as exc:
        raise LiteTravelError("Wrangler設定はcloud/lite-relay配下のファイルを指定してください。") from exc
    if candidate.suffix not in {".json", ".jsonc"} or ".example." in candidate.name or not candidate.is_file():
        raise LiteTravelError(
            "Lite用クラウドの接続設定を自動確認できませんでした。"
            "「状態を確認」後も続く場合は、Nexus Arkを更新してください。"
        )
    return relay_root, candidate


def _is_legacy_primary_registration(provider: str, binding_id: str) -> bool:
    metadata = PROVIDER_SECRET_METADATA.get(provider)
    return bool(metadata and metadata["bindings"] and metadata["bindings"][0] == binding_id)


def _validate_provider_registration_options(
    options: Any,
    provider: str,
    binding_id: str,
    profile_id: str,
) -> dict[str, Any]:
    """Workerのowner-only登録枠が要求したbinding/profileを正本として持つか確認する。"""
    if not isinstance(options, dict) or not isinstance(options.get("providers"), list):
        raise LiteTravelError("Lite用クラウドの登録枠情報が不正です。クラウドを更新してください。")
    provider_entry = None
    selected: dict[str, Any] | None = None
    for entry in options["providers"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("provider"), str):
            raise LiteTravelError("Lite用クラウドの登録枠情報が不正です。クラウドを更新してください。")
        if entry["provider"] == provider:
            if provider_entry is not None:
                raise LiteTravelError("Lite用クラウドの登録枠情報が重複しています。")
            provider_entry = entry
        slots = entry.get("slots")
        if not isinstance(slots, list):
            raise LiteTravelError("Lite用クラウドの登録枠情報が不正です。クラウドを更新してください。")
        for slot in slots:
            if not isinstance(slot, dict):
                raise LiteTravelError("Lite用クラウドの登録枠情報が不正です。クラウドを更新してください。")
            slot_binding = slot.get("secret_binding_id")
            slot_profile = slot.get("credential_profile_id")
            registered_profile = slot.get("registered_profile_id")
            if not isinstance(slot_binding, str) or not isinstance(slot_profile, str):
                raise LiteTravelError("Lite用クラウドの登録枠情報が不正です。クラウドを更新してください。")
            if registered_profile is not None and not isinstance(registered_profile, str):
                raise LiteTravelError("Lite用クラウドの登録済みprofile情報が不正です。")
            if slot_binding == binding_id:
                if entry["provider"] != provider:
                    raise LiteTravelError("プロバイダとCloudflare Secret bindingの対応が不正です。")
                if selected is not None:
                    raise LiteTravelError("指定したSecret bindingの登録枠が重複しています。")
                selected = slot
            if registered_profile == profile_id and slot_binding != binding_id:
                raise LiteTravelError("指定した資格情報profileは別のSecret bindingに登録済みです。")

    if provider_entry is None:
        raise LiteTravelError("選択したプロバイダの登録枠を確認できません。クラウドを更新してください。")

    if selected is None:
        raise LiteTravelError("プロバイダとCloudflare Secret bindingの対応が不正です。クラウドを更新してください。")
    registered_profile = selected.get("registered_profile_id")
    if registered_profile not in {None, profile_id}:
        raise LiteTravelError("指定したSecret bindingは別の資格情報profileに登録済みです。")
    # 未登録枠はcanonical profileだけを受け付ける。既存profileの再登録は、
    # 同じbindingの所有者であることを確認できた場合に限り旧custom IDも維持する。
    if registered_profile is None and selected.get("credential_profile_id") != profile_id:
        raise LiteTravelError("Secret bindingと資格情報profileの対応が不正です。クラウドを更新してください。")
    return selected


def _preflight_provider_registration(
    provider: str,
    binding_id: str,
    profile_id: str,
) -> dict[str, Any] | None:
    """Secret送信前にWorkerの登録枠・既存所有を確認する。旧Workerのprimaryだけは従来経路を許可。"""
    try:
        options = _owner_request("GET", "/v1/provider-profile-registration-options")
    except LiteTravelHTTPError as exc:
        if exc.status_code == 404 and _is_legacy_primary_registration(provider, binding_id):
            if profile_id != PROVIDER_SECRET_METADATA[provider]["default_profile_id"]:
                raise LiteTravelError(
                    "独自のAI接続IDを再登録する前にLite用クラウドを更新してください。"
                ) from exc
            return None
        if exc.status_code == 404:
            raise LiteTravelError("Lite用クラウドを更新してから予備キーを登録してください。") from exc
        raise LiteTravelError("Lite用クラウドの登録枠を確認できないため、Secret登録を停止しました。") from exc
    except LiteTravelUncertainRequestError as exc:
        raise LiteTravelError("Lite用クラウドの登録枠を確認できないため、Secret登録を停止しました。") from exc
    return _validate_provider_registration_options(options, provider, binding_id, profile_id)


def register_provider_secret(
    provider: str,
    local_key_reference: str,
    secret_binding_id: str,
    credential_profile_id: str,
    display_name: str,
    wrangler_config_path: str,
    *,
    runner=subprocess.run,
    node_command: str | Path | None = None,
) -> Dict[str, Any]:
    """明示操作された1件だけをWrangler Secretへ登録し、非秘密profileをWorkerへ同期する。"""
    provider = str(provider or "").strip().lower()
    metadata = PROVIDER_SECRET_METADATA.get(provider)
    if not metadata or secret_binding_id not in metadata["bindings"]:
        raise LiteTravelError("プロバイダとCloudflare Secret bindingの対応が不正です。")
    profile_id = str(credential_profile_id or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,99}", profile_id):
        raise LiteTravelError("資格情報プロファイルIDが不正です。")
    source_metadata = get_local_key_metadata(provider, local_key_reference)
    safe_display_name = build_provider_profile_display_name(
        provider,
        local_key_reference,
        display_name,
    )
    if not safe_display_name or len(safe_display_name) > 100:
        raise LiteTravelError("資格情報プロファイル表示名は1〜100文字で指定してください。")
    _preflight_provider_registration(provider, secret_binding_id, profile_id)
    secret_value = _resolve_local_key(provider, local_key_reference)
    relay_root, config_path = _resolve_wrangler_config(wrangler_config_path)
    try:
        node, wrangler, _wrangler_cli, _runtime = (
            lite_cloud_setup.resolve_lite_command_runtime(
                relay_root, node_command=node_command, runner=runner
            )
        )
    except lite_cloud_setup.LiteCloudSetupError as exc:
        raise LiteTravelError(
            "Lite専用runtimeを確認できません。準備ツールの修復を実行してください。"
        ) from exc
    try:
        result = runner(
            [node, str(wrangler), "secret", "put", secret_binding_id, "--config", config_path.as_posix()],
            cwd=relay_root.as_posix(),
            input=secret_value + "\n",
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiteTravelError("Cloudflare Secret登録を実行できませんでした。") from exc
    if result.returncode != 0:
        raise LiteTravelError("Cloudflare Secret登録に失敗しました。Wranglerの認証と設定を確認してください。")
    try:
        profile = _owner_request(
            "PUT",
            f"/v1/provider-profiles/{profile_id}",
            json_body={
                "display_name": safe_display_name,
                "provider": provider,
                "secret_binding_id": secret_binding_id,
                "allowed_base_url_id": metadata["base_url_id"],
                "enabled": True,
            },
        )
    except Exception as exc:
        raise LiteTravelError(
            "Cloudflare Secretは登録されましたが、資格情報プロファイルの同期に失敗しました。"
            "同じ内容で再実行するか、不要ならwrangler secret deleteで削除してください。"
        ) from exc
    remember_registered_provider_profile(
        profile_id,
        provider,
        safe_display_name,
        **source_metadata,
    )
    return {
        "registered": True,
        "profile": profile,
        "provider": provider,
        "credential_profile_id": profile_id,
        "display_name": safe_display_name,
        **source_metadata,
    }


def update_provider_profile_display_name(
    provider: str,
    local_key_reference: str,
    secret_binding_id: str,
    credential_profile_id: str,
) -> Dict[str, Any]:
    """登録済みprofileの表示名だけを本体キー名へ同期する。

    登録枠とprofileの所有関係をOwner APIで二重に確認してから、既存の
    provider/binding/base URL/enabledをそのままPUTする。Secret解決やWrangler
    実行はこの経路では行わず、Owner APIの更新成功後だけローカル表示を更新する。
    """
    normalized_provider = str(provider or "").strip().lower()
    metadata = PROVIDER_SECRET_METADATA.get(normalized_provider)
    binding = str(secret_binding_id or "").strip()
    profile_id = str(credential_profile_id or "").strip()
    if not metadata or binding not in metadata["bindings"]:
        raise LiteTravelError("プロバイダとCloudflare Secret bindingの対応が不正です。")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,99}", profile_id):
        raise LiteTravelError("資格情報プロファイルIDが不正です。")

    source_metadata = get_local_key_metadata(normalized_provider, local_key_reference)

    # 旧Workerや未登録枠では、表示名だけの更新を許可しない。既存登録である
    # registered_profile_idの一致まで確認し、同一bindingの別profile競合も弾く。
    try:
        options = _owner_request("GET", "/v1/provider-profile-registration-options")
    except LiteTravelHTTPError as exc:
        if exc.status_code == 404:
            raise LiteTravelError(
                "表示名だけの更新には、登録枠対応を確認できる最新版のLite用クラウドが必要です。"
            ) from exc
        raise
    selected = _validate_provider_registration_options(
        options,
        normalized_provider,
        binding,
        profile_id,
    )
    if selected.get("registered_profile_id") != profile_id:
        raise LiteTravelError("選択した登録枠はLiteに登録済みではありません。Secretの再登録は行わず、枠を確認してください。")

    profiles_body = _owner_request("GET", "/v1/provider-profiles")
    if not isinstance(profiles_body, dict) or not isinstance(profiles_body.get("profiles"), list):
        raise LiteTravelError("Lite用クラウドの既存接続一覧が不正です。表示名を更新しませんでした。")
    matches = [
        item
        for item in profiles_body["profiles"]
        if isinstance(item, dict)
        and str(item.get("credential_profile_id") or "").strip() == profile_id
    ]
    if len(matches) != 1:
        raise LiteTravelError("更新対象のLite接続が一意に確認できません。表示名を更新しませんでした。")
    current = matches[0]
    current_provider = str(current.get("provider") or "").strip().lower()
    current_base_url_id = str(current.get("allowed_base_url_id") or "").strip()
    current_enabled = current.get("enabled")
    if current_provider != normalized_provider:
        raise LiteTravelError("既存Lite接続のプロバイダが登録枠と一致しません。表示名を更新しませんでした。")
    if current_base_url_id != str(metadata["base_url_id"]):
        raise LiteTravelError("既存Lite接続の接続先が登録枠と一致しません。表示名を更新しませんでした。")
    if not isinstance(current_enabled, bool):
        raise LiteTravelError("既存Lite接続の有効状態を確認できません。表示名を更新しませんでした。")

    safe_display_name = build_provider_profile_display_name(
        normalized_provider,
        local_key_reference,
        str(current.get("display_name") or ""),
    )
    saved = _owner_request(
        "PUT",
        f"/v1/provider-profiles/{profile_id}",
        json_body={
            "display_name": safe_display_name,
            "provider": normalized_provider,
            "secret_binding_id": binding,
            "allowed_base_url_id": current_base_url_id,
            "enabled": current_enabled,
        },
    )
    if not isinstance(saved, dict):
        raise LiteTravelError("表示名の更新結果を確認できませんでした。ローカル表示を更新しませんでした。")
    if (
        str(saved.get("credential_profile_id") or "").strip() != profile_id
        or str(saved.get("display_name") or "").strip() != safe_display_name
        or str(saved.get("provider") or "").strip().lower() != normalized_provider
        or str(saved.get("allowed_base_url_id") or "").strip() != current_base_url_id
        or saved.get("enabled") is not current_enabled
    ):
        raise LiteTravelError("表示名の更新結果が既存接続と一致しません。ローカル表示を更新しませんでした。")

    remember_registered_provider_profile(
        profile_id,
        normalized_provider,
        safe_display_name,
        **source_metadata,
    )
    return {
        "updated": True,
        "profile": saved,
        "provider": normalized_provider,
        "credential_profile_id": profile_id,
        "display_name": safe_display_name,
        **source_metadata,
    }


# UI配線・隔離検証でlabel表現を使う場合の別名。display_name名を正本に
# 残し、既存のローカル呼び出しやテストも同じ実装へ接続する。
update_provider_profile_label = update_provider_profile_display_name


def _assert_portable_text(name: str, value: str) -> str:
    value = str(value or "")
    if len(value) > MAX_SECTION_CHARS.get(name, 20_000):
        raise LiteTravelError(f"{name}がPhase 1のサイズ上限を超えています。")
    if _SECRET_VALUE_PATTERN.search(value):
        raise LiteTravelError(f"{name}に秘密情報らしき値が含まれています。")
    if _ABSOLUTE_PATH_PATTERN.search(value):
        raise LiteTravelError(f"{name}にローカル絶対パスが含まれています。")
    return value


def _recent_messages(room_name: str, limit: int = 40) -> list[dict[str, str]]:
    log_path, *_ = room_manager.get_room_files_paths(room_name)
    if not log_path:
        return []
    resolved_limit = max(0, min(int(limit), 40))
    if resolved_limit == 0:
        return []
    messages = utils.load_chat_log(log_path)
    result: list[dict[str, str]] = []
    # 全履歴を検査してから末尾を切ると、持ち出し対象外の古い巨大メッセージや
    # 秘密らしき文字列までPhase 1の出発を妨げる。新しい側から必要件数だけ選び、
    # 実際にsnapshotへ含める本文に対してのみ安全検査を行う。
    for message in reversed(messages):
        role = str(message.get("role") or "").upper()
        if role not in {"USER", "AGENT"}:
            continue
        content = utils.clean_persona_text(str(message.get("content") or "")).strip()
        if not content:
            continue
        result.append({
            "role": "user" if role == "USER" else "assistant",
            "content": _assert_portable_text("recent_message", content),
        })
        if len(result) >= resolved_limit:
            break
    result.reverse()
    return result


def build_snapshot(
    room_name: str,
    system_prompt: str,
    core_memory: str = "",
    episodic_summary: str = "",
    *,
    credential_profile_id: Optional[str] = None,
    model_id: Optional[str] = None,
    retention_days: Optional[int] = None,
    travel_session_id: Optional[str] = None,
    created_at: Optional[str] = None,
    include_core_memory: bool = True,
    recent_message_limit: int = 40,
) -> Dict[str, Any]:
    room_name = _validate_room_name(room_name)
    if not (Path(constants.ROOMS_DIR) / room_name).is_dir():
        raise LiteTravelError("対象ペルソナが見つかりません。")
    settings = get_settings()
    resolved_profile = str(
        credential_profile_id or settings.get("credential_profile_id") or DEFAULT_CREDENTIAL_PROFILE_ID
    ).strip()
    resolved_model = str(model_id or settings.get("model_id") or "").strip()
    resolved_retention = int(retention_days if retention_days is not None else settings.get("retention_days", 7))
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,99}", resolved_profile):
        raise LiteTravelError("資格情報プロファイルIDが不正です。")
    if not resolved_model or resolved_retention not in VALID_RETENTION_DAYS:
        raise LiteTravelError("モデルまたは本文保持日数の設定が不正です。")
    prompt = _assert_portable_text("system_prompt", system_prompt).strip()
    if not prompt:
        raise LiteTravelError("システムプロンプトは空にできません。")
    room_config = room_manager.get_room_config(room_name) or {}
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "travel_session_id": travel_session_id or str(uuid.uuid4()),
        "persona_id": room_name,
        "persona_display_name": str(room_config.get("room_name") or room_name),
        "system_prompt": prompt,
        "core_memory": _assert_portable_text("core_memory", core_memory) if include_core_memory else "",
        "episodic_summary": _assert_portable_text("episodic_summary", episodic_summary),
        "recent_messages": _recent_messages(room_name, recent_message_limit),
        "initial_route": {
            "credential_profile_id": resolved_profile,
            "model_id": resolved_model,
        },
        "budget": {
            "daily_limit_usd": settings.get("budget_daily_limit_usd"),
            "session_limit_usd": settings.get("budget_session_limit_usd"),
            "warning_ratio": float(settings.get("budget_warning_ratio", 0.8)),
            "allow_unknown_price": bool(settings.get("budget_allow_unknown_price", False)),
            "max_output_tokens": _normalize_max_output_tokens(settings.get("budget_max_output_tokens")),
            "timezone": str(settings.get("budget_timezone") or "Asia/Tokyo"),
        },
        "cache_policy": str(settings.get("cache_policy") or "auto"),
        "retention_days": resolved_retention,
        "created_at": created_at or _now_iso(),
    }
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized) > MAX_SNAPSHOT_CHARS:
        raise LiteTravelError("snapshot全体がサイズ上限を超えています。")
    return snapshot


def _require_matching_presence_session(state: Optional[Dict[str, Any]], travel_session_id: str) -> None:
    if (
        state
        and state.get("status") in LOCKING_STATES
        and str(state.get("travel_session_id") or "") != str(travel_session_id or "")
    ):
        raise LiteTravelError(
            "別のお出かけの操作では現在の状態を変更できません。現在のお出かけの状態を確認してください。"
        )


def set_presence_state(room_name: str, status: str, travel_session_id: str, **details: Any) -> Dict[str, Any]:
    room_name = _validate_room_name(room_name)
    if status not in {"armed", "active", "returning", "closed", "emergency_reclaimed"}:
        raise LiteTravelError("不正な存在ロック状態です。")
    # 判定と保存を同じロック内で行い、古い操作や同時操作による差し替えを防ぐ。
    with file_lock_utils.locked_file(_presence_path(room_name).as_posix()):
        previous = presence_status(room_name)
        _require_matching_presence_session(previous, travel_session_id)
        allowed = {
            None: {"armed"},
            "armed": {"active", "closed", "emergency_reclaimed"},
            "active": {"returning", "emergency_reclaimed"},
            "returning": {"closed", "emergency_reclaimed"},
            "closed": {"armed"},
            "emergency_reclaimed": {"armed", "closed"},
        }
        previous_status = previous.get("status") if previous else None
        if status != previous_status and status not in allowed.get(previous_status, set()):
            raise LiteTravelError(f"存在ロックを{previous_status or '未設定'}から{status}へ変更できません。")
        value = {
            "schema_version": SCHEMA_VERSION,
            "room_name": room_name,
            "travel_session_id": travel_session_id,
            "status": status,
            "updated_at": _now_iso(),
            **details,
        }
        _write_json(_presence_path(room_name), value)
        return value


def presence_status(room_name: str) -> Optional[Dict[str, Any]]:
    room_name = _validate_room_name(room_name)
    path = _presence_path(room_name)
    if not path.exists():
        return None
    try:
        value = _read_json(path, None)
        return value if isinstance(value, dict) else {"status": "armed", "corrupted": True}
    except Exception:
        return {"status": "armed", "corrupted": True}


def is_presence_locked(room_name: str) -> bool:
    value = presence_status(room_name)
    return bool(
        value
        and value.get("status") in LOCKING_STATES
        and value.get("presence_mode", "exclusive") == "exclusive"
    )


def record_deferred_home_event(room_name: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """単一存在中に本体へ届いたイベントを破棄せずローカル待機列へ積む。"""
    room_name = _validate_room_name(room_name)
    queue_path = _metadata_root() / "deferred_events" / f"{_room_key(room_name)}.jsonl"
    item = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "room_name": room_name,
        "event_type": str(event_type or "unknown")[:100],
        "occurred_at": _now_iso(),
        "payload": payload if isinstance(payload, dict) else {},
    }
    with file_lock_utils.locked_file(queue_path.as_posix()):
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with queue_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


def emergency_reclaim(room_name: str, reason: str) -> Dict[str, Any]:
    state = presence_status(room_name)
    if not state or state.get("status") not in LOCKING_STATES:
        raise LiteTravelError("緊急帰還できるお出かけセッションがありません。")
    return set_presence_state(
        room_name,
        "emergency_reclaimed",
        str(state.get("travel_session_id") or ""),
        emergency_reason=str(reason or "").strip()[:500],
        branch_divergence_possible=True,
    )


def emergency_reclaim_remote(room_name: str, reason: str) -> Dict[str, Any]:
    state = emergency_reclaim(room_name, reason)
    session_id = str(state.get("travel_session_id") or "")
    try:
        remote = _owner_request(
            "POST",
            f"/v1/travel-sessions/{session_id}/personas/{quote(room_name, safe='')}/emergency-reclaim",
            json_body={"reason": str(reason or "").strip()[:500]},
        )
        return {**state, "remote": remote, "remote_pending": False}
    except Exception as exc:
        return {**state, "remote_pending": True, "remote_error": str(exc)}


def preview_online_return(travel_session_id: str) -> Dict[str, Any]:
    _require_worker_compatibility("帰宅内容の確認", resumable_return=True)
    manifest = _owner_request("GET", f"/v1/travel-sessions/{travel_session_id}/return/manifest")
    preview = []
    for persona in manifest.get("personas") or []:
        room_name = _validate_room_name(str(persona.get("persona_id") or ""))
        state = presence_status(room_name) or {}
        anchor_changed = False
        try:
            current_anchor = _home_anchor(room_name, _now_iso())["log_tail_hash"]
            anchor_changed = current_anchor != str(persona.get("home_anchor_hash") or "")
        except Exception:
            anchor_changed = True
        preview.append({
            "persona_id": room_name,
            "display_name": str(persona.get("display_name") or room_name),
            "presence_mode": str(persona.get("presence_mode") or "exclusive"),
            "high_water_sequence": int(persona.get("high_water_sequence") or 0),
            "home_anchor_changed": anchor_changed,
            "emergency_reclaimed": state.get("status") == "emergency_reclaimed",
            "branch_divergence_possible": bool(persona.get("branch_divergence_possible")),
            "activation_mode": str((manifest.get("session") or {}).get("activation_mode") or "planned"),
        })
    return {"travel_session_id": travel_session_id, "personas": preview}


def save_route_proposals(persona_results: Iterable[Dict[str, Any]]) -> list[tuple[str, str]]:
    choices = []
    for result in persona_results:
        room_name = _validate_room_name(str(result.get("persona_id") or ""))
        route = result.get("final_route")
        if not isinstance(route, dict):
            continue
        value = {
            "schema_version": 1, "persona_id": room_name, "final_route": route,
            "created_at": _now_iso(), "applied": False,
        }
        _write_json(_metadata_root() / "route_proposals" / f"{_room_key(room_name)}.json", value)
        choices.append((f"{room_name}: {route.get('provider')} / {route.get('model_id')}", room_name))
    return choices


def apply_route_proposals(room_names: Iterable[str]) -> int:
    settings = get_settings()
    templates = dict(settings.get("persona_templates") or {})
    applied = 0
    for room_name in dict.fromkeys(room_names or []):
        room_name = _validate_room_name(room_name)
        path = _metadata_root() / "route_proposals" / f"{_room_key(room_name)}.json"
        proposal = _read_json(path, None)
        route = proposal.get("final_route") if isinstance(proposal, dict) else None
        if not isinstance(route, dict) or not route.get("credential_profile_id") or not route.get("model_id"):
            continue
        templates[room_name] = {
            "credential_profile_id": str(route["credential_profile_id"]),
            "model_id": str(route["model_id"]),
        }
        proposal["applied"] = True
        proposal["applied_at"] = _now_iso()
        _write_json(path, proposal)
        applied += 1
    if applied:
        settings["persona_templates"] = templates
        if not config_manager.update_config_keys({"lite_travel_settings": settings}):
            raise LiteTravelError("最終routeテンプレートを保存できませんでした。")
    return applied


def _owner_request(method: str, path: str, *, json_body: Any = None, timeout: float = 30.0) -> Any:
    settings = get_settings()
    worker_url = str(settings.get("worker_url") or "").rstrip("/")
    owner_token = str(settings.get("owner_token") or "")
    if not worker_url or not owner_token:
        raise LiteTravelError("Worker接続設定が未完了です。")
    try:
        response = requests.request(
            method,
            f"{worker_url}{path}",
            headers={"Authorization": f"Bearer {owner_token}", "Content-Type": "application/json"},
            json=json_body,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise LiteTravelUncertainRequestError("Workerへ接続できませんでした。") from exc
    if 300 <= response.status_code < 400:
        raise LiteTravelError("Workerからのredirectを拒否しました。")
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not response.ok:
        code = body.get("error") if isinstance(body, dict) else None
        if response.status_code >= 500:
            raise LiteTravelUncertainRequestError(f"Worker操作に失敗しました（{code or response.status_code}）。")
        raise LiteTravelHTTPError(
            f"Worker操作に失敗しました（{code or response.status_code}）。",
            response.status_code,
            str(code) if code else None,
        )
    return body


def validate_snapshot_initial_routes(snapshot: Dict[str, Any]) -> list[Dict[str, Any]]:
    """snapshot登録前に初期経路をWorkerのliveモデル一覧と料金ガードへ照合する。"""

    if not isinstance(snapshot, dict):
        raise LiteTravelError("初期経路を確認するsnapshotが不正です。")
    if snapshot.get("schema_version") == MULTI_SNAPSHOT_SCHEMA_VERSION:
        personas = snapshot.get("personas")
        if not isinstance(personas, list) or not personas:
            raise LiteTravelError("初期経路を確認するペルソナ情報が不正です。")
        routes = [
            (
                str(persona.get("persona_display_name") or persona.get("persona_id") or "ペルソナ"),
                persona.get("initial_route"),
            )
            for persona in personas
            if isinstance(persona, dict)
        ]
        budget = snapshot.get("session_budget")
    else:
        routes = [
            (
                str(snapshot.get("persona_display_name") or snapshot.get("persona_id") or "ペルソナ"),
                snapshot.get("initial_route"),
            )
        ]
        budget = snapshot.get("budget")
    if not routes:
        raise LiteTravelError("初期経路を確認するペルソナ情報が不正です。")
    catalogs: Dict[str, Dict[str, Any]] = {}
    validated = []
    for persona_label, route in routes:
        if not isinstance(route, dict):
            raise LiteTravelError(f"{persona_label}の初期経路が不正です。")
        profile_id = str(route.get("credential_profile_id") or "").strip()
        model_id = str(route.get("model_id") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,99}", profile_id) or not model_id:
            raise LiteTravelError(f"{persona_label}の初期経路が不正です。")
        if profile_id not in catalogs:
            try:
                catalog = _owner_request(
                    "GET",
                    f"/v1/provider-profiles/{quote(profile_id, safe='')}/models?refresh=1",
                    timeout=30,
                )
            except LiteTravelError as exc:
                raise LiteTravelError(
                    f"{persona_label}の初期モデル一覧を確認できません。"
                    "利用可能な経路を明示選択してから再試行してください。"
                ) from exc
            if not isinstance(catalog, dict) or catalog.get("source") != "live":
                raise LiteTravelError(
                    f"{persona_label}の初期モデルをlive一覧で確認できません。"
                    "現在の経路を変更せず、再試行してください。"
                )
            catalogs[profile_id] = catalog
        catalog = catalogs[profile_id]
        models = catalog.get("models")
        selected = next(
            (
                item for item in models
                if isinstance(item, dict) and item.get("model_id") == model_id
            ),
            None,
        ) if isinstance(models, list) else None
        if not selected or selected.get("available") is not True:
            reason = selected.get("unavailable_reason") if isinstance(selected, dict) else "model_not_listed"
            raise LiteTravelError(
                f"{persona_label}の初期モデル「{model_id}」は利用可能と確認できません"
                f"（{reason or 'unavailable'}）。利用可能な経路を明示選択してください。"
            )
        pricing_known = selected.get("pricing_known") is True
        validated.append({
            "persona": persona_label,
            "credential_profile_id": profile_id,
            "model_id": model_id,
            "pricing_known": pricing_known,
            "catalog_source": "live",
        })
    return validated


def verify_owner_token(candidate_token: str) -> bool:
    settings = get_settings()
    worker_url = str(settings.get("worker_url") or "").rstrip("/")
    candidate = str(candidate_token or "").strip()
    if not worker_url or len(candidate) < 16:
        return False
    try:
        response = requests.get(
            f"{worker_url}/v1/admin/diagnostics",
            headers={"Authorization": f"Bearer {candidate}"},
            timeout=15,
            allow_redirects=False,
        )
    except requests.RequestException:
        return False
    return response.status_code == 200


def promote_owner_token(candidate_token: str) -> bool:
    """Workerが新Tokenを受理した後にだけローカル正本を切り替える。"""
    candidate = str(candidate_token or "").strip()
    if not verify_owner_token(candidate):
        raise LiteTravelError("新しい所有者Tokenの検証に成功していないため切り替えません。")
    settings = get_settings()
    settings["owner_token"] = candidate
    return bool(config_manager.update_config_keys({"lite_travel_settings": settings}))


def promote_bundle_signing_key(candidate_key: str, *, worker_verified: bool) -> bool:
    """旧bundle検証鍵をringへ残したまま署名鍵正本を切り替える。"""
    candidate = str(candidate_key or "").strip()
    if len(candidate) < 16 or not worker_verified:
        raise LiteTravelError("Workerの新しい署名鍵を検証してから切り替えてください。")
    settings = get_settings()
    current = str(settings.get("bundle_signing_key") or "").strip()
    previous = [str(item) for item in (settings.get("bundle_signing_key_previous") or []) if item]
    if current and current != candidate:
        previous.insert(0, current)
    settings["bundle_signing_key"] = candidate
    settings["bundle_signing_key_previous"] = list(dict.fromkeys(previous))[:2]
    return bool(config_manager.update_config_keys({"lite_travel_settings": settings}))


def _reserve_departure(room_name: str, session_id: str, presence_mode: str) -> None:
    # 同じsnapshotの再実行も拒否し、結果不明の登録を上書きしない。
    room_name = _validate_room_name(room_name)
    with file_lock_utils.locked_file(_presence_path(room_name).as_posix()):
        existing = presence_status(room_name)
        if existing and existing.get("status") in LOCKING_STATES:
            raise LiteTravelError(f"{room_name}はすでにお出かけ中、または出発結果の確認待ちです。")
        set_presence_state(room_name, "armed", session_id, presence_mode=presence_mode)


def start_departure(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    room_name = str(snapshot.get("persona_id") or "")
    session_id = str(snapshot.get("travel_session_id") or "")
    if not room_name or not session_id:
        raise LiteTravelError("snapshotのセッション対応が不正です。")
    validate_snapshot_initial_routes(snapshot)
    _reserve_departure(room_name, session_id, "exclusive")
    try:
        result = _owner_request("POST", "/v1/travel-sessions", json_body=snapshot)
    except LiteTravelUncertainRequestError as exc:
        raise LiteTravelUncertainRequestError(
            "出発の結果を確認できませんでした。本体の停止を保ったまま、通信回復後に「帰宅する内容を確認」へ進んでください。"
        ) from exc
    except Exception:
        set_presence_state(room_name, "closed", session_id, departure_failed=True)
        raise
    try:
        set_presence_state(room_name, "active", session_id)
    except Exception as exc:
        raise LiteTravelUncertainRequestError(
            "クラウドへの出発登録は完了しましたが、本体の状態を保存できませんでした。保存先を確認し、「帰宅する内容を確認」へ進んでください。"
        ) from exc
    return result


def _room_snapshot_source(
    room_name: str,
    *,
    include_episodic_memory: bool = True,
    episodic_memory_days: int = 2,
) -> tuple[str, str, str]:
    _, system_prompt_path, _, memory_path, *_ = room_manager.get_room_files_paths(room_name)
    if not system_prompt_path or not Path(system_prompt_path).is_file():
        raise LiteTravelError(f"{room_name}のシステムプロンプトが見つかりません。")
    system_prompt = Path(system_prompt_path).read_text(encoding="utf-8")
    core_memory = Path(memory_path).read_text(encoding="utf-8") if memory_path and Path(memory_path).is_file() else ""
    resolved_episodic_days = max(0, min(int(episodic_memory_days or 0), 30))
    episodic_summary = ""
    if include_episodic_memory and resolved_episodic_days > 0:
        from episodic_memory_manager import EpisodicMemoryManager

        episodic_summary = EpisodicMemoryManager(room_name).export_recent_memories(resolved_episodic_days)
    return system_prompt, core_memory, episodic_summary


def _home_anchor(room_name: str, created_at: str) -> Dict[str, str]:
    log_path, *_ = room_manager.get_room_files_paths(room_name)
    tail = b""
    if log_path and Path(log_path).is_file():
        with Path(log_path).open("rb") as stream:
            try:
                stream.seek(-65536, os.SEEK_END)
            except OSError:
                stream.seek(0)
            tail = stream.read()
    return {"created_at": created_at, "log_tail_hash": hashlib.sha256(tail).hexdigest()}


def build_multi_snapshot(
    room_names: Iterable[str],
    *,
    parallel_rooms: Optional[Iterable[str]] = None,
    travel_session_id: Optional[str] = None,
    created_at: Optional[str] = None,
    include_core_memory: bool = True,
    include_episodic_memory: bool = True,
    episodic_memory_days: int = 2,
    recent_message_limit: int = 40,
) -> Dict[str, Any]:
    """複数ペルソナを1旅行セッションへ束ねたsnapshot v4を生成する。"""
    selected = [_validate_room_name(name) for name in dict.fromkeys(room_names or [])]
    if not 1 <= len(selected) <= MAX_TRAVEL_PERSONAS:
        raise LiteTravelError(f"持ち出すペルソナは1〜{MAX_TRAVEL_PERSONAS}件で選択してください。")
    parallel = {_validate_room_name(name) for name in (parallel_rooms or [])}
    if not parallel.issubset(set(selected)):
        raise LiteTravelError("並行存在の対象は持ち出すペルソナから選択してください。")
    settings = get_settings()
    timestamp = created_at or _now_iso()
    profile_id = str(settings.get("credential_profile_id") or DEFAULT_CREDENTIAL_PROFILE_ID).strip()
    model_id = str(settings.get("model_id") or "").strip()
    retention_days = int(settings.get("retention_days", 7))
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,99}", profile_id) or not model_id:
        raise LiteTravelError("初期経路の設定が不正です。")
    if retention_days not in VALID_RETENTION_DAYS:
        raise LiteTravelError("本文保持日数の設定が不正です。")
    personas = []
    persona_templates = settings.get("persona_templates") if isinstance(settings.get("persona_templates"), dict) else {}
    for room_name in selected:
        if not (Path(constants.ROOMS_DIR) / room_name).is_dir():
            raise LiteTravelError(f"対象ペルソナ {room_name} が見つかりません。")
        prompt, core_memory, episodic_summary = _room_snapshot_source(
            room_name,
            include_episodic_memory=include_episodic_memory,
            episodic_memory_days=episodic_memory_days,
        )
        room_config = room_manager.get_room_config(room_name) or {}
        template = persona_templates.get(room_name) if isinstance(persona_templates.get(room_name), dict) else {}
        persona_profile = str(template.get("credential_profile_id") or profile_id).strip()
        persona_model = str(template.get("model_id") or model_id).strip()
        personas.append({
            "persona_id": room_name,
            "persona_display_name": str(room_config.get("room_name") or room_name),
            "presence_mode": "parallel" if room_name in parallel else "exclusive",
            "system_prompt": _assert_portable_text("system_prompt", prompt).strip(),
            "core_memory": _assert_portable_text("core_memory", core_memory) if include_core_memory else "",
            "episodic_summary": _assert_portable_text("episodic_summary", episodic_summary),
            "recent_messages": _recent_messages(room_name, recent_message_limit),
            "home_anchor": _home_anchor(room_name, timestamp),
            "initial_route": {"credential_profile_id": persona_profile, "model_id": persona_model},
            "budget": {
                "daily_limit_usd": settings.get("budget_daily_limit_usd"),
                "session_limit_usd": settings.get("budget_session_limit_usd"),
                "max_output_tokens": _normalize_max_output_tokens(settings.get("budget_max_output_tokens")),
            },
            "cache_policy": str(settings.get("cache_policy") or "auto"),
        })
    snapshot = {
        "schema_version": MULTI_SNAPSHOT_SCHEMA_VERSION,
        "travel_session_id": travel_session_id or str(uuid.uuid4()),
        "retention_days": retention_days,
        "session_budget": {
            "daily_limit_usd": settings.get("budget_daily_limit_usd"),
            "session_limit_usd": settings.get("budget_session_limit_usd"),
            "warning_ratio": float(settings.get("budget_warning_ratio", 0.8)),
            "allow_unknown_price": bool(settings.get("budget_allow_unknown_price", False)),
            "max_output_tokens": _normalize_max_output_tokens(settings.get("budget_max_output_tokens")),
            "timezone": str(settings.get("budget_timezone") or "Asia/Tokyo"),
        },
        "personas": personas,
        "created_at": timestamp,
    }
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized) > MAX_SNAPSHOT_CHARS * 2:
        raise LiteTravelError("複数ペルソナsnapshot全体がサイズ上限を超えています。")
    return snapshot


def _standby_home_instance_id() -> str:
    settings = get_settings()
    value = str(settings.get("standby_home_instance_id") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,99}", value):
        return value
    value = f"home-{uuid.uuid4()}"
    settings["standby_home_instance_id"] = value
    if not config_manager.update_config_keys({"lite_travel_settings": settings}):
        raise LiteTravelError("待機snapshot用の本体IDを保存できませんでした。")
    return value


def prepare_standby_snapshot(
    room_names: Iterable[str],
    *,
    parallel_rooms: Optional[Iterable[str]] = None,
    retention_days: Optional[int] = None,
    automatic: bool = False,
    include_core_memory: bool = True,
    include_episodic_memory: bool = True,
    episodic_memory_days: int = 2,
    recent_message_limit: int = 40,
    prepared_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """存在ロックを開始せず、暗号化待機snapshotをWorkerへ直接登録する。"""
    _require_worker_compatibility("お出かけ前データの準備")
    selected_rooms = [_validate_room_name(name) for name in dict.fromkeys(room_names or [])]
    for room_name in selected_rooms:
        state = presence_status(room_name)
        if state and state.get("status") in LOCKING_STATES:
            raise LiteTravelError(f"{room_name}はお出かけ中のため待機snapshotを更新できません。")
    settings = get_settings()
    resolved_retention = int(
        retention_days if retention_days is not None else settings.get("standby_retention_days", 7)
    )
    if not 1 <= resolved_retention <= 30:
        raise LiteTravelError("待機snapshot保持日数は1〜30日で指定してください。")
    if prepared_snapshot is None:
        snapshot = build_multi_snapshot(
            selected_rooms,
            parallel_rooms=parallel_rooms,
            include_core_memory=include_core_memory,
            include_episodic_memory=include_episodic_memory,
            episodic_memory_days=episodic_memory_days,
            recent_message_limit=recent_message_limit,
        )
    else:
        snapshot = json.loads(json.dumps(prepared_snapshot, ensure_ascii=False))
        personas = snapshot.get("personas") if isinstance(snapshot, dict) else None
        snapshot_rooms = [
            str(item.get("persona_id") or "")
            for item in (personas or [])
            if isinstance(item, dict)
        ]
        if snapshot.get("schema_version") != MULTI_SNAPSHOT_SCHEMA_VERSION or snapshot_rooms != selected_rooms:
            raise LiteTravelError("確認したsnapshotと選択中のペルソナが一致しません。")
    validate_snapshot_initial_routes(snapshot)
    hash_source = json.loads(json.dumps(snapshot, ensure_ascii=False))
    hash_source.pop("travel_session_id", None)
    hash_source.pop("created_at", None)
    for persona in hash_source.get("personas", []):
        if isinstance(persona, dict) and isinstance(persona.get("home_anchor"), dict):
            persona["home_anchor"].pop("created_at", None)
    input_hash = hashlib.sha256(
        json.dumps(hash_source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    state_path = _metadata_root() / "standby" / "refresh_state.json"
    refresh_state = _read_json(state_path, {})
    latest = latest_standby_manifest()
    if automatic and isinstance(refresh_state, dict) and latest:
        succeeded_at = str(refresh_state.get("succeeded_at") or "")
        try:
            succeeded = dt.datetime.fromisoformat(succeeded_at.replace("Z", "+00:00"))
            elapsed = dt.datetime.now(dt.timezone.utc) - succeeded.astimezone(dt.timezone.utc)
        except (TypeError, ValueError):
            elapsed = dt.timedelta.max
        minimum = max(1, min(168, int(settings.get("standby_refresh_min_interval_hours", 6))))
        if elapsed < dt.timedelta(hours=minimum):
            return {**latest, "automatic_skipped": "minimum_interval"}
        if refresh_state.get("input_hash") == input_hash:
            return {**latest, "automatic_skipped": "unchanged"}
    manifest = _owner_request(
        "POST",
        "/v1/standby-snapshots",
        json_body={
            "home_instance_id": _standby_home_instance_id(),
            "retention_days": resolved_retention,
            "snapshot": snapshot,
        },
    )
    if not isinstance(manifest, dict) or any(
        key in manifest for key in ("snapshot", "snapshot_json", "ciphertext", "nonce", "owner_token")
    ):
        raise LiteTravelError("Workerの待機manifestに非公開情報が含まれています。")
    _write_json(_metadata_root() / "standby" / "latest_manifest.json", manifest)
    _write_json(state_path, {"input_hash": input_hash, "succeeded_at": _now_iso()})
    return manifest


def latest_standby_manifest() -> Optional[Dict[str, Any]]:
    value = _read_json(_metadata_root() / "standby" / "latest_manifest.json", None)
    return value if isinstance(value, dict) else None


def diagnose_worker() -> Dict[str, Any]:
    """public healthとowner診断を分離取得し、未知schemaでは安全停止する。"""
    settings = get_settings()
    worker_url = str(settings.get("worker_url") or "").rstrip("/")
    if not worker_url:
        return {"state": "unreachable", "error_code": "worker_url_missing"}
    try:
        response = requests.get(f"{worker_url}/v1/health", timeout=10, allow_redirects=False)
        if 300 <= response.status_code < 400:
            return {"state": "unreachable", "error_code": "redirect_rejected"}
        health = response.json() if response.ok else {}
    except (requests.RequestException, ValueError):
        return {"state": "unreachable", "error_code": "worker_unreachable"}
    api_schema = health.get("api_schema_version") if isinstance(health, dict) else None
    if not isinstance(api_schema, int):
        return {"state": "unknown_schema", "health": _safe_diagnostic_payload(health)}
    if api_schema > 10:
        return {"state": "client_update_required", "health": _safe_diagnostic_payload(health)}
    if api_schema < 10:
        return {"state": "worker_update_required", "health": _safe_diagnostic_payload(health)}
    public_d1_schema = health.get("d1_schema_version") if isinstance(health, dict) else None
    storage_schema_ready = health.get("storage_schema_ready") if isinstance(health, dict) else None
    if isinstance(public_d1_schema, int):
        if public_d1_schema > 10:
            return {"state": "client_update_required", "health": _safe_diagnostic_payload(health)}
        if public_d1_schema < 10:
            return {"state": "migration_required", "health": _safe_diagnostic_payload(health)}
    elif storage_schema_ready is False:
        return {"state": "migration_required", "health": _safe_diagnostic_payload(health)}
    try:
        diagnostics = _owner_request("GET", "/v1/admin/diagnostics", timeout=15)
    except LiteTravelError:
        return {"state": "unauthorized", "health": _safe_diagnostic_payload(health)}
    d1_schema = (diagnostics.get("d1") or {}).get("d1_schema_version") if isinstance(diagnostics, dict) else None
    if not isinstance(d1_schema, int):
        return {"state": "unknown_schema", "health": _safe_diagnostic_payload(health)}
    if d1_schema > 10:
        return {"state": "client_update_required", "health": _safe_diagnostic_payload(health)}
    if d1_schema < 10:
        return {"state": "migration_required", "health": _safe_diagnostic_payload(health)}
    state = str(diagnostics.get("state") or "unknown_schema") if isinstance(diagnostics, dict) else "unknown_schema"
    return {
        "state": state,
        "health": _safe_diagnostic_payload(health),
        "diagnostics": _safe_diagnostic_payload(diagnostics),
    }


def _require_worker_compatibility(action_label: str, *, resumable_return: bool = False) -> Dict[str, Any]:
    """本文を送る操作の直前にWorkerとD1の互換性を一般向け文言で確認する。"""
    diagnostic = diagnose_worker()
    state = str(diagnostic.get("state") or "unknown_schema")
    if state in {"ready", "maintenance_overdue"}:
        return diagnostic
    health = diagnostic.get("health") if isinstance(diagnostic.get("health"), dict) else {}
    if (
        resumable_return
        and state == "worker_update_required"
        and health.get("api_schema_version") == 9
        and health.get("d1_schema_version") == 10
    ):
        # schema 9→10は応答長制御の追加だけで、署名付き帰宅APIとD1 schemaは不変。
        # 更新前に既存セッションを閉じられるよう、帰宅操作だけ後方互換で許可する。
        return diagnostic
    messages = {
        "unreachable": "Lite用クラウドへ接続できません。インターネット接続とWorker URLを確認してください。",
        "unauthorized": "このPCの本体確認キーをLite用クラウドが確認できません。接続情報を保存し直してください。",
        "worker_update_required": (
            "Lite用クラウドのプログラムが古いため開始できません。"
            "「Lite用クラウドを準備・管理」の更新手順を完了してください。"
        ),
        "migration_required": (
            "Lite用クラウドの保存領域（D1）の更新が必要です。"
            "「Lite用クラウドを準備・管理」で復旧点を作成してから更新してください。"
        ),
        "client_update_required": "Nexus Ark本体がLite用クラウドより古いため、本体を対応版へ更新してください。",
        "secret_action_required": "Lite用クラウドの必須キー設定が不足しています。接続管理でキーを確認してください。",
        "unknown_schema": "Lite用クラウドと保存領域（D1）の版を確認できません。接続管理で4状態診断を実行してください。",
    }
    detail = messages.get(
        state,
        "Lite用クラウドの準備状態を確認できません。接続管理で4状態診断を実行してください。",
    )
    if resumable_return:
        detail += " 帰宅途中の記録は保持されています。更新・再接続後に「オンライン帰宅」をもう一度押すと続きから再開します。"
    raise LiteTravelError(f"{action_label}を開始できません。{detail}")


def _safe_diagnostic_payload(value: Any) -> Any:
    sensitive = re.compile(r"(authorization|token|secret|password|api.?key|snapshot|ciphertext|nonce)", re.I)
    if isinstance(value, dict):
        return {
            str(key): _safe_diagnostic_payload(item)
            for key, item in value.items()
            if not sensitive.search(str(key))
        }
    if isinstance(value, list):
        return [_safe_diagnostic_payload(item) for item in value]
    if isinstance(value, str):
        if _SECRET_VALUE_PATTERN.search(value) or _ABSOLUTE_PATH_PATTERN.search(value):
            return "[REDACTED]"
        return value[:1000]
    return value


def list_remote_devices() -> list[Dict[str, Any]]:
    result = _owner_request("GET", "/v1/admin/devices")
    devices = result.get("devices") if isinstance(result, dict) else None
    return [item for item in (devices or []) if isinstance(item, dict)]


def revoke_all_remote_devices() -> Dict[str, Any]:
    return _owner_request("POST", "/v1/admin/devices/revoke-all")


def revoke_remote_device(device_id: str) -> Dict[str, Any]:
    value = str(device_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", value):
        raise LiteTravelError("端末IDが不正です。")
    return _owner_request("POST", f"/v1/devices/{quote(value, safe='')}/revoke")


def cleanup_expired_remote_devices() -> Dict[str, Any]:
    return _owner_request("POST", "/v1/admin/devices/cleanup-expired")


def preview_remote_retention() -> Dict[str, Any]:
    return _owner_request("POST", "/v1/admin/maintenance/retention/preview")


def run_remote_retention() -> Dict[str, Any]:
    return _owner_request("POST", "/v1/admin/maintenance/retention/run")


def start_multi_departure(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if snapshot.get("schema_version") != MULTI_SNAPSHOT_SCHEMA_VERSION:
        raise LiteTravelError("複数ペルソナsnapshotのschemaが不正です。")
    session_id = str(snapshot.get("travel_session_id") or "")
    personas = snapshot.get("personas")
    if not session_id or not isinstance(personas, list) or not 1 <= len(personas) <= MAX_TRAVEL_PERSONAS:
        raise LiteTravelError("複数ペルソナsnapshotの対応が不正です。")
    validate_snapshot_initial_routes(snapshot)
    armed: list[tuple[str, str]] = []
    try:
        for persona in sorted(personas, key=lambda item: str(item.get("persona_id") or "")):
            room_name = _validate_room_name(str(persona.get("persona_id") or ""))
            mode = str(persona.get("presence_mode") or "")
            if mode not in {"exclusive", "parallel"}:
                raise LiteTravelError("存在方針が不正です。")
            _reserve_departure(room_name, session_id, mode)
            armed.append((room_name, mode))
        result = _owner_request("POST", "/v1/travel-sessions", json_body=snapshot)
    except LiteTravelUncertainRequestError as exc:
        raise LiteTravelUncertainRequestError(
            "出発の結果を確認できませんでした。本体の停止を保ったまま、通信回復後に「帰宅する内容を確認」へ進んでください。"
        ) from exc
    except Exception:
        for room_name, mode in armed:
            try:
                set_presence_state(room_name, "closed", session_id, presence_mode=mode, departure_failed=True)
            except Exception:
                pass
        raise
    # 登録後の保存失敗では、Workerで開始済みのお出かけをローカルだけ解除しない。
    try:
        for room_name, mode in armed:
            set_presence_state(room_name, "active", session_id, presence_mode=mode)
    except Exception as exc:
        raise LiteTravelUncertainRequestError(
            "クラウドへの出発登録は完了しましたが、本体の状態を保存できませんでした。保存先を確認し、「帰宅する内容を確認」へ進んでください。"
        ) from exc
    return result


PAIRING_CODE_TTL_MINUTES = (5, 15, 30)


def create_pairing_code(ttl_minutes: int = 15) -> Dict[str, Any]:
    if type(ttl_minutes) is not int or ttl_minutes not in PAIRING_CODE_TTL_MINUTES:
        raise LiteTravelError("コードの有効期限は5・15・30分から選んでください。")
    result = _owner_request("POST", "/v1/pairing-codes", json_body={"ttl_minutes": ttl_minutes})
    if not isinstance(result, dict) or type(result.get("ttl_minutes")) is not int or result["ttl_minutes"] != ttl_minutes:
        raise LiteTravelError("Lite用クラウドがコードの期限選択に対応していません。本体の接続管理からクラウド更新を確認してください。")
    return result


def export_return_bundle(travel_session_id: str) -> Dict[str, Any]:
    return _owner_request("POST", f"/v1/travel-sessions/{travel_session_id}/export")


def _verify_v4_signed(value: Dict[str, Any], signing_key: str) -> tuple[Dict[str, Any], str]:
    if value.get("algorithm") != "HMAC-SHA-256":
        raise LiteTravelError("帰宅データの署名方式が不正です。")
    canonical = value.get("payload_canonical")
    if not isinstance(canonical, str) or len(canonical) > 10_000_000:
        raise LiteTravelError("帰宅データ本文が不正です。")
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).digest()
    hash_text = __import__("base64").urlsafe_b64encode(payload_hash).decode("ascii").rstrip("=")
    if not hmac.compare_digest(hash_text, str(value.get("payload_hash") or "")):
        raise LiteTravelError("帰宅データのhashが一致しません。")
    previous = get_settings().get("bundle_signing_key_previous") or []
    keyring = [signing_key] + [str(item) for item in previous if isinstance(item, str)]
    signature_valid = False
    for candidate in dict.fromkeys(keyring):
        signature = hmac.new(candidate.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest()
        signature_text = __import__("base64").urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        if hmac.compare_digest(signature_text, str(value.get("signature") or "")):
            signature_valid = True
            break
    if not signature_valid:
        raise LiteTravelError("帰宅データの署名を検証できません。")
    try:
        payload = json.loads(canonical)
    except json.JSONDecodeError as exc:
        raise LiteTravelError("帰宅データ本文を解析できません。") from exc
    if payload != value.get("payload") or payload.get("schema_version") != 4:
        raise LiteTravelError("帰宅データのschema対応が不正です。")
    return payload, hash_text


def _validate_v4_persona_payload(payload: Dict[str, Any], expected_room: Optional[str] = None) -> tuple[str, str, int, int]:
    session = payload.get("session")
    persona = payload.get("persona")
    cursor = payload.get("cursor")
    events = payload.get("events")
    receipts = payload.get("receipts")
    if not all(isinstance(item, dict) for item in (session, persona, cursor)) or not isinstance(events, list) or not isinstance(receipts, list):
        raise LiteTravelError("帰宅差分の構造が不正です。")
    session_id = str(session.get("travel_session_id") or "")
    room_name = _validate_room_name(str(persona.get("persona_id") or ""))
    if expected_room and room_name != expected_room:
        raise LiteTravelError("帰宅差分のペルソナ対応が一致しません。")
    after = cursor.get("after_sequence")
    through = cursor.get("through_sequence")
    if not session_id or not isinstance(after, int) or not isinstance(through, int) or after < 0 or through < after:
        raise LiteTravelError("帰宅差分のcursorが不正です。")
    expected_sequences = list(range(after + 1, through + 1))
    sequences: list[int] = []
    event_ids: set[str] = set()
    assistant_ids: set[str] = set()
    for event in events:
        if not isinstance(event, dict) or event.get("persona_id") != room_name or event.get("branch_id") != "travel":
            raise LiteTravelError("帰宅差分のevent対応が不正です。")
        sequence = event.get("sequence_no")
        event_id = str(event.get("event_id") or "")
        content = event.get("content")
        if not isinstance(sequence, int) or not event_id or event_id in event_ids or not isinstance(content, str):
            raise LiteTravelError("帰宅差分のevent契約が不正です。")
        expected_hash = __import__("base64").urlsafe_b64encode(hashlib.sha256(content.encode("utf-8")).digest()).decode("ascii").rstrip("=")
        if not hmac.compare_digest(expected_hash, str(event.get("content_hash") or "")):
            raise LiteTravelError("帰宅差分のevent本文hashが一致しません。")
        if event.get("type") not in {"user_message", "assistant_message", "route_changed"} or event.get("status") != "committed":
            raise LiteTravelError("帰宅差分のevent種別が不正です。")
        sequences.append(sequence)
        event_ids.add(event_id)
        if event.get("type") == "assistant_message":
            assistant_ids.add(event_id)
    if sequences != expected_sequences:
        raise LiteTravelError("帰宅差分のイベント順序に欠番があります。")
    receipt_events: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("persona_id") != room_name:
            raise LiteTravelError("帰宅差分のreceipt対応が不正です。")
        event_id = str(receipt.get("event_id") or "")
        if not receipt.get("receipt_id") or event_id not in assistant_ids or event_id in receipt_events:
            raise LiteTravelError("帰宅差分のreceipt契約が不正です。")
        receipt_events.add(event_id)
    if receipt_events != assistant_ids:
        raise LiteTravelError("帰宅差分のAI応答とreceiptが一致しません。")
    return session_id, room_name, after, through


def _import_v4_persona_payload(payload: Dict[str, Any], payload_hash: str) -> Dict[str, Any]:
    session_id, room_name, after, through = _validate_v4_persona_payload(payload)
    state = presence_status(room_name)
    if not state or str(state.get("travel_session_id") or "") != session_id:
        raise LiteTravelError("本体の存在状態と帰宅差分のセッションが一致しません。")
    operation_lock = _metadata_root() / "imports" / f"{_room_key(room_name)}.operation"
    with file_lock_utils.locked_file(operation_lock.as_posix()):
        import_state = _read_json(_import_state_path(room_name), {
            "travel_session_id": session_id, "imported_event_ids": [], "imported_receipt_ids": [],
            "bundle_hashes": [], "last_sequence": 0,
        })
        if str(import_state.get("travel_session_id") or "") != session_id:
            # セッション切替時も、未整理／整理済みmarkerと既存の未知metadataを
            # 保持する。既存stateを先に土台へコピーしてから既知のcursorだけ更新する。
            reset_state = dict(import_state)
            reset_state.update({
                "travel_session_id": session_id, "imported_event_ids": [], "imported_receipt_ids": [],
                "bundle_hashes": [], "last_sequence": 0,
            })
            import_state = carry_return_pending(import_state, reset_state)
        last_sequence = int(import_state.get("last_sequence") or 0)
        if after != last_sequence and through > last_sequence:
            raise LiteTravelError("本体の帰宅cursorとWorker差分が一致しません。")
        recovery_path = _metadata_root() / "recovery_bundles" / f"{session_id}_{_room_key(room_name)}_{payload_hash[:16]}.json"
        _write_json(recovery_path, payload)
        ledger_result = usage_ledger.import_travel_receipts(payload["receipts"], session_id, room_name)
        event_ids = [str(event["event_id"]) for event in payload["events"]]
        imported_ids = set(import_state.get("imported_event_ids") or [])
        log_path, *_ = room_manager.get_room_files_paths(room_name)
        marker = f"<!-- Lite Travel Branch: {session_id}:{room_name}:travel:{through}:{payload_hash} -->"
        new_events: list[Dict[str, Any]] = []
        if payload["events"] and not log_path:
            raise LiteTravelError("対象ルームの会話ログを取得できません。")
        if log_path:
            target_log = Path(utils._resolve_monthly_log_file(log_path))
            with file_lock_utils.locked_file(target_log.as_posix()):
                existing = target_log.read_text(encoding="utf-8") if target_log.exists() else ""
                imported_ids.update(
                    _logged_event_ids(existing, session_id, room_name, payload["events"])
                )
                new_events = [
                    event for event in payload["events"]
                    if str(event["event_id"]) not in imported_ids
                ]
                if new_events:
                    room_manager.create_backup(room_name, "log")
                    chunk_payload = {
                        "session": {"travel_session_id": session_id},
                        "events": new_events,
                    }
                    target_log.parent.mkdir(parents=True, exist_ok=True)
                    with target_log.open("a", encoding="utf-8") as stream:
                        if existing and not existing.endswith("\n\n"):
                            stream.write("\n\n")
                        mode = str(payload.get("persona", {}).get("presence_mode") or "exclusive")
                        if (
                            mode == "parallel"
                            or state.get("status") == "emergency_reclaimed"
                            or bool(payload.get("persona", {}).get("branch_divergence_possible"))
                            or payload.get("session", {}).get("activation_mode") == "recovery_unconfirmed"
                        ):
                            stream.write(f"{marker}\n## SYSTEM:外出\n並行存在または緊急帰還後のtravel分岐を独立区間として統合します。\n\n")
                        else:
                            stream.write(marker + "\n")
                        stream.write(_format_import_log(chunk_payload, payload_hash, room_name))
            if new_events:
                utils.invalidate_chat_log_cache(log_path)
                utils.invalidate_chat_log_cache(target_log.as_posix())
        imported_ids.update(event_ids)
        hashes = list(import_state.get("bundle_hashes") or [])
        if payload_hash not in hashes:
            hashes.append(payload_hash)
        try:
            import_state = merge_return_pending(import_state, payload["events"], session_id, room_name)
        except ValueError as exc:
            raise LiteTravelError("帰宅会話の記憶整理待ち状態を保存できません。") from exc
        updated_state = dict(import_state)
        updated_state.update({
            "schema_version": SCHEMA_VERSION, "room_name": room_name, "travel_session_id": session_id,
            "imported_event_ids": sorted(imported_ids),
            "imported_receipt_ids": sorted(set(import_state.get("imported_receipt_ids") or []) | {
                str(receipt["receipt_id"]) for receipt in payload["receipts"]
            }),
            "bundle_hashes": hashes, "last_sequence": max(last_sequence, through), "updated_at": _now_iso(),
        })
        _write_json(_import_state_path(room_name), updated_state)
    return {
        "travel_session_id": session_id, "persona_id": room_name, "through_sequence": through,
        "payload_hash": payload_hash, "imported_event_count": len(new_events),
        "imported_receipt_count": int(ledger_result["imported"]), "recovery_bundle": recovery_path.as_posix(),
        "final_route": dict(payload.get("persona", {}).get("final_route") or {}),
    }


def online_return(travel_session_id: str) -> Dict[str, Any]:
    """段階journalを使い、署名付きpersona差分の取込・ACK・closeを再開する。"""
    travel_session_id = str(travel_session_id or "").strip()
    if not travel_session_id:
        raise LiteTravelError("オンライン帰宅できるセッションがありません。")
    key = str(get_settings().get("bundle_signing_key") or "")
    if not key:
        raise LiteTravelError("帰宅bundle署名鍵が未設定です。")
    operation_path = _return_operation_path(travel_session_id)
    operation_lock = operation_path.with_suffix(".operation")
    with file_lock_utils.locked_file(operation_lock.as_posix()):
        existed = operation_path.exists()
        operation = _read_json(operation_path, None)
        if not isinstance(operation, dict) or operation.get("travel_session_id") != travel_session_id:
            operation = _new_return_operation(travel_session_id)
            _save_return_operation(operation)
            existed = False
        if operation.get("status") == "completed":
            persona_journal = operation.get("personas")
            if isinstance(persona_journal, dict):
                for room_name, journal in persona_journal.items():
                    if isinstance(journal, dict):
                        _restore_completed_return_pending(
                            travel_session_id, str(room_name), journal
                        )
            journal_values = persona_journal.values() if isinstance(persona_journal, dict) else []
            stored_results = [
                dict(item.get("result") or {})
                for item in journal_values
                if isinstance(item, dict) and isinstance(item.get("result"), dict)
            ]
            return {
                "travel_session_id": travel_session_id,
                "personas": stored_results,
                "closed": True,
                "route_proposals": operation.get("route_proposals") or [],
                "operation_id": operation.get("operation_id"),
                "operation_status": "completed",
                "resumed": True,
                "completed_steps": list(operation.get("completed_steps") or []),
            }
        try:
            _require_worker_compatibility("オンライン帰宅", resumable_return=True)
            operation["status"] = "running"
            operation.pop("failure_stage", None)
            operation.pop("last_error", None)
            _complete_return_step(operation, "compatibility")
            _save_return_operation(operation)

            known_personas = [
                str(value) for value in operation.get("manifest_persona_ids") or [] if str(value)
            ]
            persona_journal = operation.setdefault("personas", {})
            all_known_acknowledged = bool(known_personas) and all(
                isinstance(persona_journal.get(room_name), dict)
                and persona_journal[room_name].get("acknowledged")
                for room_name in known_personas
            )
            if operation.get("close_completed") or all_known_acknowledged:
                results = [
                    dict(persona_journal[room_name].get("result") or {})
                    for room_name in known_personas
                ]
                for room_name in known_personas:
                    journal = persona_journal[room_name]
                    if journal.get("local_closed"):
                        continue
                    current_presence = presence_status(room_name)
                    _require_matching_presence_session(current_presence, travel_session_id)
                    presence_mode = str(
                        (current_presence or {}).get("presence_mode")
                        or journal.get("presence_mode")
                        or "exclusive"
                    )
                    activation_mode = str(
                        (current_presence or {}).get("activation_mode")
                        or journal.get("activation_mode")
                        or "planned"
                    )
                    divergence = bool(
                        (current_presence or {}).get("branch_divergence_possible")
                        or journal.get("branch_divergence_possible")
                    )
                    if not current_presence:
                        details = {
                            "presence_mode": presence_mode,
                            "activation_mode": activation_mode,
                            "branch_divergence_possible": divergence,
                        }
                        set_presence_state(room_name, "armed", travel_session_id, **details)
                        set_presence_state(room_name, "active", travel_session_id, **details)
                        current_presence = presence_status(room_name)
                    if current_presence and current_presence.get("status") == "active":
                        set_presence_state(
                            room_name, "returning", travel_session_id,
                            presence_mode=presence_mode,
                            activation_mode=activation_mode,
                            branch_divergence_possible=divergence,
                        )
                        current_presence = presence_status(room_name)
                    if current_presence and current_presence.get("status") != "closed":
                        set_presence_state(
                            room_name, "closed", travel_session_id,
                            presence_mode=presence_mode,
                            activation_mode=activation_mode,
                            branch_divergence_possible=divergence,
                            imported_event_count=int(
                                (journal.get("result") or {}).get("imported_event_count") or 0
                            ),
                        )
                    journal["local_closed"] = True
                    _save_return_operation(operation)
                if not operation.get("close_completed"):
                    _owner_request("POST", f"/v1/travel-sessions/{travel_session_id}/close")
                    operation["close_completed"] = True
                    operation["closed_at"] = _now_iso()
                    _complete_return_step(operation, "close")
                    _save_return_operation(operation)
                proposals = save_route_proposals(results)
                operation["route_proposals"] = proposals
                _complete_return_step(operation, "route_proposals")
                operation["status"] = "completed"
                operation["completed_at"] = _now_iso()
                _save_return_operation(operation)
                return {
                    "travel_session_id": travel_session_id,
                    "personas": results,
                    "closed": True,
                    "route_proposals": proposals,
                    "operation_id": operation.get("operation_id"),
                    "operation_status": "completed",
                    "resumed": True,
                    "completed_steps": list(operation.get("completed_steps") or []),
                }

            manifest = _owner_request("POST", f"/v1/travel-sessions/{travel_session_id}/return/start")
            activation_mode = str((manifest.get("session") or {}).get("activation_mode") or "planned")
            operation["return_started_at"] = operation.get("return_started_at") or _now_iso()
            operation["manifest_persona_ids"] = [
                str(item.get("persona_id") or "")
                for item in manifest.get("personas") or []
                if isinstance(item, dict)
            ]
            _complete_return_step(operation, "return_started")
            _save_return_operation(operation)

            results = []
            for persona in manifest.get("personas") or []:
                room_name = _validate_room_name(str(persona.get("persona_id") or ""))
                journal = persona_journal.setdefault(room_name, {"persona_id": room_name})
                journal.setdefault("presence_mode", str(persona.get("presence_mode") or "exclusive"))
                journal.setdefault("activation_mode", activation_mode)
                journal.setdefault(
                    "branch_divergence_possible",
                    bool(persona.get("branch_divergence_possible")),
                )
                local_presence = presence_status(room_name)
                _require_matching_presence_session(local_presence, travel_session_id)
                recoverable_presence = not local_presence or local_presence.get("status") in {
                    "closed", "emergency_reclaimed"
                }
                if recoverable_presence and not journal.get("imported"):
                    presence_mode = str(persona.get("presence_mode") or "exclusive")
                    anchor_changed = True
                    try:
                        current_anchor = _home_anchor(room_name, _now_iso())["log_tail_hash"]
                        anchor_changed = current_anchor != str(persona.get("home_anchor_hash") or "")
                    except Exception:
                        pass
                    divergence = bool(
                        activation_mode == "recovery_unconfirmed"
                        or persona.get("branch_divergence_possible")
                        or anchor_changed
                        or (local_presence or {}).get("status") == "emergency_reclaimed"
                    )
                    details = {
                        "presence_mode": presence_mode,
                        "activation_mode": activation_mode,
                        "branch_divergence_possible": divergence,
                    }
                    set_presence_state(room_name, "armed", travel_session_id, **details)
                    set_presence_state(room_name, "active", travel_session_id, **details)
                    local_presence = presence_status(room_name)
                if local_presence and local_presence.get("status") == "active":
                    set_presence_state(
                        room_name, "returning", travel_session_id,
                        presence_mode=str(local_presence.get("presence_mode") or "exclusive"),
                        activation_mode=activation_mode,
                        branch_divergence_possible=bool(
                            local_presence.get("branch_divergence_possible")
                            or persona.get("branch_divergence_possible")
                        ),
                    )

                result = dict(journal.get("result") or {}) if journal.get("imported") else {}
                if not result:
                    import_state = _read_json(_import_state_path(room_name), {"last_sequence": 0})
                    after = int(import_state.get("last_sequence") or 0) if str(
                        import_state.get("travel_session_id") or ""
                    ) == travel_session_id else 0
                    chunk = _owner_request(
                        "GET",
                        f"/v1/travel-sessions/{travel_session_id}/return/chunks?persona_id={quote(room_name, safe='')}&after_sequence={after}",
                    )
                    payload, payload_hash = _verify_v4_signed(chunk, key)
                    _validate_v4_persona_payload(payload, room_name)
                    result = _import_v4_persona_payload(payload, payload_hash)
                    journal.update(
                        imported=True,
                        imported_at=_now_iso(),
                        result=_return_journal_result(result),
                    )
                    _complete_return_step(operation, f"persona:{room_name}:import")
                    _save_return_operation(operation)

                if not journal.get("acknowledged"):
                    ack_request = journal.get("ack_request")
                    if not isinstance(ack_request, dict):
                        ack_request = {
                            "ack_id": str(uuid.uuid4()).replace("-", "_"),
                            "persona_id": room_name,
                            "through_sequence": int(result["through_sequence"]),
                            "payload_hash": str(result["payload_hash"]),
                        }
                        journal["ack_request"] = ack_request
                        _save_return_operation(operation)
                    ack = _owner_request(
                        "POST",
                        f"/v1/travel-sessions/{travel_session_id}/return/ack",
                        json_body=ack_request,
                    )
                    journal.update(acknowledged=True, acknowledged_at=_now_iso(), ack=ack)
                    _complete_return_step(operation, f"persona:{room_name}:ack")
                    _save_return_operation(operation)
                ack = journal.get("ack") or {"acknowledged": True, "resumed_from_journal": True}

                current_presence = presence_status(room_name) or {}
                if current_presence.get("status") != "closed":
                    set_presence_state(
                        room_name, "closed", travel_session_id,
                        presence_mode=str(current_presence.get("presence_mode") or "exclusive"),
                        activation_mode=str(current_presence.get("activation_mode") or activation_mode),
                        branch_divergence_possible=bool(
                            current_presence.get("branch_divergence_possible")
                            or persona.get("branch_divergence_possible")
                        ),
                        imported_event_count=int(result.get("imported_event_count") or 0),
                    )
                journal["local_closed"] = True
                journal["result"] = _return_journal_result({**result, "ack": ack})
                _save_return_operation(operation)
                results.append(dict(journal["result"]))

            if not operation.get("close_completed"):
                _owner_request("POST", f"/v1/travel-sessions/{travel_session_id}/close")
                operation["close_completed"] = True
                operation["closed_at"] = _now_iso()
                _complete_return_step(operation, "close")
                _save_return_operation(operation)
            proposals = save_route_proposals(results)
            operation["route_proposals"] = proposals
            _complete_return_step(operation, "route_proposals")
            operation["status"] = "completed"
            operation["completed_at"] = _now_iso()
            _save_return_operation(operation)
            return {
                "travel_session_id": travel_session_id,
                "personas": results,
                "closed": True,
                "route_proposals": proposals,
                "operation_id": operation.get("operation_id"),
                "operation_status": "completed",
                "resumed": existed,
                "completed_steps": list(operation.get("completed_steps") or []),
            }
        except Exception as exc:
            operation["status"] = "blocked" if isinstance(exc, LiteTravelError) else "failed"
            operation["failure_stage"] = str((operation.get("completed_steps") or ["start"])[-1])
            operation["last_error"] = str(exc)[:1000]
            _save_return_operation(operation)
            raise


def _validate_bundle_route(value: Any, *, expected_epoch: Optional[int] = None) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise LiteTravelError("帰宅bundleの経路情報が不正です。")
    if set(value) != {"credential_profile_id", "provider", "model_id", "route_epoch"}:
        raise LiteTravelError("帰宅bundleの経路情報が不正です。")
    profile_id = str(value.get("credential_profile_id") or "")
    provider = str(value.get("provider") or "")
    model_id = str(value.get("model_id") or "")
    route_epoch = value.get("route_epoch")
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,99}", profile_id)
        or provider not in VALID_PROVIDERS
        or not model_id
        or len(model_id) > 200
        or not isinstance(route_epoch, int)
        or route_epoch < 0
        or (expected_epoch is not None and route_epoch != expected_epoch)
    ):
        raise LiteTravelError("帰宅bundleの経路情報が不正です。")
    return {
        "credential_profile_id": profile_id,
        "provider": provider,
        "model_id": model_id,
        "route_epoch": route_epoch,
    }


def _verify_bundle(bundle: Dict[str, Any], room_name: str, signing_key: str) -> tuple[Dict[str, Any], str]:
    if bundle.get("algorithm") != "HMAC-SHA-256":
        raise LiteTravelError("帰宅bundleの署名方式が不正です。")
    canonical = bundle.get("payload_canonical")
    if not isinstance(canonical, str) or len(canonical) > 5_000_000:
        raise LiteTravelError("帰宅bundle本文が不正です。")
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).digest()
    expected_hash = __import__("base64").urlsafe_b64encode(payload_hash).decode("ascii").rstrip("=")
    if not hmac.compare_digest(expected_hash, str(bundle.get("payload_hash") or "")):
        raise LiteTravelError("帰宅bundleのhashが一致しません。")
    expected_signature = hmac.new(signing_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest()
    expected_signature_text = __import__("base64").urlsafe_b64encode(expected_signature).decode("ascii").rstrip("=")
    if not hmac.compare_digest(expected_signature_text, str(bundle.get("signature") or "")):
        raise LiteTravelError("帰宅bundleの署名を検証できません。")
    try:
        payload = json.loads(canonical)
    except json.JSONDecodeError as exc:
        raise LiteTravelError("帰宅bundle本文を解析できません。") from exc
    bundle_schema = payload.get("schema_version")
    if payload != bundle.get("payload") or bundle_schema not in {1, 2, 3}:
        raise LiteTravelError("帰宅bundleのpayload対応が不正です。")
    session = payload.get("session") or {}
    if session.get("persona_id") != room_name or not session.get("travel_session_id"):
        raise LiteTravelError("帰宅bundleのペルソナ対応が一致しません。")
    if bundle_schema == 3:
        budget = session.get("budget")
        if not isinstance(budget, dict) or set(budget) != {
            "daily_limit_usd", "session_limit_usd", "warning_ratio", "allow_unknown_price",
            "max_output_tokens", "timezone",
        }:
            raise LiteTravelError("帰宅bundleの予算契約が不正です。")
        limits = (budget.get("daily_limit_usd"), budget.get("session_limit_usd"))
        if any(value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0) for value in limits):
            raise LiteTravelError("帰宅bundleの予算契約が不正です。")
        if (
            not isinstance(budget.get("warning_ratio"), (int, float))
            or not 0 < budget["warning_ratio"] <= 1
            or not isinstance(budget.get("allow_unknown_price"), bool)
            or (
                budget.get("max_output_tokens") is not None
                and (not isinstance(budget.get("max_output_tokens"), int)
                     or not 1 <= budget["max_output_tokens"] <= MAX_MANUAL_OUTPUT_TOKENS)
            )
            or not isinstance(budget.get("timezone"), str)
            or not isinstance(session.get("usage_summary"), dict)
        ):
            raise LiteTravelError("帰宅bundleの予算契約が不正です。")
    events = payload.get("events")
    if not isinstance(events, list):
        raise LiteTravelError("帰宅bundleのイベント一覧が不正です。")
    event_ids: set[str] = set()
    assistant_event_ids: set[str] = set()
    assistant_events: Dict[str, Dict[str, Any]] = {}
    sequences: list[int] = []
    current_route: Optional[Dict[str, Any]] = None
    routes_by_epoch: Dict[int, Dict[str, Any]] = {}
    if bundle_schema >= 2:
        current_route = _validate_bundle_route(session.get("initial_route"), expected_epoch=0)
        routes_by_epoch[0] = current_route
        final_route = _validate_bundle_route(session.get("final_route"))
    for event in events:
        if not isinstance(event, dict):
            raise LiteTravelError("帰宅bundleに不正なイベントがあります。")
        event_id = str(event.get("event_id") or "")
        sequence = event.get("sequence_no")
        if (
            not event_id
            or event_id in event_ids
            or not isinstance(sequence, int)
            or event.get("persona_id") != room_name
            or event.get("type") not in (
                {"user_message", "assistant_message"} if bundle_schema == 1
                else {"user_message", "assistant_message", "route_changed"}
            )
            or not isinstance(event.get("content"), str)
            or event.get("status") != "committed"
        ):
            raise LiteTravelError("帰宅bundleのイベント契約が不正です。")
        content_hash = hashlib.sha256(event["content"].encode("utf-8")).digest()
        content_hash_text = __import__("base64").urlsafe_b64encode(content_hash).decode("ascii").rstrip("=")
        if not hmac.compare_digest(content_hash_text, str(event.get("content_hash") or "")):
            raise LiteTravelError("帰宅bundleのイベント本文hashが一致しません。")
        event_ids.add(event_id)
        event_type = event.get("type")
        if bundle_schema >= 2:
            route_epoch = event.get("route_epoch")
            if not isinstance(route_epoch, int) or current_route is None:
                raise LiteTravelError("帰宅bundleのroute epochが不正です。")
            if event_type == "route_changed":
                try:
                    route_detail = json.loads(event["content"])
                except json.JSONDecodeError as exc:
                    raise LiteTravelError("帰宅bundleの経路変更内容が不正です。") from exc
                next_route = _validate_bundle_route(route_detail, expected_epoch=current_route["route_epoch"] + 1)
                if (
                    route_epoch != next_route["route_epoch"]
                    or event.get("provider") != next_route["provider"]
                    or event.get("model_requested") != next_route["model_id"]
                    or event.get("model_resolved") != next_route["model_id"]
                ):
                    raise LiteTravelError("帰宅bundleの経路変更対応が不正です。")
                current_route = next_route
                routes_by_epoch[route_epoch] = next_route
            elif route_epoch != current_route["route_epoch"]:
                raise LiteTravelError("帰宅bundleのroute epochが不正です。")
            elif event_type == "user_message":
                if any(event.get(key) is not None for key in ("provider", "model_requested", "model_resolved")):
                    raise LiteTravelError("帰宅bundleのユーザー発言経路が不正です。")
            elif (
                event.get("provider") != current_route["provider"]
                or event.get("model_requested") != current_route["model_id"]
                or not isinstance(event.get("model_resolved"), str)
                or not event.get("model_resolved")
            ):
                raise LiteTravelError("帰宅bundleのAI応答経路が不正です。")
        if event_type == "assistant_message":
            assistant_event_ids.add(event_id)
            assistant_events[event_id] = event
        sequences.append(sequence)
    if sequences != list(range(1, len(sequences) + 1)):
        raise LiteTravelError("帰宅bundleのイベント順序に欠番があります。")
    if bundle_schema >= 2 and current_route != final_route:
        raise LiteTravelError("帰宅bundleの最終経路が一致しません。")
    receipts = payload.get("receipts")
    if not isinstance(receipts, list):
        raise LiteTravelError("帰宅bundleの利用receipt一覧が不正です。")
    receipt_ids: set[str] = set()
    receipt_event_ids: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise LiteTravelError("帰宅bundleに不正な利用receiptがあります。")
        receipt_id = str(receipt.get("receipt_id") or "")
        receipt_event_id = str(receipt.get("event_id") or "")
        if (
            not receipt_id
            or receipt_id in receipt_ids
            or receipt_event_id not in assistant_event_ids
            or receipt_event_id in receipt_event_ids
            or receipt.get("persona_id") != room_name
        ):
            raise LiteTravelError("帰宅bundleの利用receipt契約が不正です。")
        if bundle_schema == 1:
            if receipt.get("provider") != "gemini":
                raise LiteTravelError("帰宅bundleの利用receipt契約が不正です。")
        else:
            assistant = assistant_events[receipt_event_id]
            route = routes_by_epoch.get(assistant.get("route_epoch"))
            provider = receipt.get("provider")
            gateway = receipt.get("gateway")
            if (
                route is None
                or provider != assistant.get("provider")
                or receipt.get("route_epoch") != assistant.get("route_epoch")
                or receipt.get("credential_profile_id") != route["credential_profile_id"]
                or receipt.get("model_requested") != assistant.get("model_requested")
                or receipt.get("model_resolved") != assistant.get("model_resolved")
                or (provider == "openrouter" and gateway != "openrouter")
                or (provider != "openrouter" and gateway is not None)
                or (provider != "openrouter" and receipt.get("upstream_provider") is not None)
            ):
                raise LiteTravelError("帰宅bundleの利用receipt経路が不正です。")
        if bundle_schema == 3:
            numeric_fields = (
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                "provider_reported_cost_usd", "input_cost_usd", "output_cost_usd",
                "cache_read_cost_usd", "cache_creation_cost_usd", "cache_storage_cost_usd",
                "estimated_cost_usd", "cache_ttl_seconds",
                "cache_creation_5m_tokens", "cache_creation_1h_tokens",
            )
            if any(
                receipt.get(field) is not None
                and (not isinstance(receipt.get(field), (int, float)) or not math.isfinite(receipt[field]) or receipt[field] < 0)
                for field in numeric_fields
            ):
                raise LiteTravelError("帰宅bundleの利用receipt金額またはtoken数が不正です。")
            savings = receipt.get("estimated_savings_usd")
            if savings is not None and (not isinstance(savings, (int, float)) or not math.isfinite(savings)):
                raise LiteTravelError("帰宅bundleの利用receipt節約額が不正です。")
            estimate_status = receipt.get("estimate_status")
            cost_basis = receipt.get("cost_basis")
            if (
                estimate_status not in {"estimated", "unknown_price", "missing_usage"}
                or cost_basis not in {"provider_reported", "catalog", "none"}
                or (estimate_status == "estimated") != (receipt.get("estimated_cost_usd") is not None)
                or not isinstance(receipt.get("cache_status"), str)
            ):
                raise LiteTravelError("帰宅bundleの利用receipt料金根拠が不正です。")
            if cost_basis == "catalog":
                line_items = [
                    receipt.get(field) for field in (
                        "input_cost_usd", "output_cost_usd", "cache_read_cost_usd",
                        "cache_creation_cost_usd", "cache_storage_cost_usd",
                    )
                ]
                if (
                    not isinstance(receipt.get("pricing_version"), str)
                    or not all(isinstance(value, (int, float)) for value in line_items)
                    or not math.isclose(sum(line_items), receipt["estimated_cost_usd"], rel_tol=1e-9, abs_tol=1e-12)
                ):
                    raise LiteTravelError("帰宅bundleの利用receipt料金内訳が一致しません。")
            elif cost_basis == "provider_reported":
                if (
                    receipt.get("provider_reported_cost_usd") is None
                    or not math.isclose(
                        receipt["provider_reported_cost_usd"], receipt["estimated_cost_usd"],
                        rel_tol=1e-9, abs_tol=1e-12,
                    )
                ):
                    raise LiteTravelError("帰宅bundleのprovider報告料金が一致しません。")
            elif receipt.get("estimated_cost_usd") is not None:
                raise LiteTravelError("帰宅bundleの金額不明receiptが既知額を含んでいます。")
        receipt_ids.add(receipt_id)
        receipt_event_ids.add(receipt_event_id)
    if receipt_event_ids != assistant_event_ids:
        raise LiteTravelError("帰宅bundleのAI応答と利用receiptが一致しません。")
    return payload, expected_hash


def _format_import_log(payload: Dict[str, Any], payload_hash: str, room_name: str) -> str:
    weekday_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    def event_footer(value: Any) -> Optional[str]:
        timestamp = parse_event_datetime(value)
        if timestamp is None:
            return None
        return (
            f"{timestamp:%Y-%m-%d} ({weekday_names[timestamp.weekday()]}) "
            f"{timestamp:%H:%M:%S}"
        )

    session_id = payload["session"]["travel_session_id"]
    lines = [
        f"<!-- Lite Travel Import: {payload_hash} -->",
        "## SYSTEM:外出",
        f"Lite独立お出かけセッション {session_id} の会話を統合しました。",
        "",
    ]
    for event in payload["events"]:
        event_marker = _event_import_marker(session_id, room_name, str(event["event_id"]))
        created_at = html.escape(str(event.get("created_at") or ""), quote=True)
        lines.extend([event_marker, f"<!-- Lite Travel Created At: {created_at} -->"])
        if event["type"] == "route_changed":
            route = json.loads(event["content"])
            provider_label = {
                "gemini": "Gemini", "openai": "OpenAI", "anthropic": "Anthropic",
                "xai": "xAI", "openrouter": "OpenRouter",
            }.get(route.get("provider"), str(route.get("provider") or "不明"))
            lines.extend([
                "## SYSTEM:外出",
                f"会話経路を {provider_label} / {route.get('model_id')} へ切り替えました（epoch {route.get('route_epoch')}）。",
                "",
            ])
            continue
        header = "## USER:user" if event["type"] == "user_message" else f"## AGENT:{room_name}"
        content = str(event["content"]).strip()
        footer = event_footer(event.get("created_at"))
        if footer is not None:
            content = f"{content}\n\n{footer}"
        lines.extend([header, content, ""])
    lines.extend(["## SYSTEM:外出", f"Lite独立お出かけセッション {session_id} の統合終了。", ""])
    return "\n".join(lines).rstrip() + "\n\n"


def import_return_bundle(bundle: Dict[str, Any], room_name: str, signing_key: Optional[str] = None) -> Dict[str, Any]:
    room_name = _validate_room_name(room_name)
    key = str(signing_key or get_settings().get("bundle_signing_key") or "")
    if not key:
        raise LiteTravelError("帰宅bundle署名鍵が未設定です。")
    if isinstance(bundle.get("payload"), dict) and bundle["payload"].get("schema_version") == 4:
        payload, _ = _verify_v4_signed(bundle, key)
        results = []
        for persona_payload in payload.get("personas") or []:
            if not isinstance(persona_payload, dict):
                raise LiteTravelError("帰宅bundle v4のペルソナ差分が不正です。")
            canonical = json.dumps(persona_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            persona_hash = __import__("base64").urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).decode("ascii").rstrip("=")
            results.append(_import_v4_persona_payload(persona_payload, persona_hash))
        return {
            "travel_session_id": str(payload.get("manifest", {}).get("session", {}).get("travel_session_id") or ""),
            "imported_event_count": sum(item["imported_event_count"] for item in results),
            "imported_receipt_count": sum(item["imported_receipt_count"] for item in results),
            "duplicate": all(item["imported_event_count"] == 0 for item in results), "personas": results,
        }
    payload, payload_hash = _verify_bundle(bundle, room_name, key)
    session_id = str(payload["session"]["travel_session_id"])
    state = presence_status(room_name)
    if not state or state.get("travel_session_id") != session_id:
        raise LiteTravelError("本体の存在ロックと帰宅bundleのセッションが一致しません。")
    if state.get("status") == "active":
        set_presence_state(room_name, "returning", session_id)

    event_ids = [str(event["event_id"]) for event in payload["events"]]
    operation_lock = _metadata_root() / "imports" / f"{_room_key(room_name)}.operation"
    with file_lock_utils.locked_file(operation_lock.as_posix()):
        import_state = _read_json(
            _import_state_path(room_name),
            {"imported_event_ids": [], "imported_receipt_ids": [], "bundle_hashes": []},
        )
        imported = set(import_state.get("imported_event_ids") or [])

        recovery_path = _metadata_root() / "recovery_bundles" / f"{session_id}_{payload_hash[:16]}.json"
        _write_json(recovery_path, bundle)
        ledger_result = {"imported": 0, "duplicate": 0}
        if payload.get("schema_version") == 3:
            try:
                ledger_result = usage_ledger.import_travel_receipts(payload["receipts"], session_id, room_name)
            except Exception as exc:
                raise LiteTravelError("帰宅bundleの料金台帳を保存できませんでした。") from exc
        log_path, *_ = room_manager.get_room_files_paths(room_name)
        if not log_path:
            raise LiteTravelError("対象ルームの会話ログを取得できません。")
        marker = f"<!-- Lite Travel Import: {payload_hash} -->"
        target_log = Path(utils._resolve_monthly_log_file(log_path))
        new_events: list[Dict[str, Any]] = []
        marker_exists = False
        with file_lock_utils.locked_file(target_log.as_posix()):
            existing = target_log.read_text(encoding="utf-8") if target_log.exists() else ""
            marker_exists = marker in existing
            if marker_exists:
                imported.update(event_ids)
            imported.update(_logged_event_ids(existing, session_id, room_name, payload["events"]))
            new_events = [
                event for event in payload["events"]
                if str(event["event_id"]) not in imported
            ]
            if new_events:
                room_manager.create_backup(room_name, "log")
                chunk_payload = dict(payload)
                chunk_payload["events"] = new_events
                if marker not in existing:
                    target_log.parent.mkdir(parents=True, exist_ok=True)
                    with target_log.open("a", encoding="utf-8") as stream:
                        if existing and not existing.endswith("\n\n"):
                            stream.write("\n\n")
                        stream.write(_format_import_log(chunk_payload, payload_hash, room_name))
        if new_events:
            utils.invalidate_chat_log_cache(log_path)
            utils.invalidate_chat_log_cache(target_log.as_posix())

        imported.update(event_ids)
        hashes = list(import_state.get("bundle_hashes") or [])
        if payload_hash not in hashes:
            hashes.append(payload_hash)
        try:
            import_state = merge_return_pending(import_state, payload["events"], session_id, room_name)
        except ValueError as exc:
            raise LiteTravelError("帰宅会話の記憶整理待ち状態を保存できません。") from exc
        updated_state = dict(import_state)
        updated_state.update({
            "schema_version": SCHEMA_VERSION,
            "room_name": room_name,
            "imported_event_ids": sorted(imported),
            "imported_receipt_ids": sorted(
                set(import_state.get("imported_receipt_ids") or [])
                | {str(receipt["receipt_id"]) for receipt in payload["receipts"]}
            ),
            "bundle_hashes": hashes,
            "updated_at": _now_iso(),
        })
        _write_json(_import_state_path(room_name), updated_state)
    set_presence_state(room_name, "closed", session_id, imported_event_count=len(event_ids))
    return {
        "travel_session_id": session_id,
        "imported_event_count": len(new_events),
        "duplicate": not new_events,
        "imported_receipt_count": int(ledger_result["imported"]),
        "recovery_bundle": recovery_path.as_posix(),
    }


def close_remote_session(travel_session_id: str) -> Dict[str, Any]:
    return _owner_request("POST", f"/v1/travel-sessions/{travel_session_id}/close")


def delete_remote_content(travel_session_id: str) -> Dict[str, Any]:
    return _owner_request("DELETE", f"/v1/travel-sessions/{travel_session_id}/content")


def acknowledge_file_return(result: Dict[str, Any]) -> Dict[str, Any]:
    """bundle v4のローカル確定結果をpersona別にackしてsessionを閉じる。"""
    session_id = str(result.get("travel_session_id") or "")
    personas = result.get("personas")
    if not session_id or not isinstance(personas, list):
        raise LiteTravelError("ファイル帰宅結果の対応が不正です。")
    acknowledgements = []
    for item in personas:
        ack = _owner_request("POST", f"/v1/travel-sessions/{session_id}/return/ack", json_body={
            "ack_id": str(uuid.uuid4()).replace("-", "_"),
            "persona_id": str(item.get("persona_id") or ""),
            "through_sequence": int(item.get("through_sequence") or 0),
            "payload_hash": str(item.get("payload_hash") or ""),
        })
        room_name = str(item.get("persona_id") or "")
        state = presence_status(room_name)
        if state and state.get("status") in {"active", "returning", "emergency_reclaimed"}:
            if state.get("status") == "active":
                set_presence_state(room_name, "returning", session_id, presence_mode=state.get("presence_mode", "exclusive"))
            set_presence_state(room_name, "closed", session_id, presence_mode=state.get("presence_mode", "exclusive"))
        acknowledgements.append(ack)
    _owner_request("POST", f"/v1/travel-sessions/{session_id}/close")
    return {"acknowledgements": acknowledgements, "closed": True}
