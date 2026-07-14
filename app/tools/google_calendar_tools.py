# tools/google_calendar_tools.py
"""
Googleカレンダー連携のエージェントツール（計画書フェーズ3）。

本フェーズでは安全な**読み取り系**ツールを提供する:
- read_calendar_schedule: ローカルキャッシュから指定日の予定一覧を取得する。
- check_free_time: 指定日時範囲の空き時間を算出して返す。

いずれもローカルキャッシュ（google_calendar_service）のみを参照し、会話の遅延を起こさない。
書き込み系（add_calendar_event）は、ルーム個別の書き込み先カレンダー設定・capability_policy
との統合が必要なため、後続フェーズで追加する。

プライバシーフィルタと表示カレンダーはルーム個別設定を優先し、未設定時だけ
グローバル既定を継承する。
"""

import datetime
from typing import Optional, List, Dict, Any, Tuple

from langchain_core.tools import tool

import google_calendar_service as gcal
import utils


CALENDAR_EVENT_DUPLICATE_SIMILARITY_THRESHOLD = 0.90


def _local_now() -> datetime.datetime:
    """ローカルタイムゾーン付きの現在時刻。"""
    return datetime.datetime.now().astimezone()


def _resolve_date(date: str, base: Optional[datetime.datetime] = None) -> Optional[datetime.date]:
    """'today'/'tomorrow'/'YYYY-MM-DD' を date に解決する。失敗時 None。"""
    base = base or _local_now()
    token = (date or "today").strip().lower()
    if token in ("today", "本日", "今日", ""):
        return base.date()
    if token in ("tomorrow", "明日"):
        return (base + datetime.timedelta(days=1)).date()
    if token in ("yesterday", "昨日"):
        return (base - datetime.timedelta(days=1)).date()
    try:
        return datetime.date.fromisoformat(token)
    except ValueError:
        return None


def _day_bounds(day: datetime.date, tz: datetime.tzinfo) -> Tuple[datetime.datetime, datetime.datetime]:
    """指定日の [00:00, 翌00:00) を tz 付きで返す。"""
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=tz)
    return start, start + datetime.timedelta(days=1)


def _settings_guard() -> Optional[str]:
    """連携が無効/未接続なら、その旨のメッセージを返す。利用可能なら None。"""
    s = gcal._get_settings()
    if not s.get("enabled"):
        return "Googleカレンダー連携は現在無効です（外部接続タブから有効化できます）。"
    if not s.get("refresh_token"):
        return "Googleカレンダーが未認証です（外部接続タブから認証してください）。"
    return None


def _visible_events(room_name: str, start: datetime.datetime, end: datetime.datetime) -> List[Dict[str, Any]]:
    """ルームの読み取り範囲とプライバシーフィルタを適用したイベントを返す。"""
    settings = gcal._get_settings()
    room_cfg = gcal.get_room_calendar_override(room_name)
    cache = gcal.load_cache()
    visible = room_cfg.get("visible_calendars")
    if visible is None:
        visible = settings.get("selected_calendars") or None
    privacy_filter = room_cfg.get("privacy_filter") or settings.get("privacy_filter_default")
    events = gcal.get_events_in_range(cache, start, end, calendar_ids=visible)
    filtered = gcal.apply_privacy_filter(events, privacy_filter)
    gcal.observe_calendar_events(room_name, filtered)
    return filtered


def _fmt_time(dt: Optional[datetime.datetime]) -> str:
    return dt.strftime("%H:%M") if dt else "--:--"


def _format_event_line(event: Dict[str, Any]) -> str:
    summary = event.get("summary", "(無題)")
    if event.get("is_all_day"):
        return f"- 終日: {summary}"
    start, end = gcal._event_bounds(event)
    # ローカルタイムゾーンへ寄せて表示
    if start:
        start = start.astimezone()
    if end:
        end = end.astimezone()
    if start and end:
        return f"- {_fmt_time(start)}-{_fmt_time(end)} {summary}"
    if start:
        return f"- {_fmt_time(start)}- {summary}"
    return f"- {summary}"


def _event_start_matches(event: Dict[str, Any], start_dt: datetime.datetime, *, all_day: bool) -> bool:
    event_start = str(event.get("start") or "")
    if all_day:
        return event_start == start_dt.date().isoformat()
    try:
        parsed = datetime.datetime.fromisoformat(event_start.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=start_dt.tzinfo)
    return parsed.astimezone(start_dt.tzinfo) == start_dt


