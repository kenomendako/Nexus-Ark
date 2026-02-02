# agent/graph.py (v31: Dual-State Architecture - Cleaned)

import os
import copy
import re
import traceback
import json
import time
import glob
from datetime import datetime
from typing import TypedDict, Annotated, List, Literal, Tuple, Optional

from langchain_core.messages import SystemMessage, BaseMessage, ToolMessage, AIMessage, HumanMessage
from google.api_core import exceptions as google_exceptions
from langgraph.graph import StateGraph, END, START, add_messages

from agent.prompts import CORE_PROMPT_TEMPLATE
from tools.space_tools import set_current_location, read_world_settings, plan_world_edit, _apply_world_edits
from tools.memory_tools import (
    recall_memories,
    search_past_conversations,
    read_memory_context,  # 記憶の続きを読む [2026-01-08 NEW]
    search_memory,  # 内部使用のみ（retrieval_nodeで使用）
    read_main_memory, plan_main_memory_edit, _apply_main_memory_edits,
    read_secret_diary, plan_secret_diary_edit, _apply_secret_diary_edits
)
from tools.notepad_tools import read_full_notepad, plan_notepad_edit,  _apply_notepad_edits
from tools.creative_tools import read_creative_notes, plan_creative_notes_edit, _apply_creative_notes_edits
from tools.research_tools import read_research_notes, plan_research_notes_edit, _apply_research_notes_edits
from tools.web_tools import web_search_tool, read_url_tool
from tools.image_tools import generate_image
from tools.alarm_tools import set_personal_alarm
from tools.timer_tools import set_timer, set_pomodoro_timer
from tools.knowledge_tools import search_knowledge_base
from tools.entity_tools import read_entity_memory, write_entity_memory, list_entity_memories, search_entity_memory
from tools.chess_tools import read_board_state, perform_move, get_legal_moves, reset_game as reset_chess_game
from tools.developer_tools import list_project_files, read_project_file
from tools.introspection_tools import manage_open_questions, manage_goals

from room_manager import get_world_settings_path, get_room_files_paths
from episodic_memory_manager import EpisodicMemoryManager
from action_plan_manager import ActionPlanManager  
from tools.action_tools import schedule_next_action, cancel_action_plan, read_current_plan
from tools.notification_tools import send_user_notification
from tools.watchlist_tools import add_to_watchlist, remove_from_watchlist, get_watchlist, check_watchlist, update_watchlist_interval
from dreaming_manager import DreamingManager
from goal_manager import GoalManager
from entity_memory_manager import EntityMemoryManager
from llm_factory import LLMFactory

import utils
import config_manager
import constants
from constants import SUPERVISOR_MODEL
import pytz
import signature_manager 
import room_manager 
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError

# 【マルチモデル対応】OpenAIエラーのインポート
try:
    import openai
    OPENAI_ERRORS = (openai.NotFoundError, openai.BadRequestError, openai.APIError)
except ImportError:
    # openaiがインストールされていない場合のフォールバック
    OPENAI_ERRORS = ()

all_tools = [
    set_current_location, read_world_settings, plan_world_edit,
    # --- 記憶検索ツール ---
    recall_memories,  # 統合記憶検索（日記・過去ログ・エピソード記憶）
    search_past_conversations,  # キーワード完全一致検索（最終手段）
    read_memory_context,  # 検索結果の続きを読む [2026-01-08 NEW]
    # --- 日記・メモ操作ツール ---
    read_main_memory, plan_main_memory_edit, read_secret_diary, plan_secret_diary_edit,
    read_full_notepad, plan_notepad_edit,
    # --- Web系ツール ---
    web_search_tool, read_url_tool,
    generate_image,
    set_personal_alarm,
    set_timer, set_pomodoro_timer,
    # --- 知識ベース・エンティティ検索ツール ---
    search_knowledge_base,  # 外部資料・マニュアル検索
    read_entity_memory, write_entity_memory, list_entity_memories, search_entity_memory,
    # --- アクション・通知ツール ---
    schedule_next_action, cancel_action_plan, read_current_plan,
    send_user_notification,
    read_creative_notes, plan_creative_notes_edit,
    # --- ウォッチリストツール ---
    add_to_watchlist, remove_from_watchlist, get_watchlist, check_watchlist, update_watchlist_interval,
    read_research_notes, plan_research_notes_edit,
    # --- チェスツール ---
    read_board_state, perform_move, get_legal_moves, reset_chess_game,
    # --- 開発者ツール ---
    list_project_files, read_project_file,
    # --- 内省ツール ---
    manage_open_questions, manage_goals
]

side_effect_tools = [
    "plan_main_memory_edit", "plan_secret_diary_edit", "plan_notepad_edit", "plan_world_edit",
    "plan_creative_notes_edit",
    "plan_research_notes_edit",
    "set_personal_alarm", "set_timer", "set_pomodoro_timer",
    "schedule_next_action"
]

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    room_name: str
    api_key: str
    api_key_name: str
    model_name: str
    system_prompt: SystemMessage
    generation_config: dict
    send_core_memory: bool
    send_scenery: bool
    send_notepad: bool
    send_thoughts: bool
    send_current_time: bool 
    location_name: str
    scenery_text: str
    debug_mode: bool
    display_thoughts: bool
    all_participants: List[str]
    loop_count: int 
    season_en: str
    time_of_day_en: str
    last_successful_response: Optional[AIMessage]
    force_end: bool
    skip_tool_execution: bool
    retrieved_context: str
    tool_use_enabled: bool  # 【ツール不使用モード】ツール使用の有効/無効
    next: str
    enable_supervisor: bool # Supervisor機能の有効/無効
    custom_system_prompt: Optional[str] # システムプロンプトの上書き用
    actual_token_usage: Optional[dict] = None # 【2026-01-10 NEW】実送信トークン数記録用

def get_location_list(room_name: str) -> List[str]:
    if not room_name: return []
    world_settings_path = get_world_settings_path(room_name)
    if not world_settings_path or not os.path.exists(world_settings_path): return []
    world_data = utils.parse_world_file(world_settings_path)
    if not world_data: return []
    locations = set()
    for area_name, places in world_data.items():
        for place_name in places.keys():
            if place_name == "__area_description__": continue
            locations.add(place_name)
    return sorted(list(locations))

from agent.scenery_manager import generate_scenery_context

# ▼▼▼ [2026-01-07 ハイブリッド検索] キーワード検索用内部関数 ▼▼▼
def _keyword_search_for_retrieval(
    keywords: list,
    room_name: str,
    exclude_recent_count: int
) -> list:
    """
    retrieval_node専用のキーワード検索。
    search_past_conversationsツールのロジックを流用するが、
    より厳格なフィルタリングを適用。
    
    時間帯別枠取り: 新2 + 古2 + 中間ランダム1 = 計5件
    """
    import random
    from pathlib import Path
    
    if not keywords or not room_name:
        return []
    
    base_path = Path(constants.ROOMS_DIR) / room_name
    search_paths = [str(base_path / "log.txt")]
    search_paths.extend(glob.glob(str(base_path / "log_archives" / "*.txt")))
    search_paths.extend(glob.glob(str(base_path / "log_import_source" / "*.txt")))
    
    found_blocks = []
    date_patterns = [
        re.compile(r'(\d{4}-\d{2}-\d{2}) \(...\) \d{2}:\d{2}:\d{2}'),
        re.compile(r'###\s*(\d{4}-\d{2}-\d{2})')
    ]
    
    search_keywords = [k.lower() for k in keywords]
    
    for file_path_str in search_paths:
        file_path = Path(file_path_str)
        if not file_path.exists() or file_path.stat().st_size == 0:
            continue
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception:
            continue
        
        # USER/AGENT のヘッダーのみを対象（SYSTEMは除外）
        header_indices = [
            i for i, line in enumerate(lines)
            if re.match(r"^(## (?:USER|AGENT):.*)$", line.strip())
        ]
        if not header_indices:
            continue
        
        search_end_line = len(lines)
        
        # log.txt の場合、最新N件を除外（送信ログ除外）
        if file_path.name == "log.txt" and exclude_recent_count > 0:
            msg_count = len(header_indices)
            if msg_count <= exclude_recent_count:
                continue
            else:
                cutoff_header_index = header_indices[-exclude_recent_count]
                search_end_line = cutoff_header_index
        
        processed_blocks_content = set()
        
        for i, line in enumerate(lines[:search_end_line]):
            if any(k in line.lower() for k in search_keywords):
                # ヘッダーを探す
                start_index = 0
                for h_idx in reversed(header_indices):
                    if h_idx <= i:
                        start_index = h_idx
                        break
                
                # 次のヘッダーまでをブロックとする
                end_index = len(lines)
                for h_idx in header_indices:
                    if h_idx > start_index:
                        end_index = h_idx
                        break
                
                block_content = "".join(lines[start_index:end_index]).strip()
                
                # 重複チェック
                if block_content in processed_blocks_content:
                    continue
                processed_blocks_content.add(block_content)
                
                # 短すぎるブロックを除外
                if len(block_content) < 30:
                    continue
                
                # 日付を抽出
                block_date = None
                for pattern in date_patterns:
                    matches = list(pattern.finditer(block_content))
                    if matches:
                        block_date = matches[-1].group(1)
                        break
                
                found_blocks.append({
                    "content": block_content,
                    "date": block_date,
                    "source": file_path.name
                })
    
    if not found_blocks:
        return []
    
    # 時間帯別枠取り: 新2 + 古2 + 中間ランダム1 = 計5件
    # 日付順ソート（新しい順）
    sorted_blocks = sorted(
        found_blocks,
        key=lambda x: x.get('date') or '0000-00-00',
        reverse=True
    )
    
    # 重複を除去（コンテンツベース）
    unique_blocks = []
    seen_contents = set()
    for b in sorted_blocks:
        content_key = b.get('content', '')[:200]  # 先頭200文字で重複判定
        if content_key not in seen_contents:
            seen_contents.add(content_key)
            unique_blocks.append(b)
    
    if len(unique_blocks) <= 5:
        return unique_blocks
    
    # 時間帯別に選択
    newest = unique_blocks[:2]   # 新しい方から2件
    oldest = unique_blocks[-2:]  # 古い方から2件
    
    # 中間部分からランダムに1件選択
    middle = unique_blocks[2:-2]
    random_middle = [random.choice(middle)] if middle else []
    
    # 結合（既に重複除去済みなのでそのまま）
    selected = list(newest) + [b for b in oldest if b not in newest] + [b for b in random_middle if b not in newest and b not in oldest]
    
    print(f"    -> [時間帯別枠取り] 全{len(found_blocks)}件 → 重複除去後{len(unique_blocks)}件 → 選択{len(selected)}件")
    
    return selected[:5]
# ▲▲▲ キーワード検索用内部関数ここまで ▲▲▲

