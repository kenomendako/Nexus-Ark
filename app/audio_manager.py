import datetime
import os
import traceback
import wave
from typing import Any, Dict, Optional

import google.genai as genai
import google.genai.errors
import requests
from google.genai import types
from openai import OpenAI


MAX_TEXT_LENGTH = 8000
DEFAULT_GEMINI_TTS_MODEL = "gemini-3.1-flash-tts-preview"


def _truncate_text(text: str) -> str:
    if len(text) > MAX_TEXT_LENGTH:
        print(f"  - 警告: テキストが長すぎるため、{MAX_TEXT_LENGTH}文字に切り詰めました。")
        return text[:MAX_TEXT_LENGTH] + "..."
    return text


def _build_prompt(text: str, style_prompt: Optional[str]) -> str:
    text_to_speak = _truncate_text(text or "")
    if style_prompt and style_prompt.strip():
        return f"{style_prompt.strip()}: {text_to_speak}"
    return text_to_speak


def _safe_filename_part(value: Any) -> str:
    safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in str(value or "voice"))
    return safe[:80] or "voice"


def _get_save_path(room_name: str, voice_id: str, extension: str = "wav") -> str:
    save_dir = os.path.join("characters", room_name, "audio_cache")
    os.makedirs(save_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{_safe_filename_part(voice_id)}.{extension.lstrip('.')}"
    return os.path.abspath(os.path.join(save_dir, filename))


def _normalize_gemini_model_name(model_name: Optional[str]) -> str:
    model = (model_name or DEFAULT_GEMINI_TTS_MODEL).strip()
    return model if model.startswith("models/") else f"models/{model}"


def _write_wav(filepath: str, audio_data: bytes, channels: int = 1, rate: int = 24000, sample_width: int = 2) -> None:
    with wave.open(filepath, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(audio_data)


def _generate_gemini_tts(
    text: str,
    api_key: str,
    voice_id: str,
    room_name: str,
    style_prompt: Optional[str],
    model_name: Optional[str],
) -> Optional[str]:
    final_prompt = _build_prompt(text, style_prompt)
    model = _normalize_gemini_model_name(model_name)

    print(f"--- 音声生成開始 (Provider: Gemini, Room: {room_name}, Model: {model}, Voice: {voice_id}) ---")
    print(f"  - 最終プロンプト: {final_prompt[:100]}...")

    client = genai.Client(api_key=api_key)
    generation_config_object = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_id)
            )
        ),
    )

    response = client.models.generate_content(
        model=model,
        contents=[types.Content(parts=[types.Part(text=final_prompt)])],
        config=generation_config_object,
    )

    if (
        response
        and response.candidates
        and response.candidates[0].content
        and response.candidates[0].content.parts
        and response.candidates[0].content.parts[0].inline_data
    ):
        audio_data = response.candidates[0].content.parts[0].inline_data.data
        if not audio_data:
            print("--- エラー: API応答のインラインデータが空です ---")
            return None
    else:
        print("--- エラー: API応答に予期した音声データが含まれていませんでした。 ---")
        if response and response.candidates:
            candidate = response.candidates[0]
            finish_reason = candidate.finish_reason.name if hasattr(candidate, "finish_reason") and hasattr(candidate.finish_reason, "name") else "不明"
            safety_ratings = candidate.safety_ratings if hasattr(candidate, "safety_ratings") else "取得不能"
            print(f"  - 終了理由: {finish_reason}")
            print(f"  - 安全性評価: {safety_ratings}")
        return None

    filepath = _get_save_path(room_name, voice_id, "wav")
    _write_wav(filepath, audio_data)
    print(f"  - 音声ファイル(WAV)を生成しました: {filepath}")
    return filepath


def _read_openai_audio_response(response: Any) -> bytes:
    if hasattr(response, "read"):
        return response.read()
    if hasattr(response, "content"):
        return response.content
    if isinstance(response, bytes):
        return response
    return bytes(response)


