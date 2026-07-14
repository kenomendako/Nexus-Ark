"""ui_handlers のうち「デザイン/テーマ」ドメイン。

ui_handlers パッケージから再エクスポートされ、呼び出し側は従来どおり
ui_handlers.<関数名> でアクセスできる。
"""

from typing import Optional, Tuple, List, Dict, Union, Any
from pathlib import Path
import os
import re
import shutil
import textwrap
import traceback
import gradio as gr
import gemini_api, config_manager, alarm_manager, room_manager, utils, constants, chatgpt_importer, claude_importer, generic_importer

from .styling import generate_room_style_css, _generate_style_from_settings, _resolve_background_image


def _get_theme_previews(theme_name: str) -> Tuple[Optional[str], Optional[str]]:
    """指定されたテーマ名のライト/ダーク両方のプレビュー画像パスを返す。なければNoneを返す。"""
    base_path = Path("assets/theme_previews")
    # プレースホルダー画像が存在しない場合も考慮
    placeholder_path = base_path / "no_preview.png"
    placeholder = str(placeholder_path) if placeholder_path.exists() else None

    light_path = base_path / f"{theme_name}_light.png"
    dark_path = base_path / f"{theme_name}_dark.png"

    light_preview = str(light_path) if light_path.exists() else placeholder
    dark_preview = str(dark_path) if dark_path.exists() else placeholder

    return light_preview, dark_preview


def handle_theme_tab_load():
    """テーマタブが選択されたときに、設定を読み込んでUIを初期化する。"""
    all_themes_map = config_manager.get_all_themes()

    # UIドロップダウン用の選択肢リストを作成
    choices = []
    # カテゴリごとに区切り線と項目を追加
    if any(src == "file" for src in all_themes_map.values()):
        choices.append("--- ファイルベース ---")
        choices.extend([name for name, src in all_themes_map.items() if src == "file"])
    if any(src == "json" for src in all_themes_map.values()):
        choices.append("--- カスタム (JSON) ---")
        choices.extend([name for name, src in all_themes_map.items() if src == "json"])
    if any(src == "preset" for src in all_themes_map.values()):
        choices.append("--- プリセット ---")
        choices.extend([name for name, src in all_themes_map.items() if src == "preset"])

    active_theme_name = config_manager.CONFIG_GLOBAL.get("theme_settings", {}).get("active_theme", "nexus_ark_theme")

    # 最初のプレビュー画像
    light_preview, dark_preview = _get_theme_previews(active_theme_name)

    return gr.update(choices=choices, value=active_theme_name), light_preview, dark_preview


def handle_theme_selection(selected_theme_name: str):
    """ドロップダウンでテーマが選択されたときに、プレビューUIとカスタマイズUIを更新する。"""
    if not selected_theme_name or selected_theme_name.startswith("---"):
        # 区切り線が選択された場合は、何も更新しない
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(interactive=False), gr.update(interactive=False)

    all_themes_map = config_manager.get_all_themes()
    theme_source = all_themes_map.get(selected_theme_name, "preset")

    # サムネイルを更新
    light_preview, dark_preview = _get_theme_previews(selected_theme_name)

    # カスタマイズUIの値を更新
    params = {}
    is_editable = True

    # プリセットテーマの定義
    preset_params = {
        "Soft": {"primary_hue": "blue", "secondary_hue": "sky", "neutral_hue": "slate", "font": ["Source Sans Pro"]},
        "Default": {"primary_hue": "orange", "secondary_hue": "amber", "neutral_hue": "gray", "font": ["Noto Sans"]},
        "Monochrome": {"primary_hue": "neutral", "secondary_hue": "neutral", "neutral_hue": "neutral", "font": ["IBM Plex Mono"]},
        "Glass": {"primary_hue": "teal", "secondary_hue": "cyan", "neutral_hue": "gray", "font": ["Quicksand"]},
    }

    if theme_source == "preset":
        params = preset_params.get(selected_theme_name, {})
    elif theme_source == "json":
        params = config_manager.CONFIG_GLOBAL.get("theme_settings", {}).get("custom_themes", {}).get(selected_theme_name, {})
    elif theme_source == "file":
        is_editable = False # ファイルベースのテーマは直接編集不可
        # UI内に説明テキストを配置するため、ポップアップは出さない
        params = preset_params["Soft"]

    font_name = params.get("font", ["Source Sans Pro"])[0]

    return (
        light_preview,
        dark_preview,
        gr.update(value=params.get("primary_hue"), interactive=is_editable),
        gr.update(value=params.get("secondary_hue"), interactive=is_editable),
        gr.update(value=params.get("neutral_hue"), interactive=is_editable),
        gr.update(value=font_name, interactive=is_editable),
        gr.update(interactive=is_editable), # Save button
        gr.update(interactive=is_editable)  # Export button
    )


