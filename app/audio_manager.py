import datetime
import base64
import json
import os
import subprocess
import traceback
import wave
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import google.genai as genai
import google.genai.errors
import requests
from google.genai import types
from openai import APIStatusError, OpenAI


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


def _get_error_body(error: APIStatusError) -> Any:
    body = getattr(error, "body", None)
    if body:
        return body
    response = getattr(error, "response", None)
    if response is None:
        return None
    try:
        return response.json()
    except Exception:
        try:
            return response.text
        except Exception:
            return None


def _format_openai_tts_api_error(
    error: APIStatusError,
    model: str,
    voice_id: str,
    base_url: Optional[str],
    audio_format: str,
) -> str:
    status_code = getattr(error, "status_code", None)
    body = _get_error_body(error)
    context = f"接続先={base_url or 'OpenAI公式'}, model={model}, voice={voice_id}, format={audio_format}"
    body_text = str(body or "")

    if "model_terms_required" in body_text or "requires terms acceptance" in body_text:
        return (
            "Groq TTSモデルの利用規約承認が必要です。"
            f"{context}。Groq Consoleで組織管理者が対象モデルのTermsを承認してから再試行してください。"
            f" API応答: {body}"
        )
    if "response_format must be one of [wav]" in body_text:
        return (
            "Groq TTSの出力形式はwavのみ対応です。"
            f"{context}。Nexus Ark側でGroqプロファイルはwavへ自動補正するため、再試行してください。"
            f" API応答: {body}"
        )

    if status_code == 403:
        return (
            "OpenAI互換TTSの権限が拒否されました。"
            f"{context}。APIキーのチーム/組織でこの音声生成モデルの実行権限があるか、"
            "選択中のOpenAI互換プロファイルが意図した接続先か確認してください。"
            f" API応答: {body}"
        )
    if status_code == 401:
        return f"OpenAI互換TTSのAPIキーが無効です。{context}。API応答: {body}"
    if status_code in {400, 404}:
        return f"OpenAI互換TTSのリクエストが無効です。モデル名、声、出力形式を確認してください。{context}。API応答: {body}"
    return f"OpenAI互換TTS APIエラー: HTTP {status_code}。{context}。API応答: {body}"


def _is_xai_tts_endpoint(base_url: Optional[str], model: str) -> bool:
    if not base_url:
        return False
    parsed = urlparse(base_url)
    model_key = (model or "").strip().lower()
    return parsed.netloc.lower().endswith("api.x.ai") and model_key in {"xai/grok-tts", "grok-tts"}


def _build_xai_tts_url(base_url: Optional[str]) -> str:
    if not base_url:
        return "https://api.x.ai/v1/tts"
    return base_url.rstrip("/") + "/tts"


def _generate_xai_tts(
    text: str,
    api_key: str,
    voice_id: str,
    room_name: str,
    base_url: Optional[str],
    response_format: Optional[str],
    extra_body: Optional[Dict[str, Any]],
) -> Optional[str]:
    audio_format = (response_format or "mp3").strip().lower()
    final_prompt = _truncate_text(text or "")
    url = _build_xai_tts_url(base_url)
    output_format = {"codec": audio_format}
    payload: Dict[str, Any] = {
        "text": final_prompt,
        "voice_id": voice_id,
        "language": "auto",
        "output_format": output_format,
    }

    if extra_body:
        extra_payload = dict(extra_body)
        extra_output_format = extra_payload.pop("output_format", None)
        if isinstance(extra_output_format, dict):
            output_format.update(extra_output_format)
        payload.update(extra_payload)

    print(f"  - xAIネイティブTTSエンドポイントを使用: {url}")
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            "xAI TTS APIエラー: "
            f"HTTP {response.status_code}。接続先={url}, voice={voice_id}, format={audio_format}。"
            f"API応答: {response.text[:500]}"
        )

    audio_data = response.content
    if not audio_data:
        return None

    extension = audio_format if audio_format != "mpeg" else "mp3"
    filepath = _get_save_path(room_name, voice_id, extension)
    with open(filepath, "wb") as f:
        f.write(audio_data)
    print(f"  - 音声ファイル({extension})を生成しました: {filepath}")
    return filepath


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
    print(f"  - 接続先: {base_url or 'OpenAI公式'}, format: {audio_format}")

    if _is_xai_tts_endpoint(base_url, model):
        return _generate_xai_tts(text, api_key, voice_id, room_name, base_url, audio_format, extra_body)

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

    try:
        response = client.audio.speech.create(**kwargs)
    except APIStatusError as e:
        raise RuntimeError(_format_openai_tts_api_error(e, model, voice_id, base_url, audio_format)) from e
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


