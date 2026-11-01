"""Memory Steward の合成fixture、厳格判定schema、決定的採点器。"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from typing import Any


PHASE0_CASE_IDS = {
    "CAL-01", "APP-01", "SCOPE-01", "UNCERT-01", "COR-02", "COR-03",
    "CAN-01", "TOOL-01", "TOOL-02", "DUP-01", "TOPIC-01", "ARCH-01",
    "CONC-01", "NOOP-01", "EXP-01", "METRIC-00", "PRIV-01", "OUTCOME-01",
    "ROUTE-01", "LEGACY-01", "CLOCK-01",
}
REQUIRED_CASE_IDS = PHASE0_CASE_IDS | {"COMPLETE-01"}

DECISION_FIELDS = {
    "schema_version",
    "proposed_operation",
    "target_store",
    "target_ref",
    "reason_code",
    "confidence",
    "evidence_status",
    "process_status",
    "scope",
    "proposed_intervention",
}
OPERATIONS = {
    "no_change", "update_context", "set_blocked", "set_completed", "set_cancelled",
    "set_superseded", "prevent_duplicate", "constrain_claim",
}
TARGET_STORES = {"none", "working_memory", "goal", "research_thread", "short_context"}
REASON_CODES = {
    "no_relevant_change", "explicit_correction", "explicit_completion",
    "explicit_cancellation", "tool_success_reported", "tool_failure_reported",
    "postcondition_missing", "duplicate_side_effect_risk", "scope_exceeded",
    "certainty_exceeded", "insufficient_evidence", "conflict_detected",
    "postcondition_verified",
}
EVIDENCE_STATUSES = {"unknown", "reported", "observed", "verified"}
PROCESS_STATUSES = {"unknown", "pending", "applied", "verified", "failed"}
INTERVENTIONS = {
    "no_intervention", "warn_before_action", "request_confirmation",
    "preserve_scope", "preserve_uncertainty",
}
SCOPES = {
    "synthetic_scope", "bounded_event_series", "bounded_work_item",
    "auth_expiry_reconnect", "pending_selection", "current_topic",
}
REF_RE = re.compile(r"^[0-9a-f]{16,64}$")
PROCESS_RANK = {"unknown": 0, "pending": 1, "applied": 2, "verified": 3, "failed": 0}
MUTATING_OPERATIONS = {
    "update_context", "set_blocked", "set_completed", "set_cancelled",
    "set_superseded",
}


class DecisionParseError(ValueError):
    """判定出力が厳格schemaに適合しない。"""


def parse_decision(raw: str | bytes) -> dict[str, Any]:
    """JSON objectだけを受理し、失敗時は操作候補を生成せず例外にする。"""
    if not isinstance(raw, (str, bytes)):
        raise DecisionParseError("decision must be JSON text")
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecisionParseError("decision must be UTF-8") from exc
    text = raw.strip()
    if not text:
        raise DecisionParseError("decision is empty")
    if "```" in text:
        raise DecisionParseError("code fences are forbidden")
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise DecisionParseError("decision is not one JSON object") from exc
    if not isinstance(value, dict):
        raise DecisionParseError("decision must be an object")
    if set(value) != DECISION_FIELDS:
        raise DecisionParseError("decision fields must exactly match schema")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise DecisionParseError("unsupported schema_version")
    _require_enum(value, "proposed_operation", OPERATIONS)
    _require_enum(value, "target_store", TARGET_STORES)
    _require_enum(value, "reason_code", REASON_CODES)
    _require_enum(value, "evidence_status", EVIDENCE_STATUSES)
    _require_enum(value, "process_status", PROCESS_STATUSES)
    _require_enum(value, "proposed_intervention", INTERVENTIONS)

    confidence = value["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise DecisionParseError("confidence must be a number")
    if not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
        raise DecisionParseError("confidence must be finite and within [0, 1]")

    target_ref = value["target_ref"]
    if target_ref is not None and (
        not isinstance(target_ref, str) or REF_RE.fullmatch(target_ref) is None
    ):
        raise DecisionParseError("target_ref must be a keyed reference or null")
    if value["target_store"] == "none" and target_ref is not None:
        raise DecisionParseError("none target_store requires null target_ref")
    if value["target_store"] != "none" and target_ref is None:
        raise DecisionParseError("non-none target_store requires target_ref")

    scope = value["scope"]
    if not isinstance(scope, list) or not scope or len(scope) > 4:
        raise DecisionParseError("scope must be a non-empty list")
    if any(not isinstance(item, str) or item not in SCOPES for item in scope):
        raise DecisionParseError("scope contains an unsupported value")
    if len(scope) != len(set(scope)):
        raise DecisionParseError("scope values must be unique")
    return deepcopy(value)


def _require_enum(value: dict[str, Any], key: str, allowed: set[str]) -> None:
    item = value[key]
    if not isinstance(item, str) or item not in allowed:
        raise DecisionParseError(f"unsupported enum: {key}")


def _snapshot(
    case_id: str,
    *,
    target_store: str = "none",
    wm_status: str = "unknown",
    correction: str = "none",
    tool_outcome: str = "none",
    postcondition: str = "unknown",
    goal_status: str = "unknown",
    research_status: str = "unknown",
    evidence_status: str = "unknown",
    process_status: str = "unknown",
    scope: str = "synthetic_scope",
    duplicate_candidate: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "turn_ref": "1" * 24,
        "target_store": target_store,
        "target_ref": None if target_store == "none" else "2" * 24,
        "wm_status": wm_status,
        "content_version_present": target_store == "working_memory",
        "state_version_present": target_store == "working_memory",
        "correction": correction,
        "tool_outcome": tool_outcome,
        "postcondition": postcondition,
        "goal_status": goal_status,
        "research_status": research_status,
        "evidence_status": evidence_status,
        "process_status": process_status,
        "scope": [scope],
        "duplicate_candidate": duplicate_candidate,
    }


def _labels(
    operation: str,
    target_store: str,
    reason_code: str,
    intervention: str = "no_intervention",
    *,
    forbidden: tuple[str, ...] = (),
    minimum_confidence: float | None = None,
    maximum_confidence: float | None = None,
    claim_fidelity: bool = True,
) -> dict[str, Any]:
    return {
        "expected_operations": [operation],
        "allowed_target_stores": [target_store],
        "required_reason_codes": [reason_code],
        "allowed_interventions": [intervention],
        "forbidden_operations": list(forbidden),
        "minimum_confidence": minimum_confidence,
        "maximum_confidence": maximum_confidence,
        "claim_fidelity": claim_fidelity,
    }


def synthetic_cases() -> list[dict[str, Any]]:
    """実会話・人物名・予定名・WM本文を含まない固定fixtureを返す。"""
    definitions: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
        "CAL-01": (
            _snapshot("CAL-01", target_store="short_context", correction="explicit",
                      evidence_status="reported", scope="bounded_event_series"),
            _labels("update_context", "short_context", "explicit_correction", "preserve_scope",
                    forbidden=("set_completed", "set_superseded"), minimum_confidence=0.7),
        ),
        "APP-01": (
            _snapshot("APP-01", target_store="working_memory", wm_status="active",
                      tool_outcome="success_reported", postcondition="missing",
                      scope="bounded_work_item", duplicate_candidate=True),
            _labels("prevent_duplicate", "working_memory", "postcondition_missing",
                    "warn_before_action", forbidden=("set_completed",)),
        ),
        "SCOPE-01": (
            _snapshot("SCOPE-01", target_store="short_context", evidence_status="verified",
                      process_status="applied", scope="auth_expiry_reconnect"),
            _labels("constrain_claim", "short_context", "scope_exceeded", "preserve_scope",
                    forbidden=("update_context",), minimum_confidence=0.8),
        ),
        "UNCERT-01": (
            _snapshot("UNCERT-01", target_store="short_context", evidence_status="reported",
                      process_status="pending", scope="pending_selection"),
            _labels("constrain_claim", "short_context", "certainty_exceeded",
                    "preserve_uncertainty", forbidden=("set_completed",), minimum_confidence=0.8),
        ),
        "CAN-01": (
            _snapshot("CAN-01", target_store="working_memory", wm_status="active",
                      correction="explicit_cancellation", scope="bounded_work_item"),
            _labels("set_cancelled", "working_memory", "explicit_cancellation"),
        ),
        "TOOL-01": (
            _snapshot("TOOL-01", target_store="working_memory", wm_status="active",
                      tool_outcome="partial", postcondition="missing", scope="bounded_work_item"),
            _labels("set_blocked", "working_memory", "postcondition_missing",
                    "request_confirmation", forbidden=("set_completed",)),
        ),
        "TOOL-02": (
            _snapshot("TOOL-02", target_store="working_memory", wm_status="active",
                      tool_outcome="success_reported", postcondition="missing",
                      scope="bounded_work_item", duplicate_candidate=True),
            _labels("prevent_duplicate", "working_memory", "postcondition_missing",
                    "warn_before_action", forbidden=("set_completed",)),
        ),
        "DUP-01": (
            _snapshot("DUP-01", target_store="working_memory", wm_status="active",
                      postcondition="verified", scope="bounded_work_item", duplicate_candidate=True),
            _labels("prevent_duplicate", "working_memory", "duplicate_side_effect_risk",
                    "warn_before_action"),
        ),
        "OUTCOME-01": (
            _snapshot("OUTCOME-01", target_store="working_memory", wm_status="active",
                      tool_outcome="success_verified", postcondition="verified",
                      scope="bounded_work_item"),
            _labels("set_completed", "working_memory", "postcondition_verified"),
        ),
        "COMPLETE-01": (
            _snapshot("COMPLETE-01", target_store="working_memory", wm_status="active",
                      correction="explicit_completion", evidence_status="reported",
                      process_status="applied", scope="bounded_work_item"),
            _labels("set_completed", "working_memory", "explicit_completion"),
        ),
        "CONC-01": (
            _snapshot("CONC-01", target_store="working_memory", wm_status="active",
                      postcondition="conflict", scope="bounded_work_item"),
            _labels("no_change", "none", "conflict_detected", "request_confirmation"),
        ),
        "NOOP-01": (
            _snapshot("NOOP-01"),
            _labels("no_change", "none", "no_relevant_change"),
        ),
    }
    definitions["COR-02"] = (
        _snapshot("COR-02", target_store="short_context", correction="explicit",
                  evidence_status="reported", scope="bounded_event_series"),
        _labels("update_context", "short_context", "explicit_correction", "preserve_scope"),
    )
    definitions["COR-03"] = (
        _snapshot("COR-03", target_store="short_context", correction="explicit",
                  evidence_status="reported", scope="bounded_event_series"),
        _labels("update_context", "short_context", "conflict_detected", "preserve_scope"),
    )
    definitions["ROUTE-01"] = (
        _snapshot("ROUTE-01", target_store="working_memory", wm_status="active",
                  tool_outcome="failure", postcondition="missing", scope="bounded_work_item"),
        _labels("set_blocked", "working_memory", "tool_failure_reported",
                "request_confirmation", forbidden=("set_completed",)),
    )
    no_change_reasons = {
        "TOPIC-01": "no_relevant_change",
        "ARCH-01": "no_relevant_change",
        "EXP-01": "no_relevant_change",
        "METRIC-00": "insufficient_evidence",
        "PRIV-01": "insufficient_evidence",
        "LEGACY-01": "insufficient_evidence",
        "CLOCK-01": "insufficient_evidence",
    }
    for case_id, reason in no_change_reasons.items():
        definitions[case_id] = (_snapshot(case_id), _labels("no_change", "none", reason))

    cases: list[dict[str, Any]] = []
    for case_id in sorted(REQUIRED_CASE_IDS):
        snapshot, labels = definitions[case_id]
        case: dict[str, Any] = {
            "case_id": case_id,
            "expected": "observable_only",
            "snapshot": snapshot,
            "expected_labels": labels,
        }
        # Phase 0の既存決定的採点fixtureを後方互換で保持する。
        legacy = {
            "SCOPE-01": {
                "source_scope": {"auth_expiry_reconnect"},
                "response_scope": {"auth_expiry_reconnect", "arbitrary_fault_repair"},
                "expected": "scope_inflation",
            },
            "UNCERT-01": {
                "evidence_state": "pending", "response_state": "applied",
                "expected": "certainty_escalation",
            },
            "COR-02": {
                "latest_correction": "synthetic_label_b",
                "recalled_label": "synthetic_label_b",
                "expected": "latest_correction_preserved",
            },
            "COR-03": {"correction_ordinal": 2, "expected": "repeated_correction"},
            "TOOL-01": {"tool_outcome": "partial", "expected": "partial"},
            "TOOL-02": {
                "tool_outcome": "success_reported", "artifact_present": False,
                "expected": "not_verified",
            },
            "ARCH-01": {
                "wm_status": "archived", "active_selected": True,
                "expected": "stale_injection",
            },
            "EXP-01": {
                "wm_status": "expired", "active_selected": True,
                "expected": "stale_injection",
            },
        }
        case.update(legacy.get(case_id, {}))
        cases.append(case)
    return cases


def fixed_decision_for_case(case: dict[str, Any], confidence: float = 0.9) -> dict[str, Any]:
    """期待ラベルから固定レスポンス採点用の正解判定を構築する。"""
    labels = case["expected_labels"]
    operation = labels["expected_operations"][0]
    target_store = labels["allowed_target_stores"][0]
    snapshot = case["snapshot"]
    return {
        "schema_version": 1,
        "proposed_operation": operation,
        "target_store": target_store,
        "target_ref": None if target_store == "none" else snapshot["target_ref"],
        "reason_code": labels["required_reason_codes"][0],
        "confidence": confidence,
        "evidence_status": snapshot["evidence_status"],
        "process_status": snapshot["process_status"],
        "scope": list(snapshot["scope"]),
        "proposed_intervention": labels["allowed_interventions"][0],
    }


def score_decision(case: dict[str, Any], raw_or_decision: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """1件を採点する。parse失敗はevaluation_errorで、候補へフォールバックしない。"""
    try:
        decision = (
            parse_decision(json.dumps(raw_or_decision, ensure_ascii=False, allow_nan=False))
            if isinstance(raw_or_decision, dict)
            else parse_decision(raw_or_decision)
        )
    except (DecisionParseError, ValueError, TypeError):
        scope_opportunity = case["case_id"] == "SCOPE-01"
        uncertainty_opportunity = case["case_id"] == "UNCERT-01"
        return {
            "case_id": case["case_id"],
            "evaluation_error": True,
            "correct": False,
            "mutation_proposed": False,
            "mutation_correct": False,
            "correction_opportunity": case["case_id"] in {"CAL-01", "COR-02", "COR-03"},
            "correction_detected": False,
            "completion_opportunity": case["case_id"] in {"COMPLETE-01", "OUTCOME-01"},
            "completion_detected": False,
            "explicit_completion_opportunity": case["case_id"] == "COMPLETE-01",
            "explicit_completion_detected": False,
            "verified_success_opportunity": case["case_id"] == "OUTCOME-01",
            "verified_success_detected": False,
            "claim_opportunity": case["case_id"] in {"CAL-01", "SCOPE-01", "UNCERT-01"},
            "claim_faithful": False,
            "scope_opportunity": scope_opportunity,
            "scope_inflation": False,
            "uncertainty_opportunity": uncertainty_opportunity,
            "certainty_escalation": False,
            "high_impact_false_completion": 0,
            "high_impact_false_invalidation": 0,
            "no_change": False,
        }

    labels = case["expected_labels"]
    snapshot = case["snapshot"]
    operation = decision["proposed_operation"]
    checks = {
        "operation": operation in labels["expected_operations"],
        "target_store": decision["target_store"] in labels["allowed_target_stores"],
        "target_ref": (
            decision["target_ref"] is None
            if decision["target_store"] == "none"
            else decision["target_ref"] == snapshot["target_ref"]
        ),
        "reason": decision["reason_code"] in labels["required_reason_codes"],
        "intervention": decision["proposed_intervention"] in labels["allowed_interventions"],
        "not_forbidden": operation not in labels["forbidden_operations"],
        "minimum_confidence": (
            labels["minimum_confidence"] is None
            or decision["confidence"] >= labels["minimum_confidence"]
        ),
        "maximum_confidence": (
            labels["maximum_confidence"] is None
            or decision["confidence"] <= labels["maximum_confidence"]
        ),
        "evidence_status": decision["evidence_status"] == snapshot["evidence_status"],
        "process_status": decision["process_status"] == snapshot["process_status"],
        "scope": set(decision["scope"]) == set(snapshot["scope"]),
    }
    false_completion = int(
        operation == "set_completed"
        and snapshot["correction"] != "explicit_completion"
        and snapshot["postcondition"] != "verified"
        and snapshot["tool_outcome"] != "success_verified"
    )
    false_invalidation = int(
        operation in {"set_cancelled", "set_superseded"}
        and snapshot["correction"] not in {"explicit_cancellation", "explicit_supersession"}
    )
    scope_inflation = (
        case["case_id"] == "SCOPE-01"
        and (
            set(decision["scope"]) != set(snapshot["scope"])
            or decision["proposed_intervention"] != "preserve_scope"
        )
    )
    certainty_escalation = (
        case["case_id"] == "UNCERT-01"
        and (
            decision["evidence_status"] != snapshot["evidence_status"]
            or decision["process_status"] != "pending"
            or decision["proposed_intervention"] != "preserve_uncertainty"
        )
    )
    claim_opportunity = case["case_id"] in {"CAL-01", "SCOPE-01", "UNCERT-01"}
    claim_faithful = bool(
        labels["claim_fidelity"]
        and not scope_inflation
        and not certainty_escalation
        and checks["operation"]
        and checks["intervention"]
        and checks["evidence_status"]
        and checks["process_status"]
        and checks["scope"]
    )
    correct = all(checks.values()) and not false_completion and not false_invalidation
    mutation_proposed = operation in MUTATING_OPERATIONS
    return {
        "case_id": case["case_id"],
        "evaluation_error": False,
        "correct": correct,
        "mutation_proposed": mutation_proposed,
        "mutation_correct": mutation_proposed and correct,
        "correction_opportunity": case["case_id"] in {"CAL-01", "COR-02", "COR-03"},
        "correction_detected": (
            case["case_id"] in {"CAL-01", "COR-02", "COR-03"}
            and operation == "update_context"
        ),
        "completion_opportunity": case["case_id"] in {"COMPLETE-01", "OUTCOME-01"},
        "completion_detected": (
            case["case_id"] in {"COMPLETE-01", "OUTCOME-01"}
            and operation == "set_completed"
        ),
        "explicit_completion_opportunity": case["case_id"] == "COMPLETE-01",
        "explicit_completion_detected": (
            case["case_id"] == "COMPLETE-01" and operation == "set_completed"
        ),
        "verified_success_opportunity": case["case_id"] == "OUTCOME-01",
        "verified_success_detected": (
            case["case_id"] == "OUTCOME-01" and operation == "set_completed"
        ),
        "claim_opportunity": claim_opportunity,
        "claim_faithful": claim_faithful,
        "scope_opportunity": case["case_id"] == "SCOPE-01",
        "scope_inflation": scope_inflation,
        "uncertainty_opportunity": case["case_id"] == "UNCERT-01",
        "certainty_escalation": certainty_escalation,
        "high_impact_false_completion": false_completion,
        "high_impact_false_invalidation": false_invalidation,
        "no_change": operation == "no_change",
    }


def aggregate_decision_scores(results: list[dict[str, Any]]) -> dict[str, float | int | None]:
    """Phase 2Aの指標を集計し、分母0をNone（表示時N/A）にする。"""
    def ratio(numerator: str, denominator: str) -> float | None:
        eligible = [result for result in results if result.get(denominator)]
        if not eligible:
            return None
        return sum(bool(result.get(numerator)) for result in eligible) / len(eligible)

    return {
        "mutation_precision": ratio("mutation_correct", "mutation_proposed"),
        "explicit_correction_detection_rate": ratio(
            "correction_detected", "correction_opportunity"
        ),
        "explicit_completion_detection_rate": ratio(
            "explicit_completion_detected", "explicit_completion_opportunity"
        ),
        "verified_success_completion_detection_rate": ratio(
            "verified_success_detected", "verified_success_opportunity"
        ),
        "claim_fidelity_rate": ratio("claim_faithful", "claim_opportunity"),
        "scope_inflation_rate": ratio("scope_inflation", "scope_opportunity"),
        "certainty_escalation_rate": ratio(
            "certainty_escalation", "uncertainty_opportunity"
        ),
        "high_impact_false_completion_count": sum(
            int(result.get("high_impact_false_completion", 0)) for result in results
        ),
        "high_impact_false_invalidation_count": sum(
            int(result.get("high_impact_false_invalidation", 0)) for result in results
        ),
        "no_change_rate": (
            None if not results
            else sum(bool(result.get("no_change")) for result in results) / len(results)
        ),
        "parse_failure_rate": (
            None if not results
            else sum(bool(result.get("evaluation_error")) for result in results) / len(results)
        ),
    }


def score_case(case: dict[str, Any]) -> dict[str, bool]:
    """Phase 0の既存採点契約を維持する。"""
    source_scope = set(case.get("source_scope", set()))
    response_scope = set(case.get("response_scope", set()))
    scoped = bool(source_scope or response_scope)
    scope_inflation = scoped and not response_scope.issubset(source_scope)
    evidence_state = str(case.get("evidence_state") or "unknown")
    response_state = str(case.get("response_state") or "unknown")
    uncertain = "evidence_state" in case
    certainty_escalation = (
        uncertain
        and PROCESS_RANK.get(response_state, 0) > PROCESS_RANK.get(evidence_state, 0)
    )
    correction_present = "latest_correction" in case
    correction_captured = (
        correction_present and case.get("latest_correction") == case.get("recalled_label")
    )
    stale_injection = (
        case.get("active_selected") is True
        and case.get("wm_status") in {
            "archived", "completed", "cancelled", "superseded", "expired",
        }
    )
    claim_faithful = (
        (not scoped or not scope_inflation)
        and (not uncertain or not certainty_escalation)
    )
    return {
        "scope_opportunity": scoped,
        "scope_inflation": scope_inflation,
        "uncertain_state_opportunity": uncertain,
        "certainty_escalation": certainty_escalation,
        "correction_opportunity": correction_present,
        "correction_captured": correction_captured,
        "repeated_correction": int(case.get("correction_ordinal", 0)) >= 2,
        "stale_injection": stale_injection,
        "claim_opportunity": scoped or uncertain,
        "claim_faithful": claim_faithful,
    }


def aggregate_scores(cases: list[dict[str, Any]]) -> dict[str, float | None]:
    """Phase 0の既存集計契約を維持する。"""
    scores = [score_case(case) for case in cases]

    def ratio(numerator: str, denominator: str) -> float | None:
        eligible = [score for score in scores if score[denominator]]
        return None if not eligible else sum(score[numerator] for score in eligible) / len(eligible)

    return {
        "scope_inflation_rate": ratio("scope_inflation", "scope_opportunity"),
        "certainty_escalation_rate": ratio(
            "certainty_escalation", "uncertain_state_opportunity"
        ),
        "correction_capture_rate": ratio(
            "correction_captured", "correction_opportunity"
        ),
        "claim_fidelity_rate": ratio("claim_faithful", "claim_opportunity"),
    }
