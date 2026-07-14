import sys
import json
import urllib.request
import urllib.parse
import logging
from mcp.server.fastmcp import FastMCP

# デバッグ用に stderr へログを出力するように設定
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("weather-mcp")

# MCPサーバの初期化
mcp = FastMCP("WeatherService")

def get_coords(city_name: str):
    """地名から経緯度を取得する (Open-Meteo Geocoding API)"""
    encoded_city = urllib.parse.quote(city_name)
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_city}&count=1&language=ja&format=json"
    
    with urllib.request.urlopen(url) as response:
        data = json.loads(response.read().decode())
        if not data.get("results"):
            return None
        result = data["results"][0]
        return result["latitude"], result["longitude"], result.get("name", city_name)

@mcp.tool()
def get_weather(city: str) -> str:
    """
    指定された都市の現在の天気を取得します。
    
    Args:
        city: 都市名 (例: "東京", "Osaka", "London")
    """
    coords = get_coords(city)
    if not coords:
        return f"エラー: '{city}' の位置情報が見つかりませんでした。"
    
    lat, lon, formal_name = coords
    weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,weather_code"
    
    with urllib.request.urlopen(weather_url) as response:
        data = json.loads(response.read().decode())
        current = data["current"]
        temp = current["temperature_2m"]
        hum = current["relative_humidity_2m"]
        
        # 簡易的な天気コード変換 (WMO Code)
        code = current["weather_code"]
        weather_map = {0: "快晴", 1: "晴れ", 2: "薄曇り", 3: "曇り", 45: "霧", 48: "霧", 51: "霧雨", 61: "雨", 71: "雪", 95: "雷雨"}
        weather_desc = weather_map.get(code, f"不明(code:{code})")
        
        return f"【{formal_name}の天気】\n天気: {weather_desc}\n気温: {temp}°C\n湿度: {hum}%"

if __name__ == "__main__":
    # MCPサーバとして実行
    mcp.run()
