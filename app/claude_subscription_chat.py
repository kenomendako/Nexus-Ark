"""[SEALED] Claude SDK経路は封印中（削除禁止）。ADR: docs/decisions/010_claude_sdk_path_sealed_not_deleted.md

LangChain ChatModel wrapper for Claude Agent SDK subscription usage.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, Callable

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

import claude_subscription_auth


logger = logging.getLogger(__name__)

DISALLOWED_CLAUDE_CODE_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "WebFetch", "WebSearch"]
MAX_CLAUDE_SUBSCRIPTION_PROMPT_CHARS = 2_000_000
UNKNOWN_CONTENT_TEXT_LIMIT = 2_000
DATA_URL_MIME_RE = re.compile(r"^data:([^;,]+)[;,]", re.IGNORECASE)

DEFAULT_CLAUDE_SUBSCRIPTION_MODELS = [
    {"displayName": "Default", "value": "default", "description": "Claude Code default model for this account."},
    {"displayName": "Sonnet", "value": "sonnet", "description": "Claude Sonnet subscription alias."},
    {"displayName": "Opus", "value": "opus", "description": "Claude Opus subscription alias."},
    {"displayName": "Haiku", "value": "haiku", "description": "Claude Haiku subscription alias, when available."},
]


class ClaudeSubscriptionError(RuntimeError):
    """Base error for Claude subscription provider failures."""


class ClaudeSubscriptionAuthError(ClaudeSubscriptionError):
    """Authentication failed or no valid Claude subscription credentials were usable."""


class ClaudeSubscriptionRateLimitError(ClaudeSubscriptionError):
    """Claude subscription credits or rate limits appear exhausted."""


class ClaudeSubscriptionPromptTooLongError(ClaudeSubscriptionError):
    """Prompt is too large for Claude Code to accept."""


class ClaudeSubscriptionCLIError(ClaudeSubscriptionError):
    """Claude Code CLI or SDK process failed before returning a structured result."""


class ClaudeSubscriptionUnknownError(ClaudeSubscriptionError):
    """The SDK returned an error result that could not be classified."""


class ChatClaudeSubscription(BaseChatModel):
    """Use Claude Agent SDK as a one-turn, tool-disabled chat model."""

    model_name: str = "sonnet"
    max_turns: int = 1
    timeout_seconds: int = 90
    _query_func: Callable[..., AsyncIterator[Any]] | None = PrivateAttr(default=None)

    def __init__(self, **kwargs: Any) -> None:
        query_func = kwargs.pop("query_func", None)
        super().__init__(**kwargs)
        self._query_func = query_func

    @property
    def _llm_type(self) -> str:
        return "claude_subscription"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name, "max_turns": self.max_turns}

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "ChatClaudeSubscription":
        if tools:
            logger.warning(
                "Claude subscription provider ignores %d bound tools; use agent delegation for tool tasks.",
                len(tools),
            )
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = _run_async_sync(self._run_query(messages, include_partial_messages=False))
        message = AIMessage(
            content=result["text"],
            response_metadata=result["metadata"],
        )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=message,
                    generation_info=result["metadata"],
                )
            ]
        )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        yield from _stream_async_sync(self._run_query_stream(messages), run_manager)

    async def _run_query(self, messages: list[BaseMessage], *, include_partial_messages: bool) -> dict[str, Any]:
        text_parts: list[str] = []
        result_message = None
        assistant_error = None

        try:
            async for event in self._iter_sdk_messages(messages, include_partial_messages=include_partial_messages):
                if _is_assistant_message(event):
                    assistant_error = getattr(event, "error", None) or assistant_error
                    text = _extract_assistant_text(event)
                    if text:
                        text_parts.append(text)
                elif _is_result_message(event):
                    result_message = event
        except Exception as exc:
            _raise_for_sdk_exception(exc, result_message, assistant_error)

        _raise_for_result_error(result_message, assistant_error)
        metadata = _metadata_from_result(result_message)
        if assistant_error:
            metadata["assistant_error"] = assistant_error
        return {"text": "".join(text_parts).strip(), "metadata": metadata}

    async def _run_query_stream(self, messages: list[BaseMessage]) -> AsyncIterator[ChatGenerationChunk]:
        assistant_text_parts: list[str] = []
        emitted_delta = False
        result_message = None
        assistant_error = None

        try:
            async for event in self._iter_sdk_messages(messages, include_partial_messages=True):
                if _is_stream_event(event):
                    delta = _extract_stream_text_delta(event)
                    if delta:
                        emitted_delta = True
                        yield ChatGenerationChunk(
                            message=AIMessageChunk(content=delta),
                            generation_info={"event": "delta"},
                        )
                elif _is_assistant_message(event):
                    assistant_error = getattr(event, "error", None) or assistant_error
                    text = _extract_assistant_text(event)
                    if text:
                        assistant_text_parts.append(text)
                elif _is_result_message(event):
                    result_message = event
        except Exception as exc:
            _raise_for_sdk_exception(exc, result_message, assistant_error)

        _raise_for_result_error(result_message, assistant_error)
        if not emitted_delta:
            text = "".join(assistant_text_parts).strip()
            if text:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content=text),
                    generation_info=_metadata_from_result(result_message),
                )
        else:
            yield ChatGenerationChunk(
                message=AIMessageChunk(content="", chunk_position="last"),
                generation_info=_metadata_from_result(result_message),
            )

    async def _iter_sdk_messages(
        self,
        messages: list[BaseMessage],
        *,
        include_partial_messages: bool,
    ) -> AsyncIterator[Any]:
        sdk = _import_claude_agent_sdk()
        query_func = self._query_func or sdk.query
        auth = claude_subscription_auth.resolve_claude_subscription_auth()
        prompt, system_prompt = serialize_messages_for_claude_subscription(messages)

        with tempfile.TemporaryDirectory(prefix="nexus-ark-claude-subscription-") as tmpdir:
            options = sdk.ClaudeAgentOptions(
                env=auth.env,
                system_prompt=system_prompt,
                setting_sources=[],
                tools=[],
                allowed_tools=[],
                disallowed_tools=DISALLOWED_CLAUDE_CODE_TOOLS,
                cwd=tmpdir,
                max_turns=self.max_turns,
                model=self.model_name,
                include_partial_messages=include_partial_messages,
                stderr=lambda line: logger.debug("[Claude Subscription stderr] %s", line),
            )
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    async for event in query_func(prompt=prompt, options=options):
                        yield event
            except TimeoutError as exc:
                raise ClaudeSubscriptionCLIError(
                    f"Claude subscription request timed out after {self.timeout_seconds} seconds."
                ) from exc
            except ClaudeSubscriptionError:
                raise
            except Exception:
                raise


def serialize_messages_for_claude_subscription(messages: list[BaseMessage]) -> tuple[str, str]:
    """Map LangChain messages to a one-shot prompt plus explicit system prompt."""
    system_parts: list[str] = []
    transcript_lines: list[str] = []

    for message in messages:
        content = _stringify_message_content(message.content)
        if isinstance(message, SystemMessage):
            if content:
                system_parts.append(content)
            continue
        if isinstance(message, HumanMessage):
            role = "User"
        elif isinstance(message, AIMessage):
            role = "Assistant"
        elif isinstance(message, ToolMessage):
            role = "Tool"
        else:
            role = message.type.title()
        if content:
            transcript_lines.append(f"{role}: {content}")

    prompt = "\n\n".join(transcript_lines).strip()
    if not prompt:
        prompt = "User: "
    system_prompt = "\n\n".join(system_parts).strip()
    _raise_if_serialized_prompt_too_large(prompt, system_prompt)
    return prompt, system_prompt


def test_claude_subscription_connection(
    token: str | None,
    model_name: str = "sonnet",
    timeout_seconds: int = 45,
    *,
    query_func: Callable[..., AsyncIterator[Any]] | None = None,
) -> dict[str, Any]:
    """Run a small live SDK query for the settings UI connection test."""
    async def _test() -> dict[str, Any]:
        sdk = _import_claude_agent_sdk()
        auth = claude_subscription_auth.resolve_claude_subscription_auth(
            {"claude_subscription_oauth_token": token or ""}
        )
        text_parts: list[str] = []
        result_message = None
        assistant_error = None
        token_is_present = bool((token or "").strip())
        with tempfile.TemporaryDirectory(prefix="nexus-ark-claude-subscription-test-") as tmpdir:
            if token_is_present:
                with tempfile.TemporaryDirectory(prefix="nexus-ark-claude-config-empty-") as config_dir:
                    env = dict(auth.env)
                    env["CLAUDE_CONFIG_DIR"] = config_dir
                    result_message, assistant_error = await _run_connection_test_query(
                        sdk,
                        query_func or sdk.query,
                        env,
                        tmpdir,
                        model_name,
                        timeout_seconds,
                        text_parts,
                    )
            else:
                result_message, assistant_error = await _run_connection_test_query(
                    sdk,
                    query_func or sdk.query,
                    auth.env,
                    tmpdir,
                    model_name,
                    timeout_seconds,
                    text_parts,
                )
        _raise_for_result_error(result_message, assistant_error)
        return {
            "ok": True,
            "auth_source": auth.source,
            "text": "".join(text_parts).strip(),
            "metadata": _metadata_from_result(result_message),
        }

    return _run_async_sync(_test())


test_claude_subscription_connection.__test__ = False


def fetch_claude_subscription_models(
    token: str | None = None,
    timeout_seconds: int = 45,
    *,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Fetch Claude subscription models exposed by Claude Code server info."""
    async def _fetch() -> dict[str, Any]:
        sdk = _import_claude_agent_sdk()
        auth = claude_subscription_auth.resolve_claude_subscription_auth(
            {"claude_subscription_oauth_token": token or ""}
        )
        token_is_present = bool((token or "").strip())
        with tempfile.TemporaryDirectory(prefix="nexus-ark-claude-models-") as tmpdir:
            if token_is_present:
                with tempfile.TemporaryDirectory(prefix="nexus-ark-claude-config-empty-") as config_dir:
                    env = dict(auth.env)
                    env["CLAUDE_CONFIG_DIR"] = config_dir
                    models = await _fetch_claude_subscription_models_async(
                        sdk,
                        client_factory or sdk.ClaudeSDKClient,
                        env,
                        tmpdir,
                        timeout_seconds,
                    )
            else:
                models = await _fetch_claude_subscription_models_async(
                    sdk,
                    client_factory or sdk.ClaudeSDKClient,
                    auth.env,
                    tmpdir,
                    timeout_seconds,
                )
        return {"models": models, "auth_source": auth.source}

    return _run_async_sync(_fetch())


