# tools/research_subscription_tools.py
"""
ペルソナが「継続リサーチ・テーマ（購読）」を自分で管理するためのツール。

自分の興味に基づいてテーマを登録すると、定期的に自動でディープリサーチが走り、
結果がそのテーマの継続研究スレッドへ自分の言葉で追記されていく（Phase 2/3 と連動）。
"""

from typing import Optional

from langchain_core.tools import tool

import constants
from research_subscription_manager import ResearchSubscriptionManager
from research_thread_manager import ResearchThreadManager


def _freq_label(value: str) -> str:
    return constants.RESEARCH_SUBSCRIPTION_FREQUENCY_OPTIONS.get(value, value)


def _depth_label(value: str) -> str:
    return constants.RESEARCH_SUBSCRIPTION_DEPTH_OPTIONS.get(value, value)


def _format_subscription(sub: dict) -> str:
    state = "有効" if sub.get("enabled", True) else "停止中"
    last = sub.get("last_run") or "未実行"
    focus = sub.get("focus") or "（特になし）"
    return (
        f"- [{sub.get('id')}] {sub.get('topic')} "
        f"（{_freq_label(sub.get('frequency'))}・{_depth_label(sub.get('depth'))}・{state}）\n"
        f"    特に知りたい点: {focus}\n"
        f"    最終実行: {last}"
    )


