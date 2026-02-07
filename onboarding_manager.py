import os
import json
import config_manager

# Onboarding States
STATUS_NEW_USER = "new_user"
STATUS_MIGRATED_USER = "migrated_user"
STATUS_ACTIVE_USER = "active_user"

def check_status():
    """
    Check the current user status for onboarding purposes.
    Returns one of the STATUS_* constants.
    """
    # 1. Check if config.json exists
    if not os.path.exists("config.json"):
        return STATUS_NEW_USER
    
    # 2. Check setup_completed flag in config
    # We load it freshly to be sure
    try:
        if os.path.exists("config.json"):
            with open("config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
                
            if config.get("setup_completed", False):
                return STATUS_ACTIVE_USER
            
            # --- [Fix for Existing Users] ---
            # If setup_completed is missing, but we have an API key, assume it's a migrated user.
            # We check for 'gemini_api_key' or 'gemini_api_keys' or 'common_settings.gemini_api_key'
            has_legacy_key = False
            
            # Check top-level single key
            _gemini_api_key = config.get("gemini_api_key")
            if _gemini_api_key and "YOUR_API_KEY" not in _gemini_api_key:
                has_legacy_key = True
            
            # Check dict keys
            if not has_legacy_key and config.get("gemini_api_keys"):
                # If dictionary is not empty and has values other than example
                keys = config.get("gemini_api_keys")
                if isinstance(keys, dict):
                    for k, v in keys.items():
                        if v and "YOUR_API_KEY" not in v:
                            has_legacy_key = True
                            break

            # Check common_settings
            if not has_legacy_key:
                common = config.get("common_settings", {})
                _common_key = common.get("gemini_api_key") if common else None
                if _common_key and "YOUR_API_KEY" not in _common_key:
                    has_legacy_key = True
            
            if has_legacy_key:
                print("[Onboarding] Existing valid configuration detected. Auto-completing setup.")
                mark_setup_completed()
                return STATUS_ACTIVE_USER
            # --------------------------------
                
    except Exception as e:
        print(f"[Onboarding] Error checking status: {e}")
        return STATUS_NEW_USER # Safe fallback

    return STATUS_NEW_USER

def mark_setup_completed():
    """
    Mark the onboarding as completed in config.json.
    """
    config_manager.save_config_if_changed("setup_completed", True)
    print("[Onboarding] Setup marked as completed.")
