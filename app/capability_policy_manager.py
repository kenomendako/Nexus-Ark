"""Capability policy and audit manager for autonomous actions."""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import constants


class CapabilityPolicyManager:
    """Manages per-room capability policy and audit logs."""

    POLICY_VERSION = 1

    DEFAULT_CATEGORIES = {
        "world": {"mode": "allow", "risk": "low"},
        "memory": {"mode": "allow", "risk": "low"},
        "notes": {"mode": "allow", "risk": "low"},
        "web": {"mode": "allow", "risk": "low"},
        "time": {"mode": "allow", "risk": "low"},
        "autonomy": {"mode": "allow", "risk": "low"},
        "watchlist": {"mode": "allow", "risk": "low"},
        "items": {"mode": "allow", "risk": "low"},
        "chess": {"mode": "allow", "risk": "low"},
        "research": {"mode": "allow", "risk": "low"},
        "procedure": {"mode": "allow", "risk": "low"},
        "image": {"mode": "ask", "risk": "medium"},
        "roblox": {"mode": "ask", "risk": "medium"},
        "twitter": {"mode": "ask", "risk": "medium"},
        "discord": {"mode": "ask", "risk": "medium"},
        "custom": {"mode": "ask", "risk": "medium"},
        "browser": {"mode": "deny", "risk": "high"},
        "filesystem": {"mode": "deny", "risk": "high"},
        "shell": {"mode": "deny", "risk": "high"},
        "developer": {"mode": "deny", "risk": "high"},
        "external_post": {"mode": "deny", "risk": "high"},
    }

    VALID_MODES = {"allow", "ask", "deny"}
    VALID_RISKS = {"low", "medium", "high"}

    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.memory_dir = self.room_dir / "memory"
        self.policy_path = self.memory_dir / "capability_policy.json"
        self.audit_dir = self.memory_dir / "capability_audit"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    def read_policy(self) -> Dict[str, Any]:
        policy = self._load_policy()
        changed = self._normalize_policy(policy)
        if changed:
            self._save_policy(policy)
        return policy

    def request_approval(
        self,
        category: str,
        intent: str,
        details: str = "",
        risk_acknowledgement: str = "",
    ) -> Dict[str, Any]:
        policy = self.read_policy()
        normalized_category = self._safe_category(category)
        category_policy = self.get_category_policy(normalized_category, policy)
        mode = category_policy["mode"]
        risk = category_policy["risk"]
        request_id = self._make_request_id()

        if mode == "allow":
            status = "approved"
            decision = "自動許可"
        elif mode == "deny":
            status = "denied"
            decision = "高リスクまたは未許可カテゴリのため拒否"
        else:
            status = "pending"
            decision = "ユーザー承認待ち"

        request = {
            "request_id": request_id,
            "created_at": self._now(),
            "category": normalized_category,
            "mode": mode,
            "risk": risk,
            "intent": self._clean(intent),
            "details": self._clean(details),
            "risk_acknowledgement": self._clean(risk_acknowledgement),
            "status": status,
            "decision": decision,
        }

        if status == "pending":
            policy.setdefault("pending_requests", []).append(request)
            self._save_policy(policy)

        self.record_audit(
            category=normalized_category,
            action="request_approval",
            intent=intent,
            status=status,
            details=details,
            request_id=request_id,
        )
        return request

    def record_audit(
        self,
        category: str,
        action: str,
        intent: str,
        status: str,
        details: str = "",
        related_timeline_id: str = "",
        request_id: str = "",
    ) -> Dict[str, Any]:
        record = {
            "timestamp": self._now(),
            "room_name": self.room_name,
            "category": self._safe_category(category),
            "action": self._clean(action),
            "intent": self._clean(intent),
            "status": self._clean(status),
            "details": self._clean(details),
            "related_timeline_id": self._clean(related_timeline_id),
            "request_id": self._clean(request_id),
        }
        audit_path = self.audit_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {**record, "audit_path": str(audit_path)}

    def get_category_policy(self, category: str, policy: Dict[str, Any] | None = None) -> Dict[str, str]:
        policy = policy or self.read_policy()
        categories = policy.setdefault("categories", {})
        if category in categories:
            return self._normalized_category_policy(categories[category])
        default_mode = policy.get("default_mode", "ask")
        if default_mode not in self.VALID_MODES:
            default_mode = "ask"
        return {"mode": default_mode, "risk": "medium"}

    def _load_policy(self) -> Dict[str, Any]:
        if not self.policy_path.exists():
            policy = self._default_policy()
            self._save_policy(policy)
            return policy
        try:
            return json.loads(self.policy_path.read_text(encoding="utf-8"))
        except Exception:
            policy = self._default_policy()
            self._save_policy(policy)
            return policy

    def _default_policy(self) -> Dict[str, Any]:
        return {
            "version": self.POLICY_VERSION,
            "default_mode": "ask",
            "categories": dict(self.DEFAULT_CATEGORIES),
            "pending_requests": [],
            "audit": {"log_dir": "memory/capability_audit"},
            "rollback_policy": {
                "principle": "外部副作用を伴う操作は、実行前に承認状態を確認し、実行後に結果と戻し方を監査ログへ残す。",
                "required_for": ["twitter", "discord", "roblox", "custom", "browser", "filesystem", "shell", "external_post"],
            },
        }

    def _normalize_policy(self, policy: Dict[str, Any]) -> bool:
        changed = False
        if policy.get("version") != self.POLICY_VERSION:
            policy["version"] = self.POLICY_VERSION
            changed = True
        if policy.get("default_mode") not in self.VALID_MODES:
            policy["default_mode"] = "ask"
            changed = True
        categories = policy.setdefault("categories", {})
        for category, default_value in self.DEFAULT_CATEGORIES.items():
            if category not in categories:
                categories[category] = dict(default_value)
                changed = True
            else:
                normalized = self._normalized_category_policy(categories[category])
                if categories[category] != normalized:
                    categories[category] = normalized
                    changed = True
        policy.setdefault("pending_requests", [])
        policy.setdefault("audit", {"log_dir": "memory/capability_audit"})
        policy.setdefault("rollback_policy", self._default_policy()["rollback_policy"])
        return changed

    def _save_policy(self, policy: Dict[str, Any]) -> None:
        self.policy_path.write_text(
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _normalized_category_policy(self, value: Dict[str, Any]) -> Dict[str, str]:
        mode = str(value.get("mode", "ask")).strip().lower()
        risk = str(value.get("risk", "medium")).strip().lower()
        if mode not in self.VALID_MODES:
            mode = "ask"
        if risk not in self.VALID_RISKS:
            risk = "medium"
        return {"mode": mode, "risk": risk}

    def _safe_category(self, category: str) -> str:
        cleaned = self._clean(category).lower().replace(" ", "_")
        return "".join(ch for ch in cleaned if ch.isalnum() or ch in {"_", "-"}) or "custom"

    def _make_request_id(self) -> str:
        return f"capreq_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _clean(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return str(value).strip()
