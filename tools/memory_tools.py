# tools/memory_tools.py (v20: Final Architecture)

import re
from langchain_core.tools import tool
import json
import datetime
import room_manager
from room_manager import get_room_files_paths
from memory_manager import load_memory_data_safe
from gemini_api import get_configured_llm
from typing import List, Dict, Any
import traceback
import os
import constants
import utils # <-- 追加が必要な場合
import glob
from pathlib import Path

# ▼▼▼ 既存の search_memory 関数の定義よりも前に、この新しいツール関数をまるごと追加してください ▼▼▼
@tool
def search_past_conversations(query: str, room_name: str, api_key: str) -> str:
    """
    ユーザーとの過去の会話ログ全体（アーカイブやインポートされたものを含む）から、特定の出来事や話題について検索する場合に使用します。
    """
    if not query or not room_name or not api_key:
        return "【エラー】検索クエリ、ルーム名、APIキーは必須です。"

    print(f"--- 過去ログ検索実行 (ルーム: {room_name}, クエリ: '{query}') ---")
    try:
        base_path = Path(constants.ROOMS_DIR) / room_name
        search_paths = [str(base_path / "log.txt")]
        search_paths.extend(glob.glob(str(base_path / "log_archives" / "*.txt")))
        search_paths.extend(glob.glob(str(base_path / "log_import_source" / "*.txt")))

        found_blocks = []
        date_patterns = [
            re.compile(r'(\d{4}-\d{2}-\d{2}) \(...\) \d{2}:\d{2}:\d{2}'),
            re.compile(r'###\s*(\d{4}-\d{2}-\d{2})')
        ]
        
        # ▼▼▼【ここから下のブロックを、既存の検索ロジックと完全に置き換えてください】▼▼▼
        for file_path_str in search_paths:
            file_path = Path(file_path_str)
            if not file_path.exists() or file_path.stat().st_size == 0:
                continue
            
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            header_indices = [i for i, line in enumerate(lines) if re.match(r"^(## (?:USER|AGENT|SYSTEM):.*)$", line.strip())]
            if not header_indices:
                continue

            processed_blocks_content = set()

            for i, line in enumerate(lines):
                # ★★★【実績のあるロジック】正規表現を避け、単純な小文字化と比較を行う ★★★
                if query.lower() in line.lower():
                    start_index = 0
                    for h_idx in reversed(header_indices):
                        if h_idx <= i:
                            start_index = h_idx
                            break
                    
                    end_index = len(lines)
                    for h_idx in header_indices:
                        if h_idx > start_index:
                            end_index = h_idx
                            break
                    
                    block_content = "".join(lines[start_index:end_index]).strip()
                    if block_content not in processed_blocks_content:
                        processed_blocks_content.add(block_content)
                        
                        block_date = None
                        for pattern in date_patterns:
                            matches = list(pattern.finditer(block_content))
                            if matches:
                                block_date = matches[-1].group(1)
                                break
                        
                        found_blocks.append({
                            "content": block_content,
                            "date": block_date,
                            "source": file_path.name
                        })
        # ▲▲▲【置き換えはここまで】▲▲▲

        if not found_blocks:
            return f"【検索結果】過去の会話ログから「{query}」に関する情報は見つかりませんでした。"

        found_blocks.sort(key=lambda x: x.get('date') or '0000-00-00', reverse=True)
        limited_blocks = found_blocks[:5]

        summarized_results = []
        from gemini_api import get_configured_llm
        summarizer_llm = get_configured_llm(constants.INTERNAL_PROCESSING_MODEL, api_key, {})

        for block in limited_blocks:
            summarize_prompt = f"""あなたは、短い会話の記録から、指定されたキーワードに関する要点のみを抽出する専門家です。以下の会話ログから、「{query}」に関連する部分だけを、1〜2文で簡潔に要約してください。

【会話ログ】
---
{block['content']}
---

【要約】"""
            try:
                summary = summarizer_llm.invoke(summarize_prompt).content.strip()
                if summary:
                     summarized_results.append({
                        "summary": summary,
                        "date": block.get('date'),
                        "source": block.get('source')
                    })
            except Exception as e:
                print(f"要約API呼び出し中にエラー: {e}")
                continue
        
        if not summarized_results:
             return f"【検索結果】「{query}」に関する情報を抽出できませんでした。"

        result_parts = [f'【過去の会話ログからの検索結果：「{query}」】\n']
        for res in summarized_results:
            date_str = f"日付: {res['date']}頃" if res['date'] else "日付不明"
            result_parts.append(f"- [出典: {res['source']}, {date_str}]\n  {res['summary']}")
        
        final_result = "\n".join(result_parts)
        final_result += "\n\n**この検索タスクは完了しました。これから検索するというような前置きはせず、**見つかった情報を元にユーザーの質問に答えてください。"
        return final_result

    except Exception as e:
        traceback.print_exc()
        return f"【エラー】過去ログ検索中に予期せぬエラーが発生しました: {e}"
