from langchain_core.tools import tool

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