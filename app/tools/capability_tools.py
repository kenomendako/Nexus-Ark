import json

from langchain_core.tools import tool


@tool
def request_capability(category: str, intent: str, details: str = "") -> str:
    """
    必要な能力カテゴリをシステムに要求します。

    category: 使用したい能力カテゴリ。
      world, memory, notes, web, image, time, autonomy, music, watchlist, items,
      chess, developer, roblox, twitter, discord, custom のいずれか。
      場所移動・現在地変更は world を使います（location/place/space 等の別名もworld扱い）。
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
    return (
        "【能力要求を受け付けました】\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        "次の思考ステップで、このカテゴリに属する実ツールだけが提示されます。"
        "必要な実ツールを無言で呼び出してください。"
        "外部副作用や高リスク操作は、別途Capability承認確認が必要です。"
    )
