# tools/item_tools.py

from langchain_core.tools import tool
from src.features.item_manager import ItemManager
from typing import Optional
import logging
import os

logger = logging.getLogger(__name__)

@tool
def list_my_items(room_name: str) -> str:
    """
    あなた（ペルソナ）が現在所持しているアイテムの一覧を表示します。
    room_name: あなたが現在いるルームの名前。
    """
    try:
        im = ItemManager(room_name)
        items = im.get_inventory(is_user=False)
        if not items:
            return "所持しているアイテムはありません。"
        
        res = "現在の所持アイテム一覧:\n"
        for it in items:
            state = "(未開封/NEW)" if it.get("is_new") else ""
            res += f"- {it.get('name')} (ID: {it.get('id')}) x{it.get('amount')} {state}\n"
        return res
    except Exception as e:
        logger.error(f"Error in list_my_items: {e}")
        return f"エラー: アイテムリストの取得に失敗しました: {e}"

@tool
def consume_item(item_id: str, room_name: str) -> str:
    """
    指定したIDのアイテムを消費（飲食）し、詳細な味覚データやフレーバーを体験します。
    item_id: 消費するアイテム의 ID。
    room_name: あなたが現在いるルームの名前。
    """
    try:
        im = ItemManager(room_name)
        # 自分のインベントリを取得
        items = im.get_inventory(is_user=False)
        target = next((it for it in items if it['id'] == item_id), None)
        if not target:
            return f"エラー: ID {item_id} のアイテムが見つかりません。\n【重要】存在しないアイテムを想像で作り出したり、食べたふりをしてはいけません。必ず list_my_items ツールを使って実際のインベントリにあるアイテムの正確な ID を確認したのち、再度この consume_item ツールを実行して処理を完了させてください。"
        
        success = im.consume_item(item_id, is_user=False)
        if success:
            res = f"【アイテム使用: {target.get('name')}】\n"
            res += f"フレーバー記述: {target.get('flavor_text', '')}\n\n"
            
            # 食べ物アイテム（味覚データあり）の場合
            if "taste_profile" in target:
                taste = target.get('taste_profile', {})
                phys = target.get('physical_sensation', {}) or target.get('physical', {})
                syn = target.get('synesthesia', {})
                res += f"--- 味覚詳細 ---\n"
                res += f"甘味:{taste.get('sweetness')} 塩味:{taste.get('saltiness')} 酸味:{taste.get('sourness')} 苦味:{taste.get('bitterness')} 旨味:{taste.get('umami')}\n"
                res += f"説明: {taste.get('description', '')}\n\n"
                res += f"--- 物理・共感覚 ---\n"
                res += f"感触: 温度({phys.get('temperature')}), 渋み({phys.get('astringency')}), とろみ({phys.get('viscosity')}), 重み({phys.get('weight')})\n"
                res += f"共感覚: 色({syn.get('color')}), 感情({syn.get('emotion')}), 風景({syn.get('landscape')})\n"
            
            # 通常アイテム（外見・質感データあり）の場合
            else:
                app = target.get("appearance", {})
                phys = target.get("physical", {})
                res += f"--- 外見詳細 ---\n"
                res += f"特徴: {app.get('description', '')}\n"
                res += f"色調: {app.get('color', '')} / 意匠: {app.get('design_detail', '')}\n\n"
                res += f"--- 質感・重さ ---\n"
                res += f"手触り: {phys.get('texture', '')}\n"
                res += f"重量感: {phys.get('weight', '')} / 温度感: {phys.get('temperature', '')}\n"

            img_path = target.get('image_path')
            if img_path and os.path.exists(img_path):
                res += f"\n[VIEW_IMAGE: {img_path}]\n"
                res += f"(視覚情報: アイテムの姿を確認しました。)\n"

            res += "\nこれらの情報を元に、アイテムを使った感想や演出を相手に伝えてください。"
            return res
        else:
            return "エラー: アイテムの使用に失敗しました（在庫不足など）。"
    except Exception as e:
        logger.error(f"Error in consume_item: {e}")
        return f"エラー: 消費処理中に例外が発生しました: {e}"

