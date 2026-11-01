"""Top-level spawn entry point for the native delegation read-only canary."""

from __future__ import annotations

import asyncio
import queue
from typing import Any

from agent_delegation.native_backend import NativeAgentBackend
from agent_delegation.worker_contract import (
    NATIVE_WORKER_SETTING_KEYS,
    NATIVE_WORKER_MAX_STEERS,
    NativeWorkerControl,
    NativeWorkerControlMessage,
    NativeWorkerError,
    NativeWorkerEvent,
    NativeWorkerMessage,
    NativeWorkerRequest,
    NativeWorkerResult,
    resolve_native_spawn_canary_mode,
)


class _IpcWorkerControl(NativeWorkerControl):
    def __init__(self, event_connection: Any, control_queue: Any):
        self._event_connection = event_connection
        self._control_queue = control_queue
        self._cancelled = False
        self._steers: list[str] = []
        self._control_broken = False

    def _drain_control(self) -> None:
        if self._control_broken:
            return
        while True:
            try:
                payload = self._control_queue.get_nowait()
            except queue.Empty:
                return
            except Exception as exc:
                self._control_broken = True
                self._report_control_error("control channel read failed", exc)
                return
            try:
                message = NativeWorkerControlMessage.from_payload(payload)
            except Exception as exc:
                self._report_control_error("invalid control message rejected", exc)
                continue
            if message.control_type == "cancel":
                self._cancelled = True
            elif len(self._steers) < NATIVE_WORKER_MAX_STEERS:
                self._steers.append(message.instruction)
            else:
                self._report_control_error("steer message rejected", "worker steer limit reached")

    def _report_control_error(self, message: str, error: Any) -> None:
        try:
            self.report_event(NativeWorkerEvent("log", {
                "message": message,
                "extra": {"error": f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)},
            }))
        except Exception:
            pass

    def is_cancelled(self) -> bool:
        self._drain_control()
        return self._cancelled

    def drain_steer(self) -> list[str]:
        self._drain_control()
        pending = list(self._steers)
        self._steers.clear()
        return pending

    def report_event(self, event: NativeWorkerEvent) -> None:
        self._event_connection.send(NativeWorkerMessage("event", event.to_payload()).to_payload())


def _validate_spawn_canary(request: NativeWorkerRequest) -> None:
    spec = request.spec
    scope_tiers = {str(spec.permission_tier or "read").strip().lower()}
    scope_tiers.update(str(scope.tier or "read").strip().lower() for scope in spec.extra_scopes)
    mode = resolve_native_spawn_canary_mode(request.settings)
    primary_tier = str(spec.permission_tier or "read").strip().lower()
    if not scope_tiers.issubset({"read", "write", "full"}):
        raise ValueError("spawn canaryに未対応のpermission tierがあります。")
    if mode == "full":
        if primary_tier != "full":
            raise ValueError("spawn canary mode=fullはprimary permission_tier=fullだけを実行できます。")
    elif "full" in scope_tiers:
        raise ValueError("spawn canaryのfull scopeには明示mode=fullが必要です。")
    if mode in {"read", "web"} and scope_tiers != {"read"}:
        raise ValueError(f"spawn canary mode={mode}ではwrite scopeを利用できません。")
    effective_web = bool(request.settings.get("allow_web_tools")) and mode in {"web", "write", "full"}
    if bool(spec.allow_web_tools) != effective_web:
        raise ValueError("spawn canaryのweb設定がspecとsettingsで一致しません。")
    unexpected_settings = set(request.settings).difference(NATIVE_WORKER_SETTING_KEYS)
    if unexpected_settings:
        names = ", ".join(sorted(unexpected_settings))
        raise ValueError(f"spawn canaryのsettings snapshotに未許可キーがあります: {names}")


def native_worker_entry(request_payload: dict[str, Any], event_connection: Any, control_queue: Any) -> None:
    """Build the LLM/tools inside a spawned child and emit JSON-only messages."""

    try:
        request = NativeWorkerRequest.from_payload(request_payload)
        _validate_spawn_canary(request)
        control = _IpcWorkerControl(event_connection, control_queue)
        backend = NativeAgentBackend(settings=request.settings)
        result = asyncio.run(backend.run(request.spec, control=control))
        payload = NativeWorkerResult.from_run_result(result).to_payload()
        event_connection.send(NativeWorkerMessage("result", payload).to_payload())
    except BaseException as exc:  # Child boundary: normalize every exit that can still report.
        metadata = getattr(exc, "metadata", {})
        safe_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        assistant_text = getattr(exc, "assistant_text", "")
        if assistant_text:
            safe_metadata["assistant_text"] = str(assistant_text)
        error = NativeWorkerError(type(exc).__name__, str(exc), safe_metadata)
        try:
            event_connection.send(NativeWorkerMessage("error", error.to_payload()).to_payload())
        except Exception:
            pass
    finally:
        try:
            event_connection.close()
        except Exception:
            pass
