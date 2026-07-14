---
id: atelier-api-buttons
title: Atelier API buttons and Nexus Ark payloads
summary: アトリエPWAからNexus Arkへ送信・記録・権限付きAPI呼び出しを行うとき。payload、scope、token更新、成功/失敗表示を実行役が確認します。
applies_to:
  workspace_kinds: [persona, persona_project_read]
  keywords: [app, pwa, アプリ, webアプリ, api, nexus, capabilities, send_chat, write_event, read_items, write_items, payload, scope, token, 送信, ボタン, 権限, 記録, チャット, アイテム, エラー]
  require_keywords: true
priority: 99
max_chars: 1700
---

## Nexus Ark API buttons
Use this for apps that call Nexus Ark. Paths below are relative to `/api/v1/rooms/{roomId}/`.
Always provide visible feedback for sending, success, partial success, and failure.

### Endpoints
- Safe: `GET capabilities` (full live list), `status`, `locations`.
- Read scopes: `read_chat`, `read_memory`, `read_notes`, `read_letters`, `read_calendar`, `read_twitter`, `read_items`, `read_autonomy`.
- Action scopes: `send_chat` (chat/regenerate/uploads), `write_event`, `write_notes`, `write_calendar`, `write_items`, `write_location`, `write_autonomy`, `use_voice`, `manage_push`, `post_twitter`.

Fetch `GET capabilities` before implementation and follow its live `method`, `path`, `access`, and `scope`.

Declare scopes in `workspace/apps/<name>/nexus.json`: `{ "requested_scopes": ["write_event", "send_chat"] }`.

### Required payload shapes
- `chat`: `{ "message":"...", "source":"atelier:<app>", "client_message_id":"..." }`; never `text`.
- `chat/regenerate`: `{ "target_message_id":"latest-agent-message-id", "client_message_id":"unique-id" }`; latest reply only, no tool/action re-run.
- `items/actions`: `{ "action":"gift|consume|place|pickup|consume_location", "item_id":"...", "amount":1, "location":"...", "furniture":"...", "client_action_id":"stable-unique-id" }`; reuse the ID on retry to prevent double execution.
- `events`: `{ "event_type":"...", "source":"atelier:<app>", "summary":"...", "details":{}, "event_data":{}, "importance":"normal|high|critical" }`; never `type`/`content`.
- A 422 response usually means token/scope is fine but JSON body is wrong. Read the body and fix the exact field.

### Must-have diagnostics
- Fetch `./_nexus/config` with `cache: "no-store"` near the API call; do not reuse startup tokens forever.
- Validate payloads locally and name the exact missing field before sending.
- Inspect `cfg.grantedScopes` / `cfg.pendingScopes` and show: ready / permission pending / denied or expired / payload incomplete.
- Provide visible feedback for sending, success, partial success, and failure in an in-app status area that persists after any toast disappears.
- On 401/403, refresh config once, retry once, then show the required scope and whether it is pending or denied.
- On 5xx/network errors, show failing stage, retryability, and endpoint. Do not surface only "500".

For item UI, fetch `GET items` first and only offer actions allowed by the returned item/location state. Treat `write_items` as sensitive: explain that granting it permits consumption, gifting, placement, and pickup, and require a visible confirmation before POST.

For record/report buttons, POST `events` first so data is saved through the lightweight event path.
Treat POST `chat` as optional follow-up because it waits for persona response generation.

`owner_only` endpoints cannot be enabled by an app grant.

### Final report
Report requested scopes, observed granted/pending scopes, endpoints called, payload shape with secrets redacted, validation performed, and files/functions to inspect if the button still fails.
