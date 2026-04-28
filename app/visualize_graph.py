# visualize_graph.py (v5: Font Path Direct Injection - The Final Gambit)

import os
import sys
import argparse
import logging
from pathlib import Path
import time
import traceback

import networkx as nx
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ▼▼▼【ここからが最終修正の核心】▼▼▼
# Windows環境に標準で搭載されている可能性が高いフォントのパスをリストアップ
# 上から順に見つかったものが使われる
FONT_PATHS_TO_TRY = [
    "C:/Windows/Fonts/YuGothM.ttc", # 遊ゴシック Medium
    "C:/Windows/Fonts/Meiryo.ttc",   # メイリオ
    "C:/Windows/Fonts/msgothic.ttc", # MS ゴシック
    "C:/Windows/Fonts/yumin.ttf",    # 游明朝
]

def get_japanese_font_properties() -> FontProperties | None:
    """
    利用可能な日本語フォントのプロパティオブジェクトを取得する。
    """
    for font_path in FONT_PATHS_TO_TRY:
        if os.path.exists(font_path):
            logging.info(f"Japanese font found at: {font_path}")
            return FontProperties(fname=font_path)

    logging.error("No standard Japanese fonts found in C:/Windows/Fonts/. Cannot display Japanese characters.")
    # 見つからなかった場合は、エラーメッセージを出力するためにNoneを返す
    return None
# ▲▲▲【修正はここまで】▲▲▲


def visualize_knowledge_graph(room_name: str):
    """
    Loads a directed knowledge graph and saves a visualization with a directly specified Japanese font.
    """
    # ▼▼▼【ここからが修正の核心】▼▼▼
    jp_font_prop = get_japanese_font_properties()
    if jp_font_prop is None:
        # フォントが見つからなかったことをUI側に伝える
        error_msg = "Error: Could not find any standard Japanese fonts in C:/Windows/Fonts/."
        print(error_msg, file=sys.stderr)
        sys.exit(1)
    # ▲▲▲【修正はここまで】▲▲▲

    rag_data_path = Path("characters") / room_name / "rag_data"
    graph_path = rag_data_path / "knowledge_graph.graphml"
    output_dir = rag_data_path / "visualizations"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"graph_{room_name}_{int(time.time())}.png"

    if not graph_path.exists():
        print(f"Error: Knowledge graph file not found at {graph_path}", file=sys.stderr)
        sys.exit(1)

    try:
        G = nx.read_graphml(graph_path)
        if not isinstance(G, nx.DiGraph):
            G = G.to_directed()

        if not G.nodes():
             print(f"Error: Knowledge graph for {room_name} is empty.", file=sys.stderr)
             sys.exit(1)

        plt.figure(figsize=(20, 16))
        pos = nx.spring_layout(G, k=1.5, iterations=50, seed=42)
        nx.draw_networkx_nodes(G, pos, node_size=4000, node_color='lightblue', alpha=0.9)

        # ▼▼▼【ここからが修正の核心】▼▼▼
        # ラベルとエッジラベルの描画時に、取得したフォントプロパティを直接指定する
        nx.draw_networkx_labels(G, pos, font_size=12, font_weight='bold', font_family=jp_font_prop.get_name())

        # ▼▼▼【ここからが修正箇所】▼▼▼
        # エッジの頻度に応じて線の太さを決定
        edge_widths = [d.get('frequency', 1) for u, v, d in G.edges(data=True)]

        edge_labels = nx.get_edge_attributes(G, 'label')
        nx.draw_networkx_edges(
            G,
            pos,
            edge_color='gray',
            width=[w * 0.8 for w in edge_widths], # 頻度に応じて太さを変更
            arrowstyle='->',
            arrowsize=20,
            alpha=0.7,
            node_size=4000
        )
        # ▲▲▲【修正ここまで】▲▲▲

        nx.draw_networkx_edge_labels(
            G, pos, edge_labels=edge_labels, font_color='red', font_size=10,
            font_family=jp_font_prop.get_name(),
            bbox=dict(facecolor='white', alpha=0.6, edgecolor='none', pad=0.5)
        )

        # タイトルにもフォントプロパティを適用
        plt.title(f"Knowledge Graph for {room_name}", size=24, fontproperties=jp_font_prop)
        # ▲▲▲【修正はここまで】▲▲▲

        plt.axis('off')
        plt.tight_layout()
        plt.savefig(output_path, format='png', dpi=150)
        plt.close()

        print(str(output_path))

    except Exception as e:
        error_details = traceback.format_exc()
        logging.error(f"Failed to visualize graph: {e}\n{error_details}", file=sys.stderr)
        print(f"Error: Failed to visualize graph. Details: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nexus Ark Knowledge Graph Visualizer")
    parser.add_argument("room_name", help="The name of the room to visualize.")
    args = parser.parse_args()
    visualize_knowledge_graph(args.room_name)
