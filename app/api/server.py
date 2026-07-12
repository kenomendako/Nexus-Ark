from __future__ import annotations

import logging
import secrets
import threading
import time
import datetime
import hashlib
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import uvicorn
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import config_manager
import constants
import atelier_app_grants
from api import atelier_tokens
from api import service
from api.schemas import (
    AutonomyPresetRequest,
    AutonomyPresetResponse,
    CalendarEventCreateRequest,
    CalendarEventCreateResponse,
    CalendarEventsResponse,
    AutonomyStatusResponse,
    ChatHistoryResponse,
    ChatRequest,
    ChatResponse,
    EventNotificationSettingsRequest,
    EventNotificationSettingsResponse,
    EventRequest,
    EventResponse,
    LetterDetailResponse,
    LetterListResponse,
    LocationListResponse,
    LocationSetRequest,
    LocationSetResponse,
    MemorySearchResponse,
    NoteResponse,
    NoteUpdateRequest,
    PushPublicKeyResponse,
    PushSendResponse,
    PushStatusResponse,
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
    UploadResponse,
)

logger = logging.getLogger(__name__)
app = FastAPI(title="Nexus Ark API Gateway", version="0.1.0")
app.mount("/lite/static", StaticFiles(directory="mobile_app/static"), name="nexus_ark_lite_static")
_server: Optional[uvicorn.Server] = None
_server_thread: Optional[threading.Thread] = None
_rate_limit_lock = threading.Lock()
_rate_limit_buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_ATELIER_SAFE_EXACT_PATHS = {"status", "locations"}
_ATELIER_FULL_EXACT_PATHS = {"chat/history", "memory/search"}
_ATELIER_FINE_READ_PATHS = {
    "chat/history": atelier_tokens.ATELIER_SCOPE_READ_CHAT,
    "memory/search": atelier_tokens.ATELIER_SCOPE_READ_MEMORY,
    "calendar/events": atelier_tokens.ATELIER_SCOPE_READ_CALENDAR,
    "twitter/drafts": atelier_tokens.ATELIER_SCOPE_READ_TWITTER,
}
_ATELIER_FINE_WRITE_PATHS = {
    "location": atelier_tokens.ATELIER_SCOPE_WRITE_LOCATION,
    "chat": atelier_tokens.ATELIER_SCOPE_SEND_CHAT,
    "events": atelier_tokens.ATELIER_SCOPE_WRITE_EVENT,
    "calendar/events": atelier_tokens.ATELIER_SCOPE_WRITE_CALENDAR,
}


def _settings() -> dict:
    return config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}) or {}


def _expected_token() -> str:
    return str(_settings().get("auth_token") or "").strip()


def _atelier_settings() -> dict:
    return config_manager.CONFIG_GLOBAL.get("atelier_serve_settings", {}) or {}


def _atelier_api_enabled() -> bool:
    return bool(_atelier_settings().get("api_integration_enabled", False))


def _audit_enabled() -> bool:
    return bool(_settings().get("audit_enabled", True))


def _client_fingerprint(request: Optional[Request]) -> str:
    host = ""
    if request and request.client:
        host = request.client.host or ""
    if not host:
        host = "unknown"
    return hashlib.sha256(host.encode("utf-8")).hexdigest()[:12]


def _audit_log(event: str, request: Optional[Request] = None, status: str = "", detail: str = "") -> None:
    if not _audit_enabled():
        return
    try:
        audit_dir = Path(constants.METADATA_DIR) / "api_gateway" / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        record = {
            "timestamp": now.isoformat(),
            "event": event,
            "status": status,
            "method": request.method if request else "",
            "path": request.url.path if request else "",
            "client": _client_fingerprint(request),
        }
        if detail:
            record["detail"] = str(detail)[:500]
        audit_path = audit_dir / f"api_gateway_audit_{now.strftime('%Y-%m-%d')}.jsonl"
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("Failed to write API Gateway audit log: %s", e)


