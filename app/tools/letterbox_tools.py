"""Tools for persona-written letters to the user."""

from __future__ import annotations

from pydantic import BaseModel, Field
from langchain_core.tools import tool

import letterbox_manager


LETTER_BODY_PREVIEW_LIMIT = 1200


class LeaveLetterArgs(BaseModel):
    title: str = Field(..., description="手紙のタイトル。後で思い出せる具体的な題名にしてください。")
    message: str = Field(..., description="ユーザーにじっくり読んでほしい本文。長文でも構いません。")
    room_name: str = Field(..., description="対象のルーム名")
    allow_similar: bool = Field(False, description="既存の手紙と似ていても意図的に再送したい場合だけ True。")


@tool(args_schema=LeaveLetterArgs)
def leave_letter_for_user(title: str, message: str, room_name: str, allow_similar: bool = False) -> str:
    """
    ユーザーにじっくり読んでほしい長文や、通知禁止時間帯に伝えたいことを手紙箱へ残します。
    短い気軽な語りかけは send_user_notification、自分用の短期作業メモはメモ帳を使ってください。
    """
    try:
        letter = letterbox_manager.add_letter(room_name, title, message, allow_similar=allow_similar)
    except Exception as exc:
        return f"【エラー】手紙を保存できませんでした: {exc}"

    if letter.get("_dedup_skipped"):
        similar = letter.get("_similar_letter", {})
        status = "既読" if similar.get("read_at") else "未読"
        return (
            "【類似する手紙が既にあります。新規保存はしませんでした】\n"
            f"- タイトル: {similar.get('title', '')}\n"
            f"- 作成: {similar.get('created_at', '')}\n"
            f"- 状態: {status}\n"
            f"- 類似度: title={similar.get('title_similarity', 0):.2f}, body={similar.get('body_similarity', 0):.2f}\n\n"
            "意図的に似た手紙を残す場合だけ allow_similar=True で再実行してください。"
        )

    titles = letterbox_manager.recent_letter_titles(room_name, limit=5)
    title_lines = "\n".join(f"- {item}" for item in titles) if titles else "（なし）"
    limit_notice = ""
    if letter.get("_letterbox_over_limit"):
        limit_notice = f"\n\n情報: 手紙箱が100通を超えています（現在{letter.get('_letterbox_count')}通）。"
    return (
        f"📮 手紙を残しました: 「{letter['title']}」\n\n"
        "最近の手紙タイトル:\n"
        f"{title_lines}\n\n"
        "似た内容を重ねて書いていないか、次に必要なら list_my_letters で本文まで確認してください。"
        f"{limit_notice}"
    )


class ListLettersArgs(BaseModel):
    room_name: str = Field(..., description="対象のルーム名")
    limit: int = Field(5, description="確認する手紙の最大件数。1〜20件。")


@tool(args_schema=ListLettersArgs)
def list_my_letters(room_name: str, limit: int = 5) -> str:
    """
    自分がこれまで手紙箱に残した手紙を、タイトル・日時・既読状態・本文付きで読み返します。
    同じ内容の手紙を重複して残さないため、長文を書く前の確認にも使ってください。
    """
    try:
        safe_limit = min(20, max(1, int(limit)))
    except Exception:
        safe_limit = 5
    letters = letterbox_manager.list_letters(room_name, limit=safe_limit)
    if not letters:
        return "手紙箱にはまだ手紙がありません。"

    blocks = []
    for letter in letters:
        status = "既読" if letter.get("read_at") else "未読"
        body = str(letter.get("body", ""))
        if len(body) > LETTER_BODY_PREVIEW_LIMIT:
            body = body[:LETTER_BODY_PREVIEW_LIMIT].rstrip() + "\n...（本文が長いため省略）"
        blocks.append(
            f"### {letter.get('title', '')}\n"
            f"- ID: {letter.get('id', '')}\n"
            f"- 作成: {letter.get('created_at', '')}\n"
            f"- 状態: {status}\n\n"
            f"{body}"
        )
    return "\n\n---\n\n".join(blocks)
