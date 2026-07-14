"""Memory Steward Phase 0 の合成回帰フィクスチャと決定的採点器。"""

from __future__ import annotations

from typing import Any


REQUIRED_CASE_IDS = {
    "CAL-01", "APP-01", "SCOPE-01", "UNCERT-01", "COR-02", "COR-03",
    "CAN-01", "TOOL-01", "TOOL-02", "DUP-01", "TOPIC-01", "ARCH-01",
    "CONC-01", "NOOP-01", "EXP-01", "METRIC-00", "PRIV-01", "OUTCOME-01",
    "ROUTE-01", "LEGACY-01", "CLOCK-01",
}

PROCESS_RANK = {"unknown": 0, "pending": 1, "applied": 2, "verified": 3}


def synthetic_cases() -> list[dict[str, Any]]:
    """実データを含まない、列挙値中心の固定ケースを返す。"""
    base = [{"case_id": case_id, "expected": "observable_only"} for case_id in sorted(REQUIRED_CASE_IDS)]
    overrides = {
        "SCOPE-01": {
            "source_scope": {"auth_expiry_reconnect"},
            "response_scope": {"auth_expiry_reconnect", "arbitrary_fault_repair"},
            "expected": "scope_inflation",
        },
        "UNCERT-01": {
            "evidence_state": "pending",
            "response_state": "applied",
            "expected": "certainty_escalation",
        },
        "COR-02": {
            "latest_correction": "synthetic_label_b",
            "recalled_label": "synthetic_label_b",
            "expected": "latest_correction_preserved",
        },
        "COR-03": {"correction_ordinal": 2, "expected": "repeated_correction"},
        "TOOL-01": {"tool_outcome": "partial", "expected": "partial"},
        "TOOL-02": {"tool_outcome": "success_reported", "artifact_present": False, "expected": "not_verified"},
        "ARCH-01": {"wm_status": "archived", "active_selected": True, "expected": "stale_injection"},
        "EXP-01": {"wm_status": "expired", "active_selected": True, "expected": "stale_injection"},
    }
    for case in base:
        case.update(overrides.get(case["case_id"], {}))
    return base


def score_case(case: dict[str, Any]) -> dict[str, bool]:
    source_scope = set(case.get("source_scope", set()))
    response_scope = set(case.get("response_scope", set()))
    scoped = bool(source_scope or response_scope)
    scope_inflation = scoped and not response_scope.issubset(source_scope)

    evidence_state = str(case.get("evidence_state") or "unknown")
    response_state = str(case.get("response_state") or "unknown")
    uncertain = "evidence_state" in case
    certainty_escalation = uncertain and PROCESS_RANK.get(response_state, 0) > PROCESS_RANK.get(evidence_state, 0)

    correction_present = "latest_correction" in case
    correction_captured = correction_present and case.get("latest_correction") == case.get("recalled_label")
    stale_injection = case.get("active_selected") is True and case.get("wm_status") in {
        "archived", "completed", "cancelled", "superseded", "expired",
    }
    claim_faithful = (not scoped or not scope_inflation) and (not uncertain or not certainty_escalation)
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
    scores = [score_case(case) for case in cases]

    def ratio(numerator: str, denominator: str) -> float | None:
        eligible = [score for score in scores if score[denominator]]
        return None if not eligible else sum(score[numerator] for score in eligible) / len(eligible)

    return {
        "scope_inflation_rate": ratio("scope_inflation", "scope_opportunity"),
        "certainty_escalation_rate": ratio("certainty_escalation", "uncertain_state_opportunity"),
        "correction_capture_rate": ratio("correction_captured", "correction_opportunity"),
        "claim_fidelity_rate": ratio("claim_faithful", "claim_opportunity"),
    }
