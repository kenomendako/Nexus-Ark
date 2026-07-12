"""Shared path containment helpers for project-scoped tools."""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from pathlib import Path
from typing import Iterable


READ_ONLY_TOOL_NAMES = ("Read", "Glob", "Grep")
WRITE_TOOL_NAMES = ("Edit", "Write")
SHELL_TOOL_NAMES = ("Bash",)


def normalize_exclude_list(values: Iterable[str] | str | None) -> list[str]:
    if isinstance(values, str):
        return [part.strip() for part in values.split(",") if part.strip()]
    return [str(part).strip() for part in (values or []) if str(part).strip()]


def resolve_project_path(root_path: str, raw_path: str | None = None) -> Path:
    root = Path(root_path).expanduser().resolve()
    if raw_path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.resolve()
    return root


def is_within_root(path: str | Path, root_path: str | Path) -> bool:
    try:
        root = Path(root_path).expanduser().resolve()
        target = Path(path).expanduser().resolve()
        return os.path.commonpath([str(root), str(target)]) == str(root)
    except Exception:
        return False


def should_exclude_path(
    path: str | Path,
    root_path: str | Path,
    exclude_dirs: Iterable[str] | str | None = None,
    exclude_files: Iterable[str] | str | None = None,
) -> bool:
    root = Path(root_path).expanduser().resolve()
    target = Path(path).expanduser().resolve()
    if not is_within_root(target, root):
        return True

    exclude_dir_names = set(normalize_exclude_list(exclude_dirs))
    exclude_file_patterns = normalize_exclude_list(exclude_files)
    rel_path = os.path.relpath(str(target), str(root))
    parts = [] if rel_path == "." else rel_path.split(os.sep)

    for part in parts:
        if part in exclude_dir_names:
            return True

    filename = target.name
    for pattern in exclude_file_patterns:
        if fnmatch.fnmatch(filename, pattern):
            return True
    return False


def explain_path_policy_denial(
    path: str | Path,
    root_path: str | Path,
    exclude_dirs: Iterable[str] | str | None = None,
    exclude_files: Iterable[str] | str | None = None,
) -> str | None:
    root = Path(root_path).expanduser().resolve()
    target = Path(path).expanduser().resolve()
    if not is_within_root(target, root):
        return f"Nexus Ark policy: path is outside the delegated workspace ({target})."
    if should_exclude_path(target, root, exclude_dirs, exclude_files):
        return f"Nexus Ark policy: path is excluded by project_explorer settings ({target})."
    return None


def check_delegation_tool_permission(
    tool_name: str,
    tool_input: dict,
    *,
    tier: str,
    workspace: str,
    exclude_dirs: Iterable[str] | str | None = None,
    exclude_files: Iterable[str] | str | None = None,
    app_root: str | Path | None = None,
    extra_scopes: Iterable | None = None,
) -> str | None:
    """Return a Nexus Ark policy denial message for a delegated tool call.

    This helper is shared by the Claude SDK backend and the native backend so
    both paths enforce the same tier and workspace containment rules.

    When ``extra_scopes`` is provided (dual-scope delegation, e.g. a read-only
    project root alongside an atelier write root), permission is decided per path
    by the most specific (deepest) scope that contains it, each scope keeping its
    OWN configured tier. When ``extra_scopes`` is empty the behaviour is identical
    to the original single-workspace logic.
    """
    if extra_scopes:
        primary = (workspace, tier, exclude_dirs, exclude_files)
        return _check_multi_scope(tool_name, tool_input, primary=primary, extra_scopes=extra_scopes, app_root=app_root)
    if tool_name in READ_ONLY_TOOL_NAMES:
        return _read_tool_denial(tool_name, tool_input, workspace, exclude_dirs, exclude_files)
    if tool_name in WRITE_TOOL_NAMES:
        if tier not in {"write", "full"}:
            return "Nexus Ark policy: write tools require delegation permission tier 2 or higher."
        raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
        if not raw_path:
            return "Nexus Ark policy: write tool call did not include a target path."
        target = resolve_project_path(workspace, str(raw_path))
        return explain_path_policy_denial(target, workspace, exclude_dirs, exclude_files)
    if tool_name in SHELL_TOOL_NAMES and tier != "full":
        return "Nexus Ark policy: Bash requires delegation permission tier 3."
    if tool_name in SHELL_TOOL_NAMES and tier == "full":
        return _bash_redline_denial(tool_input, workspace, exclude_dirs, exclude_files, app_root=app_root)
    return None


