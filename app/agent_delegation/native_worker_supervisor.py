"""Parent-side spawn supervisor for the native delegation read-only canary."""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
import secrets
import signal
import time
from typing import Any, Callable

from agent_delegation.native_worker import native_worker_entry
from agent_delegation.native_worker_processes import (
    NativeWorkerProcessIdentity,
    capture_process_identity,
    refresh_native_worker_registry,
    remove_native_worker_registry_run,
    stop_native_worker_process_tree,
)
from agent_delegation.types import AgentRunResult, AgentTaskSpec
from agent_delegation.worker_contract import (
    NATIVE_WORKER_MAX_STEERS,
    NativeWorkerControl,
    NativeWorkerControlMessage,
    NativeWorkerError,
    NativeWorkerEvent,
    NativeWorkerExecutionError,
    NativeWorkerMessage,
    NativeWorkerRequest,
    NativeWorkerResult,
    native_worker_settings_snapshot,
    resolve_native_spawn_canary_mode,
)


POLL_INTERVAL_SECONDS = 0.05
WORKER_STOP_GRACE_SECONDS = 2.0
WORKER_CANCEL_GRACE_SECONDS = 1.5


def is_native_spawn_canary_eligible(spec: AgentTaskSpec, settings: dict[str, Any]) -> bool:
    if not bool(settings.get("native_spawn_canary_enabled")):
        return False
    mode = resolve_native_spawn_canary_mode(settings)
    primary_tier = str(spec.permission_tier or "read").strip().lower()
    tiers = {str(spec.permission_tier or "read").strip().lower()}
    tiers.update(str(scope.tier or "read").strip().lower() for scope in spec.extra_scopes)
    if not tiers.issubset({"read", "write", "full"}):
        return False
    if mode == "full":
        if primary_tier != "full":
            return False
    elif "full" in tiers:
        return False
    if mode in {"read", "web"} and tiers != {"read"}:
        return False
    effective_web = bool(settings.get("allow_web_tools")) and mode in {"web", "write", "full"}
    return bool(spec.allow_web_tools) == effective_web


