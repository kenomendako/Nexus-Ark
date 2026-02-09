# === [CRITICAL FIX FOR EMBEDDED PYTHON] ===
# This block MUST be at the absolute top of the file.
import sys
import os
import base64


# Get the absolute path of the directory where this script is located.
# This ensures that even in an embedded environment, Python knows where to find other modules.
script_dir = os.path.dirname(os.path.abspath(__file__))

# Add the script's directory to Python's module search path.
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
# === [END CRITICAL FIX] ===

# --- [ロギング設定の強制上書き] ---
import logging
import logging.config
from pathlib import Path
from sys import stdout

LOGS_DIR = Path(os.getenv("MEMOS_BASE_PATH", Path.cwd())) / ".memos" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE_PATH = LOGS_DIR / "nexus_ark.log"

LOGGING_CONFIG = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": { "standard": { "format": "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(funcName)s - %(message)s" } },
    "handlers": {
        "console": { "level": "INFO", "class": "logging.StreamHandler", "stream": stdout, "formatter": "standard" },
        "file": {
            "level": "DEBUG", "class": "concurrent_log_handler.ConcurrentRotatingFileHandler",
            "filename": LOG_FILE_PATH, "maxBytes": 1024 * 1024 * 10, "backupCount": 5,
            "formatter": "standard", "use_gzip": True,
        },
    },
    "root": { "level": "DEBUG", "handlers": ["console", "file"] },
    "loggers": {
        "memos": { "level": "WARNING", "propagate": True },
        "gradio": { "level": "WARNING", "propagate": True },
        "httpx": { "level": "WARNING", "propagate": True },
        "neo4j": { "level": "WARNING", "propagate": True },
    },
}
logging.config.dictConfig(LOGGING_CONFIG)
# この一行が、他のライブラリによる設定の上書きを完全に禁止する
logging.config.dictConfig = lambda *args, **kwargs: None
print("--- [Nexus Ark] アプリケーション固有のロギング設定を適用しました ---")
print("--- [Nexus Ark] ライブラリを読み込み中... (初回は2〜3分かかる場合があります) ---")
# --- [ここまでが新しいブロック] ---

# --- [Gradio警告の抑制] ---
# Gradioの`special_args`関数がlambdaシグネチャを正しく解析できず、
# 起動時に大量の「Unexpected argument. Filling with None.」警告を出力する問題を抑制
import warnings
warnings.filterwarnings("ignore", message="Unexpected argument. Filling with None.")
# --- [ここまで] ---

# nexus_ark.py (v18: グループ会話FIX・最終版)

import shutil
import utils
import json
import gradio as gr
import traceback
import pandas as pd
import config_manager, room_manager, alarm_manager, ui_handlers, constants, onboarding_manager
from game.chess_engine import game_instance

def handle_user_chess_move(move_json):
    """
    Handle move from frontend (JS).
    move_json: '{"from": "e2", "to": "e4"}'
    
    Returns (fen, status_message).
    - Legal move: updates game state, returns new FEN and success message.
    - Illegal move: logs the attempt to chat (so persona can teach), returns current FEN and error.
    """
    if not move_json:
        return game_instance.get_fen(), "No move data"
    
    try:
        move_data = json.loads(move_json)
        start_sq = move_data.get("from")
        end_sq = move_data.get("to")
        move_str = f"{start_sq}{end_sq}"
        
        # Attempt the move
        move_successful = False
        error_msg = None
        try:
            game_instance.make_move(move_str)
            move_successful = True
        except ValueError as e:
            # Retry with promotion to queen (for pawn reaching last rank)
            try:
                game_instance.make_move(move_str + "q")
                move_successful = True
            except ValueError as e2:
                error_msg = str(e2) if "illegal" in str(e2).lower() else str(e)
        
        if move_successful:
            return game_instance.get_fen(), f"Moved: {move_str}"
        else:
            # --- Record illegal move attempt for persona visibility ---
            # The persona can see this via read_board_state tool
            game_instance.record_illegal_attempt(start_sq, end_sq, error_msg or "不正な手")
            print(f"  - [Chess] ユーザーが不正な手を試みました: {start_sq} → {end_sq} (理由: {error_msg})")
            
            return game_instance.get_fen(), f"Illegal move: {move_str}"
    except Exception as e:
        print(f"Chess move error: {e}")
        return game_instance.get_fen(), f"Error: {e}"


if not utils.acquire_lock():
    print("ロックが取得できなかったため、アプリケーションを終了します。")
    if os.name == "nt": os.system("pause")
    else: input("続行するにはEnterキーを押してください...")
    sys.exit(1)
os.environ["MEM0_TELEMETRY_ENABLED"] = "false"

