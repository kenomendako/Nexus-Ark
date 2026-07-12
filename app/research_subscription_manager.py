# research_subscription_manager.py
"""
テーマ駆動の継続リサーチ（購読）管理モジュール。

ユーザー（またはペルソナ）が「欲しい情報のテーマ」を購読登録すると、各テーマは
継続研究スレッド（research_threads）にひも付き、定期的に委任ディープリサーチで
新情報を収集・重複回避しつつ追記していく（実際のリサーチ実行は Phase 2 のスケジューラ）。

本モジュールは購読データの CRUD と「実行すべき購読の抽出（due 判定）」を担う。
保存先: characters/<room>/memory/research_subscriptions.json
"""

import os
import json
import uuid
import datetime
from typing import Any, Dict, List, Optional

import constants
from research_thread_manager import ResearchThreadManager
from utils import normalized_text_similarity


SUBSCRIPTION_DEDUP_SIMILARITY_THRESHOLD = 0.85
MAX_RESEARCH_SUBSCRIPTIONS = 10


def _now_iso() -> str:
    return datetime.datetime.now().isoformat()


class ResearchSubscriptionManager:
    """テーマ購読の管理クラス。"""

    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = os.path.join(constants.ROOMS_DIR, room_name)
        self.memory_dir = os.path.join(self.room_dir, "memory")
        self.path = os.path.join(self.memory_dir, constants.RESEARCH_SUBSCRIPTIONS_FILENAME)
        os.makedirs(self.memory_dir, exist_ok=True)

    # --- 読み書き ---

    def _default_data(self) -> Dict[str, Any]:
        return {"version": 1, "subscriptions": []}

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("version", 1)
                    data.setdefault("subscriptions", [])
                    return data
            except (json.JSONDecodeError, IOError):
                pass
        return self._default_data()

    def _save(self, data: Dict[str, Any]) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # --- CRUD ---

    def list_subscriptions(self) -> List[Dict[str, Any]]:
        return self._load().get("subscriptions", [])

    def get_subscription(self, sub_id: str) -> Optional[Dict[str, Any]]:
        for sub in self.list_subscriptions():
            full = sub.get("id", "")
            if full == sub_id or full.startswith(sub_id):
                return sub
        return None

    def get_by_topic(self, topic: str) -> Optional[Dict[str, Any]]:
        topic_norm = (topic or "").strip()
        for sub in self.list_subscriptions():
            if sub.get("topic", "").strip() == topic_norm:
                return sub
        return None

    def get_similar_topic(self, topic: str) -> Optional[Dict[str, Any]]:
        topic_norm = (topic or "").strip()
        if not topic_norm:
            return None
        for sub in self.list_subscriptions():
            similarity = normalized_text_similarity(topic_norm, sub.get("topic", ""))
            if similarity >= SUBSCRIPTION_DEDUP_SIMILARITY_THRESHOLD:
                result = dict(sub)
                result["_dedup_similarity"] = similarity
                return result
        return None

    def add_subscription(
        self,
        topic: str,
        focus: str = "",
        frequency: str = constants.RESEARCH_SUBSCRIPTION_DEFAULT_FREQUENCY,
        depth: str = constants.RESEARCH_SUBSCRIPTION_DEFAULT_DEPTH,
        seed_urls: Optional[List[str]] = None,
        run_time: str = constants.RESEARCH_SUBSCRIPTION_DEFAULT_RUN_TIME,
        created_by: str = "user",
    ) -> Dict[str, Any]:
        """テーマを購読登録する。同名テーマが既にあればそれを返す。

        ひも付く継続研究スレッドを（無ければ）作成し、thread_id を保存する。
        """
        topic = (topic or "").strip()
        if not topic:
            raise ValueError("テーマ（topic）が空です。")

        data = self._load()
        existing = self.get_similar_topic(topic)
        if existing:
            existing["_dedup_skipped"] = True
            return existing

        if len(data.get("subscriptions", [])) >= MAX_RESEARCH_SUBSCRIPTIONS:
            return {
                "_limit_exceeded": True,
                "topic": topic,
                "limit": MAX_RESEARCH_SUBSCRIPTIONS,
                "enabled": False,
            }

        frequency = frequency if frequency in constants.RESEARCH_SUBSCRIPTION_FREQUENCY_OPTIONS else constants.RESEARCH_SUBSCRIPTION_DEFAULT_FREQUENCY
        depth = depth if depth in constants.RESEARCH_SUBSCRIPTION_DEPTH_OPTIONS else constants.RESEARCH_SUBSCRIPTION_DEFAULT_DEPTH

        # ひも付く研究スレッドを用意（テーマ名をタイトルに）
        thread_id = ""
        try:
            thread = ResearchThreadManager(self.room_name).create_or_update_thread(thread_id="", title=topic)
            thread_id = thread.get("thread_id", "")
        except Exception:
            thread_id = ""

        sub = {
            "id": f"sub_{uuid.uuid4().hex[:12]}",
            "topic": topic,
            "focus": (focus or "").strip(),
            "frequency": frequency,
            "depth": depth,
            "seed_urls": list(seed_urls or []),
            "run_time": run_time or constants.RESEARCH_SUBSCRIPTION_DEFAULT_RUN_TIME,
            "enabled": True,
            "thread_id": thread_id,
            "created_by": created_by,
            "created_at": _now_iso(),
            "last_run": None,
        }
        data["subscriptions"].append(sub)
        self._save(data)
        return sub

    def update_subscription(self, sub_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        allowed = {"topic", "focus", "frequency", "depth", "seed_urls", "run_time", "enabled", "thread_id", "last_run"}
        data = self._load()
        for sub in data["subscriptions"]:
            full = sub.get("id", "")
            if full == sub_id or full.startswith(sub_id):
                for key, value in kwargs.items():
                    if key in allowed:
                        sub[key] = value
                self._save(data)
                return sub
        return None

    def remove_subscription(self, sub_id: str) -> bool:
        """購読を解除する（ひも付く研究スレッドは残す＝蓄積を消さない）。"""
        data = self._load()
        before = len(data["subscriptions"])
        target = self.get_subscription(sub_id)
        if not target:
            return False
        full = target["id"]
        data["subscriptions"] = [s for s in data["subscriptions"] if s.get("id") != full]
        if len(data["subscriptions"]) < before:
            self._save(data)
            return True
        return False

    def mark_run(self, sub_id: str, when: Optional[datetime.datetime] = None) -> Optional[Dict[str, Any]]:
        when = when or datetime.datetime.now()
        return self.update_subscription(sub_id, last_run=when.isoformat())

    # --- スケジューラ用（Phase 2 で使用） ---

    def get_due_subscriptions(self, now: Optional[datetime.datetime] = None) -> List[Dict[str, Any]]:
        """自動リサーチを実行すべき購読を返す（古いものから順）。"""
        now = now or datetime.datetime.now()
        due: List[Dict[str, Any]] = []
        for sub in self.list_subscriptions():
            if not sub.get("enabled", True):
                continue
            freq = sub.get("frequency", constants.RESEARCH_SUBSCRIPTION_DEFAULT_FREQUENCY)
            if freq == "manual":
                continue
            if self._is_due(sub, freq, now):
                due.append(sub)
        # 最終実行が古い順（None=未実行を最優先）
        due.sort(key=lambda s: s.get("last_run") or "")
        return due

    def _is_due(self, sub: Dict[str, Any], freq: str, now: datetime.datetime) -> bool:
        run_time = sub.get("run_time") or constants.RESEARCH_SUBSCRIPTION_DEFAULT_RUN_TIME
        try:
            rh, rm = map(int, run_time.split(":"))
        except (ValueError, AttributeError):
            rh, rm = 7, 0
        scheduled_today = now.replace(hour=rh, minute=rm, second=0, microsecond=0)
        if now < scheduled_today:
            return False  # 本日の実行時刻前

        last_run = sub.get("last_run")
        if not last_run:
            return True  # 一度も実行していない（実行時刻は過ぎている）
        try:
            last_dt = datetime.datetime.fromisoformat(last_run)
        except (ValueError, TypeError):
            return True

        if freq == "daily":
            # 本日の実行時刻より前に最後の実行があれば due
            return last_dt < scheduled_today
        if freq == "weekly":
            return (now - last_dt).total_seconds() >= 7 * 24 * 3600
        return False

    def count_runs_on(self, date: Optional[datetime.date] = None) -> int:
        """指定日に実行済みの購読数（全テーマ合計の1日上限チェック用）。"""
        date = date or datetime.date.today()
        count = 0
        for sub in self.list_subscriptions():
            last_run = sub.get("last_run")
            if not last_run:
                continue
            try:
                if datetime.datetime.fromisoformat(last_run).date() == date:
                    count += 1
            except (ValueError, TypeError):
                continue
        return count


def get_research_subscription_manager(room_name: str) -> ResearchSubscriptionManager:
    return ResearchSubscriptionManager(room_name)
