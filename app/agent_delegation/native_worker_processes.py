"""Parent-owned native worker identity, registry, and process-tree cleanup."""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import psutil

import constants
from file_lock_utils import safe_json_read, safe_json_update


logger = logging.getLogger(__name__)


def _create_time_tolerance_seconds() -> float:
    return 0.001 if os.name == "nt" else 0.000001


CREATE_TIME_TOLERANCE_SECONDS = _create_time_tolerance_seconds()


@dataclass(frozen=True)
class NativeWorkerProcessIdentity:
    task_id: str
    run_token: str
    pid: int
    create_time: float
    started_at: float

    def to_record(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_token": self.run_token,
            "pid": self.pid,
            "create_time": self.create_time,
            "started_at": self.started_at,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "NativeWorkerProcessIdentity":
        if set(record) != {"task_id", "run_token", "pid", "create_time", "started_at"}:
            raise ValueError("native worker registry record has unexpected fields")
        identity = cls(
            task_id=str(record["task_id"]),
            run_token=str(record["run_token"]),
            pid=int(record["pid"]),
            create_time=float(record["create_time"]),
            started_at=float(record["started_at"]),
        )
        if not identity.task_id or not identity.run_token or identity.pid <= 1:
            raise ValueError("native worker registry record is incomplete")
        return identity


def native_worker_registry_path() -> Path:
    return Path(constants.METADATA_DIR) / "agent_delegation" / "native_workers.json"


def _protected_pids() -> set[int]:
    return {1, os.getpid(), os.getppid()}


def _is_safe_pid(pid: int) -> bool:
    return int(pid) > 1 and int(pid) not in _protected_pids()


def _process_create_time(process: psutil.Process) -> float:
    if sys.platform.startswith("linux"):
        stat_text = Path(f"/proc/{process.pid}/stat").read_text(encoding="utf-8")
        closing_paren = stat_text.rfind(")")
        fields_after_command = stat_text[closing_paren + 2:].split()
        start_ticks = int(fields_after_command[19])
        return start_ticks / float(os.sysconf("SC_CLK_TCK"))
    return float(process.create_time())


def _identity_is_from_current_boot(identity: NativeWorkerProcessIdentity) -> bool:
    if not sys.platform.startswith("linux"):
        return True
    try:
        return identity.started_at >= float(psutil.boot_time()) - 5.0
    except (OSError, RuntimeError):
        return False


def _matching_process(identity: NativeWorkerProcessIdentity) -> psutil.Process | None:
    if not _is_safe_pid(identity.pid):
        logger.warning("refused protected native worker pid=%s", identity.pid)
        return None
    if not _identity_is_from_current_boot(identity):
        logger.warning("native worker pid=%s belongs to a previous boot; refusing signal", identity.pid)
        return None
    try:
        process = psutil.Process(identity.pid)
        actual_create_time = _process_create_time(process)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except (psutil.AccessDenied, OSError):
        logger.warning("cannot verify native worker pid=%s; refusing signal", identity.pid)
        return None
    if abs(actual_create_time - identity.create_time) > CREATE_TIME_TOLERANCE_SECONDS:
        logger.warning(
            "native worker pid=%s create_time mismatch expected=%s actual=%s; refusing signal",
            identity.pid,
            identity.create_time,
            actual_create_time,
        )
        return None
    return process


def capture_process_identity(*, task_id: str, run_token: str, pid: int, started_at: float) -> NativeWorkerProcessIdentity:
    provisional = NativeWorkerProcessIdentity(str(task_id), str(run_token), int(pid), 0.0, float(started_at))
    if not _is_safe_pid(provisional.pid):
        raise ValueError(f"protected pid cannot be registered: {provisional.pid}")
    process = psutil.Process(provisional.pid)
    return NativeWorkerProcessIdentity(
        task_id=provisional.task_id,
        run_token=provisional.run_token,
        pid=provisional.pid,
        create_time=_process_create_time(process),
        started_at=provisional.started_at,
    )


def _snapshot_descendants(root: NativeWorkerProcessIdentity) -> list[NativeWorkerProcessIdentity]:
    process = _matching_process(root)
    if process is None:
        return []
    identities: list[NativeWorkerProcessIdentity] = []

    def visit(parent: psutil.Process) -> None:
        try:
            children = parent.children(recursive=False)
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied, OSError):
            return
        for child in children:
            visit(child)
            try:
                identity = capture_process_identity(
                    task_id=root.task_id,
                    run_token=root.run_token,
                    pid=child.pid,
                    started_at=root.started_at,
                )
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied, OSError, ValueError):
                continue
            identities.append(identity)

    visit(process)
    return identities


def _registry_records() -> list[dict[str, Any]]:
    data = safe_json_read(str(native_worker_registry_path()), default={"workers": []})
    if not isinstance(data, dict) or not isinstance(data.get("workers"), list):
        return []
    return [record for record in data["workers"] if isinstance(record, dict)]


