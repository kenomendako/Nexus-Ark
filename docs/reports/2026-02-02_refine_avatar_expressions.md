# レポート: 表情管理システムの洗練とプロンプト同期の改善

**日付:** 2026-02-02  
**ブランチ:** `feature/refine-avatar-expressions`  
**ステータス:** ✅ 完了

---

## 問題の概要

1. アセットが存在しない廃止済みの表情タグ（`contentment`等）をAIが出力してしまう問題の修正。
2. UIの表情リストから削除・追加した内容がAIの認識（システムプロンプト）に即座に反映されない問題の修正。
3. 表情管理UIのドロップダウンにおいて、標準感情カテゴリが表示されない、または重複する問題の修正。

---

## 修正内容

### UI・管理ロジック
- **ドロップダウンの統合**: `ui_handlers.py` に `get_all_expression_choices()` ヘルパーを導入し、`idle/thinking`、標準感情、カスタム表情を重複なくリスト化するように統一。
- **管理UIの改善**: 操作ボタンをカードリストの上に配置し、アクセシビリティを向上。標準感情カテゴリの削除制限を解除（`idle/thinking`のみ固定）。
- **プロンプト同期の強化**: `room_manager.py` の `get_available_expression_files()` を修正し、アセットファイルが存在するだけでなく、UIのリスト（`expressions.json` 等）に登録されている表情のみをAIに提示するように変更。

### 内的感情ロジック（Emotion vs Expression）
- **感情カテゴリの維持**: `contentment`（満足）や `protective`（保護欲）は、表情アセットがなくても内的感情としては重要であるため、`MotivationManager` や `arousal_calculator.py`、システムプロンプトの感情定義からは削除せず、維持するように調整。

---

## 変更したファイル

- [nexus_ark.py](file:///home/baken/nexus_ark/nexus_ark.py) - 初期ロード時のドロップダウン生成ロジックとUIレイアウトの修正
- [ui_handlers.py](file:///home/baken/nexus_ark/ui_handlers.py) - 統合ドロップダウン更新ヘルパーの追加と各ハンドラの修正
- [room_manager.py](file:///home/baken/nexus_ark/room_manager.py) - 利用可能表情のフィルタリングロジック（プロンプト同期）の修正
- [agent/prompts.py](file:///home/baken/nexus_ark/agent/prompts.py) - 感情定義の整理（`contentment`/`protective`を内的感情として維持）
- [motivation_manager.py](file:///home/baken/nexus_ark/motivation_manager.py) - 感情ロジックの整合性維持
- [arousal_calculator.py](file:///home/baken/nexus_ark/arousal_calculator.py) - 感情ウェイトの整合性維持

---

## 検証結果

- [x] **表情リストの同期**: UIで表情を削除すると、システムプロンプトの「利用可能な表情」から即座に消えることを確認。
- [x] **ドロップダウンの正常化**: 重複がなくなり、標準感情も含めた全ての操作（編集・削除）が可能になった。
- [x] **内的感情の動作**: AIが `contentment` や `protective` の感情タグを末尾に正しく付与できることを確認。
- [x] **UI配線検証**: `validate_wiring.py` を実行。本件に関連する箇所（表情管理）にエラーがないことを確認（既存の別件エラーは継続）。

---

## 残課題（あれば）

- なし。表情管理と感情表現の整合性が高まりました。
