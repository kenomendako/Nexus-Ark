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
import spacy
import networkx as nx
import constants
import utils

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Graceful Shutdown ---
shutdown_flag = False
def signal_handler(signum, frame):
    global shutdown_flag
    logger.warning(f"Shutdown signal {signum} received. Saving progress...")
    shutdown_flag = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# --- spaCy Model Loading ---
try:
    nlp = spacy.load("ja_core_news_lg")
    logger.info("spaCy Japanese model 'ja_core_news_lg' loaded successfully.")
except OSError:
    logger.error("spaCy model 'ja_core_news_lg' not found. Please run 'python -m spacy download ja_core_news_lg' to download it.")
    sys.exit(1)


def chunk_log(log_content: str) -> List[str]:
    """
    Splits a clean log file content into meaningful conversation chunks.
    """
    if not log_content:
        return []
    messages = log_content.strip().split('\n\n')
    chunk_size = 4
    chunks = ['\n\n'.join(messages[i:i+chunk_size]) for i in range(0, len(messages), chunk_size)]
    logger.info(f"Log content chunked into {len(chunks)} parts.")
    return chunks


def main(room_name: str):
    G = nx.Graph()
    pending_analysis_tasks = []
    rag_data_path = Path("characters") / room_name / "rag_data"
    graph_path = rag_data_path / "knowledge_graph.graphml"
    analysis_file_path = rag_data_path / "pending_analysis.json"

    try:
        # --- Setup ---
        log_source_path = Path("characters") / room_name / "log_import_source"
        log_source_path.mkdir(parents=True, exist_ok=True)
        rag_data_path.mkdir(parents=True, exist_ok=True)

        if graph_path.exists():
            os.remove(graph_path)
            logger.info("Removed existing knowledge graph to rebuild.")
        if analysis_file_path.exists():
            os.remove(analysis_file_path)
            logger.info("Removed existing analysis file.")

        G = nx.Graph()
        logger.info("Created a new, empty knowledge graph.")

        # --- Entity & Initial Edge Extraction ---
        log_files = [f for f in log_source_path.glob("*.txt")]
        logger.info(f"Found {len(log_files)} log files.")

        all_chunks = []
        for log_file_path in log_files:
            if shutdown_flag: break
            logger.info(f"Reading and parsing log file: {log_file_path.name}")
            log_entries = utils.load_chat_log(str(log_file_path))
            full_conversation_text = "\n\n".join([entry["content"] for entry in log_entries if "content" in entry])
            all_chunks.extend(chunk_log(full_conversation_text))

        for i, chunk in enumerate(all_chunks):
            if shutdown_flag: break
            logger.info(f"Processing chunk {i+1}/{len(all_chunks)} for skeleton creation.")
            doc = nlp(chunk)
            entities = list(set([ent.text for ent in doc.ents if ent.label_ in ["PERSON", "ORG", "GPE", "FAC", "LOC"]]))

            if len(entities) >= 2:
                for entity in entities:
                    if not G.has_node(entity): G.add_node(entity)
                for j in range(len(entities)):
                    for k in range(j + 1, len(entities)):
                        u, v = entities[j], entities[k]
                        if not G.has_edge(u, v):
                            G.add_edge(u, v, relation="related_to")
                            task = {"entity1": u, "entity2": v, "chunk": chunk}
                            pending_analysis_tasks.append(task)

        if shutdown_flag:
             logger.warning("Shutdown signal received during chunk processing.")

    finally:
        # --- Save Skeleton Graph and Analysis "To-Do" List ---
        logger.info("Saving skeleton graph and pending analysis tasks...")

        nx.write_graphml(G, graph_path)
        logger.info(f"Skeleton knowledge graph saved to {graph_path}")

        with open(analysis_file_path, 'w', encoding='utf-8') as f:
            json.dump(pending_analysis_tasks, f, indent=2, ensure_ascii=False)
        logger.info(f"Pending analysis tasks saved to {analysis_file_path}")

        if shutdown_flag:
            logger.warning("Importer stopped due to shutdown signal. Partial files were saved.")
        else:
            logger.info("Skeleton creation process completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nexus Ark Knowledge Graph Skeleton Creator")
    parser.add_argument("room_name", help="The name of the room to process.")
    args = parser.parse_args()
    main(args.room_name)
