from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import constants
from file_lock_utils import safe_json_read, safe_json_write

READ_SCOPES = {
    "read_chat", "read_memory", "read_notes", "read_calendar", "read_twitter", "read_items",
    "read_letters", "read_autonomy",
}
WRITE_SCOPES = {
    "write_location", "send_chat", "write_event", "write_calendar", "write_items", "write_notes",
    "write_autonomy", "use_voice", "manage_push",
}
OUTWARD_SCOPES = {"post_twitter"}
GRANTABLE_SCOPES = set(READ_SCOPES) | set(WRITE_SCOPES) | set(OUTWARD_SCOPES)
GRANTS_FILENAME = "atelier_app_grants.json"
AUDIT_DIR_NAME = "atelier_app_audit"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_room_name(room_name: str) -> str:
    room = str(room_name or "").strip()
    if (
        not room
        or room in {".", ".."}
        or "/" in room
        or "\\" in room
        or "\x00" in room
        or any(part in {".", ".."} for part in Path(room).parts)
    ):
        raise ValueError("invalid room name")
    return room


def normalize_app_id(app_id: str) -> str:
    app = str(app_id or "").strip()
    if not app:
        raise ValueError("invalid app_id")
    cleaned = "".join(ch for ch in app if ch.isalnum() or ch in {"_", ".", "-"})
    if cleaned != app or len(cleaned) > 80:
        raise ValueError("invalid app_id")
    return app


def _memory_dir(room_name: str) -> Path:
    return Path(constants.ROOMS_DIR) / _safe_room_name(room_name) / "memory"


def grants_path(room_name: str) -> Path:
    return _memory_dir(room_name) / GRANTS_FILENAME


def audit_dir(room_name: str) -> Path:
    return _memory_dir(room_name) / AUDIT_DIR_NAME


def read_store(room_name: str) -> dict[str, Any]:
    data = safe_json_read(str(grants_path(room_name)), default={})
    return data if isinstance(data, dict) else {}


def write_store(room_name: str, store: dict[str, Any]) -> bool:
    return safe_json_write(str(grants_path(room_name)), store if isinstance(store, dict) else {})


def record_audit(room_name: str, action: str, app_id: str, scope: str = "", status: str = "", details: str = "") -> dict[str, Any]:
    room = _safe_room_name(room_name)
    try:
        app = normalize_app_id(app_id)
    except ValueError:
        app = str(app_id or "").strip()[:80]
    record = {
        "timestamp": _now_iso(),
        "room_name": room,
        "app_id": app,
        "scope": str(scope or "").strip(),
        "action": str(action or "").strip(),
        "status": str(status or "").strip(),
        "details": str(details or "").strip()[:500],
    }
    path = audit_dir(room)
    path.mkdir(parents=True, exist_ok=True)
    audit_path = path / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {**record, "audit_path": str(audit_path)}


def _app_record(store: dict[str, Any], app_id: str) -> dict[str, Any]:
    app = normalize_app_id(app_id)
    record = store.setdefault(app, {})
    if not isinstance(record, dict):
        record = {}
        store[app] = record
    grants = record.setdefault("grants", {})
    if not isinstance(grants, dict):
        record["grants"] = {}
    for key in ("pending", "last_manifest", "denied"):
        if not isinstance(record.get(key), list):
            record[key] = []
    return record


def _valid_scope(scope: str) -> str:
    normalized = str(scope or "").strip()
    if normalized not in GRANTABLE_SCOPES:
        raise ValueError("invalid atelier app scope")
    return normalized


def _parse_expiry(value: Any):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def grant_active(grant: Any, now: datetime | None = None) -> bool:
    if not isinstance(grant, dict) or grant.get("mode") != "allow":
        return False
    expires_at = _parse_expiry(grant.get("expires_at"))
    if not expires_at:
        return True
    now = now or datetime.now(timezone.utc)
    return expires_at > now


def has_grant(room_name: str, app_id: str, scope: str) -> bool:
    scope = _valid_scope(scope)
    try:
        app = normalize_app_id(app_id)
    except ValueError:
        return False
    store = read_store(room_name)
    record = store.get(app) if isinstance(store, dict) else None
    grants = record.get("grants", {}) if isinstance(record, dict) else {}
    return grant_active(grants.get(scope))


