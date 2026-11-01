"""Package 0で共有するrelease manifestと永続path契約。

このモジュールは配布build、archive validator、更新hostのすべてから利用する。
秘密値や環境依存の絶対pathは扱わず、Python標準ライブラリだけに依存する。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


RELEASE_MANIFEST_FILENAME = "release_manifest.json"
RELEASE_MANIFEST_SCHEMA_VERSION = 1
PERSISTENT_CATALOG_SCHEMA_VERSION = 1

# `preserve`は更新時に現行環境からnextへ引き継ぐ。
# `secret`もcopy方針はpreserveだが、診断・journalへ内容を出してはならない。
# `ephemeral`はPackage 0ではデータ損失を避けてpreserveし、cleanupを別操作にする。
PERSISTENT_PATH_RULES: tuple[dict[str, str], ...] = (
    {"pattern": "config.json", "kind": "secret"},
    {"pattern": "alarms.json", "kind": "preserve"},
    {"pattern": "redaction_rules.json", "kind": "preserve"},
    {"pattern": ".gemini_key_states.json", "kind": "secret"},
    {"pattern": "characters", "kind": "preserve"},
    {"pattern": "memories", "kind": "preserve"},
    {"pattern": "logs", "kind": "preserve"},
    {"pattern": "metadata", "kind": "secret"},
    {"pattern": "backups", "kind": "secret"},
    {"pattern": ".memos", "kind": "preserve"},
    {"pattern": "usage_logs", "kind": "preserve"},
    {"pattern": "user_closet", "kind": "preserve"},
    {"pattern": "cache", "kind": "secret"},
    {"pattern": "data", "kind": "preserve"},
    {"pattern": "temp", "kind": "ephemeral"},
    {"pattern": "cloud/lite-relay/.env", "kind": "secret"},
    {"pattern": "cloud/lite-relay/.dev.vars", "kind": "secret"},
    {"pattern": "cloud/lite-relay/.wrangler", "kind": "secret"},
    {"pattern": "cloud/lite-relay/node_modules", "kind": "ephemeral"},
    {"pattern": "cloud/lite-relay/wrangler*.jsonc", "kind": "secret"},
    {"pattern": "*.lock", "kind": "ephemeral"},
    {"pattern": "**/*.lock", "kind": "ephemeral"},
    {"pattern": "*.tmp", "kind": "ephemeral"},
    {"pattern": "**/*.tmp", "kind": "ephemeral"},
    {"pattern": "*.bak", "kind": "preserve"},
    {"pattern": "**/*.bak", "kind": "preserve"},
)

# 配布物に存在してよいが、更新時は現行ユーザー側を正本とする初回seed。
SEED_PATH_RULES: tuple[str, ...] = (
    "config.json",
    "characters/_shared/memory/procedures",
)

# glob型の永続規則に一致しても、通常の配布所有物として認める明示例外。
RELEASE_OWNED_EXCEPTIONS: tuple[str, ...] = (
    "uv.lock",
    "cloud/lite-relay/wrangler.phase1.example.jsonc",
    "cloud/lite-relay/wrangler.phase2.example.jsonc",
)

# Python／Wranglerが起動時にapp treeへ生成し得るが、署名対象にも更新時の
# 引き継ぎ対象にもしてはならない限定状態。永続path catalogへ含めると旧更新hostが
# 新catalog digestを拒否するため、installed treeの再検証だけで別扱いにする。
_WRANGLER_ACCOUNT_CACHE_PATHS = {
    ".wrangler/cache/wrangler-account.json",
    "cloud/lite-relay/.wrangler/cache/wrangler-account.json",
}

DEFAULT_REQUIRED_ENTRIES: tuple[str, ...] = (
    "nexus_ark.py",
    "version.json",
    "pyproject.toml",
    "uv.lock",
)

_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_COMPONENT_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+\-]{0,127}$")
_SAFE_RUNTIME_TARGET = re.compile(r"^LiteRuntime-[0-9A-Za-z._+\-]{1,188}\.tar\.gz$")
_RUNTIME_BINDING_KEYS = {
    "present",
    "id",
    "version",
    "target_name",
    "manifest_digest",
    "artifact_digest",
    "node_version",
    "wrangler_version",
}


def _normalize_relative_path(value: str | os.PathLike[str]) -> str:
    raw = str(value).replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe relative path: {value}")
    normalized = "/".join(part for part in path.parts if part not in {"", "."})
    if not normalized:
        raise ValueError(f"empty relative path: {value}")
    return normalized


def _windows_path_key(value: str) -> str:
    """Windowsで同一扱いになるpathを検出し、展開不能名を拒否する。"""
    path = _normalize_relative_path(value)
    normalized_parts: list[str] = []
    for part in PurePosixPath(path).parts:
        normalized = unicodedata.normalize("NFC", part)
        if normalized.endswith((" ", ".")) or re.search(r'[<>:"|?*]', normalized):
            raise ValueError(f"Windows-incompatible release path: {path}")
        basename = normalized.split(".", 1)[0].casefold()
        if basename in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"Windows-reserved release path: {path}")
        normalized_parts.append(normalized.casefold())
    return "/".join(normalized_parts)


def _is_same_or_child(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _matches_rule(path: str, pattern: str) -> bool:
    if "*" in pattern or "?" in pattern or "[" in pattern:
        return PurePosixPath(path).match(pattern)
    return _is_same_or_child(path, pattern)


def classify_release_path(relative_path: str) -> str:
    """配布ファイルを`owned`、`seed`、永続種別のいずれかへ分類する。"""
    path = _normalize_relative_path(relative_path)
    if any(_matches_rule(path, exception) for exception in RELEASE_OWNED_EXCEPTIONS):
        return "owned"
    if any(_matches_rule(path, seed) for seed in SEED_PATH_RULES):
        return "seed"
    for rule in PERSISTENT_PATH_RULES:
        if _matches_rule(path, rule["pattern"]):
            return rule["kind"]
    return "owned"


def is_generated_release_state(relative_path: str) -> bool:
    """署名対象外の安全な起動時生成fileだけを限定判定する。"""

    path = _normalize_relative_path(relative_path)
    return path in _WRANGLER_ACCOUNT_CACHE_PATHS


def is_python_bytecode_cache(relative_path: str) -> bool:
    """app treeから除去できるPython bytecode cacheだけを判定する。"""

    path = _normalize_relative_path(relative_path)
    parts = PurePosixPath(path).parts
    return "__pycache__" in parts[:-1] and parts[-1].casefold().endswith(
        (".pyc", ".pyo")
    )


def persistent_catalog_payload() -> dict[str, Any]:
    return {
        "schema_version": PERSISTENT_CATALOG_SCHEMA_VERSION,
        "path_rules": sorted(PERSISTENT_PATH_RULES, key=lambda item: (item["pattern"], item["kind"])),
        "seed_path_rules": sorted(SEED_PATH_RULES),
        "release_owned_exceptions": sorted(RELEASE_OWNED_EXCEPTIONS),
    }


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def persistent_catalog_digest() -> str:
    return hashlib.sha256(_canonical_json_bytes(persistent_catalog_payload())).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_project_contract(app_dir: Path) -> dict[str, Any]:
    version_data = json.loads((app_dir / "version.json").read_text(encoding="utf-8"))
    pyproject_data = tomllib.loads((app_dir / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject_data.get("project", {})
    requires_python = str(project.get("requires-python") or "").strip()
    if not requires_python:
        raise ValueError("pyproject.toml project.requires-python is required")
    release_version = str(version_data.get("version") or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+-]*", release_version):
        raise ValueError("version.json version is invalid")
    return {
        "release_version": release_version,
        "requires_python": requires_python,
        "declared_min_python_version": str(version_data.get("min_python_version") or "").strip(),
    }


def _iter_regular_files(app_dir: Path) -> Iterable[tuple[str, Path]]:
    for path in sorted(app_dir.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValueError(f"release tree must not contain symlinks: {path.relative_to(app_dir)}")
        if not path.is_file():
            continue
        relative = _normalize_relative_path(path.relative_to(app_dir).as_posix())
        if relative == RELEASE_MANIFEST_FILENAME:
            continue
        yield relative, path


def build_release_manifest(
    app_dir: str | os.PathLike[str],
    *,
    required_entries: Iterable[str] = DEFAULT_REQUIRED_ENTRIES,
    target_platform: str,
    target_cpu: str,
    runtime_component: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(app_dir)
    project = _load_project_contract(root)
    required = sorted({_normalize_relative_path(path) for path in required_entries})
    entries: list[dict[str, Any]] = []
    for relative, path in _iter_regular_files(root):
        if is_generated_release_state(relative) or is_python_bytecode_cache(relative):
            raise ValueError(f"release file overlaps generated state path: {relative}")
        kind = classify_release_path(relative)
        if kind not in {"owned", "seed"}:
            raise ValueError(f"release file overlaps {kind} path: {relative}")
        entries.append(
            {
                "kind": kind,
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    windows_paths: dict[str, str] = {}
    for entry in entries:
        key = _windows_path_key(entry["path"])
        previous = windows_paths.get(key)
        if previous is not None:
            raise ValueError(
                f"Windows path collision in release: {previous} / {entry['path']}"
            )
        windows_paths[key] = entry["path"]
    paths = {entry["path"] for entry in entries}
    missing = sorted(set(required) - paths)
    if missing:
        raise ValueError(f"missing required release entries: {', '.join(missing)}")
    pyproject_path = root / "pyproject.toml"
    lock_path = root / "uv.lock"
    manifest = {
        "schema_version": RELEASE_MANIFEST_SCHEMA_VERSION,
        "release_version": project["release_version"],
        "target": {
            "platform": str(target_platform).strip().lower(),
            "cpu": str(target_cpu).strip().lower(),
        },
        "required_entries": required,
        "entries": entries,
        "file_count": len(entries),
        "total_bytes": sum(entry["size"] for entry in entries),
        "components": {
            "app": {"version": project["release_version"]},
            "python_env": {
                "present": True,
                "requires_python": project["requires_python"],
                "declared_min_python_version": project["declared_min_python_version"],
                "pyproject_sha256": _sha256_file(pyproject_path),
                "lock_sha256": _sha256_file(lock_path),
            },
            "runtime": _validate_runtime_binding(
                {"present": False} if runtime_component is None else runtime_component
            ),
        },
        "persistent_path_catalog": {
            "schema_version": PERSISTENT_CATALOG_SCHEMA_VERSION,
            "digest": persistent_catalog_digest(),
        },
    }
    if not manifest["target"]["platform"] or not manifest["target"]["cpu"]:
        raise ValueError("target platform and cpu are required")
    return manifest


def write_release_manifest(app_dir: str | os.PathLike[str], manifest: Mapping[str, Any]) -> Path:
    root = Path(app_dir)
    destination = root / RELEASE_MANIFEST_FILENAME
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temp = destination.with_name(destination.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, destination)
    return destination


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()


def _validate_runtime_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("release runtime component must be an object")
    present = value.get("present")
    if present is False:
        if set(value) != {"present"}:
            raise ValueError("absent runtime component has unexpected fields")
        return {"present": False}
    if present is not True or set(value) != _RUNTIME_BINDING_KEYS:
        raise ValueError("present runtime component binding is incomplete")
    component_id = str(value.get("id") or "")
    component_version = str(value.get("version") or "")
    target_name = str(value.get("target_name") or "")
    node_version = str(value.get("node_version") or "")
    wrangler_version = str(value.get("wrangler_version") or "")
    if component_id != "lite-wrangler":
        raise ValueError("release runtime component id mismatch")
    if not _SAFE_COMPONENT_VERSION.fullmatch(component_version):
        raise ValueError("release runtime component version is invalid")
    if not _SAFE_RUNTIME_TARGET.fullmatch(target_name):
        raise ValueError("release runtime target name is invalid")
    if not _SAFE_COMPONENT_VERSION.fullmatch(node_version):
        raise ValueError("release runtime Node version is invalid")
    if not _SAFE_COMPONENT_VERSION.fullmatch(wrangler_version):
        raise ValueError("release runtime Wrangler version is invalid")
    for digest_key in ("manifest_digest", "artifact_digest"):
        if not _SHA256.fullmatch(str(value.get(digest_key) or "")):
            raise ValueError(f"release runtime {digest_key} is invalid")
    return {key: value[key] for key in sorted(_RUNTIME_BINDING_KEYS)}


def validate_runtime_binding(value: Any) -> dict[str, Any]:
    """アプリmanifest用runtime bindingを正規化して検証する。"""

    return _validate_runtime_binding(value)


def _validate_manifest_shape(
    manifest: Mapping[str, Any],
    *,
    expected_platform: str | None = None,
    expected_cpu: str | None = None,
) -> dict[str, dict[str, Any]]:
    if manifest.get("schema_version") != RELEASE_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported release manifest schema")
    target = manifest.get("target")
    if not isinstance(target, Mapping):
        raise ValueError("release manifest target is required")
    platform_value = str(target.get("platform") or "").lower()
    cpu_value = str(target.get("cpu") or "").lower()
    if expected_platform and platform_value != expected_platform.lower():
        raise ValueError(f"release target platform mismatch: {platform_value}")
    if expected_cpu and cpu_value != expected_cpu.lower():
        raise ValueError(f"release target cpu mismatch: {cpu_value}")
    components = manifest.get("components")
    if not isinstance(components, Mapping):
        raise ValueError("release manifest components are required")
    app_component = components.get("app")
    if not isinstance(app_component, Mapping) or str(app_component.get("version") or "") != str(
        manifest.get("release_version") or ""
    ):
        raise ValueError("release app component version mismatch")
    _validate_runtime_binding(components.get("runtime"))
    catalog = manifest.get("persistent_path_catalog")
    if not isinstance(catalog, Mapping) or catalog.get("digest") != persistent_catalog_digest():
        raise ValueError("persistent path catalog digest mismatch")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("release manifest entries must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    windows_paths: dict[str, str] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            raise ValueError("release manifest entry must be an object")
        path = _normalize_relative_path(str(raw_entry.get("path") or ""))
        if path in indexed:
            raise ValueError(f"duplicate release manifest path: {path}")
        windows_key = _windows_path_key(path)
        if windows_key in windows_paths:
            raise ValueError(
                f"Windows path collision in manifest: {windows_paths[windows_key]} / {path}"
            )
        windows_paths[windows_key] = path
        kind = raw_entry.get("kind")
        if kind not in {"owned", "seed"} or classify_release_path(path) != kind:
            raise ValueError(f"release ownership mismatch: {path}")
        size = raw_entry.get("size")
        sha256 = str(raw_entry.get("sha256") or "")
        if not isinstance(size, int) or size < 0 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError(f"invalid release entry metadata: {path}")
        indexed[path] = dict(raw_entry)
    if manifest.get("file_count") != len(indexed):
        raise ValueError("release manifest file count mismatch")
    if manifest.get("total_bytes") != sum(entry["size"] for entry in indexed.values()):
        raise ValueError("release manifest total size mismatch")
    required = {_normalize_relative_path(path) for path in manifest.get("required_entries", [])}
    if not required or not required <= indexed.keys():
        raise ValueError("release manifest required entries mismatch")
    return indexed


def validate_release_tree(
    app_dir: str | os.PathLike[str],
    *,
    expected_platform: str | None = None,
    expected_cpu: str | None = None,
    allow_persistent_state: bool = False,
    allow_unlisted_legacy_overlay: bool = False,
) -> dict[str, Any]:
    root = Path(app_dir)
    manifest_path = root / RELEASE_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    indexed = _validate_manifest_shape(
        manifest,
        expected_platform=expected_platform,
        expected_cpu=expected_cpu,
    )
    actual_paths: set[str] = set()
    for relative, path in _iter_regular_files(root):
        actual_paths.add(relative)
        entry = indexed.get(relative)
        if entry is None:
            if allow_persistent_state:
                if is_generated_release_state(relative):
                    continue
                # 共有手順は通常の利用で増えるMarkdownデータ。初回seedの
                # 未掲載追加も利用環境だけで許可し、配布物や実行コードは緩めない。
                procedure = PurePosixPath(relative)
                if (procedure.parent.as_posix() == "characters/_shared/memory/procedures"
                        and procedure.suffix.lower() == ".md"):
                    continue
                if classify_release_path(relative) in {
                    "preserve",
                    "secret",
                    "ephemeral",
                }:
                    continue
            # 旧overlay updaterが残した配布所有fileは、legacy launcher移行時だけ
            # 無視できる。manifest掲載fileの欠落・hash・size検証は一切緩めない。
            if allow_unlisted_legacy_overlay:
                continue
            raise ValueError(f"unlisted release file: {relative}")
        if allow_persistent_state and entry["kind"] == "seed":
            continue
        if path.stat().st_size != entry["size"] or _sha256_file(path) != entry["sha256"]:
            raise ValueError(f"release file hash or size mismatch: {relative}")
    missing = set(indexed) - actual_paths
    if missing:
        raise ValueError(f"missing release files: {', '.join(sorted(missing))}")
    return {
        "manifest": manifest,
        "manifest_digest": manifest_digest(manifest),
        "file_count": len(indexed),
        "total_bytes": manifest["total_bytes"],
    }


def validate_release_inventory(
    manifest: Mapping[str, Any],
    inventory: Mapping[str, Mapping[str, Any]],
    *,
    expected_platform: str | None = None,
    expected_cpu: str | None = None,
) -> dict[str, Any]:
    """展開前archive等のpath/size/hash一覧をmanifestと完全照合する。"""
    indexed = _validate_manifest_shape(
        manifest,
        expected_platform=expected_platform,
        expected_cpu=expected_cpu,
    )
    normalized_inventory: dict[str, Mapping[str, Any]] = {}
    windows_paths: dict[str, str] = {}
    for raw_path, metadata in inventory.items():
        path = _normalize_relative_path(raw_path)
        if path == RELEASE_MANIFEST_FILENAME:
            continue
        if path in normalized_inventory:
            raise ValueError(f"duplicate release inventory path: {path}")
        windows_key = _windows_path_key(path)
        if windows_key in windows_paths:
            raise ValueError(
                f"Windows path collision in inventory: {windows_paths[windows_key]} / {path}"
            )
        windows_paths[windows_key] = path
        normalized_inventory[path] = metadata
    if set(indexed) != set(normalized_inventory):
        missing = sorted(set(indexed) - set(normalized_inventory))
        extra = sorted(set(normalized_inventory) - set(indexed))
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise ValueError("release inventory path mismatch: " + " ".join(details))
    for path, entry in indexed.items():
        actual = normalized_inventory[path]
        if actual.get("size") != entry["size"] or actual.get("sha256") != entry["sha256"]:
            raise ValueError(f"release inventory hash or size mismatch: {path}")
    return {
        "manifest_digest": manifest_digest(manifest),
        "file_count": len(indexed),
        "total_bytes": manifest["total_bytes"],
    }
