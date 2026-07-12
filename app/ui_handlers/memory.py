"""ui_handlers のうち「記憶」ドメイン（コア記憶・アイデンティティ・目的・日記・
夢日記・エピソード記憶・エンティティ記憶・ワーキングメモリ・記憶メンテナンス
/consolidation・再インデックス）。

ui_handlers パッケージから再エクスポートされ、呼び出し側は従来どおり
ui_handlers.<関数名> でアクセスできる。
"""

import os
import sys
import re
import glob
import json
import datetime
import subprocess
import logging
import traceback
from typing import Optional, Tuple, List, Dict, Union, Any
import pandas as pd
import gradio as gr
import gemini_api, config_manager, alarm_manager, room_manager, utils, constants, chatgpt_importer, claude_importer, generic_importer
import rag_manager
import utils
from room_manager import get_room_files_paths, get_world_settings_path
from episodic_memory_manager import EpisodicMemoryManager
from file_lock_utils import safe_json_read, safe_text_read, safe_text_write


def load_core_memory_content(room_name: str) -> str:
    """core_memory.txtの内容を安全に読み込むヘルパー関数。"""
    if not room_name: return ""
    core_memory_path = os.path.join(constants.ROOMS_DIR, room_name, "core_memory.txt")
    # core_memory.txt は ensure_room_files で作成されない場合があるため、ここで存在チェックと作成を行う
    if not os.path.exists(core_memory_path):
        try:
            safe_text_write(core_memory_path, "") # 空ファイルを作成
            return ""
        except Exception as e:
            print(f"コアメモリファイルの作成に失敗: {e}")
            return "（コアメモリファイルの作成に失敗しました）"

    return safe_text_read(core_memory_path)


def handle_save_core_memory(room_name: str, content: str) -> str:
    """コアメモリの保存ボタンのイベントハンドラ。"""
    if content is None or str(content).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、データ保護のために保存を中止しました。")
        return content

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return content

    # ▼▼▼【ここに追加】▼▼▼
    room_manager.create_backup(room_name, 'core_memory')

    core_memory_path = os.path.join(constants.ROOMS_DIR, room_name, "core_memory.txt")
    try:
        safe_text_write(core_memory_path, content)
        gr.Info(f"「{room_name}」のコアメモリを保存しました。")
        return content
    except Exception as e:
        gr.Error(f"コアメモリの保存エラー: {e}")
        return content


def handle_reload_core_memory(room_name: str) -> str:
    """コアメモリの再読込ボタンのイベントハンドラ。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return ""
    content = load_core_memory_content(room_name)
    gr.Info(f"「{room_name}」のコアメモリを再読み込みしました。")
    return content


def handle_init_purpose_profile(room_name: str) -> tuple[str, str]:
    """Purpose Profileを初期化または読み込み、JSONとして返す。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return "", "ルームが選択されていません。"
    try:
        from purpose_profile_manager import PurposeProfileManager
        manager = PurposeProfileManager(room_name)
        content = manager.to_pretty_json()
        gr.Info(f"「{room_name}」のPurpose Profileを読み込みました。")
        return content, "Purpose Profileを読み込みました。未作成の場合は初期ファイルを作成済みです。"
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Purpose Profileの初期化エラー: {e}")
        return "", f"エラー: {e}"


def handle_reload_purpose_profile(room_name: str) -> tuple[str, str]:
    """Purpose Profileを再読み込みする。"""
    return handle_init_purpose_profile(room_name)


def handle_save_purpose_profile(room_name: str, content: str) -> tuple[str, str]:
    """Purpose Profile JSONを保存する。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return content or "", "ルームが選択されていません。"
    if content is None or str(content).strip() == "":
        gr.Warning("空の内容は保存できません。")
        return content or "", "空の内容は保存できません。"
    try:
        from purpose_profile_manager import PurposeProfileManager
        parsed = json.loads(content)
        manager = PurposeProfileManager(room_name)
        normalized = manager.save_profile_from_ui(parsed)
        pretty = json.dumps(normalized, ensure_ascii=False, indent=2)
        gr.Info(f"「{room_name}」のPurpose Profileを保存しました。")
        return pretty, "Purpose Profileを保存しました。"
    except json.JSONDecodeError as e:
        gr.Error(f"JSONの形式が不正です: {e}")
        return content, f"JSONの形式が不正です: {e}"
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Purpose Profileの保存エラー: {e}")
        return content, f"エラー: {e}"


def handle_approve_purpose_change(room_name: str, proposal_id: str) -> tuple[str, str]:
    """保留中のPurpose Profile変更提案を承認する。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return "", "ルームが選択されていません。"
    if not proposal_id or not proposal_id.strip():
        gr.Warning("proposal_idを入力してください。")
        try:
            from purpose_profile_manager import PurposeProfileManager
            return PurposeProfileManager(room_name).to_pretty_json(), "proposal_idを入力してください。"
        except Exception:
            return "", "proposal_idを入力してください。"
    try:
        from purpose_profile_manager import PurposeProfileManager
        manager = PurposeProfileManager(room_name)
        manager.approve_change(proposal_id.strip())
        gr.Info(f"提案 {proposal_id.strip()} を承認しました。")
        return manager.to_pretty_json(), f"提案 {proposal_id.strip()} を承認しました。"
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Purpose Profile提案の承認エラー: {e}")
        try:
            from purpose_profile_manager import PurposeProfileManager
            return PurposeProfileManager(room_name).to_pretty_json(), f"エラー: {e}"
        except Exception:
            return "", f"エラー: {e}"


def handle_discard_purpose_change(room_name: str, proposal_id: str) -> tuple[str, str]:
    """保留中のPurpose Profile変更提案を破棄する。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return "", "ルームが選択されていません。"
    if not proposal_id or not proposal_id.strip():
        gr.Warning("proposal_idを入力してください。")
        try:
            from purpose_profile_manager import PurposeProfileManager
            return PurposeProfileManager(room_name).to_pretty_json(), "proposal_idを入力してください。"
        except Exception:
            return "", "proposal_idを入力してください。"
    try:
        from purpose_profile_manager import PurposeProfileManager
        manager = PurposeProfileManager(room_name)
        manager.discard_change(proposal_id.strip(), reason="UIから破棄")
        gr.Info(f"提案 {proposal_id.strip()} を破棄しました。")
        return manager.to_pretty_json(), f"提案 {proposal_id.strip()} を破棄しました。"
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Purpose Profile提案の破棄エラー: {e}")
        try:
            from purpose_profile_manager import PurposeProfileManager
            return PurposeProfileManager(room_name).to_pretty_json(), f"エラー: {e}"
        except Exception:
            return "", f"エラー: {e}"


def handle_save_diary_raw(room_name, text_content):
    if text_content is None or str(text_content).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、データ保護のために保存を中止しました。")
        return text_content

    if not room_name: gr.Warning("ルームが選択されていません。"); return gr.update()

    # ▼▼▼【ここに追加】▼▼▼
    room_manager.create_backup(room_name, 'diary')

    # 5番目の戻り値が memory_diary_path
    _, _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if not memory_txt_path: gr.Error(f"「{room_name}」の記憶パス取得失敗。"); return gr.update()
    try:
        with open(memory_txt_path, "w", encoding="utf-8") as f:
            f.write(text_content)
        # room_config.json にも更新日時を記録
        config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        config = room_manager.get_room_config(room_name) or {}
        config["memory_diary_last_updated"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        gr.Info(f"'{room_name}' の日記を保存しました。")
        return gr.update(value=text_content)
    except Exception as e: gr.Error(f"日記保存エラー: {e}"); traceback.print_exc(); return gr.update()


def handle_reload_diary_raw(room_name: str):
    """日記のRAWエディタ用に全文を読み込む"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return ""

    gr.Info(f"「{room_name}」の日記を再読み込みしました。")

    memory_content = ""
    # 5番目の戻り値が memory_diary_path
    _, _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if memory_txt_path and os.path.exists(memory_txt_path):
        with open(memory_txt_path, "r", encoding="utf-8") as f:
            memory_content = f.read()

    return memory_content


def _parse_diary_entries(content: str) -> list:
    """
    日記からタイムスタンプセクションをパースしてエントリリストを返す。
    形式: ### YYYY-MM-DD や ** YYYY-MM-DD など見出し
    """
    entries = []

    # 日付パターン（様々な形式に対応）
    # ### 2026-01-15 形式
    # ** 2026-01-15 ** 形式
    # *   **2026-01-15(曜日):** 形式（箇条書き）
    # 2026-01-15 のみの行
    date_pattern = re.compile(r'^[\*\s]*(?:###|##|\*\*|#)?\s*\**\s*(\d{4}-\d{2}-\d{2})(?:[\s\S]*?)$', re.MULTILINE)

    # 日付でセクションを分割
    matches = list(date_pattern.finditer(content))

    for i, match in enumerate(matches):
        date_str = match.group(1)
        start_pos = match.start()

        # 次のマッチまでまたは終端まで
        if i + 1 < len(matches):
            end_pos = matches[i + 1].start()
        else:
            end_pos = len(content)

        section = content[start_pos:end_pos].strip()
        # 見出し行を除いたコンテンツ
        header_end = match.end() - match.start()
        entry_content = section[header_end:].strip()

        if not entry_content:
            entry_content = "(本日はまだ日記がありません。ツールで追記するか、RAW編集で記入してください)"

        entries.append({
            "timestamp": date_str,
            "date": date_str,
            "content": entry_content,
            "raw_section": section
        })

    return entries


def handle_load_identity(room_name: str):
    """永続記憶(Identity)を読み込み、UIを更新"""
    if not room_name:
        return ""

    _, _, _, identity_path, _, _, _ = get_room_files_paths(room_name)
    if not identity_path or not os.path.exists(identity_path):
        gr.Info("永続記憶ファイルが見つかりません。")
        return ""

    with open(identity_path, "r", encoding="utf-8") as f:
        content = f.read()

    return content


def handle_save_identity(room_name: str, content: str):
    """永続記憶(Identity)を保存"""
    if content is None or str(content).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、データ保護のために保存を中止しました。")
        return content

    if not room_name:
        return gr.Info("ルームが選択されていません。")

    _, _, _, identity_path, _, _, _ = get_room_files_paths(room_name)
    if not identity_path:
        return gr.Info("保存先パスが見つかりません。")

    try:
        # バックアップ作成
        room_manager.create_backup(room_name, file_type='memory')

        with open(identity_path, "w", encoding="utf-8") as f:
            f.write(content)
        gr.Info("永続記憶を保存しました。")
    except Exception as e:
        gr.Error(f"保存に失敗しました: {e}")


def handle_refresh_identity_edit_requests(room_name: str):
    """Identity編集提案ボックスの承認待ち一覧を更新する。"""
    columns = ["ID", "時刻", "状態", "提案内容"]
    if not room_name:
        return gr.update(value=pd.DataFrame(columns=columns))

    try:
        from identity_edit_request_manager import pending_requests_dataframe_rows

        rows = pending_requests_dataframe_rows(room_name)
        return gr.update(value=pd.DataFrame(rows, columns=columns))
    except Exception as e:
        gr.Error(f"Identity編集提案の読み込みに失敗しました: {e}")
        return gr.update(value=pd.DataFrame(columns=columns))


