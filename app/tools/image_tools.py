# tools/image_tools.py

import os
import io
import base64
import datetime
import mimetypes
import traceback
from urllib.parse import quote
from typing import List, Optional
import requests as http_requests
from PIL import Image
import google.genai as genai
import httpx
from langchain_core.tools import tool
from google.genai import types
import config_manager 
import constants
import closet_manager
import usage_ledger
from file_lock_utils import safe_json_read, safe_json_write


REFERENCE_IMAGE_LIMIT = 4
REFERENCE_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
REFERENCE_UNSUPPORTED_NOTE = "\n（このモデルは参照画像未対応のため、プロンプト記述のみ反映）"
REFERENCE_CAPTION_NOTE = "\n（参照画像はキャプションとしてプロンプトに反映）"
REFERENCE_TEXT_MAX_CHARS = 260


def image_model_supports_reference(provider: str, model: str) -> bool:
    """Return whether Nexus Ark currently passes reference image data to this image model."""
    provider = str(provider or "").strip().lower()
    model = str(model or "").strip().lower()
    if provider == "gemini":
        return "image" in model
    if provider == "openai":
        return "gpt-image" in model
    return False


def _normalize_reference_image_paths(reference_image_paths: Optional[List[str]]) -> List[str]:
    result = []
    for path in reference_image_paths or []:
        path = str(path or "").strip()
        if path and path not in result:
            result.append(path)
    return result


def _load_reference_images(reference_image_paths: Optional[List[str]]) -> List[Image.Image]:
    images = []
    for path in _normalize_reference_image_paths(reference_image_paths):
        if len(images) >= REFERENCE_IMAGE_LIMIT:
            break
        if os.path.splitext(path)[1].lower() not in REFERENCE_IMAGE_SUFFIXES:
            print(f"  - [Image Reference] 未対応の拡張子のためスキップ: {path}")
            continue
        if not os.path.exists(path):
            print(f"  - [Image Reference] 参照画像が見つからないためスキップ: {path}")
            continue
        try:
            images.append(Image.open(path))
        except Exception as e:
            print(f"  - [Image Reference] 参照画像を開けないためスキップ: {path} ({e})")
    return images


def _open_reference_files(reference_image_paths: Optional[List[str]]) -> List[object]:
    files = []
    for path in _normalize_reference_image_paths(reference_image_paths):
        if len(files) >= REFERENCE_IMAGE_LIMIT:
            break
        if os.path.splitext(path)[1].lower() not in REFERENCE_IMAGE_SUFFIXES:
            print(f"  - [Image Reference] 未対応の拡張子のためスキップ: {path}")
            continue
        if not os.path.exists(path):
            print(f"  - [Image Reference] 参照画像が見つからないためスキップ: {path}")
            continue
        try:
            files.append(open(path, "rb"))
        except OSError as e:
            print(f"  - [Image Reference] 参照画像を開けないためスキップ: {path} ({e})")
    return files


def _close_reference_files(files: List[object]) -> None:
    for file_obj in files or []:
        try:
            file_obj.close()
        except Exception:
            pass


