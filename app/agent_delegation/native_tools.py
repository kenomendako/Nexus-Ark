"""Native filesystem tools for the delegated agent backend.

These tools are intentionally not wired into the backend in Phase B. Phase C will
bind them to the native loop.
"""

from __future__ import annotations

import glob as globlib
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from agent_delegation import resource_limits
from agent_delegation.types import AgentTaskSpec
from tools.path_policy import check_delegation_tool_permission, resolve_project_path


logger = logging.getLogger(__name__)

MAX_NATIVE_TOOL_RESULT_CHARS = 20000
MAX_NATIVE_TOOL_RESULTS = 200
# 巨大走査の上流ガード（OOM/暴走予防）
MAX_NATIVE_READ_BYTES = 2_000_000  # 1ファイルを丸読みする上限（Read/Grep）
MAX_NATIVE_GREP_SCAN_FILES = 5000  # Grep でツリー走査するファイル数の上限
MAX_NATIVE_GLOB_SCAN = 20000  # Glob で評価する候補数の上限
NATIVE_BASH_TIMEOUT_SECONDS = 120


def build_native_tools(
    spec: AgentTaskSpec,
    *,
    app_root: str | Path | None = None,
    settings: dict[str, Any] | None = None,
) -> list[BaseTool]:
    """Build backend-neutral workspace tools for a native delegated agent."""
    context = _NativeToolContext(spec=spec, app_root=app_root, settings=settings)
    tools: list[BaseTool] = [
        StructuredTool.from_function(
            name="Read",
            func=context.read,
            description="Read a UTF-8 text file inside the delegated workspace.",
        ),
        StructuredTool.from_function(
            name="Glob",
            func=context.glob,
            description="List files inside the delegated workspace matching a glob pattern.",
        ),
        StructuredTool.from_function(
            name="Grep",
            func=context.grep,
            description="Search UTF-8 text files inside the delegated workspace for a regular expression.",
        ),
    ]
    # ツールの提供可否は全スコープ（primary＋extra_scopes）のティアの和で判断する。
    # 実際にどのパスへ書ける/Bashできるかは path_policy が各スコープのティアで個別に判定する。
    scope_tiers = {context.tier} | {
        str(getattr(scope, "tier", "read") or "read") for scope in (spec.extra_scopes or [])
    }
    has_write = bool(scope_tiers & {"write", "full"})
    has_full = "full" in scope_tiers
    if has_write:
        tools.extend(
            [
                StructuredTool.from_function(
                    name="Edit",
                    func=context.edit,
                    description="Replace text in a UTF-8 file inside a writable delegated scope.",
                ),
                StructuredTool.from_function(
                    name="Write",
                    func=context.write,
                    description="Write a UTF-8 text file inside a writable delegated scope.",
                ),
            ]
        )
    if has_full:
        tools.append(
            StructuredTool.from_function(
                name="Bash",
                func=context.bash,
                description="Run a shell command inside a full-tier delegated scope with resource limits and redline checks.",
            )
        )
    # Web ツールはファイルシステムに触れないためティアとは独立。allow_web_tools でのみ提供する。
    if bool(context.settings.get("allow_web_tools")):
        tools.extend(
            [
                StructuredTool.from_function(
                    name="WebSearch",
                    func=context.web_search,
                    description=(
                        "Search the web for up-to-date information. Returns summarized results with source links. "
                        "Use concise queries; call multiple times to investigate different angles."
                    ),
                ),
                StructuredTool.from_function(
                    name="WebFetch",
                    func=context.web_fetch,
                    description=(
                        "Fetch and read the textual content of a web page or PDF by URL. "
                        "Use after WebSearch to read a specific source in depth."
                    ),
                ),
            ]
        )
    return tools


