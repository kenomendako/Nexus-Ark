"""ui_handlers のうち「ノート（メモ帳・創作ノート・研究ノート・研究購読）」ドメイン。

ui_handlers パッケージから再エクスポートされ、呼び出し側は従来どおり
ui_handlers.<関数名> でアクセスできる。
"""

import os
import re
import json
import datetime
import traceback
import gradio as gr
import pandas as pd
import gemini_api, config_manager, alarm_manager, room_manager, utils, constants, chatgpt_importer, claude_importer, generic_importer
import letterbox_manager
import note_storage
import file_lock_utils
from room_manager import get_room_files_paths, get_world_settings_path
from file_lock_utils import safe_text_read, safe_text_write


LETTERBOX_COLUMNS = ["タイトル", "日時", "状態"]


def _empty_letterbox_df() -> pd.DataFrame:
    return pd.DataFrame(columns=LETTERBOX_COLUMNS)


def _format_letterbox_datetime(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _letterbox_choice(letter: dict) -> tuple[str, str]:
    status = "既読" if letter.get("read_at") else "未読"
    created = _format_letterbox_datetime(letter.get("created_at", ""))
    return (f"{status} | {created} | {letter.get('title', '')}", letter.get("id", ""))


def _build_letterbox_outputs(room_name: str):
    letters = letterbox_manager.list_letters(room_name, limit=100) if room_name else []
    rows = [
        [
            letter.get("title", ""),
            _format_letterbox_datetime(letter.get("created_at", "")),
            "既読" if letter.get("read_at") else "未読",
        ]
        for letter in letters
    ]
    choices = [_letterbox_choice(letter) for letter in letters]
    unread = sum(1 for letter in letters if not letter.get("read_at"))
    status = f"📮 未読 {unread} 通 / 全 {len(letters)} 通" if letters else "📮 手紙はまだありません。"
    return pd.DataFrame(rows, columns=LETTERBOX_COLUMNS) if rows else _empty_letterbox_df(), choices, status


def refresh_letterbox(room_name: str):
    df, choices, status = _build_letterbox_outputs(room_name)
    selected = choices[0][1] if choices else None
    return (
        df,
        gr.update(choices=choices, value=selected),
        status,
        "手紙を選ぶと本文が表示され、既読になります。" if choices else "",
        "",
    )


def select_letterbox_letter(room_name: str, letter_id: str):
    if not room_name:
        return gr.update(), gr.update(), "ルームが選択されていません。", "", ""
    if not letter_id:
        df, choices, status = _build_letterbox_outputs(room_name)
        return df, gr.update(choices=choices, value=None), status, "", ""

    letter = letterbox_manager.mark_read(room_name, letter_id)
    if not letter:
        df, choices, status = _build_letterbox_outputs(room_name)
        return df, gr.update(choices=choices, value=None), status, "手紙が見つかりませんでした。", ""

    df, choices, status = _build_letterbox_outputs(room_name)
    title = letter.get("title", "")
    created = _format_letterbox_datetime(letter.get("created_at", ""))
    read_at = _format_letterbox_datetime(letter.get("read_at", ""))
    meta = f"### {title}\n- 届いた日時: {created}\n- 状態: 既読（{read_at}）"
    body = letter.get("body", "")
    return df, gr.update(choices=choices, value=letter_id), status, meta, body


def handle_delete_letterbox_letter(confirmed: str, room_name: str, letter_id: str):
    """選択中の手紙を削除する。

    confirm結果の隠しTextbox経由で呼ばれ、最後の戻り値でTextboxを空にリセットする
    （自己リセット式・gradio_notes.md #19）。
    """
    if str(confirmed).strip().lower() != "true":
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), ""
    if not room_name or not letter_id:
        gr.Warning("削除する手紙が選択されていません。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), ""

    deleted = letterbox_manager.delete_letter(room_name, letter_id)
    df, choices, status = _build_letterbox_outputs(room_name)
    if not deleted:
        gr.Warning("手紙が見つかりませんでした。すでに削除されている可能性があります。")
        return df, gr.update(choices=choices, value=None), status, "", "", ""

    gr.Info(f"手紙「{deleted.get('title', '')}」を削除しました。")
    return df, gr.update(choices=choices, value=None), status, "", "", ""


def load_notepad_content(room_name: str) -> str:
    if not room_name: return ""
    _, _, _, _, _, notepad_path, _ = get_room_files_paths(room_name)
    if notepad_path and os.path.exists(notepad_path):
        return safe_text_read(notepad_path)
    return ""


def handle_save_notepad_click(room_name: str, content: str) -> str:
    if content is None or str(content).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、データ保護のために保存を中止しました。")
        return content

    if not room_name: gr.Warning("ルームが選択されていません。"); return content

    # ▼▼▼【ここに追加】▼▼▼
    room_manager.create_backup(room_name, 'notepad')

    _, _, _, _, _, notepad_path, _ = room_manager.get_room_files_paths(room_name)
    if not notepad_path: gr.Error(f"「{room_name}」のメモ帳パス取得失敗。"); return content
    lines = [f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}] {line.strip()}" if line.strip() and not re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]", line.strip()) else line.strip() for line in content.strip().split('\n') if line.strip()]
    final_content = "\n".join(lines)
    try:
        safe_text_write(notepad_path, final_content + ('\n' if final_content else ''))
        gr.Info(f"「{room_name}」のメモ帳を保存しました。"); return final_content
    except Exception as e: gr.Error(f"メモ帳の保存エラー: {e}"); return content


def handle_clear_notepad_click(room_name: str) -> str:
    if not room_name: gr.Warning("ルームが選択されていません。"); return ""
    _, _, _, _, _, notepad_path, _ = room_manager.get_room_files_paths(room_name)
    if not notepad_path: gr.Error(f"「{room_name}」のメモ帳パス取得失敗。"); return ""
    try:
        safe_text_write(notepad_path, "")
        gr.Info(f"「{room_name}」のメモ帳を空にしました。"); return ""
    except Exception as e: gr.Error(f"メモ帳クリアエラー: {e}"); return f"エラー: {e}"


def handle_reload_notepad(room_name: str) -> str:
    if not room_name: gr.Warning("ルームが選択されていません。"); return ""
    content = load_notepad_content(room_name); gr.Info(f"「{room_name}」のメモ帳を再読み込みしました。"); return content


def _get_room_note_path(room_name: str, default_filename: str, filename: str = None) -> str:
    """ノートファイルのパスを取得する。アーカイブ名は notes/archives 配下へ解決する。"""
    return note_storage.get_room_note_path(room_name, default_filename, filename)


def _get_creative_notes_path(room_name: str, filename: str = None) -> str:
    """創作ノートのパスを取得"""
    return note_storage.get_creative_notes_path(room_name, filename)


def load_creative_notes_content(room_name: str, filename: str = None) -> str:
    """創作ノートの内容を読み込む"""
    return note_storage.read_note_content(room_name, "creative", filename)


def handle_save_creative_notes(room_name: str, content: str, filename: str = None) -> str:
    """創作ノートを保存"""
    if content is None or str(content).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、データ保護のために保存を中止しました。")
        return content

    if not room_name: gr.Warning("ルームが選択されていません。"); return content
    try:
        note_storage.write_note_content(room_name, "creative", content, filename)
        gr.Info(f"「{room_name}」の創作ノートを保存しました。"); return content
    except Exception as e: gr.Error(f"創作ノートの保存エラー: {e}"); return content


def handle_reload_creative_notes(room_name: str, filename: str = None) -> str:
    """創作ノートを再読み込み"""
    if not room_name: gr.Warning("ルームが選択されていません。"); return ""
    content = load_creative_notes_content(room_name, filename); gr.Info(f"「{room_name}」の創作ノートを再読み込みしました。"); return content


