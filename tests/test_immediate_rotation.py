
import unittest
from unittest.mock import MagicMock, patch, patch
import sys
import os
import time

# Mock modules
sys.modules["agent.graph"] = MagicMock()
mock_app = MagicMock()
sys.modules["agent.graph"].app = mock_app

mock_config_manager = MagicMock()
sys.modules["config_manager"] = mock_config_manager

sys.modules["room_manager"] = MagicMock()
sys.modules["utils"] = MagicMock()
sys.modules["signature_manager"] = MagicMock()
sys.modules["episodic_memory_manager"] = MagicMock()
sys.modules["tiktoken"] = MagicMock()
sys.modules["google"] = MagicMock()
sys.modules["google.genai"] = MagicMock()
sys.modules["google.genai.errors"] = MagicMock()
sys.modules["langchain_core.messages"] = MagicMock()
sys.modules["langchain_google_genai"] = MagicMock()
sys.modules["langchain_google_genai.chat_models"] = MagicMock()
sys.modules["PIL"] = MagicMock()
sys.modules["filetype"] = MagicMock()
sys.modules["httpx"] = MagicMock()
sys.modules["agent.scenery_manager"] = MagicMock()

# Define Fake Exceptions
class ResourceExhausted(Exception):
    pass

sys.modules["google.api_core"] = MagicMock()
sys.modules["google.api_core.exceptions"] = MagicMock()
sys.modules["google.api_core.exceptions"].ResourceExhausted = ResourceExhausted

class AIMessage:
    def __init__(self, content, **kwargs):
        self.content = content
        self.additional_kwargs = kwargs.get("additional_kwargs", {})
        self.response_metadata = kwargs.get("response_metadata", {})
        self.tool_calls = kwargs.get("tool_calls", [])

sys.modules["langchain_core.messages"].AIMessage = AIMessage
sys.modules["langchain_core.messages"].HumanMessage = MagicMock()
sys.modules["langchain_core.messages"].SystemMessage = MagicMock()

# Import the module to test
import gemini_api

class TestImmediateRotation(unittest.TestCase):
    def setUp(self):
        mock_config_manager.reset_mock()
        mock_app.reset_mock()
        
        self.api_keys = {"key1": "fake1", "key2": "fake2"}
        mock_config_manager.GEMINI_API_KEYS = self.api_keys
        mock_config_manager.get_effective_settings.return_value = {
            "model_name": "gemini-1.5-pro",
            "enable_api_key_rotation": True,
            "api_key_name": "key1"
        }
        mock_config_manager.is_key_exhausted.return_value = False
        mock_config_manager.get_active_provider.return_value = "google"
        
        # Configure room_manager
        sys.modules["room_manager"].get_room_files_paths.return_value = (None, None, None, None, None, None)

    def test_invoke_nexus_agent_stream_fast_rotation(self):
        """Verify that 429 results in fast rotation (low sleep time)"""
        
        exhausted_keys = []
        def mark_exhausted(key):
            exhausted_keys.append(key)
            
        mock_config_manager.mark_key_as_exhausted.side_effect = mark_exhausted
        
        def get_next_key(current_exhausted_key, excluded_keys=None):
            if current_exhausted_key == "key1":
                return "key2"
            return None
            
        mock_config_manager.get_next_available_gemini_key.side_effect = get_next_key
        
        # Simulate app.stream failure then success
        call_count = 0
        def stream_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            print(f"DEBUG app.stream call {call_count}")
            if call_count == 1:
                print("DEBUG Raising ResourceExhausted")
                raise ResourceExhausted("429 Resource exhausted")
            
            yield ("values", {"messages": [AIMessage(content="Success")]})
                
        mock_app.stream.side_effect = stream_side_effect
        mock_app.invoke.side_effect = stream_side_effect
        
        agent_args = {
            "room_to_respond": "test", "api_key_name": "key1", "api_history_limit": "10",
            "debug_mode": False, "history_log_path": None, "user_prompt_parts": [],
            "soul_vessel_room": "soul", "active_participants": [], "active_attachments": [],
            "shared_location_name": "", "shared_scenery_text": "", "season_en": "spring", "time_of_day_en": "day"
        }
        
        start_time = time.time()
        generator = gemini_api.invoke_nexus_agent_stream(agent_args)
        results = []
        for r in generator:
            print(f"DEBUG Result: {r}")
            results.append(r)
        end_time = time.time()
        
        # Duration should be very low (close to 0.1s + overhead)
        duration = end_time - start_time
        print(f"\nRotation Duration: {duration:.4f}s")
        
        self.assertIn("key1", exhausted_keys)
        self.assertTrue(any("Success" in str(r) for r in results))
        # We changed time.sleep(1) to time.sleep(0.1), so duration should be < 0.5s
        self.assertLess(duration, 1.0, "Rotation took too long!")

if __name__ == "__main__":
    unittest.main()
