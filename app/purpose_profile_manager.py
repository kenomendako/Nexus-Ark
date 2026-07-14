# purpose_profile_manager.py
"""
Purpose Profile manager.

Purpose Profile stores a persona's stable values, long-running interests,
active interests, open questions, and proposed changes. Stable fields are
user/admin controlled; persona tools can update only active fields and proposals.
"""

import json
import re
import unicodedata
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import constants
from utils import normalized_text_similarity
from resolution_memory import is_substantive_reflection, save_resolution_insight


STABLE_FIELDS = {"core_values", "stable_interests", "preferred_behaviors", "avoid_behaviors"}
PERSONA_MUTABLE_FIELDS = {"active_interests", "open_questions", "proposed_changes"}
PURPOSE_OPEN_QUESTIONS_MAX = 10
PURPOSE_ACTIVE_INTERESTS_MAX = 8
PURPOSE_DEDUP_SIMILARITY_THRESHOLD = 0.85
PURPOSE_QUESTION_DEFAULT_PRIORITY = 0.8


class PurposeProfileManager:
    """Manages characters/<room>/memory/purpose_profile.json."""

    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.memory_dir = self.room_dir / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.profile_path = self.memory_dir / constants.PURPOSE_PROFILE_FILENAME

    def default_profile(self) -> Dict[str, Any]:
        now = self._now()
        return {
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "core_values": [],
            "stable_interests": [],
            "preferred_behaviors": [
                "過去ノートを再読して差分を追記する",
                "同じ題材は新規ノートではなく既存テーマへ深化する",
                "仮説、反証、次の問いを残す",
            ],
            "avoid_behaviors": [
                "同一テーマの新規まとめを量産する",
                "Web巡回結果を保存して終わる",
            ],
            "active_interests": [],
            "open_questions": [],
            "proposed_changes": [],
            "metadata": {
                "stable_fields": sorted(STABLE_FIELDS),
                "persona_mutable_fields": sorted(PERSONA_MUTABLE_FIELDS),
            },
        }

    def ensure_profile(self) -> Dict[str, Any]:
        if not self.profile_path.exists():
            profile = self.default_profile()
            self._save(profile)
            return profile
        return self.load_profile()

    def load_profile(self) -> Dict[str, Any]:
        if not self.profile_path.exists():
            return self.ensure_profile()

        with open(self.profile_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        profile = self.default_profile()
        profile.update(data if isinstance(data, dict) else {})
        profile["metadata"] = {
            **self.default_profile().get("metadata", {}),
            **(profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}),
        }
        return profile

    def save_profile_from_ui(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Save the whole profile from trusted UI/admin editing."""
        if not isinstance(profile, dict):
            raise ValueError("Purpose Profile はJSONオブジェクトである必要があります。")
        normalized = self._normalize_profile(profile)
        self._save(normalized)
        return normalized

    def update_active_purpose(
        self,
        active_interests: Optional[List[Dict[str, Any]]] = None,
        open_questions: Optional[List[Dict[str, Any]]] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Persona-safe update for active interests and open questions."""
        profile = self.ensure_profile()
        now = self._now()

        if active_interests is not None:
            before_count = len(profile.get("active_interests", []))
            profile["active_interests"] = self._normalize_active_interests(active_interests, now)
            print(
                f"[Purpose Update] active_interests: {before_count}件 -> "
                f"{len(profile['active_interests'])}件 (reason: {reason or 'なし'})"
            )
        if open_questions is not None:
            before_count = len(profile.get("open_questions", []))
            normalized_questions = self._normalize_open_questions(
                open_questions,
                now,
                existing_items=profile.get("open_questions", []),
            )
            after_count = len(normalized_questions)
            if before_count > 0 and after_count < before_count / 2 and not reason.strip():
                raise ValueError(
                    "Purpose Profileのopen_questionsを半分未満に削減する場合はreasonが必要です。"
                    "read_purpose_profileで既存の問いを確認し、残す問いも含めた完全なリストと、"
                    "何をなぜ解決/削除したかをreasonに渡してください。"
                )
            profile["open_questions"] = normalized_questions
            print(
                f"[Purpose Update] open_questions: {before_count}件 -> "
                f"{after_count}件 (reason: {reason or 'なし'})"
            )

        profile["updated_at"] = now
        profile.setdefault("metadata", {})["last_persona_update_reason"] = reason or "active purpose update"
        self._save(profile)
        return profile

    def propose_change(self, field: str, proposal: str, reason: str, proposed_by: str = "persona") -> Dict[str, Any]:
        if field not in STABLE_FIELDS:
            raise ValueError(f"提案対象 field は {', '.join(sorted(STABLE_FIELDS))} のいずれかにしてください。")
        if not proposal.strip():
            raise ValueError("proposal が空です。")

        profile = self.ensure_profile()
        now = self._now()
        profile.setdefault("proposed_changes", []).append({
            "id": f"proposal_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
            "field": field,
            "proposal": proposal.strip(),
            "reason": reason.strip(),
            "proposed_by": proposed_by,
            "status": "pending",
            "created_at": now,
        })
        profile["updated_at"] = now
        self._save(profile)
        return profile

    def approve_change(self, proposal_id: str) -> Dict[str, Any]:
        profile = self.ensure_profile()
        proposals = profile.get("proposed_changes", [])
        target = None
        for item in proposals:
            if item.get("id") == proposal_id:
                target = item
                break
        if not target:
            raise ValueError(f"提案IDが見つかりません: {proposal_id}")
        if target.get("status") == "approved":
            return profile

        field = target.get("field")
        proposal = target.get("proposal", "").strip()
        if field not in STABLE_FIELDS:
            raise ValueError(f"提案対象 field が不正です: {field}")

        current = profile.setdefault(field, [])
        if not isinstance(current, list):
            current = []
            profile[field] = current
        if proposal and proposal not in current:
            current.append(proposal)

        target["status"] = "approved"
        target["approved_at"] = self._now()
        profile["updated_at"] = self._now()
        self._save(profile)
        return profile

    def discard_change(self, proposal_id: str, reason: str = "") -> Dict[str, Any]:
        """Mark a pending Purpose Profile proposal as discarded."""
        profile = self.ensure_profile()
        proposals = profile.get("proposed_changes", [])
        target = None
        for item in proposals:
            if item.get("id") == proposal_id:
                target = item
                break
        if not target:
            raise ValueError(f"提案IDが見つかりません: {proposal_id}")
        if target.get("status") == "discarded":
            return profile

        target["status"] = "discarded"
        target["discarded_at"] = self._now()
        if reason.strip():
            target["discard_reason"] = reason.strip()
        profile["updated_at"] = self._now()
        self._save(profile)
        return profile

    def sync_from_goal_closure(self, goal_id: str, goal_text: str = "", status: str = "completed") -> Dict[str, Any]:
        """
        Goal が completed/abandoned になった時に PP の goal 由来 active_interests を整理する。

        既存の Purpose Profile がないルームでは新規作成しない。
        """
        if not self.profile_path.exists():
            return self.default_profile()

        profile = self.load_profile()
        now = self._now()
        changed = False
        closed_status = status if status in {"completed", "abandoned"} else "closed"
        goal_key = self._normalize_text_for_dedup(goal_text)
        new_interests = []

        for item in profile.get("active_interests", []):
            if not isinstance(item, dict):
                new_interests.append(item)
                continue
            item_goal_id = str(item.get("goal_id", "")).strip()
            item_source = str(item.get("source", "")).strip()
            item_key = self._normalize_text_for_dedup(self._item_text(item, ["topic", "question"]))
            matches_id = bool(goal_id and item_goal_id == goal_id)
            matches_goal_text = (
                bool(goal_key and item_source == "goal" and item_key)
                and self._find_similar_index(goal_key, [item_key]) is not None
            )
            if matches_id or matches_goal_text:
                changed = True
                continue
            new_interests.append(item)

        if changed:
            profile["active_interests"] = new_interests
            profile["updated_at"] = now
            metadata = profile.setdefault("metadata", {})
            metadata["last_goal_sync_at"] = now
            metadata["last_goal_sync_reason"] = f"goal_{goal_id or 'unknown'}_{closed_status}"
            self._save(profile)
        return profile

    def sync_from_question_closure(self, question_text: str, action: str = "resolved") -> Dict[str, Any]:
        """
        Motivation/open question が resolved/removed になった時に PP の類似 open_questions を除去する。

        既存の Purpose Profile がないルームでは新規作成しない。
        """
        if not self.profile_path.exists():
            return self.default_profile()

        target_key = self._normalize_text_for_dedup(question_text)
        if not target_key:
            return self.load_profile()

        profile = self.load_profile()
        now = self._now()
        changed = False
        new_questions = []
        removed_questions = []

        for item in profile.get("open_questions", []):
            if isinstance(item, dict):
                item_key = self._normalize_text_for_dedup(self._item_text(item, ["question", "topic"]))
            else:
                item_key = self._normalize_text_for_dedup(str(item))
            if item_key and self._find_similar_index(target_key, [item_key]) is not None:
                changed = True
                removed_questions.append(item)
                continue
            new_questions.append(item)

        if changed:
            for item in removed_questions:
                self._record_open_question_trace(
                    item,
                    trace_kind="resolved" if action == "resolved" else "removed",
                    reason=f"question_{action}",
                )
            profile["open_questions"] = new_questions
            profile["updated_at"] = now
            metadata = profile.setdefault("metadata", {})
            metadata["last_question_sync_at"] = now
            metadata["last_question_sync_reason"] = f"question_{action}"
            self._save(profile)
        return profile

    def consolidate_from_sleep(
        self,
        dream_open_questions: Optional[List[Dict[str, Any]]] = None,
        motivation_questions: Optional[List[Dict[str, Any]]] = None,
        reflection_level: int = 1,
        reason: str = "sleep consolidation",
    ) -> Dict[str, Any]:
        """
        睡眠時整理で、Purpose Profileの可変領域を現在の研究・目標・問いから再構成する。

        安定領域は直接変更せず、週次以上の省察で高優先度の反復関心だけ提案に回す。
        """
        profile = self.ensure_profile()
        now = self._now()

        active_interests = list(profile.get("active_interests", []))
        open_questions = list(profile.get("open_questions", []))

        active_interests.extend(self._collect_research_thread_interests())
        active_interests.extend(self._collect_goal_interests())
        open_questions.extend(self._question_items_from_research_threads())
        open_questions.extend(self._question_items_from_motivation(motivation_questions or []))
        open_questions.extend(self._question_items_from_dream(dream_open_questions or []))

        profile["active_interests"] = self._normalize_active_interests(active_interests, now)
        profile["open_questions"] = self._normalize_open_questions(
            open_questions,
            now,
            existing_items=profile.get("open_questions", []),
        )

        if reflection_level >= 2:
            self._add_stable_interest_proposals(profile, now)

        metadata = profile.setdefault("metadata", {})
        metadata["last_sleep_consolidated_at"] = now
        metadata["last_sleep_consolidation_reason"] = reason
        profile["updated_at"] = now
        self._save(profile)
        return profile

    def get_summary_for_prompt(self, max_items: int = 5) -> str:
        profile = self.ensure_profile()

        def bullet_list(values: Any, key: str = "") -> List[str]:
            if not isinstance(values, list):
                return []
            lines = []
            for value in values[:max_items]:
                if isinstance(value, dict):
                    text = value.get(key) or value.get("topic") or value.get("question") or value.get("proposal") or ""
                    priority = value.get("priority")
                    if priority is not None:
                        text = f"{text} (priority: {priority})"
                else:
                    text = str(value)
                if text:
                    lines.append(f"- {text}")
            return lines

        sections = ["\n### Purpose Profile（目的意識）"]
        stable = bullet_list(profile.get("stable_interests"))
        active = bullet_list(profile.get("active_interests"), "topic")
        questions = bullet_list(profile.get("open_questions"), "question")
        preferred = bullet_list(profile.get("preferred_behaviors"))
        avoid = bullet_list(profile.get("avoid_behaviors"))

        if stable:
            sections.append("長期関心:\n" + "\n".join(stable))
        if active:
            sections.append("現在の関心:\n" + "\n".join(active))
        if questions:
            sections.append("目的に紐づく問い:\n" + "\n".join(questions))
        if preferred:
            sections.append("優先したい行動:\n" + "\n".join(preferred[:3]))
        if avoid:
            sections.append("避けたい行動:\n" + "\n".join(avoid[:3]))

        if len(sections) == 1:
            return ""
        return "\n\n".join(sections) + "\n"

    def to_pretty_json(self) -> str:
        return json.dumps(self.ensure_profile(), ensure_ascii=False, indent=2)

    def _normalize_profile(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        base = self.default_profile()
        merged = deepcopy(base)
        merged.update(profile)
        for field in STABLE_FIELDS | PERSONA_MUTABLE_FIELDS:
            if not isinstance(merged.get(field), list):
                merged[field] = []
        if not isinstance(merged.get("metadata"), dict):
            merged["metadata"] = {}
        merged["version"] = int(merged.get("version") or 1)
        merged["updated_at"] = self._now()
        merged.setdefault("created_at", self._now())
        merged["metadata"]["stable_fields"] = sorted(STABLE_FIELDS)
        merged["metadata"]["persona_mutable_fields"] = sorted(PERSONA_MUTABLE_FIELDS)
        return merged

    def _normalize_items(self, items: List[Dict[str, Any]], now: str) -> List[Dict[str, Any]]:
        normalized = []
        for item in items:
            if isinstance(item, str):
                item = {"topic": item}
            if not isinstance(item, dict):
                continue
            clean_item = dict(item)
            clean_item.setdefault("updated_by", "persona")
            clean_item.setdefault("updated_at", now)
            normalized.append(clean_item)
        return normalized

    def _normalize_active_interests(self, items: List[Dict[str, Any]], now: str) -> List[Dict[str, Any]]:
        normalized = self._normalize_items(items, now)
        return self._merge_similar_items(
            normalized,
            key_candidates=["topic", "question"],
        )[:PURPOSE_ACTIVE_INTERESTS_MAX]

    def _normalize_open_questions(
        self,
        items: List[Dict[str, Any]],
        now: str,
        existing_items: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        normalized = self._normalize_items(items, now)
        self._normalize_question_priorities(normalized, existing_items or [])
        deduped = self._merge_similar_items(
            normalized,
            key_candidates=["question", "topic"],
            fill_fields=["context"],
            priority_default=PURPOSE_QUESTION_DEFAULT_PRIORITY,
        )
        return self._limit_open_questions(deduped)

    def _merge_similar_items(
        self,
        items: List[Dict[str, Any]],
        key_candidates: List[str],
        fill_fields: Optional[List[str]] = None,
        priority_default: float = 0.0,
    ) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        normalized_keys: List[str] = []
        fill_fields = fill_fields or []

        for item in items:
            if not isinstance(item, dict):
                continue
            key = self._item_text(item, key_candidates)
            norm_key = self._normalize_text_for_dedup(key)
            if not norm_key:
                continue

            duplicate_index = self._find_similar_index(norm_key, normalized_keys)
            if duplicate_index is None:
                merged.append(dict(item))
                normalized_keys.append(norm_key)
                continue

            existing = merged[duplicate_index]
            existing["priority"] = max(
                self._priority_value(existing, default=priority_default),
                self._priority_value(item, default=priority_default),
            )
            for field in fill_fields:
                if not existing.get(field) and item.get(field):
                    existing[field] = item[field]
            if not existing.get("updated_at") and item.get("updated_at"):
                existing["updated_at"] = item["updated_at"]

        return sorted(
            merged,
            key=lambda item: (
                self._priority_value(item, default=priority_default),
                self._timestamp_sort_key(item),
            ),
            reverse=True,
        )

    def _normalize_question_priorities(
        self,
        items: List[Dict[str, Any]],
        existing_items: List[Dict[str, Any]],
    ) -> None:
        existing_keys: List[str] = []
        existing_priorities: List[float] = []
        for existing in existing_items:
            if not isinstance(existing, dict):
                continue
            key = self._normalize_text_for_dedup(self._item_text(existing, ["question", "topic"]))
            if not key:
                continue
            priority = self._coerce_priority(existing.get("priority"), default=None)
            if priority is None:
                continue
            existing_keys.append(key)
            existing_priorities.append(priority)

        for item in items:
            priority = self._coerce_priority(item.get("priority"), default=None)
            if priority is None:
                priority = self._inherited_question_priority(item, existing_keys, existing_priorities)
            if priority is None:
                priority = PURPOSE_QUESTION_DEFAULT_PRIORITY
            item["priority"] = priority

    def _inherited_question_priority(
        self,
        item: Dict[str, Any],
        existing_keys: List[str],
        existing_priorities: List[float],
    ) -> Optional[float]:
        key = self._normalize_text_for_dedup(self._item_text(item, ["question", "topic"]))
        if not key:
            return None
        duplicate_index = self._find_similar_index(key, existing_keys)
        if duplicate_index is None:
            return None
        return existing_priorities[duplicate_index]

    def _item_text(self, item: Dict[str, Any], key_candidates: List[str]) -> str:
        for candidate in key_candidates:
            text = str(item.get(candidate, "")).strip()
            if text:
                return text
        return ""

    def _normalize_text_for_dedup(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
        normalized = re.sub(r"\s+", "", normalized)
        return "".join(
            char
            for char in normalized
            if not unicodedata.category(char).startswith(("P", "S"))
        )

    def _find_similar_index(self, norm_key: str, normalized_keys: List[str]) -> Optional[int]:
        for index, existing_key in enumerate(normalized_keys):
            if norm_key == existing_key:
                return index
            if normalized_text_similarity(norm_key, existing_key) >= PURPOSE_DEDUP_SIMILARITY_THRESHOLD:
                return index
        return None

    def _coerce_priority(self, value: Any, default: Optional[float] = 0.0) -> Optional[float]:
        try:
            if value is None or value == "":
                return default
            return min(1.0, max(0.0, float(value)))
        except Exception:
            return default

    def _priority_value(self, item: Dict[str, Any], default: float = 0.0) -> float:
        value = self._coerce_priority(item.get("priority"), default=default)
        return default if value is None else value

    def _timestamp_sort_key(self, item: Dict[str, Any]) -> str:
        return str(
            item.get("updated_at")
            or item.get("created_at")
            or item.get("asked_at")
            or ""
        )

    def _record_open_question_trace(self, item: Any, trace_kind: str, reason: str = "") -> bool:
        if isinstance(item, dict):
            question_text = self._item_text(item, ["question", "topic"])
            priority = item.get("priority")
            created_at = item.get("created_at") or item.get("asked_at") or ""
            updated_at = item.get("updated_at", "")
        else:
            question_text = str(item or "")
            priority = None
            created_at = ""
            updated_at = ""

        if not is_substantive_reflection(question_text):
            return False

        metadata = {
            "kind": trace_kind,
            "question": question_text,
            "priority": priority,
            "created_at": created_at,
            "updated_at": updated_at,
            "reason": reason,
        }
        insight = f"Purpose Profileの問いが{trace_kind}で保留解除された: {question_text}"
        return save_resolution_insight(
            self.room_name,
            trigger_topic=question_text,
            insight=insight,
            source_type=f"purpose_profile_question_{trace_kind}",
            metadata=metadata,
        )

    def _limit_open_questions(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sorted_items = sorted(
            items,
            key=lambda item: (
                self._priority_value(item, default=PURPOSE_QUESTION_DEFAULT_PRIORITY),
                self._timestamp_sort_key(item),
            ),
            reverse=True,
        )
        if len(sorted_items) <= PURPOSE_OPEN_QUESTIONS_MAX:
            return sorted_items
        trimmed_count = len(sorted_items) - PURPOSE_OPEN_QUESTIONS_MAX
        print(
            f"[Purpose Profile] open_questions上限{PURPOSE_OPEN_QUESTIONS_MAX}件のため"
            f"{trimmed_count}件を切り捨てました。"
        )
        for item in sorted_items[PURPOSE_OPEN_QUESTIONS_MAX:]:
            self._record_open_question_trace(item, trace_kind="pruned", reason="open_questions_limit")
        return sorted_items[:PURPOSE_OPEN_QUESTIONS_MAX]

    def _collect_research_thread_interests(self) -> List[Dict[str, Any]]:
        try:
            from research_thread_manager import ResearchThreadManager
            threads = ResearchThreadManager(self.room_name).list_threads(status="active")
        except Exception:
            return []
        items = []
        for thread in threads[:6]:
            title = thread.get("title") or thread.get("thread_id")
            if not title:
                continue
            items.append({
                "topic": title,
                "reason": "active Research Thread",
                "priority": float(thread.get("priority", 0.5) or 0.5),
                "source": "research_thread",
                "thread_id": thread.get("thread_id", ""),
                "updated_by": "sleep_consolidation",
                "updated_at": self._now(),
            })
        return items

    def _collect_goal_interests(self) -> List[Dict[str, Any]]:
        try:
            from goal_manager import GoalManager
            goals = GoalManager(self.room_name).get_active_goals()
        except Exception:
            return []
        items = []
        for goal in goals[:6]:
            text = goal.get("goal", "")
            if not text:
                continue
            priority = goal.get("priority", 3)
            try:
                score = max(0.2, 1.0 - float(priority) * 0.2)
            except Exception:
                score = 0.5
            items.append({
                "topic": text,
                "reason": "active goal",
                "priority": score,
                "source": "goal",
                "goal_id": goal.get("id", ""),
                "updated_by": "sleep_consolidation",
                "updated_at": self._now(),
            })
        return items

    def _question_items_from_research_threads(self) -> List[Dict[str, Any]]:
        try:
            from research_thread_manager import ResearchThreadManager
            threads = ResearchThreadManager(self.room_name).list_threads(status="active")
        except Exception:
            return []
        items = []
        for thread in threads[:6]:
            for question in thread.get("open_questions", []) or []:
                text = str(question).strip()
                if not text:
                    continue
                items.append({
                    "question": text,
                    "source": "research_thread",
                    "thread_id": thread.get("thread_id", ""),
                    "priority": float(thread.get("priority", 0.5) or 0.5),
                    "updated_by": "sleep_consolidation",
                    "updated_at": self._now(),
                })
        return items

    def _question_items_from_motivation(self, questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        items = []
        for question in questions:
            if not isinstance(question, dict) or question.get("resolved_at"):
                continue
            text = question.get("question") or question.get("topic")
            if not text:
                continue
            items.append({
                "question": str(text).strip(),
                "context": question.get("context", ""),
                "source": "motivation",
                "priority": float(question.get("priority", 0.5) or 0.5),
                "updated_by": "sleep_consolidation",
                "updated_at": self._now(),
            })
        return items

    def _question_items_from_dream(self, questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        items = []
        for question in questions:
            if not isinstance(question, dict):
                continue
            text = question.get("question") or question.get("topic")
            if not text:
                continue
            items.append({
                "question": str(text).strip(),
                "context": question.get("context", ""),
                "source": "dreaming",
                "priority": float(question.get("priority", 0.5) or 0.5),
                "updated_by": "sleep_consolidation",
                "updated_at": self._now(),
            })
        return items

    def _dedupe_items(self, items: List[Dict[str, Any]], key_candidates: List[str]) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if isinstance(item, str):
                item = {key_candidates[0]: item}
            if not isinstance(item, dict):
                continue
            key = ""
            for candidate in key_candidates:
                key = str(item.get(candidate, "")).strip()
                if key:
                    break
            if not key:
                continue
            norm_key = key.casefold()
            # 完全一致チェック
            existing = merged.get(norm_key)
            if existing:
                if float(item.get("priority", 0.0) or 0.0) >= float(existing.get("priority", 0.0) or 0.0):
                    merged[norm_key] = dict(item)
                continue
            # トークン重複率チェック（Jaccard類似度 >= 0.6 で重複とみなす）
            item_tokens = set(norm_key.split())
            if len(item_tokens) >= 2:
                is_similar = False
                for existing_key, existing_item in merged.items():
                    existing_tokens = set(existing_key.split())
                    if not existing_tokens:
                        continue
                    intersection = item_tokens & existing_tokens
                    union = item_tokens | existing_tokens
                    if union and len(intersection) / len(union) >= 0.6:
                        # 類似項目 → priority が高い方を残す
                        if float(item.get("priority", 0.0) or 0.0) > float(existing_item.get("priority", 0.0) or 0.0):
                            del merged[existing_key]
                            merged[norm_key] = dict(item)
                        is_similar = True
                        break
                if is_similar:
                    continue
            merged[norm_key] = dict(item)
        return sorted(
            merged.values(),
            key=lambda item: float(item.get("priority", 0.0) or 0.0),
            reverse=True,
        )

    def _add_stable_interest_proposals(self, profile: Dict[str, Any], now: str) -> None:
        stable = set(str(item).casefold() for item in profile.get("stable_interests", []))
        pending = {
            str(item.get("proposal", "")).casefold()
            for item in profile.get("proposed_changes", [])
            if isinstance(item, dict) and item.get("status") == "pending"
        }
        proposals = profile.setdefault("proposed_changes", [])
        added = 0
        for interest in profile.get("active_interests", []):
            topic = str(interest.get("topic", "")).strip()
            if not topic:
                continue
            priority = float(interest.get("priority", 0.0) or 0.0)
            key = topic.casefold()
            if priority < 0.85 or key in stable or key in pending:
                continue
            proposals.append({
                "id": f"proposal_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
                "field": "stable_interests",
                "proposal": topic,
                "reason": "睡眠時整理で高優先度の反復関心として検出",
                "proposed_by": "sleep_consolidation",
                "status": "pending",
                "created_at": now,
            })
            added += 1
            if added >= 2:
                break

    def sync_from_thread_deactivation(
        self,
        thread_id: str,
        thread_title: str = "",
        resolved_questions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Research Thread が archived/paused になった時に PP を同期する。

        - thread_id に紐づく active_interests のソースを降格する。
        - resolved_questions に含まれる問いを open_questions から除去する。
        """
        profile = self.ensure_profile()
        now = self._now()
        changed = False

        # active_interests から当該スレッド由来の関心を降格
        if thread_id:
            new_interests = []
            for item in profile.get("active_interests", []):
                if isinstance(item, dict) and item.get("thread_id") == thread_id:
                    # 降格: 優先度を大幅に下げて残す（完全削除ではなく痕跡を保持）
                    item["priority"] = max(0.1, float(item.get("priority", 0.5)) * 0.3)
                    item["deactivated_reason"] = "research_thread_archived"
                    item["updated_at"] = now
                    changed = True
                new_interests.append(item)
            profile["active_interests"] = new_interests

        # open_questions から当該スレッド由来の問いを除去
        resolved_set = set()
        for q in (resolved_questions or []):
            resolved_set.add(str(q).strip().casefold())

        if resolved_set:
            new_questions = []
            for item in profile.get("open_questions", []):
                key = ""
                if isinstance(item, dict):
                    if item.get("thread_id") == thread_id:
                        changed = True
                        continue  # スレッド由来の問いは除去
                    key = str(item.get("question") or item.get("topic") or "").strip().casefold()
                else:
                    key = str(item).strip().casefold()
                if key and key in resolved_set:
                    changed = True
                    continue  # テキスト一致する問いも除去
                new_questions.append(item)
            profile["open_questions"] = new_questions

        if changed:
            profile["updated_at"] = now
            metadata = profile.setdefault("metadata", {})
            metadata["last_thread_sync_at"] = now
            metadata["last_thread_sync_reason"] = f"thread_{thread_id}_deactivated"
            self._save(profile)
        return profile

    def sync_open_questions_from_threads(self) -> Dict[str, Any]:
        """
        アクティブな Research Threads の open_questions を PP の open_questions に反映する。

        睡眠時の consolidate_from_sleep とは独立に、
        Research Thread 更新直後に軽量に呼べるメソッド。
        """
        profile = self.ensure_profile()
        now = self._now()
        new_questions = list(profile.get("open_questions", []))
        new_questions.extend(self._question_items_from_research_threads())
        profile["open_questions"] = self._normalize_open_questions(
            new_questions,
            now,
            existing_items=profile.get("open_questions", []),
        )
        profile["updated_at"] = now
        self._save(profile)
        return profile

    def _save(self, profile: Dict[str, Any]) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        normalized = self._normalize_profile(profile)
        with open(self.profile_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
