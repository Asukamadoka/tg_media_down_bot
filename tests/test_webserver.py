"""The HTTP file server, driven over a real socket.

These tests matter more than their size suggests: this server is the one
component that is reachable from the internet, so "only a valid, unexpired
token gets a file" has to hold.
"""

from __future__ import annotations

import asyncio
import socket
import time

import aiohttp
import pytest

from tgmd.config import HttpConfig
from tgmd.signing import make_token
from tgmd.webserver import (
    STREAM_CONCURRENCY,
    FileServer,
    RangeNotSatisfiable,
    content_disposition,
    parse_range,
)

SECRET = "server-test-secret"
CONTENT = b"the quick brown fox" * 100


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
async def server(tmp_path):
    port = free_port()
    config = HttpConfig(
        enabled=True,
        host="127.0.0.1",
        port=port,
        public_base_url=f"http://127.0.0.1:{port}",
        url_ttl=60,
    )
    instance = FileServer(config, SECRET)
    await instance.start()
    yield instance
    await instance.stop()


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(CONTENT)
    return path


async def fetch(url: str) -> tuple[int, bytes]:
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, await response.read()


class TestServing:
    async def test_published_file_is_served(self, server, sample):
        url = server.publish(sample)
        status, body = await fetch(url)
        assert status == 200
        assert body == CONTENT

    async def test_url_carries_the_public_base_and_name(self, server, sample):
        url = server.publish(sample)
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith("/clip.mp4")

    async def test_display_name_can_be_overridden(self, server, sample):
        url = server.publish(sample, name="renamed.mkv")
        assert url.endswith("/renamed.mkv")
        status, body = await fetch(url)
        assert status == 200 and body == CONTENT

    async def test_content_disposition_names_the_file(self, server, sample):
        url = server.publish(sample, name="named.mp4")
        async with aiohttp.ClientSession() as session, session.get(url) as response:
            assert "named.mp4" in response.headers["Content-Disposition"]

    async def test_range_requests_work(self, server, sample):
        url = server.publish(sample)
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, headers={"Range": "bytes=0-9"}) as response,
        ):
            assert response.status == 206
            assert await response.read() == CONTENT[:10]

    async def test_health_endpoint(self, server, sample):
        server.publish(sample)
        base = server.base_url
        status, body = await fetch(f"{base}/healthz")
        assert status == 200
        assert b'"ok"' in body

    async def test_two_publishes_get_different_urls(self, server, sample):
        assert server.publish(sample) != server.publish(sample)


class TestRejections:
    async def test_unsigned_token_is_not_found(self, server, sample):
        server.publish(sample)
        base = server.base_url
        status, _ = await fetch(f"{base}/f/forged-token/clip.mp4")
        assert status == 404

    async def test_token_signed_with_another_secret_is_refused(self, server, sample):
        server.publish(sample)
        base = server.base_url
        forged = make_token("wrong-secret", "whatever", int(time.time()) + 60)
        status, _ = await fetch(f"{base}/f/{forged}/clip.mp4")
        assert status == 404

    async def test_valid_signature_for_an_unknown_file_is_refused(self, server):
        base = server.base_url
        token = make_token(SECRET, "never-registered", int(time.time()) + 60)
        status, _ = await fetch(f"{base}/f/{token}/clip.mp4")
        assert status == 404

    async def test_expired_token_is_refused(self, server, sample):
        url = server.publish(sample, ttl=-5)
        status, _ = await fetch(url)
        assert status == 404

    async def test_unpublished_file_is_no_longer_served(self, server, sample):
        url = server.publish(sample)
        server.unpublish_all(sample)
        status, _ = await fetch(url)
        assert status == 404

    async def test_deleted_file_is_reported_missing(self, server, sample):
        url = server.publish(sample)
        sample.unlink()
        status, _ = await fetch(url)
        assert status == 404


class TestDisabledServer:
    async def test_disabled_server_does_not_bind(self, sample):
        instance = FileServer(HttpConfig(enabled=False), SECRET)
        await instance.start()
        try:
            assert not instance.usable
            with pytest.raises(RuntimeError, match="not running"):
                instance.publish(sample)
        finally:
            await instance.stop()

    async def test_enabled_without_public_url_is_not_usable(self, sample):
        port = free_port()
        instance = FileServer(
            HttpConfig(enabled=True, host="127.0.0.1", port=port), SECRET
        )
        await instance.start()
        try:
            assert not instance.usable
            with pytest.raises(RuntimeError):
                instance.publish(sample)
        finally:
            await instance.stop()


class TestExpiry:
    """A file PikPak is still fetching is deleted once its URL has expired."""

    async def test_a_marked_file_is_deleted_when_it_expires(self, server, sample):
        server.publish(sample, ttl=60)
        server.delete_on_expiry(sample)
        assert server.sweep(now=time.time() + 30) == 0
        assert sample.exists()
        assert server.sweep(now=time.time() + 120) == 1
        assert not sample.exists()

    async def test_an_unmarked_file_is_only_unregistered(self, server, sample):
        # Local mode and the too-large fallback keep their files on purpose.
        server.publish(sample, ttl=60)
        server.sweep(now=time.time() + 120)
        assert sample.exists()

    async def test_a_file_still_served_elsewhere_is_kept(self, server, sample):
        server.publish(sample, ttl=60)
        server.publish(sample, ttl=600)
        server.delete_on_expiry(sample)
        server.sweep(now=time.time() + 120)
        assert sample.exists()
        server.sweep(now=time.time() + 1200)
        assert not sample.exists()

    async def test_fetching_an_expired_url_leaves_the_deletion_to_the_sweep(
        self, server, sample
    ):
        url = server.publish(sample, ttl=1)
        server.delete_on_expiry(sample)
        time.sleep(1.1)
        assert (await fetch(url))[0] == 404
        server.sweep()
        assert not sample.exists()