def handle_clear_creative_notes(room_name: str, filename: str = None) -> str:
    """創作ノートを空にする"""
    if not room_name: gr.Warning("ルームが選択されていません。"); return ""
    path = _get_creative_notes_path(room_name, filename)
    try:
        safe_text_write(path, "")
        gr.Info(f"「{room_name}」の創作ノートを空にしました。"); return ""
    except Exception as e: gr.Error(f"創作ノートクリアエラー: {e}"); return f"エラー: {e}"


def _parse_notes_entries(content: str) -> list:
    """
    タイムスタンプセクションでノートをパースしてエントリリストを返す。
    形式: --- で始まり、📝 YYYY-MM-DD HH:MM のヘッダーがあるセクション
    または --- で始まり、[YYYY-MM-DD HH:MM] のヘッダーがあるセクション
    """
    import re
    entries = []

    # 区切り線(---)の後にタイムスタンプが続く場合のみ分割（本文中の罫線による誤分割を防止）
    # \s* を追加して区切り線とアイコンの間の不必要な空白・改行を許容する。
    # split位置も保持する。本文が同じエントリが複数ある場合、文字列の最初の
    # 出現だけを置換すると別エントリを更新するため、保存時に物理位置を使う。
    separator_pattern = re.compile(r"\n---+\n\s*(?=📝|\[)")
    sections = []
    cursor = 0
    for separator in separator_pattern.finditer(content):
        sections.append((content[cursor:separator.start()], cursor))
        cursor = separator.end()
    sections.append((content[cursor:], cursor))

    for raw_section, raw_offset in sections:
        section = raw_section.strip()
        if not section:
            continue

        leading = len(raw_section) - len(raw_section.lstrip())
        trailing = len(raw_section.rstrip())
        section_start = raw_offset + leading
        section_end = raw_offset + trailing

        # タイムスタンプを探す (📝 YYYY-MM-DD HH:MM 形式)
        match1 = re.search(r'📝\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})', section)
        # [YYYY-MM-DD HH:MM] 形式
        match2 = re.search(r'\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\]', section)

        if match1:
            date_str = match1.group(1)
            time_str = match1.group(2)
            timestamp = f"{date_str} {time_str}"
            # ヘッダー行を除いたコンテンツ
            content_start = match1.end()
            entry_content = section[content_start:].strip()
        elif match2:
            date_str = match2.group(1)
            time_str = match2.group(2)
            timestamp = f"{date_str} {time_str}"
            content_start = match2.end()
            entry_content = section[content_start:].strip()
        else:
            # タイムスタンプがない場合はセクション全体を1つのエントリとして扱う
            timestamp = "日付なし"
            date_str = ""
            entry_content = section

        if entry_content:
            entries.append({
                "timestamp": timestamp,
                "date": date_str,
                "content": entry_content,
                "raw_section": section,
                "_raw_start": section_start,
                "_raw_end": section_end,
            })

    return entries[::-1]


def handle_load_creative_entries(room_name: str, filename: str = None):
    """創作ノートのエントリを読み込み、UIを更新"""
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), ""

    content = load_creative_notes_content(room_name, filename)
    if not content.strip():
        print("--- [UI] 対象の創作ノートは空です。 ---")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(), content

    entries = _parse_notes_entries(content)

    # 年・月リストを抽出
    years = set()
    months = set()
    choices = []

    for i, entry in enumerate(entries):
        _collect_research_filter_dates(entry, years, months)

        # ラベル作成（タイムスタンプ + 内容のプレビュー）
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{entry['timestamp']} - {preview}"
        # 値はインデックス（文字列として）
        choices.append((label, str(i)))

    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))

    print(f"--- [UI] {len(entries)}件のエントリを読み込みました。 ---")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value=None),
        content  # RAWエディタにも反映
    )


def handle_show_latest_creative(room_name: str, filename: str = None):
    """創作ノートを読み込み、最新のエントリを自動的に選択して表示する。

    Returns:
        (year_filter, month_filter, entry_dropdown, editor_content, raw_editor_content)
    """
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), "", ""

    content = load_creative_notes_content(room_name, filename)
    if not content.strip():
        print("--- [UI] 対象の創作ノートは空です。 ---")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(), "", content

    entries = _parse_notes_entries(content)

    if not entries:
        gr.Info("エントリが見つかりません。RAW編集を使用してください。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(), "", content

    # 年・月リストを抽出
    years = set()
    months = set()
    choices = []

    for i, entry in enumerate(entries):
        _collect_research_filter_dates(entry, years, months)

        # ラベル作成
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{entry['timestamp']} - {preview}"
        choices.append((label, str(i)))

    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))

    # 最新のエントリ（インデックス0）を選択して詳細を表示
    latest_entry = entries[0]
    latest_content = latest_entry.get("content", "")

    gr.Info("最新エントリを表示しています。")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value="0"),  # 最新エントリを選択
        latest_content,  # エディタに最新エントリの内容を表示
        content  # RAWエディタにも反映
    )


def handle_creative_filter_change(room_name: str, year: str, month: str, filename: str = None):
    """創作ノートのフィルタ変更時にドロップダウン選択肢を更新"""
    if not room_name:
        return gr.update(choices=[])

    content = load_creative_notes_content(room_name, filename)
    entries = _parse_notes_entries(content)

    choices = []
    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")

        # フィルタ条件チェック
        match_year = (year == "すべて" or (len(date_str) >= 4 and date_str[:4] == year))
        match_month = (month == "すべて" or (len(date_str) >= 7 and date_str[5:7] == month))

        if match_year and match_month:
            preview = entry["content"][:30].replace("\n", " ")
            if len(entry["content"]) > 30:
                preview += "..."
            label = f"{entry['timestamp']} - {preview}"
            choices.append((label, str(i)))

    return gr.update(choices=choices, value=None)


def handle_creative_selection(room_name: str, selected_idx: str, filename: str = None):
    """創作ノートのエントリ選択時に詳細を表示"""
    if not room_name or selected_idx is None:
        return ""

    try:
        idx = int(selected_idx)
        content = load_creative_notes_content(room_name, filename)
        entries = _parse_notes_entries(content)

        if 0 <= idx < len(entries):
            entry = entries[idx]
            return entry["content"]
        return ""
    except (ValueError, IndexError) as e:
        print(f"エントリ選択エラー: {e}")
        return ""


def handle_save_creative_entry(room_name: str, selected_idx: str, new_content: str, filename: str = None):
    """選択された創作ノートエントリを保存（エントリ内容のみ更新）"""
    if new_content is None or str(new_content).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、データ保護のために保存を中止しました。")
        return new_content

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return new_content

    if selected_idx is None:
        gr.Warning("エントリが選択されていません。RAW編集から全文を編集してください。")
        return new_content

    try:
        idx = int(selected_idx)
        content = load_creative_notes_content(room_name, filename)
        entries = _parse_notes_entries(content)

        if 0 <= idx < len(entries):
            # 元のセクションを新しい内容で置き換え
            old_section = entries[idx]["raw_section"]
            # タイムスタンプヘッダーを保持して内容のみ更新
            timestamp = entries[idx]["timestamp"]
            if timestamp != "日付なし":
                new_section = f"📝 {timestamp}\n{new_content.strip()}"
            else:
                new_section = new_content.strip()

            # 全文の中で置き換え
            updated_content = content.replace(old_section, new_section, 1)

            # 最新ファイルの場合のみ、保存直前にアーカイブチェックを行う (handle_save_creative_notesと同様のロジックを期待するなら)
            # ただし、ここでは特定エントリの更新なので、そのまま上書きで良い。

            path = _get_creative_notes_path(room_name, filename)
            safe_text_write(path, updated_content)

            gr.Info(f"エントリを保存しました。")
            return new_content
        else:
            gr.Warning("選択されたエントリが見つかりません。")
            return new_content
    except Exception as e:
        gr.Error(f"保存エラー: {e}")
        return new_content


