---
name: release-management
description: Nexus Arkの配布用パッケージビルドと公開リポジトリへのデプロイを管理するためのスキル
---

# Release Management Skill

Nexus Arkの配布版（Distribution Package）を作成し、公開用リポジトリへデプロイするための手順とチェックリスト。

## 前提条件

- 開発用リポジトリ: `kenomendako/gradiotest` (Private/Dev)
- 公開用リポジトリ: `kenomendako/Nexus-Ark` (Public/Dist) ※開発用とは別のリモートURLを設定する必要がある
- ビルドツール: `tools/build_release.py`, `tools/zip_dist.py`
- UV環境: `uv` コマンドが利用可能であること

## リリース手順

### 1. ビルド前チェック

- [ ] **バージョン更新**: `version.json` のバージョン番号とリリース日を更新する。
- [ ] **CHANGELOG**: `CHANGELOG.md` に最新の変更履歴を記載する。
- [ ] **ログレベル確認**: `nexus_ark.py` のロギング設定を確認する。
    - **重要**: `root` レベルは `INFO` に設定すること。`DEBUG` にするとWindows環境でディスクIOエラー（`OSError: [Errno 28] No space left on device`）の原因なる。
    - 特に `PIL` などの外部ライブラリは `WARNING` に抑制する。

### 2. パッケージビルド

```bash
# クリーンビルドとZIP作成
python3 tools/build_release.py
python3 tools/zip_dist.py
```

- **成果物**: `dist/` ディレクトリと `NexusArk_x.x.x.zip`
- **構造確認**: `dist/` 直下に `README.md`, `Start.bat` 等があり、ソースコードは `app/` 以下に格納されていること（Two-tier structure）。

### 3. README画像リンクの確認

- `tools/build_release.py` は、`README_DIST.md` を `dist/README.md` にコピーする際、画像パスを自動的に `app/assets/` 経由に書き換える機能を持っている。
- `dist/README.md` を開き、画像リンクが `![Alt](./app/assets/images/...)` のようになっているか確認する。

### 4. 公開リポジトリへのデプロイ

**⚠️ 注意**: `dist` フォルダは開発用リポジトリでは `.gitignore` されている。公開用リポジトリには `dist` の中身だけをプッシュする。

```bash
cd dist

# 初回/リセット時（推奨）: 新規リポジトリとして初期化して強制プッシュ
rm -rf .git
git init
git checkout -b main
git config user.email "your_email@example.com"
git config user.name "Your Name"
git add .
git commit -m "Release x.x.x"

# 公開用リモートを追加（URLに注意！）
git remote add origin https://github.com/kenomendako/Nexus-Ark.git

# 強制プッシュ（履歴を上書き）
git push -f origin main
```

### 5. リリースノート作成 (GitHub)

- GitHubの Releases ページで `Draft a new release` を選択。
- タグバージョンを作成（例: `v0.2.0`）。
- 生成された `NexusArk_x.x.x.zip` を添付する。

## トラブルシューティング

### Q. GitHubのトップページで画像が表示されない
- **原因 1**: 画像パスが `assets/...` のままで、`app/assets/...` になっていない。
    - **対策**: `tools/build_release.py` の置換ロジックが働いているか確認。
- **原因 2**: リモートURLが間違っている（テスト用リポジトリに投げている）。
    - **対策**: `cd dist && git remote -v` で確認。

### Q. Windowsで起動しない・クラッシュする
- **原因**: ログ出力が多すぎる。
    - **対策**: `nexus_ark.py` のログ設定を `INFO` に戻して再ビルド。

### Q. 公開リポジトリに余計なファイル（.agentなど）が混じった
- **原因**: 親ディレクトリ（開発環境）のGit情報を引き継いでプッシュしてしまった。
- **対策**: `dist` 内の `.git` を削除し、`git init` からやり直して強制プッシュする。
