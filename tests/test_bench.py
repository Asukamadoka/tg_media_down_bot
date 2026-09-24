"""``python -m tgmd.bench``: the measurement tool handed to Cowork."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from tgmd import bench
from tgmd.downloader import Transfer


def args(link="https://t.me/somechannel/42", connections=(1, 4), keep=False):
    return argparse.Namespace(link=link, connections=list(connections), keep=keep)


class TestArguments:
    def test_counts_parse(self):
        assert bench.parse_counts("1,4,8") == [1, 4, 8]

    @pytest.mark.parametrize("text", ["0", "", "1,-2", "1,2,3,4,5,6,7"])
    def test_bad_counts_are_refused(self, text):
        with pytest.raises(argparse.ArgumentTypeError):
            bench.parse_counts(text)

    def test_the_default_compares_one_with_four(self):
        assert bench.build_parser().parse_args(["https://t.me/x/1"]).connections == [1, 4]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "0" * 32)
    monkeypatch.setenv("TG_BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(bench, "load_dotenv", lambda: None)
    return tmp_path


class TestErrors:
    async def test_not_a_link(self, env, capsys):
        assert await bench.run(args(link="hello")) == 2
        assert "not a Telegram message link" in capsys.readouterr().err

    async def test_no_reading_account(self, env, capsys):
        assert await bench.run(args()) == 2
        assert "/setup telegram" in capsys.readouterr().err


class FakeClient:
    def __init__(self, *a, **k) -> None:
        self.disconnected = False

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return True

    async def disconnect(self):
        self.disconnected = True


class FakeResolver:
    def __init__(self, client) -> None:
        pass

    async def resolve(self, ref):
        message = SimpleNamespace(id=42, media=object(), file=SimpleNamespace(name="v.mkv"))
        return SimpleNamespace(id=1), [message]


class FakeDownloader:
    runs: list[int] = []  # noqa: RUF012 - reset by the test

    def __init__(self, client, *, connections: int) -> None:
        self.connections = connections
        self.last = None

    async def download(self, message, target: Path, **_kwargs):
        target.write_bytes(b"x")
        FakeDownloader.runs.append(self.connections)
        self.last = Transfer(
            size=100 * 1024 * 1024, seconds=10.0 / self.connections,
            connections=self.connections, dc_id=4,
        )
        return target


class TestARun:
    async def test_each_count_is_measured_and_cleaned_up(self, env, monkeypatch, capsys):
        FakeDownloader.runs = []
        # A session object, as TG_USER_SESSION or /setup telegram yield.
        monkeypatch.setattr(
            bench, "user_session_source", lambda config, stored: (object(), "env")
        )
        monkeypatch.setattr(bench, "TelegramClient", FakeClient)
        monkeypatch.setattr(bench, "Resolver", FakeResolver)
        monkeypatch.setattr(bench, "Downloader", FakeDownloader)

        assert await bench.run(args(connections=(1, 4))) == 0

        out = capsys.readouterr().out
        assert FakeDownloader.runs == [1, 4]
        assert "10.0 MiB/s" in out and "40.0 MiB/s" in out
        # Nothing left behind in the download directory.
        leftovers = [p for p in (env / "downloads").rglob("*") if p.is_file()]
        assert leftovers == []
