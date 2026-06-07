from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import logging
import mimetypes
import os
import re
import tempfile
import uuid
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional

import filetype
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import config_manager
import constants
import gemini_api
import room_manager
import utils
from api.schemas import (
    AutonomyPresetRequest,
    AutonomyPresetResponse,
    AutonomyStatusResponse,
    ChatHistoryMessage,
    ChatHistoryResponse,
    ChatRequest,
    ChatResponse,
    EventNotificationSettingsRequest,
    EventNotificationSettingsResponse,
    EventRequest,
    EventResponse,
    LocationListResponse,
    LocationSetRequest,
    LocationSetResponse,
    LocationSummary,
    MemorySearchResponse,
    NoteResponse,
    PushPublicKeyResponse,
    PushSendResponse,
    PushStatusResponse,
    PushSubscriptionSummary,
    PushSubscriptionRequest,
    PushSubscriptionResponse,
    PushTestRequest,
    RoomStatus,
    RoomSummary,
    TranscriptionResponse,
    TtsRequest,
    TtsResponse,
    TwitterDraftActionRequest,
    TwitterDraftActionResponse,
    TwitterDraftListResponse,
    TwitterDraftSummary,
    UploadResponse,
)
from tts_key_rotation import generate_audio_with_key_rotation
from tts_text_policy import prepare_tts_text_plan

logger = logging.getLogger(__name__)
_room_locks: Dict[str, threading.Lock] = {}
_ALLOWED_UPLOAD_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_ALLOWED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".webm", ".ogg"}
_ALLOWED_TTS_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".pcm"}
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024
_MAX_AUDIO_UPLOAD_BYTES = 20 * 1024 * 1024
_DEFAULT_EVENT_NOTIFICATION_DEDUPE_SECONDS = 300
_DEFAULT_WEB_PUSH_SUBSCRIPTION_TTL_DAYS = 90
_PUSH_RESPONSE_EXCERPT_CHARS = 42
_EVENT_IMPORTANCE_ORDER = {"low": 0, "normal": 1, "high": 2, "critical": 3}
_event_notification_last_sent: Dict[tuple[str, str, str], datetime.datetime] = {}


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _room_exists(room_id: str) -> bool:
    return any(folder == room_id for _, folder in room_manager.get_room_list_for_ui())


def _require_room(room_id: str) -> None:
    if not _room_exists(room_id):
        raise ValueError(f"Room not found: {room_id}")


def _get_room_lock(room_id: str) -> threading.Lock:
    if room_id not in _room_locks:
        _room_locks[room_id] = threading.Lock()
    return _room_locks[room_id]


def _api_gateway_settings() -> dict:
    return dict(config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}) or {})


def _api_gateway_settings_from_file() -> dict:
    try:
        with open(constants.CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return {}
    settings = config.get("api_gateway_settings") if isinstance(config, dict) else {}
    return dict(settings or {}) if isinstance(settings, dict) else {}


def _save_api_gateway_settings(settings: dict) -> None:
    merged = _api_gateway_settings_from_file()
    merged.update(_api_gateway_settings())
    merged.update(settings)
    config_manager.save_config_if_changed("api_gateway_settings", merged)


def _generate_vapid_keys() -> tuple[str, str]:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    private_value = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return _b64url_encode(public_bytes), _b64url_encode(private_value)


def _valid_vapid_keys(public_key: str, private_key: str) -> bool:
    try:
        public_bytes = _b64url_decode(public_key)
        private_bytes = _b64url_decode(private_key)
    except Exception:
        return False
    return len(public_bytes) == 65 and public_bytes[:1] == b"\x04" and len(private_bytes) == 32


def get_push_public_key() -> PushPublicKeyResponse:
    settings = _api_gateway_settings()
    public_key = str(settings.get("web_push_public_key") or "").strip()
    private_key = str(settings.get("web_push_private_key") or "").strip()
    if not _valid_vapid_keys(public_key, private_key):
        public_key, private_key = _generate_vapid_keys()
        settings["web_push_public_key"] = public_key
        settings["web_push_private_key"] = private_key
        settings["web_push_subscriptions"] = {}
        _save_api_gateway_settings(settings)
    return PushPublicKeyResponse(public_key=public_key)


def _push_subscriptions_by_room(settings: Optional[dict] = None) -> dict:
    settings = settings or _api_gateway_settings()
    subscriptions = settings.get("web_push_subscriptions") or {}
    return subscriptions if isinstance(subscriptions, dict) else {}


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso_datetime(value: Any) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _coerce_positive_int(value: Any, fallback: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, coerced)


def _event_notification_settings(settings: Optional[dict] = None) -> EventNotificationSettingsResponse:
    settings = settings or _api_gateway_settings()
    minimum_importance = str(settings.get("event_notification_minimum_importance") or "high").strip().lower()
    if minimum_importance not in {"high", "critical"}:
        minimum_importance = "high"
    cooldowns = settings.get("event_notification_cooldowns") or {}
    normalized_cooldowns: dict[str, int] = {}
    if isinstance(cooldowns, dict):
        for key, value in cooldowns.items():
            source = str(key or "").strip()
            if source:
                normalized_cooldowns[source] = min(_coerce_positive_int(value, 0), 86400)
    return EventNotificationSettingsResponse(
        enabled=bool(settings.get("event_notifications_enabled", True)),
        minimum_importance=minimum_importance,
        default_cooldown_seconds=min(
            _coerce_positive_int(
                settings.get("event_notification_default_cooldown_seconds", _DEFAULT_EVENT_NOTIFICATION_DEDUPE_SECONDS),
                _DEFAULT_EVENT_NOTIFICATION_DEDUPE_SECONDS,
            ),
            86400,
        ),
        source_cooldowns=normalized_cooldowns,
        response_preview_enabled=bool(settings.get("response_notification_preview_enabled", True)),
    )


def get_event_notification_settings() -> EventNotificationSettingsResponse:
    return _event_notification_settings()


def update_event_notification_settings(request: EventNotificationSettingsRequest) -> EventNotificationSettingsResponse:
    source_cooldowns = {
        str(source).strip(): min(_coerce_positive_int(seconds, 0), 86400)
        for source, seconds in (request.source_cooldowns or {}).items()
        if str(source).strip()
    }
    settings = _api_gateway_settings()
    settings["event_notifications_enabled"] = bool(request.enabled)
    settings["event_notification_minimum_importance"] = request.minimum_importance
    settings["event_notification_default_cooldown_seconds"] = min(
        _coerce_positive_int(request.default_cooldown_seconds, _DEFAULT_EVENT_NOTIFICATION_DEDUPE_SECONDS),
        86400,
    )
    settings["event_notification_cooldowns"] = source_cooldowns
    settings["response_notification_preview_enabled"] = bool(request.response_preview_enabled)
    _save_api_gateway_settings(settings)
    response = _event_notification_settings(settings)
    response.status = "saved"
    return response


def _web_push_subscription_ttl_days(settings: dict) -> int:
    return _coerce_positive_int(
        settings.get("web_push_subscription_ttl_days", _DEFAULT_WEB_PUSH_SUBSCRIPTION_TTL_DAYS),
        _DEFAULT_WEB_PUSH_SUBSCRIPTION_TTL_DAYS,
    )


def _push_subscription_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]


def _push_endpoint_host(endpoint: str) -> str:
    if "://" not in endpoint:
        return endpoint[:80]
    try:
        return endpoint.split("/")[2]
    except IndexError:
        return endpoint[:80]


