"""The forward fast path and its fallbacks.

Four outcomes matter, because each sends the job down a different road:
forwarded (nothing downloaded), restricted, no cache channel, and a forward
that Telegram refused. The last three must all fall back to cloning.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.errors import ChatForwardsRestrictedError, ChatWriteForbiddenError

from tgmd.config import Config, DeliveryConfig
from tgmd.db import Database
from tgmd.downloader import MediaInfo
from tgmd.forwarder import Forwarder, Outcome, forwardable

USER_CHAT = 7
CACHE_CHAT = -100555
INFO = MediaInfo(file_name="clip.mp4", size=64)


class FakeReader:
    """The reading account: forwards into the cache channel, or refuses.

    ``knows`` is whether its session already has the cache channel cached;
    ``member`` is whether listing dialogs would find it.
    """

    def __init__(
        self, *, error: Exception | None = None, knows: bool = True, member: bool = True
    ) -> None:
        self.error = error
        self.knows = knows
        self.member = member
        self.listings = 0
        self.forwards: list[tuple[int, int, object]] = []

    async def get_input_entity(self, chat_id):
        if not self.knows:
            raise ValueError("Could not find the input entity")
        return ("peer", chat_id)

    async def iter_dialogs(self):
        self.listings += 1
        self.knows = self.member
        for _ in ():
            yield

    async def forward_messages(self, entity, messages, from_peer=None):
        if self.error is not None:
            raise self.error
        self.forwards.append((entity, messages, from_peer))
        return SimpleNamespace(id=9001)


class FakeBot:
    def __init__(self, *, sees_copy: bool = True) -> None:
        self.sees_copy = sees_copy
        self.sent: list[tuple[int, object]] = []

    async def get_messages(self, chat, ids):
        if not self.sees_copy:
            return None
        return SimpleNamespace(id=ids, media=f"bot-ref-to-{ids}")

    async def send_file(self, chat_id, file, **_kwargs):
        self.sent.append((chat_id, file))


def message(*, noforwards: bool = False):
    return SimpleNamespace(id=42, media="reader-ref", noforwards=noforwards)


def chat(*, noforwards: bool = False):
    return SimpleNamespace(id=1234, title="Source", noforwards=noforwards)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "f.sqlite3")
    await database.connect()
    yield database
    await database.close()


def make(db, *, reader=None, bot=None, cache: bool = True) -> tuple[Forwarder, FakeBot]:
    bot = bot or FakeBot()
    config = Config(delivery=DeliveryConfig(cache_chat_id=CACHE_CHAT if cache else None))
    return Forwarder(bot, config, db, reader=lambda: reader), bot


async def deliver(forwarder: Forwarder, *, msg=None, source=None):
    return await forwarder.deliver(
        chat_id=USER_CHAT,
        message=msg or message(),
        source=source or chat(),
        caption="caption",
        key="1234:42",
        info=INFO,
    )


class TestForwardable:
    def test_an_ordinary_message_can_be_forwarded(self):
        assert forwardable(message(), chat())

    def test_a_protected_message_cannot(self):
        assert not forwardable(message(noforwards=True), chat())

    def test_a_protected_chat_cannot(self):
        assert not forwardable(message(), chat(noforwards=True))

    def test_missing_flags_mean_allowed(self):
        assert forwardable(SimpleNamespace(), SimpleNamespace())


class TestTheFourBranches:
    async def test_forwardable_goes_through_the_cache_channel(self, db):
        reader = FakeReader()
        forwarder, bot = make(db, reader=reader)
        attempt = await deliver(forwarder)
        assert attempt.outcome is Outcome.FORWARDED
        # The reading account forwarded; the bot re-sent with its own reference.
        assert reader.forwards[0][:2] == (("peer", CACHE_CHAT), 42)
        assert bot.sent == [(USER_CHAT, "bot-ref-to-9001")]

    async def test_restricted_is_never_attempted(self, db):
        reader = FakeReader()
        forwarder, bot = make(db, reader=reader)
        attempt = await deliver(forwarder, source=chat(noforwards=True))
        assert attempt.outcome is Outcome.RESTRICTED
        assert reader.forwards == []
        assert bot.sent == []

    async def test_no_cache_channel_falls_back(self, db):
        reader = FakeReader()
        forwarder, bot = make(db, reader=reader, cache=False)
        attempt = await deliver(forwarder)
        assert attempt.outcome is Outcome.NO_CACHE
        assert reader.forwards == []
        assert bot.sent == []

    async def test_a_refused_forward_falls_back(self, db):
        reader = FakeReader(error=ChatWriteForbiddenError(request=None))
        forwarder, bot = make(db, reader=reader)
        attempt = await deliver(forwarder)
        assert attempt.outcome is Outcome.FAILED
        assert bot.sent == []


class TestEdges:
    async def test_telegram_saying_restricted_counts_as_restricted(self, db):
        # The chat changed its setting after the message was read.
        forwarder, _ = make(db, reader=FakeReader(error=ChatForwardsRestrictedError(None)))
        assert (await deliver(forwarder)).outcome is Outcome.RESTRICTED

    async def test_a_copy_the_bot_cannot_see_falls_back(self, db):
        # The bot is not an admin of the cache channel, for instance.
        forwarder, bot = make(db, reader=FakeReader(), bot=FakeBot(sees_copy=False))
        assert (await deliver(forwarder)).outcome is Outcome.FAILED
        assert bot.sent == []

    async def test_a_forward_is_remembered_in_the_upload_cache(self, db):
        forwarder, _ = make(db, reader=FakeReader())
        await deliver(forwarder)
        entry = await db.cache_lookup("1234:42")
        assert (entry["cache_chat_id"], entry["cache_msg_id"]) == (CACHE_CHAT, 9001)

    async def test_when_the_bot_reads_it_resends_its_own_reference(self, db):
        # No reading account: the bot fetched the message, so the reference
        # is already the bot's and no cache channel is needed.
        forwarder, bot = make(db, reader=None, cache=False)
        attempt = await deliver(forwarder)
        assert attempt.outcome is Outcome.FORWARDED
        assert bot.sent == [(USER_CHAT, "reader-ref")]

    async def test_the_reader_is_looked_up_at_call_time(self, db):
        # /setup telegram can swap the reading account while the bot runs.
        current = {"reader": None}
        bot = FakeBot()
        config = Config(delivery=DeliveryConfig(cache_chat_id=CACHE_CHAT))
        forwarder = Forwarder(bot, config, db, reader=lambda: current["reader"])
        current["reader"] = FakeReader()
        await deliver(forwarder)
        assert current["reader"].forwards


class TestResolvingTheCacheChannel:
    """A string session forgets every entity on restart (see resolve_peer)."""

    async def test_a_fresh_session_lists_dialogs_once_then_forwards(self, db):
        reader = FakeReader(knows=False)
        forwarder, _ = make(db, reader=reader)
        assert (await deliver(forwarder)).outcome is Outcome.FORWARDED
        assert (await deliver(forwarder)).outcome is Outcome.FORWARDED
        assert reader.listings == 1

    async def test_not_a_member_falls_back_without_listing_again(self, db):
        # Listing every dialog of the user's own account on every file would
        # be exactly the kind of repeated bulk read red line 6 rules out.
        reader = FakeReader(knows=False, member=False)
        forwarder, _ = make(db, reader=reader)
        assert (await deliver(forwarder)).outcome is Outcome.FAILED
        assert (await deliver(forwarder)).outcome is Outcome.FAILED
        assert reader.listings == 1

    async def test_it_tries_again_after_a_while(self, db, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr("tgmd.forwarder.time.monotonic", lambda: clock[0])
        reader = FakeReader(knows=False, member=False)
        forwarder, _ = make(db, reader=reader)
        await deliver(forwarder)
        reader.member = True  # someone added the account to the channel
        clock[0] += 601
        assert (await deliver(forwarder)).outcome is Outcome.FORWARDED
        assert reader.listings == 2


class TestIntoTheCacheChannelItself:
    """M7.2 B: a request made in the cache channel is answered there."""

    async def test_the_forward_is_the_delivery(self, db):
        forwarder, bot = make(db, reader=FakeReader())
        attempt = await forwarder.deliver(
            chat_id=CACHE_CHAT, message=message(), source=chat(), caption="c",
            key="1234:42", info=INFO,
        )
        assert attempt.outcome is Outcome.FORWARDED
        assert bot.sent == []  # no second post of the same file
        assert (await db.cache_lookup("1234:42"))["cache_msg_id"] == 9001
