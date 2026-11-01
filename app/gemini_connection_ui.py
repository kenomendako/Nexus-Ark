"""Gemini Developer API / Vertex AI接続先の独立Gradioパネル。"""

from __future__ import annotations

from typing import Any

import gradio as gr


def _update_value(component_value: Any, default: Any = None) -> Any:
    """handlerのgr.update相当値から初期表示値を取り出す。"""

    if isinstance(component_value, dict) and component_value.get("__type__") == "update":
        return component_value.get("value", default)
    return component_value if component_value is not None else default


def _update_choices(component_value: Any) -> list[tuple[str, str]]:
    """handlerの更新値からDropdownのchoicesを取り出す。"""

    if isinstance(component_value, dict) and component_value.get("__type__") == "update":
        choices = component_value.get("choices")
    else:
        choices = None
    if not isinstance(choices, (list, tuple)):
        return [("共通Vertexキー", "shared")]
    result: list[tuple[str, str]] = []
    for choice in choices:
        if isinstance(choice, (list, tuple)) and len(choice) >= 2:
            result.append((str(choice[0]), str(choice[1])))
        elif choice is not None:
            result.append((str(choice), str(choice)))
    return result or [("共通Vertexキー", "shared")]


def build_gemini_connection_panel(scope: str = "global", room_name: str | None = None) -> dict[str, Any]:
    """共通／ルームのGemini接続先パネルを構築する。

    ``outputs`` は常に
    ``[inherit, backend, auth_mode, project, location, key_ref, summary, state, status]``
    の9個で固定する。初期化時は保存値を読むだけで、保存イベントは発火しない。
    """

    from ui_handlers import gemini_connection as handlers

    if scope not in {"global", "room"}:
        raise ValueError("scope must be global or room")
    if scope == "room" and not room_name:
        raise ValueError("room panel requires room_name")

    initial = handlers.initial_panel_values(room_name if scope == "room" else None)
    prefix = "room_gemini_connection" if scope == "room" else "global_gemini_connection"
    initial_inherit = bool(_update_value(initial["inherit"], scope == "room"))
    initial_backend = _update_value(initial["backend"], "developer")
    initial_auth = _update_value(initial["auth_mode"], "adc")
    initial_project = _update_value(initial["project"], "")
    initial_location = _update_value(initial["location"], "global")
    initial_key_ref = _update_value(initial["key_ref"], "shared")
    initial_key_choices = _update_choices(initial["key_ref"])
    initial_summary = initial["summary"]
    initial_state = initial["state"]
    initial_status = initial["status"]
    is_vertex = str(initial_backend).strip().lower() == "vertex"
    is_api_key = is_vertex and str(initial_auth).strip().lower() == "api_key"
    show_key_ref = is_api_key and len(initial_key_choices) > 1

    with gr.Accordion("☁️ Vertex AI接続設定", open=False, elem_id=prefix,
                      elem_classes=[] if is_vertex else ["gemini-connection-hidden"]) as container:
        # Gradio 6ではvisible=Falseの子を後からvisible=Trueへ戻すとDOMが生成されない
        # 版があるため、部品は常にDOMへ置き、表示切替はCSS classの更新で行う。
        gr.HTML(
            "<style>.gemini-connection-hidden { display: none !important; }</style>",
            elem_id=f"{prefix}_style",
        )
        gr.Markdown(
            ("このルームの応答に使います。内部処理は共通設定を使います。" if scope == "room" else
             "共通の応答と内部処理のテキストLLMに使います（画像・音声・Embeddingは対象外）。"),
            elem_id=f"{prefix}_description",
        )
        inherit = gr.Checkbox(
            label="共通設定を使う",
            value=initial_inherit if scope == "room" else False,
            visible=False,
            interactive=False,
            elem_id=f"{prefix}_inherit",
        )
        backend = gr.Radio(
            choices=[("Developer API", "developer"), ("Vertex AI", "vertex")],
            value=initial_backend,
            label="接続先",
            visible=False,
            interactive=False,
            elem_id=f"{prefix}_backend",
        )
        auth_mode = gr.Radio(
            choices=[("ADC（Application Default Credentials）", "adc"), ("APIキー", "api_key")],
            value=initial_auth,
            label="Vertex認証方式",
            visible=True,
            interactive=is_vertex,
            elem_classes=[] if is_vertex else ["gemini-connection-hidden"],
            elem_id=f"{prefix}_auth_mode",
        )
        project = gr.Textbox(
            value=initial_project,
            label="Google Cloud Project",
            placeholder="my-project",
            info="APIキー認証では、空欄はExpress、入力するとこのプロジェクトへ接続します。",
            visible=True,
            interactive=is_vertex,
            elem_classes=[] if is_vertex else ["gemini-connection-hidden"],
            elem_id=f"{prefix}_project",
        )
        location = gr.Textbox(
            value=initial_location,
            label="Vertex Location",
            placeholder="global または us-central1",
            info="通常はglobal。Projectを指定する接続で使用します。",
            visible=True,
            interactive=is_vertex,
            elem_classes=[] if is_vertex else ["gemini-connection-hidden"],
            elem_id=f"{prefix}_location",
        )
        key_ref = gr.Dropdown(
            choices=initial_key_choices,
            value=initial_key_ref,
            label="使用するVertexキー",
            info="APIキー管理で登録したキーを選択します。",
            visible=True,
            interactive=show_key_ref,
            elem_classes=[] if show_key_ref else ["gemini-connection-hidden"],
            elem_id=f"{prefix}_key_ref",
        )
        summary = gr.Markdown(initial_summary, elem_id=f"{prefix}_summary")
        state = gr.State(initial_state)
        with gr.Row():
            save = gr.Button("Vertex設定を保存", variant="primary", size="sm", elem_id=f"{prefix}_save")
            refresh = gr.Button("Vertex設定を再読込", variant="secondary", size="sm", elem_id=f"{prefix}_refresh")
        status = gr.Markdown(initial_status, elem_id=f"{prefix}_status")

    outputs = [inherit, backend, auth_mode, project, location, key_ref, summary, state, status]
    return {
        "container": container,
        "inherit": inherit,
        "backend": backend,
        "auth_mode": auth_mode,
        "project": project,
        "location": location,
        "key_ref": key_ref,
        "summary": summary,
        "state": state,
        "status": status,
        "save": save,
        "refresh": refresh,
        "outputs": outputs,
    }


