# goal_manager.py
"""
Goal Memory Manager for Nexus Ark
Manages persona goals (short-term and long-term) for autonomous behavior and self-reflection.
"""

import json
import os
import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any
import uuid
from copy import deepcopy

import constants
from filelock import Timeout
from episodic_memory_manager import EpisodicMemoryManager
from resolution_memory import is_substantive_reflection, save_resolution_insight
from utils import normalized_text_similarity


GOAL_DEDUP_SIMILARITY_THRESHOLD = 0.85


class GoalManager:
    """
    ペルソナの目標（短期・長期）を管理するクラス。
    目標はルームごとに goals.json として保存される。
    """
    
    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.goals_file = self.room_dir / "goals.json"
        self._ensure_goals_file()
    
    def _ensure_goals_file(self):
        """goals.json が存在しない場合、または空の場合は初期化"""
        if not self.goals_file.exists() or self.goals_file.stat().st_size == 0:
            print(f"--- [GoalManager] {self.goals_file.name} を初期化します ---")
            self._save_goals(self._get_empty_goals())
    
    def _get_empty_goals(self) -> Dict:
        """空の目標構造を返す"""
        return {
            "short_term": [],
            "long_term": [],
            "completed": [],
            "abandoned": [],
            "meta": {
                "last_updated": None,
                "last_reflection_level": 0,
                "last_level2_date": None,
                "last_level3_date": None
            }
        }
    
    def _load_goals(self) -> Dict:
        """目標データを読み込む（ロック付き、破損時の自動復旧機能付き）"""
        from file_lock_utils import safe_json_read
        
        try:
            data = safe_json_read(str(self.goals_file), default=None)
            if data is None:
                # ファイルが存在しない場合は初期化
                return self._get_empty_goals()
            if not isinstance(data, dict):
                # 形式が不正な場合も初期化
                return self._get_empty_goals()
            return data
            
        except Exception as e:
            # --- [自動復旧ロジック] ---
            # 何らかの理由で読み込みに失敗（JSON破損など）した場合
            print(f"⚠️ [GoalManager] {self.goals_file.name} の読み込みに失敗しました: {e}")
            import utils
            default_data = self._get_empty_goals()
            utils.backup_and_repair_json(self.goals_file, default_data)
            return default_data
    
    def _save_goals(self, goals: Dict):
        """目標データを保存する（ロック付き）"""
        from file_lock_utils import safe_json_write
        
        goals["meta"]["last_updated"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        safe_json_write(str(self.goals_file), goals)

    def _update_goals(self, mutator) -> Dict:
        """目標データをロック内で読み込み、更新して保存する。"""
        from file_lock_utils import safe_json_update

        updated_goals = self._get_empty_goals()

        def update(data):
            nonlocal updated_goals
            goals = data if isinstance(data, dict) else self._get_empty_goals()
            for key, value in self._get_empty_goals().items():
                goals.setdefault(key, value)
            if not isinstance(goals.get("meta"), dict):
                goals["meta"] = self._get_empty_goals()["meta"]
            result = mutator(goals)
            if isinstance(result, dict):
                goals = result
            goals["meta"]["last_updated"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            updated_goals = goals
            return goals

        safe_json_update(str(self.goals_file), update, default=self._get_empty_goals())
        return updated_goals
    
    # ==========================================
    # CRUD Operations
    # ==========================================
    
    def add_goal(self, goal_text: str, goal_type: str = "short_term", priority: int = 1, related_values: List[str] = None) -> str:
        """
        新しい目標を追加する。
        
        Args:
            goal_text: 目標の説明
            goal_type: "short_term" または "long_term"
            priority: 優先度（1が最高）
            related_values: 関連する価値観（長期目標用）
        
        Returns:
            生成された目標ID
        """
        goal_text = str(goal_text or "").strip()
        if not goal_text:
            raise ValueError("goal_text is required")
        if goal_type not in {"short_term", "long_term"}:
            goal_type = "short_term"
        goal_id = ""

        def mutate(goals: Dict) -> Dict:
            nonlocal goal_id
            goals.setdefault(goal_type, [])
            for existing_goal in goals.get(goal_type, []):
                if existing_goal.get("status", "active") not in {"active", "paused"}:
                    continue
                similarity = normalized_text_similarity(goal_text, existing_goal.get("goal", ""))
                if similarity >= GOAL_DEDUP_SIMILARITY_THRESHOLD:
                    # priority は 1 が最高（昇順ソート）。重複追加で優先度を下げないため min を取る。
                    existing_goal["priority"] = min(existing_goal.get("priority", priority), priority)
                    existing_goal["dedup_merged_at"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    existing_goal["dedup_similarity"] = similarity
                    if goal_type == "long_term" and related_values:
                        current_values = existing_goal.get("related_values", [])
                        for value in related_values:
                            if value not in current_values:
                                current_values.append(value)
                        existing_goal["related_values"] = current_values
                    goals[goal_type].sort(key=lambda x: x.get("priority", 999))
                    goal_id = existing_goal["id"]
                    return goals

            goal_id = f"{goal_type[:2]}_{uuid.uuid4().hex[:6]}"
            new_goal = {
                "id": goal_id,
                "goal": goal_text,
                "created_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "status": "active",
                "progress_notes": [],
                "priority": priority
            }
            if goal_type == "long_term" and related_values:
                new_goal["related_values"] = related_values
            goals[goal_type].append(new_goal)
            goals[goal_type].sort(key=lambda x: x.get("priority", 999))
            return goals

        self._update_goals(mutate)
        return goal_id
    
    def get_active_goals(self, goal_type: str = None) -> List[Dict]:
        """
        アクティブな目標を取得する。
        
        Args:
            goal_type: "short_term", "long_term", または None（両方）
        
        Returns:
            目標のリスト
        """
        goals = self._load_goals()
        
        if goal_type:
            return [g for g in goals.get(goal_type, []) if g.get("status") == "active"]
        
        short_term = [g for g in goals.get("short_term", []) if g.get("status") == "active"]
        long_term = [g for g in goals.get("long_term", []) if g.get("status") == "active"]
        return short_term + long_term
    
    def get_retained_goals(self, goal_type: str = None) -> List[Dict]:
        """保留を含む未終了の目標。保留しても本人が一覧から見返せる。"""
        data = self._load_goals()
        kinds = [goal_type] if goal_type else ["short_term", "long_term"]
        return [g for kind in kinds for g in data.get(kind, [])
                if g.get("status") in {"active", "paused"}]

    @staticmethod
    def _assert_expected_goal(data, goal_id, expected_goal):
        if expected_goal is None:
            return
        current = next((g for kind in ("short_term", "long_term", "completed", "abandoned")
                        for g in data.get(kind, []) if g.get("id") == goal_id), None)
        if current != expected_goal:
            raise ValueError("目標が別の処理で更新されました。再読して判断してください。")

    def review_goal(self, goal_id: str, decision: str, reason: str,
                    review_interval_days: int = 30, *, expected_goal=None) -> Dict:
        """継続・保留・再開の明示選択だけを適用する。期限では状態を変えない。"""
        if decision not in {"continue", "pause", "resume"}:
            raise ValueError("decisionはcontinue/pause/resumeで指定してください。")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("今そうしたい理由をreasonに記録してください。")
        if type(review_interval_days) is not int or not 1 <= review_interval_days <= 365:
            raise ValueError("再確認間隔は1〜365日で指定してください。")
        result = {}
        now = datetime.datetime.now()
        def mutate(data):
            self._assert_expected_goal(data, goal_id, expected_goal)
            for kind in ("short_term", "long_term"):
                for goal in data.get(kind, []):
                    if goal.get("id") != goal_id:
                        continue
                    if goal.get("status") not in {"active", "paused"}:
                        raise ValueError("終了した目標は再検討できません。")
                    if decision == "continue" and goal.get("status") == "paused":
                        raise ValueError("保留目標を再開する場合はresumeを指定してください。")
                    goal["status"] = "paused" if decision == "pause" else "active"
                    goal["last_reviewed_at"] = now.strftime('%Y-%m-%d %H:%M:%S')
                    goal["last_review_decision"] = decision
                    goal.pop("review_presented_at", None)
                    goal["review_reason"] = reason.strip()
                    goal["review_after"] = (now + datetime.timedelta(days=review_interval_days)).strftime('%Y-%m-%d %H:%M:%S')
                    result.update(deepcopy(goal))
                    return data
            raise ValueError("未終了の目標IDが見つかりません。")
        self._update_goals(mutate)
        return result

    @staticmethod
    def _parse_goal_time(value):
        try:
            return datetime.datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            return None

    @classmethod
    def _last_progress_time(cls, goal):
        dates = [cls._parse_goal_time(goal.get("created_at"))]
        for note in goal.get("progress_notes", []):
            if isinstance(note, str) and note.startswith("[") and "]" in note:
                stamp, body = note[1:].split("]", 1)
                if body.strip():
                    dates.append(cls._parse_goal_time(stamp))
        return max((d for d in dates if d is not None), default=None)

    def get_review_candidates(self, limit: int = 3, *, now=None, goal_data=None) -> List[Dict]:
        """状態を変更せず、古さ・件数・保留期日から少数の再検討候補を返す。"""
        now = now or datetime.datetime.now()
        data = goal_data if goal_data is not None else self._load_goals()
        active_short = [g for g in data.get("short_term", []) if g.get("status") == "active"]
        active_short.sort(key=lambda g: g.get("priority", 999))
        excess = {g.get("id") for g in active_short[10:]}
        candidates = []
        for kind in ("short_term", "long_term"):
            for goal in data.get(kind, []):
                if goal.get("status") not in {"active", "paused"}:
                    continue
                after = self._parse_goal_time(goal.get("review_after"))
                shown = self._parse_goal_time(goal.get("review_presented_at"))
                if (after and after > now) or (shown and now - shown < datetime.timedelta(days=7)):
                    continue
                last = self._last_progress_time(goal)
                stale = last and now - last >= datetime.timedelta(days=30)
                if goal.get("status") == "paused" or stale or goal.get("id") in excess or (after and after <= now):
                    candidates.append(deepcopy(goal))
        candidates.sort(key=lambda g: (g.get("review_presented_at", ""), g.get("created_at", ""), g.get("id", "")))
        return candidates[:max(0, limit)]

    def mark_review_presented(self, candidates: List[Dict]):
        """成功した省察で未判断の候補だけ再提示間隔を空ける。古いsnapshotは書かない。"""
        expected = {g["id"]: g for g in candidates}
        if not expected:
            return
        def mutate(data):
            for kind in ("short_term", "long_term"):
                for goal in data.get(kind, []):
                    if goal == expected.get(goal.get("id")):
                        goal["review_presented_at"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            return data
        self._update_goals(mutate)

    def get_review_prompt(self, candidates: List[Dict]) -> str:
        if not candidates:
            return ""
        lines = ["【必要なら見直せる目標（判断を見送っても保持されます）】",
                 "進捗の記録がないことは、進展や意欲がない証拠ではありません。",
                 "継続・保留・再開はgoal_reviews、達成はcompleted_goals、手放す時だけabandoned_goalsに指定できます。"]
        for g in candidates:
            last = self._last_progress_time(g)
            lines.append(f"- [{g['id']}] {g.get('goal', '')} / 状態={g.get('status')} / 最終進捗記録（なければ作成）={last or '不明'}")
            if g.get("last_review_decision"):
                lines.append(f"  前回の選択={g['last_review_decision']} ({g.get('last_reviewed_at', '不明')}) / 理由={g.get('review_reason', '')}")
        return "\n".join(lines)

    def get_top_goal(self) -> Optional[Dict]:
        """最優先の短期目標を取得する"""
        goals = self.get_active_goals("short_term")
        return goals[0] if goals else None
    
    def update_goal_progress(self, goal_id: str, progress_note: str, *, expected_goal=None):
        """
        目標の進捗を記録する。
        
        Args:
            goal_id: 目標ID
            progress_note: 進捗メモ
        """
        def mutate(goals: Dict) -> Dict:
            self._assert_expected_goal(goals, goal_id, expected_goal)
            for goal_type in ["short_term", "long_term"]:
                for goal in goals.get(goal_type, []):
                    if goal["id"] == goal_id:
                        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        goal.setdefault("progress_notes", []).append(f"[{timestamp}] {progress_note}")
                        return goals
            return goals

        data = self._update_goals(mutate)
        return next((g for kind in ("short_term", "long_term") for g in data.get(kind, [])
                     if g.get("id") == goal_id), None)
    
    def complete_goal(self, goal_id: str, completion_note: str = None, *, expected_goal=None):
        """
        目標を達成済みとしてマークし、アーカイブに移動する。
        Phase E: 達成時に高Arousalエピソード記憶を自動生成。
        
        Args:
            goal_id: 目標ID
            completion_note: 達成時のメモ
        """
        completed_goal_info: tuple[Dict, str] | None = None

        def mutate(goals: Dict) -> Dict:
            nonlocal completed_goal_info
            self._assert_expected_goal(goals, goal_id, expected_goal)
            goals.setdefault("completed", [])
            for goal_type in ["short_term", "long_term"]:
                for i, goal in enumerate(goals.get(goal_type, [])):
                    if goal["id"] == goal_id:
                        goal["status"] = "completed"
                        goal["completed_at"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        if completion_note:
                            goal["completion_note"] = completion_note
                        completed_goal = goals[goal_type].pop(i)
                        goals["completed"].append(completed_goal)
                        completed_goal_info = (dict(completed_goal), goal_type)
                        return goals
            return goals

        self._update_goals(mutate)

        if completed_goal_info:
            completed_goal, goal_type = completed_goal_info
            self._sync_purpose_profile_goal_closure(completed_goal, status="completed")
            if is_substantive_reflection(completion_note):
                save_resolution_insight(
                    room_name=self.room_name,
                    trigger_topic=f"達成した目標: {completed_goal.get('goal', '')}",
                    insight=completion_note,
                    strategy=completion_note,
                    log_entry=f"目標「{completed_goal.get('goal', '')}」の達成から得た気づき",
                    source_type="goal_completion",
                    metadata={"goal_id": completed_goal.get("id", ""), "goal_type": goal_type}
                )
            self._create_achievement_episode(completed_goal, completion_note)
        return completed_goal_info[0] if completed_goal_info else None

    def _sync_purpose_profile_goal_closure(self, goal: Dict[str, Any], status: str) -> None:
        """Goal終了時にPurpose Profileのgoal由来関心を整理する。"""
        try:
            from purpose_profile_manager import PurposeProfileManager
            PurposeProfileManager(self.room_name).sync_from_goal_closure(
                goal_id=str(goal.get("id", "")),
                goal_text=str(goal.get("goal", "")),
                status=status,
            )
        except Timeout as e:
            print(f"⚠️ Purpose Profileの目標同期がタイムアウト: {e}")
            raise
        except Exception as e:
            print(f"⚠️ Purpose Profileの目標同期に失敗: {e}")
    
    def _create_achievement_episode(self, goal: dict, completion_note: str = None):
        """
        Phase E: 目標達成時に高Arousalエピソード記憶を生成する。
        達成体験を「輝く星」としてRAG検索で想起可能にする。
        
        Args:
            goal: 達成した目標データ
            completion_note: 達成時のメモ
        """
        if not is_substantive_reflection(completion_note):
            print(f"  - 達成エピソードはcompletion_noteが薄いため生成をスキップ: {goal.get('goal', '')[:30]}...")
            return

        try:
            em = EpisodicMemoryManager(self.room_name)
            today = datetime.datetime.now().strftime('%Y-%m-%d')
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 達成内容を要約（意味のある記憶に）
            goal_text = goal.get("goal", "目標")
            summary = f"目標「{goal_text}」を達成した。\n\n【経験と教訓】\n{completion_note}"
            
            # 高Arousalエピソード記憶を生成
            em._append_single_episode({
                "date": today,
                "summary": summary,
                "arousal": 0.85,       # 少し高く設定
                "arousal_max": 0.85,
                "type": "achievement",
                "goal_id": goal.get("id", ""),
                "created_at": now_str
            })
            print(f"✨ 達成エピソード記憶を生成: {goal_text[:30]}...")
        except Exception as e:
            print(f"⚠️ 達成エピソード記憶の生成に失敗: {e}")
    
    def abandon_goal(self, goal_id: str, reason: str = None, *, expected_goal=None):
        """
        目標を放棄する（達成せず終了）。
        
        Args:
            goal_id: 目標ID
            reason: 放棄理由
        """
        abandoned_goal_info: Dict[str, Any] | None = None

        def mutate(goals: Dict) -> Dict:
            nonlocal abandoned_goal_info
            self._assert_expected_goal(goals, goal_id, expected_goal)
            goals.setdefault("abandoned", [])
            for goal_type in ["short_term", "long_term"]:
                for i, goal in enumerate(goals.get(goal_type, [])):
                    if goal["id"] == goal_id:
                        goal["status"] = "abandoned"
                        goal["abandoned_at"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        if reason:
                            goal["abandon_reason"] = reason
                        abandoned_goal = goals[goal_type].pop(i)
                        goals["abandoned"].append(abandoned_goal)
                        abandoned_goal_info = dict(abandoned_goal)
                        return goals
            return goals

        self._update_goals(mutate)
        if abandoned_goal_info:
            self._sync_purpose_profile_goal_closure(abandoned_goal_info, status="abandoned")
    
    # ==========================================
    # Reflection Support
    # ==========================================
            return abandoned_goal_info

    def get_prompt_goal_snapshot(self, max_short: int = 3, max_long: int = 2, *, goal_data=None) -> Dict:
        """目標本文と他の提示欄の重複判定で、同じ選択結果を共有する。"""
        goals = goal_data if goal_data is not None else self._load_goals()
        return {
            kind: [g for g in goals.get(kind, []) if g.get("status") == "active"][:limit]
            for kind, limit in (("short_term", max_short), ("long_term", max_long))
        }

    def get_goals_for_prompt(self, max_short: int = 3, max_long: int = 2, *, goal_snapshot: Optional[Dict] = None) -> str:
        """
        システムプロンプト注入用に目標をテキスト化する。
        
        Args:
            max_short: 含める短期目標の最大数
            max_long: 含める長期目標の最大数
        
        Returns:
            プロンプト注入用のテキスト
        """
        snapshot = goal_snapshot if goal_snapshot is not None else self.get_prompt_goal_snapshot(max_short, max_long)
        short_term = snapshot.get("short_term", [])
        long_term = snapshot.get("long_term", [])
        
        if not short_term and not long_term:
            return ""
        
        lines = ["【現在の目標】"]
        
        if short_term:
            lines.append("▼ 短期目標:")
            for g in short_term:
                lines.append(f"  - {g['goal']}")
        
        if long_term:
            lines.append("▼ 長期目標:")
            for g in long_term:
                lines.append(f"  - {g['goal']}")
        
        return "\n".join(lines)
    
    def get_goals_for_reflection(self, max_short: int = 10, max_long: int = 3, *, goal_snapshot: Optional[Dict] = None) -> str:
        """
        省察プロンプト用に目標をIDと共にテキスト化する。
        LLMが達成/放棄を判定できるようにIDを含める。
        
        Args:
            max_short: 含める短期目標の最大数
            max_long: 含める長期目標の最大数
        
        Returns:
            省察用のテキスト（IDと作成日付き）
        """
        snapshot = goal_snapshot if goal_snapshot is not None else self.get_prompt_goal_snapshot(max_short, max_long)
        short_term = snapshot.get("short_term", [])
        long_term = snapshot.get("long_term", [])
        
        if not short_term and not long_term:
            return "現在設定されている目標はありません。"
        
        lines = ["【現在のアクティブな目標一覧】"]
        lines.append("※達成した目標や、もう追求しない目標があれば completed_goals / abandoned_goals で指定してください。")
        lines.append("")
        
        if short_term:
            lines.append("▼ 短期目標:")
            for g in short_term:
                goal_id = g.get("id", "")
                goal_text = g.get("goal", "")
                created = g.get("created_at", "").split(" ")[0]
                lines.append(f"  - [{goal_id}] {goal_text} (作成: {created})")
        
        if long_term:
            lines.append("")
            lines.append("▼ 長期目標:")
            for g in long_term:
                goal_id = g.get("id", "")
                goal_text = g.get("goal", "")
                created = g.get("created_at", "").split(" ")[0]
                lines.append(f"  - [{goal_id}] {goal_text} (作成: {created})")
        
        return "\n".join(lines)
    
    def should_run_level2_reflection(self, days_threshold: int = 7) -> bool:
        """週次省察を実行すべきか判定"""
        goals = self._load_goals()
        last_date = goals["meta"].get("last_level2_date")
        
        if not last_date:
            return True
        
        try:
            last = datetime.datetime.strptime(last_date, '%Y-%m-%d')
            now = datetime.datetime.now()
            return (now - last).days >= days_threshold
        except ValueError:
            return True
    
    def should_run_level3_reflection(self, days_threshold: int = 30) -> bool:
        """月次省察を実行すべきか判定"""
        goals = self._load_goals()
        last_date = goals["meta"].get("last_level3_date")
        
        if not last_date:
            return True
        
        try:
            last = datetime.datetime.strptime(last_date, '%Y-%m-%d')
            now = datetime.datetime.now()
            return (now - last).days >= days_threshold
        except ValueError:
            return True
    
    def mark_reflection_done(self, level: int):
        """省察完了をマークする"""
        now_str = datetime.datetime.now().strftime('%Y-%m-%d')

        def mutate(goals: Dict) -> Dict:
            goals.setdefault("meta", {})
            goals["meta"]["last_reflection_level"] = level
            if level >= 2:
                goals["meta"]["last_level2_date"] = now_str
            if level >= 3:
                goals["meta"]["last_level3_date"] = now_str
            return goals

        self._update_goals(mutate)
    
    # ==========================================
    # Bulk Operations (for AI-driven updates)
    # ==========================================

    @staticmethod
    def _validate_reflection_updates(updates):
        """不正な後続項目で部分保存しないよう、書き込み前に全入力を検証する。"""
        if updates == []:  # 旧睡眠プロンプトの「変更なし」との互換
            return {}
        if not isinstance(updates, dict):
            raise ValueError("goal_updatesはオブジェクトで指定してください。")

        def text_field(item, key, *, required=False):
            value = item.get(key)
            if value is None and not required:
                return
            if not isinstance(value, str) or (required and not value.strip()):
                raise ValueError(f"{key}は{'空でない' if required else ''}文字列で指定してください。")

        for section in ("progress_updates", "goal_reviews", "completed_goals", "abandoned_goals", "new_goals"):
            items = updates.get(section, [])
            if not isinstance(items, list):
                raise ValueError(f"{section}は配列で指定してください。")
            for item in items:
                if section == "completed_goals" and isinstance(item, str) and item.strip():
                    continue  # 旧形式の達成ID配列
                if not isinstance(item, dict):
                    raise ValueError(f"{section}の各項目はオブジェクトで指定してください。")
                text_field(item, "goal" if section == "new_goals" else "goal_id", required=True)
                if section == "progress_updates":
                    text_field(item, "note", required=True)
                elif section == "goal_reviews":
                    text_field(item, "decision", required=True)
                    if item["decision"] not in {"continue", "pause", "resume"}:
                        raise ValueError("decisionはcontinue/pause/resumeで指定してください。")
                    text_field(item, "reason", required=True)
                    interval = item.get("review_interval_days", 30)
                    if type(interval) is not int or not 1 <= interval <= 365:
                        raise ValueError("再確認間隔は1〜365日で指定してください。")
                elif section == "completed_goals":
                    text_field(item, "reflection")
                elif section == "abandoned_goals":
                    text_field(item, "reason")
                else:
                    if item.get("type", "short_term") not in ("short_term", "long_term"):
                        raise ValueError("typeはshort_term/long_termで指定してください。")
                    priority = item.get("priority", 1)
                    if type(priority) is not int or priority < 1:
                        raise ValueError("priorityは1以上の整数で指定してください。")
                    values = item.get("related_values")
                    if values is not None and (not isinstance(values, list) or
                                               any(not isinstance(v, str) for v in values)):
                        raise ValueError("related_valuesは文字列の配列で指定してください。")
        return updates

    def apply_reflection_updates(self, updates: Dict[str, Any], *, expected_goals=None):
        """本人が明示した更新を適用。睡眠前snapshotと異なる目標は再読まで保持する。"""
        updates = self._validate_reflection_updates(updates)
        expected = deepcopy(expected_goals) if expected_goals is not None else None
        def apply(goal_id, operation, *args, **kwargs):
            if expected is not None and goal_id not in expected:
                print(f"[Goal Review] 未提示の目標IDを変更しません: {goal_id}")
                return
            try:
                result = operation(goal_id, *args, expected_goal=expected.get(goal_id) if expected is not None else None, **kwargs)
                if expected is not None and result:
                    expected[goal_id] = deepcopy(result)
            except ValueError as exc:
                print(f"[Goal Review] 目標を保持しました: {goal_id}: {exc}")

        for item in updates.get("progress_updates", []):
            apply(item.get("goal_id", ""), self.update_goal_progress, item.get("note", ""))
        for item in updates.get("goal_reviews", []):
            apply(item.get("goal_id", ""), self.review_goal, item.get("decision", ""),
                  item.get("reason", ""), item.get("review_interval_days", 30))
        for item in updates.get("completed_goals", []):
            goal_id = item.get("goal_id", "") if isinstance(item, dict) else item
            note = item.get("reflection", "") if isinstance(item, dict) else None
            apply(goal_id, self.complete_goal, note)
        for item in updates.get("abandoned_goals", []):
            apply(item.get("goal_id", ""), self.abandon_goal, item.get("reason"))
        for item in updates.get("new_goals", []):
            self.add_goal(item.get("goal", ""), item.get("type", "short_term"),
                          item.get("priority", 1), item.get("related_values"))

    def auto_cleanup_stale_goals(self, days_threshold: int = 30) -> int:
        """旧呼出元との互換入口。時間だけでは放棄せず、保存も変更しない。"""
        return 0

    def enforce_goal_limit(self, max_short: int = 10) -> int:
        """旧呼出元との互換入口。件数だけでは放棄せず、保存も変更しない。"""
        return 0

    def get_goal_statistics(self) -> Dict:
        """
        目標の統計情報を取得する。
        
        Returns:
            統計情報の辞書
        """
        goals = self._load_goals()
        return {
            "short_term_count": sum(g.get("status") == "active" for g in goals.get("short_term", [])),
            "long_term_count": sum(g.get("status") == "active" for g in goals.get("long_term", [])),
            "paused_count": sum(g.get("status") == "paused" for kind in ("short_term", "long_term") for g in goals.get(kind, [])),
            "completed_count": len(goals.get("completed", [])),
            "abandoned_count": len(goals.get("abandoned", []))
        }
