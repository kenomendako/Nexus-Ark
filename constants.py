# constants.py
# アプリケーション全体で共有される定数を定義するファイル

# --- アプリケーション情報 ---
APP_VERSION = "v0.9.0 (Beta)"

import os

# --- ディレクトリとファイル名 ---
ROOMS_DIR = "characters"
ASSETS_DIR = "assets"
SAMPLE_PERSONA_DIR = os.path.join(ASSETS_DIR, "sample_persona")
AVATAR_DIR = "avatar"  # キャラクターフォルダ内のアバター動画用ディレクトリ
PROFILE_IMAGE_FILENAME = "profile.png"
MEMORY_FILENAME = "memory.txt"
NOTEPAD_FILENAME = "notepad.md"
RESEARCH_NOTES_FILENAME = "research_notes.md"  # Phase 3: 研究・分析ノート
CONFIG_FILE = "config.json"
ALARMS_FILE = "alarms.json"
REDACTION_RULES_FILE = "redaction_rules.json"
NOTES_DIR_NAME = "notes"
CREATIVE_NOTES_FILENAME = "creative_notes.md"
NOTES_MAX_SIZE_BYTES = 200 * 1024  # 200KB
LOGS_DIR_NAME = "logs"             # [NEW] チャットログ分割用フォルダ


# --- UIとAPIの挙動に関する定数 ---
# (以降、変更なし)
UI_HISTORY_MAX_LIMIT = 200
API_HISTORY_LIMIT_OPTIONS = {"today": "本日分", "1": "1往復", "3": "3往復", "5": "5往復", "10": "10往復", "20": "20往復", "30": "30往復", "40": "40往復", "50": "50往復", "60": "60往復", "70": "70往復", "80": "80往復", "90": "90往復", "100": "100往復", "all": "全ログ"}
DEFAULT_API_HISTORY_LIMIT_OPTION = "20"
DEFAULT_ALARM_API_HISTORY_TURNS = 10

# --- 自律行動設定 ---
MIN_AUTONOMOUS_INTERVAL_MINUTES = 120  # 自律行動の最小実行間隔（分）

# --- 「本日分」ログ設定 ---
MIN_TODAY_LOG_FALLBACK_TURNS = 5  # エピソード記憶作成後の最低表示・送信往復数

# --- 内部処理用AIモデル ---
INTERNAL_PROCESSING_MODEL = "gemini-2.5-flash-lite"
SUMMARIZATION_MODEL = "gemini-2.5-flash"          # 高品質（要約、文章生成など）
EMBEDDING_MODEL = "gemini-embedding-001"


# --- Intent-Aware Retrieval設定 (2026-01-15) ---
# クエリ意図に応じた複合スコアリングの重み
# α: 類似度、β: Arousal（感情的重要度）、γ: 時間減衰
INTENT_WEIGHTS = {
    "emotional": {"alpha": 0.3, "beta": 0.6, "gamma": 0.1},   # 感情的質問: Arousal重視、時間無視
    "factual": {"alpha": 0.5, "beta": 0.2, "gamma": 0.3},     # 事実的質問: バランス
    "technical": {"alpha": 0.3, "beta": 0.1, "gamma": 0.6},   # 技術的質問: 時間重視（古い情報は価値低下）
    "temporal": {"alpha": 0.2, "beta": 0.2, "gamma": 0.6},    # 時間軸質問: 時間重視
    "relational": {"alpha": 0.4, "beta": 0.4, "gamma": 0.2},  # 関係性質問: Arousalやや重視
}
DEFAULT_INTENT = "factual"  # Intent分類失敗時のデフォルト
TIME_DECAY_RATE = 0.05  # 時間減衰率（約14日で半減）

# --- 自動会話要約設定 ---
AUTO_SUMMARY_DEFAULT_THRESHOLD = 12000  # デフォルト閾値（文字数）
AUTO_SUMMARY_MIN_THRESHOLD = 5000       # 最小閾値
AUTO_SUMMARY_MAX_THRESHOLD = 100000     # 最大閾値
AUTO_SUMMARY_KEEP_RECENT_TURNS = 5      # 要約せず保持する直近往復数
AUTO_SUMMARY_TARGET_LENGTH = 1200       # 要約の目標トークン数

# --- ツール専用AIモデル ---
SEARCH_MODEL = "gemini-2.5-flash"

# --- 検索プロバイダ設定 ---
SEARCH_PROVIDER_OPTIONS = {
    "google": "Google (Gemini Native) - 有料プランでグラウンディング使用可",
    "tavily": "Tavily - LLM最適化・高精度（無料枠: 月1000クレジット）",
    "ddg": "DuckDuckGo - 高速・無料",
    "disabled": "無効"
}
DEFAULT_SEARCH_PROVIDER = "ddg"  # デフォルトはDuckDuckGo（無料）

