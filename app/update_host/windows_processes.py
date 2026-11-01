"""Windowsで更新hostが所有するprocess treeを安全に停止する補助。"""

from __future__ import annotations

import ctypes
import math
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any


__all__ = ["is_owned_process_creation", "terminate_owned_process_tree"]


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SYNCHRONIZE = 0x00100000
_PROCESS_ACCESS = (
    _PROCESS_QUERY_LIMITED_INFORMATION
    | _PROCESS_TERMINATE
    | _PROCESS_SYNCHRONIZE
)
_TH32CS_SNAPPROCESS = 0x00000002
_ERROR_NO_MORE_FILES = 18
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_ROUNDS = 8
_MAX_WAIT_MILLISECONDS = 0xFFFFFFFE  # Win32のINFINITE値は使用しない。


def is_owned_process_creation(parent_creation: int, child_creation: int) -> bool:
    """親PIDの再利用を避けるため、作成時刻だけで子の所有を判定する。

    Toolhelpの親PIDは履歴を持たないため、古いprocessが再利用された親PIDを
    指していても、それだけでは現在の更新processの子と判定できない。Windowsの
    FILETIME（100ns単位）の作成時刻が親以上である候補だけを対象にする。
    """

    if (
        isinstance(parent_creation, bool)
        or not isinstance(parent_creation, int)
        or isinstance(child_creation, bool)
        or not isinstance(child_creation, int)
    ):
        return False
    return child_creation >= parent_creation


@dataclass(frozen=True)
class _ProcessEntry:
    pid: int
    parent_pid: int


@dataclass
class _OwnedProcess:
    pid: int
    creation: int
    handle: Any


@dataclass(frozen=True)
class _WindowsApi:
    create_snapshot: Any
    process32_first: Any
    process32_next: Any
    open_process: Any
    get_process_times: Any
    terminate_process: Any
    wait_for_single_object: Any
    close_handle: Any
    process_entry_type: Any
    filetime_type: Any
    handle_type: Any


def _load_windows_api() -> _WindowsApi:
    """ctypesのWindows bindingを関数呼出時だけ構築する。"""

    # ctypes.WinDLLはLinuxにも属性があるとは限らないため、import時には触れない。
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    handle_type = wintypes.HANDLE
    filetime_type = wintypes.FILETIME

    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = handle_type

    process32_first = kernel32.Process32FirstW
    process32_first.argtypes = [handle_type, ctypes.POINTER(_PROCESSENTRY32W)]
    process32_first.restype = wintypes.BOOL

    process32_next = kernel32.Process32NextW
    process32_next.argtypes = [handle_type, ctypes.POINTER(_PROCESSENTRY32W)]
    process32_next.restype = wintypes.BOOL

    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = handle_type

    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        handle_type,
        ctypes.POINTER(filetime_type),
        ctypes.POINTER(filetime_type),
        ctypes.POINTER(filetime_type),
        ctypes.POINTER(filetime_type),
    ]
    get_process_times.restype = wintypes.BOOL

    terminate_process = kernel32.TerminateProcess
    terminate_process.argtypes = [handle_type, wintypes.UINT]
    terminate_process.restype = wintypes.BOOL

    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [handle_type, wintypes.DWORD]
    wait_for_single_object.restype = wintypes.DWORD

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [handle_type]
    close_handle.restype = wintypes.BOOL

    return _WindowsApi(
        create_snapshot=create_snapshot,
        process32_first=process32_first,
        process32_next=process32_next,
        open_process=open_process,
        get_process_times=get_process_times,
        terminate_process=terminate_process,
        wait_for_single_object=wait_for_single_object,
        close_handle=close_handle,
        process_entry_type=_PROCESSENTRY32W,
        filetime_type=filetime_type,
        handle_type=handle_type,
    )


def _handle_value(handle: Any) -> int | None:
    if handle is None:
        return None
    value = getattr(handle, "value", getattr(handle, "handle", handle))
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value == 0 or value == -1 or value == _INVALID_HANDLE_VALUE:
        return None
    return value


def _as_handle(api: _WindowsApi, value: int) -> Any:
    return api.handle_type(value)


def _close_handle(api: _WindowsApi, handle: Any) -> bool:
    try:
        return bool(api.close_handle(handle))
    except (OSError, ctypes.ArgumentError, TypeError, ValueError):
        return False