def _path_to_data_url(path: str) -> Optional[str]:
    path = str(path or "").strip()
    if not path or os.path.splitext(path)[1].lower() not in REFERENCE_IMAGE_SUFFIXES:
        return None
    if not os.path.exists(path):
        return None
    mime_type = mimetypes.guess_type(path)[0] or "image/png"
    if mime_type == "image/jpg":
        mime_type = "image/jpeg"
    try:
        with open(path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        print(f"  - [Image Reference] data URL化をスキップ: {path} ({e})")
        return None
    return f"data:{mime_type};base64,{encoded}"


def _reference_data_urls(reference_image_paths: Optional[List[str]]) -> List[str]:
    urls = []
    for path in _normalize_reference_image_paths(reference_image_paths):
        if len(urls) >= REFERENCE_IMAGE_LIMIT:
            break
        data_url = _path_to_data_url(path)
        if data_url:
            urls.append(data_url)
    return urls


def _append_reference_unsupported_note(result: str, reference_image_paths: Optional[List[str]]) -> str:
    if not _normalize_reference_image_paths(reference_image_paths):
        return result
    if str(result or "").startswith("【エラー】"):
        return result
    return f"{result}{REFERENCE_UNSUPPORTED_NOTE}"


def _append_reference_caption_note(result: str, captioned: bool) -> str:
    if not captioned:
        return result
    if str(result or "").startswith("【エラー】"):
        return result
    return f"{result}{REFERENCE_CAPTION_NOTE}"


def _caption_cache_path(room_name: str) -> str:
    room_name = str(room_name or "").strip()
    return os.path.join(constants.ROOMS_DIR, room_name, "closet", "caption_cache.json")


def _reference_cache_key(path: str) -> Optional[str]:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return f"{path}|{stat.st_mtime_ns}"


def _read_caption_cache(room_name: str) -> dict:
    try:
        data = safe_json_read(_caption_cache_path(room_name), default={"captions": {}})
    except Exception:
        return {"captions": {}}
    return data if isinstance(data, dict) else {"captions": {}}


def _write_caption_cache(room_name: str, data: dict) -> None:
    try:
        safe_json_write(_caption_cache_path(room_name), data if isinstance(data, dict) else {"captions": {}})
    except Exception as e:
        print(f"  - [Image Reference] キャプションキャッシュ保存をスキップ: {e}")


def _compact_reference_text(text: str) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > REFERENCE_TEXT_MAX_CHARS:
        return text[:REFERENCE_TEXT_MAX_CHARS].rstrip() + "..."
    return text


def _is_failed_caption(text: str) -> bool:
    text = str(text or "").strip()
    return not text or ("キャプション生成エラー" in text) or ("生成できませんでした" in text)


def resolve_reference_texts(room_name: str, reference_image_paths: Optional[List[str]], api_key_name: str = None) -> List[str]:
    """Resolve reference images into compact text for image models without image-reference support."""
    texts = []
    cache = None
    cache_changed = False

    for path in _normalize_reference_image_paths(reference_image_paths)[:REFERENCE_IMAGE_LIMIT]:
        if os.path.splitext(path)[1].lower() not in REFERENCE_IMAGE_SUFFIXES:
            continue
        if not os.path.exists(path):
            continue

        try:
            description = closet_manager.describe_reference_image(room_name, path)
        except Exception:
            description = None
        if description:
            texts.append(_compact_reference_text(description))
            continue

        cache_key = _reference_cache_key(path)
        if not cache_key:
            continue
        if cache is None:
            cache = _read_caption_cache(room_name)
        captions = cache.setdefault("captions", {})
        if not isinstance(captions, dict):
            captions = {}
            cache["captions"] = captions
        cached_caption = captions.get(cache_key)
        if cached_caption:
            texts.append(_compact_reference_text(cached_caption))
            continue

        caption = generate_image_caption(path, api_key_name=api_key_name)
        if _is_failed_caption(caption):
            continue
        caption = _compact_reference_text(caption)
        captions[cache_key] = caption
        cache_changed = True
        texts.append(caption)

    if cache is not None and cache_changed:
        _write_caption_cache(room_name, cache)

    result = []
    for text in texts:
        if text and text not in result:
            result.append(text)
    return result


def _generate_with_gemini(
    prompt: str,
    model_name: str,
    api_key: str,
    save_dir: str,
    room_name: str,
    api_key_name: str = "Unknown",
    reference_image_paths: Optional[List[str]] = None,
) -> str:
    """Gemini (google.genai) で画像を生成する"""
    client = genai.Client(api_key=api_key)
    reference_images = []
    
    try:
        contents = prompt
        if image_model_supports_reference("gemini", model_name):
            reference_images = _load_reference_images(reference_image_paths)
            if reference_images:
                contents = [*reference_images, prompt]
                print(f"  - [Image Reference] Geminiへ参照画像を {len(reference_images)} 件渡します。")

        response = client.models.generate_content(
            model=model_name,
            contents=contents,
        )
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "Resource Exhausted" in error_str:
            print(f"  - 画像生成で429エラーが発生しました。キー: {api_key_name}, モデル: {model_name}")
            # 枯渇状態を記録（有料キーの場合は内部でスキップされる）
            config_manager.mark_key_as_exhausted(api_key_name, model_name)
            return "【エラー】画像生成の制限（無料枠またはRPM制限）に達しました。しばらく待ってから再度お試しください。"
        # その他のエラーは呼び出し元で処理（または再送出）
        raise
    finally:
        for image in reference_images:
            try:
                image.close()
            except Exception:
                pass

    image_data = None
    image_text_response = ""
    if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
        for part in response.candidates[0].content.parts:
            if part.text:
                image_text_response = part.text
                print(f"  - APIからのテキスト応答: {part.text}")
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                image_data = io.BytesIO(part.inline_data.data)

    if not image_data:
        return "【エラー】APIから画像データが返されませんでした。プロンプトが不適切か、安全フィルターにブロックされた可能性があります。"

    image = Image.open(image_data)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{room_name.lower()}_{timestamp}.png"
    save_path = os.path.join(save_dir, filename)

    image.save(save_path, "PNG")
    print(f"  - 画像を保存しました: {save_path}")

    model_comment = f"\nAI Model Comment: {image_text_response}" if image_text_response else ""
    return f"[Generated Image: {save_path}]{model_comment}\n📝 Prompt: {prompt}\n画像生成完了。この画像についてコメントを添えてください。\n[VIEW_IMAGE: {save_path}]"



def _extract_openrouter_image_url(image_item) -> str:
    if isinstance(image_item, dict):
        image_url = image_item.get("image_url")
        if isinstance(image_url, dict):
            return image_url.get("url")
        return image_url

    image_url = getattr(image_item, "image_url", None)
    if isinstance(image_url, dict):
        return image_url.get("url")
    if image_url is not None:
        return getattr(image_url, "url", image_url)
    return None


def _generate_with_openrouter(
    prompt: str,
    model_name: str,
    base_url: str,
    api_key: str,
    save_dir: str,
    room_name: str,
    reference_image_paths: Optional[List[str]] = None,
    api_key_name: str = None,
) -> str:
    """OpenRouter の Chat Completions 画像生成方式で画像を生成する。"""
    reference_urls = []
    if _openrouter_model_supports_reference(model_name):
        reference_urls = _reference_data_urls(reference_image_paths)

    # 参照非対応モデルでも、参照画像をキャプション化してプロンプトへ反映する（5-cap フォールバック）。
    captioned_reference = False
    if reference_image_paths and not reference_urls:
        reference_texts = resolve_reference_texts(room_name, reference_image_paths, api_key_name=api_key_name)
        if reference_texts:
            prompt = (
                f"{prompt}\n\n[参考画像の外見]\n"
                + "\n".join(f"- {text}" for text in reference_texts)
            )
            captioned_reference = True

    message_content = prompt
    if reference_urls:
        message_content = [{"type": "text", "text": prompt}]
        message_content.extend(
            {"type": "image_url", "image_url": {"url": data_url}}
            for data_url in reference_urls
        )
        print(f"  [OpenRouter Image] 参照画像を {len(reference_urls)} 件渡します。")

    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": message_content}],
        "modalities": ["image", "text"],
        "image_config": {"aspect_ratio": "1:1", "image_size": "1K"},
    }

    print(f"  [OpenRouter Image] POST {endpoint}, model={model_name}")
    response = http_requests.post(endpoint, headers=headers, json=payload, timeout=180)

    if response.status_code == 429:
        return "【エラー】OpenRouter のレート制限に達しました。しばらく待ってから再度お試しください。"
    if response.status_code != 200:
        detail = (response.text or "")[:300]
        return f"【エラー】OpenRouter APIエラー (HTTP {response.status_code}): {detail}"

    try:
        data = response.json()
    except ValueError:
        return "【エラー】OpenRouter APIからJSONではない応答が返されました。"

    choices = data.get("choices") or []
    if not choices:
        return "【エラー】OpenRouter APIから画像データが返されませんでした。モデルが画像生成に対応していない可能性があります。"

    message = (choices[0] or {}).get("message") or {}
    images = message.get("images") or []
    if not images:
        return "【エラー】OpenRouter APIから画像データが返されませんでした。モデルが画像生成に対応していない可能性があります。"

    image_url = _extract_openrouter_image_url(images[0])
    if not image_url:
        return "【エラー】OpenRouter APIから画像データが返されませんでした。モデルが画像生成に対応していない可能性があります。"

    if not isinstance(image_url, str) or not image_url.startswith("data:image/") or ";base64," not in image_url:
        return "【エラー】OpenRouter APIから対応していない画像URL形式が返されました。"

    _prefix, b64_data = image_url.split(";base64,", 1)
    image_data = base64.b64decode(b64_data)
    image = Image.open(io.BytesIO(image_data))

    text_response = (message.get("content") or "").strip()
    if text_response:
        print(f"  [OpenRouter Image] APIからのテキスト応答: {text_response}")
    model_comment = f"\nAI Model Comment: {text_response}" if text_response else ""
    result = _save_generated_image(image, prompt, save_dir, room_name, model_comment=model_comment)
    if reference_urls:
        return result
    if captioned_reference:
        return _append_reference_caption_note(result, True)
    return _append_reference_unsupported_note(result, reference_image_paths)


