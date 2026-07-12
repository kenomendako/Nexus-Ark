"""Layered backups for small JSON settings files."""

from __future__ import annotations

import datetime
import filecmp
import json
import os
import re
import shutil
from typing import Any


HOURLY_BACKUP_COUNT = 48
DAILY_BACKUP_COUNT = 30
SUSPICIOUS_BACKUP_COUNT = 50

_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}$")
_GUIDELINE_PATHS = {
    "autonomous_settings.autonomous_guidelines",
    "override_settings.autonomous_settings.autonomous_guidelines",
}
_QUIET_START_PATHS = {
    "autonomous_settings.quiet_hours_start",
    "override_settings.autonomous_settings.quiet_hours_start",
}
_QUIET_END_PATHS = {
    "autonomous_settings.quiet_hours_end",
    "override_settings.autonomous_settings.quiet_hours_end",
}
_STRING_OR_NONE_SUFFIXES = (
    "provider",
    "model_name",
    "api_key_name",
    "tts_profile_name",
    "base_url",
    "model",
)


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(data, dict):
        return {prefix: data} if prefix else {}
    result: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            result.update(_flatten(value, path))
        else:
            result[path] = value
    return result


def _safe_copy_if_distinct(source_path: str, target_path: str) -> bool:
    if os.path.exists(target_path):
        try:
            if filecmp.cmp(source_path, target_path, shallow=False):
                return False
        except OSError:
            pass
    shutil.copy2(source_path, target_path)
    os.utime(target_path, None)
    return True


def _rotate_dir(directory: str, keep_count: int) -> None:
    if keep_count <= 0 or not os.path.isdir(directory):
        return
    backups = sorted(
        [f for f in os.listdir(directory) if f.endswith(".bak")],
        key=lambda f: os.path.getmtime(os.path.join(directory, f)),
    )
    overflow = len(backups) - keep_count
    if overflow <= 0:
        return
    for filename in backups[:overflow]:
        try:
            os.remove(os.path.join(directory, filename))
            manifest_path = os.path.join(directory, f"{filename}.json")
            if os.path.exists(manifest_path):
                os.remove(manifest_path)
        except OSError:
            pass


def _latest_backup_is_same(source_path: str, backup_dir: str) -> bool:
    backups = sorted(
        [f for f in os.listdir(backup_dir) if f.endswith(".bak")],
        key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)),
    )
    if not backups:
        return False
    try:
        return filecmp.cmp(source_path, os.path.join(backup_dir, backups[-1]), shallow=False)
    except OSError:
        return False


def _detect_suspicious_change(old_data: Any, new_data: Any) -> list[str]:
    old_flat = _flatten(old_data)
    new_flat = _flatten(new_data)
    reasons: list[str] = []

    for path in _GUIDELINE_PATHS:
        old_value = old_flat.get(path)
        new_value = new_flat.get(path)
        if (
            isinstance(old_value, str)
            and isinstance(new_value, str)
            and len(old_value.strip()) >= 20
            and _TIME_ONLY_RE.match(new_value.strip())
        ):
            reasons.append(f"{path}: long_text_to_time")
        elif (
            isinstance(new_value, str)
            and _TIME_ONLY_RE.match(new_value.strip())
        ):
            reasons.append(f"{path}: time_like_guideline")

    for start_path, end_path in zip(sorted(_QUIET_START_PATHS), sorted(_QUIET_END_PATHS)):
        old_start = old_flat.get(start_path)
        old_end = old_flat.get(end_path)
        new_start = new_flat.get(start_path)
        new_end = new_flat.get(end_path)
        if (
            isinstance(new_start, str)
            and isinstance(new_end, str)
            and new_start == new_end
            and (old_start != old_end)
        ):
            reasons.append(f"{start_path}/{end_path}: quiet_hours_collapsed")

    for path, old_value in old_flat.items():
        if path not in new_flat:
            continue
        new_value = new_flat[path]
        key = path.rsplit(".", 1)[-1]
        if key in _STRING_OR_NONE_SUFFIXES and isinstance(old_value, (str, type(None))):
            if new_value is not None and not isinstance(new_value, str):
                reasons.append(f"{path}: string_type_to_{type(new_value).__name__}")

    return reasons[:8]


def _write_suspicious_manifest(target_path: str, reasons: list[str]) -> None:
    manifest_path = f"{target_path}.json"
    payload = {
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "backup_file": os.path.basename(target_path),
        "reasons": reasons,
    }
    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def create_layered_settings_backup(
    source_path: str,
    backup_dir: str,
    original_filename: str,
    *,
    rotation_count: int,
    new_data: Any | None = None,
    timestamp: str | None = None,
) -> str | None:
    """Create latest/hourly/daily/suspicious backups for a JSON settings file."""
    if not source_path or not os.path.exists(source_path):
        return None

    os.makedirs(backup_dir, exist_ok=True)
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    latest_path: str | None = None

    if not _latest_backup_is_same(source_path, backup_dir):
        latest_path = os.path.join(backup_dir, f"{timestamp}_{original_filename}.bak")
        shutil.copy2(source_path, latest_path)
        os.utime(latest_path, None)

    now = datetime.datetime.now()
    hourly_dir = os.path.join(backup_dir, "hourly")
    os.makedirs(hourly_dir, exist_ok=True)
    hourly_path = os.path.join(hourly_dir, f"{now.strftime('%Y%m%d_%H')}_{original_filename}.bak")
    _safe_copy_if_distinct(source_path, hourly_path)
    _rotate_dir(hourly_dir, HOURLY_BACKUP_COUNT)

    daily_dir = os.path.join(backup_dir, "daily")
    os.makedirs(daily_dir, exist_ok=True)
    daily_path = os.path.join(daily_dir, f"{now.strftime('%Y%m%d')}_{original_filename}.bak")
    _safe_copy_if_distinct(source_path, daily_path)
    _rotate_dir(daily_dir, DAILY_BACKUP_COUNT)

    if new_data is not None:
        try:
            old_data = _load_json(source_path)
            reasons = _detect_suspicious_change(old_data, new_data)
        except Exception:
            reasons = ["json_compare_failed"]
        if reasons:
            suspicious_dir = os.path.join(backup_dir, "suspicious")
            os.makedirs(suspicious_dir, exist_ok=True)
            suspicious_path = os.path.join(
                suspicious_dir,
                f"{timestamp}_suspicious_{original_filename}.bak",
            )
            shutil.copy2(source_path, suspicious_path)
            os.utime(suspicious_path, None)
            _write_suspicious_manifest(suspicious_path, reasons)
            _rotate_dir(suspicious_dir, SUSPICIOUS_BACKUP_COUNT)

    _rotate_dir(backup_dir, max(0, int(rotation_count or 0)))
    return latest_path
