"""The job queue, end to end against fake clients.

Everything here is asserted at the level a user or an operator would notice:
what the chat ends up saying, what the job history records, and what is
left on disk. None of it depends on how the queue is built inside, because
stage 2 rewrites that (forwarding, parallel download, routing) and these
tests are what tells it whether the old behaviour survived.

Real: the database, the job queue and its workers, the delivery layer, the
progress reporter. Fake: the Telegram clients, the resolver and downloader
that sit on top of them, PikPak and the HTTP file server.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pikpakapi import DownloadStatus

from tgmd.config import Config, DeliveryConfig, DownloadConfig, PikPakConfig
from tgmd.db import Database, cache_key
from tgmd.delivery import Delivery
from tgmd.downloader import DownloadCancelled, DownloadError
from tgmd.forwarder import Forwarder
from tgmd.links import MessageRef
from tgmd.pikpak import OfflineTask, PikPakError
from tgmd.resolver import ResolveError
from tgmd.tasks import Job, JobKind, JobQueue, JobState, QueueFull

USER = 7
CHAT_ID = 1234
CACHE_CHAT = -100999


# ------------------------------------------------------------------- fakes


class FakeStatusMessage:
    """The progress message the reporter posts and then keeps editing."""

    def __init__(self, text: str) -> None:
        self.id = 1
        self.texts = [text]

    async def edit(self, text: str, **_kwargs) -> None:
        self.texts.append(text)

    @property
    def text(self) -> str:
        return self.texts[-1]


class FakeBot:
    def __init__(self) -> None:
        self.status: list[FakeStatusMessage] = []
        self.sent: list[tuple[int, object, dict]] = []
        self.cached_message = None
        self.upload_error: Exception | None = None

    async def send_message(self, chat_id, text, **_kwargs):
        message = FakeStatusMessage(text)
        self.status.append(message)
        return message

    async def send_file(self, chat_id, file, **kwargs):
        if self.upload_error is not None and chat_id != CACHE_CHAT:
            raise self.upload_error
        self.sent.append((chat_id, file, kwargs))
        return SimpleNamespace(id=500 + len(self.sent))

    async def get_messages(self, chat_id, ids):
        return self.cached_message

    @property
    def last_status(self) -> str:
        return self.status[-1].text if self.status else ""

    def uploads_to(self, chat_id: int) -> list:
        return [file for target, file, _ in self.sent if target == chat_id]


class FakeResolver:
    def __init__(
        self, messages=(), *, error: Exception | None = None, noforwards: bool = False
    ) -> None:
        self.entity = SimpleNamespace(id=CHAT_ID, title="Some Channel", noforwards=noforwards)
        self.messages = list(messages)
        self.error = error

    async def resolve(self, ref):
        if self.error is not None:
            raise self.error
        return self.entity, list(self.messages)


class FakeDownloader:
    """Writes a small file where the real one would, or fails on cue."""

    def __init__(self, *, fail: dict[int, Exception] | None = None, block: bool = False):
        self.fail = fail or {}
        self.block = block
        self.downloaded: list[int] = []
        self.started = asyncio.Event()

    async def download(self, message, destination: Path, *, progress=None, cancel=None):
        self.started.set()
        if message.id in self.fail:
            raise self.fail[message.id]
        if self.block:
            await cancel.wait()
            raise DownloadCancelled()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * 64)
        if progress is not None:
            await progress(64, 64)
        self.downloaded.append(message.id)
        return destination


class FakePikPak:
    def __init__(self, *, available: bool = True, status=DownloadStatus.done) -> None:
        self.available = available
        self.status = status
        self.offline: list[tuple[str, str | None]] = []
        self.shares: list[str] = []

    async def available_for(self, user_id: int) -> bool:
        return self.available

    async def offline_download(self, url, *, folder=None, name=None, user_id=None):
        self.offline.append((url, name))
        return OfflineTask(task_id="t", file_id="f", name=name or "from-url.bin")

    async def wait_for_task(self, task, *, timeout=None, user_id=None):
        return self.status

    async def restore_share(self, url, *, pass_code=None, user_id=None):
        if "broken" in url:
            raise PikPakError("the share link is not usable")
        self.shares.append(url)
        return ["one.mkv", "two.mkv"]


class FakeFileServer:
    def __init__(self) -> None:
        self.usable = True
        self.published: list[Path] = []
        self.marked: list[Path] = []
        self.streams: list[tuple[str, int]] = []

    def publish_stream(self, opener, *, name, size, ttl=None):
        self.streams.append((name, size))
        return f"s{len(self.streams)}", f"https://media.example.com/s/token/{name}"

    def unpublish_stream(self, stream_id: str) -> None:
        pass

    def publish(self, path: Path, *, name=None, ttl=None) -> str:
        self.published.append(path)
        return f"https://media.example.com/f/token/{name}"

    def unpublish_all(self, path: Path) -> None:
        pass

    def delete_on_expiry(self, path: Path) -> None:
        self.marked.append(path)


def media_message(message_id: int, name: str = "clip.mp4", *, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        media=object(),
        file=SimpleNamespace(
            name=name, size=64, mime_type="video/mp4", duration=None, width=None, height=None
        ),
        document=None,
        photo=None,
        message=text,
        date=None,
    )


def text_message(message_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=message_id, media=None, message="just words")


# ----------------------------------------------------------------- harness

_NO_FORWARDER = object()


class Harness:
    """A running queue plus handles on everything the tests look at."""

    def __init__(self, tmp_path: Path, db: Database, **options) -> None:
        self.db = db
        self.bot = options.pop("bot", None) or FakeBot()
        self.resolver = options.pop("resolver", FakeResolver([media_message(1)]))
        self.downloader = options.pop("downloader", FakeDownloader())
        self.bot_downloader = options.pop("bot_downloader", FakeDownloader())
        self.pikpak = options.pop("pikpak", FakePikPak())
        # Absent: no forwarder at all. Present: the reading account it uses,
        # None meaning the bot reads for itself.
        forward_with = options.pop("forward_with", _NO_FORWARDER)
        after_pikpak = options.pop("after_pikpak", None)
        self.files = FakeFileServer()
        self.config = Config(
            download=DownloadConfig(
                dir=tmp_path / "downloads",
                data_dir=tmp_path / "data",
                concurrent=options.pop("concurrent", 1),
                max_queue_per_user=options.pop("max_queue", 5),
                max_batch=options.pop("max_batch", 10),
                progress_interval=1,
                delete_after_delivery=options.pop("delete_after_delivery", True),
                media_dir=options.pop("media_dir", None),
                local_url_prefix=options.pop("local_url_prefix", ""),
            ),
            delivery=DeliveryConfig(
                max_upload_size_mb=options.pop("max_upload_mb", 2000),
                cache_chat_id=options.pop("cache_chat_id", None),
            ),
            pikpak=PikPakConfig(stream=options.pop("pikpak_stream", False)),
        )
        assert not options, f"unknown options: {options}"
        self.delivery = Delivery(self.bot, self.config, db, self.pikpak, self.files)
        self.queue = JobQueue(
            config=self.config,
            db=db,
            bot=self.bot,
            resolver=self.resolver,
            downloader=self.downloader,
            bot_downloader=self.bot_downloader,
            delivery=self.delivery,
            pikpak=self.pikpak,
            forwarder=(
                None
                if forward_with is _NO_FORWARDER
                else Forwarder(self.bot, self.config, db, reader=lambda: forward_with)
            ),
            after_pikpak=after_pikpak,
        )

    @property
    def download_dir(self) -> Path:
        return self.config.download.dir

    def files_on_disk(self) -> list[Path]:
        if not self.download_dir.exists():
            return []
        return [path for path in self.download_dir.rglob("*") if path.is_file()]

    async def job(self, mode: str = "telegram", kind: JobKind = JobKind.MESSAGE, **fields):
        link = fields.pop("url", None) or "https://t.me/somechannel/1"
        job_id = await self.db.record_job(USER, link, mode)
        return Job(
            id=job_id,
            user_id=USER,
            chat_id=USER,
            mode=mode,
            kind=kind,
            label=link,
            ref=fields.pop("ref", MessageRef(chat="somechannel", ids=(1,))),
            url=link if kind in (JobKind.URL, JobKind.SHARE) else None,
            reply_to=1,
            **fields,
        )

    async def run(self, job: Job) -> dict:
        """Submit one job, wait for the queue to drain, return its history row."""
        await self.queue.submit(job)
        await self.drain()
        return await self.history(job.id)

    async def drain(self) -> None:
        await asyncio.wait_for(self.queue._queue.join(), timeout=5)  # noqa: SLF001

    async def history(self, job_id: int) -> dict:
        rows = await self.db.recent_jobs(USER, limit=50)
        return next(row for row in rows if row["id"] == job_id)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "data" / "tgmd.sqlite3")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def make(tmp_path, db, monkeypatch):
    """Build started harnesses; every one is stopped at teardown."""
    # get_peer_id wants a real TL object. The id only feeds the cache key.
    monkeypatch.setattr("tgmd.tasks.get_peer_id", lambda entity: entity.id)
    started: list[Harness] = []

    async def factory(**options) -> Harness:
        harness = Harness(tmp_path, db, **options)
        await harness.queue.start()
        started.append(harness)
        return harness

    yield factory
    for harness in started:
        await harness.queue.stop()


# ------------------------------------------------------------------- tests


class TestTelegramMode:
    async def test_the_file_is_sent_back_and_not_kept(self, make):
        harness = await make()
        row = await harness.run(await harness.job("telegram"))
        assert row["status"] == "done"
        assert len(harness.bot.uploads_to(USER)) == 1
        assert harness.files_on_disk() == []
        assert harness.bot.last_status.startswith("✅")

    async def test_the_caption_carries_the_source(self, make):
        harness = await make(resolver=FakeResolver([media_message(1, text="hello <world>")]))
        await harness.run(await harness.job("telegram"))
        (_, _, kwargs) = harness.bot.sent[0]
        assert "hello &lt;world&gt;" in kwargs["caption"]
        assert "Some Channel" in kwargs["caption"]

    async def test_a_cache_hit_skips_the_download(self, make, db):
        harness = await make(cache_chat_id=CACHE_CHAT)
        await db.cache_store(cache_key(CHAT_ID, 1), CACHE_CHAT, 77, "clip.mp4", 64)
        harness.bot.cached_message = SimpleNamespace(media="cached-media")
        row = await harness.run(await harness.job("telegram"))
        assert row["status"] == "done"
        assert harness.downloader.downloaded == []
        assert harness.bot.uploads_to(USER) == ["cached-media"]

    async def test_an_upload_is_stored_in_the_cache_for_next_time(self, make, db):
        harness = await make(cache_chat_id=CACHE_CHAT)
        await harness.run(await harness.job("telegram"))
        assert len(harness.bot.uploads_to(CACHE_CHAT)) == 1
        assert await db.cache_lookup(cache_key(CHAT_ID, 1)) is not None

    async def test_too_large_to_upload_falls_back_to_keeping_it(self, make):
        # Losing a download that already cost the bandwidth is worse than
        # leaving it on disk and saying why.
        harness = await make(max_upload_mb=0)
        row = await harness.run(await harness.job("telegram"))
        assert row["status"] == "done"
        assert harness.bot.uploads_to(USER) == []
        assert len(harness.files_on_disk()) == 1
        assert "saved to" in harness.bot.last_status

    async def test_a_failed_upload_does_not_leave_the_file_behind(self, make):
        harness = await make()
        harness.bot.upload_error = RuntimeError("connection reset")
        row = await harness.run(await harness.job("telegram"))
        assert row["status"] == "failed"
        assert "connection reset" in row["error"]
        assert harness.files_on_disk() == []

    async def test_files_are_kept_when_the_operator_says_so(self, make):
        harness = await make(delete_after_delivery=False)
        await harness.run(await harness.job("telegram"))
        assert len(harness.files_on_disk()) == 1


class TestLocalMode:
    async def test_the_file_stays_and_the_user_is_told_where(self, make):
        harness = await make()
        row = await harness.run(await harness.job("local"))
        assert row["status"] == "done"
        assert len(harness.files_on_disk()) == 1
        assert harness.bot.uploads_to(USER) == []
        assert "saved to" in harness.bot.last_status

    async def test_an_ampersand_in_the_name_is_escaped(self, make):
        harness = await make(resolver=FakeResolver([media_message(1, "Tom & Jerry.mp4")]))
        await harness.run(await harness.job("local"))
        assert "Tom &amp; Jerry.mp4" in harness.bot.last_status
        assert "Tom & Jerry" not in harness.bot.last_status


class TestPikPakMode:
    async def test_a_finished_transfer_removes_the_local_copy(self, make):
        harness = await make()
        row = await harness.run(await harness.job("pikpak"))
        assert row["status"] == "done"
        assert harness.pikpak.offline[0][1] == "clip.mp4"
        assert harness.files_on_disk() == []

    async def test_the_final_message_survives_an_ampersand(self, make):
        # An unescaped "&" made Telegram reject the final edit, and the status
        # stayed on "handing to PikPak" forever.
        harness = await make(resolver=FakeResolver([media_message(1, "Tom & Jerry.mp4")]))
        await harness.run(await harness.job("pikpak"))
        assert "Tom &amp; Jerry.mp4" in harness.bot.last_status

    async def test_a_transfer_still_running_hands_the_file_to_the_server(self, make):
        # It must stay served, so it cannot be deleted now; the file server
        # deletes it when the URL expires instead.
        harness = await make(pikpak=FakePikPak(status=DownloadStatus.downloading))
        row = await harness.run(await harness.job("pikpak"))
        assert row["status"] == "done"
        on_disk = harness.files_on_disk()
        assert len(on_disk) == 1
        assert harness.files.marked == on_disk

    async def test_a_rejected_transfer_is_a_failure_and_cleans_up(self, make):
        harness = await make(pikpak=FakePikPak(status=DownloadStatus.error))
        row = await harness.run(await harness.job("pikpak"))
        assert row["status"] == "failed"
        assert harness.files_on_disk() == []

    async def test_no_account_is_a_failure(self, make):
        harness = await make(pikpak=FakePikPak(available=False))
        row = await harness.run(await harness.job("pikpak"))
        assert row["status"] == "failed"
        assert "/pikpak login" in row["error"]


class TestBatches:
    async def test_one_bad_message_does_not_sink_the_rest(self, make):
        harness = await make(
            resolver=FakeResolver([media_message(1), media_message(2), media_message(3)]),
            downloader=FakeDownloader(fail={2: DownloadError("file reference expired")}),
        )
        row = await harness.run(await harness.job("local"))
        assert row["status"] == "partial"
        assert harness.downloader.downloaded == [1, 3]
        assert "2: file reference expired" in row["error"]
        assert "2/3" in harness.bot.last_status

    async def test_messages_without_media_are_skipped(self, make):
        harness = await make(resolver=FakeResolver([text_message(1), media_message(2)]))
        row = await harness.run(await harness.job("local"))
        assert row["status"] == "done"
        assert harness.downloader.downloaded == [2]

    async def test_nothing_to_download_is_a_failure(self, make):
        harness = await make(resolver=FakeResolver([text_message(1)]))
        row = await harness.run(await harness.job("local"))
        assert row["status"] == "failed"

    async def test_the_batch_cap_is_applied(self, make):
        messages = [media_message(index) for index in range(1, 6)]
        harness = await make(resolver=FakeResolver(messages), max_batch=2)
        await harness.run(await harness.job("local"))
        assert harness.downloader.downloaded == [1, 2]

    async def test_an_unresolvable_link_is_reported(self, make):
        harness = await make(resolver=FakeResolver(error=ResolveError("no chat called @x")))
        row = await harness.run(await harness.job("local"))
        assert row["status"] == "failed"
        assert "no chat called @x" in harness.bot.last_status


class TestRobustness:
    async def test_an_unexpected_error_closes_the_status_and_spares_the_worker(self, make):
        # Before, the status message stayed on "looking up…" forever.
        harness = await make(resolver=FakeResolver(error=RuntimeError("flood wait 900s")))
        row = await harness.run(await harness.job("local"))
        assert row["status"] == "failed"
        assert "flood wait 900s" in harness.bot.last_status
        assert harness.bot.last_status.startswith("❌")

        # The worker is still alive and takes the next job.
        harness.resolver.error = None
        harness.resolver.messages = [media_message(1)]
        assert (await harness.run(await harness.job("local")))["status"] == "done"

    async def test_the_queue_limit_is_per_user(self, make):
        harness = await make(max_queue=1, downloader=FakeDownloader(block=True))
        await harness.queue.submit(await harness.job("local"))
        with pytest.raises(QueueFull):
            await harness.queue.submit(await harness.job("local"))
        harness.queue.cancel(USER)
        await harness.drain()


class TestCancellation:
    async def test_a_running_download_can_be_cancelled(self, make):
        harness = await make(downloader=FakeDownloader(block=True))
        job = await harness.job("local")
        await harness.queue.submit(job)
        await asyncio.wait_for(harness.downloader.started.wait(), timeout=5)

        cancelled = harness.queue.cancel(USER)
        await harness.drain()

        assert [item.id for item in cancelled] == [job.id]
        assert (await harness.history(job.id))["status"] == "cancelled"
        assert job.state is JobState.CANCELLED

    async def test_a_queued_job_is_cancelled_before_it_starts(self, make):
        harness = await make(downloader=FakeDownloader(block=True))
        first = await harness.job("local")
        second = await harness.job("local")
        await harness.queue.submit(first)
        await harness.queue.submit(second)
        await asyncio.wait_for(harness.downloader.started.wait(), timeout=5)

        harness.queue.cancel(USER, second.id)
        assert harness.queue.snapshot(USER) == [first]
        harness.queue.cancel(USER, first.id)
        await harness.drain()
        assert (await harness.history(second.id))["status"] == "cancelled"

    async def test_stop_does_not_swallow_its_callers_cancellation(self, make):
        # stop() waits for workers it has just cancelled. Their cancellation
        # is expected; one aimed at whoever called stop() must still arrive.
        harness = await make()
        release = asyncio.Event()

        async def slow_to_die():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise

        harness.queue._workers.append(asyncio.create_task(slow_to_die()))  # noqa: SLF001
        await asyncio.sleep(0)
        stopper = asyncio.create_task(harness.queue.stop())
        await asyncio.sleep(0.01)
        stopper.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        assert stopper.cancelled()


class TestPikPakOnlyJobs:
    async def test_a_magnet_link_is_handed_over(self, make):
        harness = await make()
        job = await harness.job("pikpak", JobKind.URL, url="magnet:?xt=urn:btih:abc")
        row = await harness.run(job)
        assert row["status"] == "done"
        assert harness.pikpak.offline == [("magnet:?xt=urn:btih:abc", None)]

    async def test_a_magnet_link_without_an_account_fails_clearly(self, make):
        harness = await make(pikpak=FakePikPak(available=False))
        job = await harness.job("pikpak", JobKind.URL, url="magnet:?xt=urn:btih:abc")
        row = await harness.run(job)
        assert row["status"] == "failed"
        assert "/pikpak login" in harness.bot.last_status

    async def test_a_share_link_is_saved(self, make):
        harness = await make()
        job = await harness.job("pikpak", JobKind.SHARE, url="https://mypikpak.com/s/ABC")
        row = await harness.run(job)
        assert row["status"] == "done"
        assert "one.mkv" in harness.bot.last_status

    async def test_a_broken_share_link_fails(self, make):
        harness = await make()
        job = await harness.job("pikpak", JobKind.SHARE, url="https://mypikpak.com/s/broken")
        row = await harness.run(job)
        assert row["status"] == "failed"


class TestInbound:
    async def test_media_sent_to_the_bot_is_downloaded_by_the_bot(self, make):
        harness = await make()
        job = await harness.job("local", JobKind.INBOUND, message=media_message(9))
        row = await harness.run(job)
        assert row["status"] == "done"
        assert harness.bot_downloader.downloaded == [9]
        assert harness.downloader.downloaded == []

    async def test_a_message_without_media_fails(self, make):
        harness = await make()
        job = await harness.job("local", JobKind.INBOUND, message=text_message(9))
        row = await harness.run(job)
        assert row["status"] == "failed"


class FakeReader:
    """The reading account, as far as forwarding goes."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.forwarded: list[int] = []

    async def get_input_entity(self, chat_id):
        return chat_id

    async def forward_messages(self, entity, messages, from_peer=None):
        if self.error is not None:
            raise self.error
        self.forwarded.append(messages)
        return SimpleNamespace(id=7000 + messages)


