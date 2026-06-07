# weather_service.py
"""
Open-Meteo API を利用した天気情報の取得、ジオコーディング、
および気温・日出日没に基づく体感季節・時間帯判定を行うサービス。
"""

import sys
import os
import time
import datetime
import urllib.request
import urllib.parse
import json
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

# グローバルメモリキャッシュ
_weather_cache: Optional[Dict] = None
_weather_cache_time: float = 0.0
_CACHE_TTL_SECONDS = 1800  # 30分

# WMO天気コードから日本語説明へのマッピング
WMO_CODE_MAP = {
    0: "快晴",
    1: "晴れ",
    2: "やや曇り",
    3: "曇り",
    45: "霧",
    48: "凍結霧",
    51: "弱い霧雨",
    53: "霧雨",
    55: "強い霧雨",
    56: "弱い凍結霧雨",
    57: "強い凍結霧雨",
    61: "小雨",
    63: "雨",
    65: "大雨",
    66: "弱い凍結雨",
    67: "強い凍結雨",
    71: "小雪",
    73: "雪",
    75: "大雪",
    77: "霰 (あられ)",
    80: "弱い小糠雨",
    81: "にわか雨",
    82: "激しいにわか雨",
    85: "弱いお天気雪",
    86: "にわか雪",
    95: "雷雨",
    96: "霰を伴う弱い雷雨",
    99: "霰を伴う激しい雷雨"
}

@dataclass
class WeatherData:
    temperature: float          # 気温 (℃)
    apparent_temperature: float    # 体感温度 (℃)
    weather_code: int           # WMO天気コード
    weather_description: str    # 日本語の天気説明 (例: "快晴")
    humidity: int               # 湿度 (%)
    precipitation: float        # 降水量 (mm)
    wind_speed: float          # 風速 (km/h)
    is_day: bool                # 昼か夜か (True: 昼, False: 夜)
    sunrise: str                # 本日の日出時刻 (例: "04:27")
    sunset: str                 # 本日の日没時刻 (例: "18:50")
    fetched_at: str             # 取得タイムスタンプ (ISO8601)

