import json
import re
import config_manager
from typing import List, Callable, Dict, Optional, Set
import tools.roblox_webhook as roblox_webhook
import room_manager

class ToolRegistry:
    """
    ツールの動的登録・カテゴリ管理を行うクラス。
    ルーム設定やシステム状態に応じて、最適なツールセットをAIに提供します。
    """
    
    def __init__(self, all_tools_list: List[Callable]):
        self._all_tools_map = {t.name: t for t in all_tools_list}
        
        # 基本ツール（常時有効）
        self.CORE_TOOL_NAMES = [
            "request_capability",
            "list_available_locations", "set_current_location", "read_world_settings", "plan_world_edit",
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
            "place_item_to_location", "pickup_item_from_location", "list_location_items", "consume_item_from_location",
            "create_standard_item", "examine_item", "create_and_gift_item"
        ]
        
        # 特殊カテゴリ
        self.ROBLOX_TOOL_NAMES = ["send_roblox_command", "roblox_build", "capture_roblox_screenshot"]
        self.CHESS_TOOL_NAMES = ["read_board_state", "perform_move", "get_legal_moves", "reset_chess_game"]
        self.DEVELOPER_TOOL_NAMES = ["list_project_files", "read_project_file"]
        self.TWITTER_TOOL_NAMES = ["draft_tweet", "post_tweet", "get_twitter_timeline", "get_twitter_mentions", "get_twitter_notifications"]
        self.CUSTOM_TOOL_NAMES: List[str] = []
        self.BROKER_TOOL_NAMES = ["request_capability"]

        self.DEFAULT_TOOL_NAMES = list(self.BROKER_TOOL_NAMES)
        self.TOOL_CATEGORIES: Dict[str, List[str]] = {
            "world": ["list_available_locations", "set_current_location", "read_world_settings", "plan_world_edit"],
            "memory": [
                "recall_memories", "search_past_conversations", "read_memory_context",
                "read_identity_memory", "plan_identity_memory_edit",
                "read_diary_memory", "plan_diary_append",
                "read_secret_diary", "plan_secret_diary_edit",
                "read_entity_memory", "write_entity_memory", "list_entity_memories", "search_entity_memory",
            ],
            "notes": [
                "read_full_notepad", "plan_notepad_edit",
                "read_working_memory", "update_working_memory", "list_working_memories", "switch_working_memory",
                "read_creative_notes", "plan_creative_notes_edit",
                "read_research_notes", "plan_research_notes_edit",
            ],
            "web": ["web_search_tool", "read_url_tool"],
            "knowledge": ["search_knowledge_base"],
            "image": ["generate_image", "view_past_image"],
            "time": ["set_personal_alarm", "set_timer", "set_pomodoro_timer"],
            "autonomy": ["schedule_next_action", "cancel_action_plan", "read_current_plan", "send_user_notification"],
            "watchlist": ["add_to_watchlist", "remove_from_watchlist", "get_watchlist", "check_watchlist", "update_watchlist_interval"],
            "items": [
                "list_my_items", "consume_item", "gift_item_to_user", "create_food_item",
                "place_item_to_location", "pickup_item_from_location", "list_location_items", "consume_item_from_location",
                "create_standard_item", "examine_item", "create_and_gift_item",
            ],
            "chess": self.CHESS_TOOL_NAMES,
            "developer": self.DEVELOPER_TOOL_NAMES,
            "roblox": self.ROBLOX_TOOL_NAMES,
            "twitter": self.TWITTER_TOOL_NAMES,
            "custom": self.CUSTOM_TOOL_NAMES,
        }
        
        # カスタムツールのロード
        self.custom_tools = []
        try:
            settings = config_manager.load_config_file()
            custom_settings = settings.get("custom_tools_settings", {})
            
            if custom_settings.get("enabled", True):
                from custom_tool_manager import CustomToolManager
                ct_manager = CustomToolManager()
                self.custom_tools = ct_manager.get_all_custom_tools()
                
                for t in self.custom_tools:
                    if t.name not in self._all_tools_map:
                        self._all_tools_map[t.name] = t
                    if t.name not in self.CUSTOM_TOOL_NAMES:
                        self.CUSTOM_TOOL_NAMES.append(t.name)
                        # print(f"  - [ToolRegistry] カスタムツールを登録しました: {t.name}")
                self.TOOL_CATEGORIES["custom"] = list(self.CUSTOM_TOOL_NAMES)
        except Exception as e:
            print(f"  - [ToolRegistry] カスタムツールのロード中にエラー: {e}")

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
            
        # Twitterモード判定
        if self._is_twitter_enabled(room_name):
            active_names.extend(self.TWITTER_TOOL_NAMES)
            
        # チェス（現状は常時有効に近いが、将来的に明示的なフラグで制御可能にする）
        active_names.extend(self.CHESS_TOOL_NAMES)
        
        # 開発者ツール（デバッグモード等の条件で制御可能）
        # 現状は互換性維持のため追加
        active_names.extend(self.DEVELOPER_TOOL_NAMES)
        
        # カスタムツールを追加
        for t in self.custom_tools:
            if t.name not in active_names:
                active_names.append(t.name)
        
        # 存在するツールのみを抽出
        return [self._all_tools_map[name] for name in active_names if name in self._all_tools_map]

    def select_tools_for_turn(
        self,
        room_name: str,
        latest_user_text: str = "",
        tool_use_enabled: bool = True,
        model_name: str = "",
        is_roblox_active: Optional[bool] = None,
        image_generation_enabled: bool = True,
    ) -> List[Callable]:
        """
        現在のターンでモデルに直接提示するツールをカテゴリ単位で絞り込む。

        能力自体は ToolRegistry に残したまま、毎回すべての tool schema を bind しないことで、
        Gemini 3 Flash などの reasoning モデルに渡る入力負荷を下げる。
        """
        if not tool_use_enabled:
            return []

        requested_category = self.extract_requested_capability(latest_user_text)
        if requested_category:
            return self.get_tools_for_capability(
                room_name=room_name,
                category=requested_category,
                tool_use_enabled=tool_use_enabled,
                is_roblox_active=is_roblox_active,
                image_generation_enabled=image_generation_enabled,
            )

        active_tools = self.get_active_tools(room_name, tool_use_enabled=tool_use_enabled)

        active_names = {tool.name for tool in active_tools}
        ordered_active_names = [tool.name for tool in active_tools]
        if is_roblox_active is True:
            for name in self.ROBLOX_TOOL_NAMES:
                if name in self._all_tools_map:
                    active_names.add(name)
                    if name not in ordered_active_names:
                        ordered_active_names.append(name)
        if not image_generation_enabled:
            active_names.discard("generate_image")

        if is_roblox_active is False:
            active_names.difference_update(self.ROBLOX_TOOL_NAMES)

        selected_names = set(self.DEFAULT_TOOL_NAMES)

        ordered_names = [name for name in ordered_active_names if name in selected_names and name in active_names]
        return [self._all_tools_map[name] for name in ordered_names if name in self._all_tools_map]

    def get_tools_for_capability(
        self,
        room_name: str,
        category: str,
        tool_use_enabled: bool = True,
        is_roblox_active: Optional[bool] = None,
        image_generation_enabled: bool = True,
    ) -> List[Callable]:
        active_tools = self.get_active_tools(room_name, tool_use_enabled=tool_use_enabled)
        if not tool_use_enabled:
            return []

        active_names = {tool.name for tool in active_tools}
        ordered_active_names = [tool.name for tool in active_tools]
        if is_roblox_active is True:
            for name in self.ROBLOX_TOOL_NAMES:
                if name in self._all_tools_map:
                    active_names.add(name)
                    if name not in ordered_active_names:
                        ordered_active_names.append(name)
        if is_roblox_active is False:
            active_names.difference_update(self.ROBLOX_TOOL_NAMES)
        if not image_generation_enabled:
            active_names.discard("generate_image")

        selected_names = set(self.TOOL_CATEGORIES.get((category or "").strip().lower(), []))
        selected_names.update(self.BROKER_TOOL_NAMES)
        ordered_names = [name for name in ordered_active_names if name in selected_names and name in active_names]
        return [self._all_tools_map[name] for name in ordered_names if name in self._all_tools_map]

    def extract_requested_capability(self, text: str) -> Optional[str]:
        if not text or "【能力要求を受け付けました】" not in text:
            return None
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except Exception:
            return None
        category = str(payload.get("category", "")).strip().lower()
        return category if category in self.TOOL_CATEGORIES else None

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

    def _is_twitter_enabled(self, room_name: str) -> bool:
        """Twitter設定が有効か判定"""
        try:
            # room_manager を使用して設定を取得
            config = room_manager.get_room_config(room_name)
            if not config:
                return True # デフォルトで有効（ツールが表示されるように）

            # override_settings.twitter_settings を参照
            overrides = config.get("override_settings", {})
            twitter_settings = overrides.get("twitter_settings", {})
            
            # enabled フラグを確認（デフォルト False）
            return twitter_settings.get("enabled", False)
        except Exception:
            return False # エラー時も安全側に倒して False

    def get_all_tools(self) -> List[Callable]:
        """登録されている全てのツールを返す（互換性用）"""
        return list(self._all_tools_map.values())

    def get_custom_tool_catalog(self, limit: int = 30) -> str:
        """通常ターンのプロンプトに載せる短い拡張ツールカタログを返す。"""
        if not self.custom_tools:
            return "現在有効な拡張ツールはありません。"

        lines = []
        for tool in self.custom_tools[:limit]:
            meta = getattr(tool, "nexus_tool_metadata", {}) or {}
            source = meta.get("source", "custom")
            source_name = meta.get("source_name", "")
            summary = meta.get("summary") or getattr(tool, "description", "")
            use_when = meta.get("use_when", "")
            source_label = "Local Plugin" if source == "local_plugin" else "MCP" if source == "mcp" else source
            desc = str(summary or "").strip()
            if len(desc) > 120:
                desc = desc[:119].rstrip() + "..."
            line = f"- `{tool.name}` ({source_label}: {source_name}): {desc}"
            if use_when:
                use_text = str(use_when).strip()
                if len(use_text) > 90:
                    use_text = use_text[:89].rstrip() + "..."
                line += f" / 使う場面: {use_text}"
            lines.append(line)

        remaining = len(self.custom_tools) - len(lines)
        if remaining > 0:
            lines.append(f"- 他 {remaining} 件の拡張ツールがあります。必要なら `custom` を要求してください。")
        return "\n".join(lines)

    def is_custom_tool(self, tool_name: str) -> bool:
        return tool_name in self.CUSTOM_TOOL_NAMES
