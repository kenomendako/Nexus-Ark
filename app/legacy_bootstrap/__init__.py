"""旧Windowsルートを起動せずに更新を準備するbootstrap境界。"""

from .acquire import (
    APP_NAME,
    MAX_ARCHIVE_BYTES,
    MAX_EXTRACTED_BYTES,
    MAX_FILES,
    LegacyBootstrapError,
    acquire_release,
    validate_bootstrap_policy,
)

__all__ = [
    "APP_NAME",
    "MAX_ARCHIVE_BYTES",
    "MAX_EXTRACTED_BYTES",
    "MAX_FILES",
    "LegacyBootstrapError",
    "acquire_release",
    "validate_bootstrap_policy",
]
