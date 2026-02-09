
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# プロジェクトのルートディレクトリをパスに追加
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tools.space_tools import _apply_world_edits
from utils import format_tool_result_for_ui

class TestWorldEditSummary(unittest.TestCase):

    @patch('utils.parse_world_file')
    @patch('room_manager.get_world_settings_path')
    @patch('world_builder.save_world_data')
    def test_apply_world_edits_summary(self, mock_save, mock_get_path, mock_parse):
        # 準備
        mock_parse.return_value = {"エリア1": {"場所1": "説明1"}}
        mock_get_path.return_value = "/dummy/path"
        
        instructions = [
            {"operation": "update_place_description", "area_name": "エリア1", "place_name": "場所1", "value": "新しい説明"},
            {"operation": "add_place", "area_name": "エリア2", "place_name": "場所2", "value": "追加の説明"},
            {"operation": "delete_place", "area_name": "エリア1", "place_name": "場所3"} # 場所3は存在しないが削除指示
        ]
        
        # 実行
        result = _apply_world_edits(instructions, "test_room")
        
        # 検証
        self.assertIn("成功: 以下の変更を世界設定(world_settings.txt)に適用しました：", result)
        self.assertIn("- [更新] エリア1 > 場所1", result)
        self.assertIn("- [追加] エリア2 > 場所2", result)
        self.assertIn("- [削除] エリア1 > 場所3", result)
        
        # UI表示フォーマットの検証
        ui_announcement = format_tool_result_for_ui("plan_world_edit", result)
        self.assertIn("🛠️ 世界設定を更新しました", ui_announcement)
        self.assertIn("[更新] エリア1>場所1", ui_announcement)
        self.assertIn("[追加] エリア2>場所2", ui_announcement)
        self.assertIn("[削除] エリア1>場所3", ui_announcement)

    def test_format_tool_result_for_ui_long_summary(self):
        # 長いサマリーの切り詰め検証
        long_result = "成功: 以下の変更を世界設定(world_settings.txt)に適用しました：\n"
        for i in range(10):
            long_result += f"- [更新] エリア{i} > 場所{i}\n"
        
        ui_announcement = format_tool_result_for_ui("plan_world_edit", long_result)
        self.assertTrue(ui_announcement.endswith("...）"))
        self.assertLessEqual(len(ui_announcement), 100) # 🛠️ 等を含めても極端に長くならないこと

if __name__ == '__main__':
    unittest.main()
