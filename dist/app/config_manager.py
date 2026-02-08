# config_manager.py (v7: The True Final Covenant - 真・最終版)

import json
import os
import time
from typing import Any, List, Dict, Tuple, Optional
import time 
import shutil 
import datetime 

import constants

# --- グローバル変数 ---
CONFIG_GLOBAL = {}
GEMINI_API_KEYS = {}
GEMINI_KEY_STATES = {} # {key_name: {'exhausted': bool, 'exhausted_at': timestamp}}
KEY_STATES_FILE = ".gemini_key_states.json"
TAVILY_API_KEY = ""  # Tavily検索用APIキー
AVAILABLE_MODELS_GLOBAL = []
DEFAULT_MODEL_GLOBAL = "gemini-3-flash-preview"
NOTIFICATION_SERVICE_GLOBAL = "discord"
NOTIFICATION_WEBHOOK_URL_GLOBAL = None
PUSHOVER_CONFIG = {}
ZHIPU_API_KEY = ""    # [Phase 3] Zhipu AI (GLM-4) 用APIキー
GROQ_API_KEY = ""     # [Phase 3b] Groq用APIキー
MOONSHOT_API_KEY = "" # [Phase 3d] Moonshot AI (Kimi) 用APIキー
LOCAL_MODEL_PATH = "" # [Phase 3c] ローカルLLM (llama.cpp) 用GGUFモデルパス
AVAILABLE_ZHIPU_MODELS = constants.ZHIPU_MODELS


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


# --- [v8] 自己修復機能付きコンフィグ管理 ---

def _create_config_backup():
    """config.jsonのバックアップを作成し、ローテーションする。"""
    backup_dir = os.path.join("backups", "config")
    os.makedirs(backup_dir, exist_ok=True)

    if not os.path.exists(constants.CONFIG_FILE):
        return # バックアップ対象がない場合は何もしない

    try:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"config_{timestamp}.json.bak"
        backup_path = os.path.join(backup_dir, backup_filename)
        shutil.copy2(constants.CONFIG_FILE, backup_path)

        # ローテーション処理
        rotation_count = CONFIG_GLOBAL.get("backup_rotation_count", 10)
        existing_backups = sorted(
            [f for f in os.listdir(backup_dir) if f.endswith(".bak")],
            key=lambda f: os.path.getmtime(os.path.join(backup_dir, f))
        )
        if len(existing_backups) > rotation_count:
            for f_del in existing_backups[:len(existing_backups) - rotation_count]:
                os.remove(os.path.join(backup_dir, f_del))

    except Exception as e:
        print(f"警告: config.jsonのバックアップ作成に失敗しました: {e}")

def _restore_from_backup() -> bool:
    """最も新しいバックアップからconfig.jsonを復元する。"""
    backup_dir = os.path.join("backups", "config")
    if not os.path.isdir(backup_dir):
        return False

    try:
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.endswith(".bak")],
            key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)),
            reverse=True # 新しいものが先頭に来るように
        )
        if not backups:
            return False

        latest_backup = os.path.join(backup_dir, backups[0])
        print(f"--- [自己修復] 破損したconfig.jsonをバックアップ '{backups[0]}' から復元します ---")
        shutil.copy2(latest_backup, constants.CONFIG_FILE)
        return True

    except Exception as e:
        print(f"!!! エラー: バックアップからの復元に失敗しました: {e}")
        return False

def load_gemini_key_states():
    """APIキーの枯渇状態をファイルから読み込む。"""
    global GEMINI_KEY_STATES
    if os.path.exists(KEY_STATES_FILE):
        try:
            with open(KEY_STATES_FILE, "r", encoding="utf-8") as f:
                GEMINI_KEY_STATES = json.load(f)
                print(f"--- [API Key Rotation] Loaded {len(GEMINI_KEY_STATES)} key states from {KEY_STATES_FILE} ---")
        except Exception as e:
            print(f"警告: {KEY_STATES_FILE} の読み込みに失敗しました: {e}")
            GEMINI_KEY_STATES = {}
    else:
        GEMINI_KEY_STATES = {}

def save_gemini_key_states():
    """APIキーの枯渇状態をファイルに保存する。"""
    try:
        with open(KEY_STATES_FILE, "w", encoding="utf-8") as f:
            json.dump(GEMINI_KEY_STATES, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"警告: {KEY_STATES_FILE} の保存に失敗しました: {e}")

