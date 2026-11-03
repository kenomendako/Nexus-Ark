"""初回更新失敗の非秘密診断。復旧receiptや署名検証には使用しない。"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import stat
import sys
from collections.abc import Mapping
import uuid

STEPS = frozenset({
    'unknown', 'download', 'runtime', 'copy', 'environment', 'trial', 'switch',
    'write_swap_intent', 'park_old_root', 'write_old_parked', 'promote_new_root',
    'write_new_promoted', 'verify_promoted_root', 'write_committed',
})
EXCEPTIONS = frozenset({
    'OSError', 'PermissionError', 'FileNotFoundError', 'FileExistsError',
    'NotADirectoryError', 'IsADirectoryError', 'RuntimeError', 'ValueError',
    'TransactionError', 'UnknownTransactionError', 'UnsafePathError',
    'ManualRecoveryRequired', 'BootstrapError',
})
RECOVERY_STATES = frozenset({
    'unknown', 'preparing', 'prepared', 'swap_intent', 'old_parked',
    'new_promoted', 'committed', 'rolled_back', 'manual_recovery_required',
})

# ``windows_context`` is deliberately a flat, closed schema.  In particular,
# it must never become a generic exception/environment dump: this detail can be
# displayed to a user and is also copied into a failure sidecar.
_WINDOWS_CONTEXT_WINERRORS = frozenset({
    'root_delete_winerror',
    'parent_delete_child_winerror',
    'workspace_add_subdir_winerror',
})
_WINDOWS_CONTEXT_BOOLEANS = frozenset({
    'cwd_in_old_root',
    'executable_in_old_root',
    'uv_cache_in_old_root',
    'uv_python_store_in_old_root',
})
_WINDOWS_CONTEXT_KEYS = _WINDOWS_CONTEXT_WINERRORS | _WINDOWS_CONTEXT_BOOLEANS
_MISSING = object()


def _mapping_value(value, key):
    """Return one allowlisted mapping value without enumerating untrusted keys."""

    if not isinstance(value, Mapping):
        return _MISSING
    try:
        # Calling ``get`` for known keys only is intentional.  It avoids
        # traversing a potentially huge/cyclic dictionary or copying unknown
        # values into a diagnostic payload.
        return value.get(key, _MISSING)
    except BaseException:
        # Diagnostics are best-effort and must not replace the update error.
        return _MISSING


def _safe_attribute(value, name):
    """Read an exception/foreign object attribute without exposing its value."""

    try:
        return getattr(value, name)
    except BaseException:
        return _MISSING


def _bounded_integer(value, maximum):
    """Accept only a real bounded integer (``bool`` is deliberately rejected)."""

    if type(value) is int and 0 <= value <= maximum:
        return value
    return None


def _sanitize_windows_context(value):
    """Copy only the fixed, non-secret Windows context fields."""

    sanitized = {}
    for key in _WINDOWS_CONTEXT_WINERRORS:
        number = _bounded_integer(_mapping_value(value, key), 65535)
        if number is not None:
            sanitized[key] = number
    for key in _WINDOWS_CONTEXT_BOOLEANS:
        boolean = _mapping_value(value, key)
        if type(boolean) is bool:
            sanitized[key] = boolean
    return sanitized


def _sanitize_failure_detail(detail):
    """Re-sanitize caller-provided detail before returning or persisting it."""

    result = {}
    for key, allowed in (
        ('step', STEPS),
        ('exception_type', EXCEPTIONS | {'unknown'}),
        ('recovery_state', RECOVERY_STATES),
    ):
        value = _mapping_value(detail, key)
        result[key] = value if type(value) is str and value in allowed else 'unknown'
    for key, maximum in (('errno', 4095), ('winerror', 65535)):
        number = _bounded_integer(_mapping_value(detail, key), maximum)
        if number is not None:
            result[key] = number
    windows_context = _sanitize_windows_context(_mapping_value(detail, 'windows_context'))
    if windows_context:
        result['windows_context'] = windows_context
    return result


def _normalized_lexical_path(value):
    """Normalize a path without resolving links or touching the filesystem."""

    try:
        value = os.fspath(value)
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        if not isinstance(value, str) or not value:
            return None
        return os.path.normcase(os.path.abspath(value))
    except BaseException:
        return None


def _is_lexically_under(candidate, root):
    """Return a boundary-aware lexical containment result or ``None`` unknown."""

    normalized_root = _normalized_lexical_path(root)
    normalized_candidate = _normalized_lexical_path(candidate)
    if normalized_root is None or normalized_candidate is None:
        return None
    try:
        if os.path.splitdrive(normalized_root)[0] != os.path.splitdrive(normalized_candidate)[0]:
            return False
        # commonpath, rather than a string prefix, keeps ``old`` separate from
        # ``old-other`` and rejects paths on another Windows drive.
        return os.path.commonpath((normalized_root, normalized_candidate)) == normalized_root
    except BaseException:
        return None


def _last_winerror():
    """Read ctypes' last error as a bounded integer, never as exception text."""

    try:
        value = ctypes.get_last_error()
    except BaseException:
        return None
    return _bounded_integer(value, 65535)


