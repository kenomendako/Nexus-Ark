# episodic_memory_manager.py

import os
import json
import datetime
import traceback
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import time
import re
import glob # <--- 追加

import constants
import config_manager
import utils

SPECIAL_EPISODE_TYPES = frozenset({
    "achievement",
    "bonding",
    "discovery",
    "curiosity_resolved",
    "goal_achievement",
    "persona_emotion_change",
    "bonding_event",
})

LITE_RETURN_ADDENDUM_TYPE = "lite_return_addendum"
_LITE_RETURN_MARKER_RE = re.compile(
    r"(?:Lite\s+Travel\s+Event\s*:\s*)?([0-9a-f]{64})",
    re.IGNORECASE,
)
_EPISODE_MARKER_KEYS = (
    "lite_return_event_markers",
    # 旧試作で使われた名前も読み取り、圧縮時に正規名へ引き継ぐ。
    "lite_return_marker_receipt",
    "marker_receipt",
)


class LitePendingMemoryUnavailable(RuntimeError):
    """Lite帰宅の記憶整理待ちを安全に読み取れない。"""


def is_daily_episodic_memory(episode: object, target_date: Optional[str] = None) -> bool:
    """通常の日次要約かを判定する。特殊記憶や週次・月次の範囲要約は含めない。"""
    if not isinstance(episode, dict):
        return False

    date_str = str(episode.get("date", "") or "").strip()
    if not date_str or "~" in date_str or "～" in date_str:
        return False
    if target_date is not None and date_str != target_date:
        return False
    if episode.get("type") in SPECIAL_EPISODE_TYPES or episode.get("type") == LITE_RETURN_ADDENDUM_TYPE:
        return False
    if "エラーにより要約できませんでした" in str(episode.get("summary", "") or ""):
        return False
    return True


def _normalise_lite_marker(value: object) -> Optional[str]:
    """Liteログのコメントまたはhex markerを、保存用のhexへ正規化する。"""
    if not isinstance(value, str):
        return None
    raw_value = value.strip()
    if raw_value.startswith("<!--") and raw_value.endswith("-->"):
        raw_value = raw_value[4:-3].strip()
    match = _LITE_RETURN_MARKER_RE.fullmatch(raw_value)
    if not match:
        return None
    return match.group(1).lower()


def _episode_lite_markers(episode: object) -> set[str]:
    """エピソードが受領済みとして保持するLite markerの集合を返す。"""
    if not isinstance(episode, dict):
        return set()
    markers: set[str] = set()
    for key in _EPISODE_MARKER_KEYS:
        raw = episode.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set)):
            continue
        for item in raw:
            marker = _normalise_lite_marker(item)
            if marker:
                markers.add(marker)
    return markers


