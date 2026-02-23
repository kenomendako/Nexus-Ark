
content_part2 = r'''
def safe_tool_executor(state: AgentState):
    """
    AIのツール呼び出しを仲介し、計画されたファイル編集タスクを実行する。
    """
    import signature_manager
    from gemini_api import get_configured_llm
    from room_manager import read_main_memory, read_secret_diary, read_full_notepad, read_world_settings
    import room_manager
    import re
    import time
    import traceback
    
    print("--- ツール実行ノード (safe_tool_executor) 実行 ---")
    last_message = state['messages'][-1]
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        return {}

    tool_call = last_message.tool_calls[0]
    tool_name = tool_call["name"]
    tool_args = tool_call["args"]

    # --- [Dual-State] 最新の署名を取得 ---
    current_signature = signature_manager.get_thought_signature(state['room_name'])
    # -----------------------------------

    skip_execution = state.get("skip_tool_execution", False)
    side_effect_tools = ["plan_main_memory_edit", "plan_secret_diary_edit", "plan_notepad_edit", "plan_world_edit", "generate_image"]

    if skip_execution and tool_name in side_effect_tools:
        print(f"  - [リトライ検知] 副作用のあるツール '{tool_name}' の再実行をスキップします。")
        output = "【リトライ成功】このツールは直前の試行で既に正常に実行されています。その結果についてユーザーに報告してください。"
        tool_msg = ToolMessage(content=output, tool_call_id=tool_call["id"], name=tool_name)
        
        # 署名注入
        if current_signature:
            tool_msg.artifact = {"thought_signature": current_signature}
            
        return {"messages": [tool_msg]}

    room_name = state.get('room_name')
    api_key = state.get('api_key')

    is_plan_main_memory = tool_name == "plan_main_memory_edit"
    is_plan_secret_diary = tool_name == "plan_secret_diary_edit"
    is_plan_notepad = tool_name == "plan_notepad_edit"
    is_plan_world = tool_name == "plan_world_edit"

    output = ""

    if is_plan_main_memory or is_plan_secret_diary or is_plan_notepad or is_plan_world:
        try:
            print(f"  - ファイル編集プロセスを開始: {tool_name}")
            
            # バックアップ作成
            if is_plan_main_memory: room_manager.create_backup(room_name, 'memory')
            elif is_plan_secret_diary: room_manager.create_backup(room_name, 'secret_diary')
            elif is_plan_notepad: room_manager.create_backup(room_name, 'notepad')
            elif is_plan_world: room_manager.create_backup(room_name, 'world_setting')

            read_tool = None
            if is_plan_main_memory: read_tool = read_main_memory
            elif is_plan_secret_diary: read_tool = read_secret_diary
            elif is_plan_notepad: read_tool = read_full_notepad
            elif is_plan_world: read_tool = read_world_settings

            raw_content = read_tool.invoke({"room_name": room_name})

            if is_plan_main_memory or is_plan_secret_diary or is_plan_notepad:
                lines = raw_content.split('\\n')
                numbered_lines = [f"{i+1}: {line}" for i, line in enumerate(lines)]
                current_content = "\\n".join(numbered_lines)
            else:
                current_content = raw_content

            print(f"  - ペルソナAI ({state['model_name']}) に編集タスクを依頼します。")
            llm_persona = get_configured_llm(state['model_name'], state['api_key'], state['generation_config'])
 
            # テンプレート定義（省略せず記述）
            instruction_templates = {
                "plan_main_memory_edit": (
                    "【最重要指示：これは『対話』ではなく『記憶の設計タスク』です】\\n"
                    "あなたは今、自身の記憶ファイル(`memory_main.txt`)を更新するための『設計図』を作成しています。\\n\\n"
                    "このファイルは以下の厳格なセクションで構成されています。 **あなたは、他のセクションの見出しや説明文を決して変更・複製してはいけません。**\\n"
                    "  - `## 永続記憶 (Permanent)`: あなたの自己定義など、永続的な情報を記述する聖域です。\\n"
                    "  - `## 日記 (Diary)`: 日々の出来事や感情を時系列で記録する場所です。\\n"
                    "  - `## アーカイブ要約 (Archive Summary)`: システムが古い日記の要約を保管する場所です。\\n\\n"
                    "【あなたのタスク】\\n"
                    "あなたのタスクは、提示された【行番号付きデータ】とあなたの【変更要求】に基づき、**`## 日記` セクション内にのみ**変更を加えるための、完璧な【差分指示のリスト】を生成することです。\\n\\n"
                    "【行番号付きデータ（memory_main.txt全文）】\\n---\\n{current_content}\\n---\\n\\n"
                    "【あなたの変更要求】\\n「{modification_request}」\\n\\n"
                    "【絶対的な出力ルール】\\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\\n"
                    "- 各指示は \"operation\" ('replace', 'delete', 'insert_after'), \"line\" (対象行番号), \"content\" (新しい内容) のキーを持つ辞書です。\\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                 "plan_secret_diary_edit": (
                    "【最重要指示：これは『対話』ではなく『設計タスク』です】\\n"
                    "あなたは今、自身の秘密の日記(`secret_diary.txt`)を更新するための『設計図』を作成しています。\\n"
                    "このファイルは自由な書式のテキストファイルです。提示された【行番号付きデータ】とあなたの【変更要求】に基づき、完璧な【差分指示のリスト】を生成してください。\\n\\n"
                    "【行番号付きデータ（secret_diary.txt全文）】\\n---\\n{current_content}\\n---\\n\\n"
                    "【あなたの変更要求】\\n「{modification_request}」\\n\\n"
                    "【絶対的な出力ルール】\\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\\n"
                    "- 各指示は \"operation\" ('replace', 'delete', 'insert_after'), \"line\" (対象行番号), \"content\" (新しい内容) のキーを持つ辞書です。\\n\\n"
                    "- **【操作方法】**\\n"
                    "  - **`delete` (削除):** 指定した`line`番号の行を削除します。`content`は不要です。\\n"
                    "  - **`replace` (置換):** 指定した`line`番号の行を、新しい`content`に置き換えます。\\n"
                    "  - **`insert_after` (挿入):** 指定した`line`番号の**直後**に、新しい行として`content`を挿入します。\\n"
                    "  - **複数行の操作:** 複数行をまとめて削除・置換する場合は、**各行に対して**個別の指示を生成してください。\\n\\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_world_edit": (
                    "【最重要指示：これは『対話』ではなく『世界構築タスク』です】\\n"
                    "あなたは今、世界設定を更新するための『設計図』を作成しています。\\n"
                    "提示された【既存のデータ】とあなたの【変更要求】に基づき、完璧な【差分指示のリスト】を生成してください。\\n\\n"
                    "【既存のデータ（world_settings.txt全文）】\\n---\\n{current_content}\\n---\\n\\n"
                    "【あなたの変更要求】\\n「{modification_request}」\\n\\n"
                    "【絶対的な出力ルール】\\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\\n"
                    "- 各指示は \"operation\" ('update_place_description', 'add_place', 'delete_place'), \"area_name\", \"place_name\", \"value\" のキーを持つ辞書です。\\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                ),
                "plan_notepad_edit": (
                    "【最重要指示：これは『対話』ではなく『設計タスク』です】\\n"
                    "あなたは今、自身の短期記憶であるメモ帳(`notepad.md`)を更新するための『設計図』を作成しています。\\n"
                    "このファイルは自由な書式のテキストファイルです。提示された【行番号付きデータ】とあなたの【変更要求】に基づき、完璧な【差分指示のリスト】を生成してください。\\n\\n"
                    "【行番号付きデータ（notepad.md全文）】\\n---\\n{current_content}\\n---\\n\\n"
                    "【あなたの変更要求】\\n「{modification_request}」\\n\\n"
                    "【絶対的な出力ルール】\\n"
                    "- 思考や挨拶は含めず、【差分指示のリスト】（有効なJSON配列）のみを出力してください。\\n"
                    "- 各指示は \"operation\" ('replace', 'delete', 'insert_after'), \"line\" (対象行番号), \"content\" (新しい内容) のキーを持つ辞書です。\\n\\n"
                    "- **タイムスタンプ `[YYYY-MM-DD HH:MM]` はシステムが自動で付与するため、あなたは`content`に含める必要はありません。**\\n\\n"
                    "- **【操作方法】**\\n"
                    "  - **`delete` (削除):** 指定した`line`番号の行を削除します。`content`は不要です。\\n"
                    "  - **`replace` (置換):** 指定した`line`番号の行を、新しい`content`に置き換えます。\\n"
                    "  - **`insert_after` (挿入):** 指定した`line`番号の**直後**に、新しい行として`content`を挿入します。\\n"
                    "  - **複数行の操作:** 複数行をまとめて削除・置換する場合は、**各行に対して**個別の指示を生成してください。\\n\\n"
                    "- 出力は ` ```json ` と ` ``` ` で囲んでください。"
                )
            }
            formatted_instruction = instruction_templates[tool_name].format(
                current_content=current_content,
                modification_request=tool_args.get('modification_request')
            )
            edit_instruction_message = HumanMessage(content=formatted_instruction)

            history_for_editing = [msg for msg in state['messages'] if msg is not last_message]
            final_context_for_editing = [state['system_prompt']] + history_for_editing + [edit_instruction_message]

            if state.get("debug_mode", False):
                pass # デバッグ出力省略

            edited_content_document = None
            max_retries = 5
            base_delay = 5
            for attempt in range(max_retries):
                try:
                    response = llm_persona.invoke(final_context_for_editing)
                    edited_content_document = response.content.strip()
                    break
                except google_exceptions.ResourceExhausted as e:
                    error_str = str(e)
                    if "PerDay" in error_str or "Daily" in error_str:
                        raise RuntimeError("回復不能なAPIレート上限（日間など）に達したため、処理を中断しました。") from e
                    wait_time = base_delay * (2 ** attempt)
                    match = re.search(r"retry_delay {\s*seconds: (\d+)\s*}", error_str)
                    if match: wait_time = int(match.group(1)) + 1
                    if attempt < max_retries - 1:
                        time.sleep(wait_time)
                    else: raise e
                except (google_exceptions.ServiceUnavailable, google_exceptions.InternalServerError) as e:
                    if attempt < max_retries - 1:
                        wait_time = base_delay * (2 ** attempt)
                        time.sleep(wait_time)
                    else: raise e

            if edited_content_document is None:
                raise RuntimeError("編集AIからの応答が、リトライ後も得られませんでした。")

            print("  - AIからの応答を受け、ファイル書き込みを実行します。")

            if is_plan_main_memory or is_plan_secret_diary or is_plan_world or is_plan_notepad:
                json_match = re.search(r'```json\s*([\s\S]*?)\s*```', edited_content_document, re.DOTALL)
                content_to_process = json_match.group(1).strip() if json_match else edited_content_document
                instructions = json.loads(content_to_process)

                if is_plan_main_memory:
                    output = _apply_main_memory_edits(instructions=instructions, room_name=room_name)
                elif is_plan_secret_diary:
                    output = _apply_secret_diary_edits(instructions=instructions, room_name=room_name)
                elif is_plan_notepad:
                    output = _apply_notepad_edits(instructions=instructions, room_name=room_name)
                else: # is_plan_world
                    output = _apply_world_edits(instructions=instructions, room_name=room_name)

            if "成功" in output:
                output += " **このファイル編集タスクは完了しました。**あなたが先ほどのターンで計画した操作は、システムによって正常に実行されました。その結果についてユーザーに報告してください。"

        except Exception as e:
            output = f"ファイル編集プロセス中にエラーが発生しました ('{tool_name}'): {e}"
            traceback.print_exc()
    else:
        print(f"  - 通常ツール実行: {tool_name}")
        tool_args_for_log = tool_args.copy()
        if 'api_key' in tool_args_for_log: tool_args_for_log['api_key'] = '<REDACTED>'
        tool_args['room_name'] = room_name
        if tool_name in ['generate_image', 'search_past_conversations']:
            tool_args['api_key'] = api_key
            api_key_name = None
            try:
                for k, v in config_manager.GEMINI_API_KEYS.items():
                    if v == api_key:
                        api_key_name = k
                        break
            except Exception: api_key_name = None
            tool_args['api_key_name'] = api_key_name

        selected_tool = next((t for t in all_tools if t.name == tool_name), None)
        if not selected_tool: output = f"Error: Tool '{tool_name}' not found."
        else:
            try: output = selected_tool.invoke(tool_args)
            except Exception as e:
                output = f"Error executing tool '{tool_name}': {e}"
                traceback.print_exc()

    # ▼▼▼ 追加: 実行結果をログに出力 ▼▼▼
    print(f"  - ツール実行結果: {str(output)[:200]}...") 
    # ▲▲▲ 追加ここまで ▲▲▲

    # --- [Thinkingモデル対応] ToolMessageへの署名注入 ---
    tool_msg = ToolMessage(content=str(output), tool_call_id=tool_call["id"], name=tool_name)
    
    if current_signature:
        # LangChain Google GenAI の実装によっては artifact を使う可能性がある
        tool_msg.artifact = {"thought_signature": current_signature}
        print(f"  - [Thinking] ツール実行結果に署名を付与しました。")

    return {"messages": [tool_msg], "loop_count": state.get("loop_count", 0)}

def route_after_agent(state: AgentState) -> Literal["__end__", "safe_tool_node", "agent"]:
    print("--- エージェント後ルーター (route_after_agent) 実行 ---")
    if state.get("force_end"): return "__end__"
    
    last_message = state["messages"][-1]
    loop_count = state.get("loop_count", 0)
    if last_message.tool_calls:
        print("  - ツール呼び出しあり。ツール実行ノードへ。")
        return "safe_tool_node"

    import config_manager
    active_provider = config_manager.get_active_provider()
    
    # Google以外のプロバイダ（OpenAI/Groq等）の場合は、レート制限回避のため再思考ループを無効化する
    # if active_provider != "google":
    #     print(f"  - [Route] Provider is '{active_provider}'. Re-thinking loop disabled to save tokens.")
    #     return "__end__"

    if loop_count < 2:
        print(f"  - ツール呼び出しなし。再思考します。(ループカウント: {loop_count})")
        return "agent"
    print(f"  - ツール呼び出しなし。最大ループ回数({loop_count})に達したため、グラフを終了します。")
    return "__end__"

workflow = StateGraph(AgentState)
workflow.add_node("context_generator", context_generator_node)
workflow.add_node("retrieval_node", retrieval_node)
workflow.add_node("agent", agent_node)
workflow.add_node("safe_tool_node", safe_tool_executor)

workflow.set_entry_point("context_generator")

workflow.add_edge("context_generator", "retrieval_node")
workflow.add_edge("retrieval_node", "agent")

workflow.add_conditional_edges("agent", route_after_agent, {"safe_tool_node": "safe_tool_node", "agent": "agent", "__end__": END})
workflow.add_edge("safe_tool_node", "agent")
app = workflow.compile()
'''