class ForwardingBot(FakeBot):
    """Sees the copies the reading account forwards into the cache channel."""

    async def get_messages(self, chat_id, ids):
        if chat_id == CACHE_CHAT and ids and ids >= 7000:
            return SimpleNamespace(id=ids, media=f"forwarded-{ids}")
        return await super().get_messages(chat_id, ids)


class TestForwardFastPath:
    """AUDIT/CC_BRIEF 2a: forwardable media is never downloaded."""

    async def make_forwarding(self, make, **options):
        return await make(bot=ForwardingBot(), **options)

    async def test_forwardable_media_is_copied_not_downloaded(self, make):
        reader = FakeReader()
        harness = await self.make_forwarding(
            make, forward_with=reader, cache_chat_id=CACHE_CHAT
        )
        row = await harness.run(await harness.job("telegram"))
        assert row["status"] == "done"
        assert reader.forwarded == [1]
        assert harness.downloader.downloaded == []
        assert harness.bot.uploads_to(USER) == ["forwarded-7001"]
        assert "nothing downloaded" in harness.bot.last_status

    async def test_restricted_media_is_downloaded_instead(self, make):
        reader = FakeReader()
        harness = await self.make_forwarding(
            make,
            forward_with=reader,
            cache_chat_id=CACHE_CHAT,
            resolver=FakeResolver([media_message(1)], noforwards=True),
        )
        row = await harness.run(await harness.job("telegram"))
        assert row["status"] == "done"
        assert reader.forwarded == []
        assert harness.downloader.downloaded == [1]

    async def test_without_a_cache_channel_it_downloads_and_suggests_one(self, make):
        harness = await self.make_forwarding(make, forward_with=FakeReader())
        row = await harness.run(await harness.job("telegram"))
        assert row["status"] == "done"
        assert harness.downloader.downloaded == [1]
        assert "/cache" in harness.bot.last_status

    async def test_a_batch_suggests_it_once(self, make):
        harness = await self.make_forwarding(
            make,
            forward_with=FakeReader(),
            resolver=FakeResolver([media_message(1), media_message(2)]),
        )
        await harness.run(await harness.job("telegram"))
        assert harness.bot.last_status.count("/cache") == 1

    async def test_a_refused_forward_is_downloaded_instead(self, make):
        from telethon.errors import ChatWriteForbiddenError

        harness = await self.make_forwarding(
            make,
            forward_with=FakeReader(error=ChatWriteForbiddenError(request=None)),
            cache_chat_id=CACHE_CHAT,
        )
        row = await harness.run(await harness.job("telegram"))
        assert row["status"] == "done"
        assert harness.downloader.downloaded == [1]
        assert "/cache" not in harness.bot.last_status

    async def test_other_modes_never_forward(self, make):
        # Local and PikPak need the bytes, so there is nothing to skip.
        reader = FakeReader()
        harness = await self.make_forwarding(
            make, forward_with=reader, cache_chat_id=CACHE_CHAT
        )
        await harness.run(await harness.job("local"))
        assert reader.forwarded == []
        assert harness.downloader.downloaded == [1]

    async def test_a_second_request_comes_from_the_cache(self, make):
        reader = FakeReader()
        harness = await self.make_forwarding(
            make, forward_with=reader, cache_chat_id=CACHE_CHAT
        )
        await harness.run(await harness.job("telegram"))
        await harness.run(await harness.job("telegram"))
        assert reader.forwarded == [1]  # the second one never reached the reader
        assert "served from cache" in harness.bot.last_status