def _openrouter_model_supports_reference(model_name: str) -> bool:
    model = str(model_name or "").strip().lower()
    return "gemini" in model and "image" in model


def _generate_with_openai(
    prompt: str,
    model_name: str,
    base_url: str,
    api_key: str,
    save_dir: str,
    room_name: str,
    reference_image_paths: Optional[List[str]] = None,
    api_key_name: str = None,
) -> str:
    """OpenAI互換API (Images API) で画像を生成する"""
    print(f"  [OpenAI Image] base_url={base_url}, model={model_name}")
    print(f"  [OpenAI Image] api_key set: {bool(api_key and len(api_key) > 5)}")

    if "openrouter.ai" in (base_url or "").lower():
        return _generate_with_openrouter(prompt, model_name, base_url, api_key, save_dir, room_name, reference_image_paths, api_key_name=api_key_name)

    from openai import OpenAI
    import requests

    client = OpenAI(base_url=base_url, api_key=api_key)
    
    # モデルによってサイズを調整
    size = "1024x1024"
    if "dall-e-3" in model_name:
        size = "1024x1024"  # DALL-E 3は1024x1024, 1792x1024, 1024x1792
    
    # gpt-image-1系モデルはresponse_formatをサポートしない（URLベースのみ）
    is_gpt_image = "gpt-image" in model_name.lower() or "gptimage" in model_name.lower()
    is_grok = "grok" in model_name.lower()
    print(f"  [OpenAI Image] is_gpt_image={is_gpt_image}, is_grok={is_grok}, size={size}")

    reference_files = []
    used_reference_images = False
    captioned_reference = False
    if reference_image_paths and is_gpt_image:
        reference_files = _open_reference_files(reference_image_paths)
    elif reference_image_paths:
        reference_texts = resolve_reference_texts(room_name, reference_image_paths, api_key_name=api_key_name)
        if reference_texts:
            prompt = (
                f"{prompt}\n\n[参考画像の外見]\n"
                + "\n".join(f"- {text}" for text in reference_texts)
            )
            captioned_reference = True
        reference_image_paths = None
    
    if is_gpt_image:
        # GPT Image モデル用（response_formatパラメータを渡さないが、b64_jsonで返る）
        print(f"  [OpenAI Image] Calling {'images.edit' if reference_files else 'images.generate'} (gpt-image mode, no response_format param)...")
        
        gen_params = {
            "model": model_name,
            "prompt": prompt,
            "n": 1,
        }
        if is_grok:
            # Grok は size をサポートせず aspect_ratio を使用する
            gen_params["extra_body"] = {"aspect_ratio": "1:1", "resolution": "1k"}
        else:
            gen_params["size"] = size

        try:
            if reference_files:
                response = client.images.edit(image=reference_files, **gen_params)
                used_reference_images = True
                print(f"  [OpenAI Image] images.edit に参照画像を {len(reference_files)} 件渡しました。")
            else:
                response = client.images.generate(**gen_params)
        except Exception as e:
            if reference_files:
                print(f"  [OpenAI Image] images.edit が失敗したため generate へフォールバックします: {e}")
                response = client.images.generate(**gen_params)
                used_reference_images = False
            else:
                raise
        finally:
            _close_reference_files(reference_files)
        print(f"  [OpenAI Image] Response received")
        
        # gpt-image-1は実際にはb64_jsonで返す（urlはNone）
        if response.data and response.data[0].b64_json:
            print(f"  [OpenAI Image] Found b64_json data, decoding...")
            image_data = base64.b64decode(response.data[0].b64_json)
            image = Image.open(io.BytesIO(image_data))
        elif response.data and response.data[0].url:
            # フォールバック: URLがある場合
            image_url = response.data[0].url
            print(f"  [OpenAI Image] Downloading from URL: {image_url[:100]}...")
            img_response = requests.get(image_url, timeout=60)
            img_response.raise_for_status()
            image = Image.open(io.BytesIO(img_response.content))
        else:
            print(f"  [OpenAI Image] ERROR: No image data in response")
            return "【エラー】APIから画像データが返されませんでした。"
        
        print(f"  [OpenAI Image] Image processed successfully")
    else:
        # DALL-E等（b64_json対応）
        print(f"  [OpenAI Image] Calling images.generate (b64_json mode)...")
        
        gen_params = {
            "model": model_name,
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json"
        }
        if is_grok:
            # Grok は size をサポートせず aspect_ratio を使用する
            gen_params["extra_body"] = {"aspect_ratio": "1:1", "resolution": "1k"}
        else:
            gen_params["size"] = size
            
        response = client.images.generate(**gen_params)
        print(f"  [OpenAI Image] Response received")
        
        if not response.data or not response.data[0].b64_json:
            print(f"  [OpenAI Image] ERROR: No b64_json in response.data")
            return "【エラー】APIから画像データが返されませんでした。"
        
        image_data = base64.b64decode(response.data[0].b64_json)
        image = Image.open(io.BytesIO(image_data))
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{room_name.lower()}_{timestamp}.png"
    save_path = os.path.join(save_dir, filename)
    
    image.save(save_path, "PNG")
    print(f"  - 画像を保存しました: {save_path}")

    revised_prompt = getattr(response.data[0], 'revised_prompt', None)
    model_comment = f"\nRevised Prompt: {revised_prompt}" if revised_prompt else ""
    result = f"[Generated Image: {save_path}]{model_comment}\n📝 Prompt: {prompt}\n画像生成完了。この画像についてコメントを添えてください。\n[VIEW_IMAGE: {save_path}]"
    if used_reference_images:
        return result
    result = _append_reference_caption_note(result, captioned_reference)
    return _append_reference_unsupported_note(result, reference_image_paths)