def granted_scopes(room_name: str, app_id: str) -> list[str]:
    try:
        app = normalize_app_id(app_id)
    except ValueError:
        return []
    store = read_store(room_name)
    record = store.get(app) if isinstance(store, dict) else None
    grants = record.get("grants", {}) if isinstance(record, dict) else {}
    return sorted(scope for scope in GRANTABLE_SCOPES if grant_active(grants.get(scope)))


def update_manifest_requests(room_name: str, app_id: str, requested_scopes: list[dict[str, str]]) -> tuple[list[str], list[str]]:
    app = normalize_app_id(app_id)
    normalized_manifest: list[dict[str, str]] = []
    requested: list[str] = []
    for item in requested_scopes or []:
        if not isinstance(item, dict):
            continue
        scope = str(item.get("scope") or "").strip()
        if scope not in GRANTABLE_SCOPES:
            continue
        reason = str(item.get("reason") or "").strip()[:500]
        normalized_manifest.append({"scope": scope, "reason": reason})
        if scope not in requested:
            requested.append(scope)

    store = read_store(room_name)
    record = _app_record(store, app)
    record["last_manifest"] = normalized_manifest
    denied = set(str(scope) for scope in record.get("denied", []) if str(scope) in GRANTABLE_SCOPES)
    grants = record.get("grants", {}) if isinstance(record.get("grants"), dict) else {}
    pending = [
        scope for scope in requested
        if scope not in denied and not grant_active(grants.get(scope))
    ]
    record["pending"] = pending
    write_store(room_name, store)
    if pending:
        record_audit(room_name, "manifest_pending", app, status="pending", details=",".join(pending))
    return granted_scopes(room_name, app), pending


def grant_scope(room_name: str, app_id: str, scope: str, expires_at: str | None = None) -> dict[str, Any]:
    app = normalize_app_id(app_id)
    scope = _valid_scope(scope)
    store = read_store(room_name)
    record = _app_record(store, app)
    grants = record.setdefault("grants", {})
    grants[scope] = {"mode": "allow", "granted_at": _now_iso(), "expires_at": expires_at}
    record["pending"] = [item for item in record.get("pending", []) if item != scope]
    record["denied"] = [item for item in record.get("denied", []) if item != scope]
    write_store(room_name, store)
    record_audit(room_name, "grant", app, scope, "allowed")
    return grants[scope]


def deny_scope(room_name: str, app_id: str, scope: str) -> None:
    app = normalize_app_id(app_id)
    scope = _valid_scope(scope)
    store = read_store(room_name)
    record = _app_record(store, app)
    record["pending"] = [item for item in record.get("pending", []) if item != scope]
    if scope not in record.get("denied", []):
        record["denied"].append(scope)
    write_store(room_name, store)
    record_audit(room_name, "deny", app, scope, "denied")


def revoke_scope(room_name: str, app_id: str, scope: str) -> None:
    app = normalize_app_id(app_id)
    scope = _valid_scope(scope)
    store = read_store(room_name)
    record = _app_record(store, app)
    grants = record.get("grants", {}) if isinstance(record.get("grants"), dict) else {}
    grants.pop(scope, None)
    record["grants"] = grants
    write_store(room_name, store)
    record_audit(room_name, "revoke", app, scope, "revoked")


def pending_requests(room_name: str) -> list[dict[str, str]]:
    store = read_store(room_name)
    rows: list[dict[str, str]] = []
    for app_id, record in sorted(store.items()):
        if not isinstance(record, dict):
            continue
        reasons = {
            str(item.get("scope") or ""): str(item.get("reason") or "")
            for item in record.get("last_manifest", [])
            if isinstance(item, dict)
        }
        for scope in record.get("pending", []):
            if scope in GRANTABLE_SCOPES:
                rows.append({"app_id": str(app_id), "scope": scope, "reason": reasons.get(scope, "")})
    return rows


def active_grants(room_name: str) -> list[dict[str, str]]:
    store = read_store(room_name)
    rows: list[dict[str, str]] = []
    for app_id, record in sorted(store.items()):
        if not isinstance(record, dict):
            continue
        grants = record.get("grants", {}) if isinstance(record.get("grants"), dict) else {}
        for scope in sorted(GRANTABLE_SCOPES):
            grant = grants.get(scope)
            if grant_active(grant):
                rows.append({
                    "app_id": str(app_id),
                    "scope": scope,
                    "granted_at": str(grant.get("granted_at") or ""),
                    "expires_at": str(grant.get("expires_at") or ""),
                })
    return rows
