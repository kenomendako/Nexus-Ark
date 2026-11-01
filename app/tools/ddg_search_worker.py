"""Isolated DuckDuckGo search worker.

``primp`` (the native transport used by ``ddgs``) keeps its root store in a
process-global cache.  Running the provider in a short-lived interpreter lets
us select the bundled certifi store before that cache is initialized without
changing the trust settings of the main Nexus Ark process.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import certifi
from ddgs import DDGS


_MAX_QUERY_LENGTH = 4_000
_MAX_RESULTS = 10


def _write(payload: dict[str, Any]) -> None:
    """Write one JSON response and nothing else to stdout."""

    json.dump(payload, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")


def _request_from_stdin() -> dict[str, Any]:
    # The parent sends a small bounded request.  Refuse oversized input before
    # parsing it so a broken caller cannot turn the worker into an unbounded
    # memory sink.
    raw = sys.stdin.read(32_768)
    if len(raw) >= 32_768:
        raise ValueError("request-too-large")
    request = json.loads(raw)
    if not isinstance(request, dict):
        raise ValueError("request-invalid")
    return request


def main() -> int:
    try:
        request = _request_from_stdin()
        query = request.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_LENGTH:
            raise ValueError("query-invalid")

        max_results = request.get("max_results", 5)
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise ValueError("max-results-invalid")
        max_results = max(1, min(max_results, _MAX_RESULTS))

        proxy = request.get("proxy")
        if proxy is not None and (not isinstance(proxy, str) or len(proxy) > 2_048):
            raise ValueError("proxy-invalid")

        # This is intentionally the first primp client in this interpreter.
        # The parent process never receives PRIMP_CA_BUNDLE or CA_CERT_FILE.
        results = DDGS(
            proxy=proxy or None,
            timeout=10,
            verify=certifi.where(),
        ).text(query.strip(), max_results=max_results)

        # Keep the IPC contract small and limited to fields consumed by the UI.
        normalized: list[dict[str, str]] = []
        for item in results or []:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "title": str(item.get("title") or "No Title")[:2000],
                    "href": str(item.get("href") or "#")[:8192],
                    "body": str(item.get("body") or "")[:16000],
                }
            )
        _write({"ok": True, "results": normalized[:max_results]})
        return 0
    except Exception as exc:  # noqa: BLE001 - worker boundary must be total
        # Do not return exception text: native errors can contain local paths,
        # proxy credentials, or certificate details.  The parent only needs a
        # stable category for the user-facing fallback.
        name = type(exc).__name__
        code = "timeout" if "timeout" in name.lower() else "provider-error"
        _write({"ok": False, "error": code})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