def handle_save_custom_theme(new_name, primary_hue, secondary_hue, neutral_hue, font):
    """「カスタムテーマとして保存」ボタンのロジック。config.jsonに保存する。"""
    if not new_name or not new_name.strip():
        gr.Warning("新しいテーマ名を入力してください。")
        return gr.update(), gr.update()

    new_name = new_name.strip()
    # プリセットテーマ名やファイルベースのテーマ名との重複もチェック
    all_themes_map = config_manager.get_all_themes()
    if new_name in all_themes_map and all_themes_map[new_name] != "json":
        gr.Warning(f"名前「{new_name}」はファイルテーマまたはプリセットテーマとして既に存在します。")
        return gr.update(), gr.update(value="")

    current_config = config_manager.load_config_file()
    theme_settings = current_config.get("theme_settings", {})
    custom_themes = theme_settings.get("custom_themes", {})

    custom_themes[new_name] = {
        "primary_hue": primary_hue, "secondary_hue": secondary_hue,
        "neutral_hue": neutral_hue, "font": [font]
    }
    theme_settings["custom_themes"] = custom_themes
    config_manager.save_config_if_changed("theme_settings", theme_settings)

    # グローバル変数を更新して即時反映
    config_manager.load_config()

    gr.Info(f"カスタムテーマ「{new_name}」をJSONとして保存しました。")

    # ドロップダウンの選択肢を再生成して更新
    updated_choices, _, _ = handle_theme_tab_load()

    return updated_choices, gr.update(value="") # フォームをクリア


def handle_export_theme_to_file(new_name, primary_hue, secondary_hue, neutral_hue, font):
    """「ファイルにエクスポート」ボタンのロジック。"""
    if not new_name or not new_name.strip():
        gr.Warning("ファイル名として使用するテーマ名を入力してください。")
        return gr.update()

    file_name = new_name.strip().replace(" ", "_").lower()
    file_name = re.sub(r'[^a-z0-9_]', '', file_name) # 安全なファイル名に
    if not file_name:
        gr.Warning("有効なファイル名を生成できませんでした。")
        return gr.update()

    themes_dir = Path("themes")
    themes_dir.mkdir(exist_ok=True)
    file_path = themes_dir / f"{file_name}.py"

    if file_path.exists():
        gr.Warning(f"テーマファイル '{file_path.name}' は既に存在します。")
        return gr.update()

    # Pythonファイルの内容を生成
    # Gradioのテーマオブジェクトを正しく構築するためのテンプレート
    content = textwrap.dedent(f"""
        import gradio as gr

        def load():
            \"\"\"Gradioテーマオブジェクトを返す。この関数は必須です。\"\"\"
            theme = gr.themes.Default(
                primary_hue="{primary_hue}",
                secondary_hue="{secondary_hue}",
                neutral_hue="{neutral_hue}",
                font=[gr.themes.GoogleFont("{font}")]
            ).set(
                # ここに他の.set()パラメータを追加できます
            )
            return theme
    """)

    try:
        file_path.write_text(content.strip(), encoding="utf-8")
        gr.Info(f"テーマをファイル '{file_path.name}' としてエクスポートしました。")
        # グローバルキャッシュをクリアして次回タブを開いたときに再読み込みさせる
        config_manager._file_based_themes_cache.clear()
        return "" # テキストボックスをクリア
    except Exception as e:
        gr.Error(f"テーマファイルのエクスポート中にエラーが発生しました: {e}")
        return gr.update()


