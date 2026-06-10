# tools/research_tools.py (Phase 3: Contextual Analysis)

from langchain_core.tools import tool
import os
from room_manager import get_room_files_paths
import json
from typing import List, Dict, Any
import traceback
import datetime
import re

@tool
def read_research_notes(room_name: str) -> str:
    """
    研究・分析ノートの全内容を読み取る。
    Web閲覧ツール等で得た知識や、AIによる自律的な分析結果が蓄積されています。
    """
    _, _, _, _, _, _, research_notes_path = get_room_files_paths(room_name)
    if not research_notes_path or not os.path.exists(research_notes_path):
        return ""
    with open(research_notes_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()
        return content

from pydantic import BaseModel, Field

class PlanResearchNotesEditArgs(BaseModel):
    context_type: str = Field(..., description="過去の記録との関係性（'CONTINUE': 続き, 'DEEPEN': 深掘り, 'NEW': 新規, 'CONTRADICT': 反証）")
    intent_and_reasoning: str = Field(..., description="なぜこの分類を選んだのか、過去の記憶やノートのどの内容に基づいているかの説明。NEWの場合は、既存への追記(CONTINUE/DEEPEN)ではなく新規である理由を含めること。")
    modification_request: str = Field(..., description="保存したい内容そのもの。")
    room_name: str = Field(..., description="対象のルーム名")
    thread_id: str = Field("", description="関連するResearch Thread ID。context_typeがDEEPEN/CONTINUE/CONTRADICTの場合は、thread_id か target_heading のいずれかが必須。")
    target_heading: str = Field("", description="既存研究ノートの関連見出し。context_typeがDEEPEN/CONTINUE/CONTRADICTの場合は、thread_id か target_heading のいずれかが必須。")
    evidence_of_prior_read: str = Field("", description="既存ノートやスレッドを読んだ内容の要約や根拠。context_typeがDEEPEN/CONTINUE/CONTRADICTの場合は必須。")
    next_action: str = Field("", description="この追記後に次に行うべきこと。")

@tool(args_schema=PlanResearchNotesEditArgs)
def plan_research_notes_edit(
    context_type: str,
    intent_and_reasoning: str,
    modification_request: str,
    room_name: str,
    thread_id: str = "",
    target_heading: str = "",
    evidence_of_prior_read: str = "",
    next_action: str = ""
) -> str:
    """
    研究・分析ノートの変更を計画します。
    このツールを使用する際は、必ず過去の文脈との繋がりを明示しなければなりません。
    """
    target = f" thread_id={thread_id}" if thread_id else ""
    heading = f" target_heading={target_heading}" if target_heading else ""
    return f"【{context_type}】システムへの研究ノート編集計画を受け付けました。理由: {intent_and_reasoning}{target}{heading}"

def _apply_research_notes_edits(instructions: List[Dict[str, Any]], room_name: str) -> str:
    """
    【追記専用モード】研究ノートに新しいエントリを追加する。
    
    行番号ベースの編集は廃止し、常にファイル末尾にタイムスタンプ付きセクションを追加する。
    これにより、AIが「どこに書くか」を迷う問題を解消し、安定した追記動作を保証する。
    """
    if not room_name:
        return "【エラー】ルーム名が指定されていません。"
    if not isinstance(instructions, list) or not instructions:
        return "【エラー】編集指示がリスト形式ではないか、空です。"

    _, _, _, _, _, _, research_notes_path = get_room_files_paths(room_name)
    if not research_notes_path:
        return f"【エラー】ルーム'{room_name}'の研究ノートファイルパスが見つかりません。"
    
    # [2026-02-02] 書き込み前にアーカイブ判定
    import room_manager
    import constants
    room_manager.archive_large_note(room_name, constants.RESEARCH_NOTES_FILENAME)

    # アーカイブ後にパスが空になっている可能性（実際には新規作成される）を確認
    if not os.path.exists(research_notes_path):
        os.makedirs(os.path.dirname(research_notes_path), exist_ok=True)
        with open(research_notes_path, 'w', encoding='utf-8') as f:
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
                return "【警告】書き込み内容が実質的に空（空白のみ）であったため、研究ノートは更新されませんでした。"
            return "【エラー】有効な編集指示が見つからないか、内容が空です。研究ノートは更新されませんでした。"
        
        # 既存コンテンツを読み込み
        with open(research_notes_path, 'r', encoding='utf-8') as f:
            existing_content = f.read()
        
        # タイムスタンプ付きセクションを作成 (他のノートと形式を統一)
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        section_header = f"\n---\n📝 {timestamp}\n"
        new_section = section_header + "\n".join(contents_to_add)
        
        # 既存コンテンツがある場合は区切りを追加
        if existing_content.strip():
            updated_content = existing_content.rstrip() + "\n" + new_section
        else:
            # 空ファイルの場合はヘッダーなしで開始
            updated_content = new_section.lstrip("\n")
        
        with open(research_notes_path, "w", encoding="utf-8") as f:
            f.write(updated_content)

        return f"成功: 研究ノート(research_notes.md)に新しいエントリを追加しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】研究ノートの編集中に予期せぬエラーが発生しました: {e}"
