"""Lite Windows専用runtimeの検証済み絶対path resolver。

利用者環境のPATH、system Node、npm、registryへfallbackしない。runtime artifactの
全ファイル検証と実行version確認が両方通った場合だけ、呼び出し側へentry pointを返す。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from update_host.runtime import validate_runtime_tree


MINIMUM_NODE_MAJOR = 22
EXPECTED_WRANGLER_VERSION = "4.149.0"
_VERSION_OUTPUT = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
_APP_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$")


class LiteRuntimeError(RuntimeError):
    """専用runtimeを安全に利用できない時の固定failure。"""

    def __init__(self, message: str, *, failure_code: str):
        super().__init__(message)
        self.failure_code = failure_code


@dataclass(frozen=True)
class LiteRuntimePaths:
    root: Path
    node: Path
    wrangler: Path
    wrangler_cli: Path
    node_version: str
    wrangler_version: str
    component_version: str
    manifest_digest: str
    artifact_digest: str


def _parse_version(output: Any) -> str | None:
    match = _VERSION_OUTPUT.search(str(output or ""))
    return ".".join(match.groups()) if match else None


def _run_version(
    command: Sequence[str | Path],
    *,
    runner: Callable[..., Any],
    cwd: Path,
) -> str | None:
    try:
        result = runner(
            [str(value) for value in command],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            cwd=str(cwd),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if int(getattr(result, "returncode", 1)) != 0:
        return None
    return _parse_version(getattr(result, "stdout", ""))


def _prepare_runtime_state_root(runtime_root: Path) -> Path:
    """Wranglerのlocal cacheを署名済みapp／runtime外へ固定する。"""

    install_root = runtime_root.parent
    cache_root = install_root / "cache"
    state_root = cache_root / "lite_runtime"
    try:
        if cache_root.is_symlink() or state_root.is_symlink():
            raise OSError("runtime state path must not be a symlink")
        state_root.mkdir(parents=True, exist_ok=True)
        if not state_root.is_dir():
            raise OSError("runtime state path must be a directory")
    except OSError as exc:
        raise LiteRuntimeError(
            "Lite専用runtimeの一時状態を準備できません。",
            failure_code="runtime_state_unavailable",
        ) from exc
    return state_root.absolute()


def _app_version_tuple(value: str) -> tuple[int, int, int, int]:
    match = _APP_VERSION.fullmatch(value)
    if not match:
        raise LiteRuntimeError(
            "アプリとruntimeの互換versionを確認できません。",
            failure_code="runtime_app_version_invalid",
        )
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def _validate_app_compatibility(manifest: Mapping[str, Any], app_version: str) -> None:
    compatibility = manifest.get("app_compatibility")
    if not isinstance(compatibility, Mapping):
        raise LiteRuntimeError(
            "runtimeのアプリ互換情報がありません。",
            failure_code="runtime_manifest_invalid",
        )
    current = _app_version_tuple(app_version)
    minimum = _app_version_tuple(str(compatibility.get("min_release") or ""))
    maximum = _app_version_tuple(str(compatibility.get("max_release") or ""))
    if minimum > maximum or not minimum <= current <= maximum:
        raise LiteRuntimeError(
            "このruntimeは現在のNexus Arkと互換性がありません。",
            failure_code="runtime_app_incompatible",
        )


def resolve_lite_runtime(
    runtime_root: str | Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    app_version: str | None = None,
) -> LiteRuntimePaths:
    """完全検証済みの専用Node／Wrangler絶対pathを返す。"""

    root = Path(runtime_root).absolute()
    try:
        validated = validate_runtime_tree(root)
    except (OSError, ValueError) as exc:
        raise LiteRuntimeError(
            "Lite専用runtimeの署名済み内容を確認できません。",
            failure_code="runtime_manifest_invalid",
        ) from exc
    manifest = validated["manifest"]
    if app_version is not None:
        _validate_app_compatibility(manifest, app_version)

    entrypoints = manifest["entrypoints"]
    node = (root / entrypoints["node"]).absolute()
    wrangler = (root / entrypoints["wrangler"]).absolute()
    wrangler_cli = (root / entrypoints["wrangler_cli"]).absolute()
    for path in (node, wrangler, wrangler_cli):
        if not path.is_file() or not path.is_relative_to(root):
            raise LiteRuntimeError(
                "Lite専用runtimeのentry pointが不正です。",
                failure_code="runtime_entrypoint_invalid",
            )

    state_root = _prepare_runtime_state_root(root)

    node_version = _run_version([node, "--version"], runner=runner, cwd=state_root)
    if node_version != str(manifest["node_version"]):
        raise LiteRuntimeError(
            "Lite専用Nodeのversionがmanifestと一致しません。",
            failure_code="runtime_node_version_mismatch",
        )
    if int(node_version.split(".", 1)[0]) < MINIMUM_NODE_MAJOR:
        raise LiteRuntimeError(
            "Lite専用Nodeはversion 22以上が必要です。",
            failure_code="runtime_node_22_required",
        )
    wrangler_version = _run_version(
        [node, wrangler, "--version"], runner=runner, cwd=state_root
    )
    if (
        wrangler_version != str(manifest["wrangler_version"])
        or wrangler_version != EXPECTED_WRANGLER_VERSION
    ):
        raise LiteRuntimeError(
            "Lite専用Wranglerの固定versionが一致しません。",
            failure_code="runtime_wrangler_version_mismatch",
        )
    return LiteRuntimePaths(
        root=root,
        node=node,
        wrangler=wrangler,
        wrangler_cli=wrangler_cli,
        node_version=node_version,
        wrangler_version=wrangler_version,
        component_version=str(manifest["component"]["version"]),
        manifest_digest=str(validated["manifest_digest"]),
        artifact_digest=str(validated["artifact_digest"]),
    )


__all__ = [
    "EXPECTED_WRANGLER_VERSION",
    "MINIMUM_NODE_MAJOR",
    "LiteRuntimeError",
    "LiteRuntimePaths",
    "resolve_lite_runtime",
]
