"""ui_handlers のうち「ナレッジ（RAG用知識ファイル）」ドメイン。

ui_handlers パッケージから再エクスポートされ、呼び出し側は従来どおり
ui_handlers.<関数名> でアクセスできる。
"""

from typing import Optional, Tuple, List, Dict, Union, Any
from pathlib import Path
import datetime
import html
import shutil
import time
import traceback
import gradio as gr
import gemini_api, config_manager, alarm_manager, room_manager, utils, constants, chatgpt_importer, claude_importer, generic_importer
import rag_manager


def _get_knowledge_files(room_name: str) -> List[Dict[str, str]]:
    """指定されたルームのknowledgeフォルダ内のファイル情報をリストで取得する。"""
    knowledge_dir = Path(constants.ROOMS_DIR) / room_name / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    files_info = []
    for file_path in knowledge_dir.iterdir():
        if file_path.is_file():
            stat = file_path.stat()
            files_info.append({
                "ファイル名": file_path.name,
                "サイズ (KB)": f"{stat.st_size / 1024:.2f}",
                "最終更新日時": datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            })
    # ファイル名でソートして返す
    files_info = sorted(files_info, key=lambda x: x["ファイル名"])
    return files_info


def _render_knowledge_files_table(files_info: List[Dict[str, str]]) -> str:
    """知識ファイル一覧を軽量なHTMLテーブルとして描画する。"""
    if not files_info:
        return "<p class='info-text'>知識ファイルはまだありません。</p>"

    rows = []
    for item in files_info:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.get('ファイル名', ''))}</td>"
            f"<td>{html.escape(item.get('サイズ (KB)', ''))}</td>"
            f"<td>{html.escape(item.get('最終更新日時', ''))}</td>"
            "</tr>"
        )

    return (
        "<table class='knowledge-file-table'>"
        "<thead><tr><th>ファイル名</th><th>サイズ (KB)</th><th>最終更新日時</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _knowledge_file_selector_update(
    files_info: List[Dict[str, str]],
    selected_filename: Optional[str] = None,
):
    """知識ファイル選択Dropdownの更新値を返す。"""
    choices = [item["ファイル名"] for item in files_info]
    value = selected_filename if selected_filename in choices else None
    return gr.update(choices=choices, value=value, interactive=bool(choices))


def _get_knowledge_status(room_name: str) -> str:
    """知識ベースの現在の状態（索引の有無など）を示す文字列を返す。"""
    base_dir = Path(constants.ROOMS_DIR) / room_name / "rag_data"
    static_index = base_dir / "faiss_index_static"
    dynamic_index = base_dir / "faiss_index_dynamic"
    legacy_index = base_dir / "faiss_index"  # レガシーパス（配布版デフォルト）

    # タブ表示時は軽量性を優先し、ディレクトリ走査ではなく代表ファイルの存在だけを見る。
    index_paths = (static_index, dynamic_index, legacy_index)
    is_created = any(
        index_path.exists()
        and (
            (index_path / "index.faiss").exists()
            or (index_path / "index.pkl").exists()
            or (index_path / "docstore.json").exists()
        )
        for index_path in index_paths
    )

    if is_created:
        return "✅ 索引は作成済みです。（知識ベースやログが更新された場合は、再構築ボタンを押してください）"
    else:
        return "⚠️ 索引がまだ作成されていません。「索引を作成 / 更新」ボタンを押してください。"


def handle_knowledge_tab_load(room_name: str):
    """「知識」タブが選択されたときの初期化処理。"""
    perf_start = time.time()

    if not room_name:
        return _render_knowledge_files_table([]), _knowledge_file_selector_update([]), "ルームが選択されていません。"

    try:
        print(f"--- [PERF] handle_knowledge_tab_load start for room: {room_name} ---")

        files_info = _get_knowledge_files(room_name)
        print(f"  - _get_knowledge_files finished (Files: {len(files_info)})")

        status_text = _get_knowledge_status(room_name)
        print(f"--- [PERF] handle_knowledge_tab_load finished. Total: {time.time() - perf_start:.4f}s ---")

        return (
            _render_knowledge_files_table(files_info),
            _knowledge_file_selector_update(files_info),
            gr.update(value=str(status_text)),
        )

    except Exception as e:
        print(f"  - [CRITICAL ERROR] handle_knowledge_tab_load: {e}")
        traceback.print_exc()
        return (
            _render_knowledge_files_table([]),
            _knowledge_file_selector_update([]),
            gr.update(value=f"⚠️ 読み込み中にエラーが発生しました: {e}"),
        )


