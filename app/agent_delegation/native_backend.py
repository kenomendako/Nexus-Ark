"""Native delegated-agent backend loop."""

from __future__ import annotations

import asyncio
import ctypes
import gc
import inspect
import functools
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

import config_manager
import constants
from agent.tool_message_compression import (
    LONG_TOOL_MESSAGE_THRESHOLD,
    compress_tool_messages_for_agent,
    make_tool_result_excerpt,
)
from agent_delegation.native_tools import build_native_tools
from agent_delegation.types import AgentRunResult, AgentTaskSpec
from agent_delegation.worker_contract import NativeWorkerControl, NativeWorkerEvent, NullNativeWorkerControl

try:
    import psutil
except ImportError:  # pragma: no cover - depends on optional runtime dependency
    psutil = None


EMPTY_RESPONSE_RETRY_LIMIT = 2
BLOCKING_CALL_POLL_INTERVAL_SECONDS = 1.0
SERVER_ERROR_TURN_RETRY_LIMIT = 2
SERVER_ERROR_TURN_RETRY_WAIT_SECONDS = 25.0
SERVER_ERROR_TURN_RETRY_POLL_SECONDS = 5.0
# 5xxステータスコード（500/502/503）の単語境界一致。
_SERVER_ERROR_CODE_RE = re.compile(r"\b50[023]\b")
NATIVE_UNSUPPORTED_MESSAGE = "このプロバイダ/モデルでは委任を実行できません。ツール呼び出し対応モデルを選択してください。"
NATIVE_EMPTY_RESPONSE_MESSAGE = "native 委任バックエンドが空応答を返したため、タスクを中断しました。"
NATIVE_COMPRESSIBLE_TOOL_RESULT_NAMES = {"Read", "Glob", "Grep", "WebSearch", "WebFetch"}
DENIAL_MARKERS = (
    "Nexus Ark policy:",
    "outside the delegated workspace",
    "excluded by project_explorer settings",
    "requires delegation permission tier",
)


