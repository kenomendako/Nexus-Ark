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
DISCORD_BOT_ENABLED = False
DISCORD_BOT_TOKEN = ""
DISCORD_AUTHORIZED_USER_IDS = []
DISCORD_BOT_LINKED_ROOM = None
AVAILABLE_MODELS_GLOBAL = []
DEFAULT_MODEL_GLOBAL = "gemini-3.1-flash-lite-preview"
NOTIFICATION_SERVICE_GLOBAL = "discord"
NOTIFICATION_WEBHOOK_URL_GLOBAL = None
PUSHOVER_CONFIG = {}
ZHIPU_API_KEY = ""    # [Phase 3] Zhipu AI (GLM-4) 用APIキー
GROQ_API_KEY = ""     # [Phase 3b] Groq用APIキー
MOONSHOT_API_KEY = "" # [Phase 3d] Moonshot AI (Kimi) 用APIキー
LOCAL_MODEL_PATH = "" # [Phase 3c] ローカルLLM (llama.cpp) 用GGUFモデルパス
ANTHROPIC_API_KEY = "" # [Phase 4] Anthropic (Claude) 用APIキー
NIM_API_KEY = ""       # [Phase 4] Nvidia NIM 用APIキー
XAI_API_KEY = ""       # [Phase 4] X.ai (Grok) 用APIキー
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


# --- [2026-02-11 FIX] APIキー名クレンジング ---
def _clean_api_key_name(key_name: Any) -> Any:
    """APIキー名から表示用などの付加情報を除去する（例: 'kenokaicoo (Paid)' -> 'kenokaicoo'）"""
    if isinstance(key_name, str) and " (Paid)" in key_name:
        return key_name.replace(" (Paid)", "").strip()
    return key_name

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
    """APIキーの枯渇状態をファイルから読み込み、GEMINI_KEY_STATESにマージする。
    [2026-02-19 FIX] 起動時は全枯渇マークをクリアする（新セッション＝クリーンスタート）。
    2回目以降の呼び出し（llm_factoryからのload_config経由など）ではスキップする。
    """
    global GEMINI_KEY_STATES
    
    # [2026-02-19 FIX] 初回のみ枯渇マークをクリアし、以降はスキップ
    if hasattr(load_gemini_key_states, '_initialized'):
        return  # 既に初期化済み、枯渇状態をリセットしない
    load_gemini_key_states._initialized = True
    
    if os.path.exists(KEY_STATES_FILE):
        try:
            # 起動時は前セッションの枯渇マークを全クリア
            GEMINI_KEY_STATES.clear()
            save_gemini_key_states()
            print("--- [API Key Rotation] 起動時: 前セッションの枯渇状態を全クリアしました ---")
                
        except Exception as e:
            print(f"警告: {KEY_STATES_FILE} の読み込みに失敗しました: {e}")


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
    # 探索対象のパスリスト（カレント -> 親ディレクトリ）
    # dist/app 構造を考慮
    candidate_paths = [
        constants.CONFIG_FILE,
        os.path.join("..", constants.CONFIG_FILE)
    ]
    
    target_path = None
    for p in candidate_paths:
        if os.path.exists(p):
            target_path = p
            break
            
    if target_path:
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                content = f.read()
            if not content.strip(): # 空ファイルの場合
                raise json.JSONDecodeError("File is empty", "", 0)
            return json.loads(content)
        except (json.JSONDecodeError, IOError):
            print(f"警告: {target_path} が空または破損しています。バックアップからの復元を試みます...")
            # 注意: バックアップからの復元ロジックは常にカレントディレクトリへの復元を試みる
            if _restore_from_backup():
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
    # print(f"[config_manager] save_config_if_changed: key={key}")  # DEBUG
    # print(f"[config_manager]   current_value: {current_value}")  # DEBUG
    # print(f"[config_manager]   new_value: {value}")  # DEBUG
    # print(f"[config_manager]   are_equal: {current_value == value}")  # DEBUG
    
    # 変更チェック
    if current_value == value:
        # print(f"[config_manager]   -> No change, skipping save")  # DEBUG
        return False  # 変更なし

    # 変更があれば保存
    if key == "last_api_key_name":
        value = _clean_api_key_name(value)
    elif key == "paid_api_key_names" and isinstance(value, list):
        value = [_clean_api_key_name(v) for v in value]
        
    config[key] = value
    _save_config_file(config)
    print(f"[config_manager]   -> Saved to file")  # DEBUG
    
    # 【重要】メモリ上の設定も更新して、再起動なしで反映させる
    if CONFIG_GLOBAL is None:
        CONFIG_GLOBAL = {}
    CONFIG_GLOBAL[key] = value
    
    return True

def update_config_keys(updates: Dict[str, Any]) -> bool:
    """
    config.jsonを最新状態から読み直し、指定されたキーだけを差分更新する。
    複数端末で設定画面を開いている場合でも、未変更キーを古いUI値で巻き戻さないための保存API。
    """
    global CONFIG_GLOBAL

    if not updates:
        return "no_change"

    config = load_config_file()
    changed = False
    applied_updates = {}
    for key, value in updates.items():
        if key == "last_api_key_name":
            value = _clean_api_key_name(value)
        elif key == "paid_api_key_names" and isinstance(value, list):
            value = [_clean_api_key_name(v) for v in value]

        if config.get(key) != value:
            config[key] = value
            changed = True
        applied_updates[key] = value

    if not changed:
        return "no_change"

    _save_config_file(config)
    if CONFIG_GLOBAL is None:
        CONFIG_GLOBAL = {}
    CONFIG_GLOBAL.update(applied_updates)
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

