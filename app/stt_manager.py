import io
import os
import re
import struct
import time
import traceback
import wave
from dataclasses import dataclass
from typing import Optional

import google.genai as genai
from google.genai import types
import google.genai.errors
from openai import OpenAI

import constants


class SttApiError(RuntimeError):
    """音声認識API呼び出しが失敗したことを呼び出し元へ伝える例外。"""


@dataclass
class SttResult:
    text: str
    uncertain: bool = False
    model_name: str = ""


_COMMON_STT_HALLUCINATIONS = {
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "ありがとうございました",
    "お疲れ様でした",
    "字幕視聴ありがとうございました",
}


def _is_retryable_error(error: Exception) -> bool:
    err = str(error).upper()
    return any(token in err for token in ("500", "502", "503", "504", "UNAVAILABLE", "OVERLOADED", "TIMEOUT"))


def transcribe_audio_file(audio_path: str, api_key: str, model_name: Optional[str] = None) -> str:
    result = transcribe_audio_file_detailed(audio_path, api_key, model_name=model_name)
    return result.text


def transcribe_audio_file_openai_detailed(
    audio_path: str,
    api_key: str,
    model_name: str = "whisper-1",
    base_url: Optional[str] = None,
) -> SttResult:
    """OpenAI Audio Transcriptions APIで短い音声クリップを文字起こしする。"""
    if not audio_path or not os.path.exists(audio_path):
        return SttResult(text="")
    if not api_key:
        return SttResult(text="")

    target_model = (model_name or "whisper-1").strip() or "whisper-1"
    try:
        with open(audio_path, "rb") as f:
            audio_data = f.read()
        if not audio_data:
            return SttResult(text="")
        audio_data = _prepare_wav_for_stt(audio_data)

        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url.rstrip("/")
        client = OpenAI(**client_kwargs)
        response = client.audio.transcriptions.create(
            model=target_model,
            file=("discord_voice_input.wav", audio_data, "audio/wav"),
            language="ja",
            temperature=0,
            response_format="verbose_json",
        )
        text = _extract_openai_transcription_text(response)
        uncertain = _is_openai_transcription_uncertain(response, text)
        return SttResult(text=text, uncertain=uncertain, model_name=target_model)
    except Exception as e:
        print(f"--- OpenAI STT error ({target_model}): {e} ---")
        traceback.print_exc()
        raise SttApiError(f"OpenAI STT APIエラー ({target_model}): {e}") from e


def transcribe_audio_file_detailed(
    audio_path: str,
    api_key: str,
    model_name: Optional[str] = None,
    mime_type: str = "audio/wav",
) -> SttResult:
    """Geminiで短い音声クリップを文字起こしする。"""
    if not audio_path or not os.path.exists(audio_path):
        return SttResult(text="")
    if not api_key:
        return SttResult(text="")

    target_model = model_name or constants.DISCORD_VOICE_STT_MODEL
    model_candidates = [target_model]
    fallback_model = constants.INTERNAL_PROCESSING_MODEL
    if fallback_model and fallback_model not in model_candidates:
        model_candidates.append(fallback_model)
    try:
        with open(audio_path, "rb") as f:
            audio_data = f.read()
        if not audio_data:
            return SttResult(text="")
        normalized_mime_type = (mime_type or "audio/wav").strip() or "audio/wav"
        if normalized_mime_type == "audio/wav":
            audio_data = _prepare_wav_for_stt(audio_data)

        client = genai.Client(api_key=api_key)
        last_error: Optional[Exception] = None
        strict_prompt = (
            "以下はDiscordボイスチャンネルから切り出した短い日本語音声です。"
            "聞こえた発話内容を文字起こししてください。"
            "音声に含まれない語を補完したり、意味が通る文に言い換えたりしないでください。"
            "発話内容だけを返し、説明・前置き・引用符・タイムスタンプは付けないでください。"
            "音声が不明瞭、短すぎる、ノイズだけ、または確信が低い場合は、推測で短い断片を作らず空文字を返してください。"
            "同じ語の不自然な反復に聞こえる場合も、幻聴として補完せず空文字を返してください。"
        )
        relaxed_prompt = (
            "以下はDiscordボイスチャンネルから切り出した短い日本語音声です。"
            "日本語として聞こえる発話内容を、聞こえた範囲で文字起こししてください。"
            "説明・前置き・引用符・タイムスタンプは付けず、発話内容だけを返してください。"
            "完全に無音またはノイズだけの場合のみ空文字を返してください。"
            "自信が低い場合でも、音声から最も近い候補を短く返してください。"
        )
        for candidate_model in model_candidates:
            max_attempts = 2 if candidate_model == target_model else 1
            for attempt in range(max_attempts):
                try:
                    cleaned = _request_transcript(client, candidate_model, audio_data, strict_prompt, normalized_mime_type)
                    if cleaned:
                        return SttResult(text=cleaned, model_name=candidate_model)
                    relaxed = _request_transcript(client, candidate_model, audio_data, relaxed_prompt, normalized_mime_type)
                    if relaxed:
                        return SttResult(text=relaxed, uncertain=True, model_name=candidate_model)
                    break
                except (google.genai.errors.ClientError, google.genai.errors.ServerError) as e:
                    last_error = e
                    if not _is_retryable_error(e):
                        raise SttApiError(f"Gemini STT APIエラー ({candidate_model}): {e}") from e
                    if attempt + 1 < max_attempts:
                        wait_seconds = 2 * (attempt + 1)
                        print(f"--- STT API retryable error ({candidate_model}): {e}; retrying after {wait_seconds}s ---")
                        time.sleep(wait_seconds)
                        continue
                    print(f"--- STT API retryable error ({candidate_model}): {e}; trying fallback if available ---")
                    break
        if last_error:
            raise SttApiError(f"Gemini STT APIが一時的に混み合っています。少し待って再試行してください ({last_error})") from last_error
        return SttResult(text="")
    except (google.genai.errors.ClientError, google.genai.errors.ServerError) as e:
        raise SttApiError(f"Gemini STT APIエラー ({target_model}): {e}") from e
    except SttApiError:
        raise
    except Exception as e:
        print(f"--- STT unexpected error ({target_model}): {e} ---")
        traceback.print_exc()
        raise SttApiError(f"STT処理中に予期しないエラーが発生しました: {e}") from e


