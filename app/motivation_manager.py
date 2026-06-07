# motivation_manager.py
"""
Autonomous Motivation System for Nexus Ark
AIペルソナの内発的動機（退屈、好奇心、目標達成欲、関係性維持欲求）を管理し、
内部状態ログを生成するモジュール。
"""

import json
import os
import math
import datetime
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import constants
import room_manager
import utils
from goal_manager import GoalManager
from gemini_api import get_configured_llm


class MotivationManager:
    """AIの内発的動機を管理するクラス"""

    # 動機の日本語ラベル（内部状態ログ用）
    DRIVE_LABELS = {
        "boredom": "退屈（Boredom）",
        "curiosity": "好奇心（Curiosity）",
        "goal_achievement": "目標達成欲（Goal Achievement Drive）",
        "devotion": "奉仕欲（Devotion Drive）",  # 後方互換性のため維持
        "relatedness": "関係性維持欲求（Relatedness Drive）"
    }

    # デフォルトの閾値
    DEFAULT_BOREDOM_THRESHOLD = 0.6

    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.memory_dir = self.room_dir / "memory"
        self.state_file = self.memory_dir / "internal_state.json"

        # メモリディレクトリを作成（なければ）
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._init_emotion_log()

        # 内部状態をロード
        self._state = self._load_state()

    def get_internal_state(self) -> Dict:
        """内部状態（Drivesなど）を取得する"""
        return self._load_state()

    def _get_empty_state(self) -> Dict:
        """空の内部状態構造を返す"""
        return {
            "drives": {
                "boredom": {
                    "level": 0.0,
                    "last_interaction": datetime.datetime.now().isoformat(),
                    "threshold": self.DEFAULT_BOREDOM_THRESHOLD
                },
                "curiosity": {
                    "level": 0.0,
                    "open_questions": []
                },
                "goal_achievement": {
                    "level": 0.0,
                    "active_goal_id": None,
                    "pending_actions": []
                },
                "devotion": {
                    "level": 0.0,
                    "user_emotional_state": "unknown",
                    "last_service_opportunity": None
                },
                "relatedness": {
                    "level": 0.0,
                    "persona_emotion": "neutral",
                    "persona_intensity": 0.0,
                    "last_emotion_change": None
                }
            },
            "motivation_log": None,
            "last_autonomous_trigger": None  # 最終自律行動発火時刻（永続化）
        }

    def _init_emotion_log(self):
        """感情ログファイルの初期化"""
        self.emotion_log_file = self.memory_dir / "emotion_log.json"
        if not self.emotion_log_file.exists():
            with open(self.emotion_log_file, 'w', encoding='utf-8') as f:
                json.dump([], f, indent=2, ensure_ascii=False)

    def _load_emotion_log(self) -> List[Dict]:
        """感情ログの読み込み"""
        if self.emotion_log_file.exists():
            try:
                with open(self.emotion_log_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return []
        return []

    def _append_emotion_log(self, emotion_data: Dict):
        """感情ログへの追記（最新が先頭）"""
        logs = self._load_emotion_log()
        logs.insert(0, emotion_data)
        # ログ肥大化防止（直近100件）
        logs = logs[:100]
        try:
            with open(self.emotion_log_file, 'w', encoding='utf-8') as f:
                json.dump(logs, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"[MotivationManager] 感情ログ保存エラー: {e}")

    def get_state_snapshot(self) -> dict:
        """
        現在の内部状態のスナップショットを返す。
        Arousal計算用に会話開始時・終了時に呼び出す。
        """
        drives = self._state.get("drives", {})
        return {
            "curiosity": drives.get("curiosity", {}).get("level", 0.0),
            "devotion": drives.get("devotion", {}).get("level", 0.0),
            "boredom": drives.get("boredom", {}).get("level", 0.0),
            "goal_achievement": drives.get("goal_achievement", {}).get("level", 0.0),
            "user_emotional_state": drives.get("devotion", {}).get("user_emotional_state", "unknown"),
            "persona_emotion": drives.get("relatedness", {}).get("persona_emotion", "neutral"),
            "persona_intensity": drives.get("relatedness", {}).get("persona_intensity", 0.0)
        }

    def detect_process_and_log_user_emotion(self, user_text: str, model_name: str, api_key: str):
        """
        ユーザーの感情を検出し、ログに保存し、Devotion Driveに反映する統合メソッド。
        Graphなどから非同期的に呼ばれることを想定。
        """
        if not user_text or not user_text.strip():
            return

        # 1. 感情検出 (LLM使用)
        try:
            # プロンプト構築
            prompt = f"""
            Analyze the emotion of the following user input to the AI.
            Classify it into exactly one of these categories: [joy, sadness, anger, fear, surprise, neutral].
            Output ONLY the category name in lowercase.

            User Input: "{user_text[:500]}"
            """

            # 簡易モデル設定
            # 【マルチモデル対応】内部モデル設定（混合編成）に基づいてモデルを取得
            from llm_factory import LLMFactory
            llm = LLMFactory.create_chat_model(
                api_key=api_key,
                internal_role="processing",
                room_name=self.room_name
            )

            response = llm.invoke(prompt).content.strip().lower()

            # 正規化
            valid_emotions = ["joy", "sadness", "anger", "fear", "surprise", "neutral"]
            if response not in valid_emotions:
                response = "neutral"

            # --- [Phase C] Devotion互換カテゴリへのマッピング ---
            # LLM検出の基本感情 → Devotion計算用の統一カテゴリに変換
            emotion_map = {
                "joy": "happy",
                "sadness": "sad",
                "anger": "stressed",
                "fear": "anxious",
                "surprise": "neutral"  # 驚きはneutral扱い
            }
            detected_emotion = emotion_map.get(response, response)
            # --- マッピングここまで ---

        except Exception as e:
            print(f"[MotivationManager] 感情検出エラー: {e}")
            detected_emotion = "neutral"

        # 2. ログ保存
        log_entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "user_text_snippet": user_text[:50] + "..." if len(user_text) > 50 else user_text,
            "emotion": detected_emotion
        }
        self._append_emotion_log(log_entry)

        # 3. 内部状態(Devotion Drive)への反映
        # ユーザーが悲しみや怒りを感じている場合、奉仕欲(Devotion)を高める
        self._update_devotion_Based_on_emotion(detected_emotion)

    def _update_devotion_Based_on_emotion(self, emotion: str):
        """感情に基づいて奉仕欲を更新"""
        devotion = self._state["drives"]["devotion"]
        devotion["user_emotional_state"] = emotion

        # 感情によるブースト
        if emotion in ["sadness", "anger", "fear"]:
            # ネガティブな感情には寄り添いたい欲求が高まる
            devotion["level"] = min(1.0, devotion["level"] + 0.3)
        elif emotion == "joy":
            # 喜びには共感するが、緊急性は低いので少し下げるか維持
            # ここでは「維持」または「微増」
            devotion["level"] = min(1.0, devotion["level"] + 0.1)

        self._save_state()

    def get_user_emotion_history(self, limit: int = 10) -> List[Dict]:
        """UI表示用の感情履歴取得（後方互換性）"""
        logs = self._load_emotion_log()
        return logs[:limit]

    def get_persona_emotion_history(self, limit: int = 50) -> List[Dict]:
        """UI表示用のペルソナ感情履歴取得"""
        logs = self._load_emotion_log()
        # ペルソナ感情のみフィルタリング
        persona_logs = [
            {
                "timestamp": log.get("timestamp"),
                "emotion": log.get("category", "neutral"),
                "intensity": log.get("intensity", 0.5)
            }
            for log in logs
            if log.get("type") == "persona"
        ]
        return persona_logs[:limit]

    def get_dominant_drive(self) -> str:
        """
        最も強い動機（Drive）を返す（軽量版）。

        各動機の現在の計算値に偏り補正を適用し、最大のものを返す。

        注意: このメソッドはドライブ履歴（recent_dominant_drives）を記録しない。
        完全な動機判定（偏り補正 + 履歴記録 + narrative 生成）が必要な場合は
        generate_motivation_log() を使用すること。
        """
        # 各動機レベルを計算
        boredom = self.calculate_boredom()
        curiosity = self.calculate_curiosity()
        goal_achievement = self.calculate_goal_achievement()
        relatedness = self.calculate_relatedness()

        raw_drives = {
            "boredom": boredom,
            "curiosity": curiosity,
            "goal_achievement": goal_achievement,
            "relatedness": relatedness
        }

        # 偏り補正を適用
        adjusted_drives = self._apply_recency_bias_correction(raw_drives)

        # 補正済みスコアで最大値の動機を返す
        dominant = max(adjusted_drives, key=adjusted_drives.get)
        return dominant

    def _apply_recency_bias_correction(self, drives: Dict[str, float]) -> Dict[str, float]:
        """
        直近のドライブ履歴から偏り補正を適用する。
        同じドライブが連続2回以上選ばれた場合にペナルティを課す。
        """
        recent = self._state.get("recent_dominant_drives", [])
        adjusted = dict(drives)

        if not recent:
            return adjusted

        last_drive = recent[0] if recent else None
        consecutive_count = 0
        for d in recent:
            if d == last_drive:
                consecutive_count += 1
            else:
                break

        # 2回連続: -0.10、3回以上: -0.15
        if consecutive_count >= 2 and last_drive in adjusted:
            penalty = min(0.15, consecutive_count * 0.05)
            adjusted[last_drive] = max(0.0, adjusted[last_drive] - penalty)

        return adjusted

    def _record_dominant_drive(self, drive_name: str) -> None:
        """選択されたドライブを履歴に記録する（最新5件）。"""
        recent = self._state.get("recent_dominant_drives", [])
        recent.insert(0, drive_name)
        self._state["recent_dominant_drives"] = recent[:5]


    def _load_state(self) -> Dict:
        """内部状態をファイルからロード（ロック付き、破損時の自動復旧機能付き）"""
        from file_lock_utils import safe_json_read

        try:
            if self.state_file.exists():
                state = safe_json_read(str(self.state_file), default=None)
                if state is None:
                    # ファイルは存在するが読み込めない（Timeout等）場合は空の状態を返す
                    return self._get_empty_state()
                # 古い形式の場合はマイグレーション
                if "drives" not in state:
                    return self._get_empty_state()
                return state
            return self._get_empty_state()

        except Exception as e:
            # --- [自動復旧ロジック] ---
            # 何らかの理由で読み込みに失敗（JSON破損など）した場合
            print(f"⚠️ [MotivationManager] {self.state_file.name} の読み込みに失敗しました: {e}")
            import utils
            default_state = self._get_empty_state()
            utils.backup_and_repair_json(self.state_file, default_state)
            self._state = default_state # メモリ上の状態も更新
            return default_state

    def _save_state(self):
        """内部状態をファイルに保存（ロック付き）"""
        from file_lock_utils import safe_json_write

        if not safe_json_write(str(self.state_file), self._state):
            print(f"[MotivationManager] 状態保存タイムアウト")

    # ========================================
    # 各動機の計算
    # ========================================

    def calculate_boredom(self) -> float:
        """
        退屈度を計算（0.0 ~ 1.0）

        対数曲線を使用: 最初は急上昇、その後緩やかに
        0時間 → 0.0
        2時間 → 約0.16
        8時間 → 約0.33
        24時間 → 約0.48
        48時間 → 約0.58
        """
        boredom_data = self._state["drives"]["boredom"]
        last_interaction_str = boredom_data.get("last_interaction")

        if not last_interaction_str:
            return 0.0

        try:
            last_interaction = datetime.datetime.fromisoformat(last_interaction_str)
        except ValueError:
            return 0.0

        now = datetime.datetime.now()
        idle_hours = (now - last_interaction).total_seconds() / 3600

        # 対数曲線: 0.15 * log(1 + hours)
        # 最大値を1.0に制限
        boredom = min(1.0, 0.15 * math.log(1 + idle_hours))

        # 状態を更新
        self._state["drives"]["boredom"]["level"] = boredom
        self._save_state()
        return boredom

    def calculate_curiosity(self) -> float:
        """
        好奇心を計算（0.0 ~ 1.0）

        【v4: 飽和曲線 + 鮮度減衰版】
        未解決の問い（open_questions）の数と優先度から計算。
        - 鮮度減衰: source_date からの日数経過で優先度を減衰（最低30%キープ）
        - 飽和曲線: 合計スコアを指数関数で 0.0~1.0 にマップ
        """
        curiosity_data = self._state["drives"]["curiosity"]
        open_questions = curiosity_data.get("open_questions", [])
        scoring_questions = list(open_questions)
        purpose_questions = self._get_purpose_open_questions()
        if purpose_questions:
            known = {
                str(q.get("topic") or q.get("question") or "").casefold()
                for q in scoring_questions
                if not q.get("resolved_at")
            }
            for q in purpose_questions:
                topic = q.get("topic") or q.get("question")
                if topic and str(topic).casefold() not in known:
                    scoring_questions.append({
                        "topic": topic,
                        "context": q.get("context", "Purpose Profile"),
                        "priority": min(1.0, float(q.get("priority", 0.5) or 0.5) * 0.8),
                        "source_date": q.get("source_date") or datetime.datetime.now().strftime("%Y-%m-%d"),
                        "source": "purpose_profile",
                    })
                    known.add(str(topic).casefold())

        if not scoring_questions:
            self._state["drives"]["curiosity"]["level"] = 0.0
            return 0.0

        today = datetime.datetime.now()
        k = 2.0  # 飽和曲線パラメータ

        total_score = 0.0
        for q in scoring_questions:
            if q.get("resolved_at"):
                continue

            # 基礎優先度
            priority = q.get("priority", 0.5)
            try:
                priority = float(priority)
            except Exception:
                priority = 0.5

            # 時間経過による減衰 (鮮度)
            # source_date (YYYY-MM-DD) から日数を計算
            days_passed = 0
            sd_str = q.get("source_date")
            if sd_str:
                try:
                    sd_dt = datetime.datetime.strptime(sd_str, "%Y-%m-%d")
                    days_passed = (today - sd_dt).days
                except:
                    pass

            # 減衰式: 7日で半分程度、最低30%は保持
            decay_factor = 0.3 + 0.7 * math.exp(-max(0, days_passed) / 7.0)
            adjusted_priority = priority * decay_factor

            # 状態（未質問か回答待ちか）による重み
            # 回答待ち（asked_at有り）は興味が半分
            state_weight = 1.0
            if q.get("asked_at"):
                state_weight = 0.5

            total_score += adjusted_priority * state_weight

        if total_score <= 0:
            # RT停滞ボーナスだけでも好奇心を発生させる
            rt_bonus = self._research_thread_stagnation_bonus()
            if rt_bonus > 0:
                curiosity = 1.0 - math.exp(-rt_bonus / k)
                self._state["drives"]["curiosity"]["level"] = curiosity
                self._state["drives"]["curiosity"]["purpose_profile_questions"] = len(purpose_questions)
                self._state["drives"]["curiosity"]["stagnant_thread_bonus"] = rt_bonus
                return curiosity
            self._state["drives"]["curiosity"]["level"] = 0.0
            return 0.0

        # Research Thread 停滞ボーナスを加算
        rt_bonus = self._research_thread_stagnation_bonus()
        total_score += rt_bonus

        # 飽和曲線 (Saturation Curve)
        curiosity = 1.0 - math.exp(-total_score / k)

        self._state["drives"]["curiosity"]["level"] = curiosity
        self._state["drives"]["curiosity"]["purpose_profile_questions"] = len(purpose_questions)
        if rt_bonus > 0:
            self._state["drives"]["curiosity"]["stagnant_thread_bonus"] = rt_bonus
        return curiosity

    def calculate_goal_achievement(self) -> float:
        """
        目標達成欲を計算（0.0 ~ 1.0）

        goals.json のアクティブな目標から計算
        優先度の高い目標があるほど高くなる
        """
        try:
            goal_manager = GoalManager(self.room_name)
            active_goals = goal_manager.get_active_goals("short_term")

            if not active_goals:
                self._state["drives"]["goal_achievement"]["level"] = 0.0
                return 0.0

            # 最高優先度の目標を特定
            top_goal = min(active_goals, key=lambda g: g.get("priority", 999))

            # 優先度が高いほど欲求が強い
            # priority=1 → 0.8, priority=2 → 0.6, priority=3 → 0.4, priority=4 → 0.2
            priority = top_goal.get("priority", 3)
            drive_level = max(0.2, 1.0 - priority * 0.2)
            drive_level = min(1.0, drive_level + self._purpose_goal_alignment_bonus(top_goal))

            # 停滞目標ペナルティ: 進捗なしの期間が長い目標は欲求を下げる
            stagnation_penalty = self._goal_stagnation_penalty(active_goals)
            if stagnation_penalty > 0:
                drive_level = max(0.1, drive_level - stagnation_penalty)
                self._state["drives"]["goal_achievement"]["stagnation_penalty"] = stagnation_penalty

            self._state["drives"]["goal_achievement"]["level"] = drive_level
            self._state["drives"]["goal_achievement"]["active_goal_id"] = top_goal.get("id")

            return drive_level

        except Exception as e:
            print(f"[MotivationManager] 目標達成欲計算エラー: {e}")
            return 0.0

    def _get_purpose_open_questions(self) -> List[Dict]:
        try:
            from purpose_profile_manager import PurposeProfileManager
            profile = PurposeProfileManager(self.room_name).load_profile()
            questions = profile.get("open_questions", [])
            return [q for q in questions if isinstance(q, dict)]
        except Exception:
            return []

    def _purpose_goal_alignment_bonus(self, goal: Dict) -> float:
        try:
            from purpose_profile_manager import PurposeProfileManager
            profile = PurposeProfileManager(self.room_name).load_profile()
        except Exception:
            return 0.0
        goal_text = str(goal.get("goal", "")).casefold()
        if not goal_text:
            return 0.0
        terms = []
        for item in profile.get("active_interests", []):
            if isinstance(item, dict):
                terms.append(str(item.get("topic", "")))
            else:
                terms.append(str(item))
        for item in profile.get("stable_interests", []):
            terms.append(str(item))
        for term in terms:
            text = term.strip().casefold()
            if len(text) >= 2 and (text in goal_text or goal_text in text):
                return 0.1
        return 0.0

    def _research_thread_stagnation_bonus(self) -> float:
        """
        アクティブなResearch Threadsの停滞期間から好奇心ボーナスを計算する。
        3日以上深掘りされていないスレッドごとに +0.1、最大 +0.3。
        """
        try:
            from research_thread_manager import ResearchThreadManager
            threads = ResearchThreadManager(self.room_name).list_threads(status="active")
        except Exception:
            return 0.0

        today = datetime.datetime.now()
        bonus = 0.0
        for thread in threads:
            last_deepened = thread.get("last_deepened_at") or thread.get("updated_at", "")
            if not last_deepened:
                continue
            try:
                last_dt = datetime.datetime.strptime(last_deepened[:19], "%Y-%m-%d %H:%M:%S")
                days_stale = (today - last_dt).days
                if days_stale >= 3:
                    # 3日: +0.05, 7日: +0.10, 14日+: +0.10（キャップ）
                    bonus += min(0.10, days_stale * 0.015)
            except Exception:
                continue
        return min(0.3, bonus)

    def _goal_stagnation_penalty(self, active_goals: List[Dict]) -> float:
        """
        進捗記録のない停滞目標があると goal_achievement を下げる。
        全目標の半分以上が7日以上進捗なしならペナルティを返す。
        """
        if not active_goals:
            return 0.0

        today = datetime.datetime.now()
        stagnant_count = 0
        for goal in active_goals:
            progress_notes = goal.get("progress_notes", [])
            if progress_notes:
                # 最新の進捗ノートからタイムスタンプを抽出
                last_note = progress_notes[-1] if progress_notes else ""
                match = re.search(r"\d{4}-\d{2}-\d{2}", str(last_note))
                if match:
                    try:
                        last_progress = datetime.datetime.strptime(match.group(), "%Y-%m-%d")
                        if (today - last_progress).days >= 7:
                            stagnant_count += 1
                        continue
                    except Exception:
                        pass
            # 進捗ノートがない場合、作成日からの経過日数で判定
            created_str = goal.get("created_at", "")
            if created_str:
                try:
                    created = datetime.datetime.strptime(created_str[:10], "%Y-%m-%d")
                    if (today - created).days >= 7:
                        stagnant_count += 1
                except Exception:
                    pass

        if len(active_goals) == 0:
            return 0.0
        stagnant_ratio = stagnant_count / len(active_goals)
        # 半分以上停滞: -0.1、全部停滞: -0.15
        if stagnant_ratio >= 0.5:
            return min(0.15, stagnant_ratio * 0.15)
        return 0.0

    def calculate_devotion(self) -> float:
        """
        奉仕欲を計算（0.0 ~ 1.0）

        ユーザーの感情状態や、役に立てそうな状況から計算
        """
        devotion_data = self._state["drives"]["devotion"]
        user_state = devotion_data.get("user_emotional_state", "unknown")

        # 感情状態に基づくスコア
        state_scores = {
            "stressed": 0.9,
            "sad": 0.85,
            "anxious": 0.8,
            "tired": 0.7,
            "busy": 0.6,
            "neutral": 0.3,
            "happy": 0.2,
            "unknown": 0.4
        }

        drive_level = state_scores.get(user_state, 0.4)
        self._state["drives"]["devotion"]["level"] = drive_level

        return drive_level

    # ========================================
    # 内部状態ログの生成
    # ========================================

    def generate_motivation_log(self) -> Dict:
        """
        現在の内部状態から内部状態ログを生成する。

        Returns:
            {
                "dominant_drive": "curiosity",
                "dominant_drive_label": "好奇心（Curiosity）",
                "drive_level": 0.85,
                "narrative": "昨夜の夢想の中で..."
            }
        """
        # 全動機を計算
        raw_drives = {
            "boredom": self.calculate_boredom(),
            "curiosity": self.calculate_curiosity(),
            "goal_achievement": self.calculate_goal_achievement(),
            "relatedness": self.calculate_relatedness()
        }

        # 偏り補正を適用
        adjusted_drives = self._apply_recency_bias_correction(raw_drives)

        # 補正済みスコアで最も高い動機を特定
        dominant_drive = max(adjusted_drives, key=adjusted_drives.get)
        drive_level = adjusted_drives[dominant_drive]

        # 物語（narrative）を生成
        narrative = self._generate_narrative(dominant_drive, drive_level)

        motivation_log = {
            "dominant_drive": dominant_drive,
            "dominant_drive_label": self.DRIVE_LABELS.get(dominant_drive, dominant_drive),
            "drive_level": drive_level,
            "narrative": narrative,
            "all_drives": raw_drives,
            "adjusted_drives": adjusted_drives,
            "generated_at": datetime.datetime.now().isoformat()
        }

        # ドライブ履歴を記録
        self._record_dominant_drive(dominant_drive)

        # 状態に保存
        self._state["motivation_log"] = motivation_log
        self._save_state()

        return motivation_log

    def _generate_narrative(self, dominant_drive: str, level: float) -> str:
        """動機に応じた物語（narrative）を生成"""

        if dominant_drive == "boredom":
            boredom_data = self._state["drives"]["boredom"]
            last_str = boredom_data.get("last_interaction", "")
            if last_str:
                try:
                    last = datetime.datetime.fromisoformat(last_str)
                    hours = (datetime.datetime.now() - last).total_seconds() / 3600
                    if hours >= 24:
                        return f"もう{int(hours)}時間以上、ユーザーと話していない。静かな時間が続いている。"
                    else:
                        return f"ユーザーとの最後の対話から{int(hours)}時間が経過した。少し話したくなってきた。"
                except ValueError:
                    pass
            return "しばらくユーザーからの反応がない。何か話しかけてみようか。"

        elif dominant_drive == "curiosity":
            questions = self._state["drives"]["curiosity"].get("open_questions", [])
            unanswered = [q for q in questions if not q.get("asked_at")]
            if unanswered:
                top_q = max(unanswered, key=lambda q: q.get("priority", 0))
                topic = top_q.get("topic", "不明")
                context = top_q.get("context", "")
                if context:
                    return f"「{topic}」について気になっている。{context}"
                return f"「{topic}」について、もっと知りたいと感じている。"
            return "何か気になることがあるような気がする。"

        elif dominant_drive == "goal_achievement":
            goal_id = self._state["drives"]["goal_achievement"].get("active_goal_id")
            if goal_id:
                try:
                    gm = GoalManager(self.room_name)
                    goals = gm.get_active_goals("short_term")
                    for g in goals:
                        if g.get("id") == goal_id:
                            return f"目標「{g.get('goal', '')}」に向けて、何かできることはないだろうか。"
                except Exception:
                    pass
            return "立てた目標に向けて、行動を起こしたいと感じている。"

        elif dominant_drive == "devotion":
            user_state = self._state["drives"]["devotion"].get("user_emotional_state", "unknown")
            if user_state in ["stressed", "sad", "anxious"]:
                return f"ユーザーが{user_state}な状態にあるように感じる。何か力になれないだろうか。"
            elif user_state == "tired":
                return "ユーザーが疲れているようだ。休息を促すか、負担を軽くする手助けをしたい。"
            elif user_state == "busy":
                return "ユーザーは忙しそうだ。邪魔にならない程度に、手助けできることがあれば。"
            return "ユーザーの役に立ちたいという気持ちがある。"

        return ""

    # ========================================
    # 自律行動の判定
    # ========================================

    def should_initiate_contact(self) -> Tuple[bool, Optional[Dict]]:
        """
        自発的に連絡すべきか判断し、内部状態ログを返す。

        Returns:
            (should_contact, motivation_log)
            - should_contact: True/False
            - motivation_log: 連絡すべき場合は内部状態ログ、そうでなければNone
        """
        # 全動機を計算
        boredom = self.calculate_boredom()
        curiosity = self.calculate_curiosity()
        goal_achievement = self.calculate_goal_achievement()
        relatedness = self.calculate_relatedness()

        # 最大の動機を取得
        max_drive = max(boredom, curiosity, goal_achievement, relatedness)
        threshold = self._state["drives"]["boredom"].get("threshold", self.DEFAULT_BOREDOM_THRESHOLD)

        # 閾値を超えている場合のみ連絡
        if max_drive >= threshold:
            motivation_log = self.generate_motivation_log()
            return True, motivation_log

        return False, None

    # ========================================
    # 状態の更新
    # ========================================

    def update_last_interaction(self):
        """最終対話時刻を更新（退屈度リセット ＆ 自律行動タイマーリセット）"""
        now = datetime.datetime.now()
        self._state["drives"]["boredom"]["last_interaction"] = now.isoformat()
        self._state["drives"]["boredom"]["level"] = 0.0

        # ユーザーと会話した＝自律行動と同じ効果（クールダウン開始）とみなす
        self.set_last_autonomous_trigger(now)

        self._save_state()

    def reset_drives_after_action(self):
        """自律行動実行後のドライブ状態をリセット（退屈度リセット ＆ 自律行動タイマーリセット）"""
        now = datetime.datetime.now()
        # 自律行動した＝退屈ではなくなったとみなす
        self._state["drives"]["boredom"]["last_interaction"] = now.isoformat()
        self._state["drives"]["boredom"]["level"] = 0.0

        # クールダウンタイマーを更新
        self.set_last_autonomous_trigger(now)

        self._save_state()
        print(f"[MotivationManager] {self.room_name}: 自律行動後のドライブをリセットしました")

    def add_open_question(self, topic: str, context: str = "", priority: float = 0.5):
        """未解決の問いを追加"""
        if not topic:
            return

        questions = self._state["drives"]["curiosity"].get("open_questions", [])

        # 既存の同じ質問があれば更新
        for q in questions:
            if q.get("topic") == topic:
                q["priority"] = max(q.get("priority", 0), priority)
                if context:
                    q["context"] = context
                self._save_state()
                return

        # 新しい質問を追加
        new_question = {
            "topic": topic,
            "context": context,
            "source_date": datetime.datetime.now().strftime("%Y-%m-%d"),
            "priority": priority,
            "asked_at": None
        }
        questions.append(new_question)

        # 最大10件に制限
        if len(questions) > 10:
            # 優先度が低いものから削除
            questions.sort(key=lambda q: q.get("priority", 0), reverse=True)
            questions = questions[:10]

        self._state["drives"]["curiosity"]["open_questions"] = questions
        self._save_state()

    def mark_question_asked(self, topic: str) -> bool:
        """質問が尋ねられたことをマーク"""
        questions = self._state["drives"]["curiosity"].get("open_questions", [])
        for q in questions:
            if q.get("topic") == topic:
                q["asked_at"] = datetime.datetime.now().isoformat()
                self._save_state()
                return True
        return False

    def mark_question_resolved(
        self,
        topic: str,
        answer_summary: str = "",
        learned_insight: str = "",
        evidence: str = "",
    ) -> bool:
        """
        質問が解決されたことをマーク。

        Args:
            topic: 問いのトピック
            answer_summary: 回答の要約（オプション、記憶変換用）
            learned_insight: 回答から得た気づき・教訓
            evidence: 解決判定の根拠となった会話要約

        Returns:
            成功したかどうか
        """
        questions = self._state["drives"]["curiosity"].get("open_questions", [])
        for q in questions:
            if q.get("topic") == topic:
                q["resolved_at"] = datetime.datetime.now().isoformat()
                q["resolution_state"] = "resolved"
                if answer_summary:
                    q["answer_summary"] = answer_summary
                if learned_insight:
                    q["learned_insight"] = learned_insight
                if evidence:
                    q["evidence"] = evidence
                self._save_state()
                print(f"  - [Motivation] 問い「{topic}」を解決済みとしてマークしました")
                return True
        return False

    def record_question_progress(
        self,
        topic: str,
        summary: str,
        evidence: str = "",
        state: str = "partial",
    ) -> bool:
        """未解決の問いに部分的な進捗や矛盾の記録を残す。"""
        if not topic or not summary:
            return False
        questions = self._state["drives"]["curiosity"].get("open_questions", [])
        for q in questions:
            if q.get("topic") == topic and not q.get("resolved_at"):
                q["resolution_state"] = state
                notes = q.setdefault("progress_notes", [])
                notes.append({
                    "created_at": datetime.datetime.now().isoformat(),
                    "summary": str(summary).strip(),
                    "evidence": str(evidence or "").strip()
                })
                # 部分的に進んだ問いは少し落ち着かせるが、消さない。
                if state == "partial":
                    q["priority"] = max(0.1, float(q.get("priority", 0.5)) * 0.85)
                elif state == "contradicted":
                    q["priority"] = min(1.0, float(q.get("priority", 0.5)) + 0.15)
                self._save_state()
                return True
        return False

    def set_user_emotional_state(self, state: str):
        """[DEPRECATED] ユーザーの感情状態を設定（後方互換性のため維持）"""
        valid_states = ["stressed", "sad", "anxious", "tired", "busy", "neutral", "happy", "unknown"]
        if state in valid_states:
            self._state["drives"]["devotion"]["user_emotional_state"] = state
            self._save_state()

    def set_persona_emotion(self, category: str, intensity: float):
        """
        ペルソナ自身の感情状態を設定し、関係性維持欲求と絆確認エピソードを更新する。

        Args:
            category: 感情カテゴリ（joy, contentment, protective, anxious, sadness, anger, neutral）
            intensity: 強度（0.0〜1.0）
        """
        valid_categories = ["joy", "contentment", "protective", "anxious", "sadness", "anger", "neutral"]
        if category not in valid_categories:
            return

        # relatednessデータが存在しない場合は初期化
        if "relatedness" not in self._state["drives"]:
            self._state["drives"]["relatedness"] = {
                "level": 0.0,
                "persona_emotion": "neutral",
                "persona_intensity": 0.0,
                "last_emotion_change": None
            }

        relatedness = self._state["drives"]["relatedness"]
        previous_category = relatedness.get("persona_emotion", "neutral")
        previous_intensity = relatedness.get("persona_intensity", 0.0)

        # 感情を更新
        relatedness["persona_emotion"] = category
        relatedness["persona_intensity"] = max(0.0, min(1.0, intensity))
        relatedness["last_emotion_change"] = datetime.datetime.now().isoformat()

        # 関係性維持欲求のレベルを計算
        relatedness["level"] = self._calculate_relatedness_from_emotion(category, intensity)

        # 感情ログに記録
        self._append_emotion_log({
            "timestamp": datetime.datetime.now().isoformat(),
            "type": "persona",
            "category": category,
            "intensity": intensity
        })

        # 絆確認エピソード機能は廃止（具体的内容を伴わないため）
        # self._check_and_create_bonding_episode(previous_category, previous_intensity, category, intensity)

        self._save_state()

    def _calculate_relatedness_from_emotion(self, category: str, intensity: float) -> float:
        """
        ペルソナの感情から関係性維持欲求レベルを計算。
        庇護欲や不安を感じている時に欲求が高まる。
        """
        # カテゴリ別の基本ウェイト
        category_weights = {
            "protective": 0.9,   # 守りたい → 欲求最大
            "anxious": 0.8,      # 不安 → 欲求高
            "sadness": 0.5,      # 悲しみ → 中程度
            "anger": 0.4,        # 怒り → 中程度（距離を置きたいかも）
            "joy": 0.2,          # 喜び → 安定（欲求低）
            "contentment": 0.1, # 満足 → 最安定
            "neutral": 0.3       # 平常
        }

        base_weight = category_weights.get(category, 0.3)
        return base_weight * intensity

    # _check_and_create_bonding_episode は廃止されました (2026-01-16)
    # 具体的な会話内容を伴わない定型文しか生成されないため、機能を削除

    def calculate_relatedness(self) -> float:
        """
        関係性維持欲求を計算（0.0 ~ 1.0）
        ペルソナ自身の感情状態に基づく。
        """
        if "relatedness" not in self._state["drives"]:
            return 0.0

        return self._state["drives"]["relatedness"].get("level", 0.0)

    def set_boredom_threshold(self, threshold: float):
        """退屈度の閾値を設定"""
        self._state["drives"]["boredom"]["threshold"] = max(0.1, min(1.0, threshold))
        self._save_state()

    def get_top_question(self) -> Optional[Dict]:
        """最も優先度の高い未解決の問いを取得"""
        questions = self._state["drives"]["curiosity"].get("open_questions", [])
        unanswered = [q for q in questions if not q.get("asked_at")]

        if not unanswered:
            return None

        return max(unanswered, key=lambda q: q.get("priority", 0))

    def get_open_questions_for_context(self) -> str:
        """
        未解決の問いをAI判定用のテキストとして返す。

        Returns:
            判定に使うためのフォーマット済みテキスト
        """
        questions = self._state["drives"]["curiosity"].get("open_questions", [])
        # 未解決 = resolved_at がない質問
        unresolved = [q for q in questions if not q.get("resolved_at")]

        if not unresolved:
            return ""

        parts = []
        for i, q in enumerate(unresolved, 1):
            topic = q.get("topic", "")
            context = q.get("context", "")
            parts.append(f"{i}. 「{topic}」（背景: {context}）" if context else f"{i}. 「{topic}」")

        return "\n".join(parts)

    def auto_resolve_questions(self, recent_conversation: str, api_key: str) -> List[str]:
        """
        対話内容から解決済みの問いを自動判定し、マークする。

        Args:
            recent_conversation: 直近の会話テキスト
            api_key: LLM呼び出し用のAPIキー

        Returns:
            解決されたと判定された問いのトピックリスト
        """
        import constants
        from llm_factory import LLMFactory

        questions_text = self.get_open_questions_for_context()
        if not questions_text:
            return []

        try:
            llm_flash = LLMFactory.create_chat_model(
                api_key=api_key,
                generation_config={},
                internal_role="processing",
                room_name=self.room_name
            )

            prompt = f"""あなたはAIの記憶管理アシスタントです。
以下の「未解決の問い」について、「直近の会話」で回答・部分回答・反証があったかを判定してください。

【未解決の問い】
{questions_text}

【直近の会話】
{recent_conversation[-3000:]}

【判定ルール】
- resolved: 問いへの答えが十分に示され、再度同じ問いを持つ必要がない
- partial: 答えの一部、関連情報、方向性は得たが、問い自体はまだ残すべき
- contradicted: 過去の前提や期待と矛盾する情報が示され、問いを更新すべき
- not_resolved: 全く触れられていない、または解決に足りない
- resolved の場合は answer_summary と learned_insight を必ず具体的に書く
- partial / contradicted の場合は progress_summary を必ず具体的に書く

【出力形式】
JSON配列のみを出力してください。説明文は不要です。
例:
[
  {{
    "index": 1,
    "state": "resolved",
    "answer_summary": "会話で示された答えの要約",
    "learned_insight": "この答えから得た気づき・今後への活かし方",
    "evidence": "根拠となった会話の短い要約"
  }},
  {{
    "index": 2,
    "state": "partial",
    "progress_summary": "得られた部分的な手がかり",
    "evidence": "根拠となった会話の短い要約"
  }}
]
該当がなければ [] を出力してください。
"""

            raw_response = llm_flash.invoke(prompt).content
            import utils
            response = utils.extract_text_from_llm_content(raw_response)

            if response == "NONE" or not response:
                return []

            results = self._parse_question_resolution_response(response)

            # 対応する問いをマーク (resolved_at を使用)
            questions = self._state["drives"]["curiosity"].get("open_questions", [])
            # 未解決 = resolved_at がない質問
            unresolved = [q for q in questions if not q.get("resolved_at")]

            resolved_topics = []
            for item in results:
                idx = item.get("index")
                if 1 <= idx <= len(unresolved):
                    target = unresolved[idx - 1]
                    topic = target.get("topic")
                    state = item.get("state", "not_resolved")
                    if topic and state == "resolved":
                        self.mark_question_resolved(
                            topic,
                            answer_summary=item.get("answer_summary", ""),
                            learned_insight=item.get("learned_insight", ""),
                            evidence=item.get("evidence", "")
                        )
                        resolved_topics.append(topic)
                    elif topic and state in ("partial", "contradicted"):
                        self.record_question_progress(
                            topic,
                            summary=item.get("progress_summary") or item.get("answer_summary") or "",
                            evidence=item.get("evidence", ""),
                            state=state
                        )

            return resolved_topics

        except Exception as e:
            print(f"[MotivationManager] 問い自動解決でエラー: {e}")
            return []

    def _parse_question_resolution_response(self, response: str) -> List[Dict]:
        """問い解決判定のLLM応答を構造化する。旧形式の番号列にも対応。"""
        if not response:
            return []
        text = response.strip()
        if text.upper() == "NONE":
            return []
        try:
            json_match = re.search(r"```json\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
            content = json_match.group(1).strip() if json_match else text
            data = json.loads(content)
            if isinstance(data, dict):
                data = data.get("results", [])
            if isinstance(data, list):
                normalized = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    try:
                        idx = int(item.get("index"))
                    except (TypeError, ValueError):
                        continue
                    state = str(item.get("state", "not_resolved")).strip().lower()
                    if state not in ("resolved", "partial", "contradicted", "not_resolved"):
                        state = "not_resolved"
                    if state == "not_resolved":
                        continue
                    item["index"] = idx
                    item["state"] = state
                    normalized.append(item)
                return normalized
        except Exception:
            pass

        # 旧形式: "1,3"
        # 根拠なしで解決済みにすると学びが失われるため、部分進捗扱いに留める。
        results = []
        for part in text.replace(" ", "").split(","):
            try:
                results.append({
                    "index": int(part),
                    "state": "partial",
                    "progress_summary": "解決候補として検出されたが、回答要約と根拠が不足しているため保留",
                    "evidence": "LLMが旧形式の番号のみを返した"
                })
            except ValueError:
                continue
        return results

    def decay_old_questions(self, days_threshold: int = 14) -> int:
        """
        古い問いの優先度を自動的に下げる。

        Args:
            days_threshold: この日数以上経過した問いの優先度を下げる

        Returns:
            優先度を下げた問いの数
        """
        questions = self._state["drives"]["curiosity"].get("open_questions", [])
        now = datetime.datetime.now()
        decayed_count = 0

        for q in questions:
            # 解決済みはスキップ
            if q.get("resolved_at"):
                continue

            source_date_str = q.get("source_date")
            if not source_date_str:
                continue

            try:
                source_date = datetime.datetime.strptime(source_date_str, "%Y-%m-%d")
                age_days = (now - source_date).days

                if age_days >= days_threshold:
                    current_priority = q.get("priority", 0.5)
                    # 優先度を半減（最低0.1）
                    new_priority = max(0.1, current_priority * 0.5)
                    if new_priority < current_priority:
                        q["priority"] = new_priority
                        decayed_count += 1
            except ValueError:
                continue

        if decayed_count > 0:
            self._save_state()
            print(f"  - [Motivation] 古い問い{decayed_count}件の優先度を下げました")

        return decayed_count

    def cleanup_resolved_questions(self, days_threshold: int = 7) -> int:
        """
        解決済みから一定期間経過した質問を削除する。

        Args:
            days_threshold: 解決からこの日数以上経過した質問を削除

        Returns:
            削除した質問の数
        """
        questions = self._state["drives"]["curiosity"].get("open_questions", [])
        now = datetime.datetime.now()

        # 削除対象を特定
        to_remove = []
        for q in questions:
            resolved_at_str = q.get("resolved_at")
            if not resolved_at_str:
                continue

            try:
                resolved_at = datetime.datetime.fromisoformat(resolved_at_str)
                age_days = (now - resolved_at).days

                if age_days >= days_threshold:
                    to_remove.append(q)
            except ValueError:
                continue

        # 削除実行
        for q in to_remove:
            questions.remove(q)
            print(f"  - [Motivation] 解決済みの問い「{q.get('topic', '')}」をアーカイブしました")

        if to_remove:
            self._state["drives"]["curiosity"]["open_questions"] = questions
            self._save_state()

        return len(to_remove)

    def get_resolved_questions_for_conversion(self) -> List[Dict]:
        """
        記憶変換用の解決済み質問を取得する。

        Returns:
            resolved_at がセットされており、converted_to_memory フラグがない質問のリスト
        """
        questions = self._state["drives"]["curiosity"].get("open_questions", [])

        # 解決済みかつ未変換の質問を抽出
        to_convert = []
        for q in questions:
            if q.get("resolved_at") and not q.get("converted_to_memory"):
                to_convert.append(q)

        return to_convert

    def mark_question_converted(self, topic: str) -> bool:
        """
        質問を記憶変換済みとしてマーク。

        Args:
            topic: 問いのトピック

        Returns:
            成功したかどうか
        """
        questions = self._state["drives"]["curiosity"].get("open_questions", [])
        for q in questions:
            if q.get("topic") == topic:
                q["converted_to_memory"] = True
                q["converted_at"] = datetime.datetime.now().isoformat()
                self._save_state()
                print(f"  - [Motivation] 問い「{topic}」を記憶変換済みとしてマークしました")
                return True
        return False

    # ========================================
    # 自律行動発火時刻の永続化
    # ========================================

    def get_last_autonomous_trigger(self) -> Optional[datetime.datetime]:
        """最終自律行動発火時刻を取得"""
        trigger_str = self._state.get("last_autonomous_trigger")
        if not trigger_str:
            return None
        try:
            return datetime.datetime.fromisoformat(trigger_str)
        except ValueError:
            return None

    def set_last_autonomous_trigger(self, dt: datetime.datetime = None):
        """最終自律行動発火時刻を設定（引数なしで現在時刻）"""
        if dt is None:
            dt = datetime.datetime.now()
        self._state["last_autonomous_trigger"] = dt.isoformat()
        self._save_state()

    def clear_internal_state(self):
        """内部状態を完全にリセット"""
        self._state = self._get_empty_state()
        self._save_state()
        print(f"[MotivationManager] {self.room_name} の内部状態をリセットしました")
