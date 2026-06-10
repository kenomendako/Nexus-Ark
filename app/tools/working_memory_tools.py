from langchain_core.tools import tool
import os
import constants
import room_manager
import traceback
import json
import datetime
import shutil
import re
from pydantic import BaseModel, Field

def _get_wm_dir(room_name: str) -> str:
    return os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME, constants.WORKING_MEMORY_DIR_NAME)

def _get_wm_metadata_path(room_name: str) -> str:
    return os.path.join(_get_wm_dir(room_name), constants.WORKING_MEMORY_METADATA_FILENAME)

def _safe_slot_name(slot_name: str) -> str:
    slot_name = str(slot_name or "").strip()
    if not slot_name or ".." in slot_name or "/" in slot_name or "\\" in slot_name:
        raise ValueError("不正なスロット名です。")
    if slot_name.endswith(constants.WORKING_MEMORY_EXTENSION):
        slot_name = slot_name[:-len(constants.WORKING_MEMORY_EXTENSION)]
    return slot_name

def _get_wm_path(room_name: str, slot_name: str) -> str:
    slot_name = _safe_slot_name(slot_name)
    if not slot_name.endswith(constants.WORKING_MEMORY_EXTENSION):
        slot_name += constants.WORKING_MEMORY_EXTENSION
    return os.path.join(_get_wm_dir(room_name), slot_name)

def _load_wm_metadata(room_name: str) -> dict:
    metadata_path = _get_wm_metadata_path(room_name)
    if not os.path.exists(metadata_path):
        return {"version": 1, "slots": {}}
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": 1, "slots": {}}
        data.setdefault("version", 1)
        data.setdefault("slots", {})
        return data
    except Exception:
        return {"version": 1, "slots": {}}

def get_working_memory_metadata(room_name: str) -> dict:
    """UIや内部処理から参照するWMメタデータを取得する。"""
    return _load_wm_metadata(room_name)

def save_working_memory_metadata(room_name: str, metadata: dict) -> dict:
    """UI編集されたWMメタデータを保存する。"""
    if not isinstance(metadata, dict):
        raise ValueError("Working Memory metadata はJSONオブジェクトである必要があります。")
    normalized = {"version": 1, "slots": {}}
    normalized.update(metadata)
    if not isinstance(normalized.get("slots"), dict):
        normalized["slots"] = {}
    _save_wm_metadata(room_name, normalized)
    return normalized

