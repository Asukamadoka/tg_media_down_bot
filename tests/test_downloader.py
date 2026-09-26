"""Media inspection and the download loop.

The inspection values end up on disk and in the re-uploaded file's
attributes. The loop is where retries, flood waits and cancellation live,
and where a failure must never leave half a file on the NAS.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.errors import FileReferenceExpiredError, FloodWaitError
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaWebPage,
)

from tgmd.downloader import (
    DownloadCancelled,
    Downloader,
    DownloadError,
    RateTracker,
    describe_media,
    ensure_disk_space,
    has_downloadable_media,
)

# "Some media is attached." The old default was object(), evaluated once when
# the function was defined, so every call already shared one instance; this
# only names it.
_ANY_MEDIA = object()


def make_message(
    *,
    message_id: int = 7,
    file=None,
    document=None,
    photo=None,
    media=_ANY_MEDIA,
):
    """A stand-in for a Telethon message, carrying only what we read."""
    return SimpleNamespace(
        id=message_id, file=file, document=document, photo=photo, media=media
    )


def make_file(**kwargs):
    defaults = {
        "name": None,
        "size": None,
        "mime_type": None,
        "duration": None,
        "width": None,
        "height": None,
    }
    return SimpleNamespace(**{**defaults, **kwargs})


class TestHasDownloadableMedia:
    def test_no_media(self):
        assert not has_downloadable_media(make_message(media=None))

    def test_link_preview_is_not_media(self):
        message = make_message(media=MessageMediaWebPage(webpage=None))
        assert not has_downloadable_media(message)

    def test_document_is_media(self):
        message = make_message(file=make_file(name="a.mp4", size=10))
        assert has_downloadable_media(message)


class TestDescribeMedia:
    def test_filename_from_the_file_helper(self):
        info = describe_media(
            make_message(file=make_file(name="clip.mp4", size=2048, mime_type="video/mp4"))
        )
        assert info.file_name == "clip.mp4"
        assert info.size == 2048
        assert info.mime_type == "video/mp4"

    def test_filename_from_a_document_attribute(self):
        document = SimpleNamespace(
            attributes=[DocumentAttributeFilename(file_name="from-attr.mkv")]
        )
        info = describe_media(make_message(file=make_file(size=1), document=document))
        assert info.file_name == "from-attr.mkv"

    def test_video_attributes_are_collected(self):
        document = SimpleNamespace(
            attributes=[
                DocumentAttributeFilename(file_name="movie.mp4"),
                DocumentAttributeVideo(
                    duration=123, w=1920, h=1080, supports_streaming=True
                ),
            ]
        )
        info = describe_media(
            make_message(file=make_file(size=99, mime_type="video/mp4"), document=document)
        )
        assert info.is_video
        assert info.duration == 123
        assert (info.width, info.height) == (1920, 1080)
        assert not info.is_round

    def test_round_video_is_flagged(self):
        document = SimpleNamespace(
            attributes=[DocumentAttributeVideo(duration=5, w=240, h=240, round_message=True)]
        )
        info = describe_media(make_message(file=make_file(size=1), document=document))
        assert info.is_video and info.is_round

    def test_voice_note_is_flagged(self):
        document = SimpleNamespace(
            attributes=[DocumentAttributeAudio(duration=9, voice=True)]
        )
        info = describe_media(make_message(file=make_file(size=1), document=document))
        assert info.is_audio and info.is_voice
        assert info.duration == 9

    def test_photo_gets_a_jpg_name(self):
        info = describe_media(
            make_message(message_id=42, photo=SimpleNamespace(id=1), file=make_file())
        )
        assert info.is_photo
        assert info.file_name == "42.jpg"
        assert info.mime_type == "image/jpeg"

    def test_unnamed_document_falls_back_to_the_message_id(self):
        info = describe_media(
            make_message(message_id=55, file=make_file(mime_type="video/mp4", size=1))
        )
        assert info.file_name.startswith("55")
        assert info.file_name.endswith(".mp4")

    def test_unknown_mime_type_gets_a_bin_extension(self):
        info = describe_media(make_message(message_id=8, file=make_file(size=1)))
        assert info.file_name == "8.bin"

    def test_dangerous_filename_becomes_one_harmless_component(self):
        info = describe_media(
            make_message(file=make_file(name="../../etc/passwd", size=1))
        )
        # Separators are what make a name dangerous; dots left inside it are
        # inert, and build_relative_path drops any component that is just "..".
        assert "/" not in info.file_name
        assert "\\" not in info.file_name
        assert info.file_name not in (".", "..")
        assert Path(info.file_name).name == info.file_name

    def test_missing_size_is_none_not_zero(self):
        info = describe_media(make_message(file=make_file(name="a.bin")))
        assert info.size is None


class TestEnsureDiskSpace:
    def test_unknown_size_is_allowed(self, tmp_path):
        ensure_disk_space(tmp_path, None)

    def test_small_file_fits(self, tmp_path):
        ensure_disk_space(tmp_path, 1024)

    def test_creates_the_target_directory(self, tmp_path):
        target = tmp_path / "new" / "deeper"
        ensure_disk_space(target, 1024)
        assert target.is_dir()

    def test_oversized_file_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            shutil, "disk_usage", lambda _: SimpleNamespace(free=1024, total=0, used=0)
        )
        with pytest.raises(DownloadError, match="disk space"):
            ensure_disk_space(tmp_path, 10 * 1024**3)


class TestRateTracker:
    def test_rate_is_positive(self):
        assert RateTracker().rate(1024) > 0

    def test_eta_is_none_without_a_total(self):
        assert RateTracker().eta(100, None) is None

    def test_eta_is_none_when_complete(self):
        assert RateTracker().eta(100, 100) is None

    def test_eta_is_a_number_mid_transfer(self):
        assert RateTracker().eta(50, 100) >= 0


class ScriptedClient:
    """download_media() that plays back a script, writing a partial file
    before each failure the way an interrupted transfer would."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def download_media(self, message, file, progress_callback=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        Path(file).write_bytes(b"partial")
        if progress_callback is not None:
            # Two chunks, as Telethon would report them.
            await progress_callback(7, 100)
            await progress_callback(14, 100)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "ok":
            Path(file).write_bytes(b"complete")
            return file
        return None


