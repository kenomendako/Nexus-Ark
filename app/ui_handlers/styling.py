"""ui_handlers のうち「テーマ/背景のスタイルCSS生成」共有モジュール。

ルーム切替・初期ロード・アイテムタブ・テーマ設定など複数ドメインから参照される
生成系ヘルパーを集約する。ui_handlers パッケージから再エクスポートされる。
"""

import os
import gemini_api, config_manager, alarm_manager, room_manager, utils, constants, chatgpt_importer, claude_importer, generic_importer
import utils


def hex_to_rgba(hex_code, alpha):
    """HexカラーコードをRGBA文字列に変換するヘルパー関数"""
    if not hex_code or not str(hex_code).startswith("#"):
        return hex_code
    hex_code = hex_code.lstrip('#')
    if len(hex_code) == 3: hex_code = "".join([c*2 for c in hex_code])
    if len(hex_code) != 6: return f"#{hex_code}"
    try:
        rgb = tuple(int(hex_code[i:i+2], 16) for i in (0, 2, 4))
        return f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, {alpha})"
    except:
        return f"#{hex_code}"


def _resolve_background_image(room_name: str, settings: dict) -> str:
    """背景画像ソースモードに基づいて、使用すべき画像パスを決定する"""
    mode = settings.get("theme_bg_src_mode", "画像を指定 (Manual)")
    # print(f"DEBUG: Resolving background for {room_name}, Mode: {mode}, Repr: {repr(mode)}")

    if mode == "現在地と連動 (Sync)":
        # [NEW] 一時的現在地がアクティブな場合はそちらを優先
        from agent.temporary_location_manager import TemporaryLocationManager
        tlm = TemporaryLocationManager()
        if tlm.is_active(room_name):
            data = tlm.get_current_data(room_name)
            temp_image_path = data.get("image_path")
            if temp_image_path and os.path.exists(temp_image_path):
                return temp_image_path

        # 現在地（仮想現在地）から画像を探す
        location_id = utils.get_current_location(room_name)
        if location_id:
            season_en, time_of_day_en = utils._get_current_time_context(room_name)
            scenery_path = utils.find_scenery_image(room_name, location_id, season_en=season_en, time_of_day_en=time_of_day_en)
            if scenery_path:
                return scenery_path
        # 見つからない場合はNone（背景なし）
        return None
    else:
        # Manualモード: 設定された画像パスを使用
        return settings.get("theme_bg_image", None)


def handle_refresh_background_css(room_name: str) -> str:
    """[v21] 現在地連動背景: 画像生成/登録後にstyle_injectorを更新するためのハンドラ"""
    effective_settings = config_manager.get_effective_settings(room_name)
    return _generate_style_from_settings(room_name, effective_settings)


def _generate_style_from_settings(room_name: str, settings: dict) -> str:
    """設定辞書からCSSを生成するヘルパー（背景画像解決込み）"""
    is_sync = (settings.get("theme_bg_src_mode") == "現在地と連動 (Sync)")

    def get_bg_val(key_manual, key_sync, default):
        return settings.get(key_sync if is_sync else key_manual, default)

    return generate_room_style_css(
        settings.get("room_theme_enabled", False),
        settings.get("font_size", 15),
        settings.get("line_height", 1.6),
        settings.get("chat_style", "Chat (Default)"),
        settings.get("theme_primary", None),
        settings.get("theme_secondary", None),
        settings.get("theme_background", None),
        settings.get("theme_text", None),
        settings.get("theme_accent_soft", None),
        settings.get("theme_input_bg", None),
        settings.get("theme_input_border", None),
        settings.get("theme_code_bg", None),
        settings.get("theme_subdued_text", None),
        settings.get("theme_button_bg", None),
        settings.get("theme_button_hover", None),
        settings.get("theme_stop_button_bg", None),
        settings.get("theme_stop_button_hover", None),
        settings.get("theme_checkbox_off", None),
        settings.get("theme_table_bg", None),
        settings.get("theme_radio_label", None),
        settings.get("theme_dropdown_list_bg", None),
        settings.get("theme_ui_opacity", 0.9), # Default 0.9
        _resolve_background_image(room_name, settings),
        get_bg_val("theme_bg_opacity", "theme_bg_sync_opacity", 0.4),
        get_bg_val("theme_bg_blur", "theme_bg_sync_blur", 0),
        get_bg_val("theme_bg_size", "theme_bg_sync_size", "cover"),
        get_bg_val("theme_bg_position", "theme_bg_sync_position", "center"),
        get_bg_val("theme_bg_repeat", "theme_bg_sync_repeat", "no-repeat"),
        get_bg_val("theme_bg_custom_width", "theme_bg_sync_custom_width", "300px"),
        get_bg_val("theme_bg_radius", "theme_bg_sync_radius", 0),
        get_bg_val("theme_bg_mask_blur", "theme_bg_sync_mask_blur", 0),
        get_bg_val("theme_bg_front_layer", "theme_bg_sync_front_layer", False)
    )


