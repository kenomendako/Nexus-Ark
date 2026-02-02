# 📥 タスク・メモ

思いついたタスクや気づいたバグをここにメモしてください。  
Antigravityが定期的に確認し、優先順位をつけてタスクリストに整理します。

---

## 📝 新規タスク追加（コピペ用）

```markdown
- [ ] [タスク名/問題の説明]
  - 詳細: 
  - 優先度: 🔴高 / 🟡中 / 🟢低
```

---

- [ ] [アバター画像・動画システム改善]
  - 詳細: 現在デフォルトでは「使用可能な表情: idle, happy, sad, angry, surprised, embarrassed」となっているが、これを廃し、ペルソナの感情カテゴリの感情を使用する。（docs/specifications/MEMORY_SYSTEM_SPECIFICATION.md参照）
  - `joy` - 喜び
- `contentment` - 満足・安心
- `protective` - 庇護欲・守りたい気持ち
- `anxious` - 不安
- `sadness` - 悲しみ
- `anger` - 怒り
- `neutral` - 平常
待機状態は`neutral`使用。ペルソナの応答に表情タグが無かった場合はこれらを使用。
現在の表情追加UIは使いにくすぎるので廃止。リスト（テーブル）は細長いUIに不向き。
編集後のルーム固有表情一覧はシステムプロンプトに埋め込みペルソナに応答時に出力するよう指示。
「検出キーワード」も廃止したい。表情タグが無い場合は感情カテゴリで代用。それも無ければ待機表情で。
  - 優先度: 🔴高

---

---


## 関連リンク

- **ステータス**: [docs/STATUS.md](STATUS.md)
- **タスクリスト**: [docs/plans/TASK_LIST.md](plans/TASK_LIST.md)
- **開発サイクル**: `.agent/workflows/dev-cycle.md`
