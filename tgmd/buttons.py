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
