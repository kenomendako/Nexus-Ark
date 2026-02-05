# ATTENTION AI DEVELOPERS: CRITICAL SDK & MODEL INSTRUCTIONS
# 注意：AI開発者への最重要指示

## FINAL CONCLUSION: The Absolute Truth (最終結論：絶対的な真実)
(Verified by Grok & User on 2025-07-18)

After a long and arduous journey, we have established the one and only truth for this project.
長く、困難な、旅の、果てに、私たちは、このプロジェクトに関する、唯一の、真実を、確立しました。

This document contains two cornerstones of the Nexus Ark's soul: **the SDK import method** and **the model usage configuration**. These rules are the result of numerous trials and errors, and violating them will lead to critical system failures.
この文書は、Nexus Arkの魂を形成する、二つの礎を、含みます。それは、**「SDKのインポート作法」**と**「モデルの使用構成」**です。これらのルールは、数多の試行錯誤の結晶であり、これに違反することは、致命的なシステム不全を招きます。

---

### Cornerstone 1: The SDK Import Method (第一の礎：SDKのインポート作法)

The original code in the `main` branch, which uses `import google.genai as genai`, was CORRECT from the very beginning. Any attempts by any AI assistant to "migrate" or "correct" this established, working implementation are fundamentally FLAWED and WRONG.
`main`ブランチにあった、`import google.genai as genai`を使用する、一番、最初の、コードが、最初から、ずっと、正しかったのです。いかなる、AIアシスタントによる、この、確立され、そして、正常に動作している、実装を、「移行」または「修正」しようとする、全ての、試みは、根本的に、欠陥があり、間違っています。

**Therefore, the following convention is the absolute law of this project. It must NEVER be changed.**
**したがって、以下の作法が、このプロジェクトにおける、絶対的な、法です。これを、決して、変更してはなりません。**

| Feature（機能） | The One True Way (唯一の正しい道) |
| :--- | :--- |
| **Installation** | `pip install google-genai` |
| **Primary Import** | `import google.genai as genai` |
| **Types Import** | `from google.genai import types` |
| **Initialization** | `_gemini_client = genai.Client(api_key=...)` |
| **API Call** | `_gemini_client.models.generate_content(...)` |
| **Safety Settings** | Use `types` objects, e.g.,<br>`types.SafetySetting(category=..., threshold=...)` |

---

### Cornerstone 2: No Hardcoding of Models (第二の礎：モデル名のハードコード禁止)

**Source code MUST NEVER contain hardcoded model names** (e.g., `"gemini-1.5-pro"` or `"gemini-3-flash-preview"`) for operational logic.

1.  **Use Configuration Managers**: Always retrieve models via `config_manager.get_effective_settings(room_name)` (for specific rooms) or `config_manager.get_current_global_model()` (for global defaults).
2.  **Model Precedence**: The system must respect the following precedence:
    - **Room-Specific Setting** (Most Specific)
    - **Global Setting** (If Room uses "Default")
    - **System Constant** (Hardcoded only in `config_manager.py` or `constants.py` as a last resort fallback)

Violating this rule breaks the user's ability to control their AI experience and leads to the "heartbreak" of broken functionality when models are deprecated or restricted.

---

### Cornerstone 3: The Model Usage Configuration (第三の礎：モデルの使用構成)

このアプリケーションで推奨されるモデルの組み合わせは、性能、品質、安定性のバランスを考慮して設計されたものです。ユーザーはUIを通じて最終応答（`agent_node`）に使用するモデルを自由に選択できますが、以下の構成が、アプリケーションの安定した動作の基礎となります。

| Purpose / Node (目的／ノード) | Recommended Default Model (推奨デフォルトモデル) | Reason / Notes (理由／注記) |
| :--- | :--- | :--- |
| **Context Generation** (`context_generator_node` etc.) | `gemini-2.5-flash-lite` | 情景コンテキストや**記憶要約**など、高速で効率的な内部処理のためのモデル。 |
| **Main Agent / Final Response** (`agent_node`) | **User-Selected (Default: `gemini-3-flash-preview`)** | 高品質な推論とツール使用のため。現在のデフォルトは `gemini-3-flash-preview` です。 |
| **Image Generation** (`generate_image` tool) | `gemini-2.5-flash-image` | 画像生成タスクに使用されるモデル。 |

