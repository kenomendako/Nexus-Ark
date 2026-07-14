"""Memory Steward Phase 0 の内容非保持型観測基盤。

会話・記憶の正本を変更せず、列挙値、件数、経過時間、鍵付き参照だけを
TTL付きキャッシュへ記録する。観測失敗は必ず呼び出し元から分離する。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Iterable

import constants
from file_lock_utils import get_file_lock, safe_append_text


SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 14
MAX_DAILY_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
REF_RE = re.compile(r"^[0-9a-f]{16,64}$")
_SALT_CACHE: dict[str, bytes] = {}
EVENT_TYPES = {
    "context_generation",
    "wm_injection",
    "wm_token_estimate",
    "wm_operation",
    "wm_archive_sweep",
    "tool_outcome",
    "memory_provenance_available",
    "calendar_observation",
    "observer_health",
}
ENUMS = {
    "route": {"agent_context", "autonomous_context", "gemini_token_estimate", "tool", "ui", "cli", "calendar", "rag", "observer"},
    "outcome": {"observed", "success_reported", "success_verified", "partial", "error", "cancelled", "unknown"},
    "status": {"active", "archived", "blocked", "completed", "cancelled", "superseded", "expired", "legacy_active", "missing", "unknown"},
    "operation": {"read", "update", "patch", "switch", "reactivate", "link_thread", "link_goal", "archive_sweep", "inject", "estimate", "write", "cleanup"},
    "channel": {"tool", "ui", "autonomy", "system", "cli"},
    "tool_family": {"working_memory", "calendar", "delegation", "notes", "memory", "goal", "research", "external_side_effect", "read_only", "other"},
    "side_effect_class": {"none", "memory_write", "external_write", "workspace_write", "unknown"},
    "postcondition_evidence": {"verified", "reported_only", "missing", "not_applicable", "unknown"},
    "reported_status": {"ok", "error"},
    "time_bucket": {"all_day", "morning", "afternoon", "evening", "overnight", "unknown"},
    "store_type": {"working_memory", "core_memory", "diary_rag", "conversation", "entity", "episodic", "dream", "goal", "research_thread", "notepad", "unknown"},
    "selection_route": {"static_prompt", "rag", "keyword", "suggestive", "tool", "unknown"},
}
REF_FIELDS = {"turn_ref", "room_ref", "slot_ref", "content_ref", "event_ref", "series_ref", "calendar_ref", "source_ref", "action_ref"}
BOOL_FIELDS = {"archived", "active_selected", "content_changed", "all_day", "linked", "success", "wm_present"}
INT_FIELDS = {
    "char_count", "content_age_proxy_sec", "last_used_age_sec", "scanned_count", "archived_count",
    "selected_count", "dropped_count", "bytes_written", "duration_ms", "next_action_present",
}
COMMON_FIELDS = {"schema_version", "observed_at", "event_type", "route", "outcome", "room_ref", "turn_ref"}
EVENT_FIELDS = {
    "context_generation": {"wm_present"},
    "wm_injection": {"slot_ref", "content_ref", "status", "char_count", "content_age_proxy_sec", "last_used_age_sec", "archived", "active_selected", "next_action_present"},
    "wm_token_estimate": {"slot_ref", "content_ref", "status", "char_count", "content_age_proxy_sec", "last_used_age_sec", "archived", "active_selected", "next_action_present"},
    "wm_operation": {"slot_ref", "content_ref", "status", "operation", "channel", "content_changed", "next_action_present"},
    "wm_archive_sweep": {"operation", "scanned_count", "archived_count", "active_selected"},
    "tool_outcome": {"slot_ref", "action_ref", "tool_family", "side_effect_class", "postcondition_evidence", "reported_status"},
    "memory_provenance_available": {"store_type", "source_ref", "selection_route", "selected_count"},
    "calendar_observation": {"event_ref", "series_ref", "calendar_ref", "all_day", "time_bucket"},
    "observer_health": {"operation", "success", "duration_ms", "dropped_count", "bytes_written"},
}

SIDE_EFFECT_TOOL_NAMES = {
    "update_working_memory", "patch_working_memory", "switch_working_memory",
    "link_working_memory_to_research_thread", "link_working_memory_to_goal",
    "reactivate_working_memory_slot", "add_calendar_event", "update_calendar_event",
    "delete_calendar_event", "delegate_agent_task", "delegate_atelier_task",
    "delegate_deep_research", "plan_notepad_edit", "plan_research_notes_edit",
    "plan_creative_notes_edit", "manage_goals", "update_research_thread",
}


def is_enabled() -> bool:
    value = os.environ.get("NEXUS_ARK_MEMORY_STEWARD_PHASE0", "1").strip().lower()
    return value not in {"0", "false", "off", "no"}


def _cache_dir() -> Path:
    override = os.environ.get("NEXUS_ARK_MEMORY_STEWARD_CACHE_DIR", "").strip()
    return Path(override) if override else Path("cache") / "memory_steward"


def _salt() -> bytes:
    root = _cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".observer_salt"
    cache_key = str(path.resolve())
    cached = _SALT_CACHE.get(cache_key)
    if cached and path.exists():
        return cached
    if cached:
        _SALT_CACHE.pop(cache_key, None)
    lock = get_file_lock(str(path), timeout=0.2)
    with lock:
        if path.exists():
            value = path.read_bytes()
            if len(value) >= 32:
                _SALT_CACHE[cache_key] = value
                return value
        value = secrets.token_bytes(32)
        path.write_bytes(value)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _SALT_CACHE[cache_key] = value
        return value


def keyed_ref(value: Any) -> str:
    raw = str(value or "").encode("utf-8")
    return hmac.new(_salt(), raw, hashlib.sha256).hexdigest()[:24]


def new_turn_ref() -> str:
    return secrets.token_hex(12)


def _iso_now() -> str:
    return dt.datetime.now().astimezone().isoformat()


def _validate_event(event_type: str, record: dict[str, Any]) -> None:
    if event_type not in EVENT_TYPES:
        raise ValueError("unsupported event_type")
    allowed = COMMON_FIELDS | EVENT_FIELDS[event_type]
    if set(record) - allowed:
        raise ValueError("unknown observer fields")
    for key, value in record.items():
        if key in ENUMS and value not in ENUMS[key]:
            raise ValueError(f"unsupported enum: {key}")
        if key in REF_FIELDS and value is not None and not REF_RE.fullmatch(str(value)):
            raise ValueError(f"invalid ref: {key}")
        if key in BOOL_FIELDS and not isinstance(value, bool):
            raise ValueError(f"invalid bool: {key}")
        if key in INT_FIELDS and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError(f"invalid integer: {key}")
        if isinstance(value, str) and key not in ENUMS and key not in REF_FIELDS and key not in {"observed_at", "event_type"}:
            raise ValueError(f"free text is forbidden: {key}")
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError(f"nested values are forbidden: {key}")


def record_event(room_name: str, event_type: str, *, turn_ref: str | None = None, **fields: Any) -> bool:
    if not is_enabled() or not room_name:
        return False
    started = time.perf_counter()
    record = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": _iso_now(),
        "event_type": event_type,
        "route": fields.pop("route", "cli"),
        "outcome": fields.pop("outcome", "observed"),
        "room_ref": keyed_ref(room_name),
        "turn_ref": turn_ref or new_turn_ref(),
        **fields,
    }
    _validate_event(event_type, record)
    root = _cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    path = root / f"observations_{dt.date.today().isoformat()}.jsonl"
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    current_size = path.stat().st_size if path.exists() else 0
    total_size = sum(p.stat().st_size for p in root.glob("observations_*.jsonl") if p.is_file())
    if current_size + len(payload.encode("utf-8")) > MAX_DAILY_BYTES or total_size + len(payload.encode("utf-8")) > MAX_TOTAL_BYTES:
        return False
    safe_append_text(str(path), payload, timeout=0.2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _cleanup_expired(root)
    duration_ms = max(0, int((time.perf_counter() - started) * 1000))
    if event_type != "observer_health":
        _record_health(
            room_name,
            path,
            success=True,
            duration_ms=duration_ms,
            dropped_count=0,
            bytes_written=len(payload.encode("utf-8")),
        )
    return True


def safe_record_event(room_name: str, event_type: str, *, turn_ref: str | None = None, **fields: Any) -> bool:
    started = time.perf_counter()
    try:
        return record_event(room_name, event_type, turn_ref=turn_ref, **fields)
    except Exception as exc:
        try:
            root = _cache_dir()
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"observations_{dt.date.today().isoformat()}.jsonl"
            _record_health(
                room_name,
                path,
                success=False,
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                dropped_count=1,
                bytes_written=0,
            )
        except Exception:
            pass
        print(f"  - [MemorySteward Observer] 観測をスキップしました: {type(exc).__name__}")
        return False


def _record_health(
    room_name: str,
    path: Path,
    *,
    success: bool,
    duration_ms: int,
    dropped_count: int,
    bytes_written: int,
) -> None:
    if not is_enabled() or not room_name:
        return
    record = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": _iso_now(),
        "event_type": "observer_health",
        "route": "observer",
        "outcome": "observed",
        "room_ref": keyed_ref(room_name),
        # semantic turnの10 events上限とは別に数える運用イベント。
        "turn_ref": new_turn_ref(),
        "operation": "write",
        "success": success,
        "duration_ms": duration_ms,
        "dropped_count": dropped_count,
        "bytes_written": bytes_written,
    }
    _validate_event("observer_health", record)
    safe_append_text(
        str(path),
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n",
        timeout=0.2,
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _cleanup_expired(root: Path, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    retention_days = max(1, min(int(retention_days), MAX_RETENTION_DAYS))
    cutoff = dt.date.today() - dt.timedelta(days=retention_days)
    removed = 0
    for path in root.glob("observations_*.jsonl"):
        try:
            file_date = dt.date.fromisoformat(path.stem.replace("observations_", ""))
        except ValueError:
            continue
        if file_date < cutoff:
            path.unlink(missing_ok=True)
            Path(str(path) + ".lock").unlink(missing_ok=True)
            removed += 1
    return removed


def _parse_timestamp(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for parser in (dt.datetime.fromisoformat, lambda v: dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S")):
        try:
            parsed = parser(text)
            return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()
        except (ValueError, TypeError):
            continue
    return None


def _age_seconds(value: Any, now: dt.datetime) -> int | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _next_action_present(content: str) -> int:
    match = re.search(r"^##\s+Next Action\s*$([\s\S]*?)(?=^##\s+|\Z)", content, re.MULTILINE)
    return int(bool(match and match.group(1).strip()))


def observe_working_memory(
    room_name: str,
    slot_name: str,
    content: str,
    metadata: dict[str, Any] | None,
    *,
    event_type: str = "wm_injection",
    route: str = "agent_context",
    turn_ref: str | None = None,
    operation: str | None = None,
    channel: str | None = None,
    content_changed: bool | None = None,
) -> bool:
    if not is_enabled():
        return False
    now = dt.datetime.now().astimezone()
    slot_meta = ((metadata or {}).get("slots") or {}).get(slot_name, {})
    status = str(slot_meta.get("status") or "legacy_active")
    if status not in ENUMS["status"]:
        status = "unknown"
    path = Path(constants.ROOMS_DIR) / room_name / constants.NOTES_DIR_NAME / constants.WORKING_MEMORY_DIR_NAME / f"{slot_name}{constants.WORKING_MEMORY_EXTENSION}"
    fields: dict[str, Any] = {
        "route": route,
        "slot_ref": keyed_ref(slot_name),
        "content_ref": keyed_ref(content),
        "status": status,
        "archived": status == "archived",
        "active_selected": True,
        "next_action_present": _next_action_present(content),
    }
    if event_type in {"wm_injection", "wm_token_estimate"}:
        fields["char_count"] = len(content)
        if path.exists():
            fields["content_age_proxy_sec"] = max(0, int(now.timestamp() - path.stat().st_mtime))
        last_age = _age_seconds(slot_meta.get("last_used_at"), now)
        if last_age is not None:
            fields["last_used_age_sec"] = last_age
    else:
        fields.pop("archived", None)
        fields.pop("active_selected", None)
        fields["operation"] = operation or "read"
        fields["channel"] = channel or "system"
        fields["content_changed"] = bool(content_changed)
    return safe_record_event(room_name, event_type, turn_ref=turn_ref, **fields)


def normalize_tool_outcome(result: Any, reported_status: str = "ok") -> str:
    text = str(result or "").strip()
    lowered = text.lower()
    if reported_status == "error" or text.startswith("【エラー】") or lowered.startswith("error:") or "traceback" in lowered:
        return "error"
    if any(marker in text for marker in ("部分成功", "一部成功", "追加作業", "未完了")):
        return "partial"
    if any(marker in text for marker in ("キャンセル", "中止しました", "取り消しました")):
        return "cancelled"
    if text.startswith("成功") or "完了しました" in text:
        return "success_reported"
    return "unknown"


def classify_tool(tool_name: str) -> tuple[str, str]:
    name = str(tool_name or "")
    if "working_memory" in name:
        return "working_memory", "memory_write" if name in SIDE_EFFECT_TOOL_NAMES else "none"
    if "calendar" in name:
        return "calendar", "external_write" if name in SIDE_EFFECT_TOOL_NAMES else "none"
    if name.startswith("delegate_"):
        return "delegation", "workspace_write"
    if name in SIDE_EFFECT_TOOL_NAMES:
        return "other", "unknown"
    if name.startswith(("read_", "list_", "search_", "recall_", "check_", "get_")):
        return "read_only", "none"
    return "other", "unknown"


def action_memory_event(room_name: str, tool_name: str, result: Any, reported_status: str, slot_name: str = "") -> dict[str, Any]:
    family, side_effect = classify_tool(tool_name)
    outcome = normalize_tool_outcome(result, reported_status)
    event = {
        "schema_version": SCHEMA_VERSION,
        "tool_family": family,
        "side_effect_class": side_effect,
        "normalized_outcome": outcome,
        "postcondition_evidence": "reported_only" if outcome == "success_reported" else "missing",
    }
    if slot_name:
        event["slot_ref"] = keyed_ref(slot_name)
    return event


def record_tool_outcome(room_name: str, tool_name: str, result: Any, reported_status: str, slot_name: str = "") -> dict[str, Any]:
    if not is_enabled():
        return {}
    event = action_memory_event(room_name, tool_name, result, reported_status, slot_name)
    fields = {
        "route": "tool",
        "outcome": event["normalized_outcome"],
        "action_ref": keyed_ref(f"{tool_name}:{time.time_ns()}"),
        "tool_family": event["tool_family"],
        "side_effect_class": event["side_effect_class"],
        "postcondition_evidence": event["postcondition_evidence"],
        "reported_status": "error" if reported_status == "error" else "ok",
    }
    if event.get("slot_ref"):
        fields["slot_ref"] = event["slot_ref"]
    safe_record_event(room_name, "tool_outcome", **fields)
    return event


def record_available_provenance(
    room_name: str,
    store_type: str,
    source_content: Any,
    *,
    route: str,
    selection_route: str,
    selected_count: int,
    turn_ref: str | None = None,
) -> bool:
    if not is_enabled() or not source_content:
        return False
    return safe_record_event(
        room_name,
        "memory_provenance_available",
        turn_ref=turn_ref,
        route=route,
        store_type=store_type,
        source_ref=keyed_ref(source_content),
        selection_route=selection_route,
        selected_count=selected_count,
    )


def load_events(*, days: int = 7, room_name: str = "", cache_dir: Path | None = None) -> list[dict[str, Any]]:
    root = cache_dir or _cache_dir()
    start = dt.date.today() - dt.timedelta(days=max(1, days) - 1)
    wanted_room_ref = keyed_ref(room_name) if room_name else ""
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("observations_*.jsonl")):
        try:
            file_date = dt.date.fromisoformat(path.stem.replace("observations_", ""))
        except ValueError:
            continue
        if file_date < start:
            continue
        try:
            lines: Iterable[str] = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
                _validate_event(str(record.get("event_type")), record)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            if wanted_room_ref and record.get("room_ref") != wanted_room_ref:
                continue
            records.append(record)
    return records


def rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator
