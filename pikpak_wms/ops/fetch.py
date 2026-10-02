"""Fetching one big file over several HTTP Range connections (docs/wms/M8.3 §G7).

A single connection to PikPak's CDN gave 0.2-1.1 MiB/s on the NAS; the link
is limited per connection, so the file is split into ``connections`` ranges
fetched at once, each written at its own offset of one ``.part`` file.

* **Resumable.** A sidecar ``<name>.part.state`` records how far each range
  got. A later run carries on from there. A ``.part`` without a sidecar (a
  file the old single-stream code left, or one whose sidecar was lost while
  the file was shorter than it should be) counts as a finished prefix and the
  rest is split into ranges. A part as long as the whole file with no sidecar
  cannot be trusted and starts over.
* **Links expire.** A 403/410 mid-way fetches a fresh link and carries on
  from where that range stopped (at most :data:`MAX_RELINKS` times per file).
* **No ranges, no problem.** A server that ignores Range gets one plain
  stream, from the start.
* **A shared budget.** Every connection of every file being fetched at once
  takes a slot from one :class:`ConnectionPool` (docs/wms/M9.2 §D.2), so many
  files cannot open hundreds of connections and trigger the CDN's 503s.
* **Trouble is not fatal.** With a retry window, HTTP 429/5xx and connection
  errors back off (5 s doubling to 2 min), ask for fresh links after two
  failed attempts, and keep going until the window has passed with no progress
  at all (docs/wms/M9.2 §D.3).

The network sits behind :class:`RangeIO`, so tests drive it with a fake.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

CHUNK = 1024 * 1024
MIN_SEGMENT = 4 * 1024 * 1024
SEGMENT = 64 * 1024 * 1024
"""A big file is cut into ranges of about this size, so ranges still waiting can
be given to the faster of two links."""
MAX_CONNECTIONS_HARD = 32
RAMP_STEP = 4
RAMP_GAIN = 1.15
IDENTITY_BYTES = 256 * 1024
"""How much of the start of a file is compared between the web and origin links."""
"""A range smaller than this is not worth a connection of its own."""
MAX_RELINKS = 3
RETRIES = 3
BACKOFF_FIRST = 5.0
BACKOFF_MAX = 120.0
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
SAVE_EVERY = 8 * 1024 * 1024
"""How much progress passes between writes of the sidecar."""

Progress = Callable[[int, int], None]
"""(bytes of the file in place, bytes in the whole file)."""

UrlFor = Callable[[], Awaitable["str | Links"]]

throttle: Callable[[int], Awaitable[None]] | None = None
"""A hook the host application may set to hold downloads back, called with the
size of each chunk before it is written. The bot sets it to wait while its
direct-traffic cap is reached (TRAFFIC_DIRECT_DAILY_GB, docs/wms/M9 §D.1)."""


class ConnectionPool:
    """Connection slots shared by every file in flight (first come, first served).

    A connection takes a slot for as long as one stream is open and gives it
    back between ranges and while backing off, so files beyond the budget wait
    for connections, not for each other."""

    def __init__(self, capacity: int = 32) -> None:
        self.capacity = capacity
        self.used = 0
        self._waiters: deque[asyncio.Future] = deque()

    async def acquire(self) -> None:
        if self.used < self.capacity and not self._waiters:
            self.used += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                self.release()  # granted just as it was cancelled: hand the slot on
            else:
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)
            raise

    def release(self) -> None:
        self.used = max(self.used - 1, 0)
        self._wake()

    def _wake(self) -> None:
        while self._waiters and self.used < self.capacity:
            waiter = self._waiters.popleft()
            if not waiter.done():
                self.used += 1
                waiter.set_result(None)

    def configure(self, capacity: int) -> None:
        self.capacity = max(int(capacity), 1)
        self._wake()


POOL = ConnectionPool()
"""The budget every download shares unless it is given its own (the bot sets the
capacity from ``OUTBOUND_MAX_TOTAL_CONNECTIONS``)."""


class LinkExpired(Exception):
    """The direct link was refused (HTTP 403 / 410): ask for a new one."""


class RangeIO(Protocol):
    async def probe(self, url: str) -> int | None:
        """Size of the file if the server serves ranges, else None."""

    def stream(self, url: str, start: int, end: int | None) -> AsyncIterator[bytes]:
        """Bytes ``[start, end)`` (to the end of the file when ``end`` is None)."""


class AiohttpIO:  # pragma: no cover - real network
    def __init__(self, *, sock_read: float = 120.0) -> None:
        self.sock_read = sock_read

    def _timeout(self):
        import aiohttp

        return aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=self.sock_read)

    @staticmethod
    def _check(status: int) -> None:
        if status in (403, 410):
            raise LinkExpired(f"HTTP {status}")

    async def probe(self, url: str) -> int | None:
        import aiohttp

        async with aiohttp.ClientSession(timeout=self._timeout()) as session, session.get(
            url, headers={"Range": "bytes=0-0"}
        ) as response:
            self._check(response.status)
            if response.status == 206:
                found = re.search(r"/(\d+)\s*$", response.headers.get("Content-Range", ""))
                return int(found.group(1)) if found else None
            response.raise_for_status()
            return None

    async def stream(self, url: str, start: int, end: int | None) -> AsyncIterator[bytes]:
        import aiohttp

        headers = {"Range": f"bytes={start}-{'' if end is None else end - 1}"}
        async with aiohttp.ClientSession(timeout=self._timeout()) as session, session.get(
            url, headers=headers
        ) as response:
            self._check(response.status)
            response.raise_for_status()
            async for chunk in response.content.iter_chunked(CHUNK):
                yield chunk


@dataclass
class _Range:
    start: int
    end: int
    done: int = 0
    busy: bool = False

    @property
    def left(self) -> int:
        return self.end - self.start - self.done


def _split(start: int, end: int, parts: int) -> list[_Range]:
    length = end - start
    if length <= 0:
        return []
    parts = max(1, min(parts, length))
    step, extra = divmod(length, parts)
    out, cursor = [], start
    for i in range(parts):
        size = step + (1 if i < extra else 0)
        out.append(_Range(cursor, cursor + size))
        cursor += size
    return out


def state_path(part: Path) -> Path:
    return part.with_name(part.name + ".state")


def _range_count(total: int, connections: int) -> int:
    """How many ranges: at least one per connection, more for a big file so that
    ranges still waiting can be handed to the faster link (M9.1 §C.2)."""
    wanted = max(connections, -(-total // SEGMENT))
    return max(1, min(wanted, -(-total // MIN_SEGMENT)))


def _load_ranges(part: Path, total: int, connections: int) -> list[_Range]:
    side = state_path(part)
    if side.exists():
        try:
            raw = json.loads(side.read_text())
            if raw.get("size") == total:
                ranges = [_Range(int(a), int(b), int(c)) for a, b, c in raw["ranges"]]
                if all(0 <= r.done <= r.end - r.start for r in ranges):
                    return ranges
        except (ValueError, KeyError, TypeError, OSError):
            pass
        side.unlink(missing_ok=True)
    have = part.stat().st_size if part.exists() else 0
    parts = _range_count(total, connections)
    if 0 < have < total:
        # A prefix from an earlier single-stream run: keep it, split the rest.
        return [_Range(0, have, have), *_split(have, total, parts)]
    return _split(0, total, parts)


def _save_ranges(part: Path, total: int, ranges: list[_Range]) -> None:
    payload = {"size": total, "ranges": [[r.start, r.end, r.done] for r in ranges]}
    side = state_path(part)
    tmp = side.with_name(side.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(side)


@dataclass
class Links:
    """What ``url_for`` may return instead of one URL: the web link and the
    origin media link PikPak gave for the same file."""

    web: str
    origin: str | None = None


@dataclass
class FetchStats:
    """How a download went; filled in by :func:`download`."""

    connections: int = 0
    """Connections open now."""
    peak_connections: int = 0
    links: str = "web"
    """``web`` or ``web+origin``: the links that actually carried bytes' sources."""
    hosts: list[str] = field(default_factory=list)
    seconds: float = 0.0
    bytes: int = 0
    origin_note: str = ""
    """Why the origin link was not used, when it was offered and refused."""

    @property
    def avg_mib_s(self) -> float:
        return self.bytes / self.seconds / (1024 * 1024) if self.seconds > 0 else 0.0

    def as_dict(self) -> dict:
        return {"avg_mib_s": round(self.avg_mib_s, 2), "links": self.links,
                "peak_connections": self.peak_connections, "hosts": list(self.hosts)}


