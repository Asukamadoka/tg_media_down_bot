"""Model hosts on the LAN that come and go (docs/wms/M8 §A).

The ``openai`` backend may name several hosts, best first: a Mac on a direct
cable and a Windows PC, neither of them on around the clock. Before a
sentence is sent anywhere, each host is asked ``GET <base>/models`` with a
short timeout. Any HTTP answer means the host is up; no answer means it is
off. Both verdicts are remembered for :data:`TTL` seconds, so an offline host
costs one short timeout a minute, not one per sentence.

The verdicts live in :data:`BOARD`, one per process: the bot builds a new
translator for every sentence, and the memory has to outlast it. ``/verify``
and ``wms doctor`` read the same board.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

TTL = 60.0
"""How long an online or offline verdict stands."""

PROBE_TIMEOUT = 1.5
"""For ``GET /models``, and for connecting before a translation."""

Get = Callable[[str, dict[str, str], float], Awaitable[int]]
"""(url, headers, timeout) → HTTP status; raises when nothing answers."""


class HostsConfigError(ValueError):
    """``NL_OPENAI_MODEL`` lists a number of models that fits no reading."""


@dataclass(frozen=True)
class Host:
    base_url: str
    model: str

    @property
    def label(self) -> str:
        return self.base_url


def parse_hosts(base_urls: str, models: str) -> list[Host]:
    """``NL_OPENAI_BASE_URL`` and ``NL_OPENAI_MODEL``, comma-separated.

    One model serves every host; otherwise there is one model per host, in
    the same order.
    """
    urls = [url.strip().rstrip("/") for url in (base_urls or "").split(",") if url.strip()]
    names = [name.strip() for name in (models or "").split(",") if name.strip()]
    if not urls:
        return []
    if len(names) == 1:
        names = names * len(urls)
    elif names and len(names) != len(urls):
        raise HostsConfigError(
            f"NL_OPENAI_MODEL lists {len(names)} models for {len(urls)} hosts; "
            "give one for all of them, or one per host"
        )
    elif not names:
        names = [""] * len(urls)
    return [Host(url, name) for url, name in zip(urls, names, strict=True)]


@dataclass
class HostState:
    online: bool | None = None
    """None until the host has been asked."""
    checked_at: float = 0.0
    latency_ms: float | None = None
    """How long the host took to answer the last translation."""
    error: str = ""


async def _http_get(url: str, headers: dict[str, str], timeout: float) -> int:  # pragma: no cover
    import aiohttp

    limit = aiohttp.ClientTimeout(total=timeout, connect=timeout)
    async with aiohttp.ClientSession(timeout=limit) as session, session.get(
        url, headers=headers
    ) as response:
        return response.status


class HostBoard:
    """What is known about each host, shared by every translator."""

    def __init__(self, *, ttl: float = TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.clock = clock
        self.states: dict[str, HostState] = {}

    def state(self, host: Host) -> HostState:
        return self.states.setdefault(host.base_url, HostState())

    def fresh(self, host: Host) -> bool:
        state = self.state(host)
        return state.online is not None and self.clock() - state.checked_at < self.ttl

    async def online(self, host: Host, headers: dict[str, str], get: Get | None = None) -> bool:
        """Whether ``host`` answers, asking it at most once per :data:`TTL`."""
        state = self.state(host)
        if self.fresh(host):
            return bool(state.online)
        try:
            await (get or _http_get)(f"{host.base_url}/models", headers, PROBE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - no answer at all is the point
            self.offline(host, f"{type(exc).__name__}: {exc}"[:120])
            return False
        state.online, state.checked_at, state.error = True, self.clock(), ""
        return True

    def offline(self, host: Host, why: str) -> None:
        state = self.state(host)
        if state.online is not False:
            log.info("model host %s is offline: %s", host.label, why)
        state.online, state.checked_at, state.error = False, self.clock(), why

    def answered(self, host: Host, seconds: float) -> None:
        state = self.state(host)
        state.online, state.checked_at = True, self.clock()
        state.latency_ms = round(seconds * 1000, 1)

    def reset(self) -> None:
        self.states.clear()


BOARD = HostBoard()
