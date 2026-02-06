# お出かけ機能のエピソード記憶読み込み修正

## 概要
お出かけ機能（「💼 お出かけ」タブ）において、エピソード記憶の読み込みが「データなし」または「0文字」となる不具合を修正しました。
これは、エピソード記憶の保存形式が月次フォルダ（`memory/episodic/YYYY-MM.json`）に変更された一方、読み込みロジックが古い単一ファイル（`episodic_memory.json`）のみを参照していたことが原因でした。

## 変更点

### `ui_handlers.py`
- `_get_episodic_memory_entries` 関数を修正。
- 古い直接ファイル読み込みロジックを廃止。
- `EpisodicMemoryManager` クラスを使用して、全期間（レガシー＋月次）のエピソード記憶を取得するように変更。
- 取得したデータを日付でフィルタリングし、昇順でソートして整形するロジックを実装。

```python
# 変更イメージ
def _get_episodic_memory_entries(room_name: str, days: int) -> str:
    # ...
    from episodic_memory_manager import EpisodicMemoryManager
    manager = EpisodicMemoryManager(room_name)
    all_episodes = manager._load_memory()
    # ... フィルタリングとソート ...
    return formatted_text
```

## 検証結果
検証スクリプト `tools/verify_outing_fix.py` を作成し、AST（抽象構文木）抽出によるロジックテストを実施しました。
Mockデータを用いたテストにより、以下の正常動作を確認しました：

1. **データ取得**: `EpisodicMemoryManager` 経由で複数の月次データが取得されること。
2. **フィルタリング**: 指定日数（例: 30日）以内のデータのみが抽出されること。
3. **ソート**: 日付の昇順（古い順）に正しく並び替えられること。
4. **整形**: 期待される `### YYYY-MM-DD` 形式でテキスト化されること。

```text
Found dates: ['### 2026-01-15', '### 2026-02-01', '### 2026-02-05']
SUCCESS: Dates are sorted correctly.
```

## 影響範囲
- 「お出かけ」タブのエピソード記憶プレビュー・エクスポート機能のみ。
- チャットや自律行動における記憶想起には影響しません。
