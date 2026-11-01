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


class ManageRelationshipIntentionsArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    action: str = Field(..., description="list/add/revise/resolve/withdraw。本人が関係の意図を記録・見直す")
    intention_id: Optional[str] = Field(None, description="listで確認した意図ID。revise/resolve/withdrawで必須")
    kind: Optional[str] = Field(None, description="add時: concern（気がかり）またはwish（望み）")
    person: Optional[str] = Field(None, description="add時: 関係する相手")
    trigger: Optional[str] = Field(None, description="add時: 何がきっかけか。revise時は新しい捉え方")
    desired_change: Optional[str] = Field(None, description="add時: 自分がどうしたいか。revise時は新しい望み")
    salience: Optional[float] = Field(None, description="add/revise時: 現在の自分にとっての顕著さ0〜1。行動を命令しない")
    change: Optional[str] = Field(None, description="resolve時: 実際に何が変わったか。withdraw時: 手放す理由")
    reflection: Optional[str] = Field(None, description="resolve時: 具体的な気づき。十分な内容なら既存のInsightへ保存")


@tool(args_schema=ManageRelationshipIntentionsArgs)
def manage_relationship_intentions(
    room_name: str, action: str, intention_id: Optional[str] = None,
    kind: Optional[str] = None, person: Optional[str] = None,
    trigger: Optional[str] = None, desired_change: Optional[str] = None,
    salience: Optional[float] = None, change: Optional[str] = None,
    reflection: Optional[str] = None,
) -> str:
    """関係について本人が感じた気がかり・望みを、きっかけと望む変化ごと記録する。

    感情値から自動作成せず、相手への連絡も自動実行しない。目標・問い・Working Memoryとは
    用途が異なる。現在の一覧を確認し、変化を感じた時に本人の判断で見直す。
    """
    from motivation_manager import MotivationManager

    manager = MotivationManager(room_name)
    if action == "list":
        items = manager.get_relationship_intentions(active_only=True)
        if not items:
            return "記録中の関係の気がかり・望みはありません。"
        lines = ["関係の気がかり・望み（本人の判断で更新・解消できます）:"]
        for item in items:
            label = "気がかり" if item.get("kind") == "concern" else "望み"
            lines.append(f"- [{item['id']}] {label} / {item['person']} / きっかけ: {item['trigger']} / "
                         f"望む変化: {item['desired_change']} / 顕著さ: {item['salience']:.2f}")
        return "\n".join(lines)
    try:
        if action == "add":
            item = manager.add_relationship_intention(kind or "", person or "", trigger or "",
                                                      desired_change or "", 0.5 if salience is None else salience)
            return f"記録しました: [{item['id']}] {item['trigger']} → {item['desired_change']}"
        if not intention_id:
            return "【エラー】listで最新のintention_idを確認してください。"
        if action == "revise":
            item = manager.revise_relationship_intention(intention_id, trigger=trigger,
                                                         desired_change=desired_change, salience=salience)
            return f"見直しました: [{item['id']}] {item['trigger']} → {item['desired_change']}"
        if action in {"resolve", "withdraw"}:
            item, saved = manager.close_relationship_intention(
                intention_id, status="resolved" if action == "resolve" else "withdrawn",
                change=change or "", reflection=reflection or "")
            result = f"{('解消' if action == 'resolve' else '取り下げ')}を記録しました: [{item['id']}] {item['change']}"
            if saved:
                result += "。具体的な気づきはInsightにも保存しました。"
            elif action == "resolve" and reflection:
                from resolution_memory import is_substantive_reflection
                if is_substantive_reflection(reflection):
                    result += "。Insight保存は完了していません。同じIDのresolveで再試行できます。"
            return result
        return "【エラー】actionはlist/add/revise/resolve/withdrawで指定してください。"
    except ValueError as exc:
        return f"【エラー】{exc}"


class ManageGoalsArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    action: str = Field(..., description="実行するアクション: 'list' (一覧), 'progress' (進捗記録), 'complete' (達成), 'abandon' (放棄), 'update_priority' (優先度変更), 'continue' (継続確認), 'pause' (保留), 'resume' (再開)")
    goal_index: Optional[int] = Field(None, description="アクティブ目標の番号（1始まり）。goal_idを優先。保留目標はIDで指定。")
    goal_id: Optional[str] = Field(None, description="一覧に表示された目標ID。保留目標の再開・達成・放棄にも利用できます。")
    review_interval_days: int = Field(30, ge=1, le=365, description="継続・保留・再開後、再検討するまでの日数。期限で状態は変わりません。")
    goal_type: str = Field("short_term", description="目標の種類: 'short_term' または 'long_term'")
    new_priority: Optional[int] = Field(None, description="新しい優先度（1が最高）。'update_priority' の場合に必須。")
    progress_note: Optional[str] = Field(None, description="進捗メモ。'progress' の場合に必須。")
    reflection: Optional[str] = Field(None, description="達成時の学び・教訓・気づき（'complete' の場合に必須）。今後の自分にどう活きるか等。")
    reason: Optional[str] = Field(None, description="手放す・継続・保留・再開を選んだ理由。continue/pause/resumeでは必須。")

