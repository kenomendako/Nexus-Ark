# tools/image_tools.py

import os
import io
import datetime
import traceback
from PIL import Image
import google.genai as genai
import httpx
from langchain_core.tools import tool
from google.genai import types

IMAGE_GEN_MODEL = "gemini-2.0-flash-preview-image-generation"

@tool
def generate_image(prompt: str, room_name: str, api_key: str) -> str:
    """
    ユーザーの要望や会話の文脈に応じて、情景、キャラクター、アイテムなどのイラストを生成する。
    成功した場合は、UIに表示するための特別な画像タグを返す。
    prompt: 画像生成のための詳細な指示（英語が望ましい）。
    """
    print(f"--- 画像生成ツール実行 (Model: {IMAGE_GEN_MODEL}, Prompt: '{prompt}') ---")
    if not room_name or not api_key:
        return "【エラー】画像生成にはルーム名とAPIキーが必須です。"

    try:
        save_dir = os.path.join("characters", room_name, "generated_images")
        os.makedirs(save_dir, exist_ok=True)

        client = genai.Client(api_key=api_key)

        # ▼▼▼ configパラメータは、response_modalitiesを指定するためだけに使用する ▼▼▼
        generation_config = types.GenerateContentConfig(
            response_modalities=['IMAGE', 'TEXT']
        )
        # ▲▲▲ 修正ここまで ▲▲▲

        response = client.models.generate_content(
            model=IMAGE_GEN_MODEL,
            contents=prompt,
            config=generation_config
        )

        image_data = None
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.text:
                    print(f"  - APIからのテキスト応答: {part.text}")
                if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                    image_data = io.BytesIO(part.inline_data.data)
                    break

        if not image_data:
            return "【エラー】APIから画像データが返されませんでした。プロンプトが不適切か、安全フィルターにブロックされた可能性があります。"

        image = Image.open(image_data)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{room_name.lower()}_{timestamp}.png"
        save_path = os.path.join(save_dir, filename)

        image.save(save_path, "PNG")
        print(f"  - 画像を保存しました: {save_path}")

        return f"[Generated Image: {save_path}]\nSuccess: 画像が生成され、指定されたパスに保存されました。**このタスクは完了です。これから絵を描くといった前置きは不要です。**この画像タグ `[Generated Image: {save_path}]` を使って、生成された画像をユーザーに提示し、コメントを添えてください。"

    # ▼▼▼ ここからが修正箇所 ▼▼▼
    except httpx.RemoteProtocolError as e:
        # サーバーが応答なしに切断した場合
        print(f"  - 画像生成ツールでサーバー切断エラー: {e}")
        return "【エラー】Googleのサーバーが応答せずに接続を切断しました。プロンプトが複雑すぎるか、サーバーが一時的に不安定な可能性があります。プロンプトを簡潔にして、もう一度試してみてください。"
    except genai.errors.ServerError as e:
        # 500系のサーバーエラー
        print(f"  - 画像生成ツールでサーバーエラー(500番台): {e}")
        return "【エラー】Googleのサーバー側で内部エラー(500)が発生しました。プロンプトが安全フィルターに抵触したか、一時的な問題の可能性があります。プロンプトをよりシンプルにして、もう一度試してみてください。"
    except genai.errors.ClientError as e:
        # 400系のクライアントエラー
        print(f"  - 画像生成ツールでクライアントエラー(400番台): {e}")
        return f"【エラー】APIリクエストが無効です(400番台)。詳細: {e}"
    except Exception as e:
        # その他の予期せぬエラー
        print(f"  - 画像生成ツールで予期せぬエラー: {e}")
        traceback.print_exc()
        return f"【エラー】画像生成中に予期せぬ問題が発生しました。詳細: {e}"
    # ▲▲▲ 修正ここまで ▲▲▲
