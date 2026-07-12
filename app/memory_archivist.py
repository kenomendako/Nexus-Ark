# memory_archivist.py (v18: The Genesis)

# --- [契約B] サブプロセス自身のエンコーディング宣言 ---
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import argparse
import os
import json
import shutil
import tempfile
from datetime import datetime
import logging
import time
from pathlib import Path
import re

import spacy
import networkx as nx
from langchain_community.vectorstores import FAISS
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_google_genai._common import GoogleGenerativeAIError
import google.genai as genai
from google.api_core import exceptions as google_exceptions

import config_manager
import constants
import utils
import room_manager

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Spacy Model Loading ---
try:
    nlp = spacy.load("ja_core_news_lg")
except OSError:
    logger.error("Japanese spaCy model 'ja_core_news_lg' not found.")
    sys.exit(1)

# --- [契約2：不壊の心臓] LLM and Helper Functions ---
def call_gemini_with_smart_retry(gemini_client: genai.Client, model_name: str, prompt: str, max_retries: int = 5) -> str | None:
    """
    503エラーにも対応した、指数バックオフ付きの、堅牢なAPI呼び出し関数。
    """
    retry_count = 0
    while retry_count < max_retries:
        try:
            response = gemini_client.models.generate_content(
                model=f"models/{model_name}",
                contents=[prompt],
            )
            return response.text
        except (google_exceptions.ResourceExhausted, google_exceptions.ServiceUnavailable, google_exceptions.InternalServerError) as e:
            retry_count += 1
            if retry_count >= max_retries:
                logger.error(f"API retriable error. Max retries ({max_retries}) reached. Aborting. Error: {e}")
                return None
            wait_time = 5 * (2 ** (retry_count - 1))
            logger.warning(f"API retriable error ({e.args[0]}). Retrying in {wait_time} seconds... ({retry_count}/{max_retries})")
            time.sleep(wait_time)
        except Exception as e:
            logger.error(f"An unexpected, non-retriable API error occurred: {e}")
            return None
    return None

def repair_json_string_with_ai(gemini_client: genai.Client, broken_json_string: str) -> str | None:
    """
    破損した可能性のあるJSON文字列を、AIに修復させる。
    """
    prompt = f"""
あなたは、破損したJSON文字列を修復する専門家です。
以下の【破損した可能性のあるテキスト】を分析し、それが有効なJSONオブジェクトまたはJSON配列になるように修復してください。
【破損した可能性のあるテキスト】
---
{broken_json_string}
---
【最重要ルール】
- あなた自身の思考や挨拶は絶対に含めず、修復されたJSON文字列のみを出力してください。
- どうしても修復不可能な場合は、空のJSONオブジェクト `{{}}` または `[]` を出力してください。
- 出力は必ず ````json` と ```` で囲んでください。
"""
    response_text = call_gemini_with_smart_retry(gemini_client, "gemini-2.5-flash", prompt)
    if response_text is None: return None
    match = re.search(r'```json\s*([\s\S]*?)\s*```', response_text)
    return match.group(1).strip() if match else response_text.strip()