def _format_identity_edit_request_detail(request: Optional[Dict[str, Any]]) -> str:
    if not request:
        return "※ 提案が選択されていません"
    created_at = str(request.get("created_at") or "").replace("T", " ")[:19]
    timeline_id = str(request.get("timeline_id") or "").strip() or "-"
    status = str(request.get("status") or "-")
    intent = str(request.get("intent") or "").strip() or "-"
    return (
        f"**状態:** {status}  \n"
        f"**作成:** {created_at or '-'}  \n"
        f"**timeline_id:** `{timeline_id}`  \n"
        f"**意図:** {intent}"
    )


def handle_load_selected_identity_edit_request(evt: gr.SelectData, df: pd.DataFrame, room_name: str):
    """選択されたIdentity編集提案を詳細欄へ読み込む。"""
    if not hasattr(evt, "index") or evt.index is None or df is None or df.empty:
        return "", "", "※ 提案が選択されていません", ""

    try:
        row_idx = evt.index[0]
        request_id = str(df.iloc[row_idx]["ID"])
    except (IndexError, KeyError, TypeError):
        return "", "", "※ 読み込めませんでした", ""

    from identity_edit_request_manager import get_identity_edit_request

    request = get_identity_edit_request(room_name, request_id)
    if not request:
        return "", "", "※ 読み込めませんでした", ""

    return (
        request_id,
        str(request.get("modification_request") or ""),
        _format_identity_edit_request_detail(request),
        "",
    )


def handle_approve_identity_edit_request(room_name: str, request_id: str):
    """Identity編集提案を承認し、編集AI経由でmemory_identity.txtへ反映する。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update(), "", "", "※ ルームが選択されていません", ""
    if not request_id:
        gr.Warning("操作対象のIdentity編集提案が選択されていません。")
        return gr.update(), gr.update(), "", "", "※ 提案が選択されていません", ""

    try:
        from identity_edit_request_manager import approve_identity_edit_request

        updated = approve_identity_edit_request(room_name, request_id)
        gr.Info("Identity編集提案を承認し、永続記憶へ反映しました。")
        pending_df = handle_refresh_identity_edit_requests(room_name)
        identity_content = handle_load_identity(room_name)
        detail = (
            f"反映しました。\n\n"
            f"request_id: `{request_id}`\n\n"
            f"結果: {updated.get('result', '')}"
        )
        return pending_df, identity_content, "", "", detail, ""
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Identity編集提案の承認に失敗しました: {e}")
        return gr.update(), gr.update(), gr.update(), gr.update(), f"承認に失敗しました: {e}", gr.update()


def handle_reject_identity_edit_request(room_name: str, request_id: str, reason: str = ""):
    """Identity編集提案を却下し、結果をペルソナが次ターンで読めるログへ残す。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), "", "", "※ ルームが選択されていません", ""
    if not request_id:
        gr.Warning("操作対象のIdentity編集提案が選択されていません。")
        return gr.update(), "", "", "※ 提案が選択されていません", ""

    try:
        from identity_edit_request_manager import reject_identity_edit_request

        reject_identity_edit_request(room_name, request_id, reason)
        gr.Info("Identity編集提案を却下しました。")
        pending_df = handle_refresh_identity_edit_requests(room_name)
        detail = f"却下しました。\n\nrequest_id: `{request_id}`"
        if reason:
            detail += f"\n\n理由: {reason}"
        return pending_df, "", "", detail, ""
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Identity編集提案の却下に失敗しました: {e}")
        return gr.update(), gr.update(), gr.update(), f"却下に失敗しました: {e}", gr.update()


def handle_load_diary_entries(room_name: str):
    """日記のエントリを読み込み、UIを更新"""
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), ""

    # 5番目の戻り値が memory_diary_path
    _, _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if not memory_txt_path or not os.path.exists(memory_txt_path):
        gr.Info("日記はまだありません。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(), ""

    with open(memory_txt_path, "r", encoding="utf-8") as f:
        content = f.read()

    if not content.strip():
        gr.Info("日記は空です。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(), content

    entries = _parse_diary_entries(content)

    if not entries:
        # エントリが見つからない場合は全文を1エントリとして扱う
        gr.Info("日付形式のエントリが見つかりません。RAW編集を使用してください。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(), content

    # 年・月リストを抽出
    years = set()
    months = set()
    indexed_entries = []

    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")
        if len(date_str) >= 7:
            years.add(date_str[:4])
            months.add(date_str[5:7])

        # プレビュー作成
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{date_str} - {preview}"
        indexed_entries.append({
            "label": label,
            "index": str(i),
            "date": date_str
        })

    # 最新（日付降順）にソートして表示
    indexed_entries.sort(key=lambda x: x["date"], reverse=True)
    choices = [(e["label"], e["index"]) for e in indexed_entries]

    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))

    print(f"--- [UI] {len(entries)}件のエントリを読み込みました。 ---")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value=None),
        content  # RAWエディタにも反映
    )


