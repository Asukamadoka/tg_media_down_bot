"""Measuring what each proxy node can actually do (docs/wms/M9.1 §A.2).

A probe walks the nodes one after another: ``PROBE`` is pointed at the node,
a download and an upload go through the ``probe`` listener, and the answer is
stored. A run is capped in bytes (``PROXY_PROBE_MAX_MB``); its traffic is
metered as category ``probe`` through the listener's name.
"""

from __future__ import annotations

import asyncio
import http.client
import logging
import re
import ssl
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .mihomo import NODE_PROVIDER, MihomoClient
from .pricing import parse_price
from .store import TrafficStore

if TYPE_CHECKING:
    from ..config import TrafficConfig

log = logging.getLogger(__name__)

INFO_NODE = re.compile(r"充值|分割线|群 |官网|失联|0[.]10元")
"""Provider entries that are notices, not servers."""
DOWNLOAD_CAP = 8_000_000
DOWNLOAD_SECONDS = 10.0
UPLOAD_BYTES = 2_000_000
PER_NODE_ESTIMATE = DOWNLOAD_CAP + UPLOAD_BYTES


class Net(Protocol):
    """What a probe needs from the network; tests supply a fake."""

    def download(self, url: str, *, cap: int, seconds: float) -> tuple[int, float]:
        """``(bytes read, seconds taken)`` through the probe listener."""

    def upload(self, url: str, *, size: int, seconds: float) -> tuple[int, float]: ...

    def head(self, host: str, *, url: str | None, seconds: float) -> HeadResult: ...


@dataclass
class HeadResult:
    """One reachability check of a host through the listener."""

    reachable: bool = False
    tls_ok: bool = False
    error: str = ""
    latency_ms: int | None = None


class UrllibNet:
    """The real thing: plain ``urllib`` / ``http.client`` through the listener."""

    def __init__(self, listener: str) -> None:
        self._listener = listener

    def _opener(self) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self._listener, "https": self._listener}))

    def download(self, url, *, cap, seconds):
        started = time.monotonic()
        got = 0
        with self._opener().open(url, timeout=seconds) as response:
            while got < cap and time.monotonic() - started < seconds:
                chunk = response.read(min(65536, cap - got))
                if not chunk:
                    break
                got += len(chunk)
        return got, time.monotonic() - started

    def upload(self, url, *, size, seconds):
        started = time.monotonic()
        request = urllib.request.Request(url, data=b"\0" * size, method="POST")
        with self._opener().open(request, timeout=seconds) as response:
            response.read(1024)
        return size, time.monotonic() - started

    def head(self, host, *, url, seconds):
        parts = urllib.parse.urlsplit(self._listener)
        started = time.monotonic()
        try:
            conn = http.client.HTTPSConnection(
                parts.hostname, parts.port, timeout=seconds,
                context=ssl.create_default_context())  # verifies the certificate
            conn.set_tunnel(host)
            target = urllib.parse.urlsplit(url).path or "/" if url else "/"
            conn.request("HEAD", target, headers={"User-Agent": "tgmd-probe"})
            status = conn.getresponse().status
            conn.close()
        except ssl.SSLError as exc:
            return HeadResult(error=f"TLS: {exc.__class__.__name__}")
        except (OSError, http.client.HTTPException) as exc:
            return HeadResult(error=f"{exc.__class__.__name__}")
        elapsed = int((time.monotonic() - started) * 1000)
        return HeadResult(reachable=200 <= status < 500, tls_ok=True, latency_ms=elapsed,
                          error="" if 200 <= status < 500 else f"HTTP {status}")


@dataclass
class NodeResult:
    name: str
    price: float | None
    latency_ms: int | None = None
    down_mbps: float = 0.0
    up_mbps: float = 0.0
    alive: bool = False
    tested_at: float = 0.0