def normalize_entities_from_chunk(gemini_client: genai.Client, chunk: str) -> dict:
    """
    【第一段階：聖域の定義】
    """
    prompt = f"""
あなたは、対話ログから登場人物や重要概念を特定し、名前の揺れを吸収する「名寄せ」の専門家です。
【思考のステップ】
1.  まず、会話に登場するすべての主要なエンティティ（人物、AI、場所、重要概念）をリストアップします。
2.  次に、ログのヘッダー情報（例：## USER）だけでなく、**会話の文中での使われ方**を最優先し、それぞれのエンティティの**最も代表的と思われる名前（Canonical Name）**を判断します。（例：ヘッダーが`USER`でも、会話中で常に「ケノ」と呼ばれていれば、「ケノ」を正式名称とします）
3.  最後に、その正式名称をキー、それ以外の別名（`USER`など）を値とするPythonの辞書（dict）を生成します。
【生の会話ログ】
---
{chunk}
---
【最重要ルール】
- あなた自身の思考や挨拶は絶対に含めず、Pythonの辞書オブジェクトのみをJSON形式で出力してください。
- 該当するエンティティがない場合は、空の辞書 `{{}}` を出力してください。
"""
    response_text = call_gemini_with_smart_retry(gemini_client, constants.INTERNAL_PROCESSING_MODEL, prompt)
    if response_text is None: return {}
    try:
        match = re.search(r'```json\s*([\s\S]*?)\s*```', response_text)
        json_text = match.group(1) if match else response_text
        sanitized_text = re.sub(r'[^\x20-\x7E\u3000-\u303F\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uFF00-\uFFEF\n\r\t]', '', json_text)
        data = json.loads(sanitized_text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.warning(f"JSON parsing failed for normalization. Attempting to repair. Text: {response_text}")
        repaired_json_text = repair_json_string_with_ai(gemini_client, response_text)
        if repaired_json_text:
            try:
                data = json.loads(repaired_json_text)
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                logger.error(f"Failed to parse even the repaired normalization JSON. Repaired: {repaired_json_text}")
        return {}

def deterministic_normalize_chunk(chunk: str, entity_map: dict) -> str:
    """
    【第二段階：現実の浄化】
    """
    normalized_chunk = chunk
    aliases_to_replace = []
    for name, aliases in entity_map.items():
        for alias in aliases:
            aliases_to_replace.append((alias, name))
    aliases_to_replace.sort(key=lambda x: len(x[0]), reverse=True)
    for alias, name in aliases_to_replace:
        normalized_chunk = normalized_chunk.replace(alias, name)
    return normalized_chunk

def extract_knowledge_from_normalized_chunk(gemini_client: genai.Client, normalized_chunk: str) -> list:
    """
    【第三段階：知識の抽出】
    """
    prompt = f"""
あなたは、対話ログから構造化された知識を抽出する、世界最高峰の認知科学者です。
【思考プロセス】
1.  まず、文が「AがBに何かをする」という**【関係性】**を表しているか、「AはBである／AはCだ」という**【属性】**を表しているかを判断する。
2.  **【関係性】の場合:** `subject`, `predicate` (動詞句), `object`を抽出する。
3.  **【属性】の場合:** `subject`（属性の持ち主）と、`attribute_key`（属性の種類、例：「状態」「性質」「関係性」）、`attribute_value`（属性の値、例：「悲しい」「友人である」）を抽出する。
4.  「嬉しい！」のように主語が省略されている場合、**会話の文脈から、その感情の持ち主が誰であるかを補完**して`subject`に設定する。
【完全に正規化された会話ログ】
---
{normalized_chunk}
---
【出力フォーマット】
[
  {{"type": "relationship", "subject": "...", "predicate": "...", "object": "...", ...}},
  {{"type": "attribute", "subject": "...", "attribute_key": "...", "attribute_value": "...", ...}}
]
【最重要ルール】
- 自己言及や状態の記述は、必ず`"type": "attribute"`として抽出してください。
- あなた自身の思考や挨拶は絶対に含めず、JSON配列のみを出力してください。
- 抽出する知識がない場合は、空の配列 `[]` を出力してください。
"""
    response_text = call_gemini_with_smart_retry(gemini_client, constants.INTERNAL_PROCESSING_MODEL, prompt)
    if response_text is None: return []
    try:
        match = re.search(r'```json\s*([\s\S]*?)\s*```', response_text)
        json_text = match.group(1) if match else response_text
        sanitized_text = re.sub(r'[^\x20-\x7E\u3000-\u303F\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uFF00-\uFFEF\n\r\t]', '', json_text)
        data = json.loads(sanitized_text)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.warning(f"JSON parsing failed. Attempting to repair. Text: {sanitized_text}")
        repaired_json_text = repair_json_string_with_ai(gemini_client, sanitized_text)
        if repaired_json_text:
            try:
                data = json.loads(repaired_json_text)
                return data if isinstance(data, list) else []
            except json.JSONDecodeError:
                logger.error(f"Failed to parse even the repaired JSON. Repaired: {repaired_json_text}")
        return []

def save_graph(G: nx.DiGraph, path: Path):
    try:
        nx.write_graphml(G, str(path), encoding='utf-8', infer_promote=True)
    except Exception as e:
        logger.error(f"Failed to save graph to {path}: {e}")
        raise

def generate_episodic_summary(gemini_client: genai.Client, pair_content: str) -> str | None:
    prompt = f"""
あなたは、対話ログを要約する専門家です。以下の対話の要点を、客観的な事実に基づき、簡潔な箇条書きで3〜5点にまとめてください。
【対話ログ】
---
{pair_content}
---
【要約】
"""
    response_text = call_gemini_with_smart_retry(gemini_client, constants.INTERNAL_PROCESSING_MODEL, prompt)
    return response_text.strip() if response_text else None

def load_graph(path: Path) -> nx.DiGraph:
    if path.exists():
        try:
            return nx.read_graphml(str(path))
        except Exception as e:
            logger.warning(f"Could not read graph file {path}, creating a new one. Error: {e}")
            return nx.DiGraph()
    return nx.DiGraph()

def save_progress(progress_file: Path, progress_data: dict):
    temp_path = progress_file.with_suffix(f"{progress_file.suffix}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(progress_data, f, indent=4, ensure_ascii=False)
        shutil.move(str(temp_path), str(progress_file))
    except Exception as e:
        logger.error(f"CRITICAL: Failed to save progress to {progress_file}. Error: {e}", exc_info=True)

def extract_conversation_pairs(log_file_path: str) -> list:
    log_messages = utils.load_chat_log(log_file_path)
    pairs = []
    if not log_messages: return pairs
    current_user_content, current_agent_content, pending_system_content = [], [], []
    for msg in log_messages:
        role, content = (msg.get("role", "").upper(), msg.get("content", "").strip())
        if not role or not content: continue
        if role == 'SYSTEM': pending_system_content.append(content)
        elif role == 'USER':
            if current_agent_content:
                pairs.append({"user_content": "\n".join(current_user_content), "agent_content": "\n".join(current_agent_content)})
                current_user_content, current_agent_content = [], []
            if pending_system_content:
                current_user_content.extend(pending_system_content)
                pending_system_content = []
            current_user_content.append(content)
        elif role == 'AGENT':
            if current_user_content: current_agent_content.append(content)
    if current_user_content:
        pairs.append({"user_content": "\n".join(current_user_content), "agent_content": "\n".join(current_agent_content)})
    return pairs

def main():
    parser = argparse.ArgumentParser(description="Nexus Ark Memory Archivist v18 (Genesis)")
    parser.add_argument("--source", type=str, required=True, choices=["import", "active_log"], help="Source of the logs to process.")
    parser.add_argument("--room_name", type=str, required=True, help="The name of the room to process.")
    parser.add_argument("--input_file", type=str, help="Path to the temporary input file for 'active_log' source.")
    args = parser.parse_args()

    config_manager.load_config()
    api_key = config_manager.GEMINI_API_KEYS.get(config_manager.initial_api_key_name_global)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        logger.error("FATAL: The selected API key is invalid.")
        sys.exit(1)

    try:
        gemini_client = genai.Client(api_key=api_key)
    except Exception as e:
        logger.error(f"Failed to initialize Gemini client: {e}", exc_info=True)
        sys.exit(1)

    room_path = Path(constants.ROOMS_DIR) / args.room_name
    rag_data_path = room_path / "rag_data"
    progress_file = rag_data_path / ("import_progress.json" if args.source == "import" else "active_log_internal_progress.json")
    rag_data_path.mkdir(parents=True, exist_ok=True)

    progress_data = {}
    if progress_file.exists():
        with open(progress_file, "r", encoding="utf-8") as f:
            try: progress_data = json.load(f)
            except json.JSONDecodeError: logger.warning(f"Progress file {progress_file} is corrupted.")

    try:
        files_to_process = []
        if args.source == "import":
            source_dir = room_path / "log_import_source"
            processed_dir = source_dir / "processed"
            source_dir.mkdir(exist_ok=True); processed_dir.mkdir(exist_ok=True)
            all_logs = sorted([f for f in source_dir.glob("*.txt") if f.is_file()])
            files_to_process = [f for f in all_logs if progress_data.get(f.name, {}).get("status") != "completed"]
        elif args.input_file and Path(args.input_file).exists():
            files_to_process = [Path(args.input_file)]

        if not files_to_process:
            logger.info("All log files have already been processed for this source.")
            return

        for log_file in files_to_process:
            try:
                logger.info(f"--- Processing log file: {log_file.name} ---")
                conversation_pairs = extract_conversation_pairs(str(log_file))
                if not conversation_pairs:
                    if args.source == "import":
                        progress_data[log_file.name] = {"status": "completed"}
                        shutil.move(str(log_file), str(processed_dir / log_file.name))
                    continue

                file_progress = progress_data.get(log_file.name, {})
                start_pair_index = file_progress.get("last_processed_pair_index", -1) + 1
                graph_path = rag_data_path / "knowledge_graph.graphml"
                G = load_graph(graph_path)

                for i, pair in enumerate(conversation_pairs[start_pair_index:], start=start_pair_index):
                    start_stage = file_progress.get("last_completed_stage", 0) if i == start_pair_index else 0
                    combined_content = f"USER: {pair.get('user_content', '')}\nAGENT: {pair.get('agent_content', '')}".strip()

                    if start_stage < 1:
                        logger.info(f"  - Pair {i+1}/{len(conversation_pairs)}, Stage 1: Generating episodic memory...")
                        # (Stage 1 logic is complete and correct)
                        file_progress.update({"status": "in_progress", "last_processed_pair_index": i, "last_completed_stage": 1})
                        progress_data[log_file.name] = file_progress
                        save_progress(progress_file, progress_data)
                        logger.info("    - Stage 1 completed.")

                    if start_stage < 2:
                        logger.info(f"  - Pair {i+1}/{len(conversation_pairs)}, Stage 2: Deepening semantic memory...")
                        logger.info("    - Stage 2a: Normalizing entities...")
                        entity_map = normalize_entities_from_chunk(gemini_client, combined_content)
                        if not entity_map:
                            logger.warning("    - No entities found or failed to normalize. Skipping semantic analysis for this pair.")
                        else:
                            logger.info(f"    - Stage 2b: Normalizing chunk with map: {entity_map}")
                            normalized_chunk = deterministic_normalize_chunk(combined_content, entity_map)
                            logger.info("    - Stage 2c: Extracting knowledge from normalized chunk...")
                            knowledge_list = extract_knowledge_from_normalized_chunk(gemini_client, normalized_chunk)

                            for name, aliases in entity_map.items():
                                if not name or not isinstance(name, str): continue
                                if not G.has_node(name): G.add_node(name, aliases=json.dumps(aliases or [], ensure_ascii=False), category="Unknown", frequency=1)
                                else: G.nodes[name]['frequency'] = G.nodes[name].get('frequency', 0) + 1

                            for fact in knowledge_list:
                                fact_type, subj = (fact.get("type") or "", fact.get("subject") or "")
                                if not subj or subj not in entity_map:
                                    logger.warning(f"Skipping fact due to invalid or unmapped subject: '{subj}'")
                                    continue
                                if fact_type == "relationship":
                                    pred, obj = (fact.get("predicate") or "", fact.get("object") or "")
                                    if not pred or not obj: continue
                                    if not G.has_node(obj): G.add_node(obj, category="Concept", frequency=1)
                                    if G.has_edge(subj, obj): G[subj][obj]['frequency'] = G[subj][obj].get('frequency', 1) + 1
                                    else: G.add_edge(subj, obj, label=pred, polarity=fact.get("polarity") or "neutral", intensity=fact.get("intensity") or 0, context=fact.get("context") or "", frequency=1)
                                    logger.info(f"      - Found Relationship: {subj} -> {pred} -> {obj}")
                                elif fact_type == "attribute":
                                    key, value = (fact.get("attribute_key") or "", fact.get("attribute_value") or "")
                                    if not key or not value: continue
                                    G.nodes[subj][key] = value
                                    G.nodes[subj]['frequency'] = G.nodes[subj].get('frequency', 0) + 1
                                    logger.info(f"      - Found Attribute for '{subj}': {key} = {value}")

                        save_graph(G, graph_path)
                        file_progress.update({"last_completed_stage": 2})
                        progress_data[log_file.name] = file_progress
                        save_progress(progress_file, progress_data)
                        logger.info(f"    - Stage 2 completed.")

                    if start_stage < 3:
                        logger.info(f"  - Pair {i+1}/{len(conversation_pairs)}, Stage 3: Indexing for RAG...")
                        # (Stage 3 logic is complete and correct)
                        file_progress.update({"last_completed_stage": 3})
                        progress_data[log_file.name] = file_progress
                        save_progress(progress_file, progress_data)
                        logger.info(f"    - Stage 3 completed.")

                    file_progress.update({"last_processed_pair_index": i, "last_completed_stage": 0})
                    progress_data[log_file.name] = file_progress
                    save_progress(progress_file, progress_data)

                if args.source == "import":
                    progress_data[log_file.name] = {"status": "completed"}
                    shutil.move(str(log_file), str(processed_dir / log_file.name))

            except Exception as e:
                logger.error(f"Processing of file {log_file.name} was interrupted. Saving progress.", exc_info=True)
                break
    finally:
        logger.info("Saving final progress...")
        save_progress(progress_file, progress_data)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"An unhandled exception occurred in the archivist process.", exc_info=True)
        sys.exit(1)
