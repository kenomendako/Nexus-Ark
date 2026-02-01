# 📊 プロジェクトステータス

| 2026-02-01 | **応答再生成時の添付ファイル復元**<br>・ログ内のマーカーパースによる添付ファイル復元機能を実装<br>・添付ファイル処理ロジックを共通ヘルパーへ集約し安定化 | [レポート](reports/2026-02-01_regeneration_attachment_fix.md) |
| 2026-02-01 | **APIキーローテーションの信頼性向上**<br>・枯渇状態のディスク永続化による可用性向上<br>・DreamingManagerへの自動再試行ロジック追加<br>・ローテーション後のキー名保存漏れ修正 | [レポート](reports/2026-02-01_api_key_rotation_reliability.md) |

| 2026-02-01 | **RAG モデル不整合対策と内部処理モデルの最適化**<br>・索引作成時のモデルID保存と検証ロジックを実装<br>・Llama 3.1 等の内部処理モデルの出力冗長化をプロンプトとパースで解決<br>・フォールバック発生時のチャット UI 通知機能を実装 | [レポート](reports/2026-02-01_rag_consistency_and_fallback_optimization.md) |

| 2026-01-31 | **APIキーローテーション初回選択時の枯渇チェック追加**<br>・指定キーが枯渇状態でも使用される問題を修正<br>・初回選択時に自動で代替キーへ切り替え | [レポート](reports/2026-01-31_api_key_rotation_initial_check.md) |
| 2026-01-31 | **read_project_fileツール失敗誤検知の根本修正**<br>・ファイル内容に `Exception:` 等が含まれていても誤検知しなくなった<br>・開発者ツールのエラー検知を `【エラー】` 行のみに限定 | [レポート](reports/2026-01-31_tool_error_false_detection_fix.md) |
| 2026-01-31 | **内省ツール実装**<br>・`manage_open_questions` / `manage_goals` ツール追加<br>・問い解決時にArousalスパイク + エピソード記憶生成<br>・目標達成時の記憶を意味のある形式に改善 | [レポート](reports/2026-01-31_introspection_tools.md) |
| 2026-01-31 | **画像生成マルチプロバイダ対応**<br>・Gemini/OpenAI互換/無効の3プロバイダ選択<br>・プロファイル選択方式（APIキー一元管理）<br>・gpt-image-1対応、照明指示改善 | [レポート](reports/2026-01-31_ImageGenMultiProvider.md) |
| 2026-01-30 | **問いの自動解決機能修正 + Arousalスパイク**<br>・睡眠時処理に問い自動解決判定を追加<br>・問い解決時に充足感（Arousal）スパイクを発生<br>・ツール失敗誤検知（read_project_file）を修正 | [レポート](reports/2026-01-30_question_auto_resolve_fix.md) |
| 2026-01-30 | **Moonshot AI (Kimi) 認証・パラメータ修正**<br>・401 Unauthorized (エンドポイント不整合) と 400 Bad Request (温度パラメータ制約) の完全修正<br>・動的キー注入とパラメータ強制オーバーライドの実装 | [レポート](reports/2026-01-30_Moonshot_Auth_Fix.md) |
| 2026-01-29 | **Moonshot AI (Kimi) 連携の実装**<br>・Moonshot AI (Kimi K2.5) をAPIキー設定およびプロバイダ選択肢（内部処理・ルーム個別）に追加<br>・OpenAI互換クライアントを通じて利用可能に | [レポート](reports/2026-01-29_moonshot_integration.md) |
| 2026-01-29 | **APIキーローテーション完全対応と429エラー対策**<br>・サブノードやバックグラウンドタスク（アラーム・タイマー）にもローテーションを適用<br>・`ChatGoogleGenerativeAIError`も検知対象に追加<br>・情景描写の遅延生成（Lazy Generation）によるバックグラウンド処理の堅牢化 | [レポート](reports/2026-01-29_API_Key_Rotation_Fix.md) |
| 2026-01-29 | **APIキーローテーションの1周制限とRAG安定化**<br>・利用可能な全キーを試行した後はリトライを停止し、無限ループを防止<br>・RAG現行ログ索引更新にチェックポイント（途中保存）機能を追加<br>・`RAGManager`の初期化バグ（tried_keys不足）を修正 | [レポート](reports/2026-01-29_api_key_rotation_limit_resilience.md) |
| 2026-01-28 | **Zhipu AI (GLM-4) 統合と安定化の完了**<br>・`glm-4.7-flash` 正式サポート、パラメータのホワイトリスト化による不具合解消<br>・ツール実行時のプロバイダ不整合 (404) および Error 1210 の修正<br>・ルーム設定UIへの統合と動的モデルリスト取得の安定化 | [レポート](reports/2026-01-28_Zhipu_Stability_Final_Fixes.md) |
| 2026-01-28 | **- APIキーローテーション: 🟢 正常稼働（無料キー優先ロジック実装済み）**<br>・ResourceExhaustedエラー捕捉時の自動キー切り替え<br>・グローバルおよびルーム個別の有効化設定スイッチ<br>・ユニットテストによるローテーション動作の保証 | [レポート](reports/2026-01-28_api_key_rotation.md) |
| 2026-01-27 | **APIキー・モデル名のターミナル表示**<br>・使用中のAPIキー名とモデル名をターミナルログに出力<br>・APIキーローテーション機能の利用状況を可視化 | [レポート](reports/2026-01-27_terminal_api_key_display.md) |
| 2026-01-27 | **ツール出力のログ保存最適化**<br>・高ボリューム出力ツール（12種）をアナウンスのみの保存に切り替え<br>・過去の生データによるコンテキスト圧迫を解消しAPIコストを大幅削減 | [レポート](reports/2026-01-27_tool_result_log_optimization.md) |
| 2026-01-27 | **過去の会話検索結果のヘッダー簡略化**<br>・検索結果のロールヘッダー（## AGENT:等）を簡略化<br>・LLMによる現在と過去の会話の混同を防止<br>・冗長な文字列の削除によるトークン削減 | [レポート](reports/2026-01-27_Search_Header_Simplification.md) |
| 2026-01-27 | **プロジェクト探索機能の実装とUI不整合の解消**<br>・AIによるファイルスキャン・詳細読解ツール（行範囲指定対応）を追加<br>・ルーム個別設定への統合と、UI出力同期エラーの修正<br>・ツール結果の誤検知防止など信頼性を向上 | [レポート](reports/2026-01-27_ProjectExplorerImplementation.md) |