def _is_public_bind_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    return normalized in {"0.0.0.0", "::"} or (normalized and normalized not in _LOCAL_HOSTS)


def _rate_limit_settings_for_path(path: str) -> tuple[bool, int, int, str]:
    settings = _settings()
    if not bool(settings.get("rate_limit_enabled", True)):
        return False, 0, 60, "disabled"
    window = int(settings.get("rate_limit_window_seconds", 60) or 60)
    if "/chat" in path or "/voice/transcribe" in path or "/tts" in path or "/uploads" in path:
        return True, int(settings.get("rate_limit_heavy_per_minute", 30) or 30), window, "heavy"
    if "/events" in path:
        return True, int(settings.get("rate_limit_events_per_minute", 60) or 60), window, "events"
    return True, int(settings.get("rate_limit_general_per_minute", 240) or 240), window, "general"


def _rate_limit_key(request: Request, bucket: str) -> tuple[str, str]:
    token = ""
    authorization = request.headers.get("authorization") or ""
    if authorization.startswith("Bearer "):
        token = hashlib.sha256(authorization[len("Bearer "):].strip().encode("utf-8")).hexdigest()[:12]
    identity = token or _client_fingerprint(request)
    return identity, bucket


def _origin_allowed_for_atelier(origin: str) -> bool:
    if not _atelier_api_enabled():
        return False
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    settings = _atelier_settings()
    atelier_port = int(settings.get("port", 8765) or 8765)
    https_port = int(settings.get("tailscale_https_port", 8443) or 8443)
    origin_port = parsed.port
    if parsed.scheme == "http" and origin_port == atelier_port:
        return True
    if parsed.scheme == "https" and origin_port == https_port:
        return True
    return False


