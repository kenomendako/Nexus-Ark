"""Playwright driverが使うNodeを、検証済み修正版だけに制限する。

Windows配布版はapp bindingに結合された専用runtimeを使う。wheelに残る旧Nodeや
利用者のPATH、任意のPLAYWRIGHT_NODEJS_PATHへfallbackしない。開発checkoutでは
明示した開発用Node、その他では固定依存の内蔵Nodeを版検証して使う。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# https://nodejs.org/en/blog/vulnerability/october-2026-security-releases
FIXED_NODE_VERSIONS = {22: (22, 23, 4), 24: (24, 22, 1), 26: (26, 12, 1)}
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)\s*\Z")


class PlaywrightNodeError(RuntimeError):
    """未検証Nodeでブラウザを起動しないための利用可能性エラー。"""


def is_fixed_node_version(value: str) -> bool:
    match = _VERSION.fullmatch(value.strip())
    if match is None:
        return False
    version = tuple(map(int, match.groups()))
    minimum = FIXED_NODE_VERSIONS.get(version[0])
    return minimum is not None and version >= minimum


def _select_node(app_root: Path, platform: str) -> tuple[Path, str | None]:
    manifest_path = app_root / "release_manifest.json"
    if manifest_path.is_file() and platform == "win32":
        from update_host.runtime import validate_bound_runtime

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            exact = validate_bound_runtime(manifest, app_root.parent / "runtime")
            runtime = exact["manifest"]
            return app_root.parent / "runtime" / runtime["entrypoints"]["node"], runtime["node_version"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PlaywrightNodeError(
                "ブラウザ機能に必要な準備ツールを確認できません。自動更新とLiteの準備ツールの状態を確認してください。"
            ) from exc
    explicit = os.environ.get("NEXUS_ARK_LITE_DEV_NODE", "").strip()
    if not manifest_path.exists() and (app_root / ".git").exists() and explicit:
        node = Path(explicit)
        if not node.is_absolute():
            raise PlaywrightNodeError("開発用Nodeには絶対pathが必要です。")
        return node.resolve(), None
    if platform == "win32" or (app_root / ".git").exists():
        raise PlaywrightNodeError("検証済みのブラウザ用実行環境がありません。配布版を更新するか、開発用Nodeを明示してください。")
    # 環境変数overrideを参照せず、固定wheelに同梱されたbinaryそのものを検証する。
    import playwright
    return Path(playwright.__file__).parent / "driver" / ("node.exe" if platform == "win32" else "node"), None


def prepare_playwright_node(*, app_root: Path | None = None) -> str:
    """hash/bindingと実versionの検証後、公式overrideを設定して絶対pathを返す。"""
    root = Path(app_root) if app_root is not None else Path(__file__).resolve().parent
    node, expected = _select_node(root, sys.platform)
    if not node.is_absolute() or not node.is_file() or node.is_symlink():
        raise PlaywrightNodeError("ブラウザ機能用のNodeを確認できません。")
    try:
        result = subprocess.run([str(node), "--version"], capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PlaywrightNodeError("ブラウザ機能用のNodeの版を確認できません。") from exc
    version = result.stdout.strip()
    if not is_fixed_node_version(version) or (expected is not None and version.removeprefix("v") != expected):
        raise PlaywrightNodeError("ブラウザ機能用のNodeがセキュリティ更新前です。Nexus Arkを更新してください。")
    os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(node)
    return str(node)
