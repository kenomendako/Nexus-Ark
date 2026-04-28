# agent/temporary_location_manager.py
# 一時的現在地システム - データモデル・永続化・画像→テキスト生成

import os
import json
import traceback
from datetime import datetime
from typing import Optional, List, Dict, Any

import constants
import utils


class TemporaryLocationManager:
    """
    一時的現在地データの管理を担当するマネージャ。
    
    ワールドビルダーで設計した仮想の固定された場所とは別に、
    ユーザーが実際にいる場所の情報（写真・テキスト）を管理する。
    """

    CACHE_FILENAME = "temporary_location.json"

    def _get_cache_path(self, room_name: str) -> str:
        """キャッシュファイルのパスを返す"""
        return os.path.join(constants.ROOMS_DIR, room_name, "cache", self.CACHE_FILENAME)

    def _load_data(self, room_name: str) -> dict:
        """JSONファイルからデータを読み込む"""
        path = self._get_cache_path(room_name)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"--- [TempLocation Warning] データ読み込みエラー: {e} ---")
        return self._default_data()

    def _save_data(self, room_name: str, data: dict) -> None:
        """JSONファイルにデータを書き出す"""
        path = self._get_cache_path(room_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            print(f"--- [TempLocation Error] データ保存エラー: {e} ---")

    def _default_data(self) -> dict:
        """デフォルトのデータ構造を返す"""
        return {
            "active": False,
            "current": {
                "scenery_text": "",
                "source_description": "",
                "image_path": "",
                "created_at": ""
            },
            "saved_locations": []
        }

    # --- 公開API ---

    def is_active(self, room_name: str) -> bool:
        """一時的現在地がアクティブかどうかを返す"""
        data = self._load_data(room_name)
        return data.get("active", False)

    def set_active(self, room_name: str, active: bool) -> None:
        """一時的現在地のアクティブ状態を切り替える"""
        data = self._load_data(room_name)
        data["active"] = active
        self._save_data(room_name, data)
        print(f"--- [TempLocation] アクティブ状態を {'ON' if active else 'OFF'} に変更 (room: {room_name}) ---")

    def get_current_data(self, room_name: str) -> dict:
        """現在の一時的現在地データを返す"""
        data = self._load_data(room_name)
        return data.get("current", self._default_data()["current"])

    def update_current(self, room_name: str, scenery_text: str, source_description: str = "", image_path: str = "") -> None:
        """現在の一時的現在地データを更新する"""
        data = self._load_data(room_name)
        data["current"] = {
            "scenery_text": scenery_text,
            "source_description": source_description,
            "image_path": image_path,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self._save_data(room_name, data)
        print(f"--- [TempLocation] 現在地データを更新 (room: {room_name}, text_len: {len(scenery_text)}) ---")

    def generate_from_image(self, room_name: str, image_path: str, api_key: str, user_hint: str = "") -> str:
        """
        添付画像からAIが情景テキストを生成する。
        
        Args:
            room_name: ルーム名
            image_path: 画像ファイルのパス
            api_key: Gemini APIキー
            user_hint: ユーザーからの補足情報（場所の名前や詳細など）
            
        Returns:
            生成された情景テキスト
        """
        from llm_factory import LLMFactory
        import config_manager
        from langchain_core.messages import HumanMessage

        try:
            # 内部処理モデルを取得
            llm = LLMFactory.create_chat_model(
                api_key=api_key,
                generation_config={},
                internal_role="processing"
            )

            # 画像をリサイズ+Base64エンコード（トークン消費削減）
            resize_result = utils.resize_image_for_api(image_path, max_size=1024)
            if not resize_result:
                return "（画像の読み込みに失敗しました）"
            
            image_data, image_format = resize_result
            
            # フォーマットからMIMEタイプを決定
            format_to_mime = {
                "jpeg": "image/jpeg", "jpg": "image/jpeg",
                "png": "image/png", "gif": "image/gif",
                "webp": "image/webp", "bmp": "image/bmp"
            }
            mime_type = format_to_mime.get(image_format.lower(), "image/jpeg")

            hint_section = ""
            if user_hint:
                hint_section = f"\n\n【ユーザーからの補足情報】\n{user_hint}"

            prompt_text = (
                "あなたは、写真に写っている場所の情景を描き出す情景描写の専門家です。\n\n"
                "【あなたのタスク】\n"
                "この写真を分析し、写っている場所の雰囲気を伝える**情景描写の文章のみを、3〜5文で生成してください。**\n\n"
                "【重点指示】\n"
                "- 写真から読み取れる場所の特徴（建物、自然、照明、天候など）を描写してください。\n"
                "- 看板や標識が見える場合は、場所の名称も含めてください。\n"
                "- 五感に訴えかける、空気感まで伝わるような描写を重視してください。\n\n"
                "【厳守すべきルール】\n"
                "- あなたの思考過程や判断理由は、絶対に出力に含めないでください。\n"
                "- 人物の描写は含めないでください。\n"
                "- 情景描写の文章のみを出力してください。"
                f"{hint_section}"
            )

            # マルチモーダルメッセージを構築
            message = HumanMessage(
                content=[
                    {"type": "text", "text": prompt_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_data}"
                        }
                    }
                ]
            )

            response = llm.invoke([message])
            scenery_text = utils.get_content_as_string(response.content)

            if scenery_text:
                # 生成結果を自動的に現在データとして保存（画像パスも保持）
                self.update_current(room_name, scenery_text, source_description=user_hint, image_path=image_path)

            return scenery_text

        except Exception as e:
            print(f"--- [TempLocation Error] 画像からの情景生成に失敗: {e} ---")
            traceback.print_exc()
            return f"（画像からの情景生成に失敗しました: {e}）"

    # --- 保存・ロード機能 ---

    def list_saved_locations(self, room_name: str) -> List[str]:
        """保存済みの場所名一覧を返す"""
        data = self._load_data(room_name)
        return [loc.get("name", "") for loc in data.get("saved_locations", [])]

    def save_location(self, room_name: str, name: str) -> bool:
        """
        現在の一時的現在地データを名前をつけて保存する。
        同名の場所が存在する場合は上書きする。
        
        Returns:
            成功した場合 True
        """
        data = self._load_data(room_name)
        current = data.get("current", {})

        if not current.get("scenery_text"):
            print("--- [TempLocation Warning] 保存する情景データがありません ---")
            return False

        new_entry = {
            "name": name,
            "scenery_text": current.get("scenery_text", ""),
            "source_description": current.get("source_description", ""),
            "image_path": current.get("image_path", ""),
            "created_at": current.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        }

        # 同名エントリの上書き
        saved = data.get("saved_locations", [])
        updated = False
        for i, loc in enumerate(saved):
            if loc.get("name") == name:
                saved[i] = new_entry
                updated = True
                break
        if not updated:
            saved.append(new_entry)

        data["saved_locations"] = saved
        self._save_data(room_name, data)
        print(f"--- [TempLocation] 場所を保存: '{name}' (room: {room_name}) ---")
        return True

    def load_location(self, room_name: str, name: str) -> bool:
        """
        保存済みの場所データを現在地として読み込む。
        
        Returns:
            成功した場合 True
        """
        data = self._load_data(room_name)
        saved = data.get("saved_locations", [])

        for loc in saved:
            if loc.get("name") == name:
                data["current"] = {
                    "scenery_text": loc.get("scenery_text", ""),
                    "source_description": loc.get("source_description", ""),
                    "image_path": loc.get("image_path", ""),
                    "created_at": loc.get("created_at", "")
                }
                self._save_data(room_name, data)
                print(f"--- [TempLocation] 場所をロード: '{name}' (room: {room_name}) ---")
                return True

        print(f"--- [TempLocation Warning] 場所 '{name}' が見つかりません ---")
        return False

    def delete_location(self, room_name: str, name: str) -> bool:
        """
        保存済みの場所データを削除する。
        
        Returns:
            成功した場合 True
        """
        data = self._load_data(room_name)
        saved = data.get("saved_locations", [])
        original_len = len(saved)

        data["saved_locations"] = [loc for loc in saved if loc.get("name") != name]

        if len(data["saved_locations"]) < original_len:
            self._save_data(room_name, data)
            print(f"--- [TempLocation] 場所を削除: '{name}' (room: {room_name}) ---")
            return True

        print(f"--- [TempLocation Warning] 削除対象の場所 '{name}' が見つかりません ---")
        return False
