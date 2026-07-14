---
id: atelier-persona-creative-handoff
title: Persona Creative Handoff for atelier apps
summary: アトリエPWAの文言・アイコン・雰囲気にペルソナらしさが必要なとき。実装役が代筆しすぎず、ペルソナへ表現依頼を渡すための手順です。
applies_to:
  workspace_kinds: [persona, persona_project_read]
  keywords: [app, pwa, アプリ, webアプリ, アイコン, icon, 文言, 台詞, 口調, ペルソナ, persona, copy]
  require_keywords: true
priority: 80
max_chars: 1000
---

## Persona Creative Handoff
You are the implementation agent, not the persona. For persona-flavored UI copy or app icons, do not
invent deeply personal wording from generic assumptions or shared samples.

When a new or polished PWA needs distinctive voice, include a `Persona Creative Handoff` in the final
report so the persona can provide final language and icon direction.

Icon handoff: the persona can generate an image after implementation, then call `set_atelier_app_icon`
with the generated image path or result string. Mention whether the app still uses the default icon.

### What to include
- UI text slots that benefit from persona voice: `app_title`, `primary_button`, `success_message`,
  `partial_success_message`, `error_message`, `empty_state`, `notification_text`, or app-specific slots.
- Files, selectors, or functions to update for each slot.
- Neutral fallback text for each slot. Keep it plain; do not mimic the persona deeply.
- A simple icon concept and image prompt, marked `persona-review-needed`.
- Whether the app currently uses the default icon or already has `icon.png` / `icon-maskable.png`.

For prototypes or bug fixes, the default icon is acceptable. For a finished app, recommend a
persona-reviewed icon, but do not block technical completion unless the user requested polish.

### Privacy rule
Do not copy room-specific names, private relationship terms, or persona-only wording into reusable
templates, tests, or shared playbooks. Ask the persona for those replacements in the active room.
