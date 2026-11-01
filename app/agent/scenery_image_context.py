"""Build the optional scenery image part shared by chat and background agents.

The helper deliberately keeps temporary-location photos out of permanent chat
logs.  It only returns prompt parts for the current invocation and records a
small per-room delivery state so ``変更時のみ`` can detect an image replaced
in place.  The temporary-location cache mtime is also observed, allowing a
deactivate/re-activate cycle to trigger a resend even if no request occurred
while the temporary location was inactive.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import config_manager
import constants
import room_manager
import utils

from file_lock_utils import safe_json_read, safe_json_update

logger = logging.getLogger(__name__)

_CACHE_FILENAME = "scenery_image_context.json"
_SYSTEM_TEXT = "（システム：現在の光景）"


@dataclass(frozen=True)
class SceneryImageContext:
    """Prompt-ready scenery image state for one agent invocation.

    ``parts`` can be passed directly to ``user_prompt_parts`` with
    ``list(context.parts)``.  ``image_path`` and ``fingerprint`` describe the
    selected candidate even when no part was sent, which keeps callers from
    having to resolve temporary versus virtual scenery again.
    """

    parts: tuple[dict[str, Any], ...]
    image_path: str | None
    fingerprint: str | None
    source: str | None
    temporary_active: bool
    sent: bool
    reason: str
    # Snapshot metadata used to commit only the observation that was actually
    # prepared.  Older callers can keep constructing the context positionally.
    delivery_epoch: int = 0
    temporary_state_marker: str | None = None


def _cache_path(room_name: str) -> Path | None:
    """Return a room-local cache path, rejecting path traversal inputs."""

    if not room_name or Path(room_name).name != room_name:
        return None
    return Path(constants.ROOMS_DIR) / room_name / "cache" / _CACHE_FILENAME


def _file_fingerprint(path: str | None) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as image_file:
            for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, ValueError):
        logger.warning("Could not fingerprint scenery image: %s", path, exc_info=True)
        return None
    return f"sha256:{digest.hexdigest()}"


def _temporary_manager(manager: Any | None) -> Any:
    if manager is not None:
        return manager
    from agent.temporary_location_manager import TemporaryLocationManager

    return TemporaryLocationManager()


def _resolve_candidate(
    room_name: str,
    *,
    season_en: str | None,
    time_of_day_en: str | None,
    manager: Any,
    include_virtual: bool,
    resolve_image: bool = True,
) -> tuple[str | None, str | None, bool, str | None]:
    """Resolve temporary scenery first, then the room's virtual scenery."""

    room_cache_path = _cache_path(room_name)
    temporary_state_marker = None
    if room_cache_path is not None:
        temporary_cache_path = room_cache_path.parent / "temporary_location.json"
        try:
            temporary_state_marker = str(temporary_cache_path.stat().st_mtime_ns)
        except OSError:
            temporary_state_marker = None

    try:
        temporary_active = bool(manager.is_active(room_name))
    except Exception:
        logger.warning("Could not read temporary location state for %s", room_name, exc_info=True)
        temporary_active = False

    if temporary_active:
        if not resolve_image:
            # The disabled path only needs the active-state marker.  Avoid
            # touching the temporary-location payload (and therefore its
            # image path) until sending is enabled again.
            return (None, "temporary", True, temporary_state_marker)
        try:
            current = manager.get_current_data(room_name) or {}
            image_path = current.get("image_path") if isinstance(current, Mapping) else None
            return (
                str(image_path) if image_path else None,
                "temporary",
                True,
                temporary_state_marker,
            )
        except Exception:
            logger.warning("Could not read temporary location image for %s", room_name, exc_info=True)
            return (None, "temporary", True, temporary_state_marker)

    if not include_virtual:
        return (None, "virtual", False, temporary_state_marker)

    if not resolve_image:
        return (None, "virtual", False, temporary_state_marker)

    try:
        location_id = utils.get_current_location(room_name)
        image_path = utils.find_scenery_image(
            room_name,
            location_id,
            season_en=season_en,
            time_of_day_en=time_of_day_en,
        )
        return (
            str(image_path) if image_path else None,
            "virtual",
            False,
            temporary_state_marker,
        )
    except Exception:
        logger.warning("Could not resolve virtual scenery image for %s", room_name, exc_info=True)
        return (None, "virtual", False, temporary_state_marker)


_OBSERVATION_KEYS = (
    "path",
    "fingerprint",
    "source",
    "temporary_active",
    "temporary_state_marker",
    "enabled",
)


