# 📋 Nexus Ark タスクリスト

## 🏁 Release 1.0 (MVP) - 安定化・研磨

現在、v0.2.2.2 (Beta) までリリース済み。1.0に向けた最終仕上げフェーズです。

## 🔴 最優先: リリースブロッカー / バグ修正

リリース前に必ず修正すべき不具合およびユーザー体験を著しく損なう問題。

- [x] **[BUG/Improve] Gemini 3 Flash (Thinking Only) 表示改善** <!-- id: gemini-3-flash-thinking-fix --> (2026-02-16) [レポート](../reports/2026-02-16_Gemini_3_Flash_Thinking_Only_Improvement.md)
  - 詳細: テキストなしの思考のみ応答時に `(Thinking Only):` と表示される問題を、標準の `[THOUGHT]` タグへの変換に修正。
  - 優先度: 🔴高

- [x] **[Investigation] 応答速度低下の調査とパフォーマンス改善** <!-- id: performance-investigation-20260216 --> (2026-02-16) [レポート](../reports/2026-02-16_investigate-perf-slowness.md)
  - 詳細: ログ増加に伴い、ペルソナ「応答中」から数分かかるケースが報告されている。Lazy Loading導入後の現状調査とボトルネック特定・解消を行う。
  - 優先度: 🔴高

- [x] **[BUG] 通知禁止時間帯に自律行動が睡眠に入らない不具合の調査と修正** <!-- id: autonomous-sleep-fix --> (2026-02-15) [レポート](../reports/2026-02-15_Fix_Sleep_Bugs_and_Improve_Insight_Injection.md)
  - 詳細: 記憶0件の状態での夢日記生成バグ（早期リターン）や、Unpackエラー、指針の遮蔽問題を包括的に解消。
  - 優先度: 🔴高

- [x] **[BUG] チャットログ読み込みの最適化とボタン不具合修正** <!-- id: log-loading-btn-fix --> (2026-02-15) [レポート](../reports/2026-02-15_Log_Loading_Optimization_and_Fixes.md)
  - 詳細: 巨大なログ環境での読み込み低速化の解消と、Lazy Loading導入に伴う音声再生・翻訳ボタンのインデックス不整合を修正。
  - 優先度: 🔴高

- [x] **[BUG] 秘密の日記書き込みロジック見直し** <!-- id: secret-diary-fix --> (2026-02-14) [レポート](../reports/2026-02-14_fix_diary_writing_issues.md)
  - 詳細: 書き込み失敗や日付しか書き込めていないことが頻発している。
  - 優先度: 🔴高

- [x] **[Feature] チャット欄マスク機能** <!-- id: chat-masking --> (2026-02-14) [レポート](../reports/2026-02-14_Chat_Masking_Feature.md)
  - 詳細: UIを人に見せたいけどチャット欄は見せたくない時用に、チャット欄全体をダミー表示に切り替える機能。
  - 優先度: 🟡中

- [x] **[BUG] APIキーの削除ができない問題** <!-- id: api-key-delete-fix --> (2026-02-13) [レポート](../reports/2026-02-13_Fix_API_Key_Deletion.md)
  - 詳細: 「選択したキーを削除」ボタンがあるが、選択方法がなく、再入力しても削除できない。
  - 優先度: 🟡中

- [x] **[BUG] 画像生成設定のドロップダウン消失修正** <!-- id: image-gen-dropdown-fix -->
  - 詳細: Geminiプロバイダ選択時にAPIキー選択ドロップダウンが表示されない問題を修正。
  - 優先度: 🔴高

- [x] **[BUG] Playwrightブラウザ未インストール問題の自動解消** <!-- id: playwright-auto-install --> ([Report](../reports/2026-02-14_Refine_Translation_Tone_and_Fix_Playwright.md))
  - 詳細: Playwright使用時にブラウザバイナリがない場合のエラーを捕捉し、自動インストールまたは案内を行う。
  - 優先度: 🔴高

- [x] **[BUG] 思考ログ翻訳時のID重複/上書きバグ修正** <!-- id: thought-translation-fix --> ([Report](../reports/2026-02-14_Refine_Translation_Tone_and_Fix_Playwright.md))
  - 詳細: 複数の思考ログがある場合、翻訳時に内容が意図せず上書きされる問題を修正。
  - 優先度: 🔴高

- [x] **[BUG] ポモドーロタイマー・タイマーの永続化** <!-- id: timer-persistence -->
  - 詳細: 再起動でリセットされてしまうのでアラームのように続きからカウントするように修正。
  - 優先度: 🔴高
  - [完了報告](../reports/2026-02-14_Timer_Persistence.md)

