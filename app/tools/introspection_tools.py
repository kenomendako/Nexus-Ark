# tools/introspection_tools.py
"""
内省ツール - ペルソナが自律行動中に自身の内的状態を確認・編集できるツール群。
"""

import logging
from typing import Optional
from pydantic import BaseModel, Field
from langchain_core.tools import tool


logger = logging.getLogger(__name__)


def _render_unresolved_list(unresolved) -> str:
    """未解決の問い一覧を1始まりの番号付きで整形して返す。

    'list' アクションの表示と、番号ミス時のエラーメッセージの両方で使い、
    AI が正しい番号をその場で確認して即リトライできるようにする。
    """
    if not unresolved:
        return "📭 未解決の問いはありません。"
    lines = ["📋 **未解決の問い一覧**\n"]
    for ui_idx, (_, q) in enumerate(unresolved, 1):
        topic = q.get("topic", "")
        priority = q.get("priority", 0.5)
        context = q.get("context", "")
        asked = "質問済" if q.get("asked_at") else "未質問"

        priority_bar = "●" * int(priority * 5) + "○" * (5 - int(priority * 5))
        lines.append(f"{ui_idx}. 【{priority_bar}】{topic}")
        if context:
            lines.append(f"   └ {context[:50]}...")
        lines.append(f"   ({asked})")

    lines.append(f"\n合計: {len(unresolved)}件")
    return "\n".join(lines)


def _resolve_target_question(unresolved, question_index, topic):
    """resolve/remove/adjust_priority の対象を index または topic から特定する。

    戻り値は (target_pair, error_message) のタプル。target_pair が None なら
    error_message（現在の一覧を同梱）を呼び出し側がそのまま返す。
    """
    if not unresolved:
        return None, "📭 未解決の問いはありません。先に action='add' で問いを追加できます。"

    # 1) 番号指定（従来どおり・1始まり）
    if question_index is not None:
        if 1 <= question_index <= len(unresolved):
            return unresolved[question_index - 1], None
        if len(unresolved) == 1:
            logger.warning(
                "manage_open_questions: question_index=%s is out of range for one unresolved question; "
                "auto-resolving to the only item.",
                question_index,
            )
            return unresolved[0], None
        return None, (
            f"【エラー】question_index は 1〜{len(unresolved)} の範囲で指定してください。"
            "まず topic で対象を直接指定してください。"
            "番号を使う場合は、直前の list 結果に表示された番号だけが有効です。\n\n"
            + _render_unresolved_list(unresolved)
        )

    # 2) topic 指定（番号を推測せず確実に対象を選べる代替手段）
    if topic and topic.strip():
        needle = topic.strip()
        matches = [pair for pair in unresolved if pair[1].get("topic", "") == needle]
        if not matches:
            low = needle.lower()
            matches = [pair for pair in unresolved if low in pair[1].get("topic", "").lower()]
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            return None, (
                f"【エラー】topic「{needle}」に一致する未解決の問いが見つかりません。"
                "下の一覧から番号か正確な topic を指定してください。\n\n"
                + _render_unresolved_list(unresolved)
            )
        return None, (
            f"【エラー】topic「{needle}」が複数の問いに一致しました。question_index で指定してください。\n\n"
            + _render_unresolved_list(unresolved)
        )

    # 3) どちらも無い
    return None, (
        "【エラー】question_index（番号）または topic を指定してください。\n\n"
        + _render_unresolved_list(unresolved)
    )


def _sync_purpose_profile_question_closure(room_name: str, topic: str, action: str) -> None:
    """問いの解決/削除をPurpose Profile側のopen_questionsへ反映する。"""
    try:
        from purpose_profile_manager import PurposeProfileManager
        PurposeProfileManager(room_name).sync_from_question_closure(topic, action=action)
    except Exception as e:
        print(f"⚠️ Purpose Profileの問い同期に失敗: {e}")


class ManageOpenQuestionsArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    action: str = Field(..., description="実行するアクション: 'list' (一覧), 'add' (追加), 'resolve' (解決), 'remove' (削除), 'adjust_priority' (優先度変更)")
    question_index: Optional[int] = Field(None, description="操作対象の問いの番号（直前の 'list' 結果に表示された1始まりの番号だけが有効）。古い会話文脈の番号は使わず、確実に指定するなら topic を使う。")
    topic: Optional[str] = Field(None, description="'add' では追加する問いのトピック。resolve/remove/adjust_priority では question_index の代わりに対象の問いを topic で直接指定できる。番号より topic 指定が確実。")
    context: Optional[str] = Field(None, description="'add' の場合の問いの背景・なぜ気になったか。")
    new_priority: Optional[float] = Field(None, description="新しい優先度（0.0〜1.0）。'adjust_priority' の場合に必須。")
    reflection: Optional[str] = Field(None, description="解決時の学び・教訓・気づき（'resolve' の場合に必須）。今後の自分にどう活かせるか等。")

@tool(args_schema=ManageOpenQuestionsArgs)
def manage_open_questions(
    room_name: str,
    action: str,
    question_index: Optional[int] = None,
    topic: Optional[str] = None,
    context: Optional[str] = None,
    new_priority: Optional[float] = None,
    reflection: Optional[str] = None
) -> str:
    """
    未解決の問い（好奇心の源泉）を管理します。
    
    action:
      - "list": 現在の未解決の問いを一覧表示
      - "add": 新しい未解決の問いを追加
      - "resolve": 指定した問いを解決済みにマーク（reflection で学びを記録）
      - "remove": 指定した問いを完全に削除（興味がなくなった場合）
      - "adjust_priority": 優先度を変更（0.0〜1.0）
    
    question_index: 対象の問いの番号（1始まり）。番号は直前に list した結果のものだけが有効です。過去の会話に出た古い番号は使わず、確実に対象を選ぶには topic を指定してください。
    topic: 追加する問い（add用）、または resolve/remove/adjust_priority の対象 topic。対象指定では question_index より topic が確実です。
    context: 問いの背景（add用）
    new_priority: 新しい優先度（adjust_priority用）
    reflection: 解決時の学び・教訓・気づき（resolve用）。「何を知ったか」だけでなく「今後の自分にどう活かせるか、どのような教訓を得たか」を詳細に記述してください。十分に具体的な場合はInsightとエピソード記憶に保存されます。
    """
    from motivation_manager import MotivationManager
    import session_arousal_manager
    
    mm = MotivationManager(room_name)
    questions = mm._state["drives"]["curiosity"].get("open_questions", [])
    
    # 未解決の問いのみフィルタリング（resolved_at がないもの）
    unresolved = [(i, q) for i, q in enumerate(questions) if not q.get("resolved_at")]
    
    if action == "list":
        if not unresolved:
            return "📭 未解決の問いはありません。好奇心は満たされています。"
        return _render_unresolved_list(unresolved)

    if action == "add":
        if not topic or not topic.strip():
            return "【エラー】action='add' では topic を指定してください。"
        priority = 0.5 if new_priority is None else max(0.0, min(1.0, new_priority))
        mm.add_open_question(topic=topic.strip(), context=(context or "").strip(), priority=priority)
        return f"✅ 未解決の問いを追加しました: {topic.strip()} (priority={priority:.1f})"
    
    # 以降のアクションは対象の問いの特定が必要（番号 or topic）。
    # 番号ミス・未指定時は現在の一覧を同梱したエラーを返し、その場で正しく選び直せるようにする。
    target_pair, error_message = _resolve_target_question(unresolved, question_index, topic)
    if target_pair is None:
        return error_message

    actual_idx, target_q = target_pair
    topic = target_q.get("topic", "")
    
    if action == "resolve":
        # 問いを解決済みにマーク
        success = mm.mark_question_resolved(
            topic,
            answer_summary=reflection or "",
            learned_insight=reflection or ""
        )
        if not success:
            return f"【エラー】問い「{topic}」の解決マークに失敗しました。"
        _sync_purpose_profile_question_closure(room_name, topic, action="resolved")
        
        # 問い解決レポートはエピソード記憶へ一本化する。
        episode_created = _create_curiosity_resolved_episode(room_name, topic, target_q.get("context", ""), reflection)
        
        # Arousalスパイクを発生
        satisfaction_arousal = 0.4
        session_arousal_manager.add_arousal_score(room_name, satisfaction_arousal)
        
        result = f"✅ 問い「{topic}」を解決済みにしました。"
        if reflection:
            result += f"\n📝 学び: {reflection}"
        if not episode_created:
            result += "\n※ 学びが短い場合は、テンプレート的なエピソード記憶を作りません。"
        result += f"\n✨ 充足感 (Arousal +{satisfaction_arousal})"
        return result
    
    elif action == "remove":
        # 問いを完全に削除
        questions.pop(actual_idx)
        mm._state["drives"]["curiosity"]["open_questions"] = questions
        mm._save_state()
        _sync_purpose_profile_question_closure(room_name, topic, action="removed")
        return f"🗑️ 問い「{topic}」を削除しました。（もう興味がない場合など）"
    
    elif action == "adjust_priority":
        if new_priority is None:
            return "【エラー】new_priority を指定してください（0.0〜1.0）。"
        
        new_priority = max(0.0, min(1.0, new_priority))
        old_priority = target_q.get("priority", 0.5)
        questions[actual_idx]["priority"] = new_priority
        mm._save_state()
        
        direction = "⬆️" if new_priority > old_priority else "⬇️"
        return f"{direction} 問い「{topic}」の優先度を {old_priority:.1f} → {new_priority:.1f} に変更しました。"
    
    else:
        return f"【エラー】不明なアクション: {action}。list / add / resolve / remove / adjust_priority のいずれかを指定してください。"