def update_pushover_config(user_key: str, app_token: str, preserve_blank: bool = False):
    config = load_config_file()
    if preserve_blank and not user_key:
        user_key = config.get("pushover_user_key", "")
    if preserve_blank and not app_token:
        app_token = config.get("pushover_app_token", "")
    config["pushover_user_key"] = user_key
    config["pushover_app_token"] = app_token
    _save_config_file(config)
    if CONFIG_GLOBAL is not None:
        CONFIG_GLOBAL["pushover_user_key"] = user_key
        CONFIG_GLOBAL["pushover_app_token"] = app_token


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
def fetch_models_from_api(base_url: str, api_key: str = "", free_only: bool = False) -> list[str]:
    """
    OpenAI互換API (/v1/models) からモデルリストを取得する。
    Groq, Ollama, OpenRouter など全てに対応。
    
    Args:
        base_url: プロバイダのベースURL（例: https://api.groq.com/openai/v1）
        api_key: APIキー（Ollamaは不要）
        free_only: 無料モデルのみを取得するか（OpenRouter等で有効）
    
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
        response = requests.get(models_url, headers=headers, timeout=30)
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
        
        if free_only:
            if "openrouter.ai" in base_url.lower():
                models = [m for m in models if m.endswith(":free")]
            # 他のプロバイダで明確な判別基準があればここに追加可能
        
        return sorted(models)
    except Exception as e:
        print(f"[config_manager] モデルリスト取得エラー: {e}")
        return []


def fetch_gemini_models(api_key: str, free_only: bool = False, exclude_special: bool = False) -> list[str]:
    """Gemini API から利用可能なモデルリストを取得する"""
    import requests
    if not api_key:
        return []
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        models = []
        for m in data.get("models", []):
            name = m.get("name", "")
            if name.startswith("models/"):
                model_id = name.replace("models/", "")
                # 有効なモデルのみ抽出 (gemini, learnlm, gemma など)
                if any(kw in model_id.lower() for kw in ["gemini", "learnlm", "gemma"]):
                    # [2026-04-28] 特殊用途モデル（embedding, tts, computer, image, customtools, robotics）の除外オプション
                    if exclude_special:
                        if any(kw in model_id.lower() for kw in ["embedding", "tts", "computer", "image", "customtools", "robotics"]):
                            continue
                    models.append(model_id)
        return sorted(models)
    except Exception as e:
        print(f"[config_manager] Gemini モデルリスト取得エラー: {e}")
        return []


def fetch_anthropic_models(api_key: str) -> list[str]:
    """Anthropic API から利用可能なモデルリストを取得する"""
    import requests
    if not api_key:
        return []
    url = "https://api.anthropic.com/v1/models"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01"
    }
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        # Anthropic のレスポンス形式: {"data": [{"type": "model", "id": "...", "display_name": "..."}, ...]}
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return sorted(models)
    except Exception as e:
        print(f"[config_manager] Anthropic モデルリスト取得エラー: {e}")
        return []


def fetch_image_models(provider: str, base_url: str = "", api_key: str = "", free_only: bool = False) -> list[str]:
    """
    画像生成用モデルリストをAPIから取得する。
    """
    import requests
    
    if provider == "pollinations":
        try:
            # Pollinations.ai の画像モデルリストエンドポイント
            response = requests.get("https://image.pollinations.ai/models", timeout=30)
            response.raise_for_status()
            models = response.json()
            if isinstance(models, list):
                # 既知のモデルも含めてユニークにする
                known_models = ["flux", "zimage", "klein", "gptimage", "kontext", "wan-image", "qwen-image"]
                all_models = list(set(models + known_models))
                return sorted(all_models)
        except Exception as e:
            print(f"[config_manager] Pollinations モデルリスト取得エラー: {e}")
            return []
            
    elif provider == "openai":
        all_models = fetch_models_from_api(base_url, api_key, free_only=free_only)
        # 画像生成に関係ありそうなキーワードでフィルタリング
        image_keywords = ["dall-e", "stable-diffusion", "flux", "image", "sdxl", "diffusion", "pixel", "art", "canvas", "midjourney"]
        image_models = [m for m in all_models if any(kw in m.lower() for kw in image_keywords)]
        
        # フィルタリングして空になった場合は全リストを返す
        return sorted(image_models) if image_models else sorted(all_models)
    
    elif provider == "gemini":
        all_models = fetch_gemini_models(api_key, free_only=free_only, exclude_special=False)
        # 画像生成に関係ありそうなものをフィルタ
        image_keywords = ["image", "vision"]
        image_models = [m for m in all_models if any(kw in m.lower() for kw in image_keywords)]
        return sorted(image_models) if image_models else sorted(all_models)
        
    return []


def get_image_models_for_openai_profile(profile_name: str) -> list[str]:
    """OpenAI互換プロファイル専用の画像モデルリストを取得する"""
    available_image_models = CONFIG_GLOBAL.get("available_image_models", {})
    openai_profiles_models = available_image_models.get("openai_profiles", {})
    return openai_profiles_models.get(profile_name, [])


def save_image_models_for_openai_profile(profile_name: str, models: list[str]):
    """OpenAI互換プロファイル専用の画像モデルリストを保存する"""
    available_image_models = CONFIG_GLOBAL.get("available_image_models", {})
    if "openai_profiles" not in available_image_models:
        available_image_models["openai_profiles"] = {}
    available_image_models["openai_profiles"][profile_name] = models
    save_config_if_changed("available_image_models", available_image_models)


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
                "name": "Ollama (Local)",
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
    global ANTHROPIC_API_KEY, NIM_API_KEY, XAI_API_KEY
    global DISCORD_BOT_ENABLED, DISCORD_BOT_TOKEN, DISCORD_AUTHORIZED_USER_IDS, DISCORD_BOT_LINKED_ROOM
    global LINE_BOT_ENABLED, LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, LINE_AUTHORIZED_USER_IDS, LINE_BOT_LINKED_ROOM


    # [2026-02-11 FIX] APIキーの枯渇状態の読み込みは、GEMINI_KEY_STATESの初期化後に行う
    # ここでの読み込みは削除（ステップ8の直後に移動）

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
                "name": "Ollama (Local)",
                "base_url": "http://localhost:11434/v1",
                "api_key": "ollama",
                "default_model": "phi3.5",
                "tool_use_enabled": False,  # 【ツール不使用モード】Ollamaはデフォルトでツール無効
                "available_models": [
                    # VRAM 4GB対応モデル（低スペックPC向け）
                    "phi3.5",       # 最適！2.5GB、ツール対応
                    "qwen2.5:3b",   # バランス良、ツール対応
                    "gemma",
                    "gemma:2b",
                    "gemma:9b",
                    "gemma:27b",
                    "gemma4:e2b",
                    "gemma4:e4b",
                    "gemma4:26b",
                    "gemma4:31b",
                    "gemma2:2b",    # 超軽量
                    "qwen3.5:2b",   # Qwen3.5 小規模
                    "qwen3.5:4b",   # Qwen3.5 万能軽量
                    "qwen3.5:9b",   # Qwen3.5 中間
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
            "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite-preview"
        ],
        "default_model": "gemini-3.1-flash-lite-preview",
        # --- 画像生成設定（マルチプロバイダ対応）---
        "image_generation_provider": "gemini",  # gemini | openai | pollinations | huggingface | disabled
        "image_generation_model": "gemini-2.5-flash-image",  # 使用するモデル名
        "image_generation_openai_settings": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-image-1"
        },
        # Pollinations.ai 設定
        "pollinations_api_key": "",
        "image_generation_pollinations_model": "flux",
        # Hugging Face 設定
        "huggingface_api_token": "",
        "image_generation_huggingface_model": "black-forest-labs/FLUX.1-schnell",
        "available_image_models": {
            "gemini": ["gemini-2.5-flash-image", "gemini-3.1-flash-image-preview", "gemini-3-pro-image-preview"],
            "openai": ["gpt-image-1", "gpt-image-1.5", "dall-e-3", "dall-e-2"],
            "pollinations": ["flux", "zimage", "klein", "gptimage", "kontext", "wan-image", "qwen-image"],
            "huggingface": ["black-forest-labs/FLUX.1-schnell", "stabilityai/stable-diffusion-xl-base-1.0"]
        }, 
        # --- ユーザー用画像生成プロンプト補助 ---
        "user_image_gen_instruction_templates": [
            {
                "name": "今の情景を画像に",
                "instruction": "今のチャットログの会話の情景を分析し、Stable Diffusionなどの画像生成AIで利用可能な、詳細で美しい英語のプロンプトを1つ生成してください。プロンプトのみを出力してください。"
            }
        ],
        "user_image_gen_selected_template_index": 0,
        "search_provider": constants.DEFAULT_SEARCH_PROVIDER,
        "tavily_api_key": "",  # Tavily検索用APIキー
        "custom_tools_settings": {
            "enabled": True,
            "mcp_servers": [],
            "tool_metadata": {}
        },
        "last_room": "Default",
        "last_model": "gemini-3.1-flash-lite-preview",
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
        "log_backup_rotation_count": 30,
        "periodic_backup_interval": 10800,
        "theme_settings": {
            "active_theme": "nexus_modern", # デフォルトテーマをモダン版に変更
            "custom_themes": {} # config.jsonで管理するカスタムテーマは最初は空
        },
        "watchlist_settings": {
            "notify_on_change": True  # デフォルトで通知有効
        },
        "autonomous_settings": {
            "enabled": False,
            "inactivity_minutes": 120,
            "schedule_cooldown_minutes": 60,
            "quiet_hours_start": "00:00",
            "quiet_hours_end": "07:00",
            "allow_schedule_tool": True,
            "autonomous_guidelines": ""
        },
        "internal_model_settings": {
            "processing_provider_cat": "google",
            "processing_openai_profile": "",
            "processing_model": constants.INTERNAL_PROCESSING_MODEL,
            "summarization_provider_cat": "google",
            "summarization_openai_profile": "",
            "summarization_model": constants.SUMMARIZATION_MODEL,
            "translation_provider_cat": "google",
            "translation_openai_profile": "",
            "translation_model": constants.INTERNAL_PROCESSING_MODEL,
            "embedding_provider": "google",
            "embedding_model": "gemini-embedding-001",
            "fallback_enabled": True
        },
        "local_model_path": "",
        "discord_bot_settings": {
            "enabled": False,
            "token": "",
            "authorized_user_ids": [],
            "linked_room": None
        },
        "line_bot_enabled": False,
        "line_channel_access_token": "",
        "line_channel_secret": "",
        "line_authorized_user_ids": [],
        "line_bot_linked_room": None
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
    obsolete_keywords = ["gemini-1.5", "gemini-2.0", "gemini-3-pro-preview"]

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

    # ステップ4.6：【賢いマージ】available_image_modelsを統合する
    default_image_models = default_config.get("available_image_models", {})
    user_image_models = user_config.get("available_image_models", {})
    merged_image_models = {}
    for provider, models in default_image_models.items():
        u_models = user_image_models.get(provider, [])
        # デフォルトにあるモデルはすべて含め、ユーザーが追加したモデル（もしあれば）もマージする
        merged_image_models[provider] = sorted(list(set(models) | set(u_models)))

    # ステップ5：ユーザー設定を優先しつつ、不足キーを補完
    config = default_config.copy()
    config.update(user_config)
    # 統合したモデルリストとテーマ設定で、最終的な設定を上書き
    config["available_models"] = merged_models
    config["theme_settings"] = final_theme_settings
    config["openai_provider_settings"] = merged_openai_providers
    config["available_image_models"] = merged_image_models
    
    # ステップ4.7：【賢いマージ】内部モデル設定をディープマージ
    default_internal = default_config.get("internal_model_settings", {})
    user_internal = user_config.get("internal_model_settings", {})
    merged_internal = default_internal.copy()
    merged_internal.update(user_internal)
    config["internal_model_settings"] = merged_internal

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
    # [2026-02-11 FIX] 初期化後にファイルから枯渇状態を復元
    load_gemini_key_states()
    TAVILY_API_KEY = config.get("tavily_api_key", "")
    ZHIPU_API_KEY = config.get("zhipu_api_key", "")
    GROQ_API_KEY = config.get("groq_api_key", "")
    MOONSHOT_API_KEY = config.get("moonshot_api_key", "")
    LOCAL_MODEL_PATH = config.get("local_model_path", "")
    ANTHROPIC_API_KEY = config.get("anthropic_api_key", "")
    NIM_API_KEY = config.get("nim_api_key", "")
    XAI_API_KEY = config.get("xai_api_key", "")
    
    discord_settings = config.get("discord_bot_settings", {})
    DISCORD_BOT_ENABLED = discord_settings.get("enabled", False)
    DISCORD_BOT_TOKEN = discord_settings.get("token", "")
    DISCORD_AUTHORIZED_USER_IDS = discord_settings.get("authorized_user_ids", [])
    DISCORD_BOT_LINKED_ROOM = discord_settings.get("linked_room", None)
    
    LINE_BOT_ENABLED = config.get("line_bot_enabled", False)
    LINE_CHANNEL_ACCESS_TOKEN = config.get("line_channel_access_token", "")
    LINE_CHANNEL_SECRET = config.get("line_channel_secret", "")
    LINE_AUTHORIZED_USER_IDS = config.get("line_authorized_user_ids", [])
    LINE_BOT_LINKED_ROOM = config.get("line_bot_linked_room", None)
    
    # OpenAI互換プロバイダーのデフォルト設定を生成・補完
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
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite-preview"
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
            "update_current_log_index": True,
            "update_entity_memory": True,
            "compress_old_episodes": True
        },
        "watchlist_settings": {
            "notify_on_change": True
        },
        "project_explorer": {
            "root_path": "",
            "exclude_dirs": [".git", "venv", "__pycache__", "node_modules", ".agent", ".gemini"],
            "exclude_files": ["*.pyc", ".env", "config.json"]
        },
        "autonomous_settings": {
            "enabled": False,
            "inactivity_minutes": 120,
            "schedule_cooldown_minutes": 60,
            "quiet_hours_start": "00:00",
            "quiet_hours_end": "07:00",
            "allow_schedule_tool": True,
            "autonomous_guidelines": ""
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
            # ルーム個別のプロバイダ設定を先に取得
            room_provider = override_settings.get("provider")

            # [2026-02-19 CLEANUP] 古い設定値がグローバルを上書きし続ける問題への対策
            # ファイルに永続化することで、次回以降のログスパムを防ぐ
            if "enable_api_key_rotation" in override_settings:
                if not room_provider or room_provider == "default":
                    del override_settings["enable_api_key_rotation"]
                    # ファイルに書き戻して永続化
                    try:
                        room_config["override_settings"] = override_settings
                        with open(room_config_path, "w", encoding="utf-8") as f:
                            json.dump(room_config, f, ensure_ascii=False, indent=2)
                        print(f"  [Cleanup] Stale rotation setting permanently removed for room: {room_name}")
                    except Exception as cleanup_err:
                        print(f"  [Cleanup] Failed to persist cleanup for {room_name}: {cleanup_err}")
            
            for k, v in override_settings.items():
                if v is not None and k != "model_name":
                    effective_settings[k] = v
            # ルーム個別のモデル設定を一時保存（後のロジックで使用）
            room_model_name = override_settings.get("model_name")
            room_zhipu_model = override_settings.get("zhipu_model")
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
    
    elif active_provider == "anthropic":
        # Anthropic (Claude) モード
        room_anthropic_settings = effective_settings.get("anthropic_settings")
        if room_anthropic_settings and room_anthropic_settings.get("model"):
            effective_settings["model_name"] = room_anthropic_settings["model"]
        else:
            effective_settings["model_name"] = CONFIG_GLOBAL.get("anthropic_default_model", "claude-3-7-sonnet-20250219")
            
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
    
    # [2026-02-11 FIX] allow_custom_value=False のため、選択肢が空だとエラーになるのを防ぐ
    if not choices:
        choices.append(("（APIキー未設定）", ""))
        
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

# --- [APIキーローテーション用のメモリ上変数] ---
_CURRENT_STARTING_KEY_NAME = None


def get_active_gemini_api_key(room_name: str = None, model_name: str = None, excluded_keys: set = None) -> Optional[str]:
    """
    指定されたルームの設定（またはグローバル設定）に基づいて、
    現在有効な Gemini API キーの『値（文字列）』を直接返す。
    キーが設定されていない場合は None を返す。
    """
    # model_name が指定されていない場合は、デフォルトの内部処理モデルを使用
    if not model_name:
        model_name = constants.INTERNAL_PROCESSING_MODEL

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

                # [2026-04-29] 画像生成モデルの場合、無料キーでのローテーションは意味がない（全滅するため）
                # かつ、ユーザーが意図しないキーの切り替えを防ぐため、個別設定がなければローテーションを無効化する
                if is_image_generation_model(model_name):
                    rotation_enabled_room = False

                # プロバイダ設定を確認（Noneの場合は共通設定に従うため、個別キー設定も無視する）
                room_provider = override_settings.get("provider")
                
                if room_provider is not None:
                    room_api_key_name = _clean_api_key_name(override_settings.get("api_key_name"))
                    if room_api_key_name:
                        key_val = GEMINI_API_KEYS.get(room_api_key_name)
                        if key_val and not key_val.startswith("YOUR_API_KEY"):
                            # キーが枯渇しているかチェック
                            if rotation_enabled_room and is_key_exhausted(room_api_key_name, model_name=model_name):
                                if excluded_keys is None: excluded_keys = set()
                                excluded_keys.add(room_api_key_name)
                                print(f"Warning: Room key '{room_api_key_name}' is exhausted for model '{model_name}'. Falling back to common pool.")
                                # フォールバック: 下記の共通設定ロジックへ流れる
                            else:
                                return key_val
            except Exception:
                pass

    # 共通設定でのローテーション確認
    # ルーム指定がない、またはルーム設定でフォールバックした場合
    rotation_enabled = rotation_enabled_room if room_name else rotation_enabled_global
    
    # [2026-04-29] グローバルな画像生成モデル判定
    if is_image_generation_model(model_name):
        rotation_enabled = False

    # [2026-02-11 FIX] ユーザー選択キーを優先する
    # メモリ上の CONFIG_GLOBAL から優先的に取得（UIの状態を即座に反映）
    global _CURRENT_STARTING_KEY_NAME
    
    key_name = None
    if isinstance(CONFIG_GLOBAL, dict):
        key_name = _clean_api_key_name(CONFIG_GLOBAL.get("last_api_key_name"))
    
    if not key_name:
        key_name = _clean_api_key_name(get_latest_api_key_name_from_config())

    # --- 起点となるキーを決定 ---
    candidate_key = _CURRENT_STARTING_KEY_NAME if _CURRENT_STARTING_KEY_NAME else key_name

    if candidate_key:
        key_val = GEMINI_API_KEYS.get(candidate_key)
        if key_val and not key_val.startswith("YOUR_API_KEY"):
            if not is_key_exhausted(candidate_key, model_name=model_name):
                # 起点キーが有効
                paid_keys = set(CONFIG_GLOBAL.get("paid_api_key_names", [])) if isinstance(CONFIG_GLOBAL, dict) else set()
                if candidate_key not in paid_keys:
                    _CURRENT_STARTING_KEY_NAME = candidate_key
                else:
                    _CURRENT_STARTING_KEY_NAME = None
                return key_val
            elif rotation_enabled:
                # 起点キーが枯渇 → ローテーションで代替キーを探す
                if excluded_keys is None: excluded_keys = set()
                excluded_keys.add(candidate_key)
                print(f"--- [API Key Rotation] キー '{candidate_key}' は対象モデル({model_name})でクールダウン中のため、API呼び出しを省略して次候補へ進みます。 ---")
                
                alt_key = get_next_available_gemini_key(current_exhausted_key=candidate_key, excluded_keys=excluded_keys, model_name=model_name)
                if alt_key:
                    paid_keys = set(CONFIG_GLOBAL.get("paid_api_key_names", [])) if isinstance(CONFIG_GLOBAL, dict) else set()
                    if alt_key not in paid_keys:
                        _CURRENT_STARTING_KEY_NAME = alt_key
                    else:
                        _CURRENT_STARTING_KEY_NAME = None
                    return GEMINI_API_KEYS.get(alt_key)
                # 代替も見つからない場合、起点キーをそのまま返す（rescue strategyで対応）
                return key_val
            else:
                # ローテーション無効 → 枯渇していても起点キーを返す
                return key_val

    # キー名が設定されていない場合のフォールバック
    if rotation_enabled:
        available_key_name = get_next_available_gemini_key(excluded_keys=excluded_keys, model_name=model_name)
        if available_key_name:
            paid_keys = set(CONFIG_GLOBAL.get("paid_api_key_names", [])) if isinstance(CONFIG_GLOBAL, dict) else set()
            if available_key_name not in paid_keys:
                _CURRENT_STARTING_KEY_NAME = available_key_name
            else:
                _CURRENT_STARTING_KEY_NAME = None
            return GEMINI_API_KEYS.get(available_key_name)

    return None


def get_active_gemini_api_key_name(room_name: str = None, model_name: str = None, excluded_keys: set = None) -> Optional[str]:
    """
    指定されたルームの設定（またはグローバル設定）に基づいて、
    現在有効な Gemini API キーの『名称』を返す。
    キーが設定されていない場合は None を返す。
    """
    # model_name が指定されていない場合は、デフォルトの内部処理モデルを使用
    if not model_name:
        model_name = constants.INTERNAL_PROCESSING_MODEL

    rotation_enabled_global = CONFIG_GLOBAL.get("enable_api_key_rotation", True)
    
    # [2026-04-29] 画像生成モデル判定
    if is_image_generation_model(model_name):
        rotation_enabled_global = False

    rotation_enabled_room = rotation_enabled_global 
    
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
                
                # [2026-04-29] 画像生成モデル判定（ルーム個別）
                if is_image_generation_model(model_name):
                    rotation_enabled_room = False

                # プロバイダ設定を確認
                room_provider = override_settings.get("provider")
                
                if room_provider is not None:
                    room_api_key_name = _clean_api_key_name(override_settings.get("api_key_name"))
                    if room_api_key_name:
                        # キー自体が存在することを確認
                        key_val = GEMINI_API_KEYS.get(room_api_key_name)
                        if key_val and not key_val.startswith("YOUR_API_KEY"):
                            # キーが枯渇しているかチェック
                            if rotation_enabled_room and is_key_exhausted(room_api_key_name, model_name=model_name):
                                if excluded_keys is None: excluded_keys = set()
                                excluded_keys.add(room_api_key_name)
                                alt_key = get_next_available_gemini_key(excluded_keys=excluded_keys, model_name=model_name)
                                if alt_key:
                                    print(f"  - [Rotation] Room key '{room_api_key_name}' is exhausted for model '{model_name}'. Rotating to '{alt_key}'.")
                                    return alt_key
                                return room_api_key_name # フォールバックしても見つかららなければ元の名前を返す
                            else:
                                return room_api_key_name
            except Exception:
                pass

    rotation_enabled = rotation_enabled_room if room_name else rotation_enabled_global

    # [2026-02-11 FIX] ユーザー選択キーを優先する
    global _CURRENT_STARTING_KEY_NAME

    key_name = None
    if isinstance(CONFIG_GLOBAL, dict):
        key_name = _clean_api_key_name(CONFIG_GLOBAL.get("last_api_key_name"))
    
    if not key_name:
        key_name = _clean_api_key_name(get_latest_api_key_name_from_config())

    # --- 起点となるキーを決定 ---
    candidate_key = _CURRENT_STARTING_KEY_NAME if _CURRENT_STARTING_KEY_NAME else key_name

    if candidate_key:
        key_val = GEMINI_API_KEYS.get(candidate_key)
        if key_val and not key_val.startswith("YOUR_API_KEY"):
            if not is_key_exhausted(candidate_key, model_name=model_name):
                paid_keys = set(CONFIG_GLOBAL.get("paid_api_key_names", [])) if isinstance(CONFIG_GLOBAL, dict) else set()
                if candidate_key not in paid_keys:
                    _CURRENT_STARTING_KEY_NAME = candidate_key
                else:
                    _CURRENT_STARTING_KEY_NAME = None
                return candidate_key  # 起点キーが有効
            elif rotation_enabled:
                if excluded_keys is None: excluded_keys = set()
                excluded_keys.add(candidate_key)
                print(f"--- [API Key Rotation] キー '{candidate_key}' は対象モデル({model_name})でクールダウン中のため、API呼び出しを省略して次候補へ進みます。 ---")
                
                alt_key = get_next_available_gemini_key(current_exhausted_key=candidate_key, excluded_keys=excluded_keys, model_name=model_name)
                if alt_key:
                    paid_keys = set(CONFIG_GLOBAL.get("paid_api_key_names", [])) if isinstance(CONFIG_GLOBAL, dict) else set()
                    if alt_key not in paid_keys:
                        _CURRENT_STARTING_KEY_NAME = alt_key
                    else:
                        _CURRENT_STARTING_KEY_NAME = None
                    return alt_key
                return candidate_key  # 代替なし → 起点キーを返す
            else:
                return candidate_key  # ローテーション無効

    # キー名が設定されていない場合のフォールバック
    if rotation_enabled:
        available_key_name = get_next_available_gemini_key(excluded_keys=excluded_keys, model_name=model_name)
        if available_key_name:
            paid_keys = set(CONFIG_GLOBAL.get("paid_api_key_names", [])) if isinstance(CONFIG_GLOBAL, dict) else set()
            if available_key_name not in paid_keys:
                _CURRENT_STARTING_KEY_NAME = available_key_name
            else:
                _CURRENT_STARTING_KEY_NAME = None
            return available_key_name

    return key_name


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

# --- [Phase 4] 追加プロバイダのAPIキー保存関数 ---

def save_single_api_key(key_name: str, key_value: str, config_key: str):
    """
    Anthropic, Zhipu, Groqなどの単一APIキーを保存する統合ハンドラ
    """
    global ZHIPU_API_KEY, GROQ_API_KEY, MOONSHOT_API_KEY, LOCAL_MODEL_PATH
    global ANTHROPIC_API_KEY, NIM_API_KEY, XAI_API_KEY
    
    if config_key == "zhipu_api_key": ZHIPU_API_KEY = key_value
    elif config_key == "groq_api_key": GROQ_API_KEY = key_value
    elif config_key == "moonshot_api_key": MOONSHOT_API_KEY = key_value
    elif config_key == "anthropic_api_key": ANTHROPIC_API_KEY = key_value
    elif config_key == "nim_api_key": NIM_API_KEY = key_value
    elif config_key == "xai_api_key": XAI_API_KEY = key_value
    elif config_key == "local_model_path": LOCAL_MODEL_PATH = key_value
        
    save_config_if_changed(config_key, key_value)

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
                if room_provider and room_provider in ["google", "openai", "local", "anthropic", "zhipu"]:
                    return room_provider
            except Exception:
                pass
    # フォールバック: グローバル設定
    return CONFIG_GLOBAL.get("active_provider", "google")

def set_active_provider(provider: str):
    """アクティブなプロバイダを切り替える"""
    if provider in ["google", "openai", "local", "anthropic", "zhipu"]:
        save_config_if_changed("active_provider", provider)

def get_openai_settings_list() -> List[Dict]:
    """OpenAI互換プロバイダの設定リストを返す"""
    return CONFIG_GLOBAL.get("openai_provider_settings", [])

def save_openai_settings_list(settings_list: List[Dict]):
    """OpenAI互換プロバイダの設定リストを保存する"""
    if isinstance(settings_list, list):
        save_config_if_changed("openai_provider_settings", settings_list)

def add_or_update_openai_profile(profile_data: Dict):
    """
    OpenAI互換プロファイルを新規追加、または既存のものを上書き更新する。
    profile_dataには少なくとも 'name', 'base_url', 'api_key' が必要。
    """
    if "name" not in profile_data:
        return False
        
    settings = get_openai_settings_list()
    updated = False
    
    for i, s in enumerate(settings):
        if s.get("name") == profile_data["name"]:
            # 既存の設定を上書き（available_models等は保持しつつ更新）
            merged = s.copy()
            merged.update(profile_data)
            settings[i] = merged
            updated = True
            break
            
    if not updated:
        # 新規プロファイルとして追加
        if "available_models" not in profile_data:
            profile_data["available_models"] = []
        if "default_model" not in profile_data:
            profile_data["default_model"] = ""
        settings.append(profile_data)
        
    save_openai_settings_list(settings)
    return True

def save_openai_provider_setting(name: str, base_url: str, api_key: str, available_models: list = None, default_model: str = "", tool_use_enabled: bool = True):
    """
    OpenAI互換プロファイルを設定として追加・更新する便利なラッパー関数
    """
    profile_data = {
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "available_models": available_models or [],
        "default_model": default_model,
        "tool_use_enabled": tool_use_enabled
    }
    return add_or_update_openai_profile(profile_data)

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
        elif target_setting.get("name") == "Pollinations.ai":
            global_key = CONFIG_GLOBAL.get("pollinations_api_key")
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
        # 処理モデル設定
        "processing_provider_cat": "google",
        "processing_openai_profile": "",
        "processing_model": constants.INTERNAL_PROCESSING_MODEL,
        
        # 要約モデル設定
        "summarization_provider_cat": "google",
        "summarization_openai_profile": "",
        "summarization_model": constants.SUMMARIZATION_MODEL,
        
        # 翻訳モデル設定
        "translation_provider_cat": "google",
        "translation_openai_profile": "",
        "translation_model": constants.INTERNAL_PROCESSING_MODEL,
        
        # エンベディング設定
        "embedding_provider": "google",
        "embedding_model": "gemini-embedding-001",
        
        # フォールバック設定
        "fallback_enabled": True,
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
        "translation_provider": "google",
        "translation_model": constants.INTERNAL_PROCESSING_MODEL,
        "openai_profile": None,
        "embedding_provider": "google",
        "embedding_model": "gemini-embedding-001",
        "fallback_enabled": True,
        "fallback_order": ["google"],
    }
    
    save_internal_model_settings(default_settings)
    return default_settings


def get_effective_internal_model(role: str) -> Tuple[str, str, str]:
    """
    指定されたロールに応じた内部処理モデルのプロバイダ名、モデル名、およびプロファイル名を取得する。
    
    Args:
        role: "processing", "summarization", "supervisor", "translation" のいずれか
    
    Returns:
        (provider_cat, model_name, profile_name) のタプル
    """
    settings = get_internal_model_settings()
    
    # ロールごとのキーマッピング
    cat_key_map = {
        "processing": "processing_provider_cat",
        "summarization": "summarization_provider_cat",
        "supervisor": "supervisor_provider_cat",
        "translation": "translation_provider_cat",
    }
    profile_key_map = {
        "processing": "processing_openai_profile",
        "summarization": "summarization_openai_profile",
        "supervisor": "supervisor_openai_profile",
        "translation": "translation_openai_profile",
    }
    model_key_map = {
        "processing": "processing_model",
        "summarization": "summarization_model",
        "supervisor": "supervisor_model",
        "translation": "translation_model",
    }
    
    # 旧形式の互換性維持 (provider_cat が無い場合は provider を見る)
    legacy_provider_key_map = {
        "processing": "processing_provider",
        "summarization": "summarization_provider",
        "supervisor": "supervisor_provider",
        "translation": "translation_provider",
    }

    cat_key = cat_key_map.get(role)
    profile_key = profile_key_map.get(role)
    model_key = model_key_map.get(role)
    legacy_key = legacy_provider_key_map.get(role)
    
    provider_cat = settings.get(cat_key)
    if not provider_cat:
        # 旧形式からの移行: 旧 provider が openai なら、それをプロファイル名として扱う
        old_provider = settings.get(legacy_key, "google")
        
        # 表示名から内部値へのマッピング
        label_to_cat = {
            "Google (Gemini)": "google",
            "Google (Gemini Native)": "google",
            "OpenAI (公式)": "openai_official",
            "OpenAI互換": "openai",
            "OpenAI互換 (OpenRouter / Groq / Ollama / Zhipu AI)": "openai",
            "Anthropic (Claude)": "anthropic",
            "ローカル (GGUF直接ロード)": "local",
            "ローカル(llama.cpp/GGUF)": "local",
            "Local (llama.cpp)": "local"
        }
        
        if old_provider in ["google", "openai", "openai_official", "anthropic", "local"]:
            provider_cat = old_provider
        elif old_provider in label_to_cat:
            provider_cat = label_to_cat[old_provider]
        else:
            # プロファイル名が入っていると推測される場合
            provider_cat = "openai"
            settings[profile_key] = old_provider

    profile_name = settings.get(profile_key, CONFIG_GLOBAL.get("active_openai_profile", "OpenRouter"))
    model_name = settings.get(model_key, constants.INTERNAL_PROCESSING_MODEL)
    
    return (provider_cat, model_name, profile_name)
    

# --- APIキーローテーション関連 ---


def is_key_exhausted(key_name: str, model_name: str = None) -> bool:
    """
    指定されたキー（および必要に応じて特定のモデル）が現在枯渇状態かどうかを返す。
    model_nameがNoneの場合は、そのキー自体のグローバルな枯渇状態（旧形式）または
    何らかのモデルで枯渇しているかを確認する。
    """
    key_name = _clean_api_key_name(key_name)
    
    # 探索対象のステートキーを決定
    state_keys = []
    if model_name:
        state_keys.append(f"{key_name}@{model_name}")
    state_keys.append(f"{key_name}@*") # ワイルドカード（全体）
    state_keys.append(key_name)         # 旧形式（互換性用）
    
    state = None
    applied_state_key = None
    for sk in state_keys:
        if sk in GEMINI_KEY_STATES:
            state = GEMINI_KEY_STATES[sk]
            applied_state_key = sk
            break
            
    if not state or not state.get('exhausted'):
        return False
    
    # 有料キーは枯渇マークされないはずだが、念のためチェック
    paid_keys = set()
    if isinstance(CONFIG_GLOBAL, dict):
        paid_keys = set(CONFIG_GLOBAL.get("paid_api_key_names", []))
    if key_name in paid_keys:
        if applied_state_key:
            GEMINI_KEY_STATES[applied_state_key]['exhausted'] = False
        save_gemini_key_states()
        return False
    
    # 無料キーの自動復帰ロジック
    exhausted_at = state.get('exhausted_at', 0)
    limit_type = state.get('limit_type', 'RPM')
    
    if limit_type == "RPD":
        reset_timestamp = state.get('reset_at', 0)
        # 後方互換性と安全のため、reset_at が無い場合は24時間とする
        if not reset_timestamp:
            reset_timestamp = exhausted_at + 24 * 3600
            
        if time.time() < reset_timestamp:
            return True # まだRPDリセット時刻（太平洋時間0時）になっていない
        else:
            print(f"--- [API Key Rotation] Key '{applied_state_key}' auto-recovered (Daily Reset) ---")
            GEMINI_KEY_STATES[applied_state_key]['exhausted'] = False
            save_gemini_key_states()
            return False
    else:
        # RPM制限の自動復帰（1分クールダウン）
        if time.time() - exhausted_at > 60:  # 1分 (GoogleのRPM制限を考慮)
            print(f"--- [API Key Rotation] Key '{applied_state_key}' auto-recovered (1分経過/RPM回復) ---")
            GEMINI_KEY_STATES[applied_state_key]['exhausted'] = False
            save_gemini_key_states()
            return False
        
    return True

def mark_key_as_exhausted(key_name: str, model_name: str = None, limit_type: str = "RPM"):
    """キー（および特定のモデル）を枯渇状態としてマークし、制限の種類を記録する。"""
    key_name = _clean_api_key_name(key_name)
    if not key_name: return
    
    # 有料キーは枯渇マークしない（リトライのバックオフだけで対応）
    paid_keys = set()
    if isinstance(CONFIG_GLOBAL, dict):
        paid_keys = set(CONFIG_GLOBAL.get("paid_api_key_names", []))
    if key_name in paid_keys:
        print(f"--- [API Key Rotation] Key '{key_name}' is PAID - skipping exhaustion mark (backoff only) ---")
        return
    
    # [2026-04-29] 画像生成モデルの場合、無料キーは常に失敗するため、枯渇マークをスキップする
    # (他機能への影響を防ぐため)
    if is_image_generation_model(model_name):
        print(f"--- [API Key Rotation] Key '{key_name}' failed for image model, skipping exhaustion mark for FREE key ---")
        return
    
    state_key = f"{key_name}@{model_name}" if model_name else key_name
    
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    # Google APIのリセット時刻: 太平洋時間0時 (UTC 08:00 または 07:00)。安全のため遅い方の08:00を使用
    reset_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now >= reset_time:
        reset_time += datetime.timedelta(days=1)
    
    GEMINI_KEY_STATES[state_key] = {
        'exhausted': True,
        'exhausted_at': time.time(),
        'limit_type': limit_type,
        'reset_at': reset_time.timestamp() if limit_type == "RPD" else 0
    }
    save_gemini_key_states()
    
    if limit_type == "RPD":
        print(f"--- [API Key Rotation] Key '{state_key}' marked as EXHAUSTED (RPD Limit: until {reset_time.strftime('%Y-%m-%d %H:%M:%S')} UTC) ---")
    else:
        print(f"--- [API Key Rotation] Key '{state_key}' marked as EXHAUSTED (RPM Limit: 1min cooldown) ---")

def clear_exhausted_keys():
    """すべてのキーの枯渇状態を解除する"""
    GEMINI_KEY_STATES.clear()
    save_gemini_key_states()
    print("--- [API Key Rotation] All exhausted states cleared ---")

def get_next_available_gemini_key(current_exhausted_key: str = None, excluded_keys: set = None, model_name: str = None) -> Optional[str]:
    """
    有効なキーの中から、枯渇していないものを探して返す。
    探索順序（コストと安定性のバランス）:
    1. 無料キー（未試行 かつ 非枯渇）
    2. 有料キー（未試行 かつ 非枯渇） ※以前は無料枯渇後に有料だったが、ここでは「未試行」を優先
    3. 無料キー（救済: 最も古い枯渇キー）
    4. 有料キー（救済: 最も古い枯渇キー）
    """
    if excluded_keys is None:
        excluded_keys = set()
    
    # 現在の枯渇キーを明示的に除外リストに追加
    if current_exhausted_key:
        excluded_keys.add(current_exhausted_key)
        
    config = load_config_file()
    paid_key_names = set(config.get("paid_api_key_names", []))
    
    # 全有効キーのリスト
    all_valid_keys = [
        k for k, v in GEMINI_API_KEYS.items()
        if v and isinstance(v, str) and not v.startswith("YOUR_API_KEY")
    ]
    
    if not all_valid_keys:
        return None

    # current_exhausted_key があれば、その次から探索するようにリストをシフトする（巻き戻り防止）
    if current_exhausted_key and current_exhausted_key in all_valid_keys:
        idx = all_valid_keys.index(current_exhausted_key)
        all_valid_keys = all_valid_keys[idx+1:] + all_valid_keys[:idx+1]

    # --- フェーズ1: 未試行かつ非枯渇のキーを探す ---
    # (無料 -> 有料 の順で、まだ今回のリトライループで試していないものを優先)
    untried_keys = [k for k in all_valid_keys if k not in excluded_keys]
    
    # 無料キー(未試行)
    free_untried = [k for k in untried_keys if k not in paid_key_names]
    for k in free_untried:
        if not is_key_exhausted(k, model_name):
            return k
            
    # 有料キー(未試行)
    paid_untried = [k for k in untried_keys if k in paid_key_names]
    for k in paid_untried:
        if not is_key_exhausted(k, model_name):
            return k
            
    # --- フェーズ2: 救済ロジック (Rescue Strategy) ---
    # 全ての未試行キーが枯渇しているか、全てのキーを試し終えた場合
    print(f"--- [API Key Rotation] All candidates for model '{model_name}' are exhausted or tried. Attempting rescue... ---")
    
    # 無料キーの救済を最優先（コスト保護）
    candidates_free = []
    candidates_paid = []
    
    for k in all_valid_keys:
        # [2026-04-23 FIX] 今回の試行サイクル(excluded_keys)ですでに試したキーは救済対象からも外す
        if k in excluded_keys:
            continue
            
        # ステートキーを確認
        state_key = f"{k}@{model_name}" if model_name else k
        
        state = None
        if state_key in GEMINI_KEY_STATES:
            state = GEMINI_KEY_STATES[state_key]
        elif k in GEMINI_KEY_STATES: # 旧形式
            state = GEMINI_KEY_STATES[k]
            
        if state and state.get('exhausted'):
            if k in paid_key_names: candidates_paid.append((k, state.get('exhausted_at', 0)))
            else: candidates_free.append((k, state.get('exhausted_at', 0)))
        else:
            # 枯渇していない、またはステートなし (フェーズ1で見落とされた可能性のあるもの)
            if k in paid_key_names: candidates_paid.append((k, 0))
            else: candidates_free.append((k, 0))

    # --- Rescue Strategy (救済策) ---
    # 候補が全滅している場合、最後に記録された使用可能時刻が最も古いキーを強制的に1つ返す
    # (バックオフ時間が経過している可能性があるため)
    
    # 1. 無料キーの救済を最優先（コスト保護）
    if candidates_free:
        candidates_free.sort(key=lambda x: x[1])
        rescued_key = candidates_free[0][0]
        rescued_time = candidates_free[0][1]
        elapsed = time.time() - rescued_time if rescued_time > 0 else 999999
        print(f"--- [API Key Rotation] RESCUED FREE Key '{rescued_key}' (Exhausted {elapsed:.1f}s ago). ---")
        return rescued_key

    # 2. 無料キーがない場合のみ有料キーを救済（最終防波堤）
    if candidates_paid:
        candidates_paid.sort(key=lambda x: x[1])
        rescued_key = candidates_paid[0][0]
        rescued_time = candidates_paid[0][1]
        elapsed = time.time() - rescued_time if rescued_time > 0 else 999999
        print(f"--- [API Key Rotation] RESCUED PAID Key '{rescued_key}' (Exhausted {elapsed:.1f}s ago). ---")
        return rescued_key

    print(f"--- [API Key Rotation] CRITICAL: No candidates for model '{model_name}' rescue! ---")
    return None

def save_discord_bot_settings(enabled: bool = None, token: str = None, authorized_user_ids: List[str] = None, linked_room: str = None):
    """Discord Botの設定を保存する"""
    global CONFIG_GLOBAL
    settings = CONFIG_GLOBAL.get("discord_bot_settings", {})
    
    if enabled is not None: settings["enabled"] = enabled
    if token is not None: settings["token"] = token
    if authorized_user_ids is not None: settings["authorized_user_ids"] = authorized_user_ids
    if linked_room is not None: settings["linked_room"] = linked_room
    
    CONFIG_GLOBAL["discord_bot_settings"] = settings
    _save_config_file(CONFIG_GLOBAL)
    
def save_line_bot_settings(enabled: bool = None, token: str = None, secret: str = None, authorized_user_ids: List[str] = None, linked_room: str = None):
    """LINE Botの設定を保存する"""
    global CONFIG_GLOBAL
    
    if enabled is not None: CONFIG_GLOBAL["line_bot_enabled"] = enabled
    if token is not None: CONFIG_GLOBAL["line_channel_access_token"] = token
    if secret is not None: CONFIG_GLOBAL["line_channel_secret"] = secret
    if authorized_user_ids is not None: CONFIG_GLOBAL["line_authorized_user_ids"] = authorized_user_ids
    if linked_room is not None: CONFIG_GLOBAL["line_bot_linked_room"] = linked_room
    
    _save_config_file(CONFIG_GLOBAL)
    _save_config_file(CONFIG_GLOBAL)
    load_config() # グローバル変数を再反映

def is_image_generation_model(model_name: str) -> bool:
    """指定されたモデル名が画像生成用（Imagen等）であるか判定する"""
    if not model_name: return False
    # Google SDKのImagenモデルは通常名前に "image" を含む
    # 例: gemini-2.5-flash-image, imagen-3.0-generate-001
    return "image" in model_name.lower()

def get_key_name_by_value(key_value: str) -> str:
    """APIキーの値を元に、対応するキー設定名を取得する"""
    for k, v in GEMINI_API_KEYS.items():
        if v == key_value:
            return k
    return "Unknown"
