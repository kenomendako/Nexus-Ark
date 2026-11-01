import os
import logging
import json
import functools
import platform
import shutil
import subprocess
import urllib.parse
import uuid
import hashlib
from pathlib import Path
from filelock import Timeout
from tuf.api.exceptions import BadVersionNumberError
from secure_tuf_client import SecureTufClient
from file_lock_utils import get_file_lock
from runtime_tuf import RuntimeTargetClient
from update_host.contracts import is_python_bytecode_cache, validate_release_tree
from update_host.runtime import validate_bound_runtime
from version_manager import VersionManager

logger = logging.getLogger(__name__)


LEGACY_MIGRATION_HOST_FILES = (
    "__init__.py",
    "contracts.py",
    "runtime.py",
    "runtime_archive.py",
    "transaction.py",
    "trial.py",
    "supervisor.py",
)

_UPDATE_HOST_MESSAGES = {
    "not_required": "この環境では保護更新hostの移行は必要ありません。",
    "ready": "保護された更新hostが準備済みです。",
    "migration_required": "新しい更新方式への移行が必要です。共通の本体更新画面から移行を準備してください。",
    "migration_required_unavailable": "新しい更新方式への移行が必要ですが、署名済みの移行ファイルを確認できません。完全配布パッケージから復旧してください。",
    "migration_pending": "更新方式の移行準備中です。Nexus Arkを終了し、別の移行状況ウィンドウで完了を確認してから同じStart.batで起動してください。",
    "migration_blocked": "更新hostの復旧状態を確認できません。Nexus Arkを再起動し、状態を確認してください。",
    "migration_lock": "別の更新方式移行が準備中です。完了を待ってから再試行してください。",
}