def _request_transcript(client, model_name: str, audio_data: bytes, prompt: str, mime_type: str = "audio/wav") -> str:
    response = client.models.generate_content(
        model=model_name,
        contents=[
            prompt,
            types.Part.from_bytes(data=audio_data, mime_type=mime_type or "audio/wav"),
        ],
    )
    text = getattr(response, "text", "") or ""
    cleaned = text.strip().strip('"').strip("'").strip()
    if cleaned in {"空文字", "（空文字）", "(空文字)", "なし", "発話なし", "聞き取れません"}:
        return ""
    return cleaned


def _extract_openai_transcription_text(response) -> str:
    if isinstance(response, str):
        return response.strip()
    return (getattr(response, "text", "") or "").strip()


def _get_response_value(value, key: str, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _is_openai_transcription_uncertain(response, text: str) -> bool:
    normalized = re.sub(r"[\s。．.!！?？、,「」『』\"'`]+", "", text or "")
    if not normalized:
        return False
    if normalized in {re.sub(r"[\s。．.!！?？、,「」『』\"'`]+", "", phrase) for phrase in _COMMON_STT_HALLUCINATIONS}:
        return True

    segments = _get_response_value(response, "segments", []) or []
    if not segments:
        return False

    no_speech_values = []
    avg_logprob_values = []
    compression_values = []
    for segment in segments:
        no_speech = _get_response_value(segment, "no_speech_prob")
        avg_logprob = _get_response_value(segment, "avg_logprob")
        compression = _get_response_value(segment, "compression_ratio")
        if isinstance(no_speech, (int, float)):
            no_speech_values.append(float(no_speech))
        if isinstance(avg_logprob, (int, float)):
            avg_logprob_values.append(float(avg_logprob))
        if isinstance(compression, (int, float)):
            compression_values.append(float(compression))

    if no_speech_values and max(no_speech_values) >= 0.6:
        return True
    if avg_logprob_values and min(avg_logprob_values) <= -1.2:
        return True
    if compression_values and max(compression_values) >= 2.8:
        return True
    return False


def _prepare_wav_for_stt(audio_data: bytes) -> bytes:
    """STTへ渡す前に、余白の多いDiscord録音を軽く整える。"""
    try:
        with wave.open(io.BytesIO(audio_data), "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        if sample_width != 2 or channels != 1 or sample_rate <= 0 or not frames:
            return audio_data

        samples = list(struct.unpack("<" + "h" * (len(frames) // 2), frames))
        if not samples:
            return audio_data

        window_size = max(1, int(sample_rate * 0.02))
        window_rms = []
        for start in range(0, len(samples), window_size):
            window = samples[start:start + window_size]
            if not window:
                continue
            rms = int((sum(sample * sample for sample in window) / len(window)) ** 0.5)
            window_rms.append(rms)
        if not window_rms:
            return audio_data

        peak_rms = max(window_rms)
        if peak_rms < 500:
            return audio_data
        threshold = max(450, int(peak_rms * 0.12))

        active_windows = [index for index, rms in enumerate(window_rms) if rms >= threshold]
        if not active_windows:
            return audio_data
        pad_windows = 8
        start_window = max(0, active_windows[0] - pad_windows)
        end_window = min(len(window_rms), active_windows[-1] + pad_windows + 1)
        trimmed = samples[start_window * window_size:min(len(samples), end_window * window_size)]
        if not trimmed:
            return audio_data

        compressed = []
        silent_run = 0
        max_silent_windows = 12
        for start in range(0, len(trimmed), window_size):
            window = trimmed[start:start + window_size]
            if not window:
                continue
            rms = int((sum(sample * sample for sample in window) / len(window)) ** 0.5)
            if rms < threshold:
                silent_run += 1
                if silent_run > max_silent_windows:
                    continue
            else:
                silent_run = 0
            compressed.extend(window)
        if not compressed:
            return audio_data

        abs_samples = sorted(abs(sample) for sample in compressed)
        robust_peak = abs_samples[min(len(abs_samples) - 1, int(len(abs_samples) * 0.95))]
        if robust_peak > 0:
            target_peak = 18000
            gain = min(3.0, target_peak / robust_peak)
            if gain > 1.05:
                compressed = [max(-32768, min(32767, int(sample * gain))) for sample in compressed]

        output = io.BytesIO()
        with wave.open(output, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(struct.pack("<" + "h" * len(compressed), *compressed))
        return output.getvalue()
    except Exception:
        return audio_data
