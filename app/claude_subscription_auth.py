"""[SEALED] Claude SDK経路は封印中（削除禁止）。ADR: docs/decisions/010_claude_sdk_path_sealed_not_deleted.md

Authentication and environment helpers for Claude subscription SDK calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, MutableMapping


CONFIG_TOKEN_KEY = "claude_subscription_oauth_token"
CLAUDE_OAUTH_ENV_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"


@dataclass(frozen=True)
class ClaudeSubscriptionAuth:
    """Resolved authentication state for a Claude Agent SDK subprocess."""

    env: dict[str, str]
    source: str
    has_oauth_token: bool


def _clean_token(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def get_configured_oauth_token(config: Mapping[str, object] | None = None) -> str:
    """Return the Nexus Ark stored Claude subscription OAuth token, if present."""
    if config is None:
        import config_manager

        config = config_manager.CONFIG_GLOBAL or {}
    return _clean_token(config.get(CONFIG_TOKEN_KEY))


def build_claude_subscription_env(
    oauth_token: str | None = None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an SDK subprocess env that cannot fall back to Anthropic API-key billing.

    Claude Agent SDK merges options.env over os.environ. Therefore API-key env
    vars must be overwritten with empty strings instead of only being removed.
    """
    env: MutableMapping[str, str] = dict(base_env if base_env is not None else os.environ)
    env[ANTHROPIC_API_KEY_ENV] = ""
    env[ANTHROPIC_AUTH_TOKEN_ENV] = ""

    cleaned_token = _clean_token(oauth_token)
    if cleaned_token:
        env[CLAUDE_OAUTH_ENV_KEY] = cleaned_token
    return dict(env)


def resolve_claude_subscription_auth(
    config: Mapping[str, object] | None = None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> ClaudeSubscriptionAuth:
    """Resolve Claude subscription auth using Nexus settings first, then CLI login."""
    token = get_configured_oauth_token(config)
    env = build_claude_subscription_env(token, base_env=base_env)
    if token:
        source = "nexus_config_oauth_token"
    elif env.get(CLAUDE_OAUTH_ENV_KEY):
        source = "environment_oauth_token"
    else:
        source = "claude_code_login"
    return ClaudeSubscriptionAuth(env=env, source=source, has_oauth_token=bool(token))
