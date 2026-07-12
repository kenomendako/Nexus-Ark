# google_calendar_service.py
"""
Googleカレンダーのバックグラウンド差分同期とローカルキャッシュを担うサービス。
（計画書フェーズ2 / 5.1・5.2節に対応）

主な責務:
- syncToken による差分同期（410 Gone 時はフル同期へフォールバック）
- ローリングウィンドウ（既定: 過去1日〜先2週間）でのキャッシュ剪定
- reminders.useDefault の二段解決（overrides → カレンダー既定 default_reminders）
- 終日イベント・タイムゾーンの正規化
- file_lock_utils によるアトミックなキャッシュ入出力

設計上の注意:
- 本モジュールは「同期とキャッシュ」までを担う独立基盤であり、
  コンテキスト注入・アラーム登録・UI配線は後続フェーズ（並行開発の着地後にrebaseして実装）で行う。
- 重い依存（google系・他マネージャ）はすべて関数内で遅延インポートし、
  純粋なロジック（正規化・剪定・範囲抽出）はライブラリ非依存で単体テスト可能にする。
"""

import datetime
import os
import threading
from typing import Optional, List, Dict, Any, Tuple

# ローリングウィンドウの既定値（設定で上書き可能）
DEFAULT_PAST_DAYS = 1
DEFAULT_FUTURE_DAYS = 14

# キャッシュファイル名（保存先ディレクトリは constants.METADATA_DIR を遅延参照）
_CACHE_FILENAME = "calendar_cache.json"


# =====================================================================
# 設定・パスのヘルパー
# =====================================================================

def _get_settings() -> Dict[str, Any]:
    """グローバルのカレンダー設定を防御的に読み込む（未設定なら空辞書）。"""
    try:
        import config_manager
        config = config_manager.load_config_file()
        return config.get("google_calendar_settings", {}) or {}
    except Exception as e:  # noqa: BLE001
        print(f"--- [Calendar] 設定の読み込みに失敗（空設定で続行）: {e} ---")
        return {}


def _cache_path() -> str:
    import constants
    return os.path.join(constants.METADATA_DIR, _CACHE_FILENAME)


def load_cache() -> Dict[str, Any]:
    """ローカルキャッシュをアトミックに読み込む。存在しなければ空構造を返す。"""
    from file_lock_utils import safe_json_read
    default = {"last_synced_at": None, "calendars": {}}
    data = safe_json_read(_cache_path(), default=default)
    if not isinstance(data, dict) or "calendars" not in data:
        return default
    return data


def save_cache(cache: Dict[str, Any]) -> bool:
    """ローカルキャッシュをアトミックに書き込む。"""
    from file_lock_utils import safe_json_write
    cache_path = _cache_path()
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    return safe_json_write(cache_path, cache)


# =====================================================================
# 純粋ロジック（ライブラリ非依存・単体テスト可能）
# =====================================================================

def _parse_dt(value: str) -> Optional[datetime.datetime]:
    """ISO8601文字列を timezone-aware な datetime に変換する。失敗時 None。"""
    if not value:
        return None
    try:
        # 'Z' 終端を +00:00 に正規化（Python 3.10系の fromisoformat 互換のため）
        normalized = value.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _resolve_reminders(raw: Dict[str, Any],
                       default_reminders: List[Dict[str, Any]]) -> Tuple[bool, bool, List[int]]:
    """
    イベントの通知設定を二段で解決する（計画書アイデアB）。
    1. reminders.overrides があればその分数を採用。
    2. reminders.useDefault が真なら、所属カレンダーの default_reminders の分数を採用。

    Returns:
        (has_reminders, use_default, minutes)
    """
    reminders = raw.get("reminders") or {}
    overrides = reminders.get("overrides")
    use_default = bool(reminders.get("useDefault"))

    if overrides:
        minutes = [int(o.get("minutes", 0)) for o in overrides if o.get("minutes") is not None]
        return (len(minutes) > 0, False, sorted(set(minutes)))

    if use_default:
        minutes = [int(o.get("minutes", 0)) for o in (default_reminders or []) if o.get("minutes") is not None]
        return (len(minutes) > 0, True, sorted(set(minutes)))

    return (False, False, [])