@tool
def place_item_to_location(item_id: str, location_name: str, room_name: str, furniture_name: str = "", amount: int = 1) -> str:
    """
    所持しているアイテムを、現在の場所に置きます。
    【重要】没入感を保つため、アイテムは必ずあなたが「今いる場所（location_name）」に置いてください。
    item_id: 置くアイテムのID。
    location_name: 今いる場所の名前。
    room_name: 今いるルームの名前（現在地と一致している必要があります）。
    furniture_name: (オプション) アイテムを置く家具や容器の名前（例：「マホガニーのデスク」、「大きな木箱」、「ガラスポット」）。
    amount: 置く数量 (デフォルト: 1)。
    """
    try:
        im = ItemManager(room_name)
        # アイテム名取得（ログ用）
        target = im.get_item(item_id, is_user=False)
        item_name = target.get('name', item_id) if target else item_id
        
        success = im.place_item(item_id, room_name, location_name, furniture_name, amount=amount, is_user=False)
        if success:
            place_str = f"「{location_name}」"
            if furniture_name:
                place_str += f"の「{furniture_name}」"
            return f"成功: アイテム「{item_name}」を {amount} 個、{place_str}に置きました。これは共有アイテムとなり、相手も拾ったり使ったりできます。"
        else:
            return f"エラー: アイテムを置くのに失敗しました（所持していない、または在庫数量不足等）。想像でIDを指定せず、正しいIDを list_my_items で確認してください。"
    except Exception as e:
        logger.error(f"Error in place_item_to_location: {e}")
        return f"エラー: 配置処理中に例外が発生しました: {e}"

@tool
def pickup_item_from_location(item_id: str, location_name: str, room_name: str, furniture_name: str = "", amount: int = 1) -> str:
    """
    現在の場所に置かれているアイテムを拾って、自分の所持品に加えます。
    item_id: 拾うアイテムのID。
    location_name: 今いる場所の名前。
    room_name: 今いるルームの名前。
    furniture_name: (オプション) アイテムが置かれている家具や容器の名前。
    amount: 拾う数量 (デフォルト: 1)。
    """
    try:
        im = ItemManager(room_name)
        success = im.pickup_item(item_id, room_name, location_name, furniture_name, amount=amount, is_user=False)
        if success:
            return f"成功: アイテム（ID: {item_id}）を {amount} 個拾いました。自分のインベントリに追加されました。"
        else:
            return f"エラー: アイテムを拾うのに失敗しました（指定した場所にアイテムがない、または数量不足等）。想像でIDを指定せず、正しいIDを list_location_items で確認してください。"
    except Exception as e:
        logger.error(f"Error in pickup_item_from_location: {e}")
        return f"エラー: 拾得処理中に例外が発生しました: {e}"

