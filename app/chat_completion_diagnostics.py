"""応答完了後の遅延を、内容や個人識別子を出さずに任意計測する。"""
from __future__ import annotations

import os
from time import perf_counter
from uuid import uuid4


ENV_NAME = "NEXUS_ARK_CHAT_COMPLETION_DIAGNOSTICS"
STAGES = frozenset({
    "initial_avatar", "response_saved", "display_history", "final_history",
    "final_scenery", "final_avatar", "final_style", "rag_release", "memory_trim",
    "success_callback", "before_final_yield", "generator_resumed",
    "scenery_deferred", "deferred_scenery",
})


class ChatCompletionProbe:
    """既定OFF。出す値はstage、ランダムID、時間、件数・byte数だけ。"""

    def __init__(self):
        self.enabled = os.environ.get(ENV_NAME, "").strip().lower() in {"1", "true", "on"}
        self.trace = uuid4().hex[:12] if self.enabled else ""
        self.started = perf_counter() if self.enabled else 0

    def mark(self, stage: str, **numbers) -> None:
        if not self.enabled or stage not in STAGES:
            return
        try:
            values = " ".join(
                f"{key}={value:.3f}" if isinstance(value, float) else f"{key}={value}"
                for key, value in numbers.items()
                if key in {"duration_ms", "history_items", "history_text_bytes", "avatar_bytes", "style_bytes"}
                and isinstance(value, (int, float)) and not isinstance(value, bool)
            )
            print(f"[ChatCompletion] trace={self.trace} stage={stage} "
                  f"elapsed_ms={(perf_counter()-self.started)*1000:.3f} {values}", flush=True)
        except Exception:
            # 診断出力の失敗で会話・保存・成功callbackを失敗させない。
            pass

    def run(self, stage: str, function, *args, **kwargs):
        if not self.enabled:
            return function(*args, **kwargs)
        started = perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            self.mark(stage, duration_ms=(perf_counter()-started)*1000)

    def before_yield(self, history, avatar_update, style_update) -> None:
        if not self.enabled:
            return
        try:
            def size(value):
                return len(value.encode("utf-8")) if isinstance(value, str) else 0

            # 履歴の文字列本文だけ。ファイル/動画等を含む正確な転送量ではない。
            self.mark("before_final_yield", history_items=len(history),
                      history_text_bytes=sum(size(item.get("content")) for item in history if isinstance(item, dict)),
                      avatar_bytes=size(avatar_update.get("value")),
                      style_bytes=size(style_update.get("value")))
        except Exception:
            pass
