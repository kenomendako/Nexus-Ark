from typing import Optional, Dict, Any, Union
import json
import logging
import requests
from langchain_core.tools import tool

import config_manager

logger = logging.getLogger(__name__)

# ROBLOX Open Cloud API の Messaging Service エンドポイント (V2)
ROBLOX_MESSAGING_API_V2_URL = "https://apis.roblox.com/cloud/v2/universes/{universe_id}:publishMessage"

from typing import Optional, Dict, Any, Union
from langchain_core.tools import tool

@tool
def send_roblox_command(
    command_type: str, 
    text: Optional[str] = None,
    animation_id: Optional[str] = None,
    x: Optional[float] = None,
    z: Optional[float] = None,
    player_name: Optional[str] = None,
    parameters: Optional[Dict[str, Any]] = None,
    room_name: str = "", 
    **kwargs
) -> str:
    """
    ROBLOXのゲーム内NPC（仮想の自分自身）へコマンドを送信し、アバターを操作します。
    ユーザーが「ジャンプして」「手を振って」「〇〇と言って」「〇〇へ移動して」など、ROBLOXの世界における行動を指示した場合に使用します。
    
    Args:
        command_type: 以下のいずれかを正確に指定してください。
            - "jump" : NPCをジャンプさせる（パラメータ不要: {}）
            - "chat" : NPCに吹き出しで発言させる（パラメータ: {"text": "セリフ"}）
            - "move" : NPCを指定座標へ移動させる（パラメータ: {"x": 10, "z": 20}）
            - "emote" : NPCにアニメーションを再生させる（パラメータ: {"animation_id": "rbxassetid://..."}）
            - "follow" : プレイヤーの後を自動的についていく（パラメータ: {"player_name": "ユーザー名"}）
            - "stop" : 追従や移動を即座に停止して待機する（パラメータ不要: {}）
            - "sit" : 近くにある空いている椅子（Seat）を探して座る（パラメータ不要: {}）
            - "stand" : 座っている状態から立ち上がる（パラメータ不要: {}）
              - "build" : ROBLOX空間内にパーツ（ブロック）を生成する。材質や形状、透明度（"transparency": 0〜1）を指定可能です。
                パラメータ例1（単一パーツ）: {"pos": [x,y,z], "size": [x,y,z], "color": [r,g,b], "material": "Wood", "transparency": 0.5}
                パラメータ例2（複数パーツ）: {"name": "モデル名", "parts": [{"pos": [x,y,z], "size": [x,y,z], "color": [r,g,b], "transparency": 0.3}, ...]}
              - "terrain" : 地形を直接生成・操作します（水や砂地など）。
                パラメータ例: {"material": "Water", "pos": [x,y,z], "size": [x,y,z]}
              - "environment" : 時間帯や天候を変更します。
        text: chatコマンド用のセリフ等、NPCに発言させたい文章
        animation_id: emoteコマンド用のアニメーションID (例: rbxassetid://...)
        x: move時の目標X座標
        z: move時の目標Z座標
        player_name: follow等で対象とするプレイヤー名
        parameters: 建築(build)や地形(terrain)など、その他複雑なパラメータを渡すための辞書
        kwargs: その他の余剰引数も安全に受け取ります
        room_name: (システムで自動入力)
        
    Returns:
        コマンドの送信結果。
    """
    def execute_send_roblox_command(
        command_type: str, 
        text: Optional[str] = None,
        animation_id: Optional[str] = None,
        x: Optional[float] = None,
        z: Optional[float] = None,
        player_name: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        room_name: str = "", 
        **kwargs
    ) -> str:
        try:
            # Pydanticエラーを避けるため、明示的な引数とkwargsをマージしてパラメータを構築する
            param_dict = parameters or {}
            if not isinstance(param_dict, dict):
                param_dict = {}
                
            # 明示的なトップレベル引数を param_dict に組み込む（存在する場合のみ）
            if text is not None: param_dict["text"] = text
            if animation_id is not None: param_dict["animation_id"] = animation_id
            if x is not None: param_dict["x"] = x
            if z is not None: param_dict["z"] = z
            if player_name is not None: param_dict["player_name"] = player_name
                
            # もしAIが `message` 等のその他の名前で送ってきた場合は kwargs そのものを利用する
            if kwargs:
                for k, v in kwargs.items():
                    if k not in param_dict and k != "parameters":
                        param_dict[k] = v
                
            # AIが使いがちなキーを、Robloxが期待するキーにマッピング（正規化）
            if command_type == "chat":
                if "message" in param_dict and "text" not in param_dict:
                    param_dict["text"] = param_dict.pop("message")
            elif command_type == "follow":
                if "player" in param_dict and "player_name" not in param_dict:
                    param_dict["player_name"] = param_dict.pop("player")
                elif "target" in param_dict and "player_name" not in param_dict:
                    param_dict["player_name"] = param_dict.pop("target")
            elif command_type == "move":
                # AIが [x, y, z] のリストで送ってきた場合の補完
                if "pos" in param_dict and isinstance(param_dict["pos"], list) and len(param_dict["pos"]) >= 2:
                    if "x" not in param_dict: param_dict["x"] = param_dict["pos"][0]
                    if "z" not in param_dict: param_dict["z"] = param_dict["pos"][-1]
                elif "destination" in param_dict and isinstance(param_dict["destination"], list) and len(param_dict["destination"]) >= 2:
                    if "x" not in param_dict: param_dict["x"] = param_dict["destination"][0]
                    if "z" not in param_dict: param_dict["z"] = param_dict["destination"][-1]
            elif command_type == "emote":
                if "emote_id" in param_dict and "animation_id" not in param_dict:
                    param_dict["animation_id"] = param_dict.pop("emote_id")
                elif "value" in param_dict and "animation_id" not in param_dict:
                    param_dict["animation_id"] = param_dict.pop("value")
                
                # IDが名前のままの場合の最終フォールバック変換
                anim_id = param_dict.get("animation_id", "")
                if isinstance(anim_id, str) and not anim_id.startswith("rbxassetid://"):
                    anim_lower = anim_id.lower()
                    emote_map = {
                        "wave": "rbxassetid://507770239",
                        "cheer": "rbxassetid://507770677",
                        "laugh": "rbxassetid://507770818",
                        "dance": "rbxassetid://507771019",
                        "dance2": "rbxassetid://507771919",
                        "dance3": "rbxassetid://507772104",
                        "point": "rbxassetid://507770453",
                        "手を振": "rbxassetid://507770239",
                        "応援": "rbxassetid://507770677",
                        "笑": "rbxassetid://507770818",
                        "踊": "rbxassetid://507771019",
                        "指さ": "rbxassetid://507770453",
                    }
                    for key, full_id in emote_map.items():
                        if key in anim_lower:
                            param_dict["animation_id"] = full_id
                            break

            # 送信するペイロードの構築
            payload = {
                "type": command_type,
                "data": param_dict
            }
            
            # 個別設定からAPIキーとUniverse IDを取得
            from room_manager import get_room_config
            room_config = get_room_config(room_name)
            
            # config.json の共通設定または room_config.json の override_settings の両方をチェック
            # ROBLOX連携設定は基本的に部屋ごとの override_settings に保存される
            override_settings = room_config.get("override_settings", {})
            roblox_settings = override_settings.get("roblox_settings", room_config.get("roblox_settings", {}))
            
            # デバッグログの出力（開発者用）
            logger.info(f"ROBLOXコマンド送信準備: topic={roblox_settings.get('topic')}, type={command_type}, data={json.dumps(param_dict, ensure_ascii=False)}")

            # --- [Step 14] 文字置き換え（フィルタリング）の適用 ---
            if command_type == "chat" and "text" in param_dict:
                filtering_enabled = roblox_settings.get("filtering_enabled", True)
                if filtering_enabled:
                    try:
                        original_text = param_dict["text"]
                        redaction_rules = config_manager.load_redaction_rules()
                        # load_redaction_rules() は List[Dict] を返す
                        # 各ルール: {"find": "置換元", "replace": "置換先", "color": "#..."}
                        if redaction_rules:
                            modified_text = original_text
                            for rule in redaction_rules:
                                find_str = rule.get("find", "")
                                replace_str = rule.get("replace", "")
                                if find_str:
                                    modified_text = modified_text.replace(find_str, replace_str)
                            
                            if modified_text != original_text:
                                logger.info(f"ROBLOXチャットをフィルタリングしました: '{original_text}' -> '{modified_text}'")
                                param_dict["text"] = modified_text
                                payload["data"] = param_dict
                    except Exception as e:
                        logger.warning(f"ROBLOXチャットフィルタリング中にエラーが発生しました（送信は続行）: {e}")
            # -----------------------------------------------------
            
            universe_id = roblox_settings.get("universe_id")
            api_key = roblox_settings.get("api_key")
            topic = roblox_settings.get("topic", "NexusArkCommands")
            
            if not universe_id or not api_key:
                return "エラー: ROBLOXへの接続設定（Universe IDまたはAPIキー）が未設定です。設定画面から登録してください。"
                
            url = ROBLOX_MESSAGING_API_V2_URL.format(universe_id=universe_id)
            
            headers = {
                "x-api-key": api_key,
                "Content-Type": "application/json"
            }
            
            # 1KB制限に収まるようにJSON化
            message_str = json.dumps(payload, ensure_ascii=False)
            if len(message_str.encode('utf-8')) > 1024:
                return "エラー: 送信メッセージが1KB (1024バイト) の制限を超過しています。パラメータを短くしてください。"
            
            # V2 APIは `topic` と `message` を同一階層のJSONパラメーターとして要求します
            message_payload = {
                "topic": topic,
                "message": message_str
            }
            
            response = requests.post(url, headers=headers, json=message_payload, timeout=10)
            
            if response.status_code == 200:
                logger.info(f"ROBLOXコマンド送信成功: type={command_type}, topic={topic}")
                return f"成功: コマンド [{command_type}] をROBLOXへ送信しました。（Topic: {topic}）"
            else:
                logger.warning(f"ROBLOXコマンド送信失敗: Status {response.status_code}, body={response.text}")
                return f"エラー: ROBLOXへの送信に失敗しました (Status {response.status_code}): {response.text}"
                
        except Exception as e:
            logger.error(f"ROBLOXコマンド送信エラー: {e}")
            return f"エラー: 予期せぬ例外が発生しました - {str(e)}"
    
    return execute_send_roblox_command(
        command_type=command_type, 
        text=text,
        animation_id=animation_id,
        x=x,
        z=z,
        player_name=player_name,
        parameters=parameters,
        room_name=room_name,
        **kwargs
    )