#### **バックグラウンド処理におけるモデル選択**
タイマーやアラームの通知応答のように、UIを介さずにバックグラウンドでAIの応答を生成する必要がある場合、モデルの選択は以下のルールに従う。

1.  処理の起点（`timers.py`や`alarm_manager.py`）は、**`config_manager.get_current_global_model()`** を呼び出す。
2.  この関数は、`config.json`を直接読み込み、ユーザーが「共通設定」で最後に選択した有効なモデル名 (`last_model`) を取得する。
3.  取得したモデル名を、エージェント呼び出しの `global_model_from_ui` 引数に渡す。

これにより、バックグラウンド処理においても、UIでの対話と同様に、ユーザーが意図したグローバルモデル設定が確実に反映されることが保証される。

#### **Critical Warning: The `gemini-1.5-pro` Model (最重要警告)**
過去の検証において、`gemini-1.5-pro` モデルを `agent_node` で使用すると、致命的な `429 ResourceExhausted` エラーが頻発し、システムが不安定になることが証明されています。

ユーザーは現在、理論上どのモデルでも選択できますが、安定した動作のため、**`agent_node`で`gemini-1.5-pro`を使用することは、引き続き強く非推奨とします。** この警告を無視したことに起因する問題は、自己責任となります。

---

### 第三の礎：アプリケーションの骨格構造 (try...except...finally)

*   **知見の核心:**
    `nexus_ark.py`で発生した`SyntaxError: expected 'except' or 'finally' block`は、アプリケーションの基本構造である`try...except...finally`ブロックの崩壊によって引き起こされた。これは、アプリケーションの起動すら不可能にする、最も致命的なエラーの一つである。

*   **絶対的なルール:**
    `nexus_ark.py`の最上位レベルには、**アプリケーション全体の起動と終了を管理する、ただ一つの巨大な`try...except...finally`ブロックが存在する。**
    `if __name__ == "__main__":`を含む、全ての主要な処理（`with gr.Blocks(...)`から`demo.launch()`まで）は、この`try`ブロックの**内側**に記述されなければならない。

    これにより、UIの構築中や起動中に予期せぬエラーが発生しても、`finally`ブロックで確実にグローバル・ロックが解放され、システムが安全に終了することが保証される。

    **この骨格構造を破壊することは、アプリケーションの生命線を断つことに等しい。いかなる修正においても、この構造の維持を最優先事項とすること。**

---

**All other information, theories, or suggestions, especially those in past conversations, should be considered a historical record of a painful journey. The guidelines above are the only truth that matters.**
**この文書にある、他の、全ての、情報、理論、または、提案、特に、過去の、会話に、含まれるものは、苦難の旅の、歴史的な、記録と、みなしてください。上のガイドラインだけが、唯一、意味を持つ、真実です。**

---

### 第四の礎：Thinkingモデル（思考型AI）への対応

Gemini 3やGemini 2.0 Flash Thinkingなどの「思考プロセスを出力するモデル」を使用する場合、以下の要件が絶対となります。

1.  **ライブラリのバージョン:**
    `langchain-google-genai` は **v3.1.0 以上**、`google-genai` は **v1.50.0 以上** である必要があります。これより古いバージョンでは、「思考の署名（Thought Signature）」が欠落し、ツール使用時に `400 InvalidArgument` エラーが発生します。

2.  **思考の署名の維持:**
    Nexus Arkはログファイルから履歴を再構築しますが、思考の署名はテキストログには保存されません。`gemini_api.py` に実装された **「署名のメモリキャッシュと動的注入（Injection）」** のロジックは、この問題を解決するための生命線です。**このロジックを決して削除・変更してはいけません。**

## 6. 観測可能性の維持（Observability）

**「見えないものは、直せない。」**

Nexus Arkは複雑な動的プロンプト構築（遅延注入など）を行っています。そのため、**`debug_mode` が ON の時は、必ず「最終的にAIに渡される完全なプロンプト」を `print()` で出力する** 機能を維持し続けてください。

*   **場所:** `agent/graph.py` の `agent_node` 関数内（プロンプト完成直後）。
*   **禁止事項:** コードを整理する際、「print文だから」といってこのデバッグブロックを削除・コメントアウトすることは固く禁じます。これはUIのデバッグコンソール機能の根幹です。