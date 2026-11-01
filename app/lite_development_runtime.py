"""明示opt-inされた開発用Lite runtimeのローカル検証。

製品配布の署名済みWindows runtimeとは別の経路で、source checkout上の
WSL/Linux開発環境から既存のrelayを実行するための小さな検証層を提供する。
Node/npmの自動導入、registry通信、PATHへの暗黙fallbackは行わない。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DEVELOPMENT_NODE_ENV = "NEXUS_ARK_LITE_DEV_NODE"
MINIMUM_NODE_MAJOR = 22
DEFAULT_WRANGLER_VERSION = "4.149.0"
DEFAULT_REQUIRED_RELAY_FILES = (
    "package.json",
    "package-lock.json",
    "src/index.ts",
    "migrations/0001_phase0_core.sql",
    "migrations/0009_phase5_operations_standby.sql",
    "migrations/0010_return_high_water_guards.sql",
    "scripts/build-unified-lite.mjs",
    "scripts/wrangler-secret-stdin-preload.cjs",
    "wrangler.phase2.example.jsonc",
)
_VERSION_OUTPUT = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")


class LiteDevelopmentRuntimeError(RuntimeError):
    """明示された開発runtimeを安全に利用できない時の固定failure。"""

    def __init__(self, message: str, *, failure_code: str):
        super().__init__(message)
        self.failure_code = failure_code


@dataclass(frozen=True)
class LiteDevelopmentRuntimePaths:
    """検証済みsource checkout用Node／Wrangler entry point。"""

    workspace_root: Path
    node: Path
    wrangler: Path
    wrangler_cli: Path
    node_version: str
    wrangler_version: str

    @property
    def runtime_source(self) -> str:
        return "development"


def workspace_root_for_relay(relay: str | Path) -> Path:
    """配布app／source checkoutのどちらからでもworkspace rootを求める。"""

    resolved = Path(relay).absolute()
    if resolved.name == "lite-relay" and resolved.parent.name == "cloud":
        root = resolved.parent.parent
    else:
        root = resolved
    return root.parent if root.name == "app" else root


def _release_manifest_present(workspace_root: Path) -> bool:
    """配布appのmanifestがあれば開発envを無視する。"""

    return any(
        (workspace_root / relative).is_file()
        for relative in ("release_manifest.json", "app/release_manifest.json")
    )


def _read_version(
    command: Sequence[str | Path],
    *,
    runner: Callable[..., Any],
    cwd: Path,
    failure_code: str,
    label: str,
) -> str:
    try:
        result = runner(
            [str(value) for value in command],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            cwd=str(cwd),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiteDevelopmentRuntimeError(
            f"開発用{label}のversionを確認できません。",
            failure_code=failure_code,
        ) from exc
    if int(getattr(result, "returncode", 1)) != 0:
        raise LiteDevelopmentRuntimeError(
            f"開発用{label}のversion確認に失敗しました。",
            failure_code=failure_code,
        )
    match = _VERSION_OUTPUT.search(str(getattr(result, "stdout", "") or ""))
    if not match:
        raise LiteDevelopmentRuntimeError(
            f"開発用{label}のversionを解釈できません。",
            failure_code=failure_code,
        )
    return ".".join(match.groups())


def _lock_versions(relay: Path) -> tuple[str, str, str]:
    try:
        package = json.loads((relay / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((relay / "package-lock.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise LiteDevelopmentRuntimeError(
            "開発用Liteのpackage lockを確認できません。",
            failure_code="development_runtime_lock_mismatch",
        ) from exc
    package = package if isinstance(package, Mapping) else {}
    package_dependencies = package.get("devDependencies")
    package_dependencies = (
        package_dependencies if isinstance(package_dependencies, Mapping) else {}
    )
    package_version = str(package_dependencies.get("wrangler") or "")
    lock_root = lock.get("packages") if isinstance(lock, Mapping) else None
    lock_root = lock_root if isinstance(lock_root, Mapping) else {}
    lock_package = lock_root.get("") if isinstance(lock_root.get(""), Mapping) else {}
    lock_node = (
        lock_root.get("node_modules/wrangler")
        if isinstance(lock_root.get("node_modules/wrangler"), Mapping)
        else {}
    )
    lock_version = str((lock_node or {}).get("version") or "")
    lock_dependencies = (lock_package or {}).get("devDependencies")
    lock_dependencies = (
        lock_dependencies if isinstance(lock_dependencies, Mapping) else {}
    )
    lock_root_version = str(lock_dependencies.get("wrangler") or "")
    return package_version, lock_root_version, lock_version


def resolve_lite_development_runtime(
    relay: str | Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    expected_wrangler_version: str = DEFAULT_WRANGLER_VERSION,
    required_relay_files: Sequence[str] = DEFAULT_REQUIRED_RELAY_FILES,
) -> LiteDevelopmentRuntimePaths | None:
    """明示された開発Nodeを検証し、source checkoutでだけ返す。

    環境変数が未指定なら ``None`` を返す。配布manifestがある場合も、
    開発envを無視して呼び出し側の署名済みruntime経路を維持する。
    それ以外の不備は、暗黙fallbackを避けるため専用failureで停止する。
    """

    environment = os.environ if environ is None else environ
    raw_node = str(environment.get(DEVELOPMENT_NODE_ENV) or "").strip()
    if not raw_node:
        return None

    relay_path = Path(relay).absolute()
    workspace_root = workspace_root_for_relay(relay_path)
    if _release_manifest_present(workspace_root):
        return None
    if not (workspace_root / ".git").exists():
        raise LiteDevelopmentRuntimeError(
            "開発用Lite runtimeはGit管理下のsource checkoutでだけ利用できます。",
            failure_code="development_runtime_source_required",
        )

    node = Path(raw_node).expanduser()
    if not node.is_absolute():
        raise LiteDevelopmentRuntimeError(
            "開発用Nodeは絶対pathで指定してください。",
            failure_code="development_runtime_path_not_absolute",
        )
    node = node.resolve()
    if not node.is_file():
        raise LiteDevelopmentRuntimeError(
            "指定された開発用Nodeを確認できません。",
            failure_code="development_runtime_node_missing",
        )

    missing = [
        relative
        for relative in required_relay_files
        if not (relay_path / relative).is_file()
    ]
    if missing:
        raise LiteDevelopmentRuntimeError(
            "Lite relayの開発用資源が不足しています。",
            failure_code="development_runtime_relay_resources_missing",
        )

    package_version, lock_root_version, lock_version = _lock_versions(relay_path)
    if any(
        version != expected_wrangler_version
        for version in (package_version, lock_root_version, lock_version)
    ):
        raise LiteDevelopmentRuntimeError(
            "開発用Wranglerのlock versionが固定値と一致しません。",
            failure_code="development_runtime_lock_mismatch",
        )

    wrangler = relay_path / "node_modules" / "wrangler" / "bin" / "wrangler.js"
    wrangler_cli = relay_path / "node_modules" / "wrangler" / "wrangler-dist" / "cli.js"
    if not wrangler.is_file() or not wrangler_cli.is_file():
        raise LiteDevelopmentRuntimeError(
            "開発用Wranglerのentry pointが不足しています。",
            failure_code="development_runtime_entrypoint_missing",
        )

    node_version = _read_version(
        [node, "--version"],
        runner=runner,
        cwd=relay_path,
        failure_code="development_runtime_node_version_unavailable",
        label="Node",
    )
    try:
        node_major = int(node_version.split(".", 1)[0])
    except (TypeError, ValueError) as exc:
        raise LiteDevelopmentRuntimeError(
            "開発用Nodeのmajor versionを解釈できません。",
            failure_code="development_runtime_node_version_unavailable",
        ) from exc
    if node_major < MINIMUM_NODE_MAJOR:
        raise LiteDevelopmentRuntimeError(
            "開発用Nodeはversion 22以上が必要です。",
            failure_code="development_runtime_node_22_required",
        )

    wrangler_version = _read_version(
        [node, wrangler, "--version"],
        runner=runner,
        cwd=relay_path,
        failure_code="development_runtime_wrangler_version_unavailable",
        label="Wrangler",
    )
    if wrangler_version != expected_wrangler_version:
        raise LiteDevelopmentRuntimeError(
            "開発用Wranglerの実versionが固定値と一致しません。",
            failure_code="development_runtime_wrangler_version_mismatch",
        )

    return LiteDevelopmentRuntimePaths(
        workspace_root=workspace_root,
        node=node,
        wrangler=wrangler.absolute(),
        wrangler_cli=wrangler_cli.absolute(),
        node_version=node_version,
        wrangler_version=wrangler_version,
    )