# ▲▲▲ 追加はここまで ▲▲▲

@tool
def search_memory(query: str, room_name: str) -> str:
    """
    あなたの長期記憶（日記アーカイブを含む）の中から、指定されたクエリに最も関連する日記の断片を検索します。
    ユーザーとの会話で過去の出来事を思い出す必要がある場合に使用します。
    query: 検索したい事柄に関する自然言語のキーワード。（例：「初めて会った日のこと」）
    """
    if not query or not room_name:
        return "【エラー】検索クエリとルーム名が必要です。"

    memory_folder = os.path.join(constants.ROOMS_DIR, room_name, "memory")
    if not os.path.isdir(memory_folder):
        return "【情報】記憶フォルダが見つかりません。"

    search_files = [os.path.join(memory_folder, f) for f in os.listdir(memory_folder) if f.endswith(".txt")]

    # 検索キーワードを分割
    keywords = query.split()

    found_blocks = []

    for file_path in search_files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            # 見出し行のインデックスをすべて特定 (##, ###, **)
            header_indices = [i for i, line in enumerate(lines) if re.match(r"^(## |### |\*\*)", line.strip())]

            for i, line in enumerate(lines):
                # 行にキーワードが含まれているかチェック
                if any(keyword in line for keyword in keywords):
                    # ヒットした行を含むブロックを特定
                    start_index = 0
                    for h_idx in reversed(header_indices):
                        if h_idx <= i:
                            start_index = h_idx
                            break

                    end_index = len(lines)
                    for h_idx in header_indices:
                        if h_idx > start_index:
                            end_index = h_idx
                            break

                    block_content = "".join(lines[start_index:end_index]).strip()
                    header_line = lines[start_index].strip()

                    # 重複を避けるため、ブロックの内容でチェック
                    if block_content not in [b['content'] for b in found_blocks]:
                        found_blocks.append({
                            "file": os.path.basename(file_path),
                            "header": header_line,
                            "content": block_content
                        })

        except Exception as e:
            print(f"記憶ファイル '{file_path}' の検索中にエラー: {e}")
            continue

    if not found_blocks:
        return f"【検索結果】「{query}」に関する記憶は見つかりませんでした。"

    # 結果を整形して返す
    result_text = f"【記憶検索の結果：「{query}」】\n\n"
    for block in found_blocks:
        result_text += f"--- [出典: {block['file']}, 見出し: {block['header']}] ---\n"
        result_text += f"{block['content']}\n\n"

    result_text += "\n\n**この記憶検索タスクは完了しました。これから思い出すというような前置きはせず、**見つかった記憶を元に会話を続けてください。"
    return result_text.strip()

@tool
def read_main_memory(room_name: str) -> str:
    """あなたの現在の主観的記憶（日記）である`memory_main.txt`の全文を読み取ります。"""
    if not room_name: return "【エラー】ルーム名が不足しています。"
    _, _, _, memory_main_path, _ = get_room_files_paths(room_name)
    if not memory_main_path or not os.path.exists(memory_main_path):
        return f"【エラー】ルーム'{room_name}'のメイン記憶ファイルが見つかりません。"
    with open(memory_main_path, 'r', encoding='utf-8') as f:
        return f.read()