def handle_knowledge_file_upload(room_name: str, files: List[Any]):
    """知識ベースにファイルをアップロードする処理。"""
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update(), gr.update()
    if not files:
        return gr.update(), gr.update(), gr.update()

    knowledge_dir = Path(constants.ROOMS_DIR) / room_name / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    for temp_file in files:
        original_filename = Path(temp_file.name).name
        target_path = knowledge_dir / original_filename
        shutil.move(temp_file.name, str(target_path))
        print(f"--- [Knowledge] ファイルをアップロードしました: {target_path} ---")

    gr.Info(f"{len(files)}個のファイルを知識ベースに追加しました。索引の更新が必要です。")

    files_info = _get_knowledge_files(room_name)
    return (
        _render_knowledge_files_table(files_info),
        _knowledge_file_selector_update(files_info),
        "⚠️ 索引の更新が必要です。「索引を作成 / 更新」ボタンを押してください。",
    )


def handle_knowledge_file_delete(room_name: str, selected_filename: Optional[str]):
    """選択された知識ベースのファイルを削除する処理。"""

    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update(), gr.update()

    if not selected_filename:
        gr.Warning("削除するファイルをリストから選択してください。")
        return gr.update(), gr.update(), gr.update()

    try:
        current_files = _get_knowledge_files(room_name)
        filenames = {item["ファイル名"] for item in current_files}
        if selected_filename not in filenames:
            gr.Error("選択されたファイルが見つかりません。リストが古い可能性があります。")
            return (
                _render_knowledge_files_table(current_files),
                _knowledge_file_selector_update(current_files),
                _get_knowledge_status(room_name),
            )

        file_path_to_delete = Path(constants.ROOMS_DIR) / room_name / "knowledge" / selected_filename

        if file_path_to_delete.exists():
            file_path_to_delete.unlink()
            gr.Info(f"ファイル「{selected_filename}」を削除しました。索引の更新が必要です。")
        else:
            gr.Warning(f"ファイル「{selected_filename}」が見つかりませんでした。")

    except (OSError, KeyError) as e:
        gr.Error(f"ファイルの特定に失敗しました: {e}")

    # 処理後、再度ファイルリストを読み込んでUIを更新
    updated_files = _get_knowledge_files(room_name)
    return (
        _render_knowledge_files_table(updated_files),
        _knowledge_file_selector_update(updated_files),
        "⚠️ 索引の更新が必要です。「索引を作成 / 更新」ボタンを押してください。",
    )


def handle_knowledge_reindex(room_name: str, api_key_name: str):
    """知識ベースの索引を作成/更新する。RAGManagerを使用。"""
    if not room_name or not api_key_name:
        gr.Warning("ルームとAPIキーを選択してください。")
        yield gr.update(), gr.update()
        return

    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        gr.Error(f"APIキー「{api_key_name}」が無効です。")
        yield gr.update(), gr.update()
        return

    # 処理開始を通知
    yield "処理中: 知識ドキュメントのインデックスを構築しています...", gr.update(interactive=False)

    try:
        manager = rag_manager.RAGManager(room_name, api_key)
        # 知識索引のみ更新
        result_message = manager.update_knowledge_index()

        gr.Info(f"✅ {result_message}")
        yield f"ステータス: {result_message}", gr.update(interactive=True)

    except Exception as e:
        error_msg = f"索引の作成中にエラーが発生しました: {e}"
        gr.Error(error_msg)
        print(f"--- [知識索引作成エラー] ---")
        traceback.print_exc()
        yield error_msg, gr.update(interactive=True)
        return

    yield _get_knowledge_status(room_name), gr.update(interactive=True)
