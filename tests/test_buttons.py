"""Inline keyboard construction.

Telegram's schema layer changes these constructors, and a wrong name imports
fine from memory then fails at runtime on the first button press. Serialising
each markup proves it is a well-formed TL object that would actually go on
the wire, not merely that the attribute names exist.
"""

from __future__ import annotations

from telethon.tl.types import (
    InlineButtonTypeUrl,
    InlineButtonTypeWebView,
    ReplyInlineMarkup,
)

from tgmd.buttons import rows, url_button, webview_button

URL = "https://media.example.com/pikpak/app"


class TestWebviewButton:
    def test_shape(self):
        markup = webview_button("Connect", URL)
        assert isinstance(markup, ReplyInlineMarkup)
        assert len(markup.rows) == 1
        button = markup.rows[0].buttons[0]
        assert button.text == "Connect"
        assert isinstance(button.type, InlineButtonTypeWebView)
        assert button.type.url == URL

    def test_serialises(self):
        # If the layer moves again, this is where it shows up.
        assert webview_button("Connect", URL)._bytes()  # noqa: SLF001 - serialising is the point of the test


class TestUrlButton:
    def test_shape(self):
        button = url_button("Open", URL).rows[0].buttons[0]
        assert isinstance(button.type, InlineButtonTypeUrl)
        assert button.type.url == URL

    def test_serialises(self):
        assert url_button("Open", URL)._bytes()  # noqa: SLF001 - serialising is the point of the test


class TestRows:
    def test_one_row_per_button(self):
        markup = rows(("A", URL, True), ("B", "https://example.com", False))
        assert len(markup.rows) == 2
        assert isinstance(markup.rows[0].buttons[0].type, InlineButtonTypeWebView)
        assert isinstance(markup.rows[1].buttons[0].type, InlineButtonTypeUrl)

    def test_empty_is_allowed(self):
        assert rows().rows == []

    def test_serialises(self):
        assert rows(("A", URL, True), ("B", URL, False))._bytes()  # noqa: SLF001 - serialising is the point of the test
