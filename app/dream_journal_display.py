"""夢日記の表示用テキストを組み立てる純粋なformatter。"""

from collections.abc import Mapping
from typing import Any


def format_dream_journal_entry(record: Mapping[str, Any]) -> str:
    """夢日記1件を本体・Lite API共通のMarkdownテキストへ整形する。"""
    created_at = record.get("created_at") or record.get("date") or "（記録なし）"
    trigger_topic = record.get("trigger_topic") or "話題なし"
    insight = record.get("insight") or "（記録なし）"
    log_entry = record.get("log_entry") or "（記録なし）"
    strategy = record.get("strategy") or "（記録なし）"
    return (
        f"【日付】 {created_at}\n"
        f"【トリガー】 {trigger_topic}\n\n"
        f"## 💡 得られた洞察 (Insight)\n"
        f"{insight}\n\n"
        f"## 💭 夢の日記 (Dream Log)\n"
        f"{log_entry}\n\n"
        f"## 🧭 今後の指針 (Strategy)\n"
        f"{strategy}"
    )
