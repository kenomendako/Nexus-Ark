"""新ZIP専用の初回更新入口。旧project/venvをimportしない。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

# -Iで環境PYTHONPATHを無視し、この信頼したZIPのsourceだけを追加する。
APP = Path(__file__).absolute().parent.parent
sys.path.insert(0, str(APP))


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--uv', required=True, type=Path)
    args = parser.parse_args()
    try:
        if os.name != 'nt':
            raise RuntimeError('windows-required')
        if args.source_root.absolute() / 'app' != APP:
            raise RuntimeError('source-boundary')
        from update_host.contracts import validate_release_tree
        validate_release_tree(APP, expected_platform='windows', expected_cpu='x86_64')
        from legacy_bootstrap.installer import install, BootstrapError, package_default_update_url
        phases = {'download', 'runtime', 'copy', 'environment', 'trial', 'switch', 'complete'}
        def progress(phase):
            mapped = phase if phase in phases else ('runtime' if 'runtime' in phase else 'download')
            emit({'event': 'progress', 'phase': mapped})
        # 配布先の既定値を新ZIPの公開設定から読む。旧moduleはimportしない。
        default = package_default_update_url(args.source_root)
        result = install(args.root, args.source_root, args.uv, default_update_url=default, progress=progress)
        emit(result)
        return 0
    except Exception as exc:
        code = getattr(exc, 'code', 'verification-failed')
        if type(exc).__name__ == 'LegacyBootstrapError':
            detail = str(exc).lower()
            if 'policy' in detail or 'release certifi' in detail:
                code = 'secure-release-required'
            elif 'url' in detail:
                code = 'invalid-update-url'
            elif 'metadata' in detail:
                code = 'signature-verification-failed'
            elif 'target' in detail or 'download' in detail:
                code = 'download-verification-failed'
        allowed = {'unsafe-path', 'invalid-input', 'invalid-update-url', 'windows-required',
                   'data-read-failed', 'data-changed', 'process-or-lock', 'environment-failed',
                   'environment-verification-failed', 'trial-port-unavailable', 'trial-exited',
                   'trial-timeout', 'trial-stop-unconfirmed', 'source-target-overlap', 'legacy-only',
                   'insufficient-space', 'post-swap-verification', 'unexpected-trial-marker',
                   'fresh-preparation-required', 'verification-failed', 'secure-release-required',
                   'signature-verification-failed', 'download-verification-failed'}
        if code not in allowed:
            code = 'verification-failed'
        # 内部例外、URL、path、設定内容をGUIへ転送しない。
        emit({'event': 'result', 'state': 'blocked', 'error_code': code,
              'recovery_state': 'preserved-review-required'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
