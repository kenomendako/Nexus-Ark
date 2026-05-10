# Roblox Webhook 外部公開ガイド

Nexus Ark の AI が Roblox 内の出来事（プレイヤーの接近やチャットなど）を認識するためには、Roblox サーバーから Nexus Ark の Webhook サーバーへイベントデータを送信する必要があります。

しかし、Roblox Studio の仕様上、**`localhost` (127.0.0.1) へのデータ送信はセキュリティ上の理由でブロックされます。**
そのため、ローカルの Nexus Ark サーバー（デフォルト: ポート `7861`）を一時的または恒久的にインターネット上に公開する（トンネルを通す）必要があります。

このガイドでは、最も安全で簡単な **Cloudflare Tunnel** を使用した外部公開の手順を解説します。

---

## 0. 前提条件：Roblox Studio の「HTTP要求を許可」設定

Roblox側のスクリプトから外部サーバーへHTTP通信を行うには、この設定を**必ず**有効にしてください。

> [!CAUTION]
> この設定がオフのままだと、スクリプトからの全てのHTTPリクエストがブロックされます。
> エラーメッセージ「`HttpService is not allowed to send HTTP requests`」が出る場合はこの設定を確認してください。

### 設定手順

1. **Roblox Studio** で該当のゲームを開きます。
2. 上部メニューバーの **「ファイル」** をクリックします。
3. メニュー下部にある **「バーチャル空間設定」** を選択します。

   > 💡 **「ゲーム設定」という項目は存在しません。** 日本語版では「バーチャル空間設定」と表記されています。

4. 左側メニューから **「セキュリティ」** を選択します。
5. **「HTTP要求を許可」** のトグルを **ON（緑）** にします。
6. 右下の **「保存」** をクリックして設定を保存します。

---

## 1. なぜ Cloudflare Tunnel なのか？

- **無料**で利用可能。
- **安全**: ローカルマシンのポートを直接開放（ポートフォワーディング）する必要がないため、セキュアです。
- 手軽に試せる「Quick Tunnel（アカウント不要）」と、固定URLで継続運用できる「Persistent Tunnel（アカウント・ドメイン要）」から選べます。

---

## 2. お手軽設定：Quick Tunnel (アカウント不要 / テスト用)

とりあえず少しだけ試してみたい場合に最適な方法です。
**注意**: トンネルを起動するたびに URL が変わるため、毎回 Roblox 側のスクリプト（`WEBHOOK_URL`）を書き換える必要があります。

### 支援スクリプトを使う方法 (Linux/macOS/WSL向け)

Nexus Ark のリポジトリに用意されているスクリプトを実行するだけで公開できます。

1. ターミナルを開き、Nexus Ark のディレクトリに移動します。
2. 以下のコマンドを実行します：
   ```bash
   bash scripts/start_webhook_tunnel.sh
   ```
3. スクリプトが `cloudflared` をダウンロード（または起動）し、トンネルを作成します。
4. ターミナル上に以下のような URL が表示されます。
   ```text
   +-------------------------------------------------------------+
   |  Your quick Tunnel has been created! Visit it at (it may    |
   |  take some time to be reachable):                           |
   |  https://xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.trycloudflare.com |
   +-------------------------------------------------------------+
   ```
   *これが現在あなたの Nexus Ark に繋がっている Webhook URL です。*

### 手動で実行する方法 (Windows等)

1. [Cloudflare の公式サイト](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) からご使用の OS に合った `cloudflared` をダウンロードします。
2. コマンドプロンプトや PowerShell を開き、以下のコマンドを実行します。
   ```bash
   cloudflared tunnel --url http://127.0.0.1:7861
   ```
3. 上記と同様に `https://*.trycloudflare.com` の URL が出力されます。

---

## 3. 本格運用：Persistent Tunnel (アカウント・ドメイン必須)

固定の URL を利用したい場合や、他人に遊ばせるゲームとして Roblox に公開する場合はこちらを設定します。

> [!IMPORTANT]
> **独自ドメインが必要です。**  
> Cloudflare Tunnel で固定 URL を作成するには、自分自身のドメイン（例：`example.com`）を所有しており、それを Cloudflare に登録している必要があります。ドメインを持っていない場合は、セクション2の「Quick Tunnel」を使い続けるか、新しくドメインを取得する必要があります。

