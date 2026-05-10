import os
import importlib.util
import inspect
import asyncio
from contextlib import asynccontextmanager
import subprocess
import sys
import py_compile
import tempfile
from typing import List, Callable, Dict, Any, Tuple, Optional
import traceback
import httpx
from langchain_core.tools import tool, BaseTool

_MCP_TOOLS_CACHE = None

DEFAULT_CUSTOM_TOOL_RESULT_PROMPT = (
    "実行結果を踏まえて、必要な場合は相手に自然に報告してください。"
    "失敗や不足がある場合は隠さず、次に必要な確認事項を伝えてください。"
)

class CustomToolManager:
    """
    ユーザー自作ツール（PythonプラグインおよびMCPツール）を管理するクラス。
    """
    def __init__(self, plugin_dir: str = "custom_tools"):
        self.plugin_dir = plugin_dir

    @classmethod
    def clear_mcp_cache(cls):
        global _MCP_TOOLS_CACHE
        _MCP_TOOLS_CACHE = None

    def get_all_custom_tools(self) -> List[Callable]:
        """
        ローカルプラグインとMCPツールを統合して返す。
        """
        local_tools = self.load_local_plugins()
        mcp_tools = self.load_mcp_tools()
        return local_tools + mcp_tools

    def load_local_plugins(self) -> List[Callable]:
        """
        custom_tools/ ディレクトリ内の .py ファイルから @tool デコレータが付いた関数をロードする。
        """
        tools = []
        if not os.path.exists(self.plugin_dir):
            return tools

        import config_manager
        config = config_manager.load_config_file()
        settings = config.get("custom_tools_settings", {})
        disabled_plugins = settings.get("disabled_local_plugins", [])
        stored_metadata = settings.get("tool_metadata", {})

        for filename in os.listdir(self.plugin_dir):
            if filename.endswith(".py") and filename != "__init__.py":
                if filename in disabled_plugins:
                    continue
                module_name = filename[:-3]
                file_path = os.path.abspath(os.path.join(self.plugin_dir, filename))
                try:
                    spec = importlib.util.spec_from_file_location(module_name, file_path)
                    if spec is None: continue
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if self._is_langchain_tool(attr):
                            if not any(t.name == attr.name for t in tools):
                                metadata = self._build_local_tool_metadata(
                                    filename=filename,
                                    tool_name=attr.name,
                                    tool_description=getattr(attr, "description", ""),
                                    module=module,
                                    stored_metadata=stored_metadata,
                                )
                                object.__setattr__(attr, "nexus_tool_metadata", metadata)
                                tools.append(attr)
                except Exception as e:
                    print(f"  - [CustomTool] '{filename}' ロード失敗: {e}")

        return tools

    def load_mcp_tools(self) -> List[Callable]:
        """
        config.json に設定された MCP サーバからツールをロードする。
        （プロセスごとに一度だけロードし、キャッシュする）
        """
        global _MCP_TOOLS_CACHE
        if _MCP_TOOLS_CACHE is not None:
            return _MCP_TOOLS_CACHE

        import config_manager
        settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
        if not settings.get("enabled", True):
            return []
            
        mcp_servers = settings.get("mcp_servers", [])
        if not mcp_servers:
            return []

        all_mcp_tools = []
        for server in mcp_servers:
            # 個別サーバの有効/無効チェック
            if not server.get("enabled", True):
                continue
                
            try:
                # サーバごとのツールを取得 (別スレッドで実行してネストしたループを避ける)
                tools = self._run_sync(self._fetch_tools_from_mcp_server(server))
                
                # ツール単位の無効化チェック
                disabled_tools = server.get("disabled_tools", [])
                if disabled_tools:
                    tools = [t for t in tools if t.name not in disabled_tools]
                
                all_mcp_tools.extend(tools)
            except Exception as e:
                print(f"  - [CustomTool] MCPサーバ '{server.get('name')}' からのロード失敗: {e}")
        
        _MCP_TOOLS_CACHE = all_mcp_tools
        return all_mcp_tools

    @staticmethod
    def metadata_key(source: str, source_name: str, tool_name: str) -> str:
        """設定保存用の安定キーを生成する。"""
        return f"{source}:{source_name}:{tool_name}"

    @staticmethod
    def _shorten(text: str, limit: int = 180) -> str:
        value = " ".join(str(text or "").split())
        if len(value) <= limit:
            return value
        return value[:limit - 1].rstrip() + "..."

    def _normalize_tool_metadata(
        self,
        *,
        source: str,
        source_name: str,
        tool_name: str,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        meta = dict(metadata or {})
        summary = meta.get("summary") or self._shorten(description)
        return {
            "source": source,
            "source_name": source_name,
            "tool_name": tool_name,
            "display_name": meta.get("display_name") or tool_name,
            "summary": summary,
            "use_when": meta.get("use_when", ""),
            "result_prompt": meta.get("result_prompt") or DEFAULT_CUSTOM_TOOL_RESULT_PROMPT,
            "visibility": meta.get("visibility", "normal"),
            "metadata_key": self.metadata_key(source, source_name, tool_name),
        }

    def _extract_module_metadata(self, module: Any, tool_name: str) -> Dict[str, Any]:
        raw = getattr(module, "NEXUS_TOOL_METADATA", None)
        if not isinstance(raw, dict):
            return {}

        if tool_name in raw and isinstance(raw.get(tool_name), dict):
            return dict(raw[tool_name])

        # 単一ツールファイル向けの省略形: NEXUS_TOOL_METADATA = {"summary": "..."}
        known_keys = {"display_name", "summary", "use_when", "result_prompt", "visibility"}
        if any(k in raw for k in known_keys):
            return dict(raw)
        return {}

    def _build_local_tool_metadata(
        self,
        *,
        filename: str,
        tool_name: str,
        tool_description: str,
        module: Any,
        stored_metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        base = self._extract_module_metadata(module, tool_name)
        key = self.metadata_key("local_plugin", filename, tool_name)
        if isinstance(stored_metadata.get(key), dict):
            base.update(stored_metadata[key])
        return self._normalize_tool_metadata(
            source="local_plugin",
            source_name=filename,
            tool_name=tool_name,
            description=tool_description,
            metadata=base,
        )

    def _build_mcp_tool_metadata(
        self,
        *,
        server_conf: Dict[str, Any],
        tool_name: str,
        tool_description: str,
    ) -> Dict[str, Any]:
        server_name = server_conf.get("name", "")
        base = {}
        server_metadata = server_conf.get("tool_metadata", {})
        if isinstance(server_metadata.get(tool_name), dict):
            base.update(server_metadata[tool_name])

        try:
            import config_manager
            settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
            stored_metadata = settings.get("tool_metadata", {})
            key = self.metadata_key("mcp", server_name, tool_name)
            if isinstance(stored_metadata.get(key), dict):
                base.update(stored_metadata[key])
        except Exception:
            pass

        return self._normalize_tool_metadata(
            source="mcp",
            source_name=server_name,
            tool_name=tool_name,
            description=tool_description,
            metadata=base,
        )

    @classmethod
    def get_tool_result_prompt(cls, tool: Callable, was_error: bool = False) -> str:
        meta = getattr(tool, "nexus_tool_metadata", None)
        if not isinstance(meta, dict):
            return ""
        prompt = meta.get("result_prompt") or DEFAULT_CUSTOM_TOOL_RESULT_PROMPT
        if was_error and "失敗" not in prompt and "エラー" not in prompt:
            prompt += " 失敗やエラーが含まれる場合は、成功したように扱わず正直に説明してください。"
        return str(prompt).strip()

    def _run_sync(self, coro):
        """非同期コルーチンを同期的に実行するヘルパー"""
        import threading
        
        result = [None]
        error = [None]

        def target():
            try:
                # 新しいイベントループを作成して実行
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result[0] = loop.run_until_complete(coro)
                loop.close()
            except Exception as e:
                error[0] = e

        thread = threading.Thread(target=target)
        thread.start()
        thread.join()

        if error[0]:
            raise error[0]
        return result[0]

    @asynccontextmanager
    async def _create_transport_context(self, server_conf: Dict[str, Any]):
        """
        トランスポート種別に応じた MCP クライアント接続コンテキストを返す。
        stdio / sse / streamable_http を統一的に扱う。
        yield: (read_stream, write_stream)
        """
        transport_type = server_conf.get("type", "stdio")

        if transport_type == "stdio":
            from mcp.client.stdio import stdio_client
            params = self._build_stdio_params(server_conf)
            async with stdio_client(params) as (read, write):
                yield read, write

        elif transport_type == "streamable_http":
            from mcp.client.streamable_http import streamable_http_client
            url = server_conf.get("url", "")
            if not url:
                raise ValueError("Streamable HTTP サーバの URL が指定されていません。")
            async with streamable_http_client(url) as (read, write, _get_session_id):
                yield read, write

        elif transport_type == "sse":
            from mcp.client.sse import sse_client
            url = server_conf.get("url", "")
            if not url:
                raise ValueError("SSE サーバの URL が指定されていません。")
            async with sse_client(url) as (read, write):
                yield read, write

        else:
            raise ValueError(f"未対応のトランスポート種別: {transport_type}")

    async def _fetch_tools_from_mcp_server(self, server_conf: Dict[str, Any]) -> List[Callable]:
        """
        特定の MCP サーバに接続し、ツールのリストを取得してラップする。
        """
        from mcp import ClientSession
        from langchain_core.tools import StructuredTool

        tools = []

        try:
            if server_conf.get("type") != "simple_http":
                async with self._create_transport_context(server_conf) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        mcp_tools_resp = await session.list_tools()
                        
                        for mcp_tool in mcp_tools_resp.tools:
                            # MCPツールを同期的な LangChain ツールに変換
                            
                            def create_mcp_executor(t_name, s_conf):
                                def execute(input_args: Dict[str, Any] = None, **kwargs):
                                    # kwargs と input_args を統合
                                    merged_args = {}
                                    if input_args and isinstance(input_args, dict):
                                        merged_args.update(input_args)
                                    merged_args.update(kwargs)
                                    # 実行時に再度接続して呼び出す
                                    return self._run_sync(self._call_mcp_tool(s_conf, t_name, merged_args))
                                return execute
    
                            # StructuredTool を使用。args_schema は本来 JSON Schema から生成すべきだが、
                            # ここでは AI が説明文から引数を推測できるように、description を強化する。
                            desc = mcp_tool.description or ""
                            if mcp_tool.inputSchema:
                                import json
                                desc += f"\nArgs Schema: {json.dumps(mcp_tool.inputSchema.get('properties', {}), ensure_ascii=False)}"
    
                            lc_tool = StructuredTool.from_function(
                                func=create_mcp_executor(mcp_tool.name, server_conf),
                                name=mcp_tool.name,
                                description=desc
                            )
                            metadata = self._build_mcp_tool_metadata(
                                server_conf=server_conf,
                                tool_name=mcp_tool.name,
                                tool_description=mcp_tool.description or "",
                            )
                            object.__setattr__(lc_tool, "nexus_tool_metadata", metadata)
                            tools.append(lc_tool)
            elif server_conf.get("type") == "simple_http":
                # シンプルな JSON-RPC over HTTP POST
                url = server_conf.get("url", "")
                if not url:
                    raise ValueError("Simple HTTP サーバの URL が指定されていません。")
                
                # list_tools を呼び出す
                resp = await self._call_simple_http_jsonrpc(url, "tools/list", {})
                if "result" in resp and "tools" in resp["result"]:
                    from langchain_core.tools import StructuredTool
                    for mcp_tool in resp["result"]["tools"]:
                        def create_simple_executor(t_name, s_conf):
                            def execute(input_args: Dict[str, Any] = None, **kwargs):
                                merged_args = {}
                                if input_args and isinstance(input_args, dict):
                                    merged_args.update(input_args)
                                merged_args.update(kwargs)
                                return self._run_sync(self._call_mcp_tool(s_conf, t_name, merged_args))
                            return execute
                        
                        name = mcp_tool.get("name")
                        desc = mcp_tool.get("description", "")
                        schema = mcp_tool.get("inputSchema", {})
                        if schema:
                            import json
                            desc += f"\nArgs Schema: {json.dumps(schema.get('properties', {}), ensure_ascii=False)}"
                        
                        lc_tool = StructuredTool.from_function(
                            func=create_simple_executor(name, server_conf),
                            name=name,
                            description=desc
                        )
                        metadata = self._build_mcp_tool_metadata(
                            server_conf=server_conf,
                            tool_name=name,
                            tool_description=mcp_tool.get("description", ""),
                        )
                        object.__setattr__(lc_tool, "nexus_tool_metadata", metadata)
                        tools.append(lc_tool)
                else:
                    raise ValueError(f"Simple HTTP からのツールリスト取得に失敗しました: {resp}")
        except BaseException as e:
            # mcpライブラリ(anyio)のクリーンアップ処理（プロセス終了時）で例外が出ることがあるが、
            # ツールが取得できていれば無視してよい。
            if not tools:
                raise e
            
        return tools

    async def _call_mcp_tool(self, server_conf: Dict[str, Any], tool_name: str, args: Dict[str, Any]) -> str:
        """
        ツール実行時に MCP サーバに接続して呼び出しを行う。
        """
        from mcp import ClientSession

        try:
            if server_conf.get("type") != "simple_http":
                async with self._create_transport_context(server_conf) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, args)
                        # 結果を文字列として結合
                        text_parts = [p.text for p in result.content if hasattr(p, 'text')]
                        return "\n".join(text_parts) if text_parts else str(result)
            else:
                url = server_conf.get("url", "")
                resp = await self._call_simple_http_jsonrpc(url, "tools/call", {"name": tool_name, "arguments": args})
                if "result" in resp and "content" in resp["result"]:
                    text_parts = [p.get("text", "") for p in resp["result"]["content"] if p.get("type") == "text"]
                    return "\n".join(text_parts) if text_parts else str(resp["result"])
                elif "error" in resp:
                    return f"Error from simple_http tool: {resp['error']}"
                else:
                    return str(resp.get("result", resp))
        except BaseException as e:
            return f"Error calling MCP tool '{tool_name}': {str(e)}"

    async def _call_simple_http_jsonrpc(self, url: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """シンプルな JSON-RPC over HTTP POST 呼び出し"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            return resp.json()

    def _build_stdio_params(self, server_conf: Dict[str, Any]):
        """StdioServerParameters を構築する共通ヘルパー。相対パスを自動解決する。"""
        import sys
        from mcp import StdioServerParameters
        base_dir = os.path.dirname(os.path.abspath(__file__))
        
        # コマンドが python 系なら、Nexus Ark と同じ Python を使用する
        command = server_conf["command"]
        if os.path.basename(command).rstrip("0123456789.") in ("python", ""):
            # symlink を解決すると venv 環境が壊れる場合があるためそのまま使用
            command = sys.executable
        
        # 引数内の相対パスを絶対パスに変換
        resolved_args = []
        for arg in server_conf.get("args", []):
            candidate = os.path.join(base_dir, arg)
            if os.path.exists(candidate):
                resolved_args.append(os.path.abspath(candidate))
            else:
                resolved_args.append(arg)
        
        # venv の site-packages を子プロセスで認識できるよう環境変数を設定
        env = os.environ.copy()
        venv_dir = os.path.join(base_dir, ".venv")
        if os.path.isdir(venv_dir):
            env["VIRTUAL_ENV"] = venv_dir
            env["PATH"] = os.path.join(venv_dir, "bin") + os.pathsep + env.get("PATH", "")
        
        return StdioServerParameters(
            command=command,
            args=resolved_args,
            env=env,
            cwd=base_dir
        )

    def _is_langchain_tool(self, attr: any) -> bool:
        try:
            from langchain_core.tools import BaseTool
            if isinstance(attr, BaseTool): return True
        except ImportError: pass
        if hasattr(attr, "name") and hasattr(attr, "description") and hasattr(attr, "args_schema"):
            return True
        return False

    # --- ツール自作支援機能 ---
    
    @staticmethod
    def validate_code(code: str) -> Tuple[bool, str]:
        """Pythonコードの構文チェックを行う"""
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", encoding="utf-8", delete=False) as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        
        try:
            py_compile.compile(tmp_path, doraise=True)
            return True, "Success"
        except Exception as e:
            # エラーメッセージを簡潔に抽出
            err_msg = str(e)
            if "PyCompileError:" in err_msg:
                err_msg = err_msg.split("PyCompileError:")[1].strip()
            return False, err_msg
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @staticmethod
    def get_dependencies(code: str) -> List[str]:
        """コード内の # dependencies: pkg1, pkg2 形式のコメントを抽出する"""
        deps = []
        for line in code.splitlines():
            line = line.strip()
            if line.startswith("# dependencies:"):
                parts = line.replace("# dependencies:", "").split(",")
                deps.extend([p.strip() for p in parts if p.strip()])
        return deps

    @staticmethod
    def install_dependencies(deps: List[str]) -> Tuple[bool, str]:
        """依存パッケージをチェックし、承認済みであれば uv pip でインストールする"""
        if not deps:
            return True, "No dependencies to install"
            
        import config_manager
        settings = config_manager.CONFIG_GLOBAL.get("custom_tools_settings", {})
        allowed_deps = settings.get("allowed_dependencies", [])
        
        # 1. 未承認のパッケージをチェック
        unauthorized = []
        for dep in deps:
            # バージョン指定を含めた完全一致でチェック
            if dep not in allowed_deps:
                unauthorized.append(dep)
        
        if unauthorized:
            # 承認待ちリストに追加（重複排除）
            pending = settings.get("pending_dependencies", [])
            for u in unauthorized:
                if u not in pending:
                    pending.append(u)
            settings["pending_dependencies"] = pending
            config_manager.save_config_if_changed("custom_tools_settings", settings)
            
            return False, f"⚠️ セキュリティ保護のため、以下の新しい依存関係（バージョン指定含む）にはユーザーの承認が必要です: {', '.join(unauthorized)}。拡張ツール設定画面で承認してください。"

        # 2. 承認済みパッケージのインストール実行
        installed_results = []
        base_dir = os.path.dirname(os.path.abspath(__file__))
        uv_path = os.path.join(base_dir, ".venv", "bin", "uv")
        if not os.path.exists(uv_path):
            uv_path = "uv"
            
        for dep in deps:
            # インストール済みチェック（パッケージ名のみで判定）
            pkg_name = dep.split("==")[0].split(">")[0].split("<")[0].split("[")[0].strip()
            try:
                # 既にインポート可能かチェック
                importlib.import_module(pkg_name.replace("-", "_"))
                # バージョン指定がある場合は、インストール済みでも uv pip install を呼ぶことで
                # 指定バージョンへの固定（ダウングレード/アップグレード）を確実に実行させる
                if "==" not in dep:
                    continue
            except ImportError:
                pass

            try:
                # uv pip install --python <current_python> <package>
                subprocess.check_call([uv_path, "pip", "install", "--python", sys.executable, dep])
                installed_results.append(dep)
            except Exception:
                try:
                    subprocess.check_call([sys.executable, "-m", "pip", "install", dep])
                    installed_results.append(dep)
                except Exception as e2:
                    return False, f"Failed to install {dep}: {str(e2)}"
                    
        if installed_results:
            return True, f"✅ 承認済みパッケージをインストールしました: {', '.join(installed_results)}"
        return True, "All dependencies already satisfied"

if __name__ == "__main__":
    manager = CustomToolManager()
    loaded_tools = manager.get_all_custom_tools()
    print(f"Loaded {len(loaded_tools)} tools.")
    for t in loaded_tools:
        print(f" - {t.name}")
