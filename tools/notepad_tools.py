# tools/notepad_tools.py (v20: Final Architecture)

from langchain_core.tools import tool
import os
from room_manager import get_room_files_paths

@tool
def read_full_notepad(room_name: str) -> str:
    """
    短期記憶用のメモ帳の全内容を読み上げる。内容がなければその旨を伝える。
    主に、編集以外の目的で内容を確認したい場合に使用します。
    """
    _, _, _, _, notepad_path = get_room_files_paths(room_name)
    if not notepad_path or not os.path.exists(notepad_path):
        return "【メモ帳ファイルが見つかりません】"
    with open(notepad_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()
        return content if content else "【メモ帳は空です】"

@tool
def plan_notepad_edit(modification_request: str, room_name: str) -> str:
    """
    【ステップ1：計画】短期記憶用のメモ帳の変更を計画します。
    このツールは、あなたがメモ帳に対してどのような変更を行いたいかの「意図」をシステムに伝えるために、最初に呼び出します。
    """
    return f"システムへのメモ帳編集計画を受け付けました。意図:「{modification_request}」"

def _write_notepad_file(full_content: str, room_name: str, modification_request: str) -> str:
    """【内部専用】AIが生成した整形済みの完全な文字列を受け取り、notepad.mdに書き込む。"""
    if not room_name:
        return "【エラー】ルーム名が必要です。"
    _, _, _, _, notepad_path = get_room_files_paths(room_name)
    if not notepad_path:
        return f"【エラー】ルーム'{room_name}'のメモ帳パスが見つかりません。"
    try:
        content_to_write = full_content or ""
        with open(notepad_path, 'w', encoding='utf-8') as f:
            f.write(content_to_write)
        return "成功: メモ帳を完全に更新しました。"
    except Exception as e:
        return f"【エラー】メモ帳の上書き中に予期せぬエラーが発生しました: {e}"