def generate_room_style_css(enabled=True, font_size=15, line_height=1.6, chat_style="Chat (Default)",
                             primary=None, secondary=None, bg=None, text=None, accent_soft=None,
                             input_bg=None, input_border=None, code_bg=None, subdued_text=None,
                             button_bg=None, button_hover=None, stop_button_bg=None, stop_button_hover=None,
                             checkbox_off=None, table_bg=None, radio_label=None, dropdown_list_bg=None, ui_opacity=0.9,
                             bg_image=None, bg_opacity=0.4, bg_blur=0, bg_size="cover", bg_position="center", bg_repeat="no-repeat",
                             bg_custom_width="", bg_radius=0, bg_mask_blur=0, bg_front_layer=False):
    """ルーム個別のCSS（文字サイズ、Novel Mode、テーマカラー）を生成する"""

    # 個別テーマが無効の場合は空のCSSを返す
    if not enabled:
        return "<style>#style_injector_component { display: none !important; }</style>"

    # Check for None values (Gradio updates might send None)
    if not font_size: font_size = 15
    if not line_height: line_height = 1.6

    # 1. Readability & Novel Mode (Common)
    css = f"""
    #chat_output_area .message-bubble,
    #chat_output_area .message-row .message-bubble,
    #chat_output_area .message-wrap .message,
    #chat_output_area .prose,
    #chat_output_area .prose > *,
    #chat_output_area .prose p,
    #chat_output_area .prose li {{
        font-size: {font_size}px !important;
        line-height: {line_height} !important;
    }}
    #chat_output_area code,
    #chat_output_area pre,
    #chat_output_area pre span {{
        font-size: {int(font_size)*0.9}px !important;
        line-height: {line_height} !important;
    }}
    #style_injector_component {{ display: none !important; }}
    """

    if chat_style == "Novel (Text only)":
        css += """
        #chat_output_area .message-row .message-bubble,
        #chat_output_area .message-row .message-bubble:before,
        #chat_output_area .message-row .message-bubble:after,
        #chat_output_area .message-wrap .message,
        #chat_output_area .message-wrap .message.bot,
        #chat_output_area .message-wrap .message.user,
        #chat_output_area .bot-row .message-bubble,
        #chat_output_area .user-row .message-bubble {
            background: transparent !important;
            background-color: transparent !important;
            border: none !important;
            box-shadow: none !important;
            padding: 0 !important;
            margin: 4px 0 !important;
            border-radius: 0 !important;
        }
        #chat_output_area .message-row,
        #chat_output_area .user-row,
        #chat_output_area .bot-row {
            display: flex !important;
            justify-content: flex-start !important;
            margin-bottom: 12px !important;
            background: transparent !important;
            border: none !important;
            width: 100% !important;
        }
        #chat_output_area .avatar-container { display: none !important; }
        #chat_output_area .message-wrap .message { padding: 0 !important; }
        """

    # 2. Color Theme Overrides
    overrides = []

    # メインカラー: Interactive elements (Checkbox, Slider, Loader)
    if primary:
        overrides.append(f"--color-accent: {primary} !important;")
        overrides.append(f"--loader-color: {primary} !important;")
        overrides.append(f"--primary-500: {primary} !important;") # Fallback for some themes
        overrides.append(f"--primary-600: {primary} !important;")

    # サブカラー: Chat bubbles, Panel backgrounds, Item box highlights
    if secondary:
        overrides.append(f"--background-fill-secondary: {secondary} !important;")
        overrides.append(f"--block-label-background-fill: {secondary} !important;")
        # Custom CSS variable often used for bot bubbles in Nexus Ark
        overrides.append(f"--secondary-500: {secondary} !important;")
        # タブのオーバーフローメニュー（…）のホバー時にサブカラーを適用
        css += f"""
        /* タブのオーバーフローメニューのホバー時 - サブカラーを適用 */
        div.overflow-dropdown button:hover,
        .overflow-dropdown button:hover {{
            background-color: {secondary} !important;
            background: {secondary} !important;
        }}
        /* チャット入力欄全体の背景色（MultiModalTextbox）- サブカラーを適用 */
        #chat_input_multimodal,
        #chat_input_multimodal > div,
        #chat_input_multimodal .block,
        div.block.multimodal-textbox,
        div.block.multimodal-textbox.svelte-1svsvh2,
        div[class*="multimodal-textbox"][class*="block"],
        div.full-container,
        div.full-container.svelte-5gfv2q,
        [aria-label*="ultimedia input field"],
        [aria-label*="ultimedia input field"] > div {{
            background-color: {secondary} !important;
            background: {secondary} !important;
        }}
        """

    # タブのオーバーフローメニュー（…）の非ホバー時 - 背景色を適用
    if bg:
        css += f"""
        /* タブのオーバーフローメニュー（…）の背景色 - 非ホバー時 */
        div.overflow-dropdown,
        .overflow-dropdown {{
            background-color: {bg} !important;
            background: {bg} !important;
        }}
        """

    # 背景色: Overall App Background & Content Boxes
    if bg:
        overrides.append(f"--body-background-fill: {bg} !important;")
        overrides.append(f"--background-fill-primary: {bg} !important;")
        overrides.append(f"--block-background-fill: {bg} !important;")

    # テキスト色: Body text, labels, headers
    if text:
        overrides.append(f"--body-text-color: {text} !important;")
        overrides.append(f"--block-label-text-color: {text} !important;")
        overrides.append(f"--block-info-text-color: {text} !important;")
        overrides.append(f"--section-header-text-color: {text} !important;")
        overrides.append(f"--prose-text-color: {text} !important;")
        # ダークモード用の変数も追加
        overrides.append(f"--block-label-text-color-dark: {text} !important;")
        # 直接ラベル要素にスタイルを適用（CSS変数が効かない場合の対策）
        # Gradioが生成するdata-testid属性を使用
        css += f"""
        [data-testid="block-info"],
        [data-testid="block-label"],
        span[data-testid="block-info"],
        span[data-testid="block-label"],
        .gradio-container label,
        .gradio-container label span,
        .dark [data-testid="block-info"],
        .dark [data-testid="block-label"],
        .dark label,
        .dark label span {{
            color: {text} !important;
        }}
        """


    # ユーザー発言背景 (Accent Soft)
    if accent_soft:
        overrides.append(f"--color-accent-soft: {accent_soft} !important;")

    # === 詳細設定 ===

    # 入力欄の背景色 (Form Background)
    if input_bg:
        overrides.append(f"--input-background-fill: {input_bg} !important;")
        overrides.append(f"--input-background-fill-hover: {input_bg} !important;")
        # スクロールバーも連動させる
        css += f"""
        *::-webkit-scrollbar {{ width: 8px; height: 8px; }}
        *::-webkit-scrollbar-thumb {{
            background-color: {input_bg} !important;
            border-radius: 4px;
        }}
        *::-webkit-scrollbar-track {{ background-color: transparent; }}
        """

    # ドロップダウンリストの背景色 (Dropdown List Background)
    if dropdown_list_bg:
        css += f"""
        /* ドロップダウンリストの背景色 */
        ul.options,
        ul.options.svelte-y6qw75,
        .gradio-container ul[role="listbox"],
        .gradio-container .options {{
            background-color: {dropdown_list_bg} !important;
            background: {dropdown_list_bg} !important;
        }}
        """

    # 入力欄の枠線色 (Form Border)
    if input_border:
        overrides.append(f"--border-color-primary: {input_border} !important;")
        overrides.append(f"--input-border-color: {input_border} !important;")
        overrides.append(f"--input-border-color-focus: {input_border} !important;")

    # コードブロック背景色 (Code Block BG)
    if code_bg:
        overrides.append(f"--code-background-fill: {code_bg} !important;")
        # チャット内のコードブロックにも適用
        css += f"""
        #chat_output_area pre,
        #chat_output_area code,
        .prose pre,
        .prose code {{
            background-color: {code_bg} !important;
        }}
        """

    # サブテキスト色（説明文など）
    if subdued_text:
        overrides.append(f"--body-text-color-subdued: {subdued_text} !important;")
        overrides.append(f"--block-info-text-color: {subdued_text} !important;")
        overrides.append(f"--input-placeholder-color: {subdued_text} !important;")

    # ボタン背景色（secondaryボタン）
    if button_bg:
        overrides.append(f"--button-secondary-background-fill: {button_bg} !important;")
        overrides.append(f"--button-secondary-background-fill-dark: {button_bg} !important;")
        # 直接セレクターでも適用
        css += f"""
        button.secondary,
        .gradio-container button.secondary {{
            background-color: {button_bg} !important;
        }}
        """

    # プライマリーボタン背景色（メインカラーを使用）
    if primary:
        overrides.append(f"--button-primary-background-fill: {primary} !important;")
        overrides.append(f"--button-primary-background-fill-dark: {primary} !important;")
        overrides.append(f"--button-primary-background-fill-hover: {primary} !important;")
        overrides.append(f"--button-primary-background-fill-hover-dark: {primary} !important;")
        css += f"""
        button.primary,
        .gradio-container button.primary {{
            background-color: {primary} !important;
        }}
        button.primary:hover,
        .gradio-container button.primary:hover {{
            background-color: {primary} !important;
            filter: brightness(1.1);
        }}
        """

    # ボタンホバー色
    if button_hover:
        overrides.append(f"--button-secondary-background-fill-hover: {button_hover} !important;")
        overrides.append(f"--button-secondary-background-fill-hover-dark: {button_hover} !important;")
        css += f"""
        button.secondary:hover,
        .gradio-container button.secondary:hover {{
            background-color: {button_hover} !important;
        }}
        """

    # 停止ボタン背景色（stop/cancelボタン）
    if stop_button_bg:
        overrides.append(f"--button-cancel-background-fill: {stop_button_bg} !important;")
        overrides.append(f"--button-cancel-background-fill-dark: {stop_button_bg} !important;")
        css += f"""
        button.stop,
        button.cancel,
        .gradio-container button.stop,
        .gradio-container button.cancel {{
            background-color: {stop_button_bg} !important;
        }}
        """

    # 停止ボタンホバー色
    if stop_button_hover:
        overrides.append(f"--button-cancel-background-fill-hover: {stop_button_hover} !important;")
        overrides.append(f"--button-cancel-background-fill-hover-dark: {stop_button_hover} !important;")
        css += f"""
        button.stop:hover,
        button.cancel:hover,
        .gradio-container button.stop:hover,
        .gradio-container button.cancel:hover {{
            background-color: {stop_button_hover} !important;
        }}
        """

    # チェックボックスオフ時の背景色
    if checkbox_off:
        overrides.append(f"--checkbox-background-color: {checkbox_off} !important;")
        overrides.append(f"--checkbox-background-color-dark: {checkbox_off} !important;")
        css += f"""
        input[type="checkbox"]:not(:checked),
        .gradio-container input[type="checkbox"]:not(:checked),
        .checkbox-container:not(.selected),
        [data-testid="checkbox"]:not(:checked) {{
            background-color: {checkbox_off} !important;
        }}
        """

    # テーブル背景色
    if table_bg:
        overrides.append(f"--table-even-background-fill: {table_bg} !important;")
        overrides.append(f"--table-odd-background-fill: {table_bg} !important;")
        css += f"""
        table,
        .table-container,
        .table-wrap,
        .gradio-container table,
        .gradio-container .table-container,
        [role="grid"] {{
            background-color: {table_bg} !important;
        }}
        table td,
        table th,
        .table-wrap td,
        .table-wrap th {{
            background-color: {table_bg} !important;
        }}
        """

    # ラジオ/チェックボックスのラベル背景色
    if radio_label:
        css += f"""
        /* ラジオボタン・チェックボックスのラベル背景色 */
        label.svelte-1bx8sav,
        .gradio-container label[data-testid*="-radio-label"],
        .gradio-container label[data-testid*="-checkbox-label"] {{
            background-color: {radio_label} !important;
            background: {radio_label} !important;
        }}
        """

    if overrides:
        # Create a more aggressive global override block
        css += f"""
        :root, body, gradio-app, .gradio-container, .dark {{
            {' '.join(overrides)}
        }}
        /* Specific overrides for common containers */
        #chat_output_area, #room_theme_color_settings {{
            {' '.join(overrides)}
        }}
        """

    # 背景画像
    if bg_image:
        import base64
        from PIL import Image, ImageOps
        import io

        bg_image_url = ""

        # HTTP URLならそのまま
        if bg_image.startswith("http"):
             bg_image_url = bg_image
        # ローカルファイルならBase64エンコード（リサイズ処理付き）
        elif os.path.exists(bg_image):
            try:
                with Image.open(bg_image) as raw_img:
                    img = ImageOps.exif_transpose(raw_img) or raw_img
                    # 最大サイズ制限 (Full HD相当)
                    max_size = 1920
                    if max(img.size) > max_size:
                        ratio = max_size / max(img.size)
                        new_size = (int(img.width * ratio), int(img.height * ratio))
                        img = img.resize(new_size, Image.Resampling.LANCZOS)

                    buffer = io.BytesIO()
                    # JPEG変換して軽量化 (PNGだと重い場合があるが、画質優先ならPNG)
                    # ここでは元のフォーマットに近い形で、ただし透過考慮でPNG推奨
                    img.save(buffer, format="PNG")
                    encoded_string = base64.b64encode(buffer.getvalue()).decode('utf-8')
                    bg_image_url = f"data:image/png;base64,{encoded_string}"
            except Exception as e:
                print(f"Error encoding/resizing background image: {e}")

        if bg_image_url:
             # スタンプモード（custom）か壁紙モードか
             is_stamp_mode = (bg_size == "custom" and bg_custom_width)

             if is_stamp_mode:
                 # スタンプモード: width/heightを指定し、配置を細かく制御
                 # アスペクト比は維持したいが、CSSのbackground-imageでアスペクト比維持しつつサイズ指定は
                 # containerのサイズを画像に合わせる必要がある。
                 # ここではwidthを基準に、heightはautoにしたいが、fixed要素でheight:auto空だと表示されないことがある。
                 # 正方形またはcontainで表示領域を確保する。

                 size_style = f"width: {bg_custom_width}; height: {bg_custom_width}; background-size: contain;"
                 if bg_repeat == "no-repeat":
                     size_style += " background-repeat: no-repeat;"

                 # 配置ロジック (簡易変換)
                 # ユーザーが "top left" (文字列) を選んだ場合の変換
                 # CSSの background-position は "top left" そのままで有効だが、
                 # fixed要素自体の配置(top, left)とは別。
                 # スタンプモードでは fixed要素自体を動かすのが自然。

                 pos_style = "top: 50%; left: 50%; transform: translate(-50%, -50%);" # Default Center
                 bg_p = bg_position.lower()

                 if bg_p == "top left": pos_style = "top: 20px; left: 20px;"
                 elif bg_p == "top right": pos_style = "top: 20px; right: 20px;"
                 elif bg_p == "bottom left": pos_style = "bottom: 20px; left: 20px;"
                 elif bg_p == "bottom right": pos_style = "bottom: 20px; right: 20px;"
                 elif bg_p == "top": pos_style = "top: 20px; left: 50%; transform: translateX(-50%);"
                 elif bg_p == "bottom": pos_style = "bottom: 20px; left: 50%; transform: translateX(-50%);"
                 elif bg_p == "left": pos_style = "top: 50%; left: 20px; transform: translateY(-50%);"
                 elif bg_p == "right": pos_style = "top: 50%; right: 20px; transform: translateY(-50%);"
                 # center 以外の場合、transformを上書きする形になるので注意

                 # border-radius
                 radius_style = f"border-radius: {bg_radius}%;" if bg_radius else ""
                 bg_p_style = "" # 初期化

             else:
                 # 壁紙モード
                 size_style = f"width: 100%; height: 100%; background-size: {bg_size}; background-repeat: {bg_repeat};"
                 # background-position はCSSプロパティとしてそのまま渡す
                 pos_style = "top: 0; left: 0;"
                 # 壁紙モードでも角丸を適用可能にする
                 radius_style = f"border-radius: {bg_radius}%;" if bg_radius else ""
                 bg_p_style = f"background-position: {bg_position};"

             # エッジぼかし (Mask) - 両方のモードで有効
             mask_style = ""
             if bg_mask_blur > 0:
                 # エッジから内側に向けてぼかす
                 # radial-gradient: circle at center, black (100% - blur), transparent 100%
                 # ただしStampモード(正方形とは限らない)の場合、closest-sideなどが良い
                 mask_style = f"mask-image: radial-gradient(closest-side, black calc(100% - {bg_mask_blur}px), transparent 100%); -webkit-mask-image: radial-gradient(closest-side, black calc(100% - {bg_mask_blur}px), transparent 100%);"

             # オーバーレイ設定 (最前面表示)
             if bg_front_layer:
                 z_index_val = 9999
                 # [Safety] フロントレイヤー時は、操作不能になるのを防ぐため不透明度を最大0.4に制限する
                 if bg_opacity > 0.4: bg_opacity = 0.4
             else:
                 z_index_val = 0 # 背景(標準)は0にし、コンテンツを1にする戦略に変更

             # UI Opacity Logic: テーマカラーが指定されている場合はそれを透過し、なければ黒等をベースにする
             sec_color = hex_to_rgba(secondary, ui_opacity) if secondary else f"rgba(0, 0, 0, {ui_opacity})"
             block_color = hex_to_rgba(bg, ui_opacity) if bg else f"rgba(0, 0, 0, {ui_opacity})"
             # ユーザーバブル(Accent Soft)も透過させる
             # 指定がない場合はデフォルト(Generic Theme)の色に合わせるのが難しいが、白かグレーの透過が無難
             accent_soft_color = hex_to_rgba(accent_soft, ui_opacity) if accent_soft else None

             css += f"""
        /* 背景画像レイヤー */
        body::before, .gradio-container::before, gradio-app::before {{
            content: "";
            position: fixed;
            {pos_style}
            {size_style}
            background-image: url('{bg_image_url}');
            {bg_p_style if not is_stamp_mode else ''}

            opacity: {bg_opacity};
            filter: blur({bg_blur}px);
            z-index: {z_index_val};
            pointer-events: none;
            {radius_style}
            {mask_style}
        }}

        /* 背景画像が見えるようにCSS変数レベルで背景を透明化 */
        :root, body, .gradio-container, .dark, .dark .gradio-container {{
            --background-fill-primary: transparent !important;
            /* UI Opacity Control */
            --background-fill-secondary: {sec_color} !important;
            --block-background-fill: {block_color} !important;
            /* ユーザーバブルが未指定の場合も透過させる (Fallback to dark tint) */
            {f'--color-accent-soft: {accent_soft_color} !important;' if accent_soft_color else f'--color-accent-soft: rgba(0, 0, 0, {ui_opacity}) !important;'}
        }}
        /* コンテンツを背景の上に表示 (標準モード対策, z-index: 1) */
        .gradio-container {{
            position: relative;
            z-index: 1;
        }}

        /* コンテナ自体の背景も透明 */
        .gradio-container {{
            background-color: transparent !important;
            background: transparent !important;
        }}

        /* サイドバー（左カラム）のスクロール設定を明示的に保証 */
        /* NOTE: .tabs > div はGradioのタブオーバーフローメニュー（…）に干渉するため除外 */
        .gradio-container > div > div,
        .contain > div,
        [class*="column"],
        .tabitem > div {{
            overflow-y: auto !important;
            overflow-x: hidden !important;
            -webkit-overflow-scrolling: touch !important;
        }}
        /* タブのオーバーフローメニュー（…）を正常に表示するため */
        .tabs > div {{
            overflow: visible !important;
        }}

        /* チャットバブルの背景を直接透過 (CSS変数が効かない場合の対策) */
        #chat_output_area .message-bubble,
        #chat_output_area .message-row .message-bubble,
        #chat_output_area .message-wrap .message,
        #chat_output_area .message-wrap .message.bot,
        #chat_output_area .bot-row .message-bubble {{
            background-color: {sec_color} !important;
            background: {sec_color} !important;
        }}
        #chat_output_area .message-wrap .message.user,
        #chat_output_area .user-row .message-bubble {{
            background-color: {f'{accent_soft_color}' if accent_soft_color else f'rgba(0, 0, 0, {ui_opacity})'} !important;
            background: {f'{accent_soft_color}' if accent_soft_color else f'rgba(0, 0, 0, {ui_opacity})'} !important;
        }}
        /* チャット欄全体のコンテナも透過 (より包括的) */
        #chat_output_area,
        #chat_output_area > div,
        #chat_output_area > div > div,
        #chat_output_area .wrap,
        #chat_output_area .chatbot,
        .chatbot,
        .chatbot > div,
        .chatbot .wrap,
        .chatbot .wrapper,
        [data-testid="chatbot"],
        [data-testid="chatbot"] > div,
        div[class*="chatbot"],
        div[class*="chat-"] {{
            background-color: transparent !important;
            background: transparent !important;
        }}
        /* Gradio 4.x 対応: 追加のコンテナセレクタ */
        .message-row,
        .bot-row,
        .user-row,
        .messages-wrapper,
        .scroll-hide {{
            background-color: transparent !important;
            background: transparent !important;
        }}

        /* チャット入力欄（MultiModalTextbox）- 最外側のブロックのみ色を付ける */
        div.block.multimodal-textbox,
        div.block.multimodal-textbox.svelte-1svsvh2,
        div[class*="multimodal-textbox"][class*="block"] {{
            background-color: {block_color} !important;
            background: {block_color} !important;
        }}

        /* 内側の要素は透明にして重なりを防止 */
        #chat_input_multimodal > div,
        #chat_input_multimodal .multimodal-input,
        #chat_input_multimodal textarea,
        #chat_input_multimodal .wrap,
        #chat_input_multimodal .full-container,
        #chat_input_multimodal .input-container,
        .multimodal-textbox > div,
        .multimodal-textbox textarea,
        .multimodal-textbox .full-container,
        div.full-container.svelte-5gfv2q,
        div.input-container.svelte-5gfv2q,
        [aria-label*="ultimedia input field"],
        [aria-label*="ultimedia input field"] > div,
        .gradio-container div.full-container,
        .gradio-container div.input-container,
        .gradio-container [role="group"][aria-label*="ultimedia"],
        .gradio-container [role="group"][aria-label*="ultimedia"] > div,
        div[class*="full-container"],
        div[class*="input-container"][class*="svelte"],
        div.wrap.default.full.svelte-btia7y,
        .block.multimodal-textbox div.wrap,
        div.wrap.default.full,
        div.form.svelte-1vd8eap,
        div.form[class*="svelte"] {{
            background-color: transparent !important;
            background: transparent !important;
        }}

        /* ドロップダウンメニュー等の視認性修正 */
        .options, ul.options, .wrap.options, .dropdown-options {{
            background-color: #1f2937 !important; /* ダークグレー */
            color: #f3f4f6 !important;
            opacity: 1 !important;
            z-index: 10000 !important;
        }}
        /* 選択中のアイテム */
        li.item.selected {{
            background-color: #374151 !important;
        }}

        /* ===== Front Layer Mode: コンテンツをオーバーレイより上に表示 ===== */
        /* チャット欄の「テキストと画像だけ」をオーバーレイより上に（吹き出し背景は透過のまま） */
        #chat_output_area .prose,
        #chat_output_area .prose p,
        #chat_output_area .prose span,
        #chat_output_area .prose li,
        #chat_output_area .prose code,
        #chat_output_area .prose pre,
        #chat_output_area .message-bubble p,
        #chat_output_area .message-bubble span {{
            position: relative;
            z-index: 10001 !important;
        }}
        /* チャット欄内の画像も上に */
        #chat_output_area img {{
            position: relative;
            z-index: 10002 !important;
        }}
        /* プロフィール・情景画像も上に */
        #profile_image_display,
        #scenery_image_display {{
            position: relative;
            z-index: 10002 !important;
        }}

        /* ===== モバイル対応: 狭い画面ではz-indexを通常に戻す ===== */
        @media (max-width: 768px) {{
            #chat_output_area .prose,
            #chat_output_area .prose p,
            #chat_output_area .prose span,
            #chat_output_area .prose li,
            #chat_output_area .prose code,
            #chat_output_area .prose pre,
            #chat_output_area .message-bubble p,
            #chat_output_area .message-bubble span,
            #chat_output_area img {{
                z-index: auto !important;
            }}
        }}
        """

    return f"<style>{css}</style>"