class NativeAgentBackend:
    def __init__(self, settings: dict[str, Any], *, llm: Any = None):
        # タスクごとの開始RSSをprivate keyへ保持するため、呼び出し元の共有設定を変更しない。
        self.settings = dict(settings or {})
        self.llm = llm

    async def run(
        self,
        spec: AgentTaskSpec,
        *,
        control: NativeWorkerControl | None = None,
        sdk_factory: Any = None,
        client_factory: Any = None,
    ) -> AgentRunResult:
        worker_control = control or NullNativeWorkerControl()
        memory_guard = _prepare_native_memory_guard(self.settings, control=worker_control)
        baseline_mb = memory_guard.get("baseline_rss_mb")
        if baseline_mb is not None:
            self.settings["_deleg_rss_baseline_mb"] = baseline_mb
        self.settings["_deleg_memory_guard_metadata"] = memory_guard
        _raise_if_rss_limit_exceeded(self.settings, phase="開始前メモリ整理後")

        async def execute() -> AgentRunResult:
            return await self._run_loop(spec, control=worker_control)

        return await asyncio.wait_for(execute(), timeout=int(spec.timeout_seconds))

    def _create_llm(self, spec: AgentTaskSpec) -> Any:
        from llm_factory import LLMFactory

        # 委任ループには独自のリトライ機構が無いため、プロバイダ側の一時的な 5xx
        # （Gemini の 500 INTERNAL / 503 等）で1ターン目から委任全体が即失敗していた。
        # 軽いリトライ（指数バックオフ）を与えて一時障害を吸収する。総実行時間は
        # spec.timeout_seconds（asyncio.wait_for）で上限が掛かるため暴走しない。
        if spec.model_override:
            try:
                return LLMFactory.create_chat_model(
                    room_name=spec.room_name,
                    internal_role="delegation",
                    temperature=0.2,
                    max_retries=2,
                    delegation_model_override=tuple(spec.model_override),
                )
            except Exception as exc:
                print(
                    "[Native Delegation] routed delegation model failed; "
                    f"falling back to default delegation model: {type(exc).__name__}: {exc}"
                )

        if config_manager.get_effective_delegation_model(spec.room_name):
            return LLMFactory.create_chat_model(
                room_name=spec.room_name,
                internal_role="delegation",
                temperature=0.2,
                max_retries=2,
            )

        return LLMFactory.create_chat_model(
            room_name=spec.room_name,
            temperature=0.2,
            max_retries=2,
        )

    async def _run_loop(self, spec: AgentTaskSpec, *, control: NativeWorkerControl) -> AgentRunResult:
        provider = _active_provider_for_spec(spec)
        routed_model = _routed_model_metadata(spec)
        ledger_model_name = _ledger_model_name_for_spec(spec)
        ledger_key_name = _ledger_key_name_for_spec(spec, ledger_model_name)
        try:
            tools = build_native_tools(spec, app_root=Path(constants.ROOMS_DIR).resolve().parent, settings=self.settings)
            tools_by_name = {tool.name: tool for tool in tools}
            llm = self.llm or self._create_llm(spec)
            if not ledger_model_name:
                ledger_model_name = getattr(llm, "model_name", getattr(llm, "model", ""))
            llm_with_tools = llm.bind_tools(tools)
        except Exception as exc:
            return AgentRunResult(
                assistant_text=f"{NATIVE_UNSUPPORTED_MESSAGE}\n{type(exc).__name__}: {exc}",
                metadata={
                    "tool_uses": [],
                    "denied_tool_count": 0,
                    "auth_source": f"native:{provider}",
                    "backend": "native",
                    "memory_guard": _memory_guard_metadata(self.settings),
                    **({"routed_model": routed_model} if routed_model else {}),
                },
            )

        from agent_delegation import manager

        task = manager._task_dict_from_spec(spec)
        messages = [
            SystemMessage(content=manager._delegation_system_prompt(task)),
            HumanMessage(content=manager._delegation_prompt(task)),
        ]
        assistant_text_parts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        denied_tool_count = 0
        empty_response_count = 0

        # --- 自動レビュー反復（B-2）---
        # 最終回答が出たら、報告前に内部モデルで期待アウトプットに照らして点検し、
        # 不足なら指摘を差し戻して直させる（既定 OFF＝従来挙動）。
        review_budget = int(self.settings.get("deleg_review_iterations") or 0)
        review_role = str(self.settings.get("deleg_review_internal_role") or "summarization")
        review_enabled = review_budget > 0 and bool(str(spec.expected_output or "").strip())
        review_verdicts: list[str] = []

        max_turns = int(spec.max_turns)
        for _turn in range(max_turns):
            if control.is_cancelled():
                raise asyncio.CancelledError("委任タスクはキャンセルされました。")
            _raise_if_rss_limit_exceeded(self.settings, phase="ターン開始時")

            # 途中指示（ステアリング・D-1）：保留があれば HumanMessage として注入し、次の思考へ反映する。
            if control is not None:
                pending_steers = control.drain_steer()
                if pending_steers:
                    steer_body = "\n".join(f"- {s}" for s in pending_steers)
                    messages.append(
                        HumanMessage(
                            content=(
                                "【稼働中の追加指示】\n"
                                f"{steer_body}\n"
                                "この指示を踏まえて作業を続けてください。"
                            )
                        )
                    )
                    _report_native_progress(
                        spec,
                        phase="steering",
                        turn=_turn + 1,
                        max_turns=max_turns,
                        tool_uses=tool_uses,
                        denied_tool_count=denied_tool_count,
                        control=control,
                    )

            turn_no = _turn + 1
            _report_native_progress(
                spec,
                phase="thinking",
                turn=turn_no,
                max_turns=max_turns,
                tool_uses=tool_uses,
                denied_tool_count=denied_tool_count,
                control=control,
            )

            messages_for_llm, compressed_count, original_chars, compressed_chars = compress_tool_messages_for_agent(messages)
            if compressed_count:
                print(
                    "[Native Delegation] "
                    f"長大なToolMessage {compressed_count}件を次思考用に圧縮しました "
                    f"({original_chars} -> {compressed_chars} chars)。"
                )
            ai_message = await _invoke_llm_with_server_retry(
                llm_with_tools,
                messages_for_llm,
                settings=self.settings,
                control=control,
                spec=spec,
                turn=turn_no,
            )
            try:
                import usage_ledger
                usage_ledger.record_response(ai_message, ledger_model_name, "delegation", api_key_name=ledger_key_name)
            except Exception:
                pass
            messages.append(ai_message)
            text = _message_text(ai_message).strip()
            tool_calls = _message_tool_calls(ai_message)

            if text:
                assistant_text_parts.append(text)

            if not tool_calls and not text:
                empty_response_count += 1
                if empty_response_count <= EMPTY_RESPONSE_RETRY_LIMIT:
                    messages.append(HumanMessage(content="前の応答が空でした。必要ならツールを使い、完了時は要約を返してください。"))
                    continue
                return AgentRunResult(
                    assistant_text=NATIVE_EMPTY_RESPONSE_MESSAGE,
                    metadata={
                        "tool_uses": tool_uses,
                        "tool_call_counts": _aggregate_tool_call_counts(tool_uses),
                        "denied_tool_count": denied_tool_count,
                        "auth_source": f"native:{provider}",
                        "backend": "native",
                        "memory_guard": _memory_guard_metadata(self.settings),
                        **({"routed_model": routed_model} if routed_model else {}),
                    },
                )
            empty_response_count = 0

            if not tool_calls:
                # 自動レビュー反復（B-2）：最終回答を期待アウトプットに照らして点検し、
                # 不足かつ予算が残っていれば指摘を差し戻して直させる。
                if review_enabled and review_budget > 0 and text.strip():
                    review_text, verdict = await _run_blocking_with_polling(
                        manager.evaluate_output,
                        spec.task_description,
                        spec.expected_output,
                        text,
                        settings=self.settings,
                        control=control,
                        phase="自動レビュー待機中",
                        room_name=spec.room_name,
                        review_role=review_role,
                    )
                    review_verdicts.append(verdict)
                    if verdict == "revise":
                        review_budget -= 1
                        messages.append(
                            HumanMessage(
                                content=(
                                    "【自動レビューの指摘】\n"
                                    f"{review_text}\n\n"
                                    "上記の指摘を反映し、期待アウトプットを満たすよう成果を直してください。"
                                    "直し終えたら、ツールを使わずに最終成果の要約を返してください。"
                                )
                            )
                        )
                        _report_native_progress(
                            spec,
                            phase="revising",
                            turn=turn_no,
                            max_turns=max_turns,
                            tool_uses=tool_uses,
                            denied_tool_count=denied_tool_count,
                            control=control,
                        )
                        continue
                    if verdict == "unknown":
                        print(f"[Native Delegation] 自動レビュー判定不能: task_id={spec.task_id}")
                        try:
                            control.report_event(NativeWorkerEvent("log", {
                                "message": "review verdict unknown",
                                "extra": {"review_verdict": "unknown", "review_text": review_text[:1000]},
                            }))
                        except Exception:
                            pass
                return AgentRunResult(
                    assistant_text=text,
                    metadata={
                        "tool_uses": tool_uses,
                        "tool_call_counts": _aggregate_tool_call_counts(tool_uses),
                        "denied_tool_count": denied_tool_count,
                        "auth_source": f"native:{provider}",
                        "backend": "native",
                        "memory_guard": _memory_guard_metadata(self.settings),
                        "review_iterations": len(review_verdicts),
                        "review_verdicts": list(review_verdicts),
                        "review_verdict": review_verdicts[-1] if review_verdicts else "",
                        **({"routed_model": routed_model} if routed_model else {}),
                    },
                )

            for tool_call in tool_calls:
                name = str(tool_call.get("name") or "")
                args = tool_call.get("args") or tool_call.get("input") or {}
                tool_call_id = str(tool_call.get("id") or f"native_tool_{len(tool_uses) + 1}")
                tool = tools_by_name.get(name)
                if tool is None:
                    result = f"Native agent tool is not available: {name}"
                else:
                    result = await _invoke_tool(tool, args, settings=self.settings, control=control, phase=f"{name} 実行中")
                result_text = str(result)
                denied = _is_denial_result(result_text)
                if denied:
                    denied_tool_count += 1
                tool_uses.append({"name": name, "input": args, "result": result_text[:1000], "denied": denied})
                messages.append(ToolMessage(content=_stored_tool_result_content(name, result_text), tool_call_id=tool_call_id, name=name))
                _report_native_progress(
                    spec,
                    phase="tool",
                    turn=turn_no,
                    max_turns=max_turns,
                    tool_uses=tool_uses,
                    denied_tool_count=denied_tool_count,
                    last_tool=name,
                    last_tool_denied=denied,
                    control=control,
                )
                _raise_if_rss_limit_exceeded(self.settings, phase=f"{name} 実行後")

        metadata = {
            "tool_uses": tool_uses,
            "tool_call_counts": _aggregate_tool_call_counts(tool_uses),
            "denied_tool_count": denied_tool_count,
            "auth_source": f"native:{provider}",
            "backend": "native",
            "memory_guard": _memory_guard_metadata(self.settings),
            "review_iterations": len(review_verdicts),
            "review_verdicts": list(review_verdicts),
            "review_verdict": review_verdicts[-1] if review_verdicts else "",
            **({"routed_model": routed_model} if routed_model else {}),
        }
        raise manager.MaxTurnsReachedError(
            "native delegated agent reached maximum number of turns.",
            "\n\n".join(part for part in assistant_text_parts if part).strip(),
            metadata,
        )


