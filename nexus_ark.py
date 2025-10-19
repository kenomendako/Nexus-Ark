# --- [ロギング設定の強制上書き] ---
import logging
import logging.config
import os
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
# --- [ここまでが新しいブロック] ---


# nexus_ark.py (v18: グループ会話FIX・最終版)

import os
import sys
# ▼▼▼【以下のimportを追加】▼▼▼
import shutil
# ▲▲▲【追加はここまで】▲▲▲
import utils
import json
import gradio as gr
import traceback
import pandas as pd
import config_manager, room_manager, alarm_manager, ui_handlers, constants

if not utils.acquire_lock():
    print("ロックが取得できなかったため、アプリケーションを終了します。")
    if os.name == "nt": os.system("pause")
    else: input("続行するにはEnterキーを押してください...")
    sys.exit(1)
os.environ["MEM0_TELEMETRY_ENABLED"] = "false"

try:
    config_manager.load_config()

    # ▼▼▼【ここから下のブロックをまるごと追加】▼▼▼
    # --- [初回起動シーケSン] ---
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
                config_manager.save_config("last_room", "Olivie")
                config_manager.load_config() # 設定を再読み込み
            except Exception as e:
                print(f"!!! [致命的エラー] サンプルペルソナのコピーに失敗しました: {e}")
        else:
            print(f"!!! [警告] サンプルペルソナのディレクトリが見つかりません: {sample_persona_path}")
    # --- [初回起動シーケンス ここまで] ---
    # ▲▲▲【追加はここまで】▲▲▲


    # ▼▼▼【ここから追加：テーマ適用ロジック】▼▼▼
    def get_active_theme() -> gr.themes.Base:
        """config.jsonから現在アクティブなテーマを読み込み、Gradioのテーマオブジェクトを生成する。"""
        theme_settings = config_manager.CONFIG_GLOBAL.get("theme_settings", {})
        active_theme_name = theme_settings.get("active_theme", "Soft")
        custom_themes = theme_settings.get("custom_themes", {})

        # プリセットテーマのマップ
        preset_themes = {
            "Default": gr.themes.Default,
            "Soft": gr.themes.Soft,
            "Monochrome": gr.themes.Monochrome,
            "Glass": gr.themes.Glass,
        }

        if active_theme_name in preset_themes:
            print(f"--- [テーマ] プリセットテーマ '{active_theme_name}' を適用します ---")
            return preset_themes[active_theme_name]()
        elif active_theme_name in custom_themes:
            print(f"--- [テーマ] カスタムテーマ '{active_theme_name}' を適用します ---")
            params = custom_themes[active_theme_name]

            # gr.themes.Default と .set() の引数を分離
            default_args = {}
            set_args = {}
            # gr.themes.Defaultがコンストラクタで受け付ける引数のリスト
            default_arg_keys = [
                "primary_hue", "secondary_hue", "neutral_hue",
                "text_size", "spacing_size", "radius_size", "font", "font_mono"
            ]

            for key, value in params.items():
                if key in default_arg_keys:
                    default_args[key] = value
                else:
                    set_args[key] = value

            # text_size, font などの値をGradioオブジェクトに変換
            if 'text_size' in default_args and isinstance(default_args['text_size'], dict):
                # Sizeオブジェクトのコンストラクタが受け付けるキーのみを渡す
                valid_keys = ["xxs", "xs", "sm", "md", "lg", "xl", "xxl"]
                size_params = {k: v for k, v in default_args['text_size'].items() if k in valid_keys}
                default_args['text_size'] = gr.themes.Size(**size_params)

            if 'font' in default_args and isinstance(default_args['font'], list):
                # GoogleFontとそれ以外（システムフォントなど）を区別しない
                # Gradioが内部で処理してくれる
                default_args['font'] = [gr.themes.GoogleFont(name) if name not in ['ui-sans-serif', 'system-ui', 'sans-serif'] else name for name in default_args['font']]

            # テーマオブジェクトを構築
            theme_obj = gr.themes.Default(**default_args)
            if set_args:
                theme_obj = theme_obj.set(**set_args)

            return theme_obj
        else:
            print(f"--- [テーマ警告] アクティブなテーマ '{active_theme_name}' が見つかりません。デフォルトの'Soft'テーマを適用します ---")
            return gr.themes.Soft()

    active_theme_object = get_active_theme()
    # ▲▲▲【追加ここまで】▲▲▲

    alarm_manager.load_alarms()
    alarm_manager.start_alarm_scheduler_thread()

    custom_css = """
    /* --- [Final Styles - v8: The Renaissance] --- */

    /* ルール1: <pre>タグ（外側のコンテナ）のスタイル */
    #chat_output_area .code_wrap pre {
        background-color: var(--background-fill-secondary);
        color: var(--text-color-secondary);
        border: 1px solid var(--border-color-primary); /* ← これが復活させる外枠です */
        padding: 10px;
        border-radius: 8px;
        font-family: var(--font-mono);
        font-size: 0.9em;
        white-space: pre-wrap !important;
        word-break: break-word;
    }

    /* ルール2: <code>タグ（内側のテキスト）のスタイルをリセット */
    #chat_output_area .code_wrap code {
        background: none !important;      /* 背景をリセット */
        border: none !important;          /* 枠線をリセット */
        padding: 0 !important;            /* パディングをリセット */
        background-image: none !important; /* 背景画像をリセット */
        white-space: inherit !important; /* 親の折り返し設定を強制的に継承する */
    }

    /* ゴミ箱アイコン（クリアボタン）を強制的に非表示にする */
    #chat_output_area button[aria-label="会話をクリア"] {
        display: none !important;
    }

    /* --- [Layout & Utility Styles] --- */
    #memory_json_editor_code .cm-editor, #core_memory_editor_code textarea {
        max-height: 400px !important; overflow-y: auto !important;
    }
    #notepad_editor_code textarea, #system_prompt_editor textarea {
        max-height: 400px !important; overflow-y: auto !important; box-sizing: border-box;
    }
    #memory_json_editor_code, #notepad_editor_code, #system_prompt_editor, #core_memory_editor_code {
        max-height: 410px; border: 1px solid var(--border-color-primary); border-radius: 5px; padding: 0;
    }
    #alarm_dataframe_display { border-radius: 8px !important; }
    #alarm_dataframe_display table { width: 100% !important; }
    #alarm_dataframe_display th, #alarm_dataframe_display td {
        text-align: left !important; padding: 4px 8px !important; white-space: normal !important; font-size: 0.95em;
    }
    #alarm_dataframe_display th:nth-child(1), #alarm_dataframe_display td:nth-child(1) {
        width: 50px !important; text-align: center !important;
    }
    #selection_feedback { font-size: 0.9em; color: var(--text-color-secondary); margin-top: 0px; margin-bottom: 5px; padding-left: 5px; }
    #token_count_display { text-align: right; font-size: 0.85em; color: var(--text-color-secondary); padding-right: 10px; margin-bottom: 5px; }
    #tpm_note_display { text-align: right; font-size: 0.75em; color: var(--text-color-secondary); padding-right: 10px; margin-bottom: -5px; margin-top: 0px; }
    #chat_container { position: relative; }
    #app_version_display {
        text-align: center;
        font-size: 0.85em;                  /* infoテキストに合わせてサイズを調整 */
        color: var(--text-color-secondary); /* 副次テキスト用のテーマカラーを維持 */
        margin-top: 8px;                    /* 上の要素との間隔を調整 */
        font-weight: 300;                   /* フォントを少し細くして控えめな印象に */
    }
    """
    custom_js = """
    function() {
        // This function is intentionally left blank.
    }
    """

    with gr.Blocks(theme=active_theme_object, css=custom_css, js=custom_js) as demo:
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
            config_manager.save_config("last_room", new_room_folder)
            if new_room_folder == "Default" and "Default" not in folder_names_on_startup:
                room_manager.ensure_room_files("Default")
                room_list_on_startup = room_manager.get_room_list_for_ui()

        # --- Stateの定義 ---
        world_data_state = gr.State({})
        current_room_name = gr.State(effective_initial_room)
        current_model_name = gr.State(config_manager.initial_model_global)
        current_api_key_name_state = gr.State(config_manager.initial_api_key_name_global)
        api_history_limit_state = gr.State(config_manager.initial_api_history_limit_option_global)
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
        redaction_rules_state = gr.State(lambda: config_manager.load_redaction_rules())
        selected_redaction_rule_state = gr.State(None) # 編集中のルールのインデックスを保持
        active_attachments_state = gr.State([]) # アクティブな添付ファイルパスのリストを保持
        selected_attachment_index_state = gr.State(None) # Dataframeで選択された行のインデックスを保持
        redaction_rule_color_state = gr.State("#62827e")
        imported_theme_params_state = gr.State({}) # インポートされたテーマの詳細設定を一時保持
        selected_knowledge_file_index_state = gr.State(None)
        with gr.Tabs():
            with gr.TabItem("チャット"):
                # --- [ここからが新しい3カラムレイアウト] ---
                with gr.Row(equal_height=False):
                    # --- 左カラム ---
                    with gr.Column(scale=2, min_width=300): # ← scale=1 を 2 に変更
                        room_dropdown = gr.Dropdown(choices=room_list_on_startup, value=effective_initial_room, label="ルームを選択", interactive=True)

                        with gr.Accordion("⚙️ 設定", open=False):
                            with gr.Tabs() as settings_tabs:
                                with gr.TabItem("共通") as common_settings_tab:
                                    with gr.Accordion("🔑 APIキー / Webhook管理", open=False):
                                        with gr.Accordion("Gemini APIキー", open=True):
                                            gemini_key_name_input = gr.Textbox(label="キーの名前（管理用の半角英数字）", placeholder="例: my_personal_key")
                                            gemini_key_value_input = gr.Textbox(label="APIキーの値", type="password")
                                            with gr.Row():
                                                save_gemini_key_button = gr.Button("Geminiキーを保存", variant="primary")
                                                delete_gemini_key_button = gr.Button("削除")
                                        with gr.Accordion("Pushover", open=False):
                                            pushover_user_key_input = gr.Textbox(label="Pushover User Key", type="password", value=lambda: config_manager.PUSHOVER_CONFIG.get("user_key"))
                                            pushover_app_token_input = gr.Textbox(label="Pushover App Token/Key", type="password", value=lambda: config_manager.PUSHOVER_CONFIG.get("app_token"))
                                            save_pushover_config_button = gr.Button("Pushover設定を保存", variant="primary")
                                        with gr.Accordion("Discord", open=False):
                                            discord_webhook_input = gr.Textbox(label="Discord Webhook URL", type="password", value=lambda: config_manager.NOTIFICATION_WEBHOOK_URL_GLOBAL or "")
                                            save_discord_webhook_button = gr.Button("Discord Webhookを保存", variant="primary")
                                        gr.Markdown("⚠️ **注意:** APIキーやWebhook URLはPC上の `config.json` ファイルに平文で保存されます。取り扱いには十分ご注意ください。")

                                    gr.Markdown("#### ⚙️ 一般設定")
                                    model_dropdown = gr.Dropdown(choices=config_manager.AVAILABLE_MODELS_GLOBAL, value=config_manager.initial_model_global, label="デフォルトAIモデル", interactive=True)
                                    api_key_dropdown = gr.Dropdown(choices=list(config_manager.GEMINI_API_KEYS.keys()), value=config_manager.initial_api_key_name_global, label="使用するGemini APIキー", interactive=True)
                                    api_history_limit_dropdown = gr.Dropdown(choices=list(constants.API_HISTORY_LIMIT_OPTIONS.values()), value=constants.API_HISTORY_LIMIT_OPTIONS.get(config_manager.initial_api_history_limit_option_global, "全ログ"), label="APIへの履歴送信", interactive=True)
                                    debug_mode_checkbox = gr.Checkbox(label="デバッグモードを有効化 (デバッグコンソールにシステムプロンプトを出力)", value=False, interactive=True)
                                    api_test_button = gr.Button("API接続をテスト", variant="secondary")

                                    gr.Markdown("---")
                                    with gr.Accordion("📢 通知サービス設定", open=False):
                                        notification_service_radio = gr.Radio(choices=["Discord", "Pushover"], label="アラーム通知に使用するサービス", value=config_manager.NOTIFICATION_SERVICE_GLOBAL.capitalize(), interactive=True)
                                        gr.Markdown("---")

                                    with gr.Accordion("💾 バックアップ設定", open=False):
                                        backup_rotation_count_number = gr.Number(
                                            label="バックアップの最大保存件数（世代数）",
                                            value=lambda: config_manager.CONFIG_GLOBAL.get("backup_rotation_count", 10),
                                            step=1,
                                            minimum=1,
                                            interactive=True,
                                            info="ファイル（ログ、記憶など）ごとに、ここで指定した数だけ最新のバックアップが保持されます。"
                                        )
                                        open_backup_folder_button = gr.Button("現在のルームのバックアップフォルダを開く", variant="secondary")
                                with gr.TabItem("個別") as individual_settings_tab:
                                    room_settings_info = gr.Markdown("ℹ️ *現在選択中のルーム「...」にのみ適用される設定です。*")
                                    save_room_settings_button = gr.Button("このルームの個別設定を保存", variant="primary")

                                    with gr.Accordion("🖼️ 情景描写設定", open=False):
                                        enable_scenery_system_checkbox = gr.Checkbox(
                                            label="🖼️ このルームで情景描写システムを有効にする",
                                            info="有効にすると、チャット画面右側に情景が表示され、AIもそれを認識します。",
                                            interactive=True
                                        )
                                    with gr.Accordion("📜 ストリーミング表示設定", open=False):
                                        enable_typewriter_effect_checkbox = gr.Checkbox(label="タイプライター風の逐次表示を有効化", interactive=True)
                                        streaming_speed_slider = gr.Slider(
                                            minimum=0.0, maximum=0.1, step=0.005,
                                            label="表示速度", info="値が小さいほど速く、大きいほどゆっくり表示されます。(0.0で最速)",
                                            interactive=True
                                        )
                                    with gr.Accordion("🎤 音声設定", open=False):
                                        gr.Markdown("チャットの発言を選択して、ここで設定した声で再生できます。")
                                        room_voice_dropdown = gr.Dropdown(label="声を選択（個別）", choices=list(config_manager.SUPPORTED_VOICES.values()), interactive=True)
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
                                            room_safety_harassment_dropdown = gr.Dropdown(choices=safety_choices, label="嫌がらせコンテンツ", interactive=True)
                                            room_safety_hate_speech_dropdown = gr.Dropdown(choices=safety_choices, label="ヘイトスピーチ", interactive=True)
                                        with gr.Row():
                                            room_safety_sexually_explicit_dropdown = gr.Dropdown(choices=safety_choices, label="性的コンテンツ", interactive=True)
                                            room_safety_dangerous_content_dropdown = gr.Dropdown(choices=safety_choices, label="危険なコンテンツ", interactive=True)
                                    with gr.Accordion("📡 APIコンテキスト設定", open=False):
                                        room_add_timestamp_checkbox = gr.Checkbox(label="メッセージにタイムスタンプを追加", interactive=True)
                                        room_send_current_time_checkbox = gr.Checkbox(
                                            label="現在時刻をAPIに送信",
                                            info="挨拶の自然さを向上させますが、特定の時間帯を演じたい場合はOFFにしてください。",
                                            interactive=True
                                        )
                                        room_send_thoughts_checkbox = gr.Checkbox(label="思考過程をAPIに送信", interactive=True)
                                        room_send_notepad_checkbox = gr.Checkbox(label="メモ帳の内容をAPIに送信", interactive=True)
                                        room_use_common_prompt_checkbox = gr.Checkbox(label="共通ツールプロンプトを送信", interactive=True)
                                        room_send_core_memory_checkbox = gr.Checkbox(label="コアメモリをAPIに送信", interactive=True)
                                        room_send_scenery_checkbox = gr.Checkbox(
                                            label="空間描写・設定をAPIに送信 (情景システムと連動)",
                                            interactive=False,
                                            visible=True
                                        )
                                        auto_memory_enabled_checkbox = gr.Checkbox(label="対話の自動記憶を有効化", interactive=True, visible=False)
                                with gr.TabItem("🎨 パレット") as theme_tab:
                                    theme_settings_state = gr.State({})
                                    theme_selector = gr.Dropdown(label="テーマを選択", interactive=True)
                                    gr.Markdown("---")
                                    gr.Markdown("#### プレビュー＆カスタマイズ\n選択したテーマをカスタマイズして、新しい名前で保存できます。")
                                    AVAILABLE_HUES = [
                                        "slate", "gray", "zinc", "neutral", "stone", "red", "orange", "amber",
                                        "yellow", "lime", "green", "emerald", "teal", "cyan", "sky", "blue",
                                        "indigo", "violet", "purple", "fuchsia", "pink", "rose"
                                    ]
                                    with gr.Row():
                                        primary_hue_picker = gr.Dropdown(choices=AVAILABLE_HUES, label="プライマリカラー系統", value="blue")
                                        secondary_hue_picker = gr.Dropdown(choices=AVAILABLE_HUES, label="セカンダリカラー系統", value="sky")
                                        neutral_hue_picker = gr.Dropdown(choices=AVAILABLE_HUES, label="ニュートラルカラー系統", value="slate")
                                    AVAILABLE_FONTS = sorted([
                                        "Alice", "Archivo", "Bitter", "Cabin", "Cormorant Garamond", "Crimson Pro",
                                        "Dm Sans", "Eczar", "Fira Sans", "Glegoo", "IBM Plex Mono", "Inconsolata", "Inter",
                                        "Jost", "Lato", "Libre Baskerville", "Libre Franklin", "Lora", "Merriweather",
                                        "Montserrat", "Mulish", "Noto Sans", "Noto Sans JP", "Open Sans", "Playfair Display",
                                        "Poppins", "Pt Sans", "Pt Serif", "Quattrocento", "Quicksand", "Raleway",
                                        "Roboto", "Roboto Mono", "Rubik", "Source Sans Pro", "Source Serif Pro",
                                        "Space Mono", "Spectral", "Sriracha", "Titillium Web", "Ubuntu", "Work Sans"
                                    ])
                                    font_dropdown = gr.Dropdown(choices=AVAILABLE_FONTS, label="メインフォント", value="Noto Sans JP", interactive=True)
                                    gr.Markdown("---")
                                    custom_theme_name_input = gr.Textbox(label="新しいテーマ名として保存", placeholder="例: My Cool Theme")
                                    save_theme_button = gr.Button("カスタムテーマとして保存", variant="secondary")
                                    apply_theme_button = gr.Button("このテーマを適用（要再起動）", variant="primary")
                                    gr.Markdown("⚠️ **注意:** テーマの変更を完全に反映するには、コンソールを閉じて `nexus_ark.py` を再実行する必要があります。")

                        with gr.Accordion("⏰ 時間管理", open=False):
                            with gr.Tabs():
                                with gr.TabItem("アラーム"):
                                    gr.Markdown("ℹ️ **操作方法**: リストから操作したいアラームの行を選択し、下のボタンで操作します。")
                                    alarm_dataframe = gr.Dataframe(headers=["状態", "時刻", "予定", "ルーム", "内容"], datatype=["bool", "str", "str", "str", "str"], interactive=True, row_count=(5, "dynamic"), col_count=5, wrap=True, elem_id="alarm_dataframe_display")
                                    selection_feedback_markdown = gr.Markdown("アラームを選択してください", elem_id="selection_feedback")
                                    with gr.Row():
                                        enable_button = gr.Button("✔️ 選択を有効化"); disable_button = gr.Button("❌ 選択を無効化"); delete_alarm_button = gr.Button("🗑️ 選択したアラームを削除", variant="stop")
                                    gr.Markdown("---"); gr.Markdown("#### 新規 / 更新")
                                    alarm_hour_dropdown = gr.Dropdown(choices=[str(i).zfill(2) for i in range(24)], label="時", value="08")
                                    alarm_minute_dropdown = gr.Dropdown(choices=[str(i).zfill(2) for i in range(60)], label="分", value="00")
                                    alarm_room_dropdown = gr.Dropdown(choices=room_list_on_startup, value=effective_initial_room, label="ルーム")
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
                                    timer_room_dropdown = gr.Dropdown(choices=room_list_on_startup, value=effective_initial_room, label="通知ルーム", interactive=True); timer_status_output = gr.Textbox(label="タイマー設定状況", interactive=False, placeholder="ここに設定内容が表示されます。"); timer_submit_button = gr.Button("タイマー開始", variant="primary")

                        with gr.Accordion("🧑‍🤝‍🧑 グループ会話", open=False):
                            session_status_display = gr.Markdown("現在、1対1の会話モードです。")
                            participant_checkbox_group = gr.CheckboxGroup(
                                label="会話に招待するルーム",
                                choices=sorted([c for c in room_list_on_startup if c != effective_initial_room]),
                                interactive=True
                            )
                            with gr.Row():
                                start_session_button = gr.Button("このメンバーで会話を開始 / 更新", variant="primary")
                                end_session_button = gr.Button("会話を終了 (1対1に戻る)", variant="secondary")

                        with gr.Accordion("🗨️ チャットルームの作成・管理", open=False) as manage_room_accordion:
                            with gr.Tabs() as room_management_tabs:
                                with gr.TabItem("作成") as create_room_tab:
                                    new_room_name = gr.Textbox(label="ルーム名（必須）", info="UIやグループ会話で表示される名前です。フォルダ名は自動で生成されます。")
                                    new_user_display_name = gr.Textbox(label="あなたの表示名（任意）", placeholder="デフォルト: ユーザー")
                                    new_agent_display_name = gr.Textbox(label="Agentの表示名（任意）", placeholder="AIのデフォルト表示名。未設定の場合はルーム名が使われます。")
                                    new_room_description = gr.Textbox(label="ルームの説明（任意）", lines=3, placeholder="このルームがどのような場所かをメモしておけます。")
                                    initial_system_prompt = gr.Textbox(label="初期システムプロンプト（任意）", lines=5, placeholder="このルームの基本的なルールやAIの役割などを設定します。")
                                    create_room_button = gr.Button("ルームを作成", variant="primary")
                                
                                with gr.TabItem("管理") as manage_room_tab:
                                    manage_room_selector = gr.Dropdown(label="管理するルームを選択", choices=room_list_on_startup, interactive=True)
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
                                        gr.Markdown("### ChatGPTデータインポート\n`conversations.json`ファイルをアップロードして、過去の対話をNexus Arkにインポートします。")
                                        chatgpt_import_file = gr.File(label="`conversations.json` をアップロード", file_types=[".json"])
                                        with gr.Column(visible=False) as chatgpt_import_form:
                                            chatgpt_thread_dropdown = gr.Dropdown(label="インポートする会話スレッドを選択", interactive=True)
                                            chatgpt_room_name_textbox = gr.Textbox(label="新しいルーム名", interactive=True)
                                            chatgpt_user_name_textbox = gr.Textbox(label="あなたの表示名（ルーム内）", value="ユーザー", interactive=True)
                                            chatgpt_import_button = gr.Button("この会話をNexus Arkにインポートする", variant="primary")
                                    with gr.Accordion("🟠 Claude (公式)", open=False):
                                        gr.Markdown("### Claudeデータインポート\n`conversations.json`ファイルをアップロードして、過去の対話をNexus Arkにインポートします。")
                                        claude_import_file = gr.File(label="`conversations.json` をアップロード", file_types=[".json"])
                                        with gr.Column(visible=False) as claude_import_form:
                                            claude_thread_dropdown = gr.Dropdown(label="インポートする会話スレッドを選択", interactive=True)
                                            claude_room_name_textbox = gr.Textbox(label="新しいルーム名", interactive=True)
                                            claude_user_name_textbox = gr.Textbox(label="あなたの表示名（ルーム内）", value="ユーザー", interactive=True)
                                            claude_import_button = gr.Button("この会話をNexus Arkにインポートする", variant="primary")

                                    with gr.Accordion("📄 その他テキスト/JSON", open=False):
                                        gr.Markdown(
                                            "### 汎用インポーター\n"
                                            "ChatGPT Exporter形式のファイルや、任意の話者ヘッダーを持つテキストログをインポートします。"
                                        )
                                        generic_import_file = gr.File(label="JSON, MD, TXT ファイルをアップロード", file_types=[".json", ".md", ".txt"])
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

                        gr.Markdown(f"Nexus Ark {constants.APP_VERSION} (Beta)", elem_id="app_version_display")

                    # --- 中央カラム ---
                    with gr.Column(scale=6): # ← scale=3 を 6 に変更
                        # ▼▼▼【ここから下のブロックをまるごと追加】▼▼▼
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
                        # ▲▲▲【追加ここまで】▲▲▲

                        chatbot_display = gr.Chatbot(
                            height=490, # ← height を 490 に変更
                            elem_id="chat_output_area",
                            show_copy_button=True,
                            show_label=False,
                            render_markdown=True,
                            group_consecutive_messages=False,
                            editable="all"  # ← ★★★ この行を追加 ★★★
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

                    # --- 右カラム ---
                    with gr.Column(scale=3, min_width=300): # ← scale=1.5 を 3 に変更
                        with gr.Accordion("🖼️ プロフィール・情景", open=True, elem_id="profile_scenery_accordion") as profile_scenery_accordion:
                            # --- プロフィール画像セクション ---
                            profile_image_display = gr.Image(
                                height=200, interactive=False, show_label=False, elem_id="profile_image_display"
                            )
                            with gr.Accordion("プロフィール画像を変更", open=False) as profile_image_accordion:
                                staged_image_state = gr.State()
                                image_upload_button = gr.UploadButton("新しい画像をアップロード", file_types=["image"])
                                cropper_image_preview = gr.ImageEditor(
                                    sources=["upload"], type="pil", interactive=True, show_label=False,
                                    visible=False, transforms=["crop"], brush=None, eraser=None,
                                )
                                save_cropped_image_button = gr.Button("この範囲で保存", visible=False)

                            # --- 情景ビジュアルセクション ---
                            scenery_image_display = gr.Image(label="現在の情景ビジュアル", interactive=False, height=200, show_label=False)
                            current_scenery_display = gr.Textbox( # ← ここに移動し、labelを削除
                                interactive=False, lines=4, max_lines=10, show_label=False,
                                placeholder="現在の情景が表示されます..."
                            )

                            # --- 移動メニュー ---
                            location_dropdown = gr.Dropdown(label="現在地 / 移動先を選択", interactive=True) # ← label を変更

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
                                            interactive=True
                                        )
                                        fixed_time_of_day_dropdown = gr.Dropdown(
                                            label="時間帯を選択",
                                            choices=["朝", "昼", "夕方", "夜"],
                                            interactive=True
                                        )
                                    # --- [ここからが修正箇所] ---
                                    # ボタンを fixed_time_controls の外に移動し、常に表示されるようにする
                                    save_time_settings_button = gr.Button("このルームの時間設定を保存", variant="secondary")
                                    # --- [修正はここまで] ---
                                
                                scenery_style_radio = gr.Dropdown(
                                    choices=["写真風 (デフォルト)", "イラスト風", "アニメ風", "水彩画風"],
                                    label="画風を選択", value="写真風 (デフォルト)", interactive=True
                                )
                                generate_scenery_image_button = gr.Button("情景画像を生成 / 更新", variant="secondary")
                                refresh_scenery_button = gr.Button("情景テキストを更新", variant="secondary")

                # --- [3カラムレイアウトはここまで] ---

            with gr.TabItem(" 記憶・メモ・指示"):
                gr.Markdown("##  記憶・メモ・指示\nルームの根幹をなす設定ファイルを、ここで直接編集できます。")
                with gr.Tabs():
                    with gr.TabItem("システムプロンプト"):
                        system_prompt_editor = gr.Textbox(label="システムプロンプト (System Prompt)", interactive=True, elem_id="system_prompt_editor", lines=20, autoscroll=True)
                        with gr.Row():
                            save_prompt_button = gr.Button("プロンプトを保存", variant="secondary")
                            reload_prompt_button = gr.Button("再読込", variant="secondary")
                    with gr.TabItem("記憶"):
                        memory_txt_editor = gr.Textbox(
                            label="主観的記憶（日記） - memory_main.txt",
                            interactive=True,
                            elem_id="memory_txt_editor_code",
                            lines=20,
                            autoscroll=True
                        )
                        with gr.Row():
                            save_memory_button = gr.Button("主観的記憶を保存", variant="secondary")
                            reload_memory_button = gr.Button("再読込", variant="secondary")
                            core_memory_update_button = gr.Button("コアメモリを更新", variant="primary")

                        # ▼▼▼ ここからが修正・追加するUIブロック ▼▼▼
                        with gr.Accordion("📝 古い日記をアーカイブする", open=False) as memory_archive_accordion:
                            # ▼▼▼ 以下のgr.Markdownとgr.Dropdownのテキストを変更 ▼▼▼
                            gr.Markdown(
                                "指定した日付**まで**の日記を要約し、別ファイルに保存して、このメインファイルから削除します。\n"
                                "**⚠️注意:** この操作は`memory_main.txt`を直接変更します（処理前にバックアップは作成されます）。"
                            )
                            archive_date_dropdown = gr.Dropdown(label="この日付までをアーカイブ", interactive=True)
                            # ▲▲▲ 変更ここまで ▲▲▲
                            archive_confirm_state = gr.Textbox(visible=False) # 確認ダイアログ用
                            archive_memory_button = gr.Button("アーカイブを実行", variant="stop")
                        # ▲▲▲ 修正・追加ブロックここまで ▲▲▲

                    with gr.TabItem("コアメモリ"):
                        core_memory_editor = gr.Textbox(
                            label="コアメモリ (core_memory.txt) - AIの自己同一性の核",
                            interactive=True,
                            elem_id="core_memory_editor_code",
                            lines=20,
                            autoscroll=True
                        )
                        with gr.Row():
                            save_core_memory_button = gr.Button("コアメモリを保存", variant="secondary")
                            reload_core_memory_button = gr.Button("再読込", variant="secondary")

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
                    with gr.TabItem("メモ帳"):
                        notepad_editor = gr.Textbox(label="メモ帳の内容", interactive=True, elem_id="notepad_editor_code", lines=20, autoscroll=True)
                        with gr.Row():
                            save_notepad_button = gr.Button("メモ帳を保存", variant="secondary")
                            reload_notepad_button = gr.Button("再読込", variant="secondary")
                            clear_notepad_button = gr.Button("メモ帳を全削除", variant="stop")

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
                                area_selector = gr.Dropdown(label="エリア (`##`)", interactive=True)
                                place_selector = gr.Dropdown(label="場所 (`###`)", interactive=True)
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
            room_add_timestamp_checkbox, room_send_current_time_checkbox,room_send_thoughts_checkbox, room_send_notepad_checkbox,
            room_use_common_prompt_checkbox, room_send_core_memory_checkbox,
            enable_scenery_system_checkbox,
            auto_memory_enabled_checkbox,
        ]
        context_token_calc_inputs = [current_room_name, current_api_key_name_state, api_history_limit_state] + context_checkboxes

        attachment_change_token_calc_inputs = [
            current_room_name,
            current_api_key_name_state,
            api_history_limit_state,
            chat_input_multimodal,
            active_attachments_state,
        ] + context_checkboxes

        initial_load_chat_outputs = [
            current_room_name, chatbot_display, current_log_map_state,
            chat_input_multimodal,
            profile_image_display,
            memory_txt_editor, notepad_editor, system_prompt_editor,
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
            # --- context_checkboxes の中身を展開してここに追加 ---
            room_add_timestamp_checkbox, room_send_current_time_checkbox, room_send_thoughts_checkbox, room_send_notepad_checkbox,
            room_use_common_prompt_checkbox, room_send_core_memory_checkbox,
            room_send_scenery_checkbox, # 連動される非表示チェックボックス
            auto_memory_enabled_checkbox,
            # --- ここまでが context_checkboxes ---
            room_settings_info,
            scenery_image_display,
            # --- 新しい部品をリストの末尾に追加 ---
            enable_scenery_system_checkbox, # マスタースイッチ
            profile_scenery_accordion # gr.update() から元のコンポーネント名に戻す
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
            onboarding_guide # <<<<<<< この行を追加
        ]

        world_builder_outputs = [world_data_state, area_selector, world_settings_raw_editor, place_selector]
        session_management_outputs = [active_participants_state, session_status_display, participant_checkbox_group]

        all_room_change_outputs = initial_load_chat_outputs + world_builder_outputs + session_management_outputs + [
            redaction_rules_df,
            archive_date_dropdown,
            time_mode_radio,
            fixed_season_dropdown,
            fixed_time_of_day_dropdown,
            fixed_time_controls,
            attachments_df,
            active_attachments_display 
        ]

        demo.load(
            fn=ui_handlers.handle_initial_load,
            inputs=[gr.State(effective_initial_room), current_api_key_name_state],
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

        # ▼▼▼ chat_inputs のリスト定義から streaming_speed_slider を削除し、代わりに関連チェックボックスを追加 ▼▼▼
        chat_inputs = [
            chat_input_multimodal,
            current_room_name,
            current_api_key_name_state,
            api_history_limit_state,
            debug_mode_checkbox,
            debug_console_state,
            active_participants_state,
            selected_attachment_index_state,
            model_dropdown,
            enable_typewriter_effect_checkbox,
            streaming_speed_slider,
            current_scenery_display,
        ]

# ▼▼▼ rerun_inputs のリスト定義から streaming_speed_slider を削除し、代わりに関連チェックボックスを追加 ▼▼▼
        rerun_inputs = [
            selected_message_state,
            current_room_name,
            current_api_key_name_state,
            api_history_limit_state,
            debug_mode_checkbox,
            debug_console_state,
            active_participants_state,
            active_attachments_state,
            model_dropdown,
            enable_typewriter_effect_checkbox,
            streaming_speed_slider,
            current_scenery_display,
        ]

        # 新規送信と再生成で、UI更新の対象（outputs）を完全に一致させる
        unified_streaming_outputs = [
            chatbot_display, current_log_map_state, chat_input_multimodal,
            token_count_display,
            location_dropdown, # ← current_location_display の代わりにこれを追加
            current_scenery_display,
            alarm_dataframe_original_data, alarm_dataframe, scenery_image_display,
            debug_console_state, debug_console_output,
            stop_button, chat_reload_button,
            action_button_group
        ]

        rerun_event = rerun_button.click(
            fn=ui_handlers.handle_rerun_button_click,
            inputs=rerun_inputs,
            outputs=unified_streaming_outputs
        )

        # 戻り値の最後に token_count_display と current_room_name を追加
        all_room_change_outputs.extend([token_count_display, current_room_name])

        # 【v5: 堅牢化】ルーム変更イベントを2段階に分離
        # 1. まず、選択されたルーム名をconfig.jsonに即時保存するだけの小さな処理を実行
        room_dropdown.change(
            fn=lambda room_name: config_manager.save_config("last_room", room_name),
            inputs=[room_dropdown],
            outputs=None
        # 2. その後(.then)、UI全体を更新する重い処理を実行
        ).then(
            fn=ui_handlers.handle_room_change_for_all_tabs,
            inputs=[room_dropdown, api_key_dropdown],
            outputs=all_room_change_outputs
        )

        chat_reload_button.click(
            fn=ui_handlers.reload_chat_log,
            inputs=[current_room_name, api_history_limit_state, room_add_timestamp_checkbox, screenshot_mode_checkbox, redaction_rules_state],
            outputs=[chatbot_display, current_log_map_state]
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
        # --- [ここから修正] ---
        chatbot_display.edit(
            fn=ui_handlers.handle_chatbot_edit,
            inputs=[
                chatbot_display,  # ★★★ この行を追加 ★★★
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
            outputs=[message_delete_confirmed_state], # 出力先を新しい名前に変更
            js="() => confirm('本当にこのメッセージを削除しますか？この操作は元に戻せません。')"
        )
        message_delete_confirmed_state.change( # 監視対象を新しい名前に変更
            fn=ui_handlers.handle_delete_button_click,
            inputs=[message_delete_confirmed_state, selected_message_state, current_room_name, api_history_limit_state], # 引数を正しいものに
            outputs=[chatbot_display, current_log_map_state, selected_message_state, action_button_group, message_delete_confirmed_state]
        )

        api_history_limit_dropdown.change(
            fn=ui_handlers.update_api_history_limit_state_and_reload_chat,
            inputs=[api_history_limit_dropdown, current_room_name, room_add_timestamp_checkbox, screenshot_mode_checkbox, redaction_rules_state],
            outputs=[api_history_limit_state, chatbot_display, current_log_map_state]
        ).then(
            fn=ui_handlers.handle_context_settings_change,
            inputs=context_token_calc_inputs,
            outputs=token_count_display
        )

        create_room_button.click(
            fn=ui_handlers.handle_create_room,
            inputs=[new_room_name, new_user_display_name, new_agent_display_name, new_room_description, initial_system_prompt],
            outputs=[
                room_dropdown,
                manage_room_selector,
                alarm_room_dropdown,
                timer_room_dropdown,
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
            inputs=[manage_folder_name_display, room_delete_confirmed_state, api_key_dropdown],
            outputs=all_room_change_outputs + [room_delete_confirmed_state] # ★★★ ここにトリガー自身を追加 ★★★
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
        )
        clear_rule_form_button.click(
            fn=lambda: (None, "", "", "#62827e", "#62827e"),
            outputs=[selected_redaction_rule_state, redaction_find_textbox, redaction_replace_textbox, redaction_color_picker, redaction_rule_color_state]
        )
        delete_rule_button.click(
            fn=ui_handlers.handle_delete_redaction_rule,
            inputs=[redaction_rules_state, selected_redaction_rule_state],
            outputs=[redaction_rules_df, redaction_rules_state, selected_redaction_rule_state, redaction_find_textbox, redaction_replace_textbox, redaction_color_picker]
        )
        screenshot_mode_checkbox.change(
            fn=ui_handlers.reload_chat_log,
            inputs=[current_room_name, api_history_limit_state, room_add_timestamp_checkbox, screenshot_mode_checkbox, redaction_rules_state],
            outputs=[chatbot_display, current_log_map_state]
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
        save_room_settings_button.click(
            fn=ui_handlers.handle_save_room_settings,
            inputs=[
                current_room_name, room_voice_dropdown, room_voice_style_prompt_textbox
            ] + gen_settings_inputs + [
                enable_typewriter_effect_checkbox, # ← enable_typewriter_effect と streaming_speed の順番を変更
                streaming_speed_slider,
            ] + [
                # ▼▼▼ context_checkboxes に合わせて inputs を修正 ▼▼▼
                room_add_timestamp_checkbox, room_send_current_time_checkbox, room_send_thoughts_checkbox, room_send_notepad_checkbox,
                room_use_common_prompt_checkbox, room_send_core_memory_checkbox,
                enable_scenery_system_checkbox, # 新しいマスタースイッチを渡す
                auto_memory_enabled_checkbox
            ],
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
        for checkbox in context_checkboxes: checkbox.change(fn=ui_handlers.handle_context_settings_change, inputs=context_token_calc_inputs, outputs=token_count_display)
        # streaming_speed_slider.change(fn=ui_handlers.handle_streaming_speed_change, inputs=[streaming_speed_slider], outputs=None)
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
        # ▼▼▼【送信と停止のイベント定義を全面的に更新】▼▼▼
        # chat_submit_outputs の定義を削除し、代わりに unified_streaming_outputs を使用
        submit_event = chat_input_multimodal.submit(
            fn=ui_handlers.handle_message_submission,
            inputs=chat_inputs,
            outputs=unified_streaming_outputs # ここを変更
        )

        stop_button.click(
            fn=ui_handlers.handle_stop_button_click,
            inputs=[current_room_name, api_history_limit_state, room_add_timestamp_checkbox, screenshot_mode_checkbox, redaction_rules_state],
            outputs=[stop_button, chat_reload_button, chatbot_display, current_log_map_state],
            cancels=[submit_event, rerun_event]
        )
        # ▲▲▲【修正ここまで】▲▲▲

        # トークン計算イベント（入力内容が変更されるたびに実行）
        token_calc_on_input_inputs = [
            current_room_name, current_api_key_name_state, api_history_limit_state,
            chat_input_multimodal # 変更
        ] + context_checkboxes
        chat_input_multimodal.change(
            fn=ui_handlers.update_token_count_on_input,
            inputs=token_calc_on_input_inputs,
            outputs=token_count_display,
            show_progress=False
        )

        refresh_scenery_button.click(fn=ui_handlers.handle_scenery_refresh, inputs=[current_room_name, api_key_dropdown], outputs=[location_dropdown, current_scenery_display, scenery_image_display])
        location_dropdown.change(
            fn=ui_handlers.handle_location_change,
            inputs=[current_room_name, location_dropdown, api_key_dropdown],
            outputs=[location_dropdown, current_scenery_display, scenery_image_display]
        )
        cancel_selection_button.click(fn=lambda: (None, gr.update(visible=False)), inputs=None, outputs=[selected_message_state, action_button_group])

        save_prompt_button.click(fn=ui_handlers.handle_save_system_prompt, inputs=[current_room_name, system_prompt_editor], outputs=None)
        reload_prompt_button.click(fn=ui_handlers.handle_reload_system_prompt, inputs=[current_room_name], outputs=[system_prompt_editor])
        save_memory_button.click(fn=ui_handlers.handle_save_memory_click, inputs=[current_room_name, memory_txt_editor], outputs=[memory_txt_editor])
        reload_memory_button.click(fn=ui_handlers.handle_reload_memory, inputs=[current_room_name], outputs=[memory_txt_editor, archive_date_dropdown])
        save_notepad_button.click(fn=ui_handlers.handle_save_notepad_click, inputs=[current_room_name, notepad_editor], outputs=[notepad_editor])
        reload_notepad_button.click(fn=ui_handlers.handle_reload_notepad, inputs=[current_room_name], outputs=[notepad_editor])
        clear_notepad_button.click(fn=ui_handlers.handle_clear_notepad_click, inputs=[current_room_name], outputs=[notepad_editor])
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
        timer_submit_button.click(fn=ui_handlers.handle_timer_submission, inputs=[timer_type_radio, timer_duration_number, pomo_work_number, pomo_break_number, pomo_cycles_number, timer_room_dropdown, timer_work_theme_input, timer_break_theme_input, api_key_dropdown, normal_timer_theme_input], outputs=[timer_status_output])

        notification_service_radio.change(fn=ui_handlers.handle_notification_service_change, inputs=[notification_service_radio], outputs=[])
        save_gemini_key_button.click(
            fn=ui_handlers.handle_save_gemini_key,
            # ▼▼▼【inputs に current_room_name を再度追加】▼▼▼
            inputs=[gemini_key_name_input, gemini_key_value_input, current_room_name],
            # ▼▼▼【outputs を UI全体を更新するリストに変更】▼▼▼
            outputs=[
                api_key_dropdown, onboarding_guide, chat_input_multimodal
            ] + all_room_change_outputs
            # ▲▲▲【変更ここまで】▲▲▲
        )
        delete_gemini_key_button.click(fn=ui_handlers.handle_delete_gemini_key, inputs=[gemini_key_name_input], outputs=[api_key_dropdown])
        save_pushover_config_button.click(fn=ui_handlers.handle_save_pushover_config, inputs=[pushover_user_key_input, pushover_app_token_input], outputs=[])
        save_discord_webhook_button.click(fn=ui_handlers.handle_save_discord_webhook, inputs=[discord_webhook_input], outputs=[])
        # ▼▼▼ ここからが修正の核心 ▼▼▼

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

        # ▲▲▲ ここまで ▲▲▲
        core_memory_update_button.click(
            fn=ui_handlers.handle_core_memory_update_click,
            inputs=[current_room_name, current_api_key_name_state],
            outputs=[core_memory_editor] # <-- None から変更
        )

        # --- 新規追加: コアメモリ用イベント ---
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

        generate_scenery_image_button.click(fn=ui_handlers.handle_generate_or_regenerate_scenery_image, inputs=[current_room_name, api_key_dropdown, scenery_style_radio], outputs=[scenery_image_display])
        audio_player.stop(fn=lambda: gr.update(visible=False), inputs=None, outputs=[audio_player])
        # ▼▼▼【ここからが追加する行】▼▼▼
        audio_player.pause(fn=lambda: gr.update(visible=False), inputs=None, outputs=[audio_player])
        # ▲▲▲【追加はここまで】▲▲▲

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

        # --- プロフィール画像編集機能のイベント接続 ---

        # 1. アップロードボタンに画像が渡されたら、編集プレビューを表示する
        image_upload_button.upload(
            fn=ui_handlers.handle_staging_image_upload,
            inputs=[image_upload_button],
            outputs=[staged_image_state, cropper_image_preview, save_cropped_image_button, profile_image_accordion]
        )

        # 2. 編集プレビューで範囲が選択され、「保存」ボタンが押されたら、最終処理を呼び出す
        save_cropped_image_button.click(
            fn=ui_handlers.handle_save_cropped_image,
            inputs=[current_room_name, staged_image_state, cropper_image_preview],
            outputs=[profile_image_display, cropper_image_preview, save_cropped_image_button]
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

        # --- ChatGPT Importer Event Handlers ---
        chatgpt_import_file.upload(
            fn=ui_handlers.handle_chatgpt_file_upload,
            inputs=[chatgpt_import_file],
            outputs=[chatgpt_thread_dropdown, chatgpt_import_form, chatgpt_thread_choices_state]
        )

        chatgpt_thread_dropdown.select(
            fn=ui_handlers.handle_chatgpt_thread_selection,
            inputs=[chatgpt_thread_choices_state],
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

        claude_thread_dropdown.select(
            fn=ui_handlers.handle_claude_thread_selection,
            inputs=[claude_thread_choices_state],
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

        # --- Theme Management Event Handlers ---
        theme_tab.select(
            fn=ui_handlers.handle_theme_tab_load,
            inputs=None,
            outputs=[theme_settings_state, theme_selector]
        )

        theme_selector.change(
            fn=ui_handlers.handle_theme_selection,
            inputs=[theme_settings_state, theme_selector],
            outputs=[primary_hue_picker, secondary_hue_picker, neutral_hue_picker, font_dropdown]
        )

        save_theme_button.click(
            fn=ui_handlers.handle_save_custom_theme,
            inputs=[
                theme_settings_state, custom_theme_name_input,
                primary_hue_picker, secondary_hue_picker, neutral_hue_picker, font_dropdown
            ],
            outputs=[theme_settings_state, theme_selector, custom_theme_name_input]
        )

        apply_theme_button.click(
            fn=ui_handlers.handle_apply_theme,
            inputs=[theme_settings_state, theme_selector],
            outputs=None # ポップアップ通知のみ
        )

        # ▼▼▼【ここから下のブロックをまるごと追加】▼▼▼
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
        # ▲▲▲【追加はここまで】▲▲▲

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

        # ▼▼▼【ここから下のブロックをイベントハンドラ定義の末尾に追加】▼▼▼
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

        play_audio_event = play_audio_button.click(
            fn=ui_handlers.handle_play_audio_button_click,
            inputs=[selected_message_state, current_room_name, api_key_dropdown],
            outputs=[audio_player, play_audio_button, rerun_button]
        )
        play_audio_event.failure(fn=ui_handlers._reset_play_audio_on_failure, inputs=None, outputs=[audio_player, play_audio_button, rerun_button])

        print("\n" + "="*60); print("アプリケーションを起動します..."); print(f"起動後、以下のURLでアクセスしてください。"); print(f"\n  【PCからアクセスする場合】"); print(f"  http://127.0.0.1:7860"); print(f"\n  【スマホからアクセスする場合（PCと同じWi-Fiに接続してください）】"); print(f"  http://<お使いのPCのIPアドレス>:7860"); print("  (IPアドレスが分からない場合は、PCのコマンドプロモートやターミナルで"); print("   `ipconfig` (Windows) または `ifconfig` (Mac/Linux) と入力して確認できます)"); print("="*60 + "\n")
        demo.queue().launch(server_name="0.0.0.0", server_port=7860, share=False, allowed_paths=["."], inbrowser=True)

except Exception as e:
    print("\n" + "X"*60); print("!!! [致命的エラー] アプリケーションの起動中に、予期せぬ例外が発生しました。"); print("X"*60); traceback.print_exc()
finally:
    utils.release_lock()
    if os.name == "nt": os.system("pause")
    else: input("続行するにはEnterキーを押してください...")

