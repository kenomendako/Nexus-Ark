# tools/notepad_tools.py (v20: Final Architecture)

from langchain_core.tools import tool
import os
from room_manager import get_room_files_paths
import json
from typing import List, Dict, Any
import traceback
import datetime
import re

@tool
def read_full_notepad(room_name: str) -> str:
    """
    短期記憶用のメモ帳の全内容を読み上げる。内容がなければその旨を伝える。
    主に、編集以外の目的で内容を確認したい場合に使用します。
    """
    _, _, _, _, _, notepad_path, _ = get_room_files_paths(room_name)
    if not notepad_path or not os.path.exists(notepad_path):
        return "【メモ帳ファイルが見つかりません】"
    with open(notepad_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()
        return content if content else "【メモ帳は空です】"

@tool
def plan_notepad_edit(modification_request: str, room_name: str) -> str:
    """
    【口述筆記モデル】
    メモ帳(`notepad.md`)に保存したい内容（文章）そのものを `modification_request` に記述してください。
    あなたの書いた文章がそのままメモとして記録されます。
    """
    return f"システムへのメモ帳編集計画を受け付けました。意図:「{modification_request}」"

def _apply_notepad_edits(instructions: List[Dict[str, Any]], room_name: str) -> str:
    """【内部専用】AIが生成した行番号ベースの差分編集指示リストを解釈し、notepad.mdに適用する。"""
    if not room_name:
        return "【エラー】ルーム名が指定されていません。"
    if not isinstance(instructions, list):
        return "【エラー】編集指示がリスト形式ではありません。"

    _, _, _, _, _, notepad_path, _ = get_room_files_paths(room_name)
    if not notepad_path or not os.path.exists(notepad_path):
        return f"【エラー】ルーム'{room_name}'のメモ帳ファイルパスが見つかりません。"

    # [2026-02-02] 書き込み前にアーカイブ判定
    import room_manager
    import constants
    room_manager.archive_large_note(room_name, constants.NOTEPAD_FILENAME)

    try:
        with open(notepad_path, 'r', encoding='utf-8') as f:
            lines = f.read().split('\n')

        # 差分指示を適用するロジック (memory_tools.py と同じ)
        line_plan = {}
        insertions = {}
        timestamp = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}]"
        
        applied_count = 0
        skipped_empty_count = 0

        for inst in instructions:
            op = inst.get("operation", "").lower()
            line_num = inst.get("line")
            if line_num is None: continue
            target_index = line_num - 1
            if not (0 <= target_index < len(lines)): continue

            final_content = inst.get("content", "")
            content_stripped = str(final_content).strip()

            # バリデーション: contentに実質な内容がない場合はスキップ
            if not content_stripped and op != "delete":
                skipped_empty_count += 1
                continue

            if op in ["replace", "insert_after"]:
                # content内の各行を処理する
                lines_in_content = str(final_content).split('\n')
                processed_lines = []
                for line in lines_in_content:
                    if line.strip() and not re.match(r"^\[\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}\]", line.strip()):
                        processed_lines.append(f"{timestamp} {line}")
                    else:
                        processed_lines.append(line)
                final_content = "\n".join(processed_lines)

            if op == "delete":
                line_plan[target_index] = {"operation": "delete"}
                applied_count += 1
            elif op == "replace":
                line_plan[target_index] = {"operation": "replace", "content": final_content}
                applied_count += 1
            elif op == "insert_after":
                if target_index not in insertions:
                    insertions[target_index] = []
                insertions[target_index].extend(str(final_content).split('\n'))
                applied_count += 1

        if applied_count == 0 and len(instructions) > 0:
            if skipped_empty_count > 0:
                return f"【警告】書き込み内容が実質的に空（空白のみ）であったため、{skipped_empty_count}件の編集指示をスキップしました。メモ帳は更新されませんでした。"
            return "【エラー】有効な編集指示が見つかりませんでした。メモ帳は更新されませんでした。"

        new_lines = []
        for i, line_content in enumerate(lines):
            plan = line_plan.get(i)
            if plan is None:
                new_lines.append(line_content)
            elif plan["operation"] == "replace":
                new_lines.append(plan["content"])
            elif plan["operation"] == "delete":
                pass

            if i in insertions:
                new_lines.extend(insertions[i])

        with open(notepad_path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines))

        msg = f"成功: {applied_count}件の指示に基づき、メモ帳(notepad.md)を更新しました。"
        if skipped_empty_count > 0:
            msg += f"（※空文字のため{skipped_empty_count}件をスキップしました）"
        return msg
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】メモ帳の編集中に予期せぬエラーが発生しました: {e}"