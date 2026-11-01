from langchain_core.tools import tool
import os
import constants
import room_manager
import traceback
import json
import datetime
import shutil
import re
import hashlib
from pydantic import BaseModel, Field
from file_lock_utils import (
    locked_file,
    safe_json_read,
    safe_json_update,
    safe_text_read,
    safe_text_write,
)

WM_METADATA_SCHEMA_VERSION = 2
WM_ALLOWED_STATUSES = {
    "active", "blocked", "completed", "cancelled", "superseded", "archived",
}
WM_INJECTABLE_STATUSES = {"active", "blocked"}
WM_TERMINAL_STATUSES = {"completed", "cancelled", "superseded", "archived"}


class WorkingMemoryConflictError(ValueError):
    """楽観的競合制御で更新を拒否したことを示す。"""

def _get_wm_dir(room_name: str) -> str:
    return os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME, constants.WORKING_MEMORY_DIR_NAME)

def _get_wm_metadata_path(room_name: str) -> str:
    return os.path.join(_get_wm_dir(room_name), constants.WORKING_MEMORY_METADATA_FILENAME)

def _safe_slot_name(slot_name: str) -> str:
    slot_name = str(slot_name or "").strip()
    if not slot_name or ".." in slot_name or "/" in slot_name or "\\" in slot_name:
        raise ValueError("不正なスロット名です。")
    if slot_name.endswith(constants.WORKING_MEMORY_EXTENSION):
        slot_name = slot_name[:-len(constants.WORKING_MEMORY_EXTENSION)]
    return slot_name

def _get_wm_path(room_name: str, slot_name: str) -> str:
    slot_name = _safe_slot_name(slot_name)
    if not slot_name.endswith(constants.WORKING_MEMORY_EXTENSION):
        slot_name += constants.WORKING_MEMORY_EXTENSION
    return os.path.join(_get_wm_dir(room_name), slot_name)

def _non_negative_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _normalize_slot_metadata(raw_meta) -> dict:
    meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    raw_status = str(meta.get("status", "active") or "active").strip().lower()
    meta["status"] = raw_status if raw_status in WM_ALLOWED_STATUSES else "unknown"
    meta["state_version"] = _non_negative_int(meta.get("state_version"))
    meta["content_version"] = _non_negative_int(meta.get("content_version"))
    for key in (
        "last_read_at", "last_content_updated_at", "last_verified_at",
        "last_selected_at", "state_changed_at", "completed_at", "cancelled_at",
        "superseded_at", "archived_at",
    ):
        meta.setdefault(key, None)
    for key in (
        "state_change_reason", "completion_reason", "cancel_reason",
        "supersede_reason", "archive_reason", "related_action_ref",
    ):
        meta.setdefault(key, "")
    return meta


def _normalize_wm_metadata(raw_data) -> dict:
    data = dict(raw_data) if isinstance(raw_data, dict) else {}
    raw_slots = data.get("slots")
    data["version"] = WM_METADATA_SCHEMA_VERSION
    data["revision"] = _non_negative_int(data.get("revision"))
    data["slots"] = {
        str(slot_name): _normalize_slot_metadata(slot_meta)
        for slot_name, slot_meta in (raw_slots.items() if isinstance(raw_slots, dict) else [])
    }
    return data


def _load_wm_metadata(room_name: str) -> dict:
    metadata_path = _get_wm_metadata_path(room_name)
    try:
        data = safe_json_read(metadata_path, default={"version": 1, "slots": {}})
        return _normalize_wm_metadata(data)
    except Exception:
        return _normalize_wm_metadata({})

def get_working_memory_metadata(room_name: str) -> dict:
    """UIや内部処理から参照するWMメタデータを取得する。"""
    return _load_wm_metadata(room_name)

def save_working_memory_metadata(room_name: str, metadata: dict) -> dict:
    """UI編集されたWMメタデータをroot revision照合後に保存する。"""
    if not isinstance(metadata, dict):
        raise ValueError("Working Memory metadata はJSONオブジェクトである必要があります。")
    raw_expected_revision = metadata.get("revision")
    saved = {}

    def update(current):
        current_normalized = _normalize_wm_metadata(current)
        expected_revision = (
            current_normalized["revision"]
            if raw_expected_revision is None and current_normalized["revision"] == 0
            else _non_negative_int(raw_expected_revision, -1)
        )
        if expected_revision != current_normalized["revision"]:
            raise WorkingMemoryConflictError(
                "Working Memory metadataが別の操作で更新されています。"
                "最新の状態に更新して差分を確認してください。"
            )
        normalized = _normalize_wm_metadata(metadata)
        normalized["revision"] = current_normalized["revision"] + 1
        normalized["updated_at"] = _now()
        saved.update(normalized)
        return normalized

    os.makedirs(_get_wm_dir(room_name), exist_ok=True)
    safe_json_update(
        _get_wm_metadata_path(room_name),
        update,
        default={"version": 1, "slots": {}},
    )
    return saved


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _update_wm_metadata(room_name: str, update_func) -> dict:
    os.makedirs(_get_wm_dir(room_name), exist_ok=True)
    saved = {}

    def update(current):
        metadata = _normalize_wm_metadata(current)
        update_func(metadata)
        metadata["version"] = WM_METADATA_SCHEMA_VERSION
        metadata["revision"] = _non_negative_int(metadata.get("revision")) + 1
        metadata["updated_at"] = _now()
        saved.update(metadata)
        return metadata

    safe_json_update(
        _get_wm_metadata_path(room_name),
        update,
        default={"version": 1, "slots": {}},
    )
    return saved


def _save_wm_metadata(room_name: str, metadata: dict) -> None:
    """互換用。最新revisionを基準に正規化済み全体を原子的に保存する。"""
    proposed = dict(metadata) if isinstance(metadata, dict) else {}

    def replace(current):
        current_revision = _normalize_wm_metadata(current)["revision"]
        normalized = _normalize_wm_metadata(proposed)
        normalized["revision"] = current_revision + 1
        normalized["updated_at"] = _now()
        return normalized

    os.makedirs(_get_wm_dir(room_name), exist_ok=True)
    safe_json_update(
        _get_wm_metadata_path(room_name),
        replace,
        default={"version": 1, "slots": {}},
    )


