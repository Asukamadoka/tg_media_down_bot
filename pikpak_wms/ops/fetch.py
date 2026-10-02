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

The network sits behind :class:`RangeIO`, so tests drive it with a fake.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

CHUNK = 1024 * 1024
MIN_SEGMENT = 4 * 1024 * 1024
"""A range smaller than this is not worth a connection of its own."""
MAX_RELINKS = 3
RETRIES = 3
SAVE_EVERY = 8 * 1024 * 1024
"""How much progress passes between writes of the sidecar."""

Progress = Callable[[int, int], None]
"""(bytes of the file in place, bytes in the whole file)."""

UrlFor = Callable[[], Awaitable[str]]

throttle: Callable[[int], Awaitable[None]] | None = None
"""A hook the host application may set to hold downloads back, called with the
size of each chunk before it is written. The bot sets it to wait while its
direct-traffic cap is reached (TRAFFIC_DIRECT_DAILY_GB, docs/wms/M9 §D.1)."""


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
    parts = max(1, min(connections, -(-total // MIN_SEGMENT)))
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


async def download(
    url_for: UrlFor, io: RangeIO, part: Path, *, connections: int = 8,
    progress: Progress | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Fetch the file behind ``url_for()`` into ``part``; returns its size.

    Cancelling (or failing) leaves ``part`` and its sidecar for the next run.
    """
    state = {"url": await url_for(), "generation": 0, "relinks": 0}
    relink_lock = asyncio.Lock()

    async def relink(seen: int) -> None:
        async with relink_lock:
            if state["generation"] != seen:
                return  # another range already got a fresh link
            if state["relinks"] >= MAX_RELINKS:
                raise LinkExpired("the link kept expiring")
            state["relinks"] += 1
            state["url"] = await url_for()
            state["generation"] += 1

    while True:
        try:
            total = await io.probe(state["url"])
            break
        except LinkExpired:
            await relink(state["generation"])

    part.parent.mkdir(parents=True, exist_ok=True)
    if total is None:
        return await _plain(url_for, io, part, progress)

    ranges = _load_ranges(part, total, connections)
    mode = "r+b" if part.exists() else "w+b"
    with part.open(mode) as handle:
        handle.truncate(total)
        fd = handle.fileno()
        saved = {"at": sum(r.done for r in ranges)}

        def advance() -> None:
            got = sum(r.done for r in ranges)
            if progress is not None:
                progress(got, total)
            if got - saved["at"] >= SAVE_EVERY:
                saved["at"] = got
                _save_ranges(part, total, ranges)

        async def run(span: _Range) -> None:
            failures = 0
            while span.left > 0:
                seen = state["generation"]
                try:
                    async for chunk in io.stream(state["url"], span.start + span.done, span.end):
                        chunk = chunk[: span.left]
                        if not chunk:
                            break
                        if throttle is not None:
                            await throttle(len(chunk))
                        await asyncio.to_thread(os.pwrite, fd, chunk, span.start + span.done)
                        span.done += len(chunk)
                        advance()
                    if span.left > 0:
                        raise OSError("the stream ended early")
                except LinkExpired:
                    await relink(seen)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception:
                    failures += 1
                    if failures > RETRIES:
                        raise
                    await sleep(min(failures, 3))

        advance()
        tasks = [asyncio.create_task(run(r)) for r in ranges if r.left > 0]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(OSError):
                handle.flush()
                _save_ranges(part, total, ranges)
            raise
    state_path(part).unlink(missing_ok=True)
    return total


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