def _snapshot_processes(api: _WindowsApi) -> list[_ProcessEntry] | None:
    snapshot = api.create_snapshot(_TH32CS_SNAPPROCESS, 0)
    snapshot_value = _handle_value(snapshot)
    if snapshot_value is None:
        return None
    snapshot_handle = _as_handle(api, snapshot_value)
    entries: list[_ProcessEntry] = []
    close_ok = True
    try:
        entry = api.process_entry_type()
        entry.dwSize = ctypes.sizeof(entry)
        if not api.process32_first(snapshot_handle, ctypes.byref(entry)):
            return None
        while True:
            entries.append(
                _ProcessEntry(
                    pid=int(entry.th32ProcessID),
                    parent_pid=int(entry.th32ParentProcessID),
                )
            )
            if api.process32_next(snapshot_handle, ctypes.byref(entry)):
                continue
            if ctypes.get_last_error() != _ERROR_NO_MORE_FILES:
                return None
            break
    finally:
        close_ok = _close_handle(api, snapshot_handle)
    if not close_ok:
        return None
    return entries


def _creation_time(api: _WindowsApi, handle: Any) -> int | None:
    created = api.filetime_type()
    exited = api.filetime_type()
    kernel = api.filetime_type()
    user = api.filetime_type()
    if not api.get_process_times(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        return None
    return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)


def _open_process(api: _WindowsApi, pid: int) -> Any | None:
    handle = api.open_process(_PROCESS_ACCESS, False, pid)
    value = _handle_value(handle)
    if value is None:
        return None
    return _as_handle(api, value)


def _wait_status(api: _WindowsApi, handle: Any, milliseconds: int) -> int:
    return int(api.wait_for_single_object(handle, milliseconds))