def _normalize_voicevox_base_url(base_url: Optional[str], provider: str) -> str:
    default_urls = {
        "aivisspeech": "http://127.0.0.1:10101",
        "voicevox": "http://127.0.0.1:50021",
        "coeiroink": "http://127.0.0.1:50032",
    }
    normalized = (base_url or default_urls.get(provider) or default_urls["voicevox"]).strip()
    return normalized.rstrip("/") + "/"


def _is_wsl_environment() -> bool:
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _get_wsl_host_ip() -> Optional[str]:
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) == 2 and parts[0] == "nameserver":
                    return parts[1]
    except OSError:
        return None
    return None


def _voicevox_engine_url_candidates(base_url: str) -> list[str]:
    candidates = [base_url]
    parsed = urlparse(base_url)
    if parsed.hostname in {"127.0.0.1", "localhost"} and _is_wsl_environment():
        host_ip = _get_wsl_host_ip()
        if host_ip:
            netloc = host_ip
            if parsed.port:
                netloc = f"{host_ip}:{parsed.port}"
            if parsed.username:
                auth = parsed.username
                if parsed.password:
                    auth += f":{parsed.password}"
                netloc = f"{auth}@{netloc}"
            wsl_host_url = urlunparse(parsed._replace(netloc=netloc)).rstrip("/") + "/"
            if wsl_host_url not in candidates:
                candidates.append(wsl_host_url)
    return candidates


def _resolve_voicevox_speaker(base_url: str, voice_id: str) -> int:
    if voice_id and str(voice_id).strip().lower() != "auto":
        return int(str(voice_id).strip())

    response = requests.get(urljoin(base_url, "speakers"), headers={"Connection": "close"}, timeout=15)
    response.raise_for_status()
    speakers = response.json()
    for speaker in speakers:
        for style in speaker.get("styles", []):
            style_id = style.get("id")
            if style_id is not None:
                return int(style_id)
    raise RuntimeError("VOICEVOX互換エンジンから話者IDを取得できませんでした。")


def _parse_voicevox_parameters(style_prompt: Optional[str]) -> Dict[str, float]:
    """
    style_promptからVOICEVOX用の音響パラメータを抽出する。
    対応フォーマット例: 
      "話速: 1.2, 音高: -0.05, 抑揚: 1.1, 音量: 1.0"
      "speedScale=1.2; pitchScale=-0.05"
    """
    import re
    params = {}
    if not style_prompt:
        return params

    patterns = {
        "speedScale": [
            r"話速[:= ]?\s*([0-9.]+)",
            r"speedScale\s*[:= ]?\s*([0-9.]+)",
            r"speed\s*[:= ]?\s*([0-9.]+)"
        ],
        "pitchScale": [
            r"音高[:= ]?\s*([-0-9.]+)",
            r"pitchScale\s*[:= ]?\s*([-0-9.]+)",
            r"pitch\s*[:= ]?\s*([-0-9.]+)"
        ],
        "intonationScale": [
            r"抑揚[:= ]?\s*([0-9.]+)",
            r"intonationScale\s*[:= ]?\s*([0-9.]+)",
            r"intonation\s*[:= ]?\s*([0-9.]+)"
        ],
        "volumeScale": [
            r"音量[:= ]?\s*([0-9.]+)",
            r"volumeScale\s*[:= ]?\s*([0-9.]+)",
            r"volume\s*[:= ]?\s*([0-9.]+)"
        ]
    }

    for key, pat_list in patterns.items():
        for pat in pat_list:
            match = re.search(pat, style_prompt, re.IGNORECASE)
            if match:
                try:
                    params[key] = float(match.group(1))
                    break
                except ValueError:
                    pass
    return params


def _try_generate_voicevox_compatible_tts(
    text: str,
    voice_id: str,
    room_name: str,
    provider: str,
    engine_url: str,
    style_prompt: Optional[str] = None,
    speed_scale: Optional[float] = None,
    pitch_scale: Optional[float] = None,
    intonation_scale: Optional[float] = None,
    volume_scale: Optional[float] = None,
) -> Optional[str]:
    speaker = _resolve_voicevox_speaker(engine_url, voice_id)

    print(f"--- 音声生成開始 (Provider: {provider}, Room: {room_name}, Engine: {engine_url}, Speaker: {speaker}) ---")

    query_response = requests.post(
        urljoin(engine_url, "audio_query"),
        params={"text": text, "speaker": speaker},
        headers={"Connection": "close"},
        timeout=30,
    )
    query_response.raise_for_status()

    query_json = query_response.json()
    vox_params = _parse_voicevox_parameters(style_prompt)
    if speed_scale is not None:
        vox_params["speedScale"] = speed_scale
    if pitch_scale is not None:
        vox_params["pitchScale"] = pitch_scale
    if intonation_scale is not None:
        vox_params["intonationScale"] = intonation_scale
    if volume_scale is not None:
        vox_params["volumeScale"] = volume_scale

    if vox_params:
        print(f"  - VOICEVOXパラメータ適用: {vox_params}")
        for k, v in vox_params.items():
            query_json[k] = v

    synthesis_response = requests.post(
        urljoin(engine_url, "synthesis"),
        params={"speaker": speaker},
        json=query_json,
        headers={"Content-Type": "application/json", "Connection": "close"},
        timeout=120,
    )
    synthesis_response.raise_for_status()

    audio_data = synthesis_response.content
    if not audio_data:
        return None

    filepath = _get_save_path(room_name, f"{provider}_{speaker}", "wav")
    with open(filepath, "wb") as f:
        f.write(audio_data)
    print(f"  - 音声ファイル(WAV)を生成しました: {filepath}")
    return filepath


