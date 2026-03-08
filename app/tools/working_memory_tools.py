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
def switch_working_memory(slot_name: str, room_name: str) -> str:
    """
    アクティブなワーキングメモリのスロット（話題）を切り替える。
    存在しないスロット名を指定した場合は、新しくその話題のスロットが作成される。
    slot_nameには拡張子('.md')やパスを含めず、話題の名前（例: 'kobe_trip', 'nexus_ark_dev'）だけを含めること。
    
    【使用ガイドライン】
    話題が別のプロジェクトやテーマに移行した際は、元のスロットを汚染しないよう積極的にこのツールを使って新しいスロットを作成・切り替えること。
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
def update_working_memory(content: str, room_name: str, slot_name: str = None) -> str:
    """
    ワーキングメモリの内容を完全に上書き更新する。
    現在のプラン、状態、または直近の重要なコンテキストをシステムに保持させたい場合に使用する。
    既存の内容はすべて書き換えられるため、保持したい情報は含めた上で更新すること。
    slot_nameを指定しない場合は、現在アクティブなスロットが更新される。
    
    【記録に関する厳格なガイドライン】
    - ワーキングメモリは「現在進行中の具体的なタスクや話題」を維持するための短期的な作業机です。
    - 普遍的な「目標（Goal）」や「ユーザーへの感情/指針（Diary/Internal State）」は既存の記憶システムが担うため、**ここには記述しないでください**。
    - 「今日」「明日」という相対表現は時間の混乱を招くため避け、必ず「2/25」のような絶対日付で記録すること。
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