def _save_generated_image(image: Image.Image, prompt: str, save_dir: str, room_name: str, model_comment: str = "") -> str:
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{room_name.lower()}_{timestamp}.png"
    save_path = os.path.join(save_dir, filename)

    image.save(save_path, "PNG")
    print(f"  - 画像を保存しました: {save_path}")

    return f"[Generated Image: {save_path}]{model_comment}\n📝 Prompt: {prompt}\n画像生成完了。この画像についてコメントを添えてください。\n[VIEW_IMAGE: {save_path}]"


def _generate_with_pollinations(
    prompt: str,
    model_name: str,
    api_key: str,
    save_dir: str,
    room_name: str,
    reference_image_paths: Optional[List[str]] = None,
) -> str:
    """Pollinations.ai へ OpenAI SDK を使わず直接リクエストして画像を生成する。"""
    if not api_key:
        return "【エラー】Pollinations.ai のAPIキーが設定されていません。"

    endpoint = "https://gen.pollinations.ai/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "NexusArk/0.2.7 PollinationsDirect",
    }
    payload = {
        "model": model_name or "flux",
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }

    print(f"  [Pollinations Image] POST {endpoint}, model={payload['model']}")
    response = http_requests.post(endpoint, headers=headers, json=payload, timeout=180)

    if response.status_code in (401, 403) and "blocked" in (response.text or "").lower():
        print("  [Pollinations Image] POSTがブロックされたため、GET /image 経路へフォールバックします。")
        image_url = f"https://gen.pollinations.ai/image/{quote(prompt, safe='')}"
        get_response = http_requests.get(
            image_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "image/png,image/jpeg",
                "User-Agent": "NexusArk/0.2.7 PollinationsDirect",
            },
            params={"model": payload["model"], "width": 1024, "height": 1024, "key": api_key},
            timeout=180,
        )
        if get_response.status_code != 200:
            detail = (get_response.text or "")[:300]
            return f"【エラー】Pollinations.ai APIエラー (HTTP {get_response.status_code}): {detail}"
        content_type = get_response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            return f"【エラー】Pollinations.ai から画像以外のデータが返されました (Content-Type: {content_type})。"
        image = Image.open(io.BytesIO(get_response.content))
        result = _save_generated_image(image, prompt, save_dir, room_name)
        return _append_reference_unsupported_note(result, reference_image_paths)

    if response.status_code != 200:
        detail = (response.text or "")[:300]
        return f"【エラー】Pollinations.ai APIエラー (HTTP {response.status_code}): {detail}"

    try:
        data = response.json()
    except ValueError:
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("image/"):
            image = Image.open(io.BytesIO(response.content))
            result = _save_generated_image(image, prompt, save_dir, room_name)
            return _append_reference_unsupported_note(result, reference_image_paths)
        return "【エラー】Pollinations.ai APIからJSONでも画像でもない応答が返されました。"

    image_items = data.get("data") or []
    if not image_items:
        return "【エラー】Pollinations.ai APIから画像データが返されませんでした。"

    first_image = image_items[0]
    if first_image.get("b64_json"):
        image_data = base64.b64decode(first_image["b64_json"])
        image = Image.open(io.BytesIO(image_data))
    elif first_image.get("url"):
        image_response = http_requests.get(first_image["url"], timeout=120)
        image_response.raise_for_status()
        image = Image.open(io.BytesIO(image_response.content))
    else:
        return "【エラー】Pollinations.ai APIから画像URLまたはbase64データが返されませんでした。"

    revised_prompt = first_image.get("revised_prompt")
    model_comment = f"\nRevised Prompt: {revised_prompt}" if revised_prompt else ""
    result = _save_generated_image(image, prompt, save_dir, room_name, model_comment=model_comment)
    return _append_reference_unsupported_note(result, reference_image_paths)


