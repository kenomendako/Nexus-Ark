# tools/developer_tools.py
import os
import glob
import fnmatch
from typing import List, Dict, Any, Optional
from langchain_core.tools import tool
import config_manager

def _should_exclude(path: str, root_path: str, exclude_dirs: List[str], exclude_files: List[str]) -> bool:
    """パスが除外対象かどうかを判定する"""
    rel_path = os.path.relpath(path, root_path)
    parts = rel_path.split(os.sep)
    
    # ディレクトリ除外チェック
    for part in parts:
        if part in exclude_dirs:
            return True
            
    # ファイル除外チェック
    filename = os.path.basename(path)
    for pattern in exclude_files:
        if fnmatch.fnmatch(filename, pattern):
            return True
            
    return False

@tool
def list_project_files(room_name: str, depth: int = 2) -> str:
    """
    指定されたプロジェクトフォルダ（root_path）内のファイルとディレクトリをリストアップします。
    設定された除外リストに従ってフィルタリングされます。
    room_name: 実行中のルーム名（設定取得用）
    depth: 探索する階層の深さ（デフォルト2）
    """
    settings = config_manager.get_effective_settings(room_name)
    explorer_config = settings.get("project_explorer", {})
    
    root_path = explorer_config.get("root_path") or os.getcwd()
    exclude_dirs = explorer_config.get("exclude_dirs", [])
    exclude_files = explorer_config.get("exclude_files", [])
    
    if not os.path.exists(root_path):
        return f"【エラー】プロジェクトルートが見つかりません: {root_path}"

    result = [f"【プロジェクトファイル一覧: {root_path} (深さ: {depth})】"]
    
    try:
        for root, dirs, files in os.walk(root_path):
            # 現在の深さを計算
            rel_dir = os.path.relpath(root, root_path)
            current_depth = 0 if rel_dir == "." else len(rel_dir.split(os.sep))
            
            if current_depth >= depth:
                del dirs[:] # これ以上深く探索しない
                continue

            # 除外ディレクトリをフィルタリング
            dirs[:] = [d for d in dirs if not _should_exclude(os.path.join(root, d), root_path, exclude_dirs, exclude_files)]
            
            indent = "  " * current_depth
            if rel_dir != ".":
                result.append(f"{indent}📁 {os.path.basename(root)}/")
            
            for file in files:
                file_path = os.path.join(root, file)
                if not _should_exclude(file_path, root_path, exclude_dirs, exclude_files):
                    result.append(f"{indent}  📄 {file}")
                    
        return "\n".join(result)
    except Exception as e:
        return f"【エラー】ファイル一覧の取得中にエラーが発生しました: {e}"

@tool
def read_project_file(room_name: str, relative_path: str, start_line: int = 1, end_line: int = None) -> str:
    """
    指定されたファイルの内容を読み取ります。
    start_line: 読み取り開始行（1始まり、デフォルト1）
    end_line: 読み取り終了行（デフォルト最後まで）
    ※大きなファイルは Antigravity のように数回に分けて読むのが賢明です。
    """
    settings = config_manager.get_effective_settings(room_name)
    explorer_config = settings.get("project_explorer", {})
    
    root_path = explorer_config.get("root_path") or os.getcwd()
    exclude_dirs = explorer_config.get("exclude_dirs", [])
    exclude_files = explorer_config.get("exclude_files", [])
    
    full_path = os.path.abspath(os.path.join(root_path, relative_path))
    
    # セキュリティチェック: root_path配下であることを確認
    if not full_path.startswith(os.path.abspath(root_path)):
        return "【エラー】セキュリティ制限により、プロジェクトルート外のファイルは読み取れません。"
        
    if not os.path.exists(full_path):
        return f"【エラー】ファイルが見つかりません: {relative_path}"
        
    if os.path.isdir(full_path):
        return f"【エラー】'{relative_path}' はディレクトリです。中身を読むには read_project_file ではなく、まず list_project_files で中身を確認してください。"

    # 除外チェック
    if _should_exclude(full_path, root_path, exclude_dirs, exclude_files):
        return "【エラー】このファイルは設定により読み取りが除外されています。"

    try:
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
            
        total_lines = len(all_lines)
        start = max(1, start_line)
        end = end_line if end_line is not None else total_lines
        end = min(end, total_lines)
        
        if start > total_lines:
            return f"【エラー】開始行({start})がファイルの合計行数({total_lines})を超えています。"
            
        if start > end:
            return f"【エラー】開始行({start})は終了行({end})以下である必要があります。"

        selected_lines = all_lines[start-1:end]
        
        # 行番号を付与
        output_lines = []
        for i, line in enumerate(selected_lines):
            line_num = start + i
            output_lines.append(f"{line_num:4}: {line.rstrip()}")
            
        content = "\n".join(output_lines)
        
        header = f"【ファイル内容: {relative_path} (L{start}-L{end} / 全{total_lines}行)】\n"
        footer = ""
        if end < total_lines:
            footer = f"\n\n... (L{end+1}以降も続きがあります。続きが必要な場合は read_project_file で start_line={end+1} を指定してください)"
            
        # あまりに巨大な場合の最終防衛（基本は行数指定でコントロールさせる）
        if len(content) > 15000:
            content = content[:15000] + "\n... (一度に読み取る文字数が上限を超えたため、切り詰められました。より狭い行範囲を指定してください)"

        return f"{header}\n{content}{footer}"
    except Exception as e:
        return f"【エラー】ファイルの読み取り中にエラーが発生しました: {e}"
