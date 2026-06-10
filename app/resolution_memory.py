"""
Helpers for preserving question/goal resolution insights.

This module keeps "what was learned" separate from episodic memories so that
thin template entries are not created when a question or goal is merely marked
resolved/completed without a substantive reflection.
"""

import datetime
import re
from pathlib import Path
from typing import Dict, Optional

import constants
from file_lock_utils import safe_json_update


MIN_REFLECTION_CHARS = 24


def is_substantive_reflection(text: Optional[str], min_chars: int = MIN_REFLECTION_CHARS) -> bool:
    """Return True when text is specific enough to preserve as a special memory."""
    if not text:
        return False
    normalized = re.sub(r"\s+", "", str(text))
    if len(normalized) < min_chars:
        return False

    thin_phrases = [
        "解決した",
        "達成した",
        "一つの区切り",
        "特になし",
        "なし",
        "わかった",
        "理解した",
    ]
    return normalized not in thin_phrases


def save_resolution_insight(
    room_name: str,
    trigger_topic: str,
    insight: str,
    strategy: str = "",
    log_entry: str = "",
    source_type: str = "resolution",
    metadata: Optional[Dict] = None,
) -> bool:
    """Save a resolution insight to the monthly dreaming/insight store."""
    if not room_name or not insight or not str(insight).strip():
        return False

    now = datetime.datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    month_str = now.strftime("%Y-%m")
    memory_dir = Path(constants.ROOMS_DIR) / room_name / "memory"
    dreaming_dir = memory_dir / "dreaming"
    dreaming_dir.mkdir(parents=True, exist_ok=True)
    path = dreaming_dir / f"{month_str}.json"

    record = {
        "created_at": now_str,
        "trigger_topic": trigger_topic,
        "insight": str(insight).strip(),
        "strategy": str(strategy or "").strip(),
        "log_entry": str(log_entry or f"{trigger_topic} から得た気づき").strip(),
        "source_type": source_type,
    }
    if metadata:
        record["metadata"] = metadata

    def update_func(data):
        if not isinstance(data, list):
            data = []
        data.insert(0, record)
        return data[:100]

    return bool(safe_json_update(str(path), update_func, default=[]))
