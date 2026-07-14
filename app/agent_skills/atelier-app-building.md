---
id: atelier-app-building
title: Building atelier web apps / PWAs (core)
summary: アトリエにWebアプリ/PWAを作る・直すときの基礎。配置場所、Nexus Ark APIの入口、PWA配信、CSP、プレビュー確認を実行役が踏まえます。
applies_to:
  workspace_kinds: [persona, persona_project_read]
  task_kinds: []
  keywords: [app, pwa, アプリ, webアプリ, manifest, service worker]
priority: 100
max_chars: 1200
---

## Atelier web app / PWA core
A web app placed at `workspace/apps/<name>/index.html` is served by Nexus Ark. Keep the app
self-contained in that folder: `index.html`, optional scripts/styles, `nexus.json`, and optional
local `icon.png` / `icon-maskable.png`.

### Runtime config and API
If the app calls Nexus Ark, fetch runtime config with a relative path and no cache:

```js
const cfg = await (await fetch("./_nexus/config", { cache: "no-store" })).json();
// cfg = { apiBase, roomId, token, grantedScopes, pendingScopes, appId, expiresIn }
```

Tokens are short-lived. Re-fetch before API calls; on 401/403, refresh once and retry once. API
payloads, scopes, and button feedback are covered by `atelier-api-buttons`.

### Persona / creative surface
You are the implementation agent, not the persona. Do not invent deeply personal wording from generic
assumptions. For polished app copy/icons, `atelier-persona-creative-handoff` tells you to list UI
text slots, neutral fallbacks, files/selectors, and icon concepts for the persona to review.

### PWA installability
Nexus Ark auto-injects the installable PWA manifest, app icons, apple-touch-icon, and service worker.
Do not add your own manifest, `manifest.json`, or service worker. Do not reference external icon URLs;
put custom icons in the app folder as local PNG files.

### CSP and bundled libraries
The atelier CSP is strict: no CDN scripts and no `unsafe-eval`. Use `/atelier/_lib/...` libraries
when needed. Vue DOM templates and browser-side template compilation are unsafe; prefer Vanilla JS
for small apps.

### Completion checks
- Preview the app after creating or modifying it. Any page error mentioning `unsafe-eval`,
  `new Function`, or string evaluation must be fixed in app code.
- Verify a phone-sized viewport: main action reachable, current state visible, and errors concrete.
- Existing apps can be modified in place under `workspace/apps/<name>/`; Nexus Ark invalidates the
  app service-worker cache when app files change.
