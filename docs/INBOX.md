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

- [ ] [睡眠時記憶整理でエピソード記憶と現行ログ索引更新でエラー]
  - 詳細: エピソード記憶はログファイルを月毎管理に移行した影響？ログの索引は有料APIキーあるのに何故かAPIキーローテーションが出来てない。

    🌙 ルシアン: エピソード記憶を更新中...
--- [Episodic Memory] 更新処理開始: ルシアン ---
  ❌ ルシアン: エピソード記憶更新エラー - name 'log_files' is not defined
  🌙 ルシアン: 記憶索引を更新中...
[RAGManager] エンベディングモード: gemini (遅延初期化待ち)
--- [RAG Memory] 記憶索引を更新中: 過去ログ、エピソード記憶、夢日記、日記ファイルの差分を確認...
--- [RAG Memory] 新規追加アイテム: 2件。処理中...
  - グループ処理中 (1〜2 / 2)...
    [FILTER] 無意味なチャンク 18件を除外
    [BATCH] 開始: 696 チャンク, 35 バッチ (途中保存: 20バッチごと)
      ! ベクトル化エラー (試行 1/3): Error embedding content (RESOURCE_EXHAUSTED): 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. \n* Quota exceeded for metric: generativelanguage.googleapis.com/embed_content_free_tier_requests, limit: 100, model: gemini-embedding-1.0\nPlease retry in 33.384105706s.', 'status': 'RESOURCE_EXHAUSTED', 'details': [{'@type': 'type.googleapis.com/google.rpc.Help', 'links': [{'description': 'Learn more about Gemini API quotas', 'url': 'https://ai.google.dev/gemini-api/docs/rate-limits'}]}, {'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaMetric': 'generativelanguage.googleapis.com/embed_content_free_tier_requests', 'quotaId': 'EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier', 'quotaDimensions': {'model': 'gemini-embedding-1.0', 'location': 'global'}, 'quotaValue': '100'}]}, {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '33s'}]}}
      ! API制限検知（ローテーション不可）。10秒待機してリトライ...
  - 優先度: 🔴高


- [ ] [APIキーローテーションで有料キーに到着した後ずっとそれを使い続けないか確認]
  - 詳細: 有料キーは枯渇しないのでユーザー設定のキーを優先して確認
  - 優先度: 🔴高 

- [ ] [アラーム応答生成時にターミナルに全シスプロ出力されるのを廃止]
  - 詳細: 通常応答と同じようにする
  - 優先度: 🔴高 

- [ ] [「研究ノートが空です」「創作ノートが空です」というアナウンスの抑制]
  - 詳細: 
  - 優先度: 🔴高 

---

## 関連リンク

- **ステータス**: [docs/STATUS.md](STATUS.md)
- **タスクリスト**: [docs/plans/TASK_LIST.md](plans/TASK_LIST.md)
- **開発サイクル**: `.agent/workflows/dev-cycle.md`
