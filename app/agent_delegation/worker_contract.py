"""JSON-only contracts shared by native delegation workers and their supervisor."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from agent_delegation.types import AgentRunResult, AgentTaskSpec, DelegationScope


JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
NATIVE_WORKER_SETTING_KEYS = {
    "allow_web_tools",
    "deleg_rlimit_as_mb",
    "deleg_rlimit_cpu_seconds",
    "deleg_rlimit_fsize_mb",
    "deleg_rlimit_nproc",
    "deleg_review_internal_role",
    "deleg_review_iterations",
    "deleg_rss_headroom_mb",
    "deleg_rss_limit_mb",
    "native_spawn_canary_mode",
}
NATIVE_WORKER_CANARY_MODES = {"read", "web", "write", "full"}
NATIVE_WORKER_MAX_STEERS = 10
NATIVE_WORKER_MAX_STEER_CHARS = 2000


def _json_copy(value: Any, *, label: str) -> JSONValue:
    """Validate and detach an IPC payload without coercing runtime objects."""

    def validate(item: Any, path: str) -> None:
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise TypeError(f"{label}{path} contains a non-finite float")
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                validate(child, f"{path}[{index}]")
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise TypeError(f"{label}{path} contains a non-string key")
                validate(child, f"{path}.{key}")
            return
        raise TypeError(f"{label}{path} contains a non-JSON value: {type(item).__name__}")

    validate(value, "")
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _scope_to_payload(scope: DelegationScope) -> dict[str, JSONValue]:
    return {
        "root": scope.root,
        "tier": scope.tier,
        "exclude_dirs": list(scope.exclude_dirs),
        "exclude_files": list(scope.exclude_files),
    }


def task_spec_to_payload(spec: AgentTaskSpec) -> dict[str, JSONValue]:
    payload = asdict(spec)
    payload["extra_scopes"] = [_scope_to_payload(scope) for scope in spec.extra_scopes]
    payload["model_override"] = list(spec.model_override) if spec.model_override else None
    return _json_copy(payload, label="spec")  # type: ignore[return-value]


def task_spec_from_payload(payload: dict[str, Any]) -> AgentTaskSpec:
    data = _json_copy(payload, label="spec")
    if not isinstance(data, dict):  # pragma: no cover - guarded by the annotation/caller
        raise TypeError("spec must be a JSON object")
    scopes = [DelegationScope(**scope) for scope in data.get("extra_scopes", [])]
    model_override = data.get("model_override")
    return AgentTaskSpec(
        **{
            **data,
            "extra_scopes": scopes,
            "model_override": tuple(model_override) if model_override else None,
        }
    )


def resolve_native_spawn_canary_mode(settings: dict[str, Any]) -> str:
    raw_mode = str(settings.get("native_spawn_canary_mode") or "read").strip().lower()
    if raw_mode not in NATIVE_WORKER_CANARY_MODES:
        return "read"
    if raw_mode == "web" and not bool(settings.get("allow_web_tools")):
        return "read"
    return raw_mode


def native_worker_settings_snapshot(settings: dict[str, Any]) -> dict[str, JSONValue]:
    """Copy only non-secret native-loop settings required inside the worker."""

    snapshot = {key: settings[key] for key in NATIVE_WORKER_SETTING_KEYS if key in settings}
    mode = resolve_native_spawn_canary_mode(settings)
    snapshot["native_spawn_canary_mode"] = mode
    snapshot["allow_web_tools"] = bool(settings.get("allow_web_tools")) and mode in {"web", "write", "full"}
    copied = _json_copy(snapshot, label="settings")
    if not isinstance(copied, dict):  # pragma: no cover - constructed as a dict above
        raise TypeError("worker settings snapshot must be a JSON object")
    return copied


@dataclass(frozen=True)
class NativeWorkerRequest:
    spec: AgentTaskSpec
    settings: dict[str, Any]

    def to_payload(self) -> dict[str, JSONValue]:
        return {
            "spec": task_spec_to_payload(self.spec),
            "settings": _json_copy(self.settings, label="settings"),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NativeWorkerRequest":
        data = _json_copy(payload, label="request")
        if not isinstance(data, dict) or not isinstance(data.get("spec"), dict) or not isinstance(data.get("settings"), dict):
            raise TypeError("worker request requires JSON object spec and settings")
        return cls(spec=task_spec_from_payload(data["spec"]), settings=dict(data["settings"]))


@dataclass(frozen=True)
class NativeWorkerEvent:
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, JSONValue]:
        if self.event_type not in {"progress", "log"}:
            raise ValueError(f"unsupported native worker event: {self.event_type}")
        return {
            "event_type": self.event_type,
            "payload": _json_copy(self.payload, label="event.payload"),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NativeWorkerEvent":
        data = _json_copy(payload, label="event")
        if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
            raise TypeError("worker event requires a JSON object payload")
        event = cls(event_type=str(data.get("event_type") or ""), payload=dict(data["payload"]))
        event.to_payload()
        return event


@dataclass(frozen=True)
class NativeWorkerResult:
    assistant_text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, JSONValue]:
        return {
            "assistant_text": str(self.assistant_text),
            "metadata": _json_copy(self.metadata, label="result.metadata"),
        }

    @classmethod
    def from_run_result(cls, result: AgentRunResult) -> "NativeWorkerResult":
        return cls(assistant_text=result.assistant_text, metadata=dict(result.metadata))

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NativeWorkerResult":
        data = _json_copy(payload, label="result")
        if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
            raise TypeError("worker result requires a JSON object metadata")
        return cls(assistant_text=str(data.get("assistant_text") or ""), metadata=dict(data["metadata"]))

    def to_run_result(self) -> AgentRunResult:
        return AgentRunResult(assistant_text=self.assistant_text, metadata=dict(self.metadata))


@dataclass(frozen=True)
class NativeWorkerError:
    error_type: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, JSONValue]:
        return {
            "error_type": str(self.error_type),
            "message": str(self.message),
            "metadata": _json_copy(self.metadata, label="error.metadata"),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NativeWorkerError":
        data = _json_copy(payload, label="error")
        if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
            raise TypeError("worker error requires a JSON object metadata")
        return cls(
            error_type=str(data.get("error_type") or "RuntimeError"),
            message=str(data.get("message") or ""),
            metadata=dict(data["metadata"]),
        )


@dataclass(frozen=True)
class NativeWorkerMessage:
    message_type: str
    payload: dict[str, Any]

    def to_payload(self) -> dict[str, JSONValue]:
        if self.message_type not in {"event", "result", "error"}:
            raise ValueError(f"unsupported native worker message: {self.message_type}")
        return {
            "message_type": self.message_type,
            "payload": _json_copy(self.payload, label="message.payload"),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NativeWorkerMessage":
        data = _json_copy(payload, label="message")
        if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
            raise TypeError("worker message requires a JSON object payload")
        message = cls(message_type=str(data.get("message_type") or ""), payload=dict(data["payload"]))
        message.to_payload()
        return message


@dataclass(frozen=True)
class NativeWorkerControlMessage:
    control_type: str
    instruction: str = ""

    def to_payload(self) -> dict[str, JSONValue]:
        if self.control_type not in {"cancel", "steer"}:
            raise ValueError(f"unsupported native worker control: {self.control_type}")
        instruction = str(self.instruction or "")
        if self.control_type == "cancel" and instruction:
            raise ValueError("cancel control must not contain an instruction")
        if self.control_type == "steer":
            if not instruction.strip():
                raise ValueError("steer control requires an instruction")
            if len(instruction) > NATIVE_WORKER_MAX_STEER_CHARS:
                raise ValueError(f"steer control exceeds {NATIVE_WORKER_MAX_STEER_CHARS} characters")
        return {"control_type": self.control_type, "instruction": instruction}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NativeWorkerControlMessage":
        data = _json_copy(payload, label="control")
        if not isinstance(data, dict):
            raise TypeError("worker control must be a JSON object")
        message = cls(
            control_type=str(data.get("control_type") or ""),
            instruction=str(data.get("instruction") or ""),
        )
        message.to_payload()
        return message


class NativeWorkerExecutionError(RuntimeError):
    """A structured worker failure normalized by the parent supervisor."""

    def __init__(self, message: str, *, error_type: str = "RuntimeError", metadata: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_type = str(error_type or "RuntimeError")
        self.metadata = dict(metadata or {})


class NativeWorkerControl(Protocol):
    """Only parent services visible to the native execution loop."""

    def is_cancelled(self) -> bool:
        ...

    def drain_steer(self) -> list[str]:
        ...

    def report_event(self, event: NativeWorkerEvent) -> None:
        ...


class NullNativeWorkerControl:
    def is_cancelled(self) -> bool:
        return False

    def drain_steer(self) -> list[str]:
        return []

    def report_event(self, event: NativeWorkerEvent) -> None:
        event.to_payload()