- [x] **[BUG] Gemini 2.5 Pro の応答不備（空応答・途切れ）修正** <!-- id: gemini-2.5-pro-fix --> (2026-02-15) [レポート](../reports/2026-02-15_Fix_Gemini_2.5_Pro_Response.md)
  - 詳細: 2.5 Pro が推論モデルとして認識されず、パラメータが正しく設定されていない問題を修正。
  - 優先度: 🔴高

- [x] **[BUG] APIキーローテーション不具合修正** <!-- id: api-key-rotation-bug --> (2026-02-15) [レポート](../reports/2026-02-15_api_key_rotation_bug_fix.md)
  - 詳細: 有料APIキー設定時に429エラーが発生し、UIに通知されない問題を修正。名前とキーの不一致の可能性を調査。
  - 優先度: 🔴高

- [x] **[BUG] 思考ログタグの不備（閉じ忘れ等）対応** <!-- id: thought-tag-fix --> (2026-02-15) [レポート](../reports/2026-02-15_Gemini_Flash_Thought_Handling_Fix.md)
  - 詳細: [THOUGHT] タグの閉じ忘れや <thinking> タグの混入による表示崩れを防止。
  - 優先度: 🔴高

- [x] **[BUG] 内部の軽量処理モデルのAPIキーローテーション不具合** <!-- id: internal-rotation-fix --> (2026-02-17) [レポート](../reports/2026-02-17_internal_model_rotation_fix.md)
  - 詳細: `retrieval_node` 等の内部処理で 429 エラー（Quota exceeded）が発生した際、ローテーションが機能せず空応答になる問題。
  - 優先度: 🔴高

- [x] **[BUG] エピソード記憶生成エラーの修正** <!-- id: episodic-memory-attr-fix --> (2026-02-16) [レポート](../reports/2026-02-16_Fix_Episodic_Memory_Attr_Error.md)
  - 詳細: `EpisodicMemoryManager` の属性名誤記 (`_get_last_memory_date` -> `get_latest_memory_date`) および `ui_handlers.py` での `traceback` インポート、Gradio 配線不整合の修正。
  - 優先度: 🔴高

- [ ] **[Feature] ツール使用時、最低限の内容はログに残す** <!-- id: tool-logging -->
  - 詳細: 「何をやったか」が履歴に残らず、AI自身やユーザーが後から確認できない問題を修正。透明性を向上させる。例えば自律行動時に何かをWeb検索し、その後「静観」を選び応答もノートへの記録もしていない時、もう本人にもユーザーにも何もわからない。
  - 優先度: 🔴高

- [x] **[Improvement] エピソード記憶を「問い」の解決時同様中身のあるものにする** <!-- id: memory-quality --> (2026-02-16) [レポート](../reports/2026-02-16_fix-episodic-memory-generation.md)
  - 詳細: エピソード記憶のテンプレート的な記述を改善し、「経験と教訓」セクションを追加。また、生成スキップバグやUIフリーズ、重複データの最適化も実施。

## 🟡 優先: 機能改善 / UI向上

リリース推奨。ユーザー体験を向上させる改善。

- [x] **[Feature] 画像生成時の有料APIキー選択機能** <!-- id: image-gen-api-key -->
  - 詳細: ローテーションOFFで無料キー使用時でも、画像生成には有料キーを明示的に指定できるようにする。画像生成設定UIにキー選択ドロップダウンを追加。
  - 優先度: 🔴高
  - [完了報告](../reports/2026-02-13_Image_Gen_API_Key_Setting.md)

- [ ] **[Feature] 「隠す」ボタンを「会話を隠す」に変更** <!-- id: rename-hide-button -->
  - 詳細: ボタンのラベルを変更し、意図をより明確にする。
  - 優先度: 🔴高

- [ ] **[Feature] 画像アルバム機能** <!-- id: image-album -->
  - 詳細: 生成した画像や添付画像を整理・閲覧できる機能。

- [ ] **Webhook管理UIの整合性確認・修正** <!-- id: webhook-ui -->
  - 詳細: Webhook設定画面のUIやロジックが最新の仕様（通知システム等）と整合しているか確認・更新。

- [ ] **内部処理モデル設定の自由化** <!-- id: internal-model-mix -->
  - 詳細: 内部処理モデル（要約・思考等）を、メインプロバイダ以外のモデルでも自由に組み合わせられるようにする。

- [ ] **モデル不安定時の再生成挙動改善** <!-- id: regen-tool-history -->
  - 詳細: 再生成時にツール使用履歴が消え、同じツールを重複実行してしまう問題への対策（履歴を残すオプション等）。

