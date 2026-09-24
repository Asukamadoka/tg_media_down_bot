"""Parallel multi-connection downloads (CC_BRIEF 2b).

The scheduler is exercised against fake part sources that serve a known
byte string, so every test can check the file that comes out bit for bit.
The Telethon connection layer is exercised against a fake client, and one
test pins the private Telethon attributes it relies on.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import TelegramClient
from telethon.errors import FileMigrateError, FileReferenceExpiredError, FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import DcOption
from telethon.tl.types.upload import FileCdnRedirect

from tgmd import parallel
from tgmd.downloader import DownloadCancelled, Downloader, DownloadError
from tgmd.parallel import (
    MediaRoute,
    ParallelUnavailable,
    download_parts,
    media_endpoints,
    telethon_sources,
)

PART = 1024  # small parts keep the tests fast; the logic is size-independent
CONTENT = bytes(range(256)) * 41  # 10,496 bytes: ten full parts and a short one


class Source:
    """Serves CONTENT, with scripted failures keyed by call number."""

    def __init__(self, content: bytes = CONTENT, *, script=None, name: str = "") -> None:
        self.content = content
        self.script = dict(script or {})
        self.calls = 0
        self.served: list[int] = []
        self.locations: list[object] = []
        self.name = name

    async def get(self, location, offset: int, limit: int) -> bytes:
        self.calls += 1
        self.locations.append(location)
        await asyncio.sleep(0)  # let the other workers interleave
        failure = self.script.pop(self.calls, None)
        if failure is not None:
            raise failure
        self.served.append(offset)
        return self.content[offset : offset + limit]


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake(seconds, *args):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("tgmd.parallel.asyncio.sleep", fake)
    return slept


async def run(tmp_path: Path, sources, **kwargs) -> Path:
    path = tmp_path / "out.bin"
    await download_parts(
        sources,
        location=kwargs.pop("location", "loc-1"),
        size=kwargs.pop("size", len(CONTENT)),
        path=path,
        part_size=PART,
        **kwargs,
    )
    return path


class TestScheduling:
    async def test_the_file_is_assembled_exactly(self, tmp_path):
        path = await run(tmp_path, [Source(), Source(), Source()])
        assert path.read_bytes() == CONTENT

    async def test_every_connection_takes_a_share(self, tmp_path):
        sources = [Source(), Source(), Source(), Source()]
        await run(tmp_path, sources)
        assert all(source.served for source in sources)
        served = sorted(offset for source in sources for offset in source.served)
        assert served == list(range(0, len(CONTENT), PART))

    async def test_progress_reaches_the_full_size(self, tmp_path):
        seen: list[tuple[int, int]] = []
        await run(tmp_path, [Source(), Source()], progress=lambda r, t: seen.append((r, t)))
        assert seen[-1] == (len(CONTENT), len(CONTENT))
        assert [r for r, _ in seen] == sorted(r for r, _ in seen)

    async def test_no_sources_means_fall_back(self, tmp_path):
        with pytest.raises(ParallelUnavailable):
            await run(tmp_path, [])


class TestFloodWaits:
    async def test_a_short_wait_is_honoured_then_resumed(self, tmp_path, no_sleep):
        waits: list[int] = []
        source = Source(script={2: FloodWaitError(request=None, capture=7)})
        path = await run(tmp_path, [source, Source()], on_flood_wait=waits.append)
        assert path.read_bytes() == CONTENT
        assert waits == [7]
        assert 8 in no_sleep

    async def test_a_long_wait_is_reported_not_slept(self, tmp_path, no_sleep):
        source = Source(script={1: FloodWaitError(request=None, capture=3600)})
        with pytest.raises(DownloadError, match="3600s"):
            await run(tmp_path, [source], flood_ceiling=300)
        assert 3601 not in no_sleep


class TestFileReferences:
    async def test_an_expired_reference_is_refreshed_once(self, tmp_path):
        refreshed: list[int] = []

        async def refresh():
            refreshed.append(1)
            return "loc-2"

        # Both connections hit the expiry; only one refresh must happen.
        sources = [
            Source(script={1: FileReferenceExpiredError(request=None)}),
            Source(script={1: FileReferenceExpiredError(request=None)}),
        ]
        path = await run(tmp_path, sources, refresh=refresh)
        assert path.read_bytes() == CONTENT
        assert refreshed == [1]
        assert all(source.locations[-1] == "loc-2" for source in sources)

    async def test_a_reference_that_keeps_expiring_falls_back(self, tmp_path):
        async def refresh():
            return "loc-again"

        expired = {n: FileReferenceExpiredError(request=None) for n in range(1, 10)}
        with pytest.raises(ParallelUnavailable, match="keeps expiring"):
            await run(tmp_path, [Source(script=expired)], refresh=refresh)

    async def test_without_a_way_to_refresh_it_falls_back(self, tmp_path):
        source = Source(script={1: FileReferenceExpiredError(request=None)})
        with pytest.raises(ParallelUnavailable):
            await run(tmp_path, [source])


class TestFallbacks:
    async def test_a_migrated_file_falls_back(self, tmp_path):
        source = Source(script={1: FileMigrateError(request=None, capture=5)})
        with pytest.raises(ParallelUnavailable, match="DC 5"):
            await run(tmp_path, [source])

    async def test_a_dropped_connection_is_retried(self, tmp_path, no_sleep):
        source = Source(script={3: ConnectionError("reset")})
        path = await run(tmp_path, [source])
        assert path.read_bytes() == CONTENT

    async def test_a_connection_that_keeps_dropping_falls_back(self, tmp_path, no_sleep):
        failing = {n: ConnectionError("reset") for n in range(1, 10)}
        with pytest.raises(ParallelUnavailable, match="kept failing"):
            await run(tmp_path, [Source(script=failing)])

    async def test_a_short_part_in_the_middle_falls_back(self, tmp_path):
        with pytest.raises(ParallelUnavailable, match="expected"):
            await run(tmp_path, [Source(content=CONTENT[:3000])])

    async def test_one_worker_failing_stops_the_others(self, tmp_path):
        slow = Source()
        failing = Source(script={1: FileMigrateError(request=None, capture=2)})
        with pytest.raises(ParallelUnavailable):
            await run(tmp_path, [failing, slow])
        assert slow.calls < len(CONTENT) // PART + 1


class TestCancellation:
    async def test_cancelling_stops_the_download(self, tmp_path):
        cancel = asyncio.Event()

        def stop_early(received, total):
            if received >= 3 * PART:
                cancel.set()

        with pytest.raises(DownloadCancelled):
            await run(tmp_path, [Source(), Source()], progress=stop_early, cancel=cancel)


# ------------------------------------------------------------- connections


class FakeSender:
    """Stands in for telethon.network.MTProtoSender."""

    opened: list[FakeSender] = []  # noqa: RUF012 - reset per test by the fixture
    refuse: set[str] = set()  # noqa: RUF012
    fail_after: int | None = None

    def __init__(self, auth_key, *, loggers) -> None:
        self.auth_key = auth_key
        self.connected_to = None
        self.requests: list[object] = []
        self.disconnected = False

    async def connect(self, connection):
        ip = connection[0]
        if ip in FakeSender.refuse or (
            FakeSender.fail_after is not None and len(FakeSender.opened) >= FakeSender.fail_after
        ):
            raise ConnectionError(f"refused {ip}")
        self.connected_to = ip
        FakeSender.opened.append(self)

    async def send(self, request):
        self.requests.append(request)
        return SimpleNamespace(bytes=b"")

    async def disconnect(self):
        self.disconnected = True


class FakeClient:
    def __init__(self, *, home_dc: int = 2) -> None:
        self.session = SimpleNamespace(dc_id=home_dc, auth_key="home-key")
        self.borrowed: list[int] = []
        self.returned: list[object] = []
        self._log = logging.getLogger("fake")
        self._proxy = None
        self._local_addr = None
        self._init_request = SimpleNamespace(
            api_id=1,
            device_model="m",
            system_version="s",
            app_version="a",
            system_lang_code="en",
            lang_pack="",
            lang_code="en",
            proxy=None,
            params=None,
        )

    async def _get_dc(self, dc_id):
        return SimpleNamespace(id=dc_id, ip_address=f"10.0.0.{dc_id}", port=443)

    async def _borrow_exported_sender(self, dc_id):
        self.borrowed.append(dc_id)
        return SimpleNamespace(auth_key=f"dc{dc_id}-key", dc_id=dc_id)

    async def _return_exported_sender(self, sender):
        self.returned.append(sender)

    def _connection(self, ip, port, dc_id, **_kwargs):
        return (ip, port, dc_id)


@pytest.fixture
def fake_senders(monkeypatch):
    FakeSender.opened = []
    FakeSender.refuse = set()
    FakeSender.fail_after = None
    monkeypatch.setattr(parallel, "MTProtoSender", FakeSender)
    return FakeSender


class TestConnections:
    async def test_the_home_dc_reuses_the_session_key(self, fake_senders):
        client = FakeClient(home_dc=2)
        async with telethon_sources(client, 2, 4) as (sources, _endpoint):
            assert len(sources) == 4
            assert {s.auth_key for s in fake_senders.opened} == {"home-key"}
        assert client.borrowed == []

    async def test_another_dc_borrows_telethons_key_and_gives_it_back(self, fake_senders):
        client = FakeClient(home_dc=2)
        async with telethon_sources(client, 4, 3) as (_sources, endpoint):
            assert {s.auth_key for s in fake_senders.opened} == {"dc4-key"}
            assert endpoint.ip_address == "10.0.0.4"
        assert client.borrowed == [4]
        assert len(client.returned) == 1

    async def test_every_connection_is_closed_afterwards(self, fake_senders):
        async with telethon_sources(FakeClient(), 2, 3):
            pass
        assert all(sender.disconnected for sender in fake_senders.opened)

    async def test_each_connection_introduces_itself(self, fake_senders):
        async with telethon_sources(FakeClient(), 2, 2):
            pass
        for sender in fake_senders.opened:
            assert type(sender.requests[0]).__name__ == "InvokeWithLayerRequest"

    async def test_a_refused_endpoint_moves_on_to_the_next(self, fake_senders):
        async def two(client, dc_id):
            return [
                SimpleNamespace(ip_address="203.0.113.1", port=443),
                SimpleNamespace(ip_address="10.0.0.2", port=443),
            ]

        fake_senders.refuse = {"203.0.113.1"}
        async with telethon_sources(FakeClient(), 2, 2, endpoints=two) as (_, endpoint):
            assert endpoint.ip_address == "10.0.0.2"

    async def test_no_endpoint_at_all_falls_back_and_still_returns_the_key(self, fake_senders):
        fake_senders.refuse = {"10.0.0.4"}
        client = FakeClient()
        with pytest.raises(ParallelUnavailable):
            async with telethon_sources(client, 4, 2):
                pass
        assert len(client.returned) == 1

    async def test_fewer_connections_is_better_than_none(self, fake_senders):
        fake_senders.fail_after = 2
        async with telethon_sources(FakeClient(), 2, 4) as (sources, _):
            assert len(sources) == 2

    async def test_a_cdn_redirect_falls_back(self):
        class Redirecting:
            async def send(self, request):
                return FileCdnRedirect(
                    dc_id=203, file_token=b"t", encryption_key=b"k", encryption_iv=b"i",
                    file_hashes=[],
                )

        source = parallel._SenderSource(Redirecting())  # noqa: SLF001
        with pytest.raises(ParallelUnavailable, match="CDN"):
            await source.get("loc", 0, PART)


class TestTelethonStillHasWhatWeUse:
    """These are private Telethon names. An upgrade that moves them should
    fail here, not halfway through someone's download."""

    def test_the_private_attributes_exist(self):
        client = TelegramClient(StringSession(), 1, "0" * 32)
        for name in (
            "_get_dc",
            "_borrow_exported_sender",
            "_return_exported_sender",
            "_connection",
            "_log",
            "_proxy",
            "_local_addr",
            "_init_request",
        ):
            assert hasattr(client, name), name
        request = client._init_request  # noqa: SLF001
        for field in ("api_id", "device_model", "system_version", "app_version",
                      "system_lang_code", "lang_pack", "lang_code", "proxy", "params"):
            assert hasattr(request, field), field

    def test_the_requests_we_send_are_well_formed(self):
        # Serialising proves the TL objects would actually go on the wire:
        # a wrong field name or type fails here, not against Telegram.
        from telethon.tl.functions.help import GetNearestDcRequest
        from telethon.tl.functions.upload import GetFileRequest

        client = TelegramClient(StringSession(), 1, "0" * 32)
        document = SimpleNamespace(id=1, access_hash=2, file_reference=b"ref")
        location = parallel.document_location(document)
        get = GetFileRequest(location=location, offset=3 * parallel.PART_SIZE,
                             limit=parallel.PART_SIZE)
        assert get._bytes()  # noqa: SLF001 - serialising is the point
        init = parallel._init_connection(client, GetNearestDcRequest())  # noqa: SLF001
        assert init._bytes()  # noqa: SLF001

    def test_parts_obey_the_upload_getfile_rules(self):
        # limit divides 1 MiB, and no part crosses a 1 MiB boundary.
        mib = 1024 * 1024
        assert mib % parallel.PART_SIZE == 0
        assert parallel.PART_SIZE % 4096 == 0