def _touch_slot_metadata(
    room_name: str,
    slot_name: str,
    *,
    operation: str = "selection",
    **updates,
) -> dict:
    slot_name = _safe_slot_name(slot_name)
    now = _now()

    def update(metadata):
        slot_meta = metadata.setdefault("slots", {}).setdefault(
            slot_name, _normalize_slot_metadata({})
        )
        current_status = _normalize_slot_metadata(slot_meta)["status"]
        requested_status = str(updates.get("status", current_status)).strip().lower()
        if (
            current_status in WM_TERMINAL_STATUSES
            and requested_status == "active"
            and requested_status != current_status
        ):
            raise ValueError(
                "terminal状態からactiveへ戻すには明示的な再開操作が必要です。"
            )
        slot_meta.update(updates)
        slot_meta.setdefault("status", "active")
        if operation == "read":
            slot_meta["last_read_at"] = now
        elif operation == "content":
            slot_meta["last_content_updated_at"] = now
            slot_meta["content_version"] = _non_negative_int(
                slot_meta.get("content_version")
            ) + 1
        elif operation == "verify":
            slot_meta["last_verified_at"] = now
        elif operation == "selection":
            slot_meta["last_selected_at"] = now
        elif operation == "auto_selection":
            slot_meta["auto_selected_at"] = now
        if operation in {"content", "verify", "selection"}:
            # 旧schemaとの表示互換用。鮮度判定の正本には使用しない。
            slot_meta["last_used_at"] = now
        metadata["slots"][slot_name] = _normalize_slot_metadata(slot_meta)

    return _update_wm_metadata(room_name, update)


def get_working_memory_content_version(room_name: str, slot_name: str) -> int:
    slot_name = _safe_slot_name(slot_name)
    meta = _load_wm_metadata(room_name).get("slots", {}).get(slot_name, {})
    return _non_negative_int(meta.get("content_version"))


def get_working_memory_status(room_name: str, slot_name: str) -> str:
    slot_name = _safe_slot_name(slot_name)
    metadata = _load_wm_metadata(room_name)
    if slot_name in metadata.get("slots", {}):
        return _normalize_slot_metadata(metadata["slots"][slot_name])["status"]
    if (
        slot_name == room_manager.get_active_working_memory_slot(room_name)
        and os.path.exists(_get_wm_path(room_name, slot_name))
    ):
        return "active"
    return "unregistered"


def is_working_memory_injectable(metadata_or_status, slot_name: str = None) -> bool:
    """副作用なくWMの注入可否を判定する単一正本。"""
    if isinstance(metadata_or_status, dict):
        if slot_name is not None:
            slots = metadata_or_status.get("slots", {})
            if slot_name not in slots:
                return False
            raw = slots.get(slot_name, {})
            status = _normalize_slot_metadata(raw)["status"]
        else:
            status = _normalize_slot_metadata(metadata_or_status)["status"]
    else:
        status = str(metadata_or_status or "active").strip().lower()
    return status in WM_INJECTABLE_STATUSES


def mark_working_memory_read(room_name: str, slot_name: str) -> dict:
    slot_name = _safe_slot_name(slot_name)
    metadata = _load_wm_metadata(room_name)
    if slot_name not in metadata.get("slots", {}):
        # 未登録旧スロットは読取だけで永続移行しない。
        return metadata
    return _touch_slot_metadata(room_name, slot_name, operation="read")


def mark_working_memory_verified(room_name: str, slot_name: str) -> dict:
    return _touch_slot_metadata(room_name, slot_name, operation="verify")


def mark_working_memory_selected(room_name: str, slot_name: str) -> dict:
    """ユーザーまたはペルソナによる明示選択を意味ある活動として記録する。"""
    return _touch_slot_metadata(room_name, slot_name, operation="selection")


def load_injectable_working_memory(
    room_name: str,
    slot_name: str,
    *,
    mark_read: bool = False,
) -> str:
    """状態契約を適用して本文を読む。token見積もりではmark_read=Falseを使う。"""
    slot_name = _safe_slot_name(slot_name)
    if get_working_memory_status(room_name, slot_name) not in WM_INJECTABLE_STATUSES:
        return ""
    path = _get_wm_path(room_name, slot_name)
    if not os.path.exists(path):
        return ""
    content = safe_text_read(path).strip()
    if content and mark_read:
        mark_working_memory_read(room_name, slot_name)
    return content


