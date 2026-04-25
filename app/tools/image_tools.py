# tools/image_tools.py

import os
import io
import base64
import datetime
import traceback
import requests as http_requests
from PIL import Image
import google.genai as genai
import httpx
from langchain_core.tools import tool
from google.genai import types
import config_manager 


def _generate_with_gemini(prompt: str, model_name: str, api_key: str, save_dir: str, room_name: str, api_key_name: str = "Unknown") -> str:
    """Gemini (google.genai) で画像を生成する"""
    client = genai.Client(api_key=api_key)
    
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
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
    return f"[Generated Image: {save_path}]{model_comment}\n📝 Prompt: {prompt}\n画像生成完了。この画像についてコメントを添えてください。\n[VIEW_IMAGE: {save_path}]"


def _generate_with_huggingface(prompt: str, model_id: str, hf_token: str, save_dir: str, room_name: str) -> str:
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

    return f"[Generated Image: {save_path}]\n📝 Prompt: {prompt}\n画像生成完了。この画像についてコメントを添えてください。\n[VIEW_IMAGE: {save_path}]"


@tool
def generate_image(prompt: str, room_name: str, api_key: str, api_key_name: str = None) -> str:
    """
    ユーザーの要望や会話の文脈に応じて、情景、キャラクター、アイテムなどのイラストを生成する。
    成功した場合は、UIに表示するための特別な画像タグを返す。
    prompt: 画像生成のための詳細な指示（英語が望ましい）。
    """
    return _generate_image_impl(prompt, room_name, api_key, api_key_name)

def _generate_image_impl(prompt: str, room_name: str, api_key: str, api_key_name: str = None) -> str:
    """generate_image の実体ロジック（他のツールからも呼び出し可能）"""
    # --- 最新の設定を読み込む ---
    latest_config = config_manager.load_config_file()

    # 画像生成設定で指定されたAPIキーがあれば、それを最優先する（通常は有料キー）
    image_gen_key_name = latest_config.get("image_generation_api_key_name")
    if image_gen_key_name:
        configured_key = config_manager.GEMINI_API_KEYS.get(image_gen_key_name)
        if configured_key and not configured_key.startswith("YOUR_API_KEY"):
            api_key = configured_key
            api_key_name = image_gen_key_name
            print(f"  - 画像生成設定の指定キーを使用します: {api_key_name}")
    
    # api_key_name が未指定の場合は逆引きで特定
    if not api_key_name:
        api_key_name = config_manager.get_api_key_name_by_value(api_key)

    provider = latest_config.get("image_generation_provider", "gemini")
    model_name = latest_config.get("image_generation_model", "gemini-2.5-flash-image")
    openai_settings = latest_config.get("image_generation_openai_settings", {})

    # プロバイダが無効の場合
    if provider == "disabled":
        return "【エラー】画像生成機能は現在、設定で無効化されています。"

    if not room_name:
        return "【エラー】画像生成にはルーム名が必須です。"

    # ログ表示用の実際のモデル名を特定
    actual_model_name = model_name
    if provider == "openai":
        actual_model_name = openai_settings.get("model", model_name)
    elif provider == "pollinations":
        actual_model_name = latest_config.get("image_generation_pollinations_model", "flux")
    elif provider == "huggingface":
        actual_model_name = latest_config.get("image_generation_huggingface_model", "black-forest-labs/FLUX.1-schnell")

    print(f"--- 画像生成ツール実行 (Provider: {provider}, Model: {actual_model_name}, Key: {api_key_name}, Prompt: '{prompt[:100]}...') ---")

    try:
        save_dir = os.path.join("characters", room_name, "generated_images")
        os.makedirs(save_dir, exist_ok=True)

        if provider == "gemini":
            # Gemini用のAPIキーを使用
            if not api_key:
                return "【エラー】Gemini画像生成にはAPIキーが必須です。"
            return _generate_with_gemini(prompt, model_name, api_key, save_dir, room_name, api_key_name=api_key_name)
        
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
        
        elif provider == "pollinations":
            # Pollinations.ai は OpenAI 互換 API
            poll_api_key = latest_config.get("pollinations_api_key", "")
            poll_model = latest_config.get("image_generation_pollinations_model", "flux")
            if not poll_api_key:
                return "【エラー】Pollinations.ai のAPIキーが設定されていません。「共通設定」→「画像生成設定」でAPIキーを入力してください。\nAPIキーは https://enter.pollinations.ai で取得できます。"
            return _generate_with_openai(prompt, poll_model, "https://gen.pollinations.ai/v1", poll_api_key, save_dir, room_name)
        
        elif provider == "huggingface":
            # Hugging Face Inference API
            hf_token = latest_config.get("huggingface_api_token", "")
            hf_model = latest_config.get("image_generation_huggingface_model", "black-forest-labs/FLUX.1-schnell")
            if not hf_token:
                return "【エラー】Hugging Face のAPIトークンが設定されていません。「共通設定」→「画像生成設定」でトークンを入力してください。\nトークンは https://huggingface.co/settings/tokens で取得できます。"
            return _generate_with_huggingface(prompt, hf_model, hf_token, save_dir, room_name)
        
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