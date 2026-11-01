"""OpenRouter用Chat Completions adapter.

OpenRouterのChat Completionsは、推論モデルの応答に
``choices[].message.reasoning_details``（ストリームでは
``choices[].delta.reasoning_details``）を含めることがある。現行の
``langchain-openai``はこのprovider拡張フィールドを ``AIMessage`` へ移さないため、
この薄いadapterだけで受信・ツール往復の再送を補う。

Responses APIの変換やプロバイダ固有の推論解釈は担当しない。factory側で
OpenRouterの正確なhostかつ応答モデル用の場合だけ選択する。
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

from pydantic import PrivateAttr

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI


_REASONING_DETAILS_KEY = "reasoning_details"
_REASONING_CONTEXT_KEY = "_openrouter_reasoning_context"


def _copy_reasoning_details(value: Any) -> list[Any]:
    """APIのreasoning_detailsを、型を変えずにリストへ正規化する。"""

    if isinstance(value, list):
        return copy.deepcopy(value)
    if isinstance(value, tuple):
        return copy.deepcopy(list(value))
    if isinstance(value, Mapping):
        return [copy.deepcopy(dict(value))]
    return []


def _tool_call_ids(message: Any) -> list[str]:
    """LangChain messageのtool call順序を、再送検証用のID列へ変換する。"""

    calls = list(getattr(message, "tool_calls", None) or [])
    calls.extend(list(getattr(message, "invalid_tool_calls", None) or []))
    result: list[str] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        call_id = call.get("id")
        if call_id is not None and str(call_id):
            result.append(str(call_id))
    return result


def _chunk_tool_call_ids(message: Any) -> list[str]:
    """部分tool_call chunkから、すでに到着したIDを抽出する。"""

    chunks = list(getattr(message, "tool_call_chunks", None) or [])
    result: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        call_id = chunk.get("id")
        if call_id is not None and str(call_id) and str(call_id) not in result:
            result.append(str(call_id))
    if result:
        return result
    return _tool_call_ids(message)


def _append_unique_ids(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _response_choices(response: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """dict/Pydantic responseからmodelとchoicesを安全に取り出す。"""

    if isinstance(response, Mapping):
        response_dict = dict(response)
    elif hasattr(response, "model_dump"):
        response_dict = response.model_dump()
    else:
        response_dict = {}
    choices = response_dict.get("choices") or []
    return response_dict, [dict(choice) for choice in choices if isinstance(choice, Mapping)]


def _choice_delta(chunk: Any) -> dict[str, Any]:
    """通常streamとbeta streamの両方からdeltaを取得する。"""

    if isinstance(chunk, Mapping):
        choices = chunk.get("choices") or (chunk.get("chunk") or {}).get("choices") or []
    else:
        choices = []
    if not choices or not isinstance(choices[0], Mapping):
        return {}
    delta = choices[0].get("delta")
    return dict(delta) if isinstance(delta, Mapping) else {}


class OpenRouterChatOpenAI(ChatOpenAI):
    """OpenRouter Chat Completionsのreasoning detailsを保持するChatOpenAI。"""

    _openrouter_stream_state: dict[str, Any] | None = PrivateAttr(default=None)
    _openrouter_request_model: str | None = PrivateAttr(default=None)

    @staticmethod
    def _set_context(message: Any, model: str, tool_call_ids: list[str]) -> None:
        if not tool_call_ids:
            return
        response_metadata = dict(getattr(message, "response_metadata", {}) or {})
        response_metadata[_REASONING_CONTEXT_KEY] = {
            "model": model,
            "tool_call_ids": list(tool_call_ids),
        }
        message.response_metadata = response_metadata

    def _attach_response_details(
        self,
        result: ChatResult,
        response: Any,
        *,
        request_model: str,
    ) -> ChatResult:
        response_dict, choices = _response_choices(response)
        raw_usage = response_dict.get("usage")
        for generation, choice in zip(result.generations, choices):
            message = generation.message
            if not isinstance(message, AIMessage):
                continue
            if isinstance(raw_usage, Mapping):
                response_metadata = dict(message.response_metadata or {})
                # langchain-openai keeps OpenRouter's raw cost only in
                # ChatResult.llm_output.  Copy the provider usage onto the
                # message so stream/invoke callers can record it uniformly.
                response_metadata["usage"] = copy.deepcopy(dict(raw_usage))
                response_metadata["billing_backend"] = "openrouter"
                message.response_metadata = response_metadata
            raw_message = choice.get("message")
            if isinstance(raw_message, Mapping):
                raw_message_dict = raw_message
            elif hasattr(raw_message, "model_dump"):
                raw_message_dict = raw_message.model_dump()
            else:
                raw_message_dict = {}
            details = _copy_reasoning_details(raw_message_dict.get(_REASONING_DETAILS_KEY))
            if not details:
                continue

            additional_kwargs = dict(message.additional_kwargs or {})
            additional_kwargs[_REASONING_DETAILS_KEY] = details
            message.additional_kwargs = additional_kwargs

            raw_ids = [
                str(tool_call.get("id"))
                for tool_call in (raw_message_dict.get("tool_calls") or [])
                if isinstance(tool_call, Mapping) and tool_call.get("id")
            ]
            # OpenRouter may return a resolved model/alias for ``response.model``
            # while the next request must use the exact model string sent by the
            # caller (including a preset suffix).  Bind the replay check to that
            # request value; the raw response model remains in llm_output.
            self._set_context(message, request_model, raw_ids or _tool_call_ids(message))
        return result

    def _create_chat_result(
        self,
        response: dict | Any,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        return self._attach_response_details(
            result,
            response,
            request_model=str(self._openrouter_request_model or self.model_name),
        )

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        result = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if result is None or not isinstance(result.message, AIMessageChunk):
            return result

        raw_usage = chunk.get("usage") if isinstance(chunk, Mapping) else None
        if isinstance(raw_usage, Mapping):
            response_metadata = dict(result.message.response_metadata or {})
            response_metadata["usage"] = copy.deepcopy(dict(raw_usage))
            response_metadata["billing_backend"] = "openrouter"
            result.message.response_metadata = response_metadata

        details = _copy_reasoning_details(_choice_delta(chunk).get(_REASONING_DETAILS_KEY))
        state = self._openrouter_stream_state
        if details:
            additional_kwargs = dict(result.message.additional_kwargs or {})
            additional_kwargs[_REASONING_DETAILS_KEY] = details
            result.message.additional_kwargs = additional_kwargs
            if state is not None:
                state["has_reasoning_details"] = True

        if state is not None:
            if self._openrouter_request_model:
                state["model"] = self._openrouter_request_model
            _append_unique_ids(state["tool_call_ids"], _chunk_tool_call_ids(result.message))
        return result

    def _finalize_stream_chunk(self, generation_chunk: ChatGenerationChunk | None) -> None:
        state = self._openrouter_stream_state
        if generation_chunk is None or state is None or not state.get("has_reasoning_details"):
            return
        message = generation_chunk.message
        if isinstance(message, AIMessageChunk):
            self._set_context(
                message,
                str(state.get("model") or self._openrouter_request_model or self.model_name),
                list(state.get("tool_call_ids") or []),
            )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        *,
        stream_usage: bool | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        self._openrouter_stream_state = {
            "has_reasoning_details": False,
            "tool_call_ids": [],
            "model": str(kwargs.get("model") or self.model_name),
        }
        pending: ChatGenerationChunk | None = None
        try:
            for generation_chunk in super()._stream(
                messages,
                stop=stop,
                run_manager=run_manager,
                stream_usage=stream_usage,
                **kwargs,
            ):
                if pending is not None:
                    yield pending
                pending = generation_chunk
            self._finalize_stream_chunk(pending)
            if pending is not None:
                yield pending
        finally:
            self._openrouter_stream_state = None

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        *,
        stream_usage: bool | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self._openrouter_stream_state = {
            "has_reasoning_details": False,
            "tool_call_ids": [],
            "model": str(kwargs.get("model") or self.model_name),
        }
        pending: ChatGenerationChunk | None = None
        try:
            async for generation_chunk in super()._astream(
                messages,
                stop=stop,
                run_manager=run_manager,
                stream_usage=stream_usage,
                **kwargs,
            ):
                if pending is not None:
                    yield pending
                pending = generation_chunk
            self._finalize_stream_chunk(pending)
            if pending is not None:
                yield pending
        finally:
            self._openrouter_stream_state = None

    @staticmethod
    def _message_reasoning_details_for_request(
        message: Any,
        current_model: str,
    ) -> list[Any]:
        if not isinstance(message, AIMessage):
            return []
        additional_kwargs = message.additional_kwargs or {}
        details = _copy_reasoning_details(additional_kwargs.get(_REASONING_DETAILS_KEY))
        if not details:
            return []
        context = (message.response_metadata or {}).get(_REASONING_CONTEXT_KEY)
        if not isinstance(context, Mapping):
            return []
        if str(context.get("model") or "") != str(current_model):
            return []
        expected_ids = [str(value) for value in context.get("tool_call_ids") or []]
        current_ids = _tool_call_ids(message)
        if not expected_ids or current_ids != expected_ids:
            return []
        return details

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        self._openrouter_request_model = str(payload.get("model") or self.model_name)
        # The adapter deliberately does not add Responses API support. If a caller
        # selects it explicitly, the base payload is returned untouched.
        serialized_messages = payload.get("messages")
        if not isinstance(serialized_messages, list):
            return payload

        messages = self._convert_input(input_).to_messages()
        if len(messages) != len(serialized_messages):
            return payload
        current_model = str(payload.get("model") or self.model_name)
        for message, serialized in zip(messages, serialized_messages):
            if not isinstance(serialized, dict):
                continue
            details = self._message_reasoning_details_for_request(message, current_model)
            if details:
                serialized[_REASONING_DETAILS_KEY] = details
        return payload


__all__ = ["OpenRouterChatOpenAI"]