def _coerce_epoch(value: Any) -> int:
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        return 0
    return max(epoch, 0)


def _observation_changed(previous: Any, observation: Mapping[str, Any]) -> bool:
    if not isinstance(previous, Mapping):
        return True
    for key in _OBSERVATION_KEYS:
        if key == "temporary_state_marker" and not (
            bool(previous.get("temporary_active"))
            or bool(observation.get("temporary_active"))
        ):
            # A temporary cache rewrite while the temporary location is
            # inactive must not cause a virtual scenery resend.
            continue
        if previous.get(key) != observation.get(key):
            return True
    return False


def _next_observation_epoch(
    data: dict[str, Any],
    observation: dict[str, Any],
) -> tuple[int, bool]:
    """Advance the delivery epoch only when the effective observation changes."""

    previous = data.get("last_observed")
    previous_epoch = _coerce_epoch(data.get("delivery_epoch"))
    if isinstance(previous, Mapping):
        previous_epoch = max(previous_epoch, _coerce_epoch(previous.get("delivery_epoch")))
    last_sent = data.get("last_sent")
    if isinstance(last_sent, Mapping):
        previous_epoch = max(previous_epoch, _coerce_epoch(last_sent.get("delivery_epoch")))

    changed = _observation_changed(previous, observation)
    epoch = (
        previous_epoch + 1
        if changed
        else max(previous_epoch, _coerce_epoch(previous.get("delivery_epoch")))
    )
    observation["delivery_epoch"] = epoch
    return epoch, changed


def _state_update(
    room_name: str,
    *,
    image_path: str | None,
    fingerprint: str | None,
    source: str | None,
    temporary_active: bool,
    temporary_state_marker: str | None,
    enabled: bool,
    mode: str,
) -> tuple[bool, int]:
    """Record observation and decide whether the image should be sent."""

    cache_path = _cache_path(room_name)
    if cache_path is None:
        # A safe room id is expected in production.  Keep a useful fallback for
        # callers that cannot persist state rather than failing the response.
        return bool(enabled and image_path and (mode == "毎ターン" or fingerprint)), 0

    observation = {
        "path": image_path,
        "fingerprint": fingerprint,
        "source": source,
        "temporary_active": temporary_active,
        "temporary_state_marker": temporary_state_marker,
        "enabled": enabled,
    }
    decision = {"send": False}
    decision["epoch"] = 0

    def update(state: Any) -> dict[str, Any]:
        data = dict(state) if isinstance(state, Mapping) else {}
        previous_observation = data.get("last_observed")
        last_sent = data.get("last_sent")
        epoch, _ = _next_observation_epoch(data, observation)

        should_send = False
        if enabled and image_path and fingerprint:
            if mode == "毎ターン":
                should_send = True
            elif not isinstance(last_sent, Mapping) or not last_sent.get("fingerprint"):
                # Seed the hash state for rooms created before this helper.
                should_send = True
            elif (
                last_sent.get("delivery_epoch") is None
                or _coerce_epoch(last_sent.get("delivery_epoch")) != epoch
            ):
                # A changed observation remains pending until a successful
                # provider call commits this exact epoch.  This is what keeps
                # a failed re-activation/re-application retryable even though
                # last_observed was already updated during prepare.
                should_send = True
            elif isinstance(previous_observation, Mapping) and (
                previous_observation.get("enabled") is False
                or previous_observation.get("source") != source
                or bool(previous_observation.get("temporary_active")) != temporary_active
                or (
                    temporary_active
                    and previous_observation.get("temporary_state_marker")
                    != temporary_state_marker
                )
            ):
                should_send = True
            elif (
                last_sent.get("source") != source
                or bool(last_sent.get("temporary_active")) != temporary_active
            ):
                # A temporary photo can be applied between two requests.  The
                # last observation alone may then already be temporary while
                # the last delivered image still came from virtual scenery.
                should_send = True
            else:
                should_send = (
                    last_sent.get("path") != image_path
                    or last_sent.get("fingerprint") != fingerprint
                )

        data["delivery_epoch"] = epoch
        data["last_observed"] = observation
        decision["send"] = should_send
        decision["epoch"] = epoch
        return data

    try:
        safe_json_update(cache_path, update, default={})
    except Exception:
        logger.warning("Could not persist scenery image state for %s", room_name, exc_info=True)
        return bool(enabled and image_path and fingerprint and mode == "毎ターン"), 0
    return bool(decision["send"]), _coerce_epoch(decision["epoch"])


