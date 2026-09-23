"""The in-Telegram setup wizard.

The PikPak half is exercised end to end against fakes. The Telegram
user-session half needs a real MTProto login, so only its guard rails are
covered here: that the environment can pin the session, that a group chat is
refused, and that any command aborts a conversation instead of trapping the
user in it.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from tgmd.config import (
    AccessConfig,
    Config,
    DeliveryConfig,
    PikPakConfig,
    TelegramConfig,
)
from tgmd.db import Database
from tgmd.pikpak import PikPakError
from tgmd.setup import USER_SESSION_KEY, SetupWizard, Step, stored_user_session

GOOD_PASSWORD = "correct-horse"


class FakeMessage:
    """What event.reply()/respond() hands back."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.edits: list[str] = []

    async def edit(self, text: str, **_kwargs) -> None:
        self.edits.append(text)
        self.text = text

    @property
    def last(self) -> str:
        return self.edits[-1] if self.edits else self.text


class FakeEvent:
    """A stand-in for a Telethon message event."""

    def __init__(self, text: str = "", user_id: int = 42, private: bool = True) -> None:
        self.raw_text = text
        self.sender_id = user_id
        self.chat_id = user_id
        self.is_private = private
        self.replies: list[FakeMessage] = []
        self.deleted = False

    async def reply(self, text: str, **_kwargs) -> FakeMessage:
        message = FakeMessage(text)
        self.replies.append(message)
        return message

    async def respond(self, text: str, **_kwargs) -> FakeMessage:
        return await self.reply(text)

    async def delete(self) -> None:
        self.deleted = True

    @property
    def last(self) -> str:
        return self.replies[-1].last if self.replies else ""


class FakePikPak:
    def __init__(self, *, configured: bool = False, allow_login: bool = True) -> None:
        self.configured = configured
        self.user_login_allowed = allow_login
        self.logins: list[tuple[int, str, str]] = []
        self.sessions: set[int] = set()

    async def login_with_password(self, user_id: int, username: str, password: str):
        if password != GOOD_PASSWORD:
            raise PikPakError("wrong password")
        self.logins.append((user_id, username, password))
        self.sessions.add(user_id)
        return username

    async def has_user_session(self, user_id: int) -> bool:
        return user_id in self.sessions

    async def available_for(self, user_id: int) -> bool:
        return self.configured or user_id in self.sessions


class FakeTelegramClient:
    """Stands in for the half-authenticated client a login leaves open."""

    def __init__(self) -> None:
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


class FakePortal:
    def __init__(self, reason: str | None = None) -> None:
        self._reason = reason

    def unavailable_reason(self) -> str | None:
        return self._reason


def make_config(*, user_session: str = "", cache_chat_id: int | None = None) -> Config:
    return Config(
        telegram=TelegramConfig(
            api_id=1, api_hash="h", bot_token="123456789:x", user_session=user_session
        ),
        access=AccessConfig(admin_user_ids=[42]),
        delivery=DeliveryConfig(cache_chat_id=cache_chat_id),
        pikpak=PikPakConfig(),
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "setup.sqlite3")
    await database.connect()
    yield database
    await database.close()


def make_wizard(
    db,
    *,
    pikpak: FakePikPak | None = None,
    portal: FakePortal | None = None,
    config: Config | None = None,
    has_user_client: bool = False,
    adopt=None,
) -> SetupWizard:
    return SetupWizard(
        bot=SimpleNamespace(),
        config=config or make_config(),
        db=db,
        pikpak=pikpak or FakePikPak(),
        portal=portal or FakePortal(),
        adopt_session=adopt,
        has_user_client=lambda: has_user_client,
    )


