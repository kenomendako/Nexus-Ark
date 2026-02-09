# README刷新と配布パッケージ構成の最終化レポート (2026-02-09)

## 概要
ユーザーフィードバックに基づき、`README.md` (GitHub用) と `README_DIST.md` (配布用) の内容を刷新・統一しました。
また、配布パッケージのフォルダ構成を見直し、ユーザーが迷わずに情報にアクセスできるよう改善しました。
これにより、Nexus Arkの一般公開（v0.2.1）に向けたドキュメント整備が完了しました。

## 変更内容

### 1. READMEの内容刷新と統一
*   **トーンの変更**: 「体験を提供する」といったマーケティング的な表現を避け、「生活し、記憶を蓄積する」というGroundedかつUser-centricな表現に統一。
*   **機能紹介の拡充**: 「長期記憶」「自律行動」「夢と内省」「変化する世界」に加え、「お出かけ機能（APIコスト回避と体験の統合）」のメリットを明記。
*   **免責事項の追加**: 個人開発である旨と、Mac/Linux環境の非公式サポート（動作保証外）についての注記を追加。
*   **インストール手順の整理**: 一般ユーザー向けのZip/Batch手順を最優先に配置。

### 2. 配布パッケージ構成の改善 (`tools/build_release.py`)
*   **ルートへのドキュメント配置**:
    *   `README.md` (完全版) を配布パッケージのルート (`dist/README.md`) に配置。
    *   `docs/NEXUS_ARK_SPECIFICATION.md` (詳細仕様書) をルート (`dist/NEXUS_ARK_SPECIFICATION.md`) にコピー。
*   **簡易版READMEの廃止**: `assets/launchers/README.md` を配布物から除外。
*   **参照パスの修正**: READMEから仕様書へのリンクを `./NEXUS_ARK_SPECIFICATION.md` に修正。

### 3. ドキュメント更新
*   `docs/plans/DISTRIBUTION_CHECKLIST.md`: README関連タスクを完了とし、Phase 3（将来課題）を次回以降に見送り。

## 検証結果
*   **ビルド検証**: `python tools/build_release.py` を実行し、`dist/` 以下の構成が意図通りであることを確認。
    *   `dist/README.md` が存在し、内容が正しいこと。
    *   `dist/NEXUS_ARK_SPECIFICATION.md` が存在すること。
*   **Markdown表示**: 各Markdownファイルのプレビューを確認し、リンク切れやレイアウト崩れがないことを確認。

## 次のステップ
*   リリースページ用のスクリーンショット撮影（ユーザー様担当）
*   v0.2.1 のリリースビルド作成と公開
