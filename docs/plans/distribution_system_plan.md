# 📦 Nexus Ark 配布・更新システム実装計画 (Modern Distribution Strategy)

## 🎯 ゴール
調査レポートに基づき、**uv**（高速パッケージ管理）と **Pinokio**（AIブラウザ）を活用した、現代的でユーザー負担の少ない配布システムを構築します。
また、案内役「オリヴェ」の記憶（ユーザーとの思い出）を守りつつ、彼女の知識（仕様書）だけを最新に保つ仕組みを導入します。

## ⚠️ ユーザーレビュー必須事項
> [!IMPORTANT]
> **データ保護と更新の共存**
> - **Pinokio/Git更新**を利用するため、コードベースは常に最新に同期されます。
> - **ユーザーデータ保護**: `characters/` フォルダなどは `.gitignore` で管理済みですが、更新時にこれらが上書きされないことを二重に担保します。
> - **配布リポジトリの分離**: 開発用リポジトリとは別に、配布用リポジトリ `kenomendako/Nexus-Ark` を使用します。これに伴い、「リリースビルド」のプロセスを定義します。

## 🔧 提案する変更内容

### 0. リリースパイプライン (配布用ビルド)
**[NEW] [tools/build_release.py](file:///home/baken/nexus_ark/tools/build_release.py)**
開発環境から不要なファイルを除外して、配布用パッケージを作成するスクリプトです。

- **機能**:
    - Project Rootのファイルを走査し、ホワイトリスト/ブラックリストに基づいてコピー。
    - **除外対象**: `docs/` (仕様書以外), `tests/`, `outing/`, `.agent/`, `.git/`, `__pycache__/` 等。
    - **キャラクター制限**: `characters/` 内は **「オリヴェ」以外全て除外** します。
    - **同梱対象**: `nexus_ark.py`, `agent/`, `characters/オリヴェ/`, `utils/`, `static/`, `themes/`, `pinokio.js` 等。
    - `version.json` の自動生成/更新。
- **運用**: 開発完了後、このスクリプトを実行 → 生成されたファイルを `kenomendako/Nexus-Ark` にプッシュ。

### 0.5. 配布用仕様書の整備
**[NEW] [docs/NEXUS_ARK_SPECIFICATION.md](file:///home/baken/nexus_ark/docs/NEXUS_ARK_SPECIFICATION.md)**
オリヴェの知識更新ソースとなる公式仕様書を作成します。
現在の `README.md` や `docs/` 内の情報を集約し、オリヴェがユーザに説明しやすい形式で記述します。

### 1. 環境管理の刷新 (`uv` 導入)
従来の `pip` + `venv` ではなく、Rust製の高速ツール `uv` を採用します。

#### [NEW] [pyproject.toml](file:///home/baken/nexus_ark/pyproject.toml)
- プロジェクトの依存関係を現代的な形式で定義。
- これにより、Windows/Mac/Linux 問わず、コマンド一発で同一の環境を再現可能になります。

#### [DELETE] [requirements.txt]
- `pyproject.toml` に移行するため、将来的に削除（互換性のために当面は残すか、uvから自動生成する運用にします）。

### 2. 配布プラットフォーム対応 (Pinokio)
ユーザーはPinokioブラウザで「Download」を押すだけで導入できるようになります。

#### [NEW] [pinokio.js] (ルートディレクトリ)
- Pinokio用の構成ファイル。
- `uv` の自動インストール、依存関係の解決、アプリ起動を定義します。
- **Menu構成**:
  - 🚀 **Start**: アプリ起動
  - 🔄 **Update**: `git pull` + `uv sync` + 知識同期
  - 📂 **Open Folder**: フォルダを開く

### 3. アプリケーションロジック (知識同期)

#### [NEW] [update_manager.py](file:///home/baken/nexus_ark/update_manager.py)
- アプリ起動時に呼び出されるユーティリティ。
- **機能1: スマート初期化 (Fresh Install)**
  - `characters/オリヴェ` が存在しない場合 (新規):
    - `assets/sample_persona/Olivie` を `characters/オリヴェ` にコピーします。
    - これにより、新規ユーザーは「初めまして」の状態から開始できます。
- **機能2: 知識同期 (Update)**
  - `characters/オリヴェ` が既に存在する場合 (既存):
    - `docs/NEXUS_ARK_SPECIFICATION.md` (最新) を `characters/オリヴェ/knowledge/` に上書きコピーします。
    - **重要**: ログ (`log.txt`) や画像、その他のファイルは**一切変更しません**。
    - **重要**: `characters/` 内の他のフォルダ（ユーザーが作成したペルソナ）には**一切触れません**。
  - （ログには「知識をアップデートしました」と記録を残す）

#### [MODIFY] [nexus_ark.py](file:///home/baken/nexus_ark/nexus_ark.py)
- 起動シーケンスの冒頭に `update_manager.sync_knowledge()` を追加。
- これにより、更新直後の初回起動で自動的にオリヴェが賢くなります。

### 4. オンボーディングシステム刷新
ユーザーの状態に合わせて適切な初期設定フローを提供します。

#### [NEW] [onboarding_manager.py](file:///home/baken/nexus_ark/onboarding_manager.py)
- **`check_status()`**: ユーザー状態を判定します。
  - **New User**: `config.json` がない、または `setup_completed` フラグが false。
  - **Migrated User**: `config.json` はあるが、`version` が古い。
  - **Active User**: 設定済み。

#### [MODIFY] [nexus_ark.py](file:///home/baken/nexus_ark/nexus_ark.py) (UI)
- 起動時に `onboarding_manager` をチェック。
- **オンボーディングモードの場合**:
  - メイン画面の上に **「初期設定ウィザード」** オーバーレイを表示します。
  - **Step 1: ようこそ** (オリヴェからの挨拶)
  - **Step 2: APIキー設定** (Gemini / OpenAI などを入力・検証)
  - **Step 3: 完了** (設定保存 & スタート)

## ✅ 検証計画

### 自動テスト
- `update_manager.py` の単体テストを作成し、ファイルコピー動作と例外処理（ファイルが開かれている場合など）を確認します。

### 手動検証
1. **Pinokioインストール**:
   - クリーン環境でPinokio経由のインストールが成功するか。
2. **知識同期テスト**:
   - オリヴェの知識ファイルを意図的に古い内容に書き換える。
   - アプリを再起動。
   - 知識ファイルが最新の仕様書に戻っていることを確認。
   - 同時に、会話ログ（`log.txt`）が**消えていない**ことを確認。
