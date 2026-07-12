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

import constants
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
                if existing_goal.get("status", "active") != "active":
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
    
    def get_top_goal(self) -> Optional[Dict]:
        """最優先の短期目標を取得する"""
        goals = self.get_active_goals("short_term")
        return goals[0] if goals else None
    
    def update_goal_progress(self, goal_id: str, progress_note: str):
        """
        目標の進捗を記録する。
        
        Args:
            goal_id: 目標ID
            progress_note: 進捗メモ
        """
        def mutate(goals: Dict) -> Dict:
            for goal_type in ["short_term", "long_term"]:
                for goal in goals.get(goal_type, []):
                    if goal["id"] == goal_id:
                        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        goal.setdefault("progress_notes", []).append(f"[{timestamp}] {progress_note}")
                        return goals
            return goals

        self._update_goals(mutate)
    
    def complete_goal(self, goal_id: str, completion_note: str = None):
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
        return

    def _sync_purpose_profile_goal_closure(self, goal: Dict[str, Any], status: str) -> None:
        """Goal終了時にPurpose Profileのgoal由来関心を整理する。"""
        try:
            from purpose_profile_manager import PurposeProfileManager
            PurposeProfileManager(self.room_name).sync_from_goal_closure(
                goal_id=str(goal.get("id", "")),
                goal_text=str(goal.get("goal", "")),
                status=status,
            )
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
    
    def abandon_goal(self, goal_id: str, reason: str = None):
        """
        目標を放棄する（達成せず終了）。
        
        Args:
            goal_id: 目標ID
            reason: 放棄理由
        """
        abandoned_goal_info: Dict[str, Any] | None = None

        def mutate(goals: Dict) -> Dict:
            nonlocal abandoned_goal_info
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
    
    def get_goals_for_prompt(self, max_short: int = 3, max_long: int = 2) -> str:
        """
        システムプロンプト注入用に目標をテキスト化する。
        
        Args:
            max_short: 含める短期目標の最大数
            max_long: 含める長期目標の最大数
        
        Returns:
            プロンプト注入用のテキスト
        """
        short_term = self.get_active_goals("short_term")[:max_short]
        long_term = self.get_active_goals("long_term")[:max_long]
        
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
    
    def get_goals_for_reflection(self, max_short: int = 10, max_long: int = 3) -> str:
        """
        省察プロンプト用に目標をIDと共にテキスト化する。
        LLMが達成/放棄を判定できるようにIDを含める。
        
        Args:
            max_short: 含める短期目標の最大数
            max_long: 含める長期目標の最大数
        
        Returns:
            省察用のテキスト（IDと作成日付き）
        """
        short_term = self.get_active_goals("short_term")[:max_short]
        long_term = self.get_active_goals("long_term")[:max_long]
        
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
    
    def apply_reflection_updates(self, updates: Dict[str, Any]):
        """
        AI省察からの一括更新を適用する。
        
        Args:
            updates: AI からの更新データ（形式は dreaming_manager と連携）
            {
                "new_goals": [{"goal": "...", "type": "short_term", "priority": 1}],
                "progress_updates": [{"goal_id": "...", "note": "..."}],
                "completed_goals": ["goal_id_1", "goal_id_2"],
                "abandoned_goals": [{"goal_id": "...", "reason": "..."}]
            }
        """
        # 新規目標追加
        for new_goal in updates.get("new_goals", []):
            self.add_goal(
                goal_text=new_goal.get("goal", ""),
                goal_type=new_goal.get("type", "short_term"),
                priority=new_goal.get("priority", 1),
                related_values=new_goal.get("related_values")
            )
        
        # 進捗更新
        for progress in updates.get("progress_updates", []):
            self.update_goal_progress(
                goal_id=progress.get("goal_id", ""),
                progress_note=progress.get("note", "")
            )
        
        # 達成マーク
        for goal_id in updates.get("completed_goals", []):
            self.complete_goal(goal_id)
        
        # 放棄マーク
        for abandoned in updates.get("abandoned_goals", []):
            self.abandon_goal(
                goal_id=abandoned.get("goal_id", ""),
                reason=abandoned.get("reason")
            )
    
    # ==========================================
    # Auto Cleanup (Phase D)
    # ==========================================
    
    def auto_cleanup_stale_goals(self, days_threshold: int = 30) -> int:
        """
        長期間アクティブな短期目標を自動放棄する。
        
        Args:
            days_threshold: この日数以上経過した短期目標は放棄対象
        
        Returns:
            放棄した目標の数
        """
        now = datetime.datetime.now()
        abandoned_count = 0

        def mutate(goals: Dict) -> Dict:
            nonlocal abandoned_count
            goals.setdefault("abandoned", [])
            remaining_short_term = []
            for goal in goals.get("short_term", []):
                created_str = goal.get("created_at", "")
                if not created_str:
                    remaining_short_term.append(goal)
                    continue
                try:
                    created = datetime.datetime.strptime(created_str, '%Y-%m-%d %H:%M:%S')
                    days_elapsed = (now - created).days
                except ValueError:
                    remaining_short_term.append(goal)
                    continue

                if days_elapsed < days_threshold:
                    remaining_short_term.append(goal)
                    continue

                goal["status"] = "abandoned"
                goal["abandoned_at"] = now.strftime('%Y-%m-%d %H:%M:%S')
                goal["abandon_reason"] = f"自動整理: {days_threshold}日以上進展なし"
                goals["abandoned"].append(goal)
                abandoned_count += 1

            goals["short_term"] = remaining_short_term
            return goals

        self._update_goals(mutate)
        return abandoned_count
    
    def enforce_goal_limit(self, max_short: int = 10) -> int:
        """
        短期目標の上限を設定し、超過分は放棄する。
        優先度が低く、古い目標から放棄する。
        
        Args:
            max_short: 短期目標の最大数
        
        Returns:
            放棄した目標の数
        """
        abandoned_count = 0

        def mutate(goals: Dict) -> Dict:
            nonlocal abandoned_count
            short_term = goals.get("short_term", [])
            if len(short_term) <= max_short:
                return goals

            sorted_goals = sorted(
                short_term,
                key=lambda g: (g.get("priority", 999), g.get("created_at", "")),
                reverse=True
            )
            to_abandon_ids = {goal.get("id") for goal in sorted_goals[:len(short_term) - max_short]}
            goals.setdefault("abandoned", [])
            remaining_short_term = []
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for goal in short_term:
                if goal.get("id") in to_abandon_ids:
                    goal["status"] = "abandoned"
                    goal["abandoned_at"] = now_str
                    goal["abandon_reason"] = "自動整理: 目標上限超過"
                    goals["abandoned"].append(goal)
                    abandoned_count += 1
                else:
                    remaining_short_term.append(goal)
            goals["short_term"] = remaining_short_term
            return goals

        self._update_goals(mutate)
        return abandoned_count
    
    def get_goal_statistics(self) -> Dict:
        """
        目標の統計情報を取得する。
        
        Returns:
            統計情報の辞書
        """
        goals = self._load_goals()
        return {
            "short_term_count": len(goals.get("short_term", [])),
            "long_term_count": len(goals.get("long_term", [])),
            "completed_count": len(goals.get("completed", [])),
            "abandoned_count": len(goals.get("abandoned", []))
        }