| 2026-01-26 | **研究・分析ノートの表示順序不整合を修正**<br>・ファイル末尾（最新）のエントリが先頭に来るようにパース順序を逆転<br>・「📄 最新を表示」ボタンの挙動を修正し、ドロップダウンも最新順に並ぶよう改善 | [レポート](reports/2026-01-26_Reverse_Notes_Order_Fix.md) |
| 2026-01-26 | **RAGレジリエンス強化 & 記憶のクリーンアップ**<br>・Gemini API 503エラー対策（指数リトライ・GC）<br>・エピソード記憶の重複除去（809件→230件）とレガシーファイル引退<br>・UIの圧縮説明を実挙動（3日/4週ルール）に修正 | [レポート](reports/2026-01-26_Memory_Stability_and_UI_Optimization.md) |
| 2026-01-25 | **RAGメモリ最適化 & ログ順序不整合の修復**<br>・FAISSインデックスのキャッシュ化により検索速度を大幅向上 (0.00s)<br>・重量級ライブラリの遅延ロードによる起動時メモリ節約<br>・不整合（巻き戻り）が発生した会話ログの時系列物理ソート修復 | [レポート](reports/2026-01-25_RAG_OOM_Fix_and_Log_Recovery.md) |
| 2026-01-24 | **週次圧縮の未来日付バグ修正**<br>・`compress_old_episodes`が未来日付を含む範囲を作成する問題を修正<br>・週終了日を「カレンダー上の日曜」から「実データの最終日」に変更<br>・日次要約が「処理済み」と誤判定される問題を解消 | [レポート](reports/2026-01-24_weekly_compression_future_date_fix.md) |
| 2026-01-24 | **Phase 3c & 4: ローカルLLM対応 & フォールバック機構**<br>・llama-cpp-python によるGGUFモデルサポート<br>・Ollama廃止、配布容易性を向上<br>・プロバイダ障害時のGoogleへの自動フォールバック | [レポート](reports/2026-01-24_local_llm_fallback_phase3c_4.md) |
| 2026-01-24 | **Phase 3b: Groq 内部処理モデル対応**<br>・Groq をプロバイダとして追加<br>・APIキー管理UI と内部モデル選択肢に Groq を追加<br>・ルーム設定のAPI入力を非表示化（共通設定で一元管理） | [レポート](reports/2026-01-24_groq_internal_model_phase3b.md) |
| 2026-01-23 | **Phase 3: 内部処理モデル設定 & Zhipu AI 統合完了**<br>・Zhipu AI (GLM-4) プロバイダの統合<br>・APIキー管理UIの集約と配置改善<br>・内部モデル設定のUI連携と初期化バグ修正 | [レポート](reports/2026-01-23_zhipu_ai_integration_phase3_final.md) |
| 2026-01-23 | **Phase 2.5: 内部処理モデル動的選択**<br>・`internal_role`引数による自動モデル選択<br>・14箇所のget_configured_llm呼び出しを移行<br>・将来のOpenAI/Claude対応の基盤完成 | [レポート](reports/2026-01-23_internal_model_migration_phase2.5.md) |
| 2026-01-22 | **エピソード記憶注入バグ修正**<br>・週次記憶の重複削除、フィルタリングロジック改善<br>・ルックバック基準を「今日」に修正<br>・datetime.now()エラー・配線バグ修正 | [レポート](reports/2026-01-22_episodic_memory_injection_fix.md) |
| 2026-01-21 | **日記・ノートに「📄 最新を表示」ボタン追加**<br>・日記/創作ノート/研究ノートに最新表示ボタンを追加<br>・表情ファイルアップロードの配線バグを修正 | [レポート](reports/2026-01-21_show_latest_button.md) |
| 2026-01-21 | **ノート形式の標準化とUI不具合の修正**<br>・全ノート形式を `📝` アイコンヘッダーに統一<br>・本文内 `---` による誤分割の防止とAI生成抑制<br>・RAWエディタのスクロール対応（CSS改善） | [レポート](reports/2026-01-21_notes_ui_standardization.md) |
| 2026-01-21 | **チャット支援のログ修正で思考ログ消失問題を修正**<br>・新形式`[THOUGHT]`タグに対応<br>・`handle_log_punctuation_correction`と`handle_chatbot_edit`を拡張<br>・後方互換性維持（旧形式`【Thoughts】`も動作） | [レポート](reports/2026-01-21_thought_log_fix.md) |
| 2026-01-20 | **Gemini 3 Flash API 完全対応**<br>・LangGraphでの503/空応答問題を解決<br>・AFC無効化、レスポンス正規化、Thinking救出ロジックを実装<br>・「テキストなしThinkingのみ」のケースも救済し、沈黙を回避 | [レポート](reports/2026-01-20_Gemini_3_Flash_Debug.md) |
| 2026-01-19 | **日記・ノートUI大幅改善**<br>・創作/研究ノート/日記を「索引+詳細表示」形式に変更<br>・年・月フィルタ、RAW編集機能を追加<br>・全削除ボタン廃止でデータ安全性向上<br>・AIツールを追記専用化し書き込み安定化 | [レポート](reports/2026-01-19_notes-ui-improvement.md) |
| 2026-01-19 | **日次要約プロンプトの改善**<br>・序文禁止を明示（メタ語り抑制）<br>・文字数上限を800-1200に厳密化<br>・行数指示を削除し文字数のみに統一 | [レポート](reports/2026-01-19_daily_summary_prompt_fix.md) |
| 2026-01-18 | **トークン表示の推定精度向上と表示形式改善**<br>・履歴構築ロジック統一、コンテキスト見積もり追加<br>・ツールスキーマオーバーヘッド（約12kトークン）を推定に追加<br>・表示を「入力(推定) / 実入力 / 実合計」の3項目に変更<br>・推定精度: 10k差→3k差に改善 | [レポート](reports/2026-01-18_token_display_fix.md) |
| 2026-01-18 | **本日分ログフィルタの月別エピソード対応**<br>・`_get_effective_today_cutoff`が新形式を参照するよう修正<br>・「本日分」設定で昨日のログが表示される問題を解消 | [レポート](reports/2026-01-18_today_log_monthly_episodic.md) |
| 2026-01-18 | **階層的エピソード記憶圧縮**<br>・日次→週次→月次の3層圧縮を導入<br>・週次閾値60日→3日、月次圧縮を新規追加<br>・過去1ヶ月選択時: 約7,000文字（従来比1/6） | [レポート](reports/2026-01-18_hierarchical_episodic_compression.md) |
| 2026-01-18 | **チェス フリームーブモード完全実装**<br>・盤面同期不全と永続化バグを修正<br>・ペルソナ操作のリアルタイム反映とドラッグ操作回避制御<br>・JS-Python通信DOM可視化による基本動作修復 | [レポート](reports/2026-01-18_chess_free_move.md) |
| 2026-01-17 | **エピソード記憶の月次ファイル分割**<br>・`memory/episodic/YYYY-MM.json`形式で月ごとに分割<br>・移行スクリプト`tools/migrate_monthly_episodes.py`を追加<br>・書き込みエラー時の全データロストリスクを軽減 | [レポート](reports/2026-01-17_monthly_episodic_file_split.md) |
| 2026-01-17 | **ファイル競合対策（Race Condition防止）**<br>・filelockライブラリを導入し主要Managerに適用<br>・自律行動とユーザー対話の同時実行による破損リスクを解消 | [レポート](reports/2026-01-17_file_lock_race_condition_fix.md) |

