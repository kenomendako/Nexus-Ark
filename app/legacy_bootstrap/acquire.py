"""旧Windowsルートを起動せずに署名済み更新を準備する境界。

このmoduleは旧app、旧venv、旧metadataを読み込まない。呼び出し側が用意した
兄弟workspaceだけへTUF検証済みの新app/runtimeを準備し、最終的なrootの切替は
親controllerへ委ねる。外部通信は``SecureTufClient``だけが行い、URLとCAの
境界はこの入口で先に検証する。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import tarfile
import tempfile
import tomllib
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

import certifi
from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version

from runtime_tuf import RuntimeTargetClient
from secure_tuf_client import SecureTufClient
from update_host.contracts import _windows_path_key, validate_release_tree


APP_NAME = "Nexus-Ark"
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_FILES = 5_000
MAX_ARCHIVE_MEMBERS = MAX_FILES * 2 + 64
MAX_JSON_BYTES = 8 * 1024 * 1024
MINIMUM_CERTIFI = "2026.10.23"
POLICY_PATH = "release/legacy_bootstrap_policy.json"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TARGET_RE = re.compile(r"^Nexus-Ark-(?P<version>[^/\\]+)\.tar\.gz$")
_ROOT_REQUIRED_TYPE = "root"
_REGULAR_TAR_TYPES = {tarfile.REGTYPE, tarfile.AREGTYPE}
_POLICY = {
    "schema_version": 1,
    "tls_policy": "certifi-pinned-v1",
    "bootstrap_version": 1,
    "minimum_certifi": MINIMUM_CERTIFI,
}


class LegacyBootstrapError(ValueError):
    """bootstrapが安全に更新を準備できない場合の例外。"""


Progress = Callable[[str], Any]


def _fail(message: str, exc: BaseException | None = None) -> LegacyBootstrapError:
    error = LegacyBootstrapError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _notify(progress: Progress | None, event: str) -> None:
    if progress is None:
        return
    try:
        progress(event)
    except Exception as exc:
        # Progress UI is advisory.  A broken callback must never turn a
        # verified update into a different filesystem operation.
        raise _fail("progress callback failed", exc) from None


def _path_exists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _is_reparse_point(path: Path) -> bool:
    """Detect symlinks and Windows junction/reparse attributes without following them."""

    if path.is_symlink():
        return True
    try:
        stat_result = path.lstat()
    except OSError:
        return False
    return bool(getattr(stat_result, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_no_reparse_ancestors(path: Path, label: str) -> None:
    """Reject every existing component, including Windows junctions."""

    cursor = Path(path).absolute()
    ancestors: list[Path] = []
    while True:
        ancestors.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    for ancestor in reversed(ancestors):
        if _path_exists(ancestor) and _is_reparse_point(ancestor):
            raise _fail(f"{label} contains a link or reparse point")


def _assert_regular_file(path: Path, label: str) -> None:
    _assert_no_reparse_ancestors(path, label)
    if _is_reparse_point(path) or not path.is_file():
        raise _fail(f"{label} is not a regular file")
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise _fail(f"{label} cannot be inspected", exc) from None
    if getattr(stat_result, "st_nlink", 1) != 1:
        raise _fail(f"{label} has an unsafe link count")


def _assert_directory(path: Path, label: str, *, create: bool = False) -> None:
    _assert_no_reparse_ancestors(path, label)
    if _path_exists(path):
        if _is_reparse_point(path) or not path.is_dir():
            raise _fail(f"{label} is not a regular directory")
        return
    if not create:
        raise _fail(f"{label} is missing")
    parent = path.parent
    if _path_exists(parent) and (_is_reparse_point(parent) or not parent.is_dir()):
        raise _fail(f"{label} parent is unsafe")
    try:
        path.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise _fail(f"{label} cannot be created", exc) from None


def _read_json_object(path: Path, label: str, *, max_bytes: int = MAX_JSON_BYTES) -> Mapping[str, Any]:
    _assert_regular_file(path, label)
    try:
        if path.stat().st_size > max_bytes:
            raise _fail(f"{label} is oversized")
        payload = path.read_bytes()
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except LegacyBootstrapError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise _fail(f"{label} cannot be read", exc) from None
    if not isinstance(value, Mapping):
        raise _fail(f"{label} must be an object")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _copy_trusted_root(trusted_root: Path, work_dir: Path) -> Path:
    """新ZIP側のrootだけをmetadataへcopyし、既存metadataを使わない。"""

    _assert_regular_file(trusted_root, "trusted root")
    source = _read_json_object(trusted_root, "trusted root", max_bytes=MAX_JSON_BYTES)
    signed = source.get("signed")
    if not isinstance(signed, Mapping) or signed.get("_type") != _ROOT_REQUIRED_TYPE:
        raise _fail("trusted root is not TUF root metadata")

    metadata_dir = work_dir / "metadata"
    if _path_exists(metadata_dir):
        raise _fail("bootstrap metadata directory already exists")
    _assert_directory(metadata_dir, "bootstrap metadata directory", create=True)
    destination = metadata_dir / "root.json"
    try:
        payload = trusted_root.read_bytes()
        temporary = metadata_dir / ("root.json.tmp-" + os.urandom(8).hex())
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            if "temporary" in locals() and _path_exists(temporary):
                temporary.unlink()
        except OSError:
            pass
        raise _fail("trusted root cannot be copied", exc) from None
    return destination


def _validate_update_url(update_url: str) -> str:
    raw = str(update_url or "")
    if not raw or any(ord(char) < 32 or char.isspace() for char in raw):
        raise _fail("update URL is unsafe")
    try:
        parsed = urllib.parse.urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise _fail("update URL is unsafe", exc) from None
    decoded_path = urllib.parse.unquote(parsed.path)
    local_http = parsed.scheme == "http" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    unsafe_path = (
        "\\" in decoded_path
        or any(ord(char) < 32 for char in decoded_path)
        or any(part in {".", ".."} for part in decoded_path.split("/"))
    )
    if (
        (parsed.scheme != "https" and not local_http)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or unsafe_path
    ):
        raise _fail("update URL is unsafe")
    path = parsed.path.rstrip("/") + "/"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _runtime_repository_url(update_url: str) -> str:
    parsed = urllib.parse.urlsplit(update_url)
    path = parsed.path.rstrip("/") + "/lite-runtime/"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _bootstrap_certifi_version() -> Version:
    """bootstrap自身のCA bundle versionを検証する。

    独立テストではこの関数を差し替えられる。製品実行時はbootstrapの
    import済みcertifiとmetadataのversionを一致させ、古いCAでTUF通信を
    始めない。
    """

    raw = getattr(certifi, "__version__", None)
    if not raw:
        try:
            raw = importlib.metadata.version("certifi")
        except importlib.metadata.PackageNotFoundError as exc:
            raise _fail("bootstrap certifi is unavailable", exc) from None
    try:
        return Version(str(raw))
    except InvalidVersion as exc:
        raise _fail("bootstrap certifi version is invalid", exc) from None


def _validate_certifi_requirement(app_path: Path, minimum: Version) -> None:
    pyproject = app_path / "pyproject.toml"
    _assert_regular_file(pyproject, "release pyproject")
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise _fail("release pyproject cannot be read", exc) from None
    project = data.get("project")
    dependencies = project.get("dependencies") if isinstance(project, Mapping) else None
    if not isinstance(dependencies, list):
        raise _fail("release certifi dependency is missing")
    found = False
    for raw_requirement in dependencies:
        if not isinstance(raw_requirement, str):
            continue
        try:
            requirement = Requirement(raw_requirement)
        except Exception:
            continue
        if requirement.name.casefold() != "certifi":
            continue
        found = True
        pins = [item.version for item in requirement.specifier if item.operator == "==" and "*" not in item.version]
        if len(pins) != 1 or Version(pins[0]) < minimum:
            raise _fail("release certifi must be pinned at or above the bootstrap minimum")
    if not found:
        raise _fail("release certifi dependency is missing")


def _validate_policy(app_path: Path) -> Mapping[str, Any]:
    policy_path = app_path / Path(POLICY_PATH)
    policy = _read_json_object(policy_path, "legacy bootstrap policy")
    if dict(policy) != _POLICY:
        raise _fail("legacy bootstrap policy is invalid")
    try:
        minimum = Version(str(policy["minimum_certifi"]))
    except InvalidVersion as exc:
        raise _fail("legacy bootstrap policy certifi version is invalid", exc) from None
    if _bootstrap_certifi_version() < minimum:
        raise _fail("bootstrap certifi is below the release minimum")
    _validate_certifi_requirement(app_path, minimum)
    return policy


def validate_bootstrap_policy(
    app_path: Path,
    exact: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate the policy against the already verified release manifest.

    Installed-tree validators can call this public wrapper with the result of
    ``validate_release_tree``.  Keeping the manifest ownership and certifi
    checks here prevents an installed old release from being accepted merely
    because its policy file happens to exist outside the signed inventory.
    """

    if not isinstance(exact, Mapping):
        raise _fail("release validation result is invalid")
    manifest = exact.get("manifest")
    if not isinstance(manifest, Mapping):
        raise _fail("release manifest is missing from validation result")
    entries = manifest.get("entries")
    policy_entries = [
        entry
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("path") == POLICY_PATH
    ] if isinstance(entries, list) else []
    if len(policy_entries) != 1 or policy_entries[0].get("kind") != "owned":
        raise _fail("legacy bootstrap policy is not an owned manifest entry")
    return _validate_policy(Path(app_path))


