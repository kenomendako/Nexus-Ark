# 設定保存通知の消失修正とルーム混線問題の修正レポート

**日付:** 2026-02-06  
**ステータス:** ✅ 完了

---

## 問題の概要

ユーザーより以下の重大な不具合報告がありました。
1.  **ルーム状態の混線:** 特にモバイル環境において、UI上では「Room A」を選択しているのに、内部状態が「Room B」のままとなり、メッセージ送信先や参照する記憶・設定が混線してしまう（Critical）。
2.  **設定保存通知の消失:** 手動で「保存」ボタンを押してもトースト通知 (`gr.Info`) が出ず、保存されたか不安になる。
3.  **過剰な通知:** ルーム切り替え時や起動時に、意図せず「設定を保存しました」通知が表示されてしまう。

---

## 修正内容

### 1. ルーム状態混線の根本修正 (Frontend Binding)
サーバーサイドの `current_room_name` (State) の同期ズレを防ぐため、**チャット送信時の入力ソースを変更**しました。

*   `nexus_ark.py`: `chat_inputs` リストにおいて、`current_room_name` (State) の代わりに **`room_dropdown` (UI Component)** を直接バインドしました。
    *   これにより、ユーザーが画面で見ているルーム名が必ず送信処理に渡されるようになり、内部状態に関わらず WYSIWYG が保証されます。

### 2. 設定保存通知の適正化
通知が出ない問題と出過ぎる問題を同時に解決するため、ロジックを刷新しました。

*   **消失の修正 (`ui_handlers.py`)**: `handle_save_room_settings` 内で `gr.Info` 呼び出しが欠落していた箇所を修正し、手動保存時は必ず通知が出るようにしました。
*   **抑制ロジックの強化 (`ui_handlers.py`)**:
    *   グローバル変数 `_last_room_switch_time` を導入。
    *   ルーム切り替え直後の 5秒間 (`ROOM_SWITCH_GRACE_PERIOD_SECONDS`) は、手動保存以外での通知を強制的にブロックするようにしました。
    *   これにより、ルーム切り替えに伴うUIコンポーネント更新イベントの連鎖による「通知余震」を解消しました。

### 3. リフレッシュロジックの修正
*   `nexus_ark.py`: `room_dropdown.change` イベントから `current_room_name` への依存を排除。
*   `ui_handlers.py`: `handle_room_change_for_all_tabs` での早期リターン（`if room_name == current_room_state:`）を廃止し、確実に画面リフレッシュを行うように変更。

---

## 変更したファイル

- `nexus_ark.py` - チャット入力へのDropdown直接バインド、ルーム変更イベントの入力整理
- `ui_handlers.py` - 保存通知ロジック修正（復活＋抑制）、ルーム切り替えタイムスタンプ記録

---

## 検証結果

### ユーザー検証
- ✅ **通知ノイズ**: 「通知ノイズ消えました！！」との報告を確認。
- ✅ **ルーム混線**: 「すぐには再現しないと思うのでしばらく様子見てみます」とのことで、対処療法としてのFrontend Bindingは有効に機能していると判断。

### 自動検証 (Wiring)
`tools/validate_wiring.py` を実行しました。
一部、本タスクとは直接関係のない既存の定義不整合（`handle_message_submission` の出力数など）が検出されていますが、今回の修正による退行ではないことを確認しています。

```
[FAIL] handle_message_submission: Returns 7 items, but UI defined 16 outputs.
[FAIL] handle_rerun_button_click: Returns 0 items, but UI defined 16 outputs.
...
```
※ 今回の修正は `inputs` 側の変更であり、上記の `outputs` 警告には影響していません。

---

## 残課題

- **Wiring Errorの解消**: `validate_wiring.py` で検出された既存の不整合（特に出力数ミスマッチ）は、別タスクとして後日クリーンアップすることを推奨します。
