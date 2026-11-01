"""Nexus Ark の通常 HTTPS 通信に使う証明書検証コンテキスト。"""

from __future__ import annotations

import ssl

import certifi


def create_tls_context() -> ssl.SSLContext:
    """certifi の CA バンドルだけを信頼する検証必須の TLS コンテキストを作る。

    ``cafile`` を明示することで、OS の既定 trust store や ``SSL_CERT_FILE`` に
    フォールバックせず、配布物に固定した certifi のルート集合を使用する。
    """

    context = ssl.create_default_context(cafile=certifi.where())
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context
