# action_plan_manager.py

import json
import os
from pathlib import Path
from typing import Dict, Optional, Literal
import datetime
import constants

class ActionPlanManager:
    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.memory_dir = self.room_dir / "memory"
        self.plan_file = self.memory_dir / "action_plan.json"
        
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def _load_plan(self) -> Dict:
        if self.plan_file.exists():
            try:
                with open(self.plan_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_plan(self, plan: Dict):
        with open(self.plan_file, 'w', encoding='utf-8') as f:
            json.dump(plan, f, indent=2, ensure_ascii=False)

    def schedule_action(self, intent: str, emotion: str, next_action_description: str, wake_up_minutes: int) -> str:
        """
        未来の行動を計画し、ファイルに保存する。
        （実際のタイマーセットはツール側で行うが、ここはデータの保存を担当）
        """
        wake_up_time = datetime.datetime.now() + datetime.timedelta(minutes=wake_up_minutes)
        
        plan = {
            "status": "scheduled",
            "intent": intent,
            "emotion": emotion,
            "description": next_action_description,
            "created_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "wake_up_time": wake_up_time.strftime('%Y-%m-%d %H:%M:%S')
        }
        self._save_plan(plan)
        return f"行動計画を保存しました。\n目的: {intent}\n感情: {emotion}\n予定: {wake_up_minutes}分後"

    def get_active_plan(self) -> Optional[Dict]:
        """現在有効な計画があれば返す。なければNone"""
        plan = self._load_memory() # _load_plan の間違い修正
        if plan and plan.get("status") == "scheduled":
            return plan
        return None
        
    def _load_memory(self) -> Dict: # 内部メソッド名の統一
        return self._load_plan()

    def clear_plan(self) -> str:
        """計画を破棄する（完了、またはユーザー割り込み時）"""
        self._save_plan({}) # 空にする
        return "行動計画をクリアしました。"

    def get_plan_context_for_prompt(self) -> str:
        """
        プロンプト注入用。
        計画が存在する場合、それをテキスト化して返す。
        """
        plan = self._load_plan()
        if not plan or plan.get("status") != "scheduled":
            return ""
            
        return (
            f"【現在進行中の行動計画 (Action Plan)】\n"
            f"- 目的 (Intent): {plan.get('intent')}\n"
            f"- 感情 (Emotion): {plan.get('emotion')}\n"
            f"- 次の予定 (Action): {plan.get('description')}\n"
            f"- 計画時刻: {plan.get('wake_up_time')}\n"
            f"※ あなたはこの計画に基づいて行動しようとしていました。"
        )