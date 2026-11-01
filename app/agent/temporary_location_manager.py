# agent/temporary_location_manager.py
# 一時的現在地システム - データモデル・永続化・画像→テキスト生成

import hashlib
from io import BytesIO
import os
import threading
import traceback
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path

import constants
import utils
from file_lock_utils import safe_json_read, safe_json_write


_TEMP_LOCATION_IO_LOCK = threading.RLock()


class TemporaryLocationConflictError(ValueError):
    """保存済みの場所を確認なしに別内容で上書きしようとした。"""


class TemporaryLocationImageError(ValueError):
    """保存地点の画像が存在しない、または画像・ルーム境界の条件を満たさない。"""


class TemporaryLocationManager:
    """
    一時的現在地データの管理を担当するマネージャ。
    
    ワールドビルダーで設計した仮想の固定された場所とは別に、
    ユーザーが実際にいる場所の情報（写真・テキスト）を管理する。
    """

    CACHE_FILENAME = "temporary_location.json"

    def __init__(self):
        # ルーム単位の read-modify-write を同一プロセス内で直列化する。
        # プロセス間は file_lock_utils のロックで保護する。
        self._io_lock = _TEMP_LOCATION_IO_LOCK

    def _get_cache_path(self, room_name: str) -> str:
        """キャッシュファイルのパスを返す"""
        return os.path.join(constants.ROOMS_DIR, room_name, "cache", self.CACHE_FILENAME)

    def _load_data(self, room_name: str) -> dict:
        """JSONファイルからデータを読み込む"""
        path = self._get_cache_path(room_name)
        with self._io_lock:
            if os.path.exists(path):
                try:
                    data = safe_json_read(path, default=self._default_data())
                    if isinstance(data, dict):
                        return data
                    raise ValueError("一時的現在地データのJSONルートがオブジェクトではありません")
                except Exception as e:
                    print(f"--- [TempLocation Warning] データ読み込みエラー: {e} ---")
                    # 壊れた既存データを空の既定値で上書きしない。
                    raise
            return self._default_data()

    def _save_data(self, room_name: str, data: dict) -> bool:
        """JSONファイルにアトミックに書き出す。

        保存失敗を握りつぶすと、呼び出し元が成功通知を出して実データとUIが
        食い違うため、I/O例外はそのまま呼び出し元へ返す。
        """
        path = self._get_cache_path(room_name)
        with self._io_lock:
            try:
                saved = safe_json_write(path, data, indent=2)
                if not saved:
                    raise IOError("一時的現在地データの保存に失敗しました")
                return True
            except Exception as e:
                print(f"--- [TempLocation Error] データ保存エラー: {e} ---")
                raise

    def _default_data(self) -> dict:
        """デフォルトのデータ構造を返す"""
        return {
            "active": False,
            "current": {
                "scenery_text": "",
                "source_description": "",
                "image_path": "",
                "location_name": "",
                "created_at": ""
            },
            "saved_locations": []
        }

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _as_mapping(value: Any) -> dict:
        return value if isinstance(value, dict) else {}

    def _room_root(self, room_name: str) -> Path:
        return (Path(constants.ROOMS_DIR).expanduser() / str(room_name)).resolve()

    def _resolve_image_source(self, room_name: str, image_path: Any) -> Path:
        """画像参照を解決し、別ルームへの越境を拒否する。

        本体UIが作るPC一時画像はルーム外の通常ファイルとして許可するが、
        ルーム内のパスが別ルームや外部へ向くsymlinkは許可しない。
        """

        raw = str(image_path or "").strip()
        if not raw:
            raise TemporaryLocationImageError("image path is empty")

        room_root = self._room_root(room_name)
        requested = Path(raw).expanduser()
        if not requested.is_absolute():
            candidates = [Path.cwd() / requested, room_root / requested]
            requested = next((item for item in candidates if item.exists()), candidates[0])

        if not requested.exists() or not requested.is_file():
            raise TemporaryLocationImageError("image not found")
        if requested.is_symlink():
            raise TemporaryLocationImageError("image symlink is not allowed")

        try:
            resolved = requested.resolve(strict=True)
        except OSError as exc:
            raise TemporaryLocationImageError("image path could not be resolved") from exc

        try:
            lexical_in_room = requested.absolute().relative_to(room_root)
        except ValueError:
            lexical_in_room = None
        try:
            resolved.relative_to(room_root)
            resolved_in_room = True
        except ValueError:
            resolved_in_room = False

        # 同じROOMS_DIRにある別ルームを、PC一時画像として誤って許可しない。
        rooms_root = Path(constants.ROOMS_DIR).expanduser().resolve()
        try:
            resolved.relative_to(rooms_root)
            resolved_in_rooms_root = True
        except ValueError:
            resolved_in_rooms_root = False
        if resolved_in_rooms_root and not resolved_in_room:
            raise TemporaryLocationImageError("image does not belong to this room")

        # room内のsymlink/..経由で別roomや外部へ出る参照を拒否する。
        if lexical_in_room is not None and not resolved_in_room:
            raise TemporaryLocationImageError("image does not belong to this room")
        return resolved

    @staticmethod
    def _verified_image_content(image_path: Path) -> tuple[bytes, str]:
        """読み込んだ同一bytesを検証し、コピーにもそのbytesを使う。"""

        try:
            from PIL import Image

            content = image_path.read_bytes()
            with Image.open(BytesIO(content)) as image:
                image.verify()
                image_format = str(image.format or "").lower()
        except Exception as exc:
            raise TemporaryLocationImageError("asset is not an image") from exc

        extensions = {
            "jpeg": ".jpg",
            "jpg": ".jpg",
            "png": ".png",
            "webp": ".webp",
            "gif": ".gif",
            "bmp": ".bmp",
            "tiff": ".tiff",
            "ico": ".ico",
        }
        extension = extensions.get(image_format)
        if extension:
            return content, extension
        source_extension = image_path.suffix.lower()
        if source_extension in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".ico"}:
            return content, ".jpg" if source_extension == ".jpeg" else source_extension
        raise TemporaryLocationImageError("unsupported image format")

    def _image_signature(self, room_name: str, image_path: Any) -> Optional[tuple[str, str]]:
        raw = str(image_path or "").strip()
        if not raw:
            return None
        source = self._resolve_image_source(room_name, raw)
        content, extension = self._verified_image_content(source)
        digest = hashlib.sha256(content).hexdigest()
        return digest, extension

    def _copy_image_for_saved_location(self, room_name: str, image_path: Any) -> str:
        """画像を同一roomの安定領域へコピーし、保存用絶対pathを返す。"""

        source = self._resolve_image_source(room_name, image_path)
        content, extension = self._verified_image_content(source)
        digest = hashlib.sha256(content).hexdigest()
        room_root = self._room_root(room_name)
        target_dir = room_root / "attachments" / "temporary_locations"
        try:
            target_dir.resolve(strict=False).relative_to(room_root)
        except ValueError as exc:
            raise TemporaryLocationImageError("saved image directory is outside this room") from exc
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            target_dir = target_dir.resolve(strict=True)
            target_dir.relative_to(room_root)
        except (OSError, ValueError) as exc:
            raise TemporaryLocationImageError("saved image directory is outside this room") from exc
        target = target_dir / f"{digest}{extension}"
        if target.is_symlink():
            raise TemporaryLocationImageError("saved image path is a symlink")
        if target.exists():
            if not target.is_file() or target.read_bytes() != content:
                # ハッシュ衝突時だけUUIDを足して既存ファイルを壊さない。
                target = target_dir / f"{digest}-{uuid.uuid4().hex}{extension}"

        if not target.exists():
            temporary = target_dir / f".{target.name}.{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_bytes(content)
                os.replace(temporary, target)
            finally:
                try:
                    if temporary.exists():
                        temporary.unlink()
                except OSError:
                    pass
        return str(target)

    def _snapshot_signature(self, room_name: str, snapshot: dict) -> tuple[Any, ...]:
        image_path = str(snapshot.get("image_path") or "").strip()
        image_signature = self._image_signature(room_name, image_path) if image_path else None
        return (
            str(snapshot.get("scenery_text") or ""),
            str(snapshot.get("source_description") or ""),
            image_signature,
        )

    def _normalize_saved_entry(self, name: str, snapshot: dict) -> dict:
        created_at = str(snapshot.get("created_at") or "").strip() or self._now_text()
        image_path = str(snapshot.get("image_path") or "").strip()
        return {
            "name": name,
            "location_name": name,
            "scenery_text": str(snapshot.get("scenery_text") or ""),
            "source_description": str(snapshot.get("source_description") or ""),
            "image_path": image_path,
            "created_at": created_at,
        }

    # --- 公開API ---

    def is_active(self, room_name: str) -> bool:
        """一時的現在地がアクティブかどうかを返す"""
        data = self._load_data(room_name)
        return data.get("active", False)

    def set_active(self, room_name: str, active: bool) -> bool:
        """一時的現在地のアクティブ状態を切り替える"""
        with self._io_lock:
            data = self._load_data(room_name)
            if active and not str(data.get("current", {}).get("scenery_text") or "").strip():
                raise ValueError("情景テキストがない状態では一時的現在地を有効化できません")
            data["active"] = active
            saved = self._save_data(room_name, data)
        print(f"--- [TempLocation] アクティブ状態を {'ON' if active else 'OFF'} に変更 (room: {room_name}) ---")
        return saved

    def get_current_data(self, room_name: str) -> dict:
        """現在の一時的現在地データを返す"""
        data = self._load_data(room_name)
        current = self._as_mapping(data.get("current"))
        # 旧JSONにはlocation_nameがないため、読み取り時は空名を補う。
        result = dict(current)
        result["location_name"] = str(current.get("location_name") or "")
        return result

    def update_current(
        self,
        room_name: str,
        scenery_text: str,
        source_description: str = "",
        image_path: str = "",
        location_name: Optional[str] = None,
    ) -> bool:
        """現在の一時的現在地データを更新する"""
        with self._io_lock:
            data = self._load_data(room_name)
            previous = self._as_mapping(data.get("current"))
            next_location_name = (
                str(previous.get("location_name") or "")
                if location_name is None
                else str(location_name)
            )
            data["current"] = {
                "scenery_text": scenery_text,
                "source_description": source_description,
                "image_path": image_path,
                "location_name": next_location_name,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            saved = self._save_data(room_name, data)
        print(f"--- [TempLocation] 現在地データを更新 (room: {room_name}, text_len: {len(scenery_text)}) ---")
        return saved

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
        saved = data.get("saved_locations", [])
        if not isinstance(saved, list):
            return []
        return [loc.get("name", "") for loc in saved if isinstance(loc, dict)]

    def save_location(self, room_name: str, name: str) -> bool:
        """
        現在の一時的現在地データを名前をつけて保存する。
        同名の場所が存在する場合は上書きする。
        
        Returns:
            成功した場合 True
        """
        with self._io_lock:
            data = self._load_data(room_name)
            current = self._as_mapping(data.get("current"))

            if not current.get("scenery_text"):
                print("--- [TempLocation Warning] 保存する情景データがありません ---")
                return False
            snapshot = dict(current)
            # 既存UIの保存は従来どおり同名上書き。
            try:
                self.save_location_data(room_name, name, snapshot, overwrite=True)
            except (TemporaryLocationImageError, OSError, ValueError) as exc:
                print(f"--- [TempLocation Warning] 場所の画像を保存できません: {exc} ---")
                return False
        print(f"--- [TempLocation] 場所を保存: '{name}' (room: {room_name}) ---")
        return True

    def save_location_data(
        self,
        room_name: str,
        name: str,
        snapshot: Dict[str, Any],
        *,
        overwrite: bool = False,
    ) -> dict:
        """指定したスナップショットを保存する。

        ``name`` はJSON上の文字列キーであり、ファイル名には使わない。既存名と
        内容が同じ再送は冪等に既存エントリを返し、内容が違う場合だけ
        ``overwrite=True`` を要求する。current/activeは読み取るだけで変更しない。
        """

        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be an object")
        name = str(name or "").strip()
        if not name:
            raise ValueError("location name is required")
        if not str(snapshot.get("scenery_text") or "").strip():
            raise ValueError("scenery_text is required")

        with self._io_lock:
            data = self._load_data(room_name)
            saved = data.get("saved_locations", [])
            if not isinstance(saved, list):
                # 壊れた既存データを、保存操作の副作用で空配列へ置換しない。
                raise ValueError("saved_locations must be a list")
            existing_index = next(
                (index for index, item in enumerate(saved)
                 if isinstance(item, dict) and str(item.get("name") or "") == name),
                None,
            )
            if existing_index is not None:
                existing = self._normalize_saved_entry(name, self._as_mapping(saved[existing_index]))
                requested_signature = self._snapshot_signature(room_name, snapshot)
                existing_signature_error: Optional[Exception] = None
                try:
                    existing_signature = self._snapshot_signature(room_name, existing)
                except (TemporaryLocationImageError, OSError) as exc:
                    # 欠損した旧画像は明示overwriteで修復できるようにする。
                    existing_signature = None
                    existing_signature_error = exc
                if existing_signature_error is None and requested_signature == existing_signature:
                    # 旧保存が元写真の絶対pathを指していた場合も、この再送を機に
                    # 安定コピーへ更新する。ただし配列件数は増やさない。
                    if requested_signature[2] is not None:
                        stable_path = self._copy_image_for_saved_location(room_name, snapshot.get("image_path"))
                        if existing.get("image_path") != stable_path:
                            existing["image_path"] = stable_path
                            saved[existing_index] = existing
                            data["saved_locations"] = saved
                            if not self._save_data(room_name, data):
                                raise IOError("一時的現在地データの保存に失敗しました")
                    return dict(existing)
                if not overwrite:
                    if existing_signature_error is not None:
                        raise TemporaryLocationConflictError(
                            f"saved location requires overwrite because its image is unavailable: {name}"
                        ) from existing_signature_error
                    raise TemporaryLocationConflictError(f"saved location already exists: {name}")

            normalized = self._normalize_saved_entry(name, snapshot)
            if normalized["image_path"]:
                normalized["image_path"] = self._copy_image_for_saved_location(
                    room_name, normalized["image_path"]
                )

            if existing_index is None:
                saved.append(normalized)
            else:
                # 壊れた旧データに同名重複があっても、新API保存後は1件に戻す。
                replaced = False
                deduplicated = []
                for item in saved:
                    if isinstance(item, dict) and str(item.get("name") or "") == name:
                        if not replaced:
                            deduplicated.append(normalized)
                            replaced = True
                        continue
                    deduplicated.append(item)
                saved = deduplicated
            data["saved_locations"] = saved
            if not self._save_data(room_name, data):
                raise IOError("一時的現在地データの保存に失敗しました")
            return dict(normalized)

    def get_saved_location(self, room_name: str, name: str) -> Optional[dict]:
        """保存済み地点を読み取る。読み取りでcurrent/activeは変更しない。"""

        requested_name = str(name or "")
        data = self._load_data(room_name)
        saved = data.get("saved_locations", [])
        if not isinstance(saved, list):
            return None
        for location in saved:
            if isinstance(location, dict) and str(location.get("name") or "") == requested_name:
                normalized = self._normalize_saved_entry(requested_name, location)
                # 旧保存はnameをlocation_nameとして扱う。
                if not str(location.get("location_name") or "").strip():
                    normalized["location_name"] = requested_name
                else:
                    normalized["location_name"] = str(location.get("location_name"))
                return normalized
        return None

    def load_location(self, room_name: str, name: str) -> bool:
        """
        保存済みの場所データを現在地として読み込む。
        
        Returns:
            成功した場合 True
        """
        with self._io_lock:
            data = self._load_data(room_name)
            saved = data.get("saved_locations", [])

            for loc in saved:
                if loc.get("name") == name:
                    data["current"] = {
                        "scenery_text": loc.get("scenery_text", ""),
                        "source_description": loc.get("source_description", ""),
                        "image_path": loc.get("image_path", ""),
                        "location_name": loc.get("location_name") or loc.get("name", ""),
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
        with self._io_lock:
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