def _normalize_event(raw: Dict[str, Any],
                     default_reminders: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Google APIの生イベントを、キャッシュスキーマ（計画書5.2節）に正規化する。
    終日イベント（date）と時刻付きイベント（dateTime）を区別する。
    """
    start = raw.get("start", {}) or {}
    end = raw.get("end", {}) or {}
    is_all_day = "date" in start  # 終日イベントは dateTime ではなく date を持つ

    has_reminders, use_default, minutes = _resolve_reminders(raw, default_reminders)

    event: Dict[str, Any] = {
        "id": raw.get("id"),
        "summary": raw.get("summary", "(無題)"),
        "is_all_day": is_all_day,
        "has_reminders": has_reminders,
        "reminder_use_default": use_default,
        "reminder_minutes": minutes,
        "visibility": raw.get("visibility", "default"),
        "status": raw.get("status", "confirmed"),
        "description": raw.get("description", ""),
        "location": raw.get("location", ""),
    }
    if is_all_day:
        event["start_date"] = start.get("date")
        event["end_date"] = end.get("date")
    else:
        event["start"] = start.get("dateTime")
        event["end"] = end.get("dateTime")
    return event


def _parse_all_day(date_str: str, tz, is_end: bool) -> Optional[datetime.datetime]:
    """
    終日イベントの日付（YYYY-MM-DD）を tz-aware な datetime にする。
    - tz が None のときは UTC で解釈する（剪定など日跨ぎ精度が不要な用途向け）。
    - Google の終日 end.date は「排他的（最終日の翌日0:00）」なので、終端は
      1マイクロ秒引いて「最終日の最後の瞬間」に補正する（翌日へのリークを防ぐ）。
    """
    if not date_str:
        return None
    try:
        y, m, d = (int(x) for x in date_str.split("-"))
    except (ValueError, AttributeError):
        return None
    base_tz = tz or datetime.timezone.utc
    naive = datetime.datetime(y, m, d, 0, 0, 0)
    if hasattr(base_tz, "localize"):  # pytz
        dt = base_tz.localize(naive)
    else:
        dt = naive.replace(tzinfo=base_tz)
    if is_end:
        dt = dt - datetime.timedelta(microseconds=1)
    return dt


def _event_bounds(event: Dict[str, Any],
                  tz=None) -> Tuple[Optional[datetime.datetime], Optional[datetime.datetime]]:
    """
    イベントの開始・終了を timezone-aware な datetime で返す（剪定・範囲判定用）。
    終日イベントは tz を指定するとその tz の暦日として解釈し、終端は排他補正する。
    """
    if event.get("is_all_day"):
        start = _parse_all_day(event.get("start_date"), tz, is_end=False)
        end = _parse_all_day(event.get("end_date"), tz, is_end=True)
        return start, end
    return _parse_dt(event.get("start")), _parse_dt(event.get("end"))


def _overlaps_window(event: Dict[str, Any],
                     window_start: datetime.datetime,
                     window_end: datetime.datetime,
                     tz=None) -> bool:
    """イベントが [window_start, window_end] と少しでも重なるか。"""
    start, end = _event_bounds(event, tz)
    # 終了が無いイベントは開始のみで判定
    if start is None and end is None:
        return False
    eff_start = start or end
    eff_end = end or start
    return eff_start <= window_end and eff_end >= window_start


def prune_events(events: List[Dict[str, Any]],
                 now: datetime.datetime,
                 past_days: int = DEFAULT_PAST_DAYS,
                 future_days: int = DEFAULT_FUTURE_DAYS) -> List[Dict[str, Any]]:
    """
    ローリングウィンドウ外のイベントを剪定する（計画書5.1節）。
    syncToken は過去・遠未来の変更も返すため、キャッシュ肥大を防ぐ。
    """
    window_start = now - datetime.timedelta(days=past_days)
    window_end = now + datetime.timedelta(days=future_days)
    return [e for e in events if _overlaps_window(e, window_start, window_end)]


def get_events_in_range(cache: Dict[str, Any],
                        start: datetime.datetime,
                        end: datetime.datetime,
                        calendar_ids: Optional[List[str]] = None,
                        tz=None) -> List[Dict[str, Any]]:
    """
    キャッシュから指定範囲に重なるイベントを開始時刻順で返す（読み取りの基本API）。
    calendar_ids を指定すると対象カレンダーを絞る（ルーム個別の表示集合などに使用）。
    tz を指定すると終日イベントをその tz の暦日として解釈する（日跨ぎ精度が要る注入用）。
    """
    out: List[Dict[str, Any]] = []
    for cal_id, cal_state in (cache.get("calendars") or {}).items():
        if calendar_ids is not None and cal_id not in calendar_ids:
            continue
        for event in (cal_state.get("events") or []):
            if _overlaps_window(event, start, end, tz):
                enriched = dict(event)
                enriched["calendar_id"] = cal_id
                out.append(enriched)

    def _sort_key(e: Dict[str, Any]):
        s, _ = _event_bounds(e, tz)
        return (s is None, s or datetime.datetime.max.replace(tzinfo=datetime.timezone.utc))

    out.sort(key=_sort_key)
    return out


def apply_privacy_filter(events: List[Dict[str, Any]],
                         filter_config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    プライバシーフィルタを適用する（計画書アイデアC）。
    除外キーワード（summary/description/location を対象）または visibility=="private" の予定は
    内容をマスクするが、**イベント自体は残す（時間は busy として扱える）**。
    """
    cfg = filter_config or {}
    keywords = [str(k).strip().lower() for k in cfg.get("exclude_keywords", []) if str(k).strip()]
    mask_private = bool(cfg.get("mask_private_events", False))

    out: List[Dict[str, Any]] = []
    for e in events:
        masked = False
        if mask_private and e.get("visibility") == "private":
            masked = True
        if not masked and keywords:
            haystack = " ".join([
                str(e.get("summary", "")), str(e.get("description", "")), str(e.get("location", "")),
            ]).lower()
            if any(k in haystack for k in keywords):
                masked = True

        ev = dict(e)
        if masked:
            ev["summary"] = "（非公開の予定）"
            ev["description"] = ""
            ev["location"] = ""
            ev["masked"] = True
        out.append(ev)
    return out


def compute_free_slots(events: List[Dict[str, Any]],
                       range_start: datetime.datetime,
                       range_end: datetime.datetime) -> List[Tuple[datetime.datetime, datetime.datetime]]:
    """
    指定範囲内の空き時間帯を返す（計画書アイデアE）。
    終日イベントはその日全体を busy として扱う。マスク済みイベントも busy として数える。
    """
    busy: List[Tuple[datetime.datetime, datetime.datetime]] = []
    for e in events:
        s, en = _event_bounds(e)
        if s is None and en is None:
            continue
        bs = s or en
        be = en or s
        # 範囲でクランプ
        bs = max(bs, range_start)
        be = min(be, range_end)
        if bs < be:
            busy.append((bs, be))

    busy.sort()
    merged: List[Tuple[datetime.datetime, datetime.datetime]] = []
    for bs, be in busy:
        if merged and bs <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], be))
        else:
            merged.append((bs, be))

    free: List[Tuple[datetime.datetime, datetime.datetime]] = []
    cursor = range_start
    for bs, be in merged:
        if bs > cursor:
            free.append((cursor, bs))
        cursor = max(cursor, be)
    if cursor < range_end:
        free.append((cursor, range_end))
    return free


