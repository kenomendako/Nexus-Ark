
filename = r"c:\Users\baken\OneDrive\デスクトップ\gradio_github\gradiotest\nexus_ark.py"
search_strs = ["handle_theme_selection", "gr.Tab", "テーマ"]

try:
    with open(filename, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            for s in search_strs:
                if s in line:
                    print(f"Found '{s}' at line {i}")
except Exception as e:
    print(f"Error: {e}")
