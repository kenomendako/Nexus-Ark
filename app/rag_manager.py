# rag_manager.py (v6: Incremental Save / Checkpoint System)

import gc
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import List, Dict, Optional, Set, Tuple, Union
import traceback
import logging
import json
import hashlib
import math
from datetime import datetime

from langchain_core.embeddings import Embeddings
from langchain_community.vectorstores import FAISS
from langchain_community.docstore.document import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from google.api_core import exceptions as google_exceptions

import constants
import config_manager
import utils
import psutil

# ロギング設定
logger = logging.getLogger(__name__)


class RotatingEmbeddings(Embeddings):
    """
    APIキーローテーションに対応したエンベディングラッパー。
    FAISSオブジェクト等に渡された後でも、RAGManagerの最新のAPIキーに基づく
    エンベディングモデルを動的に使用する。
    """
    def __init__(self, manager: 'RAGManager'):
        self.manager = manager
    
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # [2026-04-28 安定化] 検索時と索引作成時のベクトル不一致を防ぐため task_type を統一
        actual = self.manager._get_actual_embeddings(task_type="retrieval_document")
        try:
            embeddings = actual.embed_documents(texts)
            if len(embeddings) == len(texts):
                return embeddings
            
            # 長さが一致しない場合（API側で一部がフィルタリングされた可能性など）
            print(f"      [RAG Warning] ベクトル化結果の数が一致しません (Docs:{len(texts)}, Embs:{len(embeddings)})。個別処理に切り替えます。")
        except Exception as e:
            # ネットワークエラー等は上位（_create_index_in_batches）のリトライに任せるが、
            # 明らかな引数/形式エラーや長さ不一致エラーの場合は個別処理を試みる
            err_msg = str(e)
            if "equal length" in err_msg or "400" in err_msg:
                print(f"      [RAG Warning] 一括ベクトル化でエラーが発生しました。個別処理を試みます: {e}")
            else:
                raise e

        # 個別処理によるフォールバック
        results = []
        for i, t in enumerate(texts):
            try:
                # 1件ずつ処理することで、問題のあるチャンクを特定・スキップ可能にする
                emb = actual.embed_query(t)
                results.append(emb)
            except Exception as ee:
                print(f"      ! チャンク [{i}] のベクトル化に失敗しました (スキップ): {ee}")
                # 失敗したチャンクには0ベクトルを詰め、インデックス全体の崩壊を防ぐ
                # 次元数は既存の結果から取得するか、取得できなければ再度試行
                dim = 768 # Gemini Embedding のデフォルト
                if results and len(results[0]) > 0:
                    dim = len(results[0])
                results.append([0.0] * dim)
        
        return results
    
    def embed_query(self, text: str) -> List[float]:
        perf_start = time.time()
        # [2026-04-28 安定化] 検索時と索引作成時のベクトル不一致を防ぐため task_type を統一
        result = self.manager._get_actual_embeddings(task_type="retrieval_document").embed_query(text)
        print(f"--- [PERF] RotatingEmbeddings.embed_query took: {time.time() - perf_start:.4f}s ---")
        return result




