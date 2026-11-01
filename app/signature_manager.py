"""Geminiのツール呼び出し用思考署名を、ターン単位で保存・復元する。"""

import base64
import json
import os
import time
from typing import Any, Dict, Optional

import constants
import file_lock_utils
from gemini_model_features import normalize_model_name, tool_signature_map


_SIGNATURE_MODEL_KEY = "signature_model_name"
_SIGNATURE_CONNECTION_KEY = "signature_connection_id"



def _connection_identity(room_name: str, explicit: Optional[str] = None) -> str:
    if explicit is not None:
        return explicit
    try:
        import gemini_connection
        return gemini_connection.connection_id(gemini_connection.get_connection(room_name))
    except (ValueError, TypeError, OSError, gemini_connection.GeminiConnectionError):
        # 壊れた接続設定をDeveloper由来と認定しない。
        return "invalid"


def _get_signature_file_path(room_name: str) -> str:
    """署名ファイルのパスを取得する。"""
    return os.path.join(constants.ROOMS_DIR, room_name, "private", "thought_signatures.json")


def _canonical_model_name(model_name: Optional[str]) -> Optional[str]:
    """保存・比較用のモデル名を正規化する。"""
    normalized = normalize_model_name(model_name or "")
    return normalized or None


def _stored_model_name(data: Dict[str, Any]) -> Optional[str]:
    """署名生成元の正本キーだけからモデル名を取得する。"""
    value = data.get(_SIGNATURE_MODEL_KEY)
    return _canonical_model_name(str(value)) if value else None


def _json_safe(value: Any) -> Any:
    """JSON保存できる形へ変換する（SDKのbytes署名も保持する）。"""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii") if value else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def _copy_tool_calls(tool_calls: Any) -> list:
    """保存対象のtool callを浅い辞書コピーで隔離する。"""
    if not isinstance(tool_calls, list):
        return []
    return [dict(call) for call in tool_calls if isinstance(call, dict)]


def _signature_map(signature: Any, tool_calls: list) -> dict:
    """署名をtool call ID単位へ変換し、並列呼出しへの複製を防ぐ。"""
    if tool_calls:
        return tool_signature_map(signature, tool_calls)
    if isinstance(signature, dict):
        safe = _json_safe(signature)
        return safe if isinstance(safe, dict) else {}
    return {}


def _legacy_signature_value(signature: Any, mapped: dict) -> Any:
    """旧last_signatureキー用のJSON互換値を生成する。"""
    if mapped:
        return mapped
    if isinstance(signature, list):
        return _json_safe(signature[0]) if signature else None
    return _json_safe(signature)


def _new_context(signature: Any, tool_calls: Any, model_name: Optional[str], connection_id: str = "developer") -> Dict[str, Any]:
    """新しいターンコンテキストを生成する。"""
    calls = _copy_tool_calls(tool_calls)
    mapped = _signature_map(signature, calls)
    data: Dict[str, Any] = {
        "gemini_function_call_thought_signatures": mapped or None,
        "last_tool_calls": calls,
        "last_signature": _legacy_signature_value(signature, mapped),
        "updated_at": str(time.time()),
    }
    canonical_model = _canonical_model_name(model_name)
    if canonical_model:
        data[_SIGNATURE_MODEL_KEY] = canonical_model
    if connection_id != "developer":
        data[_SIGNATURE_CONNECTION_KEY] = connection_id
    return data


def _save_context_update(
    existing: Any,
    signature: Any,
    tool_calls: Any,
    model_name: Optional[str],
    connection_id: str = "developer",
) -> Dict[str, Any]:
    """保存時のモデル境界とAct 2保全を一箇所で適用する。"""
    previous = dict(existing) if isinstance(existing, dict) else {}
    calls = _copy_tool_calls(tool_calls)
    requested_model = _canonical_model_name(model_name)
    previous_model = _stored_model_name(previous)

    # Act 2の最終テキストはツール呼出しを持たない。署名が届いても、
    # 直前のfunctionCall署名へ対応付ける根拠がないため、旧contextを保つ。
    if not calls and previous.get("last_tool_calls"):
        same_model = (
            model_name is None
            or (requested_model is not None and previous_model == requested_model)
        )
        same_connection = previous.get(_SIGNATURE_CONNECTION_KEY, "developer") == connection_id
        if same_model and same_connection:
            return previous

    updated = _new_context(signature, calls, requested_model, connection_id)

    # model_nameを明示しない旧呼び出しは生成元を証明できない。
    # 既存の由来を新しい署名へ誤継承しないよう、tool call更新時は落とす。
    if model_name is None:
        updated.pop(_SIGNATURE_MODEL_KEY, None)
    return updated


def save_turn_context(
    room_name: str,
    signature: Any,
    tool_calls: list,
    model_name: Optional[str] = None,
    connection_id: Optional[str] = None,
) -> None:
    """最新ターンの署名・tool call・生成元モデルを原子的に保存する。"""
    if not room_name:
        return

    file_path = _get_signature_file_path(room_name)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    try:
        file_lock_utils.safe_json_update(
            file_path,
            lambda existing: _save_context_update(
                existing, signature, tool_calls, model_name, _connection_identity(room_name, connection_id),
            ),
            default={},
        )
    except Exception as exc:
        print(f"  - [SignatureManager] 保存エラー: {exc}")


def get_turn_context(room_name: str, model_name: Optional[str] = None, connection_id: Optional[str] = None) -> Dict[str, Any]:
    """最新contextを読み込む。モデル指定時は由来モデルが一致する場合だけ返す。"""
    if not room_name:
        return {}
    file_path = _get_signature_file_path(room_name)
    if not os.path.exists(file_path):
        return {}

    try:
        data = file_lock_utils.safe_json_read(file_path, default={})
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    if data.get(_SIGNATURE_CONNECTION_KEY, "developer") != _connection_identity(room_name, connection_id):
        return {}
    if model_name is not None:
        requested_model = _canonical_model_name(model_name)
        if not requested_model or _stored_model_name(data) != requested_model:
            return {}
    return data


def get_thought_signature(room_name: str, model_name: Optional[str] = None, connection_id: Optional[str] = None) -> Optional[Any]:
    """後方互換API。モデル指定時は一致したcontextの署名だけ返す。"""
    data = get_turn_context(room_name, model_name=model_name, connection_id=connection_id)
    return data.get("gemini_function_call_thought_signatures") or data.get("last_signature")


def save_thought_signature(room_name: str, signature: Any) -> None:
    """ツール呼出しなしの旧APIを維持する。"""
    save_turn_context(room_name, signature, [])
