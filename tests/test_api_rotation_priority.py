import unittest
from unittest.mock import MagicMock, patch
import os
import sys

# Add current dir to path
sys.path.append(os.getcwd())

import config_manager

class TestRotationPriority(unittest.TestCase):
    
    def setUp(self):
        # Reset global state for each test
        config_manager.GEMINI_API_KEYS = {
            "free1": "AIzaFree1",
            "free2": "AIzaFree2",
            "paid1": "AIzaPaid1"
        }
        config_manager.GEMINI_KEY_STATES = {}
        
    @patch('config_manager.load_config_file')
    def test_free_key_priority(self, mock_load):
        # Case: last_api_key_name is a free key, all are healthy
        mock_load.return_value = {
            "last_api_key_name": "free1",
            "paid_api_key_names": ["paid1"],
            "gemini_api_keys": config_manager.GEMINI_API_KEYS
        }
        
        # Should return free1
        self.assertEqual(config_manager.get_next_available_gemini_key(), "free1")
        
    @patch('config_manager.load_config_file')
    @patch('config_manager.is_key_exhausted')
    def test_rotation_among_free_keys(self, mock_exhausted, mock_load):
        # Case: free1 is exhausted, free2 is healthy, paid1 is healthy
        mock_load.return_value = {
            "last_api_key_name": "free1",
            "paid_api_key_names": ["paid1"],
            "gemini_api_keys": config_manager.GEMINI_API_KEYS
        }
        
        def exhausted_side_effect(name):
            return name == "free1"
        mock_exhausted.side_effect = exhausted_side_effect
        
        # Should return free2, not paid1
        self.assertEqual(config_manager.get_next_available_gemini_key(), "free2")

    @patch('config_manager.load_config_file')
    @patch('config_manager.is_key_exhausted')
    def test_fallback_to_paid_only_if_all_free_exhausted(self, mock_exhausted, mock_load):
        # Case: all free keys are exhausted
        mock_load.return_value = {
            "last_api_key_name": "free1",
            "paid_api_key_names": ["paid1"],
            "gemini_api_keys": config_manager.GEMINI_API_KEYS
        }
        
        def exhausted_side_effect(name):
            return name in ["free1", "free2"]
        mock_exhausted.side_effect = exhausted_side_effect
        
        # Should return paid1
        self.assertEqual(config_manager.get_next_available_gemini_key(), "paid1")

    @patch('config_manager.load_config_file')
    @patch('config_manager.is_key_exhausted')
    def test_ignore_paid_even_if_it_was_last_used(self, mock_exhausted, mock_load):
        # Case: last_api_key_name is a paid key, but a free key is healthy
        mock_load.return_value = {
            "last_api_key_name": "paid1",
            "paid_api_key_names": ["paid1"],
            "gemini_api_keys": config_manager.GEMINI_API_KEYS
        }
        mock_exhausted.return_value = False
        
        # Should return free1 (alphabetical order or just first free)
        # Note: dict keys order might vary, but any free key is better than paid
        result = config_manager.get_next_available_gemini_key()
        self.assertIn(result, ["free1", "free2"])

if __name__ == "__main__":
    unittest.main()