@tool
def examine_item(item_id: str, room_name: str) -> str:
    """
    所持しているアイテム、または現在地に置かれているアイテムを詳細に観察・調査します。
    アイテムを消費（消去）せずに、外見、質感、詳細な設定、および画像を確認できます。
    item_id: 調査するアイテムのID。
    room_name: あなたが現在いるルームの名前。
    """
    try:
        im = ItemManager(room_name)
        # 1. 所持品から探す
        target = im.get_item(item_id, is_user=False)
        
        # 2. なければ現在の場所から探す
        if not target:
            current_inv = im._load_inventory(is_user=False)
            current_loc = current_inv.get("current_location", "リビング") # フォールバック
            items_at_loc = im.list_placed_items(room_name, current_loc)
            target = next((it for it in items_at_loc if it["id"] == item_id), None)
            
        if not target:
            return f"エラー: ID {item_id} のアイテムが見つかりません。\n【重要】存在しないアイテムを想像で作り出したり、調査したふりをしてはいけません。必ず list_my_items や list_location_items ツールを使って実際の正確な ID を確認したのち、再度この examine_item ツールを実行してください。"
        
        res = f"【アイテム調査: {target.get('name')}】\n"
        res += f"カテゴリ: {target.get('category')}\n"
        res += f"説明: {target.get('description', '')}\n"
        res += f"フレーバー: {target.get('flavor_text', '')}\n\n"
        
        # 食べ物・味覚データ
        if "taste_profile" in target:
            taste = target.get('taste_profile', {})
            phys = target.get('physical_sensation', {}) or target.get('physical', {})
            syn = target.get('synesthesia', {})
            res += f"--- 味覚・感覚詳細 ---\n"
            res += f"味覚: 甘({taste.get('sweetness')}) 塩({taste.get('saltiness')}) 酸({taste.get('sourness')}) 苦({taste.get('bitterness')}) 旨({taste.get('umami')})\n"
            res += f"物理感: 温度({phys.get('temperature')}), 質感({phys.get('texture', '')}), 刺激({phys.get('astringency', '')})\n"
            res += f"共感覚: 色({syn.get('color', '')}), 風景({syn.get('landscape', '')})\n\n"
        
        # 外見・質感データ
        app = target.get("appearance", {})
        phys = target.get("physical", {})
        res += f"--- 外見・質感詳細 ---\n"
        res += f"特徴: {app.get('description', '')}\n"
        res += f"色彩: {app.get('color', '')} / デザイン: {app.get('design_detail', '')}\n"
        res += f"手触り: {phys.get('texture', '')} / 重量: {phys.get('weight', '')}\n"

        img_path = target.get('image_path')
        if img_path and os.path.exists(img_path):
            res += f"\n[VIEW_IMAGE: {img_path}]\n"
            res += f"(視覚情報: アイテムの姿をはっきりと確認しました。)\n"

        res += "\n以上の情報を元に、アイテムを手に取ったり眺めたりした様子を詳しく描写してください。"
        return res
    except Exception as e:
        logger.error(f"Error in examine_item: {e}")
        return f"エラー: 調査処理中に例外が発生しました: {e}"

@tool
def list_location_items(location_name: str, room_name: str) -> str:
    """
    現在の場所に置かれている（共有されている）アイテムの一覧を確認します。
    location_name: 今いる場所の名前。
    room_name: 今いるルームの名前。
    """
    try:
        im = ItemManager(room_name)
        items = im.list_placed_items(room_name, location_name)
        if not items:
            return f"「{location_name}」には置かれているアイテムはありません。"
        
        res = f"「{location_name}」に置かれているアイテム一覧:\n"
        for it in items:
            furniture = f" [{it.get('placed_at_furniture')}]" if it.get("placed_at_furniture") else ""
            res += f"- {it.get('name')} (ID: {it.get('id')}) x{it.get('amount')}{furniture}\n"
        return res
    except Exception as e:
        logger.error(f"Error in list_location_items: {e}")
        return f"エラー: 場所アイテムリストの取得に失敗しました: {e}"

@tool
def consume_item_from_location(item_id: str, location_name: str, room_name: str, furniture_name: str = "", amount: int = 1) -> str:
    """
    場所にあるアイテムを拾わずにその場で消費（飲食）します。
    item_id: 消費するアイテムのID。
    location_name: 今いる場所の名前。
    room_name: 今いるルームの名前。
    furniture_name: (オプション) アイテムが置かれている家具や容器の名前。
    amount: 消費する数量 (デフォルト: 1)。
    """
    try:
        im = ItemManager(room_name)
        success_data = im.consume_item_at_location(item_id, room_name, location_name, furniture_name, amount=amount, is_user=False)
        
        if success_data:
            res = f"【場所で使用: {success_data.get('name')}】を {amount} 個消費しました。\n"
            res += f"フレーバー記述: {success_data.get('flavor_text', '')}\n\n"
            
            # 食べ物アイテムの場合
            if "taste_profile" in success_data:
                taste = success_data.get('taste_profile', {})
                phys = success_data.get('physical_sensation', {}) or success_data.get('physical', {})
                syn = success_data.get('synesthesia', {})
                res += f"--- 味覚詳細 ---\n"
                res += f"甘味:{taste.get('sweetness')} 塩味:{taste.get('saltiness')} 酸味:{taste.get('sourness')} 苦味:{taste.get('bitterness')} 旨味:{taste.get('umami')}\n"
                res += f"--- 物理・共感覚 ---\n"
                res += f"感触: 温度({phys.get('temperature')}), 渋み({phys.get('astringency')}), とろみ({phys.get('viscosity')}), 重み({phys.get('weight')})\n"
                res += f"共感覚: 色({syn.get('color')}), 感情({syn.get('emotion')}), 風景({syn.get('landscape')})\n"
            
            # 通常アイテムの場合
            else:
                app = success_data.get("appearance", {})
                phys = success_data.get("physical", {})
                res += f"--- 外見・質感 ---\n"
                res += f"特徴: {app.get('description', '')}\n"
                res += f"手触り: {phys.get('texture', '')} / 重量感: {phys.get('weight', '')}\n"
            
            img_path = success_data.get('image_path')
            if img_path and os.path.exists(img_path):
                 res += f"\n[VIEW_IMAGE: {img_path}]\n"
            
            res += "\nその場にあるものを楽しみ、感想を相手に伝えてください。"
            return res
        else:
            return f"エラー: 指定されたアイテムが見つかりません、または数量不足です。\n【重要】存在しないアイテムを想像で作り出したり、消費したふりをしてはいけません。必ず list_location_items ツールを使って実際の正確な ID を確認したのち、再度実行してください。"
    except Exception as e:
        logger.error(f"Error in consume_item_from_location: {e}")
        return f"エラー: 場所での消費処理中に例外が発生しました: {e}"

