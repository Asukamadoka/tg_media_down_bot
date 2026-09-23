"""Booting the whole application.

Every other test exercises one component. This one constructs the real
:class:`~tgmd.app.Application` and runs ``start()`` against stubbed Telegram
clients, because the wiring graph is the part unit tests cannot see: the
order services are built in, what gets handed to whom, and whether teardown
releases everything it acquired.

Only the two MTProto clients are faked. The database, HTTP server, login
portal, job queue, handlers, wizard and self-configuration are all the real
objects doing their real work.
"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest
from telethon.sessions import StringSession

from tgmd import bootstrap
from tgmd.app import Application
from tgmd.config import (
    AccessConfig,
    Config,
    DeliveryConfig,
    DownloadConfig,
    HttpConfig,
    PikPakConfig,
    TelegramConfig,
)

BOT_TOKEN = "123456789:AAHfiqksKZ8wmoyzYeb1n1pbDVHQHKQ1abc"

# Telethon validates the shape before anything else looks at it, so the tests
# need a string it actually accepts. An empty session serialises to one.
VALID_SESSION = StringSession().save()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class FakeClient:
    """Enough of a Telethon client for startup and shutdown."""

    def __init__(self, *, user_id: int = 123456789, username: str = "pikpak_WMS_bot",
                 is_bot: bool = True) -> None:
        self.me = SimpleNamespace(
            id=user_id, username=username, bot=is_bot,
            first_name="Test", last_name=None, premium=False,
        )
        self.handlers: list = []
        self.requests: list = []
        self.disconnected = False

    async def get_me(self):
        return self.me

    def add_event_handler(self, callback, event=None):
        self.handlers.append((callback, event))

    async def __call__(self, request):
        # botconfig sends its command menu and profile text through here.
        self.requests.append(request)
        return None

    async def disconnect(self):
        self.disconnected = True


def make_config(tmp_path, *, http: bool = False, admins=None) -> Config:
    port = free_port()
    return Config(
        telegram=TelegramConfig(
            api_id=1,
            api_hash="0123456789abcdef0123456789abcdef",
            bot_token=BOT_TOKEN,
            session_dir=tmp_path / "sessions",
        ),
        access=AccessConfig(admin_user_ids=list(admins or [])),
        download=DownloadConfig(
            dir=tmp_path / "downloads",
            data_dir=tmp_path / "data",
            concurrent=2,
        ),
        delivery=DeliveryConfig(),
        pikpak=PikPakConfig(),
        http=HttpConfig(
            enabled=http,
            host="127.0.0.1",
            port=port,
            public_base_url=f"http://127.0.0.1:{port}" if http else "",
        ),
    )


@pytest.fixture
def fake_clients(monkeypatch):
    """Replace the MTProto clients, leaving everything else real."""
    bot = FakeClient()
    user = FakeClient(user_id=987654321, username="reader", is_bot=False)

    async def fake_start_clients(config, stored_session=None):
        return bot, user

    monkeypatch.setattr("tgmd.app.start_clients", fake_start_clients)
    return bot, user


@pytest.fixture
async def app(tmp_path, fake_clients):
    instance = Application(make_config(tmp_path))
    await instance.start()
    yield instance
    await instance.stop()


class TestStartup:
    async def test_every_service_is_built(self, app):
        assert app.bot is not None
        assert app.user is not None
        assert app.pikpak is not None
        assert app.portal is not None
        assert app.file_server is not None
        assert app.queue is not None
        assert app.handlers is not None
        assert app.wizard is not None

    async def test_the_database_is_open(self, app):
        # A real query, not just a non-None attribute.
        assert await app.db.get_or_create_secret()

    async def test_directories_are_created(self, app):
        assert app.config.download.dir.is_dir()
        assert app.config.download.data_dir.is_dir()
        assert app.config.telegram.session_dir.is_dir()

    async def test_workers_are_running(self, app):
        workers = app.queue._workers  # noqa: SLF001 - the point of the test
        assert len(workers) == app.config.download.concurrent
        assert all(not worker.done() for worker in workers)

    async def test_handlers_are_registered(self, app, fake_clients):
        bot, _ = fake_clients
        # One per command, plus the catch-all link handler.
        assert len(bot.handlers) >= 10

    async def test_the_wizard_is_attached_to_the_handlers(self, app):
        assert app.handlers._wizard is app.wizard  # noqa: SLF001

    async def test_the_handlers_know_the_reading_client(self, app, fake_clients):
        _, user = fake_clients
        assert app.handlers._user_client is user  # noqa: SLF001

    async def test_self_configuration_ran(self, app, fake_clients):
        bot, _ = fake_clients
        # The command menu and the profile text.
        assert len(bot.requests) == 2


class TestClaimAnnouncement:
    async def test_an_unclaimed_bot_gets_a_code(self, app):
        assert await app.db.kv_get(bootstrap.CLAIM_CODE_KEY)

    async def test_a_configured_admin_means_no_code(self, tmp_path, fake_clients):
        instance = Application(make_config(tmp_path, admins=[42]))
        await instance.start()
        try:
            assert await instance.db.kv_get(bootstrap.CLAIM_CODE_KEY) is None
        finally:
            await instance.stop()

    async def test_a_previous_claim_is_restored_on_the_next_boot(
        self, tmp_path, fake_clients
    ):
        config = make_config(tmp_path)
        first = Application(config)
        await first.start()
        try:
            code = await first.db.kv_get(bootstrap.CLAIM_CODE_KEY)
            await bootstrap.claim_admin(first.db, config, code, 777)
        finally:
            await first.stop()

        # A fresh process, same data directory.
        second = Application(make_config(tmp_path))
        await second.start()
        try:
            assert second.config.access.is_admin(777)
            assert await second.db.kv_get(bootstrap.CLAIM_CODE_KEY) is None
        finally:
            await second.stop()

    async def test_no_claim_code_is_ever_logged_after_a_claim(
        self, tmp_path, fake_clients, caplog
    ):
        config = make_config(tmp_path)
        first = Application(config)
        with caplog.at_level("DEBUG"):
            await first.start()
        try:
            code = await first.db.kv_get(bootstrap.CLAIM_CODE_KEY)
            # Proves the check below can see the line when it is there.
            assert code in caplog.text
            await bootstrap.claim_admin(first.db, config, code, 777)
        finally:
            await first.stop()

        caplog.clear()
        with caplog.at_level("DEBUG"):
            second = Application(make_config(tmp_path))
            await second.start()
            await second.stop()
        logged = caplog.text
        assert code not in logged
        assert "/claim" not in logged
        assert "NO ADMIN YET" not in logged


class TestHttpSurface:
    async def test_the_server_serves_when_enabled(self, tmp_path, fake_clients):
        import aiohttp

        instance = Application(make_config(tmp_path, http=True))
        await instance.start()
        try:
            base = instance.config.http.base_url
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/healthz") as response:
                    assert response.status == 200
                # The portal registered its routes on the same server.
                async with session.get(f"{base}/pikpak/app") as response:
                    assert response.status == 200
        finally:
            await instance.stop()

    async def test_plain_http_offers_no_mini_app(self, tmp_path, fake_clients):
        # Telegram refuses web_app buttons that are not HTTPS, so /pikpak
        # login falls back to /setup pikpak in chat.
        instance = Application(make_config(tmp_path, http=True))
        await instance.start()
        try:
            assert "HTTPS" in (instance.portal.unavailable_reason() or "")
            assert instance.portal.miniapp_url is None
        finally:
            await instance.stop()

    async def test_the_mini_app_follows_the_live_access_list(self, tmp_path, fake_clients):
        # Bound to the same list /claim appends to, so a claim made after
        # startup is honoured without a restart.
        config = make_config(tmp_path)
        instance = Application(config)
        await instance.start()
        try:
            is_allowed = instance.portal._is_allowed  # noqa: SLF001 - the wiring is the point
            assert not is_allowed(777)
            code = await instance.db.kv_get(bootstrap.CLAIM_CODE_KEY)
            await bootstrap.claim_admin(instance.db, config, code, 777)
            assert is_allowed(777)
        finally:
            await instance.stop()

    async def test_nothing_binds_when_disabled(self, app):
        assert not app.file_server.usable


class TestAdoptUserSession:
    async def test_a_new_session_replaces_the_reading_client(
        self, app, monkeypatch, fake_clients
    ):
        _, original = fake_clients
        replacement = FakeClient(user_id=555, username="newreader", is_bot=False)

        async def fake_connect():
            return None

        async def fake_authorized():
            return True

        replacement.connect = fake_connect
        replacement.is_user_authorized = fake_authorized
        monkeypatch.setattr(
            "tgmd.app.TelegramClient", lambda *args, **kwargs: replacement
        )

        label = await app.adopt_user_session(VALID_SESSION)

        assert "newreader" in label
        assert app.user is replacement
        assert app.handlers._user_client is replacement  # noqa: SLF001
        # The client it replaced is released, not left connected.
        assert original.disconnected

    async def test_an_unauthorized_session_is_refused(
        self, app, monkeypatch, fake_clients
    ):
        _, original = fake_clients
        replacement = FakeClient(user_id=555, is_bot=False)

        async def fake_connect():
            return None

        async def fake_authorized():
            return False

        replacement.connect = fake_connect
        replacement.is_user_authorized = fake_authorized
        monkeypatch.setattr(
            "tgmd.app.TelegramClient", lambda *args, **kwargs: replacement
        )

        with pytest.raises(RuntimeError, match="not authorized"):
            await app.adopt_user_session(VALID_SESSION)
        # The working client is kept.
        assert app.user is original

    async def test_a_malformed_session_string_is_reported_clearly(self, app):
        # Telethon's own error is just "Not a valid string".
        with pytest.raises(RuntimeError, match="not a Telegram session string"):
            await app.adopt_user_session("obviously-not-a-session")


class TestShutdown:
    async def test_stop_releases_everything(self, tmp_path, fake_clients):
        bot, user = fake_clients
        instance = Application(make_config(tmp_path, http=True))
        await instance.start()
        workers = list(instance.queue._workers)  # noqa: SLF001

        await instance.stop()

        assert all(worker.done() for worker in workers)
        assert bot.disconnected
        assert user.disconnected
        assert not instance.file_server.usable

    async def test_stop_is_safe_to_call_twice(self, tmp_path, fake_clients):
        instance = Application(make_config(tmp_path))
        await instance.start()
        await instance.stop()
        await instance.stop()

    async def test_request_stop_unblocks_run(self, tmp_path, fake_clients):
        instance = Application(make_config(tmp_path))
        await instance.start()

        async def never_disconnects():
            await asyncio.Event().wait()

        instance.bot.run_until_disconnected = never_disconnects
        try:
            instance.request_stop()
            await asyncio.wait_for(instance.run(), timeout=5)
        finally:
            await instance.stop()