try:
    config_manager.load_config()

    # --- [初回起動シーケンス] ---
    # characters ディレクトリが存在しない、または空の場合にサンプルペルソナをコピー
    if not os.path.exists(constants.ROOMS_DIR) or not os.listdir(constants.ROOMS_DIR):
        print("--- [初回起動] charactersディレクトリが空のため、サンプルペルソナを展開します ---")
        sample_persona_path = os.path.join(constants.SAMPLE_PERSONA_DIR, "Olivie")
        target_path = os.path.join(constants.ROOMS_DIR, "Olivie")
        if os.path.isdir(sample_persona_path):
            try:
                shutil.copytree(sample_persona_path, target_path)
                print(f"--- サンプルペルソナ「オリヴェ」を {target_path} にコピーしました ---")
                # 初回起動時、configのデフォルトルームをオリヴェに設定
                config_manager.save_config_if_changed("last_room", "Olivie")
                config_manager.load_config() # 設定を再読み込み
            except Exception as e:
                print(f"!!! [致命的エラー] サンプルペルソナのコピーに失敗しました: {e}")
        else:
            print(f"!!! [警告] サンプルペルソナのディレクトリが見つかりません: {sample_persona_path}")
    # --- [初回起動シーケンス ここまで] ---

    # ▼▼▼【ここから追加：テーマ適用ロジック】▼▼▼
    def get_active_theme() -> gr.themes.Base:
        """config.jsonから現在アクティブなテーマを読み込み、Gradioのテーマオブジェクトを生成する。"""
        theme_settings = config_manager.CONFIG_GLOBAL.get("theme_settings", {})
        active_theme_name = theme_settings.get("active_theme", "Soft")
        
        print(f"--- [テーマ] アクティブなテーマ '{active_theme_name}' を読み込んでいます ---")
        theme_obj = config_manager.get_theme_object(active_theme_name)
        print(f"--- [テーマ] テーマオブジェクトの読み込みに成功しました ---")
        return theme_obj

    active_theme_object = get_active_theme()
    # ▲▲▲【追加ここまで】▲▲▲

    alarm_manager.load_alarms()
    alarm_manager.start_alarm_scheduler_thread()

    custom_css = """
    /* --- [Onboarding Overlay] --- */
    #onboarding_overlay {
        position: fixed !important;
        top: 0;
        left: 0;
        width: 100vw;
        height: 100vh;
        z-index: 99999 !important;
        background-color: var(--background-fill-primary);
        display: flex;
        justify-content: center;
        align-items: center;
        padding: 20px;
        backdrop-filter: blur(5px);
    }
    #onboarding_content {
        max-width: 600px;
        width: 100%;
        background: var(--background-fill-secondary);
        padding: 40px;
        border-radius: 16px;
        box-shadow: 0 10px 25px rgba(0, 0, 0, 0.2);
        border: 1px solid var(--border-color-primary);
    }
    #onboarding_content h1 {
        text-align: center;
        color: var(--primary-500);
        margin-bottom: 20px;
        font-size: 1.8em;
    }
    /* --- [Final Styles - v9: Nexus Modern Polish] --- */

    /* Rule 1: <pre> tag (Outer container) styling */
    #chat_output_area .code_wrap pre {
        background-color: var(--background-fill-secondary);
        color: var(--text-color-secondary);
        border: 1px solid var(--border-color-primary);
        padding: 12px;
        border-radius: 12px;
        font-family: var(--font-mono);
        font-size: 0.9em;
        white-space: pre-wrap !important;
        word-break: break-word;
        box-shadow: 0 1px 2px rgba(0,0,0,0.05); /* Subtle shadow for depth */
    }

    /* Rule 2: Resetting <code> tag styles */
    #chat_output_area .code_wrap code {
        background: none !important;
        border: none !important;
        padding: 0 !important;
        background-image: none !important;
        white-space: inherit !important;
    }

    /* Hide Clear Button (Trash Icon) */
    #chat_output_area button[aria-label="会話をクリア"] {
        display: none !important;
    }

    /* --- [Modern Transitions & interactive elements] --- */
    button {
        transition: all 0.2s ease-in-out !important;
    }
    button:hover {
        transform: translateY(-1px);
        filter: brightness(1.05);
    }
    button:active {
        transform: translateY(0px);
    }

    /* --- [Custom Scrollbar (Webkit) for a premium feel] --- */
    ::-webkit-scrollbar {
        width: 8px;
        height: 8px;
    }
    ::-webkit-scrollbar-track {
        background: transparent; 
    }
    ::-webkit-scrollbar-thumb {
        background-color: var(--neutral-300);
        border-radius: 4px;
    }
    .dark ::-webkit-scrollbar-thumb {
        background-color: var(--neutral-700);
    }
    ::-webkit-scrollbar-thumb:hover {
        background-color: var(--neutral-400);
    }
    .dark ::-webkit-scrollbar-thumb:hover {
        background-color: var(--neutral-600);
    }

    /* --- [Chat Bubble Refinement] --- */
    /* Making user/bot messages distinct and modern */
    .message-row.user-row .message-bubble {
        border-radius: 16px 16px 0 16px !important; /* Top-Left, Top-Right, Bottom-Right (0), Bottom-Left */
        background: var(--primary-600); /* Use primary color for user */
        color: white;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    .message-row.bot-row .message-bubble {
        border-radius: 16px 16px 16px 0 !important;
        background: var(--background-fill-secondary);
        border: 1px solid var(--border-color-primary);
        box-shadow: 0 1px 2px rgba(0,0,0,0.05);
    }

    /* --- [Layout & Utility Styles] --- */
    #memory_txt_editor_code textarea, #core_memory_editor_code textarea {
        max-height: 400px !important; overflow-y: auto !important;
    }
    #notepad_editor_code textarea, #system_prompt_editor textarea, #creative_notes_editor_code textarea, #research_notes_editor_code textarea,
    #diary_raw_editor textarea, #creative_notes_raw_editor textarea, #research_notes_raw_editor textarea {
        max-height: 600px !important; overflow-y: auto !important; box-sizing: border-box;
    }
    #memory_txt_editor_code, #notepad_editor_code, #system_prompt_editor, #core_memory_editor_code {
        max-height: 410px; border: 1px solid var(--border-color-primary); border-radius: 8px; padding: 0;
    }

    /* ID: alarm_list_table */
    #alarm_list_table th:nth-child(2), #alarm_list_table td:nth-child(2) {
        min-width: 80px !important;
    }
    #alarm_list_table th:nth-child(3), #alarm_list_table td:nth-child(3) {
        min-width: 100px !important;
    }

    #selection_feedback { font-size: 0.9em; color: var(--text-color-secondary); margin-top: 0px; margin-bottom: 5px; padding-left: 5px; }
    #token_count_display { text-align: right; font-size: 0.85em; color: var(--text-color-secondary); padding-right: 10px; margin-bottom: 5px; }
    #tpm_note_display { text-align: right; font-size: 0.75em; color: var(--text-color-secondary); padding-right: 10px; margin-bottom: -5px; margin-top: 0px; }
    #chat_container { position: relative; }
    
    #app_version_display {
        text-align: center;
        font-size: 0.85em;
        color: var(--text-color-secondary);
        margin-top: 12px;
        font-weight: 400;
        opacity: 0.7;
    }
    /* --- [Novel Mode Styles] --- */
    .novel-mode .message-row .message-bubble,
    .novel-mode .message-row .message-bubble:before,
    .novel-mode .message-row .message-bubble:after,
    .novel-mode .message-wrap .message,
    .novel-mode .message-wrap .message.bot,
    .novel-mode .message-wrap .message.user,
    .novel-mode .bot-row .message-bubble,
    .novel-mode .user-row .message-bubble {
        background: transparent !important;
        background-color: transparent !important;
        border: none !important;
        box-shadow: none !important;
        padding: 0 !important;
        margin: 4px 0 !important;
        border-radius: 0 !important;
    }
    .novel-mode .message-row,
    .novel-mode .user-row,
    .novel-mode .bot-row {
        display: flex !important;
        justify-content: flex-start !important; /* Force all messages to left */
        margin-bottom: 12px !important;
        background: transparent !important;
        border: none !important;
        width: 100% !important; /* Ensure full width */
    }
    /* Hide avatar container in novel mode if desired, or just transparent */
    .novel-mode .avatar-container {
        display: none !important;
    }
    /* Ensure text color is readable and layout is dense */
    .novel-mode .message-wrap .message {
        padding: 0 !important;
    }

    /* --- [Thinking Animation] --- */
    @keyframes pulse-glow {
        0% { box-shadow: 0 0 0 0 rgba(147, 51, 234, 0.4); border-color: var(--primary-500); }
        70% { box-shadow: 0 0 0 10px rgba(147, 51, 234, 0); border-color: var(--primary-400); }
        100% { box-shadow: 0 0 0 0 rgba(147, 51, 234, 0); border-color: var(--primary-500); }
    }
    .thinking-pulse .prose {
        animation: pulse-glow 2s infinite;
    }
    /* Note: Gradio Image component puts the class on the wrapper. 
       We target the inner image or container if needed, but 'elem_classes' usually applies to the outer container. 
       Adjusting selector to match Gradio's structure for Image component.
    */
    .thinking-pulse {
        animation: pulse-glow 2s infinite;
        border-radius: 12px; /* Ensure border radius matches if needed */
    }

    /* --- [Chat Input Area Styling] --- */
    /* チャット入力欄全体の背景色をテーマのサブカラーに連動 */
    #chat_input_multimodal,
    #chat_input_multimodal > div,
    #chat_input_multimodal .block,
    div.block.multimodal-textbox,
    div.full-container,
    [aria-label*="ultimedia input field"] {
        background-color: var(--background-fill-secondary) !important;
        background: var(--background-fill-secondary) !important;
    }

    /* --- [RAWログエディタ] 高さ制限とスクロール --- */
    #chat_log_raw_editor {
        max-height: 600px;
        overflow-y: auto !important;
    }
    #chat_log_raw_editor .cm-scroller {
        max-height: 580px;
        overflow-y: auto !important;
    }

    /* --- [Sidebar & Content Scrolling Fix] --- */
    /* 左右サイドバー共通設定 */
    #left_sidebar, #right_sidebar {
        height: 100dvh !important;
        display: flex !important;
        flex-direction: column !important;
    }
    
    /* 右サイドバー（プロフィール・情景）を左サイドバーより上に表示 */
    #left_sidebar {
        z-index: 1000 !important;
    }
    #right_sidebar {
        z-index: 1001 !important;
    }
    
    /* 縦長画面でのサイドバーつまみ位置調整：
       左サイドバーのつまみを下にずらし、左右のつまみが重ならないようにする。
       これにより開いたサイドバーが100%幅でも、もう一方のつまみが操作可能になる。 */
    @media (max-width: 768px) {
        #left_sidebar > .toggle-button {
            top: 60px !important;
        }
    }
    
    /* サイドバー内のコンテナをスクロール可能にし、中身が詰まらないようにする */
    #left_sidebar > div.sidebar-container,
    #right_sidebar > div.sidebar-container {
        overflow-y: auto !important;
        flex-grow: 1 !important;
        height: 100% !important;
        padding-bottom: 100px !important; /* スマホやブラウザのUIによる隠れを防止 */
    }

    /* メインエリアのスクロール確保（特にスマホ表示時） */
    @media (max-width: 768px) {
        /* Gradioのコンテナ自体もスクロールの邪魔をしないように調整 */
        .gradio-container {
            overflow-y: auto !important;
            height: auto !important;
            min-height: 100dvh !important;
        }
    }

    /* アコーディオンの開閉時に高さが正しく再計算されるように設定 */
    .accordion {
        height: auto !important;
    }

    /* --- [お出かけエクスポート ダウンロードリンク] --- */
    #outing_download_file a {
        display: inline-block !important;
        padding: 10px 20px !important;
        background: var(--primary-500) !important;
        color: white !important;
        border-radius: 8px !important;
        font-weight: bold !important;
        text-decoration: none !important;
        margin-top: 8px !important;
    }
    #outing_download_file a:hover {
        background: var(--primary-600) !important;
        transform: translateY(-1px);
    }

    /* お出かけタブのテキストエリア高さを制限し、スクロールを強制する */
    #outing_tab textarea {
        max-height: 400px !important;
        overflow-y: auto !important;
    }


    """
    custom_js = """
    function() {
        // This function is intentionally left blank.
    }
    """

    # --- [テーマ適用ロジック] ---
    # 新しいconfig_managerの関数を呼び出すように変更
    active_theme_object = config_manager.get_theme_object(
        config_manager.CONFIG_GLOBAL.get("theme_settings", {}).get("active_theme", "nexus_ark_theme")
    )

    with gr.Blocks(theme=active_theme_object, css=custom_css, js=custom_js) as demo:
        # --- [Onboarding Wizard] ---
        initial_status = onboarding_manager.check_status()
        is_onboarding = (initial_status != onboarding_manager.STATUS_ACTIVE_USER)
        
        # オンボーディングモーダル: 初期状態は非表示、demo.loadで必要に応じて表示
        # これにより、リロード時に一瞬オンボーディングが見えることを防止
        with gr.Group(visible=False, elem_id="onboarding_overlay") as onboarding_group:
            with gr.Column(elem_id="onboarding_content"):
                gr.Markdown("# Welcome to Nexus Ark")
                gr.Markdown("Nexus Arkへようこそ！<br>Nexus Arkはあなただけのペルソナ（AI人格）と暮らし、育むための場です。")
                
                # --- Step 1: 選択画面 ---
                with gr.Group(visible=True) as onboarding_step1:
                    gr.Markdown("<br>")
                    gr.Markdown("### セットアップ方法を選択してください")
                    
                    with gr.Row():
                        onboarding_new_btn = gr.Button("🆕 新規インストール", variant="primary", size="lg", scale=1)
                        onboarding_migrate_btn = gr.Button("📦 旧版からデータを引き継ぐ", variant="secondary", size="lg", scale=1)
                    
                    gr.Markdown("💡 旧バージョンのNexus Arkをお使いの方は「旧版からデータを引き継ぐ」を選択すると、設定やキャラクターデータを自動で移行できます。")
                
                # --- Step 2a: 新規インストール（APIキー設定） ---
                with gr.Group(visible=False) as onboarding_step2_new:
                    gr.Markdown("<br>")
                    gr.Markdown("### 🔑 APIキー設定")
                    gr.Markdown("Nexus Arkを動作させるには、[Google Gemini API](https://aistudio.google.com/apikey)のAPIキーが必要です。（無料プランあり）")
                    
                    onboarding_key_name = gr.Textbox(
                        label="キーの名前（任意）",
                        placeholder="例: my_free_key",
                        value="default",
                        info="複数のAPIキーを管理する際の識別名です。"
                    )
                    
                    onboarding_api_key = gr.Textbox(
                        label="Gemini API Key",
                        placeholder="AIzaSy...",
                        type="password"
                    )
                    
                    gr.Markdown("※ APIキーは端末内にのみ保存され、外部に送信されることはありません。")
                    
                    with gr.Row():
                        onboarding_back_btn1 = gr.Button("← 戻る", variant="secondary", size="sm")
                        onboarding_finish_btn = gr.Button("✨ 設定を保存して開始", variant="primary", size="lg")
                    onboarding_error_msg = gr.Textbox(visible=False, label="エラー")
                
                # --- Step 2b: マイグレーション ---
                with gr.Group(visible=False) as onboarding_step2_migrate:
                    gr.Markdown("<br>")
                    gr.Markdown("### 📦 旧バージョンからのデータ移行")
                    gr.Markdown("旧Nexus Arkのフォルダパスを入力してください。設定ファイルとキャラクターデータが自動的に移行されます。")
                    
                    onboarding_migrate_path = gr.Textbox(
                        label="旧Nexus Arkフォルダのパス",
                        placeholder="例: C:\\Users\\username\\Documents\\NexusArk",
                        info="config.json があるフォルダを指定してください"
                    )
                    
                    gr.Markdown("""
**移行されるデータ:**
- `config.json` (APIキー設定)
- `characters/` フォルダ (キャラクターデータ全て)
- `alarms.json` (アラーム設定)
- その他の設定ファイル
""")
                    
                    with gr.Row():
                        onboarding_back_btn2 = gr.Button("← 戻る", variant="secondary", size="sm")
                        onboarding_migrate_exec_btn = gr.Button("📦 データを移行して開始", variant="primary", size="lg")
                    onboarding_migrate_status = gr.Textbox(visible=False, label="ステータス")
                
                # --- イベントハンドラ ---
                def show_new_install():
                    return gr.update(visible=False), gr.update(visible=True), gr.update(visible=False)
                
                def show_migrate():
                    return gr.update(visible=False), gr.update(visible=False), gr.update(visible=True)
                
                def go_back():
                    return gr.update(visible=True), gr.update(visible=False), gr.update(visible=False)
                
                onboarding_new_btn.click(
                    fn=show_new_install,
                    outputs=[onboarding_step1, onboarding_step2_new, onboarding_step2_migrate]
                )
                onboarding_migrate_btn.click(
                    fn=show_migrate,
                    outputs=[onboarding_step1, onboarding_step2_new, onboarding_step2_migrate]
                )
                onboarding_back_btn1.click(
                    fn=go_back,
                    outputs=[onboarding_step1, onboarding_step2_new, onboarding_step2_migrate]
                )
                onboarding_back_btn2.click(
                    fn=go_back,
                    outputs=[onboarding_step1, onboarding_step2_new, onboarding_step2_migrate]
                )
                
                def finish_onboarding(key_name, api_key):
                    if not api_key:
                        return gr.update(visible=True, value="APIキーを入力してください。"), gr.update(visible=True)
                    
                    # キー名が空の場合はdefaultを使用
                    safe_key_name = key_name.strip() if key_name and key_name.strip() else "default"
                    
                    try:
                        # gemini_api_keys 辞書形式で保存（システムが参照する正しい形式）
                        config_manager.add_or_update_gemini_key(safe_key_name, api_key)
                        
                        # last_api_key_name も設定
                        config_manager.save_config_if_changed("last_api_key_name", safe_key_name)

                        # Mark as complete
                        onboarding_manager.mark_setup_completed()
                        
                        # グローバル設定を再読み込み
                        config_manager.load_config()
                        
                        return gr.update(visible=False), gr.update(visible=False) # Hide overlay
                    except Exception as e:
                        return gr.update(visible=True, value=f"保存に失敗しました: {e}"), gr.update(visible=True)
                
                def execute_migration(migrate_path):
                    import shutil
                    from pathlib import Path
                    
                    if not migrate_path or not migrate_path.strip():
                        return gr.update(visible=True, value="パスを入力してください。"), gr.update(visible=True)
                    
                    migrate_path = migrate_path.strip()
                    src_path = Path(migrate_path)
                    dest_path = Path(__file__).parent
                    
                    # パス存在チェック
                    if not src_path.exists():
                        return gr.update(visible=True, value=f"指定されたパスが見つかりません: {migrate_path}"), gr.update(visible=True)
                    
                    # config.json の存在チェック
                    if not (src_path / "config.json").exists():
                        return gr.update(visible=True, value=f"config.json が見つかりません。正しいNexus Arkフォルダを指定してください。"), gr.update(visible=True)
                    
                    try:
                        # --- 1. ルート設定ファイルの移行 ---
                        for filename in ["config.json", "alarms.json", "redaction_rules.json", ".gemini_key_states.json"]:
                            src_file = src_path / filename
                            dest_file = dest_path / filename
                            
                            if src_file.exists():
                                if dest_file.exists():
                                    backup_file = dest_file.with_suffix(dest_file.suffix + ".bak")
                                    shutil.copy2(dest_file, backup_file)
                                    print(f"[Migration] Created backup: {filename}")
                                
                                shutil.copy2(src_file, dest_file)
                                print(f"[Migration] Copied: {filename}")
                        
                        # --- 2. charactersフォルダの移行 ---
                        src_chars = src_path / "characters"
                        dest_chars = dest_path / "characters"
                        
                        if src_chars.exists():
                            for char_dir in src_chars.iterdir():
                                if not char_dir.is_dir() or char_dir.name.startswith("."):
                                    continue
                                
                                target_dir = dest_chars / char_dir.name
                                print(f"[Migration] Migrating character: {char_dir.name}")
                                
                                if target_dir.exists():
                                    # 既存フォルダをバックアップ
                                    backup_dir = dest_chars / f"{char_dir.name}_new_bak"
                                    if not backup_dir.exists():
                                        shutil.move(str(target_dir), str(backup_dir))
                                    else:
                                        shutil.rmtree(target_dir)
                                
                                shutil.copytree(str(char_dir), str(target_dir))
                                print(f"[Migration] Copied character: {char_dir.name}")
                        
                        # --- 3. オリヴェのRAG索引を新バージョンから差し替え ---
                        # 新しい仕様書に対応したRAG索引を事前にビルド済みなので、それで上書き
                        sample_olivie_rag = dest_path / "assets" / "sample_persona" / "Olivie" / "rag_data"
                        olivie_names = ["Olivie", "オリヴェ"]
                        for olivie_name in olivie_names:
                            target_olivie = dest_chars / olivie_name
                            if target_olivie.exists() and sample_olivie_rag.exists():
                                target_rag = target_olivie / "rag_data"
                                # 古いRAG索引を削除して新しいものに差し替え
                                if target_rag.exists():
                                    shutil.rmtree(target_rag)
                                shutil.copytree(str(sample_olivie_rag), str(target_rag))
                                print(f"[Migration] Replaced RAG index for: {olivie_name}")
                        
                        # Mark as complete
                        onboarding_manager.mark_setup_completed()
                        
                        # グローバル設定を再読み込み
                        config_manager.load_config()
                        
                        # 成功メッセージを表示（__SUCCESS__マーカーでJSがリロードをトリガー）
                        gr.Info("✅ データ移行が完了しました！自動でリロードします...")
                        return gr.update(visible=True, value="__SUCCESS__ 移行完了！リロード中..."), gr.update(visible=True)
                    except Exception as e:
                        import traceback
                        error_details = traceback.format_exc()
                        print(f"[Migration Error] {error_details}")
                        return gr.update(visible=True, value=f"移行に失敗しました: {e}\n\n詳細:\n{error_details[:500]}"), gr.update(visible=True)

                onboarding_finish_btn.click(
                    fn=finish_onboarding,
                    inputs=[onboarding_key_name, onboarding_api_key],
                    outputs=[onboarding_error_msg, onboarding_group]
                ).then(
                    fn=None,
                    js="() => { setTimeout(() => { window.location.reload(); }, 500); }"
                )
                
                onboarding_migrate_exec_btn.click(
                    fn=execute_migration,
                    inputs=[onboarding_migrate_path],
                    outputs=[onboarding_migrate_status, onboarding_group]
                ).then(
                    fn=None,
                    # ステータス欄のテキストに__SUCCESS__が含まれていたらリロード
                    js="""() => { 
                        setTimeout(() => {
                            const statusElements = document.querySelectorAll('#onboarding_overlay textarea, #onboarding_overlay input');
                            for (const el of statusElements) {
                                if (el.value && el.value.includes('__SUCCESS__')) {
                                    window.location.reload();
                                    return;
                                }
                            }
                            // フォールバック: オーバーレイが隠れているかチェック
                            const overlay = document.getElementById('onboarding_overlay');
                            if (overlay && !overlay.offsetParent) {
                                window.location.reload();
                            }
                        }, 500);
                    }"""
                )

        room_list_on_startup = room_manager.get_room_list_for_ui()
        if not room_list_on_startup:
            print("--- 有効なルームが見つからないため、'Default'ルームを作成します。 ---")
            room_manager.ensure_room_files("Default")
            room_list_on_startup = room_manager.get_room_list_for_ui()

        folder_names_on_startup = [folder for _display, folder in room_list_on_startup]
        effective_initial_room = config_manager.initial_room_global

        if not effective_initial_room or effective_initial_room not in folder_names_on_startup:
            new_room_folder = folder_names_on_startup[0] if folder_names_on_startup else "Default"
            print(f"警告: 最後に使用したルーム '{effective_initial_room}' が見つからないか無効です。'{new_room_folder}' で起動します。")
            effective_initial_room = new_room_folder
            config_manager.save_config_if_changed("last_room", new_room_folder)
            if new_room_folder == "Default" and "Default" not in folder_names_on_startup:
                room_manager.ensure_room_files("Default")
                room_list_on_startup = room_manager.get_room_list_for_ui()

        # --- Stateの定義 ---
        world_data_state = gr.State({})
        current_room_name = gr.State(effective_initial_room)
        current_model_name = gr.State(config_manager.initial_model_global)
        current_api_key_name_state = gr.State(config_manager.initial_api_key_name_global)
        api_history_limit_state = gr.State(config_manager.initial_api_history_limit_option_global)
        
        # --- style_injector: 常に表示される場所に配置し、起動時からCSSが適用されるようにする ---
        # visible=TrueかつCSSで非表示にすることで、GradioがDOMを更新する
        style_injector = gr.HTML(value="<style></style>", visible=True, elem_id="style_injector_component")
        alarm_dataframe_original_data = gr.State(pd.DataFrame())
        selected_alarm_ids_state = gr.State([])
        editing_alarm_id_state = gr.State(None)
        selected_message_state = gr.State(None)
        message_delete_confirmed_state = gr.Textbox(visible=False) # delete_confirmed_state から改名
        current_log_map_state = gr.State([])
        room_delete_confirmed_state = gr.Textbox(visible=False) # ルーム削除専用
        active_participants_state = gr.State([]) # 現在アクティブなグループ会話の参加者リスト
        debug_console_state = gr.State("")
        chatgpt_thread_choices_state = gr.State([]) # ChatGPTインポート用のスレッド選択肢を保持
        claude_thread_choices_state = gr.State([]) # Claudeインポート用のスレッド選択肢を保持
        archivist_pid_state = gr.State(None) # 記憶アーキビストのプロセスIDを保持
        redaction_rules_state = gr.State(config_manager.load_redaction_rules())
        selected_redaction_rule_state = gr.State(None) # 編集中のルールのインデックスを保持
        active_attachments_state = gr.State([]) # アクティブな添付ファイルパスのリストを保持
        selected_attachment_index_state = gr.State(None) # Dataframeで選択された行のインデックスを保持
        redaction_rule_color_state = gr.State("#62827e")
        imported_theme_params_state = gr.State({}) # インポートされたテーマの詳細設定を一時保持
        selected_knowledge_file_index_state = gr.State(None)
        last_sent_scenery_image_state = gr.State(None)  # 情景画像のAI送信用：最後に送信した画像パスを記憶
        # --- グローバル・左サイドバー (設定) ---
        with gr.Sidebar(label="設定", width=320, open=True, elem_id="left_sidebar"):
            with gr.Column(elem_classes=["sidebar-container"]):
                # [Fix] 初期化時にchoicesとvalueを設定してエラーを防ぐ
                room_dropdown = gr.Dropdown(
                    label="ルームを選択", 
                    choices=room_list_on_startup, 
                    value=effective_initial_room, 
                    interactive=True, allow_custom_value=True)

                with gr.Accordion("⚙️ 設定", open=False):
                    with gr.Tabs() as settings_tabs:
                        with gr.TabItem("共通") as common_settings_tab:
                            with gr.Accordion("🔑 APIキー / Webhook管理", open=False):
                                with gr.Accordion("Gemini APIキー", open=True):
                                    gemini_key_name_input = gr.Textbox(label="キーの名前（管理用の半角英数字）", placeholder="例: my_personal_key")
                                    gemini_key_value_input = gr.Textbox(label="APIキーの値", type="password")
                                    with gr.Row():
                                        save_gemini_key_button = gr.Button("新しいキーを追加", variant="primary")
                                        delete_gemini_key_button = gr.Button("選択したキーを削除", variant="secondary")
                                    gr.Markdown("---")
                                    gr.Markdown("#### 登録済みAPIキーリスト\nチェックを入れたキーが、有料プラン（Pay-as-you-go）として扱われます。")
                                    paid_keys_checkbox_group = gr.CheckboxGroup(
                                        label="有料プランのキーを選択",
                                        choices=[pair[1] for pair in config_manager.get_api_key_choices_for_ui()],
                                        # value=... を削除
                                        interactive=True
                                    )
                                with gr.Accordion("Pushover", open=False):
                                    pushover_user_key_input = gr.Textbox(label="Pushover User Key", type="password", interactive=True) 
                                    pushover_app_token_input = gr.Textbox(label="Pushover App Token/Key", type="password", interactive=True)
                                    save_pushover_config_button = gr.Button("Pushover設定を保存", variant="primary")
                                with gr.Accordion("Discord", open=False):
                                    discord_webhook_input = gr.Textbox(label="Discord Webhook URL", type="password", interactive=True)
                                    save_discord_webhook_button = gr.Button("Discord Webhookを保存", variant="primary")
                                
                                # Zhipu AI [Phase 3]
                                with gr.Accordion("Zhipu AI", open=False) as zhipu_api_key_group:
                                    gr.Markdown("💡 **Zhipu AI APIキー**: `https://open.bigmodel.cn/usercenter/apikeys` でAPIキーを取得してください（登録で500万トークン無料）。")
                                    zhipu_api_key_input = gr.Textbox(
                                        label="Zhipu APIキー",
                                        type="password",
                                        placeholder="[API_KEY_ID].[API_KEY_SECRET]",
                                        value=config_manager.ZHIPU_API_KEY or "",
                                        interactive=True
                                    )
                                    save_zhipu_key_button = gr.Button("Zhipu APIキーを保存", variant="primary", size="sm")

                                # Groq [Phase 3b]
                                with gr.Accordion("Groq", open=False) as groq_api_key_group:
                                    gr.Markdown("💡 **Groq APIキー**: console.groq.com/keys でAPIキーを取得してください（無料枠あり・毎日リセット）。")
                                    groq_api_key_input = gr.Textbox(
                                        label="Groq APIキー",
                                        type="password",
                                        placeholder="gsk_...",
                                        value=config_manager.GROQ_API_KEY or "",
                                        interactive=True
                                    )
                                    save_groq_key_button = gr.Button("Groq APIキーを保存", variant="primary", size="sm")
                                
                                # Moonshot AI (Kimi) [Phase 3d]
                                with gr.Accordion("Moonshot AI (Kimi)", open=False) as moonshot_api_key_group:
                                    gr.Markdown("💡 **Moonshot APIキー**: `https://platform.moonshot.cn` で取得")
                                    moonshot_api_key_input = gr.Textbox(
                                        label="Moonshot APIキー",
                                        type="password",
                                        placeholder="sk-...",
                                        value=config_manager.MOONSHOT_API_KEY or "",
                                        interactive=True
                                    )
                                    save_moonshot_key_button = gr.Button("Moonshot APIキーを保存", variant="primary", size="sm")

                                # ローカルLLM [Phase 3c]
                                with gr.Accordion("ローカルLLM (llama.cpp)", open=False) as local_llm_group:
                                    gr.Markdown(
                                        "💡 **ローカルLLM**: llama-cpp-python を使用してGGUFモデルをローカルで実行します。\n\n"
                                        "📦 **インストール**: `pip install llama-cpp-python`\n\n"
                                        "📁 **推奨モデル**: Qwen 2.5 3B Instruct (Q4_K_M) など軽量なGGUFモデル"
                                    )
                                    local_model_path_input = gr.Textbox(
                                        label="GGUFモデルパス",
                                        placeholder="/path/to/model.gguf",
                                        value=config_manager.LOCAL_MODEL_PATH or "",
                                        info="ローカルに保存したGGUFモデルファイルの絶対パス",
                                        interactive=True
                                    )
                                    save_local_model_path_button = gr.Button("モデルパスを保存", variant="primary", size="sm")

                                # Tavily (Web Search) [Phase 3]
                                with gr.Accordion("Tavily (Web検索)", open=False) as tavily_api_key_group:
                                    gr.Markdown("💡 **Tavily APIキー**: [tavily.com](https://tavily.com) で無料アカウントを作成してAPIキーを取得してください（月1000クレジット無料）。")
                                    tavily_api_key_input = gr.Textbox(
                                        label="Tavily APIキー",
                                        type="password",
                                        placeholder="tvly-...",
                                        value=config_manager.TAVILY_API_KEY or "",
                                        interactive=True
                                    )
                                    save_tavily_key_button = gr.Button("Tavily APIキーを保存", variant="primary", size="sm")
                                gr.Markdown("⚠️ **注意:** APIキーやWebhook URLはPC上の `config.json` ファイルに平文で保存されます。取り扱いには十分ご注意ください。")

                            with gr.Accordion("⚡ AIモデルプロバイダ設定（デフォルト）", open=False):
                                gr.Markdown("会話に使用するAIモデルのプロバイダを切り替えます。")
                                            
                                current_provider = config_manager.get_active_provider()
                                            
                                provider_radio = gr.Radio(
                                    choices=[
                                        ("標準設定を使用", "default"),
                                        ("Google (Gemini Native)", "google"),
                                        ("OpenAI互換 (OpenRouter / Groq / Ollama / Zhipu AI)", "openai")
                                    ],
                                    value=current_provider,
                                    label="アクティブなプロバイダ",
                                    interactive=True
                                )
                                            
                                # --- Google設定エリア ---
                                with gr.Group(visible=(current_provider == "google")) as google_settings_group:
                                    gr.Markdown(
                                        "💡 ここで設定したAPIキーは、内部処理でも使用されます。（2.5 Flash / Flash Lite）\n\n"
                                        "💡 ルームごとのモデル・APIキー設定は、「個別」タブから行えます。"
                                    )
                                    model_dropdown = gr.Dropdown(choices=config_manager.AVAILABLE_MODELS_GLOBAL, label="デフォルトAIモデル", interactive=True, allow_custom_value=True)
                                    with gr.Row():
                                        delete_model_button = gr.Button("選択中のモデルを削除", variant="secondary", size="sm")
                                        reset_models_button = gr.Button("デフォルトに戻す", variant="secondary", size="sm")
                                    api_key_dropdown = gr.Dropdown(
                                        label="使用するGemini APIキー", 
                                        choices=config_manager.get_api_key_choices_for_ui(),
                                        interactive=True, allow_custom_value=True)
                                    api_test_button = gr.Button("API接続をテスト", variant="secondary")
                                    # [Phase 1.5] ローテーション設定
                                    settings_rotation_checkbox = gr.Checkbox(
                                        label="APIキー自動ローテーションを有効にする",
                                        value=True,
                                        interactive=True,
                                        info="レート制限 (429) 発生時、自動的に他の有効なキーに切り替えます。"
                                    )



                                # --- OpenAI互換設定エリア ---
                                with gr.Group(visible=(current_provider == "openai")) as openai_settings_group:
                                    openai_profiles = [s["name"] for s in config_manager.get_openai_settings_list()]
                                    current_openai_profile = config_manager.get_active_openai_profile_name()
                                                
                                    openai_profile_dropdown = gr.Dropdown(
                                        choices=openai_profiles,
                                        value=current_openai_profile,
                                        label="プロファイル選択",
                                        interactive=True,
                                        allow_custom_value=False # 既存のみ選択可
                                    )
                                                
                                    with gr.Row():
                                        openai_base_url_input = gr.Textbox(label="Base URL", placeholder="例: https://openrouter.ai/api/v1")
                                        openai_api_key_input = gr.Textbox(label="API Key", type="password", placeholder="sk-...")
                                                
                                    # モデル選択をDropdownに変更
                                    # 現在のプロファイルからモデルリストを取得
                                    _current_openai_setting = config_manager.get_active_openai_setting() or {}
                                    _current_models = _current_openai_setting.get("available_models", [])
                                    _current_default_model = _current_openai_setting.get("default_model", "")
                                                
                                    openai_model_dropdown = gr.Dropdown(
                                        choices=_current_models,
                                        value=_current_default_model,
                                        label="デフォルトモデル",
                                        interactive=True,
                                        allow_custom_value=True,  # カスタム値の直接入力も許可
                                        info="リストから選択するか、新しいモデル名を直接入力できます"
                                    )
                                                
                                    # カスタムモデル追加UI
                                    with gr.Accordion("カスタムモデルを追加", open=False):
                                        with gr.Row():
                                            custom_model_name_input = gr.Textbox(
                                                label="モデル名",
                                                placeholder="例: my-custom-model",
                                                scale=3
                                            )
                                            add_custom_model_button = gr.Button("追加", scale=1, variant="secondary")
                                        gr.Markdown("💡 追加したモデルはプロファイルに保存され、次回起動時も利用できます。")
                                    
                                    with gr.Row():
                                        delete_openai_model_button = gr.Button("選択中のモデルを削除", variant="secondary", size="sm")
                                        reset_openai_models_button = gr.Button("デフォルトに戻す", variant="secondary", size="sm")
                                    with gr.Row():
                                        fetch_models_button = gr.Button("📥 モデルリスト取得", variant="secondary", size="sm")
                                        toggle_favorite_button = gr.Button("⭐ お気に入りに追加/削除", variant="secondary", size="sm")
                                    gr.Markdown("⚠️ すべてのモデルがNexus Arkで動作するわけではありません。", elem_id="common_openai_model_warning")
                                                
                                    # 【ツール不使用モード】ツール使用チェックボックス
                                    _tool_use_enabled = _current_openai_setting.get("tool_use_enabled", True)
                                    openai_tool_use_checkbox = gr.Checkbox(
                                        label="ツール使用（Function Calling）を有効にする",
                                        value=_tool_use_enabled,
                                        interactive=True,
                                        info="OFFにすると、AIはWeb検索・画像生成・記憶編集などのツールを使用できなくなりますが、ツール非対応モデルでも会話できるようになります。"
                                    )
                                                
                                    save_openai_config_button = gr.Button("このプロファイル設定を保存", variant="secondary")

                            with gr.Accordion("🔧 内部処理モデル設定", open=False):
                                gr.Markdown(
                                    "要約・RAGクエリ生成・エンベディングなど、バックグラウンド処理に使用するモデルを設定します。\n"
                                    "各タスクごとにプロバイダとモデルを自由に組み合わせできます。"
                                )
                                
                                # 現在の設定を取得
                                _internal_settings = config_manager.get_internal_model_settings()
                                _openai_provider_names = [s.get("name", "") for s in config_manager.CONFIG_GLOBAL.get("openai_provider_settings", [])]
                                _provider_choices = [("Google (Gemini)", "google")] + [(name, name) for name in _openai_provider_names] + [("ローカル", "local")]
                                
                                # --- 処理モデル（軽量タスク用） ---
                                gr.Markdown("### 🚀 処理モデル（軽量タスク）")
                                gr.Markdown("RAGクエリ生成、Intent分類、グループ会話の司会などに使用します。", elem_classes=["info-text"])
                                with gr.Row():
                                    internal_processing_provider = gr.Dropdown(
                                        choices=_provider_choices,
                                        value=_internal_settings.get("processing_provider", "google"),
                                        label="プロバイダ",
                                        scale=1,
                                        allow_custom_value=True,
                                        interactive=True
                                    )
                                    internal_processing_model = gr.Dropdown(
                                        choices=[
                                            (constants.INTERNAL_PROCESSING_MODEL + " (Gemini)", constants.INTERNAL_PROCESSING_MODEL),
                                            ("llama-3.1-8b-instant (Groq推奨)", "llama-3.1-8b-instant"),
                                            ("glm-4.7-flash (Zhipu)", "glm-4.7-flash"),
                                            ("qwen3-32b (Groq)", "qwen3-32b"),
                                        ],
                                        value=_internal_settings.get("processing_model", constants.INTERNAL_PROCESSING_MODEL),
                                        label="モデル",
                                        scale=2,
                                        allow_custom_value=True,
                                        interactive=True
                                    )
                                
                                # --- 要約モデル（文章生成用） ---
                                gr.Markdown("### 📝 要約モデル（文章生成）")
                                gr.Markdown("日次/週次要約、コアメモリ圧縮、ペルソナデータ圧縮などに使用します。", elem_classes=["info-text"])
                                with gr.Row():
                                    internal_summarization_provider = gr.Dropdown(
                                        choices=_provider_choices,
                                        value=_internal_settings.get("summarization_provider", "google"),
                                        label="プロバイダ",
                                        scale=1,
                                        allow_custom_value=True,
                                        interactive=True
                                    )
                                    internal_summarization_model = gr.Dropdown(
                                        choices=[
                                            (constants.SUMMARIZATION_MODEL + " (Gemini)", constants.SUMMARIZATION_MODEL),
                                            ("glm-4.7-flash (Zhipu推奨)", "glm-4.7-flash"),
                                            ("meta-llama/llama-4-scout-17b-16e-instruct (Groq)", "meta-llama/llama-4-scout-17b-16e-instruct"),
                                        ],
                                        value=_internal_settings.get("summarization_model", constants.SUMMARIZATION_MODEL),
                                        label="モデル",
                                        scale=2,
                                        allow_custom_value=True,
                                        interactive=True
                                    )
                                
                                # --- エンベディングモデル ---
                                gr.Markdown("### 🧠 エンベディングモデル")
                                gr.Markdown("記憶の検索（RAG）と話題クラスタリングで使用します。ローカルは初回のみダウンロードし、以降はオフラインで動作します。", elem_classes=["info-text"])
                                with gr.Row():
                                    embedding_provider_radio = gr.Dropdown(
                                        choices=[
                                            ("Google (Gemini)", "gemini"),
                                            ("OpenAI互換", "openai"),
                                            ("ローカル (推奨)", "local")
                                        ],
                                        value=_internal_settings.get("embedding_provider", "gemini"),
                                        label="プロバイダ",
                                        scale=1,
                                        allow_custom_value=True,
                                        interactive=True
                                    )
                                    embedding_model_dropdown = gr.Dropdown(
                                        choices=[
                                            ("gemini-embedding-001 (Google)", "gemini-embedding-001"),
                                            ("text-embedding-3-small (OpenAI)", "text-embedding-3-small"),
                                            ("multilingual-e5-small (ローカル推奨)", "multilingual-e5-small"),
                                            ("paraphrase-multilingual-MiniLM-L12-v2", "paraphrase-multilingual-MiniLM-L12-v2"),
                                        ],
                                        value=_internal_settings.get("embedding_model", "gemini-embedding-001"),
                                        label="モデル",
                                        scale=2,
                                        allow_custom_value=True,
                                        interactive=True
                                    )
                                
                                # --- フォールバック設定 ---
                                gr.Markdown("---")
                                internal_fallback_checkbox = gr.Checkbox(
                                    label="フォールバック有効（プロバイダ障害時にGoogleへ自動切替）",
                                    value=_internal_settings.get("fallback_enabled", True),
                                    info="プライマリプロバイダでエラーが発生した場合、Google (Gemini) にフォールバック",
                                    interactive=True
                                )
                                
                                with gr.Row():
                                    reset_internal_model_button = gr.Button("デフォルトに戻す", variant="secondary", size="sm")
                                    save_internal_model_button = gr.Button("設定を保存", variant="primary", size="sm")
                                
                                internal_model_status = gr.Markdown("", visible=False)

                            with gr.Accordion("🎨 画像生成設定", open=False):
                                # Configから現在の設定を読み込む
                                current_img_provider = config_manager.CONFIG_GLOBAL.get("image_generation_provider", "gemini")
                                current_img_model = config_manager.CONFIG_GLOBAL.get("image_generation_model", "gemini-2.5-flash-image")
                                available_gemini_models = config_manager.CONFIG_GLOBAL.get("available_image_models", {}).get("gemini", ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"])
                                available_openai_models = config_manager.CONFIG_GLOBAL.get("available_image_models", {}).get("openai", ["gpt-image-1", "dall-e-3"])
                                openai_settings = config_manager.CONFIG_GLOBAL.get("image_generation_openai_settings", {})

                                image_gen_provider_radio = gr.Radio(
                                    choices=[
                                        ("Gemini", "gemini"),
                                        ("OpenAI互換", "openai"),
                                        ("無効", "disabled")
                                    ],
                                    value=current_img_provider,
                                    label="画像生成プロバイダ",
                                    interactive=True,
                                    info="「無効」にすると、AIのプロンプトからも画像生成に関する項目が削除されます。"
                                )

                                # Geminiモデル選択
                                with gr.Column(visible=(current_img_provider == "gemini")) as gemini_model_section:
                                    gemini_image_model_dropdown = gr.Dropdown(
                                        choices=available_gemini_models,
                                        value=current_img_model if current_img_model in available_gemini_models else available_gemini_models[0],
                                        label="Gemini画像生成モデル",
                                        interactive=True, allow_custom_value=True)

                                # OpenAI互換設定
                                with gr.Column(visible=(current_img_provider == "openai")) as openai_image_section:
                                    # 既存のOpenAI互換プロファイルから選択
                                    openai_provider_names = [s.get("name", "") for s in config_manager.CONFIG_GLOBAL.get("openai_provider_settings", [])]
                                    openai_image_profile_dropdown = gr.Dropdown(
                                        choices=openai_provider_names,
                                        value=openai_settings.get("profile_name", openai_provider_names[0] if openai_provider_names else "OpenAI Official"),
                                        label="使用するプロファイル（APIキー/Webhook管理で設定）",
                                        interactive=True,
                                        info="プロファイルのAPIキーとBase URLを使用します", allow_custom_value=True)
                                    openai_image_model_dropdown = gr.Dropdown(
                                        choices=available_openai_models,
                                        value=openai_settings.get("model", "gpt-image-1"),
                                        label="OpenAI画像生成モデル",
                                        interactive=True,
                                        allow_custom_value=True,
                                        info="カスタムモデル名も入力可能（ComfyUI等）"
                                    )

                                # 設定保存ボタン
                                save_image_gen_button = gr.Button("画像生成設定を保存", variant="primary")

                            with gr.Accordion("🔍 検索プロバイダ設定", open=False):
                                current_search_provider = config_manager.CONFIG_GLOBAL.get("search_provider", constants.DEFAULT_SEARCH_PROVIDER)
                                # constants.pyの定数からUI用の選択肢を生成
                                search_provider_choices = [(label, key) for key, label in constants.SEARCH_PROVIDER_OPTIONS.items()]
                                search_provider_radio = gr.Radio(
                                    choices=search_provider_choices,
                                    value=current_search_provider,
                                    label="Web検索プロバイダ (web_search_tool)",
                                    interactive=True,
                                    info="AIがWeb検索を行う際に使用するサービスを選択します。"
                                )
                                
                                # キー入力欄は「APIキー / Webhook管理」に移動しました
                                pass


                            with gr.Accordion("📢 通知サービス設定", open=False):
                                notification_service_radio = gr.Radio(
                                    choices=["Discord", "Pushover"], 
                                    label="アラーム通知に使用するサービス",
                                    interactive=True
                                )
                                gr.Markdown("---")

                            with gr.Accordion("💾 バックアップ設定", open=False):
                                backup_rotation_count_number = gr.Number(
                                    label="バックアップの最大保存件数（世代数）",
                                    # value=... を削除
                                    step=1,
                                    minimum=1,
                                    interactive=True,
                                    info="ファイル（ログ、記憶など）ごとに、ここで指定した数だけ最新のバックアップが保持されます。"
                                )
                                open_backup_folder_button = gr.Button("現在のルームのバックアップフォルダを開く", variant="secondary")
                            
                            # --- ネットワーク設定 ---
                            with gr.Accordion("🌐 ネットワーク設定", open=False):
                                gr.Markdown("⚠️ **設定変更後はアプリの再起動が必要です。**")
                                allow_external_connection_checkbox = gr.Checkbox(
                                    label="外部接続を許可（同じネットワーク内の他デバイスからアクセス可能）",
                                    interactive=True,
                                    info="有効にすると、スマホなど他のデバイスからアクセスできます。"
                                )
                            
                            # --- デバッグ設定 ---
                            debug_mode_checkbox = gr.Checkbox(label="デバッグモードを有効化 (デバッグコンソールにシステムプロンプトを出力)", interactive=True)
                        with gr.TabItem("個別") as individual_settings_tab:
                            room_settings_info = gr.Markdown("ℹ️ *現在選択中のルーム「...」にのみ適用される設定です。設定は自動保存されます。*")

                            # --- [Phase 3] 個別設定用AIモデルプロバイダ設定 (一番上に配置) ---
                            with gr.Accordion("⚡ AIモデルプロバイダ設定（このルーム）", open=False):
                                gr.Markdown("このルームで使用するAIプロバイダを設定します。「共通設定に従う」を選ぶとデフォルト設定が適用されます。")
                                            
                                room_provider_radio = gr.Radio(
                                    choices=[
                                        ("共通設定に従う", "default"),
                                        ("Google (Gemini Native)", "google"),
                                        ("OpenAI互換 (OpenRouter / Groq / Moonshot / Zhipu AI / Ollama)", "openai")
                                    ],
                                    value="default",
                                    label="このルームで使用するプロバイダ",
                                    interactive=True
                                )
                                            
                                # --- Google設定グループ ---
                                with gr.Group(visible=False) as room_google_settings_group:
                                    room_model_dropdown = gr.Dropdown(
                                        choices=config_manager.AVAILABLE_MODELS_GLOBAL,
                                        label="このルームで使用するAIモデル",
                                        info="Gemini APIで使用するモデルを選択します。",
                                        interactive=True,
                                        allow_custom_value=True
                                    )
                                                
                                    # カスタムモデル追加UI
                                    with gr.Accordion("カスタムモデルを追加", open=False):
                                        with gr.Row():
                                            room_google_custom_model_input = gr.Textbox(
                                                label="モデル名",
                                                placeholder="例: gemini-2.5-flash-exp",
                                                scale=3
                                            )
                                            room_google_add_model_button = gr.Button("追加", scale=1, variant="secondary")
                                        gr.Markdown("💡 追加したモデルは現在のセッション中のみ有効です。")
                                    
                                    with gr.Row():
                                        room_delete_gemini_model_button = gr.Button("選択中のモデルを削除", variant="secondary", size="sm")
                                        room_reset_gemini_models_button = gr.Button("デフォルトに戻す", variant="secondary", size="sm")
                                                
                                    room_api_key_dropdown = gr.Dropdown(
                                        choices=config_manager.get_api_key_choices_for_ui(),
                                        label="このルームで使用するAPIキー",
                                        info="共通設定で登録したAPIキーから選択します。",
                                        interactive=True, allow_custom_value=True)
                                    # [Phase 1.5] 個別ローテーション設定
                                    room_rotation_dropdown = gr.Dropdown(
                                        choices=[("共通設定に従う", None), ("有効", True), ("無効", False)],
                                        value=None,
                                        label="このルームでローテーションを有効にする",
                                        interactive=True, allow_custom_value=True)
                                    
                                    room_thinking_level_dropdown = gr.Dropdown(
                                        choices=list(constants.THINKING_LEVEL_OPTIONS.values()),
                                        label="Thinking レベル (Gemini 3系)",
                                        info="思考モデルの予算を指定します。高いほど深い推論が可能ですが、待ち時間が長くなります。",
                                        interactive=True, allow_custom_value=True)
                                    

        
                                # --- OpenAI互換設定グループ ---
                                with gr.Column(visible=False) as room_openai_settings_group:
                                    # プロファイル選択
                                    room_openai_profile_dropdown = gr.Dropdown(
                                        choices=[s["name"] for s in config_manager.get_openai_settings_list()],
                                        label="プロファイル選択",
                                        info="共通設定で登録したプロファイルを使用します。APIキーは共通設定で管理されます。",
                                        interactive=True, allow_custom_value=True)
                                                
                                    # Base URL/API Keyは非表示（共通設定で一元管理）
                                    with gr.Row(visible=False):
                                        room_openai_base_url_input = gr.Textbox(
                                            label="Base URL",
                                            placeholder="例: https://openrouter.ai/api/v1",
                                            interactive=True
                                        )
                                        room_openai_api_key_input = gr.Textbox(
                                            label="API Key",
                                            type="password",
                                            placeholder="sk-...",
                                            interactive=True
                                        )
                                                
                                    # モデル選択（Dropdown + カスタム値入力可能）
                                    # 起動時に最初のプロファイルのモデルリストを取得しておく
                                    _room_openai_settings_list = config_manager.get_openai_settings_list()
                                    _room_initial_models = _room_openai_settings_list[0].get("available_models", []) if _room_openai_settings_list else []
                                    _room_initial_default_model = _room_openai_settings_list[0].get("default_model", "") if _room_openai_settings_list else ""
                                    room_openai_model_dropdown = gr.Dropdown(
                                        choices=_room_initial_models,
                                        value=_room_initial_default_model,
                                        label="デフォルトモデル",
                                        interactive=True,
                                        allow_custom_value=True,
                                        info="プロファイル選択で自動入力されるか、直接入力できます"
                                    )
                                                
                                    # カスタムモデル追加UI
                                    with gr.Accordion("カスタムモデルを追加", open=False):
                                        with gr.Row():
                                            room_openai_custom_model_input = gr.Textbox(
                                                label="モデル名",
                                                placeholder="例: my-custom-model",
                                                scale=3
                                            )
                                            room_openai_add_model_button = gr.Button("追加", scale=1, variant="secondary")
                                        gr.Markdown("💡 追加したモデルは現在のセッション中のみ有効です。")
                                    
                                    with gr.Row():
                                        room_delete_openai_model_button = gr.Button("選択中のモデルを削除", variant="secondary", size="sm")
                                        room_reset_openai_models_button = gr.Button("デフォルトに戻す", variant="secondary", size="sm")
                                    with gr.Row():
                                        room_fetch_models_button = gr.Button("📥 モデルリスト取得", variant="secondary", size="sm")
                                        room_toggle_favorite_button = gr.Button("⭐ お気に入りに追加/削除", variant="secondary", size="sm")
                                    gr.Markdown("⚠️ すべてのモデルがNexus Arkで動作するわけではありません。", elem_id="openai_model_warning")
                                                
                                    # ツール使用オンオフ
                                    room_openai_tool_use_checkbox = gr.Checkbox(
                                        label="ツール使用（Function Calling）を有効にする",
                                        value=True,
                                        interactive=True,
                                        info="OFFにすると、AIはWeb検索・画像生成・記憶編集などのツールを使用できなくなりますが、ツール非対応モデルでも会話できるようになります。"
                                    )

                            with gr.Accordion("🖼️ 情景描写設定", open=False):
                                enable_scenery_system_checkbox = gr.Checkbox(
                                    label="🖼️ このルームで情景描写システムを有効にする",
                                    info="有効にすると、チャット画面右側に情景が表示され、AIもそれを認識します。",
                                    interactive=True
                                )
                            with gr.Accordion("📜 チャット表示設定", open=False):
                                with gr.Group():
                                    gr.Markdown("##### 逐次表示設定")
                                    enable_typewriter_effect_checkbox = gr.Checkbox(label="タイプライター風の逐次表示を有効化", interactive=True)
                                    streaming_speed_slider = gr.Slider(
                                        minimum=0.0, maximum=0.1, step=0.005,
                                        label="表示速度", info="値が小さいほど速く、大きいほどゆっくり表示されます。(0.0で最速)",
                                        interactive=True
                                    )
                                
                                with gr.Group():
                                    gr.Markdown("##### 表示モード")
                                    # --- [v19] Novel Mode Toggle ---
                                    chat_style_radio = gr.Radio(
                                        choices=["Chat (Default)", "Novel (Text only)"],
                                        label="スタイル選択",
                                        value="Chat (Default)",
                                        interactive=True,
                                        info="「Novel」にすると吹き出しや枠線が消え、小説のような表示になります。"
                                    )

                                with gr.Group():
                                    gr.Markdown("##### 文字サイズ・行間")
                                    font_size_slider = gr.Slider(minimum=10, maximum=30, value=15, step=1, label="文字サイズ (px)", interactive=True)
                                    line_height_slider = gr.Slider(minimum=1.0, maximum=3.0, value=1.6, step=0.1, label="行間", interactive=True)
                                
                                # style_injector moved to Palette tab to ensure active rendering
                            with gr.Accordion("🎤 音声設定", open=False):
                                gr.Markdown("チャットの発言を選択して、ここで設定した声で再生できます。")
                                room_voice_dropdown = gr.Dropdown(label="声を選択（個別）", choices=list(config_manager.SUPPORTED_VOICES.values()), interactive=True, allow_custom_value=True)
                                room_voice_style_prompt_textbox = gr.Textbox(label="音声スタイルプロンプト", placeholder="例：囁くように、楽しそうに、落ち着いたトーンで", interactive=True)
                                with gr.Row():
                                    room_preview_text_textbox = gr.Textbox(value="こんにちは、Nexus Arkです。これは音声のテストです。", show_label=False, scale=3)
                                    room_preview_voice_button = gr.Button("試聴", scale=1)
                                open_audio_folder_button = gr.Button("📂 現在のルームの音声フォルダを開く", variant="secondary")
                            with gr.Accordion("🔬 AI生成パラメータ調整", open=False):
                                gr.Markdown("このルームの応答の「創造性」と「安全性」を調整します。")
                                room_temperature_slider = gr.Slider(minimum=0.0, maximum=2.0, step=0.05, label="Temperature", info="値が高いほど、AIの応答がより創造的で多様になります。(推奨: 0.7 ~ 0.9)")
                                room_top_p_slider = gr.Slider(minimum=0.0, maximum=1.0, step=0.01, label="Top-P", info="値が低いほど、ありふれた単語が選ばれやすくなります。(推奨: 0.95)")
                                safety_choices = ["ブロックしない", "低リスク以上をブロック", "中リスク以上をブロック", "高リスクのみブロック"]
                                with gr.Row():
                                    room_safety_harassment_dropdown = gr.Dropdown(choices=safety_choices, label="嫌がらせコンテンツ", interactive=True, allow_custom_value=True)
                                    room_safety_hate_speech_dropdown = gr.Dropdown(choices=safety_choices, label="ヘイトスピーチ", interactive=True, allow_custom_value=True)
                                with gr.Row():
                                    room_safety_sexually_explicit_dropdown = gr.Dropdown(choices=safety_choices, label="性的コンテンツ", interactive=True, allow_custom_value=True)
                                    room_safety_dangerous_content_dropdown = gr.Dropdown(choices=safety_choices, label="危険なコンテンツ", interactive=True, allow_custom_value=True)
                                        
                            with gr.Accordion("📡 送信コンテキスト設定", open=False):
                                room_api_history_limit_dropdown = gr.Dropdown(
                                    choices=list(constants.API_HISTORY_LIMIT_OPTIONS.values()), 
                                    label="APIへの履歴送信（短期記憶の長さ）", 
                                    info="AIに送信する直近の会話ログの長さを設定します。",
                                    interactive=True, allow_custom_value=True)
                                
                                # --- 自動会話要約設定 ---
                                room_auto_summary_checkbox = gr.Checkbox(
                                    label="本日分が長くなったら自動で要約する",
                                    info="閾値を超えると、古い会話を要約してAPIコストを削減します。",
                                    interactive=True
                                )
                                room_auto_summary_threshold_slider = gr.Slider(
                                    minimum=constants.AUTO_SUMMARY_MIN_THRESHOLD,
                                    maximum=constants.AUTO_SUMMARY_MAX_THRESHOLD,
                                    step=1000,
                                    value=constants.AUTO_SUMMARY_DEFAULT_THRESHOLD,
                                    label="要約閾値（文字数）",
                                    info="この文字数を超えたら要約を開始します。",
                                    interactive=True,
                                    visible=False  # チェックボックスONで表示
                                )

                                room_episode_memory_days_dropdown = gr.Dropdown(
                                    choices=list(constants.EPISODIC_MEMORY_OPTIONS.values()),
                                    label="エピソード記憶の参照期間（中期記憶）",
                                    info="生ログより前の期間について、要約された記憶をどれくらい遡って参照するか設定します。",
                                    interactive=True, allow_custom_value=True)

                                room_enable_retrieval_checkbox = gr.Checkbox(
                                    label="記憶の想起（長期記憶）を有効化",
                                    info="▼AIが応答する前に、過去ログや知識ベースから関連情報を自律的に検索・想起します。",
                                    interactive=True
                                )

                                room_display_thoughts_checkbox = gr.Checkbox( 
                                    label="AIの思考過程 [THOUGHT] をチャットに表示する",
                                    interactive=True
                                )
                                room_send_thoughts_checkbox = gr.Checkbox(label="思考過程をAPIに送信", interactive=True)
                                                                                    
                                room_add_timestamp_checkbox = gr.Checkbox(label="メッセージにタイムスタンプを追加", interactive=True)                                        
                                room_send_current_time_checkbox = gr.Checkbox(
                                    label="現在時刻をAPIに送信",
                                    info="▼挨拶の自然さを向上させますが、特定の時間帯を演じたい場合はOFFにしてください。",
                                    interactive=True
                                )

                                room_send_notepad_checkbox = gr.Checkbox(label="メモ帳の内容をAPIに送信", interactive=True)
                                room_use_common_prompt_checkbox = gr.Checkbox(label="共通ツールプロンプトを送信", interactive=True)
                                room_send_core_memory_checkbox = gr.Checkbox(label="コアメモリをAPIに送信", interactive=True)
                                room_send_scenery_checkbox = gr.Checkbox(
                                    label="情景画像をAIに共有",
                                    info="▼現在の景色をAIに見せます。送信タイミングは下で選択。",
                                    interactive=True,
                                    visible=True
                                )
                                room_scenery_send_mode_dropdown = gr.Dropdown(
                                    choices=["変更時のみ", "毎ターン"],
                                    value="変更時のみ",
                                    label="送信タイミング",
                                    info="「変更時のみ」=場所移動・画像更新時、「毎ターン」=毎回送信",
                                    interactive=True,
                                    visible=True, allow_custom_value=True)
                                auto_memory_enabled_checkbox = gr.Checkbox(label="対話の自動記憶を有効化", interactive=True, visible=False)
                                room_enable_self_awareness_checkbox = gr.Checkbox(
                                    label="自己意識機能（動機・感情検出・夢の指針・目標）",
                                    info="▼AIが動機や感情を認識し、夢の指針や目標をコンテキストに含めます。OFFにするとAPIコストを削減できます。",
                                    interactive=True,
                                    value=True
                                )

                            with gr.Accordion("✨ 自律行動設定 (Beta)", open=False):
                                gr.Markdown(
                                    "ユーザーからの入力がない間も、AIが自律的に思考し、行動（日記の整理、検索、発話など）を行います。\n"
                                    "**注意:** 設定した頻度で自動的にAPIを呼び出すため、コストにご注意ください。"
                                )
                                room_enable_autonomous_checkbox = gr.Checkbox(
                                    label="自律行動モードを有効化",
                                    interactive=True
                                )
                                room_autonomous_inactivity_slider = gr.Slider(
                                    minimum=10, maximum=1440, step=10, value=120,
                                    label="無操作判定時間（分）",
                                    info="最後の会話からこの時間が経過すると、AIが「何かすべきことはないか」と思考を開始します。",
                                    interactive=True
                                )
                                            
                                gr.Markdown("#### 🌙 通知禁止時間帯 (Quiet Hours)")
                                gr.Markdown(
                                    "この時間帯にAIが行動した場合、通知（Discord/Pushover）は送信されません。\n"
                                    "また、この時間帯はAIの「睡眠時間」とみなされ、**夢日記の作成**と**睡眠時記憶整理**が実行されます。詳しくは「記憶タブ → 夢日記」をご覧ください。"
                                )

                                with gr.Row():
                                    time_options = [f"{i:02d}:00" for i in range(24)]
                                    room_quiet_hours_start = gr.Dropdown(choices=time_options, value="00:00", label="開始時刻", interactive=True, allow_custom_value=True)
                                    room_quiet_hours_end = gr.Dropdown(choices=time_options, value="07:00", label="終了時刻", interactive=True, allow_custom_value=True) 

                            # --- ウォッチリスト管理 ---
                            with gr.Accordion("📋 ウォッチリスト管理", open=False) as watchlist_accordion:
                                gr.Markdown("監視対象URLを管理します。AIに「〇〇を監視リストに追加して」と言うこともできます。")
                                
                                with gr.Tabs():
                                    with gr.TabItem("URL一覧"):
                                        with gr.Row():
                                            watchlist_url_input = gr.Textbox(
                                                label="URL",
                                                placeholder="https://example.com/page",
                                                scale=3
                                            )
                                            watchlist_name_input = gr.Textbox(
                                                label="表示名",
                                                placeholder="例: 公式ブログ",
                                                scale=2
                                            )
                                            watchlist_interval_dropdown = gr.Dropdown(
                                                choices=[
                                                    ("手動のみ", "manual"),
                                                    ("1時間ごと", "hourly_1"),
                                                    ("3時間ごと", "hourly_3"),
                                                    ("6時間ごと", "hourly_6"),
                                                    ("12時間ごと", "hourly_12"),
                                                    ("毎日指定時刻", "daily"),
                                                ],
                                                value="manual",
                                                label="監視頻度",
                                                scale=1, allow_custom_value=True)
                                        
                                        with gr.Row(visible=False) as watchlist_daily_time_row:
                                            watchlist_daily_time = gr.Dropdown(
                                                choices=[f"{i:02d}:00" for i in range(24)],
                                                value="09:00",
                                                label="📅 毎日のチェック時刻",
                                                info="「毎日指定時刻」を選択した場合の実行時刻",
                                                scale=1, allow_custom_value=True)
                                        
                                        with gr.Row():
                                            watchlist_add_button = gr.Button("➕ 追加/更新", variant="primary", scale=1)
                                            watchlist_check_button = gr.Button("🔄 全件チェック", variant="secondary", scale=1)
                                            watchlist_refresh_button = gr.Button("🔃 一覧を更新", variant="secondary", scale=1)
                                        
                                        watchlist_status = gr.Textbox(label="ステータス", interactive=False, max_lines=2)
                                        
                                        gr.Markdown("### 登録済みURL一覧")
                                        watchlist_dataframe = gr.Dataframe(
                                            headers=["ID", "名前", "URL", "頻度", "最終確認", "有効", "グループ"],
                                            datatype=["str", "str", "str", "str", "str", "bool", "str"],
                                            interactive=False,
                                            wrap=True,
                                            row_count=(5, "dynamic"),
                                            col_count=(7, "fixed")
                                        )
                                        
                                        with gr.Row():
                                            watchlist_selected_id = gr.Textbox(label="選択中のID", visible=False)
                                            watchlist_move_group_dropdown = gr.Dropdown(
                                                choices=[("グループなし", "")],
                                                label="グループに移動",
                                                scale=2, allow_custom_value=True)
                                            watchlist_move_button = gr.Button("📁 移動", variant="secondary", scale=1)
                                            watchlist_delete_button = gr.Button("🗑️ 削除", variant="stop", scale=1)
                                    
                                    with gr.TabItem("グループ管理"):
                                        gr.Markdown("グループを作成すると、複数のURLの巡回時刻を一括で変更できます。")
                                        
                                        with gr.Row():
                                            group_name_input = gr.Textbox(
                                                label="グループ名",
                                                placeholder="例: AI技術ニュース",
                                                scale=2
                                            )
                                            group_description_input = gr.Textbox(
                                                label="説明（任意）",
                                                placeholder="例: 機械学習・AI関連のブログ",
                                                scale=3
                                            )
                                        
                                        with gr.Row():
                                            group_interval_dropdown = gr.Dropdown(
                                                choices=[
                                                    ("手動のみ", "manual"),
                                                    ("1時間ごと", "hourly_1"),
                                                    ("3時間ごと", "hourly_3"),
                                                    ("6時間ごと", "hourly_6"),
                                                    ("12時間ごと", "hourly_12"),
                                                    ("毎日指定時刻", "daily"),
                                                ],
                                                value="manual",
                                                label="巡回頻度",
                                                scale=1, allow_custom_value=True)
                                            group_daily_time = gr.Dropdown(
                                                choices=[f"{i:02d}:00" for i in range(24)],
                                                value="09:00",
                                                label="時刻（毎日指定時刻用）",
                                                scale=1,
                                                visible=True, allow_custom_value=True)
                                            group_create_button = gr.Button("➕ グループ作成", variant="primary", scale=1)
                                        
                                        group_status = gr.Textbox(label="ステータス", interactive=False, max_lines=2)
                                        
                                        gr.Markdown("### グループ一覧")
                                        group_dataframe = gr.Dataframe(
                                            headers=["ID", "名前", "説明", "頻度", "件数", "有効"],
                                            datatype=["str", "str", "str", "str", "number", "bool"],
                                            interactive=False,
                                            wrap=True,
                                            row_count=(3, "dynamic"),
                                            col_count=(6, "fixed")
                                        )
                                        
                                        with gr.Row():
                                            group_selected_id = gr.Textbox(label="選択中のグループID", visible=False)
                                            group_new_interval_dropdown = gr.Dropdown(
                                                choices=[
                                                    ("手動のみ", "manual"),
                                                    ("1時間ごと", "hourly_1"),
                                                    ("3時間ごと", "hourly_3"),
                                                    ("6時間ごと", "hourly_6"),
                                                    ("12時間ごと", "hourly_12"),
                                                    ("毎日指定時刻", "daily"),
                                                ],
                                                label="新しい巡回頻度",
                                                scale=1, allow_custom_value=True)
                                            group_new_daily_time = gr.Dropdown(
                                                choices=[f"{i:02d}:00" for i in range(24)],
                                                value="09:00",
                                                label="時刻",
                                                scale=1, allow_custom_value=True)
                                            group_update_interval_button = gr.Button("⏰ 時刻一括変更", variant="secondary", scale=1)
                                            group_delete_button = gr.Button("🗑️ グループ削除", variant="stop", scale=1)
                                        
                                        # --- AI自動リスト作成 ---
                                        gr.Markdown("---")
                                        gr.Markdown("### 🤖 AI自動リスト作成")
                                        gr.Markdown("ジャンルを指定すると、AIがWeb検索で関連サイトを収集し、候補リストを作成します。")
                                        
                                        with gr.Row():
                                            ai_genre_input = gr.Textbox(
                                                label="ジャンル",
                                                placeholder="例: AI技術ニュース、機械学習ブログ",
                                                scale=3
                                            )
                                            ai_generate_button = gr.Button("🔍 候補を検索", variant="secondary", scale=1)
                                        
                                        ai_generate_status = gr.Textbox(label="検索ステータス", interactive=False, max_lines=2)
                                        
                                        # 候補リスト（CheckboxGroup）
                                        ai_candidates_checkboxgroup = gr.CheckboxGroup(
                                            choices=[],
                                            label="📋 候補サイト（追加するものを選択）",
                                            visible=False
                                        )
                                        
                                        # 候補データ保持用（非表示）
                                        ai_candidates_data = gr.State([])
                                        
                                        with gr.Row(visible=False) as ai_add_row:
                                            ai_add_to_group_dropdown = gr.Dropdown(
                                                choices=[("グループなし", "")],
                                                label="追加先グループ",
                                                scale=2, allow_custom_value=True)
                                            ai_add_button = gr.Button("✅ 選択したサイトを追加", variant="primary", scale=1)

                            with gr.Accordion("📁 プロジェクト探索設定", open=False):
                                gr.Markdown("AIが `list_project_files` や `read_project_file` ツールでアクセスできるフォルダを設定します。")
                                room_project_root_input = gr.Textbox(
                                    label="プロジェクトルートの絶対パス",
                                    placeholder="例: /home/user/my_project",
                                    info="空の場合は Nexus Ark の実行ディレクトリが使用されます。",
                                    interactive=True
                                )
                                with gr.Row():
                                    room_project_exclude_dirs_input = gr.Textbox(
                                        label="除外ディレクトリ (カンマ区切り)",
                                        placeholder=".git, venv, __pycache__",
                                        interactive=True
                                    )
                                    room_project_exclude_files_input = gr.Textbox(
                                        label="除外ファイル (カンマ区切り)",
                                        placeholder="*.pyc, .env, config.json",
                                        interactive=True
                                    )
                                gr.Markdown("💡 設定はルームごとに保存されます。")


                        with gr.TabItem("デザイン") as theme_tab:
                            # チェックボックスをタブの最上部に配置
                            room_theme_enabled_checkbox = gr.Checkbox(label="個別テーマを有効にする", value=False, interactive=True)
                            gr.Markdown("このルーム専用の配色を設定・保存します。（未指定の場合は下記ベーステーマが適用されます）")
                            
                            with gr.Accordion("🎀 ルーム別デザイン", open=False):
                                with gr.Accordion("メイン配色", open=False):
                                    with gr.Row():
                                        theme_primary_picker = gr.ColorPicker(label="メインカラー（強調・ローダー）", interactive=True)
                                        theme_secondary_picker = gr.ColorPicker(label="サブカラー（AI発言・ラベル背景）", interactive=True)
                                        theme_accent_soft_picker = gr.ColorPicker(label="ユーザー発言色", interactive=True)
                                    with gr.Row():
                                        theme_background_picker = gr.ColorPicker(label="背景色", interactive=True)
                                        theme_text_picker = gr.ColorPicker(label="文字色", interactive=True)
                                
                                with gr.Accordion("詳細配色", open=False):
                                    gr.Markdown("ドロップダウンやテキストボックス、コードブロック、ボタンなどの色を個別に設定できます。")
                                    with gr.Row():
                                        theme_input_bg_picker = gr.ColorPicker(label="テキストボックス・スクロールバー", interactive=True)
                                        theme_input_border_picker = gr.ColorPicker(label="入力欄の枠線色", interactive=True)
                                        theme_code_bg_picker = gr.ColorPicker(label="コードブロック背景色", interactive=True)
                                    with gr.Row():
                                        theme_subdued_text_picker = gr.ColorPicker(label="サブテキスト色（説明文など）", interactive=True)
                                        theme_button_bg_picker = gr.ColorPicker(label="ボタン背景色", interactive=True)
                                        theme_button_hover_picker = gr.ColorPicker(label="ボタンホバー色", interactive=True)
                                    with gr.Row():
                                        theme_stop_button_bg_picker = gr.ColorPicker(label="停止ボタン背景色", interactive=True)
                                        theme_stop_button_hover_picker = gr.ColorPicker(label="停止ボタンホバー色", interactive=True)
                                        theme_checkbox_off_picker = gr.ColorPicker(label="未チェックボックス色 (Off)", value=None)
                                    theme_table_bg_picker = gr.ColorPicker(label="テーブル背景色", value=None)
                                    theme_radio_label_picker = gr.ColorPicker(label="ラジオ/チェックボックスのラベル背景色", value=None)
                                    theme_dropdown_list_bg_picker = gr.ColorPicker(label="ドロップダウンリスト背景色", value=None)
                                
                                with gr.Accordion("背景画像設定", open=False):
                                    gr.Markdown("ルームの背景に画像を設定します。")
                                    theme_ui_opacity_slider = gr.Slider(0.0, 1.0, value=0.7, step=0.05, label="UI背景の不透明度 (透明 0.0 - 1.0 不透明)")
                                    theme_bg_src_mode = gr.Radio(label="背景ソース", choices=["画像を指定 (Manual)", "現在地と連動 (Sync)"], value="現在地と連動 (Sync)", interactive=True)
                                    
                                    # Manual Mode Settings
                                    with gr.Group(visible=False) as manual_bg_settings_group:
                                        theme_bg_image_picker = gr.Image(label="背景画像 (Manualモード用)", type="filepath", interactive=True, height=200)
                                        with gr.Row():
                                            theme_bg_opacity_slider = gr.Slider(label="不透明度 (Opacity)", minimum=0.0, maximum=1.0, step=0.1, value=0.3, interactive=True)
                                            theme_bg_blur_slider = gr.Slider(label="ぼかし (Blur)", minimum=0, maximum=20, step=1, value=2, interactive=True)
                                        with gr.Row():
                                            theme_bg_size_dropdown = gr.Dropdown(label="サイズ", choices=["cover", "contain", "auto", "custom"], value="cover", interactive=True, allow_custom_value=True)
                                            theme_bg_position_dropdown = gr.Dropdown(label="位置", choices=["center", "top", "bottom", "left", "right", "top left", "top right", "bottom left", "bottom right"], value="center", interactive=True, allow_custom_value=True)
                                        with gr.Row():
                                             theme_bg_repeat_dropdown = gr.Dropdown(label="繰り返し", choices=["no-repeat", "repeat"], value="no-repeat", interactive=True, allow_custom_value=True)
                                             theme_bg_custom_width = gr.Textbox(label="カスタム幅 (custom時のみ)", placeholder="300px", value="300px", interactive=True)
                                        with gr.Row():
                                             theme_bg_radius_slider = gr.Slider(label="角丸 (%)", minimum=0, maximum=50, step=1, value=0, interactive=True)
                                             theme_bg_mask_blur_slider = gr.Slider(label="エッジぼかし (px)", minimum=0, maximum=100, step=1, value=0, interactive=True)
                                             theme_bg_overlay_checkbox = gr.Checkbox(label="前面に表示 (Overlay)", value=False, interactive=True)

                                    # Sync Mode Settings
                                    with gr.Group(visible=True) as sync_bg_settings_group:
                                        gr.Markdown("※ 画像は現在地に合わせて自動選択されます。")
                                        with gr.Row():
                                            theme_bg_sync_opacity_slider = gr.Slider(label="不透明度 (Opacity)", minimum=0.0, maximum=1.0, step=0.1, value=0.3, interactive=True)
                                            theme_bg_sync_blur_slider = gr.Slider(label="ぼかし (Blur)", minimum=0, maximum=20, step=1, value=2, interactive=True)
                                        with gr.Row():
                                            theme_bg_sync_size_dropdown = gr.Dropdown(label="サイズ", choices=["cover", "contain", "auto", "custom"], value="cover", interactive=True, allow_custom_value=True)
                                            theme_bg_sync_position_dropdown = gr.Dropdown(label="位置", choices=["center", "top", "bottom", "left", "right", "top left", "top right", "bottom left", "bottom right"], value="center", interactive=True, allow_custom_value=True)
                                        with gr.Row():
                                             theme_bg_sync_repeat_dropdown = gr.Dropdown(label="繰り返し", choices=["no-repeat", "repeat"], value="no-repeat", interactive=True, allow_custom_value=True)
                                             theme_bg_sync_custom_width = gr.Textbox(label="カスタム幅 (custom時のみ)", placeholder="300px", value="300px", interactive=True)
                                        with gr.Row():
                                             theme_bg_sync_radius_slider = gr.Slider(label="角丸 (%)", minimum=0, maximum=50, step=1, value=0, interactive=True)
                                             theme_bg_sync_mask_blur_slider = gr.Slider(label="エッジぼかし (px)", minimum=0, maximum=100, step=1, value=0, interactive=True)
                                             theme_bg_sync_overlay_checkbox = gr.Checkbox(label="前面に表示 (Overlay)", value=False, interactive=True)

                                    theme_bg_src_mode.change(
                                        fn=lambda x: (gr.update(visible=x=="画像を指定 (Manual)"), gr.update(visible=x=="現在地と連動 (Sync)")),
                                        inputs=[theme_bg_src_mode],
                                        outputs=[manual_bg_settings_group, sync_bg_settings_group]
                                    )
                                
                                save_room_theme_button = gr.Button("🎀 現在のテーマ設定をこのルームに保存", size="sm", variant="primary")
                            
                            with gr.Accordion("🏛️ ベーステーマ選択", open=False):
                                gr.Markdown("アプリ全体のテーマを変更します。適用には再起動が必要です。")
                                theme_settings_state = gr.State({})
                                with gr.Row():
                                    theme_selector = gr.Dropdown(label="テーマを選択", interactive=True, scale=3, allow_custom_value=True)
                                    apply_theme_button = gr.Button("適用（要再起動）", variant="primary", scale=1)
                                        
                                # --- [サムネイル表示エリア] ---
                                with gr.Row():
                                    with gr.Column():
                                        gr.Markdown("##### ライトモード プレビュー")
                                        theme_preview_light = gr.Image(label="Light Mode Preview", interactive=False, height=200)
                                    with gr.Column():
                                        gr.Markdown("##### ダークモード プレビュー")
                                        theme_preview_dark = gr.Image(label="Dark Mode Preview", interactive=False, height=200)
                                
                                # --- [カスタマイズ: 折り畳み可能] ---
                                with gr.Accordion("🔧 カスタマイズ", open=False):
                                    gr.Markdown("選択したテーマをカスタマイズして、新しい名前で保存できます。\n※ファイルベースのテーマは直接編集できません。")
                                    AVAILABLE_HUES = [
                                        "slate", "gray", "zinc", "neutral", "stone", "red", "orange", "amber",
                                        "yellow", "lime", "green", "emerald", "teal", "cyan", "sky", "blue",
                                        "indigo", "violet", "purple", "fuchsia", "pink", "rose"
                                    ]
                                    with gr.Row():
                                        primary_hue_picker = gr.Dropdown(choices=AVAILABLE_HUES, label="プライマリカラー系統", value="blue", allow_custom_value=True)
                                        secondary_hue_picker = gr.Dropdown(choices=AVAILABLE_HUES, label="セカンダリカラー系統", value="sky", allow_custom_value=True)
                                        neutral_hue_picker = gr.Dropdown(choices=AVAILABLE_HUES, label="ニュートラルカラー系統", value="slate", allow_custom_value=True)
                                            
                                    AVAILABLE_FONTS = sorted([
                                        "Alice", "Archivo", "Bitter", "Cabin", "Cormorant Garamond", "Crimson Pro",
                                        "Dm Sans", "Eczar", "Fira Sans", "Glegoo", "IBM Plex Mono", "Inconsolata", "Inter",
                                        "Jost", "Lato", "Libre Baskerville", "Libre Franklin", "Lora", "Merriweather",
                                        "Montserrat", "Mulish", "Noto Sans", "Noto Sans JP", "Open Sans", "Playfair Display",
                                        "Poppins", "Pt Sans", "Pt Serif", "Quattrocento", "Quicksand", "Raleway",
                                        "Roboto", "Roboto Mono", "Rubik", "Source Sans Pro", "Source Serif Pro",
                                        "Space Mono", "Spectral", "Sriracha", "Titillium Web", "Ubuntu", "Work Sans"
                                    ])
                                    font_dropdown = gr.Dropdown(choices=AVAILABLE_FONTS, label="メインフォント", value="Noto Sans JP", interactive=True, allow_custom_value=True)
                                            
                                    gr.Markdown("---")
                                    custom_theme_name_input = gr.Textbox(label="新しいテーマ名として保存", placeholder="例: My Cool Theme")
                                            
                                    with gr.Row():
                                        save_theme_button = gr.Button("カスタムテーマとして保存", variant="secondary")
                                        export_theme_button = gr.Button("ファイルにエクスポート", variant="secondary")

                with gr.Accordion("⏰ 時間管理", open=False):
                    with gr.Tabs():
                        with gr.TabItem("アラーム"):
                            gr.Markdown("ℹ️ **操作方法**: リストから操作したいアラームの行を選択し、下のボタンで操作します。")
                            alarm_dataframe = gr.Dataframe(
                                headers=["状態", "時刻", "予定", "ルーム", "内容"], 
                                datatype=["bool", "str", "str", "str", "str"], 
                                interactive=True, 
                                col_count=5, 
                                row_count=(10, "dynamic"),
                                wrap=False, 
                                elem_id="alarm_list_table",
                                value=[[True, "08:00", "テスト1", "Default", "テストアラーム1"], [False, "12:00", "テスト2", "Default", "テストアラーム2"], [True, "18:00", "テスト3", "Default", "テストアラーム3"]]
                            )
                            selection_feedback_markdown = gr.Markdown("アラームを選択してください", elem_id="selection_feedback")
                            with gr.Row():
                                enable_button = gr.Button("✔️ 選択を有効化"); disable_button = gr.Button("❌ 選択を無効化"); delete_alarm_button = gr.Button("🗑️ 選択したアラームを削除", variant="stop")
                            gr.Markdown("---"); gr.Markdown("#### 新規 / 更新")
                            alarm_hour_dropdown = gr.Dropdown(choices=[str(i).zfill(2) for i in range(24)], label="時", value="08", allow_custom_value=True)
                            alarm_minute_dropdown = gr.Dropdown(choices=[str(i).zfill(2) for i in range(60)], label="分", value="00", allow_custom_value=True)
                            alarm_room_dropdown = gr.Dropdown(choices=room_list_on_startup, value=effective_initial_room, label="ルーム", allow_custom_value=True)
                            alarm_context_input = gr.Textbox(label="内容", placeholder="AIに伝える内容や目的を簡潔に記述します。\n例：朝の目覚まし、今日も一日頑張ろう！", lines=3)
                            alarm_emergency_checkbox = gr.Checkbox(label="緊急通知として送信 (マナーモードを貫通)", value=False, interactive=True)
                            alarm_days_checkboxgroup = gr.CheckboxGroup(choices=["月", "火", "水", "木", "金", "土", "日"], label="曜日", value=[])
                            with gr.Row():
                                alarm_add_button = gr.Button("アラーム追加")
                                cancel_edit_button = gr.Button("編集をキャンセル", visible=False)
                        with gr.TabItem("タイマー"):
                            timer_type_radio = gr.Radio(["通常タイマー", "ポモドーロタイマー"], label="タイマー種別", value="通常タイマー")
                            with gr.Column(visible=True) as normal_timer_ui:
                                timer_duration_number = gr.Number(label="タイマー時間 (分)", value=10, minimum=1, step=1); normal_timer_theme_input = gr.Textbox(label="通常タイマーのテーマ", placeholder="例: タイマー終了！")
                            with gr.Column(visible=False) as pomo_timer_ui:
                                pomo_work_number = gr.Number(label="作業時間 (分)", value=25, minimum=1, step=1); pomo_break_number = gr.Number(label="休憩時間 (分)", value=5, minimum=1, step=1); pomo_cycles_number = gr.Number(label="サイクル数", value=4, minimum=1, step=1); timer_work_theme_input = gr.Textbox(label="作業終了時テーマ", placeholder="作業終了！"); timer_break_theme_input = gr.Textbox(label="休憩終了時テーマ", placeholder="休憩終了！")
                            timer_room_dropdown = gr.Dropdown(choices=room_list_on_startup, value=effective_initial_room, label="通知ルーム", interactive=True, allow_custom_value=True); timer_status_output = gr.Textbox(label="タイマー設定状況", interactive=False, placeholder="ここに設定内容が表示されます。"); timer_submit_button = gr.Button("タイマー開始", variant="primary")

                with gr.Accordion("🧑‍🤝‍🧑 グループ会話", open=False):
                    session_status_display = gr.Markdown("現在、1対1の会話モードです。")
                    participant_checkbox_group = gr.CheckboxGroup(
                        label="会話に招待するルーム",
                        choices=sorted([c for c in room_list_on_startup if c != effective_initial_room]),
                        interactive=True
                    )
                    group_hide_thoughts_checkbox = gr.Checkbox(
                        label="思考ログを非表示（セッション中のみ）",
                        value=False,
                        info="チェックすると、グループ会話中の全参加者の思考ログが非表示になります。"
                    )
                    # [v18] Supervisorモード（AI自動進行）
                    enable_supervisor_cb = gr.Checkbox(
                        label="AI自動進行（司会モード） [Beta]",
                        value=False,
                        info="AIが会話の流れを読んで、次に誰が話すべきかを自動で指名します。（ONにすると会話が自律的に進みます）",
                        visible=False  # 一時封印
                    )
                    with gr.Row():
                        start_session_button = gr.Button("このメンバーで会話を開始 / 更新", variant="primary")
                        end_session_button = gr.Button("会話を終了 (1対1に戻る)", variant="secondary")

                with gr.Accordion("🏠 チャットルームの作成・管理", open=False) as manage_room_accordion:
                    with gr.Tabs() as room_management_tabs:
                        with gr.TabItem("作成") as create_room_tab:
                            new_room_name = gr.Textbox(label="ルーム名（必須）", info="UIやグループ会話で表示される名前です。フォルダ名は自動で生成されます。")
                            new_user_display_name = gr.Textbox(label="あなたの表示名（任意）", placeholder="デフォルト: ユーザー")
                            new_agent_display_name = gr.Textbox(label="Agentの表示名（任意）", placeholder="AIのデフォルト表示名。未設定の場合はルーム名が使われます。")
                            new_room_description = gr.Textbox(label="ルームの説明（任意）", lines=3, placeholder="このルームがどのような場所かをメモしておけます。")
                            initial_system_prompt = gr.Textbox(label="初期システムプロンプト（任意）", lines=5, placeholder="このルームの基本的なルールやAIの役割などを設定します。")
                            create_room_button = gr.Button("ルームを作成", variant="primary")
                                    
                        with gr.TabItem("管理") as manage_room_tab:
                            manage_room_selector = gr.Dropdown(label="管理するルームを選択", choices=room_list_on_startup, interactive=True, allow_custom_value=True)
                            with gr.Column(visible=False) as manage_room_details:
                                open_room_folder_button = gr.Button("📂 ルームフォルダを開く", variant="secondary")
                                manage_room_name = gr.Textbox(label="ルーム名")
                                manage_user_display_name = gr.Textbox(label="あなたの表示名")
                                manage_agent_display_name = gr.Textbox(label="Agentの表示名")
                                manage_room_description = gr.Textbox(label="ルームの説明", lines=3)
                                manage_folder_name_display = gr.Textbox(label="フォルダ名（編集不可）", interactive=False)
                                save_room_config_button = gr.Button("変更を保存", variant="primary")
                                delete_room_button = gr.Button("このルームを削除", variant="stop")
                                    
                        with gr.TabItem("インポート") as import_tab:
                            with gr.Accordion("🔵 ChatGPT (公式)", open=False):
                                gr.Markdown("### ChatGPTデータインポート\n`conversations.json` またはデータ全体のZIPファイルをアップロードして、過去の対話をNexus Arkにインポートします。")
                                chatgpt_import_file = gr.File(label="`conversations.json` (または ZIP) をアップロード", file_types=[".json", ".zip"])
                                with gr.Column(visible=False) as chatgpt_import_form:
                                    chatgpt_thread_dropdown = gr.Dropdown(label="インポートする会話スレッドを選択 (複数選択可)", interactive=True, multiselect=True, allow_custom_value=True)
                                    chatgpt_room_name_textbox = gr.Textbox(label="新しいルーム名", interactive=True)
                                    chatgpt_user_name_textbox = gr.Textbox(label="あなたの表示名（ルーム内）", value="ユーザー", interactive=True)
                                    chatgpt_import_button = gr.Button("この会話をNexus Arkにインポートする", variant="primary")
                            with gr.Accordion("🟠 Claude (公式)", open=False):
                                gr.Markdown("### Claudeデータインポート\n`conversations.json` またはデータ全体のZIPファイルをアップロードして、過去の対話をNexus Arkにインポートします。")
                                claude_import_file = gr.File(label="`conversations.json` (または ZIP) をアップロード", file_types=[".json", ".zip"])
                                with gr.Column(visible=False) as claude_import_form:
                                    claude_thread_dropdown = gr.Dropdown(label="インポートする会話スレッドを選択 (複数選択可)", interactive=True, multiselect=True, allow_custom_value=True)
                                    claude_room_name_textbox = gr.Textbox(label="新しいルーム名", interactive=True)
                                    claude_user_name_textbox = gr.Textbox(label="あなたの表示名（ルーム内）", value="ユーザー", interactive=True)
                                    claude_import_button = gr.Button("この会話をNexus Arkにインポートする", variant="primary")

                            with gr.Accordion("📄 その他テキスト/JSON", open=False):
                                gr.Markdown(
                                    "### 汎用インポーター\n"
                                    "ChatGPT Exporter形式のファイルや、任意の話者ヘッダーを持つテキストログをインポートします。"
                                )
                                generic_import_file = gr.File(label="JSON, MD, TXT ファイルをアップロード (複数可)", file_types=[".json", ".md", ".txt"], file_count="multiple")
                                with gr.Column(visible=False) as generic_import_form:
                                    generic_room_name_textbox = gr.Textbox(label="新しいルーム名", interactive=True)
                                    generic_user_name_textbox = gr.Textbox(label="あなたの表示名（ルーム内）", interactive=True)
                                    gr.Markdown("---")
                                    gr.Markdown(
                                        "**話者ヘッダーの指定**\n"
                                        "ファイル内の、誰の発言かを示す行頭の文字列を正確に入力してください。"
                                    )
                                    generic_user_header_textbox = gr.Textbox(label="あなたの発言ヘッダー", placeholder="例: Prompt:")
                                    generic_agent_header_textbox = gr.Textbox(label="AIの発言ヘッダー", placeholder="例: Response:")
                                    generic_import_button = gr.Button("このファイルをインポートする", variant="primary")



                with gr.Accordion("🛠️ チャット支援ツール", open=False):
                    with gr.Tabs():
                        with gr.TabItem("文字置き換え"):
                            gr.Markdown("チャット履歴内の特定の文字列を、スクリーンショット用に一時的に別の文字列に置き換えます。**元のログファイルは変更されません。**")
                            screenshot_mode_checkbox = gr.Checkbox(
                                label="スクリーンショットモードを有効にする",
                                info="有効にすると、下のルールに基づいてチャット履歴の表示が置き換えられます。"
                            )
                            with gr.Row():
                                with gr.Column(scale=3):
                                    gr.Markdown("**現在のルールリスト**")
                                    redaction_rules_df = gr.Dataframe(
                                        headers=["元の文字列 (Find)", "置換後の文字列 (Replace)", "背景色"],
                                        datatype=["str", "str", "str"],
                                        row_count=(5, "dynamic"),
                                        col_count=(3, "fixed"),
                                        interactive=False
                                    )
                                with gr.Column(scale=2):
                                    gr.Markdown("**ルールの編集**")
                                    redaction_find_textbox = gr.Textbox(label="元の文字列 (Find)")
                                    redaction_replace_textbox = gr.Textbox(label="置換後の文字列 (Replace)")
                                    redaction_color_picker = gr.ColorPicker(label="背景色", value="#62827e")
                                    with gr.Row():
                                        add_rule_button = gr.Button("ルールを追加/更新", variant="primary")
                                        clear_rule_form_button = gr.Button("フォームをクリア")
                                    delete_rule_button = gr.Button("選択したルールを削除", variant="stop")
                        with gr.TabItem("ログ修正"):
                            gr.Markdown("選択した**発言**以降の**AIの応答**に含まれる読点（、）を、AIを使って自動で修正し、自然な文章に校正します。")
                            gr.Markdown("⚠️ **注意:** この操作はログファイルを直接上書きするため、元に戻せません。処理の前に、ログファイルのバックアップが自動的に作成されます。")
                            correct_punctuation_button = gr.Button("選択発言以降の読点をAIで修正", variant="secondary")
                            correction_confirmed_state = gr.Textbox(visible=False)
                        with gr.TabItem("添付ファイル") as attachment_tab:
                            gr.Markdown(
                                "過去に添付したファイルの一覧です。\n\n"
                                "リストを選択してアクティブにすることで、解除するまで送信に含められます。\n\n"
                                "**⚠️注意:** ここでファイルを削除すると、チャット履歴の画像表示なども含めて、ファイルへのすべての参照が失われます。"
                            )
                            active_attachments_display = gr.Markdown("現在アクティブな添付ファイルはありません。")
                            gr.Markdown("---") # 区切り線

                            attachments_df = gr.Dataframe(
                                headers=["ファイル名", "種類", "サイズ(KB)", "添付日時"],
                                datatype=["str", "str", "str", "str"],
                                row_count=(5, "dynamic"),
                                col_count=(4, "fixed"),
                                interactive=True,  # 行選択を有効にする
                                wrap=True
                            )
                            with gr.Row():
                                open_attachments_folder_button = gr.Button("📂 添付ファイルフォルダを開く", variant="secondary")
                                delete_attachment_button = gr.Button("選択したファイルを削除", variant="stop")

                gr.Markdown(f"Nexus Ark {constants.APP_VERSION}", elem_id="app_version_display")



        # --- グローバル・右サイドバー (情景・プロフィール) ---
        with gr.Sidebar(label="情景・プロフィール", width=350, open=True, position="right", elem_id="right_sidebar"):
            with gr.Column(elem_classes=["sidebar-container"]):
                with gr.Accordion("🖼️ プロフィール・情景", open=True, elem_id="profile_scenery_accordion") as profile_scenery_accordion:
                    # --- プロフィール画像/アバター表示セクション ---
                    # gr.HTMLを使用して動画アバターまたは静止画を表示
                    # 動画がある場合はループ再生、ない場合は静止画にフォールバック
                    profile_image_display = gr.HTML(
                        value="",  # 初期値は空（handle_initial_loadで設定される）
                        elem_id="profile_avatar_container"
                    )
                    
                    # 60秒後に待機表情に戻るためのタイマー (constants.AVATAR_IDLE_TIMEOUT = 60)
                    auto_idle_timer = gr.Timer(constants.AVATAR_IDLE_TIMEOUT, active=False)

                    with gr.Accordion("🖼️ アバター・表情を管理", open=False) as profile_image_accordion:
                        avatar_mode_radio = gr.Radio(
                            choices=[("静止画 (profile.png)", "static"), ("動画 (idle.mp4等)", "video")],
                            value="static",
                            label="アバターモード",
                            info="「静止画」は従来のプロフィール画像、「動画」はループ再生されるアニメーション"
                        )
                        staged_image_state = gr.State()
                        
                        cropper_image_preview = gr.ImageEditor(
                            sources=["upload"], type="pil", interactive=True, show_label=False,
                            visible=False, transforms=["crop"], brush=None, eraser=None,
                        )
                        save_cropped_image_button = gr.Button("この範囲で保存", visible=False)
                        
                        # ★★★ 新規: 表情差分管理 ★★★
                        with gr.Accordion("🎭 表情差分の管理", open=False) as expression_management_accordion:
                            gr.Markdown(
                                "AIとの会話中、感情やタグに応じてアバターが切り替わります。ここでは登録済みの表情を確認・管理できます。"
                            )
                            
                            # 表情追加・編集・削除フォーム（操作ボタンを上に配置）
                            gr.Markdown("### 表情の管理")
                            with gr.Row():
                                # 登録済みの表情を選択（新規追加も兼用）
                                expressions_config = room_manager.get_expressions_config(effective_initial_room)
                                # 重複を除去: idle, thinking + expressions.json + DEFAULT_EXPRESSIONS
                                base_expressions = ["idle", "thinking"]
                                config_expressions = expressions_config.get("expressions", [])
                                # 統合リスト: base + config + DEFAULT（重複除去）
                                all_initial_choices = base_expressions.copy()
                                for e in config_expressions + constants.DEFAULT_EXPRESSIONS:
                                    if e not in all_initial_choices:
                                        all_initial_choices.append(e)
                                expression_target_dropdown = gr.Dropdown(
                                    choices=all_initial_choices,
                                    label="操作対象の表情を選択",
                                    allow_custom_value=True,
                                    info="既存の表情を更新するか、新しい表情名を入力してください。",
                                    scale=2
                                )
                                expression_file_upload = gr.UploadButton(
                                    "画像を紐付け / 更新", 
                                    file_types=["image", ".mp4", ".webm", ".gif"],
                                    scale=1
                                )
                            
                            with gr.Row():
                                add_expression_button = gr.Button("➕ リストに登録", variant="primary", scale=1)
                                delete_expression_button = gr.Button("🗑️ リストから削除", variant="stop", scale=1)
                            
                            gr.Markdown("💡 **idle / thinking** は状態表示用のため削除できません。その他の表情（感情カテゴリ含む）は自由に編集・削除可能です。")
                            
                            # 表情リスト表示 (カード形式) - 操作ボタンの下に配置
                            expressions_html = gr.HTML(
                                value=ui_handlers.refresh_expressions_ui(effective_initial_room),
                                label="登録済みの表情リスト"
                            )

                    # --- 情景ビジュアルセクション ---
                    # フルスクリーンボタンにバグがあるため無効化
                    scenery_image_display = gr.Image(label="現在の情景ビジュアル", interactive=False, height=200, show_label=False, show_fullscreen_button=False)
                    current_scenery_display = gr.Textbox( # ← ここに移動し、labelを削除
                        interactive=False, lines=4, max_lines=10, show_label=False,
                        placeholder="現在の情景が表示されます..."
                    )

                    # --- 移動メニュー ---
                    # [Fix] 初期化時にchoicesを設定
                    # location_dropdown の正しい初期値を計算
                    _loc_choices = ui_handlers._get_location_choices_for_ui(effective_initial_room)
                    _loc_val = None
                    if _loc_choices:
                         # ヘッダー以外で最初の有効な値を探す
                         valid_vals = [v for k, v in _loc_choices if not v.startswith("__AREA_HEADER_")]
                         if valid_vals: _loc_val = valid_vals[0]

                    location_dropdown = gr.Dropdown(
                        label="現在地 / 移動先を選択", 
                        choices=_loc_choices,
                        value=_loc_val,
                        interactive=True, allow_custom_value=True)

                    # --- 画像生成メニュー ---
                    with gr.Accordion("🌄情景設定・生成", open=False):
                        with gr.Accordion("季節・時間を指定", open=False) as time_control_accordion:
                            gr.Markdown("（この設定はルームごとに保存されます）", elem_id="time_control_note")
                            time_mode_radio = gr.Radio(
                                choices=["リアル連動", "選択する"],
                                label="モード選択",
                                interactive=True
                            )
                            with gr.Column(visible=False) as fixed_time_controls:
                                fixed_season_dropdown = gr.Dropdown(
                                    label="季節を選択",
                                    choices=["春", "夏", "秋", "冬"],
                                    interactive=True, allow_custom_value=True)
                                fixed_time_of_day_dropdown = gr.Dropdown(
                                    label="時間帯を選択",
                                    choices=["朝", "昼", "夕方", "夜"],
                                    interactive=True, allow_custom_value=True)
                            # ボタンを fixed_time_controls の外に移動し、常に表示されるようにする
                            save_time_settings_button = gr.Button("このルームの時間設定を保存", variant="secondary")
                                    
                        scenery_style_radio = gr.Dropdown(
                            choices=["写真風 (デフォルト)", "イラスト風", "アニメ風", "水彩画風"],
                            label="画風を選択", value="写真風 (デフォルト)", interactive=True, allow_custom_value=True)
                        generate_scenery_image_button = gr.Button("情景画像を生成 / 更新", variant="secondary")
                        refresh_scenery_button = gr.Button("情景テキストを更新", variant="secondary")

                        with gr.Accordion("🎨 情景画像プロンプトを出力", open=False):
                            gr.Markdown("外部の画像生成サービスで利用するための、現在の情景に基づいたプロンプトを生成します。")
                            scenery_prompt_output_textbox = gr.Textbox(
                                label="生成されたプロンプト",
                                interactive=False,
                                lines=5,
                                placeholder="下のボタンを押してプロンプトを生成します..."
                            )
                            generate_scenery_prompt_button = gr.Button("プロンプトを生成", variant="secondary")
                            copy_scenery_prompt_button = gr.Button("プロンプトをコピー")

                        with gr.Accordion("🏞️ カスタム情景画像の登録", open=False):
                            gr.Markdown("AI生成の代わりに、ご自身で用意した画像を情景として登録します。")
                            custom_scenery_location_dropdown = gr.Dropdown(
                                label="場所を選択", 
                                choices=_loc_choices, # 上で計算したものを使用
                                interactive=True, allow_custom_value=True)
                            with gr.Row():
                                custom_scenery_season_dropdown = gr.Dropdown(label="季節", choices=["春", "夏", "秋", "冬"], value="秋", interactive=True, allow_custom_value=True)
                                custom_scenery_time_dropdown = gr.Dropdown(label="時間帯", choices=["早朝", "朝", "昼前", "昼下がり", "夕方", "夜", "深夜"], value="夜", interactive=True, allow_custom_value=True)
                            custom_scenery_image_upload = gr.Image(label="画像をアップロード", type="filepath", interactive=True)
                            register_custom_scenery_button = gr.Button("この画像を情景として登録", variant="secondary")

        with gr.Tabs():
            with gr.TabItem("チャット"):
                # サブタブ構造: 会話表示 / RAWログエディタ
                with gr.Tabs():
                    with gr.TabItem("💬 会話") as chat_conversation_tab:
                        # --- 中央チャットエリア ---
                        with gr.Column(scale=1):
                            onboarding_guide = gr.Markdown(
                                """
                                ## Nexus Arkへようこそ！
                                **まずはAIと対話するための準備をしましょう。**
                                1.  **Google AI Studio** などで **Gemini APIキー** を取得してください。
                                2.  左カラムの **「⚙️ 設定」** を開きます。
                                3.  **「共通」** タブ内の **「🔑 APIキー / Webhook管理」** を開きます。
                                4.  **「Gemini APIキー」** の項目に、キーの名前（管理用のあだ名）と、取得したAPIキーの値を入力し、**「Geminiキーを保存」** ボタンを押してください。

                                設定が完了すると、このメッセージは消え、チャットが利用可能になります。
                                """,
                                visible=False, # 初期状態では非表示
                                elem_id="onboarding_guide"
                            )

                            chatbot_display = gr.Chatbot(
                                height=580, 
                                elem_id="chat_output_area",
                                show_copy_button=True,
                                show_label=False,
                                render_markdown=True,
                                type="tuples",
                                group_consecutive_messages=False,
                                editable="all" 
                            )

                            with gr.Row():
                                audio_player = gr.Audio(label="音声プレーヤー", visible=False, autoplay=True, interactive=True, elem_id="main_audio_player")
                            with gr.Row(visible=False) as action_button_group:
                                rerun_button = gr.Button("🔄 再生成")
                                play_audio_button = gr.Button("🔊 選択した発言を再生")
                                delete_selection_button = gr.Button("🗑️ 選択した発言を削除", variant="stop")
                                cancel_selection_button = gr.Button("✖️ 選択をキャンセル")

                            chat_input_multimodal = gr.MultimodalTextbox(
                                file_types=["image", "audio", "video", "text", ".pdf", ".md", ".py", ".json", ".html", ".css", ".js"],
                                file_count="multiple",  # 複数ファイルの添付を許可
                                max_plain_text_length=100000,
                                placeholder="メッセージを入力してください (Shift+Enterで送信)",
                                show_label=False,
                                lines=3,
                                interactive=True
                            )

                            token_count_display = gr.Markdown(
                                "入力トークン数: 0 / 0",
                                elem_id="token_count_display"
                            )

                            with gr.Row():
                                stop_button = gr.Button("⏹️ ストップ", variant="stop", visible=False, scale=1)
                                chat_reload_button = gr.Button("🔄 履歴を更新", scale=1)

                            with gr.Row():
                                add_log_to_memory_queue_button = gr.Button("現在の対話を記憶に追加", scale=1, visible=False)

                            # --- 書き置き機能（自律行動時に伝えるメッセージ）---
                            with gr.Accordion("📝 書き置き（自律行動時に伝える）", open=False):
                                gr.Markdown("次回の自律行動時にAIに渡されます。送信後は自動でクリアされます。")
                                user_memo_textbox = gr.Textbox(
                                    label="書き置き内容",
                                    lines=3,
                                    placeholder="例: 今から外出するよ / 今日は仕事でバタバタ",
                                    interactive=True
                                )
                                with gr.Row():
                                    save_user_memo_button = gr.Button("💾 保存", size="sm", variant="primary")
                                    clear_user_memo_button = gr.Button("🗑️ クリア", size="sm", variant="secondary")

                            # --- チェスアコーディオン ---
                            with gr.Accordion("♟️ チェス（ペルソナと対戦）", open=False):
                                gr.Markdown("駒を動かすと、ペルソナもそれを認識します。ツールを使ってペルソナに動かしてもらうことも可能です。")
                                with gr.Row():
                                    with gr.Column(scale=2):
                                        chess_board_html = gr.HTML("""
                                            <div id="chess_board_container" style="width: 100%; max-width: 400px; margin: 0 auto;"></div>
                                            <link rel="stylesheet" href="https://unpkg.com/@chrisoakman/chessboardjs@1.0.0/dist/chessboard-1.0.0.min.css" />
                                        """)
                                        init_board_button = gr.Button("チェス盤をセット・再開", variant="secondary", size="sm")

                                    with gr.Column(scale=1):
                                        reset_game_button = gr.Button("リセット", variant="secondary", size="sm")
                                        free_move_mode_cb = gr.Checkbox(
                                            label="フリームーブモード",
                                            value=False,
                                            info="有効にすると、ルールに関係なく自由に駒を配置できます"
                                        )
                                        toggle_turn_button = gr.Button("手番を切替", variant="secondary", size="sm", visible=False)
                                        force_sync_button = gr.Button("盤面を強制同期", variant="secondary", size="sm", visible=False)
                                        game_status_output = gr.Textbox(label="ステータス", interactive=False, value="チェス盤をセットしてください", lines=1)
                                        # Hidden components for JS<->Python communication
                                        user_move_input = gr.Textbox(visible=True, elem_id="user_move_input", lines=1, label="Debug Input (Do Not Edit)")
                                        board_fen_state = gr.Textbox(visible=False, elem_id="board_fen_state")

                                # --- Python function to initialize with room-based persistence ---
                                def init_chess_board(room_name, free_mode):
                                    """Initialize chess board with room-specific saved state."""
                                    if room_name:
                                        # Force reload from disk to ensure we have the latest state
                                        game_instance.set_room(room_name, force_reload=True)
                                    
                                    fen = game_instance.get_fen()
                                    turn = "白番" if fen.split(' ')[1] == 'w' else "黒番"
                                    msg = f"フリームーブ ON ({turn})" if free_mode else f"Loaded: {fen[:15]}... ({turn})"
                                    return fen, msg

                                # --- JavaScript Definition ---
                                init_chess_js = """
                                async (fen) => {
                                    const ta = document.querySelector("#user_move_input textarea");
                                    const updateDebug = (msg) => {
                                        if(ta) {
                                          ta.value = JSON.stringify({error: msg});
                                          ta.dispatchEvent(new Event("input", { bubbles: true }));
                                        }
                                    };
                                    
                                    const loadScript = (src) => {
                                        return new Promise((resolve, reject) => {
                                            if(document.querySelector(`script[src="${src}"]`)) { resolve(); return; }
                                            const s = document.createElement('script');
                                            s.src = src;
                                            s.onload = () => resolve();
                                            s.onerror = () => reject(new Error(`Failed to load: ${src}`));
                                            document.head.appendChild(s);
                                        });
                                    };

                                    try {
                                        updateDebug("Loading...");
                                        await loadScript("https://code.jquery.com/jquery-3.6.0.min.js");
                                        await loadScript("https://unpkg.com/@chrisoakman/chessboardjs@1.0.0/dist/chessboard-1.0.0.min.js");
                                        
                                        const container = document.getElementById("chess_board_container");
                                        if(!container) throw new Error("Container not found");
                                        
                                        // If fen is null/empty (free move mode), preserve current board if it exists
                                        let currentPosition = null;
                                        if(window.chess_board_obj && (!fen || fen === "")) {
                                            try { currentPosition = window.chess_board_obj.position('fen'); } catch(e) {}
                                        }
                                        
                                        if(window.chess_board_obj) {
                                            try { window.chess_board_obj.destroy(); } catch(e) {}
                                        }
                                        container.innerHTML = "";

                                        // Prioritize FEN from Python if it's a valid position string.
                                        // This ensures that clicking "Set/Resume" actually loads the server state.
                                        const position = ((fen && fen.length > 10) ? fen : (currentPosition || 'start'));
                                        console.log("Initializing chess board with position:", position);
                                        
                                        window.chess_board_obj = Chessboard(container, {
                                            position: position,
                                            draggable: true,
                                            pieceTheme: 'https://chessboardjs.com/img/chesspieces/wikipedia/{piece}.png',
                                            onDragStart: function(source, piece, position, orientation) {
                                                window.isDragging = true;
                                            },
                                            onDrop: function(source, target, piece, newPos, oldPos, orient) {
                                                window.isDragging = false;
                                                if(source === target) return;
                                                // We will sync ONLY onSnapEnd to ensure animations are finished
                                                // and avoid dual-message race conditions.
                                                window.lastMove = {from: source, to: target};
                                            },
                                            onSnapEnd: function() {
                                                if(window.chess_board_obj && ta) {
                                                    const fen = window.chess_board_obj.position('fen');
                                                    const msg = {sync_fen: fen};
                                                    if(window.lastMove) {
                                                        msg.from = window.lastMove.from;
                                                        msg.to = window.lastMove.to;
                                                        window.lastMove = null;
                                                    }
                                                    ta.value = JSON.stringify(msg);
                                                    ta.dispatchEvent(new Event("input", { bubbles: true }));
                                                }
                                            }
                                        });
                                        
                                        window.updateBoardFromFen = (fen) => {
                                            if(!window.chess_board_obj) return;
                                            
                                            // Skip update if user is dragging a piece
                                            if(window.isDragging) {
                                                console.log("Skipping update since dragging");
                                                return;
                                            }

                                            const currentFen = window.chess_board_obj.position('fen');
                                            // Only update if FEN actually changed (ignoring move counts/en passant parts for visual board)
                                            // chess.Board.fen() includes full state, chessboardjs uses only placement
                                            // So we check if placement part is different
                                            const placement = fen.split(' ')[0];
                                            const currentPlacement = currentFen; // chessboardjs returns placement or object
                                            
                                            // Simple check: update position
                                            if (currentFen !== placement) {
                                                console.log("Updating board from server:", placement);
                                                window.chess_board_obj.position(placement);
                                            }
                                        };
                                        
                                        window.forceSyncBoard = () => {
                                             if(ta) {
                                                const fen = window.chess_board_obj.position('fen');
                                                ta.value = JSON.stringify({force: true, sync_fen: fen});
                                                ta.dispatchEvent(new Event("input", { bubbles: true }));
                                             }
                                        };
                                        
                                        updateDebug("Ready!");
                                    } catch(e) {
                                        console.error(e);
                                        updateDebug("Error: " + e.message);
                                    }
                                }
                                """

                                # Event Wiring for Chess - Python first (sets room & loads state), then JS
                                init_board_button.click(
                                    fn=init_chess_board,
                                    inputs=[current_room_name, free_move_mode_cb],
                                    outputs=[board_fen_state, game_status_output]
                                ).then(
                                    fn=None,
                                    inputs=[board_fen_state],
                                    outputs=[],
                                    js=init_chess_js
                                )

                                def handle_debug_or_move(data_json, free_mode):
                                    if not data_json: return game_instance.get_fen(), "No Data"
                                    try:
                                        print(f"  - [Chess DEBUG] Received: {data_json}")
                                        data = json.loads(data_json)
                                        if "error" in data:
                                            return game_instance.get_fen(), data['error']
                                        
                                        # Handle Sync (either standalone or combined with move)
                                        sync_successful = False
                                        if "sync_fen" in data:
                                            sync_fen = data["sync_fen"]
                                            if free_mode and sync_fen:
                                                current_full_fen = game_instance.get_fen()
                                                fen_parts = current_full_fen.split(' ')
                                                # Use incoming placement, keep other markers (turn, castling, etc)
                                                new_full_fen = f"{sync_fen} {' '.join(fen_parts[1:])}"
                                                game_instance.set_position_free(new_full_fen)
                                                sync_successful = True
                                                print(f"  - [Chess DEBUG] Sync successful: {sync_fen}")
                                        
                                        # Handle Move in Free Mode (Informational only, persistence is handled by sync_fen)
                                        if free_mode and "from" in data:
                                            start_sq = data.get("from")
                                            end_sq = data.get("to")
                                            turn = get_turn_text()
                                            status = f"Free: {start_sq} → {end_sq} ({turn})"
                                            # Return current backend FEN to ensure UI is in sync with persistence
                                            return game_instance.get_fen(), status
                                        
                                        # Handle Force Sync status
                                        if data.get("force"):
                                            return None, f"盤面を強制同期しました ({get_turn_text()})"
                                        
                                        # Normal Mode Move
                                        if not free_mode and "from" in data:
                                            return handle_user_chess_move(data_json)
                                            
                                        return None, gr.skip()
                                    except Exception as e:
                                        print(f"  - [Chess DEBUG] Error: {e}")
                                        return game_instance.get_fen(), f"Error: {e}"

                                user_move_input.change(fn=handle_debug_or_move, inputs=[user_move_input, free_move_mode_cb], outputs=[board_fen_state, game_status_output])
                                
                                # Only update UI board when fen is not None (skip in free move mode)
                                board_fen_state.change(fn=None, inputs=[board_fen_state], js="(fen) => { if(fen && window.updateBoardFromFen) window.updateBoardFromFen(fen); }")
                                
                                def get_turn_text():
                                    """Get current turn as readable text."""
                                    fen = game_instance.get_fen()
                                    turn = fen.split(' ')[1] if ' ' in fen else 'w'
                                    return "白番" if turn == 'w' else "黒番"
                                
                                def reset_chess_game_fn():
                                    game_instance.reset_board()
                                    return game_instance.get_fen(), f"リセット完了 ({get_turn_text()})"
                                reset_game_button.click(fn=reset_chess_game_fn, outputs=[board_fen_state, game_status_output])
                                
                                def toggle_free_move_mode(enabled):
                                    game_instance.set_free_move_mode(enabled)
                                    turn = get_turn_text()
                                    mode_text = f"フリームーブ ON ({turn})" if enabled else f"通常モード ({turn})"
                                    # Show/hide toggle turn/force sync buttons based on free move mode
                                    return mode_text, gr.update(visible=enabled), gr.update(visible=enabled)
                                free_move_mode_cb.input(fn=toggle_free_move_mode, inputs=[free_move_mode_cb], outputs=[game_status_output, toggle_turn_button, force_sync_button])
                                
                                def handle_toggle_turn():
                                    result = game_instance.toggle_turn()
                                    if result:
                                        turn_text = "黒番" if result == 'b' else "白番"
                                        return f"手番切替: {turn_text}"
                                    return "Error"
                                toggle_turn_button.click(fn=handle_toggle_turn, outputs=[game_status_output])
                                
                                force_sync_button.click(fn=None, inputs=[], outputs=[], js="() => { if(window.forceSyncBoard) window.forceSyncBoard(); }")
                                
                                # Polling timer to sync board state (only in normal mode)
                                board_sync_timer = gr.Timer(1.0)
                                def sync_board_if_normal(free_mode):
                                    # Always return FEN.
                                    # JS side will decide whether to apply it (e.g., skip if dragging).
                                    return game_instance.get_fen()
                                board_sync_timer.tick(fn=sync_board_if_normal, inputs=[free_move_mode_cb], outputs=[board_fen_state])

                    with gr.TabItem("📝 ログ管理") as chat_log_management_tab:
                        gr.Markdown(
                            "過去の会話ログの閲覧・編集・検索ができます。\n\n"
                            "> **⚠️ 注意:** 保存前に自動バックアップが作成されますが、書式（## USER: 等）を崩すと表示が壊れる可能性があります。"
                        )
                        with gr.Row():
                            chat_log_month_dropdown = gr.Dropdown(
                                choices=["最新"],
                                value="最新",
                                label="表示する月を選択",
                                interactive=True,
                                scale=2, allow_custom_value=True)
                            refresh_chat_log_months_button = gr.Button("🔄 リスト更新", scale=1)

                        with gr.Row():
                            chat_log_search_textbox = gr.Textbox(
                                label="ログ内をキーワード検索",
                                placeholder="検索したい単語を入力（空欄で検索すると全件表示）",
                                scale=3
                            )
                            chat_log_search_button = gr.Button("🔍 検索", variant="secondary", scale=1)

                        with gr.Tabs():
                            with gr.TabItem("📄 RAWエディタ"):
                                chat_log_raw_editor = gr.Code(
                                    label="ログの内容 (Markdown形式)",
                                    language="markdown",
                                    interactive=True,
                                    lines=25,
                                    elem_id="chat_log_raw_editor"
                                )
                                with gr.Row():
                                    save_chat_log_button = gr.Button("💾 編集内容を保存", variant="primary")
                                    reload_chat_log_button = gr.Button("🔄 変更を破棄して再読込", variant="secondary")
                            
                            with gr.TabItem("💬 チャット形式プレビュー") as chat_log_preview_tab:
                                chat_log_preview_chatbot = gr.Chatbot(
                                    label="ログのプレビュー (閲覧専用)",
                                    elem_id="chat_log_preview_chatbot",
                                    height=600,
                                    latex_delimiters=[],
                                    show_copy_button=True,
                                    type="tuples"
                                )


            with gr.TabItem("📝 記憶・ノート・知識"):
                gr.Markdown("##  記憶・ノート・知識\nルームの根幹をなす設定ファイルを、ここで直接編集できます。")
                with gr.Tabs():
                    with gr.TabItem("記憶"):
                        # --- システムプロンプト (Accordion) ---
                        with gr.Accordion("📜 システムプロンプト (ペルソナ設定)", open=False) as system_prompt_accordion:
                            system_prompt_editor = gr.Textbox(label="SystemPrompt.txt", interactive=True, elem_id="system_prompt_editor", lines=15, autoscroll=True)
                            with gr.Row():
                                save_prompt_button = gr.Button("保存", variant="secondary")
                                reload_prompt_button = gr.Button("再読込", variant="secondary")

                        # --- コアメモリ (Accordion) ---
                        with gr.Accordion("💎 コアメモリ (自己同一性の核)", open=False) as core_memory_accordion:
                            core_memory_editor = gr.Textbox(
                                label="core_memory.txt - AIの自己同一性の核",
                                interactive=True,
                                elem_id="core_memory_editor_code",
                                lines=15,
                                autoscroll=True
                            )
                            with gr.Row():
                                save_core_memory_button = gr.Button("保存", variant="secondary")
                                reload_core_memory_button = gr.Button("再読込", variant="secondary")

                        # --- 日記 (Accordion) ---
                        with gr.Accordion("📝 主観的記憶（日記）", open=False) as memory_main_accordion:
                            gr.Markdown("ペルソナの主観的な記録です。感情、思考、重要な出来事を書き留めます。")
                            with gr.Row():
                                refresh_diary_button = gr.Button("📚 エントリを読み込む", variant="primary")
                                show_latest_diary_button = gr.Button("📄 最新を表示", variant="secondary")
                                core_memory_update_button = gr.Button("コアメモリを更新", variant="secondary")
                            
                            with gr.Row():
                                diary_year_filter = gr.Dropdown(label="年で絞り込む", choices=["すべて"], value="すべて", scale=1, allow_custom_value=True)
                                diary_month_filter = gr.Dropdown(label="月で絞り込む", choices=["すべて"], value="すべて", scale=1, allow_custom_value=True)
                            
                            with gr.Row():
                                with gr.Column(scale=1):
                                    diary_entry_dropdown = gr.Dropdown(
                                        label="エントリを選択",
                                        choices=[],
                                        interactive=True,
                                        info="最新のエントリが上に表示されます", allow_custom_value=True)
                                with gr.Column(scale=2):
                                    memory_txt_editor = gr.Textbox(
                                        label="エントリの内容",
                                        interactive=True,
                                        elem_id="memory_txt_editor_code",
                                        lines=15,
                                        max_lines=20,
                                        placeholder="エントリを選択するか、「RAW編集」で直接編集してください"
                                    )
                            
                            with gr.Row():
                                save_memory_button = gr.Button("選択エントリを保存", variant="secondary")
                                reload_memory_button = gr.Button("再読込", variant="secondary")
                            
                            with gr.Accordion("📝 RAW編集（全文）", open=False):
                                diary_raw_editor = gr.Textbox(
                                    label="memory_main.txt 全文",
                                    interactive=True,
                                    lines=15,
                                    max_lines=25,
                                    autoscroll=True,
                                    elem_id="diary_raw_editor",
                                    placeholder="ファイル全体を直接編集できます"
                                )
                                with gr.Row():
                                    save_diary_raw_button = gr.Button("RAW全文を保存", variant="primary")
                                    reload_diary_raw_button = gr.Button("RAW再読込", variant="secondary")
                            
                            # --- 古い日記のアーカイブ ---
                            with gr.Accordion("📦 古い日記をアーカイブする", open=False) as memory_archive_accordion:
                                gr.Markdown(
                                    "指定した日付**まで**の日記を要約し、別ファイルに保存して、このメインファイルから削除します。\n"
                                    "**⚠️注意:** この操作は`memory_main.txt`を直接変更します（処理前にバックアップは作成されます）。"
                                )
                                archive_date_dropdown = gr.Dropdown(label="この日付までをアーカイブ", interactive=True, allow_custom_value=True)
                               
                                archive_confirm_state = gr.Textbox(visible=False) # 確認ダイアログ用
                                archive_memory_button = gr.Button("アーカイブを実行", variant="stop")

                        # --- [Phase 14] エピソード記憶閲覧 ---
                        with gr.Accordion("📚 エピソード記憶（中期記憶）の管理", open=False):
                            episodic_memory_info_display = gr.Markdown("昨日までの会話ログを日ごとに要約し、中期記憶として保存します。\n**最新の記憶:** (未取得)")
                            with gr.Row():
                                refresh_episodic_button = gr.Button("📚 エピソード記憶を読み込む", variant="primary")
                                show_latest_episodic_button = gr.Button("📄 最新を表示", variant="secondary")
                            
                            with gr.Row():
                                episodic_year_filter = gr.Dropdown(label="年で絞り込む", choices=["すべて"], value="すべて", scale=1, allow_custom_value=True)
                                episodic_month_filter = gr.Dropdown(label="月で絞り込む", choices=["すべて"], value="すべて", scale=1, allow_custom_value=True)
                            
                            with gr.Row():
                                with gr.Column(scale=1):
                                    episodic_date_dropdown = gr.Dropdown(
                                        label="閲覧するエピソードの日付を選択",
                                        choices=[],
                                        interactive=True,
                                        info="最新のエピソードが上に表示されます。", allow_custom_value=True)
                                with gr.Column(scale=2):
                                    episodic_detail_text = gr.Textbox(
                                        label="エピソードの内容",
                                        lines=15,
                                        interactive=False,
                                        autoscroll=False,
                                        placeholder="日付を選択すると、ここに詳細が表示されます。"
                                    )

                        # --- 夢日記 ---
                        with gr.Accordion("🌙 夢日記 (Dream Journal)", open=False):
                            gr.Markdown("AIが通知禁止時間帯（寝ている間）に見た夢の記録です。\n過去の記憶と直近の出来事を照らし合わせ、AIが得た「洞察」や「深層心理」を閲覧できます。")
                            with gr.Row():
                                refresh_dream_button = gr.Button("🌛 夢日記を読み込む", variant="primary")
                                show_latest_dream_button = gr.Button("📄 最新を表示", variant="secondary")
                            
                            with gr.Row():
                                dream_year_filter = gr.Dropdown(label="年で絞り込む", choices=["すべて"], value="すべて", scale=1, allow_custom_value=True)
                                dream_month_filter = gr.Dropdown(label="月で絞り込む", choices=["すべて"], value="すべて", scale=1, allow_custom_value=True)
                            
                            with gr.Row():
                                with gr.Column(scale=1):
                                    dream_date_dropdown = gr.Dropdown(
                                        label="閲覧する日記の日付を選択",
                                        choices=[],
                                        interactive=True,
                                        info="最新の日記が上に表示されます。", allow_custom_value=True)
                                with gr.Column(scale=2):
                                    dream_detail_text = gr.Textbox(
                                        label="夢の詳細・深層心理",
                                        lines=15,
                                        interactive=False,
                                        placeholder="日付を選択すると、ここに詳細が表示されます。"
                                    )
                            
                        # --- 📌 エンティティ記憶 (Entity Memory) ---
                        with gr.Accordion("📌 エンティティ記憶 (Entity Memory)", open=False):
                            gr.Markdown("会話から抽出された重要な物事や人物（エンティティ）に関する詳細な記録です。")
                            refresh_entity_button = gr.Button("📌 エンティティ一覧を読み込む", variant="primary")
                            
                            with gr.Row():
                                with gr.Column(scale=1):
                                    entity_dropdown = gr.Dropdown(
                                        label="エンティティを選択",
                                        choices=[],
                                        interactive=True,
                                        info="自動・手動で作成されたエンティティが一覧表示されます。", allow_custom_value=True)
                                    with gr.Row():
                                        save_entity_button = gr.Button("変更を保存", variant="secondary")
                                        delete_entity_button = gr.Button("削除", variant="stop")
                                with gr.Column(scale=2):
                                    entity_content_editor = gr.Textbox(
                                        label="記録内容 (.md)",
                                        lines=15,
                                        interactive=True,
                                        placeholder="エンティティを選択すると、ここに内容が表示されます。直接編集して保存することも可能です。"
                                    )

                        # --- 🎯 目標 (Goals) ---
                        with gr.Accordion("🎯 目標 (Goals)", open=False):
                            gr.Markdown("ペルソナが睡眠時省察で自発的に立てた目標です。短期目標と長期目標を確認できます。")
                            refresh_goals_button = gr.Button("🎯 目標を読み込む", variant="primary")
                            
                            with gr.Row():
                                with gr.Column(scale=1):
                                    gr.Markdown("#### 短期目標")
                                    short_term_goals_display = gr.Textbox(
                                        label="",
                                        lines=5,
                                        max_lines=10,
                                        interactive=False,
                                        placeholder="目標を読み込むと表示されます"
                                    )
                                with gr.Column(scale=1):
                                    gr.Markdown("#### 長期目標")
                                    long_term_goals_display = gr.Textbox(
                                        label="",
                                        lines=5,
                                        max_lines=10,
                                        interactive=False,
                                        placeholder="目標を読み込むと表示されます"
                                    )
                            
                            with gr.Row():
                                goals_meta_display = gr.Textbox(
                                    label="省察メタデータ",
                                    lines=2,
                                    interactive=False,
                                    placeholder="最終省察レベル、週次/月次省察の日付が表示されます"
                                )

                        # --- 🧠 自己意識 (Self-Awareness) ---
                        with gr.Accordion("🧠 自己意識 (Self-Awareness)", open=False):
                            gr.Markdown("ペルソナの内発的な動機と、気になっている話題を確認できます。")
                            refresh_internal_state_button = gr.Button("🧠 内的状態を読み込む", variant="primary")
                            
                            gr.Markdown("#### 📊 現在の動機レベル")
                            with gr.Row():
                                with gr.Column(scale=1):
                                    boredom_level_display = gr.Slider(
                                        label="退屈 (Boredom)", minimum=0, maximum=1, value=0,
                                        interactive=False, info="無操作時間に比例"
                                    )
                                    curiosity_level_display = gr.Slider(
                                        label="好奇心 (Curiosity)", minimum=0, maximum=1, value=0,
                                        interactive=False, info="未解決の問いに比例"
                                    )
                                with gr.Column(scale=1):
                                    goal_achievement_level_display = gr.Slider(
                                        label="目標達成欲 (Goal Drive)", minimum=0, maximum=1, value=0,
                                        interactive=False, info="アクティブな目標に比例"
                                    )
                                    devotion_level_display = gr.Slider(
                                        label="関係性維持 (Relatedness)", minimum=0, maximum=1, value=0,
                                        interactive=False, info="ペルソナ感情に比例"
                                    )
                            
                            dominant_drive_display = gr.Textbox(
                                label="現在の最強動機",
                                lines=3,
                                interactive=False,
                                placeholder="読み込むと表示されます"
                            )
                            
                            gr.Markdown("#### ❓ 未解決の問い（好奇心の源泉）")
                            gr.Markdown("行を選択してから操作ボタンをクリックしてください。", elem_id="open_questions_hint")
                            open_questions_display = gr.Dataframe(
                                headers=["話題", "背景・文脈", "優先度", "尋ねた日時"],
                                datatype=["str", "str", "number", "str"],
                                row_count=(3, "dynamic"),
                                col_count=(4, "fixed"),
                                interactive=True,  # 選択可能に
                                wrap=True
                            )
                            selected_question_topics_state = gr.State([])  # 選択された話題リスト
                            
                            with gr.Row():
                                resolve_selected_questions_button = gr.Button("✅ 選択を解決済みに", variant="secondary")
                                delete_selected_questions_button = gr.Button("🗑️ 選択を削除", variant="stop")
                                clear_open_questions_button = gr.Button("🗑️ 全てクリア", variant="stop")
                            
                            open_questions_status = gr.Markdown("---")
                            
                            gr.Markdown("#### 📈 感情モニタリング")
                            user_emotion_history_plot = gr.ScatterPlot(
                                x="timestamp", 
                                y="intensity",
                                color="emotion",
                                title="ペルソナ感情の推移",
                                tooltip=["timestamp", "emotion", "intensity"],
                                height=250,
                                width="100%",
                                interactive=False
                            )
                            
                            internal_state_last_update = gr.Markdown("最終更新: ---")

                        with gr.Accordion("💫 睡眠時記憶整理 (Sleep Consolidation)", open=False):

                            gr.Markdown(
                                "**発生条件:** 自律行動が有効で、通知禁止時間帯（デフォルト: 0:00〜7:00）に無操作時間を超過すると、AIは「眠り」に入り夢日記を作成します。\n\n"
                                "夢日記を作成する際に、以下の処理も連続して実行します。（チェックを変更すると即座に保存されます）"
                            )
                            sleep_consolidation_episodic_cb = gr.Checkbox(
                                label="エピソード記憶を作成・更新する",
                                value=True,
                                interactive=True
                            )
                            sleep_consolidation_memory_index_cb = gr.Checkbox(
                                label="記憶の索引を更新する",
                                value=True,
                                interactive=True
                            )
                            sleep_consolidation_current_log_cb = gr.Checkbox(
                                label="現行ログの索引を更新する（時間がかかります）",
                                value=False,  # デフォルトOFF（時間がかかるため）
                                interactive=True
                            )
                            sleep_consolidation_entity_memory_cb = gr.Checkbox(
                                label="エンティティ記憶を更新する",
                                value=True,
                                interactive=True,
                                info="会話から重要な対象（人物・事物）の情報を整理"
                            )
                            # Parameters moved to Maintenance Accordion
                            sleep_consolidation_compress_cb = gr.Checkbox(
                                label="📦 古い記憶を圧縮する",
                                value=False,  # デフォルトOFF（破壊的操作のため）
                                interactive=True,
                                info="3日以上前のエピソード記憶を週単位に統合"
                            )
                            sleep_consolidation_extract_questions_cb = gr.Checkbox(
                                label="❓ 未解決の問いを抽出する",
                                value=True,  # デフォルトON
                                interactive=True,
                                info="会話から「気になること」を抽出し、好奇心の源泉として記録"
                            )


                        # --- [Phase 14] 🛠️ 記憶のメンテナンス (手動実行) ---
                        with gr.Accordion("🛠️ 記憶のメンテナンス (手動実行)", open=False) as maintenance_accordion:
                            gr.Markdown("大規模な記憶の更新や、データの最適化を手動で実行します。")
                            
                            with gr.Row():
                                with gr.Column():
                                    gr.Markdown("### 📚 エピソード記憶の更新")
                                    update_episodic_memory_button = gr.Button("エピソード記憶を今すぐ更新", variant="primary")
                                    episodic_update_status = gr.Textbox(label="エピソード更新ステータス", interactive=False, placeholder="更新を実行すると、ここに最終処理日等が表示されます")

                                with gr.Column():
                                    gr.Markdown("### 📌 エンティティ記憶 (Entity Memory) の更新")
                                    manual_dream_button = gr.Button("エンティティ記憶を更新（睡眠時記憶整理を実行）", variant="primary")
                                    dream_status_display = gr.Textbox(label="最終実行日時", interactive=False, placeholder="まだ実行されていません")
                                


                            gr.Markdown("---")
                            with gr.Row():
                                with gr.Column():
                                    gr.Markdown("### 🔍 記憶索引 (RAG) の再構築")
                                    memory_reindex_button = gr.Button("記憶の索引を更新", variant="secondary")
                                    full_reindex_button = gr.Button("🗑️ 索引を初期化して再構築", variant="stop")
                                    gr.Markdown("<small>⚠️ モデル変更時はこちらを使用してください</small>")
                                    memory_reindex_status = gr.Textbox(label="記憶索引ステータス", interactive=False)
                                
                                with gr.Column():
                                    gr.Markdown("### 🔍 現行ログの索引更新")
                                    current_log_reindex_button = gr.Button("現行ログの索引を更新", variant="secondary")
                                    current_log_reindex_status = gr.Textbox(label="現行ログ索引ステータス", interactive=False)

                            gr.Markdown("---")
                            gr.Markdown("### 📦 記憶の圧縮 (Archive)")
                            gr.Markdown("3日以上経過した記憶を週・月単位に圧縮し、RAGの検索効率を向上させます。")
                            compress_episodes_button = gr.Button("古い記憶を手動で圧縮する", variant="secondary")
                            compress_episodes_status = gr.Textbox(label="圧縮ステータス", interactive=False)
                            
                            gr.Markdown("---")
                            gr.Markdown("### 🧠 内部状態のリセット")
                            gr.Markdown("動機レベル、未解決の問い、最終発火時刻をすべてリセットします。")
                            reset_internal_state_button = gr.Button("🧹 内部状態をリセット", variant="stop")
                            reset_internal_state_status = gr.Textbox(label="リセットステータス", interactive=False)



                    with gr.TabItem("知識グラフ管理", visible=False):
                        gr.Markdown("## 知識グラフの管理")
                        gr.Markdown("過去の対話ログを分析し、エンティティ間の関係性を抽出して、AIの永続的な知識グラフを構築・更新します。")
                        with gr.Row():
                            memos_import_button = gr.Button("過去ログから記憶を構築", variant="primary", scale=3)
                            importer_stop_button = gr.Button("処理を中断", variant="stop", visible=False, scale=1)
                        gr.Markdown("---")
                        with gr.Row():
                            visualize_graph_button = gr.Button("現在の知識グラフを可視化する")
                        graph_image_display = gr.Image(label="知識グラフの可視化結果", interactive=False, visible=False)
                        gr.Markdown("---")
                        gr.Markdown("### 索引管理（旧機能）")
                        rag_update_button = gr.Button("手帳の索引を更新", variant="secondary", visible=False)
                    with gr.TabItem("ノート"):
                        # --- メモ帳アコーディオン ---
                        with gr.Accordion("📝 メモ帳（共有ホワイトボード）", open=False):
                            gr.Markdown("ユーザーとペルソナが共有するメモ帳です。")
                            notepad_editor = gr.Textbox(label="メモ帳の内容", interactive=True, elem_id="notepad_editor_code", lines=15, autoscroll=True)
                            with gr.Row():
                                save_notepad_button = gr.Button("保存", variant="secondary")
                                reload_notepad_button = gr.Button("再読込", variant="secondary")
                                clear_notepad_button = gr.Button("全削除", variant="stop")
                        
                        # --- 創作ノートアコーディオン ---
                        with gr.Accordion("🎨 創作ノート", open=False):
                            gr.Markdown("ペルソナの創作活動専用スペースです。詩、物語、アイデアスケッチなど。")
                            with gr.Row():
                                creative_notes_file_dropdown = gr.Dropdown(label="対象ファイル", choices=[constants.CREATIVE_NOTES_FILENAME], value=constants.CREATIVE_NOTES_FILENAME, scale=3, allow_custom_value=True)
                                refresh_creative_file_list_button = gr.Button("📁 リスト更新", scale=1)
                                refresh_creative_notes_button = gr.Button("📚 読込", variant="primary", scale=1)
                                show_latest_creative_button = gr.Button("📄 最新", variant="secondary", scale=1)
                            
                            with gr.Row():
                                creative_year_filter = gr.Dropdown(label="年で絞り込む", choices=["すべて"], value="すべて", scale=1, allow_custom_value=True)
                                creative_month_filter = gr.Dropdown(label="月で絞り込む", choices=["すべて"], value="すべて", scale=1, allow_custom_value=True)
                            
                            with gr.Row():
                                with gr.Column(scale=1):
                                    creative_entry_dropdown = gr.Dropdown(
                                        label="エントリを選択",
                                        choices=[],
                                        interactive=True,
                                        info="最新のエントリが上に表示されます", allow_custom_value=True)
                                with gr.Column(scale=2):
                                    creative_notes_editor = gr.Textbox(
                                        label="エントリの内容",
                                        interactive=True,
                                        elem_id="creative_notes_editor_code",
                                        lines=15,
                                        max_lines=20,
                                        placeholder="エントリを選択するか、「RAW編集」で直接編集してください"
                                    )
                            
                            with gr.Row():
                                save_creative_notes_button = gr.Button("選択エントリを保存", variant="secondary")
                                reload_creative_notes_button = gr.Button("再読込", variant="secondary")
                            
                            with gr.Accordion("📝 RAW編集（全文）", open=False):
                                creative_notes_raw_editor = gr.Textbox(
                                    label="creative_notes.md 全文",
                                    interactive=True,
                                    lines=15,
                                    max_lines=25,
                                    autoscroll=True,
                                    elem_id="creative_notes_raw_editor",
                                    placeholder="ファイル全体を直接編集できます"
                                )
                                with gr.Row():
                                    save_creative_raw_button = gr.Button("RAW全文を保存", variant="primary")
                                    reload_creative_raw_button = gr.Button("RAW再読込", variant="secondary")
                        
                        # --- 研究・分析ノートアコーディオン ---
                        with gr.Accordion("🔬 研究・分析ノート", open=False):
                            gr.Markdown("Web巡回ツールによる分析結果や洞察が蓄積されるスペースです。AIが自律的に更新します。")
                            with gr.Row():
                                research_notes_file_dropdown = gr.Dropdown(label="対象ファイル", choices=[constants.RESEARCH_NOTES_FILENAME], value=constants.RESEARCH_NOTES_FILENAME, scale=3, allow_custom_value=True)
                                refresh_research_file_list_button = gr.Button("📁 リスト更新", scale=1)
                                refresh_research_notes_button = gr.Button("📚 読込", variant="primary", scale=1)
                                show_latest_research_button = gr.Button("📄 最新", variant="secondary", scale=1)
                            
                            with gr.Row():
                                research_year_filter = gr.Dropdown(label="年で絞り込む", choices=["すべて"], value="すべて", scale=1, allow_custom_value=True)
                                research_month_filter = gr.Dropdown(label="月で絞り込む", choices=["すべて"], value="すべて", scale=1, allow_custom_value=True)
                            
                            with gr.Row():
                                with gr.Column(scale=1):
                                    research_entry_dropdown = gr.Dropdown(
                                        label="エントリを選択",
                                        choices=[],
                                        interactive=True,
                                        info="最新のエントリが上に表示されます", allow_custom_value=True)
                                with gr.Column(scale=2):
                                    research_notes_editor = gr.Textbox(
                                        label="エントリの内容",
                                        interactive=True,
                                        elem_id="research_notes_editor_code",
                                        lines=15,
                                        max_lines=20,
                                        placeholder="エントリを選択するか、「RAW編集」で直接編集してください"
                                    )
                            
                            with gr.Row():
                                save_research_notes_button = gr.Button("選択エントリを保存", variant="secondary")
                                reload_research_notes_button = gr.Button("再読込", variant="secondary")
                            
                            with gr.Accordion("📝 RAW編集（全文）", open=False):
                                research_notes_raw_editor = gr.Textbox(
                                    label="research_notes.md 全文",
                                    interactive=True,
                                    lines=15,
                                    max_lines=25,
                                    autoscroll=True,
                                    elem_id="research_notes_raw_editor",
                                    placeholder="ファイル全体を直接編集できます"
                                )
                                with gr.Row():
                                    save_research_raw_button = gr.Button("RAW全文を保存", variant="primary")
                                    reload_research_raw_button = gr.Button("RAW再読込", variant="secondary")

                    # ▼▼▼【ここから下のブロックを「メモ帳」タブの直後に追加】▼▼▼
                    with gr.TabItem("知識") as knowledge_tab:
                        gr.Markdown("## 知識ベース (RAG)\nこのルームのAIが参照する知識ドキュメントを管理します。")

                        knowledge_file_df = gr.DataFrame(
                            headers=["ファイル名", "サイズ (KB)", "最終更新日時"],
                            datatype=["str", "str", "str"],
                            row_count=(5, "dynamic"),
                            col_count=(3, "fixed"),
                            interactive=True # 行を選択可能にする
                        )

                        with gr.Row():
                            knowledge_upload_button = gr.UploadButton(
                                "ファイルをアップロード",
                                file_types=[".txt", ".md"],
                                file_count="multiple"
                            )
                            knowledge_delete_button = gr.Button("選択したファイルを削除", variant="stop")

                        gr.Markdown("---")
                        knowledge_reindex_button = gr.Button("索引を作成 / 更新", variant="primary")
                        knowledge_status_output = gr.Textbox(label="ステータス", interactive=False)
                    # ▲▲▲【追加はここまで】▲▲▲


            with gr.TabItem("ワールド・ビルダー") as world_builder_tab:
                gr.Markdown("## ワールド・ビルダー\n`world_settings.txt` の内容を、直感的に、または直接的に編集・確認できます。")

                with gr.Tabs():
                    with gr.TabItem("構造化エディタ"):
                        gr.Markdown("エリアと場所を選択して、その内容をピンポイントで編集します。")
                        with gr.Row(equal_height=False):
                            with gr.Column(scale=1, min_width=250):
                                gr.Markdown("### 1. 編集対象を選択")
                                area_selector = gr.Dropdown(label="エリア (`##`)", interactive=True, allow_custom_value=True)
                                place_selector = gr.Dropdown(label="場所 (`###`)", interactive=True, allow_custom_value=True)
                                gr.Markdown("---")
                                add_area_button = gr.Button("エリアを新規作成")
                                add_place_button = gr.Button("場所を新規作成")
                                with gr.Column(visible=False) as new_item_form:
                                    new_item_form_title = gr.Markdown("#### 新規作成")
                                    new_item_type = gr.Textbox(visible=False)
                                    new_item_name = gr.Textbox(label="エリア名 / 場所名 (必須)", placeholder="例: メインエントランス")
                                    with gr.Row():
                                        confirm_add_button = gr.Button("決定", variant="primary")
                                        cancel_add_button = gr.Button("キャンセル")
                            with gr.Column(scale=3):
                                gr.Markdown("### 2. 内容を編集")
                                content_editor = gr.Textbox(label="世界設定を記述", lines=20, interactive=True, visible=False)
                                with gr.Row(visible=False) as save_button_row:
                                    save_button = gr.Button("この場所の設定を保存", variant="primary")
                                    delete_place_button = gr.Button("この場所を削除", variant="stop")

                    with gr.TabItem("RAWテキストエディタ"):
                        gr.Markdown("世界設定ファイル (`world_settings.txt`) の全体像を直接編集します。**書式（`##`や`###`）を崩さないようご注意ください。**")
                        world_settings_raw_editor = gr.Code( # 変数名を _raw_display から _raw_editor に変更
                            label="world_settings.txt",
                            language="markdown",
                            interactive=True, # 編集可能に
                            lines=25
                        )
                        with gr.Row():
                            save_raw_button = gr.Button("RAWテキスト全体を保存", variant="primary")
                            reload_raw_button = gr.Button("最後に保存した内容を読み込む", variant="secondary")

            # ===== 💼 お出かけタブ =====
            # ===== お出かけタブ =====
            with gr.TabItem("お出かけ", elem_id="outing_tab"):
                with gr.Tabs():
                    # --- Tab 1: エクスポート ---
                    with gr.TabItem("📤 エクスポート", elem_id="outing_export_tab"):
                        gr.Markdown("## ペルソナエクスポート\n外部AIツール（Antigravity等）で会話するためのペルソナデータをエクスポートします。")
                        
                        # --- データ読み込みボタン ---
                        with gr.Row():
                            outing_load_button = gr.Button("📥 データ読み込み", variant="primary", scale=1)
                            outing_total_char_count = gr.Markdown("📝 合計文字数: ---")
                        
                        # --- セクション別アコーディオン ---
                        # システムプロンプト
                        with gr.Accordion("📜 システムプロンプト", open=False):
                            outing_system_prompt_text = gr.Textbox(
                                label="システムプロンプト", lines=8, max_lines=20, interactive=True,
                                placeholder="「データ読み込み」でロードされます"
                            )
                            with gr.Row():
                                outing_system_prompt_chars = gr.Markdown("文字数: ---")
                                outing_system_prompt_reload = gr.Button("🔄", variant="secondary", scale=0, min_width=40)
                                outing_system_prompt_compress = gr.Button("✨ 圧縮", variant="secondary", scale=0)
                                outing_system_prompt_enabled = gr.Checkbox(label="エクスポートに含める", value=True)
                        
                        # コアメモリ（永続記憶）
                        with gr.Accordion("🧠 コアメモリ（永続記憶）", open=False):
                            outing_permanent_text = gr.Textbox(
                                label="永続記憶", lines=8, max_lines=20, interactive=True,
                                placeholder="「データ読み込み」でロードされます"
                            )
                            with gr.Row():
                                outing_permanent_chars = gr.Markdown("文字数: ---")
                                outing_permanent_reload = gr.Button("🔄", variant="secondary", scale=0, min_width=40)
                                outing_permanent_compress = gr.Button("✨ 圧縮", variant="secondary", scale=0)
                                outing_permanent_enabled = gr.Checkbox(label="エクスポートに含める", value=True)
                        
                        # コアメモリ（日記要約）
                        with gr.Accordion("📔 コアメモリ（日記要約）", open=False):
                            outing_diary_text = gr.Textbox(
                                label="日記要約", lines=8, max_lines=20, interactive=True,
                                placeholder="「データ読み込み」でロードされます"
                            )
                            with gr.Row():
                                outing_diary_chars = gr.Markdown("文字数: ---")
                                outing_diary_reload = gr.Button("🔄", variant="secondary", scale=0, min_width=40)
                                outing_diary_compress = gr.Button("✨ 圧縮", variant="secondary", scale=0)
                                outing_diary_enabled = gr.Checkbox(label="エクスポートに含める", value=True)
                        
                        # エピソード記憶
                        with gr.Accordion("📖 エピソード記憶", open=False):
                            outing_episode_days_slider = gr.Slider(
                                minimum=0, maximum=30, value=7, step=1,
                                label="過去N日分", info="0で無効"
                            )
                            outing_episodic_text = gr.Textbox(
                                label="エピソード記憶", lines=8, max_lines=20, interactive=True,
                                placeholder="「データ読み込み」でロードされます"
                            )
                            with gr.Row():
                                outing_episodic_chars = gr.Markdown("文字数: ---")
                                outing_episodic_reload = gr.Button("🔄", variant="secondary", scale=0, min_width=40)
                                outing_episodic_compress = gr.Button("✨ 圧縮", variant="secondary", scale=0)
                                outing_episodic_enabled = gr.Checkbox(label="エクスポートに含める", value=True)
                        
                        # 会話ログ
                        with gr.Accordion("💬 会話ログ", open=False):
                            with gr.Row():
                                outing_log_count_slider = gr.Slider(
                                    minimum=5, maximum=50, value=20, step=5,
                                    label="最新N件", scale=2
                                )
                                outing_logs_include_timestamp = gr.Checkbox(label="タイムスタンプを含む", value=False, scale=1)
                                outing_logs_include_model = gr.Checkbox(label="モデル名を含む", value=False, scale=1)
                            outing_logs_text = gr.Textbox(
                                label="会話ログ", lines=8, max_lines=20, interactive=True,
                                placeholder="「データ読み込み」でロードされます"
                            )
                            with gr.Row():
                                outing_logs_chars = gr.Markdown("文字数: ---")
                                outing_logs_reload = gr.Button("🔄", variant="secondary", scale=0, min_width=40)
                                outing_logs_compress = gr.Button("✨ 圧縮", variant="secondary", scale=0)
                                outing_logs_enabled = gr.Checkbox(label="エクスポートに含める", value=True)
                                outing_logs_wrap_tags = gr.Checkbox(label="過去ログをタグで囲む（帰宅時の重複除去用）", value=True)
                        
                        # --- エクスポート実行 ---
                        gr.Markdown("---")
                        with gr.Row():
                            outing_export_button = gr.Button("📤 エクスポート", variant="primary", scale=2)
                            outing_open_folder_button = gr.Button("📂 フォルダを開く", variant="secondary", scale=1)
                        outing_download_file = gr.File(label="ダウンロード", visible=False, elem_id="outing_download_file")
                    
                    # --- Tab 2: 帰宅（インポート） ---
                    with gr.TabItem("🏠 帰宅 (インポート)", elem_id="outing_import_tab"):
                        gr.Markdown("## 会話ログの統合\nAntigravity等からエクスポートした会話ログを、現在のルームの履歴に統合（追記）します。")
                        
                        # ファイル取り込み
                        with gr.Group():
                            gr.Markdown("### 📂 ファイルから取り込み")
                            outing_import_file = gr.File(label="ログファイルをアップロード（MD/TXT）", file_types=[".md", ".txt"])
                            with gr.Row():
                                outing_import_source = gr.Textbox(label="お出かけ先の名称", value="Antigravity", placeholder="例: Antigravity, 外出先")
                                outing_import_user_header = gr.Textbox(label="ユーザーの発言ヘッダー", value="[user]", placeholder="例: [user]")
                                outing_import_agent_header = gr.Textbox(label="AIの発言ヘッダー", value="[ルシアン]", placeholder="例: [ルシアン]")
                            outing_import_button = gr.Button("ログを統合して帰宅する (ファイル)", variant="primary")
                        
                        # URL取り込み (Gemini)
                        with gr.Group():
                            gr.Markdown("### ♊ Gemini共有URLから取り込み")
                            gr.Markdown("共有リンクから会話内容を直接読み込みます。")
                            gemini_import_url = gr.Textbox(label="共有URL", placeholder="https://gemini.google.com/share/...", lines=1)
                            gemini_import_button = gr.Button("ログを統合して帰宅する (URL)", variant="primary")
                            gemini_import_status = gr.Markdown("")

                        outing_import_status = gr.Markdown("ステータス: 待機中")

            with gr.TabItem("デバッグコンソール"):
                gr.Markdown("## デバッグコンソール\nアプリケーションの内部的な動作ログ（ターミナルに出力される内容）をここに表示します。")
                debug_console_output = gr.Textbox(
                    label="コンソール出力",
                    lines=30,
                    interactive=False,
                    autoscroll=True
                )
                clear_debug_console_button = gr.Button("コンソールをクリア", variant="secondary")

        # --- イベントハンドラ定義 ---
        context_checkboxes = [
            room_display_thoughts_checkbox,
            room_send_thoughts_checkbox, 
            room_enable_retrieval_checkbox,
            room_add_timestamp_checkbox,
            room_send_current_time_checkbox,
            room_send_notepad_checkbox,
            room_use_common_prompt_checkbox,
            room_send_core_memory_checkbox,
            enable_scenery_system_checkbox,
            auto_memory_enabled_checkbox,
            room_auto_summary_checkbox,
            room_enable_self_awareness_checkbox,
        ]
        
        context_token_calc_inputs = [
            current_room_name, current_api_key_name_state, api_history_limit_state,
            room_episode_memory_days_dropdown,
            chat_input_multimodal
        ] + context_checkboxes + [
            room_auto_summary_threshold_slider
        ]

        attachment_change_token_calc_inputs = context_token_calc_inputs

        initial_load_chat_outputs = [
            current_room_name, chatbot_display, current_log_map_state,
            chat_input_multimodal,
            profile_image_display,
            memory_txt_editor, notepad_editor, creative_notes_editor, research_notes_editor, system_prompt_editor,
            core_memory_editor,
            room_dropdown,
            alarm_room_dropdown, timer_room_dropdown, manage_room_selector,
            location_dropdown,
            current_scenery_display, room_voice_dropdown,
            room_voice_style_prompt_textbox,
            enable_typewriter_effect_checkbox,
            streaming_speed_slider,
            room_temperature_slider, room_top_p_slider,
            room_safety_harassment_dropdown, room_safety_hate_speech_dropdown,
            room_safety_sexually_explicit_dropdown, room_safety_dangerous_content_dropdown,
            room_display_thoughts_checkbox,
            room_send_thoughts_checkbox, 
            room_enable_retrieval_checkbox, 
            room_add_timestamp_checkbox,
            room_send_current_time_checkbox,
            room_send_notepad_checkbox,
            room_use_common_prompt_checkbox,
            room_send_core_memory_checkbox,
            room_send_scenery_checkbox,
            room_scenery_send_mode_dropdown,
            auto_memory_enabled_checkbox,
            room_enable_self_awareness_checkbox,
            room_settings_info,
            scenery_image_display,
            enable_scenery_system_checkbox,
            profile_scenery_accordion,
            room_api_history_limit_dropdown,
            room_thinking_level_dropdown,
            api_history_limit_state,
            room_episode_memory_days_dropdown,
            episodic_memory_info_display,
            room_enable_autonomous_checkbox,
            room_autonomous_inactivity_slider,
            room_quiet_hours_start,
            room_quiet_hours_end,
            room_model_dropdown,  # [追加] ルーム個別モデル設定 (Dropdown)
            # [Phase 3] 個別プロバイダ設定
            room_provider_radio,
            room_google_settings_group,
            room_openai_settings_group,
            room_api_key_dropdown,
            room_openai_profile_dropdown,  # 追加: プロファイル選択
            room_openai_base_url_input,
            room_openai_api_key_input,
            room_openai_model_dropdown,
            room_openai_tool_use_checkbox,  # 追加: ツール使用オンオフ
            room_rotation_dropdown, # [Phase 1.5]
            # --- 睡眠時記憶整理 ---
            sleep_consolidation_episodic_cb,
            sleep_consolidation_memory_index_cb,
            sleep_consolidation_current_log_cb,
            sleep_consolidation_entity_memory_cb,
            sleep_consolidation_compress_cb,
            compress_episodes_status,
            # --- [v25] テーマ設定 ---
            room_theme_enabled_checkbox,  # 個別テーマのオンオフ
            chat_style_radio,
            font_size_slider,
            line_height_slider,
            theme_primary_picker,
            theme_secondary_picker,
            theme_background_picker,
            theme_text_picker,
            theme_accent_soft_picker,
            # --- 詳細設定 ---
            theme_input_bg_picker,
            theme_input_border_picker,
            theme_code_bg_picker,
            theme_subdued_text_picker,
            theme_button_bg_picker,
            theme_button_hover_picker,
            theme_stop_button_bg_picker,
            theme_stop_button_hover_picker,
            theme_checkbox_off_picker,
            theme_table_bg_picker,
            theme_radio_label_picker,
            theme_dropdown_list_bg_picker,
            theme_ui_opacity_slider,
            # 背景画像設定
            theme_bg_image_picker,
            theme_bg_opacity_slider,
            theme_bg_blur_slider,
            theme_bg_size_dropdown,
            theme_bg_position_dropdown,
            theme_bg_repeat_dropdown,
            theme_bg_custom_width,
            theme_bg_radius_slider,
            theme_bg_mask_blur_slider,
            theme_bg_overlay_checkbox,
            theme_bg_src_mode,
            # Sync設定
            theme_bg_sync_opacity_slider,
            theme_bg_sync_blur_slider,
            theme_bg_sync_size_dropdown,
            theme_bg_sync_position_dropdown,
            theme_bg_sync_repeat_dropdown,
            theme_bg_sync_custom_width,
            theme_bg_sync_radius_slider,
            theme_bg_sync_mask_blur_slider,
            theme_bg_sync_overlay_checkbox,
            # ---
            save_room_theme_button,
            style_injector,
            # --- [Phase 11/12] 夢日記対応 ---
            dream_date_dropdown,
            dream_detail_text,
            dream_year_filter,
            dream_month_filter,
            # --- [Phase 14] エピソード記憶閲覧 ---
            episodic_date_dropdown,
            episodic_detail_text,
            episodic_year_filter,
            episodic_month_filter,
            episodic_update_status, # [Phase 14 追加] エピソード更新ステータス
            entity_dropdown,
            entity_content_editor,
            embedding_provider_radio, # [Phase 16 → 統合] エンベディングプロバイダ同期用
            dream_status_display,  # [Phase 17 追加] 睡眠時記憶整理ステータス
            room_auto_summary_checkbox,
            room_auto_summary_threshold_slider,
            room_project_root_input,
            room_project_exclude_dirs_input,
            room_project_exclude_files_input,
            expressions_html,
            expression_target_dropdown,
            creative_notes_file_dropdown,
            research_notes_file_dropdown
        ]

        initial_load_outputs = [
            alarm_dataframe, alarm_dataframe_original_data, selection_feedback_markdown
        ] + initial_load_chat_outputs + [
            redaction_rules_df, token_count_display, api_key_dropdown,
            world_data_state,
            time_mode_radio,
            fixed_season_dropdown,
            fixed_time_of_day_dropdown,
            fixed_time_controls,
            onboarding_guide,
            onboarding_group,  # オンボーディングモーダルを動的に制御
            model_dropdown,
            debug_mode_checkbox,
            notification_service_radio,
            backup_rotation_count_number,
            pushover_user_key_input,
            pushover_app_token_input,
            discord_webhook_input,
            image_gen_provider_radio,
            gemini_image_model_dropdown,
            openai_image_model_dropdown,
            paid_keys_checkbox_group,
            allow_external_connection_checkbox,
            custom_scenery_location_dropdown,
            custom_scenery_time_dropdown,
            # --- [追加] OpenAI設定UIへの反映 ---
            openai_profile_dropdown,
            openai_base_url_input,
            openai_api_key_input,
            openai_model_dropdown,
            openai_tool_use_checkbox,
            # --- 索引ステータス欄（最終更新日時表示用）---
            memory_reindex_status,
            current_log_reindex_status,
            # --- [Phase 3] 内部モデル設定（混合編成対応） ---
            internal_processing_provider,
            internal_processing_model,
            internal_summarization_provider,
            internal_summarization_model,
            embedding_model_dropdown, # エンベディングモデルをここに追加
            internal_fallback_checkbox, # [Phase 4]
            groq_api_key_input, # [Phase 3b]
            local_model_path_input, # [Phase 3c]
            tavily_api_key_input, # [Phase 3]
            settings_rotation_checkbox, # [Phase 1.5]
        ]

        world_builder_outputs = [world_data_state, area_selector, world_settings_raw_editor, place_selector]
        session_management_outputs = [active_participants_state, session_status_display, participant_checkbox_group]

        # 【v5: 司令塔契約統一版】
        # ルームの変更や削除時に、UI全体をリフレッシュする全てのコンポーネントをここに集約する
        unified_full_room_refresh_outputs = initial_load_chat_outputs + world_builder_outputs + session_management_outputs + [
            redaction_rules_df,
            archive_date_dropdown,
            time_mode_radio,
            fixed_season_dropdown,
            fixed_time_of_day_dropdown,
            fixed_time_controls,
            attachments_df,
            active_attachments_display,
            custom_scenery_location_dropdown,
            # 司令塔間で戻り値の数を統一するための追加コンポーネント
            token_count_display,
            room_delete_confirmed_state, # handle_delete_room が返すリセット値用
            memory_reindex_status,
            current_log_reindex_status
        ]
        full_refresh_output_count = gr.State(len(unified_full_room_refresh_outputs))
        
        full_refresh_output_count = gr.State(len(unified_full_room_refresh_outputs))
        
        # 数が一致することを確認（デバッグ用）
        # print(f"DEBUG: initial_load_outputs len = {len(initial_load_outputs)}")
        initial_load_output_count = gr.State(len(initial_load_outputs))
        demo.load(
            fn=ui_handlers.handle_initial_load,
            inputs=[current_room_name, initial_load_output_count], 
            outputs=initial_load_outputs
        )


        start_session_button.click(
            fn=ui_handlers.handle_start_session,
            inputs=[current_room_name, participant_checkbox_group],
            outputs=[active_participants_state, session_status_display]
        )
        end_session_button.click(
            fn=ui_handlers.handle_end_session,
            inputs=[current_room_name, active_participants_state],
            outputs=[active_participants_state, session_status_display, participant_checkbox_group]
        )
       
        chat_inputs = [
            chat_input_multimodal,
            room_dropdown, # [Fix] StateではなくUIコンポーネントの値を直接使用して混線を防止
            current_api_key_name_state,
            api_history_limit_state,
            debug_mode_checkbox,
            debug_console_state,
            active_participants_state,
            group_hide_thoughts_checkbox,  # グループ会話 思考ログ非表示
            active_attachments_state, 
            model_dropdown,
            enable_typewriter_effect_checkbox,
            streaming_speed_slider,
            current_scenery_display,
            screenshot_mode_checkbox, 
            redaction_rules_state,
            enable_supervisor_cb, # [v18] Supervisorモード    
        ]
    
        rerun_inputs = [
            selected_message_state,
            current_room_name,
            current_api_key_name_state,
            api_history_limit_state,
            debug_mode_checkbox,
            debug_console_state,
            active_participants_state,
            group_hide_thoughts_checkbox,  # グループ会話 思考ログ非表示
            active_attachments_state,
            model_dropdown,
            enable_typewriter_effect_checkbox,
            streaming_speed_slider,
            current_scenery_display,
            screenshot_mode_checkbox, 
            redaction_rules_state,
            enable_supervisor_cb, # [v18] Supervisorモード    
        ]

        # 新規送信と再生成で、UI更新の対象（outputs）を完全に一致させる
        unified_streaming_outputs = [
            chatbot_display, current_log_map_state, chat_input_multimodal,
            token_count_display,
            location_dropdown, 
            current_scenery_display,
            alarm_dataframe_original_data, alarm_dataframe, scenery_image_display,
            debug_console_state, debug_console_output,
            stop_button, chat_reload_button,
            action_button_group,
            profile_image_display, # [v19] Added for Thinking Animation
            style_injector # [v21] Sync Background
        ]

        rerun_event = rerun_button.click(
            fn=lambda: gr.update(active=False),
            outputs=[auto_idle_timer]
        ).then(
            fn=ui_handlers.handle_rerun_button_click,
            inputs=rerun_inputs,
            outputs=unified_streaming_outputs
        ).then(
            fn=lambda: gr.update(active=True),
            outputs=[auto_idle_timer]
        )

        # 【v5: 堅牢化】ルーム変更イベントを2段階に分離
        # 1. まず、選択されたルーム名をconfig.jsonに即時保存するだけの小さな処理を実行
        room_dropdown.change(
            fn=ui_handlers.handle_save_last_room, # <<< lambdaから専用ハンドラに変更
            inputs=[room_dropdown],
            outputs=None
        # 2. その後(.then)、UI全体を更新する重い処理を実行
        ).then(
            fn=ui_handlers.handle_room_change_for_all_tabs,
            inputs=[room_dropdown, api_key_dropdown, full_refresh_output_count],
            outputs=unified_full_room_refresh_outputs
        # 3. [v6] アバターモードラジオを更新
        ).then(
            fn=ui_handlers.get_avatar_mode_for_room,
            inputs=[room_dropdown],
            outputs=[avatar_mode_radio]
        )

        chat_reload_button.click(
            fn=ui_handlers.reload_chat_log,
            inputs=[current_room_name, api_history_limit_state, room_add_timestamp_checkbox, room_display_thoughts_checkbox, screenshot_mode_checkbox, redaction_rules_state],
            outputs=[chatbot_display, current_log_map_state]
        ).then(
            fn=ui_handlers.load_user_memo,
            inputs=[current_room_name],
            outputs=[user_memo_textbox]
        )

        # --- 日記アーカイブ機能のイベント接続 ---

        # 「記憶をアーカイブする」アコーディオンが開かれた時に、日付ドロップダウンを更新
        memory_archive_accordion.expand(
            fn=ui_handlers.handle_archive_memory_tab_select,
            inputs=[current_room_name],
            outputs=[archive_date_dropdown]
        )

        # アーカイブ実行ボタンがクリックされたら、JavaScriptで確認ダイアログを表示し、
        # 結果を非表示のTextbox `archive_confirm_state` に書き込む
        archive_memory_button.click(
            fn=None,
            inputs=None,
            outputs=[archive_confirm_state],
            js="() => confirm('本当によろしいですか？ この操作はmemory_main.txtを直接変更します。')"
        )

        # 非表示Textboxの値が変更されたら（＝ユーザーがダイアログを操作したら）、
        # バックエンドの処理を実行する
        archive_confirm_state.change(
            fn=ui_handlers.handle_archive_memory_click,
            inputs=[archive_confirm_state, current_room_name, api_key_dropdown, archive_date_dropdown],
            outputs=[memory_txt_editor, archive_date_dropdown]
        )
        chatbot_display.select(
            fn=ui_handlers.handle_chatbot_selection,
            inputs=[current_room_name, api_history_limit_state, current_log_map_state],
            outputs=[selected_message_state, action_button_group, play_audio_button],
            show_progress=False
        )
        
        chatbot_display.edit(
            fn=ui_handlers.handle_chatbot_edit,
            inputs=[
                chatbot_display,  
                current_room_name,
                api_history_limit_state,
                current_log_map_state,
                room_add_timestamp_checkbox
            ],
            outputs=[chatbot_display, current_log_map_state]
        )

        delete_selection_button.click(
            fn=None,
            inputs=None,
            outputs=[message_delete_confirmed_state], 
            js="() => confirm('本当にこのメッセージを削除しますか？この操作は元に戻せません。')"
        )
        message_delete_confirmed_state.change( 
            fn=ui_handlers.handle_delete_button_click,
            inputs=[
                message_delete_confirmed_state, 
                selected_message_state, 
                current_room_name, 
                api_history_limit_state,
                room_add_timestamp_checkbox,
                screenshot_mode_checkbox,
                redaction_rules_state,
                room_display_thoughts_checkbox
            ], 
            outputs=[chatbot_display, current_log_map_state, selected_message_state, action_button_group, message_delete_confirmed_state]
        )

        room_api_history_limit_dropdown.change(
            fn=ui_handlers.update_api_history_limit_state_and_reload_chat,
            inputs=[
                room_api_history_limit_dropdown, 
                current_room_name, 
                room_add_timestamp_checkbox, 
                room_display_thoughts_checkbox, 
                screenshot_mode_checkbox, 
                redaction_rules_state
            ],
            outputs=[api_history_limit_state, chatbot_display, current_log_map_state]
        ).then(
            fn=ui_handlers.handle_context_settings_change,
            inputs=context_token_calc_inputs, # ※注意: このリストの中身も更新が必要（後述）
            outputs=token_count_display
        )

        create_room_button.click(
            fn=ui_handlers.handle_create_room,
            inputs=[new_room_name, new_user_display_name, new_agent_display_name, new_room_description, initial_system_prompt],
            outputs=[
                room_dropdown,             # メインルーム選択
                manage_room_selector,      # 管理タブ
                alarm_room_dropdown,       # アラーム
                timer_room_dropdown,       # タイマー
                new_room_name,
                new_user_display_name,
                new_agent_display_name,
                new_room_description,
                initial_system_prompt
            ]
        )

        # 既存のイベントハンドラのoutputsを再利用しやすいように変数に格納
        manage_room_select_outputs = [
            manage_room_details,
            manage_room_name,
            manage_user_display_name,
            manage_agent_display_name,
            manage_room_description,
            manage_folder_name_display
        ]

        # 既存のイベント
        manage_room_selector.select(
            fn=ui_handlers.handle_manage_room_select,
            inputs=[manage_room_selector],
            outputs=manage_room_select_outputs
        )

        # アコーディオンが開かれた時にも同じ関数を呼び出す
        manage_room_accordion.expand(
            fn=ui_handlers.handle_manage_room_select,
            inputs=[manage_room_selector],
            outputs=manage_room_select_outputs
        )

        save_room_config_button.click(
            fn=ui_handlers.handle_save_room_config,
            inputs=[
                manage_folder_name_display,
                manage_room_name,
                manage_user_display_name,
                manage_agent_display_name,
                manage_room_description
            ],
            outputs=[room_dropdown, manage_room_selector]
        )

        delete_room_button.click(
            fn=None,
            inputs=None,
            outputs=[room_delete_confirmed_state],
            js="() => confirm('本当にこのルームを削除しますか？この操作は取り消せません。')"
        )
        room_delete_confirmed_state.change(
            fn=ui_handlers.handle_delete_room,
            inputs=[room_delete_confirmed_state, manage_folder_name_display, api_key_dropdown, current_room_name, full_refresh_output_count],
            outputs=unified_full_room_refresh_outputs
        )

        # --- Screenshot Helper Event Handlers ---
        redaction_rules_df.select(
            fn=ui_handlers.handle_redaction_rule_select,
            inputs=[redaction_rules_df],
            outputs=[selected_redaction_rule_state, redaction_find_textbox, redaction_replace_textbox, redaction_color_picker]
        )
        redaction_color_picker.change(
            fn=lambda color: color,
            inputs=[redaction_color_picker],
            outputs=[redaction_rule_color_state]
        )
        add_rule_button.click(
            fn=ui_handlers.handle_add_or_update_redaction_rule,
            inputs=[redaction_rules_state, selected_redaction_rule_state, redaction_find_textbox, redaction_replace_textbox, redaction_rule_color_state],
            outputs=[redaction_rules_df, redaction_rules_state, selected_redaction_rule_state, redaction_find_textbox, redaction_replace_textbox, redaction_color_picker]
        ).then(
            # メインチャットの更新（reload_chat_log を強制的に呼ぶ必要があるが、現状のロジックでは自動更新されない仕様の可能性がある）
            # ここではプレビューの更新のみを追加する（要望範囲）
            fn=ui_handlers.handle_update_log_preview,
            inputs=[
                current_room_name, 
                chat_log_month_dropdown,
                room_add_timestamp_checkbox,
                room_display_thoughts_checkbox,
                screenshot_mode_checkbox,
                redaction_rules_state
            ],
            outputs=[chat_log_preview_chatbot]
        )
        clear_rule_form_button.click(
            fn=lambda: (None, "", "", "#62827e", "#62827e"),
            outputs=[selected_redaction_rule_state, redaction_find_textbox, redaction_replace_textbox, redaction_color_picker, redaction_rule_color_state]
        )
        delete_rule_button.click(
            fn=ui_handlers.handle_delete_redaction_rule,
            inputs=[redaction_rules_state, selected_redaction_rule_state],
            outputs=[redaction_rules_df, redaction_rules_state, selected_redaction_rule_state, redaction_find_textbox, redaction_replace_textbox, redaction_color_picker]
        ).then(
            fn=ui_handlers.handle_update_log_preview,
            inputs=[
                current_room_name, 
                chat_log_month_dropdown,
                room_add_timestamp_checkbox,
                room_display_thoughts_checkbox,
                screenshot_mode_checkbox,
                redaction_rules_state
            ],
            outputs=[chat_log_preview_chatbot]
        )
        screenshot_mode_checkbox.change(
            fn=ui_handlers.reload_chat_log,
            inputs=[current_room_name, api_history_limit_state, room_add_timestamp_checkbox, room_display_thoughts_checkbox, screenshot_mode_checkbox, redaction_rules_state],
            outputs=[chatbot_display, current_log_map_state]
        ).then(
            fn=ui_handlers.handle_update_log_preview,
            inputs=[
                current_room_name, 
                chat_log_month_dropdown,
                room_add_timestamp_checkbox,
                room_display_thoughts_checkbox,
                screenshot_mode_checkbox,
                redaction_rules_state
            ],
            outputs=[chat_log_preview_chatbot]
        )

        correct_punctuation_button.click(
            fn=None,
            inputs=None,
            outputs=[correction_confirmed_state],
            # 確認ダイアログを表示するJavaScript
            js="() => confirm('選択した行以降のAI応答の読点を修正します。\\nこの操作はログファイルを直接変更し、元に戻せません。\\n（処理前にバックアップが作成されます）\\n\\n本当によろしいですか？')"
        )

        correction_confirmed_state.change(
            fn=ui_handlers.handle_log_punctuation_correction,
            inputs=[correction_confirmed_state, selected_message_state, current_room_name, current_api_key_name_state, api_history_limit_state, room_add_timestamp_checkbox],
            outputs=[chatbot_display, current_log_map_state, correct_punctuation_button, selected_message_state, action_button_group, correction_confirmed_state]
        )
        gen_settings_inputs = [
            room_temperature_slider, room_top_p_slider,
            room_safety_harassment_dropdown, room_safety_hate_speech_dropdown,
            room_safety_sexually_explicit_dropdown, room_safety_dangerous_content_dropdown
        ]
        room_individual_settings_inputs = [
            current_room_name, room_voice_dropdown, room_voice_style_prompt_textbox
        ] + gen_settings_inputs + [
            enable_typewriter_effect_checkbox,
            streaming_speed_slider,
        ] + [
            room_display_thoughts_checkbox,
            room_send_thoughts_checkbox, 
            room_enable_retrieval_checkbox, 
            room_add_timestamp_checkbox, 
            room_send_current_time_checkbox, 
            room_send_notepad_checkbox,
            room_use_common_prompt_checkbox, room_send_core_memory_checkbox,
            room_send_scenery_checkbox,
            room_scenery_send_mode_dropdown,
            enable_scenery_system_checkbox,
            auto_memory_enabled_checkbox,
            room_enable_self_awareness_checkbox,
            room_api_history_limit_dropdown,
            room_thinking_level_dropdown,
            room_episode_memory_days_dropdown,
            room_enable_autonomous_checkbox,
            room_autonomous_inactivity_slider,
            room_quiet_hours_start,
            room_quiet_hours_end,
            room_model_dropdown,
            room_provider_radio,
            room_api_key_dropdown,
            room_openai_profile_dropdown,
            room_openai_base_url_input,
            room_openai_api_key_input,
            room_openai_model_dropdown,
            room_openai_tool_use_checkbox,
            room_rotation_dropdown,
            # --- 睡眠時記憶整理 ---
            sleep_consolidation_episodic_cb,
            sleep_consolidation_memory_index_cb,
            sleep_consolidation_current_log_cb,
            sleep_consolidation_entity_memory_cb,
            sleep_consolidation_compress_cb,
            sleep_consolidation_extract_questions_cb,  # 追加: 未解決の問い抽出
            room_auto_summary_checkbox,
            room_auto_summary_threshold_slider,
            room_project_root_input,
            room_project_exclude_dirs_input,
            room_project_exclude_files_input,
        ]

        # 個別設定の即時保存対応: 各コンポーネントに変更イベントを登録
        # (ただし、current_room_name 自身やボタン自体には不要)
        for comp in room_individual_settings_inputs[1:]:
            comp.change(
                fn=lambda *args: ui_handlers.handle_save_room_settings(*args, silent=False, force_notify=False),
                inputs=room_individual_settings_inputs,
                outputs=None
            )

        preview_event = room_preview_voice_button.click(
            fn=ui_handlers.handle_voice_preview, 
            inputs=[current_room_name, room_voice_dropdown, room_voice_style_prompt_textbox, room_preview_text_textbox, api_key_dropdown], 
            outputs=[audio_player, play_audio_button, room_preview_voice_button]
        )
        preview_event.failure(
            fn=ui_handlers._reset_preview_on_failure, 
            inputs=None, 
            outputs=[audio_player, play_audio_button, room_preview_voice_button]
        )

        # --- [Phase 3] 個別プロバイダ切り替えイベント ---
        room_provider_radio.change(
            fn=lambda provider: (
                gr.update(visible=(provider == "google")),  # room_google_settings_group
                gr.update(visible=(provider == "openai")),  # room_openai_settings_group
            ),
            inputs=[room_provider_radio],
            outputs=[room_google_settings_group, room_openai_settings_group]
        )

        # --- [Phase 3] Google用カスタムモデル追加イベント（永続保存） ---
        room_google_add_model_button.click(
            fn=lambda room, model: ui_handlers.handle_add_room_custom_model(room, model, "google"),
            inputs=[current_room_name, room_google_custom_model_input],
            outputs=[room_model_dropdown, room_google_custom_model_input]
        )

        # --- [Phase 3] 個別プロファイル選択時の自動入力イベント ---
        def _load_room_openai_profile(profile_name):
            """プロファイル選択時に共通設定から設定を読み込んで自動入力"""
            if not profile_name:
                return "", "", gr.update(choices=[], value=None)
            settings_list = config_manager.get_openai_settings_list()
            target = next((s for s in settings_list if s["name"] == profile_name), None)
            if not target:
                return "", "", gr.update(choices=[], value=None)
            available_models = target.get("available_models", [])
            default_model = target.get("default_model", "")
            return (
                target.get("base_url", ""),
                target.get("api_key", ""),
                gr.update(choices=available_models, value=default_model)
            )
        
        room_openai_profile_dropdown.change(
            fn=_load_room_openai_profile,
            inputs=[room_openai_profile_dropdown],
            outputs=[room_openai_base_url_input, room_openai_api_key_input, room_openai_model_dropdown]
        )
        
        # --- [Phase 3] OpenAI互換カスタムモデル追加イベント（永続保存） ---
        room_openai_add_model_button.click(
            fn=lambda room, model: ui_handlers.handle_add_room_custom_model(room, model, "openai"),
            inputs=[current_room_name, room_openai_custom_model_input],
            outputs=[room_openai_model_dropdown, room_openai_custom_model_input]
        )

        # [v25] Theme & Display Handlers
        theme_preview_inputs = [
            room_theme_enabled_checkbox,  # 個別テーマのオンオフ
            font_size_slider, line_height_slider, chat_style_radio,
            # 基本配色
            theme_primary_picker, theme_secondary_picker, theme_background_picker, theme_text_picker, theme_accent_soft_picker,
            # 詳細設定
            theme_input_bg_picker, theme_input_border_picker, theme_code_bg_picker, theme_subdued_text_picker,
            theme_button_bg_picker, theme_button_hover_picker, theme_stop_button_bg_picker, theme_stop_button_hover_picker,
            theme_checkbox_off_picker, theme_table_bg_picker, theme_radio_label_picker, theme_dropdown_list_bg_picker,
            theme_ui_opacity_slider,
            # 背景画像設定
            theme_bg_image_picker, theme_bg_opacity_slider, theme_bg_blur_slider,
            theme_bg_size_dropdown, theme_bg_position_dropdown, theme_bg_repeat_dropdown,
            theme_bg_custom_width, theme_bg_radius_slider, theme_bg_mask_blur_slider,
            theme_bg_overlay_checkbox,
            theme_bg_src_mode,
            # Sync設定 (追加)
            theme_bg_sync_opacity_slider, theme_bg_sync_blur_slider,
            theme_bg_sync_size_dropdown, theme_bg_sync_position_dropdown, theme_bg_sync_repeat_dropdown,
            theme_bg_sync_custom_width, theme_bg_sync_radius_slider, theme_bg_sync_mask_blur_slider,
            theme_bg_sync_overlay_checkbox
        ]
        
        for comp in theme_preview_inputs:
            comp.change(
                fn=ui_handlers.handle_theme_preview,
                inputs=[current_room_name] + theme_preview_inputs,
                outputs=[style_injector]
            )

        save_room_theme_button.click(
            fn=lambda *args: ui_handlers.handle_save_theme_settings(*args, force_notify=True),
            inputs=[room_dropdown] + theme_preview_inputs,
            outputs=None
        )

        # ▼▼▼【ここからが新しいイベント定義です】▼▼▼
        # 思考表示チェックボックスの変更イベント
        room_display_thoughts_checkbox.change(
            fn=lambda is_checked: gr.update(interactive=is_checked) if is_checked else gr.update(interactive=False, value=False),
            inputs=[room_display_thoughts_checkbox],
            outputs=[room_send_thoughts_checkbox]
        ).then(
            fn=ui_handlers.handle_context_settings_change,
            inputs=context_token_calc_inputs,
            outputs=token_count_display
        )
        
        other_context_checkboxes = [
            room_send_thoughts_checkbox, 
            room_enable_retrieval_checkbox, 
            room_add_timestamp_checkbox, 
            room_send_current_time_checkbox,
            room_send_notepad_checkbox, room_use_common_prompt_checkbox, room_send_core_memory_checkbox, 
            enable_scenery_system_checkbox, auto_memory_enabled_checkbox, room_enable_self_awareness_checkbox
        ]
        for checkbox in other_context_checkboxes:
             checkbox.change(fn=ui_handlers.handle_context_settings_change, inputs=context_token_calc_inputs, outputs=token_count_display)

        # 自動要約設定のイベント
        room_auto_summary_checkbox.change(
            fn=lambda is_checked: gr.update(visible=is_checked),
            inputs=[room_auto_summary_checkbox],
            outputs=[room_auto_summary_threshold_slider]
        ).then(
            fn=ui_handlers.handle_context_settings_change,
            inputs=context_token_calc_inputs,
            outputs=token_count_display
        ).then(
            fn=lambda *args: ui_handlers.handle_save_room_settings(*args, silent=False, force_notify=False),
            inputs=room_individual_settings_inputs,
            outputs=None
        )
        room_auto_summary_threshold_slider.change(
            fn=ui_handlers.handle_context_settings_change,
            inputs=context_token_calc_inputs,
            outputs=token_count_display
        ).then(
            fn=lambda *args: ui_handlers.handle_save_room_settings(*args, silent=False, force_notify=False),
            inputs=room_individual_settings_inputs,
            outputs=None
        )

        # 履歴制限・エピソード記憶期間の変更イベント
        room_api_history_limit_dropdown.change(
            fn=ui_handlers.handle_context_settings_change,
            inputs=context_token_calc_inputs,
            outputs=token_count_display
        )
        room_episode_memory_days_dropdown.change(
            fn=ui_handlers.handle_context_settings_change,
            inputs=context_token_calc_inputs,
            outputs=token_count_display
        )

        # model_dropdownのイベント
        model_dropdown.change(fn=ui_handlers.update_model_state, inputs=[model_dropdown], outputs=[current_model_name]).then(fn=ui_handlers.handle_context_settings_change, inputs=context_token_calc_inputs, outputs=token_count_display)
        
        api_key_dropdown.change(
            fn=ui_handlers.update_api_key_state,
            inputs=[api_key_dropdown],
            outputs=[current_api_key_name_state],
        ).then(
            fn=ui_handlers.handle_context_settings_change,
            inputs=context_token_calc_inputs,
            outputs=token_count_display
        )
        api_test_button.click(fn=ui_handlers.handle_api_connection_test, inputs=[api_key_dropdown], outputs=None)
        # chat_submit_outputs の定義を削除し、代わりに unified_streaming_outputs を使用
        submit_event = chat_input_multimodal.submit(
            fn=lambda: gr.update(active=False),
            outputs=[auto_idle_timer]
        ).then(
            fn=ui_handlers.handle_message_submission,
            inputs=chat_inputs,
            outputs=unified_streaming_outputs # ここを変更
        ).then(
            fn=lambda: gr.update(active=True),
            outputs=[auto_idle_timer]
        )

        stop_button.click(
            fn=ui_handlers.handle_stop_button_click,
            inputs=[current_room_name, api_history_limit_state, room_add_timestamp_checkbox, room_display_thoughts_checkbox, screenshot_mode_checkbox, redaction_rules_state],
            outputs=[stop_button, chat_reload_button, chatbot_display, current_log_map_state],
            cancels=[submit_event, rerun_event]
        )

        # トークン計算イベント（入力内容が変更されるたびに実行）
        token_calc_on_input_inputs = context_token_calc_inputs
        chat_input_multimodal.change(
            fn=ui_handlers.update_token_count_on_input,
            inputs=token_calc_on_input_inputs,
            outputs=token_count_display,
            show_progress=False
        )

        refresh_scenery_button.click(fn=ui_handlers.handle_scenery_refresh, inputs=[current_room_name, api_key_dropdown], outputs=[location_dropdown, current_scenery_display, scenery_image_display, custom_scenery_location_dropdown, style_injector])
        location_dropdown.change(
            fn=ui_handlers.handle_location_change,
            inputs=[current_room_name, location_dropdown, api_key_dropdown],
            outputs=[location_dropdown, current_scenery_display, scenery_image_display, custom_scenery_location_dropdown, style_injector]
        )
        cancel_selection_button.click(fn=lambda: (None, gr.update(visible=False)), inputs=None, outputs=[selected_message_state, action_button_group])

        save_prompt_button.click(fn=ui_handlers.handle_save_system_prompt, inputs=[current_room_name, system_prompt_editor], outputs=None)
        reload_prompt_button.click(fn=ui_handlers.handle_reload_system_prompt, inputs=[current_room_name], outputs=[system_prompt_editor])
        # --- 主観的記憶（日記）のイベントハンドラ ---
        # エントリ読み込み → 年・月フィルタと日付リストを更新
        refresh_diary_button.click(
            fn=ui_handlers.handle_load_diary_entries,
            inputs=[current_room_name],
            outputs=[diary_year_filter, diary_month_filter, diary_entry_dropdown, diary_raw_editor]
        )
        # 最新を表示ボタン
        show_latest_diary_button.click(
            fn=ui_handlers.handle_show_latest_diary,
            inputs=[current_room_name],
            outputs=[diary_year_filter, diary_month_filter, diary_entry_dropdown, memory_txt_editor, diary_raw_editor]
        )
        # フィルタ変更時 → ドロップダウン選択肢を更新
        diary_year_filter.change(
            fn=ui_handlers.handle_diary_filter_change,
            inputs=[current_room_name, diary_year_filter, diary_month_filter],
            outputs=[diary_entry_dropdown]
        )
        diary_month_filter.change(
            fn=ui_handlers.handle_diary_filter_change,
            inputs=[current_room_name, diary_year_filter, diary_month_filter],
            outputs=[diary_entry_dropdown]
        )
        # エントリ選択時 → 詳細表示
        diary_entry_dropdown.change(
            fn=ui_handlers.handle_diary_selection,
            inputs=[current_room_name, diary_entry_dropdown],
            outputs=[memory_txt_editor]
        )
        # 保存・再読込
        save_memory_button.click(fn=ui_handlers.handle_save_diary_entry, inputs=[current_room_name, diary_entry_dropdown, memory_txt_editor], outputs=[memory_txt_editor])
        reload_memory_button.click(fn=ui_handlers.handle_diary_selection, inputs=[current_room_name, diary_entry_dropdown], outputs=[memory_txt_editor])
        # RAW編集
        save_diary_raw_button.click(fn=ui_handlers.handle_save_memory_click, inputs=[current_room_name, diary_raw_editor], outputs=[diary_raw_editor])
        reload_diary_raw_button.click(fn=ui_handlers.handle_reload_memory_raw, inputs=[current_room_name], outputs=[diary_raw_editor, archive_date_dropdown])
        save_notepad_button.click(fn=ui_handlers.handle_save_notepad_click, inputs=[current_room_name, notepad_editor], outputs=[notepad_editor])
        reload_notepad_button.click(fn=ui_handlers.handle_reload_notepad, inputs=[current_room_name], outputs=[notepad_editor])
        clear_notepad_button.click(fn=ui_handlers.handle_clear_notepad_click, inputs=[current_room_name], outputs=[notepad_editor])
        # --- 創作ノートのイベントハンドラ ---
        # ファイルリスト更新
        refresh_creative_file_list_button.click(
            fn=lambda r: ui_handlers.handle_note_file_list_refresh(r, "creative"),
            inputs=[current_room_name],
            outputs=[creative_notes_file_dropdown]
        )
        # ファイル選択変更時
        creative_notes_file_dropdown.change(
            fn=ui_handlers.handle_load_creative_entries,
            inputs=[current_room_name, creative_notes_file_dropdown],
            outputs=[creative_year_filter, creative_month_filter, creative_entry_dropdown, creative_notes_raw_editor]
        )
        # エントリ読み込み → 年・月フィルタと日付リストを更新
        refresh_creative_notes_button.click(
            fn=ui_handlers.handle_load_creative_entries,
            inputs=[current_room_name, creative_notes_file_dropdown],
            outputs=[creative_year_filter, creative_month_filter, creative_entry_dropdown, creative_notes_raw_editor]
        )
        # 最新を表示ボタン
        show_latest_creative_button.click(
            fn=ui_handlers.handle_show_latest_creative,
            inputs=[current_room_name, creative_notes_file_dropdown],
            outputs=[creative_year_filter, creative_month_filter, creative_entry_dropdown, creative_notes_editor, creative_notes_raw_editor]
        )
        # フィルタ変更時 → ドロップダウン選択肢を更新
        creative_year_filter.change(
            fn=ui_handlers.handle_creative_filter_change,
            inputs=[current_room_name, creative_year_filter, creative_month_filter, creative_notes_file_dropdown],
            outputs=[creative_entry_dropdown]
        )
        creative_month_filter.change(
            fn=ui_handlers.handle_creative_filter_change,
            inputs=[current_room_name, creative_year_filter, creative_month_filter, creative_notes_file_dropdown],
            outputs=[creative_entry_dropdown]
        )
        # エントリ選択時 → 詳細表示
        creative_entry_dropdown.change(
            fn=ui_handlers.handle_creative_selection,
            inputs=[current_room_name, creative_entry_dropdown, creative_notes_file_dropdown],
            outputs=[creative_notes_editor]
        )
        # 保存・再読込
        save_creative_notes_button.click(fn=ui_handlers.handle_save_creative_entry, inputs=[current_room_name, creative_entry_dropdown, creative_notes_editor, creative_notes_file_dropdown], outputs=[creative_notes_editor])
        reload_creative_notes_button.click(fn=ui_handlers.handle_creative_selection, inputs=[current_room_name, creative_entry_dropdown, creative_notes_file_dropdown], outputs=[creative_notes_editor])
        # RAW編集
        save_creative_raw_button.click(fn=ui_handlers.handle_save_creative_notes, inputs=[current_room_name, creative_notes_raw_editor, creative_notes_file_dropdown], outputs=[creative_notes_raw_editor])
        reload_creative_raw_button.click(fn=ui_handlers.handle_reload_creative_notes, inputs=[current_room_name, creative_notes_file_dropdown], outputs=[creative_notes_raw_editor])
        
        # --- 研究・分析ノートのイベントハンドラ ---
        # ファイルリスト更新
        refresh_research_file_list_button.click(
            fn=lambda r: ui_handlers.handle_note_file_list_refresh(r, "research"),
            inputs=[current_room_name],
            outputs=[research_notes_file_dropdown]
        )
        # ファイル選択変更時
        research_notes_file_dropdown.change(
            fn=ui_handlers.handle_load_research_entries,
            inputs=[current_room_name, research_notes_file_dropdown],
            outputs=[research_year_filter, research_month_filter, research_entry_dropdown, research_notes_raw_editor]
        )
        refresh_research_notes_button.click(
            fn=ui_handlers.handle_load_research_entries,
            inputs=[current_room_name, research_notes_file_dropdown],
            outputs=[research_year_filter, research_month_filter, research_entry_dropdown, research_notes_raw_editor]
        )
        # 最新を表示ボタン
        show_latest_research_button.click(
            fn=ui_handlers.handle_show_latest_research,
            inputs=[current_room_name, research_notes_file_dropdown],
            outputs=[research_year_filter, research_month_filter, research_entry_dropdown, research_notes_editor, research_notes_raw_editor]
        )
        research_year_filter.change(
            fn=ui_handlers.handle_research_filter_change,
            inputs=[current_room_name, research_year_filter, research_month_filter, research_notes_file_dropdown],
            outputs=[research_entry_dropdown]
        )
        research_month_filter.change(
            fn=ui_handlers.handle_research_filter_change,
            inputs=[current_room_name, research_year_filter, research_month_filter, research_notes_file_dropdown],
            outputs=[research_entry_dropdown]
        )
        research_entry_dropdown.change(
            fn=ui_handlers.handle_research_selection,
            inputs=[current_room_name, research_entry_dropdown, research_notes_file_dropdown],
            outputs=[research_notes_editor]
        )
        save_research_notes_button.click(fn=ui_handlers.handle_save_research_entry, inputs=[current_room_name, research_entry_dropdown, research_notes_editor, research_notes_file_dropdown], outputs=[research_notes_editor])
        reload_research_notes_button.click(fn=ui_handlers.handle_research_selection, inputs=[current_room_name, research_entry_dropdown, research_notes_file_dropdown], outputs=[research_notes_editor])
        save_research_raw_button.click(fn=ui_handlers.handle_save_research_notes, inputs=[current_room_name, research_notes_raw_editor, research_notes_file_dropdown], outputs=[research_notes_raw_editor])
        reload_research_raw_button.click(fn=ui_handlers.handle_reload_research_notes, inputs=[current_room_name, research_notes_file_dropdown], outputs=[research_notes_raw_editor])
        alarm_dataframe.select(
            fn=ui_handlers.handle_alarm_selection_for_all_updates,
            inputs=[alarm_dataframe_original_data],
            outputs=[
                selected_alarm_ids_state, selection_feedback_markdown,
                alarm_add_button, alarm_context_input, alarm_room_dropdown,
                alarm_days_checkboxgroup, alarm_emergency_checkbox,
                alarm_hour_dropdown, alarm_minute_dropdown,
                editing_alarm_id_state, cancel_edit_button
            ],
            show_progress=False
        )
        enable_button.click(fn=lambda ids: ui_handlers.toggle_selected_alarms_status(ids, True), inputs=[selected_alarm_ids_state], outputs=[alarm_dataframe_original_data, alarm_dataframe])
        disable_button.click(fn=lambda ids: ui_handlers.toggle_selected_alarms_status(ids, False), inputs=[selected_alarm_ids_state], outputs=[alarm_dataframe_original_data, alarm_dataframe])
        delete_alarm_button.click(
            fn=ui_handlers.handle_delete_alarms_and_update_ui,
            inputs=[selected_alarm_ids_state],
            outputs=[
                alarm_dataframe_original_data, alarm_dataframe,
                selected_alarm_ids_state, selection_feedback_markdown
            ]
        )
        alarm_add_button.click(
            fn=ui_handlers.handle_add_or_update_alarm,
            inputs=[
                editing_alarm_id_state, alarm_hour_dropdown, alarm_minute_dropdown,
                alarm_room_dropdown, alarm_context_input, alarm_days_checkboxgroup,
                alarm_emergency_checkbox
            ],
            outputs=[
                alarm_dataframe_original_data, alarm_dataframe,
                alarm_add_button, alarm_context_input, alarm_room_dropdown,
                alarm_days_checkboxgroup, alarm_emergency_checkbox,
                alarm_hour_dropdown, alarm_minute_dropdown,
                editing_alarm_id_state, selected_alarm_ids_state,
                selection_feedback_markdown, cancel_edit_button
            ]
        )
        cancel_edit_button.click(
            fn=ui_handlers.handle_cancel_alarm_edit,
            inputs=None,
            outputs=[
                alarm_add_button, alarm_context_input, alarm_room_dropdown,
                alarm_days_checkboxgroup, alarm_emergency_checkbox,
                alarm_hour_dropdown, alarm_minute_dropdown,
                editing_alarm_id_state, selected_alarm_ids_state,
                selection_feedback_markdown, cancel_edit_button
            ]
        )
        timer_type_radio.change(fn=lambda t: (gr.update(visible=t=="通常タイマー"), gr.update(visible=t=="ポモドーロタイマー"), ""), inputs=[timer_type_radio], outputs=[normal_timer_ui, pomo_timer_ui, timer_status_output])
        timer_submit_button.click(
            fn=ui_handlers.handle_timer_submission,
            inputs=[
            timer_type_radio,
            timer_duration_number,
            pomo_work_number,
            pomo_break_number,
            pomo_cycles_number,
            timer_room_dropdown,
            timer_work_theme_input,
            timer_break_theme_input,
            current_api_key_name_state,
            normal_timer_theme_input
            ],
            outputs=[timer_status_output]
        )

        notification_service_radio.change(fn=ui_handlers.handle_notification_service_change, inputs=[notification_service_radio], outputs=[])

        # Pushover保存ボタンのイベント
        save_pushover_config_button.click(
            fn=ui_handlers.handle_save_pushover_config,
            inputs=[pushover_user_key_input, pushover_app_token_input],
            outputs=None
        )

        # Discord保存ボタンのイベント
        save_discord_webhook_button.click(
            fn=ui_handlers.handle_save_discord_webhook,
            inputs=[discord_webhook_input],
            outputs=None
        )

        # 【v14: 責務分離アーキテクチャ】
        # 1. まず、キーの保存と、それに関連するUIのみを更新する
        save_key_event = save_gemini_key_button.click(
            fn=ui_handlers.handle_save_gemini_key,
            inputs=[gemini_key_name_input, gemini_key_value_input],
            outputs=[
                api_key_dropdown,
                paid_keys_checkbox_group,
                gemini_key_name_input,
                gemini_key_value_input,
            ]
        )
        # 2. その後(.then)、UI全体を初期化する司令塔を呼び出す
        save_key_event.then(
            fn=ui_handlers.handle_initial_load,
            inputs=None,
            outputs=initial_load_outputs
        )

        memory_archiving_outputs = [
            memos_import_button,
            importer_stop_button,
            archivist_pid_state,
            debug_console_state,
            debug_console_output,
            chat_input_multimodal,
            visualize_graph_button
        ]

        import_event = memos_import_button.click(
            fn=ui_handlers.handle_memory_archiving,
            inputs=[current_room_name, debug_console_state],
            outputs=memory_archiving_outputs
        )

        importer_stop_button.click(
            fn=ui_handlers.handle_archivist_stop,
            inputs=[archivist_pid_state],
            outputs=[
                memos_import_button,
                importer_stop_button,
                archivist_pid_state,
                chat_input_multimodal
            ],
            cancels=[import_event] # 実行中のイベントをキャンセル
        )

        add_log_to_memory_queue_button.click(
            fn=ui_handlers.handle_add_current_log_to_queue,
            inputs=[current_room_name, debug_console_state],
            # 成功/失敗を通知するだけなので、outputは無しで良い
            outputs=None
        )

        visualize_graph_button.click(
            fn=ui_handlers.handle_visualize_graph,
            inputs=[current_room_name],
            outputs=[graph_image_display]
        )

        core_memory_update_button.click(
            fn=ui_handlers.handle_core_memory_update_click,
            inputs=[current_room_name, current_api_key_name_state],
            outputs=[core_memory_editor] # <-- None から変更
        )

        update_episodic_memory_button.click(
            fn=ui_handlers.handle_update_episodic_memory,
            inputs=[current_room_name, current_api_key_name_state],
            outputs=[update_episodic_memory_button, chat_input_multimodal, episodic_update_status]
        )

        # --- Goals Events ---
        refresh_goals_button.click(
            fn=ui_handlers.handle_refresh_goals,
            inputs=[current_room_name],
            outputs=[short_term_goals_display, long_term_goals_display, goals_meta_display]
        )
        
        clear_open_questions_button.click(
            fn=ui_handlers.handle_clear_open_questions,
            inputs=[current_room_name],
            outputs=[open_questions_display, open_questions_status, selected_question_topics_state]
        )
        
        # selectイベント：選択された行の話題をStateに保存
        open_questions_display.select(
            fn=ui_handlers.handle_question_row_selection,
            inputs=[open_questions_display],
            outputs=[selected_question_topics_state, open_questions_status]
        )
        
        delete_selected_questions_button.click(
            fn=ui_handlers.handle_delete_selected_questions,
            inputs=[current_room_name, selected_question_topics_state],
            outputs=[open_questions_display, open_questions_status, selected_question_topics_state]
        )
        
        resolve_selected_questions_button.click(
            fn=ui_handlers.handle_resolve_selected_questions,
            inputs=[current_room_name, selected_question_topics_state],
            outputs=[open_questions_display, open_questions_status, selected_question_topics_state]
        )
        
        # --- Internal State Maintenance ---
        reset_internal_state_button.click(
            fn=ui_handlers.handle_reset_internal_state,
            inputs=[current_room_name],
            outputs=[reset_internal_state_status]
        )

        # --- Watchlist Events ---
        watchlist_refresh_button.click(
            fn=ui_handlers.handle_watchlist_refresh,
            inputs=[current_room_name],
            outputs=[watchlist_dataframe, watchlist_status]
        )
        
        # 監視頻度変更時に指定時刻入力欄の表示/非表示を切り替え
        def toggle_daily_time_visibility(interval):
            return gr.update(visible=(interval == "daily"))
        
        watchlist_interval_dropdown.change(
            fn=toggle_daily_time_visibility,
            inputs=[watchlist_interval_dropdown],
            outputs=[watchlist_daily_time_row]
        )
        
        watchlist_add_button.click(
            fn=ui_handlers.handle_watchlist_add,
            inputs=[current_room_name, watchlist_url_input, watchlist_name_input, watchlist_interval_dropdown, watchlist_daily_time],
            outputs=[watchlist_dataframe, watchlist_status]
        )
        
        watchlist_check_button.click(
            fn=ui_handlers.handle_watchlist_check_all,
            inputs=[current_room_name, api_key_dropdown],
            outputs=[watchlist_dataframe, watchlist_status]
        )
        
        # DataFrameの行選択イベント（Golden Contract準拠）
        def on_watchlist_select(df_data, evt: gr.SelectData):
            if evt is None or evt.index is None or df_data is None:
                return [""] * 5
            
            # evt.indexはタプル(row, col)または単一の整数の場合がある
            idx = evt.index
            row_idx = idx[0] if isinstance(idx, (tuple, list)) else idx
            
            if row_idx is not None:
                try:
                    # df_dataがDataFrameの場合
                    if hasattr(df_data, "iloc"):
                        row = df_data.iloc[row_idx]
                        selected_id = str(row.iloc[0])
                        name = str(row.iloc[1])
                        url = str(row.iloc[2])
                        interval_display = str(row.iloc[3])
                    else:
                        # リストの場合
                        row = df_data[row_idx]
                        selected_id = str(row[0])
                        name = str(row[1])
                        url = str(row[2])
                        interval_display = str(row[3])
                    
                    # 頻度表示（"毎日 09:00" など）から内部値（"daily", "09:00"）を復元
                    interval_val = "manual"
                    daily_time_val = "09:00"
                    
                    if "毎日" in interval_display:
                        interval_val = "daily"
                        if " " in interval_display:
                            daily_time_val = interval_display.split(" ")[1]
                    elif "1時間" in interval_display: interval_val = "hourly_1"
                    elif "3時間" in interval_display: interval_val = "hourly_3"
                    elif "6時間" in interval_display: interval_val = "hourly_6"
                    elif "12時間" in interval_display: interval_val = "hourly_12"
                    
                    return selected_id, url, name, interval_val, daily_time_val
                except Exception as e:
                    print(f"Error in on_watchlist_select: {e}")
            
            return [""] * 5
        
        watchlist_dataframe.select(
            fn=on_watchlist_select,
            inputs=[watchlist_dataframe],
            outputs=[watchlist_selected_id, watchlist_url_input, watchlist_name_input, watchlist_interval_dropdown, watchlist_daily_time]
        )
        
        def delete_selected_wrapper(room_name, selected_id, df_data):
            if not selected_id:
                import gradio as gr
                gr.Warning("削除するエントリを選択してください")
                return gr.update(), "エントリを選択してください"
            
            # 選択されたIDを含む行を探す
            selected_row = None
            if df_data is not None:
                # df_dataがDataFrameの場合とリストの場合の両方に対応
                import pandas as pd
                if isinstance(df_data, pd.DataFrame):
                    for _, row in df_data.iterrows():
                        if str(row.iloc[0]) == selected_id:
                            # 後の処理(handle_watchlist_delete)がリストを期待しているため変換
                            selected_row = row.tolist()
                            break
                elif isinstance(df_data, list):
                    for row in df_data:
                        if str(row[0]) == selected_id:
                            selected_row = row
                            break
            
            return ui_handlers.handle_watchlist_delete(room_name, selected_row)
        
        watchlist_delete_button.click(
            fn=delete_selected_wrapper,
            inputs=[current_room_name, watchlist_selected_id, watchlist_dataframe],
            outputs=[watchlist_dataframe, watchlist_status]
        )
        
        # ウォッチリストアコーディオンが開いたときにリフレッシュ
        def refresh_watchlist_and_groups(room_name):
            df, status = ui_handlers.handle_watchlist_refresh(room_name)
            group_df, _ = ui_handlers.handle_group_refresh(room_name)
            choices_update = ui_handlers.handle_get_group_choices(room_name)
            return df, status, group_df, choices_update
        
        watchlist_accordion.expand(
            fn=refresh_watchlist_and_groups,
            inputs=[current_room_name],
            outputs=[watchlist_dataframe, watchlist_status, group_dataframe, watchlist_move_group_dropdown]
        )
        
        # --- Group Management Events ---
        
        # グループ作成
        group_create_button.click(
            fn=ui_handlers.handle_group_add,
            inputs=[current_room_name, group_name_input, group_description_input, group_interval_dropdown, group_daily_time],
            outputs=[group_dataframe, group_status]
        ).then(
            fn=ui_handlers.handle_get_group_choices,
            inputs=[current_room_name],
            outputs=[watchlist_move_group_dropdown]
        )
        
        # グループ選択
        def on_group_select(df_data, evt: gr.SelectData):
            if evt is None or evt.index is None or df_data is None:
                return ""
            
            idx = evt.index
            row_idx = idx[0] if isinstance(idx, (tuple, list)) else idx
            
            if row_idx is not None:
                try:
                    if hasattr(df_data, "iloc"):
                        selected_id = str(df_data.iloc[row_idx].iloc[0])
                    else:
                        selected_id = str(df_data[row_idx][0])
                    return selected_id
                except:
                    pass
            return ""
        
        group_dataframe.select(
            fn=on_group_select,
            inputs=[group_dataframe],
            outputs=[group_selected_id]
        )
        
        # グループ削除
        group_delete_button.click(
            fn=ui_handlers.handle_group_delete,
            inputs=[current_room_name, group_selected_id],
            outputs=[group_dataframe, watchlist_dataframe, group_status]
        ).then(
            fn=ui_handlers.handle_get_group_choices,
            inputs=[current_room_name],
            outputs=[watchlist_move_group_dropdown]
        )
        
        # グループ時刻一括変更
        group_update_interval_button.click(
            fn=ui_handlers.handle_group_update_interval,
            inputs=[current_room_name, group_selected_id, group_new_interval_dropdown, group_new_daily_time],
            outputs=[group_dataframe, watchlist_dataframe, group_status]
        )
        
        # エントリーをグループに移動
        watchlist_move_button.click(
            fn=ui_handlers.handle_move_entry_to_group,
            inputs=[current_room_name, watchlist_selected_id, watchlist_move_group_dropdown],
            outputs=[watchlist_dataframe, watchlist_status]
        )
        
        # --- AI自動リスト作成イベント ---
        
        # 候補を検索
        ai_generate_button.click(
            fn=ui_handlers.handle_ai_generate_candidates,
            inputs=[current_room_name, ai_genre_input, api_key_dropdown],
            outputs=[ai_generate_status, ai_candidates_checkboxgroup, ai_candidates_data, ai_add_row, ai_add_to_group_dropdown]
        )
        
        # 選択したサイトを追加
        ai_add_button.click(
            fn=ui_handlers.handle_ai_add_selected,
            inputs=[current_room_name, ai_candidates_checkboxgroup, ai_candidates_data, ai_add_to_group_dropdown],
            outputs=[watchlist_dataframe, group_dataframe, ai_generate_status]
        )

        # --- Dream Journal Events ---
        refresh_dream_button.click(
            fn=ui_handlers.handle_refresh_dream_journal,
            inputs=[current_room_name],
            outputs=[dream_date_dropdown, dream_detail_text, dream_year_filter, dream_month_filter]
        )
        
        show_latest_dream_button.click(
            fn=ui_handlers.handle_show_latest_dream,
            inputs=[current_room_name],
            outputs=[dream_date_dropdown, dream_detail_text, dream_year_filter, dream_month_filter]
        )
        
        dream_year_filter.change(
            fn=ui_handlers.handle_dream_filter_change,
            inputs=[current_room_name, dream_year_filter, dream_month_filter],
            outputs=[dream_date_dropdown]
        )
        
        dream_month_filter.change(
            fn=ui_handlers.handle_dream_filter_change,
            inputs=[current_room_name, dream_year_filter, dream_month_filter],
            outputs=[dream_date_dropdown]
        )
        
        dream_date_dropdown.change(
            fn=ui_handlers.handle_dream_journal_selection_from_dropdown,
            inputs=[current_room_name, dream_date_dropdown],
            outputs=[dream_detail_text]
        )

        # --- [Phase 14] Episodic Memory Browser Events ---
        refresh_episodic_button.click(
            fn=ui_handlers.handle_refresh_episodic_entries,
            inputs=[current_room_name],
            outputs=[episodic_date_dropdown, episodic_detail_text, episodic_year_filter, episodic_month_filter]
        )
        
        show_latest_episodic_button.click(
            fn=ui_handlers.handle_show_latest_episodic,
            inputs=[current_room_name],
            outputs=[episodic_date_dropdown, episodic_detail_text, episodic_year_filter, episodic_month_filter]
        )
        
        episodic_year_filter.change(
            fn=ui_handlers.handle_episodic_filter_change,
            inputs=[current_room_name, episodic_year_filter, episodic_month_filter],
            outputs=[episodic_date_dropdown]
        )
        
        episodic_month_filter.change(
            fn=ui_handlers.handle_episodic_filter_change,
            inputs=[current_room_name, episodic_year_filter, episodic_month_filter],
            outputs=[episodic_date_dropdown]
        )
        
        episodic_date_dropdown.change(
            fn=ui_handlers.handle_episodic_selection_from_dropdown,
            inputs=[current_room_name, episodic_date_dropdown],
            outputs=[episodic_detail_text]
        )

        # --- 📌 Entity Memory Events ---
        refresh_entity_button.click(
            fn=ui_handlers.handle_refresh_entity_list,
            inputs=[current_room_name],
            outputs=[entity_dropdown, entity_content_editor]
        )
        
        entity_dropdown.change(
            fn=ui_handlers.handle_entity_selection_change,
            inputs=[current_room_name, entity_dropdown],
            outputs=[entity_content_editor]
        )
        
        save_entity_button.click(
            fn=ui_handlers.handle_save_entity_memory,
            inputs=[current_room_name, entity_dropdown, entity_content_editor],
            outputs=None
        ).then(fn=lambda: gr.Info("保存しました"), outputs=None)
        
        delete_entity_button.click(
            fn=ui_handlers.handle_delete_entity_memory,
            inputs=[current_room_name, entity_dropdown],
            outputs=[entity_dropdown, entity_content_editor]
        )

        # --- 睡眠時記憶整理チェックボックス即保存 ---
        sleep_consolidation_inputs = [
            current_room_name,
            sleep_consolidation_episodic_cb,
            sleep_consolidation_memory_index_cb,
            sleep_consolidation_current_log_cb,
            sleep_consolidation_entity_memory_cb,
            sleep_consolidation_compress_cb
        ]
        sleep_consolidation_episodic_cb.change(
            fn=ui_handlers.handle_sleep_consolidation_change,
            inputs=sleep_consolidation_inputs,
            outputs=None
        )
        sleep_consolidation_memory_index_cb.change(
            fn=ui_handlers.handle_sleep_consolidation_change,
            inputs=sleep_consolidation_inputs,
            outputs=None
        )
        sleep_consolidation_current_log_cb.change(
            fn=ui_handlers.handle_sleep_consolidation_change,
            inputs=sleep_consolidation_inputs,
            outputs=None
        )
        sleep_consolidation_entity_memory_cb.change(
            fn=ui_handlers.handle_sleep_consolidation_change,
            inputs=sleep_consolidation_inputs,
            outputs=None
        )
        sleep_consolidation_compress_cb.change(
            fn=ui_handlers.handle_sleep_consolidation_change,
            inputs=sleep_consolidation_inputs,
            outputs=None
        )


        
        # --- 手動圧縮ボタン ---
        compress_episodes_button.click(
            fn=ui_handlers.handle_compress_episodes,
            inputs=[current_room_name, current_api_key_name_state],
            outputs=[compress_episodes_status]
        )
        

        
        # --- エンベディングプロバイダ設定（統合後） ---
        embedding_provider_radio.change(
            fn=ui_handlers.handle_embedding_mode_change,
            inputs=[current_room_name, embedding_provider_radio],
            outputs=None
        )

        save_core_memory_button.click(
            fn=ui_handlers.handle_save_core_memory,
            inputs=[current_room_name, core_memory_editor],
            outputs=[core_memory_editor]
        )
        reload_core_memory_button.click(
            fn=ui_handlers.handle_reload_core_memory,
            inputs=[current_room_name],
            outputs=[core_memory_editor]
        )


        # [v21] 画像生成後に背景CSSも更新
        generate_scenery_image_button.click(
            fn=ui_handlers.handle_generate_or_regenerate_scenery_image,
            inputs=[current_room_name, api_key_dropdown, scenery_style_radio],
            outputs=[scenery_image_display]
        ).then(
            fn=ui_handlers.handle_refresh_background_css,
            inputs=[current_room_name],
            outputs=[style_injector]
        )
        # [v21] カスタム画像登録後に背景CSSも更新
        register_custom_scenery_button.click(
            fn=ui_handlers.handle_register_custom_scenery,
            inputs=[current_room_name, api_key_dropdown, custom_scenery_location_dropdown, custom_scenery_season_dropdown, custom_scenery_time_dropdown, custom_scenery_image_upload],
            outputs=[current_scenery_display, scenery_image_display]
        ).then(
            fn=ui_handlers.handle_refresh_background_css,
            inputs=[current_room_name],
            outputs=[style_injector]
        )
        audio_player.stop(fn=lambda: gr.update(visible=False), inputs=None, outputs=[audio_player])
        audio_player.pause(fn=lambda: gr.update(visible=False), inputs=None, outputs=[audio_player])

        world_builder_tab.select(
            fn=ui_handlers.handle_world_builder_load,
            inputs=[current_room_name],
            outputs=[world_data_state, area_selector, world_settings_raw_editor, place_selector]
        )
        area_selector.change(
            fn=ui_handlers.handle_wb_area_select,
            inputs=[world_data_state, area_selector],
            outputs=[place_selector]
        )
        place_selector.change(
            fn=ui_handlers.handle_wb_place_select,
            inputs=[world_data_state, area_selector, place_selector],
            outputs=[content_editor, save_button_row, delete_place_button]
        )
        save_button.click(
            fn=ui_handlers.handle_wb_save,
            inputs=[current_room_name, world_data_state, area_selector, place_selector, content_editor],
            outputs=[world_data_state, world_settings_raw_editor, location_dropdown]
        )
        delete_place_button.click(
            fn=ui_handlers.handle_wb_delete_place,
            inputs=[current_room_name, world_data_state, area_selector, place_selector],
            outputs=[world_data_state, area_selector, place_selector, content_editor, save_button_row, delete_place_button, world_settings_raw_editor, location_dropdown]
        )
        add_area_button.click(
            fn=lambda: ("area", gr.update(visible=True), "#### 新しいエリアの作成"),
            outputs=[new_item_type, new_item_form, new_item_form_title]
        )
        add_place_button.click(
            fn=ui_handlers.handle_wb_add_place_button_click,
            inputs=[area_selector],
            outputs=[new_item_type, new_item_form, new_item_form_title]
        )
        confirm_add_button.click(
            fn=ui_handlers.handle_wb_confirm_add,
            inputs=[current_room_name, world_data_state, area_selector, new_item_type, new_item_name],
            outputs=[world_data_state, area_selector, place_selector, new_item_form, new_item_name, world_settings_raw_editor, location_dropdown]
        )
        cancel_add_button.click(
            fn=lambda: (gr.update(visible=False), ""),
            outputs=[new_item_form, new_item_name]
        )

        # --- アバターアップロード機能のイベント接続 ---

        # 3. アバターモード切り替えイベント
        avatar_mode_radio.change(
            fn=ui_handlers.handle_avatar_mode_change,
            inputs=[current_room_name, avatar_mode_radio],
            outputs=[profile_image_display, expressions_html]
        )

        # 5. 表情差分管理イベント
        # アコーディオンが開かれたら表情リストを読み込む
        expression_management_accordion.expand(
            fn=ui_handlers.refresh_expressions_list,
            inputs=[current_room_name],
            outputs=[expressions_html]
        )
        
        # 表情追加ボタン
        add_expression_button.click(
            fn=ui_handlers.handle_add_expression,
            inputs=[current_room_name, expression_target_dropdown],
            outputs=[expressions_html, expression_target_dropdown, expression_target_dropdown]
        )
        
        # 表情ファイルアップロード
        expression_file_upload.upload(
            fn=ui_handlers.handle_expression_file_upload,
            inputs=[expression_file_upload, current_room_name, expression_target_dropdown],
            outputs=[expressions_html, expression_target_dropdown]
        )

        # 表情削除ボタン
        delete_expression_button.click(
            fn=ui_handlers.handle_delete_expression,
            inputs=[current_room_name, expression_target_dropdown],
            outputs=[expressions_html, expression_target_dropdown]
        )

        # 6. アバター自動待機化タイマー
        auto_idle_timer.tick(
            fn=lambda r: (ui_handlers.get_avatar_html(r, state="neutral"), gr.update(active=False)),
            inputs=[current_room_name],
            outputs=[profile_image_display, auto_idle_timer]
        )

        world_builder_raw_outputs = [
            world_data_state,
            area_selector,
            place_selector,
            world_settings_raw_editor,
            location_dropdown
        ]

        save_raw_button.click(
            fn=ui_handlers.handle_save_world_settings_raw,
            inputs=[current_room_name, world_settings_raw_editor],
            outputs=world_builder_raw_outputs
        )
        reload_raw_button.click(
            fn=ui_handlers.handle_reload_world_settings_raw,
            inputs=[current_room_name],
            outputs=world_builder_raw_outputs
        )

        # --- 会話ログ管理のイベント接続 ---
        # タブが選択された時にリストを更新し、最新ログを読み込む → 最下部にスクロール
        chat_log_management_tab.select(
            fn=ui_handlers.handle_refresh_chat_log_months,
            inputs=[current_room_name],
            outputs=[chat_log_month_dropdown]
        ).then(
            fn=ui_handlers.handle_load_chat_log_raw,
            inputs=[
                current_room_name, 
                chat_log_month_dropdown,
                room_add_timestamp_checkbox,
                room_display_thoughts_checkbox,
                screenshot_mode_checkbox,
                redaction_rules_state
            ],
            outputs=[chat_log_raw_editor, chat_log_preview_chatbot]
        ).then(
            fn=None,
            inputs=None,
            outputs=None,
            js="""
            () => {
                setTimeout(() => {
                    const editor = document.querySelector('#chat_log_raw_editor .cm-scroller');
                    if (editor) {
                        editor.scrollTop = editor.scrollHeight;
                    }
                }, 100);
            }
            """
        )
        
        # 保存ボタン: ログを保存してチャット表示・プレビューを更新 → 最下部にスクロール
        save_chat_log_button.click(
            fn=ui_handlers.handle_save_chat_log_raw,
            inputs=[
                current_room_name,
                chat_log_raw_editor,
                api_history_limit_state,
                room_add_timestamp_checkbox,
                room_display_thoughts_checkbox,
                screenshot_mode_checkbox,
                redaction_rules_state,
                chat_log_month_dropdown
            ],
            outputs=[chat_log_raw_editor, chatbot_display, current_log_map_state, chat_log_preview_chatbot]
        ).then(
            fn=None,
            inputs=None,
            outputs=None,
            js="""
            () => {
                setTimeout(() => {
                    const editor = document.querySelector('#chat_log_raw_editor .cm-scroller');
                    if (editor) {
                        editor.scrollTop = editor.scrollHeight;
                    }
                }, 100);
            }
            """
        )
        
        # 再読込ボタン: 最後に保存した内容を読み込む → 最下部にスクロール
        reload_chat_log_button.click(
            fn=ui_handlers.handle_reload_chat_log_raw,
            inputs=[
                current_room_name, 
                chat_log_month_dropdown,
                room_add_timestamp_checkbox,
                room_display_thoughts_checkbox,
                screenshot_mode_checkbox,
                redaction_rules_state
            ],
            outputs=[chat_log_raw_editor, chat_log_preview_chatbot]
        ).then(
            fn=None,
            inputs=None,
            outputs=None,
            js="""
            () => {
                setTimeout(() => {
                    const editor = document.querySelector('#chat_log_raw_editor .cm-scroller');
                    if (editor) {
                        editor.scrollTop = editor.scrollHeight;
                    }
                }, 100);
            }
            """
        )

        # 月選択ドロップダウン変更時
        chat_log_month_dropdown.change(
            fn=ui_handlers.handle_load_chat_log_raw,
            inputs=[
                current_room_name, 
                chat_log_month_dropdown,
                room_add_timestamp_checkbox,
                room_display_thoughts_checkbox,
                screenshot_mode_checkbox,
                redaction_rules_state
            ],
            outputs=[chat_log_raw_editor, chat_log_preview_chatbot]
        ).then(
            fn=None,
            inputs=None,
            outputs=None,
            js="""
            () => {
                setTimeout(() => {
                    const editor = document.querySelector('#chat_log_raw_editor .cm-scroller');
                    if (editor) {
                        editor.scrollTop = editor.scrollHeight;
                    }
                }, 100);
            }
            """
        )

        # リスト更新ボタン
        refresh_chat_log_months_button.click(
            fn=ui_handlers.handle_refresh_chat_log_months,
            inputs=[current_room_name],
            outputs=[chat_log_month_dropdown]
        )

        # 検索ボタン
        chat_log_search_button.click(
            fn=ui_handlers.handle_search_chat_log_keyword,
            inputs=[current_room_name, chat_log_search_textbox],
            outputs=[chat_log_month_dropdown]
        ).then(
            # 検索後に（もしヒットして選択値が変わっていれば）その月のログを読み込む
            fn=ui_handlers.handle_load_chat_log_raw,
            inputs=[
                current_room_name, 
                chat_log_month_dropdown,
                room_add_timestamp_checkbox,
                room_display_thoughts_checkbox,
                screenshot_mode_checkbox,
                redaction_rules_state
            ],
            outputs=[chat_log_raw_editor, chat_log_preview_chatbot]
        ).then(
            fn=None,
            inputs=None,
            outputs=None,
            js="""
            () => {
                setTimeout(() => {
                    const editor = document.querySelector('#chat_log_raw_editor .cm-scroller');
                    if (editor) {
                        editor.scrollTop = editor.scrollHeight;
                    }
                }, 100);
            }
            """
        )

        # 検索ボックスでEnterキーを押した時も同様
        chat_log_search_textbox.submit(
            fn=ui_handlers.handle_search_chat_log_keyword,
            inputs=[current_room_name, chat_log_search_textbox],
            outputs=[chat_log_month_dropdown]
        ).then(
            fn=ui_handlers.handle_load_chat_log_raw,
            inputs=[
                current_room_name, 
                chat_log_month_dropdown,
                room_add_timestamp_checkbox,
                room_display_thoughts_checkbox,
                screenshot_mode_checkbox,
                redaction_rules_state
            ],
            outputs=[chat_log_raw_editor, chat_log_preview_chatbot]
        )

        clear_debug_console_button.click(
            fn=lambda: ("", ""),
            outputs=[debug_console_state, debug_console_output]
        )
        # --- Attachment Management Event Handlers ---
        attachment_tab.select(
            fn=ui_handlers.handle_attachment_tab_load,
            inputs=[current_room_name],
            outputs=[attachments_df, active_attachments_state, active_attachments_display]
        )

        attachments_df.select(
            fn=ui_handlers.handle_attachment_selection,
            inputs=[current_room_name, attachments_df, active_attachments_state],
            outputs=[active_attachments_state, active_attachments_display, selected_attachment_index_state],
            show_progress=False
        ).then(
            fn=ui_handlers.update_token_count_after_attachment_change,
            inputs=attachment_change_token_calc_inputs,
            outputs=token_count_display
        )

        delete_attachment_button.click(
            fn=ui_handlers.handle_delete_attachment,
            inputs=[current_room_name, selected_attachment_index_state, active_attachments_state],
            outputs=[attachments_df, selected_attachment_index_state, active_attachments_state, active_attachments_display]
        ).then(
            fn=ui_handlers.update_token_count_after_attachment_change,
            inputs=attachment_change_token_calc_inputs,
            outputs=token_count_display
        )

        open_attachments_folder_button.click(
            fn=ui_handlers.handle_open_attachments_folder,
            inputs=[current_room_name],
            outputs=None
        )

        # --- 書き置き機能 Event Handlers ---
        save_user_memo_button.click(
            fn=ui_handlers.handle_save_user_memo,
            inputs=[current_room_name, user_memo_textbox],
            outputs=None
        )
        clear_user_memo_button.click(
            fn=ui_handlers.handle_clear_user_memo,
            inputs=[current_room_name],
            outputs=[user_memo_textbox]
        )

        # --- ChatGPT Importer Event Handlers ---
        chatgpt_import_file.upload(
            fn=ui_handlers.handle_chatgpt_file_upload,
            inputs=[chatgpt_import_file],
            outputs=[chatgpt_thread_dropdown, chatgpt_import_form, chatgpt_thread_choices_state]
        )

        chatgpt_thread_dropdown.change(
            fn=ui_handlers.handle_chatgpt_thread_selection,
            inputs=[chatgpt_thread_choices_state, chatgpt_thread_dropdown],
            outputs=[chatgpt_room_name_textbox]
        )


        chatgpt_import_button.click(
            fn=ui_handlers.handle_chatgpt_import_button_click,
            inputs=[
                chatgpt_import_file,
                chatgpt_thread_dropdown,
                chatgpt_room_name_textbox,
                chatgpt_user_name_textbox
            ],
            outputs=[
                chatgpt_import_file,
                chatgpt_import_form,
                room_dropdown,
                manage_room_selector,
                alarm_room_dropdown,
                timer_room_dropdown
            ]
        )

        # --- Claude Importer Event Handlers ---
        claude_import_file.upload(
            fn=ui_handlers.handle_claude_file_upload,
            inputs=[claude_import_file],
            outputs=[claude_thread_dropdown, claude_import_form, claude_thread_choices_state]
        )

        claude_thread_dropdown.change(
            fn=ui_handlers.handle_claude_thread_selection,
            inputs=[claude_thread_choices_state, claude_thread_dropdown],
            outputs=[claude_room_name_textbox]
        )

        claude_import_button.click(
            fn=ui_handlers.handle_claude_import_button_click,
            inputs=[
            claude_import_file,
            claude_thread_dropdown,
            claude_room_name_textbox,
            claude_user_name_textbox
            ],
            outputs=[
            claude_import_file,
            claude_import_form,
            room_dropdown,
            manage_room_selector,
            alarm_room_dropdown,
            timer_room_dropdown
            ]
        )

        # --- Generic Importer Event Handlers ---
        generic_import_file.upload(
            fn=ui_handlers.handle_generic_file_upload,
            inputs=[generic_import_file],
            outputs=[
            generic_import_form,
            generic_room_name_textbox,
            generic_user_name_textbox,
            generic_user_header_textbox,
            generic_agent_header_textbox
            ]
        )

        generic_import_button.click(
            fn=ui_handlers.handle_generic_import_button_click,
            inputs=[
            generic_import_file,
            generic_room_name_textbox,
            generic_user_name_textbox,
            generic_user_header_textbox,
            generic_agent_header_textbox
            ],
            outputs=[
            generic_import_file,
            generic_import_form,
            room_dropdown,
            manage_room_selector,
            alarm_room_dropdown,
            timer_room_dropdown
            ]
        )

        # --- Gemini Importer Event Handlers ---
        gemini_import_button.click(
            fn=ui_handlers.handle_gemini_import_button_click,
            inputs=[
                gemini_import_url,
                current_room_name,
                api_history_limit_state,
                room_add_timestamp_checkbox,
                room_display_thoughts_checkbox,
                screenshot_mode_checkbox,
                redaction_rules_state
            ],
            outputs=[chatbot_display, current_log_map_state, gemini_import_status, gemini_import_url]
        )

        # --- Theme Management Event Handlers ---
        theme_tab.select(
            fn=ui_handlers.handle_theme_tab_load,
            inputs=None,
            outputs=[theme_selector, theme_preview_light, theme_preview_dark]
        ).then(
            fn=ui_handlers.handle_room_theme_reload,
            inputs=[room_dropdown],
            outputs=[
                room_theme_enabled_checkbox,  # 個別テーマのオンオフ
                chat_style_radio, font_size_slider, line_height_slider,
                # 基本配色
                theme_primary_picker, theme_secondary_picker, theme_background_picker,
                theme_text_picker, theme_accent_soft_picker,
                # 詳細設定
                theme_input_bg_picker, theme_input_border_picker, theme_code_bg_picker,
                theme_subdued_text_picker,
                theme_button_bg_picker, theme_button_hover_picker,
                theme_stop_button_bg_picker, theme_stop_button_hover_picker,
                theme_checkbox_off_picker, theme_table_bg_picker, theme_radio_label_picker, theme_dropdown_list_bg_picker,
                theme_ui_opacity_slider,
                # 背景画像設定
                theme_bg_image_picker, theme_bg_opacity_slider, theme_bg_blur_slider,
                theme_bg_size_dropdown, theme_bg_position_dropdown, theme_bg_repeat_dropdown,
                theme_bg_custom_width, theme_bg_radius_slider, theme_bg_mask_blur_slider,
                theme_bg_overlay_checkbox,
                theme_bg_src_mode,
                # Sync設定
                theme_bg_sync_opacity_slider, theme_bg_sync_blur_slider,
                theme_bg_sync_size_dropdown, theme_bg_sync_position_dropdown, theme_bg_sync_repeat_dropdown,
                theme_bg_sync_custom_width, theme_bg_sync_radius_slider, theme_bg_sync_mask_blur_slider,
                theme_bg_sync_overlay_checkbox,
                # CSS注入
                style_injector
            ]
        )

        theme_selector.change(
            fn=ui_handlers.handle_theme_selection,
            inputs=[theme_selector],
            outputs=[
                theme_preview_light, theme_preview_dark,
                primary_hue_picker, secondary_hue_picker, neutral_hue_picker,
                font_dropdown, save_theme_button, export_theme_button
            ]
        )

        save_theme_button.click(
            fn=ui_handlers.handle_save_custom_theme,
            inputs=[
                custom_theme_name_input, primary_hue_picker, 
                secondary_hue_picker, neutral_hue_picker, font_dropdown
            ],
            outputs=[theme_selector, custom_theme_name_input]
        )
        
        export_theme_button.click(
            fn=ui_handlers.handle_export_theme_to_file,
            inputs=[
                custom_theme_name_input, primary_hue_picker,
                secondary_hue_picker, neutral_hue_picker, font_dropdown
            ],
            outputs=[custom_theme_name_input]
        )

        apply_theme_button.click(
            fn=ui_handlers.handle_apply_theme,
            inputs=[theme_selector],
            outputs=None
        )

        backup_rotation_count_number.change(
            fn=ui_handlers.handle_save_backup_rotation_count,
            inputs=[backup_rotation_count_number],
            outputs=None
        )
        
        open_backup_folder_button.click(
            fn=ui_handlers.handle_open_backup_folder,
            inputs=[current_room_name],
            outputs=None
        )

        # --- [v6: 時間連動情景更新イベント] ---
        # 時間設定UIのいずれかの値が変更されたら、新しい統合ハンドラを呼び出す
        time_setting_inputs = [
            current_room_name,
            current_api_key_name_state,
            time_mode_radio,
            fixed_season_dropdown,
            fixed_time_of_day_dropdown
        ]
        time_setting_outputs = [
            current_scenery_display,
            scenery_image_display
        ]

        # 1. モードが切り替わった時
        time_mode_radio.change(
            fn=ui_handlers.handle_time_settings_change_and_update_scenery,
            inputs=time_setting_inputs,
            outputs=time_setting_outputs
        ).then(
            # その後、UIの表示/非表示を切り替える
            fn=ui_handlers.handle_time_mode_change,
            inputs=[time_mode_radio],
            outputs=[fixed_time_controls]
        )

        # 2. 固定モードの季節が変更された時
        fixed_season_dropdown.change(
            fn=ui_handlers.handle_time_settings_change_and_update_scenery,
            inputs=time_setting_inputs,
            outputs=time_setting_outputs
        )

        # 3. 固定モードの時間帯が変更された時
        fixed_time_of_day_dropdown.change(
            fn=ui_handlers.handle_time_settings_change_and_update_scenery,
            inputs=time_setting_inputs,
            outputs=time_setting_outputs
        )

        # 4. 保存ボタンが押された時（念のため残すが、主役はchangeイベント）
        save_time_settings_button.click(
            fn=ui_handlers.handle_time_settings_change_and_update_scenery,
            inputs=time_setting_inputs,
            outputs=time_setting_outputs
        )

        # --- [v7: 情景システム ON/OFF イベント] ---
        enable_scenery_system_checkbox.change(
            fn=ui_handlers.handle_enable_scenery_system_change,
            inputs=[enable_scenery_system_checkbox],
            outputs=[profile_scenery_accordion, room_send_scenery_checkbox]
        )

        # フォルダを開くボタンのイベント
        open_room_folder_button.click(
            fn=ui_handlers.handle_open_room_folder,
            inputs=[manage_folder_name_display], # 管理タブで選択されているルームのフォルダ名
            outputs=None
        )
        open_audio_folder_button.click(
            fn=ui_handlers.handle_open_audio_folder,
            inputs=[current_room_name], # 現在チャット中のルーム名
            outputs=None
        )

        # --- Knowledge Tab Event Handlers ---
        knowledge_tab.select(
            fn=ui_handlers.handle_knowledge_tab_load,
            inputs=[current_room_name],
            outputs=[knowledge_file_df, knowledge_status_output]
        )

        knowledge_upload_button.upload(
            fn=ui_handlers.handle_knowledge_file_upload,
            inputs=[current_room_name, knowledge_upload_button],
            outputs=[knowledge_file_df, knowledge_status_output]
        )

        knowledge_file_df.select(
            fn=ui_handlers.handle_knowledge_file_select,
            inputs=[knowledge_file_df],
            outputs=[selected_knowledge_file_index_state],
            show_progress=False
        )

        knowledge_delete_button.click(
            fn=ui_handlers.handle_knowledge_file_delete,
            inputs=[current_room_name, selected_knowledge_file_index_state],
            outputs=[knowledge_file_df, knowledge_status_output, selected_knowledge_file_index_state]
        )

        knowledge_reindex_button.click(
            fn=ui_handlers.handle_knowledge_reindex,
            inputs=[current_room_name, current_api_key_name_state],
            outputs=[knowledge_status_output, knowledge_reindex_button]
        )

        memory_reindex_button.click(
            fn=ui_handlers.handle_memory_reindex,
            inputs=[current_room_name, current_api_key_name_state],
            outputs=[memory_reindex_status, memory_reindex_button]
        )

        full_reindex_button.click(
            fn=ui_handlers.handle_full_reindex,
            inputs=[current_room_name, current_api_key_name_state],
            outputs=[memory_reindex_status, memory_reindex_button] # 既存のステータスとボタンを共有
        )

        current_log_reindex_button.click(
            fn=ui_handlers.handle_current_log_reindex,
            inputs=[current_room_name, current_api_key_name_state],
            outputs=[current_log_reindex_status, current_log_reindex_button]
        )

        manual_dream_button.click(
            fn=ui_handlers.handle_manual_dreaming,
            inputs=[current_room_name, current_api_key_name_state],
            outputs=[manual_dream_button, dream_status_display]
        )



        play_audio_event = play_audio_button.click(
            fn=ui_handlers.handle_play_audio_button_click,
            inputs=[selected_message_state, current_room_name, api_key_dropdown],
            outputs=[audio_player, play_audio_button, rerun_button]
        )
        play_audio_event.failure(fn=ui_handlers._reset_play_audio_on_failure, inputs=None, outputs=[audio_player, play_audio_button, rerun_button])

        copy_scenery_prompt_button.click(
            fn=None, inputs=[scenery_prompt_output_textbox], outputs=None,
            js="(text) => { navigator.clipboard.writeText(text); const toast = document.createElement('gradio-toast'); toast.setAttribute('description', 'プロンプトをコピーしました！'); document.querySelector('.gradio-toast-container-x-center').appendChild(toast); }"
        )

        generate_scenery_prompt_button.click(
            fn=ui_handlers.handle_show_scenery_prompt,
            inputs=[current_room_name, api_key_dropdown, scenery_style_radio],
            outputs=[scenery_prompt_output_textbox]
        )

        search_provider_radio.change(
            fn=ui_handlers.handle_search_provider_change,
            inputs=[search_provider_radio],
            outputs=None  # 個別表示制御を廃止し、常時表示（アコーディオン）へ
        )
        
        save_tavily_key_button.click(
            fn=ui_handlers.handle_save_tavily_key,
            inputs=[tavily_api_key_input],
            outputs=None
        )
        
        save_zhipu_key_button.click(
            fn=ui_handlers.handle_save_zhipu_key,
            inputs=[zhipu_api_key_input],
            outputs=None
        )
        
        save_groq_key_button.click(
            fn=ui_handlers.handle_save_groq_key,
            inputs=[groq_api_key_input],
            outputs=[groq_api_key_input]
        )

        save_moonshot_key_button.click(
            fn=ui_handlers.handle_save_moonshot_key,
            inputs=[moonshot_api_key_input],
            outputs=None
        )
        
        save_local_model_path_button.click(
            fn=ui_handlers.handle_save_local_model_path,
            inputs=[local_model_path_input],
            outputs=[local_model_path_input]
        )

# --- API Key / Webhook Events ---
        settings_rotation_checkbox.change(
            fn=ui_handlers.handle_rotation_setting_change,
            inputs=[settings_rotation_checkbox],
            outputs=None
        )

        paid_keys_checkbox_group.change(
            fn=ui_handlers.handle_paid_keys_change,
            inputs=[paid_keys_checkbox_group],
            outputs=[api_key_dropdown]
        )
        
        allow_external_connection_checkbox.change(
            fn=ui_handlers.handle_allow_external_connection_change,
            inputs=[allow_external_connection_checkbox],
            outputs=None
        )

# --- Multi-Provider Events ---
        provider_radio.change(
            fn=ui_handlers.handle_provider_change,
            inputs=[provider_radio],
            outputs=[google_settings_group, openai_settings_group]
        )
        
        openai_profile_dropdown.change(
            fn=ui_handlers.handle_openai_profile_select,
            inputs=[openai_profile_dropdown],
            outputs=[openai_base_url_input, openai_api_key_input, openai_model_dropdown]
        )
        
        save_openai_config_button.click(
            fn=ui_handlers.handle_save_openai_config,
            inputs=[openai_profile_dropdown, openai_base_url_input, openai_api_key_input, openai_model_dropdown, openai_tool_use_checkbox],
            outputs=None
        )
        
        # --- [Phase 3] 内部処理モデル設定ボタンのイベント ---
        
        
        save_internal_model_button.click(
            fn=ui_handlers.handle_save_internal_model_settings,
            inputs=[
                internal_processing_provider,
                internal_processing_model,
                internal_summarization_provider,
                internal_summarization_model,
                embedding_provider_radio,
                embedding_model_dropdown,
                internal_fallback_checkbox
            ],
            outputs=[internal_model_status]
        )
        
        reset_internal_model_button.click(
            fn=ui_handlers.handle_reset_internal_model_settings,
            inputs=None,
            outputs=[
                internal_processing_provider,
                internal_processing_model,
                internal_summarization_provider,
                internal_summarization_model,
                embedding_provider_radio,
                embedding_model_dropdown,
                internal_fallback_checkbox, 
                internal_model_status
            ]
        )
        
        # --- 画像生成マルチプロバイダ設定のイベント ---
        image_gen_provider_radio.change(
            fn=ui_handlers.handle_image_gen_provider_change,
            inputs=[image_gen_provider_radio],
            outputs=[gemini_model_section, openai_image_section]
        )
        
        save_image_gen_button.click(
            fn=ui_handlers.handle_save_image_generation_settings,
            inputs=[image_gen_provider_radio, gemini_image_model_dropdown, openai_image_profile_dropdown, openai_image_model_dropdown],
            outputs=None
        )
        
        # カスタムモデル追加ボタンのイベント
        add_custom_model_button.click(
            fn=ui_handlers.handle_add_custom_openai_model,
            inputs=[openai_profile_dropdown, custom_model_name_input],
            outputs=[openai_model_dropdown, custom_model_name_input]
        )

        # --- Geminiモデルリスト管理ボタンのイベント ---
        delete_model_button.click(
            fn=ui_handlers.handle_delete_gemini_model,
            inputs=[model_dropdown],
            outputs=[model_dropdown]
        )
        
        reset_models_button.click(
            fn=ui_handlers.handle_reset_gemini_models_to_default,
            inputs=None,
            outputs=[model_dropdown]
        )

        # --- OpenAI互換モデルリスト管理ボタンのイベント ---
        delete_openai_model_button.click(
            fn=ui_handlers.handle_delete_openai_model,
            inputs=[openai_profile_dropdown, openai_model_dropdown],
            outputs=[openai_model_dropdown]
        )
        
        reset_openai_models_button.click(
            fn=ui_handlers.handle_reset_openai_models_to_default,
            inputs=[openai_profile_dropdown],
            outputs=[openai_model_dropdown]
        )
        
        fetch_models_button.click(
            fn=ui_handlers.handle_fetch_models,
            inputs=[openai_profile_dropdown, openai_base_url_input, openai_api_key_input],
            outputs=[openai_model_dropdown]
        )
        
        toggle_favorite_button.click(
            fn=ui_handlers.handle_toggle_favorite,
            inputs=[openai_profile_dropdown, openai_model_dropdown],
            outputs=[openai_model_dropdown]
        )

        # --- 個別設定のモデルリスト管理ボタンのイベント ---
        # Gemini個別設定
        room_delete_gemini_model_button.click(
            fn=ui_handlers.handle_delete_gemini_model,
            inputs=[room_model_dropdown],
            outputs=[room_model_dropdown]
        )
        
        room_reset_gemini_models_button.click(
            fn=ui_handlers.handle_reset_gemini_models_to_default,
            inputs=None,
            outputs=[room_model_dropdown]
        )
        
        # OpenAI互換個別設定
        room_delete_openai_model_button.click(
            fn=ui_handlers.handle_delete_openai_model,
            inputs=[room_openai_profile_dropdown, room_openai_model_dropdown],
            outputs=[room_openai_model_dropdown]
        )
        
        room_reset_openai_models_button.click(
            fn=ui_handlers.handle_reset_openai_models_to_default,
            inputs=[room_openai_profile_dropdown],
            outputs=[room_openai_model_dropdown]
        )
        
        room_fetch_models_button.click(
            fn=ui_handlers.handle_fetch_models,
            inputs=[room_openai_profile_dropdown, room_openai_base_url_input, room_openai_api_key_input],
            outputs=[room_openai_model_dropdown]
        )
        
        room_toggle_favorite_button.click(
            fn=ui_handlers.handle_toggle_favorite,
            inputs=[room_openai_profile_dropdown, room_openai_model_dropdown],
            outputs=[room_openai_model_dropdown]
        )

        # --- 「💼 お出かけ」専用タブのイベント接続 ---
        
        # データ読み込み
        outing_load_button.click(
            fn=ui_handlers.handle_outing_load_all_sections,
            inputs=[
                current_room_name, outing_episode_days_slider, outing_log_count_slider,
                outing_logs_include_timestamp, outing_logs_include_model
            ],
            outputs=[
                outing_system_prompt_text, outing_system_prompt_chars,
                outing_permanent_text, outing_permanent_chars,
                outing_diary_text, outing_diary_chars,
                outing_episodic_text, outing_episodic_chars,
                outing_logs_text, outing_logs_chars,
                outing_total_char_count
            ]
        )
        
        # セクション別圧縮
        outing_system_prompt_compress.click(
            fn=lambda text, room: ui_handlers.handle_outing_compress_section(text, "システムプロンプト", room),
            inputs=[outing_system_prompt_text, current_room_name],
            outputs=[outing_system_prompt_text, outing_system_prompt_chars]
        )
        outing_permanent_compress.click(
            fn=lambda text, room: ui_handlers.handle_outing_compress_section(text, "永続記憶", room),
            inputs=[outing_permanent_text, current_room_name],
            outputs=[outing_permanent_text, outing_permanent_chars]
        )
        outing_diary_compress.click(
            fn=lambda text, room: ui_handlers.handle_outing_compress_section(text, "日記要約", room),
            inputs=[outing_diary_text, current_room_name],
            outputs=[outing_diary_text, outing_diary_chars]
        )
        outing_episodic_compress.click(
            fn=lambda text, room: ui_handlers.handle_outing_compress_section(text, "エピソード記憶", room),
            inputs=[outing_episodic_text, current_room_name],
            outputs=[outing_episodic_text, outing_episodic_chars]
        )
        outing_logs_compress.click(
            fn=lambda text, room: ui_handlers.handle_outing_compress_section(text, "会話ログ", room),
            inputs=[outing_logs_text, current_room_name],
            outputs=[outing_logs_text, outing_logs_chars]
        )
        
        # エクスポート
        outing_export_button.click(
            fn=ui_handlers.handle_outing_export_sections,
            inputs=[
                current_room_name,
                outing_system_prompt_text, outing_system_prompt_enabled,
                outing_permanent_text, outing_permanent_enabled,
                outing_diary_text, outing_diary_enabled,
                outing_episodic_text, outing_episodic_enabled,
                outing_logs_text, outing_logs_enabled,
                outing_logs_wrap_tags
            ],
            outputs=[outing_download_file]
        )
        
        # フォルダを開く
        outing_open_folder_button.click(
            fn=ui_handlers.handle_open_outing_folder,
            inputs=[current_room_name],
            outputs=None
        )

        # 帰宅（インポート）
        outing_import_button.click(
            fn=ui_handlers.handle_import_return_log,
            inputs=[
                outing_import_file, current_room_name,
                outing_import_source,
                outing_import_user_header, outing_import_agent_header,
                api_history_limit_state, room_add_timestamp_checkbox,
                room_display_thoughts_checkbox, screenshot_mode_checkbox, redaction_rules_state
            ],
            outputs=[chatbot_display, current_log_map_state, outing_import_status, outing_import_file]
        )
        
        # 合計文字数のリアルタイム更新（テキスト・チェックボックス変更時）
        outing_total_inputs = [
            outing_system_prompt_text, outing_system_prompt_enabled,
            outing_permanent_text, outing_permanent_enabled,
            outing_diary_text, outing_diary_enabled,
            outing_episodic_text, outing_episodic_enabled,
            outing_logs_text, outing_logs_enabled
        ]
        
        # 各テキストボックスのchange
        for textbox in [outing_system_prompt_text, outing_permanent_text, outing_diary_text, outing_episodic_text, outing_logs_text]:
            textbox.change(
                fn=ui_handlers.handle_outing_update_total_chars,
                inputs=outing_total_inputs,
                outputs=[outing_total_char_count]
            )
        
        # 各チェックボックスのchange
        for checkbox in [outing_system_prompt_enabled, outing_permanent_enabled, outing_diary_enabled, outing_episodic_enabled, outing_logs_enabled]:
            checkbox.change(
                fn=ui_handlers.handle_outing_update_total_chars,
                inputs=outing_total_inputs,
                outputs=[outing_total_char_count]
            )
        
        # スライダー変更時にセクションを再読み込み
        outing_episode_days_slider.change(
            fn=ui_handlers.handle_outing_reload_episodic,
            inputs=[current_room_name, outing_episode_days_slider],
            outputs=[outing_episodic_text, outing_episodic_chars]
        )
        outing_log_count_slider.change(
            fn=ui_handlers.handle_outing_reload_logs,
            inputs=[current_room_name, outing_log_count_slider, outing_logs_include_timestamp, outing_logs_include_model],
            outputs=[outing_logs_text, outing_logs_chars]
        )
        
        # ログ表示オプション変更時に再読み込み
        for opt in [outing_logs_include_timestamp, outing_logs_include_model]:
            opt.change(
                fn=ui_handlers.handle_outing_reload_logs,
                inputs=[current_room_name, outing_log_count_slider, outing_logs_include_timestamp, outing_logs_include_model],
                outputs=[outing_logs_text, outing_logs_chars]
            )
        
        # セクション別リセット（🔄）
        outing_system_prompt_reload.click(
            fn=ui_handlers.handle_outing_reload_system_prompt,
            inputs=[current_room_name],
            outputs=[outing_system_prompt_text, outing_system_prompt_chars]
        )
        
        # 永続記憶と日記要約は同じ core_memory.txt から読み込むため、同じ関数を呼び出してそれぞれの出力を更新
        outing_permanent_reload.click(
            fn=lambda room: ui_handlers.handle_outing_reload_core_memory(room)[:2],
            inputs=[current_room_name],
            outputs=[outing_permanent_text, outing_permanent_chars]
        )
        outing_diary_reload.click(
            fn=lambda room: ui_handlers.handle_outing_reload_core_memory(room)[2:],
            inputs=[current_room_name],
            outputs=[outing_diary_text, outing_diary_chars]
        )
        
        outing_episodic_reload.click(
            fn=ui_handlers.handle_outing_reload_episodic,
            inputs=[current_room_name, outing_episode_days_slider],
            outputs=[outing_episodic_text, outing_episodic_chars]
        )
        outing_logs_reload.click(
            fn=ui_handlers.handle_outing_reload_logs,
            inputs=[current_room_name, outing_log_count_slider, outing_logs_include_timestamp, outing_logs_include_model],
            outputs=[outing_logs_text, outing_logs_chars]
        )

        # --- [Phase 2] 内的状態ダッシュボードの更新イベント ---
        refresh_internal_state_button.click(
            fn=ui_handlers.handle_refresh_internal_state,
            inputs=[current_room_name],
            outputs=[
                boredom_level_display, curiosity_level_display, 
                goal_achievement_level_display, devotion_level_display,
                dominant_drive_display, open_questions_display, 
                internal_state_last_update,
                user_emotion_history_plot
            ]
        )

        # Room Provider Events [Phase 3]
        room_provider_radio.change(
            fn=ui_handlers.handle_room_provider_change,
            inputs=[room_provider_radio],
            outputs=[room_google_settings_group, room_openai_settings_group]
        )

        # --- 外部接続設定に基づいてserver_nameを決定 ---
        allow_external = config_manager.CONFIG_GLOBAL.get("allow_external_connection", False)
        server_name_value = "0.0.0.0" if allow_external else "127.0.0.1"
        
        print("\n" + "="*60)
        print("アプリケーションを起動します...")
        print(f"起動後、以下のURLでアクセスしてください。")
        print(f"\n  【PCからアクセスする場合】")
        print(f"  http://127.0.0.1:7860")
        if allow_external:
            print(f"\n  【スマホからアクセスする場合（PCと同じWi-Fiに接続してください）】")
            print(f"  http://<お使いのPCのIPアドレス>:7860")
            print("  (IPアドレスが分からない場合は、PCのコマンドプロンプトやターミナルで")
            print("   `ipconfig` (Windows) または `ifconfig` (Mac/Linux) と入力して確認できます)")
        else:
            print(f"\n  ※外部接続は無効です。共通設定で有効化できます。")
        print("="*60 + "\n")
        
        demo.queue().launch(server_name=server_name_value, server_port=7860, share=False, allowed_paths=[".", constants.ROOMS_DIR, os.path.join(script_dir, "assets")], inbrowser=True)

except Exception as e:
    print("\n" + "X"*60); print("!!! [致命的エラー] アプリケーションの起動中に、予期せぬ例外が発生しました。"); print("X"*60); traceback.print_exc()

finally:
    utils.release_lock()
    if os.name == "nt": os.system("pause")
    else: input("続行するにはEnterキーを押してください...")

