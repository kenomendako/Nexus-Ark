# tests/test_auto_summary_verification.py
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# パス追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import constants
import gemini_api
from langchain_core.messages import HumanMessage, AIMessage

class TestAutoSummaryVerification(unittest.TestCase):
    def setUp(self):
        self.room_name = "test_room"
        self.api_key = "test_api_key"
        self.threshold = constants.AUTO_SUMMARY_DEFAULT_THRESHOLD  # 12,000

    @patch("summary_manager.load_today_summary")
    @patch("summary_manager.generate_summary")
    @patch("summary_manager.save_today_summary")
    def test_apply_auto_summary_below_threshold(self, mock_save, mock_generate, mock_load):
        # 閾値以下のメッセージ
        messages = [
            HumanMessage(content="Hello" * 100),  # 500 chars
            AIMessage(content="Hi" * 100)        # 200 chars
        ]
        
        result = gemini_api._apply_auto_summary(
            messages, self.room_name, self.api_key, self.threshold, allow_generation=True
        )
        
        # 閾値以下ならそのまま返されるはず
        self.assertEqual(len(result), 2)
        self.assertEqual(result, messages)
        mock_generate.assert_not_called()

    @patch("summary_manager.load_today_summary")
    @patch("summary_manager.generate_summary")
    @patch("summary_manager.save_today_summary")
    def test_apply_auto_summary_above_threshold(self, mock_save, mock_generate, mock_load):
        # 閾値(12,000)を超えるメッセージ
        # 保持される分（5往復=10件）を除いた部分が要約対象
        
        # 12件のメッセージを作成（2件が要約対象、10件が保持）
        older_messages = [
            HumanMessage(content="Very long message " * 500), # 9000 chars
            AIMessage(content="Another long response " * 500)  # 11000 chars
        ]
        recent_messages = [
            HumanMessage(content=f"Recent {i}") for i in range(5)
        ] + [
            AIMessage(content=f"Response {i}") for i in range(5)
        ]
        
        messages = older_messages + recent_messages
        
        # モックの設定
        mock_load.return_value = None  # 既存の要約なし
        mock_generate.return_value = "This is a summary of the long conversation."
        
        result = gemini_api._apply_auto_summary(
            messages, self.room_name, self.api_key, self.threshold, allow_generation=True
        )
        
        # 結果の検証
        # 1 (要約メッセージ) + 10 (保持分) = 11件になるはず
        self.assertEqual(len(result), 11)
        self.assertIn("This is a summary", result[0].content)
        self.assertEqual(result[1:], recent_messages)
        
        # generate_summary が呼ばれたことを確認
        mock_generate.assert_called_once()
        # save_today_summary が呼ばれたことを確認
        mock_save.assert_called_once()

    @patch("summary_manager.load_today_summary")
    @patch("summary_manager.generate_summary")
    @patch("summary_manager.save_today_summary")
    def test_apply_auto_summary_incremental(self, mock_save, mock_generate, mock_load):
        # 既存の要約がある場合の追加要約
        
        # 12件のメッセージ
        older_messages = [
            HumanMessage(content="Message A " * 100),
            AIMessage(content="Message B " * 100)
        ]
        recent_messages = [HumanMessage(content=f"R{i}") for i in range(5)] + [AIMessage(content=f"S{i}") for i in range(5)]
        messages = older_messages + recent_messages
        
        # 既存の要約データ
        mock_load.return_value = {
            "summary": "Existing summary",
            "chars_summarized": 0  # まだこの older_messages は要約に含まれていないと仮定
        }
        mock_generate.return_value = "Updated summary"
        
        # 閾値を低くして強制的に発動させる
        result = gemini_api._apply_auto_summary(
            messages, self.room_name, self.api_key, threshold=1000, allow_generation=True
        )
        
        self.assertEqual(len(result), 11)
        self.assertIn("Updated summary", result[0].content)
        mock_generate.assert_called_once()

if __name__ == "__main__":
    unittest.main()