class TestContentDisposition:
    def test_an_ascii_name_is_plain(self):
        header = content_disposition("clip.mp4")
        assert 'filename="clip.mp4"' in header
        assert "filename*=UTF-8''clip.mp4" in header

    def test_a_chinese_name_is_encoded_not_sent_raw(self):
        header = content_disposition("视频 1.mp4")
        header.encode("ascii")  # a raw UTF-8 header is what RFC 6266 forbids
        assert "filename*=UTF-8''%E8%A7%86%E9%A2%91%201.mp4" in header

    def test_quotes_cannot_break_out_of_the_fallback(self):
        header = content_disposition('a"b.mp4')
        assert 'filename="a_b.mp4"' in header


# ------------------------------------------------ streaming (PIKPAK_STREAM)

STREAMED = bytes(range(256)) * 20  # 5,120 bytes


def opener(content: bytes = STREAMED, *, record: list | None = None, gate=None):
    """A stream opener serving ``content`` in small pieces, like Telegram would."""

    async def open_range(start: int, end: int):
        if record is not None:
            record.append((start, end))
        if gate is not None:
            await gate()
        for offset in range(start, end + 1, 700):
            yield content[offset : min(offset + 700, end + 1)]

    return open_range


async def request(url: str, method: str = "GET", headers=None):
    async with (
        aiohttp.ClientSession() as session,
        session.request(method, url, headers=headers or {}) as response,
    ):
        return response.status, await response.read(), dict(response.headers)


class TestParseRange:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (None, None),
            ("bytes=0-99", (0, 99)),
            ("bytes=100-", (100, 999)),
            ("bytes=-50", (950, 999)),
            ("bytes=900-5000", (900, 999)),  # clipped to the file
            ("bytes=0-1,5-9", None),  # multi-range: served whole
            ("items=0-1", None),
        ],
    )
    def test_accepted(self, header, expected):
        assert parse_range(header, 1000) == expected

    @pytest.mark.parametrize("header", ["bytes=1000-", "bytes=5-2", "bytes=-", "bytes=-0"])
    def test_unsatisfiable(self, header):
        with pytest.raises(RangeNotSatisfiable):
            parse_range(header, 1000)


class TestStreaming:
    async def test_the_whole_file(self, server):
        _, url = server.publish_stream(opener(), name="film.mkv", size=len(STREAMED))
        status, body, headers = await request(url)
        assert status == 200
        assert body == STREAMED
        assert headers["Accept-Ranges"] == "bytes"
        assert int(headers["Content-Length"]) == len(STREAMED)

    async def test_a_range(self, server):
        seen: list = []
        _, url = server.publish_stream(
            opener(record=seen), name="film.mkv", size=len(STREAMED)
        )
        status, body, headers = await request(url, headers={"Range": "bytes=1000-2999"})
        assert status == 206
        assert body == STREAMED[1000:3000]
        assert headers["Content-Range"] == f"bytes 1000-2999/{len(STREAMED)}"
        assert int(headers["Content-Length"]) == 2000
        assert seen == [(1000, 2999)]  # only what was asked for is read

    async def test_the_last_bytes(self, server):
        _, url = server.publish_stream(opener(), name="f", size=len(STREAMED))
        status, body, _ = await request(url, headers={"Range": "bytes=-100"})
        assert status == 206
        assert body == STREAMED[-100:]

    async def test_head_reads_nothing_from_telegram(self, server):
        seen: list = []
        _, url = server.publish_stream(opener(record=seen), name="f", size=len(STREAMED))
        status, body, headers = await request(url, "HEAD")
        assert status == 200
        assert int(headers["Content-Length"]) == len(STREAMED)
        assert body == b""
        assert seen == []

    async def test_an_impossible_range(self, server):
        _, url = server.publish_stream(opener(), name="f", size=len(STREAMED))
        status, _, headers = await request(url, headers={"Range": f"bytes={len(STREAMED)}-"})
        assert status == 416
        assert headers["Content-Range"] == f"bytes */{len(STREAMED)}"

    async def test_unpublished_is_gone(self, server):
        stream_id, url = server.publish_stream(opener(), name="f", size=len(STREAMED))
        server.unpublish_stream(stream_id)
        assert (await request(url))[0] == 404

    async def test_expired_streams_are_swept(self, server):
        stream_id, _ = server.publish_stream(opener(), name="f", size=10, ttl=60)
        server.sweep(now=time.time() + 120)
        assert stream_id not in server._streams  # noqa: SLF001

    async def test_a_file_token_does_not_open_a_stream(self, server, sample):
        file_url = server.publish(sample)
        assert (await request(file_url.replace("/f/", "/s/")))[0] == 404

    async def test_concurrent_pulls_are_bounded(self, server):
        # PikPak may fetch ranges in parallel; each is a read from the user's
        # own Telegram account, so only a few run at once.
        running = 0
        peak = 0
        release = asyncio.Event()

        async def gate():
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await release.wait()
            running -= 1

        _, url = server.publish_stream(opener(gate=gate), name="f", size=len(STREAMED))
        pulls = [
            asyncio.create_task(request(url, headers={"Range": f"bytes={i}-{i + 9}"}))
            for i in range(STREAM_CONCURRENCY + 3)
        ]
        await asyncio.sleep(0.3)
        assert peak == STREAM_CONCURRENCY
        release.set()
        results = await asyncio.gather(*pulls)
        assert all(status == 206 for status, _, _ in results)
