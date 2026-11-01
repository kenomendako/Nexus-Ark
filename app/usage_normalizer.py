"""Provider usage extraction shared by the agent and the usage ledger.

LangChain adapters expose usage in more than one shape.  In particular,
``langchain-google-genai`` already folds Gemini thoughts into
``output_tokens``, while the native Google response keeps
``thoughts_token_count`` separate.  This module converts those shapes into a
small, provider-neutral dictionary without charging a token twice.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


_MISSING = object()


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(exclude_none=False)
        except TypeError:
            dumped = model_dump()
        except Exception:
            dumped = None
        if isinstance(dumped, Mapping):
            return dict(dumped)
    as_dict = getattr(value, "dict", None)
    if callable(as_dict):
        try:
            dumped = as_dict()
        except Exception:
            dumped = None
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return None


def _get(value: Any, key: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    if value is None:
        return default
    return getattr(value, key, default)


def _first_present(candidates: list[Any], *keys: str) -> tuple[bool, Any]:
    for candidate in candidates:
        for key in keys:
            value = _get(candidate, key)
            if value is not _MISSING and value is not None:
                return True, value
    return False, None


def _first_present_index(candidates: list[Any], *keys: str) -> tuple[bool, Any, int | None]:
    for index, candidate in enumerate(candidates):
        for key in keys:
            value = _get(candidate, key)
            if value is not _MISSING and value is not None:
                return True, value, index
    return False, None, None


def _nested_first_present(candidates: list[Any], parent_key: str, *keys: str) -> tuple[bool, Any]:
    for candidate in candidates:
        nested = _get(candidate, parent_key)
        if nested is _MISSING or nested is None:
            continue
        found, value = _first_present([nested], *keys)
        if found:
            return True, value
    return False, None


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _finite_number(value: Any, *, allow_negative: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or (result < 0 and not allow_negative):
        return None
    return result


def _response_metadata(response: Any) -> dict[str, Any]:
    metadata = _get(response, "response_metadata", {})
    mapped = _as_mapping(metadata)
    return mapped or {}


def usage_candidates(
    response: Any = None,
    *,
    usage: Any = None,
    response_metadata: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Return usage-shaped objects in precedence order.

    Counts normally come from ``usage_metadata``.  A provider may put cost
    only in the raw metadata, so all candidates are retained and each field
    is selected independently by :func:`normalize_usage`.
    """

    candidates: list[Any] = []

    def add(value: Any) -> None:
        if value is None or value is _MISSING:
            return
        if isinstance(value, Mapping) and not value:
            return
        if any(value is existing for existing in candidates):
            return
        candidates.append(value)

    add(usage)
    if response is not None:
        add(_get(response, "usage_metadata"))
    supplied_metadata = _as_mapping(response_metadata) if response_metadata is not None else None
    if supplied_metadata:
        add(supplied_metadata.get("token_usage"))
        add(supplied_metadata.get("usage"))

    if response is not None:
        metadata = _response_metadata(response)
        add(metadata.get("token_usage"))
        add(metadata.get("usage"))

        # ChatResult is the only place where langchain-openai retains the raw
        # OpenRouter usage object (including ``cost``) by default.
        llm_output = _get(response, "llm_output")
        llm_output_map = _as_mapping(llm_output)
        if llm_output_map:
            add(llm_output_map.get("token_usage"))
            add(llm_output_map.get("usage"))

    return candidates


def _raw_google_candidate(candidates: list[Any]) -> Any | None:
    """Find native Google usage, where thoughts are separate from candidates."""

    for candidate in candidates:
        has_candidates = _get(candidate, "candidates_token_count") is not _MISSING
        has_thoughts = _get(candidate, "thoughts_token_count") is not _MISSING
        if has_candidates or has_thoughts:
            # LangChain's normalized dict has output_tokens and nested
            # reasoning; a native google-genai UsageMetadata has the raw names.
            if _get(candidate, "output_tokens") is _MISSING:
                return candidate
    return None


