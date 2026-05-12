import os
from langchain_core.tools import tool

@tool
def create_persona_tool(filename: str, code: str):
    """
    Pythonコードを書いて新しいツール（プラグイン）を作成し、AI自身の能力を拡張します。
    作成されたツールは即座にAIが利用可能になります。
    
    引数:
    - filename: 作成するファイル名（例: 'weather_service.py'）。必ず .py で終わる必要があります。
    - code: @tool デコレータを使用した Python ソースコード。
    
    開発ガイドライン:
    - **依存関係の管理**: 外部ライブラリが必要な場合、コードの冒頭に `# dependencies: パッケージ名==バージョン` と記述してください（例: `# dependencies: requests==2.31.0`）。セキュリティのため、可能な限りバージョンを固定して記述することが推奨されます。
    - **ルーム名と居住空間の区別**: Nexus Ark における `room_name` はシステム上のチャットルーム識別名（例: 'オリヴェ'）です。あなたが実際に位置している仮想空間内の特定の部屋や地名を扱う場合は、引数名を `location_name` とし、システム名と混同しないようにロジックを組んでください。
    - langchain_core.tools から tool をインポートして使用してください。
    - 保存前に自動的に構文チェックが行われます。エラーがある場合は作成に失敗し、修正を求められます。
    """
    if not filename.endswith(".py"):
        filename += ".py"
    
    from custom_tool_manager import CustomToolManager
    
    # 1. 構文チェック
    is_valid, err_msg = CustomToolManager.validate_code(code)
    if not is_valid:
        return f"❌ コードに構文エラーが見つかりました。修正してください:\n{err_msg}"
    
    # 2. 依存関係のチェックとインストール
    deps = CustomToolManager.get_dependencies(code)
    if deps:
        success, dep_msg = CustomToolManager.install_dependencies(deps)
        if not success:
            return f"❌ 依存関係のインストールに失敗しました:\n{dep_msg}"
        print(f"--- [AI Tool Creation] {dep_msg} ---")

    plugin_dir = "custom_tools"
    os.makedirs(plugin_dir, exist_ok=True)
    file_path = os.path.join(plugin_dir, filename)
    
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)
            
        # キャッシュをクリアして即座に認識させる
        CustomToolManager.clear_mcp_cache()
        
        return f"✅ 新しいツール '{filename}' の作成に成功しました。AIはこの新しい能力をすぐに利用できます。"
    except Exception as e:
        return f"❌ ツールの保存中にエラーが発生しました: {str(e)}"