def _target_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _fail("release target cannot be read", exc) from None
    return digest.hexdigest()


def _target_descriptor(meta: Any) -> tuple[str, Version] | None:
    if isinstance(meta, (str, Path)):
        raw_path = meta
    else:
        raw_path = getattr(meta, "target_path_str", None)
        if raw_path is None:
            raw_path = getattr(meta, "path", None)
        if raw_path is None:
            raw_path = getattr(meta, "filename", None)
    raw_text = str(raw_path or "")
    raw_posix = PurePosixPath(raw_text)
    filename = raw_posix.name if raw_path is not None else ""
    if raw_posix.is_absolute() or len(raw_posix.parts) != 1 or not filename:
        return None
    match = _TARGET_RE.fullmatch(filename)
    if not match:
        return None
    try:
        version = Version(match.group("version"))
    except InvalidVersion:
        return None
    return filename, version


def _target_path_is_safe(target_path: Any, expected_name: str) -> None:
    raw = str(target_path or "")
    if not raw or "\\" in raw or "\x00" in raw:
        raise _fail("signed release target path is unsafe")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _fail("signed release target path is unsafe")
    if len(path.parts) != 1 or path.name != expected_name:
        raise _fail("signed release target path is not a full app archive")


def _target_metadata_length(target_info: Any) -> int:
    length = getattr(target_info, "length", None)
    if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
        raise _fail("signed release target length is invalid")
    if length > MAX_ARCHIVE_BYTES:
        raise _fail("signed release target exceeds archive limit")
    return length