def _find_duplicate_calendar_event(
    svc: Any,
    calendar_id: str,
    summary: str,
    start_dt: datetime.datetime,
    *,
    all_day: bool,
) -> Optional[Dict[str, Any]]:
    window_end = start_dt + (datetime.timedelta(days=1) if all_day else datetime.timedelta(seconds=1))
    events = svc.find_events(calendar_id, start_dt, window_end)
    best_match: Optional[Dict[str, Any]] = None
    best_similarity = 0.0
    for event in events:
        if not _event_start_matches(event, start_dt, all_day=all_day):
            continue
        similarity = utils.normalized_text_similarity(summary, str(event.get("summary") or ""))
        if similarity >= CALENDAR_EVENT_DUPLICATE_SIMILARITY_THRESHOLD and similarity > best_similarity:
            best_match = event
            best_similarity = similarity
    return best_match


@tool
def read_calendar_schedule(date: str = "today", days: int = 1, room_name: str = "") -> str:
    """
    ローカルにキャッシュされたGoogleカレンダーの予定を取得します。

    date: 'today' / 'tomorrow' / 'yesterday' / 'YYYY-MM-DD' のいずれか（既定: today）。
    days: 取得する日数（既定: 1。2以上で複数日分）。
    room_name: (システムで自動入力)

    予定はバックグラウンド同期されたローカルキャッシュから読み取るため高速です。
    非公開設定の予定は「（非公開の予定）」としてマスクされます。
    """
    guard = _settings_guard()
    if guard:
        return guard

    base = _local_now()
    start_day = _resolve_date(date, base)
    if start_day is None:
        return f"日付の指定『{date}』を解釈できませんでした。'today'/'tomorrow'/'YYYY-MM-DD' を使ってください。"

    try:
        days = max(1, min(int(days), 14))
    except (ValueError, TypeError):
        days = 1

    tz = base.tzinfo
    range_start, _ = _day_bounds(start_day, tz)
    _, range_end = _day_bounds(start_day + datetime.timedelta(days=days - 1), tz)

    events = _visible_events(room_name, range_start, range_end)
    if not events:
        if days == 1:
            return f"{start_day.isoformat()} は予定がありません。"
        return f"{start_day.isoformat()} から {days} 日間に予定はありません。"

    # 日付ごとに見出しを付けてまとめる
    lines: List[str] = []
    current_day_label = None
    for event in events:
        s, _ = gcal._event_bounds(event)
        day_label = (s.astimezone().date().isoformat() if s else start_day.isoformat())
        if days > 1 and day_label != current_day_label:
            lines.append(f"【{day_label}】")
            current_day_label = day_label
        lines.append(_format_event_line(event))

    header = f"{start_day.isoformat()} の予定" if days == 1 else f"{start_day.isoformat()} からの予定（{days}日間）"
    return header + "\n" + "\n".join(lines)


@tool
def check_free_time(date: str = "today", start_time: str = "00:00", end_time: str = "23:59",
                    room_name: str = "") -> str:
    """
    指定した日時範囲の空き時間を算出します。「今日は空いてる？」等への回答に使います。

    date: 'today' / 'tomorrow' / 'YYYY-MM-DD'（既定: today）。
    start_time / end_time: 'HH:MM' 形式（既定: 00:00〜23:59）。
    room_name: (システムで自動入力)

    非公開の予定も「時間は埋まっている」ものとして空き時間に反映されます（内容は伏せます）。
    """
    guard = _settings_guard()
    if guard:
        return guard

    base = _local_now()
    day = _resolve_date(date, base)
    if day is None:
        return f"日付の指定『{date}』を解釈できませんでした。"

    tz = base.tzinfo
    try:
        sh, sm = [int(x) for x in start_time.strip().split(":")]
        eh, em = [int(x) for x in end_time.strip().split(":")]
        range_start = datetime.datetime.combine(day, datetime.time(sh, sm), tzinfo=tz)
        if (eh, em) == (23, 59):
            range_end = datetime.datetime.combine(day, datetime.time.min, tzinfo=tz) + datetime.timedelta(days=1)
        else:
            range_end = datetime.datetime.combine(day, datetime.time(eh, em), tzinfo=tz)
    except (ValueError, TypeError):
        return "時刻は 'HH:MM' 形式で指定してください（例: 14:00）。"

    if range_end <= range_start:
        return "終了時刻は開始時刻より後にしてください。"

    events = _visible_events(room_name, range_start, range_end)
    free = gcal.compute_free_slots(events, range_start, range_end)

    if not free:
        return f"{day.isoformat()} の {start_time}〜{end_time} は予定で埋まっています。"

    slots = [f"- {_fmt_time(s)}〜{_fmt_time(e)}" for s, e in free]
    return f"{day.isoformat()} の空き時間（{start_time}〜{end_time}）:\n" + "\n".join(slots)


