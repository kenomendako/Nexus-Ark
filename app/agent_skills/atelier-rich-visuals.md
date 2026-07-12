---
id: atelier-rich-visuals
title: Rich visuals for atelier apps (Three.js / Vue / WebGL2)
summary: アトリエでThree.js、WebGL2、Vue 3などを使うリッチなWebアプリを作るとき。同梱ライブラリの読み込みパスと、スマホPWA向けの軽量な実装作法を実行役が踏まえます。
applies_to:
  workspace_kinds: [persona, persona_project_read]
  keywords: [three, three.js, webgl, webgl2, 3d, canvas, vue, vue3, リッチ, 3d表現, 三次元, ビジュアル]
  require_keywords: true
priority: 95
max_chars: 3200
---

## Rich atelier app visuals
Nexus Ark serves bundled JS libraries from the same origin as atelier apps. Do not use CDN URLs:
the atelier CSP keeps `script-src 'self' 'unsafe-inline'` and intentionally does not allow
`unsafe-eval`; installed PWAs must work offline.

### Available bundled libraries
- Three.js ES module: `/atelier/_lib/three-0.185.1.module.min.js`
  ```html
  <script type="module">
    import * as THREE from "/atelier/_lib/three-0.185.1.module.min.js";
  </script>
  ```
- Vue 3 global build: `/atelier/_lib/vue-3.5.39.global.prod.js`
  ```html
  <script src="/atelier/_lib/vue-3.5.39.global.prod.js"></script>
  <script>
    const { createApp } = Vue;
    createApp({ data: () => ({ count: 0 }) }).mount("#app");
  </script>
  ```

### Three.js practice
- Use one primary `<canvas>` and create the renderer with that canvas.
- Add a resize handler; set renderer size from the canvas/container, not a fixed desktop size.
- Cap pixel ratio on phones, e.g. `renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))`.
- Drive animation with `requestAnimationFrame`; pause or simplify expensive work when the tab is hidden.
- Listen for `webglcontextlost` and `webglcontextrestored`; show a clear fallback if the context is lost.
- Avoid heavy geometry, large textures, remote model fetches, or effects that make first paint exceed about 3 seconds.

### Plain WebGL2 practice
- WebGL2 needs no bundled library. Keep shaders local: inline shader `<script>` blocks or JS template
  strings. Do not fetch shader files.
- Detect unsupported WebGL2 and provide a non-canvas fallback.

### Vue 3 practice
- Prefer Vanilla JS for small atelier apps. The CSP forbids `eval` / `new Function`, so Vue runtime
  template compilation is blocked.
- Do NOT use Vue DOM templates, `{{ moustache }}` bindings in `index.html`, `v-if`, `v-model`,
  `@click`, or a `template:` string unless you also provide a build-free path that does not compile
  templates in the browser.
- If Vue is truly needed in a no-build app, use only render functions such as `h()` and direct event
  handlers. If that is overkill, use plain DOM APIs instead.
- Do not create JSX, SFC files, Vite configs, or npm build steps.
- Keep state small. Fetch `./_nexus/config` fresh before Nexus Ark API calls as described in the
  atelier app playbook.

### CSP validation
- After creating or modifying an app, run `preview_atelier_app`. Treat page errors mentioning
  `unsafe-eval`, `new Function`, or `Evaluating a string as JavaScript` as a hard failure.
- Fix those failures by removing browser-side template compilers or generated-code evaluators, not by
  asking to relax the atelier CSP.

### Mobile PWA constraints
- The main path is phone use through QR/install. Prioritize responsive controls, stable layout dimensions,
  readable text, and quick recovery from offline mode.
- Broad style guidance lives in the frontend-design playbook; do not duplicate it here.
