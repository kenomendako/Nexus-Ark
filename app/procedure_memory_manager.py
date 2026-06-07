"""Procedural memory manager for reusable autonomous action routines."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import constants


class ProcedureMemoryManager:
    """Manages shared and per-room procedure markdown files."""

    DEFAULT_SHARED_PROCEDURE_ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "default_shared_procedures"

    DEFAULT_SHARED_PROCEDURES = {
        "sample_safe_context_update": """# Sample: 安全な文脈確認と更新

## Metadata
- procedure_id: sample_safe_context_update
- scope: shared
- created_at: 2026-05-24T00:00:00+09:00
- updated_at: 2026-05-24T00:00:00+09:00
- source_timeline_id:

## Purpose
読み取り系ツールで既存文脈を確認してから、必要な差分だけを更新し、最後に結果を短く報告するための共通手順。

## Triggers
- ノート、記憶、Working Memory、Research Threadなどを更新したい時
- 既存文脈を確認せずに書くと重複や矛盾が起きそうな時
- 自律行動や通常応答で「まず確認してから進める」必要がある時

## Steps
1. 目的を一文で決める。何を確認し、何を更新したいのかを明確にする。
2. 関連しそうな読み取りツールを一つだけ選ぶ。例: `read_working_memory`、`read_research_notes`、`read_entity_memory`。
3. 読み取った内容から、今回の目的に直接関係する事実だけを抜き出す。
4. 更新が必要な場合だけ、対応する保存・編集ツールを使う。既存文脈を丸ごと置き換えず、差分を中心にする。
5. 保存後は、何を読んだか、何を変えたか、次に確認すべきことを短く報告する。

## Success Criteria
- 既存文脈を確認してから更新している。
- 更新内容が今回の目的に限定されている。
- ユーザーに、読んだもの・変えたもの・次の確認点が伝わっている。