def _can_use_windows_local_engine_bridge(base_url: str) -> bool:
    parsed = urlparse(base_url)
    return _is_wsl_environment() and parsed.hostname in {"127.0.0.1", "localhost"}


def _build_windows_local_engine_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    return urlunparse(parsed._replace(netloc=f"127.0.0.1:{parsed.port}" if parsed.port else "127.0.0.1")).rstrip("/") + "/"


def _try_generate_voicevox_compatible_tts_via_powershell(
    text: str,
    voice_id: str,
    room_name: str,
    provider: str,
    base_url: str,
    style_prompt: Optional[str] = None,
    speed_scale: Optional[float] = None,
    pitch_scale: Optional[float] = None,
    intonation_scale: Optional[float] = None,
    volume_scale: Optional[float] = None,
) -> Optional[str]:
    engine_url = _build_windows_local_engine_url(base_url)
    text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    voice_b64 = base64.b64encode(str(voice_id or "auto").encode("utf-8")).decode("ascii")
    url_b64 = base64.b64encode(engine_url.encode("utf-8")).decode("ascii")

    vox_params = _parse_voicevox_parameters(style_prompt)
    if speed_scale is not None:
        vox_params["speedScale"] = speed_scale
    if pitch_scale is not None:
        vox_params["pitchScale"] = pitch_scale
    if intonation_scale is not None:
        vox_params["intonationScale"] = intonation_scale
    if volume_scale is not None:
        vox_params["volumeScale"] = volume_scale

    params_json = json.dumps(vox_params)
    params_b64 = base64.b64encode(params_json.encode("utf-8")).decode("ascii")

    script = r"""
$ErrorActionPreference = "Stop"
$text = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("__TEXT_B64__"))
$voiceId = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("__VOICE_B64__"))
$baseUrl = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("__URL_B64__")).TrimEnd('/') + '/'
$paramsJson = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("__PARAMS_B64__"))
Add-Type -AssemblyName System.Net.Http
$client = [System.Net.Http.HttpClient]::new()
$client.Timeout = [System.TimeSpan]::FromSeconds(180)
$client.DefaultRequestHeaders.Connection.Add("close")
try {
    if ([string]::IsNullOrWhiteSpace($voiceId) -or $voiceId.ToLowerInvariant() -eq "auto") {
        $speakersTask = $client.GetAsync($baseUrl + "speakers")
        $speakersTask.Wait()
        $speakersResponse = $speakersTask.Result
        if ($null -eq $speakersResponse) { throw "Speakers response was null" }
        $speakersResponse.EnsureSuccessStatusCode() | Out-Null
        $speakers = $speakersResponse.Content.ReadAsStringAsync().Result | ConvertFrom-Json
        $voiceId = [string]$speakers[0].styles[0].id
    }
    $escapedText = [System.Uri]::EscapeDataString($text)
    $queryUri = $baseUrl + "audio_query?text=" + $escapedText + "&speaker=" + $voiceId
    $queryTask = $client.PostAsync($queryUri, $null)
    $queryTask.Wait()
    $queryResponse = $queryTask.Result
    if ($null -eq $queryResponse) { throw "Query response was null" }
    $queryResponse.EnsureSuccessStatusCode() | Out-Null
    $queryJson = $queryResponse.Content.ReadAsStringAsync().Result
    
    $queryJsonObj = ConvertFrom-Json $queryJson
    $extraParams = ConvertFrom-Json $paramsJson
    if ($null -ne $extraParams) {
        foreach ($prop in $extraParams.PSObject.Properties) {
            $queryJsonObj.($prop.Name) = $prop.Value
        }
    }
    $queryJson = ConvertTo-Json $queryJsonObj -Depth 10
    
    $content = [System.Net.Http.StringContent]::new($queryJson, [System.Text.Encoding]::UTF8, "application/json")
    $synthesisUri = $baseUrl + "synthesis?speaker=" + $voiceId
    $synthesisTask = $client.PostAsync($synthesisUri, $content)
    $synthesisTask.Wait()
    $synthesisResponse = $synthesisTask.Result
    if ($null -eq $synthesisResponse) { throw "Synthesis response was null" }
    $synthesisResponse.EnsureSuccessStatusCode() | Out-Null
    $bytes = $synthesisResponse.Content.ReadAsByteArrayAsync().Result
    [Console]::Out.WriteLine("VOICE_ID:" + $voiceId)
    [Console]::Out.WriteLine([System.Convert]::ToBase64String($bytes))
} finally {
    $client.Dispose()
}
"""
    script = script.replace("__TEXT_B64__", text_b64).replace("__VOICE_B64__", voice_b64).replace("__URL_B64__", url_b64).replace("__PARAMS_B64__", params_b64)
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Windows側の{provider} Engine呼び出しに失敗しました: {completed.stderr.strip() or completed.stdout.strip()}")

    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    voice_line = next((line for line in lines if line.startswith("VOICE_ID:")), "")
    resolved_voice_id = voice_line.removeprefix("VOICE_ID:") if voice_line else str(voice_id or "auto")
    audio_b64 = next((line for line in reversed(lines) if not line.startswith("VOICE_ID:")), "")
    audio_data = base64.b64decode(audio_b64)
    if not audio_data:
        return None

    filepath = _get_save_path(room_name, f"{provider}_{resolved_voice_id}", "wav")
    with open(filepath, "wb") as f:
        f.write(audio_data)
    print(f"  - Windows側VOICEVOX互換エンジン経由で音声ファイル(WAV)を生成しました: {filepath}")
    return filepath


