"""Process memory watchdog for in-process delegated agent execution."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import config_manager
import utils

try:
    import psutil
except ImportError:  # pragma: no cover - depends on optional runtime dependency
    psutil = None


logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_SYSTEM_AVAILABLE_LIMIT_MB = 512
DEFAULT_NOTICE_COOLDOWN_SECONDS = 300
DEFAULT_CANCEL_AFTER_SECONDS = 60

_watchdog_lock = threading.Lock()
_watchdog_started = False


@dataclass
class MemoryWatchdogState:
    pressure_since: float | None = None
    last_relief_at: float = 0.0
    last_notice_at: float = 0.0


_state = MemoryWatchdogState()


def start_memory_watchdog() -> bool:
    """Start a single daemon watchdog thread if enabled and psutil is available."""

    global _watchdog_started
    settings = _settings()
    if psutil is None or not settings["enabled"]:
        logger.info("[MEM Watchdog] disabled; psutil=%s enabled=%s", bool(psutil), settings["enabled"])
        return False
    with _watchdog_lock:
        if _watchdog_started:
            return False
        thread = threading.Thread(target=_watchdog_loop, name="nexus-memory-watchdog", daemon=True)
        thread.start()
        _watchdog_started = True
    logger.info("[MEM Watchdog] started")
    return True


def check_memory_pressure_once(*, now: float | None = None, state: MemoryWatchdogState | None = None) -> bool:
    """Run one watchdog check. Returns True when memory pressure was observed."""

    if psutil is None:
        return False
    settings = _settings()
    if not settings["enabled"]:
        return False
    state = state or _state
    now = time.time() if now is None else now

    process_rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
    system_available_mb = psutil.virtual_memory().available / 1024 / 1024
    pressure_reasons = _pressure_reasons(settings, process_rss_mb, system_available_mb)
    if not pressure_reasons:
        state.pressure_since = None
        return False

    if state.pressure_since is None:
        state.pressure_since = now

    message = (
        "[MEM Watchdog] メモリ逼迫を検知しました: "
        + ", ".join(pressure_reasons)
        + f"（RSS {process_rss_mb:.1f}MB / available {system_available_mb:.1f}MB）"
    )
    logger.warning(message)
    _notice(message, now=now, state=state, cooldown_seconds=settings["notice_cooldown_seconds"])

    if now - state.last_relief_at >= settings["notice_cooldown_seconds"]:
        _release_caches()
        state.last_relief_at = now

    if _still_under_pressure(settings) and now - state.pressure_since >= settings["cancel_after_seconds"]:
        _cancel_running_delegations()
    return True


def _watchdog_loop() -> None:
    while True:
        settings = _settings()
        time.sleep(settings["interval_seconds"])
        try:
            check_memory_pressure_once()
        except Exception:
            logger.exception("[MEM Watchdog] check failed")


def _settings() -> dict[str, Any]:
    raw = {}
    if isinstance(config_manager.CONFIG_GLOBAL, dict):
        raw = config_manager.CONFIG_GLOBAL.get("memory_watchdog_settings", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "interval_seconds": max(5, int(raw.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS)),
        "process_rss_limit_mb": max(0, int(raw.get("process_rss_limit_mb") or 0)),
        "system_available_limit_mb": max(0, int(raw.get("system_available_limit_mb") or DEFAULT_SYSTEM_AVAILABLE_LIMIT_MB)),
        "cancel_after_seconds": max(0, int(raw.get("cancel_after_seconds") or DEFAULT_CANCEL_AFTER_SECONDS)),
        "notice_cooldown_seconds": max(30, int(raw.get("notice_cooldown_seconds") or DEFAULT_NOTICE_COOLDOWN_SECONDS)),
    }


def _pressure_reasons(settings: dict[str, Any], process_rss_mb: float, system_available_mb: float) -> list[str]:
    reasons: list[str] = []
    process_limit = int(settings.get("process_rss_limit_mb") or 0)
    available_limit = int(settings.get("system_available_limit_mb") or 0)
    if process_limit > 0 and process_rss_mb > process_limit:
        reasons.append(f"process RSS > {process_limit}MB")
    if available_limit > 0 and system_available_mb < available_limit:
        reasons.append(f"system available < {available_limit}MB")
    return reasons


def _still_under_pressure(settings: dict[str, Any]) -> bool:
    process_rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
    system_available_mb = psutil.virtual_memory().available / 1024 / 1024
    return bool(_pressure_reasons(settings, process_rss_mb, system_available_mb))


def _release_caches() -> None:
    try:
        from rag_manager import RAGManager

        RAGManager.clear_cache()
    except Exception:
        logger.exception("[MEM Watchdog] RAG cache clear failed")


def _cancel_running_delegations() -> None:
    try:
        from agent_delegation import manager

        for task_id, runner in list(manager._RUNNERS.items()):
            if runner.thread.is_alive():
                manager.cancel_task(task_id, reason="[MEM Watchdog] メモリ逼迫が継続したため委任タスクを停止しました。")
    except Exception:
        logger.exception("[MEM Watchdog] delegation cancellation failed")


def _notice(message: str, *, now: float, state: MemoryWatchdogState, cooldown_seconds: int) -> None:
    if now - state.last_notice_at < cooldown_seconds:
        return
    state.last_notice_at = now
    try:
        utils.add_system_notice(message, level="warning")
    except Exception:
        logger.debug("[MEM Watchdog] system notice failed", exc_info=True)
