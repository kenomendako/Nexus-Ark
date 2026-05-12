# 🎮 Roblox × Nexus Ark クイックスタートガイド

Nexus ArkのAIをRoblox内のNPCとして動かすための手順をまとめています。

---

## ステップ1：Roblox側の準備

### 1-1. ゲームの作成と公開
1. **Roblox Studio** で新規プレースを作成し、「Robloxに公開」します。
2. [Creator Dashboard](https://create.roblox.com/dashboard/creations) でゲーム設定を開き、「**APIサービスへのStudioアクセスを有効にする**」をONにします。
3. 再度「Robloxに公開」して反映します。

### 1-2. Universe ID の取得
[Creator Dashboard](https://create.roblox.com/dashboard/credentials) でゲームを検索し、「**バーチャル空間ID**」をコピーします。
※ URLの `experiences/` の直後の数字でも確認できます。

### 1-3. Open Cloud APIキーの取得
1. [Creator Dashboard > Credentials](https://create.roblox.com/dashboard/credentials) で「**APIキーを作成**」をクリックします。
2. **API System**: 「**Messaging Service API**」を選択します。
3. **Restrict by Experience**: ONにして、自分のゲームを追加し、「**Publish**」操作を選択します。
4. 「保存してキーを生成」→ 表示されたキーを**必ずコピーして保存**（一度しか表示されません）。

### 1-4. NPCの配置
1. Roblox Studio の Explorer で `Workspace` に **Model** を追加し、名前を `NexusArkNPC` にします。
2. 「アバター」タブ → 「リグの構築」 → **R15 ブロック型アバター** でダミーを配置します。
3. ダミー（`Rig`）を `NexusArkNPC` の中にドラッグし、`HumanoidRootPart` の **Anchored** をONにします。

### 1-5. スクリプトの設置
1. `ServerScriptService` に **Script** を追加し、以下の内容を貼り付けます:
   - 📄 [roblox_integrated_script_v3.lua](../guides/roblox_integrated_script_v3.lua)
2. 冒頭の `WEBHOOK_URL`, `WEBHOOK_SECRET`, `ROBLOX_TOPIC` をNexus Arkの設定値に合わせます。

> **プレイヤー建築ツール**（任意）: `StarterPlayerScripts` に [roblox_player_build_tool.lua](../guides/roblox_player_build_tool.lua) を追加すると、クリックで直接建築できます。

---

## ステップ2：Nexus Ark側の設定

上部の「外部接続」タブ → 「🎮 Roblox」を開きます。

| 設定項目 | 入力内容 |
|---------|---------|
| **API キー** | ステップ1-3で取得したキー |
| **Universe ID** | ステップ1-2で取得したID |
| **Topic** | `NexusArkCommands`（スクリプトと一致させる） |

「💾 このルームのROBLOX設定を保存」→「🔌 接続テスト」で `✅ 接続成功！` を確認します。

---

## ステップ3：Webhook連携（双方向通信）

Roblox側からのイベント（チャット・接近など）をNexus Arkで受信するための設定です。

### 3-1. なぜ必要？
Roblox Studio は `localhost` への直接通信をブロックするため、**Cloudflare Tunnel** でローカルサーバーを外部公開します。

### 3-2. Quick Tunnel（テスト用・URL変動）
```bash
# Nexus Arkディレクトリで実行:
bash scripts/start_webhook_tunnel.sh
```
表示される `https://xxxx.trycloudflare.com` がWebhook URLです。

> **手動実行**: `cloudflared tunnel --url http://127.0.0.1:7861`

### 3-3. 固定URL（Persistent Tunnel）
独自ドメインが必要です。[Cloudflare Zero Trust > Tunnels](https://dash.cloudflare.com/) で設定してください。
- Service Type: `HTTP`, URL: `127.0.0.1:7861`

### 3-4. Nexus ArkでWebhook設定を保存
1. 「Webhookドメイン」欄にトンネルURL（`https://xxxx.trycloudflare.com`）を入力し、横の「💾 URL保存」を押します。
2. 表示された **Webhook Secret Token** をコピーします。

### 3-5. Robloxスクリプトを更新
Luaスクリプト冒頭の設定を更新し、「Robloxに公開」します:
```lua
local WEBHOOK_URL = "https://xxxx.trycloudflare.com/api/roblox/event"
local WEBHOOK_SECRET = "ここにトークンを貼り付け"
```

> ⚠️ URLの末尾に必ず `/api/roblox/event` を追加してください。

### 3-6. HTTP要求の許可
Roblox Studio → 「ファイル」→「バーチャル空間設定」→「セキュリティ」→「**HTTP要求を許可**」をONにします。

---

## テスト方法

> ⚠️ Roblox Studio の「プレイ」ボタンではメッセージを受信できません。**Robloxクライアントからゲームに参加**してください。

1. Robloxクライアントでゲームに参加します。
2. **F9キー** → Developer Console → Serverタブで `[NexusArk] ✅ コマンドハンドラ起動完了` を確認します。
3. Nexus Arkのチャットで「Robloxでジャンプして」などと指示します。

---

## トラブルシューティング

| 症状 | 原因と対処 |
|------|-----------|
| `HttpService is not allowed` | 「バーチャル空間設定」→「セキュリティ」→「HTTP要求を許可」をON |
| NPCが反応しない | Roblox Studio ではなくクライアントでテスト / NPCの構造確認 / Tunnel起動確認 |
| `502 Bad Gateway` | Nexus Arkが起動していない（ポート7861で待機中か確認） |
| `No room found for this token` | Nexus ArkでWebhook設定を保存し、Secret Tokenをスクリプトに貼り付け |
| Quick TunnelのURL変更 | 再起動するたびにURL更新が必要。固定URLにはPersistent Tunnelを使用 |

---

> 💡 **詳細ガイド**: より詳しい手順は以下を参照してください。
> - [セットアップ詳細ガイド](roblox_setup_guide.md)
> - [Webhook外部公開ガイド](roblox_webhook_guide.md)