def _generate_with_huggingface(
    prompt: str,
    model_id: str,
    hf_token: str,
    save_dir: str,
    room_name: str,
    reference_image_paths: Optional[List[str]] = None,
) -> str:
    """Hugging Face Inference API で画像を生成する"""
    api_url = f"https://router.huggingface.co/hf-inference/models/{model_id}"
    headers = {"Authorization": f"Bearer {hf_token}"}
    payload = {"inputs": prompt}

    print(f"  [HuggingFace Image] model={model_id}, prompt='{prompt[:80]}...'")

    response = http_requests.post(api_url, headers=headers, json=payload, timeout=120)

    if response.status_code == 503:
        # モデルがロード中の場合
        return "【エラー】Hugging Face のモデルが現在読み込み中です。数分後に再度お試しください。"
    if response.status_code == 401:
        return "【エラー】Hugging Face のAPIトークンが無効です。設定を確認してください。"
    if response.status_code == 429:
        return "【エラー】Hugging Face のレート制限に達しました。しばらく待ってから再度お試しください。"
    if response.status_code != 200:
        error_detail = response.text[:200] if response.text else "不明"
        return f"【エラー】Hugging Face APIエラー (HTTP {response.status_code}): {error_detail}"

    # レスポンスは画像バイナリ
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        return f"【エラー】Hugging Face APIから画像以外のデータが返されました (Content-Type: {content_type})。モデルがtext-to-imageタスクに対応しているか確認してください。"

    image = Image.open(io.BytesIO(response.content))
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{room_name.lower()}_{timestamp}.png"
    save_path = os.path.join(save_dir, filename)

    image.save(save_path, "PNG")
    print(f"  - 画像を保存しました: {save_path}")

    result = f"[Generated Image: {save_path}]\n📝 Prompt: {prompt}\n画像生成完了。この画像についてコメントを添えてください。\n[VIEW_IMAGE: {save_path}]"
    return _append_reference_unsupported_note(result, reference_image_paths)


