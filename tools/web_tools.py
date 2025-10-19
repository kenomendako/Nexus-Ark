# tools/web_tools.py (v6.0 - The Restoration of Proven Glory)

from langchain_core.tools import tool
import google.genai as genai
from google.genai import types
import traceback
import config_manager
import constants

@tool
def web_search_tool(query: str, room_name: str) -> str:
    """
    ユーザーからのクエリに基づいて、最新の情報を得るためにGoogle検索を実行します。
    このツールは、Geminiモデル自身の思考プロセスに、Google検索の結果を直接統合（グラウンディング）させます。
    room_name引数は、ツール呼び出しの統一性のために存在しますが、このツールでは直接使用されません。
    """
    print(f"--- Geminiネイティブ検索ツール実行 (Query: '{query}') ---")
    try:
        api_key_name = config_manager.initial_api_key_name_global
        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
        if not api_key or api_key.startswith("YOUR_API_KEY"):
            return f"[エラー: 有効なGoogle APIキー '{api_key_name}' が設定されていません]"

        client = genai.Client(api_key=api_key)

        # ▼▼▼【ここからが修正箇所】▼▼▼

        # 1. グラウンディングのための「検索ツール」を新しい形式で定義
        search_tool_for_api = types.Tool(
            google_search=types.GoogleSearch()
        )

        # 2. ツール設定を、プロジェクト規約に則り GenerateContentConfig オブジェクトに格納
        generation_config_with_tool = types.GenerateContentConfig(
            tools=[search_tool_for_api]
        )

        # 3. 検索機能が保証された専用モデルを定数から呼び出す
        response = client.models.generate_content(
            model=f'models/{constants.SEARCH_MODEL}', # ← この行を変更
            contents=[query],
            config=generation_config_with_tool
        )

        # ▲▲▲【修正はここまで】▲▲▲

        # 4. 応答からテキストと引用情報を抽出し、整形して返します
        grounding_attributions = []
        text_parts = []

        if response and response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.text:
                    text_parts.append(part.text)

        # ▼▼▼【ここからが修正箇所】▼▼▼
        # 'grounding_attributions' 属性が存在するかどうかを、hasattr() を使って安全に確認する
        if response and response.candidates and hasattr(response.candidates[0], 'grounding_attributions'):
             for attribution in response.candidates[0].grounding_attributions:
                if attribution.web:
                    title = attribution.web.title or "無題のページ"
                    uri = attribution.web.uri
                    grounding_attributions.append(f"- [{title}]({uri})")
        # ▲▲▲【修正はここまで】▲▲▲

        if not text_parts and not grounding_attributions:
            return "[情報：Web検索で結果が見つかりませんでした]"

        final_response = "".join(text_parts)
        if grounding_attributions:
            final_response += "\n\n**引用元:**\n" + "\n".join(grounding_attributions)

        return final_response.strip()

    except Exception as e:
        print(f"  - Geminiネイティブ検索ツールでエラー: {e}")
        traceback.print_exc()
        return f"[エラー：Web検索中に問題が発生しました。詳細: {e}]"

@tool
def read_url_tool(urls: list[str], room_name: str) -> str:
    """
    指定されたURLリストの内容を読み取り、結合して単一の文字列として返すツール。
    """
    if not urls:
        return "URLが指定されていません。"
    try:
        return "Success: URLの内容を読み取りました。（この機能は現在スタブです）**このタスクは完了しました。これから読むというような前置きはせず、**読み取った情報を元に会話を続けてください。"
    except Exception as e:
        return f"URLの内容取得中に予期せぬシステムエラーが発生しました。URLが無効か、ページがアクセスできない可能性があります。詳細: {e}"
