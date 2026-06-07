"""Minimal token relay for Nexus Ark API Gateway.

Run:
    NEXUS_API_BASE_URL="http://127.0.0.1:8000" \
    NEXUS_API_TOKEN="your-nexus-api-token" \
    NEXUS_ROOM_ID="Default" \
    NEXUS_RELAY_TOKEN="relay-token-for-your-client" \
    uvicorn assets.guides.api_gateway_token_relay_server:app --host 0.0.0.0 --port 8011

Clients send events to this relay with X-Relay-Token. The relay keeps the real
Nexus Ark Bearer Token on the server side and forwards only allowed event data.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field


EventImportance = Literal["low", "normal", "high", "critical"]


class RelayEventRequest(BaseModel):
    event_type: str = Field(..., min_length=1, max_length=80)
    summary: Optional[str] = Field(default=None, max_length=500)
    details: Dict[str, Any] = Field(default_factory=dict)
    importance: EventImportance = "normal"
    attachments: List[str] = Field(default_factory=list, max_length=10)
    event_data: Dict[str, Any] = Field(default_factory=dict)
    trigger_notification: bool = False
    source: str = Field(default="external_relay", min_length=1, max_length=80)


app = FastAPI(title="Nexus Ark Token Relay", version="0.1.0")
_request_history: Dict[str, Deque[float]] = defaultdict(deque)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _require_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise HTTPException(status_code=500, detail=f"{name} is not configured")
    return value


def _check_relay_token(token: Optional[str]) -> None:
    expected = _require_env("NEXUS_RELAY_TOKEN")
    if not token or token != expected:
        raise HTTPException(status_code=403, detail="invalid relay token")


def _check_rate_limit(client_key: str) -> None:
    limit = int(_env("NEXUS_RELAY_RATE_LIMIT_PER_MINUTE", "60") or "60")
    now = time.monotonic()
    history = _request_history[client_key]
    while history and now - history[0] > 60:
        history.popleft()
    if len(history) >= limit:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    history.append(now)


def _allowed_event_types() -> set[str]:
    raw = _env("NEXUS_RELAY_ALLOWED_EVENT_TYPES")
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _forward_to_nexus(event: RelayEventRequest) -> Dict[str, Any]:
    base_url = _env("NEXUS_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    room_id = urllib.parse.quote(_env("NEXUS_ROOM_ID", "Default"), safe="")
    nexus_token = _require_env("NEXUS_API_TOKEN")
    url = f"{base_url}/api/v1/rooms/{room_id}/events"

    event_data = dict(event.event_data)
    if event.details:
        event_data.setdefault("details", event.details)

    payload = {
        "event_type": event.event_type,
        "source": event.source,
        "trigger_notification": event.trigger_notification,
        "summary": event.summary,
        "details": event.details,
        "importance": event.importance,
        "attachments": event.attachments,
        "event_data": event_data,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {nexus_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {"status": "success"}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise HTTPException(status_code=502, detail=f"Nexus API rejected the event: {detail}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"Nexus API is unreachable: {exc.reason}") from exc


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/events")
def relay_event(
    event: RelayEventRequest,
    request: Request,
    x_relay_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _check_relay_token(x_relay_token)
    client_key = request.client.host if request.client else "unknown"
    _check_rate_limit(client_key)

    allowed = _allowed_event_types()
    if allowed and event.event_type not in allowed:
        raise HTTPException(status_code=400, detail="event_type is not allowed by this relay")

    upstream = _forward_to_nexus(event)
    return {"status": "forwarded", "upstream": upstream}