def _normalize_push_subscription(item: dict, now: Optional[datetime.datetime] = None) -> dict:
    now = now or _utc_now()
    endpoint = str(item.get("endpoint") or "").strip()
    created_at = str(item.get("created_at") or item.get("updated_at") or now.isoformat())
    updated_at = str(item.get("updated_at") or created_at)
    return {
        "id": str(item.get("id") or _push_subscription_id(endpoint)),
        "endpoint": endpoint,
        "keys": item.get("keys") if isinstance(item.get("keys"), dict) else {},
        "user_agent": str(item.get("user_agent") or ""),
        "created_at": created_at,
        "updated_at": updated_at,
        "last_success_at": str(item.get("last_success_at") or ""),
        "last_failure_at": str(item.get("last_failure_at") or ""),
        "failure_count": _coerce_positive_int(item.get("failure_count", 0), 0),
    }


def _clean_push_subscriptions(settings: dict, now: Optional[datetime.datetime] = None) -> tuple[dict, int]:
    now = now or _utc_now()
    ttl_days = _web_push_subscription_ttl_days(settings)
    cutoff = now - datetime.timedelta(days=ttl_days) if ttl_days > 0 else None
    subscriptions = _push_subscriptions_by_room(settings)
    cleaned: dict[str, list[dict]] = {}
    removed_count = 0
    for room_id, items in subscriptions.items():
        room_items = items if isinstance(items, list) else []
        kept: list[dict] = []
        for item in room_items:
            if not isinstance(item, dict):
                removed_count += 1
                continue
            normalized = _normalize_push_subscription(item, now)
            if not normalized["endpoint"] or not normalized["keys"].get("p256dh") or not normalized["keys"].get("auth"):
                removed_count += 1
                continue
            last_seen = _parse_iso_datetime(normalized.get("updated_at") or normalized.get("created_at"))
            if cutoff and last_seen and last_seen < cutoff:
                removed_count += 1
                continue
            kept.append(normalized)
        if kept:
            cleaned[room_id] = kept[-20:]
    return cleaned, removed_count


def _save_push_subscriptions(settings: dict, subscriptions: dict) -> None:
    settings["web_push_subscriptions"] = subscriptions
    _save_api_gateway_settings(settings)


def _is_expired_push_failure(detail: str) -> bool:
    normalized = str(detail or "").lower()
    return normalized.startswith("404 ") or normalized.startswith("410 ") or "expired" in normalized or "gone" in normalized


def _response_push_body(room_id: str, reply: str) -> str:
    speaker = _room_display_name(room_id)
    settings = _api_gateway_settings()
    if not bool(settings.get("response_notification_preview_enabled", True)):
        return f"{speaker}からのメッセージがあります。"
    cleaned = re.sub(r"\s+", " ", utils.clean_persona_text(reply or "")).strip()
    if not cleaned:
        return f"{speaker}からのメッセージがあります。"
    if len(cleaned) > _PUSH_RESPONSE_EXCERPT_CHARS:
        cleaned = cleaned[:_PUSH_RESPONSE_EXCERPT_CHARS].rstrip() + "..."
    return f"{speaker}「{cleaned}」"


def subscribe_push(room_id: str, request: PushSubscriptionRequest) -> PushSubscriptionResponse:
    _require_room(room_id)
    get_push_public_key()
    endpoint = (request.endpoint or "").strip()
    keys = request.keys or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("valid push subscription endpoint and keys are required")

    settings = _api_gateway_settings()
    subscriptions, cleaned_count = _clean_push_subscriptions(settings)
    room_subscriptions = [item for item in subscriptions.get(room_id, []) if isinstance(item, dict)]
    now = _utc_now().isoformat()
    existing_by_endpoint = {str(item.get("endpoint") or ""): item for item in room_subscriptions}
    existing = existing_by_endpoint.get(endpoint) or {}
    subscription = {
        "id": _push_subscription_id(endpoint),
        "endpoint": endpoint,
        "keys": {"p256dh": keys.get("p256dh", ""), "auth": keys.get("auth", "")},
        "user_agent": request.user_agent or "",
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        "last_success_at": existing.get("last_success_at") or "",
        "last_failure_at": existing.get("last_failure_at") or "",
        "failure_count": _coerce_positive_int(existing.get("failure_count", 0), 0),
    }
    replaced = False
    for index, item in enumerate(room_subscriptions):
        if item.get("endpoint") == endpoint:
            room_subscriptions[index] = subscription
            replaced = True
            break
    if not replaced:
        room_subscriptions.append(subscription)
    subscriptions[room_id] = room_subscriptions[-20:]
    _save_push_subscriptions(settings, subscriptions)
    detail = f"cleaned={cleaned_count}" if cleaned_count else ""
    return PushSubscriptionResponse(status="saved", subscription_count=len(subscriptions[room_id]), detail=detail)


def unsubscribe_push(room_id: str, subscription_id: str) -> PushSubscriptionResponse:
    _require_room(room_id)
    target_id = (subscription_id or "").strip()
    if not target_id:
        raise ValueError("push subscription id is required")

    settings = _api_gateway_settings()
    all_subscriptions, cleaned_count = _clean_push_subscriptions(settings)
    room_subscriptions = list(all_subscriptions.get(room_id, []) or [])
    kept = []
    removed = 0
    for item in room_subscriptions:
        endpoint = str(item.get("endpoint") or "")
        item_id = str(item.get("id") or _push_subscription_id(endpoint))
        if item_id == target_id:
            removed += 1
            continue
        kept.append(item)

    if kept:
        all_subscriptions[room_id] = kept[-20:]
    else:
        all_subscriptions.pop(room_id, None)
    _save_push_subscriptions(settings, all_subscriptions)

    detail_parts = []
    if removed:
        detail_parts.append(f"removed={removed}")
    if cleaned_count:
        detail_parts.append(f"cleaned={cleaned_count}")
    status = "deleted" if removed else "not_found"
    return PushSubscriptionResponse(
        status=status,
        subscription_count=len(all_subscriptions.get(room_id, []) or []),
        detail="; ".join(detail_parts),
    )


def get_push_status(room_id: str) -> PushStatusResponse:
    _require_room(room_id)
    settings = _api_gateway_settings()
    all_subscriptions, cleaned_count = _clean_push_subscriptions(settings)
    if cleaned_count:
        _save_push_subscriptions(settings, all_subscriptions)
    subscriptions = list(all_subscriptions.get(room_id, []) or [])
    endpoints = []
    summaries: list[PushSubscriptionSummary] = []
    for index, item in enumerate(subscriptions[:10], start=1):
        endpoint = str(item.get("endpoint") or "")
        endpoint_host = f"Push端末 {index}"
        endpoints.append(endpoint_host)
        summaries.append(
            PushSubscriptionSummary(
                id=str(item.get("id") or _push_subscription_id(endpoint)),
                endpoint_host=endpoint_host,
                user_agent=str(item.get("user_agent") or ""),
                created_at=str(item.get("created_at") or ""),
                updated_at=str(item.get("updated_at") or ""),
                last_success_at=str(item.get("last_success_at") or ""),
                last_failure_at=str(item.get("last_failure_at") or ""),
                failure_count=_coerce_positive_int(item.get("failure_count", 0), 0),
            )
        )
    return PushStatusResponse(
        room_id=room_id,
        has_vapid_keys=bool(settings.get("web_push_public_key") and settings.get("web_push_private_key")),
        subscription_count=len(subscriptions),
        endpoints=endpoints,
        subscriptions=summaries,
        cleaned_count=cleaned_count,
    )


def _send_web_push(subscription: dict, payload: dict) -> tuple[bool, str]:
    settings = _api_gateway_settings()
    private_key = str(settings.get("web_push_private_key") or "").strip()
    public_key = str(settings.get("web_push_public_key") or "").strip()
    if not private_key or not public_key:
        get_push_public_key()
        settings = _api_gateway_settings()
        private_key = str(settings.get("web_push_private_key") or "").strip()

    try:
        from pywebpush import WebPushException, webpush
    except Exception:
        return False, "dependency_missing: pywebpush is not installed"

    vapid_claims = {
        "sub": str(settings.get("web_push_vapid_subject") or "mailto:nexus-ark@example.invalid"),
    }
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.get("endpoint", ""),
                "keys": subscription.get("keys", {}),
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=private_key,
            vapid_claims=vapid_claims,
            ttl=60 * 60,
            timeout=5,
        )
        return True, "sent"
    except WebPushException as e:
        response = getattr(e, "response", None)
        if response is not None:
            return False, f"{response.status_code} {response.reason}: {response.text[:500]}"
        return False, str(e)
    except Exception as e:
        return False, str(e)


