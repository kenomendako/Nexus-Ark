import gradio as gr

def load():
    """Gradioテーマオブジェクトを返す。この関数は必須です。"""

    theme = gr.themes.Base(
        primary_hue=gr.themes.Color(c100="#dfdde8", c200="#cdc8db", c300="#b0a8c6", c400="#867ba9", c50="#eae9f0", c500="#73669b", c600="#685c8d", c700="#5b507b", c800="#554b73", c900="#3e3754", c950="#322c43"),
        secondary_hue=gr.themes.Color(c100="#ede9fe", c200="#ddd6fe", c300="#c4b5fd", c400="#a78bfa", c50="#f5f3ff", c500="#8b5cf6", c600="#8560ce", c700="#6c40c4", c800="#5b34a8", c900="#4d2c8e", c950="#431d7f"),
        spacing_size="sm",
        radius_size="lg",
    ).set(
        background_fill_primary='*primary_200',
        background_fill_primary_dark='*primary_950',
        background_fill_secondary='*primary_100',
        background_fill_secondary_dark='*primary_900',
        border_color_accent='*neutral_400',
        border_color_accent_dark='*secondary_950',
        border_color_accent_subdued='*neutral_400',
        border_color_accent_subdued_dark='*neutral_500',
        border_color_primary='*neutral_500',
        border_color_primary_dark='*neutral_950',
        color_accent='*secondary_600',
        color_accent_soft='*neutral_100',
        block_background_fill='*primary_50',
        block_info_text_color='*neutral_500',
        block_info_text_weight='200',
        layout_gap='*spacing_xl',
        button_large_text_size='*text_md',
        button_large_text_weight='500',
        button_medium_text_weight='400',
        button_primary_background_fill='*primary_400',
        button_primary_background_fill_dark='*primary_700',
        button_primary_background_fill_hover='*primary_700',
        button_primary_background_fill_hover_dark='*primary_500',
        button_primary_border_color='*neutral_50',
        button_secondary_background_fill='*neutral_100',
        button_cancel_background_fill='#4cb2b8',
        button_cancel_background_fill_dark='#3f9aa0',
        button_cancel_background_fill_hover='*neutral_300'
    )

    return theme