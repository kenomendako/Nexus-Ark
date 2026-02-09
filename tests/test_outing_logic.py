
import os
import sys
import datetime
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.append(str(Path(__file__).parent.parent))

from ui_handlers import _get_log_entries_since_date

def test_get_log_entries_since_date():
    temp_log = "temp_test_log.txt"
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    
    content = f"""## USER:user
{yesterday} (Mon) 10:00:00
昨日のお知らせ。

## AGENT:ルシアン
{yesterday} (Mon) 10:01:00
昨日の返信。

## USER:user
{today} (Tue) 12:00:00
今日のお知らせ。

## AGENT:ルシアン
{today} (Tue) 12:01:00
今日の返信。
"""
    
    with open(temp_log, "w", encoding="utf-8") as f:
        f.write(content)
        
    try:
        # 1. 今日以降を抽出
        entries = _get_log_entries_since_date(temp_log, today, include_timestamp=True)
        assert len(entries) == 2
        assert entries[0][0] == "user"
        assert "今日のお知らせ" in entries[0][1]
        assert entries[1][0] == "ルシアン"
        assert "今日の返信" in entries[1][1]
        print("Test 1 (Today Filtering) Passed")
        
        # 2. 昨日以降を抽出
        entries_all = _get_log_entries_since_date(temp_log, yesterday, include_timestamp=True)
        assert len(entries_all) == 4
        print("Test 2 (Yesterday Filtering) Passed")
        
        # 3. タイムスタンプ除外
        entries_no_ts = _get_log_entries_since_date(temp_log, today, include_timestamp=False)
        assert "12:00:00" not in entries_no_ts[0][1]
        print("Test 3 (No Timestamp) Passed")
        
    finally:
        if os.path.exists(temp_log):
            os.remove(temp_log)

if __name__ == "__main__":
    try:
        test_get_log_entries_since_date()
        print("\nAll isolated logic tests passed!")
    except Exception as e:
        print(f"\nTests failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