class TestStatus:
    async def test_bot_account_is_always_done(self, db):
        text = await make_wizard(db).status_text(42)
        assert "✅ <b>Bot account</b>" in text

    async def test_missing_reading_account_is_flagged(self, db):
        text = await make_wizard(db).status_text(42)
        assert "⬜ <b>Reading account</b>" in text
        assert "/setup telegram" in text

    async def test_present_reading_account_names_its_source(self, db):
        wizard = make_wizard(
            db, config=make_config(user_session="abc"), has_user_client=True
        )
        text = await wizard.status_text(42)
        assert "✅ <b>Reading account</b>" in text
        assert "from the environment" in text

    async def test_in_chat_login_is_named_as_the_source(self, db):
        await db.kv_set(USER_SESSION_KEY, "stored")
        text = await make_wizard(db, has_user_client=True).status_text(42)
        assert "from an in-chat login" in text

    async def test_missing_pikpak_is_flagged(self, db):
        text = await make_wizard(db).status_text(42)
        assert "⬜ <b>PikPak</b>" in text

    async def test_own_pikpak_account_is_reported(self, db):
        pikpak = FakePikPak()
        pikpak.sessions.add(42)
        text = await make_wizard(db, pikpak=pikpak).status_text(42)
        assert "your own account" in text

    async def test_shared_pikpak_account_is_reported(self, db):
        text = await make_wizard(db, pikpak=FakePikPak(configured=True)).status_text(42)
        assert "the shared account" in text

    async def test_cache_chat_state(self, db):
        without = await make_wizard(db).status_text(42)
        assert "⬜ <b>Upload cache</b>" in without
        wizard = make_wizard(db, config=make_config(cache_chat_id=-100123))
        assert "✅ <b>Upload cache</b>" in await wizard.status_text(42)

    async def test_public_channels_work_without_a_reading_account(self, db):
        text = await make_wizard(db).status_text(42)
        assert "Public channels already work" in text


class TestConversationLifecycle:
    async def test_no_conversation_by_default(self, db):
        wizard = make_wizard(db)
        assert not wizard.active(42)
        assert not await wizard.cancel(42)

    async def test_handle_ignores_strangers(self, db):
        assert await make_wizard(db).handle(FakeEvent("hello")) is False

    async def test_begin_pikpak_starts_one(self, db):
        wizard = make_wizard(db)
        await wizard.begin_pikpak(FakeEvent())
        assert wizard.active(42)

    async def test_cancel_ends_it(self, db):
        wizard = make_wizard(db)
        await wizard.begin_pikpak(FakeEvent())
        assert await wizard.cancel(42)
        assert not wizard.active(42)

    async def test_starting_again_replaces_the_conversation(self, db):
        wizard = make_wizard(db)
        await wizard.begin_pikpak(FakeEvent())
        await wizard.begin_pikpak(FakeEvent())
        assert wizard.active(42)

    async def test_a_command_aborts_and_defers(self, db):
        wizard = make_wizard(db)
        await wizard.begin_pikpak(FakeEvent())
        # False means "not mine", so the command handler still runs.
        assert await wizard.handle(FakeEvent("/status")) is False
        assert not wizard.active(42)

    async def test_a_stale_conversation_times_out(self, db):
        wizard = make_wizard(db)
        await wizard.begin_pikpak(FakeEvent())
        wizard._conversations[42].started_at = time.monotonic() - 10_000  # noqa: SLF001
        event = FakeEvent("something")
        assert await wizard.handle(event) is True
        assert "timed out" in event.last
        assert not wizard.active(42)

    async def test_expiry_through_active_closes_the_stale_client(self, db):
        wizard = make_wizard(db)
        await wizard.begin_pikpak(FakeEvent())
        stale = wizard._conversations[42]  # noqa: SLF001
        stale.started_at = time.monotonic() - 10_000
        client = FakeTelegramClient()
        stale.client = client

        assert wizard.active(42) is False
        # The close runs in the background; let it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert client.disconnected
        assert not wizard._background  # noqa: SLF001 - the task finished and was dropped

    async def test_expiry_does_not_close_a_conversation_started_right_after(self, db):
        """The race this guards against: an asynchronous expiry that pops by
        user id would find, and close, the fresh conversation instead."""
        wizard = make_wizard(db)
        await wizard.begin_pikpak(FakeEvent())
        wizard._conversations[42].started_at = time.monotonic() - 10_000  # noqa: SLF001

        assert wizard.active(42) is False
        # A new conversation, started before the background close has run.
        await wizard.begin_pikpak(FakeEvent())
        fresh = wizard._conversations[42]  # noqa: SLF001

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert wizard._conversations.get(42) is fresh  # noqa: SLF001
        assert wizard.active(42)

    async def test_conversations_are_per_user(self, db):
        wizard = make_wizard(db)
        await wizard.begin_pikpak(FakeEvent(user_id=1))
        assert wizard.active(1)
        assert not wizard.active(2)


