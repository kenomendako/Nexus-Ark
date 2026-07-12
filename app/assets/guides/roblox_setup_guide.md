# Roblox × Nexus Ark 連携セットアップガイド

（Open Cloud API V2 / 最新Roblox Studio UI対応版）

このガイドでは、Nexus ArkのAIエンティティがRoblox内のNPCを操作し、空間を共に共有できるようにするための初期セットアップ（Phase 1）の手順を解説します。

---

## 1. Robloxプレースの作成と公開

まずはNPCを配置するためのベースとなるゲーム（プレース）を作成します。

1. **Roblox Studio** を開き、「Baseplate」等のテンプレートを使って新規プレースを作成します。
2. 画面左上の「ファイル（File）」メニューから **「Robloxに公開（Publish to Roblox）」** を選択し、ゲーム名をつけてRobloxサーバーに保存（公開）します。
3. （※この時点ではまだIDなどは見えません。次の手順でWebから取得します）
4. **【重要】API サービスの有効化**:
   - [Roblox Creator Dashboard](https://create.roblox.com/dashboard/creations) で先ほど公開したゲームの設定を開きます（「環境設定」→「設定」）。
   - 「プライバシー」セクションの下にある **「API サービスへのStudio アクセスを有効にする」** にチェックを入れます。
   - → これがオフのままだと、MessagingService がサーバー上で動作せず、コマンドを一切受信できません。
   - 設定を保存したら、Roblox Studio に戻って再度「**Robloxに公開**」してください。

---

## 2. Universe ID (バーチャル空間 ID) の取得

公開が完了したら、Webブラウザからこのゲーム全体を指すID（Universe ID、またはExperience ID / バーチャル空間 ID）を取得します。
※画像などで見かける「開始プレースID (Place ID)」とは**異なるID**ですので注意してください。

1. ブラウザで [Roblox Creator Dashboard (Credentials/APIキー設定画面)](https://create.roblox.com/dashboard/credentials) または任意のダッシュボード画面にアクセスしてログインします。
2. 権限設定（後述の「Restrict by Experience」）などで自分のゲーム（例：`Nexus Ark テスト`）を検索して選択します。
3. 選択したゲーム名の横にあるコピーアイコン（四角が2つ重なったマーク）をクリックすると、「**バーチャル空間 ID をクリップボードにコピー**」と表示され、Universe IDがコピーされます。
   * (代替方法): または、ブラウザのURLから取得することもできます。`https://create.roblox.com/dashboard/creations/experiences/1234567890/overview` のようなURLになっている場合、`experiences/` の直後の数字がUniverse IDです。

---

## 3. Open Cloud APIキーの取得

外部（Nexus Ark）からRobloxのサーバーに対して指示を送るための暗号鍵（APIキー）を発行します。

1. [Roblox Creator Dashboard (Credentials)](https://create.roblox.com/dashboard/credentials) にアクセスします。
2. 画面右上にある **「APIキーを作成（Create API Key）」** ボタンをクリックします。
3. **キーの情報入力**:
   - 「名前（Name）」: `Nexus Ark Messaging Key` など、わかりやすい名前をつけます。
4. **アクセス権限（Access Permissions）**:
   - **「APIシステムを選択（Select API System）」**: **「Messaging Service API」** を選択します。
   - 「Restrict by Experience（エクスペリエンスで制限）」のトグルをONにします。（※警告が出たら「続ける」を選択）
   - 「エクスペリエンスを追加」の項目で、先ほど作成した「Nexus Ark」のテスト用ゲームを検索して追加します。
   - そのすぐ近くに出る **「追加する操作を選択（Select Operations）」** から **「Publish」** （公開）を選択して追加します。
5. **セキュリティ（オプション）**:
   - Accepted IP Addresses: テスト環境で特に指定がなければ `0.0.0.0/0` を入力（または空欄）します。
6. 一番下の **「保存してキーを生成（Save & Generate Key）」** をクリックします。
7. 【**重要**】生成された長い文字列（APIキー）が表示されます。**このキーは一度しか表示されません**。必ずコピーして安全な場所に保存してください。

---

## 4. Roblox StudioでのNPCアバター配置

Roblox Studioに戻り、AIの身体となるNPC（ダミー人形）を配置します。

1. **モデル（Model）の準備**:
   - 画面右側の「エクスプローラー（Explorer）」パネルで、「Workspace」の右の `+` を押し、**「Model」** を追加します。
   - 追加した Model を右クリック等で名前変更し、名前を **`NexusArkNPC`** にします。
2. **ダミーの配置**:
   - 画面上部の **「アバター（Avatar）」** タブをクリックします。（※以前はプラグインタブにありましたが、UI変更によりここへ移動しました）
   - オレンジ色の🔧スパナマークがついた **「リグの構築（Rig Builder / キャラクター...等の表記の場合あり）」** アイコンをクリックします。
   - 画面上にパネルが開くので、リグタイプ **「R15」** を選び、**「ブロック型アバター (2012年)」** をクリックしてダミーを出現させます。
3. **ダミーをNPCモデルに入れる**:
   - エクスプローラーに出現したダミー（名前は `Rig` などになっています）をドラッグして、1で作った `NexusArkNPC` の中に入れます。
4. **NPCを固定する**:
   - デフォルトだと倒れてしまうため、`NexusArkNPC` の中の `Rig` に入っている **「HumanoidRootPart」** を選択します。
   - 画面下の「プロパティ（Properties）」パネルで、**「Anchored」** にチェックを入れます（Trueにします）。

---

## 5-A. 統合スクリプトの設置（コマンド受信・空間認識・建築）

Nexus Arkからの指示を受け取り、NPCを動かしたり、周囲の状況をAIに伝えたり、建物を建てたりするためのプログラムをRoblox側に導入します。

1. エクスプローラーの下部にある **「ServerScriptService」** の右の `+` を押し、**「Script（スクリプト）」** を追加します。（名前は `NexusArkIntegratedScript` などがお勧めです）
2. 追加したスクリプトをダブルクリックして開き、最初に入っている `print("Hello world!")` をすべて消します。
3. 以下のファイルの中身を**丸ごとコピーして貼り付けます**。

   📄 **[roblox_integrated_script_v3.lua](../guides/roblox_integrated_script_v3.lua)**

4. 貼り付けたら、スクリプト **冒頭の設定セクション** を自分の環境に合わせて変更します。

   ```lua
   -- ⚙️ 設定 (Nexus Ark の UI 表示に合わせて変更してください)
   local WEBHOOK_URL = "https://your-tunnel-url.trycloudflare.com/api/roblox/event"  -- 後述のWebhook設定で取得
   local WEBHOOK_SECRET = "YOUR_WEBHOOK_SECRET_KEY"  -- 後述のWebhook設定で取得
   local ROBLOX_TOPIC = "NexusArkCommands"  -- Nexus ArkのUI設定と一致させてください
   local NPC_NAME = "NexusArkNPC"  -- 手順4で設定したNPCモデル名と一致させてください
   ```

> [!TIP]
> **WebhookのURLとシークレットについて**:
> Webhookを使用しない場合（Phase 1のみ）でも基本動作しますが、`WEBHOOK_URL` 等が未設定だとエラーログが出ます。
> 完全に連携させたい場合は [Roblox Webhook 外部公開ガイド](roblox_webhook_guide.md) を参照してください。

### 統合スクリプトの主な機能

| 機能 | 説明 |
|------|------|
| **コマンド受信** | jump（ジャンプ）、chat（吹き出し発言）、move（移動）、emote（エモート）、build（建築） |
| **空間認識** | 周囲30スタッド以内のオブジェクトとプレイヤーの位置を定期的にAIへ送信 |
| **チャット検知** | プレイヤーがゲーム内チャットで発言した内容をAIへ上記のWebhook転送 |
| **行動検知** | プレイヤーのジャンプやエモートを検知してAIへ通知 |
| **建築** | 単一パーツまたは複数パーツ＋材質指定での構造物建築（モデル化対応） |
| **プレイヤー建築** | プレイヤーが直接ブロックを配置するためのRemoteEvent処理 |

---

## 5-B. プレイヤー建築ツールの設置（オプション）

プレイヤーがクリックで直接ブロックを配置し、AIと一緒に建築を楽しむためのツールです。
この手順は任意ですが、導入すると建築体験が大幅に向上します。

1. エクスプローラーの **「StarterPlayer」** を展開し、その中にある **「StarterPlayerScripts」** の右の `+` を押します。
2. **「LocalScript」** を追加します。（名前は `NexusArkPlayerBuildTool` がお勧めです）
3. 追加したスクリプトをダブルクリックして開き、以下のファイルの中身を**丸ごとコピーして貼り付けます**。

   📄 **[roblox_player_build_tool.lua](../guides/roblox_player_build_tool.lua)**

4. 保存すれば完了です。設定の変更は不要です。

### プレイヤー建築ツールの操作方法

画面左の **MODEボタン** で操作を切り替えます。

| モード (MODE) | 説明 |
|---------------|------|
| **PLACE (配置)** | クリックした場所に新しいパーツを設置します。SHAPE（形状）やMAT（材質）を変更可能です。 |
| **EDIT (編集)** | 配置済みの「NexusArk」パーツをクリックして選択し、UIボタンで移動(X/Y/Z)やサイズ変更(SIZE)ができます。 |
| **DELETE (削除)** | クリックした「NexusArk」パーツを即座に削除します。 |

| 設定項目 (PLACE時) | 説明 |
|--------------------|------|
| **SHAPE (形状)** | Block (立方体) / Wedge (くさび) / Sphere (球) / Cylinder (円柱) を切り替え |
| **MAT (材質)** | Plastic / Wood / Metal / Brick / Concrete / Neon / Glass 等を切り替え |

> [!TIP]
> **Shift + C** キーで、建築エディタUIの表示/非表示をいつでも切り替えられます。

> [!NOTE]
> プレイヤー建築ツールは **荒らし対策** として、自分から30スタッド以内の場所にしか配置できない制限がかかっています。
> AIが配置した建築物はこの制限の対象外です。

---

## 6. 建築データの保存（セーブ）設定
AIやプレイヤーが作った建築物を、サーバー再起動後も残したい場合は、以下の設定が必要です。

1.  Roblox Studioの上部メニューから **「Home」 > 「Game Settings」** を開きます。
2.  左側のタブから **「Security」** を選択します。
3.  **「Enable Studio Access to API Services」** を **ON** にします。（※DataStore機能を使用するために必須です）
4.  **「Save」** をクリックして閉じます。

これで、建築・編集・削除が行われるたびに自動的にデータが保存され、次回起動時に自動で復元されるようになります。

---

## 7. 最終確認とNexus Ark側の設定

1. Roblox Studioでもう一度「ファイル（File）」＞**「Robloxに公開（Publish to Roblox）」** をクリックし、ここまでの変更を保存します。
2. **Nexus Ark** の画面を開き、ルーム設定（または個別設定）の「ROBLOX連携」パネルを開きます。
   - **API Key**: Webで取得した「Open Cloud APIキー」
   - **Universe ID**: Webでコピーした「Universe ID（バーチャル空間 ID）」
   - **Topic**: `NexusArkCommands` （※スクリプト内の TOPIC 変数と一致させます）
3. 設定を保存し、「🔌 接続テスト」ボタンを押して `✅ 接続成功！` が表示されることを確認します。

---

## 7. テスト方法

> ⚠️ **重要**: Roblox Studio の「プレイ」ボタンで起動したローカルテストでは、Open Cloud API からのメッセージは**受信できません**。
> これは Roblox プラットフォームの仕様で、メッセージはライブゲームサーバーにのみ配信されます。

### テスト手順

1. **Roblox クライアント（デスクトップアプリまたはWeb版）でゲームに参加**します。
   - Creator Dashboard → 自分のゲーム → 「プレイ」や「ゲームを開く」で実際のサーバーに接続します。
   - ゲームが「プライベート」設定なら自分だけが入れます。
2. ゲーム内で **F9 キー** を押して Developer Console を開きます。
   - 「Log」タブ → 「Server」フィルターに切り替えます。
   - `[NexusArk] ✅ コマンドハンドラ起動完了` が表示されていれば、スクリプトは正常に動いています。
3. **Nexus Ark** のチャットでAIに「**RobloxのNPCをジャンプさせて**」や「**Robloxで『こんにちは』と言って**」と指示します。
4. Roblox上のダミー人形が指示通りに動けば、連携成功です！

### デバッグ用情報

- **出力パネルの表示方法**: Roblox Studio では「**ウィンドウ**」メニュー → 「**出力**」をクリックすると、サーバーログが確認できます。
- **スクリプトの全ログに `[NexusArk]` プレフィックス**がついているので、フィルタで絞り込めます。

---

## 8. 次のステップ: Webhook連携（Phase 2）

AIがRoblox内の出来事（プレイヤーが近づいた、チャットした等）を認識するためには、RobloxからNexus Arkにイベントを送信する必要があります。
詳細は [Roblox Webhook 外部公開ガイド](roblox_webhook_guide.md) を参照して、ローカルサーバーのトンネル設定を行ってください。
