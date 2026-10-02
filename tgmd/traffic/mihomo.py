"""A read-only client for mihomo's controller API.

Only ``GET``. The bot never changes groups, configs or providers: a route
change is a deploy-time edit, and the single Telegram exit must stay stable
(docs/wms/M9 §D.4).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable

Fetch = Callable[[str], dict]


def http_get_json(url: str, timeout: float = 5.0) -> dict:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    # The controller is local and plain HTTP; no other scheme is ever used.
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not an http(s) URL: {url!r}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class MihomoClient:
    def __init__(self, base_url: str, *, fetch: Fetch = http_get_json) -> None:
        self._base = base_url.rstrip("/")
        self._fetch = fetch

    def connections(self) -> dict:
        return self._fetch(f"{self._base}/connections")

    def current_exit(self, group: str, *, depth: int = 6) -> list[str]:
        """The chain a group currently resolves to, e.g. ``["TG", "TG-OTHER", "node"]``."""
        chain = [group]
        name = group
        for _ in range(depth):
            proxy = self._fetch(f"{self._base}/proxies/{urllib.parse.quote(name, safe='')}")
            chosen = proxy.get("now")
            if not chosen:
                break
            chain.append(str(chosen))
            name = str(chosen)
        return chain
