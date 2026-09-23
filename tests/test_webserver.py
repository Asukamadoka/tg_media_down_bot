"""The HTTP file server, driven over a real socket.

These tests matter more than their size suggests: this server is the one
component that is reachable from the internet, so "only a valid, unexpired
token gets a file" has to hold.
"""

from __future__ import annotations

import socket
import time

import aiohttp
import pytest

from tgmd.config import HttpConfig
from tgmd.signing import make_token
from tgmd.webserver import FileServer

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