# ---------------------------------------------------------------- Downloader


def big_message(size: int = 20 * 1024 * 1024, *, dc_id: int = 4):
    document = SimpleNamespace(
        id=1, access_hash=2, file_reference=b"ref", dc_id=dc_id, attributes=[]
    )
    return SimpleNamespace(
        id=99,
        document=document,
        photo=None,
        media=object(),
        file=SimpleNamespace(
            name="big.mkv", size=size, mime_type="video/x-matroska",
            duration=None, width=None, height=None,
        ),
    )


class SequentialClient:
    def __init__(self) -> None:
        self.sequential = 0

    async def download_media(self, message, file, progress_callback=None):
        self.sequential += 1
        Path(file).write_bytes(b"one connection")
        return file


def fake_sources(monkeypatch, *, error: Exception | None = None, count: int = 4):
    """Replace the connection opener with one yielding in-memory sources."""
    import contextlib

    @contextlib.asynccontextmanager
    async def opener(client, dc_id, wanted, *, endpoints, on_refused=None):
        if error is not None:
            raise error
        yield [Source(content=b"z" * (20 * 1024 * 1024)) for _ in range(min(wanted, count))], (
            SimpleNamespace(ip_address="10.0.0.4", port=443, media_only=False)
        )

    monkeypatch.setattr("tgmd.downloader.telethon_sources", opener)