@tool
def manage_research_subscriptions(
    room_name: str,
    action: str = "list",
    topic: str = "",
    focus: str = "",
    frequency: str = "",
    depth: str = "",
    subscription_id: str = "",
    enabled: Optional[bool] = None,
) -> str:
    """
    継続リサーチ・テーマ（購読）を自分で管理する。
    あなたが関心を持って継続的に追いかけたいテーマを登録すると、定期的に自動でディープリサーチが走り、
    その結果をあなた自身がテーマの研究スレッドへ追記して育てていける。

    action:
      - "list"   : 登録済みテーマの一覧
      - "add"    : テーマを追加（topic 必須）
      - "update" : テーマの設定変更（subscription_id 必須。focus/frequency/depth/enabled を変更）
      - "remove" : テーマの購読を解除（subscription_id 必須。蓄積した研究スレッドは残る）
      - "suggest": 既存の継続研究スレッドのうち、まだテーマ購読になっていない候補を提案
      - "run_now": 指定テーマの調査を今すぐ1回実行（subscription_id 必須。完了するとあなたが起こされ、結果を追記できる）
    topic: テーマ名（add に必須。例「長期記憶のための知識」）
    focus: 特に知りたい点（任意。例「特にRAG・記憶圧縮」）
    frequency: "daily"（毎日）/ "weekly"（週1）/ "manual"（手動のみ）。省略時は既定=毎日。
    depth: "quick"（手早く）/ "standard"（標準）/ "deep"（網羅的）。省略時は既定=標準。
    subscription_id: update / remove の対象ID（list で確認できる。先頭一致可）。
    enabled: update で有効(True)/停止(False)を切り替える。
    """
    try:
        manager = ResearchSubscriptionManager(room_name)
        action = (action or "list").strip().lower()

        if action == "list":
            subs = manager.list_subscriptions()
            if not subs:
                return "【継続リサーチ・テーマはまだありません】`action=\"add\"` で登録できます。"
            lines = ["【継続リサーチ・テーマ一覧】"]
            lines.extend(_format_subscription(s) for s in subs)
            return "\n".join(lines)

        if action == "add":
            if not (topic or "").strip():
                return "【エラー】テーマ名（topic）が必要です。"
            freq = frequency.strip().lower() if frequency else constants.RESEARCH_SUBSCRIPTION_DEFAULT_FREQUENCY
            dep = depth.strip().lower() if depth else constants.RESEARCH_SUBSCRIPTION_DEFAULT_DEPTH
            existed = manager.get_by_topic(topic) is not None
            sub = manager.add_subscription(
                topic=topic, focus=focus, frequency=freq, depth=dep, created_by="persona"
            )
            if sub.get("_limit_exceeded"):
                return (
                    f"【上限です】継続リサーチ・テーマは最大{sub.get('limit', 10)}件です。"
                    "不要な購読を remove してから追加してください。"
                )
            if sub.get("_dedup_skipped"):
                return f"【似たテーマが既に登録済みです。新規追加はしませんでした】\n{_format_subscription(sub)}"
            if existed:
                return f"【既に登録済みのテーマです】\n{_format_subscription(sub)}"
            return (
                "【テーマを追加しました】定期的に自動でリサーチし、研究スレッドへ追記していきます。\n"
                f"{_format_subscription(sub)}"
            )

        if action == "update":
            if not (subscription_id or "").strip():
                return "【エラー】変更対象の subscription_id が必要です（list で確認）。"
            kwargs = {}
            if focus:
                kwargs["focus"] = focus
            if frequency:
                freq = frequency.strip().lower()
                if freq not in constants.RESEARCH_SUBSCRIPTION_FREQUENCY_OPTIONS:
                    return f"【エラー】frequency は {list(constants.RESEARCH_SUBSCRIPTION_FREQUENCY_OPTIONS)} のいずれかです。"
                kwargs["frequency"] = freq
            if depth:
                dep = depth.strip().lower()
                if dep not in constants.RESEARCH_SUBSCRIPTION_DEPTH_OPTIONS:
                    return f"【エラー】depth は {list(constants.RESEARCH_SUBSCRIPTION_DEPTH_OPTIONS)} のいずれかです。"
                kwargs["depth"] = dep
            if enabled is not None:
                kwargs["enabled"] = bool(enabled)
            if not kwargs:
                return "【エラー】変更内容（focus / frequency / depth / enabled）を1つ以上指定してください。"
            sub = manager.update_subscription(subscription_id, **kwargs)
            if not sub:
                return f"【エラー】テーマが見つかりません: {subscription_id}"
            return f"【テーマを更新しました】\n{_format_subscription(sub)}"

        if action == "remove":
            if not (subscription_id or "").strip():
                return "【エラー】解除対象の subscription_id が必要です（list で確認）。"
            ok = manager.remove_subscription(subscription_id)
            if not ok:
                return f"【エラー】テーマが見つかりません: {subscription_id}"
            return "【テーマの購読を解除しました】これまでの研究スレッド（蓄積）は残してあります。"

        if action == "suggest":
            subscribed = {s.get("thread_id") for s in manager.list_subscriptions() if s.get("thread_id")}
            try:
                threads = ResearchThreadManager(room_name).list_threads(status="active")
            except Exception:
                threads = []
            candidates = [t for t in threads if t.get("thread_id") not in subscribed]
            if not candidates:
                return (
                    "【提案できる候補はありません】\n"
                    "新しい関心があれば `action=\"add\"` で直接テーマを登録できます。"
                    "自分の目的に立ち返りたいときは `read_purpose_profile` も参考になります。"
                )
            lines = ["【テーマ購読の候補（既存の研究スレッドから）】"]
            for t in candidates[:10]:
                lines.append(f"- {t.get('title')}（thread_id={t.get('thread_id')}）")
            lines.append(
                "\n継続的に追いかけたいものがあれば、その title を topic にして "
                "`action=\"add\"` で購読登録してください。"
            )
            return "\n".join(lines)

        if action == "run_now":
            if not (subscription_id or "").strip():
                return "【エラー】実行対象の subscription_id が必要です（list で確認）。"
            import alarm_manager
            result = alarm_manager.run_research_subscription_now(room_name, subscription_id)
            if result.get("ok"):
                return (
                    f"【調査を開始しました】{result.get('message')}\n"
                    "完了すると起こされるので、そのとき結果を研究スレッドへ追記してください。"
                )
            return f"【実行できませんでした】{result.get('message')}"

        return f"【エラー】未知の action: {action}（list/add/update/remove/suggest/run_now）"
    except Exception as e:
        return f"【エラー】継続リサーチ・テーマの操作に失敗しました: {e}"