class TestAutoMode:
    """CC_BRIEF 2d: forwardable goes back through Telegram, restricted stays."""

    async def test_forwardable_is_copied_back_like_telegram_mode(self, make):
        reader = FakeReader()
        harness = await make(
            bot=ForwardingBot(), forward_with=reader, cache_chat_id=CACHE_CHAT
        )
        row = await harness.run(await harness.job("auto"))
        assert row["status"] == "done"
        assert harness.downloader.downloaded == []
        assert harness.bot.uploads_to(USER) == ["forwarded-7001"]

    async def test_restricted_is_kept_in_the_media_directory(self, make, tmp_path):
        media = tmp_path / "nas-media"
        harness = await make(
            bot=ForwardingBot(),
            forward_with=FakeReader(),
            cache_chat_id=CACHE_CHAT,
            media_dir=media,
            resolver=FakeResolver([media_message(1, "Film.mkv")], noforwards=True),
        )
        row = await harness.run(await harness.job("auto"))
        assert row["status"] == "done"
        # Original name, by chat, and kept despite delete_after_delivery.
        assert (media / "Some Channel" / "Film.mkv").is_file()
        assert harness.bot.uploads_to(USER) == []
        assert harness.files_on_disk() == []  # nothing in the working directory

    async def test_forwardable_without_a_cache_channel_is_still_sent_back(self, make):
        harness = await make(bot=ForwardingBot(), forward_with=FakeReader())
        await harness.run(await harness.job("auto"))
        assert len(harness.bot.uploads_to(USER)) == 1
        assert harness.files_on_disk() == []

    async def test_without_a_forwarder_the_flags_still_decide(self, make, tmp_path):
        media = tmp_path / "m"
        harness = await make(
            media_dir=media,
            resolver=FakeResolver([media_message(1)], noforwards=True),
        )
        await harness.run(await harness.job("auto"))
        assert (media / "Some Channel" / "clip.mp4").is_file()

    async def test_media_sent_to_the_bot_is_kept(self, make, tmp_path):
        media = tmp_path / "m"
        harness = await make(media_dir=media)
        job = await harness.job("auto", JobKind.INBOUND, message=media_message(9, "v.mp4"))
        await harness.run(job)
        assert (media / "direct" / "v.mp4").is_file()