# --- エピソード記憶設定 ---
EPISODIC_MEMORY_OPTIONS = {
    "0": "なし（無効）",
    "1": "過去 1日",
    "2": "過去 2日",
    "3": "過去 3日",
    "4": "過去 4日",
    "5": "過去 5日",
    "7": "過去 1週間",
    "14": "過去 2週間",
    "30": "過去 1ヶ月",
    "90": "過去 3ヶ月"
}
DEFAULT_EPISODIC_MEMORY_DAYS = "0"

# --- Thinking (Reasoning) モデル設定 ---
THINKING_LEVEL_OPTIONS = {
    "auto": "既定 (AIに任せる / 通常モデル)",
    "none": "無効 (思考プロセスをスキップ)",
    "low": "低 (1,024 tokens)",
    "medium": "中 (4,096 tokens)",
    "high": "高 (16,384 tokens)",
    "extreme": "極高 (32,768 tokens)"
}
DEFAULT_THINKING_LEVEL = "auto"

# --- 表情差分設定 ---
EXPRESSIONS_FILE = "expressions.json"
EXPRESSION_TAG_PATTERN = r"【表情】…(\w+)…"  # 正規表現パターン

# デフォルト表情リスト（感情カテゴリ）
DEFAULT_EXPRESSIONS = [
    "neutral",     # 平常、特に強い感情なし（待機時）
    "joy",         # 喜び、楽しさ、嬉しさ
    "anxious",     # 不安、心配
    "sadness",     # 悲しみ、寂しさ
    "anger"        # 怒り、苛立ち
]

# 表情名の日本語表示用マッピング
EXPRESSION_NAMES_JP = {
    "idle": "待機中",
    "thinking": "思考中",
    "neutral": "平常",
    "joy": "喜び",
    "anxious": "不安",
    "sadness": "悲しみ",
    "anger": "怒り"
}

AVATAR_IDLE_TIMEOUT = 60  # 待機表情への復帰時間（秒）

# 表情→感情キーワードのマッピングは廃止（タグまたは内的状態に連動）

# --- ツール結果のログ保存設定 ---
# ログに[RAW_RESULT]を含めて保存するツール（再現に必要なもの）
TOOLS_SAVE_RAW_RESULT = {"generate_image"}

# ログにアナウンスのみ保存するツール（RAW_RESULT除外）
# これ以外のツールは通常通り全データを保存
TOOLS_SAVE_ANNOUNCEMENT_ONLY = {
    # 記憶・検索系
    "recall_memories",
    "search_past_conversations",
    "read_memory_context",
    "search_memory",
    "search_knowledge_base",    # 追加
    # Web巡回・検索系
    "check_watchlist",
    "web_search_tool",
    "tavily_search",
    "tavily_extract",
    "read_url_tool",            # 追加
    # ファイル読み取り系（追加）
    "read_project_file",
    "list_project_files",
    "read_main_memory",
    "read_secret_diary",
    "read_creative_notes",
    "read_research_notes",
    "read_full_notepad",
    "read_world_settings",
    # ファイル編集系（ペルソナ向け指示はログ不要）
    "plan_research_notes_edit",
    "plan_main_memory_edit",
    "plan_secret_diary_edit",
    "plan_notepad_edit",
    "plan_world_edit",
    "plan_creative_notes_edit",
    "read_entity_memory",
    "list_entity_memories",
    "search_entity_memory",
    "read_current_plan",
}

# --- エピソード記憶予算設定 (2026-01-17) ---
EPISODIC_BUDGET_HIGH = 450    # 高Arousal (>= 0.6): 詳細な記録
EPISODIC_BUDGET_MEDIUM = 250  # 中Arousal (>= 0.3): 適度な記録
EPISODIC_BUDGET_LOW = 100     # 低Arousal (< 0.3): 簡潔な記録

# --- Arousal正規化設定 (2026-01-17) ---
# 長期運用でのArousalインフレ防止
AROUSAL_NORMALIZATION_THRESHOLD = 0.6  # 平均がこれを超えたら正規化発動
AROUSAL_NORMALIZATION_FACTOR = 0.9     # 減衰係数（10%減衰）

# --- 階層的圧縮設定 (2026-01-18) ---
# 日次→週次→月次の階層的圧縮で長期記憶を低コスト化
EPISODIC_WEEKLY_COMPRESSION_DAYS = 3    # 3日経過後に週次圧縮
EPISODIC_MONTHLY_COMPRESSION_WEEKS = 4  # 4週経過後に月次圧縮
EPISODIC_WEEKLY_BUDGET = 450            # 週次圧縮の目標文字数
EPISODIC_MONTHLY_BUDGET = 600           # 月次圧縮の目標文字数

# --- Zhipu AI Models ---
ZHIPU_MODELS = [
    "glm-4.7-flash",
    "glm-4.7",
    "glm-4-plus",
    "glm-4.5",
    "glm-4.5-air",
    "glm-zero-preview"
]

# --- Moonshot AI (Kimi) Models ---
MOONSHOT_MODELS = [
    "kimi-k2.5",
    "moonshot-v1-8k",
    "moonshot-v1-32k",
    "moonshot-v1-128k"
]