def _bash_redline_denial(
    tool_input: dict,
    workspace: str,
    exclude_dirs: Iterable[str] | str | None,
    exclude_files: Iterable[str] | str | None,
    *,
    app_root: str | Path | None = None,
) -> str | None:
    """Best-effort Bash guard for obvious red-line paths and secrets.

    This is not a shell sandbox and does not attempt full shell parsing. It only
    blocks clear attempts to reference secrets, persona memory/private areas, or
    paths outside the delegated workspace before the backend receives the call.
    """
    command = str(tool_input.get("command") or "")
    if not command.strip():
        return None
    static_denial = _bash_static_pattern_denial(command)
    if static_denial:
        return static_denial

    for token in _bash_path_tokens(command):
        denial = _bash_path_token_denial(token, workspace, exclude_dirs, exclude_files, app_root=app_root)
        if denial:
            return denial
    return None


def _bash_path_tokens(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        parts = re.split(r"\s+", command)
    tokens: list[str] = []
    path_next = False
    for part in parts:
        token = str(part).strip().strip(";|&(){}[]<>")
        if not token:
            continue
        lower = token.lower()
        if path_next:
            tokens.append(token)
            path_next = False
            continue
        if lower in {"cd", "pushd"}:
            path_next = True
            continue
        if "=" in token and not token.startswith(("/", "./", "../", "~")):
            _, maybe_path = token.split("=", 1)
            token = maybe_path.strip()
        if _looks_like_path_token(token):
            tokens.append(token)
    return tokens


def _looks_like_path_token(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if token in {".", "..", "~"}:
        return True
    if token.startswith(("/", "./", "../", "~/")):
        return True
    return "/" in token or "\\" in token


def _bash_path_token_denial(
    token: str,
    workspace: str,
    exclude_dirs: Iterable[str] | str | None,
    exclude_files: Iterable[str] | str | None,
    *,
    app_root: str | Path | None = None,
) -> str | None:
    try:
        target = resolve_project_path(workspace, token)
    except Exception:
        return f"Nexus Ark policy: Bash command contains an unresolved path-like token ({token})."
    denial = explain_path_policy_denial(target, workspace, exclude_dirs, exclude_files)
    if denial:
        return f"Nexus Ark policy: Bash command path is not allowed: {denial}"

    if app_root is not None:
        try:
            resolved = target.resolve()
            workspace_root = Path(workspace).resolve()
            repo_root = Path(app_root).resolve()
            if resolved == repo_root and resolved != workspace_root:
                return "Nexus Ark policy: Bash command references the Nexus Ark repository root outside the delegated workspace."
        except Exception:
            return None
    return None


def _read_tool_denial(
    tool_name: str,
    tool_input: dict,
    workspace: str,
    exclude_dirs: Iterable[str] | str | None,
    exclude_files: Iterable[str] | str | None,
) -> str | None:
    raw_path = _read_tool_path_candidate(tool_name, tool_input)
    if raw_path is None:
        return None
    if raw_path == "":
        return f"Nexus Ark policy: {tool_name} tool call did not include a target path."
    target = resolve_project_path(workspace, raw_path)
    return explain_path_policy_denial(target, workspace, exclude_dirs, exclude_files)


def _read_tool_path_candidate(tool_name: str, tool_input: dict) -> str | None:
    if tool_name == "Read":
        return str(tool_input.get("file_path") or tool_input.get("path") or "")
    if tool_name == "Grep":
        raw_path = tool_input.get("path")
        return str(raw_path) if raw_path else None
    if tool_name == "Glob":
        raw_path = tool_input.get("path")
        if raw_path:
            return str(raw_path)
        pattern = str(tool_input.get("pattern") or "").strip()
        if not pattern:
            return None
        return _glob_pattern_base_path(pattern)
    return None


def _bash_static_pattern_denial(command: str) -> str | None:
    """Block obvious secret/private references in a Bash command (scope-independent)."""
    lowered = command.lower()
    secret_patterns = (
        "config.json",
        ".env",
        "keys",
        "api_key",
        "secret",
        ".key",
        "token",
    )
    for pattern in secret_patterns:
        if pattern in lowered:
            return f"Nexus Ark policy: Bash command references a protected secret/config pattern ({pattern})."

    private_patterns = (
        "secret_diaries",
        "working_memory",
        "memory/",
        "memory\\",
        "/memory",
        "\\memory",
        " memory/",
        " memory\\",
    )
    for pattern in private_patterns:
        if pattern in lowered:
            return f"Nexus Ark policy: Bash command references protected persona/private data ({pattern.strip()})."
    return None


def _scope_fields(scope) -> tuple[str, str, object, object]:
    """Normalize a scope (DelegationScope dataclass or tuple) to (root, tier, ed, ef)."""
    if isinstance(scope, tuple):
        root, tier, ed, ef = (list(scope) + [None, None, None, None])[:4]
        return str(root), str(tier or "read"), ed, ef
    return (
        str(getattr(scope, "root", "")),
        str(getattr(scope, "tier", "read") or "read"),
        getattr(scope, "exclude_dirs", None),
        getattr(scope, "exclude_files", None),
    )


def _scope_decision(target: Path, scopes: list[tuple[str, str, object, object]]):
    """Pick the most specific (deepest root) scope that contains target without excluding it.

    Returns (scope_tuple, None) when allowed, or (None, denial_message) otherwise.
    """
    within_any = False
    best: tuple[str, str, object, object] | None = None
    best_len = -1
    for root, tier, ed, ef in scopes:
        try:
            root_resolved = Path(root).expanduser().resolve()
        except Exception:
            continue
        if is_within_root(target, root_resolved):
            within_any = True
            if not should_exclude_path(target, root_resolved, ed, ef):
                root_len = len(str(root_resolved))
                if root_len > best_len:
                    best = (str(root_resolved), tier, ed, ef)
                    best_len = root_len
    if best is not None:
        return best, None
    if within_any:
        return None, f"Nexus Ark policy: path is excluded by project_explorer settings ({target})."
    return None, f"Nexus Ark policy: path is outside the delegated workspace ({target})."


def _check_multi_scope(
    tool_name: str,
    tool_input: dict,
    *,
    primary: tuple[str, str, object, object],
    extra_scopes: Iterable,
    app_root: str | Path | None,
) -> str | None:
    """Per-scope permission check for dual/multi-scope delegation (most specific scope wins)."""
    primary_root = str(primary[0])
    scopes = [(_scope_fields(primary))] + [_scope_fields(scope) for scope in extra_scopes]

    if tool_name in READ_ONLY_TOOL_NAMES:
        candidate = _read_tool_path_candidate(tool_name, tool_input)
        if candidate is None:
            return None
        if candidate == "":
            return f"Nexus Ark policy: {tool_name} tool call did not include a target path."
        target = resolve_project_path(primary_root, candidate)
        scope, reason = _scope_decision(target, scopes)
        return None if scope is not None else reason

    if tool_name in WRITE_TOOL_NAMES:
        raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
        if not raw_path:
            return "Nexus Ark policy: write tool call did not include a target path."
        target = resolve_project_path(primary_root, str(raw_path))
        scope, reason = _scope_decision(target, scopes)
        if scope is None:
            return reason
        if scope[1] not in {"write", "full"}:
            return "Nexus Ark policy: write tools require delegation permission tier 2 or higher."
        return None

    if tool_name in SHELL_TOOL_NAMES:
        full_scopes = [scope for scope in scopes if scope[1] == "full"]
        if not full_scopes:
            return "Nexus Ark policy: Bash requires delegation permission tier 3."
        return _bash_redline_denial_multi(tool_input, full_scopes, primary_root, app_root=app_root)
    return None


def _bash_redline_denial_multi(
    tool_input: dict,
    full_scopes: list[tuple[str, str, object, object]],
    primary_root: str,
    *,
    app_root: str | Path | None = None,
) -> str | None:
    """Bash guard for multi-scope delegation: path tokens must live inside a full-tier scope."""
    command = str(tool_input.get("command") or "")
    if not command.strip():
        return None
    static_denial = _bash_static_pattern_denial(command)
    if static_denial:
        return static_denial

    for token in _bash_path_tokens(command):
        try:
            target = resolve_project_path(primary_root, token)
        except Exception:
            return f"Nexus Ark policy: Bash command contains an unresolved path-like token ({token})."
        scope, reason = _scope_decision(target, full_scopes)
        if scope is None:
            return f"Nexus Ark policy: Bash command path is not allowed: {reason}"
    return None


def _glob_pattern_base_path(pattern: str) -> str | None:
    first_glob = min((idx for idx in (pattern.find("*"), pattern.find("?"), pattern.find("[")) if idx >= 0), default=-1)
    if first_glob < 0:
        return os.path.dirname(pattern) or pattern
    static_prefix = pattern[:first_glob]
    stripped_prefix = static_prefix.rstrip("/\\")
    if stripped_prefix and static_prefix != stripped_prefix:
        base = stripped_prefix
    else:
        base = os.path.dirname(stripped_prefix)
    if base:
        return base
    if Path(pattern).is_absolute():
        return os.path.sep
    return None