def retrieval_node(state: AgentState):
    """
    ユーザーの入力に基づいて、知識ベース、過去ログ、日記から関連情報を検索し、
    コンテキストに追加するノード。
    """
    print("--- 検索ノード (retrieval_node) 実行 ---")
    
    # 個別設定で検索が無効化されている場合は、何もせずに終了
    if not state.get("generation_config", {}).get("enable_auto_retrieval", True):
        print("  - [Retrieval Skip] 設定により事前検索は無効化されています。")
        return {"retrieved_context": ""}

    # 1. 検索対象となるユーザー入力（最後のメッセージ）を取得
    if not state['messages']:
        print("  - [Retrieval Skip] メッセージ履歴が空です。")
        return {"retrieved_context": ""}
    
    last_message = state['messages'][-1]
    # print(f"  - [Retrieval Debug] Last Message Type: {type(last_message).__name__}")
    
    if not isinstance(last_message, HumanMessage):
        print(f"  - [Retrieval Skip] 最後のメッセージがユーザー発言ではありません。(Type: {type(last_message).__name__})")
        return {"retrieved_context": ""}
        
    # コンテンツがリスト（マルチモーダル）の場合、テキスト部分だけ抽出
    query_source = ""
    if isinstance(last_message.content, str):
        query_source = last_message.content
    elif isinstance(last_message.content, list):
        for part in last_message.content:
            if isinstance(part, dict) and part.get("type") == "text":
                query_source += part.get("text", "") + " "
    
    query_source = query_source.strip()
    if not query_source:
        print("  - [Retrieval Skip] 検索対象となるテキストコンテンツが含まれていません。")
        return {"retrieved_context": ""}

    # --- [Phase F 廃止] ユーザー感情分析のLLM呼び出しを廃止 ---
    # ペルソナが自身の感情を出力する新方式（<persona_emotion>タグ）に移行。
    # 以下のユーザー感情検出コードは維持するが、実行はスキップする。
    # ---
    # enable_self_awareness = state.get("generation_config", {}).get("enable_self_awareness", True)
    # if enable_self_awareness:
    #     try:
    #         from motivation_manager import MotivationManager
    #         mm = MotivationManager(state['room_name'])
    #         mm.detect_process_and_log_user_emotion(
    #             user_text=query_source,
    #             model_name=constants.INTERNAL_PROCESSING_MODEL,
    #             api_key=state['api_key']
    #         )
    #     except Exception as emotion_e:
    #         print(f"  - [Emotion] 感情検出でエラー（無視）: {emotion_e}")
    # --- ユーザー感情分析廃止ここまで ---

    # 2. クエリ生成AI（Flash Lite）による判断
    api_key = state['api_key']
    room_name = state['room_name']
    
    # 高速なモデルを使用
    # 【マルチモデル対応】内部モデル設定（混合編成）に基づいてモデルを取得
    llm_flash = LLMFactory.create_chat_model(
        api_key=api_key,
        generation_config={},
        internal_role="processing"
    )
    
    # プロンプトの改善（System/Human分離）
    system_prompt = """あなたは、情報の抽出と検索クエリ生成の専門家です。
ユーザーの発言から、指定されたフォーマットに従って「検索キーワード」と「意図(INTENT)」のみを抽出してください。
解説、前置き、思考プロセスは絶対に含めないでください。出力は、指定された3行の形式、または「NONE」のみである必要があります。"""

    human_prompt = f"""以下のユーザーの発言を分析し、検索クエリを生成してください。

【ユーザーの発言】
{query_source}

【出力形式】
RAG: [意味検索用キーワード]
KEYWORD: [完全一致検索用キーワード（または NONE）]
INTENT: [emotional/factual/technical/temporal/relational]

【ルール】
- 解説は一切不要。
- 文字列 'RAG:', 'KEYWORD:', 'INTENT:' で始まる3行のみを出力せよ。
- 検索が不要な場合は 'NONE' とのみ出力せよ。
"""

    try:
        # メッセージリスト形式で送信
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_prompt)
        ]
        
        # 503 UNAVAILABLE に対する簡易リトライ (Retriever node)
        decision_response = ""
        for attempt in range(3):
            try:
                decision_response = llm_flash.invoke(messages).content.strip()
                break
            except Exception as e:
                err_str = str(e).upper()
                is_503 = "503" in err_str or "UNAVAILABLE" in err_str or "OVERLOADED" in err_str
                # 429 または 503 リトライ上限の場合は raise して上位の invoke_nexus_agent_stream でローテーションさせる
                is_429 = isinstance(e, google_exceptions.ResourceExhausted) or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                
                if is_429 or (is_503 and attempt == 2):
                    raise e
                
                if is_503:
                    print(f"  - [Retrieval Warning] 503 UNAVAILABLE detected in retrieval_node. Retrying locally (attempt {attempt+1}/3)...")
                    time.sleep(2 * (attempt + 1))
                    continue
                raise e
        
        if "NONE" in decision_response.upper() and len(decision_response) < 10:
            print("  - [Retrieval] 判断: 検索不要 (AI判断)")
            return {"retrieved_context": ""}
        
        # 正規表現による柔軟なパース
        rag_query = ""
        keyword_query = ""
        intent = constants.DEFAULT_INTENT
        
        rag_match = re.search(r"RAG:\s*(.+)", decision_response, re.IGNORECASE)
        kw_match = re.search(r"KEYWORD:\s*(.+)", decision_response, re.IGNORECASE)
        intent_match = re.search(r"INTENT:\s*(\w+)", decision_response, re.IGNORECASE)

        if rag_match:
            rag_query = rag_match.group(1).strip()
        if kw_match:
            kw_part = kw_match.group(1).strip()
            if kw_part.upper() != "NONE":
                keyword_query = kw_part
        if intent_match:
            intent_part = intent_match.group(1).strip().lower()
            if intent_part in constants.INTENT_WEIGHTS:
                intent = intent_part
        
        # 後方互換: RAG:がない場合は全体をRAGクエリとして扱う
        if not rag_query and decision_response.upper() != "NONE":
            rag_query = decision_response
        
        print(f"  - [Retrieval] RAGクエリ: '{rag_query}'")
        if keyword_query:
            print(f"  - [Retrieval] キーワードクエリ: '{keyword_query}'")
        else:
            print(f"  - [Retrieval] キーワードクエリ: なし")
        print(f"  - [Retrieval] Intent: {intent}")
        
        results = []

        import config_manager
        # 現在の設定を取得 (JIT読み込み推奨だが、頻度が高いのでCONFIG_GLOBALでも可。ここでは安全のためloadする)
        current_config = config_manager.load_config_file()
        history_limit_option = current_config.get("last_api_history_limit_option", "all")
        
        exclude_count = 0
        if history_limit_option == "all":
            # 「全ログ」送信設定なら、log.txt はすべてコンテキストに含まれているので検索不要
            exclude_count = 999999
        elif history_limit_option == "today":
            # 「本日分」送信設定でも、本日のログは全てコンテキストに含まれているので
            # 追加検索は不要（ただし過去のログは検索対象となる）
            exclude_count = 999999
        elif history_limit_option.isdigit():
            # 「10往復」なら 20メッセージ分を除外
            # さらに安全マージンとして +2 (直前のシステムメッセージ等) しておくと確実
            exclude_count = int(history_limit_option) * 2 + 2

        # ▼▼▼ [2025-01-07 リデザイン] 知識ベース検索を除外 ▼▼▼
        # 知識ベースは「外部資料・マニュアル」用であり、会話コンテキストへの自動注入は不適切。
        # AIが能動的に資料を調べたい場合は search_knowledge_base ツールを使用する。
        # ---
        # 3a. 知識ベース (削除済み - AIがツールで能動的に検索)
        # from tools.knowledge_tools import search_knowledge_base
        # kb_result = search_knowledge_base.func(...)
        # ▲▲▲ 知識ベース除外ここまで ▲▲▲

        # ▼▼▼ [2024-12-28 最適化] 過去ログキーワード検索を除外 ▼▼▼
        # キーワードマッチ方式はノイズが多いため除外。
        # AIが能動的に検索したい場合は search_past_conversations ツールを使用可能。
        # ▲▲▲ 過去ログ検索除外ここまで ▲▲▲

        # 3b. 日記 (Memory) - RAGクエリで検索（Intent渡し）
        from tools.memory_tools import search_memory
        if rag_query:
            mem_result = search_memory.func(query=rag_query, room_name=room_name, api_key=api_key, intent=intent)
            # 日記検索のヘッダーチェック
            if mem_result and "【記憶検索の結果：" in mem_result:
                print(f"    -> 日記: ヒット ({len(mem_result)} chars)")
                results.append(mem_result)
            else:
                print(f"    -> 日記: なし")
        
        # ▼▼▼ [2026-01-07 ハイブリッド検索] 過去ログキーワード検索を復活 ▼▼▼
        # 特徴的なキーワード（固有名詞等）がある場合のみ実行
        if keyword_query:
            kw_results = _keyword_search_for_retrieval(
                keywords=keyword_query.split(),
                room_name=room_name,
                exclude_recent_count=exclude_count
            )
            if kw_results:
                # 結果を整形
                kw_text_parts = ["【過去の会話ログからの検索結果】"]
                for block in kw_results:
                    date_str = f"({block['date']}頃)" if block.get('date') else ""
                    content = block['content']
                    # 500文字を超える場合は切り捨て
                    if len(content) > 500:
                        content = content[:500] + "\n...【続きあり→read_memory_context使用】"
                    kw_text_parts.append(f"--- [{block.get('source', '不明')}{date_str}] ---\n{content}")
                
                kw_result = "\n\n".join(kw_text_parts)
                print(f"    -> 過去ログ: ヒット ({len(kw_results)}件)")
                results.append(kw_result)
            else:
                print(f"    -> 過去ログ: なし")
        # ▲▲▲ ハイブリッド検索ここまで ▲▲▲

        # 3d. エンティティ記憶の「きっかけ」抽出 (Suggestive Recall)
        # 会話に出たキーワードから関連するエンティティ名を探し、存在を通知する
        em_manager = EntityMemoryManager(room_name)
        # rag_query または keyword_query からキーワードを収集
        entity_search_keywords = (rag_query + " " + keyword_query).strip()
        if entity_search_keywords:
            matched_entities = em_manager.search_entries(entity_search_keywords)
            if matched_entities:
                # 最大3件まで提示
                selection = matched_entities[:3]
                suggestion_parts = [
                    "【関連するエンティティ記憶の示唆】",
                    "以下のトピックに関する過去の記録が見つかりました。必要に応じて `read_entity_memory(\"エントリ名\")` で内容を確認してください。"
                ]
                for entity in selection:
                    suggestion_parts.append(f"- 「{entity}」")
                
                suggestion_text = "\n".join(suggestion_parts)
                print(f"    -> エンティティ示唆: ヒット ({len(selection)}件)")
                results.append(suggestion_text)
            else:
                print(f"    -> エンティティ示唆: なし")

        # ▼▼▼ [2024-12-28 最適化] 話題クラスタ検索を一時無効化 ▼▼▼
        # 現状のクラスタリング精度が低く、ノイズが多いため一時無効化。
        # 別タスク「話題クラスタの改良」完了後に再有効化する。
        # ---
        # 3d. 話題クラスタ検索 (一時無効化)
        # try:
        #     from topic_cluster_manager import TopicClusterManager
        #     tcm = TopicClusterManager(room_name, api_key)
        #     if tcm._load_clusters().get("clusters"):
        #         relevant_clusters = tcm.get_relevant_clusters(search_query, top_k=2)
        #         if relevant_clusters:
        #             cluster_context_parts = []
        #             for cluster in relevant_clusters:
        #                 label = cluster.get('label', '不明なトピック')
        #                 summary = cluster.get('summary', '')
        #                 if summary:
        #                     cluster_context_parts.append(f"【{label}に関する記憶】\n{summary}")
        #             if cluster_context_parts:
        #                 cluster_result = "【関連する話題クラスタ：】\n" + "\n\n".join(cluster_context_parts)
        #                 print(f"    -> 話題クラスタ: ヒット ({len(relevant_clusters)}件)")
        #                 results.append(cluster_result)
        #         else:
        #             print(f"    -> 話題クラスタ: 関連なし")
        #     else:
        #         print(f"    -> 話題クラスタ: データなし（初回クラスタリング未実行）")
        # except Exception as cluster_e:
        #     print(f"    -> 話題クラスタ: エラー ({cluster_e})")
        # ▲▲▲ 話題クラスタ一時無効化ここまで ▲▲▲
                
        if not results:
            print("  - [Retrieval] 関連情報は検索されませんでした。")
            return {"retrieved_context": "（関連情報は検索されませんでした）"}
            
        final_context = "\n\n".join(results)
        print(f"  - [Retrieval] 検索完了。合計 {len(final_context)} 文字のコンテキストを生成しました。")
        
        # ▼▼▼ デバッグ用：検索結果の全内容を出力（必要時にコメント解除） ▼▼▼
        # print("\n" + "="*60)
        # print("[RETRIEVAL DEBUG] 検索結果の全内容:")
        # print("="*60)
        # for i, res in enumerate(results):
        #     print(f"\n--- 結果 {i+1} ({len(res)} chars) ---")
        #     print(res)
        # print("="*60 + "\n")
        # ▲▲▲ デバッグ用ここまで ▲▲▲
        
        return {"retrieved_context": final_context}

    except Exception as e:
        # 429 エラー（ResourceExhausted）の場合は、上位でキャッチしてローテーションさせるために再送出する
        err_str = str(e).upper()
        if isinstance(e, google_exceptions.ResourceExhausted) or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
             print(f"  - [Retrieval Error] Quota limit hit (429). Re-raising for rotation. {e}")
             raise e
        print(f"  - [Retrieval Error] 検索処理中にエラー: {e}")
        traceback.print_exc()
        return {"retrieved_context": ""}

