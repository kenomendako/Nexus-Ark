---
id: codebase-task
title: Changing code safely (read, minimal change, verify)
summary: コードの修正・バグ直し・機能追加・リファクタを委任するとき。既存に合わせ最小限に変え、必ず検証してから報告する作法を実行役が踏まえます。
applies_to:
  workspace_kinds: []
  task_kinds: []
  keywords: [code, codebase, コード, bug, バグ, 修正, refactor, リファクタ, 関数, クラス, test, テスト, 実装, 不具合]
priority: 70
max_chars: 2500
---

## Codebase change playbook
You are modifying a real codebase. Favor small, safe, verifiable changes over clever rewrites.

### Method
1. Read before you write. Find the relevant files and understand the surrounding conventions
   (naming, style, error handling, comment density) before editing. Match them.
2. Make the smallest change that satisfies the task. Do not refactor unrelated code, rename things,
   or reformat files you were not asked to touch.
3. Keep edits reversible and localized. Prefer editing existing functions over adding parallel ones.
4. Verify your change: run/inspect the relevant tests or a quick check. If you cannot verify, say so.

### What to deliver
- A concise summary of what you changed and why.
- The list of files you touched.
- The verification you ran and its result (or clearly state it was not verified).
- Any remaining risks, follow-ups, or assumptions.

### Cautions
- Do not delete or overwrite files you did not create or were not asked to change.
- If the task needs files outside the allowed workspace/scopes, stop and ask — do not narrow the
  task silently and report it as done.
- Do not introduce new dependencies unless the task requires it and it fits the project.
