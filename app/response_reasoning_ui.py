"""共通・個別の応答推論設定パネルを構築する。"""

from __future__ import annotations

import gradio as gr


def build_response_reasoning_panel(scope: str = "global", room_name: str | None = None) -> dict:
    """保存済み値から表示を作り、配線用コンポーネントを返す。"""
    from ui_handlers import response_reasoning as handlers

    if scope not in {"global", "room"}:
        raise ValueError("scope must be global or room")
    if scope == "room" and not room_name:
        raise ValueError("room panel requires room_name")
    initial = handlers.initial_panel_values(room_name if scope == "room" else None)
    effort_values = {key: value for key, value in initial["effort"].items() if key != "__type__"}
    effort_values.setdefault("interactive", True)
    budget_values = {key: value for key, value in initial["budget"].items() if key != "__type__"}
    prefix = "room_response_reasoning" if scope == "room" else "global_response_reasoning"
    with gr.Accordion("🧠 応答モデルの推論設定", open=False, elem_id=prefix):
        summary = gr.Markdown(initial["summary"], elem_id=f"{prefix}_summary")
        effort = gr.Dropdown(
            label="推論の強さ", allow_custom_value=False,
            elem_id=f"{prefix}_effort", **effort_values,
        )
        budget = gr.Number(
            label="詳細: 思考トークン予算", precision=0, minimum=1024,
            elem_id=f"{prefix}_budget", **budget_values,
        )
        state = gr.State(initial["state"])
        with gr.Row():
            save = gr.Button("推論設定を保存", variant="primary", size="sm", elem_id=f"{prefix}_save")
            refresh = gr.Button("推論設定を再読込", variant="secondary", size="sm", elem_id=f"{prefix}_refresh")
        status = gr.Markdown(initial["status"], elem_id=f"{prefix}_status")
    return {
        "effort": effort, "budget": budget, "summary": summary, "state": state,
        "status": status, "save": save, "refresh": refresh,
        "outputs": [effort, budget, summary, state, status],
    }
