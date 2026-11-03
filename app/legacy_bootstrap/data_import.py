"""新規展開済みWindowsパッケージへ旧データを移す安全なコア。

このモジュールは旧アプリや旧venvを import／実行しない。旧インストールの
``app`` にある永続データだけを、新パッケージの ``app`` へ一度 staging
してから反映する。配布物の所有ファイルは ``update_host.contracts`` の
catalog／release manifestに任せ、ここではcatalogの判定だけを利用する。

失敗時は destination 側に ``.data-import-incomplete`` を残す。部分的な
promoteを自動で巻き戻すと、失敗の証拠やユーザーデータをさらに失うため、
再実行は起動側のガードと新ZIP再展開に委ねる。
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
from dataclasses import dataclass
from typing import Any, Callable, Mapping
import uuid

from update_host.contracts import (
    classify_release_path,
    is_generated_release_state,
    is_python_bytecode_cache,
    validate_release_tree,
)
from update_host.transaction import UpdateLock


DATA_IMPORT_MARKER = ".data-import-incomplete"
DATA_TRANSFER_LOCK = "data-transfer.lock"
_STAGE_PREFIX = ".data-import-stage-"
_REPARSE_POINT = 0x400

# インストール済みユーザーデータに含めない、旧アプリの実行環境・cache。
# installer.persistent_files と同じ境界を持たせるが、installer 自体は
# importしない（配布先で依存が追加されることを防ぐため）。
_EXCLUDED_DATA = (
    ".venv",
    "__pycache__",
    ".git",
    "cloud/lite-relay/node_modules",
)

# 新規venv構築・展開中の二重保持を含め、installerと同じ保守的な容量境界を
# 先に確認する。容量不足後に個人データだけを一部promoteする事態を避ける。
RESERVE_BYTES = 512 * 1024 * 1024
ENVIRONMENT_BUDGET = 3 * 1024 * 1024 * 1024
INITIAL_INSTALL_OVERHEAD = 600 * 1024 * 1024

# 旧版の設定に残ることがある、アプリツリー内絶対pathだけを再定位する。
# 任意の設定値を推測して変更せず、外部の絶対pathはそのまま残す。
_REBASE_PATH_KEYS = frozenset(
    {
        "theme_bg_image",
        "local_model_path",
        "last_sent_scenery_image",
        "wrangler_config_path",
    }
)

_FRESH_ROOT_STATE_NAMES = frozenset(
    {
        ".venv",
        "runtime",
        "update_recovery",
        "update_staging",
        "app.next",
        "app.previous",
        ".venv.next",
        ".venv.previous",
        "runtime.next",
        "runtime.previous",
        "updater.next",
        "updater.previous",
    }
)


class DataImportError(RuntimeError):
    """利用者へ安全に返せる固定codeだけを持つ移行失敗。"""

    def __init__(self, code: str, *, partial: bool = False):
        self.code = str(code)
        self.partial = bool(partial)
        super().__init__(self.code)


@dataclass(frozen=True)
class _FileRecord:
    size: int
    sha256: str


def _fail(code: str, *, partial: bool = False) -> None:
    # 旧configの値、絶対path、OSエラー本文を例外chainへ漏らさない。
    raise DataImportError(code, partial=partial) from None


def _path(value: str | os.PathLike[str]) -> Path:
    try:
        return Path(os.path.abspath(value))
    except (TypeError, ValueError, OSError):
        _fail("invalid-path")
    raise AssertionError("unreachable")


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _regular(path: Path, *, directory: bool = False, hardlink: bool = False) -> None:
    """リンク／特殊fileをfollowせず、通常のfileまたはdirectoryだけ許可する。"""

    try:
        info = path.lstat()
    except FileNotFoundError:
        _fail("missing-path")
    except OSError:
        _fail("unsafe-path")
    if path.is_symlink() or _reparse(info):
        _fail("unsafe-path")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        _fail("unsafe-path")
    if hardlink and not directory and info.st_nlink != 1:
        _fail("unsafe-path")


def _safe_ancestors(path: Path) -> None:
    """既存する親をすべて確認し、junction／symlink経由を拒否する。"""

    absolute = path.absolute()
    for parent in reversed(absolute.parents):
        if os.path.lexists(parent):
            _regular(parent, directory=True)


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _has_required_data(base: Path) -> bool:
    config = base / "config.json"
    characters = base / "characters"
    if not _exists(config) or not _exists(characters):
        return False
    _regular(config, hardlink=True)
    _regular(characters, directory=True)
    return True


def _reject_source_incomplete_or_unused(source_root: Path, source_app: Path) -> None:
    """中断済み移行treeや未使用の新ZIPをsourceにしない。"""

    marker_paths = {source_root / DATA_IMPORT_MARKER, source_app / DATA_IMPORT_MARKER}
    if any(_exists(path) for path in marker_paths):
        _fail("source-incomplete")

    manifest = source_app / "release_manifest.json"
    if not _exists(manifest):
        return
    _regular(manifest)
    try:
        validate_release_tree(
            source_app,
            expected_platform="windows",
            expected_cpu="x86_64",
            allow_persistent_state=False,
        )
    except Exception:
        # manifest付きでも、旧利用者のconfig／room差分があればこの検証は
        # 失敗する。そのtreeはsourceデータとして後続のinventoryへ渡す。
        return
    _fail("source-unused-package")


def _resolve_source(source: str | os.PathLike[str]) -> tuple[Path, Path]:
    """旧root／旧root/app／旧フラットrootを判定し、データappを返す。"""

    selected = _path(source)
    _safe_ancestors(selected)
    _regular(selected, directory=True)

    nested_app = selected / "app"
    if _exists(nested_app):
        _regular(nested_app, directory=True)
        # secure rootではapp側を正本とする。root/config.jsonは更新URL用で
        # あってもここへ到達せず、絶対に移行対象へ含めない。
        if _has_required_data(nested_app):
            source_root, source_app = selected, nested_app
            _reject_source_incomplete_or_unused(source_root, source_app)
            return source_root, source_app
        # appを含むflat-like treeを推測して別解釈しない。入力誤りを固定codeで返す。
        if _has_required_data(selected):
            _fail("ambiguous-source")
        _fail("source-missing-data")

    if not _has_required_data(selected):
        _fail("source-missing-data")
    # 旧フラット配置は、旧アプリ本体の存在も確認してから採用する。
    # ``.../app`` を明示指定した場合は、最小データfixtureや旧版の構成で
    # nexus_ark.pyが欠けていても、ここをデータappとして扱う。
    nexus = selected / "nexus_ark.py"
    if not _exists(nexus) and selected.name.casefold() != "app":
        _fail("source-missing-data")
    if _exists(nexus):
        _regular(nexus, hardlink=False)
    # root/appを直接受け取った場合も、旧root全体をsource境界とみなす。
    # destinationがその親root配下へ置かれる誤操作をoverlapで止める。
    source_root = selected.parent if selected.name.casefold() == "app" else selected
    _reject_source_incomplete_or_unused(source_root, selected)
    return source_root, selected


def _path_excluded(relative: str) -> bool:
    return (
        any(
            relative == prefix or relative.startswith(prefix + "/")
            for prefix in _EXCLUDED_DATA
        )
        or relative == "nexus_ark.lock"
        or is_generated_release_state(relative)
        or is_python_bytecode_cache(relative)
        or classify_release_path(relative) == "ephemeral"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except (OSError, ValueError):
        _fail("data-read-failed")
    return digest.hexdigest()


def _source_inventory(app: Path) -> dict[str, _FileRecord]:
    """catalog上の永続pathだけを列挙し、links／hardlinksを拒否する。"""

    _regular(app, directory=True)
    inventory: dict[str, _FileRecord] = {}

    def walk_error(error: OSError) -> None:
        _fail("data-read-failed")

    try:
        walker = os.walk(app, topdown=True, followlinks=False, onerror=walk_error)
        for current, directories, files in walker:
            parent = Path(current)
            _regular(parent, directory=True)
            for name in list(directories):
                child = parent / name
                relative = child.relative_to(app).as_posix()
                if _path_excluded(relative):
                    directories.remove(name)
                    continue
                _regular(child, directory=True)
            for name in files:
                source = parent / name
                relative = source.relative_to(app).as_posix()
                if _path_excluded(relative):
                    continue
                _regular(source)
                if classify_release_path(relative) == "owned":
                    continue
                _regular(source, hardlink=True)
                inventory[relative] = _FileRecord(
                    size=source.stat().st_size,
                    sha256=_sha256(source),
                )
    except DataImportError:
        raise
    except OSError:
        _fail("data-read-failed")
    return dict(sorted(inventory.items()))


def _same_inventory(
    first: Mapping[str, _FileRecord], second: Mapping[str, _FileRecord]
) -> bool:
    return dict(first) == dict(second)


def _assert_source_unchanged(
    source_app: Path,
    expected: Mapping[str, _FileRecord],
    *,
    partial: bool = False,
) -> None:
    try:
        actual = _source_inventory(source_app)
    except DataImportError as error:
        # stage後に削除／リンク化された場合も、旧sourceを変更した観測結果
        # として扱う。初回inventory時のunsafe-pathは呼び出し側でそのまま返す。
        if error.code in {"missing-path", "unsafe-path", "data-read-failed"}:
            _fail("source-changed", partial=partial)
        raise
    if not _same_inventory(expected, actual):
        _fail("source-changed", partial=partial)


def _paths_overlap(first: Path, second: Path) -> bool:
    first = first.absolute()
    second = second.absolute()
    return first == second or first in second.parents or second in first.parents


def _validate_destination(destination: Path) -> Path:
    """新規展開済みappだけを受け入れ、個人データのあるdestを拒否する。"""

    _safe_ancestors(destination)
    _regular(destination, directory=True)
    marker = destination / DATA_IMPORT_MARKER
    if _exists(marker):
        _fail("incomplete-import")
    try:
        root_entries = tuple(destination.iterdir())
    except OSError:
        _fail("destination-not-fresh")
    for entry in root_entries:
        name = entry.name
        if name in _FRESH_ROOT_STATE_NAMES or name.startswith(_STAGE_PREFIX):
            _fail("destination-used")
    # root/config.jsonは旧更新URLのoverrideであり、新規展開先の個人状態を
    # 意味する。配布物のapp/config.jsonはmanifest seedとして下で検証する。
    if _exists(destination / "config.json"):
        _fail("destination-used")
    app = destination / "app"
    if not _exists(app):
        _fail("destination-manifest")
    _regular(app, directory=True)
    _validate_no_links(app)
    manifest = app / "release_manifest.json"
    if not _exists(manifest):
        _fail("destination-manifest")
    _regular(manifest)
    try:
        validate_release_tree(
            app,
            expected_platform="windows",
            expected_cpu="x86_64",
        )
    except DataImportError:
        raise
    except Exception:
        _fail("destination-not-fresh")
    return app


def _validate_no_links(root: Path) -> None:
    """新規展開先のappにもreparse／hardlinkを残さない。"""

    def walk_error(error: OSError) -> None:
        _fail("unsafe-path")

    try:
        for current, directories, files in os.walk(
            root, topdown=True, followlinks=False, onerror=walk_error
        ):
            parent = Path(current)
            _regular(parent, directory=True)
            for name in directories:
                _regular(parent / name, directory=True)
            for name in files:
                _regular(parent / name, hardlink=True)
    except DataImportError:
        raise
    except OSError:
        _fail("unsafe-path")


def _disk_capacity(destination: Path, bytes_to_copy: int) -> None:
    try:
        available = shutil.disk_usage(destination.parent).free
    except OSError:
        _fail("disk-space")
    required = (
        max(1, int(bytes_to_copy))
        + RESERVE_BYTES
        + ENVIRONMENT_BUDGET
        + INITIAL_INSTALL_OVERHEAD
    )
    if available < required:
        _fail("insufficient-space")


def _notify(progress: Callable[[str], Any] | None, phase: str) -> None:
    if progress is None:
        return
    try:
        progress(phase)
    except DataImportError:
        raise
    except Exception:
        _fail("progress-failed")


def _copy_to_stage(source: Path, stage: Path, inventory: Mapping[str, _FileRecord]) -> None:
    for relative, record in inventory.items():
        source_path = source / PurePosixPath(relative)
        target = stage / "app" / PurePosixPath(relative)
        _regular(source_path, hardlink=True)
        _safe_ancestors(target.parent)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _safe_ancestors(target.parent)
            shutil.copy2(source_path, target)
        except (OSError, ValueError):
            _fail("copy-failed")
        _regular(target, hardlink=True)
        if target.stat().st_size != record.size or _sha256(target) != record.sha256:
            _fail("source-changed")


def _stage_records(stage: Path, inventory: Mapping[str, _FileRecord]) -> None:
    for relative, record in inventory.items():
        target = stage / "app" / PurePosixPath(relative)
        _regular(target, hardlink=True)
        if target.stat().st_size != record.size or _sha256(target) != record.sha256:
            _fail("stage-verification")


def _rewrite_stage_room_configs(
    stage: Path,
    old_app: Path,
    destination_app: Path,
    inventory: Mapping[str, _FileRecord],
) -> dict[str, _FileRecord]:
    """room_config.jsonにも既知pathの限定的な再定位を適用する。

    source→stageのraw SHA照合を済ませた後にだけ書き換えるため、sourceの
    内容は変わらず、最終source inventoryは常にraw fileと比較できる。
    """

    rewritten = dict(inventory)
    allowed_relative = set(inventory)
    for relative, record in inventory.items():
        name = PurePosixPath(relative).name.casefold()
        if name != "room_config.json":
            continue
        staged = stage / "app" / PurePosixPath(relative)
        try:
            payload = json.loads(staged.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError, TypeError):
            # 旧版のroom_configが壊れていても、path再定位できないだけで
            # データ本体の移行は継続する。app configは別途必須検証する。
            continue
        updated = _rebase_known_paths(
            payload,
            old_app,
            destination_app,
            allowed_relative=allowed_relative,
        )
        if updated == payload:
            continue
        temporary = staged.with_name(staged.name + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(updated, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, staged)
        except (OSError, TypeError, ValueError):
            try:
                temporary.unlink()
            except OSError:
                pass
            _fail("config-rewrite-failed")
        _regular(staged, hardlink=True)
        rewritten[relative] = _FileRecord(
            size=staged.stat().st_size,
            sha256=_sha256(staged),
        )
    return rewritten


def _destination_target(app: Path, relative: str) -> Path:
    target = app / PurePosixPath(relative)
    _safe_ancestors(target.parent)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError):
        _fail("partial-promotion", partial=True)
    _safe_ancestors(target.parent)
    _regular(target.parent, directory=True)
    return target


def _promote(
    destination_app: Path,
    stage: Path,
    inventory: Mapping[str, _FileRecord],
    *,
    skip: frozenset[str] = frozenset(),
) -> None:
    for relative, record in inventory.items():
        if relative in skip:
            continue
        staged = stage / "app" / PurePosixPath(relative)
        target = _destination_target(destination_app, relative)
        _regular(staged, hardlink=True)
        try:
            os.replace(staged, target)
        except (OSError, ValueError):
            _fail("partial-promotion", partial=True)
        _regular(target, hardlink=True)
        if target.stat().st_size != record.size or _sha256(target) != record.sha256:
            _fail("partial-promotion", partial=True)


def _verify_promoted(
    destination_app: Path,
    inventory: Mapping[str, _FileRecord],
    *,
    skip: frozenset[str] = frozenset(),
) -> None:
    for relative, record in inventory.items():
        if relative in skip:
            continue
        target = destination_app / PurePosixPath(relative)
        _regular(target, hardlink=True)
        if target.stat().st_size != record.size or _sha256(target) != record.sha256:
            _fail("destination-verification", partial=True)


def _is_absolute_text(value: str) -> bool:
    return os.path.isabs(value) or PureWindowsPath(value).is_absolute()


def _allowed_rebase_relative(
    relative: str,
    *,
    allowed_relative: set[str],
    new_app: Path,
) -> bool:
    """実際に移行されるsource fileか、展開先に実在するfileだけ許可する。"""

    normalized = PurePosixPath(relative.replace("\\", "/")).as_posix()
    if normalized in allowed_relative:
        return True
    destination = new_app / PurePosixPath(normalized)
    try:
        info = destination.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not destination.is_symlink() and not _reparse(info)


def _rebase_posix(
    value: str,
    old_app: Path,
    new_app: Path,
    *,
    allowed_relative: set[str],
) -> str | None:
    if not os.path.isabs(value):
        return None
    old_text = str(old_app.absolute())
    # normpath avoids a harmless `old/app/../app` spelling difference while
    # retaining lexical semantics (no symlink resolution of user paths).
    old_norm = os.path.normpath(old_text)
    value_norm = os.path.normpath(value)
    try:
        relative = os.path.relpath(value_norm, old_norm)
    except ValueError:
        return None
    if relative == os.curdir or relative == "":
        return str(new_app.absolute())
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None
    if not _allowed_rebase_relative(
        relative,
        allowed_relative=allowed_relative,
        new_app=new_app,
    ):
        return None
    return str((new_app / relative).absolute())


def _rebase_windows(
    value: str,
    old_app: Path,
    new_app: Path,
    *,
    allowed_relative: set[str],
) -> str | None:
    if not PureWindowsPath(value).is_absolute():
        return None
    old_text = str(old_app.absolute()).replace("/", "\\")
    new_text = str(new_app.absolute()).replace("/", "\\")
    old_norm = ntpath.normcase(ntpath.normpath(old_text))
    value_norm = ntpath.normcase(ntpath.normpath(value))
    try:
        relative = ntpath.relpath(value_norm, old_norm)
    except ValueError:
        return None
    if relative in (".", ""):
        return new_text
    if relative == ".." or relative.startswith("..\\"):
        return None
    if not _allowed_rebase_relative(
        relative,
        allowed_relative=allowed_relative,
        new_app=new_app,
    ):
        return None
    return ntpath.join(new_text, relative)


def _rebase_known_paths(
    value: Any,
    old_app: Path,
    new_app: Path,
    key: str | None = None,
    *,
    allowed_relative: set[str] | None = None,
) -> Any:
    if allowed_relative is None:
        allowed_relative = set()
    if isinstance(value, dict):
        return {
            item_key: _rebase_known_paths(
                item_value,
                old_app,
                new_app,
                str(item_key),
                allowed_relative=allowed_relative,
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [
            _rebase_known_paths(
                item,
                old_app,
                new_app,
                key,
                allowed_relative=allowed_relative,
            )
            for item in value
        ]
    if key not in _REBASE_PATH_KEYS or not isinstance(value, str) or not _is_absolute_text(value):
        return value
    rebased = _rebase_posix(
        value,
        old_app,
        new_app,
        allowed_relative=allowed_relative,
    )
    if rebased is not None:
        return rebased
    rebased = _rebase_windows(
        value,
        old_app,
        new_app,
        allowed_relative=allowed_relative,
    )
    return value if rebased is None else rebased


def _prepare_final_config(
    staged_config: Path,
    old_app: Path,
    destination_app: Path,
    *,
    allowed_relative: set[str],
) -> None:
    try:
        payload = json.loads(staged_config.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError, TypeError):
        _fail("invalid-config")
    if not isinstance(payload, dict):
        _fail("invalid-config")
    payload = _rebase_known_paths(
        payload,
        old_app,
        destination_app,
        allowed_relative=allowed_relative,
    )
    payload["setup_completed"] = True
    temporary = staged_config.with_name(staged_config.name + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, staged_config)
    except (OSError, TypeError, ValueError):
        try:
            temporary.unlink()
        except OSError:
            pass
        _fail("config-commit-failed", partial=True)


def _validate_source_config(source_app: Path) -> None:
    """configをmarker作成前にparseし、壊れた旧設定でdestを触らない。"""

    config = source_app / "config.json"
    try:
        payload = json.loads(config.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError, TypeError):
        _fail("invalid-config")
    if not isinstance(payload, dict):
        _fail("invalid-config")


def _remove_marker(marker: Path) -> None:
    try:
        marker.unlink()
    except (OSError, ValueError):
        _fail("marker-cleanup-failed", partial=True)


def _create_marker(destination: Path, operation_id: str) -> Path:
    marker = destination / DATA_IMPORT_MARKER
    if _exists(marker):
        _fail("incomplete-import")
    try:
        with marker.open("x", encoding="ascii", newline="\n") as stream:
            stream.write("incomplete\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        _fail("incomplete-import")
    except (OSError, ValueError):
        _fail("marker-create-failed")
    return marker


def _new_stage(destination: Path, operation_id: str) -> Path:
    stage = destination / f"{_STAGE_PREFIX}{operation_id}"
    if _exists(stage):
        _fail("incomplete-import")
    try:
        stage.mkdir()
        (stage / "app").mkdir()
    except (OSError, ValueError):
        _fail("stage-create-failed")
    return stage


def _cleanup_stage(stage: Path) -> None:
    try:
        shutil.rmtree(stage)
    except (OSError, ValueError):
        _fail("stage-cleanup-failed", partial=True)


def _import_locked(
    source_app: Path,
    destination: Path,
    destination_app: Path,
    initial_inventory: Mapping[str, _FileRecord],
    progress: Callable[[str], Any] | None,
    operation_id: str,
) -> dict[str, Any]:
    # lock取得後にsource／destinationを再確認し、preflightとの間の差し替えを
    # marker作成前に止める。
    _assert_source_unchanged(source_app, initial_inventory)
    _validate_source_config(source_app)
    destination_app = _validate_destination(destination)
    marker = _create_marker(destination, operation_id)
    stage: Path | None = None
    try:
        _notify(progress, "stage")
        stage = _new_stage(destination, operation_id)
        # 既存bootstrap進行表示との互換alias。どちらもpathや設定値を
        # 含まない固定phaseで、copy開始前にsource変更を検知できる。
        _notify(progress, "copy")
        _copy_to_stage(source_app, stage, initial_inventory)
        # コピー直後にsourceとstageを全件照合する。個々のcopy後の照合だけ
        # では、先にコピーしたfileの後発変更を拾えない。
        _stage_records(stage, initial_inventory)
        _assert_source_unchanged(source_app, initial_inventory)
        promotion_inventory = _rewrite_stage_room_configs(
            stage,
            source_app,
            destination_app,
            initial_inventory,
        )
        _notify(progress, "staged")

        config_relative = "config.json"
        _promote(
            destination_app,
            stage,
            promotion_inventory,
            skip=frozenset({config_relative}),
        )
        _verify_promoted(
            destination_app,
            promotion_inventory,
            skip=frozenset({config_relative}),
        )
        _notify(progress, "promote")

        # ここからもsourceinventoryを最後まで照合し、最後のconfig commitまで
        # source変更を見逃さない。configはこの時点ではまだdestへ触れない。
        _assert_source_unchanged(source_app, initial_inventory, partial=True)
        _notify(progress, "verify")

        staged_config = stage / "app" / config_relative
        _regular(staged_config, hardlink=True)
        _prepare_final_config(
            staged_config,
            source_app,
            destination_app,
            allowed_relative=set(initial_inventory),
        )
        final_config_digest = _sha256(staged_config)
        try:
            os.replace(staged_config, destination_app / config_relative)
        except (OSError, ValueError):
            _fail("partial-promotion", partial=True)
        _regular(destination_app / config_relative, hardlink=True)
        if _sha256(destination_app / config_relative) != final_config_digest:
            _fail("destination-verification", partial=True)

        # release-owned fileのhashを再度確認しつつ、persistent overlayを許可した
        # manifest検証で、別のapp codeが混入していないことも確認する。
        try:
            validate_release_tree(
                destination_app,
                expected_platform="windows",
                expected_cpu="x86_64",
                allow_persistent_state=True,
            )
        except DataImportError:
            raise
        except Exception:
            _fail("destination-verification", partial=True)
        _notify(progress, "complete")
        _assert_source_unchanged(source_app, initial_inventory, partial=True)

        if stage is not None:
            _cleanup_stage(stage)
        # markerの解除は全照合、完了通知、最後のsource照合、stage cleanupの
        # 最後にだけ行う。
        _remove_marker(marker)
        copied_bytes = sum(record.size for record in initial_inventory.values())
        return {
            "event": "result",
            "state": "complete",
            "copied_files": len(initial_inventory),
            "copied_bytes": copied_bytes,
        }
    except DataImportError:
        # marker／stageは新ZIP再展開の判断材料として残す。
        raise
    except Exception:
        _fail("import-failed", partial=True)
    finally:
        # 失敗時は意図的にstageを消さない。成功時のcleanupは上で完了済み。
        pass


def import_data(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    progress: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """旧データを新規展開済みdestinationへ移行する。

    ``source`` は旧root、旧root/app、旧フラットrootのいずれか。戻り値は
    秘密値・pathを含まない件数だけの完了結果で、失敗時は
    :class:`DataImportError` を送出する。
    """

    source_root, source_app = _resolve_source(source)
    destination_root = _path(destination)
    if _paths_overlap(source_root, destination_root) or _paths_overlap(
        source_app, destination_root
    ):
        _fail("source-target-overlap")
    destination_app = _validate_destination(destination_root)
    inventory = _source_inventory(source_app)
    _validate_source_config(source_app)
    _disk_capacity(destination_root, sum(record.size for record in inventory.values()))
    _notify(progress, "validate")

    operation_id = uuid.uuid4().hex
    try:
        with UpdateLock(
            destination_root / DATA_TRANSFER_LOCK,
            operation_id,
            reclaim_stale=False,
        ):
            return _import_locked(
                source_app,
                destination_root,
                destination_app,
                inventory,
                progress,
                operation_id,
            )
    except DataImportError:
        raise
    except RuntimeError:
        # 既存lockはstaleでも勝手に回収しない。親の起動側が新ZIP再展開を案内する。
        _fail("import-locked")
    except (OSError, ValueError, TypeError):
        _fail("import-failed")
    raise AssertionError("unreachable")


__all__ = [
    "DATA_IMPORT_MARKER",
    "DATA_TRANSFER_LOCK",
    "DataImportError",
    "import_data",
]