def _legacy_migration_creation_flags():
    """PowerShell 5.1 helperをコンソール非表示のまま終了待ちさせる。"""

    # DETACHED_PROCESS と DEVNULL の組合せではPowerShellが即終了する。
    return (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def _module_project_root():
    """update_manager.pyの配置から、照会対象のプロジェクトrootを導出する。"""

    module_dir = Path(__file__).resolve().parent
    return module_dir.parent if module_dir.name == "app" else module_dir


def _path_present(path):
    """存在する通常ファイルだけでなく、dangling symlinkも検出する。"""

    candidate = Path(path)
    return candidate.exists() or candidate.is_symlink()


def _safe_regular_file(path, parent=None):
    """symlinkを経由しない通常ファイルか、軽量に確認する。"""

    candidate = Path(path).absolute()
    if candidate.is_symlink() or not candidate.is_file():
        return False
    if parent is None:
        return True
    root = Path(parent).absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return False
    current = root
    if current.is_symlink() or not current.is_dir():
        return False
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            return False
    return True


def _read_small_json_object(path, *, max_bytes=64 * 1024):
    """更新状態の照会用に、小さなJSON記録だけを読む。"""

    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        if candidate.stat().st_size > max_bytes:
            return None
        # Windows PowerShell 5.1のSet-Content -Encoding UTF8はBOMを付ける。
        # Python生成の記録もそのまま受理できるよう、先頭BOMだけを許容する。
        payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _migration_record_is_safe(stage, *, verify_staged_files=False):
    """移行helperが扱うrecordの形だけを軽量に確認する。"""

    stage = Path(stage).absolute()
    record_path = stage / "record.json"
    if not _safe_regular_file(record_path, stage):
        return None
    record = _read_small_json_object(record_path)
    if record is None:
        return None
    operation_id = record.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id or operation_id != stage.name:
        return None
    schema_version = record.get("schema_version", 1)
    if isinstance(schema_version, bool) or schema_version != 1:
        return None
    files = record.get("files")
    if not isinstance(files, list) or not files:
        return None
    for entry in files:
        if not isinstance(entry, dict):
            return None
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not relative:
            return None
        normalized = relative.replace("\\", "/")
        if (
            normalized.startswith("/")
            or normalized.startswith("../")
            or "/../" in normalized
            or normalized == ".."
            or (len(normalized) >= 2 and normalized[1] == ":")
        ):
            return None
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            return None
        if verify_staged_files and not _safe_regular_file(stage / Path(normalized), stage):
            return None
    return record


def _inspect_legacy_migration_stages(recovery_dir):
    """移行stagingの状態を ``pending`` / ``complete`` / ``blocked`` で返す。"""

    migration_root = Path(recovery_dir) / "legacy-host-migration"
    if not _path_present(migration_root):
        return None
    if migration_root.is_symlink() or not migration_root.is_dir():
        return "blocked"
    try:
        entries = list(migration_root.iterdir())
    except OSError:
        return "blocked"
    pending = False
    for stage in entries:
        if stage.is_symlink() or not stage.is_dir():
            return "blocked"
        record = _migration_record_is_safe(stage)
        if record is None:
            return "blocked"
        result_path = stage / "result.json"
        if not _path_present(result_path):
            if _migration_record_is_safe(stage, verify_staged_files=True) is None:
                return "blocked"
            pending = True
            continue
        result = _read_small_json_object(result_path)
        if result is None:
            return "blocked"
        result_state = result.get("state")
        if result_state not in ("complete", "failed"):
            return "blocked"
        result_operation_id = result.get("operation_id")
        if result_state == "complete" and result_operation_id != record["operation_id"]:
            return "blocked"
        if result_operation_id is not None and result_operation_id != record["operation_id"]:
            return "blocked"
        if result_state == "failed":
            return "blocked"
    if pending:
        return "pending"
    return "complete" if entries else None


def _inspect_transaction_state(recovery_dir):
    """更新transactionの存在とphaseだけを副作用なしで確認する。"""

    recovery = Path(recovery_dir)
    lock = recovery / "transaction.lock"
    if _path_present(lock):
        return "blocked"
    journal = recovery / "transaction.json"
    if not _path_present(journal):
        return None
    payload = _read_small_json_object(journal)
    if payload is None:
        return "blocked"
    phase = payload.get("phase")
    if phase not in ("committed", "rolled_back"):
        return "blocked"
    return None


def _inspect_update_host_layout(root):
    """updaterの部分配置を検出する。戻り値は ``(blocked, current_present)``。"""

    root = Path(root).absolute()
    updater = root / "updater"
    updater_next = root / "updater.next"
    if _path_present(updater_next):
        return True, False
    if not _path_present(updater):
        return False, False
    if updater.is_symlink() or not updater.is_dir():
        return True, False
    current = updater / "current"
    if not _path_present(current):
        try:
            return (any(updater.iterdir()), False)
        except OSError:
            return True, False
    if current.is_symlink() or not current.is_dir():
        return True, True
    supervisor = current / "update_host" / "supervisor.py"
    if not _safe_regular_file(supervisor, current):
        return True, True
    return False, True


def _launcher_uses_protected_host(root):
    """Start.batが新hostへ切替済みなのに配置が不足する状態を検出する。"""

    start = Path(root) / "Start.bat"
    if not _safe_regular_file(start, Path(root)):
        return False
    try:
        launcher = start.read_text(encoding="utf-8-sig", errors="strict").lower()
    except (OSError, UnicodeError):
        return False
    return "update_host.supervisor" in launcher


def _migration_source_available(root):
    """移行準備の入力が存在するかだけを確認する（完全検証はprepare側で行う）。"""

    root = Path(root).absolute()
    app = root / "app"
    if app.is_symlink() or not app.is_dir():
        return False
    required = [
        root / "Start.bat",
        app / "release_manifest.json",
        app / "pyproject.toml",
        app / "uv.lock",
        app / "release" / "update_host_bridge" / "Start.bat",
        app / "release" / "update_host_bridge" / "ApplyLegacyHostMigration.ps1",
    ]
    required.extend(app / "update_host" / name for name in LEGACY_MIGRATION_HOST_FILES)
    return all(_safe_regular_file(path, app if path.is_relative_to(app) else root) for path in required)


def inspect_update_host_status(project_root=None):
    """更新hostの準備状態を、書込みなしで照会する。

    戻り値はUIが安全に扱える ``state`` / ``can_prepare`` / ``message`` の3キーだけを
    持つ。署名manifestの完全検証や移行stagingの作成は、この関数では行わない。
    """

    if platform.system() != "Windows":
        return {
            "state": "not_required",
            "can_prepare": False,
            "message": _UPDATE_HOST_MESSAGES["not_required"],
        }
    root = Path(project_root).absolute() if project_root is not None else _module_project_root()
    recovery = root / "update_recovery"
    if recovery.is_symlink() or (_path_present(recovery) and not recovery.is_dir()):
        return {
            "state": "migration_blocked",
            "can_prepare": False,
            "message": _UPDATE_HOST_MESSAGES["migration_blocked"],
        }
    transaction_state = _inspect_transaction_state(recovery)
    migration_state = _inspect_legacy_migration_stages(recovery)
    layout_blocked, current_present = _inspect_update_host_layout(root)
    if transaction_state == "blocked" or migration_state == "blocked" or layout_blocked:
        return {
            "state": "migration_blocked",
            "can_prepare": False,
            "message": _UPDATE_HOST_MESSAGES["migration_blocked"],
        }
    if migration_state == "pending":
        return {
            "state": "migration_pending",
            "can_prepare": False,
            "message": _UPDATE_HOST_MESSAGES["migration_pending"],
        }
    if protected_update_host_ready(root):
        return {
            "state": "ready",
            "can_prepare": False,
            "message": _UPDATE_HOST_MESSAGES["ready"],
        }
    if (
        current_present
        or migration_state == "complete"
        or _launcher_uses_protected_host(root)
    ):
        return {
            "state": "migration_blocked",
            "can_prepare": False,
            "message": _UPDATE_HOST_MESSAGES["migration_blocked"],
        }
    can_prepare = _migration_source_available(root)
    return {
        "state": "migration_required",
        "can_prepare": can_prepare,
        "message": (
            _UPDATE_HOST_MESSAGES["migration_required"]
            if can_prepare
            else _UPDATE_HOST_MESSAGES["migration_required_unavailable"]
        ),
    }


def _serialize_legacy_migration_prepare(method):
    """同一rootのUIから移行準備を二重起動しない。"""

    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        if platform.system() != "Windows":
            return method(self, *args, **kwargs)
        initial_status = self.update_host_status()
        if (
            initial_status["state"] != "migration_required"
            or not initial_status["can_prepare"]
        ):
            return method(self, *args, **kwargs)
        recovery = Path(self.project_root) / "update_recovery"
        try:
            recovery.mkdir(parents=True, exist_ok=True)
            lock = get_file_lock(str(recovery / "legacy-host-migration"), timeout=0)
            with lock:
                return method(self, *args, **kwargs)
        except Timeout:
            return False, _UPDATE_HOST_MESSAGES["migration_lock"]
        except OSError as exc:
            logger.error("Legacy migration lock failed: %s", type(exc).__name__)
            return False, "更新方式の移行を安全に準備できませんでした。"

    return wrapped


def protected_update_host_ready(project_root):
    """新しい原子更新hostと固定shimが一組で配置されている場合だけTrueを返す。"""

    root = Path(project_root).absolute()
    start = root / "Start.bat"
    supervisor = root / "updater" / "current" / "update_host" / "supervisor.py"
    if (
        start.is_symlink()
        or supervisor.is_symlink()
        or not start.is_file()
        or not supervisor.is_file()
    ):
        return False
    try:
        launcher = start.read_text(encoding="utf-8-sig", errors="strict")
    except (OSError, UnicodeError):
        return False
    lowered = launcher.lower()
    return "update_host.supervisor" in lowered and "robocopy" not in lowered


def legacy_overlay_allowed(project_root):
    """旧launcher移行済みでbackupが残る場合だけ、未掲載の旧配布fileを許容する。"""

    root = Path(project_root).absolute()
    if not protected_update_host_ready(root):
        return False
    backups = root / "update_recovery" / "bridge-backups"
    if backups.is_symlink() or not backups.is_dir():
        return False
    try:
        return any(
            candidate.is_dir()
            and not candidate.is_symlink()
            and (candidate / "Start.bat").is_file()
            and not (candidate / "Start.bat").is_symlink()
            for candidate in backups.iterdir()
        )
    except OSError:
        return False


def _runtime_repository_url(update_url):
    """本体更新repositoryと同じorigin配下のruntime専用URLを返す。"""

    parsed = urllib.parse.urlsplit(str(update_url))
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("更新サーバーURLが安全な形式ではありません。") from exc
    decoded_path = urllib.parse.unquote(parsed.path)
    unsafe_path = (
        "\\" in decoded_path
        or any(ord(character) < 32 for character in decoded_path)
        or any(part in {".", ".."} for part in decoded_path.split("/"))
    )
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    if (
        (parsed.scheme != "https" and not local_http)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or unsafe_path
    ):
        raise ValueError("更新サーバーURLが安全な形式ではありません。")
    base_path = parsed.path.rstrip("/") + "/"
    runtime_path = base_path + UpdateManager.RUNTIME_UPDATE_SUBDIRECTORY + "/"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, runtime_path, "", ""))