- [ ] **外部ログインポートへのGeminiインポート機能統合** <!-- id: gemini-import-unite -->
  - 詳細: 現在「お出かけ」にあるインポート機能を、外部ログインポート機能と統合して整理する。

- [x] **[Feature] 自動更新システム (tufupベース) の実装** <!-- id: update-system --> (2026-02-15) [レポート](../reports/2026-02-15_auto_update_system.md)
  - [x] `version.json` 作成
  - [x] `update_manager.py` 実装
  - [x] `Start.bat` / `start.sh` 等の再起動ループ実装
  - [x] `nexus_ark.py` にアップデート確認UI追加
  - [x] デプロイ保護スクリプトの強化
  - [x] 運用ドキュメント作成

## 📅 Backlog (将来の課題 / Rescued from Archive)

優先度は低いが、将来的に検討すべきアイデアや改善案。

### 機能・インテグレーション
- [ ] **ChatGPT/Claude アプリからの共有URLインポート対応** (Playwright活用)
- [ ] **OpenAI公式・互換モデルにもセーフティー設定を追加**
- [ ] **Google検索を無料GeminiAPIでも使う方法検討** (Flash 2.0 Thinking等活用)
- [ ] **Ollamaをllama.cppに置き換える検討**
- [ ] **ナレッジの画像対応** (Image RAG)
- [ ] **記憶のグラフ構造化（GraphRAG）検討**

### UI / UX
- [ ] **チャット発言のコピー/編集時にタイムスタンプ除外**
- [ ] **空応答時のチャットログヘッダー統一**
- [ ] **Web巡回後の分析報告やアラーム応答などがログに追加された際のチャット欄自動更新**
- [ ] **モデルリストのお気に入り保持機能**
- [ ] **左カラムのスクロールが下まで出来ないときがある問題の修正**
- [ ] **メニューの日本語/英語切り替え対応**

### 記憶・ペルソナ
- [ ] **エンティティ記憶の想起改善** (関連エントリー名の自動提示)
- [ ] **エンティティ記憶の視点が不安定** (常体・ペルソナ視点の統一)
- [ ] **本日分のログの文字数超過分要約の調整**
- [ ] **エピソード記憶の文字数削減**
- [ ] **記憶想起の重複問題の解決**
- [ ] **アバター表情のフォールバック変更** (キーワード → 感情ベース)
- [ ] **アバター静止画複数モード対応** (待機中/思考中)
- [ ] **年次反省（Annual Reflection）の追加**
- [ ] **夢の洞察・解決した問・達成目標のペルソナ反映強化**
- [ ] **忘却機能（ガベージコレクション）の設計**

### システム・開発
- [ ] **スーパーツール化によるトークン削減**
- [ ] **自律行動クールダウンのデバッグログの頻度を下げる**
- [ ] **safe_tool_nodeのAPIキー注入ツールリストをリファクタリング**
- [ ] **アラーム時にデバッグログ（システムプロンプト）がターミナルに全部出てる問題の修正**
- [ ] **TavilyResearchの属性重複警告の解消**

### その他
- [ ] 日記の月次管理化検討
- [ ] エピソード・エンティティ記憶の編集・削除機能
- [ ] アラームの日付指定・期間指定・祝日除外
- [ ] タイマー・ポモドーロの一覧表示
- [ ] 自律行動の時間ランダム化
- [ ] エンティティ記憶の「キーワードシーディング」機能
- [ ] 自律タイマー設定＆ユーザー許可制
- [ ] ルームごとのギャラリー機能
- [ ] アバター画像・動画の表情対応
- [ ] Riveの導入検討・アバター動画編集
- [ ] 「お出かけ」中自律行動一時オフ機能
- [ ] お出かけエクスポートに最終閲覧・編集欄
- [ ] **[Improvement] 翻訳モデルを軽量処理用モデルと別で設定できるようにする** <!-- id: independent-translation-model -->
- [ ] **[Improvement] 設定の保存時にトースト通知を出すのをやめて他の方法を検討する** <!-- id: rethink-settings-notification -->
- [ ] **[BUG] 思考ログ翻訳機能と文字置き換え機能の衝突解消** <!-- id: thought-translation-replacement-conflict -->

- [x] **[BUG] 同一時刻アラーム多重登録禁止** @kenomendako (2026-02-16) <!-- id: alarm-duplicate-prevention -->
  - 詳細: 同じ時刻のアラームが重複して登録されるのを防ぐ、またはエラーとして扱うロジックの導入。
  - 優先度: 🔴高

---

## 🗄️ アーカイブ
[全タスク履歴・アーカイブはこちら](ARCHIVE_TASK_LIST.md)
