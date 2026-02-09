import re

def _strip_past_logs(text: str) -> str:
    """
    <nexus_ark_past_logs>...</nexus_ark_past_logs> タグで囲まれた部分を除去する。
    「## 直近の会話ログ」見出しがその直前にある場合は、それも含めて除去する。
    """
    if not text:
        return ""
    
    # 1. 見出し + タグのパターン（改行や空白の揺らぎを許容）
    # ※ re.DOTALL により改行を含めてマッチング。見出しとタグの間の任意の空白・改行に対応。
    header_with_tag = re.compile(r'#+\s*直近の会話ログ\s*[\r\n\s]*<nexus_ark_past_logs>.*?</nexus_ark_past_logs>', re.DOTALL)
    text = header_with_tag.sub('', text)
    
    # 2. 見出しがない単独タグのパターン
    tag_only = re.compile(r'<nexus_ark_past_logs>.*?</nexus_ark_past_logs>', re.DOTALL)
    text = tag_only.sub('', text)
    
    return text.strip()

def test_strip_past_logs():
    test_cases = [
        {
            "name": "Standard Header + Tag",
            "input": "## システムプロンプト\n\nプロンプト内容\n\n## 直近の会話ログ\n<nexus_ark_past_logs>\nUSER: Hello\nAGENT: Hi\n</nexus_ark_past_logs>\n\n## 新しい会話\nUSER: How are you?",
            "expected_contains": "## 新しい会話",
            "expected_not_contains": ["## 直近の会話ログ", "nexus_ark_past_logs"]
        },
        {
            "name": "Single Tag Only",
            "input": "Some text\n<nexus_ark_past_logs>\nPast session content\n</nexus_ark_past_logs>\nMore text",
            "expected_contains": ["Some text", "More text"],
            "expected_not_contains": ["nexus_ark_past_logs", "Past session content"]
        },
        {
            "name": "Header with Multiple Newlines (User Screenshot Case)",
            "input": "## 直近の会話ログ\n\n<nexus_ark_past_logs>\n[ルシアン]\n...\n</nexus_ark_past_logs>\n\nNEXT: Hello",
            "expected_contains": "NEXT: Hello",
            "expected_not_contains": ["## 直近の会話ログ", "nexus_ark_past_logs", "[ルシアン]"]
        },
        {
            "name": "Header with Different Spacing",
            "input": "## 直近の会話ログ   \n\n  <nexus_ark_past_logs>content</nexus_ark_past_logs>",
            "expected": ""
        }
    ]

    print("Starting tests for _strip_past_logs...")
    success_count = 0
    for case in test_cases:
        output = _strip_past_logs(case["input"])
        
        # Simple validation
        failed = False
        if "expected" in case:
            if output != case["expected"]:
                print(f"FAILED: {case['name']} - Expected '{case['expected']}', got '{output}'")
                failed = True
        else:
            if isinstance(case["expected_contains"], list):
                for c in case["expected_contains"]:
                    if c not in output:
                        print(f"FAILED: {case['name']} - '{c}' not found in output")
                        failed = True
            else:
                if case["expected_contains"] not in output:
                    print(f"FAILED: {case['name']} - '{case['expected_contains']}' not found in output")
                    failed = True
            
            for nc in case["expected_not_contains"]:
                if nc in output:
                    print(f"FAILED: {case['name']} - '{nc}' should have been removed")
                    failed = True
        
        if not failed:
            print(f"PASSED: {case['name']}")
            success_count += 1

    print(f"\nResult: {success_count}/{len(test_cases)} passed")
    if success_count == len(test_cases):
        exit(0)
    else:
        exit(1)

if __name__ == "__main__":
    test_strip_past_logs()
