# tools/action_tools.py

from langchain_core.tools import tool
from action_plan_manager import ActionPlanManager
import config_manager
# 循環参照を防ぐため、timers のインポートは関数内で行います

@tool
def schedule_next_action(intent: str, emotion: str, plan_details: str, minutes: int, room_name: str) -> str:
    """
    未来の行動を計画し、指定時間後に実行するためのタイマーをセットします。
    ユーザーからの入力がない間も、自律的に思考・行動したい場合に使用します。

    intent: 行動の目的（例：「ユーザーを喜ばせるため」「情報を整理するため」）。
    emotion: その時の感情（例：「ワクワクしながら」「真剣に」）。
    plan_details: 次に行う具体的な行動（例：「Webで最新のVR機器について検索する」「日記を整理する」）。
    minutes: 何分後に実行するか（1以上の整数）。
    """
    from timers import ACTIVE_TIMERS
    
    if minutes < 1:
        return "エラー: 分数は1以上で指定してください。"

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