def _generate_voicevox_compatible_tts(
    text: str,
    voice_id: str,
    room_name: str,
    provider: str,
    base_url: Optional[str],
    style_prompt: Optional[str] = None,
    speed_scale: Optional[float] = None,
    pitch_scale: Optional[float] = None,
    intonation_scale: Optional[float] = None,
    volume_scale: Optional[float] = None,
) -> Optional[str]:
    final_text = _truncate_text(text or "")
    requested_url = _normalize_voicevox_base_url(base_url, provider)
    last_error: Optional[Exception] = None

    for engine_url in _voicevox_engine_url_candidates(requested_url):
        if engine_url != requested_url:
            print(f"  - WSL環境のためWindowsホスト側URLも試行します: {engine_url}")
        try:
            return _try_generate_voicevox_compatible_tts(
                final_text, voice_id, room_name, provider, engine_url, style_prompt,
                speed_scale, pitch_scale, intonation_scale, volume_scale
            )
        except requests.exceptions.ConnectionError as e:
            last_error = e
            print(f"  - VOICEVOX互換エンジンへ接続できませんでした: {engine_url} ({e})")

    if last_error:
        if _can_use_windows_local_engine_bridge(requested_url):
            print("  - WSLから直接接続できないため、Windows側localhostへPowerShell経由で接続します。")
            return _try_generate_voicevox_compatible_tts_via_powershell(
                final_text, voice_id, room_name, provider, requested_url, style_prompt,
                speed_scale, pitch_scale, intonation_scale, volume_scale
            )

        provider_labels = {
            "aivisspeech": "AivisSpeech",
            "voicevox": "VOICEVOX",
        }
        label = provider_labels.get(provider, provider)
        raise RuntimeError(
            f"{label} Engineに接続できませんでした。エンジンを起動しているか、TTSモデル欄のURL（現在: {requested_url}）とポートを確認してください。"
        ) from last_error
    return None


def _resolve_coeiroink_speaker(base_url: str, voice_id: str, room_name: str) -> tuple[str, int]:
    """
    COEIROINK v2のGET /v1/speakersから、voice_idとroom_nameに基づいて適切なspeakerUuidとstyleIdを解決する。
    """
    voice_str = str(voice_id or "auto").strip()

    # 1. "UUID:styleId" の直接指定パターン
    if ":" in voice_str:
        parts = voice_str.split(":", 1)
        return parts[0], int(parts[1])

    # 2. APIから話者一覧を取得
    response = requests.get(urljoin(base_url, "v1/speakers"), headers={"Connection": "close"}, timeout=15)
    response.raise_for_status()
    speakers = response.json()
    if not speakers:
        raise RuntimeError("COEIROINKから利用可能な話者を取得できませんでした。")

    # 3. ルーム名による部分一致検索
    target_room = room_name.strip() if room_name else ""
    if target_room:
        for spk in speakers:
            spk_name = spk.get("speakerName", "")
            if spk_name and (spk_name in target_room or target_room in spk_name):
                styles = spk.get("styles", [])
                if not styles:
                    continue
                try:
                    style_id_val = int(voice_str)
                    for st in styles:
                        if st.get("styleId") == style_id_val:
                            return spk.get("speakerUuid"), style_id_val
                except ValueError:
                    pass
                return spk.get("speakerUuid"), styles[0].get("styleId")

    # 4. ルーム名不一致時のフォールバック: voice_idが整数なら全話者からstyleIdが一致するものを探す
    try:
        style_id_val = int(voice_str)
        for spk in speakers:
            for st in spk.get("styles", []):
                if st.get("styleId") == style_id_val:
                    return spk.get("speakerUuid"), style_id_val
    except ValueError:
        pass

    # 5. 最終フォールバック: 最初の話者の最初のスタイル
    first_spk = speakers[0]
    first_style = first_spk.get("styles", [])[0]
    return first_spk.get("speakerUuid"), first_style.get("styleId")