def _generate_openai_compatible_tts(
    text: str,
    api_key: str,
    voice_id: str,
    room_name: str,
    style_prompt: Optional[str],
    model_name: Optional[str],
    base_url: Optional[str],
    response_format: Optional[str],
    extra_body: Optional[Dict[str, Any]],
) -> Optional[str]:
    model = (model_name or "gpt-4o-mini-tts").strip()
    audio_format = (response_format or "mp3").strip().lower()
    final_prompt = _truncate_text(text or "")

    print(f"--- 音声生成開始 (Provider: OpenAI互換, Room: {room_name}, Model: {model}, Voice: {voice_id}) ---")

    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    kwargs: Dict[str, Any] = {
        "model": model,
        "voice": voice_id,
        "input": final_prompt,
        "response_format": audio_format,
    }
    if style_prompt and style_prompt.strip() and model.startswith("gpt-4o"):
        kwargs["instructions"] = style_prompt.strip()
    if extra_body:
        kwargs["extra_body"] = extra_body

    response = client.audio.speech.create(**kwargs)
    audio_data = _read_openai_audio_response(response)
    if not audio_data:
        return None

    filepath = _get_save_path(room_name, voice_id, audio_format)
    with open(filepath, "wb") as f:
        f.write(audio_data)
    print(f"  - 音声ファイル({audio_format})を生成しました: {filepath}")
    return filepath


def _generate_elevenlabs_tts(
    text: str,
    api_key: str,
    voice_id: str,
    room_name: str,
    style_prompt: Optional[str],
    model_name: Optional[str],
    response_format: Optional[str],
) -> Optional[str]:
    model = (model_name or "eleven_flash_v2_5").strip()
    audio_format = (response_format or "mp3").strip().lower()
    final_prompt = _build_prompt(text, style_prompt)

    print(f"--- 音声生成開始 (Provider: ElevenLabs, Room: {room_name}, Model: {model}, Voice: {voice_id}) ---")

    output_format = "mp3_44100_128" if audio_format == "mp3" else "pcm_24000"
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    response = requests.post(
        url,
        headers={
            "xi-api-key": api_key,
            "Accept": "audio/mpeg" if audio_format == "mp3" else "audio/wav",
            "Content-Type": "application/json",
        },
        params={"output_format": output_format},
        json={
            "text": final_prompt,
            "model_id": model,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
            },
        },
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"ElevenLabs APIエラー: {response.status_code} {response.text[:300]}")

    audio_data = response.content
    if not audio_data:
        return None

    extension = "mp3" if audio_format == "mp3" else "pcm"
    filepath = _get_save_path(room_name, voice_id, extension)
    with open(filepath, "wb") as f:
        f.write(audio_data)
    print(f"  - 音声ファイル({extension})を生成しました: {filepath}")
    return filepath


def generate_audio_from_text(
    text: str,
    api_key: str,
    voice_id: str,
    room_name: str,
    style_prompt: str = None,
    tts_provider: str = "gemini",
    tts_model: str = None,
    base_url: str = None,
    response_format: str = None,
    extra_body: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    指定されたTTSプロバイダで音声を生成し、再生可能な音声ファイルとして保存する。
    既存呼び出し互換のため、デフォルトはGemini TTS。
    """
    try:
        provider = (tts_provider or "gemini").strip().lower()
        if provider == "google":
            provider = "gemini"

        if not api_key or str(api_key).startswith("YOUR_API_KEY"):
            return "【エラー】TTS用APIキーが設定されていません。"

        if provider == "gemini":
            return _generate_gemini_tts(text, api_key, voice_id, room_name, style_prompt, tts_model)
        if provider in {"openai", "openai_compatible"}:
            return _generate_openai_compatible_tts(
                text, api_key, voice_id, room_name, style_prompt, tts_model, base_url, response_format or "mp3", extra_body
            )
        if provider == "elevenlabs":
            return _generate_elevenlabs_tts(text, api_key, voice_id, room_name, style_prompt, tts_model, response_format or "mp3")

        return f"【エラー】未対応のTTSプロバイダです: {tts_provider}"

    except google.genai.errors.ClientError as e:
        if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
            error_message = "【エラー】音声生成APIの利用上限に達しました。しばらく待ってから再試行してください。"
        else:
            error_message = "【エラー】APIリクエストが無効です。プロンプト、モデル、音声名を確認してください。"
        print(f"--- {error_message} 詳細: {e} ---")
        return error_message
    except google.genai.errors.ServerError as e:
        error_message = "【エラー】APIサーバー側で内部エラーが発生しました。一時的な問題の可能性があります。"
        print(f"--- {error_message} 詳細: {e} ---")
        return error_message
    except Exception as e:
        error_message = "【エラー】音声生成中に予期せぬエラーが発生しました。"
        print(f"--- {error_message} 詳細: {e} ---")
        traceback.print_exc()
        return error_message
