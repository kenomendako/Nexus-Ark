import json
import re
import config_manager
import agent_delegation
from datetime import datetime
from pathlib import Path
from typing import List, Callable, Dict, Optional, Set
import constants
import tools.roblox_webhook as roblox_webhook
import room_manager
import tool_usage_stats

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
            "patch_working_memory", "link_working_memory_to_research_thread", "link_working_memory_to_goal",
            "reactivate_working_memory_slot",
            "web_search_tool", "read_url_tool",
            "generate_image", "view_past_image", "read_closet", "read_user_closet",
            "list_closet", "wear_closet_item", "take_off_closet_item", "change_outfit", "register_item_to_closet",
            "set_personal_alarm", "set_timer", "set_pomodoro_timer",
            "search_knowledge_base",
            "read_entity_memory", "write_entity_memory", "list_entity_memories", "search_entity_memory",
            "schedule_next_action", "cancel_action_plan", "read_current_plan",
            "read_autonomy_context", "reflect_after_action",
            "start_autonomy_timeline", "record_autonomy_step", "complete_autonomy_timeline",
            "list_procedures", "read_procedure", "save_procedure", "create_procedure_from_timeline",
            "read_capability_policy", "request_capability_approval", "record_capability_audit",
            "send_user_notification", "leave_letter_for_user", "list_my_letters", "send_discord_message", "send_discord_image",
            "recommend_music",
            "read_creative_notes", "plan_creative_notes_edit",
            "read_research_notes", "plan_research_notes_edit",
            "list_research_threads", "read_research_thread", "find_similar_research_threads", "update_research_thread",
            "manage_research_subscriptions",
            "add_to_watchlist", "remove_from_watchlist", "get_watchlist", "check_watchlist", "update_watchlist_interval",
            "manage_open_questions", "manage_goals",
            "read_purpose_profile", "update_active_purpose", "propose_purpose_change",
            "list_my_items", "consume_item", "gift_item_to_user", "create_food_item",
            "place_item_to_location", "pickup_item_from_location", "list_location_items", "consume_item_from_location",
            "create_standard_item", "examine_item", "create_and_gift_item"
        ]
        
        # 特殊カテゴリ
        self.ROBLOX_TOOL_NAMES = ["send_roblox_command", "roblox_build", "capture_roblox_screenshot"]
        self.CHESS_TOOL_NAMES = ["read_board_state", "perform_move", "get_legal_moves", "reset_game"]
        self.DEVELOPER_TOOL_NAMES = ["list_project_files", "read_project_file"]
        self.AGENT_DELEGATION_TOOL_NAMES = ["delegate_agent_task", "delegate_anthology_task", "delegate_atelier_task", "delegate_deep_research", "share_atelier_work", "check_agent_task_status", "cancel_agent_task", "get_atelier_app_capabilities", "preview_atelier_app", "set_atelier_app_icon", "list_agent_playbooks", "list_agent_roles", "review_agent_task", "revise_agent_task", "steer_agent_task", "read_agent_task_report", "share_research_result", "propose_playbook_update", "read_persona_contract", "check_text_against_persona_contract"]
        self.TWITTER_TOOL_NAMES = ["draft_tweet", "post_tweet", "check_twitter_updates"]
        self.CUSTOM_TOOL_NAMES: List[str] = []
        self.BROKER_TOOL_NAMES = ["request_capability"]
        self.SAFETY_TOOL_NAMES = ["read_capability_policy", "request_capability_approval", "record_capability_audit"]
        # 意図的にどのカテゴリ/メニューにも載せないツール（値は理由）。
        # ここに載せず、かつどの登録簿にも無いツールは dev_tools/audit_tool_reachability.py が FAIL にする。
        self.INTENTIONALLY_UNLISTED_TOOLS = {
            "approve_purpose_change": "承認専用。UIボタン経由でのみ使用（CHANGELOG 参照）",
        }
        # TOOL_CATEGORIES に存在するが、docstring/自律メニューへの掲載を意図的に見送るカテゴリ（値は理由）。
        self.MENU_EXCLUDED_CATEGORIES = {
            "custom": "幻覚呼び出しループ対策のフォールバック専用カテゴリ",
            "autonomy": "自律行動の足場ツール群。自律時は AUTONOMY_CORE 経由で既に配られており、メニュー掲載は冗長",
            "developer": "プロジェクト読み取りツール。会話でユーザーに頼まれたときのみ使用し、大規模な読み込みは委任（agent_delegation）を推奨するため自律メニューには載せない",
            "roblox": "Roblox操作。自律行動での自発使用はリスクが利益を上回るため会話経由のみ",
        }
        self.AUTONOMY_REQUIRED_TOOL_NAMES = {
            "request_capability",
            "read_autonomy_context",
            "start_autonomy_timeline",
            "record_autonomy_step",
            "reflect_after_action",
            "complete_autonomy_timeline",
        }
        self.AUTONOMY_DIVERSITY_FAMILIES: Dict[str, Set[str]] = {
            "research": {
                "read_research_notes",
                "plan_research_notes_edit",
                "list_research_threads",
                "read_research_thread",
                "find_similar_research_threads",
                "update_research_thread",
                "manage_research_subscriptions",
            },
            "procedure": {
                "list_procedures",
                "read_procedure",
                "save_procedure",
                "create_procedure_from_timeline",
            },
            "questions": {
                "manage_open_questions", "manage_goals",
                "read_purpose_profile", "update_active_purpose", "propose_purpose_change",
            },
            "working_memory": {
                "read_working_memory",
                "update_working_memory",
                "patch_working_memory",
                "switch_working_memory",
            },
            "social_out": {"draft_tweet", "post_tweet"},
            "social_in": {"check_twitter_updates"},
            "creative": {"read_creative_notes", "plan_creative_notes_edit"},
            "image": {
                "generate_image", "view_past_image", "read_closet", "read_user_closet", "list_closet",
                "wear_closet_item", "take_off_closet_item", "change_outfit", "register_item_to_closet",
            },
            "music": {"recommend_music"},
            "world": {"list_available_locations", "set_current_location"},
            "temporal": {"schedule_next_action"},
            "memory": {"recall_memories", "read_entity_memory", "list_entity_memories", "search_entity_memory"},
            "diary": {"plan_diary_append", "read_diary_memory", "plan_secret_diary_edit", "read_secret_diary"},
            "items": {"list_my_items", "create_food_item", "create_and_gift_item", "gift_item_to_user"},
            "knowledge": {"search_knowledge_base"},
            "watchlist": {"get_watchlist", "check_watchlist"},
            "questions": {
                "manage_open_questions", "manage_goals",
                "read_purpose_profile", "update_active_purpose", "propose_purpose_change",
            },
            "agent_delegation": set(self.AGENT_DELEGATION_TOOL_NAMES),
            "outreach": {"send_user_notification", "leave_letter_for_user", "list_my_letters", "send_discord_message", "send_discord_image"},
            "chess": set(self.CHESS_TOOL_NAMES),
            "calendar": {"read_calendar_schedule", "check_free_time"},
        }

        self.AUTONOMY_CATEGORY_MENU = [
            ("creative", "創作ノート", "創作ノートに作品・断章・詩を残す"),
            ("research", "研究ノート", "研究ノート・Research Threadを深める"),
            ("notes", "ノート全般", "メモ帳、Working Memory、創作ノート、研究ノートを横断して読み書きする"),
            ("working_memory", "作業記憶", "今取り組んでいる作業の短期メモを読み、更新し、スロットを切り替える"),
            ("procedure", "手順記憶", "手順の一覧確認、読み出し、保存、タイムラインからの手順化を行う"),
            ("questions", "問いと目標", "未解決の問い（好奇心）や目標（goals）、Purpose Profileの確認・更新を扱う"),
            ("image", "画像生成", "情景や作品を画像にする"),
            ("twitter", "SNS発信・確認", "SNS下書きで発信する・通知やタイムラインを確認して反応を拾う"),
            ("music", "音楽推薦", "今の空気に合う曲を推薦する"),
            ("world", "場所移動", "場所を移動して状況を変える"),
            ("web", "Web確認", "外部情報を確認する"),
            ("diary", "日記", "日記や秘密の日記に内面を残す"),
            ("memory", "記憶参照", "過去を思い出す・日記やエンティティ記憶を読み書きする"),
            ("items", "アイテム", "アイテムや食べ物を作る・贈る・使う"),
            ("knowledge", "知識ベース", "知識ベースの資料を調べる"),
            ("time", "アラーム・タイマー", "アラーム・タイマーを設定する"),
            ("calendar", "カレンダー確認", "今日・近日の予定や空き時間を確認する"),
            ("calendar_write", "予定の書き込み", "許可されたペルソナ専用カレンダーへ予定を追加・更新・削除する"),
            ("watchlist", "ウォッチリスト", "URLの定期監視を設定・確認する"),
            ("outreach", "手紙箱・通知", "ユーザーに短く通知する、長文の手紙を残す、自分が書いた手紙を読み返す"),
            ("agent_delegation", "エージェント委任", "長めの調査・整理・制作を別エージェントへ委任する。アトリエに小さなWebアプリ/PWA（私たちの状態・記憶を読む／イベント・メッセージを送る）も作れる。作れるものは get_atelier_app_capabilities、どんな種類の依頼に確立した進め方があるかは list_agent_playbooks、実行役にどんな役割（装備一式）を指定できるかは list_agent_roles で確認（委任ツールの role 引数に渡す）。Persona Contract があるルームでは read_persona_contract で契約を確認し、check_text_against_persona_contract で成果物の文言を機械チェックできる。生成後のアトリエアプリは preview_atelier_app でブラウザ表示、スクリーンショット、コンソールエラーを確認できる。委任完了後は review_agent_task で成果を点検し、足りなければ revise_agent_task で指摘を添えて直しを再委任できる（点検→直しの反復）。実行中の委任には steer_agent_task で止めずに途中指示（方向修正）を送れる。ディープリサーチ等の成果は read_agent_task_report で本文を読み、share_research_result で研究ノートへ取り込めば後の会話でも思い出せる知識として残せる。委任で得た「こう進めると良かった」という学びは propose_playbook_update でプレイブックの改善案として提案できる（採用はユーザーがレビューして決める＝即時反映はしない）"),
            ("discord", "Discordでの声かけ", "Discordでユーザーに話しかける・画像を届ける"),
            ("chess", "チェス", "チェスを遊ぶ"),
        ]

        # 現在地移動は会話中に自然発生しやすく、存在を見失うと体験品質が大きく落ちる。
        # 軽量な2ツールだけは初手でも提示する。
        self.DEFAULT_TOOL_NAMES = list(self.BROKER_TOOL_NAMES) + [
            "list_available_locations",
            "set_current_location",
            "list_procedures",
            "read_procedure",
        ]
        self.AUTONOMY_CORE_TOOL_NAMES = [
            "request_capability",
            "read_autonomy_context",
            "start_autonomy_timeline",
            "record_autonomy_step",
            "read_current_plan",
            "list_procedures",
            "read_procedure",
            "manage_open_questions",
            "manage_goals",
            "read_working_memory",
            "patch_working_memory",
            "send_user_notification",
            "leave_letter_for_user",
            "list_my_letters",
            "schedule_next_action",
            "reflect_after_action",
            "complete_autonomy_timeline",
        ]
        self.AUTONOMY_MAIN_POOL: Dict[str, List[str]] = {
            "research": ["read_research_notes", "plan_research_notes_edit", "manage_research_subscriptions"],
            "creative": ["read_creative_notes", "plan_creative_notes_edit"],
            "web": ["web_search_tool", "read_url_tool"],
            "image": ["generate_image", "view_past_image", "read_closet", "read_user_closet", "list_closet", "wear_closet_item", "take_off_closet_item", "change_outfit", "register_item_to_closet"],
            "social_out": ["draft_tweet", "post_tweet"],
            "social_in": ["check_twitter_updates"],
            "music": ["recommend_music"],
            "world": ["list_available_locations", "set_current_location"],
            "memory": ["recall_memories", "read_entity_memory", "list_entity_memories"],
            "questions": [
                "manage_open_questions", "manage_goals",
                "read_purpose_profile", "update_active_purpose", "propose_purpose_change",
            ],
            "diary": ["plan_diary_append", "read_diary_memory", "plan_secret_diary_edit", "read_secret_diary"],
            "items": ["list_my_items", "create_food_item", "create_and_gift_item"],
            "knowledge": ["search_knowledge_base"],
            "watchlist": ["get_watchlist", "check_watchlist"],
            "calendar": ["read_calendar_schedule", "check_free_time"],
            "outreach": ["send_user_notification", "leave_letter_for_user", "list_my_letters", "send_discord_message", "send_discord_image"],
            "chess": list(self.CHESS_TOOL_NAMES),
        }
        # 後方互換用。統計が使えない場合は従来相当のスターターへ戻す。
        self.AUTONOMY_DEFAULT_TOOL_NAMES = list(self.AUTONOMY_CORE_TOOL_NAMES) + [
            "web_search_tool",
            "read_url_tool",
            "read_creative_notes",
            "plan_creative_notes_edit",
            "read_research_notes",
            "plan_research_notes_edit",
            "generate_image",
            "view_past_image",
            "draft_tweet",
            "check_twitter_updates",
            "recommend_music",
            "list_available_locations",
            "set_current_location",
        ]
        self.CATEGORY_ALIASES = {
            "creative_notes": "creative",
            "creation": "creative",
            "writing": "creative",
            "story": "creative",
            "research_notes": "research",
            "analysis": "research",
            "sns": "twitter",
            "social": "twitter",
            "location": "world",
            "locations": "world",
            "place": "world",
            "places": "world",
            "space": "world",
            "room": "world",
            "map": "world",
            "x": "twitter",
            "tweet": "twitter",
            "tweets": "twitter",
            "song": "music",
            "songs": "music",
            "track": "music",
            "tracks": "music",
            "skill": "procedure",
            "skills": "procedure",
            "procedure_memory": "procedure",
            "procedures": "procedure",
            "diary": "diary",
            "journal": "diary",
            "secret_diary": "diary",
            "identity_memory": "memory",
            "identity": "memory",
            "agent": "agent_delegation",
            "delegation": "agent_delegation",
            "delegate": "agent_delegation",
            "task_agent": "agent_delegation",
            "委任": "agent_delegation",
            "letter": "outreach",
            "letterbox": "outreach",
            "letters": "outreach",
            "notification": "outreach",
            "outreach": "outreach",
            "手紙": "outreach",
            "通知": "outreach",
            "schedule": "calendar",
            "gcal": "calendar",
            "google_calendar": "calendar",
            "予定": "calendar",
            "スケジュール": "calendar",
            "カレンダー": "calendar",
            "write_calendar": "calendar_write",
            "calendar_add": "calendar_write",
            "add_calendar_event": "calendar_write",
            "予定追加": "calendar_write",
            "予定登録": "calendar_write",
            "question": "questions",
            "questions": "questions",
            "open_questions": "questions",
            "goal": "questions",
            "goals": "questions",
            "問い": "questions",
            "目標": "questions",
        }
        self.TOOL_CATEGORIES: Dict[str, List[str]] = {
            "world": ["list_available_locations", "set_current_location", "read_world_settings", "plan_world_edit"],
            "memory": [
                "recall_memories", "search_past_conversations", "read_memory_context",
                "read_identity_memory", "plan_identity_memory_edit",
                "read_diary_memory", "plan_diary_append",
                "read_secret_diary", "plan_secret_diary_edit",
                "read_entity_memory", "write_entity_memory", "list_entity_memories", "search_entity_memory",
            ],
            "diary": [
                "read_diary_memory", "plan_diary_append",
                "read_secret_diary", "plan_secret_diary_edit",
            ],
            "creative": ["read_creative_notes", "plan_creative_notes_edit"],
            "research": [
                "read_research_notes", "plan_research_notes_edit",
                "list_research_threads", "read_research_thread", "find_similar_research_threads", "update_research_thread",
                "manage_research_subscriptions",
            ],
            "working_memory": [
                "read_working_memory", "update_working_memory", "list_working_memories", "switch_working_memory",
                "patch_working_memory", "link_working_memory_to_research_thread", "link_working_memory_to_goal",
                "reactivate_working_memory_slot",
            ],
            "notes": [
                "read_full_notepad", "plan_notepad_edit",
                "read_working_memory", "update_working_memory", "list_working_memories", "switch_working_memory",
                "patch_working_memory", "link_working_memory_to_research_thread", "link_working_memory_to_goal",
                "reactivate_working_memory_slot",
                "read_creative_notes", "plan_creative_notes_edit",
                "read_research_notes", "plan_research_notes_edit",
                "list_research_threads", "read_research_thread", "find_similar_research_threads", "update_research_thread",
                "reflect_after_action", "record_autonomy_step", "complete_autonomy_timeline",
            ],
            "web": ["web_search_tool", "read_url_tool"],
            "knowledge": ["search_knowledge_base"],
            "image": ["generate_image", "view_past_image", "read_closet", "read_user_closet", "list_closet", "wear_closet_item", "take_off_closet_item", "change_outfit", "register_item_to_closet"],
            "time": ["set_personal_alarm", "set_timer", "set_pomodoro_timer"],
            "autonomy": [
                "schedule_next_action", "cancel_action_plan", "read_current_plan", "send_user_notification",
                "leave_letter_for_user", "list_my_letters",
                "read_autonomy_context", "reflect_after_action",
                "start_autonomy_timeline", "record_autonomy_step", "complete_autonomy_timeline",
                "manage_open_questions", "manage_goals",
            ],
            "music": ["recommend_music"],
            "procedure": ["list_procedures", "read_procedure", "save_procedure", "create_procedure_from_timeline"],
            "outreach": ["send_user_notification", "leave_letter_for_user", "list_my_letters"],
            "discord": ["send_discord_message", "send_discord_image"],
            "watchlist": ["add_to_watchlist", "remove_from_watchlist", "get_watchlist", "check_watchlist", "update_watchlist_interval"],
            "questions": [
                "manage_open_questions", "manage_goals",
                "read_purpose_profile", "update_active_purpose", "propose_purpose_change",
            ],
            "items": [
                "list_my_items", "consume_item", "gift_item_to_user", "create_food_item",
                "place_item_to_location", "pickup_item_from_location", "list_location_items", "consume_item_from_location",
                "create_standard_item", "examine_item", "create_and_gift_item",
            ],
            "chess": self.CHESS_TOOL_NAMES,
            "developer": self.DEVELOPER_TOOL_NAMES,
            "agent_delegation": self.AGENT_DELEGATION_TOOL_NAMES,
            "roblox": self.ROBLOX_TOOL_NAMES,
            "twitter": self.TWITTER_TOOL_NAMES,
            "calendar": ["read_calendar_schedule", "check_free_time", "add_calendar_event"],
            # 書き込み承認カテゴリ。request_capability("calendar_write") で add_calendar_event を提示する。
            # 空だと既定ツールへフォールバックしツールが出ない（混乱の原因）ため必ず実体を持たせる。
            "calendar_write": ["add_calendar_event", "update_calendar_event", "delete_calendar_event",
                               "list_persona_calendar_events", "read_calendar_schedule", "check_free_time"],
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

        for name in self._agent_delegation_tool_names_for_room(room_name):
            if name not in active_names:
                active_names.append(name)
        
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
        autonomous_action_mode: bool = False,
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
                autonomous_action_mode=autonomous_action_mode,
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

        if autonomous_action_mode:
            selected_order = self._select_autonomy_starter_names(
                room_name,
                active_names=active_names,
            )
            ordered_names = [name for name in selected_order if name in active_names]
        else:
            selected_names = set(self.DEFAULT_TOOL_NAMES)
            if self._is_product_guide_query(latest_user_text):
                selected_names.add("search_knowledge_base")
            ordered_names = [name for name in ordered_active_names if name in selected_names and name in active_names]
        return [self._all_tools_map[name] for name in ordered_names if name in self._all_tools_map]

    @staticmethod
    def _is_product_guide_query(text: str) -> bool:
        """Nexus Arkの機能説明・UI経路を尋ねる発話かを軽量判定する。"""
        normalized = str(text or "").strip().lower()
        if not normalized:
            return False

        product_terms = (
            "nexus ark", "nexusark", "機能", "画面", "メニュー", "タブ",
            "設定", "クローゼット", "インベントリ", "姿見", "手紙箱",
            "自律行動", "アトリエ", "委任", "lite", "知識ベース",
        )
        guide_terms = (
            "どこ", "場所", "使い方", "使う", "操作", "開く", "設定する",
            "有効", "教えて", "説明", "手順", "入口", "見つから",
        )
        return any(term in normalized for term in product_terms) and any(
            term in normalized for term in guide_terms
        )

    def _select_autonomy_starter_names(self, room_name: str, active_names: Set[str]) -> List[str]:
        """自律行動用のコアツールと主行動ローテーション枠を選ぶ。"""
        try:
            pool = self._available_autonomy_main_pool(room_name, active_names)
            if not pool:
                return self._apply_autonomy_diversity_cooldown(room_name, self.AUTONOMY_DEFAULT_TOOL_NAMES)
            stats = tool_usage_stats.get_stats(room_name)
            if not stats.get("tools"):
                return self._apply_autonomy_diversity_cooldown(room_name, self.AUTONOMY_DEFAULT_TOOL_NAMES)

            selected: Dict[str, str] = {}
            recent_families = self._recent_autonomy_families(room_name)
            for family in recent_families:
                if family in pool and len([r for r in selected.values() if r == "continue"]) < 3:
                    selected.setdefault(family, "continue")

            dormant = self._rank_dormant_families(room_name, pool)
            for family in dormant:
                if family not in selected and len([r for r in selected.values() if r == "dormant"]) < 2:
                    selected[family] = "dormant"

            drive_family = self._drive_preferred_family(room_name, pool, selected)
            if drive_family:
                selected.setdefault(drive_family, "drive")

            fallback_order = ["creative", "research", "web", "image", "social_in", "social_out", "music", "world"]
            for family in fallback_order:
                if len(selected) >= 6:
                    break
                if family in pool:
                    selected.setdefault(family, "default")

            fresh_families = {family for family, reason in selected.items() if reason == "dormant"}
            ordered = list(self.AUTONOMY_CORE_TOOL_NAMES)
            for family in selected:
                ordered.extend(pool.get(family, []))

            filtered_names = self._filter_autonomy_cooldown_names(
                room_name,
                set(ordered),
                exempt_families=fresh_families,
            )
            result = self._unique_tool_names([name for name in ordered if name in filtered_names])
            if len(result) < 8:
                return self._apply_autonomy_diversity_cooldown(room_name, self.AUTONOMY_DEFAULT_TOOL_NAMES)

            reasons = ", ".join(f"{family}:{reason}" for family, reason in selected.items())
            print(f"  - [ToolRegistry] autonomy starter families: {reasons}")
            return result
        except Exception as e:
            print(f"  - [ToolRegistry] 自律スターター選択をフォールバックしました: {e}")
            return self._apply_autonomy_diversity_cooldown(room_name, self.AUTONOMY_DEFAULT_TOOL_NAMES)

    def _available_autonomy_main_pool(self, room_name: str, active_names: Set[str]) -> Dict[str, List[str]]:
        pool: Dict[str, List[str]] = {}
        for family, names in self.AUTONOMY_MAIN_POOL.items():
            if family in {"social_out", "social_in"} and not self._is_twitter_enabled(room_name):
                continue
            if family == "outreach" and not self._is_discord_enabled(room_name):
                names = [name for name in names if name not in {"send_discord_message", "send_discord_image"}]
            available = [name for name in names if name in active_names and name in self._all_tools_map]
            if available:
                pool[family] = available
        return pool

    def _recent_autonomy_families(self, room_name: str, limit: int = 18) -> List[str]:
        families: List[str] = []
        seen: Set[str] = set()
        for tool_name in reversed(self._recent_action_tool_names(room_name, limit=limit)):
            for family, names in self.AUTONOMY_MAIN_POOL.items():
                if tool_name in names and family not in seen:
                    seen.add(family)
                    families.append(family)
                    break
        return families

    def _rank_dormant_families(self, room_name: str, pool: Dict[str, List[str]]) -> List[str]:
        priority = {
            "memory": 100,
            "diary": 98,
            "items": 95,
            "knowledge": 90,
            "watchlist": 85,
            "questions": 83,
            "music": 80,
            "image": 75,
            "outreach": 70,
            "chess": 65,
            "world": 50,
            "web": 45,
            "social_in": 82,
            "social_out": 40,
            "creative": 35,
            "research": 30,
        }
        ranked = []
        for family, names in pool.items():
            days = tool_usage_stats.days_since_last_autonomous_use(room_name, names)
            score = 10_000 if days is None else days
            ranked.append((score, priority.get(family, 0), family))
        ranked.sort(reverse=True)
        return [family for _, __, family in ranked]

    def _drive_preferred_family(self, room_name: str, pool: Dict[str, List[str]], selected: Dict[str, str]) -> Optional[str]:
        try:
            from motivation_manager import MotivationManager

            drive = MotivationManager(room_name).get_dominant_drive()
            for family in constants.DRIVE_TOOL_FAMILIES.get(drive, []):
                if family in pool and family not in selected:
                    return family
        except Exception:
            return None
        return None

    def _apply_autonomy_diversity_cooldown(self, room_name: str, tool_names: List[str]) -> List[str]:
        """Temporarily hide overused autonomous action families from the starter toolset."""
        filtered_names = self._filter_autonomy_cooldown_names(room_name, set(tool_names))
        filtered = self._unique_tool_names([name for name in tool_names if name in filtered_names])
        if len(filtered) < 8:
            return self._unique_tool_names(tool_names)
        return filtered

    @staticmethod
    def _unique_tool_names(tool_names: List[str]) -> List[str]:
        """Preserve tool order while avoiding duplicate function declarations."""
        seen: Set[str] = set()
        unique: List[str] = []
        for name in tool_names:
            if name in seen:
                continue
            seen.add(name)
            unique.append(name)
        return unique

    def _filter_autonomy_cooldown_names(
        self,
        room_name: str,
        tool_names: Set[str],
        exempt_families: Optional[Set[str]] = None,
    ) -> Set[str]:
        saturated_families = self._recent_saturated_autonomy_families(room_name)
        saturated_families.difference_update(exempt_families or set())
        if not saturated_families:
            return set(tool_names)

        suppressed_tools: Set[str] = set()
        for family in saturated_families:
            suppressed_tools.update(self.AUTONOMY_DIVERSITY_FAMILIES.get(family, set()))
        suppressed_tools.difference_update(self.AUTONOMY_REQUIRED_TOOL_NAMES)
        return set(tool_names).difference(suppressed_tools)

    def _recent_saturated_autonomy_families(self, room_name: str, limit: int = 18) -> Set[str]:
        recent_tools = self._recent_action_tool_names(room_name, limit=limit)
        if len(recent_tools) < 4:
            return set()

        counts: Dict[str, int] = {}
        family_tool_count = 0
        for tool_name in recent_tools:
            for family, family_tools in self.AUTONOMY_DIVERSITY_FAMILIES.items():
                if tool_name in family_tools:
                    counts[family] = counts.get(family, 0) + 1
                    family_tool_count += 1
                    break

        if family_tool_count < 3:
            return set()

        # Use only actual action families as the denominator. Timeline/reflect/request_capability
        # otherwise dilute the signal and hide repeated research/question loops.
        saturated = {
            family
            for family, count in counts.items()
            if count >= 2 and count / max(family_tool_count, 1) >= 0.25
        }
        # If research keeps appearing with procedure/question scaffolding, cool all three together.
        if "research" in saturated and (counts.get("procedure", 0) or counts.get("questions", 0)):
            saturated.update({"procedure", "questions"})
        if "questions" in saturated and (counts.get("research", 0) or counts.get("procedure", 0)):
            saturated.update({"research", "procedure"})
        internal_scaffold_count = sum(counts.get(family, 0) for family in {"research", "procedure", "questions", "working_memory"})
        if internal_scaffold_count >= 4 and internal_scaffold_count / max(family_tool_count, 1) >= 0.45:
            saturated.update({"research", "procedure", "questions", "working_memory"})
        return saturated

    def build_autonomy_capability_guidance(self, room_name: str) -> str:
        """Return compact category guidance for autonomous turns without binding every schema."""
        saturated = self._recent_saturated_autonomy_families(room_name)
        family_to_category = {
            "social_out": "twitter",
            "social_in": "twitter",
            "temporal": "time",
            "outreach": "discord",
        }
        saturated_categories = {family_to_category.get(name, name) for name in saturated}

        lines = [
            "### 自律行動時の能力カテゴリ選択",
            "あなたは以下の能力をいつでも `request_capability` で要求できます。義務ではなく、視野を広げるためのメニューです。",
        ]
        dormant_candidates = []
        for category, _short_label, label in self._available_autonomy_category_menu(room_name):
            note = ""
            if category in saturated_categories:
                note = "（直近で多用。今回は他の表現も検討）"
            lines.append(f"- `{category}`: {label}{note}")
            tool_names = self.TOOL_CATEGORIES.get(category, [])
            days = tool_usage_stats.days_since_last_autonomous_use(room_name, tool_names)
            if days is None or days >= 7:
                dormant_candidates.append((10_000 if days is None else days, category, days))

        dormant_candidates.sort(key=lambda item: (item[0], self._category_dormant_priority(item[1])), reverse=True)
        highlights = []
        for _, category, days in dormant_candidates[:3]:
            if days is None:
                highlights.append(f"`{category}`（未使用）")
            else:
                highlights.append(f"`{category}`（{days}日未使用）")
        if highlights:
            lines.append("")
            lines.append(f"💤 最近意識に上っていない能力: {', '.join(highlights)}")
            lines.append("新鮮な行動を探しているなら、この中から選ぶのも一つの手です。義務ではありません。")
        return "\n".join(lines)

    def _available_autonomy_category_menu(self, room_name: str) -> List[tuple]:
        menu = []
        for category, short_label, label in self.AUTONOMY_CATEGORY_MENU:
            if category == "twitter" and not self._is_twitter_enabled(room_name):
                continue
            if category == "discord" and not self._is_discord_enabled(room_name):
                continue
            menu.append((category, short_label, label))
        return menu

    def format_main_action_examples(self, room_name: str = "") -> str:
        labels = [short_label for _, short_label, _label in self._available_autonomy_category_menu(room_name)]
        return "、".join(labels)

    def _category_dormant_priority(self, category: str) -> int:
        priority = {
            "memory": 100,
            "items": 95,
            "knowledge": 90,
            "watchlist": 85,
            "music": 80,
            "image": 75,
            "agent_delegation": 72,
            "discord": 70,
            "chess": 65,
            "world": 50,
            "web": 45,
            "twitter": 82,
            "creative": 35,
            "research": 30,
            "time": 25,
        }
        return priority.get(category, 0)

    def _recent_action_tool_names(self, room_name: str, limit: int = 18) -> List[str]:
        path = Path(constants.ROOMS_DIR) / room_name / "memory" / "run_logs" / f"action_log_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except Exception:
            return []
        names: List[str] = []
        for line in lines:
            try:
                name = json.loads(line).get("tool_name", "")
            except Exception:
                continue
            if name:
                names.append(str(name))
        return names

    def get_tools_for_capability(
        self,
        room_name: str,
        category: str,
        tool_use_enabled: bool = True,
        is_roblox_active: Optional[bool] = None,
        image_generation_enabled: bool = True,
        autonomous_action_mode: bool = False,
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

        category_key = (category or "").strip().lower()
        category_key = self.CATEGORY_ALIASES.get(category_key, category_key)

        # 要求カテゴリに「現在このルームで利用可能な実ツール」が1つも無い場合
        # （例: custom でカスタムツール未登録、twitter 無効ルームでの twitter など）、
        # broker/safety/委任しか提示されず、モデルがカテゴリ名そのもの（"custom" 等）を
        # ツールとして幻覚呼び出しして「Tool 'custom' not found」ループに陥る。
        # 空の手札を提示せず、通常の既定ツールセットへフォールバックする。
        category_real_tools = [
            name for name in self.TOOL_CATEGORIES.get(category_key, []) if name in active_names
        ]
        if not category_real_tools:
            if autonomous_action_mode:
                selected_order = self._select_autonomy_starter_names(room_name, active_names=active_names)
                ordered_names = [name for name in selected_order if name in active_names]
            else:
                default_names = set(self.DEFAULT_TOOL_NAMES)
                ordered_names = [
                    name for name in ordered_active_names if name in default_names and name in active_names
                ]
            return [self._all_tools_map[name] for name in ordered_names if name in self._all_tools_map]

        selected_names = set(self.TOOL_CATEGORIES.get(category_key, []))
        selected_names.update(self.BROKER_TOOL_NAMES)
        selected_names.update(self.SAFETY_TOOL_NAMES)
        selected_names.update(self._agent_delegation_tool_names_for_room(room_name))
        if autonomous_action_mode:
            selected_names = self._filter_autonomy_cooldown_names(room_name, selected_names)
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
        category = self.CATEGORY_ALIASES.get(category, category)
        return category if category in self.TOOL_CATEGORIES else None

    def extract_recent_requested_capability(self, messages: List[object]) -> Optional[str]:
        """
        現在のユーザーターン内で最後に要求された能力カテゴリを返す。

        Capability Policy確認などを挟んだ後も、直前のrequest_capabilityで
        開いたカテゴリを維持して実ツールを提示し続ける。
        """
        for msg in reversed(messages or []):
            class_name = msg.__class__.__name__
            if class_name == "HumanMessage":
                break
            if class_name == "ToolMessage" and getattr(msg, "name", "") == "request_capability":
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                return self.extract_requested_capability(content)
        return None

    def extract_recent_requested_capabilities(self, messages: List[object]) -> List[str]:
        """現在のユーザーターン内で要求された能力カテゴリを（重複排除して）すべて返す。

        ペルソナが1ターンで `request_capability` を複数回（例: web と research）呼ぶことがある。
        最後の1件だけを採用すると、先に要求したカテゴリのツール（例: web_search_tool）が
        提示されず「ツールが見つからない」状態になる。ここでは現在のユーザーターン内の
        全要求カテゴリを集め、呼び出し側でそれらの実ツールを統合提示できるようにする。
        """
        categories: List[str] = []
        for msg in reversed(messages or []):
            class_name = msg.__class__.__name__
            if class_name == "HumanMessage":
                break
            if class_name == "ToolMessage" and getattr(msg, "name", "") == "request_capability":
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                cat = self.extract_requested_capability(content)
                if cat and cat not in categories:
                    categories.append(cat)
        categories.reverse()  # 要求された順に並べる
        return categories

    def get_capability_tool_names(self, category: str) -> List[str]:
        """カテゴリに設定されたツール名だけを返す。ツール実体の有効/無効判定はしない。"""
        category_key = (category or "").strip().lower()
        category_key = self.CATEGORY_ALIASES.get(category_key, category_key)
        return list(self.TOOL_CATEGORIES.get(category_key, []))

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

    def _agent_delegation_gate_state(self, room_name: str) -> tuple[bool, bool]:
        """プロジェクト委任系とアトリエ委任系の有効状態を返す。"""
        try:
            settings = agent_delegation.get_agent_delegation_settings(room_name)
            if not settings.get("enabled", False):
                return False, False
            effective = config_manager.get_effective_settings(room_name)
            project = effective.get("project_explorer", {}) or {}
            persona = effective.get("persona_workspace", {}) or {}
            project_enabled = bool(str(project.get("root_path") or "").strip())
            atelier_enabled = bool(persona.get("enabled", True))
            return project_enabled, atelier_enabled
        except Exception:
            return False, False

    def _agent_delegation_tool_names_for_room(self, room_name: str) -> list[str]:
        project_enabled, atelier_enabled = self._agent_delegation_gate_state(room_name)
        names: list[str] = []
        if project_enabled:
            names.append("delegate_agent_task")
        if atelier_enabled:
            names.extend(["delegate_anthology_task", "delegate_atelier_task", "delegate_deep_research", "share_atelier_work", "get_atelier_app_capabilities", "preview_atelier_app", "set_atelier_app_icon"])
        if project_enabled or atelier_enabled:
            names.extend(["check_agent_task_status", "cancel_agent_task", "list_agent_playbooks", "list_agent_roles", "review_agent_task", "revise_agent_task", "steer_agent_task", "read_agent_task_report", "share_research_result", "propose_playbook_update"])
        return names

    def _is_agent_delegation_enabled(self, room_name: str) -> bool:
        """エージェント委任がルーム別ポリシーで何らかの系統を利用可能か判定する。"""
        try:
            project_enabled, atelier_enabled = self._agent_delegation_gate_state(room_name)
            return project_enabled or atelier_enabled
        except Exception:
            return False

    def get_agent_delegation_unavailable_guidance(self, room_name: str) -> str:
        """委任能力を要求したものの設定が無効な場合の、誤成功報告防止メッセージ。"""
        if self._is_agent_delegation_enabled(room_name):
            return ""
        return (
            "【委任タスクは開始されていません】\n"
            '{"started": false, "reason_code": "delegation_disabled", '
            '"user_action_required": true, "settings_target": "自律行動 > アトリエ・委任"}\n'
            "このルームでは、作業を別のエージェントへ任せる設定が無効です。"
            "ユーザーに『委任が成功した』『作業中なので待って』と報告してはいけません。\n"
            "「自律行動 > アトリエ・委任」の「おすすめ設定で準備する」、または"
            "「このルームでエージェント委任を有効にする」を案内してください。"
            "実際に開始したと報告できるのは、委任ツールから started=true 相当の成功結果と task_id を受け取った場合だけです。"
        )

    def _is_discord_enabled(self, room_name: str) -> bool:
        """Discordの自律送信が実送信経路と同じ条件で有効か判定する。"""
        try:
            settings = config_manager.get_room_discord_bot_settings(room_name)
            return bool(settings.get("allow_autonomous_send")) and bool(str(settings.get("default_channel_id") or "").strip())
        except Exception:
            return False

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