def _try_generate_coeiroink_tts(
    text: str,
    voice_id: str,
    room_name: str,
    engine_url: str,
    style_prompt: Optional[str] = None,
    speed_scale: Optional[float] = None,
    pitch_scale: Optional[float] = None,
    intonation_scale: Optional[float] = None,
    volume_scale: Optional[float] = None,
) -> Optional[str]:
    speaker_uuid, style_id = _resolve_coeiroink_speaker(engine_url, voice_id, room_name)

    print(f"--- 音声生成開始 (Provider: coeiroink, Room: {room_name}, Engine: {engine_url}, SpeakerUuid: {speaker_uuid}, StyleId: {style_id}) ---")

    vox_params = _parse_voicevox_parameters(style_prompt)
    if speed_scale is not None:
        vox_params["speedScale"] = speed_scale
    if pitch_scale is not None:
        vox_params["pitchScale"] = pitch_scale
    if intonation_scale is not None:
        vox_params["intonationScale"] = intonation_scale
    if volume_scale is not None:
        vox_params["volumeScale"] = volume_scale

    payload = {
        "text": text,
        "speakerUuid": speaker_uuid,
        "styleId": style_id,
        "speedScale": vox_params.get("speedScale", 1.0),
        "volumeScale": vox_params.get("volumeScale", 1.0),
        "pitchScale": vox_params.get("pitchScale", 0.0),
        "intonationScale": vox_params.get("intonationScale", 1.0),
        "prePhonemeLength": 0.1,
        "postPhonemeLength": 0.5,
        "outputSamplingRate": 44100
    }

    synthesis_response = requests.post(
        urljoin(engine_url, "v1/synthesis"),
        json=payload,
        headers={"Content-Type": "application/json", "Connection": "close"},
        timeout=120,
    )
    synthesis_response.raise_for_status()

    audio_data = synthesis_response.content
    if not audio_data:
        return None

    filepath = _get_save_path(room_name, f"coeiroink_{style_id}", "wav")
    with open(filepath, "wb") as f:
        f.write(audio_data)
    print(f"  - 音声ファイル(WAV)を生成しました: {filepath}")
    return filepath


