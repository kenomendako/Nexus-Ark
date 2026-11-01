"""Lite画面の版と正本資産の改訂を、秘密情報なしで照合する。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


UI_ASSET_NAMES = (
    "index.html",
    "static/app.js",
    "static/styles.css",
    "static/pairing-handoff.js",
    "static/lite-continuity-state.js",
    "static/travel-adapter.js",
    "static/lite-update-state.js",
    "service-worker.js",
    "manifest.webmanifest",
    "badge.png",
)
MAX_METADATA_BYTES = 4096


def local_ui_metadata(source_root: Path | None = None) -> dict[str, str | int]:
    """正本だけの固定allowlistを読み、公開用の小さな版情報を返す。"""
    root = source_root or Path(__file__).resolve().parent / "mobile_app"
    rows = []
    for name in sorted(UI_ASSET_NAMES):
        path = root / name
        if any(part.is_symlink() for part in (path, *path.parents[:len(Path(name).parts)])) or not path.is_file():
            raise ValueError("Lite画面の資産を確認できません。")
        rows.append(name + "\0" + hashlib.sha256(path.read_bytes()).hexdigest() + "\n")
    app = (root / "static/app.js").read_text(encoding="utf-8")
    match = re.search(r'const LITE_UI_BUILD = "(v\d{1,8})";', app)
    if match is None:
        raise ValueError("Lite画面の版を確認できません。")
    digest = hashlib.sha256("".join(rows).encode("utf-8")).hexdigest()
    return {"format_version": 1, "ui_build": match[1], "asset_digest": digest}


def parse_ui_metadata(value: object) -> dict[str, str | int]:
    """外部の版情報を、用途が固定された3項目へ絞る。"""
    if (
        not isinstance(value, dict)
        or set(value) != {"format_version", "ui_build", "asset_digest"}
        or type(value.get("format_version")) is not int
        or value.get("format_version") != 1
    ):
        raise ValueError("Lite画面の版情報の形式を確認できません。")
    build = value.get("ui_build")
    digest = value.get("asset_digest")
    if not isinstance(build, str) or re.fullmatch(r"v\d{1,8}", build) is None:
        raise ValueError("Lite画面の版を確認できません。")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("Lite画面の改訂を確認できません。")
    return {"format_version": 1, "ui_build": build, "asset_digest": digest}


def read_ui_metadata_json(data: bytes) -> dict[str, str | int]:
    if len(data) > MAX_METADATA_BYTES:
        raise ValueError("Lite画面の版情報が大きすぎます。")
    return parse_ui_metadata(json.loads(data))
