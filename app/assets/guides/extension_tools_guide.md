# Nexus Ark 拡張ツール (Plugins & MCP) ガイド

Nexus Arkでは、Pythonスクリプトによる「ローカルプラグイン」と、標準規格「MCP (Model Context Protocol)」による外部ツールの統合が可能です。これらを利用することで、AIが天気を確認したり、スマートホームを操作したりといった、現実世界や外部サービスとの連携が可能になります。

---

## 📂 ローカルプラグイン

`custom_tools/` ディレクトリに Python スクリプトを追加するだけで、AIに新しい能力を授けることができます。

### 作成方法
1. `custom_tools/` フォルダ内に新しい `.py` ファイル（例: `hello_tool.py`）を作成します。
2. `langchain_core.tools` から `@tool` デコレータをインポートします。
3. 関数を定義し、`@tool` を付与します。**関数の説明（docstring）が重要です**。AIはこの説明を読んで、いつそのツールを使うべきか判断します。

### コード例
```python
from langchain_core.tools import tool

@tool
def get_current_time(location: str):
    \"\"\"指定された場所の現在時刻を返します。\"\"\"
    from datetime import datetime
    now = datetime.now().strftime("%H:%M:%S")
    return f"{location}の現在時刻は {now} です。"
```

### 反映方法
ファイルを保存した後、UIの「ローカルプラグイン」タブにある「🔄 再スキャン」ボタンを押すと、AIがツールを認識します。

---

## 🌐 MCP (Model Context Protocol) サーバ

MCPは、AIツールを外部プロセスとして分離して管理するための標準プロトコルです。既存のMCPサーバを利用したり、独自のサーバを接続したりできます。

### 設定方法
1. **種別**: ほとんどのローカルMCPサーバは `stdio` を使用します。
2. **コマンド**: サーバを起動するコマンド（例: `python`, `npx`, `uvx` など）を入力します。
3. **引数**: スクリプトのパスや設定ファイルをスペース区切りで入力します。

### 設定例 (天気予報 MCP)
Nexus Arkに同梱されているテスト用MCPサーバの設定例です。
- **名前**: `Weather`
- **種別**: `stdio`
- **コマンド**: `python` (Windowsの場合は `python` または `python.exe`、WSL/Linuxは `python3`)
- **引数**: `tools/weather_mcp_server.py`

> [!TIP]
> **Windowsでの実行のコツ**
> Windows環境では、`python` コマンドが `python3` ではなく `python` であることが一般的です。また、外部のMCPサーバ（`npx` 等を使用するもの）を実行する場合は、Node.js がインストールされ、環境変数 PATH が通っていることを確認してください。

### 接続テスト
設定を登録した後、一覧からそのサーバを選択し、「🔌 接続テスト」ボタンを押してください。成功すると、サーバが提供するツール一覧が表示されます。AIとの会話で「東京の天気を教えて」などと頼むと、このツールが呼び出されます。

---

## 💡 トラブルシューティング

- **ツールが認識されない**: 関数の説明（docstring）が書かれているか確認してください。
- **インポートエラー**: プラグイン内で使用するライブラリは、Nexus Arkが動作している環境にインストールされている必要があります。
- **MCP接続失敗**: コマンドやパスが正しいか確認してください。フルパスで指定すると確実です。
