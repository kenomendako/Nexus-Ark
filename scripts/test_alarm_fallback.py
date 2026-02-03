
import sys
import os
import unittest
from unittest.mock import MagicMock, patch
from io import StringIO

# Add parent directory to path to import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import alarm_manager
import gemini_api

class TestAlarmFallback(unittest.TestCase):

    def setUp(self):
        # Mock necessary modules
        self.mock_config_manager = patch('alarm_manager.config_manager').start()
        self.mock_room_manager = patch('alarm_manager.room_manager').start()
        self.mock_utils = patch('alarm_manager.utils').start()
        self.mock_gemini_api = patch('alarm_manager.gemini_api').start()
        self.mock_notification = patch('alarm_manager.send_notification').start() # Mock notification to prevent actual calls
        
        # FIX: Ensure alarm_manager uses the REAL exception class for catching
        self.mock_gemini_api.ResourceExhausted = gemini_api.ResourceExhausted
        
        # FIX: Mock time to avoid sleep delays
        self.mock_time = patch('alarm_manager.time').start()

        # Setup default mock/return values
        self.mock_config_manager.get_current_global_model.return_value = "gemini-test"
        self.mock_utils._get_current_time_context.return_value = ("Spring", "Morning")
        self.mock_utils.remove_thoughts_from_text.side_effect = lambda x: x # Passthrough
        
        # Setup room file paths mock
        self.mock_log_file = MagicMock()
        self.mock_room_manager.get_room_files_paths.return_value = (self.mock_log_file, None, None, None, None, None)

        self.alarm_config = {
            "id": "test_alarm_id",
            "character": "TestRoom",
            "time": "12:00",
            "context_memo": "Test Alarm",
            "enabled": True
        }

    def tearDown(self):
        patch.stopall()

    def test_resource_exhausted_fallback(self):
        """Test that ResourceExhausted triggers the API limit message."""
        # Setup invoke_nexus_agent_stream to raise ResourceExhausted
        self.mock_gemini_api.invoke_nexus_agent_stream.side_effect = gemini_api.ResourceExhausted("Quota exceeded")
        
        # Capture stdout to check for print statements (optional, but good for debugging)
        captured_output = StringIO()
        sys.stdout = captured_output

        try:
            alarm_manager.trigger_alarm(self.alarm_config, "test_key")
        except Exception:
            pass # We expect handled exceptions or just print output
        
        sys.stdout = sys.__stdout__

        # Check that send_notification was called with the API limit message
        self.mock_notification.assert_called()
        args, _ = self.mock_notification.call_args
        message_text = args[1]
        
        self.assertIn("APIの利用上限に達したため", message_text)
        print("\n[SUCCESS] ResourceExhausted triggered API limit message.")

    def test_empty_response_fallback(self):
        """Test that empty response (no AIMessage) triggers a DIFFERENT fallback message."""
        # Setup invoke_nexus_agent_stream to return an empty state or just finish without yielding AIMessage
        # Simulating a generator that yields final state but no AIMessage content
        final_state = {
            "messages": [], # No messages produced
            "model_name": "gemini-test"
        }
        
        # Mock generator behavior
        def mock_stream(*args, **kwargs):
            yield "values", final_state

        self.mock_gemini_api.invoke_nexus_agent_stream.side_effect = mock_stream
        
        # Capture stdout
        captured_output = StringIO()
        sys.stdout = captured_output

        alarm_manager.trigger_alarm(self.alarm_config, "test_key")
        
        sys.stdout = sys.__stdout__

        # Check notification content
        self.mock_notification.assert_called()
        args, _ = self.mock_notification.call_args
        message_text = args[1]

        # Verify correct fallback message for empty response
        self.assertNotIn("APIの利用上限に達したため", message_text, "Should not blame API limits for empty response")
        self.assertIn("AIからの応答がありませんでした", message_text)
        
        print("\n[SUCCESS] Empty response handled correctly (not blamed on API).")

if __name__ == '__main__':
    unittest.main()
