import gradio as gr

def load():
    """Gradioテーマオブジェクトを返す。この関数は必須です。"""
    
    # config_manager.pyから移動してきたテーマ定義
    nexus_ark_theme_params = {
        # gr.themes.Default の引数
        "primary_hue": "neutral",
        "secondary_hue": "neutral",
        "neutral_hue": "neutral",
        "font": [gr.themes.GoogleFont('Source Sans Pro'), 'ui-sans-serif', 'system-ui', 'sans-serif'],
        "font_mono": [gr.themes.GoogleFont('IBM Plex Mono'), 'ui-monospace', 'Consolas', 'monospace'],
        # .set() で設定する引数
        "body_background_fill": '*neutral_200',
        "body_background_fill_dark": '*neutral_900',
        "body_text_color": '*neutral_600',
        "body_text_color_dark": '*neutral_300',
        "background_fill_primary": '*neutral_100',
        "background_fill_secondary": '*neutral_100',
        "background_fill_secondary_dark": '*neutral_800',
        "border_color_primary": '*neutral_400',
        "block_background_fill": '*neutral_100',
        "block_label_text_size": '*text_xxs',
        "section_header_text_weight": '100',
        "chatbot_text_size": '*text_md',
        "button_large_padding": '*spacing_md',
        "button_large_radius": '*radius_xs',
        "button_large_text_size": '*text_md',
        "button_large_text_weight": '400',
        "button_small_radius": '*radius_xs',
        "button_medium_radius": '*radius_xs',
        "button_medium_text_weight": '300',
        "button_cancel_background_fill": '#eb4d63',
        "button_cancel_background_fill_dark": '#901124',
        "button_cancel_background_fill_hover": '#fe7385',
        "button_cancel_background_fill_hover_dark": '#b8152d',
        "button_primary_background_fill": '*primary_400',
        "button_primary_background_fill_dark": '*primary_700',
        "button_primary_background_fill_hover": '*primary_500',
        "button_primary_background_fill_hover_dark": '*primary_500',
        "button_primary_border_color_dark": '*primary_50',
        "button_secondary_background_fill": '*neutral_300',
        "button_secondary_background_fill_hover": '*neutral_400',
        "button_secondary_background_fill_hover_dark": '*neutral_500',
        "block_title_text_size": '*text_sm',
        "section_header_text_size": '*text_sm',
        "checkbox_label_text_size": '*text_sm'
    }

    # gr.themes.Default と .set() の引数を分離
    default_args = {
        k: v for k, v in nexus_ark_theme_params.items() 
        if k in ["primary_hue", "secondary_hue", "neutral_hue", "text_size", "spacing_size", "radius_size", "font", "font_mono"]
    }
    set_args = {
        k: v for k, v in nexus_ark_theme_params.items() 
        if k not in default_args
    }

    theme_obj = gr.themes.Default(**default_args)
    if set_args:
        theme_obj = theme_obj.set(**set_args)
        
    return theme_obj