def _get_research_notes_path(room_name: str, filename: str = None) -> str:
    """研究ノートのパスを取得"""
    return note_storage.get_research_notes_path(room_name, filename)


def load_research_notes_content(room_name: str, filename: str = None) -> str:
    """研究ノートの内容を読み込む"""
    return note_storage.read_note_content(room_name, "research", filename)


def handle_save_research_notes(room_name: str, content: str, filename: str = None) -> str:
    """研究ノートを保存"""
    if content is None or str(content).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、データ保護のために保存を中止しました。")
        return content

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return content

    try:
        note_storage.write_note_content(room_name, "research", content, filename)
        gr.Info(f"「{room_name}」の研究ノートを保存しました。")
        return content
    except Exception as e:
        gr.Error(f"研究ノートの保存エラー: {e}")
        return content


def handle_reload_research_notes(room_name: str, filename: str = None) -> str:
    """研究ノートを再読み込み"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return ""
    content = load_research_notes_content(room_name, filename)
    gr.Info(f"「{room_name}」の研究ノートを再読み込みしました。")
    return content


def handle_clear_research_notes(room_name: str, filename: str = None) -> str:
    """研究ノートを空にする"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return ""
    path = _get_research_notes_path(room_name, filename)
    try:
        safe_text_write(path, "")
        gr.Info(f"「{room_name}」の研究ノートを空にしました。")
        return ""
    except Exception as e:
        gr.Error(f"研究ノートクリアエラー: {e}")
        return f"エラー: {e}"


def handle_refresh_research_threads(room_name: str):
    """Research Threads の一覧とindex JSONを読み込む。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(choices=[], value=None), "", "", "ルームが選択されていません。"
    try:
        from research_thread_manager import ResearchThreadManager
        manager = ResearchThreadManager(room_name)
        threads = manager.list_threads(status="")
        choices = [thread.get("thread_id") for thread in threads if thread.get("thread_id")]
        selected = choices[0] if choices else None
        body = manager.read_thread(selected) if selected else ""
        status = f"Research Threadsを読み込みました（{len(choices)}件）。"
        gr.Info(status)
        return gr.update(choices=choices, value=selected), body, manager.to_pretty_index_json(), status
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Research Threadsの読み込みエラー: {e}")
        return gr.update(), "", "", f"エラー: {e}"


def handle_research_thread_selection(room_name: str, thread_id: str) -> tuple[str, str]:
    """選択されたResearch Thread本文を読み込む。"""
    if not room_name or not thread_id:
        return "", "Research Threadが選択されていません。"
    try:
        from research_thread_manager import ResearchThreadManager
        body = ResearchThreadManager(room_name).read_thread(thread_id)
        return body, f"Research Thread '{thread_id}' を読み込みました。"
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Research Threadの読み込みエラー: {e}")
        return "", f"エラー: {e}"


def handle_save_research_thread_body(room_name: str, thread_id: str, content: str) -> tuple[str, str]:
    """Research Thread本文を保存する。"""
    if not room_name or not thread_id:
        gr.Warning("ルームまたはResearch Threadが選択されていません。")
        return content or "", "ルームまたはResearch Threadが選択されていません。"
    if content is None or str(content).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、保存を中止しました。")
        return content or "", "無効な内容のため保存を中止しました。"
    try:
        from research_thread_manager import ResearchThreadManager
        manager = ResearchThreadManager(room_name)
        manager.write_thread(thread_id, content)
        gr.Info(f"Research Thread '{thread_id}' を保存しました。")
        return manager.read_thread(thread_id), f"Research Thread '{thread_id}' を保存しました。"
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Research Thread本文の保存エラー: {e}")
        return content or "", f"エラー: {e}"


def handle_save_research_threads_index(room_name: str, content: str):
    """Research Threads index JSONを保存する。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), "", content or "", "ルームが選択されていません。"
    try:
        from research_thread_manager import ResearchThreadManager
        parsed = json.loads(content)
        manager = ResearchThreadManager(room_name)
        index = manager.save_index_from_ui(parsed)
        choices = [thread.get("thread_id") for thread in index.get("threads", []) if thread.get("thread_id")]
        selected = choices[0] if choices else None
        body = manager.read_thread(selected) if selected else ""
        pretty = json.dumps(index, ensure_ascii=False, indent=2)
        gr.Info("Research Threads indexを保存しました。")
        return gr.update(choices=choices, value=selected), body, pretty, "Research Threads indexを保存しました。"
    except json.JSONDecodeError as e:
        gr.Error(f"JSONの形式が不正です: {e}")
        return gr.update(), "", content or "", f"JSONの形式が不正です: {e}"
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Research Threads indexの保存エラー: {e}")
        return gr.update(), "", content or "", f"エラー: {e}"


def _format_research_entry_value(entry: dict, fallback_index: int) -> str:
    """研究ノートのDropdown値に、元ファイルとファイル内インデックスを埋め込む。"""
    source_filename = entry.get("source_filename") or constants.RESEARCH_NOTES_FILENAME
    source_index = entry.get("source_index", fallback_index)
    return f"{source_filename}::{source_index}"


def _parse_research_entry_value(selected_idx: str, filename: str = None) -> tuple[str, int]:
    """新旧形式の研究ノートDropdown値を、元ファイルとインデックスへ戻す。"""
    selected_text = str(selected_idx)
    if "::" in selected_text:
        source_filename, raw_idx = selected_text.rsplit("::", 1)
        return source_filename, int(raw_idx)

    return filename or constants.RESEARCH_NOTES_FILENAME, int(selected_text)


def _get_research_entry_date_candidates(entry: dict) -> set[str]:
    """研究ノートのフィルタ対象日付を、エントリヘッダー日付だけから集める。"""
    import re

    candidates = set()
    date_str = entry.get("date", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        candidates.add(date_str)

    return candidates


def _normalize_research_filter_year(year) -> str:
    if year is None:
        return "すべて"
    normalized = str(year).strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    return normalized or "すべて"


def _normalize_research_filter_month(month) -> str:
    if month is None:
        return "すべて"
    normalized = str(month).strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if normalized == "すべて":
        return normalized
    if normalized.isdigit():
        return normalized.zfill(2)
    return normalized or "すべて"


def _collect_research_filter_dates(entry: dict, years: set, months: set) -> None:
    for date_candidate in _get_research_entry_date_candidates(entry):
        years.add(date_candidate[:4])
        months.add(date_candidate[5:7])


def _research_entry_matches_filter(entry: dict, year: str, month: str) -> bool:
    year = _normalize_research_filter_year(year)
    month = _normalize_research_filter_month(month)
    candidates = _get_research_entry_date_candidates(entry)
    if not candidates:
        return year == "すべて" and month == "すべて"

    for date_candidate in candidates:
        match_year = year == "すべて" or date_candidate[:4] == year
        match_month = month == "すべて" or date_candidate[5:7] == month
        if match_year and match_month:
            return True

    return False


def _research_entry_sort_key(entry: dict) -> tuple[str, str]:
    """日付なしを末尾に回し、日付ありは新しい順に並べるためのキー。"""
    timestamp = entry.get("timestamp", "")
    if timestamp == "日付なし":
        return ("0000-00-00 00:00", entry.get("source_filename", ""))
    return (timestamp, entry.get("source_filename", ""))


def _load_research_entries_for_index(room_name: str, filename: str = None) -> list:
    """研究ノートの一覧表示用エントリを返す。通常表示ではアーカイブも横断する。"""
    if not room_name:
        return []

    if filename and filename != constants.RESEARCH_NOTES_FILENAME:
        target_files = [filename]
    else:
        target_files = room_manager.get_note_files(room_name, "research") or [constants.RESEARCH_NOTES_FILENAME]

    entries = []
    for source_filename in target_files:
        content = load_research_notes_content(room_name, source_filename)
        for source_index, entry in enumerate(_parse_notes_entries(content)):
            enriched_entry = dict(entry)
            enriched_entry["source_filename"] = source_filename
            enriched_entry["source_index"] = source_index
            entries.append(enriched_entry)

    return sorted(entries, key=_research_entry_sort_key, reverse=True)


def handle_load_research_entries(room_name: str, filename: str = None):
    """研究ノートのエントリを読み込み、UIを更新"""
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), ""

    content = load_research_notes_content(room_name, filename)
    entries = _load_research_entries_for_index(room_name, filename)
    if not entries:
        print("--- [UI] 対象の研究ノートは空です。 ---")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(), content

    # 年・月リストを抽出
    years = set()
    months = set()
    choices = []

    for i, entry in enumerate(entries):
        _collect_research_filter_dates(entry, years, months)

        # ラベル作成（タイムスタンプ + 内容のプレビュー）
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{entry['timestamp']} - {preview}"
        choices.append((label, _format_research_entry_value(entry, i)))

    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))

    print(f"--- [UI] {len(entries)}件のエントリを読み込みました。 ---")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value=None),
        content  # RAWエディタにも反映
    )


