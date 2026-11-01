# 取得できる情報から探すAPIガイド

Liteで表示している情報を、自作ツールやアトリエアプリでも利用するためのガイドです。APIを選ぶときは、取得情報・返却形式・権限・接続先を確認してください。
全APIと利用条件の正本は本体の「外部接続」→「API・外部ツール」→「利用可能なAPI一覧・外部連携リファレンス」に表示されます。同じ一覧を `GET /api/v1/rooms/{room_id}/capabilities` からJSONで取得でき、ペルソナのアトリエ能力確認にも使われます。
以下は目的別の読み方と合成データの例です。例のフィールドは一部を省略しています。実際の全項目・型は本体の `/docs` で確認できます。

## 本体接続と独立モードの接続先

| 接続先 | 利用できる情報 | 認証とデータの時点 |
| :--- | :--- | :--- |
| 本体API Gateway `/api/v1/...` | 現在地・状態、履歴、ノート、手紙、アイテム、予定、管理情報など | 本体が稼働し到達できることが必要。所有者Token、または対象ルーム・アプリ権限に限定したアトリエTokenを使用 |
| Lite独立用Worker `/v1/...` | 現在セッション、引継ぎ会話、独立中の会話イベント、利用量、モデル一覧など | ペアリングした端末のaccess tokenを使用。本体Tokenやアトリエのscopeとは別の仕組み |

独立中の履歴は、お出かけ前に引き継いだ会話とWorkerに確定した新しい会話を組み合わせます。引継ぎデータの作成時刻と、独立中の最新イベントを区別してください。
独立中でも、本体が稼働して接続情報が残っていれば、Liteの記録・管理の一部から本体APIを参照できる場合があります。その取得元は本体です。PC停止中にWorkerだけでノートや本体の現在地を読み直せる機能ではありません。

## 本体APIで取得できる情報

`{room_id}` は対象ルームIDに置き換えます。「追加承認不要」も認証不要という意味ではなく、有効なアトリエTokenの対象ルームで使える範囲です。所有者Tokenは本体の接続用Tokenです。

| 目的 | GETパス | 主な情報・絞り込み | アトリエの必要権限 |
| :--- | :--- | :--- | :--- |
| 現在地・動機・感情を見る | `/api/v1/rooms/{room_id}/status` | `current_location`、`drives`、`current_expression`、`persistent_emotions`、`arousal`・件数・日付、`active_goals_count`、`profile_image_path` | 追加承認不要 |
| 移動先を選ぶ | `/api/v1/rooms/{room_id}/locations` | 現在地と`locations[].id/name/area` | 追加承認不要 |
| 会話・音楽カード・通知案内を読む | `/api/v1/rooms/{room_id}/chat/history` | `messages[]`。`limit=1〜50`、既定12。本文、話者、ID、時刻、モデル、添付パス | `read_chat` |
| 生成画像・アイテム画像を見る | `/api/v1/rooms/{room_id}/assets` | `path`必須。画像バイナリを返す。履歴・状態・アイテムに含まれる画像パスを指定 | `read_items` |
| 生成済み音声を聴く | `/api/v1/rooms/{room_id}/audio` | `path`必須。音声バイナリを返す。音声の生成は別途`POST .../tts` | `use_voice` |
| 記憶を検索する | `/api/v1/rooms/{room_id}/memory/search` | `query`必須、`limit=1〜20`。`results[].type/content/metadata` | `read_memory` |
| ノート・日記等を読む | `/api/v1/rooms/{room_id}/notes/{note_type}` | `content`、`headings`、`content_hash`、更新時刻、編集可否。下記の読取指定に対応 | `read_notes` |
| 手紙を探す | `/api/v1/rooms/{room_id}/letters` | `unread_count`とid・件名・作成日時・既読状態。`limit=1〜200`、既定100 | `read_letters` |
| 手紙本文を読む | `/api/v1/rooms/{room_id}/letters/{letter_id}` | `body`、作成日時、既読日時。既定で既読化。確認だけなら`mark_read=false` | `read_letters` |
| 所持品・配置アイテムを読む | `/api/v1/rooms/{room_id}/items` | `target=user/persona/location`、`location`。id・名前・個数・説明・画像パス | `read_items` |
| 予定を表示する | `/api/v1/rooms/{room_id}/calendar/events` | `days=1〜31`、既定7。共有範囲内の予定の件名・開始/終了・終日・場所 | `read_calendar` |
| Twitter下書きを確認する | `/api/v1/rooms/{room_id}/twitter/drafts` | `drafts[]`の本文・警告・返信先・画像パス。取得では投稿しない | `read_twitter` |
| 自律行動の設定を確認する | `/api/v1/rooms/{room_id}/autonomy` | 有効状態、行動間隔、予約間隔、quiet hours、`preset` | `read_autonomy` |
| Push登録状況を調べる | `/api/v1/rooms/{room_id}/push/status` | 登録端末数、端末別の成功/失敗時刻・失敗数。通知本文一覧ではない | `manage_push` |

