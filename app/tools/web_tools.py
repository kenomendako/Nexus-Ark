# tools/web_tools.py (v7.0 - Tavily Integration & URL Reading)

from langchain_core.tools import tool
import google.genai as genai
from google.genai import types
import traceback
import time
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

# Playwright（JS主体の動的ページ取得のフォールバック）
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# requests+BeautifulSoup の抽出がこの文字数未満なら、JS描画ページとみなし Playwright を試す
PLAYWRIGHT_FALLBACK_MIN_CHARS = 200
# ブラウザ未インストールの警告は一度だけ出す（ホットパスで巨大DLは走らせない）
_playwright_missing_warned = False


def _fetch_with_playwright(url: str, timeout_ms: int = 20000) -> str | None:
    """JS描画後のページ本文をPlaywrightで取得する。

    同期Playwright APIは実行中のasyncioループ内で呼べないため、必ず専用スレッドで実行する。
    ブラウザ実行ファイルが無い場合はホットパスで巨大ダウンロードを走らせず、None を返して
    呼び出し側のフォールバック（requests/BeautifulSoup）に委ねる。
    """
    if not PLAYWRIGHT_AVAILABLE:
        return None

    import threading

    result: dict = {}

    def _worker() -> None:
        global _playwright_missing_warned
        try:
            import playwright_utils

            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(headless=True)
                except Exception as launch_exc:
                    if playwright_utils.is_executable_missing_error(launch_exc):
                        if not _playwright_missing_warned:
                            print("  - [Playwright] ブラウザ未インストールのため動的取得をスキップ（`python -m playwright install chromium` で有効化）")
                            _playwright_missing_warned = True
                        return
                    raise
                try:
                    context = browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    )
                    page = context.new_page()
                    page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                    result["text"] = page.inner_text("body")
                finally:
                    browser.close()
        except Exception as exc:
            result["error"] = exc

    worker = threading.Thread(target=_worker, name="web-fetch-playwright", daemon=True)
    worker.start()
    worker.join(timeout=timeout_ms / 1000 + 10)
    if worker.is_alive():
        print(f"  - [Playwright] 取得がタイムアウトしました: {url}")
        return None
    if "error" in result:
        print(f"  - [Playwright] 取得に失敗: {result['error']}")
        return None
    text = str(result.get("text") or "").strip()
    return text or None


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
    """Google検索（Gemini Native）を使用して検索を実行する内部関数。

    [2026-06-23 FIX] 検索もWebツール（内部処理的）であり、APIキーは共通設定のものを使い、
    429（そのキーのその検索モデルの枠超過）ではキーをローテーションする。503/504等の
    モデル混雑は同一キーで短く再試行し上限で諦める。従来は initial_api_key_name_global の
    1キー固定・ローテーション無しで、無料枠0のモデル等で即 429 失敗していた。
    """
    # 検索モデルは設定で上書き可能（モデル廃止・移行に追従）。未設定なら既定値。
    search_model = str(config_manager.CONFIG_GLOBAL.get("search_model") or "").strip() or constants.SEARCH_MODEL
    rotation_enabled = config_manager.CONFIG_GLOBAL.get("enable_api_key_rotation", True)

    search_tool_for_api = types.Tool(google_search=types.GoogleSearch())
    generation_config_with_tool = types.GenerateContentConfig(tools=[search_tool_for_api])

    # 共通設定のキーから開始（内部処理と同方針）。無ければ従来の初期キーへフォールバック。
    api_key = config_manager.get_active_gemini_api_key(None, model_name=search_model)
    if not api_key:
        api_key = config_manager.GEMINI_API_KEYS.get(config_manager.initial_api_key_name_global)
    if not api_key or api_key.startswith("YOUR_API_KEY"):
        return "[エラー: 有効なGoogle APIキーが設定されていません]"

    tried_keys = set()
    models_tried = set()
    server_error_retries = 0
    MAX_SERVER_ERROR_RETRIES = 3
    max_attempts = max(3, len(config_manager.GEMINI_API_KEYS)) + 2
    last_error = None

    for attempt in range(max_attempts):
        models_tried.add(search_model)
        key_name = config_manager.get_key_name_by_value(api_key)
        if key_name != "Unknown":
            tried_keys.add(key_name)

        # 枯渇キーは事前にローテーション
        if config_manager.is_key_exhausted(key_name, model_name=search_model):
            if not rotation_enabled:
                return "[エラー：Web検索キーが枯渇しています（共通設定でローテーション無効）]"
            nxt = config_manager.get_next_available_gemini_key(
                current_exhausted_key=key_name, excluded_keys=tried_keys, model_name=search_model)
            if not nxt:
                return "[エラー：Web検索に利用可能なAPIキーがありません（全キー枯渇）]"
            key_name = nxt
            api_key = config_manager.GEMINI_API_KEYS[nxt]
            tried_keys.add(nxt)

        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=f'models/{search_model}',
                contents=[query],
                config=generation_config_with_tool
            )

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
                        grounding_attributions.append(f"- [{title}]({attribution.web.uri})")

            if not text_parts and not grounding_attributions:
                return "[情報：Web検索で結果が見つかりませんでした]"

            final_response = "".join(text_parts)
            if grounding_attributions:
                final_response += "\n\n**引用元:**\n" + "\n".join(grounding_attributions)
            return final_response.strip()

        except Exception as e:
            last_error = e
            err = str(e).upper()
            # モデル未提供（廃止・無効・そのティアで利用不可）はキーを替えても解決しない。
            # 例: 404 NOT_FOUND "no longer available" / 429 で limit:0（=そのモデルが使えない）。
            is_model_unavailable = (
                "NOT_FOUND" in err or "NOT FOUND" in err or "NO LONGER AVAILABLE" in err
                or "LIMIT: 0" in err or "QUOTAVALUE': '0'" in err or '"QUOTAVALUE": "0"' in err
            )
            is_429 = "429" in err or "RESOURCE_EXHAUSTED" in err
            is_server = any(c in err for c in ["502", "503", "504", "500"]) or \
                any(m in err for m in ["UNAVAILABLE", "DEADLINE_EXCEEDED", "TIMEOUT", "CONNECTION", "CONNECTERROR", "PEER RESET"])

            if is_model_unavailable:
                # 設定の検索モデルが使えない → 全キー総当たりせず、既定モデルへ一度だけフォールバック。
                if constants.SEARCH_MODEL not in models_tried:
                    print(f"  - [Web検索] 検索モデル '{search_model}' は利用不可（{str(e)[:80]}）。既定モデル '{constants.SEARCH_MODEL}' で再試行します。")
                    search_model = constants.SEARCH_MODEL
                    tried_keys = set()
                    server_error_retries = 0
                    api_key = config_manager.get_active_gemini_api_key(None, model_name=search_model) or api_key
                    continue
                return (
                    f"[エラー：設定の検索モデル『{search_model}』が利用できません（廃止・無効の可能性）。"
                    "共通設定→検索プロバイダ設定で、有効な検索モデル（例: gemini-2.5-flash）に変更してください]"
                )

            if is_429:
                # そのキーのその検索モデルの枠超過 → 枯渇マークして別キーへ
                if key_name != "Unknown":
                    config_manager.mark_key_as_exhausted(key_name, model_name=search_model)
                print(f"  - [Web検索] 429 ({key_name}@{search_model})。キーをローテーションして再試行...")
                if not rotation_enabled:
                    break
                nxt = config_manager.get_next_available_gemini_key(
                    current_exhausted_key=key_name, excluded_keys=tried_keys, model_name=search_model)
                if not nxt:
                    break
                api_key = config_manager.GEMINI_API_KEYS[nxt]
                continue
            if is_server:
                # モデル混雑はキーを替えても無意味 → 同一キーで短く再試行、上限で諦める
                server_error_retries += 1
                if server_error_retries > MAX_SERVER_ERROR_RETRIES:
                    print(f"  - [Web検索] サーバ混雑(503等)が継続。諦めます: {str(e)[:120]}")
                    break
                wait = min(2 * server_error_retries, 8)
                print(f"  - [Web検索] サーバ混雑(503等) 同一キーで再試行 {server_error_retries}/{MAX_SERVER_ERROR_RETRIES} ({wait}s)...")
                time.sleep(wait)
                continue
            # その他は即終了
            print(f"  - Geminiネイティブ検索ツールでエラー: {e}")
            traceback.print_exc()
            return f"[エラー：Web検索中に問題が発生しました。詳細: {e}]"

    return f"[エラー：Web検索中に問題が発生しました（リトライ後）。詳細: {last_error}]"