# =====================================================================
# ペルソナ状況認識コンテキスト（アイデアA / F / G）
# =====================================================================
#
# 「本日の予定サマリー」は空間描写（scenery）ではなく状況認識の情報なので、
# scenery のシーン記述キャッシュとは完全に分離してここで生成する（計画書アイデアA注記）。
# 純粋ロジック（cache を引数で受ける）として実装し、単体テスト可能にする。

# 注入時の上限（トークン圧迫を避ける）
_SUMMARY_MAX_ITEMS = 5


def _local_hhmm(dt: Optional[datetime.datetime], tz) -> str:
    """tz-aware な datetime をローカルタイムゾーンの HH:MM 文字列にする。"""
    if dt is None:
        return ""
    return dt.astimezone(tz).strftime("%H:%M")


def _format_event_line(event: Dict[str, Any], tz) -> str:
    """1イベントを簡潔な1行にする（終日／時刻付き／開始のみを区別）。"""
    summary = event.get("summary") or "(無題)"
    if event.get("is_all_day"):
        return f"- 終日: {summary}"
    start, end = _event_bounds(event)
    start_s = _local_hhmm(start, tz)
    end_s = _local_hhmm(end, tz)
    if start_s and end_s and end_s != start_s:
        return f"- {start_s}-{end_s} {summary}"
    if start_s:
        return f"- {start_s} {summary}"
    return f"- {summary}"


def _format_day_events(events: List[Dict[str, Any]], tz,
                       max_items: int = _SUMMARY_MAX_ITEMS) -> List[str]:
    """その日のイベント群を行リストに整形する（上限超過は「他N件」で省略）。"""
    lines = [_format_event_line(e, tz) for e in events[:max_items]]
    overflow = len(events) - max_items
    if overflow > 0:
        lines.append(f"- 他 {overflow} 件の予定があります")
    return lines