def _record_sent(
    room_name: str,
    *,
    image_path: str,
    fingerprint: str,
    source: str,
    temporary_active: bool,
    temporary_state_marker: str | None,
    delivery_epoch: int,
) -> bool:
    """Persist both the legacy path contract and the hash-aware sidecar."""

    cache_path = _cache_path(room_name)
    if cache_path is None:
        try:
            room_manager.update_room_config(
                room_name,
                {"last_sent_scenery_image": image_path},
            )
        except Exception:
            logger.warning("Could not persist last scenery image for %s", room_name, exc_info=True)
        return True
    sent_state = {
        "path": image_path,
        "fingerprint": fingerprint,
        "source": source,
        "temporary_active": temporary_active,
        "temporary_state_marker": temporary_state_marker,
        "delivery_epoch": _coerce_epoch(delivery_epoch),
    }
    decision = {"accepted": False}

    def update(state: Any) -> dict[str, Any]:
        data = dict(state) if isinstance(state, Mapping) else {}
        current_epoch = data.get("delivery_epoch")
        if current_epoch is None and isinstance(data.get("last_observed"), Mapping):
            current_epoch = data["last_observed"].get("delivery_epoch")
        # A late success from an older invocation must not overwrite the
        # newer observation's delivery state.
        if current_epoch is not None and _coerce_epoch(current_epoch) != sent_state["delivery_epoch"]:
            return data
        data["last_sent"] = sent_state
        decision["accepted"] = True
        return data

    try:
        safe_json_update(cache_path, update, default={})
    except Exception:
        logger.warning("Could not persist sent scenery image state for %s", room_name, exc_info=True)
        # If the sidecar is unavailable, retaining the legacy path is still
        # compatible; the next sidecar-backed prepare will resend safely.
        decision["accepted"] = True

    if decision["accepted"]:
        try:
            room_manager.update_room_config(
                room_name,
                {"last_sent_scenery_image": image_path},
            )
        except Exception:
            logger.warning("Could not persist last scenery image for %s", room_name, exc_info=True)
    return bool(decision["accepted"])


def commit_scenery_image_context(
    room_name: str,
    context: SceneryImageContext,
) -> bool:
    """Mark a prepared image as delivered after the provider succeeds.

    Preparing or encoding prompt parts must not suppress a later retry.  The
    invocation owner calls this only after its provider result is known to be
    successful.  Repeating the commit is harmless and keeps the legacy room
    config path contract in sync with the hash-aware sidecar.
    """

    if not isinstance(context, SceneryImageContext) or not context.sent:
        return False
    if not context.image_path or not context.fingerprint or not context.source:
        return False
    return _record_sent(
        room_name,
        image_path=context.image_path,
        fingerprint=context.fingerprint,
        source=context.source,
        temporary_active=context.temporary_active,
        temporary_state_marker=context.temporary_state_marker,
        delivery_epoch=context.delivery_epoch,
    )


def _observe_disabled_state(
    room_name: str,
    *,
    source: str | None,
    temporary_active: bool,
    temporary_state_marker: str | None,
) -> int:
    """Record a disabled observation only when it changes.

    This preserves the disabled -> enabled transition for the next send while
    avoiding a sidecar rewrite on every disabled turn.  It intentionally does
    not resolve or hash any scenery image.
    """

    cache_path = _cache_path(room_name)
    if cache_path is None:
        return 0
    observation = {
        "path": None,
        "fingerprint": None,
        "source": source,
        "temporary_active": temporary_active,
        "temporary_state_marker": temporary_state_marker,
        "enabled": False,
    }
    try:
        state = safe_json_read(cache_path, default={})
        previous = state.get("last_observed") if isinstance(state, Mapping) else None
        if not _observation_changed(previous, observation):
            state_epoch = state.get("delivery_epoch") if isinstance(state, Mapping) else None
            return max(_coerce_epoch(state_epoch), _coerce_epoch(previous.get("delivery_epoch")))
    except Exception:
        # The atomic update below remains the source of truth if a concurrent
        # reader cannot parse the sidecar yet.
        pass

    def update(state: Any) -> dict[str, Any]:
        data = dict(state) if isinstance(state, Mapping) else {}
        epoch, _ = _next_observation_epoch(data, observation)
        data["delivery_epoch"] = epoch
        data["last_observed"] = observation
        decision["epoch"] = epoch
        return data

    decision = {"epoch": 0}
    try:
        safe_json_update(cache_path, update, default={})
    except Exception:
        logger.warning("Could not persist disabled scenery state for %s", room_name, exc_info=True)
    return _coerce_epoch(decision["epoch"])


