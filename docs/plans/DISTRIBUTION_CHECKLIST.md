# 📦 Nexus Ark 配布チェックリスト

配布準備および今後のリリースで必要な作業をまとめた永続的なドキュメントです。

---

## 🔧 配布前の必須作業

### 1. バージョン更新
- [x] `constants.py` の `APP_VERSION` を更新
- [x] `version.json` の `version` と `release_date` を更新

### 2. 開発ファイルのクリーンアップ
- [x] `pyproject.toml` への依存関係移行 ✅
- [x] `requirements.txt` の削除（uv移行完了後）
- [x] `start.sh` を uv 使用に更新（毎回インストール表示の解消）

### 3. 仕様書の更新
- [ ] `docs/NEXUS_ARK_SPECIFICATION.md` を現在の実装に合わせて更新
  - 追記が必要な主要機能：
    - ルームごとのCSSテーマ設定
    - Arousalベース記憶システム
    - 内省機能（目標・未解決の問い・夢日記）
    - 複数プロバイダ対応（Google、Zhipu、Groq、OpenAI互換、ローカルLLM）
    - 月次ログ分割管理
    - ウォッチリスト巡回機能
    - チェス機能
    - Geminiログインポート
    - Claude/ChatGPT複数スレッドインポート
- [ ] `docs/guides/NEXUS_ARK_SPECIFICATION.md` の重複を解消

### 4. 配布用サンプルペルソナ（オリヴェ）の整備
場所: `assets/sample_persona/Olivie/`

- [ ] `room_config.json` にテーマ設定を反映
- [ ] `rag_data/` に仕様書のRAGインデックスを事前構築
- [ ] `SystemPrompt.txt` の内容確認
- [ ] `log.txt` の初期内容確認（空 or 挨拶メッセージ）

### 5. ビルド & 検証
- [ ] `python tools/build_release.py` を実行
- [ ] `dist/` の内容を確認
  - 不要ファイルが含まれていないか
  - 必要ファイルが揃っているか
- [ ] クリーン環境での起動テスト

---

## 📝 仕様書更新の詳細ガイド

仕様書 (`docs/NEXUS_ARK_SPECIFICATION.md`) はオリヴェがユーザーの質問に答えるための知識ベースです。

### 更新時の注意点
1. **ユーザー視点で書く**: 技術的な実装詳細より「何ができるか」「どう使うか」を重視
2. **セクション構造を維持**: 既存の番号付きセクション構造に合わせる
3. **CHANGELOGを参照**: 未記載機能は `CHANGELOG.md` から確認

### 追記が必要なセクション案

```markdown
## 12. ルームテーマ設定
（ルームごとのカラーテーマ、背景画像、UIカスタマイズ）

## 13. 記憶の重要度（Arousal）システム
（感情的に重要な記憶の優先保持、時間減衰との関係）

## 14. 内省機能
（目標管理、未解決の問い、夢日記、動機システム）

## 15. 複数AIプロバイダ対応
（Google Gemini、Zhipu AI、Groq、OpenAI互換、ローカルLLM）

## 16. ログ管理の詳細
（月次分割、検索機能、ログアーカイブ）
```

---

## 🔄 今後のリリースで追加検討

- [ ] Pinokio環境での実機テスト
- [ ] tufup による安全な更新システム（将来）
- [ ] 自動更新チェック機能（将来）

---

## 📎 関連ドキュメント

- [配布システム計画](distribution_system_plan.md)
- [配布調査レポート](../research/配布調査.md)
- [配布システム更新レポート](../reports/2026-02-06_Distribution_System_Update.md)
