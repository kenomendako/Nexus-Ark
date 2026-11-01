"""新版GradioのHTTP readyを確認してtrial成功markerを交換対象外へ保存する。"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Mapping


TRIAL_MARKER_FILENAME = "trial-success.json"
_IDENTIFIER = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")


def _trial_contract_from_env(app_dir: Path, port: int) -> tuple[Path, dict[str, Any]] | None:
    if os.environ.get("NEXUS_ARK_UPDATE_TRIAL") != "1":
        return None
    operation_id = str(os.environ.get("NEXUS_ARK_UPDATE_OPERATION_ID") or "")
    target_version = str(os.environ.get("NEXUS_ARK_UPDATE_TARGET_VERSION") or "")
    manifest_digest = str(os.environ.get("NEXUS_ARK_UPDATE_MANIFEST_DIGEST") or "")
    process_token = str(os.environ.get("NEXUS_ARK_UPDATE_PROCESS_TOKEN") or "")
    marker_value = str(os.environ.get("NEXUS_ARK_UPDATE_TRIAL_MARKER") or "")
    if not _IDENTIFIER.fullmatch(operation_id):
        raise ValueError("invalid update trial operation id")
    if not _IDENTIFIER.fullmatch(process_token):
        raise ValueError("invalid update trial process token")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_digest):
        raise ValueError("invalid update trial manifest digest")
    marker = Path(marker_value)
    expected_root = app_dir.parent / "update_recovery"
    try:
        marker.resolve().relative_to(expected_root.resolve())
    except (OSError, ValueError) as exc:
        raise ValueError("update trial marker must stay under update_recovery") from exc
    if marker.name != TRIAL_MARKER_FILENAME:
        raise ValueError("update trial marker filename mismatch")
    version_data = json.loads((app_dir / "version.json").read_text(encoding="utf-8"))
    release_version = str(version_data.get("version") or "")
    if not _IDENTIFIER.fullmatch(release_version):
        raise ValueError("invalid update trial release version")
    if target_version != release_version:
        raise ValueError("update trial target version mismatch")
    pid = os.getpid()
    if pid <= 0:
        raise ValueError("invalid update trial process pid")
    return marker, {
        "schema_version": 1,
        "operation_id": operation_id,
        "release_version": release_version,
        "manifest_digest": manifest_digest,
        "process_token": process_token,
        "pid": pid,
        "port": int(port),
    }


def _write_marker(marker: Path, payload: Mapping[str, Any]) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    temp = marker.with_name(marker.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, marker)


def start_trial_ready_monitor(app_dir: Path, port: int, *, timeout_seconds: float = 300.0) -> bool:
    """trial時だけlocalhostを監視し、ready後にmarkerを書くdaemon threadを開始する。"""
    contract = _trial_contract_from_env(app_dir, port)
    if contract is None:
        return False
    marker, payload = contract

    def monitor() -> None:
        deadline = time.monotonic() + timeout_seconds
        url = f"http://127.0.0.1:{port}/"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2.0) as response:
                    if 200 <= response.status < 500:
                        response.read(1)
                        ready_payload = dict(payload)
                        ready_payload["ready_at"] = int(time.time())
                        _write_marker(marker, ready_payload)
                        return
            except Exception:
                time.sleep(0.25)

    threading.Thread(target=monitor, daemon=True, name="update-trial-ready").start()
    return True


def validate_trial_marker(
    marker: Path,
    *,
    operation_id: str,
    release_version: str,
    manifest_digest: str,
    process_token: str,
    port: int,
    # Kept as a source-compatible keyword for callers that used the former
    # host-Popen PID contract.  Windows venv launchers can report a shim PID
    # different from the Python process PID, so it is deliberately not
    # compared with marker['pid'].
    pid: int | None = None,
) -> dict[str, Any]:
    if not _IDENTIFIER.fullmatch(str(process_token)):
        raise ValueError("invalid expected trial process token")
    data = json.loads(marker.read_text(encoding="utf-8"))
    expected = {
        "schema_version": 1,
        "operation_id": operation_id,
        "release_version": release_version,
        "manifest_digest": manifest_digest,
        "process_token": process_token,
        "port": port,
    }
    for key, value in expected.items():
        if data.get(key) != value:
            raise ValueError(f"trial marker mismatch: {key}")
    marker_pid = data.get("pid")
    if isinstance(marker_pid, bool) or not isinstance(marker_pid, int) or marker_pid <= 0:
        raise ValueError("trial marker pid is invalid")
    ready_at = data.get("ready_at")
    if isinstance(ready_at, bool) or not isinstance(ready_at, int):
        raise ValueError("trial marker ready timestamp is missing")
    return data
