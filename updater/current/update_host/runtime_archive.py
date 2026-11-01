"""TUFで取得済みLite runtime archiveの安全な展開境界。

このモジュールはTUF clientそのものを実装しない。呼び出し側で署名とtargetの
hashを検証済みの ``.tar.gz`` を受け取り、展開前にarchiveの全memberを走査して
runtime manifestのinventoryと照合する。その後だけ同一親ディレクトリの一時
directoryへ書き出し、展開済みtreeとrelease manifestのbindingを再検証して
``os.replace`` でstagingへ昇格する。

archiveの内容、秘密、環境依存の絶対pathは例外メッセージへ出さない。リンクや
Windowsで解釈が揺れるpathは、tarfileが展開する前にfail-closedで拒否する。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .contracts import _windows_path_key
from .runtime import (
    RUNTIME_MANIFEST_FILENAME,
    _extended_windows_path as _runtime_extended_windows_path,
    _runtime_io_path,
    RUNTIME_MAX_BYTES,
    RUNTIME_MAX_FILES,
    _assert_runtime_file_policy,
    _ensure_relative,
    validate_bound_runtime,
    validate_runtime_inventory,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# The runtime manifest is small compared with the runtime budget.  Bounding it
# before json.loads avoids allowing a malformed archive to consume the whole
# runtime budget just for metadata.
_MAX_MANIFEST_BYTES = min(RUNTIME_MAX_BYTES, 8 * 1024 * 1024)
_CHUNK_BYTES = 1024 * 1024
_MAX_ARCHIVE_MEMBERS = RUNTIME_MAX_FILES * 2
_REGULAR_TAR_TYPES = {tarfile.REGTYPE, tarfile.AREGTYPE}


class RuntimeArchiveError(ValueError):
    """runtime archiveを安全にstagingできない場合の例外。"""


def _error(message: str) -> RuntimeArchiveError:
    """例外文を固定し、入力のpath・内容・秘密を含めない。"""

    return RuntimeArchiveError(message)


def _is_lexists(path: Path) -> bool:
    """``Path.exists`` が見落とすdangling symlinkも含めて確認する。"""

    return os.path.lexists(os.fspath(path))


def _extended_windows_path(value: str) -> str:
    try:
        return _runtime_extended_windows_path(value)
    except ValueError:
        raise _error("runtime filesystem path is unsafe") from None


def _io_path(value: str | os.PathLike[str]) -> Path:
    try:
        return _runtime_io_path(value)
    except ValueError:
        raise _error("runtime filesystem path is unsafe") from None


def _is_reparse_point(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _assert_no_reparse_ancestors(path: Path, *, stop: Path | None = None) -> None:
    for current in (path, *path.parents):
        if _is_lexists(current) and _is_reparse_point(current):
            raise _error("runtime staging destination path is invalid")
        if current == stop:
            break


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_manifest_payload(payload: bytes) -> Mapping[str, Any]:
    if not payload or len(payload) > _MAX_MANIFEST_BYTES:
        raise _error("runtime archive manifest is oversized")
    try:
        text = payload.decode("utf-8")
        manifest = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise _error("runtime archive manifest cannot be read") from None
    if not isinstance(manifest, Mapping):
        raise _error("runtime archive manifest is invalid")
    return manifest


def _canonical_member_name(raw_name: Any, *, is_directory: bool) -> str:
    """tar member名をWindowsで一意な相対POSIX pathへ限定する。"""

    if not isinstance(raw_name, str):
        raise _error("runtime archive member path is unsafe")
    # A backslash is not accepted even when it happens to look like a benign
    # separator.  Windows APIs could interpret it differently from tarfile.
    if "\\" in raw_name or "\x00" in raw_name:
        raise _error("runtime archive member path is unsafe")
    if any(ord(character) < 32 for character in raw_name):
        raise _error("runtime archive member path is unsafe")
    if raw_name.startswith("/") or re.match(r"^[A-Za-z]:", raw_name):
        raise _error("runtime archive member path is unsafe")

    name = raw_name
    if is_directory:
        # tar conventionally stores directory names with a trailing slash.
        # Remove only that marker; all other empty/dot components remain
        # ambiguous and are rejected below.
        name = name.rstrip("/")
    if not name:
        raise _error("runtime archive member path is unsafe")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _error("runtime archive member path is unsafe")

    try:
        normalized = _ensure_relative(name, label="archive member")
        # ``_windows_path_key`` checks reserved names, trailing dots/spaces,
        # invalid Windows characters, and NFC/case-fold collisions.
        _windows_path_key(normalized)
        _assert_runtime_file_policy(normalized)
    except (TypeError, ValueError):
        raise _error("runtime archive member path is unsafe") from None
    return normalized


def _stream_digest(
    stream: BinaryIO,
    *,
    declared_size: int,
    capture_limit: int | None = None,
) -> tuple[int, str, bytes | None]:
    """streamの実byte数／hashを計算し、必要ならbounded payloadを保持する。"""

    if isinstance(declared_size, bool) or not isinstance(declared_size, int) or declared_size < 0:
        raise _error("runtime archive member size is invalid")
    if declared_size > RUNTIME_MAX_BYTES:
        raise _error("runtime archive exceeds its byte budget")
    digest = hashlib.sha256()
    captured = bytearray() if capture_limit is not None else None
    total = 0
    while True:
        try:
            chunk = stream.read(_CHUNK_BYTES)
        except (OSError, EOFError, tarfile.TarError):
            raise _error("runtime archive member stream cannot be read") from None
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise _error("runtime archive member stream is invalid")
        chunk_bytes = bytes(chunk)
        total += len(chunk_bytes)
        if total > RUNTIME_MAX_BYTES:
            raise _error("runtime archive exceeds its byte budget")
        digest.update(chunk_bytes)
        if captured is not None:
            if len(captured) + len(chunk_bytes) > capture_limit:
                # Keep reading only long enough to establish that the stream
                # itself is oversized; the payload is intentionally discarded.
                captured = None
            else:
                captured.extend(chunk_bytes)
    if total != declared_size:
        raise _error("runtime archive member stream size mismatch")
    return total, digest.hexdigest(), bytes(captured) if captured is not None else None


def _member_stream(archive: tarfile.TarFile, info: tarfile.TarInfo) -> BinaryIO:
    try:
        stream = archive.extractfile(info)
    except (OSError, EOFError, tarfile.TarError):
        raise _error("runtime archive member stream cannot be opened") from None
    if stream is None:
        raise _error("runtime archive member stream cannot be opened")
    return stream


def _validate_member_type(info: tarfile.TarInfo) -> bool:
    """member typeを検証し、regular fileかどうかを返す。"""

    try:
        if info.issym() or info.islnk() or info.isdev() or info.isfifo():
            raise _error("runtime archive contains links or special files")
        # GNU sparse files are regular-looking entries whose logical content
        # is reconstructed by tarfile.  They are unnecessary for this runtime
        # and are rejected to keep the stream contract unambiguous.
        if getattr(info, "sparse", None):
            raise _error("runtime archive contains links or special files")
    except (AttributeError, TypeError):
        raise _error("runtime archive member type is invalid") from None
    if info.type in _REGULAR_TAR_TYPES:
        return True
    if info.isdir() or info.type == tarfile.DIRTYPE:
        return False
    # Unknown GNU/PAX/sparse types are not needed by the runtime and are safer
    # to reject than to let tarfile assign an implicit filesystem meaning.
    raise _error("runtime archive contains links or special files")


def _check_ancestor_collisions(member_kinds: Mapping[str, str]) -> None:
    for relative, kind in member_kinds.items():
        if kind != "file":
            continue
        parts = relative.split("/")
        for index in range(1, len(parts)):
            if member_kinds.get("/".join(parts[:index])) == "file":
                raise _error("runtime archive contains duplicate or colliding members")


def _scan_archive(archive_path: Path) -> tuple[Mapping[str, Any], dict[str, dict[str, Any]]]:
    """archiveを展開せずに全memberを走査し、manifestと実stream inventoryを返す。"""

    manifest_payload: bytes | None = None
    manifest_seen = False
    inventory: dict[str, dict[str, Any]] = {}
    member_kinds: dict[str, str] = {}
    windows_keys: dict[str, str] = {}
    declared_bytes = 0
    runtime_file_count = 0
    member_count = 0

    try:
        archive = tarfile.open(archive_path, mode="r:gz")
    except (OSError, EOFError, tarfile.TarError):
        raise _error("runtime archive cannot be opened") from None
    try:
        try:
            for info in archive:
                member_count += 1
                if member_count > _MAX_ARCHIVE_MEMBERS:
                    raise _error("runtime archive exceeds its member budget")
                is_file = _validate_member_type(info)
                relative = _canonical_member_name(info.name, is_directory=not is_file)
                kind = "file" if is_file else "directory"
                if relative in member_kinds:
                    raise _error("runtime archive contains duplicate or colliding members")
                try:
                    windows_key = _windows_path_key(relative)
                except (TypeError, ValueError):
                    raise _error("runtime archive member path is unsafe") from None
                if windows_key in windows_keys:
                    raise _error("runtime archive contains duplicate or colliding members")
                windows_keys[windows_key] = relative
                member_kinds[relative] = kind

                declared_size = info.size
                if (
                    isinstance(declared_size, bool)
                    or not isinstance(declared_size, int)
                    or declared_size < 0
                ):
                    raise _error("runtime archive member size is invalid")
                if declared_size > RUNTIME_MAX_BYTES:
                    raise _error("runtime archive exceeds its byte budget")
                if not is_file and declared_size != 0:
                    raise _error("runtime archive directory size is invalid")

                if is_file:
                    if relative != RUNTIME_MANIFEST_FILENAME:
                        runtime_file_count += 1
                        if runtime_file_count > RUNTIME_MAX_FILES:
                            raise _error("runtime archive exceeds its file budget")
                        declared_bytes += declared_size
                        if declared_bytes > RUNTIME_MAX_BYTES:
                            raise _error("runtime archive exceeds its byte budget")

                    stream = _member_stream(archive, info)
                    try:
                        _, digest, captured = _stream_digest(
                            stream,
                            declared_size=declared_size,
                            capture_limit=(
                                _MAX_MANIFEST_BYTES
                                if relative == RUNTIME_MANIFEST_FILENAME
                                else None
                            ),
                        )
                    finally:
                        stream.close()
                    if relative == RUNTIME_MANIFEST_FILENAME:
                        if manifest_seen:
                            raise _error("runtime archive contains duplicate manifest")
                        manifest_seen = True
                        if captured is None:
                            raise _error("runtime archive manifest is oversized")
                        manifest_payload = captured
                    else:
                        inventory[relative] = {
                            "size": declared_size,
                            "sha256": digest,
                        }
            _check_ancestor_collisions(member_kinds)
        except RuntimeArchiveError:
            raise
        except (OSError, EOFError, tarfile.TarError, ValueError, TypeError):
            raise _error("runtime archive cannot be scanned") from None
    finally:
        archive.close()

    if not manifest_seen or manifest_payload is None:
        raise _error("runtime archive manifest is missing")
    return _read_manifest_payload(manifest_payload), inventory


def _safe_destination(destination: str | os.PathLike[str]) -> tuple[Path, Path]:
    try:
        target = _io_path(destination)
    except (TypeError, ValueError):
        raise _error("runtime staging destination is invalid") from None
    if not str(target) or target.name in {"", ".", ".."}:
        raise _error("runtime staging destination is invalid")
    if _is_lexists(target):
        raise _error("runtime staging destination already exists")
    parent = target.parent
    try:
        _assert_no_reparse_ancestors(parent)
        # The staging parent is created by the app updater on first use.  Do not
        # replace a parent symlink with a directory outside the intended area.
        if parent.exists() and parent.is_symlink():
            raise _error("runtime staging parent is invalid")
        parent.mkdir(parents=True, exist_ok=True)
        _assert_no_reparse_ancestors(parent)
    except RuntimeArchiveError:
        raise
    except (OSError, ValueError):
        raise _error("runtime staging parent is invalid") from None
    if not parent.is_dir() or parent.is_symlink():
        raise _error("runtime staging parent is invalid")
    return target, parent


def _validate_archive_file(archive: str | os.PathLike[str]) -> Path:
    try:
        path = _io_path(archive)
    except (TypeError, ValueError):
        raise _error("runtime archive is invalid") from None
    try:
        _assert_no_reparse_ancestors(path)
        if path.is_symlink() or not path.is_file():
            raise _error("runtime archive is invalid")
        # A regular file is required; stat follows no symlink because of the
        # check above.  A zero-byte archive cannot contain the root manifest.
        size = path.stat().st_size
    except RuntimeArchiveError:
        raise
    except OSError:
        raise _error("runtime archive is invalid") from None
    if size <= 0:
        raise _error("runtime archive is invalid")
    return path


def _verify_archive_digest(path: Path, expected: str | None) -> None:
    if expected is None:
        return
    digest = str(expected).strip().lower()
    if not _SHA256.fullmatch(digest):
        raise _error("runtime archive hash is invalid")
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                hasher.update(chunk)
    except OSError:
        raise _error("runtime archive hash cannot be read") from None
    if hasher.hexdigest() != digest:
        raise _error("runtime archive hash mismatch")


def _write_archive(
    archive_path: Path,
    temporary_root: Path,
    inventory: Mapping[str, Mapping[str, Any]],
) -> None:
    """2回目のstreamをtemporary_rootへ書き込み、hash／sizeを再確認する。"""

    written: set[str] = set()
    try:
        archive = tarfile.open(archive_path, mode="r:gz")
    except (OSError, EOFError, tarfile.TarError):
        raise _error("runtime archive cannot be opened") from None
    try:
        try:
            for info in archive:
                is_file = _validate_member_type(info)
                relative = _canonical_member_name(info.name, is_directory=not is_file)
                target = temporary_root.joinpath(*relative.split("/"))
                if is_file:
                    # Manifest is copied as well; its content was validated in
                    # the preflight pass and validate_runtime_tree will parse it
                    # again after writing.
                    if relative != RUNTIME_MANIFEST_FILENAME and relative not in inventory:
                        raise _error("runtime archive inventory changed")
                    if relative in written:
                        raise _error("runtime archive contains duplicate members")
                    _assert_no_reparse_ancestors(target.parent, stop=temporary_root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if _is_reparse_point(target.parent) or target.exists() and not target.is_file():
                        raise _error("runtime archive destination path is invalid")
                    stream = _member_stream(archive, info)
                    try:
                        digest = hashlib.sha256()
                        total = 0
                        with target.open("xb") as handle:
                            while True:
                                try:
                                    chunk = stream.read(_CHUNK_BYTES)
                                except (OSError, EOFError, tarfile.TarError):
                                    raise _error("runtime archive member stream cannot be read") from None
                                if not chunk:
                                    break
                                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                                    raise _error("runtime archive member stream is invalid")
                                chunk_bytes = bytes(chunk)
                                total += len(chunk_bytes)
                                if total > RUNTIME_MAX_BYTES:
                                    raise _error("runtime archive exceeds its byte budget")
                                digest.update(chunk_bytes)
                                handle.write(chunk_bytes)
                            handle.flush()
                            os.fsync(handle.fileno())
                    except FileExistsError:
                        raise _error("runtime archive contains duplicate members") from None
                    finally:
                        stream.close()
                    expected = (
                        inventory.get(relative)
                        if relative != RUNTIME_MANIFEST_FILENAME
                        else None
                    )
                    if total != info.size:
                        raise _error("runtime archive member stream size mismatch")
                    if expected is not None and (
                        total != expected.get("size")
                        or digest.hexdigest() != expected.get("sha256")
                    ):
                        raise _error("runtime archive member stream hash or size mismatch")
                    written.add(relative)
                else:
                    _assert_no_reparse_ancestors(target, stop=temporary_root)
                    if target.exists() and not target.is_dir():
                        raise _error("runtime archive destination path is invalid")
                    if target.is_symlink():
                        raise _error("runtime archive destination path is invalid")
                    target.mkdir(parents=True, exist_ok=True)
        except RuntimeArchiveError:
            raise
        except (OSError, EOFError, tarfile.TarError, ValueError, TypeError):
            raise _error("runtime archive cannot be extracted") from None
    finally:
        archive.close()


def _fsync_directory(path: Path) -> None:
    """directory durabilityを可能なOSだけで補助する。"""

    try:
        fd = os.open(os.fspath(path), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def extract_runtime_archive(
    archive: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    release_manifest: Mapping[str, Any],
    *,
    expected_archive_sha256: str | None = None,
    archive_sha256: str | None = None,
) -> Path:
    """TUF hash検証済みruntime archiveを検証・原子的にstagingへ展開する。

    ``archive_sha256`` は呼び出し側の短い別名で、両方を指定した場合は同値で
    なければ拒否する。通常はTUF clientがhashを検証済みであるため省略できる。
    戻り値は確定したdestinationである。
    """

    if expected_archive_sha256 is not None and archive_sha256 is not None:
        if str(expected_archive_sha256).strip().lower() != str(archive_sha256).strip().lower():
            raise _error("runtime archive hash arguments disagree")
    expected_digest = expected_archive_sha256 if expected_archive_sha256 is not None else archive_sha256

    target, parent = _safe_destination(destination)
    archive_path = _validate_archive_file(archive)
    _verify_archive_digest(archive_path, expected_digest)

    temporary_root: Path | None = None
    try:
        manifest, inventory = _scan_archive(archive_path)
        try:
            validate_runtime_inventory(manifest, inventory)
        except Exception:
            raise _error("runtime archive inventory hash or size mismatch") from None

        # Re-check destination immediately before creating the sibling temp
        # directory and again before commit.  os.replace itself is atomic, but
        # overwriting a destination that appeared concurrently is forbidden.
        if _is_lexists(target):
            raise _error("runtime staging destination already exists")
        try:
            temporary_root = Path(
                tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".tmp", dir=os.fspath(parent))
            )
        except OSError:
            raise _error("runtime staging temporary directory cannot be created") from None

        _write_archive(archive_path, temporary_root, inventory)
        try:
            validate_bound_runtime(release_manifest, temporary_root)
        except Exception:
            raise _error("runtime archive release/runtime binding mismatch") from None

        _fsync_directory(temporary_root)
        if _is_lexists(target):
            raise _error("runtime staging destination already exists")
        try:
            os.replace(os.fspath(temporary_root), os.fspath(target))
        except OSError:
            raise _error("runtime staging destination cannot be committed") from None
        temporary_root = None
        _fsync_directory(parent)
        return Path(destination)
    except RuntimeArchiveError:
        raise
    except Exception:
        raise _error("runtime archive cannot be prepared") from None
    finally:
        if temporary_root is not None:
            try:
                shutil.rmtree(temporary_root)
            except OSError:
                # Never remove a destination or a caller-owned directory while
                # handling a failed extraction.  The only cleanup target here
                # is the sibling directory created above.
                pass


# Names used by callers that describe the same staging boundary differently.
stage_runtime_archive = extract_runtime_archive
install_runtime_archive = extract_runtime_archive
extract_runtime_archive_to_staging = extract_runtime_archive


__all__ = [
    "RuntimeArchiveError",
    "extract_runtime_archive",
    "stage_runtime_archive",
    "install_runtime_archive",
    "extract_runtime_archive_to_staging",
]
