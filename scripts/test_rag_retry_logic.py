
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.getcwd())

from rag_manager import RAGManager
from langchain_core.documents import Document

class TestRAGRetry(unittest.TestCase):
    
    @patch('rag_manager.config_manager')
    def test_search_retry_flow(self, mock_config):
        # Setup mocks
        mock_config.get_effective_settings.return_value = {"embedding_mode": "api"}
        mock_config.get_key_name_by_value.return_value = "TEST_KEY_1"
        
        # Initialize Manager
        rm = RAGManager("test_room", "fake_key")
        
        # Mock DB
        mock_db = MagicMock()
        
        # Define side effects:
        # 1. First call triggers 429
        # 2. Second call succeeds (Dynamic retry)
        # 3. Third call succeeds (Static) - since we mock both DBs with same object
        error_429 = Exception("429 ResourceExhausted: Quota exceeded")
        success_result = [(Document(page_content="Success", metadata={"arousal": 0.5}), 0.1)]
        
        mock_db.similarity_search_with_score.side_effect = [error_429, success_result, success_result]
        
        # Patch internals
        with patch.object(rm, '_safe_load_index', return_value=mock_db):
            with patch.object(rm, '_rotate_api_key', return_value=True) as mock_rotate:
                print("\n--- Starting Retry Test ---")
                results = rm.search("test query", k=1)
                
                # Verifications
                print(f"Rotation called: {mock_rotate.called}")
                print(f"Search call count: {mock_db.similarity_search_with_score.call_count}")
                print(f"Results: {len(results)}")
                
                self.assertTrue(mock_rotate.called, "Should attempt to rotate key on 429")
                self.assertEqual(mock_db.similarity_search_with_score.call_count, 3, 
                                 "Should call: 1.Dyn(Fail) -> 2.Dyn(Ok) -> 3.Static(Ok)")
                self.assertEqual(len(results), 2, "Should return results from both dynamic and static (2 items)")
                print("--- Test Passed ---")

    def test_retry_fail_if_rotation_fails(self):
        """Rotation fails -> Should catch exception and not retry loop"""
        with patch('rag_manager.config_manager') as mock_config:
            mock_config.get_effective_settings.return_value = {"embedding_mode": "api"}
            
            rm = RAGManager("test_room", "fake_key")
            mock_db = MagicMock()
            mock_db.similarity_search_with_score.side_effect = Exception("429 ResourceExhausted")
            
            with patch.object(rm, '_safe_load_index', return_value=mock_db):
                with patch.object(rm, '_rotate_api_key', return_value=False): # Fail rotation
                    print("\n--- Starting Rotation Fail Test ---")
                    results = rm.search("test query")
                    print(f"Results (should be empty): {results}")
                    self.assertEqual(len(results), 0)

if __name__ == '__main__':
    unittest.main()
