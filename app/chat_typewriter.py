"""完成済みの会話から、発言本文だけを時間に沿って表示する。"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from time import monotonic


FRAME_INTERVAL = 0.04
_TIMESTAMP = re.compile(
    r"(?:\n\n)?\d{4}-\d{2}-\d{2}\s*\([A-Za-z月火水木金土日]{1,3}\)"
    r"\s*\d{2}:\d{2}:\d{2}(?:\s*\|[^\n]*)?\s*$"
)
_DISPLAY_UNIT = re.compile(
    r"```[\s\S]*?```|`[^`\n]+`|<[^>]+>|"
    r"&(?:#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);|"
    r"!?\[[^\]\n]*\]\([^\n)]*\)|[\s\S]"
)


@dataclass
class TypewriterTarget:
    """整形処理だけが作る表示境界。本文のない最新発言も保持する。"""

    log_index: int
    responder: str
    message_index: int | None = None
    prefix: str = ""
    body: str = ""


def split_timestamp(text: str) -> tuple[str, str]:
    """ログ末尾の付与情報を、マスクやMarkdown整形の前に分ける。"""
    match = _TIMESTAMP.search(text)
    if match is None:
        return text, ""
    return text[:match.start()].rstrip(), match.group().strip()


def validate_delay(value) -> float:
    delay = float(value)
    if not math.isfinite(delay) or not 0 <= delay <= 0.1:
        raise ValueError("表示の待ち時間は0～0.1秒の有限の値で指定してください。")
    return delay


def iter_typewriter_history(history, target, delay, stop_event):
    """約25回/秒を上限に本文を更新し、最後は元の履歴をそのまま返す。

    各文字のsleepを加算せず、経過時間から表示位置を決める。描画や通信に
    時間がかかった場合も追いつく。停止・close時は完了フレームを送らない。
    HTMLタグ・エンティティ・コード・リンクは途中で分割しない。
    """
    if stop_event.is_set():
        return
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        delay = 0
    if (not math.isfinite(delay) or delay <= 0 or target is None
            or target.message_index is None or not target.body):
        yield history
        return

    index = target.message_index
    message = history[index]
    units = _DISPLAY_UNIT.findall(target.body)
    # HTML構造と完成したコード/リンクは一括。エンティティは表示上1文字。
    costs = [int(len(unit) == 1 or unit.startswith("&")) for unit in units]
    total = sum(costs)
    if not total:
        yield history
        return

    def frame(content):
        result = list(history)
        result[index] = {**message, "content": target.prefix + content + "▌"}
        return result

    yield frame("")
    started = monotonic()
    position = 0
    shown = 0
    rendered = []
    while position < len(units):
        # 長い保存済み待機値でも40ms以内に停止通知を受け取れる。
        remaining = max(0, total * delay - (monotonic() - started))
        if stop_event.wait(min(FRAME_INTERVAL, remaining) if remaining else 0):
            return
        due = min(total, int((monotonic() - started) / delay + 1e-7))
        if due <= shown:
            continue
        while position < len(units) and (costs[position] == 0 or shown < due):
            rendered.append(units[position])
            shown += costs[position]
            position += 1
        if stop_event.is_set():
            return
        if position == len(units):
            # 日時・モデル名は本文完了と同じフレームで一括表示する。
            yield history
        else:
            yield frame("".join(rendered))