def handle_show_latest_diary(room_name: str):
    """日記を読み込み、最新のエントリを自動的に選択して表示する。

    Returns:
        (year_filter, month_filter, entry_dropdown, editor_content, raw_editor_content)
    """
    if not room_name:
        return gr.update(choices=["すべて"]), gr.update(choices=["すべて"]), gr.update(choices=[]), "", ""

    # 5番目の戻り値が memory_diary_path
    _, _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if not memory_txt_path or not os.path.exists(memory_txt_path):
        gr.Info("日記はまだありません。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(), "", ""

    with open(memory_txt_path, "r", encoding="utf-8") as f:
        content = f.read()

    if not content.strip():
        gr.Info("日記は空です。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(), "", content

    entries = _parse_diary_entries(content)

    if not entries:
        gr.Info("日付形式のエントリが見つかりません。RAW編集を使用してください。")
        return gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて"), gr.update(), "", content

    # 年・月リストを抽出
    years = set()
    months = set()
    indexed_entries = []

    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")
        if len(date_str) >= 7:
            years.add(date_str[:4])
            months.add(date_str[5:7])

        # プレビュー作成
        preview = entry["content"][:30].replace("\n", " ")
        if len(entry["content"]) > 30:
            preview += "..."
        label = f"{date_str} - {preview}"
        indexed_entries.append({
            "label": label,
            "index": str(i),
            "date": date_str,
            "content": entry["content"]
        })

    # 最新（日付降順）にソート
    indexed_entries.sort(key=lambda x: x["date"], reverse=True)
    choices = [(e["label"], e["index"]) for e in indexed_entries]

    year_choices = ["すべて"] + sorted(list(years), reverse=True)
    month_choices = ["すべて"] + sorted(list(months))

    # 最新のエントリを選択して詳細を表示
    latest_entry = indexed_entries[0]
    latest_content = latest_entry["content"]
    latest_idx = latest_entry["index"]

    gr.Info("最新の日記を表示しています。")
    return (
        gr.update(choices=year_choices, value="すべて"),
        gr.update(choices=month_choices, value="すべて"),
        gr.update(choices=choices, value=latest_idx),  # 最新エントリのインデックスを選択
        latest_content,  # エディタに最新エントリの内容を表示
        content  # RAWエディタにも反映
    )


def handle_diary_filter_change(room_name: str, year: str, month: str):
    """日記のフィルタ変更時にドロップダウン選択肢を更新"""
    if not room_name:
        return gr.update(choices=[])

    # 5番目の戻り値が memory_diary_path
    _, _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if not memory_txt_path or not os.path.exists(memory_txt_path):
        return gr.update(choices=[])

    with open(memory_txt_path, "r", encoding="utf-8") as f:
        content = f.read()

    entries = _parse_diary_entries(content)

    indexed_entries = []
    for i, entry in enumerate(entries):
        date_str = entry.get("date", "")

        match_year = (year == "すべて" or (len(date_str) >= 4 and date_str[:4] == year))
        match_month = (month == "すべて" or (len(date_str) >= 7 and date_str[5:7] == month))

        if match_year and match_month:
            preview = entry["content"][:30].replace("\n", " ")
            if len(entry["content"]) > 30:
                preview += "..."
            label = f"{date_str} - {preview}"
            indexed_entries.append({
                "label": label,
                "index": str(i),
                "date": date_str
            })

    # 最新（日付降順）にソート
    indexed_entries.sort(key=lambda x: x["date"], reverse=True)
    choices = [(e["label"], e["index"]) for e in indexed_entries]

    return gr.update(choices=choices, value=None)


def handle_diary_selection(room_name: str, selected_idx: str):
    """日記のエントリ選択時に詳細を表示"""
    if not room_name or selected_idx is None:
        return ""

    try:
        idx = int(selected_idx)
        # 5番目の戻り値が memory_diary_path
        _, _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
        if not memory_txt_path or not os.path.exists(memory_txt_path):
            return ""

        with open(memory_txt_path, "r", encoding="utf-8") as f:
            content = f.read()

        entries = _parse_diary_entries(content)

        if 0 <= idx < len(entries):
            entry = entries[idx]
            return entry["content"]
        return ""
    except (ValueError, IndexError) as e:
        print(f"日記エントリ選択エラー: {e}")
        return ""


def handle_save_diary_entry(room_name: str, selected_idx: str, new_content: str):
    """選択された日記エントリを保存（エントリ内容のみ更新）"""
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
        # 5番目の戻り値が memory_diary_path
        _, _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
        if not memory_txt_path or not os.path.exists(memory_txt_path):
            return new_content

        with open(memory_txt_path, "r", encoding="utf-8") as f:
            content = f.read()

        entries = _parse_diary_entries(content)

        if 0 <= idx < len(entries):
            old_section = entries[idx]["raw_section"]
            date_str = entries[idx]["date"]
            # 日付ヘッダーを保持して内容のみ更新
            new_section = f"### {date_str}\n{new_content.strip()}"

            updated_content = content.replace(old_section, new_section, 1)

            with open(memory_txt_path, "w", encoding="utf-8") as f:
                f.write(updated_content)

            gr.Info(f"日記エントリを保存しました。")
            return new_content
        else:
            gr.Warning("選択されたエントリが見つかりません。")
            return new_content
    except Exception as e:
        gr.Error(f"保存エラー: {e}")
        return new_content


def _get_date_choices_from_memory(room_name: str) -> List[str]:
    """memory_main.txtの日記セクションから日付見出しを抽出する。"""
    if not room_name:
        return []
    try:
        # 5番目の戻り値が memory_diary_path
        _, _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
        if not memory_txt_path or not os.path.exists(memory_txt_path):
            return []

        with open(memory_txt_path, 'r', encoding='utf-8') as f:
            content = f.read()

        diary_match = re.search(r'##\s*(?:日記|Diary).*?(?=^##\s+|$)', content, re.DOTALL | re.IGNORECASE)
        if not diary_match:
            return []

        diary_content = diary_match.group(0)
        date_pattern = r'(?:###|\*\*)?\s*(\d{4}-\d{2}-\d{2})'
        dates = re.findall(date_pattern, diary_content)

        # 重複を除き、降順で返す
        return sorted(list(set(dates)), reverse=True)
    except Exception as e:
        print(f"日記の日付抽出中にエラー: {e}")
        return []


def handle_archive_memory_tab_select(room_name: str):
    """「記憶」タブが表示されたときに、日付選択肢を更新する。"""
    dates = _get_date_choices_from_memory(room_name)
    return gr.update(choices=dates, value=dates[0] if dates else None)


def handle_archive_memory_click(
    confirmed: any, # Gradioから渡される型が不定なため、anyで受け取る
    room_name: str,
    api_key_name: str,
    archive_date: str
):
    """「アーカイブ実行」ボタンのイベントハンドラ。"""
    # ▼▼▼ 修正点1: キャンセル判定をより厳格に ▼▼▼
    if str(confirmed).lower() != 'true':
        gr.Info("アーカイブ処理をキャンセルしました。")
        return gr.update(), gr.update()

    if not all([room_name, api_key_name, archive_date]):
        gr.Warning("ルーム、APIキー、アーカイブする日付をすべて選択してください。")
        return gr.update(), gr.update()

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or str(api_key).startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        return gr.update(), gr.update()

    gr.Info("古い日記のアーカイブ処理を開始します。この処理には少し時間がかかります...")

    from tools import memory_tools
    result = memory_tools.archive_old_diary_entries.func(
        room_name=room_name,
        api_key=api_key,
        archive_until_date=archive_date
    )

    if "成功" in result:
        gr.Info(f"✅ {result}")
    else:
        gr.Error(f"アーカイブ処理に失敗しました。詳細: {result}")

    # ▼▼▼ 修正点2: 戻り値を自身で正しく構築する ▼▼▼
    # handle_reload_memoryを呼び出さず、必要な処理を直接行う
    new_memory_content = ""
    # 5番目の戻り値が memory_diary_path
    _, _, _, _, memory_txt_path, _, _ = get_room_files_paths(room_name)
    if memory_txt_path and os.path.exists(memory_txt_path):
        with open(memory_txt_path, "r", encoding="utf-8") as f:
            new_memory_content = f.read()

    new_dates = _get_date_choices_from_memory(room_name)
    date_dropdown_update = gr.update(choices=new_dates, value=new_dates[0] if new_dates else None)

    return new_memory_content, date_dropdown_update


def handle_maintenance_accordion_load(room_name: str, api_key_name: str):
    """メンテナンスアコーディオン展開時に、保存済みの最終実行日時を復元する。
    起動高速化で initial_fast_load_outputs から除外されたステータスの遅延ロード。"""
    if not room_name:
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    # エピソード更新ステータスの復元
    room_config = room_manager.get_room_config(room_name) or {}
    override_settings = room_config.get("override_settings", {}) if isinstance(room_config, dict) else {}
    last_episodic_update = (
        override_settings.get("last_episodic_update")
        or room_config.get("last_episodic_update", "未実行")
    )

    # 夢生成ステータスの復元
    last_dream_time = "未実行"
    try:
        api_key = config_manager.GEMINI_API_KEYS.get(
            config_manager._clean_api_key_name(api_key_name or "")
        )
        if api_key:
            from dreaming_manager import DreamingManager
            dm = DreamingManager(room_name, api_key)
            last_dream_time = dm.get_last_dream_time()
    except Exception:
        pass

    # RAG索引ステータスの復元
    memory_index_last_updated = _get_rag_index_last_updated(room_name, "memory")
    current_log_index_last_updated = _get_rag_index_last_updated(room_name, "current_log")

    # 圧縮ステータスの復元
    try:
        from episodic_memory_manager import EpisodicMemoryManager
        stats = EpisodicMemoryManager(room_name).get_compression_stats()
        last_date = stats["last_compressed_date"] or "なし"
        pending = stats["pending_count"]
        last_exec = override_settings.get("last_compression_result") or room_config.get("last_compression_result", "未実行")
        last_compression_result = f"{last_date}まで圧縮済み (対象: {pending}件) | 最終: {last_exec}"
    except Exception as e:
        print(f"圧縮ステータス取得エラー: {e}")
        last_compression_result = "エラー (圧縮状況の取得に失敗しました)"

    return (
        gr.update(value=last_episodic_update),
        gr.update(value=last_dream_time),
        gr.update(value=f"最終更新: {memory_index_last_updated}"),
        gr.update(value=f"最終更新: {current_log_index_last_updated}"),
        gr.update(value=last_compression_result),  # compress_episodes_status
        gr.update(value=format_sleep_maintenance_status(room_name)),
    )


def format_sleep_maintenance_status(room_name: str) -> str:
    """睡眠時メンテナンスのジョブ別結果を表示用に整形する。"""
    if not room_name:
        return "ルームが選択されていません。"

    try:
        data = safe_json_read(str(alarm_manager._maintenance_status_path()), default={"rooms": {}})
    except Exception as e:
        print(f"睡眠時整理ステータス読み込みエラー: {e}")
        return "ステータスの読み込みに失敗しました。"

    room_state = (data or {}).get("rooms", {}).get(room_name, {})
    jobs = room_state.get("jobs", {}) if isinstance(room_state, dict) else {}
    if not jobs:
        return "記録なし"

    order = ["dream", "episodic_memory", "memory_index", "current_log_index", "episode_compression"]
    lines = []
    for job_key in order + [key for key in sorted(jobs) if key not in order]:
        job = jobs.get(job_key)
        if not isinstance(job, dict):
            continue
        label = job.get("label") or job_key
        status = job.get("last_status") or "unknown"
        status_label = {"success": "成功", "failure": "失敗"}.get(status, status)
        message = str(job.get("last_message") or "").strip() or "メッセージなし"
        last_success = job.get("last_success_at") or "-"
        lines.append(f"- {label}: {status_label} / 最終成功: {last_success} / {message}")

    return "\n".join(lines) if lines else "記録なし"


def handle_refresh_sleep_maintenance_status(room_name: str) -> str:
    """手動更新ボタンから睡眠時整理の最終結果を読み込む。"""
    return format_sleep_maintenance_status(room_name)


def handle_manual_sleep_maintenance(room_name: str, api_key_name: str):
    """夜間と同じ睡眠時記憶整理を、静かな自律行動なしで手動投入する。"""
    if not room_name:
        gr.Warning("ルームを選択してください。")
        return gr.update(), format_sleep_maintenance_status(room_name)

    clean_api_key_name = config_manager._clean_api_key_name(api_key_name or "")
    api_key = config_manager.GEMINI_API_KEYS.get(clean_api_key_name)
    current_api_key = clean_api_key_name
    if not api_key or str(api_key).startswith("YOUR_API_KEY"):
        current_api_key = config_manager.get_active_gemini_api_key_name(room_name)
        api_key = config_manager.GEMINI_API_KEYS.get(current_api_key)

    if not api_key or str(api_key).startswith("YOUR_API_KEY"):
        gr.Warning("有効なAPIキーが解決できませんでした。")
        return gr.update(), format_sleep_maintenance_status(room_name)

    try:
        effective_settings = config_manager.get_effective_settings(room_name)
        from dreaming_manager import DreamingManager

        dm = DreamingManager(room_name, api_key)
        has_dreamed_today = alarm_manager._has_real_dream_today(dm._load_insights(), datetime.date.today())
        accepted = alarm_manager.submit_sleep_maintenance(
            room_name,
            effective_settings,
            current_api_key,
            api_key,
            motivation_log=None,
            has_dreamed_today=has_dreamed_today,
            skip_quiet_action=True,
        )
        if not accepted:
            gr.Info("睡眠時記憶整理はすでに実行中です。")
            return gr.update(), format_sleep_maintenance_status(room_name)

        gr.Info("睡眠時記憶整理をバックグラウンドで開始しました。")
        return gr.update(), format_sleep_maintenance_status(room_name)
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"睡眠時記憶整理の開始に失敗しました: {e}")
        return gr.update(), format_sleep_maintenance_status(room_name)


def handle_update_episodic_memory(room_name: str, api_key_name: str):
    """エピソード記憶の更新ボタンのハンドラ"""
    # 初期状態の戻り値 (何も変更しない)
    no_change = (gr.update(), gr.update(), gr.update())

    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        yield (gr.update(), gr.update(), gr.update())
        return

    clean_api_key_name = config_manager._clean_api_key_name(api_key_name)
    _, summary_model_name, _ = config_manager.get_effective_internal_model("summarization")
    summary_model_name = utils.sanitize_model_name(summary_model_name or "")
    api_key = config_manager.GEMINI_API_KEYS.get(clean_api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        api_key = config_manager.get_active_gemini_api_key(room_name, model_name=summary_model_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"エピソード記憶の要約に使用できるGemini APIキーが見つかりません。現在の選択: 「{clean_api_key_name}」")
        yield (gr.update(), gr.update(), gr.update())
        return

    # 1. UIをロック (ボタン:更新中..., チャット欄:無効化)
    yield (
        gr.update(value="⏳ 更新中...", interactive=False),
        gr.update(interactive=False, placeholder="エピソード記憶を更新中です...お待ちください"),
        gr.update()
    )

    gr.Info(f"「{room_name}」のエピソード記憶（要約）を作成・更新しています...")

    msg_buffer = ""
    try:
        manager = EpisodicMemoryManager(room_name)
        msg_buffer = manager.update_memory(api_key)
        gr.Info(f"✅ {msg_buffer}")
    except Exception as e:
        msg_buffer = f"エピソード記憶の更新中にエラーが発生しました: {e}"
        print(msg_buffer)
        import traceback
        traceback.print_exc()
        gr.Error(msg_buffer)

    # UIのロック解除と情報の更新
    try:
        latest_date = manager.get_latest_memory_date()
        new_info_text = f"昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** {latest_date}"
    except:
        new_info_text = "昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** 取得エラー"

    status_text = f"最終更新: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    # 実行結果を room_config.json に保存
    try:
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        if os.path.exists(room_config_path):
            with open(room_config_path, "r", encoding="utf-8") as f:
                room_config = json.load(f)
            room_config["last_episodic_update"] = status_text
            with open(room_config_path, "w", encoding="utf-8") as f:
                json.dump(room_config, f, indent=2, ensure_ascii=False)
    except:
        pass

    yield (
        gr.update(interactive=True, value="エピソード記憶を今すぐ更新"),
        gr.update(interactive=True, placeholder="メッセージを入力してください (Shift+Enterで送信)"),
        gr.update(value=status_text)
    )


def _get_working_memory_updates(room_name: str) -> tuple[gr.update, gr.update, str]:
    """
    指定したルームのワーキングメモリのスロット一覧とアクティブな内容を取得し、
    gr.update オブジェクトとアクティブ状態のMarkdown文字列を返すヘルパー。
    """
    if not room_name:
        return gr.update(choices=[], value=None), gr.update(value="", placeholder="ルームが選択されていません。"), "ルームが選択されていません。"

    slots, active_slot = load_working_memory_slots(room_name)
    content = load_working_memory_content(room_name, active_slot)
    
    character_name = room_manager.get_character_name(room_name)
    active_label = f"現在 {character_name} が使用中のスロット: **{active_slot}**"

    return (
        gr.update(choices=slots, value=active_slot),
        gr.update(value=content, placeholder="ワーキングメモリは空です"),
        active_label
    )


def handle_reload_working_memory(room_name: str, slot_name: str = None) -> tuple[gr.update, gr.update, str]:
    """ワーキングメモリを再読み込み。スロットリストも強制的に同期する(v3)"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update(), ""

    wm_slots_update, wm_content_update, active_label = _get_working_memory_updates(room_name)
    gr.Info(f"「{room_name}」のワーキングメモリを再読み込み（同期）しました。")
    return wm_slots_update, wm_content_update, active_label


def handle_reload_working_memory_metadata(room_name: str) -> tuple[str, str]:
    """ワーキングメモリのメタデータJSONを読み込む。"""
    if not room_name:
        return "", "ルームが選択されていません。"
    try:
        from tools.working_memory_tools import get_working_memory_metadata
        metadata = get_working_memory_metadata(room_name)
        return json.dumps(metadata, ensure_ascii=False, indent=2), "Working Memory metadataを読み込みました。"
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Working Memory metadataの読み込みエラー: {e}")
        return "", f"エラー: {e}"


def handle_save_working_memory_metadata(room_name: str, content: str) -> tuple[str, str]:
    """ワーキングメモリのメタデータJSONを保存する。"""
    if not room_name:
        return content or "", "ルームが選択されていません。"
    try:
        from tools.working_memory_tools import save_working_memory_metadata
        parsed = json.loads(content or "{}")
        metadata = save_working_memory_metadata(room_name, parsed)
        gr.Info("Working Memory metadataを保存しました。")
        return json.dumps(metadata, ensure_ascii=False, indent=2), "Working Memory metadataを保存しました。"
    except json.JSONDecodeError as e:
        gr.Error(f"JSONの形式が不正です: {e}")
        return content or "", f"JSONの形式が不正です: {e}"
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Working Memory metadataの保存エラー: {e}")
        return content or "", f"エラー: {e}"


def handle_manual_dreaming(room_name: str, api_key_name: str):
    """睡眠時記憶整理（夢想プロセス）を手動で実行する"""
    if not room_name:
        return gr.update(), "ルーム名が指定されていません。"

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        return gr.update(), "⚠️ 有効なAPIキーが設定されていません。"

    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, api_key)

        # 夢を見る（洞察生成 & エンティティ更新 & 目標更新）
        result_msg = dm.dream_with_auto_level()

        # 最終実行日時を取得
        last_time = dm.get_last_dream_time()

        if result_msg and ("エラー" in result_msg or "失敗" in result_msg):
            gr.Warning(result_msg)
            return gr.update(), result_msg

        gr.Info("睡眠時記憶整理（手動）が完了しました。")
        return gr.update(), last_time

    except Exception as e:
        print(f"Manual dreaming error: {e}")
        traceback.print_exc()
        return gr.update(), f"エラーが発生しました: {e}"


def handle_manual_insight_only(room_name: str, api_key_name: str):
    """夢日記（洞察）のみを手動で生成する（高速版）"""
    if not room_name:
        return gr.update(), "ルーム名が指定されていません。"

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        return gr.update(), "⚠️ 有効なAPIキーが設定されていません。"

    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, api_key)

        # 夢日記のみ生成 (重い統合処理をスキップ)
        dm.dream_insight_only()

        # 最終実行日時を取得
        last_time = dm.get_last_dream_time()

        gr.Info("夢日記のみの生成（高速）が完了しました。")
        return gr.update(), f"完了 (最新: {last_time})"

    except Exception as e:
        print(f"Manual insight error: {e}")
        traceback.print_exc()
        return gr.update(), f"エラーが発生しました: {e}"


def handle_refresh_dream_journal(room_name: str):
    """夢日記（insights.json）を読み込み、Dropdown の選択肢とフィルタの選択肢を返す"""
    if not room_name:
        return gr.update(choices=[]), "", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])

    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, "dummy_key")
        insights = dm._load_insights()

        # 最新順にソート (created_at は YYYY-MM-DD HH:MM:SS 形式)
        insights.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        choices = []
        years = set()
        months = set()

        for item in insights:
            created_at = item.get("created_at", "")
            if not created_at:
                continue

            date_part = created_at.split(" ")[0] # YYYY-MM-DD
            y, m, d = date_part.split("-")
            years.add(y)
            months.add(m)

            topic = item.get("trigger_topic", "話題なし")
            # トピックを15文字で短縮
            topic_short = (topic[:15] + "..") if len(topic) > 15 else topic

            # ラベルは「日付 (トピック短縮)」、値は「created_at (一意なキー)」
            label = f"{date_part} ({topic_short})"
            choices.append((label, created_at))

        year_choices = ["すべて"] + sorted(list(years), reverse=True)
        month_choices = ["すべて"] + sorted(list(months))

        gr.Info(f"{len(choices)}件の夢日記を読み込みました。")
        return (
            gr.update(choices=choices, value=None),
            "日付を選択すると、ここに詳細が表示されます。",
            gr.update(choices=year_choices, value="すべて"),
            gr.update(choices=month_choices, value="すべて")
        )

    except Exception as e:
        print(f"夢日記読み込みエラー: {e}")
        return gr.update(choices=[]), f"エラー: {e}", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])


def handle_dream_filter_change(room_name: str, year: str, month: str):
    """年・月のフィルタ変更に合わせて、日付ドロップダウンの選択肢を絞り込む"""
    if not room_name:
        return gr.update(choices=[])

    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, "dummy_key")
        insights = dm._load_insights()
        insights.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        filtered_choices = []
        for item in insights:
            created_at = item.get("created_at", "")
            if not created_at: continue

            date_part = created_at.split(" ")[0]
            y, m, _d = date_part.split("-")

            if year != "すべて" and y != year:
                continue
            if month != "すべて" and m != month:
                continue

            topic = item.get("trigger_topic", "話題なし")
            topic_short = (topic[:15] + "..") if len(topic) > 15 else topic
            label = f"{date_part} ({topic_short})"
            filtered_choices.append((label, created_at))

        return gr.update(choices=filtered_choices, value=None)
    except Exception as e:
        print(f"夢日記フィルタリングエラー: {e}")
        return gr.update(choices=[])


def handle_dream_journal_selection_from_dropdown(room_name: str, selected_created_at: str):
    """夢日記のドロップダウンから選択した際、詳細を表示する"""
    if not room_name or not selected_created_at:
        return ""

    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, "dummy_key")
        insights = dm._load_insights()

        # created_at が一意のキーとして動作する
        selected_dream = next((item for item in insights if item.get("created_at") == selected_created_at), None)

        if selected_dream:
            # 詳細テキストを構築
            details = (
                f"【日付】 {selected_dream.get('created_at')}\n"
                f"【トリガー】 {selected_dream.get('trigger_topic')}\n\n"
                f"## 💡 得られた洞察 (Insight)\n"
                f"{selected_dream.get('insight', '（記録なし）')}\n\n"
                f"## 💭 夢の日記 (Dream Log)\n"
                f"{selected_dream.get('log_entry', '（記録なし）')}\n\n"
                f"## 🧭 今後の指針 (Strategy)\n"
                f"{selected_dream.get('strategy', '（記録なし）')}"
            )
            return details

        return "選択された日記が見つかりませんでした。"
    except Exception as e:
        return f"詳細表示エラー: {e}"


def handle_show_latest_dream(room_name: str):
    """
    夢日記を読み込み、最新のエントリを自動的に選択して表示する。

    Returns:
        (date_dropdown, detail_text, year_filter, month_filter)
    """
    if not room_name:
        return gr.update(choices=[]), "", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])

    try:
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, "dummy_key")
        insights = dm._load_insights()

        if not insights:
            gr.Info("夢日記がありません。")
            return gr.update(choices=[]), "夢日記がまだありません。", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])

        # 最新順にソート
        insights.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        choices = []
        years = set()
        months = set()

        for item in insights:
            created_at = item.get("created_at", "")
            if not created_at:
                continue

            date_part = created_at.split(" ")[0]
            y, m, d = date_part.split("-")
            years.add(y)
            months.add(m)

            topic = item.get("trigger_topic", "話題なし")
            topic_short = (topic[:15] + "..") if len(topic) > 15 else topic
            label = f"{date_part} ({topic_short})"
            choices.append((label, created_at))

        year_choices = ["すべて"] + sorted(list(years), reverse=True)
        month_choices = ["すべて"] + sorted(list(months))

        # 最新のエントリを選択して詳細を表示
        latest = insights[0]
        latest_created_at = latest.get("created_at", "")

        details = (
            f"【日付】 {latest.get('created_at')}\\n"
            f"【トリガー】 {latest.get('trigger_topic')}\\n\\n"
            f"## 💡 得られた洞察 (Insight)\\n"
            f"{latest.get('insight', '（記録なし）')}\\n\\n"
            f"## 💭 夢の日記 (Dream Log)\\n"
            f"{latest.get('log_entry', '（記録なし）')}\\n\\n"
            f"## 🧭 今後の指針 (Strategy)\\n"
            f"{latest.get('strategy', '（記録なし）')}"
        )

        gr.Info("最新の夢日記を表示しています。")
        return (
            gr.update(choices=choices, value=latest_created_at),
            details,
            gr.update(choices=year_choices, value="すべて"),
            gr.update(choices=month_choices, value="すべて")
        )

    except Exception as e:
        print(f"夢日記最新表示エラー: {e}")
        traceback.print_exc()
        return gr.update(choices=[]), f"エラー: {e}", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])


def handle_show_latest_episodic(room_name: str):
    """
    エピソード記憶を読み込み、最新のエントリを自動的に選択して表示する。

    Returns:
        (date_dropdown, detail_text, year_filter, month_filter)
    """
    if not room_name:
        return gr.update(choices=[]), "", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])

    try:
        # EpisodicMemoryManagerを使用（月次ファイル対応）
        manager = EpisodicMemoryManager(room_name)
        episodes = manager._load_memory()

        if not episodes:
            gr.Info("エピソード記憶がありません。")
            return gr.update(choices=[]), "エピソード記憶がまだありません。", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])

        # 最新順にソート
        episodes.sort(key=lambda x: x.get("date", ""), reverse=True)

        choices_set = set()
        years = set()
        months = set()

        for ep in episodes:
            date_str = ep.get("date", "")
            if not date_str:
                continue

            parts = date_str.split("-")
            if len(parts) >= 2:
                years.add(parts[0])
                months.add(parts[1])

            choices_set.add(date_str)

        choices = sorted(list(choices_set), reverse=True)
        year_choices = ["すべて"] + sorted(list(years), reverse=True)
        month_choices = ["すべて"] + sorted(list(months))

        # 最新のエントリを選択して詳細を表示
        latest = episodes[0]
        latest_date = latest.get("date", "")
        summary = latest.get("summary", "（なし）")

        gr.Info("最新のエピソード記憶を表示しています。")
        return (
            gr.update(choices=choices, value=latest_date),
            summary,
            gr.update(choices=year_choices, value="すべて"),
            gr.update(choices=month_choices, value="すべて")
        )

    except Exception as e:
        print(f"エピソード記憶最新表示エラー: {e}")
        traceback.print_exc()
        return gr.update(choices=[]), f"エラー: {e}", gr.update(choices=["すべて"]), gr.update(choices=["すべて"])


def _entity_choice_label(entity_id: str, canonical_name: str) -> str:
    return f"{canonical_name} ({entity_id})"


def _resolve_entity_ref(em, entity_ref: str):
    index = em.get_index()
    if entity_ref in index.get("entities", {}):
        return entity_ref, index["entities"].get(entity_ref)
    return em._find_meta_by_name(entity_ref, index)


def handle_refresh_entity_list(room_name: str):
    """エンティティの一覧を取得してドロップダウンを更新する"""
    if not room_name:
        return gr.update(), gr.update(), "", ""

    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    index = em.get_index()
    entities = [
        (_entity_choice_label(entity_id, meta.get("canonical_name", entity_id)), entity_id)
        for entity_id, meta in index.get("entities", {}).items()
        if isinstance(meta, dict) and meta.get("status") != "archived"
    ]

    if not entities:
        return gr.update(), gr.update(), "エンティティがまだ登録されていません。", ""

    entities.sort(key=lambda item: item[0].lower())

    selected = entities[0][1]
    content = em.read_entry_by_id(selected)
    meta = _format_entity_metadata(em, selected)
    merge_targets = [item for item in entities if item[1] != selected]
    merge_target_value = merge_targets[0][1] if merge_targets else None
    return (
        gr.update(choices=entities, value=selected),
        gr.update(choices=merge_targets, value=merge_target_value),
        content,
        meta,
    )


ENTITY_MERGE_CANDIDATE_COLUMNS = ["残す側名", "統合される側名", "類似度", "記録日"]


def _empty_entity_merge_candidate_df() -> "pd.DataFrame":
    return pd.DataFrame(columns=ENTITY_MERGE_CANDIDATE_COLUMNS)


def _build_entity_merge_candidate_dataframe_and_choices(room_name: str):
    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    result = em.list_merge_candidates_for_review(limit=50)
    records = []
    choices = []
    for item in result.get("rows", []):
        records.append([
            item.get("merge_target_name", ""),
            item.get("merge_source_name", ""),
            item.get("similarity", 0.0),
            (item.get("detected_at", "") or "")[:19],
        ])
        label = (
            f"{item.get('source_name', '')} ↔ {item.get('target_name', '')} "
            f"({float(item.get('similarity', 0.0) or 0.0):.3f})"
        )
        choices.append((label, item.get("candidate_id", "")))
    df = pd.DataFrame(records, columns=ENTITY_MERGE_CANDIDATE_COLUMNS) if records else _empty_entity_merge_candidate_df()
    return df, choices, int(result.get("total", 0) or 0), int(result.get("overflow", 0) or 0)


def _entity_merge_candidate_status(total: int, overflow: int) -> str:
    if total <= 0:
        return "🔀 レビュー待ちのマージ候補はありません。"
    suffix = f" 上位50件を表示中。他{overflow}件あります。" if overflow > 0 else ""
    return f"🔀 マージ候補 {total} 件。類似度の高い順に表示しています。{suffix}"


def _entity_list_updates_after_merge_review(em):
    entities = [
        (_entity_choice_label(eid, meta.get("canonical_name", eid)), eid)
        for eid, meta in em.get_index().get("entities", {}).items()
        if isinstance(meta, dict) and meta.get("status") != "archived"
    ]
    entities.sort(key=lambda item: item[0].lower())
    selected = entities[0][1] if entities else None
    merge_targets = [item for item in entities if item[1] != selected] if selected else []
    merge_target_value = merge_targets[0][1] if merge_targets else None
    content = em.read_entry_by_id(selected) if selected else ""
    meta = _format_entity_metadata(em, selected) if selected else ""
    return (
        gr.update(choices=entities, value=selected),
        gr.update(choices=merge_targets, value=merge_target_value),
        content,
        meta,
        json.dumps(em.get_index(), ensure_ascii=False, indent=2),
    )


def refresh_entity_merge_candidates(room_name: str):
    """マージ候補レビューの一覧を読み込む。(df, dropdown, status, note, keep, merge)。"""
    if not room_name:
        return (
            _empty_entity_merge_candidate_df(),
            gr.update(choices=[], value=None),
            "ルームが選択されていません。",
            "",
            "",
            "",
        )
    try:
        df, choices, total, overflow = _build_entity_merge_candidate_dataframe_and_choices(room_name)
        return (
            df,
            gr.update(choices=choices, value=None),
            _entity_merge_candidate_status(total, overflow),
            "候補を選択すると、残す側と統合される側の本文を比較できます。",
            "",
            "",
        )
    except Exception as e:
        traceback.print_exc()
        gr.Warning(f"マージ候補の読み込みに失敗しました: {e}")
        return _empty_entity_merge_candidate_df(), gr.update(choices=[], value=None), f"❌ 読み込み失敗: {e}", "", "", ""


def select_entity_merge_candidate(room_name: str, candidate_id: str):
    """候補選択時に、マージ方向と本文プレビューを表示する。"""
    if not room_name or not candidate_id:
        return "候補が選択されていません。", "", ""
    try:
        from entity_memory_manager import EntityMemoryManager
        em = EntityMemoryManager(room_name)
        detail = em.get_merge_candidate_review_detail(candidate_id)
        if not detail:
            return "候補が見つかりません。一覧を更新してください。", "", ""
        warnings = detail.get("warnings", [])
        warning_text = "\n".join(f"⚠️ {item}" for item in warnings) if warnings else "警告: なし"
        note = (
            f"マージ方向: **{detail.get('merge_source_name')}** → **{detail.get('merge_target_name')}**\n\n"
            f"類似度: {float(detail.get('similarity', 0.0) or 0.0):.3f}\n\n"
            f"{warning_text}"
        )
        return note, detail.get("keep_content", ""), detail.get("merge_content", "")
    except Exception as e:
        traceback.print_exc()
        return f"プレビューを表示できませんでした: {e}", "", ""


def _refresh_merge_candidate_review_outputs(room_name: str, status: str, note: str = ""):
    df, choices, total, overflow = _build_entity_merge_candidate_dataframe_and_choices(room_name)
    if not status:
        status = _entity_merge_candidate_status(total, overflow)
    return df, gr.update(choices=choices, value=None), status, note, "", ""


def approve_entity_merge_candidate(room_name: str, candidate_id: str):
    """選択候補を手動承認し、Phase 2 と同じ merge_entities 経路で統合する。"""
    if not room_name or not candidate_id:
        gr.Warning("統合する候補を選択してください。")
        df, dropdown, status, note, keep, merge = refresh_entity_merge_candidates(room_name)
        return (
            df, dropdown, status, note, keep, merge,
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
        )
    try:
        from entity_memory_manager import EntityMemoryManager
        em = EntityMemoryManager(room_name)
        merge_api_key = None
        try:
            _, merge_model_name, _ = config_manager.get_effective_internal_model("processing")
            merge_api_key = config_manager.get_active_gemini_api_key(room_name, model_name=merge_model_name)
        except Exception:
            merge_api_key = None
        result = em.merge_review_candidate(candidate_id, api_key=merge_api_key)
        if not result.get("ok"):
            gr.Warning(result.get("error", "統合できませんでした。"))
            status = f"❌ 統合できませんでした: {result.get('error', '')}"
        else:
            history = result.get("history", {})
            gr.Info(f"候補を統合しました: {history.get('source_name')} → {history.get('target_name')}")
            status = f"✅ 統合しました: {history.get('source_name')} → {history.get('target_name')}"
        review_df, review_dropdown, review_status, review_note, keep, merge = _refresh_merge_candidate_review_outputs(room_name, status)
        entity_dropdown, merge_target_dropdown, entity_content, entity_meta, entity_index = _entity_list_updates_after_merge_review(em)
        return (
            review_df, review_dropdown, review_status, review_note, keep, merge,
            entity_dropdown, merge_target_dropdown, entity_content, entity_meta, entity_index,
        )
    except Exception as e:
        traceback.print_exc()
        gr.Warning(f"統合に失敗しました: {e}")
        review_df, review_dropdown, review_status, review_note, keep, merge = _refresh_merge_candidate_review_outputs(
            room_name,
            f"❌ 統合に失敗しました: {e}",
        )
        return (
            review_df, review_dropdown, review_status, review_note, keep, merge,
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
        )


def dismiss_entity_merge_candidate(room_name: str, candidate_id: str):
    """選択候補を却下し、次回検出でも戻らないよう dismissed_merge_pairs に記録する。"""
    if not room_name or not candidate_id:
        gr.Warning("候補から外す対象を選択してください。")
        df, dropdown, status, note, keep, merge = refresh_entity_merge_candidates(room_name)
        return (
            df, dropdown, status, note, keep, merge,
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
        )
    try:
        from entity_memory_manager import EntityMemoryManager
        em = EntityMemoryManager(room_name)
        detail = em.get_merge_candidate_review_detail(candidate_id)
        if not detail:
            gr.Warning("候補が見つかりません。一覧を更新してください。")
            status = "候補が見つかりません。一覧を更新してください。"
        else:
            ok = em.dismiss_merge_pair(detail["source_entity_id"], detail["target_entity_id"])
            if ok:
                gr.Info("候補を却下しました。")
                status = f"❌ 候補から外しました: {detail.get('source_name')} ↔ {detail.get('target_name')}"
            else:
                gr.Warning("候補を却下できませんでした。")
                status = "候補を却下できませんでした。"
        review_df, review_dropdown, review_status, review_note, keep, merge = _refresh_merge_candidate_review_outputs(room_name, status)
        entity_dropdown, merge_target_dropdown, entity_content, entity_meta, entity_index = _entity_list_updates_after_merge_review(em)
        return (
            review_df, review_dropdown, review_status, review_note, keep, merge,
            entity_dropdown, merge_target_dropdown, entity_content, entity_meta, entity_index,
        )
    except Exception as e:
        traceback.print_exc()
        gr.Warning(f"候補の却下に失敗しました: {e}")
        review_df, review_dropdown, review_status, review_note, keep, merge = _refresh_merge_candidate_review_outputs(
            room_name,
            f"❌ 候補の却下に失敗しました: {e}",
        )
        return (
            review_df, review_dropdown, review_status, review_note, keep, merge,
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
        )


def handle_search_chat_log_keyword(room_name: str, keyword: str) -> gr.update:
    """
    指定されたキーワードを含むログ月を検索し、ドロップダウンの選択肢をフィルタリングする。
    キーワードが空の場合は全件表示に戻す。
    """
    if not room_name:
        return gr.update()

    base_path = os.path.join(constants.ROOMS_DIR, room_name)
    logs_dir = os.path.join(base_path, constants.LOGS_DIR_NAME)

    if not os.path.exists(logs_dir):
        return gr.update(choices=["最新"], value="最新")

    # 全ファイル取得 (年月リスト構築ロジックの再利用)
    all_files = glob.glob(os.path.join(logs_dir, "*.txt"))
    month_map = {} # "YYYY-MM" -> path
    for fpath in all_files:
        filename = os.path.basename(fpath)
        if re.match(r"\d{4}-\d{2}\.txt", filename):
            month = filename.replace(".txt", "")
            month_map[month] = fpath

    # 検索実行
    if not keyword or not keyword.strip():
        # キーワードなし -> 全件表示
        choices = ["最新"] + sorted(list(month_map.keys()), reverse=True)
        return gr.update(choices=choices, value="最新")

    matched_months = []

    # キーワード検索
    for month, fpath in month_map.items():
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                if keyword in content:
                    matched_months.append(month)
        except:
            continue

    if not matched_months:
        gr.Info(f"キーワード「{keyword}」を含むログは見つかりませんでした。")
        # 0件でも空にするよりは全件表示する方が親切か？ あるいは空リストにするか。
        # ここではリストはそのまま（あるいは "該当なし" を出す？）だが、
        # フィルタ結果として空を返すと操作不能になるので、Warningを出してリセットしない、あるいは空にする。
        # 直感的には「絞り込み結果0件」を表示すべき。
        return gr.update()

    # ヒットした月 + (ヒットした中に最新月が含まれるかは不明だが、「最新」という概念はファイルではないので検索対象外)
    # だが、「最新」ログ（=現在進行中の月）も当然検索対象に含めたい。
    # 現在の月を特定して検索結果に含める必要がある。
    # しかし month_map には全ての月が含まれているはずなので、current_month がヒットしていればそれでよい。
    # UI上、「最新」という選択肢を残すかどうか。
    # 「最新」は便利ショートカットなので、検索時は具体的な月 "YYYY-MM" を指定させる形で良い。

    choices = sorted(matched_months, reverse=True)
    gr.Info(f"{len(choices)} 件のログファイルがヒットしました。")
    return gr.update(choices=choices, value=choices[0] if choices else None)


def handle_entity_selection_change(room_name: str, entity_name: str):
    """選択されたエンティティの内容を読み込む"""
    if not room_name or not entity_name:
        return "", "", gr.update()

    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    entity_id, _ = _resolve_entity_ref(em, entity_name)
    if not entity_id:
        return "読み込みに失敗しました。", "", gr.update()
    index = em.get_index()
    entities = [
        (_entity_choice_label(eid, meta.get("canonical_name", eid)), eid)
        for eid, meta in index.get("entities", {}).items()
        if isinstance(meta, dict) and meta.get("status") != "archived" and eid != entity_id
    ]
    entities.sort(key=lambda item: item[0].lower())
    content = em.read_entry_by_id(entity_id)
    meta = _format_entity_metadata(em, entity_id)
    merge_target_value = entities[0][1] if entities else None

    if content is None or content.startswith("Error:"):
        return content or "読み込みに失敗しました。", meta, gr.update(choices=entities, value=merge_target_value)

    return content, meta, gr.update(choices=entities, value=merge_target_value)


def handle_save_entity_memory(room_name: str, entity_name: str, content: str):
    """エンティティの内容を保存する"""
    if not room_name or not entity_name:
        return "", ""

    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    entity_id, _ = _resolve_entity_ref(em, entity_name)
    if entity_id:
        em.save_entry_by_id(entity_id, content, append=False, consolidate=False)
        return _format_entity_metadata(em, entity_id), json.dumps(em.get_index(), ensure_ascii=False, indent=2)
    em.create_or_update_entry(entity_name, content, append=False, consolidate=False)
    return _format_entity_metadata(em, entity_name), json.dumps(em.get_index(), ensure_ascii=False, indent=2)


def handle_delete_entity_memory(room_name: str, entity_name: str):
    """エンティティを削除する"""
    if not room_name or not entity_name:
        return gr.update(), gr.update(), gr.update(), "", ""

    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)

    entity_id, _ = _resolve_entity_ref(em, entity_name)
    if not entity_id:
        return gr.update(), gr.update(), gr.update(), "", ""
    success = em.delete_entry_by_id(entity_id)

    if success:
        gr.Info(f"エンティティ '{entity_name}' を削除しました。")
        # リストを再取得
        entities = [
            (_entity_choice_label(eid, meta.get("canonical_name", eid)), eid)
            for eid, meta in em.get_index().get("entities", {}).items()
            if isinstance(meta, dict) and meta.get("status") != "archived"
        ]
        if entities:
            entities.sort(key=lambda item: item[0].lower())
            selected = entities[0][1]
            merge_targets = [item for item in entities if item[1] != selected]
            merge_target_value = merge_targets[0][1] if merge_targets else None
            return (
                gr.update(choices=entities, value=selected),
                gr.update(choices=merge_targets, value=merge_target_value),
                em.read_entry_by_id(selected),
                _format_entity_metadata(em, selected),
                json.dumps(em.get_index(), ensure_ascii=False, indent=2),
            )
        return gr.update(choices=entities, value=None), gr.update(choices=[], value=None), "", "", json.dumps(em.get_index(), ensure_ascii=False, indent=2)
    else:
        gr.Error(f"エンティティ '{entity_name}' の削除に失敗しました。")
        return gr.update(), gr.update(), "", "", ""


def handle_dormant_entity_candidates(room_name: str, days: int = 90):
    if not room_name:
        return ""
    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    dormant = em.mark_dormant_candidates(days=days)
    if not dormant:
        return f"{days}日以上の休眠候補はありません。"
    return f"休眠化しました: {', '.join(dormant)}"


def handle_restore_entity_memory(room_name: str, entity_name: str):
    if not room_name or not entity_name:
        return "", ""
    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    entity_id, _ = _resolve_entity_ref(em, entity_name)
    if entity_id and em.set_entity_status_by_id(entity_id, "active"):
        return _format_entity_metadata(em, entity_id), json.dumps(em.get_index(), ensure_ascii=False, indent=2)
    return "復帰に失敗しました。", json.dumps(em.get_index(), ensure_ascii=False, indent=2)


def handle_mark_entity_dormant(room_name: str, entity_name: str):
    if not room_name or not entity_name:
        return "", ""
    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    entity_id, _ = _resolve_entity_ref(em, entity_name)
    if entity_id and em.set_entity_status_by_id(entity_id, "dormant"):
        return _format_entity_metadata(em, entity_id), json.dumps(em.get_index(), ensure_ascii=False, indent=2)
    return "休眠化に失敗しました。", json.dumps(em.get_index(), ensure_ascii=False, indent=2)


def handle_merge_entity_into_target(room_name: str, source_entity_name: str, target_entity_name: str, reason: str = ""):
    if not room_name or not source_entity_name or not target_entity_name:
        return gr.update(), gr.update(), "", ""
    from entity_memory_manager import EntityMemoryManager
    import config_manager
    em = EntityMemoryManager(room_name)
    index = em.get_index()
    source_id, _ = _resolve_entity_ref(em, source_entity_name)
    target_id, _ = _resolve_entity_ref(em, target_entity_name)
    if not source_id or not target_id:
        return gr.update(), gr.update(), "", "統合先または統合元が見つかりません。"
    if source_id == target_id:
        return gr.update(), gr.update(), "", "統合元と統合先が同じです。"
    merge_api_key = None
    try:
        _, merge_model_name, _ = config_manager.get_effective_internal_model("processing")
        merge_api_key = config_manager.get_active_gemini_api_key(room_name, model_name=merge_model_name)
    except Exception:
        merge_api_key = None
    result = em.merge_entities(source_id, target_id, reason=reason, api_key=merge_api_key)
    entities = [
        (_entity_choice_label(eid, meta.get("canonical_name", eid)), eid)
        for eid, meta in em.get_index().get("entities", {}).items()
        if isinstance(meta, dict) and meta.get("status") != "archived"
    ]
    entities.sort(key=lambda item: item[0].lower())
    merge_targets = [item for item in entities if item[1] != source_id]
    merge_target_value = merge_targets[0][1] if merge_targets else None
    source_value = target_id if any(eid == target_id for _, eid in entities) else next((eid for _, eid in entities if eid == source_id), None)
    return (
        gr.update(choices=entities, value=source_value),
        gr.update(choices=merge_targets, value=merge_target_value),
        _format_entity_metadata(em, target_id),
        json.dumps(em.get_index(), ensure_ascii=False, indent=2),
    )


def handle_show_entity_index(room_name: str):
    if not room_name:
        return ""
    from entity_memory_manager import EntityMemoryManager
    em = EntityMemoryManager(room_name)
    return json.dumps(em.get_index(), ensure_ascii=False, indent=2)


def _format_entity_metadata(em, entity_name: str) -> str:
    index = em.get_index()
    _, meta = em._find_meta_by_id(entity_name, index)
    if not meta:
        _, meta = em._find_meta_by_name(entity_name, index)
    if not meta:
        return ""
    public_meta = {
        "entity_id": next((eid for eid, item in index.get("entities", {}).items() if item is meta), None),
        "canonical_name": meta.get("canonical_name"),
        "status": meta.get("status"),
        "aliases": meta.get("aliases", []),
        "entity_type": meta.get("entity_type"),
        "importance": meta.get("importance"),
        "confidence": meta.get("confidence"),
        "read_count": meta.get("read_count", 0),
        "write_count": meta.get("write_count", 0),
        "recall_count": meta.get("recall_count", 0),
        "last_read_at": meta.get("last_read_at"),
        "last_written_at": meta.get("last_written_at"),
        "last_recalled_at": meta.get("last_recalled_at"),
        "merge_candidates": meta.get("merge_candidates", []),
        "related_ids": meta.get("related_ids", []),
        "merged_into": meta.get("merged_into"),
        "archived_file": meta.get("archived_file"),
    }
    return json.dumps(public_meta, ensure_ascii=False, indent=2)


def handle_refresh_episodic_entries(room_name: str):
    """エピソード記憶（episodic_memory.json）を読み込み、Dropdown の選択肢とフィルタの選択肢を返す"""
    if not room_name:
        return gr.update(), gr.update(value="日付を選択してください"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて")

    try:
        manager = EpisodicMemoryManager(room_name)
        data = manager._load_memory()

        if not data:
            return gr.update(), gr.update(value="エピソード記憶がまだ作成されていません。"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて")

        # 日付リスト（最新順）- 重複を排除
        entries_set = set()
        years = set()
        months = set()

        for item in data:
            d = item.get('date', '').strip()
            if not d: continue

            entries_set.add(d)

            # 年・月抽出 (YYYY-MM-DD or YYYY-MM-DD~YYYY-MM-DD)
            # 範囲の場合は開始日を使う
            base_date = d.split('~')[0].split('～')[0].strip()
            if len(base_date) >= 7:
                years.add(base_date[:4])
                months.add(base_date[5:7])

        entries = sorted(list(entries_set), reverse=True)
        year_choices = ["すべて"] + sorted(list(years), reverse=True)
        month_choices = ["すべて"] + sorted(list(months))

        return (
            gr.update(choices=entries, value=None),
            gr.update(value="日付を選択すると、ここに内容が表示されます。"),
            gr.update(choices=year_choices, value="すべて"),
            gr.update(choices=month_choices, value="すべて")
        )
    except Exception as e:
        print(f"Error refreshing episodic entries: {e}")
        return gr.update(), gr.update(value=f"読み込みエラー: {e}"), gr.update(choices=["すべて"], value="すべて"), gr.update(choices=["すべて"], value="すべて")


def handle_episodic_filter_change(room_name: str, year: str, month: str):
    """年・月のフィルタ変更に合わせて、エピソードドロップダウンの選択肢を絞り込む"""
    if not room_name:
        return gr.update()

    try:
        manager = EpisodicMemoryManager(room_name)
        data = manager._load_memory()

        filtered_entries_set = set()
        for item in data:
            d = item.get('date', '').strip()
            if not d: continue

            # 判定用日付（範囲なら開始日）
            base_date = d.split('~')[0].split('～')[0].strip()

            match_year = (year == "すべて" or base_date.startswith(year))
            match_month = (month == "すべて" or (len(base_date) >= 7 and base_date[5:7] == month))

            if match_year and match_month:
                filtered_entries_set.add(d)

        filtered_entries = sorted(list(filtered_entries_set), reverse=True)
        return gr.update(choices=filtered_entries, value=None)
    except Exception as e:
        print(f"Error filtering episodic entries: {e}")
        return gr.update()


def handle_episodic_selection_from_dropdown(room_name: str, selected_date: str):
    """エピソードのドロップダウンから選択した際、詳細を表示する"""
    if not room_name or not selected_date:
        return ""

    try:
        manager = EpisodicMemoryManager(room_name)
        data = manager._load_memory()

        # 同じ日付の全エピソードを収集
        matching_episodes = []
        for item in data:
            if item.get('date', '').strip() == selected_date.strip():
                matching_episodes.append(item)

        if not matching_episodes:
            return "選択されたエピソードが見つかりませんでした。"

        # created_at順でソート（古いものが先）
        matching_episodes.sort(key=lambda x: x.get('created_at', ''))

        # 全エピソードを表示
        all_details = []

        # 複数エピソードがある場合は冒頭に案内を追加
        if len(matching_episodes) > 1:
            header = f"📌 この日には {len(matching_episodes)} 件のエピソードがあります（作成順に表示）\n"
            header += "=" * 50 + "\n\n"
            all_details.append(header)

        for idx, item in enumerate(matching_episodes, 1):
            summary_raw = item.get('summary', '')

            # [Type Safety] summary が list や dict の場合にテキストを抽出
            if isinstance(summary_raw, list):
                text_parts = []
                for p in summary_raw:
                    if isinstance(p, str): text_parts.append(p)
                    elif isinstance(p, dict) and "text" in p: text_parts.append(p["text"])
                    else: text_parts.append(str(p))
                summary = "\n".join(text_parts)
            elif isinstance(summary_raw, dict) and "text" in summary_raw:
                summary = summary_raw["text"]
            else:
                summary = str(summary_raw)

            created_at = item.get('created_at', '不明')
            episode_type = item.get('type', '日次要約')

            # タイプのラベル変換
            type_labels = {
                'achievement': '🏆 目標達成',
                'bonding': '💕 絆確認',
                'discovery': '💡 発見'
            }
            type_label = type_labels.get(episode_type, '📝 日次要約')

            details = f"【{type_label}】\n"
            details += f"【日付】 {selected_date}\n"
            details += f"【記録日時】 {created_at}\n"
            if item.get('compressed'):
                details += f"【種別】 統合済みエピソード（元ログ数: {item.get('original_count', '?')}）\n"
            details += "-" * 30 + "\n\n"
            details += summary
            all_details.append(details)

        # 複数ある場合は区切り線で分離
        separator = "\n\n" + "=" * 50 + "\n\n"
        return separator.join(all_details)

    except Exception as e:
        return f"エピソード表示エラー: {e}"


def _get_working_memory_path(room_name: str, slot_name: str = None) -> str:
    """ワーキングメモリスロットのパスを取得"""
    if not room_name: return ""
    wm_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME, constants.WORKING_MEMORY_DIR_NAME)
    os.makedirs(wm_dir, exist_ok=True)
    # 非文字列(bool等)が渡ってきても落ちないよう、文字列でなければアクティブスロットへフォールバック
    if not slot_name or not isinstance(slot_name, str):
        slot_name = room_manager.get_active_working_memory_slot(room_name)
    if not slot_name.endswith(constants.WORKING_MEMORY_EXTENSION):
        slot_name += constants.WORKING_MEMORY_EXTENSION
    return os.path.join(wm_dir, slot_name)


def load_working_memory_content(room_name: str, slot_name: str = None) -> str:
    """ワーキングメモリの内容を読み込む"""
    if not room_name: return ""
    path = _get_working_memory_path(room_name, slot_name)
    if os.path.exists(path):
        return safe_text_read(path)
    return ""


def load_working_memory_slots(room_name: str) -> tuple[list[str], str]:
    """ワーキングメモリスロット一覧と現在のアクティブスロットを返す"""
    if not room_name: return ([], constants.WORKING_MEMORY_DEFAULT_SLOT)
    wm_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME, constants.WORKING_MEMORY_DIR_NAME)
    os.makedirs(wm_dir, exist_ok=True)

    slots = [f.replace(constants.WORKING_MEMORY_EXTENSION, '') for f in os.listdir(wm_dir) if f.endswith(constants.WORKING_MEMORY_EXTENSION)]
    active_slot = room_manager.get_active_working_memory_slot(room_name)

    if active_slot not in slots:
        slots.append(active_slot)

    return (slots, active_slot)


def handle_working_memory_slot_change(room_name: str, selected_slot: str) -> tuple[str, str]:
    """UIからのスロット切り替え要求を処理し、指定スロットの内容とアクティブ状態ラベルを返す"""
    if not room_name or not selected_slot: return "", ""
    room_manager.set_active_working_memory_slot(room_name, selected_slot)
    content = load_working_memory_content(room_name, selected_slot)
    
    character_name = room_manager.get_character_name(room_name)
    active_label = f"現在 {character_name} が使用中のスロット: **{selected_slot}**"
    return content, active_label


def handle_new_working_memory_slot(room_name: str) -> tuple:
    """新しいワーキングメモリスロットの作成（UI上での仮追加と保存による実体化の準備）"""
    if not room_name:
        return (gr.update(), gr.update(), "")

    import datetime
    new_slot = f"new_topic_{datetime.datetime.now().strftime('%M%S')}"

    # スロット一覧を再取得してから追加して選択状態に
    current_slots, _ = load_working_memory_slots(room_name)
    new_slots = current_slots + [new_slot]

    character_name = room_manager.get_character_name(room_name)
    active_label = f"現在 {character_name} が使用中のスロット: **{new_slot}** (未保存)"

    # 選択すると自動で handle_working_memory_slot_change をトリガーして
    # アクティブスロットとして記録される想定
    return (gr.update(choices=new_slots, value=new_slot), "", active_label)


def handle_action_memory_refresh(room_name: str) -> str:
    """
    指定ルームの今日のアクションログを取得し、UI表示用にフォーマットして返す。
    """
    if not room_name:
        return "ルームが選択されていません。"

    try:
        import action_logger
        import datetime
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        actions = action_logger.get_actions_by_date(room_name, today_str)

        if not actions:
            return "本日のアクション記録はありません。"

        lines = []
        # 最新のものが下に来るように、または上に来るように（ここでは新しい順に表示する場合は reversed を使うなど）
        # 時系列順（古い順）の方が見やすいのでそのまま
        for act in actions:
            t = act.get("time", "")
            tool = act.get("tool_name", "")
            res = str(act.get("result_summary", ""))[:150].replace("\n", " ")
            lines.append(f"[{t}] {tool}: {res}")

        return "\n".join(lines)
    except Exception as e:
        print(f"--- [Action Memory] 履歴の取得に失敗: {e} ---")
        return f"履歴の取得に失敗しました: {e}"


def handle_save_working_memory(room_name: str, content: str, slot_name: str = None) -> str:
    """ワーキングメモリを保存"""
    if content is None or str(content).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、データ保護のために保存を中止しました。")
        return content

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return content

    path = _get_working_memory_path(room_name, slot_name)
    try:
        # バックアップ作成 (独自のバックアップディレクトリへ)
        backup_dir = os.path.join(constants.ROOMS_DIR, room_name, "backups", "working_memories")
        os.makedirs(backup_dir, exist_ok=True)
        if os.path.exists(path):
            import datetime, shutil
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            target_slot = slot_name if slot_name else room_manager.get_active_working_memory_slot(room_name)
            backup_filename = f"{timestamp}_{target_slot}{constants.WORKING_MEMORY_EXTENSION}.bak"
            shutil.copy2(path, os.path.join(backup_dir, backup_filename))

        safe_text_write(path, content)

        # 新規作成等でアクティブでなかった場合はアクティブにする
        if slot_name:
            room_manager.set_active_working_memory_slot(room_name, slot_name)

        gr.Info(f"「{room_name}」の話題「{slot_name or room_manager.get_active_working_memory_slot(room_name)}」を保存しました。")
        return content
    except Exception as e:
        gr.Error(f"ワーキングメモリの保存エラー: {e}")
        return content


def handle_memos_batch_import(room_name: str, console_content: str):
    """
    【v3: 最終FIX版】
    知識グラフの構築を、2段階のサブプロセスとして、堅牢に実行する。
    いかなる状況でも、UIがフリーズしないことを保証する。
    """
    # UIコンポーネントの数をハードコードするのではなく、動的に取得するか、
    # 確実な数（今回は6）を返すようにする。
    NUM_OUTPUTS = 6

    # 処理中のUI更新を定義
    # ★★★ あなたの好みに合わせてテキストを修正 ★★★
    yield (
        gr.update(value="知識グラフ構築中...", interactive=False), # Button
        gr.update(visible=True), # Stop Button (今回は実装しないが将来のため)
        None, # Process State
        console_content, # Console State
        console_content, # Console Output
        gr.update(interactive=False)  # Chat Input
    )

    full_log_output = console_content
    script_path_1 = "batch_importer.py"
    script_path_2 = "soul_injector.py"

    try:
        # --- ステージ1: 骨格の作成 ---
        gr.Info("ステージ1/2: 知識グラフの骨格を作成しています...")

        # ▼▼▼【ここからが修正箇所】▼▼▼
        # text=True を削除し、stdoutを直接扱う
        proc1 = subprocess.run(
            [sys.executable, "-X", "utf8", script_path_1, room_name],
            capture_output=True
        )
        # バイトストリームを、エラーを無視して強制的にデコードする
        output_log = proc1.stdout.decode('utf-8', errors='replace')
        error_log = proc1.stderr.decode('utf-8', errors='replace')
        log_chunk = f"\n--- [{script_path_1} Output] ---\n{output_log}\n{error_log}"
        # ▲▲▲【修正ここまで】▲▲▲

        full_log_output += log_chunk
        yield (
            gr.update(), gr.update(), None,
            full_log_output, full_log_output, gr.update()
        )

        if proc1.returncode != 0:
            raise RuntimeError(f"{script_path_1} failed with return code {proc1.returncode}")

        gr.Info("ステージ1/2: 骨格の作成に成功しました。")

        # --- ステージ2: 魂の注入 ---
        # ★★★ あなたの好みに合わせてテキストを修正 ★★★
        gr.Info("ステージ2/2: 知識グラフを構築中です...")

        # ▼▼▼【ここからが修正箇所】▼▼▼
        proc2 = subprocess.run(
            [sys.executable, "-X", "utf8", script_path_2, room_name],
            capture_output=True
        )
        output_log = proc2.stdout.decode('utf-8', errors='replace')
        error_log = proc2.stderr.decode('utf-8', errors='replace')
        log_chunk = f"\n--- [{script_path_2} Output] ---\n{output_log}\n{error_log}"
        # ▲▲▲【修正ここまで】▲▲▲
        full_log_output += log_chunk
        yield (
            gr.update(), gr.update(), None,
            full_log_output, full_log_output, gr.update()
        )

        if proc2.returncode != 0:
            raise RuntimeError(f"{script_path_2} failed with return code {proc2.returncode}")

        gr.Info("✅ 知識グラフの構築が、正常に完了しました！")

    except Exception as e:
        error_message = f"知識グラフの構築中にエラーが発生しました: {e}"
        logging.error(error_message)
        logging.error(traceback.format_exc())
        gr.Error(error_message)

    finally:
        # --- 最終処理: UIを必ず元の状態に戻す ---
        yield (
            gr.update(value="知識グラフを構築/更新する", interactive=True), # Button
            gr.update(visible=False), # Stop Button
            None, # Process State
            full_log_output, # Console State
            full_log_output, # Console Output
            gr.update(interactive=True) # Chat Input
        )


def handle_core_memory_update_click(room_name: str, api_key_name: str):
    """
    コアメモリの更新を同期的に実行し、完了後にUIのテキストエリアを更新する。
    """
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        return gr.update() # 何も更新しない

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Warning(f"APIキー '{api_key_name}' が有効ではありません。")
        return gr.update()

    gr.Info(f"「{room_name}」のコアメモリ更新を開始しました...")
    try:
        from tools import memory_tools
        result = memory_tools.summarize_and_update_core_memory.func(room_name=room_name, api_key=api_key)

        if "成功" in result:
            gr.Info(f"✅ コアメモリの更新が正常に完了しました。")
            # 成功した場合、更新された内容を読み込んで返す
            updated_content = load_core_memory_content(room_name)
            return gr.update(value=updated_content)
        else:
            gr.Error(f"コアメモリの更新に失敗しました。詳細: {result}")
            return gr.update() # 失敗時はUIを更新しない

    except Exception as e:
        gr.Error(f"コアメモリ更新中に予期せぬエラーが発生しました: {e}")
        traceback.print_exc()
        return gr.update()


def handle_reflect_identity_to_core(room_name: str):
    """
    永続記憶(memory_identity.txt)の内容をコアメモリ(core_memory.txt)の該当セクションにのみ反映する。
    日記の再要約は行わない軽量な処理。
    """
    if not room_name:
        gr.Warning("ルームを選択してください。")
        return gr.update()

    try:
        # パス取得
        from room_manager import get_room_files_paths
        _, _, _, memory_identity_path, _, _, _ = get_room_files_paths(room_name)
        core_memory_path = os.path.join(constants.ROOMS_DIR, room_name, "core_memory.txt")

        if not os.path.exists(memory_identity_path):
            gr.Error("永続記憶ファイルが見つかりません。")
            return gr.update()

        # 永続記憶の読み込み
        with open(memory_identity_path, 'r', encoding='utf-8') as f:
            identity_content = f.read().strip()

        # コアメモリの読み込みと差し替え
        if os.path.exists(core_memory_path):
            with open(core_memory_path, 'r', encoding='utf-8') as f:
                core_content = f.read()

            # セクション分割
            # 正規表現でセクションを特定して置換
            # セクション末尾は次のセクションの開始かファイル末尾
            pattern = r"(--- \[永続記憶 \(Permanent\) - 要約せずそのまま記載\] ---\n)(.*?)(\n--- \[|$)"
            if re.search(pattern, core_content, re.DOTALL):
                # 既存セクションを置換
                new_core_content = re.sub(pattern, rf"\1{identity_content}\3", core_content, flags=re.DOTALL)
            else:
                # セクションが見つからない場合は先頭に追加
                new_core_content = f"--- [永続記憶 (Permanent) - 要約せずそのまま記載] ---\n{identity_content}\n\n" + core_content
        else:
            # 新規作成（標準的なヘッダーを付与）
            new_core_content = (
                f"--- [永続記憶 (Permanent) - 要約せずそのまま記載] ---\n{identity_content}\n\n"
                f"--- [日記 (Diary) - AIによる要約] ---\n（日記の記録はまだありません）"
            )

        # 保存
        with open(core_memory_path, 'w', encoding='utf-8') as f:
            f.write(new_core_content.strip() + "\n")

        gr.Info(f"✅ 「{room_name}」の永続記憶をコアメモリに反映しました。")

        # UI更新（反映後の内容を返す）
        return gr.update(value=new_core_content)

    except Exception as e:
        gr.Error(f"反映中にエラーが発生しました: {e}")
        traceback.print_exc()
        return gr.update()


def _get_rag_index_last_updated(room_name: str, index_type: str = "memory") -> str:
    """指定された索引の最終更新日時を取得する"""
    from pathlib import Path
    import datetime

    if index_type == "memory":
        index_path = Path("characters") / room_name / "rag_data" / "faiss_index_static"
    elif index_type == "current_log":
        index_path = Path("characters") / room_name / "rag_data" / "current_log_index"
    else:
        return "不明"

    if not index_path.exists():
        return "未作成"

    try:
        # フォルダの最終更新時刻を取得
        mtime = index_path.stat().st_mtime
        dt = datetime.datetime.fromtimestamp(mtime)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "取得失敗"


def handle_sleep_consolidation_change(room_name: str, update_episodic: bool, update_memory_index: bool, update_current_log: bool, update_entity: bool = True, compress_episodes: bool = False):
    """睡眠時記憶整理設定を即座に保存する"""
    if not room_name:
        return

    try:
        updates = {
            "sleep_consolidation": {
                "update_episodic_memory": bool(update_episodic),
                "update_memory_index": bool(update_memory_index),
                "update_current_log_index": bool(update_current_log),
                "update_entity_memory": bool(update_entity),
                "compress_old_episodes": bool(compress_episodes)
            }
        }
        room_manager.update_room_config(room_name, updates)
        # print(f"--- [睡眠時記憶整理] 設定保存: {room_name} ---")
    except Exception as e:
        print(f"--- [睡眠時記憶整理] 設定保存エラー: {e} ---")


def handle_compress_episodes(room_name: str, api_key_name: str):
    """エピソード記憶を手動で圧縮する"""
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        return "エラー: ルームとAPIキーを選択してください。"

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        return "エラー: APIキーが無効です。"

    try:
        manager = EpisodicMemoryManager(room_name)
        result = manager.compress_old_episodes(api_key)

        # 実行後の最新統計を取得してステータス文字列を更新
        stats = manager.get_compression_stats()
        last_date = stats["last_compressed_date"] or "なし"
        pending = stats["pending_count"]
        full_status = f"{last_date}まで圧縮済み (対象: {pending}件) | 最終: {result}"

        # 最終実行結果を room_config.json に保存
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        config = {}
        if os.path.exists(room_config_path):
            with open(room_config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        config["last_compression_result"] = result
        with open(room_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        gr.Info(f"✅ {result}")
        return full_status
    except Exception as e:
        error_msg = f"圧縮中にエラーが発生しました: {e}"
        gr.Error(error_msg)
        traceback.print_exc()
        return error_msg


def handle_embedding_mode_change(room_name: str, embedding_mode: str):
    """エンベディングモード設定を保存する"""
    if not room_name:
        return

    try:
        room_manager.update_room_config(room_name, {"embedding_mode": embedding_mode})

        mode_name = "ローカル" if embedding_mode == "local" else "Gemini API"
        gr.Info(f"📌 エンベディングモードを「{mode_name}」に変更しました。次回の索引更新から適用されます。")
        print(f"--- [Embedding Mode] {room_name}: {embedding_mode} ---")
    except Exception as e:
        print(f"--- [Embedding Mode] 設定保存エラー: {e} ---")


def handle_memory_reindex(room_name: str, api_key_name: str):
    """記憶の索引（過去ログ、エピソード記憶、夢日記、日記ファイル）を更新する（リアルタイム進捗表示付き）。"""
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        yield gr.update(), gr.update()
        return

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        yield gr.update(), gr.update()
        return

    yield "開始中...", gr.update(interactive=False)

    try:
        manager = rag_manager.RAGManager(room_name, api_key)

        last_message = ""
        for current_step, total_steps, status_message in manager.update_memory_index_with_progress():
            last_message = status_message
            yield f"{status_message}", gr.update(interactive=False)

        gr.Info(f"✅ {last_message}")
        last_updated = _get_rag_index_last_updated(room_name, "memory")
        yield f"{last_message}（最終更新: {last_updated}）", gr.update(interactive=True)

    except Exception as e:
        error_msg = f"記憶索引の作成中にエラーが発生しました: {e}"
        gr.Error(error_msg)
        print(f"--- [記憶索引作成エラー] ---")
        traceback.print_exc()
        yield error_msg, gr.update(interactive=True)
        return


def handle_full_reindex(room_name: str, api_key_name: str):
    """すべての索引を削除し、現在のモデル設定で完全に作成し直す（リアルタイム進捗表示付き）。"""
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        yield gr.update(), gr.update()
        return

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        yield gr.update(), gr.update()
        return

    yield "インデックス消去中...", gr.update(interactive=False)

    try:
        manager = rag_manager.RAGManager(room_name, api_key)

        last_message = ""
        # manager.rebuild_all_indices は内部で進捗を callback で報告するように作る（または generator 化する）
        # 現状の update_memory_index_with_progress 方式を流用するため、直接 rebuild メソッドを generator として定義するか、
        # rebuild メソッド内で yield させる。

        # 修正: rebuild_all_indices を generator 化するのは大変なので、まず消去して、そのあと通常の進捗付きを呼ぶ
        def status_callback(msg):
            nonlocal last_message
            last_message = msg

        # 索引消去 & 再構築 (これは rag_manager のメソッド)
        # ※ rebuild_all_indices が generator でない場合は、こちらで yield する。
        # rag_manager に追加した rebuild_all_indices は generator ではないため、
        # update_memory_index_with_progress 等を直接呼ぶようにここで展開するか、
        # rag_manager 側を yield 対応にする。

        # 面倒なのでここで「消去」を行ってから handle_memory_reindex のロジックを呼ぶ
        manager.rebuild_all_indices(status_callback=lambda m: print(f"[Rebuild Status] {m}"))

        # 上記で消去済みなので、改めて進捗付きで実行
        for current_step, total_steps, status_message in manager.update_memory_index_with_progress():
            last_message = status_message
            yield f"再構築中: {status_message}", gr.update(interactive=False)

        # 続いて現行ログの索引も更新する
        yield f"現行ログの索引を更新中...", gr.update(interactive=False)
        for b_num, b_total, status_msg in manager.update_current_log_index_with_progress():
            last_message = status_msg
            yield f"現行ログ更新: {status_msg}", gr.update(interactive=False)

        gr.Info(f"✅ インデックス（記憶・現行ログ）の完全再構築が完了しました")
        last_updated = _get_rag_index_last_updated(room_name, "memory")
        yield f"再構築完了（最終更新: {last_updated}）", gr.update(interactive=True)

    except Exception as e:
        error_msg = f"再構築中にエラーが発生しました: {e}"
        gr.Error(error_msg)
        traceback.print_exc()
        yield error_msg, gr.update(interactive=True)
        return


def handle_current_log_reindex(room_name: str, api_key_name: str):
    """現行ログ（log.txt）の索引を更新する（リアルタイム進捗表示付き）。"""
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        yield gr.update(), gr.update()
        return

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        yield gr.update(), gr.update()
        return

    yield "開始中...", gr.update(interactive=False)

    try:
        manager = rag_manager.RAGManager(room_name, api_key)

        last_message = ""
        for batch_num, total_batches, status_message in manager.update_current_log_index_with_progress():
            last_message = status_message
            yield f"{status_message}", gr.update(interactive=False)

        gr.Info(f"✅ {last_message}")
        last_updated = _get_rag_index_last_updated(room_name, "current_log")
        yield f"{last_message}（最終更新: {last_updated}）", gr.update(interactive=True)

    except Exception as e:
        error_msg = f"現行ログ索引の作成中にエラーが発生しました: {e}"
        gr.Error(error_msg)
        print(f"--- [現行ログ索引作成エラー] ---")
        traceback.print_exc()
        yield error_msg, gr.update(interactive=True)
        return


def handle_refresh_goals(room_name: str):
    """
    目標を読み込んで表示用テキストを生成する。

    Returns:
        (short_term_text, long_term_text, meta_text)
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return "", "", ""

    try:
        import goal_manager
        gm = goal_manager.GoalManager(room_name)
        goals = gm._load_goals()  # get_goals → _load_goals

        # 短期目標
        short_term = goals.get("short_term", [])
        short_lines = []
        for g in short_term:
            status_icon = "✅" if g.get("status") == "completed" else "🎯"
            short_lines.append(f"{status_icon} {g.get('goal', '（目標なし）')} [優先度: {g.get('priority', 1)}]")
        short_text = "\n".join(short_lines) if short_lines else "短期目標はありません"

        # 長期目標
        long_term = goals.get("long_term", [])
        long_lines = []
        for g in long_term:
            status_icon = "✅" if g.get("status") == "completed" else "🌟"
            long_lines.append(f"{status_icon} {g.get('goal', '（目標なし）')}")
        long_text = "\n".join(long_lines) if long_lines else "長期目標はありません"

        # メタデータ
        meta = goals.get("meta", {})
        level = meta.get("last_reflection_level", 1)
        level2_date = meta.get("last_level2_date", "未実施")
        level3_date = meta.get("last_level3_date", "未実施")
        meta_text = f"最終省察レベル: {level} | 週次省察: {level2_date} | 月次省察: {level3_date}"

        return short_text, long_text, meta_text

    except Exception as e:
        print(f"Refresh Goals Error: {e}")
        traceback.print_exc()
        return "エラー", "エラー", str(e)
