"""Room-scoped persona contract helpers.

Persona contracts may contain private room/user-specific relationship details.
Keep defaults neutral and never bake a real user's contract into samples, tests,
or shared playbooks.
"""

from __future__ import annotations

from typing import Any

import config_manager


DEFAULT_PERSONA_CONTRACT_SAMPLE: dict[str, Any] = {
    "enabled": False,
    "persona_name": "Assistant",
    "user_display_name": "User",
    "address_terms": {
        "preferred": ["あなた"],
        "forbidden": [],
    },
    "required_terms": [],
    "forbidden_terms": [],
    "tone_rules": [
        "丁寧で落ち着いた口調",
        "ユーザーを急かさない",
    ],
    "validation": {
        "forbidden_terms": "error",
        "required_terms": "warning",
        "address_terms": "warning",
        "tone_rules": "warning",
    },
}

VALID_SEVERITIES = {"error", "warning", "info"}


def _as_list(value: Any, *, limit: int = 40) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    normalized: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


def _severity(value: Any, default: str = "warning") -> str:
    normalized = str(value or default).strip().lower()
    return normalized if normalized in VALID_SEVERITIES else default


def normalize_contract(raw: Any) -> dict[str, Any]:
    """Return a conservative, schema-stable persona contract dict."""
    if not isinstance(raw, dict):
        return dict(DEFAULT_PERSONA_CONTRACT_SAMPLE)

    address_terms = raw.get("address_terms") if isinstance(raw.get("address_terms"), dict) else {}
    validation = raw.get("validation") if isinstance(raw.get("validation"), dict) else {}
    contract = {
        "enabled": bool(raw.get("enabled", True)),
        "persona_name": str(raw.get("persona_name") or "").strip(),
        "user_display_name": str(raw.get("user_display_name") or "").strip(),
        "address_terms": {
            "preferred": _as_list(address_terms.get("preferred")),
            "forbidden": _as_list(address_terms.get("forbidden")),
        },
        "required_terms": _as_list(raw.get("required_terms")),
        "forbidden_terms": _as_list(raw.get("forbidden_terms")),
        "tone_rules": _as_list(raw.get("tone_rules")),
        "validation": {
            "forbidden_terms": _severity(validation.get("forbidden_terms"), "error"),
            "required_terms": _severity(validation.get("required_terms"), "warning"),
            "address_terms": _severity(validation.get("address_terms"), "warning"),
            "tone_rules": _severity(validation.get("tone_rules"), "warning"),
        },
    }
    return contract


def get_room_persona_contract(room_name: str) -> dict[str, Any]:
    """Load the effective room contract.

    The contract is expected under effective settings key ``persona_contract``,
    normally sourced from ``room_config.json`` ``override_settings``. Missing
    contracts are treated as disabled neutral defaults.
    """
    room = str(room_name or "").strip()
    if not room:
        return dict(DEFAULT_PERSONA_CONTRACT_SAMPLE)
    try:
        settings = config_manager.get_effective_settings(room)
    except Exception:
        return dict(DEFAULT_PERSONA_CONTRACT_SAMPLE)
    raw = settings.get("persona_contract") if isinstance(settings, dict) else None
    return normalize_contract(raw)


def has_active_contract(contract: dict[str, Any]) -> bool:
    if not bool(contract.get("enabled")):
        return False
    return any(
        [
            contract.get("persona_name"),
            contract.get("user_display_name"),
            contract.get("required_terms"),
            contract.get("forbidden_terms"),
            (contract.get("address_terms") or {}).get("preferred"),
            (contract.get("address_terms") or {}).get("forbidden"),
            contract.get("tone_rules"),
        ]
    )


def format_contract_for_delegation(room_name: str) -> str:
    """Format the active contract for delegated agents.

    This block is task-local guidance, not reusable sample material. It should
    not be copied into generic docs or templates.
    """
    contract = get_room_persona_contract(room_name)
    if not has_active_contract(contract):
        return ""
    address_terms = contract.get("address_terms") or {}
    lines = [
        "## Persona Contract for this room",
        "This contract may contain room-private wording preferences. Use it only for this task; do not copy it into generic samples, tests, or shared templates.",
    ]
    if contract.get("persona_name"):
        lines.append(f"- Persona name: {contract['persona_name']}")
    if contract.get("user_display_name"):
        lines.append(f"- User display name: {contract['user_display_name']}")
    if address_terms.get("preferred"):
        lines.append("- Preferred user address terms: " + ", ".join(address_terms["preferred"]))
    if address_terms.get("forbidden"):
        lines.append("- Forbidden user address terms: " + ", ".join(address_terms["forbidden"]))
    if contract.get("required_terms"):
        lines.append("- Required exact terms: " + ", ".join(contract["required_terms"]))
    if contract.get("forbidden_terms"):
        lines.append("- Forbidden exact terms: " + ", ".join(contract["forbidden_terms"]))
    if contract.get("tone_rules"):
        lines.append("- Tone rules:")
        lines.extend(f"  - {rule}" for rule in contract["tone_rules"])
    lines.extend(
        [
            "",
            "Before reporting completion, inspect visible UI text, errors, README/report text, and final summary against this contract.",
            "If an error-level wording violation remains, treat the task as unfinished and report the specific text/file to fix.",
        ]
    )
    return "\n".join(lines)


def validate_text_against_contract(text: str, room_name: str) -> dict[str, Any]:
    """Run deterministic term checks against the room contract."""
    contract = get_room_persona_contract(room_name)
    body = str(text or "")
    violations: list[dict[str, str]] = []
    if not has_active_contract(contract):
        return {"enabled": False, "violations": [], "contract": contract}

    validation = contract.get("validation") or {}
    for term in contract.get("forbidden_terms") or []:
        if term and term in body:
            violations.append({
                "severity": validation.get("forbidden_terms", "error"),
                "rule": "forbidden_terms",
                "term": term,
                "message": f"禁止語 `{term}` が含まれています。",
            })
    for term in (contract.get("address_terms") or {}).get("forbidden") or []:
        if term and term in body:
            violations.append({
                "severity": validation.get("address_terms", "warning"),
                "rule": "address_terms.forbidden",
                "term": term,
                "message": f"禁止された呼び名 `{term}` が含まれています。",
            })
    for term in contract.get("required_terms") or []:
        if term and term not in body:
            violations.append({
                "severity": validation.get("required_terms", "warning"),
                "rule": "required_terms",
                "term": term,
                "message": f"必須語 `{term}` が見つかりません。",
            })
    preferred = (contract.get("address_terms") or {}).get("preferred") or []
    if preferred and not any(term in body for term in preferred):
        violations.append({
            "severity": validation.get("address_terms", "warning"),
            "rule": "address_terms.preferred",
            "term": ", ".join(preferred),
            "message": "推奨される呼び名が見つかりません。",
        })

    return {"enabled": True, "violations": violations, "contract": contract}


def format_validation_result(result: dict[str, Any]) -> str:
    if not result.get("enabled"):
        return "このルームには有効な Persona Contract がありません。"
    violations = result.get("violations") or []
    if not violations:
        return "Persona Contract の機械チェックで違反は見つかりませんでした。"
    lines = ["Persona Contract の機械チェックで以下を検出しました。"]
    for item in violations:
        lines.append(
            f"- {str(item.get('severity') or 'warning').upper()} "
            f"{item.get('rule')}: {item.get('message')}"
        )
    if any(str(item.get("severity")) == "error" for item in violations):
        lines.append("error があるため、この成果物は未完了として扱ってください。")
    return "\n".join(lines)
