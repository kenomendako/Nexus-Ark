#!/usr/bin/env python3
"""
配布用ペルソナ（オリヴェ）のRAGインデックスを構築するスクリプト

使用方法: 
  uv run tools/build_olivie_rag.py
"""
import os
import sys
from pathlib import Path

# プロジェクトルートをパスに追加
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

def build_olivie_rag():
    """配布用オリヴェのRAGインデックスを構築"""
    print("🧠 Building RAG index for Olivie (sample persona)...")
    
    # 必要なモジュールをインポート（遅延ロード）
    import config_manager
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    from langchain_community.vectorstores import FAISS
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from langchain_community.document_loaders import TextLoader
    
    # 配布用ペルソナのパス
    olivie_dir = Path(project_root) / "assets" / "sample_persona" / "Olivie"
    knowledge_dir = olivie_dir / "knowledge"
    rag_data_dir = olivie_dir / "rag_data"
    faiss_index_dir = rag_data_dir / "faiss_index"
    
    if not knowledge_dir.exists():
        print(f"❌ Knowledge directory not found: {knowledge_dir}")
        return False
    
    # 知識ファイルの確認
    knowledge_files = list(knowledge_dir.glob("*.md")) + list(knowledge_dir.glob("*.txt"))
    print(f"📚 Found {len(knowledge_files)} knowledge file(s):")
    for f in knowledge_files:
        print(f"   - {f.name} ({f.stat().st_size:,} bytes)")
    
    if not knowledge_files:
        print("❌ No knowledge files found")
        return False
    
    # APIキーの確認（config.jsonから直接読み込み）
    import json
    config_path = Path(project_root) / "config.json"
    api_key = None
    
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            gemini_keys = config.get("gemini_api_keys", {})
            for key_name, key_value in gemini_keys.items():
                if key_value and not key_value.startswith("YOUR_"):
                    api_key = key_value
                    print(f"🔑 Using API key: {key_name}")
                    break
        except Exception as e:
            print(f"⚠️ Failed to read config.json: {e}")
    
    if not api_key:
        print("❌ No valid Gemini API key found")
        return False
    
    try:
        # ドキュメントの読み込み
        print("📖 Loading documents...")
        documents = []
        for file_path in knowledge_files:
            try:
                loader = TextLoader(str(file_path), encoding='utf-8')
                documents.extend(loader.load())
            except Exception as e:
                print(f"   ⚠️ Failed to load {file_path.name}: {e}")
        
        if not documents:
            print("❌ No documents loaded")
            return False
        
        print(f"   Loaded {len(documents)} document(s)")
        
        # テキストの分割
        print("✂️ Splitting text...")
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
            separators=["\n## ", "\n### ", "\n#### ", "\n\n", "\n", " ", ""]
        )
        splits = text_splitter.split_documents(documents)
        print(f"   Created {len(splits)} chunks")
        
        # エンベディングの初期化
        print("🔮 Initializing embeddings...")
        embeddings = GoogleGenerativeAIEmbeddings(
            model="models/gemini-embedding-001",
            google_api_key=api_key
        )
        
        # FAISSインデックスの構築
        print("⚙️ Building FAISS index...")
        vectorstore = FAISS.from_documents(splits, embeddings)
        
        # インデックスの保存
        faiss_index_dir.mkdir(parents=True, exist_ok=True)
        vectorstore.save_local(str(faiss_index_dir))
        
        # 結果確認
        index_file = faiss_index_dir / "index.faiss"
        if index_file.exists():
            size = index_file.stat().st_size
            print(f"✅ Successfully built RAG index ({size:,} bytes)")
            print(f"   Location: {faiss_index_dir}")
            return True
        else:
            print("❌ Index file not created")
            return False
            
    except Exception as e:
        print(f"❌ Error building index: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = build_olivie_rag()
    sys.exit(0 if success else 1)