async def _await_with_polling(awaitable: Any, *, settings: dict[str, Any], control: NativeWorkerControl, phase: str = "待機中") -> Any:
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=BLOCKING_CALL_POLL_INTERVAL_SECONDS)
            if done:
                return await task
            if control.is_cancelled():
                task.cancel()
                raise asyncio.CancelledError("委任タスクはキャンセルされました。")
            _raise_if_rss_limit_exceeded(settings, phase=phase)
    except asyncio.CancelledError:
        task.cancel()
        raise


async def _run_blocking_with_polling(
    func: Any,
    *args: Any,
    settings: dict[str, Any],
    control: NativeWorkerControl,
    phase: str = "同期処理待機中",
    **kwargs: Any,
) -> Any:
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="native-blocking-call")
    call = functools.partial(func, *args, **kwargs)
    future = loop.run_in_executor(executor, call)
    try:
        return await _await_with_polling(future, settings=settings, control=control, phase=phase)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


async def _invoke_llm(llm_with_tools: Any, messages: list[Any], *, settings: dict[str, Any], control: NativeWorkerControl) -> Any:
    ainvoke = getattr(llm_with_tools, "ainvoke", None)
    if callable(ainvoke):
        return await _await_with_polling(ainvoke(messages), settings=settings, control=control, phase="LLM応答待機中")
    return await _run_blocking_with_polling(
        llm_with_tools.invoke,
        messages,
        settings=settings,
        control=control,
        phase="LLM応答待機中",
    )


