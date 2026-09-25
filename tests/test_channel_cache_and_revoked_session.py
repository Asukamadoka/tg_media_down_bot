"""Two failures seen on the NAS on 2026-09-25.

* ``/cache`` posted inside a broadcast channel got no answer at all: a channel
  post carries the channel as its sender, so the allow list refused it and the
  refusal is silent outside private chats.
* A user session Telegram had revoked (AuthKeyDuplicatedError) made startup
  raise, so the bot crash-looped and /setup telegram, the way to fix it, was
  unreachable too.
"""

from __future__ import annotations

import pytest
from telethon.errors import AuthKeyDuplicatedError

from tgmd import clients as clients_module
from tgmd import handlers as handlers_module
from tgmd.config import Config
from tgmd.handlers import BotHandlers

ADMIN = 4242
CHANNEL = -1003729105427


class Rights:
    def __init__(self, *, admin=False, creator=False):
        self.is_admin = admin
        self.is_creator = creator


class FakeBot:
    def __init__(self, rights):
        self.rights = rights  # {user_or_"me": Rights | Exception}

    async def get_permissions(self, chat_id, who):
        value = self.rights.get(who)
        if isinstance(value, Exception):
            raise value
        return value or Rights()


class ChannelPost:
    def __init__(self, text="/cache"):
        self.raw_text = text
        self.sender_id = CHANNEL
        self.chat_id = CHANNEL
        self.is_private = False
        self.is_channel = True
        self.is_group = False
        self.replies: list[str] = []

    async def reply(self, text, **_kwargs):
        self.replies.append(text)


@pytest.fixture
def config():
    config = Config()
    config.access.admin_user_ids = [ADMIN]
    return config


def make(config, rights, monkeypatch):
    stored = []

    async def set_cache_chat(_db, cfg, chat_id):
        stored.append(chat_id)
        cfg.delivery.cache_chat_id = chat_id

    monkeypatch.setattr(handlers_module.bootstrap, "set_cache_chat", set_cache_chat)
    handlers = BotHandlers(
        bot=FakeBot(rights), config=config, db=object(), queue=object(),
        pikpak=object(), portal=object(),
    )
    return handlers, stored


class TestCacheInsideAChannel:
    async def test_a_channel_run_by_the_admin_becomes_the_cache(self, config, monkeypatch):
        handlers, stored = make(
            config, {"me": Rights(admin=True), ADMIN: Rights(creator=True)}, monkeypatch
        )
        event = ChannelPost()
        await handlers.on_cache(event)
        assert stored == [CHANNEL]
        assert event.replies  # answered, not silent

    async def test_an_admin_who_is_only_a_channel_admin_counts(self, config, monkeypatch):
        handlers, stored = make(
            config, {"me": Rights(admin=True), ADMIN: Rights(admin=True)}, monkeypatch
        )
        await handlers.on_cache(ChannelPost())
        assert stored == [CHANNEL]

    async def test_a_stranger_s_channel_is_refused_out_loud(self, config, monkeypatch):
        # Someone else adds the bot to their channel and posts /cache: that
        # would copy every cached file into their channel.
        handlers, stored = make(
            config,
            {"me": Rights(admin=True), ADMIN: ValueError("not a participant")},
            monkeypatch,
        )
        event = ChannelPost()
        await handlers.on_cache(event)
        assert stored == []
        assert len(event.replies) == 1

    async def test_the_bot_must_still_be_an_admin_there(self, config, monkeypatch):
        handlers, stored = make(
            config, {"me": Rights(), ADMIN: Rights(creator=True)}, monkeypatch
        )
        event = ChannelPost()
        await handlers.on_cache(event)
        assert stored == []
        assert event.replies


class FakeUserClient:
    def __init__(self, error=None, authorized=True):
        self.error = error
        self.authorized = authorized
        self.disconnected = False

    async def connect(self):
        if self.error:
            raise self.error

    async def is_user_authorized(self):
        return self.authorized

    async def disconnect(self):
        self.disconnected = True


class FakeBotClient:
    async def start(self, bot_token):
        return self

    async def get_me(self):
        class Me:
            id = 123
            username = "b"
            first_name = "b"
            bot = True

        return Me()


class TestARevokedSessionDoesNotStopTheBot:
    async def test_auth_key_duplicated_leaves_the_bot_running(self, config, monkeypatch):
        config.telegram.bot_token = "123:AAAA"
        user = FakeUserClient(error=AuthKeyDuplicatedError(request=None))
        monkeypatch.setattr(clients_module, "build_bot_client", lambda _c: FakeBotClient())
        monkeypatch.setattr(clients_module, "build_user_client", lambda _c, _s=None: user)
        bot, reader = await clients_module.start_clients(config, "dead-session")
        assert bot is not None
        assert reader is None
        assert user.disconnected
