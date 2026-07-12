import datetime
import json
from pathlib import Path
from typing import Dict, List, Optional

import constants
from file_lock_utils import safe_json_read, safe_json_write


VALID_TRIGGERS = {"chat", "autonomous", "scheduled", "system_proxy"}


def _stats_path(room_name: str) -> Path:
    return Path(constants.ROOMS_DIR) / room_name / "memory" / "tool_usage_stats.json"


def _empty_stats() -> Dict:
    return {"tools": {}, "updated_at": None}


def _normalize_trigger(trigger: str) -> str:
    value = str(trigger or "chat").strip().lower()
    return value if value in VALID_TRIGGERS else "chat"


def _normalize_status(status: str) -> str:
    value = str(status or "ok").strip().lower()
    return value if value in {"ok", "error"} else "ok"


def _parse_datetime(value: object) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        return None


def get_stats(room_name: str) -> Dict:
    try:
        data = safe_json_read(str(_stats_path(room_name)), default=_empty_stats())
        if not isinstance(data, dict):
            return _empty_stats()
        tools = data.get("tools")
        if not isinstance(tools, dict):
            data["tools"] = {}
        return data
    except Exception:
        return _empty_stats()


def record_usage(room_name: str, tool_name: str, trigger: str = "chat", status: str = "ok") -> bool:
    """ツール利用統計を更新する。失敗しても呼び出し元の行動経路は止めない。"""
    if not room_name or not tool_name:
        return False

    try:
        path = _stats_path(room_name)
        data = get_stats(room_name)
        now = datetime.datetime.now().isoformat()
        trigger = _normalize_trigger(trigger)
        status = _normalize_status(status)
        tools = data.setdefault("tools", {})
        entry = tools.setdefault(
            str(tool_name),
            {
                "count_total": 0,
                "count_autonomous": 0,
                "count_scheduled": 0,
                "count_system_proxy": 0,
                "count_error": 0,
                "last_used_at": None,
                "last_used_autonomous_at": None,
                "last_used_scheduled_at": None,
                "last_used_system_proxy_at": None,
                "last_error_at": None,
            },
        )
        entry.setdefault("count_system_proxy", 0)
        entry.setdefault("count_error", 0)
        entry.setdefault("last_used_system_proxy_at", None)
        entry.setdefault("last_error_at", None)
        if status == "error":
            entry["count_error"] = int(entry.get("count_error", 0) or 0) + 1
            entry["last_error_at"] = now
            data["updated_at"] = now
            return safe_json_write(str(path), data)

        entry["count_total"] = int(entry.get("count_total", 0) or 0) + 1
        entry["last_used_at"] = now
        if trigger == "autonomous":
            entry["count_autonomous"] = int(entry.get("count_autonomous", 0) or 0) + 1
            entry["last_used_autonomous_at"] = now
        elif trigger == "scheduled":
            entry["count_scheduled"] = int(entry.get("count_scheduled", 0) or 0) + 1
            entry["last_used_scheduled_at"] = now
        elif trigger == "system_proxy":
            entry["count_system_proxy"] = int(entry.get("count_system_proxy", 0) or 0) + 1
            entry["last_used_system_proxy_at"] = now
        data["updated_at"] = now
        return safe_json_write(str(path), data)
    except Exception as e:
        print(f"  - [tool_usage_stats] 記録をスキップしました: {e}")
        return False


def days_since_last_autonomous_use(room_name: str, tool_names: List[str]) -> Optional[int]:
    """代表ツール群の最後の自律利用からの日数を返す。未使用ならNone。"""
    try:
        data = get_stats(room_name)
        latest: Optional[datetime.datetime] = None
        for tool_name in tool_names or []:
            entry = data.get("tools", {}).get(tool_name, {})
            used_at = _parse_datetime(entry.get("last_used_autonomous_at"))
            if used_at and (latest is None or used_at > latest):
                latest = used_at
        if latest is None:
            return None
        return max(0, (datetime.datetime.now() - latest).days)
    except Exception:
        return None


def rebuild_from_action_logs(room_name: str) -> Dict:
    """既存action_logから統計を再構築する。trigger欠落分はtotalのみ計上する。"""
    rebuilt = _empty_stats()
    run_logs = Path(constants.ROOMS_DIR) / room_name / "memory" / "run_logs"
    try:
        for path in sorted(run_logs.glob("action_log_*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                tool_name = str(record.get("tool_name") or "")
                if not tool_name:
                    continue
                timestamp = str(record.get("timestamp") or rebuilt.get("updated_at") or datetime.datetime.now().isoformat())
                trigger = _normalize_trigger(record.get("trigger", "chat"))
                entry = rebuilt["tools"].setdefault(
                    tool_name,
                    {
                        "count_total": 0,
                        "count_autonomous": 0,
                        "count_scheduled": 0,
                        "count_system_proxy": 0,
                        "count_error": 0,
                        "last_used_at": None,
                        "last_used_autonomous_at": None,
                        "last_used_scheduled_at": None,
                        "last_used_system_proxy_at": None,
                        "last_error_at": None,
                    },
                )
                entry.setdefault("count_system_proxy", 0)
                entry.setdefault("count_error", 0)
                entry.setdefault("last_used_system_proxy_at", None)
                entry.setdefault("last_error_at", None)
                status = _normalize_status(record.get("status", "ok"))
                if status == "error":
                    entry["count_error"] += 1
                    entry["last_error_at"] = timestamp
                    rebuilt["updated_at"] = timestamp
                    continue
                entry["count_total"] += 1
                entry["last_used_at"] = timestamp
                if trigger == "autonomous":
                    entry["count_autonomous"] += 1
                    entry["last_used_autonomous_at"] = timestamp
                elif trigger == "scheduled":
                    entry["count_scheduled"] += 1
                    entry["last_used_scheduled_at"] = timestamp
                elif trigger == "system_proxy":
                    entry["count_system_proxy"] += 1
                    entry["last_used_system_proxy_at"] = timestamp
                rebuilt["updated_at"] = timestamp
        safe_json_write(str(_stats_path(room_name)), rebuilt)
    except Exception as e:
        print(f"  - [tool_usage_stats] 再構築をスキップしました: {e}")
    return rebuilt