def save_working_memory_content(
    room_name: str,
    slot_name: str,
    content: str,
    *,
    expected_content_version: int = None,
    normalize: bool = False,
    metadata_updates: dict = None,
) -> int:
    """slot単位トランザクションロック内で競合確認・本文・versionを更新する。"""
    slot_name = _safe_slot_name(slot_name)
    path = _get_wm_path(room_name, slot_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    transaction_path = f"{path}.transaction"
    with locked_file(transaction_path):
        current_version = get_working_memory_content_version(room_name, slot_name)
        if (
            expected_content_version is not None
            and current_version != int(expected_content_version)
        ):
            raise WorkingMemoryConflictError(
                "Working Memory本文が別の操作で更新されています。"
                "最新の状態に更新して差分を確認してください。"
            )
        _backup_wm_file(room_name, slot_name, path)
        body = _normalize_working_memory_text(content).rstrip() + "\n" if normalize else str(content)
        safe_text_write(path, body)
        _touch_slot_metadata(
            room_name,
            slot_name,
            operation="content",
            **(metadata_updates or {}),
        )
        return get_working_memory_content_version(room_name, slot_name)


def _set_working_memory_state(
    room_name: str,
    slot_name: str,
    status: str,
    reason: str,
    related_action_ref: str = "",
    *,
    allow_reactivate: bool = False,
    expected_state_version: int = None,
) -> dict:
    slot_name = _safe_slot_name(slot_name)
    status = str(status or "").strip().lower()
    reason = str(reason or "").strip()
    if status not in WM_ALLOWED_STATUSES:
        raise ValueError(f"未対応のstatusです: {status}")
    if not reason:
        raise ValueError("状態変更の理由を指定してください。")
    now = _now()

    def update(metadata):
        slot_meta = metadata.setdefault("slots", {}).setdefault(
            slot_name, _normalize_slot_metadata({})
        )
        current_status = _normalize_slot_metadata(slot_meta)["status"]
        current_version = _non_negative_int(slot_meta.get("state_version"))
        if expected_state_version is not None and current_version != int(expected_state_version):
            raise WorkingMemoryConflictError(
                "Working Memoryの状態が別の操作で更新されています。最新の状態を確認してください。"
            )
        if current_status == status:
            raise ValueError(f"statusは既に{status}です。")
        if status == "active" and current_status in WM_TERMINAL_STATUSES and not allow_reactivate:
            raise ValueError("terminal状態からactiveへ戻すには明示的な再開操作が必要です。")
        if current_status in WM_TERMINAL_STATUSES and status != "active":
            raise ValueError(f"terminal状態（{current_status}）から{status}へは遷移できません。")
        if status == "blocked" and current_status != "active":
            raise ValueError("blockedへ遷移できるのはactive状態だけです。")
        if status in WM_TERMINAL_STATUSES and current_status not in {"active", "blocked"}:
            raise ValueError(f"{current_status}から{status}へは遷移できません。")

        slot_meta["status"] = status
        slot_meta["state_version"] = current_version + 1
        slot_meta["state_changed_at"] = now
        slot_meta["state_change_reason"] = reason
        slot_meta["related_action_ref"] = str(related_action_ref or "")
        if status == "completed":
            slot_meta["completed_at"] = now
            slot_meta["completion_reason"] = reason
        elif status == "cancelled":
            slot_meta["cancelled_at"] = now
            slot_meta["cancel_reason"] = reason
        elif status == "superseded":
            slot_meta["superseded_at"] = now
            slot_meta["supersede_reason"] = reason
        elif status == "archived":
            slot_meta["archived_at"] = now
            slot_meta["archive_reason"] = reason
        elif status == "active" and current_status in WM_TERMINAL_STATUSES:
            slot_meta["reactivated_at"] = now
            slot_meta["reactivate_reason"] = reason
        slot_meta["last_used_at"] = now
        metadata["slots"][slot_name] = _normalize_slot_metadata(slot_meta)

    return _update_wm_metadata(room_name, update)


class SetWorkingMemoryStateArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    slot_name: str = Field(..., description="対象のスロット名")
    status: str = Field(..., description="active, blocked, completed, cancelled, superseded, archived のいずれか")
    reason: str = Field(..., description="状態を変更する具体的な理由")
    related_action_ref: str = Field("", description="任意の関連Action参照")


@tool(args_schema=SetWorkingMemoryStateArgs)
def set_working_memory_state(
    room_name: str,
    slot_name: str,
    status: str,
    reason: str,
    related_action_ref: str = "",
) -> str:
    """Working Memoryの状態だけを制約付きで変更する。自動完了には使用しない。"""
    try:
        result = _set_working_memory_state(
            room_name,
            slot_name,
            status,
            reason,
            related_action_ref,
        )
        version = result["slots"][slot_name]["state_version"]
        _observe_wm_operation(room_name, slot_name, "state_change")
        return f"成功: ワーキングメモリ '{slot_name}' を {status} に変更しました（state_version={version}）。"
    except Exception as e:
        return f"【エラー】ワーキングメモリの状態変更に失敗しました: {e}"

def _observe_wm_operation(room_name: str, slot_name: str, operation: str, *, channel: str = "tool", content_changed: bool = False) -> None:
    """Phase 0観測。失敗してもWorking Memoryの本処理へ影響させない。"""
    try:
        import memory_steward_observer
        path = _get_wm_path(room_name, slot_name)
        content = safe_text_read(path) if os.path.exists(path) else ""
        memory_steward_observer.observe_working_memory(
            room_name,
            slot_name,
            content,
            _load_wm_metadata(room_name),
            event_type="wm_operation",
            route="ui" if channel == "ui" else "tool",
            operation=operation,
            channel=channel,
            content_changed=content_changed,
        )
    except Exception:
        pass

def archive_stale_working_memories(room_name: str, days: int = 30) -> list[str]:
    """一定期間使われていないWMスロットをmetadata上で休眠扱いにする。"""
    metadata = _load_wm_metadata(room_name)
    slots_meta = metadata.setdefault("slots", {})
    if not slots_meta:
        return []

    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    archived = []
    for slot_name, meta in slots_meta.items():
        if not isinstance(meta, dict) or meta.get("status") in WM_TERMINAL_STATUSES:
            continue
        last_used = get_meaningful_working_memory_activity(meta)
        if not last_used:
            continue
        if last_used < cutoff:
            archived.append(slot_name)
    if archived:
        for slot_name in archived:
            _set_working_memory_state(
                room_name,
                slot_name,
                "archived",
                f"{days}日以上未使用",
            )
    try:
        import memory_steward_observer
        active_slot = room_manager.get_active_working_memory_slot(room_name)
        memory_steward_observer.safe_record_event(
            room_name,
            "wm_archive_sweep",
            route="agent_context",
            operation="archive_sweep",
            scanned_count=len(slots_meta),
            archived_count=len(archived),
            active_selected=active_slot in archived,
        )
    except Exception:
        pass
    return archived

def select_working_memory_for_research_context(room_name: str, query: str = "", set_active: bool = True) -> str:
    """
    Research Thread・目標・問いの優先度/類似度から、自律行動に使うWMスロットを選ぶ。
    Purpose Profileの関心でスコアをブーストし、長期目的に沿うスロットを優先する。
    """
    try:
        # --- Phase 1: Research Thread 起点の候補 ---
        from research_thread_manager import ResearchThreadManager
        manager = ResearchThreadManager(room_name)
        candidates = []
        if query:
            candidates = manager.find_similar_threads(query=query, limit=5, boost_by_purpose=True)
        if not candidates:
            candidates = manager.list_threads(status="active", boost_by_purpose=True)

        metadata = _load_wm_metadata(room_name)
        slots_meta = metadata.get("slots", {})
        for thread in candidates:
            slot_name = thread.get("working_memory_slot", "")
            if not slot_name:
                continue
            try:
                slot_name = _safe_slot_name(slot_name)
            except ValueError:
                continue
            slot_meta = slots_meta.get(slot_name, {})
            if not is_working_memory_injectable(slot_meta):
                continue
            path = _get_wm_path(room_name, slot_name)
            if not os.path.exists(path):
                continue
            if set_active:
                room_manager.set_active_working_memory_slot(room_name, slot_name)
                _touch_slot_metadata(
                    room_name,
                    slot_name,
                    operation="auto_selection",
                    linked_thread_id=thread.get("thread_id", slot_meta.get("linked_thread_id", "")),
                    auto_selected_reason="research_thread_context",
                )
            return slot_name

        # --- Phase 2: 目標起点の候補 ---
        selected = _select_slot_by_goal(room_name, query, slots_meta, set_active)
        if selected:
            return selected
    except Exception:
        traceback.print_exc()
    return ""


def _select_slot_by_goal(room_name: str, query: str, slots_meta: dict, set_active: bool) -> str:
    """目標に紐づくWMスロットから候補を選択する。"""
    try:
        from goal_manager import GoalManager
        gm = GoalManager(room_name)
        goals = gm.get_active_goals()
        if not goals:
            return ""

        query_lower = str(query or "").lower()
        # 目標テキストとクエリの一致度で候補をスコアリング
        scored_goals = []
        for goal in goals:
            goal_text = str(goal.get("goal", "")).lower()
            goal_id = goal.get("id", "")
            # クエリとの簡易一致スコア
            score = 0
            if query_lower:
                for word in query_lower.split():
                    if len(word) >= 2 and word in goal_text:
                        score += 1
            scored_goals.append((score, goal_id, goal_text))

        # スコア順にソート
        scored_goals.sort(key=lambda x: x[0], reverse=True)

        for _score, goal_id, _goal_text in scored_goals:
            # このgoal_idに紐づくスロットを探す
            for slot_name, meta in slots_meta.items():
                if not isinstance(meta, dict):
                    continue
                if meta.get("linked_goal_id") != goal_id:
                    continue
                if not is_working_memory_injectable(meta):
                    continue
                try:
                    slot_name = _safe_slot_name(slot_name)
                except ValueError:
                    continue
                path = _get_wm_path(room_name, slot_name)
                if not os.path.exists(path):
                    continue
                if set_active:
                    room_manager.set_active_working_memory_slot(room_name, slot_name)
                    _touch_slot_metadata(
                        room_name,
                        slot_name,
                        operation="auto_selection",
                        auto_selected_reason="goal_context",
                    )
                return slot_name
    except Exception:
        traceback.print_exc()
    return ""

def _backup_wm_file(room_name: str, slot_name: str, path: str) -> None:
    if not os.path.exists(path):
        return
    backup_dir = os.path.join(constants.ROOMS_DIR, room_name, "backups", "working_memories")
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"{timestamp}_{slot_name}{constants.WORKING_MEMORY_EXTENSION}.bak"
    shutil.copy2(path, os.path.join(backup_dir, backup_filename))

_STANDARD_WM_SECTIONS = ["Current Intent", "Known Context", "Next Action", "Stop Condition"]
_JSON_WM_KEY_TO_SECTION = {
    "current_intent": "Current Intent",
    "known_context": "Known Context",
    "linked_goal": "Linked Goal",
    "linked_thread": "Linked Thread",
    "next_action": "Next Action",
    "stop_condition": "Stop Condition",
}

def _section_from_key(key: str) -> str:
    clean_key = str(key or "").strip().strip("'\"")
    normalized = clean_key.replace("-", "_").replace(" ", "_").lower()
    if normalized in _JSON_WM_KEY_TO_SECTION:
        return _JSON_WM_KEY_TO_SECTION[normalized]
    return clean_key.replace("_", " ").strip().title() or "Notes"


def _parse_wm_datetime(value) -> datetime.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except (TypeError, ValueError):
        return None


def get_meaningful_working_memory_activity(meta: dict) -> datetime.datetime | None:
    """読取・自動選択を除外した最終活動日時を返す。旧値は補助値としてのみ使う。"""
    meta = meta if isinstance(meta, dict) else {}
    canonical = [
        _parse_wm_datetime(meta.get(key))
        for key in (
            "last_content_updated_at",
            "last_verified_at",
            "last_selected_at",
            "state_changed_at",
        )
    ]
    valid = [value for value in canonical if value is not None]
    if valid:
        return max(valid)
    return _parse_wm_datetime(meta.get("last_used_at"))


def get_working_memory_health(
    room_name: str,
    slot_name: str,
    *,
    metadata: dict = None,
    content: str = None,
    stale_days: int = 30,
    active_slot: str = None,
) -> dict:
    """本文を外へ出さず、決定的なWM構造・鮮度フラグだけを返す。"""
    slot_name = _safe_slot_name(slot_name)
    metadata = metadata if isinstance(metadata, dict) else _load_wm_metadata(room_name)
    slots_meta = metadata.get("slots", {})
    registered = slot_name in slots_meta
    meta = _normalize_slot_metadata(slots_meta.get(slot_name, {}))
    path = _get_wm_path(room_name, slot_name)
    file_exists = os.path.exists(path)
    if content is None:
        content = safe_text_read(path) if file_exists else ""
    _preface, sections = _split_wm_markdown_sections(content)
    # 診断だけで日付付き見出しを同じ役割として数える。本文再構成には使わない。
    headings = []
    dated_operational_sections = False
    for heading, _body in sections:
        role = _section_from_key(heading)
        dated = re.fullmatch(
            r"(Current Intent|Next Action|Stop Condition)\s*[（(][^()（）]*\d{4}-\d{2}-\d{2}[^()（）]*[)）]",
            role, re.IGNORECASE,
        )
        if dated:
            role = _section_from_key(dated.group(1))
            dated_operational_sections = True
        headings.append(role)
    meaningful = get_meaningful_working_memory_activity(meta) if registered else None
    stale = bool(
        meaningful
        and meaningful < datetime.datetime.now() - datetime.timedelta(days=stale_days)
    )
    active_slot = active_slot or room_manager.get_active_working_memory_slot(room_name)
    if registered:
        effective_status = meta["status"]
    elif slot_name == active_slot and file_exists:
        effective_status = "active"
    else:
        effective_status = "unregistered"
    flags = []
    if not registered:
        flags.append("metadata_unregistered")
    if not file_exists:
        flags.append("file_missing")
    if stale:
        flags.append(f"stale_{stale_days}d")
    if dated_operational_sections:
        flags.append("dated_operational_sections")
    if not any(heading in _STANDARD_WM_SECTIONS for heading in headings):
        flags.append("standard_sections_missing")
    for heading in ("Current Intent", "Next Action", "Stop Condition"):
        if headings.count(heading) > 1:
            flags.append(f"duplicate_{heading.lower().replace(' ', '_')}")
    if slot_name == active_slot and effective_status in WM_TERMINAL_STATUSES:
        flags.append("terminal_selected")
    return {
        "status": effective_status,
        "active": slot_name == active_slot,
        "meaningful_activity_at": (
            meaningful.strftime("%Y-%m-%d %H:%M:%S") if meaningful else None
        ),
        "flags": flags,
        "file_exists": file_exists,
        "bytes": os.path.getsize(path) if file_exists else 0,
    }


_WM_CLEANUP_RELEVANT_FLAGS = {
    "metadata_unregistered",
    "standard_sections_missing",
    "stale_30d",
    "terminal_selected",
}


def get_working_memory_cleanup_assessment(room_name: str) -> dict:
    """公開後の整理案内に使う、副作用のない決定的な判定結果を返す。"""
    if not room_name:
        return {
            "affected": False,
            "fingerprint": "",
            "active_slot": "",
            "unregistered_slots": [],
            "active_flags": [],
            "file_count": 0,
        }

    wm_dir = _get_wm_dir(room_name)
    if not os.path.isdir(wm_dir):
        return {
            "affected": False,
            "fingerprint": "",
            "active_slot": room_manager.get_active_working_memory_slot(room_name),
            "unregistered_slots": [],
            "active_flags": [],
            "file_count": 0,
        }

    metadata = _load_wm_metadata(room_name)
    active_slot = room_manager.get_active_working_memory_slot(room_name)
    file_slots = sorted(
        filename[:-len(constants.WORKING_MEMORY_EXTENSION)]
        for filename in os.listdir(wm_dir)
        if filename.endswith(constants.WORKING_MEMORY_EXTENSION)
    )
    registered_slots = metadata.get("slots", {})
    unregistered_slots = [
        slot_name for slot_name in file_slots if slot_name not in registered_slots
    ]
    active_path = _get_wm_path(room_name, active_slot)
    active_content = safe_text_read(active_path) if os.path.exists(active_path) else ""
    active_health = get_working_memory_health(
        room_name,
        active_slot,
        metadata=metadata,
        content=active_content,
        stale_days=30,
        active_slot=active_slot,
    )
    active_flags = sorted(
        flag
        for flag in active_health["flags"]
        if (
            flag in _WM_CLEANUP_RELEVANT_FLAGS
            or flag.startswith("duplicate_")
        )
    )
    active_needs_cleanup = bool(active_content.strip() and active_flags)
    affected = bool(unregistered_slots or active_needs_cleanup)
    fingerprint_payload = {
        "active_slot": active_slot,
        "active_flags": active_flags if active_content.strip() else [],
        "unregistered_slots": unregistered_slots,
        "file_count": len(file_slots),
    }
    fingerprint = (
        hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if affected
        else ""
    )
    return {
        "affected": affected,
        "fingerprint": fingerprint,
        "active_slot": active_slot,
        "unregistered_slots": unregistered_slots,
        "active_flags": active_flags if active_content.strip() else [],
        "file_count": len(file_slots),
    }


def _fresh_working_memory_body(slot_name: str) -> str:
    return (
        f"# Working Focus: {slot_name}\n\n"
        "## Current Intent\n\n"
        "## Known Context\n\n"
        "## Next Action\n\n"
        "## Stop Condition\n"
    )


def start_fresh_working_memory(room_name: str) -> dict:
    """既存本文を保持・退避したうえで、標準構造の新しいactive WMへ切り替える。"""
    if not room_name:
        raise ValueError("ルームが選択されていません。")

    wm_dir = _get_wm_dir(room_name)
    room_dir = os.path.join(constants.ROOMS_DIR, room_name)
    room_config_path = os.path.join(room_dir, "room_config.json")
    os.makedirs(wm_dir, exist_ok=True)

    with locked_file(os.path.join(wm_dir, ".cleanup")):
        assessment = get_working_memory_cleanup_assessment(room_name)
        if not assessment["affected"]:
            return {
                "changed": False,
                "new_slot": assessment["active_slot"],
                "backup_dir": "",
                "assessment": assessment,
            }

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_dir = os.path.join(
            room_dir, "backups", "working_memories", f"cleanup_{timestamp}"
        )
        backup_wm_dir = os.path.join(backup_dir, "working_memories")
        os.makedirs(backup_dir, exist_ok=False)
        shutil.copytree(wm_dir, backup_wm_dir)
        had_room_config = os.path.exists(room_config_path)
        if had_room_config:
            shutil.copy2(room_config_path, os.path.join(backup_dir, "room_config.json"))

        original_active = assessment["active_slot"]
        base_slot = f"current_focus_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        new_slot = base_slot
        suffix = 2
        while os.path.exists(_get_wm_path(room_name, new_slot)):
            new_slot = f"{base_slot}_{suffix}"
            suffix += 1

        try:
            migrate_working_memory_metadata_v2(room_name)
            save_working_memory_content(
                room_name,
                new_slot,
                _fresh_working_memory_body(new_slot),
                metadata_updates={"status": "active"},
            )
            if not room_manager.set_active_working_memory_slot(room_name, new_slot):
                raise RuntimeError("新しい作業メモをactiveに設定できませんでした。")

            original_path = _get_wm_path(room_name, original_active)
            original_status = get_working_memory_status(room_name, original_active)
            if os.path.exists(original_path) and original_status in WM_INJECTABLE_STATUSES:
                _set_working_memory_state(
                    room_name,
                    original_active,
                    "superseded",
                    "新しい作業メモへの安全な切り替え",
                    related_action_ref=new_slot,
                )
        except Exception:
            metadata_backup = os.path.join(
                backup_wm_dir, constants.WORKING_MEMORY_METADATA_FILENAME
            )
            metadata_path = _get_wm_metadata_path(room_name)
            if os.path.exists(metadata_backup):
                shutil.copy2(metadata_backup, metadata_path)
            elif os.path.exists(metadata_path):
                os.remove(metadata_path)
            new_path = _get_wm_path(room_name, new_slot)
            if os.path.exists(new_path):
                os.remove(new_path)
            for generated_lock_path in (
                f"{new_path}.lock",
                f"{new_path}.transaction.lock",
            ):
                if os.path.exists(generated_lock_path):
                    os.remove(generated_lock_path)
            config_backup = os.path.join(backup_dir, "room_config.json")
            if had_room_config and os.path.exists(config_backup):
                shutil.copy2(config_backup, room_config_path)
            elif not had_room_config and os.path.exists(room_config_path):
                os.remove(room_config_path)
            raise

        return {
            "changed": True,
            "new_slot": new_slot,
            "previous_slot": original_active,
            "backup_dir": backup_dir,
            "assessment": assessment,
        }


def migrate_working_memory_metadata_v2(room_name: str) -> dict:
    """指定した1ルームの旧スロットを本文非破壊でschema v2へ明示移行する。"""
    wm_dir = _get_wm_dir(room_name)
    os.makedirs(wm_dir, exist_ok=True)
    metadata_path = _get_wm_metadata_path(room_name)
    raw = safe_json_read(metadata_path, default={"version": 1, "slots": {}})
    active_slot = room_manager.get_active_working_memory_slot(room_name)
    md_slots = sorted(
        filename[:-len(constants.WORKING_MEMORY_EXTENSION)]
        for filename in os.listdir(wm_dir)
        if filename.endswith(constants.WORKING_MEMORY_EXTENSION)
    )

    def build_migration(current) -> tuple[dict, list[str]]:
        normalized = _normalize_wm_metadata(current)
        added_slots = []
        for slot_name in md_slots:
            if slot_name in normalized["slots"]:
                continue
            path = _get_wm_path(room_name, slot_name)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            initial = {
                "status": "active" if slot_name == active_slot else "archived",
                "last_content_updated_at": mtime,
            }
            if slot_name != active_slot:
                initial["archive_reason"] = "旧形式スロットの安全な初期登録"
            normalized["slots"][slot_name] = _normalize_slot_metadata(initial)
            added_slots.append(slot_name)
        return normalized, added_slots

    normalized, added = build_migration(raw)
    changed = raw != normalized
    if not changed:
        return {"changed": False, "added_slots": [], "metadata": normalized}

    if os.path.exists(metadata_path):
        backup_dir = os.path.join(
            constants.ROOMS_DIR, room_name, "backups", "working_memories"
        )
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(
            backup_dir,
            f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_metadata.json.bak",
        )
        shutil.copy2(metadata_path, backup_path)

    saved_added = []

    def replace(current):
        current_normalized = _normalize_wm_metadata(current)
        result, current_added = build_migration(current)
        if current == result:
            return current
        result["revision"] = current_normalized["revision"] + 1
        result["updated_at"] = _now()
        saved_added.extend(current_added)
        return result

    safe_json_update(
        metadata_path,
        replace,
        default={"version": 1, "slots": {}},
    )
    return {
        "changed": True,
        "added_slots": saved_added or added,
        "metadata": _load_wm_metadata(room_name),
    }

def _stringify_wm_value(value) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, indent=2).strip()

