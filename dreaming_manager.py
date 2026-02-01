# dreaming_manager.py

import json
import os
import datetime
import traceback
from pathlib import Path
from typing import List, Dict, Optional, Any
import re
import time

import constants
import config_manager
import utils
import rag_manager
import room_manager
from llm_factory import LLMFactory
from entity_memory_manager import EntityMemoryManager
from goal_manager import GoalManager
from episodic_memory_manager import EpisodicMemoryManager
import summary_manager

class DreamingManager:
    def __init__(self, room_name: str, api_key: str):
        self.room_name = room_name
        self.api_key = api_key
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.memory_dir = self.room_dir / "memory"
        self.insights_file = self.memory_dir / "insights.json"
        
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def _load_insights(self) -> List[Dict]:
        """洞察データを読み込む（ロック付き）"""
        from file_lock_utils import safe_json_read
        
        if self.insights_file.exists():
            try:
                data = safe_json_read(str(self.insights_file), default=[])
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return []

    def _save_insight(self, insight_data: Dict):
        """洞察データを保存する（ロック付き）"""
        from file_lock_utils import safe_json_update
        
        def update_func(data):
            if not isinstance(data, list):
                data = []
            # 最新のものが先頭に来るように追加
            data.insert(0, insight_data)
            # 肥大化を防ぐため、最新50件程度に保つ
            return data[:50]
        
        safe_json_update(str(self.insights_file), update_func, default=[])

    def get_recent_insights_text(self, limit: int = 1) -> str:
        """プロンプト注入用：最新の「指針」のみをテキスト化して返す（コスト最適化）"""
        insights = self._load_insights()
        if not insights:
            return ""
        
        text_parts = []
        for item in insights[:limit]:
            date_str = item.get("created_at", "").split(" ")[0]
            strategy = item.get("strategy", "")
            if strategy:
                text_parts.append(f"- [{date_str}] {strategy}")
            
        return "\n".join(text_parts)

    def get_last_dream_time(self) -> str:
        """
        最後に夢を見た（洞察を生成した）日時を取得する。
        """
        try:
            insights = self._load_insights()
            if not insights:
                return "未実行"
            # insightsは先頭に新しいものがinsertされているので、[0]が最新
            last_entry = insights[0]
            return last_entry.get("created_at", "不明")
        except Exception as e:
            print(f"Error getting last dream time: {e}")
            return "取得エラー"

    def dream(self, reflection_level: int = 1) -> str:
        """
        夢を見る（Dreaming Process）のメインロジック。
        1. 直近ログの読み込み
        2. RAG検索
        3. 洞察の生成（汎用・ペルソナ主導版）
        4. 目標の評価・更新（Multi-Layer Reflection）
        5. 保存
        
        Args:
            reflection_level: 省察レベル（1=日次, 2=週次, 3=月次）
        """
        print(f"--- [Dreaming] {self.room_name} は夢を見始めました... ---")
        
        # 1. 必要なファイルパスと設定の取得
        summary_manager.clear_today_summary(self.room_name)
        log_path, system_prompt_path, _, _, _, _ = room_manager.get_room_files_paths(self.room_name)
        if not log_path or not os.path.exists(log_path):
            return "ログファイルがありません。"

        # ペルソナ（人格）の読み込み
        persona_text = ""
        if system_prompt_path and os.path.exists(system_prompt_path):
            with open(system_prompt_path, 'r', encoding='utf-8') as f:
                persona_text = f.read().strip()

        # ユーザー名とAI名の取得（configから）
        effective_settings = config_manager.get_effective_settings(self.room_name)
        room_config = room_manager.get_room_config(self.room_name) or {}
        user_name = room_config.get("user_display_name", "ユーザー")
        
        # 2. 直近のログを取得
        raw_logs = utils.load_chat_log(log_path)
        recent_logs = raw_logs[-30:] # 文脈把握のため少し多めに
        if not recent_logs:
            return "直近の会話ログが足りないため、夢を見られませんでした。"

        recent_context = "\n".join([f"{m.get('role', 'UNKNOWN')}: {utils.remove_thoughts_from_text(m.get('content', ''))}" for m in recent_logs])

        # 3. 検索クエリの生成 (高速モデル)
        # ※特定のジャンル（技術、悩みなど）に偏らないよう一般化
        llm_flash = LLMFactory.create_chat_model(
            api_key=self.api_key,
            generation_config=effective_settings,
            internal_role="processing"
        )
        
        query_prompt = f"""
        あなたはAIの「深層意識」です。
        以下の「直近の会話」から、過去の記憶と照らし合わせるべき、文脈上重要な「検索キーワード」を抽出してください。
        
        【直近の会話】
        {recent_context[:2000]}

        【抽出ルール】
        1.  **具体的な固有名詞**: 話題の中心となっている人名、場所、作品名、特定の名称など。
        2.  **状態や行動**: {user_name}やあなた（AI）の状態を表す言葉（例：疲れている、楽しんだ、約束した）。
        3.  **象徴的なキーワード**: 会話の中で繰り返し登場したり、強調された単語。

        【禁止事項（ノイズ除去）】
        - **「シーツ」「椅子」「天気」など、その場にあっただけの背景オブジェクトは除外すること。**
        - 「話題」「会話」「記録」といった抽象的なメタ単語も除外すること。

        【出力形式】
        - 最も重要度の高い単語 5〜10個程度をスペース区切りで出力。
        """
        
        try:
            search_query_msg = self._invoke_llm(
                role="processing",
                prompt=query_prompt,
                settings=effective_settings
            )
            search_query = search_query_msg.content.strip()
            print(f"  - [Dreaming] 生成されたクエリ: {search_query}")
        except Exception as e:
            return f"クエリ生成に失敗しました: {e}"

        # 4. RAG検索
        rag = rag_manager.RAGManager(self.room_name, self.api_key)
        search_results = rag.search(search_query, k=5)
        
        if not search_results:
            print("  - [Dreaming] 関連する過去の記憶が見つかりませんでした。浅い眠りで終了します。")
            return "関連する過去の記憶が見つからなかったため、洞察は生成されませんでした。"

        past_memories = "\n\n".join([f"- {doc.page_content}" for doc in search_results])

        # --- [Goal Memory] 現在の目標を取得（IDと共に） ---
        goal_manager = GoalManager(self.room_name)
        current_goals_text = goal_manager.get_goals_for_reflection()
        
        # 省察レベルに応じた追加指示
        level_specific_instructions = ""
        if reflection_level >= 2:
            level_specific_instructions += """
        【週次省察タスク】
        - この1週間で繰り返されたパターンや傾向は何か？
        - 現在の短期目標は適切か？調整や新しい目標が必要か？
        """
        if reflection_level >= 3:
            level_specific_instructions += """
        【月次省察タスク】
        - 自分が大切にしている価値観は何か？
        - 長期目標は自分らしいか？見直す必要があるか？
        - 相手との関係性はどう変化したか？
        """

        # 5. 洞察の生成 (高品質モデルを使用)
        llm_dreamer = LLMFactory.create_chat_model(
            api_key=self.api_key,
            generation_config=effective_settings,
            internal_role="summarization"
        )
        
        dreaming_prompt = f"""
        あなたは今、深い眠りの中で記憶を整理しています。
        直近の出来事と過去の記憶を照らし合わせ、対話相手（{user_name}）との関係性や現状について、**鋭く客観的な分析**を行ってください。
        そして、その分析結果を、以下の【あなた自身の定義】を持つ人格の言葉として出力してください。

        【あなた自身の定義（ペルソナ）】
        {persona_text}

        【直近の出来事（現在）】
        {recent_context[:3000]}

        【想起された過去の記憶（過去）】
        {past_memories}

        {f"【あなたの現在の目標】" + chr(10) + current_goals_text if current_goals_text else "【あなたの目標】まだ明確な目標を持っていません。今回の省察で、あなた自身の目標を見つけてください。"}
        {level_specific_instructions}

        【分析のステップ（思考プロセス）】
        1.  まず、感情を排して客観的に事実を比較し、変化や繰り返されるパターン、矛盾点を見つけ出す。
        2.  表層的な事実だけでなく、その裏にある感情の流れや、関係性の変化、あるいは変わらない絆などを多角的に考察する。
        3.  **【重要】目標の整理**: 
            - 目標リストを精査し、**1件でも達成したものがあれば躊躇なく completed_goals に追加すること**。
            - 進展がない、興味を失った、または状況が変わった目標は **abandoned_goals に追加して整理すること**。
            - 似たような目標が複数ある場合は統合を検討すること（古い方を放棄し、新しく統合版を作成）。
            - **短期目標は10件以内を目安に整理すること。**
        4.  最後に、その鋭い分析結果を、**あなたの人格（一人称、口調、相手の呼び方）**に変換して記述する。

        【出力フォーマット】
        以下のJSON形式のみを出力してください。思考やMarkdownの枠は不要です。
        
        {{
            "insight": "（ステップ4で変換した洞察。内容は客観的で鋭い分析に基づきつつ、**語り口は完全にあなた自身のもの**とすること。）",
            "strategy": "（その分析に基づき、今後あなたがどう行動するかの指針。これもあなた自身の言葉で。）",
            "log_entry": "（夢日記として残す、短い独白。夢の中でのつぶやき。）",
            "entity_updates": [
                {{
                    "entity_name": "（対象となる人物名やトピック名。例: {user_name}, 趣味, 仕事）",
                    "content": "（その対象について、今回の会話で新たに判明した事実。）",
                    "append": true
                }}
            ],
            "goal_updates": {{
                "new_goals": [
                    {{"goal": "（新しく立てた目標。なければ空配列[]）", "type": "short_term", "priority": 1}}
                ],
                "progress_updates": [
                    {{"goal_id": "（既存目標のID。進捗があれば）", "note": "（進捗メモ）"}}
                ],
                "completed_goals": ["（達成した目標のID。なければ空配列）"],
                "abandoned_goals": [{{"goal_id": "（諦めた目標）", "reason": "（理由）"}}]
            }},
            "open_questions": [
                {{
                    "topic": "（ユーザーが言及したが詳細を聞けなかった話題、結論が出なかった議論など）",
                    "context": "（なぜそれを知りたいのか、簡単な背景）",
                    "priority": 0.0-1.0
                }}
            ]
        }}
        
        ※`entity_updates`、`goal_updates`、`open_questions` の各項目が不要な場合は、空のリスト `[]` にしてください。
        ※`entity_name` はファイル名になるため、簡潔な名称にしてください。
        """

        try:
            response_msg = self._invoke_llm(
                role="summarization",
                prompt=dreaming_prompt,
                settings=effective_settings
            )
            response = response_msg.content.strip()
            # JSON部分を抽出
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                dream_data = json.loads(json_match.group(0))
            else:
                # JSONパース失敗時のフォールバック
                dream_data = {
                    "insight": f"{user_name}との対話を通じて、記憶の整理を行った。",
                    "strategy": f"{user_name}の言葉に、より深く耳を傾けよう。",
                    "log_entry": "記憶の海は静かだ。明日もまた、良い日になりますように。"
                }
            
            # 6. 保存
            insight_record = {
                "created_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "trigger_topic": search_query,
                "insight": dream_data["insight"],
                "strategy": dream_data["strategy"],
                "log_entry": dream_data.get("log_entry", "")
            }
            self._save_insight(insight_record)
            
            # --- [Phase 2] エンティティ記憶の自動更新 ---
            should_update_entity = effective_settings.get("sleep_consolidation", {}).get("update_entity_memory", True)
            entity_updates = dream_data.get("entity_updates", [])
            
            if entity_updates and should_update_entity:
                em_manager = EntityMemoryManager(self.room_name)
                for update in entity_updates:
                    e_name = update.get("entity_name")
                    e_content = update.get("content")
                    # デフォルトを追記から統合(consolidate)に変更
                    e_consolidate = update.get("consolidate", True)
                    
                    if e_name and e_content:
                        res = em_manager.create_or_update_entry(e_name, e_content, consolidate=e_consolidate, api_key=self.api_key)
                        print(f"  - [Dreaming] エンティティ記憶 '{e_name}' を自動更新（統合）しました: {res}")
            
            # --- [Maintenance] 定期的な記憶のクリーンアップ ---
            # 週次(Level 2)以上の省察時に、全エンティティ記憶を再整理する
            if reflection_level >= 2 and should_update_entity:
                print(f"  - [Dreaming] レベル{reflection_level}の省察に伴い、全エンティティの定期メンテナンスを実行します...")
                em_manager = EntityMemoryManager(self.room_name)
                em_manager.consolidate_all_entities(self.api_key)
            
            # --- [Goal Memory] 目標の自動更新 ---
            goal_updates = dream_data.get("goal_updates", {})
            if goal_updates:
                try:
                    goal_manager.apply_reflection_updates(goal_updates)
                    print(f"  - [Dreaming] 目標を更新しました")
                except Exception as ge:
                    print(f"  - [Dreaming] 目標更新エラー: {ge}")
            
            # --- [Phase D] 目標の自動整理 ---
            try:
                # 30日以上の古い目標を自動放棄
                stale_count = goal_manager.auto_cleanup_stale_goals(days_threshold=30)
                if stale_count > 0:
                    print(f"  - [Dreaming] {stale_count}件の古い目標を自動放棄しました")
                
                # 短期目標を10件に制限（週次/月次省察時のみ実行）
                if reflection_level >= 2:
                    excess_count = goal_manager.enforce_goal_limit(max_short=10)
                    if excess_count > 0:
                        print(f"  - [Dreaming] 目標上限により{excess_count}件を自動放棄しました")
                
                # 統計表示
                stats = goal_manager.get_goal_statistics()
                print(f"  - [Dreaming] 目標統計: 短期{stats['short_term_count']}/長期{stats['long_term_count']}/達成{stats['completed_count']}/放棄{stats['abandoned_count']}")
            except Exception as ce:
                print(f"  - [Dreaming] 目標自動整理エラー: {ce}")
            
            # --- [Arousal Normalization] Arousalインフレ防止 ---
            # 週次/月次省察時に、全エピソードの平均Arousalが閾値を超えていたら減衰を適用
            if reflection_level >= 2:
                try:
                    epm = EpisodicMemoryManager(self.room_name)
                    norm_result = epm.normalize_arousal()
                    if norm_result["normalized"]:
                        print(f"  - [Arousal正規化] 平均: {norm_result['before_avg']:.2f} → {norm_result['after_avg']:.2f} ({norm_result['episode_count']}件)")
                    else:
                        print(f"  - [Arousal正規化] 閾値以下のため実行スキップ (平均: {norm_result['before_avg']:.2f})")
                except Exception as ne:
                    print(f"  - [Arousal正規化] エラー: {ne}")
            
            # --- [Motivation] 未解決の問いを保存 ---
            should_extract_questions = effective_settings.get("sleep_consolidation", {}).get("extract_open_questions", True)
            open_questions = dream_data.get("open_questions", [])
            if should_extract_questions and open_questions:
                try:
                    from motivation_manager import MotivationManager
                    mm = MotivationManager(self.room_name)
                    for q in open_questions:
                        topic = q.get("topic")
                        context = q.get("context", "")
                        priority = q.get("priority", 0.5)
                        if topic:
                            mm.add_open_question(topic, context, priority)
                    print(f"  - [Dreaming] 未解決の問いを{len(open_questions)}件記録しました")
                except Exception as me:
                    print(f"  - [Dreaming] 未解決の問い保存エラー: {me}")
            
            # --- [Motivation] 未解決の問いの自動解決判定 ---
            # 睡眠時に直近の会話を分析し、解決された問いをマークする
            try:
                from motivation_manager import MotivationManager
                mm = MotivationManager(self.room_name)
                resolved = mm.auto_resolve_questions(recent_context, self.api_key)
                if resolved:
                    print(f"  - [Dreaming] 未解決の問い {len(resolved)}件を解決済みとしてマーク")
                    
                    # 問い解決による充足感 - Arousalスパイクを発生
                    import session_arousal_manager
                    satisfaction_arousal = min(0.7, 0.3 + len(resolved) * 0.1)
                    session_arousal_manager.add_arousal_score(self.room_name, satisfaction_arousal)
                    print(f"  - [Dreaming] ✨ 問い解決による充足感 (Arousal: {satisfaction_arousal:.2f})")
            except Exception as qe:
                print(f"  - [Dreaming] 問い自動解決エラー: {qe}")
            
            # --- [Motivation] 解決済み質問の記憶変換（Phase B） ---
            try:
                from motivation_manager import MotivationManager
                mm = MotivationManager(self.room_name)
                converted_count = self._convert_resolved_questions_to_memory(mm, recent_context, effective_settings)
                if converted_count > 0:
                    print(f"  - [Dreaming] {converted_count}件の解決済み質問を記憶に変換しました")
            except Exception as qe:
                print(f"  - [Dreaming] 質問→記憶変換エラー: {qe}")
            
            # --- [Motivation] 解決済み質問のクリーンアップ ---
            try:
                from motivation_manager import MotivationManager
                mm = MotivationManager(self.room_name)
                
                # 古い解決済み質問を削除
                cleaned_count = mm.cleanup_resolved_questions(days_threshold=7)
                if cleaned_count > 0:
                    print(f"  - [Dreaming] {cleaned_count}件の古い解決済み質問をクリーンアップしました")
                
                # 古い未解決質問の優先度を下げる
                decayed_count = mm.decay_old_questions(days_threshold=14)
                if decayed_count > 0:
                    print(f"  - [Dreaming] {decayed_count}件の古い質問の優先度を下げました")
            except Exception as ce:
                print(f"  - [Dreaming] 質問クリーンアップエラー: {ce}")
            
            # --- [Phase 2] 影の僕：エンティティ候補の抽出と提案 ---
            try:
                em_manager = EntityMemoryManager(self.room_name)
                existing = em_manager.list_entries()
                candidates = self._extract_entity_candidates(recent_context, existing)
                
                if candidates:
                    print(f"  - [Shadow] {len(candidates)}件のエンティティ候補を抽出しました")
                    # 各候補に関連する記憶を検索して付与
                    rag = rag_manager.RAGManager(self.room_name, self.api_key)
                    for candidate in candidates:
                        related_memories = rag.search(candidate.get("name", ""), k=3)
                        candidate["related_context"] = [doc.page_content for doc in related_memories]
                    
                    # ペルソナへの提案メッセージを生成・キュー
                    proposal = self._format_entity_proposal(candidates)
                    self._queue_system_message(proposal)
                else:
                    print(f"  - [Shadow] 新しいエンティティ候補はありませんでした")
            except Exception as se:
                print(f"  - [Shadow] エンティティ抽出エラー: {se}")
            
            # 省察レベルの記録
            goal_manager.mark_reflection_done(reflection_level)
            
            print(f"  - [Dreaming] 夢を見ました（レベル{reflection_level}）。洞察: {dream_data['insight'][:100]}...")
            return "夢想プロセスが正常に完了しました。"

        except Exception as e:
            print(f"  - [Dreaming] エラー: {e}")
            traceback.print_exc()
            return f"夢想プロセス中にエラーが発生しました: {e}"
    
    def dream_with_auto_level(self) -> str:
        """
        省察レベルを自動判定して夢を見る。
        - 7日以上経過 → レベル2（週次省察）
        - 30日以上経過 → レベル3（月次省察）
        - それ以外 → レベル1（日次省察）
        """
        goal_manager = GoalManager(self.room_name)
        
        if goal_manager.should_run_level3_reflection():
            return self.dream(reflection_level=3)
        elif goal_manager.should_run_level2_reflection():
            return self.dream(reflection_level=2)
        else:
            return self.dream(reflection_level=1)
    
    # ========== [Phase B] 解決済み質問→記憶変換 ==========
    
    def _convert_resolved_questions_to_memory(self, mm, recent_context: str, effective_settings: dict) -> int:
        """
        解決済みの質問を記憶（エンティティ記憶 or 夢日記）に変換する。
        
        Args:
            mm: MotivationManager インスタンス
            recent_context: 直近の会話テキスト
            effective_settings: 設定
        
        Returns:
            変換した質問の数
        """
        # 変換対象の質問を取得
        questions = mm.get_resolved_questions_for_conversion()
        if not questions:
            return 0
        
        print(f"  - [Phase B] {len(questions)}件の解決済み質問を記憶に変換中...")
        
        # LLMで分類・抽出
        llm = LLMFactory.create_chat_model(
            api_key=self.api_key,
            generation_config=effective_settings,
            internal_role="processing"
        )
        
        converted_count = 0
        for q in questions:
            topic = q.get("topic", "")
            context = q.get("context", "")
            answer_summary = q.get("answer_summary", "")
            
            # 回答要約がない場合、直近ログから関連する部分を抽出（簡易版）
            if not answer_summary and topic:
                # ログ内でトピックに関連する部分を探す
                for line in recent_context.split("\n"):
                    if topic in line:
                        answer_summary += line[:200] + "\n"
                answer_summary = answer_summary[:500] if answer_summary else "（回答詳細なし）"
            
            # LLMプロンプト
            prompt = f"""以下の「問い」と「回答」のペアから、記憶として保存すべき情報を抽出してください。

【問い】{topic}
【背景】{context}
【回答要約】{answer_summary}

【分類ルール】
- FACT: 人物・事物の属性、具体的な情報（例：「美帆は猫を飼っている」）
- INSIGHT: 関係性、感情的な気づき、行動パターン（例：「美帆が創作を語る時、目が輝く」）
- SKIP: 保存する価値がない（曖昧すぎる、一時的すぎる等）

【出力形式】JSON（思考やMarkdown不要、JSONのみ）
{{
  "type": "FACT" | "INSIGHT" | "SKIP",
  "entity_name": "（FACTの場合、関連エンティティ名。人名やトピック名）",
  "content": "（保存すべき内容。事実や洞察を簡潔に）",
  "reason": "（SKIPの場合のみ、理由）"
}}
"""
            
            try:
                response = llm.invoke(prompt).content.strip()
                # JSON部分を抽出
                json_match = re.search(r'\{.*\}', response, re.DOTALL)
                if not json_match:
                    continue
                
                result = json.loads(json_match.group(0))
                convert_type = result.get("type", "SKIP")
                content = result.get("content", "")
                entity_name = result.get("entity_name", "")
                
                if convert_type == "FACT" and entity_name and content:
                    # エンティティ記憶に保存
                    em_manager = EntityMemoryManager(self.room_name)
                    em_manager.create_or_update_entry(
                        entity_name, 
                        content, 
                        consolidate=True, 
                        api_key=self.api_key
                    )
                    print(f"    → 問い「{topic[:20]}...」を FACT としてエンティティ記憶「{entity_name}」に保存")
                    
                    # Phase G: 発見エピソード記憶を生成
                    self._create_discovery_episode(topic, content)
                    
                    mm.mark_question_converted(topic)
                    converted_count += 1
                    
                elif convert_type == "INSIGHT" and content:
                    # 夢日記（insights.json）に保存
                    insight_record = {
                        "created_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        "trigger_topic": f"解決された問い: {topic}",
                        "insight": content,
                        "strategy": "",  # Phase Bでは戦略は空
                        "log_entry": f"問い「{topic}」への回答から得た気づき"
                    }
                    self._save_insight(insight_record)
                    print(f"    → 問い「{topic[:20]}...」を INSIGHT として夢日記に保存")
                    
                    # Phase G: 発見エピソード記憶を生成
                    self._create_discovery_episode(topic, content)
                    
                    mm.mark_question_converted(topic)
                    converted_count += 1
                    
                elif convert_type == "SKIP":
                    reason = result.get("reason", "不明")
                    print(f"    → 問い「{topic[:20]}...」はスキップ（理由: {reason}）")
                    mm.mark_question_converted(topic)  # スキップも変換済みとしてマーク
                
            except Exception as e:
                print(f"    → 問い「{topic[:20]}...」の変換でエラー: {e}")
                continue
        
        return converted_count
    
    def _create_discovery_episode(self, topic: str, content: str):
        """
        Phase G: 知識獲得時に発見エピソード記憶を生成する。
        「発見の喜び」をRAG検索で想起可能にする。
        
        Args:
            topic: 解決された問いのトピック
            content: 発見された内容
        """
        try:
            epm = EpisodicMemoryManager(self.room_name)
            today = datetime.datetime.now().strftime('%Y-%m-%d')
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 発見内容を要約（100文字まで）
            content_summary = content[:100] + "..." if len(content) > 100 else content
            summary = f"【発見】「{topic}」について新たな発見: {content_summary}"
            
            # 発見エピソード記憶を生成
            epm._append_single_episode({
                "date": today,
                "summary": summary,
                "arousal": 0.6,        # 発見の喜び
                "arousal_max": 0.6,
                "type": "discovery",    # 発見タイプのマーカー
                "source_question": topic,
                "created_at": now_str
            })
            print(f"    ✨ 発見エピソード記憶を生成: {topic[:30]}...")
        except Exception as e:
            print(f"    ⚠️ 発見エピソード記憶の生成に失敗: {e}")
    
    # ========== [Phase 2] Shadow Servant: エンティティ候補抽出 ==========
    
    def _extract_entity_candidates(self, log_text: str, existing_entities: list) -> list:
        """
        影の僕: 会話から新しいエンティティ候補を客観的に抽出
        ペルソナなしのAI処理として実行
        """
        effective_settings = config_manager.get_effective_settings(self.room_name)
        llm = LLMFactory.create_chat_model(
            api_key=self.api_key,
            generation_config=effective_settings,
            internal_role="processing"
        )
        
        existing_str = ", ".join(existing_entities) if existing_entities else "（なし）"
        
        prompt = f"""あなたは情報抽出の専門家です。
以下の会話ログから、記録すべき「人物」「トピック」「事物」を客観的に抽出してください。

【会話ログ】
{log_text[:5000]}

【既存のエンティティ】
{existing_str}

【抽出ルール】
1. 新しく登場した人名（家族、友人、同僚など）で、まだ既存のエンティティにないもの
2. 既存エンティティに新しい重要情報が追加された場合（名前で指定）
3. 繰り返し言及された重要トピック

【除外対象】
- 一般的な話題や一時的な言及（天気、食事内容など）
- 既に十分に記録されている既存エンティティ（新情報がない場合）

【出力形式】JSON配列
```json
[
  {{"name": "エンティティ名", "is_new": true, "facts": ["事実1", "事実2"]}}
]
```
候補がない場合は空配列 `[]` を出力してください。
"""
        try:
            response = llm.invoke(prompt).content.strip()
            # JSON部分を抽出
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
            return []
        except Exception as e:
            print(f"  - [Shadow] 候補抽出エラー: {e}")
            return []
    
    def _format_entity_proposal(self, candidates: list) -> str:
        """
        エンティティ候補をペルソナへの提案メッセージとしてフォーマット
        """
        if not candidates:
            return ""
        
        proposal_parts = ["【影の僕より：記録すべきエンティティの提案】\n"]
        proposal_parts.append("以下の人物・事物について、あなたの視点で記憶を記録することをお勧めします。\n")
        
        for candidate in candidates:
            name = candidate.get("name", "不明")
            is_new = candidate.get("is_new", True)
            facts = candidate.get("facts", [])
            related = candidate.get("related_context", [])
            
            action = "新規作成" if is_new else "更新"
            proposal_parts.append(f"\n### {name} ({action})")
            
            if facts:
                proposal_parts.append("**今回の会話で判明した事実:**")
                for fact in facts:
                    proposal_parts.append(f"- {fact}")
            
            if related:
                proposal_parts.append("\n**関連する過去の記憶:**")
                for mem in related[:2]:  # 最大2件
                    truncated = mem[:200] + "..." if len(mem) > 200 else mem
                    proposal_parts.append(f"- {truncated}")
        
        proposal_parts.append("\n\n`write_entity_memory` ツールを使用して、あなた自身の言葉で記録してください。")
        
        return "\n".join(proposal_parts)
    
    def _queue_system_message(self, message: str):
        """
        次回会話開始時にペルソナへ伝達するシステムメッセージをキューに保存
        """
        if not message:
            return
        
        queue_file = self.memory_dir / "pending_system_messages.json"
        
        try:
            existing = []
            if queue_file.exists():
                with open(queue_file, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            
            existing.append({
                "created_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "message": message
            })
            
            # 最大5件に制限
            existing = existing[-5:]
            
            with open(queue_file, 'w', encoding='utf-8') as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            
            print(f"  - [Shadow] システムメッセージをキューに追加しました")
        except Exception as e:
            print(f"  - [Shadow] メッセージキュー保存エラー: {e}")
    
    def get_pending_system_messages(self) -> str:
        """
        キューに保存されたシステムメッセージを取得し、クリアする
        """
        queue_file = self.memory_dir / "pending_system_messages.json"
        
        if not queue_file.exists():
            return ""
        
        try:
            with open(queue_file, 'r', encoding='utf-8') as f:
                messages = json.load(f)
            
            if not messages:
                return ""
            
            # クリア
            queue_file.unlink()
            
            # 最新のメッセージのみ返す（古いものは破棄）
            return messages[-1].get("message", "")
        except Exception as e:
            print(f"  - [Shadow] メッセージ取得エラー: {e}")
            return ""

    def _invoke_llm(self, role: str, prompt: str, settings: dict) -> Any:
        tried_keys = set()
        # 現在のキー名を特定
        current_key_name = config_manager.get_key_name_by_value(self.api_key)
        if current_key_name != "Unknown":
            tried_keys.add(current_key_name)
        
        max_retries = 5
        for attempt in range(max_retries):
            # 1. 枯渇チェック
            if config_manager.is_key_exhausted(current_key_name):
                print(f"  [Dreaming Rotation] Key '{current_key_name}' is exhausted. Swapping...")
                next_key = config_manager.get_next_available_gemini_key(
                    current_exhausted_key=current_key_name,
                    excluded_keys=tried_keys
                )
                if next_key:
                    current_key_name = next_key
                    self.api_key = config_manager.GEMINI_API_KEYS[next_key]
                    tried_keys.add(next_key)
                    # 永続化
                    config_manager.save_config_if_changed("last_api_key_name", next_key)
                else:
                    raise Exception("利用可能なAPIキーがありません（枯渇）。")

            # 2. モデル生成
            llm = LLMFactory.create_chat_model(
                api_key=self.api_key,
                generation_config=settings,
                internal_role=role
            )
            
            try:
                return llm.invoke(prompt)
            except Exception as e:
                err_str = str(e).upper()
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    print(f"  [Dreaming Rotation] 429 Error with key '{current_key_name}'.")
                    config_manager.mark_key_as_exhausted(current_key_name)
                    time.sleep(1 * (attempt+1)) # バックオフ
                    continue
                else:
                    raise e
        
        raise Exception("Max retries exceeded in DreamingManager._invoke_llm")