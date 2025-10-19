# config_manager.py (v7: The True Final Covenant - 真・最終版)

import json
import os
from typing import Any, List, Dict

import constants

# --- グローバル変数 ---
CONFIG_GLOBAL = {}
GEMINI_API_KEYS = {}
AVAILABLE_MODELS_GLOBAL = []
DEFAULT_MODEL_GLOBAL = "gemini-2.5-pro"
NOTIFICATION_SERVICE_GLOBAL = "discord"
NOTIFICATION_WEBHOOK_URL_GLOBAL = None
PUSHOVER_CONFIG = {}

SUPPORTED_VOICES = {
    "zephyr": "Zephyr (明るい)", "puck": "Puck (アップビート)", "charon": "Charon (情報が豊富)",
    "kore": "Kore (しっかりした)", "fenrir": "Fenrir (興奮した)", "leda": "Leda (若々しい)",
    "orus": "Orus (しっかりした)", "aoede": "Aoede (軽快)", "callirrhoe": "Callirrhoe (のんびりした)",
    "autonoe": "Autonoe (明るい)", "enceladus": "Enceladus (息遣いの多い)", "iapetus": "Iapetus (クリア)",
    "umbriel": "Umbriel (のんびりした)", "algieba": "Algieba (スムーズ)", "despina": "Despina (スムーズ)",
    "erinome": "Erinome (クリア)", "algenib": "Algenib (しわがれた)", "rasalgethi": "Rasalgethi (情報が豊富)",
    "laomedeia": "Laomedeia (アップビート)", "achernar": "Achernar (柔らかい)", "alnilam": "Alnilam (しっかりした)",
    "schedar": "Schedar (均一)", "gacrux": "Gacrux (成熟したt)", "pulcherrima": "Pulcherrima (前向き)",
    "achird": "Achird (フレンドリー)", "zubenelgenubi": "Zubenelgenubi (カジュアル)",
    "vindemiatrix": "Vindemiatrix (優しい)", "sadachbia": "Sadachbia (生き生きした)",
    "sadaltager": "Sadaltager (知識が豊富)", "sulafat": "Sulafat (温かい)",
}

# --- 起動時の初期値を保持するグローバル変数 ---
initial_api_key_name_global = "default"
initial_room_global = "Default"
initial_model_global = DEFAULT_MODEL_GLOBAL
initial_send_thoughts_to_api_global = True
initial_api_history_limit_option_global = constants.DEFAULT_API_HISTORY_LIMIT_OPTION
initial_alarm_api_history_turns_global = constants.DEFAULT_ALARM_API_HISTORY_TURNS
initial_streaming_speed_global = 0.01


# --- 内部ヘルパー関数 ---
def _load_config_file() -> dict:
    if os.path.exists(constants.CONFIG_FILE):
        try:
            with open(constants.CONFIG_FILE, "r", encoding="utf-8") as f:
                content = f.read()
                if not content.strip(): return {}
                return json.loads(content)
        except (json.JSONDecodeError, IOError): return {}
    return {}

def _save_config_file(config_data: dict):
    """
    設定データを一時ファイルに書き込んでからリネームすることで、
    書き込み中のクラッシュによるファイル破損を防ぐ、アトミックな保存処理。
    """
    temp_file_path = constants.CONFIG_FILE + ".tmp"
    try:
        with open(temp_file_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
        # 書き込みが成功したら、一時ファイルを本番ファイルにリネーム（アトミック操作）
        os.replace(temp_file_path, constants.CONFIG_FILE)
    except Exception as e:
        print(f"'{constants.CONFIG_FILE}' 保存エラー: {e}")
        # エラーが発生した場合、一時ファイルを削除する
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)

def save_config(key: str, value: Any):
    """
    単一のキーと値をconfig.jsonに安全に保存する。
    """
    config = _load_config_file()
    config[key] = value
    _save_config_file(config)


# --- 公開APIキー管理関数 ---
def add_or_update_gemini_key(key_name: str, key_value: str):
    global GEMINI_API_KEYS
    config = _load_config_file()
    if "gemini_api_keys" not in config or not isinstance(config.get("gemini_api_keys"), dict):
        config["gemini_api_keys"] = {}

    existing_keys = config["gemini_api_keys"]
    if len(existing_keys) == 1 and "your_key_name" in existing_keys:
        del existing_keys["your_key_name"]

    config["gemini_api_keys"][key_name] = key_value
    _save_config_file(config)
    GEMINI_API_KEYS = config["gemini_api_keys"]

