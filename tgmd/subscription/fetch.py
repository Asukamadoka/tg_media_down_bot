"""The one place the bot downloads a subscription (docs/wms/M9.6 §D).

Plain ``urllib``: no environment proxy (the bot sits in the proxy's own network and
the host must be routed DIRECT), no redirects (a redirect would walk past the
SSRF guard), a time limit and a size cap. Errors carry a stable code, never the URL.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

TIMEOUT = 15.0
MAX_BYTES = 2 * 1024 * 1024


class FetchError(Exception):
    """The download failed. ``code``: ``timeout``, ``network``, ``tls``,
    ``too_large`` or ``encoding``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Fetched:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    """Header names in lower case."""


Fetcher = Callable[[str], Fetched]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _inflate(body: bytes, cap: int) -> bytes:
    try:
        if body[:2] == b"\x1f\x8b":
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        else:
            inflater = zlib.decompressobj()
        out = inflater.decompress(body, cap + 1)
    except zlib.error:
        raise FetchError("encoding") from None
    if len(out) > cap:
        raise FetchError("too_large")
    return out


def make_fetcher(user_agent: str, *, timeout: float = TIMEOUT, cap: int = MAX_BYTES) -> Fetcher:
    """A fetcher for :class:`~tgmd.subscription.service.SubscriptionService`."""

    def fetch(url: str) -> Fetched:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        request = urllib.request.Request(url, headers={
            "User-Agent": user_agent, "Accept": "*/*", "Accept-Encoding": "gzip"})
        try:
            with opener.open(request, timeout=timeout) as response:
                headers = {k.lower(): v for k, v in response.headers.items()}
                body = response.read(cap + 1)
                status = response.status
        except urllib.error.HTTPError as exc:  # 3xx (not followed), 4xx, 5xx: no body wanted
            return Fetched(exc.code, b"", {k.lower(): v for k, v in exc.headers.items()})
        except TimeoutError:
            raise FetchError("timeout") from None
        except urllib.error.URLError as exc:
            raise FetchError("tls" if isinstance(exc.reason, ssl.SSLError) else "network") from None
        except ssl.SSLError:
            raise FetchError("tls") from None
        except (OSError, ValueError):
            raise FetchError("network") from None
        if len(body) > cap:
            raise FetchError("too_large")
        if headers.get("content-encoding", "").lower() in ("gzip", "deflate"):
            body = _inflate(body, cap)
        return Fetched(status, body, headers)

    return fetch
