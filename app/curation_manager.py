"""Persona atelier curation flows."""

from __future__ import annotations

import shutil
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import agent_delegation
import config_manager
import constants
from file_lock_utils import locked_file, safe_json_read, safe_json_update, safe_json_write


ANTHOLOGY_FILENAMES = ["作品集.md", "これまでの歩み.md", "今後の方向性.md"]
ATELIER_STATES = {"locked", "unlocked", "archived"}


def get_persona_creative_settings() -> dict[str, int]:
    defaults = {
        "anthology_max_turns": 30,
        "anthology_timeout_seconds": 900,
        "snapshot_keep": 0,
        "attic_after_days": 21,
    }
    raw = config_manager.CONFIG_GLOBAL.get("persona_creative_settings", {}) if isinstance(config_manager.CONFIG_GLOBAL, dict) else {}
    settings = {**defaults, **(raw if isinstance(raw, dict) else {})}
    return {
        "anthology_max_turns": max(3, int(settings.get("anthology_max_turns") or defaults["anthology_max_turns"])),
        "anthology_timeout_seconds": max(30, int(settings.get("anthology_timeout_seconds") or defaults["anthology_timeout_seconds"])),
        "snapshot_keep": max(0, int(settings.get("snapshot_keep") or 0)),
        "attic_after_days": max(1, int(settings.get("attic_after_days") or defaults["attic_after_days"])),
    }


def start_anthology(
    room_name: str,
    *,
    sdk_factory: Any | None = None,
    client_factory: Any | None = None,
) -> dict[str, Any]:
    workspace, _exclude_dirs, _exclude_files = agent_delegation._persona_workspace(room_name)
    workspace_path = Path(workspace)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    source_dir = workspace_path / "_source" / timestamp
    anthology_dir = workspace_path / "anthologies" / timestamp

    copied = create_anthology_source_snapshot(room_name, source_dir)
    if not copied:
        if source_dir.exists():
            shutil.rmtree(source_dir, ignore_errors=True)
        return {
            "started": False,
            "message": "編纂する蓄積がまだありません。",
            "source_dir": str(source_dir),
            "anthology_dir": str(anthology_dir),
            "copied_sources": [],
        }

    anthology_dir.mkdir(parents=True, exist_ok=True)
    settings = get_persona_creative_settings()
    expected_output = _anthology_expected_output(timestamp)
    task = agent_delegation.submit_task(
        room_name=room_name,
        task_description=_anthology_task_description(timestamp),
        expected_output=expected_output,
        permission_tier="write",
        workspace_kind="persona",
        trigger="anthology",
        max_turns=settings["anthology_max_turns"],
        timeout_seconds=settings["anthology_timeout_seconds"],
        metadata={
            "task_type": "anthology",
            "source_snapshot_dir": str(source_dir),
            "anthology_dir": str(anthology_dir),
            "anthology_timestamp": timestamp,
            "copied_sources": copied,
            "snapshot_keep": settings["snapshot_keep"],
        },
        sdk_factory=sdk_factory,
        client_factory=client_factory,
    )
    return {
        "started": True,
        "message": "編纂タスクを開始しました。",
        "task": task,
        "source_dir": str(source_dir),
        "anthology_dir": str(anthology_dir),
        "copied_sources": copied,
    }


def create_anthology_source_snapshot(room_name: str, source_dir: Path) -> list[str]:
    room_dir = Path(constants.ROOMS_DIR) / room_name
    copied: list[str] = []
    source_dir.mkdir(parents=True, exist_ok=True)

    note_sources = [
        (room_dir / constants.NOTES_DIR_NAME / constants.CREATIVE_NOTES_FILENAME, source_dir / constants.CREATIVE_NOTES_FILENAME),
        (room_dir / constants.NOTES_DIR_NAME / constants.RESEARCH_NOTES_FILENAME, source_dir / constants.RESEARCH_NOTES_FILENAME),
    ]
    for src, dst in note_sources:
        if _copy_non_empty_file(src, dst):
            copied.append(str(dst.relative_to(source_dir)))

    threads_src = room_dir / "memory" / constants.RESEARCH_THREADS_DIR_NAME
    threads_dst = source_dir / constants.RESEARCH_THREADS_DIR_NAME
    copied.extend(_copy_non_empty_tree(threads_src, threads_dst, source_dir))
    return copied


