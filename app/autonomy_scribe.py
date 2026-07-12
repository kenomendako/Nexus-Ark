"""Lightweight reflection drafting for autonomous actions."""

import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage

import utils
from llm_factory import LLMFactory


VALID_OUTCOME_TYPES = {"progressed", "observed", "learned", "rest"}
VALID_CONTEXT_TYPES = {"CONTINUE", "DEEPEN", "NEW", "ORGANIZE", "SOCIAL", "REST"}


def draft_reflection(
    room_name: str,
    final_response_text: str,
    tool_history: List[Dict[str, Any]],
    motivation_log: Dict[str, Any] | None = None,
    timeout_seconds: float = 10.0,
) -> Dict[str, Any]:
    """Draft a reflect_after_action-compatible record using the internal processing model."""
    clean_response = utils.remove_thoughts_from_text(final_response_text or "").strip()
    if not clean_response:
        raise ValueError("final_response_text is empty")

    payload = {
        "final_response_text": clean_response[:2000],
        "tool_history": _compact_tool_history(tool_history),
        "motivation": {
            "dominant_drive": (motivation_log or {}).get("dominant_drive", ""),
            "dominant_drive_label": (motivation_log or {}).get("dominant_drive_label", ""),
            "narrative": (motivation_log or {}).get("narrative", ""),
        },
    }
    prompt = _build_scribe_prompt(payload)

    def _invoke() -> str:
        # [2026-06-23 FIX] 内部処理は共通設定のキー＋ローテーションで実行する。
        # invoke_internal_llm が429時に共通プールのキーを回す（単発呼び出しでもローテーションが効く）。
        response, _ = LLMFactory.invoke_internal_llm(
            internal_role="processing",
            prompt=[HumanMessage(content=prompt)],
            room_name=room_name,
            temperature=0.2,
        )
        return utils.extract_text_from_llm_content(response.content)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke)
        try:
            raw = future.result(timeout=timeout_seconds)
        except TimeoutError as exc:
            raise TimeoutError("autonomy scribe timed out") from exc

    parsed = _extract_json_object(raw)
    return _normalize_reflection(parsed)


def _build_scribe_prompt(payload: Dict[str, Any]) -> str:
    return (
        "あなたはNexus Arkの自律行動ログを書き写す軽量スクライブです。\n"
        "入力にある最終応答とツール実行履歴だけを根拠に、Reflect記録JSONを作ってください。\n"
        "そこに無い事実、感情、成功、ユーザー反応を創作してはいけません。\n"
        "出力はJSONオブジェクトのみです。Markdownや説明文は不要です。\n\n"
        "必須キー:\n"
        "- action_summary: ペルソナ視点の短い一人称要約。最終応答の口調を大きく変えない。\n"
        "- outcome_type: progressed / observed / learned / rest のいずれか。\n"
        "- next_action: 次に戻るための具体的な一手。なければ「次回、今回の結果を確認する。」\n"
        "- context_type: CONTINUE / DEEPEN / NEW / ORGANIZE / SOCIAL / REST のいずれか。\n\n"
        f"入力JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _compact_tool_history(tool_history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    compact = []
    for item in tool_history or []:
        name = str(item.get("name") or item.get("tool_name") or "")[:120]
        content = str(item.get("content") or item.get("result") or "")
        compact.append({
            "name": name,
            "content": content.replace("\n", " ")[:500],
        })
    return compact[:20]


def _extract_json_object(text: str) -> Dict[str, Any]:
    content = (text or "").strip()
    if not content:
        raise ValueError("scribe returned empty content")
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError("scribe response did not contain a JSON object")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("scribe JSON is not an object")
    return parsed


def _normalize_reflection(data: Dict[str, Any]) -> Dict[str, str]:
    action_summary = str(data.get("action_summary") or data.get("summary") or "").strip()
    if not action_summary:
        raise ValueError("scribe reflection is missing action_summary")
    if len(action_summary) > 360:
        action_summary = action_summary[:360].rstrip() + "..."

    outcome_type = str(data.get("outcome_type") or "observed").strip().lower()
    if outcome_type not in VALID_OUTCOME_TYPES:
        outcome_type = "observed"

    next_action = str(data.get("next_action") or "").strip()
    if not next_action:
        next_action = "次回、今回の結果を確認する。"
    if len(next_action) > 240:
        next_action = next_action[:240].rstrip() + "..."

    context_type = str(data.get("context_type") or "CONTINUE").strip().upper()
    if context_type not in VALID_CONTEXT_TYPES:
        context_type = "CONTINUE"

    return {
        "action_summary": action_summary,
        "outcome_type": outcome_type,
        "next_action": next_action,
        "context_type": context_type,
    }