## Notes
このSkillは書き方サンプルでもある。共通Skillには、人格・口調・関係性・固有の美学を含めない。そうした内容は各ルームのprivate Skillに保存する。
""",
        "autonomy_reflect_and_close": """# Sample: 自律行動後のReflectとTimeline完了

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
""",
        "skill_creation_duplicate_check": """# Sample: Skill作成前の重複確認

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
""",
    }

    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.memory_dir = self.room_dir / "memory"
        self.procedures_dir = self.memory_dir / "procedures"
        self.shared_procedures_dir = Path(constants.ROOMS_DIR) / "_shared" / "memory" / "procedures"
        self.timeline_dir = self.memory_dir / "autonomy_timeline"
        self.procedures_dir.mkdir(parents=True, exist_ok=True)
        self.shared_procedures_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_default_shared_procedures()

    def list_procedures(self, include_shared: bool = True) -> List[Dict[str, str]]:
        procedures = []
        if include_shared:
            procedures.extend(self._list_from_dir(self.shared_procedures_dir, scope="shared"))
        procedures.extend(self._list_from_dir(self.procedures_dir, scope="private"))
        return procedures

    def read_procedure(self, procedure_id: str) -> str:
        path = self._procedure_path(procedure_id, scope="")
        if not path.exists():
            raise FileNotFoundError(f"Procedureが見つかりません: {procedure_id}")
        return path.read_text(encoding="utf-8")

    def save_raw_procedure(
        self,
        procedure_id: str,
        content: str,
        scope: str = "private",
    ) -> Dict[str, str]:
        procedure_id = self._safe_id(procedure_id)
        if not procedure_id:
            raise ValueError("procedure_id が必要です。")
        content = str(content or "").strip()
        if not content:
            raise ValueError("content が空です。")
        scope = self._normalize_scope(scope)
        path = self._procedure_path(procedure_id, scope=scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
        return {"procedure_id": procedure_id, "path": str(path), "scope": scope}

    def delete_procedure(self, procedure_id: str) -> Dict[str, str]:
        path = self._procedure_path(procedure_id, scope="")
        if not path.exists():
            raise FileNotFoundError(f"Procedureが見つかりません: {procedure_id}")
        path.unlink()
        return {"procedure_id": path.stem, "path": str(path)}

    def _ensure_default_shared_procedures(self) -> None:
        for procedure_id, content in self._load_default_shared_procedures().items():
            path = self.shared_procedures_dir / f"{procedure_id}.md"
            if path.exists():
                continue
            path.write_text(content.strip() + "\n", encoding="utf-8")

    def _load_default_shared_procedures(self) -> Dict[str, str]:
        defaults = dict(self.DEFAULT_SHARED_PROCEDURES)
        assets_dir = self.DEFAULT_SHARED_PROCEDURE_ASSETS_DIR
        if not assets_dir.exists():
            return defaults
        for path in sorted(assets_dir.glob("*.md")):
            procedure_id = self._safe_id(path.stem)
            if not procedure_id:
                continue
            try:
                defaults[procedure_id] = path.read_text(encoding="utf-8")
            except Exception:
                continue
        return defaults

    def save_procedure(
        self,
        procedure_id: str,
        title: str,
        purpose: str,
        steps: List[str],
        triggers: Optional[List[str]] = None,
        success_criteria: str = "",
        source_timeline_id: str = "",
        notes: str = "",
        scope: str = "private",
    ) -> Dict[str, str]:
        procedure_id = self._safe_id(procedure_id or title)
        if not procedure_id:
            raise ValueError("procedure_id または title が必要です。")
        scope = self._normalize_scope(scope)
        title = title.strip() or procedure_id.replace("_", " ")
        steps = [str(step).strip() for step in (steps or []) if str(step).strip()]
        if not steps:
            raise ValueError("steps が空です。")

        now = self._now()
        lines = [
            f"# {title}",
            "",
            "## Metadata",
            f"- procedure_id: {procedure_id}",
            f"- scope: {scope}",
            f"- created_at: {now}",
            f"- updated_at: {now}",
            f"- source_timeline_id: {source_timeline_id or ''}",
            "",
            "## Purpose",
            purpose.strip() or "この手順を使う目的は未記入。",
            "",
            "## Triggers",
        ]
        trigger_items = [str(item).strip() for item in (triggers or []) if str(item).strip()]
        if trigger_items:
            lines.extend(f"- {item}" for item in trigger_items)
        else:
            lines.append("- 類似する自律行動を再開・深化したい時")

        lines.extend(["", "## Steps"])
        for i, step in enumerate(steps, 1):
            lines.append(f"{i}. {step}")

        lines.extend([
            "",
            "## Success Criteria",
            success_criteria.strip() or "次回アクション、関連する記憶、未解決の問いが残っている。",
            "",
            "## Notes",
            notes.strip() or "運用しながら改善する。",
            "",
        ])

        path = self._procedure_path(procedure_id, scope=scope)
        path.write_text("\n".join(lines), encoding="utf-8")
        return {"procedure_id": procedure_id, "title": title, "path": str(path), "scope": scope}

    def create_from_timeline(
        self,
        timeline_id: str,
        procedure_id: str = "",
        title: str = "",
        purpose: str = "",
    ) -> Dict[str, str]:
        timeline_id = self._safe_timeline_id(timeline_id)
        if not timeline_id:
            raise ValueError("timeline_id が必要です。")
        records = self._load_timeline_records(timeline_id)
        if not records:
            raise FileNotFoundError(f"timeline_id の記録が見つかりません: {timeline_id}")

        steps = self._steps_from_records(records)
        if not steps:
            raise ValueError("手順化できるstepが見つかりません。")

        inferred_title = title.strip() or self._infer_title(records, timeline_id)
        inferred_purpose = purpose.strip() or self._infer_purpose(records)
        inferred_id = procedure_id or inferred_title
        triggers = self._infer_triggers(records)
        success_criteria = self._infer_success_criteria(records)
        notes = f"source_timeline_id={timeline_id} から自動抽出。必要に応じて人間またはAIが編集する。"
        return self.save_procedure(
            procedure_id=inferred_id,
            title=inferred_title,
            purpose=inferred_purpose,
            steps=steps,
            triggers=triggers,
            success_criteria=success_criteria,
            source_timeline_id=timeline_id,
            notes=notes,
            scope="private",
        )

    def _list_from_dir(self, procedures_dir: Path, scope: str) -> List[Dict[str, str]]:
        procedures = []
        for path in sorted(procedures_dir.glob("*.md")):
            title = path.stem.replace("_", " ")
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("# "):
                        title = line[2:].strip() or title
                        break
            except Exception:
                pass
            procedures.append({
                "procedure_id": path.stem,
                "title": title,
                "path": str(path),
                "scope": scope,
            })
        return procedures

    def _load_timeline_records(self, timeline_id: str) -> List[Dict[str, Any]]:
        if not self.timeline_dir.exists():
            return []
        records = []
        for path in sorted(self.timeline_dir.glob("autonomy_steps_*.jsonl")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if record.get("timeline_id") == timeline_id:
                        records.append(record)
            except Exception:
                continue
        return records

    def _steps_from_records(self, records: List[Dict[str, Any]]) -> List[str]:
        steps = []
        for record in records:
            if record.get("event_type") != "step":
                continue
            step_type = record.get("step_type", "")
            summary = record.get("summary", "").strip()
            if not summary:
                continue
            tool_name = record.get("tool_name", "")
            selected_action = record.get("selected_action", "")
            if step_type == "observe":
                steps.append(f"観察: {summary}")
            elif step_type == "orient":
                steps.append(f"判断: {summary}")
            elif step_type == "decide":
                action = f"（選択: {selected_action}）" if selected_action else ""
                steps.append(f"決定: {summary}{action}")
            elif step_type == "act":
                tool = f" using `{tool_name}`" if tool_name else ""
                steps.append(f"実行: {summary}{tool}")
            elif step_type == "reflect":
                steps.append(f"振り返り: {summary}")
        return steps

    def _infer_title(self, records: List[Dict[str, Any]], timeline_id: str) -> str:
        for record in records:
            if record.get("event_type") == "timeline_start":
                query = record.get("query", "").strip()
                trigger = record.get("trigger", "").strip()
                if query:
                    return f"Procedure: {query}"
                if trigger:
                    return f"Procedure: {trigger}"
        return f"Procedure: {timeline_id}"

    def _infer_purpose(self, records: List[Dict[str, Any]]) -> str:
        for record in records:
            if record.get("event_type") == "timeline_start":
                motivation = record.get("motivation", "").strip()
                query = record.get("query", "").strip()
                if motivation:
                    return motivation
                if query:
                    return f"{query} に関する自律行動を再現・改善する。"
        for record in records:
            if record.get("step_type") == "decide" and record.get("summary"):
                return record.get("summary")
        return "過去に成功した自律行動を再利用しやすくする。"

    def _infer_triggers(self, records: List[Dict[str, Any]]) -> List[str]:
        triggers = []
        for record in records:
            if record.get("event_type") != "timeline_start":
                continue
            for key in ("trigger", "query"):
                value = record.get(key, "").strip()
                if value:
                    triggers.append(value)
        return self._dedupe(triggers)

    def _infer_success_criteria(self, records: List[Dict[str, Any]]) -> str:
        for record in reversed(records):
            if record.get("event_type") == "timeline_complete" and record.get("summary"):
                return record.get("summary")
            if record.get("step_type") == "reflect":
                details = record.get("details")
                if isinstance(details, dict) and details.get("next_action"):
                    return f"次回アクションが残っている: {details.get('next_action')}"
                if record.get("summary"):
                    return record.get("summary")
        return "Reflectまで完了し、次の一手が残っている。"

    def _procedure_path(self, procedure_id: str, scope: str = "") -> Path:
        raw_id = str(procedure_id or "").strip()
        if raw_id.startswith("shared:"):
            return self.shared_procedures_dir / f"{self._safe_id(raw_id.split(':', 1)[1])}.md"
        if raw_id.startswith("private:"):
            return self.procedures_dir / f"{self._safe_id(raw_id.split(':', 1)[1])}.md"

        safe_id = self._safe_id(raw_id)
        normalized_scope = self._normalize_scope(scope)
        if normalized_scope == "shared":
            return self.shared_procedures_dir / f"{safe_id}.md"
        private_path = self.procedures_dir / f"{safe_id}.md"
        if private_path.exists() or normalized_scope == "private":
            return private_path
        return self.shared_procedures_dir / f"{safe_id}.md"

    def _normalize_scope(self, scope: str) -> str:
        return "shared" if str(scope or "").strip().lower() == "shared" else "private"

    @staticmethod
    def _safe_id(value: str) -> str:
        value = str(value or "").strip().lower()
        safe = "".join(c if c.isalnum() else "_" for c in value)
        while "__" in safe:
            safe = safe.replace("__", "_")
        return safe.strip("_")[:100]

    @staticmethod
    def _safe_timeline_id(value: str) -> str:
        return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(value or "").strip())[:120]

    @staticmethod
    def _dedupe(values: List[str]) -> List[str]:
        seen = set()
        result = []
        for value in values:
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")
