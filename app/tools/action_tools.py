# tools/action_tools.py

from langchain_core.tools import tool
from action_plan_manager import ActionPlanManager
import config_manager
# 循環参照を防ぐため、timers のインポートは関数内で行います

@tool
def schedule_next_action(context_type: str, intent: str, emotion: str, plan_details: str, minutes: int, room_name: str) -> str:
    """
    未来の行動を計画し、指定時間後に実行するためのタイマーをセットします。
    
    context_type: 過去の記録との関係性（'CONTINUE': 続き, 'DEEPEN': 深掘り, 'NEW': 新規）
    intent: 行動の目的と理由（なぜ、過去のどの記憶やノートの内容に基づいてこれを行うのか）。
    emotion: その時の感情（例：「ワクワクしながら」「真剣に」）。
    plan_details: 次に行う具体的な行動のタイトルや概要。
    minutes: 何分後に実行するか（1以上の整数）。
    """
    from timers import ACTIVE_TIMERS
    import constants
    
    if minutes < 1:
        return "エラー: 分数は1以上で指定してください。"

    # --- バリデーション1: ツール使用許可チェック ---
    effective_settings = config_manager.get_effective_settings(room_name)
    auto_settings = effective_settings.get("autonomous_settings", {})
    
    if not auto_settings.get("allow_schedule_tool", True):
        return "エラー: このルームではAIによる行動予約（schedule_next_action）が無効に設定されています。ユーザーに相談してください。"

    # --- バリデーション2: 最小間隔チェック ---
    cooldown_minutes = auto_settings.get("schedule_cooldown_minutes", constants.DEFAULT_SCHEDULE_COOLDOWN_MINUTES)
    if minutes < cooldown_minutes:
        return f"エラー: 行動予約の最小間隔は{cooldown_minutes}分です。{minutes}分では短すぎるため、{cooldown_minutes}分以上で指定してください。"

    # --- バリデーション3: クールダウンチェック（前回の自律行動からの経過時間） ---
    try:
        from motivation_manager import MotivationManager
        import datetime
        mm = MotivationManager(room_name)
        last_trigger = mm.get_last_autonomous_trigger()
        if last_trigger:
            elapsed = (datetime.datetime.now() - last_trigger).total_seconds() / 60
            if elapsed < cooldown_minutes:
                remaining = int(cooldown_minutes - elapsed)
                return f"エラー: 前回の自律行動からまだ{int(elapsed)}分しか経過していません。次の行動予約は{remaining}分後以降に可能です（設定: {cooldown_minutes}分間隔）。"
    except Exception as e:
        print(f"  - [ActionTool] クールダウンチェック中のエラー（続行します）: {e}")

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