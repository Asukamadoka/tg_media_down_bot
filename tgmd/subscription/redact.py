"""Keeping the subscription URL out of everything that is written down.

The URL is a credential: whoever holds it can use the balance. It lives in one kv
value and in memory while it is being handled; every log line, message, exception
and audit row uses :func:`redact_url` instead (docs/wms/M9.6 §C). Never ``repr`` a URL.
"""

from __future__ import annotations

from urllib.parse import urlsplit

INVALID = "<invalid url>"


def redact_url(url: object) -> str:
    """``scheme://host/…`` and nothing else: no userinfo, port, path or query."""
    try:
        parts = urlsplit(str(url).strip())
        host = parts.hostname or ""
    except ValueError:
        return INVALID
    if not parts.scheme or not host:
        return INVALID
    return f"{parts.scheme.lower()}://{host}/…"


def scrub(text: str, *urls: str) -> str:
    """``text`` with every one of ``urls`` replaced by its redacted form."""
    for url in urls:
        if url:
            text = text.replace(url, redact_url(url))
    return text