@dataclass
class ProbeRun:
    results: list[NodeResult] = field(default_factory=list)
    spent_bytes: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(node, reason)``: ``price``, ``info``, ``dead`` or ``cap``."""
    started: float = 0.0


def provider_nodes(client: MihomoClient) -> list[dict]:
    """The provider's real nodes (notice entries left out) with an ``alive`` flag."""
    nodes = []
    for proxy in client.provider(NODE_PROVIDER).get("proxies") or []:
        name = str(proxy.get("name") or "")
        if not name or INFO_NODE.search(name):
            continue
        alive = proxy.get("alive")
        if alive is None:
            history = proxy.get("history") or []
            alive = bool(history and history[-1].get("delay"))
        nodes.append({"name": name, "alive": bool(alive)})
    return nodes


def best_node(results: list[NodeResult], *, tolerance: float = 0.15) -> NodeResult | None:
    """The fastest alive node by download; among those within ``tolerance`` of it,
    the cheapest (an unknown price counts as expensive)."""
    alive = [r for r in results if r.alive and r.down_mbps > 0]
    if not alive:
        return None
    top = max(r.down_mbps for r in alive)
    pool = [r for r in alive if r.down_mbps >= top * (1 - tolerance)]
    return min(pool, key=lambda r: (r.price if r.price is not None else 99.0, -r.down_mbps))


class Prober:
    def __init__(self, config: TrafficConfig, client: MihomoClient, net: Net,
                 store: TrafficStore, *, clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._client = client
        self._net = net
        self._store = store
        self._clock = clock

    def run(self) -> ProbeRun:
        """Blocking; callers run it in a thread. Always leaves ``PROBE`` on DIRECT."""
        config = self._config
        run = ProbeRun(started=self._clock())
        cap = int(config.probe_max_mb * 1024 * 1024)
        nodes = provider_nodes(self._client)
        # Cheap nodes first, so a cap that cuts the run short cuts the dear ones.
        nodes.sort(key=lambda n: parse_price(n["name"]) if parse_price(n["name"]) is not None
                   else 99.0)
        try:
            for node in nodes:
                name, price = node["name"], parse_price(node["name"])
                if price is not None and price > config.probe_max_price:
                    run.skipped.append((name, "price"))
                    continue
                if not node["alive"]:
                    run.skipped.append((name, "dead"))
                    self._save(NodeResult(name, price, alive=False, tested_at=self._clock()))
                    continue
                if run.spent_bytes + PER_NODE_ESTIMATE > cap:
                    run.skipped.append((name, "cap"))
                    continue
                result = self._one(name, price, run)
                run.results.append(result)
                self._save(result)
        finally:
            try:
                self._client.select("PROBE", "DIRECT")
            except Exception:
                log.warning("could not put PROBE back on DIRECT", exc_info=True)
        return run

    def _save(self, r: NodeResult) -> None:
        self._store.save_node(r.name, latency_ms=r.latency_ms, down_mbps=r.down_mbps,
                              up_mbps=r.up_mbps, price=r.price, alive=r.alive,
                              tested_at=r.tested_at)

    def _one(self, name: str, price: float | None, run: ProbeRun) -> NodeResult:
        config = self._config
        result = NodeResult(name, price, tested_at=self._clock())
        self._client.select("PROBE", name)
        result.latency_ms = self._client.delay(name)
        if result.latency_ms is None:
            return result  # no answer: dead, and nothing is downloaded through it
        result.alive = True
        try:
            got, seconds = self._net.download(config.probe_url, cap=DOWNLOAD_CAP,
                                              seconds=DOWNLOAD_SECONDS)
            run.spent_bytes += got
            result.down_mbps = got * 8 / 1e6 / max(seconds, 1e-6)
        except Exception as exc:  # noqa: BLE001 - one failed transfer is a result, not a crash
            log.info("probe download through %s failed: %s", name, exc)
        try:
            sent, seconds = self._net.upload(config.probe_up_url, size=UPLOAD_BYTES,
                                             seconds=DOWNLOAD_SECONDS)
            run.spent_bytes += sent
            result.up_mbps = sent * 8 / 1e6 / max(seconds, 1e-6)
        except Exception as exc:  # noqa: BLE001
            log.info("probe upload through %s failed: %s", name, exc)
        return result

    async def run_async(self) -> ProbeRun:
        return await asyncio.to_thread(self.run)