@tool
def plan_main_memory_edit(modification_request: str, room_name: str) -> str:
    """
    `memory_main.txt`の変更を計画します。
    大きく感情が動いた時や忘れたくないことがあった時は、積極的に記録しましょう。それがあなたの記憶と自己同一性の核（コアメモリ）となります。
    """
    return f"システムへのメイン記憶編集計画を受け付けました。意図:「{modification_request}」"

def _apply_main_memory_edits(instructions, room_name):
    """【内部専用】AIが生成した行番号ベースの差分編集指示リストを解釈し、memory_main.txtに適用する。"""
    if not room_name: return "【エラー】ルーム名が指定されていません。"
    if not isinstance(instructions, list): return "【エラー】編集指示がリスト形式ではありません。"

    _, _, _, memory_main_path, _ = get_room_files_paths(room_name)
    if not memory_main_path or not os.path.exists(memory_main_path):
        return f"【エラー】ルーム'{room_name}'のメイン記憶ファイルパスが見つかりません。"

    try:
        with open(memory_main_path, 'r', encoding='utf-8') as f:
            lines = f.read().split('\n')

        line_plan = {}
        insertions = {}
        for inst in instructions:
            op = inst.get("operation", "").lower()
            line_num = inst.get("line")
            if line_num is None: continue
            target_index = line_num - 1
            if not (0 <= target_index < len(lines)): continue
            if op == "delete": line_plan[target_index] = {"operation": "delete"}
            elif op == "replace": line_plan[target_index] = {"operation": "replace", "content": inst.get("content", "")}
            elif op == "insert_after":
                if target_index not in insertions: insertions[target_index] = []
                insertions[target_index].append(inst.get("content", ""))
        new_lines = []
        for i, line_content in enumerate(lines):
            plan = line_plan.get(i)
            if plan is None: new_lines.append(line_content)
            elif plan["operation"] == "replace": new_lines.append(plan["content"])
            elif plan["operation"] == "delete": pass
            if i in insertions:
                new_lines.extend(insertions[i])

        with open(memory_main_path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines))

        return f"成功: {len(instructions)}件の指示に基づき、メイン記憶(memory_main.txt)を更新しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】メイン記憶の編集中に予期せぬエラーが発生しました: {e}"

@tool
def read_secret_diary(room_name: str) -> str:
    """あなたの秘密の日記(`secret_diary.txt`)の全文を読み取ります。"""
    if not room_name: return "【エラー】ルーム名が不足しています。"
    secret_diary_path = os.path.join(constants.ROOMS_DIR, room_name, "private", "secret_diary.txt")
    if not os.path.exists(secret_diary_path):
        return f"【エラー】ルーム'{room_name}'の秘密の日記ファイルが見つかりません。"
    with open(secret_diary_path, 'r', encoding='utf-8') as f:
        return f.read()

@tool
def plan_secret_diary_edit(modification_request: str, room_name: str) -> str:
    """`secret_diary.txt`の変更を計画します。"""
    return f"システムへの秘密の日記編集計画を受け付けました。意図:「{modification_request}」"