def _remaining_milliseconds(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0
    return min(
        _MAX_WAIT_MILLISECONDS,
        max(1, int(math.ceil(remaining * 1000))),
    )


def _collect_owned_descendants(
    api: _WindowsApi,
    entries: list[_ProcessEntry],
    *,
    root_pid: int,
    root_creation: int,
    root_stopped: bool,
    owned: dict[int, _OwnedProcess],
) -> tuple[bool, set[int]]:
    """snapshotから所有候補を追加し、取得したhandleをownedへ保持する。"""

    by_parent: dict[int, list[_ProcessEntry]] = {}
    by_pid: dict[int, _ProcessEntry] = {}
    for entry in entries:
        if entry.pid <= 0:
            continue
        by_parent.setdefault(entry.parent_pid, []).append(entry)
        by_pid[entry.pid] = entry

    root_entry = by_pid.get(root_pid)
    if root_entry is None and not root_stopped:
        return False, set()
    if root_entry is None and root_stopped:
        # rootが消えた後の同じPPIDは、終了済みrootの子か、再利用されたPIDの
        # 無関係な子かをsnapshotだけでは区別できない。推測で殺さずfail closed。
        if any(entry.parent_pid == root_pid and entry.pid not in owned for entry in entries):
            return False, set()
    if root_entry is not None:
        root_handle = _open_process(api, root_pid)
        if root_handle is None:
            return False, set()
        try:
            current_root_creation = _creation_time(api, root_handle)
        finally:
            close_ok = _close_handle(api, root_handle)
        if not close_ok or current_root_creation != root_creation:
            return False, set()

    newly_owned: set[int] = set()
    queue: list[tuple[int, int]] = []
    queued: set[int] = set()
    if root_entry is not None:
        queue.append((root_pid, root_creation))
        queued.add(root_pid)

    # Rootが終了してsnapshotから消えた後も、既に保持した子の下に生じた
    # descendantは追跡できる。終了済みhandleのPIDは再利用の可能性があるため、
    # 新しい子の親としては扱わない。
    for process in tuple(owned.values()):
        entry = by_pid.get(process.pid)
        if entry is None:
            status = _wait_status(api, process.handle, 0)
            if status == _WAIT_FAILED:
                return False, set()
            if status == _WAIT_TIMEOUT:
                # 実行中のknown processがsnapshotから消えた状態では、その下の
                # processを親PIDだけで安全に所有判定できない。
                return False, set()
            if any(
                child.parent_pid == process.pid and child.pid not in owned
                for child in entries
            ):
                return False, set()
            continue
        status = _wait_status(api, process.handle, 0)
        if status == _WAIT_FAILED:
            return False, set()
        if status == _WAIT_OBJECT_0:
            if any(
                child.parent_pid == process.pid and child.pid not in owned
                for child in entries
            ):
                return False, set()
            continue
        current_handle = _open_process(api, process.pid)
        if current_handle is None:
            return False, set()
        try:
            current_creation = _creation_time(api, current_handle)
        finally:
            close_ok = _close_handle(api, current_handle)
        if not close_ok or current_creation != process.creation:
            return False, set()
        if process.pid not in queued:
            queue.append((process.pid, process.creation))
            queued.add(process.pid)

    while queue:
        parent_pid, parent_creation = queue.pop(0)
        for entry in by_parent.get(parent_pid, ()):
            if entry.pid == root_pid or entry.pid in queued:
                continue
            candidate = _open_process(api, entry.pid)
            if candidate is None:
                return False, newly_owned
            creation = _creation_time(api, candidate)
            if creation is None:
                _close_handle(api, candidate)
                return False, newly_owned
            if not is_owned_process_creation(parent_creation, creation):
                if not _close_handle(api, candidate):
                    return False, newly_owned
                continue
            previous = owned.get(entry.pid)
            if previous is not None:
                if creation != previous.creation:
                    _close_handle(api, candidate)
                    return False, newly_owned
                if not _close_handle(api, candidate):
                    return False, newly_owned
                queue.append((entry.pid, previous.creation))
                queued.add(entry.pid)
                continue
            owned[entry.pid] = _OwnedProcess(entry.pid, creation, candidate)
            newly_owned.add(entry.pid)
            queue.append((entry.pid, creation))
            queued.add(entry.pid)
    return True, newly_owned


def _terminate_handle(api: _WindowsApi, handle: Any) -> bool:
    status = _wait_status(api, handle, 0)
    if status == _WAIT_OBJECT_0:
        return True
    if status == _WAIT_FAILED:
        return False
    if status != _WAIT_TIMEOUT:
        return False
    try:
        return bool(api.terminate_process(handle, 1))
    except (OSError, ctypes.ArgumentError, TypeError, ValueError):
        return False


def _wait_for_handles(api: _WindowsApi, handles: list[Any], deadline: float) -> bool:
    for handle in handles:
        milliseconds = _remaining_milliseconds(deadline)
        if milliseconds <= 0:
            return False
        status = _wait_status(api, handle, milliseconds)
        if status != _WAIT_OBJECT_0:
            return False
    return True


def terminate_owned_process_tree(process: subprocess.Popen, timeout: float) -> bool:
    """保持中のWindows processと、作成時刻で所有を確認できる子を停止する。

    PIDだけを指定する終了コマンドは使用しない。rootのPopen handleは呼出側の
    所有物なので閉じず、子だけをOpenProcessしたhandleとして保持・終了する。
    Windows以外、実Popenでない値、APIの不確実な失敗はすべてFalseを返す。
    """

    if os.name != "nt" or not isinstance(process, subprocess.Popen):
        return False
    try:
        timeout = float(timeout)
    except (OSError, TypeError, ValueError):
        return False
    if not math.isfinite(timeout) or timeout <= 0:
        return False

    try:
        root_pid = int(process.pid)
        root_handle_value = _handle_value(getattr(process, "_handle", None))
    except (TypeError, ValueError, AttributeError):
        return False
    if root_pid <= 0 or root_handle_value is None:
        return False

    owned: dict[int, _OwnedProcess] = {}
    success = False
    api: _WindowsApi | None = None
    try:
        api = _load_windows_api()
        root_handle = _as_handle(api, root_handle_value)
        root_creation = _creation_time(api, root_handle)
        if root_creation is None:
            return False
        deadline = time.monotonic() + timeout
        root_stopped = False

        for _round in range(_MAX_ROUNDS):
            if _remaining_milliseconds(deadline) <= 0:
                break
            entries = _snapshot_processes(api)
            if entries is None:
                break
            collected, _newly_owned = _collect_owned_descendants(
                api,
                entries,
                root_pid=root_pid,
                root_creation=root_creation,
                root_stopped=root_stopped,
                owned=owned,
            )
            if not collected:
                break

            if not root_stopped:
                root_status = _wait_status(api, root_handle, 0)
                if root_status == _WAIT_TIMEOUT:
                    if not _terminate_handle(api, root_handle):
                        break
                elif root_status != _WAIT_OBJECT_0:
                    break
                root_stopped = True

            for child in sorted(owned.values(), key=lambda item: item.creation):
                if not _terminate_handle(api, child.handle):
                    break
            else:
                if not _wait_for_handles(
                    api,
                    [root_handle]
                    + [child.handle for child in owned.values()],
                    deadline,
                ):
                    break
                if _remaining_milliseconds(deadline) <= 0:
                    break
                after = _snapshot_processes(api)
                if after is None:
                    break
                before = set(owned)
                collected, newly_owned = _collect_owned_descendants(
                    api,
                    after,
                    root_pid=root_pid,
                    root_creation=root_creation,
                    root_stopped=True,
                    owned=owned,
                )
                if not collected:
                    break
                if not newly_owned and before == set(owned):
                    success = True
                    break
                continue
            break
    except (OSError, ctypes.ArgumentError, RuntimeError, TypeError, ValueError):
        success = False
    finally:
        if api is not None:
            for child in owned.values():
                if not _close_handle(api, child.handle):
                    success = False
    return success
