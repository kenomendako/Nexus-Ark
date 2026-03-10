# tools/web_tools.py (v7.0 - Tavily Integration & URL Reading)

from langchain_core.tools import tool
import google.genai as genai
from google.genai import types
import traceback
import config_manager
import constants
from ddgs import DDGS

# Tavilyのインポート（インストールされていない場合のフォールバック対応）
try:
    from langchain_tavily import TavilySearch, TavilyExtract
    TAVILY_AVAILABLE = True
except ImportError:
    TAVILY_AVAILABLE = False
    print("警告: langchain-tavilyがインストールされていません。Tavily機能は利用できません。")

# pypdfのインポート
try:
    import pypdf
    import io
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False


def _search_with_tavily(query: str) -> str:
    """Tavily APIを使用して検索を実行する内部関数"""
    if not TAVILY_AVAILABLE:
        return "[エラー: Tavilyライブラリがインストールされていません。`pip install langchain-tavily` を実行してください]"
    
    api_key = config_manager.TAVILY_API_KEY
    if not api_key:
        return "[エラー: Tavily APIキーが設定されていません。共通設定 → APIキー管理 から設定してください]"
    
    try:
        tavily = TavilySearch(
            tavily_api_key=api_key,
            max_results=5,
            include_answer=True,  # AIが生成した回答も含める
            include_raw_content=False,  # 生コンテンツは不要（トークン節約）
        )
        results = tavily.invoke(query)
        
        if not results:
            return "[情報: Tavily検索で結果が見つかりませんでした]"
        
        # 結果を整形
        formatted_parts = []
        citations = []
        
        # Tavilyの回答がある場合は最初に表示
        if isinstance(results, dict):
            if results.get("answer"):
                formatted_parts.append(f"**AI要約:**\n{results['answer']}\n")
            
            for result in results.get("results", []):
                title = result.get("title", "No Title")
                url = result.get("url", "#")
                content = result.get("content", "")
                
                # コンテンツを適度な長さに切り詰め
                if len(content) > 500:
                    content = content[:500] + "..."
                
                formatted_parts.append(f"### {title}\n{content}")
                citations.append(f"- [{title}]({url})")
        elif isinstance(results, list):
            # リスト形式の場合
            for result in results:
                title = result.get("title", "No Title")
                url = result.get("url", "#")
                content = result.get("content", "")
                
                if len(content) > 500:
                    content = content[:500] + "..."
                
                formatted_parts.append(f"### {title}\n{content}")
                citations.append(f"- [{title}]({url})")
        
        final_response = "\n\n".join(formatted_parts)
        if citations:
            final_response += "\n\n**引用元 (Tavily):**\n" + "\n".join(citations)
        
        return final_response
        
    except Exception as e:
        print(f"  - Tavily検索でエラー: {e}")
        traceback.print_exc()
        return f"[エラー: Tavily検索中に問題が発生しました。詳細: {e}]"


def _search_with_ddg(query: str) -> str:
    """DuckDuckGoを使用して検索を実行する内部関数"""
    try:
        results = DDGS().text(query, max_results=5)
        if not results:
            return "[情報: DuckDuckGo検索で結果が見つかりませんでした]"
        
        formatted_results = []
        citations = []
        for i, res in enumerate(results):
            title = res.get('title', 'No Title')
            href = res.get('href', '#')
            body = res.get('body', '')
            formatted_results.append(f"### {title}\n{body}")
            citations.append(f"- [{title}]({href})")
        
        final_response = "\n\n".join(formatted_results)
        if citations:
            final_response += "\n\n**引用元 (DuckDuckGo):**\n" + "\n".join(citations)
        
        return final_response
        
    except Exception as e:
        print(f"  - DuckDuckGo検索でエラー: {e}")
        traceback.print_exc()
        return f"[エラー: DuckDuckGo検索中に問題が発生しました。詳細: {e}]"


