"""Gemini Developer API / Vertex AI の接続設定とクライアント境界。

このモジュールは、既存の ``provider="google"`` を置き換えずに、テキスト
LLM が使う接続先だけを切り替えるための小さな境界を提供する。設定の読込は
設定ファイルとメモリ上の辞書に限定し、認証情報の探索や SDK クライアントの
生成は ``create_client`` / ``build_chat_model`` の呼出し時だけ行う。
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import importlib
import json
import os
from typing import Any, AsyncIterator, Iterator, Self

import google.genai as genai
import google.auth
from google.auth import exceptions as auth_exceptions
from google.genai import errors as genai_errors
import httpx

import constants
import file_lock_utils
from gemini_model_features import uses_argon_request_rules
from pydantic import Field, model_validator
from langchain_google_genai import ChatGoogleGenerativeAI


CONNECTION_KEY = "gemini_connection"
_VERTEX_CREDENTIALS_KEY = "vertex_credentials"
_VERTEX_SAVED_KEYS_KEY = "saved_api_keys"
SHARED_VERTEX_KEY_REF = "shared"
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_DEFAULT_LOCATION = "global"
_ROOM_GOOGLE_PROVIDERS = {"google", "Google (Gemini)"}
_PROVIDER_CHOICES = frozenset({"google", "vertex", "openai", "anthropic", "local"})
_ROOM_PROVIDER_CHOICES = _PROVIDER_CHOICES | {"default"}


class GeminiConnectionError(RuntimeError):
    """接続設定、認証、または SDK クライアント生成の失敗。"""

    _vertex_http_status: int | None
    _vertex_api_status: str | None
    _vertex_error_type: str
    _vertex_initializing: bool


class ConnectionConflictError(GeminiConnectionError):
    """古い UI state が別の接続設定を上書きしようとした。"""


_VERTEX_ERROR_STATUSES = frozenset({
    "INVALID_ARGUMENT", "UNAUTHENTICATED", "PERMISSION_DENIED", "NOT_FOUND",
    "RESOURCE_EXHAUSTED", "FAILED_PRECONDITION", "ABORTED", "OUT_OF_RANGE",
    "UNIMPLEMENTED", "INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED",
    "CANCELLED", "UNKNOWN", "ALREADY_EXISTS",
})
_VERTEX_ERROR_TYPES = frozenset({
    "SDKError", "ClientError", "ServerError", "APIError", "HTTPStatusError",
    "TimeoutError", "ConnectionError", "AuthenticationError", "TypeError", "ValueError",
})


def _vertex_operation_error(exc: Exception, *, initializing: bool = False) -> GeminiConnectionError:
    """本文・URL・任意class名を使わず、構造化コードと固定の種別だけ公開する。"""

    current = exc
    seen = set()
    error_type = "SDKError"
    http_status = None
    api_status = None
    # LangChainがClientErrorを包む場合も原因まで辿る。循環と過深なchainを打ち切る。
    for _ in range(10):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        # チャット側で再表示するときも元のmessageを信用せず、閉じた診断値を
        # 再検証して再生成する。argsやcontextにある秘密の本文は読まない。
        if type(current) is GeminiConnectionError:
            data = vars(current)
            kind = data.get("_vertex_error_type")
            if type(kind) is str and kind in _VERTEX_ERROR_TYPES:
                error_type = kind
                code = data.get("_vertex_http_status")
                if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599:
                    http_status = int(code)
                status = data.get("_vertex_api_status")
                if type(status) is str and status in _VERTEX_ERROR_STATUSES:
                    api_status = status
                initializing = initializing or data.get("_vertex_initializing") is True
                break
        if isinstance(current, genai_errors.APIError):
            error_type = ("ClientError" if isinstance(current, genai_errors.ClientError) else
                          "ServerError" if isinstance(current, genai_errors.ServerError) else "APIError")
            code = current.code
            if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599:
                http_status = int(code)
            status = current.status
            if type(status) is str and status in _VERTEX_ERROR_STATUSES:
                api_status = status
            break
        if isinstance(current, httpx.HTTPStatusError):
            code = current.response.status_code
            if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599:
                http_status = int(code)
            error_type = "HTTPStatusError"
            break
        if isinstance(current, (httpx.TimeoutException, TimeoutError)):
            error_type = "TimeoutError"
            break
        if isinstance(current, (httpx.TransportError, ConnectionError)):
            error_type = "ConnectionError"
            break
        if isinstance(current, auth_exceptions.GoogleAuthError):
            error_type = "AuthenticationError"
            break
        if error_type == "SDKError":
            if isinstance(current, TypeError):
                error_type = "TypeError"
            elif isinstance(current, ValueError):
                error_type = "ValueError"
        current = current.__cause__ or current.__context__

    hints = {
        400: "APIキー・モデル名・対応する入力形式・推論設定を確認してください。",
        401: "APIキーまたは認証情報を確認してください。",
        403: "APIの有効化・利用権限・請求設定を確認してください。",
        404: "モデルID・接続方式・リージョン・利用権限を確認してください。",
        429: "割当上限や混雑状況を確認し、時間を置いてください。",
    }
    hint = hints.get(http_status)
    if hint is None:
        if http_status is not None and http_status >= 500:
            hint = "接続先で一時的なエラーが発生しています。時間を置いてください。"
        elif error_type == "AuthenticationError":
            hint = "実行環境の認証情報と権限を確認してください。"
        elif error_type in {"TimeoutError", "ConnectionError"}:
            hint = "ネットワーク接続・タイムアウト・接続先の稼働状況を確認してください。"
        elif error_type in {"TypeError", "ValueError"}:
            hint = "SDKの送信パラメータと対応する入力形式を確認してください。"
        else:
            hint = "接続設定・認証・モデルの対応状況とSDKの互換性を確認してください。"
    details = ([f"HTTP {http_status}"] if http_status is not None else [])
    if api_status is not None:
        details.append(api_status)
    details.append(error_type)
    operation = "SDKクライアントの初期化" if initializing else "呼出し"
    error = GeminiConnectionError(
        f"Vertex Gemini の{operation}に失敗しました（{' / '.join(details)}）。{hint}"
    )
    error._vertex_http_status = http_status
    error._vertex_api_status = api_status
    error._vertex_error_type = error_type
    error._vertex_initializing = initializing
    return error


def vertex_error_message(exc: Exception) -> str:
    """ログ／チャット向けに閉じた診断値だけから本文を再生成する。"""

    return str(_vertex_operation_error(exc))


_DEFAULT_CONNECTION = {
    "backend": "developer",
    # Developer API の実キーは旧 gemini_api_keys から取得するため、ここは
    # 接続辞書上の認証方式を持たないことを示す互換値として adc を使う。
    "auth_mode": "adc",
    "project": "",
    "location": _DEFAULT_LOCATION,
    "api_key": "",
}


def _as_text(value: Any, default: str) -> Any:
    """正しい文字列だけを軽く正規化し、不正値は検証用に残す。"""

    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else default
    return value


def _validation_error(message: str) -> GeminiConnectionError:
    return GeminiConnectionError(f"Gemini接続設定が不正です: {message}")


def normalize_connection(value: Any, *, require_valid: bool = False) -> dict[str, Any]:
    """接続設定を既定の5フィールドへそろえる。

    ``require_valid=False`` では保存済みの不正な Vertex 設定も ``backend`` を
    保持して返す。これにより、壊れた Vertex 設定が Developer へ静かに戻って
    認証先を変えることを防ぐ。実際に SDK を作る経路では ``require_valid=True``
    が使われる。
    """

    if value is None:
        normalized = deepcopy(_DEFAULT_CONNECTION)
    elif not isinstance(value, Mapping):
        # 未登録（None）だけを Developer の既定値にする。登録済みの非辞書値は
        # 認証時に明示的に失敗させるため、backend を不正値として保持する。
        normalized = {
            "backend": value,
            "auth_mode": "api_key",
            "project": "",
            "location": _DEFAULT_LOCATION,
            "api_key": "",
        }
    elif not value:
        # 空の保存値は、まだ接続設定を登録していない旧設定として扱う。
        normalized = deepcopy(_DEFAULT_CONNECTION)
    else:
        # backendを省略した非空の辞書は未登録Developerとはみなさない。
        # project/api_keyだけを残した壊れたVertex値がDeveloperへ戻ると、
        # 認証先を意図せず切り替えるため、明示的な不正値として保持する。
        backend = value.get("backend", "invalid")
        if backend == "developer":
            # Developer API のキーは従来の gemini_api_keys から解決する。
            # 新しい辞書に秘密をコピーしない。
            normalized = {
                "backend": "developer",
                # Developer API は旧 gemini_api_keys の台帳だけを使う。
                # auth_mode は接続辞書の互換マーカーとして adc に固定する。
                "auth_mode": "adc",
                "project": "",
                "location": _DEFAULT_LOCATION,
                "api_key": "",
            }
        elif backend == "vertex":
            auth_mode = _as_text(value.get("auth_mode"), "adc")
            normalized = {
                "backend": "vertex",
                "auth_mode": auth_mode,
                "project": _as_text(value.get("project"), ""),
                "location": (
                    _DEFAULT_LOCATION
                    if auth_mode == "api_key" and not _as_text(value.get("project"), "")
                    else _as_text(value.get("location"), _DEFAULT_LOCATION)
                ),
                "api_key": _as_text(value.get("api_key"), ""),
            }
        else:
            # 不明な backend も保持する。require_valid でエラーにし、
            # get_connection が Developer へフォールバックしないようにする。
            normalized = {
                "backend": backend,
                "auth_mode": _as_text(value.get("auth_mode"), "api_key"),
                "project": _as_text(value.get("project"), ""),
                "location": _as_text(value.get("location"), _DEFAULT_LOCATION),
                "api_key": _as_text(value.get("api_key"), ""),
            }

    if require_valid:
        _validate_connection(normalized)
    return normalized


def _validate_connection(connection: Mapping[str, Any]) -> None:
    backend = connection.get("backend")
    auth_mode = connection.get("auth_mode")
    if backend not in {"developer", "vertex"}:
        raise _validation_error("backend は developer または vertex で指定してください")
    if auth_mode not in {"api_key", "adc"}:
        raise _validation_error("auth_mode は adc または api_key で指定してください")

    for field in ("project", "location", "api_key"):
        value = connection.get(field)
        if not isinstance(value, str):
            raise _validation_error(f"{field} は文字列で指定してください")

    if backend == "developer":
        if auth_mode not in {"adc", "api_key"}:
            raise _validation_error("Developer API の auth_mode が不正です")
        return

    if auth_mode == "adc":
        if not connection.get("project"):
            raise _validation_error("Vertex ADC には project が必要です")
        if not connection.get("location"):
            raise _validation_error("Vertex ADC には location が必要です")
        if connection.get("api_key"):
            raise _validation_error("Vertex ADC に API キーは指定できません")
    elif not connection.get("api_key"):
        raise _validation_error("Vertex API key には api_key が必要です")


def _config_manager():
    # import 時に設定ファイルを読み込まない。既存アプリの起動順と、単独 import
    # するテストの両方を保つため、依存モジュールは API 呼出し時に解決する。
    return importlib.import_module("config_manager")


def _room_manager():
    return importlib.import_module("room_manager")


def _global_raw_connection() -> tuple[Any, bool]:
    config_manager = _config_manager()
    # 実ファイルが存在する通常運用では、別プロセス／別画面の保存を検知する
    # ため、メモリ上のCONFIG_GLOBALよりディスクの最新値を正本にする。
    # config_manager.load_config_file() は破損時にバックアップ復元を行うため、
    # 接続読取では使わず、候補ファイルを直接読む。
    config_path = getattr(constants, "CONFIG_FILE", None)
    candidates: list[str] = []
    if config_path:
        configured_path = os.fspath(config_path)
        candidates.append(configured_path)
        if not os.path.isabs(configured_path):
            candidates.append(os.path.join("..", configured_path))
    for candidate in candidates:
        try:
            os.stat(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            raise GeminiConnectionError(
                "Gemini接続設定ファイルを読み取れないため、安全のため接続を停止しました。"
            ) from None
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                content = handle.read()
            if not content.strip():
                raise ValueError("empty")
            with_value = json.loads(content)
        except Exception:
            raise GeminiConnectionError(
                "Gemini接続設定ファイルを読み取れないため、安全のため接続を停止しました。"
            ) from None
        if not isinstance(with_value, Mapping):
            raise GeminiConnectionError(
                "Gemini接続設定ファイルの形式が不正なため、安全のため接続を停止しました。"
            )
        if CONNECTION_KEY in with_value:
            return with_value.get(CONNECTION_KEY), True
        # diskからキーが削除された場合もcacheを再利用しない。
        return None, False

    config = getattr(config_manager, "CONFIG_GLOBAL", {})
    if isinstance(config, Mapping) and CONNECTION_KEY in config:
        return config.get(CONNECTION_KEY), True
    return None, False


def _global_active_provider() -> Any:
    """共通providerの保存値だけを読み取る（接続秘密は返さない）。"""

    config_manager = _config_manager()
    config_path = getattr(constants, "CONFIG_FILE", None)
    candidates: list[str] = []
    if config_path:
        configured_path = os.fspath(config_path)
        candidates.append(configured_path)
        if not os.path.isabs(configured_path):
            candidates.append(os.path.join("..", configured_path))
    for candidate in candidates:
        try:
            os.stat(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            raise GeminiConnectionError(
                "共通provider設定を読み取れないため、安全のため選択を停止しました。"
            ) from None
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                content = handle.read()
            if not content.strip():
                raise ValueError("empty")
            config = json.loads(content)
        except Exception:
            raise GeminiConnectionError(
                "共通provider設定を読み取れないため、安全のため選択を停止しました。"
            ) from None
        if not isinstance(config, Mapping):
            raise GeminiConnectionError(
                "共通provider設定の形式が不正なため、安全のため選択を停止しました。"
            )
        return config.get("active_provider", "google")

    config = getattr(config_manager, "CONFIG_GLOBAL", {})
    if isinstance(config, Mapping):
        return config.get("active_provider", "google")
    return "google"


def _room_raw_connection(room_name: str) -> dict[str, Any]:
    """ルームの override と provider を、保存対象判定用にまとめて返す。"""

    result = {
        "present": False,
        "inherit": False,
        "provider": None,
        "raw": None,
    }
    if not room_name:
        return result

    # room_manager.get_room_config() は破損時にバックアップ復元を行うため、
    # 接続判定の正本には使わない。実ファイルがある場合は直接読むことで、
    # 破損した個別 Vertex 設定が共通 Developer へ静かに切り替わるのを防ぐ。
    room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
    try:
        os.stat(room_config_path)
    except FileNotFoundError:
        config = None
    except OSError:
        raise GeminiConnectionError(
            "Geminiルーム設定ファイルを読み取れないため、安全のため接続を停止しました。"
        ) from None
    else:
        try:
            with open(room_config_path, "r", encoding="utf-8") as handle:
                content = handle.read()
            if not content.strip():
                raise ValueError("empty")
            config = json.loads(content)
        except Exception:
            raise GeminiConnectionError(
                "Geminiルーム設定ファイルを読み取れないため、安全のため接続を停止しました。"
            ) from None
        if not isinstance(config, Mapping):
            raise GeminiConnectionError(
                "Geminiルーム設定の形式が不正なため、安全のため接続を停止しました。"
            )

    # FakeConfig／初期作成前など、ファイルがまだない場合だけ既存のメモリ
    # getterへ進む。getterの例外は従来どおり未登録として扱う。
    if config is None:
        try:
            config = _room_manager().get_room_config(room_name)
        except Exception:
            config = None
    if config is None:
        return result
    if not isinstance(config, Mapping):
        raise GeminiConnectionError(
            "Geminiルーム設定の形式が不正なため、安全のため接続を停止しました。"
        )
    overrides = config.get("override_settings", {})
    if not isinstance(overrides, Mapping):
        raise GeminiConnectionError(
            "Geminiルームのoverride_settings形式が不正なため、安全のため接続を停止しました。"
        )
    result["provider"] = overrides.get("provider")
    if CONNECTION_KEY not in overrides:
        return result
    result["present"] = True
    raw = overrides.get(CONNECTION_KEY)
    result["raw"] = raw
    result["inherit"] = isinstance(raw, Mapping) and raw.get("inherit") is True
    return result


def _effective_room_provider(provider: Any) -> bool:
    return provider in _ROOM_GOOGLE_PROVIDERS


def _provider_choice_for_category(provider: Any, *, fallback: str = "google") -> str:
    """旧provider名を公開UIの5カテゴリへ読み取り変換する。"""

    if provider in _ROOM_GOOGLE_PROVIDERS:
        return "google"
    if provider in {"openai", "openai_official", "zhipu", "groq", "ollama"}:
        return "openai"
    if provider in {"anthropic", "claude_subscription"}:
        return "anthropic"
    if provider == "local":
        return "local"
    return fallback


def _provider_choice_for_connection(raw: Any, *, fallback: str = "google") -> str:
    """接続辞書から秘密を返さず、GoogleのbackendだけをUI値へ変換する。"""

    if isinstance(raw, Mapping):
        backend = raw.get("backend")
        if backend == "vertex":
            return "vertex"
        if backend == "developer":
            return "google"
    return fallback


def provider_choice(room_name: str | None = None) -> str:
    """保存形式を変えずに、統合provider UIの現在値を返す。

    ``provider="google"`` は ``gemini_connection.backend`` と組み合わせて
    ``google``／``vertex`` を導出する。roomが未登録・継承中・``default`` の
    場合はroomでは ``default`` を返す。Googleを明示したroomが未登録／継承中
    の場合だけ共通接続のbackendを表示し、個別設定をDeveloperへ暗黙に戻さない。
    """

    global_provider = _global_active_provider()
    raw_global, registered = _global_raw_connection()
    global_category = _provider_choice_for_category(global_provider)
    global_google_choice = _provider_choice_for_connection(
        raw_global if registered else None,
        fallback="google",
    )
    global_choice = global_google_choice if global_category == "google" else global_category

    if not room_name:
        return global_choice

    room = _room_raw_connection(room_name)
    provider = room.get("provider")
    if provider in (None, "", "default"):
        return "default"
    category = _provider_choice_for_category(provider, fallback=global_google_choice)
    if category != "google":
        return category
    # 個別connectionがない／inherit中なら、共通接続の選択を表示する。
    if not room.get("present") or room.get("inherit"):
        return global_google_choice
    return _provider_choice_for_connection(room.get("raw"), fallback=global_google_choice)


def get_connection(room_name: str | None = None, *, force_global: bool = False) -> dict[str, Any]:
    """現在有効な接続を返す。設定読込はローカル辞書／ルームファイルだけ。"""

    raw_global, registered = _global_raw_connection()
    global_connection = _resolve_connection(raw_global if registered else None, raw_global)
    if force_global or not room_name:
        return global_connection

    room = _room_raw_connection(room_name)
    # default、非 Google、未指定の provider では room の接続ドラフトを使わない。
    if not _effective_room_provider(room["provider"]):
        return global_connection
    if not room["present"] or room["inherit"]:
        return global_connection
    return _resolve_connection(room["raw"], raw_global)


def is_vertex(
    room_name: str | None = None,
    *,
    connection: Mapping[str, Any] | None = None,
) -> bool:
    """指定接続または有効接続が Vertex backend か判定する。"""

    selected = normalize_connection(connection) if connection is not None else get_connection(room_name)
    return selected.get("backend") == "vertex"


def connection_id(connection: Mapping[str, Any] | Any) -> str:
    """秘密値を含まない安定した接続識別子を返す。"""

    normalized = normalize_connection(connection)
    if normalized.get("backend") == "developer":
        return "developer"

    api_key = normalized.get("api_key")
    if isinstance(api_key, str):
        key_digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    else:
        key_digest = hashlib.sha256(b"").hexdigest()
    # API keyは正規化辞書の値をそのまま識別子へ出さず、ハッシュ値で
    # 置換したcanonical JSON全体をさらにハッシュ化する。
    canonical = dict(normalized)
    canonical["api_key"] = key_digest
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    prefix = "vertex" if normalized.get("backend") == "vertex" else "invalid"
    return f"{prefix}:{digest}"


def _retained_vertex_credentials(value: Any) -> dict[str, Any]:
    """同じ保存先のVertex資格情報を読み、旧active形式も書換えず取り込む。"""

    raw = value if isinstance(value, Mapping) else {}
    stored = raw.get(_VERTEX_CREDENTIALS_KEY, {})
    if not isinstance(stored, Mapping):
        raise _validation_error("Vertexの保存済み資格情報が不正です")
    result = {"api_key": "", "project": "", "location": _DEFAULT_LOCATION, "auth_mode": "adc",
              "api_key_project": "", "api_key_location": _DEFAULT_LOCATION}
    for key in result:
        if key in stored:
            if not isinstance(stored[key], str):
                raise _validation_error("Vertexの保存済み資格情報が不正です")
            result[key] = stored[key].strip()
    if result["auth_mode"] not in {"adc", "api_key"}:
        raise _validation_error("Vertexの保存済み認証方式が不正です")
    if raw.get("backend") == "vertex":
        current = normalize_connection(raw)
        mode = current["auth_mode"]
        if mode in {"adc", "api_key"}:
            result["auth_mode"] = mode
            fields = ("api_key",) if mode == "api_key" else ("project", "location")
            for field in fields:
                if isinstance(current[field], str) and current[field]:
                    result[field] = current[field]
            if mode == "api_key":
                # ADCの保持値をExpressへ暗黙適用しない。空欄も明示的な経路選択。
                result["api_key_project"] = current["project"]
                result["api_key_location"] = current["location"]
    return result


def _key_reference(value: Any) -> str:
    if value == SHARED_VERTEX_KEY_REF:
        return value
    if (not isinstance(value, str) or not value.startswith("legacy-") or len(value) != 39
            or any(c not in "0123456789abcdef" for c in value[7:])):
        raise _validation_error("Vertex登録キーの参照が不正です")
    return value


def _vertex_key_registry(raw_global: Any) -> dict[str, dict[str, str]]:
    """共通台帳の秘密値を内部用に読む。UIにはchoicesと登録有無だけを返す。"""

    stored = _retained_vertex_credentials(raw_global)
    result = {SHARED_VERTEX_KEY_REF: {"label": "共通Vertexキー", "api_key": stored["api_key"]}}
    raw = raw_global if isinstance(raw_global, Mapping) else {}
    bank = raw.get(_VERTEX_CREDENTIALS_KEY, {})
    entries = bank.get(_VERTEX_SAVED_KEYS_KEY, {})
    if not isinstance(entries, Mapping):
        raise _validation_error("Vertex登録キーの台帳が不正です")
    for reference, entry in entries.items():
        _key_reference(reference)
        if (reference == SHARED_VERTEX_KEY_REF or not isinstance(entry, Mapping)
                or not isinstance(entry.get("label"), str)
                or not isinstance(entry.get("api_key"), str)
                or not entry["api_key"].strip()):
            raise _validation_error("Vertex登録キーの台帳が不正です")
        result[reference] = {"label": entry["label"], "api_key": entry["api_key"].strip()}
    return result


def _stored_key_reference(raw: Any) -> str | None:
    if not isinstance(raw, Mapping):
        return None
    bank = raw.get(_VERTEX_CREDENTIALS_KEY, {})
    if not isinstance(bank, Mapping):
        raise _validation_error("Vertexの保存済み資格情報が不正です")
    reference = bank.get("key_ref") if raw.get("inherit") is True else raw.get("key_ref", bank.get("key_ref"))
    return None if reference is None else _key_reference(reference)


def _selected_key_reference(raw: Any, room_name: str | None, raw_global: Any) -> str:
    reference = _stored_key_reference(raw)
    if reference is not None:
        return reference
    legacy_key = _retained_vertex_credentials(raw)["api_key"]
    if not room_name or not legacy_key:
        return SHARED_VERTEX_KEY_REF
    registry = _vertex_key_registry(raw_global)
    for key_ref, entry in registry.items():
        if entry["api_key"] == legacy_key:
            return key_ref
    identity = hashlib.sha256((room_name + "\0" + legacy_key).encode("utf-8")).hexdigest()[:32]
    return "legacy-" + identity


def _resolve_connection(raw: Any, raw_global: Any) -> dict[str, Any]:
    """保存用参照を解決し、SDKへ渡す従来の5フィールドだけを返す。"""

    selected = normalize_connection(raw)
    if selected["backend"] == "vertex" and selected["auth_mode"] == "api_key":
        reference = _stored_key_reference(raw)
        if reference is not None:
            # 参照先が消えても旧inline値や別の登録キーへフォールバックしない。
            selected["api_key"] = _vertex_key_registry(raw_global).get(reference, {}).get("api_key", "")
        elif not selected["api_key"]:
            selected["api_key"] = _retained_vertex_credentials(raw)["api_key"]
    return selected


def get_vertex_key_choices(room_name: str | None = None) -> list[tuple[str, str]]:
    raw_global, _ = _global_raw_connection()
    registry = _vertex_key_registry(raw_global)
    choices = [(entry["label"], reference) for reference, entry in registry.items()]
    raw = _room_raw_connection(room_name)["raw"] if room_name else raw_global
    reference = _selected_key_reference(raw, room_name, raw_global)
    if reference not in registry and room_name and _retained_vertex_credentials(raw)["api_key"]:
        choices.append(("従来の個別キー（保存時に引継ぎ）", reference))
    elif reference not in registry:
        choices.append(("登録が見つからないキー", reference))
    return choices


def get_vertex_settings(room_name: str | None = None, *, key_ref: str | None = None) -> dict[str, Any]:
    """登録状態と選択中認証方式の再表示値を返す。キー値は公開しない。"""

    raw_global, _ = _global_raw_connection()
    if room_name:
        raw = _room_raw_connection(room_name)["raw"]
    else:
        raw = raw_global
    stored = _retained_vertex_credentials(raw)
    reference = _key_reference(key_ref) if key_ref is not None else _selected_key_reference(raw, room_name, raw_global)
    registry = _vertex_key_registry(raw_global)
    pending = bool(room_name and stored["api_key"] and _stored_key_reference(raw) is None)
    configured = bool(registry.get(reference, {}).get("api_key"))
    if pending and reference == _selected_key_reference(raw, room_name, raw_global):
        configured = bool(stored["api_key"])
    public = {key: stored[key] for key in ("auth_mode", "project", "location")}
    if stored["auth_mode"] == "api_key":
        public.update(project=stored["api_key_project"], location=stored["api_key_location"])
    return public | {
        "api_key_configured": configured,
        "key_ref": reference,
        "key_choices": get_vertex_key_choices(room_name),
        "legacy_key_pending": pending,
    }


def _storage_id(value: Any) -> str:
    """非使用中の資格情報も含むCAS印。実行時の接続IDとは分ける。"""

    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _state_invalid_id(kind: str, value: Any) -> str:
    """読めるが実行には使えない保存値を、秘密なしのCAS印へ畳み込む。"""

    try:
        digest = _storage_id({"kind": kind, "value": value})
    except Exception:
        # 通常はJSON由来の値なのでこの分岐には入らない。メモリ上のtest
        # doubleが非JSON値を返した場合も、値や例外本文をstateへ出さない。
        digest = hashlib.sha256(
            f"{kind}:{type(value).__name__}:{repr(value)}".encode("utf-8", "replace")
        ).hexdigest()
    return f"invalid:{digest}"


def _state_connection_shape_is_valid(raw: Any) -> bool:
    """read_state専用の軽い構造検査。

    実行時の ``get_connection`` や詳細保存の検証を置き換えず、JSONとして
    読める不正値だけを不透明なCAS印へ隔離する。Vertexの必須値の有無は
    初回選択用draftを許すため、ここでは判定しない。
    """

    if raw is None:
        return True
    if not isinstance(raw, Mapping):
        return False
    if not raw:
        return True

    backend = raw.get("backend")
    if backend not in {"developer", "vertex"}:
        return False
    if "auth_mode" in raw:
        auth_mode = raw.get("auth_mode")
        if not isinstance(auth_mode, str) or auth_mode not in {"adc", "api_key"}:
            return False
    for field in ("project", "location", "api_key"):
        if field in raw and not isinstance(raw.get(field), str):
            return False
    if "key_ref" in raw:
        try:
            _key_reference(raw.get("key_ref"))
        except Exception:
            return False

    bank = raw.get(_VERTEX_CREDENTIALS_KEY)
    if bank is None:
        return True
    if not isinstance(bank, Mapping):
        return False
    try:
        _retained_vertex_credentials(raw)
        entries = bank.get(_VERTEX_SAVED_KEYS_KEY, {})
        if not isinstance(entries, Mapping):
            return False
        for reference, entry in entries.items():
            _key_reference(reference)
            if (reference == SHARED_VERTEX_KEY_REF
                    or not isinstance(entry, Mapping)
                    or not isinstance(entry.get("label"), str)
                    or not isinstance(entry.get("api_key"), str)
                    or not entry["api_key"].strip()):
                return False
    except Exception:
        return False
    return True


def _state_connection_id(raw: Any, raw_global: Any) -> str:
    """CAS用に接続を識別する。未実行の不正値は固定invalid印にする。"""

    if not _state_connection_shape_is_valid(raw):
        return _state_invalid_id("connection", raw)
    try:
        return connection_id(_resolve_connection(raw, raw_global))
    except Exception:
        # 参照先台帳が不正でも、読み取り可能な保存内容の変更はCASで
        # 検出できるようにする。実行経路のfail-closedは維持する。
        return _state_invalid_id("connection", raw)


def _state_vertex_keys_id(raw_global: Any) -> str:
    """Vertex台帳の不正値を例外ではなく秘密なしのCAS印にする。"""

    try:
        return _storage_id(_vertex_key_registry(raw_global))
    except Exception:
        return _state_invalid_id("vertex_keys", raw_global)


def _state_storage_id(kind: str, value: Any) -> str:
    """保存内容digestを作り、メモリ上の非JSON値も秘密なしで表す。"""

    try:
        return _storage_id(value)
    except Exception:
        return _state_invalid_id(kind, value)


def _state_resolved_connection(raw: Any, raw_global: Any) -> dict[str, Any]:
    """read_state内部の表示用接続を安全に作る。

    戻り値はstateへ直接渡さず、stateの識別子は必ず
    ``_state_connection_id``から作る。ここでのfallbackは、snapshotの形を
    保つためだけのもので、SDKや詳細保存の妥当性検証には使わない。
    """

    try:
        return _resolve_connection(raw, raw_global)
    except Exception:
        try:
            return normalize_connection(raw)
        except Exception:
            return deepcopy(_DEFAULT_CONNECTION)


def _room_state_snapshot(room_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    room = _room_raw_connection(room_name)
    global_raw, registered = _global_raw_connection()
    global_raw_for_resolution = global_raw if registered else None
    global_connection = _state_resolved_connection(global_raw_for_resolution, global_raw)
    global_connection_id = _state_connection_id(global_raw_for_resolution, global_raw)
    if room["present"] and not room["inherit"]:
        room_connection = _state_resolved_connection(room["raw"], global_raw)
        room_connection_id = _state_connection_id(room["raw"], global_raw)
    else:
        room_connection = None
        room_connection_id = None
    if (
        _effective_room_provider(room["provider"])
        and room["present"]
        and not room["inherit"]
    ):
        effective = room_connection
        effective_id = room_connection_id
    else:
        effective = deepcopy(global_connection)
        effective_id = global_connection_id
    room_meta = {
        "present": bool(room["present"]),
        "inherit": bool(room["inherit"]),
        "provider": room["provider"],
        "connection": room_connection,
        "connection_id": room_connection_id,
    }
    return effective, {
        "global": global_connection,
        "global_id": global_connection_id,
        "room": room_meta,
        "effective_id": effective_id,
    }


def read_state(room_name: str | None = None) -> dict[str, Any]:
    """CAS 用の読み取り state を作る。

    state は呼出し側が ``gr.State`` に保持する前提で、module global には保存し
    ない。CAS 印には接続識別子と保存内容のdigestを使い、API キーを state へコピーしない。
    """

    raw_global, registered = _global_raw_connection()
    global_raw_for_resolution = raw_global if registered else None
    global_connection_id = _state_connection_id(global_raw_for_resolution, raw_global)
    state: dict[str, Any] = {
        "version": 3,
        "vertex_keys_id": _state_vertex_keys_id(raw_global),
        "global_storage_id": _state_storage_id("global_storage", [registered, raw_global]),
        "room_storage_id": None,
        "room_name": room_name,
        "target": "room" if room_name else "global",
        # state は gr.State に保持する CAS 印であり、秘密値を含めない。
        "baseline": global_connection_id,
        "global_baseline": global_connection_id,
        "baseline_id": global_connection_id,
        "global_baseline_id": global_connection_id,
        "room_baseline": None,
        "room_baseline_id": None,
        "room_override_present": False,
        "room_inherit": False,
        "room_provider": None,
        # providerカテゴリとGemini backendの組み合わせを表す非秘密CAS印。
        "selection_provider": provider_choice(room_name),
    }
    if not room_name:
        return state

    effective, details = _room_state_snapshot(room_name)
    room_meta = details["room"]
    state.update(
        {
            "baseline": details["effective_id"],
            "baseline_id": details["effective_id"],
            "room_baseline": room_meta["connection_id"],
            "room_baseline_id": room_meta["connection_id"],
            "room_override_present": room_meta["present"],
            "room_inherit": room_meta["inherit"],
            "room_provider": room_meta["provider"],
            "room_storage_id": _state_storage_id(
                "room_storage", _room_raw_connection(room_name)
            ),
            "local": {"inherit": bool(room_meta["inherit"])},
        }
    )
    return state


def _raise_conflict() -> None:
    raise ConnectionConflictError(
        "Gemini接続設定が別の画面で変更されたため保存を拒否しました。最新状態を再読してください。"
    )


def _check_state_for_save(room_name: str | None, state: Mapping[str, Any] | None) -> None:
    if state is None:
        return
    if not isinstance(state, Mapping) or state.get("room_name") != room_name:
        _raise_conflict()
    if state.get("version") != 3:
        _raise_conflict()

    latest = read_state(room_name)
    if state.get("vertex_keys_id") != latest.get("vertex_keys_id"):
        _raise_conflict()
    # 接続CASだけではconfig/roomのprovider変更を検出できないため、公開UIの
    # 非秘密選択値も同時に照合する。古いstateは安全のため保存を拒否する。
    if state.get("selection_provider") != latest.get("selection_provider"):
        _raise_conflict()
    if room_name is None:
        if state.get("global_storage_id") != latest["global_storage_id"]:
            _raise_conflict()
        expected = state.get("global_baseline_id", state.get("baseline_id"))
        if expected != latest["global_baseline_id"]:
            _raise_conflict()
        return

    expected_room = state.get("room_baseline_id")
    latest_room = latest.get("room_baseline_id")
    room_flags_match = (
        bool(state.get("room_override_present")) == bool(latest.get("room_override_present"))
        and bool(state.get("room_inherit")) == bool(latest.get("room_inherit"))
        and state.get("room_provider") == latest.get("room_provider")
    )
    if (expected_room != latest_room or not room_flags_match
            or state.get("room_storage_id") != latest.get("room_storage_id")):
        _raise_conflict()

    # Inherited／未登録の room は global connection が実効値なので、そこが
    # 変わった古い画面からの保存も拒否する。
    if bool(state.get("room_inherit")) or not bool(state.get("room_override_present")):
        if (state.get("global_baseline_id") != latest.get("global_baseline_id")
                or state.get("global_storage_id") != latest.get("global_storage_id")):
            _raise_conflict()


def _existing_connection_for_save(room_name: str | None) -> Any:
    """同じ保存先だけを読む。継承中でもローカル保持値は共通へ置換しない。"""

    if room_name:
        room = _room_raw_connection(room_name)
        return deepcopy(room["raw"]) if room["present"] else None
    raw, registered = _global_raw_connection()
    return deepcopy(raw) if registered else None


def _bank_for_storage(raw: Any) -> dict[str, Any]:
    retained = _retained_vertex_credentials(raw)
    bank = raw.get(_VERTEX_CREDENTIALS_KEY, {}) if isinstance(raw, Mapping) else {}
    result = deepcopy(dict(bank))
    for key, value in retained.items():
        # 旧Developer設定の選択だけで空の新規フィールドを書き足すと、
        # 同じ画面のキー登録stateまで古くなる。APIキー接続の明示保存時に追加する。
        if key in {"api_key_project", "api_key_location"} and key not in bank:
            if value == ("" if key == "api_key_project" else _DEFAULT_LOCATION):
                continue
        result[key] = value
    return result


def _import_room_key(raw_global: Any, existing: Any, room_name: str) -> tuple[Any, str]:
    """旧個別キーを台帳へ先に保全するための値を作る。まだ保存しない。"""

    reference = _selected_key_reference(existing, room_name, raw_global)
    legacy = _retained_vertex_credentials(existing)["api_key"]
    if not legacy or _stored_key_reference(existing) is not None:
        return raw_global, reference
    registry = _vertex_key_registry(raw_global)
    if reference in registry:
        return raw_global, reference
    if raw_global is not None and not isinstance(raw_global, Mapping):
        raise _validation_error("共通の接続設定が不正です")
    amended = deepcopy(dict(raw_global)) if raw_global else deepcopy(_DEFAULT_CONNECTION)
    bank = _bank_for_storage(raw_global)
    entries = deepcopy(bank.get(_VERTEX_SAVED_KEYS_KEY, {}))
    entries[reference] = {"label": "旧個別キー（" + room_name[:120] + "）", "api_key": legacy}
    bank[_VERTEX_SAVED_KEYS_KEY] = entries
    amended[_VERTEX_CREDENTIALS_KEY] = bank
    return amended, reference


def _validate_provider_choice(choice: Any, *, room_name: str | None) -> str:
    allowed = _ROOM_PROVIDER_CHOICES if room_name else _PROVIDER_CHOICES
    if not isinstance(choice, str) or choice not in allowed:
        raise _validation_error("providerの選択が不正です")
    return choice


def _storage_provider_for_choice(choice: str) -> str:
    return "google" if choice in {"google", "vertex"} else choice


def _has_vertex_draft(raw: Any) -> bool:
    """Developer backendに切り替えた後も残るVertex draftの有無を判定する。"""

    if not isinstance(raw, Mapping):
        return False
    if raw.get("backend") == "vertex":
        return True
    bank = raw.get(_VERTEX_CREDENTIALS_KEY)
    if not isinstance(bank, Mapping):
        return False
    if any(
        isinstance(bank.get(field), str) and bool(bank.get(field).strip())
        for field in ("api_key", "project")
    ):
        return True
    if bank.get("location") not in (None, "", _DEFAULT_LOCATION):
        return True
    if bank.get("auth_mode") == "api_key":
        return True
    reference = bank.get("key_ref", raw.get("key_ref"))
    if reference not in (None, SHARED_VERTEX_KEY_REF):
        return True
    entries = bank.get(_VERTEX_SAVED_KEYS_KEY)
    return isinstance(entries, Mapping) and bool(entries)


def _vertex_selection_value(
    room_name: str | None,
    room_raw: Any,
    raw_global: Any,
) -> dict[str, Any]:
    """Vertex初回選択用の非秘密payloadを作る。

    既存のVertex draftを優先し、個別draftが未登録／継承中なら共通draftを
    参照する。台帳のAPIキー値はpayloadへコピーせず、key_refだけを返す。
    """

    room_source = _has_vertex_draft(room_raw)
    source = room_raw if room_source else (
        raw_global if isinstance(raw_global, Mapping) and raw_global.get("backend") == "vertex"
        else None
    )
    retained_source = source if source is not None else raw_global
    retained = _retained_vertex_credentials(retained_source)
    current = (
        normalize_connection(source)
        if isinstance(source, Mapping) and source.get("backend") == "vertex"
        else None
    )
    if current is not None:
        auth_mode = current.get("auth_mode")
        project = current.get("project", "")
        location = current.get("location", _DEFAULT_LOCATION)
    else:
        auth_mode = retained.get("auth_mode", "adc")
        prefix = "api_key_" if auth_mode == "api_key" else ""
        project = retained.get(prefix + "project", "")
        location = retained.get(prefix + "location", _DEFAULT_LOCATION)

    registry = _vertex_key_registry(raw_global)
    has_registered_key = any(
        isinstance(entry.get("api_key"), str) and bool(entry.get("api_key"))
        for entry in registry.values()
    )
    has_retained_key = bool(retained.get("api_key"))
    # ADC projectがない状態で登録済みキーがあれば、API keyを初期選択する。
    # 何も登録されていない新規VertexもAPI keyの未設定状態として保持し、
    # Developerへ暗黙に戻さない。
    if not project and (has_registered_key or has_retained_key):
        auth_mode = "api_key"
    elif auth_mode not in {"adc", "api_key"}:
        auth_mode = "api_key"
    elif auth_mode == "adc" and not project:
        auth_mode = "api_key"

    if auth_mode == "api_key" and not project:
        location = _DEFAULT_LOCATION

    if source is not None:
        key_ref = _stored_key_reference(source)
        if key_ref is None:
            key_ref = _selected_key_reference(
                source,
                room_name if room_source else None,
                raw_global,
            )
    else:
        key_ref = _stored_key_reference(raw_global)
        if key_ref is None:
            key_ref = SHARED_VERTEX_KEY_REF

    return {
        "backend": "vertex",
        "auth_mode": auth_mode,
        "project": project,
        "location": location,
        "api_key": "",
        "key_ref": key_ref,
    }


def _selection_value(
    choice: str,
    *,
    room_name: str | None,
    room_raw: Any,
    raw_global: Any,
) -> dict[str, Any] | None:
    if choice == "google":
        return {
            "backend": "developer",
            "auth_mode": "adc",
            "project": "",
            "location": _DEFAULT_LOCATION,
            "api_key": "",
        }
    if choice == "vertex":
        return _vertex_selection_value(room_name, room_raw, raw_global)
    if choice == "default":
        return {"inherit": True}
    return None


def _connection_for_save(value: Any, existing: Any, *, room_name: str | None = None,
                         raw_global: Any = None,
                         allow_incomplete: bool = False) -> tuple[dict[str, Any], Any]:
    """参照と接続設定を保存する。旧ルームキーは先に台帳へ取り込む。"""

    if not isinstance(value, Mapping):
        raise _validation_error("接続先の指定が不正です")
    provided = value.get("api_key", "")
    legacy = _retained_vertex_credentials(existing)["api_key"]
    if not isinstance(provided, str) or (provided and provided.strip() != legacy):
        raise _validation_error("APIキーはAPIキー管理で登録してください")
    bank = _bank_for_storage(existing)
    amended_global = raw_global
    old_reference = _selected_key_reference(existing, room_name, raw_global)
    if room_name:
        amended_global, old_reference = _import_room_key(raw_global, existing, room_name)
    reference = _key_reference(value.get("key_ref", old_reference))
    if reference not in _vertex_key_registry(amended_global):
        raise _validation_error("使用するVertex登録キーが見つかりません")
    inherit = room_name is not None and value.get("inherit") is True
    selected = deepcopy(_DEFAULT_CONNECTION) if inherit else normalize_connection(value)
    selected["api_key"] = ""
    selected["key_ref"] = reference
    if selected.get("backend") == "vertex" and selected.get("auth_mode") == "adc":
        selected["project"] = selected.get("project", "") or bank["project"]
    if not inherit and not allow_incomplete:
        _validate_connection(_resolve_connection(selected, amended_global))
    if selected.get("backend") == "vertex":
        bank["auth_mode"] = selected["auth_mode"]
        if selected["auth_mode"] == "adc":
            bank.update(project=selected["project"], location=selected["location"])
        else:
            bank.update(api_key_project=selected["project"], api_key_location=selected["location"])
    bank["key_ref"] = reference
    if room_name:
        # room_managerはdeep mergeする。省略すると旧inlineの秘密値が残る。
        bank["api_key"] = ""
    selected[_VERTEX_CREDENTIALS_KEY] = bank
    if room_name:
        selected["inherit"] = bool(inherit)
    return selected, amended_global


def _vertex_key_registration_for_save(api_key: Any, existing: Any, *, key_ref: str) -> dict[str, Any]:
    if not isinstance(api_key, str):
        raise _validation_error("Vertex APIキーは文字列で指定してください")
    registry = _vertex_key_registry(existing)
    reference = _key_reference(key_ref)
    if reference not in registry:
        raise _validation_error("登録先のVertexキーが見つかりません")
    key = api_key.strip() or registry[reference]["api_key"]
    if not key:
        raise _validation_error("Vertex APIキーを入力してください")
    bank = _bank_for_storage(existing)
    if reference == SHARED_VERTEX_KEY_REF:
        bank["api_key"] = key
    else:
        bank[_VERTEX_SAVED_KEYS_KEY][reference]["api_key"] = key
    selected_reference = _selected_key_reference(existing, None, existing)
    selected = normalize_connection(existing)
    selected["api_key"] = ""
    selected["key_ref"] = selected_reference
    bank["key_ref"] = selected_reference
    selected[_VERTEX_CREDENTIALS_KEY] = bank
    return selected


def _save_room_selection(
    room_manager: Any,
    room_name: str,
    provider: str | None,
    connection_value: Mapping[str, Any] | None = None,
) -> Any:
    """providerと接続draftを、可能なら1回のroom差分書込で保存する。"""

    updates: dict[str, Any] = {}
    if provider is not None:
        updates["provider"] = provider
    if connection_value is not None:
        updates[CONNECTION_KEY] = deepcopy(dict(connection_value))
    update_config = getattr(room_manager, "update_room_config", None)
    if callable(update_config):
        return update_config(room_name, {"override_settings": updates})

    # 既存の最小test double／旧実装との互換。製品room_managerは上の
    # update_room_config経路でproviderと接続を同一差分へまとめる。
    if connection_value is not None:
        saved = room_manager.update_room_override_key(
            room_name, CONNECTION_KEY, deepcopy(dict(connection_value))
        )
        if saved is False:
            return False
    if provider is None:
        return True
    return room_manager.update_room_override_key(room_name, "provider", provider)


def _save_connection(
    value: Any,
    *,
    room_name: str | None = None,
    state: Mapping[str, Any] | None = None,
    register_vertex_key: bool = False,
    key_ref: str = SHARED_VERTEX_KEY_REF,
    route_choice: str | None = None,
) -> dict[str, Any]:
    """既存のロック順で参照を保存し、必要なら旧キーを先に共通台帳へ保全する。"""

    if route_choice is not None:
        route_choice = _validate_provider_choice(route_choice, room_name=room_name)

    # response_reasoning_settings と同じく、ロック取得→最新read→CAS→差分保存→
    # 保存後readを一つの区間にする。room保存もlock順は共通→roomに固定する。
    with ExitStack() as stack:
        config_manager = _config_manager()
        stack.enter_context(
            file_lock_utils.locked_file(
                constants.CONFIG_FILE,
                timeout=getattr(
                    config_manager,
                    "CONFIG_LOCK_TIMEOUT",
                    file_lock_utils.DEFAULT_LOCK_TIMEOUT,
                ),
            )
        )
        room_manager = None
        if room_name:
            room_manager = _room_manager()
            room_guard = getattr(room_manager, "room_config_write_guard", None)
            if callable(room_guard):
                stack.enter_context(room_guard(room_name))

        _check_state_for_save(room_name, state)
        # OpenAI／Anthropic／local、またroomのdefaultはGemini接続draftを
        # 変更せずproviderだけを保存する。後でGoogleへ戻した時にdraftを復元できる。
        if route_choice is not None and route_choice not in {"google", "vertex"}:
            storage_provider = _storage_provider_for_choice(route_choice)
            if room_name:
                saved = _save_room_selection(room_manager, room_name, storage_provider)
            else:
                try:
                    saved = config_manager.update_config_keys(
                        {"active_provider": storage_provider}
                    )
                except Exception:
                    raise GeminiConnectionError("共通provider設定を保存できませんでした") from None
            if saved is False:
                raise GeminiConnectionError("provider設定を保存できませんでした")
            return read_state(room_name)

        if room_name:
            existing = _existing_connection_for_save(room_name)
            raw_global, _ = _global_raw_connection()
            save_value, amended_global = _connection_for_save(
                value, existing, room_name=room_name, raw_global=raw_global,
                allow_incomplete=route_choice is not None,
            )
            if amended_global != raw_global:
                try:
                    registered = config_manager.update_config_keys({CONNECTION_KEY: amended_global})
                except Exception:
                    raise GeminiConnectionError("旧VertexキーをAPIキー管理へ引き継げませんでした") from None
                if registered is False:
                    raise GeminiConnectionError("旧VertexキーをAPIキー管理へ引き継げませんでした")
            try:
                saved = _save_room_selection(
                    room_manager,
                    room_name,
                    _storage_provider_for_choice(route_choice) if route_choice else None,
                    save_value,
                )
            except Exception as exc:
                raise GeminiConnectionError("ルームのGemini接続設定を保存できませんでした") from None
            if saved is False:
                raise GeminiConnectionError("ルームのGemini接続設定を保存できませんでした")
        else:
            if isinstance(value, Mapping) and value.get("inherit") is True:
                raise _validation_error("共通設定には inherit を指定できません")
            existing = _existing_connection_for_save(None)
            normalized = (
                _vertex_key_registration_for_save(value, existing, key_ref=key_ref)
                if register_vertex_key else
                _connection_for_save(
                    value,
                    existing,
                    raw_global=existing,
                    allow_incomplete=route_choice is not None,
                )[0]
            )
            updates = {CONNECTION_KEY: normalized}
            if route_choice is not None:
                updates["active_provider"] = _storage_provider_for_choice(route_choice)
            try:
                saved = config_manager.update_config_keys(updates)
            except Exception as exc:
                raise GeminiConnectionError("共通のGemini接続設定を保存できませんでした") from None
            if saved is False:
                # update_config_keys の通常の「変更なし」は "no_change"。False は
                # 保存失敗を表す test double／実装だけをここで拒否する。
                raise GeminiConnectionError("共通のGemini接続設定を保存できませんでした")
        return read_state(room_name)


def save_connection(
    value: Any,
    *,
    room_name: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """接続先を選択して保存し、同じscopeの資格情報を保持する。"""

    return _save_connection(value, room_name=room_name, state=state)


def select_provider(
    choice: str,
    *,
    room_name: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """統合provider UIの選択を保存し、更新後のCAS stateを返す。

    ``vertex`` は永続providerとして保存せず、従来の ``provider="google"`` と
    connection backend ``vertex`` の組み合わせへ変換する。選択操作ではVertex
    APIキーやADC projectが未登録でもdraftを保存でき、実行時の既存validationが
    不完全設定をfail-closedで止める。
    """

    choice = _validate_provider_choice(choice, room_name=room_name)
    raw_global, registered = _global_raw_connection()
    if not registered:
        raw_global = None
    room_raw = _room_raw_connection(room_name).get("raw") if room_name else None
    value = _selection_value(
        choice,
        room_name=room_name,
        room_raw=room_raw,
        raw_global=raw_global,
    )
    return _save_connection(
        value if value is not None else {},
        room_name=room_name,
        state=state,
        route_choice=choice,
    )


def save_vertex_api_key(api_key: Any, *, state: Mapping[str, Any] | None = None,
                        key_ref: str = SHARED_VERTEX_KEY_REF) -> dict[str, Any]:
    """共通Vertexキーを登録する。選択中の接続先は切り替えない。"""

    return _save_connection(api_key, state=state, register_vertex_key=True, key_ref=key_ref)


def _is_non_empty_secret(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _legacy_api_key(api_key: str | None = None) -> str | None:
    if _is_non_empty_secret(api_key):
        return api_key.strip()

    config_manager = _config_manager()
    getter = getattr(config_manager, "get_active_gemini_api_key", None)
    if callable(getter):
        try:
            value = getter(None)
        except TypeError:
            try:
                value = getter()
            except Exception:
                value = None
        except Exception:
            value = None
        if _is_non_empty_secret(value) and not value.strip().startswith("YOUR_API_KEY"):
            return value.strip()

    keys = getattr(config_manager, "GEMINI_API_KEYS", {})
    if isinstance(keys, Mapping):
        for value in keys.values():
            if _is_non_empty_secret(value) and not value.strip().startswith("YOUR_API_KEY"):
                return value.strip()
    return None


def _with_http_options(kwargs: dict[str, Any], http_options: Any) -> dict[str, Any]:
    if http_options is not None:
        kwargs["http_options"] = http_options
    return kwargs


def create_client(
    connection: Mapping[str, Any] | Any,
    *,
    api_key: str | None = None,
    http_options: Any = None,
) -> genai.Client:
    """接続設定から ``google.genai.Client`` を生成する。

    認証失敗の例外本文は SDK が秘密値を含める可能性があるため、そのまま公開
    せず、このモジュールの例外へ包む。
    """

    normalized = normalize_connection(connection, require_valid=True)
    backend = normalized["backend"]
    auth_mode = normalized["auth_mode"]
    if backend == "developer":
        key = _legacy_api_key(api_key)
        if not key:
            raise _validation_error("Developer API の従来 gemini_api_keys に有効なキーがありません")
    try:
        if backend == "developer":
            kwargs = {"vertexai": False, "api_key": key}
            _with_http_options(kwargs, http_options)
            return genai.Client(**kwargs)

        if auth_mode == "adc":
            credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
            kwargs = {
                "vertexai": True,
                "credentials": credentials,
                "project": normalized["project"],
                "location": normalized["location"],
            }
            _with_http_options(kwargs, http_options)
            return genai.Client(**kwargs)

        # projectが空ならExpress、指定時は同じキーでproject単位の経路を使う。
        # SDKにはキーを明示し、環境変数やADCへ認証を委ねない。
        key = normalized["api_key"]
        kwargs = {"vertexai": True, "api_key": key}
        if normalized["project"]:
            kwargs.update(project=normalized["project"], location=normalized["location"])
        _with_http_options(kwargs, http_options)
        return genai.Client(**kwargs)
    except Exception as exc:
        if backend == "vertex":
            raise _vertex_operation_error(exc, initializing=True) from None
        raise GeminiConnectionError(
            f"Gemini SDK クライアントの初期化に失敗しました ({type(exc).__name__})"
        ) from None


class _ConnectionChatGoogleGenerativeAI(ChatGoogleGenerativeAI):
    """接続境界だけを差し替え、LangChain の通常処理を継承する。"""

    gemini_connection: dict[str, Any] = Field(
        default_factory=lambda: deepcopy(_DEFAULT_CONNECTION),
        exclude=True,
        repr=False,
    )
    # テスト／呼出し側が SDK 互換の薄い fake client を注入できるようにする。
    client: Any = Field(default=None, exclude=True, repr=False)
    http_options: Any = Field(default=None, exclude=True, repr=False)

    @property
    def connection(self) -> dict[str, Any]:
        """短い別名。公開設定の正本は ``gemini_connection``。"""

        return self.gemini_connection

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        connection = normalize_connection(self.gemini_connection, require_valid=True)
        object.__setattr__(self, "gemini_connection", connection)
        vertex = connection.get("backend") == "vertex"
        provided_api_key = None
        if not vertex and "google_api_key" in self.model_fields_set:
            candidate = self.google_api_key
            if hasattr(candidate, "get_secret_value"):
                candidate = candidate.get_secret_value()
            provided_api_key = candidate

        # 親の SDK 認証 validator は Vertex API key のため os.environ を一時変更
        # するため呼ばない。親が担っていた値検証と metadata 初期化だけ保つ。
        if self.temperature is not None and not 0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be in the range [0.0, 2.0]")
        if self.top_p is not None and not 0 <= self.top_p <= 1:
            raise ValueError("top_p must be in the range [0.0, 1.0]")
        if self.top_k is not None and self.top_k <= 0:
            raise ValueError("top_k must be positive")

        # 親 4.2 の Gemini 3 既定温度を取り込む。private helper の存在は SDK
        # 版差分に備えて任意扱いにする。
        if "temperature" not in self.model_fields_set:
            try:
                from langchain_google_genai.chat_models import _is_gemini_3_or_later

                if _is_gemini_3_or_later(self.model):
                    self.temperature = 1.0
            except Exception:
                pass

        for name in ("frequency_penalty", "presence_penalty"):
            value = getattr(self, name)
            if value is not None and not -2 <= value <= 2:
                raise ValueError(f"{name} must be in the range [-2.0, 2.0]")

        additional_headers = self.additional_headers or {}
        self.default_metadata = tuple(additional_headers.items())
        object.__setattr__(self, "vertexai", vertex)
        object.__setattr__(self, "google_api_key", None)
        use_vertex_project = vertex and connection.get("auth_mode") == "adc"
        object.__setattr__(
            self,
            "project",
            connection.get("project") if use_vertex_project else None,
        )
        object.__setattr__(
            self,
            "location",
            connection.get("location") if use_vertex_project else None,
        )

        if self.client is None:
            injected_api_key = None
            if not vertex:
                injected_api_key = provided_api_key
            options = self.http_options or _build_http_options(self)
            object.__setattr__(
                self,
                "client",
                create_client(connection, api_key=injected_api_key, http_options=options),
            )
        # 親4.4系はasync_clientとacloseがこの共有cleanup tokenを使う。
        # 認証validatorを迂回しても、SDK clientの寿命管理は親と揃える。
        from langchain_google_genai.chat_models import _ClientCleanup

        self._client_cleanup = _ClientCleanup(self.client)
        return self

    def _prepare_request(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        request = super()._prepare_request(messages, **kwargs)
        if not uses_argon_request_rules(self.model):
            return request
        config = request["config"]
        thinking = config.thinking_config
        if thinking is not None:
            if thinking.thinking_budget is not None:
                raise ValueError("Argonの推論設定にはbudgetではなくlow／medium／highを指定してください。")
            level = thinking.thinking_level
            normalized_level = str(getattr(level, "value", level)).upper()
            if level is not None and normalized_level not in {"LOW", "MEDIUM", "HIGH"}:
                raise ValueError("Argonの推論レベルはlow／medium／highです。")
        history = request["contents"]
        if history and history[-1].role == "model":
            raise ValueError("Argonの送信履歴はユーザー入力またはツール結果で終える必要があります。")
        # constructor・bind・generation_configのどこから来た値でも送信直前に除く。
        # 履歴partや思考署名は変更しない。明示cacheはArgonでは利用しない。
        request["config"] = config.model_copy(update={
            key: None for key in ("temperature", "top_p", "top_k", "candidate_count",
                                  "frequency_penalty", "presence_penalty", "cached_content")
        })
        return request

    def _is_vertex_connection(self) -> bool:
        return self.gemini_connection.get("backend") == "vertex"

    @staticmethod
    def _vertex_invocation_error(exc: Exception) -> GeminiConnectionError:
        """SDKの原因本文を隠し、コードと種別だけを診断へ残す。"""

        return _vertex_operation_error(exc)

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        """Vertex の SDK 例外を秘密を含まないエラーへ変換する。"""

        if not self._is_vertex_connection():
            # Developer API は既存の 429/503 回転とエラー型を維持する。
            return super()._generate(*args, **kwargs)
        try:
            return super()._generate(*args, **kwargs)
        except Exception as exc:
            raise self._vertex_invocation_error(exc) from None

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        """ストリーム反復中に発生した Vertex 例外も安全な型へ変換する。"""

        if not self._is_vertex_connection():
            yield from super()._stream(*args, **kwargs)
            return
        try:
            yield from super()._stream(*args, **kwargs)
        except Exception as exc:
            raise self._vertex_invocation_error(exc) from None

    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        """非同期 Vertex 呼出しの例外本文を公開しない。"""

        if not self._is_vertex_connection():
            return await super()._agenerate(*args, **kwargs)
        try:
            return await super()._agenerate(*args, **kwargs)
        except Exception as exc:
            raise self._vertex_invocation_error(exc) from None

    async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        """非同期ストリーム反復中の Vertex 例外も安全な型へ変換する。"""

        if not self._is_vertex_connection():
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
            return
        try:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
        except Exception as exc:
            raise self._vertex_invocation_error(exc) from None


def _build_http_options(model: _ConnectionChatGoogleGenerativeAI) -> Any:
    """親 validator と同じ metadata/header の初期化を行う。"""

    from google.genai.types import HttpOptions

    base_url = model.base_url
    if isinstance(base_url, dict):
        keys = list(base_url.keys())
        if keys == ["api_endpoint"]:
            base_url = base_url["api_endpoint"]
        elif keys:
            raise ValueError(
                "When providing base_url as a dict, it can only contain the "
                "api_endpoint key."
            )
        else:
            raise ValueError(
                "base_url must be a string or a dict containing the "
                "api_endpoint key."
            )
    from langchain_google_genai.chat_models import get_user_agent

    _, user_agent = get_user_agent("ChatGoogleGenerativeAI")
    headers = {"User-Agent": user_agent, **dict(model.additional_headers or {})}
    return HttpOptions(
        base_url=base_url,
        api_version=model.api_version,
        headers=headers,
        client_args=model.client_args,
        async_client_args=model.client_args,
    )


def build_chat_model(connection: Mapping[str, Any] | Any, **kwargs: Any) -> ChatGoogleGenerativeAI:
    """接続設定を埋め込んだ LangChain Gemini chat model を作る。"""

    normalized = normalize_connection(connection, require_valid=True)
    model_kwargs = dict(kwargs)
    model_kwargs.pop("connection", None)
    model_kwargs.pop("gemini_connection", None)
    model_kwargs["gemini_connection"] = normalized

    vertex = normalized["backend"] == "vertex"
    legacy_api_key = None
    if not vertex:
        # get_configured_llm の既存呼出しは google_api_key、直接利用者は
        # api_key を渡すため、どちらも Developer の従来キー上書きとして扱う。
        legacy_api_key = kwargs.get("api_key") or kwargs.get("google_api_key")
    # Pydantic の env default (GOOGLE_API_KEY / GOOGLE_CLOUD_LOCATION) を使わせず、
    # 接続 dict を常に正本にする。
    model_kwargs["vertexai"] = vertex
    model_kwargs["api_key"] = None if vertex else legacy_api_key
    use_vertex_project = vertex and normalized.get("auth_mode") == "adc"
    model_kwargs["location"] = normalized.get("location") if use_vertex_project else None
    model_kwargs["project"] = normalized.get("project") if use_vertex_project else None
    return _ConnectionChatGoogleGenerativeAI(**model_kwargs)


__all__ = [
    "CONNECTION_KEY",
    "ConnectionConflictError",
    "GeminiConnectionError",
    "build_chat_model",
    "connection_id",
    "create_client",
    "get_connection",
    "get_vertex_settings",
    "is_vertex",
    "normalize_connection",
    "provider_choice",
    "read_state",
    "save_connection",
    "save_vertex_api_key",
    "select_provider",
    "vertex_error_message",
]