履歴の添付パスを読む権限と、画像本体を取得する`read_items`は別です。画像付き会話ビューでは、両方の承認が必要になります。
手紙本文の既読化、Push状態取得時の無効な登録情報の整理など、GETでも状態が変わるものがあります。記憶検索は構成により検索用Embeddingの外部API送信・費用を伴います。

### 履歴の音楽カード・通知・ツール案内

`role: "system"`の`content`には、PC向けに整形した音楽カードや通知・タイマー設定・現在地変更・ツール失敗等の案内が入ります。音楽はMarkdown形式の曲名・理由・検索リンクで、曲の専用JSON配列や音声データではありません。
`[RAW_RESULT]`以下の内部結果は返しません。通知ツールが記録した通知本文と、ツールの実行結果を知らせる案内は別の履歴行になることがあります。
添付画像は`attachments[]`から取得します。案内・添付を持つ行も履歴件数に含まれます。履歴は保存済みの表示情報で、現在稼働中のタイマー一覧や自律行動の全タイムラインを返すAPIではありません。
`include_thoughts=true`は所有者Tokenで明示したときだけ利用でき、さらに本体の表示設定に従います。アトリエの`read_chat`承認だけでは取得できません。

### ノート・記憶の読み方

`note_type`は`research`（研究）、`creative`（創作）、`diary`（日記）、`notepad`（メモ帳）、`dream`（夢日記）、`episodic`（エピソード）です。

- `headings_only=true`: 見出し一覧だけを取得。
- `heading=<見出し>`: 選んだセクションを取得。URLエンコードして指定。
- `latest=true`: 最新の一件を取得。夢日記・エピソードにも利用可能。
- 研究・創作の本文更新は`write_notes`と`PUT`が必要です。`base_hash`で更新前の内容を照合します。日記・メモ帳・夢・エピソードは読み取り専用です。

## 所有者Tokenで確認できる本体管理情報

Liteが利用していても、以下はアトリエアプリへ無条件に公開するAPIではありません。アプリの承認へscopeを追加しても所有者限定のAPIは利用できません。

| GETパス | 取得できる情報 |
| :--- | :--- |
| `/api/v1/rooms` | 本体にあるルームのID・表示名・説明・現在地 |
| `/api/v1/rooms/{room_id}/api-key` | 登録キーの表示名・共通/個別の選択状態。キー本体は返さない |
| `/api/v1/rooms/{room_id}/response-settings` | 実効プロバイダ・モデル・キー名・回転設定・共通/個別の値・候補・`revision` |
| `/api/v1/rooms/{room_id}/user-memo` | ユーザーからペルソナへの置き手紙の`body`・`content_hash`・更新時刻 |
| `/api/v1/rooms/{room_id}/temporary-location` | 一時的現在地の適用状態・名前・情景文・写真有無と画像URL |
| `/api/v1/rooms/{room_id}/temporary-location/saved` | 保存済み一時地点の一覧 |
| `/api/v1/rooms/{room_id}/temporary-location/saved-entry` | `name`で選んだ保存地点の情景・写真。取得だけでは適用しない |
| `/api/v1/rooms/{room_id}/chat/active` | 進行中の本体接続チャット要求。タイマー一覧ではない |
| `/api/v1/lite-travel/standby` | 本体が管理するお出かけ前データの準備状況 |
| `/api/v1/lite-travel/diagnostics` | 本体とLite用クラウドの互換性・運用診断 |