@pytest.fixture
def no_sleep(monkeypatch):
    """Skip the back-off and flood-wait sleeps, recording how long they were."""
    slept: list[float] = []

    async def fake_sleep(seconds, *_args):
        slept.append(seconds)

    monkeypatch.setattr("tgmd.downloader.asyncio.sleep", fake_sleep)
    return slept


def target(tmp_path) -> Path:
    return tmp_path / "chat" / "1_clip.mp4"


MESSAGE = make_message(file=make_file(name="clip.mp4", size=100))


class TestDownloadLoop:
    async def test_success_returns_the_written_path(self, tmp_path):
        path = await Downloader(ScriptedClient("ok")).download(MESSAGE, target(tmp_path))
        assert path.read_bytes() == b"complete"

    async def test_a_transient_error_is_retried(self, tmp_path, no_sleep):
        client = ScriptedClient(ConnectionError("reset"), "ok")
        path = await Downloader(client).download(MESSAGE, target(tmp_path))
        assert path.exists()
        assert client.calls == 2

    async def test_repeated_failure_gives_up_and_leaves_nothing(self, tmp_path, no_sleep):
        client = ScriptedClient(*(TimeoutError("slow") for _ in range(3)))
        with pytest.raises(DownloadError, match="after 3 attempts"):
            await Downloader(client).download(MESSAGE, target(tmp_path))
        assert not target(tmp_path).exists()

    async def test_a_short_flood_wait_is_waited_out(self, tmp_path, no_sleep):
        client = ScriptedClient(FloodWaitError(request=None, capture=12), "ok")
        await Downloader(client).download(MESSAGE, target(tmp_path))
        assert 13 in no_sleep

    async def test_a_long_flood_wait_is_reported_not_slept(self, tmp_path, no_sleep):
        # Sleeping for minutes inside a worker is worse than telling the user.
        client = ScriptedClient(FloodWaitError(request=None, capture=3600))
        with pytest.raises(DownloadError, match="3600s"):
            await Downloader(client).download(MESSAGE, target(tmp_path))
        assert no_sleep == []
        assert not target(tmp_path).exists()

    async def test_an_unexpected_error_still_removes_the_partial_file(self, tmp_path):
        # These were not retried, and used to leave the partial file behind.
        client = ScriptedClient(FileReferenceExpiredError(request=None))
        with pytest.raises(FileReferenceExpiredError):
            await Downloader(client).download(MESSAGE, target(tmp_path))
        assert not target(tmp_path).exists()
        assert client.calls == 1

    async def test_cancelling_mid_transfer_removes_the_partial_file(self, tmp_path):
        cancel = asyncio.Event()

        async def cancel_on_first_progress(_received, _total):
            cancel.set()

        client = ScriptedClient("ok")
        with pytest.raises(DownloadCancelled):
            await Downloader(client).download(
                MESSAGE, target(tmp_path), progress=cancel_on_first_progress, cancel=cancel
            )
        assert not target(tmp_path).exists()

    async def test_already_cancelled_never_starts(self, tmp_path):
        cancel = asyncio.Event()
        cancel.set()
        client = ScriptedClient("ok")
        with pytest.raises(DownloadCancelled):
            await Downloader(client).download(MESSAGE, target(tmp_path), cancel=cancel)
        assert client.calls == 0

    async def test_no_file_from_telegram_is_an_error(self, tmp_path):
        with pytest.raises(DownloadError, match="no file"):
            await Downloader(ScriptedClient(None)).download(MESSAGE, target(tmp_path))


