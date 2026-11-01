# Nexus Ark Lite Relay

Lite独立お出かけモードの契約を、合成データだけでローカル／Cloudflare remote検証するための
Phase 0ハーネスと、Phase 1単一ペルソナMVP、Phase 2主要マルチプロバイダ対応、Phase 3料金／cacheの
Workerテンプレートを管理する。

Phase 5はWorker API schema 10、D1 `0010`、暗号化待機snapshot、`recovery_unconfirmed`障害開始、
owner診断、端末一覧・全失効、retention preview／run／Cron記録、strict CORSを実装する。
現行Nexus Ark Liteの`mobile_app/`をhome／travel共通UI正本とし、`npm run build:unified-lite`で
Worker Static Assetsを生成する。`public/`を直接編集して別実装を作らない。

Phase 1は実装と実機ゲートを完了している。端末ペアリング、snapshot登録、Gemini SSE会話、
二重送信防止、travel専用Lite画面、署名付き帰宅bundle、本体帰宅取込、本文保持期限を実装し、
専用Cloudflare Worker／D1とPC停止状態のスマホ10往復、重複なし帰宅統合を確認済みである。

Phase 2はパッケージ5まで完了している。資格情報プロファイルと冪等な`route_epoch`切替に加え、
Gemini、OpenAI Responses、Anthropic Messages、xAI Chat Completions、OpenRouter Chat Completionsを
同じ予約・非自動再送・atomic確定経路で扱う。OpenRouterは上流fallbackを無効化し、違反応答を
会話へ確定しない。公式モデル一覧と能力カタログの重ね合わせ、KV短時間cache、利用不能理由付きの
travel Lite切替UI、3経路結合シナリオ、本体の初期route設定と1件単位Secret登録導線、bundle v2の
署名export／検証／SYSTEM切替記録を実装済みである。既存ユーザー専用Worker／D1へPhase 2を配置し、
主要5社モデル一覧のlive／KV cache、直接3社＋OpenRouterの実会話、PC本体停止中のスマホでの
Gemini→xAI→OpenAI切替、欠番なし帰宅統合、本文削除、端末失効まで確認した。Anthropicは生成前の
既知失敗として会話へ確定しないことをremoteで確認した。

Phase 3のローカル実装は、版付き料金allowlist、価格不明の非0円化、日次／セッション予算予約、
Anthropic automatic cache、Gemini Explicit cacheの作成／再利用／削除、利用状況APIとtravel Lite表示、
署名bundle v3、本体料金台帳への`receipt_id`冪等取込を含む。remote migration、実API、スマホ確認は
ユーザーの明示許可後に行う。

## 安全境界

- `test/fixtures/`は合成データだけを含む。
- `.dev.vars`、`.env`、`.wrangler/`、`dist/`はGit管理外である。
- `.dev.vars.example`の値はplaceholderであり、実APIキーを記入しない。
- Phase 0検証では固定合成入力だけを使い、実ユーザーデータを送らない。
- remote検証endpointはランダムな`PHASE0_VALIDATION_TOKEN`で保護する。
- Phase 1所有者APIは`OWNER_AUTH_TOKEN`、帰宅bundleは`BUNDLE_SIGNING_KEY`で保護する。
- 待機snapshot本文は`STANDBY_ENCRYPTION_KEY`でAES-GCM暗号化する。通常の状態確認では本文をLiteへ返さず、
  device認証・外部送信への明示同意を伴う外部AI export時だけ、選択personaの許可項目を整形して返す。
- 外部AI export本文はbrowser storageとService Worker cacheへ保存せず、内部ID・経路・予算・秘密値を返さない。
- cross-origin接続は`LITE_ALLOWED_ORIGIN`と完全一致するoriginだけを許可する。
- 端末tokenはD1へSHA-256 hashだけを保存し、生tokenをログ、D1、Service Worker cacheへ入れない。
- `.dev.vars.example`のplaceholderを実運用へ流用しない。

## 必要環境