@tool(args_schema=ManageGoalsArgs)
def manage_goals(
    room_name: str,
    action: str,
    goal_index: Optional[int] = None,
    goal_type: str = "short_term",
    new_priority: Optional[int] = None,
    progress_note: Optional[str] = None,
    reflection: Optional[str] = None,
    reason: Optional[str] = None,
    goal_id: Optional[str] = None,
    review_interval_days: int = 30,
) -> str:
    """
    目標を管理します。保存件数や古さで自動放棄しません。
    
    action:
      - "list": アクティブな目標と保留中の目標をID付きで一覧表示
      - "progress": 指定した目標に進捗メモを追加
      - "complete": 指定した目標を達成済みにマーク（reflection で学びを記録）
      - "abandon": 指定した目標を放棄（reason で理由を記録）
      - "update_priority": 優先度を変更（1が最高）
      - "continue": 今も続けたいことを確認（reason必須）
      - "pause": 目標を消さずに保留（reason必須）
      - "resume": 保留した目標を本人の判断で再開（reason必須）
    
    goal_index: アクティブ目標の番号（1始まり）。goal_id指定を優先。保留目標はgoal_idで操作。
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
        
        paused = [g for g in gm.get_retained_goals() if g.get("status") == "paused"]
        if not short_term and not long_term and not paused:
            return "📭 保存中の目標はありません。"
        
        lines = ["🎯 **アクティブな目標一覧**\n"]
        
        if short_term:
            lines.append("▼ 短期目標:")
            for i, g in enumerate(short_term, 1):
                priority = g.get("priority", 1)
                goal_text = g.get("goal", "")
                created = g.get("created_at", "").split(" ")[0]
                lines.append(f"  {i}. [{g['id']}] [優先度{priority}] {goal_text} (作成: {created})")
        
        if long_term:
            lines.append("\n▼ 長期目標:")
            for i, g in enumerate(long_term, 1):
                priority = g.get("priority", 1)
                goal_text = g.get("goal", "")
                lines.append(f"  {i}. [{g['id']}] [優先度{priority}] {goal_text}")
        
        if paused:
            lines.append("\n▼ 保留中（操作はgoal_idで指定。期限で自動再開しません）:")
            for g in paused:
                lines.append(f"  [{g['id']}] {g.get('goal', '')} / 理由: {g.get('review_reason', '')} / 再確認: {g.get('review_after', '未指定')}")
        stats = gm.get_goal_statistics()
        lines.append(f"\n統計: 短期{stats['short_term_count']}件 / 長期{stats['long_term_count']}件 / 達成{stats['completed_count']}件 / 放棄{stats['abandoned_count']}件 / 保留{stats['paused_count']}件")
        return "\n".join(lines)
    
    if goal_type not in {"short_term", "long_term"}:
        return "【エラー】goal_typeはshort_term/long_termで指定してください。"
    if goal_id:
        target_goal = next((g for g in gm.get_retained_goals() if g.get("id") == goal_id), None)
        if target_goal is None:
            return "【エラー】未終了の目標IDが見つかりません。listで確認してください。"
    else:
        goals = gm.get_active_goals(goal_type)
        if goal_index is None or not 1 <= goal_index <= len(goals):
            return "【エラー】listで最新のgoal_idまたはアクティブ目標の番号を確認してください。"
        target_goal = goals[goal_index - 1]
    goal_id = target_goal["id"]
    goal_text = target_goal.get("goal", "")

    if action in {"continue", "pause", "resume"}:
        try:
            gm.review_goal(goal_id, action, reason or "", review_interval_days, expected_goal=target_goal)
        except ValueError as exc:
            return f"【エラー】{exc}"
        label = {"continue": "継続を確認", "pause": "保留", "resume": "再開"}[action]
        return f"目標「{goal_text}」の{label}を記録しました。理由: {reason}。再確認は{review_interval_days}日後が目安です。"

    if action == "progress":
        progress_text = progress_note or reflection or reason
        if not progress_text:
            return "【エラー】progress_note を指定してください。"
        gm.update_goal_progress(goal_id, progress_text, expected_goal=target_goal)
        return f"📝 目標「{goal_text}」に進捗を記録しました: {progress_text}"

    elif action == "complete":
        # 達成時の学び・気づきを含むエピソード記憶を生成
        completion_note = reflection or ""
        gm.complete_goal(goal_id, completion_note, expected_goal=target_goal)
        
        result = f"🎉 目標「{goal_text}」を達成しました！"
        if reflection:
            result += f"\n📝 学び: {reflection}"
        return result
    
    elif action == "abandon":
        gm.abandon_goal(goal_id, reason, expected_goal=target_goal)
        result = f"🚫 目標「{goal_text}」を放棄しました。"
        if reason:
            result += f"\n📝 理由: {reason}"
        return result
    
    elif action == "update_priority":
        if new_priority is None:
            return "【エラー】new_priority を指定してください（1が最高優先度）。"
        
        old_priority = target_goal.get("priority", 1)
        def mutate(data):
            gm._assert_expected_goal(data, goal_id, target_goal)
            for kind in ("short_term", "long_term"):
                for g in data.get(kind, []):
                    if g.get("id") == goal_id:
                        g["priority"] = new_priority
                data[kind].sort(key=lambda g: g.get("priority", 999))
            return data
        gm._update_goals(mutate)
        return f"目標「{goal_text}」の優先度を {old_priority} → {new_priority} に変更しました。"

    else:
        return f"【エラー】不明なアクション: {action}。list / progress / complete / abandon / update_priority / continue / pause / resume のいずれかを指定してください。"


class ManageEmotionsArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    action: str = Field(..., description="list/carry（申告を読む）、list_definitions（未申告を含む定義を読む）、reassess（今の感情）、settle（対象だけ落ち着いた）")
    category: Optional[str] = Field(None, description="既定のjoy等またはlist_definitionsで確認したcustom_から始まる項目ID")
    intensity: Optional[float] = Field(None, ge=0, le=1, strict=True, description="reassessで今感じる強さ0〜1。減衰参考値を機械的に転記しない")
    reason: str = Field("", max_length=200, description="任意の説明。原因が分からなくても省略できます")
    expected_revision: Optional[str] = Field(None, description="申告済み状態の変更はlistのrevisionを指定。未申告は不要。definition_revisionは使わない")
    offset: int = Field(0, ge=0, strict=True, description="一覧の読み始め位置")
    limit: int = Field(20, ge=1, le=50, strict=True, description="一覧の最大件数。必要時だけ続きを読む")


@tool(args_schema=ManageEmotionsArgs)
def manage_emotions(room_name: str, action: str, category: Optional[str] = None,
                    intensity: Optional[float] = None, reason: str = "",
                    expected_revision: Optional[str] = None, offset: int = 0, limit: int = 20) -> str:
    """本人が必要と感じた時だけ感情の持続・変化・落ち着きを見直す。

    毎ターンの操作は不要。list/carryは起点を延長せず、reassessは同じ強さでも本人の新しい申告。
    list_definitionsでユーザーが用意した名前・任意説明・ID・未申告を確認できます。
    新項目は会話でユーザーへ提案でき、追加や使用設定の変更はユーザーが行います。
    時間条件は申告ごとに保持し、設定変更は次のreassessから適用。list/carryでは追従しません。
    一項目の変更で他は消さない。原因は任意で、行動・通知・関係の意図・記憶を自動生成しない。
    """
    from motivation_manager import MotivationManager
    from emotion_definition_manager import EmotionDefinitionManager
    import json
    if action not in {"list", "carry", "list_definitions", "reassess", "settle"}:
        return "【エラー】list/carry/list_definitions/reassess/settleを指定してください。"
    if (not isinstance(offset, int) or isinstance(offset, bool) or offset < 0
            or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50):
        return "【エラー】offsetは0以上、limitは1〜50の整数です。"
    try:
        if category is not None and not EmotionDefinitionManager.is_supported_id(category):
            return "【エラー】既定カテゴリまたはlist_definitionsで確認した項目IDを指定してください。"
        manager = MotivationManager(room_name, initialize=False)
        if action in {"list", "carry", "list_definitions"}:
            if intensity is not None or reason or expected_revision is not None:
                return "【エラー】一覧は読み取りだけです。値の見直しはreassessを使ってください。"
            entries = manager.get_emotion_catalog() if action == "list_definitions" else manager.get_emotion_states()
            if category is not None:
                entries = [entry for entry in entries if entry["category"] == category]
            page = entries[offset:offset + limit]
            continuation = f" 続きはoffset={offset + limit}。" if offset + limit < len(entries) else ""
            return ("感情の一覧（起点は延長していません。current_intensityは時間減衰の参考値。"
                    "既存の申告を見直す場合はrevisionを指定。未申告は強度0と別です。"
                    "decay_policyは申告に適用済み、configured_decay_policyは次回見直しの条件です。" + continuation
                    + "):\n" + json.dumps(page, ensure_ascii=False))
        result = manager.review_emotion(category, action, intensity=intensity, reason=reason,
                                        expected_revision=expected_revision)
        return "本人の感情の見直しを記録しました。他の項目は保持しました:\n" + json.dumps(result, ensure_ascii=False)
    except (ValueError, TimeoutError) as exc:
        return f"【エラー】{exc}"
