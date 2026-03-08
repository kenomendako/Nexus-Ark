import gradio as gr

def load():
    """Nexus Modern テーマ (Dark Mode Optimized)"""

    # ベースとするテーマ: Soft
    # 理由: スペーシングとフォント設定がモダンなため
    theme = gr.themes.Soft(
        primary_hue=gr.themes.colors.indigo,
        secondary_hue=gr.themes.colors.violet,
        neutral_hue=gr.themes.colors.slate,
        text_size="sm",
        spacing_size="sm",
        radius_size="md",
        font=[
            gr.themes.GoogleFont('Inter'), 
            'ui-sans-serif', 
            'system-ui', 
            'sans-serif'
        ],
        font_mono=[
            gr.themes.GoogleFont('JetBrains Mono'), 
            'ui-monospace', 
            'Consolas', 
            'monospace'
        ],
    ).set(
        # --- Body ---
        body_background_fill='*neutral_50',
        body_background_fill_dark='#0f172a', # Slate-900 (Deep Dark Blue)
        body_text_color='*neutral_900',
        body_text_color_dark='*neutral_100',
        
        # --- Blocks (Container) ---
        block_background_fill='white',
        block_background_fill_dark='#1e293b', # Slate-800
        block_border_width='0px',
        block_border_width_dark='0px',
        block_shadow='*shadow_drop',
        block_shadow_dark='0 4px 6px -1px rgba(0, 0, 0, 0.3), 0 2px 4px -1px rgba(0, 0, 0, 0.16)',
        
        # --- Buttons (Primary) ---
        button_primary_background_fill='*primary_600',
        button_primary_background_fill_dark='*primary_600',
        button_primary_background_fill_hover='*primary_700',
        button_primary_text_color='white',
        button_primary_border_color='transparent',
        button_primary_shadow='*shadow_sm',
        
        # --- Buttons (Secondary) ---
        button_secondary_background_fill='*neutral_100',
        button_secondary_background_fill_dark='*neutral_700',
        button_secondary_background_fill_hover='*neutral_200',
        button_secondary_background_fill_hover_dark='*neutral_600',
        button_secondary_border_color='transparent',

        # --- Inputs (Textbox, Dropdown, etc) ---
        input_background_fill='*neutral_50',
        input_background_fill_dark='#334155', # Slate-700
        input_border_color='*neutral_200',
        input_border_color_dark='*neutral_600',
        input_border_width='0px',
        input_shadow='inner',
        input_shadow_dark='none',
        
        # --- Labels ---
        block_label_text_weight='600',
        block_label_text_size='*text_sm',
        block_label_margin='0 0 8px 0',
        
        # --- Borders (Generic) ---
        border_color_primary='*neutral_200',
        border_color_primary_dark='*neutral_700',

        # --- Scrollbar ---
        # Note: Gradio themes don't fully control scrollbar via variables, 
        # normally done in custom CSS, but we set scrollbar colors just in case future support adds it.
    )

    return theme
