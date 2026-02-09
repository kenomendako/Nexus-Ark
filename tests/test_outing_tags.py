import re

def _strip_past_logs(text: str) -> str:
    if not text:
        return ""
    pattern = re.compile(r'<nexus_ark_past_logs>.*?</nexus_ark_past_logs>', re.DOTALL)
    return pattern.sub('', text).strip()

def test_strip_past_logs():
    # 1. タグが含まれる場合
    test_input = """
## SYSTEM:情報
これはシステム情報です。

<nexus_ark_past_logs>
[user]
こんにちは
[ルシアン]
こんにちは！
</nexus_ark_past_logs>

新しい会話がここから始まります。
"""
    expected = "## SYSTEM:情報\nこれはシステム情報です。\n\n新しい会話がここから始まります。"
    result = _strip_past_logs(test_input)
    assert "こんにちは" not in result
    assert "新しい会話" in result
    print("Test 1 (Basic Tags) Passed")

    # 2. タグが含まれない場合
    test_input_2 = "通常のテキストのみ"
    assert _strip_past_logs(test_input_2) == "通常のテキストのみ"
    print("Test 2 (No Tags) Passed")

    # 3. 複数のタグが含まれる場合
    test_input_3 = "Start <nexus_ark_past_logs>Old 1</nexus_ark_past_logs> Middle <nexus_ark_past_logs>Old 2</nexus_ark_past_logs> End"
    result_3 = _strip_past_logs(test_input_3)
    assert result_3 == "Start  Middle  End"
    print("Test 3 (Multiple Tags) Passed")

    # 4. タグ内が空の場合
    test_input_4 = "Start <nexus_ark_past_logs></nexus_ark_past_logs> End"
    assert _strip_past_logs(test_input_4) == "Start  End"
    print("Test 4 (Empty Tags) Passed")

if __name__ == "__main__":
    test_strip_past_logs()
    print("All logic tests passed!")
