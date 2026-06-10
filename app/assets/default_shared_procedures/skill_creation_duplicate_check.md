# Sample: Skill作成前の重複確認

## Metadata
- procedure_id: skill_creation_duplicate_check
- scope: shared
- created_at: 2026-05-25T00:00:00+09:00
- updated_at: 2026-05-25T00:00:00+09:00
- source_timeline_id:

## Purpose
新しいSkillを保存する前に、既存Skillとの重複や粒度のズレを確認し、必要以上にSkillを増やさないための共通手順。

## Triggers
- 行動が成功し、次回も同じ型で使えそうだと感じた時
- `save_procedure` または `create_procedure_from_timeline` を使いたくなった時
- 既存Skillを改善すべきか、新規Skillとして保存すべきか迷った時

## Steps
1. `list_procedures` で既存のshared/private Skillを確認する。
2. 類似しそうなSkillがあれば `read_procedure` で本文を読む。
3. 既存Skillで足りる場合は新規保存せず、そのSkillを現在文脈に合わせて使う。
4. 既存Skillの一部改善で足りる場合は、重複Skillを作らず既存Skillの改善として保存する。
5. 明確に新しい反復手順で、成功条件とトリガーが説明できる場合だけ `save_procedure` または `create_procedure_from_timeline` を使う。
6. scopeは原則として、人格・関係性・口調・個別の美学を含むものは `private`、機能的で汎用な作業手順だけ `shared` にする。

## Success Criteria
- 保存前に既存Skillを確認している。
- 重複Skillを増やしていない。
- 新規保存する場合、トリガー、手順、成功条件が明確になっている。
- `shared` と `private` の境界が守られている。

## Notes
このSkillはSkillを増やしすぎないためのメタSkill。迷ったらprivateに保存し、shared化は人格非依存だと確認できてから行う。