def _markdown_from_json_working_memory(raw_text: str) -> str:
    stripped = str(raw_text or "").strip()
    if not stripped.startswith("{"):
        return str(raw_text or "")
    decoder = json.JSONDecoder()
    try:
        parsed, end = decoder.raw_decode(stripped)
    except Exception:
        return str(raw_text or "")
    if not isinstance(parsed, dict):
        return str(raw_text or "")

    blocks = []
    for key, value in parsed.items():
        value_text = _stringify_wm_value(value)
        if not value_text:
            continue
        blocks.append(f"## {_section_from_key(key)}\n{value_text}")
    remainder = stripped[end:].strip()
    if remainder:
        blocks.append(remainder)
    return "\n\n".join(blocks)

def _split_wm_markdown_sections(raw_text: str) -> tuple[str, list[tuple[str, str]]]:
    text = _markdown_from_json_working_memory(raw_text)
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
    if not matches:
        return text.strip(), []

    preface = text[:matches[0].start()].strip()
    sections = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[start:end].strip()))
    return preface, sections

def _rebuild_wm_markdown(preface: str, sections: list[tuple[str, str]]) -> str:
    merged = {}
    order = []
    for heading, body in sections:
        heading = _section_from_key(heading)
        body = str(body or "").strip()
        if not heading:
            continue
        if heading not in order:
            order.append(heading)
        # Later duplicate sections are usually fresher patch results.
        merged[heading] = body

    ordered_headings = [heading for heading in _STANDARD_WM_SECTIONS if heading in merged]
    ordered_headings.extend(heading for heading in order if heading not in ordered_headings)

    blocks = []
    if preface and not preface.startswith("{"):
        blocks.append(preface)
    for heading in ordered_headings:
        body = merged.get(heading, "").strip()
        blocks.append(f"## {heading}\n{body}".rstrip())
    return "\n\n".join(blocks).strip()

