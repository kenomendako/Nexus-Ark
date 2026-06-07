import json
import os
from datetime import datetime
from typing import Any, Dict, List
from urllib.parse import quote_plus

from langchain_core.tools import tool

import constants


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_tracks(tracks: Any) -> List[Dict[str, str]]:
    if isinstance(tracks, str):
        try:
            tracks = json.loads(tracks)
        except Exception:
            tracks = [{"title": tracks}]

    if isinstance(tracks, dict):
        tracks = [tracks]

    normalized: List[Dict[str, str]] = []
    for item in tracks or []:
        if not isinstance(item, dict):
            item = {"title": item}

        title = _safe_text(item.get("title") or item.get("song") or item.get("name"))
        if not title:
            continue

        normalized.append({
            "title": title,
            "artist": _safe_text(item.get("artist") or item.get("artists")),
            "reason": _safe_text(item.get("reason") or item.get("persona_reason")),
        })

    return normalized[:3]


def _search_links(title: str, artist: str) -> Dict[str, str]:
    query = " ".join(part for part in [title, artist] if part).strip()
    encoded = quote_plus(query)
    return {
        "YouTube": f"https://www.youtube.com/results?search_query={encoded}",
        "Spotify": f"https://open.spotify.com/search/{encoded}",
        "Bandcamp": f"https://bandcamp.com/search?q={encoded}",
        "SoundCloud": f"https://soundcloud.com/search?q={encoded}",
    }


def _append_music_audit(room_name: str, payload: Dict[str, Any]) -> None:
    try:
        audit_dir = os.path.join(constants.METADATA_DIR, "music", "audit")
        os.makedirs(audit_dir, exist_ok=True)
        date_key = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(audit_dir, f"{date_key}.jsonl")
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "room_name": room_name,
            **payload,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # 推薦カード自体を失敗させないため、監査ログ失敗は握りつぶす。
        return


@tool
def recommend_music(mood: str, reason: str, tracks: List[Dict[str, str]], room_name: str) -> str:
    """
    音楽推薦カードを作成します。音楽の再生、PCスピーカー操作、Spotify制御、Discord VC再生は行いません。

    ユーザーに曲を勧めたい時、まずこのツールで1〜3曲の推薦カードを作ってください。
    Spotify Premiumやローカル音源がなくても使えるよう、YouTube/Spotify/Bandcamp/SoundCloudの検索リンクを付けます。

    Args:
        mood: 今の気分や場面。例: 静かな夜、集中したい、少し元気がほしい。
        reason: なぜこの音楽を勧めたいか。
        tracks: 最大3件の曲候補。各要素は title, artist, reason を含めます。
        room_name: (システムで自動入力)
    """
    normalized_tracks = _normalize_tracks(tracks)
    if not normalized_tracks:
        return "音楽推薦カードを作れませんでした。`tracks` に少なくとも1曲の `title` を指定してください。"

    mood_text = _safe_text(mood) or "今の気分"
    reason_text = _safe_text(reason)

    lines = [
        "## 音楽推薦カード",
        "",
        f"**気分/場面:** {mood_text}",
    ]
    if reason_text:
        lines.append(f"**推薦したい理由:** {reason_text}")
    lines.extend([
        "",
        "※このカードはリンク推薦のみです。PCスピーカー再生や音声ストリーミングは行っていません。",
        "",
    ])

    for index, track in enumerate(normalized_tracks, start=1):
        title = track["title"]
        artist = track.get("artist", "")
        title_line = f"{index}. **{title}**"
        if artist:
            title_line += f" - {artist}"
        lines.append(title_line)

        track_reason = track.get("reason", "")
        if track_reason:
            lines.append(f"   - 理由: {track_reason}")

        links = _search_links(title, artist)
        link_text = " / ".join(f"[{name}]({url})" for name, url in links.items())
        lines.append(f"   - 聴く/探す: {link_text}")

    _append_music_audit(room_name, {
        "action": "recommend_music",
        "mood": mood_text,
        "reason": reason_text,
        "tracks": normalized_tracks,
        "output": "recommendation_card",
    })

    return "\n".join(lines)
