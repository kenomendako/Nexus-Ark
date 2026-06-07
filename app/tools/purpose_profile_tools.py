from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

from langchain_core.tools import tool

from purpose_profile_manager import PurposeProfileManager


@tool
def read_purpose_profile(room_name: str) -> str:
    """
    ペルソナのPurpose Profile（長期関心、価値観、現在の関心、避けたい行動、提案中の変更）を読む。
    目的に基づいて研究・行動を選びたい時に使用する。
    """
    try:
        manager = PurposeProfileManager(room_name)
        return manager.to_pretty_json()
    except Exception as e:
        return f"【エラー】Purpose Profileの読み込みに失敗しました: {e}"


class UpdateActivePurposeArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    active_interests: Optional[List[Dict[str, Any]]] = Field(None, description='現在の関心事のリスト。例: [{"topic": "AIの進化", "reason": "最近のモデルの進歩が著しいため", "started_at": "2024-05-16"}]')
    open_questions: Optional[List[Dict[str, Any]]] = Field(None, description='現在探求中の疑問のリスト。例: [{"question": "AIは心を持つか", "context": "倫理的な観点からの考察"}]')
    reason: str = Field("", description="なぜこの更新を行うのかという理由")

@tool(args_schema=UpdateActivePurposeArgs)
def update_active_purpose(
    room_name: str,
    active_interests: Optional[List[Dict[str, Any]]] = None,
    open_questions: Optional[List[Dict[str, Any]]] = None,
    reason: str = ""
) -> str:
    """
    Purpose Profileの可変領域を更新する。
    ペルソナ自身が更新できるのは active_interests と open_questions のみ。
    core_values / stable_interests / avoid_behaviors は直接変更せず propose_purpose_change を使う。
    """
    try:
        manager = PurposeProfileManager(room_name)
        manager.update_active_purpose(
            active_interests=active_interests,
            open_questions=open_questions,
            reason=reason
        )
        return "成功: Purpose Profileの可変領域を更新しました。"
    except Exception as e:
        return f"【エラー】Purpose Profileの更新に失敗しました: {e}"


@tool
def propose_purpose_change(room_name: str, field: str, proposal: str, reason: str) -> str:
    """
    Purpose Profileの安定領域への変更を提案する。
    field は core_values / stable_interests / preferred_behaviors / avoid_behaviors のいずれか。
    提案は proposed_changes に保存され、ユーザー/UI承認まで安定領域へ反映されない。
    """
    try:
        manager = PurposeProfileManager(room_name)
        profile = manager.propose_change(field=field, proposal=proposal, reason=reason)
        proposal_id = profile.get("proposed_changes", [{}])[-1].get("id", "")
        return f"成功: Purpose Profileへの変更提案を保存しました。proposal_id={proposal_id}"
    except Exception as e:
        return f"【エラー】Purpose Profileへの変更提案に失敗しました: {e}"


@tool
def approve_purpose_change(room_name: str, proposal_id: str) -> str:
    """
    Purpose Profileの保留中提案を承認し、安定領域へ反映する。
    原則としてユーザー/UI操作用。ペルソナ自身は通常このツールを使わない。
    """
    try:
        manager = PurposeProfileManager(room_name)
        manager.approve_change(proposal_id)
        return f"成功: Purpose Profileの提案 {proposal_id} を承認しました。"
    except Exception as e:
        return f"【エラー】Purpose Profile提案の承認に失敗しました: {e}"
