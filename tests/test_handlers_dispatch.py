"""The catch-all message handler, and how it shares events with commands.

Telethon runs *every* matching handler, so ``on_message`` also sees the
commands that ``on_setup`` and friends are already dealing with. Getting that
overlap wrong is not a cosmetic bug: it silently broke the whole setup wizard,
because ``/setup telegram`` cancelled the conversation it had just opened and
the first answer then fell through to the "send me a link" fallback.
"""

from __future__ import annotations

import pytest

from tgmd import handlers as handlers_module
from tgmd.config import Config
from tgmd.handlers import BotHandlers

ADMIN = 4242


class FakeWizard:
    """Records what the dispatcher does to a setup conversation."""

    def __init__(self, *, open_for: set[int] | None = None) -> None:
        self.open_for = set(open_for or ())
        self.cancelled: list[int] = []
        self.handled: list[object] = []

    def active(self, user_id: int) -> bool:
        return user_id in self.open_for

    async def cancel(self, user_id: int) -> bool:
        self.cancelled.append(user_id)
        return self.open_for.discard(user_id) is None

    async def handle(self, event) -> bool:
        self.handled.append(event)
        return True


class FakeEvent:
    def __init__(self, text: str, *, user_id: int = ADMIN) -> None:
        self.raw_text = text
        self.sender_id = user_id
        self.chat_id = user_id
        self.is_private = True
        self.message = object()
        self.replies: list[str] = []

    async def reply(self, text, **_kwargs):
        self.replies.append(text)
        return self


@pytest.fixture
def config():
    config = Config()
    config.access.admin_user_ids = [ADMIN]
    return config


@pytest.fixture
def bot(config, monkeypatch):
    monkeypatch.setattr(handlers_module, "has_downloadable_media", lambda _m: False)
    return BotHandlers(
        bot=object(),
        config=config,
        db=object(),
        queue=object(),
        pikpak=object(),
        portal=object(),
    )


class TestSetupDoesNotCancelItself:
    """The regression that made /setup unusable."""

    async def test_setup_leaves_its_own_conversation_open(self, bot):
        wizard = FakeWizard(open_for={ADMIN})
        bot.attach_wizard(wizard)
        await bot.on_message(FakeEvent("/setup telegram"))
        assert wizard.cancelled == []
        assert ADMIN in wizard.open_for

    async def test_bare_setup_also_leaves_it_open(self, bot):
        wizard = FakeWizard(open_for={ADMIN})
        bot.attach_wizard(wizard)
        await bot.on_message(FakeEvent("/setup"))
        assert wizard.cancelled == []

    async def test_the_wizard_never_sees_a_command(self, bot):
        wizard = FakeWizard(open_for={ADMIN})
        bot.attach_wizard(wizard)
        await bot.on_message(FakeEvent("/setup telegram"))
        assert wizard.handled == []


class TestOtherCommandsStillAbort:
    """The property the original code was protecting: nobody gets trapped."""

    @pytest.mark.parametrize("command", ["/help", "/status", "/mode local", "/cancel"])
    async def test_any_other_command_cancels(self, bot, command):
        wizard = FakeWizard(open_for={ADMIN})
        bot.attach_wizard(wizard)
        await bot.on_message(FakeEvent(command))
        assert wizard.cancelled == [ADMIN]

    async def test_a_command_never_reaches_the_link_parser(self, bot):
        wizard = FakeWizard()
        bot.attach_wizard(wizard)
        event = FakeEvent("/help")
        await bot.on_message(event)
        assert event.replies == []


class TestOrdinaryMessages:
    async def test_an_answer_goes_to_an_open_conversation(self, bot):
        wizard = FakeWizard(open_for={ADMIN})
        bot.attach_wizard(wizard)
        event = FakeEvent("+8613800138000")
        await bot.on_message(event)
        assert wizard.handled == [event]
        assert event.replies == []

    async def test_without_a_conversation_it_falls_through(self, bot):
        wizard = FakeWizard()
        bot.attach_wizard(wizard)
        event = FakeEvent("+8613800138000")
        await bot.on_message(event)
        assert wizard.handled == []
        assert len(event.replies) == 1
