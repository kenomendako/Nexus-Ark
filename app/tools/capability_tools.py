import json

from langchain_core.tools import tool


@tool
def request_capability(category: str, intent: str, details: str = "") -> str:
    """
    必要な能力カテゴリをシステムに要求します。

    category: 使用したい能力カテゴリ。
      world, memory, diary, identity_memory, notes, creative, research, working_memory, web, knowledge, image, time,
      autonomy, procedure, questions, agent_delegation, music, watchlist, items, outreach, chess, developer, roblox,
      twitter, discord, calendar, calendar_write, custom のいずれか。
      場所移動・現在地変更は world を使います（location/place/space 等の別名もworld扱い）。
      創作ノートは creative、研究ノートは research、Working Memory整理は working_memory を優先します。
      知識ベース検索は knowledge、手順記憶の確認・保存は procedure、ユーザーへの通知や手紙は outreach を使います。
      未解決の問いの確認・追加・解決や目標管理は questions を使います。
      予定・スケジュール・空き時間の確認は calendar を使います（schedule/予定/gcal 等の別名もcalendar扱い）。
      カレンダーへの予定登録（add_calendar_event）の承認確認には calendar_write を使います。
    intent: なぜその能力を使いたいか。
    details: 実行したい内容、判断材料、ユーザーに見せたい意図など。

    注: このツールは能力カテゴリを開くだけで、外部副作用の承認ではありません。
    Twitterの実投稿、Discord/Roblox/custom/外部投稿/PC操作/開発者系などは、
    実行前に capability policy/approval ツールで承認状態を確認してください。
    Twitter下書き作成（draft_tweet）は実投稿しないため、承認確認なしで実行できます。
    """
    payload = {
        "category": (category or "").strip().lower(),
        "intent": (intent or "").strip(),
        "details": (details or "").strip(),
    }
    tool_list_line = ""
    try:
        from agent.tool_registry import ToolRegistry

        tool_names = ToolRegistry([]).get_capability_tool_names(payload["category"])
        if tool_names:
            tool_list_line = (
                f"\n次ターンで付与されるツール: {', '.join(tool_names)}\n"
                "目的のツールがこの一覧に無い場合は、このターンのうちに別のカテゴリを要求し直してください。"
            )
    except Exception:
        tool_list_line = ""

    return (
        "【能力要求を受け付けました】\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        f"{tool_list_line}\n"
        "**あなたのターンはこのまま続いています。待つ必要も、ターンを終える必要もありません。**"
        "この直後にあなたは自動的に再び呼ばれ、要求したカテゴリの実ツールが提示されます。"
        "そのまま続けて、目的の実ツールを呼び出してください。\n"
        "※ 同じ内容で `request_capability` を繰り返さないでください。"
        "別カテゴリの実ツールも必要な場合のみ追加で要求できます（要求したカテゴリは併せて有効になります）。"
        "外部副作用や高リスク操作は、別途Capability承認確認が必要です。"
    )
