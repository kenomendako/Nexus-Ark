import json
from typing import List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from procedure_memory_manager import ProcedureMemoryManager


def _skill_event_line(action: str, procedure_id: str = "", scope: str = "", title: str = "", timeline_id: str = "") -> str:
    payload = {
        "type": "skill_event",
        "action": action,
    }
    if procedure_id:
        payload["procedure_id"] = procedure_id
    if scope:
        payload["scope"] = scope
    if title:
        payload["title"] = title
    if timeline_id:
        payload["timeline_id"] = timeline_id
    return f"[skill_event] {json.dumps(payload, ensure_ascii=False)}"


def _resolve_procedure_scope(room_name: str, procedure_id: str) -> str:
    raw_id = str(procedure_id or "").strip()
    if raw_id.startswith("shared:"):
        return "shared"
    if raw_id.startswith("private:"):
        return "private"
    manager = ProcedureMemoryManager(room_name)
    for item in manager.list_procedures(include_shared=True):
        if item.get("procedure_id") == raw_id:
            return item.get("scope", "")
    return ""


@tool
def list_procedures(room_name: str, include_shared: bool = True) -> str:
    """
    保存済みの手順記憶（Procedural Memory / Skills）一覧を取得する。
    繰り返し行う自律行動、研究深化、ノート更新、外部連携、設定確認などの既存手順を探す時に使う。
    「以前うまくいった手順がありそう」「同じ作業をまた行う」「更新前に確認が必要」と感じたら最初に使う。
    include_shared=true の場合、共通基盤スキルとペルソナ専用スキルの両方を返す。
    """
    try:
        procedures = ProcedureMemoryManager(room_name).list_procedures(include_shared=include_shared)
        if not procedures:
            return "【手順記憶はまだありません】"
        lines = ["Procedures:"]
        for item in procedures:
            lines.append(f"- [{item.get('scope', 'private')}] {item.get('procedure_id')}: {item.get('title')}")
        lines.append("")
        lines.append(_skill_event_line("list", title=f"{len(procedures)} procedures"))
        return "\n".join(lines)
    except Exception as e:
        return f"【エラー】手順記憶一覧の取得に失敗しました: {e}"


@tool
def read_procedure(room_name: str, procedure_id: str) -> str:
    """
    指定した手順記憶を読む。
    類似する通常応答や自律行動を行う前に、過去に成功した手順を確認するために使う。
    読んだSkillは盲目的に実行せず、現在の文脈に合う手順だけを採用する。
    共有手順を明示したい場合は `shared:<procedure_id>`、ペルソナ専用手順は `private:<procedure_id>` を指定できる。
    """
    try:
        body = ProcedureMemoryManager(room_name).read_procedure(procedure_id)
        clean_id = str(procedure_id or "").split(":", 1)[-1]
        scope = _resolve_procedure_scope(room_name, procedure_id)
        return f"{body.rstrip()}\n\n{_skill_event_line('read', procedure_id=clean_id, scope=scope)}"
    except Exception as e:
        return f"【エラー】手順記憶の読み込みに失敗しました: {e}"


class SaveProcedureArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    procedure_id: str = Field(..., description="手順ID。英数字推奨。例: deepen_research_thread")
    title: str = Field(..., description="手順タイトル")
    purpose: str = Field("", description="この手順を使う目的")
    steps: List[str] = Field(..., description="実行手順のリスト")
    triggers: Optional[List[str]] = Field(None, description="この手順を使うきっかけ")
    success_criteria: str = Field("", description="成功条件")
    notes: str = Field("", description="補足メモ")
    scope: str = Field("private", description="private または shared。人格・関係性に関わる手順は必ずprivate、API手順など基盤機能だけshared")


@tool(args_schema=SaveProcedureArgs)
def save_procedure(
    room_name: str,
    procedure_id: str,
    title: str,
    steps: List[str],
    purpose: str = "",
    triggers: Optional[List[str]] = None,
    success_criteria: str = "",
    notes: str = "",
    scope: str = "private",
) -> str:
    """
    手順記憶を作成・更新する。
    何度も使いたい自律行動パターンを、Procedureとして保存する。
    成功した流れを次回も再利用したい時、または既存Skillの改善点が明確な時だけ使う。
    保存前に、必要なら `list_procedures` / `read_procedure` で重複や既存Skillを確認する。
    """
    try:
        result = ProcedureMemoryManager(room_name).save_procedure(
            procedure_id=procedure_id,
            title=title,
            purpose=purpose,
            steps=steps,
            triggers=triggers or [],
            success_criteria=success_criteria,
            notes=notes,
            scope=scope,
        )
        return (
            f"成功: 手順記憶 '{result.get('procedure_id')}' を {result.get('scope')} に保存しました。\n"
            f"{_skill_event_line('save', procedure_id=result.get('procedure_id', ''), scope=result.get('scope', ''), title=result.get('title', ''))}"
        )
    except Exception as e:
        return f"【エラー】手順記憶の保存に失敗しました: {e}"


class CreateProcedureFromTimelineArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    timeline_id: str = Field(..., description="手順化する自律行動timeline_id")
    procedure_id: str = Field("", description="保存するprocedure_id。空なら自動推定")
    title: str = Field("", description="手順タイトル。空ならtimelineから自動推定")
    purpose: str = Field("", description="手順の目的。空ならtimelineから自動推定")


@tool(args_schema=CreateProcedureFromTimelineArgs)
def create_procedure_from_timeline(
    room_name: str,
    timeline_id: str,
    procedure_id: str = "",
    title: str = "",
    purpose: str = "",
) -> str:
    """
    成功した自律行動timelineから手順記憶を生成する。
    observe/orient/decide/act/reflect の記録を、再利用可能なProcedure Markdownへ変換する。
    一連の自律行動が明確に成功し、次回も同じ型で使えそうな時に使う。
    """
    try:
        result = ProcedureMemoryManager(room_name).create_from_timeline(
            timeline_id=timeline_id,
            procedure_id=procedure_id,
            title=title,
            purpose=purpose,
        )
        return (
            f"成功: timeline '{timeline_id}' から手順記憶 '{result.get('procedure_id')}' を作成しました。\n"
            f"{_skill_event_line('create_from_timeline', procedure_id=result.get('procedure_id', ''), scope=result.get('scope', ''), title=result.get('title', ''), timeline_id=timeline_id)}"
        )
    except Exception as e:
        return f"【エラー】timelineからの手順記憶作成に失敗しました: {e}"
