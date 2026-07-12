# 外部接続とNexus Ark Lite

外部接続は、Nexus Arkをスマホ、SNS、Bot、自作ツールへ広げるための設定です。
中央「外部接続」タブにまとまっています。

## Twitter (X)

中央「外部接続」→「Twitter (X)」で、Twitter投稿の下書き、承認、投稿履歴を扱います。
投稿は下書きキューに入り、人間が承認するまで外部へ投稿されません。
画像生成付き下書きや、投稿履歴の確認にも対応します。

DiscordからTwitter下書きを承認する場合は、Discord側の許可ユーザーID設定が必要です。

## Discord / LINE

中央「外部接続」→「Discord / LINE」で、Discord BotとLINE Botを設定します。

Discord Botでは、ルームごとのBot割り当て、スラッシュコマンド、ルーム切替、再生成、Twitter下書き承認などを扱います。
LINE Botでは、LINEからペルソナと会話できます。
外部からアクセスする場合は、Cloudflare Tunnel、Tailscale Funnel、ngrokなどのHTTPS URLが必要になることがあります。

## 拡張ツール

中央「外部接続」→「拡張ツール」では、ローカルプラグインとMCPを管理します。

- 「ローカルプラグイン」: Pythonファイルとして自作ツールを追加します。
- 「MCP」: stdio、sse、streamable_http、simple_httpの接続を登録できます。

拡張ツールは便利ですが、外部コマンドや外部サービスへつながる場合があります。
有効化するプラグインやMCPサーバは、内容を理解してから使ってください。

## API Gateway / Lite

中央「外部接続」→「API Gateway / Lite」で、REST APIとスマホ向けNexus Ark Liteを設定します。
「個人用の使い方」に概要が表示されます。

基本手順は次の通りです。

1. 「API設定」を開き、「API Gatewayを有効化」をONにする。
2. 「Token認証」をONにし、「Token生成」で接続用Tokenを作る。
3. 「API設定を保存」を押す。
4. 「接続情報を更新」を押し、「Nexus Ark Lite 接続情報」を確認する。
5. 表示されたURLをスマホで開く。

Nexus Ark Liteでは、チャット、履歴再取得、画像添付、音声入力、TTS再生、現在地移動、自律行動プリセット変更、Twitter下書き承認/却下、研究ノート・創作ノートの閲覧と編集、手紙箱の閲覧などができます。
手紙箱はメニューの「📮 手紙箱」から開き、手紙を選ぶと本文が表示されて既読になります（本体側と同期します）。

## 安全診断

「API Gateway / Lite」→「安全診断」では、Host、Port、Token認証、レート制限、監査ログ、Tailscale HTTPS候補などを確認できます。
公開HostでToken認証なしにする設定は安全側で拒否されます。

インターネットへ直接公開する運用は推奨しません。
同一LANやTailscaleなど、本人が管理できる範囲で使ってください。

## 外部イベント

「API Gateway / Lite」→「外部イベントテスター」では、外部イベントを現在のルームへ記録できます。
自作アプリや通知システムから、状態変化やイベントをペルソナへ渡したい場合の確認に使います。