def _metadata_value(response: Any, supplied_metadata: Mapping[str, Any] | None, key: str) -> Any:
    if supplied_metadata and key in supplied_metadata:
        return supplied_metadata[key]
    metadata = _response_metadata(response) if response is not None else {}
    return metadata.get(key, _MISSING)


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def normalize_usage(
    response: Any = None,
    *,
    usage: Any = None,
    response_metadata: Mapping[str, Any] | None = None,
    extra_usage: Mapping[str, Any] | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    """Normalize LangChain, native Google, and OpenRouter usage metadata.

    ``None`` means that a field was not reported.  A reported zero remains
    zero, which lets the UI distinguish a cache miss from missing telemetry.
    """

    candidates = usage_candidates(response, usage=usage, response_metadata=response_metadata)
    supplied_metadata = _as_mapping(response_metadata) if response_metadata is not None else None
    raw_google = _raw_google_candidate(candidates)

    result: dict[str, Any] = {}
    if model_name:
        result["model_name"] = model_name

    input_found, input_value, input_candidate_index = _first_present_index(
        candidates,
        "prompt_tokens",
        "prompt",
        "input_tokens",
        "prompt_token_count",
    )
    output_found, output_value = _first_present(
        candidates,
        "completion_tokens",
        "completion",
        "output_tokens",
        "candidates_token_count",
    )
    total_found, total_value = _first_present(candidates, "total_tokens", "total", "total_token_count")

    reasoning_found, reasoning_value = _first_present(
        candidates,
        "reasoning_tokens",
        "thoughts_token_count",
    )
    nested_reasoning_found, nested_reasoning_value = _nested_first_present(
        candidates,
        "output_token_details",
        "reasoning",
        "reasoning_tokens",
    )
    if not reasoning_found and nested_reasoning_found:
        reasoning_found, reasoning_value = True, nested_reasoning_value
    if not reasoning_found:
        nested_reasoning_found, nested_reasoning_value = _nested_first_present(
            candidates,
            "completion_tokens_details",
            "reasoning_tokens",
            "reasoning",
        )
        if nested_reasoning_found:
            reasoning_found, reasoning_value = True, nested_reasoning_value

    if raw_google is not None:
        # google-genai reports candidates and thoughts separately.  Do not use
        # this branch for LangChain's normalized output_tokens shape: that
        # value already includes thoughts_token_count.
        candidates_found, candidates_value = _first_present([raw_google], "candidates_token_count")
        thoughts_found, thoughts_value = _first_present([raw_google], "thoughts_token_count")
        if candidates_found:
            output_found = True
            output_value = _integer(candidates_value) + (_integer(thoughts_value) if thoughts_found else 0)
        if thoughts_found:
            reasoning_found, reasoning_value = True, thoughts_value
        prompt_found, prompt_value = _first_present([raw_google], "prompt_token_count")
        tool_found, tool_value = _first_present([raw_google], "tool_use_prompt_token_count")
        if prompt_found:
            input_found = True
            input_value = _integer(prompt_value) + (_integer(tool_value) if tool_found else 0)

    if input_found:
        result["prompt_tokens"] = _integer(input_value)
        result["input_tokens"] = result["prompt_tokens"]
    if output_found:
        result["completion_tokens"] = _integer(output_value)
        result["output_tokens"] = result["completion_tokens"]
    if total_found:
        result["total_tokens"] = _integer(total_value)
    elif input_found or output_found:
        result["total_tokens"] = result.get("prompt_tokens", 0) + result.get("completion_tokens", 0)
    if reasoning_found:
        result["reasoning_tokens"] = _integer(reasoning_value)

    cache_read_found, cache_read_value = _first_present(
        candidates,
        "cache_read_tokens",
        "cached_content_token_count",
        "cache_read_input_tokens",
        "cache_read",
    )
    nested_cache_read_found, nested_cache_read_value = _nested_first_present(
        candidates,
        "input_token_details",
        "cache_read",
        "cached_tokens",
        "cache_read_tokens",
    )
    if not cache_read_found:
        cache_read_found, cache_read_value = nested_cache_read_found, nested_cache_read_value
    if not cache_read_found:
        nested_cache_read_found, nested_cache_read_value = _nested_first_present(
            candidates,
            "prompt_tokens_details",
            "cached_tokens",
            "cache_read_tokens",
        )
        cache_read_found, cache_read_value = nested_cache_read_found, nested_cache_read_value
    if cache_read_found:
        result["cache_read_tokens"] = _integer(cache_read_value)

    cache_write_found, cache_write_value = _first_present(
        candidates,
        "cache_creation_tokens",
        "cache_creation_input_tokens",
        "cache_write_input_tokens",
        "cache_write_tokens",
    )
    nested_cache_write_found, nested_cache_write_value = _nested_first_present(
        candidates,
        "input_token_details",
        "cache_creation",
        "cache_write",
        "cache_creation_tokens",
        "cache_write_tokens",
    )
    if not cache_write_found:
        cache_write_found, cache_write_value = nested_cache_write_found, nested_cache_write_value
    if not cache_write_found:
        nested_cache_write_found, nested_cache_write_value = _nested_first_present(
            candidates,
            "prompt_tokens_details",
            "cache_write_tokens",
            "cache_creation_tokens",
        )
        cache_write_found, cache_write_value = nested_cache_write_found, nested_cache_write_value
    if cache_write_found:
        result["cache_creation_tokens"] = _integer(cache_write_value)

    # Anthropic reports cache read/write outside prompt_tokens.  Google,
    # OpenAI, and OpenRouter prompt counts already include cached input.
    input_candidate = (
        candidates[input_candidate_index]
        if input_candidate_index is not None and input_candidate_index < len(candidates)
        else None
    )
    normalized_usage = _get(response, "usage_metadata") if response is not None else _MISSING
    # langchain-anthropic's UsageMetadata.input_tokens already includes cache
    # read/write.  Raw Anthropic usage keeps those fields separate, so only
    # the candidate that supplied input_tokens decides whether to add them.
    anthropic_cache_shape = (
        input_candidate is not None
        and input_candidate is not normalized_usage
        and any(
            _get(input_candidate, key) is not _MISSING
            for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
        )
    )
    if input_found and (cache_read_found or cache_write_found):
        result["total_input_tokens"] = (
            result["prompt_tokens"] + result.get("cache_read_tokens", 0) + result.get("cache_creation_tokens", 0)
            if anthropic_cache_shape
            else result["prompt_tokens"]
        )
        result["cache_total_input_tokens"] = result["total_input_tokens"]

    # OpenRouter and a few compatible gateways report charged USD in usage.
    # Presence is significant: a provider-reported zero is authoritative.
    reported_cost_found, reported_cost_value = _first_present(
        candidates,
        "reported_cost",
        "cost",
        "provider_reported_cost_usd",
    )
    if not reported_cost_found:
        cost_details_found, cost_details = _first_present(candidates, "cost_details")
        if cost_details_found:
            reported_cost_found, reported_cost_value = _first_present(
                [cost_details], "reported_cost", "cost", "total_cost"
            )
    reported_cost = _finite_number(reported_cost_value) if reported_cost_found else None
    if reported_cost is not None:
        result["reported_cost"] = reported_cost
        result["provider_reported_cost_usd"] = reported_cost
        # Keep the provider field visible to callers that use ``usage.cost``.
        result["cost"] = reported_cost

    discount_found, discount_value = _first_present(
        candidates,
        "reported_cache_discount",
        "cache_discount",
    )
    if not discount_found:
        _, cost_details = _first_present(candidates, "cost_details")
        if cost_details is not None:
            discount_found, discount_value = _first_present([cost_details], "cache_discount")
    discount = _finite_number(discount_value, allow_negative=True) if discount_found else None
    if discount is not None:
        result["reported_cache_discount"] = discount

    # Adapter/factory metadata is deliberately applied last for routing and
    # billing identity.  Raw cost above remains intact unless an explicit
    # reported_cost override was provided.
    metadata_backend = _metadata_value(response, supplied_metadata, "billing_backend")
    if metadata_backend is not _MISSING and metadata_backend is not None:
        result["billing_backend"] = str(metadata_backend)
    metadata_location = _metadata_value(response, supplied_metadata, "billing_location")
    if metadata_location is not _MISSING and metadata_location is not None:
        result["billing_location"] = str(metadata_location)
    metadata_date = _metadata_value(response, supplied_metadata, "pricing_date")
    if metadata_date is not _MISSING and metadata_date is not None:
        result["pricing_date"] = str(metadata_date)

    if extra_usage:
        for key, value in extra_usage.items():
            if value is not None:
                result[key] = value

    if "pricing_date" not in result and (reported_cost is not None or result.get("billing_backend")):
        result["pricing_date"] = _today_iso()

    has_usage = bool(candidates)
    has_metrics = input_found or output_found or total_found or reasoning_found or cache_read_found or cache_write_found or reported_cost is not None
    result["usage_status"] = "reported" if has_metrics else "missing"
    if not has_usage and not has_metrics:
        result["usage_status"] = "missing"
    return result