def cleanup_anthology_sources_for_task(task: dict[str, Any]) -> None:
    metadata = task.get("metadata") or {}
    source_snapshot = metadata.get("source_snapshot_dir")
    if not source_snapshot:
        return
    source_dir = Path(str(source_snapshot))
    source_root = source_dir.parent
    status = str(task.get("status") or "")
    keep = max(0, int(metadata.get("snapshot_keep") or get_persona_creative_settings()["snapshot_keep"]))
    if status == "done" and keep == 0:
        shutil.rmtree(source_dir, ignore_errors=True)
    else:
        effective_keep = keep if status == "done" else max(1, keep)
        prune_source_snapshots(source_root, effective_keep)


def register_anthology_for_task(task: dict[str, Any]) -> dict[str, Any] | None:
    """Register a completed anthology as a locked atelier work."""
    if str(task.get("status") or "") != "done":
        return None
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    if metadata.get("task_type") != "anthology":
        return None
    anthology_dir_raw = metadata.get("anthology_dir")
    if not anthology_dir_raw:
        return None
    anthology_dir = Path(str(anthology_dir_raw))
    paths = [path for path in (anthology_dir / name for name in ANTHOLOGY_FILENAMES) if path.exists() and path.is_file()]
    if not paths:
        return None

    room_name = str(task.get("room_name") or "").strip()
    if not room_name:
        return None
    now = _now_iso()
    task_id = str(task.get("id") or "")
    anthology_timestamp = str(metadata.get("anthology_timestamp") or anthology_dir.name)
    work_id = _atelier_work_id(room_name, anthology_timestamp, task_id or str(anthology_dir))
    stored_paths = [_store_path(path) for path in paths]

    saved_record: dict[str, Any] = {}

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal saved_record
        works = data.setdefault("works", [])
        existing = _find_work(works, room_name=room_name, selector=work_id)
        if existing is None and task_id:
            existing = _find_work(works, room_name=room_name, selector=task_id)

        record = existing if existing is not None else {}
        record.update(
            {
                "id": record.get("id") or work_id,
                "room": room_name,
                "kind": "anthology",
                "task_id": task_id,
                "anthology_timestamp": anthology_timestamp,
                "created_at": record.get("created_at") or str(task.get("finished_at") or task.get("created_at") or now),
                "last_referenced_at": now,
                "paths": stored_paths,
                "state": record.get("state") if record.get("state") in ATELIER_STATES else "locked",
            }
        )
        if existing is None:
            works.append(record)
        saved_record = dict(record)
        return data

    _update_atelier_index(mutate)
    return saved_record


def share_atelier_work(room_name: str, selector: str) -> dict[str, Any]:
    """Unlock an atelier work selected by id, task id, timestamp, or path fragment."""
    room = str(room_name or "").strip()
    key = str(selector or "").strip()
    if not room:
        raise ValueError("room_name が未指定です。")
    if not key:
        raise ValueError("開示するアトリエ作品のIDまたはパスが未指定です。")

    updated_record: dict[str, Any] = {}

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal updated_record
        works = data.setdefault("works", [])
        record = _find_work(works, room_name=room, selector=key)
        if record is None:
            raise KeyError(f"アトリエ作品が見つかりません: {key}")
        if record.get("state") == "archived":
            raise ValueError("屋根裏部屋に移動済みの作品は、このツールでは開示できません。")

        record["state"] = "unlocked"
        record["last_referenced_at"] = _now_iso()
        updated_record = dict(record)
        return data

    _update_atelier_index(mutate)
    _send_atelier_share_notification(room, updated_record)
    return updated_record