class TestDownloaderUsesIt:
    async def test_a_large_document_goes_parallel(self, tmp_path, monkeypatch, caplog):
        fake_sources(monkeypatch)
        client = SequentialClient()
        downloader = Downloader(client, connections=4)
        with caplog.at_level(logging.INFO, logger="tgmd.downloader"):
            path = await downloader.download(big_message(), tmp_path / "big.mkv")
        assert path.stat().st_size == 20 * 1024 * 1024
        assert client.sequential == 0
        assert downloader.last.connections == 4
        assert "from DC 4 over 4 connection(s)" in caplog.text

    async def test_a_small_file_uses_one_connection(self, tmp_path, monkeypatch):
        fake_sources(monkeypatch)
        client = SequentialClient()
        downloader = Downloader(client, connections=4)
        await downloader.download(big_message(size=1024), tmp_path / "small.mkv")
        assert client.sequential == 1

    async def test_one_configured_connection_never_goes_parallel(self, tmp_path, monkeypatch):
        fake_sources(monkeypatch)
        client = SequentialClient()
        await Downloader(client, connections=1).download(big_message(), tmp_path / "b.mkv")
        assert client.sequential == 1

    async def test_unavailable_falls_back_to_one_connection(self, tmp_path, monkeypatch):
        fake_sources(monkeypatch, error=ParallelUnavailable("CDN"))
        client = SequentialClient()
        path = await Downloader(client, connections=4).download(
            big_message(), tmp_path / "b.mkv"
        )
        assert client.sequential == 1
        assert path.read_bytes() == b"one connection"

    async def test_the_connection_count_is_capped(self):
        assert Downloader(SequentialClient(), connections=50)._connections == 8  # noqa: SLF001