def _return_prediction_line(events: List[Dict[str, Any]], now: datetime.datetime,
                            tz, within_hours: int = 2) -> Optional[str]:
    """
    アイデアG：現在時刻から within_hours 以内に終了する外出系イベント（終了時刻あり）を検出し、
    最も遅い終了時刻から「帰還が予想されます」の行を作る。
    """
    horizon = now + datetime.timedelta(hours=within_hours)
    latest_end: Optional[datetime.datetime] = None
    for e in events:
        if e.get("is_all_day"):
            continue
        _, end = _event_bounds(e, tz)
        if end is None:
            continue
        if now < end <= horizon:
            if latest_end is None or end > latest_end:
                latest_end = end
    if latest_end is None:
        return None
    return f"→ {_local_hhmm(latest_end, tz)}頃の帰還が予想されます"


def build_schedule_summary(cache: Dict[str, Any],
                           now: datetime.datetime,
                           tz,
                           visible_calendars: Optional[List[str]] = None,
                           privacy_filter: Optional[Dict[str, Any]] = None,
                           include_tomorrow: bool = True,
                           return_prediction: bool = True) -> str:
    """
    本日（と任意で翌日）の予定サマリーを生成する（アイデアA/F/G）。
    マスク済みイベントも busy として残るため、空き時間判定との整合が保たれる。
    """
    local_now = now.astimezone(tz)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + datetime.timedelta(days=1)
    tomorrow_end = today_start + datetime.timedelta(days=2)

    today_events = get_events_in_range(cache, today_start, today_end, visible_calendars, tz)
    today_events = apply_privacy_filter(today_events, privacy_filter)

    parts: List[str] = ["【本日の予定】"]
    if today_events:
        parts.extend(_format_day_events(today_events, tz))
        if return_prediction:
            pred = _return_prediction_line(today_events, now, tz)
            if pred:
                parts.append(pred)
    else:
        parts.append("本日は予定がありません")

    if include_tomorrow:
        tomorrow_events = get_events_in_range(cache, today_end, tomorrow_end, visible_calendars, tz)
        tomorrow_events = apply_privacy_filter(tomorrow_events, privacy_filter)
        if tomorrow_events:
            parts.append("")
            parts.append("【明日の予定】")
            parts.extend(_format_day_events(tomorrow_events, tz))

    return "\n".join(parts)


def get_room_calendar_override(room_name: str) -> Dict[str, Any]:
    """
    ルーム個別のカレンダー設定（override_settings.google_calendar）を取得する。
    未設定キーは既定値（注入OFF・全カレンダー表示・書き込み未設定）で補う。
    """
    defaults = {
        "inject_context": False,          # 既定: 予定サマリーを注入しない
        "visible_calendars": None,        # None = グローバル選択の全カレンダー
        "privacy_filter": None,           # None = グローバル既定フィルタを使用
        "persona_write_calendar_id": "",  # 空 = 書き込み不可
        "persona_write_requires_approval": True,
    }
    try:
        import room_manager
        room_config = room_manager.get_room_config(room_name) or {}
        overrides = (room_config.get("override_settings") or {}).get("google_calendar") or {}
    except Exception as e:  # noqa: BLE001
        print(f"--- [Calendar] ルーム設定の読み込みに失敗（既定で続行）: {e} ---")
        overrides = {}
    merged = dict(defaults)
    for k, v in overrides.items():
        if v is not None:
            merged[k] = v
    return merged


def get_persona_schedule_context(room_name: str,
                                 now: Optional[datetime.datetime] = None) -> Optional[str]:
    """
    ペルソナの状況認識コンテキストへ注入する「本日の予定サマリー」文字列を返す。
    グローバル無効・未認証・ルームの注入OFF の場合は None（注入なし）。
    同期エラー等で取得できない場合は捏造防止のため明示メッセージを返す。
    """
    settings = _get_settings()
    if not settings.get("enabled") or not settings.get("refresh_token"):
        return None

    room_cfg = get_room_calendar_override(room_name)
    if not room_cfg.get("inject_context", False):
        return None

    try:
        import pytz
        tz = pytz.timezone("Asia/Tokyo")
    except Exception:  # noqa: BLE001
        tz = datetime.timezone(datetime.timedelta(hours=9))

    now = now or datetime.datetime.now(datetime.timezone.utc)

    try:
        cache = load_cache()
    except Exception as e:  # noqa: BLE001
        print(f"--- [Calendar] キャッシュ読み込み失敗: {e} ---")
        return "【本日の予定】\nカレンダー情報は現在取得できません。"

    # 表示対象カレンダー: ルーム個別 visible_calendars があればそれ、無ければグローバル選択
    visible = room_cfg.get("visible_calendars")
    if not visible:
        visible = settings.get("selected_calendars") or None

    # プライバシーフィルタ: ルーム個別があれば優先、無ければグローバル既定
    privacy_filter = room_cfg.get("privacy_filter") or settings.get("privacy_filter_default")

    return build_schedule_summary(
        cache, now, tz,
        visible_calendars=visible,
        privacy_filter=privacy_filter,
        include_tomorrow=True,
        return_prediction=bool(settings.get("return_prediction_enabled", True)),
    )


