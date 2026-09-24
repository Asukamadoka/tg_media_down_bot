"""Downloading media out of a resolved Telegram message."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
)

from .parallel import (
    MAX_CONNECTIONS,
    MIN_PARALLEL_SIZE,
    EndpointChooser,
    MediaRoute,
    ParallelUnavailable,
    default_endpoints,
    document_location,
    download_parts,
    media_endpoints,
    telethon_sources,
)
from .utils import human_rate, human_size, sanitize_component

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], Awaitable[None] | None]

# Leave this much headroom so the disk never fills completely.
_DISK_HEADROOM = 256 * 1024 * 1024

_MAX_ATTEMPTS = 3
_FLOOD_WAIT_CEILING = 300


class DownloadCancelled(Exception):
    """The user cancelled the job while it was downloading."""


class DownloadError(RuntimeError):
    """The download failed, with a message meant for the user."""


@dataclass
class MediaInfo:
    """What is known about a message's attachment before downloading it."""

    file_name: str
    size: int | None = None
    mime_type: str | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    is_video: bool = False
    is_audio: bool = False
    is_photo: bool = False
    is_round: bool = False
    is_voice: bool = False


def has_downloadable_media(message) -> bool:
    """True when the message carries something worth downloading.

    Link previews are excluded: they are media objects, but downloading one
    just yields the preview thumbnail, which is never what a user wants.
    """
    media = getattr(message, "media", None)
    if media is None:
        return False
    if isinstance(media, MessageMediaWebPage):
        return False
    return isinstance(media, (MessageMediaDocument, MessageMediaPhoto)) or bool(
        getattr(message, "file", None)
    )


def _guessed_extension(mime_type: str | None, fallback: str = ".bin") -> str:
    if not mime_type:
        return fallback
    if mime_type == "image/jpeg":
        return ".jpg"
    return mimetypes.guess_extension(mime_type) or fallback


def describe_media(message) -> MediaInfo:
    """Collect the metadata needed for naming, limits and re-upload."""
    file_helper = getattr(message, "file", None)
    document = getattr(message, "document", None)
    photo = getattr(message, "photo", None)

    mime_type = getattr(file_helper, "mime_type", None)
    size = getattr(file_helper, "size", None)

    name: str | None = getattr(file_helper, "name", None)
    duration = getattr(file_helper, "duration", None)
    width = getattr(file_helper, "width", None)
    height = getattr(file_helper, "height", None)

    is_video = False
    is_audio = False
    is_round = False
    is_voice = False

    if document is not None:
        for attribute in document.attributes:
            if isinstance(attribute, DocumentAttributeFilename) and not name:
                name = attribute.file_name
            elif isinstance(attribute, DocumentAttributeVideo):
                is_video = True
                is_round = bool(getattr(attribute, "round_message", False))
                duration = duration or attribute.duration
                width = width or attribute.w
                height = height or attribute.h
            elif isinstance(attribute, DocumentAttributeAudio):
                is_audio = True
                is_voice = bool(getattr(attribute, "voice", False))
                duration = duration or attribute.duration

    is_photo = photo is not None and document is None
    if is_photo:
        mime_type = mime_type or "image/jpeg"

    if not name:
        extension = ".jpg" if is_photo else _guessed_extension(mime_type)
        name = f"{message.id}{extension}"

    return MediaInfo(
        file_name=sanitize_component(name, fallback=f"{message.id}.bin"),
        size=size,
        mime_type=mime_type,
        duration=int(duration) if duration else None,
        width=width,
        height=height,
        is_video=is_video,
        is_audio=is_audio,
        is_photo=is_photo,
        is_round=is_round,
        is_voice=is_voice,
    )


def ensure_disk_space(target_dir: Path, needed: int | None) -> None:
    """Raise :class:`DownloadError` if ``needed`` bytes will not fit."""
    if not needed:
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target_dir)
    if usage.free < needed + _DISK_HEADROOM:
        raise DownloadError(
            f"not enough free disk space: {needed / 1024 / 1024:.0f} MiB needed, "
            f"{usage.free / 1024 / 1024:.0f} MiB free"
        )


