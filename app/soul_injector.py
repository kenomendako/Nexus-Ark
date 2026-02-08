# --- ステップ1: 必要な標準ライブラリと設定マネージャーをインポート ---
import os
import sys
import argparse
import logging
import json
import time
import signal
import traceback
from typing import List
from pathlib import Path

# --- ステップ2: Nexus Arkのコア設定を、何よりも先に読み込む ---
import config_manager
config_manager.load_config()

# --- ステップ3: 設定が完了した後で、外部ライブラリをインポート ---
import google.genai as genai
import networkx as nx
import constants

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Graceful Shutdown ---
shutdown_flag = False
def signal_handler(signum, frame):
    global shutdown_flag
    logger.warning(f"Shutdown signal {signum} received. Finishing current task and saving progress...")
    shutdown_flag = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def get_relation_from_gemini(client: 'genai.client.Client', chunk: str, entity1: str, entity2: str) -> str:
    """
    Gemini APIを呼び出して、2つのエンティティ間の関係性を推論する。
    """
    model_name = constants.INTERNAL_PROCESSING_MODEL
    prompt = f"""
    あなたは、二つのエンティティ間の関係性を定義する、高精度の言語分析AIです。
    【コンテキストとなる会話】
    ---
    {chunk}
    ---
    【あなたのタスク】
    上記の会話において、「{entity1}」と「{entity2}」の間に存在する最も的確な関係性を、以下の選択肢から一つだけ選び、その単語のみを返答してください。
    【関係性の選択肢】
    IS_IN (〜にいる), GOES_TO (〜へ行く), TALKS_ABOUT (〜について話す), LIKES (〜を好む), DISLIKES (〜を嫌う), HAS (〜を持つ)
    【最重要ルール】
    - あなたの思考や挨拶は不要です。選択肢の中から最も適切な単語、ただ一つだけを出力してください。
    - どの選択肢も当てはまらない場合は、`UNKNOWN`とだけ出力してください。
    """
    try:
        response = client.models.generate_content(
            model=f"models/{model_name}",
            contents=[prompt]
        )
        relation = response.text.strip()
        valid_relations = ["IS_IN", "GOES_TO", "TALKS_ABOUT", "LIKES", "DISLIKES", "HAS", "UNKNOWN"]
        return relation if relation in valid_relations else "UNKNOWN"
    except Exception as e:
        logging.error(f"Gemini API call failed for entities ('{entity1}', '{entity2}'): {e}")
        return "API_ERROR_RETRY"


def main(room_name: str):
    logger.info("--- Soul Injector Process Started ---")

    # --- API Key and Client Initialization ---
    try:
        selected_key_name = config_manager.initial_api_key_name_global
        api_key = config_manager.GEMINI_API_KEYS.get(selected_key_name)
        if not api_key or api_key == "YOUR_API_KEY_HERE":
            logger.error(f"FATAL: The selected API key '{selected_key_name}' is invalid or a placeholder.")
            return
        gemini_client = genai.Client(api_key=api_key)
        logger.info(f"Gemini API client created successfully for key '{selected_key_name}'.")
    except Exception as e:
        logger.error(f"An error occurred during API client initialization: {e}")
        traceback.print_exc()
        return

    # --- File and Progress Setup ---
    G = None
    progress = None
    rag_data_path = Path("characters") / room_name / "rag_data"
    graph_path = rag_data_path / "knowledge_graph.graphml"
    analysis_file_path = rag_data_path / "pending_analysis.json"
    progress_file_path = rag_data_path / "injector_progress.json"

    try:
        if not graph_path.exists() or not analysis_file_path.exists():
            logger.error("Skeleton graph or analysis file not found. Please run batch_importer.py first.")
            return

        G = nx.read_graphml(graph_path)
        with open(analysis_file_path, 'r', encoding='utf-8') as f:
            tasks = json.load(f)

        progress = {}
        if progress_file_path.exists():
            with open(progress_file_path, 'r', encoding='utf-8') as f:
                progress = json.load(f)

        start_index = progress.get('last_processed_task_index', -1) + 1
        logger.info(f"Found {len(tasks)} tasks. Resuming from index {start_index}.")

        # --- Relationship Enrichment ---
        for i, task in enumerate(tasks[start_index:], start=start_index):
            if shutdown_flag: break
            u, v, chunk = task["entity1"], task["entity2"], task["chunk"]
            logger.info(f"Analyzing relation for ({u}, {v}) - Task {i+1}/{len(tasks)}")

            retry_count, max_retries, relation = 0, 5, "API_ERROR_RETRY"
            while retry_count < max_retries and not shutdown_flag:
                relation = get_relation_from_gemini(gemini_client, chunk, u, v)
                if relation != "API_ERROR_RETRY": break
                retry_count += 1
                time.sleep(5 * (2 ** (retry_count - 1)))

            if relation != "UNKNOWN" and G.has_edge(u, v):
                G[u][v]['relation'] = relation

            progress['last_processed_task_index'] = i

        if not shutdown_flag:
            logger.info("All tasks completed. Cleaning up.")
            if os.path.exists(analysis_file_path): os.remove(analysis_file_path)
            if os.path.exists(progress_file_path): os.remove(progress_file_path)

    finally:
        if G is not None:
            nx.write_graphml(G, graph_path)
            logger.info(f"Knowledge graph saved to {graph_path}")
        if progress and shutdown_flag:
            with open(progress_file_path, 'w', encoding='utf-8') as f:
                json.dump(progress, f, indent=2, ensure_ascii=False)
            logger.info(f"Saved injector progress to {progress_file_path}")

        logger.info("--- Soul Injector Process Finished ---")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nexus Ark Knowledge Graph Soul Injector")
    parser.add_argument("room_name", help="The name of the room to process.")
    args = parser.parse_args()
    main(args.room_name)
