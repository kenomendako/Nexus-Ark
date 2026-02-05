---
name: development-rules
description: Nexus Ark開発の鉄則。全エージェントが作業前に必ず確認すべきルール。
trigger: always_on
---

# 開発ルール - 鉄の掟

## 1. 言語
**すべて日本語**で記述（思考、タスク名、コミットメッセージ含む）

## 2. 対話優先
相談には**まず回答・提案**。明示的な指示があるまで実装しない。

## 3. Git規約
- `main`直接コミット禁止
- ブランチ: `[feat|fix|improve|docs]/[task-name]`
- コミット: `[prefix]: 日本語説明`

## 4. UI変更時の必須事項
- 変更後: `python tools/validate_wiring.py` 実行
- 責務分離: `nexus_ark.py`=レイアウト / `ui_handlers.py`=ロジック

---

## 必読ドキュメント

コード変更前に必ず確認：

1. **SDK作法・モデル構成**: `docs/guides/AI_DEVELOPMENT_GUIDELINES.md`
2. **Gradioの罠**: `docs/guides/gradio_notes.md`
3. **UI実装パターン**: `docs/guides/UI_IMPLEMENTATION_PATTERNS.md`

詳細は `/coding-style` スキルを参照。