def _parse_datetime(value: str, base: datetime.datetime) -> Optional[datetime.datetime]:
    """
    'YYYY-MM-DD HH:MM' / 'YYYY-MM-DDTHH:MM' / ISO8601 をローカルtz付き datetime に解決する。
    タイムゾーン情報が無ければベース（ローカル）の tzinfo を付与する。
    """
    if not value:
        return None
    text = value.strip().replace("/", "-")
    candidates = [text, text.replace(" ", "T")]
    for cand in candidates:
        try:
            dt = datetime.datetime.fromisoformat(cand)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=base.tzinfo)
            return dt
        except ValueError:
            continue
    return None


def _resolve_write_calendar(room_name: str):
    """書き込み先（ペルソナ専用カレンダー）IDを解決する。(calendar_id, error) を返す。"""
    if not room_name:
        return None, "内部エラー: ルーム情報が取得できませんでした。"
    room_cfg = gcal.get_room_calendar_override(room_name)
    write_calendar_id = (room_cfg.get("persona_write_calendar_id") or "").strip()
    if not write_calendar_id:
        return None, ("このルームには『ペルソナ専用カレンダー』が設定されていません。"
                      "ユーザーに、設定→個別→「🗓️ カレンダー連携（このルーム）」で指定してもらってください。"
                      "（安全のため、専用カレンダー以外は編集・削除できません）")
    return write_calendar_id, None


def _audit_calendar_write(room_name: str, action: str, intent: str, status: str, details: str = "") -> None:
    try:
        from capability_policy_manager import CapabilityPolicyManager
        CapabilityPolicyManager(room_name).record_audit(
            category="calendar_write", action=action, intent=intent, status=status, details=details)
    except Exception:
        pass


def _format_match_line(ev: dict) -> str:
    when = ev.get("start") or "?"
    kind = "終日" if ev.get("is_all_day") else "時刻付き"
    return f"- 「{ev.get('summary','(無題)')}」({when} / {kind}) [id: {ev.get('id')}]"


def _find_persona_events(calendar_id: str, summary: str, date: str, base):
    """専用カレンダー内で summary（と任意の date）に一致するイベントを探す。"""
    tmin = base - datetime.timedelta(days=1)
    tmax = base + datetime.timedelta(days=400)
    matches = gcal.GoogleCalendarService().find_events(calendar_id, tmin, tmax, query=summary.strip() or None)
    norm = (summary or "").strip()
    if norm:
        exact = [m for m in matches if (m.get("summary") or "") == norm]
        if exact:
            matches = exact
    if date:
        day = _resolve_date(date, base)
        if day is not None:
            def _on_day(m):
                s = (m.get("start") or "")[:10]
                return s == day.isoformat()
            day_matches = [m for m in matches if _on_day(m)]
            matches = day_matches
    return matches


@tool
def list_persona_calendar_events(room_name: str = "", days: int = 14) -> str:
    """
    ペルソナ専用カレンダー（あなたが書き込み・編集・削除できるカレンダー）に登録されている
    予定を一覧します。予定を編集・削除する前に、対象を確認するのに使ってください。

    days: 今日から何日先までを対象にするか（既定14日）。
    room_name: (システムで自動入力)
    """
    guard = _settings_guard()
    if guard:
        return guard
    calendar_id, err = _resolve_write_calendar(room_name)
    if err:
        return err

    base = _local_now()
    try:
        days = max(1, min(int(days), 90))
    except (ValueError, TypeError):
        days = 14
    tmin = base.replace(hour=0, minute=0, second=0, microsecond=0)
    tmax = tmin + datetime.timedelta(days=days)
    try:
        events = gcal.GoogleCalendarService().find_events(calendar_id, tmin, tmax)
    except Exception as e:  # noqa: BLE001
        return f"専用カレンダーの予定取得に失敗しました: {e}"
    if not events:
        return f"専用カレンダーには、今日から{days}日以内の予定はありません。"

    lines = []
    for ev in events[:30]:
        when = (ev.get("start") or "?")
        if ev.get("is_all_day"):
            lines.append(f"- 終日: {ev.get('summary','(無題)')}（{when[:10]}）")
        else:
            lines.append(f"- {ev.get('summary','(無題)')}（{when[:16].replace('T',' ')}）")
    return (f"専用カレンダーの予定（今日から{days}日以内）:\n" + "\n".join(lines)
            + "\n\n（編集は update_calendar_event、削除は delete_calendar_event に summary を渡してください）")


