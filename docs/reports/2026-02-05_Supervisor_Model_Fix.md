# Supervisorモデル定数廃止とログ表示の適正化

## 概要
これまで `agent/graph.py` のログ出力でハードコードされた定数 `SUPERVISOR_MODEL` ("gemma-3-12b-it") が表示されていました。実動作としては共通設定（`config_manager`）に基づくモデル（デフォルト: `gemini-2.5-flash-lite`等）が使用されていましたが、ログが実態と乖離していました。
本変更により、定数を廃止し、実際にインスタンス化されたモデルの名前をログに出力するように修正しました。

## 変更点

### コード変更
- `agent/graph.py`: 
  - `SUPERVISOR_MODEL` のインポートを削除。
  - `supervisor_node` 内で、LLMインスタンス作成後にその `model_name` 属性（または `model` 属性）を取得してログに出力するように変更。
- `constants.py`: 
  - 使用されなくなった `SUPERVISOR_MODEL` 定数を削除。

### ログ出力の修正
**Before:**
```
  - Supervisor AI (gemma-3-12b-it) が次の進行を判断中...
```
(実際には gemini-2.5-flash-lite などが使われていても表示は固定)

**After:**
```
  - Supervisor AI (gemini-2.5-flash-lite) が次の進行を判断中...
```
(実際に使用されているモデル名が表示される)

## 検証結果

### 検証スクリプトによる確認
`verify_supervisor_model.py` を作成し、`LLMFactory` をモックして検証を行いました。

**実行コマンド:**
```bash
venv/bin/python verify_supervisor_model.py
```

**実行結果:**
```
--- Starting Verification ---
--- Supervisor Node 実行 ---
  - Supervisor AI (mock-gemini-2.5-flash-lite) が次の進行を判断中...
  - Supervisor生応答: {"next_speaker": "Agent A"}
  - Supervisorの決定: Agent A
--- Verification Finished Successfully ---
```

ログに `mock-gemini-2.5-flash-lite` （モックで指定した名前）が表示されており、動的に名前が取得できていることを確認しました。