def _try_generate_coeiroink_tts_via_powershell(
    text: str,
    voice_id: str,
    room_name: str,
    base_url: str,
    style_prompt: Optional[str] = None,
    speed_scale: Optional[float] = None,
    pitch_scale: Optional[float] = None,
    intonation_scale: Optional[float] = None,
    volume_scale: Optional[float] = None,
) -> Optional[str]:
    engine_url = _build_windows_local_engine_url(base_url)
    print(f"--- [DEBUG:PowerShellBridge] coeiroink synthesis starting. base_url={base_url}, engine_url={engine_url}, voice_id={voice_id} ---")
    voice_b64 = base64.b64encode(str(voice_id or "auto").encode("utf-8")).decode("ascii")
    room_b64 = base64.b64encode(str(room_name or "").encode("utf-8")).decode("ascii")
    url_b64 = base64.b64encode(engine_url.encode("utf-8")).decode("ascii")

    vox_params = _parse_voicevox_parameters(style_prompt)
    if speed_scale is not None:
        vox_params["speedScale"] = speed_scale
    if pitch_scale is not None:
        vox_params["pitchScale"] = pitch_scale
    if intonation_scale is not None:
        vox_params["intonationScale"] = intonation_scale
    if volume_scale is not None:
        vox_params["volumeScale"] = volume_scale

    payload = {
        "text": text,
        "speakerUuid": "__SPEAKER_UUID__",
        "styleId": 999999,  # 数値置換用の仮ID
        "speedScale": vox_params.get("speedScale", 1.0),
        "volumeScale": vox_params.get("volumeScale", 1.0),
        "pitchScale": vox_params.get("pitchScale", 0.0),
        "intonationScale": vox_params.get("intonationScale", 1.0),
        "prePhonemeLength": 0.1,
        "postPhonemeLength": 0.5,
        "outputSamplingRate": 44100
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    payload_b64 = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")

    script = r"""
$ErrorActionPreference = "Stop"
$voiceId = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("__VOICE_B64__"))
$roomName = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("__ROOM_B64__"))
$baseUrl = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("__URL_B64__")).TrimEnd('/') + '/'
Add-Type -AssemblyName System.Net.Http
$client = [System.Net.Http.HttpClient]::new()
$client.Timeout = [System.TimeSpan]::FromSeconds(180)
$client.DefaultRequestHeaders.Connection.Add("close")

try {
    [Console]::Out.WriteLine("[PS_DEBUG] Fetching speakers from: " + $baseUrl + "v1/speakers")
    $speakersTask = $client.GetAsync($baseUrl + "v1/speakers")
    $speakersTask.Wait()
    $speakersResponse = $speakersTask.Result
    if ($null -eq $speakersResponse) { throw "Speakers response was null" }
    $speakersResponse.EnsureSuccessStatusCode() | Out-Null
    $speakers = $speakersResponse.Content.ReadAsStringAsync().Result | ConvertFrom-Json
    [Console]::Out.WriteLine("[PS_DEBUG] Speakers fetched successfully.")

    $speakerUuid = $null
    $styleId = $null

    if ($voiceId.Contains(":")) {
        $parts = $voiceId.Split(":", 2)
        $speakerUuid = $parts[0]
        $styleId = [int]$parts[1]
    } else {
        if (-not [string]::IsNullOrWhiteSpace($roomName)) {
            foreach ($spk in $speakers) {
                if ($spk.speakerName.Contains($roomName) -or $roomName.Contains($spk.speakerName)) {
                    $speakerUuid = $spk.speakerUuid
                    $styles = $spk.styles
                    if ($null -ne $styles -and $styles.Length -gt 0) {
                        $parsedInt = 0
                        if ([int]::TryParse($voiceId, [ref]$parsedInt)) {
                            foreach ($st in $styles) {
                                if ([int]$st.styleId -eq $parsedInt) {
                                    $styleId = [int]$st.styleId
                                    break
                                }
                            }
                        }
                        if ($null -eq $styleId) {
                            $styleId = [int]$styles[0].styleId
                        }
                    }
                    break
                }
            }
        }

        if ($null -eq $speakerUuid) {
            $parsedInt = 0
            if ([int]::TryParse($voiceId, [ref]$parsedInt)) {
                foreach ($spk in $speakers) {
                    foreach ($st in $spk.styles) {
                        if ([int]$st.styleId -eq $parsedInt) {
                            $speakerUuid = $spk.speakerUuid
                            $styleId = [int]$st.styleId
                            break
                        }
                    }
                    if ($null -ne $speakerUuid) { break }
                }
            }
        }

        if ($null -eq $speakerUuid) {
            $speakerUuid = $speakers[0].speakerUuid
            $styleId = [int]$speakers[0].styles[0].styleId
        }
    }

    $payloadJson = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("__PAYLOAD_B64__"))
    $payloadJson = $payloadJson.Replace("__SPEAKER_UUID__", $speakerUuid).Replace("999999", $styleId)

    [Console]::Out.WriteLine("[PS_DEBUG] Sending synthesis request to: " + $baseUrl + "v1/synthesis")
    $content = [System.Net.Http.StringContent]::new($payloadJson, [System.Text.Encoding]::UTF8, "application/json")
    $synthesisTask = $client.PostAsync($baseUrl + "v1/synthesis", $content)
    $synthesisTask.Wait()
    $synthesisResponse = $synthesisTask.Result
    if ($null -eq $synthesisResponse) { throw "Synthesis response was null" }
    $synthesisResponse.EnsureSuccessStatusCode() | Out-Null
    [Console]::Out.WriteLine("[PS_DEBUG] Synthesis completed successfully.")

    $bytes = $synthesisResponse.Content.ReadAsByteArrayAsync().Result
    [Console]::Out.WriteLine("STYLE_ID:" + $styleId)
    [Console]::Out.WriteLine([System.Convert]::ToBase64String($bytes))
} catch {
    [Console]::Error.WriteLine("ERROR: " + $_.Exception.ToString())
    if ($null -ne $_.Exception.InnerException) {
        [Console]::Error.WriteLine("INNER_ERROR: " + $_.Exception.InnerException.ToString())
    }
    exit 1
} finally {
    $client.Dispose()
}
"""
    script = script.replace("__VOICE_B64__", voice_b64).replace("__ROOM_B64__", room_b64).replace("__URL_B64__", url_b64).replace("__PAYLOAD_B64__", payload_b64)
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if completed.returncode != 0:
        err_msg = f"Windows側のcoeiroink Engine呼び出しに失敗しました。\n[STDOUT]\n{completed.stdout.strip()}\n[STDERR]\n{completed.stderr.strip()}"
        raise RuntimeError(err_msg)

    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    style_line = next((line for line in lines if line.startswith("STYLE_ID:")), "")
    resolved_style_id = style_line.removeprefix("STYLE_ID:") if style_line else str(voice_id or "auto")
    audio_b64 = next((line for line in reversed(lines) if not line.startswith("STYLE_ID:")), "")
    audio_data = base64.b64decode(audio_b64)
    if not audio_data:
        return None

    filepath = _get_save_path(room_name, f"coeiroink_{resolved_style_id}", "wav")
    with open(filepath, "wb") as f:
        f.write(audio_data)
    print(f"  - Windows側COEIROINK経由で音声ファイル(WAV)を生成しました: {filepath}")
    return filepath


def _generate_coeiroink_tts(
    text: str,
    voice_id: str,
    room_name: str,
    base_url: Optional[str],
    style_prompt: Optional[str] = None,
    speed_scale: Optional[float] = None,
    pitch_scale: Optional[float] = None,
    intonation_scale: Optional[float] = None,
    volume_scale: Optional[float] = None,
) -> Optional[str]:
    final_text = _truncate_text(text or "").strip()
    if not final_text:
        return None
    requested_url = _normalize_voicevox_base_url(base_url, "coeiroink")
    print(f"--- [DEBUG:COEIROINK_TTS] base_url={base_url}, requested_url={requested_url}, voice_id={voice_id}, text={final_text[:50]} ---")
    last_error: Optional[Exception] = None

    for engine_url in _voicevox_engine_url_candidates(requested_url):
        if engine_url != requested_url:
            print(f"  - WSL環境のためWindowsホスト側URLも試行します: {engine_url}")
        try:
            return _try_generate_coeiroink_tts(
                final_text, voice_id, room_name, engine_url, style_prompt,
                speed_scale, pitch_scale, intonation_scale, volume_scale
            )
        except requests.exceptions.ConnectionError as e:
            last_error = e
            print(f"  - COEIROINKエンジンへ接続できませんでした: {engine_url} ({e})")

    if last_error:
        if _can_use_windows_local_engine_bridge(requested_url):
            print("  - WSLから直接接続できないため、Windows側localhostへPowerShell経由で接続します。")
            return _try_generate_coeiroink_tts_via_powershell(
                final_text, voice_id, room_name, requested_url, style_prompt,
                speed_scale, pitch_scale, intonation_scale, volume_scale
            )

        raise RuntimeError(
            f"COEIROINK Engineに接続できませんでした。エンジンを起動しているか、TTSモデル欄のURL（現在: {requested_url}）とポートを確認してください。"
        ) from last_error
    return None


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
    speed_scale: Optional[float] = None,
    pitch_scale: Optional[float] = None,
    intonation_scale: Optional[float] = None,
    volume_scale: Optional[float] = None,
) -> Optional[str]:
    """
    指定されたTTSプロバイダで音声を生成し、再生可能な音声ファイルとして保存する。
    既存呼び出し互換のため、デフォルトはGemini TTS。
    """
    try:
        provider = (tts_provider or "gemini").strip().lower()
        if provider == "google":
            provider = "gemini"

        local_voicevox_providers = {"aivisspeech", "voicevox"}
        if provider == "coeiroink":
            local_voicevox_providers = set()  # coeiroinkは独自処理

        if provider not in local_voicevox_providers and provider != "coeiroink" and (not api_key or str(api_key).startswith("YOUR_API_KEY")):
            return "【エラー】TTS用APIキーが設定されていません。"

        if provider == "gemini":
            return _generate_gemini_tts(text, api_key, voice_id, room_name, style_prompt, tts_model)
        if provider in {"openai", "openai_compatible"}:
            return _generate_openai_compatible_tts(
                text, api_key, voice_id, room_name, style_prompt, tts_model, base_url, response_format or "mp3", extra_body
            )
        if provider == "elevenlabs":
            return _generate_elevenlabs_tts(text, api_key, voice_id, room_name, style_prompt, tts_model, response_format or "mp3")
        if provider == "coeiroink":
            return _generate_coeiroink_tts(
                text, voice_id, room_name, tts_model, style_prompt,
                speed_scale, pitch_scale, intonation_scale, volume_scale
            )
        if provider in local_voicevox_providers:
            return _generate_voicevox_compatible_tts(
                text, voice_id, room_name, provider, tts_model, style_prompt,
                speed_scale, pitch_scale, intonation_scale, volume_scale
            )

        return f"【エラー】未対応のTTSプロバイダです: {tts_provider}"

    except google.genai.errors.ClientError as e:
        if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
            error_detail = str(e).upper()
            limit_type = "RPD" if any(
                marker in error_detail
                for marker in (
                    "GENERATEREQUESTSPERDAY",
                    "PERDAY",
                    "PER_DAY",
                    "FREE_TIER_REQUESTS",
                    "DAILY",
                    "RPD",
                )
            ) else "RPM"
            error_message = f"【エラー】音声生成APIの利用上限に達しました（{limit_type}）。しばらく待ってから再試行してください。"
        else:
            error_message = "【エラー】APIリクエストが無効です。プロンプト、モデル、音声名を確認してください。"
        print(f"--- {error_message} 詳細: {e} ---")
        return error_message
    except google.genai.errors.ServerError as e:
        error_message = "【エラー】APIサーバー側で内部エラーが発生しました。一時的な問題の可能性があります。"
        print(f"--- {error_message} 詳細: {e} ---")
        return error_message
    except RuntimeError as e:
        error_message = f"【エラー】{e}"
        print(f"--- {error_message} ---")
        return error_message
    except Exception as e:
        error_message = "【エラー】音声生成中に予期せぬエラーが発生しました。"
        print(f"--- {error_message} 詳細: {e} ---")
        traceback.print_exc()
        return error_message