class UpdateManager:
    """
    Nexus Ark の自動更新を管理するクラス。
    tufup (The Update Framework) を使用して、安全な差分更新を実現します。
    """
    APP_NAME = "Nexus-Ark"
    # デフォルトの更新サーバーURL (GitHub Pages等を想定)
    DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/kenomendako/Nexus-Ark/main/updates/"

    RUNTIME_UPDATE_SUBDIRECTORY = "lite-runtime"

    def __init__(
        self,
        update_url=None,
        *,
        client_factory=SecureTufClient,
        runtime_client_factory=RuntimeTargetClient,
        cleanup_old_archives=True,
    ):
        self.current_version = VersionManager.get_current_version()
        self.install_dir = Path(__file__).parent.resolve()
        
        # プロジェクトルートの特定 (2段階構造 dist/app を考慮)
        self.project_root = self.install_dir
        if self.install_dir.name == "app":
            self.project_root = self.install_dir.parent
            
        # メタデータディレクトリはプロジェクトルート直下
        self.metadata_dir = self.project_root / "metadata"
        self.metadata_dir.mkdir(exist_ok=True)
        
        # config.json からの上書きをチェック (プロジェクトルートまたはインストールディレクトリ)
        source_info = "default"
        if not update_url:
            # プロジェクトルート -> インストールディレクトリ の順で探す
            config_paths = [self.project_root / "config.json", self.install_dir / "config.json"]
            for config_path in config_paths:
                if config_path.exists():
                    try:
                        with open(config_path, "r", encoding="utf-8") as f:
                            config_data = json.load(f)
                            update_url = config_data.get("update_url")
                            if update_url:
                                source_info = f"config.json ({config_path.parent.name})"
                                break
                    except Exception as e:
                        logger.error(f"Error loading config.json for update_url: {e}")
        else:
            source_info = "provided argument"

        self.update_url = update_url or self.DEFAULT_UPDATE_URL
        if not self.update_url.endswith("/"):
            self.update_url += "/"
        self.runtime_update_url = _runtime_repository_url(self.update_url)
        self.runtime_client_factory = runtime_client_factory
            
        logger.info(f"UpdateManager initialized with URL: {self.update_url} (source: {source_info})")
        logger.info(f"Metadata dir: {self.metadata_dir}")
        logger.info(f"Current version: {self.current_version}")

        # ステージングディレクトリ（展開先）: プロジェクトルート直下に配置
        self.staging_dir = self.project_root / "update_staging"
        self.app_staging_dir = self.staging_dir / "app"
        self.app_download_dir = self.staging_dir / "app.download"

        # ターゲットディレクトリ（ダウンロード先）: プロジェクトルート直下に配置
        self.target_dir = self.project_root / "update_cache"
        self.target_dir.mkdir(exist_ok=True)

        # 古い不要アーカイブの自動クリーンアップ
        if cleanup_old_archives:
            self._cleanup_old_archives()

        # tufup クライアントの初期化
        try:
            self.client = client_factory(
                app_name=self.APP_NAME,
                app_install_dir=str(self.install_dir),
                current_version=self.current_version,
                metadata_dir=str(self.metadata_dir),
                metadata_base_url=self.update_url + "metadata/",
                target_dir=str(self.target_dir),
                target_base_url=self.update_url + "targets/",
                extract_dir=self.app_download_dir,
            )
            logger.info(f"tufup client initialized. Metadata base URL: {self.update_url}metadata/")
        except Exception as e:
            logger.error(f"Failed to initialize tufup client: {e}")
            raise

    def _ensure_staging_root(self):
        if self.staging_dir.is_symlink():
            raise RuntimeError("更新staging rootの配置を確認できません。")
        try:
            self.staging_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError("更新staging rootを準備できません。") from exc
        if self.staging_dir.is_symlink() or not self.staging_dir.is_dir():
            raise RuntimeError("更新staging rootの配置を確認できません。")

    def _remove_generated_app_bytecode(self):
        """署名検証前に既知のPython生成物だけをapp treeから除く。"""

        for cache_dir in self.install_dir.rglob("__pycache__"):
            if cache_dir.is_symlink() or not cache_dir.is_dir():
                continue
            for candidate in cache_dir.iterdir():
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                relative = candidate.relative_to(self.install_dir).as_posix()
                if is_python_bytecode_cache(relative):
                    candidate.unlink()

    def _install_windows_update(self, *, src_dir, dst_dir, require_runtime=False, **_kwargs):
        """app archiveを検証後に完成stagingへ原子確定する。"""

        self._ensure_staging_root()
        source = Path(src_dir).absolute()
        if source != self.app_download_dir.absolute() or source.is_symlink():
            raise RuntimeError("更新download stagingの配置を確認できません。")
        try:
            exact = validate_release_tree(
                source,
                expected_platform="windows",
                expected_cpu="x86_64",
            )
        except Exception as exc:
            raise RuntimeError(
                f"download済み更新manifestを検証できません ({type(exc).__name__})。"
            ) from exc
        if require_runtime and exact["manifest"]["components"]["runtime"].get("present") is not True:
            raise RuntimeError("Lite runtime修復にはruntime付きの署名済み更新が必要です。")
        if self.app_staging_dir.exists() or self.app_staging_dir.is_symlink():
            raise RuntimeError("完成済みapp stagingがあるため再起動または復旧確認が必要です。")
        os.replace(source, self.app_staging_dir)
        self._complete_windows_staging(
            src_dir=self.app_staging_dir,
            dst_dir=dst_dir,
            require_runtime=require_runtime,
        )

    def _complete_windows_staging(self, *, src_dir, dst_dir, require_runtime=False, **_kwargs):
        """署名済みapp stagingへ必要なruntimeを揃えてから更新hostへ引き渡す。"""

        source = Path(src_dir).absolute()
        destination = Path(dst_dir).absolute()
        if (
            source != self.app_staging_dir.absolute()
            or destination != self.install_dir.absolute()
            or source.is_symlink()
            or not source.is_dir()
        ):
            raise RuntimeError("更新stagingの配置を確認できません。")
        try:
            exact = validate_release_tree(
                source,
                expected_platform="windows",
                expected_cpu="x86_64",
            )
        except Exception as exc:
            raise RuntimeError(
                f"更新manifestを検証できません ({type(exc).__name__})。"
            ) from exc

        manifest = exact["manifest"]
        runtime_binding = manifest["components"]["runtime"]
        if require_runtime and runtime_binding.get("present") is not True:
            raise RuntimeError("Lite runtime修復にはruntime付きの署名済み更新が必要です。")
        runtime_staging = self.staging_dir / "runtime"
        if runtime_binding["present"] is True:
            if runtime_staging.exists() or runtime_staging.is_symlink():
                try:
                    validate_bound_runtime(manifest, runtime_staging)
                except Exception as exc:
                    raise RuntimeError(
                        f"保持中のruntime stagingを検証できません ({type(exc).__name__})。"
                    ) from exc
            else:
                manager = self.runtime_client_factory(
                    self.project_root,
                    update_url=self.runtime_update_url,
                )
                acquired = Path(manager.acquire(manifest)).absolute()
                if acquired != runtime_staging.absolute() or not acquired.is_dir():
                    raise RuntimeError("runtime stagingを検証できません。")
                validate_bound_runtime(manifest, acquired)
        elif runtime_staging.exists() or runtime_staging.is_symlink():
            raise RuntimeError("runtimeなしの更新にruntime stagingが残っています。")

        self.trigger_restart()

    def _legacy_overlay_allowed(self):
        """旧launcher移行backupがある環境だけ、旧配布残存物を診断対象外にする。"""

        return legacy_overlay_allowed(self.project_root)

    def update_host_status(self):
        """副作用のない共通更新host照会へ委譲する。"""

        return inspect_update_host_status(self.project_root)

    def runtime_repair_mode(self):
        """現在版を差し替えずに選べるruntime復旧境界を返す。"""

        import platform

        if platform.system() != "Windows":
            return "unsupported_platform"
        if not protected_update_host_ready(self.project_root):
            return "legacy_update_host_migration_required"
        try:
            self._remove_generated_app_bytecode()
            exact = validate_release_tree(
                self.install_dir,
                expected_platform="windows",
                expected_cpu="x86_64",
                allow_persistent_state=True,
                allow_unlisted_legacy_overlay=self._legacy_overlay_allowed(),
            )
        except Exception:
            return "signed_update_required"
        binding = exact["manifest"]["components"]["runtime"]
        if binding.get("present") is not True:
            return "signed_update_required"
        runtime = self.project_root / "runtime"
        if not runtime.exists() and not runtime.is_symlink():
            return "runtime_bootstrap_required"
        # An interrupted first install can leave only an empty destination.
        # It contains no user/runtime data and is safe to retry; every
        # non-empty or linked tree still fails closed.
        if runtime.is_dir() and not runtime.is_symlink():
            try:
                if next(runtime.iterdir(), None) is None:
                    return "runtime_bootstrap_required"
            except OSError:
                return "signed_update_required"
        try:
            validate_bound_runtime(exact["manifest"], runtime)
        except Exception:
            return "signed_update_required"
        return "ready"

    @_serialize_legacy_migration_prepare
    def prepare_legacy_update_host_migration(self):
        """署名済みappから旧launcherの一度限りの移行を終了待ちhelperへ渡す。"""

        import platform

        if platform.system() != "Windows":
            return False, "この環境では更新方式の移行を開始できません。"
        host_status = self.update_host_status()
        if host_status["state"] != "migration_required":
            return False, host_status["message"]
        if not host_status["can_prepare"]:
            return False, host_status["message"]
        try:
            self._remove_generated_app_bytecode()
            validate_release_tree(
                self.install_dir,
                expected_platform="windows",
                expected_cpu="x86_64",
                allow_persistent_state=True,
                allow_unlisted_legacy_overlay=True,
            )
            bridge_source = self.install_dir / "release" / "update_host_bridge"
            start_source = bridge_source / "Start.bat"
            apply_source = bridge_source / "ApplyLegacyHostMigration.ps1"
            host_source = self.install_dir / "update_host"
            required_host = LEGACY_MIGRATION_HOST_FILES
            source_files = [start_source, apply_source]
            source_files.extend(host_source / name for name in required_host)
            source_files.extend(
                (self.install_dir / name) for name in ("pyproject.toml", "uv.lock")
            )
            if any(
                path.is_symlink() or not path.is_file()
                for path in source_files
            ):
                return False, "署名済みの移行ファイルを確認できません。"
            start_text = start_source.read_text(encoding="utf-8-sig", errors="strict").lower()
            if "update_host.supervisor" not in start_text or "robocopy" in start_text:
                return False, "署名済みランチャーの内容を確認できません。"

            if (self.project_root / "updater" / "current").exists():
                return False, "更新hostの配置が既にあるため、上書きせず停止しました。"
            if (self.project_root / "updater.next").exists():
                return False, "中断した更新hostの配置があるため、上書きせず停止しました。"
            transaction_lock = self.project_root / "update_recovery" / "transaction.lock"
            if transaction_lock.exists() or transaction_lock.is_symlink():
                return False, "更新処理の記録があるため、移行を開始できません。"

            operation_id = uuid.uuid4().hex
            stage = (
                self.project_root
                / "update_recovery"
                / "legacy-host-migration"
                / operation_id
            )
            if stage.exists() or stage.is_symlink():
                return False, "移行準備の保存先を安全に作成できません。"
            stage.mkdir(parents=True)
            staged_files = []

            def stage_file(source, relative):
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                if destination.is_symlink() or not destination.is_file():
                    raise RuntimeError("staged migration file is unavailable")
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                if digest != hashlib.sha256(source.read_bytes()).hexdigest():
                    raise RuntimeError("staged migration file hash mismatch")
                staged_files.append({"path": relative.as_posix(), "sha256": digest})

            stage_file(start_source, Path("Start.bat"))
            stage_file(apply_source, Path("ApplyLegacyHostMigration.ps1"))
            for name in required_host:
                stage_file(
                    host_source / name,
                    Path("updater.current") / "update_host" / name,
                )
            for name in ("pyproject.toml", "uv.lock"):
                stage_file(self.install_dir / name, Path(name))

            record = {
                "schema_version": 1,
                "operation_id": operation_id,
                "source_version": str(self.current_version),
                "files": staged_files,
            }
            (stage / "record.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            creation_flags = _legacy_migration_creation_flags()
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-STA",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(stage / "ApplyLegacyHostMigration.ps1"),
                    "-Root",
                    str(self.project_root),
                    "-Stage",
                    str(stage),
                    "-WaitPid",
                    str(os.getpid()),
                    "-ShowProgress",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creation_flags,
            )
            return True, "更新方式の移行準備ができました。"
        except Exception as exc:
            logger.error("Legacy update host migration preparation failed: %s", type(exc).__name__)
            return False, "更新方式の移行を安全に準備できませんでした。"

    def bootstrap_bound_runtime(self):
        """空のruntime枠へ、現在appに結合された署名済みtargetだけを原子的に初回導入する。"""

        if self.runtime_repair_mode() != "runtime_bootstrap_required":
            return False, "現在の環境では準備ツールの初回導入を開始できません。"
        blocked = self._runtime_repair_block_reason()
        if blocked:
            return False, blocked
        destination = self.project_root / "runtime"
        destination_was_empty = False
        if destination.is_symlink():
            return False, "既存の準備ツールは直接置き換えません。"
        if destination.exists():
            try:
                destination_was_empty = (
                    destination.is_dir()
                    and next(destination.iterdir(), None) is None
                )
            except OSError:
                destination_was_empty = False
            if not destination_was_empty:
                return False, "既存の準備ツールは直接置き換えません。"
        phase = "release_validation"
        try:
            self._remove_generated_app_bytecode()
            exact = validate_release_tree(
                self.install_dir,
                expected_platform="windows",
                expected_cpu="x86_64",
                allow_persistent_state=True,
                allow_unlisted_legacy_overlay=self._legacy_overlay_allowed(),
            )
            manifest = exact["manifest"]
            if manifest["components"]["runtime"].get("present") is not True:
                return False, "現在版に結合された準備ツールがありません。"
            self._ensure_staging_root()
            if self.app_staging_dir.exists() or self.app_staging_dir.is_symlink():
                return False, "更新準備中のため、Nexus Arkを再起動してください。"
            phase = "runtime_acquire"
            manager = self.runtime_client_factory(
                self.project_root,
                update_url=self.runtime_update_url,
            )
            acquired = Path(manager.acquire(manifest)).absolute()
            expected = (self.staging_dir / "runtime").absolute()
            if acquired != expected or not acquired.is_dir() or acquired.is_symlink():
                return False, "署名済み準備ツールの配置を確認できません。"
            phase = "staging_validation"
            validate_bound_runtime(manifest, acquired)
            phase = "runtime_commit"
            if destination_was_empty:
                # Never recursively remove this directory. If another process
                # wrote even one entry, rmdir fails and commit stops.
                destination.rmdir()
            elif destination.exists() or destination.is_symlink():
                return False, "既存の準備ツールは直接置き換えません。"
            os.replace(acquired, destination)
            phase = "installed_validation"
            validate_bound_runtime(manifest, destination)
            return True, "署名済みのLite準備ツールを導入しました。"
        except Exception as exc:
            logger.error(
                "Lite runtime bootstrap failed during %s: %s",
                phase,
                type(exc).__name__,
            )
            return False, "署名済み準備ツールを安全に導入できませんでした。現在の環境は変更していません。"

    def _cleanup_old_archives(self):
        """
        過去のアップデートで不注意によりプロジェクトルートやインストールディレクトリ等に残ってしまった
        不要な更新アーカイブファイル (.tar.gz, .zip) を検索し、削除します。
        """
        # 検索対象ディレクトリ
        search_dirs = [self.project_root, self.install_dir]
        if hasattr(self, 'target_dir'):
            search_dirs.append(self.target_dir)
            
        for d in search_dirs:
            if not d.exists():
                continue
            # Nexus-Ark-*.tar.gz と Nexus-Ark-*.zip を対象にする
            for ext in [".tar.gz", ".zip"]:
                pattern = f"{self.APP_NAME}-*{ext}"
                for file_path in d.glob(pattern):
                    try:
                        if file_path.is_file():
                            os.remove(file_path)
                            logger.info(f"Cleaned up old archive file: {file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete old archive '{file_path}': {e}")

        # --- v0.2.3.0 誤配布データのクリーンアップ ---
        # v0.2.3.0 で開発者の個人アイテムデータ (data/items/) が
        # 誤って配布パッケージに含まれた。更新適用済みユーザーの環境から
        # これらのファイルを安全に削除する。
        # ユーザー自身のアイテムデータは characters/*/data/items/ に保存されるため影響しない。
        leaked_data_dir = self.install_dir / "data" / "items"
        if leaked_data_dir.exists():
            try:
                import shutil
                shutil.rmtree(leaked_data_dir)
                logger.info(f"Cleaned up leaked data directory: {leaked_data_dir}")
                # 親の data/ ディレクトリも空なら削除
                data_dir = self.install_dir / "data"
                if data_dir.exists() and not any(data_dir.iterdir()):
                    data_dir.rmdir()
                    logger.info(f"Removed empty data directory: {data_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up leaked data: {e}")

    def is_configured(self):
        """
        更新システムが正しく構成されているか確認します。
        """
        return (self.metadata_dir / "root.json").exists()

    def check_for_updates(self):
        """
        更新を確認し、新しいバージョンがあればその情報を返します。
        
        Returns:
            tuple: (new_version_string, message) または (None, message)
        """
        result = self.check_for_updates_result()
        if result["state"] == "available":
            version = str(result["version"])
            return version, f"新しいバージョン {version} が利用可能です。"
        if result["state"] == "no_update":
            return None, "最新バージョンを使用中です。"
        if result["state"] == "not_configured":
            return None, "更新サーバーが設定されていません。"
        return None, "更新確認エラーが発生しました。"

    def check_for_updates_result(self):
        """更新有無と確認失敗を混同しない非秘密の状態を返す。"""

        if not self.is_configured():
            logger.warning("Update system not configured: metadata/root.json is missing.")
            return {"state": "not_configured", "version": None}
        try:
            logger.info(f"Checking for updates from {self.update_url}...")
            new_archive_meta = self.client.check_for_updates()
        except Exception as exc:
            logger.error("Failed to check for updates: %s", type(exc).__name__)
            if isinstance(exc, BadVersionNumberError):
                return {
                    "state": "check_failed",
                    "version": None,
                    "error_code": "metadata_version_mismatch",
                }
            return {"state": "check_failed", "version": None}
        if new_archive_meta:
            version = str(new_archive_meta.version)
            logger.info("New version found: %s", version)
            return {"state": "available", "version": version}
        logger.info("No updates found.")
        return {"state": "no_update", "version": None}

    def _runtime_repair_block_reason(self):
        """更新hostが処理中または手動復旧待ちなら修復の二重開始を止める。"""

        recovery_dir = self.project_root / "update_recovery"
        if (recovery_dir / "transaction.lock").exists():
            return "前回の更新処理を確認中です。Nexus Arkを再起動してください。"
        journal_path = recovery_dir / "transaction.json"
        if journal_path.exists():
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                phase = str(journal.get("phase") or "") if isinstance(journal, dict) else ""
            except (OSError, ValueError, TypeError):
                return "更新の復旧状態を確認できません。Nexus Arkを再起動してください。"
            if phase == "manual_recovery_required":
                return "自動修復を続けられません。公式配布パッケージから復旧してください。"
            if phase not in {"", "committed", "rolled_back"}:
                return "前回の更新処理を確認中です。Nexus Arkを再起動してください。"
        runtime_staging = self.staging_dir / "runtime"
        if (runtime_staging.exists() or runtime_staging.is_symlink()) and not self.app_staging_dir.is_dir():
            return "前回の更新データが残っています。Nexus Arkを再起動してください。"
        return None

    def download_and_apply(self, progress_hook=None, *, require_runtime=False):
        """
        更新パッケージをダウンロードし、現在のインストール環境に適用します。
        適用後はアプリケーションの再起動が必要です。

        Args:
            progress_hook (callable): 進捗通知用コールバック関数

        Returns:
            tuple: (success_bool, message)
        """
        if not self.is_configured():
            return False, "更新システムが構成されていません。"

        if platform.system() == "Windows":
            host_status = self.update_host_status()
            if host_status["state"] != "ready":
                return False, host_status["message"]

        if require_runtime:
            blocked = self._runtime_repair_block_reason()
            if blocked:
                return False, blocked

        try:
            if platform.system() == "Windows":
                self._ensure_staging_root()
                if self.app_staging_dir.is_dir():
                    self._complete_windows_staging(
                        src_dir=self.app_staging_dir,
                        dst_dir=self.install_dir,
                        require_runtime=require_runtime,
                    )
                    return True, "準備済みの更新データを確認し、再起動を開始しました。"

            # 適用前に必ず最新のメタデータを再チェックして状態を更新する
            # (UIハンドラが毎回インスタンスを作り直すため、tufup側の状態を復元する必要がある)
            logger.info("Refreshing update state before application...")
            new_archive_meta = self.client.check_for_updates()
            if not new_archive_meta:
                 # すでに最新か、確認エラー
                 return False, "適用可能な更新が見つかりませんでした。"

            logger.info(f"Downloading update (Version: {new_archive_meta.version})...")
            
            if platform.system() == "Windows":
                if self.app_download_dir.exists() or self.app_download_dir.is_symlink():
                    if self.app_download_dir.is_symlink() or not self.app_download_dir.is_dir():
                        return False, "更新download stagingの配置を確認できません。"
                    shutil.rmtree(self.app_download_dir)
                completed = False

                def install(**kwargs):
                    nonlocal completed
                    self._install_windows_update(require_runtime=require_runtime, **kwargs)
                    completed = True

                self.client.download_and_apply_update(
                    progress_hook=progress_hook,
                    skip_confirmation=True,
                    install=install,
                )
                if not completed:
                    return False, "更新データを完全に準備できませんでした。再試行してください。"
                return True, "更新データの準備が完了しました。再起動を開始します。"
            else:
                # Linux/macOS では通常通り同期実行を試みる
                if self.client.download_and_apply_update(progress_hook=progress_hook, skip_confirmation=True):
                    logger.info("Update applied successfully.")
                    return True, "更新の適用に成功しました。まもなく自動的に再起動します..."
                else:
                    logger.warning("Failed to apply update.")
                    return False, "更新の適用に失敗しました。"
        except Exception as e:
            logger.error(f"Update application error: {e}")
            return False, f"予期せぬエラーが発生しました: {e}"




    def trigger_restart(self):
        """
        アプリケーションを明示的に再起動します。
        exit code 123 を返して終了し、ランチャーに後継処理を任せます。
        """
        logger.info("Restarting application (exit code 123)...")
        import threading
        import time
        import os

        # 更新後の再起動では新規タブを開かず、更新適用ボタンが仕込んだ旧タブ側の
        # ポーリングJSによる同一タブリロードに委ねる。ランチャー再起動では環境変数を
        # 引き継げないため、ファイルマーカーで新プロセスへ伝える。
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            cache_dir = os.path.join(base_dir, "cache")
            os.makedirs(cache_dir, exist_ok=True)
            with open(os.path.join(cache_dir, "restart_pending.marker"), "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except Exception as e:
            logger.warning(f"Failed to write restart marker: {e}")

        def _delayed_exit():
            time.sleep(5)
            os._exit(123)

        threading.Thread(target=_delayed_exit, daemon=True).start()

    @classmethod
    def quick_check(cls):
        """
        簡便に更新チェックを行うためのクラスメソッド。
        """
        instance = cls()
        return instance.check_for_updates()
