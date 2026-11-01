"""Approximate API pricing used for per-turn cost display.

Prices are in USD per 1,000,000 tokens. New Gemini prices were verified
2026-10-19; dated rates and billing routes must not be inferred from model families.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import math
from typing import Optional


MODEL_PRICES = {
    # Anthropic Claude API, first-party global pricing.
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    "claude-mythos-5": {"input": 10.0, "output": 50.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    # Google Gemini API, Standard tier text/image/video prices where applicable.
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-3.1-flash-lite-preview": {"input": 0.25, "output": 1.50},
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-2.5-flash-lite-preview-09-2025": {"input": 0.10, "output": 0.40},
    # Free Gemma model surfaced by OpenRouter/Gemini-compatible settings.
    "gemma-3-27b-it": {"input": 0.0, "output": 0.0},
}

# Approximate standard per-image prices (USD), verified 2026-07-10.
# Representative size/quality: Gemini standard 1K (Gemini 3 Pro: 1K/2K),
# OpenAI 1024x1024 medium, DALL-E standard 1024x1024.
# Sources:
# - https://ai.google.dev/gemini-api/docs/pricing
# - https://developers.openai.com/api/docs/models/gpt-image-1
# - https://developers.openai.com/api/docs/models/gpt-image-1.5
# - https://developers.openai.com/api/docs/models/gpt-image-1-mini
# - https://developers.openai.com/api/docs/models/dall-e-3
IMAGE_PRICES = {
    "gemini-2.5-flash-image": 0.039,
    "gemini-3.1-flash-lite-image": 0.0336,
    "gemini-3.1-flash-image": 0.067,
    "gemini-3-pro-image": 0.134,
    "gpt-image-1-mini": 0.011,
    "gpt-image-1.5": 0.034,
    "gpt-image-1": 0.042,
    "dall-e-3": 0.04,
    "dall-e-2": 0.02,
}

CACHE_MULTIPLIERS = {
    "anthropic": {"read": 0.1, "write": 1.25},
    "openai": {"read": 0.5, "write": 0.0},
    "gemini_implicit": {"read": 0.1, "write": 0.0},
    "gemini_explicit": {"read": 0.1, "write": 1.0},
}

GEMINI_EXPLICIT_STORAGE_PER_MTOKEN_HOUR = 1.0


def normalize_model_name(model_name: str) -> str:
    normalized = str(model_name or "").strip().lower()
    if normalized.startswith("models/"):
        normalized = normalized.split("/", 1)[1]
    if normalized.startswith("google/"):
        normalized = normalized.split("/", 1)[1]
    if normalized.endswith(":free"):
        normalized = normalized[:-5]
    return normalized


def infer_pricing_provider(model_name: str, cache_mode: str = "") -> Optional[str]:
    if cache_mode == "gemini_explicit":
        return "gemini_explicit"

    normalized = normalize_model_name(model_name)
    if normalized.startswith("claude"):
        return "anthropic"
    if normalized.startswith(("gemini", "gemma")):
        return "gemini_implicit"
    if normalized.startswith(("gpt", "o1", "o3", "o4", "o5")) or normalized.startswith("o"):
        return "openai"
    return None


# Standard online inference only. Batch/Flex/Priority are not sent by the app.
# Sources (verified 2026-10-19):
# https://ai.google.dev/gemini-api/docs/pricing
# https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing
# https://openrouter.ai/google/gemini-3.8-flash
# https://openrouter.ai/google/gemini-4-argon
GEMINI_DATED_PRICES = {
    "gemini-3.8-flash": {
        "start": date(2026, 9, 2), "change": date(2027, 1, 1),
        "intro": {"input": 0.75, "output": 3.75, "cache_read": 0.075, "storage": 0.50},
        "standard": {"input": 1.50, "output": 7.50, "cache_read": 0.15, "storage": 1.0},
    },
    "gemini-4-argon": {
        "start": date(2026, 10, 13), "change": date(2026, 12, 1),
        "intro": {"input": 2.0, "output": 10.0, "cache_read": 0.10},
        "standard": {"input": 4.0, "output": 20.0, "cache_read": 0.20},
    },
}


def _pricing_date(value=None) -> date | None:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def get_model_price(model_name: str, *, billing_backend: str = "", billing_location: str = "",
                    pricing_date=None) -> Optional[dict]:
    normalized = normalize_model_name(model_name)
    backend = billing_backend or ("openrouter" if str(model_name).strip().lower().startswith("google/") else "developer")
    # Only this explicitly registered free route has a verified zero rate.
    if str(model_name).strip().lower() == "google/gemma-3-27b-it:free" and backend in {"developer", "openrouter"}:
        return {"input": 0.0, "output": 0.0}
    if ":" in str(model_name):
        return None
    dated = GEMINI_DATED_PRICES.get(normalized)
    if dated:
        day = _pricing_date(pricing_date)
        if day is None or day < dated["start"]:
            return None
        if backend not in {"developer", "vertex", "openrouter"}:
            return None
        if backend == "vertex" and not billing_location:
            return None
        # OpenRouter routing/discounts may change independently of Google.
        if backend == "openrouter" and day >= dated["change"]:
            return None
        # Vertex promotion is issued as later credits, not a lower usage charge.
        # Estimate gross list spend, without assuming the account receives credits.
        rates = dict(dated["standard"] if backend == "vertex" or day >= dated["change"] else dated["intro"])
        if backend == "vertex" and billing_location != "global":
            rates = {key: value * 1.1 for key, value in rates.items()}
        return rates
    if backend not in {"developer", "anthropic", "openai_official"}:
        return None
    if normalized in MODEL_PRICES:
        return MODEL_PRICES[normalized]
    # Explicit aliases only; a well-formed but unregistered date is not evidence
    # of a model's price. Source: https://platform.claude.com/docs/en/models/sonnet-4-5/overview
    aliases = {"claude-sonnet-4-5-20250929": "claude-sonnet-4-5"}
    alias = aliases.get(normalized)
    return MODEL_PRICES.get(alias) if alias else None



def get_image_price(model_name: str) -> Optional[float]:
    """Return a representative per-image price for a registered image model."""
    normalized = normalize_model_name(model_name)
    for pattern in sorted(IMAGE_PRICES, key=len, reverse=True):
        if normalized == pattern or normalized.startswith(f"{pattern}-"):
            return float(IMAGE_PRICES[pattern])
    return None


def usage_int(usage: dict, *keys: str) -> int:
    for key in keys:
        try:
            value = usage.get(key)
        except AttributeError:
            value = None
        if value is None:
            continue
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def estimate_turn_cost(usage: dict) -> Optional[dict]:
    """Estimate verified usage; unavailable routes/rates/usage return None."""
    if not isinstance(usage, dict):
        return None
    model_name = usage.get("model_name") or usage.get("model")
    if not model_name:
        return None
    backend = str(usage.get("billing_backend") or "")
    cache_mode = str(usage.get("cache_mode") or "")
    provider = infer_pricing_provider(str(model_name), cache_mode=cache_mode)
    if backend == "openrouter":
        # The charged OpenRouter cost already includes routing, caching and fees.
        reported = usage.get("reported_cost")
        if isinstance(reported, (int, float)) and not isinstance(reported, bool) and math.isfinite(reported) and reported >= 0:
            discount = usage.get("reported_cache_discount")
            savings = float(discount) if isinstance(discount, (int, float)) and math.isfinite(discount) else 0.0
            return {"cost": float(reported), "savings": savings, "is_registration": savings < 0,
                    "is_paid": True, "provider": "openrouter", "cost_estimate_basis": "provider_reported"}
    model_price = get_model_price(str(model_name), billing_backend=backend,
                                  billing_location=str(usage.get("billing_location") or ""),
                                  pricing_date=usage.get("pricing_date"))
    multipliers = CACHE_MULTIPLIERS.get(provider or "")
    if not provider or not model_price or not multipliers:
        return None
    if usage.get("usage_status") == "missing" or not any(key in usage for key in (
        "prompt_tokens", "prompt", "input_tokens", "prompt_token_count", "completion_tokens", "completion", "output_tokens", "candidates_token_count"
    )):
        return None
    if usage.get("pricing_tier", "standard") != "standard":
        return None

    prompt_tokens = usage_int(usage, "prompt_tokens", "prompt", "input_tokens", "prompt_token_count")
    completion_tokens = usage_int(usage, "completion_tokens", "completion", "output_tokens", "candidates_token_count")
    cache_read_tokens = usage_int(usage, "cache_read_tokens")
    cache_creation_tokens = usage_int(usage, "cache_creation_tokens")
    if min(prompt_tokens, completion_tokens, cache_read_tokens, cache_creation_tokens) < 0:
        return None
    total_input_tokens = usage_int(usage, "total_input_tokens", "cache_total_input_tokens") or prompt_tokens
    is_gemini = provider.startswith("gemini")
    if is_gemini and cache_read_tokens > total_input_tokens:
        return None
    if normalize_model_name(str(model_name)) == "gemini-4-argon" and cache_mode == "gemini_explicit":
        return None
    if backend == "openrouter" and cache_creation_tokens:
        # Per-provider write/storage semantics cannot be inferred from read rates.
        return None
    is_registration = bool(usage.get("cache_just_created")) or cache_creation_tokens > 0
    if provider == "anthropic":
        non_cached_tokens = max(0, total_input_tokens - cache_read_tokens - cache_creation_tokens)
    elif is_gemini:
        # Google prompt count includes cached tokens. A newly created explicit
        # cache can be read in the same request; do not turn that read into a write.
        non_cached_tokens = max(0, total_input_tokens - cache_read_tokens)
        cache_creation_tokens = 0  # storage is separate; not a second input charge
    else:
        non_cached_tokens = max(0, total_input_tokens - cache_read_tokens - cache_creation_tokens)

    input_rate = float(model_price["input"]) / 1_000_000
    output_rate = float(model_price["output"]) / 1_000_000
    read_rate = float(model_price.get("cache_read", model_price["input"] * multipliers["read"])) / 1_000_000
    actual_input_cost = (non_cached_tokens * input_rate + cache_read_tokens * read_rate
                         + cache_creation_tokens * input_rate * float(multipliers["write"]))
    output_cost = completion_tokens * output_rate
    baseline_input_cost = (non_cached_tokens + cache_read_tokens + cache_creation_tokens) * input_rate
    savings = baseline_input_cost - actual_input_cost
    return {
        "cost": actual_input_cost + output_cost, "savings": savings,
        "is_registration": is_registration or savings < 0,
        "is_paid": True if backend in {"vertex", "openrouter"} or normalize_model_name(str(model_name)) == "gemini-4-argon" else bool(usage.get("is_paid", True)),
        "provider": backend if backend in {"vertex", "openrouter"} else provider,
        "cost_estimate_basis": "vertex_list_before_credits" if backend == "vertex" else "catalog",
        "storage_excluded": provider == "gemini_explicit",
    }


def format_usd_estimate(value: float) -> str:
    if value > 0 and value < 0.0001:
        return "<$0.0001"
    return f"≈${value:.4f}"
