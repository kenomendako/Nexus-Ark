# Nexus Ark Lite PWA 接続ガイド

Nexus Ark Liteは、Nexus Ark API Gatewayから配信されるスマホ向け軽量PWAです。追加のフロントエンドサーバーは不要で、API Gatewayを起動した端末へスマホブラウザからアクセスします。

## 起動

Gradio本体と同時に使う場合は、「外部接続 > API Gateway / Lite」でREST API Gatewayを有効化し、Host/Port、接続用Tokenを保存します。Host/Portや有効化状態の変更後は、保存時に即時反映されます。

API/PWAだけ単体で起動する場合:

```bash
.venv/bin/python run_api_server.py
```

PC内では次を開きます。

```text
http://127.0.0.1:8000/lite
```

## 同一Wi-Fiでスマホから接続

1. 「外部接続 > API Gateway / Lite」でAPI Hostが `0.0.0.0` になっていることを確認し（初期値は `0.0.0.0` です）、API Gatewayを有効化して保存します。
2. 接続用Tokenを生成して保存します。
3. PCとスマホを同じWi-Fiへ接続します。
4. スマホで `http://<PCのIPアドレス>:8000/lite` を開きます。
5. PWAのToken欄へ保存した接続用Tokenを入力して接続します。

WSL上でNexus Arkを動かしている場合、同一Wi-Fi直結にはWindows側のportproxyとファイアウォール許可が必要になることがあります。接続できない場合はTailscale HTTPS経由を推奨します。

## Tailscale HTTPSで接続

スマホブラウザのマイク権限、PWA、通知はHTTPSのほうが安定します。Tailscaleを使う場合、PC側で次を実行します。

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8000
```

設定後はスマホで次を開きます。

```text
https://<PCのTailscale DNS名>.ts.net/lite
```

「外部接続 > API Gateway / Lite」の接続情報には、検出できたTailscale DNS名、HTTPS URL候補、実行コマンドを表示します。Tailscale設定を変えた後は「接続情報を更新」を押してください。

## 現在できること

- ルーム一覧の取得
- ルーム状態の表示
- チャット送信
- 画像添付付きチャット送信
- 音声入力の文字起こし
- AI応答のTTS再生
- 管理メニューからのTwitter下書き承認/却下
- 現在地移動
- 自律行動プリセット切替
- 研究ノート・創作ノートの確認
- ローカル通知とWeb Push通知
- AI応答通知の本文プレビュー（通知設定からOFFにできます）
- 保存済み接続先への自動再接続
- 直近チャット履歴の表示

## PWA更新後の表示確認

Lite PWAはService Workerで `/lite` と静的ファイルをキャッシュします。更新後にスマホ表示が古い場合は、ブラウザで再読み込みしてください。それでも古い表示が残る場合は、ホーム画面のPWAアイコンを削除して再追加すると確実です。
