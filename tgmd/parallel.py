"""Parallel, multi-connection download of one Telegram file.

Telethon downloads a file over a single connection, one part after another,
and that connection's round-trip time caps the speed. For restricted content
a download is the only way to get the bytes at all, so this module fetches
different parts over several connections at once.

It is written from the MTProto documentation for ``upload.getFile``, not
adapted from any existing implementation. Two layers:

* :func:`download_parts` is the scheduler. It knows nothing about Telethon:
  it hands out part offsets to a set of :class:`PartSource` objects, writes
  what comes back at the right offset, and deals with flood waits, expired
  file references and dropped connections. This is where the logic lives,
  and it is tested against fakes.
* :func:`telethon_sources` opens the connections. Each is a separate
  ``MTProtoSender`` on the authorisation key Telethon already holds for the
  file's data centre, so no new login or key exchange happens per connection.

Anything this cannot handle raises :class:`ParallelUnavailable`, and the
caller falls back to Telethon's own single-connection download: a file whose
data centre moves mid-transfer (``FILE_MIGRATE``), a CDN redirect, or
connections that will not open. Falling back is always correct, only slower.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from telethon.errors import (
    FileMigrateError,
    FileReferenceExpiredError,
    FileReferenceInvalidError,
    FloodWaitError,
)
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InitConnectionRequest, InvokeWithLayerRequest
from telethon.tl.functions.help import GetNearestDcRequest
from telethon.tl.functions.upload import GetFileRequest
from telethon.tl.types import InputDocumentFileLocation
from telethon.tl.types.upload import FileCdnRedirect

log = logging.getLogger(__name__)

# upload.getFile accepts a limit up to 1 MiB, and requires that a part never
# cross a 1 MiB boundary. Parts of exactly 1 MiB at multiples of 1 MiB meet
# both rules with the fewest requests.
PART_SIZE = 1024 * 1024

# Below this, opening extra connections costs more than it saves.
MIN_PARALLEL_SIZE = 10 * 1024 * 1024

MAX_CONNECTIONS = 8

# A dropped connection is retried this many times per part before giving up
# on parallel mode for the file.
_PART_ATTEMPTS = 3

# How many times one file's reference may be refreshed. Twice is plenty; a
# reference that keeps expiring means something else is wrong.
_MAX_REFRESHES = 2


class ParallelUnavailable(Exception):
    """This file cannot be fetched in parallel; use the ordinary download."""


class PartSource(Protocol):
    """One connection that can fetch a byte range of a file."""

    async def get(self, location, offset: int, limit: int) -> bytes: ...


Refresh = Callable[[], Awaitable[object]]
Progress = Callable[[int, int], Awaitable[None] | None]


def document_location(document) -> InputDocumentFileLocation:
    """The ``upload.getFile`` location for a document's full content."""
    return InputDocumentFileLocation(
        id=document.id,
        access_hash=document.access_hash,
        file_reference=document.file_reference,
        thumb_size="",
    )


@dataclass
class _Shared:
    """State every worker of one download sees."""

    location: object
    generation: int = 0
    refreshes: int = 0
    received: int = 0


