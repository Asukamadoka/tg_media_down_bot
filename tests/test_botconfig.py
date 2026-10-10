"""The bot's self-configuration.

The command menu is the thing a user sees first, and the easiest thing to let
drift. The drift test below is the point of this file: a command added to the
handlers but not the menu, or left in the menu after its handler is gone,
fails here.
"""

from __future__ import annotations

import re

import pytest

from tgmd import botconfig
from tgmd.handlers import BotHandlers

# Telegram's documented limits for a command entry.
_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_DESCRIPTION_LIMIT = 256

# Handler methods that are not slash commands.
_NOT_COMMANDS = {"on_message"}


def registered_commands() -> set[str]:
    """Command names implied by the handler methods, as `on_<name>`."""
    return {
        name[len("on_") :]
        for name in dir(BotHandlers)
        if name.startswith("on_") and name not in _NOT_COMMANDS
    }


class TestMenuMatchesHandlers:
    def test_every_menu_entry_has_a_handler(self):
        missing = {name for name, _ in botconfig.COMMANDS} - registered_commands()
        assert not missing, f"menu lists commands with no handler: {missing}"

    def test_every_handler_is_in_the_menu(self):
        listed = {name for name, _ in botconfig.COMMANDS}
        missing = registered_commands() - listed
        assert not missing, f"handlers not offered in the menu: {missing}"

    def test_start_is_handled_even_though_telegram_adds_it_itself(self):
        # /start shares on_help; Telegram always shows it, so it is not listed.
        assert hasattr(BotHandlers, "on_help")
        assert "start" not in {name for name, _ in botconfig.COMMANDS}


class TestMenuEntries:
    def test_there_are_commands(self):
        assert botconfig.COMMANDS

    def test_no_duplicates(self):
        names = [name for name, _ in botconfig.COMMANDS]
        assert len(names) == len(set(names))

    def test_help_comes_first(self):
        assert botconfig.COMMANDS[0][0] == "help"

    @pytest.mark.parametrize("name", [name for name, _ in botconfig.COMMANDS])
    def test_names_are_valid(self, name):
        assert _NAME_RE.match(name), f"{name!r} is not a valid command name"

    @pytest.mark.parametrize(
        ("name", "description"), list(botconfig.COMMANDS)
    )
    def test_descriptions_are_usable(self, name, description):
        assert description, f"{name} has no description"
        assert len(description) <= _DESCRIPTION_LIMIT
        assert "\n" not in description


class TestProfileText:
    def test_about_fits(self):
        assert len(botconfig.ABOUT) <= botconfig.ABOUT_LIMIT

    def test_description_fits(self):
        assert len(botconfig.DESCRIPTION) <= botconfig.DESCRIPTION_LIMIT

    def test_description_points_at_setup(self):
        assert "/setup" in botconfig.DESCRIPTION

    def test_manual_steps_are_explained(self):
        assert botconfig.MANUAL_STEPS
        for label, why in botconfig.MANUAL_STEPS:
            assert label and why
            # Each entry has to say why it matters, not just name a setting.
            assert len(why) > 30


class TestCommandList:
    def test_objects_serialise(self):
        # Proves these are well-formed TL objects that would go on the wire.
        for command in botconfig.command_list():
            assert command._bytes()  # noqa: SLF001 - serialising is the point of the test

    def test_order_is_preserved(self):
        built = [command.command for command in botconfig.command_list()]
        assert built == [name for name, _ in botconfig.COMMANDS]


class FakeClient:
    """Records the requests apply() sends."""

    def __init__(self, *, fail: type[Exception] | None = None) -> None:
        self.requests: list[object] = []
        self._fail = fail

    async def __call__(self, request):
        if self._fail is not None:
            raise self._fail("nope")
        self.requests.append(request)
        return None


class TestApply:
    async def test_it_sends_both_requests(self):
        client = FakeClient()
        applied = await botconfig.apply(client)
        assert len(client.requests) == 2
        assert any("command menu" in item for item in applied)
        assert "profile text" in applied

    async def test_the_menu_request_carries_every_command(self):
        client = FakeClient()
        await botconfig.apply(client)
        menu = client.requests[0]
        assert len(menu.commands) == len(botconfig.COMMANDS)

    async def test_the_profile_request_carries_both_texts(self):
        client = FakeClient()
        await botconfig.apply(client)
        info = client.requests[1]
        assert info.about == botconfig.ABOUT
        assert info.description == botconfig.DESCRIPTION

    async def test_a_failure_does_not_raise(self):
        # Startup must not die because a cosmetic call was rate-limited.
        assert await botconfig.apply(FakeClient(fail=RuntimeError)) == []
