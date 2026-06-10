# tools/introspection_tools.py
"""
内省ツール - ペルソナが自律行動中に自身の内的状態を確認・編集できるツール群。
"""

from typing import Optional
from pydantic import BaseModel, Field
from langchain_core.tools import tool


class ManageOpenQuestionsArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    action: str = Field(..., description="実行するアクション: 'list' (一覧), 'add' (追加), 'resolve' (解決), 'remove' (削除), 'adjust_priority' (優先度変更)")
    question_index: Optional[int] = Field(None, description="操作対象の問いの番号（1始まり）。'list' 以外のアクションでは必須。")
    topic: Optional[str] = Field(None, description="'add' の場合に追加する問いのトピック。")
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
    
    question_index: 対象の問いの番号（1始まり、resolve/remove/adjust_priorityで必要）
    topic: 追加する問い（add用）
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

    if action == "add":
        if not topic or not topic.strip():
            return "【エラー】action='add' では topic を指定してください。"
        priority = 0.5 if new_priority is None else max(0.0, min(1.0, new_priority))
        mm.add_open_question(topic=topic.strip(), context=(context or "").strip(), priority=priority)
        return f"✅ 未解決の問いを追加しました: {topic.strip()} (priority={priority:.1f})"
    
    # 以降のアクションはインデックスが必要
    if question_index is None:
        return "【エラー】question_index を指定してください。まず action='list' で一覧を確認できます。"
    
    if question_index < 1 or question_index > len(unresolved):
        return f"【エラー】question_index は 1〜{len(unresolved)} の範囲で指定してください。"
    
    # UI番号から実際のインデックスを取得
    actual_idx, target_q = unresolved[question_index - 1]
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
