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

@tool
def search_knowledge_base(query: str, room_name: str) -> str:
    """
    AI自身の長期的な知識ベース（Knowledge Base）に保存されている、外部から与えられたドキュメント（マニュアル、設定資料など）の内容について、自然言語で検索する。
    AI自身の記憶や過去の会話ではなく、普遍的な事実や情報を調べる場合に使用する。
    query: 検索したい内容を記述した、自然言語の質問文（例：「Nexus Arkの基本的な使い方は？」）。
    """
    # LangChainのモジュールは重いので、必要になってからインポート
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    from langchain_community.vectorstores import FAISS

    # 1. 前提条件のチェック
    if not room_name:
        return "【エラー】検索対象のルームが指定されていません。"
    if not query:
        return "【エラー】検索クエリが指定されていません。"

    # 2. 索引（FAISSインデックス）のパスを確認
    index_path = Path(constants.ROOMS_DIR) / room_name / "rag_data" / "faiss_index"
    if not index_path.exists() or not os.listdir(str(index_path)):
        return "【情報】このルームには、まだ知識ベースの索引が構築されていません。UIから索引を作成してください。"

    # 3. APIキーとエンベディングモデルの準備
    api_key_name = config_manager.initial_api_key_name_global
    api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        return f"【エラー】知識ベースの検索に必要なAPIキー「{api_key_name}」が無効です。"

    try:
        embeddings = GoogleGenerativeAIEmbeddings(
            model=constants.EMBEDDING_MODEL,
            google_api_key=api_key,
            task_type="retrieval_query"
        )

        # 4. FAISSインデックスをロードして検索を実行 (日本語パス対応)
        docs = []
        # 日本語パス問題を回避するため、ASCIIパスの一時ディレクトリにインデックスをコピーして読み込む
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_index_path = Path(temp_dir) / "faiss_index"
            shutil.copytree(str(index_path), str(temp_index_path))

            # 一時ディレクトリからFAISSインデックスをロード
            db = FAISS.load_local(
                str(temp_index_path),
                embeddings,
                allow_dangerous_deserialization=True
            )
            docs = db.similarity_search(query, k=3) # 上位3件を取得
        # ▲▲▲【置き換えはここまで】▲▲▲

        if not docs:
            return f"【検索結果】知識ベースから「{query}」に関連する情報は見つかりませんでした。"
        
        # 5. 結果を整形して返す
        result_parts = [f'【知識ベースからの検索結果：「{query}」】\n']
        for doc in docs:
            source = doc.metadata.get("source", "不明なドキュメント")
            result_parts.append(f"- [出典: {os.path.basename(source)}]\n  {doc.page_content}")

        final_result = "\n".join(result_parts)
        final_result += "\n\n**この検索タスクは完了しました。これから検索するというような前置きはせず、**見つかった情報を元にユーザーの質問に答えてください。"
        return final_result

    except Exception as e:
        print(f"--- [知識ベース検索エラー] ---")
        traceback.print_exc()
        return f"【エラー】知識ベースの検索中に予期せぬエラーが発生しました: {e}"