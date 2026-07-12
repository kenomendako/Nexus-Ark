from __future__ import annotations

from dataclasses import dataclass
import re
from typing import List


TTS_MODE_TRIM = "trim"
TTS_MODE_SPLIT = "split"
TTS_DEFAULT_MAX_CHARS = 1500
TTS_MIN_MAX_CHARS = 400
TTS_MAX_MAX_CHARS = 3000


@dataclass(frozen=True)
class TtsTextPlan:
    mode: str
    segments: List[str]
    original_length: int
    max_chars: int
    truncated: bool = False
    notice: str = ""


def normalize_tts_mode(mode: str | None) -> str:
    normalized = (mode or TTS_MODE_TRIM).strip().lower()
    if normalized in {TTS_MODE_TRIM, "auto_trim", "first"}:
        return TTS_MODE_TRIM
    if normalized in {TTS_MODE_SPLIT, "chunk", "chunks"}:
        return TTS_MODE_SPLIT
    return TTS_MODE_TRIM


def clamp_tts_max_chars(max_chars: int | None) -> int:
    try:
        value = int(max_chars or TTS_DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        value = TTS_DEFAULT_MAX_CHARS
    return max(TTS_MIN_MAX_CHARS, min(TTS_MAX_MAX_CHARS, value))


def prepare_tts_text_plan(text: str, mode: str | None = None, max_chars: int | None = None) -> TtsTextPlan:
    clean_text = (text or "").strip()
    effective_mode = normalize_tts_mode(mode)
    effective_max = clamp_tts_max_chars(max_chars)

    if not clean_text:
        return TtsTextPlan(
            mode=effective_mode,
            segments=[],
            original_length=0,
            max_chars=effective_max,
        )

    original_length = len(clean_text)
    if original_length <= effective_max:
        return TtsTextPlan(
            mode=effective_mode,
            segments=[clean_text],
            original_length=original_length,
            max_chars=effective_max,
        )

    if effective_mode == TTS_MODE_SPLIT:
        segments = split_tts_text(clean_text, effective_max)
        return TtsTextPlan(
            mode=effective_mode,
            segments=segments,
            original_length=original_length,
            max_chars=effective_max,
            truncated=False,
            notice=f"長い応答のため、{len(segments)}分割して再生します。",
        )

    return TtsTextPlan(
        mode=TTS_MODE_TRIM,
        segments=[clean_text[:effective_max].rstrip()],
        original_length=original_length,
        max_chars=effective_max,
        truncated=True,
        notice="長い応答のため、先頭部分だけ再生します。",
    )


def split_tts_text(text: str, max_chars: int) -> List[str]:
    chunks: List[str] = []
    current = ""
    for part in _iter_tts_parts(text):
        if len(part) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.extend(_hard_split(part, max_chars))
            continue
        candidate = f"{current}{part}" if current else part
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            current = part
    if current:
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


def _iter_tts_parts(text: str) -> List[str]:
    normalized = re.sub(r"\n{3,}", "\n\n", text)
    parts = re.split(r"([。！？!?]\s*|\n\n+|\n)", normalized)
    merged: List[str] = []
    for i in range(0, len(parts), 2):
        body = parts[i]
        delimiter = parts[i + 1] if i + 1 < len(parts) else ""
        piece = f"{body}{delimiter}"
        if piece:
            merged.append(piece)
    return merged or [normalized]


def _hard_split(text: str, max_chars: int) -> List[str]:
    return [text[i:i + max_chars].strip() for i in range(0, len(text), max_chars) if text[i:i + max_chars].strip()]
