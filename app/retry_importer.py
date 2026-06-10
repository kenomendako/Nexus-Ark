# retry_importer.py

import os
import sys
import json
import argparse
import time
import re
from typing import List, Dict

# Nexus Arkの既存モジュールを正しくインポートする
import config_manager
import room_manager

# --- 定数 ---
ERROR_LOG_FILE = "importer_errors.log"
SUCCESS_LOG_FILE = "retry_success.log"

def parse_error_log() -> List[Dict]:
    """importer_errors.log を解析し、会話ペアのリストを抽出する。"""
    if not os.path.exists(ERROR_LOG_FILE):
        print(f"エラー: {ERROR_LOG_FILE} が見つかりません。")
        return []

    with open(ERROR_LOG_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # JSONオブジェクトを抽出するための正規表現
    # "Pair Content:" から始まり、次の "---" の前までを対象とする
    json_blocks = re.findall(r"Pair Content:\n(\[.*?\])", content, re.DOTALL)

    pairs = []
    for block in json_blocks:
        try:
            pair = json.loads(block)
            if isinstance(pair, list) and len(pair) == 2:
                pairs.append(pair)
        except json.JSONDecodeError:
            print(f"警告: ログ内のJSONブロックの解析に失敗しました:\n{block}")
            continue

    return pairs

def log_success(pair: List[Dict]):
    """正常にインポートできたペアを成功ログに記録する。"""
    with open(SUCCESS_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"--- SUCCESS at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        json.dump(pair, f, indent=2, ensure_ascii=False)
        f.write("\n\n")

def main():
    # ... (argparseの定義は変更なし) ...
    parser = argparse.ArgumentParser(description="Nexus Arkのインポートエラーログから、失敗した会話ペアを対話的に再インポートするツール")
    parser.add_argument("--character", required=True, help="対象のルーム名（MemOSの初期化に必要）")
    args = parser.parse_args()

    # ▼▼▼【ここから下のブロックをまるごと置き換え】▼▼▼
    print("\n" + "="*60)
    print("!!! [重要なお知らせ] !!!")
    print("Nexus Arkの記憶システムは、現在新しい『Cognee』システムへの移行作業中です。")
    print("そのため、このリトライインポータースクリプトは一時的に無効化されています。")
    print("="*60 + "\n")
    # ▲▲▲【置き換えここまで】▲▲▲

if __name__ == "__main__":
    main()
