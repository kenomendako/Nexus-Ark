# レポート: 情景描写時の API キーローテーション不具合の修正

**日付:** 2026-02-08  
**ブランチ:** `fix/api-rotation-scenery`  
**ステータス:** ✅ 完了

---

## 問題の概要

情景描写の生成時に Gemini API の無料枠制限（429 Resource Exhausted）が発生した際、自動キーローテーションが機能せず、エラーのまま処理が停止してしまう問題を解決しました。

---

## 修正内容

1. **リトライ・ローテーションロジックの実装 (`agent/scenery_manager.py`)**  
   `generate_scenery_context` 関数内に最大3回のリトライループを追加。429エラー検知時に使用キーを枯渇マークし、次に利用可能なキー（有料キー等）を自動取得して再試行するロジックを実装しました。

2. **エラーハンドリングの改善 (`ui_handlers.py`)**  
   リトライを使い切った場合に、単なるシステムエラーではなく「API制限」である旨を具体的に表示・ログ出力するように変更しました。

---

## 変更したファイル

- `agent/scenery_manager.py` - リトライループとキーローテーションロジックの追加
- `ui_handlers.py` - エラーメッセージの具体化と例外処理の強化

---

## 検証結果

- [x] アプリ起動確認（`python tools/validate_wiring.py` にて配線確認）
- [x] 機能動作確認（モックテスト `test_api_rotation_fix.py` による自動検証）
- [x] 機能動作確認（ユーザーによる手動テストでの正常動作を確認）
- [x] 副作用チェック（既存の情景キャッシュ機能との併用で問題ないことを確認）

### 自動テストログ
```bash
$ uv run python3 test_api_rotation_fix.py
- [Scenery Rotation] Key 'default' marked as exhausted.
- [Scenery Rotation] Attempting retry 2/3 with new key: paid

✅ API Key Rotation Test Passed!
.
----------------------------------------------------------------------
Ran 1 test in 0.004s

OK
```

---

## 残課題（あれば）

なし

---
修正担当: Antigravity