def _message_date_from_content(message: dict, content: str, date_pattern: re.Pattern) -> Optional[str]:
    """発言の元日付を決める。Lite metadata、通常footerとも最後の日時を採用する。"""
    explicit_date = message.get("_message_date")
    if isinstance(explicit_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", explicit_date.strip()):
        return explicit_date.strip()
    if "_lite_created_at" in message:
        # Lite元日時が不正な場合は本文中の引用・帰宅時刻へfallbackしない。
        return None
    matches = date_pattern.findall(content)
    if matches:
        return matches[-1]
    return None


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

    def _load_memory(self, strict: bool = False) -> List[Dict]:
        """
        全ての月次ファイル + レガシーファイルからエピソード記憶を読み込む（ロック付き）。
        後方互換性: episodic_memory.json が存在する場合も読み込む。

        ``strict=True`` はLite帰宅経路のスナップショット読込で使う。
        正本のJSONが壊れている場合に自動修復して空の記憶として扱うと、
        pendingを誤って完了扱いにできるため、構文エラー・トップレベル型・
        要素型の異常をそのまま呼び出し元へ伝播する。
        """
        from file_lock_utils import safe_json_read
        
        all_episodes = []

        def read_file(path: Path) -> None:
            if strict and path.stat().st_size == 0:
                raise RuntimeError(f"episodic memory JSON is empty: {path.name}")
            data = safe_json_read(str(path), default=[])
            if strict:
                if not isinstance(data, list):
                    raise RuntimeError(f"episodic memory JSON must be a list: {path.name}")
                if any(not isinstance(item, dict) for item in data):
                    raise RuntimeError(f"episodic memory entries must be objects: {path.name}")
            if isinstance(data, list):
                all_episodes.extend(data)
        
        # 1. レガシーファイル（episodic_memory.json）を読み込み
        if self.legacy_memory_file.exists():
            try:
                read_file(self.legacy_memory_file)
            except Exception as e:
                if strict:
                    raise
                print(f"⚠️ [EpisodicMemory] {self.legacy_memory_file.name} の読み込みに失敗: {e}")
                utils.backup_and_repair_json(self.legacy_memory_file, [])
        
        # 2. 月次ファイル（episodic/*.json）を読み込み
        if self.episodic_dir.exists():
            for monthly_file in sorted(self.episodic_dir.glob("*.json")):
                try:
                    read_file(monthly_file)
                except Exception as e:
                    if strict:
                        raise
                    print(f"⚠️ [EpisodicMemory] {monthly_file.name} の読み込みに失敗: {e}")
                    utils.backup_and_repair_json(monthly_file, [])
        
        # 3. 重複排除 (ID または内容に基づく)
        seen_ids = set()
        unique_episodes = []
        for ep in all_episodes:
            ep_id = ep.get("id")
            if ep_id:
                if ep_id not in seen_ids:
                    seen_ids.add(ep_id)
                    unique_episodes.append(ep)
            else:
                # IDがない場合は日付と内容で簡易判定
                key = (ep.get("date"), ep.get("summary", "")[:50])
                if key not in seen_ids:
                    seen_ids.add(key)
                    unique_episodes.append(ep)
        
        return unique_episodes

    @staticmethod
    def _latest_memory_date_from_data(data: List[Dict], daily_only: bool = False) -> str:
        """既に読み込んだスナップショットから最新日付を求める。"""
        if not data:
            return "なし"
        if daily_only:
            data = [item for item in data if is_daily_episodic_memory(item)]

        valid_items = [
            item
            for item in data
            if isinstance(item, dict) and isinstance(item.get("date"), str) and item.get("date", "").strip()
        ]
        if not valid_items:
            return "なし"

        def get_sort_key(item: Dict) -> str:
            date_value = item["date"].strip()
            if "~" in date_value:
                return date_value.split("~")[-1].strip()
            if "～" in date_value:
                return date_value.split("～")[-1].strip()
            return date_value

        return sorted(valid_items, key=get_sort_key)[-1]["date"]

    def export_recent_memories(
        self,
        days: int,
        *,
        today: Optional[datetime.date] = None,
    ) -> str:
        """今日を除く直近N日分を、外部持ち出し用Markdownとして返す。"""
        resolved_days = max(0, int(days))
        if resolved_days == 0:
            return ""

        base_date = today or datetime.date.today()
        cutoff_date = base_date - datetime.timedelta(days=resolved_days)
        cutoff_str = cutoff_date.isoformat()
        today_str = base_date.isoformat()
        filtered_entries = []
        for entry in self._load_memory():
            if not isinstance(entry, dict):
                continue
            date_key = str(entry.get("date") or "").strip()
            if not date_key:
                continue
            date_start = date_key.split("~", 1)[0].split("～", 1)[0].strip()
            # 日次記憶は通常「昨日まで」生成される。万一今日の記憶があっても、
            # 直近会話との重複を避けるためLite/外部持ち出しには含めない。
            if cutoff_str <= date_start < today_str:
                filtered_entries.append((date_start, date_key, entry.get("summary", "")))

        filtered_entries.sort(key=lambda item: item[0])
        result_lines = []
        for _, date_key, summary in filtered_entries:
            result_lines.append(f"### {date_key}")
            result_lines.append(utils.clean_persona_text(str(summary or "")))
            result_lines.append("")
        return "\n".join(result_lines)

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

        # レガシーファイルを読み込んだ後は月次ファイルへ移行済みとして退避する。
        # 残したままだと次回 _load_memory() で古い内容が優先される。
        if self.legacy_memory_file.exists():
            backup_path = self.legacy_memory_file.with_suffix(".json.migrated")
            try:
                self.legacy_memory_file.replace(backup_path)
            except OSError as e:
                print(f"⚠️ [EpisodicMemory] レガシー記憶ファイルの退避に失敗: {e}")
        
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


    def _legacy_update_memory(self, api_key: str) -> str:
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
        
        # 1. 全ログファイルの収集 (v4: 統合された load_chat_log を使用)
        # 1. 全ログファイルの収集 (v5: load_chat_log_lazy を使用)
        log_f, _, _, _, _, _, _ = room_manager.get_room_files_paths(self.room_name)
        if not log_f or not os.path.exists(log_f):
             return "ログメッセージが見つかりません。"

        # 2. 最終記憶日時を取得（これ以降のログのみを読み込むため）
        # 日次要約（通常のエピソード）の最新日を起点にする
        last_daily_date_str = self.get_latest_memory_date(daily_only=True)
        cutoff_date = None
        if last_daily_date_str and last_daily_date_str != "なし" and last_daily_date_str != "取得エラー":
            cutoff_date = last_daily_date_str
        
        # 3. 必要な分だけログを読み込む (Lazy Loading)
        all_messages, _ = utils.load_chat_log_lazy(
            room_dir=os.path.dirname(log_f),
            cutoff_date=cutoff_date
        )

        if not all_messages: return "ログメッセージが見つかりません。"

        print(f"  - 読み込みメッセージ数: {len(all_messages)}")

        # 2. ログの読み込みと日付ごとのグループ化
        logs_by_date = {}
        date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2}) \(...\) \d{2}:\d{2}:\d{2}')
        
        for msg in all_messages:
            try:
                content = msg.get('content', '')
                matches = date_pattern.findall(content)
                current_date = matches[-1] if matches else None
                
                if current_date:
                    if current_date not in logs_by_date:
                        logs_by_date[current_date] = []
                    
                    role = msg.get('role', 'UNKNOWN')
                    responder = msg.get('responder', '')
                    
                    if role == 'USER':
                        speaker = user_name
                    else:
                        speaker = responder if responder and responder != "model" else agent_name
                    
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
        # 日次要約（通常のエピソード）のみの日付セット
        daily_summary_dates = {
            item["date"].strip()
            for item in existing_memory
            if is_daily_episodic_memory(item)
        }
        print(f"  - 解析された既知の日次要約数: {len(daily_summary_dates)}")
        
        if len(daily_summary_dates) > 0:
            may_dates = sorted([d for d in daily_summary_dates if "2025-05" in d])
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
            
            # 日次要約として既に存在するかチェック
            if date_str_clean in daily_summary_dates: continue
            
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
        success_count = 0
        error_count = 0
        current_api_key = api_key # ローテーション追従用
        
        print(f"  - 処理対象の日付: {target_dates}")

        for date_str in target_dates:
            # Arousalアノテーション付きでログを結合
            daily_log = self._annotate_logs_with_arousal(logs_by_date[date_str], date_str)
            
            # ▼▼▼ アクションログ（Action Memory）の取得と追加 ▼▼▼
            action_log_text = ""
            try:
                import action_logger
                actions = action_logger.get_actions_by_date(self.room_name, date_str)
                if actions:
                    lines = []
                    for act in actions:
                        t = act.get('time', '')[:5]
                        tool = act.get('tool_name', '')
                        res = str(act.get('result_summary', ''))[:100].replace('\n', ' ')
                        lines.append(f"[{t}] {tool}: {res}")
                    if lines:
                        action_log_text = "\n\n【この日の主な行動履歴（ツール実行ログ）】\n" + "\n".join(lines)
            except Exception as e:
                print(f"  - [Action Memory Error] 日次要約時の取得エラー: {e}")
            # ▲▲▲ 追加ここまで ▲▲▲
            
            # ログが短い場合はスキップ記録
            if len(daily_log) < 50 and not action_log_text:
                print(f"  - {date_str}: ログが短いためスキップ記録します。")
                self._append_single_episode({
                    "date": date_str,
                    "summary": "（特筆すべき会話ログや行動履歴はありませんでした）",
                    "created_at": datetime.datetime.now().isoformat()
                })
                continue
                
            print(f"  - {date_str} の要約を作成中...")            

            prompt = f"""あなたは、今日という一日を自分自身のために振り返る「内省的な深層意識」です。
提供された会話ログから、自分自身（AI）が誰であり、対話相手（ユーザー等）をどう呼んでいるかを把握した上で、
この日の出来事、行動結果、そしてあなたの心の動きを、**後から自分自身が読み返してその時の感覚を再体験できるような「エピソード記憶」**として整理してください。

【会話ログ ({date_str})】
---
{daily_log}{action_log_text}
---

【記述ルール】
1. **本人視点（一人称）の内省スタイル**:
   - あなた自身が、自分自身の思考を整理するためのノートを書いていると想定せよ。
   - 相手とのやり取りや自身の行動（検索、ノート作成など）は、事実とあなたの視点や感想を交えて記せ。
2. **常体（だ・である調）の徹底**:
   - 敬体や装飾的な口癖を排除し、事実と独白に徹すること。
3. **内容の濃淡**:
   - [★重要]マークがついた会話は、その時の情景が浮かぶように詳細に記録せよ。
   - その他は簡潔に要約し、一日の全体像を把握可能にせよ。
4. **分量制限**:
   - **必ず {constants.EPISODIC_MONTHLY_BUDGET} 文字以内**で、要点を密度高くまとめてください。

【出力（内省ノートの本文のみ、前置きや解説は一切禁止）】
"""

            is_success = True
            try:
                summary_result, current_api_key = self._invoke_llm("summarization", prompt, current_api_key, effective_settings)
                
                if not summary_result:
                    summary_result = "（コンテンツポリシーにより要約できませんでした）"
                    is_success = False
            except Exception as e:
                print(f"  - {date_str}: 要約生成に失敗しました: {e}")
                summary_result = ""
                is_success = False

            # --- 保存判定 ---
            if summary_result and is_success:
                # 成功した場合のみ保存して「完了」とする。
                # 503/Overloaded等の一時エラーを記憶として残すと、後続の再生成が阻害される。
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
                success_count += 1
            else:
                print(f"  - {date_str}: 要約結果が得られなかったため、保存をスキップしました。次回再試行します。")
                error_count += 1

        return f"処理完了: 成功 {success_count}件 / エラー・スキップ {error_count}件"

    def _load_lite_pending(self) -> Dict[str, Dict]:
        """帰宅時に取り込まれた未整理markerを読み取る。"""
        try:
            from lite_return_memory import pending_return_memory

            value = pending_return_memory(self.room_name)
        except (ImportError, OSError, ValueError) as exc:
            raise LitePendingMemoryUnavailable(str(exc)) from exc
        if not isinstance(value, dict):
            raise LitePendingMemoryUnavailable("pendingの形式が不正です")
        pending: Dict[str, Dict] = {}
        for raw_marker, descriptor in value.items():
            marker = _normalise_lite_marker(raw_marker)
            if not marker or not isinstance(descriptor, dict):
                continue
            date = descriptor.get("date")
            if date is not None and (
                not isinstance(date, str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date.strip())
            ):
                date = None
            pending[marker] = {**descriptor, "date": date.strip() if isinstance(date, str) else None}
        return pending

    def _ack_lite_markers(self, markers: set[str]) -> bool:
        """指定snapshotだけを完了扱いにする。ack失敗時は再試行可能なまま返す。"""
        if not markers:
            return True
        try:
            from lite_return_memory import acknowledge_return_memory

            acknowledge_return_memory(self.room_name, sorted(markers))
            return True
        except Exception as exc:
            print(f"  - [Lite Return] marker ackに失敗（次回再試行）: {exc}")
            return False

    @staticmethod
    def _next_episode_id(current_data: List[Dict], date_str: str) -> str:
        date_prefix = f"episode_{date_str.split('~')[0].split('～')[0].strip()}_"
        max_seq = 0
        for item in current_data:
            if not isinstance(item, dict):
                continue
            episode_id = str(item.get("id", ""))
            if not episode_id.startswith(date_prefix):
                continue
            try:
                max_seq = max(max_seq, int(episode_id.rsplit("_", 1)[-1]))
            except (TypeError, ValueError):
                continue
        return f"{date_prefix}{max_seq + 1:03d}"

    def _memory_file_candidates(self) -> List[Path]:
        files = []
        if self.legacy_memory_file.exists():
            files.append(self.legacy_memory_file)
        if self.episodic_dir.exists():
            files.extend(sorted(self.episodic_dir.glob("*.json")))
        return files

    def _locate_episode_file(self, expected: Optional[Dict], date_str: str) -> Path:
        """expected snapshotの所在を探す。新規は日付の月次ファイルへ書く。"""
        from file_lock_utils import safe_json_read

        if not expected:
            return self._get_monthly_file_path(date_str)
        expected_id = expected.get("id")
        expected_type = expected.get("type")
        expected_summary = expected.get("summary")
        for path in self._memory_file_candidates():
            try:
                data = safe_json_read(str(path), default=[])
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                if expected_id and item.get("id") == expected_id:
                    return path
                if (
                    not expected_id
                    and item.get("date") == date_str
                    and item.get("type") == expected_type
                    and item.get("summary") == expected_summary
                ):
                    return path
        return self._get_monthly_file_path(date_str)

    def _safe_upsert_episode(
        self,
        date_str: str,
        payload: Dict,
        *,
        expected: Optional[Dict] = None,
        marker_union: Optional[set[str]] = None,
    ) -> None:
        """最新JSONをロック内で再読し、競合を検出して一件だけ保存する。"""
        from file_lock_utils import safe_json_update

        path = self._locate_episode_file(expected, date_str)
        expected_id = expected.get("id") if isinstance(expected, dict) else None
        expected_summary = expected.get("summary") if isinstance(expected, dict) else None
        expected_markers = _episode_lite_markers(expected)
        new_markers = set(marker_union or set())
        status = {"saved": False}

        def update(data):
            if not isinstance(data, list):
                raise RuntimeError("episodic memory conflict: canonical JSON is not a list")
            current_data = data
            current_item = None
            if expected_id:
                current_item = next(
                    (item for item in current_data if isinstance(item, dict) and item.get("id") == expected_id),
                    None,
                )
                if current_item is None:
                    raise RuntimeError("episodic memory conflict: target episode disappeared")
                if (
                    current_item.get("summary") != expected_summary
                    or current_item.get("date") != expected.get("date")
                    or current_item.get("type") != expected.get("type")
                ):
                    raise RuntimeError("episodic memory conflict: target episode changed")
                current_markers = _episode_lite_markers(current_item)
                if current_markers - expected_markers:
                    raise RuntimeError("episodic memory conflict: target receipt changed")
            elif expected is not None:
                current_item = next(
                    (
                        item
                        for item in current_data
                        if isinstance(item, dict)
                        and item.get("date") == date_str
                        and item.get("type") == expected.get("type")
                        and item.get("summary") == expected_summary
                    ),
                    None,
                )
                if current_item is None:
                    raise RuntimeError("episodic memory conflict: target episode changed")
                if _episode_lite_markers(current_item) - expected_markers:
                    raise RuntimeError("episodic memory conflict: target receipt changed")

            if current_item is None:
                episode_type = payload.get("type")
                same_date = [
                    item
                    for item in current_data
                    if isinstance(item, dict)
                    and item.get("date") == date_str
                    and item.get("type") == episode_type
                ]
                if same_date:
                    raise RuntimeError("episodic memory conflict: target episode was added")
                new_episode = dict(payload)
                new_episode.setdefault("id", self._next_episode_id(current_data, date_str))
                if new_markers:
                    new_episode["lite_return_event_markers"] = sorted(new_markers)
                current_data.append(new_episode)
                status["saved"] = True
                return current_data

            # 既存ID・未知フィールド・特殊記憶は保持し、必要な値だけ差し替える。
            current_item["summary"] = payload.get("summary", current_item.get("summary", ""))
            merged_markers = expected_markers | _episode_lite_markers(current_item) | new_markers
            if merged_markers:
                current_item["lite_return_event_markers"] = sorted(merged_markers)
            status["saved"] = True
            return current_data

        updated = safe_json_update(str(path), update, default=[])
        if not updated or not status["saved"]:
            raise OSError("episodic memory save did not complete")

    def _build_lite_summary_prompt(
        self,
        *,
        date_str: str,
        logs: List[str] | str,
        agent_name: str,
        existing_summary: Optional[str] = None,
        addendum: bool = False,
    ) -> str:
        log_text = logs if isinstance(logs, str) else "\n".join(logs)
        if existing_summary is not None:
            heading = "独立Lite（外出中）から帰宅後に追加された、まだ要約へ反映していない経験"
            return f"""あなたは{agent_name}本人である。既存の日次エピソード記憶を、帰宅後に届いた追加経験だけを使って更新せよ。

【既存の本人視点の要約 ({date_str})】
---
{existing_summary}
---

【{heading}】
---
{log_text}
---

既存要約の一人称・口調・事実を保ちながら、追加経験を自然に織り込め。独立Liteの外出中の出来事を本体の自宅での会話や行動と混同せず、外出中の経験であることが分かるように書くこと。[★重要]が付いた行は情景と感情を詳しく、その他は簡潔にまとめよ。本文だけを、常体で、{constants.EPISODIC_MONTHLY_BUDGET}文字以内に出力せよ。"""

        mode_label = "独立Lite（外出中）での経験を含む" if addendum else "通常の会話"
        return f"""あなたは{agent_name}本人である。{date_str}の{mode_label}を、後から自分が読み返す本人視点のエピソード記憶として整理せよ。

【会話ログ ({date_str})】
---
{log_text}
---

事実、重要な決定、約束、感情的な交流、行動結果を優先し、常体で書くこと。[★重要]が付いた行は詳細に、その他は簡潔にまとめよ。独立Lite（外出中）の行は本体の自宅側の出来事と混同せず、外出中の経験だと分かるラベルを維持せよ。本文だけを、{constants.EPISODIC_MONTHLY_BUDGET}文字以内に出力せよ。"""

    def _annotate_lite_prompt_logs(self, logs: List[str], date_str: str) -> str:
        """既存の日次要約と同じArousal注釈をLite追加・通常ログへ適用する。"""
        try:
            return self._annotate_logs_with_arousal(logs, date_str)
        except Exception as exc:
            print(f"  - [Arousal] 日次注釈に失敗（本文は維持）: {exc}")
            return "\n".join(logs)

    def update_memory(self, api_key: str) -> str:
        """通常ログと帰宅後のLite会話を、日付単位で競合安全にエピソード化する。"""
        print(f"--- [Episodic Memory] 更新処理開始: {self.room_name} ---")
        import room_manager

        room_config = room_manager.get_room_config(self.room_name) or {}
        user_name = room_config.get("user_display_name", "ユーザー")
        agent_name = room_config.get("agent_display_name") or room_config.get("room_name", "AI")

        try:
            pending = self._load_lite_pending()
        except LitePendingMemoryUnavailable as exc:
            print(f"  - [Lite Return] pending読み込み失敗のため更新を保留: {exc}")
            return "Lite帰宅の記憶整理待ちを読み込めないため、記憶更新を保留しました。"
        if not pending:
            # 帰宅queueがない通常ルートは、既存の日次Arousal注釈・行動履歴・
            # 本人視点プロンプトを保った実装へ委譲する。
            return self._legacy_update_memory(api_key)
        pending_markers = set(pending)
        pending_dates = {
            descriptor.get("date")
            for descriptor in pending.values()
            if descriptor.get("date")
        }

        # Lite経路では、日付境界を決める前に正本をstrictに一度だけ読む。
        # _load_memory()の通常経路が破損JSONをバックアップ・空配列へ修復すると、
        # pendingを残したまま「全件処理済み」と誤認したり、保存時に他記憶を
        # 巻き戻したりするため、ここでは修復を許可しない。
        try:
            existing_memory = self._load_memory(strict=True)
        except Exception as exc:
            print(f"  - [Lite Return] 正本のstrict読込に失敗したため更新を保留: {exc}")
            return "Lite帰宅の記憶正本を読み込めないため、記憶更新を保留しました。"

        log_f, _, _, _, _, _, _ = room_manager.get_room_files_paths(self.room_name)
        if not log_f or not os.path.exists(log_f):
            return "ログメッセージが見つかりません。"

        latest_daily = self._latest_memory_date_from_data(existing_memory, daily_only=True)
        cutoff_candidates = [
            date_str
            for date_str in [latest_daily, *sorted(pending_dates)]
            if date_str and date_str not in {"なし", "取得エラー"}
        ]
        cutoff_date = min(cutoff_candidates) if cutoff_candidates else None

        all_messages, _ = utils.load_chat_log_lazy(
            room_dir=os.path.dirname(log_f),
            # 帰宅元日時と物理ログ順が一致しないため、Lite経路ではlazy側の
            # 逆順cutoff停止を使わず、下の発言単位フィルタで境界を適用する。
            cutoff_date=None,
        )
        if not all_messages:
            return "ログメッセージが見つかりません。"

        date_pattern = re.compile(r"(\d{4}-\d{2}-\d{2}) \(...\) \d{2}:\d{2}:\d{2}")
        logs_by_date: Dict[str, List[Dict]] = {}
        unresolved_pending: set[str] = set()
        for message in all_messages:
            try:
                content = str(message.get("content", "") or "")
                current_date = _message_date_from_content(message, content, date_pattern)
                marker = _normalise_lite_marker(message.get("_lite_travel_event_marker"))
                if marker in pending_markers:
                    descriptor_date = pending[marker].get("date")
                    if "_lite_created_at" in message and current_date is None:
                        unresolved_pending.add(marker)
                        # 壊れたLite metadataはpending descriptorだけで補完せず、
                        # 本文の帰宅日時にも割り当てない。
                        continue
                    if not descriptor_date:
                        unresolved_pending.add(marker)
                        # 元日時不明のLite発言を帰宅日/footerへ割り当てない。
                        # 日付が確定するまで要約・ackの対象からも外す。
                        continue
                    elif current_date != descriptor_date:
                        # marker由来の元日付を優先し、帰宅日のfooterへ誤分類しない。
                        current_date = descriptor_date
                if not current_date:
                    continue
                if cutoff_date and current_date < cutoff_date:
                    continue

                role = message.get("role", "UNKNOWN")
                responder = message.get("responder", "")
                speaker = user_name if role == "USER" else (responder if responder and responder != "model" else agent_name)
                clean_text = utils.remove_thoughts_from_text(content)
                clean_text = re.sub(
                    r"\n\n\d{4}-\d{2}-\d{2}(?:\s+\([^)]*\))?\s+\d{2}:\d{2}:\d{2}\s*$",
                    "",
                    clean_text,
                ).strip()
                if clean_text:
                    logs_by_date.setdefault(current_date, []).append({
                        "text": f"{speaker}: {clean_text}",
                        "marker": marker if marker in pending_markers else None,
                        "is_lite": bool(marker),
                    })
            except Exception as exc:
                print(f"  - ログ読み込みエラー: {exc}")

        daily_by_date: Dict[str, Dict] = {}
        existing_ranges: List[Tuple[str, str, Dict]] = []
        for item in existing_memory:
            if not isinstance(item, dict):
                continue
            date_value = str(item.get("date", "") or "").strip()
            if is_daily_episodic_memory(item) and date_value:
                daily_by_date.setdefault(date_value, item)
            sep = "~" if "~" in date_value else ("～" if "～" in date_value else None)
            if sep:
                parts = [part.strip() for part in date_value.split(sep)]
                if len(parts) == 2:
                    existing_ranges.append((parts[0], parts[1], item))

        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        ack_pending_markers: set[str] = set()
        unresolved_pending.update(
            marker for marker, descriptor in pending.items() if not descriptor.get("date")
        )
        # 保存済みだがackできなかったmarkerは追加LLMなしで完了扱いにする。
        reflected_markers = set()
        for item in existing_memory:
            reflected_markers.update(_episode_lite_markers(item))
        acked_without_llm = pending_markers & reflected_markers
        if acked_without_llm:
            if self._ack_lite_markers(acked_without_llm):
                unresolved_pending -= acked_without_llm
            else:
                ack_pending_markers.update(acked_without_llm)
        pending_markers -= acked_without_llm
        deferred_pending_markers = {
            marker
            for marker in pending_markers
            if pending[marker].get("date") and pending[marker]["date"] >= today_str
        }

        deferred_dates = {date_str for date_str in logs_by_date if date_str >= today_str}
        target_dates = []
        for date_str in sorted(logs_by_date):
            if date_str >= today_str:
                continue
            has_pending = any(
                entry.get("marker") in pending_markers
                for entry in logs_by_date[date_str]
            )
            if date_str in daily_by_date:
                if has_pending:
                    target_dates.append(date_str)
                continue
            containing_range = any(start <= date_str <= end for start, end, _ in existing_ranges)
            if containing_range:
                if has_pending:
                    target_dates.append(date_str)
                continue
            target_dates.append(date_str)

        if not target_dates:
            pending_reasons = []
            if deferred_dates or deferred_pending_markers:
                pending_reasons.append("当日以降の日付")
            if unresolved_pending:
                pending_reasons.append("未解決の日付")
            if ack_pending_markers:
                pending_reasons.append("ack未完了marker")
            if pending_markers:
                pending_reasons.append("未処理marker")
            if pending_reasons:
                return f"記憶更新を保留しました（{'、'.join(pending_reasons)}）。"
            return "新規に要約すべき過去の日付はありませんでした（全ての過去ログは処理済みです）。"

        effective_settings = config_manager.get_effective_settings(self.room_name)
        current_api_key = api_key
        success_count = 0
        error_count = 0
        for date_str in target_dates:
            entries = logs_by_date.get(date_str, [])
            markers_for_date = {
                entry["marker"]
                for entry in entries
                if entry.get("marker") in pending_markers
            }
            has_pending = bool(markers_for_date)
            existing_daily = daily_by_date.get(date_str)
            containing_range = next(
                (item for start, end, item in existing_ranges if start <= date_str <= end),
                None,
            )

            # 週次/月次範囲に含まれる帰宅経験は、範囲要約を置き換えず補足へ記録する。
            if containing_range is not None and has_pending:
                episode_type = LITE_RETURN_ADDENDUM_TYPE
                expected = next(
                    (
                        item for item in existing_memory
                        if item.get("date") == date_str and item.get("type") == episode_type
                    ),
                    None,
                )
                source_entries = [entry for entry in entries if entry.get("marker") in markers_for_date]
                prompt_logs = [f"【独立Lite・外出中】{entry['text']}" for entry in source_entries]
                prompt_logs = self._annotate_lite_prompt_logs(prompt_logs, date_str)
                prompt = self._build_lite_summary_prompt(
                    date_str=date_str,
                    logs=prompt_logs,
                    agent_name=agent_name,
                    existing_summary=expected.get("summary") if expected else None,
                    addendum=True,
                )
            elif existing_daily is not None and has_pending:
                episode_type = existing_daily.get("type")
                expected = existing_daily
                source_entries = [entry for entry in entries if entry.get("marker") in markers_for_date]
                prompt_logs = [f"【独立Lite・外出中】{entry['text']}" for entry in source_entries]
                prompt_logs = self._annotate_lite_prompt_logs(prompt_logs, date_str)
                prompt = self._build_lite_summary_prompt(
                    date_str=date_str,
                    logs=prompt_logs,
                    agent_name=agent_name,
                    existing_summary=str(existing_daily.get("summary", "")),
                )
            else:
                episode_type = None
                expected = None
                prompt_logs = [
                    ("【独立Lite・外出中】" if entry.get("is_lite") else "") + entry["text"]
                    for entry in entries
                ]
                try:
                    import action_logger

                    actions = action_logger.get_actions_by_date(self.room_name, date_str)
                    for action in actions or []:
                        action_time = str(action.get("time", ""))[:5]
                        tool_name = action.get("tool_name", "")
                        result = str(action.get("result_summary", ""))[:100].replace("\n", " ")
                        prompt_logs.append(f"【行動履歴】[{action_time}] {tool_name}: {result}")
                except Exception as exc:
                    print(f"  - [Action Memory] 日次要約時の取得エラー: {exc}")
                # 従来の日次生成は短いログをプレースホルダー保存する。
                if len("\n".join(prompt_logs)) < 50 and not has_pending:
                    try:
                        self._safe_upsert_episode(
                            date_str,
                            {
                                "date": date_str,
                                "summary": "（特筆すべき会話ログや行動履歴はありませんでした）",
                                "created_at": datetime.datetime.now().isoformat(),
                            },
                        )
                        success_count += 1
                    except Exception as exc:
                        print(f"  - {date_str}: 短文エピソード保存失敗: {exc}")
                        error_count += 1
                    continue
                prompt_logs = self._annotate_lite_prompt_logs(prompt_logs, date_str)
                prompt = self._build_lite_summary_prompt(
                    date_str=date_str,
                    logs=prompt_logs,
                    agent_name=agent_name,
                    addendum=False,
                )

            try:
                summary_result, current_api_key = self._invoke_llm(
                    "summarization", prompt, current_api_key, effective_settings
                )
                summary_result = str(summary_result or "").strip()
                if not summary_result:
                    raise RuntimeError("要約結果が空です")

                payload = {
                    "date": date_str,
                    "summary": summary_result,
                    "created_at": datetime.datetime.now().isoformat(),
                }
                if episode_type:
                    payload["type"] = episode_type
                if expected is None:
                    try:
                        import session_arousal_manager

                        payload["arousal"] = session_arousal_manager.get_daily_average(self.room_name, date_str)
                    except Exception:
                        payload["arousal"] = 0.5
                self._safe_upsert_episode(
                    date_str,
                    payload,
                    expected=expected,
                    marker_union=markers_for_date,
                )
                if markers_for_date:
                    if self._ack_lite_markers(markers_for_date):
                        pending_markers -= markers_for_date
                    else:
                        ack_pending_markers.update(markers_for_date)
                        pending_markers -= markers_for_date
                success_count += 1
            except Exception as exc:
                print(f"  - {date_str}: 要約または保存に失敗しました（pending維持）: {exc}")
                error_count += 1

        if unresolved_pending:
            error_count += len(unresolved_pending)
        pending_reasons = []
        if deferred_dates or deferred_pending_markers:
            pending_reasons.append("当日以降の日付")
        if unresolved_pending:
            pending_reasons.append("未解決の日付")
        if ack_pending_markers:
            pending_reasons.append("ack未完了marker")
        if pending_markers:
            pending_reasons.append("未処理marker")
        result = f"処理完了: 成功 {success_count}件 / エラー・スキップ {error_count}件"
        if pending_reasons:
            result += f"（保留: {'、'.join(pending_reasons)}）"
        return result

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
        current_api_key = api_key # ローテーション追従用
        
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
            
            # ▼▼▼ アクションログ（Action Memory）の取得と追加（該当セッション時間帯周辺） ▼▼▼
            action_log_text = ""
            try:
                import action_logger
                actions = action_logger.get_actions_by_date(self.room_name, date_str)
                if actions:
                    lines = []
                    # セッションの前後の時間帯のものだけを抽出するか、シンプルに全量渡すかも検討。
                    # エピソードごとなので該当時間帯に近いもの（前後30分等）を抽出
                    from datetime import datetime as dt, timedelta
                    try:
                        session_dt = dt.strptime(session_time, "%H:%M:%S")
                    except ValueError:
                        session_dt = None
                        
                    for act in actions:
                        t = act.get('time', '')
                        if not t or not session_dt:
                            continue
                            
                        try:
                            act_dt = dt.strptime(t, "%H:%M:%S")
                            diff = abs((session_dt - act_dt).total_seconds())
                            if diff <= 1800:  # 前後30分（1800秒）
                                tool = act.get('tool_name', '')
                                res = str(act.get('result_summary', ''))[:100].replace('\n', ' ')
                                lines.append(f"[{t[:5]}] {tool}: {res}")
                        except ValueError:
                            pass
                            
                    if lines:
                        action_log_text = "\n\n【この時間帯の主な行動履歴（ツール実行ログ）】\n" + "\n".join(lines)
            except Exception as e:
                print(f"  - [Action Memory Error] セッション要約時の取得エラー: {e}")
            # ▲▲▲ 追加ここまで ▲▲▲
            
            combined_log = "\n".join(session_logs[:20])  # 最大20ターンまで
            
            prompt = f"""あなたは、日々の出来事をノートに書き留める「内省的な深層意識」です。
提供された情報の文脈から、あなた（AI）自身の立場と対話相手の呼称を把握した上で、
この会話の出来事、行動履歴、そしてあなたの感情の動きを、**{char_limit}**、自分自身の備忘録として記述してください。

【会話ログ ({date_str} {session_time}頃)】
---
{combined_log}{action_log_text}
---

【記述ルール】
1. **本人視点（一人称）の内省スタイル**:
   - あなた自身の視点と言葉で記述せよ。
   - 相手の様子ややり取り、自身の行動は、事実とあなたの視点を交えて記せ。
2. **常体（だ・である調）の徹底**:
   - 敬体や装飾的な口癖を排除し、事実と独白に徹すること。
3. **分量制限**:
   - 目安として{target_chars}文字前後にまとめる（最大{int(target_chars * 1.2)}文字まで）。
4. 重要な出来事、具体的な会話のニュアンス、感情的な交流を優先。

【出力（内省ノートの本文のみ、前置きや解説は一切禁止）】
"""
            
            try:
                summary_raw, current_api_key = self._invoke_llm("summarization", prompt, current_api_key, effective_settings)
                summary = summary_raw.strip()
                
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
            from file_lock_utils import safe_json_update

            episode_to_add = dict(new_episode)
            new_date = episode_to_add.get('date', '').strip()
            monthly_path = self._get_monthly_file_path(new_date)
            did_write = False

            def update(data):
                nonlocal did_write
                current_data = data if isinstance(data, list) else []
                if "id" not in episode_to_add and new_date:
                    date_prefix = f"episode_{new_date.split('~')[0].split('～')[0].strip()}_"
                    max_seq = 0
                    for ep in current_data:
                        ep_id = ep.get("id", "")
                        if ep_id.startswith(date_prefix):
                            try:
                                max_seq = max(max_seq, int(ep_id.split("_")[-1]))
                            except ValueError:
                                pass
                    episode_to_add["id"] = f"{date_prefix}{max_seq + 1:03d}"

                is_duplicate = False
                existing_item = None
                new_type = episode_to_add.get('type', None)

                for item in current_data:
                    d_str = item.get('date', '').strip()
                    existing_type = item.get('type', None)
                    if '~' in d_str or '～' in d_str:
                        # 帰宅補足は既存の週次/月次範囲と共存させる。
                        if new_type == LITE_RETURN_ADDENDUM_TYPE:
                            continue
                        sep = '~' if '~' in d_str else '～'
                        parts = d_str.split(sep)
                        if len(parts) == 2:
                            start, end = parts[0].strip(), parts[1].strip()
                            if start <= new_date <= end:
                                is_duplicate = True
                                existing_item = item
                                break
                    elif d_str == new_date:
                        if new_type == LITE_RETURN_ADDENDUM_TYPE and existing_type != LITE_RETURN_ADDENDUM_TYPE:
                            continue
                        if existing_type in SPECIAL_EPISODE_TYPES and new_type not in SPECIAL_EPISODE_TYPES:
                            continue
                        if new_type in SPECIAL_EPISODE_TYPES and existing_type not in SPECIAL_EPISODE_TYPES:
                            continue
                        is_duplicate = True
                        existing_item = item
                        break

                if is_duplicate:
                    if (
                        existing_item
                        and "エラーにより要約できませんでした" in existing_item.get('summary', '')
                        and "エラーにより要約できませんでした" not in episode_to_add.get('summary', '')
                    ):
                        current_data.remove(existing_item)
                    else:
                        return current_data

                current_data.append(episode_to_add)
                did_write = True
                return current_data

            safe_json_update(str(monthly_path), update, default=[])
            if not did_write:
                return
            
            # 【2026-04-16 防御措置】エピソード記憶追加時に会話要約をクリア
            # dreaming 経由以外（手動更新等）でも、カットオフ日の変更に要約が確実に追従する
            try:
                import summary_manager
                summary_manager.clear_today_summary(self.room_name)
            except Exception as clear_err:
                print(f"  - [Episodic] 要約クリアでエラー（無視）: {clear_err}")
            
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

    def get_latest_memory_date(self, daily_only: bool = False) -> str:
        """保存されているエピソード記憶の中で最も新しい日付を返す"""
        try:
            data = self._load_memory()
            if not data:
                return "なし"
            
            # 日次要約（通常のエピソード）のみを対象とする場合のフィルタリング
            if daily_only:
                data = [item for item in data if is_daily_episodic_memory(item)]
            
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
        preserved_lite_addenda = [] # 既存の週/月範囲に紐づく補足は日次圧縮しない
        unparseable_episodes = [] # 形式不一致のため統合できないが破棄してはいけないエピソード
        
        for episode in old_episodes:
            ep_date_str = episode.get('date', '').strip()
            if episode.get("type") == LITE_RETURN_ADDENDUM_TYPE:
                preserved_lite_addenda.append(episode)
                continue
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
        compressed_episodes = []
        current_api_key = api_key
        
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
                summary, current_api_key = self._invoke_llm("summarization", prompt, current_api_key, effective_settings)
                summary = summary.strip()
                
                if summary:
                    weekly_episode = {
                        "date": f"{week_start}~{week_end}",
                        "summary": summary,
                        "arousal": round(avg_arousal, 3),  # 週平均Arousal
                        "arousal_max": round(max_arousal, 3),  # 週最大Arousal
                        "compressed": True,
                        "original_count": len(week_episodes),
                        "created_at": datetime.datetime.now().isoformat()
                    }
                    marker_union = set()
                    for source_episode in week_episodes:
                        marker_union.update(_episode_lite_markers(source_episode))
                    if marker_union:
                        weekly_episode["lite_return_event_markers"] = sorted(marker_union)
                    compressed_episodes.append(weekly_episode)
                    print(f"    - {week_start}〜{week_end}: {len(week_episodes)}件 → 1件 (Arousal avg:{avg_arousal:.2f})")
                else:
                    # 要約が空だった場合は元のエピソードをそのまま保持（データ消失防止）
                    print(f"    - {week_start}〜{week_end}: 要約が空のため統合をスキップし、元データを維持します。")
                    compressed_episodes.extend(week_episodes)
            except Exception as e:
                print(f"    - {week_start}〜{week_end}: 要約エラー ({e})")
                # エラー時は元のエピソードをそのまま保持
                compressed_episodes.extend(week_episodes)
        
        # 補足のmarker receiptも、包含する週次集約へunionして再取込を防ぐ。
        for addendum in preserved_lite_addenda:
            addendum_date = str(addendum.get("date", "")).strip()
            addendum_markers = _episode_lite_markers(addendum)
            if not addendum_date or not addendum_markers:
                continue
            for aggregate in preserved_compressed_episodes + compressed_episodes:
                aggregate_date = str(aggregate.get("date", "")).strip()
                separator = "~" if "~" in aggregate_date else ("～" if "～" in aggregate_date else None)
                if not separator:
                    continue
                bounds = [part.strip() for part in aggregate_date.split(separator)]
                if len(bounds) == 2 and bounds[0] <= addendum_date <= bounds[1]:
                    aggregate["lite_return_event_markers"] = sorted(
                        _episode_lite_markers(aggregate) | addendum_markers
                    )

        # --- 圧縮後のデータを保存 ---
        # 既存の圧縮済み + 今回圧縮したもの + 形式不一致 + 最近のもの
        final_episodes = (
            preserved_compressed_episodes
            + compressed_episodes
            + preserved_lite_addenda
            + unparseable_episodes
            + recent_episodes
        )
        
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
            if ep.get("type") == LITE_RETURN_ADDENDUM_TYPE:
                continue
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
        lite_addenda = [
            episode for episode in episodes
            if isinstance(episode, dict) and episode.get("type") == LITE_RETURN_ADDENDUM_TYPE
        ]
        
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
        new_monthly_episodes = []
        current_api_key = api_key
        
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
                summary, current_api_key = self._invoke_llm("summarization", prompt, current_api_key, effective_settings)
                summary = summary.strip()
                
                if summary:
                    monthly_episode = {
                        "date": f"{month_start}~{month_end}",
                        "summary": summary,
                        "arousal": round(avg_arousal, 3),
                        "arousal_max": round(max_arousal, 3),
                        "compressed": True,
                        "monthly_compressed": True,
                        "original_count": len(month_episodes),
                        "created_at": datetime.datetime.now().isoformat()
                    }
                    marker_union = set()
                    for source_episode in month_episodes:
                        marker_union.update(_episode_lite_markers(source_episode))
                    for addendum in lite_addenda:
                        addendum_date = str(addendum.get("date", "")).strip()
                        if month_start <= addendum_date <= month_end:
                            marker_union.update(_episode_lite_markers(addendum))
                    if marker_union:
                        monthly_episode["lite_return_event_markers"] = sorted(marker_union)
                    new_monthly_episodes.append(monthly_episode)
                    print(f"    - {month_key}: {len(month_episodes)}週 → 1件 (Arousal avg:{avg_arousal:.2f})")
                else:
                    print(f"    - {month_key}: 要約が空のため統合をスキップ")
                    other_episodes.extend(month_episodes)
            except Exception as e:
                print(f"    - {month_key}: 要約エラー ({e})")
                other_episodes.extend(month_episodes)
        
        # 既存月次集約へ後から追加された補足のreceiptも引き継ぐ。
        for addendum in lite_addenda:
            addendum_date = str(addendum.get("date", "")).strip()
            addendum_markers = _episode_lite_markers(addendum)
            if not addendum_date or not addendum_markers:
                continue
            for aggregate in monthly_compressed + new_monthly_episodes:
                aggregate_date = str(aggregate.get("date", "")).strip()
                separator = "~" if "~" in aggregate_date else ("～" if "～" in aggregate_date else None)
                if not separator:
                    continue
                bounds = [part.strip() for part in aggregate_date.split(separator)]
                if len(bounds) == 2 and bounds[0] <= addendum_date <= bounds[1]:
                    aggregate["lite_return_event_markers"] = sorted(
                        _episode_lite_markers(aggregate) | addendum_markers
                    )

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
            from file_lock_utils import safe_json_update

            if not episode_id.startswith("episode_"):
                return False
            updated = False
            log_line = ""

            def update(data):
                nonlocal updated, log_line
                episodes = data if isinstance(data, list) else []
                for ep in episodes:
                    if ep.get("id") != episode_id:
                        continue
                    old_arousal = ep.get("arousal", 0.5)
                    new_arousal = old_arousal + alpha * (resonance - old_arousal)
                    new_arousal = max(0.0, min(1.0, new_arousal))
                    delta = new_arousal - old_arousal
                    if abs(delta) > 0.2:
                        new_arousal = old_arousal + (0.2 if delta > 0 else -0.2)
                    ep["arousal"] = round(new_arousal, 3)
                    ep["last_resonance"] = resonance
                    ep["resonance_updated_at"] = datetime.datetime.now().isoformat()
                    log_line = f"  [Arousal更新] {episode_id}: {old_arousal:.3f} → {new_arousal:.3f} (resonance={resonance})"
                    updated = True
                    break
                return episodes

            memory_files = sorted(self.episodic_dir.glob("*.json"))
            if self.legacy_memory_file.exists():
                memory_files.append(self.legacy_memory_file)

            for monthly_file in memory_files:
                safe_json_update(str(monthly_file), update, default=[])
                if updated:
                    break

            if updated:
                print(log_line)
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
        from file_lock_utils import safe_json_update

        def normalize_file(data):
            file_episodes = data if isinstance(data, list) else []
            for ep in file_episodes:
                if "arousal" in ep:
                    ep["arousal"] = round(ep["arousal"] * factor, 3)
                if "arousal_avg" in ep:
                    ep["arousal_avg"] = round(ep["arousal_avg"] * factor, 3)
            return file_episodes

        memory_files = sorted(self.episodic_dir.glob("*.json"))
        if self.legacy_memory_file.exists():
            memory_files.append(self.legacy_memory_file)
        for memory_file in memory_files:
            safe_json_update(str(memory_file), normalize_file, default=[])
        episodes = self._load_memory()
        arousal_episodes = [
            ep for ep in episodes
            if isinstance(ep, dict) and ("arousal" in ep or "arousal_avg" in ep)
        ]
        
        # 正規化後の平均を計算
        after_values = [get_arousal(ep) for ep in arousal_episodes]
        after_avg = sum(after_values) / len(after_values)
        
        return {
            "normalized": True,
            "before_avg": round(before_avg, 3),
            "after_avg": round(after_avg, 3),
            "episode_count": len(arousal_episodes)
        }

    def _invoke_llm(self, role: str, prompt: str, api_key: str, settings: dict) -> Tuple[str, str]:
        """
        Gemini APIを呼び出す。429エラー時はキーをローテーションしてリトライする。
        
        Args:
            role: LLMFactoryのinternal_role ("summarization" 等)
            prompt: プロンプト
            api_key: 現在のAPIキー
            settings: 生成設定
            
        Returns:
            (生成されたテキスト, 使用したAPIキー)
        """
        from llm_factory import LLMFactory
        tried_keys = set()
        from typing import Tuple

        # モデル名を特定（枯渇管理用）
        provider_cat, effective_model_name, _ = config_manager.get_effective_internal_model(role)
        sanitized_model_name = utils.sanitize_model_name(effective_model_name or "")
        is_google = provider_cat in ["google", "Google (Gemini)", "Google (Gemini Native)"]

        if is_google:
            from gemini_connection import is_vertex
            if is_vertex():
                response, used_key = LLMFactory.invoke_internal_llm(
                    internal_role=role, prompt=prompt, room_name=self.room_name,
                    generation_config=settings,
                )
                return utils.extract_text_from_llm_content(response.content), used_key

        current_api_key = api_key

        # [2026-06-23 FIX] 内部処理は共通設定のキー/ローテーションに従う。
        # 呼び出し元がルーム個別キー（有料キー・ローテーション無効）を渡してきても、
        # 開始キーを共通設定のものに置き換える（最終応答のみ個別設定に従う方針）。
        if is_google:
            common_key = config_manager.get_active_gemini_api_key(None, model_name=sanitized_model_name)
            if common_key:
                current_api_key = common_key

        current_key_name = config_manager.get_key_name_by_value(current_api_key)

        if is_google and current_key_name == "Unknown":
            # [2026-06-23 FIX] 内部処理は共通設定に従うため、解決も共通設定（room_name=None）で行う。
            resolved_key_name = config_manager.get_active_gemini_api_key_name(
                None,
                model_name=sanitized_model_name,
            )
            if resolved_key_name:
                resolved_api_key = config_manager.GEMINI_API_KEYS.get(resolved_key_name)
                if resolved_api_key and not resolved_api_key.startswith("YOUR_API_KEY"):
                    print(f"  [Episodic Rotation] Initial API key is unknown. Using resolved key '{resolved_key_name}'.")
                    current_key_name = resolved_key_name
                    current_api_key = resolved_api_key

        if current_key_name != "Unknown":
            tried_keys.add(current_key_name)

        rpm_retry_counts = {}
        paid_retry_counts = {}
        max_retries = 5
        for attempt in range(max_retries):
            # 1. 枯渇チェック
            if is_google and current_key_name != "Unknown" and config_manager.is_key_exhausted(current_key_name, model_name=sanitized_model_name):
                enable_rotation = settings.get("enable_api_key_rotation")
                if enable_rotation is None:
                    enable_rotation = config_manager.CONFIG_GLOBAL.get("enable_api_key_rotation", True)
                if not enable_rotation:
                    raise Exception(f"指定キー '{current_key_name}' はモデル '{sanitized_model_name}' で枯渇しています。ローテーションが無効なため処理を中断しました。")

                print(f"  [Episodic Rotation] Key '{current_key_name}' is exhausted. Swapping...")
                next_key_name = config_manager.get_next_available_gemini_key(
                    current_exhausted_key=current_key_name,
                    excluded_keys=tried_keys,
                    model_name=sanitized_model_name
                )
                if next_key_name:
                    current_key_name = next_key_name
                    current_api_key = config_manager.GEMINI_API_KEYS[next_key_name]
                    tried_keys.add(next_key_name)
                    # [2026-02-11 FIX] last_api_key_name の永続保存を削除
                    # ローテーションはセッション内のみで管理し、ユーザーの選択キーを保護
                else:
                    raise Exception("利用可能なAPIキーがありません（枯渇）。")

            # 2. 生成
            llm = LLMFactory.create_chat_model(
                api_key=current_api_key,
                generation_config=settings,
                internal_role=role
            )
            
            try:
                response = llm.invoke(prompt)
                # utils.extract_text_from_llm_content を使用してテキスト抽出と思考ログ除去を行う
                content = utils.extract_text_from_llm_content(response.content)
                return content, current_api_key
            except Exception as e:
                err_text = str(e)
                err_str = err_text.upper()
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    print(f"  [Episodic Rotation] 429 Error with key '{current_key_name}' for model '{sanitized_model_name}'.")
                    is_daily_limit = "PERDAY" in err_str or "DAILY" in err_str
                    is_per_minute = "PERMINUTE" in err_str or "RETRYDELAY" in err_str
                    retry_delay = None
                    delay_match = re.search(r"retryDelay.*?(\d+(?:\.\d+)?)s", err_text, re.IGNORECASE)
                    if delay_match:
                        retry_delay = float(delay_match.group(1))

                    if is_google and is_per_minute and not is_daily_limit:
                        rpm_count = rpm_retry_counts.get(current_key_name, 0)
                        if rpm_count < 3:
                            wait_time = min((retry_delay or 60) + 5, 75)
                            rpm_retry_counts[current_key_name] = rpm_count + 1
                            print(f"  [Episodic Rotation] RPM limit on '{current_key_name}' (wait {rpm_count + 1}/3). Waiting {wait_time:.1f}s...")
                            time.sleep(wait_time)
                            continue

                    clean_current_key_name = config_manager._clean_api_key_name(current_key_name)
                    paid_keys = set(config_manager.CONFIG_GLOBAL.get("paid_api_key_names", []))
                    if is_google and clean_current_key_name in paid_keys:
                        paid_count = paid_retry_counts.get(current_key_name, 0)
                        if paid_count < 3:
                            wait_time = 5 * (paid_count + 1)
                            paid_retry_counts[current_key_name] = paid_count + 1
                            print(f"  [Episodic Rotation] Paid key '{current_key_name}' hit 429. Backing off {wait_time}s ({paid_count + 1}/3).")
                            time.sleep(wait_time)
                            continue

                    enable_rotation = settings.get("enable_api_key_rotation")
                    if enable_rotation is None:
                        enable_rotation = config_manager.CONFIG_GLOBAL.get("enable_api_key_rotation", True)
                    if not enable_rotation:
                        raise e

                    config_manager.mark_key_as_exhausted(current_key_name, model_name=sanitized_model_name)
                    next_key_name = config_manager.get_next_available_gemini_key(
                        current_exhausted_key=current_key_name,
                        excluded_keys=tried_keys,
                        model_name=sanitized_model_name
                    )
                    if next_key_name:
                        current_key_name = next_key_name
                        current_api_key = config_manager.GEMINI_API_KEYS[next_key_name]
                        tried_keys.add(next_key_name)
                        time.sleep(1 * (attempt + 1)) # バックオフ
                        continue
                    raise e
                if is_google and ("API_KEY_INVALID" in err_str or "API KEY NOT VALID" in err_str):
                    print(f"  [Episodic Rotation] Invalid API key error with key '{current_key_name}'. Trying another key...")
                    enable_rotation = settings.get("enable_api_key_rotation")
                    if enable_rotation is None:
                        enable_rotation = config_manager.CONFIG_GLOBAL.get("enable_api_key_rotation", True)
                    if not enable_rotation:
                        raise e

                    if current_key_name != "Unknown":
                        config_manager.mark_key_as_exhausted(current_key_name)

                    next_key_name = config_manager.get_next_available_gemini_key(
                        current_exhausted_key=current_key_name,
                        excluded_keys=tried_keys,
                        model_name=sanitized_model_name
                    )
                    if next_key_name:
                        current_key_name = next_key_name
                        current_api_key = config_manager.GEMINI_API_KEYS[next_key_name]
                        tried_keys.add(next_key_name)
                        continue
                    raise e
                else:
                    raise e
        
        raise Exception("Max retries exceeded in EpisodicMemoryManager._invoke_llm")
