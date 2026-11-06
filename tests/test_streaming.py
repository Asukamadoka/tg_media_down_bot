"""PIKPAK_STREAM end to end, over a real socket.

Real file server, real delivery, real ``Downloader.stream``. Only Telegram
(a byte string behind ``iter_download``) and PikPak are fake, and the fake
PikPak fetches the URL it is given the way PikPak might: a HEAD to size the
file, then two ranges at once. What it reassembles must be the file.
"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import aiohttp
import pytest
from pikpakapi import DownloadStatus

from tgmd.config import Config, HttpConfig
from tgmd.db import Database
from tgmd.delivery import Delivery
from tgmd.downloader import Downloader, MediaInfo
from tgmd.pikpak import OfflineTask
from tgmd.webserver import FileServer

CONTENT = bytes(range(251)) * 5000  # 1,255,000 bytes: spans three requests


class Telegram:
    async def iter_download(self, document, *, offset, request_size, limit, file_size):
        for index in range(limit):
            start = offset + index * request_size
            if start >= len(CONTENT):
                return
            yield CONTENT[start : start + request_size]


class FetchingPikPak:
    """Fetches the URL itself, HEAD first, then two halves concurrently."""

    def __init__(self) -> None:
        self.received = b""
        self.head_length = None

    async def available_for(self, user_id):
        return True

    async def offline_download(self, url, *, folder=None, name=None, user_id=None):
        async with aiohttp.ClientSession() as session:
            async with session.head(url) as response:
                self.head_length = int(response.headers["Content-Length"])
            half = self.head_length // 2

            async def get(first, last):
                headers = {"Range": f"bytes={first}-{last}"}
                async with session.get(url, headers=headers) as response:
                    assert response.status == 206
                    return await response.read()

            front, back = await asyncio.gather(
                get(0, half - 1), get(half, self.head_length - 1)
            )
        self.received = front + back
        return OfflineTask(task_id="t", file_id="f", name=name)

    async def wait_for_task(self, task, *, timeout=None, user_id=None):
        return DownloadStatus.done


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
async def server():
    port = free_port()
    http = HttpConfig(
        enabled=True, host="127.0.0.1", port=port, public_base_url=f"http://127.0.0.1:{port}"
    )
    instance = FileServer(http, "secret")
    await instance.start()
    yield instance
    await instance.stop()


async def test_pikpak_gets_the_exact_file_and_nothing_is_written(server, tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    await db.connect()
    try:
        pikpak = FetchingPikPak()
        delivery = Delivery(object(), Config(), db, pikpak, server)
        message = SimpleNamespace(id=1, document=SimpleNamespace(size=len(CONTENT)))
        downloader = Downloader(Telegram())

        result = await delivery.stream_to_pikpak(
            lambda start, end: downloader.stream(message, start, end),
            MediaInfo(file_name="film.mkv", size=len(CONTENT)),
            size=len(CONTENT),
            user_id=1,
        )

        assert pikpak.head_length == len(CONTENT)
        assert pikpak.received == CONTENT
        assert "saved to PikPak" in result.summary
        assert server._streams == {}  # noqa: SLF001 - released once PikPak finished
        assert [p for p in tmp_path.rglob("*") if p.suffix == ".mkv"] == []
    finally:
        await db.close()
