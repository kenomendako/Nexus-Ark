# tools/knowledge_tools.py

import os
from pathlib import Path
from langchain_core.tools import tool
import traceback
import shutil
import tempfile
# 循環参照を避けるため、必要なモジュールは関数内でインポートする
import constants
import config_manager
import rag_manager

@tool
def search_knowledge_base(query: str, room_name: str, api_key: str = None) -> str:
    """
    AI自身の長期的な知識ベース（Knowledge Base）に保存されている、外部から与えられたドキュメント（マニュアル、設定資料など）の内容について、自然言語で検索する。
    AI自身の記憶や過去の会話ではなく、普遍的な事実や情報を調べる場合に使用する。
    query: 検索したい内容を記述した、自然言語の質問文（例：「Nexus Arkの基本的な使い方は？」）。
    """
    
    # 1. 前提条件のチェック
    if not room_name:
        return "【エラー】検索対象のルームが指定されていません。"
    if not query:
        return "【エラー】検索クエリが指定されていません。"

    # 2. APIキーの準備
    if not api_key:
        api_key_name = config_manager.initial_api_key_name_global
        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        return f"【エラー】知識ベースの検索に必要なAPIキーが無効です。"
        
    try:
        # --- [RAGManagerを使用した新しい検索ロジック] ---
        manager = rag_manager.RAGManager(room_name, api_key)
        
        # 検索実行 (上位4件取得)
        docs = manager.search(query, k=4)
        
        if not docs:
            return f"【検索結果】知識ベースから「{query}」に関連する情報は見つかりませんでした。"
        
        # 結果を整形して返す
        result_parts = [f'【知識ベースからの検索結果：「{query}」】\n']
        for doc in docs:
            # ログファイルからのヒットか、知識ドキュメントからのヒットかを判別しやすくする
            source_name = os.path.basename(doc.metadata.get("source", "不明なソース"))
            doc_type = doc.metadata.get("type", "unknown")
            
            # ログの場合は日付などもメタデータにあれば表示したいが、まずはシンプルに
            header = f"[出典: {source_name}]"
            if doc_type == "log_archive" or doc_type == "current_log":
                header = f"[出典: 過去の会話ログ ({source_name})]"

            elif doc_type == "episodic_memory":
                date = doc.metadata.get("date", "")
                header = f"[出典: エピソード記憶（要約） - {date}]"

            result_parts.append(f"- {header}\n  {doc.page_content}")

        final_result = "\n".join(result_parts)
        return final_result

    except Exception as e:
        print(f"--- [知識ベース検索エラー] ---")
        traceback.print_exc()
        return f"【エラー】知識ベースの検索中に予期せぬエラーが発生しました: {e}"