def send_push_to_room(room_id: str, title: str, body: str, url: str = "/lite/") -> PushSendResponse:
    _require_room(room_id)
    settings = _api_gateway_settings()
    all_subscriptions, cleaned_count = _clean_push_subscriptions(settings)
    subscriptions = list(all_subscriptions.get(room_id, []) or [])
    if not subscriptions:
        if cleaned_count:
            _save_push_subscriptions(settings, all_subscriptions)
        logger.info("Lite PWA web push skipped for %s: no subscriptions", room_id)
        detail = "no push subscriptions for room"
        if cleaned_count:
            detail = f"{detail}; cleaned={cleaned_count}"
        return PushSendResponse(status="no_subscriptions", subscription_count=0, detail=detail)

    payload = {"title": title, "body": body, "url": url}
    sent = 0
    failed = 0
    details = []
    kept_subscriptions: list[dict] = []
    now = _utc_now().isoformat()
    for subscription in subscriptions:
        ok, detail = _send_web_push(subscription, payload)
        if ok:
            sent += 1
            subscription["last_success_at"] = now
            subscription["last_failure_at"] = ""
            subscription["failure_count"] = 0
            kept_subscriptions.append(subscription)
        else:
            failed += 1
            subscription["last_failure_at"] = now
            subscription["failure_count"] = _coerce_positive_int(subscription.get("failure_count", 0), 0) + 1
            details.append(detail)
            if not _is_expired_push_failure(detail):
                kept_subscriptions.append(subscription)
    all_subscriptions[room_id] = kept_subscriptions[-20:]
    if not all_subscriptions[room_id]:
        all_subscriptions.pop(room_id, None)
    _save_push_subscriptions(settings, all_subscriptions)
    status = "sent" if sent and not failed else "partial" if sent else "failed"
    detail = "; ".join(details[:3])
    if cleaned_count:
        detail = f"{detail}; cleaned={cleaned_count}" if detail else f"cleaned={cleaned_count}"
    logger.info(
        "Lite PWA web push result for %s: status=%s subscriptions=%s sent=%s failed=%s detail=%s",
        room_id,
        status,
        len(subscriptions),
        sent,
        failed,
        detail,
    )
    return PushSendResponse(
        status=status,
        subscription_count=len(subscriptions),
        sent=sent,
        failed=failed,
        detail=detail,
    )


def send_push_test(room_id: str, request: PushTestRequest) -> PushSendResponse:
    return send_push_to_room(room_id, request.title, request.body)


def _room_display_name(room_id: str) -> str:
    config = room_manager.get_room_config(room_id) or {}
    display_name = str(config.get("room_name") or "").strip()
    return display_name or room_manager.get_character_name(room_id) or room_id


def list_rooms() -> List[RoomSummary]:
    rooms: List[RoomSummary] = []
    for display_name, folder_name in room_manager.get_room_list_for_ui():
        config = room_manager.get_room_config(folder_name) or {}
        rooms.append(
            RoomSummary(
                room_id=folder_name,
                display_name=config.get("room_name") or display_name or folder_name,
                description=config.get("description", ""),
                current_location=utils.get_current_location(folder_name) or "",
            )
        )
    return rooms


def get_room_status(room_id: str) -> RoomStatus:
    _require_room(room_id)
    config = room_manager.get_room_config(room_id) or {}
    snapshot: Dict[str, Any] = {}
    internal_state: Dict[str, Any] = {}
    try:
        from motivation_manager import MotivationManager

        motivation_manager = MotivationManager(room_id)
        snapshot = motivation_manager.get_state_snapshot()
        internal_state = motivation_manager.get_internal_state()
    except Exception as e:
        logger.warning("Failed to read motivation snapshot for %s: %s", room_id, e)
    drives_state = internal_state.get("drives", {}) if isinstance(internal_state, dict) else {}
    relatedness_level = (
        drives_state.get("relatedness", {}).get("level")
        if isinstance(drives_state.get("relatedness"), dict)
        else None
    )

    active_goals_count = 0
    try:
        from action_plan_manager import ActionPlanManager

        active_goals_count = 1 if ActionPlanManager(room_id).get_active_plan() else 0
    except Exception:
        active_goals_count = 0

    try:
        import session_arousal_manager

        arousal = session_arousal_manager.get_daily_average(room_id)
    except Exception:
        arousal = 0.5

    profile_image_path = None
    try:
        paths = room_manager.get_room_files_paths(room_id)
        if paths and paths[2]:
            profile_image_path = str(paths[2])
    except Exception:
        pass

    return RoomStatus(
        room_id=room_id,
        display_name=config.get("room_name") or room_manager.get_character_name(room_id),
        current_location=utils.get_current_location(room_id) or "",
        drives={
            "boredom": float(snapshot.get("boredom", 0.0)),
            "curiosity": float(snapshot.get("curiosity", 0.0)),
            "goal_drive": float(snapshot.get("goal_achievement", 0.0)),
            "relatedness": float(relatedness_level if relatedness_level is not None else snapshot.get("devotion", 0.0)),
        },
        current_expression=str(snapshot.get("persona_emotion", "neutral") or "neutral"),
        arousal=float(arousal),
        active_goals_count=active_goals_count,
        profile_image_path=profile_image_path,
        updated_at=datetime.datetime.now(datetime.timezone.utc),
    )


def _message_role_for_client(raw_role: str, responder: str) -> str:
    if raw_role == "USER":
        return "user"
    if raw_role == "AGENT":
        return "agent"
    return "system"


def _message_content_for_client(content: str, role: str) -> str:
    text = utils.remove_ai_timestamp(content or "")
    text = re.sub(r"\[(?:Generated Image|ファイル添付|VIEW_IMAGE):\s*[^\]]+?\]", "", text, flags=re.DOTALL)
    if role == "agent":
        text = utils.clean_persona_text(text)
    else:
        text = re.sub(r"\n\n\d{4}-\d{2}-\d{2}\s+\([^)]+\)\s+\d{2}:\d{2}:\d{2}\s+\|.*$", "", text).strip()
    return text.strip()


def _client_message_id_for_client(content: str) -> Optional[str]:
    match = re.search(r"\|\s*client_message_id=([A-Za-z0-9_.:-]+)", content or "")
    return match.group(1) if match else None


def _message_attachments_for_client(content: str) -> List[str]:
    paths: List[str] = []
    for match in re.finditer(r"\[(?:Generated Image|ファイル添付|VIEW_IMAGE):\s*([^\]]+?)\]", content or ""):
        path = match.group(1).strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _find_chat_response_by_client_message_id(room_id: str, log_file: str, client_message_id: str) -> Optional[ChatResponse]:
    if not client_message_id:
        return None
    try:
        messages = utils.load_chat_log(log_file)
    except Exception as e:
        logger.warning("Failed to read chat log for duplicate client_message_id check (%s): %s", room_id, e)
        return None

    user_index = -1
    for index, msg in enumerate(messages):
        if str(msg.get("role", "")).upper() != "USER":
            continue
        if _client_message_id_for_client(str(msg.get("content", ""))) == client_message_id:
            user_index = index

    if user_index < 0:
        return None

    response_text = ""
    response_attachments: List[str] = []
    model_name = None
    for msg in messages[user_index + 1:]:
        role = str(msg.get("role", "")).upper()
        if role == "USER":
            break
        if role != "AGENT":
            continue
        content = str(msg.get("content", ""))
        response_text = _message_content_for_client(content, "agent")
        response_attachments = _message_attachments_for_client(content)
        model_match = re.search(r"\|\s*([A-Za-z0-9_.:/-]+)\s*$", content)
        if model_match:
            model_name = model_match.group(1)
        break

    status = get_room_status(room_id)
    return ChatResponse(
        room_id=room_id,
        reply=response_text,
        arousal=status.arousal,
        expression=status.current_expression,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        suggested_actions=["view_generated_image"] if response_attachments else [],
        model=model_name,
        client_message_id=client_message_id,
        attachments=response_attachments,
    )


