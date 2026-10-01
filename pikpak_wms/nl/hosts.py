"""Model hosts on the LAN that come and go (docs/wms/M8 §A).

The ``openai`` backend may name several hosts, best first: a Mac on a direct
cable and a Windows PC, neither of them on around the clock. Before a
sentence is sent anywhere, each host is asked ``GET <base>/models`` with a
short timeout. Any HTTP answer means the host is up; no answer means it is
off. Both verdicts are remembered for :data:`TTL` seconds, so an offline host
costs one short timeout a minute, not one per sentence.

One missed answer is not yet "off" (docs/wms/M8.2 §E): a probe that fails is
repeated at once, and a host is written off only after two failures in a row;
a generation that fails is followed by an immediate probe, and the sentence is
tried once more if the host answers it. A single hiccup therefore no longer
costs every request of the next minute.

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

PROBE_ATTEMPTS = 2
"""Probes in a row that must fail before a host counts as offline."""

Get = Callable[[str, dict[str, str], float], Awaitable[int]]
"""(url, headers, timeout) → HTTP status; raises when nothing answers."""


class HostsConfigError(ValueError):
    """``NL_OPENAI_MODEL`` lists a number of models that fits no reading."""


DEFAULT_MAX_TOKENS = 512
"""Longest answer asked of a model. A Query is a few hundred tokens; a model that
does not stop (docs/wms/M8.3 §A) is cut off here instead of at the time limit."""


@dataclass(frozen=True)
class Host:
    base_url: str
    model: str
    name: str = ""
    """What a person calls the machine (``Mac``); the address's host name when unset."""
    max_tokens: int = DEFAULT_MAX_TOKENS

    @property
    def label(self) -> str:
        return self.base_url

    @property
    def display(self) -> str:
        from urllib.parse import urlparse

        return self.name or urlparse(self.base_url).hostname or self.base_url


def _spread(raw: str, count: int, variable: str, noun: str = "values") -> list[str]:
    """One value for every host, or one per host, in order; none at all is empty strings."""
    values = [v.strip() for v in (raw or "").split(",") if v.strip()]
    if len(values) == 1:
        return values * count
    if values and len(values) != count:
        raise HostsConfigError(
            f"{variable} lists {len(values)} {noun} for {count} hosts; "
            "give one for all of them, or one per host"
        )
    return values or [""] * count


def parse_hosts(base_urls: str, models: str, names: str = "", max_tokens: str = "") -> list[Host]:
    """``NL_OPENAI_BASE_URL``, ``NL_OPENAI_MODEL``, ``NL_OPENAI_NAMES`` and
    ``NL_OPENAI_MAX_TOKENS``, comma-separated.

    One model (name, limit) serves every host; otherwise there is one per host,
    in the same order.
    """
    urls = [url.strip().rstrip("/") for url in (base_urls or "").split(",") if url.strip()]
    if not urls:
        return []
    model_list = _spread(models, len(urls), "NL_OPENAI_MODEL", "models")
    name_list = _spread(names, len(urls), "NL_OPENAI_NAMES")
    limits = _spread(max_tokens, len(urls), "NL_OPENAI_MAX_TOKENS")
    try:
        counts = [int(v) if v else DEFAULT_MAX_TOKENS for v in limits]
    except ValueError as exc:
        raise HostsConfigError(f"NL_OPENAI_MAX_TOKENS is not a number: {max_tokens!r}") from exc
    return [Host(url, model, name, max(count, 16))
            for url, model, name, count in zip(urls, model_list, name_list, counts, strict=True)]


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

    async def online(self, host: Host, headers: dict[str, str], get: Get | None = None, *,
                     force: bool = False) -> bool:
        """Whether ``host`` answers, asking it at most once per :data:`TTL`.

        Two failed probes in a row make it offline. ``force`` asks again now
        whatever was remembered, and counts one failed probe as enough: the
        caller has just seen a generation fail, which is the first failure.
        """
        state = self.state(host)
        if not force and self.fresh(host):
            return bool(state.online)
        why = ""
        for _attempt in range(1 if force else PROBE_ATTEMPTS):
            try:
                await (get or _http_get)(f"{host.base_url}/models", headers, PROBE_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - no answer at all is the point
                why = f"{type(exc).__name__}: {exc}"[:120]
                continue
            state.online, state.checked_at, state.error = True, self.clock(), ""
            return True
        self.offline(host, why)
        return False

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
