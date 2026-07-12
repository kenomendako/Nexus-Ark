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
    # \s* を追加して区切り線とアイコンの間の不必要な空白・改行を許容する
    sections = re.split(r'\n---+\n\s*(?=📝|\[)', content)

    for section in sections:
        section = section.strip()
        if not section:
            continue

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
                "raw_section": section
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
