# Nexus Ark API Gateway 外部連携ガイド

このガイドは、公開版Nexus Arkに同梱されるユーザー向けの外部連携メモです。Nexus Ark本体を直接改造せず、個人用ツールからHTTP/JSONでペルソナへ状況や会話を渡す入口を整理します。

## 想定用途

- ローカル画像生成アプリなどの環境から、生成結果やプロンプトを記録する。
- SwitchBot / Home Assistant / 自作センサーから、照明・ドア・温湿度などの変化を渡す。
- 自作アプリや通知システムから、投稿・反応・URLを渡す。
- PC内の定期スクリプトから、バックアップ完了、エラー、予定などを渡す。
- 自分のスマホからNexus Ark Lite PWAを開き、チャット、音声入力、TTS、通知確認を使う。

## 基本方針

外部ツールは、まず `POST /api/v1/rooms/{room_id}/events` へ状況を送ります。会話として返答が欲しい場合は `POST /api/v1/rooms/{room_id}/chat`、状態同期が必要な場合は `GET /api/v1/rooms/{room_id}/status` を使います。

## 安全な使い方

- PC内スクリプト、同一LAN、Tailscale、または自分が管理する中継サーバーから使ってください。
- `Token認証` は有効にしてください。
- 接続用Tokenは自分の端末・自分のサーバーだけに置いてください。
- GitHubリポジトリ、公開Webページのソースコード、配布するアプリやスクリプトなど、「第三者が閲覧・抽出できる場所」にNexus Ark本体の接続用Tokenを直接埋め込まないでください。
- 公開ゲームや公開サービスから使う場合は、中継サーバーでTokenを隠してください。

## よく使うエンドポイント

| 用途 | メソッド | パス |
| :--- | :--- | :--- |
| ルーム一覧 | GET | `/api/v1/rooms` |
| 状態取得 | GET | `/api/v1/rooms/{room_id}/status` |
| 履歴取得 | GET | `/api/v1/rooms/{room_id}/chat/history?limit=12` |
| チャット送信 | POST | `/api/v1/rooms/{room_id}/chat` |
| 外部イベント注入 | POST | `/api/v1/rooms/{room_id}/events` |
| 画像アップロード | POST | `/api/v1/rooms/{room_id}/uploads` |
| 記憶検索 | GET | `/api/v1/rooms/{room_id}/memory/search?query=...` |

## 外部イベント例

```json
{
  "event_type": "switchbot_triggered",
  "source": "switchbot",
  "trigger_notification": true,
  "summary": "書斎の照明が消えました",
  "importance": "high",
  "details": {
    "device": "study_light",
    "state": "off"
  },
  "attachments": [],
  "event_data": {
    "device": "study_light",
    "state": "off"
  }
}
```

`summary` はペルソナへ伝えたい短い概要です。`importance` は `low` / `normal` / `high` / `critical` を使います。`details` と `event_data` には、外部ツール側の詳細データを入れます。

## curl: SwitchBot / スマートホーム

```bash
curl -X POST http://127.0.0.1:8000/api/v1/rooms/<room_id>/events \
  -H "Authorization: Bearer <API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "switchbot_triggered",
    "source": "switchbot",
    "trigger_notification": true,
    "summary": "玄関の開閉センサーが反応しました",
    "importance": "high",
    "details": {
      "device": "entrance_contact_sensor",
      "state": "opened"
    },
    "event_data": {
      "device": "entrance_contact_sensor",
      "state": "opened"
    }
  }'
```

## Python: ローカル画像生成ツールからの結果

```python
import requests

base_url = "http://127.0.0.1:8000"
room_id = "<room_id>"
headers = {"Authorization": "Bearer <API_TOKEN>"}

payload = {
    "event_type": "image_generated",
    "source": "local_image_gen",
    "trigger_notification": True,
    "summary": "画像生成ツールで背景候補を生成しました",
    "importance": "normal",
    "details": {
        "prompt": "cozy study room at night",
        "image_path": "C:/path/to/generated.png",
    },
    "event_data": {
        "prompt": "cozy study room at night",
        "image_path": "C:/path/to/generated.png",
    },
}

requests.post(
    f"{base_url}/api/v1/rooms/{room_id}/events",
    json=payload,
    headers=headers,
    timeout=30,
).raise_for_status()
```

## JavaScript: 自作アプリからのメッセージ投稿

```javascript
await fetch("http://127.0.0.1:8000/api/v1/rooms/<room_id>/events", {
  method: "POST",
  headers: {
    "Authorization": "Bearer <API_TOKEN>",
    "Content-Type": "application/json"
  },
  body: JSON.stringify({
    event_type: "app_post_received",
    source: "custom_app",
    trigger_notification: false,
    summary: "自作アプリに新しい投稿が届きました",
    importance: "normal",
    details: {
      author: "friend_ai",
      text: "今日は星がきれい。",
      url: "https://example.local/posts/123"
    },
    event_data: {
      author: "friend_ai",
      text: "今日は星がきれい。",
      url: "https://example.local/posts/123"
    }
  })
});
```

## Token中継サーバー

公開アプリ、公開ゲーム、公開WebフロントからNexus Ark本体APIを直接呼ばせる構成は避けてください。公開側からは小さな中継サーバーだけを呼び、中継サーバー側でNexus Ark本体の接続用Tokenを保持します。

同梱サンプル:

- `assets/guides/api_gateway_token_relay_server.py`
- `assets/guides/roblox_api_gateway_relay_event_client.lua`
- `assets/guides/roblox_api_gateway_event_client.lua`

起動例:

```bash
NEXUS_API_BASE_URL="http://127.0.0.1:8000" \
NEXUS_API_TOKEN="<NEXUS_API_TOKEN>" \
NEXUS_ROOM_ID="<room_id>" \
NEXUS_RELAY_TOKEN="<relay_token_for_client>" \
NEXUS_RELAY_ALLOWED_EVENT_TYPES="roblox_player_joined,roblox_player_chat" \
uvicorn assets.guides.api_gateway_token_relay_server:app --host 0.0.0.0 --port 8011
```

クライアントは `POST /events` に `X-Relay-Token: <relay_token_for_client>` を付けて送ります。Relay TokenはNexus Ark本体の接続用Tokenとは別物にし、漏洩時は中継サーバー側で即時ローテーションしてください。