def get_recent_chat_history(room_id: str, limit: int = 12) -> ChatHistoryResponse:
    _require_room(room_id)
    log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_id)
    if not log_file:
        raise ValueError(f"Room log path is not available: {room_id}")

    try:
        log_dir = os.path.dirname(log_file)
        room_dir = os.path.dirname(log_dir) if os.path.basename(log_dir) == constants.LOGS_DIR_NAME else log_dir
        raw_messages, _ = utils.load_chat_log_lazy(room_dir, limit=max(limit * 2, 20), min_turns=limit)
    except Exception:
        raw_messages = utils.load_chat_log(log_file)[-(limit * 2):]

    client_messages: List[ChatHistoryMessage] = []
    for msg in raw_messages:
        raw_role = str(msg.get("role", "")).upper()
        responder = str(msg.get("responder", ""))
        role = _message_role_for_client(raw_role, responder)
        content = _message_content_for_client(str(msg.get("content", "")), role)
        if not content:
            continue
        if role == "system" and "[RAW_RESULT]" in content:
            continue
        client_messages.append(
            ChatHistoryMessage(
                role=role,
                speaker=responder if role != "user" else "user",
                content=content,
                client_message_id=_client_message_id_for_client(str(msg.get("content", ""))) if role == "user" else None,
                attachments=_message_attachments_for_client(str(msg.get("content", ""))),
            )
        )

    return ChatHistoryResponse(room_id=room_id, messages=client_messages[-limit:])


def _build_agent_args(room_id: str, request: ChatRequest, log_file: str) -> Dict[str, Any]:
    effective_settings = config_manager.get_effective_settings(room_id)
    season_en, time_of_day_en = utils._get_current_time_context(room_id)
    return {
        "room_to_respond": room_id,
        "api_key_name": effective_settings.get("api_key_name") or config_manager.initial_api_key_name_global,
        "api_history_limit": effective_settings.get("api_history_limit_option") or constants.DEFAULT_API_HISTORY_LIMIT_OPTION,
        "debug_mode": False,
        "history_log_path": log_file,
        "user_prompt_parts": [request.message],
        "soul_vessel_room": room_id,
        "active_participants": [],
        "active_attachments": request.attachments,
        "shared_location_name": utils.get_current_location(room_id),
        "shared_scenery_text": None,
        "season_en": season_en,
        "time_of_day_en": time_of_day_en,
        "global_model_from_ui": config_manager.get_current_global_model(),
        "skip_tool_execution": False,
        "enable_supervisor": False,
    }


def _visible_tool_result_for_client(msg: Any) -> Optional[str]:
    if not isinstance(msg, ToolMessage):
        return None
    tool_name = getattr(msg, "name", "") or ""
    if tool_name != "recommend_music":
        return None
    formatted = utils.format_tool_result_for_ui(tool_name, str(getattr(msg, "content", "") or ""))
    return formatted.strip() if formatted else None


def _extract_final_ai_response(results: List[Any]) -> tuple[str, Optional[str], List[str], List[str]]:
    final_text = ""
    captured_model_name = None
    attachments: List[str] = []
    visible_tool_results: List[str] = []
    for mode, payload in results:
        if mode != "values" or not isinstance(payload, dict):
            continue
        if payload.get("model_name"):
            captured_model_name = payload.get("model_name")
        messages = payload.get("messages") or []
        last_human_index = -1
        for idx, msg in enumerate(messages):
            if isinstance(msg, HumanMessage):
                last_human_index = idx
        current_turn_messages = messages[last_human_index + 1:] if last_human_index >= 0 else messages
        for msg in current_turn_messages:
            try:
                for path in _message_attachments_for_client(utils.get_content_as_string(msg)):
                    if path not in attachments:
                        attachments.append(path)
            except Exception:
                continue
            visible_tool_result = _visible_tool_result_for_client(msg)
            if visible_tool_result and visible_tool_result not in visible_tool_results:
                visible_tool_results.append(visible_tool_result)
        ai_messages = [
            msg for msg in messages
            if isinstance(msg, AIMessage) and utils.get_content_as_string(msg).strip()
        ]
        if ai_messages:
            final_text = utils.get_content_as_string(ai_messages[-1])
    return final_text, captured_model_name, attachments, visible_tool_results


def _reflect_response_metadata(room_id: str, response_text: str) -> None:
    persona_emotion_pattern = r'<persona_emotion\s+category=["\'](\w+)["\']\s+intensity=["\']([0-9.]+)["\']\s*/>'
    emotion_match = re.search(persona_emotion_pattern, response_text, re.IGNORECASE)
    if emotion_match:
        try:
            from motivation_manager import MotivationManager

            mm = MotivationManager(room_id)
            mm.set_persona_emotion(emotion_match.group(1).lower(), float(emotion_match.group(2)))
            mm._save_state()
        except Exception as e:
            logger.warning("Failed to reflect persona emotion for %s: %s", room_id, e)

    memory_trace_pattern = r'<memory_trace\s+id=["\']([^"\']+)["\']\s+resonance=["\']([0-9.]+)["\']\s*/>'
    trace_matches = re.findall(memory_trace_pattern, response_text, re.IGNORECASE)
    if trace_matches:
        try:
            from episodic_memory_manager import EpisodicMemoryManager

            manager = EpisodicMemoryManager(room_id)
            for episode_id, resonance_str in trace_matches:
                manager.update_arousal(episode_id, float(resonance_str))
        except Exception as e:
            logger.warning("Failed to update memory arousal for %s: %s", room_id, e)


