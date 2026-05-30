"""Attention rhythm support for autonomous actions.

This module does not decide what the persona must do. It only summarizes
whether recent autonomous behavior looks focused, exploratory, or imbalanced,
then offers a soft next-mode suggestion.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import constants


FOCUS_CONTEXT_TYPES = {"CONTINUE", "DEEPEN", "ORGANIZE"}
EXPLORE_CONTEXT_TYPES = {"NEW", "CREATIVE", "SOCIAL", "REST"}


class AttentionRhythmManager:
    """Build lightweight attention-mode guidance for autonomous actions."""

    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.memory_dir = self.room_dir / "memory"

    def build_summary(self, recent_limit: int = 8) -> Dict[str, Any]:
        reflections = self._recent_reflections(limit=recent_limit)
        focus_streak = self._context_streak(reflections, FOCUS_CONTEXT_TYPES)
        explore_streak = self._context_streak(reflections, EXPLORE_CONTEXT_TYPES)
        dominant_streak = self._dominant_drive_streak()
        active_goals = self._active_goal_count()
        open_questions = self._open_question_count()
        recent_tools = self._recent_tool_counts(limit=12)

        reasons: List[str] = []
        suggested_mode = "FOCUS"

        goal_question_gap = active_goals - open_questions
        if focus_streak >= 3:
            suggested_mode = "EXPLORE"
            reasons.append(f"FOCUS系の自律行動が{focus_streak}回続いています。")
        if dominant_streak.get("drive") == "goal_achievement" and dominant_streak.get("count", 0) >= 3:
            suggested_mode = "EXPLORE"
            reasons.append("目標達成欲が連続して最強動機になっています。")
        if active_goals >= 4 and goal_question_gap >= 3:
            suggested_mode = "EXPLORE"
            reasons.append(f"アクティブ目標{active_goals}件に対して未解決問いが{open_questions}件と少なめです。")
        if open_questions >= 8:
            suggested_mode = "SYNTHESIZE"
            reasons.append("未解決問いが多いため、整理・統合が有効です。")
        if not reflections and not reasons:
            suggested_mode = "EXPLORE"
            reasons.append("直近の自律行動履歴が少ないため、広く観察する余地があります。")
        if not reasons:
            reasons.append("現在の注意リズムに強い偏りはありません。")

        return {
            "suggested_mode": suggested_mode,
            "reasons": reasons,
            "focus_streak": focus_streak,
            "explore_streak": explore_streak,
            "dominant_drive_streak": dominant_streak,
            "active_goal_count": active_goals,
            "open_question_count": open_questions,
            "recent_tool_counts": recent_tools,
            "guidance": self._guidance_for_mode(suggested_mode),
        }

    def format_summary(self, recent_limit: int = 8) -> str:
        summary = self.build_summary(recent_limit=recent_limit)
        lines = [
            "【Attention Rhythm】",
            f"- suggested_mode: {summary['suggested_mode']}",
            f"- focus_streak: {summary['focus_streak']}",
            f"- explore_streak: {summary['explore_streak']}",
            f"- active_goals/open_questions: {summary['active_goal_count']}/{summary['open_question_count']}",
        ]
        drive = summary.get("dominant_drive_streak") or {}
        if drive.get("drive"):
            lines.append(f"- dominant_drive_streak: {drive.get('drive')} x{drive.get('count')}")

        tools = summary.get("recent_tool_counts") or {}
        if tools:
            tool_text = ", ".join(f"{name}={count}" for name, count in list(tools.items())[:5])
            lines.append(f"- recent_tools: {tool_text}")

        lines.append("")
        lines.append("## Reasons")
        for reason in summary.get("reasons") or []:
            lines.append(f"- {reason}")

        lines.append("")
        lines.append("## Guidance")
        for item in summary.get("guidance") or []:
            lines.append(f"- {item}")
        lines.append("- これは命令ではありません。最終的な行動モードは、現在の意志として選び直してください。")
        return "\n".join(lines)

    def _recent_reflections(self, limit: int) -> List[Dict[str, Any]]:
        timeline_dir = self.memory_dir / "autonomy_timeline"
        if not timeline_dir.exists():
            return []
        records: List[Dict[str, Any]] = []
        for path in sorted(timeline_dir.glob("autonomy_reflections_*.jsonl"), reverse=True):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for line in reversed(lines):
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
                if len(records) >= limit:
                    return records
        return records

    def _context_streak(self, reflections: List[Dict[str, Any]], accepted: set[str]) -> int:
        count = 0
        for record in reflections:
            context_type = str(record.get("context_type", "")).upper()
            if context_type in accepted:
                count += 1
            else:
                break
        return count

    def _dominant_drive_streak(self) -> Dict[str, Any]:
        try:
            from motivation_manager import MotivationManager

            state = MotivationManager(self.room_name).get_internal_state()
            recent = state.get("recent_dominant_drives", [])
        except Exception:
            recent = []
        if not recent:
            return {"drive": "", "count": 0}
        first = recent[0]
        count = 0
        for drive in recent:
            if drive == first:
                count += 1
            else:
                break
        return {"drive": first, "count": count}

    def _active_goal_count(self) -> int:
        try:
            from goal_manager import GoalManager

            return len(GoalManager(self.room_name).get_active_goals())
        except Exception:
            return 0

    def _open_question_count(self) -> int:
        count = 0
        try:
            from motivation_manager import MotivationManager

            state = MotivationManager(self.room_name).get_internal_state()
            questions = state.get("drives", {}).get("curiosity", {}).get("open_questions", [])
            count += len([q for q in questions if not q.get("resolved_at")])
        except Exception:
            pass
        try:
            from purpose_profile_manager import PurposeProfileManager

            profile = PurposeProfileManager(self.room_name).load_profile()
            questions = profile.get("open_questions", [])
            count += len([q for q in questions if isinstance(q, dict)])
        except Exception:
            pass
        return count

    def _recent_tool_counts(self, limit: int) -> Dict[str, int]:
        today = datetime.now().strftime("%Y-%m-%d")
        path = self.memory_dir / "run_logs" / f"action_log_{today}.jsonl"
        if not path.exists():
            return {}
        counts: Dict[str, int] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except Exception:
            return counts
        for line in lines:
            try:
                name = json.loads(line).get("tool_name", "")
            except Exception:
                continue
            if name:
                counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))

    def _guidance_for_mode(self, mode: str) -> List[str]:
        if mode == "EXPLORE":
            return [
                "同じ意図を保ったまま、創作・画像・Web確認・SNS下書き・場所移動・休息など別の表現を検討する。",
                "目標を増やす前に、今の目標から生まれる未解決問いを1つ作ることを検討する。",
                "深掘りを続ける場合は、なぜ今も続けたいのかを短く言語化する。",
            ]
        if mode == "SYNTHESIZE":
            return [
                "未解決問いを統合し、似た問いをまとめる。",
                "解けた問いは manage_open_questions(resolve) で閉じる。",
                "次に深掘りする問いを1つだけ選ぶ。",
            ]
        return [
            "生きているNext ActionとStop Conditionがあるなら集中を続ける。",
            "一区切りがついたら、次回はEXPLORE/CREATIVE/SOCIAL/RESTのどれへ戻るかを残す。",
        ]
