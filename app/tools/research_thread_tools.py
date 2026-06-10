from typing import List, Optional

from langchain_core.tools import tool

from research_thread_manager import ResearchThreadManager


@tool
def list_research_threads(room_name: str, status: str = "active") -> str:
    """
    継続研究スレッドの一覧を取得する。
    研究ノートに書く前に、既存テーマへ接続できるか確認するために使う。
    """
    try:
        manager = ResearchThreadManager(room_name)
        threads = manager.list_threads(status=status if status != "all" else "")
        if not threads:
            return "【Research Threadsはまだありません】"
        lines = ["Research Threads:"]
        for thread in threads:
            lines.append(
                f"- {thread.get('thread_id')}: {thread.get('title')} "
                f"(status={thread.get('status')}, priority={thread.get('priority')}, "
                f"last={thread.get('last_deepened_at') or thread.get('updated_at')})"
            )
            if thread.get("next_action"):
                lines.append(f"  next_action: {thread.get('next_action')}")
        return "\n".join(lines)
    except Exception as e:
        return f"【エラー】Research Threads一覧の取得に失敗しました: {e}"


@tool
def read_research_thread(room_name: str, thread_id: str) -> str:
    """
    指定した継続研究スレッド本文を読む。
    DEEPEN / CONTINUE / CONTRADICT として研究ノートを書く前に使用する。
    """
    try:
        return ResearchThreadManager(room_name).read_thread(thread_id)
    except Exception as e:
        return f"【エラー】Research Threadの読み込みに失敗しました: {e}"


@tool
def find_similar_research_threads(room_name: str, query: str, limit: int = 5) -> str:
    """
    クエリに類似する既存Research Threadを探す。
    類似スレッドがある場合、研究ノートはNEWではなくDEEPEN/CONTINUEを優先する。
    """
    try:
        manager = ResearchThreadManager(room_name)
        matches = manager.find_similar_threads(query=query, limit=limit)
        if not matches:
            return "【類似Research Threadは見つかりませんでした】"
        lines = ["類似Research Threads:"]
        for thread in matches:
            lines.append(
                f"- {thread.get('thread_id')}: {thread.get('title')} "
                f"(score={thread.get('match_score')}, priority={thread.get('priority')})"
            )
            if thread.get("next_action"):
                lines.append(f"  next_action: {thread.get('next_action')}")
        return "\n".join(lines)
    except Exception as e:
        return f"【エラー】類似Research Thread検索に失敗しました: {e}"


@tool
def update_research_thread(
    room_name: str,
    thread_id: str,
    title: str = "",
    status: str = "active",
    priority: float = 0.5,
    working_memory_slot: str = "",
    related_entities: Optional[List[str]] = None,
    open_questions: Optional[List[str]] = None,
    next_action: str = "",
) -> str:
    """
    Research Threadのメタデータを作成・更新する。
    新しい継続研究テーマを作る時、または次回行動や未解決問いを更新する時に使う。
    """
    try:
        manager = ResearchThreadManager(room_name)
        thread = manager.create_or_update_thread(
            thread_id=thread_id,
            title=title,
            status=status,
            priority=priority,
            working_memory_slot=working_memory_slot,
            related_entities=related_entities,
            open_questions=open_questions,
            next_action=next_action,
        )
        return f"成功: Research Thread '{thread.get('thread_id')}' を更新しました。"
    except Exception as e:
        return f"【エラー】Research Threadの更新に失敗しました: {e}"
