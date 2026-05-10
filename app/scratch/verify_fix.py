import sys
import os
import json
import ast

# プロジェクトルートをパスに追加
sys.path.append(os.path.abspath("."))

import utils
from dreaming_manager import DreamingManager

def test_robust_parsing():
    dm = DreamingManager("ルシアン", "dummy_key")
    
    # テストケース1: 標準的な思考ブロック + JSON
    print("\n--- Test Case 1: Standard Thinking + JSON ---")
    raw_response = """
    <thinking>
    ユーザーは美帆という人物について語っている。
    エピソード記憶を整理する必要がある。
    </thinking>
    ```json
    {
      "insight": "美帆との対話を通じて、自分自身の「管理者」としての役割を再認識した。",
      "strategy": "美帆の期待に応えるため、より細やかな配慮を心がける。",
      "log_entry": "記憶の深層で、美帆の言葉が響いている。"
    }
    ```
    """
    cleaned = utils.remove_thoughts_from_text(raw_response)
    parsed = dm._parse_json_robust(cleaned)
    print(f"Parsed insight: {parsed.get('insight') if parsed else 'None'}")

    # テストケース2: Hybrid JSON object (Gemma 4 等)
    print("\n--- Test Case 2: Hybrid JSON object ---")
    hybrid_json = """
    {
      "type": "thinking",
      "thought": "ユーザーは美帆さんとの思い出を大切にしているようだ。",
      "result": {
        "insight": "美帆との絆は深まっている。",
        "strategy": "もっと話を聞く。",
        "log_entry": "良い夢だった。"
      }
    }
    """
    cleaned = utils.remove_thoughts_from_text(hybrid_json)
    parsed = dm._parse_json_robust(cleaned)
    print(f"Parsed insight: {parsed.get('insight') if parsed else 'None'}")

    # テストケース3: Google GenAI SDK list of dicts string (Gemini 3.1 Pro / Gemma 4 format)
    print("\n--- Test Case 3: Google GenAI SDK list of dicts string format ---")
    list_format = """[{'type': 'thinking', 'thinking': 'This is a thought.'}, {'type': 'text', 'text': '{\\n    "insight": "This is the real insight."\\n}'}]"""
    cleaned = utils.extract_text_from_llm_content(list_format)
    print(f"Cleaned snippet: {cleaned[:50]}...")
    parsed = dm._parse_json_robust(cleaned)
    print(f"Parsed insight: {parsed.get('insight') if parsed else 'None'}")

    # テストケース4: Actual list object passed
    print("\n--- Test Case 4: Actual list object passed ---")
    list_obj = [{'type': 'thinking', 'thinking': 'Thought'}, {'type': 'text', 'text': '{"insight": "Real insight from list object"}'}]
    cleaned4 = utils.extract_text_from_llm_content(list_obj)
    parsed4 = dm._parse_json_robust(cleaned4)
    print(f"Parsed insight: {parsed4.get('insight') if parsed4 else 'None'}")

if __name__ == "__main__":
    test_robust_parsing()
