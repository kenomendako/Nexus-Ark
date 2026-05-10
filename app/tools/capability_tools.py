import json

from langchain_core.tools import tool


@tool
def request_capability(category: str, intent: str, details: str = "") -> str:
    """
    必要な能力カテゴリをシステムに要求します。

    category: 使用したい能力カテゴリ。
      world, memory, notes, web, image, time, autonomy, watchlist, items,
      chess, developer, roblox, twitter, custom のいずれか。
    intent: なぜその能力を使いたいか。
    details: 実行したい内容、判断材料、ユーザーに見せたい意図など。
    """
    payload = {
        "category": (category or "").strip().lower(),
        "intent": (intent or "").strip(),
        "details": (details or "").strip(),
    }
    return (
        "【能力要求を受け付けました】\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        "次の思考ステップで、このカテゴリに属する実ツールだけが提示されます。"
        "必要な実ツールを無言で呼び出してください。"
    )
