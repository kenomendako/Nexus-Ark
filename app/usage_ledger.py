"""Approximate API usage ledger.

One JSONL line represents one billable main-agent LLM call. The ledger stores
derived cost summaries only, not prompts or responses.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from filelock import FileLock

import pricing
from usage_normalizer import normalize_usage


USAGE_LOG_DIR = Path("usage_logs")
SUPPORTED_PERIODS = {"today", "week", "month"}
PERIOD_LABELS = {
    "today": "今日",
    "week": "今週",
    "month": "今月",
}
SOURCE_LABELS = {
    "chat": "本処理",
    "autonomous": "自律",
    "internal": "内部処理",
    "delegation": "委任",
    "image": "画像生成",
    "travel": "お出かけ",
}


def _now() -> datetime:
    return datetime.now().astimezone()


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt


def _ledger_path(dt: datetime) -> Path:
    dt = _ensure_aware(dt)
    return USAGE_LOG_DIR / f"usage_{dt.strftime('%Y-%m')}.jsonl"


def _month_starts_between(start: datetime, end: datetime) -> Iterable[datetime]:
    cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_month = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor <= end_month:
        yield cursor
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)


def _period_start(period: str, now: datetime | None = None) -> datetime:
    current = _ensure_aware(now or _now())
    if period == "today":
        return current.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        today_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return today_start - timedelta(days=today_start.weekday())
    if period == "month":
        return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"Unsupported usage ledger period: {period}")


def _response_usage_dict(response) -> dict:
    usage = getattr(response, "usage_metadata", None)
    if usage:
        return dict(usage) if isinstance(usage, dict) else {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
        }

    metadata = getattr(response, "response_metadata", None) or {}
    if isinstance(metadata, dict):
        raw = metadata.get("usage") or metadata.get("token_usage") or {}
        if isinstance(raw, dict):
            return raw
    return {}


def _is_paid_for_usage(provider: str, api_key_name: str = "", explicit_value: object = None) -> bool:
    if explicit_value is not None:
        return bool(explicit_value)
    if provider in {"anthropic", "openai", "openrouter", "vertex", "openai_compatible"}:
        return True
    if provider in {"gemini_implicit", "gemini_explicit"}:
        if not api_key_name or api_key_name == "Unknown":
            return True
        try:
            import config_manager
            return config_manager.is_paid_api_key_name(api_key_name)
        except Exception:
            return True
    return True


def record_turn(usage: dict, source: str = "chat", api_key_name: str = "") -> bool:
    """Append one usage entry, retaining unknown prices for later aggregation."""
    try:
        if not isinstance(usage, dict):
            return False
        has_usage = any(
            key in usage
            for key in (
                "prompt_tokens", "prompt", "input_tokens", "prompt_token_count",
                "completion_tokens", "completion", "output_tokens", "candidates_token_count",
                "total_tokens", "total_token_count", "reported_cost", "cost",
            )
        )
        allow_missing = bool(usage.get("record_missing_usage"))
        if (usage.get("usage_status") == "missing" and not allow_missing) or (not has_usage and not allow_missing):
            return False

        recorded_at = _now()
        pricing_date = usage.get("pricing_date") or recorded_at.astimezone(timezone.utc).date().isoformat()
        pricing_usage = dict(usage)
        pricing_usage["pricing_date"] = pricing_date
        estimate = pricing.estimate_turn_cost(pricing_usage)
        known_price = bool(estimate)
        backend = str(usage.get("billing_backend") or "").strip().lower()
        is_argon = pricing.normalize_model_name(
            str(usage.get("model_name") or usage.get("model") or "")
        ) == "gemini-4-argon"
        if known_price:
            provider = str(estimate.get("provider") or backend or "unknown")
            if is_argon or backend in {"vertex", "openrouter"}:
                is_paid = True
            else:
                is_paid = _is_paid_for_usage(
                    provider,
                    api_key_name,
                    usage.get("is_paid") if "is_paid" in usage else None,
                )
        else:
            provider = str(
                usage.get("provider")
                or backend
                or pricing.infer_pricing_provider(
                    str(usage.get("model_name") or usage.get("model") or ""),
                    cache_mode=str(usage.get("cache_mode") or ""),
                )
                or "unknown"
            )
            explicit_paid = usage.get("is_paid") if "is_paid" in usage else None
            if explicit_paid is None:
                is_paid = False if backend == "local" else _is_paid_for_usage(provider, api_key_name)
            else:
                is_paid = bool(explicit_paid)

        model_name = usage.get("model_name") or usage.get("model") or ""
        cache_status = str(usage.get("cache_status") or "").strip().lower()
        if cache_status not in {"reported", "missing"}:
            cache_status = "reported" if (
                "cache_read_tokens" in usage or "cache_creation_tokens" in usage
            ) else "missing"
        cache_reported = cache_status == "reported"
        entry = {
            "ts": recorded_at.isoformat(timespec="seconds"),
            "provider": provider,
            "source": source or "chat",
            "is_paid": is_paid,
            "model": pricing.normalize_model_name(str(model_name)),
            "prompt_tokens": pricing.usage_int(usage, "prompt_tokens", "prompt", "input_tokens", "prompt_token_count"),
            "completion_tokens": pricing.usage_int(usage, "completion_tokens", "completion", "output_tokens", "candidates_token_count"),
            "reasoning_tokens": (
                pricing.usage_int(usage, "reasoning_tokens")
                if "reasoning_tokens" in usage else None
            ),
            "cache_read_tokens": usage.get("cache_read_tokens") if cache_reported else None,
            "cache_creation_tokens": usage.get("cache_creation_tokens") if cache_reported else None,
            "cache_status": cache_status,
            "cost": (float(estimate.get("cost")) if known_price and estimate.get("cost") is not None else None),
            "savings": float(estimate.get("savings") or 0.0) if known_price else 0.0,
            "is_registration": bool(estimate.get("is_registration")) if known_price else False,
            "unknown_price": not known_price,
            "usage_status": str(usage.get("usage_status") or "reported"),
            "pricing_date": str(pricing_date),
            "cost_estimate_basis": (
                str(
                    estimate.get("cost_estimate_basis")
                    or ("vertex_list_before_credits" if backend == "vertex" else "catalog")
                )
                if known_price else "unknown_price"
            ),
        }
        if backend:
            entry["billing_backend"] = backend
        if usage.get("billing_location"):
            entry["billing_location"] = str(usage.get("billing_location"))
        if usage.get("reported_cost") is not None:
            entry["provider_reported_cost_usd"] = usage.get("reported_cost")
        if usage.get("reported_cache_discount") is not None:
            entry["reported_cache_discount"] = usage.get("reported_cache_discount")
        if backend == "vertex":
            entry["connection_id"] = str(usage.get("connection_id") or "")
            entry["is_paid"] = True

        path = _ledger_path(recorded_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path) + ".lock"):
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception as e:
        print(f"⚠️ [UsageLedger] 記録をスキップ: {e}")
        return False


def record_response(
    response,
    model_name: str,
    source: str,
    *,
    extra_usage: dict | None = None,
    api_key_name: str = "",
) -> bool:
    """Extract LangChain response usage and append it to the ledger."""
    try:
        usage = normalize_usage(response, extra_usage=extra_usage, model_name=model_name)
        if usage.get("usage_status") == "missing":
            # Keep the attempted call visible without pretending it was free or
            # zero-token usage.  The ledger marks it unknown_price/missing.
            usage["record_missing_usage"] = True
        elif not any(
            key in usage
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reported_cost")
        ):
            return False
        return record_turn(usage, source=source, api_key_name=api_key_name)
    except Exception as e:
        print(f"⚠️ [UsageLedger] 応答記録をスキップ: {e}")
        return False


def record_image_generation(
    provider: str,
    model_name: str,
    *,
    image_count: int = 1,
    api_key_name: str = "",
    is_paid: bool | None = None,
) -> bool:
    """Append one image-generation entry, including zero-cost unknown models."""
    try:
        count = max(0, int(image_count or 0))
        if count <= 0 or not model_name:
            return False

        raw_provider = str(provider or "unknown").strip().lower()
        provider_name = f"{raw_provider}_image"
        if raw_provider in {"pollinations", "huggingface"}:
            is_paid = False
        elif is_paid is None:
            if raw_provider == "gemini":
                is_paid = _is_paid_for_usage("gemini_implicit", api_key_name)
            else:
                is_paid = True

        unit_price = pricing.get_image_price(model_name)
        # 単価未登録モデルと無料プロバイダは、金額を盛らず回数だけ記録する。
        # Geminiの無料キーなどは、既存テキスト台帳と同じく有料換算の参考額へ回す。
        cost = (
            float(unit_price) * count
            if unit_price is not None and raw_provider not in {"pollinations", "huggingface"}
            else 0.0
        )
        recorded_at = _now()
        entry = {
            "ts": recorded_at.isoformat(timespec="seconds"),
            "provider": provider_name,
            "source": "image",
            "is_paid": bool(is_paid),
            "model": pricing.normalize_model_name(model_name),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "cost": cost,
            "savings": 0.0,
            "is_registration": False,
            "units": count,
        }

        path = _ledger_path(recorded_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path) + ".lock"):
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception as e:
        print(f"⚠️ [UsageLedger] 画像生成記録をスキップ: {e}")
        return False


def import_travel_receipts(receipts: Iterable[dict], travel_session_id: str, room_name: str) -> dict:
    """署名検証済みのLite帰還receiptを、receipt_id単位で冪等に取り込む。"""
    normalized: list[tuple[datetime, dict]] = []
    for receipt in receipts:
        receipt_id = str(receipt.get("receipt_id") or "")
        occurred_at = _parse_ts(receipt.get("occurred_at"))
        if not receipt_id or occurred_at is None:
            raise ValueError("Lite travel receipt is missing id or occurred_at")
        known_cost = receipt.get("estimated_cost_usd")
        estimate_status = str(receipt.get("estimate_status") or "unknown")
        is_known = known_cost is not None and estimate_status == "estimated"
        entry = {
            "ts": occurred_at.isoformat(timespec="seconds"),
            "provider": str(receipt.get("provider") or "unknown"),
            "gateway": receipt.get("gateway"),
            "source": "travel",
            "is_paid": True,
            "model": pricing.normalize_model_name(
                str(receipt.get("model_resolved") or receipt.get("model_requested") or "unknown")
            ),
            "model_requested": receipt.get("model_requested"),
            "model_resolved": receipt.get("model_resolved"),
            "upstream_provider": receipt.get("upstream_provider"),
            "prompt_tokens": int(receipt.get("input_tokens") or 0),
            "completion_tokens": int(receipt.get("output_tokens") or 0),
            "cache_read_tokens": int(receipt.get("cache_read_tokens") or 0),
            "cache_creation_tokens": int(receipt.get("cache_creation_tokens") or 0),
            "cost": float(known_cost) if is_known else None,
            "savings": float(receipt.get("estimated_savings_usd") or 0.0) if is_known else None,
            "is_registration": False,
            "unknown_price": not is_known,
            "estimate_status": estimate_status,
            "pricing_version": receipt.get("pricing_version"),
            "cost_basis": receipt.get("cost_basis"),
            "provider_reported_cost_usd": receipt.get("provider_reported_cost_usd"),
            "estimated_cost_usd": receipt.get("estimated_cost_usd"),
            "estimated_savings_usd": receipt.get("estimated_savings_usd"),
            "input_cost_usd": receipt.get("input_cost_usd"),
            "output_cost_usd": receipt.get("output_cost_usd"),
            "cache_read_cost_usd": receipt.get("cache_read_cost_usd"),
            "cache_creation_cost_usd": receipt.get("cache_creation_cost_usd"),
            "cache_storage_cost_usd": receipt.get("cache_storage_cost_usd"),
            "unknown_reason": receipt.get("unknown_reason"),
            "usage_status": receipt.get("usage_status"),
            "receipt_id": receipt_id,
            "event_id": str(receipt.get("event_id") or ""),
            "travel_session_id": travel_session_id,
            "persona_id": room_name,
            "credential_profile_id": receipt.get("credential_profile_id"),
            "route_epoch": int(receipt.get("route_epoch") or 0),
            "cache_status": str(receipt.get("cache_status") or "unreported"),
            "cache_ttl_seconds": receipt.get("cache_ttl_seconds"),
            "cache_creation_5m_tokens": receipt.get("cache_creation_5m_tokens"),
            "cache_creation_1h_tokens": receipt.get("cache_creation_1h_tokens"),
        }
        normalized.append((occurred_at, entry))

    USAGE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    imported = 0
    duplicate = 0
    with FileLock(str(USAGE_LOG_DIR / ".travel-import.lock")):
        existing_ids: set[str] = set()
        for path in USAGE_LOG_DIR.glob("usage_*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        existing_id = json.loads(line).get("receipt_id")
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if existing_id:
                        existing_ids.add(str(existing_id))
            except OSError:
                continue

        by_path: dict[Path, list[dict]] = {}
        for occurred_at, entry in normalized:
            if entry["receipt_id"] in existing_ids:
                duplicate += 1
                continue
            existing_ids.add(entry["receipt_id"])
            by_path.setdefault(_ledger_path(occurred_at), []).append(entry)
            imported += 1
        for path, entries in by_path.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(path) + ".lock"):
                with path.open("a", encoding="utf-8") as stream:
                    for entry in entries:
                        stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return {"imported": imported, "duplicate": duplicate}


def _empty_aggregate(period: str) -> dict:
    return {
        "by_model": {},
        "by_provider": {},
        "by_source": {},
        "total_cost": 0.0,
        "total_savings": 0.0,
        "total_cost_paid": 0.0,
        "total_savings_paid": 0.0,
        "total_cost_free": 0.0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_reasoning_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_creation_tokens": 0,
        "count": 0,
        "unknown_price_count": 0,
        "period_label": PERIOD_LABELS.get(period, period),
    }


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return _ensure_aware(parsed)


def _add_amount(target: dict, key: str, value: float) -> None:
    target[key] = float(target.get(key) or 0.0) + float(value or 0.0)


def aggregate(period: str) -> dict:
    """Aggregate approximate usage for today/week/month."""
    if period not in SUPPORTED_PERIODS:
        raise ValueError(f"Unsupported usage ledger period: {period}")

    now = _now()
    start = _period_start(period, now)
    result = _empty_aggregate(period)

    for month_start in _month_starts_between(start, now):
        path = _ledger_path(month_start)
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue

        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = _parse_ts(entry.get("ts"))
            if not ts or ts < start or ts > now:
                continue

            model = str(entry.get("model") or "unknown")
            provider = str(entry.get("provider") or "unknown")
            source = str(entry.get("source") or "chat")
            is_paid = bool(entry.get("is_paid", True))
            cost = float(entry.get("cost") or 0.0)
            savings = float(entry.get("savings") or 0.0)
            prompt_tokens = int(entry.get("prompt_tokens") or 0)
            completion_tokens = int(entry.get("completion_tokens") or 0)
            reasoning_tokens = int(entry.get("reasoning_tokens") or 0)
            cache_read_tokens = int(entry.get("cache_read_tokens") or 0)
            cache_creation_tokens = int(entry.get("cache_creation_tokens") or 0)
            units = max(1, int(entry.get("units") or 1))
            unknown_price = bool(entry.get("unknown_price", False))

            if is_paid:
                model_bucket = result["by_model"].setdefault(
                    model,
                    {
                        "provider": provider,
                        "cost": 0.0,
                        "savings": 0.0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "reasoning_tokens": 0,
                        "cache_read_tokens": 0,
                        "cache_creation_tokens": 0,
                        "count": 0,
                        "unknown_price_count": 0,
                    },
                )
                if model_bucket.get("provider") not in {None, "", provider}:
                    model_bucket["provider"] = "mixed"
                _add_amount(model_bucket, "cost", cost)
                _add_amount(model_bucket, "savings", savings)
                model_bucket["prompt_tokens"] = int(model_bucket.get("prompt_tokens") or 0) + prompt_tokens
                model_bucket["completion_tokens"] = int(model_bucket.get("completion_tokens") or 0) + completion_tokens
                model_bucket["reasoning_tokens"] = int(model_bucket.get("reasoning_tokens") or 0) + reasoning_tokens
                model_bucket["cache_read_tokens"] = int(model_bucket.get("cache_read_tokens") or 0) + cache_read_tokens
                model_bucket["cache_creation_tokens"] = int(model_bucket.get("cache_creation_tokens") or 0) + cache_creation_tokens
                model_bucket["count"] = int(model_bucket.get("count") or 0) + units
                if unknown_price:
                    model_bucket["unknown_price_count"] = int(model_bucket.get("unknown_price_count") or 0) + units

                provider_bucket = result["by_provider"].setdefault(
                    provider,
                    {
                        "cost": 0.0, "savings": 0.0, "count": 0, "unknown_price_count": 0,
                        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    },
                )
                _add_amount(provider_bucket, "cost", cost)
                _add_amount(provider_bucket, "savings", savings)
                provider_bucket["count"] = int(provider_bucket.get("count") or 0) + units
                provider_bucket["prompt_tokens"] = int(provider_bucket.get("prompt_tokens") or 0) + prompt_tokens
                provider_bucket["completion_tokens"] = int(provider_bucket.get("completion_tokens") or 0) + completion_tokens
                provider_bucket["reasoning_tokens"] = int(provider_bucket.get("reasoning_tokens") or 0) + reasoning_tokens
                provider_bucket["cache_read_tokens"] = int(provider_bucket.get("cache_read_tokens") or 0) + cache_read_tokens
                provider_bucket["cache_creation_tokens"] = int(provider_bucket.get("cache_creation_tokens") or 0) + cache_creation_tokens
                if unknown_price:
                    provider_bucket["unknown_price_count"] = int(provider_bucket.get("unknown_price_count") or 0) + units

                source_bucket = result["by_source"].setdefault(
                    source,
                    {
                        "cost": 0.0, "savings": 0.0, "count": 0, "unknown_price_count": 0,
                        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    },
                )
                _add_amount(source_bucket, "cost", cost)
                _add_amount(source_bucket, "savings", savings)
                source_bucket["count"] = int(source_bucket.get("count") or 0) + units
                source_bucket["prompt_tokens"] = int(source_bucket.get("prompt_tokens") or 0) + prompt_tokens
                source_bucket["completion_tokens"] = int(source_bucket.get("completion_tokens") or 0) + completion_tokens
                source_bucket["reasoning_tokens"] = int(source_bucket.get("reasoning_tokens") or 0) + reasoning_tokens
                source_bucket["cache_read_tokens"] = int(source_bucket.get("cache_read_tokens") or 0) + cache_read_tokens
                source_bucket["cache_creation_tokens"] = int(source_bucket.get("cache_creation_tokens") or 0) + cache_creation_tokens
                if unknown_price:
                    source_bucket["unknown_price_count"] = int(source_bucket.get("unknown_price_count") or 0) + units
                _add_amount(result, "total_cost_paid", cost)
                _add_amount(result, "total_savings_paid", savings)
            else:
                _add_amount(result, "total_cost_free", cost)

            _add_amount(result, "total_cost", cost)
            _add_amount(result, "total_savings", savings)
            for aggregate_key, value in (
                ("total_prompt_tokens", prompt_tokens),
                ("total_completion_tokens", completion_tokens),
                ("total_reasoning_tokens", reasoning_tokens),
                ("total_cache_read_tokens", cache_read_tokens),
                ("total_cache_creation_tokens", cache_creation_tokens),
            ):
                result[aggregate_key] = int(result.get(aggregate_key) or 0) + value * units
            result["count"] = int(result.get("count") or 0) + units
            if unknown_price:
                result["unknown_price_count"] = int(result.get("unknown_price_count") or 0) + units

    return result