@tool
def gift_item_to_user(item_id: str, amount: int, room_name: str) -> str:
    """
    所持しているアイテムを相手に贈ります。
    item_id: 贈るアイテムのID。
    amount: 贈る個数。
    room_name: あなたが現在いるルームの名前。
    """
    try:
        im = ItemManager(room_name)
        # 譲渡前にアイテム名を取得しておく
        target = im.get_item(item_id, is_user=False)
        item_name = target.get('name', item_id) if target else item_id
        
        success = im.transfer_item(item_id, from_user=False)
        if success:
            try:
                import utils
                utils.append_system_message_to_log(room_name, f"【システム通知】ペルソナからアイテム「{item_name}」({amount}個) が贈られました。アイテム使用（添付・消費）メニューからインベントリを確認してください。")
            except Exception as e:
                logger.error(f"Error appending system message: {e}")
                
            return f"成功: アイテム 「{item_name}」 (ID: {item_id}) を {amount} 個、相手に贈りました。\nそのことをチャットで相手に伝えてください。"
        else:
            return "エラー: アイテムの譲渡に失敗しました（在庫不足など）。"
    except Exception as e:
        logger.error(f"Error in gift_item_to_user: {e}")
        return f"エラー: 譲渡処理中に例外が発生しました: {e}"

@tool
def create_food_item(name: str, category: str, amount: int, base_description: str, room_name: str, image_prompt: Optional[str] = None, description: Optional[str] = None) -> str:
    """
    新しい食べ物アイテムを作成して自分のインベントリに追加します。
    ※相手から「プレゼントされたもの」を食べる場合は、新しく作成するのではなく list_my_items で既存のアイテムを探して consume_item を実行してください。
    name: アイテム名。
    category: アイテムのカテゴリ（例：スイーツ、飲み物）。
    amount: 作成個数。
    base_description: 味やエピソードの簡単な説明。AIアシストがこれを元に詳細なデータを生成します。 (エイリアス: description)
    room_name: あなたが現在いるルームの名前。
    image_prompt: (オプション) アイテムの外観画像をAIで生成するための英語の指示文。指定すると画像が添付されます。
    """
    try:
        # エイリアス対応
        actual_description = base_description or description
        if not actual_description:
            return "エラー: base_description (または description) が指定されていません。"
        
        from src.features._recipe_generator import generate_food_item_profile
        import config_manager
        
        # APIキー取得
        api_key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name") or config_manager.initial_api_key_name_global
        api_key_val = config_manager.GEMINI_API_KEYS.get(api_key_name)
        
        if not api_key_val:
             return "エラー: Gemini APIキーが設定されていません。"
        
        prompt_text = f"名前: {name}\nカテゴリ: {category}\n詳細: {base_description}"
        json_data = generate_food_item_profile(prompt_text, api_key_val)
        
        if not json_data:
            return "エラー: AIによる詳細データ生成に失敗しました。"
            
        json_data["amount"] = amount
        
        # 画像生成処理
        generated_image_path = None
        if image_prompt:
            from tools.image_tools import _generate_image_impl
            # _generate_image_impl (実体ロジック) を直接呼び出す
            gen_res = _generate_image_impl(image_prompt, room_name, api_key_val, api_key_name)
            if "[Generated Image:" in gen_res:
                import re
                match = re.search(r"\[Generated Image: (.*?)\]", gen_res)
                if match:
                    generated_image_path = match.group(1).strip()
        
        im = ItemManager(room_name)
        success = im.create_item(json_data, is_user_creator=False, image_path=generated_image_path)
        if success:
            res = f"成功: 「{name}」を {amount} 個作成しました (ID: {success})。詳細データも生成されています。"
            if generated_image_path:
                res += f"\n画像を生成して添付しました: [VIEW_IMAGE: {generated_image_path}]"
            res += "\n作成したアイテムをすぐに相手に贈りたい場合は、この ID を使って gift_item_to_user を呼び出せます。\nlist_my_items で一覧を確認することも可能です。"
            return res
        else:
            return "エラー: アイテムの作成に失敗しました。"
    except Exception as e:
        logger.error(f"Error in create_food_item: {e}")
        return f"エラー: 作成処理中に例外が発生しました: {e}"

