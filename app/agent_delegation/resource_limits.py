"""Shared resource-limit helpers for delegated agent backends."""

from __future__ import annotations

import logging
import os
from typing import Any


logger = logging.getLogger(__name__)

DELEGATION_RLIMIT_HEADROOM_NPROC = 256
DELEGATION_RLIMIT_CPU_SECONDS = 120
DELEGATION_RLIMIT_AS_MB = 2048
DELEGATION_RLIMIT_FSIZE_MB = 512


def _delegation_resource_limits(settings: dict[str, Any]) -> dict[str, int]:
    nproc = int(settings.get("deleg_rlimit_nproc") or 0)
    if nproc <= 0:
        nproc = _current_uid_process_count() + DELEGATION_RLIMIT_HEADROOM_NPROC
    return {
        "nproc": max(64, nproc),
        "cpu_seconds": max(10, int(settings.get("deleg_rlimit_cpu_seconds") or DELEGATION_RLIMIT_CPU_SECONDS)),
        "as_kb": max(256, int(settings.get("deleg_rlimit_as_mb") or DELEGATION_RLIMIT_AS_MB)) * 1024,
        "fsize_blocks": max(16, int(settings.get("deleg_rlimit_fsize_mb") or DELEGATION_RLIMIT_FSIZE_MB)) * 1024,
    }


def _current_uid_process_count() -> int:
    try:
        import psutil

        current_uid = os.getuid() if hasattr(os, "getuid") else None
        if current_uid is None:
            return 0
        count = 0
        for proc in psutil.process_iter(["uids"]):
            try:
                uids = proc.info.get("uids")
                if uids and getattr(uids, "real", None) == current_uid:
                    count += 1
            except Exception:
                continue
        return count
    except Exception:
        logger.debug("failed to count uid processes for delegation rlimit", exc_info=True)
        return 0