def _as_links(got) -> Links:
    return got if isinstance(got, Links) else Links(str(got))


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or "?"


async def _first_block(io: RangeIO, url: str) -> bytes:
    data = bytearray()
    async for chunk in io.stream(url, 0, IDENTITY_BYTES):
        data += chunk
        if len(data) >= IDENTITY_BYTES:
            break
    return bytes(data[:IDENTITY_BYTES])


async def usable_links(io: RangeIO, links: Links) -> tuple[list[str], str]:
    """The URLs to download from, and why the origin was left out (or "").

    The origin link is used only when it reports the same total size and the
    same SHA-256 over the first 256 KiB as the web link (M9.1 §C.1).
    """
    urls = [links.web]
    if not links.origin:
        return urls, ""
    try:
        web_size = await io.probe(links.web)
        origin_size = await io.probe(links.origin)
        if web_size is None or origin_size != web_size:
            return urls, f"origin size {origin_size} differs from {web_size}"
        a = hashlib.sha256(await _first_block(io, links.web)).digest()
        b = hashlib.sha256(await _first_block(io, links.origin)).digest()
        if a != b:
            return urls, "origin content differs from the web link"
    except LinkExpired:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken origin only means we use the web link
        return urls, f"origin could not be checked: {type(exc).__name__}"
    return [links.web, links.origin], ""


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


