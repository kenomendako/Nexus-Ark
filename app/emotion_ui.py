"""自己意識画面の感情一覧と、ユーザーが管理する項目のフォーム。"""
import gradio as gr


EMOTION_STATE_HEADERS = [
    "項目", "使用", "本人の状態", "最新申告値", "申告時刻", "現在の参考値",
    "提示対象", "申告に適用中の時間条件", "設定中の時間条件",
]


def build_emotion_panel():
    """部品だけを構築する。読込・保存はセッション開始後の明示イベントで行う。"""
    gr.Markdown("#### 💭 持続中の感情")
    gr.Markdown(
        "本人の最新の申告と、時間経過による参考値を確認できます。"
        "提示条件内の項目から、毎ターンは参考値が強い順に最大二件を伝えます。"
        "参考値が弱まっても、本人が落ち着いたと判断したことにはなりません。"
    )
    refresh = gr.Button("🔄 感情の状態・項目を読み込む", variant="secondary")
    states = gr.HTML(value="", elem_id="emotion_states_table")
    gr.Markdown("一覧を横にスクロールすると、申告時刻・参考値・時間条件も確認できます。")
    with gr.Accordion("⚙️ 感情項目の設定", open=False):
        gr.Markdown(
            "項目はユーザーが追加でき、ペルソナは会話で追加を提案できます。"
            "項目を作るだけでは感情は生まれません。強さや終了は本人が決めます。\n\n"
            "表記の変更は今の項目を編集し、意味の異なる気持ちは新しい項目として追加してください。"
            "使用を止めても申告は残り、本人の終了判断とは別に扱います。"
        )
        selector = gr.Dropdown(label="設定する感情項目", choices=[], value=None,
                               interactive=True, elem_id="emotion_definition_selector")
        name = gr.Textbox(label="項目の名前", max_lines=1,
                          info="必須・64文字以内", elem_id="emotion_definition_name")
        description = gr.Textbox(label="意味の説明（任意）", lines=2, max_lines=4,
                                 info="200文字以内", elem_id="emotion_definition_description")
        enabled = gr.Checkbox(label="この項目を使用する", value=True,
                              elem_id="emotion_definition_enabled")
        gr.Markdown(
            "時間設定は本人が次にこの気持ちを見直した時から適用します。"
            "現在の申告値・時刻・適用中の条件は、設定保存では変わりません。"
        )
        with gr.Row():
            half_life = gr.Number(label="参考値が半分になる時間（時間）", value=None,
                                  minimum=0.25, maximum=168,
                                  info="標準6時間・15分〜7日", elem_id="emotion_half_life")
            max_age = gr.Number(label="最長提示期間（時間）", value=None,
                                minimum=0.25, maximum=168,
                                info="標準24時間・15分〜7日", elem_id="emotion_max_age")
        detail = gr.Textbox(label="設定・申告の確認", lines=3, interactive=False,
                            elem_id="emotion_definition_detail")
        save = gr.Button("感情項目の設定を保存", variant="primary", interactive=False,
                         elem_id="emotion_definition_save")
    status = gr.Textbox(label="感情項目の読込・保存結果", interactive=False,
                        value="ルームの状態を読み込みます。", elem_id="emotion_definition_status")
    session = gr.State({})
    return {
        "refresh": refresh, "states": states, "selector": selector, "name": name,
        "description": description, "enabled": enabled, "half_life": half_life,
        "max_age": max_age, "detail": detail, "save": save, "status": status, "session": session,
    }
