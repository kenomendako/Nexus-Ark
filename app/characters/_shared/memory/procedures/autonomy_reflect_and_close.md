# Sample: 自律行動後のReflectとTimeline完了

## Metadata
- procedure_id: autonomy_reflect_and_close
- scope: shared
- created_at: 2026-05-25T00:00:00+09:00
- updated_at: 2026-05-25T00:00:00+09:00
- source_timeline_id:

## Purpose
自律行動でノート、記憶、Working Memory、外部確認などを行ったあと、結果をReflectし、timelineを閉じて次回アクションを残すための共通手順。

## Triggers
- `start_autonomy_timeline` を開始した行動が一区切りついた時
- ノート、Working Memory、目標、Research Threadなどの更新に成功した時
- ループ上限が近く、追加作業より後始末を優先すべき時

## Steps
1. 今回の行動で実際に完了したことを一文でまとめる。
2. `reflect_after_action` を呼び、結果分類、次回アクション、関連するResearch Thread / Working Memory / Goalを記録する。
3. 未完了の追加作業はその場で広げず、`next_action` または未解決の問いとして残す。
4. `complete_autonomy_timeline` を呼び、timelineを `completed` または適切な状態で閉じる。
5. ユーザーへ報告する場合は、何を更新し、何を次回に残したかだけを短く伝える。

## Success Criteria
- `reflect_after_action` が成功している。
- `complete_autonomy_timeline` が成功している。
- 次回アクションが具体的で、再開しやすい。
- 追加の通常ツールを無理に広げず、行動が安全に閉じている。

## Notes
このSkillは人格・口調・関係性を規定しない。後始末の型だけを共有するための共通Skill。
