"""旧Windowsルートを起動せずに更新を準備するbootstrap境界。"""

__all__ = [
    "APP_NAME",
    "MAX_ARCHIVE_BYTES",
    "MAX_EXTRACTED_BYTES",
    "MAX_FILES",
    "LegacyBootstrapError",
    "acquire_release",
    "validate_bootstrap_policy",
]


def __getattr__(name):
    # データコピーには標準ライブラリだけを使用する。TUF取得依存は、
    # 実際に旧版更新APIが要求された時だけ読み込む。
    if name not in __all__:
        raise AttributeError(name)
    from importlib import import_module
    value = getattr(import_module('.acquire', __name__), name)
    globals()[name] = value
    return value
