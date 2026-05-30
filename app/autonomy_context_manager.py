"""Autonomy context and reflection helpers.

This module provides a small bridge between the existing memory/autonomy
components and future step/timeline-style agent runtimes.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import constants


VALID_STEP_TYPES = {"observe", "orient", "decide", "act", "reflect"}


class AutonomyContextManager:
    """Builds compact autonomy context and stores reflection records."""

    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.memory_dir = self.room_dir / "memory"
        self.timeline_dir = self.memory_dir / "autonomy_timeline"
        self.timeline_dir.mkdir(parents=True, exist_ok=True)

    def build_context(self, query: str = "", include_details: bool = False) -> Dict[str, Any]:
        """Return a compact Sense/Orient context for autonomous action."""
        context = {
            "room_name": self.room_name,
            "generated_at": self._now(),
            "query": query or "",
            "purpose_profile": self._purpose_summary(),
            "goals": self._active_goals(limit=5),
            "research_threads": self._research_threads(query=query, limit=5),
            "working_memory": self._working_memory_context(include_details=include_details),
            "recent_actions": self._recent_actions(limit=8),
            "attention_rhythm": self._attention_rhythm(),
            "guidance": [
                "Prefer CONTINUE/DEEPEN when an existing thread or working memory slot still feels alive.",
                "Return to EXPLORE/CREATIVE/SOCIAL/REST when focus has become repetition rather than intention.",
                "Choose one concrete action and one stop condition before acting.",
                "After acting, call reflect_after_action or update Working Memory / Research Thread next_action.",
            ],
        }
        return context

    def format_context(self, query: str = "", include_details: bool = False) -> str:
        context = self.build_context(query=query, include_details=include_details)
        lines = [
            "【Autonomy Context】",
            f"- generated_at: {context['generated_at']}",
            f"- query: {context.get('query') or '(none)'}",
        ]

        purpose = context.get("purpose_profile") or ""
        if purpose:
            lines.extend(["", "## Purpose Profile", purpose.strip()])

        goals = context.get("goals") or []
        lines.append("")
        lines.append("## Active Goals")
        if goals:
            for goal in goals:
                lines.append(f"- {goal.get('id')}: {goal.get('goal')} (priority={goal.get('priority')})")
        else:
            lines.append("- （アクティブな目標はありません）")

        threads = context.get("research_threads") or []
        lines.append("")
        lines.append("## Research Threads")
        if threads:
            for thread in threads:
                lines.append(
                    f"- {thread.get('thread_id')}: {thread.get('title')} "
                    f"(priority={thread.get('priority')}, score={thread.get('match_score', '-')})"
                )
                if thread.get("next_action"):
                    lines.append(f"  next_action: {thread.get('next_action')}")
                if thread.get("working_memory_slot"):
                    lines.append(f"  working_memory_slot: {thread.get('working_memory_slot')}")
        else:
            lines.append("- （候補Research Threadはありません）")

        wm = context.get("working_memory") or {}
        lines.append("")
        lines.append("## Working Memory")
        if wm.get("selected_slot"):
            lines.append(f"- selected_slot: {wm.get('selected_slot')}")
        if wm.get("overview"):
            lines.append(wm.get("overview").strip())
        if wm.get("active_content"):
            lines.append("")
            lines.append("### Active Slot Content")
            lines.append(wm.get("active_content").strip()[:4000])
        if not wm.get("overview") and not wm.get("active_content"):
            lines.append("- （ワーキングメモリは未設定です）")

        recent_actions = context.get("recent_actions") or ""
        lines.append("")
        lines.append("## Recent Actions")
        lines.append(recent_actions.strip() if recent_actions else "（直近のアクションログはありません）")

        attention = context.get("attention_rhythm") or ""
        if attention:
            lines.append("")
            lines.append("## Attention Rhythm")
            lines.append(attention.strip())

        lines.append("")
        lines.append("## Required Loop")
        lines.extend([
            "1. Sense: 上の目的・目標・研究・WM・直近行動を観察する。",
            "2. Orient: 今回の行動を CONTINUE / DEEPEN / NEW / CREATIVE / SOCIAL / REST のどれかに分類する。",
            "3. Decide: 対象、意図、停止条件を1つに絞る。",
            "4. Act: 必要なツールを実行する。",
            "5. Reflect: reflect_after_action または既存更新ツールで、次に戻る場所を残す。",
        ])

        return "\n".join(lines).strip()

    def append_reflection(
        self,
        action_summary: str,
        outcome_type: str,
        next_action: str,
        intent: str = "",
        context_type: str = "CONTINUE",
        thread_id: str = "",
        working_memory_slot: str = "",
        goal_id: str = "",
        unresolved_questions: Optional[List[str]] = None,
        update_thread: bool = False,
        update_goal: bool = False,
        timeline_id: str = "",
    ) -> Dict[str, Any]:
        """Persist a reflection record and optionally update linked state."""
        now = self._now()
        timeline_id = self._safe_timeline_id(timeline_id) or self._new_timeline_id(now)
        record = {
            "timestamp": now,
            "timeline_id": timeline_id,
            "context_type": (context_type or "CONTINUE").upper(),
            "intent": intent or "",
            "action_summary": action_summary or "",
            "outcome_type": outcome_type or "observed",
            "next_action": next_action or "",
            "thread_id": thread_id or "",
            "working_memory_slot": working_memory_slot or "",
            "goal_id": goal_id or "",
            "unresolved_questions": unresolved_questions or [],
        }

        path = self._daily_reflection_path(now[:10])
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        updates = {}
        if update_thread and thread_id and next_action:
            updates["research_thread"] = self._update_thread_next_action(
                thread_id=thread_id,
                next_action=next_action,
                unresolved_questions=unresolved_questions or [],
            )
        if update_goal and goal_id and action_summary:
            updates["goal"] = self._update_goal_progress(goal_id=goal_id, progress_note=action_summary)
        record["updates"] = updates

        self.append_step(
            timeline_id=timeline_id,
            step_type="reflect",
            summary=action_summary or "行動後Reflect",
            details={
                "outcome_type": outcome_type or "observed",
                "next_action": next_action or "",
                "intent": intent or "",
                "context_type": (context_type or "CONTINUE").upper(),
                "unresolved_questions": unresolved_questions or [],
                "updates": updates,
            },
            thread_id=thread_id,
            working_memory_slot=working_memory_slot,
            goal_id=goal_id,
        )
        return record

    def start_timeline(
        self,
        trigger: str = "",
        query: str = "",
        motivation: str = "",
        source: str = "autonomous",
    ) -> Dict[str, Any]:
        """Create a typed autonomy timeline start event."""
        now = self._now()
        timeline_id = self._new_timeline_id(now)
        record = {
            "event_type": "timeline_start",
            "version": 1,
            "timeline_id": timeline_id,
            "room_name": self.room_name,
            "timestamp": now,
            "source": source or "autonomous",
            "trigger": trigger or "",
            "query": query or "",
            "motivation": motivation or "",
            "status": "active",
        }
        self._append_step_record(record, now[:10])
        return record

    def append_step(
        self,
        timeline_id: str,
        step_type: str,
        summary: str,
        details: Any = "",
        selected_action: str = "",
        tool_name: str = "",
        tool_result_summary: str = "",
        thread_id: str = "",
        working_memory_slot: str = "",
        goal_id: str = "",
        action_memory_ref: str = "",
    ) -> Dict[str, Any]:
        """Append an observe/orient/decide/act/reflect step."""
        now = self._now()
        timeline_id = self._safe_timeline_id(timeline_id) or self._new_timeline_id(now)
        step_type = str(step_type or "").strip().lower()
        if step_type not in VALID_STEP_TYPES:
            raise ValueError(f"step_type は {', '.join(sorted(VALID_STEP_TYPES))} のいずれかにしてください。")

        record = {
            "event_type": "step",
            "version": 1,
            "timeline_id": timeline_id,
            "step_id": f"{timeline_id}_{step_type}_{datetime.now().strftime('%H%M%S_%f')}",
            "room_name": self.room_name,
            "timestamp": now,
            "step_type": step_type,
            "summary": summary or "",
            "details": details if isinstance(details, (dict, list)) else str(details or ""),
            "selected_action": selected_action or "",
            "tool_name": tool_name or "",
            "tool_result_summary": tool_result_summary or "",
            "refs": {
                "thread_id": thread_id or "",
                "working_memory_slot": working_memory_slot or "",
                "goal_id": goal_id or "",
                "action_memory_ref": action_memory_ref or "",
            },
        }
        self._append_step_record(record, now[:10])
        return record

    def complete_timeline(self, timeline_id: str, status: str = "completed", summary: str = "") -> Dict[str, Any]:
        """Append a timeline completion event."""
        now = self._now()
        timeline_id = self._safe_timeline_id(timeline_id)
        if not timeline_id:
            raise ValueError("timeline_id が必要です。")
        record = {
            "event_type": "timeline_complete",
            "version": 1,
            "timeline_id": timeline_id,
            "room_name": self.room_name,
            "timestamp": now,
            "status": status or "completed",
            "summary": summary or "",
        }
        self._append_step_record(record, now[:10])
        return record

    def _daily_reflection_path(self, date_str: str) -> Path:
        return self.timeline_dir / f"autonomy_reflections_{date_str}.jsonl"

    def _daily_step_path(self, date_str: str) -> Path:
        return self.timeline_dir / f"autonomy_steps_{date_str}.jsonl"

    def _append_step_record(self, record: Dict[str, Any], date_str: str) -> None:
        path = self._daily_step_path(date_str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _purpose_summary(self) -> str:
        try:
            from purpose_profile_manager import PurposeProfileManager

            return PurposeProfileManager(self.room_name).get_summary_for_prompt(max_items=5).strip()
        except Exception as e:
            return f"（Purpose Profile取得エラー: {e}）"

    def _active_goals(self, limit: int = 5) -> List[Dict[str, Any]]:
        try:
            from goal_manager import GoalManager

            goals = GoalManager(self.room_name).get_active_goals()
            goals = sorted(goals, key=lambda g: g.get("priority", 999))
            return goals[:limit]
        except Exception:
            return []

    def _research_threads(self, query: str = "", limit: int = 5) -> List[Dict[str, Any]]:
        try:
            from research_thread_manager import ResearchThreadManager

            manager = ResearchThreadManager(self.room_name)
            if query:
                matches = manager.find_similar_threads(query=query, limit=limit, boost_by_purpose=True)
                if matches:
                    return matches[:limit]
            return manager.list_threads(status="active", boost_by_purpose=True)[:limit]
        except Exception:
            return []

    def _working_memory_context(self, include_details: bool = False) -> Dict[str, str]:
        result = {"selected_slot": "", "overview": "", "active_content": ""}
        try:
            import room_manager
            from tools.working_memory_tools import get_working_memory_overview, read_working_memory

            result["selected_slot"] = room_manager.get_active_working_memory_slot(self.room_name) or ""
            result["overview"] = get_working_memory_overview(self.room_name, limit=8) or ""
            if include_details and result["selected_slot"]:
                result["active_content"] = read_working_memory.invoke({
                    "room_name": self.room_name,
                    "slot_name": result["selected_slot"],
                })
        except Exception as e:
            result["overview"] = f"（Working Memory取得エラー: {e}）"
        return result

    def _recent_actions(self, limit: int = 8) -> str:
        try:
            import action_logger

            return action_logger.get_recent_actions(self.room_name, limit=limit)
        except Exception as e:
            return f"（Action Memory取得エラー: {e}）"

    def _attention_rhythm(self) -> str:
        try:
            from attention_rhythm_manager import AttentionRhythmManager

            return AttentionRhythmManager(self.room_name).format_summary()
        except Exception as e:
            return f"（Attention Rhythm取得エラー: {e}）"

    def _update_thread_next_action(
        self,
        thread_id: str,
        next_action: str,
        unresolved_questions: List[str],
    ) -> str:
        try:
            from research_thread_manager import ResearchThreadManager

            manager = ResearchThreadManager(self.room_name)
            thread = manager.get_thread(thread_id)
            if not thread:
                return "skipped: thread not found"
            manager.create_or_update_thread(
                thread_id=thread_id,
                title=thread.get("title", ""),
                status=thread.get("status", "active"),
                priority=thread.get("priority", 0.5),
                working_memory_slot=thread.get("working_memory_slot", ""),
                open_questions=unresolved_questions or None,
                next_action=next_action,
            )
            return "updated"
        except Exception as e:
            return f"error: {e}"

    def _update_goal_progress(self, goal_id: str, progress_note: str) -> str:
        try:
            from goal_manager import GoalManager

            GoalManager(self.room_name).update_goal_progress(goal_id, progress_note)
            return "updated"
        except Exception as e:
            return f"error: {e}"

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _new_timeline_id(now: str = "") -> str:
        timestamp = (now or datetime.now().isoformat(timespec="seconds")).replace("-", "").replace(":", "")
        timestamp = timestamp.replace("T", "_")
        suffix = datetime.now().strftime("%f")
        return f"auto_{timestamp}_{suffix}"

    @staticmethod
    def _safe_timeline_id(timeline_id: str) -> str:
        value = str(timeline_id or "").strip()
        if not value:
            return ""
        safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in value)
        return safe[:120]
