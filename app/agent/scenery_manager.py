import os
import datetime
import hashlib
import json
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Tuple, Optional
from google.api_core import exceptions as google_exceptions

import constants
import utils
import config_manager
from llm_factory import LLMFactory
from room_manager import get_world_settings_path


@dataclass(frozen=True)
class SceneryRefreshIdentity:
    """情景生成結果を現在のroom状態へ結び付ける不変identity。"""

    fingerprint: str
    season_en: str
    time_of_day_en: str
    location_name: str


_SCENERY_IDENTITY_VERSION = 1
_SCENERY_IDENTITY_MISSING = {"exists": False, "sha256": None}
_WEATHER_SNAPSHOT_FIELDS = (
    "temperature",
    "apparent_temperature",
    "weather_code",
    "weather_description",
    "humidity",
    "precipitation",
    "wind_speed",
    "is_day",
    "sunrise",
    "sunset",
    "fetched_at",
)


def _safe_scenery_room_dir(room_name: str) -> Optional[Path]:
    """room名をcharacters配下へ限定し、既存ディレクトリだけを返す。"""
    if not isinstance(room_name, str) or not room_name or room_name != room_name.strip():
        return None
    if "\x00" in room_name or "/" in room_name or "\\" in room_name:
        return None
    if room_name in {".", ".."}:
        return None

    try:
        rooms_root = Path(constants.ROOMS_DIR).resolve()
        room_dir = (rooms_root / room_name).resolve()
        room_dir.relative_to(rooms_root)
    except (OSError, RuntimeError, ValueError):
        return None

    if room_dir == rooms_root or not room_dir.is_dir():
        return None
    return room_dir


def _read_scenery_identity_file(
    path: Path,
    *,
    allowed_root: Optional[Path] = None,
    parse_json: bool = False,
) -> Optional[Tuple[Dict[str, Any], Any]]:
    """ファイルを安定して読み、内容をログへ出さずhashと値を返す。

    欠落は ``exists=False`` として有効な状態にする。一方、読取中の変更、
    decode失敗、JSON破損はidentityを作れないためNoneを返す。
    """
    try:
        resolved_path = path.resolve(strict=False)
        if allowed_root is not None:
            resolved_path.relative_to(allowed_root)

        if not path.exists():
            return dict(_SCENERY_IDENTITY_MISSING), None
        if not path.is_file():
            return None

        before = path.stat()
        with path.open("rb") as file_obj:
            raw = file_obj.read()
        after = path.stat()
        if (
            before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            return None

        content = raw.decode("utf-8")
        parsed: Any = content
        if parse_json:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                return None

        return {
            "exists": True,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }, parsed
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError, ValueError):
        return None


def _read_scenery_global_config() -> Optional[dict]:
    """config.jsonを復元処理なしで読み取る。"""
    try:
        candidates = [Path(constants.CONFIG_FILE), Path("..") / str(constants.CONFIG_FILE)]
        for candidate in candidates:
            if candidate.exists():
                checked = _read_scenery_identity_file(candidate, parse_json=True)
                if checked is None:
                    return None
                config = checked[1]
                return config if isinstance(config, dict) else None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return {}


def _scenery_weather_snapshot(weather_data: Any) -> Optional[Dict[str, Any]]:
    if weather_data is None:
        return None
    snapshot: Dict[str, Any] = {}
    try:
        for field_name in _WEATHER_SNAPSHOT_FIELDS:
            value = getattr(weather_data, field_name)
            if not isinstance(value, (str, int, float, bool, type(None))):
                value = str(value)
            snapshot[field_name] = value
    except (AttributeError, TypeError, ValueError):
        return None
    return snapshot


def _scenery_location_area(world_text: Optional[str], location_name: str) -> Optional[str]:
    """generatorと同じ先頭一致規則で、現在地のareaを求める。"""
    if world_text is None:
        return None
    current_area: Optional[str] = None
    try:
        for line in world_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                current_area = stripped[3:].strip()
            elif stripped.startswith("### ") and stripped[4:].strip() == location_name:
                return current_area
    except (AttributeError, TypeError):
        return None
    return None