async def _invoke_llm_with_server_retry(
    llm_with_tools: Any,
    messages: list[Any],
    *,
    settings: dict[str, Any],
    control: NativeWorkerControl,
    spec: AgentTaskSpec,
    turn: int,
) -> Any:
    attempt = 0
    while True:
        try:
            return await _invoke_llm(llm_with_tools, messages, settings=settings, control=control)
        except Exception as exc:
            if not _is_retryable_server_error(exc) or attempt >= SERVER_ERROR_TURN_RETRY_LIMIT:
                raise
            attempt += 1
            error_text = _exception_chain_text(exc)
            try:
                control.report_event(NativeWorkerEvent("log", {
                    "message": "server error retry",
                    "extra": {
                        "turn": int(turn),
                        "attempt": attempt,
                        "max_retries": SERVER_ERROR_TURN_RETRY_LIMIT,
                        "wait_seconds": SERVER_ERROR_TURN_RETRY_WAIT_SECONDS,
                        "error": error_text[:100],
                    },
                }))
            except Exception:
                pass
            await _sleep_before_server_retry(settings=settings, control=control)


async def _sleep_before_server_retry(*, settings: dict[str, Any], control: NativeWorkerControl) -> None:
    remaining = float(SERVER_ERROR_TURN_RETRY_WAIT_SECONDS)
    poll = max(0.01, float(SERVER_ERROR_TURN_RETRY_POLL_SECONDS))
    while remaining > 0:
        if control.is_cancelled():
            raise asyncio.CancelledError("委任タスクはキャンセルされました。")
        _raise_if_rss_limit_exceeded(settings, phase="LLM 5xx リトライ待機中")
        sleep_for = min(poll, remaining)
        await asyncio.sleep(sleep_for)
        remaining -= sleep_for
    if control.is_cancelled():
        raise asyncio.CancelledError("委任タスクはキャンセルされました。")
    _raise_if_rss_limit_exceeded(settings, phase="LLM 5xx リトライ待機後")


def _is_retryable_server_error(exc: Exception) -> bool:
    text = _exception_chain_text(exc).upper()
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return False
    retryable_markers = (
        "INTERNALSERVERERROR",
        "INTERNAL SERVER ERROR",
        "SERVERERROR",
        "SERVER ERROR",
        "UNAVAILABLE",
    )
    if any(marker in text for marker in retryable_markers):
        return True
    # ステータスコードは単語境界で判定する。裸の部分文字列一致だと
    # 「input token count 150000」等の数値に "500" が誤マッチするため。
    return bool(_SERVER_ERROR_CODE_RE.search(text))


