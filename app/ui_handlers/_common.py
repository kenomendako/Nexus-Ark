"""ui_handlers 各ドメインで共有する汎用ヘルパー。

サブモジュール分割時に複数ドメインから参照される小さなユーティリティを集約し、
循環インポートを避ける土台とする。ui_handlers パッケージから再エクスポートされる。
"""

import os
import datetime
from typing import Any, List


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _normalize_file_paths(file_values) -> List[str]:
    """gr.File の値を承認処理で使えるファイルパス配列へ正規化する。"""
    if not file_values:
        return []

    if isinstance(file_values, (str, os.PathLike)):
        file_values = [file_values]

    paths = []
    for item in file_values:
        path = None
        if isinstance(item, (str, os.PathLike)):
            path = os.fspath(item)
        elif isinstance(item, dict):
            path = item.get("path") or item.get("name") or item.get("orig_name")
        elif hasattr(item, "name"):
            path = item.name

        if path:
            paths.append(os.fspath(path))

    return paths


def _settings_status_message(scope: str, label: str, result: Any, restart_required: bool = False) -> str:
    """設定保存状態を、トーストではなくUI上に出す短い文言へ整形する。"""
    now = datetime.datetime.now().strftime("%H:%M:%S")
    suffix = "（再起動後に反映）" if restart_required else ""
    if result == "no_change":
        return f"{scope}: {label} は保存済みです {now}{suffix}"
    if result:
        return f"{scope}: {label} を保存しました {now}{suffix}"
    return f"{scope}: {label} の保存に失敗しました"
