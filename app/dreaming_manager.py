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

    def dream(self, reflection_level: int = 1, skip_consolidation: bool = False, skip_goal_update: bool = False) -> str:
        """
        夢を見る（Dreaming Process）のメインロジック。
        1. 直近ログの読み込み
        2. RAG検索
        3. 洞察の生成（汎用・ペルソナ主導版）
        4. 目標の評価・更新（Multi-Layer Reflection）
        5. 保存
        
        Args:
            reflection_level: 省察レベル（1=日次, 2=週次, 3=月次）
            skip_consolidation: Trueの場合、エンティティ記憶の更新をスキップ
            skip_goal_update: Trueの場合、目標の更新をスキップ
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
        - 思考プロセスを出力する場合は、必ず `[THOUGHT]` と `[/THOUGHT]` で囲んでください。
        - 最終的な回答（単語リスト）は、思考プロセスの外に、スペース区切りで出力してください。
        - 思考プロセス以外のテキストは、スペース区切りの単語リストのみにしてください。
        """
        
        try:
            search_query_msg, current_api_key = self._invoke_llm(
                role="processing",
                prompt=query_prompt,
                settings=effective_settings
            )
            self.api_key = current_api_key
            raw_query_content = utils.extract_text_from_llm_content(search_query_msg.content)
            # JSON形式で返ってきた場合を考慮してパースを試みる
            parsed_query = self._parse_json_robust(raw_query_content)
            if isinstance(parsed_query, dict) and "query" in parsed_query:
                search_query = parsed_query["query"]
            elif isinstance(parsed_query, str):
                search_query = parsed_query
            else:
                # JSONパースに失敗した場合、テキストの末尾の行（通常はここがクエリの単語リスト）を抽出
                lines = [l.strip() for l in raw_query_content.split('\n') if l.strip()]
                search_query = lines[-1] if lines else raw_query_content.strip()
            
            print(f"  - [Dreaming] 生成されたクエリ: '{search_query}'")
        except Exception as e:
            return f"クエリ生成に失敗しました: {e}"

        # RAG Manager 初期化
        # RAG自体のエラーで夢想が止まらないよう、初期化と検索をガード
        search_results = []
        if not search_query:
            print("  - [Dreaming] 生成されたクエリが空のため、RAG検索をスキップします。")
        else:
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
        
        # --- Twitter活動ログの取得（External Codex） ---
        twitter_activity_text = ""
        try:
            import twitter_activity_logger
            today_str = datetime.datetime.now().strftime('%Y-%m-%d')
            twitter_activity_text = twitter_activity_logger.get_daily_activity_summary_for_dreaming(
                self.room_name, today_str
            )
            if twitter_activity_text:
                print(f"  - [Dreaming] Twitter活動ログを取得しました。")
        except Exception as tw_err:
            print(f"  - [Dreaming] Twitter活動ログ取得エラー: {tw_err}")

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
        
        {f"【本日の外部活動（Twitter）】" + chr(10) + twitter_activity_text if twitter_activity_text else ""}

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

        【出力形式】
        - 思考プロセスを出力する場合は、必ず `[THOUGHT]` と `[/THOUGHT]` で囲んでください。
        - 最終的な回答（JSON）は、思考プロセスの外に出力してください。
        - JSON以外に、挨拶や説明などの余計なテキストは一切含めないでください。
        
        以下のJSON形式のみを出力してください。
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
            raw_content = response_msg.content
            response = utils.extract_text_from_llm_content(raw_content)
            
            # JSON部分を抽出してパース
            dream_data = self._parse_json_robust(response)
            if not dream_data:
                print("  - [Dreaming] ⚠️ JSONのパースに失敗しました。フォールバックデータを使用します。")
                # [DEBUG] パース失敗時に生レスポンスを保存
                try:
                    debug_log_path = os.path.join(constants.ROOMS_DIR, self.room_name, "logs", "dream_error_response.txt")
                    os.makedirs(os.path.dirname(debug_log_path), exist_ok=True)
                    with open(debug_log_path, "a", encoding="utf-8") as f:
                        f.write(f"--- Error at {datetime.datetime.now().isoformat()} ---\n")
                        f.write(f"Raw Response:\n{raw_content}\n\n")
                        f.write(f"Cleaned Response:\n{response}\n\n")
                except:
                    pass
                
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
            if skip_consolidation:
                should_update_entity = False
                print("  - [Dreaming] エンティティ記憶の更新はスキップされました（フラグ指定）")

            entity_updates = dream_data.get("entity_updates", [])
            
            if entity_updates and should_update_entity:
                em_manager = EntityMemoryManager(self.room_name)
                for update in entity_updates:
                    if not isinstance(update, dict):
                        # 文字列のみが返ってきた場合などの救済処置
                        if isinstance(update, str):
                            e_name = update
                            e_content = None
                        else:
                            continue
                    else:
                        e_name = update.get("entity_name")
                        e_content = update.get("content")
                    
                    # デフォルトを追記から統合(consolidate)に変更
                    e_consolidate = True
                    if isinstance(update, dict):
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
            if goal_updates and not skip_goal_update:
                try:
                    goal_manager.apply_reflection_updates(goal_updates)
                    print(f"  ✅ {self.room_name}: 省察が完了しました。")
                except Exception as ge:
                    print(f"  - [Dreaming] 目標更新エラー: {ge}")
            elif skip_goal_update:
                print("  - [Dreaming] 目標の自動更新はスキップされました（フラグ指定）")
            
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
                        if not isinstance(q, dict):
                            continue
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
            # --- [External Codex] Twitter交流相手のエンティティ候補追加 ---
            try:
                import twitter_activity_logger
                today_str = datetime.datetime.now().strftime('%Y-%m-%d')
                twitter_users = twitter_activity_logger.get_interacted_users(self.room_name, today_str)
                if twitter_users:
                    em_manager_tw = EntityMemoryManager(self.room_name)
                    existing_tw = em_manager_tw.list_entries()
                    new_twitter_users = [u for u in twitter_users if u not in existing_tw]
                    if new_twitter_users:
                        # 新規のTwitter交流相手を提案メッセージに含める
                        twitter_proposal = "【影の僕より：Twitterでの新しい交流相手】\n"
                        twitter_proposal += "以下のユーザーと今日Twitterで交流しました。記憶に残すか判断してください。\n"
                        for user in new_twitter_users:
                            twitter_proposal += f"- {user}\n"
                        twitter_proposal += "\n`write_entity_memory` ツールで、あなたの言葉で記録してください。"
                        self._queue_system_message(twitter_proposal)
                        print(f"  - [External Codex] Twitter交流相手{len(new_twitter_users)}件をエンティティ候補として提案")
            except Exception as tw_entity_err:
                print(f"  - [External Codex] Twitterエンティティ候補抽出エラー: {tw_entity_err}")
            
            try:
                em_manager = EntityMemoryManager(self.room_name)
                existing = em_manager.list_entries()
                candidates = self._extract_entity_candidates(recent_context, existing)
                
                if candidates:
                    print(f"  - [Shadow] {len(candidates)}件のエンティティ候補を抽出しました")
                    # 各候補に関連する記憶を検索して付与
                    rag = rag_manager.RAGManager(self.room_name, self.api_key)
                    for candidate in candidates:
                        if not isinstance(candidate, dict):
                            if isinstance(candidate, str):
                                # 文字列の場合は名前として扱う
                                candidate = {"name": candidate, "facts": [], "is_new": True}
                            else:
                                continue
                        
                        c_name = candidate.get("name", "")
                        if c_name:
                            related_memories = rag.search(c_name, k=3)
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
    
    def dream_insight_only(self) -> str:
        """
        洞察（夢日記）の生成のみを行い、重い記憶統合などの処理をスキップする。
        単体テストや高速なデバッグに使用。
        """
        return self.dream(reflection_level=1, skip_consolidation=True, skip_goal_update=True)

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
        """
        questions = mm.get_resolved_questions_for_conversion()
        if not questions: return 0
        
        print(f"  - [Phase B] {len(questions)}件の解決済み質問を記憶に変換中...")
        converted_count = 0
        for q in questions:
            topic, context, answer_summary = q.get("topic", ""), q.get("context", ""), q.get("answer_summary", "")
            if not answer_summary and topic:
                for line in recent_context.split("\n"):
                    if topic in line: answer_summary += line[:200] + "\n"
                answer_summary = answer_summary[:500] if answer_summary else "（回答詳細なし）"
            
            prompt = f"以下の「問い」と「回答」から、FACT/INSIGHT/SKIPに分類して抽出してください。\n\n【問い】{topic}\n【背景】{context}\n【回答要約】{answer_summary}\n\n出力形式: JSON"
            try:
                response_msg, current_api_key = self._invoke_llm("summarization", prompt, effective_settings)
                self.api_key = current_api_key
                result = self._parse_json_robust(utils.extract_text_from_llm_content(response_msg.content))
                if not result: continue
                
                c_type, content, e_name = result.get("type", "SKIP"), result.get("content", ""), result.get("entity_name", "")
                
                if c_type == "FACT" and e_name and content:
                    em_manager = EntityMemoryManager(self.room_name)
                    em_manager.create_or_update_entry(e_name, content, consolidate=True, api_key=self.api_key)
                    print(f"    → {topic[:20]}... を FACT としてエンティティ記憶に保存")
                    self._create_discovery_episode(topic, content)
                    mm.mark_question_converted(topic)
                    converted_count += 1
                elif c_type == "INSIGHT" and content:
                    insight_record = {
                        "created_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        "trigger_topic": f"解決された問い: {topic}",
                        "insight": content,
                        "strategy": result.get("strategy", ""),
                        "log_entry": f"問い「{topic}」への回答から得た気づき"
                    }
                    self._save_insight(insight_record)
                    print(f"    → {topic[:20]}... を INSIGHT として夢日記に保存")
                    self._create_discovery_episode(topic, content)
                    mm.mark_question_converted(topic)
                    converted_count += 1
                elif c_type == "SKIP":
                    mm.mark_question_converted(topic)
            except Exception as e:
                print(f"    → 問い「{topic[:20]}...」の変換でエラー: {e}")
        return converted_count
    
    def _create_discovery_episode(self, topic: str, content: str):
        """Phase G: 知識獲得時に発見エピソード記憶を生成する。"""
        try:
            epm = EpisodicMemoryManager(self.room_name)
            today, now_str = datetime.datetime.now().strftime('%Y-%m-%d'), datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            summary = f"【発見】「{topic}」について新たな発見: {content[:100]}..."
            epm._append_single_episode({
                "date": today, "summary": summary, "arousal": 0.6, "arousal_max": 0.6,
                "type": "discovery", "source_question": topic, "created_at": now_str
            })
            print(f"    ✨ 発見エピソード記憶を生成: {topic[:30]}...")
        except Exception as e: print(f"    ⚠️ 発見エピソード記憶の生成に失敗: {e}")
    # ========== [Phase 2] Shadow Servant: エンティティ候補抽出 ==========
    
    def _extract_entity_candidates(self, log_text: str, existing_entities: list) -> list:
        """影の僕: 会話から新しいエンティティ候補を客観的に抽出"""
        effective_settings = config_manager.get_effective_settings(self.room_name)
        existing_str = ", ".join(existing_entities) if existing_entities else "（なし）"
        prompt = f"あなたは情報抽出の専門家です。以下の会話からエンティティを抽出してください。\n\n【会話ログ】\n{log_text[:3000]}\n\n【既存】\n{existing_str}\n\n出力形式: JSON配列 [{{'name': '...', 'is_new': true, 'facts': [...]}}]"
        try:
            response_msg, current_api_key = self._invoke_llm("processing", prompt, effective_settings)
            self.api_key = current_api_key
            content_text = utils.extract_text_from_llm_content(response_msg.content)
            return self._parse_json_robust(content_text) or []
        except Exception as e:
            print(f"  - [Shadow] 候補抽出エラー: {e}")
            return []
    
    def _format_entity_proposal(self, candidates: list) -> str:
        """提案メッセージのフォーマット"""
        if not candidates: return ""
        p = ["【影の僕より：記録すべきエンティティの提案】\n"]
        for c in candidates:
            if not isinstance(c, dict):
                if isinstance(c, str):
                    c = {"name": c, "facts": []}
                else:
                    continue
            name, facts = c.get("name", "不明"), c.get("facts", [])
            p.append(f"\n### {name}\n" + "\n".join([f"- {f}" for f in facts]))
        p.append("\n\n`write_entity_memory` ツールを使用して記録してください。")
        return "\n".join(p)

    def _queue_system_message(self, message: str):
        if not message: return
        queue_file = self.memory_dir / "pending_system_messages.json"
        try:
            existing = []
            if queue_file.exists():
                with open(queue_file, 'r', encoding='utf-8') as f: existing = json.load(f)
            existing.append({"created_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "message": message})
            with open(queue_file, 'w', encoding='utf-8') as f: json.dump(existing[-5:], f, indent=2, ensure_ascii=False)
        except Exception as e: print(f"  - [Shadow] メッセージキュー保存エラー: {e}")

    def get_pending_system_messages(self) -> str:
        queue_file = self.memory_dir / "pending_system_messages.json"
        if not queue_file.exists(): return ""
        try:
            with open(queue_file, 'r', encoding='utf-8') as f: messages = json.load(f)
            if not messages: return ""
            queue_file.unlink()
            return messages[-1].get("message", "")
        except Exception as e: return ""
    def _parse_json_robust(self, text: str) -> Any:
        """LLMの出力からJSON部分を抽出し、可能な限りパースする。"""
        import ast
        if not text: return None
        text = re.sub(r'```(?:json|python)?\s*', '', text)
        text = re.sub(r'```\s*', '', text)
        def find_json_blocks(t: str):
            blocks, stack, start_idx = [], [], -1
            in_str, escape, q = False, False, ''
            for i, c in enumerate(t):
                if escape: escape = False; continue
                if c == '\\': escape = True; continue
                if not in_str:
                    if c in ('"', "'"): in_str, q = True, c
                    elif c == '{':
                        if not stack: start_idx = i
                        stack.append('{')
                    elif c == '}':
                        if stack:
                            stack.pop(); 
                            if not stack: blocks.append(t[start_idx:i+1])
                else:
                    if c == q: in_str = False
            return blocks
        candidates = find_json_blocks(text)
        if not candidates:
            m = re.search(r'(\[[\s\S]*\])', text)
            candidates = [m.group(1)] if m else [text.strip()]
        
        priority_keys = ["insight", "strategy", "log_entry", "query", "entity_updates"]
        scored = []
        for cand in candidates:
            c_low = cand.lower()
            score = sum(2 for pk in priority_keys if f'"{pk}"' in c_low or f"'{pk}'" in c_low)
            if '"thinking"' in c_low or "'thinking'" in c_low: score -= 5
            if cand.strip().startswith('{') and cand.strip().endswith('}'): score += 1
            scored.append((score, cand))
        scored.sort(key=lambda x: x[0], reverse=True)

        for _, target in scored:
            if not target: continue
            try: return self._unwrap_result(json.loads(target))
            except: pass
            try:
                cleaned = re.sub(r',\s*([\]\}])', r'\1', target)
                return self._unwrap_result(json.loads(cleaned))
            except: pass
            try:
                py = target.replace(': true', ': True').replace(': false', ': False').replace(': null', ': None')
                return self._unwrap_result(ast.literal_eval(py))
            except: pass
        return None

    def _unwrap_result(self, res: Any) -> Any:
        """ラップされたJSONを剥ぎ、文字列内のエスケープを処理する。"""
        if isinstance(res, list):
            return [self._unwrap_result(item) for item in res]
        if not isinstance(res, dict):
            if isinstance(res, str):
                # エスケープされた改行を本物の改行に置換
                return res.replace('\\n', '\n').strip()
            return res

        # 重要キーが含まれる場合はそれ以上潜らないが、中身の文字列はクリーンアップする
        if any(k in res for k in ["insight", "strategy", "query", "entity_updates"]):
            for k, v in res.items():
                res[k] = self._unwrap_result(v)
            return res

        # ラッパー（result, response等）の剥離
        for w in ["result", "response", "content", "data"]:
            if w in res and isinstance(res[w], (dict, list, str)):
                v = res[w]
                if isinstance(v, str) and v.strip().startswith('{'):
                    n = self._parse_json_robust(v)
                    if n: return n
                return self._unwrap_result(v)
        
        # それ以外の辞書も中身をクリーンアップ
        for k, v in res.items():
            res[k] = self._unwrap_result(v)
        return res

    def _invoke_llm(self, role: str, prompt: str, settings: dict) -> Any:
        """LLM呼び出しラッパー。"""
        response, used_key = LLMFactory.invoke_internal_llm(role, prompt, self.room_name, settings, self.api_key)
        self.api_key = used_key
        return response, used_key
