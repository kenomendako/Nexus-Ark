# レポート: 配布・運用システムの刷新

**日付:** 2026-02-06  
**ブランチ:** `feature/distribution-system-update` (mainにて作業)  
**ステータス:** ✅ 完了

---

## 問題の概要

Nexus Ark の一般配布に向け、以下の課題を解決する必要があった：
1. **環境構築の複雑さ**: `pip install -r requirements.txt` では依存関係の衝突やPythonバージョンの不一致が起きやすい。
2. **初期設定のハードル**: 初回起動時に何をすればよいか（APIキー設定など）が分からず、離脱する可能性がある。
3. **継続的な更新**: 仕様書や知識ベースの更新を、ユーザーの手元へ確実に届ける仕組みがなかった。

---

## 実装・修正内容

### 1. パッケージ管理の刷新 (uv + pyproject.toml)
- `requirements.txt` を廃止し、PEP 621 準拠の `pyproject.toml` へ移行。
- 高速パッケージマネージャ `uv` を採用し、環境構築を高速化・安定化。
- クロスプラットフォーム起動スクリプト (`start_nexus_ark.sh`, `start_nexus_ark.bat`) を作成。

### 2. Pinokio 対応
- AIアプリケーションブラウザ "Pinokio" に対応するための定義ファイル群を作成。
  - `pinokio.js`: アプリケーション定義
  - `install.js`: ワンクリックインストーラー
  - `start.js`: 起動ランチャー
  - `update.js`: アップデータ

### 3. オンボーディングシステム (初期設定ウィザード)
- **`onboarding_manager.py`**: ユーザー状態（新規/既存/移行）を判定するロジックを実装。
- **UIオーバーレイ**: `nexus_ark.py` に `gr.Group` を用いたウィザード画面を追加。
  - APIキーの設定（Google Gemini推奨）を誘導。
  - 既存ユーザー（設定済み）の場合は自動的にスキップする移行ロジックを実装。

### 4. 知識同期ツール
- **`tools/update_knowledge.py`**: 本体仕様書 (`docs/NEXUS_ARK_SPECIFICATION.md`) を、オリヴェの知識ベース (`characters/Olivie/knowledge/`) へコピーするツール。
- 配布パッケージ作成時やアップデート時に実行することで、常に知識を最新に保つ。

---

## 変更したファイル

- `pyproject.toml` - [NEW] 依存関係定義
- `onboarding_manager.py` - [NEW] 状態判定ロジック
- `nexus_ark.py` - UI (ウィザード) 追加、起動時フック
- `tools/update_knowledge.py` - [NEW] 知識同期スクリプト
- `pinokio.js`, `install.js` 等 - [NEW] Pinokio対応
- `start_nexus_ark.sh` / `.bat` - [NEW] 起動スクリプト
- `tools/build_release.py` - [MODIFY] 配布ビルドロジック調整

---

## 検証結果

- [x] **アプリ起動確認**: `start_nexus_ark.sh` および `python nexus_ark.py` での正常起動を確認。
- [x] **オンボーディング動作確認**:
  - 新規状態: ウィザードが表示され、APIキー保存後に利用開始できることを確認。
  - 既存状態: 既存の `config.json` がある場合、自動的に検証・移行され、ウィザードが表示されないことを確認。
- [x] **知識同期**: ツール実行により仕様書がキャラクターフォルダへコピーされることを確認。
- [x] **UI副作用**: `python tools/validate_wiring.py` にて配線エラーなしを確認。

---

## 残課題

- **特になし**
  - Pinokio環境での実機テストはリリース後に行う想定だが、スクリプトの論理的な正しさは確認済み。
