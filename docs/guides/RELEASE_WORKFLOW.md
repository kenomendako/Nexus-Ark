# Nexus Ark Release Workflow Guide

このドキュメントでは、Nexus Arkの新しいバージョンをリリースする際の詳細な手順を解説します。

## 概要

Nexus Arkのリリースは以下の2つの成果物を作成することを目的とします。
1. **GitHub公開リポジトリ (`kenomendako/Nexus-Ark`) の更新**: ソースコードの公開
   (※開発用リポジトリ `kenomendako/gradiotest` とは別物です)
2. **配布用ZIPファイル (`NexusArk_x.x.x.zip`) の作成**: ユーザーがダウンロードして実行するパッケージ

## 🛠️ 事前準備

### 環境確認
- `uv` コマンドがインストールされていること。
- `git` コマンドが使用可能であること。
- **重要**: 開発用リポジトリと公開用リポジトリはリモートURLが異なります。混同しないよう注意してください。

## 📦 ビルド手順

### 1. バージョン情報の更新
`app/version.json` (またはルートの `version.json`) を編集し、バージョン番号を更新します。

```json
{
    "version": "0.3.0",  <-- ここを更新
    "release_date": "2026-03-01",
    ...
}
```

### 2. CHANGELOGの更新
`CHANGELOG.md` に新機能、修正点、既知の問題を追記します。

### 3. ロギング設定の確認 (重要!)
`nexus_ark.py` の `LOGGING_CONFIG` を確認してください。
配布版（特にWindows）では、デバッグログが多すぎるとディスク書き込みが追いつかず、起動時にクラッシュ (`OSError: No space left on device`) することがあります。

- **Root Logger**: `INFO` レベル推奨
- **PIL / urllib3**: `WARNING` レベル推奨

### 4. ビルドスクリプトの実行
プロジェクトルートで以下を実行します。

```bash
python3 tools/build_release.py
python3 tools/zip_dist.py
```

これらが完了すると、`dist/` ディレクトリとZIPファイルが生成されます。

## 🚀 デプロイ手順

### 公開リポジトリへのプッシュ
`dist/` ディレクトリ配下のみを公開リポジトリにプッシュします。
**注意**: 開発環境の `.git` 履歴を公開しないよう、`dist` 内で新規にGitを初期化します。

```bash
cd dist
rm -rf .git  # 古い履歴があれば削除
git init
git checkout -b main
git add .
git commit -m "Release バージョン名"
git remote add origin https://github.com/kenomendako/Nexus-Ark.git
git push -f origin main
```

### GitHub Releasesの作成
1. GitHubリポジトリの「Releases」ページへ移動
2. 「Draft a new release」をクリック
3. タグ（例: `v0.3.0`）を作成
4. 生成された `NexusArk_x.x.x.zip` を添付する。
5. "Publish release" をクリック

## 🐛 トラブルシューティングと知見

### Q. GitHubのREADME画像がリンク切れになる
**原因**: 配布版は `app/` フォルダの中にソースがある「2層構造」ですが、READMEはルートにあります。
**対策**: `tools/build_release.py` が自動的に `README_DIST.md` 内の画像リンク (`./assets/...`) を `app/assets/...` に書き換えて `dist/README.md` を生成します。この処理が正しく行われているか確認してください。

### Q. 公開リポジトリに開発用のファイル (.agent, docs/privateなど) が混ざった
**原因**: `dist` フォルダではなく、開発環境のルートからプッシュしてしまった可能性があります。
**対策**: 公開リポジトリに対して、正しい `dist` の内容で強制プッシュ（Force Push）を行い、履歴を上書きして消去してください。

---
**Last Updated**: 2026-02-10
