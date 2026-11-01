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


def require_selected_persona_room(room_name, selected_room, is_switching):
    """選択表示と内部ルームの不一致・切替中の書き込みを止める。"""
    import gradio as gr

    if is_switching:
        raise gr.Error("ルーム切替中のため保存しませんでした。切替完了を待ってください。")
    if not room_name or room_name != selected_room:
        raise gr.Error("選択中のルームと保存先が一致しないため、保存しませんでした。対象ルームを選び直してください。")


def require_persona_editor_target(room_name, selected_room, editor_room, is_switching):
    """本文の読込元も一致する場合だけ編集内容を保存する。"""
    import gradio as gr

    require_selected_persona_room(room_name, selected_room, is_switching)
    if room_name != editor_room:
        raise gr.Error(
            "編集内容の読込元と選択中のルームを確認できないため、保存しませんでした。"
            "未保存の文章を手元に控え、対象ルームを選択して"
            "「🔄 最新の状態に更新」で読み直してから編集してください。"
        )


def with_persona_editor_room(update, room_name):
    """本文を更新した結果にだけ読込元を付け、失敗時は双方を保持する。"""
    import gradio as gr

    if isinstance(update, dict) and "value" not in update:
        return update, gr.skip()
    return update, room_name
