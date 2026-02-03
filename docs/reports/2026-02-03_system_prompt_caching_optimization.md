# System Prompt Caching Optimization Report

**Date:** 2026-02-03
**Author:** Antigravity
**Task:** API利用時のシステムプロンプトキャッシュ対策確認

## 概要
Gemini API の Context Caching (Automatic caching) の効率を最大化するため、システムプロンプト (`agent/prompts.py`) の構造を最適化しました。

## 変更内容

### `agent/prompts.py` の `CORE_PROMPT_TEMPLATE` 再構築
プロンプトの構成要素を「静的コンテンツ（キャッシュ有効）」が先頭、「動的コンテンツ（キャッシュ無効）」が後方になるように並べ替えました。

| 順序 | セクション | 内容 | キャッシュ挙動 |
| :--- | :--- | :--- | :--- |
| **1 (New)** | `<absolute_command>` | 基本ルール・作法 | **Hit (Static)** |
| **2 (New)** | `<world_laws>` | 世界の物理法則 | **Hit (Static)** |
| **3 (New)** | `<task_manual>` | タスク判断基準 | **Hit (Static)** |
| **4 (New)** | `<available_tools>` | ツール定義一覧 | **Hit (Static)** |
| 5 | `<persona_definition>` | キャラクター定義・**記憶・ノート** | Miss (Dynamic) |
| 6 | `<current_situation>` | 現在地・時刻・情景 | Miss (Dynamic) |
| 7 | `<retrieved_information>` | 検索結果 | Miss (Dynamic) |

※ 以前は静的コンテンツ（世界法則やツール定義）が動的コンテンツの後に配置されていたため、キャッシュが無効化されやすく、毎回全トークンが処理される構造でした。

## 検証結果
- **構文チェック**: `python -m py_compile agent/prompts.py` にてエラーなし。
- **機能維持**: プロンプトの要素自体は削除せず、順序のみ変更したため、AIの挙動（ルール遵守、ツール使用）に大きな悪影響はないと判断。
- **期待される効果**:
    - 入力トークン処理コストの削減（キャッシュヒット時）
    - 応答レイテンシの低下

## 備考
- キャラクター定義 (`<{character_prompt}`) は動的な記憶 (`{core_memory}`等) と同じブロックにあるため、後半に移動しました。これにより、キャラクター定義の微調整を行った場合でも、前半のルール部分はキャッシュが効き続けます。