def _run_chat(room_id: str, request: ChatRequest) -> ChatResponse:
    _require_room(room_id)
    if not request.message.strip():
        raise ValueError("message is required")

    log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_id)
    if not log_file:
        raise ValueError(f"Room log path is not available: {room_id}")

    with _get_room_lock(room_id):
        timestamp_text = datetime.datetime.now().strftime("%Y-%m-%d (%a) %H:%M:%S")
        source = request.source or "api"
        client_message_id = (request.client_message_id or "").strip()
        existing_response = _find_chat_response_by_client_message_id(room_id, log_file, client_message_id)
        if existing_response is not None:
            logger.info("Skipped duplicate API chat request for %s client_message_id=%s", room_id, client_message_id)
            return existing_response
        client_id_suffix = f" | client_message_id={client_message_id}" if client_message_id else ""
        attachment_markers = []
        for attachment_path in request.attachments or []:
            if attachment_path:
                attachment_markers.append(f"[VIEW_IMAGE: {attachment_path}]")
        attachment_block = ("\n" + "\n".join(attachment_markers)) if attachment_markers else ""
        full_user_log_entry = f"{request.message}{attachment_block}\n\n{timestamp_text} | API:{source} | user_id={request.user_id}{client_id_suffix}"
        utils.save_message_to_log(log_file, "## USER:user", full_user_log_entry)

        internal_state_before = None
        try:
            from motivation_manager import MotivationManager

            internal_state_before = MotivationManager(room_id).get_state_snapshot()
        except Exception:
            internal_state_before = None

        agent_args = _build_agent_args(room_id, request, log_file)
        results = list(gemini_api.invoke_nexus_agent_stream(agent_args))
        full_response, model_name, response_attachments, visible_tool_results = _extract_final_ai_response(results)
        for tool_result in visible_tool_results:
            utils.save_message_to_log(log_file, "## SYSTEM:tool_result:recommend_music:api", tool_result)
        cleaned_response = utils.remove_ai_timestamp(full_response).strip()
        if cleaned_response:
            _reflect_response_metadata(room_id, cleaned_response)
            actual_model_name = model_name or config_manager.get_current_global_model()
            ai_timestamp = datetime.datetime.now().strftime("%Y-%m-%d (%a) %H:%M:%S")
            content_to_log = f"{cleaned_response}\n\n{ai_timestamp} | {utils.sanitize_model_name(actual_model_name)}"
            utils.save_message_to_log(log_file, f"## AGENT:{room_id}", content_to_log)

        arousal = 0.5
        try:
            from arousal_calculator import calculate_arousal
            from motivation_manager import MotivationManager
            import session_arousal_manager

            mm = MotivationManager(room_id)
            mm.update_last_interaction()
            if internal_state_before and cleaned_response:
                internal_state_after = mm.get_state_snapshot()
                arousal = float(calculate_arousal(internal_state_before, internal_state_after))
                session_arousal_manager.add_arousal_score(room_id, arousal, time_str=datetime.datetime.now().strftime("%H:%M:%S"))
        except Exception as e:
            logger.warning("Post chat metadata update failed for %s: %s", room_id, e)

    reply = utils.clean_persona_text(cleaned_response or full_response or "")
    if reply and source == "mobile_lite":
        push_result = send_push_to_room(room_id, "Nexus Ark Lite", _response_push_body(room_id, reply))
        if push_result.status not in {"sent", "no_subscriptions"}:
            logger.info("Lite PWA web push result for %s: %s", room_id, push_result)

    status = get_room_status(room_id)
    suggested_actions = ["view_generated_image"] if response_attachments else []
    return ChatResponse(
        room_id=room_id,
        reply=reply,
        arousal=arousal,
        expression=status.current_expression,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        suggested_actions=suggested_actions,
        model=model_name,
        client_message_id=request.client_message_id,
        attachments=response_attachments,
    )


async def chat(room_id: str, request: ChatRequest) -> ChatResponse:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_chat, room_id, request)


async def save_upload(room_id: str, filename: str, content_type: str, data: bytes) -> UploadResponse:
    _require_room(room_id)
    if not data:
        raise ValueError("file is empty")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise ValueError("file is too large")

    original_name = Path(filename or "upload").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in _ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError("only image uploads are supported")
    if content_type and not content_type.startswith("image/"):
        raise ValueError("only image uploads are supported")

    upload_dir = Path(constants.ROOMS_DIR) / room_id / "attachments" / "api_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(original_name).stem).strip("._") or "image"
    saved_path = upload_dir / f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe_name}{suffix}"
    saved_path.write_bytes(data)

    return UploadResponse(
        attachment_id=str(saved_path),
        filename=original_name,
        mime_type=content_type or "application/octet-stream",
        size=len(data),
    )


def _cleanup_cached_audio(audio_dir: Path, suffix: str) -> None:
    try:
        keep_count = int(config_manager.CONFIG_GLOBAL.get("voice_input_audio_rotation_count", 10) or 10)
    except Exception:
        keep_count = 10
    try:
        audio_files = sorted(
            [path for path in audio_dir.glob(f"*{suffix}") if path.is_file()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for old_path in audio_files[max(1, keep_count):]:
            old_path.unlink(missing_ok=True)
    except Exception as e:
        logger.debug("Failed to clean API voice input cache: %s", e)


def _save_voice_input(room_id: str, filename: str, content_type: str, data: bytes) -> tuple[Path, str]:
    _require_room(room_id)
    if not data:
        raise ValueError("audio file is empty")
    if len(data) > _MAX_AUDIO_UPLOAD_BYTES:
        raise ValueError("audio file is too large")

    original_name = Path(filename or "voice.webm").name
    suffix = Path(original_name).suffix.lower()
    base_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if suffix not in _ALLOWED_AUDIO_EXTENSIONS:
        if base_content_type == "audio/webm":
            suffix = ".webm"
        elif base_content_type == "audio/wav":
            suffix = ".wav"
        elif base_content_type == "audio/mpeg":
            suffix = ".mp3"
        elif base_content_type in {"audio/mp4", "audio/x-m4a"}:
            suffix = ".m4a"
        elif base_content_type == "audio/ogg":
            suffix = ".ogg"
        else:
            raise ValueError("only audio uploads are supported")
    if base_content_type and not (base_content_type.startswith("audio/") or base_content_type == "video/webm"):
        raise ValueError("only audio uploads are supported")

    audio_dir = Path(constants.ROOMS_DIR) / room_id / "audio_cache" / "voice_input" / "api"
    audio_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(original_name).stem).strip("._") or "voice"
    saved_path = audio_dir / f"voice_api_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe_name}{suffix}"
    saved_path.write_bytes(data)
    _cleanup_cached_audio(audio_dir, suffix)
    normalized_mime = base_content_type or {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
    }.get(suffix, "audio/wav")
    if normalized_mime == "video/webm":
        normalized_mime = "audio/webm"
    return saved_path, normalized_mime


def _transcribe_voice_sync(room_id: str, filename: str, content_type: str, data: bytes) -> TranscriptionResponse:
    audio_path, mime_type = _save_voice_input(room_id, filename, content_type, data)
    effective_settings = config_manager.get_effective_settings(room_id)
    api_key_name = effective_settings.get("api_key_name") or config_manager.initial_api_key_name_global
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name, "")
    if not api_key:
        raise ValueError("Gemini API key is not configured")

    import stt_manager

    # Lite PWA voice input is intentionally independent from hidden Discord voice-input settings.
    # Some rooms may still have legacy values such as "openai:whisper-1" there.
    normalized_model_name = constants.DISCORD_VOICE_STT_MODEL
    try:
        result = stt_manager.transcribe_audio_file_detailed(
            str(audio_path),
            api_key,
            model_name=normalized_model_name or constants.DISCORD_VOICE_STT_MODEL,
            mime_type=mime_type,
        )
    except stt_manager.SttApiError as e:
        raise ValueError(str(e)) from e
    transcript = (getattr(result, "text", "") or "").strip()
    if not transcript:
        return TranscriptionResponse(text="", provider="gemini", model=getattr(result, "model_name", normalized_model_name), uncertain=False)
    return TranscriptionResponse(
        text=transcript,
        provider="gemini",
        model=getattr(result, "model_name", normalized_model_name),
        uncertain=bool(getattr(result, "uncertain", False)),
    )


async def transcribe_voice(room_id: str, filename: str, content_type: str, data: bytes) -> TranscriptionResponse:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _transcribe_voice_sync, room_id, filename, content_type, data)


