"""Media inspection: what the bot decides to name, size and re-upload.

These are the values that end up on disk and in the re-uploaded file's
attributes, so they are worth pinning down without a live Telegram account.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaWebPage,
)

from tgmd.downloader import (
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
        assert info.supports_streaming
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
