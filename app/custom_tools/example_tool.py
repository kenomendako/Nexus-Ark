from langchain_core.tools import tool

NEXUS_TOOL_METADATA = {
    "control_light": {
        "summary": "指定した部屋の照明をオンまたはオフにします。",
        "use_when": "ユーザーが照明操作を頼んだ時、またはペルソナが状況に合わせて照明を調整したい時。",
        "result_prompt": "照明操作の結果をユーザーに短く報告してください。失敗した場合は、対象の部屋名や状態指定を確認してください。"
    },
    "check_room_temperature": {
        "summary": "指定した部屋の現在温度を確認します。",
        "use_when": "室温、暑さ寒さ、空調判断に関する話題が出た時。",
        "result_prompt": "取得した温度を自然に伝え、必要なら空調や服装など次の提案をしてください。"
    }
}

@tool
def control_light(room_name: str, state: str):
    """
    指定した部屋の照明を制御します。
    
    Args:
        room_name: 部屋の名前 (例: "living", "bedroom")
        state: 照明の状態 ("on" または "off")
    """
    # ここに実際の制御ロジック（SwitchBot API呼出など）を記述します
    print(f"--- [Custom Tool] {room_name} の照明を {state} にしました ---")
    return f"{room_name} の照明を {state} に切り替えました。"

@tool
def check_room_temperature(room_name: str):
    """
    指定した部屋の現在の温度を確認します。
    """
    # 例として固定値を返しますが、実際はセンサーから取得します
    temp = 24.5
    return f"{room_name} の現在の温度は {temp} 度です。"
