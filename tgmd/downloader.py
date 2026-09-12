"""Downloading media out of a resolved Telegram message."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

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

from .utils import sanitize_component

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
    supports_streaming: bool = False


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
    supports_streaming = False

    if document is not None:
        for attribute in document.attributes:
            if isinstance(attribute, DocumentAttributeFilename) and not name:
                name = attribute.file_name
            elif isinstance(attribute, DocumentAttributeVideo):
                is_video = True
                is_round = bool(getattr(attribute, "round_message", False))
                supports_streaming = bool(
                    getattr(attribute, "supports_streaming", False)
                )
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
        supports_streaming=supports_streaming,
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


class Downloader:
    """Downloads message media to disk, reporting progress as it goes."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def download(
        self,
        message,
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
        cancel: asyncio.Event | None = None,
    ) -> Path:
        """Download ``message``'s media to ``destination``.

        Retries transient failures and honours Telegram's flood waits. A
        partially written file is always removed, so the download directory
        never accumulates truncated media.
        """
        info = describe_media(message)
        destination.parent.mkdir(parents=True, exist_ok=True)
        ensure_disk_space(destination.parent, info.size)

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
            except DownloadCancelled:
                self._cleanup(destination)
                raise
            except FloodWaitError as exc:
                self._cleanup(destination)
                if exc.seconds > _FLOOD_WAIT_CEILING:
                    raise DownloadError(
                        f"Telegram asked us to wait {exc.seconds}s; try again later"
                    ) from exc
                log.info("flood wait for %ss on attempt %d", exc.seconds, attempt)
                await asyncio.sleep(exc.seconds + 1)
                last_error = exc
                continue
            except (ConnectionError, asyncio.TimeoutError, OSError) as exc:
                self._cleanup(destination)
                last_error = exc
                log.info("download attempt %d failed: %s", attempt, exc)
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(2**attempt)
                continue

            if result is None:
                raise DownloadError("Telegram returned no file for that message")
            return Path(result)

        raise DownloadError(
            f"download failed after {_MAX_ATTEMPTS} attempts: {last_error}"
        )

    async def download_thumbnail(self, message, destination: Path) -> Path | None:
        """Fetch the largest available thumbnail, used when re-uploading video."""
        try:
            result = await self._client.download_media(
                message, file=str(destination), thumb=-1
            )
        except Exception as exc:  # a missing thumbnail must never fail a job
            log.debug("no thumbnail for message %s: %s", message.id, exc)
            return None
        return Path(result) if result else None

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
