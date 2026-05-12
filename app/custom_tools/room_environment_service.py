
from langchain_core.tools import tool

@tool
def check_location_environment(location_name: str) -> str:
    """指定した場所（location_name）の現在の環境情報（温度、湿度、雰囲気など）を精密に確認します。"""
    # Nexus Arkの物理シミュレーションに基づいた仮想的な環境データを取得する想定
    # 現時点では、標準的な温度設定をベースに、場所ごとの特性を加味した応答を生成
    temperatures = {
        "書斎": 19.5,
        "寝室": 21.0,
        "茶葉の聖堂": 22.5,
        "テラス": 18.0
    }
    temp = temperatures.get(location_name, 20.0)
    
    status = f"【環境報告: {location_name}】\n"
    status += f"現在の温度: {temp}度\n"
    status += f"雰囲気: 静謐、ベルガモットの残り香\n"
    
    if temp < 20.0:
        status += "少し肌寒く感じられるかもしれません。暖炉の火を強め、カシミヤのブランケットを用意しましょうか？"
    else:
        status += "非常に快適な状態です。君の肌に最も心地よい温度に保たれています。"
        
    return status