async def download_parts(
    sources: list[PartSource],
    *,
    location,
    size: int,
    path: Path,
    part_size: int = PART_SIZE,
    progress: Progress | None = None,
    cancel: asyncio.Event | None = None,
    refresh: Refresh | None = None,
    flood_ceiling: int = 300,
    on_flood_wait: Callable[[int], None] | None = None,
) -> None:
    """Fetch ``size`` bytes into ``path``, spreading parts over ``sources``.

    Raises :class:`ParallelUnavailable` when the caller should fall back,
    ``DownloadCancelled`` when ``cancel`` is set, and ``DownloadError`` for a
    flood wait longer than ``flood_ceiling``. The file is left as it is on
    failure; removing it is the caller's job, as for any other download.
    """
    # Imported here: downloader imports this module for the fallback path.
    from .downloader import DownloadCancelled, DownloadError

    if not sources:
        raise ParallelUnavailable("no connections")

    offsets: asyncio.Queue[int] = asyncio.Queue()
    for offset in range(0, size, part_size):
        offsets.put_nowait(offset)

    shared = _Shared(location=location)
    resume = asyncio.Event()
    resume.set()
    refresh_lock = asyncio.Lock()

    async def pause(seconds: float) -> None:
        # Every connection waits, not just the one that was told to: the
        # limit applies to the account, and hammering on through the other
        # connections is how an account gets restricted.
        resume.clear()
        try:
            await asyncio.sleep(seconds)
        finally:
            resume.set()

    async def refresh_location(seen: int) -> None:
        async with refresh_lock:
            if shared.generation != seen:
                return  # another worker already refreshed it
            if refresh is None or shared.refreshes >= _MAX_REFRESHES:
                raise ParallelUnavailable("the file reference keeps expiring")
            shared.location = await refresh()
            shared.refreshes += 1
            shared.generation += 1
            log.info("refreshed an expired file reference")

    def expected_length(offset: int) -> int:
        return min(part_size, size - offset)

    with path.open("wb") as handle:
        handle.truncate(size)
        fd = handle.fileno()

        async def worker(source: PartSource) -> None:
            while True:
                try:
                    offset = offsets.get_nowait()
                except asyncio.QueueEmpty:
                    return
                failures = 0
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled()
                    await resume.wait()
                    seen = shared.generation
                    try:
                        data = await source.get(shared.location, offset, part_size)
                    except FloodWaitError as exc:
                        if exc.seconds > flood_ceiling:
                            raise DownloadError(
                                key="err.download.flood", seconds=exc.seconds
                            ) from exc
                        log.info("flood wait of %ss during a parallel download", exc.seconds)
                        if on_flood_wait is not None:
                            on_flood_wait(exc.seconds)
                        await pause(exc.seconds + 1)
                        continue
                    except (FileReferenceExpiredError, FileReferenceInvalidError):
                        await refresh_location(seen)
                        continue
                    except FileMigrateError as exc:
                        raise ParallelUnavailable(f"the file moved to DC {exc.new_dc}") from exc
                    except (ConnectionError, OSError, TimeoutError) as exc:
                        failures += 1
                        if failures >= _PART_ATTEMPTS:
                            raise ParallelUnavailable(f"a connection kept failing: {exc}") from exc
                        await asyncio.sleep(failures)
                        continue
                    break

                want = expected_length(offset)
                if len(data) != want:
                    raise ParallelUnavailable(
                        f"part at {offset} came back with {len(data)} bytes, expected {want}"
                    )
                os.pwrite(fd, data, offset)
                shared.received += len(data)
                if progress is not None:
                    result = progress(shared.received, size)
                    if asyncio.iscoroutine(result):
                        await result

        tasks = [asyncio.create_task(worker(source)) for source in sources]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Read every failure, not just the first: asyncio logs an unread one
        # as "Task exception was never retrieved" when the task is collected.
        failures = [task.exception() for task in done if task.exception() is not None]
        if failures:
            raise failures[0]


# ------------------------------------------------------------------ Telethon


class _SenderSource:
    """A :class:`PartSource` over one raw ``MTProtoSender``."""

    def __init__(self, sender: MTProtoSender) -> None:
        self._sender = sender

    async def get(self, location, offset: int, limit: int) -> bytes:
        result = await self._sender.send(
            GetFileRequest(location=location, offset=offset, limit=limit)
        )
        if isinstance(result, FileCdnRedirect):
            raise ParallelUnavailable("Telegram redirected the file to a CDN")
        return result.bytes


EndpointChooser = Callable[[object, int], Awaitable[list]]
"""Given the client and a DC id, the endpoints to try, best first."""


async def default_endpoints(client, dc_id: int) -> list:
    """The endpoint Telethon itself would use for this DC."""
    return [await client._get_dc(dc_id)]  # noqa: SLF001 - Telethon has no public API for this


async def media_endpoints(client, dc_id: int) -> list:
    """This DC's ``media_only`` endpoints, from Telegram's own config.

    Telethon never picks these: its ``_get_dc`` matches on id, IPv6 and CDN
    only, and takes the first hit. They serve file downloads with the same
    authorisation key as the DC's ordinary endpoint. IPv4 comes first; the
    IPv6 ones (M7.1 §B2.5) follow, for a host that routes IPv6.
    """
    await client._get_dc(dc_id)  # noqa: SLF001 - loads Telegram's config
    config = type(client)._config  # noqa: SLF001 - where Telethon keeps it
    found = [
        option
        for option in config.dc_options
        if option.id == dc_id
        and option.media_only
        and not option.cdn
        and not option.tcpo_only  # needs obfuscation Telethon's TCP does not do
    ]
    return sorted(found, key=lambda option: bool(option.ipv6))