@dataclass
class Transfer:
    """How the last download went, for the log line and for the benchmark."""

    size: int
    seconds: float
    connections: int
    dc_id: int | None
    endpoint: str | None = None

    @property
    def rate(self) -> float:
        return self.size / self.seconds if self.seconds > 0 else 0.0


class Downloader:
    """Downloads message media to disk, reporting progress as it goes."""

    def __init__(
        self,
        client: TelegramClient,
        *,
        connections: int = 1,
        endpoints: EndpointChooser = default_endpoints,
        route: MediaRoute | None = None,
    ) -> None:
        self._client = client
        self._connections = max(1, min(connections, MAX_CONNECTIONS))
        self._endpoints = endpoints
        # TG_DIRECT_MEDIA=auto: prefer media endpoints, which on the NAS are
        # reachable without the proxy. Overrides ``endpoints``.
        self._route = route
        self.last: Transfer | None = None

    async def download(
        self,
        message,
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
        cancel: asyncio.Event | None = None,
    ) -> Path:
        """Download ``message``'s media to ``destination``.

        Large documents go over several connections at once when that is
        configured; anything that cannot, falls back to one. Retries transient
        failures and honours Telegram's flood waits. A partially written file
        is always removed, so the download directory never accumulates
        truncated media.
        """
        info = describe_media(message)
        destination.parent.mkdir(parents=True, exist_ok=True)
        ensure_disk_space(destination.parent, info.size)

        started = time.monotonic()
        transfer = await self._parallel(message, info, destination, progress, cancel)
        if transfer is None:
            path = await self._sequential(message, destination, progress, cancel)
            transfer = Transfer(
                size=path.stat().st_size if path.exists() else (info.size or 0),
                seconds=0.0,
                connections=1,
                dc_id=_dc_of(message),
            )
        else:
            path = destination
        transfer.seconds = time.monotonic() - started
        self.last = transfer
        # One line per file, so the operator can see where their files live
        # (which DC) and what a connection count actually buys.
        log.info(
            "downloaded %s: %s in %.1fs (%s) from DC %s over %d connection(s)%s",
            info.file_name,
            human_size(transfer.size),
            transfer.seconds,
            human_rate(transfer.rate),
            transfer.dc_id if transfer.dc_id is not None else "?",
            transfer.connections,
            f" via {transfer.endpoint}" if transfer.endpoint else "",
        )
        return path

    async def _parallel(self, message, info: MediaInfo, destination: Path, progress, cancel):
        """Try our own connections. Returns None when they do not apply.

        Large documents use several. With a direct media route, smaller
        documents use one of ours too, because Telethon's own download would
        go over its main connection, through the proxy.
        """
        document = getattr(message, "document", None)
        size = info.size or 0
        if document is None or not size:
            return None
        dc_id = document.dc_id
        if self._connections >= 2 and size >= MIN_PARALLEL_SIZE:
            count = self._connections
        elif await self._direct_route_to(dc_id):
            count = 1
        else:
            return None

        async def refresh():
            # A file reference expires; the message, fetched again, carries a
            # fresh one.
            fresh = await self._client.get_messages(
                await message.get_input_chat(), ids=message.id
            )
            if fresh is None or getattr(fresh, "document", None) is None:
                raise ParallelUnavailable("the message is gone")
            return document_location(fresh.document)

        route = self._route
        chooser = route.endpoints if route is not None else self._endpoints
        attempts = [chooser] if route is None else [chooser, default_endpoints]
        for index, endpoints in enumerate(attempts):
            used = None
            try:
                async with telethon_sources(
                    self._client,
                    dc_id,
                    count,
                    endpoints=endpoints,
                    on_refused=route.refused if route is not None else None,
                ) as (sources, endpoint):
                    used = endpoint
                    await download_parts(
                        sources,
                        location=document_location(document),
                        size=size,
                        path=destination,
                        progress=progress,
                        cancel=cancel,
                        refresh=refresh,
                        flood_ceiling=_FLOOD_WAIT_CEILING,
                    )
                    return Transfer(
                        size=size,
                        seconds=0.0,
                        connections=len(sources),
                        dc_id=dc_id,
                        endpoint=_endpoint_label(endpoint),
                    )
            except ParallelUnavailable as exc:
                self._cleanup(destination)
                direct = getattr(used, "media_only", False)
                if direct and route is not None and index + 1 < len(attempts):
                    # Connected directly, then broke: a firewall that lets the
                    # handshake through and resets the transfer. Try the
                    # proxied endpoint before giving up on parallel.
                    route.failed(dc_id)
                    log.info("direct media download broke (%s); retrying via the proxy", exc)
                    continue
                log.info("parallel download not possible (%s); using one connection", exc)
                return None
            except BaseException:
                self._cleanup(destination)
                raise
        return None

    async def _direct_route_to(self, dc_id: int) -> bool:
        """True when a direct media route to this DC is on and exists."""
        if self._route is None or not self._route.usable(dc_id):
            return False
        try:
            return bool(await media_endpoints(self._client, dc_id))
        except Exception:  # an optimisation must never fail a download
            log.debug("could not list media endpoints for DC %s", dc_id, exc_info=True)
            return False

    async def _sequential(self, message, destination: Path, progress, cancel) -> Path:
        """Telethon's own download, over one connection, with retries."""
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            try:
                result = await self._client.download_media(
                    message,
                    file=str(destination),
                    progress_callback=self._wrap_progress(progress, cancel),
                )
            except BaseException as exc:
                # Every way out of a failed attempt, retried or not, starts by
                # removing what it half wrote.
                self._cleanup(destination)
                if not isinstance(
                    exc, (FloodWaitError, TimeoutError, ConnectionError, OSError)
                ):
                    raise
                error: Exception = exc
            else:
                if result is None:
                    raise DownloadError("Telegram returned no file for that message")
                return Path(result)

            if isinstance(error, FloodWaitError):
                if error.seconds > _FLOOD_WAIT_CEILING:
                    raise DownloadError(
                        f"Telegram asked us to wait {error.seconds}s; try again later"
                    ) from error
                log.info("flood wait for %ss on attempt %d", error.seconds, attempt)
                await asyncio.sleep(error.seconds + 1)
            else:
                log.info("download attempt %d failed: %s", attempt, error)
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(2**attempt)
            last_error = error

        raise DownloadError(
            f"download failed after {_MAX_ATTEMPTS} attempts: {last_error}"
        )

    @staticmethod
    def _wrap_progress(
        progress: ProgressCallback | None, cancel: asyncio.Event | None
    ):
        """Adapt our callback to Telethon's, and use it as a cancellation point.

        Telethon has no way to abort a transfer, but it does call the progress
        callback every chunk, so raising from there stops the download within
        a chunk or two.
        """

        async def callback(received: int, total: int) -> None:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            if progress is None:
                return
            result = progress(received, total)
            if asyncio.iscoroutine(result):
                await result

        return callback

    @staticmethod
    def _cleanup(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:  # pragma: no cover - unlikely, not fatal
            log.debug("could not remove partial download %s: %s", path, exc)


class RateTracker:
    """Tracks average transfer speed and estimates the time remaining."""

    def __init__(self) -> None:
        self._started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return max(time.monotonic() - self._started, 1e-6)

    def rate(self, received: int) -> float:
        """Average bytes per second since the transfer started."""
        return received / self.elapsed

    def eta(self, received: int, total: int | None) -> float | None:
        """Seconds remaining at the average rate, or ``None`` if unknowable."""
        if not total or received <= 0 or received >= total:
            return None
        rate = self.rate(received)
        if rate <= 0:
            return None
        return max((total - received) / rate, 0.0)


def _dc_of(message) -> int | None:
    """The data centre a message's file lives in, when there is one."""
    for attribute in ("document", "photo"):
        media = getattr(message, attribute, None)
        if media is not None and getattr(media, "dc_id", None) is not None:
            return int(media.dc_id)
    return None


def _endpoint_label(endpoint) -> str | None:
    if endpoint is None:
        return None
    kind = " media" if getattr(endpoint, "media_only", False) else ""
    return f"{getattr(endpoint, 'ip_address', '?')}:{getattr(endpoint, 'port', '?')}{kind}"