def build_vertex_key_management_panel() -> dict[str, Any]:
    """共通APIキー管理内にVertex APIキーの登録入口を作る。"""

    from ui_handlers import gemini_connection as handlers

    initial = handlers.load_vertex_key()
    prefix = "global_vertex_key_management"
    initial_key_choices = _update_choices(initial[0])
    initial_key_ref = _update_value(initial[0], "shared")
    show_key_ref = len(initial_key_choices) > 1
    with gr.Accordion("Vertex AI（現在はGemini対応）", open=False, elem_id=prefix):
        gr.Markdown("プロバイダ設定で「Vertex AI（Agent Platform）」を選択します。")
        key_ref = gr.Dropdown(
            choices=initial_key_choices,
            value=initial_key_ref,
            label="登録先Vertexキー",
            info="複数登録時だけ登録先を選択できます。",
            visible=True,
            interactive=show_key_ref,
            elem_classes=[] if show_key_ref else ["gemini-connection-hidden"],
            elem_id=f"{prefix}_key_ref",
        )
        api_key = gr.Textbox(
            value="", label="Vertex APIキー", type="password", placeholder="AIza...",
            info="変更する場合だけ入力。空欄なら保存済みキーを保持します。",
            elem_id=f"{prefix}_api_key",
        )
        summary = gr.Markdown(initial[2], elem_id=f"{prefix}_summary")
        state = gr.State(initial[3])
        with gr.Row():
            save = gr.Button("APIキーを保存", variant="primary", size="sm", elem_id=f"{prefix}_save")
            refresh = gr.Button("最新の状態を再読込", size="sm", elem_id=f"{prefix}_refresh")
        status = gr.Markdown(initial[4], elem_id=f"{prefix}_status")
    return dict(key_ref=key_ref, api_key=api_key, summary=summary, state=state, save=save, refresh=refresh,
                status=status, outputs=[key_ref, api_key, summary, state, status])


