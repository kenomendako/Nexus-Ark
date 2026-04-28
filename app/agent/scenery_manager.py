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

        try:
            from src.features.item_manager import ItemManager
            im = ItemManager(room_name)
            placed_items = im.list_placed_items(room_name, current_location_name)
            if placed_items:
                item_details = []
                for it in placed_items:
                    detail = f"{it.get('name')} x{it.get('amount')}"
                    if it.get("placed_at_furniture"):
                        detail += f" ({it.get('placed_at_furniture')}にある)"
                    item_details.append(detail)
                
                items_str = "、".join(item_details)
                space_def += f"\n\n現在、この場所には以下のアイテムが置かれています：{items_str}。"
        except Exception as e:
            # アイテム情報の取得失敗は致命的ではないため、ログのみ残す
            print(f"--- [Scenery Warning] アイテム情報の取得に失敗しました: {e} ---")

        from utils import get_season, get_time_of_day, load_scenery_cache, save_scenery_cache
        import hashlib
        import datetime

        now = datetime.datetime.now()
        effective_season = season_en or get_season(now.month)
        effective_time_of_day = time_of_day_en or get_time_of_day(now.hour)

        content_hash = hashlib.md5(space_def.encode('utf-8')).hexdigest()[:8]
        cache_key = f"{current_location_name}_{content_hash}_{effective_season}_{effective_time_of_day}"

        # プレースホルダーキー（未設定状態）の判定
        is_placeholder_key = not api_key or api_key == "YOUR_API_KEY_HERE" or api_key.startswith("AIzaSyB-") # デフォルト同梱の期限切れキー等

        if not force_regenerate:
            scenery_cache = load_scenery_cache(room_name)
            # 1. 完全一致キャッシュを確認
            if cache_key in scenery_cache:
                cached_data = scenery_cache[cache_key]
                # print(f"--- [有効な情景キャッシュを発見] ({cache_key})。APIコールをスキップします ---")
                return location_display_name, space_def, utils.get_content_as_string(cached_data["scenery_text"])
            
            # 2. 未設定状態なら、その場所の他のキャッシュを優先して探す（オンボーディング体験の保護）
            if is_placeholder_key:
                # [v10] キャッシュ流用の高度化：現在の季節・時間帯を尊重する
                # まずは「場所_ハッシュ_季節」まで一致するものを探す
                location_hash_season_prefix = f"{current_location_name}_{content_hash}_{effective_season}_"
                for k, v in scenery_cache.items():
                    if k.startswith(location_hash_season_prefix):
                        return location_display_name, space_def, v["scenery_text"]
                
                # 次に「場所_ハッシュ」が一致するものを探す（季節違いでも場所が同じなら採用）
                location_hash_prefix = f"{current_location_name}_{content_hash}_"
                for k, v in scenery_cache.items():
                    if k.startswith(location_hash_prefix):
                        return location_display_name, space_def, v["scenery_text"]
                
                # 最後に、ハッシュが含まれない古い形式（場所名のみ一致）でもあれば返す
                location_old_prefix = f"{current_location_name}_"
                for k, v in scenery_cache.items():
                    if k.startswith(location_old_prefix):
                        return location_display_name, space_def, v["scenery_text"]

        if not space_def.startswith("（"):
            try:
                # プロンプトの構築
                season_map_en_to_ja = {"spring": "春", "summer": "夏", "autumn": "秋", "winter": "冬"}
                season_ja = season_map_en_to_ja.get(effective_season, "不明な季節")
                
                time_map_en_to_ja = {
                    "early_morning": "早朝", "morning": "朝", "late_morning": "昼前",
                    "afternoon": "昼下がり", "evening": "夕方", "night": "夜", "midnight": "深夜"
                }
                time_of_day_ja = time_map_en_to_ja.get(effective_time_of_day, "不明な時間帯")

                scenery_prompt = (
                    "あなたは、与えられた情報源から、一つのまとまった情景を描き出す情景描写の専門家です。\n\n"
                    f"【情報源1：時間・季節】\n- 時間帯: {time_of_day_ja}\n- 季節: {season_ja}\n\n"
                    f"【情報源2：空間設定と設置アイテム】\n---\n{space_def}\n---\n\n"
                    "【あなたのタスク】\n"
                    "情報源を統合し、その場のリアルな雰囲気を伝える**最終的な情景描写の文章のみを、2〜3文で生成してください。**\n\n"
                    "【重点指示】\n"
                    "- **設置アイテムの描写**: 「現在、この場所には〜」の後に続くアイテム情報は、その空間の重要なアクセントです。\n"
                    "- **必ず、設置されている全てのアイテムを、その場所（例：テーブルの上など）を含めて描写に盛り込んでください。**\n"
                    "- 複数のアイテムがある場合は、それらが共存している様子を一文の中に自然に組み込んでください。\n\n"
                    "【厳守すべきルール】\n"
                    "- **あなたの思考過程や判断理由は、絶対に出力に含めないでください。**\n"
                    "- 具体的な時刻（例：「23時42分」）は文章に含めないでください。\n"
                    "- 人物やキャラクターの描写は絶対に含めないでください。\n"
                    "- 五感に訴えかける、**空気感まで伝わるような**精緻で写実的な描写を重視してください。"
                )

                # LLMFactoryの統合的なリトライ・ローテーション機能を呼び出す
                response, _ = LLMFactory.invoke_internal_llm(
                    internal_role="summarization",
                    prompt=scenery_prompt,
                    room_name=room_name,
                    api_key=api_key
                )
                
                scenery_text = utils.get_content_as_string(response.content)
                save_scenery_cache(room_name, cache_key, location_display_name, scenery_text)

            except Exception as e:
                # すべてのリトライが失敗した場合
                print(f"--- [Scenery Error] 全てのリトライが失敗しました: {e} ---")
                
                # 最終手段：他時間帯のキャッシュがあればそれを出して取り繕う
                try:
                    scenery_cache = load_scenery_cache(room_name)
                    location_prefix = f"{current_location_name}_{content_hash}_"
                    for k, v in scenery_cache.items():
                        if k.startswith(location_prefix):
                            print(f"  - [Scenery Recovery] エラー発生につき、別時間帯のキャッシュを代用します ({k})")
                            return location_display_name, space_def, v["scenery_text"]
                except: pass

                location_display_name = "（エラー）"
                scenery_text = "（情景描写の生成中にエラーが発生しました）"
                space_def = "（エラー）"
        else:
            scenery_text = "（場所の定義がないため、情景を描写できません）"

    except Exception as e:
        print(f"--- [Scenery Critical Error] 情景描写プロセスで予期せぬエラーが発生しました ---\n{traceback.format_exc()}")
        scenery_text = "（システムエラーにより情景描写を取得できませんでした）"

    return location_display_name, space_def, utils.get_content_as_string(scenery_text)