class WeatherService:
    def __init__(self):
        pass

    def search_city(self, city_name: str) -> List[Dict]:
        """
        Geocoding API を使用して都市名から緯度・経度の候補リストを取得する。
        戻り値: [{'name': 'Tokyo', 'country': 'Japan', 'latitude': 35.678, 'longitude': 139.751, 'admin1': 'Tokyo'}]
        """
        if not city_name or not city_name.strip():
            return []

        encoded_name = urllib.parse.quote(city_name.strip())
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_name}&count=5&language=ja"
        
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'NexusArkWeatherClient/1.0'})
            with urllib.request.urlopen(req, timeout=5.0) as response:
                data = json.loads(response.read().decode('utf-8'))
                results = data.get("results", [])
                if not results:
                    return []
                
                formatted_results = []
                for item in results:
                    formatted_results.append({
                        "name": item.get("name", ""),
                        "country": item.get("country", ""),
                        "latitude": item.get("latitude"),
                        "longitude": item.get("longitude"),
                        "admin1": item.get("admin1", ""),
                        "timezone": item.get("timezone", "")
                    })
                return formatted_results
        except Exception as e:
            print(f"  - [Weather] Geocoding API エラー: {e}")
            return []

    def fetch_weather(self, lat: float, lon: float) -> Optional[WeatherData]:
        """
        Open-Meteo API から指定された経緯度の現在天気を取得する。
        """
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
            f"precipitation,weather_code,wind_speed_10m,is_day"
            f"&daily=sunrise,sunset"
            f"&timezone=auto&forecast_days=1"
        )

        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'NexusArkWeatherClient/1.0'})
            with urllib.request.urlopen(req, timeout=6.0) as response:
                data = json.loads(response.read().decode('utf-8'))
                
                current = data.get("current", {})
                daily = data.get("daily", {})
                
                sunrise_list = daily.get("sunrise", [])
                sunset_list = daily.get("sunset", [])
                
                # sunrise/sunset は "2026-05-30T04:27" のような文字列で返るため、時刻部分のみ抽出
                sunrise_val = "05:00"
                sunset_val = "18:00"
                if sunrise_list and len(sunrise_list) > 0 and "T" in sunrise_list[0]:
                    sunrise_val = sunrise_list[0].split("T")[1]
                if sunset_list and len(sunset_list) > 0 and "T" in sunset_list[0]:
                    sunset_val = sunset_list[0].split("T")[1]

                w_code = current.get("weather_code", 0)
                w_desc = WMO_CODE_MAP.get(w_code, "不明な天気")

                fetched_time = datetime.datetime.now().isoformat()

                return WeatherData(
                    temperature=float(current.get("temperature_2m", 15.0)),
                    apparent_temperature=float(current.get("apparent_temperature", 15.0)),
                    weather_code=int(w_code),
                    weather_description=w_desc,
                    humidity=int(current.get("relative_humidity_2m", 50)),
                    precipitation=float(current.get("precipitation", 0.0)),
                    wind_speed=float(current.get("wind_speed_10m", 0.0)),
                    is_day=bool(current.get("is_day", True)),
                    sunrise=sunrise_val,
                    sunset=sunset_val,
                    fetched_at=fetched_time
                )
        except Exception as e:
            print(f"  - [Weather] Weather API エラー: {e}")
            return None

    def get_cached_weather(self) -> Optional[WeatherData]:
        """
        キャッシュ付きで天気情報を取得する。
        設定ファイルから緯度経度を読み込んで実行。
        """
        global _weather_cache, _weather_cache_time
        
        # 共通設定を読み込む
        import config_manager
        config = config_manager.load_config_file()
        weather_settings = config.get("weather_settings", {})
        
        lat = weather_settings.get("latitude")
        lon = weather_settings.get("longitude")
        
        if lat is None or lon is None:
            return None

        now = time.time()
        # キャッシュが有効な場合はキャッシュを返す
        if _weather_cache is not None and (now - _weather_cache_time < _CACHE_TTL_SECONDS):
            return _weather_cache

        # APIから新しく取得
        new_data = self.fetch_weather(float(lat), float(lon))
        if new_data:
            _weather_cache = new_data
            _weather_cache_time = now
            return _weather_cache
        else:
            # 取得失敗時は前回のキャッシュを返す (あれば)
            if _weather_cache is not None:
                print("  - [Weather] 天気情報の再取得に失敗したため、以前のキャッシュを継続して使用します。")
                return _weather_cache
            return None

    def get_enhanced_season(self, temp: float, month: int) -> Tuple[str, str]:
        """
        気温と月に基づいて、きめ細やかな体感季節（日本語名, 英語名）を判定する。
        戻り値: (季節名_日本語, 季節名_英語)
        """
        # 月ベースの初期英語判定
        if month in [3, 4, 5]:
            base = "spring"
        elif month in [6, 7, 8]:
            base = "summer"
        elif month in [9, 10, 11]:
            base = "autumn"
        else:
            base = "winter"

        # 気温ベースの補正
        if base == "spring":
            if temp < 10.0:
                return "早春", "early_spring"
            elif temp >= 20.0:
                return "初夏", "early_summer"
            else:
                return "春", "spring"
        elif base == "summer":
            if temp < 25.0:
                return "初夏", "early_summer"
            else:
                return "盛夏", "summer"
        elif base == "autumn":
            if temp >= 25.0:
                return "残暑", "late_summer"
            elif temp >= 15.0:
                return "秋", "autumn"
            else:
                return "晩秋", "late_autumn"
        else:  # winter
            if temp >= 10.0:
                return "晩秋", "late_autumn"
            else:
                return "冬", "winter"

    def get_enhanced_time_of_day(self, now_time: datetime.time, sunrise_str: str, sunset_str: str) -> Tuple[str, str]:
        """
        日出・日没時刻に基づいて、動的な時間帯（日本語名, 英語名）を判定する。
        戻り値: (時間帯名_日本語, 時間帯名_英語)
        """
        try:
            # 時刻文字列 "HH:MM" を datetime.time に変換
            sr_h, sr_m = map(int, sunrise_str.split(":"))
            ss_h, ss_m = map(int, sunset_str.split(":"))
            
            # 本日の日付と組み合わせて datetime 基準で計算できるようにする
            today = datetime.date.today()
            sunrise_dt = datetime.datetime.combine(today, datetime.time(sr_h, sr_m))
            sunset_dt = datetime.datetime.combine(today, datetime.time(ss_h, ss_m))
            now_dt = datetime.datetime.combine(today, now_time)
        except Exception as e:
            # パース失敗時は従来の固定時間判定にフォールバック
            print(f"  - [Weather] 日出日没のパース失敗 ({e})。固定時間判定を使用します。")
            return self._get_fallback_time_of_day(now_time.hour)

        # 境界判定 (差分を計算)
        # 1. 早朝: 日出前1時間 〜 日出
        if (sunrise_dt - datetime.timedelta(hours=1)) <= now_dt < sunrise_dt:
            return "早朝", "early_morning"
        
        # 2. 朝: 日出 〜 日出 + 3時間
        elif sunrise_dt <= now_dt < (sunrise_dt + datetime.timedelta(hours=3)):
            return "朝", "morning"
        
        # 3. 昼前: 日出 + 3時間 〜 正午
        elif (sunrise_dt + datetime.timedelta(hours=3)) <= now_dt < datetime.datetime.combine(today, datetime.time(12, 0)):
            return "昼前", "late_morning"
        
        # 4. 昼下がり: 正午 〜 日没 - 2時間
        elif datetime.datetime.combine(today, datetime.time(12, 0)) <= now_dt < (sunset_dt - datetime.timedelta(hours=2)):
            return "昼下がり", "afternoon"
        
        # 5. 夕方: 日没 - 2時間 〜 日没 + 30分
        elif (sunset_dt - datetime.timedelta(hours=2)) <= now_dt < (sunset_dt + datetime.timedelta(minutes=30)):
            return "夕方", "evening"
        
        # 6. 夜: 日没 + 30分 〜 23:00
        elif (sunset_dt + datetime.timedelta(minutes=30)) <= now_dt < datetime.datetime.combine(today, datetime.time(23, 0)):
            return "夜", "night"
        
        # 7. 深夜: それ以外 (23:00 〜 日出前1時間)
        else:
            return "深夜", "midnight"

    def _get_fallback_time_of_day(self, hour: int) -> Tuple[str, str]:
        """固定時刻ベースのフォールバック判定"""
        if 4 <= hour < 6:
            return "早朝", "early_morning"
        elif 6 <= hour < 10:
            return "朝", "morning"
        elif 10 <= hour < 12:
            return "昼前", "late_morning"
        elif 12 <= hour < 16:
            return "昼下がり", "afternoon"
        elif 16 <= hour < 19:
            return "夕方", "evening"
        elif 19 <= hour < 23:
            return "夜", "night"
        else:
            return "深夜", "midnight"