def handle_apply_theme(selected_theme_name: str):
    """「このテーマを適用」ボタンのロジック。"""
    if not selected_theme_name or selected_theme_name.startswith("---"):
        gr.Warning("適用する有効なテーマを選択してください。")
        return

    current_config = config_manager.load_config_file()
    theme_settings = current_config.get("theme_settings", {})
    theme_settings["active_theme"] = selected_theme_name

    config_manager.save_config_if_changed("theme_settings", theme_settings)

    gr.Info(f"テーマ「{selected_theme_name}」を適用設定にしました。アプリケーションを再起動してください。")


def handle_save_theme_settings(*args, silent: bool = False, force_notify: bool = False):
    """詳細なテーマ設定を保存する (Robust Debug Version)"""

    try:
        # 必要な引数数: ... + 前面表示1 + 背景ソース1 + Sync設定9 + Opacity1 + radio_label1 + dropdown_list_bg1 = 43
        if len(args) < 43:
            gr.Error(f"内部エラー: 引数が不足しています ({len(args)}/43)")
            return

        room_name = args[0]

        # 背景画像の保存処理
        bg_image_temp_path = args[23]
        saved_image_path = None

        if bg_image_temp_path:
             try:
                 room_dir = os.path.join(constants.ROOMS_DIR, room_name)
                 os.makedirs(room_dir, exist_ok=True)

                 _, ext = os.path.splitext(bg_image_temp_path)
                 if not ext: ext = ".png"

                 target_filename = f"theme_bg{ext}"
                 destination_path = os.path.join(room_dir, target_filename)

                 # 同じパスでない場合のみコピー（既存パスが渡された場合の無駄なコピー防止）
                 if os.path.abspath(bg_image_temp_path) != os.path.abspath(destination_path):
                    shutil.copy2(bg_image_temp_path, destination_path)

                 saved_image_path = destination_path
             except Exception as img_err:
                 print(f"Error saving background image: {img_err}")
                 gr.Warning(f"背景画像の保存に失敗しました: {img_err}")

        settings = {
            "room_theme_enabled": args[1],  # 個別テーマのオンオフ
            "font_size": args[2],
            "line_height": args[3],
            "chat_style": args[4],
            # 基本配色
            "theme_primary": args[5],
            "theme_secondary": args[6],
            "theme_background": args[7],
            "theme_text": args[8],
            "theme_accent_soft": args[9],
            # 詳細設定
            "theme_input_bg": args[10],
            "theme_input_border": args[11],
            "theme_code_bg": args[12],
            "theme_subdued_text": args[13],
            "theme_button_bg": args[14],
            "theme_button_hover": args[15],
            "theme_stop_button_bg": args[16],
            "theme_stop_button_hover": args[17],
            "theme_checkbox_off": args[18],
            "theme_table_bg": args[19],
            "theme_radio_label": args[20],
            "theme_dropdown_list_bg": args[21],
            "theme_ui_opacity": args[22],
            # 背景画像設定
            "theme_bg_image": saved_image_path,
            "theme_bg_opacity": args[24],
            "theme_bg_blur": args[25],
            "theme_bg_size": args[26],
            "theme_bg_position": args[27],
            "theme_bg_repeat": args[28],
            "theme_bg_custom_width": args[29],
            "theme_bg_radius": args[30],
            "theme_bg_mask_blur": args[31],
            "theme_bg_front_layer": args[32],
            "theme_bg_src_mode": args[33],

            # Sync設定 (追加)
            "theme_bg_sync_opacity": args[34],
            "theme_bg_sync_blur": args[35],
            "theme_bg_sync_size": args[36],
            "theme_bg_sync_position": args[37],
            "theme_bg_sync_repeat": args[38],
            "theme_bg_sync_custom_width": args[39],
            "theme_bg_sync_radius": args[40],
            "theme_bg_sync_mask_blur": args[41],
            "theme_bg_sync_front_layer": args[42]
        }

        # Use the centralized save function in room_manager
        result = room_manager.save_room_override_settings(room_name, settings)
        if not silent:
            if result == True or (result == "no_change" and force_notify):
                mode_val = settings.get("theme_bg_src_mode")
                gr.Info(f"「{room_name}」のテーマ設定を保存しました。\n保存モード: {mode_val}")
        if result == False:
            gr.Error(f"テーマ保存に失敗しました。コンソールを確認してください。")

    except Exception as e:
        print(f"Error in handle_save_theme_settings: {e}")
        traceback.print_exc()
        gr.Error(f"保存エラー: {e}")