# ------------------------------------------------ direct media route (2c)

# The shape of what Cowork measured from the NAS: only dc4's IPv4 media
# endpoint answered directly.
DC_OPTIONS = [
    DcOption(id=4, ip_address="149.154.167.91", port=443),
    DcOption(id=4, ip_address="149.154.166.111", port=443, media_only=True),
    DcOption(id=4, ip_address="2001:67c:4e8:f004::b", port=443, ipv6=True, media_only=True),
    DcOption(id=4, ip_address="149.154.165.1", port=443, media_only=True, tcpo_only=True),
    DcOption(id=4, ip_address="149.154.175.1", port=443, media_only=True, cdn=True),
    DcOption(id=2, ip_address="149.154.167.50", port=443),
    DcOption(id=1, ip_address="149.154.175.53", port=443),
]


class ConfiguredClient(FakeClient):
    """A fake client that also carries Telegram's DC list, as Telethon does."""

    _config = SimpleNamespace(dc_options=DC_OPTIONS)

    async def _get_dc(self, dc_id):
        return next(o for o in DC_OPTIONS if o.id == dc_id and not o.media_only)


class TestMediaEndpoints:
    async def test_only_usable_ipv4_media_endpoints_of_that_dc(self):
        found = await media_endpoints(ConfiguredClient(), 4)
        assert [o.ip_address for o in found] == ["149.154.166.111"]

    async def test_a_dc_without_one_has_none(self):
        assert await media_endpoints(ConfiguredClient(), 1) == []

    def test_telethon_keeps_the_config_where_we_read_it(self):
        # Private Telethon API, pinned like the others above.
        assert hasattr(TelegramClient, "_config")


