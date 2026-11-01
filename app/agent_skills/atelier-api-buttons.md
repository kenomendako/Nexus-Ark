---
id: atelier-api-buttons
title: Atelier API buttons and Nexus Ark payloads
summary: アトリエPWAのAPI payload、scope、再送、成功/失敗表示を確認する。
applies_to:
  workspace_kinds: [persona, persona_project_read]
  keywords: [app, pwa, アプリ, webアプリ, api, nexus, capabilities, send_chat, write_event, read_items, write_items, payload, scope, token, 送信, ボタン, 権限, 記録, チャット, アイテム, エラー]
  require_keywords: true
priority: 99
max_chars: 1700
---

## API buttons
`./_nexus/config` no-store→apiBase/roomId/token/scopes; `/api/v1/rooms/{roomId}/...`; `requested_scopes` in `workspace/apps/<name>/nexus.json`; `GET capabilities` safe/approval/owner_only; visible feedback/status area: send/success/partial/failure.
Scopes: `send_chat`,`read_chat` history(音楽/通知,no RAW),`read_items` list/images,`write_items` actions,`read_letters`,`write_notes`,`use_voice`,`manage_push`; dream/episodic読取のみ。appsはowner_only開放・owner token要求不可。
chat `{"message":"..."}`; `chat/regenerate` latest only; events `{"event_type":"..."}` not type/content; `items/actions` stable `client_action_id`,gift once,place/pickup inventory,consume only `consume_location`; presence locks writes. `client_request_id` exact replay;有料処理の自動再生成禁止。
`POST chat/thoughts/translate` owner_only `{text}`→`{text}`: masked,cost,no reply/signatures/retry/memory。paid profile/imageもownerのみ・費用表示。401/403 config再取得・再試行各1回、5xx段階/再試行案内、422 responseは不足/不正field。
`GET status`: `persistent_emotions_available`/`persistent_emotions`は申告/日時/参考/表示状態。未申告≠0、期限切れ≠本人終了。`arousal_sample_count`:0=無記録,null=不明。読取のみ。AI/manual明示保存(`taste_profile`空も有効)、自動副作用なし。
Lite owner-only: `response-settings` GET revision; PUT `{key_name,model_name,enable_api_key_rotation,base_revision}`; DELETE `{base_revision}`; stale `409`。`chat/history?include_thoughts=true`はowner明示設定。`key/memo,food,autonomy/settings,chat active/stop`; `/api/v1/lite-travel/{standby,diagnostics,return}`。
`temporary-location`:省略保持、`active:false`もtext/photo/name保持、clearは明示。`saved` GET/POST;重複`409`;画像不正/欠落はtext+flag保持。`.../generate`有料`{image_path,user_hint?,client_request_id}` exact replay。Draft generate/save/load,review/apply。
