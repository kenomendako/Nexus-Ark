---
id: atelier-api-buttons
title: Atelier API buttons and Nexus Ark payloads
summary: アトリエPWAからNexus Arkへ送信・記録・権限付きAPI呼び出しを行うとき。payload、scope、token更新、成功/失敗表示を実行役が確認します。
applies_to:
  workspace_kinds: [persona, persona_project_read]
  keywords: [app, pwa, アプリ, webアプリ, api, nexus, send_chat, write_event, payload, scope, token, 送信, ボタン, 権限, 記録, チャット, エラー]
  require_keywords: true
priority: 99
max_chars: 1700
---

## Nexus Ark API buttons
Use this for any app that may call Nexus Ark. It prevents the common first-delivery bugs: stale
tokens, wrong payload fields, missing scopes, and no user-visible result after pressing a button.

### Must-have diagnostics
- Fetch `./_nexus/config` with `cache: "no-store"` near the API call; do not reuse startup tokens forever.
- Validate payloads locally and name the exact missing field before sending.
- Inspect `cfg.grantedScopes` / `cfg.pendingScopes` and show: ready / permission pending / denied or expired / payload incomplete.
- Provide visible feedback for sending, success, partial success, and failure in an in-app status area that persists after any toast disappears.
- On 401/403, refresh config once, retry once, then show the required scope and whether it is pending or denied.
- On 5xx/network errors, show failing stage, retryability, and endpoint. Do not surface only "500".

### Endpoints
Paths are relative to `/api/v1/rooms/{roomId}/`.
- `GET status`, `GET locations` are safe.
- `GET chat/history?limit=N` requires `read_chat`; `GET memory/search?query=...` requires `read_memory`.
- `GET calendar/events` requires `read_calendar`; `POST events` requires `write_event`; `POST chat` requires `send_chat`.

Declare required scopes in `workspace/apps/<name>/nexus.json`, e.g. `{ "requested_scopes": ["write_event", "send_chat"] }`.

### Required payload shapes
Verify against `api/schemas.py` before claiming a send button is fixed.
- `POST chat` / `ChatRequest`: send `{ "message": "...", "source": "atelier:<app>", "client_message_id": "..." }`; never `{ "text": "..." }`.
- `POST events` / `EventRequest`: send `{ "event_type": "...", "source": "atelier:<app>", "summary": "...", "details": {...}, "event_data": {...}, "importance": "normal|high|critical" }`; never `{ "type": "...", "content": "..." }`.
- A 422 response usually means token/scope is fine but JSON body is wrong. Read the body and fix the exact field.

For record/report buttons, POST `events` first so data is saved through the lightweight event path.
Treat POST `chat` as optional follow-up because it waits for persona response generation.

### Final report
Report requested scopes, observed granted/pending scopes, endpoints called, payload shape with secrets redacted, validation performed, and files/functions to inspect if the button still fails.
