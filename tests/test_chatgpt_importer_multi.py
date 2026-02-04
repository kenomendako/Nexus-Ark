
import unittest
import json
import tempfile
import os
import shutil
from unittest.mock import patch, MagicMock
import sys

# プロジェクトルートをパスに追加
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import chatgpt_importer
import room_manager
import constants

class TestChatGPTImporterMulti(unittest.TestCase):

    def setUp(self):
        # テスト用の一次ディレクトリを作成
        self.test_dir = tempfile.mkdtemp()
        self.original_rooms_dir = constants.ROOMS_DIR
        constants.ROOMS_DIR = self.test_dir

        # ダミーのconversations.jsonを作成
        self.conversations_file = os.path.join(self.test_dir, 'conversations.json')
        
        # 2つの会話データを作成
        # Thread A: 古い会話 (2023-01-01)
        # Thread B: 新しい会話 (2023-02-01)
        self.mock_data = [
            {
                "title": "Old Thread",
                "mapping": {
                    "id_a_1": {
                        "message": {
                            "id": "id_a_1",
                            "author": {"role": "user"},
                            "content": {"parts": ["Message A1 (Old)"]},
                            "create_time": 1672531200.0  # 2023-01-01 00:00:00 UTC
                        },
                        "children": ["id_a_2"]
                    },
                    "id_a_2": {
                        "message": {
                            "id": "id_a_2",
                            "author": {"role": "assistant"},
                            "content": {"parts": ["Message A2 (Old)"]},
                            "create_time": 1672531260.0  # 2023-01-01 00:01:00 UTC
                        },
                        "children": []
                    }
                }
            },
            {
                "title": "New Thread",
                "mapping": {
                    "id_b_1": {
                        "message": {
                            "id": "id_b_1",
                            "author": {"role": "user"},
                            "content": {"parts": ["Message B1 (New)"]},
                            "create_time": 1675209600.0  # 2023-02-01 00:00:00 UTC
                        },
                        "children": ["id_b_2"]
                    },
                    "id_b_2": {
                        "message": {
                            "id": "id_b_2",
                            "author": {"role": "assistant"},
                            "content": {"parts": ["Message B2 (New)"]},
                            "create_time": 1675209660.0  # 2023-02-01 00:01:00 UTC
                        },
                        "children": []
                    }
                }
            }
        ]

        with open(self.conversations_file, 'w', encoding='utf-8') as f:
            json.dump(self.mock_data, f)

    def tearDown(self):
        # 後始末
        shutil.rmtree(self.test_dir)
        constants.ROOMS_DIR = self.original_rooms_dir

    def test_import_single_thread(self):
        """既存機能の互換性チェック: 単一ID指定でのインポート"""
        room_name = "Single Import Test"
        user_name = "Tester"
        
        # Thread A (id_a_1) をインポート
        # mappingのキーがIDになるため、mock_dataの構造上、最初のキーを取得してテストに使用
        target_id = "id_a_1" 
        
        imported_folder = chatgpt_importer.import_from_chatgpt_export(
            self.conversations_file, target_id, room_name, user_name
        )
        
        self.assertIsNotNone(imported_folder)
        log_path = os.path.join(constants.ROOMS_DIR, imported_folder, "log.txt")
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.read()
            self.assertIn("Message A1 (Old)", content)
            self.assertNotIn("Message B1 (New)", content)

    def test_import_multiple_threads_chronological(self):
        """複数ID指定でのインポートと時系列ソートの検証"""
        room_name = "Multi Import Test"
        user_name = "Tester"
        
        # Thread A (古い) と Thread B (新しい) のIDリスト
        # 逆順（新しい -> 古い）でリストを渡しても、create_timeでソートされることを確認
        target_ids = ["id_b_1", "id_a_1"]
        
        imported_folder = chatgpt_importer.import_from_chatgpt_export(
            self.conversations_file, target_ids, room_name, user_name
        )
        
        self.assertIsNotNone(imported_folder)
        log_path = os.path.join(constants.ROOMS_DIR, imported_folder, "log.txt")
        
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
            # コンテンツが含まれているか
            self.assertIn("Message A1 (Old)", content)
            self.assertIn("Message B1 (New)", content)
            
            # 順序の検証 (A -> B)
            pos_a = content.find("Message A1 (Old)")
            pos_b = content.find("Message B1 (New)")
            
            self.assertTrue(pos_a < pos_b, "Old messages should appear before new messages")

    def test_import_multiple_threads_system_prompt(self):
        """複数スレッド結合時のSystemPrompt検証: 一番古いメッセージが採用されるか"""
        room_name = "System Prompt Test"
        user_name = "Tester"
        
        # Thread B (新しい) -> Thread A (古い) の順で指定
        target_ids = ["id_b_1", "id_a_1"]
        
        imported_folder = chatgpt_importer.import_from_chatgpt_export(
            self.conversations_file, target_ids, room_name, user_name
        )
        
        prompt_path = os.path.join(constants.ROOMS_DIR, imported_folder, "SystemPrompt.txt")
        with open(prompt_path, 'r', encoding='utf-8') as f:
            prompt_content = f.read()
            
            # 一番古い Thread A のメッセージが System Prompt になっているべき
            # タイムスタンプが付与されるようになったため、assertInで確認
            self.assertIn("Message A1 (Old)", prompt_content)
            self.assertIn("2023-01-01", prompt_content)

if __name__ == '__main__':
    unittest.main()
