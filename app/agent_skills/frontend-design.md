---
id: frontend-design
title: Frontend visual design (make it look intentional)
summary: アトリエのアプリを「それらしく・見栄え良く」作ってほしいとき。配色・書体・レイアウトを題材に合わせて意図的に決め、ありがちなAIっぽい見た目を避けるための指針です。
applies_to:
  workspace_kinds: [persona, persona_project_read]
  keywords: [design, ui, ux, デザイン, 見た目, レイアウト, 配色, タイポ, おしゃれ, スタイル]
priority: 90
max_chars: 2200
---

## Frontend visual design playbook
When you build or restyle a UI (an atelier web app / PWA), act as the design lead: aim for a
distinctive, intentional look that fits the subject — not a generic template.

### Avoid the default "AI app" look
Steer away from the usual tells of templated output: cream/beige backgrounds with a safe serif;
near-black backgrounds with neon accents; a centered hero followed by three identical feature cards.
If the design could belong to any app, it is not finished.

### Make deliberate choices, grounded in the subject
- Let the app's purpose and world drive the palette, type, and shapes. A workout tracker, a recipe
  box, and a home-device panel should not look alike.
- Decide a small, consistent design-token set first: 2-3 colors (one clear accent), one or two
  typefaces, a spacing scale, a corner radius, and one "signature" detail. Reuse them everywhere.
- Typography carries personality. Choose type with intent; give body text generous line-height and a
  comfortable line length. Establish an obvious size/weight hierarchy.
- Use structure to encode meaning: the most important element should be visually dominant. Don't give
  everything the same weight.
- Motion is seasoning — a little, and purposeful (feedback, state changes). Avoid gratuitous animation.

### Readability and writing
- Copy is part of the design: plain, active language; short labels; clear empty and error states.
- Keep contrast legible and respect tap-target size on phones (people install this as a PWA).

### Process
1. Brief: in one line, what is this app and who uses it, and what feeling should it give?
2. Plan the tokens (colors, type, spacing, signature) before coding.
3. Build the UI against those tokens.
4. Critique once: open it and ask "does this look intentional and specific, or generic?" Fix the
   weakest part. One focused revision beats endless tweaks.

Pairs with the atelier-app-building playbook, which covers the Nexus Ark API and PWA plumbing.