def _parse_speakers_json(provider: str, speakers_data: Any) -> Dict[str, str]:
    parsed = {}
    if not isinstance(speakers_data, list):
        return parsed

    provider = provider.strip().lower()
    if provider == "coeiroink":
        for spk in speakers_data:
            spk_name = spk.get("speakerName", "")
            spk_uuid = spk.get("speakerUuid", "")
            for style in spk.get("styles", []):
                style_name = style.get("styleName", "")
                style_id = style.get("styleId")
                if spk_uuid and style_id is not None:
                    key = f"{spk_uuid}:{style_id}"
                    parsed[key] = f"{spk_name} ({style_name})"
    else:
        for spk in speakers_data:
            spk_name = spk.get("name", "")
            for style in spk.get("styles", []):
                style_name = style.get("name", "")
                style_id = style.get("id")
                if style_id is not None:
                    parsed[str(style_id)] = f"{spk_name} ({style_name})"
    return parsed


def _fetch_speakers_via_powershell(provider: str, base_url: str) -> Optional[Dict[str, str]]:
    engine_url = _build_windows_local_engine_url(base_url)
    url_b64 = base64.b64encode(engine_url.encode("utf-8")).decode("ascii")

    endpoint = "v1/speakers" if provider == "coeiroink" else "speakers"

    script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"