def handle_theme_preview(room_name, enabled, font_size, line_height, chat_style, primary, secondary, bg, text, accent_soft,
                            input_bg, input_border, code_bg, subdued_text,
                            button_bg, button_hover, stop_button_bg, stop_button_hover,
                            checkbox_off, table_bg, radio_label, dropdown_list_bg, ui_opacity,
                            bg_image, bg_opacity, bg_blur, bg_size, bg_position, bg_repeat,
                         bg_custom_width, bg_radius, bg_mask_blur, bg_front_layer, bg_src_mode,
                         # Sync args
                         sync_opacity, sync_blur, sync_size, sync_position, sync_repeat,
                         sync_custom_width, sync_radius, sync_mask_blur, sync_front_layer,
                         is_switching_room: bool = False):
    """UI変更時に即時CSSを返すだけのヘルパー (Syncモード対応)"""
    if is_switching_room:
        return gr.update()

    # プレビュー時でもSyncモードなら画像解決を行う
    mock_settings = { "theme_bg_src_mode": bg_src_mode, "theme_bg_image": bg_image }
    resolved_bg_image = _resolve_background_image(room_name, mock_settings)

    # モードに応じて設定値を切り替え
    is_sync = (bg_src_mode == "現在地と連動 (Sync)")

    use_opacity = sync_opacity if is_sync else bg_opacity
    use_blur = sync_blur if is_sync else bg_blur
    use_size = sync_size if is_sync else bg_size
    use_position = sync_position if is_sync else bg_position
    use_repeat = sync_repeat if is_sync else bg_repeat
    use_custom_width = sync_custom_width if is_sync else bg_custom_width
    use_radius = sync_radius if is_sync else bg_radius
    use_mask_blur = sync_mask_blur if is_sync else bg_mask_blur
    use_front_layer = sync_front_layer if is_sync else bg_front_layer

    return generate_room_style_css(enabled, font_size, line_height, chat_style, primary, secondary, bg, text, accent_soft,
                                   input_bg, input_border, code_bg, subdued_text,
                                   button_bg, button_hover, stop_button_bg, stop_button_hover,
                                   checkbox_off, table_bg, radio_label, dropdown_list_bg, ui_opacity,
                                   resolved_bg_image,
                                   use_opacity, use_blur, use_size, use_position, use_repeat,
                                   use_custom_width, use_radius, use_mask_blur, use_front_layer)