一時的現在地の写真は専用の`.../temporary-location/image`、保存写真は`.../temporary-location/saved-image?name=...`から取得します。どちらも所有者Token限定です。

## 独立Workerで取得できる情報

以下のパスは本体URLではなく、設定したLite用クラウドURLへ送ります。Liteの通常利用では`/v1/health`以外に端末のaccess tokenを使用し、期限切れ時の更新はrefresh tokenで行います。provider profilesとそのmodelsはWorkerの管理用owner tokenも受理します。本体TokenやアトリエTokenを流用しません。

| GETパス | 主な情報・引数 |
| :--- | :--- |
| `/v1/health` | Workerの状態、build・API/PWA/schema互換情報、capabilities（認証不要） |
| `/v1/travel-sessions/current` | 現在の`session`、複数ペルソナの`personas[]`、経路・状態、`inherited_recent_messages`、`snapshot_created_at` |
| `/v1/standby-snapshots` | `snapshots[]`の世代・作成/期限日時・準備状態・persona manifest。全記憶の本文ではない |
| `/v1/travel-sessions/{session_id}/personas/{persona_id}/events` | `after_sequence`以降の`events[]`、`next_cursor`、`missing_sequences` |
| `/v1/travel-sessions/{session_id}/usage-summary` | 既知費用、価格不明件数、予約額、予算・警告・停止状態。`persona_id`でペルソナ別に絞れる |
| `/v1/provider-profiles` | AI接続の表示名・provider・有効状態。Secret値は返さない |
| `/v1/provider-profiles/{profile_id}/models` | モデル候補・利用可否・料金表の版。`refresh=1`で再取得 |
| `/v1/message-requests/{client_message_id}` | 送信要求の状態・確定済みevent ID等。未確定送信の再照会にも使用 |

予算の明示増額は登録端末のaccess tokenで `PUT /v1/travel-sessions/{session_id}/budget` を使用します。
`scope`（`session`／`persona`）、ペルソナ範囲なら`persona_id`、確認時点の`expected`と新しい`limits`
（各`daily_limit_usd`／`session_limit_usd`）、`confirmed: true`が必要です。今回の有限上限の増額だけを許可し、
既存の無制限項目は`null`を維持します。古い上限・送信中・帰宅後は拒否し、次回初期値や保留額を変えず、自動送信しません。

単一ペルソナの旧セッションでは`/v1/travel-sessions/{session_id}/events`を使います。イベントは100件単位で取得し、必要なら返された`next_cursor`を次の`after_sequence`へ渡します。
Workerのイベント本文に音楽カードがあればLiteで表示できますが、本体のツール実行やPC側のツール結果をWorkerへ追加する機能ではありません。
Workerには本体ノート・手紙・現在地・稼働タイマー・思考を直接取得する同等のGET APIはありません。完全なペルソナsnapshotの本文をGETで読むAPIもありません。外部AI向けの書き出しは明示同意付きの別操作です。

## 本体APIの返却例

以下はすべて架空のデータです。画像パスやIDは、実際には取得結果にある値を使います。

### 現在地と状態

取得先: `GET /api/v1/rooms/{room_id}/status`
```json
{
  "room_id": "example-room",
  "display_name": "サンプル",
  "current_location": "庭",
  "drives": {"boredom": 0.2, "curiosity": 0.6},
  "current_expression": "joy",
  "arousal": 0.4,
  "arousal_sample_count": 3,
  "arousal_date": "2026-10-21",
  "persistent_emotions_available": true,
  "persistent_emotions": [
    {"category": "joy", "name": "喜び", "enabled": true, "status": "active", "presentation_status": "active", "intensity": 0.8, "started_at": "2026-10-21T03:00:00Z", "current_intensity": 0.4}
  ],
  "active_goals_count": 1,
  "profile_image_path": "profile.png",
  "updated_at": "2026-10-21T09:00:00Z"
}
```

