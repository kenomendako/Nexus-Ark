import google.genai as genai
import config_manager
import os

def check_models():
    config = config_manager.load_config_file()
    api_key_name = config.get("image_generation_api_key_name", "kenokaicoo")
    api_key = config.get("gemini_api_keys", {}).get(api_key_name)
    
    if not api_key:
        print(f"Error: API key for {api_key_name} not found.")
        return

    print(f"Using API Key: {api_key_name} ({api_key[:4]}...{api_key[-4:]})")
    client = genai.Client(api_key=api_key)
    
    print("Listing models...")
    try:
        models = client.models.list()
        for m in models:
            if "image" in m.name or "imagen" in m.name:
                print(f"Found Image Model: {m.name} (DisplayName: {m.display_name})")
    except Exception as e:
        print(f"Error listing models: {e}")

    test_model = "gemini-2.5-flash-image"
    print(f"\nChecking specific model: {test_model}")
    try:
        m_info = client.models.get(model=test_model)
        print(f"Model {test_model} exists! DisplayName: {m_info.display_name}")
    except Exception as e:
        print(f"Model {test_model} NOT found or error: {e}")

if __name__ == "__main__":
    check_models()