def get_scenery_refresh_identity(room_name: str) -> Optional[SceneryRefreshIdentity]:
    """現在の情景入力を読み取り、生成結果適用用のfingerprintを作る。

    本関数は生成・cache書込み・ItemManager構築・room初期化・network取得を行わない。
    天候は既存のメモリcacheのみ参照し、入力ファイルの欠落は状態としてhashへ含める。
    背景の入力となる一時的現在地は含め、生成した情景cache自身は含めない。
    """
    room_dir = _safe_scenery_room_dir(room_name)
    if room_dir is None:
        return None

    world_path = room_dir / "spaces" / "world_settings.txt"
    room_config_path = room_dir / "room_config.json"
    current_location_path = room_dir / "current_location.txt"
    placed_items_path = room_dir / "items" / "placed_items.json"

    world_snapshot = _read_scenery_identity_file(world_path, allowed_root=room_dir)
    room_config_snapshot = _read_scenery_identity_file(
        room_config_path,
        allowed_root=room_dir,
        parse_json=True,
    )
    current_location_snapshot = _read_scenery_identity_file(
        current_location_path,
        allowed_root=room_dir,
    )
    placed_items_snapshot = _read_scenery_identity_file(
        placed_items_path,
        allowed_root=room_dir,
        parse_json=True,
    )
    temporary_location_snapshot = _read_scenery_identity_file(
        room_dir / "cache" / "temporary_location.json", allowed_root=room_dir, parse_json=True,
    )
    if any(
        snapshot is None
        for snapshot in (
            world_snapshot,
            room_config_snapshot,
            current_location_snapshot,
            placed_items_snapshot,
            temporary_location_snapshot,
        )
    ):
        return None

    assert world_snapshot is not None
    assert room_config_snapshot is not None
    assert current_location_snapshot is not None
    assert placed_items_snapshot is not None
    assert temporary_location_snapshot is not None
    room_config = room_config_snapshot[1] if room_config_snapshot[0]["exists"] else {}
    room_config = room_config if isinstance(room_config, dict) else {}
    location_text = current_location_snapshot[1] if current_location_snapshot[0]["exists"] else None
    location_name = str(location_text or "").strip() or "リビング"
    world_text = world_snapshot[1] if world_snapshot[0]["exists"] else None
    location_area = _scenery_location_area(world_text, location_name)

    config = _read_scenery_global_config()
    if config is None:
        return None
    weather_settings = config.get("weather_settings", {})
    if not isinstance(weather_settings, dict):
        weather_settings = {}
    enable_scenery = bool(weather_settings.get("enable_scenery_reflection", False))
    latitude = weather_settings.get("latitude")
    longitude = weather_settings.get("longitude")

    weather_data = None
    if enable_scenery and latitude is not None and longitude is not None:
        try:
            from weather_service import WeatherService

            weather_data = WeatherService().get_cached_weather(cache_only=True)
        except Exception:
            return None
    weather_snapshot = _scenery_weather_snapshot(weather_data)
    if weather_data is not None and weather_snapshot is None:
        return None

    try:
        now = datetime.datetime.now()
        default_season = utils.get_season(now.month)
        default_time = utils.get_time_of_day(now.hour)
        settings = room_config.get("time_settings", {})
        if not isinstance(settings, dict) or not settings:
            override_settings = room_config.get("override_settings", {}) or {}
            nested_settings = override_settings.get("time_settings", {})
            settings = nested_settings if isinstance(nested_settings, dict) else {}

        mode = settings.get("mode", "realtime")
        season_en = default_season
        time_of_day_en = default_time
        if mode == "realtime" and weather_data is not None:
            from weather_service import WeatherService

            service = WeatherService()
            _, enhanced_season = service.get_enhanced_season(weather_data.temperature, now.month)
            _, enhanced_time = service.get_enhanced_time_of_day(
                now.time(), weather_data.sunrise, weather_data.sunset
            )
            season_en = enhanced_season or season_en
            time_of_day_en = enhanced_time or time_of_day_en
        elif mode == "fixed":
            season_en = settings.get("fixed_season", default_season)
            time_of_day_en = settings.get("fixed_time_of_day", default_time)

        if not isinstance(season_en, str) or not isinstance(time_of_day_en, str):
            return None

        fingerprint_payload = {
            "version": _SCENERY_IDENTITY_VERSION,
            "room_name": room_name,
            "location_name": location_name,
            "location_area": location_area,
            "season_en": season_en,
            "time_of_day_en": time_of_day_en,
            "files": {
                "world_settings": world_snapshot[0],
                "room_config": room_config_snapshot[0],
                "current_location": current_location_snapshot[0],
                "placed_items": placed_items_snapshot[0],
                "temporary_location": temporary_location_snapshot[0],
            },
            "global_presentation": {
                key: value for key, value in config.items()
                if key.startswith("theme_") or key in {
                    "room_theme_enabled", "font_size", "line_height", "chat_style", "enable_scenery_system",
                }
            },
            "weather": {
                "enabled": enable_scenery,
                "latitude": latitude,
                "longitude": longitude,
                "snapshot": weather_snapshot,
            },
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return SceneryRefreshIdentity(
            fingerprint=fingerprint,
            season_en=season_en,
            time_of_day_en=time_of_day_en,
            location_name=location_name,
        )
    except (AttributeError, TypeError, ValueError, OSError):
        return None


def _scenery_commit_allowed(commit_guard: Optional[Callable[[], bool]]) -> bool:
    if commit_guard is None:
        return True
    try:
        return bool(commit_guard() if callable(commit_guard) else commit_guard)
    except Exception:
        return False


def generate_scenery_context(
    room_name: str, 
    api_key: str, 
    force_regenerate: bool = False, 
    season_en: 'Optional[str]' = None, 
    time_of_day_en: 'Optional[str]' = None,
    *,
    commit_guard: Optional[Callable[[], bool]] = None,
) -> Optional[Tuple[str, str, str]]:
    scenery_text = "（現在の場所の情景描写は、取得できませんでした）"
    space_def = "（現在の場所の定義・設定は、取得できませんでした）"
    location_display_name = "（不明な場所）"
    if not _scenery_commit_allowed(commit_guard):
        return None
    try:
        current_location_name = utils.get_current_location(room_name)
        if not current_location_name:
            current_location_name = "リビング"
            location_display_name = "リビング"

        world_settings_path = get_world_settings_path(room_name)
        world_data = utils.parse_world_file(world_settings_path)
        found_location = False
        for area, places in world_data.items():
            if current_location_name in places:
                space_def = places[current_location_name]
                location_display_name = f"[{area}] {current_location_name}"
                found_location = True
                break
        if not found_location:
            space_def = f"（場所「{current_location_name}」の定義が見つかりません）"

        try:
            from src.features.item_manager import ItemManager
            im = ItemManager(room_name)
            placed_items = im.list_placed_items(room_name, current_location_name)
            if placed_items:
                item_details = []
                for it in placed_items:
                    detail = f"{it.get('name')} x{it.get('amount')}"
                    if it.get("placed_at_furniture"):
                        detail += f" ({it.get('placed_at_furniture')}にある)"
                    item_details.append(detail)
                
                items_str = "、".join(item_details)
                space_def += f"\n\n現在、この場所には以下のアイテムが置かれています：{items_str}。"
        except Exception as e:
            # アイテム情報の取得失敗は致命的ではないため、ログのみ残す
            print(f"--- [Scenery Warning] アイテム情報の取得に失敗しました: {e} ---")

        from utils import get_season, get_time_of_day, load_scenery_cache, save_scenery_cache
        import hashlib
        import datetime
        from weather_service import WeatherService

        now = datetime.datetime.now()
        
        # 設定のロード
        config = config_manager.load_config_file()
        weather_settings = config.get("weather_settings", {})
        enable_scenery = weather_settings.get("enable_scenery_reflection", False)
        
        weather_data = None
        weather_code = "none"
        enhanced_season_en = None
        enhanced_time_en = None
        
        if enable_scenery:
            try:
                lat = weather_settings.get("latitude")
                lon = weather_settings.get("longitude")
                if lat is not None and lon is not None:
                    service = WeatherService()
                    # キャッシュから優先取得
                    weather_data = service.get_cached_weather()
                        
                    if weather_data:
                        weather_code = str(weather_data.weather_code)
                        # 動的な体感季節・日出日没ベースの時間帯を判定
                        _, enhanced_season_en = service.get_enhanced_season(weather_data.temperature, now.month)
                        _, enhanced_time_en = service.get_enhanced_time_of_day(now.time(), weather_data.sunrise, weather_data.sunset)
            except Exception as we:
                print(f"--- [Scenery Weather Warning] 天気データ取得失敗 (フォールバックします): {we} ---")

        # 外部指定がない場合は、動的判定 -> 従来判定の順でフォールバック
        effective_season = season_en or enhanced_season_en or get_season(now.month)
        effective_time_of_day = time_of_day_en or enhanced_time_en or get_time_of_day(now.hour)

        content_hash = hashlib.md5(space_def.encode('utf-8')).hexdigest()[:8]
        
        # キャッシュキー設計: 天気設定が有効なら天気コードを付加する
        if enable_scenery and weather_code != "none":
            cache_key = f"{current_location_name}_{content_hash}_{effective_season}_{effective_time_of_day}_{weather_code}"
        else:
            cache_key = f"{current_location_name}_{content_hash}_{effective_season}_{effective_time_of_day}"

        # プレースホルダーキー（未設定状態）の判定
        is_placeholder_key = not api_key or api_key == "YOUR_API_KEY_HERE" or api_key.startswith("AIzaSyB-") # デフォルト同梱の期限切れキー等

        if is_placeholder_key:
            from gemini_connection import is_vertex
            is_placeholder_key = not is_vertex()

        if not force_regenerate:
            scenery_cache = load_scenery_cache(room_name)
            # 1. 完全一致キャッシュを確認
            if cache_key in scenery_cache:
                cached_data = scenery_cache[cache_key]
                # print(f"--- [有効な情景キャッシュを発見] ({cache_key})。APIコールをスキップします ---")
                if not _scenery_commit_allowed(commit_guard):
                    return None
                cached_text = utils.get_content_as_string(cached_data["scenery_text"])
                if not _scenery_commit_allowed(commit_guard):
                    return None
                return location_display_name, space_def, cached_text
            
            # 2. 未設定状態なら、その場所の他のキャッシュを優先して探す（オンボーディング体験の保護）
            if is_placeholder_key:
                # [v10] キャッシュ流用の高度化：現在の季節・時間帯を尊重する
                # まずは「場所_ハッシュ_季節」まで一致するものを探す
                location_hash_season_prefix = f"{current_location_name}_{content_hash}_{effective_season}_"
                for k, v in scenery_cache.items():
                    if k.startswith(location_hash_season_prefix):
                        if not _scenery_commit_allowed(commit_guard):
                            return None
                        return location_display_name, space_def, v["scenery_text"]
                
                # 次に「場所_ハッシュ」が一致するものを探す（季節違いでも場所が同じなら採用）
                location_hash_prefix = f"{current_location_name}_{content_hash}_"
                for k, v in scenery_cache.items():
                    if k.startswith(location_hash_prefix):
                        if not _scenery_commit_allowed(commit_guard):
                            return None
                        return location_display_name, space_def, v["scenery_text"]
                
                # 最後に、ハッシュが含まれない古い形式（場所名のみ一致）でもあれば返す
                location_old_prefix = f"{current_location_name}_"
                for k, v in scenery_cache.items():
                    if k.startswith(location_old_prefix):
                        if not _scenery_commit_allowed(commit_guard):
                            return None
                        return location_display_name, space_def, v["scenery_text"]

        if not space_def.startswith("（"):
            try:
                if not _scenery_commit_allowed(commit_guard):
                    return None
                # プロンプトの構築
                season_map_en_to_ja = {
                    "spring": "春", "early_spring": "早春",
                    "summer": "夏", "early_summer": "初夏", "late_summer": "残暑",
                    "autumn": "秋", "late_autumn": "晩秋",
                    "winter": "冬"
                }
                season_ja = season_map_en_to_ja.get(effective_season, "不明な季節")
                
                time_map_en_to_ja = {
                    "early_morning": "早朝", "morning": "朝", "late_morning": "昼前",
                    "afternoon": "昼下がり", "evening": "夕方", "night": "夜", "midnight": "深夜"
                }
                time_of_day_ja = time_map_en_to_ja.get(effective_time_of_day, "不明な時間帯")

                # 天気プロンプトの構成
                weather_info_prompt = ""
                if weather_data and enable_scenery:
                    weather_info_prompt = (
                        f"【情報源3：現在のリアル天気（居住地連動）】\n"
                        f"- 天気: {weather_data.weather_description}\n"
                        f"- 気温: {weather_data.temperature:.1f}℃ (体感気温: {weather_data.apparent_temperature:.1f}℃)\n"
                        f"- 湿度: {weather_data.humidity}%\n"
                        f"- 降水量: {weather_data.precipitation}mm\n\n"
                        f"※窓の外に見える空の色や光の差し込み方、部屋を満たす空気感（暖かい、肌寒い、湿っているなど）に、このリアル天気のニュアンスを自然かつ豊かに織り交ぜて描写してください。\n"
                    )

                scenery_prompt = (
                    "あなたは、与えられた情報源から、一つのまとまった情景を描き出す情景描写の専門家です。\n\n"
                    f"【情報源1：時間・季節】\n- 時間帯: {time_of_day_ja}\n- 季節: {season_ja}\n\n"
                    f"{weather_info_prompt}"
                    f"【情報源2：空間設定と設置アイテム】\n---\n{space_def}\n---\n\n"
                    "【あなたのタスク】\n"
                    "情報源を統合し、その場のリアルな雰囲気を伝える**最終的な情景描写の文章のみを、2〜3文で生成してください。**\n\n"
                    "【重点指示】\n"
                    "- **設置アイテムの描写**: 「現在、この場所には〜」の後に続くアイテム情報は、その空間の重要なアクセントです。\n"
                    "- **必ず、設置されている全てのアイテムを、その場所（例：テーブルの上など）を含めて描写に盛り込んでください。**\n"
                    "- 複数のアイテムがある場合は、それらが共存している様子を一文の中に自然に組み込んでください。\n\n"
                    "【厳守すべきルール】\n"
                    "- **あなたの思考過程や判断理由は、絶対に出力に含めないでください。**\n"
                    "- 具体的な時刻（例：「23時42分」）は文章に含めないでください。\n"
                    "- 人物やキャラクターの描写は絶対に含めないでください。\n"
                    "- 五感に訴えかける、**空気感まで伝わるような**精緻で写実的な描写を重視してください。\n\n"
                    "【冷暖房・熱源設備の描写制御ルール】\n"
                    "- 空間設定（情報源2）に「暖炉」「ストーブ」「ヒーター」などの暖房熱源が描写されている場合：\n"
                    "  - 気温が20.0℃以上、あるいは体感季節が「夏」「初夏」「残暑」の時は、それらは絶対に「火は消えて静かに冷めている」「休止している」と火が消えた冷たい状態として描写してください（夏場に暖炉が燃え盛る描写は厳禁です）。\n"
                    "  - 気温が15.0℃未満、あるいは体感季節が「冬」「晩秋」「早春」の時は、「赤々と火が灯っている」「心地よい温もりをもたらしている」と稼働している様子を描写してください。\n"
                    "- 空間設定に「エアコン」「扇風機」「クーラー」などが描写されている場合：\n"
                    "  - 気温や季節に応じて、適切に「涼しい風を送り出している」「暖かな空気を循環させている」、あるいは穏やかな気候の時は「静かにオフになっている（佇んでいる）」と自然に稼働状態を切り替えて描写してください。"
                )

                # LLMFactoryの統合的なリトライ・ローテーション機能を呼び出す
                response, _ = LLMFactory.invoke_internal_llm(
                    internal_role="summarization",
                    prompt=scenery_prompt,
                    room_name=room_name,
                    api_key=api_key
                )
                
                scenery_text = utils.get_content_as_string(response.content)
                if not _scenery_commit_allowed(commit_guard):
                    return None
                save_scenery_cache_result = save_scenery_cache(
                    room_name,
                    cache_key,
                    location_display_name,
                    scenery_text,
                    commit_guard=commit_guard,
                )
                if commit_guard is not None and not save_scenery_cache_result:
                    return None

            except Exception as e:
                # すべてのリトライが失敗した場合
                # 非同期更新の失敗では表示中の情景を維持する。
                # 既存の手動呼出しだけ、従来の別時間帯cache fallbackを使う。
                if commit_guard is not None:
                    return None
                print(f"--- [Scenery Error] 全てのリトライが失敗しました: {e} ---")
                if not _scenery_commit_allowed(commit_guard):
                    return None
                
                # 最終手段：他時間帯のキャッシュがあればそれを出して取り繕う
                try:
                    scenery_cache = load_scenery_cache(room_name)
                    location_prefix = f"{current_location_name}_{content_hash}_"
                    for k, v in scenery_cache.items():
                        if k.startswith(location_prefix):
                            print(f"  - [Scenery Recovery] エラー発生につき、別時間帯のキャッシュを代用します ({k})")
                            if not _scenery_commit_allowed(commit_guard):
                                return None
                            return location_display_name, space_def, v["scenery_text"]
                except: pass

                location_display_name = "（エラー）"
                scenery_text = "（情景描写の生成中にエラーが発生しました）"
                space_def = "（エラー）"
        else:
            scenery_text = "（場所の定義がないため、情景を描写できません）"

    except Exception as e:
        if commit_guard is not None:
            return None
        print(f"--- [Scenery Critical Error] 情景描写プロセスで予期せぬエラーが発生しました ---\n{traceback.format_exc()}")
        if not _scenery_commit_allowed(commit_guard):
            return None
        scenery_text = "（システムエラーにより情景描写を取得できませんでした）"

    if not _scenery_commit_allowed(commit_guard):
        return None
    final_text = utils.get_content_as_string(scenery_text)
    if not _scenery_commit_allowed(commit_guard):
        return None
    return location_display_name, space_def, final_text
