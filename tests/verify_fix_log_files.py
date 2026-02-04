
import sys
import os
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append("/home/baken/nexus_ark")

# Mock dependencies to avoid side effects
sys.modules["constants"] = MagicMock()
sys.modules["config_manager"] = MagicMock()
sys.modules["utils"] = MagicMock()
sys.modules["room_manager"] = MagicMock()
sys.modules["llm_factory"] = MagicMock()
sys.modules["session_arousal_manager"] = MagicMock()

# Import the module to test
import episodic_memory_manager
import utils
import room_manager

def test_fix():
    print("Testing fix for NameError in EpisodicMemoryManager.update_memory...")
    
    # Setup mocks
    room_manager.get_room_config.return_value = {"user_display_name": "User", "agent_display_name": "AI"}
    room_manager.get_room_files_paths.return_value = ("dummy_log.txt", None, None, None, None, None)
    
    # Mock load_chat_log to return some dummy messages
    utils.load_chat_log.return_value = [{"role": "USER", "content": "Hello"}]
    
    # Instantiate manager
    manager = episodic_memory_manager.EpisodicMemoryManager("test_room")
    
    # We want to check if the code reaches past the print statement without error
    # The print statement is right after load_chat_log
    
    try:
        # We don't want to actually run the full update loop as it needs more mocking
        # so we can mock os.path or something else to break early, 
        # OR we can just catch the exception if it fails LATER than the print.
        # But simply calling it and checking it DOESN'T raise NameError is enough.
        
        # To avoid running into further logic that might fail due to heavy mocking,
        # let's wrap the method or just run it and expect a different error or success.
        # The key is to NOT get "NameError: name 'log_files' is not defined".
        
        manager.update_memory("dummy_key")
        print("update_memory finished without critical errors.")
        
    except NameError as e:
        if "log_files" in str(e):
            print("❌ FAILED: NameError 'log_files' still exists!")
            sys.exit(1)
        else:
            print(f"❌ NameError (unrelated): {e}")
            sys.exit(1)
    except Exception as e:
        # If we hit another error, it means we passed the print statement (which is early in the function)
        print(f"✅ Passed the 'log_files' check! (Stopped by later error: {type(e).__name__}: {e})")

if __name__ == "__main__":
    test_fix()
