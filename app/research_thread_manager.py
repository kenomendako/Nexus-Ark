# research_thread_manager.py
"""
Research Threads manager.

Research Threads turn research notes from a flat chronological artifact into
ongoing topics with explicit next actions, open questions, and related context.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import constants


VALID_THREAD_STATUSES = {"active", "paused", "archived"}
VALID_RELATION_TYPES = {"CONTINUE", "DEEPEN", "NEW", "CONTRADICT", "EVIDENCE"}


class ResearchThreadManager:
    """Manages characters/<room>/memory/research_threads/."""

    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.threads_dir = self.room_dir / "memory" / constants.RESEARCH_THREADS_DIR_NAME
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.threads_dir / constants.RESEARCH_THREADS_INDEX_FILENAME

    def default_index(self) -> Dict[str, Any]:
        now = self._now()
        return {"version": 1, "created_at": now, "updated_at": now, "threads": []}

    def ensure_index(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            index = self.default_index()
            self._save_index(index)
            return index
        return self.load_index()

    def load_index(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            return self.ensure_index()
        with open(self.index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = self.default_index()
        data.setdefault("version", 1)
        data.setdefault("threads", [])
        data.setdefault("created_at", self._now())
        data.setdefault("updated_at", self._now())
        return data

    def save_index_from_ui(self, index: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(index, dict):
            raise ValueError("Research Threads index はJSONオブジェクトである必要があります。")
        normalized = self.default_index()
        normalized.update(index)
        if not isinstance(normalized.get("threads"), list):
            normalized["threads"] = []
        normalized["updated_at"] = self._now()
        self._save_index(normalized)
        for thread in normalized["threads"]:
            if isinstance(thread, dict) and thread.get("thread_id"):
                self._create_thread_file(thread)
        return normalized

    def list_threads(self, status: str = "", boost_by_purpose: bool = False) -> List[Dict[str, Any]]:
        threads = self.ensure_index().get("threads", [])
        if status:
            threads = [t for t in threads if t.get("status") == status]
        if boost_by_purpose:
            threads = self._apply_purpose_boost(threads)
        return sorted(
            threads,
            key=lambda t: (t.get("priority", 0), t.get("last_deepened_at", t.get("updated_at", ""))),
            reverse=True,
        )

    def get_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        thread_id = self._safe_thread_id(thread_id)
        for thread in self.ensure_index().get("threads", []):
            if thread.get("thread_id") == thread_id:
                return thread
        return None

    def create_or_update_thread(
        self,
        thread_id: str,
        title: str = "",
        status: str = "active",
        priority: Optional[float] = 0.5,
        working_memory_slot: str = "",
        related_entities: Optional[List[str]] = None,
        open_questions: Optional[List[str]] = None,
        next_action: str = "",
    ) -> Dict[str, Any]:
        thread_id = self._safe_thread_id(thread_id or title)
        if not thread_id:
            raise ValueError("thread_id または title が必要です。")
        status = status if status in VALID_THREAD_STATUSES else "active"
        now = self._now()

        index = self.ensure_index()
        threads = index.setdefault("threads", [])
        existing = None
        for thread in threads:
            if thread.get("thread_id") == thread_id:
                existing = thread
                break

        if existing is None:
            existing = {
                "thread_id": thread_id,
                "title": title or thread_id.replace("_", " "),
                "status": status,
                "priority": float(priority if priority is not None else 0.5),
                "working_memory_slot": working_memory_slot,
                "related_entities": related_entities or [],
                "open_questions": open_questions or [],
                "next_action": next_action,
                "created_at": now,
                "updated_at": now,
                "last_deepened_at": "",
                "last_relation_type": "",
                "target_headings": [],
            }
            threads.append(existing)
            self._create_thread_file(existing)
        else:
            old_status = existing.get("status", "active")
            if title:
                existing["title"] = title
            existing["status"] = status
            if priority is not None:
                existing["priority"] = float(priority)
            if working_memory_slot:
                existing["working_memory_slot"] = working_memory_slot
            if related_entities is not None:
                existing["related_entities"] = self._merge_unique(existing.get("related_entities", []), related_entities)
            if open_questions is not None:
                existing["open_questions"] = self._merge_unique(existing.get("open_questions", []), open_questions)
            if next_action:
                existing["next_action"] = next_action
            existing["updated_at"] = now
            self._create_thread_file(existing)

            # ステータスが archived/paused に変わったら PP の同期を試みる
            if status in {"archived", "paused"} and old_status == "active":
                self._sync_purpose_profile_on_deactivate(existing)

        index["updated_at"] = now
        self._save_index(index)
        return existing

    def read_thread(self, thread_id: str) -> str:
        thread_id = self._safe_thread_id(thread_id)
        path = self._thread_path(thread_id)
        if not path.exists():
            raise FileNotFoundError(f"Research Threadが見つかりません: {thread_id}")
        return path.read_text(encoding="utf-8")

    def write_thread(self, thread_id: str, content: str) -> None:
        thread_id = self._safe_thread_id(thread_id)
        if not thread_id:
            raise ValueError("thread_id が必要です。")
        path = self._thread_path(thread_id)
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or ""), encoding="utf-8")
        index = self.ensure_index()
        for thread in index.get("threads", []):
            if thread.get("thread_id") == thread_id:
                thread["updated_at"] = self._now()
                break
        index["updated_at"] = self._now()
        self._save_index(index)

    def to_pretty_index_json(self) -> str:
        return json.dumps(self.ensure_index(), ensure_ascii=False, indent=2)

    def append_thread_note(
        self,
        thread_id: str,
        title: str = "",
        relation_type: str = "DEEPEN",
        content: str = "",
        next_action: str = "",
        target_heading: str = "",
        evidence_of_prior_read: str = "",
    ) -> Dict[str, Any]:
        relation_type = relation_type.upper()
        if relation_type not in VALID_RELATION_TYPES:
            relation_type = "DEEPEN"
        thread = self.create_or_update_thread(thread_id=thread_id, title=title, next_action=next_action)
        now = self._now()

        path = self._thread_path(thread["thread_id"])
        if not path.exists():
            self._create_thread_file(thread)

        block = [
            "",
            f"## {relation_type}: {now}",
        ]
        if target_heading:
            block.append(f"- target_heading: {target_heading}")
        if evidence_of_prior_read:
            block.append(f"- evidence_of_prior_read: {evidence_of_prior_read}")
        if content:
            block.extend(["", content.strip()])
        if next_action:
            block.extend(["", "### Next Action", next_action.strip()])

        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(block).rstrip() + "\n")

        index = self.ensure_index()
        for item in index.get("threads", []):
            if item.get("thread_id") == thread["thread_id"]:
                item["updated_at"] = now
                item["last_relation_type"] = relation_type
                if relation_type in {"DEEPEN", "CONTINUE", "CONTRADICT", "EVIDENCE"}:
                    item["last_deepened_at"] = now
                if target_heading:
                    item["target_headings"] = self._merge_unique(item.get("target_headings", []), [target_heading])
                if next_action:
                    item["next_action"] = next_action
                break
        index["updated_at"] = now
        self._save_index(index)
        return self.get_thread(thread["thread_id"]) or thread

    def find_similar_threads(self, query: str, limit: int = 5, boost_by_purpose: bool = False) -> List[Dict[str, Any]]:
        words = self._keywords(query)
        if not words:
            return []

        purpose_terms = self._get_purpose_interest_terms() if boost_by_purpose else []

        results = []
        for thread in self.ensure_index().get("threads", []):
            haystack_parts = [
                thread.get("thread_id", ""),
                thread.get("title", ""),
                thread.get("next_action", ""),
                " ".join(thread.get("related_entities", [])),
                " ".join(thread.get("open_questions", [])),
                " ".join(thread.get("target_headings", [])),
            ]
            path = self._thread_path(thread.get("thread_id", ""))
            if path.exists():
                try:
                    haystack_parts.append(path.read_text(encoding="utf-8")[:5000])
                except Exception:
                    pass
            haystack = " ".join(haystack_parts).lower()
            score = sum(1 for word in words if word in haystack)
            if score:
                # PP関心とのテキスト一致でスコアを加算
                if purpose_terms:
                    score += self._purpose_alignment_score(thread, purpose_terms)
                result = dict(thread)
                result["match_score"] = score
                results.append(result)

        results.sort(key=lambda t: (t.get("match_score", 0), t.get("priority", 0)), reverse=True)
        return results[:limit]

    def get_summary_for_prompt(self, limit: int = 5, boost_by_purpose: bool = False) -> str:
        threads = self.list_threads(status="active", boost_by_purpose=boost_by_purpose)[:limit]
        if not threads:
            return ""
        lines = ["\n### Research Threads（継続研究スレッド）"]
        for thread in threads:
            title = thread.get("title") or thread.get("thread_id")
            tid = thread.get("thread_id", "")
            priority = thread.get("priority", 0.5)
            next_action = thread.get("next_action", "")
            last = thread.get("last_deepened_at") or thread.get("updated_at", "")
            line = f"- {tid}: {title} (priority: {priority}, last: {last or '未更新'})"
            if next_action:
                line += f"\n  next_action: {next_action}"
            lines.append(line)
        return "\n".join(lines) + "\n"

    def _create_thread_file(self, thread: Dict[str, Any]) -> None:
        path = self._thread_path(thread["thread_id"])
        if path.exists():
            return
        content = (
            f"# {thread.get('title') or thread['thread_id']}\n\n"
            "## Thesis\n\n"
            "## Evidence\n\n"
            "## Open Questions\n"
        )
        for q in thread.get("open_questions", []):
            content += f"- {q}\n"
        content += "\n## Contradictions\n\n## Next Actions\n"
        if thread.get("next_action"):
            content += f"- {thread['next_action']}\n"
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _thread_path(self, thread_id: str) -> Path:
        return self.threads_dir / f"{self._safe_thread_id(thread_id)}.md"

    def _save_index(self, index: Dict[str, Any]) -> None:
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    def _safe_thread_id(self, value: str) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"\s+", "_", text)
        text = re.sub(r"[^a-z0-9_\-ぁ-んァ-ン一-龥]", "", text)
        return text[:80]

    def _keywords(self, query: str) -> List[str]:
        text = str(query or "").lower()
        words = re.findall(r"[a-z0-9_\-ぁ-んァ-ン一-龥]{2,}", text)
        return list(dict.fromkeys(words))

    def _merge_unique(self, current: List[str], incoming: List[str]) -> List[str]:
        values = []
        for item in list(current or []) + list(incoming or []):
            text = str(item).strip()
            if text and text not in values:
                values.append(text)
        return values

    def _get_purpose_interest_terms(self) -> List[str]:
        """Purpose Profileの関心キーワードを収集する。"""
        try:
            from purpose_profile_manager import PurposeProfileManager
            profile = PurposeProfileManager(self.room_name).load_profile()
        except Exception:
            return []
        terms = []
        for item in profile.get("stable_interests", []):
            terms.append(str(item).strip().lower())
        for item in profile.get("active_interests", []):
            if isinstance(item, dict):
                terms.append(str(item.get("topic", "")).strip().lower())
            else:
                terms.append(str(item).strip().lower())
        return [t for t in terms if len(t) >= 2]

    def _purpose_alignment_score(self, thread: Dict[str, Any], purpose_terms: List[str]) -> int:
        """スレッドとPP関心の一致度を返す。"""
        haystack = " ".join([
            thread.get("thread_id", ""),
            thread.get("title", ""),
            " ".join(thread.get("related_entities", [])),
        ]).lower()
        return sum(1 for term in purpose_terms if term in haystack or haystack in term)

    def _apply_purpose_boost(self, threads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """PP関心との一致度に基づき、スレッドの priority を一時的にブーストする。"""
        purpose_terms = self._get_purpose_interest_terms()
        if not purpose_terms:
            return threads
        boosted = []
        for thread in threads:
            t = dict(thread)
            alignment = self._purpose_alignment_score(t, purpose_terms)
            if alignment > 0:
                # 一致ごとに +0.05、最大 +0.15
                boost = min(0.15, alignment * 0.05)
                t["priority"] = min(1.0, float(t.get("priority", 0.5)) + boost)
                t["_purpose_boosted"] = True
            boosted.append(t)
        return boosted

    def _sync_purpose_profile_on_deactivate(self, thread: Dict[str, Any]) -> None:
        """スレッドが非アクティブになった時、PPの関連項目を整理する。"""
        try:
            from purpose_profile_manager import PurposeProfileManager
            pp = PurposeProfileManager(self.room_name)
            pp.sync_from_thread_deactivation(
                thread_id=thread.get("thread_id", ""),
                thread_title=thread.get("title", ""),
                resolved_questions=thread.get("open_questions", []),
            )
        except Exception as e:
            print(f"  - [ResearchThread] PP同期エラー（無視）: {e}")

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
