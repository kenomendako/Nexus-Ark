import gradio as gr
from typing import Dict, Any
import room_manager
import utils

def get_world_data(room_name: str) -> Dict[str, Any]:
    """
    ルームの world_settings.txt を解析して辞書として取得する。
    """
    if not room_name:
        return {}
    world_settings_path = room_manager.get_world_settings_path(room_name)
    return utils.parse_world_file(world_settings_path)

def save_world_data(room_name: str, world_data: Dict[str, Any]):
    """
    ワールド・ビルダーUIから受け取った辞書データを、
    ## エリア名\n\n### 場所名\n内容... 形式のテキストに変換して
    world_settings.txt に書き込む。
    """
    if not room_name:
        gr.Warning("ルームが選択されていません。")
        return

    world_settings_path = room_manager.get_world_settings_path(room_name)
    if not world_settings_path:
        gr.Error("世界設定ファイルへのパスが取得できませんでした。")
        return

    # 辞書データをテキスト形式に変換
    content_parts = []
    for area_name, places in world_data.items():
        content_parts.append(f"## {area_name}\n")
        # 場所の定義をソートして、常に同じ順序で出力する
        sorted_places = sorted(places.items())
        for place_name, description in sorted_places:
            # __area_description__のような特殊キーは無視
            if place_name.startswith("__"): continue
            content_parts.append(f"### {place_name}\n{description.strip()}\n")

    final_content = "\n".join(content_parts)

    try:
        with open(world_settings_path, "w", encoding="utf-8") as f:
            f.write(final_content)
        # 通知は呼び出し元の UI ハンドラ側で一元管理するため、ここでは発行しない
    except Exception as e:
        gr.Error(f"世界設定の保存中にエラーが発生しました: {e}")