class TestMediaDirectory:
    async def test_local_mode_keeps_the_original_name(self, make, tmp_path):
        media = tmp_path / "media"
        harness = await make(
            media_dir=media, resolver=FakeResolver([media_message(1, "Holiday 2026.mp4")])
        )
        await harness.run(await harness.job("local"))
        assert (media / "Some Channel" / "Holiday 2026.mp4").is_file()

    async def test_a_name_clash_gets_a_number_not_an_overwrite(self, make, tmp_path):
        media = tmp_path / "media"
        harness = await make(
            media_dir=media,
            resolver=FakeResolver([media_message(1, "a.mp4"), media_message(2, "a.mp4")]),
        )
        await harness.run(await harness.job("local"))
        assert sorted(p.name for p in (media / "Some Channel").iterdir()) == [
            "a (1).mp4", "a.mp4",
        ]

    async def test_the_reply_gives_a_path_to_paste(self, make, tmp_path):
        harness = await make(
            media_dir=tmp_path / "media",
            local_url_prefix="smb://10.10.10.2/media/",
            resolver=FakeResolver([media_message(1, "Holiday 2026.mp4")]),
        )
        await harness.run(await harness.job("local"))
        assert (
            "smb://10.10.10.2/media/Some%20Channel/Holiday%202026.mp4"
            in harness.bot.last_status
        )

    async def test_without_media_dir_nothing_moves(self, make):
        # MEDIA_DIR defaults to DOWNLOAD_DIR, so existing deployments keep
        # finding their files where they always were.
        harness = await make()
        await harness.run(await harness.job("local"))
        assert [p.name for p in harness.files_on_disk()] == ["clip.mp4"]

    async def test_local_files_are_never_deleted(self, make, tmp_path):
        harness = await make(media_dir=tmp_path / "m", delete_after_delivery=True)
        await harness.run(await harness.job("local"))
        assert (tmp_path / "m" / "Some Channel" / "clip.mp4").is_file()

    async def test_too_large_to_upload_is_moved_to_the_media_directory(self, make, tmp_path):
        media = tmp_path / "media"
        harness = await make(max_upload_mb=0, media_dir=media)
        row = await harness.run(await harness.job("telegram"))
        assert row["status"] == "done"
        assert (media / "Some Channel" / "clip.mp4").is_file()
        assert harness.files_on_disk() == []



