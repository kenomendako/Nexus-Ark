from langchain_core.tools import tool
import os
import constants
import room_manager
import traceback

def _get_wm_dir(room_name: str) -> str:
    return os.path.join(constants.ROOMS_DIR, room_name, constants.NOTES_DIR_NAME, constants.WORKING_MEMORY_DIR_NAME)

def _get_wm_path(room_name: str, slot_name: str) -> str:
    if not slot_name.endswith(constants.WORKING_MEMORY_EXTENSION):
        slot_name += constants.WORKING_MEMORY_EXTENSION
    return os.path.join(_get_wm_dir(room_name), slot_name)

@tool
def list_working_memories(room_name: str) -> str:
    """
    現在利用可能なワーキングメモリのスロット（話題ごと）の一覧と、現在アクティブなスロット名を取得する。
    """
    try:
        wm_dir = _get_wm_dir(room_name)
        if not os.path.exists(wm_dir):
            return "【利用可能なワーキングメモリスロットはありません】"
        
        slots = [f.replace(constants.WORKING_MEMORY_EXTENSION, '') for f in os.listdir(wm_dir) if f.endswith(constants.WORKING_MEMORY_EXTENSION)]
        active_slot = room_manager.get_active_working_memory_slot(room_name)
        
        if not slots:
            return "【利用可能なワーキングメモリスロットはありません】"
            
        result = f"現在アクティブなスロット: {active_slot}\n"
        result += "利用可能なスロット一覧:\n- " + "\n- ".join(slots)
        return result
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリ一覧の取得中にエラーが発生しました: {e}"

@tool
def switch_working_memory(slot_name: str, room_name: str, intent: str = "新規タスクまたは話題の分離のため") -> str:
    """
    アクティブなワーキングメモリのスロット（話題）を切り替える。
    存在しないスロット名を指定した場合は、新しくその話題のスロットが作成される。
    
    slot_name: スロット名（例: 'kobe_trip', 'nexus_ark_dev'）。
    intent: なぜスロットを切り替えるのか、または新しく作成するのかという意図・背景（必須）。
    """
    try:
        # パストラバーサル防止
        if ".." in slot_name or "/" in slot_name or "\\" in slot_name:
            return "【エラー】不正なスロット名です。"
            
        success = room_manager.set_active_working_memory_slot(room_name, slot_name)
        if success:
            return f"成功: ワーキングメモリのスロットを '{slot_name}' に切り替えました。以後、read_working_memory や update_working_memory はこの新しいスロットに対して実行されます。"
        else:
            return "【エラー】スロットの切り替えに失敗しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの切り替え中にエラーが発生しました: {e}"

@tool
def read_working_memory(room_name: str, slot_name: str = None) -> str:
    """
    現在のプランや動的コンテキストを保持するワーキングメモリの内容を読み込む。
    slot_nameを指定しない場合は、現在アクティブなスロットが読み込まれる。
    """
    try:
        target_slot = slot_name if slot_name else room_manager.get_active_working_memory_slot(room_name)
        path = _get_wm_path(room_name, target_slot)
        
        if not os.path.exists(path):
            return f"【ワーキングメモリ '{target_slot}' はまだ作成されていません】"
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            return content if content else f"【ワーキングメモリ '{target_slot}' は空です】"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの読み込み中にエラーが発生しました: {e}"

@tool
def update_working_memory(content: str, room_name: str, context_type: str = "CONTINUE", intent: str = "情報の更新", slot_name: str = None) -> str:
    """
    ワーキングメモリの内容を完全に上書き更新する。
    このツールを使用する際は、必ず過去の文脈との繋がりと意図を明示しなければなりません。
    
    context_type: 過去の記録との関係性（'CONTINUE': 続き, 'DEEPEN': 深掘り, 'NEW': 新規）
    intent: なぜ更新するのか、過去の記憶や現在の状況のどの部分に基づいているのかの説明。
    content: 更新後の全内容。
    slot_name: 更新対象のスロット名（省略時は現在のアクティブスロット）。
    """
    try:
        target_slot = slot_name if slot_name else room_manager.get_active_working_memory_slot(room_name)
        # パストラバーサル防止
        if ".." in target_slot or "/" in target_slot or "\\" in target_slot:
            return "【エラー】不正なスロット名です。"
            
        path = _get_wm_path(room_name, target_slot)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        
        # バックアップ作成 (旧ファイルのバックアップロジックはworking_memoryというキーだったが、今回からスロット名を加味してもよいが汎用的なworking_memoryディレクトリとして扱う)
        # ただし現在の room_manager の create_backup は定数 WORKING_MEMORY_FILENAME に依存しているため、
        # ここでは拡張バックアップ（あるいは手動コピー）を行うか、ひとまずファイル直書きする
        # （のちほど room_manager 側のバックアップもマルチスロット対応にする方が望ましい）
        # 簡単のため一時的に独自のバックアップをローカルに作成するか、元の挙動を踏襲します。
        
        backup_dir = os.path.join(constants.ROOMS_DIR, room_name, "backups", "working_memories")
        os.makedirs(backup_dir, exist_ok=True)
        if os.path.exists(path):
            import datetime, shutil
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_filename = f"{timestamp}_{target_slot}{constants.WORKING_MEMORY_EXTENSION}.bak"
            shutil.copy2(path, os.path.join(backup_dir, backup_filename))
        
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"成功: ワーキングメモリのスロット '{target_slot}' を更新しました。"
    except Exception as e:
        traceback.print_exc()
        return f"【エラー】ワーキングメモリの更新中にエラーが発生しました: {e}"