def handle_room_theme_reload(room_name: str):
    """
    パレットタブが選択されたときに、ルーム個別のテーマ設定を再読み込みしてUIに反映する。
    Gradioは非表示タブのコンポーネントを初回ロードで更新しないため、タブ選択時に明示的に再読み込みが必要。

    戻り値の順番:
    0. room_theme_enabled (個別テーマのオンオフ)
    1. chat_style, 2. font_size, 3. line_height,
    4-8. 基本配色5つ (primary, secondary, background, text, accent_soft)
    9-17. 詳細設定9つ (input_bg, input_border, code_bg, subdued_text,        button_bg, button_hover, stop_button_bg, stop_button_hover,
        checkbox_off, table_bg, ui_opacity,
        resolved_bg_image, bg_opacity, bg_blur, bg_size, bg_position, bg_repeat,)
    24. style_injector
    """
    if not room_name:
        return (gr.update(),) * 43 # Updated count: 31 + 12 = 43

    effective_settings = config_manager.get_effective_settings(room_name)
    room_theme_enabled = effective_settings.get("room_theme_enabled", False)

    return (
        gr.update(value=room_theme_enabled),  # 個別テーマのオンオフ
        gr.update(value=effective_settings.get("chat_style", "Chat (Default)")),
        gr.update(value=effective_settings.get("font_size", 15)),
        gr.update(value=effective_settings.get("line_height", 1.6)),
        # 基本配色
        gr.update(value=effective_settings.get("theme_primary", None)),
        gr.update(value=effective_settings.get("theme_secondary", None)),
        gr.update(value=effective_settings.get("theme_background", None)),
        gr.update(value=effective_settings.get("theme_text", None)),
        gr.update(value=effective_settings.get("theme_accent_soft", None)),
        # 詳細設定
        gr.update(value=effective_settings.get("theme_input_bg", None)),
        gr.update(value=effective_settings.get("theme_input_border", None)),
        gr.update(value=effective_settings.get("theme_code_bg", None)),
        gr.update(value=effective_settings.get("theme_subdued_text", None)),
        gr.update(value=effective_settings.get("theme_button_bg", None)),
        gr.update(value=effective_settings.get("theme_button_hover", None)),
        gr.update(value=effective_settings.get("theme_stop_button_bg", None)),
        gr.update(value=effective_settings.get("theme_stop_button_hover", None)),
        gr.update(value=effective_settings.get("theme_checkbox_off", None)),
        gr.update(value=effective_settings.get("theme_table_bg", None)),
        gr.update(value=effective_settings.get("theme_radio_label", None)),
        gr.update(value=effective_settings.get("theme_dropdown_list_bg", None)),
        gr.update(value=effective_settings.get("theme_ui_opacity", 0.9)),
        # 背景画像設定
        gr.update(value=effective_settings.get("theme_bg_image", None)),
        gr.update(value=effective_settings.get("theme_bg_opacity", 0.4)),
        gr.update(value=effective_settings.get("theme_bg_blur", 0)),
        gr.update(value=effective_settings.get("theme_bg_size", "cover")),
        gr.update(value=effective_settings.get("theme_bg_position", "center")),
        gr.update(value=effective_settings.get("theme_bg_repeat", "no-repeat")),
        gr.update(value=effective_settings.get("theme_bg_custom_width", "300px")),
        gr.update(value=effective_settings.get("theme_bg_radius", 0)),
        gr.update(value=effective_settings.get("theme_bg_mask_blur", 0)),
        gr.update(value=effective_settings.get("theme_bg_front_layer", False)),
        gr.update(value=effective_settings.get("theme_bg_src_mode", "画像を指定 (Manual)")),
        # Sync設定
        gr.update(value=effective_settings.get("theme_bg_sync_opacity", 0.4)),
        gr.update(value=effective_settings.get("theme_bg_sync_blur", 0)),
        gr.update(value=effective_settings.get("theme_bg_sync_size", "cover")),
        gr.update(value=effective_settings.get("theme_bg_sync_position", "center")),
        gr.update(value=effective_settings.get("theme_bg_sync_repeat", "no-repeat")),
        gr.update(value=effective_settings.get("theme_bg_sync_custom_width", "300px")),
        gr.update(value=effective_settings.get("theme_bg_sync_radius", 0)),
        gr.update(value=effective_settings.get("theme_bg_sync_mask_blur", 0)),
        gr.update(value=effective_settings.get("theme_bg_sync_front_layer", False)),
        # CSS生成
        gr.update(value=_generate_style_from_settings(room_name, effective_settings)),
    )
