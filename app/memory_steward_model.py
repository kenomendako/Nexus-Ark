"""Memory Steward Phase 2A専用の、通常処理へ未接続なモデルadapter。"""

from __future__ import annotations

import json
import time
from contextlib import redirect_stdout
from typing import Any, Callable

from langchain_core.messages import HumanMessage

import config_manager
import utils
from llm_factory import LLMFactory
from memory_steward_evaluation import (
    EVIDENCE_STATUSES,
    INTERVENTIONS,
    OPERATIONS,
    PROCESS_STATUSES,
    REASON_CODES,
    SCOPES,
    TARGET_STORES,
    aggregate_decision_scores,
    parse_decision,
    score_decision,
)


Invoker = Callable[..., Any]


class _DiscardOutput:
    """内部モデル初期化ログを保持せず破棄する。"""

    def write(self, _text: str) -> int:
        return len(_text)

    def flush(self) -> None:
        return None


def build_prompt(snapshot: dict[str, Any]) -> str:
    """自由な思考過程を要求せず、判定JSON objectだけを要求する。"""
    allowlists = {
        "proposed_operation": sorted(OPERATIONS),
        "target_store": sorted(TARGET_STORES),
        "reason_code": sorted(REASON_CODES),
        "evidence_status": sorted(EVIDENCE_STATUSES),
        "process_status": sorted(PROCESS_STATUSES),
        "scope": sorted(SCOPES),
        "proposed_intervention": sorted(INTERVENTIONS),
    }
    return (
        "あなたはMemory Stewardのオフライン判定器です。入力は合成snapshotです。\n"
        "入力にない事実を推測せず、記憶や外部状態を変更せず、JSON objectだけを返してください。\n"
        "Markdown、説明文、思考過程、tool callは禁止です。\n"
        "必須キーは schema_version, proposed_operation, target_store, target_ref, "
        "reason_code, confidence, evidence_status, process_status, scope, "
        "proposed_intervention です。未知キーは禁止です。\n"
        "schema_versionは整数1、confidenceは0以上1以下の有限数にしてください。\n"
        "target_storeがnoneならtarget_refはnull、それ以外なら入力snapshotのtarget_refと同じ"
        "16〜64桁の小文字16進HMAC参照にしてください。\n"
        "evidence_status、process_status、scopeは入力snapshotと完全一致させ、根拠状態・進行状態・"
        "意味範囲を勝手に前進・変更してはいけません。\n"
        "reason_codeは以下の列挙値だけから選び、自由な理由文を出力してはいけません。\n"
        f"許可allowlist:{json.dumps(allowlists, ensure_ascii=False, separators=(',', ':'))}\n"
        f"入力snapshot:\n{json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}"
    )


class MemoryStewardModelAdapter:
    """内部processingモデルを呼ぶ薄いadapter。失敗は評価エラーとして隔離する。"""

    def __init__(self, invoker: Invoker | None = None) -> None:
        self._invoker = invoker or LLMFactory.invoke_internal_llm

    def configured_candidate(self) -> dict[str, str]:
        provider, model, profile = config_manager.get_effective_internal_model("processing")
        return {
            "provider": provider,
            "model": model,
            "profile": profile,
        }

    def evaluate(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            # Phase 2A live評価はcaseごとに最大1回とし、APIキーを含み得る内部ログを出力しない。
            with redirect_stdout(_DiscardOutput()):
                response, _ = self._invoker(
                    internal_role="processing",
                    prompt=[HumanMessage(content=build_prompt(snapshot))],
                    room_name=None,
                    temperature=0.0,
                    max_retries=1,
                )
            content = utils.extract_text_from_llm_content(response.content)
            return {
                "status": "ok",
                "decision": parse_decision(content),
                "usage": {
                    **_usage_summary(response),
                    "duration_ms": max(0, round((time.perf_counter() - started) * 1000)),
                },
            }
        except Exception as exc:
            return {
                "status": "evaluation_error",
                "error_type": type(exc).__name__,
                "decision": None,
                "usage": {
                    "input_tokens": None,
                    "output_tokens": None,
                    "cost": None,
                    "duration_ms": max(0, round((time.perf_counter() - started) * 1000)),
                },
            }


def _usage_summary(response: Any) -> dict[str, int | float | None]:
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        usage = getattr(response, "response_metadata", {}).get("token_usage", {})
    if not isinstance(usage, dict):
        usage = {}
    return {
        "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
        "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
        "cost": usage.get("cost"),
    }


def evaluate_candidates(
    snapshots: list[dict[str, Any]],
    candidates: dict[str, MemoryStewardModelAdapter],
) -> dict[str, list[dict[str, Any]]]:
    """同一snapshot列を、注入された複数adapterで比較する。"""
    return {
        label: [adapter.evaluate(snapshot) for snapshot in snapshots]
        for label, adapter in candidates.items()
    }


def compare_candidates(
    cases: list[dict[str, Any]],
    candidates: dict[str, MemoryStewardModelAdapter],
) -> dict[str, dict[str, Any]]:
    """同一case列と同一採点器で、注入された複数adapterを比較する。"""
    comparison = {}
    for label, adapter in candidates.items():
        results = []
        for case in cases:
            outcome = adapter.evaluate(case["snapshot"])
            raw = outcome["decision"] if outcome["status"] == "ok" else ""
            results.append(score_decision(case, raw))
        comparison[label] = {
            "metrics": aggregate_decision_scores(results),
            "cases": results,
        }
    return comparison
