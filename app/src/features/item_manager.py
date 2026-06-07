import json
import os
import uuid
import logging
from typing import Dict, List, Optional

import constants

# ロガーの設定
logger = logging.getLogger(__name__)

class ItemManager:
    """
    アイテムデータ（主に食べ物アイテムシステム）の管理を行うクラス。
    JSONファイルでの永続化、インベントリ操作（追加、消費、譲渡）を提供する。
    """
    def __init__(self, room_name: str):
        self.room_name = room_name
        # アイテム保存用ディレクトリ: characters/<room_name>/items/
        self.items_dir = os.path.join(constants.ROOMS_DIR, room_name, "items")
        self.images_dir = os.path.join(self.items_dir, "images")
        os.makedirs(self.items_dir, exist_ok=True)
        os.makedirs(self.images_dir, exist_ok=True)
        # ユーザー用インベントリファイル
        self.user_inventory_file = os.path.join(self.items_dir, "user_inventory.json")
        # ペルソナ用インベントリファイル
        self.persona_inventory_file = os.path.join(self.items_dir, "persona_inventory.json")
        
        # 初期化時にファイルがなければ空の構造を作成
        self._init_inventory_file(self.user_inventory_file)
        self._init_inventory_file(self.persona_inventory_file)

    def _init_inventory_file(self, filepath: str):
        if not os.path.exists(filepath):
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({"items": []}, f, ensure_ascii=False, indent=2)

    def _load_inventory(self, is_user: bool = True) -> Dict:
        """インベントリのJSONを読み込む"""
        filepath = self.user_inventory_file if is_user else self.persona_inventory_file
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"インベントリ読み込みエラー ({filepath}): {e}")
            return {"items": []}

    def _save_inventory(self, data: Dict, is_user: bool = True):
        """インベントリのJSONを保存する"""
        filepath = self.user_inventory_file if is_user else self.persona_inventory_file
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"インベントリ保存エラー ({filepath}): {e}")

    def create_item(self, item_data: Dict, is_user_creator: bool = True, image_path: Optional[str] = None) -> str:
        """
        新しいアイテムを作成、または既存のアイテムを更新し、作成者のインベントリに保存する。
        item_dataには必ず 'name', 'category', 'amount' が含まれていること。
        """
        inventory = self._load_inventory(is_user=is_user_creator)
        
        # IDの決定（既存があればそれを使う、なければ新規発行）
        item_id = item_data.get("id") or str(uuid.uuid4())
        item_data["id"] = item_id
        item_data["creator"] = item_data.get("creator") or ("User" if is_user_creator else "Persona")
        item_data["is_new"] = False # 自分で作った/編集したものは既知

        # 既存アイテムの確認
        existing_idx = next((idx for idx, i in enumerate(inventory.get("items", [])) if i.get("id") == item_id), None)
        
        # 画像の処理と保存
        if image_path and os.path.exists(image_path):
            try:
                import utils
                from PIL import Image
                
                # 画像を軽量化して保存
                with Image.open(image_path) as img:
                    if img.mode != 'RGB' and img.mode != 'RGBA':
                        img = img.convert('RGBA')
                        
                    resized_img = utils.resize_image_for_api(img, max_size=768, return_image=True)
                    if not resized_img: resized_img = img
                        
                    ext = ".png"
                    save_filename = f"{item_id}{ext}"
                    save_path = os.path.join(self.images_dir, save_filename)
                    
                    resized_img.save(save_path, format="PNG", optimize=True)
                    item_data["image_path"] = save_path
            except Exception as e:
                logger.error(f"アイテム画像保存エラー ({item_id}): {e}")
        elif existing_idx is not None:
             # 新しい画像が指定されていないが、既存アイテムがある場合は画像パスを維持
             old_item = inventory["items"][existing_idx]
             if "image_path" in old_item:
                 item_data["image_path"] = old_item["image_path"]
        
        # 重複チェックと置換（upsert）
        if existing_idx is not None:
            inventory["items"][existing_idx] = item_data
        else:
            inventory["items"].append(item_data)
            
        self._save_inventory(inventory, is_user=is_user_creator)
        return item_id

    def get_inventory(self, is_user: bool = True) -> List[Dict]:
        """指定された側（ユーザーまたはペルソナ）の所持アイテムリストを取得する"""
        inventory = self._load_inventory(is_user)
        return inventory.get("items", [])

    def get_item(self, item_id: str, is_user: bool = True) -> Optional[Dict]:
        """指定されたIDのアイテムを取得する"""
        inventory = self.get_inventory(is_user)
        for item in inventory:
            if item.get("id") == item_id:
                return item
        return None

    def consume_item(self, item_id: str, is_user: bool = True) -> Optional[Dict]:
        """
        アイテムを消費（使用）する。所持数を1減らし、詳細情報を返す。
        所持数が0になった場合はリストから削除する。
        """
        inventory = self._load_inventory(is_user)
        for item in inventory.get("items", []):
            if item.get("id") == item_id:
                item["amount"] -= 1
                item["is_new"] = False # 消費した時点で既知になる
                consumed_item_data = dict(item) # 返却用にコピー
                
                if item["amount"] <= 0:
                    inventory["items"].remove(item)
                
                self._save_inventory(inventory, is_user)
                return consumed_item_data
        return None

    def transfer_item(self, item_id: str, from_user: bool = True) -> bool:
        """
        アイテムを相手に添付（譲渡）する。
        送り主の所持数を1減らし、受取人のインベントリに全データを追加する（最初は情報を秘匿する想定）。
        """
        sender_inventory = self._load_inventory(from_user)
        receiver_inventory = self._load_inventory(not from_user)
        
        target_item = None
        for item in sender_inventory.get("items", []):
            if item.get("id") == item_id:
                target_item = dict(item) # コピーを作成して送る
                
                # 送り主から減算
                item["amount"] -= 1
                if item["amount"] <= 0:
                    sender_inventory["items"].remove(item)
                break
                
        if not target_item:
            logger.warning(f"譲渡対象のアイテムが見つかりません: {item_id}")
            return False
            
        # 受取人側への追加準備
        # 受取人にとっては「貰い物」であり、最初は中身を知らない状態にする
        target_item["amount"] = 1
        target_item["is_new"] = True 
        
        # 既に同じアイテム（ID共通）を持っていればスタックを加算
        existing_item = next((i for i in receiver_inventory.get("items", []) if i.get("id") == target_item["id"]), None)
        if existing_item:
            existing_item["amount"] += 1
            existing_item["is_new"] = True # 新たに貰ったのでNEWフラグを立て直す
        else:
            receiver_inventory["items"].append(target_item)
            
        self._save_inventory(sender_inventory, from_user)
        self._save_inventory(receiver_inventory, not from_user)
        return True

    # --- 拡張機能: 場所への配置と管理 ---

    def _get_placed_items_path(self, room_name: str) -> str:
        """ルーム内の配置アイテム保存パスを取得する"""
        import constants
        # placed_items.jsonもitemsディレクトリ配下にまとめる
        return os.path.join(constants.ROOMS_DIR, room_name, "items", "placed_items.json")

    def _load_placed_items(self, room_name: str) -> Dict:
        """場所（ルーム）に置かれたアイテム情報を読み込む"""
        path = self._get_placed_items_path(room_name)
        if not os.path.exists(path):
            return {"locations": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"配置アイテム読み込みエラー ({room_name}): {e}")
            return {"locations": {}}

    def _save_placed_items(self, room_name: str, data: Dict):
        """場所（ルーム）に置かれたアイテム情報を保存する"""
        path = self._get_placed_items_path(room_name)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"配置アイテム保存エラー ({room_name}): {e}")

    def place_item(self, item_id: str, room_name: str, location_name: str, furniture_name: str = "", amount: int = 1, is_user: bool = True) -> bool:
        """自分の持ち物を場所に置く"""
        inventory = self._load_inventory(is_user)
        placed_data = self._load_placed_items(room_name)
        
        target_item = None
        for item in inventory.get("items", []):
            if item.get("id") == item_id:
                if item["amount"] < amount:
                    return False # 在庫不足
                
                target_item = dict(item)
                item["amount"] -= amount
                if item["amount"] <= 0:
                    inventory["items"].remove(item)
                break
        
        if not target_item:
            return False
            
        target_item["amount"] = amount
        
        if location_name not in placed_data["locations"]:
            placed_data["locations"][location_name] = []
        
        # 家具（コンテナ）情報の付加
        target_item["placed_at_furniture"] = furniture_name
        
        # 既存スタック確認
        existing = next((i for i in placed_data["locations"][location_name] 
                         if i.get("id") == target_item["id"] and i.get("placed_at_furniture") == furniture_name), None)
        if existing:
            existing["amount"] += amount
        else:
            placed_data["locations"][location_name].append(target_item)
            
        self._save_inventory(inventory, is_user)
        self._save_placed_items(room_name, placed_data)
        return True

    def pickup_item(self, item_id: str, room_name: str, location_name: str, furniture_name: str = "", amount: int = 1, is_user: bool = True) -> bool:
        """場所にあるアイテムを拾う"""
        placed_data = self._load_placed_items(room_name)
        inventory = self._load_inventory(is_user)
        
        location_items = placed_data["locations"].get(location_name, [])
        target_item = None
        for item in location_items:
            if item.get("id") == item_id and item.get("placed_at_furniture") == furniture_name:
                if item["amount"] < amount:
                    return False # 設置数不足
                
                target_item = dict(item)
                item["amount"] -= amount
                if item["amount"] <= 0:
                    location_items.remove(item)
                break
                
        if not target_item:
            return False
            
        target_item["amount"] = amount
        target_item.pop("placed_at_furniture", None) # 所持品に戻る際は配置場所情報を消す
        
        # 在庫スタック
        existing = next((i for i in inventory.get("items", []) if i.get("id") == target_item["id"]), None)
        if existing:
            existing["amount"] += amount
        else:
            inventory["items"].append(target_item)
            
        self._save_inventory(inventory, is_user)
        self._save_placed_items(room_name, placed_data)
        return True

    def list_placed_items(self, room_name: str, location_name: str) -> List[Dict]:
        """特定の場所に置かれているアイテムのリストを返す"""
        placed_data = self._load_placed_items(room_name)
        return placed_data["locations"].get(location_name, [])

    def delete_item(self, item_id: str, is_user: bool = True) -> bool:
        """アイテムを完全に削除する"""
        inventory = self._load_inventory(is_user)
        original_len = len(inventory.get("items", []))
        inventory["items"] = [i for i in inventory.get("items", []) if i.get("id") != item_id]
        
        if len(inventory["items"]) < original_len:
            self._save_inventory(inventory, is_user)
            return True
        return False

    def copy_item(self, item_id: str, is_user: bool = True) -> Optional[str]:
        """アイテムをコピーして1つ増やす"""
        inventory = self._load_inventory(is_user)
        target = next((i for i in inventory.get("items", []) if i.get("id") == item_id), None)
        if target:
            new_item = dict(target)
            new_id = str(uuid.uuid4())
            new_item["id"] = new_id
            new_item["amount"] = 1
            new_item["name"] = f"{target['name']} (コピー)"
            inventory["items"].append(new_item)
            self._save_inventory(inventory, is_user)
            return new_id
        return None

    def consume_item_at_location(self, item_id: str, room_name: str, location_name: str, furniture_name: str = "", amount: int = 1, is_user: bool = True) -> Optional[Dict]:
        """場所にあるアイテムをその場で消費する（インベントリを経由しない）"""
        placed_data = self._load_placed_items(room_name)
        
        location_items = placed_data["locations"].get(location_name, [])
        target_item = None
        for item in location_items:
            if item.get("id") == item_id and item.get("placed_at_furniture") == furniture_name:
                if item["amount"] < amount:
                    return None # 設置数不足
                
                target_item = dict(item)
                item["amount"] -= amount
                if item["amount"] <= 0:
                    location_items.remove(item)
                break
                
        if not target_item:
            return None
            
        target_item["amount"] = amount
        target_item["is_new"] = False
        target_item.pop("placed_at_furniture", None)
        
        self._save_placed_items(room_name, placed_data)
        return target_item