def _verify_target_file(path: Path, target_info: Any) -> str:
    _assert_regular_file(path, "release target")
    length = _target_metadata_length(target_info)
    try:
        if path.stat().st_size != length:
            raise _fail("signed release target length mismatch")
        with path.open("rb") as handle:
            target_info.verify_length_and_hashes(handle)
    except LegacyBootstrapError:
        raise
    except Exception as exc:
        raise _fail("signed release target hash mismatch", exc) from None
    return _target_sha256(path)


def _client_target_info(client: Any, meta: Any, target_name: str) -> Any:
    getter = getattr(client, "get_targetinfo", None)
    if not callable(getter):
        raise _fail("TUF client cannot resolve the signed target")
    try:
        return getter(meta)
    except TypeError as first_error:
        try:
            return getter(target_name)
        except TypeError:
            raise _fail("TUF client target contract is invalid", first_error) from None
        except Exception as exc:
            raise _fail("signed release target lookup failed", exc) from None
    except Exception as exc:
        raise _fail("signed release target lookup failed", exc) from None


def _client_cached_target(client: Any, target_info: Any) -> str | None:
    finder = getattr(client, "find_cached_target", None)
    if not callable(finder):
        return None
    try:
        return finder(targetinfo=target_info)
    except TypeError:
        try:
            return finder(target_info)
        except TypeError as exc:
            raise _fail("TUF client cache contract is invalid", exc) from None
        except Exception as exc:
            raise _fail("signed release target cache lookup failed", exc) from None
    except Exception as exc:
        raise _fail("signed release target cache lookup failed", exc) from None