1. [Cloudflare ダッシュボード](https://dash.cloudflare.com/) にログインします。
2. 左側メニューから **「Zero Trust」** を開きます（初回はサインアップが必要な場合があります）。
3. Zero Trust の左側メニューから **「Networks」** > **「Tunnels」** を開きます。
4. **「Create Tunnel」** をクリックし、「Cloudflared」を選びます。
5. トンネルに名前（例：`nexusark-webhook`）を付けます。
6. お使いの OS や環境に合わせて、表示されるインストール・起動コマンドをローカル環境で実行してコネクタを起動します。
7. ダッシュボードに戻り、ルートの設定を行います：
   - **「Routes」** タブから **「Add a route」** をクリック。
   - 選択肢から **「Published application」**（一番左上）を選択します。
   - **Public hostname**: 割り当てたいサブドメインとドメインを入力（例：`nexusark.yourdomain.com`）
   - **Service**: 
     - Type: `HTTP`
     - URL: `127.0.0.1:7861`
8. 保存すると、指定したドメイン `https://nexusark.yourdomain.com` があなたのローカルで動く Nexus Ark Webhook (7861番) に直結されます。

---

## 4. Roblox 側の設定更新

トンネルの URL（`trycloudflare.com` または独自のドメイン）を取得したら、それを Roblox 側に設定します。

1. Roblox Studio で該当のゲームを開き、`ServerScriptService` 内の統合スクリプト（`NexusArkIntegratedScript`）を開きます。

   > 📄 最新版のスクリプトは **[roblox_integrated_script_v3.lua](../guides/roblox_integrated_script_v3.lua)** です。
   > 古いバージョンを使用している場合は、中身を丸ごと差し替えてください。

2. 冒頭の設定セクションにある `WEBHOOK_URL` と `WEBHOOK_SECRET` を、取得した URL とトークンに置き換えます。

   > [!IMPORTANT]
   > **URL の末尾には必ず `/api/roblox/event` を追加してください！**  
   > Cloudflare Tunnel の URL だけでは、Nexus Ark のどこにデータを届けるか分かりません。

   **変更例:**
   ```lua
   -- 変更前 (エラーになります)
   -- local WEBHOOK_URL = "http://127.0.0.1:7861/api/roblox/event"
   
   -- 変更後 (Cloudflare Tunnel のアドレスを指定)
   local WEBHOOK_URL = "https://xxxxxxxx-xxxx.trycloudflare.com/api/roblox/event"
   local WEBHOOK_SECRET = "ここにNexus Arkで生成されたトークンを貼り付け"
   ```
3. スクリプトを保存し、**「ファイル」→「Roblox に公開（Publish to Roblox）」** を実行します。
4. 実際にゲームに参加し、NPCに近づいたりチャットを送ると、Nexus Ark のターミナルに `[Webhook] ルーム名 - プレイヤー xxx が発言しました` などのログが出れば成功です！

> [!NOTE]
> **プレイヤー建築ツール** を使用している場合は、プレイヤーが配置したブロックも空間認識データとしてAIに送信されます。
> プレイヤー建築ツールの設置手順は [セットアップガイド](roblox_setup_guide.md) のセクション 5-B を参照してください。

---

## 5. トラブルシューティング

### ❌ `Header "Content-Type" is not allowed!`

**原因**: Robloxの `HttpService:PostAsync` のヘッダーに `Content-Type` を手動で設定している。

**解決方法**: Luaスクリプト内の `headers` テーブルから `["Content-Type"]` の行を **削除** してください。
`PostAsync` の第3引数 (`Enum.HttpContentType.ApplicationJson`) が自動的に `Content-Type` を設定するため、重複指定はエラーになります。

```lua
-- ❌ エラーになる書き方
local headers = {
    ["Content-Type"] = "application/json",
    ["Authorization"] = "Bearer " .. WEBHOOK_SECRET
}

-- ✅ 正しい書き方
local headers = {
    ["Authorization"] = "Bearer " .. WEBHOOK_SECRET
}
```

### ❌ `HttpService is not allowed to send HTTP requests`

**原因**: Roblox Studio の HTTP要求許可設定がオフになっている。

**解決方法**: 本ガイドの「0. 前提条件」セクションに従って、「バーチャル空間設定」→「セキュリティ」→「HTTP要求を許可」をONにしてください。

### ❌ NPCに近づいても何も起きない

以下を順にチェックしてください：

1. **Roblox Studio ではなく、Roblox クライアント（ゲーム本体）でテストしていますか？**
   - Roblox Studio の「プレイ」ボタンでは Open Cloud Messaging API が動作しません。「Roblox に公開」した上で、Roblox アプリからゲームに参加してください。

2. **NPC の構造は正しいですか？**
   - NPCモデルの `HumanoidRootPart` が `NexusArkNPC > Rig > HumanoidRootPart` のようにネストされている場合、スクリプト内で `FindFirstChild("Rig")` 経由で検索する必要があります。

3. **Developer Console にログは出ていますか？**
   - ゲーム内で **F9キー** を押して Developer Console を開き、`[Nexus Ark Webhook]` から始まるログを確認してください。

4. **Cloudflare Tunnel は起動していますか？**
   - ターミナルで `curl https://your-url.trycloudflare.com/health` を実行し、`{"status":"ok"}` が返ることを確認してください。

5. **Nexus Ark で Webhook 設定を保存しましたか？**
   - Nexus Ark の UI で「設定」→「個別」→「🎮 ROBLOX連携」のWebhookドメインとSecret Tokenを保存してください。

### ❌ `502 Bad Gateway`

**原因**: Cloudflare Tunnel は起動しているが、Nexus Ark が起動していない（またはポート 7861 で待機していない）。

**解決方法**: Nexus Ark を起動してください。ターミナルに `[Roblox Webhook] ポート 7861 で待機中。` と表示されていることを確認してください。PCを再起動した場合は Cloudflare Tunnel も再起動が必要です（Quick Tunnel の場合は URL が変わります）。

### ❌ `Webhook is not configured for this room` / `No room found for this token`

**原因**: Nexus Ark 側でルームの Webhook 設定が保存されていない、またはスクリプト内の `WEBHOOK_SECRET` がNexus Ark上のトークンと一致していない。

**解決方法**: 
1. Nexus Ark UI で「設定」→「個別」→「🎮 ROBLOX連携」を開く
2. 「Webhook URLを保存」ボタンを押して設定を保存する
3. 表示された **Webhook Secret Token** をコピーし、Roblox スクリプトの `WEBHOOK_SECRET` に貼り付ける

---

## 6. Quick Tunnel 利用時の注意（URL の変更について）

> [!WARNING]
> Quick Tunnel は**起動するたびにURLが変わります。**  
> PC再起動やターミナルを閉じた後は、新しいURLをRobloxスクリプトに再設定する必要があります。
> 
> この手間を省きたい場合は、セクション3の「Persistent Tunnel」を検討してください。
