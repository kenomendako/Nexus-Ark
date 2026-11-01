"""Backend-neutral contracts for delegated agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

# Delegation terminology:
# - permission tier: read/write/full。ツール・ワークスペース権限。保存キー permission_tier / extra_scopes[].tier。
# - model tier: fast/balanced/deep。タスク種別から委任実行モデルを選ぶ枠。config の model_tiers / task_model_tiers。
# - limit profile: local/cloud_light/cloud_heavy。モデル別の max_turns/timeout_seconds 自動調整。config_manager.DELEGATION_LIMIT_PROFILES。


@dataclass(frozen=True)
class DelegationScope:
    """A single (root, tier) scope a delegated agent may operate within.

    The primary scope lives on AgentTaskSpec (workspace/permission_tier/excludes).
    extra_scopes carry additional roots (e.g. a read-only project root alongside an
    atelier write root). Each scope keeps its OWN configured tier — the most specific
    (deepest) scope containing a path governs that path's permission.
    """

    root: str
    # permission tier（read/write/full）。歴史的経緯で裸名。保存互換のため変更禁止。
    tier: str = "read"
    exclude_dirs: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentTaskSpec:
    task_description: str
    workspace: str
    exclude_dirs: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)
    permission_tier: str = "read"
    max_turns: int = 20
    timeout_seconds: int = 600
    room_name: str = ""
    task_id: str = ""
    expected_output: str = ""
    extra_scopes: list[DelegationScope] = field(default_factory=list)
    workspace_kind: str = "project"
    allow_web_tools: bool = False
    role: str = ""
    role_guidance: str = ""
    model_override: tuple[str, str, str] | None = None


@dataclass(frozen=True)
class AgentRunResult:
    assistant_text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentBackend(Protocol):
    async def run(
        self,
        spec: AgentTaskSpec,
        *,
        control: Any = None,
        sdk_factory: Callable[[], Any] | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> AgentRunResult:
        ...
