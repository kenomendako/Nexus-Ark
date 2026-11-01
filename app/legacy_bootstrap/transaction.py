"""旧版インストールを安全に新版へ切り替える sibling-directory transaction。

初回更新では、既存のインストールディレクトリを直接上書きしない。新版を同じ
ボリューム上の ``.Nexus Ark.nexus-secure-update/next-root`` に準備し、準備完了の
receipt を永続化してから、旧rootを ``previous`` に退避して新版を一回のrenameで
昇格する。途中でプロセスが終了しても、次回の :func:`recover_swap` が既知の
ディレクトリidentityだけを動かして、旧版の復元または検証済み新版の確定を行う。

このモジュールはアプリケーションを起動しない。新版のmanifest検証やlocalhost
起動確認は呼び出し側が完了させ、``guard`` / ``validate_new`` で結果だけを渡す。
receiptにはユーザーのファイル内容、コマンド、URL、秘密値を保存せず、操作GUIDと
旧rootの絶対パスdigest、ディレクトリidentityだけを記録する。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from update_host.runtime import _runtime_io_path


SCHEMA_VERSION = 1
RECEIPT_NAME = "receipt.json"
NEXT_ROOT_NAME = "next-root"
PREVIOUS_NAME = "previous"
_GUID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_TEMP_RE = re.compile(r"^\.receipt\.json\.([0-9a-f]{32})\.tmp$")
_REPARSE_POINT = 0x0400
MAX_ACQUISITION_BYTES = 1024 * 1024
_WORKSPACE_ENTRIES = frozenset(
    {RECEIPT_NAME, NEXT_ROOT_NAME, PREVIOUS_NAME, "download", "acquisition.json"}
)


class TransactionError(RuntimeError):
    """Transactionの安全条件またはファイル操作に関するエラー。"""


class UnknownTransactionError(TransactionError):
    """既知のreceiptで裏付けられないworkspaceを検出した。"""


class UnsafePathError(TransactionError):
    """symlink/reparse point、特殊ファイル、cross-volume等を検出した。"""


class ManualRecoveryRequired(TransactionError):
    """自動的に旧版・新版のいずれかを確定できず、手動復旧が必要。"""


def workspace_path(root: str | os.PathLike[str]) -> Path:
    """旧rootから決定的に求めるsibling workspaceを返す。

    ``resolve()``は意図的に使わない。呼び出し側がsymlink/reparseを検査する前に
    別の場所へ追従してしまわないよう、絶対化とnormcaseだけを行う。
    """

    normalized = _absolute_path(root)
    if not normalized.name:
        # ドライブroot等でも名前のないPathを作らず、明示的に拒否する。
        raise UnsafePathError("root must have a directory name")
    return normalized.parent / f".{normalized.name}.nexus-secure-update"


def create_workspace(root: str | os.PathLike[str]) -> Path:
    """新版の準備先を作成し、そのworkspaceのPathを返す。

    既に同名workspaceがある場合は、receiptで所有を確認した後に既知の途中状態を
    復旧し、再試行可能な状態なら ``.failed-GUID`` へ退避する。未知のworkspaceや
    manual recovery状態は決して触らず、例外にする。
    """

    root_path = _validate_root(root)
    target = workspace_path(root_path)
    _assert_safe_ancestors(target)
    parent_device = _device(target.parent)
    if _device(root_path.parent) != parent_device:
        raise UnsafePathError("workspace must be on the same volume as root")

    if os.path.lexists(target):
        _assert_workspace_shape(target)
        receipt = _read_known_receipt(root_path, target)
        if receipt["state"] == "committed":
            # 既に成功済みのworkspaceを、validatorなしで「再更新可能」と扱わない。
            # 親installerが先にrecover_swap(validate_new=...)を呼び、完全確認済み
            # ならそのままreturnする責務を持つ。
            raise TransactionError("existing workspace is already committed; recover it first")
        # 既知の途中状態だけをrecoverする。新版が既に昇格している場合は
        # validate_newが必要なので、ここではmanual状態として止める。
        recovered = recover_swap(root_path, target, validate_new=lambda _path: False)
        if recovered.get("state") not in {
            "preparing",
            "prepared",
            "swap_intent",
            "rolled_back",
            "committed",
        }:
            raise ManualRecoveryRequired("existing workspace requires manual recovery")
        _archive_known_workspace(root_path, target, receipt)

    try:
        target.mkdir()
        next_root = target / NEXT_ROOT_NAME
        next_root.mkdir()
    except FileExistsError as exc:
        raise UnknownTransactionError("workspace appeared during preparation") from exc
    except OSError as exc:
        raise TransactionError(f"workspace creation failed: {exc}") from exc

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "operation_id": uuid.uuid4().hex,
        "root_path_digest": _path_digest(root_path),
        "workspace_identity": _identity(target),
        "old_identity": _identity(root_path),
        "new_identity": None,
        "ready_manifest_digest": None,
        "state": "preparing",
    }
    try:
        _write_receipt(target, receipt)
    except Exception:
        # Receiptのないworkspaceは次の試行でunknown扱いになるため、ここでは
        # 作成した空workspaceだけを安全に戻す。外部から追加されたものは消さない。
        try:
            if _is_empty_directory(next_root) and _is_empty_directory(target, excluding={NEXT_ROOT_NAME}):
                next_root.rmdir()
                target.rmdir()
        except OSError:
            pass
        raise
    return target


def commit_swap(
    root: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
    *,
    ready_manifest_digest: str,
    guard: Callable[..., Any] | None = None,
    validate_new: Callable[..., Any] | None = None,
    fault: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """準備済み新版を旧rootへ昇格する。

    ``guard`` はrename前に一度だけ呼ばれる。引数を受け取る場合は
    ``(phase, root, workspace, receipt)``、引数なしの場合は ``guard()`` とする。
    falseを返した場合は切替を開始しない。``validate_new`` は昇格後、commit保存前に
    ``(root, workspace, receipt)`` で呼び、True以外なら保全して停止する。
    ``fault`` はテスト・障害注入用で、
    ``before_old_rename``, ``after_old_rename``, ``before_new_rename``,
    ``after_new_rename``, ``before_receipt_write``, ``after_receipt_write`` の
    phaseを受け取る。fault例外は呼び出し側へ再送出するが、同期的に復旧可能な
    旧rootはこの関数内で復元する。
    """

    root_path = _validate_root(root)
    workspace_path_value = _absolute_path(workspace)
    _validate_workspace_argument(root_path, workspace_path_value)
    receipt = _read_known_receipt(root_path, workspace_path_value)
    if receipt["state"] not in {"preparing", "prepared", "rolled_back"}:
        raise TransactionError(f"workspace is not ready for commit: {receipt['state']}")

    manifest_digest = _validate_digest(ready_manifest_digest, "ready_manifest_digest")
    next_root = workspace_path_value / NEXT_ROOT_NAME
    previous = workspace_path_value / PREVIOUS_NAME
    _assert_known_workspace_entries(workspace_path_value)
    _assert_safe_tree(root_path)
    _assert_safe_tree(next_root)
    if os.path.lexists(previous):
        raise UnsafePathError("existing previous backup is not empty/known")
    if _device(next_root.parent) != _device(root_path.parent):
        raise UnsafePathError("next-root must be on the same volume as root")

    old_identity = _identity(root_path)
    new_identity = _identity(next_root)
    if not _same_identity(old_identity, receipt["old_identity"]):
        raise UnknownTransactionError("root identity changed since workspace creation")
    receipt["old_identity"] = old_identity
    receipt["new_identity"] = new_identity
    receipt["ready_manifest_digest"] = manifest_digest

    if guard is not None:
        result = _invoke_hook(guard, "before_swap", root_path, workspace_path_value, receipt)
        if result is False:
            raise TransactionError("swap guard rejected the update")

    try:
        _persist_with_fault(workspace_path_value, receipt, state="swap_intent", fault=fault)
        _invoke_fault(fault, "before_old_rename")
        _rename_known(root_path, previous, old_identity)
        _invoke_fault(fault, "after_old_rename")
        _persist_with_fault(workspace_path_value, receipt, state="old_parked", fault=fault)

        _invoke_fault(fault, "before_new_rename")
        _rename_known(next_root, root_path, new_identity)
        _invoke_fault(fault, "after_new_rename")
        _persist_with_fault(workspace_path_value, receipt, state="new_promoted", fault=fault)
        if validate_new is not None and _invoke_hook(validate_new, root_path, workspace_path_value, receipt) is not True:
            raise TransactionError("promoted root verification failed")
        _persist_with_fault(workspace_path_value, receipt, state="committed", fault=fault)
        return dict(receipt)
    except Exception as exc:
        _recover_after_commit_failure(root_path, workspace_path_value, receipt)
        raise TransactionError(f"atomic swap failed: {exc}") from exc


def recover_swap(
    root: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
    *,
    validate_new: Callable[..., Any],
) -> dict[str, Any]:
    """既知receiptの途中状態を安全に復旧する。

    rootが既知の新版identityなら、``validate_new(root, workspace, receipt)`` が
    真を返した時だけcommittedにする（単引数のvalidatorも受け付ける）。検証できない
    場合はrootを動かさずmanual recoveryにする。
    commit前のroot不在で既知の旧backupがあれば、旧rootを復元する。commit後の
    root不在は外部操作の可能性があるため自動復元しない。未知identityや
    未知workspaceは触らず例外にする。
    """

    if not callable(validate_new):
        raise TypeError("validate_new must be callable")
    root_path = _absolute_path(root)
    workspace_path_value = _absolute_path(workspace)
    _validate_workspace_argument(root_path, workspace_path_value)
    receipt = _read_known_receipt(root_path, workspace_path_value)
    _assert_known_workspace_entries(workspace_path_value)
    next_root = workspace_path_value / NEXT_ROOT_NAME
    previous = workspace_path_value / PREVIOUS_NAME
    _assert_optional_safe_tree(next_root)
    _assert_optional_safe_tree(previous)
    _assert_optional_safe_tree(root_path)

    state = str(receipt["state"])
    root_exists = os.path.lexists(root_path)
    root_identity = _identity(root_path) if root_exists else None
    previous_exists = os.path.lexists(previous)
    previous_identity = _identity(previous) if previous_exists else None
    new_identity = receipt.get("new_identity")
    old_identity = receipt["old_identity"]

    if state == "committed":
        _require_identity(root_path, new_identity, "committed root")
        if not previous_exists or not _same_identity(previous_identity, old_identity):
            raise UnknownTransactionError("committed root is missing its known old backup")
        try:
            valid = _invoke_hook(validate_new, root_path, workspace_path_value, receipt)
        except Exception:
            valid = False
        if valid is True:
            return dict(receipt)
        _set_receipt_state(workspace_path_value, receipt, "manual_recovery_required")
        raise ManualRecoveryRequired("committed root could not be re-verified")

    if root_exists and _same_identity(root_identity, old_identity):
        # 旧rootが元の場所に残っているなら、切替は開始されていないか、既に
        # 同期的なrollbackを終えている。previousが同時に存在するのは不正。
        if previous_exists:
            raise UnknownTransactionError("old root and previous backup both exist")
        if state in {"old_parked", "new_promoted"}:
            _set_receipt_state(workspace_path_value, receipt, "rolled_back")
        return dict(receipt)

    if root_exists and new_identity is not None and _same_identity(root_identity, new_identity):
        if not previous_exists or not _same_identity(previous_identity, old_identity):
            raise UnknownTransactionError("new root is missing its known old backup")
        try:
            # validate_newの第一引数は必ず現在root。guard/faultとは異なり、
            # phase文字列を先頭に置かないため、単引数のvalidatorでも使える。
            valid = _invoke_hook(validate_new, root_path, workspace_path_value, receipt)
        except Exception:
            valid = False
        if valid is True:
            _set_receipt_state(workspace_path_value, receipt, "committed")
            return dict(receipt)
        _set_receipt_state(workspace_path_value, receipt, "manual_recovery_required")
        raise ManualRecoveryRequired("new root could not be verified")

    if root_exists:
        raise UnknownTransactionError("root has an unknown directory identity")

    if previous_exists and _same_identity(previous_identity, old_identity):
        _rename_known(previous, root_path, old_identity)
        _set_receipt_state(workspace_path_value, receipt, "rolled_back")
        return dict(receipt)
    if previous_exists:
        raise UnknownTransactionError("previous backup has an unknown directory identity")
    raise ManualRecoveryRequired("root and known backup are both missing")


# ---------------------------------------------------------------------------
# Path, identity, receipt, and hook helpers


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, (str, bytes)):
        raise TypeError("path must be path-like")
    return Path(os.path.abspath(raw))


def _path_digest(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
    if isinstance(normalized, bytes):
        normalized = normalized.decode(errors="surrogateescape")
    return hashlib.sha256(normalized.encode("utf-8", errors="surrogateescape")).hexdigest()


def _is_reparse_or_symlink(path: Path, st: os.stat_result | None = None) -> bool:
    if path.is_symlink():
        return True
    info = st if st is not None else os.lstat(path)
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _assert_safe_ancestors(path: Path) -> None:
    """存在する全ancestorが通常directoryで、symlink/reparseでないことを確認。"""

    absolute = _absolute_path(path)
    chain = list(absolute.parents)[::-1] + [absolute]
    for candidate in chain:
        if not os.path.lexists(candidate):
            continue
        try:
            info = os.lstat(candidate)
        except OSError as exc:
            raise UnsafePathError(f"cannot inspect path ancestor: {candidate}") from exc
        if _is_reparse_or_symlink(candidate, info):
            raise UnsafePathError(f"symlink/reparse path is not allowed: {candidate}")
        if candidate != Path(candidate.anchor) and not stat.S_ISDIR(info.st_mode):
            raise UnsafePathError(f"path ancestor is not a directory: {candidate}")


def _assert_safe_tree(path: Path) -> None:
    if not os.path.lexists(path):
        raise TransactionError(f"required directory is missing: {path.name}")
    _assert_safe_ancestors(path)
    physical = _runtime_io_path(path)
    info = os.lstat(physical)
    if _is_reparse_or_symlink(path, info) or not stat.S_ISDIR(info.st_mode):
        raise UnsafePathError(f"directory is not a normal directory: {path.name}")
    def _walk_error(error: OSError) -> None:
        raise UnsafePathError(f"cannot inspect tree entry: {error}") from error

    # Deep wheel files can exceed MAX_PATH even when the installation root
    # itself is short. Keep receipt identities logical; extend only this I/O.
    for current, directories, files in os.walk(physical, topdown=True, followlinks=False, onerror=_walk_error):
        current_path = Path(current)
        for name in [*directories, *files]:
            child = current_path / name
            child_info = os.lstat(child)
            if _is_reparse_or_symlink(child, child_info):
                raise UnsafePathError(f"symlink/reparse file is not allowed: {child.name}")
            if name in directories:
                if not stat.S_ISDIR(child_info.st_mode):
                    raise UnsafePathError(f"tree entry is not a directory: {child.name}")
            elif not stat.S_ISREG(child_info.st_mode):
                raise UnsafePathError(f"tree entry is not a regular file: {child.name}")


def _assert_optional_safe_tree(path: Path) -> None:
    if os.path.lexists(path):
        _assert_safe_tree(path)


def _validate_root(root: str | os.PathLike[str]) -> Path:
    path = _absolute_path(root)
    _assert_safe_tree(path)
    return path


def _device(path: Path) -> int:
    try:
        return int(os.stat(path).st_dev)
    except OSError as exc:
        raise UnsafePathError(f"cannot determine filesystem device: {path}") from exc


def _identity(path: Path) -> dict[str, int]:
    info = os.stat(path)
    if int(info.st_dev) < 0 or int(info.st_ino) <= 0:
        raise UnsafePathError("filesystem does not provide a usable directory identity")
    return {"st_dev": int(info.st_dev), "st_ino": int(info.st_ino)}


def _same_identity(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> bool:
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    try:
        return int(left["st_dev"]) == int(right["st_dev"]) and int(left["st_ino"]) == int(right["st_ino"])
    except (KeyError, TypeError, ValueError):
        return False


def _normalize_identity(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"st_dev", "st_ino"}:
        raise UnknownTransactionError(f"invalid {label} identity")
    try:
        device = int(value["st_dev"])
        inode = int(value["st_ino"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UnknownTransactionError(f"invalid {label} identity") from exc
    if device < 0 or inode <= 0:
        raise UnknownTransactionError(f"invalid {label} identity")
    return {"st_dev": device, "st_ino": inode}


def _require_identity(path: Path, expected: Mapping[str, Any] | None, label: str) -> None:
    if not os.path.lexists(path) or not _same_identity(_identity(path), expected):
        raise UnknownTransactionError(f"{label} identity mismatch")


def _validate_digest(value: str, label: str) -> str:
    normalized = str(value).strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a sha256 hex digest")
    return normalized


def _validate_workspace_argument(root: Path, workspace: Path) -> None:
    expected = workspace_path(root)
    if os.path.normcase(os.path.abspath(os.fspath(expected))) != os.path.normcase(os.path.abspath(os.fspath(workspace))):
        raise UnknownTransactionError("workspace does not match the root's deterministic sibling")
    _assert_safe_ancestors(workspace)
    if not os.path.lexists(workspace):
        raise UnknownTransactionError("workspace does not exist")
    if _device(root.parent) != _device(workspace.parent):
        raise UnsafePathError("workspace is on a different volume")


def _assert_workspace_shape(workspace: Path) -> None:
    _assert_safe_tree(workspace)
    if not os.path.lexists(workspace / RECEIPT_NAME):
        if any(_RECEIPT_TEMP_RE.fullmatch(child.name) for child in workspace.iterdir()):
            raise ManualRecoveryRequired("receipt temporary file remains after interruption")
        raise UnknownTransactionError("workspace has no receipt")


def _assert_known_workspace_entries(workspace: Path) -> None:
    """親installerが置く取得成果物だけを許可し、その他はfail-closedにする。"""
    for child in workspace.iterdir():
        if _RECEIPT_TEMP_RE.fullmatch(child.name):
            # atomic writeの途中に残ったtempは、uuid形式でも勝手に削除しない。
            raise ManualRecoveryRequired("receipt temporary file remains after interruption")
        if child.name not in _WORKSPACE_ENTRIES:
            raise UnknownTransactionError(f"unknown workspace entry: {child.name}")
        if _is_reparse_or_symlink(child):
            raise UnsafePathError(f"workspace entry is symlink/reparse: {child.name}")
        if child.name == "download":
            _assert_safe_tree(child)
        elif child.name == "acquisition.json":
            _validate_acquisition_file(child)


def _validate_acquisition_file(path: Path) -> None:
    """installerの取得証明JSONを、通常fileかつサイズ上限付きで確認する。"""

    info = os.lstat(path)
    if _is_reparse_or_symlink(path, info) or not stat.S_ISREG(info.st_mode):
        raise UnsafePathError("acquisition.json must be a regular file")
    if int(info.st_size) > MAX_ACQUISITION_BYTES:
        raise UnsafePathError("acquisition.json exceeds the bounded size")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UnknownTransactionError("acquisition.json is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise UnknownTransactionError("acquisition.json must contain an object")


def _read_known_receipt(root: Path, workspace: Path) -> dict[str, Any]:
    _assert_workspace_shape(workspace)
    receipt_path = workspace / RECEIPT_NAME
    if _is_reparse_or_symlink(receipt_path):
        raise UnsafePathError("receipt is a symlink/reparse point")
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UnknownTransactionError("receipt cannot be read") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise UnknownTransactionError("unknown receipt schema")
    allowed_fields = {
        "schema_version",
        "operation_id",
        "root_path_digest",
        "workspace_identity",
        "old_identity",
        "new_identity",
        "ready_manifest_digest",
        "state",
    }
    if set(data) != allowed_fields:
        raise UnknownTransactionError("receipt contains unknown fields")
    operation_id = data.get("operation_id")
    if not isinstance(operation_id, str) or not _GUID_RE.fullmatch(operation_id):
        raise UnknownTransactionError("unknown receipt operation")
    if data.get("root_path_digest") != _path_digest(root):
        raise UnknownTransactionError("receipt belongs to another root")
    state = data.get("state")
    if state not in {
        "preparing",
        "prepared",
        "swap_intent",
        "old_parked",
        "new_promoted",
        "committed",
        "rolled_back",
        "manual_recovery_required",
    }:
        raise UnknownTransactionError("unknown receipt state")
    if state in {"preparing", "prepared"} and not os.path.lexists(workspace / NEXT_ROOT_NAME):
        raise UnknownTransactionError("preparing workspace has no next-root")
    workspace_identity = _normalize_identity(data.get("workspace_identity"), "workspace")
    if not _same_identity(_identity(workspace), workspace_identity):
        raise UnknownTransactionError("workspace identity mismatch")
    old_identity = _normalize_identity(data.get("old_identity"), "old root")
    new_identity = (
        _normalize_identity(data.get("new_identity"), "new root")
        if data.get("new_identity") is not None
        else None
    )
    manifest = data.get("ready_manifest_digest")
    if manifest is not None:
        _validate_digest(manifest, "ready_manifest_digest")
    # Ignore unknown fields only if they cannot carry path/secret data; callers never
    # receive them as trusted metadata. The known fields are copied explicitly.
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": operation_id,
        "root_path_digest": data["root_path_digest"],
        "workspace_identity": {
            "st_dev": int(workspace_identity["st_dev"]),
            "st_ino": int(workspace_identity["st_ino"]),
        },
        "old_identity": {"st_dev": int(old_identity["st_dev"]), "st_ino": int(old_identity["st_ino"])},
        "new_identity": (
            {"st_dev": int(new_identity["st_dev"]), "st_ino": int(new_identity["st_ino"])}
            if new_identity is not None
            else None
        ),
        "ready_manifest_digest": manifest,
        "state": state,
    }


def _write_receipt(workspace: Path, receipt: Mapping[str, Any]) -> None:
    receipt_path = workspace / RECEIPT_NAME
    temp = workspace / f".{RECEIPT_NAME}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, receipt_path)
        try:
            directory_fd = os.open(workspace, getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY)
        except (AttributeError, OSError):
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            if os.path.lexists(temp):
                temp.unlink()
        except OSError:
            pass


def _persist_with_fault(
    workspace: Path,
    receipt: dict[str, Any],
    *,
    state: str,
    fault: Callable[..., Any] | None,
) -> None:
    receipt["state"] = state
    _invoke_fault(fault, "before_receipt_write")
    _write_receipt(workspace, receipt)
    _invoke_fault(fault, "after_receipt_write")


def _set_receipt_state(workspace: Path, receipt: dict[str, Any], state: str) -> None:
    receipt["state"] = state
    _write_receipt(workspace, receipt)


def _invoke_hook(callback: Callable[..., Any], *args: Any) -> Any:
    """引数なし／phaseのみ／full signatureをテスト・呼び出し側に許容する。"""

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(*args)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    if any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values()):
        return callback(*args)
    return callback(*args[: len(positional)])


def _invoke_fault(fault: Callable[..., Any] | None, phase: str) -> None:
    if fault is not None:
        _invoke_hook(fault, phase)


def _rename_known(source: Path, destination: Path, expected: Mapping[str, Any]) -> None:
    if not os.path.lexists(source):
        raise TransactionError(f"rename source is missing: {source.name}")
    if not _same_identity(_identity(source), expected):
        raise UnknownTransactionError(f"rename source identity changed: {source.name}")
    _assert_safe_ancestors(destination)
    if os.path.lexists(destination):
        raise UnknownTransactionError(f"rename destination already exists: {destination.name}")
    os.rename(source, destination)


def _recover_after_commit_failure(root: Path, workspace: Path, receipt: dict[str, Any]) -> None:
    """commit_swap内の同期的エラー後、旧rootが確定的に戻せる時だけ戻す。"""

    try:
        root_exists = os.path.lexists(root)
        if root_exists:
            current = _identity(root)
            if _same_identity(current, receipt.get("old_identity")):
                previous = workspace / PREVIOUS_NAME
                if os.path.lexists(previous):
                    # 旧rootが元の場所に残ったままbackupがある状態は、renameの
                    # 外部干渉か不明な残骸。片方を選んで消さず、手動復旧にする。
                    _set_receipt_state(workspace, receipt, "manual_recovery_required")
                    return
                _set_receipt_state(workspace, receipt, "rolled_back")
                return
            # 新rootまで昇格した後のエラーは、検証なしで旧版へ戻さない。
            if _same_identity(current, receipt.get("new_identity")):
                _set_receipt_state(workspace, receipt, "manual_recovery_required")
                return
            _set_receipt_state(workspace, receipt, "manual_recovery_required")
            return
        previous = workspace / PREVIOUS_NAME
        if os.path.lexists(previous) and _same_identity(_identity(previous), receipt.get("old_identity")):
            _rename_known(previous, root, receipt["old_identity"])
            _set_receipt_state(workspace, receipt, "rolled_back")
            return
        _set_receipt_state(workspace, receipt, "manual_recovery_required")
    except Exception:
        try:
            _set_receipt_state(workspace, receipt, "manual_recovery_required")
        except Exception:
            pass


def _recoverable_state(state: str) -> bool:
    return state in {"preparing", "prepared", "rolled_back", "committed"}


def _archive_known_workspace(root: Path, workspace: Path, receipt: Mapping[str, Any]) -> Path:
    operation_id = str(receipt["operation_id"])
    candidate = workspace.parent / f"{workspace.name}.failed-{operation_id}"
    if os.path.lexists(candidate):
        raise UnknownTransactionError("failed-workspace destination already exists")
    if not _same_identity(_identity(workspace), receipt["workspace_identity"]):
        raise UnknownTransactionError("workspace changed before archive")
    os.rename(workspace, candidate)
    return candidate


def _is_empty_directory(path: Path, *, excluding: set[str] | None = None) -> bool:
    excluded = excluding or set()
    return path.is_dir() and all(child.name in excluded for child in path.iterdir())