def _resolve_tts_settings(room_id: str) -> Dict[str, Any]:
    effective_settings = config_manager.get_effective_settings(room_id)
    provider = config_manager.tts_provider_key_from_display(effective_settings.get("tts_provider", "gemini"))
    model = (effective_settings.get("tts_model") or "").strip()
    provider_models = config_manager.get_tts_model_choices(provider)
    if provider_models and model not in provider_models:
        model = provider_models[0]
    voice_source = effective_settings.get("tts_voice") or effective_settings.get("voice_id", "iapetus")
    if provider != "gemini" and voice_source in config_manager.SUPPORTED_VOICES:
        voice_choices = config_manager.get_tts_voice_map(provider)
        voice_source = next(iter(voice_choices.keys()), voice_source)
    voice_id = config_manager.resolve_tts_voice_id(provider, voice_source) or str(voice_source)
    style_prompt = effective_settings.get("tts_style_prompt", effective_settings.get("voice_style_prompt", "")) or ""
    response_format = effective_settings.get("tts_response_format") or ("wav" if provider == "gemini" else "mp3")
    api_key = None
    base_url = None
    extra_body = None

    if provider == "gemini":
        api_key_name = effective_settings.get("api_key_name") or config_manager.initial_api_key_name_global
        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
        model = model or "gemini-3.1-flash-tts-preview"
        response_format = "wav"
    elif provider == "openai":
        openai_setting = (
            config_manager.get_openai_setting_by_name("OpenAI Official")
            or config_manager.get_openai_setting_by_name("OpenAI")
            or config_manager.get_active_openai_setting()
        )
        if openai_setting:
            api_key = openai_setting.get("api_key")
            base_url = openai_setting.get("base_url") or None
        model = model or "gpt-4o-mini-tts"
        response_format = response_format or "mp3"
    elif provider == "openai_compatible":
        openai_setting = config_manager.get_active_openai_setting()
        if openai_setting:
            api_key = openai_setting.get("api_key")
            base_url = openai_setting.get("base_url") or None
            if not model:
                model = openai_setting.get("tts_model") or openai_setting.get("default_model") or "canopylabs/orpheus-v1-english"
            extra_body = openai_setting.get("tts_extra_body")
        response_format = response_format or "wav"
    elif provider == "elevenlabs":
        api_key = config_manager.CONFIG_GLOBAL.get("elevenlabs_api_key")
        model = model or "eleven_flash_v2_5"
        response_format = "mp3" if response_format == "wav" else (response_format or "mp3")
    elif provider in {"aivisspeech", "voicevox", "coeiroink"}:
        api_key = "LOCAL_VOICEVOX_COMPATIBLE"
        model = model or (config_manager.get_tts_model_choices(provider)[0] if config_manager.get_tts_model_choices(provider) else "")
        response_format = "wav"

    return {
        "provider": provider,
        "model": model,
        "voice_id": voice_id,
        "style_prompt": style_prompt,
        "api_key": api_key,
        "api_key_name": api_key_name if provider == "gemini" else None,
        "base_url": base_url,
        "response_format": response_format,
        "extra_body": extra_body,
    }


def _synthesize_speech_sync(room_id: str, request: TtsRequest) -> TtsResponse:
    _require_room(room_id)
    text_plan = prepare_tts_text_plan(request.text, request.mode, request.max_chars)
    if not text_plan.segments:
        raise ValueError("text is required")

    settings = _resolve_tts_settings(room_id)
    resolved_paths: List[Path] = []
    partial_notice = ""
    for index, segment in enumerate(text_plan.segments, start=1):
        audio_path = generate_audio_with_key_rotation(segment, room_id, settings)
        if not audio_path or str(audio_path).startswith("【エラー】"):
            if text_plan.mode == "split" and resolved_paths:
                partial_notice = (
                    f"{text_plan.notice or ''} "
                    f"{index}分割目の音声生成に失敗したため、生成済みの{len(resolved_paths)}分割だけ再生できます。"
                ).strip()
                logger.warning("Partial TTS response for %s: %s", room_id, audio_path or "no audio path")
                break
            raise ValueError(str(audio_path or "音声の生成に失敗しました。"))
        resolved_paths.append(resolve_audio_path(str(audio_path)))

    audio_ids = [str(path) for path in resolved_paths]
    mime_type = mimetypes.guess_type(str(resolved_paths[0]))[0] or "audio/wav"
    return TtsResponse(
        audio_id=audio_ids[0],
        audio_ids=audio_ids,
        mime_type=mime_type,
        provider=settings["provider"],
        model=settings["model"],
        voice=settings["voice_id"],
        mode=text_plan.mode,
        segment_count=len(audio_ids),
        truncated=text_plan.truncated or bool(partial_notice),
        notice=partial_notice or text_plan.notice or None,
    )


async def synthesize_speech(room_id: str, request: TtsRequest) -> TtsResponse:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _synthesize_speech_sync, room_id, request)


def resolve_asset_path(path: str) -> Path:
    requested = Path(path or "").expanduser()
    if not requested.exists() or not requested.is_file():
        raise ValueError("asset not found")

    try:
        resolved = requested.resolve()
        cwd = Path.cwd().resolve()
        tmp = Path(tempfile.gettempdir()).resolve()
        if not (resolved.is_relative_to(cwd) or resolved.is_relative_to(tmp)):
            raise ValueError("asset path is not allowed")
    except AttributeError:
        resolved_str = str(requested.resolve())
        if not (resolved_str.startswith(str(Path.cwd().resolve())) or resolved_str.startswith(str(Path(tempfile.gettempdir()).resolve()))):
            raise ValueError("asset path is not allowed")

    kind = filetype.guess(str(requested))
    if not (kind and kind.mime.startswith("image/")):
        raise ValueError("asset is not an image")
    return requested


def resolve_audio_path(path: str) -> Path:
    requested = Path(path or "").expanduser()
    if not requested.exists() or not requested.is_file():
        raise ValueError("audio not found")

    try:
        resolved = requested.resolve()
        cwd = Path.cwd().resolve()
        tmp = Path(tempfile.gettempdir()).resolve()
        if not (resolved.is_relative_to(cwd) or resolved.is_relative_to(tmp)):
            raise ValueError("audio path is not allowed")
    except AttributeError:
        resolved_str = str(requested.resolve())
        if not (resolved_str.startswith(str(Path.cwd().resolve())) or resolved_str.startswith(str(Path(tempfile.gettempdir()).resolve()))):
            raise ValueError("audio path is not allowed")

    suffix = requested.suffix.lower()
    guessed_type = mimetypes.guess_type(str(requested))[0] or ""
    if suffix not in _ALLOWED_TTS_EXTENSIONS and not guessed_type.startswith("audio/"):
        raise ValueError("asset is not audio")
    return requested


def record_event(room_id: str, request: EventRequest) -> EventResponse:
    _require_room(room_id)
    log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_id)
    if not log_file:
        raise ValueError(f"Room log path is not available: {room_id}")

    timestamp_text = datetime.datetime.now().strftime("%Y-%m-%d (%a) %H:%M:%S")
    summary_line = f"- summary: {request.summary}\n" if request.summary else ""
    attachments_line = f"- attachments: {request.attachments}\n" if request.attachments else ""
    event_text = (
        f"外部イベントを受信しました。\n"
        f"- type: {request.event_type}\n"
        f"- source: {request.source}\n"
        f"- importance: {request.importance}\n"
        f"{summary_line}"
        f"{attachments_line}"
        f"- data: {request.event_data}\n\n"
        f"{timestamp_text} | API:event"
    )
    utils.save_message_to_log(log_file, "## SYSTEM:external_event", event_text)

    notification_status = "not_requested"
    notification_text = None
    if request.trigger_notification:
        notification_status, notification_text = _handle_event_notification(room_id, request, log_file)

    return EventResponse(
        status="success",
        should_interact=bool(request.trigger_notification),
        notification_text=notification_text,
        notification_status=notification_status,
    )


def _event_notification_candidate(request: EventRequest) -> bool:
    settings = _event_notification_settings()
    if not settings.enabled:
        return False
    minimum = _EVENT_IMPORTANCE_ORDER.get(settings.minimum_importance, _EVENT_IMPORTANCE_ORDER["high"])
    importance = _EVENT_IMPORTANCE_ORDER.get(request.importance, 0)
    return bool(request.trigger_notification and importance >= minimum)


def _coerce_event_notification_cooldown(value: Any, fallback: int) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, min(seconds, 24 * 60 * 60))


def _get_event_notification_cooldown_seconds(source: str) -> int:
    settings = config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}) or {}
    default_seconds = _coerce_event_notification_cooldown(
        settings.get("event_notification_default_cooldown_seconds", _DEFAULT_EVENT_NOTIFICATION_DEDUPE_SECONDS),
        _DEFAULT_EVENT_NOTIFICATION_DEDUPE_SECONDS,
    )
    cooldowns = settings.get("event_notification_cooldowns") or {}
    if not isinstance(cooldowns, dict):
        return default_seconds

    source_key = (source or "").strip()
    raw_value = None
    for candidate in (source_key, source_key.lower(), "*"):
        if candidate and candidate in cooldowns:
            raw_value = cooldowns[candidate]
            break
    if raw_value is None:
        return default_seconds
    return _coerce_event_notification_cooldown(raw_value, default_seconds)


