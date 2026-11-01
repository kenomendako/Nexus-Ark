"""Lite専用runtime targetをTUF検証済みcacheへ取得するアプリ側境界。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from update_host.contracts import validate_runtime_binding
from update_host.runtime_archive import extract_runtime_archive
from secure_tuf_client import SecureTufClient


RUNTIME_TUF_APP_NAME = "Nexus-Ark-LiteRuntime"
RUNTIME_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024


class RuntimeTargetError(RuntimeError):
    """runtime targetを安全に準備できない場合の例外。"""


@dataclass(frozen=True)
class RuntimeTargetPaths:
    root: Path
    metadata: Path
    cache: Path
    staging: Path


def _runtime_binding(release_manifest: Mapping[str, Any]) -> dict[str, Any]:
    components = release_manifest.get("components")
    if not isinstance(components, Mapping):
        raise RuntimeTargetError("更新manifestにcomponent情報がありません。")
    try:
        binding = validate_runtime_binding(components.get("runtime"))
    except (TypeError, ValueError) as exc:
        raise RuntimeTargetError("更新manifestのruntime結合情報が不正です。") from exc
    if binding.get("present") is not True:
        raise RuntimeTargetError("更新manifestにruntime targetがありません。")
    return binding


def _bootstrap_runtime_root(source: Path, destination_dir: Path) -> None:
    destination = destination_dir / "root.json"
    if destination_dir.is_symlink() or destination.is_symlink():
        raise RuntimeTargetError("TUFの信頼起点を確認できません。")

    def read_root(path: Path) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise RuntimeTargetError("TUFの信頼起点を確認できません。")
        try:
            payload = path.read_bytes()
            root = json.loads(payload.decode("utf-8"))
            if root.get("signed", {}).get("_type") != "root":
                raise ValueError("not TUF root metadata")
            return payload
        except (OSError, UnicodeError, ValueError, AttributeError) as exc:
            raise RuntimeTargetError("TUFの信頼起点を確認できません。") from exc

    source_payload = read_root(source)
    if destination.is_file():
        if read_root(destination) != source_payload:
            raise RuntimeTargetError("runtime TUFの信頼起点がapp更新と一致しません。")
        return
    if destination.exists():
        raise RuntimeTargetError("TUFの信頼起点を確認できません。")
    payload = source_payload
    destination_dir.mkdir(parents=True, exist_ok=True)
    if destination_dir.is_symlink():
        raise RuntimeTargetError("TUFの信頼起点を確認できません。")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=destination_dir, prefix="root-", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise RuntimeTargetError("TUFの信頼起点を保存できません。") from exc


class RuntimeTargetClient:
    """既存app updaterとcache／stagingを分離したruntime専用TUF client。"""

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        update_url: str,
        client_factory: Callable[..., Any] = SecureTufClient,
    ) -> None:
        root = Path(project_root).absolute()
        base_url = str(update_url).rstrip("/") + "/"
        self.paths = RuntimeTargetPaths(
            root=root,
            metadata=root / "runtime_metadata",
            cache=root / "runtime_cache" / "targets",
            staging=root / "update_staging" / "runtime",
        )
        _bootstrap_runtime_root(root / "metadata" / "root.json", self.paths.metadata)
        if self.paths.cache.parent.is_symlink() or self.paths.cache.is_symlink():
            raise RuntimeTargetError("runtime target cacheの配置が不正です。")
        self.paths.cache.mkdir(parents=True, exist_ok=True)
        if self.paths.cache.parent.is_symlink() or self.paths.cache.is_symlink():
            raise RuntimeTargetError("runtime target cacheの配置が不正です。")
        try:
            self.client = client_factory(
                app_name=RUNTIME_TUF_APP_NAME,
                app_install_dir=root,
                current_version="0.0.0",
                metadata_dir=self.paths.metadata,
                metadata_base_url=base_url + "metadata/",
                target_dir=self.paths.cache,
                target_base_url=base_url + "targets/",
                extract_dir=None,
            )
        except Exception as exc:
            raise RuntimeTargetError("runtime更新の署名検証を初期化できません。") from exc

    def acquire(
        self,
        release_manifest: Mapping[str, Any],
        *,
        extractor: Callable[[Path, Path, Mapping[str, Any]], Any] = extract_runtime_archive,
    ) -> Path:
        """指定された1 targetだけを取得し、検証付きextractorへ渡す。"""

        binding = _runtime_binding(release_manifest)
        target_name = str(binding["target_name"])
        if self.paths.staging.exists() or self.paths.staging.is_symlink():
            raise RuntimeTargetError("runtime stagingが既にあるため復旧確認が必要です。")
        try:
            self.client.refresh()
            target = self.client.get_targetinfo(target_name)
            if target is None or str(getattr(target, "path", "")) != target_name:
                raise RuntimeTargetError("署名済みruntime targetが見つかりません。")
            length = getattr(target, "length", None)
            if (
                isinstance(length, bool)
                or not isinstance(length, int)
                or not 0 < length <= RUNTIME_MAX_ARCHIVE_BYTES
            ):
                raise RuntimeTargetError("runtime targetの圧縮サイズが許容範囲外です。")
            expected_download = self.paths.cache / target_name
            if expected_download.is_symlink():
                raise RuntimeTargetError("runtime target cacheの配置が不正です。")
            if expected_download.exists():
                try:
                    expected_stat = expected_download.stat()
                except OSError as exc:
                    raise RuntimeTargetError("runtime target cacheを確認できません。") from exc
                if not expected_download.is_file() or getattr(expected_stat, "st_nlink", 1) != 1:
                    raise RuntimeTargetError("runtime target cacheの配置が不正です。")
            cached = self.client.find_cached_target(target)
            if cached:
                archive = Path(cached)
            else:
                existed_before = expected_download.exists() or expected_download.is_symlink()
                try:
                    archive = Path(self.client.download_target(target))
                except Exception:
                    if (
                        not existed_before
                        and expected_download.is_file()
                        and not expected_download.is_symlink()
                    ):
                        try:
                            expected_download.unlink()
                        except OSError:
                            pass
                    raise
            if archive.is_symlink() or not archive.is_file():
                raise RuntimeTargetError("runtime target cacheを確認できません。")
            try:
                if getattr(archive.stat(), "st_nlink", 1) != 1:
                    raise RuntimeTargetError("runtime target cacheの配置が不正です。")
            except OSError as exc:
                raise RuntimeTargetError("runtime target cacheを確認できません。") from exc
            if not archive.resolve().is_relative_to(self.paths.cache.resolve()):
                raise RuntimeTargetError("runtime target cacheの配置が不正です。")
            with archive.open("rb") as handle:
                target.verify_length_and_hashes(handle)
            extractor(archive, self.paths.staging, release_manifest)
        except RuntimeTargetError:
            raise
        except Exception as exc:
            raise RuntimeTargetError(
                f"runtime targetの取得または検証に失敗しました ({type(exc).__name__})。"
            ) from exc
        if not self.paths.staging.is_dir():
            raise RuntimeTargetError("runtime targetをstagingへ準備できませんでした。")
        return self.paths.staging


__all__ = [
    "RUNTIME_MAX_ARCHIVE_BYTES",
    "RUNTIME_TUF_APP_NAME",
    "RuntimeTargetClient",
    "RuntimeTargetError",
    "RuntimeTargetPaths",
]
