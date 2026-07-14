"""ui_handlers のうち「スキル（手順・プレイブック編集）」ドメイン。

ui_handlers パッケージから再エクスポートされ、呼び出し側は従来どおり
ui_handlers.<関数名> でアクセスできる。
"""

from typing import Optional, Tuple, List, Dict, Union, Any
import html
import traceback
import gradio as gr


def _procedure_ref(item: Dict[str, str]) -> str:
    return f"{item.get('scope', 'private')}:{item.get('procedure_id', '')}"


def _render_skill_table(procedures: List[Dict[str, str]]) -> str:
    if not procedures:
        return "<p class='info-text'>Skillsはまだありません。「一覧を更新」または「新規テンプレート」で作成できます。</p>"

    rows = []
    for item in procedures:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.get('scope', 'private'))}</td>"
            f"<td>{html.escape(item.get('procedure_id', ''))}</td>"
            f"<td>{html.escape(item.get('title', ''))}</td>"
            "</tr>"
        )
    return (
        "<table class='knowledge-file-table skill-list-table'>"
        "<thead><tr><th>Scope</th><th>ID</th><th>Title</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _skill_selector_update(
    procedures: List[Dict[str, str]],
    selected_ref: Optional[str] = None,
):
    choices = [
        (f"[{item.get('scope', 'private')}] {item.get('procedure_id')}: {item.get('title')}", _procedure_ref(item))
        for item in procedures
    ]
    values = {value for _, value in choices}
    value = selected_ref if selected_ref in values else None
    return gr.update(choices=choices, value=value, interactive=bool(choices))


def _load_skills(room_name: str) -> List[Dict[str, str]]:
    from procedure_memory_manager import ProcedureMemoryManager

    if not room_name:
        return []
    return ProcedureMemoryManager(room_name).list_procedures(include_shared=True)


def _split_skill_ref(skill_ref: str) -> Tuple[str, str]:
    raw = str(skill_ref or "").strip()
    if ":" in raw:
        scope, procedure_id = raw.split(":", 1)
        scope = "shared" if scope == "shared" else "private"
        return scope, procedure_id
    return "private", raw


def handle_skills_refresh(room_name: str):
    if not room_name:
        return _render_skill_table([]), _skill_selector_update([]), "ルームが選択されていません。"
    try:
        procedures = _load_skills(room_name)
        return (
            _render_skill_table(procedures),
            _skill_selector_update(procedures),
            f"Skillsを読み込みました（{len(procedures)}件）。",
        )
    except Exception as e:
        traceback.print_exc()
        return _render_skill_table([]), _skill_selector_update([]), f"Skills一覧の読み込みエラー: {e}"


def handle_skill_select(room_name: str, skill_ref: str):
    if not room_name or not skill_ref:
        return gr.update(), gr.update(), gr.update(), "Skillを選択してください。"
    try:
        from procedure_memory_manager import ProcedureMemoryManager

        scope, procedure_id = _split_skill_ref(skill_ref)
        content = ProcedureMemoryManager(room_name).read_procedure(f"{scope}:{procedure_id}")
        return (
            gr.update(value=scope),
            gr.update(value=procedure_id),
            gr.update(value=content),
            f"Skill '{scope}:{procedure_id}' を読み込みました。",
        )
    except Exception as e:
        traceback.print_exc()
        return gr.update(), gr.update(), gr.update(), f"Skill読み込みエラー: {e}"


def handle_skill_new_template(scope: str = "private"):
    scope = "shared" if str(scope or "").strip() == "shared" else "private"
    template = (
        "# 新しいSkill\n\n"
        "## Metadata\n"
        "- procedure_id: new_skill\n"
        f"- scope: {scope}\n"
        "- created_at: \n"
        "- updated_at: \n"
        "- source_timeline_id: \n\n"
        "## Purpose\n"
        "このSkillの目的を書く。\n\n"
        "## Triggers\n"
        "- このSkillを使うきっかけを書く\n\n"
        "## Steps\n"
        "1. 最初の手順を書く\n"
        "2. 次の手順を書く\n\n"
        "## Success Criteria\n"
        "成功条件を書く。\n\n"
        "## Notes\n"
        "補足を書く。\n"
    )
    return (
        gr.update(value=scope),
        gr.update(value="new_skill"),
        gr.update(value=template),
        "テンプレートを作成しました。IDと本文を調整して保存してください。",
    )


def handle_skill_save(room_name: str, skill_ref: str, scope: str, procedure_id: str, content: str):
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update(), gr.update()
    if not procedure_id:
        gr.Warning("Skill IDを入力してください。")
        return gr.update(), gr.update(), "Skill IDを入力してください。"
    if not content or not str(content).strip():
        gr.Warning("Skill本文が空です。")
        return gr.update(), gr.update(), "Skill本文が空です。"

    try:
        from procedure_memory_manager import ProcedureMemoryManager

        normalized_scope = "shared" if str(scope or "").strip() == "shared" else "private"
        result = ProcedureMemoryManager(room_name).save_raw_procedure(
            procedure_id=procedure_id,
            content=content,
            scope=normalized_scope,
        )
        selected_ref = f"{result.get('scope')}:{result.get('procedure_id')}"
        procedures = _load_skills(room_name)
        gr.Info(f"Skill '{selected_ref}' を保存しました。")
        return (
            _render_skill_table(procedures),
            _skill_selector_update(procedures, selected_ref=selected_ref),
            f"Skill '{selected_ref}' を保存しました。",
        )
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Skill保存エラー: {e}")
        return gr.update(), gr.update(), f"Skill保存エラー: {e}"


def handle_skill_delete(room_name: str, skill_ref: str):
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), "ルームが選択されていません。"
    if not skill_ref:
        gr.Warning("削除するSkillを選択してください。")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), "削除するSkillを選択してください。"

    try:
        from procedure_memory_manager import ProcedureMemoryManager

        ProcedureMemoryManager(room_name).delete_procedure(skill_ref)
        procedures = _load_skills(room_name)
        gr.Info(f"Skill '{skill_ref}' を削除しました。")
        return (
            _render_skill_table(procedures),
            _skill_selector_update(procedures),
            gr.update(value="private"),
            gr.update(value=""),
            gr.update(value=""),
            f"Skill '{skill_ref}' を削除しました。",
        )
    except Exception as e:
        traceback.print_exc()
        gr.Error(f"Skill削除エラー: {e}")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), f"Skill削除エラー: {e}"
