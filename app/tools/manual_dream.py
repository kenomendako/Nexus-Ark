import os
import sys
import argparse

# プロジェクトルートをパスに追加
sys.path.append(os.path.abspath("."))

import config_manager
import constants
from dreaming_manager import DreamingManager

def main():
    parser = argparse.ArgumentParser(description="Nexus Ark Manual Dreaming Tool")
    parser.add_argument("--room", type=str, required=True, help="ルーム名（フォルダ名）")
    parser.add_argument("--insight-only", action="store_true", help="夢日記（洞察）の生成のみを行い、重い記憶統合をスキップする")
    parser.add_argument("--level", type=int, default=1, choices=[1, 2, 3], help="省察レベル (1:日次, 2:週次, 3:月次)")
    
    args = parser.parse_args()
    
    room_name = args.room
    
    # 設定の読み込み
    config_manager.load_config()
    effective_settings = config_manager.get_effective_settings(room_name)
    
    # APIキーの取得
    api_key_name = effective_settings.get("api_key_name") or config_manager.CONFIG_GLOBAL.get("last_api_key_name")
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        print(f"Error: 有効なAPIキーが見つかりません (Key name: {api_key_name})")
        sys.exit(1)
        
    print(f"--- [Manual Dreaming] Room: {room_name} ---")
    dm = DreamingManager(room_name, api_key)
    
    if args.insight_only:
        print("Mode: Insight Only (Fast)")
        result = dm.dream_insight_only()
    else:
        print(f"Mode: Full (Level {args.level})")
        result = dm.dream(reflection_level=args.level)
        
    print("\n--- Result ---")
    print(result)
    print("--------------")

if __name__ == "__main__":
    main()