def _build_event_notification_text(room_id: str, request: EventRequest) -> str:
    summary = (request.summary or "").strip() or f"外部イベント「{request.event_type}」を受信しました。"
    metadata = f"source={request.source} / importance={request.importance} / room={room_id}"
    return f"{summary}\n\n{metadata}"


def _build_event_push_body(request: EventRequest) -> str:
    return (request.summary or "").strip() or f"外部イベント「{request.event_type}」を受信しました。"


def _save_event_notification_audit(log_file: str, request: EventRequest, status: str, detail: str = "") -> None:
    timestamp_text = datetime.datetime.now().strftime("%Y-%m-%d (%a) %H:%M:%S")
    detail_line = f"- detail: {detail}\n" if detail else ""
    audit_text = (
        f"外部イベント通知判定。\n"
        f"- type: {request.event_type}\n"
        f"- source: {request.source}\n"
        f"- importance: {request.importance}\n"
        f"- notification_status: {status}\n"
        f"{detail_line}\n"
        f"{timestamp_text} | API:event_notification"
    )
    utils.save_message_to_log(log_file, "## SYSTEM:external_event_notification", audit_text)


def _handle_event_notification(room_id: str, request: EventRequest, log_file: str) -> tuple[str, str]:
    settings = _event_notification_settings()
    if not settings.enabled:
        status = "skipped_disabled"
        detail = "外部イベント通知設定がOFFのため通知しません。"
        _save_event_notification_audit(log_file, request, status, detail)
        return status, detail

    if not _event_notification_candidate(request):
        status = "skipped_importance"
        detail = f"trigger_notification=true ですが importance が {settings.minimum_importance} 以上ではないため通知しません。"
        _save_event_notification_audit(log_file, request, status, detail)
        return status, detail

    now = datetime.datetime.now(datetime.timezone.utc)
    dedupe_key = (room_id, request.source, request.event_type)
    last_sent = _event_notification_last_sent.get(dedupe_key)
    cooldown_seconds = _get_event_notification_cooldown_seconds(request.source)
    if cooldown_seconds > 0 and last_sent and (now - last_sent).total_seconds() < cooldown_seconds:
        status = "suppressed_duplicate"
        detail = f"{cooldown_seconds}秒以内の重複イベントのため通知を抑制しました。"
        _save_event_notification_audit(log_file, request, status, detail)
        return status, detail

    notification_text = _build_event_notification_text(room_id, request)
    delivery_details: list[str] = []
    delivered = False
    failure_details: list[str] = []

    try:
        import alarm_manager

        result = alarm_manager.send_notification(
            room_manager.get_character_name(room_id),
            notification_text,
            {"source": "api_event", "importance": request.importance, "event_type": request.event_type},
            notification_kind="notification",
        )
        success = bool(result.get("success")) if isinstance(result, dict) else bool(result)
        if success:
            delivered = True
            delivery_details.append("notification_service=sent")
        else:
            failure_details.append(f"notification_service={result}")
    except Exception as e:
        logger.warning("API event notification failed for %s/%s: %s", room_id, request.event_type, e)
        failure_details.append(f"notification_service={e}")

    try:
        push_result = send_push_to_room(
            room_id,
            "Nexus Ark Lite",
            _build_event_push_body(request),
            url="/lite/",
        )
        if push_result.sent > 0:
            delivered = True
            delivery_details.append(f"web_push={push_result.status} sent={push_result.sent} failed={push_result.failed}")
        elif push_result.status != "no_subscriptions":
            failure_details.append(f"web_push={push_result.status} {push_result.detail}".strip())
        else:
            delivery_details.append("web_push=no_subscriptions")
    except Exception as e:
        logger.warning("API event web push failed for %s/%s: %s", room_id, request.event_type, e)
        failure_details.append(f"web_push={e}")

    if delivered:
        _event_notification_last_sent[dedupe_key] = now
        status = "sent"
        detail = "; ".join(delivery_details + failure_details)
        _save_event_notification_audit(log_file, request, status, detail)
        return status, notification_text

    status = "failed"
    detail = "; ".join(failure_details) or "no notification delivery succeeded"
    _save_event_notification_audit(log_file, request, status, detail)
    return status, f"通知送信に失敗しました: {detail}"


def search_memory(room_id: str, query: str, limit: int = 5) -> MemorySearchResponse:
    _require_room(room_id)
    results: List[Dict[str, Any]] = []
    api_key_name = config_manager.get_effective_settings(room_id).get("api_key_name") or config_manager.initial_api_key_name_global
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name, "")
    try:
        from rag_manager import RAGManager

        manager = RAGManager(room_id, api_key)
        for doc in manager.search(query, k=max(1, min(limit, 20)))[:limit]:
            results.append(
                {
                    "type": doc.metadata.get("source_type", doc.metadata.get("type", "rag")),
                    "content": doc.page_content,
                    "metadata": doc.metadata,
                }
            )
    except Exception as e:
        logger.warning("RAG search failed for %s: %s", room_id, e)
        log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_id)
        recent_messages = utils.load_chat_log(log_file)[-100:] if log_file and os.path.exists(log_file) else []
        query_lower = query.lower()
        for msg in recent_messages:
            content = msg.get("content", "")
            if query_lower in content.lower():
                results.append({"type": "chat_log", "content": content[:1000], "metadata": {"role": msg.get("role")}})
            if len(results) >= limit:
                break
    return MemorySearchResponse(query=query, results=results)


def _parse_notes_entries(content: str) -> List[Dict[str, Any]]:
    """タイムスタンプセクションでノートをパースしてエントリリストを返す。"""
    import re
    entries = []
    # 区切り線(---)の後にタイムスタンプが続く場合のみ分割
    sections = re.split(r'\n---+\n\s*(?=📝|\[)', content)

    for section in sections:
        section = section.strip()
        if not section:
            continue

        match1 = re.search(r'📝\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})', section)
        match2 = re.search(r'\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\]', section)

        if match1:
            date_str = match1.group(1)
            time_str = match1.group(2)
            timestamp = f"{date_str} {time_str}"
            content_start = match1.end()
            entry_content = section[content_start:].strip()
        elif match2:
            date_str = match2.group(1)
            time_str = match2.group(2)
            timestamp = f"{date_str} {time_str}"
            content_start = match2.end()
            entry_content = section[content_start:].strip()
        else:
            timestamp = "日付なし"
            date_str = ""
            entry_content = section

        if entry_content:
            entries.append({
                "timestamp": timestamp,
                "date": date_str,
                "content": entry_content,
                "raw_section": section
            })

    return entries[::-1]