持続感情の一覧は、独自項目を含む本人の申告記録です。`intensity`は申告値、`started_at`は申告日時、
`current_intensity`は取得時点の減衰参考値です。未申告は`status="unreported"`で数値・日時がnullとなり、0と異なります。
`status`は保存上の状態、`presentation_status`は表示用で、`active`・`unreported`・`settled`・`expired`・
`below_threshold`・`disabled`・`invalid`・`unsupported`を返します。`expired`は本人による終了ではありません。
`persistent_emotions_available=false`は読取不能で、未申告や空一覧と同一視しません。理由・revision・設定編集機能は公開しません。

`arousal`は記憶整理・想起のために使う内部スコアの本日平均です。対話に加え、内省で問いを解決した際の記録も含みます。
有効記録数`arousal_sample_count=0`では実測値がなく、`arousal=0.5`は互換用の値です。
件数nullは取得不明、`arousal_date`は集計対象日です。UI110ではLiteの画面表示を外し、APIの値・件数・日付と内部処理は維持します。
既存の記録がないルームへ状態GETを行っても、感情値・申告時刻・設定・ログ・索引を新規作成／変更しません。

### 音楽カードを含む履歴

取得先: `GET /api/v1/rooms/{room_id}/chat/history`
```json
{
  "room_id": "example-room",
  "messages": [
    {
      "role": "system",
      "speaker": "tool_result:recommend_music:example-call",
      "message_id": "example-message",
      "content": "🛠️ ## 音楽推薦カード\n\n1. **Example Song** - Example Artist\n   - 聴く/探す: [YouTube](https://www.youtube.com/results?search_query=Example+Song)",
      "thoughts": "",
      "attachments": []
    },
    {
      "role": "agent",
      "content": "この曲が合いそうです。",
      "timestamp": "2026-10-20T12:00:00Z",
      "model": "example-model",
      "attachments": ["generated_images/example.png"]
    }
  ]
}
```

### 所持アイテム

取得先: `GET /api/v1/rooms/{room_id}/items`
```json
{
  "room_id": "example-room",
  "target": "user",
  "items": [{"id": "example-item", "name": "お茶", "amount": 2, "image_path": "items/example.png"}]
}
```

### ノートの選択セクション

取得先: `GET /api/v1/rooms/{room_id}/notes/{note_type}`
```json
{
  "room_id": "example-room",
  "note_type": "research",
  "title": "研究ノート",
  "content": "葉の形を比べました。",
  "headings": ["2026-10-20 12:00 - 葉の形を比べました。"],
  "content_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "editable": true
}
```

### 自律行動の設定

取得先: `GET /api/v1/rooms/{room_id}/autonomy`
```json
{
  "room_id": "example-room",
  "enabled": true,
  "inactivity_minutes": 120,
  "schedule_cooldown_minutes": 60,
  "quiet_hours_start": "00:00",
  "quiet_hours_end": "07:00",
  "preset": "normal"
}
```

### 既読にせず読む手紙

`GET /api/v1/rooms/{room_id}/letters/{letter_id}?mark_read=false`で確認だけ行う例です。

取得先: `GET /api/v1/rooms/{room_id}/letters/{letter_id}`
```json
{
  "room_id": "example-room",
  "id": "example-letter",
  "title": "夕方の散歩",
  "body": "今日は庭を歩きました。",
  "created_at": "2026-10-20T12:00:00Z",
  "read_at": null
}
```

## アトリエアプリから取得する例

アプリは`nexus.json`で必要な`requested_scopes`を宣言し、ユーザーの承認を受けます。所有者Tokenをアプリのソースへ埋め込まず、同じアプリの`./_nexus/config`から対象ルームの短期Tokenを取得します。

```javascript
const cfg = await (await fetch("./_nexus/config", { cache: "no-store" })).json();
const room = encodeURIComponent(cfg.roomId);
const response = await fetch(`${cfg.apiBase}/api/v1/rooms/${room}/chat/history?limit=12`, {
  headers: { Authorization: `Bearer ${cfg.token}` }
});
if (!response.ok) throw new Error(`履歴取得失敗: ${response.status}`);
const { messages } = await response.json();
// contentは文字列として表示。独自HTMLとして直接流し込まない。
```

この例には`read_chat`の承認が必要です。401/403では設定を一度再取得して再試行し、それでも取得できなければ権限・接続状態を表示してください。画像も表示する場合は`read_items`を宣言し、`assets?path=...`で取得します。