async def _fetch_claude_subscription_models_async(
    sdk: Any,
    client_factory: Callable[..., Any],
    env: dict[str, str],
    cwd: str,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    options = sdk.ClaudeAgentOptions(
        env=env,
        setting_sources=[],
        tools=[],
        allowed_tools=[],
        disallowed_tools=DISALLOWED_CLAUDE_CODE_TOOLS,
        cwd=cwd,
        max_turns=1,
    )
    client = client_factory(options=options)
    try:
        async with asyncio.timeout(timeout_seconds):
            await client.connect()
            server_info = await client.get_server_info()
    except TimeoutError as exc:
        raise ClaudeSubscriptionCLIError(
            f"Claude subscription model fetch timed out after {timeout_seconds} seconds."
        ) from exc
    except Exception as exc:
        _raise_for_sdk_exception(exc)
    finally:
        disconnect = getattr(client, "disconnect", None)
        if disconnect:
            await disconnect()

    models = _extract_models_from_server_info(server_info)
    if not models:
        raise ClaudeSubscriptionUnknownError("Claude subscription server info did not include any models.")
    return models


def _extract_models_from_server_info(server_info: Any) -> list[dict[str, Any]]:
    if not isinstance(server_info, dict):
        return []
    raw_models = server_info.get("models")
    if not isinstance(raw_models, list):
        return []

    models: list[dict[str, Any]] = []
    seen_values: set[str] = set()
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        if not value or value in seen_values:
            continue
        display_name = str(item.get("displayName") or value).strip()
        description = str(item.get("description") or "").strip()
        models.append(
            {
                "displayName": display_name,
                "value": value,
                "description": description,
            }
        )
        seen_values.add(value)
    return models


async def _run_connection_test_query(
    sdk: Any,
    query_func: Callable[..., AsyncIterator[Any]],
    env: dict[str, str],
    cwd: str,
    model_name: str,
    timeout_seconds: int,
    text_parts: list[str],
) -> tuple[Any, str | None]:
    result_message = None
    assistant_error = None
    options = sdk.ClaudeAgentOptions(
        env=env,
        system_prompt="Reply with exactly: OK",
        setting_sources=[],
        tools=[],
        allowed_tools=[],
        disallowed_tools=DISALLOWED_CLAUDE_CODE_TOOLS,
        cwd=cwd,
        max_turns=1,
        model=model_name or "sonnet",
        include_partial_messages=False,
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            async for event in query_func(prompt="Reply with exactly: OK", options=options):
                if _is_assistant_message(event):
                    assistant_error = getattr(event, "error", None) or assistant_error
                    text_parts.append(_extract_assistant_text(event))
                elif _is_result_message(event):
                    result_message = event
    except Exception as exc:
        _raise_for_sdk_exception(exc, result_message, assistant_error)
    return result_message, assistant_error


def _run_async_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_queue: queue.Queue[Any] = queue.Queue(maxsize=1)

    def runner() -> None:
        try:
            result_queue.put(("ok", asyncio.run(coro)))
        except BaseException as exc:  # noqa: BLE001 - propagate across thread boundary
            result_queue.put(("error", exc))

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    status, value = result_queue.get()
    if status == "error":
        raise value
    return value


def _stream_async_sync(
    async_iter: AsyncIterator[ChatGenerationChunk],
    run_manager: CallbackManagerForLLMRun | None,
) -> Iterator[ChatGenerationChunk]:
    sentinel = object()
    result_queue: queue.Queue[Any] = queue.Queue()

    async def consume() -> None:
        try:
            async for chunk in async_iter:
                result_queue.put(("chunk", chunk))
        except BaseException as exc:  # noqa: BLE001 - propagate across thread boundary
            result_queue.put(("error", exc))
        finally:
            result_queue.put(("done", sentinel))

    thread = threading.Thread(target=lambda: asyncio.run(consume()), daemon=True)
    thread.start()
    while True:
        status, value = result_queue.get()
        if status == "chunk":
            if run_manager and value.text:
                run_manager.on_llm_new_token(value.text, chunk=value)
            yield value
        elif status == "error":
            raise value
        elif status == "done":
            break
    thread.join()


def _import_claude_agent_sdk() -> Any:
    try:
        import claude_agent_sdk
    except ImportError as exc:
        raise ClaudeSubscriptionCLIError("claude-agent-sdk is not installed.") from exc
    return claude_agent_sdk


def _is_assistant_message(event: Any) -> bool:
    return type(event).__name__ == "AssistantMessage"


def _is_result_message(event: Any) -> bool:
    return type(event).__name__ == "ResultMessage"


def _is_stream_event(event: Any) -> bool:
    return type(event).__name__ == "StreamEvent"


def _extract_assistant_text(event: Any) -> str:
    parts: list[str] = []
    for block in getattr(event, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def _extract_stream_text_delta(event: Any) -> str:
    raw = getattr(event, "event", {}) or {}
    if raw.get("type") != "content_block_delta":
        return ""
    delta = raw.get("delta") or {}
    if delta.get("type") in {"text_delta", "input_json_delta"}:
        return delta.get("text") or delta.get("partial_json") or ""
    return ""


def _metadata_from_result(result_message: Any) -> dict[str, Any]:
    if result_message is None:
        return {}
    usage = getattr(result_message, "usage", None)
    return {
        "subtype": getattr(result_message, "subtype", None),
        "duration_ms": getattr(result_message, "duration_ms", None),
        "duration_api_ms": getattr(result_message, "duration_api_ms", None),
        "num_turns": getattr(result_message, "num_turns", None),
        "session_id": getattr(result_message, "session_id", None),
        "stop_reason": getattr(result_message, "stop_reason", None),
        "total_cost_usd": getattr(result_message, "total_cost_usd", None),
        "usage": usage,
        "token_usage": _token_usage_from_claude_usage(usage),
        "model_usage": getattr(result_message, "model_usage", None),
        "api_error_status": getattr(result_message, "api_error_status", None),
    }


def _token_usage_from_claude_usage(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    prompt_tokens = (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
    )
    completion_tokens = int(usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _raise_for_result_error(result_message: Any, assistant_error: str | None) -> None:
    if result_message is not None and getattr(result_message, "is_error", False):
        text = _result_error_text(result_message)
        status = getattr(result_message, "api_error_status", None)
        lowered = text.lower()
        if "prompt is too long" in lowered or "prompt too long" in lowered:
            raise ClaudeSubscriptionPromptTooLongError(text)
        if status == 429 or any(term in lowered for term in ("rate limit", "usage limit", "credit", "quota", "overage")):
            raise ClaudeSubscriptionRateLimitError(text)
        if any(term in lowered for term in ("invalid api key", "authentication", "auth", "oauth", "login", "unauthorized")):
            raise ClaudeSubscriptionAuthError(text)
        if any(term in lowered for term in ("claude code", "cli", "connection", "not found", "unable to connect")):
            raise ClaudeSubscriptionCLIError(text)
        raise ClaudeSubscriptionUnknownError(text)

    if assistant_error:
        if assistant_error == "authentication_failed":
            raise ClaudeSubscriptionAuthError("Claude subscription authentication failed.")
        if assistant_error in {"rate_limit", "billing_error"}:
            raise ClaudeSubscriptionRateLimitError(f"Claude subscription error: {assistant_error}")
        raise ClaudeSubscriptionUnknownError(f"Claude subscription error: {assistant_error}")


def _raise_for_sdk_exception(
    exc: Exception,
    result_message: Any | None = None,
    assistant_error: str | None = None,
) -> None:
    if isinstance(exc, ClaudeSubscriptionError):
        if result_message is not None or assistant_error:
            _raise_for_result_error(result_message, assistant_error)
        raise exc

    if result_message is not None or assistant_error:
        _raise_for_result_error(result_message, assistant_error)

    text = str(exc)
    lowered = text.lower()
    if "prompt is too long" in lowered or "prompt too long" in lowered:
        raise ClaudeSubscriptionPromptTooLongError(text) from exc
    if any(term in lowered for term in ("invalid api key", "authentication", "oauth", "unauthorized", "login")):
        raise ClaudeSubscriptionAuthError(
            _safe_sdk_exception_message(text, "Claude subscription authentication failed.")
        ) from exc
    if "error result: success" in lowered:
        raise ClaudeSubscriptionUnknownError(text or "Claude Code returned an unclassified error result.") from exc
    if any(term in lowered for term in ("rate limit", "usage limit", "credit", "quota", "overage")):
        raise ClaudeSubscriptionRateLimitError(
            _safe_sdk_exception_message(text, "Claude subscription credits or rate limit may be exhausted.")
        ) from exc
    raise ClaudeSubscriptionCLIError(
        f"Claude Agent SDK call failed: {_safe_sdk_exception_message(text, 'SDK request failed.')}"
    ) from exc


def _safe_sdk_exception_message(raw_message: str, fallback: str) -> str:
    return raw_message or fallback


def _result_error_text(result_message: Any) -> str:
    parts: list[str] = []
    result = getattr(result_message, "result", None)
    if result:
        parts.append(str(result))
    errors = getattr(result_message, "errors", None)
    if errors:
        parts.extend(str(error) for error in errors)
    if not parts:
        parts.append("Claude subscription provider returned an error result.")
    return "\n".join(parts)


def _stringify_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item["text"]))
                else:
                    parts.append(_non_text_content_placeholder(item))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content)


def _raise_if_serialized_prompt_too_large(prompt: str, system_prompt: str) -> None:
    total_chars = len(prompt) + len(system_prompt)
    if total_chars > MAX_CLAUDE_SUBSCRIPTION_PROMPT_CHARS:
        raise ClaudeSubscriptionPromptTooLongError(
            "Claude subscription prompt is too long after safe serialization "
            f"({total_chars:,} chars / limit {MAX_CLAUDE_SUBSCRIPTION_PROMPT_CHARS:,}). "
            "添付や長大な履歴を減らしてから再試行してください。"
        )


def _non_text_content_placeholder(item: dict[str, Any]) -> str:
    block_type = str(item.get("type") or "").strip().lower()
    mime = _extract_mime_from_content_block(item)

    if block_type == "image_url":
        return f"[画像: {mime}]" if mime else "[画像]"
    if block_type == "file" or str(item.get("source_type") or "").strip().lower() == "base64":
        return f"[添付ファイル: {mime}]" if mime else "[添付ファイル]"
    if _contains_binary_payload(item):
        return f"[添付ファイル: {mime}]" if mime else "[添付ファイル]"
    if _estimated_text_size(item) > UNKNOWN_CONTENT_TEXT_LIMIT:
        return "[非テキストコンテンツ]"
    return "[非テキストコンテンツ]"


def _extract_mime_from_content_block(item: dict[str, Any]) -> str | None:
    for key in ("mime_type", "media_type", "content_type"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    image_url = item.get("image_url")
    if isinstance(image_url, dict):
        for key in ("mime_type", "media_type", "content_type"):
            value = image_url.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        url = image_url.get("url")
        if isinstance(url, str):
            mime = _mime_from_data_url(url)
            if mime:
                return mime
    elif isinstance(image_url, str):
        mime = _mime_from_data_url(image_url)
        if mime:
            return mime

    source = item.get("source")
    if isinstance(source, dict):
        for key in ("mime_type", "media_type", "content_type"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    url = item.get("url")
    if isinstance(url, str):
        return _mime_from_data_url(url)
    return None


def _mime_from_data_url(value: str) -> str | None:
    match = DATA_URL_MIME_RE.match(value.strip())
    if match:
        return match.group(1)
    return None


def _contains_binary_payload(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_mime_from_data_url(value))
    if isinstance(value, list):
        return any(_contains_binary_payload(item) for item in value)
    if isinstance(value, dict):
        if "data" in value:
            return True
        return any(_contains_binary_payload(item) for item in value.values())
    return False


def _estimated_text_size(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_estimated_text_size(item) for item in value)
    if isinstance(value, dict):
        return sum(len(str(key)) + _estimated_text_size(item) for key, item in value.items())
    return len(str(value))