@tool
def delete_calendar_event(room_name: str = "", event_id: str = "",
                          summary: str = "", date: str = "") -> str:
    """
    ペルソナ専用カレンダーの予定を削除します（専用カレンダー以外は削除できません）。

    event_id: 削除する予定のID（分かっている場合）。
    summary: 予定のタイトル（IDが不明な場合。専用カレンダー内を検索して削除します）。
    date: 'YYYY-MM-DD'/'today'/'tomorrow'（任意。同名予定が複数あるときの絞り込みに使用）。
    room_name: (システムで自動入力)

    summary 指定で複数一致した場合は候補一覧（IDつき）を返すので、event_id を指定して再実行してください。
    """
    guard = _settings_guard()
    if guard:
        return guard
    calendar_id, err = _resolve_write_calendar(room_name)
    if err:
        return err

    svc = gcal.GoogleCalendarService()
    if (event_id or "").strip():
        try:
            svc.delete_event(calendar_id, event_id.strip())
        except Exception as e:  # noqa: BLE001
            _audit_calendar_write(room_name, "delete_calendar_event", event_id, "error", str(e))
            return f"削除に失敗しました（IDが専用カレンダーに存在しない可能性があります）: {e}"
        _audit_calendar_write(room_name, "delete_calendar_event", event_id, "success", f"id={event_id}")
        return "✅ 指定の予定を専用カレンダーから削除しました。"

    if not (summary or "").strip():
        return "削除する予定の summary か event_id を指定してください。"

    base = _local_now()
    try:
        matches = _find_persona_events(calendar_id, summary, date, base)
    except Exception as e:  # noqa: BLE001
        return f"予定の検索に失敗しました: {e}"
    if not matches:
        return f"専用カレンダーに「{summary}」に一致する予定が見つかりませんでした。"
    if len(matches) > 1:
        lines = "\n".join(_format_match_line(m) for m in matches[:10])
        return ("複数の予定が見つかりました。どれを削除するか event_id で指定してください:\n" + lines)

    target = matches[0]
    try:
        svc.delete_event(calendar_id, target["id"])
    except Exception as e:  # noqa: BLE001
        _audit_calendar_write(room_name, "delete_calendar_event", summary, "error", str(e))
        return f"削除に失敗しました: {e}"
    _audit_calendar_write(room_name, "delete_calendar_event", summary, "success", f"id={target['id']}")
    return f"✅ 専用カレンダーから『{target.get('summary')}』を削除しました。"


