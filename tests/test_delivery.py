"""Delivery: the upload cache, Telegram's size limit, and the PikPak hand-off.

The end-to-end paths run through the queue in test_tasks.py. This file pins
the edges of each destination that are awkward to reach from there.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pikpakapi import DownloadStatus

from tgmd.config import Config, DeliveryConfig
from tgmd.db import Database
from tgmd.delivery import Delivery, DeliveryError, TooLargeToUpload
from tgmd.downloader import MediaInfo
from tgmd.pikpak import OfflineTask, PikPakError

CACHE_CHAT = -100555


class FakeBot:
    def __init__(self, *, cached=None, fail_get: bool = False) -> None:
        self.cached = cached
        self.fail_get = fail_get
        self.sent: list[tuple[int, object]] = []

    async def get_messages(self, chat_id, ids):
        if self.fail_get:
            raise RuntimeError("message deleted")
        return self.cached

    async def send_file(self, chat_id, file, **_kwargs):
        self.sent.append((chat_id, file))
        return SimpleNamespace(id=900)


class FakePikPak:
    def __init__(
        self, *, available=True, status=DownloadStatus.done, error=None, message=""
    ) -> None:
        self.available = available
        self.status = status
        self.error = error
        self.message = message

    async def available_for(self, user_id):
        return self.available

    async def offline_download(self, url, *, folder=None, name=None, user_id=None):
        if self.error is not None:
            raise self.error
        return OfflineTask(task_id="t", file_id="f", name=name or "a <b> & c.iso")

    async def wait_for_task(self, task, *, timeout=None, user_id=None):
        task.message = self.message
        return self.status


class FakeFiles:
    def __init__(self, *, usable: bool = True) -> None:
        self.usable = usable
        self.live: set[Path] = set()
        self.marked: list[Path] = []
        self.streams: dict[str, object] = {}

    def publish_stream(self, opener, *, name, size, ttl=None):
        self.streams["s1"] = (opener, name, size)
        return "s1", "https://media.example.com/s/x/y"

    def unpublish_stream(self, stream_id):
        self.streams.pop(stream_id, None)

    def publish(self, path, *, name=None, ttl=None):
        self.live.add(path)
        return "https://media.example.com/f/x/y"

    def unpublish_all(self, path):
        self.live.discard(path)

    def delete_on_expiry(self, path):
        self.marked.append(path)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "t.sqlite3")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def sample(tmp_path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * 2048)
    return path


INFO = MediaInfo(file_name="clip.mp4", size=2048, is_video=True)


def make(db, *, bot=None, pikpak=None, files=None, **delivery) -> Delivery:
    config = Config(delivery=DeliveryConfig(**delivery))
    return Delivery(bot or FakeBot(), config, db, pikpak or FakePikPak(), files or FakeFiles())


class TestUploadCache:
    async def test_disabled_without_a_cache_chat(self, db):
        assert not await make(db).send_from_cache(1, "k", "caption")

    async def test_a_miss_means_download(self, db):
        assert not await make(db, cache_chat_id=CACHE_CHAT).send_from_cache(1, "k", "c")

    async def test_a_hit_resends_the_stored_media(self, db):
        await db.cache_store("k", CACHE_CHAT, 5, "clip.mp4", 1)
        bot = FakeBot(cached=SimpleNamespace(media="the-media"))
        assert await make(db, bot=bot, cache_chat_id=CACHE_CHAT).send_from_cache(1, "k", "c")
        assert bot.sent == [(1, "the-media")]

    async def test_a_dead_entry_is_forgotten(self, db):
        await db.cache_store("k", CACHE_CHAT, 5, "clip.mp4", 1)
        delivery = make(db, bot=FakeBot(fail_get=True), cache_chat_id=CACHE_CHAT)
        assert not await delivery.send_from_cache(1, "k", "c")
        assert await db.cache_lookup("k") is None


class TestTelegram:
    async def test_over_the_limit_asks_the_caller_to_fall_back(self, db, sample):
        with pytest.raises(TooLargeToUpload):
            await make(db, max_upload_size_mb=0).to_telegram(1, sample, INFO)

    async def test_a_normal_upload_is_sent(self, db, sample):
        bot = FakeBot()
        result = await make(db, bot=bot).to_telegram(1, sample, INFO)
        assert bot.sent == [(1, str(sample))]
        assert not result.kept_local


class TestLocal:
    async def test_the_path_is_escaped_for_html(self, db, tmp_path):
        path = tmp_path / "a & b" / "x<y>.mp4"
        path.parent.mkdir()
        path.write_bytes(b"1")
        result = await make(db).to_local(path, INFO)
        assert "a &amp; b" in result.summary
        assert "x&lt;y&gt;.mp4" in result.summary
        assert result.kept_local


class TestPikPak:
    async def test_no_account_is_refused_before_publishing(self, db, sample):
        files = FakeFiles()
        with pytest.raises(DeliveryError, match="/pikpak login"):
            await make(db, pikpak=FakePikPak(available=False), files=files).to_pikpak(
                sample, INFO, user_id=1
            )
        assert files.live == set()

    async def test_no_file_server_is_explained(self, db, sample):
        with pytest.raises(DeliveryError, match="HTTP file server"):
            await make(db, files=FakeFiles(usable=False)).to_pikpak(sample, INFO)

    async def test_done_unpublishes_and_releases_the_file(self, db, sample):
        files = FakeFiles()
        result = await make(db, files=files).to_pikpak(sample, INFO)
        assert files.live == set()
        assert not result.kept_local

    async def test_a_pikpak_error_unpublishes(self, db, sample):
        files = FakeFiles()
        pikpak = FakePikPak(error=PikPakError("quota exceeded"))
        with pytest.raises(DeliveryError, match="quota exceeded"):
            await make(db, pikpak=pikpak, files=files).to_pikpak(sample, INFO)
        assert files.live == set()

    async def test_pikpaks_reason_for_a_failure_reaches_the_user(self, db, sample):
        files = FakeFiles()
        pikpak = FakePikPak(status=DownloadStatus.error, message="URL <expired>")
        with pytest.raises(DeliveryError, match="URL &lt;expired&gt;"):
            await make(db, pikpak=pikpak, files=files).to_pikpak(sample, INFO)
        assert files.live == set()

    async def test_no_task_is_a_failure_not_still_fetching(self, db, sample):
        # Seen on the NAS: nothing was fetching, yet the user was told the
        # file would "appear in your drive shortly".
        files = FakeFiles()
        pikpak = FakePikPak(status=DownloadStatus.not_found)
        with pytest.raises(DeliveryError, match="no download task"):
            await make(db, pikpak=pikpak, files=files).to_pikpak(sample, INFO)
        assert files.live == set()

    async def test_still_running_stays_served(self, db, sample):
        files = FakeFiles()
        pikpak = FakePikPak(status=DownloadStatus.downloading)
        result = await make(db, pikpak=pikpak, files=files).to_pikpak(sample, INFO)
        assert files.live == {sample}
        assert result.kept_local
        # Nobody asked for it to be deleted, so it is not marked.
        assert files.marked == []

    async def test_still_running_can_be_deleted_once_its_url_expires(self, db, sample):
        files = FakeFiles()
        pikpak = FakePikPak(status=DownloadStatus.downloading)
        await make(db, pikpak=pikpak, files=files).to_pikpak(
            sample, INFO, delete_when_done=True
        )
        assert files.marked == [sample]

    async def test_a_url_transfer_escapes_the_task_name(self, db):
        result = await make(db).url_to_pikpak("magnet:?xt=urn:btih:abc")
        assert "a &lt;b&gt; &amp; c.iso" in result.summary


class TestPikPakStream:
    """PIKPAK_STREAM: the same hand-off, served from Telegram instead of disk."""

    async def stream(self, db, **pikpak):
        files = FakeFiles()
        delivery = make(db, pikpak=FakePikPak(**pikpak), files=files)
        result = await delivery.stream_to_pikpak(
            lambda start, end: None, INFO, size=2048, user_id=1
        )
        return result, files

    async def test_done_stops_serving_and_keeps_nothing(self, db):
        result, files = await self.stream(db)
        assert files.streams == {}
        assert not result.kept_local
        assert "saved to PikPak" in result.summary

    async def test_still_running_keeps_serving_until_the_url_expires(self, db):
        result, files = await self.stream(db, status=DownloadStatus.downloading)
        assert "s1" in files.streams
        assert not result.kept_local  # nothing on disk to keep

    async def test_a_pikpak_error_stops_serving(self, db):
        with pytest.raises(DeliveryError):
            await self.stream(db, error=PikPakError("quota exceeded"))

    async def test_no_account_is_refused_before_serving(self, db):
        files = FakeFiles()
        delivery = make(db, pikpak=FakePikPak(available=False), files=files)
        with pytest.raises(DeliveryError, match="/pikpak login"):
            await delivery.stream_to_pikpak(lambda s, e: None, INFO, size=1, user_id=1)
        assert files.streams == {}