def sweep_locked_atelier_works(now: datetime | None = None, room_name: str | None = None) -> list[dict[str, Any]]:
    """Move stale locked works to the attic and mark them archived."""
    current = now or datetime.now()
    room_filter = str(room_name).strip() if room_name else ""
    data = _load_atelier_index()
    moved_by_id: dict[str, list[str]] = {}
    for record in data.get("works", []):
        if not isinstance(record, dict) or record.get("state") != "locked":
            continue
        record_room = str(record.get("room") or "").strip()
        if not record_room:
            continue
        if room_filter and record_room != room_filter:
            continue
        try:
            last_ref = datetime.fromisoformat(str(record.get("last_referenced_at") or record.get("created_at") or ""))
        except ValueError:
            last_ref = current
        if current - last_ref <= timedelta(days=get_persona_creative_settings()["attic_after_days"]):
            continue
        moved_paths = _move_work_paths_to_attic(record_room, record)
        moved_by_id[str(record.get("id") or "")] = moved_paths or list(record.get("paths") or [])

    if not moved_by_id:
        return []

    archived: list[dict[str, Any]] = []

    def mutate(index: dict[str, Any]) -> dict[str, Any]:
        for record in index.get("works", []):
            record_id = str(record.get("id") or "")
            if record_id not in moved_by_id or record.get("state") != "locked":
                continue
            record["paths"] = moved_by_id[record_id]
            record["state"] = "archived"
            record["archived_at"] = current.isoformat()
            record["last_referenced_at"] = current.isoformat()
            archived.append(dict(record))
        return index

    _update_atelier_index(mutate)
    return archived


def delete_archived_atelier_work(room_name: str, selector: str) -> dict[str, Any]:
    room = str(room_name or "").strip()
    data = _load_atelier_index()
    works = data.setdefault("works", [])
    record = _find_work(works, room_name=room, selector=str(selector or "").strip())
    if record is None:
        raise KeyError("屋根裏部屋の作品が見つかりません。")
    if record.get("state") != "archived":
        raise ValueError("削除できるのは archived の作品だけです。")
    deleted_record = dict(record)
    for stored_path in record.get("paths") or []:
        path = _resolve_stored_path(stored_path)
        if path.exists() and path.is_file():
            with locked_file(path):
                if path.exists() and path.is_file():
                    path.unlink()
    parent_dirs = {_resolve_stored_path(path).parent for path in record.get("paths") or []}
    for parent in sorted(parent_dirs, key=lambda p: len(str(p)), reverse=True):
        try:
            if parent.exists() and parent.name != "_attic" and not any(parent.iterdir()):
                parent.rmdir()
        except Exception:
            pass

    def mutate(index: dict[str, Any]) -> dict[str, Any]:
        index["works"] = [
            item for item in index.get("works", [])
            if not (
                isinstance(item, dict)
                and item.get("room") == room
                and item.get("state") == "archived"
                and item.get("id") == record.get("id")
            )
        ]
        return index

    _update_atelier_index(mutate)
    return deleted_record


def list_atelier_works(room_name: str | None = None) -> list[dict[str, Any]]:
    works = [work for work in _load_atelier_index().get("works", []) if isinstance(work, dict)]
    if room_name:
        works = [work for work in works if work.get("room") == room_name]
    works.sort(key=lambda work: str(work.get("created_at") or work.get("last_referenced_at") or ""), reverse=True)
    return [dict(work) for work in works]


