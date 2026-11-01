"""Windows x64用の別署名runtime component契約。

Package 2では、Node／Wranglerの実行閉包をアプリ本体と別artifactとして扱う。
このモジュールは配布ビルダーと更新hostの両方から使えるよう、標準ライブラリだけで
runtime treeのmanifestを生成・原子的に保存し、展開後の全ファイルを検証する。

manifestにはruntimeの中身や設定値を埋め込まず、相対path、size、SHA-256と固定版情報、
監査結果だけを記録する。絶対path、秘密らしいキー、Windowsで衝突／作成不能なpath、
symlink・特殊ファイルはfail-closedで拒否する。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping

from .contracts import _windows_path_key, _normalize_relative_path


RUNTIME_MANIFEST_FILENAME = "runtime_manifest.json"
RUNTIME_MANIFEST_SCHEMA_VERSION = 1
RUNTIME_PLATFORM = "windows"
RUNTIME_CPU = "x86_64"
RUNTIME_MAX_BYTES = 320 * 1024 * 1024
RUNTIME_MAX_FILES = 2500

RUNTIME_NODE_ENTRY = "node.exe"
RUNTIME_LOCK_ENTRY = "wrangler/package-lock.json"
RUNTIME_WRANGLER_ENTRY = "wrangler/node_modules/wrangler/bin/wrangler.js"
RUNTIME_WRANGLER_CLI_ENTRY = "wrangler/node_modules/wrangler/wrangler-dist/cli.js"
RUNTIME_SBOM_ENTRY = "sbom.cdx.json"
RUNTIME_LICENSE_INVENTORY_ENTRY = "licenses/third-party.json"
RUNTIME_REQUIRED_ENTRIES = (
    RUNTIME_NODE_ENTRY,
    RUNTIME_LOCK_ENTRY,
    RUNTIME_WRANGLER_ENTRY,
    RUNTIME_WRANGLER_CLI_ENTRY,
    RUNTIME_SBOM_ENTRY,
    RUNTIME_LICENSE_INVENTORY_ENTRY,
)

# 呼び出し側で一般的に使われる短い別名も固定し、容量／必須pathの契約を
# 呼び出し側が独自に再定義しないようにする。
RUNTIME_MANIFEST_VERSION = RUNTIME_MANIFEST_SCHEMA_VERSION
MAX_RUNTIME_BYTES = RUNTIME_MAX_BYTES
MAX_RUNTIME_FILES = RUNTIME_MAX_FILES
REQUIRED_RUNTIME_ENTRIES = RUNTIME_REQUIRED_ENTRIES

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+\-]{0,127}$")
_TUF_TARGET_NAME = re.compile(r"^LiteRuntime-[0-9A-Za-z._+\-]{1,188}\.tar\.gz$")
_RELEASE_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$")
_SECRET_KEY_PARTS = {
    "secret",
    "secrets",
    "token",
    "tokens",
    "api_key",
    "apikey",
    "password",
    "credential",
    "credentials",
    "private_key",
    "access_key",
    "authorization",
    "body",
    "content",
}
_ALLOWED_TOP_LEVEL_KEYS = {
    "schema_version",
    "component",
    "node_version",
    "wrangler_version",
    "target",
    "node",
    "wrangler",
    "entrypoints",
    "runtime_lock",
    "sbom",
    "license_inventory",
    "audit",
    "entries",
    "file_count",
    "total_bytes",
    "budget",
    "required_entries",
    "app_compatibility",
    "artifact_digest",
}
_ALLOWED_REFERENCE_KEYS = {"path", "size", "sha256"}
_ALLOWED_EXECUTABLE_KEYS = {"version", "entry"}
_ALLOWED_COMPONENT_KEYS = {"id", "version"}
_ALLOWED_ENTRYPOINT_KEYS = {"node", "wrangler", "wrangler_cli"}
_ALLOWED_TARGET_KEYS = {"platform", "cpu"}
_ALLOWED_BUDGET_KEYS = {"max_bytes", "max_files"}
_ALLOWED_AUDIT_KEYS = {
    "critical",
    "high",
    "moderate",
    "low",
    "info",
    "unknown_license",
}
_ALLOWED_APP_COMPATIBILITY_KEYS = {"min_release", "max_release"}

_FORBIDDEN_RUNTIME_NAMES = {
    "npm",
    "npm.cmd",
    "npm.exe",
    "npm.ps1",
    "npx",
    "npx.cmd",
    "npx.exe",
    "npx.ps1",
    "corepack",
    "corepack.cmd",
    "corepack.exe",
    "corepack.ps1",
    ".npmrc",
    ".env",
    ".dev.vars",
    ".wrangler",
}
_ALLOWED_INTERNAL_RUNTIME_DIRECTORIES = {
    ("wrangler", "node_modules", "unenv", "dist", "runtime", "npm"),
}
_ALLOWED_NATIVE_PACKAGES = {
    "@cloudflare/workerd-windows-64",
    "@esbuild/win32-x64",
    "@img/sharp-wasm32",
    "@img/sharp-win32-x64",
}
_NATIVE_PACKAGE_PREFIXES = (
    "@cloudflare/workerd-",
    "@esbuild/",
    "@img/sharp-",
    "workerd-",
    "esbuild-",
    "sharp-",
)


def _extended_windows_path(value: str) -> str:
    """検証済みhost pathのI/O表記だけを拡張する。device namespaceは許可しない。"""
    raw = value.replace("/", "\\")
    if raw.startswith("\\\\.\\"):
        raise ValueError("runtime filesystem path is unsafe")
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
        if raw[:4].casefold() == "unc\\":
            raw = "\\\\" + raw[4:]
    path = PureWindowsPath(raw)
    if not path.is_absolute() or any(ord(char) < 32 for char in raw):
        raise ValueError("runtime filesystem path is unsafe")
    # Check before PureWindowsPath can collapse dot components. Extended paths
    # must not reinterpret traversal, DOS names, streams or trailing dots.
    if any(part in {".", ".."} for part in raw.split("\\")):
        raise ValueError("runtime filesystem path is unsafe")
    parts = list(path.parts[1:])
    if path.drive.startswith("\\\\"):
        parts += path.drive[2:].split("\\")
        prefix = "\\\\?\\UNC\\" + str(path)[2:]
    elif re.fullmatch(r"[A-Za-z]:", path.drive):
        prefix = "\\\\?\\" + str(path)
    else:
        raise ValueError("runtime filesystem path is unsafe")
    try:
        for part in parts:
            _windows_path_key(part)
    except ValueError:
        raise ValueError("runtime filesystem path is unsafe") from None
    return prefix


def _runtime_io_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if os.name != "nt":
        return path
    # Refuse traversal before abspath normalizes it. The returned extended
    # path is private to filesystem I/O, never an archive member or receipt.
    if ".." in path.parts:
        raise ValueError("runtime filesystem path is unsafe")
    return Path(_extended_windows_path(os.path.abspath(path)))


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def runtime_manifest_digest(manifest: Mapping[str, Any]) -> str:
    """manifest本文の決定的digestを返す（runtime_manifest自身は含めない）。"""

    return hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()


def build_runtime_binding(
    manifest: Mapping[str, Any], *, target_name: str
) -> dict[str, Any]:
    """アプリrelease manifestへ埋め込むruntimeの署名済み結合情報を返す。"""

    _validate_manifest_shape(manifest)
    normalized_target = str(target_name).strip()
    if not _TUF_TARGET_NAME.fullmatch(normalized_target):
        raise ValueError("runtime TUF target name is invalid")
    return {
        "present": True,
        "id": str(manifest["component"]["id"]),
        "version": str(manifest["component"]["version"]),
        "target_name": normalized_target,
        "manifest_digest": runtime_manifest_digest(manifest),
        "artifact_digest": str(manifest["artifact_digest"]),
        "node_version": str(manifest["node_version"]),
        "wrangler_version": str(manifest["wrangler_version"]),
    }


# 呼び出し側がPackage 0のmanifest_digest名を使っても誤解しないよう互換別名を公開する。
manifest_digest = runtime_manifest_digest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_relative(value: str | os.PathLike[str], *, label: str) -> str:
    try:
        normalized = _normalize_relative_path(value)
        if any(ord(character) < 32 for character in normalized):
            raise ValueError(f"Windows-incompatible runtime path: {normalized}")
        # _windows_path_key additionally rejects drive letters, reserved names,
        # trailing dots/spaces and invalid Windows characters.
        _windows_path_key(normalized)
    except (TypeError, ValueError) as exc:
        if str(exc).startswith("Windows-"):
            raise
        raise ValueError(f"invalid runtime {label}: {value}") from exc
    return normalized


def _contains_forbidden_manifest_value(value: Any, *, key: str | None = None) -> bool:
    """秘密キーまたは絶対pathをmanifestへ持ち込まない。"""

    if key is not None:
        key_name = str(key).casefold()
        if key_name in _SECRET_KEY_PARTS or any(
            part in key_name.split("_") for part in _SECRET_KEY_PARTS
        ):
            return True
    if isinstance(value, Mapping):
        return any(
            _contains_forbidden_manifest_value(item, key=str(name))
            for name, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_manifest_value(item) for item in value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("/", "\\")):
            return True
        if re.match(r"^[A-Za-z]:[\\/]", stripped):
            return True
        if stripped.casefold().startswith("file://"):
            return True
    return False


def _assert_plain_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"runtime manifest {label} must be an object")
    return value


def _assert_exact_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unexpected = set(value) - allowed
    if unexpected:
        raise ValueError(f"runtime manifest {label} has unknown keys: {', '.join(sorted(map(str, unexpected)))}")


def _validate_reference(value: Any, label: str) -> dict[str, Any]:
    reference = _assert_plain_mapping(value, label)
    _assert_exact_keys(reference, _ALLOWED_REFERENCE_KEYS, label)
    path = _ensure_relative(str(reference.get("path") or ""), label=f"{label} path")
    size = reference.get("size")
    digest = str(reference.get("sha256") or "")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError(f"runtime manifest {label} size is invalid")
    if not _SHA256.fullmatch(digest):
        raise ValueError(f"runtime manifest {label} sha256 is invalid")
    return {"path": path, "size": size, "sha256": digest}


def _validate_entries(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("runtime manifest entries must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    windows_paths: dict[str, str] = {}
    for raw in value:
        entry = _assert_plain_mapping(raw, "entry")
        _assert_exact_keys(entry, {"path", "size", "sha256"}, "entry")
        path = _ensure_relative(str(entry.get("path") or ""), label="entry path")
        if path == RUNTIME_MANIFEST_FILENAME:
            raise ValueError("runtime manifest must not list itself")
        _assert_runtime_file_policy(path)
        if path in indexed:
            raise ValueError(f"duplicate runtime manifest path: {path}")
        windows_key = _windows_path_key(path)
        if windows_key in windows_paths:
            raise ValueError(
                f"Windows path collision in runtime manifest: {windows_paths[windows_key]} / {path}"
            )
        windows_paths[windows_key] = path
        size = entry.get("size")
        digest = str(entry.get("sha256") or "")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"invalid runtime file size: {path}")
        if not _SHA256.fullmatch(digest):
            raise ValueError(f"invalid runtime file sha256: {path}")
        indexed[path] = {"path": path, "size": size, "sha256": digest}
    return indexed


def _runtime_package_name(parts: tuple[str, ...], node_modules_index: int) -> str | None:
    if node_modules_index + 1 >= len(parts):
        return None
    first = parts[node_modules_index + 1]
    if first.startswith("@"):
        if node_modules_index + 2 >= len(parts):
            return None
        return first + "/" + parts[node_modules_index + 2]
    return first


def _assert_runtime_file_policy(relative: str) -> None:
    parts = tuple(PurePosixPath(relative).parts)
    folded = tuple(part.casefold() for part in parts)
    for index, part in enumerate(folded):
        if (
            part in _FORBIDDEN_RUNTIME_NAMES
            and folded[: index + 1] not in _ALLOWED_INTERNAL_RUNTIME_DIRECTORIES
        ):
            raise ValueError(f"forbidden runtime file or directory: {relative}")
    for index, part in enumerate(folded):
        if part != "node_modules":
            continue
        package = _runtime_package_name(folded, index)
        if package is None:
            continue
        # npm itself is rejected above; native packages are restricted to the
        # Windows x64 builds and the platform-neutral sharp WASM fallback
        # observed in the fixed-lock clean closure.
        if package.startswith(_NATIVE_PACKAGE_PREFIXES) and package not in {
            value.casefold() for value in _ALLOWED_NATIVE_PACKAGES
        }:
            raise ValueError(f"unsupported native runtime package: {relative}")


def _read_json_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"runtime {label} JSON cannot be read") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"runtime {label} JSON must be an object")
    return value


def _validate_sbom_file(path: Path) -> None:
    sbom = _read_json_object(path, "SBOM")
    if sbom.get("bomFormat") != "CycloneDX":
        raise ValueError("runtime SBOM must use CycloneDX")
    components = sbom.get("components")
    if not isinstance(components, list):
        raise ValueError("runtime SBOM components must be a list")


def _validate_license_inventory_file(path: Path) -> None:
    inventory = _read_json_object(path, "license inventory")
    unknown_key = next(
        (key for key in ("unknown", "unknown_license", "unknownLicenses") if key in inventory),
        None,
    )
    if unknown_key is None:
        raise ValueError("runtime license inventory must declare unknown")
    unknown = inventory[unknown_key]
    if isinstance(unknown, bool) or not isinstance(unknown, int) or unknown != 0:
        raise ValueError("runtime license inventory has unknown licenses")


def _validate_audit(value: Any) -> dict[str, int]:
    audit = _assert_plain_mapping(value, "audit")
    _assert_exact_keys(audit, _ALLOWED_AUDIT_KEYS, "audit")
    normalized: dict[str, int] = {}
    for key in _ALLOWED_AUDIT_KEYS:
        raw = audit.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError(f"runtime audit count is invalid: {key}")
        normalized[key] = raw
    if normalized["critical"] != 0 or normalized["high"] != 0:
        raise ValueError("runtime audit has critical/high findings")
    if normalized["unknown_license"] != 0:
        raise ValueError("runtime license inventory has unknown licenses")
    return normalized


def _release_version_key(value: Any, label: str) -> tuple[int, int, int, int]:
    match = _RELEASE_VERSION.fullmatch(str(value or ""))
    if not match:
        raise ValueError(f"runtime app compatibility version is invalid: {label}")
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def _validate_manifest_shape(
    manifest: Mapping[str, Any],
    *,
    expected_platform: str = RUNTIME_PLATFORM,
    expected_cpu: str = RUNTIME_CPU,
    max_bytes: int = RUNTIME_MAX_BYTES,
    max_files: int = RUNTIME_MAX_FILES,
) -> dict[str, dict[str, Any]]:
    if not isinstance(manifest, Mapping):
        raise ValueError("runtime manifest must be an object")
    _assert_exact_keys(manifest, _ALLOWED_TOP_LEVEL_KEYS, "root")
    if _contains_forbidden_manifest_value(manifest):
        raise ValueError("runtime manifest contains a secret or absolute path")
    if manifest.get("schema_version") != RUNTIME_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported runtime manifest schema")
    component = _assert_plain_mapping(manifest.get("component"), "component")
    _assert_exact_keys(component, _ALLOWED_COMPONENT_KEYS, "component")
    component_id = str(component.get("id") or "")
    component_version = str(component.get("version") or "")
    if component_id != "lite-wrangler" or not _VERSION.fullmatch(component_version):
        raise ValueError("runtime manifest component mismatch")
    node_version_top = str(manifest.get("node_version") or "")
    wrangler_version_top = str(manifest.get("wrangler_version") or "")
    if not _VERSION.fullmatch(node_version_top) or not _VERSION.fullmatch(wrangler_version_top):
        raise ValueError("runtime top-level version is invalid")

    target = _assert_plain_mapping(manifest.get("target"), "target")
    _assert_exact_keys(target, _ALLOWED_TARGET_KEYS, "target")
    platform = str(target.get("platform") or "").casefold()
    cpu = str(target.get("cpu") or "").casefold()
    if platform != expected_platform.casefold():
        raise ValueError(f"runtime target platform mismatch: {platform}")
    if cpu != expected_cpu.casefold():
        raise ValueError(f"runtime target cpu mismatch: {cpu}")
    if platform != RUNTIME_PLATFORM or cpu != RUNTIME_CPU:
        raise ValueError("only Windows x86_64 runtime artifacts are accepted")

    for label in ("node", "wrangler"):
        executable = _assert_plain_mapping(manifest.get(label), label)
        _assert_exact_keys(executable, _ALLOWED_EXECUTABLE_KEYS, label)
        version = str(executable.get("version") or "")
        if not _VERSION.fullmatch(version):
            raise ValueError(f"runtime {label} version is invalid")
        _ensure_relative(str(executable.get("entry") or ""), label=f"{label} entry")
    if str(manifest["node"]["version"]) != node_version_top:
        raise ValueError("runtime node version mismatch")
    if str(manifest["wrangler"]["version"]) != wrangler_version_top:
        raise ValueError("runtime wrangler version mismatch")
    entrypoints = _assert_plain_mapping(manifest.get("entrypoints"), "entrypoints")
    _assert_exact_keys(entrypoints, _ALLOWED_ENTRYPOINT_KEYS, "entrypoints")
    entrypoint_values = {
        key: _ensure_relative(str(entrypoints.get(key) or ""), label=f"entrypoint {key}")
        for key in _ALLOWED_ENTRYPOINT_KEYS
    }
    if entrypoint_values != {
        "node": RUNTIME_NODE_ENTRY,
        "wrangler": RUNTIME_WRANGLER_ENTRY,
        "wrangler_cli": RUNTIME_WRANGLER_CLI_ENTRY,
    }:
        raise ValueError("runtime entrypoints mismatch")

    references = {
        "runtime_lock": _validate_reference(manifest.get("runtime_lock"), "runtime_lock"),
        "sbom": _validate_reference(manifest.get("sbom"), "sbom"),
        "license_inventory": _validate_reference(
            manifest.get("license_inventory"), "license_inventory"
        ),
    }
    indexed = _validate_entries(manifest.get("entries"))
    if manifest.get("file_count") != len(indexed):
        raise ValueError("runtime manifest file count mismatch")
    if manifest.get("total_bytes") != sum(entry["size"] for entry in indexed.values()):
        raise ValueError("runtime manifest total size mismatch")
    budget = _assert_plain_mapping(manifest.get("budget"), "budget")
    _assert_exact_keys(budget, _ALLOWED_BUDGET_KEYS, "budget")
    budget_bytes = budget.get("max_bytes")
    budget_files = budget.get("max_files")
    if (
        isinstance(budget_bytes, bool)
        or not isinstance(budget_bytes, int)
        or budget_bytes <= 0
        or budget_bytes > RUNTIME_MAX_BYTES
    ):
        raise ValueError("runtime byte budget is invalid")
    if (
        isinstance(budget_files, bool)
        or not isinstance(budget_files, int)
        or budget_files <= 0
        or budget_files > RUNTIME_MAX_FILES
    ):
        raise ValueError("runtime file budget is invalid")
    if len(indexed) > budget_files or sum(entry["size"] for entry in indexed.values()) > budget_bytes:
        raise ValueError("runtime artifact exceeds its budget")
    if len(indexed) > max_files or sum(entry["size"] for entry in indexed.values()) > max_bytes:
        raise ValueError("runtime artifact exceeds validation budget")

    raw_required = manifest.get("required_entries")
    if not isinstance(raw_required, list):
        raise ValueError("runtime manifest required entries must be a list")
    required = {
        _ensure_relative(str(path), label="required entry")
        for path in raw_required
    }
    required_baseline = set(RUNTIME_REQUIRED_ENTRIES[:4]) | {
        references["sbom"]["path"],
        references["license_inventory"]["path"],
    }
    if not required_baseline <= required:
        raise ValueError("runtime manifest required entries mismatch")
    if not required <= indexed.keys():
        raise ValueError("runtime manifest required files are missing")
    for label, reference in references.items():
        if reference["path"] not in indexed:
            raise ValueError(f"runtime {label} reference is not listed in entries")
        indexed_reference = indexed[reference["path"]]
        if indexed_reference["size"] != reference["size"] or indexed_reference["sha256"] != reference["sha256"]:
            raise ValueError(f"runtime {label} reference hash mismatch")

    # Entry references must point at the declared executable paths.
    if str(manifest["node"]["entry"]) != RUNTIME_NODE_ENTRY:
        raise ValueError("runtime node entry mismatch")
    if str(manifest["wrangler"]["entry"]) != RUNTIME_WRANGLER_ENTRY:
        raise ValueError("runtime wrangler entry mismatch")
    artifact_digest = str(manifest.get("artifact_digest") or "")
    if not _SHA256.fullmatch(artifact_digest):
        raise ValueError("runtime artifact digest is invalid")
    expected_artifact_digest = hashlib.sha256(
        _canonical_json_bytes(
            [indexed[path] for path in sorted(indexed)]
        )
    ).hexdigest()
    if artifact_digest != expected_artifact_digest:
        raise ValueError("runtime artifact digest mismatch")
    app_compatibility = _assert_plain_mapping(
        manifest.get("app_compatibility"), "app_compatibility"
    )
    _assert_exact_keys(
        app_compatibility, _ALLOWED_APP_COMPATIBILITY_KEYS, "app_compatibility"
    )
    minimum_release = _release_version_key(
        app_compatibility.get("min_release"), "min_release"
    )
    maximum_release = _release_version_key(
        app_compatibility.get("max_release"), "max_release"
    )
    if minimum_release > maximum_release:
        raise ValueError("runtime app compatibility range is invalid")
    return indexed


def _iter_runtime_files(root: Path) -> Iterable[tuple[str, Path]]:
    root = _runtime_io_path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("runtime root must be a real directory")
    for ancestor in (root, *root.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("runtime root must be a real directory")
    seen_windows_paths: dict[str, str] = {}
    def walk(parent: Path):
        for child in sorted(parent.iterdir(), key=lambda item: item.name):
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("runtime tree must not contain symlinks or reparse points")
            yield child
            if stat.S_ISDIR(info.st_mode):
                yield from walk(child)

    for path in walk(root):
        relative_path = path.relative_to(root)
        if any("\\" in part for part in relative_path.parts):
            raise ValueError(f"Windows-incompatible runtime file path: {relative_path}")
        relative = _ensure_relative(relative_path.as_posix(), label="file path")
        if relative == RUNTIME_MANIFEST_FILENAME:
            if not path.is_file():
                raise ValueError("runtime manifest path must be a regular file")
            continue
        _assert_runtime_file_policy(relative)
        if path.is_dir():
            continue
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"runtime tree contains a special file: {relative}")
        windows_key = _windows_path_key(relative)
        previous = seen_windows_paths.get(windows_key)
        if previous is not None:
            raise ValueError(f"Windows path collision in runtime tree: {previous} / {relative}")
        seen_windows_paths[windows_key] = relative
        yield relative, path


def _resolve_reference(root: Path, value: str | os.PathLike[str] | None, candidates: tuple[str, ...], label: str) -> str:
    if value is None:
        for candidate in candidates:
            candidate_path = root / candidate
            if candidate_path.is_file():
                return candidate
        raise ValueError(f"runtime {label} reference is missing")
    normalized = _ensure_relative(value, label=f"{label} path")
    if not (root / normalized).is_file():
        raise ValueError(f"runtime {label} file is missing: {normalized}")
    return normalized


def _file_reference(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"runtime reference is not a regular file: {relative}")
    return {"path": relative, "size": path.stat().st_size, "sha256": _sha256_file(path)}


def _coerce_audit(
    audit: Mapping[str, Any] | None,
    *,
    audit_critical: int | None,
    audit_high: int | None,
    audit_unknown_license: int | None,
) -> dict[str, int]:
    if audit is None:
        if audit_critical is None or audit_high is None:
            raise ValueError("runtime audit critical/high counts are required")
        audit = {
            "critical": audit_critical,
            "high": audit_high,
            "unknown_license": 0 if audit_unknown_license is None else audit_unknown_license,
        }
    else:
        audit = dict(audit)
        vulnerabilities = audit.pop("vulnerabilities", None)
        if isinstance(vulnerabilities, Mapping):
            for key in ("critical", "high", "moderate", "low", "info"):
                if key not in audit and key in vulnerabilities:
                    audit[key] = vulnerabilities[key]
        license_counts = audit.pop("license", audit.pop("licenses", None))
        if isinstance(license_counts, Mapping) and "unknown_license" not in audit:
            if "unknown_license" in license_counts:
                audit["unknown_license"] = license_counts["unknown_license"]
            elif "unknown" in license_counts:
                audit["unknown_license"] = license_counts["unknown"]
        if "unknown" in audit and "unknown_license" not in audit:
            audit["unknown_license"] = audit.pop("unknown")
        if "license_unknown" in audit and "unknown_license" not in audit:
            audit["unknown_license"] = audit.pop("license_unknown")
        if audit_critical is not None:
            audit["critical"] = audit_critical
        if audit_high is not None:
            audit["high"] = audit_high
        if audit_unknown_license is not None:
            audit["unknown_license"] = audit_unknown_license
    return _validate_audit(audit)


def build_runtime_manifest(
    runtime_root: str | os.PathLike[str],
    *,
    node_version: str,
    wrangler_version: str,
    audit: Mapping[str, Any] | None = None,
    runtime_lock_path: str | os.PathLike[str] | None = None,
    sbom_path: str | os.PathLike[str] | None = None,
    license_inventory_path: str | os.PathLike[str] | None = None,
    app_compatibility: Mapping[str, Any] | None = None,
    component_id: str = "lite-wrangler",
    component_version: str | None = None,
    target_platform: str = RUNTIME_PLATFORM,
    target_cpu: str = RUNTIME_CPU,
    max_bytes: int = RUNTIME_MAX_BYTES,
    max_files: int = RUNTIME_MAX_FILES,
    audit_critical: int | None = None,
    audit_high: int | None = None,
    audit_unknown_license: int | None = None,
    **aliases: Any,
) -> dict[str, Any]:
    """runtime treeを走査し、秘密を含まないmanifestを生成する。"""

    if runtime_lock_path is None:
        runtime_lock_path = aliases.pop("runtime_lock", aliases.pop("lock_path", None))
    if sbom_path is None:
        sbom_path = aliases.pop("sbom", aliases.pop("sbom_file", None))
    if license_inventory_path is None:
        license_inventory_path = aliases.pop(
            "license_inventory",
            aliases.pop("license_inventory_file", aliases.pop("licenses", None)),
        )
    if aliases:
        raise TypeError(f"unknown runtime manifest options: {', '.join(sorted(aliases))}")
    root = Path(runtime_root)
    if (
        not isinstance(target_platform, str)
        or not isinstance(target_cpu, str)
        or target_platform.casefold() != RUNTIME_PLATFORM
        or target_cpu.casefold() != RUNTIME_CPU
    ):
        raise ValueError("only Windows x86_64 runtime artifacts can be generated")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
        or max_bytes > RUNTIME_MAX_BYTES
    ):
        raise ValueError("runtime byte budget is invalid")
    if (
        isinstance(max_files, bool)
        or not isinstance(max_files, int)
        or max_files <= 0
        or max_files > RUNTIME_MAX_FILES
    ):
        raise ValueError("runtime file budget is invalid")
    if (
        not isinstance(node_version, str)
        or not isinstance(wrangler_version, str)
        or not _VERSION.fullmatch(node_version)
        or not _VERSION.fullmatch(wrangler_version)
    ):
        raise ValueError("runtime version is invalid")
    audit_value = _coerce_audit(
        audit,
        audit_critical=audit_critical,
        audit_high=audit_high,
        audit_unknown_license=audit_unknown_license,
    )
    compatibility = dict(
        app_compatibility
        or {"min_release": "0.0.0", "max_release": "9999.9999.9999.9999"}
    )
    if _contains_forbidden_manifest_value(compatibility):
        raise ValueError("runtime app compatibility contains a secret or absolute path")
    if (
        not isinstance(component_id, str)
        or component_id != "lite-wrangler"
        or not isinstance(component_version, (str, type(None)))
    ):
        raise ValueError("runtime component id is invalid")
    component_version_value = component_version or (
        f"node-{node_version}_wrangler-{wrangler_version}"
    )
    if not _VERSION.fullmatch(component_version_value):
        raise ValueError("runtime component version is invalid")

    lock_relative = _resolve_reference(
        root, runtime_lock_path, (RUNTIME_LOCK_ENTRY,), "runtime lock"
    )
    if lock_relative != RUNTIME_LOCK_ENTRY:
        raise ValueError("runtime lock path must be wrangler/package-lock.json")
    sbom_relative = _resolve_reference(
        root,
        sbom_path,
        (
            RUNTIME_SBOM_ENTRY,
            "sbom.json",
            "sbom/cyclonedx.json",
            "sbom/sbom.json",
            "SBOM.json",
            "bom.json",
        ),
        "SBOM",
    )
    license_relative = _resolve_reference(
        root,
        license_inventory_path,
        (
            RUNTIME_LICENSE_INVENTORY_ENTRY,
            "third-party-license-inventory.json",
            "licenses/third-party-license-inventory.json",
            "third_party_license_inventory.json",
            "licenses.json",
            "licenses/NOTICE.json",
        ),
        "license inventory",
    )
    _validate_sbom_file(root / sbom_relative)
    _validate_license_inventory_file(root / license_relative)
    # Required executable paths are fixed by ADR 036 and cannot be redirected
    # to a system PATH or arbitrary user-selected binary.
    for required in RUNTIME_REQUIRED_ENTRIES[:4]:
        if not (root / required).is_file() or (root / required).is_symlink():
            raise ValueError(f"runtime required file is missing: {required}")

    entries: list[dict[str, Any]] = []
    for relative, path in _iter_runtime_files(root):
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    entries.sort(key=lambda entry: entry["path"])
    total_bytes = sum(entry["size"] for entry in entries)
    if len(entries) > max_files or total_bytes > max_bytes:
        raise ValueError("runtime artifact exceeds budget")
    indexed = {entry["path"]: entry for entry in entries}
    required_entries = sorted(
        set(RUNTIME_REQUIRED_ENTRIES[:4]) | {sbom_relative, license_relative}
    )
    if not set(required_entries) <= indexed.keys():
        raise ValueError("runtime manifest required files are missing")
    references = {
        "runtime_lock": indexed[lock_relative],
        "sbom": indexed[sbom_relative],
        "license_inventory": indexed[license_relative],
    }
    manifest: dict[str, Any] = {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "component": {"id": component_id, "version": component_version_value},
        "node_version": node_version,
        "wrangler_version": wrangler_version,
        "target": {"platform": RUNTIME_PLATFORM, "cpu": RUNTIME_CPU},
        "node": {"version": str(node_version), "entry": RUNTIME_NODE_ENTRY},
        "wrangler": {"version": str(wrangler_version), "entry": RUNTIME_WRANGLER_ENTRY},
        "entrypoints": {
            "node": RUNTIME_NODE_ENTRY,
            "wrangler": RUNTIME_WRANGLER_ENTRY,
            "wrangler_cli": RUNTIME_WRANGLER_CLI_ENTRY,
        },
        "runtime_lock": dict(references["runtime_lock"]),
        "sbom": dict(references["sbom"]),
        "license_inventory": dict(references["license_inventory"]),
        "audit": audit_value,
        "entries": entries,
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "budget": {"max_bytes": int(max_bytes), "max_files": int(max_files)},
        "required_entries": required_entries,
        "app_compatibility": compatibility,
    }
    # Digest of the artifact inventory is useful for the outer release
    # manifest, but is not a path or secret and is not self-referential.
    manifest["artifact_digest"] = hashlib.sha256(_canonical_json_bytes(entries)).hexdigest()
    _validate_manifest_shape(
        manifest,
        expected_platform=RUNTIME_PLATFORM,
        expected_cpu=RUNTIME_CPU,
        max_bytes=max_bytes,
        max_files=max_files,
    )
    return manifest


def write_runtime_manifest(
    runtime_root: str | os.PathLike[str], manifest: Mapping[str, Any]
) -> Path:
    """runtime_manifest.jsonをflush＋atomic replaceで保存する。"""

    root = Path(runtime_root)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("runtime root must be a real directory")
    _validate_manifest_shape(manifest)
    destination = root / RUNTIME_MANIFEST_FILENAME
    root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=RUNTIME_MANIFEST_FILENAME + ".", suffix=".tmp", dir=str(root)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return destination


def read_runtime_manifest(runtime_root: str | os.PathLike[str]) -> dict[str, Any]:
    root = _runtime_io_path(runtime_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("runtime root must be a real directory")
    for ancestor in (root, *root.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("runtime root must be a real directory")
    try:
        manifest = json.loads((root / RUNTIME_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("runtime manifest cannot be read") from exc
    _validate_manifest_shape(manifest)
    return manifest


def validate_runtime_tree(
    runtime_root: str | os.PathLike[str],
    manifest: Mapping[str, Any] | None = None,
    *,
    expected_platform: str = RUNTIME_PLATFORM,
    expected_cpu: str = RUNTIME_CPU,
    max_bytes: int = RUNTIME_MAX_BYTES,
    max_files: int = RUNTIME_MAX_FILES,
) -> dict[str, Any]:
    """展開済みruntime全体をmanifestと照合する。"""

    root = _runtime_io_path(runtime_root)
    value = read_runtime_manifest(root) if manifest is None else dict(manifest)
    indexed = _validate_manifest_shape(
        value,
        expected_platform=expected_platform,
        expected_cpu=expected_cpu,
        max_bytes=max_bytes,
        max_files=max_files,
    )
    actual: dict[str, Path] = dict(_iter_runtime_files(root))
    if set(actual) != set(indexed):
        missing = sorted(set(indexed) - set(actual))
        extra = sorted(set(actual) - set(indexed))
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra=" + ",".join(extra))
        raise ValueError("runtime file inventory mismatch: " + " ".join(detail))
    for relative, path in actual.items():
        entry = indexed[relative]
        if path.stat().st_size != entry["size"] or _sha256_file(path) != entry["sha256"]:
            raise ValueError(f"runtime file hash or size mismatch: {relative}")
    _validate_sbom_file(root / value["sbom"]["path"])
    _validate_license_inventory_file(root / value["license_inventory"]["path"])
    return {
        "manifest": value,
        "manifest_digest": runtime_manifest_digest(value),
        "artifact_digest": value["artifact_digest"],
        "file_count": len(indexed),
        "total_bytes": value["total_bytes"],
    }


def validate_bound_runtime(
    release_manifest: Mapping[str, Any], runtime_root: str | os.PathLike[str]
) -> dict[str, Any]:
    """アプリrelease manifestと展開済みruntimeをdigest・互換範囲まで照合する。"""

    components = release_manifest.get("components")
    if not isinstance(components, Mapping):
        raise ValueError("release manifest components are required")
    binding = components.get("runtime")
    if not isinstance(binding, Mapping) or binding.get("present") is not True:
        raise ValueError("release manifest does not bind a runtime")
    exact = validate_runtime_tree(runtime_root)
    runtime_manifest = exact["manifest"]
    expected = build_runtime_binding(
        runtime_manifest,
        target_name=str(binding.get("target_name") or ""),
    )
    if dict(binding) != expected:
        raise ValueError("release/runtime binding mismatch")
    app_version = str(release_manifest.get("release_version") or "")
    app_key = _release_version_key(app_version, "release_version")
    compatibility = runtime_manifest["app_compatibility"]
    minimum = _release_version_key(compatibility["min_release"], "min_release")
    maximum = _release_version_key(compatibility["max_release"], "max_release")
    if not minimum <= app_key <= maximum:
        raise ValueError("release/runtime compatibility mismatch")
    return exact


def validate_runtime_inventory(
    manifest: Mapping[str, Any],
    inventory: Mapping[str, Mapping[str, Any]],
    *,
    expected_platform: str = RUNTIME_PLATFORM,
    expected_cpu: str = RUNTIME_CPU,
) -> dict[str, Any]:
    """展開前のarchive inventoryをmanifestと照合する。"""

    indexed = _validate_manifest_shape(
        manifest, expected_platform=expected_platform, expected_cpu=expected_cpu
    )
    normalized: dict[str, Mapping[str, Any]] = {}
    windows_paths: dict[str, str] = {}
    for raw_path, metadata in inventory.items():
        path = _ensure_relative(raw_path, label="inventory path")
        if path == RUNTIME_MANIFEST_FILENAME:
            continue
        _assert_runtime_file_policy(path)
        if path in normalized:
            raise ValueError(f"duplicate runtime inventory path: {path}")
        windows_key = _windows_path_key(path)
        if windows_key in windows_paths:
            raise ValueError(
                f"Windows path collision in runtime inventory: {windows_paths[windows_key]} / {path}"
            )
        windows_paths[windows_key] = path
        normalized[path] = metadata
    if set(normalized) != set(indexed):
        raise ValueError("runtime inventory path mismatch")
    for path, entry in indexed.items():
        metadata = _assert_plain_mapping(normalized[path], f"inventory {path}")
        _assert_exact_keys(metadata, {"size", "sha256"}, f"inventory {path}")
        if (
            isinstance(metadata.get("size"), bool)
            or not isinstance(metadata.get("size"), int)
            or metadata.get("size") < 0
            or not _SHA256.fullmatch(str(metadata.get("sha256") or ""))
        ):
            raise ValueError(f"invalid runtime inventory metadata: {path}")
        if metadata.get("size") != entry["size"] or metadata.get("sha256") != entry["sha256"]:
            raise ValueError(f"runtime inventory hash or size mismatch: {path}")
    return {
        "manifest_digest": runtime_manifest_digest(manifest),
        "artifact_digest": manifest["artifact_digest"],
        "file_count": len(indexed),
        "total_bytes": manifest["total_bytes"],
    }


# A descriptive alias used by archive validators.
validate_runtime_file_inventory = validate_runtime_inventory

# 名前が明示的な呼び出し側向けの別名。
generate_runtime_manifest = build_runtime_manifest
validate_runtime_manifest = validate_runtime_tree


__all__ = [
    "RUNTIME_MANIFEST_FILENAME",
    "RUNTIME_MANIFEST_SCHEMA_VERSION",
    "RUNTIME_PLATFORM",
    "RUNTIME_CPU",
    "RUNTIME_MAX_BYTES",
    "RUNTIME_MAX_FILES",
    "RUNTIME_REQUIRED_ENTRIES",
    "RUNTIME_MANIFEST_VERSION",
    "MAX_RUNTIME_BYTES",
    "MAX_RUNTIME_FILES",
    "REQUIRED_RUNTIME_ENTRIES",
    "build_runtime_manifest",
    "write_runtime_manifest",
    "read_runtime_manifest",
    "runtime_manifest_digest",
    "build_runtime_binding",
    "manifest_digest",
    "validate_runtime_tree",
    "validate_bound_runtime",
    "validate_runtime_inventory",
    "validate_runtime_file_inventory",
    "generate_runtime_manifest",
    "validate_runtime_manifest",
]