def _exception_chain_text(exc: BaseException) -> str:
    parts = [f"{type(exc).__name__}: {exc}"]
    seen = {id(exc)}
    current = exc
    while True:
        next_exc = current.__cause__ or current.__context__
        if next_exc is None or id(next_exc) in seen:
            break
        seen.add(id(next_exc))
        parts.append(f"{type(next_exc).__name__}: {next_exc}")
        current = next_exc
    return " | ".join(parts)


async def _invoke_tool(
    tool: Any,
    args: dict[str, Any],
    *,
    settings: dict[str, Any],
    control: NativeWorkerControl,
    phase: str = "ツール実行中",
) -> str:
    try:
        result = await _run_blocking_with_polling(
            tool.invoke,
            args,
            settings=settings,
            control=control,
            phase=phase,
        )
        if inspect.isawaitable(result):
            result = await _await_with_polling(result, settings=settings, control=control, phase=phase)
        return str(result)
    except Exception as exc:
        from agent_delegation import manager

        if isinstance(exc, manager.MemoryLimitExceededError):
            raise
        return f"Native tool error: {type(exc).__name__}: {exc}"


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
            else:
                text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        return "".join(parts)
    return str(content or "")


def _message_tool_calls(message: Any) -> list[dict[str, Any]]:
    calls = getattr(message, "tool_calls", None) or []
    return [dict(call) for call in calls if isinstance(call, dict)]


def _is_denial_result(result: str) -> bool:
    return any(marker in str(result) for marker in DENIAL_MARKERS)


def _report_native_progress(
    spec: AgentTaskSpec,
    *,
    phase: str,
    turn: int,
    max_turns: int,
    tool_uses: list[dict[str, Any]],
    denied_tool_count: int,
    last_tool: str | None = None,
    last_tool_denied: bool = False,
    control: NativeWorkerControl,
) -> None:
    """Report JSON-only progress/log events to the parent-side control."""
    counts = _aggregate_tool_call_counts(tool_uses)
    progress = {
        "phase": phase,
        "turn": int(turn),
        "max_turns": int(max_turns),
        "tool_call_total": sum(counts.values()),
        "tool_call_counts": counts,
        "denied_tool_count": int(denied_tool_count),
        "last_tool": str(last_tool or ""),
    }
    control.report_event(NativeWorkerEvent("progress", progress))
    try:
        if phase == "thinking":
            control.report_event(NativeWorkerEvent("log", {
                "message": "turn start",
                "extra": {"turn": int(turn), "max_turns": int(max_turns)},
            }))
        elif phase == "tool" and last_tool:
            control.report_event(NativeWorkerEvent("log", {
                "message": "tool used",
                "extra": {"turn": int(turn), "tool": str(last_tool), "denied": bool(last_tool_denied)},
            }))
    except Exception:
        pass


def _aggregate_tool_call_counts(tool_uses: list[dict[str, Any]]) -> dict[str, int]:
    """ツール別の呼び出し回数を集計して可観測性メタに残す。"""
    counts: dict[str, int] = {}
    for use in tool_uses or []:
        name = str(use.get("name") or "") if isinstance(use, dict) else ""
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _stored_tool_result_content(tool_name: str, content: str) -> str:
    if tool_name not in NATIVE_COMPRESSIBLE_TOOL_RESULT_NAMES or len(content) <= LONG_TOOL_MESSAGE_THRESHOLD:
        return content
    return make_tool_result_excerpt(tool_name, content)


def _prepare_native_memory_guard(
    settings: dict[str, Any],
    *,
    control: NativeWorkerControl | None = None,
) -> dict[str, Any]:
    """native委任開始前に解放可能な本体メモリを整理し、増分計測の基準RSSを返す。"""

    before_mb = _process_rss_mb()
    cache_release_error = ""
    try:
        from rag_manager import RAGManager

        RAGManager.clear_cache()
    except Exception as exc:
        cache_release_error = f"{type(exc).__name__}: {exc}"
        gc.collect()
        if os.name == "posix":
            try:
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except Exception:
                pass
    after_mb = _process_rss_mb()
    metadata = {
        "available": after_mb is not None,
        "preflight_rss_mb": round(before_mb, 1) if before_mb is not None else None,
        "baseline_rss_mb": round(after_mb, 1) if after_mb is not None else None,
        "released_mb": round(max(0.0, before_mb - after_mb), 1) if before_mb is not None and after_mb is not None else None,
        "absolute_limit_mb": max(0, int((settings or {}).get("deleg_rss_limit_mb") or 0)),
        "headroom_limit_mb": max(0, int((settings or {}).get("deleg_rss_headroom_mb") or 0)),
    }
    if cache_release_error:
        metadata["cache_release_error"] = cache_release_error
    if control is not None:
        try:
            control.report_event(NativeWorkerEvent("log", {
                "message": "native memory preflight",
                "extra": {"memory_guard": metadata},
            }))
        except Exception:
            pass
    return metadata


