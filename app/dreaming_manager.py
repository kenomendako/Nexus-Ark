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
        self.dreaming_dir = self.memory_dir / "dreaming"  # [NEW] 専用フォルダ
        self.legacy_insights_file = self.memory_dir / "insights.json"
        
        # ディレクトリの保証
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.dreaming_dir.mkdir(parents=True, exist_ok=True)

    def _get_monthly_file_path(self, date_str: str) -> Path:
        """
        日付文字列から対応する月次ファイルのパスを返す。
        例: "2026-02-03 11:43:08" -> memory/dreaming/2026-02.json
        """
        try:
            # YYYY-MM形式を抽出
            match = re.match(r'^(\d{4}-\d{2})', date_str.strip())
            if match:
                month_str = match.group(1)
                return self.dreaming_dir / f"{month_str}.json"
        except Exception:
            pass
        
        # パース失敗時は最新の月、または現在時刻
        month_str = datetime.datetime.now().strftime("%Y-%m")
        return self.dreaming_dir / f"{month_str}.json"

    def _load_insights(self) -> List[Dict]:
        """
        全ての月次ファイル + レガシーファイルから洞察データを読み込む（ロック付き）。
        """
        from file_lock_utils import safe_json_read
        
        # 先に移行が必要かチェック
        self._migrate_legacy_insights()
        
        all_insights = []
        
        # 月次ファイル（dreaming/*.json）を読み込み
        if self.dreaming_dir.exists():
            # 降順（新しい月が先）で読み込む
            for monthly_file in sorted(self.dreaming_dir.glob("*.json"), reverse=True):
                try:
                    data = safe_json_read(str(monthly_file), default=[])
                    if isinstance(data, list):
                        all_insights.extend(data)
                except Exception as e:
                    print(f"⚠️ [DreamingManager] {monthly_file.name} の読み込みに失敗: {e}")
                    utils.backup_and_repair_json(monthly_file, [])
        
        return all_insights

    def _migrate_legacy_insights(self):
        """
        既存の insights.json を月次ファイルに振り分けて移行する。
        """
        from file_lock_utils import safe_json_read, safe_json_update
        
        if not self.legacy_insights_file.exists():
            return
            
        print(f"  - [Dreaming Migration] {self.legacy_insights_file.name} を月次分割に移行中...")
        
        try:
            legacy_data = safe_json_read(str(self.legacy_insights_file), default=[])
            if not isinstance(legacy_data, list) or not legacy_data:
                self.legacy_insights_file.unlink()
                return

            # 日付（月）ごとにグループ化
            groups = {}
            for item in legacy_data:
                date_str = item.get("created_at", "")
                path = self._get_monthly_file_path(date_str)
                if path not in groups:
                    groups[path] = []
                groups[path].append(item)
            
            # 各ファイルに保存
            for path, items in groups.items():
                def update_func(existing_data):
                    if not isinstance(existing_data, list):
                        existing_data = []
                    # 既存データと統合（重複はcreated_at等で簡易チェック可能だが、移行時は全統合）
                    # 重複排除が必要ならここで行う
                    existing_ids = {f"{i.get('created_at')}_{i.get('trigger_topic')[:20]}" for i in existing_data}
                    for item in items:
                        item_id = f"{item.get('created_at')}_{item.get('trigger_topic', '')[:20]}"
                        if item_id not in existing_ids:
                            existing_data.append(item)
                    
                    # 日付降順でソート
                    existing_data.sort(key=lambda x: x.get("created_at", ""), reverse=True)
                    return existing_data
                
                safe_json_update(str(path), update_func, default=[])

            # 移行完了後に削除（またはリネーム）
            backup_path = self.legacy_insights_file.with_suffix(".json.migrated")
            self.legacy_insights_file.replace(backup_path)
            print(f"  - [Dreaming Migration] 移行完了: {backup_path}")
            
        except Exception as e:
            print(f"  - [Dreaming Migration] Error: {e}")
            traceback.print_exc()

    def _save_insight(self, insight_data: Dict):
        """洞察データを月次ファイルに保存する（ロック付き）"""
        from file_lock_utils import safe_json_update
        
        date_str = insight_data.get("created_at", "")
        monthly_file = self._get_monthly_file_path(date_str)
        
        def update_func(data):
            if not isinstance(data, list):
                data = []
            # 最新のものが先頭に来るように追加
            data.insert(0, insight_data)
            
            # 夢日記は1ファイルあたりの肥大化を防ぐ（月ごと100件程度で十分）
            return data[:100]
        
        safe_json_update(str(monthly_file), update_func, default=[])

    def get_recent_insights_text(self, limit: int = 10) -> str:
        """
        プロンプト注入用：最新の「指針」をテキスト化して返す。
        - 最新の「本物の夢」からの指針 (最大1件)
        - 最新の「解決された問い」からの知見 (最大1件)
        を賢く選択して返す。
        """
        insights = self._load_insights()
        if not insights:
            return ""
        
        real_dream_strategy = None
        resolved_question_strategy = None
        
        # 最新数件からスキャン
        for item in insights[:limit]:
            trigger = item.get("trigger_topic", "")
            strategy = item.get("strategy", "")
            if not strategy:
                continue
            
            # 「解決された問い」系か、本物の夢か
            if "解決された問い:" in trigger:
                if not resolved_question_strategy:
                    resolved_question_strategy = strategy
            else:
                if not real_dream_strategy:
                    real_dream_strategy = strategy
            
            # 両方見つかったら早期終了
            if real_dream_strategy and resolved_question_strategy:
                break
            
        text_parts = []
        if real_dream_strategy:
            text_parts.append(f"- 深層意識の指針: {real_dream_strategy}")
        if resolved_question_strategy:
            text_parts.append(f"- 最近の気づき(問いの解決): {resolved_question_strategy}")
            
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
        log_path, system_prompt_path, _, _, _, _, _ = room_manager.get_room_files_paths(self.room_name)
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
        
        # 2. 直近のログを取得 (Lazy Loading)
        # コンテキスト把握のために少し多め(100件)に取得し、その中から直近30件を使用する
        raw_logs, _ = utils.load_chat_log_lazy(
            room_dir=os.path.dirname(log_path),
            limit=100 
        )
        recent_logs = raw_logs[-30:] # 文脈把握のため
        
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
        
        # 可視化：睡眠開始をログに記録
        log_f, _, _, _, _, _, _ = room_manager.get_room_files_paths(self.room_name)
        if log_f:
            utils.save_message_to_log(log_f, "## SYSTEM:dreaming_start", "💤 記憶の整理（夢想）を開始しました...")

        # 1. 最近の会話をロード
        recent_context = "\n".join([f"{m.get('role', 'UNKNOWN')}: {utils.remove_thoughts_from_text(m.get('content', ''))}" for m in recent_logs])
        query_prompt = f"""
        あなたはAIの「深層意識」です。
        以下の「直近の会話」から、内部知識ベース（Wikipedia）と照らし合わせるべき、文脈上重要な「固有名詞・人名・概念」を5〜10個抽出してください。
        
        【直近の会話】
        {recent_context[:2000]}

        【抽出ルール（最優先）】
        1.  **NO META-GROUPING (最重要)**: 
            - 「{user_name}の性格」「{user_name}の娘」「{user_name}の技術相談」といった、ユーザーに紐付けたメタな名前での抽出を**厳禁**する。
            - 代わりに「娘」「[具体的な技術名]」「[疾患名]」「[学校名]」など、対象そのものの固有名詞や名詞を抽出せよ。
        2.  **第三者・固有名詞を最優先**: 会話に出た第三者や、特筆すべき新しい概念。これらはあなた自身の記憶や相手の属性とは独立した「知識」として扱います。
        3.  **状態や誓い**: あなたと{user_name}の間の重要な約束や、深刻な感情の変化。

        【禁止事項（ノイズ除去）】
        - 「話題」「会話」「記録」「性格」「趣味」「悩み」といった抽象的なメタ単語はノイズになるため**厳禁**。
        - 目の前にあるだけの日常的な物（天気、椅子、お茶など）は除外。

        【出力形式】
        - 最も重要度の高い単語 5〜10個程度をスペース区切りで出力。
        """
        
        try:
            search_query_msg, current_api_key = self._invoke_llm(
                role="processing",
                prompt=query_prompt,
                settings=effective_settings
            )
            self.api_key = current_api_key
            search_query = search_query_msg.content.strip()
            print(f"  - [Dreaming] 生成されたクエリ: {search_query}")
        except Exception as e:
            return f"クエリ生成に失敗しました: {e}"

        # RAG Manager 初期化
        # RAG自体のエラーで夢想が止まらないよう、初期化と検索をガード
        search_results = []
        try:
            rag = rag_manager.RAGManager(self.room_name, self.api_key)
            search_results = rag.search(search_query, k=5)
            
            if not search_results:
                print("  - [Dreaming] 検索結果が空です。RAG索引の更新を試みます...")
                try:
                    rag.update_memory_index()
                    search_results = rag.search(search_query, k=5)
                except Exception as e:
                    print(f"  - [Dreaming] RAG索引の更新に失敗しました（フォールバックします）: {e}")
        except Exception as e:
            print(f"  - [Dreaming] RAG検索の初期化中にエラーが発生しました（直近会話のみで続行）: {e}")

        if not search_results:
            print("  - [Dreaming] 関連する過去の記憶が見つかりませんでした。直近の会話のみで夢を見ます。")
            past_memories = "（関連する過去の記憶は見つかりませんでした）"
        else:
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
        1.  **過去と現在の対比（最重要）**: 
            - 【想起された過去の記憶】と【直近の出来事】を比較し、ユーザーの言動や状態にどのような変化（あるいは不変の一貫性）があるかを見つけ出す。
            - 以前のあなたの認識と、現在の事実に乖離はないか？あれば修正する。
        2.  **深層分析**: 
            - 表面的な言葉だけでなく、その裏にある感情の流れや、信頼関係の深化、あるいは潜在的な課題を考察する。
        3.  **目標の整理**: 
            - 目標リストを精査し、達成したものは `completed_goals`、断念したものは `abandoned_goals` に振り分ける。
            - 短期目標は常に最新の状態に更新し、10件以内に保つ。
        4.  **出力生成**: 
            - 分析結果を、**あなたの人格（一人称、口調、相手の呼び方）**に変換して記述する。

        【出力フォーマット】
        以下のJSON形式のみを出力してください。思考やMarkdownの枠は不要です。
        
        {{
            "insight": "（ステップ4で変換した洞察。過去との比較や、関係性の変化について、あなた自身の言葉で深く語ること。**300文字以内**）",
            "strategy": "（その分析に基づき、今後あなたがどう行動するかの指針。抽象的なスローガンではなく、具体的な接し方や心構え。**150文字以内**）",
            "log_entry": "（夢日記として残す、短い独白。夢の中でのつぶやき。）",
            "entity_updates": [
                {{
                    "entity_name": "（対象となる独立した人物名、概念、または固有名詞。例: 娘, 先生, [技術名], [学校名]）",
                    "content": "（その対象について、今回の会話で新たに判明した事実や本質。あなた自身の内省を含めても良いが、事実は正確に。）",
                    "consolidate": true
                }}
            ],
            "entity_reason": "（なぜこれらの項目を更新/作成したかの理由。特に「～の～」といったメタ項目ではなく、独立した記事にした理由。）",
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
            response_msg, current_api_key = self._invoke_llm(
                role="summarization",
                prompt=dreaming_prompt,
                settings=effective_settings
            )
            self.api_key = current_api_key
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
                    print(f"  ✅ {self.room_name}: 省察が完了しました。")
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

            # 可視化：睡眠完了をログに記録
            if log_f:
                utils.save_message_to_log(log_f, "## SYSTEM:dreaming_end", "✅ 記憶の整理が完了しました。")

            return dream_data["insight"]

        except Exception as e:
            print(f"  - [Dreaming] 致命的なエラー: {e}")
            traceback.print_exc()
            
            # エラー時も完了ログ（失敗版）を記録することを検討
            if log_f:
                utils.save_message_to_log(log_f, "## SYSTEM:dreaming_error", f"❌ 記憶の整理中にエラーが発生しました: {e}")

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
- FACT: 人物・事物の属性、具体的な情報（例：「田中さんは猫を飼っている」）
- INSIGHT: 関係性、感情的な気づき、行動パターン（例：「田中さんが創作を語る時、目が輝く」）
- SKIP: 保存する価値がない（曖昧すぎる、一時的すぎる等）

【出力形式】JSON（思考やMarkdown不要、JSONのみ）
{{
  "type": "FACT" | "INSIGHT" | "SKIP",
  "entity_name": "（FACTの場合、関連するエンティティ名。人名[娘など]、固有名詞、技術用語、またはトピック名。ユーザーに属する情報であっても、それ自体が独立した概念や人物であるなら、独立した名前を優先せよ）",
  "content": "（保存すべき内容。辞書やWikipediaに追記できるような、事実や洞察を簡潔に）",
  "strategy": "（INSIGHTの場合、その知見を今後の対話や行動にどう活かすか。**50文字以内の非常に簡潔な指針**）",
  "reason": "（SKIPの場合のみ、理由）"
}}
"""
            
            try:
                response_msg, current_api_key = self._invoke_llm("summarization", prompt, effective_settings)
                self.api_key = current_api_key  # キーを同期
                
                result = self._parse_json_robust(response_msg.content)
                if not result:
                    continue
                
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
                        "strategy": result.get("strategy", ""),  # LLMから得られた簡潔な指針を保存
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
以下の会話ログから、ルシアン（AI）の「内部Wikipedia（知識ベース）」に記録すべき重要な「人物」「概念・技術」「事物」を客観的に抽出してください。

【会話ログ】
{log_text[:5000]}

【既存のエンティティ】
{existing_str}

【抽出ルール】
1. **独立したエンティティの抽出**: 
   - 会話に登場した第三者（家族[娘など]、友人、特定の人物）は、ユーザーの属性としてではなく、独立した一個人として抽出せよ。
   - 重要な概念、特定の場所、技術用語、組織などは、それ自体を独立した項目として抽出せよ。
   - 「（誰）の～」といったメタな括りではなく、対象自体の名称（例：「娘」「[学校名]」「[特定の病名]」）を優先せよ。
2. **情報の鮮度**: 既存エンティティに新しい重要な事実が追加された場合も抽出対象とする。
3. **百科事典的価値**: 後で参照した際に、その対象の「辞書」として役立つ情報を優先せよ。

【除外対象】
- さほど重要でない一時的な話題、日常的な出来事（今日の食事、挨拶など）。
- 既に十分に記録されている既存エンティティ（新情報がない場合）。

【出力形式】JSON配列
```json
[
  {{"name": "エンティティ名", "is_new": true, "facts": ["事実1", "事実2"]}}
]
```
候補がない場合は空配列 `[]` を出力してください。
"""
        try:
            response_msg, current_api_key = self._invoke_llm("processing", prompt, effective_settings)
            self.api_key = current_api_key  # キーを同期
            
            return self._parse_json_robust(response_msg.content) or []
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

    def _parse_json_robust(self, text: str) -> Any:
        """
        LLMの出力からJSON部分を抽出し、可能な限りパースする。
        """
        if not text:
            return None
        
        # Markdownのコードブロックを除去
        text = re.sub(r'```json\s*', '', text)
        text = re.sub(r'```\s*', '', text)
        
        # JSONの境界を探す（{...} または [...]）
        match = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
        if not match:
            return None
            
        json_str = match.group(1).strip()
        
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # よくあるミス：末尾のカンマやコメントを除去してみる
            try:
                # 非常に単純なクリーンアップ（実用レベル）
                cleaned = re.sub(r',\s*([\]\}])', r'\1', json_str)
                cleaned = re.sub(r'//.*$', '', cleaned, flags=re.MULTILINE)
                return json.loads(cleaned)
            except Exception:
                return None

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
                    # [2026-02-11 FIX] last_api_key_name の永続保存を削除
                    # ローテーションはセッション内のみで管理し、ユーザーの選択キーを保護
                else:
                    raise Exception("利用可能なAPIキーがありません（枯渇）。")

            # 2. モデル生成
            llm = LLMFactory.create_chat_model(
                api_key=self.api_key,
                generation_config=settings,
                internal_role=role
            )
            
            try:
                return llm.invoke(prompt), self.api_key
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