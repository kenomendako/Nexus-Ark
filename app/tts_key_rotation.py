from __future__ import annotations

from typing import Any, Dict, Optional, Set

import audio_manager
import config_manager


def _find_gemini_key_name(api_key: Optional[str]) -> Optional[str]:
    if not api_key:
        return None
    for key_name, key_value in config_manager.GEMINI_API_KEYS.items():
        if key_value == api_key:
            return key_name
    return None


def _is_quota_error(result: Optional[str]) -> bool:
    if not isinstance(result, str):
        return False
    return (
        result.startswith("【エラー】")
        and (
            "利用上限" in result
            or "RESOURCE_EXHAUSTED" in result
            or "429" in result
        )
    )


def _classify_limit_type(error_text: str) -> str:
    upper = error_text.upper()
    if any(
        marker in upper
        for marker in (
            "（RPD）",
            "(RPD)",
            "GENERATEREQUESTSPERDAY",
            "PERDAY",
            "PER_DAY",
            "FREE_TIER_REQUESTS",
            "DAILY",
        )
    ):
        return "RPD"
    return "RPM"


def generate_audio_with_key_rotation(
    text: str,
    room_name: str,
    settings: Dict[str, Any],
) -> Optional[str]:
    """Gemini TTSのクォータ時だけAPIキーを切り替えて音声生成する。"""
    provider = (settings.get("provider") or "gemini").strip().lower()
    if provider == "google":
        provider = "gemini"

    if provider != "gemini":
        return audio_manager.generate_audio_from_text(
            text,
            settings.get("api_key"),
            settings.get("voice_id"),
            room_name,
            settings.get("style_prompt"),
            tts_provider=provider,
            tts_model=settings.get("model"),
            base_url=settings.get("base_url"),
            response_format=settings.get("response_format"),
            extra_body=settings.get("extra_body"),
            speed_scale=settings.get("speedScale"),
            pitch_scale=settings.get("pitchScale"),
            intonation_scale=settings.get("intonationScale"),
            volume_scale=settings.get("volumeScale"),
        )

    model_name = settings.get("model") or "gemini-3.1-flash-tts-preview"
    key_name = settings.get("api_key_name") or _find_gemini_key_name(settings.get("api_key")) or config_manager.initial_api_key_name_global
    if key_name and config_manager.is_key_exhausted(key_name, model_name=model_name):
        next_key_name = config_manager.get_next_available_gemini_key(
            current_exhausted_key=key_name,
            model_name=model_name,
        )
        if next_key_name and next_key_name != key_name:
            print(
                f"--- [TTS API Key Rotation] Gemini TTS key '{key_name}' is already exhausted; "
                f"starting with '{next_key_name}' for model '{model_name}' ---"
            )
            key_name = next_key_name
    tried_keys: Set[str] = set()
    last_result: Optional[str] = None

    while key_name and key_name not in tried_keys:
        tried_keys.add(key_name)
        api_key = config_manager.GEMINI_API_KEYS.get(key_name) or (
            settings.get("api_key") if len(tried_keys) == 1 else None
        )
        if not api_key:
            break

        result = audio_manager.generate_audio_from_text(
            text,
            api_key,
            settings.get("voice_id"),
            room_name,
            settings.get("style_prompt"),
            tts_provider=provider,
            tts_model=model_name,
            base_url=settings.get("base_url"),
            response_format=settings.get("response_format"),
            extra_body=settings.get("extra_body"),
            speed_scale=settings.get("speedScale"),
            pitch_scale=settings.get("pitchScale"),
            intonation_scale=settings.get("intonationScale"),
            volume_scale=settings.get("volumeScale"),
        )
        last_result = result
        if not _is_quota_error(result):
            return result

        limit_type = _classify_limit_type(str(result))
        config_manager.mark_key_as_exhausted(key_name, model_name=model_name, limit_type=limit_type)
        next_key_name = config_manager.get_next_available_gemini_key(
            current_exhausted_key=key_name,
            excluded_keys=tried_keys,
            model_name=model_name,
        )
        if not next_key_name or next_key_name in tried_keys:
            break

        print(
            f"--- [TTS API Key Rotation] Gemini TTS key '{key_name}' hit {limit_type}; "
            f"retrying with '{next_key_name}' for model '{model_name}' ---"
        )
        key_name = next_key_name

    return last_result or audio_manager.generate_audio_from_text(
        text,
        settings.get("api_key"),
        settings.get("voice_id"),
        room_name,
        settings.get("style_prompt"),
        tts_provider=provider,
        tts_model=model_name,
        base_url=settings.get("base_url"),
        response_format=settings.get("response_format"),
        extra_body=settings.get("extra_body"),
        speed_scale=settings.get("speedScale"),
        pitch_scale=settings.get("pitchScale"),
        intonation_scale=settings.get("intonationScale"),
        volume_scale=settings.get("volumeScale"),
    )
