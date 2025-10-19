# constants.py (v-final: Self-aware paths)
import os

# === [CRITICAL FIX] ===
# Define the absolute base directory of the app's source code.
# This ensures that all relative paths are resolved correctly,
# regardless of where the script is executed from (e.g., package root).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# === [END CRITICAL FIX] ===


# --- アプリケーション情報 ---
APP_VERSION = "v0.1.0"

# --- ディレクトリとファイル名 (All paths are now absolute) ---
ROOMS_DIR = os.path.join(BASE_DIR, "characters")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
SAMPLE_PERSONA_DIR = os.path.join(ASSETS_DIR, "sample_persona")

PROFILE_IMAGE_FILENAME = "profile.png"
MEMORY_FILENAME = "memory.txt"
NOTEPAD_FILENAME = "notepad.md"
# Note: These config files are in the package root, not the app directory.
# The logic in config_manager.py should handle this if it's also made path-aware.
# For now, assuming they are relative to the execution directory, not BASE_DIR.
CONFIG_FILE = "config.json"
ALARMS_FILE = "alarms.json"
REDACTION_RULES_FILE = "redaction_rules.json"

# --- UIとAPIの挙動に関する定数 ---
# GradioのUIに一度に表示する最大の履歴数
UI_HISTORY_MAX_LIMIT = 200
# APIに渡す履歴の長さの選択肢
API_HISTORY_LIMIT_OPTIONS = {"10": "10往復", "20": "20往復", "30": "30往復", "40": "40往復", "50": "50往復", "60": "60往復", "70": "70往復", "80": "80往復", "90": "90往復", "100": "100往復", "all": "全ログ"}
# API履歴のデフォルト設定
DEFAULT_API_HISTORY_LIMIT_OPTION = "all"
# アラーム機能が内部でAPIを呼び出す際の履歴数
DEFAULT_ALARM_API_HISTORY_TURNS = 10

# --- 内部処理用AIモデル ---
# 情景生成や記憶の要約など、アプリケーション内部の高速処理タスクで使用するモデル。
# これを一元管理することで、将来のモデル変更が容易になり、意図しない変更を防ぐ。
INTERNAL_PROCESSING_MODEL = "gemini-2.5-flash-lite"
EMBEDDING_MODEL = "gemini-embedding-001"

# --- ツール専用AIモデル ---
# 検索グラウンディングのように、特定の機能が保証されているモデルを必要とするツールで使用する。
SEARCH_MODEL = "gemini-2.5-flash"