def format_atelier_work_for_display(room_name: str, selector: str) -> str:
    record = _find_work(list_atelier_works(room_name), room_name=room_name, selector=selector)
    if record is None:
        return "アトリエ作品を選択してください。"
    state = str(record.get("state") or "locked")
    title = _display_title(record)
    header = [
        f"状態: {state}",
        f"種別: {record.get('kind', '')}",
        f"作成: {record.get('created_at', '')}",
        f"最終参照: {record.get('last_referenced_at', '')}",
    ]
    if state == "locked":
        return "鍵のかかった作品です。中身は表示しません。\n\n" + "\n".join(header)
    if state == "archived":
        return f"屋根裏部屋の作品: {title}\n\n" + "\n".join(header)

    parts = [f"{title}\n", "\n".join(header)]
    for stored_path in record.get("paths") or []:
        path = _resolve_stored_path(stored_path)
        if not path.exists() or not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except Exception as exc:
            body = f"（読み込み失敗: {type(exc).__name__}: {exc}）"
        parts.append(f"\n--- {path.name} ---\n{body}")
    return "\n\n".join(parts)


def prune_source_snapshots(source_root: Path, keep: int) -> None:
    if keep < 0 or not source_root.exists():
        return
    snapshots = [path for path in source_root.iterdir() if path.is_dir()]
    snapshots.sort(key=lambda path: path.name, reverse=True)
    for stale in snapshots[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def _metadata_dir() -> Path:
    return Path(constants.METADATA_DIR) / "persona_atelier"


def _index_path() -> Path:
    return _metadata_dir() / "index.json"


def _load_atelier_index() -> dict[str, Any]:
    data = safe_json_read(str(_index_path()), default={"works": []})
    if isinstance(data, list):
        return {"works": [item for item in data if isinstance(item, dict)]}
    if not isinstance(data, dict):
        return {"works": []}
    if not isinstance(data.get("works"), list):
        data["works"] = []
    return data


def _save_atelier_index(data: dict[str, Any]) -> bool:
    _metadata_dir().mkdir(parents=True, exist_ok=True)
    data.setdefault("works", [])
    return safe_json_write(str(_index_path()), data)


def _update_atelier_index(mutator) -> dict[str, Any]:
    _metadata_dir().mkdir(parents=True, exist_ok=True)
    updated: dict[str, Any] = {"works": []}

    def update(data: Any) -> dict[str, Any]:
        nonlocal updated
        if isinstance(data, list):
            data = {"works": [item for item in data if isinstance(item, dict)]}
        if not isinstance(data, dict):
            data = {"works": []}
        if not isinstance(data.get("works"), list):
            data["works"] = []
        result = mutator(data)
        if isinstance(result, dict):
            data = result
            if not isinstance(data.get("works"), list):
                data["works"] = []
        updated = data
        return data

    safe_json_update(str(_index_path()), update, default={"works": []})
    return updated


def _now_iso() -> str:
    return datetime.now().isoformat()


def _atelier_work_id(room_name: str, anthology_timestamp: str, seed: str) -> str:
    digest = hashlib.sha1(f"{room_name}|{anthology_timestamp}|{seed}".encode("utf-8")).hexdigest()[:10]
    return f"atelier_{anthology_timestamp}_{digest}"


def _store_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _resolve_stored_path(stored_path: str) -> Path:
    path = Path(str(stored_path or ""))
    if path.is_absolute():
        return path
    return Path.cwd() / path


def _find_work(works: list[dict[str, Any]], *, room_name: str, selector: str) -> dict[str, Any] | None:
    key = str(selector or "").strip()
    if not key:
        return None
    for record in works:
        if not isinstance(record, dict) or record.get("room") != room_name:
            continue
        candidates = {
            str(record.get("id") or ""),
            str(record.get("task_id") or ""),
            str(record.get("anthology_timestamp") or ""),
        }
        paths = [str(path) for path in record.get("paths") or []]
        if key in candidates or any(key == path or key in path for path in paths):
            return record
    return None


def _display_title(record: dict[str, Any]) -> str:
    timestamp = str(record.get("anthology_timestamp") or record.get("created_at") or "")
    if record.get("state") == "locked":
        return f"🔒 鍵付きの編纂 ███ {timestamp[:19]}"
    if timestamp:
        return f"編纂 {timestamp[:19]}"
    return str(record.get("id") or "アトリエ作品")


def _send_atelier_share_notification(room_name: str, record: dict[str, Any]) -> None:
    try:
        from tools.notification_tools import send_user_notification

        message = (
            "アトリエの作品が開示されました。\n"
            f"- 種別: {record.get('kind', '')}\n"
            f"- 作成: {record.get('created_at', '')}"
        )
        send_user_notification.invoke({"room_name": room_name, "message": message})
    except Exception:
        # 開示状態の保存を通知失敗で巻き戻さない。
        pass


def _move_work_paths_to_attic(room_name: str, record: dict[str, Any]) -> list[str]:
    paths = [_resolve_stored_path(path) for path in record.get("paths") or []]
    existing = [path for path in paths if path.exists()]
    if not existing:
        return []
    timestamp = str(record.get("anthology_timestamp") or record.get("id") or datetime.now().strftime("%Y%m%d_%H%M%S"))
    attic_dir = Path(constants.ROOMS_DIR) / room_name / "workspace" / "anthologies" / "_attic" / timestamp
    attic_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in existing:
        target = attic_dir / path.name
        if path.resolve() != target.resolve():
            if target.exists():
                with locked_file(target):
                    if target.exists():
                        target.unlink()
            with locked_file(path):
                if path.exists():
                    shutil.move(str(path), str(target))
        moved.append(_store_path(target))
    _remove_empty_anthology_dir(existing[0].parent)
    return moved


def _remove_empty_anthology_dir(path: Path) -> None:
    try:
        if not path.exists() or path.name == "_attic":
            return
        entries = list(path.iterdir())
        if entries and all(entry.is_file() and entry.name.endswith(".lock") for entry in entries):
            for entry in entries:
                entry.unlink()
            entries = []
        if not entries:
            path.rmdir()
    except Exception:
        pass


def _copy_non_empty_file(src: Path, dst: Path) -> bool:
    if not src.exists() or not src.is_file():
        return False
    try:
        if not src.read_text(encoding="utf-8").strip():
            return False
    except UnicodeDecodeError:
        if src.stat().st_size <= 0:
            return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _copy_non_empty_tree(src: Path, dst: Path, source_root: Path) -> list[str]:
    if not src.exists() or not src.is_dir():
        return []
    copied: list[str] = []
    for file_path in sorted(path for path in src.rglob("*") if path.is_file()):
        if file_path.name.startswith("."):
            continue
        try:
            if not file_path.read_text(encoding="utf-8").strip():
                continue
        except UnicodeDecodeError:
            if file_path.stat().st_size <= 0:
                continue
        relative = file_path.relative_to(src)
        target = dst / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target)
        copied.append(str(target.relative_to(source_root)))
    return copied


def _anthology_task_description(timestamp: str) -> str:
    return (
        "あなたはあなた自身の蓄積を編む編集者です。\n"
        f"`_source/{timestamp}/` 配下の創作ノート、研究ノート、研究スレッドを読み、テーマで束ね直してください。\n"
        f"成果物は `anthologies/{timestamp}/` に作成してください。\n"
        "これはあなた自身のための作業です。自己理解のためでも、研究を次へ進めるためでも、誰かに見せるためでもよく、目的はあなたが決めます。\n"
        "ワークスペース外は存在しません。素材が薄いテーマは無理に膨らませず、薄いものとして正直に扱ってください。"
    )


def _anthology_expected_output(timestamp: str) -> str:
    files = "\n".join(f"- `anthologies/{timestamp}/{name}`" for name in ANTHOLOGY_FILENAMES)
    return (
        "以下の3文書をMarkdownで作成してください。\n"
        f"{files}\n"
        "`作品集.md` は代表作・テーマ別アンソロジー、`これまでの歩み.md` は関心の変遷の物語、"
        "`今後の方向性.md` はここから先に進みたい方向としてまとめてください。"
    )