def streamable(message_id: int, name: str = "film.mkv", size: int = 64):
    message = media_message(message_id, name)
    message.document = SimpleNamespace(size=size, dc_id=4, attributes=[])
    return message


class TestPikPakStreaming:
    """CC_BRIEF 2e: with PIKPAK_STREAM on, a Telegram file never touches disk."""

    async def test_a_document_is_streamed_not_downloaded(self, make):
        harness = await make(pikpak_stream=True, resolver=FakeResolver([streamable(1)]))
        row = await harness.run(await harness.job("pikpak"))
        assert row["status"] == "done"
        assert harness.downloader.downloaded == []
        assert harness.files.streams == [("film.mkv", 64)]
        assert harness.files_on_disk() == []

    async def test_it_is_off_by_default(self, make):
        harness = await make(resolver=FakeResolver([streamable(1)]))
        await harness.run(await harness.job("pikpak"))
        assert harness.downloader.downloaded == [1]
        assert harness.files.streams == []

    async def test_a_photo_still_goes_through_the_disk(self, make):
        # No document, no size known up front: the fallback is the old path.
        harness = await make(pikpak_stream=True)
        await harness.run(await harness.job("pikpak"))
        assert harness.downloader.downloaded == [1]
        assert harness.files.streams == []

    async def test_other_modes_are_unaffected(self, make):
        harness = await make(pikpak_stream=True, resolver=FakeResolver([streamable(1)]))
        await harness.run(await harness.job("local"))
        assert harness.downloader.downloaded == [1]