def load_config_file() -> dict:
    """
    config.jsonを安全に読み込む。ファイルが破損している場合はバックアップから自動復元を試みる。
    """
    if os.path.exists(constants.CONFIG_FILE):
        try:
            with open(constants.CONFIG_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            if not content.strip(): # 空ファイルの場合
                raise json.JSONDecodeError("File is empty", "", 0)
            return json.loads(content)
        except (json.JSONDecodeError, IOError):
            print("警告: config.jsonが空または破損しています。バックアップからの復元を試みます...")
            if _restore_from_backup():
                # 復元に成功したら、再度読み込みを試みる
                try:
                    with open(constants.CONFIG_FILE, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    print(f"!!! エラー: 復元後のconfig.jsonの読み込みにも失敗しました: {e}")
    # ファイルが存在しない、または復元にも失敗した場合
    return {}


def _save_config_file(config_data: dict):
    """
    設定データを一時ファイルに書き込んでからリネームする、堅牢な保存処理。
    """
    # ステップ1: まず現在の設定をバックアップ
    _create_config_backup()

    # ステップ2: アトミックな書き込み処理
    temp_file_path = constants.CONFIG_FILE + ".tmp"
    max_retries = 5
    retry_delay = 0.1

    for attempt in range(max_retries):
        try:
            with open(temp_file_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
            os.replace(temp_file_path, constants.CONFIG_FILE)
            return
        except PermissionError as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                print(f"'{constants.CONFIG_FILE}' 保存エラー: {e}")
                if os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                    except OSError:
                        pass
        except Exception as e:
            print(f"'{constants.CONFIG_FILE}' 保存エラー: {e}")
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except OSError:
                    pass
            return


def save_zhipu_models(models: list[str]) -> bool:
    """
    Zhipu AIの利用可能モデルリストを保存する。
    """
    global AVAILABLE_ZHIPU_MODELS
    
    
    # デフォルトモデルリスト（優先順位保持のため先頭に）
    defaults = constants.ZHIPU_MODELS
    
    # マージ: デフォルト + (取得モデル - デフォルト)
    merged_models = list(defaults)
    for m in models:
        if m not in defaults:
            merged_models.append(m)
            
    # 既存のリストと比較して変更がなければスルー (`set`比較だと順序変更を検知できないためリスト比較も追加)
    if merged_models == AVAILABLE_ZHIPU_MODELS:
        return False
        
    if save_config_if_changed("zhipu_models", merged_models):
        # グローバル変数も更新
        AVAILABLE_ZHIPU_MODELS = list(merged_models) # コピーを保存
        # constantsは書き換えない（定数なので）
        return True
    return False

def save_config_if_changed(key: str, value: Any) -> bool:
    """
    現在の設定値と比較し、変更があった場合のみconfig.jsonに安全に保存する。
    変更があった場合は True を、変更がなかった場合は False を返す。
    【修正】メモリ上のグローバル変数(CONFIG_GLOBAL)も即座に更新する。
    """
    global CONFIG_GLOBAL # グローバル変数を参照

    # ファイルから最新を読み込む
    config = load_config_file()
    
    current_value = config.get(key)
    print(f"[config_manager] save_config_if_changed: key={key}")  # DEBUG
    print(f"[config_manager]   current_value: {current_value}")  # DEBUG
    print(f"[config_manager]   new_value: {value}")  # DEBUG
    print(f"[config_manager]   are_equal: {current_value == value}")  # DEBUG
    
    # 変更チェック
    if current_value == value:
        print(f"[config_manager]   -> No change, skipping save")  # DEBUG
        return False  # 変更なし

    # 変更があれば保存
    config[key] = value
    _save_config_file(config)
    print(f"[config_manager]   -> Saved to file")  # DEBUG
    
    # 【重要】メモリ上の設定も更新して、再起動なしで反映させる
    if CONFIG_GLOBAL is None:
        CONFIG_GLOBAL = {}
    CONFIG_GLOBAL[key] = value
    
    return True

# --- 公開APIキー管理関数 ---
def add_or_update_gemini_key(key_name: str, key_value: str):
    global GEMINI_API_KEYS
    config = load_config_file()
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
    config = load_config_file()
    if "gemini_api_keys" in config and isinstance(config.get("gemini_api_keys"), dict) and key_name in config["gemini_api_keys"]:
        del config["gemini_api_keys"][key_name]

        if not config["gemini_api_keys"]:
            config["gemini_api_keys"] = {"your_key_name": "YOUR_API_KEY_HERE"}

        # paid_api_key_names が存在すれば、削除する
        if "paid_api_key_names" in config and key_name in config["paid_api_key_names"]:
            try:
                config["paid_api_key_names"].remove(key_name)
            except ValueError:
                pass

        if config.get("last_api_key_name") == key_name:
            config["last_api_key_name"] = None
        _save_config_file(config)
        GEMINI_API_KEYS = config.get("gemini_api_keys", {})

def update_pushover_config(user_key: str, app_token: str):
    config = load_config_file()
    config["pushover_user_key"] = user_key
    config["pushover_app_token"] = app_token
    _save_config_file(config)


# --- Theme Management Helpers ---

_file_based_themes_cache = {}

def load_file_based_themes() -> Dict[str, "gr.themes.Base"]:
    """
    `themes/` ディレクトリをスキャンし、有効なテーマファイルを読み込んでキャッシュする。
    """
    global _file_based_themes_cache
    if _file_based_themes_cache:
        return _file_based_themes_cache

    from pathlib import Path
    import importlib.util

    themes_dir = Path("themes")
    if not themes_dir.is_dir():
        return {}

    loaded_themes = {}
    for file_path in themes_dir.glob("*.py"):
        theme_name = file_path.stem
        try:
            spec = importlib.util.spec_from_file_location(theme_name, str(file_path))
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "load") and callable(module.load):
                    theme_object = module.load()
                    import gradio as gr
                    if isinstance(theme_object, gr.themes.Base):
                        loaded_themes[theme_name] = theme_object
        except Exception as e:
            print(f"警告: テーマファイル '{file_path.name}' の読み込みに失敗しました: {e}")

    _file_based_themes_cache = loaded_themes
    return loaded_themes

def get_all_themes() -> Dict[str, str]:
    """UIのドロップダウン用に、すべての利用可能なテーマ名とソースの辞書を返す。"""
    themes = {}
    
    # 1. ファイルベースのテーマ
    file_themes = load_file_based_themes()
    for name in sorted(file_themes.keys()):
        themes[name] = "file"
        
    # 2. JSONベースのカスタムテーマ
    custom_themes_from_json = CONFIG_GLOBAL.get("theme_settings", {}).get("custom_themes", {})
    for name in sorted(custom_themes_from_json.keys()):
        if name not in themes: # ファイルテーマを優先
            themes[name] = "json"
            
    # 3. プリセットテーマ
    for name in ["Soft", "Default", "Monochrome", "Glass"]:
        if name not in themes:
            themes[name] = "preset"
            
    return themes

def get_theme_object(theme_name: str):
    """指定された名前のテーマオブジェクトを取得する。"""
    import gradio as gr
    # 1. ファイルベースのテーマから検索
    file_themes = load_file_based_themes()
    if theme_name in file_themes:
        return file_themes[theme_name]

    # 2. JSONベースのカスタムテーマから検索・構築
    custom_themes_from_json = CONFIG_GLOBAL.get("theme_settings", {}).get("custom_themes", {})
    if theme_name in custom_themes_from_json:
        params = custom_themes_from_json[theme_name]
        try:
            default_arg_keys = ["primary_hue", "secondary_hue", "neutral_hue", "text_size", "spacing_size", "radius_size", "font", "font_mono"]
            default_args = {k: v for k, v in params.items() if k in default_arg_keys}
            set_args = {k: v for k, v in params.items() if k not in default_args}

            if 'font' in default_args and isinstance(default_args['font'], list):
                 default_args['font'] = [gr.themes.GoogleFont(name) if isinstance(name, str) and ' ' in name else name for name in default_args['font']]

            theme_obj = gr.themes.Default(**default_args)
            if set_args:
                theme_obj = theme_obj.set(**set_args)
            return theme_obj
        except Exception as e:
            print(f"警告: カスタムテーマ '{theme_name}' の構築に失敗しました: {e}")

    # 3. プリセットテーマから検索
    preset_map = {"Soft": gr.themes.Soft, "Default": gr.themes.Default, "Monochrome": gr.themes.Monochrome, "Glass": gr.themes.Glass}
    if theme_name in preset_map:
        return preset_map[theme_name]()

    # 4. フォールバック
    print(f"警告: テーマ '{theme_name}' が見つかりません。デフォルトのSoftテーマを使用します。")
    return gr.themes.Soft()


# --- モデルリスト取得（API経由） ---
def fetch_models_from_api(base_url: str, api_key: str = "") -> list[str]:
    """
    OpenAI互換API (/v1/models) からモデルリストを取得する。
    Groq, Ollama, OpenRouter など全てに対応。
    
    Args:
        base_url: プロバイダのベースURL（例: https://api.groq.com/openai/v1）
        api_key: APIキー（Ollamaは不要）
    
    Returns:
        モデルIDのリスト
    """
    import requests
    
    # URLの末尾スラッシュを除去し、/modelsを追加
    models_url = base_url.rstrip('/') + '/models'
    
    headers = {"Content-Type": "application/json"}
    if api_key and api_key != "ollama":
        headers["Authorization"] = f"Bearer {api_key}"
    
    try:
        response = requests.get(models_url, headers=headers, timeout=10)
        # エラー詳細確認のため、raise_for_statusの前に内容をチェック
        if response.status_code != 200:
            print(f"[config_manager] モデルリスト取得失敗: Status={response.status_code}, Body={response.text}")
        
        response.raise_for_status()
        data = response.json()
        
        # OpenAI互換APIのレスポンス形式: {"data": [{"id": "model-name", ...}, ...]}
        models = []
        for model_info in data.get("data", []):
            model_id = model_info.get("id", "")
            if model_id:
                models.append(model_id)
        
        return sorted(models)
    except Exception as e:
        print(f"[config_manager] モデルリスト取得エラー: {e}")
        return []


def toggle_favorite_model(provider_name: str, model_name: str) -> tuple[bool, str]:
    """
    モデルのお気に入り状態をトグルする（⭐ マークの付け外し）。
    
    Args:
        provider_name: プロバイダ名（例: "OpenRouter", "Groq", "Local Ollama"）
        model_name: モデル名
    
    Returns:
        (成功したか, 新しいモデル名)
    """
    global CONFIG_GLOBAL
    
    # お気に入りマーク
    FAVORITE_MARK = "⭐ "
    
    # 現在のお気に入り状態を確認
    is_favorite = model_name.startswith(FAVORITE_MARK)
    
    # トグル後の新しいモデル名
    if is_favorite:
        new_model_name = model_name[len(FAVORITE_MARK):]  # マークを削除
    else:
        new_model_name = FAVORITE_MARK + model_name  # マークを追加
    
    # 設定内のモデルリストを更新
    provider_settings = CONFIG_GLOBAL.get("openai_provider_settings", [])
    for provider in provider_settings:
        if provider.get("name") == provider_name:
            available_models = provider.get("available_models", [])
            
            # 旧モデル名を新モデル名に置換
            if model_name in available_models:
                idx = available_models.index(model_name)
                available_models[idx] = new_model_name
                
                # 設定を保存
                save_config()
                return (True, new_model_name)
    
    return (False, model_name)


def add_model_to_list(provider_name: str, model_name: str) -> bool:
    """
    プロバイダのモデルリストにモデルを追加する。
    
    Args:
        provider_name: プロバイダ名
        model_name: 追加するモデル名
    
    Returns:
        成功したか
    """
    global CONFIG_GLOBAL
    
    provider_settings = CONFIG_GLOBAL.get("openai_provider_settings", [])
    for provider in provider_settings:
        if provider.get("name") == provider_name:
            available_models = provider.get("available_models", [])
            
            # 重複チェック（⭐ マークの有無を無視して比較）
            clean_model_name = model_name.lstrip("⭐ ")
            existing_clean = [m.lstrip("⭐ ") for m in available_models]
            
            if clean_model_name not in existing_clean:
                available_models.append(model_name)
                save_config()
                return True
            else:
                print(f"[config_manager] モデル '{model_name}' は既にリストに存在します")
                return False
    
    return False


# --- デフォルト設定を取得する関数 ---
def _get_default_config() -> dict:
    """
    デフォルト設定を返す。
    OpenAI互換プロファイルのリセット機能で使用される。
    """
    return {
        "openai_provider_settings": [
            {
                "name": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "",
                "default_model": "meta-llama/llama-3.3-70b-instruct:free",
                "available_models": [
                    "meta-llama/llama-3.3-70b-instruct:free",
                    "nvidia/nemotron-3-nano-30b-a3b:free",
                    "xiaomi/mimo-v2-flash:free",
                    "deepseek/deepseek-r1-0528:free",
                    "google/gemma-3-27b-it:free",
                    "qwen/qwen3-coder:free"
                ]
            },
            {
                "name": "Groq",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": "",
                "default_model": "llama-3.3-70b-versatile",
                "available_models": [
                    "llama-3.3-70b-versatile",
                    "llama-3.1-8b-instant",
                    "openai/gpt-oss-120b",
                    "qwen/qwen3-32b"
                ]
            },
            {
                "name": "Local Ollama",
                "base_url": "http://localhost:11434/v1",
                "api_key": "ollama",
                "default_model": "phi3.5",
                "tool_use_enabled": False,
                "available_models": [
                    "phi3.5",
                    "qwen2.5:3b",
                    "gemma2:2b",
                    "qwen2.5:0.5b"
                ]
            },
            {
                "name": "OpenAI Official",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "default_model": "gpt-5.2-2025-12-11",
                "available_models": [
                    "gpt-5.2-2025-12-11",
                    "chatgpt-4o-latest"
                ]
            },
            {
                "name": "Zhipu AI",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "api_key": "",
                "default_model": "glm-4.7-flash",
                "available_models": [
                    "glm-4.7-flash",
                    "glm-4.7",
                    "glm-4-plus",
                    "glm-4.5",
                    "glm-4.5-air",
                    "glm-zero-preview"
                ]
            }
        ]
    }


# --- メインの読み込み関数 (真・最終版) ---
def load_config():
    global CONFIG_GLOBAL, GEMINI_API_KEYS, TAVILY_API_KEY, initial_api_key_name_global, initial_room_global, initial_model_global
    global initial_send_thoughts_to_api_global, initial_api_history_limit_option_global, initial_alarm_api_history_turns_global
    global AVAILABLE_MODELS_GLOBAL, DEFAULT_MODEL_GLOBAL, initial_streaming_speed_global
    global NOTIFICATION_SERVICE_GLOBAL, NOTIFICATION_WEBHOOK_URL_GLOBAL, PUSHOVER_CONFIG
    global ZHIPU_API_KEY, GROQ_API_KEY, MOONSHOT_API_KEY, LOCAL_MODEL_PATH

    # APIキーの枯渇状態を読み込む
    load_gemini_key_states()

    # ステップ1：全てのキーを含む、理想的なデフォルト設定を定義
# ステップ1：全てのキーを含む、理想的なデフォルト設定を定義
    default_config = {
        # --- [新規] マルチプロバイダ設定 ---
        "active_provider": "google", # google, openai
        "active_openai_profile": "OpenRouter", # デフォルトで選択されるプロファイル名
        "openai_provider_settings": [
            {
                "name": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "",
                "default_model": "meta-llama/llama-3.3-70b-instruct:free",
                "available_models": [
                    # 無料モデル（2025年12月時点の有効なモデル）
                    "meta-llama/llama-3.3-70b-instruct:free",     # 13万トークン、ツール対応、安定
                    "nvidia/nemotron-3-nano-30b-a3b:free",        # 25.6万トークン、NVIDIA
                    "xiaomi/mimo-v2-flash:free",                  # 26.2万トークン、Xiaomi
                    "deepseek/deepseek-r1-0528:free",             # 16.4万トークン、推論特化
                    "google/gemma-3-27b-it:free",                 # 13万トークン
                    "qwen/qwen3-coder:free"                       # 26.2万トークン、コード特化
                ]
            },
            {
                "name": "Groq",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": "",
                "default_model": "llama-3.3-70b-versatile",
                "available_models": [
                    # Production Models (無料・高速)
                    "llama-3.3-70b-versatile",              # 最新・汎用
                    "llama-3.1-8b-instant",                 # 軽量・高速
                    "openai/gpt-oss-120b",                  # OpenAI OSS
                    "qwen/qwen3-32b"                        # Qwen3 32B (Preview)
                ]
            },
            {
                "name": "Local Ollama",
                "base_url": "http://localhost:11434/v1",
                "api_key": "ollama",
                "default_model": "phi3.5",
                "tool_use_enabled": False,  # 【ツール不使用モード】Ollamaはデフォルトでツール無効
                "available_models": [
                    # VRAM 4GB対応モデル（低スペックPC向け）
                    "phi3.5",       # 最適！2.5GB、ツール対応
                    "qwen2.5:3b",   # バランス良、ツール対応
                    "gemma2:2b",    # 超軽量
                    "qwen2.5:0.5b"  # 超々軽量（内部処理用候補）
                ]
            },
            {
                "name": "OpenAI Official",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "default_model": "gpt-5.2-2025-12-11",
                "available_models": [
                    "gpt-5.2-2025-12-11",   # 最新
                    "chatgpt-4o-latest"     # 人気（Keep4o運動）
                ]
            },
            {
                "name": "Zhipu AI",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "api_key": "",
                "default_model": "glm-4.7-flash",
                "available_models": [
                    "glm-4.7-flash",
                    "glm-4.7",
                    "glm-4-plus",
                    "glm-4.5",
                    "glm-4.5-air",
                    "glm-zero-preview"
                ]
            },
            {
                "name": "Moonshot AI",
                "base_url": "https://api.moonshot.ai/v1",
                "api_key": "",
                "default_model": "kimi-k2.5",
                "available_models": [
                    "kimi-k2.5",
                    "moonshot-v1-8k",
                    "moonshot-v1-32k",
                    "moonshot-v1-128k"
                ]
            }
        ],

        # ---------------------------------
        "gemini_api_keys": {"your_key_name": "YOUR_API_KEY_HERE"},
        "paid_api_key_names": [],
        "available_models": [
            "gemini-2.5-flash", 
            "gemini-2.5-pro", 
            "gemini-2.5-flash-lite",
            "gemini-3-flash-preview", 
            "gemini-3-pro-preview"
        ],
        "default_model": "gemini-2.5-flash",
        # --- 画像生成設定（マルチプロバイダ対応）---
        "image_generation_provider": "gemini",  # gemini | openai | disabled
        "image_generation_model": "gemini-2.5-flash-image",  # 使用するモデル名
        "image_generation_openai_settings": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-image-1"
        },
        "available_image_models": {
            "gemini": ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"],
            "openai": ["gpt-image-1", "gpt-image-1.5", "dall-e-3", "dall-e-2"]
        }, 
        "search_provider": constants.DEFAULT_SEARCH_PROVIDER,
        "tavily_api_key": "",  # Tavily検索用APIキー
        "last_room": "Default",
        "last_model": "gemini-2.5-flash",
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
        "backup_rotation_count": 10,
        "theme_settings": {
            "active_theme": "nexus_modern", # デフォルトテーマをモダン版に変更
            "custom_themes": {} # config.jsonで管理するカスタムテーマは最初は空
        },
        "watchlist_settings": {
            "notify_on_change": True  # デフォルトで通知有効
        }
    }

    # ステップ2：ユーザーの設定ファイルを読み込む
    user_config = load_config_file()

    # ステップ3：【賢いマージ】テーマ設定をディープマージする
    default_theme_settings = default_config["theme_settings"]
    user_theme_settings = user_config.get("theme_settings", {})
    # ユーザーのカスタムテーマのみを尊重する（ファイルベースのテーマはjsonにマージしない）
    final_theme_settings = {
        "active_theme": user_theme_settings.get("active_theme", default_theme_settings["active_theme"]),
        "custom_themes": user_theme_settings.get("custom_themes", {})
    }

    # ステップ4：【厳格なマージ】available_modelsを統合する
    # デフォルトを真の源泉 (Single Source of Truth) とし、ユーザー設定にある古いモデルや注釈なしの名前を排除する。
    default_models = default_config["available_models"]
    user_models = user_config.get("available_models", [])
    
    # 基本方針:
    # 1. デフォルトに含まれるモデルはそのまま採用。
    # 2. リストに含まれない「gemini-2.0」などの古いモデルは除外対象とする。
    
    merged_models = default_models.copy()
    
    # 注釈付きモデルの「ベース名」リストを作成
    annotated_base_names = [m.split(" (")[0] for m in default_models if " (" in m]
    obsolete_keywords = ["gemini-1.5", "gemini-2.0"]

    for m in user_models:
        # すでにリストにある（完全一致）ならスキップ
        if m in merged_models:
            continue
            
        # 除外判定
        is_obsolete = any(k in m for k in obsolete_keywords)
        is_unannotated_duplicate = m in annotated_base_names
        
        if not is_obsolete and not is_unannotated_duplicate:
            # どちらにも該当せず、かつデフォルトにない（ユーザーが手動で追加したカスタムモデル等）場合のみ追加を許可
            merged_models.append(m)
        else:
            print(f"--- [Config Manager] Cleaning up obsolete/duplicate model: {m} ---")

    # ステップ4.5：【賢いマージ】OpenAI互換プロバイダのavailable_modelsを統合する
    # デフォルトのモデルリストとユーザーが追加したモデルをマージし、ユーザー追加モデルが消えないようにする
    def merge_openai_provider_models(default_providers: List[Dict], user_providers: List[Dict]) -> List[Dict]:
        """OpenAI互換プロバイダの設定をマージする。ユーザー追加モデルを保持しつつ、デフォルトモデルも追加する。"""
        merged_providers = []
        
        # デフォルトプロバイダをnameでインデックス化
        default_by_name = {p["name"]: p for p in default_providers}
        user_by_name = {p["name"]: p for p in user_providers}
        
        # 全てのプロバイダ名を収集（デフォルト優先、ユーザー追加も含む）
        all_provider_names = list(default_by_name.keys())
        for name in user_by_name.keys():
            if name not in all_provider_names:
                all_provider_names.append(name)
        
        for name in all_provider_names:
            default_p = default_by_name.get(name, {})
            user_p = user_by_name.get(name, {})
            
            if not default_p and user_p:
                # ユーザーが追加したカスタムプロバイダ
                merged_providers.append(user_p)
            elif default_p and not user_p:
                # デフォルトにしかないプロバイダ（新規追加）
                merged_providers.append(default_p)
            else:
                # 両方に存在するプロバイダ：設定をマージ
                merged_p = default_p.copy()
                # ユーザー設定を優先（api_key, default_model, base_url）
                if user_p.get("api_key"):
                    merged_p["api_key"] = user_p["api_key"]
                if user_p.get("default_model"):
                    merged_p["default_model"] = user_p["default_model"]
                if user_p.get("base_url"):
                    merged_p["base_url"] = user_p["base_url"]
                
                # available_modelsはマージ（デフォルト + ユーザー追加）
                default_models = set(default_p.get("available_models", []))
                user_models = set(user_p.get("available_models", []))
                merged_p["available_models"] = sorted(list(default_models | user_models))
                
                merged_providers.append(merged_p)
        
        return merged_providers
    
    merged_openai_providers = merge_openai_provider_models(
        default_config.get("openai_provider_settings", []),
        user_config.get("openai_provider_settings", [])
    )

    # ステップ5：ユーザー設定を優先しつつ、不足キーを補完
    config = default_config.copy()
    config.update(user_config)
    # 統合したモデルリストとテーマ設定で、最終的な設定を上書き
    config["available_models"] = merged_models
    config["theme_settings"] = final_theme_settings
    config["openai_provider_settings"] = merged_openai_providers

    # ステップ5.5：【移行処理】Zhipu AI APIキーの移行
    # 既存の zhipu_api_key があり、かつ Zhipu AI プロファイルのキーが空の場合に移行
    zhipu_legacy_key = config.get("zhipu_api_key", "")
    if zhipu_legacy_key:
        for p in config["openai_provider_settings"]:
            if p.get("name") == "Zhipu AI" and not p.get("api_key"):
                print(f"--- [Config Manager] Migrating Zhipu AI API Key to OpenAI profile ---")
                p["api_key"] = zhipu_legacy_key
                break

    # [Patch] Moonshot API Key Injection
    moonshot_legacy_key = config.get("moonshot_api_key")
    if moonshot_legacy_key and "openai_provider_settings" in config:
        for p in config["openai_provider_settings"]:
            if p["name"] == "Moonshot AI" and not p.get("api_key"):
                p["api_key"] = moonshot_legacy_key
                break

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

    # ステップ8：グローバル変数を更新
    CONFIG_GLOBAL = config
    GEMINI_API_KEYS = config.get("gemini_api_keys", {})
    GEMINI_KEY_STATES = {k: {'exhausted': False} for k in GEMINI_API_KEYS}
    TAVILY_API_KEY = config.get("tavily_api_key", "")
    ZHIPU_API_KEY = config.get("zhipu_api_key", "")
    GROQ_API_KEY = config.get("groq_api_key", "")
    MOONSHOT_API_KEY = config.get("moonshot_api_key", "")
    LOCAL_MODEL_PATH = config.get("local_model_path", "")
    AVAILABLE_MODELS_GLOBAL = config.get("available_models", [])
    DEFAULT_MODEL_GLOBAL = config.get("default_model", DEFAULT_MODEL_GLOBAL)
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


# --- [モデルリスト管理関数] ---

def get_default_available_models() -> List[str]:
    """
    デフォルトのGeminiモデルリストを返す。
    リセット機能で使用される。
    """
    return [
        "gemini-2.5-flash", 
        "gemini-2.5-pro", 
        "gemini-2.5-flash-lite",
        "gemini-3-flash-preview", 
        "gemini-3-pro-preview"
    ]


def remove_model_from_list(model_name: str) -> bool:
    """
    指定されたモデルをavailable_modelsから削除して保存する。
    成功した場合はTrue、モデルが見つからない場合はFalseを返す。
    """
    global AVAILABLE_MODELS_GLOBAL
    
    current_models = list(AVAILABLE_MODELS_GLOBAL)
    if model_name not in current_models:
        return False
    
    current_models.remove(model_name)
    AVAILABLE_MODELS_GLOBAL = current_models
    save_config_if_changed("available_models", current_models)
    return True


def reset_models_to_default() -> List[str]:
    """
    モデルリストをデフォルト状態にリセットして保存する。
    リセット後のモデルリストを返す。
    """
    global AVAILABLE_MODELS_GLOBAL
    
    default_models = get_default_available_models()
    AVAILABLE_MODELS_GLOBAL = default_models
    save_config_if_changed("available_models", default_models)
    return default_models


def get_effective_settings(room_name: str, **kwargs) -> dict:
    """
    ルームのファイル設定と、UIからのリアルタイムな設定（kwargs）をマージして、
    最終的に適用される設定値を返す。
    """
    effective_settings = {
        "model_name": DEFAULT_MODEL_GLOBAL, "voice_id": "iapetus", "voice_style_prompt": "",
        "add_timestamp": True, "send_thoughts": False,
        "send_notepad": True, "use_common_prompt": True,
        "send_core_memory": True,
        "enable_scenery_system": False, 
        "enable_auto_retrieval": False,
        "send_scenery": True,
        "scenery_send_mode": "変更時のみ",  # 情景画像送信タイミング: 「変更時のみ」or「毎ターン」
        "send_current_time": True,
        "auto_memory_enabled": False,
        "thinking_level": "auto",
        "enable_typewriter_effect": True,
        "streaming_speed": 0.01,
        "temperature": 1.0, "top_p": 0.95,
        "safety_block_threshold_harassment": "BLOCK_ONLY_HIGH",
        "safety_block_threshold_hate_speech": "BLOCK_ONLY_HIGH",
        "safety_block_threshold_sexually_explicit": "BLOCK_ONLY_HIGH",
        "safety_block_threshold_dangerous_content": "BLOCK_ONLY_HIGH",
        "api_history_limit": constants.DEFAULT_API_HISTORY_LIMIT_OPTION,
        # 自動会話要約
        "auto_summary_enabled": False,
        "auto_summary_threshold": constants.AUTO_SUMMARY_DEFAULT_THRESHOLD,
        "sleep_consolidation": {
            "update_episodic_memory": True,
            "update_memory_index": True,
            "update_current_log_index": False,
            "update_entity_memory": True,
            "compress_old_episodes": False
        },
        "watchlist_settings": {
            "notify_on_change": True
        },
        "project_explorer": {
            "root_path": "",
            "exclude_dirs": [".git", "venv", "__pycache__", "node_modules", ".agent", ".gemini"],
            "exclude_files": ["*.pyc", ".env", "config.json"]
        }
    }
    
    
    room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
    room_model_name = None  # ルーム個別モデル設定（Google用）
    room_zhipu_model = None # ルーム個別モデル設定（Zhipu用）
    room_provider = None  # ルーム個別プロバイダ設定（Noneは共通設定に従う）
    if os.path.exists(room_config_path):
        try:
            with open(room_config_path, "r", encoding="utf-8") as f:
                room_config = json.load(f)
            override_settings = room_config.get("override_settings", {})
            for k, v in override_settings.items():
                # model_name は後で別ロジックで処理するので、ここでは読み込まない
                # providerとopenai_settingsはUI表示用にそのまま含める
                if v is not None and k != "model_name":
                    effective_settings[k] = v
            # ルーム個別のモデル設定を一時保存（後のロジックで使用）
            room_model_name = override_settings.get("model_name")
            room_zhipu_model = override_settings.get("zhipu_model")
            # ルーム個別のプロバイダ設定（共通設定に従うかどうか）
            room_provider = override_settings.get("provider")
        except Exception as e:
            print(f"ルーム設定ファイル '{room_config_path}' の読み込みエラー: {e}")

    for key, value in kwargs.items():
        # "global_model_from_ui" はモデル決定ロジックで使うので、ここでは除外
        if key not in ["global_model_from_ui"] and value is not None:
            effective_settings[key] = value

# --- モデル選択の最終決定ロジック ---
    global_model_from_ui = kwargs.get("global_model_from_ui")
    
    active_provider = get_active_provider(room_name)
    
    if active_provider == "openai":
        # OpenAI互換モード: ルーム個別のopenai_settings > グローバルなアクティブプロファイル の優先度
        room_openai_settings = effective_settings.get("openai_settings")
        
        # [Dynamic Injection] ルーム個別設定の場合も、APIキーはグローバル設定の最新値を注入する
        # これにより、ルーム設定保存後にAPIキーが変更された場合でも認証エラーを防ぐ
        if room_openai_settings:
            provider_name = room_openai_settings.get("name")
            if provider_name == "Zhipu AI":
                global_key = CONFIG_GLOBAL.get("zhipu_api_key")
                if global_key:
                    room_openai_settings["api_key"] = global_key
            elif provider_name == "Moonshot AI":
                global_key = CONFIG_GLOBAL.get("moonshot_api_key")
                if global_key:
                    room_openai_settings["api_key"] = global_key
        
        if room_openai_settings and room_openai_settings.get("model"):
            # ルーム個別のOpenAI設定でモデルが指定されている場合
            effective_settings["model_name"] = room_openai_settings["model"]
        else:
            # フォールバック: グローバルなアクティブプロファイルのデフォルトモデル
            openai_setting = get_active_openai_setting()
            if openai_setting:
                effective_settings["model_name"] = openai_setting.get("default_model", "gpt-3.5-turbo")

    elif active_provider == "zhipu":
        # Zhipu AI (GLM-4) モード
        # ルーム個別設定 > デフォルト (AVAILABLE_ZHIPU_MODELS[0])
        if room_zhipu_model and room_provider is not None:
             effective_settings["model_name"] = room_zhipu_model
        else:
             # フォールバック: available_settings の先頭
             effective_settings["model_name"] = AVAILABLE_ZHIPU_MODELS[0] if AVAILABLE_ZHIPU_MODELS else "glm-4.7-flash"
    else:
        # Googleモード: ルーム個別設定 > UI指定 > デフォルト の優先度でモデルを決定
        # ただし、room_providerがNone（共通設定に従う）の場合はルーム個別モデルを無視
        if room_model_name and room_provider is not None:
            # プロバイダがルーム個別設定（google等）の場合のみ、ルーム個別モデルを使用
            final_model_name = room_model_name
        elif global_model_from_ui:
            final_model_name = global_model_from_ui
        else:
            final_model_name = DEFAULT_MODEL_GLOBAL
        effective_settings["model_name"] = final_model_name

        # 念の為のフォールバック
        if not effective_settings.get("model_name"):
            effective_settings["model_name"] = DEFAULT_MODEL_GLOBAL

    # 【重要】プロバイダ情報を明示的に含める（gemini_apiなどで参照するため）
    effective_settings["provider"] = active_provider
            
    return effective_settings

from typing import Tuple

def get_api_key_choices_for_ui() -> List[Tuple[str, str]]:
    """UI用の選択肢リストを (表示名, 値) のタプルで返す。表示名には Paid ラベルを付与する。"""
    paid_key_names = CONFIG_GLOBAL.get("paid_api_key_names", []) if isinstance(CONFIG_GLOBAL, dict) else []
    choices: List[Tuple[str, str]] = []
    for key_name in sorted(GEMINI_API_KEYS.keys()):
        display = f"{key_name} (Paid)" if key_name in paid_key_names else key_name
        choices.append((display, key_name))
    return choices

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
    config = load_config_file()
    if "theme_settings" not in config:
        config["theme_settings"] = {}
    config["theme_settings"]["active_theme"] = active_theme
    config["theme_settings"]["custom_themes"] = custom_themes
    _save_config_file(config)

from typing import Optional

def get_latest_api_key_name_from_config() -> Optional[str]:
    """
    config.jsonを直接読み込み、最後に選択された有効なAPIキー名を返す。
    UIの状態に依存しないため、バックグラウンドスレッドから安全に呼び出せる。
    """
    config = load_config_file()
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


def get_active_gemini_api_key(room_name: str = None) -> Optional[str]:
    """
    指定されたルームの設定（またはグローバル設定）に基づいて、
    現在有効な Gemini API キーの『値（文字列）』を直接返す。
    キーが設定されていない場合は None を返す。
    """
    # ルーム名が指定されている場合、そのルームでのローテーション有効設定を確認
    rotation_enabled_global = CONFIG_GLOBAL.get("enable_api_key_rotation", True)
    rotation_enabled_room = True # デフォルトはTrue (有効)
    
    if room_name:
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        if os.path.exists(room_config_path):
            try:
                with open(room_config_path, "r", encoding="utf-8") as f:
                    room_config = json.load(f)
                override_settings = room_config.get("override_settings", {})
                
                # 個別設定でのスイッチ確認 (Noneなら共通設定に従う)
                room_rot_setting = override_settings.get("enable_api_key_rotation")
                if room_rot_setting is not None:
                    rotation_enabled_room = room_rot_setting
                else:
                    rotation_enabled_room = rotation_enabled_global

                # プロバイダ設定を確認（Noneの場合は共通設定に従うため、個別キー設定も無視する）
                room_provider = override_settings.get("provider")
                
                if room_provider is not None:
                    room_api_key_name = override_settings.get("api_key_name")
                    if room_api_key_name:
                        key_val = GEMINI_API_KEYS.get(room_api_key_name)
                        if key_val and not key_val.startswith("YOUR_API_KEY"):
                            # キーが枯渇しているかチェック
                            if rotation_enabled_room and is_key_exhausted(room_api_key_name):
                                print(f"Warning: Room key '{room_api_key_name}' is exhausted. Falling back to common pool.")
                                # フォールバック: 下記の共通設定ロジックへ流れる
                            else:
                                return key_val
            except Exception:
                pass

    # 共通設定でのローテーション確認
    # ルーム指定がない、またはルーム設定でフォールバックした場合
    rotation_enabled = rotation_enabled_room if room_name else rotation_enabled_global

    # 2. グローバル設定（最後に使ったキー、または最初の有効なキー）
    # ローテーション有効なら、利用可能なキーを探す
    if rotation_enabled:
        available_key_name = get_next_available_gemini_key()
        if available_key_name:
            return GEMINI_API_KEYS.get(available_key_name)
    
    # ローテーション無効、または有効なキーが見つからない場合は従来のロジック
    key_name = get_latest_api_key_name_from_config()
    if key_name:
        key_val = GEMINI_API_KEYS.get(key_name)
        # 【追加】たとえローテーション無効でも、そのキーが枯渇しているなら最後の手段として利用可能キーを探す
        if is_key_exhausted(key_name):
            alt_key = get_next_available_gemini_key()
            if alt_key:
                return GEMINI_API_KEYS.get(alt_key)
        return key_val

    return None


def get_active_gemini_api_key_name(room_name: str = None) -> Optional[str]:
    """
    指定されたルームの設定（またはグローバル設定）に基づいて、
    現在有効な Gemini API キーの『名称』を返す。
    キーが設定されていない場合は None を返す。
    """
    # ルーム名が指定されている場合、そのルームでのローテーション有効設定を確認
    rotation_enabled_global = CONFIG_GLOBAL.get("enable_api_key_rotation", True)
    rotation_enabled_room = True 
    
    if room_name:
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        if os.path.exists(room_config_path):
            try:
                with open(room_config_path, "r", encoding="utf-8") as f:
                    room_config = json.load(f)
                override_settings = room_config.get("override_settings", {})
                
                # 個別設定でのスイッチ確認
                room_rot_setting = override_settings.get("enable_api_key_rotation")
                if room_rot_setting is not None:
                    rotation_enabled_room = room_rot_setting
                else:
                    rotation_enabled_room = rotation_enabled_global

                # プロバイダ設定を確認
                room_provider = override_settings.get("provider")
                
                if room_provider is not None:
                    room_api_key_name = override_settings.get("api_key_name")
                    if room_api_key_name:
                        # キー自体が存在することを確認
                        key_val = GEMINI_API_KEYS.get(room_api_key_name)
                        if key_val and not key_val.startswith("YOUR_API_KEY"):
                            # キーが枯渇しているかチェック
                            if rotation_enabled_room and is_key_exhausted(room_api_key_name):
                                return get_next_available_gemini_key() or room_api_key_name # フォールバックしても見つからなければ元の名前を返す
                            else:
                                return room_api_key_name
            except Exception:
                pass

    rotation_enabled = rotation_enabled_room if room_name else rotation_enabled_global

    # ローテーション有効なら、利用可能なキーを探す
    if rotation_enabled:
        available_key_name = get_next_available_gemini_key()
        if available_key_name:
            return available_key_name

    # 2. グローバル設定（最後に使ったキー、または最初の有効なキー）
    return get_latest_api_key_name_from_config()


def get_key_name_by_value(api_key_value: str) -> str:
    """
    APIキーの値から、設定ファイル内の名称を逆引きする。
    見つからない場合は "Unknown" を返す。
    """
    if not api_key_value:
        return "Unknown"
        
    # 値のスペース除去などで正規化して比較
    target_val = api_key_value.strip()
    
    for name, val in GEMINI_API_KEYS.items():
        if val and isinstance(val, str) and val.strip() == target_val:
            return name
            
    return "Unknown"



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

def get_current_global_model() -> str:
    """
    config.jsonから、現在ユーザーが共通設定で選択している
    有効なグローバルモデル名を返す。
    """
    # 常に最新の設定をファイルから読み込む
    config = load_config_file()
    
    # last_modelキーが存在し、かつ利用可能モデルリストに含まれていればそれを優先
    last_model = config.get("last_model")
    available_models = config.get("available_models", [])
    if last_model and last_model in available_models:
        return last_model
        
    # それ以外の場合は、default_modelキーを返す
    return config.get("default_model", DEFAULT_MODEL_GLOBAL)

# --- [Multi-Provider Support Helpers] ---

def get_active_provider(room_name: str = None) -> str:
    """
    現在アクティブなプロバイダ名 ('google' または 'openai') を返す。
    room_nameが指定された場合、ルーム個別の設定を優先する。
    """
    if room_name:
        # ルーム個別のプロバイダ設定を確認
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        if os.path.exists(room_config_path):
            try:
                with open(room_config_path, "r", encoding="utf-8") as f:
                    room_config = json.load(f)
                override_settings = room_config.get("override_settings", {})
                room_provider = override_settings.get("provider")
                # ルーム個別にプロバイダが設定されている場合はそれを使用
                # ルーム個別にプロバイダが設定されている場合はそれを使用
                if room_provider and room_provider in ["google", "openai", "zhipu"]:
                    return room_provider
            except Exception:
                pass
    # フォールバック: グローバル設定
    return CONFIG_GLOBAL.get("active_provider", "google")

def set_active_provider(provider: str):
    """アクティブなプロバイダを切り替える"""
    """アクティブなプロバイダを切り替える"""
    if provider in ["google", "openai", "zhipu"]:
        save_config_if_changed("active_provider", provider)

def get_openai_settings_list() -> List[Dict]:
    """OpenAI互換プロバイダの設定リストを返す"""
    return CONFIG_GLOBAL.get("openai_provider_settings", [])

def save_openai_settings_list(settings_list: List[Dict]):
    """OpenAI互換プロバイダの設定リストを保存する"""
    if isinstance(settings_list, list):
        save_config_if_changed("openai_provider_settings", settings_list)

def get_active_openai_profile_name() -> str:
    """現在選択されているOpenAIプロファイル名（例: 'OpenRouter'）を返す"""
    return CONFIG_GLOBAL.get("active_openai_profile", "OpenRouter")

def set_active_openai_profile(profile_name: str):
    """アクティブなOpenAIプロファイル名を保存する"""
    save_config_if_changed("active_openai_profile", profile_name)

def get_openai_setting_by_name(profile_name: str) -> Optional[Dict]:
    """
    指定された名前（例: 'Groq', 'Zhipu AI'）のOpenAIプロファイル設定辞書を返す。
    """
    if not profile_name: return None
    
    settings = get_openai_settings_list()
    target_setting = None
    for s in settings:
        if s.get("name") == profile_name:
            target_setting = s
            break
            
    if target_setting:
        target_setting = target_setting.copy()
        # [Dynamic Injection] 特定のプロバイダの場合はグローバルな設定からAPIキーを反映
        if target_setting.get("name") == "Zhipu AI":
            global_key = CONFIG_GLOBAL.get("zhipu_api_key")
            if global_key:
                target_setting["api_key"] = global_key
        elif target_setting.get("name") == "Moonshot AI":
            global_key = CONFIG_GLOBAL.get("moonshot_api_key")
            if global_key:
                target_setting["api_key"] = global_key
        elif target_setting.get("name") == "Groq":
            global_key = CONFIG_GLOBAL.get("groq_api_key")
            if global_key:
                target_setting["api_key"] = global_key
                
        return target_setting
    return None

def get_active_openai_setting() -> Optional[Dict]:
    """現在アクティブなOpenAIプロファイルの設定辞書を返す"""
    profile_name = get_active_openai_profile_name()
    return get_openai_setting_by_name(profile_name)

def is_tool_use_enabled(room_name: str = None) -> bool:
    """
    【ツール不使用モード】
    現在のプロバイダ設定でツール使用が有効かどうかを返す。
    room_nameが指定された場合、ルーム個別の設定を優先する。
    - Googleプロバイダ: 常にTrue
    - OpenAI互換プロバイダ: ルーム個別またはプロファイルの`tool_use_enabled`設定に従う（デフォルトTrue）
    """
    active_provider = get_active_provider(room_name)
    
    if active_provider == "google":
        # Geminiは常にツール使用可能
        return True
    
    # OpenAI互換プロバイダの場合
    # まずルーム個別のopenai_settings.tool_use_enabledを確認
    if room_name:
        room_config_path = os.path.join(constants.ROOMS_DIR, room_name, "room_config.json")
        if os.path.exists(room_config_path):
            try:
                with open(room_config_path, "r", encoding="utf-8") as f:
                    room_config = json.load(f)
                override_settings = room_config.get("override_settings", {})
                room_openai_settings = override_settings.get("openai_settings", {})
                # ルーム個別のtool_use_enabledが明示的に設定されている場合はそれを使用
                if "tool_use_enabled" in room_openai_settings:
                    return room_openai_settings["tool_use_enabled"]
            except Exception:
                pass
    
    # フォールバック: グローバルなアクティブプロファイルの設定
    openai_setting = get_active_openai_setting()
    if openai_setting:
        # プロファイルのtool_use_enabled設定を取得（デフォルトTrue）
        return openai_setting.get("tool_use_enabled", True)
    
    return True  # フォールバック


# --- [Phase 2] 内部処理モデル設定管理 ---

def get_internal_model_settings() -> Dict[str, Any]:
    """
    内部処理モデルの設定を取得する。
    設定がない場合はデフォルト値を返す。
    """
    default_settings = {
        "provider": "google",  # "google", "openai"
        "processing_model": constants.INTERNAL_PROCESSING_MODEL,  # 軽量タスク
        "summarization_model": constants.SUMMARIZATION_MODEL,    # 要約・文章生成
        "supervisor_model": constants.INTERNAL_PROCESSING_MODEL,  # グループ司会
        # OpenAI互換設定
        "openai_profile": None,  # 使用するプロファイル名
        # エンベディング設定
        "embedding_provider": "gemini",  # "gemini", "openai", "local"
        "embedding_model": "gemini-embedding-001",
        # フォールバック設定
        "fallback_enabled": True,
        "fallback_order": ["google"],
    }
    
    user_settings = CONFIG_GLOBAL.get("internal_model_settings", {})
    
    # デフォルト値とマージ（ユーザー設定を優先）
    merged = default_settings.copy()
    merged.update(user_settings)
    
    return merged


def save_internal_model_settings(settings: Dict[str, Any]) -> bool:
    """
    内部処理モデルの設定を保存する。
    
    Returns:
        保存が成功したかどうか
    """
    try:
        print(f"[config_manager] save_internal_model_settings called with: {settings}")  # DEBUG
        result = save_config_if_changed("internal_model_settings", settings)
        print(f"[config_manager] save_config_if_changed returned: {result}")  # DEBUG
        return True  # 例外がなければ成功（変更がなかった場合もTrue）
    except Exception as e:
        print(f"[config_manager] 内部モデル設定の保存に失敗: {e}")
        return False


def reset_internal_model_settings() -> Dict[str, Any]:
    """
    内部処理モデルの設定をデフォルトにリセットする。
    
    Returns:
        リセット後の設定
    """
    default_settings = {
        "provider": "google",
        "processing_model": constants.INTERNAL_PROCESSING_MODEL,
        "summarization_model": constants.SUMMARIZATION_MODEL,
        "supervisor_model": constants.INTERNAL_PROCESSING_MODEL,
        "openai_profile": None,
        "embedding_provider": "gemini",
        "embedding_model": "gemini-embedding-001",
        "fallback_enabled": True,
        "fallback_order": ["google"],
    }
    
    save_internal_model_settings(default_settings)
    return default_settings


def get_effective_internal_model(role: str) -> Tuple[str, str]:
    """
    指定されたロールに応じた内部処理モデルのプロバイダとモデル名を取得する。
    
    Args:
        role: "processing", "summarization", "supervisor" のいずれか
    
    Returns:
        (provider, model_name) のタプル
    """
    settings = get_internal_model_settings()
    
    # ロールごとのプロバイダキー・モデルキーのマッピング
    provider_key_map = {
        "processing": "processing_provider",
        "summarization": "summarization_provider",
        "supervisor": "supervisor_provider",
    }
    model_key_map = {
        "processing": "processing_model",
        "summarization": "summarization_model",
        "supervisor": "supervisor_model",
    }
    
    provider_key = provider_key_map.get(role, "processing_provider")
    model_key = model_key_map.get(role, "processing_model")
    
    # 設定から取得。未設定の場合は全体設定("provider")またはデフォルト値を参照
    provider = settings.get(provider_key, settings.get("provider", "google"))
    model_name = settings.get(model_key, constants.INTERNAL_PROCESSING_MODEL)
    
    return (provider, model_name)

# --- APIキーローテーション関連 ---

def mark_key_as_exhausted(key_name: str):
    """
    指定されたキーを「枯渇（exhausted）」状態としてマークする。
    """
    if not key_name: return
    GEMINI_KEY_STATES[key_name] = {
        'exhausted': True,
        'exhausted_at': time.time()
    }
    save_gemini_key_states()
    print(f"--- [API Key Rotation] Key '{key_name}' marked as EXHAUSTED ---")

def is_key_exhausted(key_name: str) -> bool:
    """
    指定されたキーが現在枯渇状態かどうかを返す。
    一定時間（例: 1時間）経過後に自動復帰させるロジックを含める。
    """
    state = GEMINI_KEY_STATES.get(key_name)
    if not state or not state.get('exhausted'):
        return False
    
    # 自動復帰ロジック (1時間経過でリセット)
    exhausted_at = state.get('exhausted_at', 0)
    if time.time() - exhausted_at > 3600:
        print(f"--- [API Key Rotation] Key '{key_name}' auto-recovered from exhausted state ---")
        GEMINI_KEY_STATES[key_name]['exhausted'] = False
        save_gemini_key_states()
        return False
        
    return True

def clear_exhausted_keys():
    """すべてのキーの枯渇状態を解除する"""
    GEMINI_KEY_STATES.clear()
    save_gemini_key_states()
    print("--- [API Key Rotation] All exhausted states cleared ---")

def get_next_available_gemini_key(current_exhausted_key: str = None, excluded_keys: set = None) -> Optional[str]:
    """
    有効なキーの中から、枯渇していないものを探して返す。
    無料キーを最優先し、すべて枯渇した場合のみ有料キーを使用する。
    """
    if excluded_keys is None:
        excluded_keys = set()
    if current_exhausted_key:
        excluded_keys.add(current_exhausted_key)
        
    config = load_config_file()
    all_valid_keys = [
        k for k, v in GEMINI_API_KEYS.items()
        if v and isinstance(v, str) and not v.startswith("YOUR_API_KEY")
        and k not in excluded_keys
    ]
    
    if not all_valid_keys:
        return None

    paid_key_names = config.get("paid_api_key_names", [])
    free_keys = [k for k in all_valid_keys if k not in paid_key_names]
    paid_keys = [k for k in all_valid_keys if k in paid_key_names]
    
    # フェーズ1: 無料キーの探索
    # 現在選択中のキーが無料ならそれを優先
    last_key_name = config.get("last_api_key_name")
    if last_key_name in free_keys and not is_key_exhausted(last_key_name):
        return last_key_name
        
    for k in free_keys:
        if not is_key_exhausted(k):
            return k
            
    # フェーズ2: 有料キーの探索（無料キー全滅時）
    if last_key_name in paid_keys and not is_key_exhausted(last_key_name):
        return last_key_name

    for k in paid_keys:
        if not is_key_exhausted(k):
            return k
            
    # フェーズ3: 救済措置 (Rescue Strategy) - [2026-02-03 Fix]
    # 全てのキーが枯渇している場合、最も回復している可能性が高い（枯渇してから時間が経過している）キーを再利用する。
    # これにより、一時的な429エラーで全てのキーがロックされるデッドロックを防ぐ。
    
    print("--- [API Key Rotation] All keys are exhausted. Attempting to rescue the oldest one... ---")
    
    candidates = []
    # 除外されていない全ての有効なキーを対象にする
    for k in all_valid_keys:
        state = GEMINI_KEY_STATES.get(k)
        if state and state.get('exhausted'):
            candidates.append((k, state.get('exhausted_at', 0)))
            
    if candidates:
        # exhausted_at (タイムスタンプ) が古い順（昇順）にソート
        candidates.sort(key=lambda x: x[1])
        rescued_key = candidates[0][0]
        rescued_time = candidates[0][1]
        
        elapsed = time.time() - rescued_time
        print(f"--- [API Key Rotation] RESCUED Key '{rescued_key}' (Exhausted {elapsed:.1f}s ago). Retry possible. ---")
        
        # 状態をリセットせずに返す？ -> リセットしないとまた回ってきた時に除外される可能性はあるが、
        # ここで返すことで呼び出し元は使用を試みる。成功すればその後の呼び出しでexhaustedがクリアされるわけではないが、
        # 使用自体は可能。呼び出し元が成功時に明示的にクリアするロジックはないため、
        # 自動復帰(1時間)を待つか、再度ここに来て再利用されるかになる。
        # 本質的な解決として、ここでexhaustedフラグを降ろしてしまう手もあるが、
        # すぐにまた429が出たら意味がない。
        # しかし、RAGManagerなどが「ローテーションしたのに同じキーが来た」と判断するのを防ぐため、
        # 呼び出し元(RAGManager)は `tried_keys` に追加していく。
        # 救済されたキーが `tried_keys` に入っていないなら試行される。
        
        return rescued_key

    # 全滅かつ救済候補なし（ありえないはずだが）
    print("--- [API Key Rotation] CRITICAL: All keys exhausted and NO candidates for rescue! ---")
    return None

def get_key_name_by_value(key_value: str) -> str:
    """APIキーの値を元に、対応するキー設定名を取得する"""
    for k, v in GEMINI_API_KEYS.items():
        if v == key_value:
            return k
    return "Unknown"