def _download_target(client: Any, target_info: Any, target_dir: Path, target_name: str) -> Path:
    _target_metadata_length(target_info)
    cached = _client_cached_target(client, target_info)
    destination = target_dir / target_name
    archive_path: Path
    if cached:
        archive_path = Path(cached)
    else:
        downloader = getattr(client, "download_target", None)
        if not callable(downloader):
            raise _fail("TUF client cannot download the signed target")
        try:
            result = downloader(targetinfo=target_info, filepath=str(destination))
        except TypeError:
            try:
                result = downloader(target_info, str(destination))
            except TypeError as exc:
                raise _fail("TUF client download contract is invalid", exc) from None
            except Exception as exc:
                raise _fail("signed release target download failed", exc) from None
        except Exception as exc:
            raise _fail("signed release target download failed", exc) from None
        archive_path = Path(result) if result is not None else destination
    try:
        archive_path = archive_path.absolute()
        target_root = target_dir.absolute()
        if not archive_path.is_relative_to(target_root):
            raise _fail("signed release target escaped its cache")
    except ValueError:
        raise _fail("signed release target escaped its cache") from None
    if archive_path.name != target_name:
        raise _fail("signed release target filename changed")
    return archive_path


def _normalize_member_name(raw_name: Any, *, directory: bool) -> str | None:
    if not isinstance(raw_name, str) or "\x00" in raw_name or "\\" in raw_name:
        raise _fail("release archive member path is unsafe")
    if any(ord(character) < 32 for character in raw_name):
        raise _fail("release archive member path is unsafe")
    if directory and raw_name in {".", "./", ""}:
        return None
    if not directory and raw_name.endswith("/"):
        raise _fail("release archive member path is unsafe")
    if raw_name.startswith("/") or re.match(r"^[A-Za-z]:", raw_name):
        raise _fail("release archive member path is unsafe")
    name = raw_name.rstrip("/") if directory else raw_name
    parts = name.split("/")
    while parts and parts[0] == ".":
        parts.pop(0)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _fail("release archive member path is unsafe")
    normalized = "/".join(parts)
    try:
        _windows_path_key(normalized)
    except (TypeError, ValueError) as exc:
        raise _fail("release archive member path is unsafe", exc) from None
    return normalized


def _member_kind(info: tarfile.TarInfo) -> str:
    try:
        if info.issym() or info.islnk() or info.isdev() or info.isfifo() or getattr(info, "sparse", None):
            raise _fail("release archive contains links or special files")
        if info.type in _REGULAR_TAR_TYPES:
            return "file"
        if info.isdir() or info.type == tarfile.DIRTYPE:
            return "directory"
    except AttributeError as exc:
        raise _fail("release archive member type is invalid", exc) from None
    raise _fail("release archive contains unsupported member types")