def delete_gemini_key(key_name: str):
    global GEMINI_API_KEYS
    config = _load_config_file()
    if "gemini_api_keys" in config and isinstance(config.get("gemini_api_keys"), dict) and key_name in config["gemini_api_keys"]:
        del config["gemini_api_keys"][key_name]

        if not config["gemini_api_keys"]:
            config["gemini_api_keys"] = {"your_key_name": "YOUR_API_KEY_HERE"}

        if config.get("last_api_key_name") == key_name:
            config["last_api_key_name"] = None
        _save_config_file(config)
        GEMINI_API_KEYS = config.get("gemini_api_keys", {})

def update_pushover_config(user_key: str, app_token: str):
    config = _load_config_file()
    config["pushover_user_key"] = user_key
    config["pushover_app_token"] = app_token
    _save_config_file(config)


# --- メインの読み込み関数 (真・最終版) ---
def load_config():
    global CONFIG_GLOBAL, GEMINI_API_KEYS, initial_api_key_name_global, initial_room_global, initial_model_global
    global initial_send_thoughts_to_api_global, initial_api_history_limit_option_global, initial_alarm_api_history_turns_global
    global AVAILABLE_MODELS_GLOBAL, DEFAULT_MODEL_GLOBAL, initial_streaming_speed_global
    global NOTIFICATION_SERVICE_GLOBAL, NOTIFICATION_WEBHOOK_URL_GLOBAL, PUSHOVER_CONFIG

    # ステップ1：全てのキーを含む、理想的なデフォルト設定を定義

    # ▼▼▼【ここから下のブロックをまるごと追加】▼▼▼
    # 新しいデフォルトテーマ「Nexus Ark」のパラメータ定義
    # 新しいデフォルトテーマ「Nexus Ark」のパラメータ定義
    nexus_ark_theme_params = {
        # gr.themes.Default の引数
        "primary_hue": "neutral",
        "secondary_hue": "neutral",  # 新しいテーマに合わせて neutral に変更
        "neutral_hue": "neutral",
        "font": ['Source Sans Pro', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        "font_mono": ['IBM Plex Mono', 'ui-monospace', 'Consolas', 'monospace'],
        # .set() で設定する引数
        "body_background_fill": '*neutral_200',
        "body_background_fill_dark": '*neutral_900',
        "body_text_color": '*neutral_600',
        "body_text_color_dark": '*neutral_300',
        "background_fill_primary": '*neutral_100',
        "background_fill_secondary": '*neutral_100',
        "background_fill_secondary_dark": '*neutral_800',
        "border_color_primary": '*neutral_400',
        "block_background_fill": '*neutral_100',
        "block_label_text_size": '*text_xxs',
        "section_header_text_weight": '100',
        "chatbot_text_size": '*text_md',
        "button_large_padding": '*spacing_md',
        "button_large_radius": '*radius_xs',
        "button_large_text_size": '*text_md',
        "button_large_text_weight": '400',
        "button_small_radius": '*radius_xs',
        "button_medium_radius": '*radius_xs',
        "button_medium_text_weight": '300',
        "button_cancel_background_fill": '#eb4d63',
        "button_cancel_background_fill_dark": '#901124',
        "button_cancel_background_fill_hover": '#fe7385',
        "button_cancel_background_fill_hover_dark": '#b8152d',
        "button_primary_background_fill": '*primary_400',
        "button_primary_background_fill_dark": '*primary_700',
        "button_primary_background_fill_hover": '*primary_500',
        "button_primary_background_fill_hover_dark": '*primary_500',
        "button_primary_border_color_dark": '*primary_50',
        "button_secondary_background_fill": '*neutral_300',
        "button_secondary_background_fill_hover": '*neutral_400',
        "button_secondary_background_fill_hover_dark": '*neutral_500',
        "block_title_text_size": '*text_sm',
        "section_header_text_size": '*text_sm',
        "checkbox_label_text_size": '*text_sm'
    }
    # ▲▲▲【追加ここまで】▲▲▲

    # ▼▼▼ 既存の default_config の定義を、これで完全に置き換え ▼▼▼
    default_config = {
        "gemini_api_keys": {"your_key_name": "YOUR_API_KEY_HERE"},
        "available_models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
        "default_model": "gemini-2.5-pro",
        "last_room": "Default",
        "last_model": "gemini-2.5-pro",
        "last_api_key_name": None,
        "last_send_thoughts_to_api": True,
        "last_api_history_limit_option": constants.DEFAULT_API_HISTORY_LIMIT_OPTION,
        "alarm_api_history_turns": constants.DEFAULT_ALARM_API_HISTORY_TURNS,
        "notification_service": "discord",
        "notification_webhook_url": None,
        "pushover_app_token": "",
        "pushover_user_key": "",
        "log_archive_threshold_mb": 10,
        "log_keep_size_mb": 5,
        "backup_rotation_count": 10, # ← この行を追加
        "theme_settings": {
            "active_theme": "Nexus Ark", # デフォルトテーマを "Nexus Ark" に変更
            "custom_themes": {
                "Nexus Ark": nexus_ark_theme_params # 新テーマをカスタムテーマとして追加
            }
        }
    }
    # ▲▲▲【置き換えここまで】▲▲▲

    # ステップ2：ユーザーの設定ファイルを読み込む
    user_config = _load_config_file()

    # ▼▼▼【ここから下のブロックをまるごと追加】▼▼▼
    # ステップ3：【賢いマージ】テーマ設定をディープマージする
    default_theme_settings = default_config["theme_settings"]
    user_theme_settings = user_config.get("theme_settings", {})

    # ユーザーのカスタムテーマとデフォルトのカスタムテーマを結合（ユーザー設定優先）
    merged_custom_themes = default_theme_settings["custom_themes"].copy()
    merged_custom_themes.update(user_theme_settings.get("custom_themes", {}))

    # 最終的なテーマ設定を決定（アクティブテーマはユーザー設定を優先）
    final_theme_settings = {
        "active_theme": user_theme_settings.get("active_theme", default_theme_settings["active_theme"]),
        "custom_themes": merged_custom_themes
    }
    # ▲▲▲【追加ここまで】▲▲▲

    # ▼▼▼ 既存の "ステップ3：【賢いマージ】available_modelsを統合する" ブロックを、
    # "ステップ4" に変更し、その下のロジックをこれで置き換える ▼▼▼

    # ステップ4：【賢いマージ】available_modelsを統合する
    default_models_set = set(default_config["available_models"])
    user_models_set = set(user_config.get("available_models", []))
    merged_models = sorted(list(default_models_set | user_models_set))

    # ステップ5：ユーザー設定を優先しつつ、不足キーを補完
    config = default_config.copy()
    config.update(user_config)
    # 統合したモデルリストとテーマ設定で、最終的な設定を上書き
    config["available_models"] = merged_models
    config["theme_settings"] = final_theme_settings

    # ステップ6：不要なキーをクリーンアップ
    keys_to_remove = ["memos_config", "api_keys", "default_api_key_name"]
    config_keys_changed = False
    for key in keys_to_remove:
        if key in config:
            config.pop(key)
            config_keys_changed = True

    # ステップ7：キー構成の変化、またはモデルリスト/テーマ設定の変化があった場合のみファイルを更新
    if (config_keys_changed or
        set(user_config.get("available_models", [])) != set(config["available_models"]) or
        user_config.get("theme_settings") != config["theme_settings"] or # テーマ設定の変更もチェック
        not os.path.exists(constants.CONFIG_FILE)):
        print("--- [情報] 設定ファイルに新しいキーやモデル、テーマを追加、または不要なキーを削除しました。config.jsonを更新します。 ---")
        _save_config_file(config)

    # ステップ8：メモリ上の最終的な設定を、グローバル変数に反映
    CONFIG_GLOBAL = config.copy()

    GEMINI_API_KEYS = config.get("gemini_api_keys", {})
    if not GEMINI_API_KEYS:
        GEMINI_API_KEYS = {"your_key_name": "YOUR_API_KEY_HERE"}

    AVAILABLE_MODELS_GLOBAL = config.get("available_models")
    DEFAULT_MODEL_GLOBAL = config.get("default_model")
    initial_room_global = config.get("last_room")
    initial_model_global = config.get("last_model")
    initial_send_thoughts_to_api_global = config.get("last_send_thoughts_to_api")
    initial_api_history_limit_option_global = config.get("last_api_history_limit_option")
    initial_alarm_api_history_turns_global = config.get("alarm_api_history_turns")
    initial_streaming_speed_global = config.get("last_streaming_speed")
    NOTIFICATION_SERVICE_GLOBAL = config.get("notification_service")
    NOTIFICATION_WEBHOOK_URL_GLOBAL = config.get("notification_webhook_url")
    PUSHOVER_CONFIG = {
        "user_key": config.get("pushover_user_key"),
        "app_token": config.get("pushover_app_token")
    }

    valid_api_keys = [k for k, v in GEMINI_API_KEYS.items() if isinstance(v, str) and v and v != "YOUR_API_KEY_HERE"]
    last_key = config.get("last_api_key_name")
    if last_key and last_key in valid_api_keys:
        initial_api_key_name_global = last_key
    elif valid_api_keys:
        initial_api_key_name_global = valid_api_keys[0]
    else:
        initial_api_key_name_global = list(GEMINI_API_KEYS.keys())[0] if GEMINI_API_KEYS else "your_key_name"


def get_effective_settings(room_name: str, **kwargs) -> dict:
    """
    ルームのファイル設定と、UIからのリアルタイムな設定（kwargs）をマージして、
    最終的に適用される設定値を返す。
    """
    effective_settings = {
        "model_name": DEFAULT_MODEL_GLOBAL, "voice_id": "iapetus", "voice_style_prompt": "",
        "add_timestamp": True, "send_thoughts": initial_send_thoughts_to_api_global,
        "send_notepad": True, "use_common_prompt": True,
        "send_core_memory": True,
        "enable_scenery_system": True, # この行を追加
        "send_scenery": True,
        "send_current_time": True,
        "auto_memory_enabled": False,
        "enable_typewriter_effect": True,
        "streaming_speed": 0.01,
        "temperature": 0.8, "top_p": 0.95,
        "safety_block_threshold_harassment": "BLOCK_ONLY_HIGH",
        "safety_block_threshold_hate_speech": "BLOCK_ONLY_HIGH",
        "safety_block_threshold_sexually_explicit": "BLOCK_ONLY_HIGH",
        "safety_block_threshold_dangerous_content": "BLOCK_ONLY_HIGH"
    }
    room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
    if os.path.exists(room_config_path):
        try:
            with open(room_config_path, "r", encoding="utf-8") as f:
                room_config = json.load(f)
            override_settings = room_config.get("override_settings", {})
            for k, v in override_settings.items():
                if v is not None and k != "model_name":
                    effective_settings[k] = v
        except Exception as e:
            print(f"ルーム設定ファイル '{room_config_path}' の読み込みエラー: {e}")
    for key, value in kwargs.items():
        if key not in ["global_model_from_ui"] and value is not None:
            effective_settings[key] = value
    global_model_from_ui = kwargs.get("global_model_from_ui")
    final_model_name = global_model_from_ui or DEFAULT_MODEL_GLOBAL
    effective_settings["model_name"] = final_model_name
    if not effective_settings.get("model_name"):
        effective_settings["model_name"] = DEFAULT_MODEL_GLOBAL
    return effective_settings

def load_redaction_rules() -> List[Dict[str, str]]:
    """redaction_rules.jsonから置換ルールを読み込む。"""
    if os.path.exists(constants.REDACTION_RULES_FILE):
        try:
            with open(constants.REDACTION_RULES_FILE, "r", encoding="utf-8") as f:
                content = f.read()
                if not content.strip(): return []
                rules = json.loads(content)
                if isinstance(rules, list) and all(isinstance(r, dict) and "find" in r and "replace" in r for r in rules):
                    return rules
        except (json.JSONDecodeError, IOError):
            print(f"警告: {constants.REDACTION_RULES_FILE} の読み込みに失敗しました。")
    return []

def save_redaction_rules(rules: List[Dict[str, str]]):
    """置換ルールをredaction_rules.jsonに保存する。"""
    try:
        with open(constants.REDACTION_RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(rules, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"エラー: {constants.REDACTION_RULES_FILE} の保存に失敗しました: {e}")

def save_theme_settings(active_theme: str, custom_themes: Dict):
    """
    アクティブなテーマ名とカスタムテーマの定義をconfig.jsonに保存する。
    """
    config = _load_config_file()
    if "theme_settings" not in config:
        config["theme_settings"] = {}
    config["theme_settings"]["active_theme"] = active_theme
    config["theme_settings"]["custom_themes"] = custom_themes
    _save_config_file(config)

from typing import Optional

# ▼▼▼【この関数をファイルの末尾にまるごと追加】▼▼▼
def get_latest_api_key_name_from_config() -> Optional[str]:
    """
    config.jsonを直接読み込み、最後に選択された有効なAPIキー名を返す。
    UIの状態に依存しないため、バックグラウンドスレッドから安全に呼び出せる。
    """
    config = _load_config_file()
    last_key_name = config.get("last_api_key_name")

    # 有効な（値が設定されている）APIキーのリストを取得
    api_keys_dict = config.get("gemini_api_keys", {})
    valid_keys = [
        k for k, v in api_keys_dict.items()
        if v and isinstance(v, str) and not v.startswith("YOUR_API_KEY")
    ]

    # 最後に使ったキーが今も有効なら、それを返す
    if last_key_name and last_key_name in valid_keys:
        return last_key_name

    # そうでなければ、有効なキーリストの最初のものを返す
    if valid_keys:
        return valid_keys[0]

    # 有効なキーが一つもなければ、Noneを返す
    return None
# ▲▲▲【追加はここまで】▲▲▲


def has_valid_api_key() -> bool:
    """
    設定ファイルに、有効な（プレースホルダではない）Gemini APIキーが一つでも存在するかどうかを返す。
    """
    if not GEMINI_API_KEYS:
        return False
    for key, value in GEMINI_API_KEYS.items():
        if value and isinstance(value, str) and value != "YOUR_API_KEY_HERE":
            return True
    return False