# =====================================================================
# カレンダー由来リマインダーのアラーム連携（アイデアB）
# =====================================================================
#
# 通知が設定された予定のみを対象に、開始時刻のリード分前に既存アラーム機構へ登録する。
# gcal_ 名前空間のアラームだけを追加・削除して調停し、ユーザー設定のアラームには触れない。
# 静音時間帯に重なるリマインダーは抑制する。

def _time_in_quiet_hours(hhmm: str, start_str: str, end_str: str) -> bool:
    """任意の時刻 hhmm が通知禁止時間帯に含まれるか（is_in_quiet_hours は現在時刻専用のため別実装）。"""
    if not start_str or not end_str:
        return False
    try:
        t = datetime.datetime.strptime(hhmm, "%H:%M").time()
        s = datetime.datetime.strptime(start_str, "%H:%M").time()
        e = datetime.datetime.strptime(end_str, "%H:%M").time()
    except ValueError:
        return False
    if s <= e:
        return s <= t <= e
    return t >= s or t <= e


def _reminder_alarm_id(room_name: str, event_id: str) -> str:
    return f"gcal_{room_name}_{event_id}"


def _desired_room_reminders(room_name: str, cache: Dict[str, Any],
                            settings: Dict[str, Any],
                            now: datetime.datetime, tz) -> Dict[str, Dict[str, Any]]:
    """このルームで望ましいカレンダーリマインダー集合 {alarm_id: alarm_dict} を計算する。"""
    room_cfg = get_room_calendar_override(room_name)
    if not room_cfg.get("reminder_enabled", False):
        return {}

    visible = room_cfg.get("visible_calendars") or settings.get("selected_calendars") or None
    privacy_filter = room_cfg.get("privacy_filter") or settings.get("privacy_filter_default")

    # 直近48時間以内に開始する予定のみ（アラーム集合の肥大を防ぐ）
    horizon = now + datetime.timedelta(hours=48)
    events = get_events_in_range(cache, now, horizon, visible, tz)
    events = apply_privacy_filter(events, privacy_filter)

    try:
        import config_manager
        auto = (config_manager.get_effective_settings(room_name).get("autonomous_settings") or {})
        q_start = auto.get("quiet_hours_start", "00:00")
        q_end = auto.get("quiet_hours_end", "07:00")
    except Exception:  # noqa: BLE001
        q_start, q_end = "00:00", "00:00"

    now_local = now.astimezone(tz)
    desired: Dict[str, Dict[str, Any]] = {}
    for e in events:
        if e.get("is_all_day") or not e.get("has_reminders"):
            continue
        minutes = e.get("reminder_minutes") or []
        if not minutes:
            continue
        start, _ = _event_bounds(e, tz)
        if start is None:
            continue
        lead = max(minutes)  # 最も早い通知（最大リード時間）を採用
        remind_dt = (start - datetime.timedelta(minutes=lead)).astimezone(tz)
        if remind_dt <= now_local:
            continue
        hhmm = remind_dt.strftime("%H:%M")
        if _time_in_quiet_hours(hhmm, q_start, q_end):
            continue
        eid = e.get("id")
        if not eid:
            continue
        summary = e.get("summary") or "(無題)"
        start_local = start.astimezone(tz).strftime("%H:%M")
        alarm_id = _reminder_alarm_id(room_name, eid)
        desired[alarm_id] = {
            "id": alarm_id,
            "time": hhmm,
            "date": remind_dt.strftime("%Y-%m-%d"),
            "days": [],
            "character": room_name,
            "context_memo": (f"まもなく予定『{summary}』の時間です（{start_local}開始）。"
                             "ユーザーに自然な言葉で声かけしてください。"),
            "enabled": True,
            "is_emergency": False,
            "source": "google_calendar",
        }
    return desired


