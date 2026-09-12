"""Delivering a downloaded file: back through Telegram, to disk, or to PikPak."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from pikpakapi import DownloadStatus
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

from .config import Config
from .db import Database
from .downloader import MediaInfo
from .pikpak import PikPakError, PikPakService
from .utils import human_size
from .webserver import FileServer

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], Awaitable[None] | None]

# Telegram rejects captions longer than this.
CAPTION_LIMIT = 1024


class DeliveryError(RuntimeError):
    """Delivery failed, with a message meant for the user."""


class TooLargeToUpload(DeliveryError):
    """The file exceeds what a bot may upload; the caller should fall back."""


@dataclass
class DeliveryResult:
    """What happened to one file."""

    mode: str
    summary: str
    kept_local: bool = False
    remote_path: str | None = None


class Delivery:
    """Sends files to their destination and maintains the upload cache."""

    def __init__(
        self,
        bot: TelegramClient,
        config: Config,
        db: Database,
        pikpak: PikPakService,
        file_server: FileServer,
    ) -> None:
        self._bot = bot
        self._config = config
        self._db = db
        self._pikpak = pikpak
        self._files = file_server

    # ----------------------------------------------------------- upload cache

    @property
    def cache_enabled(self) -> bool:
        return self._config.delivery.cache_chat_id is not None

    async def send_from_cache(self, chat_id: int, key: str, caption: str) -> bool:
        """Re-send a previously uploaded file, skipping the download entirely.

        Returns ``False`` when there is no usable cache entry, in which case
        the caller should download as normal.
        """
        if not self.cache_enabled:
            return False
        entry = await self._db.cache_lookup(key)
        if entry is None:
            return False

        try:
            cached = await self._bot.get_messages(
                entry["cache_chat_id"], ids=entry["cache_msg_id"]
            )
            if cached is None or not cached.media:
                raise ValueError("cached message has no media")
            await self._bot.send_file(
                chat_id,
                cached.media,
                caption=caption[:CAPTION_LIMIT],
                parse_mode="html",
            )
        except Exception as exc:
            log.info("cache entry %s unusable (%s), re-downloading", key, exc)
            await self._db.cache_forget(key)
            return False

        log.info("served %s from the upload cache", key)
        return True

    async def _store_in_cache(self, key: str, path: Path, info: MediaInfo) -> None:
        """Upload a copy to the cache chat so repeat requests are free."""
        cache_chat_id = self._config.delivery.cache_chat_id
        if cache_chat_id is None:
            return
        try:
            message = await self._bot.send_file(
                cache_chat_id,
                str(path),
                caption=key,
                attributes=self._attributes(info),
                force_document=not (info.is_video or info.is_photo or info.is_audio),
            )
            await self._db.cache_store(
                key, cache_chat_id, message.id, info.file_name, info.size
            )
        except Exception:
            # A broken cache chat must never break delivery to the user.
            log.exception("could not write to the cache chat %s", cache_chat_id)

    # -------------------------------------------------------------- telegram

    @staticmethod
    def _attributes(info: MediaInfo) -> list:
        """Rebuild the attributes that make Telegram play media inline."""
        attributes: list = []
        if info.is_video:
            attributes.append(
                DocumentAttributeVideo(
                    duration=info.duration or 0,
                    w=info.width or 0,
                    h=info.height or 0,
                    round_message=info.is_round,
                    supports_streaming=True,
                )
            )
        elif info.is_audio:
            attributes.append(
                DocumentAttributeAudio(
                    duration=info.duration or 0,
                    voice=info.is_voice,
                )
            )
        return attributes

    async def to_telegram(
        self,
        chat_id: int,
        path: Path,
        info: MediaInfo,
        *,
        caption: str = "",
        cache_key: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> DeliveryResult:
        """Upload the file back to the requesting chat."""
        size = path.stat().st_size if path.exists() else (info.size or 0)
        limit = self._config.delivery.max_upload_bytes
        if size > limit:
            raise TooLargeToUpload(
                f"{human_size(size)} is over the {human_size(limit)} a bot can "
                "upload"
            )

        try:
            await self._bot.send_file(
                chat_id,
                str(path),
                caption=caption[:CAPTION_LIMIT],
                parse_mode="html",
                attributes=self._attributes(info),
                force_document=not (info.is_video or info.is_photo or info.is_audio),
                progress_callback=self._wrap_progress(progress),
            )
        except FloodWaitError as exc:
            raise DeliveryError(
                f"Telegram asked us to wait {exc.seconds}s before uploading again"
            ) from exc
        except Exception as exc:
            raise DeliveryError(f"upload failed: {exc}") from exc

        if cache_key:
            await self._store_in_cache(cache_key, path, info)

        return DeliveryResult(mode="telegram", summary=f"sent {human_size(size)}")

    # ----------------------------------------------------------------- local

    async def to_local(self, path: Path, info: MediaInfo) -> DeliveryResult:
        """Leave the file on disk and report where it landed."""
        size = path.stat().st_size if path.exists() else (info.size or 0)
        try:
            shown = path.relative_to(Path.cwd())
        except ValueError:
            shown = path
        return DeliveryResult(
            mode="local",
            summary=f"saved to <code>{shown}</code> ({human_size(size)})",
            kept_local=True,
            remote_path=str(path),
        )

    # ---------------------------------------------------------------- pikpak

    async def to_pikpak(
        self, path: Path, info: MediaInfo, *, folder: str | None = None
    ) -> DeliveryResult:
        """Hand the file to PikPak by publishing it on the bot's HTTP server."""
        if not self._pikpak.configured:
            raise DeliveryError(
                "PikPak is not configured. Set PIKPAK_USERNAME and PIKPAK_PASSWORD."
            )
        if not self._files.usable:
            raise DeliveryError(
                "PikPak cannot fetch Telegram media without the HTTP file "
                "server. Set HTTP_ENABLED=true and PUBLIC_BASE_URL, or use "
                "/mode local. Magnet and URL transfers work without it."
            )

        url = self._files.publish(path, name=info.file_name)
        status: DownloadStatus | None = None
        try:
            task = await self._pikpak.offline_download(
                url, folder=folder, name=info.file_name
            )
            status = await self._pikpak.wait_for_task(task)
        except PikPakError as exc:
            self._files.unpublish_all(path)
            raise DeliveryError(str(exc)) from exc
        finally:
            # Stop serving as soon as PikPak is done with it. While a task is
            # still running the URL has to stay alive, so it is left to expire.
            if status_is_final(status):
                self._files.unpublish_all(path)

        target = folder or self._config.pikpak.folder
        if status is DownloadStatus.done:
            return DeliveryResult(
                mode="pikpak",
                summary=f"saved to PikPak <code>{target}/{info.file_name}</code>",
                remote_path=f"{target}/{info.file_name}",
            )
        if status is DownloadStatus.error:
            raise DeliveryError("PikPak reported an error fetching the file")
        return DeliveryResult(
            mode="pikpak",
            summary=(
                f"PikPak is still fetching <code>{info.file_name}</code>; it will "
                "appear in your drive shortly"
            ),
            kept_local=True,
            remote_path=f"{target}/{info.file_name}",
        )

    async def url_to_pikpak(
        self, url: str, *, folder: str | None = None, wait: bool = False
    ) -> DeliveryResult:
        """Transfer a magnet link or direct URL without touching local disk."""
        try:
            task = await self._pikpak.offline_download(url, folder=folder)
        except PikPakError as exc:
            raise DeliveryError(str(exc)) from exc

        target = folder or self._config.pikpak.folder
        if not wait:
            return DeliveryResult(
                mode="pikpak",
                summary=f"queued in PikPak: <code>{task.name}</code> → {target}",
                remote_path=f"{target}/{task.name}",
            )

        status = await self._pikpak.wait_for_task(task)
        if status is DownloadStatus.done:
            return DeliveryResult(
                mode="pikpak",
                summary=f"saved to PikPak <code>{target}/{task.name}</code>",
                remote_path=f"{target}/{task.name}",
            )
        if status is DownloadStatus.error:
            raise DeliveryError(f"PikPak could not fetch {task.name}")
        return DeliveryResult(
            mode="pikpak",
            summary=f"PikPak is still working on <code>{task.name}</code>",
            remote_path=f"{target}/{task.name}",
        )

    # ------------------------------------------------------------- internals

    @staticmethod
    def _wrap_progress(progress: ProgressCallback | None):
        if progress is None:
            return None

        async def callback(sent: int, total: int) -> None:
            result = progress(sent, total)
            if asyncio.iscoroutine(result):
                await result

        return callback


def status_is_final(status: object) -> bool:
    """True when a PikPak task will not change state again."""
    return status in (
        DownloadStatus.done,
        DownloadStatus.error,
        DownloadStatus.not_found,
    )