def _encode_parts(image_path: str) -> tuple[dict[str, Any], ...]:
    encoded_image, output_format = utils.resize_image_for_api(image_path, max_size=512)
    # Match the existing PC block's data-URI contract (including ``image/jpg``
    # when the resize helper reports ``jpg``).
    mime_type = f"image/{output_format}"
    return (
        {"type": "text", "text": _SYSTEM_TEXT},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{encoded_image}"},
        },
    )


def get_scenery_image_context(
    room_name: str,
    *,
    settings: Mapping[str, Any] | None = None,
    season_en: str | None = None,
    time_of_day_en: str | None = None,
    manager: Any | None = None,
    include_virtual: bool = True,
) -> SceneryImageContext:
    """Return the scenery image prompt parts for one room invocation.

    The helper has no chat-log side effect.  It records an observation while
    preparing parts, but delivery metadata is updated only when the caller
    later invokes ``commit_scenery_image_context`` after a successful provider
    result.  Callers remain responsible for combining the returned parts with
    their own text and normal attachments.
    Temporary scenery always wins when active.  ``include_virtual`` is kept
    enabled for the PC caller to preserve its existing virtual-scene behavior;
    Lite/background callers disable it so this change adds only the explicitly
    consented temporary-location photo there.
    """

    try:
        effective_settings = (
            settings
            if settings is not None
            else config_manager.get_effective_settings(room_name)
        )
    except Exception:
        logger.warning("Could not read scenery settings for %s", room_name, exc_info=True)
        return SceneryImageContext((), None, None, None, False, False, "settings_error")

    # Keep the existing PC default: callers with no explicit scenery setting
    # must not unexpectedly incur an image send.
    enabled = bool(effective_settings.get("send_scenery", False))
    mode = str(effective_settings.get("scenery_send_mode") or "変更時のみ")
    location_manager = _temporary_manager(manager)

    if not enabled:
        _, source, temporary_active, temporary_state_marker = _resolve_candidate(
            room_name,
            season_en=season_en,
            time_of_day_en=time_of_day_en,
            manager=location_manager,
            include_virtual=False,
            resolve_image=False,
        )
        delivery_epoch = _observe_disabled_state(
            room_name,
            source=source,
            temporary_active=temporary_active,
            temporary_state_marker=temporary_state_marker,
        )
        return SceneryImageContext(
            (),
            None,
            None,
            source,
            temporary_active,
            False,
            "disabled",
            delivery_epoch,
            temporary_state_marker,
        )

    image_path, source, temporary_active, temporary_state_marker = _resolve_candidate(
        room_name,
        season_en=season_en,
        time_of_day_en=time_of_day_en,
        manager=location_manager,
        include_virtual=include_virtual,
        resolve_image=True,
    )
    fingerprint = _file_fingerprint(image_path)

    should_send, delivery_epoch = _state_update(
        room_name,
        image_path=image_path if fingerprint else None,
        fingerprint=fingerprint,
        source=source,
        temporary_active=temporary_active,
        temporary_state_marker=temporary_state_marker,
        enabled=enabled,
        mode=mode,
    )

    if not image_path or not fingerprint:
        return SceneryImageContext(
            (),
            image_path,
            fingerprint,
            source,
            temporary_active,
            False,
            "no_image",
            delivery_epoch,
            temporary_state_marker,
        )
    if not should_send:
        return SceneryImageContext(
            (),
            image_path,
            fingerprint,
            source,
            temporary_active,
            False,
            "unchanged",
            delivery_epoch,
            temporary_state_marker,
        )

    try:
        parts = _encode_parts(image_path)
    except Exception:
        logger.warning("Could not encode scenery image for %s", room_name, exc_info=True)
        return SceneryImageContext(
            (),
            image_path,
            fingerprint,
            source,
            temporary_active,
            False,
            "encode_error",
            delivery_epoch,
            temporary_state_marker,
        )

    # ``sent=True`` means the prompt is prepared and eligible for a commit;
    # the provider callsite must invoke ``commit_scenery_image_context`` only
    # after a successful response.
    return SceneryImageContext(
        parts,
        image_path,
        fingerprint,
        source,
        temporary_active,
        True,
        "prepared",
        delivery_epoch,
        temporary_state_marker,
    )