def _safe_extract_app_archive(archive: Path, destination: Path) -> Path:
    """tarを2回走査し、links/path collisionを拒否して原子的に展開する。"""

    if _path_exists(destination):
        raise _fail("release app destination already exists")
    parent = destination.parent
    _assert_directory(parent, "release app staging parent", create=True)
    member_records: list[tuple[str | None, str, int]] = []
    member_kinds: dict[str, str] = {}
    windows_paths: dict[str, str] = {}
    regular_files = 0
    total_bytes = 0
    member_count = 0
    try:
        handle = tarfile.open(archive, mode="r:gz")
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise _fail("release archive cannot be opened", exc) from None
    try:
        try:
            for info in handle:
                member_count += 1
                if member_count > MAX_ARCHIVE_MEMBERS:
                    raise _fail("release archive exceeds member limit")
                kind = _member_kind(info)
                relative = _normalize_member_name(info.name, directory=kind == "directory")
                if relative is None:
                    member_records.append((None, kind, 0))
                    continue
                if relative in member_kinds:
                    raise _fail("release archive contains duplicate members")
                key = _windows_path_key(relative)
                if key in windows_paths:
                    raise _fail("release archive contains Windows path collisions")
                windows_paths[key] = relative
                member_kinds[relative] = kind
                if kind == "file":
                    size = info.size
                    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                        raise _fail("release archive member size is invalid")
                    regular_files += 1
                    total_bytes += size
                    if regular_files > MAX_FILES or total_bytes > MAX_EXTRACTED_BYTES:
                        raise _fail("release archive exceeds extraction limit")
                else:
                    if info.size != 0:
                        raise _fail("release archive directory size is invalid")
                    size = 0
                member_records.append((relative, kind, size))
            for relative, kind in member_kinds.items():
                if kind != "file":
                    continue
                parts = relative.split("/")
                for index in range(1, len(parts)):
                    if member_kinds.get("/".join(parts[:index])) == "file":
                        raise _fail("release archive contains ancestor collisions")
        except LegacyBootstrapError:
            raise
        except (OSError, EOFError, tarfile.TarError, ValueError, TypeError) as exc:
            raise _fail("release archive cannot be scanned", exc) from None
    finally:
        handle.close()

    temporary: Path | None = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".app.", suffix=".tmp", dir=os.fspath(parent)))
        handle = tarfile.open(archive, mode="r:gz")
        try:
            index = 0
            for info in handle:
                relative, kind, expected_size = member_records[index]
                index += 1
                if relative is None:
                    continue
                target = temporary.joinpath(*relative.split("/"))
                if kind == "directory":
                    target.mkdir(parents=True, exist_ok=True)
                    if _is_reparse_point(target) or not target.is_dir():
                        raise _fail("release archive destination path is unsafe")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if _is_reparse_point(target.parent) or _path_exists(target):
                    raise _fail("release archive destination path is unsafe")
                stream = handle.extractfile(info)
                if stream is None:
                    raise _fail("release archive member cannot be read")
                try:
                    with target.open("xb") as output:
                        total = 0
                        while True:
                            chunk = stream.read(1024 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > MAX_EXTRACTED_BYTES:
                                raise _fail("release archive exceeds extraction limit")
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    stream.close()
                if total != expected_size or _is_reparse_point(target):
                    raise _fail("release archive member size mismatch")
        finally:
            handle.close()
        if _path_exists(destination):
            raise _fail("release app destination appeared during extraction")
        os.replace(temporary, destination)
        temporary = None
        return destination
    except LegacyBootstrapError:
        raise
    except (OSError, EOFError, tarfile.TarError, ValueError, TypeError) as exc:
        raise _fail("release archive cannot be extracted", exc) from None
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def _select_full_target(
    client: Any, current_version: Version, *, allow_current_version: bool = False
) -> tuple[Any, str, Version]:
    refresh = getattr(client, "refresh", None)
    if callable(refresh):
        try:
            refresh()
        except Exception as exc:
            raise _fail("TUF metadata refresh failed", exc) from None

    metas = getattr(client, "trusted_target_metas", None)
    if metas is None:
        checker = getattr(client, "check_for_updates", None)
        if not callable(checker):
            raise _fail("TUF client cannot enumerate signed targets")
        try:
            candidate = checker(patch=False)
        except TypeError:
            try:
                candidate = checker()
            except Exception as exc:
                raise _fail("TUF metadata target selection failed", exc) from None
        except Exception as exc:
            raise _fail("TUF metadata target selection failed", exc) from None
        metas = [candidate] if candidate is not None else []
    try:
        candidates = []
        for meta in metas:
            descriptor = _target_descriptor(meta)
            if descriptor is None:
                continue
            target_name, version = descriptor
            if (version > current_version or (allow_current_version and version == current_version)) and not version.is_prerelease and not version.is_devrelease:
                candidates.append((version, target_name, meta))
    except TypeError as exc:
        raise _fail("TUF target metadata contract is invalid", exc) from None
    if not candidates:
        raise _fail("no newer full release target is available")
    version, target_name, meta = max(candidates, key=lambda item: item[0])
    return meta, target_name, version


def _ensure_work_dir(work_dir: Path) -> Path:
    work_dir = Path(work_dir).absolute()
    _assert_no_reparse_ancestors(work_dir, "bootstrap work directory")
    if _path_exists(work_dir):
        if _is_reparse_point(work_dir) or not work_dir.is_dir():
            raise _fail("bootstrap work directory is unsafe")
    else:
        _assert_directory(work_dir, "bootstrap work directory", create=True)
    return work_dir


def acquire_release(
    *,
    work_dir: Path,
    trusted_root: Path,
    update_url: str,
    current_version: str,
    allow_current_version: bool = False,
    progress: Progress | None = None,
    client_factory: Callable[..., Any] = SecureTufClient,
    extractor: Callable[[Path, Path], Any] | None = None,
    runtime_client_factory: Callable[..., Any] = RuntimeTargetClient,
    runtime_tuf_client_factory: Callable[..., Any] | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """旧rootへ触れず、新しいapp/runtimeを兄弟workspaceへ準備する。

    ``allow_current_version`` は旧Startだけが残る混在環境の復旧に限り、
    同じ版の署名済みtargetを再取得する。targetの差し替えやdowngradeは許可しない。
    ``extractor`` はテスト用のapp archive展開差し替えで、
    ``extractor(archive_path, destination)`` を呼ぶ。デフォルトは本moduleの
    safe tar extractorである。runtimeはmanifestのbindingが存在する場合だけ、
    本体repositoryと同じoriginの``lite-runtime/``へ取得する。
    """

    try:
        current = Version(str(current_version))
    except InvalidVersion as exc:
        raise _fail("current version is invalid", exc) from None
    base_url = _validate_update_url(update_url)
    # Fail before constructing a network client.  A bootstrap carrying the
    # restored policy must never contact a repository through an old CA bundle.
    if _bootstrap_certifi_version() < Version(MINIMUM_CERTIFI):
        raise _fail("bootstrap certifi is below the release minimum")
    work_dir = _ensure_work_dir(work_dir)
    _notify(progress, "preparing-trusted-root")
    metadata_dir = work_dir / "metadata"
    root_path = _copy_trusted_root(Path(trusted_root), work_dir)
    target_dir = work_dir / "targets"
    _assert_directory(target_dir, "release target cache", create=True)
    app_destination = work_dir / "app"
    staging_dir = work_dir / "update_staging"
    _assert_directory(staging_dir, "update staging", create=True)

    try:
        client = client_factory(
            app_name=APP_NAME,
            app_install_dir=app_destination,
            current_version=str(current_version),
            metadata_dir=metadata_dir,
            metadata_base_url=base_url + "metadata/",
            target_dir=target_dir,
            target_base_url=base_url + "targets/",
            extract_dir=None,
        )
    except Exception as exc:
        raise _fail("TUF client initialization failed", exc) from None
    from .verification import record_roots

    record_roots(client, work_dir)
    meta, target_name, target_version = _select_full_target(
        client, current, allow_current_version=allow_current_version
    )
    target_info = _client_target_info(client, meta, target_name)
    if target_info is None:
        raise _fail("signed release target is missing")
    _target_path_is_safe(getattr(target_info, "path", target_name), target_name)
    _notify(progress, "downloading-release")
    archive_path = _download_target(client, target_info, target_dir, target_name)
    target_hash = _verify_target_file(archive_path, target_info)

    app_extractor = extractor or _safe_extract_app_archive
    try:
        extracted = app_extractor(archive_path, app_destination)
    except LegacyBootstrapError:
        raise
    except Exception as exc:
        raise _fail("release archive extraction failed", exc) from None
    if extracted is not None and Path(extracted).absolute() != app_destination.absolute():
        raise _fail("release extractor returned an unexpected path")
    _assert_directory(app_destination, "release app", create=False)

    _notify(progress, "validating-release")
    try:
        exact = validate_release_tree(
            app_destination,
            expected_platform="windows",
            expected_cpu="x86_64",
        )
    except Exception as exc:
        raise _fail("release manifest validation failed", exc) from None
    manifest = exact["manifest"]
    try:
        manifest_version = Version(str(manifest.get("release_version") or ""))
    except InvalidVersion as exc:
        raise _fail("release manifest version is invalid", exc) from None
    if manifest_version != target_version:
        raise _fail("release target and manifest versions differ")
    policy = validate_bootstrap_policy(app_destination, exact)

    manifest_info = dict(exact)
    manifest_info.update(
        {
            "release_version": str(manifest.get("release_version")),
            "target_name": target_name,
            "target_version": str(target_version),
            "target_length": _target_metadata_length(target_info),
            "target_sha256": target_hash,
            "legacy_bootstrap_policy": dict(policy),
        }
    )
    receipt_info: dict[str, Any] = {
        "schema_version": 1,
        "source_version": str(current_version),
        "app_path": "app",
        "target_path": f"targets/{target_name}",
        "target_name": target_name,
        "target_version": str(target_version),
        "target_length": _target_metadata_length(target_info),
        "target_sha256": target_hash,
        "manifest_digest": exact["manifest_digest"],
        "trusted_root_sha256": _target_sha256(root_path),
    }

    runtime_binding = manifest.get("components", {}).get("runtime", {})
    if isinstance(runtime_binding, Mapping) and runtime_binding.get("present") is True:
        _notify(progress, "downloading-runtime")
        runtime_url = _runtime_repository_url(base_url)
        runtime_factory = runtime_client_factory
        runtime_kwargs: dict[str, Any] = {
            "update_url": runtime_url,
        }
        if runtime_tuf_client_factory is not None:
            runtime_kwargs["client_factory"] = runtime_tuf_client_factory
        try:
            runtime_manager = runtime_factory(work_dir, **runtime_kwargs)
            runtime_path = Path(runtime_manager.acquire(manifest)).absolute()
        except Exception as exc:
            raise _fail("signed runtime acquisition failed", exc) from None
        try:
            runtime_relative = runtime_path.relative_to(work_dir.absolute()).as_posix()
        except ValueError as exc:
            raise _fail("runtime staging escaped bootstrap workspace", exc) from None
        if _is_reparse_point(runtime_path) or not runtime_path.is_dir():
            raise _fail("runtime staging is unsafe")
        receipt_info["runtime_path"] = runtime_relative
        receipt_info["runtime_target_name"] = str(runtime_binding.get("target_name"))
        receipt_info["runtime_manifest_digest"] = str(runtime_binding.get("manifest_digest"))
        receipt_info["runtime_artifact_digest"] = str(runtime_binding.get("artifact_digest"))
    _notify(progress, "release-ready")
    return app_destination, manifest_info, receipt_info


__all__ = [
    "APP_NAME",
    "MAX_ARCHIVE_BYTES",
    "MAX_EXTRACTED_BYTES",
    "MAX_FILES",
    "LegacyBootstrapError",
    "acquire_release",
    "validate_bootstrap_policy",
]
