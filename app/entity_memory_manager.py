# entity_memory_manager.py

import os
import json
from pathlib import Path
from datetime import datetime
import time
import re
import difflib
import unicodedata
import shutil
import constants
import config_manager
import utils

class EntityMemoryManager:
    """
    Manages structured memories about specific entities (people, topics, objects).
    Stores data in Markdown files under room/memory/entities/
    """
    def __init__(self, room_name: str):
        self.room_name = room_name
        self.room_dir = Path(constants.ROOMS_DIR) / room_name
        self.entities_dir = self.room_dir / "memory" / "entities"
        self.entities_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.entities_dir / "_index.json"

    def _get_entity_path(self, entity_name: str) -> Path:
        # Sanitize entity name for filename
        safe_name = "".join([c for c in entity_name if c.isalnum() or c in (' ', '_', '-')]).rstrip()
        if not safe_name:
            safe_name = "UnnamedEntity"
        return self.entities_dir / f"{safe_name}.md"

    def _now_iso(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _normalize_name(self, name: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(name or "")).strip().lower()
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = re.sub(r"[「」『』【】\[\]()（）]", "", normalized)
        normalized = re.sub(r"(さん|ちゃん|くん|様|さま)$", "", normalized)
        return normalized.strip()

    def _make_entity_id(self, entity_name: str, index: dict | None = None) -> str:
        safe = re.sub(r"[^0-9a-zA-Z_]+", "_", self._normalize_name(entity_name)).strip("_")
        if not safe:
            safe = "entity"
        entity_id = f"ent_{safe[:48]}"
        if not index or entity_id not in index.get("entities", {}):
            return entity_id
        suffix = 2
        while f"{entity_id}_{suffix}" in index.get("entities", {}):
            suffix += 1
        return f"{entity_id}_{suffix}"

    def _default_index(self) -> dict:
        return {"version": 1, "entities": {}}

    def _read_index_file(self) -> dict:
        if not self.index_path.exists():
            return self._default_index()
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return self._default_index()
            data.setdefault("version", 1)
            data.setdefault("entities", {})
            if not isinstance(data["entities"], dict):
                data["entities"] = {}
            return data
        except Exception as e:
            print(f"Entity index load error ({self.index_path}): {e}")
            return self._default_index()

    def _save_index(self, index: dict) -> None:
        tmp_path = self.index_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.index_path)

    def _entity_files(self) -> list[Path]:
        return sorted(
            f for f in self.entities_dir.glob("*.md")
            if f.is_file() and not f.name.startswith("_")
        )

    def _new_index_meta(self, entity_name: str, filename: str, now: str | None = None) -> dict:
        now = now or self._now_iso()
        return {
            "canonical_name": entity_name,
            "filename": filename,
            "aliases": [],
            "entity_type": "unknown",
            "status": "active",
            "parent_id": None,
            "related_ids": [],
            "created_at": now,
            "updated_at": now,
            "last_recalled_at": None,
            "last_read_at": None,
            "last_written_at": now,
            "last_used_at": None,
            "recall_count": 0,
            "read_count": 0,
            "write_count": 0,
            "use_count": 0,
            "confidence": 0.5,
            "importance": 0.5,
            "merge_candidates": [],
        }

    def _ensure_index(self) -> dict:
        index = self._read_index_file()
        entities = index.setdefault("entities", {})
        changed = False
        known_filenames = {
            str(meta.get("filename", ""))
            for meta in entities.values()
            if isinstance(meta, dict)
        }

        for path in self._entity_files():
            if path.name in known_filenames:
                continue
            entity_name = path.stem
            entity_id = self._make_entity_id(entity_name, index)
            entities[entity_id] = self._new_index_meta(entity_name, path.name)
            known_filenames.add(path.name)
            changed = True

        existing_filenames = {path.name for path in self._entity_files()}
        for entity_id, meta in list(entities.items()):
            if not isinstance(meta, dict):
                del entities[entity_id]
                changed = True
                continue
            defaults = self._new_index_meta(
                meta.get("canonical_name") or Path(str(meta.get("filename", entity_id))).stem,
                meta.get("filename") or f"{entity_id}.md",
                meta.get("created_at") or self._now_iso(),
            )
            for key, value in defaults.items():
                if key not in meta:
                    meta[key] = value
                    changed = True
            if meta.get("filename") not in existing_filenames and meta.get("status") != "archived":
                meta["status"] = "archived"
                meta["updated_at"] = self._now_iso()
                changed = True

        if changed:
            self._save_index(index)
        return index

    def get_index(self) -> dict:
        return self._ensure_index()

    def _find_meta_by_name(self, entity_name: str, index: dict | None = None) -> tuple[str | None, dict | None]:
        index = index or self._ensure_index()
        target = self._normalize_name(entity_name)
        matches = []
        for entity_id, meta in index.get("entities", {}).items():
            if not isinstance(meta, dict):
                continue
            names = [meta.get("canonical_name", ""), *meta.get("aliases", [])]
            if any(self._normalize_name(name) == target for name in names):
                status = meta.get("status", "active")
                status_rank = {"active": 0, "dormant": 1, "archived": 2}.get(status, 3)
                matches.append((status_rank, entity_id, meta))
        filename = self._get_entity_path(entity_name).name
        for entity_id, meta in index.get("entities", {}).items():
            if isinstance(meta, dict) and meta.get("filename") == filename:
                status = meta.get("status", "active")
                status_rank = {"active": 0, "dormant": 1, "archived": 2}.get(status, 3)
                matches.append((status_rank, entity_id, meta))
        if matches:
            matches.sort(key=lambda item: (item[0], self._normalize_name(item[2].get("canonical_name", "")) != target))
            _, entity_id, meta = matches[0]
            return entity_id, meta
        return None, None

    def _find_meta_by_id(self, entity_id: str, index: dict | None = None) -> tuple[str | None, dict | None]:
        index = index or self._ensure_index()
        meta = index.get("entities", {}).get(entity_id)
        if isinstance(meta, dict):
            return entity_id, meta
        return None, None

    def _get_entity_path_from_meta(self, meta: dict | None, fallback_name: str = "") -> Path:
        if meta and meta.get("filename"):
            return self.entities_dir / meta["filename"]
        return self._get_entity_path(fallback_name)

    def _clean_merge_text(self, text: str) -> str:
        if not text:
            return ""
        cleaned_lines = []
        skipping_merge_note = False
        for raw_line in str(text).splitlines():
            line = raw_line.rstrip()
            lowered = line.lower().strip()
            if lowered.startswith("--- merged entity memory ---"):
                skipping_merge_note = True
                continue
            if skipping_merge_note:
                if not line.strip():
                    skipping_merge_note = False
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines).strip()
        cleaned = re.sub(r"^#\s*Entity Memory:\s*.*$", "", cleaned, flags=re.MULTILINE).strip()
        cleaned = re.sub(r"^Created:\s*.*$", "", cleaned, flags=re.MULTILINE).strip()
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _build_entity_merge_prompt(self, target_name: str, source_name: str, target_content: str, source_content: str, reason: str = "") -> str:
        reason_block = f"\n【統合理由】\n{reason}\n" if reason else ""
        return f"""あなたはエンティティ記憶の統合作業者である。
以下の二つは同じ対象に関する別バージョンの記憶だとみなし、一本のエンティティ記憶に再構成せよ。

【対象名】
{target_name}

【統合元】
{source_name}

【既存本文】
{target_content}

【追加本文】
{source_content}
{reason_block}
【統合ルール】
1. 単なる追記や上下併記にはしない。
2. 重複する説明は一つにまとめる。
3. 事実が両方にまたがる場合は、より包括的で自然な表現に再構成する。
4. 矛盾がある場合は、断定を弱めて「記録上は〜」「現時点では〜」の形で統合する。
5. 追加された注記や統合ログは本文に残さない。
6. 出力は統合後の本文のみとし、前置きや解説は一切書かない。

【推奨構成】
# Entity Memory: {target_name}
## 定義と本質
## 構造と機能
## 私との関わり
## 現在の課題と展望
"""

    def _heuristic_merge_entity_content(self, target_name: str, target_content: str, source_content: str) -> str:
        target_body = self._clean_merge_text(target_content)
        source_body = self._clean_merge_text(source_content)
        merged_chunks: list[str] = []
        seen = set()

        for chunk in (target_body, source_body):
            for part in re.split(r"\n{2,}", chunk):
                text = part.strip()
                if not text:
                    continue
                normalized = re.sub(r"\s+", " ", text)
                if normalized in seen:
                    continue
                seen.add(normalized)
                merged_chunks.append(text)

        merged_body = "\n\n".join(merged_chunks).strip()
        if not merged_body:
            merged_body = source_body or target_body
        if not merged_body.startswith("#"):
            merged_body = f"# Entity Memory: {target_name}\n\n{merged_body}".strip()
        return re.sub(r"\n{3,}", "\n\n", merged_body).strip()

    def _consolidate_entity_merge_content(
        self,
        target_name: str,
        source_name: str,
        target_content: str,
        source_content: str,
        reason: str = "",
        api_key: str | None = None,
    ) -> str:
        merge_api_key = api_key
        if merge_api_key is None:
            try:
                _, model_name, _ = config_manager.get_effective_internal_model("processing")
                merge_api_key = config_manager.get_active_gemini_api_key(self.room_name, model_name=model_name)
            except Exception:
                merge_api_key = None

        if merge_api_key:
            prompt = self._build_entity_merge_prompt(target_name, source_name, target_content, source_content, reason=reason)
            try:
                response, _ = self._invoke_llm("processing", prompt, merge_api_key)
                if isinstance(response, list):
                    response = "\n".join([str(item) for item in response])
                response = str(response or "").strip()
                if response:
                    if not response.lstrip().startswith("#"):
                        response = f"# Entity Memory: {target_name}\n\n{response}"
                    return re.sub(r"\n{3,}", "\n\n", response).strip()
            except Exception as e:
                print(f"Entity merge synthesis error for {target_name}: {e}")

        return self._heuristic_merge_entity_content(target_name, target_content, source_content)

    def _read_entry_content(self, entity_name: str) -> str:
        index = self._ensure_index()
        entity_id, meta = self._find_meta_by_name(entity_name, index)
        path = self._get_entity_path_from_meta(meta, entity_name)
        if not path.exists() and meta and meta.get("archived_file"):
            archived_path = self.entities_dir / str(meta["archived_file"])
            if archived_path.exists():
                path = archived_path
        if not path.exists() and meta and meta.get("merged_into"):
            target_id = meta.get("merged_into")
            _, target_meta = self._find_meta_by_id(target_id, index)
            if target_meta:
                target_path = self._get_entity_path_from_meta(target_meta, target_meta.get("canonical_name", target_id))
                if target_path.exists():
                    path = target_path
        if not path.exists():
            return f"Error: No entity memory found for '{entity_name}'."
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def _increment_stat(self, entity_name: str, count_key: str, time_key: str) -> None:
        index = self._ensure_index()
        _, meta = self._find_meta_by_name(entity_name, index)
        if not meta:
            return
        now = self._now_iso()
        meta[count_key] = int(meta.get(count_key, 0) or 0) + 1
        meta[time_key] = now
        meta["updated_at"] = now
        self._save_index(index)

    def mark_recalled(self, entity_name: str) -> None:
        self._increment_stat(entity_name, "recall_count", "last_recalled_at")

    def read_entry_by_id(self, entity_id: str) -> str:
        index = self._ensure_index()
        _, meta = self._find_meta_by_id(entity_id, index)
        if not meta:
            return f"Error: No entity memory found for '{entity_id}'."
        entity_name = meta.get("canonical_name", entity_id)
        content = self._read_entry_content(entity_name)
        if not content.startswith("Error:"):
            now = self._now_iso()
            meta["read_count"] = int(meta.get("read_count", 0) or 0) + 1
            meta["last_read_at"] = now
            meta["updated_at"] = now
            self._save_index(index)
        return content

    def set_entity_status_by_id(self, entity_id: str, status: str) -> bool:
        index = self._ensure_index()
        _, meta = self._find_meta_by_id(entity_id, index)
        if not meta:
            return False
        meta["status"] = status
        meta["updated_at"] = self._now_iso()
        self._save_index(index)
        return True

    def delete_entry_by_id(self, entity_id: str) -> bool:
        index = self._ensure_index()
        _, meta = self._find_meta_by_id(entity_id, index)
        if not meta:
            return False
        path = self._get_entity_path_from_meta(meta, meta.get("canonical_name", entity_id))
        if path.exists():
            path.unlink()
            meta["status"] = "archived"
            meta["updated_at"] = self._now_iso()
            self._save_index(index)
            return True
        return False

    def save_entry_by_id(self, entity_id: str, content: str, append: bool = False, consolidate: bool = False, api_key: str = None) -> str:
        index = self._ensure_index()
        _, meta = self._find_meta_by_id(entity_id, index)
        if not meta:
            return f"Error: No entity memory found for '{entity_id}'."
        entity_name = meta.get("canonical_name", entity_id)
        path = self._get_entity_path_from_meta(meta, entity_name)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if consolidate and path.exists() and api_key:
            return self.consolidate_entry(entity_name, content, api_key)

        if append and path.exists():
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n\n--- Update: {timestamp} ---\n{content}")
        else:
            header = f"# Entity Memory: {entity_name}\nCreated: {timestamp}\n\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(header + content)

        now = self._now_iso()
        meta["updated_at"] = now
        meta["last_written_at"] = now
        meta["write_count"] = int(meta.get("write_count", 0) or 0) + 1
        meta["status"] = "active"
        self._save_index(index)
        return f"Entity memory for '{entity_name}' created/overwritten."

    def update_usage_from_trace(self, entity_name: str, resonance: float, alpha: float = 0.2) -> bool:
        """
        Updates usage metadata when a response explicitly reports influence from this entity.
        """
        index = self._ensure_index()
        _, meta = self._find_meta_by_name(entity_name, index)
        if not meta:
            return False

        now = self._now_iso()
        old_importance = float(meta.get("importance", 0.5) or 0.5)
        target = max(0.0, min(1.0, float(resonance)))
        new_importance = old_importance + alpha * (target - old_importance)
        new_importance = max(0.0, min(1.0, new_importance))

        meta["importance"] = round(new_importance, 3)
        meta["use_count"] = int(meta.get("use_count", 0) or 0) + 1
        meta["last_used_at"] = now
        meta["updated_at"] = now
        if meta.get("status") == "dormant" and target >= 0.5:
            meta["status"] = "active"
        self._save_index(index)
        return True

    def record_merge_candidate(self, source_entity_id: str, target_entity_id: str, reason: str = "", similarity: float = 0.0) -> bool:
        """
        Records a manual-review merge candidate on the source entity.
        No files are merged or deleted here.
        """
        index = self._ensure_index()
        entities = index.get("entities", {})
        source = entities.get(source_entity_id)
        target = entities.get(target_entity_id)
        if not isinstance(source, dict) or not isinstance(target, dict) or source_entity_id == target_entity_id:
            return False

        now = self._now_iso()
        merge_candidates = source.setdefault("merge_candidates", [])
        candidate = {
            "entity_id": target_entity_id,
            "name": target.get("canonical_name", ""),
            "similarity": round(float(similarity or 0.0), 3),
            "reason": reason,
            "detected_at": now,
        }
        for item in merge_candidates:
            if isinstance(item, dict) and item.get("entity_id") == target_entity_id:
                item.update(candidate)
                source["updated_at"] = now
                self._save_index(index)
                return True
        merge_candidates.append(candidate)
        source["updated_at"] = now
        self._save_index(index)
        return True

    def mark_dormant_candidates(self, days: int = 90, max_importance: float = 0.4) -> list[str]:
        """
        Marks low-importance active entities with no recall/read activity as dormant.
        This does not affect archived entities and never deletes Markdown files.
        """
        index = self._ensure_index()
        now = datetime.now().astimezone()
        changed = False
        dormant_names = []

        for meta in index.get("entities", {}).values():
            if not isinstance(meta, dict) or meta.get("status") != "active":
                continue
            if int(meta.get("read_count", 0) or 0) > 0 or int(meta.get("recall_count", 0) or 0) > 0:
                continue
            if int(meta.get("use_count", 0) or 0) > 0 and float(meta.get("importance", 0.5) or 0.5) >= max(0.55, max_importance):
                continue
            if float(meta.get("importance", 0.5) or 0.5) > max_importance:
                continue

            last_written = meta.get("last_written_at") or meta.get("created_at")
            try:
                last_dt = datetime.fromisoformat(str(last_written))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.astimezone()
            except Exception:
                continue

            if (now - last_dt).days >= days:
                meta["status"] = "dormant"
                meta["updated_at"] = self._now_iso()
                dormant_names.append(meta.get("canonical_name", ""))
                changed = True

        if changed:
            self._save_index(index)
        return dormant_names

    def set_entity_status(self, entity_name: str, status: str) -> bool:
        index = self._ensure_index()
        _, meta = self._find_meta_by_name(entity_name, index)
        if not meta:
            return False
        meta["status"] = status
        meta["updated_at"] = self._now_iso()
        self._save_index(index)
        return True

    def merge_entities(self, source_entity_id: str, target_entity_id: str, reason: str = "", api_key: str | None = None) -> str:
        """
        Manually merges source into target and moves the source Markdown to _merged/.
        This is intentionally explicit and ID-based to avoid accidental merges.
        The target content is rewritten as one consolidated memory instead of a simple append.
        """
        index = self._ensure_index()
        entities = index.get("entities", {})
        source = entities.get(source_entity_id)
        target = entities.get(target_entity_id)
        if not isinstance(source, dict):
            return f"Error: source entity id not found: {source_entity_id}"
        if not isinstance(target, dict):
            return f"Error: target entity id not found: {target_entity_id}"
        if source_entity_id == target_entity_id:
            return "Error: source and target are the same entity."

        source_path = self._get_entity_path_from_meta(source)
        target_path = self._get_entity_path_from_meta(target)
        if not source_path.exists():
            return f"Error: source file not found: {source.get('filename')}"
        if not target_path.exists():
            return f"Error: target file not found: {target.get('filename')}"

        source_content = source_path.read_text(encoding="utf-8").strip()
        target_content = target_path.read_text(encoding="utf-8").strip()
        merged_content = self._consolidate_entity_merge_content(
            target.get("canonical_name", target_entity_id),
            source.get("canonical_name", source_entity_id),
            target_content,
            source_content,
            reason=reason,
            api_key=api_key,
        )
        timestamp = self._now_iso()
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(merged_content.rstrip() + "\n")

        merged_dir = self.entities_dir / "_merged"
        merged_dir.mkdir(parents=True, exist_ok=True)
        safe_timestamp = re.sub(r"[^0-9A-Za-z]+", "_", timestamp).strip("_")
        archived_name = f"{safe_timestamp}__{source_path.name}"
        archived_path = merged_dir / archived_name
        shutil.move(str(source_path), str(archived_path))

        target_aliases = target.setdefault("aliases", [])
        source_name = source.get("canonical_name", "")
        if source_name and source_name not in target_aliases and source_name != target.get("canonical_name"):
            target_aliases.append(source_name)
        for alias in source.get("aliases", []):
            if alias and alias not in target_aliases and alias != target.get("canonical_name"):
                target_aliases.append(alias)

        related_ids = target.setdefault("related_ids", [])
        if source_entity_id not in related_ids:
            related_ids.append(source_entity_id)

        now = self._now_iso()
        target["updated_at"] = now
        target["last_written_at"] = now
        target["write_count"] = int(target.get("write_count", 0) or 0) + 1
        target["last_merged_at"] = now
        target["last_merged_source_id"] = source_entity_id
        source["status"] = "archived"
        source["merged_into"] = target_entity_id
        source["archived_file"] = str(archived_path.relative_to(self.entities_dir))
        source["updated_at"] = now
        self._save_index(index)
        return (
            f"Entity '{source.get('canonical_name')}' merged into "
            f"'{target.get('canonical_name')}'. Source moved to {source['archived_file']}."
        )

    def resolve_entity_candidate(self, entity_name: str, content: str = "", min_similarity: float = 0.88) -> dict:
        index = self._ensure_index()
        normalized = self._normalize_name(entity_name)
        if not normalized:
            return {"decision": "reject", "target_name": None, "confidence": 1.0, "reason": "empty name"}

        exact_id, exact_meta = self._find_meta_by_name(entity_name, index)
        if exact_meta and exact_meta.get("status") != "archived":
            return {
                "decision": "same",
                "target_id": exact_id,
                "target_name": exact_meta.get("canonical_name"),
                "confidence": 1.0,
                "reason": "canonical name or alias matched",
            }

        candidates = []
        for entity_id, meta in index.get("entities", {}).items():
            if not isinstance(meta, dict) or meta.get("status") == "archived":
                continue
            names = [meta.get("canonical_name", ""), *meta.get("aliases", [])]
            ratios = []
            for name in names:
                norm_name = self._normalize_name(name)
                if not norm_name:
                    continue
                ratio = difflib.SequenceMatcher(None, normalized, norm_name).ratio()
                if normalized in norm_name or norm_name in normalized:
                    ratio = max(ratio, 0.82)
                ratios.append((ratio, name))
            if ratios:
                best_ratio, best_name = max(ratios, key=lambda item: item[0])
                candidates.append({
                    "entity_id": entity_id,
                    "name": meta.get("canonical_name"),
                    "matched_name": best_name,
                    "similarity": round(best_ratio, 3),
                    "status": meta.get("status", "active"),
                })

        candidates.sort(key=lambda item: item["similarity"], reverse=True)
        if candidates and candidates[0]["similarity"] >= min_similarity:
            return {
                "decision": "same",
                "target_id": candidates[0]["entity_id"],
                "target_name": candidates[0]["name"],
                "confidence": candidates[0]["similarity"],
                "reason": "similar normalized name",
                "candidates": candidates[:5],
            }
        if candidates and candidates[0]["similarity"] >= 0.74:
            return {
                "decision": "related",
                "target_id": candidates[0]["entity_id"],
                "target_name": candidates[0]["name"],
                "confidence": candidates[0]["similarity"],
                "reason": "possible related or duplicate entity",
                "candidates": candidates[:5],
            }
        return {
            "decision": "new",
            "target_id": None,
            "target_name": None,
            "confidence": 0.0,
            "reason": "no close existing entity",
            "candidates": candidates[:5],
        }

    def create_or_update_entry(self, entity_name: str, content: str, append: bool = False, consolidate: bool = False, api_key: str = None) -> str:
        """
        Creates or updates an entity memory file.
        - append: Trueの場合、末尾に追記します。
        - consolidate: Trueの場合、既存の記憶と新しい情報をLLMで統合・要約します（api_keyが必要）。
        """
        resolution = self.resolve_entity_candidate(entity_name, content)
        llm_resolution = None
        if resolution.get("decision") == "related" and api_key:
            llm_resolution = self.classify_entity_candidate_with_llm(entity_name, content, resolution, api_key)
            if llm_resolution:
                resolution = {
                    "decision": llm_resolution.get("decision", resolution.get("decision")),
                    "target_id": llm_resolution.get("target_entity_id") or resolution.get("target_id"),
                    "target_name": llm_resolution.get("canonical_name") or resolution.get("target_name"),
                    "confidence": llm_resolution.get("confidence", resolution.get("confidence", 0.0)),
                    "reason": llm_resolution.get("relationship", resolution.get("reason", "")),
                    "candidates": resolution.get("candidates", []),
                    "aliases_to_add": llm_resolution.get("aliases_to_add", []),
                }

        if resolution.get("decision") == "reject":
            return f"Entity memory for '{entity_name}' skipped: {resolution.get('reason')}"
        if resolution.get("decision") == "same" and resolution.get("target_name"):
            entity_name = resolution["target_name"]

        index = self._ensure_index()
        entity_id, meta = self._find_meta_by_name(entity_name, index)
        path = self._get_entity_path_from_meta(meta, entity_name)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if consolidate and path.exists() and api_key:
            return self.consolidate_entry(entity_name, content, api_key)
        
        if append and path.exists():
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n\n--- Update: {timestamp} ---\n{content}")
        else:
            header = f"# Entity Memory: {entity_name}\nCreated: {timestamp}\n\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(header + content)

        index = self._ensure_index()
        entity_id, meta = self._find_meta_by_name(entity_name, index)
        now = self._now_iso()
        if not meta:
            entity_id = self._make_entity_id(entity_name, index)
            meta = self._new_index_meta(entity_name, path.name, now)
            index["entities"][entity_id] = meta

        if resolution.get("decision") == "related" and resolution.get("target_id"):
            merge_candidates = meta.setdefault("merge_candidates", [])
            candidate = {
                "entity_id": resolution["target_id"],
                "name": resolution.get("target_name"),
                "similarity": resolution.get("confidence", 0.0),
                "reason": resolution.get("reason", ""),
                "detected_at": now,
            }
            if not any(
                isinstance(item, dict) and item.get("entity_id") == candidate["entity_id"]
                for item in merge_candidates
            ):
                merge_candidates.append(candidate)

        if resolution.get("decision") == "parent_child" and resolution.get("target_id"):
            parent_id = resolution["target_id"]
            meta["parent_id"] = parent_id
            parent_meta = index.get("entities", {}).get(parent_id)
            if isinstance(parent_meta, dict):
                related_ids = parent_meta.setdefault("related_ids", [])
                if entity_id and entity_id not in related_ids:
                    related_ids.append(entity_id)
                parent_meta["updated_at"] = now

        for alias in resolution.get("aliases_to_add", []):
            alias = str(alias).strip()
            if alias and alias not in meta.setdefault("aliases", []):
                meta["aliases"].append(alias)

        meta["canonical_name"] = entity_name
        meta["filename"] = path.name
        meta["status"] = "active"
        meta["updated_at"] = now
        meta["last_written_at"] = now
        meta["write_count"] = int(meta.get("write_count", 0) or 0) + 1
        self._save_index(index)

        if append and path.exists():
            return f"Entity memory for '{entity_name}' updated (appended)."
        return f"Entity memory for '{entity_name}' created/overwritten."

    def consolidate_entry(self, entity_name: str, new_content: str, api_key: str) -> str:
        """
        既存の記憶と新しい情報をLLMで統合・整理します。
        文体は同ディレクトリ内の他ファイルを参照して保持します。
        """
        index = self._ensure_index()
        _, meta = self._find_meta_by_name(entity_name, index)
        path = self._get_entity_path_from_meta(meta, entity_name)
        if not path.exists():
            return self.create_or_update_entry(entity_name, new_content)

        existing_content = self._read_entry_content(entity_name)
        
        # 文体参照用に、同ディレクトリ内の他ファイルを最大2件取得
        style_samples = []
        for other_file in self.entities_dir.glob("*.md"):
            if other_file.stem != entity_name and len(style_samples) < 2:
                try:
                    sample_text = other_file.read_text(encoding="utf-8")
                    # 短すぎるものはスキップ（参考にならない）
                    if len(sample_text) > 100:
                        # 最初の500文字のみ取得
                        style_samples.append(sample_text[:500])
                except Exception:
                    continue
        
        style_reference = ""
        if style_samples:
            style_reference = "\n\n---\n\n".join(style_samples)
        else:
            style_reference = "（参考ファイルなし - 既存の記憶の文体を維持してください）"
        
        # LLMによる統合処理
        prompt = f"""あなたは、あるAIエージェントの「内省的な深層意識」です。
対象（{entity_name}）に関する既存の知見と新しく得られた情報を統合し、
永続的な「エンティティ知識・考察録（内部Wikipedia/辞書）」として更新・洗練させてください。

【既存の記憶】
{existing_content}

【新しい情報】
{new_content}

【記述ルール（最重要）】
1. **「累積的な知識ベース」としての体裁**:
   - 単なる出来事の要約（日記）ではなく、対象の本質、定義、特徴、歴史、あなたとの関わりを網羅的に蓄積せよ。
   - **情報の維持と洗練**: 既存の重要な事実（定義、設定、過去の経緯）を安易に削らず、新情報を「追記」ではなく「統合」して、より洗練された一つの記述にせよ。
2. **本人視点（一人称）の内省スタイル**:
   - あなた自身が、対象についての理解を深め、後の思考や対話に活かすための知見を整理していると想定せよ。
   - 客観的事実だけでなく、それに対するあなたの解釈や、あなたとの関係性における意味を含めて記せ。
3. **常体（だ・である調）の徹底**:
   - 敬体や装飾を排除し、情報の密度、正確さ、鋭い洞察を優先せよ。
4. **事実の厳守（ハルシネーション禁止）**:
   - 入力データにない事実を捏造してはならない。

【推奨構成（対象が「概念・技術」等の場合）】
# Entity Memory: {entity_name}
（対象の定義、本質的な機能、あなたにとっての役割）
（仕組みの詳細、関連する技術、論理的な背景）
（あなたのシステムへの影響、活用の可能性、あるいは個人的な見解・疑問）

【推奨構成（対象が「人物」等の場合）】
# Entity Memory: {entity_name}
（その人物の象徴、あなたにとっての立ち位置や意味）
（これまでの経緯、共有された重要な過去の出来事や誓い）
（分析、抱いている感情、未解決の課題や相手への理解）

【統合後のコンテンツのみを出力せよ。前置きや解説は一切禁止】
"""
        try:
            response, _ = self._invoke_llm("processing", prompt, api_key)
            
            # Gemini 3.1 等で list 型が返ってくる場合があるため文字列に結合
            if isinstance(response, list):
                response = "\n".join([str(item) for item in response])
            
            response = response.strip()
            
            # 保存
            with open(path, "w", encoding="utf-8") as f:
                f.write(response)
            self._increment_stat(entity_name, "write_count", "last_written_at")
            
            return f"Entity memory for '{entity_name}' consolidated and updated."
        except Exception as e:
            # エラー時はフォールバックとして追記
            print(f"Consolidation error for {entity_name}: {e}")
            return self.create_or_update_entry(entity_name, new_content, append=True)

    def consolidate_all_entities(self, api_key: str):
        """
        すべてのエンティティ記憶ファイルを統合・整理します。
        """
        entities = self.list_entries()
        print(f"  - [Entity Maintenance] {len(entities)}件のエンティティ記憶のメンテナンスを開始します...")
        
        for name in entities:
            # すでに十分に短いものはスキップするなど最適化も可能だが、
            # 最初はすべてのファイルをクリーンアップの対象とする
            index = self._ensure_index()
            _, meta = self._find_meta_by_name(name, index)
            path = self._get_entity_path_from_meta(meta, name)
            if path.exists() and path.stat().st_size > 100: # 100バイト未満は無視
                try:
                    # new_contentを空にして呼び出すことで、既存の情報をクリーンアップする
                    res = self.consolidate_entry(name, "", api_key)
                    print(f"    - '{name}' を整理しました: {res}")
                except Exception as e:
                    print(f"    - '{name}' の整理中にエラー: {e}")

    def read_entry(self, entity_name: str) -> str:
        """
        Reads the content of an entity memory file.
        """
        content = self._read_entry_content(entity_name)
        if not content.startswith("Error:"):
            self._increment_stat(entity_name, "read_count", "last_read_at")
        return content

    def list_entries(self) -> list:
        """
        Lists all available entity names.
        """
        index = self._ensure_index()
        names = []
        for meta in index.get("entities", {}).values():
            if not isinstance(meta, dict) or meta.get("status") == "archived":
                continue
            path = self._get_entity_path_from_meta(meta)
            if path.exists():
                names.append(meta.get("canonical_name") or path.stem)
        return sorted(set(names))

    def search_entries_detailed(self, query: str, limit: int | None = None, include_dormant: bool = False) -> list:
        """
        Searches across names, aliases, and contents with lightweight metadata scoring.
        This method does not update read counters.
        """
        normalized_query = self._normalize_name(query)
        query_words = [self._normalize_name(w) for w in str(query or "").split() if self._normalize_name(w)]
        if not query_words:
            return []

        index = self._ensure_index()
        scored_matches = []
        for entity_id, meta in index.get("entities", {}).items():
            if not isinstance(meta, dict):
                continue
            status = meta.get("status", "active")
            if status == "archived" or (status == "dormant" and not include_dormant):
                continue
            name = meta.get("canonical_name", "")
            path = self._get_entity_path_from_meta(meta, name)
            if not path.exists():
                continue

            aliases = meta.get("aliases", [])
            name_values = [name, *aliases]
            normalized_names = [self._normalize_name(value) for value in name_values if value]
            content_lower = self._read_entry_content(name).lower()

            score = 0.0
            reasons = []
            if any(normalized_query == value for value in normalized_names):
                score += 10.0
                reasons.append("正規名/別名が完全一致")
            elif any(normalized_query and (normalized_query in value or value in normalized_query) for value in normalized_names):
                score += 6.0
                reasons.append("正規名/別名が部分一致")

            best_ratio = max(
                [difflib.SequenceMatcher(None, normalized_query, value).ratio() for value in normalized_names] or [0.0]
            )
            if best_ratio >= 0.74:
                score += best_ratio * 4.0
                reasons.append("名称が類似")

            word_hits = 0
            for word in query_words:
                if any(word in value for value in normalized_names):
                    score += 2.0
                    word_hits += 1
                elif word in content_lower:
                    score += 1.0
                    word_hits += 1
            if word_hits:
                reasons.append(f"キーワード一致 {word_hits}件")

            score += float(meta.get("importance", 0.5) or 0.5) * 0.5
            use_count = int(meta.get("use_count", 0) or 0)
            if use_count > 0:
                score += min(use_count, 10) * 0.03
            if status == "active":
                score += 0.25
            elif status == "dormant":
                score -= 1.0

            if score > 0:
                scored_matches.append({
                    "entity_id": entity_id,
                    "name": name,
                    "score": round(score, 3),
                    "reason": "、".join(reasons) if reasons else "関連語が一致",
                    "status": status,
                    "filename": meta.get("filename"),
                    "aliases": aliases,
                })

        scored_matches.sort(key=lambda item: item["score"], reverse=True)
        if limit is not None:
            return scored_matches[:limit]
        return scored_matches

    def search_entries(self, query: str) -> list:
        """
        Searches for matching entity names, sorted by relevance.
        """
        return [item["name"] for item in self.search_entries_detailed(query)]


    def delete_entry(self, entity_name: str) -> bool:
        """
        Deletes an entity memory file.
        """
        index = self._ensure_index()
        _, meta = self._find_meta_by_name(entity_name, index)
        path = self._get_entity_path_from_meta(meta, entity_name)
        if path.exists():
            path.unlink()
            if meta:
                meta["status"] = "archived"
                meta["updated_at"] = self._now_iso()
                self._save_index(index)
            return True
        return False

    def _invoke_llm(self, role: str, prompt: str, initial_api_key: str) -> tuple:
        """
        APIキーローテーション対応のLLM呼び出し。
        dreaming_managerと同様のロジック。
        """
        current_api_key = initial_api_key
        
        provider_cat, _, _ = config_manager.get_effective_internal_model(role)
        is_google = provider_cat in ["google", "Google (Gemini)", "Google (Gemini Native)"]
        
        tried_keys = set()
        # 現在のキー名を特定
        current_key_name = config_manager.get_key_name_by_value(current_api_key)
        if current_key_name != "Unknown":
            tried_keys.add(current_key_name)
        
        # モデル名を特定（枯渇管理用）
        _, effective_model_name, _ = config_manager.get_effective_internal_model(role)
        sanitized_model_name = utils.sanitize_model_name(effective_model_name or "")
        
        # 全キーを1周試してもダメなら諦めるための最大回数（キー数 + 予備）
        all_keys_count = len(config_manager.GEMINI_API_KEYS)
        max_retries = max(5, all_keys_count)
        
        for attempt in range(max_retries):
            # 1. 枯渇チェック (Googleのみ)
            if is_google and config_manager.is_key_exhausted(current_key_name, model_name=sanitized_model_name):
                next_key = config_manager.get_next_available_gemini_key(
                    current_exhausted_key=current_key_name,
                    excluded_keys=tried_keys,
                    model_name=sanitized_model_name
                )
                if next_key:
                    current_key_name = next_key
                    current_api_key = config_manager.GEMINI_API_KEYS[next_key]
                    tried_keys.add(next_key)
                else:
                    # 全てのキーを試したか、利用可能なキーが完全にない
                    print(f"  [Entity Rotation] No more available keys. Tried {len(tried_keys)} keys.")
                    raise Exception("利用可能なAPIキーがありません（全キー試行済み、または枯渇）。")

            # 2. モデル生成
            from llm_factory import LLMFactory
            llm = LLMFactory.create_chat_model(
                api_key=current_api_key,
                generation_config={},
                internal_role=role
            )
            
            try:
                response = llm.invoke(prompt)
                # utils.extract_text_from_llm_content を使用してテキスト抽出と思考ログ除去を行う
                content = utils.extract_text_from_llm_content(response.content)
                return content, current_api_key
            except Exception as e:
                err_str = str(e).upper()
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "TOO_MANY_REQUESTS" in err_str:
                    if is_google:
                        print(f"  [Entity Rotation] 429 Error with key '{current_key_name}' for model '{sanitized_model_name}'.")
                        config_manager.mark_key_as_exhausted(current_key_name, model_name=sanitized_model_name)
                    else:
                        print(f"  [Entity Rotation] Rate limit error (429) for non-Google provider. Retrying...")
                    time.sleep(2 * (attempt+1))
                    continue
                elif "502" in err_str or "503" in err_str or "504" in err_str or "CONNECTION ERROR" in err_str:
                    if is_google:
                        print(f"  [Entity Rotation] Server/Connection error with key '{current_key_name}'. Details: {str(e)[:100]}")
                        print(f"  [Entity Rotation] Swapping key temporarily for retry...")
                        # 枯渇マークは付けないが、次のリトライでは別のキーを使うように強制的に切り替える
                        next_key = config_manager.get_next_available_gemini_key(
                            current_exhausted_key=current_key_name,
                            excluded_keys=tried_keys,
                            model_name=sanitized_model_name
                        )
                        if next_key:
                            current_key_name = next_key
                            current_api_key = config_manager.GEMINI_API_KEYS[next_key]
                            tried_keys.add(next_key)
                    else:
                        print(f"  [Entity Rotation] Server/Connection error. Details: {str(e)[:100]}")
                        print(f"  [Entity Rotation] Retrying... ({attempt+1}/{max_retries})")
                    time.sleep(2 * (attempt+1))
                    continue
                else:
                    raise e
        
        raise Exception("Max retries exceeded in EntityMemoryManager._invoke_llm")

    def _extract_json_object(self, text: str) -> dict | None:
        if not text:
            return None
        cleaned = text.strip()
        json_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not json_match:
            return None
        try:
            parsed = json.loads(json_match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    def _build_entity_resolution_prompt(self, candidate_name: str, candidate_content: str, existing_candidates: list[dict]) -> str:
        candidates_text = "\n".join(
            [
                f"- entity_id: {item.get('entity_id')}\n"
                f"  canonical_name: {item.get('name')}\n"
                f"  matched_name: {item.get('matched_name')}\n"
                f"  similarity: {item.get('similarity')}\n"
                f"  status: {item.get('status')}"
                for item in existing_candidates[:5]
            ]
        ) or "- 既存候補なし"
        return f"""あなたはエンティティ記憶の重複防止係です。
新しい候補が、既存エンティティと同一か、関連か、親子関係か、新規か、保存不要かを判定してください。

【新しい候補】
name: {candidate_name}
content: {candidate_content}

【既存候補】
{candidates_text}

出力はJSONのみ:
{{
  "decision": "same | related | parent_child | new | reject",
  "target_entity_id": "既存に統合する場合のID。なければnull",
  "canonical_name": "推奨正規名",
  "aliases_to_add": [],
  "relationship": "判断理由を短く",
  "confidence": 0.0
}}"""

    def classify_entity_candidate_with_llm(self, candidate_name: str, candidate_content: str, resolution: dict, api_key: str) -> dict | None:
        if not api_key:
            return None
        candidates = resolution.get("candidates") or []
        if not candidates:
            return None

        prompt = self._build_entity_resolution_prompt(candidate_name, candidate_content, candidates)
        try:
            raw_text, _ = self._invoke_llm("processing", prompt, api_key)
        except Exception as e:
            print(f"  - [Entity Resolution] LLM裁定に失敗: {e}")
            return None

        parsed = self._extract_json_object(raw_text)
        if not parsed:
            print("  - [Entity Resolution] LLM裁定のJSON解析に失敗")
            return None

        decision = str(parsed.get("decision", "")).strip().lower()
        if decision not in {"same", "related", "parent_child", "new", "reject"}:
            return None

        parsed["decision"] = decision
        parsed["target_entity_id"] = parsed.get("target_entity_id") or parsed.get("target_id")
        parsed["confidence"] = parsed.get("confidence", resolution.get("confidence", 0.0))
        parsed["canonical_name"] = parsed.get("canonical_name") or resolution.get("target_name") or candidate_name
        parsed["relationship"] = parsed.get("relationship", "")
        parsed["aliases_to_add"] = parsed.get("aliases_to_add") or []
        return parsed