def handle_show_latest_research(room_name: str, filename: str = None):
    """研究ノートを読み込み、最新のエントリを自動的に選択して表示する。

    Returns:
        (year_filter, month_filter, entry_dropdown, editor_content, raw_editor_content)
    """
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), "", ""

    content = load_research_notes_content(room_name, filename)
    entries = _load_research_entries_for_index(room_name, filename)

    if not entries:
        gr.Info("エントリが見つかりません。RAW編集を使用してください。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(), "", content

    # 年・月リストを抽出
    years = set()
    months = set()
    choices = []

    for i, entry in enumerate(entries):
        _collect_research_filter_dates(entry, years, months)

        # ラベル作成
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{entry['timestamp']} - {preview}"
        choices.append((label, _format_research_entry_value(entry, i)))

    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))

    # 最新のエントリ（インデックス0）を選択して詳細を表示
    latest_entry = entries[0]
    latest_content = latest_entry.get("content", "")

    gr.Info("最新エントリを表示しています。")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value=_format_research_entry_value(latest_entry, 0)),
        latest_content,  # エディタに最新エントリの内容を表示
        content  # RAWエディタにも反映
    )


def handle_research_filter_change(room_name: str, year: str, month: str, filename: str = None):
    """研究ノートのフィルタ変更時にドロップダウン選択肢を更新"""
    if not room_name:
        return gr.update(choices=[])

    entries = _load_research_entries_for_index(room_name, filename)

    choices = []
    for i, entry in enumerate(entries):
        if _research_entry_matches_filter(entry, year, month):
            preview = entry["content"][:30].replace("\n", " ")
            if len(entry["content"]) > 30:
                preview += "..."
            label = f"{entry['timestamp']} - {preview}"
            choices.append((label, _format_research_entry_value(entry, i)))

    return gr.update(choices=choices, value=None)


def handle_research_year_filter_change(room_name: str, year: str, month: str, filename: str = None):
    """年フィルタ変更時に、その年で有効な月候補とエントリ一覧を更新する。"""
    if not room_name:
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=[])

    year = _normalize_research_filter_year(year)
    month = _normalize_research_filter_month(month)
    entries = _load_research_entries_for_index(room_name, filename)
    months = set()
    for entry in entries:
        if _research_entry_matches_filter(entry, year, "すべて"):
            for date_candidate in _get_research_entry_date_candidates(entry):
                if year == "すべて" or date_candidate[:4] == year:
                    months.add(date_candidate[5:7])

    month_choices = ["すべて"] + sorted(months)
    selected_month = month if month in month_choices else "すべて"
    entry_update = handle_research_filter_change(room_name, year, selected_month, filename)
    return gr.update(choices=month_choices, value=selected_month), entry_update


def handle_research_selection(room_name: str, selected_idx: str, filename: str = None):
    """研究ノートのエントリ選択時に詳細を表示"""
    if not room_name or selected_idx is None:
        return ""

    try:
        source_filename, idx = _parse_research_entry_value(selected_idx, filename)
        entries = _parse_notes_entries(load_research_notes_content(room_name, source_filename))

        if 0 <= idx < len(entries):
            entry = entries[idx]
            return entry["content"]
        return ""
    except (ValueError, IndexError) as e:
        print(f"エントリ選択エラー: {e}")
        return ""


def handle_save_research_entry(room_name: str, selected_idx: str, new_content: str, filename: str = None):
    """選択された研究ノートエントリを保存（エントリ内容のみ更新）"""
    if new_content is None or str(new_content).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、データ保護のために保存を中止しました。")
        return new_content

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return new_content

    if selected_idx is None:
        gr.Warning("エントリが選択されていません。RAW編集から全文を編集してください。")
        return new_content

    try:
        source_filename, idx = _parse_research_entry_value(selected_idx, filename)
        content = load_research_notes_content(room_name, source_filename)
        entries = _parse_notes_entries(content)

        if 0 <= idx < len(entries):
            old_section = entries[idx]["raw_section"]
            timestamp = entries[idx]["timestamp"]
            if timestamp != "日付なし":
                new_section = f"📝 {timestamp}\n{new_content.strip()}"
            else:
                new_section = new_content.strip()

            updated_content = content.replace(old_section, new_section, 1)

            path = _get_research_notes_path(room_name, source_filename)
            safe_text_write(path, updated_content)

            gr.Info(f"エントリを保存しました。")
            return new_content
        else:
            gr.Warning("選択されたエントリが見つかりません。")
            return new_content
    except Exception as e:
        gr.Error(f"保存エラー: {e}")
        return new_content


def handle_note_file_list_refresh(room_name: str, note_type: str):
    """指定されたノート種別のファイルリストを更新してDropdownを返す"""
    if not room_name:
        return gr.update()

    files = room_manager.get_note_files(room_name, note_type)
    if not files:
        # フォールバック: デフォルトファイル名を表示
        default_filenam_map = {
            'notepad': constants.NOTEPAD_FILENAME,
            'research': constants.RESEARCH_NOTES_FILENAME,
            'creative': constants.CREATIVE_NOTES_FILENAME
        }
        files = [default_filenam_map.get(note_type, "notes.md")]

    return gr.update(choices=files, value=files[0])


_NOTE_HISTORY_DEFAULT_FILENAMES = {
    "research": constants.RESEARCH_NOTES_FILENAME,
    "creative": constants.CREATIVE_NOTES_FILENAME,
}
_NOTE_HISTORY_DATELESS = "日付なし"
_NOTE_HISTORY_ALL = "すべて"


class _NoteHistoryConflictError(ValueError):
    """履歴画面が読み込んだ後に対象ノートが更新された。"""


def _normalize_note_history_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    normalized = {"研究": "research", "創作": "creative"}.get(normalized, normalized)
    if normalized not in _NOTE_HISTORY_DEFAULT_FILENAMES:
        raise ValueError(f"unsupported note kind: {kind}")
    return normalized


def _normalize_note_history_filename(kind: str, filename: str | None = None) -> str:
    normalized_kind = _normalize_note_history_kind(kind)
    default_filename = _NOTE_HISTORY_DEFAULT_FILENAMES[normalized_kind]
    if not filename:
        return default_filename
    return str(filename).replace("\\", "/").strip("/") or default_filename


def _note_history_empty_state() -> dict:
    return {"loaded": False}