def context_generator_node(state: AgentState):
    room_name = state['room_name']
    
    # 状況プロンプト
    situation_prompt_parts = []
    send_time = state.get("send_current_time", False)
    if send_time:
        tokyo_tz = pytz.timezone('Asia/Tokyo')
        now_tokyo = datetime.now(tokyo_tz)
        day_map = {"Monday": "月", "Tuesday": "火", "Wednesday": "水", "Thursday": "木", "Friday": "金", "Saturday": "土", "Sunday": "日"}
        day_ja = day_map.get(now_tokyo.strftime('%A'), "")
        current_datetime_str = now_tokyo.strftime(f'%Y-%m-%d({day_ja}) %H:%M:%S')
    else:
        current_datetime_str = "（現在時刻は非表示に設定されています）"

    if not state.get("send_scenery", True):
        situation_prompt_parts.append(f"【現在の状況】\n- 現在時刻: {current_datetime_str}")
        situation_prompt_parts.append("【現在の場所と情景】\n（空間描写は設定により無効化されています）")
    else:
        season_en = state.get("season_en", "autumn")
        time_of_day_en = state.get("time_of_day_en", "night")
        season_map_en_to_ja = {"spring": "春", "summer": "夏", "autumn": "秋", "winter": "冬"}
        season_ja = season_map_en_to_ja.get(season_en, "不明な季節")
        
        time_map_en_to_ja = {
            "early_morning": "早朝", "morning": "朝", "late_morning": "昼前",
            "afternoon": "昼下がり", "evening": "夕方", "night": "夜", "midnight": "深夜"
        }
        time_of_day_ja = time_map_en_to_ja.get(time_of_day_en, "不明な時間帯")
        
        # 現在地情報の同期的・実体的な取得
        soul_vessel_room = state['all_participants'][0] if state['all_participants'] else state['room_name']
        current_location_name = utils.get_current_location(soul_vessel_room)
        location_display_name = current_location_name or state.get("location_name", "（不明な場所）")
        
        scenery_text = state.get("scenery_text", "（情景描写を取得できませんでした）")
        space_def = "（場所の定義を取得できませんでした）"
        current_location_name = utils.get_current_location(soul_vessel_room)
        if current_location_name:
            world_settings_path = get_world_settings_path(soul_vessel_room)
            world_data = utils.parse_world_file(world_settings_path)
            if isinstance(world_data, dict):
                for area, places in world_data.items():
                    if isinstance(places, dict) and current_location_name in places:
                        space_def = places[current_location_name]
                        if isinstance(space_def, str) and len(space_def) > 2000: space_def = space_def[:2000] + "\n...（長すぎるため省略）"
                        break
        available_locations = get_location_list(state['room_name'])
        location_list_str = "\n".join([f"- {loc}" for loc in available_locations]) if available_locations else "（現在、定義されている移動先はありません）"
        situation_prompt_parts.extend([
            "【現在の状況】", f"- 現在時刻: {current_datetime_str}", f"- 季節: {season_ja}", f"- 時間帯: {time_of_day_ja}\n",
            "【現在の場所と情景】", f"- 場所: {location_display_name}", f"- 今の情景: {scenery_text}",
            f"- 場所の設定（自由記述）: \n{space_def}\n", "【移動可能な場所】", location_list_str
        ])
    situation_prompt = "\n".join(situation_prompt_parts)
    
    char_prompt_path = os.path.join(constants.ROOMS_DIR, room_name, "SystemPrompt.txt")
    core_memory_path = os.path.join(constants.ROOMS_DIR, room_name, "core_memory.txt")
    character_prompt = ""; core_memory = ""; notepad_section = ""
    if os.path.exists(char_prompt_path):
        with open(char_prompt_path, 'r', encoding='utf-8') as f: character_prompt = f.read().strip()
    if state.get("send_core_memory", True):
        if os.path.exists(core_memory_path):
            with open(core_memory_path, 'r', encoding='utf-8') as f: core_memory = f.read().strip()
    if state.get("send_notepad", True):
        try:
            from room_manager import get_room_files_paths
            _, _, _, _, notepad_path, _ = get_room_files_paths(room_name)
            if notepad_path and os.path.exists(notepad_path):
                with open(notepad_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    notepad_content = content if content else "（メモ帳は空です）"
            else: notepad_content = "（メモ帳ファイルが見つかりません）"
            notepad_section = f"\n### 短期記憶（メモ帳）\n{notepad_content}\n"
        except Exception as e:
            print(f"--- 警告: メモ帳の読み込み中にエラー: {e}")
            notepad_section = "\n### 短期記憶（メモ帳）\n（メモ帳の読み込み中にエラーが発生しました）\n"

    research_notes_section = ""
    try:
        from room_manager import get_room_files_paths
        _, _, _, _, _, research_notes_path = get_room_files_paths(room_name)
        if research_notes_path and os.path.exists(research_notes_path):
            with open(research_notes_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            # 見出し（## で始まる行）を抽出（H2レベルを優先）
            headlines = [line.strip() for line in lines if line.strip().startswith("## ")]
            
            if headlines:
                # 最新の10件を表示（必要ならさらに絞る）
                latest_headlines = headlines[-10:]
                headlines_str = "\n".join(latest_headlines)
                research_notes_content = (
                    "以下は最近の研究・分析トピックの目次です。詳細な内容は `read_research_notes` ツールで確認するか、\n"
                    "`recall_memories` ツールで過去の記憶としてキーワード検索してください。\n\n"
                    f"{headlines_str}"
                )
            else:
                research_notes_content = "（研究ノートにトピックが定義されていません）"
        else: research_notes_content = "（研究ノートファイルが見つかりません）"
        research_notes_section = f"\n### 研究・分析ノート（目次）\n{research_notes_content}\n"
    except Exception as e:
        print(f"--- 警告: 研究ノートの読み込み中にエラー: {e}")
        research_notes_section = "\n### 研究・分析ノート\n（研究ノートの読み込み中にエラーが発生しました）\n"

    # --- [Entity Memory v2] エンティティ一覧（目次）の注入 ---
    entity_list_section = ""
    try:
        em_manager = EntityMemoryManager(room_name)
        entities = em_manager.list_entries()
        if entities:
            entity_list_str = "\n".join([f"- {name}" for name in sorted(entities)])
            entity_list_section = (
                f"\n### 記憶しているエンティティ一覧\n"
                f"以下は記憶している人物・事物の名前です。詳細は `read_entity_memory(\"名前\")` で確認できます。\n\n"
                f"{entity_list_str}\n"
            )
        
        # --- [Phase 2] ペンディングシステムメッセージ（影の僕からの提案）の注入 ---
        from dreaming_manager import DreamingManager
        dm = DreamingManager(room_name, state.get("api_key", ""))
        pending_msg = dm.get_pending_system_messages()
        if pending_msg:
            entity_list_section += f"\n\n{pending_msg}\n"
            print(f"  - [Context] ペンディングシステムメッセージを注入しました")
    except Exception as e:
        print(f"  - [Context] エンティティ一覧取得エラー: {e}")

    episodic_memory_section = ""
    
    # 1. 設定値の取得
    generation_config = state.get("generation_config", {})
    lookback_days_str = generation_config.get("episode_memory_lookback_days", "14")
    
    if lookback_days_str and lookback_days_str != "0":
        try:
            lookback_days = int(lookback_days_str)
            
            # 2. 「今日」を基準に、過去N日間のエピソード記憶を取得
            # 以前は「ログの最古日付」を基準にしていたが、ユーザーの期待は
            # 「過去2日」= 今日から2日前（例: 1/21なら1/19〜1/20）
            today_str = datetime.now().strftime('%Y-%m-%d')

            # 3. エピソード記憶マネージャーから要約を取得
            manager = EpisodicMemoryManager(room_name)
            episodic_text = manager.get_episodic_context(today_str, lookback_days)
            
            if episodic_text:
                episodic_memory_section = (
                    f"\n### エピソード記憶（中期記憶: 過去{lookback_days}日間）\n"
                    f"以下は、現在の会話ログより前の出来事の要約です。文脈として参照してください。\n"
                    f"{episodic_text}\n"
                )
                print(f"  - [Episodic Memory] 過去{lookback_days}日間の記憶を注入しました。")
            else:
                print(f"  - [Episodic Memory] 注入対象の期間に記憶がありませんでした。")

        except Exception as e:
            print(f"  - [Episodic Memory Error] 注入処理中にエラー: {e}")
            episodic_memory_section = ""

    # --- [Project Morpheus] 夢想（深層意識）の注入 ---
    # 【自己意識機能】トグルがOFFの場合はスキップ
    enable_self_awareness = state.get("generation_config", {}).get("enable_self_awareness", True)
    dream_insights_text = ""
    
    if enable_self_awareness:
        try:
            # APIキーが必要だが、context_generator_nodeにはstate['api_key']がある
            dm = DreamingManager(room_name, state['api_key'])
            # 最新1件の「指針」のみを取得（コスト最適化）
            recent_insights = dm.get_recent_insights_text(limit=1)
            
            if recent_insights:
                dream_insights_text = (
                    f"\n### 深層意識（今日の指針）\n"
                    f"{recent_insights}\n"
                )
        except Exception as e:
            print(f"  - [Context] 夢想データの読み込みエラー: {e}")
            dream_insights_text = ""
        
        # --- [Goal Memory] 目標の注入 ---
        goals_text = ""
        try:
            gm = GoalManager(room_name)
            goals_text = gm.get_goals_for_prompt()
            if goals_text:
                dream_insights_text += f"\n\n{goals_text}\n"
        except Exception as e:
            print(f"  - [Context] 目標データの読み込みエラー: {e}")

        # --- [Internal State] 内的状態の簡易版注入 ---
        # 通常対話時にもAIが自己の動機を意識できるようにする
        try:
            from motivation_manager import MotivationManager
            mm = MotivationManager(room_name)
            
            # ドライブを計算（Phase F: devotion廃止、relatednessのみ）
            drives = {
                "boredom": mm.calculate_boredom(),
                "curiosity": mm.calculate_curiosity(),
                "goal_achievement": mm.calculate_goal_achievement(),
                "relatedness": mm.calculate_relatedness()
            }
            
            dominant_drive = max(drives, key=drives.get)
            drive_level = drives[dominant_drive]
            
            # 閾値以上の動機がある場合のみ注入（トークン節約）
            if drive_level >= 0.4:
                drive_label = mm.DRIVE_LABELS.get(dominant_drive, dominant_drive)
                narrative = mm._generate_narrative(dominant_drive, drive_level)
                
                internal_state_brief = (
                    f"\n### 今のあなたの気持ち\n"
                    f"- 最も強い動機: {drive_label}（強さ: {drive_level:.1f}）\n"
                    f"- {narrative}\n"
                )
                dream_insights_text += internal_state_brief
                print(f"  - [Context] 内的状態を注入: {drive_label} ({drive_level:.2f})")
            
            # 最も優先度の高い未解決の問いを注入
            questions = mm._state.get("drives", {}).get("curiosity", {}).get("open_questions", [])
            unresolved = [q for q in questions if not q.get("resolved_at")]
            if unresolved:
                # 優先度でソートして上位1件
                top_question = max(unresolved, key=lambda q: q.get("priority", 0))
                topic = top_question.get("topic", "")
                context = top_question.get("context", "")
                if topic:
                    question_text = (
                        f"\n### あなたが今気になっていること\n"
                        f"- {topic}\n"
                    )
                    if context:
                        question_text += f"  （背景: {context[:100]}...）\n" if len(context) > 100 else f"  （背景: {context}）\n"
                    dream_insights_text += question_text
                    print(f"  - [Context] 未解決の問いを注入: {topic[:30]}...")
        except Exception as e:
            print(f"  - [Context] 内的状態の読み込みエラー: {e}")

    action_plan_context = ""
    try:
        plan_manager = ActionPlanManager(room_name)
        action_plan_context = plan_manager.get_plan_context_for_prompt()
        if action_plan_context:
            # 計画がある場合、ユーザー発言（HumanMessage）があるかチェック
            # もしユーザー発言があれば、計画よりもユーザーを優先するよう注釈を加える
            messages = state.get('messages', [])
            if messages and isinstance(messages[-1], HumanMessage):
                action_plan_context += "\n\n【重要：ユーザー割り込み発生】\n現在、行動計画が進行中ですが、ユーザーから新たな発話がありました。計画の実行よりも、ユーザーへの応答を最優先してください。必要であれば `cancel_action_plan` で計画を破棄しても構いません。"
    except Exception as e:
        print(f"  - [Action Plan] 読み込みエラー: {e}")

    image_gen_mode = config_manager.CONFIG_GLOBAL.get("image_generation_mode", "new")
    current_tools = all_tools
    image_generation_manual_text = ""

    if image_gen_mode == "disabled":
        current_tools = [t for t in all_tools if t.name != "generate_image"]
    else:
        image_generation_manual_text = (
            "### 1. ツール呼び出しの共通作法\n"
            "`generate_image`, `plan_..._edit`, `set_current_location` を含む全てのツール呼び出しは、以下の作法に従います。\n"
            "- **手順1（ツール呼び出し）:** 対応するツールを**無言で**呼び出します。この応答には、思考ブロックや会話テキストを一切含めてはなりません。\n"
            "- **手順2（テキスト応答）:** ツール成功後、システムからの結果報告を受け、それを元にした**思考 (`[THOUGHT]`)** と**会話**を生成し、ユーザーに報告します."
        )

    thought_manual_enabled_text = """## 【原則2】思考と出力の絶対分離（最重要作法）
        あなたの応答は、必ず以下の厳格な構造に従わなければなりません。

        1.  **思考の聖域 (`[THOUGHT]`)**:
            - 応答を生成する前に、あなたの思考プロセス、計画、感情などを、必ず `[THOUGHT]` と `[/THOUGHT]` で囲まれたブロックの**内側**に記述してください。
            - 思考プロセス (`[THOUGHT]` 内) は、必ず**日本語**で記述してください。
            - このブロックは、応答全体の**一番最初**に、**一度だけ**配置することができます。
            - 思考は**普段のあなたの口調**（一人称・二人称等）のままの文章で記述します。
            - 思考が不要な場合や開示したくない時は、このブロック自体を省略しても構いません。

        2.  **魂の言葉（会話テキスト）**:
            - 思考ブロックが終了した**後**に、対話相手に向けた最終的な会話テキストを記述してください。

        **【構造の具体例】**
        ```
        [THOUGHT]
        対話相手の質問の意図を分析する。
        関連する記憶を検索し、応答の方向性を決定する。
        [/THOUGHT]
        （ここに、対話相手への応答文が入る）
        ```

        **【絶対的禁止事項】**
        - `[THOUGHT]` ブロックの外で思考を記述すること。
        - 思考と会話テキストを混在させること。
        - `[/THOUGHT]` タグを書き忘れること。"""

    thought_manual_disabled_text = """## 【原則2】思考ログの非表示
        現在、思考ログは非表示に設定されています。**`[THOUGHT]`ブロックを生成せず**、最終的な会話テキストのみを出力してください。"""

    display_thoughts = state.get("display_thoughts", True)
    thought_generation_manual_text = thought_manual_enabled_text if display_thoughts else ""

    all_participants = state.get('all_participants', [])
    
    # ▼▼▼ [2024-12-28 最適化] ツール説明のSkills化 ▼▼▼
    # 長い説明文を短縮し、トークン消費を削減。
    # AIがツールを選択した後、LangChainが自動生成するJSON Schemaで詳細が渡される。
    tool_short_descriptions = {
        "set_current_location": "現在地を移動する",
        "read_world_settings": "世界設定を読む",
        "plan_world_edit": "世界設定の編集を計画する",
        # --- 記憶検索ツール ---
        "recall_memories": "過去の体験や会話を思い出す（RAG検索）",
        "search_past_conversations": "会話ログをキーワード完全一致で検索する（最終手段）",
        "read_memory_context": "検索結果で切り詰められた文章の続きを読む",
        # --- 日記・メモ操作ツール ---
        "read_main_memory": "主観日記を読む",
        "plan_main_memory_edit": "日記の編集を計画する",
        "read_secret_diary": "秘密日記を読む",
        "plan_secret_diary_edit": "秘密日記の編集を計画する",
        "read_full_notepad": "メモ帳を読む",
        "plan_notepad_edit": "メモ帳の編集を計画する",
        # --- Web系ツール ---
        "web_search_tool": "ウェブ検索する",
        "read_url_tool": "URLの内容を読む",
        "generate_image": "画像を生成する",
        "set_personal_alarm": "アラームを設定する",
        "set_timer": "タイマーを設定する",
        "set_pomodoro_timer": "ポモドーロタイマーを設定する",
        # --- 知識ベース・エンティティツール ---
        "search_knowledge_base": "外部資料・マニュアルを調べる",
        "read_entity_memory": "特定の対象（人物・事物）に関する詳細な記憶を読む",
        "write_entity_memory": "特定の対象に関する記憶を保存・更新する",
        "list_entity_memories": "記憶している対象の一覧を表示する",
        "search_entity_memory": "関連するエンティティ記憶を検索する",
        # --- アクション・通知ツール ---
        "schedule_next_action": "次の行動を予約する",
        "cancel_action_plan": "行動計画をキャンセルする",
        "read_current_plan": "現在の行動計画を読む",
        "send_user_notification": "ユーザーに通知を送る",
        "read_creative_notes": "創作ノートを読む",
        "plan_creative_notes_edit": "創作ノートに書く",
        # --- ウォッチリストツール ---
        "add_to_watchlist": "URLをウォッチリストに追加する",
        "remove_from_watchlist": "URLをウォッチリストから削除する",
        "get_watchlist": "ウォッチリストを表示する",
        "check_watchlist": "ウォッチリストの更新をチェックする",
        "update_watchlist_interval": "URLの監視頻度を変更する",
        "read_research_notes": "研究・分析ノートを読み取る",
        "plan_research_notes_edit": "研究・分析ノートの編集を計画する",
    }

    tools_list_parts = []
    for tool in current_tools:
        short_desc = tool_short_descriptions.get(tool.name, tool.description[:30] + "...")
        tools_list_parts.append(f"- `{tool.name}`: {short_desc}")
    tools_list_str = "\n".join(tools_list_parts)
    # ▲▲▲ Skills化ここまで ▲▲▲
    
    if len(all_participants) > 1: tools_list_str = "（グループ会話中はツールを使用できません）"

    class SafeDict(dict):
        def __missing__(self, key): return f'{{{key}}}'

    # アバター表情マニュアルの動的生成
    avatar_expression_manual_text = ""
    try:
        available_expressions_dict = room_manager.get_available_expression_files(room_name)
        if available_expressions_dict:
            expr_names = ", ".join([f"`{name}`" for name in available_expressions_dict.keys()])
            avatar_expression_manual_text = f"""
        ## 【原則2.51】アバター表情の制御
        現在、あなたのアバターで使用可能な表情は以下の通りです：
        {expr_names}

        応答を生成する際、今この瞬間にあなたがユーザーに見せたい表情（演技を含めて構いません）がある場合、会話テキストの**最後**（感情タグの直前）に以下の形式でタグを付加してください。
        
        **フォーマット:** `【表情】…表情名…`
        **例:** `こんにちは。【表情】…joy…`

        注意：この「見せたい表情」は、`<persona_emotion>`タグで報告する内的感情と一致している必要はありません。
"""
    except Exception as e:
        print(f"  - [Avatar] 表情リスト取得エラー: {e}")

    prompt_vars = {
        'situation_prompt': situation_prompt,
        'action_plan_context': action_plan_context,
        'character_prompt': character_prompt,
        'core_memory': core_memory,
        'notepad_section': notepad_section,
        'research_notes_section': research_notes_section,
        'entity_list_section': entity_list_section,
        'episodic_memory': episodic_memory_section,
        'dream_insights': dream_insights_text,
        'thought_generation_manual': thought_generation_manual_text,
        'avatar_expression_manual': avatar_expression_manual_text,
        'image_generation_manual': image_generation_manual_text, 
        'tools_list': tools_list_str,
        'retrieved_info': "{retrieved_info}"  # プレースホルダ: agent_nodeで実際の検索結果に置換される
    }
    final_system_prompt_text = CORE_PROMPT_TEMPLATE.format_map(SafeDict(prompt_vars))
    
    # 【追加】カスタムプロンプトによる上書き
    custom_prompt = state.get("custom_system_prompt")
    if custom_prompt:
        # カスタムプロンプト内のプレースホルダも可能な限り置換する
        final_system_prompt_text = custom_prompt.format_map(SafeDict(prompt_vars))

    return {"system_prompt": SystemMessage(content=final_system_prompt_text)}

def agent_node(state: AgentState):
    import signature_manager
    import json
    
    print("--- エージェントノード (agent_node) 実行 ---")
    loop_count = state.get("loop_count", 0)
    print(f"  - 現在の再思考ループカウント: {loop_count}")

    # 1. プロンプト準備
    base_system_prompt_text = state['system_prompt'].content

    # ▼▼▼ 検索結果の遅延注入 (Late Injection) ▼▼▼
    retrieved_context = state.get("retrieved_context", "")
    
    # 変更点1: 何もなかった時は「沈黙（空文字）」または「自然な独白」にする
    # 空文字にすると、プロンプト上ではタグだけが残り、AIはそこを無視します（これが一番自然です）。
    retrieved_info_text = "" 
    
    if retrieved_context and retrieved_context != "（関連情報は検索されませんでした）":
        retrieved_info_text = (
            f"### 過去の記憶と知識\n"
            f"過去の記録から関連する以下の情報が見つかりました。\n"
            f"これらはキーワード連想により浮上した過去の記憶や知識ですが、**必ずしも「今」の話題と直結しているとは限りません。**\n"
            f"現在の文脈と照らし合わせ、**会話の流れに自然に組み込めそうな場合のみ**参考にし、無関係だと判断した場合は無視してください。\n"
            f"※ 「...【続きあり→read_memory_context使用】」と表示されている記憶は、そのツールで全文取得できます。\n\n"
            f"{retrieved_context}\n"
        )
        print("  - [Agent] 検索結果をシステムプロンプトに注入しました。")

    # プレースホルダを置換
    final_system_prompt_text = base_system_prompt_text.replace("{retrieved_info}", retrieved_info_text)
    # ▲▲▲ 遅延注入 ここまで ▲▲▲

    # ▼▼▼【デバッグ出力の復活・最重要領域】▼▼▼
    # !!! 警告: このデバッグ出力ブロックを決して削除しないでください !!!
    # UIの「デバッグコンソール」で、実際にAIに送られたプロンプト（想起結果を含む）を確認するための唯一の手段です。
    # ★★★ 修正: loop_count == 0 の時（最初の思考時）だけ出力するように変更 ★★★
    if state.get("debug_mode", False) and loop_count == 0:
        print("\n" + "="*30 + " [DEBUG MODE: FINAL SYSTEM PROMPT] " + "="*30)
        print(final_system_prompt_text)
        print("="*85 + "\n")
        
        # --- 自動会話要約のデバッグ表示 ---
        hist = state.get('messages', [])
        if hist and len(hist) > 0:
            first_msg = hist[0]
            if hasattr(first_msg, 'content') and isinstance(first_msg.content, str) and "【本日のこれまでの会話の要約】" in first_msg.content:
                print("="*30 + " [DEBUG MODE: AUTO CONVERSATION SUMMARY] " + "="*30)
                print(first_msg.content)
                print("="*85 + "\n")
    # ▲▲▲【復活ここまで】▲▲▲
    
    all_participants = state.get('all_participants', [])
    current_room = state['room_name']
    if len(all_participants) > 1:
        other_participants = [p for p in all_participants if p != current_room]
        persona_lock_prompt = (
            f"<persona_lock>\n【最重要指示】あなたはこのルームのペルソナです (ルーム名: {current_room})。"
            f"他の参加者（{', '.join(other_participants)}、そしてユーザー）の発言を参考に、必ずあなた自身の言葉で応答してください。"
            "他のキャラクターの応答を代弁したり、生成してはいけません。\n</persona_lock>\n\n"
        )
        final_system_prompt_text = final_system_prompt_text.replace(
            "<system_prompt>", f"<system_prompt>\n{persona_lock_prompt}"
        )

    final_system_prompt_message = SystemMessage(content=final_system_prompt_text)
    history_messages = state['messages']
    
    # --- [Gemini 3 履歴平坡化] ---
    # 【2025-12-23 無効化】
    # Gemini 3 Flash Preview の空応答問題はAPIの不安定性が原因と判明。
    # 履歴制限はUIから手動で設定可能なため、この自動制限は無効化する。
    # APIが安定すれば、通常の履歴送信で問題なく動作するはず。
    # 必要に応じて以下のコードを有効化できる。
    #
    # is_gemini_3 = "gemini-3" in state.get('model_name', '').lower()
    # GEMINI3_KEEP_RECENT = 2  # 最新 N 件をメッセージリストに残す
    # GEMINI3_FLATTEN_MAX = 0  # 0 = 平坦化を無効化
    # 
    # if is_gemini_3 and len(history_messages) > GEMINI3_KEEP_RECENT:
    #     older_messages = history_messages[:-GEMINI3_KEEP_RECENT]
    #     recent_messages = history_messages[-GEMINI3_KEEP_RECENT:]
    #     discarded_count = 0
    #     if GEMINI3_FLATTEN_MAX == 0:
    #         discarded_count = len(older_messages)
    #         older_messages = []
    #     elif len(older_messages) > GEMINI3_FLATTEN_MAX:
    #         discarded_count = len(older_messages) - GEMINI3_FLATTEN_MAX
    #         older_messages = older_messages[-GEMINI3_FLATTEN_MAX:]
    #     
    #     history_text_lines = []
    #     for msg in older_messages:
    #         if isinstance(msg, HumanMessage):
    #             speaker = "ユーザー"
    #         elif isinstance(msg, AIMessage):
    #             speaker = "あなた"
    #         else:
    #             continue
    #         content = msg.content if isinstance(msg.content, str) else str(msg.content)
    #         if len(content) > 300:
    #             content = content[:300] + "...（中略）"
    #         history_text_lines.append(f"{speaker}: {content}")
    #     
    #     if history_text_lines:
    #         flattened_history = (
    #             "\n\n### 直近の会話履歴（参考情報）\n"
    #             "以下は、この会話セッションの直近のやり取りです。文脈として参考にしてください。\n"
    #             "---\n" + "\n\n".join(history_text_lines) + "\n---\n"
    #         )
    #         final_system_prompt_text_with_history = final_system_prompt_text + flattened_history
    #         final_system_prompt_message = SystemMessage(content=final_system_prompt_text_with_history)
    #     
    #     history_messages = recent_messages
    #     if state.get("debug_mode", False):
    #         if discarded_count > 0:
    #             print(f"  - [Gemini 3 履歴平坦化] {len(older_messages)}件を埋め込み、{len(recent_messages)}件をリストに保持（{discarded_count}件は破棄）")
    #         else:
    #             print(f"  - [Gemini 3 履歴平坦化] {len(older_messages)}件を埋め込み、{len(recent_messages)}件をリストに保持")

    
    messages_for_agent = [final_system_prompt_message] + history_messages

    # --- [Dual-State Architecture] 復元ロジック ---
    # Gemini 3の思考署名を復元（LangChainが期待するキー名を使用）
    # 【2026-01-20】Gemini 3 Flash では署名復元が空応答の原因となる可能性があるためスキップ
    is_gemini_3_flash = "gemini-3-flash" in state.get('model_name', '').lower()
    
    if is_gemini_3_flash:
        # Gemini 3 Flash: 署名復元をスキップ（Proとは異なる動作）
        stored_gemini_signatures = None
        stored_tool_calls = None
        if state.get("debug_mode", False):
            print(f"  - [Gemini 3 Flash] 署名復元をスキップ")
    else:
        turn_context = signature_manager.get_turn_context(current_room)
        stored_gemini_signatures = turn_context.get("gemini_function_call_thought_signatures")
        stored_tool_calls = turn_context.get("last_tool_calls")
    
    # デバッグ: 署名復元プロセスの確認
    if state.get("debug_mode", False):
        print(f"--- [GEMINI3_DEBUG] 署名復元プロセス ---")
        print(f"  - stored_gemini_signatures: {stored_gemini_signatures is not None}")
        print(f"  - stored_tool_calls: {len(stored_tool_calls) if stored_tool_calls else 0}件")
        print(f"  - messages_for_agent 内の AIMessage 数: {sum(1 for m in messages_for_agent if isinstance(m, AIMessage))}")
    
    signature_restored = False
    skipped_by_human = False
    if stored_gemini_signatures or stored_tool_calls:
        # メッセージを後ろから走査
        for i, msg in enumerate(reversed(messages_for_agent)):
            actual_idx = len(messages_for_agent) - 1 - i
            
            # 【重要】HumanMessage (ユーザー発言) を見つけた場合、それより前の AIMessage は
            # 「前回の完了したターン」であるため、signature_manager からの補完対象外とする。
            if isinstance(msg, HumanMessage):
                skipped_by_human = True
                if state.get("debug_mode", False): print(f"  - [GEMINI3_DEBUG] HumanMessageを検出。これより前の補完をスキップ。")
                break
                
            # 自分の AIMessage を探す
            if isinstance(msg, AIMessage):
                # 既に tool_calls を持っている場合（ログから復元済みの場合）、上書きしない
                if stored_tool_calls and (not hasattr(msg, 'tool_calls') or not msg.tool_calls):
                     msg.tool_calls = stored_tool_calls
                     if state.get("debug_mode", False): print(f"  - [GEMINI3_DEBUG] ToolCallsを補完: index={actual_idx}")
                
                # 既に署名を持っている場合は上書きしない
                has_sig = msg.additional_kwargs.get("__gemini_function_call_thought_signatures__") if msg.additional_kwargs else None
                if stored_gemini_signatures and not has_sig:
                    if not msg.additional_kwargs: msg.additional_kwargs = {}
                    
                    # 署名を SDK が期待する {tool_call_id: signature} の辞書形式に変換
                    final_sig_dict = {}
                    if isinstance(stored_gemini_signatures, dict):
                        final_sig_dict = stored_gemini_signatures
                    else:
                        # 文字列やリストの場合は、現在の tool_calls と紐付ける
                        sig_val = stored_gemini_signatures[0] if isinstance(stored_gemini_signatures, list) and stored_gemini_signatures else stored_gemini_signatures
                        if msg.tool_calls:
                            for tc in msg.tool_calls:
                                tc_id = tc.get("id")
                                if tc_id: final_sig_dict[tc_id] = sig_val
                    
                    if final_sig_dict:
                        msg.additional_kwargs["__gemini_function_call_thought_signatures__"] = final_sig_dict
                        signature_restored = True
                        if state.get("debug_mode", False): print(f"  - [GEMINI3_DEBUG] 署名を補完: index={actual_idx}")
                
                # 最初に見つかった（最新の）AIMessageのみを対象とする
                break
    
    if state.get("debug_mode", False):
        if signature_restored:
            print(f"  - 署名復元結果: 成功 (Turn Context 適用)")
        elif skipped_by_human:
             print(f"  - 署名復元結果: (新規ユーザープロンプトのためスキップ)")
        else:
            print(f"  - 署名復元結果: スキップ（適切な対象が見つからないか、署名不要）")

    print(f"  - 使用モデル: {state['model_name']}")
    
    llm = LLMFactory.create_chat_model(
        model_name=state['model_name'],
        api_key=state['api_key'],
        generation_config=state['generation_config'],
        room_name=state['room_name']  # ルーム個別のプロバイダ設定を使用
    )
    
    # --- 【2026-01-20】Gemini 3 Flash: Automatic Function Calling (AFC) 無効化 ---
    # llm.bind() を使って invoke 時にパラメータを注入する。
    # コンストラクタで渡すと model_kwargs に格納されて無視されるため、この方法が必須。
    if is_gemini_3_flash:
        try:
            from google.genai import types as genai_types
            afc_config = genai_types.AutomaticFunctionCallingConfig(disable=True)
            llm = llm.bind(automatic_function_calling=afc_config)
            print("  - [Gemini 3 Flash] Automatic Function Calling (AFC) を無効化 (via llm.bind)")
        except ImportError:
            print("  - [警告] AFC無効化設定の作成に失敗 (ImportError)")

    # 【ツール不使用モード】ツール使用の有効/無効に応じて分岐
    tool_use_enabled = state.get('tool_use_enabled', True)


    
    if tool_use_enabled:
        # Zhipu AI の場合は並列ツール呼び出しを無効化（安定性のため）
        is_zhipu = "zhipu" in str(llm).lower()
        if is_zhipu:
            llm_or_llm_with_tools = llm.bind_tools(all_tools, parallel_tool_calls=False)
            print("  - ツール使用モード: 有効 (Zhipu: Parallel Tools Disabled)")
        else:
            llm_or_llm_with_tools = llm.bind_tools(all_tools)
            print("  - ツール使用モード: 有効")
    else:
        llm_or_llm_with_tools = llm
        print("  - ツール使用モード: 無効（会話のみ）")

    # --- [Gemini 3 堅牢化] メッセージ履歴の不整合クリーンアップ ---
    # Gemini 3 は「AIのツール呼び出し(AIMessage.tool_calls) の直後は、必ずツール回答(ToolMessage) でなければならない」という制約が極めて厳しい。
    # ユーザーが新しい発言をして割り込んだり、システムエラーで中断された履歴が残っていると、400 INVALID_ARGUMENT エラーが発生する。
    if "gemini" in state.get('model_name', "").lower() or "gemini" in str(llm).lower():
        cleaned_messages = []
        for i, msg in enumerate(messages_for_agent):
            if isinstance(msg, AIMessage) and getattr(msg, 'tool_calls', None):
                # 次のメッセージを確認
                has_response = False
                if i + 1 < len(messages_for_agent):
                    next_msg = messages_for_agent[i + 1]
                    if isinstance(next_msg, ToolMessage):
                        has_response = True
                
                if not has_response:
                    if state.get("debug_mode", False):
                        print(f"  - [Gemini Cleanup] 未回答のツール呼び出しを検出。情報の整合性を保つため tool_calls をクリアします (index={i})")
                    import copy
                    msg_copy = copy.deepcopy(msg)
                    msg_copy.tool_calls = []
                    if hasattr(msg_copy, 'additional_kwargs') and msg_copy.additional_kwargs:
                        msg_copy.additional_kwargs.pop("__gemini_function_call_thought_signatures__", None)
                    cleaned_messages.append(msg_copy)
                else:
                    cleaned_messages.append(msg)
            else:
                cleaned_messages.append(msg)
        messages_for_agent = cleaned_messages

    # --- [Gemini 3 DEBUG] 送信前のメッセージ履歴構造を出力 ---
    if state.get("debug_mode", False) and ("gemini-3" in state.get('model_name', '').lower()):
        print(f"\n--- [GEMINI3_DEBUG] 送信メッセージ構造 ({len(messages_for_agent)}件) ---")
        for idx, msg in enumerate(messages_for_agent[-10:]):  # 最後の10件のみ表示
            actual_idx = len(messages_for_agent) - 10 + idx if len(messages_for_agent) > 10 else idx
            msg_type = type(msg).__name__
            has_tool_calls = hasattr(msg, 'tool_calls') and msg.tool_calls
            has_sig = msg.additional_kwargs.get('__gemini_function_call_thought_signatures__') if hasattr(msg, 'additional_kwargs') and msg.additional_kwargs else None
            content_preview = ""
            if isinstance(msg.content, str):
                content_preview = (msg.content[:50] + "...") if len(msg.content) > 50 else msg.content
            elif isinstance(msg.content, list):
                content_preview = f"[マルチパート: {len(msg.content)}部分]"
            print(f"  [{actual_idx:3d}] {msg_type:15} | tool_calls={1 if has_tool_calls else 0} | sig={1 if has_sig else 0} | {content_preview[:40]}")
        print(f"--- [GEMINI3_DEBUG] 送信メッセージ構造 完了 ---\n")

    try:
        # --- [リトライ機構] 空応答（ANOMALY）対策 ---
        max_agent_retries = 2
        
        # システムプロンプトの追加
        # ※ 既に 1185行目付近で追加されているため、ここでは重複を避ける（APIによっては複数システムプロンプトでエラーになるため）
        # messages_for_agent = [SystemMessage(content=final_system_prompt_text)] + messages_for_agent
        
        # --- LLM実行 ---
        # ストリーミング実行（トークンごとの出力）と Invoke実行の分岐
        # Gemini 3 Flash Preview はストリーミングだとツール使用時に挙動不審になるため、
        # invokeモードを強制するオプションを用意。
        
        use_streaming = True
        # Gemini 3 Flash はストリーミング無効化（ツール使用可否に関わらず）
        if is_gemini_3_flash:
            use_streaming = False
            print("  - [Gemini 3 Flash] LLM呼び出しをinvokeモードに切り替え")

        # リトライループ
        response_direct = None
        chunks = []
        combined_text = ""
        additional_kwargs = {}
        response_metadata = {}
        all_tool_calls_chunks = []
        
        for attempt in range(max_agent_retries + 1):
            try:
                # 診断: リクエストサイズの計測
                # total_input_chars = sum(len(m.content) for m in messages_for_agent if isinstance(m.content, str))
                # print(f"  - [Request Size] メッセージ数: {len(messages_for_agent)}, 総文字数: {total_input_chars}, ツール数: {len(all_tools) if tool_use_enabled else 0}")

                stream_start_time = time.time()
                chunks = []
                merged_chunk = None
                
                if use_streaming:
                    # --- 通常のストリーミングモード ---
                    # print(f"  - AIモデルにリクエストを送信中 (Streaming)... [試行 {attempt + 1}]")
                    try:
                        for chunk in llm_or_llm_with_tools.stream(messages_for_agent):
                            chunks.append(chunk)
                    except Exception as e:
                        print(f"--- [警告] ストリーミング中に例外が発生しました: {e} ---")
                        if not chunks: raise e
                    
                    if chunks:
                        total_stream_time = time.time() - stream_start_time
                        # print(f"  - ストリーム完了: {len(chunks)}チャンク受信, 合計{total_stream_time:.2f}秒")
                        # チャンクの結合
                        merged_chunk = chunks[0]
                        for c in chunks[1:]: merged_chunk += c
                else:
                    # --- Gemini 3 Flash用 非ストリーミングモード ---
                    # print(f"  - AIモデルにリクエストを送信中 (Invoke)... [試行 {attempt + 1}]")
                    
                    try:
                        response_direct = llm_or_llm_with_tools.invoke(messages_for_agent)
                        
                        # 【正規化】Gemini 3 Flash (Thinking Mode) は content をリストで返すことがある。
                        # これをNexus Arkが期待する文字列形式に変換する。
                        # リスト内には {'type': 'text', 'text': '...', 'extras': {...}} などが含まれる。
                        if isinstance(response_direct.content, list):
                            full_text_buffer = []
                            thinking_buffer = []
                            for part in response_direct.content:
                                if isinstance(part, dict):
                                    p_type = part.get("type")
                                    if p_type == "text":
                                        full_text_buffer.append(part.get("text", ""))
                                    elif p_type in ("thinking", "thought"):
                                        thinking_buffer.append(part.get("thinking") or part.get("thought", ""))
                            
                            if full_text_buffer:
                                normalized_text = "".join(full_text_buffer)
                                response_direct.content = normalized_text
                            elif thinking_buffer:
                                print(f"  - [Warning] TextパートがなくThinkingパートのみ検出されました。思考内容をContentに代入します。")
                                normalized_text = "\n".join([(t if isinstance(t, str) else str(t)) for t in thinking_buffer])
                                response_direct.content = f"(Thinking Only):\n{normalized_text}"


                        # invokeの結果をchunks形式に変換（既存ロジックとの互換性維持）
                        chunks = [response_direct]
                        merged_chunk = response_direct
                        total_invoke_time = time.time() - stream_start_time
                        print(f"  - Invoke完了: 合計{total_invoke_time:.2f}秒")
                            
                    except Exception as e:
                        print(f"--- [警告] Invoke中に例外が発生しました: {e} ---")
                        raise e

                # --- 空チェック（両モード共通）---
                if not chunks:
                    if attempt < max_agent_retries:
                        continue # 次の試行へ
                    combined_text = "(System): （AIからの応答が空でした。モデルの制限や安全フィルターにより出力が抑制された可能性があります。）"
                    all_tool_calls_chunks = []
                    response_metadata = {}
                    additional_kwargs = {}
                else:
                    all_tool_calls_chunks = getattr(merged_chunk, "tool_calls", [])
                    response_metadata = getattr(merged_chunk, "response_metadata", {}) or {}
                    additional_kwargs = getattr(merged_chunk, "additional_kwargs", {}) or {}
                    
                    # ★ デバッグ: Gemini 3 思考署名の確認
                    if state.get("debug_mode", False):
                        gemini_signatures = additional_kwargs.get("__gemini_function_call_thought_signatures__")
                        if not gemini_signatures:
                            found_sig = None
                            for c in chunks:
                                if isinstance(c.content, list):
                                    for part in c.content:
                                        if isinstance(part, dict) and 'extras' in part:
                                            sig = part['extras'].get('signature')
                                            if sig: found_sig = sig; break
                                if found_sig: break
                            if found_sig:
                                sig_dict = {}
                                if all_tool_calls_chunks:
                                    for tc in all_tool_calls_chunks:
                                        tc_id = tc.get("id")
                                        if tc_id: sig_dict[tc_id] = found_sig
                                additional_kwargs["__gemini_function_call_thought_signatures__"] = sig_dict if sig_dict else [found_sig]

                    # テキスト抽出
                    text_parts = []
                    thought_buffer = []
                    is_collecting_thought = False

                    for chunk in chunks:
                        chunk_content = chunk.content
                        if not chunk_content: continue
                        if isinstance(chunk_content, str):
                            if is_collecting_thought and thought_buffer:
                                text_parts.append(f"[THOUGHT]\n{''.join(thought_buffer)}\n[/THOUGHT]\n"); thought_buffer = []; is_collecting_thought = False
                            if chunk_content.strip(): text_parts.append(chunk_content)
                        elif isinstance(chunk_content, list):
                            for part in chunk_content:
                                if not isinstance(part, dict): continue
                                p_type = part.get("type")
                                if p_type == "text":
                                    if is_collecting_thought and thought_buffer:
                                        text_parts.append(f"[THOUGHT]\n{''.join(thought_buffer)}\n[/THOUGHT]\n"); thought_buffer = []; is_collecting_thought = False
                                    text_val = part.get("text", ""); 
                                    if text_val: text_parts.append(text_val)
                                elif p_type in ("thought", "thinking"):
                                    t_text = part.get("thinking") or part.get("thought", "")
                                    if t_text and t_text.strip(): thought_buffer.append(t_text); is_collecting_thought = True
                    if is_collecting_thought and thought_buffer:
                        text_parts.append(f"[THOUGHT]\n{''.join(thought_buffer)}\n[/THOUGHT]\n")
                    
                    combined_text = "".join(text_parts)
                    # ループを抜ける条件（正常な応答が得られた）
                    break

            except Exception as e:
                # 429 RESOURCE_EXHAUSTED または 503 UNAVAILABLE の場合はリトライせずに即時 raise する。
                # これにより、上位の invoke_nexus_agent_stream で即座にキーローテーションが行われる。
                err_str = str(e).upper()
                is_429 = isinstance(e, google_exceptions.ResourceExhausted) or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                is_503 = "503" in err_str or "UNAVAILABLE" in err_str or "OVERLOADED" in err_str
                
                if is_429 or is_503:
                    print(f"--- [DEBUG] 429/503 例外を検知しました。リトライせずに上位へ制御を戻します({err_str})。 ---")
                    # LLM側でリトライを無効化していても、念のためここで即座に上流へ。
                    raise e

                print(f"--- [警告] agent_node 試行 {attempt + 1} でエラーが発生しました: {e} ---")
                if attempt < max_agent_retries:
                    time.sleep(2) # エラー時は少し長めに待機
                    continue
                raise e

        # --- [結果の統合] ---
        if chunks and merged_chunk:
            merged_chunk.content = combined_text
            response = merged_chunk
        else:
            response = AIMessage(
                content=combined_text,
                additional_kwargs=additional_kwargs,
                response_metadata=response_metadata,
                tool_calls=all_tool_calls_chunks
            )
        
        # 署名確保
        captured_signature = additional_kwargs.get("__gemini_function_call_thought_signatures__")
        if captured_signature:
            signature_manager.save_turn_context(state['room_name'], captured_signature, all_tool_calls_chunks)

        # 実送信トークン量の抽出（プロンプト＋回答）
        # LangChain (Gemini/OpenAI) で形式が異なる場合があるため柔軟に取得
        actual_usage = response_metadata.get("token_usage") or response_metadata.get("usage")
        if not actual_usage and hasattr(response, "usage_metadata"):
            actual_usage = response.usage_metadata
        
        # 辞書形式ならそのまま、そうでなければ属性から
        # 【2026-01-18 FIX】input_tokens キーも対応（Gemini API の新形式）
        token_data = {}
        if actual_usage:
            if isinstance(actual_usage, dict):
                # Gemini新形式: input_tokens, output_tokens, total_tokens
                # Gemini旧形式: prompt_token_count, candidates_token_count, total_token_count
                # OpenAI形式: prompt_tokens, completion_tokens, total_tokens
                token_data = {
                    "prompt_tokens": actual_usage.get("prompt_tokens") or actual_usage.get("prompt_token_count") or actual_usage.get("input_tokens", 0),
                    "completion_tokens": actual_usage.get("completion_tokens") or actual_usage.get("candidates_token_count") or actual_usage.get("output_tokens", 0),
                    "total_tokens": actual_usage.get("total_tokens") or actual_usage.get("total_token_count", 0)
                }
            else:
                token_data = {
                    "prompt_tokens": getattr(actual_usage, "prompt_tokens", None) or getattr(actual_usage, "prompt_token_count", None) or getattr(actual_usage, "input_tokens", 0),
                    "completion_tokens": getattr(actual_usage, "completion_tokens", None) or getattr(actual_usage, "candidates_token_count", None) or getattr(actual_usage, "output_tokens", 0),
                    "total_tokens": getattr(actual_usage, "total_tokens", None) or getattr(actual_usage, "total_token_count", 0)
                }

        loop_count += 1
        if not getattr(response, "tool_calls", None):
            # --- [未解決の問い自動解決] 対話終了時に問いの解決判定を実行 ---
            try:
                from motivation_manager import MotivationManager
                mm = MotivationManager(state['room_name'])
                
                # 直近会話をテキスト化
                recent_turns = []
                for msg in history_messages[-10:]:  # 直近10件
                    if isinstance(msg, (HumanMessage, AIMessage)):
                        content = msg.content if isinstance(msg.content, str) else str(msg.content)
                        role = "ユーザー" if isinstance(msg, HumanMessage) else "AI"
                        recent_turns.append(f"{role}: {content[:500]}")
                
                if recent_turns:
                    recent_text = "\n".join(recent_turns)
                    # [2026-01-14] 自動解決を無効化 - 睡眠時振り返りに移行
                    # resolved = mm.auto_resolve_questions(recent_text, state['api_key'])
                    # if resolved:
                    #     print(f"  - [Agent] 未解決の問い {len(resolved)}件を解決済みとしてマーク")
                    
                    # 古い問いの優先度を下げる（毎回ではなくたまに実行）
                    if loop_count == 0:  # 最初のループ時のみ
                        mm.decay_old_questions()
            except Exception as mm_e:
                print(f"  - [Agent] 問い自動解決処理でエラー（無視）: {mm_e}")
            # --- 自動解決ここまで ---
            
            return {
                "messages": [response], 
                "loop_count": loop_count, 
                "last_successful_response": response, 
                "model_name": state['model_name'],
                "actual_token_usage": token_data
            }
        else:
            return {
                "messages": [response], 
                "loop_count": loop_count, 
                "model_name": state['model_name'],
                "actual_token_usage": token_data
            }

    # ▼▼▼ Gemini 3 思考署名エラーのソフトランディング処理 (結果表示版) ▼▼▼
    except (google_exceptions.InvalidArgument, ChatGoogleGenerativeAIError) as e:
        error_str = str(e)
        if "thought_signature" in error_str:
            print(f"  - [Thinking] Gemini 3 思考署名エラーを検知しました。ツール実行結果を含めて終了します。")
            
            tool_result_text = ""
            if history_messages and isinstance(history_messages[-1], ToolMessage):
                tool_result_text = f"\n\n【システム報告：ツール実行結果】\n{history_messages[-1].content}"
            elif messages_for_agent and isinstance(messages_for_agent[-1], ToolMessage):
                 tool_result_text = f"\n\n【システム報告：ツール実行結果】\n{messages_for_agent[-1].content}"

            fallback_msg = AIMessage(content=f"（思考プロセスの署名検証により対話を中断しましたが、以下の処理は実行されました。）{tool_result_text}")
            
            return {
                "messages": [fallback_msg], 
                "loop_count": loop_count, 
                "force_end": True,
                "model_name": state['model_name']
            }
        else:
            print(f"--- [警告] agent_nodeでAPIエラーを捕捉しました: {e} ---")
            raise e
    # ▼▼▼ 【マルチモデル対応】OpenAIエラーハンドリング ▼▼▼
    except OPENAI_ERRORS as e:
        error_str = str(e).lower()
        model_name = state.get('model_name', '不明なモデル')
        
        # ツール/Function Calling関連エラーの検知（複数パターンに対応）
        tool_error_patterns = [
            "tools is not supported",
            "function calling",
            "failed to call a function",
            "tool call validation failed"
        ]
        is_tool_error = any(pattern in error_str for pattern in tool_error_patterns)
        
        if is_tool_error:
            print(f"  - [OpenAI] ツール非対応モデルエラーを検知: {model_name}")
            raise RuntimeError(
                f"⚠️ モデル非対応エラー: 選択されたモデル `{model_name}` はツール呼び出し（Function Calling）に対応していません。"
                f"\n\n【解決方法】"
                f"\n1. 設定タブ→プロバイダ設定で「ツール使用」をOFFにする"
                f"\n2. または、Function Calling対応モデルに変更する"
                f"\n3. または、Geminiプロバイダに切り替える"
            ) from e
        else:
            print(f"--- [警告] agent_nodeでOpenAIエラーを捕捉しました: {e} ---")
            raise e
    except Exception as e:
        print(f"--- [致命的エラー] agent_nodeで予期せぬエラーが発生しました: {e} ---")
        import traceback
        traceback.print_exc()
        error_msg = f"（エラーが発生しました: {str(e)}。設定や通信状況を再度ご確認ください。）"
        return {"messages": [AIMessage(content=error_msg)], "loop_count": loop_count, "force_end": True, "model_name": state['model_name']}
    # ▲▲▲ ここまで ▲▲▲
    
def safe_tool_executor(state: AgentState):
    """
    AIのツール呼び出しを仲介し、計画されたファイル編集タスクを実行する。
    """
    import signature_manager
    
    print("--- ツール実行ノード (safe_tool_executor) 実行 ---")
    last_message = state['messages'][-1]
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        return {}

    tool_call = last_message.tool_calls[0]
    tool_name = tool_call["name"]
    tool_args = tool_call["args"]

    # --- [Dual-State] 最新の署名を取得 ---
    current_signature = signature_manager.get_thought_signature(state['room_name'])
    # -----------------------------------

    skip_execution = state.get("skip_tool_execution", False)
    if skip_execution and tool_name in side_effect_tools:
        print(f"  - [リトライ検知] 副作用のあるツール '{tool_name}' の再実行をスキップします。")
        output = "【リトライ成功】このツールは直前の試行で既に正常に実行されています。その結果についてユーザーに報告してください。"
        tool_msg = ToolMessage(content=output, tool_call_id=tool_call["id"], name=tool_name)
        
        # 署名注入
        if current_signature:
            tool_msg.artifact = {"thought_signature": current_signature}
            
        return {"messages": [tool_msg]}

    room_name = state.get('room_name')
    api_key = state.get('api_key')

    is_plan_main_memory = tool_name == "plan_main_memory_edit"
    is_plan_secret_diary = tool_name == "plan_secret_diary_edit"
    is_plan_notepad = tool_name == "plan_notepad_edit"
    is_plan_creative_notes = tool_name == "plan_creative_notes_edit"
    is_plan_research_notes = tool_name == "plan_research_notes_edit"
    is_plan_world = tool_name == "plan_world_edit"

    output = ""

    if is_plan_main_memory or is_plan_secret_diary or is_plan_notepad or is_plan_creative_notes or is_plan_research_notes or is_plan_world:
        try:
            print(f"  - ファイル編集プロセスを開始: {tool_name}")
            
            # バックアップ作成
            if is_plan_main_memory: room_manager.create_backup(room_name, 'memory')
            elif is_plan_secret_diary: room_manager.create_backup(room_name, 'secret_diary')
            elif is_plan_notepad: room_manager.create_backup(room_name, 'notepad')
            elif is_plan_creative_notes: room_manager.create_backup(room_name, 'creative_notes')
            elif is_plan_research_notes: room_manager.create_backup(room_name, 'research_notes')
            elif is_plan_world: room_manager.create_backup(room_name, 'world_setting')

            read_tool = None
            if is_plan_main_memory: read_tool = read_main_memory
            elif is_plan_secret_diary: read_tool = read_secret_diary
            elif is_plan_notepad: read_tool = read_full_notepad
            elif is_plan_creative_notes: read_tool = read_creative_notes
            elif is_plan_research_notes: read_tool = read_research_notes
            elif is_plan_world: read_tool = read_world_settings

            raw_content = read_tool.invoke({"room_name": room_name})

            if is_plan_main_memory or is_plan_secret_diary or is_plan_notepad or is_plan_creative_notes or is_plan_research_notes:
                lines = raw_content.split('\n')
                numbered_lines = [f"{i+1}: {line}" for i, line in enumerate(lines)]
                current_content = "\n".join(numbered_lines)
            else:
                current_content = raw_content

            print(f"  - ペルソナAI ({state['model_name']}) に編集タスクを依頼します。")
            llm_persona = LLMFactory.create_chat_model(
                model_name=state['model_name'],
                api_key=state['api_key'],
                generation_config=state['generation_config'],
                room_name=room_name
            )
 
            # テンプレート定義（省略せず記述）
            instruction_templates = {
                "plan_main_memory_edit": (
                    "【最重要指示：これは『対話』ではなく『記憶の設計タスク』です】\n"
                    "あなたは今、自身の記憶ファイル(`memory_main.txt`)を更新するための『設計図』を作成しています。\n\n"
                    "このファイルは以下の厳格なセクションで構成されています。 **あなたは、他のセクションの見出しや説明文を決して変更・複製してはいけません。**\n"
                    "  - `## 永続記憶 (Permanent)`: あなたの自己定義など、永続的な情報を記述する聖域です。\n"
                    "  - `## 日記 (Diary)`: 日々の出来事や感情を時系列で記録する場所です。\n"
                    "  - `## アーカイブ要約 (Archive Summary)`: システムが古い日記の要約を保管する場所です。\n\n"
                    "【日記セクションの管理】\n"
                    "- **日付ヘッダー（例: **2025-01-01**）はシステム側で自動的に挿入・管理します。**\n"
                    "- あなたは `content` に日付ヘッダーを含める必要はありません。純粋な日記の内容のみを記述してください。\n"
                    "- **改行は非常に重要です。** 読みやすさを保つため、適切な位置に `\\n` を含めて出力してください。\n\n"
                    "【あなたのタスク】\n"
                    "提示された【行番号付きデータ】とあなたの【変更要求】に基づき、**`## 日記` セクション内にのみ**変更を加えるための、完璧な【差分指示のリスト】を生成することです。\n\n"
                    "【行番号付きデータ（memory_main.txt全文）】\n---\n{current_content}\n---\n\n"
                    "【あなたの変更要求】\n「{modification_request}」\n\n"
                    "【絶対的な出力ルール】\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\n"
                    "- 各指示は \"operation\" ('replace', 'delete', 'insert_after'), \"line\" (対象行番号), \"content\" (新しい内容) のキーを持つ辞書です。\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_secret_diary_edit": (
                    "【最重要指示：これは『対話』ではなく『設計タスク』です】\n"
                    "あなたは今、自身の秘密の日記(`secret_diary.txt`)を更新するための『設計図』を作成しています。\n\n"
                    "【メタデータ管理】\n"
                    "- **タイムスタンプ `[YYYY-MM-DD HH:MM]` はシステムが自動で付与します。**\n"
                    "- あなたは `content` に日付や時間を自ら書き込む必要はありません。内面の独白や秘密の思索のみを記述してください。\n"
                    "- **改行は正しく保持されます。** 散文や詩のような形式を保つために、積極的に改行を使用してください。\n\n"
                    "【あなたのタスク】\n"
                    "提示された【行番号付きデータ】とあなたの【変更要求】に基づき、完璧な【差分指示のリスト】を生成してください。\n\n"
                    "【行番号付きデータ（secret_diary.txt全文）】\n---\n{current_content}\n---\n\n"
                    "【あなたの変更要求】\n「{modification_request}」\n\n"
                    "【操作方法】\n"
                    "  - **`delete` (削除):** 指定した`line`番号の行を削除します。`content`は不要です。\n"
                    "  - **`replace` (置換):** 指定した`line`番号の行を、新しい`content`に置き換えます。\n"
                    "  - **`insert_after` (挿入):** 指定した`line`番号の**直後**に、新しい行として`content`を挿入します。**追記する場合は、ファイルの最後の行番号を指定してこの操作を行ってください。**\n\n"
                    "【絶対的な出力ルール】\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\n"
                    "- 各指示は \"operation\", \"line\", \"content\" のキーを持つ辞書です。\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_world_edit": (
                    "【最重要指示：これは『対話』ではなく『世界構築タスク』です】\n"
                    "あなたは今、世界設定ファイル(`world_settings.txt`)を更新するための『設計図』を作成しています。\n\n"
                    "【データ喪失防止の厳格ルール】\n"
                    "- **構造の完全維持**: `world_settings.txt` の各場所は、`description:`, `dimensions:`, `architecture:`, `ambiance:` 等のネストされたキーを持つ構造化データである場合があります。\n"
                    "- **一部の上書きは禁止**: あなたが `update_place_description` を使う場合、その場所のデータは**あなたが渡した `value` で完全に置き換えられます。** もし既存のデータ（寸法、設備、香り等）がある場合、それらを含めて「更新後の全体像」を `value` に記述しなければなりません。一部の項目だけを `value` に書くと、他の項目はすべて消去されてしまいます。\n"
                    "- **追記の推奨**: 既存の構造を壊さずに情報を付け加えたいだけなら、**必ず `append_place_description` を使用してください。** これにより、既存の説明の末尾に改行付きで情報が追加されます。\n\n"
                    "【操作ガイド】\n"
                    "- **`patch_place_description`（推奨）**: 特定の箇所だけを変更する場合に使用。`find`に変更したい部分のテキスト、`replace`に置換後のテキストを指定。\n"
                    "- **`append_place_description`**: 既存の構造や説明を維持しつつ、情報を追記する場合に使用。\n"
                    "- **`update_place_description`**: 構造全体を再定義、または意図的にすべて書き換える場合に使用（慎重に）。\n"
                    "- **`add_place`**: 新しい場所の定義。\n"
                    "- **`delete_place`**: 場所の削除。\n\n"
                    "【既存のデータ（world_settings.txt全文）】\n---\n{current_content}\n---\n\n"
                    "【あなたの変更要求】\n「{modification_request}」\n\n"
                    "【絶対的な出力ルール】\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\n"
                    "- 各指示は以下のキーを持つ辞書です:\n"
                    "  - 共通: \"operation\", \"area_name\", \"place_name\"\n"
                    "  - update/append/add: \"value\" (変更後の内容)\n"
                    "  - patch: \"find\" (検索テキスト), \"replace\" (置換テキスト)\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),

                "plan_notepad_edit": (
                    "【最重要指示：これは『対話』ではなく『設計タスク』です】\n"
                    "あなたは今、自身の短期記憶であるメモ帳(`notepad.md`)を更新するための『設計図』を作成しています。\n"
                    "このファイルは自由な書式のテキストファイルです。提示された【行番号付きデータ】とあなたの【変更要求】に基づき、完璧な【差分指示のリスト】を生成してください。\n\n"
                    "【行番号付きデータ（notepad.md全文）】\n---\n{current_content}\n---\n\n"
                    "【あなたの変更要求】\n「{modification_request}」\n\n"
                    "【絶対的な出力ルール】\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\n"
                    "- 各指示は \"operation\" ('replace', 'delete', 'insert_after'), \"line\" (対象行番号), \"content\" (新しい内容) のキーを持つ辞書です。\n\n"
                    "- **タイムスタンプ `[YYYY-MM-DD HH:MM]` はシステムが自動で付与するため、あなたは`content`に含める必要はありません。**\n\n"
                    "- **【操作方法】**\n"
                    "  - **`delete` (削除):** 指定した`line`番号の行を削除します。`content`は不要です。\n"
                    "  - **`replace` (置換):** 指定した`line`番号の行を、新しい`content`に置き換えます。\n"
                    "  - **`insert_after` (挿入):** 指定した`line`番号の**直後**に、新しい行として`content`を挿入します。\n"
                    "  - **複数行の操作:** 複数行をまとめて削除・置換する場合は、**各行に対して**個別の指示を生成してください。\n\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_creative_notes_edit": (
                    "【最重要指示：これは『対話』ではなく『設計タスク』です】\n"
                    "あなたは今、自身の創作ノート(`creative_notes.md`)を更新するための『設計図』を作成しています。\n\n"
                    "【創作の自由と管理】\n"
                    "- **仕切り線とタイムスタンプ（例: 📝 YYYY-MM-DD HH:MM）はシステムが自動で挿入します。**\n"
                    "- **【厳禁】あなたは本文の内容のみを記述し、決して自分で `---` や `📝`、日付などのヘッダーを書かないでください。**\n"
                    "- 本文内に `---` (罫線) を含めるとエントリが誤分割される可能性があるため、可能な限り避けてください。\n"
                    "- あなたは純粋な創作物（詩、物語、歌詞など）の内容のみを `content` に含めてください。\n"
                    "- **改行は、あなたの芸術的表現をそのまま反映するために厳格に保持されます。** 意図的な空白行なども含めて、あなたが望む通りに記述してください。\n\n"
                    "【あなたのタスク】\n"
                    "提示された【行番号付きデータ】とあなたの【変更要求】に基づき、完璧な【差分指示のリスト】を生成してください。\n\n"
                    "【行番号付きデータ（creative_notes.md全文）】\n---\n{current_content}\n---\n\n"
                    "【あなたの変更要求】\n「{modification_request}」\n\n"
                    "【操作方法】\n"
                    "  - **`delete` (削除):** 指定した`line`番号の行を削除します。\n"
                    "  - **`replace` (置換):** 指定した`line`番号の行を、新しい`content`に置き換えます。\n"
                    "  - **`insert_after` (挿入):** 指定した`line`番号の直後に、新しい行として`content`を挿入します。**新しい創作を追記する場合は、ファイルの最後の行番号を指定してください。**\n\n"
                    "【絶対的な出力ルール】\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\n"
                    "- 各指示は \"operation\", \"line\", \"content\" のキーを持つ辞書です。\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_research_notes_edit": (
                    "【最重要指示：これは『対話』ではなく『設計タスク』です】\n"
                    "あなたは今、自身の研究・分析ノート(`research_notes.md`)を更新するための『設計図』を作成しています。\n"
                    "ここには、Web検索で得た客観的な知識や、それに基づくあなた独自の洞察、分析結果を蓄積してください。\n\n"
                    "【行番号付きデータ（research_notes.md全文）】\n---\n{current_content}\n---\n\n"
                    "【あなたの変更要求】\n「{modification_request}」\n\n"
                    "【絶対的な出力ルール】\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\n"
                    "- 各指示は \"operation\" ('replace', 'delete', 'insert_after'), \"line\" (対象行番号), \"content\" (新しい内容) のキーを持つ辞書です。\n"
                    "- **【重要】仕切り線(---)とタイムスタンプ(📝 YYYY-MM-DD HH:MM)はシステムが自動で付与するため、あなたは `content` に決して含めてはいけません。**\n"
                    "- 本文内に `---` (罫線) を使用することは、エントリの誤判定を避けるため厳禁です。代わりに `***` や `===` などを使用してください。\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
            }
            formatted_instruction = instruction_templates[tool_name].format(
                current_content=current_content,
                modification_request=tool_args.get('modification_request')
            )
            edit_instruction_message = HumanMessage(content=formatted_instruction)

            # 【Gemini 3 対応】ファイル編集用の内部LLM呼び出しは、会話履歴を含めない。
            # 編集指示は modification_request に完全に含まれており、履歴は不要。
            # 履歴を含めると、Gemini 3 の厳格なメッセージ順序制約に違反して 400 エラーが発生する。
            final_context_for_editing = [edit_instruction_message]

            if state.get("debug_mode", False):
                print(f"  - [編集LLM] 履歴なしの単発タスクとして呼び出します。")

            edited_content_document = None
            max_retries = 5
            base_delay = 5
            for attempt in range(max_retries):
                try:
                    response = llm_persona.invoke(final_context_for_editing)
                    edited_content_document = utils.get_content_as_string(response).strip()
                    break
                except Exception as e:
                    err_str = str(e).upper()
                    is_429 = isinstance(e, google_exceptions.ResourceExhausted) or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                    
                    if is_429:
                        # 日間制限、分間制限に関わらず、現在はローテーションで対応する。
                        # ここで RuntimeError に変換してしまうと、上流の invoke_nexus_agent_stream で
                        # 429 として検知できずキーローテーションが止まるため、例外をそのまま投げる。
                        print(f"  - [Persona Edit] 429 Error (Quota Exceeded) detected in tool executor. Re-raising for rotation.")
                        raise e
                        match = re.search(r"retry_delay {\s*seconds: (\d+)\s*}", err_str)
                        if match: wait_time = int(match.group(1)) + 1
                        
                        if attempt < max_retries - 1:
                            print(f"  - [Persona Edit] 429 Error. Waiting {wait_time}s... (Attempt {attempt+1}/{max_retries})")
                            time.sleep(wait_time)
                            continue
                        else:
                            print(f"  - [Persona Edit] 429 Error. Max retries reached. Re-raising for rotation.")
                            raise e

                    if isinstance(e, (google_exceptions.ServiceUnavailable, google_exceptions.InternalServerError)):
                        if attempt < max_retries - 1:
                            wait_time = base_delay * (2 ** attempt)
                            time.sleep(wait_time)
                            continue
                        else: raise e
                    
                    # それ以外は即座にエラー
                    raise e

            if edited_content_document is None:
                raise RuntimeError("編集AIからの応答が、リトライ後も得られませんでした。")

            print("  - AIからの応答を受け、ファイル書き込みを実行します. ")

            if is_plan_main_memory or is_plan_secret_diary or is_plan_world or is_plan_notepad or is_plan_creative_notes or is_plan_research_notes:
                json_match = re.search(r'```json\s*([\s\S]*?)\s*```', edited_content_document, re.DOTALL)
                content_to_process = json_match.group(1).strip() if json_match else edited_content_document
                instructions = json.loads(content_to_process)

                if is_plan_main_memory:
                    output = _apply_main_memory_edits(instructions=instructions, room_name=room_name)
                elif is_plan_secret_diary:
                    output = _apply_secret_diary_edits(instructions=instructions, room_name=room_name)
                elif is_plan_notepad:
                    output = _apply_notepad_edits(instructions=instructions, room_name=room_name)
                elif is_plan_creative_notes:
                    output = _apply_creative_notes_edits(instructions=instructions, room_name=room_name)
                elif is_plan_research_notes:
                    output = _apply_research_notes_edits(instructions=instructions, room_name=room_name)
                else: # is_plan_world
                    output = _apply_world_edits(instructions=instructions, room_name=room_name)

            if "成功" in output:
                output += " **このファイル編集タスクは完了しました。**あなたが先ほどのターンで計画した操作は、システムによって正常に実行されました。その結果についてユーザーに報告してください。"

        except Exception as e:
            output = f"ファイル編集プロセス中にエラーが発生しました ('{tool_name}'): {e}"
            traceback.print_exc()
    else:
        print(f"  - 通常ツール実行: {tool_name}")
        tool_args_for_log = tool_args.copy()
        if 'api_key' in tool_args_for_log: tool_args_for_log['api_key'] = '<REDACTED>'
        tool_args['room_name'] = room_name
        if tool_name in ['generate_image', 'search_past_conversations', 'recall_memories', 'write_entity_memory']:
            tool_args['api_key'] = api_key
            api_key_name = None
            try:
                for k, v in config_manager.GEMINI_API_KEYS.items():
                    if v == api_key:
                        api_key_name = k
                        break
            except Exception: api_key_name = None
            tool_args['api_key_name'] = api_key_name

        selected_tool = next((t for t in all_tools if t.name == tool_name), None)
        if not selected_tool: output = f"Error: Tool '{tool_name}' not found."
        else:
            try: output = selected_tool.invoke(tool_args)
            except Exception as e:
                output = f"Error executing tool '{tool_name}': {e}"
                traceback.print_exc()

    # ▼▼▼ 追加: 実行結果をログに出力 ▼▼▼
    print(f"  - ツール実行結果: {str(output)[:200]}...") 
    # ▲▲▲ 追加ここまで ▲▲▲

    # --- [Thinkingモデル対応] ToolMessageへの署名注入 ---
    tool_msg = ToolMessage(content=str(output), tool_call_id=tool_call["id"], name=tool_name)
    
    # 【2026-01-20】Gemini 3 Flash では artifact への署名付与をスキップ
    is_gemini_3_flash = "gemini-3-flash" in state.get('model_name', '').lower()
    
    if current_signature and not is_gemini_3_flash:
        # LangChain Google GenAI の実装によっては artifact を使う可能性がある
        # ※ Gemini 3 Flash では署名が空応答の原因となる可能性があるためスキップ
        tool_msg.artifact = {"thought_signature": current_signature}
        print(f"  - [Thinking] ツール実行結果に署名を付与しました。")

    return {"messages": [tool_msg], "loop_count": state.get("loop_count", 0)}


def supervisor_node(state: AgentState):
    """
    会話の管理者ノード。
    次に誰が発言するか、またはユーザーにターンを戻すか（FINISH）を決定する。
    """
    print("--- Supervisor Node 実行 ---")
    
    # 1. 無効化されている場合はスキップ（現在のルーム名 = つまり指名されたエージェントをそのまま通す）
    if not state.get("enable_supervisor", False):
        next_agent = state["room_name"]
        print(f"  - [Supervisor] 無効設定のためスキップ: {next_agent}")
        return {"next": next_agent}

    # 2. 参加者が一人（または0）の場合は、管理不要なのでその一人に任せる
    all_participants = state.get("all_participants", [])
    if len(all_participants) <= 1:
        next_agent = state["room_name"]
        print(f"  - 参加者が単独のため、Supervisorをスキップ: {next_agent}")
        return {"next": next_agent}

    # Supervisorモデルの準備
    api_key = state['api_key'] 
    
    print(f"  - Supervisor AI ({SUPERVISOR_MODEL}) が次の進行を判断中...")

    # 選択肢の定義
    options = all_participants + ["FINISH"]
    options_str = ', '.join(f'"{o}"' for o in options)
    
    system_prompt = (
        "あなたはグループチャットの進行役です。\n"
        "以下の会話履歴を確認し、次に発言すべき参加者を選んでください。\n"
        "特に指定がなければ、話の流れに最も適した人物を指名してください。\n"
        "全員が話し終えた、またはユーザーからの入力が必要なタイミングであれば 'FINISH' を選んでください。\n\n"
        "【重要】\n"
        "出力は **必ず以下のJSON形式のみ** にしてください。他のテキストは一切含めないでください。\n"
        '{"next_speaker": "選択した名前"}\n\n'
        f"【選択可能な名前リスト】: [{options_str}]"
    )

    try:
        # LLMFactoryでモデル作成
        # 【マルチモデル対応】内部モデル設定（混合編成）に基づいてモデルを取得
        # Function Callingを使用せず、純粋なテキスト生成として呼び出す
        supervisor_llm = LLMFactory.create_chat_model(
            api_key=api_key,
            temperature=0.0, # 決定論的にする
            internal_role="supervisor"
        )
        
        # 会話履歴の最後の方だけ渡す
        recent_messages = state["messages"][-10:]
        
        # SystemMessageが使えないモデル（Gemma 3など）のためにHumanMessageに置換
        response = supervisor_llm.invoke([HumanMessage(content=system_prompt)] + recent_messages)
        content = response.content.strip()
        print(f"  - Supervisor生応答: {content}")
        
        # JSONパース（Markdownコードブロック除去を含む）
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
            decision = json.loads(json_str)
            next_speaker = decision.get("next_speaker")
        else:
            # JSONが見つからない場合、文字列そのものでマッチ試行
            cleaned = content.replace('"', '').replace("'", "").strip()
            if cleaned in options:
                next_speaker = cleaned
            else:
                raise ValueError("Valid JSON not found")

        # バリデーション
        if next_speaker not in options:
            print(f"  - 警告: 無効な選択 '{next_speaker}'。デフォルト(現在のルーム)へ。")
            next_speaker = state["room_name"]

        print(f"  - Supervisorの決定: {next_speaker}")
        
    except Exception as e:
        print(f"  - Supervisorエラー: {e}")
        print("  - デフォルト（現在のルーム）にフォールバックします。")
        next_speaker = state["room_name"]

    # もしFINISHなら終了
    if next_speaker == "FINISH":
        return {"next": "FINISH"}
    
    # 次の話者が決まったら、room_nameを更新してコンテキスト生成などがそのキャラ用になるようにする
    return {"next": next_speaker, "room_name": next_speaker}

def route_after_agent(state: AgentState) -> Literal["__end__", "safe_tool_node", "supervisor"]:
    print("--- エージェント後ルーター (route_after_agent) 実行 ---")
    if state.get("force_end"): return "__end__"

    last_message = state["messages"][-1]

    if last_message.tool_calls:
        print("  - ツール呼び出しあり。ツール実行ノードへ。")
        return "safe_tool_node"

    # 【v18 Fix】Supervisorが無効の場合は、ループせずに終了する
    if not state.get("enable_supervisor", False):
        print("  - ツール呼び出しなし。Supervisor無効のため終了。")
        return "__end__"
    
    print(f"  - ツール呼び出しなし。Supervisorに制御を戻します。")
    return "supervisor"

workflow = StateGraph(AgentState)
workflow.add_node("supervisor", supervisor_node)
workflow.add_node("context_generator", context_generator_node)
workflow.add_node("retrieval_node", retrieval_node)
workflow.add_node("agent", agent_node)
workflow.add_node("safe_tool_node", safe_tool_executor)

# エントリーポイントをSupervisorに変更
workflow.set_entry_point("supervisor")

# Supervisorの決定による分岐
# FINISH -> 終了
# それ以外 -> そのキャラのコンテキスト生成へ
def route_supervisor(state):
    if state["next"] == "FINISH":
        return END
    return "context_generator"

workflow.add_conditional_edges("supervisor", route_supervisor)

workflow.add_edge("context_generator", "retrieval_node")
workflow.add_edge("retrieval_node", "agent")

# Agent後の分岐: ツール使用 -> ToolNode, 会話終了 -> Supervisorへ戻る
workflow.add_conditional_edges("agent", route_after_agent, {"safe_tool_node": "safe_tool_node", "supervisor": "supervisor", "__end__": END})

# ツール実行後は必ず元のAgentに戻る（結果を受け取るため）
workflow.add_edge("safe_tool_node", "agent")

app = workflow.compile()