"""新ZIPの信頼rootから、保管済み署名・root更新・targetを再検証する。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
from urllib.parse import urlsplit

from tuf.api.metadata import Metadata
from tuf.ngclient._internal.trusted_metadata_set import TrustedMetadataSet
from tuf.ngclient.config import EnvelopeType

from update_host.contracts import manifest_digest


class RootHistoryFetcher:
    """Clientの署名検証はそのまま、取得したroot更新だけを保管する。"""
    def __init__(self, delegate, directory: Path):
        self.delegate = delegate
        self.directory = directory

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def download_bytes(self, url, max_length):
        data = self.delegate.download_bytes(url, max_length)
        name = urlsplit(url).path.rsplit('/', 1)[-1]
        if re.fullmatch(r'[1-9][0-9]*\.root\.json', name):
            from .acquire import _assert_directory
            _assert_directory(self.directory, 'root history', create=True)
            path = self.directory / name
            with path.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        return data


def record_roots(client, work_dir: Path) -> None:
    fetcher = getattr(client, '_fetcher', None)
    if fetcher is not None:
        client._fetcher = RootHistoryFetcher(fetcher, work_dir / 'root-history')


def verify_saved_target(work_dir: Path, trusted_root: Path, acquisition: dict) -> str:
    """署名付きmetadataとblobを照合し、署名archiveのmanifest digestを返す。"""
    metadata = work_dir / 'metadata'
    anchor = trusted_root.read_bytes()
    trusted = TrustedMetadataSet(anchor, EnvelopeType.METADATA)
    final_root = (metadata / 'root.json').read_bytes()
    final_version = Metadata.from_bytes(final_root).signed.version
    if final_version < trusted.root.version or final_version > trusted.root.version + 1024:
        raise ValueError('root history version out of range')
    last = anchor
    for version in range(trusted.root.version + 1, final_version + 1):
        last = (work_dir / 'root-history' / f'{version}.root.json').read_bytes()
        trusted.update_root(last)
    if final_root != last:
        raise ValueError('cached root differs from verified root chain')
    trusted.update_timestamp((metadata / 'timestamp.json').read_bytes())
    trusted.update_snapshot((metadata / 'snapshot.json').read_bytes())
    targets = trusted.update_targets((metadata / 'targets.json').read_bytes())
    name = acquisition['target_name']
    if not isinstance(name, str) or not re.fullmatch(r'Nexus-Ark-[0-9A-Za-z.+_-]+\.tar\.gz', name):
        raise ValueError('invalid target name')
    target = targets.targets[name]
    from .acquire import _target_path_is_safe, _target_metadata_length
    _target_path_is_safe(target.path, name)
    length = acquisition.get('target_length')
    if type(length) is not int or length != _target_metadata_length(target):
        raise ValueError('invalid acquisition target length')
    if not isinstance(acquisition.get('target_sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', acquisition['target_sha256']):
        raise ValueError('invalid acquisition target hash')
    blob = work_dir / 'targets' / name
    with blob.open('rb') as stream:
        target.verify_length_and_hashes(stream)
    if blob.stat().st_size != acquisition['target_length']:
        raise ValueError('target length differs from acquisition')
    digest = hashlib.sha256()
    with blob.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != acquisition['target_sha256']:
        raise ValueError('target hash differs from acquisition')
    # directoryのmanifestを自己申告として信頼せず、署名blobから取り直す。
    with tarfile.open(blob, 'r:gz') as archive:
        entries = [entry for entry in archive if entry.name in ('release_manifest.json', './release_manifest.json')]
        if len(entries) != 1 or not entries[0].isfile() or entries[0].size > 8 * 1024 * 1024:
            raise ValueError('invalid signed manifest')
        stream = archive.extractfile(entries[0])
        if stream is None:
            raise ValueError('missing signed manifest')
        signed_manifest = json.load(stream)
    result = manifest_digest(signed_manifest)
    if result != acquisition['manifest_digest']:
        raise ValueError('manifest differs from signed target')
    return result
