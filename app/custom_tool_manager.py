import os
import importlib.util
import inspect
import asyncio
import subprocess
import sys
import py_compile
import tempfile
from typing import List, Callable, Dict, Any, Tuple, Optional
import traceback
from langchain_core.tools import tool, BaseTool

_MCP_TOOLS_CACHE = None

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

    async def _fetch_tools_from_mcp_server(self, server_conf: Dict[str, Any]) -> List[Callable]:
        """
        特定の MCP サーバに接続し、ツールのリストを取得してラップする。
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from langchain_core.tools import StructuredTool

        tools = []
        
        if server_conf.get("type") != "stdio":
            # SSE等は現在未対応
            return []

        params = self._build_stdio_params(server_conf)

        try:
            async with stdio_client(params) as (read, write):
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
                        tools.append(lc_tool)
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
        from mcp.client.stdio import stdio_client

        params = self._build_stdio_params(server_conf)

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, args)
                    # 結果を文字列として結合
                    text_parts = [p.text for p in result.content if hasattr(p, 'text')]
                    return "\n".join(text_parts) if text_parts else str(result)
        except BaseException as e:
            return f"Error calling MCP tool '{tool_name}': {str(e)}"

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