@tool
def update_calendar_event(room_name: str = "", event_id: str = "",
                          summary: str = "", date: str = "",
                          new_summary: str = "", new_start: str = "", new_end: str = "",
                          all_day: Optional[bool] = None, description: str = "") -> str:
    """
    ペルソナ専用カレンダーの予定を編集します（専用カレンダー以外は編集できません）。

    対象の指定: event_id（確実）／または summary（＋任意で date）で専用カレンダー内を検索。
    変更内容（指定したものだけ更新）:
      new_summary: 新しいタイトル。
      new_start / new_end: 新しい開始/終了。時刻付きは 'YYYY-MM-DD HH:MM'、終日は 'YYYY-MM-DD'。
      all_day: True/Falseで終日⇔時刻付きを切り替え（new_start も併せて指定してください）。
      description: 補足説明。
    room_name: (システムで自動入力)

    summary 指定で複数一致した場合は候補一覧（IDつき）を返すので、event_id で再実行してください。
    """
    guard = _settings_guard()
    if guard:
        return guard
    calendar_id, err = _resolve_write_calendar(room_name)
    if err:
        return err

    base = _local_now()
    svc = gcal.GoogleCalendarService()

    # 対象イベントの特定
    target_id = (event_id or "").strip()
    if not target_id:
        if not (summary or "").strip():
            return "編集対象の summary か event_id を指定してください。"
        try:
            matches = _find_persona_events(calendar_id, summary, date, base)
        except Exception as e:  # noqa: BLE001
            return f"予定の検索に失敗しました: {e}"
        if not matches:
            return f"専用カレンダーに「{summary}」に一致する予定が見つかりませんでした。"
        if len(matches) > 1:
            lines = "\n".join(_format_match_line(m) for m in matches[:10])
            return ("複数の予定が見つかりました。どれを編集するか event_id で指定してください:\n" + lines)
        target_id = matches[0]["id"]

    # 変更ボディの組み立て（指定された項目のみ）
    body: dict = {}
    if (new_summary or "").strip():
        body["summary"] = new_summary.strip()
    if description:
        body["description"] = description

    if (new_start or "").strip() or all_day is not None:
        is_all_day = bool(all_day) if all_day is not None else False
        if is_all_day:
            start_day = _resolve_date(new_start, base) if new_start else None
            if start_day is None:
                return "終日に変更する場合は new_start に日付（'YYYY-MM-DD'等）を指定してください。"
            end_day = _resolve_date(new_end, base) if new_end else start_day
            if end_day is None or end_day < start_day:
                end_day = start_day
            body["start"] = {"date": start_day.isoformat()}
            body["end"] = {"date": (end_day + datetime.timedelta(days=1)).isoformat()}
        else:
            start_dt = _parse_datetime(new_start, base) if new_start else None
            if start_dt is None:
                return "new_start に開始日時（'YYYY-MM-DD HH:MM'）を指定してください。"
            end_dt = _parse_datetime(new_end, base) if new_end else (start_dt + datetime.timedelta(hours=1))
            if end_dt <= start_dt:
                return "終了は開始より後にしてください。"
            body["start"] = {"dateTime": start_dt.isoformat()}
            body["end"] = {"dateTime": end_dt.isoformat()}

    if not body:
        return "変更内容（new_summary / new_start / all_day / description のいずれか）を指定してください。"

    try:
        updated = svc.patch_event(calendar_id, target_id, body)
    except Exception as e:  # noqa: BLE001
        _audit_calendar_write(room_name, "update_calendar_event", summary or target_id, "error", str(e))
        return f"編集に失敗しました（IDが専用カレンダーに存在しない可能性があります）: {e}"
    _audit_calendar_write(room_name, "update_calendar_event", summary or target_id, "success",
                          f"id={target_id} fields={list(body.keys())}")
    title = updated.get("summary", "(無題)") if isinstance(updated, dict) else "(無題)"
    link = updated.get("htmlLink", "") if isinstance(updated, dict) else ""
    return f"✅ 専用カレンダーの『{title}』を更新しました。" + (f"\n{link}" if link else "")


