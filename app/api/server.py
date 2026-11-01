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
from urllib.parse import quote, urlparse

import uvicorn
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config_manager
import constants
import atelier_app_grants
import lite_travel
from api import atelier_tokens
from api import capabilities as api_capabilities
from api import service
from api import lite_features, lite_runtime_controls
from api import lite_item_generation, lite_response_settings
from api import lite_location_library, lite_location_generation
from api import lite_thought_translation
from api.schemas import (
    ApiCapabilitiesResponse,
    AutonomyPresetRequest,
    AutonomyPresetResponse,
    CalendarEventCreateRequest,
    CalendarEventCreateResponse,
    CalendarEventsResponse,
    AutonomyStatusResponse,
    ChatHistoryResponse,
    ChatRegenerateRequest,
    ChatRequest,
    ChatResponse,
    EventNotificationSettingsRequest,
    EventNotificationSettingsResponse,
    EventRequest,
    EventResponse,
    LetterDetailResponse,
    LetterListResponse,
    ItemActionRequest,
    ItemActionResponse,
    ItemListResponse,
    LocationListResponse,
    LocationSetRequest,
    LocationSetResponse,
    LiteStandbyPrepareRequest,
    LiteTravelReturnRequest,
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
_ATELIER_FULL_EXACT_PATHS = {"chat/history", "memory/search"}


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


def _normalized_https_origin(value: str) -> str:
    """Pathやuserinfoを含まないHTTPS originだけを正規化する。"""

    try:
        parsed = urlparse(str(value or "").strip())
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return ""
    host = parsed.hostname.lower()
    return f"https://{host}{f':{port}' if port not in {None, 443} else ''}"


def _origin_allowed_for_lite(origin: str) -> bool:
    """保存済みWorker URLと完全一致するLite PWA originだけを許可する。"""

    settings = config_manager.CONFIG_GLOBAL.get("lite_travel_settings", {}) or {}
    configured = _normalized_https_origin(str(settings.get("worker_url") or ""))
    requested = _normalized_https_origin(origin)
    return bool(configured and requested and secrets.compare_digest(configured, requested))


def _origin_allowed_for_browser_api(origin: str) -> bool:
    return _origin_allowed_for_atelier(origin) or _origin_allowed_for_lite(origin)


def _apply_browser_api_cors_headers(
    response: Response,
    origin: str,
    *,
    private_network_requested: bool = False,
) -> None:
    if origin and _origin_allowed_for_browser_api(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"
        if private_network_requested and _origin_allowed_for_lite(origin):
            response.headers["Access-Control-Allow-Private-Network"] = "true"
            response.headers["Vary"] = "Origin, Access-Control-Request-Private-Network"


@app.middleware("http")
async def _browser_api_cors_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    _apply_browser_api_cors_headers(response, request.headers.get("origin", ""))
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

    app_id = str(payload.get("app_id") or "").strip()
    access, required_scope = api_capabilities.atelier_access_for(tail, request.method)
    if access == "safe":
        return True
    if (
        request.method == "GET"
        and not app_id
        and payload.get("scope") == atelier_tokens.ATELIER_SCOPE_FULL
        and tail in _ATELIER_FULL_EXACT_PATHS
    ):
        return True
    if access != "approval" or not app_id or not required_scope:
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


def require_owner_api_auth(request: Request, authorization: str = Header(default="")) -> None:
    """所有者Tokenだけを許可するルート用の認証依存関係。"""

    require_api_auth(request, authorization)
    settings = _settings()
    local_without_auth = not settings.get("require_auth", True) and not _is_public_bind_host(
        str(settings.get("host") or "127.0.0.1")
    )
    if local_without_auth:
        return
    token = _expected_token()
    supplied = authorization[len("Bearer "):].strip() if authorization.startswith("Bearer ") else ""
    if not token or not secrets.compare_digest(supplied, token):
        _audit_log("owner_auth_denied", request, "blocked")
        raise HTTPException(status_code=403, detail="Owner token is required")


@app.options("/{requested_path:path}", include_in_schema=False)
def browser_api_cors_preflight(request: Request) -> Response:
    origin = request.headers.get("origin", "")
    if not _origin_allowed_for_browser_api(origin):
        raise HTTPException(status_code=404, detail="not found")
    response = Response(status_code=204)
    _apply_browser_api_cors_headers(
        response,
        origin,
        private_network_requested=(
            request.headers.get("access-control-request-private-network", "").lower() == "true"
        ),
    )
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


@app.post("/api/v1/lite-travel/standby", dependencies=[Depends(require_api_auth)])
def prepare_lite_travel_standby(body: LiteStandbyPrepareRequest) -> dict:
    try:
        return lite_travel.prepare_standby_snapshot(
            body.room_ids,
            parallel_rooms=body.parallel_room_ids,
            retention_days=body.retention_days,
            automatic=body.automatic,
            include_core_memory=body.include_core_memory,
            include_episodic_memory=body.include_episodic_memory,
            episodic_memory_days=body.episodic_memory_days,
            recent_message_limit=body.recent_message_limit,
        )
    except lite_travel.LiteTravelError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/lite-travel/standby", dependencies=[Depends(require_api_auth)])
def get_lite_travel_standby() -> dict:
    return {"manifest": lite_travel.latest_standby_manifest()}


@app.get("/api/v1/lite-travel/diagnostics", dependencies=[Depends(require_api_auth)])
def get_lite_travel_diagnostics() -> dict:
    return lite_travel.diagnose_worker()


@app.post("/api/v1/lite-travel/return", dependencies=[Depends(require_api_auth)])
def return_lite_travel_home(body: LiteTravelReturnRequest) -> dict:
    try:
        return lite_travel.online_return(body.travel_session_id)
    except lite_travel.LiteTravelError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/rooms", response_model=list[RoomSummary], dependencies=[Depends(require_api_auth)])
def rooms() -> list[RoomSummary]:
    return service.list_rooms()


@app.get("/api/v1/rooms/{room_id}/status", response_model=RoomStatus, dependencies=[Depends(require_api_auth)])
def room_status(room_id: str) -> RoomStatus:
    try:
        return service.get_room_status(room_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/rooms/{room_id}/capabilities",
    response_model=ApiCapabilitiesResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_capabilities(room_id: str) -> ApiCapabilitiesResponse:
    try:
        return service.get_api_capabilities(room_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/v1/rooms/{room_id}/chat/history", response_model=ChatHistoryResponse, dependencies=[Depends(require_api_auth)])
def room_chat_history(
    room_id: str,
    limit: int = Query(12, ge=1, le=50),
    include_thoughts: bool = False,
    request: Request = None,
) -> ChatHistoryResponse:
    if include_thoughts:
        # read_chat grants cover the ordinary transcript only. Thought text is
        # an explicit owner opt-in and still follows the room display setting.
        settings = _settings()
        local_without_auth = not settings.get("require_auth", True) and not _is_public_bind_host(
            str(settings.get("host") or "127.0.0.1")
        )
        authorization = request.headers.get("authorization", "") if request is not None else ""
        token = _expected_token()
        supplied = authorization[len("Bearer "):].strip() if authorization.startswith("Bearer ") else ""
        if not local_without_auth and not (token and secrets.compare_digest(supplied, token)):
            raise HTTPException(status_code=403, detail="Thought logs require the owner token")
    try:
        if include_thoughts:
            return service.get_recent_chat_history(room_id, limit, include_thoughts=True)
        return service.get_recent_chat_history(room_id, limit)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post(
    "/api/v1/rooms/{room_id}/chat/thoughts/translate",
    response_model=lite_thought_translation.LiteThoughtTranslationResponse,
    dependencies=[Depends(require_owner_api_auth)],
)
def room_chat_thoughts_translate(
    room_id: str,
    request: lite_thought_translation.LiteThoughtTranslationRequest,
) -> lite_thought_translation.LiteThoughtTranslationResponse:
    try:
        translated = lite_thought_translation.translate_thought_log(room_id, request.text)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Room not found") from exc
    except lite_thought_translation.ThoughtTranslationDisabledError as exc:
        raise HTTPException(status_code=403, detail="Thought translation is disabled") from exc
    except lite_thought_translation.ThoughtTranslationFailedError as exc:
        raise HTTPException(status_code=502, detail="Thought translation failed") from exc
    return lite_thought_translation.LiteThoughtTranslationResponse(text=translated)


@app.post("/api/v1/rooms/{room_id}/chat", response_model=ChatResponse, dependencies=[Depends(require_api_auth)])
async def room_chat(room_id: str, request: ChatRequest) -> ChatResponse:
    if request.stream:
        raise HTTPException(status_code=501, detail="Streaming responses are not implemented yet")
    try:
        return await service.chat(room_id, request)
    except lite_runtime_controls.LiteRequestStopped as e:
        raise HTTPException(status_code=409, detail={"code": "response_stopped", "client_message_id": e.request_id}) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post(
    "/api/v1/rooms/{room_id}/chat/regenerate",
    response_model=ChatResponse,
    dependencies=[Depends(require_api_auth)],
)
async def room_chat_regenerate(room_id: str, request: ChatRegenerateRequest) -> ChatResponse:
    try:
        return await service.regenerate_chat(room_id, request)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


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


@app.get("/api/v1/rooms/{room_id}/audio", dependencies=[Depends(require_api_auth)])
def room_audio(room_id: str, path: str = Query(..., min_length=1)) -> FileResponse:
    try:
        audio_path = service.resolve_room_audio_path(room_id, path)
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


@app.get("/api/v1/rooms/{room_id}/assets", dependencies=[Depends(require_api_auth)])
def room_asset(room_id: str, path: str = Query(..., min_length=1)) -> FileResponse:
    try:
        asset_path = service.resolve_room_asset_path(room_id, path)
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


@app.get(
    "/api/v1/rooms/{room_id}/push/vapid-public-key",
    response_model=PushPublicKeyResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_push_vapid_public_key(room_id: str) -> PushPublicKeyResponse:
    try:
        return service.get_room_push_public_key(room_id)
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
    latest: bool = Query(False, description="見出し取得なしで最新の一件を返す"),
) -> NoteResponse:
    try:
        return lite_features.get_memory_note(room_id, note_type, headings_only=headings_only, heading=heading, latest=latest)
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


@app.get("/api/v1/rooms/{room_id}/items", response_model=ItemListResponse, dependencies=[Depends(require_api_auth)])
def room_items(
    room_id: str,
    target: str = Query("user"),
    location: str = Query(""),
) -> ItemListResponse:
    try:
        return service.list_items(room_id, target=target, location=location)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post(
    "/api/v1/rooms/{room_id}/items/actions",
    response_model=ItemActionResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_item_action(
    http_request: Request,
    room_id: str,
    request: ItemActionRequest,
) -> ItemActionResponse:
    try:
        response = service.perform_item_action(room_id, request)
        _audit_management_success(
            http_request,
            "item_action",
            f"room={room_id} action={request.action} replayed={response.replayed}",
        )
        return response
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


class LiteStopRequest(BaseModel):
    client_message_id: str = Field(..., min_length=1, max_length=200)


class LiteTemporaryLocationRequest(BaseModel):
    scenery_text: str = Field(default="", max_length=4000)
    location_name: Optional[str] = Field(default=None, max_length=120)
    source_description: Optional[str] = Field(default=None, max_length=500)
    active: bool = True
    # 既存room assetの参照。省略時は現在の画像を保持する。
    image_path: Optional[str] = Field(default=None, max_length=4096)
    # active=true の適用時だけ意味を持つ明示的な画像解除。
    clear_image: bool = False


def _lite_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except lite_location_library.LiteLocationConflictError as e:
        raise HTTPException(status_code=409, detail={"code": e.code, "message": str(e)}) from e
    except lite_response_settings.LiteResponseSettingsConflictError as e:
        raise HTTPException(status_code=409, detail={"code": e.code, "message": str(e)}) from e
    except lite_features.LiteMemoConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _temporary_location_asset_reference(
    room_id: str,
    state: lite_runtime_controls.TemporaryLocationState,
) -> tuple[Optional[str], Optional[str]]:
    """同一roomで配信可能な場合だけ、安全な相対参照とURLを返す。"""

    if not state.image_path:
        return None, None
    try:
        asset_path = service.resolve_room_asset_path(room_id, state.image_path)
    except ValueError:
        # 本体UIの既存一時ファイルなど、room asset境界外の画像はhas_imageだけ公開する。
        return None, None
    try:
        room_root = (Path(constants.ROOMS_DIR) / room_id).resolve()
        relative_path = asset_path.resolve().relative_to(room_root).as_posix()
    except (OSError, ValueError):
        return None, None
    if not relative_path or relative_path == ".":
        return None, None
    encoded_room_id = quote(room_id, safe="")
    return relative_path, f"/api/v1/rooms/{encoded_room_id}/temporary-location/image"


def _temporary_location_response(
    room_id: str,
    state: lite_runtime_controls.TemporaryLocationState,
) -> dict:
    image_path, image_asset_url = _temporary_location_asset_reference(room_id, state)
    return state.as_dict(
        public_image_path=image_path,
        image_asset_url=image_asset_url,
    )


@app.get("/api/v1/rooms/{room_id}/api-key", response_model=lite_features.LiteApiKeyOptionsResponse, dependencies=[Depends(require_api_auth)])
def room_api_key(room_id: str):
    return _lite_call(lite_features.list_api_key_options, room_id)


@app.put("/api/v1/rooms/{room_id}/api-key", response_model=lite_features.LiteApiKeySelectionResponse, dependencies=[Depends(require_api_auth)])
def room_api_key_select(room_id: str, request: lite_features.LiteApiKeySelectionRequest):
    return _lite_call(lite_features.select_room_api_key, room_id, request)


@app.delete("/api/v1/rooms/{room_id}/api-key", response_model=lite_features.LiteApiKeySelectionResponse, dependencies=[Depends(require_api_auth)])
def room_api_key_reset(room_id: str):
    return _lite_call(lite_features.reset_room_api_key_to_common, room_id)


@app.get(
    "/api/v1/rooms/{room_id}/response-settings",
    response_model=lite_response_settings.LiteResponseSettingsResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_response_settings(room_id: str):
    return _lite_call(lite_response_settings.get_response_settings, room_id)


@app.put(
    "/api/v1/rooms/{room_id}/response-settings",
    response_model=lite_response_settings.LiteResponseSettingsResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_response_settings_update(room_id: str, request: lite_response_settings.LiteResponseSettingsUpdateRequest):
    return _lite_call(lite_response_settings.update_response_settings, room_id, request)


@app.delete(
    "/api/v1/rooms/{room_id}/response-settings",
    response_model=lite_response_settings.LiteResponseSettingsResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_response_settings_reset(room_id: str, request: lite_response_settings.LiteResponseSettingsDeleteRequest):
    return _lite_call(lite_response_settings.reset_response_settings, room_id, request)


@app.put("/api/v1/rooms/{room_id}/autonomy/settings", response_model=lite_features.LiteAutonomySettingsResponse, dependencies=[Depends(require_api_auth)])
def room_autonomy_settings(room_id: str, request: lite_features.LiteAutonomySettingsUpdateRequest):
    return _lite_call(lite_features.update_autonomy_settings, room_id, request)


@app.get("/api/v1/rooms/{room_id}/user-memo", response_model=lite_features.LiteUserMemoResponse, dependencies=[Depends(require_api_auth)])
def room_user_memo(room_id: str):
    return _lite_call(lite_features.get_user_memo, room_id)


@app.put("/api/v1/rooms/{room_id}/user-memo", response_model=lite_features.LiteUserMemoResponse, dependencies=[Depends(require_api_auth)])
def room_user_memo_save(room_id: str, request: lite_features.LiteUserMemoUpdateRequest):
    return _lite_call(lite_features.save_user_memo, room_id, request)


@app.post("/api/v1/rooms/{room_id}/items/food", response_model=lite_features.LiteFoodItemCreateResponse, dependencies=[Depends(require_api_auth)])
def room_food_create(room_id: str, request: lite_features.LiteFoodItemCreateRequest):
    return _lite_call(lite_features.create_food_item, room_id, request)


@app.post(
    "/api/v1/rooms/{room_id}/items/generate-profile",
    response_model=lite_item_generation.LiteItemGenerationResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_item_generate_profile(
    room_id: str,
    request: lite_item_generation.LiteItemProfileGenerateRequest,
):
    return _lite_call(lite_item_generation.generate_item_profile, room_id, request)


@app.post(
    "/api/v1/rooms/{room_id}/items/generate-image",
    response_model=lite_item_generation.LiteItemGenerationResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_item_generate_image(
    room_id: str,
    request: lite_item_generation.LiteItemImageGenerateRequest,
):
    return _lite_call(lite_item_generation.generate_item_image, room_id, request)


@app.post(
    "/api/v1/rooms/{room_id}/items/generated",
    response_model=lite_item_generation.LiteItemGenerationResponse,
    dependencies=[Depends(require_api_auth)],
)
def room_generated_item_save(
    room_id: str,
    request: lite_item_generation.LiteGeneratedItemSaveRequest,
):
    return _lite_call(lite_item_generation.save_generated_item, room_id, request)


@app.get("/api/v1/rooms/{room_id}/temporary-location", dependencies=[Depends(require_api_auth)])
def room_temporary_location(room_id: str):
    _lite_call(service._require_room, room_id)
    state = _lite_call(lite_runtime_controls.get_temporary_location, room_id)
    return _temporary_location_response(room_id, state)


@app.get("/api/v1/rooms/{room_id}/temporary-location/image", dependencies=[Depends(require_api_auth)])
def room_temporary_location_image(room_id: str) -> FileResponse:
    _lite_call(service._require_room, room_id)
    state = _lite_call(lite_runtime_controls.get_temporary_location, room_id)
    if not state.image_path:
        raise HTTPException(status_code=404, detail="temporary location image is not set")
    try:
        image_path = service.resolve_room_asset_path(room_id, state.image_path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(str(image_path))


@app.put("/api/v1/rooms/{room_id}/temporary-location", dependencies=[Depends(require_api_auth)])
def room_temporary_location_apply(room_id: str, request: LiteTemporaryLocationRequest):
    _lite_call(service._require_room, room_id)
    if lite_travel.is_presence_locked(room_id):
        raise HTTPException(status_code=409, detail="独立モード利用中は本体の現在地を変更できません")
    function = lite_runtime_controls.apply_temporary_location_text if request.active else lite_runtime_controls.deactivate_temporary_location
    if request.active:
        state = _lite_call(
            function,
            room_id,
            request.scenery_text,
            image_path=request.image_path,
            clear_image=request.clear_image,
            location_name=request.location_name,
            source_description=request.source_description,
        )
        return _temporary_location_response(room_id, state)
    state = _lite_call(function, room_id)
    return _temporary_location_response(room_id, state)


@app.get("/api/v1/rooms/{room_id}/temporary-location/saved", dependencies=[Depends(require_api_auth)])
def room_temporary_saved_locations(room_id: str):
    return _lite_call(lite_location_library.list_saved_locations, room_id)


@app.get("/api/v1/rooms/{room_id}/temporary-location/saved-entry", dependencies=[Depends(require_api_auth)])
def room_temporary_saved_location(room_id: str, name: str = Query(..., min_length=1, max_length=120)):
    return _lite_call(lite_location_library.get_saved_location, room_id, name)


@app.get("/api/v1/rooms/{room_id}/temporary-location/saved-image", dependencies=[Depends(require_api_auth)])
def room_temporary_saved_location_image(room_id: str, name: str = Query(..., min_length=1, max_length=120)):
    entry = _lite_call(lite_location_library.get_saved_location, room_id, name)
    if not entry.image_path:
        raise HTTPException(status_code=404, detail="saved location image is unavailable")
    path = _lite_call(lite_runtime_controls._validated_room_image_path, room_id, entry.image_path)
    return FileResponse(path)


@app.post("/api/v1/rooms/{room_id}/temporary-location/saved", dependencies=[Depends(require_api_auth)])
def room_temporary_saved_location_save(room_id: str, request: lite_location_library.LiteLocationSaveRequest):
    return _lite_call(lite_location_library.save_location, room_id, request)


@app.post("/api/v1/rooms/{room_id}/temporary-location/generate", dependencies=[Depends(require_api_auth)])
def room_temporary_location_generate(room_id: str, request: lite_location_generation.LiteLocationGenerateRequest):
    return _lite_call(lite_location_generation.generate_location, room_id, request)


@app.get("/api/v1/rooms/{room_id}/chat/active", dependencies=[Depends(require_api_auth)])
def room_chat_active(room_id: str):
    _lite_call(service._require_room, room_id)
    return {"room_id": room_id, "requests": [item.as_dict() for item in lite_runtime_controls.get_active_lite_requests(room_id)]}


@app.post("/api/v1/rooms/{room_id}/chat/stop", dependencies=[Depends(require_api_auth)])
def room_chat_stop(room_id: str, request: LiteStopRequest):
    _lite_call(service._require_room, room_id)
    return lite_runtime_controls.stop_lite_request(room_id, request.client_message_id).as_dict()


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
