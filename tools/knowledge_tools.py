# tools/knowledge_tools.py
import os
from pathlib import Path
from langchain_core.tools import tool
import networkx as nx
import spacy

# --- spaCy Model Loading ---
# このツールが独立して動作できるよう、自身のプロセスでモデルをロードする
try:
    nlp = spacy.load("ja_core_news_lg")
except OSError:
    # モデルがない場合でも、ツール自体は定義されるようにする
    # 呼び出し時にエラーを返すことで、アプリケーション全体の起動は妨げない
    nlp = None

@tool
def search_knowledge_graph(query: str, room_name: str) -> str:
    """
    AI自身の長期記憶（知識グラフ）に保存されている、過去の会話から抽出された客観的な事実や、登場人物・場所・物事の関係性について、自然言語で検索する。
    query: 検索したい内容を記述した、自然言語の質問文（例：「キャラクターAとアイテムBの関係は？」）。
    """
    # 1. 前提条件のチェック
    if nlp is None:
        return "【エラー】言語解析モデルがロードされていません。管理者に連絡してください。"
    if not room_name:
        return "【エラー】検索対象のルームが指定されていません。"

    # 2. 知識グラフの読み込み
    graph_path = Path("characters") / room_name / "rag_data" / "knowledge_graph.graphml"
    if not graph_path.exists():
        return "【情報】このルームには、まだ知識グラフ（長期記憶）が構築されていません。"
    try:
        G = nx.read_graphml(graph_path)
    except Exception as e:
        return f"【エラー】知識グラフファイルの読み込みに失敗しました: {e}"

    if not G.nodes():
        return "【情報】知識グラフは存在しますが、まだ空です。"

    # 3. 質問文から、核となるエンティティを抽出
    doc = nlp(query)
    entities_in_query = [ent.text for ent in doc.ents]

    # もしspaCyが固有表現を見つけられなかった場合、
    # 質問文そのもの（query）を、検索キーワードのリストに追加するフォールバック処理
    if not entities_in_query:
        # 質問文から、助詞や句読点などを取り除き、キーワードだけを抽出する
        # （例：「ルシアンに関する情報を教えて」 -> 「ルシアン 情報」）
        keywords = [token.lemma_ for token in doc if not token.is_stop and not token.is_punct and token.pos_ not in ['AUX', 'PART']]
        if keywords:
            entities_in_query.extend(keywords)
        else:
            # それでもキーワードが見つからなければ、元のqueryをそのまま使う
            entities_in_query.append(query)

    if not entities_in_query:
        return "【エラー】質問文から、検索の核となるキーワードを特定できませんでした。"

    # 4. グラフ内から、関連する情報を検索
    found_facts = []
    # 質問に含まれる各エンティティについてループ
    for entity in entities_in_query:
        # グラフ内に、そのエンティティに「部分一致」するノードがあるか検索
        # （例：質問が「ルシアン」なら、グラフの「ルシアン」がヒットする）
        # 以前の `in` 演算子による部分一致検索は、意図しない結果を生む可能性があるため、
        # より厳格な「完全一致」でまず探し、見つからなければ部分一致を試みる、という2段階の検索に変更する。

        # ステップA: 完全一致するノードを探す
        matching_nodes = [node for node in G.nodes() if entity == node]

        # ステップB: もし完全一致で見つからなければ、部分一致を試みる
        if not matching_nodes:
            matching_nodes = [node for node in G.nodes() if entity in node]

        for node in matching_nodes:
            # そのノードに接続されている全てのエッジ（関係性）を取得
            for neighbor in G.neighbors(node):
                relation_data = G.get_edge_data(node, neighbor)
                relation = relation_data.get("relation", "不明な関係")
                fact = f"- 「{node}」は「{neighbor}」と「{relation}」の関係にあります。"
                if fact not in found_facts:
                    found_facts.append(fact)

    # 5. 結果を整形して返す
    if not found_facts:
        return f"【検索結果】「{', '.join(entities_in_query)}」に関する客観的な事実は、長期記憶の中に見つかりませんでした。"

    summary = f"【長期記憶からの検索結果】\n" + "\n".join(found_facts)
    summary += "\n\n**この知識検索タスクは完了しました。これから思い出すというような前置きはせず、**見つかった事実を元に会話を続けてください。"
    return summary
