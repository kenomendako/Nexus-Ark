"""ルーム内の感情項目の定義。変更APIはユーザー用で、ペルソナのツールには公開しない。"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import uuid

import constants
from file_lock_utils import safe_json_read, safe_json_write
from emotion_decay_policy import EmotionDecayPolicy


_UNSET = object()


EMOTION_LABELS = {
    "joy": "喜び", "contentment": "満足・穏やかさ", "protective": "庇護欲",
    "anxious": "不安", "sadness": "悲しみ", "anger": "怒り", "neutral": "平常",
}


class EmotionDefinitionManager:
    CONFIG_KEY = "emotion_definitions"
    MAX_NAME_LENGTH = 64
    MAX_DESCRIPTION_LENGTH = 200
    CUSTOM_ID_PATTERN = re.compile(r"custom_[0-9a-f]{32}\Z")

    def __init__(self, room_name: str):
        if (not isinstance(room_name, str) or not room_name or room_name in {".", ".."}
                or "/" in room_name or "\\" in room_name or "\x00" in room_name
                or Path(room_name).name != room_name):
            raise ValueError("対象のルーム名を指定してください。")
        self.room_name = room_name
        self.config_file = Path(constants.ROOMS_DIR) / room_name / "room_config.json"

    @classmethod
    def is_supported_id(cls, category):
        return isinstance(category, str) and (category in EMOTION_LABELS and category != "neutral"
                                              or cls.CUSTOM_ID_PATTERN.fullmatch(category) is not None)

    @classmethod
    def _text(cls, value, *, name=False):
        maximum = cls.MAX_NAME_LENGTH if name else cls.MAX_DESCRIPTION_LENGTH
        if not isinstance(value, str) or len(value) > maximum:
            raise ValueError(f"{'名前' if name else '説明'}は{maximum}文字以内で指定してください。")
        value = value.strip()
        if name and (not value or len(value.splitlines()) != 1
                     or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("名前は空白や改行だけにせず、一行で指定してください。")
        return value

    @staticmethod
    def _revision(raw):
        # nonceを含めた保存内容の指紋。外部編集でnonceが据え置かれても古い表示を照合できる。
        return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def _stored(cls, config):
        if not isinstance(config, dict):
            raise ValueError("ルーム設定の形式が不正です。自動で置き換えません。")
        overrides = config.get("override_settings", {})
        if not isinstance(overrides, dict):
            raise ValueError("ルーム個別設定の形式が不正です。")
        stored = overrides.get(cls.CONFIG_KEY, {})
        if not isinstance(stored, dict):
            raise ValueError("感情項目の定義形式が不正です。自動で置き換えません。")
        return stored

    @classmethod
    def _view(cls, category, raw):
        builtin = category in EMOTION_LABELS and category != "neutral"
        try:
            if not isinstance(raw, dict):
                raise ValueError("不正な定義")
            policy = (EmotionDecayPolicy.validate(raw["decay_policy"]) if "decay_policy" in raw
                      else EmotionDecayPolicy.default())
            policy_fields = {"decay_policy": policy, "decay_policy_valid": True}
        except ValueError:
            # 時間設定の破損で、以前の有効な申告まで無効化しない。
            policy_fields = {"decay_policy": None, "decay_policy_valid": False}
        try:
            if not cls.is_supported_id(category) or not isinstance(raw, dict):
                raise ValueError("未対応の項目")
            name = cls._text(raw.get("name", EMOTION_LABELS.get(category, "")), name=True)
            description = cls._text(raw.get("description", ""))
            enabled = raw.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError("使用状態はbooleanで指定してください。")
            return {"id": category, "name": name, "description": description, "enabled": enabled,
                    "kind": "builtin" if builtin else "custom", "valid": True,
                    "revision": cls._revision(raw), **policy_fields}
        except ValueError:
            return {"id": category, "name": EMOTION_LABELS.get(category, category), "description": "",
                    "enabled": False, "kind": "builtin" if builtin else "custom", "valid": False,
                    "revision": cls._revision(raw), **policy_fields}

    @classmethod
    def _definitions(cls, config):
        stored = cls._stored(config)
        definitions = {}
        for category, label in EMOTION_LABELS.items():
            if category != "neutral":
                definitions[category] = cls._view(category, stored.get(category, {"name": label}))
        for category, raw in stored.items():
            if category not in definitions:
                definitions[category] = cls._view(category, raw)
        return definitions

    def get_definitions(self):
        """欠けた定義はメモリ上の既定値。読み取りでファイル・項目を作らない。"""
        default = [] if self.config_file.exists() else {}
        return self._definitions(safe_json_read(str(self.config_file), default=default))

    @contextmanager
    def locked_definitions(self):
        """定義を確認して状態を書き終えるまで、設定側の更新を待たせる。"""
        if not self.config_file.exists():
            yield self.get_definitions()
            return
        from room_manager import room_config_write_guard
        with room_config_write_guard(self.room_name):
            yield self.get_definitions()

    @classmethod
    def _check_duplicate(cls, definitions, category, name, enabled):
        if not enabled:
            return
        normalized = unicodedata.normalize("NFKC", name).casefold()
        for other, definition in definitions.items():
            if (other != category and definition["valid"] and definition["enabled"]
                    and unicodedata.normalize("NFKC", definition["name"]).casefold() == normalized):
                raise ValueError("同じ名前の使用中の項目があります。既存項目を確認してください。")

    def _update(self, mutate):
        if not self.config_file.is_file():
            raise ValueError("既存ルームの設定が必要です。感情項目の操作でルームは作成しません。")
        from room_manager import room_config_write_guard, create_backup
        with room_config_write_guard(self.room_name):
            # ロック取得待ちに削除されても、新しいルームとして再作成しない。
            if not self.config_file.is_file():
                raise ValueError("ルーム設定が変わっています。再読してください。")
            config = safe_json_read(str(self.config_file), default=[])
            definitions = self._definitions(config)
            before = deepcopy(config)
            result = mutate(config, definitions)
            if config == before:
                return result
            config["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            config["updated_at"] = datetime.now(timezone.utc).isoformat()
            try:
                create_backup(self.room_name, "room_config", new_data=config)
            except Exception as exc:
                print(f"[EmotionDefinitionManager] 設定バックアップ失敗: {exc}")
            safe_json_write(str(self.config_file), config)
            return result

    def add(self, name: str, description: str = "", *, decay_policy=_UNSET):
        """ユーザー用。意味が別物なら、このAPIで新IDを作る。感情の状態は作らない。"""
        name, description = self._text(name, name=True), self._text(description)
        if decay_policy is not _UNSET:
            decay_policy = EmotionDecayPolicy.validate(decay_policy)
        category = "custom_" + uuid.uuid4().hex
        def mutate(config, definitions):
            self._check_duplicate(definitions, category, name, True)
            raw = {"name": name, "description": description, "enabled": True,
                   "revision": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat()}
            if decay_policy is not _UNSET:
                raw["decay_policy"] = deepcopy(decay_policy)
            config.setdefault("override_settings", {}).setdefault(self.CONFIG_KEY, {})[category] = raw
            return self._view(category, raw)
        return self._update(mutate)

    def update(self, category: str, *, expected_revision: str, name=None, description=None, enabled=None,
               decay_policy=_UNSET):
        """ユーザー用の差分更新。説明の整形・改名と、別の意味の新規追加を区別する。"""
        if not self.is_supported_id(category):
            raise ValueError("既存の感情項目IDを指定してください。")
        if name is not None:
            name = self._text(name, name=True)
        if description is not None:
            description = self._text(description)
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError("使用状態はbooleanで指定してください。")
        if decay_policy is not _UNSET:
            decay_policy = EmotionDecayPolicy.validate(decay_policy)
        def mutate(config, definitions):
            current = definitions.get(category)
            if current is None or expected_revision != current["revision"]:
                raise ValueError("感情項目が更新されています。一覧で最新revisionを確認してください。")
            stored = self._stored(config)
            raw = deepcopy(stored.get(category, {"name": EMOTION_LABELS.get(category, "")}))
            if not isinstance(raw, dict):
                raise ValueError("保存された項目の形式が不正です。自動で置き換えません。")
            for key, value in (("name", name), ("description", description), ("enabled", enabled)):
                if value is not None:
                    raw[key] = value
            if decay_policy is not _UNSET:
                raw["decay_policy"] = deepcopy(decay_policy)
            view = self._view(category, raw)
            if not view["valid"]:
                raise ValueError("項目の名前・説明・使用状態を確認してください。")
            self._check_duplicate(definitions, category, view["name"], view["enabled"])
            original = stored.get(category, {"name": EMOTION_LABELS.get(category, "")})
            if raw == original:
                return current
            raw["revision"] = uuid.uuid4().hex
            raw["updated_at"] = datetime.now(timezone.utc).isoformat()
            config.setdefault("override_settings", {}).setdefault(self.CONFIG_KEY, {})[category] = raw
            return self._view(category, raw)
        return self._update(mutate)