class RAGManager:
    # インデックスをメモリ上に保持するキャッシュ {str(path): (FAISS_db, timestamp)}
    _index_cache: Dict[str, Tuple[FAISS, float]] = {}

    @classmethod
    def clear_cache(cls):
        """
        メモリ上のインデックスキャッシュをクリアする。
        OSレベルのファイルロックを解除するために、マイグレーション前などに呼び出す。
        """
        if cls._index_cache:
            print(f"[RAGManager] Clearing {len(cls._index_cache)} cached indices from memory.")
            cls._index_cache.clear()
        gc.collect() # メモリ解放とファイルハンドル解放を促す

    def __init__(self, room_name: str, api_key: str):
        self.room_name = room_name
        self.api_key = api_key
        
        # --- [API Key Rotation Limit] ---
        # このセッションで試行したキーとその回数を記録
        self.tried_keys: Dict[str, int] = {}
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.rag_data_dir = self.room_dir / "rag_data"
        
        # [v0.2.0-fix] Legacy path support: "faiss_index" -> "faiss_index_static"
        # 配布パッケージなどで古いフォルダ名のままになっている場合、互換性を維持して読み込む
        legacy_index_path = self.rag_data_dir / "faiss_index"
        self.static_index_path = self.rag_data_dir / "faiss_index_static"
        
        if legacy_index_path.exists() and not self.static_index_path.exists():
            print(f"[RAGManager] Legacy index detected ('faiss_index'). Using as static index.")
            # 読み取り専用で運用されることが多いため、リネームせず参照先を変更するか、
            # あるいは次回保存時に static に移行されるようにパスだけセットする。
            # ここではシンプルに「存在すればそちらを読む」ようにパス変数を上書きする手もあるが、
            # 保存時に混乱するため、起動時にリネームを試みるのが最善。
            try:
                legacy_index_path.rename(self.static_index_path)
                print(f"  -> Renamed to 'faiss_index_static' for consistency.")
            except Exception as e:
                print(f"  -> Rename failed ({e}). Using legacy path for read-only access.")
                self.static_index_path = legacy_index_path

        self.dynamic_index_path = self.rag_data_dir / "faiss_index_dynamic"
        self.processed_files_record = self.rag_data_dir / "processed_static_files.json"
        
        self.rag_data_dir.mkdir(parents=True, exist_ok=True)

        # [v0.2.6-fix] Windowsの日本語パス問題を事前検出して警告
        # FAISSのネイティブC++コードは非ASCII文字を含むパスを開けない場合がある
        if os.name == 'nt':
            try:
                # [2026-04-29 FIX] ルーム名（キャラクター名）のみが日本語の場合は回避策があるため警告しない
                # Nexus Ark本体の設置パス（charactersフォルダの親以上）が日本語を含む場合のみ警告する
                rooms_parent_path = Path(constants.ROOMS_DIR).resolve()
                if any(ord(c) > 127 for c in str(rooms_parent_path)):
                    utils.add_system_notice(
                        "RAG警告: インデックスの保存先パスに日本語等の非ASCII文字が含まれています。"
                        "FAISSが正常に動作しない可能性があります。"
                        "【対処法】もしエラーが出る場合は、Nexus Arkを英数字のみのパス（例: C:\\nexus_ark）に移動することをお勧めします。",
                        level="warning"
                    )
                    print(f"[RAGManager] ⚠️ 非ASCIIパス検出 (Base): {rooms_parent_path}")
            except Exception:
                pass

        # エンベディングモードを設定から取得
        effective_settings = config_manager.get_effective_settings(room_name)
        self.embedding_mode = effective_settings.get("embedding_mode", "api")
        
        # エンベディングの初期化
        self.actual_embeddings = {}  # 実際のGoogle/Localインスタンス
        self.wrapper_embeddings = RotatingEmbeddings(self) # FAISS等に渡す永続ラッパー
        print(f"[RAGManager] エンベディングモード: {self.embedding_mode} (遅延初期化待ち)")

    def _get_embedding_model_id(self) -> str:
        """現在のエンベディング設定を識別するユニークなIDを返す"""
        settings = config_manager.get_internal_model_settings()
        provider = settings.get("embedding_provider", "google")
        # [2026-04-29 FIX] "gemini" は旧形式の値。内部的には "google" として処理する
        if provider == "gemini":
            provider = "google"
        
        if self.embedding_mode == "local":
            model_name = settings.get("embedding_model", "unknown-local")
            return f"local:{model_name}"
        else:
            model_name = settings.get("embedding_model", constants.EMBEDDING_MODEL)
            # モデル名からカッコ書きなどを除去
            model_name = utils.sanitize_model_name(model_name)
            return f"{provider}:{model_name}"

    def _get_embeddings(self):
        """FAISS等に渡すための、キー回転に追従するラッパーを取得する"""
        return self.wrapper_embeddings

    def _get_actual_embeddings(self, task_type="retrieval_document"):
        """実際のエンベディングインスタンスを取得（必要に応じて初期化/再生成）"""
        perf_start = time.time()
        
        if task_type in self.actual_embeddings:
            return self.actual_embeddings[task_type]
        
        if self.embedding_mode == "local":
            try:
                # 非常に重いライブラリをここで初めて呼ぶ
                from langchain_community.embeddings import HuggingFaceEmbeddings
                
                settings = config_manager.get_internal_model_settings()
                raw_model_name = settings.get("embedding_model", "multilingual-e5-small")
                
                # E5などの有名モデルはフルパスにマッピング
                if raw_model_name == "multilingual-e5-small":
                    hf_model_id = "intfloat/multilingual-e5-small"
                elif raw_model_name == "multilingual-e5-base":
                    hf_model_id = "intfloat/multilingual-e5-base"
                elif raw_model_name == "multilingual-e5-large":
                    hf_model_id = "intfloat/multilingual-e5-large"
                elif raw_model_name == "paraphrase-multilingual-MiniLM-L12-v2":
                    hf_model_id = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
                else:
                    hf_model_id = raw_model_name
                
                print(f"[RAGManager] ローカルエンベディングモデルをロード中: {hf_model_id}")
                self.actual_embeddings[task_type] = HuggingFaceEmbeddings(model_name=hf_model_id)
                print(f"[RAGManager] ローカルエンベディング ({self._get_embedding_model_id()}) を初期化しました")
            except Exception as e:
                # 警告を強調
                print(f"\n" + "!"*60)
                print(f"[RAGManager] 警告: ローカルエンベディングの初期化に失敗しました。")
                print(f"  - 原因: {e}")
                
                # システム通知を追加
                utils.add_system_notice(
                    f"RAG警告: ローカルエンベディングの初期化に失敗し、Google APIにフォールバックしました (原因: {e})",
                    level="warning"
                )
                settings = config_manager.get_internal_model_settings()
                model_name = settings.get("embedding_model", constants.EMBEDDING_MODEL)
                model_name = utils.sanitize_model_name(model_name)
                print(f"  - 処置: Google API ({model_name}) にフォールバックします。")
                print(f"!"*60 + "\n")
                
                from langchain_google_genai import GoogleGenerativeAIEmbeddings
                self.actual_embeddings[task_type] = GoogleGenerativeAIEmbeddings(
                    model=model_name,
                    google_api_key=self.api_key,
                    task_type=task_type
                )
        else:
            # API エンベディングを使用 (Google または OpenAI)
            settings = config_manager.get_internal_model_settings()
            provider = settings.get("embedding_provider", "google")
            # [2026-04-29 FIX] "gemini" は旧形式の値。内部的には "google" として処理する
            if provider == "gemini":
                provider = "google"
            model_name = settings.get("embedding_model", constants.EMBEDDING_MODEL)
            model_name = utils.sanitize_model_name(model_name)

            if provider == "openai":
                try:
                    from langchain_openai import OpenAIEmbeddings
                    # アクティブなOpenAIプロファイルからAPIキーとBaseURLを取得
                    openai_setting = config_manager.get_active_openai_setting()
                    if openai_setting:
                        self.actual_embeddings[task_type] = OpenAIEmbeddings(
                            model=model_name,
                            openai_api_key=openai_setting.get("api_key"),
                            openai_api_base=openai_setting.get("base_url")
                        )
                        print(f"[RAGManager] OpenAI エンベディング ({self._get_embedding_model_id()}) を初期化しました (Profile: {openai_setting.get('name')})")
                    else:
                        raise ValueError("有効なOpenAIプロファイルが見つかりません。")
                except Exception as e:
                    print(f"[RAGManager] OpenAI エンベディングの初期化に失敗しました: {e}")
                    # フォールバック: Google
                    provider = "google"

            if provider == "google":
                # Gemini API エンベディングを使用
                from langchain_google_genai import GoogleGenerativeAIEmbeddings
                self.actual_embeddings[task_type] = GoogleGenerativeAIEmbeddings(
                    model=model_name,
                    google_api_key=self.api_key,
                    task_type=task_type
                )
                print(f"[RAGManager] Gemini API エンベディング ({self._get_embedding_model_id()}) を初期化しました (Task: {task_type}, Key: {config_manager.get_key_name_by_value(self.api_key)})")
            
        print(f"--- [PERF] RAGManager._get_actual_embeddings took: {time.time() - perf_start:.4f}s ---")
        return self.actual_embeddings[task_type]

        
    def _rotate_api_key(self, error_str: str) -> Union[str, bool]:
        """
        429エラー時にAPIキーをローテーションする。
        制限の種類（PerMinute vs PerDay）を判別し、適切に処理する。
        
        Returns:
            "waited":   待機完了（同じキーでリトライ可能）
            "switched": キー切替済み（新しいキーでリトライ可能）
            False:      リトライ不可（全キー枯渇、または日次上限でローテーションOFF）
        """
        if not ("429" in error_str or "ResourceExhausted" in error_str):
            return False

        if self.embedding_mode == "local":
            return False

        # --- [核心]制限種類の判定 ---
        import re
        is_daily_limit = "PerDay" in error_str or "Daily" in error_str
        is_per_minute = "PerMinute" in error_str
        
        # APIが提案するリトライ待機時間を抽出
        retry_delay = None
        match = re.search(r'retryDelay.*?(\d+(?:\.\d+)?)s', error_str)
        if match:
            retry_delay = float(match.group(1))

        # 現在のキー名を特定
        key_name = config_manager.get_key_name_by_value(self.api_key)

        # --- Case A: 1分あたりの制限 (PerMinute) ---
        # [2026-04-28 fix] retry_delay がある場合はRPM制限とみなし、十分な待機を行う。
        # RPMは60秒でリセットされるため、3回までは同じキーでリトライする。
        if (is_per_minute or retry_delay) and not is_daily_limit:
            wait_count = self.tried_keys.get(self.api_key, 0)
            if wait_count >= 3:
                print(f"      [RAG Rotation] RPM limit persisted after {wait_count} waits on '{key_name}'. Forcing key switch...")
                is_daily_limit = True # Case B へ落として切り替えさせる
            else:
                wait = min(retry_delay + 5, 75) if retry_delay else 60
                print(f"      [RAG Rotation] PerMinute limit on '{key_name}' (wait {wait_count+1}/3). Waiting {wait:.1f}s...")
                time.sleep(wait)
                self.tried_keys[self.api_key] = wait_count + 1
                return "waited"

        # --- Case B: 1日あたりの制限 (PerDay) または判別不能な制限 ---
        if key_name != "Unknown":
            # [2026-04-26 強化] モデル名を明示して、Text(Chat)等への影響を回避
            model_id = self._get_embedding_model_id()
            clean_model_name = model_id.split(":")[-1] if ":" in model_id else model_id
            config_manager.mark_key_as_exhausted(key_name, model_name=clean_model_name)
            
            # 有料キーは mark_key_as_exhausted 内でスキップされるが、ここでのtried_keys追加は続行
            print(f"      [RAG Rotation] Key '{key_name}' marked as exhausted for model '{clean_model_name}'.")
        
        # ローテーション設定確認
        # [2026-03-20 FIX] RAG操作はシステム全体の設定を優先すべきため、グローバル設定を第一参照とする
        rotation_enabled = config_manager.CONFIG_GLOBAL.get("enable_api_key_rotation")
        
        # もしグローバル設定が明示的に設定されていない場合のみ、ルーム設定やデフォルト(True)を参照
        if rotation_enabled is None:
            effective_settings = config_manager.get_effective_settings(self.room_name)
            rotation_enabled = effective_settings.get("enable_api_key_rotation", True)
            
        if rotation_enabled:
            # [2026-04-28 fix] model_name を渡し、このEmbeddingモデルでの枚渇のみをチェックさせる
            next_key_name = config_manager.get_next_available_gemini_key(
                current_exhausted_key=key_name,
                excluded_keys=set(self.tried_keys.keys()),
                model_name=clean_model_name
            )
            if next_key_name and next_key_name not in self.tried_keys:
                next_key_val = config_manager.GEMINI_API_KEYS.get(next_key_name)
                if next_key_val:
                    print(f"      [RAG Rotation] Switching key: {key_name} -> {next_key_name} (Daily Limit Support)")
                    self.api_key = next_key_val
                    self.tried_keys[next_key_name] = 0  # [2026-04-28 fix] .add() -> dict 代入
                    self.actual_embeddings = {} # 次の _get_actual_embeddings() で新キーで再生成
                    return "switched"
        
        # [2026-02-11 FIX] 有料キーの救済措置: 代替が見つからないが、現在のキーが有料なら待機して再試行
        paid_keys = config_manager.CONFIG_GLOBAL.get("paid_api_key_names", [])
        if key_name in paid_keys:
            print(f"      [RAG Rotation] Paid key '{key_name}' throttled. Waiting 5s for backoff...")
            time.sleep(5)
            # tried_keys から現在のキーを削除して再試行可能にし、Trueを返す
            self.tried_keys.pop(key_name, None)  # [2026-04-28 fix] .remove() -> .pop()
            self.actual_embeddings = {}  # [2026-04-28 fix] None -> {} (NoneだとTypeError)
            return "waited"

        print(f"      [RAG Rotation] No more keys available to try in this session.")
        return False

    def _load_processed_record(self) -> Set[str]:
        if self.processed_files_record.exists():
            try:
                with open(self.processed_files_record, 'r', encoding='utf-8') as f:
                    return set(json.load(f))
            except Exception:
                return set()
        return set()

    def _save_processed_record(self, processed_files: Set[str]):
        with open(self.processed_files_record, 'w', encoding='utf-8') as f:
            json.dump(list(processed_files), f, indent=2, ensure_ascii=False)

    def _filter_meaningful_chunks(self, splits: List[Document]) -> List[Document]:
        """
        チャンク分割後に無意味なチャンクを除外する。
        - 短すぎるチャンク（10文字未満）
        - マークダウン記号のみのチャンク（*, -, #, **等）
        """
        MIN_CONTENT_LENGTH = 10
        # 除外対象のパターン: マークダウン記号のみ
        MEANINGLESS_PATTERNS = {'*', '-', '#', '**', '***', '---', '##', '###', '####'}
        
        filtered = []
        filtered_count = 0
        for doc in splits:
            content = doc.page_content.strip()
            # 短すぎるチャンクを除外
            if len(content) < MIN_CONTENT_LENGTH:
                filtered_count += 1
                continue
            # マークダウン記号のみのチャンクを除外
            if content in MEANINGLESS_PATTERNS:
                filtered_count += 1
                continue
            filtered.append(doc)
        
        if filtered_count > 0:
            print(f"    [FILTER] 無意味なチャンク {filtered_count}件を除外")
        
        return filtered

    def classify_query_intent(self, query: str) -> dict:
        """
        クエリの意図を分類し、Intent-Aware Retrievalの重みを返す。
        
        Returns:
            {
                "intent": "emotional" | "factual" | "technical" | "temporal" | "relational",
                "weights": {"alpha": float, "beta": float, "gamma": float}
            }
        """
        # クエリが空の場合は即座にデフォルトを返す
        if not query or not str(query).strip():
            return {
                "intent": constants.DEFAULT_INTENT,
                "weights": constants.INTENT_WEIGHTS[constants.DEFAULT_INTENT]
            }

        # [2026-02-11 FIX] 試行済みキーをリセットして全キーを試行可能にする
        self.tried_keys.clear()
        
        max_retries = 3
        attempt = 0
        while attempt < max_retries:
            try:
                from llm_factory import LLMFactory
                
                llm = LLMFactory.create_chat_model(
                    internal_role="processing",
                    api_key=self.api_key,
                    generation_config={}
                )
                
                prompt = """あなたはクエリ分類の専門家です。以下のクエリを5つのカテゴリのいずれか1つに分類してください。

カテゴリ:
- emotional: 感情・体験・思い出を問う（例：「あの時どう思った？」「嬉しかったこと」「初めて会った日」）
- factual: 事実・属性を問う（例：「猫の名前は？」「誕生日いつ？」「好きな食べ物」）
- technical: 技術・手順・設定を問う（例：「設定方法は？」「どうやって動かす？」「バージョン」）
- temporal: 時間軸で問う（例：「最近何した？」「昨日の話」「今週の予定」）
- relational: 関係性を問う（例：「〇〇との関係は？」「誰と仲良い？」「どんな人？」）

クエリ: {query}

カテゴリ名のみを1単語で回答してください（emotional/factual/technical/temporal/relational）:"""

                raw_response = llm.invoke(prompt.format(query=query)).content
                import utils
                response = utils.extract_text_from_llm_content(raw_response).lower()
                
                # 応答からIntentを抽出
                intent = constants.DEFAULT_INTENT
                for valid_intent in constants.INTENT_WEIGHTS.keys():
                    if valid_intent in response:
                        intent = valid_intent
                        break
                
                weights = constants.INTENT_WEIGHTS.get(intent, constants.INTENT_WEIGHTS[constants.DEFAULT_INTENT])
                print(f"  - [Intent] Query: '{query[:30]}...' -> {intent} (α={weights['alpha']}, β={weights['beta']}, γ={weights['gamma']})")
                
                return {"intent": intent, "weights": weights}
                
            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "ResourceExhausted" in error_str:
                    print(f"  - [Intent] API制限検知 (試行 {attempt+1}/{max_retries}): {e}")
                    res = self._rotate_api_key(error_str)
                    if res == "waited":
                        print(f"    -> 待機してリトライ中...")
                        continue
                    elif res == "switched":
                        print(f"    -> キーを切り替えました。リトライ中...")
                        attempt = 0
                        continue
                
                if attempt >= max_retries - 1:
                    print(f"  - [Intent] 分類エラー、デフォルト使用: {e}")
                    return {
                        "intent": constants.DEFAULT_INTENT,
                        "weights": constants.INTENT_WEIGHTS[constants.DEFAULT_INTENT]
                    }
                time.sleep(2)
                attempt += 1

    def calculate_time_decay(self, metadata: dict) -> float:
        """
        メタデータの日付から時間減衰スコアを計算する。
        
        Args:
            metadata: {"date": "2026-01-15", ...} または {"created_at": "2026-01-15 10:00:00", ...}
        
        Returns:
            0.0（非常に古い）～ 1.0（今日）
        """
        import math
        from datetime import datetime, timedelta
        
        # 日付を抽出（複数のフォーマットに対応）
        date_str = metadata.get("date") or metadata.get("created_at", "")
        
        if not date_str:
            return 0.5  # 日付不明は中立
        
        try:
            # 日付部分のみを抽出（"2026-01-15" or "2026-01-15 10:00:00"）
            date_part = str(date_str).split()[0]
            
            # 日付範囲の場合（"2026-01-01~2026-01-07"）は最新日を使用
            if "~" in date_part:
                date_part = date_part.split("~")[-1]
            
            record_date = datetime.strptime(date_part, "%Y-%m-%d")
            today = datetime.now()
            days_ago = (today - record_date).days
            
            if days_ago < 0:
                return 1.0  # 未来の日付は最新扱い
            
            # 指数減衰: decay = e^(-rate × days)
            decay_score = math.exp(-constants.TIME_DECAY_RATE * days_ago)
            return decay_score
            
        except Exception as e:
            # パースエラー時は中立
            return 0.5

    def _safe_save_index(self, db: FAISS, target_path: Path):
        """インデックスを安全に保存する（リネーム退避方式、同一ディレクトリ内一時保存）"""
        target_path = Path(target_path)
        parent_dir = target_path.parent
        
        # 0. ファイルシステムの書き込みチェック
        check_file = parent_dir / f".write_test_{int(time.time())}"
        try:
            check_file.touch()
            check_file.unlink()
        except OSError as e:
            if e.errno == 30: # Read-only file system
                print(f"  - [RAG Error] ファイルシステムが読み取り専用です。WSLの再起動やディスク修復が必要です。")
                raise
            else:
                print(f"  - [RAG Warning] 書き込みテスト失敗: {e}")

        # 1. 保存の一時ディレクトリ選定（転送の儀式）
        # Windowsかつ日本語パスの場合、直接保存しようとするとFAISSが失敗するため、
        # まずはシステムの一時ディレクトリ（通常は英数字）に書き出し、後で移動させる。
        is_windows_non_ascii = (os.name == 'nt' and any(ord(c) > 127 for c in str(target_path.resolve())))
        
        # Windows日本語環境ならシステム標準(%TEMP%)、そうでなければアプリ内の.tmpを使用
        tmp_base_dir = None if is_windows_non_ascii else (self.rag_data_dir / ".tmp")
        if tmp_base_dir:
            tmp_base_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix=".tmp_index_", dir=(str(tmp_base_dir) if tmp_base_dir else None)) as temp_dir:
            temp_path = Path(temp_dir)
            db.save_local(str(temp_path))
            
            # Windows/WSLでのファイルロック・競合に対応するためのリトライループ
            max_retries = 3
            for attempt in range(max_retries):
                # 退避用のパス（ハッシュ付きで衝突回避）
                old_path = parent_dir / (target_path.name + f".old_{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}")
                
                try:
                    if target_path.exists():
                        # まずリネームによる退避を試みる
                        try:
                            target_path.rename(old_path)
                        except Exception:
                            # リネーム失敗時は GC を呼んでから削除（従来方式）
                            gc.collect()
                            time.sleep(0.5)
                            if target_path.exists():
                                shutil.rmtree(str(target_path))
                    
                    # 新しいインデックスを rename で配置
                    shutil.move(str(temp_path), str(target_path))
                    
                    # モデル情報を保存
                    try:
                        model_info = {
                            "model_id": self._get_embedding_model_id(),
                            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        with open(target_path / "model_info.json", 'w', encoding='utf-8') as f:
                            json.dump(model_info, f, indent=2)
                    except Exception as e:
                        print(f"  - [RAG Warning] モデル情報の保存に失敗: {e}")

                    # キャッシュをクリア
                    cache_key = str(target_path.absolute())
                    if cache_key in RAGManager._index_cache:
                        del RAGManager._index_cache[cache_key]

                    # 成功
                    self._cleanup_old_indices(parent_dir, target_path.name)
                    return 
                    
                except (PermissionError, OSError) as e:
                    if attempt < max_retries - 1:
                        time.sleep(1)
                        continue
                    
                    # Windows特有の不備（非ASCII文字パスでの FileIOWriter 失敗など）へのアドバイス
                    error_msg = str(e)
                    if any(ord(c) > 127 for c in str(target_path)):
                        print(f"  - [RAG Error] パスに日本語（非ASCII文字）が含まれているため、保存に失敗した可能性があります: {target_path}")
                        print(f"  - 推奨: Nexus Arkを日本語を含まないパス (例: C:\\nexus_ark) に移動することを検討してください。")
                    
                    raise e

    def _cleanup_old_indices(self, parent_dir: Path, base_name: str):
        """退避された古い .old フォルダをクリーンアップする"""
        try:
            for old_dir in parent_dir.glob(f"{base_name}.old_*"):
                if old_dir.is_dir():
                    try:
                        shutil.rmtree(str(old_dir))
                    except Exception:
                        pass # 削除できない場合は諦める（次回以降に期待）
        except Exception:
            pass

    def _safe_load_index(self, target_path: Path) -> Optional[FAISS]:
        """インデックスを安全に読み込む（キャッシュ対応）"""
        perf_start = time.time()
        if not target_path or not target_path.exists():
            return None
        
        # パスを絶対パスかつ文字列として正規化（キャッシュキー用）
        target_abs_path = str(target_path.resolve())
        mtime = target_path.stat().st_mtime
        
        # キャッシュの有効性チェック
        if target_abs_path in RAGManager._index_cache:
            cache_db, cache_mtime = RAGManager._index_cache[target_abs_path]
            if cache_mtime == mtime:
                print(f"--- [Cache Hit] RAGManager._safe_load_index: {target_path.name} ---")
                return cache_db
        
        print(f"--- [Cache Miss] RAGManager._safe_load_index: Loading from disk: {target_path.name} ---")
        
        # FAISS.load_local を直接実行
        try:
            # 1. 整合性チェック
            model_info_path = target_path / "model_info.json"
            if model_info_path.exists():
                try:
                    with open(model_info_path, 'r', encoding='utf-8') as f:
                        info = json.load(f)
                    saved_model_id = info.get("model_id")
                    current_model_id = self._get_embedding_model_id()
                    
                    if saved_model_id and saved_model_id != current_model_id:
                        print(f"\n" + "="*60)
                        print(f"⚠️ [RAG 警告] エンベディングモデルの不一致を検知しました！")
                        print(f"  - 索引作成時: {saved_model_id}")
                        print(f"  - 現在の設定: {current_model_id}")
                        print(f"  ※このまま検索すると、精度が著しく低下するか、エラーが発生します。")
                        print(f"  ※解決するには『索引の完全再構築』を行ってください。")
                        print("="*60 + "\n")
                        
                        try:
                            msg = (f"RAG警告: 索引のモデル（{saved_model_id}）と現在の設定（{current_model_id}）が不一致です。"
                                   "検索精度が低下するかエラーが発生する可能性があるため、RAG管理から『完全再構築』を行ってください。")
                            utils.add_system_notice(msg, level="warning")
                            # チャットログにも記録
                            utils.add_system_message_to_chat_log(self.room_name, msg)
                        except Exception as e:
                            print(f"[RAGManager] Failed to add system notice: {e}")
                except Exception:
                    pass

            # 埋め込みモデルを取得（遅延初期化対応）
            embeddings = self._get_embeddings()
            
            # 2. ロード実行 (転送の儀式)
            # Windowsかつ日本語パスの場合、FAISS(C++)が直接ファイルを開けないため、
            # 英数字のみで構成された一時的な聖域にコピーしてからロードさせる。
            is_windows_non_ascii = (os.name == 'nt' and any(ord(c) > 127 for c in str(target_path.resolve())))
            
            if is_windows_non_ascii:
                # 聖域の儀式: システム管理下の安全な場所へ一時コピー
                with tempfile.TemporaryDirectory(prefix="faiss_sacred_") as temp_dir:
                    temp_path = Path(temp_dir)
                    # インデックスフォルダの中身を一時フォルダに複製
                    shutil.copytree(str(target_path), str(temp_path), dirs_exist_ok=True)
                    
                    # 聖域からロード
                    db = FAISS.load_local(
                        str(temp_path),
                        embeddings,
                        allow_dangerous_deserialization=True
                    )
            else:
                # 崇高なる直接ロード (Linux/Mac または英数字パスのWindows)
                db = FAISS.load_local(
                    str(target_path),
                    embeddings,
                    allow_dangerous_deserialization=True
                )
            
            # キャッシュに保存（この時点で db はメモリ上に展開されている）
            RAGManager._index_cache[target_abs_path] = (db, mtime)
            print(f"--- [PERF] RAGManager._safe_load_index({target_path.name}) took: {time.time() - perf_start:.4f}s ---")
            return db
                
        except Exception as e:
            error_str = str(e)
            print(f"  - [RAG Error] インデックス読み込み失敗 ({target_path.name}): {e}")
            # traceback.print_exc()
            
            # [v0.2.6-fix] 非ASCIIパスが原因の可能性を診断してUI通知
            try:
                full_path_str = str(target_path.resolve())
                has_non_ascii = any(ord(c) > 127 for c in full_path_str)
                is_faiss_io_error = any(kw in error_str for kw in ["io.cpp", "could not open", "No such file", "FileIOWriter", "FileIOReader"])
                if has_non_ascii and is_faiss_io_error:
                    utils.add_system_notice(
                        f"RAGエラー: インデックスの読み込みに失敗しました。"
                        f"パスに日本語等の非ASCII文字が含まれているため、FAISSが動作できない可能性があります。"
                        f"【対処法】Nexus Arkを英数字のみのパス（例: C:\\\\nexus_ark）に移動し、"
                        f"移動後に『索引を初期化して再構築』を実行してください。",
                        level="error"
                    )
                elif not has_non_ascii:
                    # パス問題ではない場合もエラーをUIに通知
                    utils.add_system_notice(
                        f"RAGエラー: インデックス({target_path.name})の読み込みに失敗しました。"
                        f"RAG管理から『索引を初期化して再構築』を実行してください。（詳細: {error_str[:100]}）",
                        level="error"
                    )
            except Exception:
                pass
            
            return None

    def _create_index_in_batches(self, splits: List[Document], existing_db: Optional[FAISS] = None, 
                                   progress_callback=None, save_callback=None, status_callback=None) -> FAISS:
        """
        大量のドキュメントをバッチ分割し、レート制限を回避しながらインデックスを作成/追記する。
        progress_callback: 進捗を報告するコールバック関数 (batch_num, total_batches) -> None
        save_callback: 途中保存用コールバック関数 (db) -> None（定期的に呼び出される）
        status_callback: UIへ進捗メッセージを送信するコールバック関数 (message) -> None
        """
        # [2026-04-28] Free Tierの100 RPM制限を考慮し、バッチサイズを縮小 (20->10)
        BATCH_SIZE = 10
        SAVE_INTERVAL_BATCHES = 20  # 20バッチごと（約200チャンクごと）に途中保存
        db = existing_db
        total_splits = len(splits)
        total_batches = (total_splits + BATCH_SIZE - 1) // BATCH_SIZE
        
        print(f"    [BATCH] 開始: {total_splits} チャンク, {total_batches} バッチ (途中保存: {SAVE_INTERVAL_BATCHES}バッチごと)")
        if status_callback:
            status_callback(f"索引処理開始: {total_splits}チャンク, {total_batches}バッチ")
        if progress_callback:
            progress_callback(0, total_batches)

        for i in range(0, total_splits, BATCH_SIZE):
            # --- [MEMORY MONITORING] ---
            # 512MB以下の空きメモリしかない場合は中断検討
            available_mem_mb = psutil.virtual_memory().available / (1024 * 1024)
            if available_mem_mb < 512:
                print(f"    [WARNING] 低メモリ状態検知 ({available_mem_mb:.1f}MB)。GC実行...")
                gc.collect()
                time.sleep(2)
                available_mem_mb = psutil.virtual_memory().available / (1024 * 1024)
                if available_mem_mb < 300:
                    print(f"    [CRITICAL] メモリ不足のためインデックス作成を中断します。")
                    if status_callback: status_callback("メモリ不足のため中断")
                    return db

            batch = splits[i : i + BATCH_SIZE]
            batch_num = (i // BATCH_SIZE) + 1
            
            # リトライループ
            max_retries = 3
            attempt = 0
            while attempt < max_retries:
                try:
                    if db is None:
                        db = FAISS.from_documents(batch, self._get_embeddings())
                    else:
                        db.add_documents(batch)
                    
                    # 進捗を報告
                    if progress_callback:
                        progress_callback(batch_num, total_batches)
                    
                    if self.embedding_mode == "api":
                        # [2026-04-28] 待機時間を延長 (2s->5s) してRPMを抑制
                        time.sleep(5) 
                    break 
                
                except Exception as e:
                    error_str = str(e)
                    print(f"      ! ベクトル化エラー (試行 {attempt+1}/{max_retries}): {e}")
                    if "429" in error_str or "ResourceExhausted" in error_str:
                        # --- [API Key Rotation] ---
                        res = self._rotate_api_key(error_str)
                        if res == "waited":
                            if status_callback:
                                status_callback(f"API制限(分間) - 待機して同じキーでリトライ...")
                            continue # attempt は増やさずリトライ
                        elif res == "switched":
                            if status_callback:
                                status_callback(f"API制限(日間) - キーを切り替えてリトライします...")
                            attempt = 0 # リセット
                            continue

                        # ローテーション不可の場合
                        is_daily = "PerDay" in error_str or "Daily" in error_str
                        if is_daily:
                            print(f"      ! 日次上限到達（ローテーション不可）。処理を中断します。")
                            if status_callback:
                                status_callback("APIの日次上限に達したため処理を中断しました")
                            return db  # 現在までのdbを返して中断
                        
                        wait_time = 10 * (attempt + 1)
                        print(f"      ! API制限検知（ローテーション不可）。{wait_time}秒待機してリトライ...")
                        if status_callback:
                            status_callback(f"API制限 - {wait_time}秒待機中...")
                        time.sleep(wait_time)
                    else:
                        if attempt >= max_retries - 1:
                            print(f"      ! このバッチをスキップします。最終エラー: {e}")
                            traceback.print_exc()
                        if self.embedding_mode == "api":
                            time.sleep(5)
                        break
                    
                    attempt += 1            
            # 定期進捗報告と途中保存（100バッチごと）
            if batch_num % SAVE_INTERVAL_BATCHES == 0:
                progress_pct = int((batch_num / total_batches) * 100)
                print(f"    [PROGRESS] {batch_num}/{total_batches} バッチ完了 ({progress_pct}%)")
                if status_callback:
                    status_callback(f"索引処理中: {batch_num}/{total_batches} ({progress_pct}%)")
                # 途中保存
                if save_callback and db:
                    print(f"    [SAVE] 途中保存実行...")
                    save_callback(db)
                
                # 20バッチごとにGC
                gc.collect()
        
        print(f"    [BATCH] 全バッチ処理完了")
        return db


    def update_memory_index(self, status_callback=None) -> str:
        """
        記憶用インデックスを更新する（過去ログ、エピソード記憶、夢日記、日記ファイル）
        """
        # [2026-02-11 FIX] 試行済みキーをリセット
        self.tried_keys.clear()
        
        def report(message):
            print(f"--- [RAG Memory] {message}")
            if status_callback: status_callback(message)

        # メモリチェック
        available_mem_mb = psutil.virtual_memory().available / (1024 * 1024)
        if available_mem_mb < 300:
            report(f"致命的な低メモリ状態です ({available_mem_mb:.1f}MB)。更新を延期します。")
            return "メモリ不足のため延期"

        report("記憶索引を更新中: 過去ログ、エピソード記憶、夢日記、日記ファイルの差分を確認...")
        
        processed_records = self._load_processed_record()
        
        # 処理対象のキュー: (record_id, document) のタプルリスト
        pending_items: List[Tuple[str, Document]] = []
        
        # 1. 過去ログ収集
        archives_dir = self.room_dir / "log_archives"
        if archives_dir.exists():
            for f in list(archives_dir.glob("*.txt")):
                record_id = f"archive:{f.name}"
                if record_id not in processed_records:
                    try:
                        content = f.read_text(encoding="utf-8")
                        if content.strip():
                            doc = Document(page_content=content, metadata={"source": f.name, "type": "log_archive", "path": str(f)})
                            pending_items.append((record_id, doc))
                    except Exception: pass

        # 2. エピソード記憶収集（月次ファイル + レガシーファイル）
        episodic_dir = self.room_dir / "memory" / "episodic"
        legacy_episodic_path = self.room_dir / "memory" / "episodic_memory.json"
        
        # エピソードファイルのリストを収集
        episodic_files = []
        if legacy_episodic_path.exists():
            episodic_files.append(legacy_episodic_path)
        if episodic_dir.exists():
            episodic_files.extend(sorted(episodic_dir.glob("*.json")))
        
        for episodic_path in episodic_files:
            try:
                with open(episodic_path, 'r', encoding='utf-8') as f:
                    episodes = json.load(f)
                if isinstance(episodes, list):
                    for ep in episodes:
                        date_str = ep.get('date', 'unknown')
                        record_id = f"episodic:{date_str}"
                        if record_id not in processed_records:
                            summary = ep.get('summary', '')
                            if summary:
                                content = f"日付: {date_str}\n内容: {summary}"
                                doc = Document(page_content=content, metadata={"source": episodic_path.name, "type": "episodic_memory", "date": date_str})
                                pending_items.append((record_id, doc))
            except Exception: pass


        # 3. 夢日記収集
        insights_path = self.room_dir / "memory" / "insights.json"
        if insights_path.exists():
            try:
                with open(insights_path, 'r', encoding='utf-8') as f:
                    insights = json.load(f)
                if isinstance(insights, list):
                    for item in insights:
                        date_str = item.get('created_at', '').split(' ')[0]
                        record_id = f"dream:{date_str}"
                        if record_id not in processed_records:
                            insight_content = item.get('insight', '')
                            strategy = item.get('strategy', '')
                            if insight_content:
                                content = f"【過去の夢・深層心理の記録 ({date_str})】\nトリガー: {item.get('trigger_topic','')}\n気づき: {insight_content}\n指針: {strategy}"
                                doc = Document(page_content=content, metadata={"source": "insights.json", "type": "dream_insight", "date": date_str})
                                pending_items.append((record_id, doc))
            except Exception: pass

        # 4. 日記ファイル収集（memory_main.txt + memory_archived_*.txt）
        diary_dir = self.room_dir / "memory"
        if diary_dir.exists():
            for f in diary_dir.glob("memory*.txt"):
                # memory_main.txt と memory_archived_*.txt が対象
                if f.name.startswith("memory") and f.name.endswith(".txt"):
                    try:
                        content = f.read_text(encoding="utf-8")
                        if content.strip():
                            # ファイル内容のハッシュでrecord_idを生成（変更検出用）
                            content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
                            record_id = f"diary:{f.name}:{content_hash}"
                            if record_id not in processed_records:
                                doc = Document(
                                    page_content=content,
                                    metadata={
                                        "source": f.name,
                                        "type": "diary",  # 日記であることを示すメタデータ
                                        "path": str(f)
                                    }
                                )
                                pending_items.append((record_id, doc))
                    except Exception as e:
                        print(f"  - 日記ファイル読み込みエラー ({f.name}): {e}")

        # [2026-02-02] ノート類収集 (research_notes, creative_notes およびアーカイブ)
        notes_dir = self.room_dir / constants.NOTES_DIR_NAME
        if notes_dir.exists():
            # 1. 最新のノート (research_notes.md, creative_notes.md)
            for filename in [constants.RESEARCH_NOTES_FILENAME, constants.CREATIVE_NOTES_FILENAME]:
                note_path = notes_dir / filename
                if note_path.exists():
                    try:
                        content = note_path.read_text(encoding="utf-8")
                        if content.strip():
                            content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
                            record_id = f"note:{filename}:{content_hash}"
                            if record_id not in processed_records:
                                doc = Document(
                                    page_content=content,
                                    metadata={
                                        "source": filename,
                                        "type": "note",
                                        "path": str(note_path)
                                    }
                                )
                                pending_items.append((record_id, doc))
                    except Exception as e:
                        print(f"  - ノート読み込みエラー ({filename}): {e}")

            # 2. アーカイブされたノート (notes/archives/*.md)
            archives_dir = notes_dir / "archives"
            if archives_dir.exists():
                for arch_f in list(archives_dir.glob("*.md")):
                    try:
                        record_id = f"note_archive:{arch_f.name}"
                        if record_id not in processed_records:
                            content = arch_f.read_text(encoding="utf-8")
                            if content.strip():
                                doc = Document(
                                    page_content=content,
                                    metadata={
                                        "source": arch_f.name,
                                        "type": "note_archive",
                                        "path": str(arch_f)
                                    }
                                )
                                pending_items.append((record_id, doc))
                    except Exception as e:
                        print(f"  - アーカイブノート読み込みエラー ({arch_f.name}): {e}")

        # 5. 現行ログ (log.txt) - 動的インデックスで処理するため、ここでは除外
        # 現行ログは頻繁に変更されるため、毎回再構築する動的インデックス側で処理する方が効率的

        # --- 実行: 小分けにして保存しながら進む ---
        if pending_items:
            total_pending = len(pending_items)
            report(f"新規追加アイテム: {total_pending}件。処理中...")
            
            static_db = self._safe_load_index(self.static_index_path)
            SAVE_INTERVAL = 5 
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=300, chunk_overlap=50)
            processed_count = 0
            
            # 途中保存用コールバック
            def interim_save(db):
                self._safe_save_index(db, self.static_index_path)
            
            for i in range(0, total_pending, SAVE_INTERVAL):
                batch_items = pending_items[i : i + SAVE_INTERVAL]
                batch_docs = [item[1] for item in batch_items]
                batch_ids = [item[0] for item in batch_items]
                
                print(f"  - グループ処理中 ({i+1}〜{min(i+SAVE_INTERVAL, total_pending)} / {total_pending})...")
                splits = text_splitter.split_documents(batch_docs)
                splits = self._filter_meaningful_chunks(splits)  # [2026-01-09] 無意味なチャンクを除外
                static_db = self._create_index_in_batches(
                    splits, 
                    existing_db=static_db,
                    save_callback=interim_save,
                    status_callback=status_callback
                )
                
                if static_db:
                    self._safe_save_index(static_db, self.static_index_path)
                    processed_records.update(batch_ids)
                    self._save_processed_record(processed_records)
                    processed_count += len(batch_items)
                else:
                    print(f"    ! グループ処理失敗。")

            result_msg = f"記憶索引: {processed_count}件を追加保存"
        else:
            result_msg = "記憶索引: 差分なし"
        
        print(f"--- [RAG Memory] 完了: {result_msg} ---")
        return result_msg

    def update_memory_index_with_progress(self):
        """
        記憶用インデックスを更新する（進捗をyieldするジェネレーター版）
        yields: (current_step, total_steps, status_message)
        """
        # [2026-02-11 FIX] 試行済みキーをリセット
        self.tried_keys.clear()
        
        yield (0, 0, "記憶索引を更新中: 差分を確認...")
        
        processed_records = self._load_processed_record()
        pending_items: List[Tuple[str, Document]] = []
        
        # 1. 過去ログ収集
        archives_dir = self.room_dir / "log_archives"
        if archives_dir.exists():
            for f in list(archives_dir.glob("*.txt")):
                record_id = f"archive:{f.name}"
                if record_id not in processed_records:
                    try:
                        content = f.read_text(encoding="utf-8")
                        if content.strip():
                            doc = Document(page_content=content, metadata={"source": f.name, "type": "log_archive", "path": str(f)})
                            pending_items.append((record_id, doc))
                    except Exception: pass

        # 2. エピソード記憶収集（月次ファイル + レガシーファイル）
        episodic_dir = self.room_dir / "memory" / "episodic"
        legacy_episodic_path = self.room_dir / "memory" / "episodic_memory.json"
        
        # エピソードファイルのリストを収集
        episodic_files = []
        if legacy_episodic_path.exists():
            episodic_files.append(legacy_episodic_path)
        if episodic_dir.exists():
            episodic_files.extend(sorted(episodic_dir.glob("*.json")))
        
        for episodic_path in episodic_files:
            try:
                with open(episodic_path, 'r', encoding='utf-8') as f:
                    episodes = json.load(f)
                if isinstance(episodes, list):
                    for ep in episodes:
                        date_str = ep.get('date', 'unknown')
                        record_id = f"episodic:{date_str}"
                        if record_id not in processed_records:
                            summary = ep.get('summary', '')
                            if summary:
                                content = f"日付: {date_str}\n内容: {summary}"
                                doc = Document(page_content=content, metadata={"source": episodic_path.name, "type": "episodic_memory", "date": date_str})
                                pending_items.append((record_id, doc))
            except Exception: pass


        # 3. 夢日記収集
        insights_path = self.room_dir / "memory" / "insights.json"
        if insights_path.exists():
            try:
                with open(insights_path, 'r', encoding='utf-8') as f:
                    insights = json.load(f)
                if isinstance(insights, list):
                    for item in insights:
                        date_str = item.get('created_at', '').split(' ')[0]
                        record_id = f"dream:{date_str}"
                        if record_id not in processed_records:
                            insight_content = item.get('insight', '')
                            strategy = item.get('strategy', '')
                            if insight_content:
                                content = f"【過去の夢・深層心理の記録 ({date_str})】\nトリガー: {item.get('trigger_topic','')}\n気づき: {insight_content}\n指針: {strategy}"
                                doc = Document(page_content=content, metadata={"source": "insights.json", "type": "dream_insight", "date": date_str})
                                pending_items.append((record_id, doc))
            except Exception: pass

        # 4. 日記ファイル収集
        diary_dir = self.room_dir / "memory"
        if diary_dir.exists():
            for f in diary_dir.glob("memory*.txt"):
                if f.name.startswith("memory") and f.name.endswith(".txt"):
                    try:
                        content = f.read_text(encoding="utf-8")
                        if content.strip():
                            content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
                            record_id = f"diary:{f.name}:{content_hash}"
                            if record_id not in processed_records:
                                doc = Document(
                                    page_content=content,
                                    metadata={"source": f.name, "type": "diary", "path": str(f)}
                                )
                                pending_items.append((record_id, doc))
                    except Exception as e:
                        print(f"  - 日記ファイル読み込みエラー ({f.name}): {e}")

        # [2026-02-02] ノート類収集 (research_notes, creative_notes およびアーカイブ)
        notes_dir = self.room_dir / constants.NOTES_DIR_NAME
        if notes_dir.exists():
            # 1. 最新のノート (research_notes.md, creative_notes.md)
            for filename in [constants.RESEARCH_NOTES_FILENAME, constants.CREATIVE_NOTES_FILENAME]:
                note_path = notes_dir / filename
                if note_path.exists():
                    try:
                        content = note_path.read_text(encoding="utf-8")
                        if content.strip():
                            content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
                            record_id = f"note:{filename}:{content_hash}"
                            if record_id not in processed_records:
                                doc = Document(
                                    page_content=content,
                                    metadata={
                                        "source": filename,
                                        "type": "note",
                                        "path": str(note_path)
                                    }
                                )
                                pending_items.append((record_id, doc))
                    except Exception as e:
                        print(f"  - ノート読み込みエラー ({filename}): {e}")

            # 2. アーカイブされたノート (notes/archives/*.md)
            archives_dir = notes_dir / "archives"
            if archives_dir.exists():
                for arch_f in list(archives_dir.glob("*.md")):
                    try:
                        record_id = f"note_archive:{arch_f.name}"
                        if record_id not in processed_records:
                            content = arch_f.read_text(encoding="utf-8")
                            if content.strip():
                                doc = Document(
                                    page_content=content,
                                    metadata={
                                        "source": arch_f.name,
                                        "type": "note_archive",
                                        "path": str(arch_f)
                                    }
                                )
                                pending_items.append((record_id, doc))
                    except Exception as e:
                        print(f"  - アーカイブノート読み込みエラー ({arch_f.name}): {e}")

        if not pending_items:
            yield (0, 0, "記憶索引: 差分なし")
            return

        total_pending = len(pending_items)
        yield (0, total_pending, f"新規追加アイテム: {total_pending}件。処理中...")
        
        static_db = self._safe_load_index(self.static_index_path)
        SAVE_INTERVAL = 5
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=300, chunk_overlap=50)
        processed_count = 0
        
        for i in range(0, total_pending, SAVE_INTERVAL):
            batch_items = pending_items[i : i + SAVE_INTERVAL]
            batch_docs = [item[1] for item in batch_items]
            batch_ids = [item[0] for item in batch_items]
            
            group_num = (i // SAVE_INTERVAL) + 1
            total_groups = (total_pending + SAVE_INTERVAL - 1) // SAVE_INTERVAL
            
            yield (group_num, total_groups, f"グループ {group_num}/{total_groups} 処理中...")
            
            splits = text_splitter.split_documents(batch_docs)
            splits = self._filter_meaningful_chunks(splits)
            
            # バッチ処理（途中保存付き）
            BATCH_SIZE = 20
            total_batches = (len(splits) + BATCH_SIZE - 1) // BATCH_SIZE
            
            for j in range(0, len(splits), BATCH_SIZE):
                batch = splits[j : j + BATCH_SIZE]
                batch_num = (j // BATCH_SIZE) + 1
                
                max_retries = 5
                attempt = 0
                while attempt < max_retries:
                    try:
                        if static_db is None:
                            static_db = FAISS.from_documents(batch, self._get_embeddings())
                        else:
                            static_db.add_documents(batch)
                        
                        if self.embedding_mode == "api":
                            time.sleep(2)
                        break
                    except Exception as e:
                        error_str = str(e)
                        print(f"      ! ベクトル化エラー (試行 {attempt+1}/{max_retries}): {e}")

                        if "429" in error_str or "ResourceExhausted" in error_str:
                            # --- [API Key Rotation] ---
                            res = self._rotate_api_key(error_str)
                            if res == "waited":
                                yield (group_num, total_groups, f"API制限(分間) - 待機して同じキーでリトライ...")
                                continue # attempt は増やさずリトライ
                            elif res == "switched":
                                yield (group_num, total_groups, "API制限(日間) - キーを切り替えてリトライ...")
                                attempt = 0 # リセットしてリトライ
                                continue
                            elif res is False:
                                # ローテーション不可で日次上限なら中断
                                is_daily = "PerDay" in error_str or "Daily" in error_str
                                if is_daily:
                                    print(f"      ! 日次上限到達（ローテーション不可）。処理を中断します。")
                                    yield (group_num, total_groups, "APIの日次上限に達したため処理を中断しました")
                                    if static_db:
                                        self._safe_save_index(static_db, self.static_index_path)
                                        # 失敗した現在の batch_ids は追加せず、これまでの成功分だけを保存
                                        self._save_processed_record(processed_records)
                                        yield (group_num, total_groups, f"⚠️ 日次上限のため途中保存 (グループ{group_num}/{total_groups})")
                                    return

                        is_retryable = any(code in error_str for code in ["429", "ResourceExhausted", "503", "504", "502", "UNAVAILABLE", "ConnectError", "name resolution"])
                        wait_time = (2 ** attempt) * 10 + (5 * (attempt + 1))
                        
                        if is_retryable and attempt < max_retries - 1:
                            print(f"      ! 待機してリトライします（{wait_time}秒）...")
                            if "429" in error_str or "ResourceExhausted" in error_str:
                                yield (group_num, total_groups, f"API制限 - {wait_time}秒待機中...")
                            elif "ConnectError" in error_str or "name resolution" in error_str:
                                yield (group_num, total_groups, f"接続エラー - {wait_time}秒待機中...")
                            else:
                                yield (group_num, total_groups, f"サーバーエラー - {wait_time}秒待機中...")
                            time.sleep(wait_time)
                            attempt += 1
                        else:
                            if attempt >= max_retries - 1:
                                print(f"      ! このバッチをスキップします。最終エラー: {e}")
                                traceback.print_exc()
                            if self.embedding_mode == "api":
                                time.sleep(5)
                            break
                
                # 20バッチごとに途中保存と進捗報告
                if static_db and batch_num % 20 == 0:
                    progress_pct = int((batch_num / total_batches) * 100)
                    yield (group_num, total_groups, f"グループ {group_num}/{total_groups}: {batch_num}/{total_batches} バッチ ({progress_pct}%)")
                    if static_db:
                        self._safe_save_index(static_db, self.static_index_path)
            
            # グループ完了時に保存
            if static_db:
                self._safe_save_index(static_db, self.static_index_path)
                # 完了したレコードのみ記録を更新
                processed_records.update(batch_ids)
                self._save_processed_record(processed_records)
                processed_count += len(batch_items)
                
                # 低頻度でGCを実行
                gc.collect()
        
        result_msg = f"記憶索引: {processed_count}件を追加保存"
        print(f"--- [RAG Memory] 完了: {result_msg} ---")
        yield (total_pending, total_pending, result_msg)

    def update_knowledge_index(self, status_callback=None) -> str:
        """
        知識用インデックスを更新する（knowledgeフォルダ内のドキュメントのみ）
        """
        # [2026-02-11 FIX] 試行済みキーをリセット
        self.tried_keys.clear()
        
        def report(message):
            print(f"--- [RAG Knowledge] {message}")
            if status_callback: status_callback(message)

        report("知識索引を再構築中...")
        dynamic_docs = []
        
        knowledge_dir = self.room_dir / "knowledge"
        if knowledge_dir.exists():
            for f in list(knowledge_dir.glob("*.txt")) + list(knowledge_dir.glob("*.md")):
                try:
                    content = f.read_text(encoding="utf-8")
                    dynamic_docs.append(Document(page_content=content, metadata={"source": f.name, "type": "knowledge"}))
                except Exception: pass

        # 知識ドキュメントのみ処理（現行ログは別ボタンで処理）
        if dynamic_docs:
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=300, chunk_overlap=50)
            dynamic_splits = text_splitter.split_documents(dynamic_docs)
            dynamic_splits = self._filter_meaningful_chunks(dynamic_splits)  # [2026-01-09] 無意味なチャンクを除外
            
            # 途中保存用コールバック
            def interim_save(db):
                self._safe_save_index(db, self.dynamic_index_path)
            
            dynamic_db = self._create_index_in_batches(
                dynamic_splits, 
                existing_db=None,
                save_callback=interim_save,
                status_callback=status_callback
            )
            
            if dynamic_db:
                self._safe_save_index(dynamic_db, self.dynamic_index_path)
                result_msg = f"知識索引: {len(dynamic_docs)}ファイルを更新"
            else:
                result_msg = "知識索引: 作成失敗"
        else:
            if self.dynamic_index_path.exists():
                shutil.rmtree(str(self.dynamic_index_path))
            result_msg = "知識索引: 対象なし"

        print(f"--- [RAG Knowledge] 完了: {result_msg} ---")
        return result_msg

    def update_current_log_index_with_progress(self):
        """
        現行ログ（log.txt）のみをインデックス化する（進捗をyieldするジェネレーター版）
        yields: (batch_num, total_batches, status_message)
        """
        # [2026-02-11 FIX] 試行済みキーをリセット
        self.tried_keys.clear()
        
        # [Modified] Use room_manager to get the correct current log path (monthly segmented)
        import room_manager
        log_file_str, _, _, _, _, _, _ = room_manager.get_room_files_paths(self.room_name)
        current_log_path = Path(log_file_str) if log_file_str else None
        
        if not current_log_path or not current_log_path.exists():
            yield (0, 0, "現行ログ: ファイルが存在しません")
            return
        
        # --- [NEW] 差分更新（チャンクハッシュ）の導入 ---
        meta_path = self.room_dir / "rag_data" / "current_log_meta.json"
        current_index_dir = self.room_dir / "rag_data" / "current_log_index"
        
        meta = {"current_file": "", "processed_hashes": []}
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception: pass
            
        # 月が変わった（ファイル名が変わった）場合はインデックスをリセット
        current_file_str = str(current_log_path.name)
        if meta.get("current_file") != current_file_str:
            meta = {"current_file": current_file_str, "processed_hashes": []}
            if current_index_dir.exists():
                shutil.rmtree(current_index_dir)
            yield (0, 0, "月が替わったため、現行インデックスをリセットして新規構築します")
            
        try:
            content = current_log_path.read_text(encoding="utf-8")
            if not content.strip():
                yield (0, 0, "現行ログ: 空のファイルです")
                return
            
            doc = Document(page_content=content, metadata={"source": current_file_str, "type": "current_log"})
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=300, chunk_overlap=50)
            all_splits = text_splitter.split_documents([doc])
            all_splits = self._filter_meaningful_chunks(all_splits)
            
            # --- 差分（未処理チャンク）の抽出 ---
            processed_hashes = set(meta.get("processed_hashes", []))
            new_splits = []
            import hashlib
            for split in all_splits:
                chunk_hash = hashlib.md5(split.page_content.encode("utf-8")).hexdigest()
                if chunk_hash not in processed_hashes:
                    # 判別用にハッシュもメタデータに入れておく
                    split.metadata["chunk_hash"] = chunk_hash
                    new_splits.append(split)
            
            if not new_splits:
                yield (0, 0, "現行ログ: 新しい会話（差分）はありません")
                return

            splits = new_splits
            # [2026-04-28] CurrentLogもバッチサイズを縮小 (20->5) し、確実性を優先
            BATCH_SIZE = 5
            total_batches = (len(splits) + BATCH_SIZE - 1) // BATCH_SIZE
            yield (0, total_batches, f"開始: 新規 {len(splits)}チャンク, {total_batches}バッチ")
            
            # 既存のインデックスがあればロードして追記モードにする
            db = None
            if current_index_dir.exists():
                db = self._safe_load_index(current_index_dir)
            
            for i in range(0, len(splits), BATCH_SIZE):
                batch = splits[i : i + BATCH_SIZE]
                batch_num = (i // BATCH_SIZE) + 1
                
                max_retries = 3
                attempt = 0
                while attempt < max_retries:
                    try:
                        if db is None:
                            db = FAISS.from_documents(batch, self._get_embeddings())
                        else:
                            db.add_documents(batch)
                        
                        yield (batch_num, total_batches, f"処理中: {batch_num}/{total_batches} バッチ完了")
                        if self.embedding_mode == "api":
                            # [2026-04-28] 待機時間を延長 (2s->5s)
                            time.sleep(5)
                        break
                    except Exception as e:
                        error_str = str(e)
                        print(f"      ! [CurrentLog] ベクトル化エラー (試行 {attempt+1}/{max_retries}): {e}")
                        if "429" in error_str or "ResourceExhausted" in error_str:
                            # --- [API Key Rotation] ---
                            res = self._rotate_api_key(error_str)
                            if res == "waited":
                                yield (batch_num, total_batches, f"API制限(分間) - 待機して同じキーでリトライ...")
                                continue # attempt は増やさずリトライ
                            elif res == "switched":
                                yield (batch_num, total_batches, "API制限(日間) - キーを切り替えてリトライします...")
                                attempt = 0 # リセット
                                continue

                            # ローテーション不可の場合
                            is_daily = "PerDay" in error_str or "Daily" in error_str
                            if is_daily:
                                print(f"      ! [CurrentLog] 日次上限到達（ローテーション不可）。処理を中断します。")
                                yield (batch_num, total_batches, "APIの日次上限に達したため処理を中断しました")
                                # 現在までの db を保存して終了
                                if db:
                                    current_log_index_path = self.room_dir / "rag_data" / "current_log_index"
                                    self._safe_save_index(db, current_log_index_path)
                                    
                                    # 今回の実行で成功したチャンクのハッシュを保存
                                    for split in splits[:i]:
                                        if "chunk_hash" in split.metadata:
                                            meta["processed_hashes"].append(split.metadata["chunk_hash"])
                                    with open(meta_path, "w", encoding="utf-8") as f:
                                        json.dump(meta, f, ensure_ascii=False)
                                        
                                    yield (total_batches, total_batches, f"⚠️ 現行ログ: 日次上限のため途中保存 ({batch_num}/{total_batches}バッチ)")
                                return

                            wait_time = 10 * (attempt + 1)
                            yield (batch_num, total_batches, f"API制限 - {wait_time}秒待機中...")
                            time.sleep(wait_time)
                        else:
                            is_network_error = any(code in error_str for code in ["ConnectError", "name resolution", "503", "504", "502", "UNAVAILABLE"])
                            if is_network_error and attempt < max_retries - 1:
                                wait_time = 15 * (attempt + 1)
                                yield (batch_num, total_batches, f"接続/サーバーエラー - {wait_time}秒待機中...")
                                print(f"      ! [CurrentLog] 接続/サーバーエラー検知。{wait_time}秒待機してリトライ...")
                                time.sleep(wait_time)
                                attempt += 1
                                continue

                            if attempt >= max_retries - 1:
                                yield (batch_num, total_batches, f"エラー: バッチ{batch_num}をスキップ")
                                print(f"      ! このバッチをスキップします。最終エラー: {e}")
                                traceback.print_exc()
                            if self.embedding_mode == "api":
                                time.sleep(5)
                            break
                        attempt += 1

                # 20バッチごとに途中保存
                if db and batch_num % 20 == 0:
                    current_log_index_path = self.room_dir / "rag_data" / "current_log_index"
                    self._safe_save_index(db, current_log_index_path)
                    gc.collect()
            
            if db:
                self._safe_save_index(db, current_index_dir)
                for split in splits:
                    if "chunk_hash" in split.metadata:
                        meta["processed_hashes"].append(split.metadata["chunk_hash"])
                        del split.metadata["chunk_hash"]
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)

                yield (total_batches, total_batches, f"✅ 現行ログ: {len(splits)}チャンクを索引化完了")
            else:
                yield (0, total_batches, "現行ログ: 索引化失敗")
                
        except Exception as e:
            traceback.print_exc()
            yield (0, 0, f"エラー: {e}")

    def create_or_update_index(self, status_callback=None) -> str:
        """
        後方互換用ラッパー: 記憶索引と知識索引の両方を更新する
        """
        memory_result = self.update_memory_index(status_callback)
        knowledge_result = self.update_knowledge_index(status_callback)
        
        final_msg = f"{memory_result} / {knowledge_result}"
        print(f"--- [RAG] 処理完了: {final_msg} ---")
        return final_msg

    def search(self, query: str, k: int = 10, score_threshold: float = 1.15, enable_intent_aware: bool = True, intent: str = None) -> List[Document]:
        """
        静的・動的インデックスの両方を検索し、複合スコアでリランキングして結果を統合する。
        
        [Phase 1.5+] Intent-Aware Retrieval対応:
        - クエリ意図を分類し、Intent別に重み付けを動的に調整
        - 高Arousal記憶は時間減衰を抑制（感情的記憶の保護）
        
        Args:
            intent: 外部から渡されたIntent（retrieval_nodeで事前分類済みの場合）。
                    指定時はLLM分類をスキップしてAPIコストを削減。
        """
        # [2026-02-11 FIX] 試行済みキーをリセット
        self.tried_keys.clear()
        
        # クエリが空の場合は即座に空リストを返す
        if not query or not str(query).strip():
            print("--- [RAG Search] クエリが空のため、検索をスキップします ---")
            return []
         
        results_with_scores = []
        
        # [Intent-Aware] クエリ意図の決定
        # 1. intentが外部から渡された場合はそれを使用（APIコスト削減）
        # 2. それ以外はLLMで分類
        if intent and intent in constants.INTENT_WEIGHTS:
            weights = constants.INTENT_WEIGHTS[intent]
            print(f"--- [RAG Search Debug] Query: '{query}' (Intent: {intent} [pre-classified], Threshold: {score_threshold}) ---")
        elif enable_intent_aware and self.api_key:
            intent_start = time.time()
            intent_info = self.classify_query_intent(query)
            print(f"--- [PERF] RAGManager.search: classify_query_intent took: {time.time() - intent_start:.4f}s ---")
            intent = intent_info["intent"]
            weights = intent_info["weights"]
            print(f"--- [RAG Search Debug] Query: '{query}' (Intent: {intent}, Threshold: {score_threshold}) ---")
        else:
            intent = constants.DEFAULT_INTENT
            weights = constants.INTENT_WEIGHTS[constants.DEFAULT_INTENT]
            print(f"--- [RAG Search Debug] Query: '{query}' (Intent: disabled, Threshold: {score_threshold}) ---")

        load_start = time.time()
        dynamic_db = self._safe_load_index(self.dynamic_index_path)
        static_db = self._safe_load_index(self.static_index_path)
        print(f"--- [PERF] RAGManager.search: safe_load_index (both) took: {time.time() - load_start:.4f}s ---")

        # [2026-02-03 Fix] 429エラー時のリトライ & ローテーションロジック
        max_retries = 5
        attempt = 0
        while attempt < max_retries:
            results_with_scores = []
            error_429_detected = False
            
            # 1. 動的インデックス検索
            if dynamic_db:
                try:
                    search_start = time.time()
                    dynamic_results = dynamic_db.similarity_search_with_score(query, k=k)
                    print(f"--- [PERF] RAGManager.search: dynamic similarity_search took: {time.time() - search_start:.4f}s ---")
                    results_with_scores.extend(dynamic_results)
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "ResourceExhausted" in err_str:
                        res = self._rotate_api_key(err_str)
                        if res == "waited":
                            print(f"  - [RAG Rotation] Dynamic search hit 429. Waited and retrying (Same key)...")
                            error_429_detected = True
                        elif res == "switched":
                            print(f"  - [RAG Rotation] Dynamic search hit 429. Rotating and retrying (New key)...")
                            error_429_detected = True
                            attempt = 0
                        else:
                            print(f"  - [RAG Warning] Dynamic search hit 429 but rotation failed: {e}")
                            attempt += 1
                    else:
                        print(f"  - [RAG Warning] Dynamic index search failed: {e}")

            if error_429_detected:
                continue

            # 2. 静的インデックス検索
            if static_db:
                try:
                    search_start = time.time()
                    static_results = static_db.similarity_search_with_score(query, k=k)
                    print(f"--- [PERF] RAGManager.search: static similarity_search took: {time.time() - search_start:.4f}s ---")
                    results_with_scores.extend(static_results)
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "ResourceExhausted" in err_str:
                        res = self._rotate_api_key(err_str)
                        if res == "waited":
                            print(f"  - [RAG Rotation] Static search hit 429. Waited and retrying (Same key)...")
                            error_429_detected = True
                        elif res == "switched":
                            print(f"  - [RAG Rotation] Static search hit 429. Rotating and retrying (New key)...")
                            error_429_detected = True
                            attempt = 0
                        else:
                            print(f"  - [RAG Warning] Static search hit 429 but rotation failed: {e}")
                            attempt += 1
                    else:
                        print(f"  - [RAG Warning] Static index search failed: {e}")
            
            if error_429_detected:
                continue
                
            # エラーなく完了したらループを抜ける
            break

        # [Intent-Aware] 3項式複合スコアリング:
        # Score = α × similarity + β × (1 - arousal) + γ × (1 - decay) × (1 - arousal)
        # - α: 類似度の重み
        # - β: Arousalの重み（高Arousal = 重要な記憶）
        # - γ: 時間減衰の重み（高Arousalで抑制）
        alpha = weights["alpha"]
        beta = weights["beta"]
        gamma = weights["gamma"]
        
        scored_results = []
        for doc, similarity_score in results_with_scores:
            arousal = doc.metadata.get("arousal", 0.5)  # デフォルト0.5（中立）
            time_decay = self.calculate_time_decay(doc.metadata)  # 0.0~1.0（新しいほど高い）
            
            # 3項式複合スコア:
            # - 類似度は低いほど良い（L2距離）
            # - Arousalは高いほど良い → (1 - arousal) で反転
            # - 時間減衰は新しいほど良い → (1 - decay) で古いほどペナルティ
            # - ただし高Arousal記憶は (1 - arousal) で減衰ペナルティを軽減
            time_penalty = (1.0 - time_decay) * (1.0 - arousal)  # Arousal高いと減衰無効化
            composite_score = alpha * similarity_score + beta * (1.0 - arousal) + gamma * time_penalty
            
            scored_results.append((doc, similarity_score, arousal, time_decay, composite_score))
        
        # 複合スコアでソート（低いほど良い）
        scored_results.sort(key=lambda x: x[4])
        
        # [2026-01-10 追加] コンテンツベースの重複除去
        seen_contents = set()
        unique_results = []
        duplicate_count = 0
        for doc, sim_score, arousal, decay, comp_score in scored_results:
            # 先頭100文字で重複判定（完全一致ではなくプレフィックス比較）
            content_key = doc.page_content[:100].strip()
            if content_key not in seen_contents:
                seen_contents.add(content_key)
                unique_results.append((doc, sim_score, arousal, decay, comp_score))
            else:
                duplicate_count += 1
        
        if duplicate_count > 0:
            print(f"  - [RAG] 重複除去: {len(scored_results)}件 → {len(unique_results)}件 ({duplicate_count}件除去)")

        filtered_docs = []
        arousal_boost_count = 0
        for doc, sim_score, arousal, decay, comp_score in unique_results:
            # 判定ロジックの緩和: 
            # 基本は sim_score <= score_threshold だが、
            # comp_score（再ランキング後の総合スコア）が十分に良い場合は救済する（0.6以下なら採用など）
            # または sim_score が閾値を僅かに超えても(閾値+0.15以内)、重要度が高ければ採用
            is_relevant = (sim_score <= score_threshold) or (comp_score <= 0.6 and sim_score <= score_threshold + 0.15)
            clean_content = doc.page_content.replace('\n', ' ')[:50]
            status_icon = "✅" if is_relevant else "❌"
            
            # Arousalが高い場合は★マーク、Decayが高い場合は🆕マーク
            markers = ""
            if arousal > 0.6:
                markers += " ★"
                arousal_boost_count += 1
            if decay > 0.9:
                markers += " 🆕"
            
            print(f"  - {status_icon} Sim: {sim_score:.3f} | Arousal: {arousal:.2f} | Decay: {decay:.2f} | Comp: {comp_score:.3f}{markers} | {clean_content}...")
            
            if is_relevant:
                filtered_docs.append(doc)
        
        if arousal_boost_count > 0:
            print(f"  - [RAG] 高Arousal記憶: {arousal_boost_count}件がブースト対象")

        return filtered_docs[:k]
    def rebuild_all_indices(self, status_callback=None) -> str:
        """
        既存のすべてのインデックスを破棄し、ゼロから再構築する。
        モデル変更時や索引が破損した時に使用。
        """
        # [2026-02-11 FIX] 試行済みキーをリセット
        self.tried_keys.clear()
        
        def report(message):
            print(f"--- [RAG Rebuild] {message}")
            if status_callback: status_callback(message)

        report("インデックスの完全再構築を開始します...")
        
        # 1. 既存のディレクトリとファイルを削除
        paths_to_delete = [
            self.static_index_path,
            self.dynamic_index_path,
            self.processed_files_record,
            self.room_dir / "rag_data" / "current_log_index",
            self.room_dir / "rag_data" / "current_log_meta.json"
        ]
        
        for p in paths_to_delete:
            if p.exists():
                try:
                    if p.is_dir():
                        shutil.rmtree(str(p))
                    else:
                        p.unlink()
                    report(f"削除完了: {p.name}")
                except Exception as e:
                    report(f"警告: {p.name} の削除に失敗: {e}")

        # キャッシュもクリア
        RAGManager._index_cache.clear()

        # 2. 再構築（通常の更新メソッドを呼ぶが、ファイルがないので全件処理になる）
        report("記憶索引の再構築を開始...")
        memory_result = self.update_memory_index(status_callback)
        
        report("知識索引の再構築を開始...")
        knowledge_result = self.update_knowledge_index(status_callback)
        
        final_msg = f"再構築完了: {memory_result} / {knowledge_result}"
        report(final_msg)
        return final_msg
