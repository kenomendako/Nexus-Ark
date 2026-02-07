# オンボーディングフロー修正レポート

**日付:** 2026-02-07  
**ブランチ:** `main`（直接作業）  
**ステータス:** ✅ 完了

---

## 問題の概要

新規ユーザー向けのオンボーディングフローが正常に動作しておらず、APIキー設定後もモーダルが再表示される問題があった。また、古いオンボーディングガイドとチャット履歴がモーダルと同時に表示される問題もあった。

---

## 修正内容

1. **オンボーディングモーダルの動的表示制御**
   - 初期状態を`visible=False`に変更し、`demo.load`で動的に表示制御
   - リロード時に一瞬モーダルが見えることを防止

2. **APIキー保存形式の修正**
   - `gemini_api_key`（単一値）ではなく`gemini_api_keys`（辞書形式）で保存するよう修正
   - `add_or_update_gemini_key()`関数を使用

3. **キー名入力フィールドの追加**
   - オンボーディング時にAPIキーの名前を設定可能に

4. **案内文の改善**
   - 新しい案内文に更新
   - `<br>`タグで改行を強制

5. **ターミナル枠のずれ修正**
   - `Start.sh`の日本語文字幅を考慮した調整

6. **onboarding_manager.py の修正**
   - `check_status()`のデフォルト戻り値を`STATUS_NEW_USER`に修正
   - `None`チェックを追加

---

## 変更したファイル

- `nexus_ark.py` - オンボーディングモーダルUI、finish_onboarding関数
- `ui_handlers.py` - handle_initial_loadにonboarding_group_update追加
- `onboarding_manager.py` - check_status()のロジック修正
- `assets/launchers/Start.sh` - ターミナル枠のずれ修正

---

## 検証結果

- [x] アプリ起動確認
- [x] オンボーディングモーダル表示確認
- [x] APIキー保存・読み込み確認  
- [x] リロード後のモーダル非表示確認
- [x] 案内文の改行表示確認

---

## 残課題

- validate_wiring.py で既存の警告あり（今回の変更とは無関係）