class TestPikPakConversation:
    async def test_disabled_logins_are_refused(self, db):
        wizard = make_wizard(db, pikpak=FakePikPak(allow_login=False))
        event = FakeEvent()
        await wizard.begin_pikpak(event)
        assert "disabled" in event.last
        assert not wizard.active(42)

    async def test_the_web_form_is_offered_when_available(self, db):
        wizard = make_wizard(db, portal=FakePortal(None))
        event = FakeEvent()
        await wizard.begin_pikpak(event)
        assert "/pikpak login" in event.last

    async def test_no_web_form_offered_when_unavailable(self, db):
        wizard = make_wizard(db, portal=FakePortal("no public url"))
        event = FakeEvent()
        await wizard.begin_pikpak(event)
        assert "/pikpak login" not in event.last

    async def test_a_bad_email_is_rejected_without_advancing(self, db):
        wizard = make_wizard(db)
        await wizard.begin_pikpak(FakeEvent())
        event = FakeEvent("not an email")
        await wizard.handle(event)
        assert "does not look like" in event.last
        assert wizard._conversations[42].step is Step.PIKPAK_EMAIL  # noqa: SLF001

    async def test_an_email_advances_and_is_deleted(self, db):
        wizard = make_wizard(db)
        await wizard.begin_pikpak(FakeEvent())
        event = FakeEvent("me@example.com")
        await wizard.handle(event)
        assert event.deleted
        assert wizard._conversations[42].step is Step.PIKPAK_PASSWORD  # noqa: SLF001

    async def test_a_phone_number_is_accepted_as_the_account(self, db):
        wizard = make_wizard(db)
        await wizard.begin_pikpak(FakeEvent())
        await wizard.handle(FakeEvent("+8613800138000"))
        assert wizard._conversations[42].step is Step.PIKPAK_PASSWORD  # noqa: SLF001

    async def test_a_good_password_connects_and_finishes(self, db):
        pikpak = FakePikPak()
        wizard = make_wizard(db, pikpak=pikpak)
        await wizard.begin_pikpak(FakeEvent())
        await wizard.handle(FakeEvent("me@example.com"))
        event = FakeEvent(GOOD_PASSWORD)
        await wizard.handle(event)
        assert pikpak.logins == [(42, "me@example.com", GOOD_PASSWORD)]
        assert event.deleted
        assert "PikPak connected" in event.last
        assert not wizard.active(42)

    async def test_a_bad_password_keeps_the_conversation(self, db):
        pikpak = FakePikPak()
        wizard = make_wizard(db, pikpak=pikpak)
        await wizard.begin_pikpak(FakeEvent())
        await wizard.handle(FakeEvent("me@example.com"))
        event = FakeEvent("wrong")
        await wizard.handle(event)
        assert pikpak.logins == []
        assert "wrong password" in event.last
        assert wizard.active(42)
        assert event.deleted

    async def test_a_retry_after_a_bad_password_succeeds(self, db):
        pikpak = FakePikPak()
        wizard = make_wizard(db, pikpak=pikpak)
        await wizard.begin_pikpak(FakeEvent())
        await wizard.handle(FakeEvent("me@example.com"))
        await wizard.handle(FakeEvent("wrong"))
        await wizard.handle(FakeEvent(GOOD_PASSWORD))
        assert len(pikpak.logins) == 1


class TestTelegramConversationGuards:
    async def test_an_environment_session_blocks_the_flow(self, db):
        wizard = make_wizard(db, config=make_config(user_session="pinned"))
        assert wizard.session_pinned_by_env
        event = FakeEvent()
        await wizard.begin_telegram(event)
        assert "TG_USER_SESSION" in event.last
        assert not wizard.active(42)

    async def test_a_group_chat_is_refused(self, db):
        wizard = make_wizard(db)
        event = FakeEvent(private=False)
        await wizard.begin_telegram(event)
        assert "directly" in event.last
        assert not wizard.active(42)

    async def test_the_prompt_warns_about_login_codes(self, db):
        wizard = make_wizard(db)
        event = FakeEvent()
        await wizard.begin_telegram(event)
        assert "Never give a Telegram login" in event.last
        assert "you run this bot yourself" in event.last

    async def test_it_asks_for_a_phone_number_first(self, db):
        wizard = make_wizard(db)
        await wizard.begin_telegram(FakeEvent())
        assert wizard._conversations[42].step is Step.TG_PHONE  # noqa: SLF001

    async def test_a_bad_phone_number_does_not_advance(self, db):
        wizard = make_wizard(db)
        await wizard.begin_telegram(FakeEvent())
        event = FakeEvent("hello")
        await wizard.handle(event)
        assert "phone number" in event.last
        assert wizard._conversations[42].step is Step.TG_PHONE  # noqa: SLF001


class TestStoredSession:
    async def test_absent_by_default(self, db):
        assert await stored_user_session(db) is None

    async def test_round_trip(self, db):
        await db.kv_set(USER_SESSION_KEY, "a-session-string")
        assert await stored_user_session(db) == "a-session-string"