def test_search_model(model: str, api_key_name: str | None = None) -> tuple[bool, str]:
    """
    指定モデル＋Geminiキーで Google検索グラウンディングが実際に使えるかを最小クエリで検証する。

    プラン・キー・モデルの組み合わせ（無料キーでの Pro/Gemma 可否など）は
    Google 側の仕様で変動するため、静的な可否表を持たずに実機テストで判定する。

    戻り値: (成功したか, ユーザー向けメッセージ)
    """
    model_name = str(model or "").strip() or constants.SEARCH_MODEL
    try:
        key_name = api_key_name or config_manager.initial_api_key_name_global
        api_key = config_manager.GEMINI_API_KEYS.get(key_name)
        if not api_key or api_key.startswith("YOUR_API_KEY"):
            return False, f"有効なGoogle APIキー '{key_name}' が設定されていません。"

        client = genai.Client(api_key=api_key)
        search_tool_for_api = types.Tool(google_search=types.GoogleSearch())
        config = types.GenerateContentConfig(tools=[search_tool_for_api])

        response = client.models.generate_content(
            model=f"models/{model_name}",
            contents=["今日のニュースを1つ教えて"],
            config=config,
        )

        # 応答が返れば、そのモデル＋キーで grounding 呼び出し自体は通っている。
        has_text = bool(
            response and response.candidates
            and response.candidates[0].content
            and response.candidates[0].content.parts
        )
        if has_text:
            return True, f"モデル「{model_name}」でGoogle検索を実行できました。"
        return True, f"モデル「{model_name}」で呼び出しは成功しましたが、応答テキストは空でした。"

    except Exception as e:
        return False, f"モデル「{model_name}」では検索を実行できませんでした。詳細: {e}"


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

            # 4. JS主体ページ対策：抽出が薄ければ Playwright で描画後の本文を取得する
            if len(text.strip()) < PLAYWRIGHT_FALLBACK_MIN_CHARS:
                rendered = _fetch_with_playwright(url)
                if rendered and len(rendered.strip()) > len(text.strip()):
                    print(f"  - [Playwright] 動的ページを描画して取得しました: {url}")
                    text = rendered

            if len(text) > 3000:
                text = text[:3000] + "\n...(省略)..."

            formatted_parts.append(f"## {url}\n\n{text}")
            
        except Exception as e:
            formatted_parts.append(f"## {url}\n\n[取得失敗: {e}]")

    if not formatted_parts:
        return "[情報: コンテンツを取得できませんでした]"
    
    final_response = "\n\n---\n\n".join(formatted_parts)
    num_ok = sum(1 for p in formatted_parts if "[取得失敗" not in p)
    
    return f"**取得完了 ({num_ok}/{len(formatted_parts)}件)**\n\n{final_response}\n\n**読み取った情報を元に、ペルソナとして適切な回答を行ってください。**"