| 2026-01-17 | **Arousal正規化プロセス（インフレ防止）**<br>・週次/月次省察時にエピソード記憶のArousalを正規化<br>・重要度のインフレを抑制し、RAG検索の検索性を維持 | [レポート](reports/2026-01-17_arousal_normalization.md) |
| 2026-01-17 | **Web巡回ツールのリスト削除エラー修正**<br>・DataFrame真偽値判定の曖昧さによるValueErrorを修正<br>・リストとDataFrameの両方の入力型に対応 | [レポート](reports/2026-01-17_watchlist_deletion_fix.md) |
| 2026-01-17 | **Arousalアノテーション付き日次要約**<br>・日次要約に各会話のArousalをアノテーション<br>・高Arousal（≥0.6）会話を★マークで詳細化<br>・週次圧縮をペルソナ視点+文字数制限に変更<br>・セッション単位の課題（コスト・口調）を解決 | [レポート](reports/2026-01-17_episodic_summary_arousal_annotation.md) |
| 2026-01-17 | **エピソード記憶の分量調整（予算緩和）**<br>・文字数予算を従来の約2倍に緩和（600/350/150文字）<br>・日次要約の記述量を5-8行へ増加<br>・各ドキュメント（仕様書、研究メモ）を最新化 | [レポート](reports/2026-01-17_episodic_memory_budget_relaxing.md) |
| 2026-01-17 | **エピソード記憶問題の修正**<br>・絆確認機能を廃止し旧データ移行問題を修正<br>・特殊タイプエピソードとの共存（重複判定）を修正<br>・UIでの複数エピソード表示、ソート、重複排除を実装 | [レポート](reports/2026-01-16_episodic_memory_fixes.md) |
| 2026-01-16 | **Intent分類APIコスト最適化**<br>・retrieval_nodeでIntent分類を統合<br>・検索あたりAPI呼び出し 2→1に削減 | [レポート](reports/2026-01-16_intent_classification_optimization.md) |
| 2026-01-16 | **Intent-Aware Retrieval**<br>・クエリ意図を5分類（emotional/factual/technical/temporal/relational）<br>・3項式複合スコアリング（Similarity + Arousal + TimeDecay）<br>・感情的質問は古い記憶も優先、技術的質問は新しい情報優先 | [レポート](reports/2026-01-16_intent_aware_retrieval.md) |
| 2026-01-15 | **Phase I: UIドライブ表示の改善**<br>・感情モニタリングをペルソナ感情に変更<br>・LinePlot→ScatterPlotで視認性向上 | [レポート](reports/2026-01-15_phase_i_ui_drive_display.md) |
| 2026-01-15 | **セッション単位エピソード記憶**<br>・日単位→セッション単位へ変更<br>・Arousal連動で詳細度調整（高: 300文字、中: 150文字、低: 50文字）<br>・MAGMA論文のSalience-Based Budgeting適用 | [レポート](reports/2026-01-15_session_based_episodic_memory.md) |
| 2026-01-15 | **Phase H: 記憶共鳴フィードバック機構**<br>・エピソード記憶にID自動付与<br>・ペルソナが`<memory_trace>`タグで共鳴度を報告<br>・共鳴度に基づきArousalを自己更新 | [レポート](reports/2026-01-15_phase_h_arousal_self_evolution.md) |
| 2026-01-15 | **Phase F: 関係性維持欲求**<br>・奉仕欲（Devotion）を廃止<br>・ペルソナ感情出力に基づくRelatedness Drive導入<br>・ユーザー感情分析LLM呼び出しを廃止（APIコスト削減）<br>・絆確認エピソード記憶の自動生成 | [レポート](reports/2026-01-15_phase_f_relatedness_drive.md) |
| 2026-01-14 | **Phase G: 発見記憶の自動生成**<br>・FACT/INSIGHT変換時に発見エピソード記憶を生成<br>・「発見の喜び」がRAG検索で想起可能に | [レポート](reports/2026-01-14_phase_g_discovery_memory.md) |
| 2026-01-14 | **Phase 1.5: Arousal複合スコアリング**<br>・RAG検索にArousalを加味したリランキング<br>・時間減衰を廃止（古い記憶も大切） | [レポート](reports/2026-01-14_phase_1.5_arousal_scoring.md) |
| 2026-01-14 | **Phase D: 目標ライフサイクル改善**<br>・省察プロンプト強化（達成/放棄を促す）<br>・30日以上の古い目標を自動放棄<br>・短期目標の上限を10件に設定 | - |
| 2026-01-14 | **Phase B: 解決済み質問→記憶変換**<br>・睡眠時整理で解決済みの問いをLLMで分析<br>・FACT→エンティティ記憶、INSIGHT→夢日記に変換 | - |
| 2026-01-14 | **Arousalベース エピソード記憶 Phase 2**<br>・Arousal永続保存（session_arousal.json）<br>・圧縮時の優先度付け（高Arousal=★） | [レポート](reports/2026-01-14_arousal_phase2.md) |
| 2026-01-13 | **Arousalベース エピソード記憶 Phase 1**<br>・内部状態スナップショット<br>・Arousalリアルタイム計算<br>・ログ出力実装 | [レポート](reports/2026-01-13_arousal_episodic_memory.md) |
| 2026-01-13 | **感情検出改善 & タイムスタンプ抑制**<br>・ペルソナ内蔵感情検出（追加APIコール削減）<br>・タイムスタンプ模倣をプロンプトで抑制<br>・感情カテゴリ統一 | [レポート](reports/2026-01-13_emotion_detection_improvement.md) |
| 2026-01-12 | **エピソード記憶改善研究**<br>・MemRL/GDPO/EILSの調査<br>・Arousalベース記憶評価の設計<br>・圧縮閾値を180日→60日に短縮 | [レポート](reports/2026-01-12_episodic_memory_research.md) |
| 2026-01-11 | **Gradio警告抑制と配線修正**<br>・起動時の大量警告を抑制<br>・主要ハンドラの戻り値不整合を修正 | [レポート](reports/2026-01-11_fix_gradio_wiring_and_warnings.md) |
| 2026-01-11 | **エンティティ記憶 v2**<br>・目次方式への移行（自動想起廃止）<br>・二層記録システム（影の僕AIによる抽出+ペルソナへの提案） | [レポート](reports/2026-01-11_entity_memory_v2.md) |
| 2026-01-11 | **サイドバーのスクロール修正とUI復旧**<br>・サイドバーのコンテンツスクロールを可能にするコンテナを追加<br>・誤ったネストによる中央カラム消失の修正 | [レポート](reports/2026-01-11_fix_sidebar_scrolling.md) |
| 2026-01-11 | **ウォッチリスト グループ化機能**<br>・複数巡回先のグループ管理・一括時刻変更機能<br>・グループ移動時の時刻自動継承 | [レポート](reports/2026-01-11_watchlist_grouping.md) |
| 2026-01-11 | **RAWログエディタの保存バグ修正**<br>・保存時の末尾改行自動付加ロジックを実装し、ログ破壊を防止 | [レポート](reports/2026-01-11_raw_log_editor_newline_fix.md) |
| 2026-01-10 | **研究・分析ノートのコンテキスト注入最適化**<br>・全文注入を廃止し、RAG索引化と「最新の見出しリスト（目次）」形式へ移行<br>・トークン消費の大幅削減（数万→数百トークン） | [レポート](reports/2026-01-10_optimize_research_notes.md) |
| 2026-01-10 | **情景画像のコスト管理と表示安定化**<br>・画像自動生成の廃止（手動更新のみ）によるAPIコスト削減<br>・昼間の画像判定ロジック改善と最終フォールバック表示の実装 | [レポート](reports/2026-01-10_auto_scenery_generation_fix.md) |
| 2026-01-10 | **検索精度の向上とエラー耐性強化**<br>・エンティティ検索のキーワード分割マッチング改善<br>・RAG検索の重複除去ロジック追加<br>・Web巡回のエラーリトライ（503時）とフォールバックの実装 | - |
| 2026-01-10 | **ログ・ノート出力の最適化**<br>・ツール実行結果のログ肥大化防止（アナウンスのみ保存）<br>・ノート編集時のタイムスタンプ重複付与バグの修正 | - |
| 2026-01-08 | **ハイブリッド検索（RAG + キーワード検索）の導入**<br>・記憶想起時に意味検索と完全一致検索を併用し、回答精度を向上 | - |

---

## 📈 1.0 リリースに向けた進捗

**ステータス:** 安定化・スリム化フェーズ（Release 1.0 MVP）
**目標:** 2026年2月中の安定版リリース

### 🎯 現在の重点課題
1. **記憶・内省の正常化**: バグ修正によるエージェントの自意識・記憶の安定化
2. **記憶の洗練**: エンティティ記憶の統合と文体統一
3. **APIコスト削減**: 要約予算の最適化
4. **配布基盤**: Pinokio/uv を前提としたモダンな配布フローの実装

---

## 📁 クイックリンク

- [タスクリスト](plans/TASK_LIST.md)
- [INBOX](INBOX.md)
- [CHANGELOG](../CHANGELOG.md)