- Node.js 22以上（source checkoutの開発・検証専用）
- `package.json`／`package-lock.json`と、既存の`node_modules/wrangler`（Wrangler `4.118.0`）

WSL・ソース版でのNode pathの指定、Git checkoutと配布manifestの境界、`状態を確認`の表示、署名済みWindows x64版との違いは、正本の[Liteクラウド準備ガイド](../../docs/user_guide/07a_lite_cloud_setup.md)の「WSL・ソース版の開発用実行環境」を参照する。開発用Nodeは`NEXUS_ARK_LITE_DEV_NODE`へ絶対pathで明示し、Node/npmの自動導入やPATH fallbackを使わない。リポジトリ本体のPython実行環境や配布版runtimeへNode.js依存は追加しない。

開発用runtimeが準備済みでも、既存Worker、D1、KV、PWA資産を更新する操作ではない。Cloudflareや独立セッションを使う場合は、対象と計画を確認した明示操作を別途行う。D1 migrationの現行終点は`0010`である。

## ローカル検証

```bash
cd cloud/lite-relay
npm run build:unified-lite
npm run verify
npm run dry-run
```

上記は既存のローカル依存を使う明示的な開発・検証コマンドである。`npm install`やregistryからの自動導入を前提にしない。lockと実際のWranglerが`4.118.0`でない場合は、先に開発環境を整合させる。

検証内容:

- Gemini、OpenAI、Anthropic、xAI、OpenRouterの合成SSE正規化
- stream中断、xAI累積usage、OpenRouter実cost・上流・fallback違反
- D1 migration、並行予約、atomic commit、cursor欠番、本文削除、`outcome_unknown`
- 単回pairing code（本体では標準15分・5/15/30分を選択）、access／refresh tokenローテーション、端末失効
- snapshot allowlist、秘密情報／絶対パス拒否、署名付き帰宅bundle、即時／期限削除
- Secret canary除去、host完全一致、redirect拒否
- home/travelのcache・storage namespaceとAPI schema境界
- 5社の公式モデル一覧、能力filter、KV hit／stale／期限切れ／障害時のlive継続
- 3経路・2回切替時のroute epoch、event sequence、要求／解決model一致
- Worker APIカタログ、所有者／端末認証の分離、未認証endpointの非公開
- Phase 3料金内訳、未知価格、予算停止／保留、Gemini cache lifecycle、bundle v3台帳冪等性
- Phase 5待機世代、暗号化、単一activation、未確認分岐、owner診断、端末、retention、strict CORS
- 待機中／独立モード開始後の外部AI export、明示同意、persona境界、非永続・非cache

所有者専用 `POST /v1/pairing-codes` は任意のJSON `{"ttl_minutes": 15}` を受け付ける。
指定できる値は整数5・15・30。bodyなし・`{}` は旧クライアント互換の5分とし、応答には
`code`・`expires_at`・確定した `ttl_minutes` を返す。不正な値は発行前に400で拒否する。
コードは有効期限に達すると使えず、期限内も1回限り。登録済み端末の認証期限は別に扱う。

## Remote検証資源

- Worker: `nexus-ark-lite-relay-phase0`
- D1: `nexus-ark-lite-relay-phase0`（APAC）
- URL: `https://nexus-ark-lite-relay-phase0.nexusark.workers.dev`
- Static Assets: `index.html`、`manifest.webmanifest`、`service-worker.js`、`static/`

検証TokenはCloudflare Secretにだけ保存する。値を`.dev.vars`、Git、D1、ログ、レポートへ記録しない。
検証後の合成D1行は削除し、空件数を確認する。

資源を破棄する場合はWorkerを先に削除する。

```bash
wrangler delete nexus-ark-lite-relay-phase0
wrangler d1 delete nexus-ark-lite-relay-phase0 --skip-confirmation
```

Phase 0の実APIゲートが完了するまでは、再検証のため資源を維持する。

## Phase 1ユーザー専用Workerテンプレート

