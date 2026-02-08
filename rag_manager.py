# rag_manager.py (v6: Incremental Save / Checkpoint System)

import gc
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import List, Dict, Optional, Set, Tuple
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
        # 呼び出しのたびにmanager._get_actual_embeddings()を介して最新インスタンスを取得
        return self.manager._get_actual_embeddings().embed_documents(texts)
    
    def embed_query(self, text: str) -> List[float]:
        return self.manager._get_actual_embeddings().embed_query(text)


class LangChainEmbeddingWrapper(Embeddings):
    """ローカルエンベディングをLangChainのインターフェースでラップ"""
    
    def __init__(self, local_provider):
        self.local_provider = local_provider
    
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # local_provider.embed_documents は np.ndarray を返す想定
        embeddings = self.local_provider.embed_documents(texts)
        return embeddings.tolist()
    
    def embed_query(self, text: str) -> List[float]:
        # local_provider.embed_query は np.ndarray を返す想定
        embedding = self.local_provider.embed_query(text)
        return embedding.tolist()


class RAGManager:
    # インデックスをメモリ上に保持するキャッシュ {str(path): (FAISS_db, timestamp)}
    _index_cache: Dict[str, Tuple[FAISS, float]] = {}

    def __init__(self, room_name: str, api_key: str):
        self.room_name = room_name
        self.api_key = api_key
        
        # --- [API Key Rotation Limit] ---
        # このセッションで試行したキーを記録（1周で止めるため）
        self.tried_keys = set()
        initial_key_name = config_manager.get_key_name_by_value(self.api_key)
        if initial_key_name != "Unknown":
            self.tried_keys.add(initial_key_name)
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

        # エンベディングモードを設定から取得
        effective_settings = config_manager.get_effective_settings(room_name)
        self.embedding_mode = effective_settings.get("embedding_mode", "api")
        
        # エンベディングの初期化
        self.actual_embeddings = None  # 実際のGoogle/Localインスタンス
        self.wrapper_embeddings = RotatingEmbeddings(self) # FAISS等に渡す永続ラッパー
        print(f"[RAGManager] エンベディングモード: {self.embedding_mode} (遅延初期化待ち)")

    def _get_embedding_model_id(self) -> str:
        """現在のエンベディング設定を識別するユニークなIDを返す"""
        if self.embedding_mode == "local":
            settings = config_manager.get_internal_model_settings()
            model_name = settings.get("embedding_model", "unknown-local")
            return f"local:{model_name}"
        else:
            return f"google:{constants.EMBEDDING_MODEL}"

    def _get_embeddings(self):
        """FAISS等に渡すための、キー回転に追従するラッパーを取得する"""
        return self.wrapper_embeddings

    def _get_actual_embeddings(self):
        """実際のエンベディングインスタンスを取得（必要に応じて初期化/再生成）"""
        if self.actual_embeddings is not None:
            return self.actual_embeddings
        
        if self.embedding_mode == "local":
            try:
                # 非常に重いライブラリをここで初めて呼ぶ
                from topic_cluster_manager import LocalEmbeddingProvider
                local_provider = LocalEmbeddingProvider()
                self.actual_embeddings = LangChainEmbeddingWrapper(local_provider)
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
                print(f"  - 処置: Google API (gemini-embedding-001) にフォールバックします。")
                print(f"!"*60 + "\n")
                
                from langchain_google_genai import GoogleGenerativeAIEmbeddings
                self.actual_embeddings = GoogleGenerativeAIEmbeddings(
                    model=constants.EMBEDDING_MODEL,
                    google_api_key=self.api_key,
                    task_type="retrieval_document"
                )
        else:
            # Gemini API エンベディングを使用
            from langchain_google_genai import GoogleGenerativeAIEmbeddings
            self.actual_embeddings = GoogleGenerativeAIEmbeddings(
                model=constants.EMBEDDING_MODEL,
                google_api_key=self.api_key,
                task_type="retrieval_document"
            )
            print(f"[RAGManager] Gemini API エンベディング ({self._get_embedding_model_id()}) を初期化しました (Key: {config_manager.get_key_name_by_value(self.api_key)})")
            
        return self.actual_embeddings

        
    def _rotate_api_key(self, error_str: str) -> bool:
        """
        429エラー時にAPIキーをローテーションする。
        回転に成功した場合は True, 失敗した場合は False (待機してリトライ用) を返す。
        """
        if not ("429" in error_str or "ResourceExhausted" in error_str):
            return False

        if self.embedding_mode == "local":
            return False

        # 現在のキー名を特定
        key_name = config_manager.get_key_name_by_value(self.api_key)
        if key_name != "Unknown":
            config_manager.mark_key_as_exhausted(key_name)
            print(f"      [RAG Rotation] Key '{key_name}' marked as exhausted.")
        
        # ローテーション設定確認
        effective_settings = config_manager.get_effective_settings(self.room_name)
        rotation_enabled = effective_settings.get("enable_api_key_rotation")
        if rotation_enabled is None:
            rotation_enabled = config_manager.CONFIG_GLOBAL.get("enable_api_key_rotation", True)
            
        if rotation_enabled:
            next_key_name = config_manager.get_next_available_gemini_key(
                current_exhausted_key=key_name,
                excluded_keys=self.tried_keys
            )
            if next_key_name and next_key_name not in self.tried_keys:
                next_key_val = config_manager.GEMINI_API_KEYS.get(next_key_name)
                if next_key_val:
                    print(f"      [RAG Rotation] Switching key: {key_name} -> {next_key_name}")
                    self.api_key = next_key_val
                    self.tried_keys.add(next_key_name)
                    # 永続化
                    config_manager.save_config_if_changed("last_api_key_name", next_key_name)
                    self.actual_embeddings = None # 次の _get_actual_embeddings() で新キーで再生成
                    return True
        
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
        max_retries = 3
        for attempt in range(max_retries):
            try:
                from llm_factory import LLMFactory
                
                llm = LLMFactory.create_chat_model(
                    api_key=self.api_key,
                    generation_config={},
                    internal_role="processing"
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

                response = llm.invoke(prompt.format(query=query)).content.strip().lower()
                
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
                    if self._rotate_api_key(error_str):
                        print(f"    -> キーを切り替えました。リトライ中...")
                        continue
                
                if attempt == max_retries - 1:
                    print(f"  - [Intent] 分類エラー、デフォルト使用: {e}")
                    return {
                        "intent": constants.DEFAULT_INTENT,
                        "weights": constants.INTENT_WEIGHTS[constants.DEFAULT_INTENT]
                    }
                time.sleep(2)

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

        # 1. 同じディレクトリ内に一時的なディレクトリを作成
        # これにより、パーティションを跨ぐコピー(shutil.move の低速モード)を回避し、高速な rename を保証する
        with tempfile.TemporaryDirectory(dir=str(parent_dir), prefix=".tmp_index_") as temp_dir:
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
        if not target_path or not target_path.exists():
            return None
        
        # パスを絶対パスかつ文字列として正規化（キャッシュキー用）
        target_abs_path = str(target_path.resolve())
        mtime = target_path.stat().st_mtime
        
        # キャッシュの有効性チェック
        if target_abs_path in RAGManager._index_cache:
            cache_db, cache_mtime = RAGManager._index_cache[target_abs_path]
            if cache_mtime == mtime:
                # print(f"  - [RAG] キャッシュからロード: {target_path.name}")
                return cache_db
        
        # FAISS.load_local は読み込み中もディレクトリ内のファイルを継続的に参照する場合があるため
        # 一時ディレクトリに完全にコピーしてからロードを完了させる方式が最も安全
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
                except Exception:
                    pass

            # 2. メモリ節約のため、まず一時コピーを作成
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_index_dir = Path(temp_dir) / "index"
                shutil.copytree(str(target_path), str(temp_index_dir))
                
                # 埋め込みモデルを取得（遅延初期化対応）
                embeddings = self._get_embeddings()
                
                # ロード実行
                db = FAISS.load_local(
                    str(temp_index_dir),
                    embeddings,
                    allow_dangerous_deserialization=True
                )
                
                # キャッシュに保存（この時点で db はメモリ上に展開されている）
                RAGManager._index_cache[target_abs_path] = (db, mtime)
                # print(f"  - [RAG] ロード成功 (キャッシュ更新): {target_path.name}")
                return db
                
        except Exception as e:
            print(f"  - [RAG Error] インデックス読み込み失敗 ({target_path.name}): {e}")
            # traceback.print_exc()
            return None

    def _create_index_in_batches(self, splits: List[Document], existing_db: Optional[FAISS] = None, 
                                   progress_callback=None, save_callback=None, status_callback=None) -> FAISS:
        """
        大量のドキュメントをバッチ分割し、レート制限を回避しながらインデックスを作成/追記する。
        progress_callback: 進捗を報告するコールバック関数 (batch_num, total_batches) -> None
        save_callback: 途中保存用コールバック関数 (db) -> None（定期的に呼び出される）
        status_callback: UIへ進捗メッセージを送信するコールバック関数 (message) -> None
        """
        BATCH_SIZE = 20
        SAVE_INTERVAL_BATCHES = 20  # 20バッチごと（約40秒間隔）に途中保存・進捗報告
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
            for attempt in range(max_retries):
                try:
                    if db is None:
                        db = FAISS.from_documents(batch, self._get_embeddings())
                    else:
                        db.add_documents(batch)
                    
                    # 進捗を報告
                    if progress_callback:
                        progress_callback(batch_num, total_batches)
                    
                    if self.embedding_mode == "api":
                        time.sleep(2) 
                    break 
                
                except Exception as e:
                    error_str = str(e)
                    print(f"      ! ベクトル化エラー (試行 {attempt+1}/{max_retries}): {e}")
                    if "429" in error_str or "ResourceExhausted" in error_str:
                        # --- [API Key Rotation] ---
                        if self._rotate_api_key(error_str):
                            if status_callback:
                                status_callback(f"API制限検知 - キーを切り替えてリトライします...")
                            # time.sleep(1) # 短いバックオフ
                            continue # 同じバッチ番号でリトライ (attemptはリセットされないが、next tryで新キー使用)

                        wait_time = 10 * (attempt + 1)
                        print(f"      ! API制限検知（ローテーション不可）。{wait_time}秒待機してリトライ...")
                        if status_callback:
                            status_callback(f"API制限 - {wait_time}秒待機中...")
                        time.sleep(wait_time)
                    else:
                        if attempt == max_retries - 1:
                            print(f"      ! このバッチをスキップします。最終エラー: {e}")
                            traceback.print_exc()
                        if self.embedding_mode == "api":
                            time.sleep(2)
            
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
                for attempt in range(max_retries):
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
                            if self._rotate_api_key(error_str):
                                yield (group_num, total_groups, "API制限検知 - キーを切り替えてリトライ...")
                                continue

                        is_retryable = any(code in error_str for code in ["429", "ResourceExhausted", "503", "504", "502", "UNAVAILABLE"])
                        wait_time = (2 ** attempt) * 10 + (5 * (attempt + 1))
                        
                        if is_retryable and attempt < max_retries - 1:
                            print(f"      ! 待機してリトライします（{wait_time}秒）...")
                            if "429" in error_str:
                                yield (group_num, total_groups, f"API制限 - {wait_time}秒待機中...")
                            else:
                                yield (group_num, total_groups, f"サーバーエラー - {wait_time}秒待機中...")
                            time.sleep(wait_time)
                        else:
                            if attempt == max_retries - 1:
                                print(f"      ! このバッチをスキップします。最終エラー: {e}")
                            if self.embedding_mode == "api":
                                time.sleep(2)
                
                # 20バッチごとに途中保存と進捗報告
                if batch_num % 20 == 0:
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
        # [Modified] Use room_manager to get the correct current log path (monthly segmented)
        import room_manager
        log_file_str, _, _, _, _, _ = room_manager.get_room_files_paths(self.room_name)
        current_log_path = Path(log_file_str) if log_file_str else None
        
        if not current_log_path or not current_log_path.exists():
            yield (0, 0, "現行ログ: ファイルが存在しません")
            return
        
        try:
            content = current_log_path.read_text(encoding="utf-8")
            
            if not content.strip():
                yield (0, 0, "現行ログ: 空のファイルです")
                return
            
            doc = Document(page_content=content, metadata={"source": "log.txt", "type": "current_log"})
            
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=300, chunk_overlap=50)
            splits = text_splitter.split_documents([doc])
            splits = self._filter_meaningful_chunks(splits)  # [2026-01-09] 無意味なチャンクを除外
            
            BATCH_SIZE = 20
            total_batches = (len(splits) + BATCH_SIZE - 1) // BATCH_SIZE
            
            yield (0, total_batches, f"開始: {len(splits)}チャンク, {total_batches}バッチ")
            
            db = None
            for i in range(0, len(splits), BATCH_SIZE):
                batch = splits[i : i + BATCH_SIZE]
                batch_num = (i // BATCH_SIZE) + 1
                
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        if db is None:
                            db = FAISS.from_documents(batch, self._get_embeddings())
                        else:
                            db.add_documents(batch)
                        
                        yield (batch_num, total_batches, f"処理中: {batch_num}/{total_batches} バッチ完了")
                        if self.embedding_mode == "api":
                            time.sleep(2)
                        break
                    except Exception as e:
                        error_str = str(e)
                        print(f"      ! [CurrentLog] ベクトル化エラー (試行 {attempt+1}/{max_retries}): {e}")
                        if "429" in error_str or "ResourceExhausted" in error_str:
                            # --- [API Key Rotation] ---
                            if self._rotate_api_key(error_str):
                                yield (batch_num, total_batches, "API制限検知 - キーを切り替えてリトライ...")
                                continue

                            wait_time = 10 * (attempt + 1)
                            yield (batch_num, total_batches, f"API制限 - {wait_time}秒待機中...")
                            time.sleep(wait_time)
                        else:
                            if attempt == max_retries - 1:
                                yield (batch_num, total_batches, f"エラー: バッチ{batch_num}をスキップ")
                                print(f"      ! このバッチをスキップします。最終エラー: {e}")
                                traceback.print_exc()
                            if self.embedding_mode == "api":
                                time.sleep(2)

                # 20バッチごとに途中保存
                if db and batch_num % 20 == 0:
                    current_log_index_path = self.room_dir / "rag_data" / "current_log_index"
                    self._safe_save_index(db, current_log_index_path)
                    gc.collect()
            
            if db:
                current_log_index_path = self.room_dir / "rag_data" / "current_log_index"
                self._safe_save_index(db, current_log_index_path)
                yield (total_batches, total_batches, f"✅ 現行ログ: {len(splits)}チャンクを索引化完了")
            else:
                yield (0, total_batches, "現行ログ: 索引化失敗")
                
        except Exception as e:
            import traceback
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

    def search(self, query: str, k: int = 10, score_threshold: float = 0.75, enable_intent_aware: bool = True, intent: str = None) -> List[Document]:
        """
        静的・動的インデックスの両方を検索し、複合スコアでリランキングして結果を統合する。
        
        [Phase 1.5+] Intent-Aware Retrieval対応:
        - クエリ意図を分類し、Intent別に重み付けを動的に調整
        - 高Arousal記憶は時間減衰を抑制（感情的記憶の保護）
        
        Args:
            intent: 外部から渡されたIntent（retrieval_nodeで事前分類済みの場合）。
                    指定時はLLM分類をスキップしてAPIコストを削減。
        """
        results_with_scores = []
        
        # [Intent-Aware] クエリ意図の決定
        # 1. intentが外部から渡された場合はそれを使用（APIコスト削減）
        # 2. それ以外はLLMで分類
        if intent and intent in constants.INTENT_WEIGHTS:
            weights = constants.INTENT_WEIGHTS[intent]
            print(f"--- [RAG Search Debug] Query: '{query}' (Intent: {intent} [pre-classified], Threshold: {score_threshold}) ---")
        elif enable_intent_aware and self.api_key:
            intent_info = self.classify_query_intent(query)
            intent = intent_info["intent"]
            weights = intent_info["weights"]
            print(f"--- [RAG Search Debug] Query: '{query}' (Intent: {intent}, Threshold: {score_threshold}) ---")
        else:
            intent = constants.DEFAULT_INTENT
            weights = constants.INTENT_WEIGHTS[constants.DEFAULT_INTENT]
            print(f"--- [RAG Search Debug] Query: '{query}' (Intent: disabled, Threshold: {score_threshold}) ---")

        dynamic_db = self._safe_load_index(self.dynamic_index_path)
        static_db = self._safe_load_index(self.static_index_path)

        # [2026-02-03 Fix] 429エラー時のリトライ & ローテーションロジック
        max_retries = 5
        for attempt in range(max_retries):
            results_with_scores = []
            error_429_detected = False
            
            # 1. 動的インデックス検索
            if dynamic_db:
                try:
                    dynamic_results = dynamic_db.similarity_search_with_score(query, k=k)
                    results_with_scores.extend(dynamic_results)
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "ResourceExhausted" in err_str:
                        if self._rotate_api_key(err_str):
                            print(f"  - [RAG Rotation] Dynamic search hit 429. Rotating and retrying (Attempt {attempt+1})...")
                            error_429_detected = True
                            # ローテーション成功時、埋め込みモデルが再初期化されるのでループ先頭に戻る
                        else:
                            print(f"  - [RAG Warning] Dynamic search hit 429 but rotation failed: {e}")
                    else:
                        print(f"  - [RAG Warning] Dynamic index search failed: {e}")

            if error_429_detected:
                continue

            # 2. 静的インデックス検索
            if static_db:
                try:
                    static_results = static_db.similarity_search_with_score(query, k=k)
                    results_with_scores.extend(static_results)
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "ResourceExhausted" in err_str:
                        if self._rotate_api_key(err_str):
                            print(f"  - [RAG Rotation] Static search hit 429. Rotating and retrying (Attempt {attempt+1})...")
                            error_429_detected = True
                        else:
                            print(f"  - [RAG Warning] Static search hit 429 but rotation failed: {e}")
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
            is_relevant = sim_score <= score_threshold
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
        def report(message):
            print(f"--- [RAG Rebuild] {message}")
            if status_callback: status_callback(message)

        report("インデックスの完全再構築を開始します...")
        
        # 1. 既存のディレクトリとファイルを削除
        paths_to_delete = [
            self.static_index_path,
            self.dynamic_index_path,
            self.processed_files_record,
            self.room_dir / "rag_data" / "current_log_index"
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