class _NativeToolContext:
    def __init__(self, spec: AgentTaskSpec, app_root: str | Path | None, settings: dict[str, Any] | None):
        self.spec = spec
        self.app_root = app_root
        self.settings = settings or {}
        self.tier = str(spec.permission_tier or "read").strip().lower()

    def read(self, file_path: str) -> str:
        """Read a file from the delegated workspace."""
        denial = self._denial("Read", {"file_path": file_path})
        if denial:
            return denial
        try:
            target = self._resolve(file_path)
            if not target.exists():
                return f"Read error: file does not exist ({target})."
            if not target.is_file():
                return f"Read error: path is not a file ({target})."
            # 巨大ファイルを丸ごとメモリに載せない（先頭だけ読む）
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            if size > MAX_NATIVE_READ_BYTES:
                with target.open("r", encoding="utf-8", errors="replace") as f:
                    head = f.read(MAX_NATIVE_READ_BYTES)
                notice = f"\n... (file truncated: {size} bytes > {MAX_NATIVE_READ_BYTES} byte limit)"
                return _truncate_tool_result(head + notice)
            return _truncate_tool_result(target.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            return f"Read error: {type(exc).__name__}: {exc}"

    def glob(self, pattern: str, path: str | None = None) -> str:
        """List files matching a glob pattern in the delegated workspace."""
        tool_input: dict[str, Any] = {"pattern": pattern}
        if path is not None:
            tool_input["path"] = path
        denial = self._denial("Glob", tool_input)
        if denial:
            return denial
        try:
            matches = self._glob_matches(pattern, path)
            if not matches:
                return "No files matched."
            return _truncate_tool_result("\n".join(_relative_or_absolute(self.spec.workspace, match) for match in matches))
        except Exception as exc:
            return f"Glob error: {type(exc).__name__}: {exc}"

    def grep(self, pattern: str, path: str | None = None) -> str:
        """Search files for a regular expression in the delegated workspace."""
        tool_input: dict[str, Any] = {"pattern": pattern}
        if path is not None:
            tool_input["path"] = path
        denial = self._denial("Grep", tool_input)
        if denial:
            return denial
        try:
            regex = re.compile(pattern)
            base = self._resolve(path) if path else resolve_project_path(self.spec.workspace)
            # 全ファイルをリスト化せず遅延走査し、走査数・ファイルサイズに上限を設けて暴走を防ぐ
            candidates = iter([base]) if base.is_file() else base.rglob("*")
            lines: list[str] = []
            scanned = 0
            for file_path in candidates:
                if scanned >= MAX_NATIVE_GREP_SCAN_FILES:
                    lines.append(f"... scan stopped after {MAX_NATIVE_GREP_SCAN_FILES} files (絞り込みのため path/pattern を指定してください)")
                    break
                try:
                    if not file_path.is_file():
                        continue
                except OSError:
                    continue
                scanned += 1
                if self._denial("Read", {"file_path": str(file_path)}):
                    continue
                try:
                    if file_path.stat().st_size > MAX_NATIVE_READ_BYTES:
                        continue
                    for line_no, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                        if regex.search(line):
                            lines.append(f"{_relative_or_absolute(self.spec.workspace, file_path)}:{line_no}: {line}")
                            if len(lines) >= MAX_NATIVE_TOOL_RESULTS:
                                lines.append(f"... truncated after {MAX_NATIVE_TOOL_RESULTS} matches")
                                return _truncate_tool_result("\n".join(lines))
                except Exception:
                    continue
            return _truncate_tool_result("\n".join(lines)) if lines else "No matches found."
        except Exception as exc:
            return f"Grep error: {type(exc).__name__}: {exc}"

    def edit(self, file_path: str, old_string: str, new_string: str) -> str:
        """Replace text in a file inside the delegated workspace."""
        denial = self._denial("Edit", {"file_path": file_path})
        if denial:
            return denial
        try:
            if old_string == "":
                return "Edit error: old_string must not be empty."
            target = self._resolve(file_path)
            if not target.exists():
                return f"Edit error: file does not exist ({target})."
            if not target.is_file():
                return f"Edit error: path is not a file ({target})."
            content = target.read_text(encoding="utf-8")
            if old_string not in content:
                return "Edit error: old_string was not found."
            target.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
            return f"Edited file: {_relative_or_absolute(self.spec.workspace, target)}"
        except Exception as exc:
            return f"Edit error: {type(exc).__name__}: {exc}"

    def write(self, file_path: str, content: str) -> str:
        """Write a file inside the delegated workspace."""
        denial = self._denial("Write", {"file_path": file_path})
        if denial:
            return denial
        try:
            target = self._resolve(file_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
            return f"Wrote file: {_relative_or_absolute(self.spec.workspace, target)} ({len(str(content))} characters)"
        except Exception as exc:
            return f"Write error: {type(exc).__name__}: {exc}"

    def bash(self, command: str) -> str:
        """Run a bounded shell command in the delegated workspace."""
        command_text = str(command or "")
        denial = self._denial("Bash", {"command": command_text})
        if denial:
            return denial
        if not command_text.strip():
            return "Bash error: command must not be empty."

        proc: subprocess.Popen[str] | None = None
        timed_out = False
        try:
            workspace = resolve_project_path(self.spec.workspace)
            limits = resource_limits._delegation_resource_limits(self.settings)
            shell_bin = shutil.which("bash")
            use_ulimit = platform.system() == "Linux" and bool(shell_bin)
            if use_ulimit:
                wrapped_command = _native_bash_command(command_text, limits)
            else:
                shell_bin = shell_bin or shutil.which("sh")
                if not shell_bin:
                    return "Bash error: no shell executable was found."
                wrapped_command = command_text
                logger.warning("native Bash running without ulimit wrapper; platform=%s shell=%s", platform.system(), shell_bin)

            proc = subprocess.Popen(
                [shell_bin, "-c", wrapped_command],
                cwd=str(workspace),
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            timeout_seconds = max(1, min(int(self.spec.timeout_seconds or NATIVE_BASH_TIMEOUT_SECONDS), NATIVE_BASH_TIMEOUT_SECONDS))
            try:
                output, _ = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(proc)
                output, _ = proc.communicate(timeout=2)

            exit_code = proc.returncode
            suffix = f"\n[exit code: {exit_code}]"
            if timed_out:
                suffix = f"\n[timeout after {timeout_seconds}s]" + suffix
            return _truncate_tool_result((output or "") + suffix)
        except Exception as exc:
            if proc is not None:
                _terminate_process_group(proc)
            return f"Bash error: {type(exc).__name__}: {exc}"
        finally:
            if proc is not None and proc.poll() is None:
                _terminate_process_group(proc)

    def web_search(self, query: str) -> str:
        """Search the web using the room's configured search provider."""
        query_text = str(query or "").strip()
        if not query_text:
            return "WebSearch error: query must not be empty."
        try:
            from tools import web_tools

            result = web_tools.web_search_tool.invoke({"query": query_text, "room_name": self.spec.room_name})
            return _truncate_tool_result(str(result))
        except Exception as exc:
            return f"WebSearch error: {type(exc).__name__}: {exc}"

    def web_fetch(self, url: str) -> str:
        """Fetch and read the textual content of a single web page or PDF."""
        url_text = str(url or "").strip()
        if not url_text:
            return "WebFetch error: url must not be empty."
        try:
            from tools import web_tools

            result = web_tools.read_url_tool.invoke({"urls": [url_text], "room_name": self.spec.room_name})
            return _truncate_tool_result(str(result))
        except Exception as exc:
            return f"WebFetch error: {type(exc).__name__}: {exc}"

    def _denial(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        return check_delegation_tool_permission(
            tool_name,
            tool_input,
            tier=self.tier,
            workspace=self.spec.workspace,
            exclude_dirs=self.spec.exclude_dirs,
            exclude_files=self.spec.exclude_files,
            app_root=self.app_root,
            extra_scopes=self.spec.extra_scopes,
        )

    def _resolve(self, raw_path: str | None) -> Path:
        return resolve_project_path(self.spec.workspace, raw_path)

    def _glob_matches(self, pattern: str, path: str | None) -> list[Path]:
        if path:
            base = self._resolve(path)
            raw_pattern = str(base / pattern)
        else:
            pattern_path = Path(pattern).expanduser()
            raw_pattern = str(pattern_path if pattern_path.is_absolute() else resolve_project_path(self.spec.workspace, pattern))
        # 候補を全件materializeせず iglob で遅延評価し、評価数に上限を設ける
        allowed: list[Path] = []
        scanned = 0
        for raw_match in globlib.iglob(raw_pattern, recursive=True):
            scanned += 1
            if scanned > MAX_NATIVE_GLOB_SCAN:
                break
            match = Path(raw_match).resolve()
            if self._denial("Read", {"file_path": str(match)}) is None:
                allowed.append(match)
            if len(allowed) >= MAX_NATIVE_TOOL_RESULTS:
                break
        allowed.sort()
        return allowed


def _native_bash_command(command: str, limits: dict[str, int]) -> str:
    return "\n".join(
        [
            f"ulimit -u {int(limits['nproc'])}",
            f"ulimit -t {int(limits['cpu_seconds'])}",
            f"ulimit -v {int(limits['as_kb'])}",
            f"ulimit -f {int(limits['fsize_blocks'])}",
            str(command),
        ]
    )


def _terminate_process_group(proc: subprocess.Popen[Any]) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
        time.sleep(0.2)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        return

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("failed to signal native Bash process group pgid=%s sig=%s", pgid, sig, exc_info=True)
            return
        time.sleep(0.2)
        if proc.poll() is not None:
            return


def _truncate_tool_result(text: str) -> str:
    value = str(text)
    if len(value) <= MAX_NATIVE_TOOL_RESULT_CHARS:
        return value
    return value[:MAX_NATIVE_TOOL_RESULT_CHARS] + "\n... truncated"


def _relative_or_absolute(workspace: str, path: str | Path) -> str:
    target = Path(path)
    try:
        return str(target.resolve().relative_to(resolve_project_path(workspace)))
    except Exception:
        return str(target)
