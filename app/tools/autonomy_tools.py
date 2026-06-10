from typing import Any, List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from autonomy_context_manager import AutonomyContextManager


class ReadAutonomyContextArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    query: str = Field("", description="今回の自律行動の関心・動機・検索語。空でもよい")
    include_details: bool = Field(False, description="アクティブなWorking Memory本文も含める場合はtrue")


@tool(args_schema=ReadAutonomyContextArgs)
def read_autonomy_context(room_name: str, query: str = "", include_details: bool = False) -> str:
    """
    自律行動前に、Purpose Profile、目標、Research Threads、Working Memory、直近Action Memoryをまとめて読む。
    Sense/Orient/Decideの足場として使い、既存深化を優先するための文脈を得る。
    """
    try:
        manager = AutonomyContextManager(room_name)
        return manager.format_context(query=query, include_details=include_details)
    except Exception as e:
        return f"【エラー】Autonomy Contextの取得に失敗しました: {e}"


class ReflectAfterActionArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    timeline_id: str = Field("", description="関連する自律行動タイムラインID。start_autonomy_timelineで得たIDを指定")
    action_summary: str = Field("", description="今回実行した行動と結果の短い要約")
    summary: str = Field("", description="action_summaryの別名。モデルが短くsummaryで出した場合の救済用")
    result_summary: str = Field("", description="action_summaryの別名。結果要約として出した場合の救済用")
    outcome_summary: str = Field("", description="action_summaryの別名。結果要約として出した場合の救済用")
    outcome_type: str = Field("observed", description="結果分類。例: progressed / blocked / learned / scheduled / rested / observed")
    result_category: str = Field("", description="outcome_typeの別名。モデルがresult_categoryで出した場合の救済用")
    next_action: str = Field("", description="次回戻るための具体的な一手。空にしないことを推奨")
    intent: str = Field("", description="今回の行動意図")
    context_type: str = Field("CONTINUE", description="過去行動との関係。CONTINUE / DEEPEN / NEW / ORGANIZE / SOCIAL / REST")
    thread_id: str = Field("", description="関連するResearch Thread ID")
    working_memory_slot: str = Field("", description="関連するWorking Memoryスロット名")
    goal_id: str = Field("", description="関連する目標ID")
    unresolved_questions: Optional[List[str]] = Field(None, description="残った問い。Research Thread更新時はopen_questionsへ統合される")
    update_thread: bool = Field(False, description="thread_idがある場合にResearch Threadのnext_actionを更新する")
    update_goal: bool = Field(False, description="goal_idがある場合に目標進捗へaction_summaryを記録する")


@tool(args_schema=ReflectAfterActionArgs)
def reflect_after_action(
    room_name: str,
    action_summary: str = "",
    timeline_id: str = "",
    summary: str = "",
    result_summary: str = "",
    outcome_summary: str = "",
    outcome_type: str = "observed",
    result_category: str = "",
    next_action: str = "",
    intent: str = "",
    context_type: str = "CONTINUE",
    thread_id: str = "",
    working_memory_slot: str = "",
    goal_id: str = "",
    unresolved_questions: Optional[List[str]] = None,
    update_thread: bool = False,
    update_goal: bool = False,
) -> str:
    """
    自律行動後のReflectステップを記録する。
    行動結果、次回アクション、関連するResearch Thread / Working Memory / Goalをタイムラインとして残す。
    """
    try:
        action_summary = (
            action_summary
            or summary
            or result_summary
            or outcome_summary
            or "自律行動を実行し、結果を確認した"
        )
        if (not outcome_type or outcome_type == "observed") and result_category:
            outcome_type = result_category
        manager = AutonomyContextManager(room_name)
        record = manager.append_reflection(
            action_summary=action_summary,
            outcome_type=outcome_type,
            next_action=next_action,
            intent=intent,
            context_type=context_type,
            thread_id=thread_id,
            working_memory_slot=working_memory_slot,
            goal_id=goal_id,
            unresolved_questions=unresolved_questions or [],
            update_thread=update_thread,
            update_goal=update_goal,
            timeline_id=timeline_id,
        )
        updates = record.get("updates", {})
        update_text = ""
        if updates:
            update_text = "\n更新: " + ", ".join(f"{k}={v}" for k, v in updates.items())
        return (
            "成功: 自律行動のReflectを記録しました。\n"
            f"結果分類: {record.get('outcome_type')}\n"
            f"次の一手: {record.get('next_action') or '未設定'}"
            f"{update_text}"
        )
    except Exception as e:
        return f"【エラー】自律行動Reflectの記録に失敗しました: {e}"


class StartAutonomyTimelineArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    trigger: str = Field("", description="自律行動の発火理由。例: boredom / curiosity / scheduled_action")
    query: str = Field("", description="今回の行動テーマや関心")
    motivation: str = Field("", description="現在の動機や内部状態の短い説明")
    source: str = Field("autonomous", description="発火元。例: alarm_manager / timer / manual")


@tool(args_schema=StartAutonomyTimelineArgs)
def start_autonomy_timeline(
    room_name: str,
    trigger: str = "",
    query: str = "",
    motivation: str = "",
    source: str = "autonomous",
) -> str:
    """
    自律行動の型付きstep timelineを開始し、timeline_idを発行する。
    observe/orient/decide/act/reflect を記録する前に使う。
    """
    try:
        record = AutonomyContextManager(room_name).start_timeline(
            trigger=trigger,
            query=query,
            motivation=motivation,
            source=source,
        )
        return (
            "成功: 自律行動タイムラインを開始しました。\n"
            f"timeline_id: {record.get('timeline_id')}"
        )
    except Exception as e:
        return f"【エラー】自律行動タイムラインの開始に失敗しました: {e}"


class RecordAutonomyStepArgs(BaseModel):
    model_config = ConfigDict(extra="allow")

    room_name: str = Field(..., description="対象のルーム名")
    timeline_id: str = Field("", description="start_autonomy_timelineで得たtimeline_id。未指定時は自動開始する")
    step_type: str = Field("", description="observe / orient / decide / act / reflect のいずれか。未指定時は入力内容から推定する")
    summary: str = Field("", description="このステップで何を観測・判断・決定・実行・反省したかの短い要約")
    details: Any = Field("", description="追加詳細。文字列、配列、JSONオブジェクトを指定可能")
    selected_action: str = Field("", description="decide/actで選んだ行動")
    tool_name: str = Field("", description="actで使ったツール名")
    tool_result_summary: str = Field("", description="actで得た結果の短い要約")
    thread_id: str = Field("", description="関連するResearch Thread ID")
    working_memory_slot: str = Field("", description="関連するWorking Memoryスロット名")
    goal_id: str = Field("", description="関連する目標ID")
    action_memory_ref: str = Field("", description="Action Memoryと相互参照するための補助メモや時刻")


def _infer_record_step_type(step_type: str, payload: dict) -> str:
    """record_autonomy_stepの旧式・省略引数からstep_typeを補完する。"""
    normalized = str(step_type or "").strip().lower()
    aliases = {
        "observation": "observe",
        "sense": "observe",
        "thinking": "orient",
        "reasoning": "orient",
        "plan": "decide",
        "decision": "decide",
        "execute": "act",
        "execution": "act",
        "result": "reflect",
        "reflection": "reflect",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {"observe", "orient", "decide", "act", "reflect"}:
        return normalized

    if payload.get("tool_name") or payload.get("tool_result_summary") or payload.get("result_summary"):
        return "act"
    if payload.get("selected_action") or payload.get("action") or payload.get("decision"):
        return "decide"
    if payload.get("reasoning") or payload.get("analysis") or payload.get("category"):
        return "orient"
    if payload.get("outcome_summary") or payload.get("next_action"):
        return "reflect"
    return "observe"


def _coerce_record_step_summary(summary: str, payload: dict) -> str:
    """必須summary欠落時に、モデルが出しやすい別名や旧形式から短い要約を作る。"""
    if summary:
        return str(summary)

    for key in [
        "action_summary",
        "result_summary",
        "outcome_summary",
        "observation",
        "reasoning",
        "decision",
        "selected_action",
        "intent",
        "instruction",
        "content",
        "text",
        "message",
        "note",
        "query",
    ]:
        value = payload.get(key)
        if value:
            return str(value)

    category = payload.get("category")
    if category:
        return f"自律行動中に {category} カテゴリの能力要求を検討した"

    compact_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"room_name", "timeline_id", "step_type", "details"} and value not in ("", None)
    }
    if compact_payload:
        return f"自律行動ステップを記録した: {compact_payload}"
    return "自律行動ステップを記録した"


