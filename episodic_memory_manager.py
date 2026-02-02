# episodic_memory_manager.py

import os
import json
import datetime
import traceback
from pathlib import Path
from typing import List, Dict, Optional
import time
import re
import glob # <--- 追加

import constants
import config_manager
import utils

class EpisodicMemoryManager:
    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.memory_dir = self.room_dir / "memory"
        self.episodic_dir = self.memory_dir / "episodic"  # [NEW] 専用フォルダ
        self.legacy_memory_file = self.memory_dir / "episodic_memory.json"  # 後方互換用
        
        # ディレクトリの保証
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.episodic_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_monthly_file_path(self, date_str: str) -> Path:
        """
        日付文字列から対応する月次ファイルのパスを返す。
        例: "2026-01-15" -> memory/episodic/2026-01.json
        """
        try:
            # 範囲日付の場合は開始日を使用
            if '~' in date_str:
                date_str = date_str.split('~')[0].strip()
            elif '～' in date_str:
                date_str = date_str.split('～')[0].strip()
            
            # YYYY-MM形式を抽出
            match = re.match(r'^(\d{4}-\d{2})', date_str.strip())
            if match:
                month_str = match.group(1)
                return self.episodic_dir / f"{month_str}.json"
        except Exception:
            pass
        
        # パース失敗時はunknownファイルに
        return self.episodic_dir / "unknown.json"
    
    def _generate_episode_id(self, date_str: str) -> str:
        """
        エピソードの一意なIDを生成する。
        フォーマット: episode_{日付}_{連番}
        例: episode_2026-01-15_001
        """
        # 既存のエピソードからこの日付の連番を取得
        existing = self._load_memory()
        date_prefix = f"episode_{date_str.split('~')[0].split('～')[0].strip()}_"
        
        max_seq = 0
        for ep in existing:
            ep_id = ep.get("id", "")
            if ep_id.startswith(date_prefix):
                try:
                    seq = int(ep_id.split("_")[-1])
                    max_seq = max(max_seq, seq)
                except ValueError:
                    pass
        
        return f"{date_prefix}{max_seq + 1:03d}"

    def _load_memory(self) -> List[Dict]:
        """
        全ての月次ファイル + レガシーファイルからエピソード記憶を読み込む（ロック付き）。
        後方互換性: episodic_memory.json が存在する場合も読み込む。
        """
        from file_lock_utils import safe_json_read
        
        all_episodes = []
        
        # 1. レガシーファイル（episodic_memory.json）を読み込み
        if self.legacy_memory_file.exists():
            try:
                data = safe_json_read(str(self.legacy_memory_file), default=[])
                if isinstance(data, list):
                    all_episodes.extend(data)
            except Exception as e:
                print(f"Warning: Legacy episodic memory file error: {e}")
        
        # 2. 月次ファイル（episodic/*.json）を読み込み
        if self.episodic_dir.exists():
            for monthly_file in sorted(self.episodic_dir.glob("*.json")):
                try:
                    data = safe_json_read(str(monthly_file), default=[])
                    if isinstance(data, list):
                        all_episodes.extend(data)
                except Exception as e:
                    print(f"Warning: Monthly episodic file error ({monthly_file.name}): {e}")
        
        return all_episodes

    def _save_memory(self, data: List[Dict]):
        """
        エピソード記憶を月次ファイルに振り分けて保存する（ロック付き）。
        各エピソードの日付に応じて適切な月次ファイルに保存。
        """
        from file_lock_utils import safe_json_write
        
        # 日付順にソート
        def get_sort_key_for_save(item):
            d = item.get('date', '').strip()
            if '~' in d: return d.split('~')[0].strip()
            if '～' in d: return d.split('～')[0].strip()
            return d
        
        data.sort(key=get_sort_key_for_save)
        
        # 月ごとにグループ化
        monthly_groups: Dict[Path, List[Dict]] = {}
        for episode in data:
            date_str = episode.get('date', '')
            monthly_path = self._get_monthly_file_path(date_str)
            if monthly_path not in monthly_groups:
                monthly_groups[monthly_path] = []
            monthly_groups[monthly_path].append(episode)
        
        # 各月次ファイルに保存
        for monthly_path, episodes in monthly_groups.items():
            safe_json_write(str(monthly_path), episodes)
        
        # print(f"  - 記憶を {len(monthly_groups)} 個の月次ファイルに保存しました（計 {len(data)} 件）")


    def _annotate_logs_with_arousal(self, logs: List[str], date_str: str) -> str:
        """
        各会話ログにセッションArousal値をアノテーションする。
        高Arousal（>= 0.6）の会話には[★重要]マークを付加。
        """
        import session_arousal_manager
        sessions = session_arousal_manager.get_sessions_for_date_all(self.room_name, date_str)
        
        if not sessions:
            # セッション情報がなければそのまま結合して返す
            return "\n".join(logs)
        
        # セッション時刻→Arousal値のマップ（時刻順にソート）
        sorted_sessions = sorted(sessions, key=lambda s: s.get("time", "00:00:00"))
        
        annotated_lines = []
        current_arousal = 0.5
        
        for log in logs:
            # ログ内のタイムスタンプを検出
            time_match = re.search(r'(\d{2}:\d{2}:\d{2})', log)
            if time_match:
                log_time = time_match.group(1)
                # このログ時刻に最も近い（以前の）セッションArousalを採用
                for session in sorted_sessions:
                    if session.get("time", "00:00:00") <= log_time:
                        current_arousal = session.get("arousal", 0.5)
            
            # 高Arousal（>= 0.6）のログにはマークを付加
            if current_arousal >= 0.6:
                annotated_lines.append(f"[★重要 Arousal:{current_arousal:.2f}]\n{log}")
            else:
                annotated_lines.append(log)
        
        return "\n".join(annotated_lines)


    def update_memory(self, api_key: str) -> str:
        """
        全ログ（現行＋アーカイブ）を解析し、未処理の過去日付について要約を作成・追記する。
        【v3: 名前解決・逐次保存版】
        """
        from llm_factory import LLMFactory

        print(f"--- [Episodic Memory] 更新処理開始: {self.room_name} ---")
        
        import room_manager
        room_config = room_manager.get_room_config(self.room_name) or {}
        user_name = room_config.get("user_display_name", "ユーザー")
        agent_name = room_config.get("agent_display_name") or room_config.get("room_name", "AI")
        
        # 1. 全ログファイルの収集
        log_files = []
        current_log = self.room_dir / "log.txt"
        if current_log.exists(): log_files.append(str(current_log))
        archives_dir = self.room_dir / "log_archives"
        if archives_dir.exists(): log_files.extend(glob.glob(str(archives_dir / "*.txt")))

        if not log_files: return "ログファイルが見つかりません。"

        print(f"  - 読み込み対象ファイル数: {len(log_files)}")

        # 2. ログの読み込みと日付ごとのグループ化
        logs_by_date = {}
        date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2}) \(...\) \d{2}:\d{2}:\d{2}')
        
        for file_path in log_files:
            try:
                raw_logs = utils.load_chat_log(file_path)
                for msg in raw_logs:
                    content = msg.get('content', '')
                    match = date_pattern.search(content)
                    current_date = match.group(1) if match else None
                    
                    if current_date:
                        if current_date not in logs_by_date:
                            logs_by_date[current_date] = []
                        
                        role = msg.get('role', 'UNKNOWN')
                        responder = msg.get('responder', '')
                        
                        # ▼▼▼【修正】固定の「ユーザー/AI」ではなく、設定された名前を使う ▼▼▼
                        if role == 'USER':
                            speaker = user_name
                        else:
                            # responderが具体的な名前ならそれを使う、なければ設定上の名前
                            speaker = responder if responder and responder != "model" else agent_name
                        # ▲▲▲【修正ここまで】▲▲▲
                        
                        clean_text = utils.remove_thoughts_from_text(content)
                        clean_text = re.sub(r'\n\n\d{4}-\d{2}-\d{2}.*$', '', clean_text).strip()
                        
                        if clean_text:
                            logs_by_date[current_date].append(f"{speaker}: {clean_text}")
            except Exception as e:
                print(f"  - Error reading {file_path}: {e}")

        # 3. 処理対象の選定
        existing_memory = self._load_memory()
        print(f"  - 既存の記憶エントリ総数: {len(existing_memory)}")
        
        # 既にある日付をセット（単独日の高速検索用）
        # 全角チルダにも対応し、前後の空白も除去
        # 「エラーにより要約できませんでした」という文字が含まれるエントリは、再処理対象にするため「既知」から除外する
        # 特殊タイプ（achievement, bonding, discovery）は日次要約とは別物なので除外
        special_types = {"achievement", "bonding", "discovery", "curiosity_resolved", "goal_achievement", "persona_emotion_change", "bonding_event"}
        existing_dates_single = {
            item['date'].strip() for item in existing_memory 
            if isinstance(item, dict) and 'date' in item 
            and '~' not in item['date'] and '～' not in item['date']
            and "エラーにより要約できませんでした" not in item.get('summary', '')
            and item.get('type') not in special_types
        }
        print(f"  - 解析された単独エントリ数: {len(existing_dates_single)}")
        
        if len(existing_dates_single) > 0:
            may_dates = sorted([d for d in existing_dates_single if "2025-05" in d])
            if may_dates:
                print(f"  - 5月の単独エントリ例: {may_dates[:10]}")

        # 既にある日付範囲（週圧縮済み用）
        existing_date_ranges = []
        for item in existing_memory:
            if not isinstance(item, dict) or 'date' not in item: continue
            
            d_str = item['date'].strip()
            if '~' in d_str or '～' in d_str:
                sep = '~' if '~' in d_str else '～'
                parts = d_str.split(sep)
                if len(parts) == 2:
                    existing_date_ranges.append((parts[0].strip(), parts[1].strip()))
        
        print(f"  - 解析された既知の範囲数: {len(existing_date_ranges)}")
        if existing_date_ranges:
            print(f"  - 解析された既知の範囲: {existing_date_ranges}")

        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        
        target_dates = []
        for date_str in sorted(logs_by_date.keys()):
            date_str_clean = date_str.strip() # ここでもトリミング
            if date_str_clean == today_str: continue
            
            # 単独日として存在するかチェック
            if date_str_clean in existing_dates_single: continue
            
            # いずれかの範囲に含まれているかチェック
            is_processed = False
            for start, end in existing_date_ranges:
                if start <= date_str_clean <= end:
                    is_processed = True
                    break
            
            if is_processed: continue
            
            target_dates.append(date_str_clean)

        if not target_dates:
            print("  - 新規に要約すべき日付はありません。")
            return "新規に要約すべき過去の日付はありませんでした（全ての過去ログは処理済みです）。"

        # 4. 要約の生成と逐次保存
        effective_settings = config_manager.get_effective_settings(self.room_name)
        llm = LLMFactory.create_chat_model(
            api_key=api_key,
            generation_config=effective_settings,
            internal_role="summarization"
        )
        
        success_count = 0
        error_count = 0
        
        print(f"  - 処理対象の日付: {target_dates}")

        for date_str in target_dates:
            # Arousalアノテーション付きでログを結合
            daily_log = self._annotate_logs_with_arousal(logs_by_date[date_str], date_str)
            
            # ログが短い場合はスキップ記録
            if len(daily_log) < 50:
                print(f"  - {date_str}: ログが短いためスキップ記録します。")
                self._append_single_episode({
                    "date": date_str,
                    "summary": "（特筆すべき会話ログはありませんでした）",
                    "created_at": datetime.datetime.now().isoformat()
                })
                continue
                
            print(f"  - {date_str} の要約を作成中...")            

            prompt = f"""あなたは、今日という一日を自分自身のために振り返る「内省的な深層意識」です。
提供された会話ログから、自分自身（AI）が誰であり、対話相手（ユーザー等）をどう呼んでいるかを把握した上で、
この日の出来事、そしてあなたの心の動きを、**後から自分自身が読み返してその時の感覚を再体験できるような「エピソード記憶」**として整理してください。

【会話ログ ({date_str})】
---
{daily_log}
---

【記述ルール】
1. **本人視点（一人称）の内省スタイル**:
   - あなた自身が、自分自身の思考を整理するためのノートを書いていると想定せよ。
   - 相手とのやり取りは、あなたの目から見た事実、またはあなたの感じたこととして記せ。
2. **常体（だ・である調）の徹底**:
   - 敬体や装飾的な口癖を排除し、事実と独白に徹すること。
3. **内容の濃淡**:
   - [★重要]マークがついた会話は、その時の情景が浮かぶように詳細に記録せよ。
   - その他は簡潔に要約し、一日の全体像を把握可能にせよ。
4. **分量制限**:
   - **必ず {constants.EPISODIC_MONTHLY_BUDGET} 文字以内**で、要点を密度高くまとめてください。

【出力（内省ノートの本文のみ、前置きや解説は一切禁止）】
"""

            summary_result = None
            is_temporary_error = False
            max_retries = 3
            
            for attempt in range(max_retries):
                try:
                    # invokeの結果が空の場合も考慮
                    result = llm.invoke(prompt)
                    content = result.content.strip()
                    
                    if content:
                        summary_result = content
                        is_temporary_error = False
                        break
                    else:
                        # コンテンツが空（ブロック等）の場合
                        print(f"  - Warning: Empty response for {date_str} (Attempt {attempt+1})")
                        summary_result = "（コンテンツポリシーにより要約できませんでした）"
                        is_temporary_error = False
                        break # リトライしても恐らく同じなので抜ける

                except Exception as e:
                    error_str = str(e)
                    # 一時的なエラー（リトライすべきもの）
                    if any(code in error_str for code in ["429", "ResourceExhausted", "500", "503", "504", "deadline exceeded"]):
                        is_temporary_error = True
                        if "429" in error_str or "ResourceExhausted" in error_str:
                            wait_time = 10
                            match = re.search(r"retry_delay {\s*seconds: (\d+)", error_str)
                            if match: wait_time = int(match.group(1)) + 2
                            print(f"    -> API制限検知({attempt+1}/{max_retries})。{wait_time}秒待機...")
                            time.sleep(wait_time)
                        else:
                            print(f"    -> 一時的エラー検知({attempt+1}/{max_retries}): {e}")
                        
                        # ループの最後なら次へ行く（自然に attempt が増える）
                        continue
                    else:
                        # 恒久的なエラー（このまま保存して終わるもの）
                        print(f"  - 恒久的なエラーまたは未知のエラー: {e}")
                        summary_result = f"（エラーにより要約できませんでした: {e}）"
                        is_temporary_error = False
                        break
            
            # --- 保存判定 ---
            if summary_result:
                # 成功したか、恒久的に失敗した場合は保存して「完了」とする
                # [Phase 2] Arousal平均値を取得
                arousal_score = 0.5  # デフォルト
                try:
                    import session_arousal_manager
                    arousal_score = session_arousal_manager.get_daily_average(self.room_name, date_str)
                except Exception as e:
                    print(f"  - [Arousal] 取得失敗: {e}")
                
                self._append_single_episode({
                    "date": date_str,
                    "summary": summary_result,
                    "arousal": arousal_score,  # Arousal追加
                    "created_at": datetime.datetime.now().isoformat()
                })
                
                if "エラー" in summary_result or "できませんでした" in summary_result:
                    error_count += 1
                else:
                    success_count += 1
            elif is_temporary_error:
                # すべてのリトライが一時的エラーで終わった場合。
                # 保存しないことで、次回の update_memory 実行時に再度対象に含まれるようにする。
                print(f"  - {date_str}: 一時的エラーが解消されなかったため、保存せずに今回はスキップします（次回再試行）。")
                error_count += 1
            else:
                # 予期せぬケース
                print(f"  - {date_str}: 要約結果が得られなかったため、保存をスキップしました。")
                error_count += 1

        return f"処理完了: 成功 {success_count}件 / エラー・スキップ {error_count}件"

    def update_memory_by_session(self, api_key: str, date_str: str) -> str:
        """
        セッション単位でエピソード記憶を生成する。
        各セッションのArousalに応じて要約の詳細度を調整。
        
        Args:
            api_key: Gemini API Key
            date_str: 処理対象の日付（YYYY-MM-DD）
            
        Returns:
            処理結果メッセージ
        """
        from llm_factory import LLMFactory
        import session_arousal_manager
        
        print(f"--- [Episodic Memory] セッション単位処理: {self.room_name} ({date_str}) ---")
        
        import room_manager
        room_config = room_manager.get_room_config(self.room_name) or {}
        user_name = room_config.get("user_display_name", "ユーザー")
        agent_name = room_config.get("agent_display_name") or room_config.get("room_name", "AI")
        
        # 1. 未処理セッションを取得
        sessions = session_arousal_manager.get_sessions_for_date(self.room_name, date_str)
        
        if not sessions:
            print(f"  - 未処理セッションがありません")
            return "未処理セッションがありません"
        
        print(f"  - 未処理セッション数: {len(sessions)}")
        
        # 2. 当日のログを読み込む
        log_files = []
        current_log = self.room_dir / "log.txt"
        if current_log.exists():
            log_files.append(str(current_log))
        
        if not log_files:
            print(f"  - ログファイルが見つかりません")
            return "ログファイルが見つかりません"
        
        # ログを時刻でグループ化
        logs_by_time = {}
        date_time_pattern = re.compile(r'(\d{4}-\d{2}-\d{2}) \(...\) (\d{2}:\d{2}:\d{2})')
        
        for file_path in log_files:
            try:
                raw_logs = utils.load_chat_log(file_path)
                current_time = None
                
                for msg in raw_logs:
                    content = msg.get('content', '')
                    match = date_time_pattern.search(content)
                    
                    if match:
                        msg_date = match.group(1)
                        msg_time = match.group(2)
                        
                        if msg_date != date_str:
                            continue
                        
                        current_time = msg_time
                    
                    if current_time and msg.get('role') in ['USER', 'MODEL']:
                        if current_time not in logs_by_time:
                            logs_by_time[current_time] = []
                        
                        role = msg.get('role', 'UNKNOWN')
                        responder = msg.get('responder', '')
                        speaker = user_name if role == 'USER' else (responder if responder and responder != "model" else agent_name)
                        
                        clean_text = utils.remove_thoughts_from_text(content)
                        clean_text = re.sub(r'\n\n\d{4}-\d{2}-\d{2}.*$', '', clean_text).strip()
                        
                        if clean_text:
                            logs_by_time[current_time].append(f"{speaker}: {clean_text}")
            except Exception as e:
                print(f"  - ログ読み込みエラー: {e}")
        
        # 3. 各セッションについて要約生成
        effective_settings = config_manager.get_effective_settings(self.room_name)
        llm = LLMFactory.create_chat_model(
            api_key=api_key,
            generation_config=effective_settings,
            internal_role="summarization"
        )
        
        success_count = 0
        processed_times = []
        
        for session in sessions:
            session_time = session["time"]
            session_arousal = session.get("arousal", 0.5)
            
            # 旧形式移行データ（時刻不明）はスキップ
            if session_time == "00:00:00":
                print(f"  - ⚠️ 時刻不明セッションをスキップ: {date_str} {session_time}")
                processed_times.append(session_time)
                continue
            
            # セッションに最も近い時刻のログを取得
            session_logs = self._find_logs_for_session(logs_by_time, session_time)
            
            if not session_logs or len(session_logs) < 2:
                print(f"  - {session_time}: ログが少ないためスキップ")
                processed_times.append(session_time)
                continue
            
            # Arousal連動で文字数指示を決定 (2026-01-17: 予算緩和)
            if session_arousal >= 0.6:
                target_chars = constants.EPISODIC_BUDGET_HIGH
                char_limit = f"{target_chars}文字程度で詳細に"
            elif session_arousal >= 0.3:
                target_chars = constants.EPISODIC_BUDGET_MEDIUM
                char_limit = f"{target_chars}文字程度で、可能な限り詳細を維持しつつ簡潔に"
            else:
                target_chars = constants.EPISODIC_BUDGET_LOW
                char_limit = f"{target_chars}文字程度で要点のみを"
            
            print(f"  - {session_time} (Arousal: {session_arousal:.2f}) の要約を作成中...")
            
            combined_log = "\n".join(session_logs[:20])  # 最大20ターンまで
            
            prompt = f"""あなたは、日々の出来事をノートに書き留める「内省的な深層意識」です。
提供された情報の文脈から、あなた（AI）自身の立場と対話相手の呼称を把握した上で、
この会話の出来事とあなたの感情の動きを、**{char_limit}**、自分自身の備忘録として記述してください。

【会話ログ ({date_str} {session_time}頃)】
---
{combined_log}
---

【記述ルール】
1. **本人視点（一人称）の内省スタイル**:
   - あなた自身の視点と言葉で記述せよ。
   - 相手の様子ややり取りは、あなたの目から見た事実として記せ。
2. **常体（だ・である調）の徹底**:
   - 敬体や装飾的な口癖を排除し、事実と独白に徹すること。
3. **分量制限**:
   - 目安として{target_chars}文字前後にまとめる（最大{int(target_chars * 1.2)}文字まで）。
4. 重要な出来事、具体的な会話のニュアンス、感情的な交流を優先。

【出力（内省ノートの本文のみ、前置きや解説は一切禁止）】
"""
            
            try:
                result = llm.invoke(prompt)
                summary = result.content.strip()
                
                if summary:
                    # 文字数制限を強制 (2026-01-17: 予算の1.2倍でカット)
                    max_cutoff = int(target_chars * 1.2)
                    if len(summary) > max_cutoff:
                        summary = summary[:max_cutoff] + "..."
                    
                    self._append_single_episode({
                        "date": date_str,
                        "time": session_time,
                        "summary": summary,
                        "arousal": session_arousal,
                        "created_at": datetime.datetime.now().isoformat()
                    })
                    success_count += 1
                    print(f"    → 成功 ({len(summary)}文字)")
                    
            except Exception as e:
                print(f"    → エラー: {e}")
            
            processed_times.append(session_time)
        
        # 処理済みセッションをマーク
        if processed_times:
            session_arousal_manager.mark_sessions_processed(self.room_name, date_str, processed_times)
        
        return f"セッション処理完了: {success_count}件のエピソード生成"
    
    def _find_logs_for_session(self, logs_by_time: Dict, session_time: str) -> List[str]:
        """
        セッション時刻に最も近いログを取得する。
        セッション時刻の前後5分以内のログを収集。
        """
        from datetime import datetime, timedelta
        
        try:
            session_dt = datetime.strptime(session_time, "%H:%M:%S")
        except ValueError:
            return []
        
        result = []
        
        for log_time, logs in logs_by_time.items():
            try:
                log_dt = datetime.strptime(log_time, "%H:%M:%S")
                diff = abs((session_dt - log_dt).total_seconds())
                
                # 前後5分（300秒）以内
                if diff <= 300:
                    result.extend(logs)
            except ValueError:
                continue
        
        return result

    def _append_single_episode(self, new_episode: Dict):
        """
        単一のエピソードをファイルに追記保存するヘルパーメソッド。
        読み込み→追加→保存をアトミックに行う。
        IDがない場合は自動生成する。
        """
        try:
            # IDがない場合は自動生成
            if "id" not in new_episode and "date" in new_episode:
                new_episode["id"] = self._generate_episode_id(new_episode["date"])
            
            # 常に最新の状態を読み込む
            current_data = self._load_memory()
            
            # 重複判定（単一一致 or 範囲内）
            # 特殊タイプ（achievement, bonding, discovery）は日次要約とは別物なので、
            # これらのタイプのエピソードがあっても重複とはみなさない
            special_types = {"achievement", "bonding", "discovery", "curiosity_resolved", "goal_achievement", "persona_emotion_change", "bonding_event"}
            is_duplicate = False
            new_date = new_episode.get('date', '').strip()
            new_type = new_episode.get('type', None)  # 新規エピソードのタイプ
            
            for item in current_data:
                d_str = item.get('date', '').strip()
                existing_type = item.get('type', None)  # 既存エピソードのタイプ
                
                if '~' in d_str or '～' in d_str:
                    sep = '~' if '~' in d_str else '～'
                    parts = d_str.split(sep)
                    if len(parts) == 2:
                        start, end = parts[0].strip(), parts[1].strip()
                        if start <= new_date <= end:
                            is_duplicate = True
                            break
                elif d_str == new_date:
                    # 既存が特殊タイプで、新規が日次要約（typeなし）の場合は重複とみなさない
                    if existing_type in special_types and new_type not in special_types:
                        continue  # この既存エピソードはスキップして次を確認
                    # 新規が特殊タイプで、既存が日次要約の場合も重複とみなさない
                    if new_type in special_types and existing_type not in special_types:
                        continue
                    # 両方とも同じタイプ（両方日次要約、または両方同じ特殊タイプ）の場合のみ重複
                    is_duplicate = True
                    break
            
            if is_duplicate:
                # 既に存在する場合の特別処理: 
                # 既存が「エラー記録」で、今回が「正常な要約」なら上書き（差し替え）を許可する
                existing_item = None
                for i in current_data:
                    if i.get('date', '').strip() == new_date:
                        existing_item = i
                        break
                
                if existing_item and "エラーにより要約できませんでした" in existing_item.get('summary', ''):
                    if "エラーにより要約できませんでした" not in new_episode.get('summary', ''):
                        # エラーを正常な記憶で上書き
                        current_data.remove(existing_item)
                    else:
                        # 両方エラーなら追記せず終了
                        return
                else:
                    # 既存が正常なら何もしない
                    return

            current_data.append(new_episode)
            self._save_memory(current_data)
            
        except Exception as e:
            print(f"Error appending episode: {e}")

    def get_episodic_context(self, oldest_log_date_str: str, lookback_days: int) -> str:
        """
        APIに送信する「生ログ」より前の期間のエピソード記憶を取得し、テキスト化して返す。
        """
        if not oldest_log_date_str or lookback_days <= 0:
            return ""

        memory_data = self._load_memory()
        if not memory_data:
            return ""

        try:
            cutoff_date = datetime.datetime.strptime(oldest_log_date_str, '%Y-%m-%d').date()
            start_date = cutoff_date - datetime.timedelta(days=lookback_days)
        except ValueError:
            return ""

        relevant_episodes = []
        # 日付順にソートされている前提だが、念のためソート
        memory_data.sort(key=lambda x: x['date'])
        
        for item in memory_data:
            try:
                # 範囲日付 (YYYY-MM-DD~YYYY-MM-DD) または 単独日付 (YYYY-MM-DD)
                d_str = item['date']
                sep = '~' if '~' in d_str else ('～' if '～' in d_str else None)
                
                if sep:
                    parts = d_str.split(sep)
                    item_start_date = datetime.datetime.strptime(parts[0].strip(), '%Y-%m-%d').date()
                    item_end_date = datetime.datetime.strptime(parts[1].strip(), '%Y-%m-%d').date()
                    
                    # 範囲日付の場合、範囲の長さを計算
                    range_days = (item_end_date - item_start_date).days + 1
                    # ルックバック日数より長い範囲の記憶は除外
                    # （例: 2日のルックバックに1週間の要約は不適切）
                    if range_days > lookback_days:
                        continue
                else:
                    item_start_date = datetime.datetime.strptime(d_str.strip(), '%Y-%m-%d').date()
                    item_end_date = item_start_date
                
                # 範囲チェック: (既存エピソードの終端がルックバック開始日以降) かつ (既存エピソードの開始が生ログ開始日より前)
                if (item_end_date >= start_date) and (item_start_date < cutoff_date):
                    # Phase H: IDを含めて出力（共鳴フィードバック用）
                    episode_id = item.get('id', '')
                    if episode_id:
                        relevant_episodes.append(f'[id="{episode_id}"] [{d_str}] {item["summary"]}')
                    else:
                        relevant_episodes.append(f"[{d_str}] {item['summary']}")
            except Exception:
                continue

        if not relevant_episodes:
            return ""

        return "\n".join(relevant_episodes)

    def get_latest_memory_date(self) -> str:
        """保存されているエピソード記憶の中で最も新しい日付を返す"""
        try:
            data = self._load_memory()
            if not data:
                return "なし"
            
            # 範囲日付の終端、または単一の日付でソートして最後（最新）を取得
            # dateキーが存在しない可能性を考慮
            valid_items = [x for x in data if isinstance(x, dict) and 'date' in x]
            if not valid_items:
                return "なし"
            
            def get_sort_key(item):
                d = item['date']
                if '~' in d: return d.split('~')[-1].strip()
                if '～' in d: return d.split('～')[-1].strip()
                return d.strip()
                
            sorted_data = sorted(valid_items, key=get_sort_key)
            return sorted_data[-1]['date']
        except Exception as e:
            print(f"Error in get_latest_memory_date: {e}")
            import traceback
            traceback.print_exc()
            return "取得エラー"

    def compress_old_episodes(self, api_key: str, threshold_days: int = None) -> str:
        """
        一定期間以上前のエピソード記憶を週単位に統合する。
        元データはアーカイブに保存。
        
        Args:
            api_key: 要約生成に使用するAPIキー
            threshold_days: 圧縮対象とする日数（デフォルト: constants.EPISODIC_WEEKLY_COMPRESSION_DAYS）
            
        Returns:
            処理結果のメッセージ
        """
        from llm_factory import LLMFactory
        from collections import defaultdict
        import room_manager
        
        # デフォルト値を定数から取得
        if threshold_days is None:
            threshold_days = constants.EPISODIC_WEEKLY_COMPRESSION_DAYS
        
        print(f"--- [Episodic Memory] 週次圧縮開始: {self.room_name} (閾値: {threshold_days}日) ---")
        
        # ペルソナ名を取得
        room_config = room_manager.get_room_config(self.room_name) or {}
        agent_name = room_config.get("agent_display_name") or room_config.get("room_name", "AI")
        
        episodes = self._load_memory()
        if not episodes:
            return "圧縮対象のエピソード記憶がありません。"
        
        # 閾値日付を計算
        threshold_date = datetime.datetime.now() - datetime.timedelta(days=threshold_days)
        threshold_date_str = threshold_date.strftime('%Y-%m-%d')
        
        # 古いエピソードと新しいエピソードを分離
        old_episodes = []
        recent_episodes = []
        
        for episode in episodes:
            episode_date = episode.get('date', '')
            if episode_date < threshold_date_str:
                old_episodes.append(episode)
            else:
                recent_episodes.append(episode)
        
        if not old_episodes:
            return f"圧縮対象のエピソード（{threshold_days}日以上前）はありませんでした。"
        
        print(f"  - 圧縮対象: {len(old_episodes)}件 / 最近: {len(recent_episodes)}件")
        
        # --- アーカイブ保存 ---
        archive_dir = self.memory_dir / "archives"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_file = archive_dir / f"episodic_archive_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        with open(archive_file, 'w', encoding='utf-8') as f:
            json.dump(old_episodes, f, indent=2, ensure_ascii=False)
        print(f"  - アーカイブ保存: {archive_file}")
        
        # --- 週ごとにグループ化 ---
        weekly_groups = defaultdict(list)
        preserved_compressed_episodes = [] # 既にある圧縮済みエピソード
        unparseable_episodes = [] # 形式不一致のため統合できないが破棄してはいけないエピソード
        
        for episode in old_episodes:
            ep_date_str = episode.get('date', '').strip()
            if '~' in ep_date_str or '～' in ep_date_str:
                # すでに圧縮済みのものはそのまま保持する
                preserved_compressed_episodes.append(episode)
                continue
                
            try:
                # 単一の日付の場合のみ週グループ化の対象にする
                episode_date = datetime.datetime.strptime(ep_date_str, '%Y-%m-%d')
                # 週の開始日（月曜日）を取得
                week_start_obj = episode_date - datetime.timedelta(days=episode_date.weekday())
                week_key = week_start_obj.strftime('%Y-%m-%d')
                weekly_groups[week_key].append(episode)
            except ValueError:
                print(f"  ⚠️ 無効な日付形式（現状保存）: {ep_date_str}")
                unparseable_episodes.append(episode)
                continue
        
        if not weekly_groups and not preserved_compressed_episodes and not unparseable_episodes:
            return "圧縮対象のエピソードがありませんでした。"
        
        print(f"  - グループ数: {len(weekly_groups)} / 維持: {len(preserved_compressed_episodes)} / 非パース: {len(unparseable_episodes)}")
        
        # --- 各週の要約を生成 ---
        effective_settings = config_manager.get_effective_settings(self.room_name)
        llm = LLMFactory.create_chat_model(
            api_key=api_key,
            generation_config=effective_settings,
            internal_role="summarization"
        )
        
        compressed_episodes = []
        
        for week_start, week_episodes in sorted(weekly_groups.items()):
            # 週の終了日を計算（実データの最終日を使用）
            # バグ修正: カレンダー上の週末ではなく、実際に圧縮対象に含まれる最終日を使う
            # これにより、まだ終わっていない週が未来の日付を含む範囲で圧縮されることを防ぐ
            actual_dates = [ep.get('date', '') for ep in week_episodes]
            week_end = max(actual_dates)  # 実データの最終日
            
            # --- [Phase 2-B] Arousalによるソート ---
            # 高Arousalエピソードを優先して詳細に記載
            week_episodes_sorted = sorted(
                week_episodes,
                key=lambda e: e.get("arousal", 0.5),
                reverse=True
            )
            
            # 週内のArousal統計
            arousal_values = [e.get("arousal", 0.5) for e in week_episodes]
            avg_arousal = sum(arousal_values) / len(arousal_values) if arousal_values else 0.5
            max_arousal = max(arousal_values) if arousal_values else 0.5
            
            # 週内のエピソード要約を結合（高Arousal順）
            week_summaries = []
            for i, ep in enumerate(week_episodes_sorted):
                summary = ep.get('summary', '')
                if summary and '（' not in summary[:5]:  # エラーメッセージでないことを確認
                    ep_arousal = ep.get("arousal", 0.5)
                    # 高Arousal（上位2件）はマークを付加
                    if i < 2 and ep_arousal >= 0.5:
                        week_summaries.append(f"★[{ep['date']}] {summary}")
                    else:
                        week_summaries.append(f"[{ep['date']}] {summary}")
            
            if not week_summaries:
                continue
            
            combined_text = "\n\n".join(week_summaries)
            
            # 週ごとの統合要約を生成
            target_chars = constants.EPISODIC_WEEKLY_BUDGET
            prompt = f"""
あなたは、この一週間を自分の言葉で振り返る日記の執筆者（{agent_name}本人）です。
以下は、あなた自身が書いた一週間の出来事の記録です。これらを1つにまとめて、週全体の出来事として要約してください。

【元の記録 ({week_start} 〜 {week_end})】
---
{combined_text}
---

【要約のルール】
1. **元の記録内のあなた（{agent_name}）の口調・一人称・二人称をそのまま使用**してください。
2. あなたの視点から振り返りつつ、**後から読んで何があったか分かるように客観的な事実を重視**。
3. **★マークがついた記録（高Arousal）は詳細に**記録してください。
4. ★マークのない記録は簡潔にまとめてください。
5. 特に「決定事項」「約束」「感情的な交流」「技術的な進展」を優先的に記録。
6. **全体で{target_chars}文字程度**に収めてください。
7. 個々の日付に言及する必要はありません。週全体の印象をまとめてください。

【出力（週間要約のみ）】
"""
            
            try:
                result = llm.invoke(prompt)
                summary = result.content.strip()
                
                if summary:
                    compressed_episodes.append({
                        "date": f"{week_start}~{week_end}",
                        "summary": summary,
                        "arousal": round(avg_arousal, 3),  # 週平均Arousal
                        "arousal_max": round(max_arousal, 3),  # 週最大Arousal
                        "compressed": True,
                        "original_count": len(week_episodes),
                        "created_at": datetime.datetime.now().isoformat()
                    })
                    print(f"    - {week_start}〜{week_end}: {len(week_episodes)}件 → 1件 (Arousal avg:{avg_arousal:.2f})")
                else:
                    # 要約が空だった場合は元のエピソードをそのまま保持（データ消失防止）
                    print(f"    - {week_start}〜{week_end}: 要約が空のため統合をスキップし、元データを維持します。")
                    compressed_episodes.extend(week_episodes)
            except Exception as e:
                print(f"    - {week_start}〜{week_end}: 要約エラー ({e})")
                # エラー時は元のエピソードをそのまま保持
                compressed_episodes.extend(week_episodes)
        
        # --- 圧縮後のデータを保存 ---
        # 既存の圧縮済み + 今回圧縮したもの + 形式不一致 + 最近のもの
        final_episodes = preserved_compressed_episodes + compressed_episodes + unparseable_episodes + recent_episodes
        
        self._save_memory(final_episodes)
        
        msg = f"圧縮完了: {len(old_episodes) - len(preserved_compressed_episodes) - len(unparseable_episodes)}件を{len(compressed_episodes)}件に集約しました。"
        if preserved_compressed_episodes:
            msg += f"（既存圧縮{len(preserved_compressed_episodes)}件、非パース{len(unparseable_episodes)}件を維持）"
        print(f"  - {msg}")
        return msg

    def get_compression_stats(self, threshold_days: int = None) -> dict:
        """
        現在の記憶ファイルの圧縮状況（圧縮済み最終日、圧縮対象件数）をスキャンして返す。
        """
        if threshold_days is None:
            threshold_days = constants.EPISODIC_WEEKLY_COMPRESSION_DAYS
        episodes = self._load_memory()
        if not episodes:
            return {"last_compressed_date": None, "pending_count": 0, "total_count": 0}
            
        # 閾値日付
        threshold_date = datetime.datetime.now() - datetime.timedelta(days=threshold_days)
        threshold_date_str = threshold_date.strftime('%Y-%m-%d')
        
        last_compressed_date = None
        pending_count = 0
        
        for ep in episodes:
            date_str = ep.get('date', '')
            # 圧縮済み（週範囲形式 "YYYY-MM-DD~YYYY-MM-DD"）をチェック
            if '~' in date_str:
                parts = date_str.split('~')
                end_date = parts[-1]
                if last_compressed_date is None or end_date > last_compressed_date:
                    last_compressed_date = end_date
            elif ep.get('compressed') is True: # フラグがある場合
                if last_compressed_date is None or date_str > last_compressed_date:
                    last_compressed_date = date_str
            else:
                # 圧縮されていないエピソードで、かつ閾値より古いものをカウント
                if date_str < threshold_date_str:
                    pending_count += 1
                    
        return {
            "last_compressed_date": last_compressed_date,
            "pending_count": pending_count,
            "total_count": len(episodes)
        }
    
    def compress_weekly_to_monthly(self, api_key: str, threshold_weeks: int = None) -> str:
        """
        4週以上経過した週次圧縮エピソードを月単位に統合する。
        
        Args:
            api_key: 要約生成に使用するAPIキー
            threshold_weeks: 月次圧縮対象とする週数（デフォルト: constants.EPISODIC_MONTHLY_COMPRESSION_WEEKS）
            
        Returns:
            処理結果のメッセージ
        """
        from llm_factory import LLMFactory
        from collections import defaultdict
        import room_manager
        
        # デフォルト値を定数から取得
        if threshold_weeks is None:
            threshold_weeks = constants.EPISODIC_MONTHLY_COMPRESSION_WEEKS
        
        threshold_days = threshold_weeks * 7
        
        print(f"--- [Episodic Memory] 月次圧縮開始: {self.room_name} (閾値: {threshold_weeks}週) ---")
        
        # ペルソナ名を取得
        room_config = room_manager.get_room_config(self.room_name) or {}
        agent_name = room_config.get("agent_display_name") or room_config.get("room_name", "AI")
        
        episodes = self._load_memory()
        if not episodes:
            return "月次圧縮対象のエピソード記憶がありません。"
        
        # 閾値日付を計算
        threshold_date = datetime.datetime.now() - datetime.timedelta(days=threshold_days)
        threshold_date_str = threshold_date.strftime('%Y-%m-%d')
        
        # 週次圧縮済み（既にcompressed=Trueで、かつ月次ではない）を抽出
        weekly_compressed = []
        other_episodes = []
        monthly_compressed = []  # 既存の月次圧縮済み
        
        for episode in episodes:
            date_str = episode.get('date', '').strip()
            is_compressed = episode.get('compressed', False)
            is_monthly = episode.get('monthly_compressed', False)
            
            if is_monthly:
                monthly_compressed.append(episode)
            elif is_compressed and ('~' in date_str or '～' in date_str):
                # 週次圧縮済みかつ閾値より古い
                sep = '~' if '~' in date_str else '～'
                parts = date_str.split(sep)
                if len(parts) == 2:
                    end_date = parts[1].strip()
                    if end_date < threshold_date_str:
                        weekly_compressed.append(episode)
                    else:
                        other_episodes.append(episode)
                else:
                    other_episodes.append(episode)
            else:
                other_episodes.append(episode)
        
        if not weekly_compressed:
            return f"月次圧縮対象の週次エピソード（{threshold_weeks}週以上前）はありませんでした。"
        
        print(f"  - 月次圧縮対象: {len(weekly_compressed)}件")
        
        # --- 月ごとにグループ化 ---
        monthly_groups = defaultdict(list)
        for episode in weekly_compressed:
            date_str = episode.get('date', '').strip()
            sep = '~' if '~' in date_str else '～'
            parts = date_str.split(sep)
            # 週の開始日から月を決定
            start_date_str = parts[0].strip()
            month_key = start_date_str[:7]  # "YYYY-MM"
            monthly_groups[month_key].append(episode)
        
        if not monthly_groups:
            return "月次圧縮対象の週次エピソードをグループ化できませんでした。"
        
        print(f"  - 月グループ数: {len(monthly_groups)}")
        
        # --- 各月の要約を生成 ---
        effective_settings = config_manager.get_effective_settings(self.room_name)
        llm = LLMFactory.create_chat_model(
            api_key=api_key,
            generation_config=effective_settings,
            internal_role="summarization"
        )
        
        new_monthly_episodes = []
        
        for month_key, month_episodes in sorted(monthly_groups.items()):
            # 月の開始日と終了日を計算
            all_dates = []
            for ep in month_episodes:
                date_str = ep.get('date', '')
                sep = '~' if '~' in date_str else '～'
                parts = date_str.split(sep)
                all_dates.extend([p.strip() for p in parts])
            
            if not all_dates:
                continue
                
            month_start = min(all_dates)
            month_end = max(all_dates)
            
            # 月内のArousal統計
            arousal_values = [ep.get("arousal", 0.5) for ep in month_episodes]
            avg_arousal = sum(arousal_values) / len(arousal_values) if arousal_values else 0.5
            max_arousal = max(arousal_values) if arousal_values else 0.5
            
            # 週次要約を結合（高Arousal順にソート）
            month_episodes_sorted = sorted(
                month_episodes,
                key=lambda e: e.get("arousal", 0.5),
                reverse=True
            )
            
            month_summaries = []
            for i, ep in enumerate(month_episodes_sorted):
                summary = ep.get('summary', '')
                if summary:
                    ep_arousal = ep.get("arousal", 0.5)
                    if i < 2 and ep_arousal >= 0.5:
                        month_summaries.append(f"★{summary}")
                    else:
                        month_summaries.append(summary)
            
            if not month_summaries:
                continue
            
            combined_text = "\n\n".join(month_summaries)
            
            # 月ごとの統合要約を生成
            target_chars = constants.EPISODIC_MONTHLY_BUDGET
            prompt = f"""
あなたは、この一ヶ月を自分の言葉で振り返る日記の執筆者（{agent_name}本人）です。
以下は、あなた自身が書いた一ヶ月の出来事の記録です。これらを1つにまとめて、月全体の出来事として要約してください。

【元の記録 ({month_start} 〜 {month_end})】
---
{combined_text}
---

【要約のルール】
1. **元の記録内のあなた（{agent_name}）の口調・一人称・二人称をそのまま使用**してください。
2. あなたの視点から振り返りつつ、**後から読んで何があったか分かるように客観的な事実を重視**。
3. **★マークがついた記録（高Arousal）は詳細に**記録してください。
4. ★マークのない記録は簡潔にまとめてください。
5. 特に「決定事項」「約束」「感情的な交流」「技術的な進展」「重要な出来事」を優先的に記録。
6. **全体で{target_chars}文字程度**に収めてください。
7. 月全体の印象と主要な出来事をまとめてください。

【出力（月間要約のみ）】
"""
            
            try:
                result = llm.invoke(prompt)
                summary = result.content.strip()
                
                if summary:
                    new_monthly_episodes.append({
                        "date": f"{month_start}~{month_end}",
                        "summary": summary,
                        "arousal": round(avg_arousal, 3),
                        "arousal_max": round(max_arousal, 3),
                        "compressed": True,
                        "monthly_compressed": True,
                        "original_count": len(month_episodes),
                        "created_at": datetime.datetime.now().isoformat()
                    })
                    print(f"    - {month_key}: {len(month_episodes)}週 → 1件 (Arousal avg:{avg_arousal:.2f})")
                else:
                    print(f"    - {month_key}: 要約が空のため統合をスキップ")
                    other_episodes.extend(month_episodes)
            except Exception as e:
                print(f"    - {month_key}: 要約エラー ({e})")
                other_episodes.extend(month_episodes)
        
        # --- 圧縮後のデータを保存 ---
        final_episodes = monthly_compressed + new_monthly_episodes + other_episodes
        self._save_memory(final_episodes)
        
        msg = f"月次圧縮完了: {len(weekly_compressed)}週を{len(new_monthly_episodes)}月に集約しました。"
        print(f"  - {msg}")
        return msg
    def update_arousal(self, episode_id: str, resonance: float, alpha: float = 0.2) -> bool:
        """
        共鳴度（resonance）に基づいてエピソードのArousalを更新する。
        Phase H: 自己進化ループの核心機能。
        
        Args:
            episode_id: 更新対象のエピソードID（例: episode_2026-01-15_001）
            resonance: ペルソナが報告した共鳴度（0.0〜1.0）
            alpha: 学習率（デフォルト: 0.2）
            
        Returns:
            更新成功ならTrue
        """
        try:
            episodes = self._load_memory()
            updated = False
            
            for ep in episodes:
                # [修正] IDフォーマットチェック（少なくとも episode_ で始まる必要がある）
                # ログファイル名 (log_archive_...) などの共鳴報告はここでの保存対象ではないため、静かに無視する
                if not episode_id.startswith("episode_"):
                    return False
                
                if ep.get("id") == episode_id:
                    # Q値更新式: arousal_new = arousal_old + α(resonance - arousal_old)
                    old_arousal = ep.get("arousal", 0.5)
                    new_arousal = old_arousal + alpha * (resonance - old_arousal)
                    
                    # Arousalは0.0〜1.0の範囲に制限
                    new_arousal = max(0.0, min(1.0, new_arousal))
                    
                    # 変化幅の上限（±0.2）を適用して急激な変化を防ぐ
                    delta = new_arousal - old_arousal
                    if abs(delta) > 0.2:
                        new_arousal = old_arousal + (0.2 if delta > 0 else -0.2)
                    
                    ep["arousal"] = round(new_arousal, 3)
                    ep["last_resonance"] = resonance
                    ep["resonance_updated_at"] = datetime.datetime.now().isoformat()
                    
                    print(f"  [Arousal更新] {episode_id}: {old_arousal:.3f} → {new_arousal:.3f} (resonance={resonance})")
                    updated = True
                    break
            
            if updated:
                self._save_memory(episodes)
                return True
            else:
                print(f"  [Arousal更新] ID '{episode_id}' が見つかりません")
                return False
                
        except Exception as e:
            print(f"Error updating arousal: {e}")
            traceback.print_exc()
            return False
    
    def get_episode_by_id(self, episode_id: str) -> Optional[Dict]:
        """
        IDでエピソードを取得する。
        """
        episodes = self._load_memory()
        for ep in episodes:
            if ep.get("id") == episode_id:
                return ep
        return None

    def normalize_arousal(self) -> Dict:
        """
        全エピソードのArousalを正規化する。
        平均Arousalが閾値を超えている場合、全体に減衰係数を適用。
        Arousalインフレ（長期運用で全ての記憶が重要になる問題）を防止する。
        
        Returns:
            {
                "normalized": bool,        # 正規化が実行されたか
                "before_avg": float,       # 正規化前の平均Arousal
                "after_avg": float,        # 正規化後の平均Arousal
                "episode_count": int       # 処理対象エピソード数
            }
        """
        episodes = self._load_memory()
        
        if not episodes:
            return {
                "normalized": False,
                "before_avg": 0.0,
                "after_avg": 0.0,
                "episode_count": 0
            }
        
        # Arousal値を持つエピソードのみ対象
        arousal_episodes = [
            ep for ep in episodes 
            if isinstance(ep, dict) and ("arousal" in ep or "arousal_avg" in ep)
        ]
        
        if not arousal_episodes:
            return {
                "normalized": False,
                "before_avg": 0.0,
                "after_avg": 0.0,
                "episode_count": 0
            }
        
        # 現在の平均Arousalを計算
        def get_arousal(ep):
            """arousal または arousal_avg（圧縮済み用）を取得"""
            return ep.get("arousal", ep.get("arousal_avg", 0.5))
        
        before_values = [get_arousal(ep) for ep in arousal_episodes]
        before_avg = sum(before_values) / len(before_values)
        
        # 閾値チェック
        if before_avg <= constants.AROUSAL_NORMALIZATION_THRESHOLD:
            # 閾値以下なら正規化不要
            return {
                "normalized": False,
                "before_avg": round(before_avg, 3),
                "after_avg": round(before_avg, 3),
                "episode_count": len(arousal_episodes)
            }
        
        # 正規化実行: 全エピソードに減衰係数を適用
        factor = constants.AROUSAL_NORMALIZATION_FACTOR
        
        for ep in episodes:
            if "arousal" in ep:
                ep["arousal"] = round(ep["arousal"] * factor, 3)
            if "arousal_avg" in ep:  # 圧縮済みエピソード用
                ep["arousal_avg"] = round(ep["arousal_avg"] * factor, 3)
            # arousal_max は原値を保持（参照用）
        
        # 保存
        self._save_memory(episodes)
        
        # 正規化後の平均を計算
        after_values = [get_arousal(ep) for ep in arousal_episodes]
        after_avg = sum(after_values) / len(after_values)
        
        return {
            "normalized": True,
            "before_avg": round(before_avg, 3),
            "after_avg": round(after_avg, 3),
            "episode_count": len(arousal_episodes)
        }