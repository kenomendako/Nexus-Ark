import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage

import config_manager
import constants
import room_manager
import utils
from llm_factory import LLMFactory
from tools.memory_tools import read_identity_memory, _apply_identity_memory_edits


REQUESTS_FILENAME = "identity_edit_requests.json"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _requests_path(room_name: str) -> Path:
    return Path(constants.ROOMS_DIR) / room_name / "memory" / REQUESTS_FILENAME


def _normalize_request_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _strip_request_text(value: Any) -> str:
    return str(value or "").strip()


def _load_json_list(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except Exception as e:
        print(f"--- [Identity Edit Requests] 読み込み失敗: {e} ---")
    return []


def load_identity_edit_requests(room_name: str) -> List[Dict[str, Any]]:
    if not room_name:
        return []
    return _load_json_list(_requests_path(room_name))


def save_identity_edit_requests(room_name: str, requests: List[Dict[str, Any]]) -> None:
    path = _requests_path(room_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(requests, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _record_identity_audit(
    room_name: str,
    action: str,
    intent: str,
    status: str,
    details: str,
    request_id: str = "",
    timeline_id: str = "",
) -> None:
    try:
        from capability_policy_manager import CapabilityPolicyManager

        CapabilityPolicyManager(room_name).record_audit(
            category="identity_memory",
            action=action,
            intent=intent,
            status=status,
            details=details,
            request_id=request_id,
            related_timeline_id=timeline_id,
        )
    except Exception as e:
        print(f"--- [Identity Edit Requests] 監査ログ記録をスキップ: {e} ---")


def create_identity_edit_request(
    room_name: str,
    modification_request: str,
    intent: str = "",
    timeline_id: str = "",
) -> Tuple[Dict[str, Any], bool]:
    """Identity編集提案を保存する。未処理提案がある場合は重複作成しない。"""
    if not room_name:
        raise ValueError("room_name is required")
    original_text = _strip_request_text(modification_request)
    if not _normalize_request_text(original_text):
        raise ValueError("modification_request is required")

    requests = load_identity_edit_requests(room_name)
    for request in requests:
        if request.get("status") == "pending":
            _record_identity_audit(
                room_name,
                action="plan_identity_memory_edit",
                intent=original_text,
                status="skipped",
                details=(
                    f"pending_request_exists={request.get('request_id')}; "
                    "未承認のIdentity編集提案があるため新規作成しませんでした。"
                ),
                request_id=str(request.get("request_id") or ""),
                timeline_id=timeline_id,
            )
            return request, False

    created_at = _now_iso()
    request_id = f"identreq_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    request = {
        "request_id": request_id,
        "created_at": created_at,
        "updated_at": created_at,
        "modification_request": original_text,
        "intent": intent or original_text,
        "timeline_id": timeline_id or "",
        "status": "pending",
    }
    requests.append(request)
    save_identity_edit_requests(room_name, requests)
    _record_identity_audit(
        room_name,
        action="plan_identity_memory_edit",
        intent=original_text,
        status="pending",
        details="自律行動中のIdentity編集提案をユーザー承認待ちキューへ保存しました。",
        request_id=request_id,
        timeline_id=timeline_id,
    )
    return request, True


def get_identity_edit_request(room_name: str, request_id: str) -> Optional[Dict[str, Any]]:
    for request in load_identity_edit_requests(room_name):
        if str(request.get("request_id") or "") == str(request_id or ""):
            return request
    return None


def pending_requests_dataframe_rows(room_name: str) -> List[List[str]]:
    rows: List[List[str]] = []
    for request in load_identity_edit_requests(room_name):
        if request.get("status") != "pending":
            continue
        created_at = str(request.get("created_at") or "")
        display_time = created_at.replace("T", " ")[:16]
        rows.append([
            str(request.get("request_id") or ""),
            display_time,
            str(request.get("status") or ""),
            str(request.get("modification_request") or ""),
        ])
    return rows


def _create_required_backup(room_name: str) -> str:
    backup_path = room_manager.create_backup(room_name, "memory")
    if backup_path:
        return backup_path
    _, _, _, identity_path, _, _, _ = room_manager.get_room_files_paths(room_name)
    if identity_path and os.path.exists(identity_path):
        return "既存バックアップと同一内容のため新規バックアップなし"
    return ""


def _build_identity_edit_instruction(current_content: str, modification_request: str) -> str:
    common_dictation_rules = (
        "【あなたの絶対的役割：無機質な書記（Cold Scribe）】\n"
        "- あなたの役割は、あなたの本体（メインAI）が『変更要求』に書き記した文章を、"
        "一字一句、一切の改変（要約、翻訳、挨拶の削除、口調の修正、誤字脱字の修正など）を加えず、"
        "指定された場所にそのまま記録することだけです。\n"
        "- `modification_request` に含まれていない文字や記号を、あなたの判断で絶対に追加しないでください。\n"
        "- 文章の内容がいかなる言語であっても、あなたはそれを解釈・翻訳せず、単なる記号としてそのまま記録してください。\n"
        "- あなた自身の思考や解釈、挨拶などは一切出力せず、JSON形式のリストのみを出力してください。\n\n"
        "【出力JSONフォーマット】\n"
        "以下のキーを持つオブジェクトのリストを出力してください：\n"
        "- `line`: 編集対象の行番号（整数）。追記の場合は最終行を指定。\n"
        "- `operation`: 操作種別。`replace`（置換）, `delete`（削除）, `insert_after`（追記）のいずれか。\n"
        "- `content`: 記録する文章（文字列）。\n"
    )
    return (
        "【これは永続記憶の設計タスクです】\n"
        "あなたは今、本体のプロフィールの基盤となる記憶(`memory_identity.txt`)を更新するための"
        "『設計図』を作成しています。\n\n"
        f"{common_dictation_rules}"
        "【行番号付きデータ（memory_identity.txt全文）】\n---\n"
        f"{current_content}\n"
        "---\n\n"
        "【本体からの変更要求（これをそのまま記録してください）】\n"
        f"「{modification_request}」\n\n"
        "【出力ルール】\n"
        "- 【差分指示のリスト】（JSON配列）のみを出力してください。\n"
        "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
    )


def _parse_json_instructions(text: str) -> List[Dict[str, Any]]:
    json_match = re.search(r"```json\s*([\s\S]*?)\s*```", text, re.DOTALL)
    content_to_process = json_match.group(1).strip() if json_match else text.strip()
    instructions = json.loads(content_to_process)
    if not isinstance(instructions, list):
        raise ValueError("編集AIの出力がJSON配列ではありません。")
    return instructions


def _mark_request(room_name: str, request_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    requests = load_identity_edit_requests(room_name)
    for request in requests:
        if str(request.get("request_id") or "") == str(request_id or ""):
            request.update(updates)
            request["updated_at"] = _now_iso()
            save_identity_edit_requests(room_name, requests)
            return request
    raise ValueError("対象のIdentity編集提案が見つかりません。")


def _notify_persona(room_name: str, request_id: str, message: str) -> None:
    try:
        log_f, *_ = room_manager.get_room_files_paths(room_name)
        utils.save_message_to_log(
            log_f,
            "## SYSTEM:identity_edit_request",
            f"（Identity編集提案 {request_id}: {message}）",
        )
    except Exception as e:
        print(f"--- [Identity Edit Requests] ペルソナ通知ログの記録をスキップ: {e} ---")


def approve_identity_edit_request(room_name: str, request_id: str) -> Dict[str, Any]:
    request = get_identity_edit_request(room_name, request_id)
    if not request:
        raise ValueError("対象のIdentity編集提案が見つかりません。")
    if request.get("status") != "pending":
        raise ValueError("このIdentity編集提案は承認待ちではありません。")

    modification_request = str(request.get("modification_request") or "").strip()
    backup_path = _create_required_backup(room_name)
    if not backup_path:
        raise RuntimeError("identity memory編集前のバックアップに失敗しました。")

    raw_content = read_identity_memory.invoke({"room_name": room_name})
    lines = str(raw_content or "").split("\n")
    current_content = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines))
    prompt = _build_identity_edit_instruction(current_content, modification_request)

    model_name = config_manager.get_current_global_model()
    api_key = config_manager.get_active_gemini_api_key(room_name, model_name=model_name)
    generation_config = config_manager.get_effective_settings(room_name)
    llm = LLMFactory.create_chat_model(
        model_name=model_name,
        api_key=api_key,
        generation_config=generation_config,
        room_name=room_name,
    )
    response = llm.invoke([HumanMessage(content=prompt)])
    response_text = utils.get_content_as_string(response).strip()
    instructions = _parse_json_instructions(response_text)
    result_text = _apply_identity_memory_edits(instructions, room_name)

    updated = _mark_request(
        room_name,
        request_id,
        {
            "status": "applied",
            "applied_at": _now_iso(),
            "result": result_text,
            "backup_path": backup_path,
        },
    )
    _record_identity_audit(
        room_name,
        action="approve_identity_edit_request",
        intent=modification_request,
        status="success",
        details=f"ユーザー承認によりIdentity編集提案を反映しました。backup_path={backup_path}; result={result_text}",
        request_id=request_id,
        timeline_id=str(request.get("timeline_id") or ""),
    )
    _notify_persona(room_name, request_id, f"ユーザー承認により反映されました。結果: {result_text}")
    return updated


def reject_identity_edit_request(room_name: str, request_id: str, reason: str = "") -> Dict[str, Any]:
    request = get_identity_edit_request(room_name, request_id)
    if not request:
        raise ValueError("対象のIdentity編集提案が見つかりません。")
    if request.get("status") != "pending":
        raise ValueError("このIdentity編集提案は承認待ちではありません。")

    reason_text = str(reason or "").strip()
    updated = _mark_request(
        room_name,
        request_id,
        {
            "status": "rejected",
            "rejected_at": _now_iso(),
            "rejection_reason": reason_text,
        },
    )
    _record_identity_audit(
        room_name,
        action="reject_identity_edit_request",
        intent=str(request.get("modification_request") or ""),
        status="rejected",
        details=f"ユーザーがIdentity編集提案を却下しました。reason={reason_text or 'なし'}",
        request_id=request_id,
        timeline_id=str(request.get("timeline_id") or ""),
    )
    _notify_persona(
        room_name,
        request_id,
        f"ユーザーにより却下されました。理由: {reason_text}" if reason_text else "ユーザーにより却下されました。",
    )
    return updated
