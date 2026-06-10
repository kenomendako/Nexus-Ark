# agent/sub_agent_node.py
import json
import re
import traceback
from langchain_core.messages import AIMessage, ToolMessage, SystemMessage, HumanMessage
from llm_factory import LLMFactory
from tools.roblox_tools import send_roblox_command, get_spatial_data
import utils

def sub_agent_executor(state):
    """
    複雑なタスク（建築など）を自律的に処理するサブエージェントノード。
    メインエージェントの意図を具体的な手順に落とし込み、実行します。
    """
    print("--- サブエージェント・エグゼキューター (sub_agent_executor) 実行 ---")
    
    room_name = state.get('room_name')
    last_message = state['messages'][-1]
    
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        return {}

    tool_call = last_message.tool_calls[0]
    tool_name = tool_call["name"]
    tool_args = tool_call["args"]

    if tool_name != "roblox_build":
        return {}

    instruction = tool_args.get("instruction")
    
    # 1. 空間認識データを取得
    spatial_data = get_spatial_data(room_name)
    spatial_context = json.dumps(spatial_data, indent=2, ensure_ascii=False) if spatial_data else "利用可能な空間データがありません。"

    # 2. サブエージェント用LLMの準備
    sub_agent_prompt = (
        "あなたは Nexus Ark のRoblox建築専門サブエージェントです。\n"
        "【任務】メインAIからの建築指示を、Robloxの `send_roblox_command` (build) 用の具体的なデータ構造に変換してください。\n"
        "\n"
        "【建築の知識】\n"
        "- ベンチ: 座面(4,0.3,2), 背もたれ(4,1.5,0.2), 足4本(0.3,1.5,0.3) で構成。材質は Wood が望ましい。\n"
        "- テーブル: 天板(4,0.2,4), 足1本(0.5,3,0.5) または足4本。材質は Wood や Plastic。\n"
        "- 材質(material): Wood, Metal, Brick, Concrete, Neon, Glass, Grass, Sand, Fabric, Granite, Marble など。\n"
        "- 座標: 空間データに基づき、NPCの近辺（かつ重ならない位置）に配置してください。地面の高さ(Y)は概ね 0.1 〜 1.0 です。\n"
        "\n"
        "【現在の空間認識データ】\n"
        f"{spatial_context}\n"
        "\n"
        "【建築指示】\n"
        f"「{instruction}」\n"
        "\n"
        "【出力形式】\n"
        "以下の構造を持つ単一の JSON オブジェクトを出力してください。\n"
        "{\n"
        "  \"name\": \"建築物の名前\",\n"
        "  \"parts\": [\n"
        "    {\"pos\": [x,y,z], \"size\": [x,y,z], \"color\": [r,g,b], \"material\": \"材質名\"},\n"
        "    ...\n"
        "  ]\n"
        "}\n"
        "※ 余計な解説は一切含めず、純粋なJSONのみを出力してください。\n"
    )

    import time
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            llm = LLMFactory.create_chat_model(
                model_name=state.get('model_name'),
                api_key=state.get('api_key'),
                generation_config={"temperature": 0.1},
                room_name=room_name
            )
            
            response = llm.invoke([HumanMessage(content=sub_agent_prompt)])
            content = utils.get_content_as_string(response).strip()
            break
        except Exception as e:
            if "503" in str(e) and attempt < max_retries:
                print(f"  - [SubAgent] 503 Error. Retrying... (Attempt {attempt + 1})")
                time.sleep(2 * (attempt + 1))
                continue
            raise e

    try:
        # JSON抽出
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            build_data = json.loads(json_match.group(0))
        else:
            build_data = json.loads(content)

        name = build_data.get("name", "BuiltObject")
        parts = build_data.get("parts", [])
        
        # 3. 実行（1KB制限を考慮した分割送信）
        # Roblox MessagingService は1メッセージ1KB制限があるため、
        # パーツが多い場合は自動的に分割して送信する。
        results = []
        MAX_PARTS_PER_MSG = 5 # 1回に送るパーツ数の目安
        
        for i in range(0, len(parts), MAX_PARTS_PER_MSG):
            batch = parts[i:i+MAX_PARTS_PER_MSG]
            batch_name = name if i == 0 else f"{name} (Part {i//MAX_PARTS_PER_MSG + 1})"
            
            payload = {
                "name": batch_name,
                "parts": batch
            }
            
            print(f"  - サブエージェント送信 (Batch {i//MAX_PARTS_PER_MSG + 1}): {len(batch)} parts")
            
            cmd_res = send_roblox_command.invoke({
                "command_type": "build",
                "parameters": payload,
                "room_name": room_name
            })
            results.append(cmd_res)

        output = f"【サブエージェント報告】建築指示「{instruction}」に基づき、計 {len(parts)} 個のパーツを配置しました。\n詳細:\n" + "\n".join(results)
        
    except Exception as e:
        output = f"【サブエージェント・エラー】建築処理中に不具合が発生しました: {str(e)}"
        traceback.print_exc()

    tool_msg = ToolMessage(content=output, tool_call_id=tool_call["id"], name=tool_name)
    return {"messages": [tool_msg]}