def _note_history_read_snapshot(room_name: str, kind: str, filename: str | None = None) -> tuple[str, str]:
    """履歴対象をロック下で読み、内容とハッシュを同じスナップショットから返す。"""
    normalized_kind = _normalize_note_history_kind(kind)
    normalized_filename = _normalize_note_history_filename(normalized_kind, filename)
    path = note_storage.get_note_path(room_name, normalized_kind, normalized_filename)
    with file_lock_utils.locked_file(path) as locked_path:
        if locked_path.exists():
            content = locked_path.read_text(encoding="utf-8")
        else:
            content = ""
    return content, note_storage.content_hash(content)


def _note_history_read_all(room_name: str, kind: str) -> list[dict]:
    """現行ファイルと全アーカイブを読み、表示用の一意な物理位置を付与する。"""
    normalized_kind = _normalize_note_history_kind(kind)
    if not room_name:
        return []

    source_filenames = room_manager.get_note_files(room_name, normalized_kind)
    if not source_filenames:
        source_filenames = [_NOTE_HISTORY_DEFAULT_FILENAMES[normalized_kind]]

    entries = []
    for source_filename in source_filenames:
        content, source_hash = _note_history_read_snapshot(
            room_name, normalized_kind, source_filename
        )
        for source_index, parsed_entry in enumerate(_parse_notes_entries(content)):
            entry = dict(parsed_entry)
            entry["source_filename"] = source_filename
            entry["source_index"] = source_index
            entry["_source_hash"] = source_hash
            entries.append(entry)

    # 日付は本文の見出しから得た値だけを使う。ファイル名のアーカイブ時刻は
    # 並び順へ混ぜず、同一時刻の別エントリも安定して全件残す。
    return sorted(
        entries,
        key=lambda entry: (
            entry.get("timestamp", "")
            if entry.get("timestamp") != _NOTE_HISTORY_DATELESS
            else "0000-00-00 00:00",
            entry.get("source_filename", ""),
            -int(entry.get("source_index", 0)),
        ),
        reverse=True,
    )