@tool(args_schema=RecordAutonomyStepArgs)
def record_autonomy_step(
    room_name: str,
    timeline_id: str = "",
    step_type: str = "",
    summary: str = "",
    details: Any = "",
    selected_action: str = "",
    tool_name: str = "",
    tool_result_summary: str = "",
    thread_id: str = "",
    working_memory_slot: str = "",
    goal_id: str = "",
    action_memory_ref: str = "",
    **extra: Any,
) -> str:
    """
    自律行動タイムラインへ observe/orient/decide/act/reflect の型付きステップを追記する。
    Action Memoryだけでは残しきれない「なぜそう動いたか」を保存するために使う。
    """
    try:
        payload = {
            "timeline_id": timeline_id,
            "step_type": step_type,
            "summary": summary,
            "details": details,
            "selected_action": selected_action,
            "tool_name": tool_name,
            "tool_result_summary": tool_result_summary,
            "thread_id": thread_id,
            "working_memory_slot": working_memory_slot,
            "goal_id": goal_id,
            "action_memory_ref": action_memory_ref,
            **extra,
        }
        step_type = _infer_record_step_type(step_type, payload)
        summary = _coerce_record_step_summary(summary, payload)
        if not selected_action:
            selected_action = str(extra.get("action") or extra.get("decision") or "")
        if not tool_result_summary:
            tool_result_summary = str(extra.get("result_summary") or extra.get("outcome_summary") or "")

        manager = AutonomyContextManager(room_name)
        if not timeline_id:
            start_record = manager.start_timeline(
                trigger=str(extra.get("trigger") or "autonomy_step_rescue"),
                query=str(extra.get("query") or extra.get("intent") or extra.get("instruction") or summary),
                motivation=str(extra.get("motivation") or ""),
                source=str(extra.get("source") or "record_autonomy_step"),
            )
            timeline_id = start_record.get("timeline_id", "")

        record = manager.append_step(
            timeline_id=timeline_id,
            step_type=step_type,
            summary=summary,
            details=details,
            selected_action=selected_action,
            tool_name=tool_name,
            tool_result_summary=tool_result_summary,
            thread_id=thread_id,
            working_memory_slot=working_memory_slot,
            goal_id=goal_id,
            action_memory_ref=action_memory_ref,
        )
        return (
            "成功: 自律行動ステップを記録しました。\n"
            f"timeline_id: {record.get('timeline_id')}\n"
            f"step_type: {record.get('step_type')}"
        )
    except Exception as e:
        return f"【エラー】自律行動ステップの記録に失敗しました: {e}"


class CompleteAutonomyTimelineArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    timeline_id: str = Field(..., description="完了するtimeline_id")
    status: str = Field("completed", description="completed / paused / abandoned など")
    summary: str = Field("", description="この自律行動全体の短いまとめ")


@tool(args_schema=CompleteAutonomyTimelineArgs)
def complete_autonomy_timeline(
    room_name: str,
    timeline_id: str,
    status: str = "completed",
    summary: str = "",
) -> str:
    """
    自律行動タイムラインを完了・中断として記録する。
    """
    try:
        record = AutonomyContextManager(room_name).complete_timeline(
            timeline_id=timeline_id,
            status=status,
            summary=summary,
        )
        return (
            "成功: 自律行動タイムラインを終了しました。\n"
            f"timeline_id: {record.get('timeline_id')}\n"
            f"status: {record.get('status')}"
        )
    except Exception as e:
        return f"【エラー】自律行動タイムラインの終了に失敗しました: {e}"