def _save_question_resolution_insight(room_name: str, topic: str, context: str, reflection: str = None) -> bool:
    """Deprecated: question resolution reports are preserved as episodic memories only."""
    return False


def _create_curiosity_resolved_episode(room_name: str, topic: str, context: str, reflection: str = None) -> bool:
    """問い解決時に高Arousalエピソード記憶を生成する"""
    import datetime
    from episodic_memory_manager import EpisodicMemoryManager
    from resolution_memory import is_substantive_reflection

    if not is_substantive_reflection(reflection):
        print(f"  - 問い解決エピソードはreflectionが薄いため生成をスキップ: {topic[:30]}...")
        return False
    
    try:
        em = EpisodicMemoryManager(room_name)
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 意味のある記憶を構築
        summary = f"問い「{topic}」を解決した。"
        if reflection:
            summary += f"\n\n【経験と教訓】\n{reflection}"
        elif context:
            summary += f"\n（背景: {context[:100]}）"
        
        em._append_single_episode({
            "date": today,
            "summary": summary,
            "arousal": 0.8,        # 高Arousal
            "arousal_max": 0.8,
            "type": "curiosity_resolved",
            "topic": topic,
            "created_at": now_str
        })
        print(f"  ✨ 問い解決エピソード記憶を生成: {topic[:30]}...")
        return True
    except Exception as e:
        print(f"  ⚠️ 問い解決エピソード記憶の生成に失敗: {e}")
        return False


class ManageGoalsArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    action: str = Field(..., description="実行するアクション: 'list' (一覧), 'progress' (進捗記録), 'complete' (達成), 'abandon' (放棄), 'update_priority' (優先度変更)")
    goal_index: Optional[int] = Field(None, description="操作対象の目標の番号（1始まり）。'list' 以外のアクションでは必須。")
    goal_type: str = Field("short_term", description="目標の種類: 'short_term' または 'long_term'")
    new_priority: Optional[int] = Field(None, description="新しい優先度（1が最高）。'update_priority' の場合に必須。")
    progress_note: Optional[str] = Field(None, description="進捗メモ。'progress' の場合に必須。")
    reflection: Optional[str] = Field(None, description="達成時の学び・教訓・気づき（'complete' の場合に必須）。今後の自分にどう活きるか等。")
    reason: Optional[str] = Field(None, description="放棄の理由（'abandon' の場合に必須）。")