def get_note(room_id: str, note_type: str, *, headings_only: bool = False, heading: str = "") -> NoteResponse:
    _require_room(room_id)
    normalized_type = (note_type or "").strip().lower()
    if normalized_type == "research":
        paths = room_manager.get_room_files_paths(room_id)
        note_path = Path(paths[6]) if paths and paths[6] else None
        title = "研究ノート"
    elif normalized_type == "creative":
        note_path = Path(room_manager.get_creative_notes_path(room_id))
        title = "創作ノート"
    else:
        raise ValueError("note_type must be research or creative")

    content = ""
    updated_at = None
    size = 0
    headings: List[str] = []
    if note_path and note_path.exists():
        size = note_path.stat().st_size
        full_content = note_path.read_text(encoding="utf-8")
        updated_at = datetime.datetime.fromtimestamp(note_path.stat().st_mtime, datetime.timezone.utc)
        
        entries = _parse_notes_entries(full_content)
        
        # エントリのラベル（ヘッダー）を作成
        headings = []
        for entry in entries:
            preview = entry["content"][:30].replace("\n", " ")
            if len(entry["content"]) > 30:
                preview += "..."
            label = f"{entry['timestamp']} - {preview}" if entry['timestamp'] != "日付なし" else preview[:30]
            headings.append(label)

        if headings_only:
            content = ""
        elif heading:
            matched_content = ""
            # 完全一致での探索
            for entry, label in zip(entries, headings):
                if label == heading or entry['timestamp'] == heading:
                    matched_content = entry["content"]
                    break
            
            # 部分一致（タイムスタンプ一致など）での探索
            if not matched_content:
                for entry, label in zip(entries, headings):
                    if heading.startswith(entry['timestamp']) or entry['timestamp'].startswith(heading):
                        matched_content = entry["content"]
                        break
            
            content = matched_content if matched_content else f"指定されたエントリ「{heading}」が見つかりませんでした。"
        else:
            content = full_content

    return NoteResponse(
        room_id=room_id,
        note_type=normalized_type,
        title=title,
        content=content,
        updated_at=updated_at,
        size=size,
        headings=headings,
    )


def _twitter_manager():
    from twitter_manager import twitter_manager

    twitter_manager.reload()
    return twitter_manager


def _find_room_twitter_draft(manager: Any, room_id: str, draft_id: str) -> Dict[str, Any]:
    for draft in manager.get_pending_list():
        if str(draft.get("id", "")) == draft_id and str(draft.get("room_name", "")) == room_id:
            return draft
    raise ValueError("Twitter draft not found for this room")


def list_twitter_drafts(room_id: str) -> TwitterDraftListResponse:
    _require_room(room_id)
    manager = _twitter_manager()
    limit = int(manager.get_twitter_post_limit(room_id) or 280)
    drafts: List[TwitterDraftSummary] = []
    for draft in manager.get_pending_list():
        if str(draft.get("room_name", "")) != room_id:
            continue
        content = str(draft.get("filtered_content") or draft.get("final_content") or draft.get("original_content") or "")
        drafts.append(
            TwitterDraftSummary(
                id=str(draft.get("id", "")),
                timestamp=str(draft.get("timestamp", "")),
                room_name=room_id,
                content=content,
                warnings=list(draft.get("warnings") or []),
                reply_to_url=draft.get("reply_to_url") or None,
                reply_to_id=draft.get("reply_to_id") or None,
                media_paths=list(draft.get("media_paths") or []),
                twitter_length=int(manager.calculate_twitter_length(content)),
                limit=limit,
            )
        )
    drafts.sort(key=lambda item: item.timestamp, reverse=True)
    return TwitterDraftListResponse(room_id=room_id, drafts=drafts)


def approve_twitter_draft(room_id: str, draft_id: str, request: TwitterDraftActionRequest) -> TwitterDraftActionResponse:
    _require_room(room_id)
    manager = _twitter_manager()
    draft = _find_room_twitter_draft(manager, room_id, draft_id)
    content = (request.content or "").strip()
    if not content:
        raise ValueError("Twitter draft content is required")
    limit = int(manager.get_twitter_post_limit(room_id) or 280)
    length = int(manager.calculate_twitter_length(content))
    if length > limit:
        raise ValueError(f"Twitter draft is too long: {length}/{limit}")
    media_paths = request.media_paths if request.media_paths else list(draft.get("media_paths") or [])
    reply_to_url = request.reply_to_url if request.reply_to_url is not None else draft.get("reply_to_url")
    if not manager.approve_tweet(draft_id, content, reply_to_url, media_paths):
        raise ValueError("Failed to approve Twitter draft")
    result = manager.execute_post(draft_id, room_name=room_id)
    if result.get("success"):
        return TwitterDraftActionResponse(
            status="posted",
            detail="Twitter下書きを承認して投稿しました。",
            posted=True,
            post_url=result.get("post_url") or None,
        )
    manager.move_back_to_drafts(draft_id)
    return TwitterDraftActionResponse(
        status="failed",
        detail="Twitter下書きは承認されましたが、投稿に失敗したため下書きへ戻しました。",
        posted=False,
        error=str(result.get("error") or "投稿に失敗しました。"),
    )


def reject_twitter_draft(room_id: str, draft_id: str) -> TwitterDraftActionResponse:
    _require_room(room_id)
    manager = _twitter_manager()
    _find_room_twitter_draft(manager, room_id, draft_id)
    manager.reject_tweet(draft_id)
    return TwitterDraftActionResponse(status="rejected", detail="Twitter下書きを却下しました。")


def list_locations(room_id: str) -> LocationListResponse:
    _require_room(room_id)
    world_path = room_manager.get_world_settings_path(room_id)
    world_data = utils.parse_world_file(world_path) if world_path else {}
    locations: List[LocationSummary] = []
    for area_name, area_data in (world_data or {}).items():
        if str(area_name).startswith("__") or not isinstance(area_data, dict):
            continue
        for place_name, place_data in area_data.items():
            if str(place_name).startswith("__"):
                continue
            location_id = str(place_name)
            locations.append(LocationSummary(id=location_id, name=str(place_name), area=str(area_name)))
    locations.sort(key=lambda item: (item.area, item.name))
    return LocationListResponse(
        room_id=room_id,
        current_location=utils.get_current_location(room_id) or "",
        locations=locations,
    )


def set_location(room_id: str, request: LocationSetRequest) -> LocationSetResponse:
    _require_room(room_id)
    available = list_locations(room_id)
    location_ids = {location.id for location in available.locations}
    if request.location_id not in location_ids:
        raise ValueError("Location not found")
    from tools.space_tools import set_current_location

    result = set_current_location.func(location_id=request.location_id, room_name=room_id)
    if not str(result).startswith("Success:"):
        raise ValueError(str(result))
    return LocationSetResponse(
        room_id=room_id,
        current_location=utils.get_current_location(room_id) or request.location_id,
        status="success",
    )


def _autonomy_status(room_id: str, status: str | None = None) -> AutonomyStatusResponse | AutonomyPresetResponse:
    effective_settings = config_manager.get_effective_settings(room_id)
    settings = effective_settings.get("autonomous_settings", {}) or {}
    enabled = bool(settings.get("enabled", False))
    payload = {
        "room_id": room_id,
        "enabled": enabled,
        "inactivity_minutes": int(settings.get("inactivity_minutes", 120) or 120),
        "schedule_cooldown_minutes": int(settings.get("schedule_cooldown_minutes", 60) or 60),
        "quiet_hours_start": str(settings.get("quiet_hours_start", "00:00") or "00:00"),
        "quiet_hours_end": str(settings.get("quiet_hours_end", "07:00") or "07:00"),
        "preset": "normal" if enabled else "quiet",
    }
    if status is not None:
        return AutonomyPresetResponse(**payload, status=status)
    return AutonomyStatusResponse(**payload)


def get_autonomy_status(room_id: str) -> AutonomyStatusResponse:
    _require_room(room_id)
    return _autonomy_status(room_id)


def set_autonomy_preset(room_id: str, request: AutonomyPresetRequest) -> AutonomyPresetResponse:
    _require_room(room_id)
    enabled = request.preset == "normal"
    if enabled:
        updates = {
            "enabled": True,
            "inactivity_minutes": 120,
            "schedule_cooldown_minutes": 60,
            "allow_schedule_tool": True,
        }
    else:
        updates = {
            "enabled": False,
            "inactivity_minutes": 10080,
            "schedule_cooldown_minutes": 10080,
            "allow_schedule_tool": False,
        }
    result = room_manager.update_room_override_nested(room_id, "autonomous_settings", updates)
    if result is False:
        raise ValueError("Failed to save autonomy preset")
    status = "自律行動を通常モードに戻しました。" if enabled else "自律行動を静かにしました。"
    return _autonomy_status(room_id, status=status)