def _search_with_google(query: str) -> str:
    """Google検索（Gemini Native）を使用して検索を実行する内部関数"""
    try:
        api_key_name = config_manager.initial_api_key_name_global
        api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
        if not api_key or api_key.startswith("YOUR_API_KEY"):
            return f"[エラー: 有効なGoogle APIキー '{api_key_name}' が設定されていません]"

        client = genai.Client(api_key=api_key)

        # グラウンディングのための検索ツールを定義
        search_tool_for_api = types.Tool(
            google_search=types.GoogleSearch()
        )

        generation_config_with_tool = types.GenerateContentConfig(
            tools=[search_tool_for_api]
        )

        response = client.models.generate_content(
            model=f'models/{constants.SEARCH_MODEL}',
            contents=[query],
            config=generation_config_with_tool
        )

        # 応答からテキストと引用情報を抽出
        grounding_attributions = []
        text_parts = []

        if response and response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.text:
                    text_parts.append(part.text)

        if response and response.candidates and hasattr(response.candidates[0], 'grounding_attributions'):
            for attribution in response.candidates[0].grounding_attributions:
                if attribution.web:
                    title = attribution.web.title or "無題のページ"
                    uri = attribution.web.uri
                    grounding_attributions.append(f"- [{title}]({uri})")

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
def web_search_tool(query: str, room_name: str) -> str:
    """
    ユーザーからのクエリに基づいて、最新の情報を得るためにWeb検索を実行します。
    設定に応じて、Tavily、Google検索（Geminiネイティブ）、またはDuckDuckGo検索を使用します。
    """
    # 設定から検索プロバイダを取得
    provider = config_manager.CONFIG_GLOBAL.get("search_provider", constants.DEFAULT_SEARCH_PROVIDER)
    
    if provider == "disabled":
        return "[情報: Web検索機能は現在無効化されています]"

    print(f"--- Web検索ツール実行 (Provider: {provider}, Query: '{query}') ---")

    # プロバイダに応じて検索を実行
    if provider == "tavily":
        return _search_with_tavily(query)
    elif provider == "ddg":
        return _search_with_ddg(query)
    else:  # google (デフォルト)
        return _search_with_google(query)


@tool
def read_url_tool(urls: list[str], room_name: str) -> str:
    """
    指定されたURLリストの内容を読み取り、結合して単一の文字列として返すツール。
    PDFの場合は直接テキストを抽出し、Webページの場合はTavily ExtractまたはBeautifulSoupを使用します。
    """
    if not urls:
        return "URLが指定されていません。"
    
    import requests
    from bs4 import BeautifulSoup
    
    # URLを5件に制限
    urls_to_fetch = urls[:5]
    formatted_parts = []
    
    for url in urls_to_fetch:
        try:
            # 1. PDF判定（拡張子またはURLパターン）
            is_pdf = url.lower().split('?')[0].endswith('.pdf')
            
            if is_pdf:
                if not PYPDF_AVAILABLE:
                    formatted_parts.append(f"## {url}\n\n[取得失敗: PDF読み取りライブラリ pypdf が未設定です]")
                    continue
                
                print(f"--- PDF読取実行: {url} ---")
                response = requests.get(url, timeout=20, stream=True, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                response.raise_for_status()
                
                with io.BytesIO(response.content) as pdf_file:
                    reader = pypdf.PdfReader(pdf_file)
                    pdf_text = []
                    max_pages = min(len(reader.pages), 10)
                    for page_num in range(max_pages):
                        page_text = reader.pages[page_num].extract_text()
                        if page_text:
                            pdf_text.append(f"--- Page {page_num + 1} ---\n{page_text}")
                    
                    text = "\n\n".join(pdf_text)
                    if len(reader.pages) > 10:
                        text += f"\n\n...(全{len(reader.pages)}ページ中 10ページ目まで抽出しました)..."
                    
                    if not text.strip():
                        text = "[情報: PDFからテキストを抽出できませんでした（画像ベースの可能性があります）]"
                    
                    formatted_parts.append(f"## {url} (PDF)\n\n{text}")
                continue

            # 2. Webページの場合：Tavily Extract (利用可能な場合)
            if TAVILY_AVAILABLE and config_manager.TAVILY_API_KEY:
                try:
                    extractor = TavilyExtract(
                        tavily_api_key=config_manager.TAVILY_API_KEY,
                        extract_depth="basic"
                    )
                    results = extractor.invoke({"urls": [url]})
                    if results and (isinstance(results, list) or isinstance(results, dict)):
                        # Tavilyの結果を展開
                        item = results[0] if isinstance(results, list) else results.get("results", [{}])[0]
                        content = item.get("raw_content", item.get("content", ""))
                        if content:
                            if len(content) > 3000:
                                content = content[:3000] + "\n...(省略)..."
                            formatted_parts.append(f"## {url}\n\n{content}")
                            continue
                except Exception as e:
                    print(f"  - Tavily Extract失敗 (URL: {url}): {e}")

            # 3. フォールバック：BeautifulSoupでのスクレイピング
            response = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            for script in soup(["script", "style"]):
                script.decompose()
            
            text = soup.get_text(separator='\n', strip=True)
            if len(text) > 3000:
                text = text[:3000] + "\n...(省略)..."
            
            formatted_parts.append(f"## {url}\n\n{text}")
            
        except Exception as e:
            formatted_parts.append(f"## {url}\n\n[取得失敗: {e}]")

    if not formatted_parts:
        return "[情報: コンテンツを取得できませんでした]"
    
    final_response = "\n\n---\n\n".join(formatted_parts)
    num_ok = sum(1 for p in formatted_parts if "[取得失敗" not in p)
    
    return f"**取得完了 ({num_ok}/{len(formatted_parts)}件)**\n\n{final_response}\n\n**読み取った情報を元に、ルシアンとして適切な回答を行ってください。**"