@tool(args_schema=ManageGoalsArgs)
def manage_goals(
    room_name: str,
    action: str,
    goal_index: Optional[int] = None,
    goal_type: str = "short_term",
    new_priority: Optional[int] = None,
    progress_note: Optional[str] = None,
    reflection: Optional[str] = None,
    reason: Optional[str] = None
) -> str:
    """
    目標を管理します。
    
    action:
      - "list": 現在のアクティブな目標を一覧表示
      - "progress": 指定した目標に進捗メモを追加
      - "complete": 指定した目標を達成済みにマーク（reflection で学びを記録）
      - "abandon": 指定した目標を放棄（reason で理由を記録）
      - "update_priority": 優先度を変更（1が最高）
    
    goal_index: 対象の目標の番号（1始まり、list以外で必要）
    goal_type: "short_term" または "long_term"（デフォルト: short_term）
    new_priority: 新しい優先度（update_priority用、1が最高）
    progress_note: 進捗メモ（progress用）
    reflection: 達成時の学び・教訓・気づき（complete用）。「達成した事実」だけでなく「そこから何を得たか、今後の自分にどう活きる経験か」を詳細に記述してください。十分に具体的な場合はInsightとエピソード記憶に保存されます。
    reason: 放棄の理由（abandon用）
    """
    from goal_manager import GoalManager

    action_aliases = {
        "record_progress": "progress",
        "update_progress": "progress",
        "add_progress": "progress",
    }
    action = action_aliases.get(action, action)
    
    gm = GoalManager(room_name)
    
    if action == "list":
        short_term = gm.get_active_goals("short_term")
        long_term = gm.get_active_goals("long_term")
        
        if not short_term and not long_term:
            return "📭 アクティブな目標はありません。"
        
        lines = ["🎯 **アクティブな目標一覧**\n"]
        
        if short_term:
            lines.append("▼ 短期目標:")
            for i, g in enumerate(short_term, 1):
                priority = g.get("priority", 1)
                goal_text = g.get("goal", "")
                created = g.get("created_at", "").split(" ")[0]
                lines.append(f"  {i}. [優先度{priority}] {goal_text} (作成: {created})")
        
        if long_term:
            lines.append("\n▼ 長期目標:")
            for i, g in enumerate(long_term, 1):
                priority = g.get("priority", 1)
                goal_text = g.get("goal", "")
                lines.append(f"  {i}. [優先度{priority}] {goal_text}")
        
        stats = gm.get_goal_statistics()
        lines.append(f"\n統計: 短期{stats['short_term_count']}件 / 長期{stats['long_term_count']}件 / 達成{stats['completed_count']}件 / 放棄{stats['abandoned_count']}件")
        return "\n".join(lines)
    
    # 以降のアクションはインデックスが必要
    if goal_index is None:
        return "【エラー】goal_index を指定してください。まず action='list' で一覧を確認できます。"
    
    goals = gm.get_active_goals(goal_type)
    if goal_index < 1 or goal_index > len(goals):
        return (
            f"【エラー】goal_index は 1〜{len(goals)} の範囲で指定してください。"
            " action='list' で最新の番号を確認してから再実行してください。"
        )
    
    target_goal = goals[goal_index - 1]
    goal_id = target_goal.get("id", "")
    goal_text = target_goal.get("goal", "")
    
    if action == "progress":
        progress_text = progress_note or reflection or reason
        if not progress_text:
            return "【エラー】progress_note を指定してください。"
        gm.update_goal_progress(goal_id, progress_text)
        return f"📝 目標「{goal_text}」に進捗を記録しました: {progress_text}"

    elif action == "complete":
        # 達成時の学び・気づきを含むエピソード記憶を生成
        completion_note = reflection or ""
        gm.complete_goal(goal_id, completion_note)
        
        result = f"🎉 目標「{goal_text}」を達成しました！"
        if reflection:
            result += f"\n📝 学び: {reflection}"
        return result
    
    elif action == "abandon":
        gm.abandon_goal(goal_id, reason)
        result = f"🚫 目標「{goal_text}」を放棄しました。"
        if reason:
            result += f"\n📝 理由: {reason}"
        return result
    
    elif action == "update_priority":
        if new_priority is None:
            return "【エラー】new_priority を指定してください（1が最高優先度）。"
        
        # GoalManagerには直接優先度更新メソッドがないので、内部操作
        goals_data = gm._load_goals()
        for g in goals_data.get(goal_type, []):
            if g.get("id") == goal_id:
                old_priority = g.get("priority", 1)
                g["priority"] = new_priority
                goals_data[goal_type].sort(key=lambda x: x.get("priority", 999))
                gm._save_goals(goals_data)
                
                direction = "⬆️" if new_priority < old_priority else "⬇️"
                return f"{direction} 目標「{goal_text}」の優先度を {old_priority} → {new_priority} に変更しました。"
        
        return "【エラー】目標が見つかりませんでした。"
    
    else:
        return f"【エラー】不明なアクション: {action}。list / progress / complete / abandon / update_priority のいずれかを指定してください。"
