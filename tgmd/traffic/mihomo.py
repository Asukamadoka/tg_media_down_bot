"""The bot's only way to talk to mihomo's controller API.

Reads are free. Writes are a strict whitelist (docs/wms/M9.1 §A.4) and nothing
else: no ``PUT /configs``, no restarts, no group other than the four the bot
owns, no provider other than ``direct-auto``. Every write goes through
:meth:`MihomoClient._write`, which refuses anything not on the list *before*
a request is made.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

Fetch = Callable[[str], dict]
Send = Callable[[str, str, dict | None], dict | None]

SELECT_GROUPS = ("FAST", "TG-PICK", "PROXY", "PROBE")
"""The groups the bot may switch; ``FAST`` and ``TG-PICK`` are its own."""
RULE_PROVIDER = "direct-auto"
NODE_PROVIDER = "main"
TG_GROUP = "TG"

_ALLOWED_WRITES = (
    ("PUT", re.compile(r"^/proxies/(" + "|".join(SELECT_GROUPS) + r")$")),
    ("DELETE", re.compile(r"^/connections/[A-Za-z0-9-]+$")),
    ("PUT", re.compile(r"^/providers/rules/" + RULE_PROVIDER + r"$")),
)


class ForbiddenWrite(PermissionError):
    """A write to mihomo that is not on the whitelist."""


def http_get_json(url: str, timeout: float = 5.0) -> dict:
    # The controller is local and plain HTTP; no other scheme is ever used.
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not an http(s) URL: {url!r}")
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_send(method: str, url: str, body: dict | None, timeout: float = 10.0) -> dict | None:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not an http(s) URL: {url!r}")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw.strip() else None


class MihomoClient:
    def __init__(self, base_url: str, *, fetch: Fetch = http_get_json,
                 send: Send | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._fetch = fetch
        self._send = send or (lambda method, url, body: http_send(method, url, body))

    # ------------------------------------------------------------------ reads

    def connections(self) -> dict:
        return self._fetch(f"{self._base}/connections")

    def proxy(self, name: str) -> dict:
        return self._fetch(f"{self._base}/proxies/{urllib.parse.quote(name, safe='')}")

    def current_exit(self, group: str, *, depth: int = 6) -> list[str]:
        """The chain a group currently resolves to, e.g. ``["TG", "TG-OTHER", "node"]``."""
        chain = [group]
        name = group
        for _ in range(depth):
            chosen = self.proxy(name).get("now")
            if not chosen:
                break
            chain.append(str(chosen))
            name = str(chosen)
        return chain

    def now(self, group: str) -> str:
        return str(self.proxy(group).get("now") or "")

    def provider(self, name: str = NODE_PROVIDER) -> dict:
        return self._fetch(
            f"{self._base}/providers/proxies/{urllib.parse.quote(name, safe='')}")

    def delay(self, node: str, *, url: str = "https://www.gstatic.com/generate_204",
              timeout_ms: int = 5000) -> int | None:
        """Latency in ms through ``node``, or None when it did not answer."""
        query = urllib.parse.urlencode({"url": url, "timeout": timeout_ms})
        path = f"{self._base}/proxies/{urllib.parse.quote(node, safe='')}/delay?{query}"
        try:
            value = self._fetch(path).get("delay")
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return int(value) if value else None

    # ----------------------------------------------------------------- writes

    def _write(self, method: str, path: str, body: dict | None = None) -> dict | None:
        if not any(method == verb and rule.match(path) for verb, rule in _ALLOWED_WRITES):
            raise ForbiddenWrite(f"{method} {path} is not an allowed mihomo write")
        return self._send(method, f"{self._base}{path}", body)

    def select(self, group: str, node: str) -> None:
        """Make ``group`` (one of :data:`SELECT_GROUPS`) use ``node``."""
        if group not in SELECT_GROUPS:
            raise ForbiddenWrite(f"{group!r} is not a group the bot may switch")
        self._write("PUT", f"/proxies/{group}", {"name": node})

    def close_connection(self, conn_id: str) -> None:
        self._write("DELETE", f"/connections/{conn_id}")

    def reload_rules(self) -> None:
        """Re-read the ``direct-auto`` rule provider's file."""
        self._write("PUT", f"/providers/rules/{RULE_PROVIDER}")

    def close_telegram_connections(self) -> int:
        """Close every live connection whose chain goes through ``TG``, so
        Telethon reconnects through one exit only (AuthKeyDuplicatedError guard)."""
        closed = 0
        for connection in self.connections().get("connections") or []:
            chains = [str(name) for name in connection.get("chains") or []]
            if TG_GROUP in chains and connection.get("id"):
                try:
                    self.close_connection(str(connection["id"]))
                    closed += 1
                except (urllib.error.URLError, OSError):
                    continue  # it may have ended by itself in the meantime
        return closed
