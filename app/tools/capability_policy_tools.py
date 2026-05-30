"""Tools for capability policy checks and audit logging."""

import json

from langchain_core.tools import tool

from capability_policy_manager import CapabilityPolicyManager


@tool
def read_capability_policy(room_name: str) -> str:
    """
    現在の能力カテゴリ別ポリシー、承認待ち要求、監査ログ方針を確認します。

    自律行動で外部副作用のある操作を検討するとき、まずこのツールで
    category の mode（allow/ask/deny）と risk（low/medium/high）を確認してください。
    """
    try:
        policy = CapabilityPolicyManager(room_name).read_policy()
        return json.dumps(policy, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"【エラー】Capability Policyの読み込みに失敗しました: {e}"


@tool
def request_capability_approval(
    room_name: str,
    category: str,
    intent: str,
    details: str = "",
    risk_acknowledgement: str = "",
) -> str:
    """
    外部副作用やPC操作を伴う能力カテゴリについて、実行前の承認状態を確認します。

    status が approved の場合だけ実行に進んでよいです。
    status が pending または denied の場合は、その行動を実行せず、ユーザーの承認や指示を待ってください。
    """
    try:
        request = CapabilityPolicyManager(room_name).request_approval(
            category=category,
            intent=intent,
            details=details,
            risk_acknowledgement=risk_acknowledgement,
        )
        return json.dumps(request, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"【エラー】Capability承認確認に失敗しました: {e}"


@tool
def record_capability_audit(
    room_name: str,
    category: str,
    action: str,
    intent: str,
    status: str,
    details: str = "",
    related_timeline_id: str = "",
    request_id: str = "",
) -> str:
    """
    能力カテゴリを使った外部副作用のある行動について、監査ログを記録します。

    実行後は status に success/failure/skipped などを入れ、details に結果と戻し方を短く残してください。
    """
    try:
        record = CapabilityPolicyManager(room_name).record_audit(
            category=category,
            action=action,
            intent=intent,
            status=status,
            details=details,
            related_timeline_id=related_timeline_id,
            request_id=request_id,
        )
        return json.dumps(record, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"【エラー】Capability監査ログの記録に失敗しました: {e}"
