"""Approximate API pricing used for per-turn cost display.

Prices are approximate as of 2026-06 and are listed in USD per
1,000,000 tokens. Update this file when provider pricing changes.
"""

from __future__ import annotations

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


def get_model_price(model_name: str) -> Optional[dict]:
    normalized = normalize_model_name(model_name)
    if normalized in MODEL_PRICES:
        return MODEL_PRICES[normalized]

    # Claude API model IDs often append release dates. Keep the table compact
    # while still matching the corresponding family price.
    for key in sorted(MODEL_PRICES, key=len, reverse=True):
        if normalized.startswith(f"{key}-"):
            return MODEL_PRICES[key]
    return None


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
    """Return approximate turn cost and cache savings for supported models."""
    if not isinstance(usage, dict):
        return None

    model_name = usage.get("model_name") or usage.get("model")
    if not model_name:
        return None

    cache_mode = str(usage.get("cache_mode") or "")
    provider = infer_pricing_provider(str(model_name), cache_mode=cache_mode)
    model_price = get_model_price(str(model_name))
    multipliers = CACHE_MULTIPLIERS.get(provider or "")
    if not provider or not model_price or not multipliers:
        return None

    prompt_tokens = usage_int(usage, "prompt_tokens", "prompt", "input_tokens", "prompt_token_count")
    completion_tokens = usage_int(usage, "completion_tokens", "completion", "output_tokens", "candidates_token_count")
    cache_read_tokens = usage_int(usage, "cache_read_tokens")
    cache_creation_tokens = usage_int(usage, "cache_creation_tokens")
    total_input_tokens = usage_int(usage, "total_input_tokens", "cache_total_input_tokens")
    if not total_input_tokens:
        total_input_tokens = prompt_tokens or cache_read_tokens + cache_creation_tokens

    is_registration = bool(usage.get("cache_just_created")) or cache_creation_tokens > 0
    if provider == "gemini_explicit" and usage.get("cache_just_created") is True and cache_creation_tokens <= 0:
        cache_creation_tokens = cache_read_tokens or total_input_tokens
        cache_read_tokens = 0

    if provider == "anthropic":
        non_cached_tokens = max(0, total_input_tokens - cache_read_tokens - cache_creation_tokens)
    else:
        non_cached_tokens = max(0, prompt_tokens - cache_read_tokens - cache_creation_tokens)
        if not prompt_tokens and total_input_tokens:
            non_cached_tokens = max(0, total_input_tokens - cache_read_tokens - cache_creation_tokens)

    input_rate = float(model_price["input"]) / 1_000_000
    output_rate = float(model_price["output"]) / 1_000_000
    actual_input_cost = (
        non_cached_tokens * input_rate
        + cache_read_tokens * input_rate * float(multipliers["read"])
        + cache_creation_tokens * input_rate * float(multipliers["write"])
    )
    output_cost = completion_tokens * output_rate
    baseline_input_cost = (non_cached_tokens + cache_read_tokens + cache_creation_tokens) * input_rate
    savings = baseline_input_cost - actual_input_cost

    return {
        "cost": actual_input_cost + output_cost,
        "savings": savings,
        "is_registration": is_registration or savings < 0,
        "is_paid": bool(usage.get("is_paid", True)),
        "provider": provider,
    }


def format_usd_estimate(value: float) -> str:
    if value > 0 and value < 0.0001:
        return "<$0.0001"
    return f"≈${value:.4f}"
