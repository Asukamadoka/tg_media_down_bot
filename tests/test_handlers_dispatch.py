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


class RecordingHandlers(BotHandlers):
    """Records which submission path on_message chose, instead of queueing."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.chosen: list[str] = []

    async def _submit_bundle(self, event, bundle) -> None:
        self.chosen.append("links")

    async def _submit_inbound(self, event) -> None:
        self.chosen.append("media")


class TestDispatchOrder:
    """Each branch looks at exactly one thing, in priority order (AUDIT A7)."""

    def make(self, config, monkeypatch, *, media: bool) -> RecordingHandlers:
        monkeypatch.setattr(handlers_module, "has_downloadable_media", lambda _m: media)
        return RecordingHandlers(
            bot=object(), config=config, db=object(), queue=object(),
            pikpak=object(), portal=object(),
        )

    async def test_links_win_over_attached_media(self, config, monkeypatch):
        handlers = self.make(config, monkeypatch, media=True)
        await handlers.on_message(FakeEvent("https://t.me/durov/1"))
        assert handlers.chosen == ["links"]

    async def test_media_alone_is_taken_in(self, config, monkeypatch):
        handlers = self.make(config, monkeypatch, media=True)
        await handlers.on_message(FakeEvent("look at this"))
        assert handlers.chosen == ["media"]

    async def test_media_wins_over_a_broken_link(self, config, monkeypatch):
        handlers = self.make(config, monkeypatch, media=True)
        await handlers.on_message(FakeEvent("https://t.me/durov"))
        assert handlers.chosen == ["media"]

    async def test_only_a_broken_link_is_explained(self, config, monkeypatch):
        handlers = self.make(config, monkeypatch, media=False)
        event = FakeEvent("https://t.me/durov")
        await handlers.on_message(event)
        assert handlers.chosen == []
        assert "no message id" in event.replies[0]

    async def test_nothing_usable_gets_the_prompt(self, config, monkeypatch):
        handlers = self.make(config, monkeypatch, media=False)
        event = FakeEvent("hello")
        await handlers.on_message(event)
        assert handlers.chosen == []
        assert len(event.replies) == 1


class HtmlEvent(FakeEvent):
    """Also records the parse mode, and the chat type can be set."""

    def __init__(self, text: str, *, user_id: int = ADMIN, private: bool = True) -> None:
        super().__init__(text, user_id=user_id)
        self.is_private = private
        self.parse_modes: list[str | None] = []

    async def reply(self, text, **kwargs):
        self.parse_modes.append(kwargs.get("parse_mode"))
        return await super().reply(text, **kwargs)


class FakePikPakService:
    user_login_allowed = True

    async def available_for(self, user_id):
        return False

    async def has_user_session(self, user_id):
        return False


class FakePortal:
    miniapp_url = "https://media.example.com/pikpak/app"

    def unavailable_reason(self):
        return None


class StartsWizard(FakeWizard):
    def __init__(self) -> None:
        super().__init__()
        self.begun: list[str] = []

    async def begin_pikpak(self, event) -> None:
        self.begun.append("pikpak")

    async def begin_telegram(self, event) -> None:
        self.begun.append("telegram")


class FakeDb:
    async def get_user(self, user_id):
        return None


MEMBER = 777


@pytest.fixture
def full(config):
    config.access.allowed_user_ids = [MEMBER]
    handlers = BotHandlers(
        bot=object(), config=config, db=FakeDb(), queue=object(),
        pikpak=FakePikPakService(), portal=FakePortal(),
    )
    handlers.attach_wizard(StartsWizard())
    return handlers


class TestCommands:
    async def test_an_escaped_reply_is_sent_as_html(self, full):
        # Escaped text sent as plain text shows the user a literal "&lt;".
        event = HtmlEvent("/mode <script>")
        await full.on_mode(event)
        assert "&lt;script&gt;" in event.replies[0]
        assert event.parse_modes == ["html"]

    async def test_pikpak_login_in_a_group_asks_for_a_private_chat(self, full):
        # Telegram rejects a web_app button there, so the user used to get
        # no answer at all.
        event = HtmlEvent("/pikpak login", private=False)
        await full.on_pikpak(event)
        assert "directly" in event.replies[0]

    async def test_pikpak_login_in_private_offers_the_mini_app(self, full):
        event = HtmlEvent("/pikpak login")
        await full.on_pikpak(event)
        assert len(event.replies) == 1
        assert "directly" not in event.replies[0]

    async def test_any_allowed_user_may_connect_their_own_pikpak(self, full):
        # It writes the sender's own token, exactly as the Mini App does.
        await full.on_setup(HtmlEvent("/setup pikpak", user_id=MEMBER))
        assert full._wizard.begun == ["pikpak"]  # noqa: SLF001

    async def test_only_an_admin_may_sign_in_a_reading_account(self, full):
        event = HtmlEvent("/setup telegram", user_id=MEMBER)
        await full.on_setup(event)
        assert full._wizard.begun == []  # noqa: SLF001
        assert "admin" in event.replies[0]

    async def test_an_admin_may_sign_in_a_reading_account(self, full):
        await full.on_setup(HtmlEvent("/setup telegram"))
        assert full._wizard.begun == ["telegram"]  # noqa: SLF001


class RecordingDb(FakeDb):
    def __init__(self) -> None:
        self.modes: list[str] = []

    async def set_user_mode(self, user_id, mode):
        self.modes.append(mode)


class TestModes:
    async def test_auto_can_be_chosen(self, full):
        db = RecordingDb()
        full._db = db  # noqa: SLF001
        event = HtmlEvent("/mode auto")
        await full.on_mode(event)
        assert db.modes == ["auto"]