class TestMediaRoute:
    async def test_media_first_then_the_ordinary_endpoint(self):
        found = await MediaRoute().endpoints(ConfiguredClient(), 4)
        assert [o.ip_address for o in found] == ["149.154.166.111", "149.154.167.91"]

    async def test_a_failed_route_is_skipped_for_a_while(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr("tgmd.parallel.time.monotonic", lambda: clock[0])
        route = MediaRoute(retry_after=1800)
        route.failed(4)
        found = await route.endpoints(ConfiguredClient(), 4)
        assert [o.ip_address for o in found] == ["149.154.167.91"]
        clock[0] += 1801
        assert route.usable(4)

    def test_refusals_only_count_against_media_endpoints(self):
        route = MediaRoute()
        route.refused(DC_OPTIONS[0])  # an ordinary endpoint
        assert route.usable(4)
        route.refused(DC_OPTIONS[1])  # the media one
        assert not route.usable(4)

    async def test_a_refused_direct_endpoint_falls_through_to_the_proxy(self, fake_senders):
        fake_senders.refuse = {"149.154.166.111"}
        route = MediaRoute()
        client = ConfiguredClient(home_dc=4)
        async with telethon_sources(
            client, 4, 2, endpoints=route.endpoints, on_refused=route.refused
        ) as (_, endpoint):
            assert endpoint.ip_address == "149.154.167.91"
        assert not route.usable(4)

    async def test_a_hanging_endpoint_times_out_quickly(self, fake_senders, monkeypatch):
        # TCP that never completes: a firewall silently dropping packets.
        real_connect = FakeSender.connect

        async def maybe_hang(self, connection):
            if connection[0] == "149.154.166.111":
                await asyncio.Event().wait()
            return await real_connect(self, connection)

        monkeypatch.setattr(FakeSender, "connect", maybe_hang)
        route = MediaRoute()
        async with telethon_sources(
            ConfiguredClient(home_dc=4), 4, 1,
            endpoints=route.endpoints, on_refused=route.refused, connect_timeout=0.05,
        ) as (_, endpoint):
            assert endpoint.ip_address == "149.154.167.91"


class TestDownloaderWithTheRoute:
    def opener_recording(self, monkeypatch, *, break_direct: bool = False):
        """A connection opener that remembers which endpoints it was offered."""
        import contextlib

        offered: list[list[str]] = []

        @contextlib.asynccontextmanager
        async def opener(client, dc_id, wanted, *, endpoints, on_refused=None):
            options = await endpoints(client, dc_id)
            offered.append([o.ip_address for o in options])
            chosen = options[0]
            if break_direct and chosen.media_only:
                failing = {n: ConnectionError("reset") for n in range(1, 50)}
                yield [Source(script=failing) for _ in range(wanted)], chosen
                return
            yield [Source(content=b"z" * (20 * 1024 * 1024)) for _ in range(wanted)], chosen

        monkeypatch.setattr("tgmd.downloader.telethon_sources", opener)
        return offered

    async def test_a_small_file_on_a_direct_dc_uses_our_connection(self, tmp_path, monkeypatch):
        # Telethon would fetch it over its main connection, through the proxy.
        offered = self.opener_recording(monkeypatch)
        client = SequentialClient()
        client.__class__ = type("C", (SequentialClient, ConfiguredClient), {})
        downloader = Downloader(client, connections=4, route=MediaRoute())
        await downloader.download(big_message(size=2 * 1024 * 1024), tmp_path / "s.mkv")
        assert client.sequential == 0
        assert downloader.last.connections == 1
        assert offered[0][0] == "149.154.166.111"
        assert "media" in downloader.last.endpoint

    async def test_a_small_file_elsewhere_is_left_to_telethon(self, tmp_path, monkeypatch):
        self.opener_recording(monkeypatch)
        client = SequentialClient()
        client.__class__ = type("C", (SequentialClient, ConfiguredClient), {})
        downloader = Downloader(client, connections=4, route=MediaRoute())
        await downloader.download(big_message(size=2 * 1024 * 1024, dc_id=1), tmp_path / "s")
        assert client.sequential == 1

    async def test_a_direct_route_that_breaks_is_retried_via_the_proxy(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("tgmd.parallel.asyncio.sleep", _instant)
        offered = self.opener_recording(monkeypatch, break_direct=True)
        client = SequentialClient()
        client.__class__ = type("C", (SequentialClient, ConfiguredClient), {})
        route = MediaRoute()
        downloader = Downloader(client, connections=4, route=route)
        path = await downloader.download(big_message(), tmp_path / "b.mkv")
        assert path.stat().st_size == 20 * 1024 * 1024
        assert client.sequential == 0
        assert offered == [["149.154.166.111", "149.154.167.91"], ["149.154.167.91"]]
        assert not route.usable(4)


async def _instant(*_args):
    return None
