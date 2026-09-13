"""Inline keyboard construction, isolated because it is layer-specific.

Telegram's schema layer 229, which Telethon 1.45 speaks, replaced the old
family of ``KeyboardButtonUrl`` / ``KeyboardButtonWebView`` constructors with
one ``KeyboardInlineButton`` carrying a separate type object. Code written
against the older names imports fine from memory and then fails at runtime,
so the construction lives here alone and is covered by tests: a Telethon
upgrade that moves it again breaks a test rather than the first button press.
"""

from __future__ import annotations

from telethon.tl.types import (
    InlineButtonTypeUrl,
    InlineButtonTypeWebView,
    KeyboardInlineButton,
    KeyboardInlineButtonRow,
    ReplyInlineMarkup,
)


def webview_button(text: str, url: str) -> ReplyInlineMarkup:
    """A button that opens ``url`` as a Mini App inside the Telegram client.

    Telegram only accepts HTTPS URLs here, and silently does nothing with
    anything else, so callers must not offer this for a plain-HTTP address.
    """
    return ReplyInlineMarkup(
        [
            KeyboardInlineButtonRow(
                [KeyboardInlineButton(text=text, type=InlineButtonTypeWebView(url=url))]
            )
        ]
    )


def url_button(text: str, url: str) -> ReplyInlineMarkup:
    """A button that opens ``url`` in the viewer's browser."""
    return ReplyInlineMarkup(
        [
            KeyboardInlineButtonRow(
                [KeyboardInlineButton(text=text, type=InlineButtonTypeUrl(url=url))]
            )
        ]
    )


def rows(*buttons: tuple[str, str, bool]) -> ReplyInlineMarkup:
    """Stack several buttons vertically.

    Each entry is ``(text, url, as_webview)``; a web-view entry opens inside
    Telegram, a plain one opens the browser.
    """
    return ReplyInlineMarkup(
        [
            KeyboardInlineButtonRow(
                [
                    KeyboardInlineButton(
                        text=text,
                        type=(
                            InlineButtonTypeWebView(url=url)
                            if as_webview
                            else InlineButtonTypeUrl(url=url)
                        ),
                    )
                ]
            )
            for text, url, as_webview in buttons
        ]
    )