def _normalize_working_memory_text(raw_text: str) -> str:
    preface, sections = _split_wm_markdown_sections(raw_text)
    if not sections:
        return preface
    return _rebuild_wm_markdown(preface, sections)

def _extract_wm_sections_from_content(content: str) -> list[tuple[str, str]]:
    _preface, sections = _split_wm_markdown_sections(str(content or ""))
    return sections

def _apply_working_memory_section_patch(existing: str, section: str, content: str, mode: str = "replace") -> str:
    existing = _normalize_working_memory_text(existing)
    preface, sections = _split_wm_markdown_sections(existing)
    target_section = _section_from_key(section)
    new_content = str(content or "").strip()

    found = False
    updated_sections = []
    for heading, body in sections:
        if _section_from_key(heading) == target_section:
            found = True
            body = f"{str(body).strip()}\n{new_content}".strip() if mode == "append" else new_content
        updated_sections.append((_section_from_key(heading), body))
    if not found:
        updated_sections.append((target_section, new_content))
    return _rebuild_wm_markdown(preface, updated_sections)

def get_working_memory_overview(room_name: str, limit: int = 8) -> str:
    try:
        wm_dir = _get_wm_dir(room_name)
        if not os.path.exists(wm_dir):
            return ""
        metadata = _load_wm_metadata(room_name)
        slots = [
            f.replace(constants.WORKING_MEMORY_EXTENSION, "")
            for f in os.listdir(wm_dir)
            if f.endswith(constants.WORKING_MEMORY_EXTENSION)
        ]
        if not slots:
            return ""
        active_slot = room_manager.get_active_working_memory_slot(room_name)
        lines = ["\n### ワーキングメモリスロット一覧"]
        injectable_slots = []
        terminal_counts = {}
        for slot in slots:
            if slot in metadata.get("slots", {}):
                meta = metadata["slots"][slot]
                status = _normalize_slot_metadata(meta)["status"]
            elif slot == active_slot:
                meta = {}
                status = "active"
            else:
                continue
            if is_working_memory_injectable(status):
                injectable_slots.append(slot)
            else:
                terminal_counts[status] = terminal_counts.get(status, 0) + 1
        for slot in injectable_slots[:limit]:
            meta = metadata.get("slots", {}).get(slot, {})
            marker = "active" if slot == active_slot else "slot"
            status = _normalize_slot_metadata(meta)["status"]
            linked_thread = meta.get("linked_thread_id", "")
            linked_goal = meta.get("linked_goal_id", "")
            purpose = meta.get("purpose", "")
            parts = [f"- {slot} ({marker}, status={status})"]
            if linked_thread:
                parts.append(f"linked_thread={linked_thread}")
            if linked_goal:
                parts.append(f"linked_goal={linked_goal}")
            if purpose:
                parts.append(f"purpose={purpose}")
            lines.append(" / ".join(parts))
        for status, count in sorted(terminal_counts.items()):
            lines.append(f"- {status}_slots: {count}件（現在の実行文脈から除外）")
        return "\n".join(lines) + "\n"
    except Exception:
        return ""

