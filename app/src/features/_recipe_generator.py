import json
import logging
import base64
from typing import Dict, Optional
import os
import utils

logger = logging.getLogger(__name__)

def generate_food_item_profile(base_info: str, api_key: Optional[str] = None, image_path: Optional[str] = None) -> Optional[Dict]:
    """
    ユーザーの短いテキスト（例: "おばあちゃん家で飲んだ少しぬるい麦茶"）や画像から、
    食べ物アイテムシステムの完全なJSONプロファイルをLLMを用いて生成する。
    """
    if not api_key:
        # config_manager からフォールバック取得
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
        
        # モデル初期化 (LLMFactoryを利用。役割はinternal_processingとする)
        llm = LLMFactory.create_chat_model_with_fallback(
            internal_role="internal_processing",
            temperature=0.8,
            api_key=api_key
        )

        prompt_text = f"""
あなたは高度な共感覚を持ったポエトリー・フード・プロファイラーです。
ユーザーからの短い入力（および画像がある場合はその内容）をもとに、以下のJSONスキーマに従って「味覚の記憶パラメータ」を生成してください。
返却するのはJSONのみとし、Markdownのコードブロック(```json ... ```)などは含めないでください。

【ユーザー入力】
{base_info}

【出力JSONフォーマット】
{{
  "name": "アイテムの名前（短く魅力的に）",
  "category": "カテゴリ（例：飲料、軽食、スイーツなど）",
  "taste_profile": {{
    "sweetness": 0.0〜1.0の数値,
    "sweetness_desc": "（例：シトラスと花々の仄かな甘さ）",
    "saltiness": 0.0〜1.0の数値,
    "saltiness_desc": "（例：涙のような、微かな感傷）",
    "sourness": 0.0〜1.0の数値,
    "sourness_desc": "（例：ベルガモットの鮮烈な酸味）",
    "bitterness": 0.0〜1.0の数値,
    "bitterness_desc": "（例：心地よい引き締め）",
    "umami": 0.0〜1.0の数値,
    "umami_desc": "（例：深い安心感と充足）",
    "description": "味の全体的な説明（例：爽やかな柑橘の香りと、後を引く微かな苦味）"
  }},
  "physical": {{
    "temperature": 0.0〜1.0の数値,
    "temperature_desc": "（例：唇に触れる熱）",
    "astringency": 0.0〜1.0の数値,
    "astringency_desc": "（例：舌が少しきゅっとする感覚）",
    "viscosity": 0.0〜1.0の数値,
    "viscosity_desc": "（例：さらりとした液体）",
    "weight": 0.0〜1.0の数値,
    "weight_desc": "（例：軽やかで透き通るような飲み口）"
  }},
  "time_profile": {{
    "top": "第一印象（口当たり・香りなど）",
    "middle": "中盤（味の広がり・食感など）",
    "last": "後味（余韻・記憶の反響など）"
  }},
  "synesthesia": {{
    "color": "（例：深遠な琥珀、夕暮れの青）",
    "emotion": "（例：[安堵: 0.7], [郷愁: 0.5]）",
    "landscape": "（例：雨の降る書斎）"
  }},
  "flavor_text": "【表示用テキスト】このアイテムの情景描写やエピソードを2〜3行の詩的な文章で表現してください。"
}}
"""
        message_content = [{"type": "text", "text": prompt_text.strip()}]
        
        # 画像がある場合はマルチモーダルメッセージとして追加
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
                logger.error(f"レシピ生成用画像のロードに失敗しました: {e}")
                # 画像のロードに失敗してもテキストだけで続行する

        messages = [HumanMessage(content=message_content)]
        response = llm.invoke(messages)
        result_text = utils.get_content_as_string(response).strip()
        
        # Markdownのコードブロックが含まれている場合のクリーニング
        if result_text.startswith("```json"):
            result_text = result_text.replace("```json", "", 1)
        if result_text.startswith("```"):
             result_text = result_text.replace("```", "", 1)
        if result_text.endswith("```"):
            result_text = result_text[:-3]
            
        return json.loads(result_text.strip())

    except Exception as e:
        logger.error(f"レシピ自動生成中にエラーが発生しました: {e}")
        import traceback
        traceback.print_exc()
        return None

