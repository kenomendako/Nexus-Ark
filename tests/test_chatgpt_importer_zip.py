
import unittest
import json
import tempfile
import os
import shutil
import zipfile
import sys

# プロジェクトルートをパスに追加
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import chatgpt_importer
import constants

class TestChatGPTImporterZip(unittest.TestCase):

    def setUp(self):
        # テスト用の一次ディレクトリを作成
        self.test_dir = tempfile.mkdtemp()
        self.original_rooms_dir = constants.ROOMS_DIR
        constants.ROOMS_DIR = self.test_dir

        # ダミーのconversations.jsonを作成
        self.json_content = [
            {
                "title": "Zip Thread",
                "mapping": {
                    "id_zip_1": {
                        "message": {
                            "id": "id_zip_1",
                            "author": {"role": "user"},
                            "content": {"parts": ["Message inside ZIP"]},
                            "create_time": 1672531200.0
                        },
                        "children": []
                    }
                }
            }
        ]
        
        self.json_file_path = os.path.join(self.test_dir, 'conversations.json')
        with open(self.json_file_path, 'w', encoding='utf-8') as f:
            json.dump(self.json_content, f)

        # ZIPファイルを作成
        self.zip_file_path = os.path.join(self.test_dir, 'conversations.zip')
        with zipfile.ZipFile(self.zip_file_path, 'w') as zipf:
            zipf.write(self.json_file_path, arcname='conversations.json')

    def tearDown(self):
        # 後始末
        shutil.rmtree(self.test_dir)
        constants.ROOMS_DIR = self.original_rooms_dir

    def test_resolve_conversations_file_path_zip(self):
        """ZIPファイルを渡してJSONパスが返ってくるか検証"""
        resolved_path = chatgpt_importer.resolve_conversations_file_path(self.zip_file_path)
        
        self.assertNotEqual(resolved_path, self.zip_file_path)
        self.assertTrue(resolved_path.endswith('conversations.json'))
        self.assertTrue(os.path.exists(resolved_path))

    def test_import_from_zip(self):
        """ZIPファイルを直接 import_from_chatgpt_export に渡してインポートできるか検証"""
        room_name = "Zip Import Test"
        user_name = "Tester"
        target_id = "id_zip_1"
        
        imported_folder = chatgpt_importer.import_from_chatgpt_export(
            self.zip_file_path, target_id, room_name, user_name
        )
        
        self.assertIsNotNone(imported_folder)
        log_path = os.path.join(constants.ROOMS_DIR, imported_folder, "log.txt")
        
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.read()
            self.assertIn("Message inside ZIP", content)

if __name__ == '__main__':
    unittest.main()
