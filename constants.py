# constants.py
# アプリケーション全体で共有される定数を定義するファイル

# --- アプリケーション情報 ---
APP_VERSION = "v0.1.0"


import os

# --- ディレクトリとファイル名 ---
ROOMS_DIR = "characters"
ASSETS_DIR = "assets" # ← この行を追加
SAMPLE_PERSONA_DIR = os.path.join(ASSETS_DIR, "sample_persona") # ← この行を追加
PROFILE_IMAGE_FILENAME = "profile.png"
MEMORY_FILENAME = "memory.txt"
NOTEPAD_FILENAME = "notepad.md"
CONFIG_FILE = "config.json"
ALARMS_FILE = "alarms.json"

# --- UIとAPIの挙動に関する定数 ---
REDACTION_RULES_FILE = "redaction_rules.json"
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
EMBEDDING_MODEL = "gemini-embedding-001" # ← この行を追加

# --- ツール専用AIモデル ---
# 検索グラウンディングのように、特定の機能が保証されているモデルを必要とするツールで使用する。
SEARCH_MODEL = "gemini-2.5-flash"