@tool
def list_working_memories(room_name: str) -> str:
    """
    現在利用可能なワーキングメモリのスロット（話題ごと）の一覧と、現在アクティブなスロット名を取得する。
    """
    try:
        wm_dir = _get_wm_dir(room_name)
        if not os.path.exists(wm_dir):
            return "【利用可能なワーキングメモリスロットはありません】"
        
        slots = [f.replace(constants.WORKING_MEMORY_EXTENSION, '') for f in os.listdir(wm_dir) if f.endswith(constants.WORKING_MEMORY_EXTENSION)]
        active_slot = room_manager.get_active_working_memory_slot(room_name)
        
        if not slots:
            return "【利用可能なワーキングメモリスロットはありません】"
            
        metadata = _load_wm_metadata(room_name)
        visible_slots = []
        hidden_counts = {}
        for slot in slots:
            if slot in metadata.get("slots", {}):
                status = _normalize_slot_metadata(
                    metadata["slots"][slot]
                )["status"]
            elif slot == active_slot:
                status = "active"
            else:
                status = "unregistered"
            if status in WM_INJECTABLE_STATUSES:
                visible_slots.append((slot, status))
            else:
                hidden_counts[status] = hidden_counts.get(status, 0) + 1

        result = f"現在アクティブなスロット: {active_slot}\n"
        result += "利用可能なスロット一覧:\n"
        if not visible_slots:
            result += "- なし\n"
        for slot, status in visible_slots:
            meta = metadata.get("slots", {}).get(slot, {})
            linked = f" / linked_thread={meta.get('linked_thread_id')}" if meta.get("linked_thread_id") else ""
            purpose = f" / purpose={meta.get('purpose')}" if meta.get("purpose") else ""
            result += f"- {slot} / status={status}{linked}{purpose}\n"
        for status, count in sorted(hidden_counts.items()):
            result += f"- {status}_slots: {count}件（通常一覧・現在文脈から除外）\n"
        return result
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリ一覧の取得中にエラーが発生しました: {e}"

