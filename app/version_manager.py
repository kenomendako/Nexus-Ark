import os
import json
import logging
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

LITE_CLOUD_RELEASE_CHANNEL_DISABLED = "disabled"
LITE_CLOUD_RELEASE_CHANNEL_STAGE2_CANARY = "stage2_canary"
LITE_CLOUD_RELEASE_CHANNEL_LIMITED_BETA = "limited_beta"
LITE_CLOUD_RELEASE_CHANNEL_KEY = "lite_cloud_new_setup_release_channel"
LITE_CLOUD_STAGE2_CANARY_VERSION = "0.5.0.21"
LITE_CLOUD_RELEASE_CHANNELS = frozenset(
    {
        LITE_CLOUD_RELEASE_CHANNEL_STAGE2_CANARY,
        LITE_CLOUD_RELEASE_CHANNEL_LIMITED_BETA,
    }
)


def resolve_lite_cloud_new_setup_release_channel(
    version_data: Mapping[str, Any] | None,
) -> str:
    """署名対象version metadataから初回準備の配布区分をfail-closedで返す。"""

    if not isinstance(version_data, Mapping):
        return LITE_CLOUD_RELEASE_CHANNEL_DISABLED
    channel = version_data.get(LITE_CLOUD_RELEASE_CHANNEL_KEY)
    if channel == LITE_CLOUD_RELEASE_CHANNEL_STAGE2_CANARY:
        if (
            version_data.get("version") == LITE_CLOUD_STAGE2_CANARY_VERSION
            and version_data.get("lite_cloud_stage2_e2e_canary") is True
        ):
            return channel
        return LITE_CLOUD_RELEASE_CHANNEL_DISABLED
    if channel == LITE_CLOUD_RELEASE_CHANNEL_LIMITED_BETA:
        if version_data.get("lite_cloud_stage2_e2e_canary") is not True:
            return channel
        return LITE_CLOUD_RELEASE_CHANNEL_DISABLED
    return LITE_CLOUD_RELEASE_CHANNEL_DISABLED


class VersionManager:
    VERSION_FILE = "version.json"
    LEGACY_MARKER = "nexus_ark.py"
    LEGACY_VERSION = "0.1.0-beta"

    @classmethod
    def get_current_version(cls):
        """自身のバージョンを version.json から取得する"""
        try:
            version_path = Path(__file__).parent / cls.VERSION_FILE
            if version_path.exists():
                with open(version_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("version", "unknown")
        except Exception as e:
            logger.error(f"Error reading current version: {e}")
        return "unknown"

    @classmethod
    def is_nexus_ark_dir(cls, path):
        """指定されたパスが Nexus Ark のディレクトリかどうか判定する"""
        p = Path(path)
        return (p / cls.LEGACY_MARKER).exists()

    @classmethod
    def get_dir_version(cls, path):
        """指定されたディレクトリのバージョンを判定する"""
        p = Path(path)
        version_file = p / cls.VERSION_FILE
        
        if version_file.exists():
            try:
                with open(version_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("version", "unknown")
            except Exception:
                pass
        
        # version.json がない場合で nexus_ark.py があれば v0.1.0 とみなす
        if (p / cls.LEGACY_MARKER).exists():
            return cls.LEGACY_VERSION
            
        return "unknown"

    @classmethod
    def find_legacy_candidates(cls):
        """旧環境の候補を主要な場所から探索する"""
        candidates = []
        search_roots = [
            Path(__file__).parent.parent,  # 兄弟ディレクトリ
            Path.home() / "Downloads",
            Path.home() / "Desktop",
            Path.home() / "Documents"
        ]
        
        # 重複を除去して存在チェック
        search_roots = list(set([p for p in search_roots if p.exists()]))
        current_dir = Path(__file__).parent.resolve()

        for root in search_roots:
            try:
                for item in root.iterdir():
                    if item.is_dir():
                        # 自身のディレクトリは除外
                        if item.resolve() == current_dir:
                            continue
                            
                        if cls.is_nexus_ark_dir(item):
                            candidates.append({
                                "path": str(item.absolute()),
                                "version": cls.get_dir_version(item)
                            })
            except Exception as e:
                logger.debug(f"Could not scan {root}: {e}")
                
        return candidates

if __name__ == "__main__":
    # デバッグ用出力
    print(f"Current Version: {VersionManager.get_current_version()}")
    print("Searching for legacy candidates...")
    for c in VersionManager.find_legacy_candidates():
        print(f"- {c['version']} at {c['path']}")
