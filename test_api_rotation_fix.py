import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# プロジェクトルートをパスに追加
sys.path.append(os.getcwd())

import agent.scenery_manager as scenery_manager
from google.api_core import exceptions as google_exceptions

class TestAPIRotation(unittest.TestCase):

    @patch('agent.scenery_manager.LLMFactory.create_chat_model')
    @patch('agent.scenery_manager.config_manager.get_active_gemini_api_key')
    @patch('agent.scenery_manager.config_manager.mark_key_as_exhausted')
    @patch('agent.scenery_manager.config_manager.get_key_name_by_value')
    @patch('agent.scenery_manager.config_manager.get_effective_settings')
    @patch('agent.scenery_manager.utils.get_current_location')
    @patch('agent.scenery_manager.utils.parse_world_file')
    @patch('agent.scenery_manager.get_world_settings_path')
    def test_scenery_rotation_on_429(self, mock_get_path, mock_parse_world, mock_get_loc, mock_get_settings, mock_get_key_name, mock_mark_exhausted, mock_get_active_key, mock_create_model):
        # 設定
        room_name = "test_room"
        initial_key = "key_1"
        next_key = "key_2"
        
        mock_get_loc.return_value = "Living"
        mock_parse_world.return_value = {"Area 1": {"Living": "A cozy living room"}}
        mock_get_settings.return_value = {"enable_api_key_rotation": True}
        
        # 1回目の呼び出しでは key_1, 2回目は key_2 と判定
        mock_get_key_name.side_effect = lambda k: "default" if k == initial_key else "paid" if k == next_key else "Unknown"
        
        # 1回目のLLM呼び出しで429エラー、2回目で成功
        mock_llm_1 = MagicMock()
        mock_llm_1.invoke.side_effect = google_exceptions.ResourceExhausted("429 Quota exhausted")
        
        mock_llm_2 = MagicMock()
        mock_llm_2.invoke.return_value.content = "Beautiful scenery"
        
        mock_create_model.side_effect = [mock_llm_1, mock_llm_2]
        
        # 429発生時に次に返されるキー
        mock_get_active_key.return_value = next_key

        # IO周りのモック (関数内のローカルインポートに対応)
        # scipy_manager.utils がモジュールとしてインポートされているので、そこをパッチする
        with patch('agent.scenery_manager.utils.load_scenery_cache', return_value={}), \
             patch('agent.scenery_manager.utils.save_scenery_cache'), \
             patch('agent.scenery_manager.utils.get_season', return_value="summer"), \
             patch('agent.scenery_manager.utils.get_time_of_day', return_value="morning"):
            
            loc, space, text = scenery_manager.generate_scenery_context(room_name, initial_key)

        # 検証
        self.assertEqual(text, "Beautiful scenery")
        self.assertEqual(mock_create_model.call_count, 2)
        
        # 1回目のキーで枯渇マークが呼ばれたか
        mock_mark_exhausted.assert_called_with("default")
        
        # 2回目のモデル作成で新しいキーが使われたか
        called_args = mock_create_model.call_args_list
        self.assertEqual(called_args[0][1]['api_key'], initial_key)
        self.assertEqual(called_args[1][1]['api_key'], next_key)
        
        print("\n✅ API Key Rotation Test Passed!")

if __name__ == '__main__':
    unittest.main()