@tool
def generate_image(
    prompt: str,
    room_name: str,
    api_key: str,
    api_key_name: str = None,
    reference_image_paths: Optional[List[str]] = None,
) -> str:
    """
    ユーザーの要望や会話の文脈に応じて、情景、キャラクター、アイテムなどのイラストを生成する。
    成功した場合は、UIに表示するための特別な画像タグを返す。
    prompt: 画像生成のための詳細な指示（英語が望ましい）。
    reference_image_paths: 自分やユーザーの姿を含む画像では、read_closet/read_user_closetで得た参照画像パスを渡すと、対応モデルでは外見が安定する。
    """
    return _generate_image_impl(prompt, room_name, api_key, api_key_name, reference_image_paths=reference_image_paths)

def _generate_image_impl(
    prompt: str, 
    room_name: str, 
    api_key: str, 
    api_key_name: str = None,
    provider: str = None,
    model_name: str = None,
    openai_profile_name: str = None,
    save_subdir: str = "generated_images",
    reference_image_paths: Optional[List[str]] = None,
) -> str:
    """generate_image の実体ロジック（他のツールからも呼び出し可能）"""
    # --- 最新の設定を読み込む ---
    latest_config = config_manager.load_config_file()

    # 引数で指定されていない場合は設定ファイルから取得
    if provider is None:
        provider = latest_config.get("image_generation_provider", "gemini")
    
    if model_name is None:
        model_name = latest_config.get("image_generation_model", "gemini-2.5-flash-image")

    # [2026-04-29] 画像生成設定で専用のAPIキーが指定されている場合、それを最優先する
    # (Google無料キーでは不可能なため、有料キーが設定されていればそちらを強制的に使う)
    image_gen_key_name = latest_config.get("image_generation_api_key_name")
    if provider == "gemini" and image_gen_key_name:
        configured_key = config_manager.GEMINI_API_KEYS.get(image_gen_key_name)
        if configured_key and not configured_key.startswith("YOUR_API_KEY"):
            api_key = configured_key
            api_key_name = image_gen_key_name
            print(f"  - 画像生成設定の専用キーを優先使用します: {api_key_name}")
    
    # api_key_name が未指定の場合は逆引きで特定
    if not api_key_name:
        api_key_name = config_manager.get_api_key_name_by_value(api_key)

    openai_settings = latest_config.get("image_generation_openai_settings", {})
    if openai_profile_name:
        # 明示的な指定がある場合はプロファイルを上書き
        openai_settings = openai_settings.copy()
        openai_settings["profile_name"] = openai_profile_name
        openai_settings["model"] = model_name

    # プロバイダが無効の場合（ツール経由のみチェック）
    if provider == "disabled":
        return "【エラー】画像生成機能は現在、設定で無効化されています。"

    if not room_name:
        return "【エラー】画像生成にはルーム名が必須です。"

    # ログ表示用の実際のモデル名を特定
    actual_model_name = model_name
    if provider == "openai":
        actual_model_name = openai_settings.get("model", model_name)
    elif provider == "pollinations":
        # 明示的な指定がない場合は設定値を使用
        if not model_name or model_name == latest_config.get("image_generation_model"):
            actual_model_name = latest_config.get("image_generation_pollinations_model", "flux")
    elif provider == "huggingface":
        if not model_name or model_name == latest_config.get("image_generation_model"):
            actual_model_name = latest_config.get("image_generation_huggingface_model", "black-forest-labs/FLUX.1-schnell")

    reference_captioned = False
    normalized_reference_paths = _normalize_reference_image_paths(reference_image_paths)
    if normalized_reference_paths and provider != "openai" and not image_model_supports_reference(provider, actual_model_name):
        reference_texts = resolve_reference_texts(room_name, normalized_reference_paths, api_key_name=api_key_name)
        if reference_texts:
            prompt = (
                f"{prompt}\n\n[参考画像の外見]\n"
                + "\n".join(f"- {text}" for text in reference_texts)
            )
            reference_captioned = True
            print(f"  - [Image Reference] 参照画像をキャプションとしてプロンプトへ反映しました: {len(reference_texts)} 件")
        reference_image_paths = None

    print(f"--- 画像生成ツール実行 (Provider: {provider}, Model: {actual_model_name}, Key: {api_key_name}, Prompt: '{prompt[:100]}...') ---")

    try:
        save_dir = os.path.join("characters", room_name, save_subdir)
        os.makedirs(save_dir, exist_ok=True)

        if provider == "gemini":
            # Gemini用のAPIキーを使用
            if not api_key:
                return "【エラー】Gemini画像生成にはAPIキーが必須です。"
            result = _generate_with_gemini(
                prompt,
                actual_model_name,
                api_key,
                save_dir,
                room_name,
                api_key_name=api_key_name,
                reference_image_paths=reference_image_paths,
            )
            result = _append_reference_caption_note(result, reference_captioned)
        
        elif provider == "openai":
            # OpenAI互換設定を取得（プロファイル名から設定を参照）
            profile_name = openai_settings.get("profile_name", "")
            openai_model = openai_settings.get("model", model_name)
            
            # プロファイルからBase URLとAPIキーを取得
            openai_provider_settings = latest_config.get("openai_provider_settings", [])
            target_profile = None
            for profile in openai_provider_settings:
                if profile.get("name") == profile_name:
                    target_profile = profile
                    break
            
            if not target_profile:
                return f"【エラー】画像生成用のOpenAI互換プロファイル '{profile_name}' が見つかりません。「共通設定」→「画像生成設定」でプロファイルを設定してください。"
            
            openai_base_url = target_profile.get("base_url", "https://api.openai.com/v1")
            openai_api_key = target_profile.get("api_key", "")
            
            # Pollinations.ai の場合、プロファイルにキーがなければグローバル設定のキーをフォールバックとして試す
            if "pollinations.ai" in openai_base_url.lower() and (not openai_api_key or "YOUR_API_KEY" in openai_api_key):
                poll_api_key = latest_config.get("pollinations_api_key", "")
                if poll_api_key and "YOUR_API_KEY" not in poll_api_key:
                    openai_api_key = poll_api_key
                    print(f"  - OpenAIプロファイルのキーが未設定のため、共通設定のPollinationsキーを使用します。")

            if not openai_api_key or "YOUR_API_KEY" in openai_api_key:
                return f"【エラー】プロファイル '{profile_name}' にAPIキーが設定されていません。「APIキー / Webhook管理」でAPIキーを設定してください。"

            if "pollinations.ai" in openai_base_url.lower():
                return "【エラー】Pollinations.ai は画像生成の専用プロバイダとして利用してください。プロバイダを「Pollinations.ai」に切り替えてください。"
            
            result = _generate_with_openai(
                prompt,
                openai_model,
                openai_base_url,
                openai_api_key,
                save_dir,
                room_name,
                reference_image_paths=reference_image_paths,
                api_key_name=api_key_name,
            )
            result = _append_reference_caption_note(result, reference_captioned)
        
        elif provider == "pollinations":
            # Pollinations.ai は OpenAI 互換 API
            poll_api_key = latest_config.get("pollinations_api_key", "")
            poll_model = latest_config.get("image_generation_pollinations_model", "flux")
            if not poll_api_key:
                return "【エラー】Pollinations.ai のAPIキーが設定されていません。「共通設定」→「画像生成設定」でAPIキーを入力してください。\nAPIキーは https://enter.pollinations.ai で取得できます。"
            result = _generate_with_pollinations(
                prompt,
                poll_model,
                poll_api_key,
                save_dir,
                room_name,
                reference_image_paths=reference_image_paths,
            )
            result = _append_reference_caption_note(result, reference_captioned)
        
        elif provider == "huggingface":
            # Hugging Face Inference API
            hf_token = latest_config.get("huggingface_api_token", "")
            hf_model = latest_config.get("image_generation_huggingface_model", "black-forest-labs/FLUX.1-schnell")
            if not hf_token:
                return "【エラー】Hugging Face のAPIトークンが設定されていません。「共通設定」→「画像生成設定」でトークンを入力してください。\nトークンは https://huggingface.co/settings/tokens で取得できます。"
            result = _generate_with_huggingface(
                prompt,
                hf_model,
                hf_token,
                save_dir,
                room_name,
                reference_image_paths=reference_image_paths,
            )
            result = _append_reference_caption_note(result, reference_captioned)
        
        else:
            return f"【エラー】不明な画像生成プロバイダ: {provider}"

        # 全画像生成経路が合流する成功点で1回だけ記録する。
        if "[Generated Image:" in str(result or ""):
            usage_ledger.record_image_generation(
                provider,
                actual_model_name,
                image_count=1,
                api_key_name=api_key_name or "",
            )
        return result

    except httpx.RemoteProtocolError as e:
        print(f"  - 画像生成ツールでサーバー切断エラー: {e}")
        return "【エラー】サーバーが応答せずに接続を切断しました。プロンプトを簡潔にして、もう一度試してみてください。"
    except genai.errors.ServerError as e:
        print(f"  - 画像生成ツールでサーバーエラー(500番台): {e}")
        return "【エラー】サーバー側で内部エラー(500)が発生しました。プロンプトをよりシンプルにして、もう一度試してみてください。"
    except genai.errors.ClientError as e:
        print(f"  - 画像生成ツールでクライアントエラー(400番台): {e}")
        return f"【エラー】APIリクエストが無効です(400番台)。詳細: {e}"
    except Exception as e:
        print(f"  - 画像生成ツールで予期せぬエラー: {e}")
        traceback.print_exc()
        return f"【エラー】画像生成中に予期せぬ問題が発生しました。詳細: {e}"