def _error_winerror(error):
    value = _safe_attribute(error, 'winerror')
    # ctypes呼び出し自体の例外で、前回のWinErrorを今回の結果と誤認しない。
    return _bounded_integer(value, 65535)


def _invalid_handle(handle):
    """Recognize CreateFileW failure without stringifying a foreign handle."""

    if handle is None:
        return True
    value = _safe_attribute(handle, 'value')
    if value is _MISSING:
        value = handle
    invalid_values = {-1, 0}
    try:
        invalid_values.add(ctypes.c_void_p(-1).value)
    except BaseException:
        pass
    return type(value) is int and value in invalid_values


def _create_file_probe(create_file, close_handle, location, access):
    """Open one directory handle, capture only WinError, and always close it."""

    try:
        path = os.fspath(location)
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not isinstance(path, str):
            return None
        handle = create_file(path, access, 7, None, 3, 0x02200000, None)
    except BaseException as error:
        return _error_winerror(error)
    if _invalid_handle(handle):
        return _last_winerror()
    closed = False
    try:
        # There is intentionally no inspection while the handle is held.
        pass
    finally:
        try:
            close_result = close_handle(handle)
            closed = close_result is True or (type(close_result) is int and close_result != 0)
        except BaseException:
            # A failed close is unknown; it must not mask the update failure.
            closed = False
    return 0 if closed else None


def capture_rename_context(root: Path, workspace: Path) -> dict:
    """Capture fixed Windows rename observations without touching user data.

    The function performs three read-only ``CreateFileW`` probes.  It never
    enumerates a directory, writes/renames anything, changes ACLs, or returns
    paths.  On non-Windows hosts it returns an empty dictionary so callers can
    invoke it unconditionally from the transaction failure path.
    """

    if os.name != 'nt':
        return {}

    try:
        current_directory = os.getcwd()
    except BaseException:
        current_directory = _MISSING
    context = {}
    cwd_in_root = _is_lexically_under(current_directory, root)
    if type(cwd_in_root) is bool:
        context['cwd_in_old_root'] = cwd_in_root
    executable_in_root = _is_lexically_under(_safe_attribute(sys, 'executable'), root)
    if type(executable_in_root) is bool:
        context['executable_in_old_root'] = executable_in_root
    # Environment variables are omitted when absent so "not configured" is
    # not confused with a configured path outside the old root.
    for environment_name, result_key in (
        ('UV_CACHE_DIR', 'uv_cache_in_old_root'),
        ('UV_PYTHON_INSTALL_DIR', 'uv_python_store_in_old_root'),
    ):
        try:
            value = os.environ.get(environment_name, _MISSING)
        except BaseException:
            value = _MISSING
        if value is not _MISSING:
            in_root = _is_lexically_under(value, root)
            if type(in_root) is bool:
                context[result_key] = in_root

    try:
        loader = getattr(ctypes, 'WinDLL')
        kernel32 = loader('kernel32', use_last_error=True)
        create_file = kernel32.CreateFileW
        close_handle = kernel32.CloseHandle
        # Explicit signatures are required for real 64-bit Windows handles;
        # fake callables in unit tests may not expose writable attributes.
        try:
            create_file.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ]
            create_file.restype = ctypes.c_void_p
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
        except BaseException:
            return _sanitize_windows_context(context)
    except BaseException:
        return context

    for key, location, access in (
        ('root_delete_winerror', root, 0x10000),
        ('parent_delete_child_winerror', _safe_attribute(root, 'parent'), 0x40),
        ('workspace_add_subdir_winerror', workspace, 0x4),
    ):
        error = _create_file_probe(create_file, close_handle, location, access)
        if error is not None:
            context[key] = error
    return _sanitize_windows_context(context)