def _note_history_entry_date(entry: dict) -> str:
    date_value = str(entry.get("date") or "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
        return date_value
    return ""


def _normalize_note_history_year(year) -> str:
    if year is None:
        return _NOTE_HISTORY_ALL
    normalized = str(year).strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    return normalized or _NOTE_HISTORY_ALL


def _normalize_note_history_month(month) -> str:
    if month is None:
        return _NOTE_HISTORY_ALL
    normalized = str(month).strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if normalized == _NOTE_HISTORY_ALL:
        return normalized
    if normalized.isdigit():
        return normalized.zfill(2)
    return normalized or _NOTE_HISTORY_ALL


def _note_history_has_date(entry: dict) -> bool:
    return bool(_note_history_entry_date(entry))


def _note_history_entry_matches(entry: dict, year, month) -> bool:
    normalized_year = _normalize_note_history_year(year)
    normalized_month = _normalize_note_history_month(month)
    date_value = _note_history_entry_date(entry)

    if normalized_year == _NOTE_HISTORY_DATELESS or normalized_month == _NOTE_HISTORY_DATELESS:
        return (
            not date_value
            and normalized_year in {_NOTE_HISTORY_ALL, _NOTE_HISTORY_DATELESS}
            and normalized_month in {_NOTE_HISTORY_ALL, _NOTE_HISTORY_DATELESS}
        )
    if not date_value:
        return normalized_year == _NOTE_HISTORY_ALL and normalized_month == _NOTE_HISTORY_ALL

    return (
        (normalized_year == _NOTE_HISTORY_ALL or date_value[:4] == normalized_year)
        and (normalized_month == _NOTE_HISTORY_ALL or date_value[5:7] == normalized_month)
    )


def _note_history_year_choices(entries: list[dict]) -> list[str]:
    years = sorted(
        {_note_history_entry_date(entry)[:4] for entry in entries if _note_history_has_date(entry)},
        reverse=True,
    )
    choices = [_NOTE_HISTORY_ALL] + years
    if any(not _note_history_has_date(entry) for entry in entries):
        choices.append(_NOTE_HISTORY_DATELESS)
    return choices


def _note_history_month_choices(entries: list[dict], year) -> list[str]:
    normalized_year = _normalize_note_history_year(year)
    if normalized_year == _NOTE_HISTORY_DATELESS:
        return [_NOTE_HISTORY_ALL]
    months = set()
    for entry in entries:
        date_value = _note_history_entry_date(entry)
        if not date_value:
            continue
        if normalized_year in {_NOTE_HISTORY_ALL, _NOTE_HISTORY_DATELESS} or date_value[:4] == normalized_year:
            months.add(date_value[5:7])

    choices = [_NOTE_HISTORY_ALL] + sorted(months)
    if normalized_year in {_NOTE_HISTORY_ALL, _NOTE_HISTORY_DATELESS} and any(
        not _note_history_has_date(entry) for entry in entries
    ):
        choices.append(_NOTE_HISTORY_DATELESS)
    return choices


def _note_history_period(entries: list[dict]) -> str:
    if not entries:
        return "期間: 記録なし"
    dates = sorted(
        {_note_history_entry_date(entry) for entry in entries if _note_history_has_date(entry)}
    )
    if not dates:
        return "期間: 日付なし"
    if len(dates) == 1:
        return f"期間: {dates[0]}"
    return f"期間: {dates[0]} ～ {dates[-1]}"


def _note_history_summary(
    entries: list[dict],
    visible_entries: list[dict],
    year=None,
    month=None,
    status: str = "",
) -> str:
    total = len(entries)
    visible = len(visible_entries)
    dateless_count = sum(1 for entry in entries if not _note_history_has_date(entry))
    lines = [f"**全件:** {total}件 / **表示:** {visible}件", _note_history_period(entries)]
    if dateless_count:
        lines.append(f"日付なし: {dateless_count}件（「日付なし」で選択できます）")
    if status:
        lines.append(status)
    return "\n\n".join(lines)


def _note_history_entry_label(entry: dict) -> str:
    preview = str(entry.get("content") or "").replace("\n", " ")
    if len(preview) > 30:
        preview = preview[:30] + "..."
    return f"{entry.get('timestamp', _NOTE_HISTORY_DATELESS)} - {preview}"


def _note_history_entry_value(entry: dict) -> str:
    return (
        f"{entry.get('source_filename', '')}::"
        f"{int(entry.get('source_index', 0))}::"
        f"{entry.get('_source_hash', '')}"
    )


def _parse_note_history_entry_value(selected) -> tuple[str, int, str] | None:
    if selected is None:
        return None
    value = str(selected)
    if value.count("::") < 2:
        return None
    filename, raw_index, expected_hash = value.rsplit("::", 2)
    try:
        source_index = int(raw_index)
    except (TypeError, ValueError):
        return None
    if not filename or source_index < 0 or not expected_hash:
        return None
    return filename, source_index, expected_hash


def _note_history_entry_choices(entries: list[dict], selected=None):
    values = [_note_history_entry_value(entry) for entry in entries]
    selected_value = selected if selected in values else None
    return gr.update(
        choices=[(_note_history_entry_label(entry), _note_history_entry_value(entry)) for entry in entries],
        value=selected_value,
    )


def _note_history_state(room_name: str, kind: str, entry: dict, source_hash: str | None = None) -> dict:
    return {
        "loaded": True,
        "room_name": room_name,
        "kind": _normalize_note_history_kind(kind),
        "filename": entry.get("source_filename", ""),
        "source_index": int(entry.get("source_index", 0)),
        "content_hash": source_hash or entry.get("_source_hash", ""),
        "raw_section": entry.get("raw_section", ""),
        "timestamp": entry.get("timestamp", _NOTE_HISTORY_DATELESS),
    }


def _note_history_find_entry(room_name: str, kind: str, filename: str, source_index: int):
    content, source_hash = _note_history_read_snapshot(room_name, kind, filename)
    entries = _parse_notes_entries(content)
    if source_index < 0 or source_index >= len(entries):
        raise _NoteHistoryConflictError("対象エントリが見つかりません。")
    entry = dict(entries[source_index])
    entry["source_filename"] = filename
    entry["source_index"] = source_index
    entry["_source_hash"] = source_hash
    return content, source_hash, entry


def _note_history_replace_entry(content: str, entry: dict, new_content: str) -> str:
    start = int(entry.get("_raw_start", -1))
    end = int(entry.get("_raw_end", -1))
    if start < 0 or end < start or end > len(content):
        raise _NoteHistoryConflictError("対象エントリの位置を確認できません。")

    raw_section = str(entry.get("raw_section") or "")
    section_prefix_match = re.match(r"---+\s*", raw_section)
    section_prefix = section_prefix_match.group(0) if section_prefix_match else ""
    timestamp = entry.get("timestamp", _NOTE_HISTORY_DATELESS)
    if timestamp == _NOTE_HISTORY_DATELESS:
        replacement = f"{section_prefix}{str(new_content).strip()}".strip()
    else:
        header_match = re.search(
            r"(?:📝\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}|\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\])",
            raw_section,
        )
        header = header_match.group(0) if header_match else f"📝 {timestamp}"
        replacement = f"{section_prefix}{header}\n{str(new_content).strip()}".strip()
    return content[:start] + replacement + content[end:]


def _note_history_visible_selection(entries: list[dict], selected, year, month):
    visible_entries = [entry for entry in entries if _note_history_entry_matches(entry, year, month)]
    selected_value = selected if selected in {_note_history_entry_value(entry) for entry in visible_entries} else None
    return visible_entries, selected_value


def handle_note_history_load(room, kind, show_latest=False):
    """創作・研究ノートを全ファイル横断で読み込む（明示的な履歴読込用）。"""
    try:
        normalized_kind = _normalize_note_history_kind(kind)
    except ValueError:
        return (
            gr.update(choices=[_NOTE_HISTORY_ALL], value=_NOTE_HISTORY_ALL),
            gr.update(choices=[_NOTE_HISTORY_ALL], value=_NOTE_HISTORY_ALL),
            gr.update(choices=[], value=None),
            gr.update(),
            "履歴を読み込めません。ノート種別を確認してください。",
            _note_history_empty_state(),
        )
    if not room:
        return (
            gr.update(choices=[_NOTE_HISTORY_ALL], value=_NOTE_HISTORY_ALL),
            gr.update(choices=[_NOTE_HISTORY_ALL], value=_NOTE_HISTORY_ALL),
            gr.update(choices=[], value=None),
            gr.update(),
            "ルームが選択されていません。",
            _note_history_empty_state(),
        )

    try:
        entries = _note_history_read_all(room, normalized_kind)
    except Exception as exc:
        return (
            gr.update(choices=[_NOTE_HISTORY_ALL], value=_NOTE_HISTORY_ALL),
            gr.update(choices=[_NOTE_HISTORY_ALL], value=_NOTE_HISTORY_ALL),
            gr.update(choices=[], value=None),
            gr.update(),
            f"履歴の読込に失敗しました: {exc}",
            _note_history_empty_state(),
        )

    selected_value = None
    editor_text = ""
    edit_state = _note_history_empty_state()
    if show_latest and entries:
        latest = entries[0]
        selected_value = _note_history_entry_value(latest)
        editor_text = latest.get("content", "")
        edit_state = _note_history_state(room, normalized_kind, latest)

    return (
        gr.update(choices=_note_history_year_choices(entries), value=_NOTE_HISTORY_ALL),
        gr.update(choices=_note_history_month_choices(entries, _NOTE_HISTORY_ALL), value=_NOTE_HISTORY_ALL),
        _note_history_entry_choices(entries, selected_value),
        editor_text,
        _note_history_summary(entries, entries),
        edit_state,
    )


def handle_note_history_latest(room, kind):
    return handle_note_history_load(room, kind, show_latest=True)


def handle_note_history_filter(room, kind, year, month):
    """選択年・月の履歴だけを表示し、選択中の詳細を解除する。"""
    try:
        normalized_kind = _normalize_note_history_kind(kind)
    except ValueError:
        return gr.update(), gr.update(choices=[], value=None), gr.update(), "履歴を読み込めません。", _note_history_empty_state()
    if not room:
        return gr.update(choices=[_NOTE_HISTORY_ALL], value=_NOTE_HISTORY_ALL), gr.update(choices=[], value=None), gr.update(), "ルームが選択されていません。", _note_history_empty_state()

    try:
        entries = _note_history_read_all(room, normalized_kind)
    except Exception as exc:
        return gr.update(), gr.update(choices=[], value=None), gr.update(), f"履歴の読込に失敗しました: {exc}", _note_history_empty_state()

    normalized_year = _normalize_note_history_year(year)
    normalized_month = _normalize_note_history_month(month)
    month_choices = _note_history_month_choices(entries, normalized_year)
    if normalized_month not in month_choices:
        normalized_month = _NOTE_HISTORY_ALL
    visible_entries = [
        entry for entry in entries if _note_history_entry_matches(entry, normalized_year, normalized_month)
    ]
    return (
        gr.update(choices=month_choices, value=normalized_month),
        _note_history_entry_choices(visible_entries),
        "",
        _note_history_summary(entries, visible_entries, normalized_year, normalized_month),
        _note_history_empty_state(),
    )


def handle_note_history_selection(room, kind, selected):
    """履歴選択値から対象ファイルを再読込して詳細編集欄へ表示する。"""
    try:
        normalized_kind = _normalize_note_history_kind(kind)
        parsed_value = _parse_note_history_entry_value(selected)
        if not room or parsed_value is None:
            raise ValueError("履歴が選択されていません。")
        filename, source_index, expected_hash = parsed_value
        if not _note_history_filename_is_current_member(room, normalized_kind, filename):
            raise _NoteHistoryConflictError("対象ファイルが現在のルームのノート一覧にありません。")
        _, source_hash, entry = _note_history_find_entry(room, normalized_kind, filename, source_index)
        if source_hash != expected_hash:
            raise _NoteHistoryConflictError("一覧表示後に対象ファイルが更新されました。")
        return entry.get("content", ""), _note_history_state(
            room, normalized_kind, entry, source_hash
        )
    except Exception as exc:
        gr.Warning(f"履歴の選択に失敗しました: {exc}。一覧を再読込してください。")
        return gr.update(), _note_history_empty_state()


def _note_history_state_matches(state, room, kind, filename, source_index):
    return bool(
        isinstance(state, dict)
        and state.get("loaded")
        and state.get("room_name") == room
        and state.get("kind") == kind
        and state.get("filename") == filename
        and int(state.get("source_index", -1)) == int(source_index)
        and state.get("content_hash")
    )


def _note_history_filename_is_current_member(room, kind, filename) -> bool:
    """保存直前に、ファイルが現在のルーム／ノート種別の候補であることを確認する。"""
    allowed = room_manager.get_note_files(room, kind)
    if not allowed:
        allowed = [_NOTE_HISTORY_DEFAULT_FILENAMES[kind]]
    return filename in allowed


def handle_note_history_save(room, kind, selected, text, state, year, month):
    """履歴の物理位置とハッシュを検証して、対象セクションだけを原子的に更新する。"""
    draft = "" if text is None else str(text)
    written_state = state
    written_entry = None
    written_hash = ""
    try:
        normalized_kind = _normalize_note_history_kind(kind)
        parsed_value = _parse_note_history_entry_value(selected)
        if not room or parsed_value is None:
            raise ValueError("保存対象の履歴が選択されていません。")
        filename, source_index, expected_hash = parsed_value
        if not _note_history_state_matches(state, room, normalized_kind, filename, source_index):
            raise _NoteHistoryConflictError("読込状態が古いか、別ルーム／別ファイルの履歴です。")
        if state.get("content_hash") != expected_hash:
            raise _NoteHistoryConflictError("一覧表示後に対象ファイルが更新されました。")
        if not _note_history_filename_is_current_member(room, normalized_kind, filename):
            raise _NoteHistoryConflictError("対象ファイルが現在のルームのノート一覧にありません。")
        if text is None or draft.strip() == "None":
            raise ValueError("無効な本文のため保存を中止しました。")

        path = note_storage.get_note_path(room, normalized_kind, filename)
        with file_lock_utils.locked_file(path) as locked_path:
            current_content = locked_path.read_text(encoding="utf-8") if locked_path.exists() else ""
            current_hash = note_storage.content_hash(current_content)
            if current_hash != state.get("content_hash"):
                raise _NoteHistoryConflictError("保存前に対象ノートが更新されました。")

            current_entries = _parse_notes_entries(current_content)
            if source_index < 0 or source_index >= len(current_entries):
                raise _NoteHistoryConflictError("対象エントリが見つかりません。")
            target = current_entries[source_index]
            if (
                target.get("raw_section") != state.get("raw_section")
                or target.get("timestamp") != state.get("timestamp")
            ):
                raise _NoteHistoryConflictError("対象エントリが変更されたため保存できません。")
            updated_content = _note_history_replace_entry(current_content, target, draft)
            written_hash = note_storage.content_hash(updated_content)
            updated_entries = _parse_notes_entries(updated_content)
            # 本文を空にした場合や、本文へ別の記録見出しを加えた場合も、
            # 保存後の位置から選択を決める。別記録のindexを読込元にしない。
            written_entry = None
            for updated_index, updated_entry in enumerate(updated_entries):
                if updated_entry.get("_raw_start") == target.get("_raw_start"):
                    source_index = updated_index
                    written_entry = dict(updated_entry)
                    written_entry["source_filename"] = filename
                    written_entry["source_index"] = updated_index
                    written_entry["_source_hash"] = written_hash
                    break
            if written_entry is not None and written_entry.get("content", "") != draft.strip():
                # 複数記録を貼り付けた場合、編集欄全体を1記録の読込元へ
                # 結びつけない。保存した記録を一覧から選び直す。
                written_entry = None
            written_state = (
                _note_history_state(room, normalized_kind, written_entry, written_hash)
                if written_entry is not None else _note_history_empty_state()
            )
            file_lock_utils._atomic_text_write(locked_path.as_posix(), updated_content)

        # 保存後の一覧再走査は表示用にのみ使う。保存直後の別書込を
        # stateへ取り込むと、画面に返した下書きがその変更を上書きし得るため、
        # stateは必ず上のロック内で実際に書いた内容から組み立てる。
        try:
            entries = _note_history_read_all(room, normalized_kind)
        except Exception as exc:
            return (
                gr.update(),
                gr.update(),
                draft,
                f"保存しましたが、一覧の更新に失敗しました: {exc}\n\n履歴を再読込してください。",
                written_state,
            )
        normalized_year = _normalize_note_history_year(year)
        normalized_month = _normalize_note_history_month(month)
        month_choices = _note_history_month_choices(entries, normalized_year)
        if normalized_month not in month_choices:
            normalized_month = _NOTE_HISTORY_ALL
        visible_entries, selected_value = _note_history_visible_selection(
            entries, selected, normalized_year, normalized_month
        )
        current_target = next(
            (
                entry
                for entry in entries
                if entry.get("source_filename") == filename
                and int(entry.get("source_index", -1)) == source_index
            ),
            None,
        )
        target_still_matches = bool(
            written_entry is not None
            and current_target
            and current_target.get("_source_hash") == written_hash
        )
        fresh_selected_value = _note_history_entry_value(written_entry) if target_still_matches else None
        if fresh_selected_value not in {
            _note_history_entry_value(entry) for entry in visible_entries
        }:
            fresh_selected_value = None
        status = "保存しました。"
        if written_entry is None:
            status = "保存しました。記録の構成が変わったため選択を解除しました。"
        elif not target_still_matches:
            status = "保存しましたが、保存直後に対象ファイルが更新されたため選択を解除しました。"
        return (
            gr.update(choices=month_choices, value=normalized_month),
            _note_history_entry_choices(visible_entries, fresh_selected_value),
            draft,
            _note_history_summary(entries, visible_entries, normalized_year, normalized_month, status),
            written_state,
        )
    except Exception as exc:
        try:
            entries = _note_history_read_all(room, _normalize_note_history_kind(kind)) if room else []
            summary = _note_history_summary(entries, [], year, month, f"保存できませんでした: {exc}")
        except Exception:
            summary = f"保存できませんでした: {exc}"
        return gr.update(), gr.update(), draft, f"{summary}\n\n下書きは保持しています。履歴を再読込してください。", state


def handle_note_raw_load(room, kind, filename):
    """RAW編集用に、指定ファイルだけを読み込む。"""
    try:
        normalized_kind = _normalize_note_history_kind(kind)
        normalized_filename = _normalize_note_history_filename(normalized_kind, filename)
        if not room:
            raise ValueError("ルームが選択されていません。")
        if not _note_history_filename_is_current_member(room, normalized_kind, normalized_filename):
            raise ValueError("指定ファイルが現在のルームのノート一覧にありません。")
        text, source_hash = _note_history_read_snapshot(room, normalized_kind, normalized_filename)
        return text, {
            "loaded": True,
            "room_name": room,
            "kind": normalized_kind,
            "filename": normalized_filename,
            "content_hash": source_hash,
        }
    except Exception as exc:
        gr.Warning(f"ファイルの読込に失敗しました: {exc}。下書きは保持されています。")
        return gr.update(), _note_history_empty_state()


def handle_note_raw_save(room, kind, filename, text, raw_state):
    """RAW編集をロード時ハッシュと照合して保存し、競合時は下書きを返す。"""
    draft = "" if text is None else str(text)
    try:
        normalized_kind = _normalize_note_history_kind(kind)
        normalized_filename = _normalize_note_history_filename(normalized_kind, filename)
        if not room:
            raise ValueError("ルームが選択されていません。")
        if not _note_history_state_matches(
            raw_state, room, normalized_kind, normalized_filename, -1
        ):
            raise _NoteHistoryConflictError("ファイルの読込状態が古いか、別ルーム／別ファイルです。")
        if not _note_history_filename_is_current_member(room, normalized_kind, normalized_filename):
            raise _NoteHistoryConflictError("対象ファイルが現在のルームのノート一覧にありません。")
        note_storage.write_note_content(
            room,
            normalized_kind,
            draft,
            normalized_filename,
            expected_hash=raw_state.get("content_hash"),
        )
        # write_note_content はロック下でこの内容を原子的に確定して返す。
        # 保存直後の別書込を再読込してstateへ取り込むと、画面の下書きと
        # 異なる内容を次回保存で上書きするため、ハッシュは実書込値から作る。
        written_hash = note_storage.content_hash(draft)
        return draft, {
            "loaded": True,
            "room_name": room,
            "kind": normalized_kind,
            "filename": normalized_filename,
            "content_hash": written_hash,
        }
    except note_storage.NoteWriteConflictError:
        gr.Warning("ファイル全文を保存できませんでした。読み込み後にノートが更新されています。下書きは保持しています。「🔄 ファイルを読み込む」で最新の内容を確認してください。")
        return draft, raw_state
    except Exception as exc:
        gr.Warning(f"ファイル全文を保存できませんでした: {exc}。下書きは保持しています。再読込してください。")
        return draft, raw_state


def _research_subscription_rows(room_name: str):
    from research_subscription_manager import ResearchSubscriptionManager
    subs = ResearchSubscriptionManager(room_name).list_subscriptions()
    rows = []
    for s in subs:
        last = s.get("last_run")
        if last:
            try:
                last_disp = datetime.datetime.fromisoformat(last).strftime("%m/%d %H:%M")
            except (ValueError, TypeError):
                last_disp = "—"
        else:
            last_disp = "未実行"
        rows.append([
            s.get("id", ""),
            s.get("topic", ""),
            s.get("focus", ""),
            constants.RESEARCH_SUBSCRIPTION_FREQUENCY_OPTIONS.get(s.get("frequency", ""), s.get("frequency", "")),
            constants.RESEARCH_SUBSCRIPTION_DEPTH_OPTIONS.get(s.get("depth", ""), s.get("depth", "")),
            bool(s.get("enabled", True)),
            last_disp,
        ])
    return rows


def handle_research_subscription_refresh(room_name: str):
    if not room_name:
        return [], "ルームが選択されていません"
    try:
        rows = _research_subscription_rows(room_name)
        return rows, (f"✅ {len(rows)}件のテーマ" if rows else "リサーチ・テーマは未登録です")
    except Exception as e:
        traceback.print_exc()
        return [], f"❌ エラー: {e}"


def handle_research_subscription_add(room_name, topic, focus, frequency, depth, run_time, seed_urls_csv):
    if not room_name:
        gr.Warning("ルームが選択されていません")
        return gr.update(), "ルームが選択されていません"
    if not topic or not topic.strip():
        gr.Warning("テーマを入力してください")
        return gr.update(), "テーマを入力してください"
    try:
        from research_subscription_manager import ResearchSubscriptionManager
        seeds = [u.strip() for u in (seed_urls_csv or "").split(",") if u.strip()]
        mgr = ResearchSubscriptionManager(room_name)
        existing = mgr.get_by_topic(topic.strip())
        if existing:
            mgr.update_subscription(existing["id"], focus=(focus or "").strip(), frequency=frequency, depth=depth, run_time=run_time, seed_urls=seeds)
            msg = f"✅ テーマ「{topic.strip()}」を更新しました"
        else:
            sub = mgr.add_subscription(topic.strip(), focus=focus, frequency=frequency, depth=depth, seed_urls=seeds, run_time=run_time, created_by="user")
            if sub.get("_limit_exceeded"):
                msg = f"❌ 継続リサーチ・テーマは最大{sub.get('limit', 10)}件です。不要な購読を削除してから追加してください"
            elif sub.get("_dedup_skipped"):
                msg = f"⚠️ 似たテーマ「{sub.get('topic', '')}」が既にあるため、新規追加しませんでした"
            else:
                msg = f"✅ テーマ「{topic.strip()}」を追加しました（研究スレッドを作成）"
        return _research_subscription_rows(room_name), msg
    except Exception as e:
        traceback.print_exc()
        return gr.update(), f"❌ エラー: {e}"


def handle_research_subscription_toggle(room_name, sub_id):
    if not room_name or not sub_id:
        gr.Warning("テーマを選択してください")
        return gr.update(), "テーマを選択してください"
    try:
        from research_subscription_manager import ResearchSubscriptionManager
        mgr = ResearchSubscriptionManager(room_name)
        sub = mgr.get_subscription(sub_id)
        if not sub:
            return gr.update(), "テーマが見つかりません"
        mgr.update_subscription(sub["id"], enabled=not bool(sub.get("enabled", True)))
        return _research_subscription_rows(room_name), "✅ 有効/無効を切り替えました"
    except Exception as e:
        traceback.print_exc()
        return gr.update(), f"❌ エラー: {e}"


def handle_research_subscription_delete(room_name, sub_id):
    if not room_name or not sub_id:
        gr.Warning("削除するテーマを選択してください")
        return gr.update(), "テーマを選択してください"
    try:
        from research_subscription_manager import ResearchSubscriptionManager
        ok = ResearchSubscriptionManager(room_name).remove_subscription(sub_id)
        msg = "✅ 削除しました（蓄積した研究スレッドは残ります）" if ok else "対象が見つかりません"
        return _research_subscription_rows(room_name), msg
    except Exception as e:
        traceback.print_exc()
        return gr.update(), f"❌ エラー: {e}"


def handle_research_subscription_run_now(room_name, sub_id):
    """選択中のテーマを「今すぐ調べる」。完了するとペルソナが起床して結果を追記する。"""
    if not room_name or not sub_id:
        gr.Warning("調べるテーマを選択してください")
        return gr.update(), "テーマを選択してください"
    try:
        import alarm_manager
        result = alarm_manager.run_research_subscription_now(room_name, sub_id)
        msg = ("🔬 " + result.get("message", "")) if result.get("ok") else ("⚠️ " + result.get("message", ""))
        if not result.get("ok"):
            gr.Warning(result.get("message", "実行できませんでした"))
        return _research_subscription_rows(room_name), msg
    except Exception as e:
        traceback.print_exc()
        return gr.update(), f"❌ エラー: {e}"


def handle_research_subscription_preview(room_name, sub_id):
    """選択中テーマにひも付く研究スレッドの最新部分をプレビュー表示する。"""
    if not room_name or not sub_id:
        return ""
    try:
        from research_subscription_manager import ResearchSubscriptionManager
        from research_thread_manager import ResearchThreadManager
        sub = ResearchSubscriptionManager(room_name).get_subscription(sub_id)
        if not sub:
            return ""
        thread_id = sub.get("thread_id") or ""
        if not thread_id:
            return "（このテーマにはまだ研究スレッドがありません）"
        body = ResearchThreadManager(room_name).read_thread(thread_id)
        body = (body or "").strip()
        if not body:
            return "（まだ何も記録されていません）"
        # 末尾＝最近の追記を優先して表示
        max_chars = 2000
        if len(body) > max_chars:
            body = "（前略）\n" + body[-max_chars:]
        return body
    except Exception as e:
        return f"（プレビュー取得エラー: {e}）"


def handle_research_import_watchlist_urls(room_name):
    """このルームのウォッチリストURLを種URL欄へ取り込む（カンマ区切りで返す）。"""
    if not room_name:
        return gr.update()
    try:
        from watchlist_manager import WatchlistManager
        entries = WatchlistManager(room_name).get_entries()
        urls = [e.get("url", "").strip() for e in entries if e.get("url")]
        # 重複排除（順序保持）
        seen, uniq = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        if not uniq:
            gr.Info("このルームのウォッチリストにURLがありません")
            return gr.update()
        return ", ".join(uniq)
    except Exception as e:
        traceback.print_exc()
        gr.Warning(f"取り込みエラー: {e}")
        return gr.update()


def handle_research_daily_cap_load():
    """1日あたり自動リサーチ上限（全テーマ合計）の現在値を返す。"""
    try:
        import alarm_manager
        return alarm_manager.get_research_subscription_daily_cap()
    except Exception:
        return constants.RESEARCH_SUBSCRIPTION_DEFAULT_DAILY_CAP


def handle_research_daily_cap_save(value):
    """1日あたり自動リサーチ上限を共通設定へ保存する。"""
    try:
        cap = max(0, int(value)) if value is not None else constants.RESEARCH_SUBSCRIPTION_DEFAULT_DAILY_CAP
        config_manager.save_config_if_changed("research_subscription_daily_cap", cap)
        return f"✅ 1日あたり上限を {cap} 件に設定しました"
    except (ValueError, TypeError):
        return "⚠️ 数値を入力してください"
    except Exception as e:
        traceback.print_exc()
        return f"❌ エラー: {e}"
