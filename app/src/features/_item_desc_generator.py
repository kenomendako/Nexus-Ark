import json
import logging
import base64
from typing import Dict, Optional
import os
import utils

logger = logging.getLogger(__name__)

def generate_standard_item_profile(base_info: str, api_key: Optional[str] = None, image_path: Optional[str] = None) -> Optional[Dict]:
    """
    ユーザーの短いテキスト（例: "アンティークな懐中時計"）や画像から、
    通常アイテム（非食べ物）の詳細情報をLLMを用いて生成する。
    """
    if not api_key:
        import config_manager
        key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name") or config_manager.initial_api_key_name_global
        if key_name:
            api_key = config_manager.GEMINI_API_KEYS.get(key_name)
        
    if not api_key:
        logger.error("APIキーが設定されていないため、アイテムの自動生成に失敗しました。")
        return None

    try:
        from llm_factory import LLMFactory
        from langchain_core.messages import HumanMessage
        
        llm = LLMFactory.create_chat_model_with_fallback(
            internal_role="internal_processing",
            temperature=0.8,
            api_key=api_key
        )

        prompt_text = f"""
あなたは卓越した審美眼を持つ、骨董鑑定家兼ストーリーテラーです。
ユーザーからの短い入力（および画像がある場合はその内容）をもとに、以下のJSONスキーマに従ってアイテムの「外見と質感のプロファイル」を生成してください。
返却するのはJSONのみとし、Markdownのコードブロック(```json ... ```)などは含めないでください。

【ユーザー入力】
{base_info}

【出力JSONフォーマット】
{{
  "name": "アイテムの名前（短く魅力的に）",
  "category": "カテゴリ（例：アクセサリー、服飾、雑貨、容器、食器、家具、道具、その他）",
  "appearance": {{
    "description": "外見の全体的な説明（色、形状、デザインの細部などを1-2文で）",
    "color": "基調となる色や光の反射（例：鈍く光る銀、深みのある藍）",
    "design_detail": "特筆すべき意匠や装飾（例：アカンサスの葉を模した微細な彫刻）"
  }},
  "physical": {{
    "texture": "触れた時の感触（例：滑らかで冷やりとした金属の肌、使い込まれた革の柔らかさ）",
    "weight": "重さの印象（例：見た目に反してずっしりと重い、羽のように軽やか）",
    "temperature": "感じられる温度感（例：熱を帯びたような温もり、芯まで冷えるような冷たさ）"
  }},
  "flavor_text": "【表示用テキスト】このアイテムの背景にある物語やエピソードを2〜3行の詩的な文章で表現してください。"
}}
"""
        message_content = [{"type": "text", "text": prompt_text.strip()}]
        
        if image_path and os.path.exists(image_path):
            from PIL import Image
            import io
            try:
                with Image.open(image_path) as img:
                    if img.mode != 'RGB' and img.mode != 'RGBA':
                        img = img.convert('RGBA')
                    
                    resized_img = utils.resize_image_for_api(img, max_size=768, return_image=True)
                    if not resized_img:
                        resized_img = img
                        
                    img_byte_arr = io.BytesIO()
                    resized_img.save(img_byte_arr, format='PNG', optimize=True)
                    base64_encoded = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
                    
                    message_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{base64_encoded}"}
                    })
            except Exception as e:
                logger.error(f"アイテム生成用画像のロードに失敗しました: {e}")

        messages = [HumanMessage(content=message_content)]
        response = llm.invoke(messages)
        result_text = utils.get_content_as_string(response).strip()
        
        if result_text.startswith("```json"):
            result_text = result_text.replace("```json", "", 1)
        if result_text.startswith("```"):
             result_text = result_text.replace("```", "", 1)
        if result_text.endswith("```"):
            result_text = result_text[:-3]
            
        return json.loads(result_text.strip())

    except Exception as e:
        logger.error(f"アイテム自動生成中にエラーが発生しました: {e}")
        return None