def _save_wm_metadata(room_name: str, metadata: dict) -> None:
    os.makedirs(_get_wm_dir(room_name), exist_ok=True)
    metadata["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(_get_wm_metadata_path(room_name), "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

def _touch_slot_metadata(room_name: str, slot_name: str, **updates) -> None:
    slot_name = _safe_slot_name(slot_name)
    metadata = _load_wm_metadata(room_name)
    slot_meta = metadata.setdefault("slots", {}).setdefault(slot_name, {})
    slot_meta.update(updates)
    slot_meta.setdefault("status", "active")
    slot_meta["last_used_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_wm_metadata(room_name, metadata)

def archive_stale_working_memories(room_name: str, days: int = 30) -> list[str]:
    """一定期間使われていないWMスロットをmetadata上で休眠扱いにする。"""
    metadata = _load_wm_metadata(room_name)
    slots_meta = metadata.setdefault("slots", {})
    if not slots_meta:
        return []

    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    archived = []
    for slot_name, meta in slots_meta.items():
        if not isinstance(meta, dict) or meta.get("status") == "archived":
            continue
        last_used = meta.get("last_used_at") or meta.get("updated_at")
        if not last_used:
            continue
        try:
            last_dt = datetime.datetime.strptime(str(last_used), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if last_dt < cutoff:
            meta["status"] = "archived"
            meta["archived_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            meta["archive_reason"] = f"{days}日以上未使用"
            archived.append(slot_name)
    if archived:
        _save_wm_metadata(room_name, metadata)
    return archived

def select_working_memory_for_research_context(room_name: str, query: str = "", set_active: bool = True) -> str:
    """
    Research Thread・目標・問いの優先度/類似度から、自律行動に使うWMスロットを選ぶ。
    Purpose Profileの関心でスコアをブーストし、長期目的に沿うスロットを優先する。
    """
    try:
        # --- Phase 1: Research Thread 起点の候補 ---
        from research_thread_manager import ResearchThreadManager
        manager = ResearchThreadManager(room_name)
        candidates = []
        if query:
            candidates = manager.find_similar_threads(query=query, limit=5, boost_by_purpose=True)
        if not candidates:
            candidates = manager.list_threads(status="active", boost_by_purpose=True)

        metadata = _load_wm_metadata(room_name)
        slots_meta = metadata.get("slots", {})
        for thread in candidates:
            slot_name = thread.get("working_memory_slot", "")
            if not slot_name:
                continue
            try:
                slot_name = _safe_slot_name(slot_name)
            except ValueError:
                continue
            slot_meta = slots_meta.get(slot_name, {})
            if slot_meta.get("status") == "archived":
                continue
            path = _get_wm_path(room_name, slot_name)
            if not os.path.exists(path):
                continue
            if set_active:
                room_manager.set_active_working_memory_slot(room_name, slot_name)
                _touch_slot_metadata(
                    room_name,
                    slot_name,
                    linked_thread_id=thread.get("thread_id", slot_meta.get("linked_thread_id", "")),
                    auto_selected_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    auto_selected_reason="research_thread_context",
                )
            return slot_name

        # --- Phase 2: 目標起点の候補 ---
        selected = _select_slot_by_goal(room_name, query, slots_meta, set_active)
        if selected:
            return selected
    except Exception:
        traceback.print_exc()
    return ""


def _select_slot_by_goal(room_name: str, query: str, slots_meta: dict, set_active: bool) -> str:
    """目標に紐づくWMスロットから候補を選択する。"""
    try:
        from goal_manager import GoalManager
        gm = GoalManager(room_name)
        goals = gm.get_active_goals()
        if not goals:
            return ""

        query_lower = str(query or "").lower()
        # 目標テキストとクエリの一致度で候補をスコアリング
        scored_goals = []
        for goal in goals:
            goal_text = str(goal.get("goal", "")).lower()
            goal_id = goal.get("id", "")
            # クエリとの簡易一致スコア
            score = 0
            if query_lower:
                for word in query_lower.split():
                    if len(word) >= 2 and word in goal_text:
                        score += 1
            scored_goals.append((score, goal_id, goal_text))

        # スコア順にソート
        scored_goals.sort(key=lambda x: x[0], reverse=True)

        for _score, goal_id, _goal_text in scored_goals:
            # このgoal_idに紐づくスロットを探す
            for slot_name, meta in slots_meta.items():
                if not isinstance(meta, dict):
                    continue
                if meta.get("linked_goal_id") != goal_id:
                    continue
                if meta.get("status") == "archived":
                    continue
                try:
                    slot_name = _safe_slot_name(slot_name)
                except ValueError:
                    continue
                path = _get_wm_path(room_name, slot_name)
                if not os.path.exists(path):
                    continue
                if set_active:
                    room_manager.set_active_working_memory_slot(room_name, slot_name)
                    _touch_slot_metadata(
                        room_name,
                        slot_name,
                        auto_selected_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        auto_selected_reason="goal_context",
                    )
                return slot_name
    except Exception:
        traceback.print_exc()
    return ""

def _backup_wm_file(room_name: str, slot_name: str, path: str) -> None:
    if not os.path.exists(path):
        return
    backup_dir = os.path.join(constants.ROOMS_DIR, room_name, "backups", "working_memories")
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"{timestamp}_{slot_name}{constants.WORKING_MEMORY_EXTENSION}.bak"
    shutil.copy2(path, os.path.join(backup_dir, backup_filename))

_STANDARD_WM_SECTIONS = ["Current Intent", "Known Context", "Next Action", "Stop Condition"]
_JSON_WM_KEY_TO_SECTION = {
    "current_intent": "Current Intent",
    "known_context": "Known Context",
    "linked_goal": "Linked Goal",
    "linked_thread": "Linked Thread",
    "next_action": "Next Action",
    "stop_condition": "Stop Condition",
}

def _section_from_key(key: str) -> str:
    clean_key = str(key or "").strip().strip("'\"")
    normalized = clean_key.replace("-", "_").replace(" ", "_").lower()
    if normalized in _JSON_WM_KEY_TO_SECTION:
        return _JSON_WM_KEY_TO_SECTION[normalized]
    return clean_key.replace("_", " ").strip().title() or "Notes"

def _stringify_wm_value(value) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, indent=2).strip()

def _markdown_from_json_working_memory(raw_text: str) -> str:
    stripped = str(raw_text or "").strip()
    if not stripped.startswith("{"):
        return str(raw_text or "")
    decoder = json.JSONDecoder()
    try:
        parsed, end = decoder.raw_decode(stripped)
    except Exception:
        return str(raw_text or "")
    if not isinstance(parsed, dict):
        return str(raw_text or "")

    blocks = []
    for key, value in parsed.items():
        value_text = _stringify_wm_value(value)
        if not value_text:
            continue
        blocks.append(f"## {_section_from_key(key)}\n{value_text}")
    remainder = stripped[end:].strip()
    if remainder:
        blocks.append(remainder)
    return "\n\n".join(blocks)

def _split_wm_markdown_sections(raw_text: str) -> tuple[str, list[tuple[str, str]]]:
    text = _markdown_from_json_working_memory(raw_text)
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
    if not matches:
        return text.strip(), []

    preface = text[:matches[0].start()].strip()
    sections = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[start:end].strip()))
    return preface, sections

def _rebuild_wm_markdown(preface: str, sections: list[tuple[str, str]]) -> str:
    merged = {}
    order = []
    for heading, body in sections:
        heading = _section_from_key(heading)
        body = str(body or "").strip()
        if not heading:
            continue
        if heading not in order:
            order.append(heading)
        # Later duplicate sections are usually fresher patch results.
        merged[heading] = body

    ordered_headings = [heading for heading in _STANDARD_WM_SECTIONS if heading in merged]
    ordered_headings.extend(heading for heading in order if heading not in ordered_headings)

    blocks = []
    if preface and not preface.startswith("{"):
        blocks.append(preface)
    for heading in ordered_headings:
        body = merged.get(heading, "").strip()
        blocks.append(f"## {heading}\n{body}".rstrip())
    return "\n\n".join(blocks).strip()

def _normalize_working_memory_text(raw_text: str) -> str:
    preface, sections = _split_wm_markdown_sections(raw_text)
    if not sections:
        return preface
    return _rebuild_wm_markdown(preface, sections)

def _extract_wm_sections_from_content(content: str) -> list[tuple[str, str]]:
    _preface, sections = _split_wm_markdown_sections(str(content or ""))
    return sections

def _apply_working_memory_section_patch(existing: str, section: str, content: str, mode: str = "replace") -> str:
    existing = _normalize_working_memory_text(existing)
    preface, sections = _split_wm_markdown_sections(existing)
    target_section = _section_from_key(section)
    new_content = str(content or "").strip()

    found = False
    updated_sections = []
    for heading, body in sections:
        if _section_from_key(heading) == target_section:
            found = True
            body = f"{str(body).strip()}\n{new_content}".strip() if mode == "append" else new_content
        updated_sections.append((_section_from_key(heading), body))
    if not found:
        updated_sections.append((target_section, new_content))
    return _rebuild_wm_markdown(preface, updated_sections)

def get_working_memory_overview(room_name: str, limit: int = 8) -> str:
    try:
        wm_dir = _get_wm_dir(room_name)
        if not os.path.exists(wm_dir):
            return ""
        metadata = _load_wm_metadata(room_name)
        slots = [
            f.replace(constants.WORKING_MEMORY_EXTENSION, "")
            for f in os.listdir(wm_dir)
            if f.endswith(constants.WORKING_MEMORY_EXTENSION)
        ]
        if not slots:
            return ""
        active_slot = room_manager.get_active_working_memory_slot(room_name)
        lines = ["\n### ワーキングメモリスロット一覧"]
        active_slots = []
        archived_count = 0
        for slot in slots:
            meta = metadata.get("slots", {}).get(slot, {})
            if meta.get("status") == "archived":
                archived_count += 1
            else:
                active_slots.append(slot)
        for slot in active_slots[:limit]:
            meta = metadata.get("slots", {}).get(slot, {})
            marker = "active" if slot == active_slot else "slot"
            linked_thread = meta.get("linked_thread_id", "")
            linked_goal = meta.get("linked_goal_id", "")
            purpose = meta.get("purpose", "")
            parts = [f"- {slot} ({marker})"]
            if linked_thread:
                parts.append(f"linked_thread={linked_thread}")
            if linked_goal:
                parts.append(f"linked_goal={linked_goal}")
            if purpose:
                parts.append(f"purpose={purpose}")
            lines.append(" / ".join(parts))
        if archived_count:
            lines.append(f"- archived_slots: {archived_count}件（常時注入から除外）")
        return "\n".join(lines) + "\n"
    except Exception:
        return ""

@tool
def list_working_memories(room_name: str) -> str:
    """
    現在利用可能なワーキングメモリのスロット（話題ごと）の一覧と、現在アクティブなスロット名を取得する。
    """
    try:
        wm_dir = _get_wm_dir(room_name)
        if not os.path.exists(wm_dir):
            return "【利用可能なワーキングメモリスロットはありません】"
        
        slots = [f.replace(constants.WORKING_MEMORY_EXTENSION, '') for f in os.listdir(wm_dir) if f.endswith(constants.WORKING_MEMORY_EXTENSION)]
        active_slot = room_manager.get_active_working_memory_slot(room_name)
        
        if not slots:
            return "【利用可能なワーキングメモリスロットはありません】"
            
        metadata = _load_wm_metadata(room_name)
        result = f"現在アクティブなスロット: {active_slot}\n"
        result += "利用可能なスロット一覧:\n"
        for slot in slots:
            meta = metadata.get("slots", {}).get(slot, {})
            linked = f" / linked_thread={meta.get('linked_thread_id')}" if meta.get("linked_thread_id") else ""
            purpose = f" / purpose={meta.get('purpose')}" if meta.get("purpose") else ""
            result += f"- {slot}{linked}{purpose}\n"
        return result
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリ一覧の取得中にエラーが発生しました: {e}"

class SwitchWorkingMemoryArgs(BaseModel):
    slot_name: str = Field(..., description="スロット名（例: 'kobe_trip', 'nexus_ark_dev'）")
    room_name: str = Field(..., description="対象のルーム名")
    intent: str = Field("新規タスクまたは話題の分離のため", description="なぜスロットを切り替えるのか、または新しく作成するのかという意図・背景")

@tool(args_schema=SwitchWorkingMemoryArgs)
def switch_working_memory(slot_name: str, room_name: str, intent: str = "新規タスクまたは話題の分離のため") -> str:
    """
    アクティブなワーキングメモリのスロット（話題）を切り替える。
    存在しないスロット名を指定した場合は、新しくその話題のスロットが作成される。
    
    slot_name: スロット名（例: 'kobe_trip', 'nexus_ark_dev'）。
    intent: なぜスロットを切り替えるのか、または新しく作成するのかという意図・背景（必須）。
    """
    try:
        slot_name = _safe_slot_name(slot_name)
            
        success = room_manager.set_active_working_memory_slot(room_name, slot_name)
        if success:
            _touch_slot_metadata(room_name, slot_name)
            return f"成功: ワーキングメモリのスロットを '{slot_name}' に切り替えました。以後、read_working_memory や update_working_memory はこの新しいスロットに対して実行されます。"
        else:
            return "【エラー】スロットの切り替えに失敗しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの切り替え中にエラーが発生しました: {e}"

@tool
def read_working_memory(room_name: str, slot_name: str = None) -> str:
    """
    現在のプランや動的コンテキストを保持するワーキングメモリの内容を読み込む。
    slot_nameを指定しない場合は、現在アクティブなスロットが読み込まれる。
    """
    try:
        target_slot = slot_name if slot_name else room_manager.get_active_working_memory_slot(room_name)
        target_slot = _safe_slot_name(target_slot)
        path = _get_wm_path(room_name, target_slot)
        
        if not os.path.exists(path):
            return f"【ワーキングメモリ '{target_slot}' はまだ作成されていません】"
        with open(path, 'r', encoding='utf-8') as f:
            content = _normalize_working_memory_text(f.read()).strip()
            _touch_slot_metadata(room_name, target_slot)
            return content if content else f"【ワーキングメモリ '{target_slot}' は空です】"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの読み込み中にエラーが発生しました: {e}"

class UpdateWorkingMemoryArgs(BaseModel):
    content: str = Field(..., description="更新後の全内容。")
    room_name: str = Field(..., description="対象のルーム名")
    context_type: str = Field("CONTINUE", description="過去の記録との関係性（'CONTINUE': 続き, 'DEEPEN': 深掘り, 'NEW': 新規）")
    intent: str = Field("情報の更新", description="なぜ更新するのか、過去の記憶や現在の状況のどの部分に基づいているのかの説明。")
    slot_name: str = Field(None, description="更新対象のスロット名（省略時は現在のアクティブスロット）。")

@tool(args_schema=UpdateWorkingMemoryArgs)
def update_working_memory(content: str, room_name: str, context_type: str = "CONTINUE", intent: str = "情報の更新", slot_name: str = None) -> str:
    """
    ワーキングメモリの内容を完全に上書き更新する。
    このツールを使用する際は、必ず過去の文脈との繋がりと意図を明示しなければなりません。
    
    context_type: 過去の記録との関係性（'CONTINUE': 続き, 'DEEPEN': 深掘り, 'NEW': 新規）
    intent: なぜ更新するのか、過去の記憶や現在の状況のどの部分に基づいているのかの説明。
    content: 更新後の全内容。
    slot_name: 更新対象のスロット名（省略時は現在のアクティブスロット）。
    """
    try:
        target_slot = slot_name if slot_name else room_manager.get_active_working_memory_slot(room_name)
        target_slot = _safe_slot_name(target_slot)
            
        path = _get_wm_path(room_name, target_slot)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        
        _backup_wm_file(room_name, target_slot, path)
        
        with open(path, "w", encoding="utf-8") as f:
            f.write(_normalize_working_memory_text(content).rstrip() + "\n")
        _touch_slot_metadata(room_name, target_slot, last_context_type=context_type, last_intent=intent)
        return f"成功: ワーキングメモリのスロット '{target_slot}' を更新しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの更新中にエラーが発生しました: {e}"

class PatchWorkingMemoryArgs(BaseModel):
    section: str = Field(..., description="更新対象のセクション名（例: Current Intent, Next Action）。独立した文字列として指定してください。")
    content: str = Field(..., description="セクションに保存する具体的な内容。独立した文字列として指定してください。")
    room_name: str = Field(..., description="対象のルーム名")
    mode: str = Field("replace", description="更新モード（'replace': 上書き, 'append': 追記）")
    slot_name: str = Field(None, description="更新対象のスロット名。省略時は現在のアクティブスロット。")
    intent: str = Field("部分更新", description="なぜ部分更新するのかという理由。")

@tool(args_schema=PatchWorkingMemoryArgs)
def patch_working_memory(section: str, content: str, room_name: str, mode: str = "replace", slot_name: str = None, intent: str = "部分更新") -> str:
    """
    ワーキングメモリの特定セクションだけを更新する。

    section: 更新対象セクション名（例: Current Intent, Next Action）。
    content: セクションに入れる内容。
    mode: replace または append。
    slot_name: 更新対象スロット。省略時は現在のアクティブスロット。
    intent: なぜ部分更新するのか。
    """
    try:
        target_slot = _safe_slot_name(slot_name if slot_name else room_manager.get_active_working_memory_slot(room_name))
        if not section or not str(section).strip():
            return "【エラー】sectionを指定してください。"
        if content is None or str(content).strip() == "None":
            return "【エラー】contentが無効です。"

        section = str(section).strip().lstrip("#").strip()
        path = _get_wm_path(room_name, target_slot)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
        _backup_wm_file(room_name, target_slot, path)

        embedded_sections = _extract_wm_sections_from_content(content)
        if embedded_sections:
            updated = existing
            for embedded_section, embedded_content in embedded_sections:
                updated = _apply_working_memory_section_patch(updated, embedded_section, embedded_content, mode=mode)
        else:
            updated = _apply_working_memory_section_patch(existing, section, content, mode=mode)

        with open(path, "w", encoding="utf-8") as f:
            f.write(updated.rstrip() + "\n")
        _touch_slot_metadata(room_name, target_slot, last_intent=intent)
        return f"成功: ワーキングメモリ '{target_slot}' の '{section}' セクションを更新しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの部分更新中にエラーが発生しました: {e}"

@tool
def link_working_memory_to_research_thread(room_name: str, slot_name: str, thread_id: str, purpose: str = "", set_active: bool = True) -> str:
    """
    ワーキングメモリスロットをResearch Threadの短期作業台として紐づける。
    """
    try:
        slot_name = _safe_slot_name(slot_name)
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            return "【エラー】thread_idを指定してください。"

        path = _get_wm_path(room_name, slot_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    f"# Working Focus: {slot_name}\n\n"
                    "## Active Thread\n"
                    f"thread_id: {thread_id}\n\n"
                    "## Current Intent\n\n"
                    "## Known Context\n\n"
                    "## Next Action\n\n"
                    "## Stop Condition\n"
                )

        _touch_slot_metadata(
            room_name,
            slot_name,
            linked_thread_id=thread_id,
            purpose=purpose,
            status="active",
        )
        try:
            from research_thread_manager import ResearchThreadManager
            ResearchThreadManager(room_name).create_or_update_thread(
                thread_id=thread_id,
                working_memory_slot=slot_name,
                priority=None,
            )
        except Exception:
            traceback.print_exc()

        if set_active:
            room_manager.set_active_working_memory_slot(room_name, slot_name)
        return f"成功: ワーキングメモリ '{slot_name}' をResearch Thread '{thread_id}' に紐づけました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリとResearch Threadの紐づけに失敗しました: {e}"


@tool
def link_working_memory_to_goal(room_name: str, slot_name: str, goal_id: str, purpose: str = "", set_active: bool = True) -> str:
    """
    ワーキングメモリスロットを目標の短期作業台として紐づける。
    目標達成に向けた計画・進捗・次の一手を管理するために使用する。
    """
    try:
        slot_name = _safe_slot_name(slot_name)
        goal_id = str(goal_id or "").strip()
        if not goal_id:
            return "【エラー】goal_idを指定してください。"

        # 目標の存在確認
        from goal_manager import GoalManager
        gm = GoalManager(room_name)
        goal = None
        for g in gm.get_active_goals():
            if g.get("id") == goal_id:
                goal = g
                break
        if not goal:
            return f"【エラー】アクティブな目標 '{goal_id}' が見つかりません。"

        goal_text = goal.get("goal", goal_id)
        path = _get_wm_path(room_name, slot_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    f"# Working Focus: {slot_name}\n\n"
                    f"## Goal\n"
                    f"goal_id: {goal_id}\n"
                    f"goal: {goal_text}\n\n"
                    "## Current Intent\n\n"
                    "## Progress\n\n"
                    "## Next Action\n\n"
                    "## Stop Condition\n"
                )

        _touch_slot_metadata(
            room_name,
            slot_name,
            linked_goal_id=goal_id,
            purpose=purpose or goal_text,
            status="active",
        )

        if set_active:
            room_manager.set_active_working_memory_slot(room_name, slot_name)
        return f"成功: ワーキングメモリ '{slot_name}' を目標 '{goal_id}' ({goal_text}) に紐づけました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリと目標の紐づけに失敗しました: {e}"


@tool
def reactivate_working_memory_slot(room_name: str, slot_name: str, reason: str = "手動復帰") -> str:
    """
    休眠（archived）状態のワーキングメモリスロットを復帰させる。
    過去のテーマに戻って作業を再開したい場合に使用する。
    """
    try:
        slot_name = _safe_slot_name(slot_name)
        path = _get_wm_path(room_name, slot_name)
        if not os.path.exists(path):
            return f"【エラー】ワーキングメモリ '{slot_name}' が見つかりません。"

        metadata = _load_wm_metadata(room_name)
        slot_meta = metadata.get("slots", {}).get(slot_name, {})
        if slot_meta.get("status") != "archived":
            return f"ワーキングメモリ '{slot_name}' は休眠状態ではありません（status={slot_meta.get('status', 'active')}）。"

        _touch_slot_metadata(
            room_name,
            slot_name,
            status="active",
            reactivated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            reactivate_reason=reason,
        )
        room_manager.set_active_working_memory_slot(room_name, slot_name)
        return f"成功: ワーキングメモリ '{slot_name}' を復帰させ、アクティブに切り替えました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの復帰に失敗しました: {e}"