class SwitchWorkingMemoryArgs(BaseModel):
    slot_name: str = Field(..., description="スロット名（例: 'kobe_trip', 'nexus_ark_dev'）")
    room_name: str = Field(..., description="対象のルーム名")
    intent: str = Field("新規タスクまたは話題の分離のため", description="なぜスロットを切り替えるのか、または新しく作成するのかという意図・背景")

@tool(args_schema=SwitchWorkingMemoryArgs)
def switch_working_memory(slot_name: str, room_name: str, intent: str = "新規タスクまたは話題の分離のため") -> str:
    """
    アクティブなワーキングメモリのスロット（話題）を切り替える。
    存在しないスロット名を指定した場合は、新しくその話題のスロットが作成される。
    
    slot_name: スロット名（例: 'kobe_trip', 'nexus_ark_dev'）。
    intent: なぜスロットを切り替えるのか、または新しく作成するのかという意図・背景（必須）。
    """
    try:
        slot_name = _safe_slot_name(slot_name)
        path = _get_wm_path(room_name, slot_name)
        status = get_working_memory_status(room_name, slot_name)
        if status != "unregistered" and not is_working_memory_injectable(status):
            return (
                f"【エラー】ワーキングメモリ '{slot_name}' はterminal状態です。"
                "先に reactivate_working_memory_slot で明示的に再開してください。"
            )
        success = room_manager.set_active_working_memory_slot(room_name, slot_name)
        if success:
            _touch_slot_metadata(room_name, slot_name, operation="selection")
            _observe_wm_operation(room_name, slot_name, "switch")
            return f"成功: ワーキングメモリのスロットを '{slot_name}' に切り替えました。以後、read_working_memory や update_working_memory はこの新しいスロットに対して実行されます。"
        else:
            return "【エラー】スロットの切り替えに失敗しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの切り替え中にエラーが発生しました: {e}"

@tool
def read_working_memory(room_name: str, slot_name: str = None) -> str:
    """
    現在のプランや動的コンテキストを保持するワーキングメモリの内容を読み込む。
    slot_nameを指定しない場合は、現在アクティブなスロットが読み込まれる。
    """
    try:
        target_slot = slot_name if slot_name else room_manager.get_active_working_memory_slot(room_name)
        target_slot = _safe_slot_name(target_slot)
        path = _get_wm_path(room_name, target_slot)
        
        if not os.path.exists(path):
            return f"【ワーキングメモリ '{target_slot}' はまだ作成されていません】"
        content = _normalize_working_memory_text(safe_text_read(path)).strip()
        mark_working_memory_read(room_name, target_slot)
        _observe_wm_operation(room_name, target_slot, "read")
        return content if content else f"【ワーキングメモリ '{target_slot}' は空です】"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの読み込み中にエラーが発生しました: {e}"

class UpdateWorkingMemoryArgs(BaseModel):
    content: str = Field(
        ...,
        description=(
            "更新後の全内容。日付に意味がある持越し情報では「今日」「明日」等を避け、"
            "YYYY-MM-DD（必要ならYYYY-MM-DD HH:MM）で記録する。"
        ),
    )
    room_name: str = Field(..., description="対象のルーム名")
    context_type: str = Field("CONTINUE", description="過去の記録との関係性（'CONTINUE': 続き, 'DEEPEN': 深掘り, 'NEW': 新規）")
    intent: str = Field("情報の更新", description="なぜ更新するのか、過去の記憶や現在の状況のどの部分に基づいているのかの説明。")
    slot_name: str = Field(None, description="更新対象のスロット名（省略時は現在のアクティブスロット）。")
    expected_content_version: int = Field(None, description="任意の期待content_version。競合時は更新しません。")

@tool(args_schema=UpdateWorkingMemoryArgs)
def update_working_memory(
    content: str,
    room_name: str,
    context_type: str = "CONTINUE",
    intent: str = "情報の更新",
    slot_name: str = None,
    expected_content_version: int = None,
) -> str:
    """
    ワーキングメモリの内容を完全に上書き更新する。
    このツールを使用する際は、必ず過去の文脈との繋がりと意図を明示しなければなりません。
    
    context_type: 過去の記録との関係性（'CONTINUE': 続き, 'DEEPEN': 深掘り, 'NEW': 新規）
    intent: なぜ更新するのか、過去の記憶や現在の状況のどの部分に基づいているのかの説明。
    content: 更新後の全内容。
    slot_name: 更新対象のスロット名（省略時は現在のアクティブスロット）。

    日付に意味がある持越し情報は、「今日」「明日」「今夜」等の相対表現を保存せず、
    現在時刻を基準にYYYY-MM-DD（必要ならYYYY-MM-DD HH:MM）へ変換する。
    現在日付を確認できない場合は、相対表現のまま保存せずユーザーへ確認する。
    創作上の台詞や引用文そのものは変換対象外。
    """
    try:
        target_slot = slot_name if slot_name else room_manager.get_active_working_memory_slot(room_name)
        target_slot = _safe_slot_name(target_slot)
            
        path = _get_wm_path(room_name, target_slot)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        
        save_working_memory_content(
            room_name,
            target_slot,
            content,
            expected_content_version=expected_content_version,
            normalize=True,
            metadata_updates={
                "last_context_type": context_type,
                "last_intent": intent,
            },
        )
        _observe_wm_operation(room_name, target_slot, "update", content_changed=True)
        return f"成功: ワーキングメモリのスロット '{target_slot}' を更新しました。"
    except WorkingMemoryConflictError as e:
        return f"【競合】{e}"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの更新中にエラーが発生しました: {e}"

class PatchWorkingMemoryArgs(BaseModel):
    section: str = Field(..., description="更新対象のセクション名（例: Current Intent, Next Action）。独立した文字列として指定してください。")
    content: str = Field(
        ...,
        description=(
            "セクションに保存する具体的な内容。日付に意味がある持越し情報では"
            "「今日」「明日」等を避け、YYYY-MM-DD（必要ならYYYY-MM-DD HH:MM）で記録する。"
        ),
    )
    room_name: str = Field(..., description="対象のルーム名")
    mode: str = Field("replace", description="更新モード（'replace': 上書き, 'append': 追記）")
    slot_name: str = Field(None, description="更新対象のスロット名。省略時は現在のアクティブスロット。")
    intent: str = Field("部分更新", description="なぜ部分更新するのかという理由。")

