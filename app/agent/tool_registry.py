import config_manager
from typing import List, Callable, Dict
import tools.roblox_webhook as roblox_webhook

class ToolRegistry:
    """
    ツールの動的登録・カテゴリ管理を行うクラス。
    ルーム設定やシステム状態に応じて、最適なツールセットをAIに提供します。
    """
    
    def __init__(self, all_tools_list: List[Callable]):
        self._all_tools_map = {t.name: t for t in all_tools_list}
        
        # 基本ツール（常時有効）
        self.CORE_TOOL_NAMES = [
            "set_current_location", "read_world_settings", "plan_world_edit",
            "recall_memories", "search_past_conversations", "read_memory_context",
            "read_identity_memory", "plan_identity_memory_edit", 
            "read_diary_memory", "plan_diary_append", 
            "read_secret_diary", "plan_secret_diary_edit",
            "read_full_notepad", "plan_notepad_edit",
            "read_working_memory", "update_working_memory", "list_working_memories", "switch_working_memory",
            "web_search_tool", "read_url_tool",
            "generate_image", "view_past_image",
            "set_personal_alarm", "set_timer", "set_pomodoro_timer",
            "search_knowledge_base",
            "read_entity_memory", "write_entity_memory", "list_entity_memories", "search_entity_memory",
            "schedule_next_action", "cancel_action_plan", "read_current_plan",
            "send_user_notification",
            "read_creative_notes", "plan_creative_notes_edit",
            "read_research_notes", "plan_research_notes_edit",
            "add_to_watchlist", "remove_from_watchlist", "get_watchlist", "check_watchlist", "update_watchlist_interval",
            "manage_open_questions", "manage_goals",
            "list_my_items", "consume_item", "gift_item_to_user", "create_food_item",
            "place_item_to_location", "pickup_item_from_location", "list_location_items", "consume_item_from_location"
        ]
        
        # 特殊カテゴリ
        self.ROBLOX_TOOL_NAMES = ["send_roblox_command", "roblox_build", "capture_roblox_screenshot"]
        self.CHESS_TOOL_NAMES = ["read_board_state", "perform_move", "get_legal_moves", "reset_chess_game"]
        self.DEVELOPER_TOOL_NAMES = ["list_project_files", "read_project_file"]

    def get_active_tools(self, room_name: str, tool_use_enabled: bool = True) -> List[Callable]:
        """
        ルーム名と設定に基づき、現在アクティブなツールのリストを返す。
        """
        if not tool_use_enabled:
            return []
            
        active_names = list(self.CORE_TOOL_NAMES)
        
        # Robloxモード判定
        if self._is_roblox_enabled(room_name):
            active_names.extend(self.ROBLOX_TOOL_NAMES)
            
        # チェス（現状は常時有効に近いが、将来的に明示的なフラグで制御可能にする）
        active_names.extend(self.CHESS_TOOL_NAMES)
        
        # 開発者ツール（デバッグモード等の条件で制御可能）
        # 現状は互換性維持のため追加
        active_names.extend(self.DEVELOPER_TOOL_NAMES)
        
        # 存在するツールのみを抽出
        return [self._all_tools_map[name] for name in active_names if name in self._all_tools_map]

    def _is_roblox_enabled(self, room_name: str) -> bool:
        """Roblox設定が有効か判定（モード設定と接続状態に基づく）"""
        try:
            settings = config_manager.load_room_settings(room_name)
            roblox_settings = settings.get("roblox_settings", {})
            
            # 1. そもそもAPIキーとUniverseIDがない場合は問答無用で無効
            if not (roblox_settings.get("api_key") and roblox_settings.get("universe_id")):
                return False
                
            # 2. 有効化モードの取得 (デフォルトは 'auto')
            mode = roblox_settings.get("activation_mode", "auto")
            
            if mode == "disabled":
                return False
            elif mode == "enabled":
                return True
            elif mode == "auto":
                # Webhook通信があれば有効、なければ無効
                return roblox_webhook.is_room_active(room_name)
            
            return False
        except Exception:
            return False

    def get_all_tools(self) -> List[Callable]:
        """登録されている全てのツールを返す（互換性用）"""
        return list(self._all_tools_map.values())