$baseUrl = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("__URL_B64__")).TrimEnd('/') + '/'
$endpoint = "__ENDPOINT__"
Add-Type -AssemblyName System.Net.Http
$client = [System.Net.Http.HttpClient]::new()
$client.Timeout = [System.TimeSpan]::FromSeconds(3)
$client.DefaultRequestHeaders.Connection.Add("close")
try {
    $speakersTask = $client.GetAsync($baseUrl + $endpoint)
    $speakersTask.Wait()
    $speakersResponse = $speakersTask.Result
    if ($null -eq $speakersResponse) { throw "Speakers response was null" }
    $speakersResponse.EnsureSuccessStatusCode() | Out-Null
    $speakersJson = $speakersResponse.Content.ReadAsStringAsync().Result
    [Console]::Out.WriteLine($speakersJson)
} finally {
    $client.Dispose()
}
"""
    script = script.replace("__URL_B64__", url_b64).replace("__ENDPOINT__", endpoint)
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"PowerShell経由の話者リスト取得に失敗しました: {completed.stderr.strip()}")

    stdout_str = completed.stdout.strip()
    if not stdout_str:
        return None

    try:
        start_idx = stdout_str.find('[')
        end_idx = stdout_str.rfind(']')
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = stdout_str[start_idx:end_idx+1]
            speakers_data = json.loads(json_str)
            return _parse_speakers_json(provider, speakers_data)
    except Exception as e:
        print(f"  - PowerShell出力のJSONパース失敗: {e}. Output was: {stdout_str[:200]}")

    return None


def fetch_local_engine_speakers(provider: str, engine_url: str) -> Optional[Dict[str, str]]:
    """
    指定されたローカルTTSエンジンから話者リストを取得する。
    WSL/Windowsブリッジもサポート。
    """
    provider = provider.strip().lower()
    requested_url = _normalize_voicevox_base_url(engine_url, provider)

    # 1. 直接接続による取得
    for url in _voicevox_engine_url_candidates(requested_url):
        try:
            if provider == "coeiroink":
                endpoint = urljoin(url, "v1/speakers")
            else:
                endpoint = urljoin(url, "speakers")

            response = requests.get(endpoint, headers={"Connection": "close"}, timeout=1.5)
            response.raise_for_status()
            speakers_data = response.json()
            return _parse_speakers_json(provider, speakers_data)
        except Exception as e:
            print(f"  - 話者リスト直接取得失敗 ({url}): {e}")

    # 2. 直接接続失敗時のPowerShellブリッジ試行
    if _can_use_windows_local_engine_bridge(requested_url):
        print("  - WSLから直接接続できないため、Windows側へPowerShell経由で話者リストを取得します。")
        try:
            return _fetch_speakers_via_powershell(provider, requested_url)
        except Exception as e:
            print(f"  - PowerShell経由の話者リスト取得失敗: {e}")

    return None