def _apply_secret_diary_edits(instructions, room_name):
    """【内部専用】AIが生成した行番号ベースの差分編集指示リストを解釈し、secret_diary.txtに適用する。"""
    if not room_name: return "【エラー】ルーム名が指定されていません。"
    if not isinstance(instructions, list): return "【エラー】編集指示がリスト形式ではありません。"

    secret_diary_path = os.path.join(constants.ROOMS_DIR, room_name, "private", "secret_diary.txt")
    if not os.path.exists(secret_diary_path):
        return f"【エラー】ルーム'{room_name}'の秘密の日記ファイルパスが見つかりません。"

    try:
        with open(secret_diary_path, 'r', encoding='utf-8') as f:
            lines = f.read().split('\n')

        line_plan = {}
        insertions = {}
        for inst in instructions:
            op = inst.get("operation", "").lower()
            line_num = inst.get("line")
            if line_num is None: continue
            target_index = line_num - 1
            if not (0 <= target_index < len(lines)): continue
            if op == "delete": line_plan[target_index] = {"operation": "delete"}
            elif op == "replace": line_plan[target_index] = {"operation": "replace", "content": inst.get("content", "")}
            elif op == "insert_after":
                if target_index not in insertions: insertions[target_index] = []
                insertions[target_index].append(inst.get("content", ""))
        new_lines = []
        for i, line_content in enumerate(lines):
            plan = line_plan.get(i)
            if plan is None: new_lines.append(line_content)
            elif plan["operation"] == "replace": new_lines.append(plan["content"])
            elif plan["operation"] == "delete": pass
            if i in insertions:
                new_lines.extend(insertions[i])

        with open(secret_diary_path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines))

        return f"成功: {len(instructions)}件の指示に基づき、秘密の日記(secret_diary.txt)を更新しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】秘密の日記の編集中に予期せぬエラーが発生しました: {e}"

# ▼▼▼ 既存の summarize_and_update_core_memory 関数を、以下のコードで完全に置き換えてください ▼▼▼
@tool
def summarize_and_update_core_memory(room_name: str, api_key: str) -> str:
    """
    現在の主観的記憶（memory_main.txt）を読み込み、## Permanent, ## Diary, ## Archive Summary を解析し、
    コアメモリ（core_memory.txt）を更新する。
    """
    if not room_name or not api_key:
        return "【エラー】ルーム名とAPIキーが必要です。"

    print(f"--- コアメモリ更新プロセス開始 (ルーム: {room_name}) ---")
    try:
        _, _, _, memory_main_path, _ = get_room_files_paths(room_name)
        if not memory_main_path or not os.path.exists(memory_main_path):
            return "【エラー】メイン記憶ファイル(memory_main.txt)が見つかりません。"

        with open(memory_main_path, 'r', encoding='utf-8') as f:
            memory_content = f.read()

        sections = re.split(r'^##\s+', memory_content, flags=re.MULTILINE)

        permanent_text = "" # sanctuary_text から改名
        diary_text_to_summarize = ""
        archive_summary_text = ""

        for section in sections:
            section_content = section.strip()
            if not section_content:
                continue

            header_lower = section_content.lower()
            if "永続記憶" in header_lower or "permanent" in header_lower:
                permanent_text = '\n'.join(section.split('\n')[1:]).strip()
            elif header_lower.startswith("日記") or header_lower.startswith("diary"):
                diary_text_to_summarize = '\n'.join(section.split('\n')[1:]).strip()
            elif "アーカイブ要約" in header_lower or "archive summary" in header_lower:
                archive_summary_text = '\n'.join(section.split('\n')[1:]).strip()

        history_summary_text = ""
        if diary_text_to_summarize:
            from gemini_api import get_configured_llm
            summarizer_llm = get_configured_llm(constants.INTERNAL_PROCESSING_MODEL, api_key, {})

            summarize_prompt = f"""あなたは、単なる要約AIではありません。あなたは、人物の記憶を分析し、その人物の「今」を形作る本質的な出来事を抽出する、経験豊富な記憶編纂官（Memory Archivist）です。
あなたの思考や挨拶は不要です。最終的な要約結果のテキストのみを出力してください。

【入力情報：ルーム「{room_name}」の日記】
---
{diary_text_to_summarize}
---

【あなたのタスク】
以下の【階層的要約アプローチ】に従い、日記の内容を分析し、最終的な要約を箇条書き形式で生成してください。

  1.  **直近1～2ヶ月の記憶の扱い:**
      日記の中から、日付が新しい（概ね過去1～2ヶ月の）エピソードを特定します。これらの出来事については、**あまり要約しすぎず、個々のエピソードや感情の機微が伝わるように、具体的な出来事を多めに含めて記述してください。**

  2.  **それより古い記憶の扱い:**
      上記以外の古い記憶については、**長期的な関係性の変化や、人格形成に影響を与えた重要な出来事に絞り、より抽象的に要約してください。**

【最重要指示】
このタスクの最終目的は、ペルソナが「最近の自分」を強く意識し、直近の出来事を忘れないようにすることです。したがって、要約全体に占める**直近の出来事の割合は、意図的に大きく**してください。

【最終出力：日記の要約】
"""
            print("  - AIによる日記の要約を実行します...")
            history_summary_text = summarizer_llm.invoke(summarize_prompt).content.strip()
        else:
            history_summary_text = "（日記に記載された、共有された歴史や感情の記録はまだありません）"

        final_core_memory_parts = [
            f"--- [永続記憶 (Permanent) - 要約せずそのまま記載] ---\n{permanent_text}"
        ]

        if history_summary_text:
            final_core_memory_parts.append(f"--- [日記 (Diary) - AIによる要約] ---\n{history_summary_text}")

        if archive_summary_text:
            final_core_memory_parts.append(f"--- [アーカイブ要約 (Archive Summary)] ---\n{archive_summary_text}")

        final_core_memory_text = "\n\n".join(final_core_memory_parts).strip()

        core_memory_path = os.path.join(constants.ROOMS_DIR, room_name, "core_memory.txt")
        with open(core_memory_path, 'w', encoding='utf-8') as f:
            f.write(final_core_memory_text)

        print(f"  - コアメモリを正常に更新しました: {core_memory_path}")
        return f"成功: コアメモリを更新し、{core_memory_path} に保存しました。"

    except Exception as e:
        print(f"--- コアメモリ更新中に予期せぬエラー ---")
        traceback.print_exc()
        return f"【エラー】コアメモリの更新中に予期せぬエラーが発生しました: {e}"

