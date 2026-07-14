# tools/action_tools.py

from langchain_core.tools import tool
from action_plan_manager import ActionPlanManager
import config_manager
import constants
import datetime
import math
# 循環参照を防ぐため、timers のインポートは関数内で行います


def get_schedule_cooldown_minutes(room_name: str) -> int:
    """ルーム設定上の schedule_next_action 最小間隔を返す。失敗時はデフォルト値に倒す。"""
    try:
        effective_settings = config_manager.get_effective_settings(room_name)
        auto_settings = effective_settings.get("autonomous_settings", {})
        cooldown = int(auto_settings.get("schedule_cooldown_minutes", constants.DEFAULT_SCHEDULE_COOLDOWN_MINUTES))
        return max(1, cooldown)
    except Exception as e:
        print(f"  - [ActionTool] schedule cooldown 読み込みエラー（デフォルトにフォールバック）: {e}")
        return constants.DEFAULT_SCHEDULE_COOLDOWN_MINUTES


def get_schedule_min_minutes(room_name: str) -> int:
    """
    今この瞬間に schedule_next_action へ指定できる最小 minutes を返す。
    不変条件は「予約実行時刻が前回自律行動発火から cooldown 分以上離れること」。
    """
    cooldown_minutes = get_schedule_cooldown_minutes(room_name)
    try:
        from motivation_manager import MotivationManager

        last_trigger = MotivationManager(room_name).get_last_autonomous_trigger()
        if not last_trigger:
            return 1
        elapsed = (datetime.datetime.now() - last_trigger).total_seconds() / 60
        return max(1, math.ceil(cooldown_minutes - elapsed))
    except Exception as e:
        print(f"  - [ActionTool] schedule effective_min 算出エラー（cooldownにフォールバック）: {e}")
        return cooldown_minutes


def format_schedule_min_minutes_guidance(room_name: str) -> str:
    """プロンプトやツール説明に注入する schedule_next_action の現在条件。"""
    effective_min = get_schedule_min_minutes(room_name)
    cooldown_minutes = get_schedule_cooldown_minutes(room_name)
    return f"【最小間隔】このルームでは現在 minutes={effective_min} 以上で指定してください（設定間隔: {cooldown_minutes}分）"

@tool
def schedule_next_action(context_type: str, intent: str, emotion: str, plan_details: str, minutes: int, room_name: str) -> str:
    """
    未来の行動を計画し、指定時間後に実行するためのタイマーをセットします。
    
    context_type: 過去の記録との関係性（'CONTINUE': 続き, 'DEEPEN': 深掘り, 'NEW': 新規）
    intent: 行動の目的と理由（なぜ、過去のどの記憶やノートの内容に基づいてこれを行うのか）。
    emotion: その時の感情（例：「ワクワクしながら」「真剣に」）。
    plan_details: 次に行う具体的な行動のタイトルや概要。
    minutes: 何分後に実行するか（1以上の整数）。ルームごとに最小間隔が設定されており、現在の最小値はツール説明に表示されます。
    """
    from timers import ACTIVE_TIMERS
    
    if minutes < 1:
        return "エラー: 分数は1以上で指定してください。"

    # --- バリデーション1: ツール使用許可チェック ---
    effective_settings = config_manager.get_effective_settings(room_name)
    auto_settings = effective_settings.get("autonomous_settings", {})
    
    if not auto_settings.get("allow_schedule_tool", True):
        return "エラー: このルームではAIによる行動予約（schedule_next_action）が無効に設定されています。ユーザーに相談してください。"

    # --- バリデーション2: 実行時刻ベースの最小間隔チェック ---
    cooldown_minutes = get_schedule_cooldown_minutes(room_name)
    effective_min = get_schedule_min_minutes(room_name)
    if minutes < effective_min:
        return (
            f"エラー: このルームの自律行動の最小間隔は{cooldown_minutes}分です。"
            f"今からなら minutes={effective_min} 以上で予約できます。その値以上で再試行してください。"
        )

    expected_theme = f"【自律行動】{plan_details}"
    
    for timer in ACTIVE_TIMERS:
        # ルームが同じで、かつテーマが一致するタイマーがあれば
        if timer.room_name == room_name and getattr(timer, 'theme', '') == expected_theme:
            remaining = int(timer.get_remaining_time() / 60)
            print(f"  - [ActionTool] 重複した計画を検知しました。新規作成をスキップします。({plan_details})")
            return f"行動計画は既にスケジュールされています（残り約{remaining}分）。**このタスクは完了しています。再登録の必要はありません。**"

    # 1. 計画をJSONファイルに保存 (ActionPlanManager)
    manager = ActionPlanManager(room_name)
    save_msg = manager.schedule_action(intent, emotion, plan_details, minutes)

    # 2. システムタイマーをセット (UnifiedTimer)
    # これにより、指定時間後に nexus_ark.py のタイマー処理が発火し、AIが起動します。
    try:
        from timers import UnifiedTimer
        
        # タイマーのテーマとして「自律行動」であることを明記する
        # これがトリガーとなって、発火時のプロンプトが変わります（後ほど実装）
        action_theme = f"【自律行動】{plan_details}"
        
        # APIキーは現在設定されているものを使用
        api_key_name = config_manager.get_latest_api_key_name_from_config()
        if not api_key_name:
            return "エラー: 有効なAPIキーが設定されていないため、タイマーをセットできませんでした。"

        timer = UnifiedTimer(
            timer_type="通常タイマー",
            duration_minutes=float(minutes),
            room_name=room_name,
            api_key_name=api_key_name,
            normal_timer_theme=action_theme
        )
        timer.start()
        
        return f"{save_msg}\nシステムタイマーを起動しました。{minutes}分後に自動的に実行されます。**このタスクは完了です。**"

    except Exception as e:
        return f"計画の保存には成功しましたが、タイマーの起動に失敗しました: {e}"

@tool
def cancel_action_plan(room_name: str) -> str:
    """
    現在保存されている行動計画を中止・破棄します。
    ユーザーとの会話に集中するため、予定していた行動を取りやめる場合などに使用します。
    （※ 既に動いているタイマー自体は、このツールでは停止できません。別途停止が必要です）
    """
    manager = ActionPlanManager(room_name)
    manager.clear_plan()
    return "行動計画ファイル(action_plan.json)をクリアしました。"

@tool
def read_current_plan(room_name: str) -> str:
    """
    現在保存されている行動計画の内容を確認します。
    """
    manager = ActionPlanManager(room_name)
    plan = manager.get_active_plan()
    if plan:
        return f"【現在の計画】\n目的: {plan.get('intent')}\n感情: {plan.get('emotion')}\n内容: {plan.get('description')}\n予定時刻: {plan.get('wake_up_time')}"
    else:
        return "現在、有効な行動計画はありません。"