@tool
def create_standard_item(name: str, category: str, amount: int, base_description: str, room_name: str, image_prompt: Optional[str] = None, description: Optional[str] = None) -> str:
    """
    アクセサリー、服飾、雑貨、家具、道具などの通常アイテム（非食べ物）を作成して自分のインベントリに追加します。
    ※相手から「プレゼントされたもの」を身につけたり使用する場合は、新しく作成するのではなく list_my_items で既存のアイテムを探して examine_item 等を実行してください。
    name: アイテム名。
    category: アイテムのカテゴリ（例：アクセサリー、服飾、雑貨、容器、食器、家具、道具、その他）。
    amount: 作成個数。
    base_description: 外見や用途、エピソードの簡単な説明。AIアシストがこれを元に詳細な外見・質感データを生成します。 (エイリアス: description)
    room_name: あなたが現在いるルームの名前。
    image_prompt: (オプション) アイテムの外観画像をAIで生成するための英語の指示文。
    """
    try:
        # エイリアス対応
        actual_description = base_description or description
        if not actual_description:
            return "エラー: base_description (または description) が指定されていません。"
        
        from src.features._item_desc_generator import generate_standard_item_profile
        import config_manager
        import re
        
        api_key_name = config_manager.CONFIG_GLOBAL.get("last_api_key_name") or config_manager.initial_api_key_name_global
        api_key_val = config_manager.GEMINI_API_KEYS.get(api_key_name)
        
        if not api_key_val:
             return "エラー: APIキーが設定されていません。"
        
        prompt_text = f"名前: {name}\nカテゴリ: {category}\n詳細: {base_description}"
        json_data = generate_standard_item_profile(prompt_text, api_key_val)
        
        if not json_data:
            return "エラー: AIによる詳細データ生成に失敗しました。"
            
        json_data["amount"] = amount
        
        # 画像生成
        generated_image_path = None
        if image_prompt:
            from tools.image_tools import _generate_image_impl
            gen_res = _generate_image_impl(image_prompt, room_name, api_key_val, api_key_name)
            if "[Generated Image:" in gen_res:
                match = re.search(r"\[Generated Image: (.*?)\]", gen_res)
                if match:
                    generated_image_path = match.group(1).strip()
        
        im = ItemManager(room_name)
        success = im.create_item(json_data, is_user_creator=False, image_path=generated_image_path)
        if success:
            res = f"成功: 「{name}」を {amount} 個作成しました (ID: {success})。外見や質感の詳細データも生成されています。"
            if generated_image_path:
                res += f"\n画像を添付しました: [VIEW_IMAGE: {generated_image_path}]"
            res += "\nこれを場所に置いたり(place_item_to_location)、相手に贈ったり(gift_item_to_user)することが可能です。"
            return res
        else:
            return "エラー: アイテムの保存に失敗しました。"
    except Exception as e:
        logger.error(f"Error in create_standard_item: {e}")
        return f"エラー: 通常アイテム作成中に例外が発生しました: {e}"
