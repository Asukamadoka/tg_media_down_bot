"""Preflight verification: ``python -m tgmd.verify`` and in-chat ``/verify``.

The command's whole value is telling the truth about a deployment, so the
tests here are mostly about false alarms: a bot whose admin was claimed in
chat, or whose reading account was signed in with /setup telegram, must not
be reported as missing either.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from tgmd import bootstrap
from tgmd.config import (
    AccessConfig,
    Config,
    DeliveryConfig,
    DownloadConfig,
    HttpConfig,
    PikPakConfig,
    TelegramConfig,
)
from tgmd.db import Database
from tgmd.identity import Report, Status
from tgmd.setup import USER_SESSION_KEY
from tgmd.verify import (
    check_access_control,
    check_bot_identity,
    check_directories,
    check_forward_path,
    check_pikpak,
    check_token,
    run_checks,
    run_live_checks,
)

BOT_ID = 123456789
BOT_TOKEN = f"{BOT_ID}:AAHfiqksKZ8wmoyzYeb1n1pbDVHQHKQ1abc"
READER = SimpleNamespace(id=555, username="reader", bot=False, premium=False)
BOT = SimpleNamespace(id=BOT_ID, username="pikpak_WMS_bot", bot=True)


def session_string() -> str:
    """A well-formed, non-empty session. An empty one serialises to ""."""
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(256))
    return session.save()


def by_name(report: Report, name: str):
    matches = [check for check in report.checks if check.name == name]
    assert matches, f"no {name!r} check in {[c.name for c in report.checks]}"
    return matches[-1]


def make_config(tmp_path, **overrides) -> Config:
    return Config(
        telegram=TelegramConfig(
            api_id=1,
            api_hash="0123456789abcdef0123456789abcdef",
            bot_token=overrides.pop("bot_token", BOT_TOKEN),
            session_dir=tmp_path / "sessions",
        ),
        access=overrides.pop("access", AccessConfig()),
        download=DownloadConfig(dir=tmp_path / "downloads", data_dir=tmp_path / "data"),
        pikpak=overrides.pop("pikpak", PikPakConfig()),
        http=overrides.pop("http", HttpConfig()),
    )


class FakeClient:
    """Both clients verify builds: a bot after start(), a reader otherwise."""

    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, session, api_id, api_hash, **_kwargs) -> None:
        self.session = session
        self.is_bot = False
        self.disconnected = False
        FakeClient.instances.append(self)

    async def start(self, bot_token=None):
        self.is_bot = True

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return BOT if self.is_bot else READER

    async def get_dialogs(self, limit=None):
        return [object()]

    async def get_permissions(self, chat, user):
        return SimpleNamespace(
            is_admin=True, is_creator=True, post_messages=True, is_banned=False, has_left=False
        )

    async def get_input_entity(self, chat_id):
        return chat_id

    async def get_entity(self, peer):
        return SimpleNamespace(id=peer, broadcast=True)

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def fake_telegram(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr("tgmd.verify.TelegramClient", FakeClient)
    return FakeClient


class TestOfflineChecks:
    def test_a_good_token_names_its_bot(self, tmp_path):
        report = Report()
        token = check_token(report, make_config(tmp_path))
        assert token is not None and token.bot_id == BOT_ID
        assert report.ok

    def test_a_bad_token_fails(self, tmp_path):
        report = Report()
        assert check_token(report, make_config(tmp_path, bot_token="nope")) is None
        assert not report.ok

    def test_writable_directories_pass(self, tmp_path):
        report = Report()
        check_directories(report, make_config(tmp_path))
        assert report.ok

    def test_nobody_allowed_is_a_failure_that_points_at_claim(self, tmp_path):
        report = Report()
        check_access_control(report, make_config(tmp_path))
        check = by_name(report, "access control")
        assert check.status is Status.FAIL
        assert "/claim" in check.detail


class TestBotIdentity:
    def test_a_matching_bot_passes(self):
        report = Report()
        check_bot_identity(report, BOT, SimpleNamespace(bot_id=BOT_ID))
        assert by_name(report, "bot identity").status is Status.OK

    def test_a_token_for_another_bot_fails(self):
        report = Report()
        check_bot_identity(report, BOT, SimpleNamespace(bot_id=1))
        assert by_name(report, "bot identity").status is Status.FAIL

    def test_a_human_account_fails(self):
        report = Report()
        check_bot_identity(report, READER, SimpleNamespace(bot_id=READER.id))
        assert by_name(report, "bot identity").status is Status.FAIL


class TestPikPakLogin:
    async def test_https_offers_the_mini_app(self, tmp_path):
        http = HttpConfig(enabled=True, public_base_url="https://media.example.com")
        report = Report()
        await check_pikpak(report, make_config(tmp_path, http=http), db=None)
        check = by_name(report, "pikpak login")
        assert check.status is Status.OK
        assert "/pikpak/app" in check.detail

    async def test_no_https_is_a_warning_not_a_failure(self, tmp_path):
        # /setup pikpak works without any web server, so users are not stuck.
        report = Report()
        await check_pikpak(report, make_config(tmp_path), db=None)
        check = by_name(report, "pikpak login")
        assert check.status is Status.WARN
        assert "/setup pikpak" in check.detail


class TestRunChecksReadsTheDatabase:
    """The A1 false alarms, in the command-line tool."""

    async def seed(self, config: Config, *, admin=False, session=False) -> None:
        db = Database(config.download.db_path)
        await db.connect()
        try:
            if admin:
                await bootstrap.add_runtime_admin(db, config, 4242)
            if session:
                await db.kv_set(USER_SESSION_KEY, session_string())
        finally:
            await db.close()

    async def test_a_claimed_admin_counts(self, tmp_path, fake_telegram):
        config = make_config(tmp_path)
        await self.seed(make_config(tmp_path), admin=True)
        report = await run_checks(config)
        assert by_name(report, "access control").status is Status.OK

    async def test_an_in_chat_reading_account_counts(self, tmp_path, fake_telegram):
        config = make_config(tmp_path, access=AccessConfig(admin_user_ids=[1]))
        await self.seed(config, session=True)
        report = await run_checks(config)
        check = by_name(report, "user session")
        assert check.status is Status.OK
        assert "in-chat login" in check.detail
        assert by_name(report, "account separation").status is Status.OK

    async def test_no_reading_account_is_only_a_warning(self, tmp_path, fake_telegram):
        config = make_config(tmp_path, access=AccessConfig(admin_user_ids=[1]))
        report = await run_checks(config)
        check = by_name(report, "user session")
        assert check.status is Status.WARN
        assert "/setup telegram" in check.detail

    async def test_a_healthy_setup_passes_and_releases_its_clients(
        self, tmp_path, fake_telegram
    ):
        config = make_config(tmp_path)
        await self.seed(config, admin=True, session=True)
        report = await run_checks(config)
        assert report.ok, report.render_text()
        assert all(client.disconnected for client in fake_telegram.instances)


class FakePikPak:
    def __init__(self, *, own: bool = False, configured: bool = False) -> None:
        self.own = own
        self.configured = configured

    async def has_user_session(self, user_id):
        return self.own

    async def quota(self, *, user_id=None):
        return SimpleNamespace(used=1, limit=10)


class FakePortal:
    def __init__(self, reason=None) -> None:
        self.reason = reason

    def unavailable_reason(self):
        return self.reason


class TestLiveChecks:
    """The in-chat /verify, which must reuse the running clients."""

    async def test_everything_running(self, tmp_path):
        bot, user = FakeClient(None, 1, "h"), FakeClient(None, 1, "h")
        bot.is_bot = True
        report = await run_live_checks(
            make_config(tmp_path),
            bot=bot,
            user=user,
            pikpak=FakePikPak(own=True),
            portal=FakePortal(),
            for_user_id=4242,
        )
        assert report.ok, report.render_text()
        assert by_name(report, "pikpak account").status is Status.OK
        assert not bot.disconnected and not user.disconnected

    async def test_without_a_reading_account_or_pikpak(self, tmp_path):
        bot = FakeClient(None, 1, "h")
        bot.is_bot = True
        report = await run_live_checks(
            make_config(tmp_path),
            bot=bot,
            user=None,
            pikpak=FakePikPak(),
            portal=FakePortal("plain HTTP"),
            for_user_id=4242,
        )
        assert by_name(report, "user session").status is Status.WARN
        assert by_name(report, "pikpak account").status is Status.WARN
        check = by_name(report, "pikpak login")
        assert check.status is Status.WARN
        assert "/setup pikpak" in check.detail


class ForwardChecks:
    """A reading account with a chosen standing in the cache channel."""

    def __init__(self, *, sees: bool = True, creator=False, poster=False, broadcast=True):
        self.sees = sees
        self.permissions = SimpleNamespace(
            is_creator=creator, post_messages=poster, is_banned=False, has_left=False
        )
        self.broadcast = broadcast

    async def get_input_entity(self, chat_id):
        if not self.sees:
            raise ValueError("Could not find the input entity")
        return chat_id

    async def iter_dialogs(self):
        for _ in ():
            yield

    async def get_entity(self, peer):
        return SimpleNamespace(id=peer, broadcast=self.broadcast)

    async def get_permissions(self, peer, user):
        return self.permissions


class TestForwardPath:
    async def check(self, tmp_path, user, *, cache=-100555):
        config = make_config(tmp_path)
        config.delivery = DeliveryConfig(cache_chat_id=cache)
        report = Report()
        await check_forward_path(report, config, user)
        return by_name(report, "forward fast path")

    async def test_no_cache_channel_says_how_to_get_one(self, tmp_path):
        check = await self.check(tmp_path, ForwardChecks(), cache=None)
        assert check.status is Status.WARN
        assert "/cache" in check.detail

    async def test_the_owner_of_the_channel_can_forward(self, tmp_path):
        check = await self.check(tmp_path, ForwardChecks(creator=True))
        assert check.status is Status.OK

    async def test_a_plain_subscriber_of_a_channel_cannot(self, tmp_path):
        check = await self.check(tmp_path, ForwardChecks())
        assert check.status is Status.WARN
        assert "cannot post" in check.detail

    async def test_any_member_of_a_group_can(self, tmp_path):
        check = await self.check(tmp_path, ForwardChecks(broadcast=False))
        assert check.status is Status.OK

    async def test_not_a_member_is_explained(self, tmp_path):
        check = await self.check(tmp_path, ForwardChecks(sees=False))
        assert check.status is Status.WARN
        assert "cannot see" in check.detail

    async def test_the_bot_reading_for_itself_needs_nothing(self, tmp_path):
        check = await self.check(tmp_path, None, cache=None)
        assert check.status is Status.OK