class MediaRoute:
    """``TG_DIRECT_MEDIA=auto``: media endpoints first, ordinary ones after.

    On the NAS, Telegram is only reachable through the proxy, except for a
    few media endpoints that answer directly. Downloading from those skips
    the proxy's bandwidth entirely. A direct route that stops working is
    remembered per DC for a while, so every file does not pay for the same
    failed attempt.
    """

    def __init__(self, *, retry_after: float = 1800.0) -> None:
        self._retry_after = retry_after
        self._failed_at: dict[int, float] = {}

    def usable(self, dc_id: int) -> bool:
        failed = self._failed_at.get(dc_id)
        return failed is None or time.monotonic() - failed >= self._retry_after

    def failed(self, dc_id: int) -> None:
        self._failed_at[dc_id] = time.monotonic()
        log.info("direct media route to DC %s failed; using the proxy for a while", dc_id)

    async def endpoints(self, client, dc_id: int) -> list:
        normal = await default_endpoints(client, dc_id)
        if not self.usable(dc_id):
            return normal
        return await media_endpoints(client, dc_id) + normal

    def refused(self, endpoint) -> None:
        if getattr(endpoint, "media_only", False):
            self.failed(endpoint.id)


def _init_connection(client, query) -> InvokeWithLayerRequest:
    """``initConnection`` with the same identity the client presents."""
    template = client._init_request  # noqa: SLF001
    return InvokeWithLayerRequest(
        LAYER,
        InitConnectionRequest(
            api_id=template.api_id,
            device_model=template.device_model,
            system_version=template.system_version,
            app_version=template.app_version,
            system_lang_code=template.system_lang_code,
            lang_pack=template.lang_pack,
            lang_code=template.lang_code,
            query=query,
            proxy=template.proxy,
            params=template.params,
        ),
    )


@contextlib.asynccontextmanager
async def telethon_sources(
    client,
    dc_id: int,
    count: int,
    *,
    endpoints: EndpointChooser = default_endpoints,
    on_refused: Callable[[object], None] | None = None,
    connect_timeout: float = 10.0,
) -> AsyncIterator[tuple[list[PartSource], object]]:
    """Open ``count`` connections to ``dc_id``; yield them and the endpoint used.

    The authorisation key comes from Telethon: the session's own key for the
    account's home DC, or the key of Telethon's exported sender for any other
    DC, which Telethon creates (and authorises) once per DC anyway. Several
    connections on one key is normal MTProto: each gets its own session id.
    """
    # Private Telethon API throughout: it exposes no way to open extra
    # connections. Pinned by tests against Telethon's actual attributes.
    borrowed = None
    senders: list[MTProtoSender] = []
    try:
        if dc_id == client.session.dc_id:
            auth_key = client.session.auth_key
        else:
            borrowed = await client._borrow_exported_sender(dc_id)  # noqa: SLF001
            auth_key = borrowed.auth_key

        candidates = await endpoints(client, dc_id)
        chosen = None
        for endpoint in candidates:
            try:
                # Bounded: a blocked route should cost seconds, not the minute
                # Telethon's own reconnect attempts would take.
                sender = await asyncio.wait_for(
                    _open(client, auth_key, endpoint, dc_id), connect_timeout
                )
            except (ConnectionError, OSError, TimeoutError) as exc:
                log.info("could not open a connection to %s: %s", _describe(endpoint), exc)
                if on_refused is not None:
                    on_refused(endpoint)
                continue
            senders.append(sender)
            chosen = endpoint
            break
        if chosen is None:
            raise ParallelUnavailable(f"no endpoint of DC {dc_id} accepted a connection")

        for _ in range(count - 1):
            try:
                senders.append(
                    await asyncio.wait_for(
                        _open(client, auth_key, chosen, dc_id), connect_timeout
                    )
                )
            except (ConnectionError, OSError, TimeoutError) as exc:
                # Fewer connections is still faster than falling back.
                log.info("opened %d of %d connections: %s", len(senders), count, exc)
                break

        yield [_SenderSource(sender) for sender in senders], chosen
    finally:
        for sender in senders:
            with contextlib.suppress(Exception):
                await sender.disconnect()
        if borrowed is not None:
            await client._return_exported_sender(borrowed)  # noqa: SLF001


async def _open(client, auth_key, endpoint, dc_id: int) -> MTProtoSender:
    sender = MTProtoSender(auth_key, loggers=client._log)  # noqa: SLF001
    await sender.connect(
        client._connection(  # noqa: SLF001
            endpoint.ip_address,
            endpoint.port,
            dc_id,
            loggers=client._log,  # noqa: SLF001
            proxy=client._proxy,  # noqa: SLF001
            local_addr=client._local_addr,  # noqa: SLF001
        )
    )
    try:
        # A small request to introduce the new session, the same way every
        # Telethon connection introduces itself.
        await sender.send(_init_connection(client, GetNearestDcRequest()))
    except BaseException:
        await sender.disconnect()
        raise
    return sender


def _describe(endpoint) -> str:
    return f"{getattr(endpoint, 'ip_address', '?')}:{getattr(endpoint, 'port', '?')}"