def _transient(exc: BaseException) -> bool:
    """Trouble that passes: the CDN saying 429/5xx, or the network failing to carry
    the stream. A 404 and the like are not retried for half an hour."""
    status = _status_of(exc)
    if status is not None:
        return status in TRANSIENT_STATUS
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    return type(exc).__module__.split(".")[0] == "aiohttp"


async def download(
    url_for: UrlFor, io: RangeIO, part: Path, *, connections: int = 8,
    progress: Progress | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_connections: int | None = None, stats: FetchStats | None = None,
    clock: Callable[[], float] = time.monotonic, rebalance_after: float = 20.0,
    adapt_every: float = 20.0, tick: float = 1.0, pool: ConnectionPool | None = None,
    retry_seconds: float = 0.0,
) -> int:
    """Fetch the file behind ``url_for()`` into ``part``; returns its size.

    ``url_for`` returns one URL, or :class:`Links` with an origin media link as
    well. Connections start at ``connections`` and ramp up by 4 while the total
    speed keeps rising, to ``max_connections``; they drop by 4 on HTTP 429/503
    or repeated resets. With two links, ranges not yet started go to the faster
    one. Cancelling (or failing) leaves ``part`` and its sidecar for the next run.

    ``retry_seconds`` > 0 turns on the patient mode of docs/wms/M9.2 §D.3: CDN and
    network trouble is retried with a growing pause until that long has passed
    with no byte arriving; 0 keeps the short retry (:data:`RETRIES`).
    """
    stats = stats if stats is not None else FetchStats()
    pool = pool if pool is not None else POOL
    ceiling = min(MAX_CONNECTIONS_HARD, max(max_connections or connections, connections))
    state: dict = {"urls": [], "generation": 0, "relinks": 0}
    relink_lock = asyncio.Lock()

    async def refresh() -> None:
        links = _as_links(await url_for())
        state["urls"], stats.origin_note = await usable_links(io, links)
        stats.links = "web+origin" if len(state["urls"]) == 2 else "web"
        stats.hosts = [_host(u) for u in state["urls"]]

    async def relink(seen: int, *, expired: bool = True) -> None:
        async with relink_lock:
            if state["generation"] != seen:
                return  # another range already got fresh links
            if expired:
                if state["relinks"] >= MAX_RELINKS:
                    raise LinkExpired("the link kept expiring")
                state["relinks"] += 1
            await refresh()  # both links are asked for again
            state["generation"] += 1

    await refresh()
    while True:
        try:
            total = await io.probe(state["urls"][0])
            break
        except LinkExpired:
            await relink(state["generation"])

    part.parent.mkdir(parents=True, exist_ok=True)
    if total is None:
        stats.links = "web"
        return await _plain(lambda: _first_url(url_for), io, part, progress)

    ranges = _load_ranges(part, total, connections)
    mode = "r+b" if part.exists() else "w+b"
    started = clock()
    with part.open(mode) as handle:
        handle.truncate(total)
        fd = handle.fileno()
        start_done = sum(r.done for r in ranges)
        saved = {"at": start_done}
        live = {"target": min(connections, ceiling), "active": 0}
        link_bytes = [0, 0]
        link_conns = [0, 0]
        link_seconds = [0.0, 0.0]
        flags = {"resets": 0, "blocked": False}

        def advance() -> None:
            got = sum(r.done for r in ranges)
            stats.bytes = got - start_done
            if progress is not None:
                progress(got, total)
            if got - saved["at"] >= SAVE_EVERY:
                saved["at"] = got
                _save_ranges(part, total, ranges)

        def pick_link() -> int:
            n = len(state["urls"])
            if n < 2:
                return 0
            if clock() - started < rebalance_after or min(link_seconds) <= 0:
                weights = [1.0, 1.0]
            else:
                weights = [max(link_bytes[i] / link_seconds[i], 1.0) for i in range(2)]
            return min(range(2), key=lambda i: (link_conns[i] + 1) / weights[i])

        def next_range() -> _Range | None:
            return next((r for r in ranges if r.left > 0 and not r.busy), None)

        trouble: dict = {"since": None}
        """When the file last stopped making progress (patient mode), else None."""

        async def run(span: _Range) -> None:
            failures = streak = 0
            while span.left > 0:
                seen = state["generation"]
                await pool.acquire()
                index = pick_link()
                urls = state["urls"]
                url = urls[index % len(urls)]
                link = index % len(urls)
                link_conns[link] += 1
                stats.connections = sum(link_conns)
                try:
                    async for chunk in io.stream(url, span.start + span.done, span.end):
                        chunk = chunk[: span.left]
                        if not chunk:
                            break
                        if throttle is not None:
                            await throttle(len(chunk))
                        await asyncio.to_thread(os.pwrite, fd, chunk, span.start + span.done)
                        span.done += len(chunk)
                        link_bytes[link] += len(chunk)
                        trouble["since"] = None
                        streak = 0
                        advance()
                    if span.left > 0:
                        raise OSError("the stream ended early")
                except LinkExpired:
                    await relink(seen)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    failures += 1
                    status = _status_of(exc)
                    if status in (429, 503) or isinstance(exc, ConnectionResetError):
                        flags["resets"] += 1
                        if status in (429, 503) or flags["resets"] >= 2:
                            flags["resets"] = 0
                            flags["blocked"] = True
                            live["target"] = max(1, live["target"] - RAMP_STEP)
                    pause: float
                    if retry_seconds > 0 and _transient(exc):
                        now = clock()
                        if trouble["since"] is None:
                            trouble["since"] = now
                        elif now - trouble["since"] >= retry_seconds:
                            raise
                        streak += 1
                        pause = min(BACKOFF_MAX, BACKOFF_FIRST * 2 ** (streak - 1))
                        if streak % 2 == 0:
                            # Two attempts failed: the link itself may be the trouble.
                            with contextlib.suppress(Exception):
                                await relink(seen, expired=False)
                    else:
                        if failures > RETRIES:
                            raise
                        pause = min(failures, 3)
                    link_conns[link] -= 1
                    stats.connections = sum(link_conns)
                    pool.release()
                    link = -1  # given back before the pause, not after it
                    await sleep(pause)
                finally:
                    if link >= 0:
                        link_conns[link] -= 1
                        stats.connections = sum(link_conns)
                        pool.release()

        async def worker() -> None:
            live["active"] += 1
            stats.peak_connections = max(stats.peak_connections, live["active"])
            try:
                while live["active"] <= live["target"]:
                    span = next_range()
                    if span is None:
                        return
                    span.busy = True
                    try:
                        await run(span)
                    finally:
                        span.busy = False
            finally:
                live["active"] -= 1

        workers: list[asyncio.Task] = []

        def spawn() -> None:
            workers.append(asyncio.create_task(worker()))

        async def monitor() -> None:
            last = clock()
            window_at, window_bytes, previous = last, sum(link_bytes), None
            idle = asyncio.Event()  # never set: a timer that does not depend on asyncio.sleep
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(idle.wait(), tick)
                now = clock()
                for i in range(2):
                    link_seconds[i] += link_conns[i] * (now - last)
                last = now
                if now - window_at < adapt_every:
                    continue
                speed = (sum(link_bytes) - window_bytes) / (now - window_at)
                rose = previous is None or speed > previous * RAMP_GAIN
                window_at, window_bytes, previous = now, sum(link_bytes), speed
                if flags["blocked"]:
                    flags["blocked"] = False
                    continue
                if rose and live["target"] < ceiling and next_range() is not None:
                    live["target"] = min(ceiling, live["target"] + RAMP_STEP)
                running = sum(1 for t in workers if not t.done())
                while running < live["target"] and next_range() is not None:
                    spawn()
                    running += 1

        advance()
        for _ in range(live["target"]):
            if next_range() is None:
                break
            spawn()
        watcher = asyncio.create_task(monitor())
        try:
            while True:
                running = [t for t in workers if not t.done()]
                for task in workers:
                    if task.done() and not task.cancelled() and task.exception() is not None:
                        raise task.exception()  # type: ignore[misc]
                if not running:
                    if next_range() is None:
                        break
                    spawn()  # every worker retired while work is left
                    continue
                await asyncio.wait(running, timeout=tick, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            for task in [watcher, *workers]:
                task.cancel()
            await asyncio.gather(watcher, *workers, return_exceptions=True)
            with contextlib.suppress(OSError):
                handle.flush()
                _save_ranges(part, total, ranges)
            raise
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    stats.seconds = clock() - started
    state_path(part).unlink(missing_ok=True)
    return total


async def bench(io: RangeIO, url: str, total: int, *, connections: int, seconds: float,
                clock: Callable[[], float] = time.monotonic) -> float:
    """MiB/s over ``connections`` streams read for ``seconds``, each from its own
    stretch of the file, nothing written to disk (``wms bench-fetch``)."""
    got = [0]
    deadline = clock() + seconds
    begin = clock()

    async def one(index: int) -> None:
        start = index * (total // connections)
        end = total if index == connections - 1 else start + total // connections
        async for chunk in io.stream(url, start, end):
            got[0] += len(chunk)
            if clock() >= deadline:
                return

    tasks = [asyncio.create_task(one(i)) for i in range(connections)]
    try:
        await asyncio.wait(tasks, timeout=seconds + 5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return got[0] / max(clock() - begin, 1e-6) / (1024 * 1024)


async def _first_url(url_for: UrlFor) -> str:
    return _as_links(await url_for()).web


async def _plain(url_for: UrlFor, io: RangeIO, part: Path, progress: Progress | None) -> int:
    """One stream from the start, for a server without ranges."""
    written = 0
    with part.open("wb") as handle:
        async for chunk in io.stream(await url_for(), 0, None):
            if throttle is not None:
                await throttle(len(chunk))
            handle.write(chunk)
            written += len(chunk)
            if progress is not None:
                progress(written, 0)
    return written


# ------------------------------------------------------------- verification


def gcid(path: Path, size: int | None = None) -> str:
    """PikPak's content id: SHA-1 over the SHA-1s of the file's blocks, where
    the block size doubles from 256 KiB until there are at most 512 of them
    (capped at 2 MiB). Upper-case hex."""
    import hashlib

    size = size if size is not None else path.stat().st_size
    block = 0x40000
    while size / block > 0x200 and block < 0x200000:
        block <<= 1
    outer = hashlib.sha1()
    with path.open("rb") as handle:
        while chunk := handle.read(block):
            outer.update(hashlib.sha1(chunk).digest())
    return outer.hexdigest().upper()
