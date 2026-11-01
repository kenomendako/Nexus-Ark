"""Geminiのモデル名に依存する履歴能力を、副作用なしで判定する。"""

import base64
import re


def normalize_model_name(model_name: str) -> str:
    name = str(model_name or "").strip().lstrip("⭐ ").lower()
    name = name.removeprefix("models/")
    return name.split(" (", 1)[0].strip()


def uses_argon_request_rules(model_name: str) -> bool:
    """公式に確認したArgonだけに固定sampling等の制約を適用する。"""
    from constants import GEMINI_ARGON_MODEL_IDS

    return normalize_model_name(model_name).rsplit("/", 1)[-1] in GEMINI_ARGON_MODEL_IDS


def supports_thought_signatures(model_name: str) -> bool:
    """generateContentでツール署名の再送が必須のGemini 3以降を判定する。"""
    match = re.match(r"gemini[-_\s]?(\d+)", normalize_model_name(model_name))
    return bool(match and int(match.group(1)) >= 3)


def supports_native_thinking_parts(model_name: str) -> bool:
    return supports_thought_signatures(model_name) or "thinking" in normalize_model_name(model_name)


def tool_signature_map(signature, tool_calls: list) -> dict:
    """受信した署名を元のtool IDへ対応付け、単一署名を並列呼出しへ複製しない。"""
    ids = [call.get("id") for call in (tool_calls or []) if isinstance(call, dict)]

    def value(raw):
        if isinstance(raw, bytes):
            return base64.b64encode(raw).decode("ascii") if raw else None
        return raw if isinstance(raw, str) and raw else None

    if isinstance(signature, dict):
        return {key: converted for key, raw in signature.items() if key in ids and (converted := value(raw))}
    values = signature if isinstance(signature, list) else [signature]
    return {key: converted for key, raw in zip(ids, values) if key and (converted := value(raw))}
