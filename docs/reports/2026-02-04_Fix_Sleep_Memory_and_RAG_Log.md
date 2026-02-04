# 睡眠時記憶整理とRAG現行ログ参照の不具合修正

**日付:** 2026-02-04  
**ブランチ:** `fix/sleep_memory_log_files`  
**ステータス:** ✅ 完了

---

## 問題の概要

1.  **睡眠時記憶整理エラー**: `episodic_memory_manager.py` において、未定義の変数 `log_files` を参照していたため `NameError` が発生し、エピソード記憶の更新が中断されていた。
2.  **RAG APIキーローテーション不全**: `rag_manager.py` のローテーション条件が、埋め込みモード `gemini` の場合に発動しない設定（`!= "api"`）になっていた。
3.  **RAG現行ログ読み込みエラー**: `rag_manager.py` が古い固定パス `log.txt` を参照し続けていたため、月次分割された現在のログを認識できず「空のファイルです」エラーが発生していた。

---

## 修正内容

### 1. エピソード記憶マネージャーの修正
- **ファイル**: `episodic_memory_manager.py`
- **内容**: ログファイルリストへの参照を削除し、読み込み済みのメッセージリストの長さをログ出力するように変更。
  ```python
  - print(f"  - 読み込み対象ファイル数: {len(log_files)}")
  + print(f"  - 読み込みメッセージ数: {len(all_messages)}")
  ```

### 2. RAGマネージャーのAPIキーローテーション条件緩和
- **ファイル**: `rag_manager.py`
- **内容**: ローテーション不可の条件を「`local` モードのみ」に限定し、`gemini` モードでもローテーションが機能するように修正。
  ```python
  - if self.embedding_mode != "api":
  + if self.embedding_mode == "local":
        return False
  ```

### 3. RAGマネージャーの現行ログパス取得ロジック修正
- **ファイル**: `rag_manager.py`
- **内容**: ハードコードされた `log.txt` を廃止し、`room_manager.get_room_files_paths` を使用して正しい月次ログパス（例: `logs/2026-02.txt`）を取得するように変更。

---

## 変更したファイル

- `episodic_memory_manager.py` - 未定義変数参照の修正
- `rag_manager.py` - APIキーローテーション条件とログパス参照の修正
- `tests/verify_fix_log_files.py` - 検証用スクリプト（新規作成）
- `tests/check_rag_manager.py` - 構文チェック用スクリプト（新規作成）

---

## 検証結果

- [x] **自動テスト**: `verify_fix_log_files.py` にて `NameError` が発生せずに `update_memory` が完了することを確認。
- [x] **構文チェック**: `check_rag_manager.py` にて修正後のファイルが正常にインポートできることを確認。
- [x] **アプリ動作確認**: ユーザー環境にて、睡眠時記憶整理が正常終了すること、およびRAGの「現行ログ」索引更新が成功すること（403チャンク索引化完了）を確認済み。

---

## 残課題

- なし