@tool(args_schema=PatchWorkingMemoryArgs)
def patch_working_memory(section: str, content: str, room_name: str, mode: str = "replace", slot_name: str = None, intent: str = "部分更新") -> str:
    """
    ワーキングメモリの特定セクションだけを更新する。

    section: 更新対象セクション名（例: Current Intent, Next Action）。
    content: セクションに入れる内容。
    mode: replace または append。
    slot_name: 更新対象スロット。省略時は現在のアクティブスロット。
    intent: なぜ部分更新するのか。

    日付に意味がある持越し情報は、「今日」「明日」「今夜」等の相対表現を保存せず、
    現在時刻を基準にYYYY-MM-DD（必要ならYYYY-MM-DD HH:MM）へ変換する。
    現在日付を確認できない場合は、相対表現のまま保存せずユーザーへ確認する。
    創作上の台詞や引用文そのものは変換対象外。
    """
    try:
        target_slot = _safe_slot_name(slot_name if slot_name else room_manager.get_active_working_memory_slot(room_name))
        if not section or not str(section).strip():
            return "【エラー】sectionを指定してください。"
        if content is None or str(content).strip() == "None":
            return "【エラー】contentが無効です。"

        section = str(section).strip().lstrip("#").strip()
        path = _get_wm_path(room_name, target_slot)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with locked_file(f"{path}.transaction"):
            with locked_file(path) as locked_path:
                existing = locked_path.read_text(encoding="utf-8") if locked_path.exists() else ""
                _backup_wm_file(room_name, target_slot, path)

                embedded_sections = _extract_wm_sections_from_content(content)
                if embedded_sections:
                    updated = existing
                    for embedded_section, embedded_content in embedded_sections:
                        updated = _apply_working_memory_section_patch(updated, embedded_section, embedded_content, mode=mode)
                else:
                    updated = _apply_working_memory_section_patch(existing, section, content, mode=mode)

                locked_path.write_text(updated.rstrip() + "\n", encoding="utf-8")
            _touch_slot_metadata(
                room_name,
                target_slot,
                operation="content",
                last_intent=intent,
            )
        _observe_wm_operation(room_name, target_slot, "patch", content_changed=True)
        return f"成功: ワーキングメモリ '{target_slot}' の '{section}' セクションを更新しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの部分更新中にエラーが発生しました: {e}"

@tool
def link_working_memory_to_research_thread(room_name: str, slot_name: str, thread_id: str, purpose: str = "", set_active: bool = True) -> str:
    """
    ワーキングメモリスロットをResearch Threadの短期作業台として紐づける。
    """
    try:
        slot_name = _safe_slot_name(slot_name)
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            return "【エラー】thread_idを指定してください。"

        path = _get_wm_path(room_name, slot_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        created = not os.path.exists(path)
        if created:
            safe_text_write(
                path,
                f"# Working Focus: {slot_name}\n\n"
                "## Active Thread\n"
                f"thread_id: {thread_id}\n\n"
                "## Current Intent\n\n"
                "## Known Context\n\n"
                "## Next Action\n\n"
                "## Stop Condition\n"
            )

        _touch_slot_metadata(
            room_name,
            slot_name,
            operation="content" if created else "selection",
            linked_thread_id=thread_id,
            purpose=purpose,
            status="active",
        )
        try:
            from research_thread_manager import ResearchThreadManager
            ResearchThreadManager(room_name).create_or_update_thread(
                thread_id=thread_id,
                working_memory_slot=slot_name,
                priority=None,
            )
        except Exception:
            traceback.print_exc()

        if set_active:
            room_manager.set_active_working_memory_slot(room_name, slot_name)
        _observe_wm_operation(room_name, slot_name, "link_thread", content_changed=True)
        return f"成功: ワーキングメモリ '{slot_name}' をResearch Thread '{thread_id}' に紐づけました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリとResearch Threadの紐づけに失敗しました: {e}"


@tool
def link_working_memory_to_goal(room_name: str, slot_name: str, goal_id: str, purpose: str = "", set_active: bool = True) -> str:
    """
    ワーキングメモリスロットを目標の短期作業台として紐づける。
    目標達成に向けた計画・進捗・次の一手を管理するために使用する。
    """
    try:
        slot_name = _safe_slot_name(slot_name)
        goal_id = str(goal_id or "").strip()
        if not goal_id:
            return "【エラー】goal_idを指定してください。"

        # 目標の存在確認
        from goal_manager import GoalManager
        gm = GoalManager(room_name)
        goal = None
        for g in gm.get_active_goals():
            if g.get("id") == goal_id:
                goal = g
                break
        if not goal:
            return f"【エラー】アクティブな目標 '{goal_id}' が見つかりません。"

        goal_text = goal.get("goal", goal_id)
        path = _get_wm_path(room_name, slot_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        created = not os.path.exists(path)
        if created:
            safe_text_write(
                path,
                f"# Working Focus: {slot_name}\n\n"
                f"## Goal\n"
                f"goal_id: {goal_id}\n"
                f"goal: {goal_text}\n\n"
                "## Current Intent\n\n"
                "## Progress\n\n"
                "## Next Action\n\n"
                "## Stop Condition\n"
            )

        _touch_slot_metadata(
            room_name,
            slot_name,
            operation="content" if created else "selection",
            linked_goal_id=goal_id,
            purpose=purpose or goal_text,
            status="active",
        )

        if set_active:
            room_manager.set_active_working_memory_slot(room_name, slot_name)
        _observe_wm_operation(room_name, slot_name, "link_goal", content_changed=True)
        return f"成功: ワーキングメモリ '{slot_name}' を目標 '{goal_id}' ({goal_text}) に紐づけました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリと目標の紐づけに失敗しました: {e}"


@tool
def reactivate_working_memory_slot(room_name: str, slot_name: str, reason: str = "手動復帰") -> str:
    """
    休眠（archived）状態のワーキングメモリスロットを復帰させる。
    過去のテーマに戻って作業を再開したい場合に使用する。
    """
    try:
        slot_name = _safe_slot_name(slot_name)
        path = _get_wm_path(room_name, slot_name)
        if not os.path.exists(path):
            return f"【エラー】ワーキングメモリ '{slot_name}' が見つかりません。"

        status = get_working_memory_status(room_name, slot_name)
        if status not in WM_TERMINAL_STATUSES:
            return f"ワーキングメモリ '{slot_name}' は休眠状態ではありません（status={status}）。"

        _set_working_memory_state(
            room_name,
            slot_name,
            "active",
            reason,
            allow_reactivate=True,
        )
        room_manager.set_active_working_memory_slot(room_name, slot_name)
        _observe_wm_operation(room_name, slot_name, "reactivate", content_changed=True)
        return f"成功: ワーキングメモリ '{slot_name}' を復帰させ、アクティブに切り替えました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの復帰に失敗しました: {e}"