def reconcile_calendar_reminders(now: Optional[datetime.datetime] = None) -> None:
    """
    全ルームのカレンダー由来リマインダーを alarms.json と調停する（アイデアB）。
    gcal_ 名前空間のアラームのみを追加・削除し、ユーザー設定のアラームには触れない。
    """
    settings = _get_settings()
    if not settings.get("enabled") or not settings.get("refresh_token"):
        return
    if not settings.get("reminder_sync_enabled", True):
        return
    try:
        import alarm_manager
        import room_manager
        try:
            import pytz
            tz = pytz.timezone("Asia/Tokyo")
        except Exception:  # noqa: BLE001
            tz = datetime.timezone(datetime.timedelta(hours=9))
    except Exception as e:  # noqa: BLE001
        print(f"--- [Calendar] リマインダー調停の初期化に失敗: {e} ---")
        return

    now = now or datetime.datetime.now(datetime.timezone.utc)
    cache = load_cache()

    try:
        rooms = [folder for _disp, folder in room_manager.get_room_list_for_ui()]
    except Exception as e:  # noqa: BLE001
        print(f"--- [Calendar] ルーム一覧の取得に失敗: {e} ---")
        return

    desired_all: Dict[str, Dict[str, Any]] = {}
    for room in rooms:
        try:
            desired_all.update(_desired_room_reminders(room, cache, settings, now, tz))
        except Exception as e:  # noqa: BLE001
            print(f"--- [Calendar] {room} のリマインダー計算に失敗（スキップ）: {e} ---")

    try:
        existing = alarm_manager.load_alarms()
    except Exception as e:  # noqa: BLE001
        print(f"--- [Calendar] アラーム読み込みに失敗: {e} ---")
        return

    # 既存の gcal アラーム（名前空間 or source で判定）
    existing_gcal = {
        a.get("id"): a for a in existing
        if str(a.get("id", "")).startswith("gcal_") or a.get("source") == "google_calendar"
    }

    # 削除: もはや不要、または時刻/日付が変わった gcal アラーム
    for aid, a in existing_gcal.items():
        d = desired_all.get(aid)
        if d is None or d.get("time") != a.get("time") or d.get("date") != a.get("date"):
            try:
                alarm_manager.delete_alarm(aid)
            except Exception as e:  # noqa: BLE001
                print(f"--- [Calendar] 旧リマインダー削除に失敗（無視）: {aid}: {e} ---")

    # 追加: まだ存在しないリマインダー
    current_ids = {a.get("id") for a in alarm_manager.load_alarms()}
    for aid, d in desired_all.items():
        if aid not in current_ids:
            try:
                alarm_manager.add_alarm_entry(d)
            except Exception as e:  # noqa: BLE001
                print(f"--- [Calendar] リマインダー登録に失敗（無視）: {aid}: {e} ---")


# =====================================================================
# 同期サービス（google系を遅延インポート）
# =====================================================================

