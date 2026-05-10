# tools/creative_tools.py
# AIペルソナの創作活動専用ノートのためのツール

from langchain_core.tools import tool
import os
import constants
from typing import List, Dict, Any
import traceback
import datetime
import re


def _get_creative_notes_path(room_name: str) -> str:
    """創作ノートのパスを取得する"""
    return os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME, constants.CREATIVE_NOTES_FILENAME)


@tool
def read_creative_notes(room_name: str) -> str:
    """
    あなたの創作ノートの全内容を読み上げます。
    創作ノートは、詩、物語、アイデアスケッチ、音楽の歌詞など、あなたの創作活動のための専用スペースです。
    メモ帳（ユーザーとの共有）や秘密の日記（内心の記録）とは異なり、純粋な創作物を自由に書き留める場所です。
    """
    path = _get_creative_notes_path(room_name)
    if not os.path.exists(path):
        return ""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read().strip()
        return content


@tool
def plan_creative_notes_edit(modification_request: str, room_name: str) -> str:
    """
    創作ノートの変更を計画します。
    
    【口述筆記モデル】
    保存したい内容（詩、物語、アイデアなど）そのものを `modification_request` に記述してください。
    あなたの書いた文章がそのまま作品として記録されます。
    """
    return f"システムへの創作ノート編集計画を受け付けました。意図:「{modification_request}」"


def _apply_creative_notes_edits(instructions: List[Dict[str, Any]], room_name: str) -> str:
    """
    【追記専用モード】創作ノートに新しいエントリを追加する。
    
    行番号ベースの編集は廃止し、常にファイル末尾にタイムスタンプ付きセクションを追加する。
    これにより、AIが「どこに書くか」を迷う問題を解消し、安定した追記動作を保証する。
    """
    if not room_name:
        return "【エラー】ルーム名が指定されていません。"
    if not isinstance(instructions, list) or not instructions:
        return "【エラー】編集指示がリスト形式ではないか、空です。"

    # [2026-02-02] 書き込み前にアーカイブ判定
    import room_manager
    room_manager.archive_large_note(room_name, constants.CREATIVE_NOTES_FILENAME)
    
    path = _get_creative_notes_path(room_name)
    # アーカイブ後にパスが空になっている可能性（実際には新規作成される）を確認
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write("")

    try:
        # 追加するコンテンツを収集
        contents_to_add = []
        for inst in instructions:
            content = inst.get("content", "")
            if content and str(content).strip() and str(content).strip() != 'None':
                contents_to_add.append(str(content).strip())
        
        if not contents_to_add:
            if instructions:
                return "【警告】書き込み内容が実質的に空（空白のみ）であったため、エントリを追加しませんでした。"
            return "【エラー】有効な編集指示が見送られたか、内容が空です。創作ノートは更新されませんでした。"
        
        # 既存コンテンツを読み込み
        with open(path, 'r', encoding='utf-8') as f:
            existing_content = f.read()
        
        # タイムスタンプ付きセクションを作成
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        section_header = f"\n---\n📝 {timestamp}\n"
        new_section = section_header + "\n".join(contents_to_add)
        
        # 既存コンテンツがある場合は区切りを追加
        if existing_content.strip():
            updated_content = existing_content.rstrip() + "\n" + new_section
        else:
            # 空ファイルの場合はヘッダーなしで開始
            updated_content = new_section.lstrip("\n")
        
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated_content)

        return f"成功: 創作ノート(creative_notes.md)に新しいエントリを追加しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】創作ノートの編集中に予期せぬエラーが発生しました: {e}"
