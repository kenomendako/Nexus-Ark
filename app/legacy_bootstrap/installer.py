"""旧アプリを実行しない初回更新の組み立て・起動確認。

新ZIPの取得用venvだけで動作し、旧app/旧venvのPython moduleをimportしない。
選択したrootは最後のdirectory transactionまで読み取り専用で扱う。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import subprocess
import time
import uuid
from typing import Callable, Mapping

from packaging.version import Version
from update_host.contracts import classify_release_path, is_generated_release_state, validate_release_tree
from update_host.runtime import validate_bound_runtime, _runtime_io_path
from update_host.supervisor import UpdateSupervisor
from update_host.trial import validate_trial_marker

from .acquire import acquire_release
from .transaction import create_workspace, commit_swap, recover_swap, workspace_path

RESERVE_BYTES = 512 * 1024 * 1024
ENVIRONMENT_BUDGET = 3 * 1024 * 1024 * 1024
REPARSE = 0x400
EXCLUDED_DATA = ('.venv', '__pycache__', '.git', 'cloud/lite-relay/node_modules')
PYTHON_DOWNLOADS_CATALOG_SHA256 = '03d6e44c6566cb8af8b429cc64265797e918a9221250cd41dbbf2b6e87fc23db'


class BootstrapError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _trusted_python_downloads_catalog(root: Path) -> Path:
    """Return only the hash-pinned catalog shipped by the signed app."""

    catalog = root / 'app/release/python-downloads.json'
    try:
        _safe_ancestors(catalog.parent)
        _regular(catalog)
        actual = _hash(catalog)
    except (BootstrapError, OSError):
        raise BootstrapError('python-downloads-catalog') from None
    if actual != PYTHON_DOWNLOADS_CATALOG_SHA256:
        raise BootstrapError('python-downloads-catalog')
    return catalog.absolute()


def _regular(path: Path, *, directory=False) -> None:
    info = path.lstat()
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & REPARSE:
        raise BootstrapError('unsafe-path')
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise BootstrapError('unsafe-path')


def _safe_ancestors(path: Path) -> None:
    for parent in list(path.absolute().parents)[::-1] + [path.absolute()]:
        if os.path.lexists(parent):
            _regular(parent, directory=True)


def _read_json(path: Path, limit=64 * 1024) -> dict:
    _regular(path)
    if path.stat().st_size > limit:
        raise BootstrapError('invalid-input')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise BootstrapError('invalid-input')
    return data


def _excluded(relative: str) -> bool:
    return (any(relative == prefix or relative.startswith(prefix + '/') for prefix in EXCLUDED_DATA)
            or relative == 'nexus_ark.lock' or is_generated_release_state(relative))


def persistent_files(root: Path) -> dict[str, Path]:
    """catalogの永続fileだけを列挙。古いvenv/native依存は持ち込まない。"""
    app = root / 'app'
    result = {}
    def walk_error(error):
        raise BootstrapError('data-read-failed') from error
    for current, directories, files in os.walk(app, topdown=True, followlinks=False, onerror=walk_error):
        parent = Path(current)
        for name in list(directories):
            child = parent / name
            relative = child.relative_to(app).as_posix()
            _regular(child, directory=True)
            if _excluded(relative):
                directories.remove(name)
        for name in files:
            source = parent / name
            relative = source.relative_to(app).as_posix()
            if _excluded(relative):
                continue
            _regular(source)
            if classify_release_path(relative) != 'owned':
                if source.stat().st_nlink != 1:
                    raise BootstrapError('unsafe-path')
                result['app/' + relative] = source
    config = root / 'config.json'
    if os.path.lexists(config):
        _regular(config)
        if config.stat().st_nlink != 1:
            raise BootstrapError('unsafe-path')
        result['config.json'] = config
    return result


def copy_user_data(root: Path, destination: Path) -> dict[str, str]:
    snapshot = {}
    for relative, source in persistent_files(root).items():
        target = destination / relative
        _safe_ancestors(target.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = _hash(source)
        shutil.copy2(source, target)
        if _hash(target) != before or _hash(source) != before:
            raise BootstrapError('data-changed')
        snapshot[relative] = before
    return snapshot


def _unchanged_user_data(root: Path, snapshot: Mapping[str, str]) -> bool:
    return {relative: _hash(path) for relative, path in persistent_files(root).items()} == dict(snapshot)


def package_default_update_url(source_root: Path) -> str:
    """新ZIPの更新先定数をASTで読む。アプリmoduleを実行しない。"""
    path = source_root / 'app/update_manager.py'
    _regular(path)
    if path.stat().st_size > 1024 * 1024:
        raise BootstrapError('invalid-update-url')
    parsed = ast.parse(path.read_text(encoding='utf-8-sig'))
    values = []
    for node in parsed.body:
        if isinstance(node, ast.ClassDef) and node.name == 'UpdateManager':
            for member in node.body:
                if isinstance(member, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'DEFAULT_UPDATE_URL' for t in member.targets):
                    if isinstance(member.value, ast.Constant) and isinstance(member.value.value, str):
                        values.append(member.value.value)
    if len(values) != 1:
        raise BootstrapError('invalid-update-url')
    return values[0]


def choose_update_url(root: Path, default: str) -> str:
    # JSONだけを読む。旧config_managerや旧更新moduleを起動しない。
    for path in (root / 'config.json', root / 'app/config.json'):
        if os.path.lexists(path):
            value = _read_json(path, limit=4 * 1024 * 1024).get('update_url')
            if value:
                if not isinstance(value, str):
                    raise BootstrapError('invalid-update-url')
                return value
    return default


def check_stopped(root: Path, source_root: Path) -> None:
    if os.name != 'nt':
        raise BootstrapError('windows-required')
    helper = source_root / 'app/release/CheckLegacyBootstrapTarget.ps1'
    _regular(helper)
    system_root = os.environ.get('SYSTEMROOT', r'C:\Windows')
    powershell = Path(system_root) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive',
                             '-ExecutionPolicy', 'Bypass', '-File', str(helper), '-Root', str(root),
                             '-SourceRoot', str(source_root)],
                            capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise BootstrapError('process-or-lock')


def environment(root: Path, uv: Path, *, log: Path) -> None:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(('PYTHON', 'PIP_', 'UV_INDEX', 'UV_DEFAULT_INDEX', 'UV_EXTRA_INDEX', 'UV_CONFIG')):
            env.pop(key, None)
    catalog = _trusted_python_downloads_catalog(root)
    env.pop('UV_PYTHON_DOWNLOADS_JSON_URL', None)
    env['UV_PYTHON_DOWNLOADS_JSON_URL'] = str(catalog)
    env['UV_PROJECT_ENVIRONMENT'] = str(root / '.venv')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PATHEXT'] = '.COM;.EXE;.BAT;.CMD'
    minimum = str(_read_json(root / 'app/version.json').get('min_python_version', '3.12.16'))
    if not minimum.startswith('3.12.') or not minimum.rsplit('.', 1)[-1].isdigit():
        raise BootstrapError('unsupported-python-version')
    minimum = str(max(Version(minimum), Version('3.12.16')))
    with log.open('wb') as output:
        result = subprocess.run([str(uv), 'sync', '--no-config', '--project', str(root / 'app'),
                                 '--frozen', '--no-install-project', '--python', 'cpython@' + minimum,
                                 '--managed-python', '--extra', 'win'],
                                cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT,
                                timeout=1800, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise BootstrapError('environment-failed')
    verify_environment(root)


def verify_environment(root: Path) -> None:
    """rename後・復旧時も実行されるPythonとCAの最低条件を確認する。"""
    minimum = str(_read_json(root / 'app/version.json').get('min_python_version', '3.12.16'))
    if not minimum.startswith('3.12.') or not minimum.rsplit('.', 1)[-1].isdigit():
        raise BootstrapError('unsupported-python-version')
    minimum = str(max(Version(minimum), Version('3.12.16')))
    python = root / '.venv/Scripts/python.exe'
    _regular(python)
    probe = subprocess.run([str(python), '-I', '-c',
                            'import certifi,sys; from packaging.version import Version; assert Version(certifi.__version__) >= Version("2026.10.23"); '
                            f'assert sys.implementation.name == "cpython" and Version(sys.version.split()[0]) >= Version("{minimum}")'],
                           capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if probe.returncode:
        raise BootstrapError('environment-verification-failed')


def _stop_owned(process) -> bool:
    if os.name == 'nt':
        from update_host.windows_processes import terminate_owned_process_tree
        return terminate_owned_process_tree(process, 15.0)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return process.poll() is not None


# 初回移行は依存のcold import、サンプル展開、旧データ移行も行う。
# 通常更新hostの300秒とは分け、旧rootを保持したまま最大10分確認する。
DEFAULT_INITIAL_TRIAL_TIMEOUT_SECONDS = 600.0


def trial(root: Path, exact: Mapping, *, log: Path, timeout=DEFAULT_INITIAL_TRIAL_TIMEOUT_SECONDS,
          process_factory=subprocess.Popen, stop=_stop_owned) -> None:
    app = root / 'app'
    marker = root / 'update_recovery/trial-success.json'
    marker.parent.mkdir(exist_ok=True)
    if marker.exists():
        raise BootstrapError('unexpected-trial-marker')
    supervisor = UpdateSupervisor(root, environ=os.environ.copy())
    port = supervisor._read_trial_port(app)
    # 起動済み別processのHTTP応答をhealth成功としない。
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', port))
        except OSError as exc:
            raise BootstrapError('trial-port-unavailable') from exc
    operation = uuid.uuid4().hex
    token = secrets.token_hex(32)
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'NEXUS_ARK_UPDATE_TRIAL_MARKER'):
        env.pop(key, None)
    env.update({'NEXUS_ARK_UPDATE_TRIAL': '1', 'NEXUS_ARK_UPDATE_OPERATION_ID': operation,
                'NEXUS_ARK_UPDATE_TARGET_VERSION': exact['manifest']['release_version'],
                'NEXUS_ARK_UPDATE_MANIFEST_DIGEST': exact['manifest_digest'],
                'NEXUS_ARK_UPDATE_PROCESS_TOKEN': token, 'NEXUS_ARK_UPDATE_TRIAL_MARKER': str(marker),
                'NEXUS_ARK_NO_BROWSER': '1', 'NEXUS_ARK_ALLOW_PORT_FALLBACK': '0',
                'NEXUS_ARK_PORT': str(port), 'GRADIO_SERVER_PORT': str(port),
                'GRADIO_ANALYTICS_ENABLED': 'False', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'})
    process = None
    try:
        with log.open('wb') as output:
            process = process_factory([str(root / '.venv/Scripts/python.exe'), str(app / 'nexus_ark.py')],
                                       cwd=app, env=env, stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise BootstrapError('trial-exited')
                if marker.exists():
                    validate_trial_marker(marker, operation_id=operation,
                                          release_version=exact['manifest']['release_version'],
                                          manifest_digest=exact['manifest_digest'], process_token=token, port=port)
                    if process.poll() is not None:
                        raise BootstrapError('trial-exited')
                    return
                time.sleep(0.25)
            raise BootstrapError('trial-timeout')
    finally:
        if process is not None and not stop(process):
            raise BootstrapError('trial-stop-unconfirmed')


def validate_installed(root: Path, workspace: Path, receipt: Mapping, *, trusted_root: Path) -> bool:
    acquisition = _read_json(workspace / 'acquisition.json')
    from .verification import verify_saved_target
    signed_digest = verify_saved_target(workspace / 'download', trusted_root, acquisition)
    exact = validate_release_tree(root / 'app', expected_platform='windows', expected_cpu='x86_64',
                                  allow_persistent_state=True)
    if exact['manifest_digest'] != receipt['ready_manifest_digest'] or exact['manifest_digest'] != signed_digest:
        return False
    # 再実行時も保存した署名検証済みtreeのdigestと突き合わせる。
    source = validate_release_tree(workspace / 'download/app', expected_platform='windows', expected_cpu='x86_64')
    if (source['manifest_digest'] != exact['manifest_digest']
            or acquisition['manifest_digest'] != exact['manifest_digest']):
        return False
    from .acquire import validate_bootstrap_policy
    validate_bootstrap_policy(root / 'app', exact)
    if exact['manifest']['components']['runtime']['present']:
        validate_bound_runtime(exact['manifest'], root / 'runtime')
    for relative in ('Start.bat', 'pyproject.toml', 'uv.lock'):
        template = root / 'app' / ('release/update_host_bridge/Start.bat' if relative == 'Start.bat' else relative)
        _regular(root / relative)
        if _hash(root / relative) != _hash(template):
            return False
    for path in (root / 'app/update_host').glob('*.py'):
        target = root / 'updater/current/update_host' / path.name
        _regular(target)
        if _hash(target) != _hash(path):
            return False
    verify_environment(root)
    return True


def install(root: Path, source_root: Path, uv: Path, *, default_update_url: str,
            acquire=acquire_release, guard=check_stopped, env_builder=environment,
            trial_runner=trial, free_bytes=None, progress=lambda phase: None, fault=None,
            installed_validator=validate_installed) -> dict:
    root = root.absolute()
    source_root = source_root.absolute()
    _safe_ancestors(root.parent)
    _safe_ancestors(source_root)
    if root == source_root or root in source_root.parents or source_root in root.parents:
        raise BootstrapError('source-target-overlap')
    workspace = workspace_path(root)
    if workspace.exists():
        recovered = recover_swap(root, workspace, validate_new=lambda r, w, rec: installed_validator(
            r, w, rec, trusted_root=source_root / 'metadata/root.json'))
        if recovered['state'] == 'committed':
            return {'event': 'result', 'state': 'complete', 'recovered': True,
                    'target_version': _read_json(root / 'app/version.json')['version']}
    guard(root, source_root)
    _regular(root / 'Start.bat')
    start_text = (root / 'Start.bat').read_text(encoding='utf-8-sig').lower()
    if 'robocopy' not in start_text and 'update_host.supervisor' not in start_text:
        raise BootstrapError('unsupported-launcher')
    old_version = str(_read_json(root / 'app/version.json')['version'])
    Version(old_version)
    update_url = choose_update_url(root, default_update_url)
    size = sum(path.stat().st_size for path in persistent_files(root).values())
    available = shutil.disk_usage(root.parent).free if free_bytes is None else free_bytes
    if available < size + ENVIRONMENT_BUDGET + 600 * 1024 * 1024 + RESERVE_BYTES:
        raise BootstrapError('insufficient-space')
    workspace = create_workspace(root)
    progress('download')
    # 旧方式で本体だけ新版になった場合も、旧Startのままなら同版の
    # 署名済み一式で環境を再構築する。通常更新のversion比較は変えない。
    acquisition_options = {'allow_current_version': True} if 'robocopy' in start_text else {}
    app, exact, acquisition = acquire(work_dir=workspace / 'download',
                                      trusted_root=source_root / 'metadata/root.json',
                                      update_url=update_url, current_version=old_version,
                                      progress=progress, **acquisition_options)
    next_root = workspace / 'next-root'
    shutil.copytree(app, next_root / 'app')
    runtime_source = acquisition.get('runtime_path')
    if runtime_source:
        progress('runtime')
        runtime_source = workspace / 'download' / runtime_source
        if not runtime_source.absolute().is_relative_to((workspace / 'download').absolute()):
            raise BootstrapError('unsafe-path')
        shutil.copytree(_runtime_io_path(runtime_source), _runtime_io_path(next_root / 'runtime'))
    progress('copy')
    snapshot = copy_user_data(root, next_root)
    shutil.copy2(app / 'release/update_host_bridge/Start.bat', next_root / 'Start.bat')
    for name in ('pyproject.toml', 'uv.lock'):
        shutil.copy2(app / name, next_root / name)
    shutil.copytree(app / 'update_host', next_root / 'updater/current/update_host')
    shutil.copytree(workspace / 'download/metadata', next_root / 'metadata')
    # archive情報は公開版・hashのみ。APIキー/会話/設定を記録しない。
    (workspace / 'acquisition.json').write_text(json.dumps(acquisition, sort_keys=True) + '\n', encoding='utf-8')
    progress('environment')
    env_builder(next_root, uv, log=workspace / 'download/environment.log')
    progress('trial')
    trial_runner(next_root, exact, log=workspace / 'download/trial.log')
    if not _unchanged_user_data(root, snapshot):
        raise BootstrapError('data-changed')
    validate_release_tree(next_root / 'app', expected_platform='windows', expected_cpu='x86_64',
                          allow_persistent_state=True)
    if not installed_validator(next_root, workspace, {'ready_manifest_digest': exact['manifest_digest']},
                               trusted_root=source_root / 'metadata/root.json'):
        raise BootstrapError('verification-failed')
    progress('switch')
    receipt = commit_swap(root, workspace, ready_manifest_digest=exact['manifest_digest'],
                          guard=lambda: guard(root, source_root), fault=fault,
                          validate_new=lambda r, w, rec: installed_validator(
                              r, w, rec, trusted_root=source_root / 'metadata/root.json'))
    progress('complete')
    return {'event': 'result', 'state': 'complete', 'target_version': exact['manifest']['release_version']}