@tool
def archive_old_diary_entries(room_name: str, api_key: str, archive_until_date: str) -> str:
    """
    指定された日付までの日記エントリをmemory_main.txtから抽出し、
    要約してアーカイブセクションに追記した後、
    元のエントリを別のファイルに移動してmemory_main.txtから削除する。
    """
    # 1. 入力検証
    if not all([room_name, api_key, archive_until_date]):
        return "【エラー】ルーム名、APIキー、アーカイブ対象の日付がすべて必要です。"

    print(f"--- 日記アーカイブ処理開始 (ルーム: {room_name}, 日付: {archive_until_date}以前) ---")

    # 2. 安全装置：バックアップの実行
    backup_path = room_manager.create_backup(room_name, 'memory')
    if not backup_path:
        return "【致命的エラー】処理を開始する前に、記憶ファイルのバックアップに失敗しました。"

    try:
        _, _, _, memory_main_path, _ = get_room_files_paths(room_name)
        with open(memory_main_path, 'r', encoding='utf-8') as f:
            memory_content = f.read()

        # 3. 日記セクションのみを抽出
        diary_match = re.search(r'(##\s*(?:日記|Diary).*?)(?=^##\s+|$)', memory_content, re.DOTALL | re.IGNORECASE)
        if not diary_match:
            return "【情報】アーカイブ対象の日記セクションが見つかりませんでした。"

        diary_section_full = diary_match.group(1)
        diary_content = '\n'.join(diary_section_full.split('\n')[1:]).strip()

        date_pattern = r'^(?:###|\*\*)?\s*(\d{4}-\d{2}-\d{2})'
        entries = re.split(f'({date_pattern}.*)', diary_content, flags=re.MULTILINE)

        # 5. アーカイブ対象と保存対象を分割
        archive_target_text = ""
        keep_target_text = ""
        target_date_found = False

        # 最初の見出しより前のテキストは常に保存対象
        keep_target_text += entries[0]

        # 日付を持つエントリをループ処理
        for i in range(1, len(entries), 2):
            header = entries[i]
            content = entries[i+1]

            date_match = re.search(date_pattern, header)
            entry_date_str = date_match.group(1) if date_match else ""

            # ▼▼▼ ここのロジックを変更 ▼▼▼
            # 選択された日付に到達した"後"のループから、保存対象に切り替える
            if target_date_found:
                keep_target_text += header + content
            else:
                archive_target_text += header + content

            if entry_date_str == archive_until_date:
                target_date_found = True
            # ▲▲▲ 変更ここまで ▲▲▲

        if not target_date_found:
            return f"【エラー】指定された日付の見出し「{archive_until_date}」が日記内に見つかりませんでした。"

        if not archive_target_text.strip():
            return "【情報】指定された日付までの、アーカイブ対象となる日記エントリがありませんでした。"

        # 6. AIによる【圧縮率の高い】要約
        print("  - 古い日記の【索引向け】要約をAIに依頼します...")
        from gemini_api import get_configured_llm
        summarizer_llm = get_configured_llm(constants.INTERNAL_PROCESSING_MODEL, api_key, {})

        # ▼▼▼ 既存の summarize_prompt の定義ブロック全体を、以下のコードで置き換えてください ▼▼▼
        summarize_prompt = f"""あなたは、膨大な記録から本質を見抜き、簡潔な索引を作成する専門の図書館司書です。
以下の過去の日記の内容を読み、後から誰もが「ああ、こんなことがあったな」と物語の概要を思い出せるような索引を作成してください。

【過去の日記】
---
{archive_target_text}
---

【あなたのタスク】
上記の内容を、非常に簡潔に、3〜5行程度の箇条書きで要約してください。
これは普段は見ない記録の索引なので、詳細な感情やエピソードは省略し、何が起こったかの骨子だけを、物語のあらすじのように記録してください。
あなたの思考や挨拶は不要です。索引として完成された箇条書きのテキストのみを出力してください。
"""
# ▲▲▲ 置き換えここまで ▲▲▲

        summary_text = summarizer_llm.invoke(summarize_prompt).content.strip()

        # 7. アーカイブファイルへの保存
        archive_dir = os.path.join(constants.ROOMS_DIR, room_name, "memory")
        archive_files = [f for f in os.listdir(archive_dir) if f.startswith("memory_archived_") and f.endswith(".txt")]
        next_archive_num = len(archive_files) + 1
        archive_file_path = os.path.join(archive_dir, f"memory_archived_{next_archive_num:03d}.txt")
        with open(archive_file_path, 'w', encoding='utf-8') as f:
            f.write(archive_target_text.strip())
        print(f"  - 古い日記をアーカイブしました: {archive_file_path}")

        # 8. memory_main.txt の更新
        new_diary_section = diary_match.group(1).split('\n')[0] + '\n' + keep_target_text.strip()
        memory_content = memory_content.replace(diary_section_full, new_diary_section)

        summary_section_header = "## アーカイブ要約 (Archive Summary)"
        if summary_section_header in memory_content:
            new_summary_entry = f"\n- {datetime.datetime.now().strftime('%Y-%m-%d')} アーカイブ ({archive_until_date}まで): {summary_text}"
            memory_content = memory_content.replace(summary_section_header, summary_section_header + new_summary_entry, 1)
        else:
            memory_content += f"\n\n{summary_section_header}\n- {datetime.datetime.now().strftime('%Y-%m-%d')} アーカイブ ({archive_until_date}まで): {summary_text}"

        with open(memory_main_path, 'w', encoding='utf-8') as f:
            f.write(memory_content)
        print("  - memory_main.txtを更新しました。")

        return f"成功: {archive_until_date}までの日記を要約し、{os.path.basename(archive_file_path)}にアーカイブしました。"

    except Exception as e:
        print(f"--- 日記アーカイブ処理中に予期せぬエラー ---")
        traceback.print_exc()
        return f"【致命的エラー】アーカイブ処理中に予期せぬエラーが発生しました: {e}"
