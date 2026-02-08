# tools/image_tools.py

import os
import io
import base64
import datetime
import traceback
from PIL import Image
import google.genai as genai
import httpx
from langchain_core.tools import tool
from google.genai import types
import config_manager 


def _generate_with_gemini(prompt: str, model_name: str, api_key: str, save_dir: str, room_name: str) -> str:
    """Gemini (google.genai) で画像を生成する"""
    client = genai.Client(api_key=api_key)
    
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
    )

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
    return f"[Generated Image: {save_path}]{model_comment}\n📝 Prompt: {prompt}\n画像生成完了。この画像についてコメントを添えてください。"


def _generate_with_openai(prompt: str, model_name: str, base_url: str, api_key: str, save_dir: str, room_name: str) -> str:
    """OpenAI互換API (Images API) で画像を生成する"""
    from openai import OpenAI
    import requests
    
    print(f"  [OpenAI Image] base_url={base_url}, model={model_name}")
    print(f"  [OpenAI Image] api_key set: {bool(api_key and len(api_key) > 5)}")
    
    client = OpenAI(base_url=base_url, api_key=api_key)
    
    # モデルによってサイズを調整
    size = "1024x1024"
    if "dall-e-3" in model_name:
        size = "1024x1024"  # DALL-E 3は1024x1024, 1792x1024, 1024x1792
    
    # gpt-image-1系モデルはresponse_formatをサポートしない（URLベースのみ）
    is_gpt_image = "gpt-image" in model_name.lower()
    print(f"  [OpenAI Image] is_gpt_image={is_gpt_image}, size={size}")
    
    if is_gpt_image:
        # GPT Image モデル用（response_formatパラメータを渡さないが、b64_jsonで返る）
        print(f"  [OpenAI Image] Calling images.generate (gpt-image mode, no response_format param)...")
        response = client.images.generate(
            model=model_name,
            prompt=prompt,
            n=1,
            size=size
        )
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
        response = client.images.generate(
            model=model_name,
            prompt=prompt,
            n=1,
            size=size,
            response_format="b64_json"
        )
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
    return f"[Generated Image: {save_path}]{model_comment}\n📝 Prompt: {prompt}\n画像生成完了。この画像についてコメントを添えてください。"


@tool
def generate_image(prompt: str, room_name: str, api_key: str, api_key_name: str = None) -> str:
    """
    ユーザーの要望や会話の文脈に応じて、情景、キャラクター、アイテムなどのイラストを生成する。
    成功した場合は、UIに表示するための特別な画像タグを返す。
    prompt: 画像生成のための詳細な指示（英語が望ましい）。
    """
    # --- 最新の設定を読み込む ---
    latest_config = config_manager.load_config_file()
    provider = latest_config.get("image_generation_provider", "gemini")
    model_name = latest_config.get("image_generation_model", "gemini-2.5-flash-image")
    openai_settings = latest_config.get("image_generation_openai_settings", {})

    # プロバイダが無効の場合
    if provider == "disabled":
        return "【エラー】画像生成機能は現在、設定で無効化されています。"

    if not room_name:
        return "【エラー】画像生成にはルーム名が必須です。"

    print(f"--- 画像生成ツール実行 (Provider: {provider}, Model: {model_name}, Prompt: '{prompt[:100]}...') ---")

    try:
        save_dir = os.path.join("characters", room_name, "generated_images")
        os.makedirs(save_dir, exist_ok=True)

        if provider == "gemini":
            # Gemini用のAPIキーを使用（ツール引数として渡されたもの）
            if not api_key:
                return "【エラー】Gemini画像生成にはAPIキーが必須です。"
            return _generate_with_gemini(prompt, model_name, api_key, save_dir, room_name)
        
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
            
            if not openai_api_key:
                return f"【エラー】プロファイル '{profile_name}' にAPIキーが設定されていません。「APIキー / Webhook管理」でAPIキーを設定してください。"
            
            return _generate_with_openai(prompt, openai_model, openai_base_url, openai_api_key, save_dir, room_name)
        
        else:
            return f"【エラー】不明な画像生成プロバイダ: {provider}"

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