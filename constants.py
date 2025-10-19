# constants.py
# アプリケーション全体で共有される定数を定義するファイル

# --- アプリケーション情報 ---
APP_VERSION = "v0.1.0"

import os

# --- ディレクトリとファイル名 ---
ROOMS_DIR = "characters"
ASSETS_DIR = "assets"
SAMPLE_PERSONA_DIR = os.path.join(ASSETS_DIR, "sample_persona")
PROFILE_IMAGE_FILENAME = "profile.png"
MEMORY_FILENAME = "memory.txt"
NOTEPAD_FILENAME = "notepad.md"
CONFIG_FILE = "config.json"
ALARMS_FILE = "alarms.json"
REDACTION_RULES_FILE = "redaction_rules.json"

# --- UIとAPIの挙動に関する定数 ---
# (以降、変更なし)
UI_HISTORY_MAX_LIMIT = 200
API_HISTORY_LIMIT_OPTIONS = {"10": "10往復", "20": "20往復", "30": "30往復", "40": "40往復", "50": "50往復", "60": "60往復", "70": "70往復", "80": "80往復", "90": "90往復", "100": "100往復", "all": "全ログ"}
DEFAULT_API_HISTORY_LIMIT_OPTION = "all"
DEFAULT_ALARM_API_HISTORY_TURNS = 10

# --- 内部処理用AIモデル ---
INTERNAL_PROCESSING_MODEL = "gemini-2.5-flash-lite"
EMBEDDING_MODEL = "gemini-embedding-001"

# --- ツール専用AIモデル ---
SEARCH_MODEL = "gemini-2.5-flash"