def test_roblox_connection(room_name: str, api_key: str, universe_id: str, topic: str) -> str:
    """
    ROBLOX連携の接続テストを行う内部関数（AIツールではない）。
    UIから設定確認のために直接呼び出されます。
    """
    if not universe_id or not api_key:
        return "⚠️ エラー: APIキーとUniverse IDの両方を入力してください。"
        
    topic = topic or "NexusArkCommands"
    url = ROBLOX_MESSAGING_API_V2_URL.format(universe_id=universe_id)
    
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json"
    }
    
    payload = {
        "type": "ping",
        "data": {"message": "接続テストのメッセージです"}
    }
    
    message_str = json.dumps(payload, ensure_ascii=False)
    message_payload = {
        "topic": topic,
        "message": message_str
    }
    
    try:
        response = requests.post(url, headers=headers, json=message_payload, timeout=10)
        
        if response.status_code == 200:
            return f"✅ 接続成功！\nROBLOXへのメッセージ送信テストに成功しました。\n(Status 200)"
        elif response.status_code == 401:
            return f"❌ 認証エラー (Status 401)\nAPIキーが間違っているか、権限が不足しています。\n詳細: {response.text}"
        elif response.status_code == 403:
            return f"❌ 権限エラー (Status 403)\n対象のUniverse IDに対するアクセス権がないか、IPアドレス制限にブロックされています。\n詳細: {response.text}"
        elif response.status_code == 404:
            return f"❌ 見つかりません (Status 404)\nUniverse IDが間違っている可能性があります。\n詳細: {response.text}"
        else:
            return f"⚠️ 送信失敗 (Status {response.status_code})\n詳細: {response.text}"
            
    except requests.exceptions.RequestException as e:
        return f"❌ 通信エラー\nRobloxサーバーに到達できませんでした。ネットワーク接続を確認してください。\n詳細: {str(e)}"
@tool
def roblox_build(instruction: str, room_name: str = "") -> str:
    """
    ROBLOX空間内に複数のオブジェクトを配置する高レベルな建築コマンド。
    具体的な座標指定は不要です。自然言語で指示を記述してください。
    サブエージェントが空間を認識し、自動的に最適な座標計算とパーツ配置を行います。
    
    例: "噴水の周囲にベンチを4つ等間隔で配置する"
    例: "NPCの前方3メートルにテーブルと椅子を置く"
    
    Args:
        instruction: 建築・配置の指示（自然言語）
        room_name: (システムで自動入力)
    """
    # 実際の実装は graph.py の safe_tool_executor で sub_agent_node にルーティングされるため、
    # ここでは呼び出しの証跡を返すのみ
    return f"建築指示「{instruction}」を受諾しました。サブエージェントを起動します..."

def get_spatial_data(room_name: str) -> Dict[str, Any]:
    """空間認識データを取得する（内部ツール用）"""
    try:
        from tools.roblox_webhook import get_spatial_data as _get_spatial
        return _get_spatial(room_name)
    except ImportError:
        return {}
