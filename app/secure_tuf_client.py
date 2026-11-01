"""TUF client adapter with an explicit CA bundle.

``tufup.client.Client`` creates an ``AuthRequestsFetcher`` internally.  The
default requests session leaves CA selection to the environment, so
``REQUESTS_CA_BUNDLE``/``CURL_CA_BUNDLE`` can make an otherwise signed update
request use an unintended trust store.  This adapter keeps tufup's metadata,
signature, authentication, and progress behaviour while replacing only the
fetcher before any network operation starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union
from urllib import parse

import certifi
import requests
from requests.auth import AuthBase
import tuf
from tufup.client import AuthRequestsFetcher, Client


def _certifi_bundle() -> str:
    """Return the packaged CA path, failing closed if it is unavailable."""

    path = Path(certifi.where())
    if not path.is_file():
        raise RuntimeError("certifiのCA bundleを確認できません。")
    return str(path)


class SecureAuthRequestsFetcher(AuthRequestsFetcher):
    """tufup's authenticated fetcher with an explicit requests CA path."""

    def __init__(
        self,
        session_auth: Optional[Dict[str, Union[Tuple[str, str], AuthBase]]] = None,
    ) -> None:
        super().__init__(session_auth=session_auth)
        self.ca_bundle = _certifi_bundle()

    def _get_session(self, url: str):
        """Create the same per-host sessions as tuf, with a pinned CA.

        ``requests.Session.verify = <path>`` alone is insufficient: its
        ``merge_environment_settings`` replaces a ``None`` request argument
        with ``REQUESTS_CA_BUNDLE`` before merging the session defaults.  The
        session subclass below forces the value after that merge while
        retaining requests' normal proxy and authentication handling.
        """
        parsed_url = parse.urlparse(url)
        if not parsed_url.scheme:
            raise ValueError(f"Failed to parse URL {url}")

        session_index = (parsed_url.scheme, parsed_url.hostname or "")
        session = self._sessions.get(session_index)
        if session is None:
            session = _PinnedCASession(self.ca_bundle)
            self._sessions[session_index] = session
            session.headers["User-Agent"] = f"tuf/{tuf.__version__} {session.headers['User-Agent']}"

        # Keep AuthRequestsFetcher' per-origin auth mapping unchanged.
        key = parse.urlunparse(
            parse.ParseResult._make(
                [parsed_url.scheme, parsed_url.netloc, "", "", "", ""]
            )
        )
        session.auth = self.session_auth.get(key)
        return session


class _PinnedCASession(requests.Session):
    """requests session that cannot be redirected to an environment CA."""

    def __init__(self, ca_bundle: str) -> None:
        super().__init__()
        self._nexus_ca_bundle = ca_bundle
        self.verify = ca_bundle

    def merge_environment_settings(self, url, proxies, stream, verify, cert):
        settings = super().merge_environment_settings(url, proxies, stream, verify, cert)
        # Keep proxies, auth, stream and cert from the normal requests merge,
        # but reject both CA environment variables and verify=False.
        settings["verify"] = self._nexus_ca_bundle
        return settings


class SecureTufClient(Client):
    """``tufup.Client`` using :class:`SecureAuthRequestsFetcher`."""

    def __init__(self, *args: Any, session_auth=None, **kwargs: Any) -> None:
        # Client initialization reads local root metadata but does not fetch
        # remote data.  Replace the fetcher immediately afterwards, before
        # callers can invoke refresh/download methods.
        super().__init__(*args, session_auth=session_auth, **kwargs)
        default_fetcher = getattr(self, "_fetcher", None)
        if not isinstance(default_fetcher, AuthRequestsFetcher):
            # _fetcher is the explicit dependency boundary used by
            # python-tuf's Updater.  Refuse to proceed if tufup changes it.
            raise RuntimeError("tufupのfetcher契約を確認できません。")
        self._fetcher = SecureAuthRequestsFetcher(session_auth=session_auth)


__all__ = ["SecureAuthRequestsFetcher", "SecureTufClient"]
