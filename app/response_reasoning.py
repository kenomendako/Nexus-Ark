"""応答モデルの推論設定に関する副作用のない共通契約。

このモジュールは、設定画面やLLMクライアントを直接 import しない。
対象の正規化・能力判定・保存値の検証だけを担当し、実際のSDK引数への変換は
呼び出し側が担当する。未知の接続先やモデルについては推測で送信しない。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


_DEFAULT_CAPABILITIES = {
    "efforts": [],
    "budget": False,
    "transport": "none",
    "note": "この接続先・モデルの推論能力は未登録です。APIの既定値を使用します。",
}

_OPENAI_EFFORTS = {
    "o3": ["low", "medium", "high"],
    "o3-mini": ["low", "medium", "high"],
    "o4-mini": ["low", "medium", "high"],
    "gpt-5": ["minimal", "low", "medium", "high"],
    "gpt-5-mini": ["minimal", "low", "medium", "high"],
    "gpt-5-nano": ["minimal", "low", "medium", "high"],
    "gpt-5.1": ["none", "low", "medium", "high"],
    "gpt-5.2": ["none", "low", "medium", "high", "xhigh"],
    "gpt-5.4": ["none", "low", "medium", "high", "xhigh"],
    "gpt-5.4-mini": ["none", "low", "medium", "high", "xhigh"],
    "gpt-5.4-nano": ["none", "low", "medium", "high", "xhigh"],
    "gpt-5.5": ["none", "low", "medium", "high", "xhigh"],
}

# xAIはモデル世代によって reasoning_effort の可否が異なるため、
# ``grok-`` 全体を許可せず、公式に確認したモデルIDだけを列挙する。
_XAI_EFFORTS = {
    "grok-3-mini": ["low", "high"],
    "grok-4.5": ["low", "medium", "high"],
    "grok-4.6": ["low", "medium", "high", "xhigh"],
    "grok-4.7": ["low", "medium", "high", "xhigh"],
}

# Anthropicの effort は対応モデルを限定する。旧Claudeの手動thinkingは
# effortとは別の ``budget`` 選択として扱う。
_ANTHROPIC_EFFORTS = {
    "claude-opus-4-5": ["low", "medium", "high"],
    "claude-opus-4-6": ["low", "medium", "high", "max"],
    "claude-sonnet-4-6": ["low", "medium", "high", "max"],
    "claude-opus-4-7": ["low", "medium", "high", "max", "xhigh"],
    "claude-opus-4-8": ["low", "medium", "high", "max", "xhigh"],
    "claude-opus-5": ["low", "medium", "high", "max", "xhigh"],
    "claude-opus-5-5": ["low", "medium", "high", "max", "xhigh"],
    "claude-sonnet-5": ["low", "medium", "high", "max", "xhigh"],
    "claude-sonnet-5-5": ["low", "medium", "high", "max", "xhigh"],
    "claude-haiku-5-5": ["low", "medium", "high", "max", "xhigh"],
    "claude-fable-5": ["low", "medium", "high", "max", "xhigh"],
    "claude-fable-5-1": ["low", "medium", "high", "max", "xhigh"],
    "claude-mythos-5": ["low", "medium", "high", "max", "xhigh"],
    "claude-mythos-5-1": ["low", "medium", "high", "max", "xhigh"],
}

_ANTHROPIC_MANUAL_THINKING_MODELS = {
    "claude-3-7-sonnet-20250219",
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
}
_ANTHROPIC_ADAPTIVE_EFFORT_MODELS = {
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
}
_ANTHROPIC_MANUAL_BUDGET_LIMITS = {
    "claude-3-7-sonnet-20250219": 32768,
    # 可視応答1024tokensを含めても標準の32K出力枠へ収める。
    "claude-opus-4-20250514": 32000 - 1024,
    "claude-sonnet-4-20250514": 32768,
    "claude-opus-4-5": 32768,
}

_GEMINI_EFFORTS_BY_MODEL = {
    "gemini-4-argon": ["low", "medium", "high"],
    "gemini-3.8-flash": ["low", "medium", "high"],
    "gemini-3.6-flash": ["minimal", "low", "medium", "high"],
    "gemini-3.5-flash-lite": ["minimal", "low", "medium", "high"],
    "gemini-3-flash-preview": ["minimal", "low", "medium", "high"],
    "gemini-3.1-pro-preview": ["low", "medium", "high"],
    "gemini-3-pro-preview": ["low", "high"],
}
_OPENROUTER_GATEWAY_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]

_DATE_SUFFIX = re.compile(r"^(?P<base>.+)-\d{4}-\d{2}-\d{2}$")
_MODEL_METADATA_REGISTRY: dict[tuple[str, str], dict[str, Any]] = {}


def _strip_model_decoration(model: Any) -> str:
    """UIのfavorite/注釈を取り除き、プロバイダへ渡すIDを保つ。"""

    value = str(model or "").strip()
    if value.startswith("⭐"):
        value = value[1:].lstrip()
    if " (" in value:
        value = value.split(" (", 1)[0].rstrip()
    return value


def _normalise_provider(provider: Any) -> str:
    return str(provider or "").strip().lower().replace(" ", "_")


def _normalise_base_url(base_url: Any) -> str:
    value = str(base_url or "").strip()
    if not value:
        return ""
    # target_keyではquery/fragment/userinfoを捨てる。保存対象のbase_url自体は
    # make_targetで原文を保持するため、presetや既存設定を壊さない。
    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return value.lower().rstrip("/")
        port = parsed.port
        netloc = hostname
        if port is not None and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
            netloc = f"{hostname}:{port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        return value.lower().split("?", 1)[0].split("#", 1)[0].rstrip("/")


def _host(base_url: Any) -> str:
    value = str(base_url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
        return (parsed.hostname or "").lower()
    except ValueError:
        return ""


def _model_base(model: str) -> str:
    """公式の日付付きIDだけを既知のcatalogへ対応づける。"""

    # OpenRouterのpreset付きIDは、明示された元モデル部分だけ判定する。
    # ``@preset/foo``単体には元モデルが無いため、呼び出し側で未知扱いにする。
    model = model.split("@preset/", 1)[0]
    match = _DATE_SUFFIX.match(model)
    return match.group("base") if match else model


def _metadata(target: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("reasoning_metadata", "model_metadata", "metadata"):
        value = target.get(key)
        if isinstance(value, Mapping) and value:
            return value
    cache_key = (_normalise_base_url(target.get("base_url")), _strip_model_decoration(target.get("model")))
    cached = _MODEL_METADATA_REGISTRY.get(cache_key)
    if isinstance(cached, Mapping):
        return cached
    return None


def register_model_metadata(base_url: Any, model: Any, metadata: Mapping[str, Any] | None) -> None:
    """既存 /models取得結果の推論metadataを共有registryへ登録する。

    APIキーやレスポンス本文は保存せず、呼び出し側が抽出したmetadataだけを
    登録する。設定ファイルへ保存するかどうかはconfig_manager側が決める。
    """

    if not isinstance(metadata, Mapping):
        return
    base_key = _normalise_base_url(base_url)
    model_key = _strip_model_decoration(model)
    if not base_key or not model_key:
        return
    _MODEL_METADATA_REGISTRY[(base_key, model_key)] = dict(metadata)


def _metadata_capabilities(target: Mapping[str, Any], transport: str) -> dict[str, Any] | None:
    """取得済み /models metadataから能力を読む。

    取得元の仕様には複数の形があるため、reasoning/supported_parametersの
    最小集合だけを扱う。任意のmetadataをそのままSDKへ流すことはしない。
    """

    metadata = _metadata(target)
    if not metadata:
        return None

    reasoning = metadata.get("reasoning") or metadata.get("reasoning_config")
    if not isinstance(reasoning, Mapping):
        reasoning = {}

    # OpenRouterの/​models metadataはsupported_effortsを省略すると「effort
    # 非対応」、nullならGatewayの全effort値を表す。空配列は明示的な非対応
    # として尊重し、supported_parametersから推測しない。
    marker = object()
    raw_efforts = metadata.get("supported_efforts", marker)
    if raw_efforts is marker:
        raw_efforts = reasoning.get("supported_efforts", marker)
    efforts: list[str] = []
    metadata_declares_reasoning = raw_efforts is not marker
    if raw_efforts is None:
        efforts = list(_OPENROUTER_GATEWAY_EFFORTS)
    elif isinstance(raw_efforts, str):
        efforts = [raw_efforts.strip().lower()]
    elif isinstance(raw_efforts, (list, tuple, set)):
        efforts = [str(item).strip().lower() for item in raw_efforts]
    efforts = [item for item in efforts if item and item not in {"default", "budget"}]

    budget_marker = metadata.get("supports_max_tokens", marker)
    if budget_marker is marker:
        budget_marker = reasoning.get("supports_max_tokens", marker)
    budget = bool(budget_marker) if budget_marker is not marker else False
    mandatory_marker = metadata.get("mandatory", marker)
    if mandatory_marker is marker:
        mandatory_marker = reasoning.get("mandatory", marker)
    if mandatory_marker is True:
        efforts = [effort for effort in efforts if effort != "none"]
    if not metadata_declares_reasoning and budget_marker is marker:
        # ``{"reasoning": {}}`` は取得結果が能力なしだった負のcache。
        # 旧registryや既知モデルfallbackを再利用しない。
        if "reasoning" in metadata or "reasoning_config" in metadata:
            return {
                "efforts": [],
                "budget": False,
                "transport": transport,
                "note": "取得済みmetadataで推論能力なしと確認されています。",
            }
        return None

    return {
        "efforts": list(dict.fromkeys(efforts)),
        "budget": budget,
        "transport": transport,
        "note": "取得済みモデルmetadataに基づく推論能力です。",
    }


def make_target(provider: Any, model: Any, base_url: Any = "", profile: Any = "") -> dict[str, str]:
    """推論設定の対象を作る。

    ``model``以外の値は設定値を保持し、モデル表示用のfavorite/注釈だけを
    除去する。OpenRouterの ``@preset/...`` や ``model@preset/...`` はそのまま残す。
    """

    return {
        "provider": str(provider or "").strip(),
        "model": _strip_model_decoration(model),
        "base_url": str(base_url or "").strip(),
        "profile": str(profile or "").strip(),
    }


def target_key(target: Mapping[str, Any]) -> str:
    """APIキー等の秘密を含めず、対象ごとの安定キーを返す。"""

    if not isinstance(target, Mapping):
        raise ValueError("target must be a mapping")
    canonical = {
        "provider": _normalise_provider(target.get("provider")),
        "model": _strip_model_decoration(target.get("model")),
        "base_url": _normalise_base_url(target.get("base_url")),
        "profile": str(target.get("profile") or "").strip(),
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "rr1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_capabilities(target: Mapping[str, Any]) -> dict[str, Any]:
    """対象の送信可能な推論値を返す。

    ``default`` と ``budget`` は ``efforts`` に含めない。未知の対象は
    API既定値だけを許可する。
    """

    if not isinstance(target, Mapping):
        return dict(_DEFAULT_CAPABILITIES)

    provider = _normalise_provider(target.get("provider"))
    model = _strip_model_decoration(target.get("model"))
    model_base = _model_base(model)
    host = _host(target.get("base_url"))

    is_official_openai = host == "api.openai.com" and provider in {"openai", "openai_official", "openaiofficial"}
    if is_official_openai:
        efforts = _OPENAI_EFFORTS.get(model_base)
        if efforts:
            return {
                "efforts": list(efforts),
                "budget": False,
                "transport": "openai_reasoning_effort",
                "note": "OpenAI公式Chat Completionsのreasoning_effortを使用します。",
            }
        metadata = _metadata_capabilities(target, "openai_reasoning_effort")
        if metadata:
            return metadata
        return dict(_DEFAULT_CAPABILITIES)

    if host == "openrouter.ai" or provider in {"openrouter", "open_router"} and host == "openrouter.ai":
        # 元モデルが明示されたpresetはmetadataで判定できる。元モデルなしの
        # @preset/slugだけは能力を推測しない。
        if model.lower().startswith("@preset/"):
            return dict(_DEFAULT_CAPABILITIES)
        metadata = _metadata_capabilities(target, "openrouter_reasoning")
        if metadata:
            return metadata
        # 既知のgatewayモデルだけは、取得済みmetadataが無い場合に限り
        # 元モデル名から控えめにfallbackする。preset名そのものは使わない。
        original_model = _model_base(model).rsplit("/", 1)[-1]
        known_efforts = _OPENAI_EFFORTS.get(original_model) or _XAI_EFFORTS.get(original_model)
        if known_efforts:
            return {
                "efforts": list(known_efforts),
                "budget": False,
                "transport": "openrouter_reasoning",
                "note": "既知モデルcatalogのfallbackです。",
            }
        return dict(_DEFAULT_CAPABILITIES)

    is_xai = host == "api.x.ai" and provider in {"openai", "xai", "grok"}
    if is_xai:
        efforts = _XAI_EFFORTS.get(model_base)
        if efforts:
            return {
                "efforts": list(efforts),
                "budget": False,
                "transport": "xai_reasoning_effort",
                "note": "xAI公式のreasoning_effort対応モデルだけを許可します。",
            }
        metadata = _metadata_capabilities(target, "xai_reasoning_effort")
        return metadata or dict(_DEFAULT_CAPABILITIES)

    is_anthropic = provider in {"anthropic", "claude"} or host == "api.anthropic.com"
    if is_anthropic:
        efforts = _ANTHROPIC_EFFORTS.get(model_base)
        if efforts:
            return {
                "efforts": list(efforts),
                "budget": model_base == "claude-opus-4-5",
                "transport": (
                    "anthropic_adaptive_effort"
                    if model_base in _ANTHROPIC_ADAPTIVE_EFFORT_MODELS
                    else "anthropic_effort"
                ),
                "note": "Anthropicの対応モデルにだけeffortを送信します。",
            }
        if model_base in _ANTHROPIC_MANUAL_THINKING_MODELS:
            return {
                "efforts": [],
                "budget": True,
                "transport": "anthropic_thinking",
                "note": "旧Claudeの手動thinking budgetを使用します。",
            }
        metadata = _metadata_capabilities(target, "anthropic_effort")
        return metadata or dict(_DEFAULT_CAPABILITIES)

    if provider in {"google", "gemini", "google_(gemini)"}:
        gemini_efforts = _GEMINI_EFFORTS_BY_MODEL.get(model_base)
        if gemini_efforts:
            return {
                "efforts": list(gemini_efforts),
                "budget": False,
                "transport": "gemini_thinking_level",
                "note": "既存Gemini thinking_level変換を使用します。",
            }
        return dict(_DEFAULT_CAPABILITIES)

    return dict(_DEFAULT_CAPABILITIES)


def validate_preference(target: Mapping[str, Any], pref: Mapping[str, Any] | str | None) -> dict[str, Any]:
    """保存済み推論設定を対象の能力に合わせて正規化する。

    不正値は呼び出し側でAPI送信せず安全に既定値へ戻せるよう、明確な
    ``ValueError`` として返す。返却辞書には送信契約に必要な値だけを残す。
    """

    if pref is None:
        pref = {}
    if isinstance(pref, str):
        pref = {"effort": pref}
    if not isinstance(pref, Mapping):
        raise ValueError("推論設定は辞書またはeffort文字列で指定してください")

    effort = pref.get("effort", "default")
    if not isinstance(effort, str):
        raise ValueError("effortは文字列で指定してください")
    effort = effort.strip().lower() or "default"

    capabilities = get_capabilities(target)
    allowed = set(capabilities.get("efforts") or [])
    if effort == "default":
        # 明示defaultは送信側で「SDK引数を省略する」ための値であり、
        # staleなbudget_tokensは落として確実に無送信にする。
        return {"effort": "default"}
    if effort == "budget":
        if not capabilities.get("budget"):
            raise ValueError("この対象では手動thinking budgetを利用できません")
        budget_tokens = pref.get("budget_tokens", 1024)
        if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens < 1024:
            raise ValueError("budget_tokensは1024以上の整数で指定してください")
        budget_limit = _ANTHROPIC_MANUAL_BUDGET_LIMITS.get(
            _model_base(_strip_model_decoration(target.get("model"))),
            1_000_000,
        )
        if budget_tokens > budget_limit:
            raise ValueError(f"budget_tokensが対応する予算上限({budget_limit})を超えています")
        return {"effort": "budget", "budget_tokens": budget_tokens}
    if effort not in allowed:
        raise ValueError(f"effort '{effort}' はこの対象で利用できません")

    result = {"effort": effort}
    budget_tokens = pref.get("budget_tokens")
    if budget_tokens is not None:
        if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens <= 0:
            raise ValueError("budget_tokensは正の整数で指定してください")
        if budget_tokens > 1_000_000:
            raise ValueError("budget_tokensが上限を超えています")
        # OpenRouterはeffortと同時にreasoning.max_tokensを受け取る。
        # 他transportへは無関係な予算値を渡さない。
        if capabilities.get("transport") == "openrouter_reasoning" and capabilities.get("budget"):
            result["budget_tokens"] = budget_tokens
    return result


def preference_to_kwargs(target: Mapping[str, Any], pref: Mapping[str, Any] | str | None) -> dict[str, Any]:
    """正規化済み設定をプロバイダ別のSDK kwargsへ変換する。"""

    normalized = validate_preference(target, pref)
    effort = normalized.get("effort", "default")
    if effort == "default":
        return {}
    transport = get_capabilities(target).get("transport")
    if transport in {"openai_reasoning_effort", "xai_reasoning_effort"}:
        return {"reasoning_effort": effort}
    if transport == "openrouter_reasoning":
        reasoning: dict[str, Any] = {} if effort == "budget" else {"effort": effort}
        if "budget_tokens" in normalized:
            reasoning["max_tokens"] = normalized["budget_tokens"]
        return {"extra_body": {"reasoning": reasoning}}
    if transport == "anthropic_adaptive_effort":
        if effort == "xhigh":
            return {
                "model_kwargs": {"output_config": {"effort": "xhigh"}},
                "thinking": {"type": "adaptive"},
            }
        return {"effort": effort, "thinking": {"type": "adaptive"}}
    if transport == "anthropic_effort" and effort == "budget":
        return {"thinking": {"type": "enabled", "budget_tokens": normalized["budget_tokens"]}}
    if transport == "anthropic_effort":
        if effort == "xhigh":
            # 現行ChatAnthropicの第一級effort Literalにはxhighが無い。
            # SDKがpayloadへ展開する正規のmodel_kwargs境界へ薄く渡す。
            return {"model_kwargs": {"output_config": {"effort": "xhigh"}}}
        return {"effort": effort}
    if transport == "anthropic_thinking" and effort == "budget":
        return {"thinking": {"type": "enabled", "budget_tokens": normalized["budget_tokens"]}}
    if transport == "gemini_thinking_level":
        return {"thinking_level": effort}
    return {}


__all__ = [
    "get_capabilities",
    "make_target",
    "preference_to_kwargs",
    "register_model_metadata",
    "target_key",
    "validate_preference",
]