class RangeClient:
    """iter_download over a known byte string, enforcing Telegram's rules."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.requests: list[tuple[int, int, int]] = []

    async def iter_download(self, document, *, offset, request_size, limit, file_size):
        # upload.getFile: offset and limit divisible by 4 KiB, no part crossing
        # a 1 MiB boundary. A misaligned stream would be refused by Telegram.
        assert offset % 4096 == 0 and request_size % 4096 == 0
        assert (1024 * 1024) % request_size == 0
        assert offset % request_size == 0
        self.requests.append((offset, request_size, limit))
        for index in range(limit):
            start = offset + index * request_size
            if start >= len(self.content):
                return
            yield self.content[start : start + request_size]


class TestStreaming:
    CONTENT = bytes(range(251)) * 9000  # 2,259,000 bytes: not a round number

    def message(self):
        return SimpleNamespace(
            id=1, document=SimpleNamespace(size=len(self.CONTENT)), media=object()
        )

    async def read(self, start, end):
        client = RangeClient(self.CONTENT)
        chunks = [
            chunk async for chunk in Downloader(client).stream(self.message(), start, end)
        ]
        return b"".join(chunks), client

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            (0, 99),
            (1000, 600_000),  # straddles a 512 KiB boundary
            (524_288, 1_048_575),  # exactly one aligned request
            (2_000_000, 2_258_999),  # the tail, short last request
            (0, 2_258_999),  # everything
        ],
    )
    async def test_any_range_comes_back_exactly(self, start, end):
        body, _ = await self.read(start, end)
        assert body == self.CONTENT[start : end + 1]

    async def test_only_the_needed_requests_are_made(self):
        _, client = await self.read(1000, 600_000)
        assert client.requests == [(0, 512 * 1024, 2)]

    def test_documents_can_be_streamed_photos_cannot(self):
        from tgmd.downloader import can_stream

        assert can_stream(self.message())
        assert not can_stream(SimpleNamespace(document=None, photo=object()))
        assert not can_stream(SimpleNamespace(document=SimpleNamespace(size=0)))
