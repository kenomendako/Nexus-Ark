from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class RoomSummary(BaseModel):
    room_id: str
    display_name: str
    description: str = ""
    current_location: str = ""


class RoomStatus(BaseModel):
    room_id: str
    display_name: str
    current_location: str = ""
    drives: Dict[str, float] = Field(default_factory=dict)
    current_expression: str = "neutral"
    arousal: float = 0.5
    active_goals_count: int = 0
    profile_image_path: Optional[str] = None
    updated_at: datetime


class ChatRequest(BaseModel):
    user_id: str = "external_user"
    message: str
    stream: bool = False
    source: str = "api"
    attachments: List[str] = Field(default_factory=list)
    client_message_id: Optional[str] = None


class ChatResponse(BaseModel):
    room_id: str
    reply: str
    arousal: float
    expression: str
    timestamp: datetime
    suggested_actions: List[str] = Field(default_factory=list)
    model: Optional[str] = None
    client_message_id: Optional[str] = None
    attachments: List[str] = Field(default_factory=list)


class ChatHistoryMessage(BaseModel):
    role: str
    speaker: str = ""
    content: str
    client_message_id: Optional[str] = None
    attachments: List[str] = Field(default_factory=list)


class ChatHistoryResponse(BaseModel):
    room_id: str
    messages: List[ChatHistoryMessage]


class UploadResponse(BaseModel):
    attachment_id: str
    filename: str
    mime_type: str
    size: int


class TranscriptionResponse(BaseModel):
    text: str
    provider: str
    model: Optional[str] = None
    uncertain: bool = False


class TtsRequest(BaseModel):
    text: str
    mode: str = "trim"
    max_chars: int = Field(default=1500, ge=400, le=3000)


class TtsResponse(BaseModel):
    audio_id: str
    audio_ids: List[str] = Field(default_factory=list)
    mime_type: str
    provider: str
    model: Optional[str] = None
    voice: Optional[str] = None
    mode: str = "trim"
    segment_count: int = 1
    truncated: bool = False
    notice: Optional[str] = None


EventImportance = Literal["low", "normal", "high", "critical"]


class EventRequest(BaseModel):
    event_type: str = Field(..., min_length=1)
    event_data: Dict[str, Any] = Field(default_factory=dict)
    trigger_notification: bool = False
    source: str = Field(default="api", min_length=1)
    summary: Optional[str] = Field(default=None, description="ペルソナへ伝える短い概要")
    details: Dict[str, Any] = Field(default_factory=dict, description="用途別の詳細データ")
    importance: EventImportance = Field(default="normal", description="イベント重要度")
    attachments: List[str] = Field(default_factory=list, description="添付IDまたは参照パス")

    @model_validator(mode="before")
    @classmethod
    def _promote_event_data_fields(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        event_data = values.get("event_data") or {}
        if not isinstance(event_data, dict):
            return values
        for key in ("summary", "details", "importance", "attachments"):
            if key not in values and key in event_data:
                values[key] = event_data[key]
        return values

    @model_validator(mode="after")
    def _mirror_official_fields_to_event_data(self) -> "EventRequest":
        data = dict(self.event_data or {})
        if self.summary is not None:
            data.setdefault("summary", self.summary)
        if self.details:
            data.setdefault("details", self.details)
        if self.importance:
            data.setdefault("importance", self.importance)
        if self.attachments:
            data.setdefault("attachments", self.attachments)
        self.event_data = data
        return self


class EventResponse(BaseModel):
    status: str
    should_interact: bool = False
    notification_text: Optional[str] = None
    notification_status: Optional[str] = None


EventNotificationMinimumImportance = Literal["high", "critical"]


class EventNotificationSettingsRequest(BaseModel):
    enabled: bool = True
    minimum_importance: EventNotificationMinimumImportance = "high"
    default_cooldown_seconds: int = Field(default=300, ge=0, le=86400)
    source_cooldowns: Dict[str, int] = Field(default_factory=dict)
    response_preview_enabled: bool = True


class EventNotificationSettingsResponse(EventNotificationSettingsRequest):
    status: str = "ok"


class PushPublicKeyResponse(BaseModel):
    public_key: str


class PushSubscriptionRequest(BaseModel):
    endpoint: str = Field(..., min_length=1)
    keys: Dict[str, str] = Field(default_factory=dict)
    user_agent: Optional[str] = None


class PushSubscriptionResponse(BaseModel):
    status: str
    subscription_count: int = 0
    detail: str = ""


class PushSubscriptionSummary(BaseModel):
    id: str
    endpoint_host: str = ""
    user_agent: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_success_at: str = ""
    last_failure_at: str = ""
    failure_count: int = 0


class PushStatusResponse(BaseModel):
    room_id: str
    has_vapid_keys: bool = False
    subscription_count: int = 0
    endpoints: list[str] = Field(default_factory=list)
    subscriptions: list[PushSubscriptionSummary] = Field(default_factory=list)
    cleaned_count: int = 0


class PushTestRequest(BaseModel):
    title: str = "Nexus Ark Lite"
    body: str = "Web Pushテストです。"


class PushSendResponse(BaseModel):
    status: str
    subscription_count: int = 0
    sent: int = 0
    failed: int = 0
    detail: str = ""


class MemorySearchResponse(BaseModel):
    query: str
    results: List[Dict[str, Any]]


NoteType = Literal["research", "creative"]


class NoteResponse(BaseModel):
    room_id: str
    note_type: NoteType
    title: str
    content: str
    updated_at: Optional[datetime] = None
    size: int = 0
    headings: List[str] = Field(default_factory=list)


class TwitterDraftSummary(BaseModel):
    id: str
    timestamp: str = ""
    room_name: str = ""
    content: str = ""
    warnings: List[str] = Field(default_factory=list)
    reply_to_url: Optional[str] = None
    reply_to_id: Optional[str] = None
    media_paths: List[str] = Field(default_factory=list)
    twitter_length: int = 0
    limit: int = 280


class TwitterDraftListResponse(BaseModel):
    room_id: str
    drafts: List[TwitterDraftSummary] = Field(default_factory=list)


class TwitterDraftActionRequest(BaseModel):
    content: str = Field(..., min_length=1)
    reply_to_url: Optional[str] = None
    media_paths: List[str] = Field(default_factory=list)


class TwitterDraftActionResponse(BaseModel):
    status: str
    detail: str = ""
    posted: bool = False
    post_url: Optional[str] = None
    error: Optional[str] = None


class LocationSummary(BaseModel):
    id: str
    name: str
    area: str = ""


class LocationListResponse(BaseModel):
    room_id: str
    current_location: str = ""
    locations: List[LocationSummary] = Field(default_factory=list)


class LocationSetRequest(BaseModel):
    location_id: str = Field(..., min_length=1)


class LocationSetResponse(BaseModel):
    room_id: str
    current_location: str
    status: str


class AutonomyStatusResponse(BaseModel):
    room_id: str
    enabled: bool = False
    inactivity_minutes: int = 120
    schedule_cooldown_minutes: int = 60
    quiet_hours_start: str = "00:00"
    quiet_hours_end: str = "07:00"
    preset: str = "normal"


class AutonomyPresetRequest(BaseModel):
    preset: Literal["quiet", "normal"]


class AutonomyPresetResponse(AutonomyStatusResponse):
    status: str