def _replace_registry_run(run_token: str, identities: Iterable[NativeWorkerProcessIdentity]) -> None:
    path = native_worker_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    replacement = [identity.to_record() for identity in identities]

    def update(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            data = {"workers": []}
        workers = data.get("workers") if isinstance(data.get("workers"), list) else []
        data["workers"] = [
            record for record in workers
            if not isinstance(record, dict) or str(record.get("run_token") or "") != run_token
        ] + replacement
        return data

    if not safe_json_update(str(path), update, default={"workers": []}):
        raise OSError("native worker registry update failed")


def refresh_native_worker_registry(
    root: NativeWorkerProcessIdentity,
    known: list[NativeWorkerProcessIdentity],
) -> list[NativeWorkerProcessIdentity]:
    fresh_descendants = _snapshot_descendants(root)
    combined = [*fresh_descendants, *known, root]
    refreshed = list({identity.pid: identity for identity in combined}.values())
    old_signature = {(identity.pid, identity.create_time) for identity in known}
    new_signature = {(identity.pid, identity.create_time) for identity in refreshed}
    if old_signature != new_signature or not native_worker_registry_path().exists():
        _replace_registry_run(root.run_token, refreshed)
    return refreshed


def remove_native_worker_registry_run(run_token: str) -> None:
    _replace_registry_run(str(run_token), [])


def _terminate_identities_leaf_first(
    identities: Iterable[NativeWorkerProcessIdentity],
    *,
    grace_seconds: float,
) -> list[int]:
    matched: list[tuple[NativeWorkerProcessIdentity, psutil.Process]] = []
    seen: set[int] = set()
    for identity in identities:
        if identity.pid in seen:
            continue
        seen.add(identity.pid)
        process = _matching_process(identity)
        if process is not None:
            matched.append((identity, process))
    stopped: list[int] = []
    for identity, process in matched:
        try:
            process.terminate()
            stopped.append(identity.pid)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, OSError):
            logger.warning("cannot terminate native worker descendant pid=%s", identity.pid)
    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    while time.monotonic() < deadline:
        if not any(_matching_process(identity) is not None for identity, _process in matched):
            return stopped
        time.sleep(0.02)
    for identity, _process in matched:
        process = _matching_process(identity)
        if process is None:
            continue
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, OSError):
            logger.warning("cannot kill native worker descendant pid=%s", identity.pid)
    return stopped


def _order_identities_leaf_first(
    identities: Iterable[NativeWorkerProcessIdentity],
) -> list[NativeWorkerProcessIdentity]:
    unique = {identity.pid: identity for identity in identities}
    parent_by_pid: dict[int, int] = {}
    for pid, identity in unique.items():
        process = _matching_process(identity)
        if process is None:
            continue
        try:
            parent_by_pid[pid] = int(process.ppid())
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied, OSError):
            continue

    def depth(pid: int) -> int:
        value = 0
        seen: set[int] = set()
        current = pid
        while current in parent_by_pid and parent_by_pid[current] in unique and current not in seen:
            seen.add(current)
            current = parent_by_pid[current]
            value += 1
        return value

    return sorted(unique.values(), key=lambda item: (depth(item.pid), item.create_time), reverse=True)


def stop_native_worker_process_tree(
    root: NativeWorkerProcessIdentity,
    known: list[NativeWorkerProcessIdentity],
    *,
    grace_seconds: float,
) -> list[int]:
    fresh = _snapshot_descendants(root)
    descendants = [identity for identity in [*fresh, *known] if identity.pid != root.pid]
    stopped = _terminate_identities_leaf_first(descendants, grace_seconds=grace_seconds)
    late_descendants = _snapshot_descendants(root)
    late = [identity for identity in late_descendants if identity.pid not in {item.pid for item in descendants}]
    if late:
        stopped.extend(_terminate_identities_leaf_first(late, grace_seconds=min(grace_seconds, 0.5)))
    return stopped


def reconcile_native_worker_registry(*, grace_seconds: float = 1.0) -> list[dict[str, Any]]:
    records = _registry_records()
    identities: list[NativeWorkerProcessIdentity] = []
    for record in records:
        try:
            identities.append(NativeWorkerProcessIdentity.from_record(record))
        except (TypeError, ValueError):
            logger.warning("discarded invalid native worker registry record")
    outcomes: list[dict[str, Any]] = []
    by_run: dict[str, list[NativeWorkerProcessIdentity]] = {}
    for identity in identities:
        by_run.setdefault(identity.run_token, []).append(identity)
    for run_token, run_identities in by_run.items():
        matched = [identity for identity in run_identities if _matching_process(identity) is not None]
        stopped = _terminate_identities_leaf_first(
            _order_identities_leaf_first(matched),
            grace_seconds=grace_seconds,
        )
        outcomes.append({
            "task_id": run_identities[0].task_id,
            "run_token": run_token,
            "matched_pids": [identity.pid for identity in matched],
            "stopped_pids": stopped,
        })
    if records:
        path = native_worker_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_json_update(str(path), lambda _data: {"workers": []}, default={"workers": []})
    return outcomes
