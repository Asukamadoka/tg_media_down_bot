"""``TG_DIRECT_MEDIA=v2``: direct media connections on keys of their own
(docs/wms/M7.1 §B).

The rule under test above all others: a key the direct route uses is never
used anywhere else. v1 broke it and the reading account's session was
revoked. Everything here runs against a fake client and fake senders; no
test touches the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.errors import (
    AuthBytesInvalidError,
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
)
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest
from telethon.tl.functions.upload import GetFileRequest
from telethon.tl.types import DcOption
from test_parallel import Source, big_message

from tgmd import direct as direct_module
from tgmd.db import Database
from tgmd.direct import COOLDOWN, DirectFlood, DirectRouteV2, egress_of
from tgmd.downloader import Downloader, DownloadError
from tgmd.parallel import ParallelUnavailable, media_endpoints

MIB = 1024 * 1024
HOME = 5
ACCOUNT = 777

# What Cowork measured from the NAS (M7.1 §B0): DC2 and DC4 media endpoints
# answer directly, and the reading account lives in DC5.
DC_OPTIONS = [
    DcOption(id=4, ip_address="149.154.167.91", port=443),
    DcOption(id=4, ip_address="2001:67c:4e8:f004::b", port=443, ipv6=True, media_only=True),
    DcOption(id=4, ip_address="149.154.166.111", port=443, media_only=True),
    DcOption(id=4, ip_address="149.154.175.1", port=443, media_only=True, cdn=True),
    DcOption(id=2, ip_address="149.154.167.50", port=443),
    DcOption(id=2, ip_address="2001:67c:4e8:f002::b", port=443, ipv6=True, media_only=True),
    DcOption(id=5, ip_address="91.108.56.130", port=443),
    DcOption(id=5, ip_address="91.108.56.200", port=443, media_only=True),
    DcOption(id=1, ip_address="149.154.175.53", port=443),
]


class FakeSender:
    """Stands in for MTProtoSender; remembers the key and route of each."""

    opened: list[FakeSender] = []  # noqa: RUF012 - reset by the fixture
    refuse: set[str] = set()  # noqa: RUF012
    fail: dict[str, Exception] = {}  # noqa: RUF012 - request name -> error, every time
    negotiated = 0

    def __init__(self, auth_key, *, loggers) -> None:
        self.auth_key = auth_key
        self.given_key = auth_key
        self.connection = None
        self.requests: list[object] = []
        self.disconnected = False

    async def connect(self, connection):
        ip = connection["ip"]
        if ip in FakeSender.refuse:
            raise ConnectionError(f"refused {ip}")
        if self.auth_key is None:
            # The DH exchange: a key nobody else has seen.
            FakeSender.negotiated += 1
            self.auth_key = SimpleNamespace(key=f"new-key-{FakeSender.negotiated}".encode())
        self.connection = connection
        FakeSender.opened.append(self)

    async def send(self, request):
        self.requests.append(request)
        inner = getattr(getattr(request, "query", None), "query", request)
        failure = FakeSender.fail.get(type(inner).__name__)
        if failure is not None:
            raise failure
        if isinstance(request, GetFileRequest):
            return SimpleNamespace(bytes=b"d" * request.limit)
        return SimpleNamespace()

    async def disconnect(self):
        self.disconnected = True

    @property
    def key_bytes(self) -> bytes:
        return self.auth_key.key


class FakeClient:
    """A reading client in DC5, going through a proxy Telethon knows about."""

    _config = SimpleNamespace(dc_options=DC_OPTIONS)

    def __init__(self, *, home_dc: int = HOME) -> None:
        self.session = SimpleNamespace(dc_id=home_dc, auth_key=SimpleNamespace(key=b"main-key"))
        self._log = logging.getLogger("fake")
        self._proxy = ("socks5", "127.0.0.1", 7890)
        self._local_addr = None
        self._init_request = SimpleNamespace(
            api_id=1, device_model="m", system_version="s", app_version="a",
            system_lang_code="en", lang_pack="", lang_code="en", proxy=None, params=None,
        )
        self.calls: list[object] = []
        self.sequential = 0
        self.export_error: Exception | None = None

    async def _get_dc(self, dc_id):
        return next(o for o in DC_OPTIONS if o.id == dc_id and not o.media_only)

    def _connection(self, ip, port, dc_id, *, loggers, proxy, local_addr):
        return {"ip": ip, "port": port, "dc": dc_id, "proxy": proxy}

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, ExportAuthorizationRequest):
            if self.export_error is not None:
                raise self.export_error
            return SimpleNamespace(id=ACCOUNT, bytes=b"exported")
        raise AssertionError(f"unexpected request {request!r}")

    async def get_me(self, input_peer=False):
        return SimpleNamespace(user_id=ACCOUNT)

    async def _borrow_exported_sender(self, dc_id):
        raise AssertionError("v2 must never borrow Telethon's exported sender")

    async def download_media(self, message, file, progress_callback=None):
        self.sequential += 1
        Path(file).write_bytes(b"one connection")
        return file


@pytest.fixture
def senders(monkeypatch):
    FakeSender.opened = []
    FakeSender.refuse = set()
    FakeSender.fail = {}
    FakeSender.negotiated = 0
    monkeypatch.setattr(direct_module, "MTProtoSender", FakeSender)
    return FakeSender


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "bot.sqlite3")
    await database.connect()
    yield database
    await database.close()


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def route(db, clock):
    return DirectRouteV2(db, clock=clock)


@pytest.fixture
def proxy_route(monkeypatch):
    """The ordinary route: records each use, serves the file in memory."""
    used: list[int] = []

    @contextlib.asynccontextmanager
    async def opener(client, dc_id, wanted, *, endpoints, on_refused=None):
        used.append(dc_id)
        yield [Source(content=b"p" * (20 * MIB)) for _ in range(wanted)], (
            SimpleNamespace(ip_address="149.154.167.91", port=443, media_only=False)
        )

    monkeypatch.setattr("tgmd.downloader.telethon_sources", opener)
    return used


@pytest.fixture
def slept(monkeypatch):
    waited: list[float] = []

    async def fake(seconds, *args):
        waited.append(seconds)

    monkeypatch.setattr("tgmd.downloader.asyncio.sleep", fake)
    return waited


# ------------------------------------------------------------- the home DC


class TestNeverTheHomeDc:
    async def test_not_available(self, route, senders):
        assert not await route.available(FakeClient(), HOME)

    async def test_asking_anyway_is_a_hard_error(self, route, senders):
        with pytest.raises(AssertionError, match="home DC"):
            async with route.sources(FakeClient(), HOME, 4):
                pass
        assert senders.opened == []

    async def test_an_unknown_home_dc_is_never_guessed(self, route, senders):
        client = FakeClient()
        client.session = None
        assert not await route.available(client, 4)

    async def test_a_home_dc_file_takes_the_ordinary_route(
        self, tmp_path, route, senders, proxy_route
    ):
        client = FakeClient()
        downloader = Downloader(client, connections=4, direct=route)
        await downloader.download(big_message(dc_id=HOME), tmp_path / "f.mkv")
        assert senders.opened == []
        assert proxy_route == [HOME]
        assert downloader.last.route == "proxy"


# ------------------------------------------------------------- its own key


class TestItsOwnKey:
    async def test_a_new_key_by_dh_on_a_direct_connection(self, route, senders, db):
        client = FakeClient()
        async with route.sources(client, 4, 3) as (sources, label):
            assert len(sources) == 3
        first = senders.opened[0]
        assert first.given_key is None  # the sender ran the key exchange itself
        key = first.key_bytes
        assert key == b"new-key-1"
        # The authorisation came over the main connection; the key did not.
        assert [type(c) for c in client.calls] == [ExportAuthorizationRequest]
        imported = first.requests[0].query.query
        assert isinstance(imported, ImportAuthorizationRequest)
        assert imported.bytes == b"exported"
        # Every connection: the media endpoint, direct, on that key.
        for sender in senders.opened:
            assert sender.connection["ip"] == "149.154.166.111"
            assert sender.connection["proxy"] is None
            assert sender.key_bytes == key
        assert await db.direct_key_get(ACCOUNT, 4, "ipv4") == key
        assert label == "direct-v2 149.154.166.111:443"

    async def test_the_key_is_never_telethons(self, route, senders):
        client = FakeClient()
        async with route.sources(client, 4, 4):
            pass
        keys = {s.key_bytes for s in senders.opened}
        assert client.session.auth_key.key not in keys
        # _borrow_exported_sender raises if touched; getting here means it was not.

    async def test_a_restart_reuses_the_stored_key(self, db, senders, clock):
        async with DirectRouteV2(db, clock=clock).sources(FakeClient(), 4, 1):
            pass
        again = FakeClient()
        async with DirectRouteV2(db, clock=clock).sources(again, 4, 2):
            pass
        assert senders.negotiated == 1
        assert again.calls == []  # no second export
        assert {s.key_bytes for s in senders.opened} == {b"new-key-1"}

    async def test_ipv6_has_a_key_of_its_own(self, route, senders, db):
        # One key is only ever used from one kind of address.
        senders.refuse = {"149.154.166.111"}
        async with route.sources(FakeClient(), 4, 1) as (_sources, label):
            assert "[2001:67c:4e8:f004::b]:443" in label
        assert await db.direct_key_get(ACCOUNT, 4, "ipv6") == b"new-key-1"
        assert await db.direct_key_get(ACCOUNT, 4, "ipv4") is None

    async def test_concurrent_first_uses_share_one_negotiation(self, route, senders):
        client = FakeClient()

        async def use():
            async with route.sources(client, 4, 1):
                await asyncio.sleep(0)

        await asyncio.gather(use(), use())
        assert senders.negotiated == 1

    def test_egress(self):
        assert egress_of(DC_OPTIONS[1]) == "ipv6"
        assert egress_of(DC_OPTIONS[2]) == "ipv4"


# ------------------------------------------------------------- refusals


class TestRefusals:
    async def seed(self, db):
        await db.direct_key_store(ACCOUNT, 4, "ipv4", b"stored-key")

    @pytest.mark.parametrize("error", [
        AuthKeyUnregisteredError(request=None),
        AuthKeyDuplicatedError(request=None),
    ])
    async def test_a_rejected_key_is_dropped_and_the_dc_rests(
        self, tmp_path, route, senders, db, proxy_route, error
    ):
        await self.seed(db)
        senders.fail = {"GetFileRequest": error}
        client = FakeClient()
        downloader = Downloader(client, connections=4, direct=route)
        path = await downloader.download(big_message(dc_id=4), tmp_path / "f.mkv")
        assert path.read_bytes()[:1] == b"p"  # the ordinary route delivered it
        assert proxy_route == [4]
        assert downloader.last.route == "proxy"
        assert await db.direct_key_get(ACCOUNT, 4, "ipv4") is None
        assert await route.resting(4)

    async def test_a_rejected_stored_key_on_connect(self, route, senders, db):
        await self.seed(db)
        senders.fail = {"GetNearestDcRequest": AuthKeyUnregisteredError(request=None)}
        with pytest.raises(ParallelUnavailable):
            async with route.sources(FakeClient(), 4, 2):
                pass
        assert await db.direct_key_get(ACCOUNT, 4, "ipv4") is None
        assert await route.resting(4)
        assert all(s.disconnected for s in senders.opened)

    async def test_a_failed_import_stores_nothing(self, route, senders, db):
        senders.fail = {"ImportAuthorizationRequest": AuthBytesInvalidError(request=None)}
        with pytest.raises(ParallelUnavailable):
            async with route.sources(FakeClient(), 4, 1):
                pass
        assert await db.direct_key_get(ACCOUNT, 4, "ipv4") is None
        assert await route.resting(4)
        assert all(s.disconnected for s in senders.opened)

    async def test_no_attempt_while_resting(self, tmp_path, route, senders, db, clock,
                                            proxy_route):
        await route.rest(4, "test")
        client = FakeClient()
        assert not await route.available(client, 4)
        await Downloader(client, connections=4, direct=route).download(
            big_message(dc_id=4), tmp_path / "f.mkv"
        )
        assert senders.opened == [] and client.calls == []
        assert proxy_route == [4]
        clock.now += COOLDOWN + 1
        assert await route.available(client, 4)

    async def test_resting_survives_a_restart(self, db, clock, senders):
        await DirectRouteV2(db, clock=clock).rest(4, "test")
        assert await DirectRouteV2(db, clock=clock).resting(4)
        assert not await DirectRouteV2(db, clock=clock).resting(2)

    async def test_a_flood_wait_is_waited_out_then_the_proxy(
        self, tmp_path, route, senders, db, proxy_route, slept
    ):
        await self.seed(db)
        senders.fail = {"GetFileRequest": FloodWaitError(request=None, capture=30)}
        downloader = Downloader(FakeClient(), connections=4, direct=route)
        await downloader.download(big_message(dc_id=4), tmp_path / "f.mkv")
        assert 31 in slept  # the account's limit, honoured before the fallback
        assert proxy_route == [4]
        assert await route.resting(4)
        # A flood wait says nothing against the key.
        assert await db.direct_key_get(ACCOUNT, 4, "ipv4") == b"stored-key"

    async def test_a_long_flood_wait_fails_the_download(
        self, tmp_path, route, senders, db, proxy_route, slept
    ):
        await self.seed(db)
        senders.fail = {"GetFileRequest": FloodWaitError(request=None, capture=3600)}
        with pytest.raises(DownloadError):
            await Downloader(FakeClient(), connections=4, direct=route).download(
                big_message(dc_id=4), tmp_path / "f.mkv"
            )
        assert proxy_route == []

    async def test_a_flood_wait_on_export(self, route, senders):
        client = FakeClient()
        client.export_error = FloodWaitError(request=None, capture=120)
        with pytest.raises(DirectFlood) as caught:
            async with route.sources(client, 4, 1):
                pass
        assert caught.value.seconds == 120
        assert await route.resting(4)

    async def test_unreachable_rests_briefly(self, route, senders, clock):
        senders.refuse = {"149.154.166.111", "2001:67c:4e8:f004::b"}
        with pytest.raises(ParallelUnavailable):
            async with route.sources(FakeClient(), 4, 1):
                pass
        assert await route.resting(4)
        clock.now += 1801
        assert not await route.resting(4)


# ------------------------------------------------------------- guard rails


class TestGuardRails:
    async def test_at_most_four_per_dc(self, route, senders):
        client = FakeClient()
        async with route.sources(client, 4, 8) as (sources, _):
            assert len(sources) == 4
            assert route.free(4) == 0
            assert not await route.available(client, 4)
            with pytest.raises(ParallelUnavailable, match="already has 4"):
                async with route.sources(client, 4, 1):
                    pass
            # Another DC is counted on its own.
            async with route.sources(client, 2, 2) as (other, _):
                assert len(other) == 2
        assert route.free(4) == 4 and route.free(2) == 4

    async def test_slots_come_back_after_a_failure(self, route, senders):
        senders.refuse = {"149.154.166.111", "2001:67c:4e8:f004::b"}
        with pytest.raises(ParallelUnavailable):
            async with route.sources(FakeClient(), 4, 4):
                pass
        assert route.free(4) == 4

    async def test_connections_come_out_of_download_connections(
        self, tmp_path, route, senders, proxy_route
    ):
        downloader = Downloader(FakeClient(), connections=2, direct=route)
        await downloader.download(big_message(dc_id=4), tmp_path / "f.mkv")
        assert len(senders.opened) == 2 == downloader.last.connections

    async def test_the_log_names_the_route(self, tmp_path, route, senders, proxy_route, caplog):
        downloader = Downloader(FakeClient(), connections=4, direct=route)
        with caplog.at_level(logging.INFO, logger="tgmd.downloader"):
            path = await downloader.download(big_message(dc_id=4), tmp_path / "f.mkv")
            await downloader.download(big_message(dc_id=1), tmp_path / "g.mkv")
        assert path.read_bytes()[:1] == b"d"
        assert proxy_route == [1]
        assert "route direct-v2" in caplog.text
        assert "route proxy" in caplog.text

    async def test_a_small_file_uses_one_direct_connection(
        self, tmp_path, route, senders, proxy_route
    ):
        client = FakeClient()
        downloader = Downloader(client, connections=4, direct=route)
        await downloader.download(big_message(size=2 * MIB, dc_id=4), tmp_path / "s.mkv")
        assert client.sequential == 0
        assert downloader.last.route == "direct-v2"
        assert downloader.last.connections == 1

    async def test_off_means_off(self, tmp_path, senders, proxy_route):
        downloader = Downloader(FakeClient(), connections=4)
        await downloader.download(big_message(dc_id=4), tmp_path / "f.mkv")
        assert senders.opened == []
        assert downloader.last.route == "proxy"


class TestMediaEndpoints:
    async def test_ipv4_first_then_ipv6(self):
        found = await media_endpoints(FakeClient(), 4)
        assert [o.ip_address for o in found] == ["149.154.166.111", "2001:67c:4e8:f004::b"]

    async def test_dc2_is_ipv6_only(self):
        found = await media_endpoints(FakeClient(), 2)
        assert [o.ip_address for o in found] == ["2001:67c:4e8:f002::b"]


class TestTheTable:
    async def test_keys_are_per_account_dc_and_egress(self, db):
        await db.direct_key_store(1, 4, "ipv4", b"a")
        await db.direct_key_store(2, 4, "ipv4", b"b")
        await db.direct_key_store(1, 4, "ipv6", b"c")
        await db.direct_key_store(1, 4, "ipv4", b"a2")
        assert await db.direct_key_get(1, 4, "ipv4") == b"a2"
        assert await db.direct_key_get(2, 4, "ipv4") == b"b"
        await db.direct_key_forget(1, 4, "ipv4")
        assert await db.direct_key_get(1, 4, "ipv4") is None
        assert await db.direct_key_get(1, 4, "ipv6") == b"c"

    async def test_an_old_database_gains_the_table(self, tmp_path):
        import sqlite3

        path = tmp_path / "old.sqlite3"
        sqlite3.connect(path).execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
        database = Database(path)
        await database.connect()
        try:
            await database.direct_key_store(1, 4, "ipv4", b"k")
            assert await database.direct_key_get(1, 4, "ipv4") == b"k"
        finally:
            await database.close()


class TestTelethonStillHasWhatWeUse:
    """Private Telethon behaviour this route depends on, pinned."""

    def test_a_sender_without_a_key_negotiates_one(self):
        from telethon import TelegramClient
        from telethon.network import MTProtoSender
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(), 1, "0" * 32)
        sender = MTProtoSender(None, loggers=client._log)  # noqa: SLF001
        assert not sender.auth_key  # falsy: connect() runs the DH exchange
        assert sender.auth_key.key is None

    def test_a_stored_key_round_trips(self):
        from telethon.crypto import AuthKey

        key = bytes(range(256))
        assert AuthKey(key).key == key

    def test_the_connection_takes_an_explicit_proxy(self):
        import inspect

        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(), 1, "0" * 32)
        parameters = inspect.signature(client._connection).parameters  # noqa: SLF001
        assert {"proxy", "local_addr", "loggers"} <= set(parameters)

    def test_the_import_goes_on_the_wire(self):
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        from tgmd.parallel import _init_connection

        client = TelegramClient(StringSession(), 1, "0" * 32)
        request = _init_connection(client, ImportAuthorizationRequest(id=1, bytes=b"x"))
        assert request._bytes()  # noqa: SLF001
        assert ExportAuthorizationRequest(dc_id=4)._bytes()  # noqa: SLF001


class TestManualEndpoints:
    """M7.2 C: TG_DIRECT_ENDPOINTS, because the DC list Telegram hands out
    through the proxy lacks the endpoints the NAS reaches directly."""

    def route(self, db, clock):
        manual = {4: [("149.154.166.110", 443), ("149.154.166.111", 443)],
                  HOME: [("9.9.9.9", 443)]}
        return DirectRouteV2(db, clock=clock, manual=manual)

    async def test_tried_first(self, db, clock, senders):
        route = self.route(db, clock)
        found = await route.endpoints(FakeClient(), 4)
        # Given order first; Telegram's own after, without repeating .111.
        assert [o.ip_address for o in found] == [
            "149.154.166.110", "149.154.166.111", "2001:67c:4e8:f004::b",
        ]
        async with route.sources(FakeClient(), 4, 1) as (_sources, label):
            assert label == "direct-v2 149.154.166.110:443"

    async def test_a_dc_with_only_a_manual_endpoint_becomes_available(self, db, clock, senders):
        route = DirectRouteV2(db, clock=clock, manual={3: [("149.154.175.100", 443)]})
        assert await route.available(FakeClient(), 3)

    async def test_the_home_dc_s_entries_are_ignored(self, db, clock, senders, caplog):
        route = self.route(db, clock)
        with caplog.at_level(logging.WARNING, logger="tgmd.direct"):
            assert not await route.available(FakeClient(), HOME)
        assert "ignored" in caplog.text
        with pytest.raises(AssertionError):
            async with route.sources(FakeClient(), HOME, 1):
                pass
        assert senders.opened == []

    async def test_an_endpoint_of_another_dc_rests_and_says_which(self, db, clock, senders,
                                                                   caplog):
        senders.fail = {"ImportAuthorizationRequest": AuthBytesInvalidError(request=None)}
        route = self.route(db, clock)
        with caplog.at_level(logging.WARNING, logger="tgmd.direct"), \
                pytest.raises(ParallelUnavailable):
            async with route.sources(FakeClient(), 4, 1):
                pass
        assert await route.resting(4)
        assert "149.154.166.110:443" in caplog.text and "TG_DIRECT_ENDPOINTS" in caplog.text
        assert await db.direct_key_get(ACCOUNT, 4, "ipv4") is None

    async def test_the_same_rules_still_hold(self, db, clock, senders):
        # Its own key, direct, no proxy: nothing changes for a manual endpoint.
        async with self.route(db, clock).sources(FakeClient(), 4, 2):
            pass
        assert all(s.connection["proxy"] is None for s in senders.opened)
        assert senders.opened[0].given_key is None
