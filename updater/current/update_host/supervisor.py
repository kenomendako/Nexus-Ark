"""Nexus Ark Windows update host.

`Start.bat` is deliberately kept as a small shim.  This module is copied to
``updater/current`` in a release and is executed with a Python interpreter
outside of the application ``.venv``.  Consequently it only imports the
Python standard library and the small ``update_host`` contract modules.

The host performs one lifecycle per invocation:

1. recover an interrupted journal;
2. create the current virtual environment on a fresh installation;
3. validate and prepare a staged release, then atomically switch generations;
4. run a localhost-only trial and commit only after its success marker; and
5. start the current application and return its exit code.

Command and process factories are injectable so that the state machine can be
tested on Linux without pretending to be Windows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from .contracts import validate_release_tree
from .runtime import validate_bound_runtime
from .transaction import (
    GenerationLayout,
    JournalStore,
    UpdatePhase,
    UpdateLock,
    _pid_is_alive,
    commit_transaction,
    prepare_transaction,
    recover_incomplete_transaction,
    rollback_transaction,
    switch_generations,
)
from .trial import validate_trial_marker


DEFAULT_TRIAL_PORT = 7860
DEFAULT_TRIAL_TIMEOUT_SECONDS = 300.0
DEFAULT_ROLLBACK_TIMEOUT_SECONDS = 15.0
_COMMITTED_STAGING_PREFIX = "committed-staging."
_COMMITTED_STAGING_ENTRIES = frozenset({"app", "app.download", "runtime"})


class SupervisorError(RuntimeError):
    """更新hostが安全に処理を継続できない場合の例外。"""


class UnsafeGenerationError(SupervisorError):
    """current一式の整合性を確認できず、自動起動してはいけない状態。"""


CommandRunner = Callable[..., Any]
ProcessFactory = Callable[..., Any]


@dataclass(frozen=True)
class SupervisorPaths:
    """インストールroot以下のhostが扱う固定配置。"""

    root: Path

    @property
    def app(self) -> Path:
        return self.root / "app"

    @property
    def staging(self) -> Path:
        return self.root / "update_staging"

    @property
    def recovery(self) -> Path:
        return self.root / "update_recovery"

    @property
    def trial_marker(self) -> Path:
        return self.recovery / "trial-success.json"

    @property
    def python_env(self) -> Path:
        return self.root / ".venv"

    @property
    def pyproject(self) -> Path:
        return self.root / "pyproject.toml"

    @property
    def uv_lock(self) -> Path:
        return self.root / "uv.lock"


def _returncode(result: Any) -> int:
    """subprocess.CompletedProcess、整数、Noneの注入結果を統一する。"""

    if result is None:
        return 0
    if isinstance(result, int):
        return int(result)
    value = getattr(result, "returncode", None)
    if value is None:
        return 0
    return int(value)


def _call_with_optional_keywords(function: Callable[..., Any], args: Sequence[Any], **kwargs: Any) -> Any:
    """テスト用の簡素なfactory（位置引数だけ）も受け入れる。"""

    try:
        return function(*args, **kwargs)
    except TypeError as keyword_error:
        # A TypeError raised *inside* a real factory should not silently be
        # retried with a different signature.  Only retry when the message
        # clearly points at unexpected keyword arguments.
        message = str(keyword_error)
        if (
            "unexpected keyword" not in message
            and "positional argument" not in message
            and "required positional" not in message
        ):
            raise
        reduced = dict(kwargs)
        reduced.pop("check", None)
        if reduced != kwargs:
            try:
                return function(*args, **reduced)
            except TypeError as reduced_error:
                reduced_message = str(reduced_error)
                if (
                    "unexpected keyword" not in reduced_message
                    and "positional argument" not in reduced_message
                    and "required positional" not in reduced_message
                ):
                    raise
        if "cwd" in kwargs and "env" in kwargs:
            return function(*args, kwargs["cwd"], kwargs["env"])
        return function(*args)


class UpdateSupervisor:
    """更新準備・試験起動・通常起動を一つのhost lifecycleとして扱う。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        command_runner: CommandRunner | None = None,
        process_factory: ProcessFactory | None = None,
        environ: Mapping[str, str] | None = None,
        trial_timeout_seconds: float = DEFAULT_TRIAL_TIMEOUT_SECONDS,
        rollback_timeout_seconds: float = DEFAULT_ROLLBACK_TIMEOUT_SECONDS,
        poll_interval_seconds: float = 0.25,
        trial_port: int | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.paths = SupervisorPaths(Path(root).absolute())
        self.layout = GenerationLayout(self.paths.root)
        self.command_runner = command_runner or subprocess.run
        self.process_factory = process_factory or subprocess.Popen
        self.environ = dict(environ or os.environ)
        self.trial_timeout_seconds = float(trial_timeout_seconds)
        self.rollback_timeout_seconds = float(rollback_timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.trial_port = trial_port
        self._sleep = sleeper
        self._monotonic = monotonic

    # ------------------------------------------------------------------
    # External process boundary
    # ------------------------------------------------------------------
    def _run_command(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> int:
        try:
            result = _call_with_optional_keywords(
                self.command_runner,
                [list(map(str, command))],
                cwd=str(cwd),
                env=dict(env),
                check=False,
            )
        except OSError as exc:
            raise SupervisorError(f"外部コマンドを実行できません: {command[0]}") from exc
        return _returncode(result)

    def _spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> Any:
        try:
            return _call_with_optional_keywords(
                self.process_factory,
                [list(map(str, command))],
                cwd=str(cwd),
                env=dict(env),
            )
        except OSError as exc:
            raise SupervisorError(f"アプリケーションprocessを起動できません: {command[0]}") from exc

    # ------------------------------------------------------------------
    # Recovery and dependency preparation
    # ------------------------------------------------------------------
    def recover(self) -> dict[str, Any] | None:
        """通常起動・依存同期より先に中断transactionを解決する。"""

        try:
            journal = recover_incomplete_transaction(self.layout)
        except Exception as exc:
            raise UnsafeGenerationError(
                f"更新journalの復旧に失敗しました: {type(exc).__name__}"
            ) from exc
        if journal and journal.get("phase") == UpdatePhase.MANUAL_RECOVERY_REQUIRED.value:
            raise UnsafeGenerationError("更新journalが手動復旧を要求しています")
        return journal

    def _python_env_exists(self) -> bool:
        if not self.paths.python_env.is_dir():
            return False
        return any(
            candidate.exists()
            for candidate in (
                self.paths.python_env / "Scripts" / "python.exe",
                self.paths.python_env / "bin" / "python",
            )
        )

    def _sync_python_env(self, destination: Path) -> None:
        """uvをhost側から呼び、指定generationへ依存環境を作る。"""

        project = self.paths.app
        if not (project / "pyproject.toml").is_file() or not (project / "uv.lock").is_file():
            raise SupervisorError("依存同期に必要なapp/pyproject.tomlまたはapp/uv.lockがありません")
        env = dict(self.environ)
        env["UV_PROJECT_ENVIRONMENT"] = str(destination)
        code = self._run_command(
            ["uv", "sync", "--project", str(project), "--frozen", "--no-install-project"],
            cwd=self.paths.root,
            env=env,
        )
        if code != 0 or not destination.is_dir():
            raise SupervisorError(f"Python依存環境の同期に失敗しました (exit={code})")

    def _build_next_python_env(self, _layout: GenerationLayout, _manifest: Mapping[str, Any]) -> None:
        destination = self.paths.root / ".venv.next"
        self._sync_python_env_from_project(self.paths.root / "app.next", destination)

    def _sync_python_env_from_project(self, project: Path, destination: Path) -> None:
        if not (project / "pyproject.toml").is_file() or not (project / "uv.lock").is_file():
            raise SupervisorError("新版Python依存同期の入力が不足しています")
        env = dict(self.environ)
        env["UV_PROJECT_ENVIRONMENT"] = str(destination)
        code = self._run_command(
            ["uv", "sync", "--project", str(project), "--frozen", "--no-install-project"],
            cwd=self.paths.root,
            env=env,
        )
        if code != 0 or not destination.is_dir():
            raise SupervisorError(f"新版Python依存環境の同期に失敗しました (exit={code})")

    def ensure_current_python_env(self) -> None:
        """fresh installでだけcurrent .venvをhostから作る。"""

        if self._python_env_exists():
            return
        self._sync_python_env(self.paths.python_env)

    # ------------------------------------------------------------------
    # Staging and transaction lifecycle
    # ------------------------------------------------------------------
    def discover_staging_app(self) -> Path | None:
        staging = self.paths.staging
        if staging.is_symlink():
            raise SupervisorError("更新staging rootがsymlinkです")
        if not staging.is_dir():
            return None
        nested = staging / "app"
        if nested.is_symlink():
            raise SupervisorError("更新app stagingがsymlinkです")
        if nested.is_dir() and (nested / "release_manifest.json").is_file():
            return nested
        if (staging / "release_manifest.json").is_file():
            return staging
        # A directory with no manifest is intentionally ignored.  It may be
        # an incomplete download; never treat it as an atomic update.
        return None

    def discover_staging_runtime(self, staging_app: Path, manifest: Mapping[str, Any]) -> Path | None:
        """runtimeを含むbundleではappの署名済み兄弟directoryだけを返す。"""

        runtime_binding = manifest.get("components", {}).get("runtime", {})
        runtime_present = isinstance(runtime_binding, Mapping) and runtime_binding.get("present") is True
        candidate = self.paths.staging / "runtime"
        if not runtime_present:
            if candidate.exists() or candidate.is_symlink():
                raise SupervisorError("runtimeなしの更新bundleにruntime stagingがあります")
            return None
        if staging_app.parent != self.paths.staging or not candidate.is_dir():
            raise SupervisorError("更新bundleに署名対象のruntime stagingがありません")
        if candidate.is_symlink():
            raise SupervisorError("更新runtime stagingがsymlinkです")
        try:
            validate_bound_runtime(manifest, candidate)
        except Exception as exc:
            raise SupervisorError(f"runtime staging検証に失敗しました: {type(exc).__name__}") from exc
        return candidate

    def _staging_tree_is_safe_to_quarantine(self, staging: Path) -> bool:
        """隔離前にroot直下の既知entryとリンク境界を確認する。"""

        try:
            if staging.is_symlink() or not staging.is_dir():
                return False
            entries = tuple(staging.iterdir())
            if any(entry.name not in _COMMITTED_STAGING_ENTRIES for entry in entries):
                return False
            self._assert_retry_path(staging, recursive=True)
        except (OSError, SupervisorError):
            return False
        return True

    def _remove_committed_staging_quarantine(self, quarantine: Path) -> bool:
        if not self._staging_tree_is_safe_to_quarantine(quarantine):
            return False
        try:
            shutil.rmtree(quarantine)
        except OSError as exc:
            print(
                f"[Nexus Ark] commit済みstaging隔離先を削除できませんでした: {quarantine.name} ({type(exc).__name__})",
                file=sys.stderr,
            )
            return False
        return True

    def _cleanup_committed_staging(self, *, expected_digest: str | None = None) -> None:
        """検証済みcommit stagingを隔離してからbest-effortで除去する。"""

        recovery = self.paths.recovery
        staging = self.paths.staging
        if expected_digest is not None:
            staging_app = self.discover_staging_app()
            if staging_app is None:
                return
            try:
                _manifest, staging_digest = self._manifest_info(staging_app)
            except SupervisorError:
                return
            if staging_digest != expected_digest:
                return
        if not self._staging_tree_is_safe_to_quarantine(staging):
            return
        try:
            # Check the parent before creating it as well; otherwise a
            # missing recovery directory below a reparse-pointed parent could
            # escape the installation root during mkdir.
            self._assert_retry_path(recovery)
            recovery.mkdir(parents=True, exist_ok=True)
            self._assert_retry_path(recovery)
            quarantine = recovery / f"{_COMMITTED_STAGING_PREFIX}{uuid.uuid4().hex}"
            if os.path.lexists(quarantine):
                return
            # Both paths are under the installation root, so os.replace is an
            # atomic same-volume rename.  No live staging entry is removed
            # before this succeeds.
            os.replace(staging, quarantine)
        except OSError as exc:
            print(
                f"[Nexus Ark] commit済みstagingを隔離できませんでした ({type(exc).__name__})",
                file=sys.stderr,
            )
            return
        except SupervisorError as exc:
            print(
                f"[Nexus Ark] commit済みstaging隔離先を安全確認できませんでした ({type(exc).__name__})",
                file=sys.stderr,
            )
            return

        self._remove_committed_staging_quarantine(quarantine)

    @staticmethod
    def _manifest_info(app_dir: Path) -> tuple[dict[str, Any], str]:
        try:
            exact = validate_release_tree(
                app_dir,
                expected_platform="windows",
                expected_cpu="x86_64",
            )
        except Exception as exc:
            raise SupervisorError(f"更新manifest検証に失敗しました: {type(exc).__name__}") from exc
        return exact["manifest"], str(exact["manifest_digest"])

    def _validate_committed_current_for_cleanup(self, expected_digest: str) -> None:
        """commit済みstagingを再試行する前にcurrent世代を再確認する。"""

        try:
            current_release = validate_release_tree(
                self.paths.app,
                expected_platform="windows",
                expected_cpu="x86_64",
                allow_persistent_state=True,
            )
            if str(current_release["manifest_digest"]) != expected_digest:
                raise ValueError("current manifest digest mismatch")
            current_manifest = current_release["manifest"]
            runtime_binding = current_manifest.get("components", {}).get("runtime", {})
            if isinstance(runtime_binding, Mapping) and runtime_binding.get("present") is True:
                validate_bound_runtime(current_manifest, self.paths.root / "runtime")
            for name in ("pyproject.toml", "uv.lock"):
                if (self.paths.root / name).read_bytes() != (self.paths.app / name).read_bytes():
                    raise ValueError("current project generation mismatch")
            if not self._python_env_exists():
                raise ValueError("current Python environment is missing")
        except Exception as exc:
            raise SupervisorError(
                f"commit済みcurrentの整合性を確認できません: {type(exc).__name__}"
            ) from exc

    def _load_journal(self) -> dict[str, Any] | None:
        try:
            return JournalStore(self.layout.journal_path).load()
        except Exception as exc:
            raise SupervisorError(f"更新journalを読み込めません: {type(exc).__name__}") from exc

    def _mark_manual_recovery(self, journal: Mapping[str, Any], reason: str) -> None:
        data = dict(journal)
        data["phase"] = UpdatePhase.MANUAL_RECOVERY_REQUIRED.value
        # JournalStore blocks arbitrary paths/secrets.  Keep diagnostics to a
        # stable exception class rather than persisting user data or messages.
        data["failure"] = reason[:80]
        data["updated_at"] = int(time.time())
        JournalStore(self.layout.journal_path).write(data)

    def _resume_prepared(self, journal: dict[str, Any], app_dir: Path) -> Any:
        """電源断がpreparedで止まった場合はswitchから安全に再開する。"""

        if journal.get("phase") != UpdatePhase.PREPARED.value:
            raise SupervisorError("prepared transactionではありません")
        if not (self.paths.root / "app.next").is_dir():
            raise SupervisorError("prepared journalに対応するapp.nextがありません")
        try:
            exact = validate_release_tree(
                self.paths.root / "app.next",
                expected_platform="windows",
                expected_cpu="x86_64",
                allow_persistent_state=True,
            )
            if bool(journal.get("runtime_present")):
                validate_bound_runtime(exact["manifest"], self.paths.root / "runtime.next")
        except Exception as exc:
            raise SupervisorError(f"prepared世代の再検証に失敗しました: {type(exc).__name__}") from exc
        switched = switch_generations(self.layout, journal)
        return self._trial_and_commit(switched, app_dir=self.paths.app)

    def _assert_retry_stopped(self, *, own_transaction_lock: bool = False) -> None:
        """削除やprocess終了を行わず、停止を確認できない再試行を拒否する。"""

        for name in ("transaction.lock", "bridge.lock"):
            if own_transaction_lock and name == "transaction.lock":
                continue
            if (self.paths.recovery / name).exists():
                raise SupervisorError("更新処理のlockが残っています。削除せず診断してください")
        if (self.paths.root / "Start.bat.lock").exists() or (self.paths.root / "updater.next").exists():
            raise SupervisorError("起動設定の準備状態を確認できないため再試行しません")
        lock = self.paths.app / "nexus_ark.lock"
        if not lock.exists():
            return
        try:
            if lock.stat().st_size > 65536:
                raise ValueError("oversized lock")
            data = json.loads(lock.read_text(encoding="utf-8"))
            pid = data.get("pid")
            if type(pid) is not int or pid <= 0:
                raise ValueError("invalid lock pid")
        except (OSError, ValueError, AttributeError) as exc:
            raise SupervisorError("本体の停止を確認できません。lockを残して診断してください") from exc
        if _pid_is_alive(pid):
            raise SupervisorError("本体が起動中、または停止を確認できません。通常終了してから再試行してください")

    def _assert_retry_path(self, path: Path, *, recursive: bool = False) -> None:
        """復旧の退避元・先をリンクやjunction経由で操作しない。"""

        def check(item: Path) -> None:
            try:
                info = item.lstat()
            except FileNotFoundError:
                return
            if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise SupervisorError("再試行対象にリンクがあるため停止しました")

        check(path)
        for parent in path.parents:
            check(parent)
        if recursive and path.is_dir():
            def cannot_inspect(error: OSError) -> None:
                raise SupervisorError("再試行対象を完全に確認できないため停止しました") from error
            for directory, dirs, files in os.walk(path, followlinks=False, onerror=cannot_inspect):
                for name in dirs + files:
                    check(Path(directory) / name)

    def prepare_explicit_retry(self) -> dict[str, Any]:
        """停止したrolled_backだけを、証跡を保持して現在データから再準備する。

        通常起動からは呼ばない。配布物側の停止・backup確認付き入口から実行し、
        成功後は元Start.batがprepared世代を通常のtrial/commit経路へ渡す。
        """

        self._assert_retry_path(self.paths.root)
        self._assert_retry_path(self.paths.recovery)
        self._assert_retry_path(self.layout.journal_path)
        self._assert_retry_path(self.layout.journal_path.with_name("transaction.json.tmp"))
        retry_guard = self.paths.recovery / "retry-pending.json"
        self._assert_retry_path(retry_guard)
        if os.path.lexists(retry_guard):
            raise SupervisorError("明示再試行が中断しています。記録を保持して個別に診断してください")
        self._assert_retry_path(self.paths.app / "nexus_ark.lock")
        self._assert_retry_stopped()
        original = self._load_journal()
        if not original or original.get("phase") != UpdatePhase.ROLLED_BACK.value:
            raise SupervisorError("旧版への復旧完了を確認できないため再試行しません")
        if original.get("active_step") is not None:
            raise SupervisorError("世代切替の未確定操作があるため再試行しません")
        staging_app = self.discover_staging_app()
        if staging_app is None:
            raise SupervisorError("失敗した更新の完全なstagingがありません")
        manifest, digest = self._manifest_info(staging_app)
        if digest != original.get("manifest_digest"):
            raise SupervisorError("失敗時と異なる更新データです。個別の復旧確認が必要です")
        staging_runtime = self.discover_staging_runtime(staging_app, manifest)
        slots = self.layout.components(runtime_present=True)
        for slot in slots:
            self._assert_retry_path(slot.current, recursive=True)
            self._assert_retry_path(slot.next, recursive=True)
        try:
            current_release = validate_release_tree(
                self.paths.app, expected_platform="windows", expected_cpu="x86_64",
                allow_persistent_state=True,
            )
            current_manifest = current_release["manifest"]
            if current_manifest.get("components", {}).get("runtime", {}).get("present"):
                validate_bound_runtime(current_manifest, self.paths.root / "runtime")
            for name in ("pyproject.toml", "uv.lock"):
                if (self.paths.root / name).read_bytes() != (self.paths.app / name).read_bytes():
                    raise ValueError("current project generation mismatch")
        except Exception as exc:
            raise SupervisorError("旧版の整合性を確認できないため再試行しません") from exc
        if not self._python_env_exists():
            raise SupervisorError("旧版のPython環境がないため再試行しません")
        self._assert_retry_path(staging_app, recursive=True)
        if staging_runtime is not None:
            self._assert_retry_path(staging_runtime, recursive=True)

        # current/previousは変更しない。中断しても旧rolled_backが残り、
        # 通常起動はこのtargetを自動再試行しない。次回の明示操作は残りのnextだけ保管できる。
        archive = self.layout.failed_dir / ("retry." + uuid.uuid4().hex)
        self._assert_retry_path(archive)
        with UpdateLock(self.layout.lock_path, original["operation_id"], reclaim_stale=False):
            if self._load_journal() != original:
                raise SupervisorError("再試行の確認中に更新状態が変わりました")
            self._assert_retry_stopped(own_transaction_lock=True)
            latest_manifest, latest_digest = self._manifest_info(staging_app)
            if latest_digest != digest:
                raise SupervisorError("確認中に更新データが変わったため再試行しません")
            self.discover_staging_runtime(staging_app, latest_manifest)
            archive.mkdir(parents=True, exist_ok=False)
            evidence = self.layout.journal_path.read_bytes()
            with (archive / "transaction.json").open("xb") as stream:
                stream.write(evidence)
                stream.flush()
                os.fsync(stream.fileno())
            receipt = {
                "schema": 1, "operation_id": original["operation_id"],
                "manifest_digest": digest,
                "journal_sha256": hashlib.sha256(evidence).hexdigest(),
                "preserved_next": [slot.next.name for slot in slots if slot.next.exists()],
            }
            with (archive / "receipt.json").open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            temp = self.layout.journal_path.with_name("transaction.json.tmp")
            if temp.exists():
                os.replace(temp, archive / temp.name)
            for slot in slots:
                if slot.next.exists():
                    os.replace(slot.next, archive / slot.next.name)

            self._assert_retry_stopped(own_transaction_lock=True)
            # journal保存が拒否されてもpreparedを勝手に再開させない独立guard。
            # 中断時は残し、最後の状態照合が完了した場合だけ所有するguardを外す。
            with retry_guard.open("x", encoding="ascii") as stream:
                json.dump({"operation_id": original["operation_id"], "manifest_digest": digest}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            prepared = prepare_transaction(
                self.layout, staging_app, staging_runtime=staging_runtime,
                python_env_builder=self._build_next_python_env,
            )
            try:
                self._assert_retry_stopped(own_transaction_lock=True)
                if prepared["manifest_digest"] != digest:
                    raise SupervisorError("準備中に更新データが変わりました。有効化せず停止しました")
            except SupervisorError:
                prepared["phase"] = UpdatePhase.PREPARING.value
                prepared["failure"] = "retry_state_changed"
                JournalStore(self.layout.journal_path).write(prepared)
                raise
            retry_guard.unlink()
        return {
            "phase": prepared["phase"], "target_version": prepared["target_version"],
            "previous_operation_id": original["operation_id"],
            "operation_id": prepared["operation_id"],
            "evidence": archive.relative_to(self.paths.root).as_posix(),
        }

    def apply_staged_update(self) -> Any | None:
        """stagingが完全なmanifestを持つ場合だけ更新し、trial processを返す。"""

        staging_app = self.discover_staging_app()
        if staging_app is None:
            return None
        manifest, digest = self._manifest_info(staging_app)
        journal = self._load_journal()
        phase = None
        old_digest = ""
        if journal:
            phase = UpdatePhase(str(journal["phase"]))
            old_digest = str(journal.get("manifest_digest") or "")
            if phase == UpdatePhase.COMMITTED and old_digest == digest:
                self._validate_committed_current_for_cleanup(digest)
                self._cleanup_committed_staging(expected_digest=digest)
                return None

        # The committed/same-digest cleanup path above deliberately ignores a
        # partial staging runtime left by an interrupted cleanup.  Every other
        # phase, including a new update and prepared retry, must keep the
        # original staging-runtime validation boundary.
        staging_runtime = self.discover_staging_runtime(staging_app, manifest)

        if journal:
            if phase == UpdatePhase.ROLLED_BACK and old_digest == digest:
                # A failed package remains for diagnosis and must not be
                # retried on every ordinary launch.
                return None
            if phase == UpdatePhase.MANUAL_RECOVERY_REQUIRED:
                raise SupervisorError("更新journalが手動復旧を要求しています")
            if phase == UpdatePhase.PREPARED and old_digest == digest:
                return self._resume_prepared(journal, app_dir=self.paths.app)
            if phase in {UpdatePhase.PREPARING, UpdatePhase.SWITCHING, UpdatePhase.TRIAL_STARTING, UpdatePhase.ROLLING_BACK}:
                raise SupervisorError("前回の更新transactionが未解決です")

        try:
            prepared = prepare_transaction(
                self.layout,
                staging_app,
                staging_runtime=staging_runtime,
                python_env_builder=self._build_next_python_env,
            )
            switched = switch_generations(self.layout, prepared)
        except SupervisorError:
            raise
        except Exception as exc:
            # prepare/switch writes its own journal and switch rolls back on
            # rename faults.  Do not delete staging or next generations here.
            raise SupervisorError(f"更新世代の準備・切替に失敗しました: {type(exc).__name__}") from exc
        return self._trial_and_commit(switched, app_dir=self.paths.app)

    # ------------------------------------------------------------------
    # Trial process and ordinary launch
    # ------------------------------------------------------------------
    def _read_trial_port(self, app_dir: Path) -> int:
        if self.trial_port is not None:
            return int(self.trial_port)
        for key in ("NEXUS_ARK_PORT", "GRADIO_SERVER_PORT"):
            raw = self.environ.get(key)
            if raw:
                try:
                    port = int(raw)
                except ValueError:
                    continue
                if 1 <= port <= 65535:
                    return port
        config = app_dir / "config.json"
        try:
            payload = json.loads(config.read_text(encoding="utf-8"))
            port = int(payload.get("gradio_port", DEFAULT_TRIAL_PORT))
            if 1 <= port <= 65535:
                return port
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return DEFAULT_TRIAL_PORT

    def _python_executable(self) -> Path:
        candidates = (
            self.paths.python_env / "Scripts" / "python.exe",
            self.paths.python_env / "bin" / "python",
        )
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise SupervisorError("current Python環境の実行ファイルが見つかりません")

    def _remove_trial_marker(self) -> None:
        try:
            self.paths.trial_marker.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SupervisorError("古いtrial markerを安全に交換できません") from exc

    def _terminate(self, process: Any) -> bool:
        try:
            running = process.poll() is None
        except AttributeError:
            running = True
        if not running:
            return True
        if os.name == "nt" and isinstance(process, subprocess.Popen):
            # Windows venvのPopen PIDはlauncher shimの場合がある。shimだけを
            # 終了すると実Pythonが残る。作成時刻と保持handleで所有を検証する。
            try:
                from .windows_processes import terminate_owned_process_tree

                return terminate_owned_process_tree(process, self.rollback_timeout_seconds)
            except (ImportError, OSError, ValueError):
                return False
        try:
            process.terminate()
        except (AttributeError, OSError):
            return False
        try:
            process.wait(timeout=self.rollback_timeout_seconds)
            return True
        except (AttributeError, OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
        except (AttributeError, OSError):
            return False
        try:
            process.wait(timeout=self.rollback_timeout_seconds)
            return True
        except (AttributeError, OSError, subprocess.TimeoutExpired):
            return False

    def _trial_and_commit(self, journal: dict[str, Any], *, app_dir: Path) -> Any:
        marker = self.paths.trial_marker
        self._remove_trial_marker()
        operation_id = str(journal["operation_id"])
        digest = str(journal["manifest_digest"])
        target_version = str(journal["target_version"])
        # This token is intentionally process-local.  It is exchanged through
        # the trial environment and marker, but never persisted in the update
        # journal (which is designed to contain no process secrets).
        process_token = secrets.token_hex(32)
        port = self._read_trial_port(app_dir)
        environment: MutableMapping[str, str] = dict(self.environ)
        environment.update(
            {
                "NEXUS_ARK_UPDATE_TRIAL": "1",
                "NEXUS_ARK_UPDATE_OPERATION_ID": operation_id,
                "NEXUS_ARK_UPDATE_TARGET_VERSION": target_version,
                "NEXUS_ARK_UPDATE_MANIFEST_DIGEST": digest,
                "NEXUS_ARK_UPDATE_PROCESS_TOKEN": process_token,
                "NEXUS_ARK_UPDATE_TRIAL_MARKER": str(marker),
                "NEXUS_ARK_NO_BROWSER": "1",
                "NEXUS_ARK_ALLOW_PORT_FALLBACK": "0",
                # Trial must bind exactly one localhost port; this also
                # prevents a config-only custom port from making health checks
                # ambiguous.
                "NEXUS_ARK_PORT": str(port),
                "GRADIO_SERVER_PORT": str(port),
            }
        )
        executable = self._python_executable()
        process = self._spawn(
            [executable, app_dir / "nexus_ark.py"],
            cwd=app_dir,
            env=environment,
        )
        deadline = self._monotonic() + self.trial_timeout_seconds
        marker_error: Exception | None = None
        while self._monotonic() < deadline:
            try:
                returncode = process.poll()
            except AttributeError:
                returncode = None
            except OSError as exc:
                marker_error = SupervisorError("新版trialのprocess状態を確認できません")
                break
            if returncode is not None:
                marker_error = SupervisorError(f"新版trialがmarker前に終了しました (exit={returncode})")
                break
            # The process must still be alive at the instant a marker is
            # accepted.  We intentionally do not compare Popen.pid with the
            # marker PID: Windows venv launchers may be a shim process while
            # Python writes the marker from its own process.
            if marker.exists():
                try:
                    marker_returncode = process.poll()
                except AttributeError:
                    marker_returncode = None
                except OSError as exc:
                    marker_error = SupervisorError("新版trialのprocess状態を確認できません")
                    break
                if marker_returncode is not None:
                    marker_error = SupervisorError(
                        f"新版trialがmarker確認時に終了しました (exit={marker_returncode})"
                    )
                    break
                try:
                    validate_trial_marker(
                        marker,
                        operation_id=operation_id,
                        release_version=target_version,
                        manifest_digest=digest,
                        process_token=process_token,
                        port=port,
                    )
                except Exception as exc:
                    marker_error = exc
                    break
                try:
                    commit_transaction(self.layout, journal)
                except Exception as exc:
                    # commit記録を確定できないtrialを残して、別processを起動しない。
                    self._terminate(process)
                    raise UnsafeGenerationError(
                        "新版の起動確認後に更新記録を確定できません。新版の停止を試みました。"
                        "重ねて起動せず、update_recoveryと各世代を保持して復旧案内を確認してください"
                    ) from exc
                self._cleanup_committed_staging(
                    expected_digest=str(journal["manifest_digest"])
                )
                return process
            self._sleep(self.poll_interval_seconds)

        if marker_error is None:
            marker_error = SupervisorError("新版trialのhealth markerがtimeoutしました")
        if not self._terminate(process):
            raise UnsafeGenerationError(
                "新版trialの停止を確認できません。世代を保持し、重ねて起動せず復旧案内を確認してください"
            ) from marker_error
        try:
            rollback_transaction(self.layout, journal)
        except Exception as rollback_error:
            raise UnsafeGenerationError(
                "新版trial失敗後のrollbackにも失敗しました"
            ) from rollback_error
        raise SupervisorError(f"新版trialを確定できませんでした: {type(marker_error).__name__}") from marker_error

    def launch_current(self) -> Any:
        executable = self._python_executable()
        environment = dict(self.environ)
        for key in (
            "NEXUS_ARK_UPDATE_TRIAL",
            "NEXUS_ARK_UPDATE_OPERATION_ID",
            "NEXUS_ARK_UPDATE_TARGET_VERSION",
            "NEXUS_ARK_UPDATE_MANIFEST_DIGEST",
            "NEXUS_ARK_UPDATE_PROCESS_TOKEN",
            "NEXUS_ARK_UPDATE_TRIAL_MARKER",
        ):
            environment.pop(key, None)
        return self._spawn(
            [executable, self.paths.app / "nexus_ark.py"],
            cwd=self.paths.app,
            env=environment,
        )

    def run(self) -> int:
        """データ移行と排他し、未完了のコピーを起動しない。"""
        try:
            with UpdateLock(self.paths.root / "data-transfer.lock", uuid.uuid4().hex,
                            reclaim_stale=True):
                if os.path.lexists(self.paths.root / ".data-import-incomplete"):
                    raise UnsafeGenerationError(
                        "データ移行が未完了です。この新版は起動せず、元のフォルダーを保持して、"
                        "ZIPを別の新しいフォルダーへ展開し直してください")
                return self._run_locked()
        except UnsafeGenerationError as exc:
            print(f"[Nexus Ark] {exc}", file=sys.stderr)
            return 1
        except (RuntimeError, OSError) as exc:
            print(f"[Nexus Ark] データ移行または起動処理と重なったため停止しました: {type(exc).__name__}", file=sys.stderr)
            return 1

    def _run_locked(self) -> int:
        """host lifecycleを実行し、最終app processのexit codeを返す。"""

        try:
            if os.path.lexists(self.paths.recovery / "retry-pending.json"):
                raise UnsafeGenerationError("明示再試行が未確定です。記録を保持し、読み取り診断を実行してください")
            if self.layout.lock_path.exists():
                try:
                    lock = json.loads(self.layout.lock_path.read_text(encoding="ascii"))
                    pid = lock["pid"]
                    if type(pid) is not int or pid <= 0 or _pid_is_alive(pid):
                        raise ValueError("active or unknown update owner")
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    raise UnsafeGenerationError("別の更新処理が使用中、または状態不明です。重ねて起動しません") from exc
            self.recover()
            self.ensure_current_python_env()
            trial_process = self.apply_staged_update()
            process = trial_process or self.launch_current()
        except UnsafeGenerationError as exc:
            print(f"[Nexus Ark] 安全のため自動起動を停止しました: {exc}", file=sys.stderr)
            return 1
        except SupervisorError as exc:
            # Stagingやtrialに失敗しても、現在世代が存在するならそれを
            # 起動する。現行世代まで特定できない場合だけhostを停止する。
            print(f"[Nexus Ark] {exc}", file=sys.stderr)
            try:
                failed_journal = self._load_journal()
            except SupervisorError as journal_error:
                print(f"[Nexus Ark] 更新状態の再確認にも失敗しました: {journal_error}", file=sys.stderr)
                return 1
            if failed_journal and failed_journal.get("phase") not in {
                UpdatePhase.PREPARING.value,
                UpdatePhase.PREPARED.value,
                UpdatePhase.COMMITTED.value,
                UpdatePhase.ROLLED_BACK.value,
            }:
                # journal保存失敗はrollbackの開始前にも起きる。appとvenvの
                # 存在だけでは同一世代の復旧を証明できない。
                print("[Nexus Ark] 更新世代の復旧を確認できないため起動を停止しました。update_recoveryと各世代を保持して復旧案内を確認してください。", file=sys.stderr)
                return 1
            if not self.paths.app.is_dir() or not self._python_env_exists():
                return 1
            try:
                process = self.launch_current()
            except SupervisorError as launch_error:
                print(f"[Nexus Ark] 現行版の起動準備にも失敗しました: {launch_error}", file=sys.stderr)
                return 1
        try:
            result = process.wait()
        except AttributeError:
            result = getattr(process, "returncode", 0)
        return _returncode(result)


def run_supervisor(root: str | os.PathLike[str], **kwargs: Any) -> int:
    """テストとStart.bat双方から使える薄い関数API。"""

    return UpdateSupervisor(root, **kwargs).run()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Nexus Ark Windows update host")
    parser.add_argument("--root", default=".", help="Nexus Ark installation root")
    parser.add_argument("--prepare-retry", action="store_true", help="explicitly preserve a rolled-back attempt and prepare it again; does not launch")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.prepare_retry:
        try:
            print(json.dumps(UpdateSupervisor(args.root).prepare_explicit_retry(), ensure_ascii=False))
            return 0
        except Exception as exc:
            # 例外本文・絶対path・個人データを診断JSONへ流さない。
            print(json.dumps({"state": "retry_stopped", "exception": type(exc).__name__,
                              "winerror": getattr(exc, "winerror", None)}, ensure_ascii=False))
            return 1
    return run_supervisor(args.root)


if __name__ == "__main__":  # pragma: no cover - exercised by launcher
    raise SystemExit(main())