class NativeWorkerSupervisor:
    """Run a JSON-only native worker under a spawn context and normalize exits."""

    def __init__(
        self,
        settings: dict[str, Any],
        *,
        worker_entry: Callable[[dict[str, Any], Any, Any], None] = native_worker_entry,
    ):
        self.settings = dict(settings or {})
        self._worker_entry = worker_entry
        self._context = multiprocessing.get_context("spawn")

    @property
    def start_method(self) -> str:
        return self._context.get_start_method()

    async def run(
        self,
        spec: AgentTaskSpec,
        *,
        control: NativeWorkerControl | None = None,
        sdk_factory: Any = None,
        client_factory: Any = None,
    ) -> AgentRunResult:
        if sdk_factory is not None or client_factory is not None:
            raise TypeError("spawn workerへSDK/client factoryは渡せません。")
        if not is_native_spawn_canary_eligible(spec, self.settings):
            raise ValueError("spawn workerの対象は明示設定されたread／web／write／full canaryだけです。")
        request = NativeWorkerRequest(spec, native_worker_settings_snapshot(self.settings))
        request_payload = request.to_payload()
        return await asyncio.to_thread(self._run_blocking, request_payload, spec, control)

    def _run_blocking(
        self,
        request_payload: dict[str, Any],
        spec: AgentTaskSpec,
        control: NativeWorkerControl | None,
    ) -> AgentRunResult:
        parent_connection, child_connection = self._context.Pipe(duplex=False)
        control_queue = self._context.Queue(maxsize=12)
        process = self._context.Process(
            target=self._worker_entry,
            args=(request_payload, child_connection, control_queue),
            name=f"native-delegation-worker-{spec.task_id}",
            daemon=True,
        )
        deadline = time.monotonic() + max(1, int(spec.timeout_seconds))
        run_token = secrets.token_urlsafe(24)
        root_identity: NativeWorkerProcessIdentity | None = None
        known_identities: list[NativeWorkerProcessIdentity] = []
        try:
            process.start()
        except Exception as exc:
            parent_connection.close()
            child_connection.close()
            self._close_control_queue(control_queue)
            raise NativeWorkerExecutionError(
                f"native workerをspawnできませんでした: {type(exc).__name__}: {exc}",
                error_type="WorkerSpawnError",
            ) from exc
        child_connection.close()
        try:
            root_identity = capture_process_identity(
                task_id=spec.task_id,
                run_token=run_token,
                pid=int(process.pid),
                started_at=time.time(),
            )
            known_identities = refresh_native_worker_registry(root_identity, [])
        except Exception as exc:
            self._stop_worker(process, root_identity, known_identities)
            parent_connection.close()
            self._close_control_queue(control_queue)
            raise NativeWorkerExecutionError(
                f"native workerのprocess identityを登録できませんでした: {type(exc).__name__}: {exc}",
                error_type="WorkerIdentityError",
            ) from exc
        cancel_requested = False
        cancel_deadline: float | None = None
        forwarded_steers = 0
        try:
            while True:
                known_identities = refresh_native_worker_registry(root_identity, known_identities)
                if control is not None and control.is_cancelled() and not cancel_requested:
                    cancel_requested = True
                    delivered = self._put_control_message(
                        control_queue,
                        NativeWorkerControlMessage("cancel"),
                        control,
                    )
                    cancel_deadline = time.monotonic() + WORKER_CANCEL_GRACE_SECONDS if delivered else time.monotonic()
                if control is not None and not cancel_requested:
                    forwarded_steers = self._forward_steers(
                        control_queue,
                        control,
                        forwarded_steers,
                    )
                terminal = self._drain_messages(parent_connection, control)
                if cancel_requested:
                    if terminal is not None or not process.is_alive():
                        self._stop_worker(process, root_identity, known_identities)
                        raise asyncio.CancelledError("委任タスクはキャンセルされました。")
                    if cancel_deadline is not None and time.monotonic() >= cancel_deadline:
                        self._report_control_log(control, "cooperative cancel timed out; worker forced to stop")
                        self._stop_worker(process, root_identity, known_identities)
                        raise asyncio.CancelledError("委任タスクはキャンセルされました。")
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue
                if terminal is not None:
                    self._join_after_terminal(process, root_identity, known_identities)
                    return self._resolve_terminal(terminal)
                if time.monotonic() >= deadline:
                    self._stop_worker(process, root_identity, known_identities)
                    raise asyncio.TimeoutError("native workerがwall-clock timeoutを超えました。")
                if not process.is_alive():
                    process.join(timeout=0)
                    terminal = self._drain_messages(parent_connection, control)
                    if terminal is not None:
                        return self._resolve_terminal(terminal)
                    raise NativeWorkerExecutionError(
                        self._unexpected_exit_message(process.exitcode),
                        error_type="WorkerExitedWithoutResult",
                        metadata={"worker_exitcode": process.exitcode},
                    )
                time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            if root_identity is not None or process.is_alive():
                self._stop_worker(process, root_identity, known_identities)
            if root_identity is not None:
                try:
                    remove_native_worker_registry_run(root_identity.run_token)
                except Exception:
                    self._report_control_log(control, "native worker registry cleanup failed")
            try:
                parent_connection.close()
            except Exception:
                pass
            self._close_control_queue(control_queue)

    @staticmethod
    def _forward_steers(control_queue: Any, control: NativeWorkerControl, forwarded: int) -> int:
        try:
            pending = control.drain_steer()
        except Exception as exc:
            NativeWorkerSupervisor._report_control_log(control, "steer channel drain failed", exc)
            return forwarded
        for instruction in pending:
            if forwarded >= NATIVE_WORKER_MAX_STEERS:
                NativeWorkerSupervisor._report_control_log(control, "steer rejected by worker channel limit")
                continue
            try:
                message = NativeWorkerControlMessage("steer", str(instruction))
                message.to_payload()
            except (TypeError, ValueError) as exc:
                NativeWorkerSupervisor._report_control_log(control, "invalid steer rejected", exc)
                continue
            if NativeWorkerSupervisor._put_control_message(control_queue, message, control):
                forwarded += 1
        return forwarded

    @staticmethod
    def _put_control_message(control_queue: Any, message: NativeWorkerControlMessage, control: NativeWorkerControl) -> bool:
        try:
            control_queue.put_nowait(message.to_payload())
            return True
        except (queue.Full, OSError, ValueError, EOFError) as exc:
            NativeWorkerSupervisor._report_control_log(control, "worker control message rejected", exc)
            return False
        except Exception as exc:
            NativeWorkerSupervisor._report_control_log(control, "worker control channel failed", exc)
            return False

    @staticmethod
    def _report_control_log(control: NativeWorkerControl | None, message: str, error: Any = None) -> None:
        if control is None:
            return
        extra = {}
        if error is not None:
            extra["error"] = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        try:
            control.report_event(NativeWorkerEvent("log", {"message": message, "extra": extra}))
        except Exception:
            pass

    @staticmethod
    def _close_control_queue(control_queue: Any) -> None:
        try:
            control_queue.cancel_join_thread()
        except Exception:
            pass
        try:
            control_queue.close()
        except Exception:
            pass

    @staticmethod
    def _drain_messages(connection: Any, control: NativeWorkerControl | None) -> NativeWorkerMessage | None:
        terminal: NativeWorkerMessage | None = None
        try:
            while connection.poll():
                message = NativeWorkerMessage.from_payload(connection.recv())
                if message.message_type == "event":
                    if control is not None:
                        control.report_event(NativeWorkerEvent.from_payload(message.payload))
                    continue
                if terminal is not None:
                    raise NativeWorkerExecutionError(
                        "native workerが複数のterminal messageを返しました。",
                        error_type="DuplicateWorkerResult",
                    )
                terminal = message
        except EOFError:
            pass
        except (TypeError, ValueError) as exc:
            raise NativeWorkerExecutionError(
                f"native workerから不正なIPC messageを受信しました: {exc}",
                error_type="InvalidWorkerMessage",
            ) from exc
        return terminal

    @staticmethod
    def _resolve_terminal(message: NativeWorkerMessage) -> AgentRunResult:
        if message.message_type == "result":
            return NativeWorkerResult.from_payload(message.payload).to_run_result()
        error = NativeWorkerError.from_payload(message.payload)
        if error.error_type == "TimeoutError":
            raise asyncio.TimeoutError(error.message)
        if error.error_type == "MaxTurnsReachedError":
            from agent_delegation import manager

            metadata = dict(error.metadata)
            assistant_text = str(metadata.pop("assistant_text", ""))
            raise manager.MaxTurnsReachedError(error.message, assistant_text, metadata)
        if error.error_type == "MemoryLimitExceededError":
            from agent_delegation import manager

            raise manager.MemoryLimitExceededError(error.message, metadata=error.metadata)
        raise NativeWorkerExecutionError(
            f"native worker内で{error.error_type}が発生しました: {error.message}",
            error_type=error.error_type,
            metadata=error.metadata,
        )

    @staticmethod
    def _join_after_terminal(
        process: Any,
        root_identity: NativeWorkerProcessIdentity | None = None,
        known_identities: list[NativeWorkerProcessIdentity] | None = None,
    ) -> None:
        process.join(timeout=WORKER_STOP_GRACE_SECONDS)
        NativeWorkerSupervisor._stop_worker(process, root_identity, known_identities)

    @staticmethod
    def _stop_worker(
        process: Any,
        root_identity: NativeWorkerProcessIdentity | None = None,
        known_identities: list[NativeWorkerProcessIdentity] | None = None,
    ) -> None:
        if root_identity is not None:
            stop_native_worker_process_tree(
                root_identity,
                known_identities or [root_identity],
                grace_seconds=WORKER_STOP_GRACE_SECONDS,
            )
        if not process.is_alive():
            process.join(timeout=0)
            return
        process.terminate()
        process.join(timeout=WORKER_STOP_GRACE_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=WORKER_STOP_GRACE_SECONDS)

    @staticmethod
    def _unexpected_exit_message(exitcode: int | None) -> str:
        if exitcode is None:
            return "native workerの終了状態を確認できず、結果も返されませんでした。"
        if exitcode == 0:
            return "native workerが結果を返さず終了しました。"
        if exitcode < 0:
            try:
                signal_name = signal.Signals(-exitcode).name
            except ValueError:
                signal_name = f"signal {-exitcode}"
            return f"native workerが{signal_name}で終了し、結果を返しませんでした。"
        return f"native workerが終了コード{exitcode}で異常終了し、結果を返しませんでした。"