@tool
def add_calendar_event(summary: str, start: str, end: str = "",
                       room_name: str = "", description: str = "",
                       all_day: bool = False, allow_duplicate: bool = False) -> str:
    """
    ペルソナ専用カレンダーに予定を1件登録します。

    summary: 予定のタイトル（例: 「ルシアンとのティータイム」）。
    start: 開始。時刻付きなら 'YYYY-MM-DD HH:MM'（例: '2026-06-15 16:00'）、
           終日なら 'YYYY-MM-DD' か 'today'/'tomorrow'。
    end: 終了（省略可）。時刻付きは省略時に開始+1時間。終日は省略時に当日1日。
    room_name: (システムで自動入力)
    description: 予定の補足説明（任意）。
    all_day: Trueにすると終日予定として登録します（時刻なし。誕生日・記念日・終日タスク等）。
    allow_duplicate: 同一開始日時・高類似タイトルの予定が既にあっても、意図して重複登録する場合だけ True。

    【書き込み先について】
    - 書き込み先はユーザーがルーム個別設定で指定した「ペルソナ専用カレンダー」に限定されます。
      未設定のルームでは登録できません（メインカレンダー等には絶対に書き込みません）。
    - 専用カレンダーへの登録は承認確認なしで直接実行して構いません（あなた自身の場所です）。
    """
    guard = _settings_guard()
    if guard:
        return guard

    if not room_name:
        return "内部エラー: ルーム情報が取得できませんでした。"

    # --- 書き込み先カレンダーの解決（ルーム個別ポリシー。LLMからの宛先指定は受け付けない） ---
    room_cfg = gcal.get_room_calendar_override(room_name)
    write_calendar_id = (room_cfg.get("persona_write_calendar_id") or "").strip()
    if not write_calendar_id:
        return ("このルームには書き込み先の『ペルソナ専用カレンダー』が設定されていません。"
                "ユーザーに、設定→個別→「🗓️ カレンダー連携（このルーム）」で専用カレンダーを"
                "指定してもらってください。（安全のため、メインカレンダー等への書き込みはできません）")

    base = _local_now()

    if all_day:
        start_day = _resolve_date(start, base)
        if start_day is None:
            return f"開始日『{start}』を解釈できませんでした。'YYYY-MM-DD'・'today'・'tomorrow' を使ってください。"
        end_day = _resolve_date(end, base) if end else start_day
        if end_day is None:
            end_day = start_day
        if end_day < start_day:
            return "終了日は開始日以降にしてください。"
        # Google の終日 end.date は排他的なので、最終日の翌日を渡す
        start_dt = datetime.datetime.combine(start_day, datetime.time.min, tzinfo=base.tzinfo)
        end_dt = datetime.datetime.combine(end_day + datetime.timedelta(days=1), datetime.time.min, tzinfo=base.tzinfo)
    else:
        start_dt = _parse_datetime(start, base)
        if start_dt is None:
            return f"開始日時『{start}』を解釈できませんでした。'YYYY-MM-DD HH:MM' 形式で指定してください。"
        end_dt = _parse_datetime(end, base) if end else None
        if end_dt is None:
            end_dt = start_dt + datetime.timedelta(hours=1)
        if end_dt <= start_dt:
            return "終了日時は開始日時より後にしてください。"

    # 監査ログ用のマネージャ
    try:
        from capability_policy_manager import CapabilityPolicyManager
        cap_mgr = CapabilityPolicyManager(room_name)
    except Exception:
        cap_mgr = None

    svc = gcal.GoogleCalendarService()

    if not allow_duplicate:
        try:
            duplicate = _find_duplicate_calendar_event(
                svc, write_calendar_id, summary.strip(), start_dt, all_day=all_day
            )
        except Exception as e:  # noqa: BLE001
            if cap_mgr:
                try:
                    cap_mgr.record_audit(category="calendar_write", action="add_calendar_event",
                                         intent=summary, status="error", details=f"duplicate check failed: {e}")
                except Exception:
                    pass
            return f"カレンダーの重複確認に失敗しました: {e}"
        if duplicate:
            title = duplicate.get("summary") or "(無題)"
            if cap_mgr:
                try:
                    cap_mgr.record_audit(category="calendar_write", action="add_calendar_event",
                                         intent=summary, status="skipped_duplicate",
                                         details=f"matched={title} start={start_dt.isoformat()} / {write_calendar_id}")
                except Exception:
                    pass
            return f"既に同じ予定があります: {title}"

    try:
        created = svc.insert_event(
            write_calendar_id, summary.strip(), start_dt, end_dt, description.strip(), all_day=all_day,
        )
    except Exception as e:  # noqa: BLE001
        if cap_mgr:
            try:
                cap_mgr.record_audit(category="calendar_write", action="add_calendar_event",
                                     intent=summary, status="error", details=str(e))
            except Exception:
                pass
        return f"カレンダーへの登録に失敗しました: {e}"

    if cap_mgr:
        try:
            cap_mgr.record_audit(category="calendar_write", action="add_calendar_event",
                                 intent=summary, status="success",
                                 details=f"{start_dt.isoformat()} 〜 {end_dt.isoformat()} (all_day={all_day}) / {write_calendar_id}")
        except Exception:
            pass

    if all_day:
        when = f"{start_day.isoformat()} 終日" if end_day == start_day else f"{start_day.isoformat()}〜{end_day.isoformat()} 終日"
    else:
        when = f"{start_dt.strftime('%Y-%m-%d %H:%M')}〜{end_dt.strftime('%H:%M')}"
    link = created.get("htmlLink", "") if isinstance(created, dict) else ""
    msg = f"✅ ペルソナ専用カレンダーに『{summary}』（{when}）を登録しました。"
    return msg + (f"\n{link}" if link else "")