def wire_gemini_connection_events(
    demo: Any,
    global_panel: dict[str, Any],
    room_panel: dict[str, Any],
    room_component: Any,
    global_provider_component: Any,
    room_provider_component: Any,
    vertex_key_panel: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Gemini接続パネルの読込・保存・切替を配線する。

    すべてのイベントは ``api_visibility="private"`` で登録する。
    """

    from ui_handlers import gemini_connection as handlers
    from ui_handlers import provider_connections as provider_handlers

    event_options = {"show_progress": "hidden", "api_visibility": "private"}

    def _change(component: Any, *, fn: Any, inputs: list[Any], outputs: list[Any]) -> None:
        if component is None or not hasattr(component, "change"):
            return
        component.change(fn=fn, inputs=inputs, outputs=outputs, **event_options)

    global_provider_inputs = [global_provider_component] if global_provider_component is not None else []
    room_provider_inputs = [room_provider_component] if room_provider_component is not None else []

    def _form_change(component: Any, **kwargs: Any) -> None:
        component.input(**kwargs, **event_options)

    # ユーザーによる方式変更だけを扱い、保存・同期の値更新では発火しない。
    for component in (
        global_panel["auth_mode"], global_panel["key_ref"],
    ):
        _form_change(
            component,
            fn=handlers.update_form,
            inputs=[
                global_panel["inherit"],
                global_panel["backend"],
                global_panel["auth_mode"],
                global_panel["project"],
                global_panel["location"],
                global_panel["key_ref"],
                global_panel["state"],
            ],
            outputs=global_panel["outputs"],
        )
    for component in (
        room_panel["auth_mode"], room_panel["key_ref"],
    ):
        _form_change(
            component,
            fn=handlers.update_form,
            inputs=[
                room_panel["inherit"],
                room_panel["backend"],
                room_panel["auth_mode"],
                room_panel["project"],
                room_panel["location"],
                room_panel["key_ref"],
                room_panel["state"],
            ],
            outputs=room_panel["outputs"],
        )

    # 保存済み値を読むイベント。provider変更とroom変更は未保存draftを捨て、
    # 旧roomのstateを次の保存へ持ち越さない。
    global_load_inputs = global_provider_inputs
    room_load_inputs = [room_component] + room_provider_inputs
    global_refresh = global_panel["refresh"].click(
        fn=handlers.load_global,
        inputs=global_load_inputs,
        outputs=global_panel["outputs"],
        **event_options,
    )
    room_refresh = room_panel["refresh"].click(
        fn=handlers.load_room,
        inputs=room_load_inputs,
        outputs=room_panel["outputs"],
        **event_options,
    )
    if room_component is not None:
        _change(
            room_component,
            fn=handlers.load_room,
            inputs=[room_component],
            outputs=room_panel["outputs"],
        )

    global_save = global_panel["save"].click(
        fn=provider_handlers.save_global_details,
        inputs=[
            global_panel["backend"],
            global_panel["auth_mode"],
            global_panel["project"],
            global_panel["location"],
            global_panel["key_ref"],
            global_panel["state"],
        ],
        outputs=global_panel["outputs"],
        **event_options,
    )
    room_save = room_panel["save"].click(
        fn=provider_handlers.save_room_details,
        inputs=[
            room_component,
            room_panel["inherit"],
            room_panel["backend"],
            room_panel["auth_mode"],
            room_panel["project"],
            room_panel["location"],
            room_panel["key_ref"],
            room_panel["state"],
            room_provider_component,
        ],
        outputs=room_panel["outputs"],
        **event_options,
    )
    key_events = {}
    if vertex_key_panel is not None:
        combined_outputs = vertex_key_panel["outputs"] + global_panel["outputs"]
        key_events["vertex_key_save"] = vertex_key_panel["save"].click(
            fn=handlers.save_vertex_key,
            inputs=[vertex_key_panel["api_key"], vertex_key_panel["key_ref"], vertex_key_panel["state"]],
            outputs=combined_outputs, **event_options,
        )
        key_events["vertex_key_ref_change"] = vertex_key_panel["key_ref"].input(
            fn=handlers.load_vertex_key_for_ref,
            inputs=[vertex_key_panel["key_ref"], vertex_key_panel["state"]],
            outputs=vertex_key_panel["outputs"],
            **event_options,
        )
        key_events["vertex_key_refresh"] = vertex_key_panel["refresh"].click(
            fn=handlers.load_vertex_key_and_connection, inputs=[],
            outputs=combined_outputs, **event_options,
        )
        # Gradio 6の初回読込は本体の1系列へ限定する。閉じた左カラムを
        # 別のloadで同時更新すると描画ループになる。最新値は明示再読込で取得する。
        global_save.then(fn=handlers.sync_vertex_key_after_connection_save,
                         inputs=[global_panel["status"]], outputs=vertex_key_panel["outputs"],
                         **event_options)
        room_save.then(fn=handlers.sync_vertex_key_choices_after_room_save,
                       inputs=[room_panel["status"], vertex_key_panel["key_ref"]],
                       outputs=vertex_key_panel["outputs"],
                       **event_options)
        global_refresh.then(fn=handlers.load_vertex_key, inputs=[],
                            outputs=vertex_key_panel["outputs"], **event_options)

    # 親側で既存の推論設定などへ `.then(...)` を追加できるよう、保存・再読込の
    # Dependencyを返す。ここで追加の推論処理は実行せず、接続UIの責務に限定する。
    return {
        "global_save": global_save,
        "room_save": room_save,
        "global_refresh": global_refresh,
        "room_refresh": room_refresh,
        **key_events,
    }


__all__ = ["build_gemini_connection_panel", "build_vertex_key_management_panel",
           "wire_gemini_connection_events", "wire_provider_connection_events"]


def wire_provider_connection_events(
    global_provider: Any, room_provider: Any, room: Any, switching: Any,
    global_panel: dict, room_panel: dict, global_groups: list, room_groups: list,
    global_developer: Any, room_developer: Any, room_status: Any, connection_events: dict,
    common_refresh: Any = None, room_refresh: Any = None,
    common_accordion: Any = None, room_accordion: Any = None,
) -> dict:
    """上位選択の1イベントで接続先とproviderを保存し、既存UIへ一括反映する。"""
    from ui_handlers import provider_connections as handlers
    from ui_handlers import gemini_connection as connection_handlers

    options = {"show_progress": "hidden", "api_visibility": "private"}
    global_view_outputs = [global_provider, *global_groups, global_developer, global_panel["container"]]
    room_view_outputs = [room_provider, *room_groups[:6], room_developer, room_panel["container"]]
    # Gradioのvalueはserver起動時のsnapshotなので、新しいセッションの
    # 最初の展開だけ最新値へ同期する。独立したdemo.loadは追加しない。
    if common_accordion is not None:
        initialized = gr.State(False)
        common_accordion.expand(fn=handlers.initialize_global, inputs=[initialized],
                                outputs=[initialized, *global_view_outputs, *global_panel["outputs"]],
                                **options)
    if room_accordion is not None:
        initialized = gr.State(False)
        room_accordion.expand(fn=handlers.initialize_room, inputs=[room, initialized],
                              outputs=[initialized, *room_view_outputs, *room_panel["outputs"]],
                              **options)
    global_select = global_provider.input(
        fn=handlers.select_global, inputs=[global_provider, global_panel["state"]],
        outputs=global_view_outputs + global_panel["outputs"], **options,
    )
    room_select = room_provider.input(
        fn=handlers.select_room, inputs=[room, room_provider, room_panel["state"], switching],
        outputs=[room_provider, *room_groups, room_developer, room_panel["container"],
                 *room_panel["outputs"], room_status], **options,
    )
    # 同じ画面でキー登録／共通接続を更新した後はroomのCAS印を同期する。
    # room本文が他画面で変わっていれば同期せず、従来どおり競合を拒否する。
    for event in [global_select, *(connection_events.get(name) for name in
                  ("global_save", "vertex_key_save", "vertex_key_refresh"))]:
        if event is not None:
            event.then(fn=handlers.acknowledge_common_change,
                       inputs=[room, room_panel["state"]], outputs=[room_panel["state"]], **options)
    # 初回loadを増やさず、ルーム切替と明示再読込だけで付随表示を追従させる。
    room.change(fn=handlers.room_route_view, inputs=[room], outputs=room_view_outputs, **options)
    for name in ("global_refresh", "global_save", "vertex_key_refresh"):
        event = connection_events.get(name)
        if event is not None:
            event.then(fn=handlers.global_route_view, inputs=[], outputs=global_view_outputs, **options)
    for name in ("room_refresh", "room_save"):
        event = connection_events.get(name)
        if event is not None:
            event.then(fn=handlers.room_route_view, inputs=[room], outputs=room_view_outputs, **options)
    if common_refresh is not None:
        common_refresh.click(fn=handlers.global_route_view, inputs=[], outputs=global_view_outputs,
                             **options).then(fn=connection_handlers.load_global, inputs=[],
                                             outputs=global_panel["outputs"], **options)
    if room_refresh is not None:
        room_refresh.click(fn=handlers.room_route_view, inputs=[room], outputs=room_view_outputs,
                           **options).then(fn=connection_handlers.load_room, inputs=[room],
                                           outputs=room_panel["outputs"], **options)
    return {"global_provider_select": global_select, "room_provider_select": room_select}