def generate_image_caption(image_path: str, api_key_name: str = None) -> str:
    """画像のキャプション（テキスト説明）を生成する"""
    import google.genai as genai
    from PIL import Image
    import config_manager
    
    try:
        # Load config to get API key if not provided
        if not api_key_name:
            latest_config = config_manager.load_config_file()
            # fallback to global setting if no key provided
            api_key_name = latest_config.get("global_google_api_key_name")
            
        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
        if not api_key or api_key.startswith("YOUR_API_KEY"):
            return "（キャプション生成エラー: 有効なAPIキーがありません）"
            
        client = genai.Client(api_key=api_key)
        
        # Use a fast multimodal model for captioning
        model_name = "gemini-2.5-flash"
        
        image = Image.open(image_path)
        
        prompt = "この画像の内容を、要点に絞って事実ベースで簡潔に説明してください。各項目は1〜2文程度で記述してください：\n1. 被写体と状態（何が、どのような様子で写っているか）\n2. 背景・シチュエーション（場所や状況、ブランド等）\n3. 主要な特徴（色、形、目立つディテール）"
        
        response = client.models.generate_content(
            model=model_name,
            contents=[image, prompt],
        )
        
        if response.text:
            return response.text.strip()
        else:
            return "（画像のキャプションを生成できませんでした）"
            
    except Exception as e:
        print(f"--- [画像キャプション生成エラー] {e} ---")
        return f"（画像キャプション生成エラー: {str(e)}）"