class GoogleCalendarService:
    """Calendar APIへのアクセスと差分同期を担うサービス。"""

    def __init__(self):
        self._service = None  # 認証済みクライアント（遅延構築）

    def _ensure_service(self) -> Any:
        if self._service is None:
            import google_auth_helper
            s = _get_settings()
            self._service = google_auth_helper.build_calendar_service(
                s.get("client_id", ""),
                s.get("client_secret", ""),
                s.get("refresh_token", ""),
            )
        return self._service

    def list_calendars(self) -> List[Dict[str, Any]]:
        """連携可能なカレンダー一覧を取得する（UIのカレンダー選択に使用）。"""
        service = self._ensure_service()
        calendars: List[Dict[str, Any]] = []
        page_token = None
        while True:
            resp = service.calendarList().list(pageToken=page_token).execute()
            for item in resp.get("items", []):
                calendars.append({
                    "id": item.get("id"),
                    "summary": item.get("summaryOverride") or item.get("summary", ""),
                    "primary": bool(item.get("primary", False)),
                    "default_reminders": item.get("defaultReminders", []),
                })
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return calendars

    def insert_event(self, calendar_id: str, summary: str,
                     start_dt: datetime.datetime, end_dt: datetime.datetime,
                     description: str = "", all_day: bool = False) -> Dict[str, Any]:
        """
        指定カレンダーにイベントを1件作成する（アイデアH）。
        all_day=True のときは終日イベント（date 指定）として作成する。end_dt は排他的
        （最終日の翌日0:00）を渡すこと。呼び出し側で書き込み先の妥当性を検証済みであること。
        """
        service = self._ensure_service()
        if all_day:
            start_body = {"date": start_dt.date().isoformat()}
            end_body = {"date": end_dt.date().isoformat()}  # Google の終日 end.date は排他的
        else:
            start_body = {"dateTime": start_dt.isoformat()}
            end_body = {"dateTime": end_dt.isoformat()}
        body: Dict[str, Any] = {"summary": summary, "start": start_body, "end": end_body}
        if description:
            body["description"] = description
        return service.events().insert(calendarId=calendar_id, body=body).execute()

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        """指定カレンダーのイベントを1件削除する（アイデアH拡張）。"""
        service = self._ensure_service()
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()

    def patch_event(self, calendar_id: str, event_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """指定カレンダーのイベントを部分更新する（アイデアH拡張）。"""
        service = self._ensure_service()
        return service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()

    def find_events(self, calendar_id: str,
                    time_min: datetime.datetime, time_max: datetime.datetime,
                    query: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        指定カレンダーのイベントをAPIから直接取得する（削除・編集の対象特定用）。
        キャッシュではなくAPIを引くため、直前に作成した予定もすぐ対象にできる。
        """
        service = self._ensure_service()
        params: Dict[str, Any] = {
            "calendarId": calendar_id,
            "singleEvents": True,
            "orderBy": "startTime",
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "maxResults": 50,
        }
        if query:
            params["q"] = query
        resp = service.events().list(**params).execute()
        out: List[Dict[str, Any]] = []
        for raw in resp.get("items", []):
            if raw.get("status") == "cancelled":
                continue
            start = raw.get("start", {}) or {}
            end = raw.get("end", {}) or {}
            out.append({
                "id": raw.get("id"),
                "summary": raw.get("summary", "(無題)"),
                "is_all_day": "date" in start,
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
            })
        return out

    def _sync_one_calendar(self, calendar_id: str, cal_state: Dict[str, Any],
                           now: datetime.datetime, _is_retry: bool = False) -> Dict[str, Any]:
        """
        単一カレンダーを同期する。syncToken があれば差分、無ければフル同期。
        410 Gone（syncToken無効）時は syncToken を破棄してフル同期に1度だけフォールバック。
        """
        from googleapiclient.errors import HttpError

        service = self._ensure_service()
        sync_token = cal_state.get("sync_token")
        default_reminders = cal_state.get("default_reminders", [])
        events_by_id: Dict[str, Any] = {
            e["id"]: e for e in cal_state.get("events", []) if e.get("id")
        }

        new_sync_token = None
        page_token = None
        try:
            while True:
                params: Dict[str, Any] = {
                    "calendarId": calendar_id,
                    "singleEvents": True,        # 繰り返しイベントを個別発生に展開
                    "showDeleted": True,         # 差分で削除（cancelled）を検知するため
                    "maxResults": 250,
                }
                if sync_token:
                    params["syncToken"] = sync_token
                else:
                    # フル同期: 初回のみ timeMin で範囲を絞る（timeMin は nextSyncToken と両立する）。
                    # ※ orderBy は付けない。orderBy を含むリクエストには nextSyncToken が返らず、
                    #   差分同期へ移行できず毎回フル同期になってしまうため（読み取り側で並べ替える）。
                    params["timeMin"] = (now - datetime.timedelta(days=DEFAULT_PAST_DAYS)).isoformat()
                if page_token:
                    params["pageToken"] = page_token

                resp = service.events().list(**params).execute()

                # default_reminders はリストレスポンスにも含まれる（useDefault解決に必要）
                if resp.get("defaultReminders") is not None:
                    default_reminders = resp.get("defaultReminders")

                for raw in resp.get("items", []):
                    eid = raw.get("id")
                    if not eid:
                        continue
                    if raw.get("status") == "cancelled":
                        events_by_id.pop(eid, None)
                        continue
                    events_by_id[eid] = _normalize_event(raw, default_reminders)

                page_token = resp.get("nextPageToken")
                if not page_token:
                    new_sync_token = resp.get("nextSyncToken")
                    break

        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if status == 410 and not _is_retry:
                # syncToken無効 → 破棄してフル同期へフォールバック
                print(f"--- [Calendar] syncToken無効（410）。フル同期にフォールバック: {calendar_id} ---")
                reset_state = dict(cal_state)
                reset_state["sync_token"] = None
                reset_state["events"] = []
                return self._sync_one_calendar(calendar_id, reset_state, now, _is_retry=True)
            raise

        events = prune_events(list(events_by_id.values()), now,
                              past_days=DEFAULT_PAST_DAYS, future_days=DEFAULT_FUTURE_DAYS)

        new_state = dict(cal_state)
        new_state["events"] = events
        new_state["default_reminders"] = default_reminders
        if new_sync_token:
            new_state["sync_token"] = new_sync_token
        return new_state

    def sync_all(self, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        """
        設定で選択された全カレンダーを同期し、キャッシュを更新して返す。
        個別カレンダーの失敗は握り潰して他カレンダーの同期を継続する（部分degrade）。
        """
        now = now or datetime.datetime.now(datetime.timezone.utc)
        settings = _get_settings()
        selected = settings.get("selected_calendars") or []
        cache = load_cache()
        calendars = cache.get("calendars") or {}

        for calendar_id in selected:
            cal_state = calendars.get(calendar_id, {})
            try:
                calendars[calendar_id] = self._sync_one_calendar(calendar_id, cal_state, now)
            except Exception as e:  # noqa: BLE001 - 個別失敗で全体を止めない
                print(f"--- [Calendar] カレンダー同期に失敗（スキップ）: {calendar_id}: {e} ---")
                # 既存のキャッシュ状態は保持（古いデータで継続）
                calendars[calendar_id] = cal_state

        cache["calendars"] = calendars
        cache["last_synced_at"] = now.isoformat()
        save_cache(cache)
        return cache


# =====================================================================
# バックグラウンド同期スケジューラ（独立スレッド）
# =====================================================================

_sync_thread: Optional[threading.Thread] = None
_sync_stop_event = threading.Event()


def _is_sync_due(settings: Dict[str, Any], cache: Dict[str, Any],
                 now: datetime.datetime) -> bool:
    """
    今、同期を実行すべきか判定する。
    無効・未認証・対象カレンダー未選択なら同期しない（安全な no-op）。
    """
    if not settings.get("enabled") or not settings.get("refresh_token"):
        return False
    if not settings.get("selected_calendars"):
        return False
    last = cache.get("last_synced_at")
    if not last:
        return True
    last_dt = _parse_dt(last)
    if last_dt is None:
        return True
    interval = max(5, int(settings.get("sync_interval_minutes") or 30))
    return (now - last_dt) >= datetime.timedelta(minutes=interval)


def run_sync_if_due(now: Optional[datetime.datetime] = None) -> bool:
    """同期が必要なら sync_all を実行する。実行したら True。"""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    settings = _get_settings()
    cache = load_cache()
    if not _is_sync_due(settings, cache, now):
        return False
    try:
        GoogleCalendarService().sync_all(now)
    except Exception as e:  # noqa: BLE001 - 定期同期の失敗で全体を止めない
        print(f"--- [Calendar] 定期同期に失敗（次回リトライ）: {e} ---")
        return False
    # 同期成功後、カレンダー由来リマインダーをアラームと調停する（アイデアB）
    try:
        reconcile_calendar_reminders(now)
    except Exception as e:  # noqa: BLE001 - リマインダー調停の失敗で同期成功を覆さない
        print(f"--- [Calendar] リマインダー調停に失敗（無視）: {e} ---")
    return True


def _sync_loop(check_interval_seconds: int = 60) -> None:
    print("--- [Calendar] カレンダー同期スレッドを開始しました ---")
    while not _sync_stop_event.is_set():
        try:
            run_sync_if_due()
        except Exception as e:  # noqa: BLE001
            print(f"--- [Calendar] 同期ループでエラー: {e} ---")
        # stop が立てば即座に抜ける（responsive shutdown）
        _sync_stop_event.wait(check_interval_seconds)
    print("--- [Calendar] カレンダー同期スレッドを停止しました ---")


def start_calendar_sync_thread() -> None:
    """カレンダー同期スレッドを起動する（多重起動防止）。連携無効時も起動するが中身は no-op。"""
    global _sync_thread
    _sync_stop_event.clear()
    if _sync_thread is None or not _sync_thread.is_alive():
        _sync_thread = threading.Thread(target=_sync_loop, daemon=True)
        _sync_thread.start()


def stop_calendar_sync_thread() -> None:
    """カレンダー同期スレッドの停止を要求する。"""
    global _sync_thread
    if _sync_thread is not None and _sync_thread.is_alive():
        _sync_stop_event.set()
        _sync_thread.join(timeout=5)
