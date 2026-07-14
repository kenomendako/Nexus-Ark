"""Tools for inspecting room-local persona contracts."""

from __future__ import annotations

from langchain_core.tools import tool

import persona_contract


@tool
def read_persona_contract(room_name: str) -> str:
    """現在ルームの Persona Contract を読みます。

    サブエージェントへ依頼する前、成果物の文言を確認する前、呼び名・固有語・口調ルールを確認したい時に使います。
    Persona Contract はルーム固有・ユーザー環境固有の情報を含み得るため、共有サンプルや汎用テンプレートへコピーしないでください。

    room_name: 実行中のルーム名。システムが自動で補完します。
    """
    try:
        block = persona_contract.format_contract_for_delegation(room_name)
        if block:
            return block
        return (
            "このルームには有効な Persona Contract がありません。\n"
            "必要なら、ユーザーに確認したうえで room_config.json の override_settings.persona_contract へ保存する案を提案してください。"
        )
    except Exception as exc:
        return f"【Persona Contract 読み取りエラー】{type(exc).__name__}: {exc}"


@tool
def check_text_against_persona_contract(room_name: str, text: str) -> str:
    """任意の文章が現在ルームの Persona Contract に反していないか機械チェックします。

    UI文言、エラーメッセージ、サブエージェント成果物の要約、ユーザーに見せる文章を貼り付けて確認します。
    これは保存や契約変更を行わない読み取り専用チェックです。

    room_name: 実行中のルーム名。システムが自動で補完します。
    text: チェック対象の文章。
    """
    try:
        result = persona_contract.validate_text_against_contract(text, room_name)
        return persona_contract.format_validation_result(result)
    except Exception as exc:
        return f"【Persona Contract チェックエラー】{type(exc).__name__}: {exc}"