def _process_rss_mb() -> float | None:
    if psutil is None:
        return None
    try:
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        return None


def _memory_guard_metadata(settings: dict[str, Any]) -> dict[str, Any]:
    metadata = (settings or {}).get("_deleg_memory_guard_metadata") or {}
    return dict(metadata) if isinstance(metadata, dict) else {}


def _raise_if_rss_limit_exceeded(settings: dict[str, Any], *, phase: str) -> None:
    absolute_limit_mb = int((settings or {}).get("deleg_rss_limit_mb") or 0)
    headroom_limit_mb = int((settings or {}).get("deleg_rss_headroom_mb") or 0)
    # 既存契約どおり絶対上限0はメモリガード全体を無効化する。
    if absolute_limit_mb <= 0 or psutil is None:
        return
    rss_mb = _process_rss_mb()
    if rss_mb is None:
        return
    baseline_raw = (settings or {}).get("_deleg_rss_baseline_mb")
    baseline_mb = float(baseline_raw) if baseline_raw is not None else None
    delta_mb = max(0.0, rss_mb - baseline_mb) if baseline_mb is not None else None
    absolute_exceeded = absolute_limit_mb > 0 and rss_mb > absolute_limit_mb
    headroom_exceeded = headroom_limit_mb > 0 and delta_mb is not None and delta_mb > headroom_limit_mb
    if not absolute_exceeded and not headroom_exceeded:
        return
    from agent_delegation import manager

    reasons = []
    if absolute_exceeded:
        reasons.append(f"絶対上限 {absolute_limit_mb}MB")
    if headroom_exceeded:
        reasons.append(f"増分上限 {headroom_limit_mb}MB")
    details = f"RSS {rss_mb:.1f}MB"
    if baseline_mb is not None and delta_mb is not None:
        details += f" / 開始時 {baseline_mb:.1f}MB / 増分 {delta_mb:.1f}MB"
    metadata = {
        "memory_guard": {
            **_memory_guard_metadata(settings),
            "phase": phase,
            "reason": "absolute_and_headroom" if absolute_exceeded and headroom_exceeded else ("absolute" if absolute_exceeded else "headroom"),
            "rss_mb": round(rss_mb, 1),
            "delta_mb": round(delta_mb, 1) if delta_mb is not None else None,
        }
    }
    raise manager.MemoryLimitExceededError(
        f"native 委任がメモリ上限を超えたため中断しました（{phase}: {details} / {'・'.join(reasons)}）。",
        metadata=metadata,
    )


def _active_provider_for_spec(spec: AgentTaskSpec) -> str:
    if spec.model_override:
        return str(spec.model_override[0] or "unknown")
    try:
        delegation_model = config_manager.get_effective_delegation_model(spec.room_name)
        if delegation_model:
            return str(delegation_model[0] or "unknown")
    except Exception:
        pass
    try:
        return str(config_manager.get_active_provider(spec.room_name) or "google")
    except Exception:
        return "unknown"


def _routed_model_metadata(spec: AgentTaskSpec) -> dict[str, str] | None:
    if not spec.model_override:
        return None
    provider_cat, model_name, profile_name = spec.model_override
    return {
        "provider": str(provider_cat or ""),
        "model": str(model_name or ""),
        "profile": str(profile_name or ""),
    }


def _ledger_model_name_for_spec(spec: AgentTaskSpec) -> str:
    if spec.model_override:
        return str(spec.model_override[1] or "")
    try:
        delegation_model = config_manager.get_effective_delegation_model(spec.room_name)
        if delegation_model:
            return str(delegation_model[1] or "")
    except Exception:
        pass
    return ""


def _ledger_key_name_for_spec(spec: AgentTaskSpec, model_name: str) -> str:
    provider = _active_provider_for_spec(spec).lower()
    if provider not in {"google", "google (gemini)", "google (gemini native)"}:
        return ""
    try:
        return config_manager.get_active_gemini_api_key_name(room_name=spec.room_name, model_name=model_name) or ""
    except Exception:
        return ""
