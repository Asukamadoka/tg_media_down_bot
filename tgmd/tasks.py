"""The job queue: one worker pool that turns links into delivered files."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from telethon import TelegramClient
from telethon.utils import get_peer_id

from .config import Config
from .db import Database, cache_key
from .delivery import Delivery, DeliveryError, TooLargeToUpload
from .downloader import (
    DownloadCancelled,
    Downloader,
    DownloadError,
    RateTracker,
    can_stream,
    describe_media,
    has_downloadable_media,
)
from .forwarder import Forwarder, Outcome, forwardable
from .i18n import Explained, describe, t
from .links import MessageRef
from .pikpak import PikPakError, PikPakService
from .reporter import Reporter
from .resolver import ResolveError, Resolver
from .utils import (
    build_relative_path,
    escape_html,
    human_duration,
    human_rate,
    human_size,
    progress_bar,
    truncate,
    unique_path,
)

log = logging.getLogger(__name__)


class JobKind(StrEnum):
    MESSAGE = "message"
    """A Telegram message link: download, then deliver."""

    URL = "url"
    """A magnet link or direct URL: PikPak fetches it itself."""

    SHARE = "share"
    """A PikPak share link: save it into the account."""

    INBOUND = "inbound"
    """Media sent or forwarded directly to the bot, downloaded by the bot."""


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


@dataclass
class Job:
    """One unit of work, as submitted by a user."""

    id: int
    user_id: int
    chat_id: int
    mode: str
    kind: JobKind
    label: str
    ref: MessageRef | None = None
    url: str | None = None
    message: object | None = None
    pikpak_folder: str | None = None
    reply_to: int | None = None
    state: JobState = JobState.QUEUED
    detail: str = ""
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    cache_hint: bool = False
    """Something could have been forwarded, had a cache channel been set."""
    forward_to: int | None = None
    """Inbound media that can be forwarded goes here as is, nothing downloaded
    (a video posted in the cache channel, M7.2 B). None: it is downloaded."""

    @property
    def active(self) -> bool:
        return self.state in (JobState.QUEUED, JobState.RUNNING)


def saved_to_pikpak(job: Job) -> bool:
    """True when the job finished with at least one file put into PikPak."""
    if job.state not in (JobState.DONE, JobState.PARTIAL):
        return False
    return job.kind in (JobKind.URL, JobKind.SHARE) or job.mode == "pikpak"


class QueueFull(Explained, RuntimeError):
    """The user already has as many jobs pending as they are allowed."""


class JobQueue:
    """Bounded worker pool that processes jobs in submission order."""

    def __init__(
        self,
        config: Config,
        db: Database,
        bot: TelegramClient,
        resolver: Resolver,
        downloader: Downloader,
        bot_downloader: Downloader,
        delivery: Delivery,
        pikpak: PikPakService,
        forwarder: Forwarder | None = None,
        after_pikpak: Callable[[Job], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._bot = bot
        self._resolver = resolver
        self._downloader = downloader
        self._bot_downloader = bot_downloader
        self._delivery = delivery
        self._pikpak = pikpak
        self._forwarder = forwarder
        # Told about every job that put files into PikPak (WMS shelving).
        self._after_pikpak = after_pikpak
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._jobs: dict[int, Job] = {}
        self._workers: list[asyncio.Task] = []

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        for index in range(self._config.download.concurrent):
            self._workers.append(
                asyncio.create_task(self._worker(index), name=f"tgmd-worker-{index}")
            )
        log.info("started %d download worker(s)", len(self._workers))

    def rebind_reader(self, resolver: Resolver, downloader: Downloader) -> None:
        """Swap in a new reading client, after an in-chat login adds one.

        Jobs already running keep the client they started with, which is what
        you want: replacing it mid-download would abort the transfer. Anything
        queued from here on uses the new one.
        """
        self._resolver = resolver
        self._downloader = downloader
        log.info("job queue rebound to a new reading client")

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        # gather collects the workers' own cancellations as results, while a
        # cancellation aimed at whoever called stop() still propagates.
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    # ------------------------------------------------------------ submission

    def pending_for(self, user_id: int) -> list[Job]:
        return [job for job in self._jobs.values() if job.user_id == user_id and job.active]

    async def submit(self, job: Job) -> int:
        """Queue a job, returning its position in line."""
        limit = self._config.download.max_queue_per_user
        if len(self.pending_for(job.user_id)) >= limit:
            raise QueueFull(key="job.queue_full", limit=limit)
        self._jobs[job.id] = job
        await self._queue.put(job)
        return self._queue.qsize()

    def cancel(self, user_id: int, job_id: int | None = None) -> list[Job]:
        """Cancel one job or every active job of a user."""
        cancelled: list[Job] = []
        for job in list(self._jobs.values()):
            if job.user_id != user_id or not job.active:
                continue
            if job_id is not None and job.id != job_id:
                continue
            job.cancel.set()
            if job.state is JobState.QUEUED:
                job.state = JobState.CANCELLED
            cancelled.append(job)
        return cancelled

    def snapshot(self, user_id: int | None = None) -> list[Job]:
        """Active jobs, oldest first, optionally limited to one user."""
        jobs = [
            job
            for job in self._jobs.values()
            if job.active and (user_id is None or job.user_id == user_id)
        ]
        return sorted(jobs, key=lambda job: job.id)

    # --------------------------------------------------------------- workers

    async def _worker(self, index: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job.cancel.is_set():
                    job.state = JobState.CANCELLED
                    await self._db.finish_job(job.id, "cancelled")
                    continue
                job.state = JobState.RUNNING
                await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a worker must never die on one job
                log.exception("worker %d crashed on job %d", index, job.id)
                job.state = JobState.FAILED
                job.detail = str(exc)
                await self._db.finish_job(job.id, "failed", error=str(exc))
            finally:
                self._queue.task_done()
                if not job.active:
                    self._jobs.pop(job.id, None)

    async def _run(self, job: Job) -> None:
        reporter = Reporter(
            self._bot,
            job.chat_id,
            interval=self._config.download.progress_interval,
            reply_to=job.reply_to,
        )
        runner = {
            JobKind.MESSAGE: self._run_message_job,
            JobKind.INBOUND: self._run_inbound_job,
            JobKind.URL: self._run_url_job,
            JobKind.SHARE: self._run_share_job,
        }[job.kind]
        try:
            await runner(job, reporter)
        except Exception as exc:
            # Each runner reports the failures it expects. Anything else would
            # leave the status message frozen mid-way, so close it here and
            # let the worker record the failure.
            await reporter.close(t("error.generic", error=escape_html(describe(exc))))
            raise
        if self._after_pikpak is not None and saved_to_pikpak(job):
            try:
                await self._after_pikpak(job)
            except Exception:
                # Shelving is a courtesy; the transfer itself already succeeded.
                log.exception("after-PikPak hook failed for job %d", job.id)

    # -------------------------------------------------------- PikPak-only jobs

    async def _run_url_job(self, job: Job, reporter: Reporter) -> None:
        """Hand a magnet link or direct URL straight to PikPak."""
        await reporter.open(
            t("job.pikpak.sending", label=escape_html(job.label))
        )
        if not await self._pikpak.available_for(job.user_id):
            job.state = JobState.FAILED
            job.detail = "no PikPak account connected"
            await reporter.close(
                t("job.pikpak.no_account")
            )
            await self._db.finish_job(job.id, "failed", error=job.detail)
            return

        try:
            result = await self._delivery.url_to_pikpak(
                job.url or "", folder=job.pikpak_folder, user_id=job.user_id
            )
        except (DeliveryError, PikPakError) as exc:
            job.state = JobState.FAILED
            job.detail = str(exc)
            await reporter.close(
                t("error.generic", error=escape_html(describe(exc)))
            )
            await self._db.finish_job(job.id, "failed", error=str(exc))
            return

        job.state = JobState.DONE
        await reporter.close(f"✅ {result.summary}")
        await self._db.finish_job(job.id, "done", file_name=result.remote_path)

    async def _run_share_job(self, job: Job, reporter: Reporter) -> None:
        """Save a PikPak share link into the account."""
        await reporter.open(t("job.share.saving"))
        try:
            names = await self._pikpak.restore_share(
                job.url or "", user_id=job.user_id
            )
        except PikPakError as exc:
            job.state = JobState.FAILED
            job.detail = str(exc)
            await reporter.close(
                t("error.generic", error=escape_html(describe(exc)))
            )
            await self._db.finish_job(job.id, "failed", error=str(exc))
            return

        listing = "\n".join(f"• <code>{escape_html(name)}</code>" for name in names[:20])
        if len(names) > 20:
            listing += t("job.share.more", count=len(names) - 20)
        job.state = JobState.DONE
        await reporter.close(
            t("job.share.saved", count=len(names), listing=listing)
        )
        await self._db.finish_job(job.id, "done", file_name=", ".join(names[:5]))

    # ----------------------------------------------------- directly sent media

    async def _run_inbound_job(self, job: Job, reporter: Reporter) -> None:
        """Handle media the user sent or forwarded to the bot itself.

        The bot already has access to this file, so the bot client downloads
        it. Sending it straight back would be pointless, so these jobs only
        ever save locally or transfer to PikPak.
        """
        message = job.message
        if message is None or not has_downloadable_media(message):
            job.state = JobState.FAILED
            await reporter.open(t("job.message.no_media"))
            await self._db.finish_job(job.id, "failed", error="no media")
            return

        info = describe_media(message)
        if job.forward_to is not None:
            # Forwardable, so Telegram copies it server-side: the "instant"
            # half of auto mode. Only what cannot be forwarded is downloaded.
            try:
                await self._bot.send_file(job.forward_to, message.media)
            except Exception as exc:  # noqa: BLE001 - a failed copy falls back to a download
                log.info("job %d: forwarding the posted media failed: %s", job.id, exc)
            else:
                job.state = JobState.DONE
                await reporter.open(
                    t("job.forwarded", prefix="", name=escape_html(info.file_name))
                )
                await self._db.finish_job(
                    job.id, "done", file_name=info.file_name, file_size=info.size
                )
                return
        await reporter.open(
            t(
                "job.inbound.downloading",
                name=escape_html(truncate(info.file_name, 48)),
                size=human_size(info.size),
            )
        )

        try:
            await self._handle_one(
                job=job,
                reporter=reporter,
                message=message,
                chat_title=_forward_origin(message),
                peer_id=job.chat_id,
                prefix="",
                downloader=self._bot_downloader,
                source=None,
            )
        except DownloadCancelled:
            job.state = JobState.CANCELLED
            await reporter.close(t("job.cancelled"))
            await self._db.finish_job(job.id, "cancelled")
            return
        except (DownloadError, DeliveryError) as exc:
            job.state = JobState.FAILED
            job.detail = str(exc)
            await reporter.close(
                t("error.generic", error=escape_html(describe(exc)))
            )
            await self._db.finish_job(job.id, "failed", error=str(exc))
            return

        job.state = JobState.DONE
        await self._db.finish_job(
            job.id, "done", file_name=info.file_name, file_size=info.size
        )

    # ------------------------------------------------------- Telegram link jobs

    async def _run_message_job(self, job: Job, reporter: Reporter) -> None:
        ref = job.ref
        assert ref is not None  # guaranteed by JobKind.MESSAGE
        await reporter.open(t("job.lookup", ref=escape_html(ref.describe())))

        try:
            entity, messages = await self._resolver.resolve(ref)
        except ResolveError as exc:
            job.state = JobState.FAILED
            job.detail = str(exc)
            await reporter.close(
                t("error.generic", error=escape_html(describe(exc)))
            )
            await self._db.finish_job(job.id, "failed", error=str(exc))
            return

        cap = self._config.download.max_batch
        truncated = len(messages) > cap
        if truncated:
            messages = messages[:cap]

        chat_title = _chat_title(entity)
        peer_id = get_peer_id(entity)

        succeeded = 0
        skipped = 0
        failures: list[tuple[int, BaseException]] = []

        for index, message in enumerate(messages, start=1):
            if job.cancel.is_set():
                break

            prefix = (
                f"[{index}/{len(messages)}] " if len(messages) > 1 else ""
            )
            if not has_downloadable_media(message):
                skipped += 1
                continue

            try:
                await self._handle_one(
                    job=job,
                    reporter=reporter,
                    message=message,
                    chat_title=chat_title,
                    peer_id=peer_id,
                    prefix=prefix,
                    downloader=self._downloader,
                    source=entity,
                )
                succeeded += 1
            except DownloadCancelled:
                break
            except (DownloadError, DeliveryError, ResolveError) as exc:
                failures.append((message.id, exc))
                log.info("job %d message %s failed: %s", job.id, message.id, exc)
            except Exception as exc:  # unexpected, but one message must not kill the job
                failures.append((message.id, exc))
                log.exception("job %d message %s crashed", job.id, message.id)

        await self._finalize(
            job, reporter, succeeded, skipped, failures, len(messages), truncated, cap
        )

    async def _handle_one(
        self,
        *,
        job: Job,
        reporter: Reporter,
        message,
        chat_title: str,
        peer_id: int,
        prefix: str,
        downloader: Downloader,
        source,
    ) -> None:
        """Deliver a single message's media, downloading only if nothing cheaper works.

        ``source`` is the chat the message was read from, or None for media
        sent straight to the bot, which is never forwarded back.
        """
        info = describe_media(message)
        key = cache_key(peer_id, message.id)
        caption = _build_caption(message, chat_title)
        # auto starts out as telegram and becomes local for what cannot be
        # forwarded; every other mode is what it says.
        to_telegram = job.mode in ("telegram", "auto")
        mode = job.mode

        # The cheapest path: a file we have already uploaded once. Only when
        # it is going back through Telegram; `and` short-circuits the await.
        if to_telegram and await self._delivery.send_from_cache(
            job.chat_id, key, caption
        ):
            await reporter.update(
                t(
                    "job.cached",
                    prefix=prefix,
                    name=escape_html(info.file_name),
                ),
                force=True,
            )
            return

        # Next cheapest: let Telegram copy it server-side. Only a restricted
        # source, or no way to forward, falls through to the download.
        hint = ""
        restricted = source is not None and not forwardable(message, source)
        if to_telegram and self._forwarder is not None and source is not None:
            attempt = await self._forwarder.deliver(
                chat_id=job.chat_id,
                message=message,
                source=source,
                caption=caption,
                key=key,
                info=info,
            )
            if attempt.delivered:
                log.info("job %d message %s forwarded, nothing downloaded", job.id, message.id)
                await reporter.update(
                    t("job.forwarded", prefix=prefix, name=escape_html(info.file_name)),
                    force=True,
                )
                return
            restricted = attempt.outcome is Outcome.RESTRICTED
            if attempt.outcome is Outcome.NO_CACHE and not job.cache_hint:
                job.cache_hint = True
                hint = t("job.hint_cache")
        if job.mode == "auto":
            # Restricted content can only be had by downloading it, and then
            # it is watched on the NAS rather than pushed anywhere else.
            mode = "local" if restricted or source is None else "telegram"

        if mode == "pikpak" and self._config.pikpak.stream and can_stream(message):
            # PIKPAK_STREAM: PikPak reads the bytes from Telegram through the
            # file server as it asks for them. No download, nothing on disk.
            await reporter.update(
                t("job.handing_to_pikpak", prefix=prefix, name=escape_html(info.file_name)),
                force=True,
            )
            result = await self._delivery.stream_to_pikpak(
                lambda start, end: downloader.stream(message, start, end),
                info,
                size=message.document.size,
                folder=job.pikpak_folder,
                user_id=job.user_id,
            )
            await reporter.update(
                t(
                    "job.delivered",
                    prefix=prefix,
                    label=escape_html(truncate(info.file_name, 48)),
                    summary=result.summary,
                ),
                force=True,
            )
            return

        def place(template: str, root: Path) -> Path:
            return root / build_relative_path(
                template,
                chat=chat_title,
                chat_id=peer_id,
                message_id=message.id,
                name=info.file_name,
                topic_id=job.ref.topic_id if job.ref else None,
                when=getattr(message, "date", None),
            )

        download = self._config.download
        keep_at = place(download.media_template, download.media_root)
        # A file that is to be kept is downloaded straight to where it stays.
        destination = unique_path(
            keep_at if mode == "local" else place(download.filename_template, download.dir)
        )

        tracker = RateTracker()
        label = truncate(info.file_name, 48)

        async def on_download(received: int, total: int) -> None:
            fraction = received / total if total else 0.0
            await reporter.update(
                t(
                    "job.downloading_progress",
                    prefix=prefix,
                    label=escape_html(label),
                    bar=progress_bar(fraction),
                    percent=f"{fraction * 100:.0f}",
                    received=human_size(received),
                    total=human_size(total),
                    rate=human_rate(tracker.rate(received)),
                    eta=human_duration(tracker.eta(received, total)),
                )
            )

        await reporter.update(
            t(
                "job.downloading",
                prefix=prefix,
                label=escape_html(label),
                size=human_size(info.size),
            ),
            force=True,
        )
        path = await downloader.download(
            message, destination, progress=on_download, cancel=job.cancel
        )

        try:
            result = await self._deliver(
                job, mode, reporter, path, info, caption, key, prefix, keep_at
            )
        except BaseException:
            # A failed delivery has no retry, so the file would sit on disk
            # with nobody told where. Keep it only if files are kept anyway.
            self._discard(path)
            raise
        if not result.kept_local:
            self._discard(path)

        await reporter.update(
            t(
                "job.delivered",
                prefix=prefix,
                label=escape_html(label),
                summary=result.summary,
            )
            + hint,
            force=True,
        )

    def _discard(self, path: Path) -> None:
        """Remove a downloaded file, unless the operator keeps them all."""
        if not self._config.download.delete_after_delivery:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("could not remove %s: %s", path, exc)

    async def _deliver(
        self, job, mode: str, reporter, path: Path, info, caption, key, prefix, keep_at: Path
    ):
        """Send the downloaded file to wherever ``mode`` points."""
        upload_tracker = RateTracker()

        async def on_upload(sent: int, total: int) -> None:
            fraction = sent / total if total else 0.0
            await reporter.update(
                t(
                    "job.uploading",
                    prefix=prefix,
                    name=escape_html(truncate(info.file_name, 48)),
                    bar=progress_bar(fraction),
                    percent=f"{fraction * 100:.0f}",
                    sent=human_size(sent),
                    total=human_size(total),
                    rate=human_rate(upload_tracker.rate(sent)),
                )
            )

        if mode == "local":
            return await self._delivery.to_local(path, info)

        if mode == "pikpak":
            await reporter.update(
                t(
                    "job.handing_to_pikpak",
                    prefix=prefix,
                    name=escape_html(info.file_name),
                ),
                force=True,
            )
            return await self._delivery.to_pikpak(
                path,
                info,
                folder=job.pikpak_folder,
                user_id=job.user_id,
                delete_when_done=self._config.download.delete_after_delivery,
            )

        try:
            return await self._delivery.to_telegram(
                job.chat_id,
                path,
                info,
                caption=caption,
                cache_key=key if self._delivery.cache_enabled else None,
                progress=on_upload,
            )
        except TooLargeToUpload as exc:
            # Falling back is better than losing a download that already cost
            # bandwidth, so the file stays on disk and the user is told why.
            log.info("job %d falling back to local: %s", job.id, exc)
            result = await self._delivery.to_local(path, info, keep_at=unique_path(keep_at))
            result.summary = f"{escape_html(describe(exc))}; {result.summary}"
            return result

    async def _finalize(
        self,
        job: Job,
        reporter: Reporter,
        succeeded: int,
        skipped: int,
        failures: list[tuple[int, BaseException]],
        total: int,
        truncated: bool,
        cap: int,
    ) -> None:
        """Set the job's final state and post a summary when it is worth one."""
        if job.cancel.is_set():
            job.state = JobState.CANCELLED
            await reporter.close(t("job.cancelled_after", count=succeeded))
            await self._db.finish_job(job.id, "cancelled")
            return

        notes: list[str] = []
        if skipped:
            notes.append(t("job.note_skipped", count=skipped))
        if truncated:
            notes.append(t("job.note_truncated", cap=cap))
        # A single file already carried the hint on its own line; a batch's
        # summary replaces those lines, so it repeats it once here.
        if job.cache_hint and total > 1:
            notes.append(t("job.hint_cache").strip())
        if failures:
            shown = "\n".join(
                f"• {message_id}: {escape_html(describe(exc))}" for message_id, exc in failures[:5]
            )
            if len(failures) > 5:
                shown += t("job.note_more", count=len(failures) - 5)
            notes.append(t("job.note_failed", count=len(failures), shown=shown))

        if succeeded and not failures:
            job.state = JobState.DONE
            status = "done"
        elif succeeded:
            job.state = JobState.PARTIAL
            status = "partial"
        else:
            job.state = JobState.FAILED
            status = "failed"

        if total > 1 or notes:
            summary = t(
                "job.summary",
                icon="✅" if succeeded else "❌",
                succeeded=succeeded,
                total=total,
            )
            if notes:
                summary += "\n" + "\n".join(notes)
            await reporter.close(summary)
        elif not succeeded:
            await reporter.close(t("job.nothing_delivered"))

        await self._db.finish_job(
            job.id, status,
            error="; ".join(f"{message_id}: {exc}" for message_id, exc in failures[:3]) or None,
        )


def _forward_origin(message) -> str:
    """Name the chat a forwarded message came from, when Telegram reveals it."""
    forward = getattr(message, "forward", None)
    if forward is not None:
        for attribute in ("chat", "sender"):
            origin = getattr(forward, attribute, None)
            if origin is not None:
                return _chat_title(origin)
        name = getattr(forward, "from_name", None)
        if name:
            return str(name)
    return "direct"


def _chat_title(entity) -> str:
    """Best available human name for a chat."""
    for attribute in ("title", "username", "first_name"):
        value = getattr(entity, attribute, None)
        if value:
            return str(value)
    return str(getattr(entity, "id", "chat"))


def _build_caption(message, chat_title: str) -> str:
    """Caption for a re-uploaded file: the original text plus its source."""
    text = (getattr(message, "message", "") or "").strip()
    source = f"<i>{escape_html(chat_title)} · {message.id}</i>"
    if not text:
        return source
    # Leave room for the source line inside Telegram's caption limit.
    body = escape_html(truncate(text, 900))
    return f"{body}\n\n{source}"