`wrangler.phase1.example.jsonc`を自分用の追跡対象外ファイルへコピーし、D1 ID、Worker名、
`TRAVEL_GEMINI_MODEL`を設定する。次の3値はCloudflare Secretへ1件ずつ登録し、設定ファイル、D1、
Lite、Gitへ書かない。

- `OWNER_AUTH_TOKEN`: 本体だけが使う所有者Token。
- `BUNDLE_SIGNING_KEY`: 本体で帰宅bundleを検証するHMAC鍵。
- `GEMINI_PERSONAL_1`: お出かけ中だけ使うGemini APIキー。

`OWNER_AUTH_TOKEN`と`BUNDLE_SIGNING_KEY`は十分な長さの別々のランダム値にする。本体の
「外部接続 → API Gateway / Lite → Lite独立お出かけモード設定」にはWorker URL、同じ所有者Token、
同じ署名鍵、同じモデルIDを保存する。全APIキーの一括同期は行わない。

Phase 1のremote配置は、対象資源と費用上限のユーザー許可後に専用設定で行う。Phase 0の
`wrangler.jsonc`をPhase 1用として上書きしない。実機ゲート済みの資源名と結果はPhase 1完了報告を参照する。

## Phase 2主要マルチプロバイダWorkerテンプレート

`wrangler.phase2.example.jsonc`を追跡対象外の自分用設定へコピーし、D1 ID、KV namespace ID、
Worker名を設定する。KV binding名は`MODEL_CATALOG_CACHE`を維持する。
`OWNER_AUTH_TOKEN`と`BUNDLE_SIGNING_KEY`に加え、利用するプロバイダのSecretをCloudflareへ
1件ずつ登録する。例には各社の第1スロットを記載しているが、使わないSecretを一括登録しない。

Phase 5ではさらに`STANDBY_ENCRYPTION_KEY`を32文字以上の独立したランダム値として登録し、
`LITE_ALLOWED_ORIGIN`へ現行LiteのHTTPS originを完全一致で設定する。provider Secretは任意であり、
利用するprofileに対応するものだけを登録する。

- `GEMINI_PERSONAL_1`
- `GEMINI_PERSONAL_2` / `GEMINI_PERSONAL_3`（任意の予備枠。初期構築の必須Secretにはしない）
- `OPENAI_PERSONAL_1`
- `ANTHROPIC_PERSONAL_1`
- `XAI_PERSONAL_1`
- `OPENROUTER_PERSONAL_1`

Secret値はD1の`provider_profiles`へ保存しない。D1にはallowlist済みのbinding IDだけを登録し、
端末向けAPIはbinding IDも返さない。モデルはsnapshot v2またはroute変更APIから指定し、Workerの
ソースコードや設定テンプレートへ運用モデル名を固定しない。

本体はSecret登録前に、所有者専用 `GET /v1/provider-profile-registration-options` で
登録枠・既存profile所有者を確認する。応答は秘密値を含まない。
予備枠未対応・profile競合・不正応答ではSecret送信を開始しない。
同じbindingを所有する既存custom profileの再登録は、そのIDを保ったまま行える。

端末向け`GET /v1/provider-profiles/{id}/models`は選択したprofileだけの公式一覧を取得し、
5分以内のKV cacheを再利用する。`?refresh=1`で明示更新できる。取得失敗時は1時間以内のstale cacheだけを
返し、それより古い一覧で新規選択を許可しない。KV自体の読み書きに失敗しても、公式APIのlive取得が
成功していれば一覧応答を継続する。

## Node versionと配布経路の境界

Wrangler 4.110以降はNode.js 22以上を必要とする。Node 18を使うためにWranglerや依存関係を古い版へ下げない。
WSL・ソース版での明示Node指定は、[Liteクラウド準備ガイド](../../docs/user_guide/07a_lite_cloud_setup.md)の「WSL・ソース版の開発用実行環境」に従う。
署名済みWindows x64配布版は同梱runtimeを使い、利用者がNode.jsやnpmを選択する経路ではない。