@tool
def view_past_image(image_path: str, room_name: str = "") -> str:
    """
    過去の画像（イラストや写真）の詳細な内容を思い出すために、指定されたパスの画像を視覚メモリにロードします。
    引数 image_path には、過去の記憶などにある [VIEW_IMAGE: path/to/image.png] などのタグから抽出したファイルパスを指定します。
    ファイルパスが不明な場合は、ファイル名のみ（例: roblox_screen_...）を指定しても構いません。
    【重要】画像パスを read_project_file や read_url_tool で読み込んではいけません（文字化けします）。必ずこの view_past_image ツールを使用してください。
    """
    import os
    
    # パスが直接存在する場合
    if os.path.exists(image_path):
        target_path = image_path
    else:
        # 見つからない場合、ルーム固有のディレクトリを検索する
        found_path = None
        if room_name:
            search_dirs = [
                os.path.join("characters", room_name, "closet", "images"),
                os.path.join("characters", room_name, "images", "roblox_screenshots"),
                os.path.join("characters", room_name, "generated_images"),
                os.path.join("characters", room_name, "images")
            ]
            filename = os.path.basename(image_path)
            # AIが拡張子を忘れたり、末尾に「...」をつけたりする場合のサニタイズ
            filename = filename.split("...")[0].strip()
            if not filename.endswith(".png") and not filename.endswith(".jpg"):
                filename += ".png" # デフォルト

            for d in search_dirs:
                potential_path = os.path.join(d, filename)
                if os.path.exists(potential_path):
                    found_path = potential_path
                    break
        
        if found_path:
            target_path = found_path
        else:
            return f"【エラー】指定された画像パスが見つかりません: {image_path} (検索したディレクトリ: characters/{room_name}/...)"

    # この特別なタグを返すことで、メインのトークルーチン（gemini_api.py）が検知し
    # 次のAPIコールの際に実際の画像をマルチモーダル入力として付与する仕組み
    return f"[VIEW_IMAGE: {target_path}]\n※システムメッセージ: 画像が視覚野にロードされました。"