def failure_detail(exc: Exception, *, phase: str = 'unknown') -> dict:
    """例外本文・filename・tracebackは一切公開せず、causeを上限付きで確認する。"""
    step = _safe_attribute(exc, 'bootstrap_step')
    if step is _MISSING:
        step = phase
    state = _safe_attribute(exc, 'bootstrap_recovery_state')
    if state is _MISSING:
        state = 'unknown'
    result = {
        'step': step if type(step) is str and step in STEPS else 'unknown',
        'exception_type': 'unknown',
        'recovery_state': state if type(state) is str and state in RECOVERY_STATES else 'unknown',
    }
    current = exc
    seen = set()
    for _ in range(8):
        if id(current) in seen:
            break
        seen.add(id(current))
        name = type(current).__name__
        result['exception_type'] = name if name in EXCEPTIONS else 'unknown'
        windows_context = _sanitize_windows_context(_safe_attribute(current, 'bootstrap_rename_context'))
        if windows_context and 'windows_context' not in result:
            result['windows_context'] = windows_context
        # 最初のOS例外のerrno/WinErrorを同じ原因から取り出す。
        if isinstance(current, OSError):
            for key, maximum in (('errno', 4095), ('winerror', 65535)):
                number = _bounded_integer(_safe_attribute(current, key), maximum)
                if number is not None:
                    result[key] = number
            break
        current = _safe_attribute(current, '__cause__')
        if current is _MISSING:
            current = None
        if current is None:
            break
    return result


def save_failure_detail(root: Path, detail: dict) -> bool:
    """所有を確認できる既存workspaceのdownload内だけへbest-effortで保存する。

    失敗時も元の更新失敗を隠さない。新規workspace作成、receipt変更、復旧判断はしない。
    """
    from .transaction import (
        _assert_safe_ancestors, _is_reparse_or_symlink, _read_known_receipt,
        workspace_path,
    )
    temporary = None
    try:
        workspace = workspace_path(root)
        _assert_safe_ancestors(workspace)
        receipt = _read_known_receipt(root, workspace)
        directory = workspace / 'download'
        _assert_safe_ancestors(directory)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or _is_reparse_or_symlink(directory, info):
            return False
        target = directory / 'bootstrap-failure.json'
        if os.path.lexists(target):
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or _is_reparse_or_symlink(target, info):
                return False
        # 呼び出し側から未検証の任意辞書を保存しない。windows_contextも
        # failure_detailと同じallowlistで再サニタイズする。
        payload = {'schema': 1, 'operation_id': receipt['operation_id']}
        payload.update(_sanitize_failure_detail(detail))
        candidate = directory / ('.bootstrap-failure.' + uuid.uuid4().hex + '.tmp')
        with candidate.open('x', encoding='utf-8') as stream:
            temporary = candidate
            json.dump(payload, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        return True
    except Exception:
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