def _apply_atelier_cors_headers(response: Response, origin: str) -> None:
    if origin and _origin_allowed_for_atelier(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"


@app.middleware("http")
async def _atelier_cors_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    _apply_atelier_cors_headers(response, request.headers.get("origin", ""))
    return response


def _check_rate_limit(request: Request) -> tuple[bool, int, str]:
    enabled, limit, window, bucket = _rate_limit_settings_for_path(request.url.path)
    if not enabled or limit <= 0:
        return True, 0, bucket
    now = time.monotonic()
    key = _rate_limit_key(request, bucket)
    with _rate_limit_lock:
        hits = _rate_limit_buckets[key]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            retry_after = max(1, int(window - (now - hits[0])))
            return False, retry_after, bucket
        hits.append(now)
    return True, 0, bucket


def _audit_management_success(request: Request, action: str, detail: str = "") -> None:
    _audit_log(f"management:{action}", request, "success", detail)


def _atelier_api_path_tail(path: str, room_id: str) -> Optional[str]:
    prefix = f"/api/v1/rooms/{room_id}/"
    if not path.startswith(prefix):
        return None
    return path[len(prefix):].strip("/")


def _required_atelier_app_scope(tail: str, method: str) -> Optional[str]:
    normalized_method = str(method or "").upper()
    normalized_tail = str(tail or "").strip("/")
    if normalized_method == "GET":
        if normalized_tail.startswith("notes/"):
            return atelier_tokens.ATELIER_SCOPE_READ_NOTES
        return _ATELIER_FINE_READ_PATHS.get(normalized_tail)
    if normalized_method == "POST":
        if _is_twitter_draft_outward_tail(normalized_tail):
            return atelier_tokens.ATELIER_SCOPE_POST_TWITTER
        return _ATELIER_FINE_WRITE_PATHS.get(normalized_tail)
    return None


def _is_twitter_draft_outward_tail(tail: str) -> bool:
    parts = [part for part in str(tail or "").strip("/").split("/") if part]
    return (
        len(parts) == 4
        and parts[0] == "twitter"
        and parts[1] == "drafts"
        and bool(parts[2])
        and parts[3] in {"approve", "reject"}
    )


def _authorize_atelier_scope_token(request: Request, supplied: str) -> bool:
    if not _atelier_api_enabled():
        return False
    payload = atelier_tokens.verify_atelier_token(supplied)
    if not payload:
        return False

    room_id = str(payload.get("room_id") or "").strip()
    request_room = str(request.path_params.get("room_id") or "").strip()
    if not request_room or request_room != room_id:
        _audit_log("atelier_scope_room_denied", request, "blocked", f"room={room_id}")
        raise HTTPException(status_code=403, detail="Atelier token room mismatch")

    tail = _atelier_api_path_tail(request.url.path, room_id)
    if not tail:
        _audit_log("atelier_scope_path_denied", request, "blocked", f"scope={payload.get('scope')}")
        raise HTTPException(status_code=403, detail="Endpoint is not allowed for this token")

    allowed = set(_ATELIER_SAFE_EXACT_PATHS)
    app_id = str(payload.get("app_id") or "").strip()
    if request.method == "GET" and not app_id and payload.get("scope") == atelier_tokens.ATELIER_SCOPE_FULL:
        allowed.update(_ATELIER_FULL_EXACT_PATHS)
    if request.method == "GET" and tail in allowed:
        return True

    required_scope = _required_atelier_app_scope(tail, request.method)
    if not app_id or not required_scope:
        _audit_log(
            "atelier_scope_path_denied",
            request,
            "blocked",
            f"scope={payload.get('scope')} method={request.method} tail={tail}",
        )
        raise HTTPException(status_code=403, detail="Endpoint is not allowed for this token")
    # アトリエアプリは同一オリジン配信なので、per-app 分離は path 由来の app_id を
    # 署名済みトークンに入れ、毎リクエストで台帳へ照合する軽い境界として扱う。
    if not atelier_app_grants.has_grant(room_id, app_id, required_scope):
        atelier_app_grants.record_audit(
            room_id,
            "authorize",
            app_id,
            required_scope,
            "blocked",
            request.url.path,
        )
        _audit_log("atelier_scope_grant_denied", request, "blocked", f"app_id={app_id} scope={required_scope}")
        raise HTTPException(status_code=403, detail="Atelier app grant is required")
    atelier_app_grants.record_audit(
        room_id,
        "authorize",
        app_id,
        required_scope,
        "allowed",
        request.url.path,
    )
    return True


def require_api_auth(request: Request, authorization: str = Header(default="")) -> None:
    allowed, retry_after, bucket = _check_rate_limit(request)
    if not allowed:
        _audit_log("rate_limited", request, "blocked", f"bucket={bucket} retry_after={retry_after}")
        raise HTTPException(
            status_code=429,
            detail="API rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )
    settings = _settings()
    token = _expected_token()
    require_auth = bool(settings.get("require_auth", True))
    host = str(settings.get("host") or "127.0.0.1")
    if not require_auth and _is_public_bind_host(host):
        _audit_log("auth_public_without_token", request, "blocked", f"host={host}")
        raise HTTPException(status_code=403, detail="API auth cannot be disabled when API host is public")
    if not require_auth:
        return
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        _audit_log("auth_missing", request, "blocked")
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    supplied = authorization[len(prefix):].strip()
    if token and secrets.compare_digest(supplied, token):
        return
    if _authorize_atelier_scope_token(request, supplied):
        return
    if not token:
        _audit_log("auth_token_missing_config", request, "blocked")
        raise HTTPException(status_code=403, detail="API auth token is not configured")
    else:
        _audit_log("auth_invalid", request, "blocked")
        raise HTTPException(status_code=403, detail="Invalid token")


@app.options("/{requested_path:path}", include_in_schema=False)
def atelier_cors_preflight(request: Request) -> Response:
    origin = request.headers.get("origin", "")
    if not _origin_allowed_for_atelier(origin):
        raise HTTPException(status_code=404, detail="not found")
    response = Response(status_code=204)
    _apply_atelier_cors_headers(response, origin)
    return response


@app.get("/health")
def health_check() -> dict:
    return {"status": "ok"}


@app.get("/lite/", include_in_schema=False)
def lite_app() -> FileResponse:
    return FileResponse("mobile_app/index.html")


@app.get("/lite", include_in_schema=False)
def lite_app_redirect() -> RedirectResponse:
    return RedirectResponse(url="/lite/", status_code=307)


@app.get("/lite/manifest.webmanifest", include_in_schema=False)
def lite_manifest() -> FileResponse:
    return FileResponse("mobile_app/manifest.webmanifest", media_type="application/manifest+json")


@app.get("/lite/service-worker.js", include_in_schema=False)
def lite_service_worker() -> FileResponse:
    return FileResponse("mobile_app/service-worker.js", media_type="application/javascript")


@app.get("/lite/icon.png", include_in_schema=False)
def lite_icon() -> FileResponse:
    return FileResponse("icon.png", media_type="image/png")


@app.get("/lite/icon-maskable.png", include_in_schema=False)
def lite_icon_maskable() -> FileResponse:
    return FileResponse("icon-maskable.png", media_type="image/png")


@app.get("/lite/badge.png", include_in_schema=False)
def lite_badge() -> FileResponse:
    return FileResponse("mobile_app/badge.png", media_type="image/png")


@app.get("/api/v1/rooms", response_model=list[RoomSummary], dependencies=[Depends(require_api_auth)])
def rooms() -> list[RoomSummary]:
    return service.list_rooms()


@app.get("/api/v1/rooms/{room_id}/status", response_model=RoomStatus, dependencies=[Depends(require_api_auth)])
def room_status(room_id: str) -> RoomStatus:
    try:
        return service.get_room_status(room_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/v1/rooms/{room_id}/chat/history", response_model=ChatHistoryResponse, dependencies=[Depends(require_api_auth)])
def room_chat_history(
    room_id: str,
    limit: int = Query(12, ge=1, le=50),
) -> ChatHistoryResponse:
    try:
        return service.get_recent_chat_history(room_id, limit)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/api/v1/rooms/{room_id}/chat", response_model=ChatResponse, dependencies=[Depends(require_api_auth)])
async def room_chat(room_id: str, request: ChatRequest) -> ChatResponse:
    if request.stream:
        raise HTTPException(status_code=501, detail="Streaming responses are not implemented yet")
    try:
        return await service.chat(room_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/v1/rooms/{room_id}/uploads", response_model=UploadResponse, dependencies=[Depends(require_api_auth)])
async def room_upload(room_id: str, file: UploadFile = File(...)) -> UploadResponse:
    try:
        data = await file.read()
        return await service.save_upload(room_id, file.filename or "upload", file.content_type or "", data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/v1/rooms/{room_id}/voice/transcribe", response_model=TranscriptionResponse, dependencies=[Depends(require_api_auth)])
async def room_voice_transcribe(room_id: str, file: UploadFile = File(...)) -> TranscriptionResponse:
    try:
        data = await file.read()
        return await service.transcribe_voice(room_id, file.filename or "voice.webm", file.content_type or "", data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/v1/rooms/{room_id}/tts", response_model=TtsResponse, dependencies=[Depends(require_api_auth)])
async def room_tts(room_id: str, request: TtsRequest) -> TtsResponse:
    try:
        return await service.synthesize_speech(room_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/v1/audio", dependencies=[Depends(require_api_auth)])
def audio(path: str = Query(..., min_length=1)) -> FileResponse:
    try:
        audio_path = service.resolve_audio_path(path)
        return FileResponse(str(audio_path))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/v1/assets", dependencies=[Depends(require_api_auth)])
def asset(path: str = Query(..., min_length=1)) -> FileResponse:
    try:
        asset_path = service.resolve_asset_path(path)
        return FileResponse(str(asset_path))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/api/v1/rooms/{room_id}/events", response_model=EventResponse, dependencies=[Depends(require_api_auth)])
def room_events(room_id: str, request: EventRequest) -> EventResponse:
    try:
        return service.record_event(room_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get(
    "/api/v1/notifications/events/settings",
    response_model=EventNotificationSettingsResponse,
    dependencies=[Depends(require_api_auth)],
)
def event_notification_settings() -> EventNotificationSettingsResponse:
    return service.get_event_notification_settings()


@app.put(
    "/api/v1/notifications/events/settings",
    response_model=EventNotificationSettingsResponse,
    dependencies=[Depends(require_api_auth)],
)
def update_event_notification_settings(
    request: Request,
    settings_request: EventNotificationSettingsRequest,
) -> EventNotificationSettingsResponse:
    try:
        response = service.update_event_notification_settings(settings_request)
        _audit_management_success(request, "event_notification_settings")
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/v1/push/vapid-public-key", response_model=PushPublicKeyResponse, dependencies=[Depends(require_api_auth)])
def push_vapid_public_key() -> PushPublicKeyResponse:
    return service.get_push_public_key()


@app.post(
    "/api/v1/rooms/{room_id}/push/subscriptions",
    response_model=PushSubscriptionResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_push_subscribe(room_id: str, request: PushSubscriptionRequest) -> PushSubscriptionResponse:
    try:
        return service.subscribe_push(room_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.delete(
    "/api/v1/rooms/{room_id}/push/subscriptions/{subscription_id}",
    response_model=PushSubscriptionResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_push_unsubscribe(request: Request, room_id: str, subscription_id: str) -> PushSubscriptionResponse:
    try:
        response = service.unsubscribe_push(room_id, subscription_id)
        _audit_management_success(request, "push_unsubscribe", f"room={room_id} status={response.status}")
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get(
    "/api/v1/rooms/{room_id}/push/status",
    response_model=PushStatusResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_push_status(room_id: str) -> PushStatusResponse:
    try:
        return service.get_push_status(room_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post(
    "/api/v1/rooms/{room_id}/push/test",
    response_model=PushSendResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_push_test(room_id: str, request: PushTestRequest) -> PushSendResponse:
    try:
        return service.send_push_test(room_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/v1/rooms/{room_id}/memory/search", response_model=MemorySearchResponse, dependencies=[Depends(require_api_auth)])
def memory_search(
    room_id: str,
    query: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
) -> MemorySearchResponse:
    try:
        return service.search_memory(room_id, query, limit)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/v1/rooms/{room_id}/letters", response_model=LetterListResponse, dependencies=[Depends(require_api_auth)])
def room_letters(
    room_id: str,
    limit: int = Query(100, ge=1, le=200, description="取得する最大件数（新しい順）"),
) -> LetterListResponse:
    try:
        return service.list_letters(room_id, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/v1/rooms/{room_id}/letters/{letter_id}", response_model=LetterDetailResponse, dependencies=[Depends(require_api_auth)])
def room_letter(
    room_id: str,
    letter_id: str,
    mark_read: bool = Query(True, description="開いた手紙を既読にする（本体UIと同じ挙動）"),
) -> LetterDetailResponse:
    try:
        return service.get_letter(room_id, letter_id, mark_read=mark_read)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/v1/rooms/{room_id}/notes/{note_type}", response_model=NoteResponse, dependencies=[Depends(require_api_auth)])
def room_note(
    room_id: str,
    note_type: str,
    headings_only: bool = Query(False, description="見出しリストだけを返す"),
    heading: str = Query("", description="特定の見出しセクションだけを返す"),
) -> NoteResponse:
    try:
        return service.get_note(room_id, note_type, headings_only=headings_only, heading=heading)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.put("/api/v1/rooms/{room_id}/notes/{note_type}", response_model=NoteResponse, dependencies=[Depends(require_api_auth)])
def room_note_update(
    room_id: str,
    note_type: str,
    request: NoteUpdateRequest,
) -> NoteResponse:
    try:
        return service.update_note(room_id, note_type, request)
    except service.NoteConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/v1/rooms/{room_id}/calendar/events", response_model=CalendarEventsResponse, dependencies=[Depends(require_api_auth)])
def room_calendar_events(
    room_id: str,
    days: int = Query(7, ge=1, le=31),
) -> CalendarEventsResponse:
    try:
        return service.get_calendar_events(room_id, days)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/api/v1/rooms/{room_id}/calendar/events", response_model=CalendarEventCreateResponse, dependencies=[Depends(require_api_auth)])
def room_calendar_event_create(
    room_id: str,
    request: CalendarEventCreateRequest,
) -> CalendarEventCreateResponse:
    try:
        return service.create_calendar_event(room_id, request)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/v1/rooms/{room_id}/twitter/drafts", response_model=TwitterDraftListResponse, dependencies=[Depends(require_api_auth)])
def twitter_drafts(room_id: str) -> TwitterDraftListResponse:
    try:
        return service.list_twitter_drafts(room_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post(
    "/api/v1/rooms/{room_id}/twitter/drafts/{draft_id}/approve",
    response_model=TwitterDraftActionResponse,
    dependencies=[Depends(require_api_auth)],
)
def twitter_draft_approve(
    http_request: Request,
    room_id: str,
    draft_id: str,
    request: TwitterDraftActionRequest,
) -> TwitterDraftActionResponse:
    try:
        response = service.approve_twitter_draft(room_id, draft_id, request)
        _audit_management_success(http_request, "twitter_draft_approve", f"room={room_id} posted={response.posted}")
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post(
    "/api/v1/rooms/{room_id}/twitter/drafts/{draft_id}/reject",
    response_model=TwitterDraftActionResponse,
    dependencies=[Depends(require_api_auth)],
)
def twitter_draft_reject(http_request: Request, room_id: str, draft_id: str) -> TwitterDraftActionResponse:
    try:
        response = service.reject_twitter_draft(room_id, draft_id)
        _audit_management_success(http_request, "twitter_draft_reject", f"room={room_id} status={response.status}")
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/v1/rooms/{room_id}/locations", response_model=LocationListResponse, dependencies=[Depends(require_api_auth)])
def room_locations(room_id: str) -> LocationListResponse:
    try:
        return service.list_locations(room_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/api/v1/rooms/{room_id}/location", response_model=LocationSetResponse, dependencies=[Depends(require_api_auth)])
def room_location_set(http_request: Request, room_id: str, request: LocationSetRequest) -> LocationSetResponse:
    try:
        response = service.set_location(room_id, request)
        _audit_management_success(http_request, "location_set", f"room={room_id}")
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/v1/rooms/{room_id}/autonomy", response_model=AutonomyStatusResponse, dependencies=[Depends(require_api_auth)])
def room_autonomy(room_id: str) -> AutonomyStatusResponse:
    try:
        return service.get_autonomy_status(room_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post(
    "/api/v1/rooms/{room_id}/autonomy/preset",
    response_model=AutonomyPresetResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_autonomy_preset(
    http_request: Request,
    room_id: str,
    request: AutonomyPresetRequest,
) -> AutonomyPresetResponse:
    try:
        response = service.set_autonomy_preset(room_id, request)
        _audit_management_success(http_request, "autonomy_preset", f"room={room_id} preset={request.preset}")
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def start_server(port: Optional[int] = None, host: Optional[str] = None, daemon: bool = True) -> None:
    global _server, _server_thread
    if _server_thread and _server_thread.is_alive():
        print("--- [API Gateway] 既に実行中です ---")
        return

    settings = _settings()
    host = host or settings.get("host") or "127.0.0.1"
    port = int(port or settings.get("port") or 8000)
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = "%(asctime)s - uvicorn.access - %(levelname)s - %(message)s"
    config = uvicorn.Config(app, host=host, port=port, log_level="info", log_config=log_config)
    _server = uvicorn.Server(config)

    def run_server() -> None:
        print(f"--- [API Gateway] http://{host}:{port} で開始しました ---")
        _server.run()

    _server_thread = threading.Thread(target=run_server, daemon=daemon)
    _server_thread.start()


def stop_server() -> None:
    global _server
    if _server:
        _server.should_exit = True
        print("--- [API Gateway] 停止しました ---")
