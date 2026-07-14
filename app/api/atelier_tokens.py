from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional

import config_manager

ATELIER_SCOPE_SAFE = "atelier_read_safe"
ATELIER_SCOPE_FULL = "atelier_read_full"
ATELIER_SCOPE_READ_CHAT = "read_chat"
ATELIER_SCOPE_READ_MEMORY = "read_memory"
ATELIER_SCOPE_READ_NOTES = "read_notes"
ATELIER_SCOPE_READ_CALENDAR = "read_calendar"
ATELIER_SCOPE_READ_TWITTER = "read_twitter"
ATELIER_SCOPE_READ_ITEMS = "read_items"
ATELIER_SCOPE_READ_LETTERS = "read_letters"
ATELIER_SCOPE_READ_AUTONOMY = "read_autonomy"
ATELIER_SCOPE_WRITE_LOCATION = "write_location"
ATELIER_SCOPE_SEND_CHAT = "send_chat"
ATELIER_SCOPE_WRITE_EVENT = "write_event"
ATELIER_SCOPE_WRITE_CALENDAR = "write_calendar"
ATELIER_SCOPE_WRITE_ITEMS = "write_items"
ATELIER_SCOPE_WRITE_NOTES = "write_notes"
ATELIER_SCOPE_WRITE_AUTONOMY = "write_autonomy"
ATELIER_SCOPE_USE_VOICE = "use_voice"
ATELIER_SCOPE_MANAGE_PUSH = "manage_push"
ATELIER_SCOPE_POST_TWITTER = "post_twitter"
ATELIER_TOKEN_TTL_SECONDS = 60 * 60
ATELIER_LEGACY_SCOPES = {ATELIER_SCOPE_SAFE, ATELIER_SCOPE_FULL}
ATELIER_FINE_READ_SCOPES = {
    ATELIER_SCOPE_READ_CHAT,
    ATELIER_SCOPE_READ_MEMORY,
    ATELIER_SCOPE_READ_NOTES,
    ATELIER_SCOPE_READ_CALENDAR,
    ATELIER_SCOPE_READ_TWITTER,
    ATELIER_SCOPE_READ_ITEMS,
    ATELIER_SCOPE_READ_LETTERS,
    ATELIER_SCOPE_READ_AUTONOMY,
}
ATELIER_FINE_WRITE_SCOPES = {
    ATELIER_SCOPE_WRITE_LOCATION,
    ATELIER_SCOPE_SEND_CHAT,
    ATELIER_SCOPE_WRITE_EVENT,
    ATELIER_SCOPE_WRITE_CALENDAR,
    ATELIER_SCOPE_WRITE_ITEMS,
    ATELIER_SCOPE_WRITE_NOTES,
    ATELIER_SCOPE_WRITE_AUTONOMY,
    ATELIER_SCOPE_USE_VOICE,
    ATELIER_SCOPE_MANAGE_PUSH,
}
ATELIER_FINE_OUTWARD_SCOPES = {
    ATELIER_SCOPE_POST_TWITTER,
}
ATELIER_TOKEN_SCOPES = (
    ATELIER_LEGACY_SCOPES
    | ATELIER_FINE_READ_SCOPES
    | ATELIER_FINE_WRITE_SCOPES
    | ATELIER_FINE_OUTWARD_SCOPES
)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _token_secret() -> str:
    settings = dict(config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}) or {})
    secret = str(settings.get("atelier_token_secret") or "").strip()
    if secret:
        return secret
    secret = secrets.token_urlsafe(48)
    settings["atelier_token_secret"] = secret
    config_manager.save_config_if_changed("api_gateway_settings", settings)
    return secret


def create_atelier_token(
    room_id: str,
    scope: str,
    ttl_seconds: int = ATELIER_TOKEN_TTL_SECONDS,
    app_id: str | None = None,
    scopes: list[str] | None = None,
) -> str:
    if scope not in ATELIER_TOKEN_SCOPES:
        raise ValueError("invalid atelier token scope")
    normalized_scopes: list[str] = []
    for item in scopes or []:
        value = str(item or "").strip()
        if value in ATELIER_TOKEN_SCOPES and value not in normalized_scopes:
            normalized_scopes.append(value)
    now = int(time.time())
    payload = {
        "room_id": str(room_id or "").strip(),
        "scope": scope,
        "scopes": normalized_scopes or [scope],
        "iat": now,
        "exp": now + int(ttl_seconds or ATELIER_TOKEN_TTL_SECONDS),
    }
    if app_id is not None:
        payload["app_id"] = str(app_id or "").strip()
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload_part = _b64url_encode(payload_bytes)
    sig = hmac.new(_token_secret().encode("utf-8"), payload_part.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_part}.{_b64url_encode(sig)}"


def verify_atelier_token(token: str) -> Optional[dict[str, Any]]:
    try:
        payload_part, sig_part = str(token or "").split(".", 1)
        expected = hmac.new(_token_secret().encode("utf-8"), payload_part.encode("ascii"), hashlib.sha256).digest()
        supplied = _b64url_decode(sig_part)
        if not hmac.compare_digest(supplied, expected):
            return None
        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        if payload.get("scope") not in ATELIER_TOKEN_SCOPES:
            return None
        if not str(payload.get("room_id") or "").strip():
            return None
        if "app_id" in payload and not str(payload.get("app_id") or "").strip():
            return None
        scopes = payload.get("scopes")
        if scopes is not None:
            if not isinstance(scopes, list):
                return None
            payload["scopes"] = [scope for scope in scopes if scope in ATELIER_TOKEN_SCOPES]
        return payload
    except Exception:
        return None
