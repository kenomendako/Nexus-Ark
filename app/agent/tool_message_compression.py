"""Utilities for trimming large tool results before the next LLM call."""

from __future__ import annotations

from typing import Iterable

from langchain_core.messages import BaseMessage, ToolMessage


LONG_TOOL_MESSAGE_THRESHOLD = 1800
LONG_TOOL_MESSAGE_HEAD = 900
LONG_TOOL_MESSAGE_TAIL = 500

COMPRESSIBLE_TOOL_RESULT_NAMES = {
    "read_entity_memory",
    "search_entity_memory",
    "list_entity_memories",
    "recall_memories",
    "search_past_conversations",
    "read_memory_context",
    "read_identity_memory",
    "read_diary_memory",
    "read_secret_diary",
    "read_full_notepad",
    "read_creative_notes",
    "read_research_notes",
    "read_research_thread",
    "find_similar_research_threads",
    "read_working_memory",
    "list_working_memories",
    "read_autonomy_context",
    "read_current_plan",
    "read_purpose_profile",
    "list_procedures",
    "read_procedure",
    "read_capability_policy",
    "list_available_locations",
    "read_world_settings",
    "search_knowledge_base",
    "web_search_tool",
    "read_url_tool",
    "list_project_files",
    "read_project_file",
}


def _string_content(content: object) -> str | None:
    if isinstance(content, str):
        return content
    return None


def make_tool_result_excerpt(tool_name: str, content: str) -> str:
    head = content[:LONG_TOOL_MESSAGE_HEAD].rstrip()
    tail = content[-LONG_TOOL_MESSAGE_TAIL:].lstrip()
    omitted = max(0, len(content) - len(head) - len(tail))

    return (
        "【長大なツール結果の次思考用圧縮】\n"
        f"tool: {tool_name}\n"
        f"original_chars: {len(content)}\n"
        f"omitted_chars: {omitted}\n"
        "note: 実行結果は長いため、次の思考入力では抜粋だけを保持しています。"
        "必要なら対応する read/search/list ツールで再取得してください。\n\n"
        "--- 先頭抜粋 ---\n"
        f"{head}\n\n"
        "--- 末尾抜粋 ---\n"
        f"{tail}"
    )


def _copy_tool_message_with_content(message: ToolMessage, content: str) -> ToolMessage:
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"content": content})
    if hasattr(message, "copy"):
        return message.copy(update={"content": content})
    return ToolMessage(
        content=content,
        tool_call_id=getattr(message, "tool_call_id", None),
        name=getattr(message, "name", None),
    )


def compress_tool_messages_for_agent(
    messages: Iterable[BaseMessage],
) -> tuple[list[BaseMessage], int, int, int]:
    """Return LLM-facing messages with long read/search tool results excerpted.

    The original message objects are not mutated. Only tools that primarily
    return reference material are compressed; writing/posting tools are left
    intact so persona-authored text stays available verbatim.
    """

    compressed_messages: list[BaseMessage] = []
    compressed_count = 0
    original_chars = 0
    compressed_chars = 0

    for message in messages:
        if not isinstance(message, ToolMessage):
            compressed_messages.append(message)
            continue

        tool_name = getattr(message, "name", "") or ""
        content = _string_content(message.content)
        if (
            tool_name not in COMPRESSIBLE_TOOL_RESULT_NAMES
            or content is None
            or len(content) <= LONG_TOOL_MESSAGE_THRESHOLD
        ):
            compressed_messages.append(message)
            continue

        excerpt = make_tool_result_excerpt(tool_name, content)
        compressed_messages.append(_copy_tool_message_with_content(message, excerpt))
        compressed_count += 1
        original_chars += len(content)
        compressed_chars += len(excerpt)

    return compressed_messages, compressed_count, original_chars, compressed_chars
