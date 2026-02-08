import os
import traceback
from typing import Tuple, Optional
from google.api_core import exceptions as google_exceptions

import constants
import utils
import config_manager
from llm_factory import LLMFactory
from room_manager import get_world_settings_path

def generate_scenery_context(
    room_name: str, 
    api_key: str, 
    force_regenerate: bool = False, 
    season_en: 'Optional[str]' = None, 
    time_of_day_en: 'Optional[str]' = None
) -> Tuple[str, str, str]:
    scenery_text = "（現在の場所の情景描写は、取得できませんでした）"
    space_def = "（現在の場所の定義・設定は、取得できませんでした）"
    location_display_name = "（不明な場所）"
    try:
        current_location_name = utils.get_current_location(room_name)
        if not current_location_name:
            current_location_name = "リビング"
            location_display_name = "リビング"

        world_settings_path = get_world_settings_path(room_name)
        world_data = utils.parse_world_file(world_settings_path)
        found_location = False
        for area, places in world_data.items():
            if current_location_name in places:
                space_def = places[current_location_name]
                location_display_name = f"[{area}] {current_location_name}"
                found_location = True
                break
        if not found_location:
            space_def = f"（場所「{current_location_name}」の定義が見つかりません）"

        from utils import get_season, get_time_of_day, load_scenery_cache, save_scenery_cache
        import hashlib
        import datetime

        now = datetime.datetime.now()
        effective_season = season_en or get_season(now.month)
        effective_time_of_day = time_of_day_en or get_time_of_day(now.hour)

        content_hash = hashlib.md5(space_def.encode('utf-8')).hexdigest()[:8]
        cache_key = f"{current_location_name}_{content_hash}_{effective_season}_{effective_time_of_day}"

        if not force_regenerate:
            scenery_cache = load_scenery_cache(room_name)
            if cache_key in scenery_cache:
                cached_data = scenery_cache[cache_key]
                print(f"--- [有効な情景キャッシュを発見] ({cache_key})。APIコールをスキップします ---")
                return location_display_name, space_def, cached_data["scenery_text"]

        if not space_def.startswith("（"):
            max_retries = 3
            tried_keys = set()
            current_api_key = api_key
            
            for attempt in range(max_retries):
                try:
                    effective_settings = config_manager.get_effective_settings(room_name)
                    # 【マルチモデル対応】内部処理はGemini固定のため force_google=True
                    llm_flash = LLMFactory.create_chat_model(
                        model_name=constants.INTERNAL_PROCESSING_MODEL,
                        api_key=current_api_key,
                        generation_config=effective_settings,
                        force_google=True
                    )

                    season_map_en_to_ja = {"spring": "春", "summer": "夏", "autumn": "秋", "winter": "冬"}
                    season_ja = season_map_en_to_ja.get(effective_season, "不明な季節")
                    
                    time_map_en_to_ja = {
                        "early_morning": "早朝", "morning": "朝", "late_morning": "昼前",
                        "afternoon": "昼下がり", "evening": "夕方", "night": "夜", "midnight": "深夜"
                    }
                    time_of_day_ja = time_map_en_to_ja.get(effective_time_of_day, "不明な時間帯")

                    scenery_prompt = (
                        "あなたは、与えられた二つの情報源から、一つのまとまった情景を描き出す、情景描写の専門家です。\n\n"
                        f"【情報源1：適用すべき時間・季節】\n- 時間帯: {time_of_day_ja}\n- 季節: {season_ja}\n\n"
                        f"【情報源2：この空間が持つ固有の設定】\n---\n{space_def}\n---\n\n"
                        "【あなたのタスク】\n"
                        "まず、心の中で【情報源1】と【情報源2】を比較し、矛盾があるかないかを判断してください。\n"
                        "その判断に基づき、**最終的な情景描写の文章のみを、2〜3文で生成してください。**\n\n"
                        "  - **矛盾がある場合** (例: 現現実が昼なのに、空間は常に夜の設定など):\n"
                        "    その**『にも関わらず』**という感覚や、その空間だけが持つ**不思議な空気感**に焦点を当てて描写してください。\n\n"
                        "  - **矛盾がない場合**:\n"
                        "    二つの情報を自然に**統合・融合**させ、その場のリアルな雰囲気をそのまま描写してください。\n\n"
                        "【厳守すべきルール】\n"
                        "- **あなたの思考過程や判断理由は、絶対に出力に含めないでください。**\n"
                        "- 具体的な時刻（例：「23時42分」）は文章に含めないでください。\n"
                        "- 人物やキャラクターの描写は絶対に含めないでください。\n"
                        "- 五感に訴えかける、**空気感まで伝わるような**精緻で写実的な描写を重視してください。"
                    )
                    scenery_text = llm_flash.invoke(scenery_prompt).content
                    save_scenery_cache(room_name, cache_key, location_display_name, scenery_text)
                    # 成功したらループを抜ける
                    break

                except Exception as e:
                    err_str = str(e).upper()
                    if isinstance(e, google_exceptions.ResourceExhausted) or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        # APIキー名を特定して枯渇マーク
                        key_name = config_manager.get_key_name_by_value(current_api_key)
                        if key_name != "Unknown":
                            config_manager.mark_key_as_exhausted(key_name)
                            tried_keys.add(key_name)
                            print(f"  - [Scenery Rotation] Key '{key_name}' marked as exhausted.")
                        
                        if attempt < max_retries - 1:
                            # 新しいキーを取得
                            new_key = config_manager.get_active_gemini_api_key(room_name)
                            new_key_name = config_manager.get_key_name_by_value(new_key)
                            
                            if new_key and new_key_name not in tried_keys:
                                print(f"  - [Scenery Rotation] Attempting retry {attempt + 2}/{max_retries} with new key: {new_key_name}")
                                current_api_key = new_key
                                continue
                            else:
                                print(f"  - [Scenery Rotation] No more available keys for retry.")
                                raise e
                        else:
                            print(f"  - [Scenery Rotation] Max retries reached.")
                            raise e
                    else:
                        # 429以外の例外はそのままリレー（下のexcept Exception as eで処理される）
                        raise e
        else:
            scenery_text = "（場所の定義がないため、情景を描写できません）"
    except Exception as e:
        # すでに内部でraiseされた例外もここでキャッチされる
        err_str = str(e).upper()
        if isinstance(e, google_exceptions.ResourceExhausted) or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
             print(f"  - [Scenery Error] Quota limit hit (429). All rotation attempts failed. {e}")
             raise e
        print(f"--- 警告: 情景描写の生成中にエラーが発生しました ---\n{traceback.format_exc()}")
        location_display_name = "（エラー）"
        scenery_text = "（情景描写の生成中にエラーが発生しました）"
        space_def = "（エラー）"
    return location_display_name, space_def, scenery_text
