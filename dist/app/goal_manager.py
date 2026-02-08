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
        """goals.json が存在しない場合は初期化"""
        if not self.goals_file.exists():
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
        """目標データを読み込む（ロック付き）"""
        from file_lock_utils import safe_json_read
        
        try:
            data = safe_json_read(str(self.goals_file), default=None)
            if data is None:
                return self._get_empty_goals()
            return data if isinstance(data, dict) else self._get_empty_goals()
        except Exception:
            return self._get_empty_goals()
    
    def _save_goals(self, goals: Dict):
        """目標データを保存する（ロック付き）"""
        from file_lock_utils import safe_json_write
        
        goals["meta"]["last_updated"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        safe_json_write(str(self.goals_file), goals)
    
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
        goals = self._load_goals()
        
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
        
        # 優先度でソート
        goals[goal_type].sort(key=lambda x: x.get("priority", 999))
        
        self._save_goals(goals)
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
        goals = self._load_goals()
        
        for goal_type in ["short_term", "long_term"]:
            for goal in goals.get(goal_type, []):
                if goal["id"] == goal_id:
                    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    goal["progress_notes"].append(f"[{timestamp}] {progress_note}")
                    self._save_goals(goals)
                    return
    
    def complete_goal(self, goal_id: str, completion_note: str = None):
        """
        目標を達成済みとしてマークし、アーカイブに移動する。
        Phase E: 達成時に高Arousalエピソード記憶を自動生成。
        
        Args:
            goal_id: 目標ID
            completion_note: 達成時のメモ
        """
        goals = self._load_goals()
        
        for goal_type in ["short_term", "long_term"]:
            for i, goal in enumerate(goals.get(goal_type, [])):
                if goal["id"] == goal_id:
                    goal["status"] = "completed"
                    goal["completed_at"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    if completion_note:
                        goal["completion_note"] = completion_note
                    
                    # アーカイブに移動
                    completed_goal = goals[goal_type].pop(i)
                    goals["completed"].append(completed_goal)
                    self._save_goals(goals)
                    
                    # Phase E: 達成エピソード記憶を生成
                    self._create_achievement_episode(completed_goal, completion_note)
                    return
    
    def _create_achievement_episode(self, goal: dict, completion_note: str = None):
        """
        Phase E: 目標達成時に高Arousalエピソード記憶を生成する。
        達成体験を「輝く星」としてRAG検索で想起可能にする。
        
        Args:
            goal: 達成した目標データ
            completion_note: 達成時のメモ
        """
        try:
            em = EpisodicMemoryManager(self.room_name)
            today = datetime.datetime.now().strftime('%Y-%m-%d')
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 達成内容を要約（意味のある記憶に）
            goal_text = goal.get("goal", "目標")
            if completion_note:
                # 学び・気づきがある場合は、それを中心に記憶を構築
                summary = f"目標「{goal_text}」を達成した。{completion_note}"
            else:
                # 学びがない場合はシンプルに
                summary = f"目標「{goal_text}」を達成した。"
            
            # 高Arousalエピソード記憶を生成
            em._append_single_episode({
                "date": today,
                "summary": summary,
                "arousal": 0.8,        # 高Arousal = 成功体験
                "arousal_max": 0.8,
                "type": "achievement",  # 達成タイプのマーカー
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
        goals = self._load_goals()
        
        # abandoned配列がない場合は初期化
        if "abandoned" not in goals:
            goals["abandoned"] = []
        
        for goal_type in ["short_term", "long_term"]:
            for i, goal in enumerate(goals.get(goal_type, [])):
                if goal["id"] == goal_id:
                    goal["status"] = "abandoned"
                    goal["abandoned_at"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    if reason:
                        goal["abandon_reason"] = reason
                    
                    goals["abandoned"].append(goals[goal_type].pop(i))
                    self._save_goals(goals)
                    return
    
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
        goals = self._load_goals()
        now_str = datetime.datetime.now().strftime('%Y-%m-%d')
        
        goals["meta"]["last_reflection_level"] = level
        if level >= 2:
            goals["meta"]["last_level2_date"] = now_str
        if level >= 3:
            goals["meta"]["last_level3_date"] = now_str
        
        self._save_goals(goals)
    
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
        goals = self._load_goals()
        now = datetime.datetime.now()
        abandoned_count = 0
        
        # abandoned配列がない場合は初期化
        if "abandoned" not in goals:
            goals["abandoned"] = []
        
        short_term = goals.get("short_term", [])
        to_abandon = []
        
        for goal in short_term:
            created_str = goal.get("created_at", "")
            if not created_str:
                continue
            try:
                created = datetime.datetime.strptime(created_str, '%Y-%m-%d %H:%M:%S')
                days_elapsed = (now - created).days
                if days_elapsed >= days_threshold:
                    to_abandon.append(goal["id"])
            except ValueError:
                continue
        
        for goal_id in to_abandon:
            self.abandon_goal(goal_id, reason=f"自動整理: {days_threshold}日以上進展なし")
            abandoned_count += 1
        
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
        goals = self._load_goals()
        short_term = goals.get("short_term", [])
        
        if len(short_term) <= max_short:
            return 0
        
        # 優先度（低いほど放棄）と作成日（古いほど放棄）でソート
        sorted_goals = sorted(
            short_term,
            key=lambda g: (g.get("priority", 999), g.get("created_at", "")),
            reverse=True  # 優先度が高い(数値が大きい)＝放棄対象
        )
        
        # 超過分を放棄
        excess_count = len(short_term) - max_short
        to_abandon = sorted_goals[:excess_count]
        
        for goal in to_abandon:
            self.abandon_goal(goal["id"], reason="自動整理: 目標上限超過")
        
        return excess_count
    
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
