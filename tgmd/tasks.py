"""The job queue: one worker pool that turns links into delivered files."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from telethon import TelegramClient
from telethon.utils import get_peer_id

from .config import Config
from .db import Database, cache_key
from .delivery import Delivery, DeliveryError, TooLargeToUpload
from .downloader import (
    DownloadCancelled,
    DownloadError,
    Downloader,
    RateTracker,
    describe_media,
    has_downloadable_media,
)
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


class JobKind(str, Enum):
    MESSAGE = "message"
    """A Telegram message link: download, then deliver."""

    URL = "url"
    """A magnet link or direct URL: PikPak fetches it itself."""

    SHARE = "share"
    """A PikPak share link: save it into the account."""

    INBOUND = "inbound"
    """Media sent or forwarded directly to the bot, downloaded by the bot."""


class JobState(str, Enum):
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

    @property
    def active(self) -> bool:
        return self.state in (JobState.QUEUED, JobState.RUNNING)


class QueueFull(RuntimeError):
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
    ) -> None:
        self._config = config
        self._db = db
        self._bot = bot
        self._resolver = resolver
        self._downloader = downloader
        self._bot_downloader = bot_downloader
        self._delivery = delivery
        self._pikpak = pikpak
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

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._workers.clear()

    # ------------------------------------------------------------ submission

    def pending_for(self, user_id: int) -> list[Job]:
        return [job for job in self._jobs.values() if job.user_id == user_id and job.active]

    async def submit(self, job: Job) -> int:
        """Queue a job, returning its position in line."""
        limit = self._config.download.max_queue_per_user
        if len(self.pending_for(job.user_id)) >= limit:
            raise QueueFull(
                f"you already have {limit} items queued; wait for them or use /cancel"
            )
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
        if job.kind is JobKind.MESSAGE:
            await self._run_message_job(job, reporter)
        elif job.kind is JobKind.INBOUND:
            await self._run_inbound_job(job, reporter)
        elif job.kind is JobKind.URL:
            await self._run_url_job(job, reporter)
        else:
            await self._run_share_job(job, reporter)

    # -------------------------------------------------------- PikPak-only jobs

    async def _run_url_job(self, job: Job, reporter: Reporter) -> None:
        """Hand a magnet link or direct URL straight to PikPak."""
        await reporter.open(f"⏳ Sending to PikPak: <code>{escape_html(job.label)}</code>")
        if not self._pikpak.configured:
            job.state = JobState.FAILED
            job.detail = "PikPak is not configured"
            await reporter.close(
                "❌ PikPak is not configured, and a magnet link or URL has "
                "nowhere else to go. Set PIKPAK_USERNAME and PIKPAK_PASSWORD."
            )
            await self._db.finish_job(job.id, "failed", error=job.detail)
            return

        try:
            result = await self._delivery.url_to_pikpak(
                job.url or "", folder=job.pikpak_folder
            )
        except (DeliveryError, PikPakError) as exc:
            job.state = JobState.FAILED
            job.detail = str(exc)
            await reporter.close(f"❌ {escape_html(str(exc))}")
            await self._db.finish_job(job.id, "failed", error=str(exc))
            return

        job.state = JobState.DONE
        await reporter.close(f"✅ {result.summary}")
        await self._db.finish_job(job.id, "done", file_name=result.remote_path)

    async def _run_share_job(self, job: Job, reporter: Reporter) -> None:
        """Save a PikPak share link into the account."""
        await reporter.open("⏳ Saving the PikPak share…")
        try:
            names = await self._pikpak.restore_share(job.url or "")
        except PikPakError as exc:
            job.state = JobState.FAILED
            job.detail = str(exc)
            await reporter.close(f"❌ {escape_html(str(exc))}")
            await self._db.finish_job(job.id, "failed", error=str(exc))
            return

        listing = "\n".join(f"• <code>{escape_html(name)}</code>" for name in names[:20])
        if len(names) > 20:
            listing += f"\n… and {len(names) - 20} more"
        job.state = JobState.DONE
        await reporter.close(f"✅ Saved {len(names)} item(s) to PikPak:\n{listing}")
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
            await reporter.open("❌ That message has no media to download.")
            await self._db.finish_job(job.id, "failed", error="no media")
            return

        info = describe_media(message)
        await reporter.open(
            f"⬇️ <code>{escape_html(truncate(info.file_name, 48))}</code> "
            f"({human_size(info.size)})"
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
            )
        except DownloadCancelled:
            job.state = JobState.CANCELLED
            await reporter.close("🚫 Cancelled.")
            await self._db.finish_job(job.id, "cancelled")
            return
        except (DownloadError, DeliveryError) as exc:
            job.state = JobState.FAILED
            job.detail = str(exc)
            await reporter.close(f"❌ {escape_html(str(exc))}")
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
        await reporter.open(f"🔍 Looking up <code>{escape_html(ref.describe())}</code>…")

        try:
            entity, messages = await self._resolver.resolve(ref)
        except ResolveError as exc:
            job.state = JobState.FAILED
            job.detail = str(exc)
            await reporter.close(f"❌ {escape_html(str(exc))}")
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
        failures: list[str] = []

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
                )
                succeeded += 1
            except DownloadCancelled:
                break
            except (DownloadError, DeliveryError, ResolveError) as exc:
                failures.append(f"{message.id}: {exc}")
                log.info("job %d message %s failed: %s", job.id, message.id, exc)
            except Exception as exc:  # unexpected, but one message must not kill the job
                failures.append(f"{message.id}: {exc}")
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
    ) -> None:
        """Download and deliver a single message's media."""
        info = describe_media(message)
        key = cache_key(peer_id, message.id)
        caption = _build_caption(message, chat_title)

        # The cheapest path: a file we have already uploaded once.
        if job.mode == "telegram":
            if await self._delivery.send_from_cache(job.chat_id, key, caption):
                await reporter.update(
                    f"♻️ {prefix}<code>{escape_html(info.file_name)}</code> "
                    "served from cache",
                    force=True,
                )
                return

        relative = build_relative_path(
            self._config.download.filename_template,
            chat=chat_title,
            chat_id=peer_id,
            message_id=message.id,
            name=info.file_name,
            topic_id=job.ref.topic_id if job.ref else None,
            when=getattr(message, "date", None),
        )
        destination = unique_path(self._config.download.dir / relative)

        tracker = RateTracker()
        label = truncate(info.file_name, 48)

        async def on_download(received: int, total: int) -> None:
            fraction = received / total if total else 0.0
            await reporter.update(
                f"⬇️ {prefix}<code>{escape_html(label)}</code>\n"
                f"{progress_bar(fraction)} {fraction * 100:.0f}% "
                f"({human_size(received)} / {human_size(total)})\n"
                f"{human_rate(tracker.rate(received))} · "
                f"ETA {human_duration(tracker.eta(received, total))}"
            )

        await reporter.update(
            f"⬇️ {prefix}<code>{escape_html(label)}</code> "
            f"({human_size(info.size)})",
            force=True,
        )
        path = await downloader.download(
            message, destination, progress=on_download, cancel=job.cancel
        )

        result = await self._deliver(job, reporter, path, info, caption, key, prefix)

        if self._config.download.delete_after_delivery and not result.kept_local:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.debug("could not remove %s: %s", path, exc)

        await reporter.update(
            f"✅ {prefix}<code>{escape_html(label)}</code> — {result.summary}",
            force=True,
        )

    async def _deliver(self, job, reporter, path: Path, info, caption, key, prefix):
        """Send the downloaded file to wherever the job's mode points."""
        upload_tracker = RateTracker()

        async def on_upload(sent: int, total: int) -> None:
            fraction = sent / total if total else 0.0
            await reporter.update(
                f"⬆️ {prefix}<code>{escape_html(truncate(info.file_name, 48))}</code>\n"
                f"{progress_bar(fraction)} {fraction * 100:.0f}% "
                f"({human_size(sent)} / {human_size(total)})\n"
                f"{human_rate(upload_tracker.rate(sent))}"
            )

        if job.mode == "local":
            return await self._delivery.to_local(path, info)

        if job.mode == "pikpak":
            await reporter.update(
                f"☁️ {prefix}handing <code>{escape_html(info.file_name)}</code> "
                "to PikPak…",
                force=True,
            )
            return await self._delivery.to_pikpak(
                path, info, folder=job.pikpak_folder
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
            result = await self._delivery.to_local(path, info)
            result.summary = f"{exc}; {result.summary}"
            return result

    async def _finalize(
        self,
        job: Job,
        reporter: Reporter,
        succeeded: int,
        skipped: int,
        failures: list[str],
        total: int,
        truncated: bool,
        cap: int,
    ) -> None:
        """Set the job's final state and post a summary when it is worth one."""
        if job.cancel.is_set():
            job.state = JobState.CANCELLED
            await reporter.close(f"🚫 Cancelled after {succeeded} file(s).")
            await self._db.finish_job(job.id, "cancelled")
            return

        notes: list[str] = []
        if skipped:
            notes.append(f"{skipped} message(s) had no media")
        if truncated:
            notes.append(
                f"only the first {cap} message(s) were processed "
                "(download.max_batch)"
            )
        if failures:
            shown = "\n".join(f"• {escape_html(item)}" for item in failures[:5])
            if len(failures) > 5:
                shown += f"\n… and {len(failures) - 5} more"
            notes.append(f"{len(failures)} failed:\n{shown}")

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
            summary = f"{'✅' if succeeded else '❌'} {succeeded}/{total} delivered"
            if notes:
                summary += "\n" + "\n".join(notes)
            await reporter.close(summary)
        elif not succeeded:
            await reporter.close("❌ Nothing was delivered.")

        await self._db.finish_job(
            job.id, status, error="; ".join(failures[:3]) or None
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
